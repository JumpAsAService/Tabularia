"""Token API: i propri (`/auth/tokens`) e, per l'amministratore, quelli di tutti
(`/admin/api-tokens`). Quelli degli account di servizio stanno in
routes/service_accounts.py. La logica è in services/api_tokens.py."""
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.routing import RottaCheRilascia
from app.db.session import get_session
from app.deps.auth import get_current_user, require_observer, require_superuser
from app.models import ApiToken, User
from app.schemas.models import UtcDateTime
from app.services import api_tokens, audit
from app.services.masking import mask_email
from app.services.permissions import is_admin, is_observer_only

router = APIRouter(route_class=RottaCheRilascia, tags=["api-tokens"])


class TokenCreate(BaseModel):
    name: str
    scope: Literal["read", "write"] = "read"
    expires_in_days: int = 90


class TokenOut(BaseModel):
    id: int
    name: str
    prefix: str  # l'inizio del token, per riconoscerlo: non basta a usarlo
    scope: str
    created_at: UtcDateTime
    expires_at: UtcDateTime
    last_used_at: Optional[UtcDateTime]
    last_used_ip: Optional[str]


class TokenCreated(TokenOut):
    """La risposta alla creazione: l'UNICA volta che il segreto si vede."""
    token: str


class AdminTokenOut(TokenOut):
    user_id: int
    user_email: str
    user_name: str
    is_service: bool


def token_out(t: ApiToken) -> TokenOut:
    # per attributi e non con model_dump(): dopo un commit la riga è «scaduta», e
    # model_dump() di una riga scaduta torna vuoto invece di rileggerla
    return TokenOut(**{campo: getattr(t, campo) for campo in TokenOut.model_fields})


def nega_se_da_token(request: Request, tabella: dict[str, str] = api_tokens.NIENTE_TOKEN_DA_TOKEN) -> None:
    """Un token non crea credenziali né concede ruoli: se trapela, revocarlo deve
    bastare, senza che si sia lasciato dietro un'altra via d'ingresso (un token,
    una password, un account riattivato, un amministratore in più). Queste cose si
    fanno da una sessione (il browser, un login vero)."""
    if api_tokens.nome_corrente(request):
        raise HTTPException(status_code=403, detail=api_tokens.messaggio(tabella, request))


def crea_e_registra(
    session: Session, request: Request, per: User, body: TokenCreate, chi: User,
) -> TokenCreated:
    """Crea il token di `per` (chiesto da `chi`) e lo scrive nell'audit."""
    scope = body.scope
    if is_observer_only(session, per):
        scope = "read"  # chi guarda e basta non ha niente da scrivere
    riga, segreto = api_tokens.crea(session, per, body.name, scope, body.expires_in_days, chi)
    audit.record_audit(
        session, actor=chi, action=audit.TOKEN_CREATE, request=request,
        target_type="user", target_id=per.id, target_label=per.email,
        detail={"name": riga.name, "scope": riga.scope, "prefix": riga.prefix, "expires_at": riga.expires_at},
    )
    return TokenCreated(**token_out(riga).model_dump(), token=segreto)


# ── i propri ────────────────────────────────────────────────────────────────

@router.get("/auth/tokens", response_model=list[TokenOut])
def my_tokens(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    return [token_out(t) for t in api_tokens.di(session, user.id)]


@router.post("/auth/tokens", response_model=TokenCreated, status_code=status.HTTP_201_CREATED)
def create_my_token(
    body: TokenCreate, request: Request,
    user: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    """Un token per i propri script: vale con i propri permessi, mai di più."""
    nega_se_da_token(request)
    if not get_settings().auth.personal_tokens and not user.is_service:
        raise HTTPException(status_code=403, detail="Personal tokens are turned off on this installation")
    if is_observer_only(session, user):
        # l'osservatore è l'ospite condiviso di una demo: una credenziale personale
        # sopravviverebbe al cambio della sua password, e la vedrebbero tutti
        raise HTTPException(status_code=403, detail="Observers do not have personal tokens")
    return crea_e_registra(session, request, user, body, user)


@router.delete("/auth/tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_my_token(
    token_id: int, request: Request,
    user: User = Depends(get_current_user), session: Session = Depends(get_session),
):
    """Revoca uno dei propri token (anche quello con cui si chiede, se è di
    lettura e scrittura: uno script può ritirare la sua credenziale finito il
    lavoro; uno di sola lettura non modifica niente, nemmeno questo). Quelli degli
    altri: 404."""
    riga = api_tokens.revoca(session, token_id, user.id)
    if riga is None:
        raise HTTPException(status_code=404, detail="Token not found")
    audit.record_audit(
        session, actor=user, action=audit.TOKEN_REVOKE, request=request,
        target_type="user", target_id=user.id, target_label=user.email,
        detail={"name": riga.name, "prefix": riga.prefix},
    )
    return None


# ── quelli di tutti (amministrazione) ───────────────────────────────────────

@router.get("/admin/api-tokens", response_model=list[AdminTokenOut])
def all_tokens(
    session: Session = Depends(get_session),
    chi_chiede: User = Depends(require_observer),
):
    """Tutti i token validi, con chi li possiede: per ritirare quello che è
    trapelato. All'osservatore senza chi né da dove."""
    pieno = is_admin(session, chi_chiede)
    righe = session.exec(
        select(ApiToken, User).join(User, User.id == ApiToken.user_id)
        .where(ApiToken.expires_at > api_tokens.adesso())
        .order_by(ApiToken.created_at.desc()).limit(500)
    ).all()
    out = []
    for t, u in righe:
        base = token_out(t).model_dump()
        if not pieno:
            base.update(last_used_ip=None, prefix="tab_…")
        out.append(AdminTokenOut(
            **base, user_id=u.id,
            user_email=u.email if pieno or u.is_service else mask_email(u.email),
            user_name=u.full_name if pieno or u.is_service else "",
            is_service=u.is_service,
        ))
    return out


@router.delete("/admin/api-tokens/{token_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_any_token(
    token_id: int, request: Request,
    session: Session = Depends(get_session),
    admin: User = Depends(require_superuser),
):
    riga = api_tokens.revoca(session, token_id)
    if riga is None:
        raise HTTPException(status_code=404, detail="Token not found")
    proprietario = session.get(User, riga.user_id)
    audit.record_audit(
        session, actor=admin, action=audit.TOKEN_REVOKE, request=request,
        target_type="user", target_id=riga.user_id, target_label=proprietario.email if proprietario else None,
        detail={"name": riga.name, "prefix": riga.prefix},
    )
    return None
