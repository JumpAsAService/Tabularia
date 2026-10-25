import logging
import time
from contextlib import contextmanager

from sqlalchemy import event, text
from sqlalchemy.exc import InvalidatePoolError, OperationalError
from sqlmodel import create_engine, select, Session, SQLModel

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# A connection used a moment ago is alive. Asking it at EVERY use (`pool_pre_ping`)
# costs a round trip to Postgres for every step of every request: under load it was 14%
# of the gateway's time. It is asked only of those left still in the pool, which are
# the only ones a Postgres restart or a firewall may have closed without anyone
# noticing.
PING_IF_IDLE_FOR_S = 5.0


def ping_only_if_idle(eng, idle_for_s: float = PING_IF_IDLE_FOR_S) -> None:
    """Tests the connection on leaving the pool only when it has been still for a while.

    When it does not answer it throws away all those opened before (as `pool_pre_ping`
    does) and the pool opens a new one: whoever asked sees nothing. What stays uncovered
    is Postgres falling in the seconds right after a use: the first request fails, and its
    error invalidates the pool for the next ones."""

    @event.listens_for(eng, "connect")
    def _new(dbapi_conn, record):
        record.info["resa"] = time.monotonic()

    @event.listens_for(eng, "checkin")
    def _returned(dbapi_conn, record):
        record.info["resa"] = time.monotonic()

    @event.listens_for(eng, "checkout")
    def _taken(dbapi_conn, record, proxy):
        if time.monotonic() - record.info.get("resa", 0.0) < idle_for_s:
            return
        try:
            eng.dialect.do_ping(dbapi_conn)
        except Exception as e:
            raise InvalidatePoolError("connection died while idle in the pool") from e


# A database that does not answer at all (a failover leaving its address silent) held
# every thread on a connection that never came, or on a query whose server was gone.
_POSTGRES = {"connect_timeout": 10, "keepalives": 1, "keepalives_idle": 30, "keepalives_interval": 10,
             "keepalives_count": 3}


def _create_engine(**options):
    dsn = get_settings().db.dsn
    if dsn.startswith("postgres"):
        options.setdefault("connect_args", dict(_POSTGRES))
    eng = create_engine(dsn, echo=False, **options)
    ping_only_if_idle(eng)
    return eng


def _pool_size() -> int:
    s = get_settings()
    return s.db.pool_size or s.app.gateway_threads


# The panel's settings before the pool is made: its size is among them (Administration →
# Settings, `db.pool_size`, `db.max_overflow`, applied when a process starts). A connection
# of its own, closed at once; a database without the table yet has none.
from app.core import overrides as _overrides  # noqa: E402

_overrides.load_at_start(get_settings().db.dsn)

# Every thread holds at most one connection at a time (see `at_end_of_route`), so the pool
# is sized on the threads. With the library's 5 the others were «extra»: opened and
# CLOSED at every request — 16,000 sessions on Postgres in ten minutes of load, and
# every opening is a process Postgres spawns.
engine = _create_engine(pool_size=_pool_size(), max_overflow=get_settings().db.max_overflow)

# A few connections kept ALWAYS in autocommit, for the «one and done» reads (the user
# every request loads): there a SELECT is one round trip to Postgres, with no BEGIN nor
# ROLLBACK around it. They are separate connections, and not the main pool's put in
# autocommit for the occasion, because the driver, putting them back, sends Postgres one
# more command at every request (`SET default_transaction_isolation`). Whoever takes one
# keeps it for a SELECT and waits for nothing else: when they are all busy one queues
# for a few milliseconds, without the others' risk of stalling.
_reads_engine = _create_engine(
    isolation_level="AUTOCOMMIT", pool_size=max(2, _pool_size() // 4), max_overflow=0
)
_READS = {engine: _reads_engine}

# The orchestrations' heartbeat has a connection of ITS OWN. If it used the requests'
# pool, the day that runs out (slow engine, many runs together) the heartbeat would
# stay in line with all the others, and after a minute the live runs would be taken for
# dead and closed.
heartbeat_engine = _create_engine(pool_size=1, max_overflow=0)


def read_engine(bind):
    """The autocommit engine that reads the same database as `bind`."""
    return _READS.get(bind) or bind.execution_options(isolation_level="AUTOCOMMIT")


def wait_for_db(max_attempts: int = 30, delay: float = 2.0) -> None:
    """Waits for the DB to be reachable before initializing the schema.

    After a host reboot (or a Postgres restart) the gateway may start before the
    `postgres` host resolves on Docker's DNS: without waiting the app would exit at start
    and — with `uvicorn --reload` — the container would stay "running" but serve nothing
    (the reload supervisor does not exit, so `restart: unless-stopped` does not fire).
    With the retry, instead, it starts again by itself. `depends_on: service_healthy` is
    NOT enough: it holds only on `compose up`, not when the Docker daemon turns the
    containers back on after a reboot."""
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            if attempt > 1:
                logger.info("DB reachable after %d attempts", attempt)
            return
        except OperationalError as e:  # DNS not resolved or Postgres not ready yet
            last_err = e
            logger.warning("DB not ready (attempt %d/%d), retrying in %.0fs…",
                           attempt, max_attempts, delay)
            time.sleep(delay)
    raise RuntimeError(f"DB unreachable after {max_attempts} attempts") from last_err


# Several processes may start together (replicas): creating the tables, applying the
# ALTERs and seeding the admin in parallel means racing on the same statements. A
# Postgres lock lines them up; they are all idempotent, so whoever comes second finds
# the work done. When the process dies with the lock in hand, Postgres frees it closing
# the connection.
_STARTUP_LOCK = 7461627531


@contextmanager
def startup_lock(eng=None):
    eng = eng or engine
    if eng.dialect.name != "postgresql":
        yield
        return
    with eng.connect() as conn:
        conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _STARTUP_LOCK})
        conn.commit()
        try:
            yield
        finally:
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _STARTUP_LOCK})
                conn.commit()
            except Exception:
                # it could not be handed back: the connection does not go back into the pool (closing
                # it, Postgres frees it)
                conn.invalidate()
                raise


def backfill_flow_versions() -> None:
    """Every flow without history gets a v1 with the current definition (for the flows
    created before versioning). Idempotent: in steady state it loads no rows."""
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
    """The request's session.

    An ASYNCHRONOUS generator on purpose: so the final closing happens on the event loop
    and not in a thread. With a synchronous generator FastAPI would run it in a thread
    taken from the same limited set as the routes: a request that has finished and only
    has to hand the connection back would stay queued for a thread, behind requests stuck
    waiting for exactly that connection (see `deps/auth._fine_passo`). Closing never waits
    for the pool: it gives, it does not take.
    """
    from app.services.permissions import REQUEST_SESSION

    session = Session(engine)
    session.info[REQUEST_SESSION] = True  # its permission answers are reused (services/permissions.py)
    try:
        yield session
    finally:
        session.close()


# ── The synchronous route releases before leaving its thread ─────────────────
def _orm_objects(value):
    """The ORM objects inside what a route returns (in lists and dicts too:
    `{"items": [...]}`)."""
    if hasattr(value, "_sa_instance_state"):
        yield value
    elif isinstance(value, (list, tuple, set, frozenset)):
        for v in value:
            yield from _orm_objects(v)
    elif isinstance(value, dict):
        for v in value.values():
            yield from _orm_objects(v)


def at_end_of_route(fn):
    """Wraps a SYNCHRONOUS route: when the route has finished, it hands the connection back
    to the pool before leaving the thread.

    After the route FastAPI validates the answer in another thread. A route leaving with
    the transaction open leaves the request with a connection in hand while it waits for
    that thread — and under a burst the threads are all busy with requests waiting for a
    connection (see `deps/auth._fine_passo`: it is the same stall, one step further).

    Before closing, what the route returns is made readable without a database: an ORM
    object «expired» by a commit (the audit's is enough) would be reloaded at the first
    read, that is in the wrong thread and with no session any more. It is reloaded here:
    they are the same queries the validation would have made, only in the right thread.
    """
    import functools

    from sqlalchemy import inspect as sa_inspect

    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        user_session = next((x for x in kwargs.values() if isinstance(x, Session)), None)
        try:
            result = fn(*args, **kwargs)
            if user_session is not None:
                for obj in _orm_objects(result):
                    state = sa_inspect(obj)
                    if state.persistent and state.session is user_session and state.unloaded:
                        user_session.refresh(obj)
            return result
        finally:
            if user_session is not None:
                user_session.close()

    wrapped.release_at_end_of_route = True
    return wrapped
