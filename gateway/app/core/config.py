from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import BaseModel, Field, SecretStr, computed_field
from functools import lru_cache
from typing import Optional


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


class Settings(BaseSettings):
    """
    Configurazione del gateway. Priorità: init > env > default.
    Campi annidati col delimitatore `__` (es. DB__HOST → db.host).
    """
    model_config = SettingsConfigDict(env_nested_delimiter="__", extra="ignore")

    app: AppSettings = Field(default_factory=AppSettings)
    db: DbSettings = Field(default_factory=DbSettings)
    jwt: JwtSettings = Field(default_factory=JwtSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    engine: EngineSettings = Field(default_factory=EngineSettings)

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
        if problems:
            raise RuntimeError(
                "Configurazione NON sicura per la produzione:\n  - " + "\n  - ".join(problems)
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
