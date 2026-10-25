"""A database connection is taken and handed back in the same step.

FastAPI runs every synchronous dependency and every synchronous route in a thread, one
per step, taken from a limited set (40). If a step leaves holding the connection, the
request keeps it while waiting for a thread for the next step — and under a burst
those threads are all busy with other requests waiting for a connection (the pool has
15). Neither group moves on until the pool's timeout fires.

Measured on Postgres, normal routes (`/flows`, `/projects`, `/datasources`,
`/auth/me`), all the requests together:

    20  → all 200 in 0.2 s
    60  → 32 errors 500, 30 s
    100 → 68 errors 500, 60 s
    200 → 160 errors 500, 120 s

Sixty requests together are a dozen people opening the same page. Two rules remove
it, and these tests pin them:

  - the authentication dependencies close their transaction before returning the
    user;
  - the request's session closes on the event loop, not in a thread: the final
    release must not wait for a free thread.
"""
import inspect
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.db import session as db_session
from app.deps import auth
from tests.conftest import make_user, token_of


def _request():
    return SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.9"))


def _spy(engine):
    """Counts the connections out of the pool and not handed back, and the commands sent to
    the database."""
    from sqlalchemy import event

    state = SimpleNamespace(in_hand=0, taken=0, commands=[])

    @event.listens_for(engine, "checkout")
    def _taken(*_):
        state.in_hand += 1
        state.taken += 1

    @event.listens_for(engine, "checkin")
    def _returned(*_):
        state.in_hand -= 1

    @event.listens_for(engine, "before_cursor_execute")
    def _command(conn, cursor, statement, *_):
        state.commands.append(statement)

    return state


def test_the_authentication_does_not_leave_with_a_connection_in_hand(db_engine, session):
    from datetime import datetime, timezone

    # seen a moment ago: `last_seen` need not be updated, so no commit releases the
    # connection by chance — it is the case of almost every request
    u = make_user(session, email="a@x.local", last_seen_at=datetime.now(timezone.utc))
    token = token_of(session, u)
    spy = _spy(db_engine)
    user_ = auth.get_current_user(_request(), token=token, session=session)
    assert spy.taken == 1 and spy.in_hand == 0
    # the user stays attached to the session: whoever reads it afterwards finds it again
    assert user_ in session
    assert user_.email == "a@x.local" and user_.id == u.id


def test_the_authentication_costs_one_read_and_the_route_does_not_read_the_user_again(db_engine):
    """Measured on the real stack before this rule: 4 commands to read the user (ping, BEGIN,
    SELECT, ROLLBACK) and a second identical SELECT in the route, because the end-of-step
    rollback had expired it."""
    from datetime import datetime, timezone

    from sqlalchemy import inspect as sa_inspect
    from sqlmodel import Session

    with Session(db_engine) as s:
        u = make_user(s, email="c@x.local", last_seen_at=datetime.now(timezone.utc))
        uid, token = u.id, token_of(s, u)
    spy = _spy(db_engine)
    with Session(db_engine) as request_:  # the new session every request receives
        user_ = auth.get_current_user(_request(), token=token, session=request_)
        # one read only with the session too: it is joined with the user
        assert len(spy.commands) == 1 and "FROM users" in spy.commands[0] and "user_sessions" in spy.commands[0]
        # what the route does first: reading its fields
        assert not sa_inspect(user_).expired
        assert (user_.id, user_.email, user_.is_active, user_.is_superuser) == (uid, "c@x.local", True, False)
        assert len(spy.commands) == 1 and spy.in_hand == 0


def test_a_disabled_or_vanished_user_is_refused_as_before(db_engine):
    from sqlmodel import Session

    from app.core.security import create_access_token

    with Session(db_engine) as s:
        off = make_user(s, email="spento@x.local")
        token_off = token_of(s, off)  # signed in while active…
        off.is_active = False               # …and disabled afterwards
        s.add(off)
        s.commit()
    for token in (token_off, create_access_token(99999, "nessuna-sessione")):
        with Session(db_engine) as request_, pytest.raises(HTTPException) as e:
            auth.get_current_user(_request(), token=token, session=request_)
        assert e.value.status_code == 401


def test_the_loaded_user_can_be_changed_and_saved_from_the_route(db_engine):
    """The route receives it attached to ITS session: changing it and committing writes, as
    when the session itself read it."""
    from datetime import datetime, timezone

    from sqlmodel import Session

    from app.models import User

    with Session(db_engine) as s:
        u = make_user(s, email="d@x.local", last_seen_at=datetime.now(timezone.utc))
        uid, token = u.id, token_of(s, u)
    with Session(db_engine) as request_:
        user_ = auth.get_current_user(_request(), token=token, session=request_)
        user_.full_name = "Nome Nuovo"
        request_.add(user_)
        request_.commit()
    with Session(db_engine) as s:
        assert s.get(User, uid).full_name == "Nome Nuovo"


def test_not_even_when_it_has_just_updated_last_seen(session):
    u = make_user(session, email="b@x.local")  # last_seen_at null: it updates it and commits
    auth.get_current_user(_request(), token=token_of(session, u), session=session)
    assert not session.in_transaction()
    session.refresh(u)
    assert u.last_seen_at is not None


@pytest.mark.parametrize("guard", ["require_superuser", "require_observer"])
def test_the_administrative_guards_do_not_leave_with_an_open_transaction(session, guard):
    admin = make_user(session, email="capo@x.local", is_superuser=True)
    assert getattr(auth, guard)(user=admin, session=session) is admin
    assert not session.in_transaction()


@pytest.mark.parametrize("guard", ["require_superuser", "require_observer"])
def test_the_guards_refuse_as_before(session, guard):
    normal = make_user(session, email="n@x.local")
    with pytest.raises(HTTPException) as e:
        getattr(auth, guard)(user=normal, session=session)
    assert e.value.status_code == 403


def test_an_invalid_token_is_refused_as_before(session):
    with pytest.raises(HTTPException) as e:
        auth.get_current_user(_request(), token="non-un-token", session=session)
    assert e.value.status_code == 401


@pytest.mark.anyio
async def test_the_requests_session_closes_on_the_event_loop(db_engine, monkeypatch):
    """An ASYNCHRONOUS generator: the entry and the exit do not go through a thread."""
    assert inspect.isasyncgenfunction(db_session.get_session)
    monkeypatch.setattr(db_session, "engine", db_engine)
    gen = db_session.get_session()
    user_session = await gen.__anext__()
    from sqlmodel import select

    from app.models import User

    user_session.exec(select(User)).all()
    assert user_session.in_transaction()
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    assert not user_session.in_transaction()


# ── The synchronous route releases before leaving its thread ─────────────────
# After the route FastAPI validates the answer in ANOTHER thread: if the route leaves
# with the transaction open, the request keeps the connection while waiting for that
# thread. Measured after fixing the authentication: 60 together fine, 100 together → 37
# errors and 30 seconds of block.
def test_at_end_of_route_the_connection_is_handed_back_and_the_result_stays_readable(session):
    from app.db.session import at_end_of_route
    from app.models import Project

    @at_end_of_route
    def route(session):
        p = Project(name="cartella")
        session.add(p)
        session.commit()  # the object is «expired»: reading it afterwards needs the database
        return p

    p = route(session=session)
    assert not session.in_transaction()
    # FastAPI will read it in the next thread: what is in memory must be enough
    assert p.name == "cartella" and p.id is not None
    assert not session.in_transaction()


def test_it_holds_for_lists_and_dicts_of_objects_too(session):
    from app.db.session import at_end_of_route
    from app.models import Project

    @at_end_of_route
    def route(session):
        a, b = Project(name="a"), Project(name="b")
        session.add(a); session.add(b)
        session.commit()
        return {"items": [a, b], "total": 2}

    r = route(session=session)
    assert [x.name for x in r["items"]] == ["a", "b"] and not session.in_transaction()


def test_an_error_in_the_route_releases_all_the_same(session):
    from sqlmodel import select

    from app.db.session import at_end_of_route
    from app.models import Project

    @at_end_of_route
    def route(session):
        session.exec(select(Project)).all()
        raise HTTPException(status_code=404, detail="non c'è")

    with pytest.raises(HTTPException):
        route(session=session)
    assert not session.in_transaction()


def test_every_gateway_router_releases_at_end_of_route():
    """A new router must not be able to forget it: every `APIRouter` declares the route class
    that wraps the synchronous routes. The asynchronous ones stay out (they do not go
    through a thread per route)."""
    import asyncio
    import importlib
    import pkgutil

    from fastapi import APIRouter
    from fastapi.routing import APIRoute

    import app.main  # noqa: F401 — loads every router
    import app.routes as package
    from app.core.routing import ReleasingRoute

    sincrone = asincrone = 0
    for info in pkgutil.iter_modules(package.__path__):
        modulo = importlib.import_module(f"app.routes.{info.name}")
        router = getattr(modulo, "router", None)
        if not isinstance(router, APIRouter):
            continue
        assert router.route_class is ReleasingRoute, info.name
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            wrapped = getattr(route.endpoint, "release_at_end_of_route", False)
            if asyncio.iscoroutinefunction(route.endpoint):
                asincrone += 1
                assert not wrapped, route.path
            else:
                sincrone += 1
                assert wrapped, route.path
    assert sincrone > 50 and asincrone > 10


# ── Fewer round trips to Postgres per request ────────────────────────────────
# The profile under load (250 users): almost half of the time the gateway worked was
# waiting on Postgres for avoidable things — the ping before every use of a connection,
# connections opened and closed at every request because the pool was 5, the same
# table read three times.
def test_the_pool_is_sized_on_the_threads():
    from app.core.config import get_settings

    s = get_settings()
    assert s.db.pool_size is None  # empty = one per thread
    assert db_session.engine.pool.size() == s.app.gateway_threads


def test_the_users_reads_have_their_own_connections_always_in_autocommit(db_engine):
    reads = db_session.read_engine(db_session.engine)
    assert reads is not db_session.engine and reads.pool is not db_session.engine.pool
    assert reads.url == db_session.engine.url
    assert reads.dialect._on_connect_isolation_level == "AUTOCOMMIT"
    # few, and never one more: whoever finds none waits for someone else's SELECT
    assert 2 <= reads.pool.size() <= db_session.engine.pool.size() // 2
    assert reads.pool._max_overflow == 0
    # any engine (the tests'): the same pool, autocommit for the occasion
    assert db_session.read_engine(db_engine).pool is db_engine.pool


def _engine_on_file(tmp_path, idle_for_s):
    from sqlalchemy import QueuePool
    from sqlmodel import create_engine

    eng = create_engine(f"sqlite:///{tmp_path}/ping.db", poolclass=QueuePool)
    db_session.ping_only_if_idle(eng, idle_for_s=idle_for_s)
    ping = []
    real = eng.dialect.do_ping
    eng.dialect.do_ping = lambda c: (ping.append(c), real(c))[1]
    return eng, ping


def test_a_connection_just_used_is_not_tested(tmp_path):
    eng, ping = _engine_on_file(tmp_path, idle_for_s=60)
    for _ in range(5):
        with eng.connect() as c:
            c.exec_driver_sql("select 1")
    assert ping == []


def test_a_connection_left_idle_is_tested(tmp_path):
    eng, ping = _engine_on_file(tmp_path, idle_for_s=0)
    for _ in range(3):
        with eng.connect() as c:
            c.exec_driver_sql("select 1")
    assert len(ping) == 3


def test_when_the_idle_connection_is_dead_whoever_asks_gets_a_new_one(tmp_path):
    from sqlalchemy import event

    eng, _ = _engine_on_file(tmp_path, idle_for_s=0)
    open_ones = []
    event.listen(eng, "connect", lambda dbapi, record: open_ones.append(dbapi))
    with eng.connect() as c:
        c.exec_driver_sql("select 1")
    assert len(open_ones) == 1
    dead_ones = {id(open_ones[0])}

    def do_ping(dbapi_conn):  # the first connection no longer answers
        if id(dbapi_conn) in dead_ones:
            raise OSError("server closed the connection unexpectedly")
        return True

    eng.dialect.do_ping = do_ping
    with eng.connect() as c:
        assert c.exec_driver_sql("select 41 + 1").scalar() == 42
    assert len(open_ones) == 2


def test_the_folders_listing_reads_the_tree_once_only(db_engine, session):
    from app.models import Permission, Project
    from app.services import permissions

    u = make_user(session, email="e@x.local")
    root = Project(name="radice", owner_id=u.id)
    other_one = Project(name="altra", owner_id=u.id)
    session.add(root); session.add(other_one); session.commit()
    child_folder = Project(name="figlia", parent_id=root.id, owner_id=u.id)
    session.add(child_folder); session.commit()
    session.add(Permission(project_id=child_folder.id, user_id=u.id, capability="view")); session.commit()

    waits = permissions.visible_project_ids(session, u)
    assert waits == {root.id, child_folder.id}  # the child, and the root to get to it
    session.commit()
    session.info[permissions.REQUEST_SESSION] = True  # as get_session marks every request's session
    spy = _spy(db_engine)
    views = permissions.visible_projects(session, u)
    assert {p.id for p in views} == waits
    # the tree once as (id, parent) pairs, then whole rows of the visible folders only
    reads = [c for c in spy.commands if "FROM projects" in c]
    assert len(reads) == 2 and "projects.name" not in reads[0] and "WHERE projects.id IN" in reads[1]
