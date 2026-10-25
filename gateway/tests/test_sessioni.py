"""Sessioni: il token vale finché la sua riga esiste, e la si può chiudere.

Prima il JWT era senza stato: l'uscita cancellava il cookie solo nel browser, e
chi avesse copiato il token lo usava fino alla scadenza (12 ore). Nessuno poteva
buttare fuori nessuno, e cambiare password non chiudeva niente. Il token stava
in un cookie che il JavaScript leggeva. Qui le regole nuove, provate dalle rotte
vere con un client HTTP: cookie HttpOnly, intestazione anti-CSRF, uscita vera,
revoca propria e dell'amministratore, password e disattivazione che chiudono,
durata massima, inattività, pulizia.
"""
import re
from datetime import timedelta

import jwt as pyjwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.core.security import create_access_token, hash_password
from app.db.session import get_session
from app.models import AuditLog, User, UserSession
from app.routes import audit as audit_routes
from app.routes import auth as auth_routes
from app.routes import users as users_routes
from app.services import sessions
from tests.conftest import make_user

PASSWORD = "una-password-lunga"
CSRF = {"X-Tabularia-Csrf": "1"}


@pytest.fixture
def app(session):
    app = FastAPI()
    for r in (auth_routes.router, audit_routes.router, users_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _user(session, email, **kw) -> int:
    """Crea l'utente e ne torna l'id: gli oggetti letti prima di una richiesta
    escono dalla sessione quando la richiesta la rilascia."""
    return make_user(session, email=email, hashed_password=hash_password(PASSWORD), **kw).id


def _row(session, sid: str) -> UserSession | None:
    session.expire_all()
    return session.get(UserSession, sid)


def _sposta(session, sid: str, **fields) -> None:
    row = _row(session, sid)
    for k, v in fields.items():
        setattr(row, k, v)
    session.add(row)
    session.commit()


def _entra(app, email, **headers) -> TestClient:
    """Un browser che fa l'accesso: da qui in poi ha il cookie."""
    client = TestClient(app)
    r = client.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=headers)
    assert r.status_code == 200, r.text
    return client


def _sid(token: str) -> str:
    return pyjwt.decode(token, options={"verify_signature": False})["sid"]


def _cookie(client: TestClient) -> str:
    return client.cookies.get(sessions.COOKIE)


def _idle(monkeypatch, minutes: int):
    monkeypatch.setenv("AUTH__SESSION_IDLE_MINUTES", str(minutes))
    get_settings.cache_clear()


# ── accesso e cookie ─────────────────────────────────────────────────────────

def test_l_accesso_apre_una_sessione_e_la_consegna_in_un_cookie_httponly(app, session):
    uid = _user(session, "a@x.local")
    client = TestClient(app)
    r = client.post("/auth/login", json={"email": "a@x.local", "password": PASSWORD},
                    headers={"User-Agent": "Firefox di prova"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    cookie = r.headers["set-cookie"]
    # il JavaScript della pagina non lo legge; lo vede anche il server dell'interfaccia
    assert cookie.startswith(f"tab_token={token};")
    assert "HttpOnly" in cookie and "Path=/" in cookie and "SameSite=lax" in cookie
    # dura quanto la sessione
    duration = int(re.search(r"Max-Age=(\d+)", cookie).group(1))
    assert get_settings().jwt.access_ttl_minutes * 60 - 5 <= duration <= get_settings().jwt.access_ttl_minutes * 60
    # in http niente Secure (sviluppo su localhost); dietro un proxy in HTTPS sì
    assert "Secure" not in cookie
    row = _row(session, _sid(token))
    assert row.user_id == uid and row.method == "password"
    assert row.ip == "testclient" and row.user_agent == "Firefox di prova"


def test_dietro_un_proxy_in_https_il_cookie_e_secure(app, session):
    _user(session, "a@x.local")
    r = TestClient(app).post("/auth/login", json={"email": "a@x.local", "password": PASSWORD},
                             headers={"X-Forwarded-Proto": "https"})
    assert "Secure" in r.headers["set-cookie"]


def test_il_cookie_basta_per_leggere(app, session):
    _user(session, "a@x.local")
    client = _entra(app, "a@x.local")
    r = client.get("/auth/me")
    assert r.status_code == 200 and r.json()["email"] == "a@x.local"


def test_col_solo_cookie_una_richiesta_che_modifica_vuole_l_intestazione_anti_csrf(app, session):
    """Il cookie il browser lo allega anche a un modulo partito da un altro sito;
    l'intestazione no."""
    _user(session, "a@x.local")
    client = _entra(app, "a@x.local")
    r = client.post("/auth/sessions/close-others")
    assert r.status_code == 403 and "X-Tabularia-Csrf" in r.json()["detail"]
    assert client.post("/auth/sessions/close-others", headers=CSRF).status_code == 204


def test_con_l_intestazione_authorization_l_anti_csrf_non_serve(app, session):
    """Uno script, o una pagina di un altro sito, non può mettere l'intestazione
    Authorization senza conoscere il token: lì il CSRF non c'è."""
    u = session.get(User, _user(session, "a@x.local"))
    token = sessions.open_session(session, u, None, "password")[1]
    r = TestClient(app).post("/auth/sessions/close-others", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 204


def test_senza_token_ne_cookie_401(app):
    assert TestClient(app).get("/auth/me").status_code == 401


# ── l'uscita è vera ──────────────────────────────────────────────────────────

def test_l_uscita_chiude_la_sessione_anche_per_chi_ha_copiato_il_token(app, session):
    _user(session, "a@x.local")
    client = _entra(app, "a@x.local")
    copiato = _cookie(client)
    r = client.post("/auth/logout", headers=CSRF)
    assert r.status_code == 204
    # il cookie si cancella nel browser…
    assert r.headers["set-cookie"].startswith('tab_token=""') and "Max-Age=0" in r.headers["set-cookie"]
    # …e il token non vale più nemmeno altrove
    assert TestClient(app).get("/auth/me", headers={"Authorization": f"Bearer {copiato}"}).status_code == 401
    assert _row(session, _sid(copiato)) is None
    assert session.exec(select(AuditLog).where(AuditLog.action == "auth.logout")).first() is not None


def test_un_401_dato_a_un_cookie_lo_cancella(app, session):
    """Altrimenti il server dell'interfaccia continuerebbe a vedere un cookie e a
    credere che ci sia una sessione, a ogni pagina caricata da capo."""
    uid = _user(session, "a@x.local")
    client = _entra(app, "a@x.local")
    sessions.close_all(session, uid)
    r = client.get("/auth/me")
    assert r.status_code == 401
    assert r.headers["set-cookie"].startswith('tab_token=""') and "Max-Age=0" in r.headers["set-cookie"]


def test_un_token_senza_sessione_non_vale(app, session):
    """Un token di prima delle sessioni (senza `sid`) non si potrebbe revocare:
    all'aggiornamento si rientra una volta, poi basta."""
    uid = _user(session, "a@x.local")
    s = get_settings().jwt
    old = pyjwt.encode({"sub": str(uid), "exp": 9_999_999_999}, s.secret.get_secret_value(), algorithm=s.algorithm)
    for token in (old, create_access_token(uid, "inventata")):
        assert TestClient(app).get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 401


# ── durata e inattività ──────────────────────────────────────────────────────

def test_oltre_la_durata_massima_non_vale(app, session):
    _user(session, "a@x.local")
    client = _entra(app, "a@x.local")
    _sposta(session, _sid(_cookie(client)), expires_at=sessions.now() - timedelta(seconds=1))
    assert client.get("/auth/me").status_code == 401


def test_ferma_oltre_il_timeout_per_inattivita_non_vale(app, session, monkeypatch):
    _idle(monkeypatch, 30)
    _user(session, "a@x.local")
    client = _entra(app, "a@x.local")
    sid = _sid(_cookie(client))
    # venti minuti fa: vale, e l'attività si aggiorna
    _sposta(session, sid, last_seen_at=sessions.now() - timedelta(minutes=20))
    assert client.get("/auth/me").status_code == 200
    assert sessions.now() - _row(session, sid).last_seen_at < timedelta(seconds=5)
    # quaranta minuti fa: non vale più
    _sposta(session, sid, last_seen_at=sessions.now() - timedelta(minutes=40))
    assert client.get("/auth/me").status_code == 401


def test_senza_timeout_conta_solo_la_durata_massima(app, session, monkeypatch):
    _idle(monkeypatch, 0)
    _user(session, "a@x.local")
    client = _entra(app, "a@x.local")
    _sposta(session, _sid(_cookie(client)), last_seen_at=sessions.now() - timedelta(hours=11))
    assert client.get("/auth/me").status_code == 200


def test_la_pagina_sa_dopo_quanto_uscire_da_sola(app, session, monkeypatch):
    _idle(monkeypatch, 45)
    _user(session, "a@x.local")
    assert _entra(app, "a@x.local").get("/auth/me").json()["session_idle_minutes"] == 45


def test_la_pulizia_toglie_le_scadute_e_le_ferme(session, monkeypatch):
    _idle(monkeypatch, 30)
    u = session.get(User, _user(session, "a@x.local"))
    viva, expired, stop = (sessions.open_session(session, u, None, "password")[0] for _ in range(3))
    ids = viva.id, expired.id, stop.id
    expired.expires_at = sessions.now() - timedelta(minutes=1)
    stop.last_seen_at = sessions.now() - timedelta(hours=1)
    session.add_all([expired, stop])
    session.commit()
    assert sessions.clean(session) == 2
    session.expire_all()
    assert [s.id for s in session.exec(select(UserSession)).all()] == [ids[0]]


# ── le proprie sessioni ──────────────────────────────────────────────────────

def test_ognuno_vede_e_chiude_solo_le_sue(app, session):
    _user(session, "a@x.local")
    _user(session, "b@x.local")
    qui, altrove = _entra(app, "a@x.local"), _entra(app, "a@x.local")
    estraneo = _entra(app, "b@x.local")

    listing = qui.get("/auth/sessions").json()
    assert len(listing) == 2
    assert [s["current"] for s in listing if s["id"] == _sid(_cookie(qui))] == [True]

    # quella di un altro non si vede: 404, come se non esistesse
    assert estraneo.delete(f"/auth/sessions/{_sid(_cookie(qui))}", headers=CSRF).status_code == 404
    assert qui.get("/auth/me").status_code == 200

    assert qui.delete(f"/auth/sessions/{_sid(_cookie(altrove))}", headers=CSRF).status_code == 204
    assert altrove.get("/auth/me").status_code == 401
    assert qui.get("/auth/me").status_code == 200
    item = session.exec(select(AuditLog).where(AuditLog.action == "auth.session_revoke")).first()
    assert item is not None and item.target_label == "a@x.local"


def test_esci_dagli_altri_dispositivi_lascia_aperta_questa(app, session):
    _user(session, "a@x.local")
    qui, altrove, ancora = (_entra(app, "a@x.local") for _ in range(3))
    assert qui.post("/auth/sessions/close-others", headers=CSRF).status_code == 204
    assert qui.get("/auth/me").status_code == 200
    assert altrove.get("/auth/me").status_code == 401 and ancora.get("/auth/me").status_code == 401


# ── l'amministratore ─────────────────────────────────────────────────────────

def test_l_amministratore_vede_tutte_le_sessioni_e_ne_chiude_una(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    _user(session, "a@x.local", full_name="Anna")
    capo = _entra(app, "capo@x.local")
    anna = _entra(app, "a@x.local", **{"User-Agent": "Firefox di Anna"})

    listing = capo.get("/audit/sessions").json()["items"]
    di_anna = next(s for s in listing if s["email"] == "a@x.local")
    assert di_anna["full_name"] == "Anna" and di_anna["user_agent"] == "Firefox di Anna"
    assert di_anna["ip"] == "testclient" and di_anna["online"] and not di_anna["current"]
    assert [s["current"] for s in listing if s["email"] == "capo@x.local"] == [True]

    assert capo.delete(f"/audit/sessions/{di_anna['id']}", headers=CSRF).status_code == 204
    assert anna.get("/auth/me").status_code == 401
    item = session.exec(select(AuditLog).where(AuditLog.action == "auth.session_revoke")).first()
    assert item.actor_label == "capo@x.local" and item.target_label == "a@x.local"
    assert capo.delete(f"/audit/sessions/{di_anna['id']}", headers=CSRF).status_code == 404


def test_l_osservatore_vede_le_sessioni_senza_chi_ne_da_dove_e_non_chiude(app, session):
    _user(session, "occhio@x.local", is_observer=True)
    _user(session, "a@x.local", full_name="Anna")
    occhio = _entra(app, "occhio@x.local")
    anna = _entra(app, "a@x.local", **{"User-Agent": "Firefox di Anna"})

    # all'osservatore arriva una maniglia, non l'id vero
    di_anna = next(s for s in occhio.get("/audit/sessions").json()["items"] if s["id"] == sessions.handle(_sid(_cookie(anna))))
    assert di_anna["email"] != "a@x.local" and di_anna["full_name"] == ""
    assert di_anna["ip"] is None and di_anna["user_agent"] is None
    assert occhio.delete(f"/audit/sessions/{di_anna['id']}", headers=CSRF).status_code == 403
    assert occhio.delete(f"/audit/sessions/{_sid(_cookie(anna))}", headers=CSRF).status_code == 403
    assert anna.get("/auth/me").status_code == 200


def test_di_un_osservatore_la_sessione_non_tiene_indirizzo_ne_browser(app, session):
    _user(session, "occhio@x.local", is_observer=True)
    client = _entra(app, "occhio@x.local", **{"User-Agent": "Firefox"})
    row = _row(session, _sid(_cookie(client)))
    assert row.ip is None and row.user_agent is None


def test_una_password_nuova_chiude_le_sessioni_dell_utente(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    a = _user(session, "a@x.local")
    capo = _entra(app, "capo@x.local")
    anna, anna_altrove = _entra(app, "a@x.local"), _entra(app, "a@x.local")
    r = capo.patch(f"/users/{a}", json={"password": "un-altra-password"}, headers=CSRF)
    assert r.status_code == 200
    assert anna.get("/auth/me").status_code == 401 and anna_altrove.get("/auth/me").status_code == 401
    item = session.exec(select(AuditLog).where(AuditLog.action == "auth.session_revoke")).first()
    assert item.detail and '"password"' in item.detail


def test_chi_cambia_la_propria_password_resta_dentro_da_dove_lo_fa(app, session):
    capo_id = _user(session, "capo@x.local", is_superuser=True)
    qui, altrove = _entra(app, "capo@x.local"), _entra(app, "capo@x.local")
    assert qui.patch(f"/users/{capo_id}", json={"password": "un-altra-password"}, headers=CSRF).status_code == 200
    assert qui.get("/auth/me").status_code == 200
    assert altrove.get("/auth/me").status_code == 401


def test_disattivare_un_account_ne_chiude_le_sessioni(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    a = _user(session, "a@x.local")
    capo = _entra(app, "capo@x.local")
    _entra(app, "a@x.local")
    assert capo.patch(f"/users/{a}", json={"is_active": False}, headers=CSRF).status_code == 200
    session.expire_all()
    assert session.exec(select(UserSession).where(UserSession.user_id == a)).all() == []


def test_cambiare_solo_il_nome_non_chiude_niente(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    a = _user(session, "a@x.local")
    capo = _entra(app, "capo@x.local")
    anna = _entra(app, "a@x.local")
    assert capo.patch(f"/users/{a}", json={"full_name": "Anna B."}, headers=CSRF).status_code == 200
    assert anna.get("/auth/me").status_code == 200


def test_eliminare_un_utente_collegato_toglie_anche_le_sue_sessioni(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    a = _user(session, "a@x.local")
    capo = _entra(app, "capo@x.local")
    _entra(app, "a@x.local")
    assert capo.delete(f"/users/{a}", headers=CSRF).status_code == 204
    session.expire_all()
    assert session.exec(select(UserSession).where(UserSession.user_id == a)).all() == []


# ── dalla revisione ──────────────────────────────────────────────────────────

def test_l_ospite_condiviso_vede_e_chiude_solo_la_propria_sessione(app, session):
    """L'osservatore è l'account di una demo pubblica, in cui entrano tutti: con
    «chiudi le altre» butterebbe fuori ogni altro visitatore."""
    _user(session, "ospite@x.local", is_observer=True)
    io, altro_visitatore = _entra(app, "ospite@x.local"), _entra(app, "ospite@x.local")
    listing = io.get("/auth/sessions").json()
    assert [s["id"] for s in listing] == [_sid(_cookie(io))]
    assert io.post("/auth/sessions/close-others", headers=CSRF).status_code == 403
    assert io.delete(f"/auth/sessions/{_sid(_cookie(altro_visitatore))}", headers=CSRF).status_code == 403
    assert altro_visitatore.get("/auth/me").status_code == 200
    # la propria la chiude: è uscire
    assert io.delete(f"/auth/sessions/{_sid(_cookie(io))}", headers=CSRF).status_code == 204
    assert altro_visitatore.get("/auth/me").status_code == 200


def test_all_osservatore_l_audit_non_consegna_gli_id_veri(app, session):
    _user(session, "occhio@x.local", is_observer=True)
    _user(session, "a@x.local")
    occhio, anna = _entra(app, "occhio@x.local"), _entra(app, "a@x.local")
    ids = {s["id"] for s in occhio.get("/audit/sessions").json()["items"]}
    assert _sid(_cookie(anna)) not in ids and all(i.startswith("h-") for i in ids)


def test_all_interfaccia_il_token_non_torna_nel_corpo(app, session):
    """L'interfaccia manda l'intestazione anti-CSRF anche all'accesso: le basta il
    cookie, e un token nel corpo lo potrebbe leggere uno script della pagina."""
    _user(session, "a@x.local")
    client = TestClient(app)
    r = client.post("/auth/login", json={"email": "a@x.local", "password": PASSWORD}, headers=CSRF)
    assert r.status_code == 200 and r.json()["access_token"] == ""
    assert client.get("/auth/me").status_code == 200
    # uno script (senza l'intestazione) lo riceve come prima
    assert TestClient(app).post("/auth/login", json={"email": "a@x.local", "password": PASSWORD}).json()["access_token"]


def test_un_accesso_nuovo_dallo_stesso_browser_chiude_la_sessione_di_prima(app, session):
    _user(session, "a@x.local")
    _user(session, "b@x.local")
    browser = _entra(app, "a@x.local")
    before = _cookie(browser)
    r = browser.post("/auth/login", json={"email": "b@x.local", "password": PASSWORD})
    assert r.status_code == 200
    assert _row(session, _sid(before)) is None
    assert browser.get("/auth/me").json()["email"] == "b@x.local"


def test_un_bearer_respinto_non_cancella_il_cookie_buono(app, session):
    """Un token di script scaduto provato da /docs, nello stesso browser: il 401 è
    suo, il cookie dell'interfaccia resta."""
    _user(session, "a@x.local")
    client = _entra(app, "a@x.local")
    r = client.get("/auth/me", headers={"Authorization": "Bearer non-un-token"})
    assert r.status_code == 401 and "set-cookie" not in r.headers
    assert client.get("/auth/me").status_code == 200


def test_le_origini_cors_non_ammettono_l_asterisco(monkeypatch):
    """Con le credenziali, Starlette rispecchierebbe qualunque origine: ogni sito
    visitato userebbe la sessione di chi lo visita."""
    from pydantic import ValidationError

    from app.core.config import Settings

    for cattiva in ('["*"]', '["null"]', '["https://app.example.com", "*"]'):
        monkeypatch.setenv("APP__CORS_ORIGINS", cattiva)
        with pytest.raises(ValidationError):
            Settings()
    monkeypatch.setenv("APP__CORS_ORIGINS", '["https://app.example.com"]')
    assert Settings().app.cors_origins == ["https://app.example.com"]


def test_col_dominio_configurato_il_cookie_lo_porta(app, session, monkeypatch):
    """Interfaccia e gateway su due host dello stesso dominio: il server
    dell'interfaccia deve vedere il cookie."""
    monkeypatch.setenv("AUTH__COOKIE_DOMAIN", "example.com")
    get_settings.cache_clear()
    _user(session, "a@x.local")
    r = TestClient(app).post("/auth/login", json={"email": "a@x.local", "password": PASSWORD})
    assert "Domain=example.com" in r.headers["set-cookie"]


def test_il_proxy_non_inoltra_all_engine_cookie_ne_intestazioni_della_sessione():
    from app.routes import proxy

    richiesta = type("R", (), {"headers": {
        "Cookie": "tab_token=segreto", "Authorization": "Bearer segreto",
        "X-Tabularia-Csrf": "1", "Accept": "application/json",
    }})()
    inoltrate = {k.lower() for k in proxy._request_headers(richiesta)}
    assert inoltrate == {"accept"}


# ── dalla seconda revisione ──────────────────────────────────────────────────

def test_una_password_cambiata_mentre_si_entra_non_apre_la_sessione(app, session, monkeypatch):
    """Fra il controllo della password (bcrypt, un quarto di secondo) e l'apertura
    della sessione un amministratore la cambia e chiude le sessioni: chi entrava
    con la vecchia non deve trovarsi una sessione nata dopo la chiusura."""
    from app.routes import auth as auth_routes

    uid = _user(session, "a@x.local")
    vera = auth_routes.verify_password

    def controlla_e_intanto_cambia(password, hashed):
        outcome = vera(password, hashed)
        # il cambio dell'amministratore, che arriva a controllo finito
        u = session.get(User, uid)
        u.hashed_password = hash_password("la-password-nuova")
        session.add(u)
        sessions.close_all(session, uid, commit=False)
        session.commit()
        return outcome

    monkeypatch.setattr(auth_routes, "verify_password", controlla_e_intanto_cambia)
    r = TestClient(app).post("/auth/login", json={"email": "a@x.local", "password": PASSWORD})
    assert r.status_code == 401
    session.expire_all()
    assert session.exec(select(UserSession).where(UserSession.user_id == uid)).all() == []
    item = session.exec(select(AuditLog).where(AuditLog.action == "auth.login_failed")).first()
    assert item is not None and "cambiata" in (item.detail or "")


def test_gli_elenchi_non_portano_le_sessioni_scadute_o_ferme(app, session, monkeypatch):
    _idle(monkeypatch, 30)
    _user(session, "capo@x.local", is_superuser=True)
    capo = _entra(app, "capo@x.local")
    expired, stop = _entra(app, "capo@x.local"), _entra(app, "capo@x.local")
    _sposta(session, _sid(_cookie(expired)), expires_at=sessions.now() - timedelta(minutes=1))
    _sposta(session, _sid(_cookie(stop)), last_seen_at=sessions.now() - timedelta(hours=1))
    assert [s["current"] for s in capo.get("/auth/sessions").json()] == [True]
    pagina = capo.get("/audit/sessions").json()
    assert pagina["total"] == 1 and len(pagina["items"]) == 1


def test_l_elenco_dell_audit_porta_le_piu_recenti_e_il_totale(app, session):
    _user(session, "capo@x.local", is_superuser=True)
    _user(session, "a@x.local")
    capo = _entra(app, "capo@x.local")
    for _ in range(3):
        _entra(app, "a@x.local")
    pagina = capo.get("/audit/sessions?limit=2").json()
    assert len(pagina["items"]) == 2 and pagina["total"] == 4


def test_all_osservatore_il_registro_non_mostra_la_persona_ne_il_browser(app, session):
    """Le chiusure di sessione hanno per bersaglio l'utente: in chiaro accanto
    alla maschera dell'attore direbbero chi è."""
    _user(session, "capo@x.local", is_superuser=True)
    _user(session, "occhio@x.local", is_observer=True)
    _user(session, "anna@x.local")
    capo = _entra(app, "capo@x.local", **{"User-Agent": "Firefox del capo"})
    anna = _entra(app, "anna@x.local")
    capo.delete(f"/audit/sessions/{_sid(_cookie(anna))}", headers=CSRF)
    occhio = _entra(app, "occhio@x.local")
    items = occhio.get("/audit?action=auth.session_revoke").json()["items"]
    assert items and all("anna@x.local" not in (v["target_label"] or "") for v in items)
    assert all(v["user_agent"] is None and v["ip"] is None for v in items)
    # l'amministratore li vede
    full = capo.get("/audit?action=auth.session_revoke").json()["items"]
    assert any(v["target_label"] == "anna@x.local" for v in full) and all(v["user_agent"] for v in full)


def test_l_indirizzo_sta_solo_nella_sessione(app, session):
    """`users.last_seen_ip` non si scrive più: nessuno lo leggeva e nessuno
    l'avrebbe cancellato. L'indirizzo sta nella sessione e se ne va con lei."""
    uid = _user(session, "a@x.local")
    client = _entra(app, "a@x.local")
    assert client.get("/auth/me").status_code == 200
    session.expire_all()
    assert session.get(User, uid).last_seen_ip is None
    assert session.get(User, uid).last_seen_at is not None
    assert _row(session, _sid(_cookie(client))).ip == "testclient"
