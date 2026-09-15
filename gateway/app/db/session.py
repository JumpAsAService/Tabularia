import logging
import time

from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlmodel import create_engine, select, Session, SQLModel

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# pool_pre_ping: evita connessioni morte dopo idle; utile in dev con restart di Postgres.
engine = create_engine(get_settings().db.dsn, echo=False, pool_pre_ping=True)


def wait_for_db(max_attempts: int = 30, delay: float = 2.0) -> None:
    """Attende che il DB sia raggiungibile prima di inizializzare lo schema.

    Dopo un reboot dell'host (o un restart di Postgres) il gateway può partire
    prima che l'host `postgres` sia risolvibile dal DNS Docker: senza attesa
    l'app uscirebbe all'avvio e — con `uvicorn --reload` — il container resterebbe
    "running" ma non servirebbe nulla (il supervisore del reload non esce, quindi
    `restart: unless-stopped` non scatta). Con il retry, invece, riparte da solo.
    `depends_on: service_healthy` NON basta: vale solo su `compose up`, non quando
    è il daemon Docker a riaccendere i container dopo un reboot."""
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            if attempt > 1:
                logger.info("DB raggiungibile dopo %d tentativi", attempt)
            return
        except OperationalError as e:  # DNS non risolto o Postgres non ancora pronto
            last_err = e
            logger.warning("DB non pronto (tentativo %d/%d), riprovo tra %.0fs…",
                           attempt, max_attempts, delay)
            time.sleep(delay)
    raise RuntimeError(f"DB non raggiungibile dopo {max_attempts} tentativi") from last_err


# create_all crea le tabelle NUOVE ma non altera quelle esistenti: le colonne
# aggiunte a tabelle già in produzione vanno dichiarate qui come ALTER
# idempotenti (IF NOT EXISTS). Quando lo schema evolverà davvero → Alembic.
_MIGRATIONS = [
    # Fase C: run di ingest (refresh datasource database) accanto ai run di flusso
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS kind VARCHAR NOT NULL DEFAULT 'flow'",
    "ALTER TABLE runs ALTER COLUMN flow_id DROP NOT NULL",
    # Fase C: datasource kind="database" (connessione + definizione sorgente)
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS connection_id INTEGER REFERENCES connections(id)",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS source_type VARCHAR",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS source_ref TEXT",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS refreshed_at TIMESTAMP",
    # Nodo Output: destinazione database dell'output del run (riassunto JSON)
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS destination TEXT",
    # Refresh schedulato delle datasource database
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS refresh_schedule VARCHAR",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS refresh_scheduled_by INTEGER REFERENCES users(id)",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS next_refresh_at TIMESTAMP",
    # Esecuzione schedulata dei flussi
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS run_schedule VARCHAR",
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS run_scheduled_by INTEGER REFERENCES users(id)",
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS next_run_at TIMESTAMP",
    # Publish di una datasource in overwrite (ripubblica sopra l'omonima kind=flow)
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS publish_overwrite BOOLEAN NOT NULL DEFAULT FALSE",
    # Dettaglio errore (traceback engine) per il debug dei run falliti
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS error_detail TEXT",
    # Origine dell'avvio: 'manual' (utente) | 'schedule' (scheduler)
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS trigger_type VARCHAR NOT NULL DEFAULT 'manual'",
    # Run figlio di un'orchestrazione (per non contare i doppioni nel calendario)
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS parent_run_id INTEGER REFERENCES runs(id)",
    # Engine di esecuzione scelto per il flusso (polars | duckdb)
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS engine VARCHAR NOT NULL DEFAULT 'polars'",
    # Descrizioni dei campi delle datasource (JSON nome→testo)
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS column_descriptions TEXT NOT NULL DEFAULT '{}'",
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS sort_keys TEXT NOT NULL DEFAULT '[]'",
    # Engine di produzione del flusso (run schedulati); NULL = come lo sviluppo
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS production_engine VARCHAR",
    # Engine con cui ciascun run è stato eseguito
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS engine VARCHAR",
    # Audit: ultima attività autenticata (per le "sessioni attive")
    # utenti solo-SSO: nessuna password locale (vedi services/sso.py)
    "ALTER TABLE users ALTER COLUMN hashed_password DROP NOT NULL",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMP",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_seen_ip VARCHAR",
    # Inizio REALE dell'esecuzione sul worker: il timeout di staleness non deve
    # contare l'attesa in coda (vedi models/run.py)
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS engine_started_at TIMESTAMP",
    # copia best-effort dell'output su S3 esterno: esito JSON, NULL = non richiesta
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS mirror TEXT",
    # Opzioni specifiche del tipo di connessione (JSON). Serve all'SMTP, che ha
    # tre impostazioni senza una colonna naturale dove stare — mittente, modalità
    # TLS e domini ammessi — e spremere `database`/`db_schema` come si fa per S3
    # ne avrebbe comunque lasciata fuori una. Gli altri tipi non la usano.
    "ALTER TABLE connections ADD COLUMN IF NOT EXISTS extra TEXT NOT NULL DEFAULT '{}'",
    # Invio email dell'output (nodo Output destType=email): esito JSON, NULL = non richiesto
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS email TEXT",
    # Run che ha prodotto lo snapshot corrente: rifiuta gli swap da run più
    # vecchi (vedi models/datasource.py). Intero semplice, nessuna FK: eviterebbe
    # un ciclo runs ↔ datasources
    "ALTER TABLE datasources ADD COLUMN IF NOT EXISTS snapshot_run_id INTEGER",
    # Identità SSO stabile (issuer + subject del token). L'SSO riconosce l'utente
    # da qui, non dall'email — vedi models/user.py e services/sso.py.
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_issuer VARCHAR",
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS oidc_subject VARCHAR",
    # Una stessa identità dell'IdP non può appartenere a due account. Indice
    # PARZIALE: gli account senza SSO hanno entrambe le colonne nulle e non
    # devono collidere fra loro.
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_oidc_identity ON users (oidc_issuer, oidc_subject)"
    " WHERE oidc_subject IS NOT NULL",
]


def init_db() -> None:
    """Crea le tabelle se non esistono e applica gli ALTER idempotenti."""
    wait_for_db()  # tollera il DB non ancora pronto (reboot, restart di Postgres)
    import app.models  # noqa: F401 — registra i modelli su SQLModel.metadata
    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        for stmt in _MIGRATIONS:
            conn.execute(text(stmt))


def backfill_flow_versions() -> None:
    """Ogni flusso senza storico riceve una v1 con la definizione corrente (per i
    flussi creati prima del versioning). Idempotente: a regime non carica righe."""
    from app.models import Flow, FlowVersion

    with Session(engine) as session:
        missing = session.exec(
            select(Flow).where(Flow.id.not_in(select(FlowVersion.flow_id)))
        ).all()
        for f in missing:
            session.add(
                FlowVersion(flow_id=f.id, version=1, definition=f.definition, note="baseline", created_by=f.owner_id)
            )
        if missing:
            session.commit()


def get_session():
    with Session(engine) as session:
        yield session
