"""Gestione utenti e appartenenza ai gruppi. Solo superuser."""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.core.security import hash_password
from app.db.session import get_session
from app.deps.auth import require_superuser
from app.models import User, Group, UserGroupLink
from app.schemas.models import UserOut, UserCreate, UserUpdate

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_superuser)])


@router.get("", response_model=list[UserOut])
def list_users(session: Session = Depends(get_session)):
    return session.exec(select(User)).all()


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(body: UserCreate, session: Session = Depends(get_session)):
    if session.exec(select(User).where(User.email == body.email)).first():
        raise HTTPException(status_code=409, detail="Email già registrata")
    user = User(
        email=body.email,
        full_name=body.full_name,
        hashed_password=hash_password(body.password),
        is_superuser=body.is_superuser,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _get_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Utente non trovato")
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(user_id: int, body: UserUpdate, session: Session = Depends(get_session)):
    user = _get_user(session, user_id)
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.password is not None:
        user.hashed_password = hash_password(body.password)
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.is_superuser is not None:
        user.is_superuser = body.is_superuser
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(
    user_id: int,
    session: Session = Depends(get_session),
    current: User = Depends(require_superuser),
):
    """Elimina l'utente. Il suo CONTENUTO resta (i dati sono dell'organizzazione,
    non della persona): flussi/datasource/connessioni/run perdono solo il
    riferimento al proprietario. Spariscono con lui i permessi personali, le
    appartenenze ai gruppi e la proprietà degli upload non ancora in un flusso.
    """
    if user_id == current.id:
        raise HTTPException(status_code=409, detail="Non puoi eliminare il tuo stesso account")
    user = _get_user(session, user_id)

    # statement bulk espliciti: l'ordine (prima i referenzianti, poi l'utente)
    # è garantito — stessa lezione dei delete di flussi/progetti
    from sqlalchemy import delete as sa_delete, update as sa_update

    from app.models import Connection, Datasource, Flow, Permission, Project, Run, Upload

    session.exec(sa_delete(Permission).where(Permission.user_id == user_id))
    session.exec(sa_delete(UserGroupLink).where(UserGroupLink.user_id == user_id))
    session.exec(sa_delete(Upload).where(Upload.owner_id == user_id))
    for model, col in (
        (Project, Project.owner_id),
        (Flow, Flow.owner_id),
        (Datasource, Datasource.owner_id),
        (Connection, Connection.owner_id),
        (Run, Run.launched_by),
    ):
        session.exec(sa_update(model).where(col == user_id).values({col.key: None}))
    session.delete(user)
    session.commit()


@router.put("/{user_id}/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def add_to_group(user_id: int, group_id: int, session: Session = Depends(get_session)):
    _get_user(session, user_id)
    if session.get(Group, group_id) is None:
        raise HTTPException(status_code=404, detail="Gruppo non trovato")
    exists = session.get(UserGroupLink, (user_id, group_id))
    if exists is None:
        session.add(UserGroupLink(user_id=user_id, group_id=group_id))
        session.commit()


@router.delete("/{user_id}/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_from_group(user_id: int, group_id: int, session: Session = Depends(get_session)):
    link = session.get(UserGroupLink, (user_id, group_id))
    if link is not None:
        session.delete(link)
        session.commit()
