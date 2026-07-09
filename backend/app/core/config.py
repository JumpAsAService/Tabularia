from pydantic_settings import (
    BaseSettings,
    SettingsConfigDict,
    PydanticBaseSettingsSource,
    TomlConfigSettingsSource,
)
from pydantic import BaseModel, Field, computed_field, field_validator, SecretStr
from functools import lru_cache
from pathlib import Path
from typing import Optional


# Nota: i blocchi annidati sono BaseModel *puri*. Tutto il caricamento (env +
# TOML) avviene sul `Settings` top-level tramite `env_nested_delimiter="__"`,
# così l'env vince sempre sul TOML per ogni campo (es. REDIS__HOST batte il
# valore di config.toml). Vedi la nota sulle priorità in `Settings`.


# ─────────────────────────────────────────────────────────────────────────────
# Redis Configuration
# ─────────────────────────────────────────────────────────────────────────────
class RedisSettings(BaseModel):
    host: str = Field(default="localhost", description="Redis host")
    port: int = Field(default=6379, description="Redis port")
    db: int = Field(default=0, description="Redis database number")
    password: Optional[SecretStr] = Field(default=None, description="Redis password")

    @computed_field
    @property
    def url(self) -> str:
        if self.password:
            return f"redis://:{self.password.get_secret_value()}@{self.host}:{self.port}/{self.db}"
        return f"redis://{self.host}:{self.port}/{self.db}"

    def __str__(self):
        return f"RedisSettings(host={self.host}, port={self.port}, db={self.db})"


# ─────────────────────────────────────────────────────────────────────────────
# Storage Configuration (qualsiasi endpoint S3-compatibile: MinIO, AWS, R2…)
# ─────────────────────────────────────────────────────────────────────────────
class StorageSettings(BaseModel):
    endpoint: str = Field(default="http://localhost:9002", description="Storage endpoint")
    access_key: str = Field(default="minioadmin", description="Access key")
    secret_key: SecretStr = Field(default=SecretStr("minioadmin"), description="Secret key")
    bucket: str = "data-prep"
    region: str = "us-east-1"

    @field_validator("secret_key", mode="before")
    @classmethod
    def _ensure_secret(cls, v: object) -> SecretStr:
        # pydantic-settings non riavvolge in SecretStr i valori che arrivano dal
        # TOML (restano str): forziamo la coercizione da qualunque sorgente.
        return v if isinstance(v, SecretStr) else SecretStr(str(v))


# ─────────────────────────────────────────────────────────────────────────────
# Celery Configuration
# ─────────────────────────────────────────────────────────────────────────────
class CelerySettings(BaseModel):
    broker_url: Optional[str] = None
    result_backend: Optional[str] = None
    worker_concurrency: int = 2
    task_serializer: str = "json"
    result_serializer: str = "json"
    timezone: str = "UTC"
    enable_utc: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Cache Configuration (step cache dell'engine)
# ─────────────────────────────────────────────────────────────────────────────
class CacheSettings(BaseModel):
    # env: CACHE__TTL_SECONDS — dopo quanto tempo dall'ultimo accesso una voce
    # di cache viene rimossa (blob parquet + indice Valkey). Default 7 giorni.
    ttl_seconds: int = 7 * 24 * 3600
    # env: CACHE__SWEEP_INTERVAL_SECONDS — ogni quanto gira l'eviction. Default 1h.
    sweep_interval_seconds: int = 3600


# ─────────────────────────────────────────────────────────────────────────────
# Metrics Configuration (osservabilità)
# ─────────────────────────────────────────────────────────────────────────────
class MetricsSettings(BaseModel):
    # env: METRICS__STORAGE_STATS_INTERVAL_SECONDS — ogni quanto campionare la
    # dimensione dei prefissi di storage (cache/, datasets/, ...). Default 60s.
    storage_stats_interval_seconds: int = 60


# ─────────────────────────────────────────────────────────────────────────────
# Security (segreti condivisi col gateway)
# ─────────────────────────────────────────────────────────────────────────────
class SecuritySettings(BaseModel):
    # env: SECURITY__FERNET_KEY — chiave condivisa gateway↔engine con cui le
    # credenziali delle connessioni DB viaggiano/riposano cifrate. Vuota = chiave
    # di sviluppo (vedi app/core/crypto.py); in produzione va impostata.
    fernet_key: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# App Configuration
# ─────────────────────────────────────────────────────────────────────────────
class AppSettings(BaseModel):
    name: str = "Data Prep API"
    version: str = "0.1.0"
    debug: bool = False
    api_v1_prefix: str = "/api/v1"
    env_name: str = "development"
    # origini permesse per il CORS (il frontend Nuxt gira su :3000)
    cors_origins: list[str] = Field(
        default_factory=lambda: ["http://localhost:3000", "http://127.0.0.1:3000"]
    )


# ─────────────────────────────────────────────────────────────────────────────
# Main Settings (composes all service settings)
# ─────────────────────────────────────────────────────────────────────────────
class Settings(BaseSettings):
    """
    Main configuration class that composes all service settings.
    
    Priority order (dalla più alta alla più bassa):
    1. Init (argomenti al costruttore)
    2. Variabili d'ambiente (per docker/produzione) — es. `REDIS__HOST=redis`
    3. secrets.toml (secret locali)
    4. config.toml (default locali committabili)
    5. Default delle classi

    I campi annidati usano il delimitatore `__`: `REDIS__HOST` → `redis.host`,
    `STORAGE__ENDPOINT` → `storage.endpoint`, ecc. Così l'env sovrascrive sempre
    il TOML per singolo campo (deep-merge), come da priorità sopra.
    """
    model_config = SettingsConfigDict(
        env_nested_delimiter="__",
        extra="ignore",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Nested service configurations
    # ─────────────────────────────────────────────────────────────────────────
    app: AppSettings = Field(default_factory=AppSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    celery: CelerySettings = Field(default_factory=CelerySettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    metrics: MetricsSettings = Field(default_factory=MetricsSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)

    # ─────────────────────────────────────────────────────────────────────────
    # Customise sources to include TOML files
    # ─────────────────────────────────────────────────────────────────────────
    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """
        Aggiunge i file TOML come sorgenti di configurazione.

        `secrets.toml` e `config.toml` sono due sorgenti *separate* (non una lista
        passata a un unico source): un'unica lista farebbe un merge shallow di
        primo livello e la `[storage]` dei secret cancellerebbe quella dei
        default. Sorgenti separate → deep-merge per campo, con secrets prima dei
        default.

        Priorità (dalla più alta): init > env > secrets.toml > config.toml.
        """
        config_dir = Path(__file__).parent.parent.parent / "config"
        secrets_toml = TomlConfigSettingsSource(settings_cls, toml_file=config_dir / "secrets.toml")
        config_toml = TomlConfigSettingsSource(settings_cls, toml_file=config_dir / "config.toml")
        return (init_settings, env_settings, secrets_toml, config_toml, file_secret_settings)

    # ─────────────────────────────────────────────────────────────────────────
    # Helper properties for quick access
    # ─────────────────────────────────────────────────────────────────────────
    @computed_field
    @property
    def celery_broker_url(self) -> str:
        """Returns Celery broker URL (from celery config or fallback to redis)"""
        return self.celery.broker_url or self.redis.url

    @computed_field
    @property
    def celery_result_backend(self) -> str:
        """Returns Celery result backend (from celery config or fallback to redis)"""
        return self.celery.result_backend or self.redis.url

    @property
    def project_root(self) -> Path:
        return Path(__file__).parent.parent.parent

    def is_production(self) -> bool:
        """Check if running in production environment"""
        return self.app.env_name.lower() in ("production", "prod")

    def is_development(self) -> bool:
        """Check if running in development environment"""
        return self.app.env_name.lower() in ("development", "dev", "local")


# ─────────────────────────────────────────────────────────────────────────────
# Singleton instance
# ─────────────────────────────────────────────────────────────────────────────
@lru_cache
def get_settings() -> Settings:
    """
    Returns cached settings instance.
    
    Usage:
        settings = get_settings()
        settings.redis.url
        settings.storage.bucket
    """
    return Settings()

