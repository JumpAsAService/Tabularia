"""Impostazioni del motore dal pannello: il gateway le conserva, le fa controllare
e applicare al motore, e le rispedisce a intervalli.

Le regole che questi test tengono ferme: si salva solo quello che il motore ha
accettato (tutto o niente; motore giù = niente cambiato); il limite duro dei task
non supera la soglia oltre cui il gateway dichiara perso un run; scrive solo
l'amministratore, mai con un token API, e l'audit dice da cosa a cosa; con
ENGINE__SETTINGS_PANEL=false si vedono e non valgono; il giro periodico rispedisce,
e una salvata che non vale più si scarta invece di lasciare quella vecchia.
"""
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.db.session import get_session
from app.models import AuditLog, EngineSetting
from app.routes import engine_settings as routes
from app.services import regolazioni_motore as rm
from tests.conftest import make_user

CSRF = {"X-Tabularia-Csrf": "1"}
PASSWORD = "una-password-lunga"


class MotoreFinto:
    """Il motore per quel che serve qui: due voci, i loro controlli, e quello che applica."""

    def __init__(self):
        self.valori: dict = {}
        self.ricevute: list[dict] = []
        self.giu = False

    def _catalogo(self, massimo):
        return {
            "items": [
                {"key": "export.max_rows", "category": "export", "unit": "rows", "min": 0, "max": 100_000_000,
                 "env_var": "EXPORT__MAX_ROWS", "env_value": 5_000_000, "value": self.valori.get("export.max_rows")},
                {"key": "celery.task_time_limit", "category": "execution", "unit": "seconds", "min": 60,
                 "max": massimo, "env_var": "CELERY__TASK_TIME_LIMIT", "env_value": 3600,
                 "value": self.valori.get("celery.task_time_limit")},
            ],
            "unavailable": ["bigquery"], "refresh_seconds": 10.0, "discarded": [],
        }

    def gestisci(self, req: httpx.Request) -> httpx.Response:
        if self.giu:
            raise httpx.ConnectError("il motore non risponde")
        assert req.url.path == "/settings"
        if req.method == "GET":
            return httpx.Response(200, json=self._catalogo(int(req.url.params["max_task_time_limit"])))
        corpo = json.loads(req.content)
        self.ricevute.append(corpo)
        errori = []
        for k, v in corpo["values"].items():
            if k in ("export.max_rows", "celery.task_time_limit") and (isinstance(v, bool) or not isinstance(v, int)):
                errori.append({"key": k, "code": "integer"})  # the engine checks the type (core/panel_settings.py)
                continue
            if k == "export.max_rows" and v < 0:
                errori.append({"key": k, "code": "min", "limit": 0})
            if k == "celery.task_time_limit" and v > corpo["max_task_time_limit"]:
                errori.append({"key": k, "code": "max", "limit": corpo["max_task_time_limit"]})
        if errori:
            return httpx.Response(422, json={"detail": {"errors": errori}})
        self.valori = dict(corpo["values"])
        return httpx.Response(200, json=self._catalogo(corpo["max_task_time_limit"]))


@pytest.fixture
def motore(monkeypatch):
    m = MotoreFinto()
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(m.gestisci), base_url="http://engine"))
    return m


@pytest.fixture
def app(session, motore):
    from app.routes import api_tokens as tokens_routes
    from app.routes import auth as auth_routes

    app = FastAPI()
    for r in (auth_routes.router, tokens_routes.router, routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _entra(app, session, email, **kw) -> TestClient:
    from app.core.security import hash_password

    make_user(session, email=email, hashed_password=hash_password(PASSWORD), **kw)
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF).status_code == 200
    return c


def _voce(stato, chiave):
    return next(v for v in stato["items"] if v["key"] == chiave)


def test_l_amministratore_cambia_e_torna_al_valore_dell_ambiente(app, session, motore):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    stato = capo.get("/admin/engine-settings").json()
    assert stato["editable"] and stato["panel_enabled"] and stato["unavailable"] == ["bigquery"]
    assert _voce(stato, "export.max_rows") | {"value": None, "applied": None, "env_value": 5_000_000} == _voce(stato, "export.max_rows")

    stato = capo.put("/admin/engine-settings", json={"values": {"export.max_rows": 1_000_000}}, headers=CSRF).json()
    v = _voce(stato, "export.max_rows")
    assert (v["value"], v["applied"], v["updated_by"]) == (1_000_000, 1_000_000, "capo@x.local")
    assert session.get(EngineSetting, "export.max_rows").value == 1_000_000
    assert motore.ricevute[-1]["values"] == {"export.max_rows": 1_000_000}

    stato = capo.put("/admin/engine-settings", json={"values": {"export.max_rows": None}}, headers=CSRF).json()
    assert _voce(stato, "export.max_rows")["value"] is None and motore.valori == {}
    session.expire_all()
    assert session.exec(select(EngineSetting)).all() == []


def test_l_audit_dice_da_cosa_a_cosa(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    capo.put("/admin/engine-settings", json={"values": {"export.max_rows": 10}}, headers=CSRF)
    capo.put("/admin/engine-settings", json={"values": {"export.max_rows": 20}}, headers=CSRF)
    voci = session.exec(select(AuditLog).where(AuditLog.action == "engine_settings.update").order_by(AuditLog.id)).all()
    assert [json.loads(v.detail)["changes"]["export.max_rows"] for v in voci] == [
        {"from": None, "to": 10}, {"from": 10, "to": 20}]
    # niente di cambiato, niente audit
    capo.put("/admin/engine-settings", json={"values": {"export.max_rows": 20}}, headers=CSRF)
    assert len(session.exec(select(AuditLog).where(AuditLog.action == "engine_settings.update")).all()) == 2


def test_si_salva_solo_quello_che_il_motore_accetta(app, session, motore):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    capo.put("/admin/engine-settings", json={"values": {"export.max_rows": 10}}, headers=CSRF)
    r = capo.put("/admin/engine-settings", json={"values": {"export.max_rows": -1}}, headers=CSRF)
    assert r.status_code == 422 and r.json()["detail"]["errors"][0] == {"key": "export.max_rows", "code": "min", "limit": 0}
    session.expire_all()
    assert session.get(EngineSetting, "export.max_rows").value == 10 and motore.valori == {"export.max_rows": 10}


def test_motore_giu_niente_cambiato(app, session, motore):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    motore.giu = True
    assert capo.put("/admin/engine-settings", json={"values": {"export.max_rows": 10}}, headers=CSRF).status_code == 503
    assert capo.get("/admin/engine-settings").status_code == 503
    assert session.exec(select(EngineSetting)).all() == []


def test_il_limite_duro_resta_sotto_la_soglia_del_gateway(app, session, motore, monkeypatch):
    """Il gateway dichiara perso un run dopo ENGINE__RUN_STALE_TIMEOUT_SECONDS: col
    limite duro sopra, chiuderebbe come fallito un run ancora vivo."""
    monkeypatch.setenv("ENGINE__RUN_STALE_TIMEOUT_SECONDS", "3900")
    get_settings.cache_clear()
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    assert _voce(capo.get("/admin/engine-settings").json(), "celery.task_time_limit")["max"] == 3600
    r = capo.put("/admin/engine-settings", json={"values": {"celery.task_time_limit": 3700}}, headers=CSRF)
    assert r.status_code == 422 and motore.ricevute[-1]["max_task_time_limit"] == 3600
    assert capo.put("/admin/engine-settings", json={"values": {"celery.task_time_limit": 1800}}, headers=CSRF).status_code == 200


def test_l_osservatore_vede_e_non_cambia(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    capo.put("/admin/engine-settings", json={"values": {"export.max_rows": 10}}, headers=CSRF)
    occhio = _entra(app, session, "occhio@x.local", is_observer=True)
    stato = occhio.get("/admin/engine-settings").json()
    v = _voce(stato, "export.max_rows")
    assert not stato["editable"] and v["value"] == 10 and v["updated_by"] != "capo@x.local"
    assert occhio.put("/admin/engine-settings", json={"values": {"export.max_rows": 1}}, headers=CSRF).status_code == 403
    chiunque = _entra(app, session, "anna@x.local")
    assert chiunque.get("/admin/engine-settings").status_code == 403


def test_un_token_non_cambia_le_impostazioni(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    token = capo.post("/auth/tokens", json={"name": "x", "scope": "write", "expires_in_days": 5}, headers=CSRF).json()["token"]
    script = TestClient(app)
    h = {"Authorization": f"Bearer {token}"}
    assert script.get("/admin/engine-settings", headers=h).json()["editable"] is False
    r = script.put("/admin/engine-settings", json={"values": {"export.max_rows": 1}}, headers={**h, "Accept-Language": "it"})
    assert r.status_code == 403 and "non cambia le impostazioni" in r.json()["detail"]


def test_col_pannello_spento_dall_ambiente_si_vedono_e_non_valgono(app, session, motore, monkeypatch):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    capo.put("/admin/engine-settings", json={"values": {"export.max_rows": 10}}, headers=CSRF)
    monkeypatch.setenv("ENGINE__SETTINGS_PANEL", "false")
    get_settings.cache_clear()
    stato = capo.get("/admin/engine-settings").json()
    assert not stato["editable"] and not stato["panel_enabled"] and _voce(stato, "export.max_rows")["value"] is None
    assert capo.put("/admin/engine-settings", json={"values": {"export.max_rows": 20}}, headers=CSRF).status_code == 403
    assert rm.in_vigore(session) == {}  # il giro periodico spedisce «nessuna»


@pytest.mark.anyio
async def test_il_giro_rispedisce_e_scarta_quella_che_non_vale_piu(session, motore, monkeypatch):
    """Salvata quando valeva (limite duro 3700 con una soglia più alta), poi la
    soglia è scesa: si spediscono le altre, non si lascia in vigore la vecchia."""
    from datetime import datetime

    for k, v in (("export.max_rows", 10), ("celery.task_time_limit", 3700)):
        session.add(EngineSetting(key=k, value=v, updated_at=datetime(2026, 10, 12)))
    session.commit()
    monkeypatch.setenv("ENGINE__RUN_STALE_TIMEOUT_SECONDS", "3900")
    get_settings.cache_clear()
    await rm.spingi_scartando(rm.in_vigore(session))
    assert motore.valori == {"export.max_rows": 10}


@pytest.mark.anyio
async def test_il_giro_periodico_rispedisce_le_salvate(db_engine, session, motore):
    """Valkey svuotato o motore ripartito: il giro rimette quelle del database."""
    from datetime import datetime

    session.add(EngineSetting(key="export.max_rows", value=77, updated_at=datetime(2026, 10, 12)))
    session.commit()
    motore.valori = {}
    assert await rm.un_giro(db_engine) is True
    assert motore.valori == {"export.max_rows": 77}


def test_un_motore_di_prima_lo_dice_invece_di_non_risponde(app, session, motore, monkeypatch):
    """Aggiornamento a metà: il motore non conosce /settings (404)."""
    monkeypatch.setattr(motore, "gestisci", lambda req: httpx.Response(404, json={"detail": "Not Found"}))
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(lambda req: motore.gestisci(req)), base_url="http://engine"))
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    r = capo.get("/admin/engine-settings")
    assert r.status_code == 503 and "older version" in r.json()["detail"]
    r = capo.put("/admin/engine-settings", json={"values": {"export.max_rows": 10}}, headers=CSRF)
    assert r.status_code == 503 and "nothing was changed" in r.json()["detail"]
    assert session.exec(select(EngineSetting)).all() == []


# ── dalla prova a scatola chiusa (2026-10-12) ───────────────────────────────

@pytest.fixture
def motore_che_rifiuta(motore, monkeypatch):
    """Il motore rifiuta le voci in `rifiuta` (salvate quando valevano: l'ambiente è cambiato)."""
    motore.rifiuta = set()
    vero = motore.gestisci

    def gestisci(req):
        if req.method == "PUT":
            corpo = json.loads(req.content)
            cattive = [k for k in corpo["values"] if k in motore.rifiuta]
            if cattive:
                motore.ricevute.append(corpo)
                return httpx.Response(422, json={"detail": {"errors": [{"key": k, "code": "below"} for k in cattive]}})
        return vero(req)
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(gestisci), base_url="http://engine"))
    return motore


@pytest.mark.parametrize("valore", [True, False, "100000", 1.5, 100000.0, [1], {"a": 1}])
def test_solo_numeri_interi(app, session, motore, valore):
    """`true` diventava 1 (e `false` 0, «nessun tetto» per BigQuery) prima che il motore lo vedesse."""
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    r = capo.put("/admin/engine-settings", json={"values": {"export.max_rows": valore}}, headers=CSRF)
    assert r.status_code == 422 and motore.valori == {}


def test_niente_chiavi_sconosciute_ne_corpi_enormi(app, session, motore):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    r = capo.put("/admin/engine-settings", json={"values": {"zz.sconosciuta": None}}, headers=CSRF)
    assert r.status_code == 422 and r.json()["detail"]["errors"] == [{"key": "zz.sconosciuta", "code": "unknown"}]
    r = capo.put("/admin/engine-settings", json={"values": {f"k{i}": None for i in range(200)}}, headers=CSRF)
    assert r.status_code == 422


def test_un_token_riceve_403_anche_col_corpo_sbagliato(app, session):
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    token = capo.post("/auth/tokens", json={"name": "x", "scope": "write", "expires_in_days": 5}, headers=CSRF).json()["token"]
    r = TestClient(app).put("/admin/engine-settings", json={"values": {"export.max_rows": "molti"}},
                            headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


@pytest.mark.anyio
async def test_il_giro_scarta_finche_il_motore_accetta(db_engine, session, motore_che_rifiuta):
    """Prima scartava una volta sola e, al secondo rifiuto, non spediva più niente."""
    from datetime import datetime

    for k, v in (("export.max_rows", 4777), ("celery.task_time_limit", 1800)):
        session.add(EngineSetting(key=k, value=v, updated_at=datetime(2026, 10, 12)))
    session.commit()
    motore_che_rifiuta.rifiuta = {"celery.task_time_limit"}
    assert await rm.un_giro(db_engine)
    assert motore_che_rifiuta.valori == {"export.max_rows": 4777}


def test_una_salvata_che_non_vale_piu_non_blocca_le_altre(app, session, motore_che_rifiuta):
    """L'ambiente è cambiato sotto un valore salvato: si cambiano le altre, e quello
    resta salvato (non applicato) finché non lo si cambia o lo si azzera."""
    from datetime import datetime

    session.add(EngineSetting(key="celery.task_time_limit", value=3250, updated_at=datetime(2026, 10, 12)))
    session.commit()
    motore_che_rifiuta.rifiuta = {"celery.task_time_limit"}
    capo = _entra(app, session, "capo@x.local", is_superuser=True)
    r = capo.put("/admin/engine-settings", json={"values": {"export.max_rows": 10}}, headers=CSRF)
    assert r.status_code == 200, r.text
    assert motore_che_rifiuta.valori == {"export.max_rows": 10}
    assert motore_che_rifiuta.ricevute[-1]["changed"] == ["export.max_rows"]
    v = _voce(r.json(), "celery.task_time_limit")
    assert v["value"] == 3250 and v["applied"] is None  # salvata, non applicata: il pannello lo dice
    r = capo.put("/admin/engine-settings", json={"values": {"celery.task_time_limit": None}}, headers=CSRF)
    assert r.status_code == 200 and session.get(EngineSetting, "celery.task_time_limit") is None
