"""The first start of the try-it stack with nothing configured: the key created in the
shared directory, the first administrator created from the sign-in page, and the
sample-data container's start-up token (services/first_run.py, core/keys.py)."""
import multiprocessing
import os

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import Settings, get_settings
from app.core.keys import read_or_create
from app.db.session import get_session
from app.models import AuditLog, User
from app.routes import auth as auth_routes
from app.services import first_run
from tests.conftest import make_user

CSRF = {"X-Tabularia-Csrf": "1"}
GOOD = "a-password-of-my-own"


@pytest.fixture
def app(session):
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.dependency_overrides[get_session] = lambda: session
    return app


@pytest.fixture
def first_start(monkeypatch, tmp_path):
    """A try-it stack: first-run setup on, a shared keys directory, no admin password."""
    monkeypatch.setenv("AUTH__FIRST_RUN_SETUP", "true")
    monkeypatch.setenv("SECURITY__KEYS_DIR", str(tmp_path))
    monkeypatch.delenv("AUTH__ADMIN_PASSWORD", raising=False)
    get_settings.cache_clear()
    return tmp_path


def test_the_sign_in_page_asks_for_nothing_on_a_configured_stack(app):
    c = TestClient(app)
    assert c.get("/auth/setup").json() == {"needed": False}
    assert c.post("/auth/setup", json={"email": "a@x.io", "password": GOOD}).status_code == 404


def test_the_first_administrator_is_created_once_and_signed_in(app, session, first_start):
    c = TestClient(app)
    assert c.get("/auth/setup").json() == {"needed": True}
    r = c.post("/auth/setup", json={"email": " Me@Example.com ", "password": GOOD, "full_name": "Me"}, headers=CSRF)
    assert r.status_code == 201, r.text
    me = c.get("/auth/me").json()  # the session cookie of the setup
    assert me["email"] == "me@example.com" and me["is_superuser"] is True
    assert c.get("/auth/setup").json() == {"needed": False}
    again = TestClient(app).post("/auth/setup", json={"email": "other@x.io", "password": GOOD})
    assert again.status_code == 409
    assert len(session.exec(select(User)).all()) == 1
    actions = [a.action for a in session.exec(select(AuditLog)).all()]
    assert "auth.first_run_setup" in actions


def test_any_existing_account_closes_the_setup(app, session, first_start):
    make_user(session, email="someone@x.io")
    c = TestClient(app)
    assert c.get("/auth/setup").json() == {"needed": False}
    assert c.post("/auth/setup", json={"email": "me@x.io", "password": GOOD}).status_code == 409


@pytest.mark.parametrize("email,password", [
    ("me@x.io", "admin"),            # published
    ("me@x.io", "short-one"),        # under 12 characters
    ("not-an-email", GOOD),
    ("me @x.io", GOOD),
])
def test_a_weak_password_or_a_bad_email_is_refused(app, session, first_start, email, password):
    r = TestClient(app).post("/auth/setup", json={"email": email, "password": password})
    assert r.status_code == 422
    assert session.exec(select(User)).first() is None


def test_production_never_offers_the_setup(app, first_start, monkeypatch):
    monkeypatch.setenv("APP__ENV_NAME", "production")
    get_settings.cache_clear()
    c = TestClient(app)
    assert c.get("/auth/setup").json() == {"needed": False}
    assert c.post("/auth/setup", json={"email": "me@x.io", "password": GOOD}).status_code == 404


def test_the_sample_container_waits_for_the_administrator_then_signs_in_as_it(app, first_start):
    first_run.ensure_bootstrap_token()
    token = (first_start / "bootstrap.token").read_text().strip()
    assert len(token) >= 32 and oct((first_start / "bootstrap.token").stat().st_mode)[-3:] == "600"
    c = TestClient(app)
    assert c.post("/auth/bootstrap", json={"token": token}).status_code == 409  # no administrator yet
    assert c.post("/auth/bootstrap", json={"token": token + "x"}).status_code == 401
    assert c.post("/auth/setup", json={"email": "me@x.io", "password": GOOD}).status_code == 201
    r = TestClient(app).post("/auth/bootstrap", json={"token": token})
    assert r.status_code == 200
    me = TestClient(app).get("/auth/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"}).json()
    assert me["email"] == "me@x.io"


def test_no_token_without_a_keys_directory_or_without_first_run(app, monkeypatch, tmp_path):
    c = TestClient(app)
    assert c.post("/auth/bootstrap", json={"token": "anything"}).status_code == 404
    monkeypatch.setenv("SECURITY__KEYS_DIR", str(tmp_path))  # a keys directory, first-run off
    get_settings.cache_clear()
    first_run.ensure_bootstrap_token()
    assert not (tmp_path / "bootstrap.token").exists()
    assert c.post("/auth/bootstrap", json={"token": "anything"}).status_code == 404


def test_without_a_password_the_gateway_starts_with_no_administrator(first_start, db_engine, monkeypatch):
    from app.db import seed

    monkeypatch.setattr(seed, "engine", db_engine)
    seed.seed_admin()  # no AUTH__ADMIN_PASSWORD: no administrator, and no refusal to start
    from sqlmodel import Session

    with Session(db_engine) as s:
        assert s.exec(select(User)).first() is None


def test_the_key_is_created_in_the_keys_directory_and_kept(first_start, monkeypatch):
    monkeypatch.delenv("SECURITY__FERNET_KEY", raising=False)
    a, b = Settings(), Settings()
    assert a.security.fernet_key and a.security.fernet_key == b.security.fernet_key
    assert (first_start / "fernet.key").read_text().strip() == a.security.fernet_key
    a.check_required_secrets()


def test_a_set_key_wins_over_the_keys_directory(first_start, monkeypatch):
    monkeypatch.setenv("SECURITY__FERNET_KEY", "x" * 43 + "=")
    assert Settings().security.fernet_key == "x" * 43 + "="
    assert not (first_start / "fernet.key").exists()


def _racer(directory, start, out):
    start.wait()
    out.put(read_or_create(directory, "fernet.key", lambda: os.urandom(16).hex()))


def test_services_starting_together_all_get_the_same_key(tmp_path):
    ctx = multiprocessing.get_context("fork")
    start, out = ctx.Event(), ctx.Queue()
    procs = [ctx.Process(target=_racer, args=(str(tmp_path / "keys"), start, out)) for _ in range(8)]
    for p in procs:
        p.start()
    start.set()
    values = {out.get(timeout=20) for _ in procs}
    for p in procs:
        p.join(timeout=20)
    assert len(values) == 1
    assert [f.name for f in (tmp_path / "keys").iterdir()] == ["fernet.key"]  # no temporary file left
