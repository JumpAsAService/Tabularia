import logging
import time
from contextlib import contextmanager

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
            raise InvalidatePoolError("connection died while idle in the pool") from e


# A database that does not answer at all (a failover leaving its address silent) held
# every thread on a connection that never came, or on a query whose server was gone.
_POSTGRES = {"connect_timeout": 10, "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10,
             "keepalives_count": 3}


def _crea_engine(**opzioni):
    dsn = get_settings().db.dsn
    if dsn.startswith("postgres"):
        opzioni.setdefault("connect_args", dict(_POSTGRES))
    eng = create_engine(dsn, echo=False, **opzioni)
    ping_solo_se_ferma(eng)
    return eng


def _misura_del_pool() -> int:
    s = get_settings()
    return s.db.pool_size or s.app.gateway_threads


# The panel's settings before the pool is made: its size is among them (Administration →
# Settings, `db.pool_size`, `db.max_overflow`, applied when a process starts). A connection
# of its own, closed at once; a database without the table yet has none.
from app.core import overrides as _overrides  # noqa: E402

_overrides.load_at_start(get_settings().db.dsn)

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

# Il battito delle orchestrazioni ha una connessione SUA. Se usasse il pool
# delle richieste, il giorno in cui quello si esaurisce (engine lento, molte
# esecuzioni insieme) il battito resterebbe in fila con tutti gli altri, e dopo
# un minuto le esecuzioni vive verrebbero date per morte e chiuse.
engine_battito = _crea_engine(pool_size=1, max_overflow=0)


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
    raise RuntimeError(f"DB unreachable after {max_attempts} attempts") from last_err


# Più processi possono partire insieme (repliche): creare le tabelle, applicare
# gli ALTER e seminare l'admin in parallelo vuol dire fare a gara sulle stesse
# istruzioni. Un lucchetto di Postgres li mette in fila; sono tutte idempotenti,
# quindi chi arriva secondo trova il lavoro fatto. Se il processo muore con il
# lucchetto in mano, Postgres lo libera chiudendo la connessione.
_LUCCHETTO_AVVIO = 7461627531


@contextmanager
def lucchetto_di_avvio(eng=None):
    eng = eng or engine
    if eng.dialect.name != "postgresql":
        yield
        return
    with eng.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _LUCCHETTO_AVVIO})
        conn.commit()
        try:
            yield
        finally:
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LUCCHETTO_AVVIO})
                conn.commit()
            except Exception:
                # non si è riusciti a renderlo: la connessione non torna nel pool
                # (chiudendola, Postgres lo libera)
                conn.invalidate()
                raise


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
    from app.services.permissions import REQUEST_SESSION

    session = Session(engine)
    session.info[REQUEST_SESSION] = True  # its permission answers are reused (services/permissions.py)
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
