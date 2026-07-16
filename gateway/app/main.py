import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.engine_client import close_engine_client
from app.db.session import backfill_flow_versions, init_db
from app.db.seed import seed_admin
from app.services.scheduler import scheduler_loop
from app.routes.auth import router as auth_router
from app.routes.users import router as users_router
from app.routes.groups import router as groups_router
from app.routes.projects import router as projects_router
from app.routes.permissions import router as permissions_router
from app.routes.flows import router as flows_router
from app.routes.runs import router as runs_router
from app.routes.datasources import router as datasources_router
from app.routes.connections import router as connections_router
from app.routes.system import router as system_router
from app.routes.proxy import router as proxy_router

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # in produzione i default di sviluppo (jwt/admin/db) bloccano l'avvio
    get_settings().check_production_safety()
    # crea le tabelle e semina l'admin da env (idempotente)
    init_db()
    seed_admin()
    backfill_flow_versions()  # v1 baseline ai flussi creati prima del versioning
    # scheduler in-process del refresh delle datasource database
    stop = asyncio.Event()
    scheduler = asyncio.create_task(scheduler_loop(stop))
    try:
        yield
    finally:
        stop.set()
        scheduler.cancel()
        try:
            await scheduler
        except asyncio.CancelledError:
            pass
        await close_engine_client()


settings = get_settings()

app = FastAPI(
    title=f"{settings.app.name} — Gateway",
    description="Control plane: auth, utenti/gruppi, progetti/permessi, proxy verso l'engine",
    version=settings.app.version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.app.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# control plane
app.include_router(auth_router)
app.include_router(users_router)
app.include_router(groups_router)
app.include_router(projects_router)
app.include_router(permissions_router)
app.include_router(flows_router)
app.include_router(runs_router)
app.include_router(datasources_router)
app.include_router(connections_router)
app.include_router(system_router)
# data plane (proxy verso l'engine interno)
app.include_router(proxy_router)


@app.get("/health", tags=["health"])
def health():
    return {"status": "ok", "service": "gateway"}


@app.get("/", tags=["health"])
def root():
    return {"message": f"{settings.app.name} gateway is running"}
