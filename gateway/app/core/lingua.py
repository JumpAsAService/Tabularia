"""La lingua di chi fa la richiesta, per i messaggi che il gateway scrive per una
persona (gli errori dell'export dbt, per cominciare). Il frontend manda la lingua
dell'interfaccia in `Accept-Language`; senza, vale la prima lingua che il browser
chiede fra quelle dell'app, e infine l'inglese, lingua di default dell'interfaccia."""
from __future__ import annotations

from typing import Optional

LINGUE = ("en", "it", "de", "es", "fr")
DEFAULT = "en"


def lingua_di(accept_language: Optional[str]) -> str:
    """`it`, `it-IT,it;q=0.9,en;q=0.8`, `fr-CH` … → una delle LINGUE."""
    for parte in (accept_language or "").split(","):
        tag = parte.split(";")[0].strip().lower()
        base = tag.split("-")[0]
        if base in LINGUE:
            return base
    return DEFAULT


def lingua_della_richiesta(request) -> str:
    try:
        return lingua_di(request.headers.get("accept-language"))
    except Exception:  # noqa: BLE001 — una richiesta finta nei test, o senza intestazioni
        return DEFAULT
