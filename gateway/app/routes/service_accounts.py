"""Service accounts: users that are an integration, not a person.

The administrator creates and removes them; the observer sees them. They sign in only
with an API token (no password, no SSO). They take permissions as everyone does: from
the groups (`/users/{id}/groups/{gid}`) and from the folders (the per-user
permissions), with the routes that exist already — a service account IS a user."""
import re
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session, func, select

from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import require_observer, require_superuser
from app.models import ApiToken, Group, User, UserGroupLink
from app.routes.api_tokens import TokenCreate, TokenCreated, TokenOut, create_and_record, deny_if_from_token, token_out
from app.schemas.models import UtcDateTime
from app.services import api_tokens, audit
from app.services.permissions import is_admin

router = APIRouter(route_class=ReleasingRoute, prefix="/service-accounts", tags=["service-accounts"])

# reserved domain (RFC 2606): such an address receives no mail, and no IdP asserts it
# as verified — an SSO login cannot take it
DOMAIN = "service.invalid"


class ServiceAccountCreate(BaseModel):
    name: str
    description: str = ""


class ServiceAccountOut(BaseModel):
    id: int
    name: str
    email: str  # synthetic: it is the key it shows up with in permissions and audit
    is_active: bool
    created_at: Optional[UtcDateTime]
    last_seen_at: Optional[UtcDateTime]
    groups: list[str]
    tokens: int  # how many valid tokens it has


def _one(session: Session, account_id: int) -> User:
    u = session.get(User, account_id)
    if u is None or not u.is_service:
        raise HTTPException(status_code=404, detail="Service account not found")
    return u


def _out(session: Session, u: User) -> ServiceAccountOut:
    groups = session.exec(
        select(Group.name).join(UserGroupLink, UserGroupLink.group_id == Group.id).where(UserGroupLink.user_id == u.id)
    ).all()
    n = session.exec(
        select(func.count()).select_from(ApiToken)
        .where(ApiToken.user_id == u.id, ApiToken.expires_at > api_tokens.now())
    ).one()
    return ServiceAccountOut(
        id=u.id, name=u.full_name, email=u.email, is_active=u.is_active, created_at=u.created_at,
        last_seen_at=u.last_seen_at, groups=sorted(groups), tokens=n,
    )


@router.get("", response_model=list[ServiceAccountOut], dependencies=[Depends(require_observer)])
def list_service_accounts(session: Session = Depends(get_session)):
    return [_out(session, u) for u in session.exec(select(User).where(User.is_service == True).order_by(User.full_name)).all()]  # noqa: E712


@router.post("", response_model=ServiceAccountOut, status_code=status.HTTP_201_CREATED)
def create_service_account(
    body: ServiceAccountCreate, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    deny_if_from_token(request)
    name = body.name.strip()
    if not name or len(name) > 80:
        raise HTTPException(status_code=422, detail="A name is required (at most 80 characters)")
    root = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "service"
    email = f"{root}@{DOMAIN}"
    if session.exec(select(User).where(User.email == email)).first() is not None:
        email = f"{root}-{secrets.token_hex(3)}@{DOMAIN}"
    u = User(email=email, full_name=name, hashed_password=None, is_service=True)
    session.add(u)
    session.commit()
    session.refresh(u)
    audit.record_audit(
        session, actor=admin, action=audit.SERVICE_ACCOUNT_CREATE, request=request,
        target_type="user", target_id=u.id, target_label=u.email,
        detail={"name": name, "description": body.description.strip()[:200] or None},
    )
    return _out(session, u)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_service_account(
    account_id: int, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    """Removes the account and its tokens. As for a person, what it created stays (it loses
    only the owner) and the audit is not touched."""
    deny_if_from_token(request)
    u = _one(session, account_id)
    email = u.email
    from app.routes.users import remove_user

    remove_user(session, account_id)
    audit.record_audit(
        session, actor=admin, action=audit.SERVICE_ACCOUNT_DELETE, request=request,
        target_type="user", target_id=account_id, target_label=email,
    )
    return None


@router.get("/{account_id}/tokens", response_model=list[TokenOut])
def list_account_tokens(account_id: int, session: Session = Depends(get_session), chi: User = Depends(require_observer)):
    _one(session, account_id)
    full = is_admin(session, chi)
    out = [token_out(t) for t in api_tokens.of_user(session, account_id)]
    if not full:  # as /admin/api-tokens: the observer sees neither beginning nor address
        for t in out:
            t.prefix, t.last_used_ip = "tab_…", None
    return out


@router.post("/{account_id}/tokens", response_model=TokenCreated, status_code=status.HTTP_201_CREATED)
def create_account_token(
    account_id: int, body: TokenCreate, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    deny_if_from_token(request)
    return create_and_record(session, request, _one(session, account_id), body, admin)


@router.delete("/{account_id}/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_account_token(
    account_id: int, token_id: int, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    u = _one(session, account_id)
    row = api_tokens.revoke(session, token_id, account_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Token not found")
    audit.record_audit(
        session, actor=admin, action=audit.TOKEN_REVOKE, request=request,
        target_type="user", target_id=u.id, target_label=u.email,
        detail={"name": row.name, "prefix": row.prefix},
    )
    return None
