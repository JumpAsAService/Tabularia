"""Banner di avvertimento mostrati nell'Explore.

Lettura: OGNI utente autenticato — i banner esistono proprio per essere visti, e
la stessa lista alimenta sia l'Explore sia l'elenco della pagina Admin.
Scrittura: solo superuser. Mettere o togliere un banner cambia ciò che vede
l'intera installazione, quindi ogni modifica finisce nell'audit log.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session, select

from app.db.session import get_session
from app.deps.auth import get_current_user, require_superuser
from app.models import Banner, User
from app.schemas.models import BannerCreate, BannerOut
from app.services import audit

router = APIRouter(prefix="/banners", tags=["banners"])


@router.get("", response_model=list[BannerOut], dependencies=[Depends(get_current_user)])
def list_banners(session: Session = Depends(get_session)):
    """Tutti i banner, dal più recente al più vecchio."""
    return session.exec(select(Banner).order_by(Banner.id.desc())).all()  # type: ignore[union-attr]


@router.post("", response_model=BannerOut, status_code=status.HTTP_201_CREATED)
def create_banner(
    body: BannerCreate,
    request: Request = None,  # type: ignore[assignment]
    user: User = Depends(require_superuser),
    session: Session = Depends(get_session),
):
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="Il messaggio del banner è vuoto")
    banner = Banner(message=message, level=body.level, created_by=user.id)
    session.add(banner)
    session.commit()
    session.refresh(banner)
    audit.record_audit(
        session, actor=user, action=audit.BANNER_CREATE, target_type="banner",
        target_id=banner.id, target_label=message[:80],
        detail={"level": banner.level}, request=request,
    )
    return banner


@router.delete("/{banner_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_banner(
    banner_id: int,
    request: Request = None,  # type: ignore[assignment]
    user: User = Depends(require_superuser),
    session: Session = Depends(get_session),
):
    banner = session.get(Banner, banner_id)
    if banner is None:
        raise HTTPException(status_code=404, detail="Banner non trovato")
    etichetta = banner.message[:80]
    session.delete(banner)
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.BANNER_DELETE, target_type="banner",
        target_id=banner_id, target_label=etichetta, request=request,
    )
