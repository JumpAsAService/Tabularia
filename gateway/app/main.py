import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings
from app.core.engine_client import close_engine_client
from app.db.migra import prepara_database
from app.db.session import backfill_flow_versions, lucchetto_di_avvio
from app.db.seed import seed_admin
from app.services import first_run
from app.services.orchestrator import coda_loop
from app.services.scheduler import scheduler_loop
from app.services.audit_retention import retention_loop
from app.services.webhooks import consegna_loop as webhooks_loop
from app.services.regolazioni_motore import spingi_loop as regolazioni_loop
from app.services.kube_scaling import riapplica_loop as scaling_loop
from app.services.siem import inoltro_loop as siem_loop
from app.routes.auth import router as auth_router
from app.routes.sso import router as sso_router
from app.routes.users import router as users_router
from app.routes.api_tokens import router as api_tokens_router
from app.routes.service_accounts import router as service_accounts_router
from app.routes.webhooks import router as webhooks_router
from app.routes.engine_settings import router as engine_settings_router
from app.routes.workers import router as workers_router
from app.routes.siem import router as siem_router
from app.routes.groups import router as groups_router
from app.routes.banners import router as banners_router
from app.routes.privacy import router as privacy_router
from app.routes.engine_policy import router as engine_policy_router
from app.routes.saved_views import router as saved_views_router
from app.routes.flow_groups import router as flow_groups_router
from app.routes.search import router as search_router
from app.routes.projects import router as projects_router
from app.routes.permissions import router as permissions_router
from app.routes.flows import router as flows_router
from app.routes.runs import router as runs_router
from app.routes.datasources import router as datasources_router
from app.routes.contracts import router as contracts_router
from app.routes.connections import router as connections_router
from app.routes.lineage import router as lineage_router
from app.routes.scheduling import router as scheduling_router
from app.routes.audit import router as audit_router
from app.routes.system import router as system_router
from app.routes.ai import router as ai_router
from app.routes.queue import router as queue_router
from app.routes.proxy import router as proxy_router
from app.core.body_limit import BodyLimitMiddleware
from app.core import api_docs

logging.basicConfig(level=logging.INFO)


def avvia_lavoro_di_fondo(stop: asyncio.Event) -> list[asyncio.Task]:
    """Il lavoro di fondo, per chi lo fa (`APP__ROLE`): un processo che risponde
    soltanto (`api`) non ne avvia. Ogni voce regge più processi che la fanno
    insieme, senza doppioni."""
    if get_settings().app.role == "api":
        return []
    return [
        # schedulazioni: refresh delle datasource ed esecuzioni dei flussi
        asyncio.create_task(scheduler_loop(stop)),
        # la coda delle esecuzioni dei flussi, e le orchestrazioni rimaste senza
        # nessuno che le porti avanti (processo morto): si chiudono dicendolo
        asyncio.create_task(coda_loop(stop)),
        # scadenza del registro delle azioni: parte solo se è configurata
        asyncio.create_task(retention_loop(stop)),
        # le consegne dei webhook, ogni paio di secondi (services/webhooks.py)
        asyncio.create_task(webhooks_loop(stop)),
        # le impostazioni del motore dal pannello, rispedite al motore ogni minuto:
        # se Valkey si svuota o il motore riparte, tornano da sole
        asyncio.create_task(regolazioni_loop(stop)),
        # on Kubernetes, the workers' autoscaling bounds set from the panel, put back
        # every minute if the cluster has others (it returns at once elsewhere)
        asyncio.create_task(scaling_loop(stop)),
        # il registro di audit verso il SIEM (Splunk), ogni pochi secondi, se acceso
        asyncio.create_task(siem_loop(stop)),
    ]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # la chiave Fernet è obbligatoria SEMPRE; in produzione anche i default di
    # sviluppo (jwt/admin/db) bloccano l'avvio
    get_settings().check_required_secrets()
    get_settings().check_sso_config()  # SSO acceso ⇒ configurazione completa
    get_settings().check_production_safety()
    import anyio.to_thread

    anyio.to_thread.current_default_thread_limiter().total_tokens = get_settings().app.gateway_threads
    # lo schema: migrato (sviluppo, DB__AUTO_MIGRATE) o controllato (produzione: lo
    # migra un passo separato) — vedi app/db/migra.py. Poi l'admin da env
    # (idempotente). Uno alla volta: più processi possono partire insieme
    prepara_database()
    with lucchetto_di_avvio():
        seed_admin()
        first_run.ensure_bootstrap_token()
        backfill_flow_versions()  # v1 baseline ai flussi creati prima del versioning
    stop = asyncio.Event()
    from app.services import openlineage

    logger.info(openlineage.describe())
    fondo = avvia_lavoro_di_fondo(stop)
    try:
        yield
    finally:
        stop.set()
        for task in fondo:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # gli eventi di lineage ancora in coda: al più 5 s, poi via (un rolling
        # update non deve aspettare un collector lento)
        await asyncio.to_thread(openlineage.wait_idle, 5.0)
        await close_engine_client()


settings = get_settings()

from app.routes import performance as performance_routes

logger = logging.getLogger(__name__)

# La pagina /docs è la documentazione per chi integra: in inglese, come docs/api.md
_DESCRIZIONE_API = (
    "The Tabularia API: everything the interface does goes through it.\n\n"
    "**Sign in** with an API token in the header `Authorization: Bearer tab_…` "
    "(Settings → API tokens, or a service account under Administration). Read-only "
    "tokens refuse every request that changes something.\n\n"
    "**Stable** for integrations: `auth/me`, `projects`, `flows`, `flows/{id}/run-now`, "
    "`runs`, `datasources`, `datasources/{id}/refresh`, `tasks/export`, `lineage`, and the "
    "webhooks. The rest may change between minor versions. The guide, with recipes and "
    "webhook signature checks, is docs/api.md in the repository."
)
_GRUPPI = [
    {"name": "auth", "description": "Sign-in, the current account, sessions."},
    {"name": "api-tokens", "description": "API tokens: your own, and every token for administrators."},
    {"name": "service-accounts", "description": "Integrations that sign in only with a token (administrators)."},
    {"name": "webhooks", "description": "Calls Tabularia makes when a flow or a refresh finishes (administrators)."},
    {"name": "settings", "description": "Engine settings changed from the administration panel, without restarts (administrators)."},
    {"name": "siem", "description": "Forwarding of the audit log to a SIEM: Splunk HTTP Event Collector (administrators)."},
]

app = FastAPI(
    title=f"{settings.app.name} — Gateway",
    description=_DESCRIZIONE_API,
    openapi_tags=_GRUPPI,
    version=settings.app.version,
    lifespan=lifespan,
    # dietro un proxy che toglie un prefisso: /docs e lo schema lo riportano,
    # e il reindirizzamento per la barra finale (che lo perderebbe) si spegne
    root_path=settings.app.root_path,
    redirect_slashes=settings.app.redirect_slashes,
    # /docs and /redoc with pinned, hash-checked scripts and a CSP (core/api_docs.py)
    docs_url=None,
    redoc_url=None,
)
api_docs.install(app)

app.add_middleware(BodyLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.app.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    # the name of a downloaded file (the dbt export's zip): a browser on another origin sees it only if exposed
    expose_headers=["Content-Disposition"],
)

@app.exception_handler(RequestValidationError)
async def _richiesta_non_valida(request: Request, exc: RequestValidationError):
    """Il 422 di FastAPI, tranne quando il valore rifiutato non si scrive in JSON:
    `NaN` e `Infinity` li accetta il parser, e rimandarli indietro nell'errore
    faceva un 500 (anche sul login, senza essere entrati). Lì l'errore dice dove e
    perché, senza ripetere il valore."""
    try:
        return await request_validation_exception_handler(request, exc)
    except ValueError:
        errori = [{k: v for k, v in e.items() if k not in ("input", "ctx")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errori)})


@app.get("/health", include_in_schema=False)
def health():
    """Liveness: l'app sta servendo. Usato dall'healthcheck del container per
    rendere VISIBILE un gateway wedged (app giù ma container ancora 'running')."""
    return {"status": "ok"}


# Un processo che fa solo da orchestratore non serve l'API: non è raggiunto dal
# proxy e risponde soltanto al controllo di salute.
if settings.app.role != "orchestrator":
    # control plane
    app.include_router(auth_router)
    app.include_router(sso_router)
    app.include_router(users_router)
    app.include_router(api_tokens_router)
    app.include_router(service_accounts_router)
    app.include_router(webhooks_router)
    app.include_router(engine_settings_router)
    app.include_router(workers_router)
    app.include_router(siem_router)
    app.include_router(groups_router)
    app.include_router(banners_router)
    app.include_router(privacy_router)
    app.include_router(engine_policy_router)
    app.include_router(ai_router)
    app.include_router(saved_views_router)
    app.include_router(flow_groups_router)
    app.include_router(search_router)
    app.include_router(projects_router)
    app.include_router(permissions_router)
    app.include_router(flows_router)
    app.include_router(runs_router)
    app.include_router(datasources_router)
    app.include_router(contracts_router)
    app.include_router(connections_router)
    app.include_router(lineage_router)
    app.include_router(scheduling_router)
    app.include_router(audit_router)
    app.include_router(system_router)
    app.include_router(queue_router)
    # data plane (proxy verso l'engine interno)
    app.include_router(proxy_router)
    app.include_router(performance_routes.router)


@app.get("/", tags=["health"])
def root():
    return {"message": f"{settings.app.name} gateway is running"}
