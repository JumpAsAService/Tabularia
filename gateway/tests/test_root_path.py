"""Il gateway pubblicato sotto un prefisso (`APP__ROOT_PATH`).

Quando un solo host serve interfaccia e API, il reverse proxy manda
`/api/*` al gateway togliendo il prefisso, perché gateway e frontend hanno
percorsi omonimi (`/flows`, `/runs`…) e non si possono distinguere altrimenti.
Il gateway deve SAPERE quel prefisso per una cosa che il proxy non vede: il
cookie della transazione SSO è ristretto a un percorso, e se il percorso non è
quello che vede il browser il cookie non torna indietro e l'accesso fallisce
con `tx_missing` — dopo che l'utente si è già autenticato sull'IdP.
"""
import pytest
from pydantic import ValidationError

from app.core.config import AppSettings, Settings
from app.routes import sso as sso_routes
from app.services import sso
from tests.test_sso import FakeRequest

OIDC = {"issuer": "https://idp/realms/t", "client_id": "c", "client_secret": "s",
        "redirect_uri": "https://app.example.com/api/auth/sso/callback",
        "post_login_url": "https://app.example.com/auth/callback"}


def _login(monkeypatch, **app):
    cfg = Settings(app=app, oidc=OIDC)
    monkeypatch.setattr(sso_routes, "get_settings", lambda: cfg)
    monkeypatch.setattr(sso_routes.sso, "build_authorize_url", lambda *a, **k: "https://idp/authorize")
    return sso_routes.sso_login(FakeRequest())


def test_senza_prefisso_il_cookie_resta_dov_era(monkeypatch):
    res = _login(monkeypatch)
    assert "Path=/auth/sso;" in res.headers["set-cookie"] + ";"


def test_il_cookie_di_transazione_segue_il_prefisso_pubblico(monkeypatch):
    res = _login(monkeypatch, root_path="/api")
    assert "Path=/api/auth/sso;" in res.headers["set-cookie"] + ";"


def test_anche_la_cancellazione_del_cookie_usa_il_prefisso(session, monkeypatch):
    # un cookie si cancella solo ripetendo il percorso con cui è stato scritto
    cfg = Settings(app={"root_path": "/api"}, oidc=OIDC)
    monkeypatch.setattr(sso_routes, "get_settings", lambda: cfg)
    res = sso_routes.sso_callback(FakeRequest(), code="abc", state="s", session=session)
    assert "error=tx_missing" in res.headers["location"]
    assert "Path=/api/auth/sso" in res.headers["set-cookie"]


@pytest.mark.parametrize("scritto, letto", [("", ""), ("/", ""), ("/api", "/api"), ("/api/", "/api"), ("api", "/api")])
def test_il_prefisso_viene_normalizzato(scritto, letto):
    assert AppSettings(root_path=scritto).root_path == letto


def test_un_prefisso_che_non_e_un_percorso_viene_rifiutato():
    for sbagliato in ("https://app.example.com/api", "/api?x=1", "/a b"):
        with pytest.raises(ValidationError):
            AppSettings(root_path=sbagliato)


def test_sotto_un_prefisso_la_barra_finale_non_reindirizza_fuori():
    """Con una barra di troppo FastAPI risponde 307 verso l'indirizzo senza barra,
    e quell'indirizzo lo costruisce SENZA il prefisso: dietro il proxy porterebbe
    `/api/users/` su `/users`, che è una pagina del frontend. Sotto un prefisso
    il reindirizzamento si spegne: un 404 dice la verità, un 307 sbagliato no."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    def app_con(impostazioni: AppSettings) -> TestClient:
        app = FastAPI(root_path=impostazioni.root_path, redirect_slashes=impostazioni.redirect_slashes)
        app.get("/users")(lambda: [])
        return TestClient(app, follow_redirects=False, base_url="https://app.example.com")

    alla_radice = app_con(AppSettings()).get("/users/")
    assert alla_radice.status_code == 307  # in sviluppo resta com'era

    sotto_api = app_con(AppSettings(root_path="/api")).get("/users/")
    assert sotto_api.status_code == 404 and "location" not in sotto_api.headers
    assert app_con(AppSettings(root_path="/api")).get("/users").status_code == 200


def test_l_applicazione_e_costruita_con_quelle_impostazioni():
    from app import main
    from app.core.config import get_settings

    cfg = get_settings().app
    assert main.app.root_path == cfg.root_path
    assert main.app.router.redirect_slashes == cfg.redirect_slashes


# ── La domanda che il proxy fa al gateway prima di aprire Grafana ────────────
def test_la_rotta_per_il_proxy_e_chiusa_dalla_guardia_degli_osservatori():
    """Quando Grafana sta dietro un reverse proxy, è il proxy a chiedere
    al gateway se chi bussa può vedere il monitoraggio (`forward_auth`). La
    rotta non deve rispondere niente e non deve costare niente: viene chiamata a
    ogni richiesta di ogni pannello. Conta solo CHI la può chiamare."""
    from app.deps.auth import require_observer
    from app.routes import auth as auth_routes

    rotta = next(r for r in auth_routes.router.routes if r.path == "/auth/observer")
    assert rotta.methods == {"GET"} and rotta.status_code == 204
    assert require_observer in [d.call for d in rotta.dependant.dependencies]
    assert auth_routes.puo_osservare() is None



def test_la_rotta_risponde_solo_a_chi_puo_osservare(session):
    """La stessa domanda, fatta davvero: senza token, con un token qualunque,
    da utente normale, da osservatore, da amministratore."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.security import create_access_token
    from app.db.session import get_session
    from app.routes import auth as auth_routes
    from tests.conftest import make_user

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)

    normale = make_user(session, email="normale@x.local")
    osservatore = make_user(session, email="osservatore@x.local", is_observer=True)
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    portatore = lambda u: {"Authorization": f"Bearer {create_access_token(u.id)}"}

    assert client.get("/auth/observer").status_code == 401
    # ciò che il proxy manda quando il cookie non c'è: «Bearer » e basta
    assert client.get("/auth/observer", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.get("/auth/observer", headers={"Authorization": "Bearer non-un-token"}).status_code == 401
    assert client.get("/auth/observer", headers=portatore(normale)).status_code == 403
    for chi in (osservatore, admin):
        risposta = client.get("/auth/observer", headers=portatore(chi))
        assert risposta.status_code == 204 and risposta.content == b""
