"""Warning banners shown in the Explore.

Read: EVERY authenticated user — banners exist precisely to be seen, and the same list
feeds both the Explore and the Admin page's list.
Write: superuser only. Putting up or taking down a banner changes what the whole
installation sees, so every change ends up in the audit log.
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session, select

from app.db.session import get_session
from app.deps.auth import get_current_user, require_superuser
from app.models import Banner, User
from app.schemas.models import BannerCreate, BannerOut
from app.services import audit
from app.core.routing import ReleasingRoute

router = APIRouter(route_class=ReleasingRoute, prefix="/banners", tags=["banners"])


@router.get("", response_model=list[BannerOut], dependencies=[Depends(get_current_user)])
def list_banners(session: Session = Depends(get_session)):
    """All the banners, newest to oldest."""
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
        raise HTTPException(status_code=422, detail="The banner message is empty")
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
        raise HTTPException(status_code=404, detail="Banner not found")
    label = banner.message[:80]
    session.delete(banner)
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.BANNER_DELETE, target_type="banner",
        target_id=banner_id, target_label=label, request=request,
    )
