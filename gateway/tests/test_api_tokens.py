"""Token API e account di servizio: la credenziale degli script e delle integrazioni.

Le regole che questi test tengono ferme: il segreto si vede una volta e non si
salva; un token vale coi permessi del suo utente e, se di sola lettura, non
modifica niente; scade, si revoca, muore con il suo account; viaggia solo
nell'intestazione; non crea altri token; l'audit dice con quale token si è fatto
cosa. Gli account di servizio non entrano con password o SSO.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_session
from app.models import ApiToken, AuditLog, Project, User
from app.routes import api_tokens as tokens_routes
from app.routes import auth as auth_routes
from app.routes import projects as projects_routes
from app.routes import service_accounts as sa_routes
from app.routes import users as users_routes
from app.services import api_tokens
from tests.conftest import make_user

PASSWORD = "una-password-lunga"
CSRF = {"X-Tabularia-Csrf": "1"}


@pytest.fixture
def app(session):
    app = FastAPI()
    for r in (auth_routes.router, tokens_routes.router, sa_routes.router, users_routes.router, projects_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _utente(session, email, **kw) -> int:
    return make_user(session, email=email, hashed_password=hash_password(PASSWORD), **kw).id


def _entra(app, email) -> TestClient:
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF).status_code == 200
    return c


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def _crea(client, **kw):
    corpo = {"name": "script", "scope": "read", "expires_in_days": 30, **kw}
    r = client.post("/auth/tokens", json=corpo, headers=CSRF)
    assert r.status_code == 201, r.text
    return r.json()


# ── il token ────────────────────────────────────────────────────────────────

def test_il_segreto_si_vede_una_volta_e_nel_database_resta_l_impronta(app, session):
    _utente(session, "a@x.local")
    browser = _entra(app, "a@x.local")
    creato = _crea(browser, name="report notturno")
    assert creato["token"].startswith("tab_") and creato["prefix"] == creato["token"][:12]
    riga = session.exec(select(ApiToken)).one()
    assert riga.token_hash == api_tokens.impronta(creato["token"]) and creato["token"] not in riga.token_hash
    elenco = browser.get("/auth/tokens").json()
    assert [t["name"] for t in elenco] == ["report notturno"] and "token" not in elenco[0]


def test_un_token_vale_come_il_suo_utente(app, session):
    _utente(session, "a@x.local", full_name="Anna")
    token = _crea(_entra(app, "a@x.local"))["token"]
    r = TestClient(app).get("/auth/me", headers=_bearer(token))
    assert r.status_code == 200 and r.json()["email"] == "a@x.local"


def test_di_sola_lettura_non_modifica_niente(app, session):
    _utente(session, "capo@x.local", is_superuser=True)
    browser = _entra(app, "capo@x.local")
    lettura = _crea(browser, scope="read")["token"]
    scrittura = _crea(browser, name="scrive", scope="write")["token"]
    script = TestClient(app)
    assert script.get("/projects", headers=_bearer(lettura)).status_code == 200
    r = script.post("/projects", json={"name": "zz"}, headers={**_bearer(lettura), "Accept-Language": "it"})
    assert r.status_code == 403 and "sola lettura" in r.json()["detail"]
    # nella lingua di chi chiede
    r = script.post("/projects", json={"name": "zz"}, headers={**_bearer(lettura), "Accept-Language": "en"})
    assert "Read-only token" in r.json()["detail"]
    assert script.post("/projects", json={"name": "zz"}, headers=_bearer(scrittura)).status_code in (200, 201)


def test_l_audit_dice_con_quale_token_si_e_fatto(app, session):
    _utente(session, "capo@x.local", is_superuser=True)
    browser = _entra(app, "capo@x.local")
    token = _crea(browser, name="airflow notte", scope="write")["token"]
    vecchio = _crea(browser, name="da ritirare")
    # uno script che ritira una credenziale: un'azione che l'audit registra
    assert TestClient(app).delete(f"/auth/tokens/{vecchio['id']}", headers=_bearer(token)).status_code == 204
    voce = session.exec(select(AuditLog).where(AuditLog.action == "token.revoke")).one()
    assert '"token": "airflow notte"' in voce.detail and '"name": "da ritirare"' in voce.detail
    # il segreto non finisce mai nel registro
    assert not any(token in (v.detail or "") for v in session.exec(select(AuditLog)).all())


def test_scaduto_revocato_o_inventato_non_vale(app, session):
    uid = _utente(session, "a@x.local")
    browser = _entra(app, "a@x.local")
    scaduto, revocato = _crea(browser, name="uno")["token"], _crea(browser, name="due")
    riga = session.exec(select(ApiToken).where(ApiToken.name == "uno")).one()
    riga.expires_at = api_tokens.adesso()
    session.add(riga)
    session.commit()
    assert browser.delete(f"/auth/tokens/{revocato['id']}", headers=CSRF).status_code == 204
    for t in (scaduto, revocato["token"], "tab_inventato"):
        assert TestClient(app).get("/auth/me", headers=_bearer(t)).status_code == 401
    assert session.get(User, uid) is not None


def test_un_account_disattivato_ferma_i_suoi_token(app, session):
    uid = _utente(session, "a@x.local")
    token = _crea(_entra(app, "a@x.local"))["token"]
    u = session.get(User, uid)
    u.is_active = False
    session.add(u)
    session.commit()
    assert TestClient(app).get("/auth/me", headers=_bearer(token)).status_code == 401


def test_viaggia_solo_nell_intestazione(app, session):
    """Il cookie è della sessione del browser: un token API lì non vale."""
    _utente(session, "a@x.local")
    token = _crea(_entra(app, "a@x.local"))["token"]
    c = TestClient(app)
    c.cookies.set("tab_token", token)
    assert c.get("/auth/me").status_code == 401


def test_un_token_non_crea_altri_token(app, session):
    _utente(session, "a@x.local")
    token = _crea(_entra(app, "a@x.local"), scope="write")["token"]
    r = TestClient(app).post("/auth/tokens", json={"name": "figlio", "scope": "write", "expires_in_days": 5}, headers=_bearer(token))
    assert r.status_code == 403


def test_scadenza_obbligatoria_e_con_un_tetto(app, session, monkeypatch):
    monkeypatch.setenv("AUTH__API_TOKEN_MAX_DAYS", "90")
    get_settings.cache_clear()
    _utente(session, "a@x.local")
    browser = _entra(app, "a@x.local")
    for giorni in (0, 91):
        assert browser.post("/auth/tokens", json={"name": "x", "expires_in_days": giorni}, headers=CSRF).status_code == 422
    assert browser.post("/auth/tokens", json={"name": "x", "expires_in_days": 90}, headers=CSRF).status_code == 201


def test_i_token_personali_si_possono_spegnere(app, session, monkeypatch):
    monkeypatch.setenv("AUTH__PERSONAL_TOKENS", "false")
    get_settings.cache_clear()
    _utente(session, "a@x.local")
    r = _entra(app, "a@x.local").post("/auth/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF)
    assert r.status_code == 403


def test_l_osservatore_non_ha_token_personali(app, session):
    """L'osservatore è l'ospite condiviso di una demo: un token gli sopravvivrebbe
    al cambio di password, e lo vedrebbero (e revocherebbero) tutti i visitatori."""
    _utente(session, "occhio@x.local", is_observer=True)
    occhio = _entra(app, "occhio@x.local")
    assert occhio.post("/auth/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF).status_code == 403
    assert occhio.get("/auth/me").json()["personal_tokens"] is False


def test_l_autenticazione_col_token_e_una_lettura_sola(db_engine):
    """Come per le sessioni: una SELECT, il token in JOIN con l'utente."""
    from datetime import datetime, timezone
    from types import SimpleNamespace

    from sqlalchemy import event
    from sqlmodel import Session

    from app.deps import auth

    with Session(db_engine) as s:
        u = make_user(s, email="c@x.local", last_seen_at=datetime.now(timezone.utc))
        _, token = api_tokens.crea(s, u, "t", "read", 5, u)
        riga = s.exec(select(ApiToken)).one()
        riga.last_used_at = api_tokens.adesso()
        s.add(riga)
        s.commit()
    comandi = []
    event.listen(db_engine, "before_cursor_execute", lambda c, cur, st, *a: comandi.append(st))
    richiesta = SimpleNamespace(headers={}, client=SimpleNamespace(host="203.0.113.9"), method="GET", state=SimpleNamespace())
    with Session(db_engine) as s:
        utente = auth.get_current_user(richiesta, token=token, session=s)
    assert utente.email == "c@x.local"
    assert len(comandi) == 1 and "api_tokens" in comandi[0] and "users" in comandi[0]


# ── gli account di servizio ─────────────────────────────────────────────────

def test_l_amministratore_crea_un_account_di_servizio_e_i_suoi_token(app, session):
    _utente(session, "capo@x.local", is_superuser=True)
    capo = _entra(app, "capo@x.local")
    acc = capo.post("/service-accounts", json={"name": "Airflow produzione"}, headers=CSRF).json()
    assert acc["email"] == "airflow-produzione@service.invalid" and acc["tokens"] == 0
    t = capo.post(f"/service-accounts/{acc['id']}/tokens", json={"name": "dag", "scope": "write", "expires_in_days": 30}, headers=CSRF)
    assert t.status_code == 201
    me = TestClient(app).get("/auth/me", headers=_bearer(t.json()["token"])).json()
    assert me["email"] == acc["email"]
    assert capo.get("/service-accounts").json()[0]["tokens"] == 1
    # entra solo col token: password e SSO no (nessuna password locale)
    assert TestClient(app).post("/auth/login", json={"email": acc["email"], "password": "x"}).status_code == 401
    assert session.get(User, acc["id"]).is_service and session.get(User, acc["id"]).hashed_password is None


def test_l_osservatore_vede_gli_account_e_non_li_tocca(app, session):
    _utente(session, "capo@x.local", is_superuser=True)
    _utente(session, "occhio@x.local", is_observer=True)
    acc = _entra(app, "capo@x.local").post("/service-accounts", json={"name": "CI"}, headers=CSRF).json()
    occhio = _entra(app, "occhio@x.local")
    assert [a["name"] for a in occhio.get("/service-accounts").json()] == ["CI"]
    assert occhio.post("/service-accounts", json={"name": "altro"}, headers=CSRF).status_code == 403
    assert occhio.post(f"/service-accounts/{acc['id']}/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF).status_code == 403


def test_i_token_dell_amministratore_solo_per_account_di_servizio(app, session):
    """Un amministratore non si crea credenziali a nome di una persona."""
    _utente(session, "capo@x.local", is_superuser=True)
    anna = _utente(session, "anna@x.local")
    r = _entra(app, "capo@x.local").post(f"/service-accounts/{anna}/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF)
    assert r.status_code == 404


def test_togliere_l_account_toglie_i_token(app, session):
    _utente(session, "capo@x.local", is_superuser=True)
    capo = _entra(app, "capo@x.local")
    acc = capo.post("/service-accounts", json={"name": "CI"}, headers=CSRF).json()
    token = capo.post(f"/service-accounts/{acc['id']}/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF).json()["token"]
    assert capo.delete(f"/service-accounts/{acc['id']}", headers=CSRF).status_code == 204
    assert TestClient(app).get("/auth/me", headers=_bearer(token)).status_code == 401
    assert session.exec(select(ApiToken)).all() == []


def test_l_elenco_di_tutti_i_token_all_osservatore_e_mascherato(app, session):
    _utente(session, "capo@x.local", is_superuser=True)
    _utente(session, "occhio@x.local", is_observer=True)
    _utente(session, "anna@x.local")
    _crea(_entra(app, "anna@x.local"), name="di anna")
    pieno = _entra(app, "capo@x.local").get("/admin/api-tokens").json()
    assert pieno[0]["user_email"] == "anna@x.local" and pieno[0]["prefix"].startswith("tab_")
    occhio = _entra(app, "occhio@x.local")
    visto = occhio.get("/admin/api-tokens").json()
    assert visto[0]["user_email"] != "anna@x.local" and visto[0]["prefix"] == "tab_…" and visto[0]["last_used_ip"] is None
    assert occhio.delete(f"/admin/api-tokens/{visto[0]['id']}", headers=CSRF).status_code == 403


# ── dalla revisione di sicurezza ─────────────────────────────────────────────

def test_una_password_nuova_ritira_anche_i_token(app, session):
    _utente(session, "capo@x.local", is_superuser=True)
    anna = _utente(session, "anna@x.local")
    token = _crea(_entra(app, "anna@x.local"))["token"]
    capo = _entra(app, "capo@x.local")
    assert capo.patch(f"/users/{anna}", json={"password": "un-altra-password"}, headers=CSRF).status_code == 200
    assert TestClient(app).get("/auth/me", headers=_bearer(token)).status_code == 401


def test_spegnere_i_token_personali_spegne_anche_quelli_dati(app, session, monkeypatch):
    _utente(session, "capo@x.local", is_superuser=True)
    _utente(session, "anna@x.local")
    personale = _crea(_entra(app, "anna@x.local"))["token"]
    capo = _entra(app, "capo@x.local")
    acc = capo.post("/service-accounts", json={"name": "CI"}, headers=CSRF).json()
    di_servizio = capo.post(f"/service-accounts/{acc['id']}/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF).json()["token"]
    monkeypatch.setenv("AUTH__PERSONAL_TOKENS", "false")
    get_settings.cache_clear()
    assert TestClient(app).get("/auth/me", headers=_bearer(personale)).status_code == 401
    assert TestClient(app).get("/auth/me", headers=_bearer(di_servizio)).status_code == 200


def test_un_account_di_servizio_non_ha_password(app, session):
    _utente(session, "capo@x.local", is_superuser=True)
    capo = _entra(app, "capo@x.local")
    acc = capo.post("/service-accounts", json={"name": "CI"}, headers=CSRF).json()
    assert capo.patch(f"/users/{acc['id']}", json={"password": "una-password-lunga"}, headers=CSRF).status_code == 422
    # e anche se ne avesse una, non entrerebbe col modulo
    u = session.get(User, acc["id"])
    u.hashed_password = hash_password(PASSWORD)
    session.add(u)
    session.commit()
    assert TestClient(app).post("/auth/login", json={"email": acc["email"], "password": PASSWORD}).status_code == 401


def test_un_token_non_crea_utenti_ne_imposta_password(app, session):
    """Un token di amministratore trapelato non deve potersi lasciare dietro una
    credenziale che duri oltre la sua revoca."""
    capo_id = _utente(session, "capo@x.local", is_superuser=True)
    token = _crea(_entra(app, "capo@x.local"), scope="write")["token"]
    script = TestClient(app)
    assert script.post("/users", json={"email": "nuovo@x.local", "password": "una-password-lunga"}, headers=_bearer(token)).status_code == 403
    assert script.patch(f"/users/{capo_id}", json={"password": "una-password-lunga"}, headers=_bearer(token)).status_code == 403


def test_l_osservatore_vede_i_token_di_un_account_mascherati(app, session):
    _utente(session, "capo@x.local", is_superuser=True)
    _utente(session, "occhio@x.local", is_observer=True)
    capo = _entra(app, "capo@x.local")
    acc = capo.post("/service-accounts", json={"name": "CI"}, headers=CSRF).json()
    capo.post(f"/service-accounts/{acc['id']}/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF)
    visto = _entra(app, "occhio@x.local").get(f"/service-accounts/{acc['id']}/tokens").json()[0]
    assert visto["prefix"] == "tab_…" and visto["last_used_ip"] is None


def test_un_token_di_sola_lettura_puo_esportare_e_fare_anteprime():
    """Export e anteprima sono POST che leggono soltanto."""
    from types import SimpleNamespace

    from fastapi import HTTPException

    riga = ApiToken(user_id=1, name="t", prefix="tab_x", token_hash="h", scope="read",
                    created_at=api_tokens.adesso(), expires_at=api_tokens.adesso())
    def richiesta(metodo, rotta):
        return SimpleNamespace(method=metodo, scope={"route": SimpleNamespace(path=rotta)}, headers={})
    for rotta in ("/tasks/export", "/tasks/preview"):
        api_tokens.controlla_livello(riga, richiesta("POST", rotta))
    for metodo, rotta in (("POST", "/tasks/transform-data"), ("POST", "/projects"), ("DELETE", "/tasks/export")):
        with pytest.raises(HTTPException):
            api_tokens.controlla_livello(riga, richiesta(metodo, rotta))


def test_la_pagina_sa_la_scadenza_massima(app, session, monkeypatch):
    monkeypatch.setenv("AUTH__API_TOKEN_MAX_DAYS", "30")
    get_settings.cache_clear()
    _utente(session, "a@x.local")
    assert _entra(app, "a@x.local").get("/auth/me").json()["api_token_max_days"] == 30


# ── dalla prova a scatola chiusa (2026-10-12) ───────────────────────────────

@pytest.fixture
def app_pieno(app):
    from app.routes import audit as audit_routes
    from app.routes import groups as groups_routes

    app.include_router(groups_routes.router)
    app.include_router(audit_routes.router)
    return app


def _capo_col_token(app, session):
    _utente(session, "capo@x.local", is_superuser=True)
    return _crea(_entra(app, "capo@x.local"), scope="write", name="trapelato")["token"]


def test_un_token_trapelato_non_lascia_un_amministratore_in_piu(app_pieno, session):
    """Revocato il token, non deve restare un'altra via d'ingresso: un account reso
    amministratore (flag, gruppo admin, appartenenza al gruppo) o osservatore."""
    from app.models import Group

    token = _capo_col_token(app_pieno, session)
    complice = _utente(session, "complice@x.local")
    admin = Group(name="Admin", is_admin=True)
    occhi = Group(name="Occhi", is_observer=True)
    normale = Group(name="Vendite")
    for g in (admin, occhi, normale):
        session.add(g)
    session.commit()
    admin, occhi, normale = (type("G", (), {"id": g.id}) for g in (admin, occhi, normale))  # solo gli id: le righe si staccano
    script = TestClient(app_pieno)
    h = {**_bearer(token), "Accept-Language": "it"}
    for metodo, percorso, corpo in (
        ("PATCH", f"/users/{complice}", {"is_superuser": True}),
        ("PATCH", f"/users/{complice}", {"is_observer": True}),
        ("PATCH", f"/groups/{normale.id}", {"is_admin": True}),
        ("PATCH", f"/groups/{normale.id}", {"is_observer": True}),
        ("PUT", f"/users/{complice}/groups/{admin.id}", None),
        ("PUT", f"/users/{complice}/groups/{occhi.id}", None),
    ):
        r = script.request(metodo, percorso, json=corpo, headers=h)
        assert r.status_code == 403 and "amministratore" in r.json()["detail"], (percorso, corpo, r.text)
    session.expire_all()
    assert session.get(User, complice).is_superuser is False
    # quel che non concede ruoli resta possibile: gruppi normali, togliere un ruolo
    assert script.put(f"/users/{complice}/groups/{normale.id}", headers=h).status_code == 204
    assert script.patch(f"/users/{complice}", json={"is_superuser": False, "full_name": "C"}, headers=h).status_code == 200


def test_disattivare_ritira_i_token_e_riattivare_non_li_riporta(app, session):
    _utente(session, "capo@x.local", is_superuser=True)
    capo = _entra(app, "capo@x.local")
    acc = capo.post("/service-accounts", json={"name": "CI"}, headers=CSRF).json()
    token = capo.post(f"/service-accounts/{acc['id']}/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF).json()["token"]
    assert capo.patch(f"/users/{acc['id']}", json={"is_active": False}, headers=CSRF).status_code == 200
    assert capo.patch(f"/users/{acc['id']}", json={"is_active": True}, headers=CSRF).status_code == 200
    assert TestClient(app).get("/auth/me", headers=_bearer(token)).status_code == 401
    assert session.exec(select(ApiToken)).all() == []


def test_un_token_non_riattiva_un_account(app, session):
    token = _capo_col_token(app, session)
    ex = _utente(session, "ex@x.local", is_active=False)
    r = TestClient(app).patch(f"/users/{ex}", json={"is_active": True}, headers=_bearer(token))
    assert r.status_code == 403
    session.expire_all()
    assert session.get(User, ex).is_active is False


def test_chi_diventa_osservatore_perde_i_token_personali(app_pieno, session):
    from app.models import Group, UserGroupLink

    _utente(session, "capo@x.local", is_superuser=True)
    capo = _entra(app_pieno, "capo@x.local")
    ids = {e: _utente(session, e) for e in ("a@x.local", "b@x.local", "c@x.local")}
    token = {e: _crea(_entra(app_pieno, e))["token"] for e in ids}
    acc = capo.post("/service-accounts", json={"name": "CI"}, headers=CSRF).json()
    t_servizio = capo.post(f"/service-accounts/{acc['id']}/tokens", json={"name": "x", "expires_in_days": 5}, headers=CSRF).json()["token"]
    # tre strade: il flag, l'ingresso in un gruppo di osservatori, il flag del gruppo
    capo.patch(f"/users/{ids['a@x.local']}", json={"is_observer": True}, headers=CSRF)
    occhi = capo.post("/groups", json={"name": "Occhi"}, headers=CSRF).json()["id"]
    capo.patch(f"/groups/{occhi}", json={"is_observer": True}, headers=CSRF)
    capo.put(f"/users/{ids['b@x.local']}/groups/{occhi}", headers=CSRF)
    vendite = capo.post("/groups", json={"name": "Vendite"}, headers=CSRF).json()["id"]
    capo.put(f"/users/{ids['c@x.local']}/groups/{vendite}", headers=CSRF)
    capo.put(f"/users/{acc['id']}/groups/{vendite}", headers=CSRF)
    assert TestClient(app_pieno).get("/auth/me", headers=_bearer(token["c@x.local"])).status_code == 200
    capo.patch(f"/groups/{vendite}", json={"is_observer": True}, headers=CSRF)
    for e in ids:
        assert TestClient(app_pieno).get("/auth/me", headers=_bearer(token[e])).status_code == 401, e
    # quelli di un account di servizio restano: sono dell'integrazione, non di una persona
    assert TestClient(app_pieno).get("/auth/me", headers=_bearer(t_servizio)).status_code == 200


def test_nan_e_infinity_sono_un_422_non_un_500(app, session):
    from fastapi.exceptions import RequestValidationError

    from app.main import _richiesta_non_valida

    app.add_exception_handler(RequestValidationError, _richiesta_non_valida)  # quello dell'app vera
    _utente(session, "a@x.local")
    browser = _entra(app, "a@x.local")
    for valore in ("NaN", "Infinity", "-Infinity"):
        r = browser.post("/auth/tokens", content='{"name": "x", "expires_in_days": %s}' % valore,
                         headers={**CSRF, "content-type": "application/json"})
        assert r.status_code == 422 and r.json()["detail"][0]["loc"] == ["body", "expires_in_days"], r.text
    r = TestClient(app).post("/auth/login", content='{"email": NaN, "password": "x"}', headers={"content-type": "application/json"})
    assert r.status_code == 422


def test_all_osservatore_l_audit_non_dice_l_inizio_dei_token_ne_si_cerca_per_ip(app_pieno, session):
    from app.models import AuditLog as Voce

    _utente(session, "capo@x.local", is_superuser=True)
    _utente(session, "occhio@x.local", is_observer=True)
    capo = _entra(app_pieno, "capo@x.local")
    _crea(capo, name="report")
    session.add(Voce(ts=api_tokens.adesso(), actor_label="anna@x.local", action="flow.create", ip="203.0.113.7",
                     target_type="flow", target_label="Margini", outcome="success"))
    session.commit()
    occhio = _entra(app_pieno, "occhio@x.local")
    voce = occhio.get("/audit", params={"action": "token.create"}).json()["items"][0]
    assert voce["detail"]["prefix"] == "tab_…"
    assert capo.get("/audit", params={"action": "token.create"}).json()["items"][0]["detail"]["prefix"].startswith("tab_")
    for q in ("203.0.113", "anna@"):
        assert occhio.get("/audit", params={"q": q}).json()["total"] == 0, q
        assert capo.get("/audit", params={"q": q}).json()["total"] >= 1, q
    assert occhio.get("/audit", params={"q": "Margini"}).json()["total"] == 1  # quel che vede, lo cerca


def test_chi_resta_solo_osservatore_perdendo_l_amministrazione_non_usa_i_token_personali(app_pieno, session):
    """Le strade per diventarlo sono tante (flag tolto, uscita dal gruppo admin,
    gruppo declassato o cancellato, sincronizzazione SSO): la regola la fa valere
    il punto in cui il token si accetta, nella stessa SELECT."""
    from app.models import Group, UserGroupLink

    _utente(session, "capo@x.local", is_superuser=True)
    capo = _entra(app_pieno, "capo@x.local")
    a = _utente(session, "a@x.local", is_superuser=True, is_observer=True)
    b = _utente(session, "b@x.local", is_observer=True)
    g = Group(name="Admin", is_admin=True)
    session.add(g)
    session.commit()
    gid = g.id
    session.add(UserGroupLink(user_id=b, group_id=gid))
    session.commit()
    token = {e: _crea(_entra(app_pieno, e))["token"] for e in ("a@x.local", "b@x.local")}
    for t in token.values():
        assert TestClient(app_pieno).get("/auth/me", headers=_bearer(t)).status_code == 200
    capo.patch(f"/users/{a}", json={"is_superuser": False}, headers=CSRF)
    capo.delete(f"/users/{b}/groups/{gid}", headers=CSRF)
    for e, t in token.items():
        assert TestClient(app_pieno).get("/auth/me", headers=_bearer(t)).status_code == 401, e


@pytest.mark.anyio
async def test_nan_nel_corpo_inoltrato_all_engine_e_un_422():
    """Il gateway inoltra il corpo all'engine: NaN passava il parser di Python e
    l'engine rispondeva 500. Non è JSON, si ferma al confine."""
    from fastapi import HTTPException
    from starlette.requests import Request

    from app.routes.proxy import _read_json

    for corpo in (b'{"limit": NaN}', b'{"x": [Infinity]}', b'{"x": -Infinity}'):
        async def receive(corpo=corpo):
            return {"type": "http.request", "body": corpo, "more_body": False}
        with pytest.raises(HTTPException) as e:
            await _read_json(Request({"type": "http", "method": "POST", "headers": [], "path": "/"}, receive))
        assert e.value.status_code == 422
