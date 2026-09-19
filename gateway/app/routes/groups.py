"""Gruppi. Lettura per ogni utente autenticato; scrittura solo superuser."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func
from sqlmodel import Session, select

from app.db.session import get_session
from app.deps.auth import get_current_user, require_superuser
from app.models import Group, User, UserGroupLink, Permission
from app.schemas.models import GroupOut, GroupCreate, GroupUpdate
from app.services import audit
from app.services.permissions import ensure_still_admin

router = APIRouter(prefix="/groups", tags=["groups"])


@router.get("", response_model=list[GroupOut], dependencies=[Depends(get_current_user)])
def list_groups(session: Session = Depends(get_session)):
    # quanti membri per gruppo, in UNA query (l'elenco admin lo mostra per ognuno)
    conteggi = dict(
        session.exec(
            select(UserGroupLink.group_id, func.count()).group_by(UserGroupLink.group_id)  # type: ignore[arg-type]
        ).all()
    )
    return [
        GroupOut(
            id=g.id, name=g.name, description=g.description,
            created_at=g.created_at, member_count=conteggi.get(g.id, 0), is_admin=g.is_admin,
        )
        for g in session.exec(select(Group)).all()
    ]


@router.post("", response_model=GroupOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_superuser)])
def create_group(body: GroupCreate, session: Session = Depends(get_session)):
    if session.exec(select(Group).where(Group.name == body.name)).first():
        raise HTTPException(status_code=409, detail="Nome gruppo già esistente")
    group = Group(name=body.name, description=body.description)
    session.add(group)
    session.commit()
    session.refresh(group)
    # esplicito come in list_groups: così la rotta dice la verità anche a chi la
    # chiama direttamente, senza passare dalla serializzazione di FastAPI
    return GroupOut(
        id=group.id, name=group.name, description=group.description,
        created_at=group.created_at, member_count=0,  # appena creato: nessun membro
    )


@router.patch("/{group_id}", response_model=GroupOut)
def update_group(
    group_id: int,
    body: GroupUpdate,
    request: Request = None,  # type: ignore[assignment]
    session: Session = Depends(get_session),
    current: User = Depends(require_superuser),
):
    """Descrizione e, soprattutto, il flag ADMIN: da quel momento ogni membro
    del gruppo è amministratore, e smette di esserlo uscendone."""
    group = session.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Gruppo non trovato")
    era_admin = group.is_admin
    if body.description is not None:
        group.description = body.description
    if body.is_admin is not None:
        group.is_admin = body.is_admin
    session.add(group)
    if era_admin and not group.is_admin:
        ensure_still_admin(session, current)
    session.commit()
    session.refresh(group)
    membri = session.exec(
        select(func.count()).select_from(UserGroupLink).where(UserGroupLink.group_id == group_id)
    ).one()
    if group.is_admin != era_admin:
        audit.record_audit(
            session, actor=current, request=request,
            action=audit.GROUP_PROMOTE if group.is_admin else audit.GROUP_DEMOTE,
            target_type="group", target_id=group.id, target_label=group.name,
            detail={"members": membri},
        )
    return GroupOut(
        id=group.id, name=group.name, description=group.description,
        created_at=group.created_at, member_count=membri, is_admin=group.is_admin,
    )


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_group(
    group_id: int,
    request: Request = None,  # type: ignore[assignment]
    session: Session = Depends(get_session),
    current: User = Depends(require_superuser),
):
    group = session.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Gruppo non trovato")
    era_admin, nome = group.is_admin, group.name
    for link in session.exec(select(UserGroupLink).where(UserGroupLink.group_id == group_id)).all():
        session.delete(link)
    # anche i PERMESSI concessi al gruppo: la chiave esterna li protegge, e senza
    # questo la cancellazione esplodeva in un 500
    for perm in session.exec(select(Permission).where(Permission.group_id == group_id)).all():
        session.delete(perm)
    session.delete(group)
    if era_admin:
        ensure_still_admin(session, current)
    session.commit()
    if era_admin:
        # sparisce un gruppo di amministratori: i suoi membri perdono i privilegi
        audit.record_audit(
            session, actor=current, action=audit.GROUP_DEMOTE, request=request,
            target_type="group", target_id=group_id, target_label=nome,
            detail={"deleted": True},
        )
