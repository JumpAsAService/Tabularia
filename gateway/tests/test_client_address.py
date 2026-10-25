"""Whose address a request comes from, and the sign-in throttle that relies on it.

The gateway used to take the first hop of X-Forwarded-For as the client, whatever
had sent it: a client writing that header itself chose the address recorded in the
audit log, in its sessions and in its tokens. The server (uvicorn) already resolves
the header, reading it only from the proxies it trusts (FORWARDED_ALLOW_IPS,
`--forwarded-allow-ips`), and puts the result in the connection's client address:
the application reads that and nothing else.
"""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response
from starlette.requests import Request
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from app.routes import auth as auth_routes
from app.schemas.models import LoginRequest
from app.services import audit, login_throttle, sessions
from sqlmodel import select

from app.core.security import hash_password
from app.models import AuditLog
from tests.conftest import make_user


def _request(client: str, forwarded: str | None = None) -> Request:
    headers = [(b"user-agent", b"pytest")]
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    return Request({"type": "http", "method": "POST", "path": "/auth/login", "headers": headers,
                    "client": (client, 50000), "query_string": b""})


def test_a_forged_forwarded_for_is_not_the_client_address():
    assert audit.client_ip(_request("172.18.0.1", "6.6.6.6")) == "172.18.0.1"


@pytest.mark.anyio
async def test_behind_a_trusted_proxy_the_server_has_already_resolved_it():
    """The trust lives in the server's proxy middleware: from a trusted proxy the
    client is the first untrusted hop from the right; from anyone else the header
    is ignored."""
    seen = []

    async def app(scope, receive, send):
        seen.append(audit.client_ip(Request(scope)))

    wrapped = ProxyHeadersMiddleware(app, trusted_hosts="10.0.0.0/8")

    def scope(client, forwarded):
        return {"type": "http", "method": "GET", "path": "/", "scheme": "http", "query_string": b"",
                "headers": [(b"x-forwarded-for", forwarded.encode())], "client": (client, 1)}

    # the ingress controller (trusted) appends the real peer to what the client wrote
    await wrapped(scope("10.1.2.3", "6.6.6.6, 203.0.113.7"), None, None)
    # a client reaching the gateway directly writes the header itself
    await wrapped(scope("198.51.100.9", "6.6.6.6"), None, None)
    assert seen == ["203.0.113.7", "198.51.100.9"]


def test_a_session_keeps_the_connection_address_not_the_header(session):
    user = make_user(session, email="u@x.it")
    row, _ = sessions.open_session(session, user, _request("172.18.0.1", "6.6.6.6"), "password")
    assert row.ip == "172.18.0.1"


def test_the_audit_keeps_the_connection_address_not_the_header(session):
    user = make_user(session, email="u@x.it")
    audit.record_audit(session, actor=user, action=audit.LOGIN, request=_request("172.18.0.1", "6.6.6.6"))
    (item,) = session.exec(select(AuditLog)).all()
    assert item.ip == "172.18.0.1"


def _login(session, email, client="172.18.0.1", forwarded=None, password="wrong"):
    return auth_routes.login(LoginRequest(email=email, password=password),
                             request=_request(client, forwarded), response=Response(), session=session)


def test_a_rotating_forwarded_for_does_not_reset_the_throttle(session):
    make_user(session, email="admin@x.it", hashed_password=hash_password("the-real-one"))
    for i in range(login_throttle.THRESHOLD):
        with pytest.raises(HTTPException) as e:
            _login(session, "admin@x.it", forwarded=f"10.9.9.{i}")
        assert e.value.status_code == 401
    with pytest.raises(HTTPException) as e:
        _login(session, "admin@x.it", forwarded="10.9.9.200")
    assert e.value.status_code == 429


# ── many accounts from one address (password spraying, enumeration) ─────────────
def test_one_address_trying_many_accounts_is_slowed(session):
    for i in range(login_throttle.ADDRESS_THRESHOLD):
        assert login_throttle.request_wait(session, "1.2.3.4", f"u{i}@x.it") == 0
        login_throttle.record_failure(session, "1.2.3.4", f"u{i}@x.it")
    # a new email, never tried: the address as a whole has had enough
    assert login_throttle.request_wait(session, "1.2.3.4", "nuovo@x.it") > 0
    # another address (a colleague elsewhere) is not touched
    assert login_throttle.request_wait(session, "9.9.9.9", "nuovo@x.it") == 0


def test_a_few_mistakes_from_a_shared_office_address_do_not_lock_it(session):
    for i in range(login_throttle.ADDRESS_THRESHOLD - 1):
        login_throttle.record_failure(session, "1.2.3.4", f"u{i}@x.it")
    assert login_throttle.request_wait(session, "1.2.3.4", "nuovo@x.it") == 0


def test_signing_in_to_one_account_does_not_clear_the_address_count(session):
    """Otherwise one valid account would be enough to spray the others forever."""
    for i in range(login_throttle.ADDRESS_THRESHOLD):
        login_throttle.record_failure(session, "1.2.3.4", f"u{i}@x.it")
    login_throttle.record_success(session, "1.2.3.4", "mio@x.it")
    assert login_throttle.request_wait(session, "1.2.3.4", "altro@x.it") > 0


def test_the_cleanup_removes_the_address_counts_too(session):
    from datetime import timedelta

    for i in range(3):
        login_throttle.record_failure(session, "1.2.3.4", f"u{i}@x.it")
    later = login_throttle._now() + timedelta(seconds=login_throttle.WINDOW_SECONDS + 1)
    from unittest.mock import patch

    with patch.object(login_throttle, "_now", return_value=later):
        assert login_throttle.clean(session) == 4  # three (address, email) + the address


# ── the answer takes as long whether the account exists or not ─────────────────
@pytest.mark.parametrize("kind", ["missing", "sso_only", "service"])
def test_every_refused_sign_in_spends_the_password_check(session, monkeypatch, kind):
    """Unknown emails answered after one SELECT (~35 ms), real accounts after
    bcrypt (~300 ms): the time told which emails have a password account."""
    if kind == "sso_only":
        make_user(session, email="who@x.it", hashed_password=None)
    elif kind == "service":
        make_user(session, email="who@x.it", is_service=True)
    calls = []
    real = auth_routes.verify_password
    monkeypatch.setattr(auth_routes, "verify_password", lambda p, h: calls.append(h) or real(p, h))
    with pytest.raises(HTTPException) as e:
        _login(session, "who@x.it")
    assert e.value.status_code == 401
    assert len(calls) == 1 and calls[0].startswith("$2")  # a real bcrypt hash, so the real cost
