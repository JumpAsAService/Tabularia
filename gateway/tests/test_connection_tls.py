"""The encryption (TLS) of the database connections, on the gateway's side.

Every database type has the same choice in `extra`: tls = verify | trust | none, and with
verify an optional company authority (`ca_cert`, PEM). Saved, it is checked (a choice the
engine knows, a certificate that reads, never a private key); sent to the engine with
every operation that opens the database (the payload); carried into the native dbt
profile. A connection saved before the choice sends nothing and keeps its driver's old
behaviour; SQL Server keeps verify as its default and takes no authority."""
import datetime as dt
import json

import pytest
from fastapi import HTTPException

from app.models import Connection
from app.routes.connections import create_connection, engine_connection_payload, update_connection
from app.schemas.models import ConnectionCreate, ConnectionUpdate
from app.services import db_tls
from app.services import dbt_export as d
from tests.conftest import make_project, make_user
from tests.test_dbt_export import _edge, _src_node, scene  # noqa: F401 (scene is a fixture)
from tests.test_dbt_export_resolution import _db_output, _flow


def _pem() -> tuple[str, str]:
    """A company authority and its private key, made on the spot."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Company CA")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + dt.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(key, hashes.SHA256()))
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return cert.public_bytes(serialization.Encoding.PEM).decode(), private


CA, PRIVATE = _pem()


@pytest.fixture
def owner(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    return admin, make_project(session, name="p")


def _create(session, user, project_id, db_type="postgresql", extra=None, name="erp"):
    body = ConnectionCreate(name=name, db_type=db_type, host="db.x.it", username="u", password="p", database="erp",
                            **({"extra": json.dumps(extra)} if extra is not None else {}))
    return create_connection(project_id, body, request=None, user=user, session=session)


@pytest.mark.parametrize("db_type", ["postgresql", "mysql", "mariadb", "clickhouse", "trino", "oracle"])
def test_every_database_takes_the_choice_and_the_company_authority(session, owner, db_type):
    admin, p = owner
    created = _create(session, admin, p.id, db_type, {"tls": "verify", "ca_cert": CA})
    payload = engine_connection_payload(session.get(Connection, created.id))
    assert payload["extra"] == {"tls": "verify", "ca_cert": CA.strip()}
    # the form reads it back as it was saved
    assert json.loads(created.extra) == {"tls": "verify", "ca_cert": CA}


def test_a_connection_saved_before_the_choice_sends_nothing(session, owner):
    admin, p = owner
    legacy = _create(session, admin, p.id)
    assert engine_connection_payload(session.get(Connection, legacy.id))["extra"] == {}
    plain = _create(session, admin, p.id, extra={"tls": "none"}, name="erp2")
    assert engine_connection_payload(session.get(Connection, plain.id))["extra"] == {"tls": "none"}


@pytest.mark.parametrize("db_type, extra, message", [
    ("mysql", {"tls": "maybe"}, "verify, trust or none"),
    ("postgresql", {"tls": "trust", "ca_cert": CA}, "only to verify"),
    ("oracle", {"ca_cert": CA}, "only to verify"),   # the old behaviour verifies nothing
    ("sqlserver", {"tls": "verify", "ca_cert": CA}, "system's store"),
    ("clickhouse", {"tls": "verify", "ca_cert": "-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----"}, "cannot be read"),
    ("clickhouse", {"tls": "verify", "ca_cert": "my company CA"}, "cannot be read"),
    ("trino", {"tls": "verify", "ca_cert": CA + PRIVATE}, "private key"),
    ("postgresql", {"tls": "verify", "ca_cert": 42}, "text (PEM)"),
])
def test_what_cannot_work_is_refused_at_saving(session, owner, db_type, extra, message):
    admin, p = owner
    with pytest.raises(HTTPException) as e:
        _create(session, admin, p.id, db_type, extra)
    assert e.value.status_code == 422 and message in e.value.detail
    assert PRIVATE not in e.value.detail


def test_editing_changes_the_choice_and_drops_the_authority(session, owner):
    admin, p = owner
    created = _create(session, admin, p.id, "mysql", {"tls": "verify", "ca_cert": CA})
    update_connection(created.id, ConnectionUpdate(extra=json.dumps({"tls": "trust"})), request=None, user=admin, session=session)
    assert engine_connection_payload(session.get(Connection, created.id))["extra"] == {"tls": "trust"}
    with pytest.raises(HTTPException):
        update_connection(created.id, ConnectionUpdate(extra=json.dumps({"tls": "trust", "ca_cert": CA})),
                          request=None, user=admin, session=session)


@pytest.mark.anyio
async def test_the_forms_test_carries_the_choice_and_refuses_a_broken_authority(session, owner, monkeypatch):
    import app.routes.connections as rc

    admin, p = owner
    seen = {}

    async def fake(payload, action, path="", **kw):
        seen.update(payload)
        return {"ok": True}

    monkeypatch.setattr(rc, "_engine_inspect", fake)
    body = ConnectionCreate(name="x", db_type="postgresql", host="h", username="u", password="p", database="d",
                            extra=json.dumps({"tls": "verify", "ca_cert": CA}))
    await rc.test_draft_connection(p.id, body, user=admin, session=session)
    assert seen["extra"] == {"tls": "verify", "ca_cert": CA.strip()}
    seen.clear()
    broken = body.model_copy(update={"extra": json.dumps({"tls": "verify", "ca_cert": "-----BEGIN CERTIFICATE-----\nx"})})
    with pytest.raises(HTTPException) as e:
        await rc.test_draft_connection(p.id, broken, user=admin, session=session)
    assert e.value.status_code == 422 and not seen


def test_the_encrypted_protocols_own_port():
    assert db_tls.default_port("clickhouse", {"tls": "verify"}, 8123) == 8443
    assert db_tls.default_port("clickhouse", "{}", 8123) == 8123
    assert db_tls.default_port("oracle", '{"tls": "trust"}', 1521) == 2484
    assert db_tls.default_port("postgresql", {"tls": "verify"}, 5432) == 5432


# ── the native dbt profile: dbt reaches the warehouse as Tabularia does ─────────
def test_the_native_export_carries_the_encryption(session, scene):
    s = scene
    s.pg.extra = json.dumps({"tls": "verify", "ca_cert": CA})
    s.ch.extra = json.dumps({"tls": "verify"})
    s.ch.port = None
    session.add(s.pg); session.add(s.ch); session.commit()
    flow = _flow(session, s, "Encrypted", [_src_node("s", s.orders.id), _db_output("o", s.pg.id, "report.righe")], [_edge("s", "o")])
    conn = d.build_export_payload(session, flow, "data-prep", "native")["native"]["conn"]
    assert conn["tls"] == "verify" and conn["ca_cert"] == CA.strip() and conn["port"] == 5432


def test_the_federated_export_says_what_it_does_not_carry(session, scene):
    s = scene
    s.pg.extra = json.dumps({"tls": "trust"})
    session.add(s.pg); session.commit()
    flow = _flow(session, s, "Federated", [_src_node("s", s.orders.id), _db_output("o", s.pg.id, "report.righe")], [_edge("s", "o")])
    notes = d.build_export_payload(session, flow, "data-prep", "duckdb")["notes"]
    assert any("«gestionale» encrypted, without verifying" in n and "PGSSLMODE=require" in n for n in notes)


@pytest.mark.anyio
async def test_a_saved_connection_is_tested_with_the_encryption_of_the_form(session, owner, monkeypatch):
    """Editing a connection saved before the choice, the user picks verify and presses Test
    without typing the password again: the saved connection must be tried WITH the new
    choice, or the test would pass on the old settings and the saved one fail later."""
    import app.routes.connections as rc

    admin, p = owner
    created = _create(session, admin, p.id)   # legacy
    seen = {}

    async def fake(payload, action, path="", **kw):
        seen.update(payload)
        return {"ok": True}

    monkeypatch.setattr(rc, "_engine_inspect", fake)
    form = rc.HostKeyAcceptance(extra=json.dumps({"tls": "verify", "ca_cert": CA, "something": "else"}))
    await rc.test_connection(created.id, form, request=None, user=admin, session=session)
    assert seen["extra"] == {"tls": "verify", "ca_cert": CA.strip()} and seen["host"] == "db.x.it"
    seen.clear()
    await rc.test_connection(created.id, None, request=None, user=admin, session=session)
    assert seen["extra"] == {}   # nothing saved: still the old behaviour
    with pytest.raises(HTTPException):
        await rc.test_connection(created.id, rc.HostKeyAcceptance(extra=json.dumps({"tls": "sometimes"})),
                                 request=None, user=admin, session=session)
