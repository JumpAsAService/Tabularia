"""Schemi di request/response del gateway. Separati dai modelli DB per non
esporre mai `hashed_password` e per validare gli input."""
from datetime import datetime
from typing import Generic, Literal, Optional, TypeVar
from pydantic import BaseModel, Field

from app.models.permission import Capability

T = TypeVar("T")


class Page(BaseModel, Generic[T]):
    """Una pagina di risultati: gli elementi + il totale che combacia col filtro
    (per mostrare 'X–Y di Z' e navigare). Il filtro `q` gira SEMPRE sul dataset
    intero lato server, non sulla pagina corrente."""
    items: list[T]
    total: int


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
    owner_name: Optional[str] = None  # nome di chi ha creato il flusso (risolto)
    engine: str = "polars"  # motore di esecuzione (polars | duckdb)
    run_schedule: Optional[str] = None  # cron; null = non schedulato
    next_run_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class FlowDetail(FlowOut):
    definition: str


class FlowCreate(BaseModel):
    name: str
    description: str = ""
    definition: str = "{}"
    engine: str = "polars"  # scelto alla creazione


class FlowScheduleUpdate(BaseModel):
    """Imposta/disabilita l'esecuzione schedulata del flusso. `cron` vuoto = off."""
    cron: Optional[str] = None


class FlowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    definition: Optional[str] = None
    project_id: Optional[int] = None  # valorizzato = sposta in un'altra cartella
    engine: Optional[str] = None  # valorizzato = cambia il motore di esecuzione


class FlowVersionOut(BaseModel):
    """Una versione della definizione di un flusso (storico + promozione)."""
    version: int
    note: str
    created_at: Optional[datetime]
    created_by: Optional[int]
    created_by_name: Optional[str] = None  # nome/email di chi ha rilasciato la versione
    is_current: bool


class FlowStatsOut(BaseModel):
    """Statistiche d'esecuzione di un flusso (dalla cronologia dei run)."""
    run_count: int
    success_count: int
    failure_count: int
    last_run_at: Optional[datetime]
    avg_duration_seconds: Optional[float]


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
    # se una datasource con questo nome esiste già nella cartella: sovrascrivila
    # (solo se è kind="flow") invece di fallire con 409
    overwrite: bool = False


class RunDestinationSpec(BaseModel):
    """Destinazione dell'output (nodo Output del flusso): tabella di database
    o oggetto/dataset su S3. La connessione è referenziata per id, le
    credenziali non passano MAI dal client."""
    type: Literal["database", "s3"] = "database"
    connection_id: int
    # type="database"
    table: str = ""
    mode: Literal["append", "replace"] = "append"
    post_sql: str = ""
    # type="s3"
    bucket: str = ""  # vuoto = bucket di default della connessione
    key: str = ""  # chiave del file, o prefisso se partizionato
    format: Literal["parquet", "csv"] = "parquet"
    partition_by: list[str] = Field(default_factory=list)  # hive: colonna=valore/…


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
    trigger_type: str = "manual"  # "manual" | "schedule"
    output_key: str
    rows_written: Optional[int]
    error: Optional[str]
    error_detail: Optional[str] = None  # traceback completo (dettaglio del fallimento)
    publish_name: Optional[str]
    datasource_id: Optional[int]
    destination: Optional[str] = None  # JSON: {db_type, host, database, table, mode}
    started_at: Optional[datetime]
    finished_at: Optional[datetime]


class RunSearchOut(RunOut):
    """Un run nella ricerca globale delle esecuzioni: come RunOut + i nomi del
    flusso / della datasource / di chi l'ha avviato, per mostrarli senza
    risolverli lato client."""
    flow_name: Optional[str] = None
    source_name: Optional[str] = None
    launched_by_name: Optional[str] = None  # None se schedulato o utente rimosso


class ActivityBucket(BaseModel):
    """Conteggi di un bucket del calendar plot: un giorno (key=YYYY-MM-DD) oppure
    un'ora (key='00'..'23'). Ogni run è un evento; il breakdown distingue esito
    (successi/falliti) e origine (manuali/schedulati)."""
    key: str
    total: int
    success: int
    failure: int
    scheduled: int
    manual: int


class RunActivityOut(BaseModel):
    """Attività delle esecuzioni per il calendar plot della pagina Flows: buckets
    per GIORNO (heatmap) o per ORA (drill-down). I bucket sono in ora LOCALE del
    client (passata via tz_offset); from_key/to_key delimitano la finestra."""
    granularity: Literal["day", "hour"]
    from_key: str
    to_key: str
    buckets: list[ActivityBucket]


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
    # refresh schedulato (cron); next_refresh_at = prossima esecuzione prevista
    refresh_schedule: Optional[str] = None
    next_refresh_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class DatasourceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    project_id: Optional[int] = None  # valorizzato = sposta in un'altra cartella


class ScheduleUpdate(BaseModel):
    """Imposta/aggiorna lo schedule del refresh. `cron` vuoto/None = disabilita."""
    cron: Optional[str] = None


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
