"""Proxy verso l'engine interno. Ogni chiamata richiede un utente autenticato.

In questo primo taglio (Fase A+B) l'accesso alle operazioni sui dati richiede
solo l'autenticazione. Quando i contenuti (datasource/flow) vivranno dentro i
progetti (Fase C), qui si aggiungerà il controllo di capability per-progetto.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.core.engine_client import get_engine_client
from app.deps.auth import get_current_user

router = APIRouter(tags=["engine"], dependencies=[Depends(get_current_user)])

# header hop-by-hop da non ritrasmettere
_SKIP_REQUEST_HEADERS = {"host", "content-length", "authorization", "connection"}
# header di risposta da passare al browser (content-disposition = nome file download)
_PASS_RESPONSE_HEADERS = {"content-type", "content-disposition", "content-length"}


async def _forward(request: Request, method: str, path: str) -> StreamingResponse:
    """Inoltra la richiesta all'engine in streaming in ENTRAMBE le direzioni:
    il body della richiesta (upload GB-scale) e quello della risposta (download
    csv/xlsx). Il gateway non bufferizza mai un file intero in RAM."""
    client = get_engine_client()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _SKIP_REQUEST_HEADERS}
    # solo i metodi con body vengono streammati
    content = request.stream() if method in ("POST", "PUT", "PATCH") else None
    engine_req = client.build_request(
        method,
        path,
        params=request.query_params,
        headers=headers,
        content=content,
    )
    engine_resp = await client.send(engine_req, stream=True)
    return StreamingResponse(
        engine_resp.aiter_raw(),
        status_code=engine_resp.status_code,
        headers={k: v for k, v in engine_resp.headers.items() if k.lower() in _PASS_RESPONSE_HEADERS},
        background=BackgroundTask(engine_resp.aclose),
    )


# ── Rotte dati inoltrate all'engine ───────────────────────────────────────────
@router.get("/tasks/operations")
async def operations(request: Request):
    return await _forward(request, "GET", "/tasks/operations")


@router.post("/tasks/preview")
async def preview(request: Request):
    return await _forward(request, "POST", "/tasks/preview")


@router.post("/tasks/transform-data")
async def transform(request: Request):
    return await _forward(request, "POST", "/tasks/transform-data")


@router.post("/tasks/export")
async def export(request: Request):
    return await _forward(request, "POST", "/tasks/export")


@router.get("/tasks/{task_id}")
async def task_status(request: Request, task_id: str):
    return await _forward(request, "GET", f"/tasks/{task_id}")


@router.post("/files")
async def upload(request: Request):
    return await _forward(request, "POST", "/files")
