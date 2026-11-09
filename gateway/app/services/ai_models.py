"""The AI providers, their model catalogs and the administrator's choice.

A provider is either the one of the environment (AI__BASE_URL, AI__SECRET_KEY: read-only
in the panel) or a row of `ai_providers`, added from the panel: an OpenAI-compatible
endpoint or Anthropic, through its own SDK (services/ai_anthropic.py).

The provider says which models EXIST; the `ai_models` table says which may be USED. A
catalog is kept in memory for a few minutes: it changes rarely, and every opening of the
chat must not cost a call to the provider.

**How a model is named.** Two providers can offer the same name, so what is stored and
what the API sends is a reference: the bare name for the environment's provider, as it
always was, and `provider:<id>/<name>` for the others (`ref_of`, `split_ref`). The id and
not the provider's name: a provider can be renamed. `model_name` is what goes on the wire
and to a price list.
"""
from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx
from cryptography.fernet import InvalidToken
from fastapi import HTTPException
from pydantic import SecretStr
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.db.session import engine as db_engine
from app.models import AiChat, AiModel, AiProvider

logger = logging.getLogger(__name__)

_CACHE_SECONDS = 300
# by endpoint: when it was read, the row's version it was read for, the ids
_cache: dict[str, tuple[float, str, list[str]]] = {}

OPENAI_COMPATIBLE = "openai_compatible"
ANTHROPIC = "anthropic"
KINDS = (OPENAI_COMPATIBLE, ANTHROPIC)
ANTHROPIC_BASE_URL = "https://api.anthropic.com"

ENVIRONMENT = "env"
_REF = re.compile(r"^provider:(\d+)/(.+)$", re.DOTALL)

# Models the endpoint lists but that cannot converse: embedding, audio, reranker, image,
# moderation, the realtime API's and the old completion ones. They stay visible to the
# administrator, but cannot be enabled for the chat (nor are they when a provider is added).
_NOT_CHAT = ("embedding", "embed-", "bge-", "whisper", "rerank", "tts", "speech", "transcri",
             "dall-e", "gpt-image", "imagen", "sora", "moderation", "realtime", "babbage", "davinci")


def _new_session() -> Session:
    """For what has no session of its own (the model of a title, of a dbt export). The tests replace it."""
    return Session(db_engine)


# ── how a model is named ─────────────────────────────────────────────────────
def split_ref(ref: str) -> tuple[Optional[int], str]:
    """`(provider id, name at the provider)`; the id is None for the environment's provider."""
    found = _REF.match(ref or "")
    return (int(found.group(1)), found.group(2)) if found else (None, ref or "")


def ref_of(provider_id: Optional[int], model: str) -> str:
    return model if provider_id is None else f"provider:{provider_id}/{model}"


def model_name(ref: str) -> str:
    return split_ref(ref)[1]


def is_chat_model(model_id: str) -> bool:
    low = model_name(model_id).lower()
    return not any(marker in low for marker in _NOT_CHAT)


# ── the providers ────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Endpoint:
    """A provider that can be called, whichever way it was configured."""

    provider_id: Optional[int]  # None = the environment's
    name: str
    kind: str
    base_url: str
    active: bool
    stamp: str  # changes with the row: a catalog read for an older version is not used
    secret_encrypted: str = ""
    secret_plain: SecretStr = SecretStr("")

    @property
    def key(self) -> str:
        return ENVIRONMENT if self.provider_id is None else f"provider:{self.provider_id}"

    def secret(self) -> str:
        """The API key, in clear, for the call being made: never kept, logged or returned."""
        if not self.secret_encrypted:
            return self.secret_plain.get_secret_value()
        try:
            return decrypt_secret(self.secret_encrypted)
        except (InvalidToken, ValueError) as e:  # the Fernet key changed since it was saved
            raise HTTPException(status_code=409, detail=f"The key of «{self.name}» can no longer be read: enter it again "
                                                        "in Administration → AI models.") from e


def environment_endpoint() -> Optional[Endpoint]:
    cfg = get_settings().ai
    if not cfg.enabled:
        return None
    # no name of its own: its host is what tells a person where the data goes
    host = urlsplit(cfg.base_url.strip()).hostname or cfg.base_url.strip()
    return Endpoint(provider_id=None, name=host, kind=OPENAI_COMPATIBLE, base_url=cfg.base_url.strip(), active=True,
                    stamp="env", secret_plain=cfg.secret_key)


def _of_row(row: AiProvider) -> Endpoint:
    return Endpoint(provider_id=row.id, name=row.name, kind=row.kind, base_url=row.base_url, active=row.active,
                    stamp=str(row.updated_at), secret_encrypted=row.secret_encrypted)


def endpoints(session: Session, only_active: bool = True) -> list[Endpoint]:
    """Every provider: the environment's first, then the others by name."""
    query = select(AiProvider).order_by(AiProvider.name)
    if only_active:
        query = query.where(AiProvider.active == True)  # noqa: E712
    found = [_of_row(row) for row in session.exec(query).all()]
    env = environment_endpoint()
    return ([env] if env else []) + found


def endpoint_of_key(session: Session, key: str) -> Optional[Endpoint]:
    """A provider by the key the API uses for it (`env`, `provider:3`), on or off."""
    if key == ENVIRONMENT:
        return environment_endpoint()
    found = re.fullmatch(r"provider:(\d+)", key or "")
    row = session.get(AiProvider, int(found.group(1))) if found else None
    return _of_row(row) if row is not None else None


def endpoint_of(ref: str, session: Optional[Session] = None) -> Optional[Endpoint]:
    """The provider of a model, if it exists and is on. The environment's needs no database."""
    provider_id, _ = split_ref(ref)
    if provider_id is None:
        return environment_endpoint()
    if session is not None:
        row = session.get(AiProvider, provider_id)
        return _of_row(row) if row is not None and row.active else None
    with _new_session() as own:
        return endpoint_of(ref, own)


def configured(session: Session) -> bool:
    return bool(endpoints(session))


def ensure_configured(session: Session) -> None:
    if not configured(session):
        raise HTTPException(status_code=503, detail="AI assistant not configured: add a provider in Administration → AI models, "
                                                    "or set AI__BASE_URL and AI__SECRET_KEY")


def normalize_base_url(kind: str, url: str) -> str:
    """The address as it is stored, or ValueError with what is wrong with it."""
    url = (url or "").strip().rstrip("/")
    if not url:
        if kind == ANTHROPIC:
            return ANTHROPIC_BASE_URL
        raise ValueError("Enter the provider's address, ending in /v1")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("The address must start with https:// (or http:// inside your network)")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("The address cannot hold a user, a password or a query: the key goes in its own field")
    # Anthropic's SDK adds /v1 itself: the address people copy from its documentation would become /v1/v1
    return url.removesuffix("/v1") if kind == ANTHROPIC else url


# ── what a provider's failure means, in words ────────────────────────────────
def failure(status: Optional[int], provider: str) -> tuple[str, str]:
    """`(code, message)` for a failed call to a provider. `status` None = it was not reached.
    Never the body of the answer: it may quote what was sent, or part of a key."""
    words: dict[int, tuple[str, str]] = {
        400: ("provider_rejected", f"«{provider}» refused the request. The conversation may be too long for this model: "
                                   "try a new one, or another model."),
        401: ("provider_key", f"«{provider}» refused the API key. An administrator can change it in Administration → AI models."),
        402: ("provider_billing", f"«{provider}» reports a billing problem on this account. An administrator can check it "
                                  "on the provider's site."),
        403: ("provider_forbidden", f"«{provider}» does not allow this API key to do this. An administrator can check the "
                                    "key's permissions on the provider's site."),
        404: ("provider_model", f"«{provider}» does not offer this model, or its address is wrong. Try another model, or ask "
                                "an administrator to check the provider."),
        413: ("provider_too_large", f"The request is too large for «{provider}»: start a new conversation."),
        429: ("provider_busy", f"«{provider}» is limiting requests on this account. Try again in a moment."),
        529: ("provider_overloaded", f"«{provider}» is overloaded right now. Try again in a moment, or choose another model."),
    }
    if status is None:
        return "provider_unreachable", f"«{provider}» could not be reached: check its address and the network."
    if status in words:
        return words[status]
    if status >= 500:
        return "provider_error", f"«{provider}» had a problem of its own ({status}). Try again in a moment."
    return "provider_rejected", f"«{provider}» refused the request ({status})."


class CatalogError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ── the catalogs ─────────────────────────────────────────────────────────────
async def _openai_compatible_catalog(ep: Endpoint) -> list[str]:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(ep.base_url.rstrip("/") + "/models", headers={"Authorization": f"Bearer {ep.secret()}"})
        resp.raise_for_status()
        return sorted({str(m.get("id")) for m in resp.json().get("data", []) if m.get("id")})
    except httpx.HTTPStatusError as e:
        raise CatalogError(*failure(e.response.status_code, ep.name)) from e
    except (httpx.HTTPError, ValueError, AttributeError) as e:  # network, or an answer that is not a catalog
        logger.warning("AI model catalog of %s not readable: %s", ep.name, type(e).__name__)
        raise CatalogError(*failure(None, ep.name)) from e


async def read_catalog(ep: Endpoint) -> list[str]:
    """The ids of the models the provider exposes now, in alphabetical order, or CatalogError."""
    if ep.kind == ANTHROPIC:
        from app.services import ai_anthropic

        return await ai_anthropic.catalog(ep)
    return await _openai_compatible_catalog(ep)


async def provider_models(force: bool = False, provider: Optional[Endpoint] = None) -> list[str]:
    """A provider's catalog, by default the environment's: the names at the provider, not references."""
    ep = provider or environment_endpoint()
    if ep is None:
        raise HTTPException(status_code=503, detail="AI assistant not configured (AI__BASE_URL and AI__SECRET_KEY)")
    held = _cache.get(ep.key)
    if not force and held is not None and held[1] == ep.stamp and (time.monotonic() - held[0]) < _CACHE_SECONDS:
        return held[2]
    try:
        ids = await read_catalog(ep)
    except CatalogError as e:
        raise HTTPException(status_code=502, detail=e.message) from e
    _cache[ep.key] = (time.monotonic(), ep.stamp, ids)
    return ids


def clear_cache() -> None:
    _cache.clear()


# ── the administrator's choice ───────────────────────────────────────────────
def enabled_model_ids(session: Session) -> list[str]:
    """The models that can be used now: enabled, fit for the chat, and of a provider that is on."""
    rows = session.exec(select(AiModel).where(AiModel.enabled == True)).all()  # noqa: E712
    alive = {ep.provider_id for ep in endpoints(session)}
    return sorted(r.model_id for r in rows if is_chat_model(r.model_id) and split_ref(r.model_id)[0] in alive)


def preferred(configured_model: str, enabled: list[str]) -> Optional[str]:
    """The model offered first. `ai.default_model` holds a reference or a bare name: with the
    same name on two providers the environment's wins, then the provider added first."""
    if configured_model in enabled:
        return configured_model
    same_name = [ref for ref in enabled if model_name(ref) == configured_model]
    if same_name:
        return min(same_name, key=lambda ref: split_ref(ref)[0] or 0)
    # with nothing configured, adding a provider does not move anyone's data there by itself
    return min(enabled, key=lambda ref: (split_ref(ref)[0] is not None, ref), default=None)


def options(session: Session, enabled: list[str]) -> list[dict[str, Any]]:
    """The enabled models with their provider, as the pickers show them: the environment's first."""
    names = {ep.provider_id: ep.name for ep in endpoints(session)}
    return [{"id": ref, "model": model_name(ref), "provider": names.get(split_ref(ref)[0], "")}
            for ref in sorted(enabled, key=lambda r: (split_ref(r)[0] is not None, names.get(split_ref(r)[0], ""), model_name(r)))]


def provider_label(session: Session, ref: str) -> str:
    """The name of a model's provider, on or off; empty when it is gone. For words shown to a person."""
    provider_id, _ = split_ref(ref)
    if provider_id is None:
        env = environment_endpoint()
        return env.name if env else ""
    row = session.get(AiProvider, provider_id)
    return row.name if row is not None else ""


def described(session: Session, ref: str) -> str:
    """A model in words, for the audit and for a person: «claude-opus-5-5 (Anthropic)». Bare for
    the environment's provider, as it always was in the records."""
    provider_id, name = split_ref(ref)
    provider = provider_label(session, ref) if provider_id is not None else ""
    return f"{name} ({provider})" if provider else name


def refuse_unless_usable(session: Session, ref: str) -> None:
    """What a request for a model gets when it cannot be used, and why."""
    if ref in enabled_model_ids(session):
        return
    provider_id, _ = split_ref(ref)
    row = session.get(AiProvider, provider_id) if provider_id is not None else None
    if row is not None and not row.active:
        raise HTTPException(status_code=403, detail=f"«{row.name}», the provider of this model, has been turned off: choose another model")
    if provider_id is not None and row is None:
        raise HTTPException(status_code=403, detail="The provider of this model no longer exists: choose another model")
    ensure_configured(session)
    raise HTTPException(status_code=403, detail="This model is not enabled by the administrator")


def forget_provider(session: Session, provider_id: int) -> None:
    """Its models go with it; the conversations stay readable. Does not commit."""
    from sqlalchemy import delete as sa_delete

    session.exec(sa_delete(AiModel).where(AiModel.model_id.startswith(f"provider:{provider_id}/")))  # type: ignore[attr-defined]
    _cache.pop(f"provider:{provider_id}", None)


def provider_usage(session: Session, provider_id: int) -> dict[str, int]:
    """What depends on a provider, for the words said before it is turned off or removed."""
    from sqlmodel import func

    prefix = f"provider:{provider_id}/"
    chats = session.exec(select(func.count()).select_from(AiChat).where(AiChat.model_id.startswith(prefix))).one()  # type: ignore[attr-defined]
    enabled = session.exec(select(func.count()).select_from(AiModel).where(
        AiModel.model_id.startswith(prefix), AiModel.enabled == True)).one()  # type: ignore[attr-defined]  # noqa: E712
    return {"chats": int(chats or 0), "enabled_models": int(enabled or 0)}
