"""In production the gateway refuses to start with weak secrets (audit 2026-10-08, B3).

Only the development default of the JWT secret was refused: any other value, even one
character, got through. The secret signs every session with HS256: a short one is
guessed offline starting from a single token, and whoever guesses it signs themselves
an administrator token."""
import pytest

from app.core.config import JWT_SECRET_MIN, Settings

GOOD = "a" * 64


def _production(**kw):
    base = {"app": {"env_name": "production"}, "jwt": {"secret": GOOD},
            "auth": {"admin_password": "una-password-vera"}, "db": {"password": "un-altra-vera"}}
    base.update(kw)
    return Settings(**base)


def test_real_secrets_pass():
    _production().check_production_safety()


@pytest.mark.parametrize("secret, reason", [
    ("change-me-in-production", "development default"),
    ("a", "too short (1 characters"),
    ("x" * (JWT_SECRET_MIN - 1), f"too short ({JWT_SECRET_MIN - 1} characters"),
])
def test_a_weak_jwt_secret_stops_the_start(secret, reason):
    with pytest.raises(RuntimeError, match="JWT__SECRET") as e:
        _production(jwt={"secret": secret}).check_production_safety()
    assert reason in str(e.value)


def test_at_the_minimum_it_passes():
    _production(jwt={"secret": "x" * JWT_SECRET_MIN}).check_production_safety()


def test_in_development_it_is_not_checked():
    Settings(app={"env_name": "development"}, jwt={"secret": "a"}).check_production_safety()
