"""No installation comes up with an administrator password everyone knows.

The code defaulted to `admin`, the example settings said `admin`, and only a gateway
with APP__ENV_NAME=production refused it: a trial installation opened to colleagues,
or a production one where the variable was forgotten, had admin/admin. Now:
- there is no default: the first start needs AUTH__ADMIN_PASSWORD;
- a known password (`admin`, the one of the guides…) or one under 12 characters is
  refused when the administrator is created, in every environment;
- an administrator that still has a known password from an older release gets the
  configured one, when that one is good; otherwise the gateway does not start;
- AUTH__ALLOW_KNOWN_ADMIN_PASSWORD=true lets a local development stack keep its
  fixed password; production ignores it.
"""
import logging

import pytest
from sqlmodel import Session, select

from app.core import config as config_module
from app.core.config import ADMIN_PASSWORD_MIN, Settings
from app.core.security import hash_password, verify_password
from app.db import seed
from app.models import User, UserSession
from tests.conftest import make_user

GOOD = "a-long-password-of-mine-2026"


@pytest.fixture
def settings(monkeypatch, db_engine):
    """Points the seed at the test database and at the settings the test gives."""
    holder = {}
    monkeypatch.setattr(seed, "engine", db_engine)
    monkeypatch.setattr(seed, "get_settings", lambda: holder["s"])

    def use(**auth):
        env = auth.pop("env", "development")
        holder["s"] = Settings(app={"env_name": env}, auth={"admin_email": "admin@x.it", **auth})
        return holder["s"]

    return use


def _admin(db_engine):
    with Session(db_engine) as s:
        return s.exec(select(User).where(User.email == "admin@x.it")).first()


def test_there_is_no_default_password(monkeypatch):
    monkeypatch.delenv("AUTH__ADMIN_PASSWORD", raising=False)
    assert Settings().auth.admin_password.get_secret_value() == ""


@pytest.mark.parametrize("password", ["", "admin", "ADMIN", "Change-me-2026", "changeme", "x" * (ADMIN_PASSWORD_MIN - 1), "y" * 73])
@pytest.mark.parametrize("env", ["development", "production"])
def test_the_first_administrator_never_gets_a_known_password(settings, db_engine, password, env):
    settings(admin_password=password, env=env)
    with pytest.raises(RuntimeError, match="AUTH__ADMIN_PASSWORD"):
        seed.seed_admin()
    assert _admin(db_engine) is None


def test_a_good_password_creates_the_administrator(settings, db_engine):
    settings(admin_password=GOOD)
    seed.seed_admin()
    admin = _admin(db_engine)
    assert admin.is_superuser and verify_password(GOOD, admin.hashed_password)


def test_a_development_stack_may_keep_its_fixed_password(settings, db_engine):
    settings(admin_password="admin", allow_known_admin_password=True)
    seed.seed_admin()
    assert verify_password("admin", _admin(db_engine).hashed_password)


def test_production_ignores_the_development_allowance(settings, db_engine):
    settings(admin_password="admin", allow_known_admin_password=True, env="production")
    with pytest.raises(RuntimeError, match="AUTH__ADMIN_PASSWORD"):
        seed.seed_admin()


def test_production_safety_names_a_weak_password():
    s = Settings(app={"env_name": "production"}, jwt={"secret": "s" * 64}, db={"password": "a-real-one"},
                 auth={"admin_password": "changeme"})
    with pytest.raises(RuntimeError, match="AUTH__ADMIN_PASSWORD"):
        s.check_production_safety()


# ── an administrator created by an older release ──────────────────────────────
def test_an_administrator_left_with_admin_gets_the_configured_password(settings, db_engine, caplog):
    with Session(db_engine) as s:
        old = make_user(s, email="admin@x.it", hashed_password=hash_password("admin"), is_superuser=True)
        s.add(UserSession(id="sid-1", user_id=old.id, created_at=old.created_at, last_seen_at=old.created_at,
                          expires_at=old.created_at))
        s.commit()
    settings(admin_password=GOOD)
    with caplog.at_level(logging.WARNING):
        seed.seed_admin()
    admin = _admin(db_engine)
    assert verify_password(GOOD, admin.hashed_password) and not verify_password("admin", admin.hashed_password)
    # whoever came in with the public password is out
    with Session(db_engine) as s:
        assert s.exec(select(UserSession)).all() == []
    assert "admin@x.it" in caplog.text


def test_an_administrator_left_with_admin_stops_the_start_without_a_good_one(settings, db_engine):
    with Session(db_engine) as s:
        make_user(s, email="admin@x.it", hashed_password=hash_password("admin"), is_superuser=True)
    settings(admin_password="admin")
    with pytest.raises(RuntimeError, match="still has"):
        seed.seed_admin()


def test_an_administrator_with_a_password_of_its_own_is_never_touched(settings, db_engine):
    with Session(db_engine) as s:
        make_user(s, email="admin@x.it", hashed_password=hash_password("chosen-in-the-app-1"), is_superuser=True)
    settings(admin_password=GOOD)
    seed.seed_admin()
    assert verify_password("chosen-in-the-app-1", _admin(db_engine).hashed_password)


def test_an_sso_only_administrator_is_left_alone(settings, db_engine):
    with Session(db_engine) as s:
        make_user(s, email="admin@x.it", hashed_password=None, is_superuser=True)
    settings(admin_password=GOOD)
    seed.seed_admin()
    assert _admin(db_engine).hashed_password is None


def test_the_known_list_is_checked_case_insensitively():
    assert config_module.is_known_admin_password("Admin")
    assert not config_module.is_known_admin_password(GOOD)
