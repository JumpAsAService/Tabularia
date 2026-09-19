"""Gestione utenti e appartenenza ai gruppi. Solo superuser."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session, select

from app.core.security import hash_password
from app.db.session import get_session
from app.deps.auth import require_superuser
from app.models import User, Group, UserGroupLink
from app.schemas.models import UserOut, UserCreate, UserUpdate
from app.services import audit
from app.services.permissions import ensure_still_admin

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_superuser)])


def _group_names(session: Session) -> dict[int, list[str]]:
    """Nomi dei gruppi per utente, in UNA query: l'elenco admin li mostra tutti e
    una query per utente sarebbe un N+1 gratuito."""
    righe = session.exec(
        select(UserGroupLink.user_id, Group.name).where(UserGroupLink.group_id == Group.id)
    ).all()
    per_utente: dict[int, list[str]] = {}
    for user_id, nome in righe:
        per_utente.setdefault(user_id, []).append(nome)
    return {k: sorted(v) for k, v in per_utente.items()}


def _admin_group_names(session: Session) -> set[str]:
    return set(session.exec(select(Group.name).where(Group.is_admin == True)).all())  # noqa: E712


def _to_out(user: User, groups: list[str] | None = None, admin_names: set[str] | None = None) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        full_name=user.full_name,
        is_active=user.is_active,
        is_superuser=user.is_superuser,
        created_at=user.created_at,
        last_seen_at=user.last_seen_at,
        # nessuna password locale ⇒ l'account entra SOLO dall'IdP (vedi services/sso.py)
        sso_only=user.hashed_password is None,
        groups=groups or [],
        admin_groups=[g for g in (groups or []) if g in (admin_names or set())],
    )


@router.get("", response_model=list[UserOut])
def list_users(session: Session = Depends(get_session)):
    per_utente = _group_names(session)
    admin_names = _admin_group_names(session)
    return [_to_out(u, per_utente.get(u.id, []), admin_names) for u in session.exec(select(User)).all()]


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED)
def create_user(
    body: UserCreate,
    request: Request = None,  # type: ignore[assignment]
    session: Session = Depends(get_session),
    current: User = Depends(require_superuser),
):
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
    if user.is_superuser:
        audit.record_audit(
            session, actor=current, action=audit.USER_PROMOTE, request=request,
            target_type="user", target_id=user.id, target_label=user.email,
            detail={"at_creation": True},
        )
    return _to_out(user)  # appena creato: nessun gruppo ancora


def _get_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Utente non trovato")
    return user


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: int,
    body: UserUpdate,
    request: Request = None,  # type: ignore[assignment]
    session: Session = Depends(get_session),
    current: User = Depends(require_superuser),
):
    user = _get_user(session, user_id)
    era_admin = user.is_superuser
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.password is not None:
        user.hashed_password = hash_password(body.password)
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.is_superuser is not None:
        user.is_superuser = body.is_superuser
    session.add(user)
    if user.id == current.id:
        # togliersi il flag o disattivarsi: ammesso solo se si resta admin per
        # un'altra via (un gruppo di amministratori)
        ensure_still_admin(session, current)
    session.commit()
    session.refresh(user)
    if user.is_superuser != era_admin:
        audit.record_audit(
            session, actor=current, request=request,
            action=audit.USER_PROMOTE if user.is_superuser else audit.USER_DEMOTE,
            target_type="user", target_id=user.id, target_label=user.email,
        )
    return _to_out(user, _group_names(session).get(user.id, []), _admin_group_names(session))


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

    L'AUDIT invece non si tocca: è append-only e deve restare leggibile dopo che
    la persona se n'è andata, altrimenti cancellare un account cancellerebbe le
    prove di ciò che ha fatto. Si azzera solo il riferimento `actor_id`, mentre
    `actor_label` conserva l'email di allora — è esattamente il motivo per cui
    quel campo esiste (vedi models/audit.py).
    """
    if user_id == current.id:
        raise HTTPException(status_code=409, detail="Non puoi eliminare il tuo stesso account")
    user = _get_user(session, user_id)

    # statement bulk espliciti: l'ordine (prima i referenzianti, poi l'utente)
    # è garantito — stessa lezione dei delete di flussi/progetti
    from sqlalchemy import delete as sa_delete, update as sa_update

    from app.models import AiChat, AiChatTurn, AuditLog, Connection, Datasource, Flow, Permission, Project, Run, Upload

    # le conversazioni con l'assistente sono personali: se ne vanno con l'account
    # (prima i turni, che referenziano la chat)
    chat_ids = [c.id for c in session.exec(select(AiChat).where(AiChat.user_id == user_id)).all()]
    if chat_ids:
        session.exec(sa_delete(AiChatTurn).where(AiChatTurn.chat_id.in_(chat_ids)))
        session.exec(sa_delete(AiChat).where(AiChat.id.in_(chat_ids)))
    session.exec(sa_delete(Permission).where(Permission.user_id == user_id))
    session.exec(sa_delete(UserGroupLink).where(UserGroupLink.user_id == user_id))
    session.exec(sa_delete(Upload).where(Upload.owner_id == user_id))
    # `audit_logs.actor_id` è una FK verso users: senza azzerarla il DELETE
    # viola il vincolo e l'API risponde 500. Non è un caso limite — `auth.login`
    # è auditato, quindi bastava aver fatto accesso una volta per non essere più
    # eliminabili. Non emergeva nei test perché SQLite ha le foreign key spente.
    session.exec(sa_update(AuditLog).where(AuditLog.actor_id == user_id).values(actor_id=None))
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
def add_to_group(
    user_id: int,
    group_id: int,
    request: Request = None,  # type: ignore[assignment]
    session: Session = Depends(get_session),
    current: User = Depends(require_superuser),
):
    user = _get_user(session, user_id)
    group = session.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Gruppo non trovato")
    exists = session.get(UserGroupLink, (user_id, group_id))
    if exists is None:
        session.add(UserGroupLink(user_id=user_id, group_id=group_id))
        session.commit()
        if group.is_admin:
            # entrare in un gruppo admin È una promozione: deve restare traccia
            audit.record_audit(
                session, actor=current, action=audit.ADMIN_GROUP_JOIN, request=request,
                target_type="user", target_id=user.id, target_label=user.email,
                detail={"group": group.name},
            )


@router.delete("/{user_id}/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_from_group(
    user_id: int,
    group_id: int,
    request: Request = None,  # type: ignore[assignment]
    session: Session = Depends(get_session),
    current: User = Depends(require_superuser),
):
    link = session.get(UserGroupLink, (user_id, group_id))
    if link is not None:
        group = session.get(Group, group_id)
        user = session.get(User, user_id)
        session.delete(link)
        if user_id == current.id:
            ensure_still_admin(session, current)
        session.commit()
        if group is not None and group.is_admin and user is not None:
            audit.record_audit(
                session, actor=current, action=audit.ADMIN_GROUP_LEAVE, request=request,
                target_type="user", target_id=user.id, target_label=user.email,
                detail={"group": group.name},
            )
