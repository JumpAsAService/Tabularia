"""Webhooks: the event is born with the run's closing, goes out signed, is retried, and
does not go where it must not.

The rules these tests pin: the delivery goes into the SAME transaction that closes the
run (the two paths: a task's reconciliation and an orchestration's closing); a fault
preparing the event does not prevent closing the run; the signature verifies with the
secret; a 5xx is retried, a forbidden address is not; only the administrator creates
and sees secrets and addresses.
"""
import hashlib
import hmac
import ipaddress
import json
from datetime import timedelta

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db.session import get_session
from app.models import Project, Run, Webhook, WebhookDelivery
from app.routes import webhooks as webhooks_routes
from app.services import webhooks as svc
from tests.conftest import make_datasource, make_flow, make_project, make_run, make_user

CSRF = {"X-Tabularia-Csrf": "1"}
EVERYONE = list(svc.EVENTS)


@pytest.fixture(autouse=True)
def one_write_at_a_time(monkeypatch):
    """The tests use in-memory SQLite with ONE shared connection (StaticPool), and the
    sending reads and writes from several threads together: two outcomes recorded at the
    same instant trod on each other (about one round in three). In production every
    thread has its own connection from Postgres's pool; here they go in line."""
    import threading

    line = threading.Lock()
    for name in ("_take_due", "_record"):
        vera = getattr(svc, name)

        def in_line(*a, _vera=vera, **k):
            with line:
                return _vera(*a, **k)
        monkeypatch.setattr(svc, name, in_line)


def _webhook(session, *, events=None, folder=None, active=True, url="http://ricevente.example/hook") -> tuple[Webhook, str]:
    secret = svc.new_secret()
    w = Webhook(name="w", url=url, secret_enc=svc.encrypt(secret), events=json.dumps(events or EVERYONE),
                project_id=folder, active=active, created_at=svc.now())
    session.add(w)
    session.commit()
    session.refresh(w)
    return w, secret


def _deliveries(session) -> list[WebhookDelivery]:
    session.expire_all()
    return list(session.exec(select(WebhookDelivery).order_by(WebhookDelivery.id)).all())


def _close(session, run: Run, state: str) -> int:
    """What the two closing paths do: new state, deliveries, one commit."""
    run.status, run.finished_at = state, svc.now()
    session.add(run)
    session.flush()
    n = svc.enqueue_for_run(session, run)
    session.commit()
    return n


# ── which events, for whom ─────────────────────────────────────────────────

def test_a_finished_flow_becomes_an_event_with_flow_folder_and_run(session):
    user_ = make_user(session, email="a@x.local")
    folder = make_project(session, name="Vendite", owner_id=user_.id)
    flow = make_flow(session, name="Margini", project_id=folder.id, owner_id=user_.id)
    _webhook(session)
    run = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id, rows_written=12)
    assert _close(session, run, "SUCCESS") == 1
    d = _deliveries(session)[0]
    body = json.loads(d.payload)
    assert d.event == body["event"] == "flow.run.succeeded"
    assert body["data"]["flow"] == {"id": flow.id, "name": "Margini"}
    assert body["data"]["folder"] == {"id": folder.id, "name": "Vendite"}
    assert body["data"]["run"]["id"] == run.id and body["data"]["run"]["status"] == "SUCCESS"


def test_the_kinds_of_run(session):
    user_ = make_user(session, email="a@x.local")
    folder = make_project(session, name="C", owner_id=user_.id)
    flow = make_flow(session, name="F", project_id=folder.id, owner_id=user_.id)
    ds = make_datasource(session, name="clienti", project_id=folder.id)
    padre = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id)
    cases = {
        "flow.run.failed": make_run(session, kind="flow", status="STARTED", task_id="t1", flow_id=flow.id),
        "datasource.refresh.succeeded": make_run(session, kind="ingest", status="STARTED", task_id="t2", datasource_id=ds.id),
        None: make_run(session, kind="flow", status="STARTED", task_id="t3", flow_id=flow.id, parent_run_id=padre.id),
    }
    cases["flow.run.failed"].status = "FAILURE"
    cases["datasource.refresh.succeeded"].status = "SUCCESS"
    cases[None].status = "SUCCESS"
    for expected, run in cases.items():
        assert svc.run_event(run) == expected
    run = cases["datasource.refresh.succeeded"]
    _webhook(session)
    _close(session, run, "SUCCESS")
    assert json.loads(_deliveries(session)[0].payload)["data"]["datasource"]["name"] == "clienti"


def test_events_not_chosen_webhooks_off_and_other_folders_receive_nothing(session):
    user_ = make_user(session, email="a@x.local")
    padre = make_project(session, name="Vendite", owner_id=user_.id)
    child_folder = make_project(session, name="Nord", owner_id=user_.id, parent_id=padre.id)
    other_one = make_project(session, name="Acquisti", owner_id=user_.id)
    flow = make_flow(session, name="F", project_id=child_folder.id, owner_id=user_.id)
    failures_only, _ = _webhook(session, events=["flow.run.failed"])
    off, _ = _webhook(session, active=False)
    of_the_parent, _ = _webhook(session, folder=padre.id)
    of_another, _ = _webhook(session, folder=other_one.id)
    run = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id)
    _close(session, run, "SUCCESS")
    # only the parent folder's: a subfolder is inside
    assert [d.webhook_id for d in _deliveries(session)] == [of_the_parent.id]


def test_a_fault_preparing_the_event_does_not_prevent_closing_the_run(session, monkeypatch):
    user_ = make_user(session, email="a@x.local")
    flow = make_flow(session, name="F", project_id=make_project(session, name="C", owner_id=user_.id).id, owner_id=user_.id)
    _webhook(session)
    monkeypatch.setattr(svc, "_run_data", lambda *a: (_ for _ in ()).throw(RuntimeError("guasto")))
    run = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id)
    assert _close(session, run, "FAILURE") == 0
    session.refresh(run)
    assert run.status == "FAILURE" and _deliveries(session) == []




@pytest.mark.anyio
async def test_a_tasks_reconciliation_enqueues_in_the_same_transaction(session, fake_engine):
    from app.routes.runs import _reconcile

    ds = make_datasource(session, name="live", kind="database", key="datasets/s1.parquet")
    _webhook(session, events=["datasource.refresh.succeeded"])
    run = make_run(session, kind="ingest", status="STARTED", task_id="task-ing", datasource_id=ds.id, output_key="datasets/s2.parquet")
    fake_engine.set_task("task-ing", "SUCCESS", result={"bucket": "data-prep", "rows_written": 3, "columns": []})
    await _reconcile(session, run)
    d = _deliveries(session)
    assert len(d) == 1 and d[0].event == "datasource.refresh.succeeded"
    # a second reconciliation of the run closed already does not repeat the event
    await _reconcile(session, run)
    assert len(_deliveries(session)) == 1


@pytest.mark.anyio
async def test_an_orchestrations_closing_enqueues(db_engine, session, monkeypatch):
    from app.services import orchestrator as orch

    monkeypatch.setattr(orch, "engine", db_engine)
    user_ = make_user(session, email="a@x.local")
    flow = make_flow(session, name="F", project_id=make_project(session, name="C", owner_id=user_.id).id, owner_id=user_.id)
    _webhook(session)
    run = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id)
    await orch._finalize_orch_run(run.id, "FAILURE", "boom")
    d = _deliveries(session)
    assert len(d) == 1 and d[0].event == "flow.run.failed" and json.loads(d[0].payload)["data"]["run"]["error"] == "boom"
    await orch._finalize_orch_run(run.id, "SUCCESS", None)  # closed already: nothing
    assert len(_deliveries(session)) == 1


# ── sending ─────────────────────────────────────────────────────────────────

def _client(answers: list[int], received: list[httpx.Request]) -> httpx.AsyncClient:
    def handle(req: httpx.Request) -> httpx.Response:
        received.append(req)
        return httpx.Response(answers.pop(0) if answers else 200)
    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


@pytest.fixture
def public_addresses(monkeypatch):
    """Every name resolves to a public address (the tests do no DNS)."""
    async def resolve(url):
        u = httpx.URL(url)
        return u, u.raw_host.decode(), "93.184.216.34"
    monkeypatch.setattr(svc, "_resolve", resolve)


@pytest.mark.anyio
async def test_goes_out_signed_and_the_signature_verifies(db_engine, session, public_addresses):
    w, secret = _webhook(session)
    svc.enqueue_ping(session, w)
    received: list[httpx.Request] = []
    assert await svc.deliver_due(db_engine, _client([200], received)) == 1
    req = received[0]
    body = req.content.decode()
    t, v1 = (dict(p.split("=", 1) for p in req.headers["X-Tabularia-Signature"].split(","))[k] for k in ("t", "v1"))
    assert hmac.compare_digest(v1, hmac.new(secret.encode(), f"{t}.{body}".encode(), hashlib.sha256).hexdigest())
    assert req.headers["X-Tabularia-Event"] == "ping" and req.headers["X-Tabularia-Delivery"] == json.loads(body)["id"]
    d = _deliveries(session)[0]
    assert d.status == "delivered" and d.last_status_code == 200 and d.attempts == 1


@pytest.mark.anyio
async def test_a_5xx_is_retried_and_gives_up_after_the_last_attempt(db_engine, session, public_addresses):
    w, _ = _webhook(session)
    svc.enqueue_ping(session, w)
    await svc.deliver_due(db_engine, _client([503], []))
    d = _deliveries(session)[0]
    assert d.status == "pending" and d.attempts == 1 and d.last_status_code == 503
    assert timedelta(seconds=25) < d.next_attempt_at - svc.now() <= timedelta(seconds=30)
    # all the attempts in vain
    for _ in range(svc.MAX_ATTEMPTS - 1):
        d = _deliveries(session)[0]
        d.next_attempt_at = svc.now()
        session.add(d)
        session.commit()
        await svc.deliver_due(db_engine, _client([500], []))
    d = _deliveries(session)[0]
    assert d.status == "failed" and d.attempts == svc.MAX_ATTEMPTS


@pytest.mark.anyio
async def test_a_delivery_taken_by_a_dead_process_goes_back_in_the_queue(db_engine, session, public_addresses):
    w, _ = _webhook(session)
    d = svc.enqueue_ping(session, w)
    d.status, d.claimed_at = "sending", svc.now() - svc.RECLAIM_AFTER - timedelta(seconds=1)
    session.add(d)
    session.commit()
    await svc.deliver_due(db_engine, _client([200], []))
    assert _deliveries(session)[0].status == "delivered"


@pytest.mark.anyio
async def test_a_webhook_off_does_not_send_what_it_had_queued(db_engine, session, public_addresses):
    w, _ = _webhook(session)
    svc.enqueue_ping(session, w)
    w.active = False
    session.add(w)
    session.commit()
    received: list[httpx.Request] = []
    await svc.deliver_due(db_engine, _client([200], received))
    assert received == [] and _deliveries(session)[0].status == "failed"


@pytest.mark.anyio
async def test_a_forbidden_address_is_not_called_nor_retried(db_engine, session):
    w, _ = _webhook(session, url="http://127.0.0.1:9/x")
    svc.enqueue_ping(session, w)
    received: list[httpx.Request] = []
    await svc.deliver_due(db_engine, _client([200], received))
    d = _deliveries(session)[0]
    assert received == [] and d.status == "failed" and "not allowed" in d.last_error


@pytest.mark.parametrize("ip,forbidden", [
    ("127.0.0.1", True), ("::1", True), ("169.254.169.254", True), ("0.0.0.0", True),
    ("::ffff:127.0.0.1", True), ("224.0.0.1", True), ("fe80::1", True),
    ("100.100.100.200", True), ("fd00:ec2::254", True), ("168.63.129.16", True),  # the clouds' metadata
    ("2002:7f00:1::", True), ("2002:a9fe:a9fe::", True), ("2002:5db8:d822::", False),  # 6to4: the IPv4 inside
    ("10.0.0.5", False), ("172.22.0.19", False), ("192.168.1.10", False), ("8.8.8.8", False),
])
def test_which_addresses_are_forbidden(ip, forbidden):
    assert svc._forbidden_ip(ipaddress.ip_address(ip)) is forbidden


@pytest.mark.parametrize("url", [
    "ftp://host/x", "http://", "file:///etc/passwd", "javascript:alert(1)",
    # what httpx or the resolver cannot read (found by trying it live)
    "http://0177.0.0.1/", "http://a..b/x", "http://" + "a" * 70 + ".com/", "http://ricevente.example:99999/",
])
def test_http_addresses_only(url):
    with pytest.raises(ValueError):
        svc.validate_url(url)


# ── the routes ──────────────────────────────────────────────────────────────

@pytest.fixture
def app(session):
    from app.routes import auth as auth_routes

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(webhooks_routes.router)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _sign_in(app, session, email, **kw) -> TestClient:
    from app.core.security import hash_password

    make_user(session, email=email, hashed_password=hash_password("una-password-lunga"), **kw)
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": "una-password-lunga"}, headers=CSRF).status_code == 200
    return c


def test_the_administrator_creates_and_the_secret_is_seen_twice_only(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    r = boss.post("/webhooks", json={"name": "Airflow", "url": "https://airflow.interno/hook", "events": ["flow.run.failed"]}, headers=CSRF)
    assert r.status_code == 201 and r.json()["secret"].startswith("whsec_")
    wid = r.json()["id"]
    assert "secret" not in boss.get("/webhooks").json()[0]
    new = boss.post(f"/webhooks/{wid}/rotate-secret", headers=CSRF).json()["secret"]
    assert new != r.json()["secret"]
    from app.core.crypto import decrypt_secret

    session.expire_all()
    assert decrypt_secret(session.get(Webhook, wid).secret_enc) == new


def test_unknown_events_and_wrong_addresses_are_refused(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    for body in ({"name": "x", "url": "https://a/b", "events": ["flow.run.esploso"]},
                  {"name": "x", "url": "https://a/b", "events": []},
                  {"name": "x", "url": "ftp://a/b", "events": ["flow.run.failed"]}):
        assert boss.post("/webhooks", json=body, headers=CSRF).status_code == 422


def test_the_observer_sees_that_they_exist_not_where_nor_what_they_send(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    wid = boss.post("/webhooks", json={"name": "Airflow", "url": "https://airflow.interno/hook?key=segreta", "events": ["flow.run.failed"]}, headers=CSRF).json()["id"]
    boss.post(f"/webhooks/{wid}/test", headers=CSRF)
    eye = _sign_in(app, session, "occhio@x.local", is_observer=True)
    seen = eye.get("/webhooks").json()[0]
    assert seen["url"] == "https://airflow.interno/…" and "secret" not in seen
    assert eye.get(f"/webhooks/{wid}/deliveries").json()[0]["payload"] is None
    assert eye.post("/webhooks", json={"name": "x", "url": "https://a/b", "events": ["flow.run.failed"]}, headers=CSRF).status_code == 403
    assert eye.post(f"/webhooks/{wid}/test", headers=CSRF).status_code == 403


def test_resending_creates_a_new_delivery_with_a_new_id(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    wid = boss.post("/webhooks", json={"name": "A", "url": "https://a/b", "events": ["flow.run.failed"]}, headers=CSRF).json()["id"]
    before = boss.post(f"/webhooks/{wid}/test", headers=CSRF).json()
    after = boss.post(f"/webhooks/deliveries/{before['id']}/redeliver", headers=CSRF).json()
    assert after["id"] != before["id"]
    a, b = (json.loads(session.get(WebhookDelivery, i).payload) for i in (before["id"], after["id"]))
    assert a["id"] != b["id"] and a["data"] == b["data"]


def test_removing_the_webhook_removes_its_deliveries(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    wid = boss.post("/webhooks", json={"name": "A", "url": "https://a/b", "events": ["flow.run.failed"]}, headers=CSRF).json()["id"]
    boss.post(f"/webhooks/{wid}/test", headers=CSRF)
    assert boss.delete(f"/webhooks/{wid}", headers=CSRF).status_code == 204
    assert _deliveries(session) == [] and session.get(Webhook, wid) is None


def test_the_cleanup_removes_the_old_log(session):
    w, _ = _webhook(session)
    old_one, recent, queued = (svc.enqueue_ping(session, w) for _ in range(3))
    old_one.status, old_one.created_at = "delivered", svc.now() - timedelta(days=31)
    recent.status = "delivered"
    queued.created_at = svc.now() - timedelta(days=31)  # still to send: it stays
    session.add_all([old_one, recent, queued])
    session.commit()
    assert svc.clean(session) == 1
    assert {d.id for d in _deliveries(session)} == {recent.id, queued.id}


# ── from the security review ─────────────────────────────────────────────────

@pytest.mark.anyio
async def test_the_checked_address_is_called_with_the_name_in_host(db_engine, session, public_addresses):
    """The name is resolved once: the connection goes to the checked address, not to the one
    DNS would give the second time."""
    w, _ = _webhook(session, url="https://ricevente.example:8443/hook")
    svc.enqueue_ping(session, w)
    received: list[httpx.Request] = []
    await svc.deliver_due(db_engine, _client([200], received))
    req = received[0]
    assert req.url.host == "93.184.216.34" and req.url.port == 8443
    assert req.headers["Host"] == "ricevente.example:8443"
    assert req.extensions.get("sni_hostname") == "ricevente.example"


@pytest.mark.anyio
async def test_international_names_are_checked_as_httpx_encodes_them(monkeypatch):
    asked = []

    async def getaddrinfo(host, port, **kw):
        asked.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    import asyncio
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    await svc._resolve("https://straße.de/x")
    assert asked == ["xn--strae-oqa.de"]


@pytest.mark.anyio
async def test_a_name_that_does_not_resolve_is_retried(db_engine, session, monkeypatch):
    async def failing(url):
        raise svc._Unresolved("name not resolved: Temporary failure in name resolution")
    monkeypatch.setattr(svc, "_resolve", failing)
    w, _ = _webhook(session)
    svc.enqueue_ping(session, w)
    await svc.deliver_due(db_engine, _client([200], []))
    d = _deliveries(session)[0]
    assert d.status == "pending" and d.attempts == 1 and "not resolved" in d.last_error


@pytest.mark.anyio
async def test_a_receiver_that_never_finishes_answering_does_not_hold_the_delivery(db_engine, session, public_addresses, monkeypatch):
    import asyncio

    monkeypatch.setenv("WEBHOOKS__TIMEOUT_SECONDS", "0.1")
    from app.core.config import get_settings
    get_settings.cache_clear()

    async def slow(req):
        await asyncio.sleep(5)
        return httpx.Response(200)

    w, _ = _webhook(session)
    svc.enqueue_ping(session, w)
    await svc.deliver_due(db_engine, httpx.AsyncClient(transport=httpx.MockTransport(slow)))
    d = _deliveries(session)[0]
    assert d.status == "pending" and "in time" in d.last_error


@pytest.mark.anyio
async def test_only_the_start_of_the_answer_is_read(db_engine, session, public_addresses):
    def huge(req):
        return httpx.Response(500, content=b"x" * 10_000_000)
    w, _ = _webhook(session)
    svc.enqueue_ping(session, w)
    await svc.deliver_due(db_engine, httpx.AsyncClient(transport=httpx.MockTransport(huge)))
    d = _deliveries(session)[0]
    assert d.last_status_code == 500 and len(d.last_error) < 400


@pytest.mark.anyio
async def test_a_put_off_delivery_is_not_taken_before_its_time(db_engine, session, public_addresses):
    w, _ = _webhook(session)
    d = svc.enqueue_ping(session, w)
    d.next_attempt_at = svc.now() + timedelta(minutes=5)
    session.add(d)
    session.commit()
    received: list[httpx.Request] = []
    await svc.deliver_due(db_engine, _client([200], received))
    assert received == [] and _deliveries(session)[0].status == "pending"


def test_the_outcome_is_written_only_by_whoever_still_has_the_delivery(session):
    """A late process does not overwrite whoever took it again later."""
    w, _ = _webhook(session)
    d = svc.enqueue_ping(session, w)
    d.status, d.claimed_at = "sending", svc.now()
    session.add(d)
    session.commit()
    svc._close_delivery(session, d.id, svc.now() - timedelta(minutes=10), status="delivered")
    assert _deliveries(session)[0].status == "sending"


def test_a_webhook_removed_while_a_run_closes_does_not_break_the_closing(session):
    """The deliveries have no foreign key towards the webhooks: a webhook removed between the
    read and the commit does not fail the run's closing."""
    user_ = make_user(session, email="a@x.local")
    flow = make_flow(session, name="F", project_id=make_project(session, name="C", owner_id=user_.id).id, owner_id=user_.id)
    w, _ = _webhook(session)
    run = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id)
    run.status, run.finished_at = "FAILURE", svc.now()
    session.add(run)
    session.flush()
    svc.enqueue_for_run(session, run)
    session.delete(session.get(Webhook, w.id))
    session.commit()
    assert session.get(Run, run.id).status == "FAILURE"


@pytest.mark.anyio
async def test_the_flows_other_closings_send_the_event(db_engine, session, monkeypatch):
    """Orphaned (process dead), closed at shutdown, left queued too long: exactly the failures
    an external system must know about."""
    from app.routes.runs import close_if_queued_too_long
    from app.services import orchestrator as orch

    monkeypatch.setattr(orch, "engine", db_engine)
    user_ = make_user(session, email="a@x.local")
    flow = make_flow(session, name="F", project_id=make_project(session, name="C", owner_id=user_.id).id, owner_id=user_.id)
    _webhook(session, events=["flow.run.failed"])
    orphan = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id)
    await orch.close_orphans()
    mia = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id, claimed_by=orch.INSTANCE)
    orch.close_mine(only_earlier=False)
    queued = make_run(session, kind="orchestration", status="PENDING", task_id="", flow_id=flow.id,
                       started_at=svc.now() - timedelta(days=1))
    await close_if_queued_too_long(session, queued)
    events = [json.loads(d.payload)["data"]["run"]["id"] for d in _deliveries(session)]
    assert sorted(events) == sorted([orphan.id, mia.id, queued.id])


def test_to_the_observer_the_log_does_not_say_the_receivers_error(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    wid = boss.post("/webhooks", json={"name": "A", "url": "https://a/b", "events": ["flow.run.failed"]}, headers=CSRF).json()["id"]
    did = boss.post(f"/webhooks/{wid}/test", headers=CSRF).json()["id"]
    d = session.get(WebhookDelivery, did)
    d.last_error = "response 500: segreti del ricevente 10.1.2.3"
    session.add(d)
    session.commit()
    eye = _sign_in(app, session, "occhio@x.local", is_observer=True)
    assert eye.get(f"/webhooks/{wid}/deliveries").json()[0]["last_error"] is None
    assert boss.get(f"/webhooks/{wid}/deliveries").json()[0]["last_error"].startswith("response 500")


def test_a_webhooks_folder_is_not_removed_silently(app, session):
    from app.routes import projects as projects_routes

    app.include_router(projects_routes.router)
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    folder = boss.post("/projects", json={"name": "Osservata"}, headers=CSRF).json()["id"]
    boss.post("/webhooks", json={"name": "Airflow", "url": "https://a/b", "events": ["flow.run.failed"], "project_id": folder}, headers=CSRF)
    r = boss.delete(f"/projects/{folder}", headers=CSRF)
    assert r.status_code == 409 and "Airflow" in r.json()["detail"]


# ── found by trying it live (2026-10-12) ─────────────────────────────────────

def _certificati(tmp_path, name):
    """A test CA and a certificate for `name` signed by it."""
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    def emetti(subject, key, emittente, issuer_key, ca, san=None):
        b = (x509.CertificateBuilder().subject_name(subject).issuer_name(emittente).public_key(key.public_key())
             .serial_number(x509.random_serial_number())
             .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1))
             .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
             .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
        if san:
            b = b.add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
        return b.sign(issuer_key, hashes.SHA256())

    k_ca, k = ec.generate_private_key(ec.SECP256R1()), ec.generate_private_key(ec.SECP256R1())
    n_ca = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "CA di prova")])
    ca = emetti(n_ca, k_ca, n_ca, k_ca, True)
    cert = emetti(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, name)]), k, n_ca, k_ca, False, san=name)
    pem = lambda c: c.public_bytes(serialization.Encoding.PEM)
    (tmp_path / "ca.pem").write_bytes(pem(ca))
    (tmp_path / "cert.pem").write_bytes(pem(cert))
    (tmp_path / "key.pem").write_bytes(k.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    return tmp_path / "ca.pem", tmp_path / "cert.pem", tmp_path / "key.pem"


@pytest.mark.anyio
async def test_a_name_without_certificate_does_not_ride_another_names_connection(db_engine, session, tmp_path, monkeypatch):
    """Two names, one address: the server has the certificate of a only. With a pool shared
    among the deliveries, the call for c travelled on the TLS connection checked for a and
    got through (tried live). Every delivery has a client of its own: c fails on the
    certificate, and every request arrives with ITS name in TLS."""
    import asyncio
    import ssl

    ca, cert, key = _certificati(tmp_path, "a.test")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    svc._tls_context.cache_clear()
    monkeypatch.setattr(svc, "PARALLEL", 1)  # in line: the second would find the first's connection free

    views = []
    server_side = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    server_side.load_cert_chain(cert, key)
    server_side.sni_callback = lambda sock, name, _c: setattr(sock, "sni", name)

    async def serve(reader, writer):
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                rows = head.decode().split("\r\n")
                h = {k.lower(): v.strip() for k, _, v in (r.partition(":") for r in rows[1:] if r)}
                await reader.readexactly(int(h.get("content-length", 0)))
                views.append((getattr(writer.get_extra_info("ssl_object"), "sni", None), h["host"], rows[0].split()[1]))
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError, ssl.SSLError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0, ssl=server_side)
    port = server.sockets[0].getsockname()[1]

    async def resolve(url):  # both names to the same address (the test server's)
        u = httpx.URL(url)
        return u, u.raw_host.decode(), "127.0.0.1"
    monkeypatch.setattr(svc, "_resolve", resolve)

    try:
        a, _ = _webhook(session, url=f"https://a.test:{port}/a")
        c, _ = _webhook(session, url=f"https://c.test:{port}/c")
        for w in (a, a, c, a):
            svc.enqueue_ping(session, w)
        await svc.deliver_due(db_engine)
    finally:
        server.close()
    outcomes = {d.id: d for d in _deliveries(session)}
    for_c = [d for d in outcomes.values() if d.webhook_id == c.id]
    assert for_c[0].status == "pending" and "CERTIFICATE_VERIFY_FAILED" in for_c[0].last_error
    assert all(d.status == "delivered" for d in outcomes.values() if d.webhook_id == a.id)
    assert ("c.test" not in [h.split(":")[0] for _, h, _ in views]), views
    assert all(sni == "a.test" and host.startswith("a.test:") for sni, host, _ in views), views


@pytest.mark.anyio
async def test_a_receiver_not_answering_does_not_hold_the_others_back(db_engine, session, public_addresses):
    """Forty deliveries queued towards a stuck receiver and one for another: live, the
    healthy one arrived after 66 s (every round took the 20 oldest and waited for them
    all). Now a webhook takes at most PER_WEBHOOK slots."""
    import asyncio

    dead, _ = _webhook(session, url="http://morto.example/x")
    healthy, _ = _webhook(session, url="http://sano.example/x")
    for _ in range(40):
        svc.enqueue_ping(session, dead)
    svc.enqueue_ping(session, healthy)
    still = asyncio.Event()
    arrived = []

    async def answer(req):
        if req.headers["Host"].startswith("morto"):
            await still.wait()
        arrived.append(req.headers["Host"])
        return httpx.Response(200)

    flight = svc._InFlight(db_engine, httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    await flight.round_()
    for _ in range(100):
        if "sano.example" in arrived:
            break
        await asyncio.sleep(0.02)
    assert arrived == ["sano.example"]
    assert sorted(flight.tasks.values()) == [dead.id] * svc.PER_WEBHOOK
    still.set()
    await flight.finish()


@pytest.mark.anyio
async def test_a_round_does_not_wait_for_the_deliveries_still_in_flight(db_engine, session, public_addresses):
    import asyncio

    slow, _ = _webhook(session, url="http://lento.example/x")
    svc.enqueue_ping(session, slow)
    still = asyncio.Event()

    async def answer(req):
        if req.headers["Host"].startswith("lento"):
            await still.wait()
        return httpx.Response(200)

    flight = svc._InFlight(db_engine, httpx.AsyncClient(transport=httpx.MockTransport(answer)))
    assert await flight.round_() == 1
    other, _ = _webhook(session, url="http://altro.example/x")
    svc.enqueue_ping(session, other)
    assert await flight.round_() == 1  # the slow one is still in flight, and the next round starts all the same
    await asyncio.sleep(0.1)
    states = {d.webhook_id: d.status for d in _deliveries(session)}
    assert states == {slow.id: "sending", other.id: "delivered"}
    still.set()
    await flight.finish()
    assert {d.status for d in _deliveries(session)} == {"delivered"}


@pytest.mark.anyio
async def test_an_address_httpx_cannot_read_closes_the_delivery_and_does_not_stop_the_others(db_engine, session):
    """Saved before the check (or read differently by a new httpx version): before, the
    exception left the round, the delivery stayed taken forever and the others of the
    batch lost their client halfway."""
    bad, _ = _webhook(session, url="http://0177.0.0.1/x")
    good, _ = _webhook(session, url="http://93.184.216.34/x")
    svc.enqueue_ping(session, bad)
    svc.enqueue_ping(session, good)
    await svc.deliver_due(db_engine, _client([200], []))
    states = {d.webhook_id: (d.status, d.last_error) for d in _deliveries(session)}
    assert states[bad.id][0] == "failed" and "invalid" in states[bad.id][1]
    assert states[good.id] == ("delivered", None)


@pytest.mark.anyio
async def test_an_unexpected_error_is_that_deliverys_outcome(db_engine, session, public_addresses, monkeypatch):
    """A fault outside the HTTP call (there the errors are an outcome already)."""
    broken, _ = _webhook(session, url="http://rotto.example/x")
    good, _ = _webhook(session, url="http://buono.example/x")
    svc.enqueue_ping(session, broken)
    svc.enqueue_ping(session, good)
    vera = svc._ship

    async def send(job, client):
        if "rotto" in job.url:
            raise RuntimeError("guasto che nessuno si aspetta")
        return await vera(job, client)
    monkeypatch.setattr(svc, "_ship", send)

    await svc.deliver_due(db_engine, _client([200], []))
    states = {d.webhook_id: d for d in _deliveries(session)}
    assert states[broken.id].status == "pending" and states[broken.id].attempts == 1  # retried, with the waits
    assert "RuntimeError" in states[broken.id].last_error and "guasto" not in states[broken.id].last_error
    assert states[good.id].status == "delivered"


@pytest.mark.anyio
async def test_at_shutdown_the_deliveries_in_flight_stay_taken(db_engine, session, public_addresses):
    """Stopped halfway, they go back in the queue after RECLAIM_AFTER: at least once."""
    import asyncio

    w, _ = _webhook(session)
    svc.enqueue_ping(session, w)

    async def mai(req):
        await asyncio.sleep(60)

    flight = svc._InFlight(db_engine, httpx.AsyncClient(transport=httpx.MockTransport(mai)))
    await flight.round_()
    await flight.finish(0.1)
    assert _deliveries(session)[0].status == "sending" and not flight.tasks


def test_the_invalid_address_is_refused_at_saving(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    for url in ("http://0177.0.0.1/", "http://a..b/", "http://x.example:70000/"):
        r = boss.post("/webhooks", json={"name": "A", "url": url, "events": ["flow.run.failed"]}, headers=CSRF)
        assert r.status_code == 422, (url, r.text)


@pytest.mark.anyio
async def test_a_long_queue_towards_a_fast_receiver_empties_at_once(db_engine, session, public_addresses, monkeypatch):
    """The real loop: it starts again as soon as a slot frees up. On the prodtest 300
    deliveries to a ready receiver went one per second (two in flight, then CYCLE still)."""
    import asyncio
    import time

    import app.db.session as db

    monkeypatch.setattr(db, "engine", db_engine)
    monkeypatch.setattr(svc, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    w, _ = _webhook(session)
    for _ in range(40):
        svc.enqueue_ping(session, w)
    stop = asyncio.Event()
    loop = asyncio.create_task(svc.delivery_loop(stop))
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10 and any(d.status != "delivered" for d in _deliveries(session)):
        await asyncio.sleep(0.05)
    duration = time.monotonic() - t0
    stop.set()
    await asyncio.wait_for(loop, 10)
    assert all(d.status == "delivered" for d in _deliveries(session))
    assert duration < svc.CYCLE * 2, duration  # before: 40 / PER_WEBHOOK rounds of CYCLE = 40 s
