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
    # Connessioni tenute aperte verso Postgres. Vuoto = una per thread
    # (APP__GATEWAY_THREADS), che è quante ne servono: ogni thread ne usa al
    # massimo una alla volta. Più basso, quelle che mancano vengono aperte e
    # chiuse a ogni richiesta. env: DB__POOL_SIZE
    pool_size: Optional[int] = Field(default=None, ge=1)
    # Quante in più nei momenti di punta (lavori di fondo compresi): queste sì
    # si chiudono appena rese. Pool + margine deve stare sotto il
    # `max_connections` di Postgres (100 di serie). env: DB__MAX_OVERFLOW
    max_overflow: int = Field(default=10, ge=0)
    # Migrazioni dello schema (Alembic): true = il gateway migra da sé all'avvio
    # (sviluppo); false = le applica un passo separato (`python -m app.db.migra`:
    # init container di Helm, servizio del compose) e il gateway controlla
    # soltanto che il database non sia indietro. env: DB__AUTO_MIGRATE
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
# JWT (firma dei token di accesso)
# ─────────────────────────────────────────────────────────────────────────────
# lunghezza minima del segreto JWT in produzione (`openssl rand -hex 32` ne dà 64)
JWT_SECRET_MIN = 32


class JwtSettings(BaseModel):
    # CAMBIALO in produzione (env JWT__SECRET). Default solo per dev.
    secret: SecretStr = SecretStr("change-me-in-production")
    algorithm: str = "HS256"
    # la durata MASSIMA di una sessione: oltre, si rientra anche se si è attivi
    access_ttl_minutes: int = 12 * 60  # 12h


# ─────────────────────────────────────────────────────────────────────────────
# Admin seedato allo startup (come Grafana/MinIO: credenziali da env, cambiabili
# velocemente in produzione)
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
    # env: AUTH__SESSION_IDLE_MINUTES — dopo quanti minuti senza attività una
    # sessione si chiude. 0 = mai (conta solo la durata massima, JWT__ACCESS_TTL_MINUTES).
    # Vale due volte: il gateway rifiuta una sessione rimasta ferma più a lungo, e
    # la pagina aperta esce da sé quando nessuno la tocca da altrettanto.
    session_idle_minutes: int = Field(default=0, ge=0)
    # env: AUTH__COOKIE_DOMAIN — il dominio del cookie della sessione. Vuoto (il
    # default) = l'host del gateway, giusto quando interfaccia e gateway stanno sullo
    # stesso host (il gateway sotto /api). Con l'interfaccia su un altro host dello
    # stesso dominio (app.example.com e api.example.com) va il dominio comune,
    # `example.com`: il server dell'interfaccia deve vedere il cookie per sapere se
    # c'è una sessione. Due domini diversi non funzionano: il browser non manda il
    # cookie da un sito all'altro.
    cookie_domain: Optional[str] = None
    # env: AUTH__PERSONAL_TOKENS — ogni utente può crearsi token API per i suoi
    # script (valgono con i SUOI permessi). Spento, i token li hanno solo gli
    # account di servizio, creati dall'amministratore.
    personal_tokens: bool = True
    # env: AUTH__API_TOKEN_MAX_DAYS — la scadenza più lontana di un token API.
    # Ogni token ne ha una: una credenziale senza scadenza è una credenziale che
    # nessuno ricorda di aver dato.
    api_token_max_days: int = Field(default=365, ge=1, le=3650)


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
            f"CELERY__TASK_TIME_LIMIT is not a whole number of seconds: '{raw}'"
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
    # env: ENGINE__SETTINGS_PANEL — l'amministratore regola alcuni parametri del
    # motore da Amministrazione → Impostazioni (row group, timeout, tetti, cache…;
    # vedi services/regolazioni_motore.py). false = solo l'ambiente: il pannello li
    # mostra senza poterli cambiare, e quelli salvati prima smettono di valere.
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
    # env: OIDC__GROUPS_CLAIM — where the groups are: 'groups' (Keycloak's group mapper),
    # 'roles' (Entra ID app roles), or a path into the token: 'realm_access.roles',
    # 'resource_access.<client>.roles' (Keycloak's realm and client roles); several,
    # comma-separated, are joined. A path missing from the id_token is looked up in the
    # access token, then in userinfo (services/sso.py `more_group_claims`)
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
    # env: OIDC__CA_FILE — a PEM file of the certificate authorities to trust for the
    # identity provider, besides the public ones: a company Keycloak whose certificate
    # its own authority signed. Empty = the public ones only (the chart's oidc.caCert)
    ca_file: str = ""
    # env: OIDC__VERIFY_TLS — false accepts the identity provider's certificate even when no
    # trusted authority signed it. Whoever can stand between the gateway and the provider
    # can then sign in as anyone: prefer OIDC__CA_FILE. Logged at every start
    verify_tls: bool = True
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
    # env: SECURITY__KEYS_DIR — a directory the services share (one machine, Compose)
    # where the secrets left empty are created at the first start and read afterwards:
    # the Fernet key (fernet.key) and, with AUTH__FIRST_RUN_SETUP, the sample-data
    # container's start-up token (bootstrap.token). Empty = no such directory: the key
    # must be set. Back it up with the database: without fernet.key the connection
    # passwords stored in Tabularia cannot be read.
    keys_dir: str = ""


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
    version: str = "1.2.2"  # la release: esposta da /system/info e nel menu dell'app
    # il nome della release. Appio Claudio Cieco, censore del 312 a.C.: riscrisse
    # le liste dei cittadini e costrui' il primo acquedotto — chi conta, classifica
    # e ripulisce i registri, e la prima pipeline. Un data-prep ante litteram.
    codename: str = "Appio"
    env_name: str = "development"
    # Cosa fa QUESTO processo. Il gateway ha due mestieri: rispondere alle
    # richieste, e portare avanti il lavoro di fondo (schedulazioni, esecuzione
    # dei flussi, pulizie). `all` li fa entrambi, come è sempre stato: un processo
    # solo. Per avere più repliche si separano: `api` risponde e basta, quante
    # copie si vuole; `orchestrator` fa solo il lavoro di fondo, anche lui in più
    # copie (il lavoro se lo dividono dal database, senza doppioni).
    # env: APP__ROLE
    role: Literal["all", "api", "orchestrator"] = "all"
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )
    # Prefisso sotto cui un reverse proxy pubblica il gateway, TOGLIENDOLO prima
    # di inoltrare (es. `/api`, quando un solo host serve interfaccia e API).
    # Le rotte non cambiano: serve a ciò che il proxy non può
    # correggere, cioè i percorsi che il gateway scrive per il browser — il
    # cookie della transazione SSO, gli indirizzi di /docs e i reindirizzamenti
    # (vedi redirect_slashes). Vuoto = pubblicato alla radice, come in sviluppo.
    # env: APP__ROOT_PATH
    root_path: str = ""

    @field_validator("cors_origins")
    @classmethod
    def _no_wildcard_origin(cls, v: list[str]) -> list[str]:
        # La sessione del browser è un cookie, e CORS è con le credenziali: con "*"
        # Starlette rispecchia QUALUNQUE origine, e qualunque sito visitato potrebbe
        # usare la sessione di chi lo visita (intestazione anti-CSRF compresa).
        for origine in v:
            if origine.strip() in ("*", "null"):
                raise ValueError(
                    f"APP__CORS_ORIGINS cannot contain '{origine.strip()}': list the addresses "
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

    # Quanti thread servono insieme le rotte e le dipendenze sincrone. Python
    # esegue un thread alla volta: oltre un certo numero i thread non lavorano di
    # più, si contendono l'interprete. Il nome è diverso da quello dell'engine
    # (APP__ENGINE_API_THREADS) perché i due servizi leggono lo stesso file
    # d'ambiente e vogliono valori opposti. env: APP__GATEWAY_THREADS
    gateway_threads: int = Field(default=40, ge=4)

    @property
    def redirect_slashes(self) -> bool:
        """Il reindirizzamento automatico «barra finale» di FastAPI costruisce
        l'indirizzo SENZA il prefisso: sotto un proxy che lo toglie porterebbe
        fuori dall'API (`/api/users/` → `/users`, una pagina del frontend).
        Quindi è acceso solo alla radice; sotto un prefisso, una barra di troppo
        è un 404."""
        return not self.root_path
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
                f"Invalid APP__TIMEZONE: '{v}' — use an IANA name (e.g. 'Europe/Rome', 'UTC')"
            )
        return v

    def tzinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


class PreviewSettings(BaseModel):
    """Campione AUTOMATICO di sviluppo. Con `default_sample_rows` > 0 ogni nodo
    sorgente che non ha un campione impostato viene letto SOLO per le prime N
    righe nelle preview dell'editor e nei run di prova (engine_mode
    "development"): l'utente lo spegne sul nodo scegliendo «tutte le righe»,
    o lo sostituisce con un campione suo. La produzione non e' mai toccata.
    Serve quando ogni lettura costa (object storage remoto, warehouse a
    consumo). 0 = nessun campione automatico. env: PREVIEW__DEFAULT_SAMPLE_ROWS"""

    default_sample_rows: int = Field(default=0, ge=0)


class AiSettings(BaseModel):
    """Assistente AI (chat sui dati). Parla con un endpoint compatibile OpenAI
    (Scaleway Generative APIs, o qualunque altro): bastano l'URL e la chiave.
    Spento finche' `base_url` o `secret_key` sono vuoti. Quali MODELLI si
    possono usare lo decide l'amministratore dal pannello (tabella ai_models):
    nessun modello e' abilitato finche' non lo abilita lui.

    env: AI__BASE_URL, AI__ACCESS_KEY, AI__SECRET_KEY, AI__DEFAULT_MODEL,
    AI__MAX_RESULT_ROWS, AI__MAX_REQUESTS, AI__MAX_HISTORY_MESSAGES"""

    base_url: str = ""
    # l'id della chiave (Scaleway lo chiama access key): il protocollo OpenAI
    # autentica con il solo secret, questo resta per riconoscere la chiave in uso
    access_key: str = ""
    secret_key: SecretStr = SecretStr("")
    # proposto per primo nel selettore, se abilitato dall'amministratore
    default_model: str = ""
    # righe massime che una query dell'assistente restituisce (al modello e alla chat)
    max_result_rows: int = Field(default=200, ge=1, le=2000)
    # giri del modello per messaggio: il tetto al ciclo di chiamate agli strumenti
    max_requests: int = Field(default=12, ge=1, le=50)
    # the same for the flow editor's assistant, whose turns take a model round-trip per
    # step it builds and checks (env: AI__MAX_FLOW_REQUESTS)
    max_flow_requests: int = Field(default=30, ge=1, le=80)
    # messaggi di storia che il client puo' rimandare a ogni turno
    max_history_messages: int = Field(default=60, ge=0, le=400)
    # Tetti per TURNO, applicati da pydantic-ai stesso (`UsageLimits`). Senza,
    # un utente qualsiasi puo' far spendere quanto vuole al proprietario della
    # chiave: il limite sui giri non limita i token (audit 2026-09-19, A8).
    # env: AI__MAX_COST_PER_TURN_USD, AI__MAX_INPUT_TOKENS_PER_TURN
    max_cost_per_turn_usd: float = Field(default=0.50, ge=0)
    # Tetto GIORNALIERO per utente, in dollari. Il tetto per turno da solo non
    # basta: mille turni sotto soglia costano comunque mille volte. Serve
    # soprattutto su un'installazione aperta a sconosciuti (una demo pubblica),
    # dove ogni domanda passa dalla chiave di chi ospita.
    # 0 = nessun tetto (comportamento precedente).
    # env: AI__MAX_COST_PER_DAY_USD
    max_cost_per_day_usd: float = Field(default=0, ge=0)
    # Tetto GIORNALIERO dell'INTERA installazione, in dollari. Quello per utente
    # non basta dove l'account è condiviso: su una demo pubblica tutti entrano
    # con lo stesso ospite, e il conto è uno solo. Raggiunto il tetto
    # l'assistente si dichiara non disponibile fino al giorno dopo.
    # 0 = nessun tetto.
    # env: AI__MAX_COST_PER_DAY_TOTAL_USD
    max_cost_per_day_total_usd: float = Field(default=0, ge=0)
    max_input_tokens_per_turn: int = Field(default=400_000, ge=1000)
    # Seconds for every AI call of ONE dbt export (descriptions, translations and their
    # sample reads together). The export is one request and the ingress cuts it at 300 s
    # (Helm values: proxy-read-timeout): the AI gives up first, with the reason, and leaves
    # the engine time to build the zip. env: AI__DBT_EXPORT_TIMEOUT_SECONDS
    dbt_export_timeout_seconds: float = Field(default=180, gt=0, le=3600)
    # quante chat tenere per utente (le piu' vecchie non vengono cancellate da
    # sole: e' solo il tetto dell'elenco)
    max_chats_listed: int = Field(default=100, ge=1, le=500)

    @field_validator("secret_key", mode="before")
    @classmethod
    def _ensure_secret(cls, v: object) -> SecretStr:
        return v if isinstance(v, SecretStr) else SecretStr("" if v is None else str(v))

    @property
    def enabled(self) -> bool:
        return bool(self.base_url.strip()) and bool(self.secret_key.get_secret_value().strip())


class WebhookSettings(BaseModel):
    """Chiamate verso sistemi esterni alla fine di un'esecuzione (vedi services/webhooks.py)."""

    # env: WEBHOOKS__RETENTION_DAYS — per quanti giorni si tiene il registro delle
    # consegne (riuscite o no); poi lo pulisce lo scheduler
    retention_days: int = Field(default=30, ge=1)
    # env: WEBHOOKS__TIMEOUT_SECONDS — quanto si aspetta la risposta del ricevente
    timeout_seconds: float = Field(default=10.0, gt=0, le=60)


class AuditSettings(BaseModel):
    """Per quanto si tiene il registro delle azioni.

    Il default è ZERO, cioè PER SEMPRE: è il comportamento che le installazioni
    esistenti hanno oggi, e cancellare un registro di sicurezza non può essere
    una cosa che succede a qualcuno senza che l'abbia chiesta. Chi apre
    un'installazione a sconosciuti — una demo pubblica — la accende e si tiene
    meno dati da custodire.

    Si misura in MINUTI apposta: su una demo la finestra utile è un'ora, non un
    anno, e un'unità grossa avrebbe costretto a scrivere frazioni.
    """

    # env: AUDIT__RETENTION_MINUTES — 0 = nessuna cancellazione
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
    # capacità di esecuzione simultanea usata SOLO per evidenziare le fasce critiche
    # nell'heatmap del carico schedule: dovrebbe rispecchiare la concorrenza del
    # worker 'celery' (CELERY__WORKER_CONCURRENCY sull'engine). Job schedulati nello
    # stesso minuto oltre questa soglia finiscono in coda → fascia critica.
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
    """Attese massime di un flusso sui nodi che devono COMPLETARSI prima dei passi
    a valle (così l'ordine è reale, non cosmetico). Oltre il tetto il flusso aborta
    invece di leggere dati stantii — alza i valori se hai refresh/output molto
    lunghi (tabelle enormi o rete lenta)."""
    # env: ORCHESTRATOR__REFRESH_WAIT_SECONDS — tetto d'attesa di un nodo Refresh
    refresh_wait_seconds: int = 600
    # env: ORCHESTRATOR__LOG_RETENTION_DAYS — after how many days a run's logs (a Script or
    # Container node's output, the engine's traceback) are emptied; 0 keeps them for ever
    # (services/run_log_retention.py)
    log_retention_days: int = Field(default=30, ge=0)
    # env: ORCHESTRATOR__CONTRACT_RESULTS_KEPT — the latest data contract reports kept per
    # datasource; 0 keeps them all (services/housekeeping.py)
    contract_results_kept: int = Field(default=100, ge=0)
    # env: ORCHESTRATOR__VERSIONS_KEPT — the latest versions kept per flow and per saved
    # script, the one in use always; 0 keeps them all
    versions_kept: int = Field(default=50, ge=0)
    # env: ORCHESTRATOR__AI_CHAT_RETENTION_DAYS — the assistant's conversations untouched for
    # this long are deleted (not the spending ledger); 0 keeps them
    ai_chat_retention_days: int = Field(default=90, ge=0)
    # An Output node has no wait cap of its own: the flow waits until it ends, and
    # its life is bounded by ENGINE__RUN_STALE_TIMEOUT_SECONDS (see orchestrator._wait_run).
    # env: ORCHESTRATOR__POLL_INTERVAL_SECONDS — cadenza di polling durante l'attesa
    poll_interval_seconds: float = 3.0
    # ── Coda delle orchestrazioni ────────────────────────────────────────────
    # Un'esecuzione di flusso nasce come riga «in coda» e la prende un processo
    # che fa da orchestratore (APP__ROLE): possono essere più d'uno.
    # env: ORCHESTRATOR__QUEUE_POLL_SECONDS — ogni quanto si guarda la coda
    queue_poll_seconds: float = Field(default=1.0, gt=0)
    # env: ORCHESTRATOR__MAX_CONCURRENT — orchestrazioni portate avanti insieme
    # da UN processo; le altre restano in coda. Ognuna usa una connessione a
    # Postgres mentre interroga l'engine: va tenuto sotto la misura del pool
    # (DB__POOL_SIZE), o un engine lento le esaurisce.
    # The cells of flow groups count in the same bound: past it, they wait.
    max_concurrent: int = Field(default=20, ge=1)
    # env: ORCHESTRATOR__QUEUE_TIMEOUT_SECONDS — un'esecuzione rimasta in coda
    # più di così (nessun orchestratore acceso, o tutti pieni) non viene più
    # eseguita: si chiude come fallita. Senza, un lancio fatto a orchestratori
    # spenti partirebbe ore dopo, quando nessuno se lo aspetta più.
    queue_timeout_seconds: float = Field(default=900.0, gt=0)
    # env: ORCHESTRATOR__HEARTBEAT_SECONDS — ogni quanto chi esegue dice «ci sono»
    heartbeat_seconds: float = Field(default=10.0, gt=0)
    # env: ORCHESTRATOR__DEAD_AFTER_SECONDS — silenzio oltre il quale
    # un'orchestrazione è data per persa (il processo è morto) e viene chiusa.
    # Gli orologi delle macchine devono essere allineati entro questo margine.
    dead_after_seconds: float = Field(default=60.0, gt=0)


class OpenLineageSettings(BaseModel):
    """Lineage verso un catalogo esterno nello standard OpenLineage (Marquez,
    DataHub, Astro…). Spento se non c'è né `url` né `file`. Vale anche la
    configurazione standard della libreria (OPENLINEAGE_URL / OPENLINEAGE_CONFIG)."""
    url: str = ""              # endpoint HTTP del collector, es. http://marquez:5000
    endpoint: str = "api/v1/lineage"
    api_key: str = ""          # Bearer token, se il collector lo chiede
    file: str = ""             # in alternativa o in aggiunta: JSON Lines su disco
    namespace: str = "tabularia"   # il namespace dei job di questa installazione
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


class Settings(BaseSettings):
    """
    Configurazione del gateway. Priorità: init > env > default.
    Campi annidati col delimitatore `__` (es. DB__HOST → db.host).
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
        """La chiave Fernet è OBBLIGATORIA in ogni ambiente, anche in sviluppo:
        senza, le credenziali delle connessioni non si possono cifrare né
        decifrare e le API fallirebbero al primo uso. Meglio non partire, con
        un messaggio chiaro. Chiamata allo startup (API, worker, beat)."""
        from cryptography.fernet import Fernet

        hint = (f"{FERNET_KEY_HINT}, and set it in infrastructure/.env (the same in gateway and engine), "
                "or give the services a shared SECURITY__KEYS_DIR where it is created")
        key = self.security.fernet_key
        if not key:
            raise RuntimeError(f"SECURITY__FERNET_KEY is not set: {hint}")
        try:
            Fernet(key)
        except Exception as e:  # base64 malformato, lunghezza sbagliata…
            raise RuntimeError(f"SECURITY__FERNET_KEY is not valid ({e}): {hint}") from e

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
        """Rifiuta di partire in produzione con i default di sviluppo.

        I default nel codice (jwt secret, password admin/db) esistono solo per
        far partire lo stack in dev senza attrito: in produzione DEVONO essere
        sovrascritti via env. Chiamata allo startup (lifespan).
        """
        if not self.is_production():
            return
        problems = []
        segreto = self.jwt.secret.get_secret_value()
        if segreto == "change-me-in-production":
            problems.append("JWT__SECRET is the development default (generate one: openssl rand -hex 32)")
        elif len(segreto) < JWT_SECRET_MIN:
            # firma ogni sessione (HS256): uno corto si indovina offline da un
            # solo token, e chi lo indovina si firma da sé un token da admin
            problems.append(
                f"JWT__SECRET is too short ({len(segreto)} characters, at least "
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
