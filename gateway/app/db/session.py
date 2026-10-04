import logging
import time

from sqlalchemy import event, text
from sqlalchemy.exc import InvalidatePoolError, OperationalError
from sqlmodel import create_engine, select, Session, SQLModel

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Una connessione usata un attimo fa è viva. Chiederglielo a OGNI uso
# (`pool_pre_ping`) costa un giro verso Postgres per ogni passo di ogni
# richiesta: sotto carico era il 14% del tempo del gateway. Si chiede solo a
# quelle rimaste ferme nel pool, che sono le uniche che un riavvio di Postgres
# o un firewall possono aver chiuso senza che nessuno se ne accorgesse.
PING_SE_FERMA_DA_S = 5.0


def ping_solo_se_ferma(eng, ferma_da_s: float = PING_SE_FERMA_DA_S) -> None:
    """Prova la connessione all'uscita dal pool solo se è ferma da un po'.

    Se non risponde butta via tutte quelle aperte prima (come fa `pool_pre_ping`)
    e il pool ne apre una nuova: chi l'ha chiesta non vede niente. Quel che resta
    scoperto è Postgres che cade nei secondi subito dopo un uso: la prima
    richiesta fallisce, e il suo errore invalida il pool per le successive."""

    @event.listens_for(eng, "connect")
    def _nuova(dbapi_conn, record):
        record.info["resa"] = time.monotonic()

    @event.listens_for(eng, "checkin")
    def _resa(dbapi_conn, record):
        record.info["resa"] = time.monotonic()

    @event.listens_for(eng, "checkout")
    def _presa(dbapi_conn, record, proxy):
        if time.monotonic() - record.info.get("resa", 0.0) < ferma_da_s:
            return
        try:
            eng.dialect.do_ping(dbapi_conn)
        except Exception as e:
            raise InvalidatePoolError("connessione morta mentre era ferma nel pool") from e


def _crea_engine(**opzioni):
    eng = create_engine(get_settings().db.dsn, echo=False, **opzioni)
    ping_solo_se_ferma(eng)
    return eng


def _misura_del_pool() -> int:
    s = get_settings()
    return s.db.pool_size or s.app.gateway_threads


# Ogni thread tiene al massimo una connessione alla volta (vedi `a_fine_rotta`),
# quindi il pool va a misura dei thread. Con i 5 della libreria le altre erano
# «di troppo»: aperte e CHIUSE a ogni richiesta — 16.000 sessioni su Postgres in
# dieci minuti di carico, e ogni apertura è un processo che Postgres fa nascere.
engine = _crea_engine(pool_size=_misura_del_pool(), max_overflow=get_settings().db.max_overflow)

# Poche connessioni tenute SEMPRE in autocommit, per le letture «una e via»
# (l'utente che ogni richiesta carica): lì una SELECT è un giro solo verso
# Postgres, senza BEGIN né ROLLBACK attorno. Sono connessioni a parte, e non
# quelle del pool principale messe in autocommit per l'occasione, perché il
# driver nel rimetterle a posto manda a Postgres un comando in più a ogni
# richiesta (`SET default_transaction_isolation`). Chi ne prende una la tiene
# per una SELECT e non aspetta nient'altro: se sono tutte occupate si fa la
# coda per qualche millisecondo, senza il rischio di stallo delle altre.
_engine_letture = _crea_engine(
    isolation_level="AUTOCOMMIT", pool_size=max(2, _misura_del_pool() // 4), max_overflow=0
)
_LETTURE = {engine: _engine_letture}


def engine_di_lettura(bind):
    """L'engine in autocommit che legge lo stesso database di `bind`."""
    return _LETTURE.get(bind) or bind.execution_options(isolation_level="AUTOCOMMIT")


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
    "ALTER TABLE runs ADD COLUMN IF NOT EXISTS publish_sort_keys VARCHAR NOT NULL DEFAULT '[]'",
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
    # Gruppo di amministratori: i membri sono admin (services/permissions.is_admin)
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE",
    # Avviso di fallimento di un'esecuzione programmata (vedi models/flow.py)
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS notify_emails TEXT",
    "ALTER TABLE flows ADD COLUMN IF NOT EXISTS notify_connection_id INTEGER",
    # Ruolo osservatore: legge i pannelli admin, non scrive (vedi models/user.py)
    "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_observer BOOLEAN NOT NULL DEFAULT FALSE",
    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS is_observer BOOLEAN NOT NULL DEFAULT FALSE",
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


async def get_session():
    """La sessione della richiesta.

    Generatore ASINCRONO di proposito: così la chiusura finale avviene sul ciclo
    degli eventi e non in un thread. Con un generatore sincrono FastAPI la
    eseguirebbe in un thread preso dallo stesso insieme limitato delle rotte:
    una richiesta che ha finito e deve solo rendere la connessione resterebbe in
    coda per un thread, dietro a richieste ferme ad aspettare proprio quella
    connessione (vedi `deps/auth._fine_passo`). Chiudere non aspetta mai il
    pool: rende, non prende.
    """
    session = Session(engine)
    try:
        yield session
    finally:
        session.close()


# ── La rotta sincrona rilascia prima di uscire dal suo thread ────────────────
def _oggetti_orm(valore):
    """Gli oggetti ORM dentro ciò che una rotta restituisce (anche in liste e
    dizionari: `{"items": [...]}`)."""
    if hasattr(valore, "_sa_instance_state"):
        yield valore
    elif isinstance(valore, (list, tuple, set, frozenset)):
        for v in valore:
            yield from _oggetti_orm(v)
    elif isinstance(valore, dict):
        for v in valore.values():
            yield from _oggetti_orm(v)


def a_fine_rotta(fn):
    """Avvolge una rotta SINCRONA: quando la rotta ha finito, rende la
    connessione al pool prima di uscire dal thread.

    Dopo la rotta FastAPI valida la risposta in un altro thread. Una rotta che
    esce con la transazione aperta lascia la richiesta con una connessione in
    mano mentre aspetta quel thread — e sotto una raffica i thread sono tutti
    occupati da richieste che aspettano una connessione (vedi
    `deps/auth._fine_passo`: è lo stesso stallo, un passo più avanti).

    Prima di chiudere, ciò che la rotta restituisce viene reso leggibile senza
    database: un oggetto ORM «scaduto» da un commit (basta quello dell'audit)
    verrebbe ricaricato alla prima lettura, cioè nel thread sbagliato e senza
    più una sessione. Lo si ricarica qui: sono le stesse query che avrebbe fatto
    la validazione, solo nel thread giusto.
    """
    import functools

    from sqlalchemy import inspect as sa_inspect

    @functools.wraps(fn)
    def avvolta(*args, **kwargs):
        sessione = next((x for x in kwargs.values() if isinstance(x, Session)), None)
        try:
            risultato = fn(*args, **kwargs)
            if sessione is not None:
                for obj in _oggetti_orm(risultato):
                    stato = sa_inspect(obj)
                    if stato.persistent and stato.session is sessione and stato.unloaded:
                        sessione.refresh(obj)
            return risultato
        finally:
            if sessione is not None:
                sessione.close()

    avvolta.rilascia_a_fine_rotta = True
    return avvolta
