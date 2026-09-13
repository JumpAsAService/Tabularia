import os

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import BaseModel, Field, SecretStr, computed_field, field_validator
from functools import lru_cache
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


# Come nell'engine: blocchi annidati BaseModel *puri*, il caricamento (env) avviene
# sul `Settings` top-level con delimitatore `__`, così l'env vince sempre.
# Esempi: DB__HOST=postgres, JWT__SECRET=..., ENGINE__BASE_URL=http://backend:8000


# ─────────────────────────────────────────────────────────────────────────────
# Postgres (metadati: utenti, gruppi, progetti, permessi)
# ─────────────────────────────────────────────────────────────────────────────
class DbSettings(BaseModel):
    host: str = "localhost"
    port: int = 5432
    user: str = "tabularia"
    password: SecretStr = SecretStr("tabularia")
    name: str = "tabularia"
    # override completo: se valorizzato, ignora host/port/... sopra
    url: Optional[str] = None

    @computed_field
    @property
    def dsn(self) -> str:
        if self.url:
            return self.url
        pw = self.password.get_secret_value()
        return f"postgresql://{self.user}:{pw}@{self.host}:{self.port}/{self.name}"


# ─────────────────────────────────────────────────────────────────────────────
# JWT (firma dei token di accesso)
# ─────────────────────────────────────────────────────────────────────────────
class JwtSettings(BaseModel):
    # CAMBIALO in produzione (env JWT__SECRET). Default solo per dev.
    secret: SecretStr = SecretStr("change-me-in-production")
    algorithm: str = "HS256"
    access_ttl_minutes: int = 12 * 60  # 12h


# ─────────────────────────────────────────────────────────────────────────────
# Admin seedato allo startup (come Grafana/MinIO: credenziali da env, cambiabili
# velocemente in produzione)
# ─────────────────────────────────────────────────────────────────────────────
class AuthSettings(BaseModel):
    admin_email: str = "admin@tabularia.local"
    admin_password: SecretStr = SecretStr("admin")
    admin_name: str = "Administrator"


def _celery_hard_limit_seconds() -> int:
    """Il limite duro di Celery COME LO VEDE L'ENGINE: stessa variabile, stesso
    `.env`, entrambi i container la ricevono. Il gateway non importa la
    configurazione dell'engine (processi e immagini diverse), quindi l'accordo fra
    i due passa da qui."""
    raw = os.getenv("CELERY__TASK_TIME_LIMIT", "3600")
    try:
        return int(raw)
    except ValueError:
        raise RuntimeError(
            f"CELERY__TASK_TIME_LIMIT non è un numero intero di secondi: '{raw}'"
        ) from None


# ─────────────────────────────────────────────────────────────────────────────
# Engine interno (data plane): il gateway ci fa da proxy, non è esposto pubblicamente
# ─────────────────────────────────────────────────────────────────────────────
class EngineSettings(BaseModel):
    base_url: str = "http://localhost:8000"
    # bucket dello storage dell'engine: il gateway lo usa solo come STRINGA nei
    # payload (non tocca mai lo storage). Deve combaciare con STORAGE__BUCKET.
    bucket: str = "data-prep"
    # env: ENGINE__RUN_STALE_TIMEOUT_SECONDS — oltre questa età un run non terminale
    # è considerato in TIMEOUT (risultato perso o task troppo lungo) e marcato
    # FAILURE.
    #
    # Il default NON è più un numero fisso: è DERIVATO dal limite duro di Celery
    # più cinque minuti di margine. Prima i due valori erano scritti a mano nei
    # due servizi (`3600 + 300` qui, `3600` là), e alzare solo quello dell'engine
    # lasciava il gateway a dichiarare perso un run ancora vivo — oppure, alzando
    # solo questo, si aspettava di più per scoprire che Celery l'aveva già ucciso.
    #
    # L'invariante da preservare: questa soglia deve restare MAGGIORE del limite
    # duro, perché il gateway rinuncia solo DOPO che Celery ha ucciso il task. È
    # anche ciò che rende sicuro non abilitare `task_acks_late` sull'engine.
    # Valorizzare ENGINE__RUN_STALE_TIMEOUT_SECONDS ha comunque la precedenza
    # (l'env batte il default), per chi vuole un margine diverso.
    run_stale_timeout_seconds: int = Field(
        default_factory=lambda: _celery_hard_limit_seconds() + 300
    )


# ─────────────────────────────────────────────────────────────────────────────
# SSO OIDC (OPZIONALE): Keycloak, Microsoft Entra ID (MSAL), Auth0, Okta…
#
# Disattivato finché `issuer` e `client_id` non sono valorizzati: senza, il
# login locale resta l'unico e il frontend non mostra nemmeno il pulsante.
# Il gateway si auto-configura da {issuer}/.well-known/openid-configuration.
# L'IdP NON sostituisce il JWT interno: dopo la validazione del token OIDC si
# emette il solito token Tabularia, così RBAC e audit restano identici.
# ─────────────────────────────────────────────────────────────────────────────
class OidcSettings(BaseModel):
    # env: OIDC__ISSUER — es. https://keycloak.example.com/realms/tabularia
    #   Entra ID: https://login.microsoftonline.com/<tenant-id>/v2.0
    issuer: str = ""
    # env: OIDC__CLIENT_ID / OIDC__CLIENT_SECRET (client confidenziale)
    client_id: str = ""
    client_secret: SecretStr = SecretStr("")
    # env: OIDC__REDIRECT_URI — deve combaciare ESATTAMENTE con quella registrata
    # sull'IdP; punta al gateway: https://gateway.example.com/auth/sso/callback
    redirect_uri: str = ""
    # env: OIDC__SCOPES — separati da spazio
    scopes: str = "openid profile email"
    # env: OIDC__GROUPS_CLAIM — 'groups' (Keycloak) o 'roles' (app role di Entra)
    groups_claim: str = "groups"
    # env: OIDC__GROUP_ALLOWLIST — nomi separati da virgola; vuoto = tutti
    group_allowlist: str = ""
    # env: OIDC__AUTO_CREATE_GROUPS — crea i gruppi Tabularia mancanti al login.
    # False (default) = mappa solo su gruppi già curati dall'admin (più igienico)
    auto_create_groups: bool = False
    # env: OIDC__AUTHORITATIVE — True: l'IdP possiede l'appartenenza (toglie i
    # gruppi non presenti nella claim). False: l'SSO aggiunge soltanto.
    authoritative: bool = True
    # env: OIDC__SUPERUSER_GROUP — appartenervi concede is_superuser (vuoto = mai)
    superuser_group: str = ""
    # ── Chi può ENTRARE (diverso da: quali gruppi riceve) ────────────────────
    # Senza questi due, chiunque l'IdP autentichi ottiene un account attivo: se
    # l'issuer è la directory aziendale, è tutta l'azienda. Entrambi spenti di
    # default, per non cambiare il comportamento di chi già usa l'SSO.
    # env: OIDC__ALLOWED_EMAIL_DOMAINS — domini ammessi, separati da virgola
    allowed_email_domains: str = ""
    # env: OIDC__REQUIRE_ALLOWLISTED_GROUP — entra solo chi appartiene ad almeno
    # un gruppo di `group_allowlist` (o al gruppo che concede l'admin)
    require_allowlisted_group: bool = False
    # env: OIDC__POST_LOGIN_URL — pagina del FRONTEND che riceve il token
    post_login_url: str = "http://localhost:3000/auth/callback"
    # env: OIDC__BUTTON_LABEL — etichetta del pulsante nella pagina di login
    button_label: str = "Single sign-on"
    # env: OIDC__DISCOVERY_TTL_SECONDS — cache del documento di discovery
    discovery_ttl_seconds: int = 3600
    # env: OIDC__TIMEOUT_SECONDS — timeout delle chiamate all'IdP
    timeout_seconds: float = 10.0
    # env: OIDC__LOGIN_TX_TTL_SECONDS — validità della transazione di login
    # (cookie con state/nonce/PKCE): quanto può durare la schermata dell'IdP
    login_tx_ttl_seconds: int = 600

    @computed_field
    @property
    def enabled(self) -> bool:
        """SSO attivo solo se configurato: nessun default nascosto."""
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
# Security: chiave Fernet condivisa con l'engine per le credenziali delle
# connessioni DB (cifrate a riposo e nei payload verso l'engine)
# ─────────────────────────────────────────────────────────────────────────────
class SecuritySettings(BaseModel):
    # env: SECURITY__FERNET_KEY (stessa variabile letta dall'engine). OBBLIGATORIA
    # in ogni ambiente: senza, il servizio non parte (check_required_secrets).
    fernet_key: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Monitoraggio: node-exporter è la fonte della RAM dell'host mostrata in UI.
# Lo si legge DIRETTAMENTE (valore istantaneo) e non da VictoriaMetrics, che
# ha una granularità di scrape di 15s: qui serve un dato realtime.
# ─────────────────────────────────────────────────────────────────────────────
class MonitoringSettings(BaseModel):
    node_exporter_url: str = "http://node-exporter:9100/metrics"
    timeout_seconds: float = 3.0
    # più browser che pollano condividono una sola fetch (il payload è ~85 KB)
    cache_seconds: float = 3.0


# ─────────────────────────────────────────────────────────────────────────────
# App
# ─────────────────────────────────────────────────────────────────────────────
class AppSettings(BaseModel):
    name: str = "Tabularia"
    version: str = "0.1.0"
    env_name: str = "development"
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    # fuso orario del DEPLOYMENT per lo SCHEDULING: le espressioni cron sono
    # interpretate in questo fuso (orario a PARETE locale, DST incluso) e poi
    # convertite in UTC per lo storage. NON tocca la visualizzazione dei timestamp
    # (quella resta nel fuso del browser di chi guarda). Nome IANA, es. Europe/Rome.
    timezone: str = "UTC"

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError(
                f"APP__TIMEZONE non valido: '{v}' — usa un nome IANA (es. 'Europe/Rome', 'UTC')"
            )
        return v

    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class SchedulingSettings(BaseModel):
    # capacità di esecuzione simultanea usata SOLO per evidenziare le fasce critiche
    # nell'heatmap del carico schedule: dovrebbe rispecchiare la concorrenza del
    # worker 'celery' (CELERY__WORKER_CONCURRENCY sull'engine). Job schedulati nello
    # stesso minuto oltre questa soglia finiscono in coda → fascia critica.
    worker_capacity: int = 2


class OrchestratorSettings(BaseModel):
    """Attese massime di un flusso sui nodi che devono COMPLETARSI prima dei passi
    a valle (così l'ordine è reale, non cosmetico). Oltre il tetto il flusso aborta
    invece di leggere dati stantii — alza i valori se hai refresh/output molto
    lunghi (tabelle enormi o rete lenta)."""
    # env: ORCHESTRATOR__REFRESH_WAIT_SECONDS — tetto d'attesa di un nodo Refresh
    refresh_wait_seconds: int = 600
    # env: ORCHESTRATOR__OUTPUT_WAIT_SECONDS — tetto d'attesa di un nodo Output
    output_wait_seconds: int = 600
    # env: ORCHESTRATOR__POLL_INTERVAL_SECONDS — cadenza di polling durante l'attesa
    poll_interval_seconds: float = 3.0


class Settings(BaseSettings):
    """
    Configurazione del gateway. Priorità: init > env > default.
    Campi annidati col delimitatore `__` (es. DB__HOST → db.host).
    """
    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")

    app: AppSettings = Field(default_factory=AppSettings)
    scheduling: SchedulingSettings = Field(default_factory=SchedulingSettings)
    orchestrator: OrchestratorSettings = Field(default_factory=OrchestratorSettings)
    db: DbSettings = Field(default_factory=DbSettings)
    jwt: JwtSettings = Field(default_factory=JwtSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    oidc: OidcSettings = Field(default_factory=OidcSettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)

    def is_production(self) -> bool:
        return self.app.env_name.lower() in ("production", "prod")

    def check_required_secrets(self) -> None:
        """La chiave Fernet è OBBLIGATORIA in ogni ambiente, anche in sviluppo:
        senza, le credenziali delle connessioni non si possono cifrare né
        decifrare e le API fallirebbero al primo uso. Meglio non partire, con
        un messaggio chiaro. Chiamata allo startup (API, worker, beat)."""
        from cryptography.fernet import Fernet

        hint = (
            'genera con: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())" e impostala in infrastructure/.env '
            "(identica in gateway ed engine)"
        )
        key = self.security.fernet_key
        if not key:
            raise RuntimeError(f"SECURITY__FERNET_KEY non impostata: {hint}")
        try:
            Fernet(key)
        except Exception as e:  # base64 malformato, lunghezza sbagliata…
            raise RuntimeError(f"SECURITY__FERNET_KEY non valida ({e}): {hint}") from e

    def check_sso_config(self) -> None:
        """Se l'SSO è acceso deve essere COMPLETO: una configurazione a metà
        (senza segreto o senza redirect URI) fallirebbe solo al primo login, con
        l'utente davanti. Chiamata allo startup. SSO spento = nessun vincolo."""
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
                "SSO OIDC attivo ma incompleto, manca: " + ", ".join(missing)
                + ". Completa la configurazione o togli OIDC__ISSUER per disattivarlo."
            )
        if "openid" not in cfg.scope_list:
            raise RuntimeError("OIDC__SCOPES deve includere 'openid' (è un requisito OIDC)")

    def check_production_safety(self) -> None:
        """Rifiuta di partire in produzione con i default di sviluppo.

        I default nel codice (jwt secret, password admin/db) esistono solo per
        far partire lo stack in dev senza attrito: in produzione DEVONO essere
        sovrascritti via env. Chiamata allo startup (lifespan).
        """
        if not self.is_production():
            return
        problems = []
        if self.jwt.secret.get_secret_value() == "change-me-in-production":
            problems.append("JWT__SECRET è il default di sviluppo (genera: openssl rand -hex 32)")
        if self.auth.admin_password.get_secret_value() == "admin":
            problems.append("AUTH__ADMIN_PASSWORD è il default 'admin'")
        if self.db.password.get_secret_value() == "tabularia" and not self.db.url:
            problems.append("DB__PASSWORD è il default di sviluppo")
        if problems:
            raise RuntimeError(
                "Configurazione NON sicura per la produzione:\n  - " + "\n  - ".join(problems)
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
