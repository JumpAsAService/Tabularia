"""Webhook: l'evento nasce con la chiusura del run, parte firmato, si ritenta, e
non va dove non deve.

Le regole che questi test tengono ferme: la consegna entra nella STESSA
transazione che chiude il run (i due percorsi: la riconciliazione di un task e la
chiusura di un'orchestrazione); un guasto nel preparare l'evento non impedisce di
chiudere il run; la firma si verifica col segreto; un 5xx si ritenta, un
indirizzo vietato no; solo l'amministratore crea e vede segreti e indirizzi.
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
TUTTI = list(svc.EVENTS)


@pytest.fixture(autouse=True)
def una_scrittura_alla_volta(monkeypatch):
    """I test usano SQLite in memoria con UNA connessione condivisa (StaticPool),
    e la spedizione legge e scrive da più thread insieme: due esiti registrati
    nello stesso istante si pestavano (circa un giro su tre). In produzione ogni
    thread ha la sua connessione dal pool di Postgres; qui vanno in fila."""
    import threading

    fila = threading.Lock()
    for name in ("_take_due", "_record"):
        vera = getattr(svc, name)

        def in_fila(*a, _vera=vera, **k):
            with fila:
                return _vera(*a, **k)
        monkeypatch.setattr(svc, name, in_fila)


def _webhook(session, *, events=None, folder=None, attivo=True, url="http://ricevente.example/hook") -> tuple[Webhook, str]:
    secret = svc.new_secret()
    w = Webhook(name="w", url=url, secret_enc=svc.encrypt(secret), events=json.dumps(events or TUTTI),
                project_id=folder, active=attivo, created_at=svc.now())
    session.add(w)
    session.commit()
    session.refresh(w)
    return w, secret


def _consegne(session) -> list[WebhookDelivery]:
    session.expire_all()
    return list(session.exec(select(WebhookDelivery).order_by(WebhookDelivery.id)).all())


def _chiudi(session, run: Run, state: str) -> int:
    """Quello che fanno i due percorsi di chiusura: stato nuovo, consegne, un commit."""
    run.status, run.finished_at = state, svc.now()
    session.add(run)
    session.flush()
    n = svc.enqueue_for_run(session, run)
    session.commit()
    return n


# ── quali eventi, per chi ──────────────────────────────────────────────────

def test_un_flusso_finito_diventa_un_evento_con_flusso_cartella_e_run(session):
    utente = make_user(session, email="a@x.local")
    folder = make_project(session, name="Vendite", owner_id=utente.id)
    flusso = make_flow(session, name="Margini", project_id=folder.id, owner_id=utente.id)
    _webhook(session)
    run = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id, rows_written=12)
    assert _chiudi(session, run, "SUCCESS") == 1
    d = _consegne(session)[0]
    body = json.loads(d.payload)
    assert d.event == body["event"] == "flow.run.succeeded"
    assert body["data"]["flow"] == {"id": flusso.id, "name": "Margini"}
    assert body["data"]["folder"] == {"id": folder.id, "name": "Vendite"}
    assert body["data"]["run"]["id"] == run.id and body["data"]["run"]["status"] == "SUCCESS"


def test_i_tipi_di_run(session):
    utente = make_user(session, email="a@x.local")
    folder = make_project(session, name="C", owner_id=utente.id)
    flusso = make_flow(session, name="F", project_id=folder.id, owner_id=utente.id)
    ds = make_datasource(session, name="clienti", project_id=folder.id)
    padre = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id)
    casi = {
        "flow.run.failed": make_run(session, kind="flow", status="STARTED", task_id="t1", flow_id=flusso.id),
        "datasource.refresh.succeeded": make_run(session, kind="ingest", status="STARTED", task_id="t2", datasource_id=ds.id),
        None: make_run(session, kind="flow", status="STARTED", task_id="t3", flow_id=flusso.id, parent_run_id=padre.id),
    }
    casi["flow.run.failed"].status = "FAILURE"
    casi["datasource.refresh.succeeded"].status = "SUCCESS"
    casi[None].status = "SUCCESS"
    for atteso, run in casi.items():
        assert svc.run_event(run) == atteso
    run = casi["datasource.refresh.succeeded"]
    _webhook(session)
    _chiudi(session, run, "SUCCESS")
    assert json.loads(_consegne(session)[0].payload)["data"]["datasource"]["name"] == "clienti"


def test_eventi_non_scelti_webhook_spenti_e_altre_cartelle_non_ricevono(session):
    utente = make_user(session, email="a@x.local")
    padre = make_project(session, name="Vendite", owner_id=utente.id)
    figlia = make_project(session, name="Nord", owner_id=utente.id, parent_id=padre.id)
    other_one = make_project(session, name="Acquisti", owner_id=utente.id)
    flusso = make_flow(session, name="F", project_id=figlia.id, owner_id=utente.id)
    solo_fallimenti, _ = _webhook(session, events=["flow.run.failed"])
    spento, _ = _webhook(session, attivo=False)
    della_madre, _ = _webhook(session, folder=padre.id)
    di_un_altra, _ = _webhook(session, folder=other_one.id)
    run = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id)
    _chiudi(session, run, "SUCCESS")
    # solo quello della cartella madre: una sottocartella è dentro
    assert [d.webhook_id for d in _consegne(session)] == [della_madre.id]


def test_un_guasto_nel_preparare_l_evento_non_impedisce_di_chiudere_il_run(session, monkeypatch):
    utente = make_user(session, email="a@x.local")
    flusso = make_flow(session, name="F", project_id=make_project(session, name="C", owner_id=utente.id).id, owner_id=utente.id)
    _webhook(session)
    monkeypatch.setattr(svc, "_run_data", lambda *a: (_ for _ in ()).throw(RuntimeError("guasto")))
    run = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id)
    assert _chiudi(session, run, "FAILURE") == 0
    session.refresh(run)
    assert run.status == "FAILURE" and _consegne(session) == []




@pytest.mark.anyio
async def test_la_riconciliazione_di_un_task_accoda_nella_stessa_transazione(session, fake_engine):
    from app.routes.runs import _reconcile

    ds = make_datasource(session, name="live", kind="database", key="datasets/s1.parquet")
    _webhook(session, events=["datasource.refresh.succeeded"])
    run = make_run(session, kind="ingest", status="STARTED", task_id="task-ing", datasource_id=ds.id, output_key="datasets/s2.parquet")
    fake_engine.set_task("task-ing", "SUCCESS", result={"bucket": "data-prep", "rows_written": 3, "columns": []})
    await _reconcile(session, run)
    d = _consegne(session)
    assert len(d) == 1 and d[0].event == "datasource.refresh.succeeded"
    # una seconda riconciliazione del run già chiuso non ripete l'evento
    await _reconcile(session, run)
    assert len(_consegne(session)) == 1


@pytest.mark.anyio
async def test_la_chiusura_di_un_orchestrazione_accoda(db_engine, session, monkeypatch):
    from app.services import orchestrator as orch

    monkeypatch.setattr(orch, "engine", db_engine)
    utente = make_user(session, email="a@x.local")
    flusso = make_flow(session, name="F", project_id=make_project(session, name="C", owner_id=utente.id).id, owner_id=utente.id)
    _webhook(session)
    run = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id)
    await orch._finalize_orch_run(run.id, "FAILURE", "boom")
    d = _consegne(session)
    assert len(d) == 1 and d[0].event == "flow.run.failed" and json.loads(d[0].payload)["data"]["run"]["error"] == "boom"
    await orch._finalize_orch_run(run.id, "SUCCESS", None)  # già chiuso: niente
    assert len(_consegne(session)) == 1


# ── la spedizione ───────────────────────────────────────────────────────────

def _client(answers: list[int], ricevute: list[httpx.Request]) -> httpx.AsyncClient:
    def gestisci(req: httpx.Request) -> httpx.Response:
        ricevute.append(req)
        return httpx.Response(answers.pop(0) if answers else 200)
    return httpx.AsyncClient(transport=httpx.MockTransport(gestisci))


@pytest.fixture
def indirizzi_pubblici(monkeypatch):
    """Ogni nome risolve a un indirizzo pubblico (i test non fanno DNS)."""
    async def risolvi(url):
        u = httpx.URL(url)
        return u, u.raw_host.decode(), "93.184.216.34"
    monkeypatch.setattr(svc, "_resolve", risolvi)


@pytest.mark.anyio
async def test_parte_firmata_e_la_firma_si_verifica(db_engine, session, indirizzi_pubblici):
    w, secret = _webhook(session)
    svc.enqueue_ping(session, w)
    ricevute: list[httpx.Request] = []
    assert await svc.deliver_due(db_engine, _client([200], ricevute)) == 1
    req = ricevute[0]
    body = req.content.decode()
    t, v1 = (dict(p.split("=", 1) for p in req.headers["X-Tabularia-Signature"].split(","))[k] for k in ("t", "v1"))
    assert hmac.compare_digest(v1, hmac.new(secret.encode(), f"{t}.{body}".encode(), hashlib.sha256).hexdigest())
    assert req.headers["X-Tabularia-Event"] == "ping" and req.headers["X-Tabularia-Delivery"] == json.loads(body)["id"]
    d = _consegne(session)[0]
    assert d.status == "delivered" and d.last_status_code == 200 and d.attempts == 1


@pytest.mark.anyio
async def test_un_5xx_si_ritenta_e_dopo_l_ultimo_tentativo_si_arrende(db_engine, session, indirizzi_pubblici):
    w, _ = _webhook(session)
    svc.enqueue_ping(session, w)
    await svc.deliver_due(db_engine, _client([503], []))
    d = _consegne(session)[0]
    assert d.status == "pending" and d.attempts == 1 and d.last_status_code == 503
    assert timedelta(seconds=25) < d.next_attempt_at - svc.now() <= timedelta(seconds=30)
    # tutti i tentativi a vuoto
    for _ in range(svc.MAX_ATTEMPTS - 1):
        d = _consegne(session)[0]
        d.next_attempt_at = svc.now()
        session.add(d)
        session.commit()
        await svc.deliver_due(db_engine, _client([500], []))
    d = _consegne(session)[0]
    assert d.status == "failed" and d.attempts == svc.MAX_ATTEMPTS


@pytest.mark.anyio
async def test_una_consegna_presa_da_un_processo_morto_torna_in_coda(db_engine, session, indirizzi_pubblici):
    w, _ = _webhook(session)
    d = svc.enqueue_ping(session, w)
    d.status, d.claimed_at = "sending", svc.now() - svc.RECLAIM_AFTER - timedelta(seconds=1)
    session.add(d)
    session.commit()
    await svc.deliver_due(db_engine, _client([200], []))
    assert _consegne(session)[0].status == "delivered"


@pytest.mark.anyio
async def test_un_webhook_spento_non_spedisce_quel_che_aveva_in_coda(db_engine, session, indirizzi_pubblici):
    w, _ = _webhook(session)
    svc.enqueue_ping(session, w)
    w.active = False
    session.add(w)
    session.commit()
    ricevute: list[httpx.Request] = []
    await svc.deliver_due(db_engine, _client([200], ricevute))
    assert ricevute == [] and _consegne(session)[0].status == "failed"


@pytest.mark.anyio
async def test_un_indirizzo_vietato_non_si_chiama_e_non_si_ritenta(db_engine, session):
    w, _ = _webhook(session, url="http://127.0.0.1:9/x")
    svc.enqueue_ping(session, w)
    ricevute: list[httpx.Request] = []
    await svc.deliver_due(db_engine, _client([200], ricevute))
    d = _consegne(session)[0]
    assert ricevute == [] and d.status == "failed" and "not allowed" in d.last_error


@pytest.mark.parametrize("ip,vietato", [
    ("127.0.0.1", True), ("::1", True), ("169.254.169.254", True), ("0.0.0.0", True),
    ("::ffff:127.0.0.1", True), ("224.0.0.1", True), ("fe80::1", True),
    ("100.100.100.200", True), ("fd00:ec2::254", True), ("168.63.129.16", True),  # metadati dei cloud
    ("2002:7f00:1::", True), ("2002:a9fe:a9fe::", True), ("2002:5db8:d822::", False),  # 6to4: l'IPv4 dentro
    ("10.0.0.5", False), ("172.22.0.19", False), ("192.168.1.10", False), ("8.8.8.8", False),
])
def test_quali_indirizzi_sono_vietati(ip, vietato):
    assert svc._forbidden_ip(ipaddress.ip_address(ip)) is vietato


@pytest.mark.parametrize("url", [
    "ftp://host/x", "http://", "file:///etc/passwd", "javascript:alert(1)",
    # quel che httpx o il resolver non sanno leggere (trovati provando dal vivo)
    "http://0177.0.0.1/", "http://a..b/x", "http://" + "a" * 70 + ".com/", "http://ricevente.example:99999/",
])
def test_solo_indirizzi_http(url):
    with pytest.raises(ValueError):
        svc.validate_url(url)


# ── le rotte ────────────────────────────────────────────────────────────────

@pytest.fixture
def app(session):
    from app.routes import auth as auth_routes

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(webhooks_routes.router)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _entra(app, session, email, **kw) -> TestClient:
    from app.core.security import hash_password

    make_user(session, email=email, hashed_password=hash_password("una-password-lunga"), **kw)
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": "una-password-lunga"}, headers=CSRF).status_code == 200
    return c


def test_l_amministratore_crea_e_il_segreto_si_vede_due_volte_sole(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    r = capo.post("/webhooks", json={"name": "Airflow", "url": "https://airflow.interno/hook", "events": ["flow.run.failed"]}, headers=CSRF)
    assert r.status_code == 201 and r.json()["secret"].startswith("whsec_")
    wid = r.json()["id"]
    assert "secret" not in capo.get("/webhooks").json()[0]
    new = capo.post(f"/webhooks/{wid}/rotate-secret", headers=CSRF).json()["secret"]
    assert new != r.json()["secret"]
    from app.core.crypto import decrypt_secret

    session.expire_all()
    assert decrypt_secret(session.get(Webhook, wid).secret_enc) == new


def test_eventi_sconosciuti_e_indirizzi_sbagliati_sono_rifiutati(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    for body in ({"name": "x", "url": "https://a/b", "events": ["flow.run.esploso"]},
                  {"name": "x", "url": "https://a/b", "events": []},
                  {"name": "x", "url": "ftp://a/b", "events": ["flow.run.failed"]}):
        assert capo.post("/webhooks", json=body, headers=CSRF).status_code == 422


def test_l_osservatore_vede_che_esistono_non_dove_ne_cosa_mandano(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    wid = capo.post("/webhooks", json={"name": "Airflow", "url": "https://airflow.interno/hook?key=segreta", "events": ["flow.run.failed"]}, headers=CSRF).json()["id"]
    capo.post(f"/webhooks/{wid}/test", headers=CSRF)
    occhio = _entra(app, session, "occhio@x.local", is_observer=True)
    seen = occhio.get("/webhooks").json()[0]
    assert seen["url"] == "https://airflow.interno/…" and "secret" not in seen
    assert occhio.get(f"/webhooks/{wid}/deliveries").json()[0]["payload"] is None
    assert occhio.post("/webhooks", json={"name": "x", "url": "https://a/b", "events": ["flow.run.failed"]}, headers=CSRF).status_code == 403
    assert occhio.post(f"/webhooks/{wid}/test", headers=CSRF).status_code == 403


def test_rispedire_crea_una_consegna_nuova_con_un_id_nuovo(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    wid = capo.post("/webhooks", json={"name": "A", "url": "https://a/b", "events": ["flow.run.failed"]}, headers=CSRF).json()["id"]
    before = capo.post(f"/webhooks/{wid}/test", headers=CSRF).json()
    after = capo.post(f"/webhooks/deliveries/{before['id']}/redeliver", headers=CSRF).json()
    assert after["id"] != before["id"]
    a, b = (json.loads(session.get(WebhookDelivery, i).payload) for i in (before["id"], after["id"]))
    assert a["id"] != b["id"] and a["data"] == b["data"]


def test_togliere_il_webhook_toglie_le_sue_consegne(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    wid = capo.post("/webhooks", json={"name": "A", "url": "https://a/b", "events": ["flow.run.failed"]}, headers=CSRF).json()["id"]
    capo.post(f"/webhooks/{wid}/test", headers=CSRF)
    assert capo.delete(f"/webhooks/{wid}", headers=CSRF).status_code == 204
    assert _consegne(session) == [] and session.get(Webhook, wid) is None


def test_la_pulizia_toglie_il_registro_vecchio(session):
    w, _ = _webhook(session)
    vecchia, recente, in_coda = (svc.enqueue_ping(session, w) for _ in range(3))
    vecchia.status, vecchia.created_at = "delivered", svc.now() - timedelta(days=31)
    recente.status = "delivered"
    in_coda.created_at = svc.now() - timedelta(days=31)  # ancora da spedire: resta
    session.add_all([vecchia, recente, in_coda])
    session.commit()
    assert svc.clean(session) == 1
    assert {d.id for d in _consegne(session)} == {recente.id, in_coda.id}


# ── dalla revisione di sicurezza ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_si_chiama_l_indirizzo_verificato_col_nome_nell_host(db_engine, session, indirizzi_pubblici):
    """Il nome si risolve una volta: la connessione va all'indirizzo controllato,
    non a quello che il DNS darebbe la seconda volta."""
    w, _ = _webhook(session, url="https://ricevente.example:8443/hook")
    svc.enqueue_ping(session, w)
    ricevute: list[httpx.Request] = []
    await svc.deliver_due(db_engine, _client([200], ricevute))
    req = ricevute[0]
    assert req.url.host == "93.184.216.34" and req.url.port == 8443
    assert req.headers["Host"] == "ricevente.example:8443"
    assert req.extensions.get("sni_hostname") == "ricevente.example"


@pytest.mark.anyio
async def test_i_nomi_internazionali_si_controllano_come_li_codifica_httpx(monkeypatch):
    chiesti = []

    async def getaddrinfo(host, port, **kw):
        chiesti.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    import asyncio
    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", getaddrinfo)
    await svc._resolve("https://straße.de/x")
    assert chiesti == ["xn--strae-oqa.de"]


@pytest.mark.anyio
async def test_un_nome_che_non_si_risolve_si_ritenta(db_engine, session, monkeypatch):
    async def guasto(url):
        raise svc._Unresolved("name not resolved: Temporary failure in name resolution")
    monkeypatch.setattr(svc, "_resolve", guasto)
    w, _ = _webhook(session)
    svc.enqueue_ping(session, w)
    await svc.deliver_due(db_engine, _client([200], []))
    d = _consegne(session)[0]
    assert d.status == "pending" and d.attempts == 1 and "not resolved" in d.last_error


@pytest.mark.anyio
async def test_un_ricevente_che_non_finisce_di_rispondere_non_tiene_la_consegna(db_engine, session, indirizzi_pubblici, monkeypatch):
    import asyncio

    monkeypatch.setenv("WEBHOOKS__TIMEOUT_SECONDS", "0.1")
    from app.core.config import get_settings
    get_settings.cache_clear()

    async def lento(req):
        await asyncio.sleep(5)
        return httpx.Response(200)

    w, _ = _webhook(session)
    svc.enqueue_ping(session, w)
    await svc.deliver_due(db_engine, httpx.AsyncClient(transport=httpx.MockTransport(lento)))
    d = _consegne(session)[0]
    assert d.status == "pending" and "in time" in d.last_error


@pytest.mark.anyio
async def test_della_risposta_si_legge_solo_l_inizio(db_engine, session, indirizzi_pubblici):
    def enorme(req):
        return httpx.Response(500, content=b"x" * 10_000_000)
    w, _ = _webhook(session)
    svc.enqueue_ping(session, w)
    await svc.deliver_due(db_engine, httpx.AsyncClient(transport=httpx.MockTransport(enorme)))
    d = _consegne(session)[0]
    assert d.last_status_code == 500 and len(d.last_error) < 400


@pytest.mark.anyio
async def test_una_consegna_rimandata_non_si_prende_prima_del_tempo(db_engine, session, indirizzi_pubblici):
    w, _ = _webhook(session)
    d = svc.enqueue_ping(session, w)
    d.next_attempt_at = svc.now() + timedelta(minutes=5)
    session.add(d)
    session.commit()
    ricevute: list[httpx.Request] = []
    await svc.deliver_due(db_engine, _client([200], ricevute))
    assert ricevute == [] and _consegne(session)[0].status == "pending"


def test_l_esito_si_scrive_solo_da_chi_ha_ancora_la_consegna(session):
    """Un processo in ritardo non sovrascrive chi l'ha ripresa dopo."""
    w, _ = _webhook(session)
    d = svc.enqueue_ping(session, w)
    d.status, d.claimed_at = "sending", svc.now()
    session.add(d)
    session.commit()
    svc._close_delivery(session, d.id, svc.now() - timedelta(minutes=10), status="delivered")
    assert _consegne(session)[0].status == "sending"


def test_un_webhook_tolto_mentre_un_run_si_chiude_non_rompe_la_chiusura(session):
    """Le consegne non hanno chiave esterna verso i webhook: un webhook tolto fra la
    lettura e il commit non fa fallire la chiusura del run."""
    utente = make_user(session, email="a@x.local")
    flusso = make_flow(session, name="F", project_id=make_project(session, name="C", owner_id=utente.id).id, owner_id=utente.id)
    w, _ = _webhook(session)
    run = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id)
    run.status, run.finished_at = "FAILURE", svc.now()
    session.add(run)
    session.flush()
    svc.enqueue_for_run(session, run)
    session.delete(session.get(Webhook, w.id))
    session.commit()
    assert session.get(Run, run.id).status == "FAILURE"


@pytest.mark.anyio
async def test_le_altre_chiusure_dei_flussi_mandano_l_evento(db_engine, session, monkeypatch):
    """Orfane (processo morto), chiuse allo spegnimento, rimaste in coda troppo:
    sono proprio i fallimenti che un sistema esterno deve sapere."""
    from app.routes.runs import close_if_queued_too_long
    from app.services import orchestrator as orch

    monkeypatch.setattr(orch, "engine", db_engine)
    utente = make_user(session, email="a@x.local")
    flusso = make_flow(session, name="F", project_id=make_project(session, name="C", owner_id=utente.id).id, owner_id=utente.id)
    _webhook(session, events=["flow.run.failed"])
    orphan = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id)
    await orch.close_orphans()
    mia = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flusso.id, claimed_by=orch.INSTANCE)
    orch.close_mine(only_earlier=False)
    in_coda = make_run(session, kind="orchestration", status="PENDING", task_id="", flow_id=flusso.id,
                       started_at=svc.now() - timedelta(days=1))
    await close_if_queued_too_long(session, in_coda)
    events = [json.loads(d.payload)["data"]["run"]["id"] for d in _consegne(session)]
    assert sorted(events) == sorted([orphan.id, mia.id, in_coda.id])


def test_all_osservatore_il_registro_non_dice_l_errore_del_ricevente(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    wid = capo.post("/webhooks", json={"name": "A", "url": "https://a/b", "events": ["flow.run.failed"]}, headers=CSRF).json()["id"]
    did = capo.post(f"/webhooks/{wid}/test", headers=CSRF).json()["id"]
    d = session.get(WebhookDelivery, did)
    d.last_error = "response 500: segreti del ricevente 10.1.2.3"
    session.add(d)
    session.commit()
    occhio = _entra(app, session, "occhio@x.local", is_observer=True)
    assert occhio.get(f"/webhooks/{wid}/deliveries").json()[0]["last_error"] is None
    assert capo.get(f"/webhooks/{wid}/deliveries").json()[0]["last_error"].startswith("response 500")


def test_una_cartella_di_un_webhook_non_si_toglie_in_silenzio(app, session):
    from app.routes import projects as projects_routes

    app.include_router(projects_routes.router)
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    folder = capo.post("/projects", json={"name": "Osservata"}, headers=CSRF).json()["id"]
    capo.post("/webhooks", json={"name": "Airflow", "url": "https://a/b", "events": ["flow.run.failed"], "project_id": folder}, headers=CSRF)
    r = capo.delete(f"/projects/{folder}", headers=CSRF)
    assert r.status_code == 409 and "Airflow" in r.json()["detail"]


# ── trovati provando dal vivo (2026-10-12) ───────────────────────────────────

def _certificati(tmp_path, name):
    """Una CA di prova e un certificato per `name` firmato da lei."""
    import datetime as dt

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    def emetti(soggetto, key, emittente, chiave_emittente, ca, san=None):
        b = (x509.CertificateBuilder().subject_name(soggetto).issuer_name(emittente).public_key(key.public_key())
             .serial_number(x509.random_serial_number())
             .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1))
             .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
             .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True))
        if san:
            b = b.add_extension(x509.SubjectAlternativeName([x509.DNSName(san)]), critical=False)
        return b.sign(chiave_emittente, hashes.SHA256())

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
async def test_un_nome_senza_certificato_non_passa_sulla_connessione_di_un_altro(db_engine, session, tmp_path, monkeypatch):
    """Due nomi, un indirizzo: il server ha il certificato solo di a. Con un pool
    condiviso fra le consegne, la chiamata per c viaggiava sulla connessione TLS
    verificata per a e arrivava (provato dal vivo). Ogni consegna ha un client suo:
    c fallisce sul certificato, e ogni richiesta arriva col SUO nome nel TLS."""
    import asyncio
    import ssl

    ca, cert, key = _certificati(tmp_path, "a.test")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca))
    svc._tls_context.cache_clear()
    monkeypatch.setattr(svc, "PARALLEL", 1)  # in fila: la seconda troverebbe libera la connessione della prima

    views = []
    lato_server = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    lato_server.load_cert_chain(cert, key)
    lato_server.sni_callback = lambda sock, name, _c: setattr(sock, "sni", name)

    async def servi(reader, writer):
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

    server = await asyncio.start_server(servi, "127.0.0.1", 0, ssl=lato_server)
    porta = server.sockets[0].getsockname()[1]

    async def risolvi(url):  # entrambi i nomi allo stesso indirizzo (quello del server di prova)
        u = httpx.URL(url)
        return u, u.raw_host.decode(), "127.0.0.1"
    monkeypatch.setattr(svc, "_resolve", risolvi)

    try:
        a, _ = _webhook(session, url=f"https://a.test:{porta}/a")
        c, _ = _webhook(session, url=f"https://c.test:{porta}/c")
        for w in (a, a, c, a):
            svc.enqueue_ping(session, w)
        await svc.deliver_due(db_engine)
    finally:
        server.close()
    esiti = {d.id: d for d in _consegne(session)}
    per_c = [d for d in esiti.values() if d.webhook_id == c.id]
    assert per_c[0].status == "pending" and "CERTIFICATE_VERIFY_FAILED" in per_c[0].last_error
    assert all(d.status == "delivered" for d in esiti.values() if d.webhook_id == a.id)
    assert ("c.test" not in [h.split(":")[0] for _, h, _ in views]), views
    assert all(sni == "a.test" and host.startswith("a.test:") for sni, host, _ in views), views


@pytest.mark.anyio
async def test_un_ricevente_che_non_risponde_non_trattiene_gli_altri(db_engine, session, indirizzi_pubblici):
    """Quaranta consegne in coda verso un ricevente fermo e una per un altro: dal
    vivo la sana arrivava dopo 66 s (ogni giro prendeva le 20 più vecchie e le
    aspettava tutte). Ora un webhook occupa al più PER_WEBHOOK posti."""
    import asyncio

    dead, _ = _webhook(session, url="http://morto.example/x")
    sano, _ = _webhook(session, url="http://sano.example/x")
    for _ in range(40):
        svc.enqueue_ping(session, dead)
    svc.enqueue_ping(session, sano)
    still = asyncio.Event()
    arrivate = []

    async def rispondi(req):
        if req.headers["Host"].startswith("morto"):
            await still.wait()
        arrivate.append(req.headers["Host"])
        return httpx.Response(200)

    flight = svc._InFlight(db_engine, httpx.AsyncClient(transport=httpx.MockTransport(rispondi)))
    await flight.round_()
    for _ in range(100):
        if "sano.example" in arrivate:
            break
        await asyncio.sleep(0.02)
    assert arrivate == ["sano.example"]
    assert sorted(flight.tasks.values()) == [dead.id] * svc.PER_WEBHOOK
    still.set()
    await flight.finish()


@pytest.mark.anyio
async def test_un_giro_non_aspetta_le_consegne_ancora_in_volo(db_engine, session, indirizzi_pubblici):
    import asyncio

    lento, _ = _webhook(session, url="http://lento.example/x")
    svc.enqueue_ping(session, lento)
    still = asyncio.Event()

    async def rispondi(req):
        if req.headers["Host"].startswith("lento"):
            await still.wait()
        return httpx.Response(200)

    flight = svc._InFlight(db_engine, httpx.AsyncClient(transport=httpx.MockTransport(rispondi)))
    assert await flight.round_() == 1
    other, _ = _webhook(session, url="http://altro.example/x")
    svc.enqueue_ping(session, other)
    assert await flight.round_() == 1  # la lenta è ancora in volo, e il giro dopo parte lo stesso
    await asyncio.sleep(0.1)
    stati = {d.webhook_id: d.status for d in _consegne(session)}
    assert stati == {lento.id: "sending", other.id: "delivered"}
    still.set()
    await flight.finish()
    assert {d.status for d in _consegne(session)} == {"delivered"}


@pytest.mark.anyio
async def test_un_indirizzo_che_httpx_non_legge_chiude_la_consegna_e_non_ferma_le_altre(db_engine, session):
    """Salvato prima del controllo (o letto diversamente da una versione nuova di
    httpx): prima l'eccezione usciva dal giro, la consegna restava presa per
    sempre e le altre del lotto perdevano il client a metà."""
    cattivo, _ = _webhook(session, url="http://0177.0.0.1/x")
    buono, _ = _webhook(session, url="http://93.184.216.34/x")
    svc.enqueue_ping(session, cattivo)
    svc.enqueue_ping(session, buono)
    await svc.deliver_due(db_engine, _client([200], []))
    stati = {d.webhook_id: (d.status, d.last_error) for d in _consegne(session)}
    assert stati[cattivo.id][0] == "failed" and "invalid" in stati[cattivo.id][1]
    assert stati[buono.id] == ("delivered", None)


@pytest.mark.anyio
async def test_un_errore_imprevisto_e_l_esito_di_quella_consegna(db_engine, session, indirizzi_pubblici, monkeypatch):
    """Un guasto fuori dalla chiamata HTTP (lì gli errori sono già un esito)."""
    rotto, _ = _webhook(session, url="http://rotto.example/x")
    buono, _ = _webhook(session, url="http://buono.example/x")
    svc.enqueue_ping(session, rotto)
    svc.enqueue_ping(session, buono)
    vera = svc._ship

    async def send(job, client):
        if "rotto" in job.url:
            raise RuntimeError("guasto che nessuno si aspetta")
        return await vera(job, client)
    monkeypatch.setattr(svc, "_ship", send)

    await svc.deliver_due(db_engine, _client([200], []))
    stati = {d.webhook_id: d for d in _consegne(session)}
    assert stati[rotto.id].status == "pending" and stati[rotto.id].attempts == 1  # si ritenta, con le attese
    assert "RuntimeError" in stati[rotto.id].last_error and "guasto" not in stati[rotto.id].last_error
    assert stati[buono.id].status == "delivered"


@pytest.mark.anyio
async def test_allo_spegnimento_le_consegne_in_volo_restano_prese(db_engine, session, indirizzi_pubblici):
    """Fermate a metà, tornano in coda dopo RECLAIM_AFTER: almeno una consegna."""
    import asyncio

    w, _ = _webhook(session)
    svc.enqueue_ping(session, w)

    async def mai(req):
        await asyncio.sleep(60)

    flight = svc._InFlight(db_engine, httpx.AsyncClient(transport=httpx.MockTransport(mai)))
    await flight.round_()
    await flight.finish(0.1)
    assert _consegne(session)[0].status == "sending" and not flight.tasks


def test_l_indirizzo_non_valido_si_rifiuta_al_salvataggio(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    for url in ("http://0177.0.0.1/", "http://a..b/", "http://x.example:70000/"):
        r = capo.post("/webhooks", json={"name": "A", "url": url, "events": ["flow.run.failed"]}, headers=CSRF)
        assert r.status_code == 422, (url, r.text)


@pytest.mark.anyio
async def test_una_coda_lunga_verso_un_ricevente_veloce_si_svuota_subito(db_engine, session, indirizzi_pubblici, monkeypatch):
    """Il ciclo vero: riparte appena un posto si libera. Sul prodtest 300 consegne
    a un ricevente pronto andavano a una al secondo (due in volo, poi CYCLE fermo)."""
    import asyncio
    import time

    import app.db.session as db

    monkeypatch.setattr(db, "engine", db_engine)
    monkeypatch.setattr(svc, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))))
    w, _ = _webhook(session)
    for _ in range(40):
        svc.enqueue_ping(session, w)
    stop = asyncio.Event()
    ciclo = asyncio.create_task(svc.delivery_loop(stop))
    t0 = time.monotonic()
    while time.monotonic() - t0 < 10 and any(d.status != "delivered" for d in _consegne(session)):
        await asyncio.sleep(0.05)
    duration = time.monotonic() - t0
    stop.set()
    await asyncio.wait_for(ciclo, 10)
    assert all(d.status == "delivered" for d in _consegne(session))
    assert duration < svc.CYCLE * 2, duration  # prima: 40 / PER_WEBHOOK giri da CYCLE = 40 s
