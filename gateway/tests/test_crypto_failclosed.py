"""Fail-closed Fernet encryption.

Regression for the fix: without `SECURITY__FERNET_KEY` the service must NOT fall back
silently on a key known in the repo — it must raise. With the key set, the
encrypt/decrypt round trip must work.
"""
import pytest
from cryptography.fernet import Fernet

from app.core import crypto


def _patch_key(monkeypatch, value: str):
    settings = type("S", (), {"security": type("Sec", (), {"fernet_key": value})()})()
    monkeypatch.setattr(crypto, "get_settings", lambda: settings)
    crypto._fernet.cache_clear()


def test_missing_key_raises(monkeypatch):
    _patch_key(monkeypatch, "")
    with pytest.raises(RuntimeError):
        crypto._fernet()
    crypto._fernet.cache_clear()


def test_roundtrip_with_key(monkeypatch):
    _patch_key(monkeypatch, Fernet.generate_key().decode())
    assert crypto.decrypt_secret(crypto.encrypt_secret("hunter2")) == "hunter2"
    crypto._fernet.cache_clear()
