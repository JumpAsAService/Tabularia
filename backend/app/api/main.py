from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.upload_limit import UploadLimit
from prometheus_fastapi_instrumentator import Instrumentator
from app.core.config import get_settings
from app.observability.metrics import register_app_metrics
from app.api.routes.healthcheck import router as healthcheck_router
from app.api.routes.tasks import router as tasks_router
from app.api.routes.files import router as files_router
from app.api.routes.db import router as db_router
from app.api.routes.sharepoint import router as sharepoint_router
from app.api.routes.sftp import router as sftp_router
from app.api.routes.http_api import router as http_api_router
from app.api.routes.placeholders import router as placeholders_router
from app.api.routes.s3_source import router as s3_source_router
from app.api.routes.observability import router as observability_router
from app.api.routes.dbt import router as dbt_router
from app.api.routes.contracts import router as contracts_router
from app.api.routes.regolazioni import router as regolazioni_router
from app.api.routes.workers import router as workers_router
from app.api.solo_dal_gateway import SoloDalGateway


# la chiave Fernet è obbligatoria in ogni ambiente: senza, meglio non partire
get_settings().check_required_secrets()


def allarga_i_thread() -> int:
    """Porta a `APP__ENGINE_API_THREADS` il numero di richieste sincrone servite insieme
    (vedi `AppSettings.engine_api_threads`). Va chiamata dentro il ciclo degli eventi:
    il limite appartiene a lui."""
    import anyio.to_thread

    limite = anyio.to_thread.current_default_thread_limiter()
    limite.total_tokens = get_settings().app.engine_api_threads
    return int(limite.total_tokens)


@asynccontextmanager
async def lifespan(app: FastAPI):
    allarga_i_thread()
    from app.core import tempdirs

    tempdirs.use_process_dir()  # dbt seeds, uploads: a killed API process leaves nothing for good
    yield


app = FastAPI(
    title="Data Prep API",
    description="API per Data Preparation Tool",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().app.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# the ceiling of a file upload, before Starlette spools the file to disk
app.add_middleware(UploadLimit)

# Solo il gateway: tutto tranne /healthcheck e /metrics vuole il suo token
app.add_middleware(SoloDalGateway)

# Metriche Prometheus: request/latency automatiche + collector custom della cache,
# esposte su GET /metrics (scrape-ate da VictoriaMetrics).
Instrumentator().instrument(app).expose(app)
register_app_metrics()

app.include_router(healthcheck_router)
app.include_router(tasks_router)
app.include_router(files_router)
app.include_router(db_router)
app.include_router(sharepoint_router)
app.include_router(sftp_router)
app.include_router(http_api_router)
app.include_router(placeholders_router)
app.include_router(s3_source_router)
app.include_router(observability_router)
app.include_router(dbt_router)
app.include_router(contracts_router)
app.include_router(regolazioni_router)
app.include_router(workers_router)

@app.get("/engines")
def list_engines():
    """Catalogo degli engine per il picker del frontend (id, label, disponibilità)."""
    from app.engine import ENGINE_CATALOG

    return ENGINE_CATALOG


@app.get("/")
def root():
    return {"message": "Data Prep API is running"}