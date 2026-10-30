"""The AI model providers, from the panel (services/ai_models.py).

- `GET  /ai/providers`         observer: every provider, without keys or addresses
- `POST /ai/providers`, `PATCH|DELETE /ai/providers/{id}`  superuser: those added here (audit)
- `POST /ai/providers/test`    superuser: does this address answer to this key?

The key is written and never read back: an empty one in a change keeps the saved one, and a
new address needs it typed again, so a saved key cannot be sent somewhere else. The provider
of the environment is listed and tested, and changed where it is set.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr
from sqlmodel import Session, func, select

from app.core.crypto import encrypt_secret
from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import require_observer, require_superuser
from app.models import AiProvider, User
from app.services import ai_models, audit
from app.services.permissions import is_admin

router = APIRouter(route_class=ReleasingRoute, prefix="/ai", tags=["ai"])


class AiProviderOut(BaseModel):
    key: str  # what `?provider=` and the model references go by: `env`, `provider:3`
    id: Optional[int] = None
    name: str
    kind: str
    # empty for an observer: an address inside the network is not theirs to read
    base_url: str = ""
    active: bool
    has_key: bool
    read_only: bool  # the environment's: it is changed where it is set
    # what depends on it, for the words said before it is turned off or removed
    chats: int = 0
    enabled_models: int = 0


class AiProviderIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    kind: Literal["openai_compatible", "anthropic"]
    base_url: str = Field(default="", max_length=500)
    api_key: str = Field(default="", max_length=2000)


class AiProviderUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=80)
    base_url: Optional[str] = Field(default=None, max_length=500)
    # empty = the one saved stays, as in the connections
    api_key: str = Field(default="", max_length=2000)
    active: Optional[bool] = None


class AiProviderTest(BaseModel):
    # the provider to test as it is saved, or the one the form is about to save
    key: Optional[str] = Field(default=None, max_length=40)
    name: Optional[str] = Field(default=None, max_length=80)
    kind: Optional[Literal["openai_compatible", "anthropic"]] = None
    base_url: Optional[str] = Field(default=None, max_length=500)
    api_key: str = Field(default="", max_length=2000)


class AiProviderTestOut(BaseModel):
    ok: bool
    models: int = 0
    code: Optional[str] = None
    message: Optional[str] = None


NEW_ADDRESS_NEEDS_KEY = "Enter the API key again when you change the address: the saved key would be sent to the new one"


def _provider_out(session: Session, user: User, ep: ai_models.Endpoint) -> AiProviderOut:
    usage = ai_models.provider_usage(session, ep.provider_id) if ep.provider_id is not None else {}
    return AiProviderOut(key=ep.key, id=ep.provider_id, name=ep.name, kind=ep.kind, active=ep.active, has_key=True,
                         read_only=ep.provider_id is None, base_url=ep.base_url if is_admin(session, user) else "", **usage)


def _valid_url(kind: str, url: str) -> str:
    try:
        return ai_models.normalize_base_url(kind, url)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


def _name_free(session: Session, name: str, not_id: Optional[int] = None) -> None:
    same = session.exec(select(AiProvider).where(func.lower(AiProvider.name) == name.lower())).first()
    if same is not None and same.id != not_id:
        raise HTTPException(status_code=409, detail=f"There is already a provider called «{same.name}»")


def _provider_row(session: Session, provider_id: int) -> AiProvider:
    row = session.get(AiProvider, provider_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    return row


@router.get("/providers", response_model=list[AiProviderOut])
def list_ai_providers(user: User = Depends(require_observer), session: Session = Depends(get_session)) -> list[AiProviderOut]:
    return [_provider_out(session, user, ep) for ep in ai_models.endpoints(session, only_active=False)]


@router.post("/providers", response_model=AiProviderOut, status_code=201)
def create_ai_provider(
    body: AiProviderIn,
    request: Request = None,  # type: ignore[assignment]
    user: User = Depends(require_superuser),
    session: Session = Depends(get_session),
) -> AiProviderOut:
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Give the provider a name")
    if not body.api_key.strip():
        raise HTTPException(status_code=422, detail="Enter the provider's API key")
    url = _valid_url(body.kind, body.base_url)
    _name_free(session, name)
    row = AiProvider(name=name, kind=body.kind, base_url=url, secret_encrypted=encrypt_secret(body.api_key.strip()), updated_by=user.id)
    session.add(row)
    session.commit()
    session.refresh(row)
    audit.record_audit(session, actor=user, action=audit.AI_PROVIDER_CREATE, target_type="ai_provider", target_id=row.id,
                       target_label=row.name, detail={"kind": row.kind, "base_url": row.base_url}, request=request)
    return _provider_out(session, user, ai_models._of_row(row))


@router.patch("/providers/{provider_id}", response_model=AiProviderOut)
def update_ai_provider(
    provider_id: int,
    body: AiProviderUpdate,
    request: Request = None,  # type: ignore[assignment]
    user: User = Depends(require_superuser),
    session: Session = Depends(get_session),
) -> AiProviderOut:
    row = _provider_row(session, provider_id)
    changed: dict[str, Any] = {}
    key = body.api_key.strip()
    if body.name is not None and body.name.strip() != row.name:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="Give the provider a name")
        _name_free(session, name, row.id)
        row.name = changed["name"] = name
    if body.base_url is not None:
        url = _valid_url(row.kind, body.base_url)
        if url != row.base_url:
            if not key:
                raise HTTPException(status_code=422, detail=NEW_ADDRESS_NEEDS_KEY)
            row.base_url = changed["base_url"] = url
    if key:
        row.secret_encrypted = encrypt_secret(key)
        changed["key_changed"] = True
    if body.active is not None and body.active != row.active:
        row.active = changed["active"] = body.active
    if not changed:
        return _provider_out(session, user, ai_models._of_row(row))
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    row.updated_by = user.id
    session.add(row)
    session.commit()
    session.refresh(row)
    ai_models.clear_cache()
    audit.record_audit(session, actor=user, action=audit.AI_PROVIDER_UPDATE, target_type="ai_provider", target_id=row.id,
                       target_label=row.name, detail=changed, request=request)
    return _provider_out(session, user, ai_models._of_row(row))


@router.delete("/providers/{provider_id}", status_code=204)
def delete_ai_provider(
    provider_id: int,
    confirm: bool = False,
    request: Request = None,  # type: ignore[assignment]
    user: User = Depends(require_superuser),
    session: Session = Depends(get_session),
) -> None:
    """Asked without `confirm` while something depends on it, it says what: the conversations stay
    readable and go on with another model, its enabled models go with it."""
    row = _provider_row(session, provider_id)
    usage = ai_models.provider_usage(session, provider_id)
    if (usage["chats"] or usage["enabled_models"]) and not confirm:
        raise HTTPException(status_code=409, detail=(
            f"«{row.name}» has {usage['enabled_models']} enabled models and {usage['chats']} conversations using them. "
            "Its models go with it; the conversations stay readable and can go on with another model."))
    name = row.name
    ai_models.forget_provider(session, provider_id)
    session.delete(row)
    session.commit()
    audit.record_audit(session, actor=user, action=audit.AI_PROVIDER_DELETE, target_type="ai_provider", target_id=provider_id,
                       target_label=name, detail=usage, request=request)


@router.post("/providers/test", response_model=AiProviderTestOut)
async def test_ai_provider(
    body: AiProviderTest,
    user: User = Depends(require_superuser),
    session: Session = Depends(get_session),
) -> AiProviderTestOut:
    """The provider's own catalog: it needs the address and the key to be right. The answer says which
    is wrong, never what the provider said."""
    saved = ai_models.endpoint_of_key(session, body.key) if body.key else None
    if body.key and saved is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    kind = saved.kind if saved is not None else body.kind
    if kind is None:
        raise HTTPException(status_code=422, detail="Say which kind of provider to test")
    key = body.api_key.strip()
    url = saved.base_url if saved is not None and body.base_url is None else _valid_url(kind, body.base_url or "")
    if saved is None and not key:
        raise HTTPException(status_code=422, detail="Enter the provider's API key")
    if saved is not None and not key and url != saved.base_url:
        raise HTTPException(status_code=422, detail=NEW_ADDRESS_NEEDS_KEY)
    if saved is not None and not key:
        target = saved
    else:
        target = ai_models.Endpoint(provider_id=None, name=(body.name or "").strip() or (saved.name if saved is not None else url), kind=kind, base_url=url,
                                    active=True, stamp="", secret_plain=SecretStr(key))
    try:
        return AiProviderTestOut(ok=True, models=len(await ai_models.read_catalog(target)))
    except ai_models.CatalogError as e:
        return AiProviderTestOut(ok=False, code=e.code, message=e.message)
