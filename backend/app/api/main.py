from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from app.core.config import get_settings
from app.observability.metrics import register_app_metrics
from app.api.routes.healthcheck import router as healthcheck_router
from app.api.routes.tasks import router as tasks_router
from app.api.routes.files import router as files_router
from app.api.routes.db import router as db_router


app = FastAPI(
    title="Data Prep API",
    description="API per Data Preparation Tool",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().app.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Metriche Prometheus: request/latency automatiche + collector custom della cache,
# esposte su GET /metrics (scrape-ate da VictoriaMetrics).
Instrumentator().instrument(app).expose(app)
register_app_metrics()

app.include_router(healthcheck_router)
app.include_router(tasks_router)
app.include_router(files_router)
app.include_router(db_router)

@app.get("/engines")
def list_engines():
    """Catalogo degli engine per il picker del frontend (id, label, disponibilità)."""
    from app.engine import ENGINE_CATALOG

    return ENGINE_CATALOG


@app.get("/")
def root():
    return {"message": "Data Prep API is running"}