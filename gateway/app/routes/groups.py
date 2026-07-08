"""Gruppi. Lettura per ogni utente autenticato; scrittura solo superuser."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.db.session import get_session
from app.deps.auth import get_current_user, require_superuser
from app.models import Group, UserGroupLink
from app.schemas.models import GroupOut, GroupCreate

router = APIRouter(prefix="/groups", tags=["groups"])


@router.get("", response_model=list[GroupOut], dependencies=[Depends(get_current_user)])
def list_groups(session: Session = Depends(get_session)):
    return session.exec(select(Group)).all()


@router.post("", response_model=GroupOut, status_code=status.HTTP_201_CREATED,
             dependencies=[Depends(require_superuser)])
def create_group(body: GroupCreate, session: Session = Depends(get_session)):
    if session.exec(select(Group).where(Group.name == body.name)).first():
        raise HTTPException(status_code=409, detail="Nome gruppo già esistente")
    group = Group(name=body.name, description=body.description)
    session.add(group)
    session.commit()
    session.refresh(group)
    return group


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT,
               dependencies=[Depends(require_superuser)])
def delete_group(group_id: int, session: Session = Depends(get_session)):
    group = session.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Gruppo non trovato")
    for link in session.exec(select(UserGroupLink).where(UserGroupLink.group_id == group_id)).all():
        session.delete(link)
    session.delete(group)
    session.commit()
