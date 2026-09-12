"""SECURITY__FERNET_KEY è obbligatoria SEMPRE (anche in sviluppo): il gateway
non parte senza una chiave valida, invece di fallire al primo uso delle
connessioni. Copre `Settings.check_required_secrets` (chiamata nel lifespan)."""
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
        _settings("non-e-una-chiave").check_required_secrets()


def test_valid_key_passes_in_every_env():
    key = Fernet.generate_key().decode()
    _settings(key).check_required_secrets()
    Settings(security={"fernet_key": key}, app={"env_name": "development"}).check_required_secrets()


def test_production_check_no_longer_owns_the_fernet_rule():
    # in produzione la chiave è già pretesa da check_required_secrets: il check di
    # produzione si occupa solo dei default di sviluppo (jwt/admin/db)
    s = Settings(security={"fernet_key": ""}, app={"env_name": "production"})
    with pytest.raises(RuntimeError) as e:
        s.check_production_safety()
    assert "FERNET" not in str(e.value)
