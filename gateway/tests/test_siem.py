"""Inoltro dell'audit a Splunk (HEC): le voci partono tutte, una volta (o, dopo un
guasto a metà, di nuovo con lo stesso id), nell'ordine, solo quando sono scritte
davvero; un guasto non ne salta nessuna; la configurazione la cambia solo un
amministratore, il token non si rilegge, l'osservatore non vede indirizzo ed errori.
"""
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.crypto import encrypt_secret
from app.db.session import get_session
from app.models import AuditLog, SiemDestination
from app.routes import siem as siem_routes
from app.services import siem, webhooks
from tests.conftest import make_user

CSRF = {"X-Tabularia-Csrf": "1"}
PASSWORD = "una-password-lunga"
TOKEN = "11111111-2222-3333-4444-555555555555"


class SplunkFinto:
    """L'HEC per quel che serve qui: controlla il token, conta gli eventi, risponde
    come Splunk ({"text": "Success", "code": 0} o un errore)."""

    def __init__(self):
        self.richieste: list[httpx.Request] = []
        self.eventi: list[dict] = []
        self.risposte: list[tuple[int, dict]] = []  # in fila; vuota = successo

    def gestisci(self, req: httpx.Request) -> httpx.Response:
        self.richieste.append(req)
        if req.headers.get("Authorization") != f"Splunk {TOKEN}":
            return httpx.Response(403, json={"text": "Invalid token", "code": 4})
        if self.risposte:
            codice, corpo = self.risposte.pop(0)
            return httpx.Response(codice, json=corpo)
        self.eventi += [json.loads(r) for r in req.content.decode().split("\n") if r]
        return httpx.Response(200, json={"text": "Success", "code": 0})

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.gestisci))


@pytest.fixture
def splunk(monkeypatch):
    s = SplunkFinto()

    async def risolvi(url):  # niente DNS nei test
        u = httpx.URL(url)
        return u, u.raw_host.decode(), "10.20.30.40"
    monkeypatch.setattr(webhooks, "_risolvi", risolvi)
    vero = siem.spedisci

    async def spedisci(d, eventi, client=None):
        return await vero(d, eventi, client or s.client())
    monkeypatch.setattr(siem, "spedisci", spedisci)
    return s


def _destinazione(session, **kw):
    d = SiemDestination(id=1, enabled=True, url="https://splunk.interno:8088", token_enc=encrypt_secret(TOKEN), **kw)
    session.add(d)
    session.commit()
    return d


def _voce(session, azione="flow.create", secondi_fa=60, **kw) -> int:
    v = AuditLog(ts=datetime.now(timezone.utc) - timedelta(seconds=secondi_fa), action=azione,
                 actor_label=kw.pop("actor_label", "anna@x.local"), **kw)
    session.add(v)
    session.commit()
    return v.id


def _dest(session) -> SiemDestination:
    session.expire_all()
    return session.get(SiemDestination, 1)


# ── che cosa parte ───────────────────────────────────────────────────────────

def test_una_voce_diventa_un_evento_hec_coi_nomi_che_splunk_si_aspetta(session):
    d = SiemDestination(index="sicurezza", host="tabularia-prod")
    v = AuditLog(id=7, ts=datetime(2026, 10, 12, 8, 30, 0), action="auth.login", outcome="failure",
                 actor_label="anna@x.local", actor_id=3, ip="10.1.2.3", user_agent="Firefox",
                 target_type="user", target_id=3, target_label="anna@x.local", detail=json.dumps({"reason": "password"}))
    e = siem.evento(v, d)
    assert e["time"] == datetime(2026, 10, 12, 8, 30, tzinfo=timezone.utc).timestamp()
    assert (e["index"], e["host"], e["source"], e["sourcetype"]) == ("sicurezza", "tabularia-prod", "tabularia", "tabularia:audit")
    assert e["event"] == {
        "id": 7, "time": "2026-10-12T08:30:00Z", "action": "auth.login", "outcome": "failure",
        "user": "anna@x.local", "user_id": 3, "src": "10.1.2.3", "user_agent": "Firefox",
        "target_type": "user", "target_id": 3, "target_label": "anna@x.local",
        "detail": {"reason": "password"}, "app": "tabularia",
    }
    # niente indice: quello di default del token; niente campi vuoti
    e = siem.evento(AuditLog(id=8, ts=datetime(2026, 10, 12), action="x", actor_label="y"), SiemDestination())
    assert "index" not in e and "src" not in e["event"] and "detail" not in e["event"]


@pytest.mark.parametrize("scritto, atteso", [
    ("https://splunk:8088", "https://splunk:8088/services/collector/event"),
    ("https://splunk:8088/", "https://splunk:8088/services/collector/event"),
    ("https://splunk:8088/services/collector", "https://splunk:8088/services/collector/event"),
    ("https://splunk:8088/services/collector/event", "https://splunk:8088/services/collector/event"),
    ("https://proxy.interno/hec/raccogli", "https://proxy.interno/hec/raccogli"),
])
def test_l_indirizzo_si_puo_incollare_in_ogni_forma(scritto, atteso):
    assert siem.endpoint(scritto) == atteso


# ── come parte ───────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_le_voci_partono_in_ordine_col_token_e_il_cursore_avanza(db_engine, session, splunk):
    _destinazione(session)
    ids = [_voce(session, f"azione.{i}") for i in range(3)]
    assert await siem.un_giro(db_engine) == 3
    assert [e["event"]["id"] for e in splunk.eventi] == ids
    req = splunk.richieste[0]
    assert req.url.host == "10.20.30.40" and req.headers["Host"] == "splunk.interno:8088"  # all'indirizzo verificato
    assert req.url.path == "/services/collector/event" and req.headers["X-Splunk-Request-Channel"]
    d = _dest(session)
    assert (d.cursor_id, d.sent_total, d.failures) == (ids[-1], 3, 0) and d.last_ok_at
    assert await siem.un_giro(db_engine) == 0  # niente due volte


@pytest.mark.anyio
async def test_le_voci_appena_scritte_aspettano_qualche_secondo(db_engine, session, splunk):
    """Gli id si assegnano prima del commit: una voce più giovane del ritardo
    potrebbe avere davanti un id più basso non ancora scritto."""
    _destinazione(session)
    vecchia = _voce(session, secondi_fa=60)
    _voce(session, secondi_fa=1)
    await siem.un_giro(db_engine)
    assert [e["event"]["id"] for e in splunk.eventi] == [vecchia]


@pytest.mark.anyio
async def test_splunk_giu_non_si_salta_niente_e_si_aspetta_sempre_di_piu(db_engine, session, splunk):
    _destinazione(session)
    ids = [_voce(session) for _ in range(2)]
    splunk.risposte = [(503, {"text": "Server is busy", "code": 9})]
    assert await siem.un_giro(db_engine) == 0
    d = _dest(session)
    assert d.cursor_id == 0 and d.failures == 1 and "Server is busy" in d.last_error
    assert d.next_attempt_at > siem.adesso()
    assert await siem.un_giro(db_engine) == 0  # prima del prossimo tentativo non si riprova
    d.next_attempt_at = siem.adesso() - timedelta(seconds=1)
    session.add(d)
    session.commit()
    assert await siem.un_giro(db_engine) == 2
    assert [e["event"]["id"] for e in splunk.eventi] == ids  # le stesse, nessuna persa
    d = _dest(session)
    assert d.failures == 0 and d.next_attempt_at is None  # l'errore di prima resta come storia, con la sua ora


def test_le_attese_crescono_fino_al_tetto():
    assert [siem.attesa(n) for n in (1, 2, 3, 4)] == [5, 10, 20, 40]
    assert siem.attesa(30) == siem.ATTESA_MAX


@pytest.mark.anyio
async def test_un_token_sbagliato_lo_dice_e_ritenta(db_engine, session, splunk):
    _destinazione(session)
    d = _dest(session)
    d.token_enc = encrypt_secret("sbagliato")
    session.add(d)
    session.commit()
    _voce(session)
    await siem.un_giro(db_engine)
    d = _dest(session)
    assert d.cursor_id == 0 and "Invalid token" in d.last_error and "403" in d.last_error


@pytest.mark.anyio
async def test_un_lotto_non_supera_il_tetto_in_byte(db_engine, session, splunk, monkeypatch):
    monkeypatch.setattr(siem, "MAX_BYTE", 3000)
    _destinazione(session)
    ids = [_voce(session, detail=json.dumps({"x": "a" * 1000})) for _ in range(6)]
    assert await siem.un_giro(db_engine) == 6
    assert len(splunk.richieste) > 1 and all(len(r.content) <= 3000 for r in splunk.richieste)
    assert [e["event"]["id"] for e in splunk.eventi] == ids


@pytest.mark.anyio
async def test_spento_non_parte_niente(db_engine, session, splunk):
    _destinazione(session)
    d = _dest(session)
    d.enabled = False
    session.add(d)
    session.commit()
    _voce(session)
    assert await siem.un_giro(db_engine) == 0 and splunk.richieste == []


# ── il pannello ──────────────────────────────────────────────────────────────

@pytest.fixture
def app(session, splunk):
    from app.routes import api_tokens as tokens_routes
    from app.routes import auth as auth_routes

    app = FastAPI()
    for r in (auth_routes.router, tokens_routes.router, siem_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _entra(app, session, email, **kw) -> TestClient:
    from app.core.security import hash_password

    make_user(session, email=email, hashed_password=hash_password(PASSWORD), **kw)
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF).status_code == 200
    return c


def test_si_accende_con_indirizzo_e_token_e_parte_da_adesso(app, session, splunk):
    vecchia = _voce(session)
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    r = capo.put("/admin/siem", json={"enabled": True}, headers=CSRF)
    assert r.status_code == 422  # senza indirizzo e token no
    r = capo.put("/admin/siem", json={"url": "https://splunk.interno:8088", "token": TOKEN, "index": "sicurezza",
                                      "enabled": True}, headers=CSRF)
    assert r.status_code == 200, r.text
    st = r.json()
    assert st["enabled"] and st["token_set"] and "token" not in st and TOKEN not in r.text
    d = _dest(session)
    assert d.cursor_id >= vecchia  # lo storico di prima resta nel pannello Audit
    assert d.token_enc and TOKEN not in d.token_enc  # cifrato
    voce = session.exec(select(AuditLog).where(AuditLog.action == "siem.update")).one()
    assert json.loads(voce.detail)["changes"]["token"] == "changed" and TOKEN not in voce.detail


def test_spento_e_riacceso_riprende_da_dove_era(app, session, splunk):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    capo.put("/admin/siem", json={"url": "https://s:8088", "token": TOKEN, "enabled": True}, headers=CSRF)
    cursore = _dest(session).cursor_id
    capo.put("/admin/siem", json={"enabled": False}, headers=CSRF)
    _voce(session)
    capo.put("/admin/siem", json={"enabled": True}, headers=CSRF)
    assert _dest(session).cursor_id == cursore  # le voci di mentre era spento partiranno


def test_la_prova_dice_cosa_ha_risposto_splunk(app, session, splunk):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    capo.put("/admin/siem", json={"url": "https://s:8088", "token": TOKEN}, headers=CSRF)
    r = capo.post("/admin/siem/test", headers=CSRF).json()
    assert r == {"ok": True, "status_code": 200, "message": "Success"}
    assert splunk.eventi[-1]["event"]["action"] == "siem.test" and splunk.eventi[-1]["event"]["test"] is True
    capo.put("/admin/siem", json={"token": "sbagliato"}, headers=CSRF)
    r = capo.post("/admin/siem/test", headers=CSRF).json()
    assert not r["ok"] and r["status_code"] == 403 and "Invalid token" in r["message"]


def test_l_osservatore_vede_lo_stato_ma_non_indirizzo_ed_errori(app, session, splunk):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    capo.put("/admin/siem", json={"url": "https://splunk.interno:8088/services/collector", "token": TOKEN, "enabled": True}, headers=CSRF)
    d = _dest(session)
    d.last_error = "risposta 403: Invalid token"
    session.add(d)
    session.commit()
    occhio = _entra(app, session, "occhio@x.local", is_observer=True)
    st = occhio.get("/admin/siem").json()
    assert st["url"] == "https://splunk.interno/…" and st["last_error"] is None and not st["editable"]
    assert occhio.put("/admin/siem", json={"enabled": False}, headers=CSRF).status_code == 403
    assert occhio.post("/admin/siem/test", headers=CSRF).status_code == 403
    assert _entra(app, session, "anna@x.local").get("/admin/siem").status_code == 403


def test_un_token_api_non_cambia_la_destinazione(app, session, splunk):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    token = capo.post("/auth/tokens", json={"name": "x", "scope": "write", "expires_in_days": 5}, headers=CSRF).json()["token"]
    script = TestClient(app)
    h = {"Authorization": f"Bearer {token}"}
    assert script.get("/admin/siem", headers=h).json()["editable"] is False
    # anche col corpo sbagliato: 403, non 422
    assert script.put("/admin/siem", json={"url": "https://altrove:8088", "token": TOKEN}, headers=h).status_code == 403
    assert script.put("/admin/siem", json={"enabled": "forse"}, headers=h).status_code == 403
    assert script.post("/admin/siem/test", headers=h).status_code == 403


def test_l_indirizzo_si_controlla(app, session, splunk):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    for url in ("ftp://splunk/x", "splunk:8088", "http://0177.0.0.1:8088"):
        assert capo.put("/admin/siem", json={"url": url}, headers=CSRF).status_code == 422, url


def test_lo_stato_dice_quante_voci_aspettano(app, session, splunk):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    capo.put("/admin/siem", json={"url": "https://s:8088", "token": TOKEN, "enabled": True}, headers=CSRF)
    for _ in range(3):
        _voce(session)
    st = capo.get("/admin/siem").json()
    assert st["backlog"] >= 3 and st["oldest_pending_at"]
