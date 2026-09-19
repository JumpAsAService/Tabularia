"""Catalogo dei modelli del provider AI e scelta dell'amministratore.

Il provider (endpoint compatibile OpenAI) dice quali modelli ESISTONO
(`GET {base_url}/models`); la tabella `ai_models` dice quali si possono USARE.
Il catalogo viene tenuto in memoria per qualche minuto: cambia di rado, e ogni
apertura della chat non deve costare una chiamata al provider.
"""
from __future__ import annotations

import logging
import time

import httpx
from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import AiModel

logger = logging.getLogger(__name__)

_CACHE_SECONDS = 300
_cache: tuple[float, list[str]] | None = None

# Modelli che l'endpoint elenca ma che non sanno conversare: embedding, audio,
# reranker. Restano visibili all'amministratore, ma non abilitabili per la chat.
_NOT_CHAT = ("embedding", "embed-", "bge-", "whisper", "rerank", "tts", "speech", "transcri")


def is_chat_model(model_id: str) -> bool:
    low = model_id.lower()
    return not any(marker in low for marker in _NOT_CHAT)


def ensure_configured() -> None:
    if not get_settings().ai.enabled:
        raise HTTPException(status_code=503, detail="Assistente AI non configurato (AI__BASE_URL e AI__SECRET_KEY)")


async def provider_models(force: bool = False) -> list[str]:
    """Gli id dei modelli che il provider espone, in ordine alfabetico."""
    global _cache
    ensure_configured()
    if not force and _cache is not None and (time.monotonic() - _cache[0]) < _CACHE_SECONDS:
        return _cache[1]
    cfg = get_settings().ai
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(
                cfg.base_url.rstrip("/") + "/models",
                headers={"Authorization": f"Bearer {cfg.secret_key.get_secret_value()}"},
            )
            resp.raise_for_status()
            ids = sorted({str(m.get("id")) for m in resp.json().get("data", []) if m.get("id")})
    except Exception as e:  # noqa: BLE001 — rete, chiave scaduta, endpoint sbagliato
        logger.warning("catalogo dei modelli AI non leggibile: %s", e)
        raise HTTPException(status_code=502, detail="Il provider AI non risponde all'elenco dei modelli") from e
    _cache = (time.monotonic(), ids)
    return ids


def clear_cache() -> None:
    global _cache
    _cache = None


def enabled_model_ids(session: Session) -> list[str]:
    """I modelli abilitati dall'amministratore (e adatti alla chat)."""
    rows = session.exec(select(AiModel).where(AiModel.enabled == True)).all()  # noqa: E712
    return sorted(r.model_id for r in rows if is_chat_model(r.model_id))
