"""Cifratura simmetrica (Fernet) per le credenziali delle connessioni database.

Stessa chiave dell'engine (env SECURITY__FERNET_KEY in entrambi): il gateway
cifra la password a riposo nel suo Postgres e la invia GIÀ CIFRATA all'engine
(`password_encrypted`), che la decifra solo nel worker al momento di aprire la
connessione. La password in chiaro non transita mai nel broker Celery.
"""
from functools import lru_cache

from cryptography.fernet import Fernet

from app.core.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    # Fail-closed: senza chiave si RIFIUTA di operare, invece di ripiegare in
    # silenzio su una chiave nota nel repo (che renderebbe i segreti cifrati
    # equivalenti a testo in chiaro). Vale in ogni ambiente, non solo in prod.
    # Dev: impostala in infrastructure/.env (uguale in gateway ed engine).
    key = get_settings().security.fernet_key
    if not key:
        raise RuntimeError(
            "SECURITY__FERNET_KEY non impostata: impossibile cifrare/decifrare le "
            "credenziali delle connessioni. Impostala (identica in gateway ed engine) — "
            'genera con: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
