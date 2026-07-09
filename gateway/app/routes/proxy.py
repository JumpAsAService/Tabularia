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
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from sqlmodel import Session

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.models import Upload, User
from app.services.objects import collect_storage_keys, ensure_can_read_keys

logger = logging.getLogger(__name__)

router = APIRouter(tags=["engine"], dependencies=[Depends(get_current_user)])

# header hop-by-hop da non ritrasmettere
_SKIP_REQUEST_HEADERS = {"host", "content-length", "authorization", "connection"}
# header di risposta da passare al browser (content-disposition = nome file download)
_PASS_RESPONSE_HEADERS = {"content-type", "content-disposition", "content-length"}


def _request_headers(request: Request) -> dict[str, str]:
    return {k: v for k, v in request.headers.items() if k.lower() not in _SKIP_REQUEST_HEADERS}


async def _forward(request: Request, method: str, path: str, content: Any = None) -> StreamingResponse:
    """Inoltra la richiesta all'engine in streaming in ENTRAMBE le direzioni:
    il body della richiesta (upload GB-scale) e quello della risposta (download
    csv/xlsx). Il gateway non bufferizza mai un file intero in RAM.
    `content` valorizzato = body già letto (per le rotte che lo ispezionano)."""
    client = get_engine_client()
    if content is None and method in ("POST", "PUT", "PATCH"):
        content = request.stream()
    engine_req = client.build_request(
        method,
        path,
        params=request.query_params,
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


async def _read_json(request: Request) -> tuple[bytes, Any]:
    raw = await request.body()
    try:
        return raw, (json.loads(raw) if raw else {})
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="Il body non è JSON valido")


# ── Rotte dati inoltrate all'engine ───────────────────────────────────────────
@router.get("/tasks/operations")
async def operations(request: Request):
    return await _forward(request, "GET", "/tasks/operations")


@router.post("/tasks/preview")
async def preview(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    raw, payload = await _read_json(request)
    ensure_can_read_keys(session, user, collect_storage_keys(payload))
    return await _forward(request, "POST", "/tasks/preview", content=raw)


@router.post("/tasks/transform-data")
async def transform(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    raw, payload = await _read_json(request)
    ensure_can_read_keys(session, user, collect_storage_keys(payload))
    # i client scrivono SOLO nell'area di lavoro out/ (l'area datasets/ è del
    # catalogo: ci scrive il gateway stesso per publish e ingest)
    output_key = payload.get("output_key") if isinstance(payload, dict) else None
    if not user.is_superuser and not str(output_key or "").startswith("out/"):
        raise HTTPException(status_code=403, detail="La scrittura è consentita solo nell'area out/")
    return await _forward(request, "POST", "/tasks/transform-data", content=raw)


@router.post("/tasks/export")
async def export(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    raw, payload = await _read_json(request)
    ensure_can_read_keys(session, user, collect_storage_keys(payload))
    return await _forward(request, "POST", "/tasks/export", content=raw)


@router.get("/tasks/{task_id}")
async def task_status(request: Request, task_id: str):
    return await _forward(request, "GET", f"/tasks/{task_id}")


@router.post("/files")
async def upload(
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Upload (streaming verso l'engine) + registrazione della proprietà.

    La risposta dell'engine è un piccolo JSON (IngestResult): si bufferizza per
    registrare chi possiede il dataset appena creato — è la base del controllo
    di lettura sugli upload non ancora dentro un flusso salvato.
    """
    client = get_engine_client()
    engine_req = client.build_request(
        "POST",
        "/files",
        params=request.query_params,
        headers=_request_headers(request),
        content=request.stream(),
    )
    engine_resp = await client.send(engine_req)

    if engine_resp.status_code < 400:
        try:
            data = engine_resp.json()
            session.add(
                Upload(
                    dataset_id=str(data.get("dataset_id") or ""),
                    bucket=get_settings().engine.bucket,
                    parquet_key=str(data.get("parquet_key") or ""),
                    raw_key=str(data.get("raw_key") or ""),
                    owner_id=user.id,
                )
            )
            session.commit()
        except Exception as e:  # la registrazione non deve rompere l'upload
            logger.warning("upload non registrato nel registro uploads: %s", e)

    return Response(
        content=engine_resp.content,
        status_code=engine_resp.status_code,
        media_type=engine_resp.headers.get("content-type"),
    )
