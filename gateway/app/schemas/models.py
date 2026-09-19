"""Schemi di request/response del gateway. Separati dai modelli DB per non
esporre mai `hashed_password` e per validare gli input."""
from datetime import datetime, timezone
from typing import Annotated, Generic, Literal, Optional, TypeVar
from pydantic import BaseModel, PlainSerializer, Field

# ── Datetime in uscita: sempre con l'offset ───────────────────────────────────
# Le colonne TIMESTAMP del DB sono naive e per convenzione in UTC (vedi
# scheduler.py). Serializzate cosi' com'erano, arrivavano al browser SENZA
# offset ("2026-09-16T20:57:02") e `new Date()` le leggeva come ora LOCALE:
# ogni orario dei run appariva spostato dell'offset del client (a Roma, due ore
# prima) — nel Gantt un run appena partito sembrava durare due ore. Qui si
# esplicita cio' che il DB sottintende: +00:00. Solo in JSON: in Python restano
# datetime, per non cambiare confronti e test.
def _utc_iso(d: datetime) -> str:
    return (d if d.tzinfo is not None else d.replace(tzinfo=timezone.utc)).isoformat()


UtcDateTime = Annotated[datetime, PlainSerializer(_utc_iso, return_type=str, when_used="json")]

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
    # Campi informativi per la pagina Admin. TUTTI con un default: `MeOut` eredita
    # da qui ed è costruito a mano in routes/auth.py, che passa solo i primi cinque.
    created_at: Optional[UtcDateTime] = None
    last_seen_at: Optional[UtcDateTime] = None  # ultima attività autenticata
    sso_only: bool = False  # nessuna password locale: entra solo dall'IdP
    groups: list[str] = []
    # Gruppi di amministratori a cui appartiene: è admin anche se `is_superuser`
    # (il flag PERSONALE, quello che l'interruttore modifica) è falso.
    admin_groups: list[str] = []


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
    created_at: Optional[UtcDateTime] = None
    member_count: int = 0
    is_admin: bool = False  # i membri sono amministratori


class GroupCreate(BaseModel):
    name: str
    description: str = ""


class GroupUpdate(BaseModel):
    description: Optional[str] = None
    is_admin: Optional[bool] = None


# ── Banners ───────────────────────────────────────────────────────────────────
class BannerOut(BaseModel):
    id: int
    message: str
    level: str
    created_at: Optional[UtcDateTime] = None
    created_by: Optional[int] = None


class BannerCreate(BaseModel):
    """Il livello decide solo icona e colore: un banner è sempre visibile a tutti."""
    message: str = Field(min_length=1, max_length=500)
    level: Literal["info", "warning", "danger"] = "warning"


# ── Engine policy (motori che l'installazione consente) ───────────────────────
class EnginePolicyOut(BaseModel):
    """Stato di un motore per l'installazione.

    `flows_using` è la lista di lavoro della migrazione: disabilitare un motore
    non ferma i flussi che lo usano già, quindi il numero dice quanti restano
    fuori standard — altrimenti la standardizzazione resterebbe cosmetica."""
    engine_id: str
    allowed: bool
    flows_using: int = 0


class EnginePolicyUpdate(BaseModel):
    allowed: bool


# ── Saved views (configurazioni del Viewer salvate in cartella) ───────────────
class SavedViewOut(BaseModel):
    """`datasource_name` è risolto lato server: l'elenco di una cartella deve
    poter dire SU COSA è la vista senza che il client risolva N id."""
    id: int
    name: str
    description: str = ""
    project_id: int
    owner_id: Optional[int] = None
    datasource_id: int
    datasource_name: Optional[str] = None
    # JSON opaco: la forma appartiene al Viewer, il gateway ne verifica solo la
    # validità sintattica (vedi models/saved_view.py)
    spec: str = "{}"
    created_at: Optional[UtcDateTime] = None
    updated_at: Optional[UtcDateTime] = None


class SavedViewCreate(BaseModel):
    name: str
    description: str = ""
    datasource_id: int
    spec: str = "{}"


class SavedViewUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    spec: Optional[str] = None
    project_id: Optional[int] = None  # valorizzato = sposta in un'altra cartella


# ── Ricerca unificata (per nome, su tutti i tipi di risorsa) ──────────────────
SearchKind = Literal["folder", "flow", "datasource", "view", "connection"]


class SearchHit(BaseModel):
    """Una risorsa trovata. `project_*` dice DOVE si trova: per una cartella è la
    cartella che la contiene, per tutto il resto è la cartella che la ospita —
    senza, un risultato con un nome comune non è distinguibile da un altro."""
    kind: SearchKind
    id: int
    name: str
    project_id: Optional[int] = None
    project_name: Optional[str] = None
    detail: str = ""  # riga di contesto, dipende dal tipo (es. db_type/host)


class SearchOut(BaseModel):
    """`counts` alimenta il selettore per tipo con i numeri: si calcola comunque
    per ordinare, quindi non costa una query in più."""
    items: list[SearchHit]
    total: int
    counts: dict[str, int] = Field(default_factory=dict)


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
    engine: str = "polars"  # motore di SVILUPPO (editor, run manuali)
    production_engine: Optional[str] = None  # motore dei run SCHEDULATI; null = come engine
    run_schedule: Optional[str] = None  # cron; null = non schedulato
    next_run_at: Optional[UtcDateTime] = None
    created_at: Optional[UtcDateTime] = None
    updated_at: Optional[UtcDateTime] = None
    # stato dell'ULTIMO run (per l'indicatore nella lista, senza aprire l'expander):
    # SUCCESS | FAILURE | STARTED | PENDING | null (mai eseguito)
    last_run_status: Optional[str] = None
    last_run_at: Optional[UtcDateTime] = None


class FlowDetail(FlowOut):
    definition: str


class FlowCreate(BaseModel):
    name: str
    description: str = ""
    definition: str = "{}"
    engine: str = "polars"  # scelto alla creazione
    production_engine: Optional[str] = None  # facoltativo; null/"" = come engine


class FlowScheduleUpdate(BaseModel):
    """Imposta/disabilita l'esecuzione schedulata del flusso. `cron` vuoto = off.
    `production_engine`: omesso = invariato; "" = torna uguale allo sviluppo."""
    cron: Optional[str] = None
    production_engine: Optional[str] = None


class FlowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    definition: Optional[str] = None
    project_id: Optional[int] = None  # valorizzato = sposta in un'altra cartella
    engine: Optional[str] = None  # valorizzato = cambia il motore di sviluppo
    production_engine: Optional[str] = None  # valorizzato = cambia il motore di produzione ("" = come sviluppo)


class FlowVersionOut(BaseModel):
    """Una versione della definizione di un flusso (storico + promozione)."""
    version: int
    note: str
    created_at: Optional[UtcDateTime]
    created_by: Optional[int]
    created_by_name: Optional[str] = None  # nome/email di chi ha rilasciato la versione
    is_current: bool


class FlowStatsOut(BaseModel):
    """Statistiche d'esecuzione di un flusso (dalla cronologia dei run)."""
    run_count: int
    success_count: int
    failure_count: int
    last_run_at: Optional[UtcDateTime]
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
    # opzioni specifiche del tipo (JSON). Oggi solo SMTP: mittente, modalità TLS
    # e domini ammessi. Non contiene segreti, quindi può tornare al client —
    # serve a ripopolare il form quando si modifica la connessione.
    extra: str = "{}"
    has_password: bool = False
    updated_at: Optional[UtcDateTime] = None


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
    extra: str = "{}"  # opzioni del tipo (SMTP: from_address, tls, allowed_domains)


class ConnectionUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    host: Optional[str] = None
    port: Optional[int] = None
    username: Optional[str] = None
    password: Optional[str] = None  # valorizzata = sostituisce quella cifrata
    database: Optional[str] = None
    db_schema: Optional[str] = None
    extra: Optional[str] = None
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
    # colonne per l'ORDER BY: il risultato viene ordinato prima di scriverlo
    # (parquet ordinato → pruning a valle) e le chiavi restano sulla datasource
    sort_keys: list[str] = Field(default_factory=list)


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


class RunMirrorSpec(BaseModel):
    """Copia dell'output su uno storage S3 esterno, IN AGGIUNTA alla
    pubblicazione come datasource (non al posto suo: per quella c'è
    `RunDestinationSpec` con type="s3", dove la scrittura È il risultato).

    Sempre in SOVRASCRITTURA: stessa chiave a ogni esecuzione, nessuna
    cronologia sul bucket esterno — chi legge trova sempre l'ultimo dato al
    percorso concordato. Niente partizioni, per lo stesso motivo.
    La connessione è referenziata per id: le credenziali non passano dal client.

    SOLO parquet, e non è una svista: il formato non è scelto perché nome del
    file e formato sarebbero indipendenti, e un CSV chiamato `.parquet` verrebbe
    aperto come parquet da chi lo legge a valle. Togliere la scelta rende
    l'errore impossibile invece di avvisare che è possibile."""
    connection_id: int
    bucket: str = ""  # vuoto = bucket di default della connessione
    key: str  # percorso completo dell'oggetto (sottocartella + nome file)


class RunEmailSpec(BaseModel):
    """Invio dell'output come allegato email (nodo Output con destType="email").

    I destinatari sono testo libero nella definizione del flusso, quindi questo è
    l'unico tipo di destinazione che può mandare dati verso un indirizzo
    arbitrario. Due conseguenze, entrambe applicate dal gateway e non dal client:
    i destinatari vengono validati contro i domini ammessi della connessione, e
    ogni invio finisce nell'audit.

    `stop_on_failure` (default acceso) fa interrompere la SEQUENZA del flusso, non
    solo fallire il run: i passi a valle di una notifica spesso la danno per
    avvenuta.
    """
    connection_id: int
    to: list[str] = Field(default_factory=list)
    cc: list[str] = Field(default_factory=list)
    subject: str = ""
    body: str = ""
    body_is_html: bool = False
    attachment_name: str = ""
    attachment_format: Literal["csv", "xlsx"] = "xlsx"
    stop_on_failure: bool = True


class RunCreate(BaseModel):
    bucket: str
    input_key: str
    operations: list[dict] = Field(default_factory=list)
    publish: Optional[PublishSpec] = None
    destination: Optional[RunDestinationSpec] = None
    # convive con `publish`: la datasource è il risultato, questa è la copia
    mirror: Optional[RunMirrorSpec] = None
    email: Optional[RunEmailSpec] = None


class RunOut(BaseModel):
    id: int
    kind: str = "flow"
    flow_id: Optional[int] = None
    status: str
    launched_by: Optional[int]
    trigger_type: str = "manual"  # "manual" | "schedule"
    engine: Optional[str] = None  # engine con cui è stato eseguito
    output_key: str
    rows_written: Optional[int]
    error: Optional[str]
    error_detail: Optional[str] = None  # traceback completo (dettaglio del fallimento)
    publish_name: Optional[str]
    datasource_id: Optional[int]
    destination: Optional[str] = None  # JSON: {db_type, host, database, table, mode}
    # JSON: {bucket, key, format, ok, error?} — copia best-effort su S3 esterno
    mirror: Optional[str] = None
    # JSON: {connection_id, host, to, subject, attachment, ok, error?} — invio email
    email: Optional[str] = None
    started_at: Optional[UtcDateTime]
    finished_at: Optional[UtcDateTime]


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
    # [{name, dtype, description?}] — `description` presente solo se curata
    columns: list[dict] = Field(default_factory=list)
    # {nome colonna: descrizione} (anche per colonne non più nello schema)
    column_descriptions: dict[str, str] = Field(default_factory=dict)
    # colonne di ORDER BY con cui il parquet è ordinato (vuoto = nessun ordine)
    sort_keys: list[str] = Field(default_factory=list)
    kind: str
    flow_id: Optional[int]
    # per kind="database"
    connection_id: Optional[int] = None
    source_type: Optional[str] = None
    source_ref: Optional[str] = None
    refreshed_at: Optional[UtcDateTime] = None
    # un ingest è in corso ORA (lo dice il server: vale anche per i refresh
    # schedulati e dopo una ricarica della pagina). Solo negli elenchi.
    refreshing: bool = False
    # refresh schedulato (cron); next_refresh_at = prossima esecuzione prevista
    refresh_schedule: Optional[str] = None
    next_refresh_at: Optional[UtcDateTime] = None
    updated_at: Optional[UtcDateTime] = None


class DatasourceUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    project_id: Optional[int] = None  # valorizzato = sposta in un'altra cartella
    # valorizzato = SOSTITUISCE la mappa {colonna: descrizione}; voci vuote scartate
    column_descriptions: Optional[dict[str, str]] = None


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
    # colonne per l'ORDER BY dell'ingest (vuoto = nessun ordine, come prima)
    sort_keys: list[str] = Field(default_factory=list)


class SharePointDatasourceCreate(BaseModel):
    """Datasource da file Excel su SharePoint: un percorso (anche con glob) e un
    foglio. Più file corrispondenti vengono impilati in una tabella sola."""
    name: str
    description: str = ""
    connection_id: int
    path: str = Field(min_length=1, max_length=1000)
    sheet: str = Field(min_length=1, max_length=255)


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
