"""Symmetric encryption (Fernet) for the database connections' credentials.

The key is SHARED by gateway and engine (env SECURITY__FERNET_KEY in both): the gateway
encrypts the password at rest in its Postgres and sends it encrypted in the ingest
requests; the engine decrypts it only in the worker, when the connection opens. So the
clear password never goes through the Celery broker nor stays in the payloads on Valkey.
"""
import hashlib
import hmac
from functools import lru_cache

from cryptography.fernet import Fernet

from app.core.config import FERNET_KEY_HINT, get_settings


@lru_cache
def _fernet() -> Fernet:
    # Fail closed: without a key it REFUSES to work, instead of falling back silently on
    # a key known in the repo (which would make the encrypted secrets as good as clear
    # text). Everywhere, not only in production: the start-up
    # (Settings.check_required_secrets) demands it already, this is the second lock.
    # Set it in infrastructure/.env (the same everywhere).
    key = get_settings().security.fernet_key
    if not key:
        raise RuntimeError(
            "SECURITY__FERNET_KEY is not set: connection credentials cannot be "
            "encrypted or decrypted. Set it (the same in gateway and engine) — "
            + FERNET_KEY_HINT
        )
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


# ── The gateway introduces itself to the engine ──────────────────────────────
# The engine API has no users of its own: it answers whoever reaches it, with the
# bucket's keys. The network closes it (compose: an internal network; Helm:
# NetworkPolicy), and this is the second lock (audit 2026-10-08, A4): every request of
# the gateway carries a token only whoever has the Fernet key can compute. Derived, not
# configured: gateway and engine share the key already, so there is no extra secret to
# generate, distribute or get wrong. The label sets it apart from the key's other uses.
# It MUST stay identical in the gateway and the engine.
INTERNAL_HEADER = "X-Tabularia-Internal"
_INTERNAL_LABEL = b"tabularia: gateway -> engine API"


@lru_cache
def internal_token() -> str:
    key = get_settings().security.fernet_key
    if not key:
        raise RuntimeError("SECURITY__FERNET_KEY is not set: it is also needed for the token between gateway and engine")
    return hmac.new(key.encode(), _INTERNAL_LABEL, hashlib.sha256).hexdigest()
