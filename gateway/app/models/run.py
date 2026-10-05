from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, Index, Text, text


def _now() -> datetime:
    return datetime.now(timezone.utc)


# la condizione dell'indice unico parziale (anche nelle migrazioni di db/session.py)
LIVE_ORCHESTRATION = "kind = 'orchestration' AND status = 'STARTED'"

# cosa si scrive su un'orchestrazione che nessuno finirà (niente apici: entra
# anche in un'istruzione SQL delle migrazioni)
ORCHESTRATION_INTERRUPTED = (
    "Orchestrazione interrotta: il processo che la eseguiva si è fermato e i passi rimanenti "
    "non sono stati eseguiti. Controlla cosa è già stato prodotto e rilancia il flusso."
)


class Run(SQLModel, table=True):
    """
    Un'esecuzione di un flusso: la riga nasce al lancio (PENDING) e viene
    riconciliata pigramente con lo stato del task Celery ogni volta che
    qualcuno la legge (nessun poller in background: chi guarda, aggiorna).

    Se al lancio è stata chiesta la pubblicazione (`publish_name`), al passaggio
    a SUCCESS l'output diventa una Datasource riusabile come sorgente.
    """
    __tablename__ = "runs"
    __table_args__ = (
        # Di un flusso gira UNA orchestrazione alla volta, e lo garantisce il
        # database: con più processi che eseguono, un elenco in memoria non lo
        # vede nessun altro. Chi prova a far partire la seconda riceve l'errore
        # dell'indice (vedi `orchestrator.rivendica`).
        Index(
            "uq_runs_one_live_orchestration",
            "flow_id",
            unique=True,
            postgresql_where=text(LIVE_ORCHESTRATION),
            sqlite_where=text(LIVE_ORCHESTRATION),
        ),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    # kind="flow": esecuzione di un flusso (flow_id valorizzato).
    # kind="ingest": refresh di una datasource database (datasource_id valorizzato).
    kind: str = Field(default="flow", index=True)
    flow_id: Optional[int] = Field(default=None, foreign_key="flows.id", index=True)
    task_id: str = Field(index=True)  # id del task Celery sull'engine
    status: str = "PENDING"  # PENDING | STARTED | SUCCESS | FAILURE
    launched_by: Optional[int] = Field(default=None, foreign_key="users.id")
    # come è stato avviato: "manual" (un utente) oppure "schedule" (lo scheduler);
    # per gli schedulati `launched_by` è l'autore dello schedule, ma in cronologia
    # mostriamo semplicemente "schedule". Colonna `trigger_type`: "trigger" è
    # parola riservata SQL.
    trigger_type: str = Field(default="manual")
    # engine con cui il run è stato ESEGUITO (dev o produzione del flusso al
    # momento del lancio): in cronologia si vede con cosa ha girato davvero
    engine: Optional[str] = None
    # run di orchestrazione che ha generato questo run figlio (output/refresh
    # lanciati DENTRO un'orchestrazione). None = esecuzione di ALTO LIVELLO
    # (orchestrazione, run diretto dell'editor, refresh standalone): così il
    # calendar plot conta le esecuzioni una sola volta, senza i doppioni figli.
    parent_run_id: Optional[int] = Field(default=None, foreign_key="runs.id", index=True)

    input_key: str
    output_bucket: str
    output_key: str
    rows_written: Optional[int] = None
    error: Optional[str] = Field(default=None, sa_column=Column(Text))  # sintesi (per la lista)
    # traceback completo dell'engine (per capire la causa): mostrato nel dettaglio
    error_detail: Optional[str] = Field(default=None, sa_column=Column("error_detail", Text))

    # richiesta di pubblicazione (facoltativa, decisa al lancio); per i run
    # kind="ingest" `datasource_id` è la datasource che il refresh aggiorna
    publish_name: Optional[str] = None
    publish_project_id: Optional[int] = Field(default=None, foreign_key="projects.id")
    publish_description: str = ""
    # sovrascrivi la datasource omonima (kind="flow") invece di crearne una nuova
    publish_overwrite: bool = False
    # colonne di ORDER BY promesse alla datasource pubblicata (JSON): il risultato
    # esce ordinato e la datasource le eredita (vedi _publish_datasource)
    publish_sort_keys: str = "[]"
    datasource_id: Optional[int] = Field(default=None, foreign_key="datasources.id")

    # destinazione database dell'output (nodo Output): riassunto JSON per la
    # cronologia — {connection_id, db_type, host, database, table, mode}
    destination: Optional[str] = Field(default=None, sa_column=Column("destination", Text))

    # copia dell'output su uno storage S3 esterno, IN AGGIUNTA alla datasource
    # pubblicata: riassunto JSON dell'esito — {bucket, key, format, ok, error?}.
    # È BEST-EFFORT per scelta: la datasource è il risultato primario, questa è
    # una consegna a valle, e un suo fallimento non deve annullare la
    # pubblicazione. L'errore finisce qui perché altrimenti resterebbe solo nei
    # log del worker, invisibile a chi guarda la cronologia.
    mirror: Optional[str] = Field(default=None, sa_column=Column("mirror", Text))

    # invio dell'output come allegato email (nodo Output destType="email"):
    # riassunto JSON — {connection_id, host, to, subject, attachment, ok, error?}.
    # A differenza della copia NON è best-effort: un report che non parte è il
    # risultato che non c'è. Il dettaglio sta qui perché l'audit registra COSA è
    # uscito e verso chi, mentre questa colonna dice se è arrivato.
    email: Optional[str] = Field(default=None, sa_column=Column("email", Text))

    # ── solo per kind="orchestration" ───────────────────────────────────────
    # Nasce PENDING = in coda; chi la esegue la porta a STARTED firmandola.
    # "development" | "production": con quale motore lanciare gli output
    engine_mode: Optional[str] = None
    # il processo che la sta eseguendo, e l'ultima volta che ha detto «ci sono».
    # Un'orchestrazione STARTED il cui battito è fermo da troppo è di un processo
    # morto: nessuno la finirà, e va chiusa dicendolo.
    claimed_by: Optional[str] = None
    heartbeat_at: Optional[datetime] = None

    started_at: datetime = Field(default_factory=_now)
    # istante in cui il task ha cominciato a girare DAVVERO sul worker (Celery
    # riporta STARTED, `task_track_started=True`). `started_at` nasce col lancio e
    # include l'attesa in coda, quindi non misura l'esecuzione: con i worker
    # occupati un run appena partito risulterebbe già scaduto. Il timeout di
    # staleness parte da qui. None = non ancora osservato in esecuzione.
    engine_started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None


TERMINAL_STATES = {"SUCCESS", "FAILURE"}
