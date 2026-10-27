import json
import os
import re
from urllib.parse import quote

from pydantic_settings import BaseSettings, SettingsConfigDict

from pydantic import BaseModel, Field, SecretStr, computed_field, field_validator, model_validator
from functools import lru_cache
from typing import Literal, Optional

from app.core.passwords import (
    KNOWN_PASSWORDS, PASSWORD_MIN_LENGTH, is_known_password, password_problem,
)
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# As in the engine: nested *pure* BaseModel blocks, the loading (env) happens on the
# top-level `Settings` with the `__` delimiter, so the env always wins.
# Examples: DB__HOST=postgres, JWT__SECRET=..., ENGINE__BASE_URL=http://backend:8000


# ─────────────────────────────────────────────────────────────────────────────
# Postgres (metadata: users, groups, projects, permissions)
# ─────────────────────────────────────────────────────────────────────────────
class DbSettings(BaseModel):
    host: str = "localhost"
    port: int = 5432
    user: str = "tabularia"
    password: SecretStr = SecretStr("tabularia")
    name: str = "tabularia"
    # a full override: when set, ignores host/port/... above
    url: Optional[str] = None
    # Connections kept open towards Postgres. Empty = one per thread
    # (APP__GATEWAY_THREADS), which is as many as needed: each thread uses at most one at a
    # time. Lower, the missing ones are opened and closed at every request.
    # env: DB__POOL_SIZE
    pool_size: Optional[int] = Field(default=None, ge=1)
    # How many more at peak times (background work included): these are closed as soon as
    # they are handed back. Pool + overflow must stay under Postgres's `max_connections`
    # (100 out of the box). env: DB__MAX_OVERFLOW
    max_overflow: int = Field(default=10, ge=0)
    # Schema migrations (Alembic): true = the gateway migrates by itself at start
    # (development); false = a separate step applies them (`python -m app.db.migra`: Helm's
    # init container, the compose service) and the gateway only checks that the database
    # is not behind. env: DB__AUTO_MIGRATE
    auto_migrate: bool = False

    @computed_field
    @property
    def dsn(self) -> str:
        if self.url:
            return self.url
        # escaped: a generated password often holds «@», «/», «%» or a space
        user, pw = quote(self.user, safe=""), quote(self.password.get_secret_value(), safe="")
        return f"postgresql://{user}:{pw}@{self.host}:{self.port}/{self.name}"


# ─────────────────────────────────────────────────────────────────────────────
# JWT (signing the access tokens)
# ─────────────────────────────────────────────────────────────────────────────
# the minimum length of the JWT secret in production (`openssl rand -hex 32` gives 64)
JWT_SECRET_MIN = 32


class JwtSettings(BaseModel):
    # CHANGE IT in production (env JWT__SECRET). A default for dev only.
    secret: SecretStr = SecretStr("change-me-in-production")
    algorithm: str = "HS256"
    # the MAXIMUM length of a session: past it, one signs in again even while active
    access_ttl_minutes: int = 12 * 60  # 12h


# ─────────────────────────────────────────────────────────────────────────────
# Admin seeded at startup (as Grafana/MinIO do: credentials from the env, quick to
# change in production)
# ─────────────────────────────────────────────────────────────────────────────
# The bootstrap administrator's password follows the rule of every password
# (core/passwords.py): the same list, the same length, the same function. Only the
# messages name the variable to change.
ADMIN_PASSWORD_MIN = PASSWORD_MIN_LENGTH
KNOWN_ADMIN_PASSWORDS = KNOWN_PASSWORDS
is_known_admin_password = is_known_password


def admin_password_problem(password: str) -> Optional[str]:
    """Why this password cannot be the administrator's, or None if it can."""
    if not password:
        return "AUTH__ADMIN_PASSWORD is not set"
    problem = password_problem(password)
    return f"AUTH__ADMIN_PASSWORD: {problem}" if problem else None


class AuthSettings(BaseModel):
    admin_email: str = "admin@tabularia.local"
    # No default: the first start needs one (see seed.seed_admin, and
    # admin_password_problem for what is refused).
    admin_password: SecretStr = SecretStr("")
    admin_name: str = "Administrator"
    # env: AUTH__ALLOW_KNOWN_ADMIN_PASSWORD — development only: lets a local stack
    # whose scripts sign in with a fixed password (admin/admin) keep it. Ignored
    # with APP__ENV_NAME=production.
    allow_known_admin_password: bool = False
    # env: AUTH__FIRST_RUN_SETUP — while no account exists, the sign-in page asks for the
    # first administrator instead of AUTH__ADMIN_PASSWORD (the try-it Compose stack,
    # which answers on 127.0.0.1 only). Ignored with APP__ENV_NAME=production, and once
    # any account exists; AUTH__ADMIN_PASSWORD, when set, still creates the administrator.
    first_run_setup: bool = False
    # env: AUTH__SESSION_IDLE_MINUTES — after how many minutes without activity a session
    # closes. 0 = never (only the maximum length counts, JWT__ACCESS_TTL_MINUTES). It holds
    # twice: the gateway refuses a session left still for longer, and the open page signs
    # out by itself when nobody has touched it for as long.
    session_idle_minutes: int = Field(default=0, ge=0)
    # env: AUTH__COOKIE_DOMAIN — the domain of the session's cookie. Empty (the default) =
    # the gateway's host, right when interface and gateway are on the same host (the
    # gateway under /api). With the interface on another host of the same domain
    # (app.example.com and api.example.com) the common domain goes here, `example.com`:
    # the interface's server must see the cookie to know whether there is a session. Two
    # different domains do not work: the browser does not send the cookie from one site
    # to the other.
    cookie_domain: Optional[str] = None
    # env: AUTH__PERSONAL_TOKENS — every user may create API tokens for their scripts
    # (they hold with THEIR permissions). Off, only the service accounts the administrator
    # creates have tokens.
    personal_tokens: bool = True
    # env: AUTH__API_TOKEN_MAX_DAYS — the farthest expiry of an API token. Every token has
    # one: a credential without expiry is a credential nobody remembers giving.
    api_token_max_days: int = Field(default=365, ge=1, le=3650)


def _celery_hard_limit_seconds() -> int:
    """Celery's hard limit AS THE ENGINE SEES IT: the same variable, the same `.env`, both
    containers receive it. The gateway does not import the engine's configuration
    (different processes and images), so the agreement between the two goes through
    here."""
    raw = os.getenv("CELERY__TASK_TIME_LIMIT", "3600")
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(
            f"CELERY__TASK_TIME_LIMIT is not a whole number of seconds: '{raw}'"
        ) from None


# ─────────────────────────────────────────────────────────────────────────────
# Internal engine (data plane): the gateway proxies it, it is not exposed publicly
# ─────────────────────────────────────────────────────────────────────────────
class EngineSettings(BaseModel):
    base_url: str = "http://localhost:8000"
    # the engine storage's bucket: the gateway uses it only as a STRING in the payloads
    # (it never touches the storage). It must match STORAGE__BUCKET.
    bucket: str = "data-prep"
    # env: ENGINE__RUN_STALE_TIMEOUT_SECONDS — past this age a non-terminal run is
    # considered in TIMEOUT (result lost or task too long) and marked FAILURE.
    #
    # The default is NO longer a fixed number: it is DERIVED from Celery's hard limit plus
    # five minutes of margin. Before, the two values were written by hand in the two
    # services (`3600 + 300` here, `3600` there), and raising only the engine's left the
    # gateway declaring lost a run still alive — or, raising only this one, it waited
    # longer to find out Celery had killed it already.
    #
    # The invariant to keep: this threshold must stay GREATER than the hard limit, because
    # the gateway gives up only AFTER Celery has killed the task. It is also what makes it
    # safe not to enable `task_acks_late` on the engine. Setting
    # ENGINE__RUN_STALE_TIMEOUT_SECONDS takes precedence anyway (the env beats the
    # default), for whoever wants another margin.
    run_stale_timeout_seconds: int = Field(
        default_factory=lambda: _celery_hard_limit_seconds() + 300
    )
    # env: ENGINE__SETTINGS_PANEL — the administrator tunes some engine parameters from
    # Administration → Settings (row group, timeouts, caps, cache…; see
    # services/engine_panel_settings.py). false = the environment only: the panel shows them
    # without being able to change them, and those saved earlier stop holding.
    settings_panel: bool = True
    # env: ENGINE__DBT_EXPORT_TIMEOUT_SECONDS — seconds the gateway waits for the engine to
    # build ONE dbt export (each call: the first, and the one after every AI translation it
    # checks). The engine compiles it in the request (seeds read from the storage, the
    # ClickHouse syntax check): past this bound the download stops with `engine_timeout`
    # (504, in the reader's language) instead of hanging until the ingress cuts it with a
    # bare 504. The ingress cuts the request at 300 s (Helm values: proxy-read-timeout), and
    # an export with AI spends up to AI__DBT_EXPORT_TIMEOUT_SECONDS (180 s) before its last
    # engine call: keep the two together under the ingress' timeout. The engine is not
    # stopped: it finishes the compile and its answer is dropped.
    dbt_export_timeout_seconds: float = Field(default=100, gt=0, le=3600)


# ─────────────────────────────────────────────────────────────────────────────
# SSO OIDC (OPTIONAL): Keycloak, Microsoft Entra ID (MSAL), Auth0, Okta…
#
# Off until `issuer` and `client_id` are set: without them, the local login stays
# the only one and the frontend does not even show the button.
# The gateway configures itself from {issuer}/.well-known/openid-configuration.
# The IdP does NOT replace the internal JWT: after the OIDC token is validated the
# usual Tabularia token is issued, so RBAC and audit stay the same.
# ─────────────────────────────────────────────────────────────────────────────
class OidcSettings(BaseModel):
    # env: OIDC__ISSUER — es. https://keycloak.example.com/realms/tabularia
    #   Entra ID: https://login.microsoftonline.com/<tenant-id>/v2.0
    issuer: str = ""
    # env: OIDC__CLIENT_ID / OIDC__CLIENT_SECRET (confidential client)
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    # env: OIDC__REDIRECT_URI — must match EXACTLY the one registered on the IdP; it
    # points at the gateway: https://gateway.example.com/auth/sso/callback
    redirect_uri: str = ""
    # env: OIDC__SCOPES — separated by spaces
    scopes: str = "openid profile email"
    # env: OIDC__GROUPS_CLAIM — where the groups are: 'groups' (Keycloak's group mapper),
    # 'roles' (Entra ID app roles), or a path into the token: 'realm_access.roles',
    # 'resource_access.<client>.roles' (Keycloak's realm and client roles); several,
    # comma-separated, are joined. A path missing from the id_token is looked up in the
    # access token, then in userinfo (services/sso.py `more_group_claims`)
    groups_claim: str = "groups"
    # env: OIDC__GROUP_ALLOWLIST — names separated by commas; empty = all
    group_allowlist: str = ""
    # env: OIDC__AUTO_CREATE_GROUPS — creates the missing Tabularia groups at login.
    # False (default) = maps only onto groups the admin curates already (cleaner)
    auto_create_groups: bool = False
    # env: OIDC__AUTHORITATIVE — True: the IdP owns the membership (removes the groups not
    # in the claim). False: the SSO only adds.
    authoritative: bool = True
    # env: OIDC__SUPERUSER_GROUP — belonging to it grants is_superuser (empty = never)
    superuser_group: str = ""
    # ── Who may SIGN IN (other than: which groups they get) ──────────────────
    # Without these two, anyone the IdP authenticates gets an active account: when the
    # issuer is the company directory, that is the whole company. Both off by default, so
    # as not to change the behaviour for whoever uses SSO already.
    # env: OIDC__ALLOWED_EMAIL_DOMAINS — allowed domains, separated by commas
    allowed_email_domains: str = ""
    # env: OIDC__REQUIRE_ALLOWLISTED_GROUP — only whoever belongs to at least one group of
    # `group_allowlist` (or to the group that grants admin) signs in
    require_allowlisted_group: bool = False
    # env: OIDC__POST_LOGIN_URL — the FRONTEND page that receives the token
    post_login_url: str = "http://localhost:3000/auth/callback"
    # env: OIDC__BUTTON_LABEL — the button's label on the login page
    button_label: str = "Single sign-on"
    # env: OIDC__DISCOVERY_TTL_SECONDS — cache of the discovery document
    discovery_ttl_seconds: int = 3600
    # env: OIDC__TIMEOUT_SECONDS — the timeout of the calls to the IdP
    timeout_seconds: float = 10.0
    # env: OIDC__CA_FILE — a PEM file of the certificate authorities to trust for the
    # identity provider, besides the public ones: a company Keycloak whose certificate
    # its own authority signed. Empty = the public ones only (the chart's oidc.caCert)
    ca_file: str = ""
    # env: OIDC__VERIFY_TLS — false accepts the identity provider's certificate even when no
    # trusted authority signed it. Whoever can stand between the gateway and the provider
    # can then sign in as anyone: prefer OIDC__CA_FILE. Logged at every start
    verify_tls: bool = True
    # env: OIDC__LOGIN_TX_TTL_SECONDS — how long the login transaction holds (cookie with
    # state/nonce/PKCE): how long the IdP's screen may last
    login_tx_ttl_seconds: int = 600

    @computed_field
    @property
    def enabled(self) -> bool:
        """SSO on only when configured: no hidden default."""
        return bool(self.issuer and self.client_id)

    @property
    def allowlist(self) -> set[str]:
        return {g.strip() for g in self.group_allowlist.split(",") if g.strip()}

    @property
    def allowed_domains(self) -> set[str]:
        return {
            d.strip().lower().lstrip("@")
            for d in self.allowed_email_domains.split(",")
            if d.strip()
        }

    @property
    def scope_list(self) -> list[str]:
        return [s for s in self.scopes.split() if s]

    @property
    def discovery_url(self) -> str:
        return self.issuer.rstrip("/") + "/.well-known/openid-configuration"


# ─────────────────────────────────────────────────────────────────────────────
# Security: the Fernet key shared with the engine for the DB connections'
# credentials (encrypted at rest and in the payloads towards the engine)
# ─────────────────────────────────────────────────────────────────────────────
class SecuritySettings(BaseModel):
    # env: SECURITY__FERNET_KEY (the same variable the engine reads). REQUIRED in every
    # environment: without it, the service does not start (check_required_secrets).
    fernet_key: str = ""
    # env: SECURITY__KEYS_DIR — a directory the services share (one machine, Compose)
    # where the secrets left empty are created at the first start and read afterwards:
    # the Fernet key (fernet.key) and, with AUTH__FIRST_RUN_SETUP, the sample-data
    # container's start-up token (bootstrap.token). Empty = no such directory: the key
    # must be set. Back it up with the database: without fernet.key the connection
    # passwords stored in Tabularia cannot be read.
    keys_dir: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Monitoring: node-exporter is the source of the host's RAM shown in the UI.
# It is read DIRECTLY (instant value) and not from VictoriaMetrics, which has a
# 15 s scrape granularity: here real-time data is needed.
# ─────────────────────────────────────────────────────────────────────────────
class MonitoringSettings(BaseModel):
    node_exporter_url: str = "http://node-exporter:9100/metrics"
    timeout_seconds: float = 3.0
    # several polling browsers share one fetch (the payload is ~85 KB)
    cache_seconds: float = 3.0


# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────
class AppSettings(BaseModel):
    name: str = "Tabularia"
    version: str = "1.2.2"  # the release: exposed by /system/info and in the app's menu
    # the release's name. Appius Claudius Caecus, censor in 312 BC: he rewrote the lists
    # of the citizens and built the first aqueduct — whoever counts, classifies and
    # cleans the registers, and the first pipeline. A data prep before its time.
    codename: str = "Appio"
    env_name: str = "development"
    # What THIS process does. The gateway has two jobs: answering the requests, and
    # carrying on the background work (schedules, running the flows, cleanups). `all` does
    # both, as it always has: one process. To have more replicas they are split: `api`
    # only answers, as many copies as wanted; `orchestrator` does only the background
    # work, in several copies too (they share the work through the database, without
    # duplicates).
    # env: APP__ROLE
    role: Literal["all", "api", "orchestrator"] = "all"
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    # The prefix under which a reverse proxy publishes the gateway, REMOVING it before
    # forwarding (e.g. `/api`, when one host serves interface and API). The routes do not
    # change: it serves what the proxy cannot fix, that is the paths the gateway writes for
    # the browser — the SSO transaction's cookie, the addresses of /docs and the redirects
    # (see redirect_slashes). Empty = published at the root, as in development.
    # env: APP__ROOT_PATH
    root_path: str = ""

    @field_validator("cors_origins")
    @classmethod
    def _no_wildcard_origin(cls, v: list[str]) -> list[str]:
        # The browser's session is a cookie, and CORS is with credentials: with "*" Starlette
        # mirrors ANY origin, and any site visited could use the visitor's session (anti-CSRF
        # header included).
        for origin in v:
            if origin.strip() in ("*", "null"):
                raise ValueError(
                    f"APP__CORS_ORIGINS cannot contain '{origin.strip()}': list the addresses "
                    "of the interface (e.g. [\"https://app.example.com\"]), or leave it empty with a single host"
                )
        return v

    @field_validator("root_path")
    @classmethod
    def _valid_root_path(cls, v: str) -> str:
        v = (v or "").strip().strip("/")
        if not v:
            return ""
        v = "/" + v
        if not re.fullmatch(r"(/[A-Za-z0-9._~-]+)+", v):
            raise ValueError(
                f"Invalid APP__ROOT_PATH: '{v}' — it is a path (e.g. '/api'), not a URL"
            )
        return v

    # How many threads serve the routes and the synchronous dependencies together. Python
    # runs one thread at a time: past a certain number the threads do not work more, they
    # fight over the interpreter. The name differs from the engine's
    # (APP__ENGINE_API_THREADS) because the two services read the same environment file
    # and want opposite values. env: APP__GATEWAY_THREADS
    gateway_threads: int = Field(default=40, ge=4)

    @property
    def redirect_slashes(self) -> bool:
        """FastAPI's automatic «trailing slash» redirect builds the address WITHOUT the
        prefix: under a proxy that removes it, it would lead out of the API (`/api/users/` →
        `/users`, a frontend page). So it is on only at the root; under a prefix, one slash
        too many is a 404."""
        return not self.root_path
    # the DEPLOYMENT's time zone for SCHEDULING: the cron expressions are read in this zone
    # (local WALL time, DST included) and then converted to UTC for storage. It does NOT
    # touch how timestamps are shown (that stays in the viewer's browser zone). An IANA
    # name, e.g. Europe/Rome.
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"Invalid APP__TIMEZONE: '{v}' — use an IANA name (e.g. 'Europe/Rome', 'UTC')"
            )
        return v

    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class PreviewSettings(BaseModel):
    """The AUTOMATIC development sample. With `default_sample_rows` > 0 every source node
    without a sample of its own is read ONLY for the first N rows in the editor's
    previews and in the trial runs (engine_mode "development"): the user turns it off on
    the node choosing «all rows», or replaces it with a sample of their own. Production
    is never touched. It helps when every read costs (remote object storage, pay-per-use
    warehouse). 0 = no automatic sample. env: PREVIEW__DEFAULT_SAMPLE_ROWS"""

    default_sample_rows: int = Field(default=0, ge=0)


class AiSettings(BaseModel):
    """AI assistant (chat over the data). It talks to an OpenAI-compatible endpoint
    (Scaleway Generative APIs, or any other): the URL and the key are enough. Off while
    `base_url` or `secret_key` are empty. Which MODELS may be used the administrator
    decides from the panel (the ai_models table): no model is enabled until they enable
    it.

    env: AI__BASE_URL, AI__ACCESS_KEY, AI__SECRET_KEY, AI__DEFAULT_MODEL,
    AI__MAX_RESULT_ROWS, AI__MAX_REQUESTS, AI__MAX_HISTORY_MESSAGES"""

    base_url: str = ""
    # the key's id (Scaleway calls it access key): the OpenAI protocol authenticates with
    # the secret alone, this stays to recognize the key in use
    access_key: str = ""
    secret_key: SecretStr = SecretStr("")
    # offered first in the selector, when the administrator enabled it
    default_model: str = ""
    # the maximum rows an assistant query returns (to the model and to the chat)
    max_result_rows: int = Field(default=200, ge=1, le=2000)
    # the model's rounds per message: the cap on the loop of tool calls
    max_requests: int = Field(default=12, ge=1, le=50)
    # the same for the flow editor's assistant, whose turns take a model round-trip per
    # step it builds and checks (env: AI__MAX_FLOW_REQUESTS)
    max_flow_requests: int = Field(default=30, ge=1, le=80)
    # the history messages the client may send back at every turn
    max_history_messages: int = Field(default=60, ge=0, le=400)
    # Caps per TURN, applied by pydantic-ai itself (`UsageLimits`). Without them, any user
    # can make the key's owner spend as much as they like: the cap on the rounds does not
    # cap the tokens (audit 2026-09-19, A8).
    # env: AI__MAX_COST_PER_TURN_USD, AI__MAX_INPUT_TOKENS_PER_TURN
    max_cost_per_turn_usd: float = Field(default=0.50, ge=0)
    # A DAILY cap per user, in dollars. The per-turn cap alone is not enough: a thousand
    # turns under the threshold still cost a thousand times. It matters above all on an
    # installation open to strangers (a public demo), where every question goes through
    # the host's key.
    # 0 = no cap (the earlier behaviour).
    # env: AI__MAX_COST_PER_DAY_USD
    max_cost_per_day_usd: float = Field(default=0, ge=0)
    # A DAILY cap for the WHOLE installation, in dollars. The per-user one is not enough
    # where the account is shared: on a public demo everyone signs in as the same guest,
    # and the bill is one. With the cap reached the assistant says it is unavailable until
    # the next day.
    # 0 = no cap.
    # env: AI__MAX_COST_PER_DAY_TOTAL_USD
    max_cost_per_day_total_usd: float = Field(default=0, ge=0)
    max_input_tokens_per_turn: int = Field(default=400_000, ge=1000)
    # Seconds for every AI call of ONE dbt export (descriptions, translations and their
    # sample reads together). The export is one request and the ingress cuts it at 300 s
    # (Helm values: proxy-read-timeout): the AI gives up first, with the reason, and leaves
    # the engine time to build the zip. env: AI__DBT_EXPORT_TIMEOUT_SECONDS
    dbt_export_timeout_seconds: float = Field(default=180, gt=0, le=3600)
    # how many chats to keep per user (the oldest are not deleted by themselves: it is
    # only the listing's cap)
    max_chats_listed: int = Field(default=100, ge=1, le=500)

    @field_validator("secret_key", mode="before")
    @classmethod
    def _ensure_secret(cls, v: object) -> SecretStr:
        return v if isinstance(v, SecretStr) else SecretStr("" if v is None else str(v))

    @property
    def enabled(self) -> bool:
        return bool(self.base_url.strip()) and bool(self.secret_key.get_secret_value().strip())


class WebhookSettings(BaseModel):
    """Calls to external systems at the end of a run (see services/webhooks.py)."""

    # env: WEBHOOKS__RETENTION_DAYS — how many days the deliveries' log is kept
    # (successful or not); then the scheduler cleans it
    retention_days: int = Field(default=30, ge=1)
    # env: WEBHOOKS__TIMEOUT_SECONDS — how long the receiver's answer is waited for
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)


class AuditSettings(BaseModel):
    """How long the action log is kept.

    The default is ZERO, that is FOREVER: it is the behaviour existing installations
    have today, and deleting a security log cannot be something that happens to someone
    without their asking. Whoever opens an installation to strangers — a public demo —
    turns it on and keeps less data to look after.

    It is measured in MINUTES on purpose: on a demo the useful window is an hour, not a
    year, and a coarse unit would have forced writing fractions.
    """

    # env: AUDIT__RETENTION_MINUTES — 0 = no deletion
    retention_minutes: int = Field(default=0, ge=0)
    # env: AUDIT__IP_RETENTION_DAYS — days a client address and browser string are
    # kept (audit rows, API tokens, sessions); past them they are emptied and the
    # row stays. 0 = kept for ever. Separate from the whole-row retention above:
    # an address is personal data, the record of who did what is evidence.
    ip_retention_days: int = Field(default=90, ge=0)


class UploadSettings(BaseModel):
    """File uploads (the editor's «Upload file»). The same variable is read by the
    engine, as a second barrier behind the gateway.

    env: UPLOAD__MAX_BYTES — the largest file accepted, in bytes; 0 = no ceiling.
    The gateway answers 413 before the engine is called when the size is declared,
    and cuts an undeclared (chunked) body at the ceiling. The file is spooled on the
    engine's disk and converted there: the ceiling is also a disk and time bound."""

    max_bytes: int = Field(default=1024 ** 3, ge=0)


def describe_bytes(n: int) -> str:
    """1073741824 -> '1 GB', 3145728 -> '3 MB': for messages people read."""
    for unit, size in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size and n % size == 0:
            return f"{n // size} {unit}"
    return f"{n} bytes"


class SchedulingSettings(BaseModel):
    # the concurrent run capacity used ONLY to highlight the critical slots in the
    # schedule load heatmap: it should mirror the concurrency of the 'celery' worker
    # (CELERY__WORKER_CONCURRENCY on the engine). Jobs scheduled in the same minute past
    # this threshold end up queued → a critical slot.
    worker_capacity: int = 2


class BackupSettings(BaseModel):
    """The automatic backup of this database (services/db_backup.py): the orchestrators run
    `pg_dump` on a schedule and the engine stores the file in the object storage
    (BACKUP__BUCKET / BACKUP__PREFIX, read by the engine: by default Tabularia's bucket,
    under backups/postgres/). The three values below are the installation's defaults;
    Administration → Backups changes them without a restart."""
    # env: BACKUP__ENABLED
    enabled: bool = True
    # env: BACKUP__SCHEDULE — five-field cron, in APP__TIMEZONE (03:00 every night)
    schedule: str = "0 3 * * *"
    # env: BACKUP__KEEP — how many backups stay; the oldest beyond go
    keep: int = Field(default=14, ge=1, le=1000)
    # env: BACKUP__PG_DUMP — the pg_dump used when the image has none of the server's own
    # version (the gateway image has 14 to 18; this one, 18's, reads older servers too)
    pg_dump: str = "pg_dump"
    # env: BACKUP__TIMEOUT_SECONDS — the longest a dump and its upload may take
    timeout_seconds: int = Field(default=3600, ge=60)

    @field_validator("schedule")
    @classmethod
    def _schedule(cls, v: str) -> str:
        from croniter import croniter

        v = (v or "").strip()
        if not croniter.is_valid(v):
            raise ValueError(f"Invalid BACKUP__SCHEDULE: '{v}' (format: 'minute hour day month day-of-week', e.g. '0 3 * * *')")
        return v


class OrchestratorSettings(BaseModel):
    """A flow's maximum waits on the nodes that must COMPLETE before the steps downstream
    (so the order is real, not cosmetic). Past the cap the flow aborts instead of
    reading stale data — raise the values when you have very long refreshes/outputs
    (huge tables or a slow network)."""
    # env: ORCHESTRATOR__REFRESH_WAIT_SECONDS — the wait cap of a Refresh node
    refresh_wait_seconds: int = 600
    # env: ORCHESTRATOR__LOG_RETENTION_DAYS — after how many days a run's logs (a Script or
    # Container node's output, the engine's traceback) are emptied; 0 keeps them for ever
    # (services/run_log_retention.py)
    log_retention_days: int = Field(default=30, ge=0)
    # env: ORCHESTRATOR__CONTRACT_RESULTS_KEPT — the latest data contract reports kept per
    # datasource; 0 keeps them all (services/housekeeping.py)
    contract_results_kept: int = Field(default=100, ge=0)
    # env: ORCHESTRATOR__VERSIONS_KEPT — the latest versions kept per flow, per saved or
    # SQL script and per notebook, the one in use always; 0 keeps them all
    versions_kept: int = Field(default=50, ge=0)
    # env: ORCHESTRATOR__AI_CHAT_RETENTION_DAYS — the assistant's conversations untouched for
    # this long are deleted (not the spending ledger); 0 keeps them
    ai_chat_retention_days: int = Field(default=90, ge=0)
    # An Output node has no wait cap of its own: the flow waits until it ends, and
    # its life is bounded by ENGINE__RUN_STALE_TIMEOUT_SECONDS (see orchestrator._wait_run).
    # env: ORCHESTRATOR__POLL_INTERVAL_SECONDS — polling cadence while waiting
    poll_interval_seconds: float = 3.0
    # ── The orchestrations' queue ────────────────────────────────────────────
    # A flow run is born as a «queued» row and a process acting as orchestrator
    # (APP__ROLE) takes it: there may be more than one.
    # env: ORCHESTRATOR__QUEUE_POLL_SECONDS — how often the queue is looked at
    queue_poll_seconds: float = Field(default=1.0, gt=0)
    # env: ORCHESTRATOR__MAX_CONCURRENT — orchestrations ONE process carries on together;
    # the others stay queued. Each uses a Postgres connection while it asks the engine: it
    # must be kept under the pool's size (DB__POOL_SIZE), or a slow engine exhausts them.
    # The cells of flow groups count in the same bound: past it, they wait.
    max_concurrent: int = Field(default=20, ge=1)
    # env: ORCHESTRATOR__QUEUE_TIMEOUT_SECONDS — a run left queued longer than this (no
    # orchestrator on, or all of them full) is no longer run: it is closed as failed.
    # Without it, a launch made with the orchestrators off would start hours later, when
    # nobody expects it any more.
    queue_timeout_seconds: float = Field(default=900.0, gt=0)
    # env: ORCHESTRATOR__HEARTBEAT_SECONDS — how often whoever runs says «here»
    heartbeat_seconds: float = Field(default=10.0, gt=0)
    # env: ORCHESTRATOR__DEAD_AFTER_SECONDS — the silence past which an orchestration is
    # taken for lost (the process died) and closed. The machines' clocks must agree within
    # this margin.
    dead_after_seconds: float = Field(default=60.0, gt=0)


class OpenLineageSettings(BaseModel):
    """Lineage to an external catalog in the OpenLineage standard (Marquez, DataHub,
    Astro…). Off when there is neither `url` nor `file`. The library's standard
    configuration holds too (OPENLINEAGE_URL / OPENLINEAGE_CONFIG)."""
    url: str = ""              # the collector's HTTP endpoint, e.g. http://marquez:5000
    endpoint: str = "api/v1/lineage"
    api_key: str = ""          # Bearer token, when the collector asks for one
    file: str = ""             # as an alternative or as well: JSON Lines on disk
    namespace: str = "tabularia"   # the namespace of this installation's jobs
    timeout_seconds: float = Field(default=10.0, gt=0)


class KubernetesSettings(BaseModel):
    """Worker autoscaling bounds set from Admin → Workers, on Kubernetes only
    (services/kube_scaling.py). Off unless `scaling` is true; the Helm chart sets all
    of it with `adminScaling.enabled`, together with a service account that may read
    the two worker Deployments and change their autoscalers, and nothing else."""
    scaling: bool = False
    # env: KUBERNETES__CLUSTER_VIEW — Administration → Cluster reads the pods, the events and
    # their metrics in Tabularia's namespaces (services/cluster_view.py; the chart's clusterView)
    cluster_view: bool = False
    api_url: str = "https://kubernetes.default.svc"
    namespace: str = ""  # empty = the pod's own, from its service account
    token_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    ca_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    namespace_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"
    # per kind of worker: its Deployment, its autoscaler ("hpa/<name>" or
    # "scaledobject/<name>", empty = none) and the highest maxReplicas the panel may set
    run_worker_deployment: str = ""
    run_worker_autoscaler: str = ""
    run_worker_max_replicas: int = Field(default=10, ge=1)
    preview_worker_deployment: str = ""
    preview_worker_autoscaler: str = ""
    preview_worker_max_replicas: int = Field(default=8, ge=1)
    timeout_seconds: float = Field(default=10.0, gt=0)
    # env: KUBERNETES__ROLLOUT, KUBERNETES__RELEASE — Administration → Settings restarts
    # the pods of this Helm release that read a changed setting only at start, one at a
    # time (services/settings_rollout.py); set by the chart (settingsRollout.enabled)
    rollout: bool = False
    release: str = ""
    # env: KUBERNETES__APP_NAME — the app.kubernetes.io/name of the release's pods, so that
    # the other charts of an umbrella release, with components of the same names, are left alone
    app_name: str = ""


# The language versions an administrator can pick for a scripting environment, and
# the image each one starts from. The chart may replace them (SCRIPTS__BASE_IMAGES),
# for instance with images pinned by digest or taken from a mirror.
DEFAULT_SCRIPT_BASE_IMAGES = {
    "python": {"3.11": "python:3.11-slim", "3.12": "python:3.12-slim", "3.13": "python:3.13-slim",
               "3.14": "python:3.14-slim"},
    "r": {"4.5": "rocker/r-ver:4.5.3", "4.6": "rocker/r-ver:4.6.0"},
}


class ScriptsSettings(BaseModel):
    """Scripting environments and the Script node (Admin → Scripting environments),
    on Kubernetes only (services/script_builds.py). Off unless `enabled` is true; the
    Helm chart sets all of it with `scripts.enabled`, together with the service
    account that may run Jobs in the two namespaces below. The Kubernetes API is
    reached as in KubernetesSettings (api_url, token_file, ca_file)."""
    enabled: bool = False
    namespace: str = ""  # where the scripts and the image checks run
    # where the editor's «Run» of a Script node runs (empty = `namespace`): apart, with a
    # ResourceQuota of its own, so trials never take resources from the flows' runs
    trial_namespace: str = ""
    build_namespace: str = ""  # where the image builds run (empty = `namespace`)
    # where the environment images are pushed: <registry>/tabularia-env-<id>:v<n>
    registry: str = ""
    registry_insecure: bool = False  # plain HTTP or a self-signed certificate (a test cluster)
    # names of Kubernetes Secrets (type dockerconfigjson): the build pushes with the
    # first, in build_namespace; the Jobs pull with the second, in namespace
    registry_push_secret: str = ""
    registry_pull_secret: str = ""
    # a ConfigMap of build_namespace holding ca.crt: the authority (PEM) that signed the
    # registry's certificate, when it is not a public one (a company CA)
    registry_ca_configmap: str = ""
    builder_image: str = "moby/buildkit:v0.33.1-rootless"
    # JSON: {"python": {"3.12": "python:3.12-slim", …}, "r": {…}}; empty = the defaults
    base_images: str = ""
    pypi_index_url: str = ""  # empty = PyPI
    cran_url: str = "https://cran.r-project.org"  # CRAN itself, or a mirror of it
    build_timeout_seconds: int = Field(default=3600, ge=60)
    # what an image build may use (Kubernetes quantities). The memory is requested
    # whole, so a build lands only on a node that has it; the compilers it runs at once
    # follow from both (one per 1.5 GiB, at most one per core: script_builds.build_jobs)
    build_cpu: str = "2"
    build_memory: str = "4Gi"
    check_timeout_seconds: int = Field(default=300, ge=30)
    poll_seconds: float = Field(default=5.0, gt=0)
    log_max_bytes: int = Field(default=262_144, ge=4096)
    job_ttl_seconds: int = Field(default=3600, ge=60)
    # the code editor underlines what Ruff finds in Python code while it is typed
    lint: bool = True

    @field_validator("build_cpu")
    @classmethod
    def _build_cpu(cls, v: str) -> str:
        if not re.fullmatch(r"\d+(\.\d+)?m?", v.strip()) or float(v.strip().rstrip("m")) <= 0:
            raise ValueError("SCRIPTS__BUILD_CPU is a Kubernetes CPU quantity: 2, 1.5 or 1500m")
        return v.strip()

    @field_validator("build_memory")
    @classmethod
    def _build_memory(cls, v: str) -> str:
        if not re.fullmatch(r"\d+(\.\d+)?(Ki|Mi|Gi|Ti|K|M|G|T)?", v.strip()):
            raise ValueError("SCRIPTS__BUILD_MEMORY is a Kubernetes memory quantity: 4Gi, 6144Mi")
        return v.strip()

    def images(self) -> dict[str, dict[str, str]]:
        if not self.base_images.strip():
            return DEFAULT_SCRIPT_BASE_IMAGES
        try:
            parsed = json.loads(self.base_images)
        except ValueError as e:
            raise ValueError(f"SCRIPTS__BASE_IMAGES is not valid JSON ({e})") from None
        return {lang: {str(v): str(img) for v, img in (parsed.get(lang) or {}).items()}
                for lang in ("python", "r")}



# How to make a Fernet key with the tools of any machine: the earlier hint imported
# the `cryptography` package, which a fresh machine does not have. Same commands as
# infrastructure/.env.example and the getting-started guide.
FERNET_KEY_HINT = (
    "generate one with: openssl rand -base64 32 | tr '+/' '-_'  "
    "(or: python3 -c \"import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())\")"
)

class ContainersSettings(BaseModel):
    """Container registries (Administration → Container registries) and the Container
    node that runs their images, on Kubernetes only: off unless `enabled` is true. The
    Helm chart sets it with `containers.enabled`, and the two namespaces below."""
    enabled: bool = False
    namespace: str = ""  # where the flows' runs run the Container nodes
    trial_namespace: str = ""  # where the editor's «Run» and «Try in the cluster» run (empty = namespace)
    # «Try in the cluster»: how long a node may take to download an image. The request
    # answers within 270 s (the ingress cuts at 300 s); a longer download goes on in the
    # cluster for up to this (services/container_cluster.py)
    pull_check_seconds: int = Field(default=300, ge=30)
    poll_seconds: float = Field(default=3.0, gt=0)
    job_ttl_seconds: int = Field(default=3600, ge=60)
    # the most a Container node may ask for: its CPU and memory, its timeout
    max_cpu: str = "4"
    max_memory: str = "16Gi"
    max_timeout_seconds: int = Field(default=86400, ge=60)
    # the end of a container's log the run keeps
    log_max_bytes: int = Field(default=262_144, ge=4096)

    @field_validator("max_cpu")
    @classmethod
    def _cpu(cls, v: str) -> str:
        if not re.fullmatch(r"\d+(\.\d+)?m?", v.strip()):
            raise ValueError("a Kubernetes CPU quantity: 4, 1.5, 500m")
        return v.strip()

    @field_validator("max_memory")
    @classmethod
    def _memory(cls, v: str) -> str:
        if not re.fullmatch(r"\d+(\.\d+)?(Ki|Mi|Gi|Ti|K|M|G|T)?", v.strip()):
            raise ValueError("a Kubernetes memory quantity: 16Gi, 512Mi")
        return v.strip()


class NotebooksSettings(BaseModel):
    """The pods of the marimo notebooks (docs/specs/notebooks.md), on Kubernetes with
    the scripting environments: they run in the scripts namespace, from the images of
    the environments (services/notebook_runtime.py). The Helm chart sets GATEWAY_URL."""
    # a pod nobody has used for this long is stopped (an editor's file becomes the draft)
    idle_minutes: int = Field(default=15, ge=1, le=24 * 60)
    # the longest a pod lives, used or not (the Job's activeDeadlineSeconds)
    max_lifetime_hours: int = Field(default=12, ge=1, le=7 * 24)
    # how many notebook pods may run at once, apps and editors together
    max_live: int = Field(default=20, ge=1, le=1000)
    # a pod not answering after this long has failed to start, saying why
    start_timeout_seconds: int = Field(default=300, ge=30, le=3600)
    # the gateway's address inside the cluster, which the pods call to read datasources
    # (http://<release>-gateway.<namespace>.svc.cluster.local:8000); empty = they cannot
    gateway_url: str = ""
    # a notebook token is honoured only from its own pod's address. Off only where the
    # gateway cannot see that address: a service mesh (Istio, Linkerd) shows its sidecar's
    pod_ip_check: bool = True


class Settings(BaseSettings):
    """
    The gateway's configuration. Priority: init > env > default.
    Nested fields with the `__` delimiter (e.g. DB__HOST → db.host).
    """
    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")

    app: AppSettings = Field(default_factory=AppSettings)
    preview: PreviewSettings = Field(default_factory=PreviewSettings)
    ai: AiSettings = Field(default_factory=AiSettings)
    scheduling: SchedulingSettings = Field(default_factory=SchedulingSettings)
    orchestrator: OrchestratorSettings = Field(default_factory=OrchestratorSettings)
    backup: BackupSettings = Field(default_factory=BackupSettings)
    db: DbSettings = Field(default_factory=DbSettings)
    jwt: JwtSettings = Field(default_factory=JwtSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    oidc: OidcSettings = Field(default_factory=OidcSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)
    audit: AuditSettings = Field(default_factory=AuditSettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    webhooks: WebhookSettings = Field(default_factory=WebhookSettings)
    openlineage: OpenLineageSettings = Field(default_factory=OpenLineageSettings)
    kubernetes: KubernetesSettings = Field(default_factory=KubernetesSettings)
    scripts: ScriptsSettings = Field(default_factory=ScriptsSettings)
    containers: ContainersSettings = Field(default_factory=ContainersSettings)
    notebooks: NotebooksSettings = Field(default_factory=NotebooksSettings)

    def is_production(self) -> bool:
        return self.app.env_name.lower() in ("production", "prod")

    @model_validator(mode="after")
    def _key_from_keys_dir(self) -> "Settings":
        """No SECURITY__FERNET_KEY but a SECURITY__KEYS_DIR: the key is the one in that
        directory, created at the first start by whichever service gets there first."""
        if not self.security.fernet_key and self.security.keys_dir:
            from cryptography.fernet import Fernet

            from app.core.keys import read_or_create

            self.security.fernet_key = read_or_create(
                self.security.keys_dir, "fernet.key", lambda: Fernet.generate_key().decode())
        return self

    def check_required_secrets(self) -> None:
        """The Fernet key is REQUIRED in every environment, development too: without it, the
        connections' credentials can be neither encrypted nor decrypted and the API would
        fail at first use. Better not to start, with a clear message. Called at startup
        (API, worker, beat)."""
        from cryptography.fernet import Fernet

        hint = (f"{FERNET_KEY_HINT}, and set it in infrastructure/.env (the same in gateway and engine), "
                "or give the services a shared SECURITY__KEYS_DIR where it is created")
        key = self.security.fernet_key
        if not key:
            raise RuntimeError(f"SECURITY__FERNET_KEY is not set: {hint}")
        try:
            Fernet(key)
        except Exception as e:  # malformed base64, wrong length…
            raise RuntimeError(f"SECURITY__FERNET_KEY is not valid ({e}): {hint}") from e

    def check_sso_config(self) -> None:
        """When SSO is on it must be COMPLETE: a half configuration (without a secret or a
        redirect URI) would fail only at the first login, with the user in front of it.
        Called at startup. SSO off = no constraint."""
        cfg = self.oidc
        if not cfg.enabled:
            return
        missing = [
            name for name, value in (
                ("OIDC__CLIENT_SECRET", cfg.client_secret.get_secret_value()),
                ("OIDC__REDIRECT_URI", cfg.redirect_uri),
                ("OIDC__POST_LOGIN_URL", cfg.post_login_url),
            ) if not value
        ]
        if missing:
            raise RuntimeError(
                "OIDC SSO is on but incomplete, missing: " + ", ".join(missing)
                + ". Complete the configuration or remove OIDC__ISSUER to turn it off."
            )
        if "openid" not in cfg.scope_list:
            raise RuntimeError("OIDC__SCOPES must include 'openid' (OIDC requires it)")
        if not cfg.verify_tls:
            import logging

            logging.getLogger(__name__).warning(
                "SSO: the identity provider's certificate is NOT checked (OIDC__VERIFY_TLS=false): whoever stands "
                "between the gateway and %s can sign in as anyone. Prefer OIDC__CA_FILE", cfg.issuer)
        if cfg.ca_file:
            import ssl

            try:
                ssl.create_default_context().load_verify_locations(cafile=cfg.ca_file)
            except (OSError, ssl.SSLError) as e:
                raise RuntimeError(f"OIDC__CA_FILE {cfg.ca_file} is not a readable PEM file of certificates: {e}") from e

    def check_production_safety(self) -> None:
        """Refuses to start in production with the development defaults.

        The defaults in the code (jwt secret, admin/db password) exist only to start the
        stack in dev without friction: in production they MUST be overridden through the
        env. Called at startup (lifespan).
        """
        if not self.is_production():
            return
        problems = []
        secret = self.jwt.secret.get_secret_value()
        if secret == "change-me-in-production":
            problems.append("JWT__SECRET is the development default (generate one: openssl rand -hex 32)")
        elif len(secret) < JWT_SECRET_MIN:
            # signs every session (HS256): a short one is guessed offline from a single token, and
            # whoever guesses it signs themselves an admin token
            problems.append(
                f"JWT__SECRET is too short ({len(secret)} characters, at least "
                f"{JWT_SECRET_MIN} are needed; generate one: openssl rand -hex 32)"
            )
        # empty or published; the length is checked when the administrator is created
        # (seed.seed_admin): an installation running for months with a shorter one
        # must not stop at the upgrade over a value that is no longer used
        admin_pw = self.auth.admin_password.get_secret_value()
        if not admin_pw or is_known_admin_password(admin_pw):
            problems.append(admin_password_problem(admin_pw))
        if self.db.password.get_secret_value() == "tabularia" and not self.db.url:
            problems.append("DB__PASSWORD is the development default")
        if problems:
            raise RuntimeError(
                "Configuration NOT safe for production:\n  - " + "\n  - ".join(problems)
            )


@lru_cache
def settings_from_env() -> Settings:
    """The settings of the environment alone (`.env`, the Helm values), read once."""
    return Settings()


def on_kubernetes() -> bool:
    """In a pod of a Kubernetes cluster. The kubelet sets KUBERNETES_SERVICE_HOST in every
    container; the service account's token is mounted only when a feature that calls the
    Kubernetes API is on (with clusterView and settingsRollout off, it is not), so the
    token alone would take such an installation for Docker Compose."""
    from pathlib import Path

    return bool(os.environ.get("KUBERNETES_SERVICE_HOST")) or Path(get_settings().kubernetes.token_file).exists()


def get_settings() -> Settings:
    """The settings in force: the environment's with, on top, the ones an administrator
    changed from Administration → Settings (app/core/overrides.py). While none are, it is
    the very object of the environment."""
    from app.core import overrides

    return overrides.overlay(settings_from_env())


def _cache_clear() -> None:
    from app.core import overrides

    settings_from_env.cache_clear()
    overrides.forget()


# tests (and code that changes the environment) start over with `get_settings.cache_clear()`
get_settings.cache_clear = _cache_clear  # type: ignore[attr-defined]
