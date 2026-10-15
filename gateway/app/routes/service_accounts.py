"""Account di servizio: utenti che sono un'integrazione, non una persona.

Li crea e li toglie l'amministratore; l'osservatore li vede. Entrano solo con un
token API (nessuna password, nessun SSO). I permessi li prendono come tutti: dai
gruppi (`/users/{id}/groups/{gid}`) e dalle cartelle (i permessi per utente), con
le rotte che esistono già — un account di servizio È un utente."""
import re
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session, func, select

from app.core.routing import RottaCheRilascia
from app.db.session import get_session
from app.deps.auth import require_observer, require_superuser
from app.models import ApiToken, Group, User, UserGroupLink
from app.routes.api_tokens import TokenCreate, TokenCreated, TokenOut, crea_e_registra, nega_se_da_token, token_out
from app.schemas.models import UtcDateTime
from app.services import api_tokens, audit
from app.services.permissions import is_admin

router = APIRouter(route_class=RottaCheRilascia, prefix="/service-accounts", tags=["service-accounts"])

# dominio riservato (RFC 2606): un indirizzo così non riceve posta, e nessun IdP lo
# afferma come verificato — non può essere preso da un login SSO
DOMINIO = "service.invalid"


class ServiceAccountCreate(BaseModel):
    name: str
    description: str = ""


class ServiceAccountOut(BaseModel):
    id: int
    name: str
    email: str  # sintetica: è la chiave con cui compare in permessi e audit
    is_active: bool
    created_at: Optional[UtcDateTime]
    last_seen_at: Optional[UtcDateTime]
    groups: list[str]
    tokens: int  # quanti token validi ha


def _uno(session: Session, account_id: int) -> User:
    u = session.get(User, account_id)
    if u is None or not u.is_service:
        raise HTTPException(status_code=404, detail="Service account not found")
    return u


def _out(session: Session, u: User) -> ServiceAccountOut:
    gruppi = session.exec(
        select(Group.name).join(UserGroupLink, UserGroupLink.group_id == Group.id).where(UserGroupLink.user_id == u.id)
    ).all()
    n = session.exec(
        select(func.count()).select_from(ApiToken)
        .where(ApiToken.user_id == u.id, ApiToken.expires_at > api_tokens.adesso())
    ).one()
    return ServiceAccountOut(
        id=u.id, name=u.full_name, email=u.email, is_active=u.is_active, created_at=u.created_at,
        last_seen_at=u.last_seen_at, groups=sorted(gruppi), tokens=n,
    )


@router.get("", response_model=list[ServiceAccountOut], dependencies=[Depends(require_observer)])
def list_service_accounts(session: Session = Depends(get_session)):
    return [_out(session, u) for u in session.exec(select(User).where(User.is_service == True).order_by(User.full_name)).all()]  # noqa: E712


@router.post("", response_model=ServiceAccountOut, status_code=status.HTTP_201_CREATED)
def create_service_account(
    body: ServiceAccountCreate, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    nega_se_da_token(request)
    nome = body.name.strip()
    if not nome or len(nome) > 80:
        raise HTTPException(status_code=422, detail="A name is required (at most 80 characters)")
    radice = re.sub(r"[^a-z0-9]+", "-", nome.lower()).strip("-")[:40] or "servizio"
    email = f"{radice}@{DOMINIO}"
    if session.exec(select(User).where(User.email == email)).first() is not None:
        email = f"{radice}-{secrets.token_hex(3)}@{DOMINIO}"
    u = User(email=email, full_name=nome, hashed_password=None, is_service=True)
    session.add(u)
    session.commit()
    session.refresh(u)
    audit.record_audit(
        session, actor=admin, action=audit.SERVICE_ACCOUNT_CREATE, request=request,
        target_type="user", target_id=u.id, target_label=u.email,
        detail={"name": nome, "description": body.description.strip()[:200] or None},
    )
    return _out(session, u)


@router.delete("/{account_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_service_account(
    account_id: int, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    """Toglie l'account e i suoi token. Come per una persona, ciò che ha creato
    resta (perde solo il proprietario) e l'audit non si tocca."""
    nega_se_da_token(request)
    u = _uno(session, account_id)
    email = u.email
    from app.routes.users import elimina_utente

    elimina_utente(session, account_id)
    audit.record_audit(
        session, actor=admin, action=audit.SERVICE_ACCOUNT_DELETE, request=request,
        target_type="user", target_id=account_id, target_label=email,
    )
    return None


@router.get("/{account_id}/tokens", response_model=list[TokenOut])
def list_account_tokens(account_id: int, session: Session = Depends(get_session), chi: User = Depends(require_observer)):
    _uno(session, account_id)
    pieno = is_admin(session, chi)
    out = [token_out(t) for t in api_tokens.di(session, account_id)]
    if not pieno:  # come /admin/api-tokens: l'osservatore non vede inizio né indirizzo
        for t in out:
            t.prefix, t.last_used_ip = "tab_…", None
    return out


@router.post("/{account_id}/tokens", response_model=TokenCreated, status_code=status.HTTP_201_CREATED)
def create_account_token(
    account_id: int, body: TokenCreate, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    nega_se_da_token(request)
    return crea_e_registra(session, request, _uno(session, account_id), body, admin)


@router.delete("/{account_id}/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_account_token(
    account_id: int, token_id: int, request: Request,
    session: Session = Depends(get_session), admin: User = Depends(require_superuser),
):
    u = _uno(session, account_id)
    riga = api_tokens.revoca(session, token_id, account_id)
    if riga is None:
        raise HTTPException(status_code=404, detail="Token not found")
    audit.record_audit(
        session, actor=admin, action=audit.TOKEN_REVOKE, request=request,
        target_type="user", target_id=u.id, target_label=u.email,
        detail={"name": riga.name, "prefix": riga.prefix},
    )
    return None
