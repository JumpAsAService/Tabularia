"""API tokens: one's own (`/auth/tokens`) and, for the administrator, everyone's
(`/admin/api-tokens`). The service accounts' ones are in routes/service_accounts.py.
The logic is in services/api_tokens.py."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import get_current_user, require_observer, require_superuser
from app.models import ApiToken, User
from app.schemas.models import UtcDateTime
from app.services import api_tokens, audit
from app.services.masking import mask_email
from app.services.permissions import is_admin, is_observer_only

router = APIRouter(route_class=ReleasingRoute, tags=["api-tokens"])


class TokenCreate(BaseModel):
    name: str
    scope: Literal["read", "write"] = "read"
    expires_in_days: int = 90


class TokenOut(BaseModel):
    id: int
    name: str
    prefix: str  # the token's beginning, to recognize it: not enough to use it
    scope: str
    created_at: UtcDateTime
    expires_at: UtcDateTime
    last_used_at: Optional[UtcDateTime]
    last_used_ip: Optional[str]


class TokenCreated(TokenOut):
    """The answer at creation: the ONLY time the secret is seen."""
    token: str


class AdminTokenOut(TokenOut):
    user_id: int
    user_email: str
    user_name: str
    is_service: bool


def token_out(t: ApiToken) -> TokenOut:
    # by attributes and not with model_dump(): after a commit the row is «expired», and
    # model_dump() of an expired row returns empty instead of reading it again
    return TokenOut(**{field: getattr(t, field) for field in TokenOut.model_fields})


def deny_if_from_token(request: Request, table: dict[str, str] = api_tokens.NO_TOKEN_FROM_TOKEN) -> None:
    """A token creates no credentials and grants no roles: when it leaks, revoking it must be
    enough, without its having left another way in behind (a token, a password, a
    reactivated account, one more administrator). These things are done from a session
    (the browser, a real login)."""
    if api_tokens.current_name(request):
        raise HTTPException(status_code=403, detail=api_tokens.message(table, request))


def create_and_record(
    session: Session, request: Request, owner: User, body: TokenCreate, asker: User,
) -> TokenCreated:
    """Creates the token of `per` (asked by `chi`) and writes it in the audit."""
    scope = body.scope
    if is_observer_only(session, owner):
        scope = "read"  # whoever only looks has nothing to write
    row, secret = api_tokens.create(session, owner, body.name, scope, body.expires_in_days, asker)
    audit.record_audit(
        session, actor=asker, action=audit.TOKEN_CREATE, request=request,
        target_type="user", target_id=owner.id, target_label=owner.email,
        detail={"name": row.name, "scope": row.scope, "prefix": row.prefix, "expires_at": row.expires_at},
    )
    return TokenCreated(**token_out(row).model_dump(), token=secret)


# ── one's own ───────────────────────────────────────────────────────────────

@router.get("/auth/tokens", response_model=list[TokenOut])
def my_tokens(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    return [token_out(t) for t in api_tokens.of_user(session, user.id)]


@router.post("/auth/tokens", response_model=TokenCreated, status_code=status.HTTP_201_CREATED)
def create_my_token(
    body: TokenCreate, request: Request,
    user: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    """A token for one's own scripts: it holds with one's own permissions, never more."""
    deny_if_from_token(request)
    if not get_settings().auth.personal_tokens and not user.is_service:
        raise HTTPException(status_code=403, detail="Personal tokens are turned off on this installation")
    if is_observer_only(session, user):
        # the observer is a demo's shared guest: a personal credential would survive a change
        # of its password, and everyone would see it
        raise HTTPException(status_code=403, detail="Observers do not have personal tokens")
    return create_and_record(session, request, user, body, user)


@router.delete("/auth/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_my_token(
    token_id: int, request: Request,
    user: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    """Revokes one of one's own tokens (the one asking too, when it is read-write: a script
    may withdraw its credential when the work is done; a read-only one changes nothing,
    not even this). The others': 404."""
    row = api_tokens.revoke(session, token_id, user.id)
    if row is None:
        raise HTTPException(status_code=404, detail="Token not found")
    audit.record_audit(
        session, actor=user, action=audit.TOKEN_REVOKE, request=request,
        target_type="user", target_id=user.id, target_label=user.email,
        detail={"name": row.name, "prefix": row.prefix},
    )
    return None


# ── everyone's (administration) ─────────────────────────────────────────────

@router.get("/admin/api-tokens", response_model=list[AdminTokenOut])
def all_tokens(
    session: Session = Depends(get_session),
    asker: User = Depends(require_observer),
):
    """All the valid tokens, with who owns them: to withdraw the one that leaked. To the
    observer without who and from where."""
    full = is_admin(session, asker)
    rows = session.exec(
        select(ApiToken, User).join(User, User.id == ApiToken.user_id)
        .where(ApiToken.expires_at > api_tokens.now())
        .order_by(ApiToken.created_at.desc()).limit(500)
    ).all()
    out = []
    for t, u in rows:
        base = token_out(t).model_dump()
        if not full:
            base.update(last_used_ip=None, prefix="tab_…")
        out.append(AdminTokenOut(
            **base, user_id=u.id,
            user_email=u.email if full or u.is_service else mask_email(u.email),
            user_name=u.full_name if full or u.is_service else "",
            is_service=u.is_service,
        ))
    return out


@router.delete("/admin/api-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_any_token(
    token_id: int, request: Request,
    session: Session = Depends(get_session),
    admin: User = Depends(require_superuser),
):
    row = api_tokens.revoke(session, token_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Token not found")
    owner = session.get(User, row.user_id)
    audit.record_audit(
        session, actor=admin, action=audit.TOKEN_REVOKE, request=request,
        target_type="user", target_id=row.user_id, target_label=owner.email if owner else None,
        detail={"name": row.name, "prefix": row.prefix},
    )
    return None
