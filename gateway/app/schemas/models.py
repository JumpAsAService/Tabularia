"""Schemi di request/response del gateway. Separati dai modelli DB per non
esporre mai `hashed_password` e per validare gli input."""
from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field

from app.models.permission import Capability


# Nota: l'email è un semplice `str`, non `EmailStr`. È uno strumento interno: gli
# admin usano spesso domini riservati (es. *.local, *.internal) che il validatore
# di deliverability rifiuterebbe. Qui l'email è solo un identificativo di login.


# ── Auth ──────────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ── Users ─────────────────────────────────────────────────────────────────────
class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    is_active: bool
    is_superuser: bool


class MeOut(UserOut):
    groups: list[str] = []


class UserCreate(BaseModel):
    email: str
    password: str = Field(min_length=6)
    full_name: str = ""
    is_superuser: bool = False


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    password: Optional[str] = Field(default=None, min_length=6)
    is_active: Optional[bool] = None
    is_superuser: Optional[bool] = None


# ── Groups ────────────────────────────────────────────────────────────────────
class GroupOut(BaseModel):
    id: int
    name: str
    description: str


class GroupCreate(BaseModel):
    name: str
    description: str = ""


# ── Projects ──────────────────────────────────────────────────────────────────
class ProjectOut(BaseModel):
    id: int
    name: str
    description: str
    parent_id: Optional[int]
    owner_id: Optional[int]


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    parent_id: Optional[int] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[int] = None


# ── Flows ─────────────────────────────────────────────────────────────────────
class FlowOut(BaseModel):
    """Voce di lista: senza `definition` (può pesare, serve solo all'editor)."""
    id: int
    name: str
    description: str
    project_id: int
    owner_id: Optional[int]
    updated_at: Optional[datetime] = None


class FlowDetail(FlowOut):
    definition: str


class FlowCreate(BaseModel):
    name: str
    description: str = ""
    definition: str = "{}"


class FlowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    definition: Optional[str] = None
    project_id: Optional[int] = None  # valorizzato = sposta in un'altra cartella


# ── Connections (connessioni a database esterni) ─────────────────────────────
class ConnectionOut(BaseModel):
    """La password NON esce mai dalle API: solo un flag che dice se è impostata."""
    id: int
    name: str
    description: str
    project_id: int
    owner_id: Optional[int]
    db_type: str
    host: str
    port: Optional[int]
    username: str
    database: str
    db_schema: str
    has_password: bool = False
    updated_at: Optional[datetime] = None


class ConnectionCreate(BaseModel):
    name: str
    description: str = ""
    db_type: str
    host: str
    port: Optional[int] = None
    username: str = ""
    password: str = ""  # in chiaro solo nel body della richiesta; cifrata a riposo
    database: str = ""
    db_schema: str = ""


class ConnectionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None  # valorizzata = sostituisce quella cifrata
    database: Optional[str] = None
    db_schema: Optional[str] = None
    project_id: Optional[int] = None  # valorizzato = sposta in un'altra cartella


# ── Runs (esecuzioni dei flussi) ──────────────────────────────────────────────
class PublishSpec(BaseModel):
    """Richiesta di pubblicare l'output del run come datasource nominata."""
    name: str
    project_id: int
    description: str = ""


class RunDestinationSpec(BaseModel):
    """Destinazione database dell'output (nodo Output del flusso): la
    connessione è referenziata per id, le credenziali non passano MAI dal client."""
    connection_id: int
    table: str
    mode: Literal["append", "replace"] = "append"
    post_sql: str = ""


class RunCreate(BaseModel):
    bucket: str
    input_key: str
    operations: list[dict] = Field(default_factory=list)
    publish: Optional[PublishSpec] = None
    destination: Optional[RunDestinationSpec] = None


class RunOut(BaseModel):
    id: int
    kind: str = "flow"
    flow_id: Optional[int] = None
    status: str
    launched_by: Optional[int]
    output_key: str
    rows_written: Optional[int]
    error: Optional[str]
    publish_name: Optional[str]
    datasource_id: Optional[int]
    destination: Optional[str] = None  # JSON: {db_type, host, database, table, mode}
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


# ── Datasources (dataset nominati nel catalogo) ───────────────────────────────
class DatasourceOut(BaseModel):
    id: int
    name: str
    description: str
    project_id: int
    owner_id: Optional[int]
    bucket: str
    key: str
    rows: Optional[int]
    columns: list[dict] = Field(default_factory=list)
    kind: str
    flow_id: Optional[int]
    # per kind="database"
    connection_id: Optional[int] = None
    source_type: Optional[str] = None
    source_ref: Optional[str] = None
    refreshed_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DatasourceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    project_id: Optional[int] = None  # valorizzato = sposta in un'altra cartella


class DbDatasourceCreate(BaseModel):
    """Datasource da database: definizione della sorgente + primo ingest."""
    name: str
    description: str = ""
    connection_id: int
    source_type: str  # table | sql
    source_ref: str  # nome tabella oppure testo SQL


# ── Permissions ───────────────────────────────────────────────────────────────
class PermissionOut(BaseModel):
    id: int
    project_id: int
    user_id: Optional[int]
    group_id: Optional[int]
    capability: str


class PermissionCreate(BaseModel):
    capability: Capability
    user_id: Optional[int] = None
    group_id: Optional[int] = None
