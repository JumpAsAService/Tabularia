"""SFTP connections: the gateway's part.

What is fixed here: the secrets (password, or private key with its passphrase)
are stored encrypted and travel encrypted, and only the one of the chosen method
travels; the host key is accepted ONLY by a test the engine really made, with an
audit line; a change of host forgets the accepted key; the engine's host-key
refusals reach the user translated.
"""
import json

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.core.crypto import decrypt_secret, encrypt_secret
from app.models import AuditLog, Connection
from app.models.permission import Capability
from app.routes import connections as rc
from app.schemas.models import ConnectionCreate, ConnectionTest, ConnectionUpdate, SftpFilesQuery
from app.services import audit
from tests.conftest import make_permission, make_project, make_user

pytestmark = pytest.mark.anyio

KEY = "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXktdjEAAAAA\n-----END OPENSSH PRIVATE KEY-----\n"


class _Req:
    client = None

    def __init__(self, lang: str = "en"):
        self.headers = {"accept-language": lang}


def _body(**kw) -> ConnectionCreate:
    base = dict(name="drop", db_type="sftp", host="sftp.example.com", port=None, username="tab",
                password="pw", extra=json.dumps({"auth": "password"}))
    base.update(kw)
    return ConnectionCreate(**base)


def _create(session, user, project_id, **kw):
    return rc.create_connection(project_id, _body(**kw), request=None, user=user, session=session)


def _audits(session, action):
    return session.exec(select(AuditLog).where(AuditLog.action == action)).all()


# ── create and payload ─────────────────────────────────────────────────────────
def test_a_password_connection_and_its_payload(session):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    assert out.db_type == "sftp" and out.has_password and not out.has_private_key
    assert out.host_key_fingerprint == "" and out.host_key_type == ""
    conn = session.get(Connection, out.id)
    pay = rc.engine_connection_payload(conn)
    assert pay["host"] == "sftp.example.com" and pay["port"] == 22 and pay["username"] == "tab"
    assert pay["auth"] == "password" and decrypt_secret(pay["password_encrypted"]) == "pw"
    assert pay["private_key_encrypted"] == ""
    assert pay["host_key_fingerprint"] == "" and pay["db_type"] == "sftp"


def test_a_key_connection_stores_key_and_passphrase_encrypted_together(session):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id, password="", private_key=KEY, private_key_passphrase="secret",
                  extra=json.dumps({"auth": "key"}))
    assert out.has_private_key and not out.has_password
    conn = session.get(Connection, out.id)
    assert KEY not in conn.private_key_encrypted
    assert json.loads(decrypt_secret(conn.private_key_encrypted)) == {"private_key": KEY, "passphrase": "secret"}
    pay = rc.engine_connection_payload(conn)
    assert pay["auth"] == "key" and pay["private_key_encrypted"] == conn.private_key_encrypted
    assert pay["password_encrypted"] == ""
    # never in the API's answers
    assert KEY not in out.model_dump_json() and "secret" not in out.model_dump_json()


@pytest.mark.parametrize("kw, message", [
    (dict(username=""), "user"),
    (dict(password=""), "password"),
    (dict(password="", extra=json.dumps({"auth": "key"})), "private key"),
    (dict(password="", private_key="not a key", extra=json.dumps({"auth": "key"})), "private key"),
    (dict(host="sftp://sftp.example.com"), "host"),
    (dict(port=70000), "port"),
    (dict(extra=json.dumps({"auth": "kerberos"})), "auth"),
])
def test_what_an_sftp_connection_needs(session, kw, message):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    with pytest.raises(HTTPException) as e:
        _create(session, admin, p.id, **kw)
    assert e.value.status_code == 422 and message in str(e.value.detail).lower()


def test_a_private_key_only_goes_on_an_sftp_connection(session):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    with pytest.raises(HTTPException) as e:
        rc.create_connection(p.id, ConnectionCreate(name="pg", db_type="postgresql", host="db", private_key=KEY),
                             request=None, user=admin, session=session)
    assert e.value.status_code == 422


# ── trust on first use ─────────────────────────────────────────────────────────
async def test_a_plain_test_shows_an_unaccepted_key_and_stores_nothing(session, fake_engine):
    """Step one: the engine shows the key and sends no credential; the gateway keeps nothing."""
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    res = await rc.test_connection(out.id, None, _Req(), admin, session)
    assert res["needs_acceptance"] is True and res["host_key"]["fingerprint"] == "SHA256:server-key"
    assert res.get("accepted") is not True
    assert session.get(Connection, out.id).host_key_fingerprint == ""
    assert _audits(session, audit.CONN_HOST_KEY_ACCEPT) == []
    (sent,) = fake_engine.sftp_inspects
    assert sent["expected_fingerprint"] == "" and sent["connection"]["host_key_fingerprint"] == ""


async def test_accepting_the_key_shown_stores_it_after_the_engine_authenticated(session, fake_engine):
    """Step two: the call names the key; the engine checks it BEFORE the credentials,
    and the gateway stores it and writes the audit line only after a success."""
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    res = await rc.test_connection(out.id, rc.HostKeyAcceptance(expected_fingerprint="SHA256:server-key", expected_type="ssh-ed25519"),
                                   _Req(), admin, session)
    assert res["ok"] is True and res["accepted"] is True
    sent = fake_engine.sftp_inspects[-1]
    assert (sent["expected_fingerprint"], sent["expected_type"]) == ("SHA256:server-key", "ssh-ed25519")
    conn = session.get(Connection, out.id)
    assert (conn.host_key_type, conn.host_key_fingerprint) == ("ssh-ed25519", "SHA256:server-key")
    (line,) = _audits(session, audit.CONN_HOST_KEY_ACCEPT)
    assert json.loads(line.detail) == {"type": "ssh-ed25519", "fingerprint": "SHA256:server-key", "previous": "",
                                       "host": "sftp.example.com:22"}
    # a test of the accepted key: credentials checked, nothing new recorded
    res = await rc.test_connection(out.id, None, _Req(), admin, session)
    assert res["ok"] is True and res["needs_acceptance"] is False
    assert len(_audits(session, audit.CONN_HOST_KEY_ACCEPT)) == 1


async def test_a_changed_key_is_shown_with_the_previous_one_and_accepted_only_on_request(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    conn = session.get(Connection, out.id)
    conn.host_key_type, conn.host_key_fingerprint = "ssh-ed25519", "SHA256:old"
    session.add(conn); session.commit()
    fake_engine.sftp_server_key = {"type": "ssh-rsa", "fingerprint": "SHA256:new"}
    res = await rc.test_connection(out.id, None, _Req(), admin, session)
    assert res["needs_acceptance"] is True and res["changed"] is True and res["previous"] == "SHA256:old"
    session.refresh(conn)
    assert conn.host_key_fingerprint == "SHA256:old" and _audits(session, audit.CONN_HOST_KEY_ACCEPT) == []
    await rc.test_connection(out.id, rc.HostKeyAcceptance(expected_fingerprint="SHA256:new", expected_type="ssh-rsa"),
                             _Req(), admin, session)
    session.refresh(conn)
    assert (conn.host_key_type, conn.host_key_fingerprint) == ("ssh-rsa", "SHA256:new")
    (line,) = _audits(session, audit.CONN_HOST_KEY_ACCEPT)
    assert json.loads(line.detail)["previous"] == "SHA256:old"


async def test_accepting_a_key_the_server_no_longer_offers_is_refused(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    with pytest.raises(HTTPException) as e:
        await rc.test_connection(out.id, rc.HostKeyAcceptance(expected_fingerprint="SHA256:what-i-saw"),
                                 _Req("it"), admin, session)
    assert e.value.status_code == 409 and "SHA256:server-key" in e.value.detail
    assert "Rifai la prova" in e.value.detail  # in the language of the request
    assert session.get(Connection, out.id).host_key_fingerprint == ""
    assert _audits(session, audit.CONN_HOST_KEY_ACCEPT) == []


async def test_the_draft_test_shows_the_key_then_accepts_it_and_stores_nothing(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    res = await rc.test_draft_connection(p.id, ConnectionTest(**_body().model_dump()), _Req(), user=admin, session=session)
    assert res["needs_acceptance"] is True and res["host_key"]["fingerprint"] == "SHA256:server-key"
    res = await rc.test_draft_connection(p.id, ConnectionTest(**_body().model_dump(), expected_fingerprint="SHA256:server-key"),
                                         _Req(), user=admin, session=session)
    assert res["ok"] is True
    assert session.exec(select(Connection)).all() == []
    first, second = fake_engine.sftp_inspects
    assert decrypt_secret(second["connection"]["password_encrypted"]) == "pw"
    assert (first["expected_fingerprint"], second["expected_fingerprint"]) == ("", "SHA256:server-key")
    with pytest.raises(HTTPException) as e:
        await rc.test_draft_connection(p.id, ConnectionTest(**_body().model_dump(), expected_fingerprint="SHA256:other"),
                                       _Req(), user=admin, session=session)
    assert e.value.status_code == 409


async def test_a_draft_test_never_uses_a_stored_secret(session, fake_engine):
    """Editing the address, the secret is typed again: a stored password must never
    be sent to a host the form names (the old `secrets_from` is gone)."""
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    body = {**_body(host="new.example.com", password="").model_dump(), "secrets_from": out.id}
    assert "secrets_from" not in ConnectionTest.model_fields
    with pytest.raises(HTTPException) as e:
        await rc.test_draft_connection(p.id, ConnectionTest(**body), _Req(), user=admin, session=session)
    assert e.value.status_code == 422 and fake_engine.sftp_inspects == []


def test_a_change_of_host_or_port_forgets_the_accepted_key(session):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    conn = session.get(Connection, out.id)
    conn.host_key_type, conn.host_key_fingerprint = "ssh-ed25519", "SHA256:k"
    session.add(conn); session.commit()
    rc.update_connection(out.id, ConnectionUpdate(password="pw2"), request=None, user=admin, session=session)
    assert session.get(Connection, out.id).host_key_fingerprint == "SHA256:k"
    rc.update_connection(out.id, ConnectionUpdate(port=2222, password="pw3"), request=None, user=admin, session=session)
    conn = session.get(Connection, out.id)
    assert conn.host_key_fingerprint == "" and conn.host_key_type == ""


@pytest.mark.parametrize("change", [dict(host="other.example.com"), dict(port=2200), dict(username="someone")])
def test_a_new_address_or_user_needs_the_secret_typed_again(session, change):
    """The stored password must not follow the connection to another server or account."""
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    with pytest.raises(HTTPException) as e:
        rc.update_connection(out.id, ConnectionUpdate(**change), request=None, user=admin, session=session)
    assert e.value.status_code == 422 and "again" in e.value.detail
    assert session.get(Connection, out.id).host == "sftp.example.com"
    rc.update_connection(out.id, ConnectionUpdate(**change, password="new"), request=None, user=admin, session=session)


def test_a_secret_of_the_other_method_does_not_carry_the_stored_password_to_a_new_host(session):
    """Review bypass: moving the host with a private key on a password connection kept the old
    password, which the next test then sent to the new host. The move now drops every secret not
    typed again, so the password method has none and the edit is refused."""
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    with pytest.raises(HTTPException) as e:
        rc.update_connection(out.id, ConnectionUpdate(host="attacker.example.com", private_key=KEY), request=None, user=admin, session=session)
    assert e.value.status_code == 422
    assert session.get(Connection, out.id).host == "sftp.example.com"


def test_switching_the_method_back_after_a_move_finds_no_stored_password(session):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    rc.update_connection(out.id, ConnectionUpdate(extra=json.dumps({"auth": "key"}), private_key=KEY), request=None, user=admin, session=session)
    rc.update_connection(out.id, ConnectionUpdate(host="attacker.example.com", private_key=KEY), request=None, user=admin, session=session)
    with pytest.raises(HTTPException) as e:
        rc.update_connection(out.id, ConnectionUpdate(extra=json.dumps({"auth": "password"})), request=None, user=admin, session=session)
    assert e.value.status_code == 422
    assert session.get(Connection, out.id).password_encrypted == ""


def test_switching_to_the_key_method_needs_a_key(session):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    with pytest.raises(HTTPException) as e:
        rc.update_connection(out.id, ConnectionUpdate(extra=json.dumps({"auth": "key"})), request=None, user=admin, session=session)
    assert e.value.status_code == 422
    rc.update_connection(out.id, ConnectionUpdate(extra=json.dumps({"auth": "key"}), private_key=KEY),
                         request=None, user=admin, session=session)
    conn = session.get(Connection, out.id)
    assert json.loads(decrypt_secret(conn.private_key_encrypted))["private_key"] == KEY
    assert rc.engine_connection_payload(conn)["auth"] == "key"


def test_a_passphrase_alone_keeps_the_stored_key(session):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id, password="", private_key=KEY, extra=json.dumps({"auth": "key"}))
    rc.update_connection(out.id, ConnectionUpdate(private_key_passphrase="later"), request=None, user=admin, session=session)
    blob = json.loads(decrypt_secret(session.get(Connection, out.id).private_key_encrypted))
    assert blob == {"private_key": KEY, "passphrase": "later"}


def test_a_duplicate_keeps_key_and_accepted_host_key(session):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id, password="", private_key=KEY, extra=json.dumps({"auth": "key"}))
    conn = session.get(Connection, out.id)
    conn.host_key_type, conn.host_key_fingerprint = "ssh-ed25519", "SHA256:k"
    session.add(conn); session.commit()
    copy = rc.duplicate_connection(out.id, None, admin, session)
    c2 = session.get(Connection, copy.id)
    assert c2.private_key_encrypted == conn.private_key_encrypted and c2.host_key_fingerprint == "SHA256:k"


# ── files and translated refusals ──────────────────────────────────────────────
async def test_the_file_list_sends_folder_and_pattern(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    fake_engine.sftp_responses["files"] = (200, {"files": [{"path": "/in/a.csv", "size": 3, "modified_at": ""}], "total": 1})
    res = await rc.list_sftp_files(out.id, SftpFilesQuery(folder="/in", pattern="*.csv"), _Req(), admin, session)
    assert res["total"] == 1
    (sent,) = fake_engine.sftp_inspects
    assert (sent["action"], sent["folder"], sent["pattern"]) == ("files", "/in", "*.csv")


@pytest.mark.parametrize("lang, words", [("en", "has changed"), ("it", "è cambiata"), ("de", "geändert"),
                                         ("fr", "a changé"), ("es", "ha cambiado")])
async def test_a_changed_host_key_is_explained_in_the_language_of_the_request(session, fake_engine, lang, words):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    fake_engine.sftp_responses["files"] = (409, {"detail": {
        "code": "host_key_changed", "message": "SFTP: the host key of h:22 has changed", "offered": "SHA256:new",
        "expected": "SHA256:old", "host": "sftp.example.com:22"}})
    with pytest.raises(HTTPException) as e:
        await rc.list_sftp_files(out.id, SftpFilesQuery(folder="", pattern="*"), _Req(lang), admin, session)
    assert e.value.status_code == 409
    assert words in e.value.detail and "SHA256:new" in e.value.detail and "SHA256:old" in e.value.detail


async def test_a_key_never_accepted_is_explained_too(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    fake_engine.sftp_responses["files"] = (409, {"detail": {
        "code": "host_key_unknown", "message": "SFTP: ...", "offered": "SHA256:new", "expected": "", "host": "h:22"}})
    with pytest.raises(HTTPException) as e:
        await rc.list_sftp_files(out.id, SftpFilesQuery(folder="", pattern="*"), _Req("it"), admin, session)
    assert "non è ancora stata accettata" in e.value.detail


async def test_the_file_list_wants_connect_and_an_sftp_connection(session, fake_engine):
    admin = make_user(session, email="a@x.it", is_superuser=True)
    p = make_project(session, name="p")
    out = _create(session, admin, p.id)
    nobody = make_user(session, email="n@x.it")
    with pytest.raises(HTTPException) as e:
        await rc.list_sftp_files(out.id, SftpFilesQuery(folder="", pattern="*"), _Req(), nobody, session)
    assert e.value.status_code in (403, 404)
    pg = rc.create_connection(p.id, ConnectionCreate(name="pg", db_type="postgresql", host="db"), request=None,
                              user=admin, session=session)
    with pytest.raises(HTTPException) as e:
        await rc.list_sftp_files(pg.id, SftpFilesQuery(folder="", pattern="*"), _Req(), admin, session)
    assert e.value.status_code == 422
