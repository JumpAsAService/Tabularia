"""Data contracts di una datasource: leggere, scrivere, verificare, proporre.

Chi può: leggere il contratto e il suo stato chi ha VIEW sulla cartella della
datasource (vede già i dati); scriverlo, verificarlo e farsene proporre uno chi
ha EDIT. Le rotte che aspettano l'engine sono asincrone e non tengono una
connessione al database mentre aspettano (vedi routes/proxy.py).
"""
import json
import logging
import re
from datetime import timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from sqlmodel import Session, select

from app.core.engine_client import get_engine_client
from app.core.routing import RottaCheRilascia
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Connection, DataContract, DataContractResult, Datasource, User
from app.models.permission import Capability
from app.routes.proxy import _release_db, _utente
from app.schemas.models import UtcDateTime
from app.services import audit, contracts, notify_targets, odcs
from app.services.permissions import has_capability

logger = logging.getLogger(__name__)
router = APIRouter(route_class=RottaCheRilascia, tags=["contracts"])

# quanto si aspetta l'engine per una verifica chiesta a mano (lui rinuncia a 120 s)
ENGINE_WAIT_SECONDS = 150


class ContractIn(BaseModel):
    document: dict[str, Any]
    enabled: bool = True
    # Chi avvisare quando lo stato cambia. None = come prima; "" toglie gli
    # indirizzi; 0 toglie la connessione (come per l'avviso dei flussi).
    notify_emails: Optional[str] = None
    notify_connection_id: Optional[int] = None


class ContractOut(BaseModel):
    datasource_id: int
    version: int
    enabled: bool
    document: dict[str, Any]
    # ciò che l'icona dice: pending | passed | warning | failed
    status: str
    checked_at: Optional[UtcDateTime] = None
    errors: int = 0
    warnings: int = 0
    # l'ultimo aggiornamento è stato rifiutato: si stanno servendo i dati di prima
    blocked: bool = False
    blocked_at: Optional[UtcDateTime] = None
    blocked_run_id: Optional[int] = None
    report: Optional[dict[str, Any]] = None
    updated_at: Optional[UtcDateTime] = None
    # la verifica chiesta con questa richiesta non è riuscita (engine giù, dataset
    # troppo grande): il contratto è salvato, lo stato resta quello di prima
    check_error: Optional[str] = None
    # chi legge può anche modificarlo (EDIT sulla cartella): l'interfaccia mostra
    # i comandi solo a chi li può usare. Chi scrive è arrivato qui con EDIT.
    editable: bool = True
    # chi viene avvisato al cambio di stato: lo vede solo chi può modificarlo
    notify_emails: str = ""
    notify_connection_id: Optional[int] = None


class ContractResultOut(BaseModel):
    id: int
    contract_version: int
    trigger: str
    run_id: Optional[int] = None
    outcome: str
    blocked: bool
    errors: int
    warnings: int
    rows: Optional[int] = None
    evaluated_at: UtcDateTime
    report: Optional[dict[str, Any]] = None


def _loads(text: str | None) -> dict | None:
    try:
        return json.loads(text) if text else None
    except json.JSONDecodeError:
        return None


def _out(c: DataContract, check_error: str | None = None) -> ContractOut:
    return ContractOut(
        datasource_id=c.datasource_id, version=c.version, enabled=c.enabled, document=_loads(c.document) or {"rules": []},
        status=contracts.effective_status(c), checked_at=c.checked_at, errors=c.errors, warnings=c.warnings,
        blocked=c.blocked_at is not None, blocked_at=c.blocked_at, blocked_run_id=c.blocked_run_id,
        report=_loads(c.last_report), updated_at=c.updated_at, check_error=check_error,
        notify_emails=c.notify_emails or "", notify_connection_id=c.notify_connection_id,
    )


def _datasource(session: Session, user: User, ds_id: int, capability: Capability) -> Datasource:
    ds = session.get(Datasource, ds_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="Datasource non trovata")
    ensure_can(session, user, ds.project_id, capability)
    return ds


def _contract(session: Session, ds_id: int) -> DataContract:
    c = contracts.get(session, ds_id)
    if c is None:
        raise HTTPException(status_code=404, detail="Questa datasource non ha un data contract")
    return c


def _snapshot(ds: Datasource) -> dict:
    """Ciò che serve all'engine per valutare i dati che la datasource serve adesso."""
    prodotto = ds.refreshed_at or ds.updated_at or ds.created_at
    if prodotto is not None and prodotto.tzinfo is None:
        prodotto = prodotto.replace(tzinfo=timezone.utc)
    return {"bucket": ds.bucket, "key": ds.key, "snapshot_at": prodotto.isoformat() if prodotto else None}


async def _ask_engine(path: str, body: dict) -> tuple[dict | None, str | None]:
    """(risposta, None) oppure (None, perché non c'è)."""
    try:
        resp = await get_engine_client().post(path, json=body, timeout=ENGINE_WAIT_SECONDS)
    except Exception as e:  # engine irraggiungibile
        logger.warning("engine %s non raggiungibile: %s", path, e)
        return None, "Engine non raggiungibile"
    if resp.status_code >= 400:
        try:
            return None, str(resp.json().get("detail") or resp.text)[:300]
        except Exception:
            return None, resp.text[:300]
    return resp.json(), None


# ── lettura ──────────────────────────────────────────────────────────────────
@router.get("/datasources/{ds_id}/contract", response_model=ContractOut)
def get_contract(ds_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    ds = _datasource(session, user, ds_id, Capability.VIEW)
    out = _out(_contract(session, ds_id))
    out.editable = has_capability(session, user, ds.project_id, Capability.EDIT)
    if not out.editable:  # gli indirizzi sono dati personali: non servono a chi legge soltanto
        out.notify_emails, out.notify_connection_id = "", None
    return out


@router.get("/datasources/{ds_id}/contract/history", response_model=list[ContractResultOut])
def contract_history(
    ds_id: int, limit: int = 30, with_report: bool = False,
    user: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    _datasource(session, user, ds_id, Capability.VIEW)
    rows = session.exec(
        select(DataContractResult)
        .where(DataContractResult.datasource_id == ds_id)
        .order_by(DataContractResult.evaluated_at.desc(), DataContractResult.id.desc())
        .limit(max(1, min(limit, 200)))
    ).all()
    return [
        ContractResultOut(
            id=r.id, contract_version=r.contract_version, trigger=r.trigger, run_id=r.run_id, outcome=r.outcome,
            blocked=r.blocked, errors=r.errors, warnings=r.warnings, rows=r.rows, evaluated_at=r.evaluated_at,
            report=_loads(r.report) if with_report else None,
        )
        for r in rows
    ]


@router.get("/datasources/{ds_id}/contract/odcs")
def export_odcs(ds_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> Response:
    """Il contratto nel formato aperto ODCS (YAML): ciò che si consegna a un
    catalogo o a un altro strumento. Solo export: la fonte resta il contratto qui."""
    ds = _datasource(session, user, ds_id, Capability.VIEW)
    c = _contract(session, ds_id)
    try:
        colonne = json.loads(ds.columns or "[]")
        descrizioni = json.loads(ds.column_descriptions or "{}")
    except json.JSONDecodeError:
        colonne, descrizioni = [], {}
    documento = odcs.to_odcs(
        datasource_id=ds.id, name=ds.name, description=ds.description or "",
        columns=colonne if isinstance(colonne, list) else [],
        column_descriptions=descrizioni if isinstance(descrizioni, dict) else {},
        document=_loads(c.document) or {"rules": []}, version=c.version, enabled=c.enabled, created_at=c.created_at,
    )
    nome = re.sub(r"[^A-Za-z0-9._-]+", "_", ds.name).strip("._") or f"datasource_{ds.id}"
    return Response(
        content=odcs.dump_yaml(documento), media_type="application/yaml",
        headers={"Content-Disposition": f'attachment; filename="{nome}.odcs.yaml"'},
    )


# ── scrittura ────────────────────────────────────────────────────────────────
def _notify_targets(session: Session, user: User, ds_id: int, body: ContractIn) -> tuple[str | None, int | None] | None:
    """(indirizzi, connessione SMTP) da salvare, controllati; None = non toccarli.

    Decidere A CHI spedisce un server di posta chiede CONNECT sulla sua
    connessione: sia quando la si sceglie, sia quando si cambiano gli indirizzi
    tenendo quella che c'è. Chi modifica soltanto le regole rimanda tutto com'è e
    non ne ha bisogno; e spegnere gli avvisi è sempre permesso."""
    if body.notify_emails is None and body.notify_connection_id is None:
        return None
    attuale = contracts.get(session, ds_id)
    prima = (attuale.notify_emails, attuale.notify_connection_id) if attuale else (None, None)
    emails, conn_id = prima
    if body.notify_emails is not None:
        emails = ", ".join(notify_targets.parse_recipients(body.notify_emails)) or None
    if body.notify_connection_id is not None:
        conn_id = None if body.notify_connection_id in (0, -1) else body.notify_connection_id
    if emails and conn_id:
        if (emails, conn_id) != prima:
            notify_targets.smtp_connection(session, user, conn_id)
        notify_targets.ensure_deliverable(session.get(Connection, conn_id), emails.split(", "))
    elif conn_id and conn_id != prima[1]:
        notify_targets.smtp_connection(session, user, conn_id)   # scelta senza indirizzi: deve comunque essere SMTP e sua
    return emails, conn_id


@router.put("/datasources/{ds_id}/contract", response_model=ContractOut)
async def put_contract(
    ds_id: int, body: ContractIn, request: Request,
    user: User = Depends(_utente), session: Session = Depends(get_session),
):
    """Crea o sostituisce il contratto, poi lo verifica sui dati correnti. La
    verifica qui non può bloccare niente (non c'è uno snapshot nuovo da
    rifiutare): dice solo se i dati che ci sono già lo rispettano."""
    try:
        document = contracts.validate_document(body.document)
    except contracts.ContractInvalid as e:
        raise HTTPException(status_code=422, detail={"message": "Contratto non valido", "errors": e.errors})

    def salva() -> tuple[dict, bool]:
        ds = _datasource(session, user, ds_id, Capability.EDIT)
        c = contracts.save(session, ds, document, body.enabled, user.id, notify=_notify_targets(session, user, ds_id, body))
        da_verificare = c.enabled and c.status == "pending" and bool(document["rules"])
        snap = _snapshot(ds)
        session.commit()
        audit.record_audit(
            session, actor=user, action=audit.CONTRACT_SAVE, target_type="datasource", target_id=ds_id,
            target_label=ds.name, detail={"version": c.version, "rules": len(document["rules"]), "enabled": c.enabled,
                                          "notify": bool(c.notify_emails and c.notify_connection_id)},
            request=request,
        )
        _release_db(session, user)
        return snap, da_verificare

    snap, da_verificare = await run_in_threadpool(salva)
    report, errore = (None, None)
    if da_verificare:
        report, errore = await _ask_engine("/contracts/evaluate", {**snap, "document": document})

    def chiudi() -> ContractOut:
        c = _contract(session, ds_id)
        # il referto vale solo se il contratto è ancora quello appena salvato
        if report is not None and json.loads(c.document) == document:
            contracts.record_result(session, c, report, trigger="save", snapshot_key=snap["key"])
            session.commit()
            session.refresh(c)
        out = _out(c, errore)
        _release_db(session, user)
        return out

    return await run_in_threadpool(chiudi)


@router.delete("/datasources/{ds_id}/contract", status_code=status.HTTP_204_NO_CONTENT)
def delete_contract(
    ds_id: int, request: Request, user: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    ds = _datasource(session, user, ds_id, Capability.EDIT)
    _contract(session, ds_id)
    contracts.forget(session, ds_id, keep_history=True)
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.CONTRACT_DELETE, target_type="datasource", target_id=ds_id,
        target_label=ds.name, request=request,
    )


@router.post("/datasources/{ds_id}/contract/check", response_model=ContractOut)
async def check_contract(ds_id: int, user: User = Depends(_utente), session: Session = Depends(get_session)):
    """Verifica adesso il contratto sui dati che la datasource sta servendo."""

    def prima() -> tuple[dict, dict]:
        ds = _datasource(session, user, ds_id, Capability.EDIT)
        c = _contract(session, ds_id)
        dati = (_snapshot(ds), _loads(c.document) or {"rules": []})
        _release_db(session, user)
        return dati

    snap, document = await run_in_threadpool(prima)
    report, errore = await _ask_engine("/contracts/evaluate", {**snap, "document": document})

    def dopo() -> ContractOut:
        c = _contract(session, ds_id)
        if report is not None and (_loads(c.document) or {}) == document:
            contracts.record_result(session, c, report, trigger="manual", snapshot_key=snap["key"])
            session.commit()
            session.refresh(c)
        out = _out(c, errore)
        _release_db(session, user)
        return out

    return await run_in_threadpool(dopo)


@router.post("/datasources/{ds_id}/contract/proposal")
async def propose_contract(ds_id: int, user: User = Depends(_utente), session: Session = Depends(get_session)) -> dict:
    """Un contratto proposto dai dati correnti. Non lo salva: è un punto di partenza."""

    def prima() -> dict:
        ds = _datasource(session, user, ds_id, Capability.EDIT)
        snap = _snapshot(ds)
        _release_db(session, user)
        return snap

    snap = await run_in_threadpool(prima)
    risposta, errore = await _ask_engine("/contracts/profile", {"bucket": snap["bucket"], "key": snap["key"]})
    if risposta is None:
        raise HTTPException(status_code=502, detail=errore or "Profilo dei dati non disponibile")
    return {"document": risposta.get("proposal") or {"rules": []}, "profile": risposta.get("profile")}
