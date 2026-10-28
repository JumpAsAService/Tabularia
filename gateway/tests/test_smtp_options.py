"""The options of an SMTP connection are checked when it is saved (review
2026-09-16, R7), and a malformed restriction never reads as "no restriction".

`allowed_domains: true` used to pass the save and then break every flow launch
with a 500 (iterating over a boolean).
"""
import json

import pytest
from fastapi import HTTPException

from app.models import Connection
from app.routes.connections import allowed_email_domains, create_connection, update_connection
from app.routes.runs import _launch_flow_run
from app.schemas.models import ConnectionCreate, ConnectionUpdate, RunCreate, RunEmailSpec
from tests.conftest import make_flow, make_project, make_user


def _create(session, user, project_id, extra):
    body = ConnectionCreate(name="mail", db_type="smtp", host="smtp.example.com", port=587,
                            username="u", password="p", extra=json.dumps(extra))
    return create_connection(project_id, body, request=None, user=user, session=session)


@pytest.mark.parametrize("extra, word", [
    ({"allowed_domains": True}, "allowed_domains"),
    ({"allowed_domains": 5}, "allowed_domains"),
    ({"allowed_domains": ["example.com", 7]}, "allowed_domains"),
    ({"tls": "tls1.2"}, "tls"),
    ({"from_address": ["a@example.com"]}, "from_address"),
])
def test_a_wrong_option_is_refused_when_saved(session, extra, word):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="P")
    with pytest.raises(HTTPException) as e:
        _create(session, admin, p.id, extra)
    assert e.value.status_code == 422 and word in e.value.detail


@pytest.mark.parametrize("domains", [["example.com", "clients.example"], "example.com, clients.example", [], ""])
def test_valid_options_are_saved(session, domains):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="P")
    out = _create(session, admin, p.id, {"from_address": "r@example.com", "tls": "ssl", "allowed_domains": domains})
    assert json.loads(out.extra)["allowed_domains"] == domains


def test_an_update_is_checked_too(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="P")
    conn = _create(session, admin, p.id, {"allowed_domains": ["example.com"]})
    with pytest.raises(HTTPException) as e:
        update_connection(conn.id, ConnectionUpdate(extra=json.dumps({"allowed_domains": True})),
                          request=None, user=admin, session=session)
    assert e.value.status_code == 422


def _stored(session, project_id, extra, name="old") -> Connection:
    # as an older release could have saved it, before the check
    c = Connection(name=name, project_id=project_id, db_type="smtp", host="smtp.example.com", port=587,
                   username="u", password_encrypted="gAAAAA-fake", extra=json.dumps(extra))
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def test_a_malformed_stored_restriction_stops_the_email_instead_of_lifting_it(session):
    p = make_project(session, name="P")
    with pytest.raises(HTTPException) as e:
        allowed_email_domains(_stored(session, p.id, {"allowed_domains": True}))
    assert e.value.status_code == 422 and "old" in e.value.detail


def test_stored_lists_and_texts_still_read_as_before(session):
    p = make_project(session, name="P")
    assert allowed_email_domains(_stored(session, p.id, {"allowed_domains": ["@Example.com", " "]}, "a")) == ["example.com"]
    assert allowed_email_domains(_stored(session, p.id, {"allowed_domains": "a.it, b.it"}, "b")) == ["a.it", "b.it"]
    assert allowed_email_domains(_stored(session, p.id, {}, "c")) == []


@pytest.mark.anyio
async def test_launching_with_a_malformed_connection_is_a_422_not_a_500(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="P")
    conn = _stored(session, p.id, {"allowed_domains": True})
    flow = make_flow(session, project_id=p.id, owner_id=admin.id)
    body = RunCreate(bucket="data-prep", input_key="datasets/x.parquet", operations=[],
                     email=RunEmailSpec(connection_id=conn.id, to=["x@example.com"], subject="Report"))
    with pytest.raises(HTTPException) as e:
        await _launch_flow_run(session, admin, flow, body)
    assert e.value.status_code == 422
    assert fake_engine.transforms == []


@pytest.mark.anyio
async def test_the_forms_test_of_a_new_connection_uses_its_options(session, monkeypatch):
    """«Test connection» before saving tried STARTTLS whatever the form said: the draft's
    options fell into the databases' branch and were dropped («STARTTLS extension not
    supported» from a server without encryption, chosen as such)."""
    import app.routes.connections as rc

    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="P")
    seen = {}

    async def fake(payload, action, path="", **kw):
        seen.update(payload)
        return {"ok": True}

    monkeypatch.setattr(rc, "_engine_inspect", fake)
    body = ConnectionCreate(name="mail", db_type="smtp", host="mailpit", port=1025, username="", password="",
                            extra=json.dumps({"tls": "none", "from_address": "reports@example.com"}))
    await rc.test_draft_connection(p.id, body, user=admin, session=session)
    assert seen["db_type"] == "smtp" and seen["tls"] == "none" and seen["port"] == 1025
    assert seen["from_address"] == "reports@example.com"
    seen.clear()
    with pytest.raises(HTTPException) as e:
        await rc.test_draft_connection(p.id, body.model_copy(update={"extra": json.dumps({"tls": "tls1.2"})}),
                                       user=admin, session=session)
    assert e.value.status_code == 422 and not seen
