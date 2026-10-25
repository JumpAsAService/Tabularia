"""Sending the output as an email attachment (an Output node with destType="email").

It is the only destination that sends data towards an address written by hand in the
flow's definition, and saving a flow requires only EDIT. So the security boundary is
not the interface but this path, and it is all here:

1. the recipients are validated against the connection's ALLOWED DOMAINS, by the
   gateway — the worker receives them resolved already and could no longer tell them
   apart;
2. using the connection requires CONNECT, as every other destination;
3. the SMTP password never leaves the gateway in clear;
4. every send ends up in the AUDIT at launch, with recipients and subject: the domains
   say WHERE data may go out, the audit says WHAT went out.
"""
import json

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.models import AuditLog, Connection, Run
from app.models.permission import Capability
from app.routes.runs import _launch_flow_run
from app.schemas.models import RunCreate, RunEmailSpec
from app.services import audit
from tests.conftest import make_flow, make_permission, make_project, make_user

pytestmark = pytest.mark.anyio


def _smtp(session, project_id: int, *, domains=None, name="posta"):
    extra = {"from_address": "report@azienda.it", "tls": "starttls"}
    if domains is not None:
        extra["allowed_domains"] = domains
    c = Connection(
        name=name, project_id=project_id, db_type="smtp",
        host="smtp.azienda.it", port=587, username="report",
        password_encrypted="gAAAAA-finta-cifrata", extra=json.dumps(extra),
    )
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _body(conn_id: int, *, to, cc=None, **kw) -> RunCreate:
    return RunCreate(
        bucket="data-prep",
        input_key="datasets/x.parquet",
        operations=[],
        email=RunEmailSpec(connection_id=conn_id, to=to, cc=cc or [], subject="Report", **kw),
    )


async def _launch(session, user, flow, body):
    return await _launch_flow_run(session, user, flow, body)


# ── 1. allowed domains: the real barrier ────────────────────────────────────
async def test_a_recipient_outside_the_allowed_domains_is_refused(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id, domains=["azienda.it"])

    with pytest.raises(HTTPException) as e:
        await _launch(session, admin, flow, _body(conn.id, to=["tizio@gmail.com"]))
    assert e.value.status_code == 422
    assert "azienda.it" in str(e.value.detail)
    assert fake_engine.transforms == [], "non deve nemmeno arrivare all'engine"


async def test_the_check_covers_cc_too(session, fake_engine):
    """Otherwise putting the external address in CC would be enough."""
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id, domains=["azienda.it"])

    with pytest.raises(HTTPException) as e:
        await _launch(session, admin, flow, _body(conn.id, to=["capo@azienda.it"], cc=["tizio@gmail.com"]))
    assert e.value.status_code == 422
    assert fake_engine.transforms == []


async def test_an_empty_list_means_no_limit(session, fake_engine):
    """Whoever does not need the constraint must not notice it."""
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id, domains=[])

    run = await _launch(session, admin, flow, _body(conn.id, to=["chiunque@ovunque.org"]))
    assert run.id is not None and len(fake_engine.transforms) == 1


async def test_allowed_domains_are_matched_case_insensitively(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id, domains=["Azienda.IT"])

    run = await _launch(session, admin, flow, _body(conn.id, to=["Capo@AZIENDA.it"]))
    assert run.id is not None


# ── 2. the connection: right kind, CONNECT, recipients ──────────────────────
async def test_a_connection_that_is_not_smtp_is_refused(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    other_one = Connection(name="db", project_id=p.id, db_type="postgresql", host="pg")
    session.add(other_one)
    session.commit()
    session.refresh(other_one)

    with pytest.raises(HTTPException) as e:
        await _launch(session, admin, flow, _body(other_one.id, to=["capo@azienda.it"]))
    assert e.value.status_code == 422


async def test_a_missing_connection_is_404(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    with pytest.raises(HTTPException) as e:
        await _launch(session, admin, flow, _body(999, to=["capo@azienda.it"]))
    assert e.value.status_code == 404


async def test_no_recipients_is_refused(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)
    with pytest.raises(HTTPException) as e:
        await _launch(session, admin, flow, _body(conn.id, to=[" "]))
    assert e.value.status_code == 422


async def test_using_the_connection_needs_connect(session, fake_engine):
    """RUN on the flow is not enough: the connection carries credentials."""
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)
    someone = make_user(session, email="t@x.local")
    make_permission(session, user_id=someone.id, project_id=p.id, capability=Capability.RUN.value)

    with pytest.raises(HTTPException) as e:
        await _launch(session, someone, flow, _body(conn.id, to=["capo@azienda.it"]))
    assert e.value.status_code == 403


# ── 3. what really goes out towards the engine ──────────────────────────────
async def test_the_secret_leaves_the_gateway_encrypted(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)

    await _launch(session, admin, flow, _body(conn.id, to=["capo@azienda.it"]))

    sent = fake_engine.transforms[0]["email"]
    assert sent["connection"]["password_encrypted"] == "gAAAAA-finta-cifrata"
    assert "password" not in sent["connection"], "nessuna password in chiaro nel payload"
    assert sent["connection"]["from_address"] == "report@azienda.it"
    assert sent["connection"]["tls"] == "starttls"


async def test_the_node_options_travel_intact(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)

    body = _body(
        conn.id, to=["capo@azienda.it"],
        body="<b>ciao</b>", body_is_html=True,
        attachment_name="vendite", attachment_format="csv", stop_on_failure=False,
    )
    await _launch(session, admin, flow, body)

    sent = fake_engine.transforms[0]["email"]
    assert sent["stop_on_failure"] is False
    assert sent["target"]["body_is_html"] is True
    assert sent["target"]["attachment_format"] == "csv"
    assert sent["target"]["attachment_name"] == "vendite"


async def test_stop_on_failure_defaults_to_on(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)

    await _launch(session, admin, flow, _body(conn.id, to=["capo@azienda.it"]))
    assert fake_engine.transforms[0]["email"]["stop_on_failure"] is True


# ── 4. traces: run row and audit ────────────────────────────────────────────
async def test_the_run_row_records_the_request_as_pending(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)

    run = await _launch(session, admin, flow, _body(conn.id, to=["capo@azienda.it"]))

    summary = json.loads(session.get(Run, run.id).email)
    assert summary["to"] == ["capo@azienda.it"]
    assert summary["host"] == "smtp.azienda.it"
    assert summary["ok"] is None, "in attesa dell'esito dal worker"


async def test_every_send_is_audited_with_who_and_where(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)

    await _launch(session, admin, flow, _body(conn.id, to=["capo@azienda.it", "cfo@azienda.it"]))

    row = session.exec(select(AuditLog).where(AuditLog.action == audit.EMAIL_SEND)).one()
    assert row.actor_label == "a@x.local"
    assert row.target_label == "f"
    detail = json.loads(row.detail)
    assert detail["to"] == ["capo@azienda.it", "cfo@azienda.it"]
    assert detail["host"] == "smtp.azienda.it"


# ── 5. dry run: the server sets the recipient ───────────────────────────────
async def test_the_dry_run_sends_only_to_the_caller(session, fake_engine):
    """If the client could choose the recipient, this route would be the handiest way around
    the allowed domains."""
    from app.routes.runs import launch_email_test

    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id, domains=["azienda.it"])

    body = _body(conn.id, to=["capo@azienda.it"], cc=["cfo@azienda.it"])
    await launch_email_test(flow.id, body, user=admin, session=session)

    sent = fake_engine.transforms[0]["email"]
    assert sent["target"]["to"] == ["a@x.local"]
    assert sent["target"]["cc"] == []


async def test_the_dry_run_ignores_the_allowed_domains(session, fake_engine):
    """Sending to oneself is not exfiltration: with the domains narrowed to the customers,
    the trial button would never work otherwise."""
    from app.routes.runs import launch_email_test

    someone = make_user(session, email="tizio@gmail.com", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id, domains=["azienda.it"])

    await launch_email_test(flow.id, _body(conn.id, to=["capo@azienda.it"]), user=someone, session=session)
    assert fake_engine.transforms[0]["email"]["target"]["to"] == ["tizio@gmail.com"]


async def test_the_dry_run_never_fails_the_run_and_writes_nothing(session, fake_engine):
    from app.routes.runs import launch_email_test

    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)

    body = _body(conn.id, to=["capo@azienda.it"], stop_on_failure=True)
    await launch_email_test(flow.id, body, user=admin, session=session)

    mail_body = fake_engine.transforms[0]
    assert mail_body["email"]["stop_on_failure"] is False  # a trial fails nothing
    assert mail_body["destination"] is None and mail_body["mirror"] is None


async def test_the_dry_run_is_audited_as_such(session, fake_engine):
    """It must stay distinguishable from a real send in the history."""
    from app.routes.runs import launch_email_test

    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)

    await launch_email_test(flow.id, _body(conn.id, to=["capo@azienda.it"]), user=admin, session=session)

    row = session.exec(select(AuditLog).where(AuditLog.action == audit.EMAIL_SEND)).one()
    detail = json.loads(row.detail)
    assert detail["trigger"] == "dry_run"
    assert detail["to"] == ["a@x.local"]


async def test_the_dry_run_needs_an_email_node(session, fake_engine):
    from app.routes.runs import launch_email_test

    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    without = RunCreate(bucket="data-prep", input_key="datasets/x.parquet", operations=[])
    with pytest.raises(HTTPException) as e:
        await launch_email_test(flow.id, without, user=admin, session=session)
    assert e.value.status_code == 422


async def test_a_refused_send_leaves_no_audit_trail_of_a_send(session, fake_engine):
    """The refusal happens BEFORE: it must not look as if something went out."""
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id, domains=["azienda.it"])

    with pytest.raises(HTTPException):
        await _launch(session, admin, flow, _body(conn.id, to=["tizio@gmail.com"]))

    assert session.exec(select(AuditLog).where(AuditLog.action == audit.EMAIL_SEND)).all() == []
