"""Flussi salvati (DAG dell'editor), contenuti nei progetti.

Permessi (ereditati dall'albero come sempre):
- lista/apertura: VIEW sul progetto del flusso;
- creazione/salvataggio/eliminazione: EDIT;
- spostamento: EDIT sia sul progetto di origine sia su quello di destinazione.
"""
import json
import unicodedata
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, status, Request, Response
from sqlalchemy import func, or_
from sqlmodel import Session, select

from app.core.config import get_settings
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Connection, Datasource, Flow, FlowVersion, Project, Run, User
from app.services.dbt_export import OpzioniDbt
from app.core.lingua import lingua_della_richiesta
from app.models.permission import Capability
from app.services import ai_chats, audit, flow_presence, openlineage, run_history
from pydantic import BaseModel, Field
from app.schemas.models import UtcDateTime
from app.schemas.models import (
    FlowCreate,
    FlowDetail,
    FlowOut,
    FlowScheduleUpdate,
    FlowStatsOut,
    FlowUpdate,
    FlowVersionOut,
    Page,
)
from app.services.engine_policy import KNOWN_ENGINES, disabled_engines
from app.services.pagination import paginate
from app.services import permissions as perm_service
from app.services import schedule_authority
from app.services.objects import collect_storage_keys, ensure_can_read_keys
from app.services.schedule import ScheduleError, next_fire, validate_schedule
from app.services.copy_names import free_copy_name
from app.core.routing import RottaCheRilascia

router = APIRouter(route_class=RottaCheRilascia, tags=["flows"])

# engine SELEZIONABILI alla creazione (sincronizzato col catalogo dell'engine:
# solo quelli `available=True`).
# `clickhouse` (server esterno) è opzionale lato engine: se non configurato, il
# run fallisce con un errore chiaro dell'engine.
_AVAILABLE_ENGINES = set(KNOWN_ENGINES)


def _validate_engine(engine: str | None, session: Session | None = None) -> str:
    """Il motore esiste nel catalogo e — se si passa la sessione — è fra quelli
    che l'amministratore consente ancora di scegliere.

    `session` è facoltativa di proposito: il controllo sul CATALOGO è puro e
    resta utilizzabile senza database (lo usano i test di validazione). Il
    criterio amministrativo si applica dove una sessione c'è, cioè in tutte le
    rotte — creazione, cambio motore e motore di produzione, che sono i tre
    punti in cui un motore si sceglie."""
    e = (engine or "polars").strip().lower()
    if e not in _AVAILABLE_ENGINES:
        raise HTTPException(
            status_code=422,
            detail=f"Engine not available: '{engine}'. Choose from: {', '.join(sorted(_AVAILABLE_ENGINES))}.",
        )
    vietati = disabled_engines(session) if session is not None else set()
    if e in vietati:
        raise HTTPException(
            status_code=422,
            detail=f"The engine '{e}' has been disabled by the administrator. "
            f"Choose from: {', '.join(sorted(_AVAILABLE_ENGINES - vietati))}.",
        )
    return e


def _validate_production_engine(engine: str | None, session: Session | None = None) -> str | None:
    """Motore di produzione: vuoto = None (uguale allo sviluppo)."""
    if engine is None or not engine.strip():
        return None
    return _validate_engine(engine, session)


# nodi che danno "qualcosa da eseguire": Output, oppure i nodi di controllo
# (refresh di una datasource, esecuzione di un altro flusso)
_RUNNABLE_NODE_TYPES = {"output", "refresh", "runflow"}


def _has_runnable_nodes(definition: str | None) -> bool:
    try:
        parsed = json.loads(definition or "{}")
    except json.JSONDecodeError:
        return False
    return any((n or {}).get("type") in _RUNNABLE_NODE_TYPES for n in parsed.get("nodes") or [])


def _authorize_definition_keys(session: Session, user: User, definition: str | None) -> None:
    """Le chiavi di storage nominate nella definition di un flusso devono essere
    LEGGIBILI da chi salva. Senza questo controllo la regola di lettura per-oggetto
    (una chiave in un flusso leggibile è leggibile) sarebbe auto-autorizzante:
    bastava scrivere la chiave altrui nel proprio flusso per guadagnarne l'accesso.
    """
    if not definition:
        return
    try:
        parsed = json.loads(definition)
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="The flow definition is not valid JSON")
    ensure_can_read_keys(session, user, collect_storage_keys(parsed))
    # A node can name a datasource by id alone: the run resolves the id to its file
    # with the authority of whoever runs it, which for a schedule is its author.
    # Without this check, anyone with EDIT on a folder could add a datasource they
    # cannot read to someone else's scheduled flow, and the next scheduled run would
    # copy it into a folder they can read.
    ids = _datasource_ids(parsed)
    if ids:
        readable = perm_service.readable_project_ids(session, user)
        hidden = [ds_id for ds_id, project_id in session.exec(
            select(Datasource.id, Datasource.project_id).where(Datasource.id.in_(ids))
        ).all() if project_id not in readable]
        if hidden:
            raise HTTPException(status_code=403, detail="The flow reads a datasource you cannot read")


def _datasource_ids(payload) -> set[int]:
    """Every `datasourceId` in a flow definition, however deep (nested operations too)."""
    found: set[int] = set()

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "datasourceId" and isinstance(value, (int, str)) and str(value).isdigit():
                    found.add(int(value))
                else:
                    walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(payload)
    return found


@router.get("/flows", response_model=list[FlowOut])
def list_all_flows(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Tutti i flussi nei progetti LEGGIBILI (per la pagina globale Flows).

    `readable_project_ids`, non `visible_project_ids`: gli antenati mostrati
    per navigazione non danno accesso al loro contenuto.
    """
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return []
    return session.exec(
        select(Flow).where(Flow.project_id.in_(readable)).order_by(Flow.name)
    ).all()


# NB: registrato PRIMA di /flows/{flow_id} così "search" non è preso per un id
@router.get("/flows/search", response_model=Page[FlowOut])
def search_flows(
    q: str | None = Query(None, description="cerca su nome/descrizione, sull'INTERO dataset"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Flussi nei progetti leggibili, PAGINATI, con ricerca server-side sul
    dataset intero. Per la pagina globale Flows."""
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return Page(items=[], total=0)
    base = select(Flow).where(Flow.project_id.in_(readable))
    if q:
        like = f"%{q}%"
        base = base.where(or_(Flow.name.ilike(like), Flow.description.ilike(like)))
    flows, total = paginate(session, base, Flow.name, limit, offset)
    return Page(items=_with_owner_names(session, flows), total=total)


def _with_owner_names(session: Session, flows: list[Flow]) -> list[FlowOut]:
    """Serializza i flussi risolvendo `owner_name` (nome di chi li ha creati) e lo
    stato dell'ULTIMO run, ciascuno con una sola query aggregata sui flussi della
    pagina."""
    owner_ids = {f.owner_id for f in flows if f.owner_id is not None}
    names: dict[int, str] = {}
    if owner_ids:
        for uid, full_name, email in session.exec(
            select(User.id, User.full_name, User.email).where(User.id.in_(owner_ids))
        ).all():
            names[uid] = full_name or email

    # the latest run of each flow on the page, from the index: reading all their runs
    # sorted the whole history of the page's flows on every page open
    last_run = run_history.latest_flow_runs(session, (f.id for f in flows if f.id is not None))

    out: list[FlowOut] = []
    for f in flows:
        item = FlowOut.model_validate(f, from_attributes=True)
        item.owner_name = names.get(f.owner_id) if f.owner_id is not None else None
        lr = last_run.get(f.id)
        if lr:
            item.last_run_status, item.last_run_at = lr
        out.append(item)
    return out


def _get_flow(session: Session, flow_id: int) -> Flow:
    flow = session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    return flow


@router.get("/projects/{project_id}/flows", response_model=list[FlowOut])
def list_flows(project_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.VIEW)
    return session.exec(select(Flow).where(Flow.project_id == project_id).order_by(Flow.name)).all()


def _check_flow_name(name: str) -> None:
    """A flow's name ends up in the Outputs' templates ({flow}: a subject, a file
    name): no control character (a line break, a tab) in a new or renamed one."""
    if any(ord(c) < 32 or ord(c) == 127 for c in name or ""):
        raise HTTPException(status_code=422, detail="The flow's name cannot hold a line break, a tab or another control character")


@router.post("/projects/{project_id}/flows", response_model=FlowDetail, status_code=status.HTTP_201_CREATED)
def create_flow(
    project_id: int,
    body: FlowCreate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.EDIT)
    _check_flow_name(body.name)
    _authorize_definition_keys(session, user, body.definition)
    flow = Flow(
        name=body.name,
        description=body.description,
        definition=body.definition,
        project_id=project_id,
        owner_id=user.id,
        engine=_validate_engine(body.engine, session),
        production_engine=_validate_production_engine(body.production_engine, session),
    )
    session.add(flow)
    session.commit()
    session.refresh(flow)
    _snapshot_version(session, flow, user, note="created")
    audit.record_audit(
        session, actor=user, action=audit.FLOW_CREATE, target_type="flow",
        target_id=flow.id, target_label=flow.name,
        detail={"project_id": project_id, "engine": flow.engine}, request=request,
    )
    return flow


@router.post("/flows/{flow_id}/duplicate", response_model=FlowDetail, status_code=status.HTTP_201_CREATED)
def duplicate_flow(
    flow_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """A copy of the flow in its folder, as «name (i)», under the same rules as a new
    flow. The definition is copied as it is, Output nodes included: whoever made the
    copy renames its outputs before running it. Schedule and failure alerts stay
    with the original."""
    src = _get_flow(session, flow_id)
    ensure_can(session, user, src.project_id, Capability.EDIT)
    _authorize_definition_keys(session, user, src.definition)
    flow = Flow(
        name=free_copy_name(session, Flow, src.project_id, src.name),
        description=src.description,
        definition=src.definition,
        project_id=src.project_id,
        owner_id=user.id,
        engine=_validate_engine(src.engine, session),
        production_engine=_validate_production_engine(src.production_engine, session),
    )
    session.add(flow)
    session.commit()
    session.refresh(flow)
    _snapshot_version(session, flow, user, note=f"copy of '{src.name}'")
    audit.record_audit(
        session, actor=user, action=audit.FLOW_CREATE, target_type="flow",
        target_id=flow.id, target_label=flow.name,
        detail={"project_id": flow.project_id, "engine": flow.engine, "duplicate_of": src.id}, request=request,
    )
    return flow


def _snapshot_version(session: Session, flow: Flow, user: User, note: str) -> None:
    """Auto-versione: registra la definizione CORRENTE del flusso come nuova
    versione. Salta se identica all'ultima (un salvataggio che non cambia il DAG
    non crea rumore nello storico)."""
    last = session.exec(
        select(FlowVersion)
        .where(FlowVersion.flow_id == flow.id)
        .order_by(FlowVersion.version.desc())
    ).first()
    if last is not None and last.definition == flow.definition:
        return
    session.add(
        FlowVersion(
            flow_id=flow.id,
            version=(last.version + 1 if last else 1),
            definition=flow.definition,
            note=note,
            created_by=user.id,
        )
    )
    session.commit()


def _flusso_per_dbt(session: Session, user: User, flow_id: int) -> Flow:
    """Solo per gli amministratori (effettivi: flag personale o gruppo admin; NON gli
    osservatori). Il progetto è autosufficiente: porta dentro i flussi a monte, il
    testo delle query e, come seed, i DATI delle sorgenti che non stanno in un
    database — anche di cartelle che chi vede il flusso non vede. È uno strumento
    del team data, non dell'utente comune (scelta dell'utente, 2026-10-10)."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    if not perm_service.is_admin(session, user):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The dbt export is for administrators only: the project carries data and definitions from other folders too",
        )
    return flow


@router.get("/flows/{flow_id}/export/dbt")
async def export_flow_dbt(
    flow_id: int,
    request: Request,
    target: str = Query("duckdb", pattern="^(duckdb|native|clickhouse)$"),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Esporta il flusso come progetto dbt (zip). `target=duckdb` (federazione),
    `native` (warehouse di origine, SQL tradotto via sqlglot) o `clickhouse` (il
    ClickHouse del cliente, che legge le sorgenti Postgres/MySQL dal vivo). Il gateway
    risolve flusso→operazioni e sorgenti→tabelle DB, l'engine compila e zippa.
    Senza opzioni: il progetto completo con i valori di default (vedi POST)."""
    return await _esporta_dbt(_flusso_per_dbt(session, user, flow_id), request, user, session, target, None)


@router.post("/flows/{flow_id}/export/dbt")
async def export_flow_dbt_con_opzioni(
    flow_id: int,
    opzioni: OpzioniDbt,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """L'export dbt con le scelte del dialogo di download: target, progetto completo o
    cartella per un progetto esistente, nomi, sorgenti già dichiarate, schema,
    materializzazioni, test, email (vedi `OpzioniDbt`)."""
    return await _esporta_dbt(_flusso_per_dbt(session, user, flow_id), request, user, session, opzioni.target, opzioni)


@router.get("/flows/{flow_id}/export/dbt/plan")
def export_flow_dbt_plan(
    flow_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Quello che serve al dialogo di download: i target possibili (o perché no, nella
    lingua di chi chiede), le sorgenti di ciascuno col nome di default, le uscite con le
    materializzazioni ammesse."""
    from app.services.dbt_export import piano_export

    from app.services.dbt_ai import modello_ai, testi_della_versione

    flow = _flusso_per_dbt(session, user, flow_id)
    piano = piano_export(session, flow, get_settings().engine.bucket, lingua_della_richiesta(request))
    modello = modello_ai(session)
    # `stored`: what the AI wrote for this version, which a re-export reuses without a call even with
    # the AI switched off: the dialog can still offer it, and the same version gives the same zip
    piano["ai"] = {"available": modello is not None, "model": modello, "stored": testi_della_versione(session, flow)}
    return piano


async def _esporta_dbt(flow: Flow, request: Request, user: User, session: Session, target: str, opzioni: OpzioniDbt | None):
    from app.services import dbt_ai
    from app.services.dbt_export import DbtExportError, _costruisci, engine_export
    from app.services.messaggi_dbt import messaggio_dell_engine, messaggio_dell_errore, traduci

    lingua = lingua_della_richiesta(request)
    scadenza = dbt_ai.nuova_scadenza()   # one budget for every AI call of this export: the ingress cuts it at 300 s
    try:
        payload, progetto = _costruisci(session, flow, get_settings().engine.bucket, target, user.email, opzioni)
        if opzioni is not None and opzioni.ai_descriptions:
            # descrizioni dove mancano, scritte dall'AI e ricordate per versione (services/dbt_ai.py)
            await dbt_ai.descrivi(session, user, flow, sorted(progetto.flussi_visti), payload, scadenza)
    except DbtExportError as e:
        raise HTTPException(status_code=422, detail=messaggio_dell_errore(e, lingua))

    proposte: dict[str, str] = {}   # modello → chiave della traduzione proposta dall'AI
    scrittori: dict[str, str] = {}  # model → the AI model that wrote its translation
    tentativi: dict[str, int] = {}  # modello → proposte chieste
    richieste: dict[str, dict] = {}  # modello → quello che l'engine ha detto servire alla traduzione
    while True:
        if proposte:
            # checking an AI proposal is one more engine call: past the budget, the ingress would cut the
            # request with a bare 504 after the AI used most of its time
            try:
                dbt_ai.controlla_scadenza(scadenza)
            except DbtExportError as e:
                raise HTTPException(status_code=422, detail=messaggio_dell_errore(e, lingua))
        try:
            resp = await engine_export(payload)   # bounded: ENGINE__DBT_EXPORT_TIMEOUT_SECONDS
        except DbtExportError as e:
            # the engine, upstream of the gateway, did not answer in time (504) or cannot be reached (502)
            raise HTTPException(status_code=504 if e.code == "engine_timeout" else 502, detail=messaggio_dell_errore(e, lingua))
        if resp.status_code == 200:
            break
        try:
            grezzo = resp.json().get("detail")
        except Exception:
            grezzo = None
        prm = (grezzo.get("params") or {}) if isinstance(grezzo, dict) else {}
        modello = prm.get("model")
        if (opzioni is not None and opzioni.ai_translations and isinstance(grezzo, dict) and grezzo.get("code") in dbt_ai.TRADUCIBILI
                and modello and modello not in proposte and len(proposte) < dbt_ai.MAX_TRADUZIONI):
            # l'engine non traduce questo modello: una proposta dell'AI, che l'engine poi verifica
            try:
                proposta, proposte[modello], scrittori[modello] = await dbt_ai.traduci(session, user, flow, prm, scadenza=scadenza)
            except DbtExportError as e:
                raise HTTPException(status_code=422, detail=messaggio_dell_errore(e, lingua))
            tentativi[modello] = 1
            richieste[modello] = prm
            payload.setdefault("overrides", {})[modello] = proposta
            continue
        if isinstance(grezzo, dict) and grezzo.get("code") == "ai_translation_invalid" and prm.get("model") in proposte:
            modello = prm["model"]
            dbt_ai.dimentica(session, flow, proposte[modello], payload["overrides"][modello])   # scartata: non si riusa
            if tentativi.get(modello, 0) < dbt_ai.TENTATIVI:
                # un secondo tentativo, con davanti il motivo (nella lingua dell'AI: inglese)
                try:
                    proposta, proposte[modello], scrittori[modello] = await dbt_ai.traduci(
                        session, user, flow, richieste[modello],
                        scartata=(payload["overrides"][modello], messaggio_dell_engine(grezzo, "en")), scadenza=scadenza)
                except DbtExportError as e:
                    raise HTTPException(status_code=422, detail=messaggio_dell_errore(e, lingua))
                tentativi[modello] += 1
                payload["overrides"][modello] = proposta
                continue
        detail = messaggio_dell_engine(grezzo, lingua) if grezzo is not None else traduci("internal", {"detail": f"engine {resp.status_code}"}, lingua)
        raise HTTPException(status_code=resp.status_code if resp.status_code >= 400 else 502, detail=detail)

    fname = "".join(c if (c.isalnum() or c in "-_") else "_" for c in flow.name).strip("_") or "flow"
    # the dialog's own name when it cannot read this header: `_folder` for the folder package
    coda = f"_dbt_{target}{'_folder' if opzioni is not None and opzioni.package == 'folder' else ''}.zip"
    # the response first, the audit row after: a download is recorded only once it can be served
    risposta = Response(
        content=resp.content, media_type="application/zip",
        headers={"Content-Disposition": _content_disposition(fname, coda)},
    )
    # the AI models that wrote what the zip carries, not the one configured now
    scritto_da = {m for m in ((payload.get("ai") or {}).get("model") or "").split(", ") if m} | {m for m in scrittori.values() if m}
    audit.record_audit(
        session, actor=user, action=audit.EXPORT_DOWNLOAD,
        target_type="flow", target_id=flow.id, target_label=flow.name,
        detail={"format": "dbt", "target": target, **({"options": opzioni.model_dump(by_alias=True)} if opzioni else {}),
                **({"ai": {"model": ", ".join(sorted(scritto_da)) or None, "translated": sorted(proposte)}} if (payload.get("ai") or proposte) else {})},
        request=request,
    )
    return risposta


def _content_disposition(base: str, coda: str) -> str:
    """`attachment` with the file name twice: in ASCII for old clients (accents
    transliterated, other letters `_`), and as RFC 5987 `filename*` in UTF-8 with every
    letter. HTTP headers are Latin-1: a raw «Ł» or «销» there was a 500."""
    ascii_ = "".join(c if c.isascii() else ("" if unicodedata.combining(c) else "_")
                     for c in unicodedata.normalize("NFKD", base)).strip("_") or "flow"
    return f"attachment; filename=\"{ascii_}{coda}\"; filename*=UTF-8''{quote(base + coda, safe='')}"


# ── Presenza nell'editor ───────────────────────────────────────────────────────
class PresenceBeat(BaseModel):
    # id dell'ISTANZA dell'editor (una per scheda), generato dal browser
    instance: str = Field(min_length=4, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class PresenceOther(BaseModel):
    user_id: int
    email: str
    full_name: str = ""
    since: UtcDateTime


class PresenceOut(BaseModel):
    others: list[PresenceOther]
    heartbeat_seconds: int


@router.post("/flows/{flow_id}/presence", response_model=PresenceOut)
def flow_presence_beat(
    flow_id: int,
    body: PresenceBeat,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """"Ho questo flusso aperto": il battito dell'editor. Risponde con chi ALTRO
    ce l'ha aperto. Non blocca e non impedisce nulla — avvisa, perche' l'ultimo
    che salva sovrascrive l'altro. Chi vede il flusso vede anche chi lo sta
    guardando; nessun altro."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    others = flow_presence.beat(session, flow_id, body.instance, user.id, user.email, user.full_name or "")
    if perm_service.is_observer_only(session, user):
        # an observer reads people masked everywhere else (audit, users): here too
        from app.services.masking import mask_email

        others = [{**o, "email": mask_email(o["email"]), "full_name": ""} for o in others]
    return PresenceOut(
        others=[PresenceOther(**{**o, "since": datetime.fromtimestamp(o["since"], timezone.utc)}) for o in others],
        heartbeat_seconds=flow_presence.HEARTBEAT_SECONDS,
    )


@router.delete("/flows/{flow_id}/presence/{instance}", status_code=status.HTTP_204_NO_CONTENT)
def flow_presence_leave(
    flow_id: int,
    instance: str,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    flow_presence.leave(session, flow_id, instance)


@router.get("/flows/{flow_id}", response_model=FlowDetail)
def get_flow(flow_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    return flow


def _guard_someone_elses_schedule(flow: Flow, user: User, new_definition: str | None,
                                   moved: bool) -> dict | None:
    """A schedule carries its author's authority. An edit by someone else that makes
    the flow reach more (datasources, files, flows it runs, connections, publish
    folders), or a move to another folder, would spend that authority on something
    the author never chose: the schedule is switched off. Returns the audit detail
    for the caller to record once the change is committed (`_record_schedule_off`)."""
    if not flow.run_schedule or flow.run_scheduled_by in (None, user.id):
        return None
    reason = None
    if moved:
        reason = "the flow was moved to another folder"
    elif new_definition is not None:
        nuove = schedule_authority.widens(flow.definition, new_definition)
        if nuove:
            reason = f"the definition now reads or writes more ({len(nuove)} new reference(s))"
    if reason is None:
        return None
    author = flow.run_scheduled_by
    flow.run_schedule = None
    flow.run_scheduled_by = None
    flow.next_run_at = None
    return {"turned_off": True, "reason": reason, "author": author}


def _record_schedule_off(session: Session, user: User, flow: Flow, detail: dict | None, request) -> None:
    if detail:
        audit.record_audit(session, actor=user, action=audit.FLOW_SCHEDULE, target_type="flow",
                           target_id=flow.id, target_label=flow.name, detail=detail, request=request)


@router.patch("/flows/{flow_id}", response_model=FlowDetail)
def update_flow(
    flow_id: int,
    body: FlowUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.EDIT)
    if body.definition is not None:
        _authorize_definition_keys(session, user, body.definition)
    if body.name is not None:
        body.name = body.name.strip()
        if not body.name:
            raise HTTPException(status_code=422, detail="The flow needs a name")
        if body.name != flow.name:  # a name saved before is left as it is
            _check_flow_name(body.name)
    changed = [k for k in ("name", "description", "definition", "project_id", "engine", "production_engine")
               if getattr(body, k, None) is not None]
    # the folder path is part of the flow's name in an external lineage catalog
    lineage_names = openlineage.catalog_names(session, flows=[flow]) if body.name is not None or body.project_id is not None else {}

    moved = body.project_id is not None and body.project_id != flow.project_id
    if moved:
        # spostamento: serve EDIT anche sulla cartella di destinazione
        if session.get(Project, body.project_id) is None:
            raise HTTPException(status_code=404, detail="Destination folder not found")
        ensure_can(session, user, body.project_id, Capability.EDIT)
    schedule_off = _guard_someone_elses_schedule(flow, user, body.definition, moved)
    if moved:
        flow.project_id = body.project_id

    if body.name is not None:
        flow.name = body.name
    if body.description is not None:
        flow.description = body.description
    if body.definition is not None:
        flow.definition = body.definition
    if body.engine is not None:
        flow.engine = _validate_engine(body.engine, session)
    if body.production_engine is not None:
        flow.production_engine = _validate_production_engine(body.production_engine, session)

    flow.updated_at = datetime.now(timezone.utc)
    session.add(flow)
    session.commit()
    session.refresh(flow)
    if body.definition is not None:  # solo i cambi di DAG creano una versione
        _snapshot_version(session, flow, user, note="edited")
    openlineage.renamed(session, lineage_names)
    audit.record_audit(
        session, actor=user, action=audit.FLOW_UPDATE, target_type="flow",
        target_id=flow.id, target_label=flow.name, detail={"changed": changed}, request=request,
    )
    _record_schedule_off(session, user, flow, schedule_off, request)
    session.refresh(flow)
    return flow


@router.get("/flows/{flow_id}/versions", response_model=list[FlowVersionOut])
def list_flow_versions(
    flow_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)
):
    """Storico delle versioni del flusso (la più recente è la corrente)."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    versions = session.exec(
        select(FlowVersion)
        .where(FlowVersion.flow_id == flow_id)
        .order_by(FlowVersion.version.desc())
    ).all()
    current = versions[0].version if versions else None
    # nomi degli autori delle versioni (chi ha rilasciato/modificato)
    uids = {v.created_by for v in versions if v.created_by is not None}
    names: dict[int, str] = {}
    if uids:
        for u in session.exec(select(User).where(User.id.in_(uids))).all():
            names[u.id] = u.full_name or u.email
    return [
        FlowVersionOut(
            version=v.version,
            note=v.note,
            created_at=v.created_at,
            created_by=v.created_by,
            created_by_name=names.get(v.created_by),
            is_current=(v.version == current),
        )
        for v in versions
    ]


@router.post("/flows/{flow_id}/versions/{version}/promote", response_model=FlowDetail)
def promote_flow_version(
    flow_id: int,
    version: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Promuove una versione vecchia a corrente: la sua definizione diventa quella
    del flusso e viene registrata come nuova versione in testa allo storico."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.EDIT)
    target = session.exec(
        select(FlowVersion).where(FlowVersion.flow_id == flow_id, FlowVersion.version == version)
    ).first()
    if target is None:
        raise HTTPException(status_code=404, detail="Version not found")
    _authorize_definition_keys(session, user, target.definition)  # RBAC come su salvataggio
    schedule_off = _guard_someone_elses_schedule(flow, user, target.definition, False)
    flow.definition = target.definition
    flow.updated_at = datetime.now(timezone.utc)
    session.add(flow)
    session.commit()
    session.refresh(flow)
    _snapshot_version(session, flow, user, note=f"promoted from v{version}")
    session.refresh(flow)
    audit.record_audit(
        session, actor=user, action=audit.FLOW_PROMOTE, target_type="flow",
        target_id=flow.id, target_label=flow.name, detail={"from_version": version}, request=request,
    )
    _record_schedule_off(session, user, flow, schedule_off, request)
    session.refresh(flow)
    return flow


@router.get("/flows/{flow_id}/stats", response_model=FlowStatsOut)
def flow_stats(
    flow_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)
):
    """Statistiche d'esecuzione dalla cronologia dei run del flusso (kind flow/
    orchestration): quanti, successi/falliti, ultima esecuzione, tempo medio."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.VIEW)

    def _count(*conds) -> int:
        return session.exec(select(func.count()).select_from(Run).where(*conds)).one()

    run_count = _count(Run.flow_id == flow_id)
    success = _count(Run.flow_id == flow_id, Run.status == "SUCCESS")
    failure = _count(Run.flow_id == flow_id, Run.status == "FAILURE")
    last_run_at = session.exec(select(func.max(Run.started_at)).where(Run.flow_id == flow_id)).one()

    # tempo medio: sugli ultimi 500 run TERMINATI, calcolato in Python (portabile
    # tra Postgres e SQLite, evita EXTRACT/julianday dialettali)
    finished = session.exec(
        select(Run.started_at, Run.finished_at)
        .where(Run.flow_id == flow_id, Run.finished_at.is_not(None))
        .order_by(Run.started_at.desc())
        .limit(500)
    ).all()
    durs = [(f - s).total_seconds() for s, f in finished if s and f and f >= s]
    avg = round(sum(durs) / len(durs), 2) if durs else None
    return FlowStatsOut(
        run_count=run_count,
        success_count=success,
        failure_count=failure,
        last_run_at=last_run_at,
        avg_duration_seconds=avg,
    )


@router.delete("/flows/{flow_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_flow(flow_id: int, request: Request, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.EDIT)
    flow_name = flow.name  # snapshot prima della cancellazione

    # FK da gestire: la cronologia dei run muore col flusso; le datasource
    # pubblicate SOPRAVVIVONO (sono contenuti di catalogo) perdendo solo la
    # provenienza (flow_id → NULL). Statement bulk: eseguiti SUBITO, così
    # l'ordine (prima i referenzianti, poi il flusso) è garantito.
    from sqlalchemy import delete as sa_delete, update as sa_update

    from app.models import Datasource

    session.exec(sa_update(Datasource).where(Datasource.flow_id == flow_id).values(flow_id=None))
    # runs this flow's runs started for other objects (the imports of its Refresh
    # nodes, the outputs of its Run-flow nodes) belong to those objects' history:
    # they stay, no longer attached, or their link would refuse the delete
    own_runs = select(Run.id).where(Run.flow_id == flow_id)
    session.exec(sa_update(Run).where(Run.parent_run_id.in_(own_runs)).values(parent_run_id=None))
    session.exec(sa_delete(Run).where(Run.flow_id == flow_id))
    session.exec(sa_delete(FlowVersion).where(FlowVersion.flow_id == flow_id))
    from app.models import DbtAiText

    session.exec(sa_delete(DbtAiText).where(DbtAiText.flow_id == flow_id))   # i testi AI dei suoi export dbt
    ai_chats.forget_flow(session, flow.id)  # the editor's assistant talked about this flow only
    session.delete(flow)
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.FLOW_DELETE, target_type="flow",
        target_id=flow_id, target_label=flow_name, request=request,
    )


# il 409 di «Esegui ora», nella lingua dell'interfaccia di chi chiede
_GIA_IN_CORSO = {
    "en": "The flow is already queued or running: it can be run again once it has finished.",
    "it": "Il flusso è già in coda o in esecuzione: si potrà rilanciare quando avrà finito.",
    "de": "Der Flow ist bereits in der Warteschlange oder läuft: Er kann erneut gestartet werden, wenn er fertig ist.",
    "es": "El flow ya está en cola o en ejecución: se podrá lanzar de nuevo cuando termine.",
    "fr": "Le flux est déjà en file d'attente ou en cours : il pourra être relancé une fois terminé.",
}


@router.post("/flows/{flow_id}/run-now", status_code=status.HTTP_202_ACCEPTED)
def run_flow_now(
    flow_id: int,
    request: Request,
    mode: str = Query("development", pattern="^(development|production)$"),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Mette in coda l'ORCHESTRAZIONE del flusso (nodi refresh/output/runflow
    nell'ordine degli archi), che un orchestratore esegue con l'autorità dell'utente.
    Serve RUN sul progetto; la RBAC dei singoli passi (CONNECT dei refresh,
    EDIT/CONNECT degli output) è ri-verificata durante l'orchestrazione.

    Crea subito un 'run di orchestrazione' tracciante e ne torna l'id: il frontend
    lo polla (`GET /runs/{id}`) fino a SUCCESS/FAILURE — così anche i flussi senza
    nodo Output (che non producono run propri) hanno uno stato osservabile.

    `mode`: "development" (default) usa il motore dell'editor; "production" usa
    il motore di produzione del flusso — come farebbe lo scheduler — per
    provarlo a mano prima di schedulare."""
    from app.services.orchestrator import create_orchestration_run, flusso_in_corso, in_fila_per_il_flusso, sveglia

    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.RUN)
    # Un lancio in più non partirebbe comunque (l'orchestratore lo chiude come
    # «già in esecuzione»), ma resterebbe una riga in `runs` e un giro della coda
    # a testa: ripetuto, spinge in fondo le esecuzioni degli altri flussi.
    in_fila_per_il_flusso(session, flow.id)  # controllo e accodamento in fila, fra repliche
    if flusso_in_corso(session, flow.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_GIA_IN_CORSO.get(lingua_della_richiesta(request), _GIA_IN_CORSO["en"]),
        )
    run = create_orchestration_run(session, user, flow, engine_mode=mode)
    sveglia()
    audit.record_audit(
        session, actor=user, action=audit.FLOW_RUN, target_type="flow",
        target_id=flow.id, target_label=flow.name, detail={"run_id": run.id, "trigger": "manual", "engine_mode": mode},
        request=request,
    )
    return {"status": "started", "flow_id": flow.id, "run_id": run.id}


def _set_failure_notice(session: Session, user: User, flow: Flow, body: FlowScheduleUpdate) -> None:
    """Chi avvisare quando un'esecuzione programmata fallisce.

    Gli indirizzi passano dalla stessa barriera del nodo email: una connessione
    SMTP può limitare i domini a cui spedisce, e l'avviso non è una scorciatoia
    per aggirarla. E serve CONNECT sulla connessione, come per usarla altrove:
    altrimenti chi ha solo RUN su un flusso potrebbe far spedire dal server di
    posta di qualcun altro."""
    from app.services import notify_targets

    if body.notify_emails is not None:
        flow.notify_emails = ", ".join(notify_targets.parse_recipients(body.notify_emails)) or None

    if body.notify_connection_id is not None:
        if body.notify_connection_id in (0, -1):  # spegne l'avviso
            flow.notify_connection_id = None
        else:
            flow.notify_connection_id = notify_targets.smtp_connection(session, user, body.notify_connection_id).id

    if flow.notify_emails and flow.notify_connection_id:
        notify_targets.ensure_deliverable(session.get(Connection, flow.notify_connection_id), flow.notify_emails.split(", "))


@router.put("/flows/{flow_id}/schedule", response_model=FlowDetail)
def set_flow_schedule(
    flow_id: int,
    body: FlowScheduleUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Imposta/disabilita l'esecuzione schedulata (cron a 5 campi; '' = off).
    Serve RUN sul progetto del flusso, e il flusso deve avere almeno un nodo
    Output (senza, un run schedulato non produrrebbe nulla). Al fire-time il
    gateway ri-risolve la definizione CORRENTE e lancia gli Output con l'autorità
    di chi schedula (EDIT/CONNECT dei singoli output ri-verificati allora)."""
    flow = _get_flow(session, flow_id)
    ensure_can(session, user, flow.project_id, Capability.RUN)

    cron = (body.cron or "").strip()
    if not cron:
        flow.run_schedule = None
        flow.run_scheduled_by = None
        flow.next_run_at = None
    else:
        if not _has_runnable_nodes(flow.definition):
            raise HTTPException(
                status_code=422,
                detail="The flow has nothing to run (Output, refresh or run flow): "
                "add at least one of these nodes before scheduling it",
            )
        try:
            cron = validate_schedule(cron)
        except ScheduleError as e:
            raise HTTPException(status_code=422, detail=str(e))
        flow.run_schedule = cron
        flow.run_scheduled_by = user.id  # autorità dei run schedulati
        flow.next_run_at = next_fire(cron, datetime.now(timezone.utc))
    if body.production_engine is not None:  # omesso = invariato; "" = come sviluppo
        flow.production_engine = _validate_production_engine(body.production_engine, session)
    if body.notify_emails is not None or body.notify_connection_id is not None:
        _set_failure_notice(session, user, flow, body)
    flow.updated_at = datetime.now(timezone.utc)
    session.add(flow)
    session.commit()
    session.refresh(flow)
    audit.record_audit(
        session, actor=user, action=audit.FLOW_SCHEDULE, target_type="flow",
        target_id=flow.id, target_label=flow.name,
        detail={"cron": flow.run_schedule or "(disattivato)", "production_engine": flow.production_engine,
                "notify": bool(flow.notify_emails and flow.notify_connection_id)}, request=request,
    )
    return flow
