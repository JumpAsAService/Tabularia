"""HTTP API connections: the gateway's part.

The connection reuses the columns of the others: `host` = the base URL,
`username` = the basic user or the OAuth2 client id, the encrypted secret = the
API key / token / password / client secret, `extra` = {auth, key_name, key_in,
token_url, scope, client_auth, headers, test_path}. Fixed here: what is refused
at save time, that the secret is stored and travels encrypted and never comes
back, that it does not follow the connection to another host (nor the OAuth2
secret to another token URL), and the routes of the test and of «Try the call».
"""
import json

import pytest
from fastapi import HTTPException

from app.core.crypto import decrypt_secret
from app.models import Connection
from app.models.permission import Capability
from app.routes import connections as rc
from app.schemas.models import ApiCallSpec, ConnectionCreate, ConnectionTest, ConnectionUpdate
from tests.conftest import make_permission, make_project, make_user

pytestmark = pytest.mark.anyio

SECRET = "gw-S3CRET-key-42"


class _Req:
    client = None
    headers = {}


def _extra(**kw) -> str:
    base = {"auth": "api_key", "key_name": "X-Api-Key", "key_in": "header"}
    base.update(kw)
    return json.dumps(base)


def _body(**kw) -> ConnectionCreate:
    base = dict(name="broker", db_type="api", host="https://api.example.com/v1", port=None, username="",
                password=SECRET, extra=_extra())
    base.update(kw)
    return ConnectionCreate(**base)


def _create(session, user, project_id, **kw):
    return rc.create_connection(project_id, _body(**kw), request=None, user=user, session=session)


def _setup(session):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    return admin, p


# ── create and payload ─────────────────────────────────────────────────────────
def test_an_api_key_connection_and_its_payload(session):
    admin, p = _setup(session)
    out = _create(session, admin, p.id, extra=_extra(headers={"Accept": "application/json"}, test_path="/ping"))
    assert out.db_type == "api" and out.has_password and SECRET not in out.model_dump_json()
    pay = rc.engine_connection_payload(session.get(Connection, out.id))
    assert pay["db_type"] == "api" and pay["base_url"] == "https://api.example.com/v1"
    assert (pay["auth"], pay["key_name"], pay["key_in"]) == ("api_key", "X-Api-Key", "header")
    assert pay["headers"] == {"Accept": "application/json"} and pay["test_path"] == "/ping"
    assert decrypt_secret(pay["secret_encrypted"]) == SECRET and SECRET not in json.dumps(pay)


def test_an_oauth2_connection(session):
    admin, p = _setup(session)
    out = _create(session, admin, p.id, username="client-1",
                  extra=_extra(auth="oauth2", token_url="https://login.example.com/token", scope="read", client_auth="basic"))
    pay = rc.engine_connection_payload(session.get(Connection, out.id))
    assert (pay["auth"], pay["username"], pay["token_url"], pay["scope"], pay["client_auth"]) == \
        ("oauth2", "client-1", "https://login.example.com/token", "read", "basic")


def test_no_auth_needs_no_secret_and_sends_none(session):
    admin, p = _setup(session)
    out = _create(session, admin, p.id, password="", extra=json.dumps({"auth": "none"}))
    pay = rc.engine_connection_payload(session.get(Connection, out.id))
    assert pay["auth"] == "none" and pay["secret_encrypted"] == ""


@pytest.mark.parametrize("kw,message", [
    (dict(host="api.example.com"), "base URL"),
    (dict(host="ftp://api.example.com"), "base URL"),
    (dict(host="https://user:pw@api.example.com"), "user"),
    (dict(host="https://api.example.com/v1?apikey=1"), "query"),
    (dict(extra=_extra(auth="magic")), "auth"),
    (dict(extra=_extra(key_name="")), "name"),
    (dict(extra=_extra(key_name="Bad Header")), "name"),
    (dict(extra=_extra(key_in="cookie")), "header or query"),
    (dict(extra=_extra(auth="oauth2", token_url="")), "token URL"),
    (dict(extra=_extra(auth="oauth2", token_url="https://login.example.com/t")), "client id"),
    (dict(extra=_extra(auth="basic")), "user"),
    (dict(extra=_extra(headers={"X-A": "a\r\nInjected: 1"})), "header"),
    (dict(extra=_extra(headers=["not", "a", "dict"])), "headers"),
    (dict(extra=_extra(test_path="https://elsewhere.example.com/")), "test path"),
    (dict(password=""), "secret"),
])
def test_what_is_refused_at_save_time(session, kw, message):
    admin, p = _setup(session)
    with pytest.raises(HTTPException) as e:
        _create(session, admin, p.id, **kw)
    assert e.value.status_code == 422 and message in e.value.detail


def test_any_address_is_allowed_with_connect(session):
    """As for databases and SFTP: the CONNECT permission governs it (user decision)."""
    admin, p = _setup(session)
    for i, host in enumerate(("http://10.0.0.5:8080", "http://localhost:9000/api", "https://192.168.1.10")):
        assert _create(session, admin, p.id, name=f"c{i}", host=host).host == host


# ── edits: the secret does not follow the connection elsewhere ─────────────────
def test_a_new_host_needs_the_secret_again(session):
    admin, p = _setup(session)
    out = _create(session, admin, p.id)
    with pytest.raises(HTTPException) as e:
        rc.update_connection(out.id, ConnectionUpdate(host="https://evil.example.com/v1"), request=None, user=admin, session=session)
    assert e.value.status_code == 422 and "secret" in e.value.detail
    assert session.get(Connection, out.id).host == "https://api.example.com/v1"
    rc.update_connection(out.id, ConnectionUpdate(host="https://evil.example.com/v1", password="other-secret"),
                         request=None, user=admin, session=session)
    assert decrypt_secret(session.get(Connection, out.id).password_encrypted) == "other-secret"


def test_another_path_on_the_same_host_keeps_the_secret(session):
    admin, p = _setup(session)
    out = _create(session, admin, p.id)
    rc.update_connection(out.id, ConnectionUpdate(host="https://api.example.com/v2"), request=None, user=admin, session=session)
    assert decrypt_secret(session.get(Connection, out.id).password_encrypted) == SECRET


def test_a_new_token_url_needs_the_client_secret_again(session):
    admin, p = _setup(session)
    out = _create(session, admin, p.id, username="c", extra=_extra(auth="oauth2", token_url="https://login.example.com/token"))
    with pytest.raises(HTTPException) as e:
        rc.update_connection(out.id, ConnectionUpdate(extra=_extra(auth="oauth2", token_url="https://evil.example.com/token")),
                             request=None, user=admin, session=session)
    assert e.value.status_code == 422 and "secret" in e.value.detail


# ── tests and «Try the call» ───────────────────────────────────────────────────
async def test_the_draft_test_goes_to_the_engine_with_the_typed_secret(session, fake_engine):
    admin, p = _setup(session)
    out = await rc.test_draft_connection(p.id, ConnectionTest(**_body().model_dump()), request=_Req(), user=admin, session=session)
    assert out["ok"]
    (sent,) = fake_engine.api_inspects
    assert sent["action"] == "test" and sent["connection"]["base_url"] == "https://api.example.com/v1"
    assert decrypt_secret(sent["connection"]["secret_encrypted"]) == SECRET and SECRET not in json.dumps(sent)


async def test_a_saved_connection_test(session, fake_engine):
    admin, p = _setup(session)
    out = _create(session, admin, p.id)
    await rc.test_connection(out.id, None, request=_Req(), user=admin, session=session)
    assert fake_engine.api_inspects[0]["action"] == "test"


async def test_an_engine_refusal_reaches_the_user(session, fake_engine):
    admin, p = _setup(session)
    out = _create(session, admin, p.id)
    fake_engine.api_responses["test"] = (400, {"detail": "API: GET https://api.example.com/v1 answered HTTP 401"})
    with pytest.raises(HTTPException) as e:
        await rc.test_connection(out.id, None, request=_Req(), user=admin, session=session)
    assert e.value.status_code == 400 and "401" in e.value.detail


async def test_try_the_call(session, fake_engine):
    admin, p = _setup(session)
    out = _create(session, admin, p.id)
    call = ApiCallSpec(path="/quote/{symbol}", variables=[{"name": "symbol", "values": ["AAPL"]}])
    res = await rc.try_api_call(out.id, call, request=_Req(), user=admin, session=session)
    assert res["ok"]
    (sent,) = fake_engine.api_inspects
    assert sent["action"] == "try" and sent["source"]["path"] == "/quote/{symbol}"


async def test_try_needs_connect_and_an_api_connection(session, fake_engine):
    admin, p = _setup(session)
    out = _create(session, admin, p.id)
    u = make_user(session, email="u@x.it")
    make_permission(session, project_id=p.id, user_id=u.id, capability=Capability.EDIT)
    with pytest.raises(HTTPException) as e:
        await rc.try_api_call(out.id, ApiCallSpec(path="/q"), request=_Req(), user=u, session=session)
    assert e.value.status_code == 403
    pg = Connection(name="pg", project_id=p.id, db_type="postgresql", host="db", database="x")
    session.add(pg)
    session.commit()
    with pytest.raises(HTTPException) as e:
        await rc.try_api_call(pg.id, ApiCallSpec(path="/q"), request=_Req(), user=admin, session=session)
    assert e.value.status_code == 422


def test_an_api_connection_is_not_a_database():
    assert "api" in rc.NOT_DATABASES and "api" in rc.SUPPORTED_DB_TYPES


def test_moving_to_another_host_without_authentication_drops_the_stored_secret(session):
    admin, p = _setup(session)
    out = _create(session, admin, p.id)
    rc.update_connection(out.id, ConnectionUpdate(host="https://open.example.com", extra=json.dumps({"auth": "none"})),
                         request=None, user=admin, session=session)
    conn = session.get(Connection, out.id)
    assert conn.host == "https://open.example.com" and conn.password_encrypted == ""
