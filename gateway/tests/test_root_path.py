"""The gateway published under a prefix (`APP__ROOT_PATH`).

When one host serves interface and API, the reverse proxy sends `/api/*` to the
gateway removing the prefix, because gateway and frontend have paths with the same
names (`/flows`, `/runs`…) that could not be told apart otherwise. The gateway must
KNOW that prefix for one thing the proxy does not see: the SSO transaction's cookie is
restricted to a path, and if the path is not the one the browser sees the cookie does
not come back and the sign-in fails with `tx_missing` — after the user has
authenticated on the IdP already.
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


def test_without_prefix_the_cookie_stays_where_it_was(monkeypatch):
    res = _login(monkeypatch)
    assert "Path=/auth/sso;" in res.headers["set-cookie"] + ";"


def test_the_transaction_cookie_follows_the_public_prefix(monkeypatch):
    res = _login(monkeypatch, root_path="/api")
    assert "Path=/api/auth/sso;" in res.headers["set-cookie"] + ";"


def test_deleting_the_cookie_uses_the_prefix_too(session, monkeypatch):
    # a cookie is deleted only by repeating the path it was written with
    cfg = Settings(app={"root_path": "/api"}, oidc=OIDC)
    monkeypatch.setattr(sso_routes, "get_settings", lambda: cfg)
    res = sso_routes.sso_callback(FakeRequest(), code="abc", state="s", session=session)
    assert "error=tx_missing" in res.headers["location"]
    assert "Path=/api/auth/sso" in res.headers["set-cookie"]


@pytest.mark.parametrize("written, read_value", [("", ""), ("/", ""), ("/api", "/api"), ("/api/", "/api"), ("api", "/api")])
def test_the_prefix_is_normalized(written, read_value):
    assert AppSettings(root_path=written).root_path == read_value


def test_a_prefix_that_is_not_a_path_is_refused():
    for wrong in ("https://app.example.com/api", "/api?x=1", "/a b"):
        with pytest.raises(ValidationError):
            AppSettings(root_path=wrong)


def test_under_a_prefix_the_trailing_slash_does_not_redirect_out():
    """With one slash too many FastAPI answers 307 towards the address without the slash, and
    builds that address WITHOUT the prefix: behind the proxy it would take `/api/users/`
    to `/users`, which is a frontend page. Under a prefix the redirect is turned off: a
    404 tells the truth, a wrong 307 does not."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    def app_with(settings_: AppSettings) -> TestClient:
        app = FastAPI(root_path=settings_.root_path, redirect_slashes=settings_.redirect_slashes)
        app.get("/users")(lambda: [])
        return TestClient(app, follow_redirects=False, base_url="https://app.example.com")

    at_root = app_with(AppSettings()).get("/users/")
    assert at_root.status_code == 307  # in development it stays as it was

    under_api = app_with(AppSettings(root_path="/api")).get("/users/")
    assert under_api.status_code == 404 and "location" not in under_api.headers
    assert app_with(AppSettings(root_path="/api")).get("/users").status_code == 200


def test_the_application_is_built_with_those_settings():
    from app import main
    from app.core.config import get_settings

    cfg = get_settings().app
    assert main.app.root_path == cfg.root_path
    assert main.app.router.redirect_slashes == cfg.redirect_slashes


# ── The question the proxy asks the gateway before opening Grafana ───────────
def test_the_proxys_route_is_closed_by_the_observers_guard():
    """When Grafana sits behind a reverse proxy, the proxy asks the gateway whether whoever
    knocks may see the monitoring (`forward_auth`). The route must answer nothing and cost
    nothing: it is called at every request of every panel. Only WHO may call it counts."""
    from app.deps.auth import require_observer
    from app.routes import auth as auth_routes

    route = next(r for r in auth_routes.router.routes if r.path == "/auth/observer")
    assert route.methods == {"GET"} and route.status_code == 204
    assert require_observer in [d.call for d in route.dependant.dependencies]
    assert auth_routes.can_observe() is None



def test_the_route_answers_only_whoever_can_observe(session):
    """The same question, really asked: without a token, with any token, as a normal user, as
    an observer, as an administrator."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.db.session import get_session
    from app.routes import auth as auth_routes
    from tests.conftest import make_user

    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app)

    normal = make_user(session, email="normale@x.local")
    observer = make_user(session, email="osservatore@x.local", is_observer=True)
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    from tests.conftest import token_of

    bearer = lambda u: {"Authorization": f"Bearer {token_of(session, u)}"}

    assert client.get("/auth/observer").status_code == 401
    # what the proxy sends when there is no cookie: «Bearer » and that is all
    assert client.get("/auth/observer", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.get("/auth/observer", headers={"Authorization": "Bearer non-un-token"}).status_code == 401
    assert client.get("/auth/observer", headers=bearer(normal)).status_code == 403
    for who in (observer, admin):
        answer = client.get("/auth/observer", headers=bearer(who))
        assert answer.status_code == 204 and answer.content == b""
