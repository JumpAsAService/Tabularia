"""The `connections.extra` column: the kind's options that have no column.

Only SMTP needs it (sender, TLS mode, allowed domains), and it comes from a precise
constraint: without a whole write→read-back round, an SMTP connection can be created
but not configured, and the email node never goes. So here the round is tested, not
the single piece:

  form → ConnectionCreate.extra → column → ConnectionOut.extra → form
                                     └→ allowed_email_domains / engine payload

The other half is the moment a broken JSON is refused: at saving, by whoever is
writing it, and not days later to whoever wonders why the email did not arrive.
"""
import json

import pytest
from fastapi import HTTPException

from app.models import Connection
from app.routes.connections import (
    allowed_email_domains,
    create_connection,
    engine_connection_payload,
    list_all_connections,
    update_connection,
)
from app.schemas.models import ConnectionCreate, ConnectionUpdate
from tests.conftest import make_project, make_user

OPTIONS = {
    "from_address": "report@azienda.it",
    "from_name": "Report Tabularia",
    "tls": "ssl",
    "allowed_domains": ["azienda.it", "clienti.it"],
}


def _create(session, user, project_id, *, extra=None, name="posta"):
    body = ConnectionCreate(
        name=name,
        db_type="smtp",
        host="smtp.azienda.it",
        port=465,
        username="report",
        password="segreta",
        **({"extra": json.dumps(extra)} if extra is not None else {}),
    )
    return create_connection(project_id, body, request=None, user=user, session=session)


@pytest.fixture
def scene(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    return admin, make_project(session, name="p")


# ── 1. the whole round ──────────────────────────────────────────────────────
def test_the_options_survive_the_round_trip_to_the_form(session, scene):
    """Without this, the edit form would reopen empty and the first save would delete
    sender, TLS and domains."""
    admin, p = scene
    created = _create(session, admin, p.id, extra=OPTIONS)
    assert json.loads(created.extra) == OPTIONS

    riletta = [c for c in list_all_connections(user=admin, session=session) if c.id == created.id][0]
    assert json.loads(riletta.extra) == OPTIONS


def test_the_saved_options_are_the_ones_that_limit_the_recipients(session, scene):
    """The column's point: the domains written in the form must be those the gateway applies
    at launch, not a copy living elsewhere."""
    admin, p = scene
    created = _create(session, admin, p.id, extra=OPTIONS)

    row = session.get(Connection, created.id)
    assert allowed_email_domains(row) == ["azienda.it", "clienti.it"]

    payload = engine_connection_payload(row)
    assert payload["from_address"] == "report@azienda.it"
    assert payload["from_name"] == "Report Tabularia"
    assert payload["tls"] == "ssl"


def test_editing_them_changes_what_the_send_will_do(session, scene):
    admin, p = scene
    created = _create(session, admin, p.id, extra=OPTIONS)

    new_ones = dict(OPTIONS, allowed_domains=["altra.it"], tls="starttls")
    updated = update_connection(
        created.id, ConnectionUpdate(extra=json.dumps(new_ones)), request=None, user=admin, session=session
    )
    assert json.loads(updated.extra)["allowed_domains"] == ["altra.it"]
    assert allowed_email_domains(session.get(Connection, created.id)) == ["altra.it"]


def test_a_rename_does_not_wipe_the_options(session, scene):
    """`extra` is optional in PATCH: omitting it must mean «do not touch it», otherwise
    renaming a connection would disable it silently."""
    admin, p = scene
    created = _create(session, admin, p.id, extra=OPTIONS)

    update_connection(
        created.id, ConnectionUpdate(name="posta-rinominata"), request=None, user=admin, session=session
    )
    assert allowed_email_domains(session.get(Connection, created.id)) == ["azienda.it", "clienti.it"]


# ── 2. the refusal happens at saving ────────────────────────────────────────
def test_broken_json_is_refused_when_it_is_saved(session, scene):
    admin, p = scene
    body = ConnectionCreate(name="rotta", db_type="smtp", host="h", extra="{non chiuso")
    with pytest.raises(HTTPException) as e:
        create_connection(p.id, body, request=None, user=admin, session=session)
    assert e.value.status_code == 422
    assert session.get(Connection, 1) is None, "niente riga a metà"


def test_json_that_is_not_an_object_is_refused(session, scene):
    """A list would pass json.loads but then `smtp_options` would discard it and the
    configuration would vanish without anyone saying so."""
    admin, p = scene
    with pytest.raises(HTTPException) as e:
        create_connection(
            p.id, ConnectionCreate(name="lista", db_type="smtp", host="h", extra='["azienda.it"]'),
            request=None, user=admin, session=session,
        )
    assert e.value.status_code == 422


def test_broken_json_is_refused_on_update_too(session, scene):
    admin, p = scene
    created = _create(session, admin, p.id, extra=OPTIONS)
    with pytest.raises(HTTPException) as e:
        update_connection(created.id, ConnectionUpdate(extra="{rotto"), request=None, user=admin, session=session)
    assert e.value.status_code == 422
    assert allowed_email_domains(session.get(Connection, created.id)) == ["azienda.it", "clienti.it"]


# ── 3. the other kinds and the secrets ──────────────────────────────────────
def test_a_connection_without_options_gets_an_empty_object(session, scene):
    """The other kinds do not use `extra`: they must see neither a null nor an error, and
    `smtp_options` must always be able to read it."""
    admin, p = scene
    created = create_connection(
        p.id, ConnectionCreate(name="pg", db_type="postgresql", host="pg", database="shop"),
        request=None, user=admin, session=session,
    )
    assert created.extra == "{}"
    assert allowed_email_domains(session.get(Connection, created.id)) == []


def test_the_options_come_back_but_the_password_never_does(session, scene):
    """`extra` may go back to the client precisely because it holds no secrets: if one day it
    held some, this test is the place to notice."""
    admin, p = scene
    created = _create(session, admin, p.id, extra=OPTIONS)
    fields = created.model_dump()
    assert "password" not in fields and "password_encrypted" not in fields
    assert fields["has_password"] is True
    assert "segreta" not in json.dumps(fields, default=str)  # default=str: there is updated_at


# ── SQL Server: TLS ─────────────────────────────────────────────────────────
# `extra.tls` = verify (default: verified certificate) | trust (self-signed) | none.
def _sqlserver(session, user, project_id, *, extra=None, name="vendite"):
    body = ConnectionCreate(name=name, db_type="sqlserver", host="sql.x.it", username="u", password="p", database="vendite",
                            **({"extra": json.dumps(extra)} if extra is not None else {}))
    return create_connection(project_id, body, request=None, user=user, session=session)


def test_sqlserver_tls_reaches_the_engine_and_verifies_by_default(session, scene):
    admin, p = scene
    with_ = _sqlserver(session, admin, p.id, extra={"tls": "trust"})
    without = _sqlserver(session, admin, p.id, name="vendite2")
    assert engine_connection_payload(session.get(Connection, with_.id))["extra"] == {"tls": "trust"}
    assert engine_connection_payload(session.get(Connection, without.id))["extra"] == {"tls": "verify"}


def test_sqlserver_an_unknown_tls_is_refused_at_saving(session, scene):
    admin, p = scene
    with pytest.raises(HTTPException) as e:
        _sqlserver(session, admin, p.id, extra={"tls": "forse"})
    assert e.value.status_code == 422 and "verify, trust or none" in e.value.detail
    created = _sqlserver(session, admin, p.id)
    with pytest.raises(HTTPException):
        update_connection(created.id, ConnectionUpdate(extra=json.dumps({"tls": "forse"})), request=None, user=admin, session=session)


@pytest.mark.anyio
async def test_sqlserver_the_forms_test_carries_the_tls(session, scene, monkeypatch):
    """The «Test» button tests the connection BEFORE saving: without TLS a server with a
    self-signed certificate would fail even with «trust» chosen."""
    import app.routes.connections as rc

    admin, p = scene
    seen = {}

    async def fake(payload, action, path="", **kw):  # kw: the language of the messages
        seen.update(payload)
        return {"ok": True}

    monkeypatch.setattr(rc, "_engine_inspect", fake)
    body = ConnectionCreate(name="x", db_type="sqlserver", host="h", username="u", password="p", database="d", extra=json.dumps({"tls": "trust"}))
    await rc.test_draft_connection(p.id, body, user=admin, session=session)
    assert seen["extra"] == {"tls": "trust"}
