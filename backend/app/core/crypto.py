"""Cifratura simmetrica (Fernet) per le credenziali delle connessioni database.

La chiave è CONDIVISA tra gateway ed engine (env SECURITY__FERNET_KEY in
entrambi): il gateway cifra la password a riposo nel suo Postgres e la manda
già cifrata nelle richieste di ingest; l'engine la decifra solo nel worker, al
momento di aprire la connessione. Così la password in chiaro non transita mai
nel broker Celery né resta nei payload su Valkey.
"""
import hashlib
import hmac
from functools import lru_cache

from cryptography.fernet import Fernet

from app.core.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    # Fail-closed: senza chiave si RIFIUTA di operare, invece di ripiegare in
    # silenzio su una chiave nota nel repo (che renderebbe i segreti cifrati
    # equivalenti a testo in chiaro). Vale in ogni ambiente, non solo in prod:
    # lo startup (Settings.check_required_secrets) la pretende già, questo è
    # il secondo lucchetto. Impostala in infrastructure/.env (uguale ovunque).
    key = get_settings().security.fernet_key
    if not key:
        raise RuntimeError(
            "SECURITY__FERNET_KEY is not set: connection credentials cannot be "
            "encrypted or decrypted. Set it (identical in gateway and engine) — "
            'generate one with: python -c "from cryptography.fernet import Fernet; '
            'print(Fernet.generate_key().decode())"'
        )
    return Fernet(key)


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode()).decode()


def decrypt_secret(token: str) -> str:
    return _fernet().decrypt(token.encode()).decode()


# ── Il gateway si presenta all'engine ────────────────────────────────────────
# L'API dell'engine non ha utenti suoi: risponde a chi la raggiunge, con le chiavi
# del bucket. La rete la chiude (compose: rete interna; Helm: NetworkPolicy), e
# questo è il secondo lucchetto (audit 2026-10-08, A4): ogni richiesta del
# gateway porta un token che solo chi ha la chiave Fernet sa calcolare. Derivato,
# non configurato: gateway ed engine la condividono già, quindi non c'è un
# segreto in più da generare, distribuire o sbagliare. L'etichetta lo separa
# dagli altri usi della chiave. DEVE restare identica nel gateway e nell'engine.
INTESTAZIONE_INTERNA = "X-Tabularia-Internal"
_ETICHETTA_INTERNA = b"tabularia: gateway -> engine API"


@lru_cache
def token_interno() -> str:
    chiave = get_settings().security.fernet_key
    if not chiave:
        raise RuntimeError("SECURITY__FERNET_KEY is not set: it is also needed for the token between gateway and engine")
    return hmac.new(chiave.encode(), _ETICHETTA_INTERNA, hashlib.sha256).hexdigest()
