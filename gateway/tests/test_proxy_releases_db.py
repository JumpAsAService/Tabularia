"""While the gateway waits for the engine, it must not hold a database connection.

Every route forwarded to the engine goes through authentication and the permission
checks, which read from the database and open a transaction. If the transaction stays
open during the wait — up to two minutes for a preview, the whole length for an
upload or an export — that request holds a pool connection without using it. The pool
has fifteen: fifteen slow previews together left every other request without a
database, sign-in included.

Measured on Postgres with twenty 6-second previews: 15 connections held, any request
stuck for 4.8 seconds, the previews finished in 12 seconds instead of 6.

Here the cause is looked at, not the symptom: at the moment the call to the engine
starts, the session must have no open transaction.
"""
import json

import httpx
import pytest
from starlette.requests import Request

from app.models import User
from app.routes import proxy
from tests.conftest import make_user

pytestmark = pytest.mark.anyio


def _request(body: dict | None = None, method: str = "POST", path: str = "/x") -> Request:
    raw = json.dumps(body).encode() if body is not None else b""
    scope = {
        "type": "http", "method": method, "path": path, "query_string": b"",
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())],
        "client": ("203.0.113.9", 4242),
    }

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(scope, receive)


@pytest.fixture
def watching_engine(session, monkeypatch):
    """A fake engine that, when called, notes whether the gateway's session has an open
    transaction at that moment."""
    seen: list[tuple[str, bool]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, session.in_transaction()))
        if request.url.path == "/engines":
            return httpx.Response(200, json=[{"id": "polars", "available": True}])
        return httpx.Response(200, json={"dataset_id": "d1", "parquet_key": "datasets/d1.parquet", "raw_key": "raw/d1"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://engine")
    monkeypatch.setattr("app.core.engine_client._client", client)
    return seen


@pytest.fixture
def admin(session) -> User:
    return make_user(session, email="capo@x.local", is_superuser=True)


def _bucket() -> str:
    from app.core.config import get_settings

    return get_settings().engine.bucket


def _body(**extra) -> dict:
    return {"bucket": _bucket(), "input_key": "datasets/prova.parquet", "operations": [], "limit": 10, **extra}


def _as_after_authentication(session, user) -> None:
    """What `get_current_user` leaves: the user read, the transaction open."""
    session.get(User, user.id)
    assert session.in_transaction()


async def test_the_preview_does_not_hold_the_connection_while_waiting(session, admin, watching_engine):
    await proxy.preview(_request(_body()), user=admin, session=session)
    assert watching_engine == [("/tasks/preview", False)]


async def test_the_export_does_not_hold_the_connection_while_waiting(session, admin, watching_engine):
    await proxy.export(_request(_body(format="csv", filename="x")), user=admin, session=session)
    assert watching_engine == [("/tasks/export", False)]


async def test_the_run_without_flow_does_not_hold_the_connection_while_waiting(session, admin, watching_engine):
    await proxy.transform(_request(_body()), user=admin, session=session)
    assert watching_engine == [("/tasks/transform-data", False)]


async def test_the_upload_does_not_hold_the_connection_and_records_the_owner_anyway(session, admin, watching_engine):
    from sqlmodel import select

    from app.models import Upload

    await proxy.upload(_request({}, path="/files"), user=admin, session=session)
    assert watching_engine == [("/files", False)]
    # after the engine's answer the session is still needed: it must work
    uploaded_ones = session.exec(select(Upload)).all()
    assert [(u.dataset_id, u.owner_id) for u in uploaded_ones] == [("d1", admin.id)]


@pytest.mark.parametrize("route, path", [("operations", "/tasks/operations"), ("engines", "/engines")])
async def test_the_short_routes_release_too(session, admin, watching_engine, route, path):
    _as_after_authentication(session, admin)
    await getattr(proxy, route)(_request(method="GET"), session=session)
    assert watching_engine == [(path, False)]


async def test_a_tasks_state_does_not_hold_the_connection(session, admin, watching_engine):
    _as_after_authentication(session, admin)
    await proxy.task_status(_request(method="GET"), task_id="t1", session=session)
    assert watching_engine == [("/tasks/t1", False)]


async def test_the_user_stays_usable_after_the_release(session, admin, watching_engine):
    """Releasing must not leave unusable objects to whoever comes after: the routes still use
    the user (owner of the upload, audit)."""
    await proxy.preview(_request(_body()), user=admin, session=session)
    assert admin.email == "capo@x.local" and admin.id is not None


async def test_even_when_an_earlier_commit_had_expired_the_user(session, admin, watching_engine):
    """It is the case of whoever comes first after a minute: authentication updates
    `last_seen` and commits, and the user's attributes expire. After the release the user
    is detached from the session: had it not been reloaded before, reading its id would
    raise an error."""
    session.commit()  # what `_touch_last_seen` does
    await proxy.upload(_request({}, path="/files"), user=admin, session=session)
    assert watching_engine == [("/files", False)]
    assert admin.id is not None and admin.email == "capo@x.local"


@pytest.mark.parametrize("route", ["preview", "export", "transform"])
async def test_the_database_checks_do_not_run_on_the_event_loop(session, admin, watching_engine, monkeypatch, route):
    """The queries are synchronous: on the event loop they would block it, and with the pool
    exhausted they would stop it waiting for a connection that can free up only if IT goes
    on. Seen live: gateway frozen for 30 seconds. So the checks must run in a thread."""
    import threading

    loop = threading.get_ident()
    where: list[int] = []
    real = proxy.ensure_reads_pinned

    def spy(*a, **k):
        where.append(threading.get_ident())
        return real(*a, **k)

    monkeypatch.setattr(proxy, "ensure_reads_pinned", spy)
    body = _body(format="csv", filename="x") if route == "export" else _body()
    await getattr(proxy, route)(_request(body), user=admin, session=session)
    assert where and all(t != loop for t in where)


def test_the_proxys_authentication_does_not_leave_the_connection_in_hand(session, admin):
    """Authentication runs in a thread and the checks in another: a request holding the
    connection between the two would wait for a thread while holding a resource other
    threads are waiting for. With 60 previews together: gateway stuck for 28 seconds. It
    must take and hand back in the same step, and return a user readable without a
    database."""
    from tests.conftest import token_of

    token = token_of(session, admin)
    user_ = proxy._user(_request(method="GET"), token=token, session=session)
    assert not session.in_transaction()
    assert (user_.id, user_.email, user_.is_superuser) == (admin.id, "capo@x.local", True)


def test_every_proxy_route_goes_through_that_authentication():
    """A route added tomorrow with `get_current_user` would reopen the stall without anything
    saying so: the authentication belongs to the router, whole."""
    from app.deps.auth import get_current_user

    for route in proxy.router.routes:
        calls = {d.call for d in route.dependant.dependencies}
        assert proxy._user in calls, route.path
        assert get_current_user not in calls, route.path
