"""Invio dell'output come allegato email (nodo Output con destType="email").

È l'unica destinazione che manda dati verso un indirizzo scritto a mano nella
definizione del flusso, e salvare un flusso richiede solo EDIT. Quindi il
confine di sicurezza non è l'interfaccia ma questo percorso, e sta tutto qui:

1. i destinatari sono validati contro i DOMINI AMMESSI della connessione, dal
   gateway — il worker li riceve già risolti e non potrebbe più distinguerli;
2. usare la connessione richiede CONNECT, come ogni altra destinazione;
3. la password SMTP non lascia mai il gateway in chiaro;
4. ogni invio finisce nell'AUDIT al lancio, con destinatari e oggetto: i domini
   dicono DOVE può uscire un dato, l'audit dice COSA è uscito.
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


def _smtp(session, project_id: int, *, domini=None, name="posta"):
    extra = {"from_address": "report@azienda.it", "tls": "starttls"}
    if domini is not None:
        extra["allowed_domains"] = domini
    c = Connection(
        name=name, project_id=project_id, db_type="smtp",
        host="smtp.azienda.it", port=587, username="report",
        password_encrypted="gAAAAA-finta-cifrata", extra=json.dumps(extra),
    )
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _corpo(conn_id: int, *, to, cc=None, **kw) -> RunCreate:
    return RunCreate(
        bucket="data-prep",
        input_key="datasets/x.parquet",
        operations=[],
        email=RunEmailSpec(connection_id=conn_id, to=to, cc=cc or [], subject="Report", **kw),
    )


async def _lancia(session, user, flow, body):
    return await _launch_flow_run(session, user, flow, body)


# ── 1. domini ammessi: la barriera vera ─────────────────────────────────────
async def test_a_recipient_outside_the_allowed_domains_is_refused(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id, domini=["azienda.it"])

    with pytest.raises(HTTPException) as e:
        await _lancia(session, admin, flow, _corpo(conn.id, to=["tizio@gmail.com"]))
    assert e.value.status_code == 422
    assert "azienda.it" in str(e.value.detail)
    assert fake_engine.transforms == [], "non deve nemmeno arrivare all'engine"


async def test_the_check_covers_cc_too(session, fake_engine):
    """Altrimenti basterebbe mettere l'indirizzo esterno in copia."""
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id, domini=["azienda.it"])

    with pytest.raises(HTTPException) as e:
        await _lancia(session, admin, flow, _corpo(conn.id, to=["capo@azienda.it"], cc=["tizio@gmail.com"]))
    assert e.value.status_code == 422
    assert fake_engine.transforms == []


async def test_an_empty_list_means_no_limit(session, fake_engine):
    """Chi non ha bisogno del vincolo non deve accorgersene."""
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id, domini=[])

    run = await _lancia(session, admin, flow, _corpo(conn.id, to=["chiunque@ovunque.org"]))
    assert run.id is not None and len(fake_engine.transforms) == 1


async def test_allowed_domains_are_matched_case_insensitively(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id, domini=["Azienda.IT"])

    run = await _lancia(session, admin, flow, _corpo(conn.id, to=["Capo@AZIENDA.it"]))
    assert run.id is not None


# ── 2. la connessione: tipo giusto, CONNECT, destinatari ────────────────────
async def test_a_connection_that_is_not_smtp_is_refused(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    altra = Connection(name="db", project_id=p.id, db_type="postgresql", host="pg")
    session.add(altra)
    session.commit()
    session.refresh(altra)

    with pytest.raises(HTTPException) as e:
        await _lancia(session, admin, flow, _corpo(altra.id, to=["capo@azienda.it"]))
    assert e.value.status_code == 422


async def test_a_missing_connection_is_404(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    with pytest.raises(HTTPException) as e:
        await _lancia(session, admin, flow, _corpo(999, to=["capo@azienda.it"]))
    assert e.value.status_code == 404


async def test_no_recipients_is_refused(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)
    with pytest.raises(HTTPException) as e:
        await _lancia(session, admin, flow, _corpo(conn.id, to=[" "]))
    assert e.value.status_code == 422


async def test_using_the_connection_needs_connect(session, fake_engine):
    """RUN sul flusso non basta: la connessione porta credenziali."""
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)
    tizio = make_user(session, email="t@x.local")
    make_permission(session, user_id=tizio.id, project_id=p.id, capability=Capability.RUN.value)

    with pytest.raises(HTTPException) as e:
        await _lancia(session, tizio, flow, _corpo(conn.id, to=["capo@azienda.it"]))
    assert e.value.status_code == 403


# ── 3. cosa parte davvero verso l'engine ────────────────────────────────────
async def test_the_secret_leaves_the_gateway_encrypted(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)

    await _lancia(session, admin, flow, _corpo(conn.id, to=["capo@azienda.it"]))

    inviato = fake_engine.transforms[0]["email"]
    assert inviato["connection"]["password_encrypted"] == "gAAAAA-finta-cifrata"
    assert "password" not in inviato["connection"], "nessuna password in chiaro nel payload"
    assert inviato["connection"]["from_address"] == "report@azienda.it"
    assert inviato["connection"]["tls"] == "starttls"


async def test_the_node_options_travel_intact(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)

    body = _corpo(
        conn.id, to=["capo@azienda.it"],
        body="<b>ciao</b>", body_is_html=True,
        attachment_name="vendite", attachment_format="csv", stop_on_failure=False,
    )
    await _lancia(session, admin, flow, body)

    inviato = fake_engine.transforms[0]["email"]
    assert inviato["stop_on_failure"] is False
    assert inviato["target"]["body_is_html"] is True
    assert inviato["target"]["attachment_format"] == "csv"
    assert inviato["target"]["attachment_name"] == "vendite"


async def test_stop_on_failure_defaults_to_on(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)

    await _lancia(session, admin, flow, _corpo(conn.id, to=["capo@azienda.it"]))
    assert fake_engine.transforms[0]["email"]["stop_on_failure"] is True


# ── 4. tracce: riga del run e audit ─────────────────────────────────────────
async def test_the_run_row_records_the_request_as_pending(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)

    run = await _lancia(session, admin, flow, _corpo(conn.id, to=["capo@azienda.it"]))

    riassunto = json.loads(session.get(Run, run.id).email)
    assert riassunto["to"] == ["capo@azienda.it"]
    assert riassunto["host"] == "smtp.azienda.it"
    assert riassunto["ok"] is None, "in attesa dell'esito dal worker"


async def test_every_send_is_audited_with_who_and_where(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)

    await _lancia(session, admin, flow, _corpo(conn.id, to=["capo@azienda.it", "cfo@azienda.it"]))

    riga = session.exec(select(AuditLog).where(AuditLog.action == audit.EMAIL_SEND)).one()
    assert riga.actor_label == "a@x.local"
    assert riga.target_label == "f"
    dettaglio = json.loads(riga.detail)
    assert dettaglio["to"] == ["capo@azienda.it", "cfo@azienda.it"]
    assert dettaglio["host"] == "smtp.azienda.it"


# ── 5. prova a vuoto: il destinatario lo impone il server ───────────────────
async def test_the_dry_run_sends_only_to_the_caller(session, fake_engine):
    """Se il client potesse scegliere il destinatario, questa rotta sarebbe il
    modo più comodo per aggirare i domini ammessi."""
    from app.routes.runs import launch_email_test

    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id, domini=["azienda.it"])

    body = _corpo(conn.id, to=["capo@azienda.it"], cc=["cfo@azienda.it"])
    await launch_email_test(flow.id, body, user=admin, session=session)

    inviato = fake_engine.transforms[0]["email"]
    assert inviato["target"]["to"] == ["a@x.local"]
    assert inviato["target"]["cc"] == []


async def test_the_dry_run_ignores_the_allowed_domains(session, fake_engine):
    """Mandare a sé stessi non è esfiltrazione: coi domini ristretti ai clienti,
    altrimenti il pulsante di prova non funzionerebbe mai."""
    from app.routes.runs import launch_email_test

    tizio = make_user(session, email="tizio@gmail.com", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id, domini=["azienda.it"])

    await launch_email_test(flow.id, _corpo(conn.id, to=["capo@azienda.it"]), user=tizio, session=session)
    assert fake_engine.transforms[0]["email"]["target"]["to"] == ["tizio@gmail.com"]


async def test_the_dry_run_never_fails_the_run_and_writes_nothing(session, fake_engine):
    from app.routes.runs import launch_email_test

    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)

    body = _corpo(conn.id, to=["capo@azienda.it"], stop_on_failure=True)
    await launch_email_test(flow.id, body, user=admin, session=session)

    corpo = fake_engine.transforms[0]
    assert corpo["email"]["stop_on_failure"] is False  # una prova non fa fallire nulla
    assert corpo["destination"] is None and corpo["mirror"] is None


async def test_the_dry_run_is_audited_as_such(session, fake_engine):
    """Deve restare distinguibile da un invio vero in cronologia."""
    from app.routes.runs import launch_email_test

    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id)

    await launch_email_test(flow.id, _corpo(conn.id, to=["capo@azienda.it"]), user=admin, session=session)

    riga = session.exec(select(AuditLog).where(AuditLog.action == audit.EMAIL_SEND)).one()
    dettaglio = json.loads(riga.detail)
    assert dettaglio["trigger"] == "dry_run"
    assert dettaglio["to"] == ["a@x.local"]


async def test_the_dry_run_needs_an_email_node(session, fake_engine):
    from app.routes.runs import launch_email_test

    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    senza = RunCreate(bucket="data-prep", input_key="datasets/x.parquet", operations=[])
    with pytest.raises(HTTPException) as e:
        await launch_email_test(flow.id, senza, user=admin, session=session)
    assert e.value.status_code == 422


async def test_a_refused_send_leaves_no_audit_trail_of_a_send(session, fake_engine):
    """Il rifiuto avviene PRIMA: non deve sembrare che qualcosa sia uscito."""
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    conn = _smtp(session, p.id, domini=["azienda.it"])

    with pytest.raises(HTTPException):
        await _lancia(session, admin, flow, _corpo(conn.id, to=["tizio@gmail.com"]))

    assert session.exec(select(AuditLog).where(AuditLog.action == audit.EMAIL_SEND)).all() == []
