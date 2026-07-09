"""Cifratura simmetrica (Fernet) per le credenziali delle connessioni database.

La chiave è CONDIVISA tra gateway ed engine (env SECURITY__FERNET_KEY in
entrambi): il gateway cifra la password a riposo nel suo Postgres e la manda
già cifrata nelle richieste di ingest; l'engine la decifra solo nel worker, al
momento di aprire la connessione. Così la password in chiaro non transita mai
nel broker Celery né resta nei payload su Valkey.
"""
import base64
from functools import lru_cache

from cryptography.fernet import Fernet

from app.core.config import get_settings

# Chiave di SVILUPPO, usata solo se SECURITY__FERNET_KEY non è impostata.
# In produzione va sovrascritta (genera con: python -c
# "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
DEV_FERNET_KEY = base64.urlsafe_b64encode(b"tabularia-dev-fernet-key-0123456").decode()


@lru_cache
def _fernet() -> Fernet:
    key = get_settings().security.fernet_key or DEV_FERNET_KEY
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()
