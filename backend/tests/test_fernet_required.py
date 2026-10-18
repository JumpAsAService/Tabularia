"""SECURITY__FERNET_KEY è obbligatoria SEMPRE: API e worker Celery non partono
senza una chiave valida (`Settings.check_required_secrets`, chiamata all'import
di app.api.main e di app.tasks.celery_app)."""
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
        _settings("abc").check_required_secrets()


def test_valid_key_passes():
    _settings(Fernet.generate_key().decode()).check_required_secrets()


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
