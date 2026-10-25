"""The AI provider's model catalog and the administrator's choice.

The provider (an OpenAI-compatible endpoint) says which models EXIST
(`GET {base_url}/models`); the `ai_models` table says which may be USED. The catalog
is kept in memory for a few minutes: it changes rarely, and every opening of the chat
must not cost a call to the provider.
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

# Models the endpoint lists but that cannot converse: embedding, audio, reranker. They
# stay visible to the administrator, but cannot be enabled for the chat.
_NOT_CHAT = ("embedding", "embed-", "bge-", "whisper", "rerank", "tts", "speech", "transcri")


def is_chat_model(model_id: str) -> bool:
    low = model_id.lower()
    return not any(marker in low for marker in _NOT_CHAT)


def ensure_configured() -> None:
    if not get_settings().ai.enabled:
        raise HTTPException(status_code=503, detail="AI assistant not configured (AI__BASE_URL and AI__SECRET_KEY)")


async def provider_models(force: bool = False) -> list[str]:
    """The ids of the models the provider exposes, in alphabetical order."""
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
    except Exception as e:  # noqa: BLE001 — network, expired key, wrong endpoint
        logger.warning("AI model catalog not readable: %s", e)
        raise HTTPException(status_code=502, detail="The AI provider does not answer the list of models") from e
    _cache = (time.monotonic(), ids)
    return ids


def clear_cache() -> None:
    global _cache
    _cache = None


def enabled_model_ids(session: Session) -> list[str]:
    """The models the administrator enabled (and fit for the chat)."""
    rows = session.exec(select(AiModel).where(AiModel.enabled == True)).all()  # noqa: E712
    return sorted(r.model_id for r in rows if is_chat_model(r.model_id))
