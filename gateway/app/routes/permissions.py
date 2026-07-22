"""Concessione/revoca permessi su un progetto. Serve MANAGE sul progetto."""
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlmodel import Session, select

from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Project, Permission, User, Group
from app.services import audit
from app.models.permission import Capability
from app.schemas.models import PermissionOut, PermissionCreate

router = APIRouter(tags=["permissions"])


@router.get("/projects/{project_id}/permissions", response_model=list[PermissionOut])
def list_permissions(project_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Progetto non trovato")
    ensure_can(session, user, project_id, Capability.MANAGE)
    return session.exec(select(Permission).where(Permission.project_id == project_id)).all()


@router.post("/projects/{project_id}/permissions", response_model=PermissionOut, status_code=status.HTTP_201_CREATED)
def grant_permission(
    project_id: int,
    body: PermissionCreate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Progetto non trovato")
    ensure_can(session, user, project_id, Capability.MANAGE)

    if (body.user_id is None) == (body.group_id is None):
        raise HTTPException(status_code=422, detail="Specifica esattamente uno tra user_id e group_id")
    if body.user_id is not None and session.get(User, body.user_id) is None:
        raise HTTPException(status_code=404, detail="Utente non trovato")
    if body.group_id is not None and session.get(Group, body.group_id) is None:
        raise HTTPException(status_code=404, detail="Gruppo non trovato")

    # niente duplicati esatti (stesso soggetto + stessa capability sullo stesso progetto)
    dup = session.exec(
        select(Permission).where(
            Permission.project_id == project_id,
            Permission.user_id == body.user_id,
            Permission.group_id == body.group_id,
            Permission.capability == body.capability.value,
        )
    ).first()
    if dup:
        return dup

    perm = Permission(
        project_id=project_id,
        user_id=body.user_id,
        group_id=body.group_id,
        capability=body.capability.value,
    )
    session.add(perm)
    session.commit()
    session.refresh(perm)
    audit.record_audit(
        session, actor=user, action=audit.PERM_GRANT, target_type="permission",
        target_id=perm.id, target_label=body.capability.value,
        detail={"project_id": project_id, "user_id": body.user_id, "group_id": body.group_id,
                "capability": body.capability.value},
        request=request,
    )
    return perm


@router.delete("/permissions/{permission_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_permission(permission_id: int, request: Request, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    perm = session.get(Permission, permission_id)
    if perm is None:
        raise HTTPException(status_code=404, detail="Permesso non trovato")
    ensure_can(session, user, perm.project_id, Capability.MANAGE)
    detail = {"project_id": perm.project_id, "user_id": perm.user_id, "group_id": perm.group_id,
              "capability": perm.capability}
    session.delete(perm)
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.PERM_REVOKE, target_type="permission",
        target_id=permission_id, target_label=perm.capability, detail=detail, request=request,
    )
