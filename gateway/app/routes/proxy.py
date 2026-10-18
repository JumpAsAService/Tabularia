"""Proxy verso l'engine interno, con RBAC per-oggetto sul data plane.

Le rotte che referenziano oggetti dello storage (preview/transform/export)
leggono il body JSON — piccolo: è l'IR del flusso — autorizzano OGNI chiave
managed trovata (vedi services/objects.py) e solo poi inoltrano. L'upload
registra la risposta dell'engine nel registro `uploads` (proprietario = chi
ha caricato), che è ciò che rende possibili gli altri controlli.

Restano ad autenticazione semplice: /tasks/operations (soli metadati) e
GET /tasks/{task_id} (stato di un task: gli id sono UUID non enumerabili) —
debito documentato, accettabile.
"""
import json
import re
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from sqlmodel import Session

from app.core.config import describe_bytes, get_settings
from app.core.engine_client import get_engine_client
from app.db.session import get_session
from app.deps.auth import get_current_user, oauth2_scheme
from app.models import Upload, User
from app.services import audit
from app.services.engine_policy import disabled_engines
from app.services.objects import collect_storage_keys, ensure_can_read_keys, ensure_can_run_keys, ensure_reads_pinned
from app.services.permissions import can_upload
from app.core.routing import RottaCheRilascia

logger = logging.getLogger(__name__)


# header hop-by-hop da non ritrasmettere
# `authorization` e `cookie` portano la sessione dell'utente: all'engine non servono
# (si fida del gateway col token interno) e non devono arrivarci
_SKIP_REQUEST_HEADERS = {"host", "content-length", "authorization", "cookie", "x-tabularia-csrf", "connection"}
# header di risposta da passare al browser (content-disposition = nome file download)
_PASS_RESPONSE_HEADERS = {"content-type", "content-disposition", "content-length"}


def _request_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() not in _SKIP_REQUEST_HEADERS}


def _release_db(session: Session | None, user: User | None = None) -> None:
    """Rende al pool la connessione al database PRIMA di aspettare l'engine.

    L'autenticazione e i controlli sui permessi leggono dal database, e la
    sessione tiene aperta quella transazione — cioè una connessione del pool —
    finché la richiesta non finisce. Ma un'anteprima aspetta l'engine fino a due
    minuti, un upload o un export per tutta la loro durata: il pool ha quindici
    connessioni, e quindici attese lente insieme lasciavano senza database ogni
    altra richiesta, accesso compreso (misurato: 4,8 secondi di attesa per una
    richiesta qualunque, e le anteprime stesse in coda dietro al pool).

    `close()` e non `commit()`: un commit farebbe scadere gli attributi
    dell'utente, e chi li legge dopo — sul ciclo degli eventi — andrebbe a
    riprendersi una connessione proprio lì dove non si deve aspettare (vedi
    sotto). Con `close()` l'utente resta leggibile senza database; per questo
    lo si carica qui, se un commit precedente (basta `last_seen`) l'aveva già
    fatto scadere. La sessione resta usabile: chi la usa dopo la risposta
    dell'engine ne apre una transazione nuova, breve. Non c'è niente in sospeso
    da perdere: i controlli leggono soltanto e l'audit fa commit per conto suo.
    """
    if session is None:
        return
    if user is not None:
        user.id  # noqa: B018 — ricarica ORA se scaduto, finché la sessione c'è
    session.close()


# ── Una connessione si prende e si rende NELLO STESSO passo ──────────────────
# Queste rotte sono `async` (inoltrano in streaming), le query sono sincrone, e
# FastAPI esegue ogni dipendenza sincrona in un thread suo. Da qui due stalli,
# entrambi osservati dal vivo, entrambi lunghi quanto il timeout del pool (30 s):
#
#  - query eseguite sul ciclo degli eventi: se il pool è esaurito il ciclo resta
#    fermo ad aspettare una connessione che può liberarsi solo quando LUI
#    riprende a far avanzare le altre richieste;
#  - connessione tenuta fra un passo e l'altro: l'autenticazione la prende in
#    un thread e la richiesta la tiene mentre aspetta un thread libero per i
#    controlli; intanto i thread sono tutti occupati da richieste che aspettano
#    una connessione. Con 60 anteprime insieme: gateway fermo 28 secondi.
#
# La regola che li toglie entrambi: ogni tratto che usa la sessione gira in un
# thread (`run_in_threadpool`) e RILASCIA prima di uscirne. Così chi aspetta una
# connessione aspetta solo thread che stanno lavorando e la renderanno da soli.
def _utente(
    request: Request, token: str = Depends(oauth2_scheme), session: Session = Depends(get_session),
) -> User:
    """`get_current_user`, ma senza lasciare la connessione in mano alla
    richiesta: autentica e rilascia nello stesso thread. L'utente che torna è
    staccato dalla sessione e già caricato — si legge senza database."""
    user = get_current_user(request, token, session)
    _release_db(session, user)
    return user


router = APIRouter(route_class=RottaCheRilascia, tags=["engine"], dependencies=[Depends(_utente)])


async def _libera(session: Session | None, user: User | None = None) -> None:
    """Rete di sicurezza prima di un'attesa: se la sessione ha ancora una
    transazione aperta, la chiude (in un thread). Di norma non ce n'è bisogno,
    perché ogni tratto rilascia da sé."""
    if session is not None and session.in_transaction():
        await run_in_threadpool(_release_db, session, user)


async def _forward(
    request: Request, method: str, path: str, content: Any = None,
    session: Session | None = None, user: User | None = None, params: Any = None,
) -> StreamingResponse:
    """Inoltra la richiesta all'engine in streaming in ENTRAMBE le direzioni:
    il body della richiesta (upload GB-scale) e quello della risposta (download
    csv/xlsx). Il gateway non bufferizza mai un file intero in RAM.
    `content` valorizzato = body già letto (per le rotte che lo ispezionano).
    `session` (e `user`): la sessione della richiesta, da rilasciare prima
    dell'attesa (vedi `_release_db`) — ogni rotta la passa. `params`: the query
    string to send instead of the browser's."""
    await _libera(session, user)
    client = get_engine_client()
    if content is None and method in ("POST", "PUT", "PATCH"):
        content = request.stream()
    engine_req = client.build_request(
        method,
        path,
        params=request.query_params if params is None else params,
        headers=_request_headers(request),
        content=content,
    )
    engine_resp = await client.send(engine_req, stream=True)
    return StreamingResponse(
        engine_resp.aiter_raw(),
        status_code=engine_resp.status_code,
        headers={k: v for k, v in engine_resp.headers.items() if k.lower() in _PASS_RESPONSE_HEADERS},
        background=BackgroundTask(engine_resp.aclose),
    )


# il body di queste rotte è l'IR di un flusso: piccolo per natura. Tetto per
# evitare che un body gigante (multi-GB) faccia OOM il gateway con await body().
MAX_JSON_BODY_BYTES = 8 * 1024 * 1024  # 8 MB


async def _read_json(request: Request) -> tuple[bytes, Any]:
    # scarto subito se il Content-Length dichiarato supera il tetto…
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_JSON_BODY_BYTES:
                raise HTTPException(status_code=413, detail="The request body is too large (limit 8 MB)")
        except ValueError:
            pass
    # …e comunque leggo a chunk con un tetto reale (il Content-Length può mentire
    # o mancare, es. Transfer-Encoding: chunked)
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_JSON_BODY_BYTES:
            raise HTTPException(status_code=413, detail="The request body is too large (limit 8 MB)")
        chunks.append(chunk)
    raw = b"".join(chunks)
    try:
        # NaN e Infinity: il parser di Python li accetta, l'engine poi non li sa
        # riscrivere e rispondeva 500. Non sono JSON: si fermano qui
        return raw, (json.loads(raw, parse_constant=_non_json) if raw else {})
    except ValueError:  # JSONDecodeError compreso
        raise HTTPException(status_code=422, detail="The request body is not valid JSON")


def _non_json(costante: str):
    raise ValueError(f"{costante} non è un valore JSON")


# ── Rotte dati inoltrate all'engine ───────────────────────────────────────────
@router.get("/tasks/operations")
async def operations(request: Request, session: Session = Depends(get_session)):
    return await _forward(request, "GET", "/tasks/operations", session=session)


@router.get("/engines")
async def engines(request: Request, session: Session = Depends(get_session)):
    """Catalogo degli engine, con la politica dell'installazione già applicata.

    NON è un inoltro cieco, ed è l'unico punto in cui il catalogo dell'engine
    viene riscritto: l'engine sa cosa è tecnicamente disponibile, il gateway sa
    cosa l'amministratore consente. Un motore disabilitato torna `available:
    false` con `disabled_by_admin: true`, perché il selettore deve poter dire
    PERCHÉ — «non consentito qui» non è «non configurato», e mostrarli uguali
    manderebbe l'utente a cercare una variabile d'ambiente che non c'entra.
    """
    await _libera(session)  # la policy si legge DOPO la risposta dell'engine
    client = get_engine_client()
    resp = await client.get("/engines")
    if resp.status_code >= 400:  # errore dell'engine: si passa così com'è
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type"),
        )
    catalogo = resp.json()

    def policy() -> set[str]:
        try:
            return set(disabled_engines(session))
        finally:
            _release_db(session)

    vietati = await run_in_threadpool(policy)
    if vietati and isinstance(catalogo, list):
        for e in catalogo:
            if isinstance(e, dict) and e.get("id") in vietati:
                e["available"] = False
                e["disabled_by_admin"] = True
    return catalogo


_SLOT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def scope_preview_slot(raw: bytes, payload: Any, user_id: int) -> bytes:
    """Lo slot di una preview ("in questo pannello conta solo l'ultima") lo
    dichiara il browser, ma una nuova preview sullo stesso slot BUTTA GIU' la
    precedente: senza un recinto, chi indovinasse lo slot di un altro gli
    annullerebbe le anteprime. Il gateway lo prefissa con l'utente autenticato,
    quindi uno slot puo' colpire solo le preview di chi lo manda. Uno slot
    malformato viene tolto (la preview gira, semplicemente senza slot); senza
    slot il body passa INTATTO, byte per byte, come prima."""
    if not isinstance(payload, dict) or "slot" not in payload:
        return raw
    slot = payload.get("slot")
    scoped = dict(payload)
    if isinstance(slot, str) and _SLOT_RE.match(slot):
        scoped["slot"] = f"u{user_id}:{slot}"
    else:
        scoped.pop("slot", None)
    return json.dumps(scoped).encode()


@router.post("/tasks/preview")
async def preview(
    request: Request,
    user: User = Depends(_utente),
    session: Session = Depends(get_session),
):
    raw, payload = await _read_json(request)

    def controlli() -> int:
        try:
            ensure_reads_pinned(session, user, payload, get_settings().engine.bucket)
            ensure_can_read_keys(session, user, collect_storage_keys(payload))
            return user.id
        finally:
            _release_db(session, user)

    user_id = await run_in_threadpool(controlli)
    raw = scope_preview_slot(raw, payload, user_id)
    return await _forward(request, "POST", "/tasks/preview", content=raw, session=session, user=user)


@router.post("/tasks/cache-status")
async def cache_status(
    request: Request,
    user: User = Depends(_utente),
    session: Session = Depends(get_session),
):
    """Which nodes of a flow have their result in the step cache (the editor's
    halo). The chains are the ones previews send, so they are checked like a
    preview's: sources pinned to the engine's bucket and readable by the user."""
    raw, payload = await _read_json(request)

    def controlli() -> None:
        try:
            ensure_reads_pinned(session, user, payload, get_settings().engine.bucket)
            ensure_can_read_keys(session, user, collect_storage_keys(payload))
        finally:
            _release_db(session, user)

    await run_in_threadpool(controlli)
    return await _forward(request, "POST", "/tasks/cache-status", content=raw, session=session, user=user)


@router.post("/tasks/transform-data")
async def transform(
    request: Request,
    user: User = Depends(_utente),
    session: Session = Depends(get_session),
):
    raw, payload = await _read_json(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="The request body must be a JSON object")
    # le destinazioni (database o S3) passano SOLO da /flows/{id}/runs: è il
    # gateway a costruire il payload di connessione (RBAC CONNECT, secret mai
    # dal client). "db_destination" è il nome storico: rifiutato anche quello.
    # "mirror" (copia su S3 esterno) ed "email" (invio dell'output come allegato)
    # appartengono alla STESSA classe: portano una connessione e una destinazione.
    # Senza questo filtro un utente qualsiasi poteva inviarle qui con endpoint e
    # secret IN CHIARO propri e farsi consegnare il parquet dal worker, saltando
    # catalogo, CONNECT, cifratura e riga Run — cioè ogni controllo che il
    # percorso /flows/{id}/runs applica. Per "email" salterebbe anche la
    # validazione dei domini ammessi, cioè l'unica barriera all'invio di dati
    # verso un indirizzo arbitrario.
    #
    # La famiglia si allunga: quando se ne aggiunge un membro va aggiunto QUI,
    # nello stesso commit. È già successo di dimenticarsene.
    if any(payload.get(k) for k in ("destination", "db_destination", "mirror", "email")):
        raise HTTPException(
            status_code=422,
            detail="Destinations are not allowed here: save the flow and use an Output node",
        )
    engine_bucket = get_settings().engine.bucket
    # la chiave di OUTPUT la sceglie il SERVER (out/<uuid> write-once), mai il
    # client: niente overwrite del blob di un altro utente né riuso della chiave
    # che avvelenerebbe la step-cache (indicizzata sul path). Il client polla il
    # task_id, non ha bisogno di conoscere/scegliere la chiave.
    payload["output_key"] = f"out/{uuid4().hex}.parquet"
    payload["bucket"] = payload.get("bucket") or engine_bucket

    def controlli() -> None:
        # sorgenti vincolate al bucket dell'engine + prefissi gestiti (no letture arbitrarie)
        ensure_reads_pinned(session, user, payload, engine_bucket)
        # autorizza le sole chiavi di LETTURA (l'output è generato dal server, non va autorizzato in lettura)
        read_payload = {k: v for k, v in payload.items() if k != "output_key"}
        keys = collect_storage_keys(read_payload)
        ensure_can_read_keys(session, user, keys)
        # …e poi il permesso di ESEGUIRE: questo non è un'anteprima, è un run senza
        # flusso salvato. Leggere non basta — vale la stessa soglia di /flows/{id}/runs
        ensure_can_run_keys(session, user, keys)
        audit.record_audit(
            session, actor=user, action=audit.TRANSFORM_RUN,
            target_type="transform", target_label=payload.get("input_key"),
            detail={"engine": payload.get("engine"), "source_keys": sorted(keys),
                    "operations": len(payload.get("operations") or [])},
            request=request,
        )
        _release_db(session, user)

    await run_in_threadpool(controlli)
    return await _forward(
        request, "POST", "/tasks/transform-data", content=json.dumps(payload).encode(), session=session, user=user,
    )


def _authorize_export(request: Request, session: Session, user: User, payload: Any) -> None:
    """Who exports must be able to read every source; the download is audited
    (who takes what: format, file, source, engine)."""
    try:
        ensure_reads_pinned(session, user, payload, get_settings().engine.bucket)
        keys = collect_storage_keys(payload)
        ensure_can_read_keys(session, user, keys)
        audit.record_audit(
            session, actor=user, action=audit.EXPORT_DOWNLOAD,
            target_type="export", target_label=payload.get("filename") or payload.get("input_key"),
            detail={
                "format": payload.get("format"),
                "filename": payload.get("filename"),
                "engine": payload.get("engine"),
                "source_keys": sorted(keys),
            },
            request=request,
        )
    finally:
        _release_db(session, user)


@router.post("/tasks/export")
async def export(
    request: Request,
    user: User = Depends(_utente),
    session: Session = Depends(get_session),
):
    """The export for API callers: the file comes back in the response."""
    raw, payload = await _read_json(request)
    await run_in_threadpool(_authorize_export, request, session, user, payload)
    return await _forward(request, "POST", "/tasks/export", content=raw, session=session, user=user)


# ── The browser's export: started, followed, its file taken once ────────────
# Nothing waits on an open request while a worker writes a large file, and the
# browser saves the file as it arrives instead of holding it all in memory. The
# engine lets only the user who started an export see it: the gateway names them.
_TASK_ID = re.compile(r"^[0-9a-f-]{36}$")


def _export_owner(user: User) -> str:
    return f"user:{user.id}"


@router.post("/tasks/exports")
async def start_export(
    request: Request,
    user: User = Depends(_utente),
    session: Session = Depends(get_session),
):
    raw, payload = await _read_json(request)
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="The request body must be a JSON object")
    await run_in_threadpool(_authorize_export, request, session, user, payload)
    payload["owner"] = _export_owner(user)  # never the browser's word for it
    return await _forward(request, "POST", "/tasks/exports", content=json.dumps(payload).encode(), session=session, user=user)


@router.get("/tasks/exports/{task_id}")
async def export_status(request: Request, task_id: str, user: User = Depends(_utente)):
    if not _TASK_ID.match(task_id):
        raise HTTPException(status_code=404, detail="Export not found")
    return await _forward(request, "GET", f"/tasks/exports/{task_id}", params={"owner": _export_owner(user)})


@router.get("/tasks/exports/{task_id}/file")
async def export_file(request: Request, task_id: str, user: User = Depends(_utente)):
    if not _TASK_ID.match(task_id):
        raise HTTPException(status_code=404, detail="Export not found")
    return await _forward(request, "GET", f"/tasks/exports/{task_id}/file", params={"owner": _export_owner(user)})


@router.get("/tasks/{task_id}")
async def task_status(request: Request, task_id: str, session: Session = Depends(get_session)):
    return await _forward(request, "GET", f"/tasks/{task_id}", session=session)


class _UploadTooLarge(Exception):
    pass


@router.post("/files")
async def upload(
    request: Request,
    user: User = Depends(_utente),
    session: Session = Depends(get_session),
):
    """Upload (streaming verso l'engine) + registrazione della proprietà.

    La risposta dell'engine è un piccolo JSON (IngestResult): si bufferizza per
    registrare chi possiede il dataset appena creato — è la base del controllo
    di lettura sugli upload non ancora dentro un flusso salvato.

    Serve EDIT da qualche parte: l'upload non ha una cartella su cui chiedere il
    permesso, e senza questo controllo un account di sola lettura poteva scrivere
    nel bucket dell'installazione (vedi permissions.can_upload).
    """

    def controlli() -> int:
        if not can_upload(session, user):
            raise HTTPException(
                status_code=403,
                detail="Uploading a file needs edit permission on at least one folder",
            )
        proprietario = user.id
        _release_db(session, user)  # l'upload può durare minuti: senza connessione in mano
        return proprietario

    limite = get_settings().upload.max_bytes
    troppo = HTTPException(
        status_code=413,
        detail=f"The file is too large: the limit is {describe_bytes(limite)} (UPLOAD__MAX_BYTES)",
    )
    # a declared size over the ceiling: refused before anything is read or sent
    dichiarati = request.headers.get("content-length") or ""
    if limite and dichiarati.isdigit() and int(dichiarati) > limite:
        raise troppo
    owner_id = await run_in_threadpool(controlli)

    superato = False

    async def contato():
        """The body as it arrives, cut at the ceiling: a chunked upload declares no
        size, and a declared one could be a lie."""
        nonlocal superato
        ricevuti = 0
        async for pezzo in request.stream():
            ricevuti += len(pezzo)
            if limite and ricevuti > limite:
                superato = True
                raise _UploadTooLarge()
            yield pezzo

    client = get_engine_client()
    engine_req = client.build_request(
        "POST",
        "/files",
        params=request.query_params,
        headers=_request_headers(request),
        content=contato(),
    )
    try:
        engine_resp = await client.send(engine_req)
    except Exception:
        # the engine sees a body that stops short and throws its spooled copy away
        if superato:
            raise troppo from None
        raise

    if engine_resp.status_code < 400:
        def registra() -> None:
            try:
                data = engine_resp.json()
                session.add(
                    Upload(
                        dataset_id=str(data.get("dataset_id") or ""),
                        bucket=get_settings().engine.bucket,
                        parquet_key=str(data.get("parquet_key") or ""),
                        raw_key=str(data.get("raw_key") or ""),
                        owner_id=owner_id,
                    )
                )
                session.commit()
            except Exception as e:  # la registrazione non deve rompere l'upload
                logger.warning("upload non registrato nel registro uploads: %s", e)
                session.rollback()

        await run_in_threadpool(registra)

    return Response(
        content=engine_resp.content,
        status_code=engine_resp.status_code,
        media_type=engine_resp.headers.get("content-type"),
    )
