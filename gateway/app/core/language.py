"""The language of whoever makes the request, for the messages the gateway writes for a
person (the dbt export's errors, to begin with). The frontend sends the interface's
language in `Accept-Language`; without it, the first language the browser asks for
among the app's holds, and finally English, the interface's default language."""
from __future__ import annotations

from typing import Optional

LANGUAGES = ("en", "it", "de", "es", "fr")
DEFAULT = "en"


def language_of(accept_language: Optional[str]) -> str:
    """`it`, `it-IT,it;q=0.9,en;q=0.8`, `fr-CH` … → one of the LANGUAGES."""
    for part in (accept_language or "").split(","):
        tag = part.split(";")[0].strip().lower()
        base = tag.split("-")[0]
        if base in LANGUAGES:
            return base
    return DEFAULT


def request_language(request) -> str:
    try:
        return language_of(request.headers.get("accept-language"))
    except Exception:  # noqa: BLE001 — a fake request in the tests, or one without headers
        return DEFAULT
