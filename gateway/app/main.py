import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError

from app.core.config import get_settings
from app.core.engine_client import close_engine_client
from app.db.migra import prepare_database
from app.db.session import backfill_flow_versions, startup_lock
from app.db.seed import seed_admin
from app.services import first_run
from app.services.orchestrator import queue_loop
from app.services.scheduler import scheduler_loop
from app.services.audit_retention import retention_loop
from app.services.housekeeping import housekeeping_loop
from app.services.db_backup import backup_loop
from app.services.webhooks import delivery_loop as webhooks_loop
from app.services.engine_panel_settings import push_loop as engine_settings_loop
from app.services.kube_scaling import reapply_loop as scaling_loop
from app.services.siem import forwarding_loop as siem_loop
from app.services.script_builds import build_loop as script_builds_loop
from app.services.notebook_runtime import reaper_loop as notebooks_loop
from app.services.settings_rollout import rollout_loop as settings_rollout_loop
from app.routes.auth import router as auth_router
from app.routes.sso import router as sso_router
from app.routes.users import router as users_router
from app.routes.api_tokens import router as api_tokens_router
from app.routes.service_accounts import router as service_accounts_router
from app.routes.webhooks import router as webhooks_router
from app.routes.engine_settings import router as engine_settings_router
from app.routes.gateway_settings import rollout_router as settings_rollout_router
from app.routes.gateway_settings import router as gateway_settings_router
from app.routes.workers import router as workers_router
from app.routes.siem import router as siem_router
from app.routes.backups import router as backups_router
from app.routes.scripting import editor_router as scripting_editor_router, router as scripting_router
from app.routes.registries import editor_router as registries_editor_router, router as registries_router
from app.routes.groups import router as groups_router
from app.routes.banners import router as banners_router
from app.routes.privacy import router as privacy_router
from app.routes.engine_policy import router as engine_policy_router
from app.routes.saved_views import router as saved_views_router
from app.routes.scripts import router as scripts_router
from app.routes.sql_scripts import router as sql_scripts_router
from app.routes.reports import router as reports_router
from app.routes.notebooks import router as notebooks_router
from app.routes.notebook_sessions import router as notebook_sessions_router
from app.routes.notebook_proxy import router as notebook_proxy_router
from app.routes.notebook_data import router as notebook_data_router
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
from app.routes.agent_plans import router as agent_plans_router
from app.routes.mcp import router as mcp_router
from app.routes.queue import router as queue_router
from app.routes.proxy import router as proxy_router
from app.core.body_limit import BodyLimitMiddleware
from app.core import api_docs

logging.basicConfig(level=logging.INFO)


def start_background_work(stop: asyncio.Event) -> list[asyncio.Task]:
    """The background work, for whoever does it (`APP__ROLE`): a process that only answers
    (`api`) starts none. Every item holds with several processes doing it together,
    without duplicates."""
    if get_settings().app.role == "api":
        return []
    return [
        # schedules: datasource refreshes and flow runs
        asyncio.create_task(scheduler_loop(stop)),
        # the queue of the flows' runs, and the orchestrations left with nobody carrying them
        # on (dead process): they are closed saying so
        asyncio.create_task(queue_loop(stop)),
        # expiry of the action log: starts only when configured
        asyncio.create_task(retention_loop(stop)),
        # the history that piles up in the database (runs' logs, contract reports,
        # versions, the assistant's old conversations), trimmed, then vacuumed
        asyncio.create_task(housekeeping_loop(stop)),
        # the database's backup into the object storage, on its schedule (Admin → Backups)
        asyncio.create_task(backup_loop(stop)),
        # the webhooks' deliveries, every couple of seconds (services/webhooks.py)
        asyncio.create_task(webhooks_loop(stop)),
        # the engine settings from the panel, sent to the engine again every minute: if Valkey
        # empties or the engine restarts, they come back by themselves
        asyncio.create_task(engine_settings_loop(stop)),
        # on Kubernetes, the workers' autoscaling bounds set from the panel, put back
        # every minute if the cluster has others (it returns at once elsewhere)
        asyncio.create_task(scaling_loop(stop)),
        # the audit log to the SIEM (Splunk), every few seconds, when on
        asyncio.create_task(siem_loop(stop)),
        # on Kubernetes, the images of the scripting environments: built, then checked
        # (it returns at once elsewhere)
        asyncio.create_task(script_builds_loop(stop)),
        # on Kubernetes, the notebooks' pods: the idle ones stopped, the editors' files kept
        # as drafts, the ones that do not start failed saying why (it returns at once elsewhere)
        asyncio.create_task(notebooks_loop(stop)),
        # on Kubernetes, the pods restarted one at a time after a setting read at start
        # changed, when an administrator asks (it returns at once elsewhere)
        asyncio.create_task(settings_rollout_loop(stop)),
    ]


@asynccontextmanager
async def lifespan(app: FastAPI):
    # the Fernet key is required ALWAYS; in production the development defaults
    # (jwt/admin/db) block the start too
    get_settings().check_required_secrets()
    get_settings().check_sso_config()  # SSO on ⇒ complete configuration
    get_settings().check_production_safety()
    import anyio.to_thread

    anyio.to_thread.current_default_thread_limiter().total_tokens = get_settings().app.gateway_threads
    # the schema: migrated (development, DB__AUTO_MIGRATE) or checked (production: a
    # separate step migrates it) — see app/db/migra.py. Then the admin from the env
    # (idempotent). One at a time: several processes may start together
    prepare_database()
    with startup_lock():
        seed_admin()
        first_run.ensure_bootstrap_token()
        backfill_flow_versions()  # v1 baseline for the flows created before versioning
    stop = asyncio.Event()
    # the panel's settings, read again every few seconds by every process, whatever its role.
    # Once now, the database being up: the first reading at import may have found it not
    # ready yet, and the background work below must not start with the environment's
    # values (a shorter retention would delete what the panel keeps)
    from app.core.overrides import overrides_loop, reload as reload_overrides
    from app.db.session import engine as db_engine

    await asyncio.to_thread(reload_overrides, db_engine)

    reread = asyncio.create_task(overrides_loop(stop))
    from app.services import openlineage

    logger.info(openlineage.describe())
    background = start_background_work(stop)
    try:
        yield
    finally:
        stop.set()
        for task in [*background, reread]:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        # the lineage events still queued: at most 5 s, then off (a rolling update must not
        # wait for a slow collector)
        await asyncio.to_thread(openlineage.wait_idle, 5.0)
        await close_engine_client()


settings = get_settings()

from app.routes import performance as performance_routes
from app.routes import cluster as cluster_routes

logger = logging.getLogger(__name__)

# The /docs page is the documentation for whoever integrates: in English, as docs/api.md
_API_DESCRIPTION = (
    "The Tabularia API: everything the interface does goes through it.\n\n"
    "**Sign in** with an API token in the header `Authorization: Bearer tab_…` "
    "(Settings → API tokens, or a service account under Administration). Read-only "
    "tokens refuse every request that changes something.\n\n"
    "**Stable** for integrations: `auth/me`, `projects`, `flows`, `flows/{id}/run-now`, "
    "`runs`, `datasources`, `datasources/{id}/refresh`, `tasks/export`, `lineage`, and the "
    "webhooks. The rest may change between minor versions. The guide, with recipes and "
    "webhook signature checks, is docs/api.md in the repository."
)
_GROUPS = [
    {"name": "auth", "description": "Sign-in, the current account, sessions."},
    {"name": "api-tokens", "description": "API tokens: your own, and every token for administrators."},
    {"name": "service-accounts", "description": "Integrations that sign in only with a token (administrators)."},
    {"name": "webhooks", "description": "Calls Tabularia makes when a flow or a refresh finishes (administrators)."},
    {"name": "settings", "description": "Engine settings changed from the administration panel, without restarts (administrators)."},
    {"name": "siem", "description": "Forwarding of the audit log to a SIEM: Splunk HTTP Event Collector (administrators)."},
]

app = FastAPI(
    title=f"{settings.app.name} — Gateway",
    description=_API_DESCRIPTION,
    openapi_tags=_GROUPS,
    version=settings.app.version,
    lifespan=lifespan,
    # behind a proxy that removes a prefix: /docs and the schema put it back, and the
    # trailing-slash redirect (which would lose it) is turned off
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

@app.exception_handler(DBAPIError)
async def _database_down(request: Request, exc: DBAPIError):
    """PostgreSQL not answering (restarting, its node drained, the network): a 503 that
    says so, instead of a bare «Internal Server Error» that sends people looking at their
    password. Other database errors stay 500s: they are bugs."""
    if not (exc.connection_invalidated or isinstance(exc, (OperationalError, InterfaceError))):
        raise exc
    logger.warning("database not answering on %s %s: %s", request.method, request.url.path, str(exc).splitlines()[0][:200])
    return JSONResponse(status_code=503, content={"detail": "The database is not answering, or too busy to: try again in a moment"})


@app.exception_handler(httpx.TransportError)
async def _service_down(request: Request, exc: httpx.TransportError):
    """The engine (or another service a request calls) not answering — a pod of it just
    stopping, its node drained: a 503 that says who, instead of a bare 500."""
    try:
        target = exc.request.url
    except RuntimeError:  # an error raised without its request
        target = None
    engine_url = httpx.URL(get_settings().engine.base_url)
    is_engine = target is not None and (target.host, target.port) == (engine_url.host, engine_url.port)
    logger.warning("%s not answering on %s %s: %s", target.host if target else "a service", request.method,
                   request.url.path, type(exc).__name__)
    who = "The engine is not answering" if is_engine else "A service Tabularia calls is not answering"
    return JSONResponse(status_code=503, content={"detail": f"{who}: try again in a moment"})


@app.exception_handler(RequestValidationError)
async def _invalid_request(request: Request, exc: RequestValidationError):
    """FastAPI's 422, except when the refused value cannot be written in JSON: the parser
    accepts `NaN` and `Infinity`, and sending them back in the error made a 500 (on the
    login too, without being signed in). There the error says where and why, without
    repeating the value."""
    try:
        return await request_validation_exception_handler(request, exc)
    except ValueError:
        errors = [{k: v for k, v in e.items() if k not in ("input", "ctx")} for e in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


@app.get("/health", include_in_schema=False)
async def health():
    """Liveness: the app is serving. Used by the container's healthcheck to make a wedged
    gateway VISIBLE (app down but container still 'running').
    On the event loop, not in the threads that serve the requests: twelve slow
    requests (a database that answers late) made the probes fail and Kubernetes
    restart a busy gateway, with everything it was doing."""
    return {"status": "ok"}


# A process that only acts as orchestrator does not serve the API: the proxy does not
# reach it and it answers only the health check.
if settings.app.role != "orchestrator":
    # control plane
    app.include_router(auth_router)
    app.include_router(sso_router)
    app.include_router(users_router)
    app.include_router(api_tokens_router)
    app.include_router(service_accounts_router)
    app.include_router(webhooks_router)
    app.include_router(engine_settings_router)
    app.include_router(gateway_settings_router)
    app.include_router(settings_rollout_router)
    app.include_router(workers_router)
    app.include_router(siem_router)
    app.include_router(backups_router)
    app.include_router(scripting_router)
    app.include_router(scripting_editor_router)
    app.include_router(registries_router)
    app.include_router(registries_editor_router)
    app.include_router(groups_router)
    app.include_router(banners_router)
    app.include_router(privacy_router)
    app.include_router(engine_policy_router)
    app.include_router(ai_router)
    # the agent's plans (the page that confirms one) and the MCP server for agents outside
    # Tabularia (docs/specs/mcp-agent.md)
    app.include_router(agent_plans_router)
    app.include_router(mcp_router)
    app.include_router(saved_views_router)
    app.include_router(scripts_router)
    app.include_router(sql_scripts_router)
    app.include_router(reports_router)
    app.include_router(notebooks_router)
    # the notebooks' pods: their sessions and «Save version» first, then the proxy to
    # marimo (its paths would take /notebooks/{id}/edit/save otherwise), and what the
    # pods read, with their own token
    app.include_router(notebook_sessions_router)
    app.include_router(notebook_proxy_router)
    app.include_router(notebook_data_router)
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
    # data plane (proxy towards the internal engine)
    app.include_router(proxy_router)
    app.include_router(performance_routes.router)
    app.include_router(cluster_routes.router)
    # the notebooks' own address (NOTEBOOKS__ORIGIN): its host reaches only the proxy to
    # marimo, never the rest of the API (routes/notebook_proxy.py, `install`)
    from app.routes import notebook_proxy

    notebook_proxy.install(app)


@app.get("/", tags=["health"])
def root():
    return {"message": f"{settings.app.name} gateway is running"}
