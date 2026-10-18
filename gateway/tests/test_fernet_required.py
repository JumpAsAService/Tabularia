"""SECURITY__FERNET_KEY è obbligatoria SEMPRE (anche in sviluppo): il gateway
non parte senza una chiave valida, invece di fallire al primo uso delle
connessioni. Copre `Settings.check_required_secrets` (chiamata nel lifespan)."""
import pytest
from cryptography.fernet import Fernet

from app.core.config import Settings


def _settings(key: str) -> Settings:
    return Settings(security={"fernet_key": key})


def test_missing_key_blocks_startup():
    with pytest.raises(RuntimeError, match="SECURITY__FERNET_KEY is not set"):
        _settings("").check_required_secrets()


def test_invalid_key_blocks_startup():
    with pytest.raises(RuntimeError, match="SECURITY__FERNET_KEY is not valid"):
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


# ── the hint works on a machine without the `cryptography` package ──────────

def _hint(key: str) -> str:
    with pytest.raises(RuntimeError) as e:
        _settings(key).check_required_secrets()
    return str(e.value)


@pytest.mark.parametrize("key", ["", "abc"])
def test_the_hint_needs_only_standard_tools(key):
    """The suggested command imported `cryptography`, which a fresh machine does
    not have (1.2.0 getting-started run): the hint now offers openssl, and a
    python one-liner of the standard library."""
    import re
    import subprocess
    import sys

    hint = _hint(key)
    assert "cryptography" not in hint
    assert "openssl rand -base64 32 | tr '+/' '-_'" in hint
    one_liner = re.search(r'python3 -c "([^"]+)"', hint).group(1)
    # isolated (-I) and without site packages (-S): the standard library only
    out = subprocess.run([sys.executable, "-I", "-S", "-c", one_liner], capture_output=True, text=True, check=True)
    Fernet(out.stdout.strip())  # a valid key


def test_the_engine_side_lock_gives_the_same_hint(monkeypatch):
    from app.core import crypto
    from app.core.config import get_settings

    monkeypatch.setenv("SECURITY__FERNET_KEY", "")
    get_settings.cache_clear()
    crypto._fernet.cache_clear()
    try:
        with pytest.raises(RuntimeError) as e:
            crypto._fernet()
        assert "cryptography" not in str(e.value) and "openssl rand" in str(e.value)
    finally:
        get_settings.cache_clear()
        crypto._fernet.cache_clear()
