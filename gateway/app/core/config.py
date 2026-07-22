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


# ─────────────────────────────────────────────────────────────────────────────
# Engine interno (data plane): il gateway ci fa da proxy, non è esposto pubblicamente
# ─────────────────────────────────────────────────────────────────────────────
class EngineSettings(BaseModel):
    base_url: str = "http://localhost:8000"
    timeout_seconds: float = 120.0
    # bucket dello storage dell'engine: il gateway lo usa solo come STRINGA nei
    # payload (non tocca mai lo storage). Deve combaciare con STORAGE__BUCKET.
    bucket: str = "data-prep"
    # env: ENGINE__RUN_STALE_TIMEOUT_SECONDS — oltre questa età un run non terminale
    # è considerato in TIMEOUT (risultato perso o task troppo lungo) e marcato
    # FAILURE. Deve essere ≥ del task_time_limit dell'engine (3600s) + margine.
    run_stale_timeout_seconds: int = 3600 + 300


# ─────────────────────────────────────────────────────────────────────────────
# Security: chiave Fernet condivisa con l'engine per le credenziali delle
# connessioni DB (cifrate a riposo e nei payload verso l'engine)
# ─────────────────────────────────────────────────────────────────────────────
class SecuritySettings(BaseModel):
    # env: SECURITY__FERNET_KEY (stessa variabile letta dall'engine). Vuota =
    # chiave di sviluppo (vedi app/core/crypto.py); in produzione va impostata.
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

    # DEMO_MODE: sandbox pubblico. Disabilita le superfici pericolose (upload file,
    # creazione connessioni → SSRF/output esterni), scope-a-sé le viste admin e
    # maschera gli IP nell'audit. Attiva il provisioning di sessioni effimere.
    demo_mode: bool = False
    # durata di una sessione demo prima del cleanup automatico (minuti)
    demo_session_ttl_minutes: int = 120


class SchedulingSettings(BaseModel):
    # capacità di esecuzione simultanea usata SOLO per evidenziare le fasce critiche
    # nell'heatmap del carico schedule: dovrebbe rispecchiare la concorrenza del
    # worker 'celery' (CELERY__WORKER_CONCURRENCY sull'engine). Job schedulati nello
    # stesso minuto oltre questa soglia finiscono in coda → fascia critica.
    worker_capacity: int = 2


class Settings(BaseSettings):
    """
    Configurazione del gateway. Priorità: init > env > default.
    Campi annidati col delimitatore `__` (es. DB__HOST → db.host).
    """
    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")

    app: AppSettings = Field(default_factory=AppSettings)
    scheduling: SchedulingSettings = Field(default_factory=SchedulingSettings)
    db: DbSettings = Field(default_factory=DbSettings)
    jwt: JwtSettings = Field(default_factory=JwtSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    monitoring: MonitoringSettings = Field(default_factory=MonitoringSettings)

    def is_production(self) -> bool:
        return self.app.env_name.lower() in ("production", "prod")

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
        if not self.security.fernet_key:
            problems.append(
                "SECURITY__FERNET_KEY manca (le credenziali DB sarebbero cifrate con la "
                "chiave di sviluppo): genera con "
                "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
            )
        if problems:
            raise RuntimeError(
                "Configurazione NON sicura per la produzione:\n  - " + "\n  - ".join(problems)
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
