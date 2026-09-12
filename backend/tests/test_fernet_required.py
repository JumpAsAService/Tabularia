"""SECURITY__FERNET_KEY è obbligatoria SEMPRE: API e worker Celery non partono
senza una chiave valida (`Settings.check_required_secrets`, chiamata all'import
di app.api.main e di app.tasks.celery_app)."""
import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings


def _settings(key: str) -> Settings:
    return Settings(security={"fernet_key": key})


def test_missing_key_blocks_startup():
    with pytest.raises(RuntimeError, match="SECURITY__FERNET_KEY non impostata"):
        _settings("").check_required_secrets()


def test_invalid_key_blocks_startup():
    with pytest.raises(RuntimeError, match="SECURITY__FERNET_KEY non valida"):
        _settings("abc").check_required_secrets()


def test_valid_key_passes():
    _settings(Fernet.generate_key().decode()).check_required_secrets()
