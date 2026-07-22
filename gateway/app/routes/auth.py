from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session, select

from app.core.security import create_access_token, verify_password
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.models import User
from app.services import audit
from app.services.permissions import user_group_ids
from app.schemas.models import LoginRequest, Token, MeOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=Token)
def login(body: LoginRequest, request: Request, session: Session = Depends(get_session)):
    user = session.exec(select(User).where(User.email == body.email)).first()
    if user is None or not verify_password(body.password, user.hashed_password):
        # login fallito: si registra chi ci ha provato e da dove (email tentata)
        audit.record_audit(
            session, actor=user, actor_label=body.email, action=audit.LOGIN_FAILED,
            outcome="failure", detail={"reason": "credenziali errate"}, request=request,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Email o password errati")
    if not user.is_active:
        audit.record_audit(
            session, actor=user, action=audit.LOGIN_FAILED, outcome="failure",
            detail={"reason": "utente disattivato"}, request=request,
        )
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Utente disattivato")
    audit.record_audit(session, actor=user, action=audit.LOGIN, request=request)
    return Token(access_token=create_access_token(user.id))


@router.get("/me", response_model=MeOut)
def me(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    from app.models import Group

    gids = user_group_ids(session, user)
    names = session.exec(select(Group.name).where(Group.id.in_(gids))).all() if gids else []
    return MeOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        is_active=user.is_active,
        is_superuser=user.is_superuser,
        groups=list(names),
    )
