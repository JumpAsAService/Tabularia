"""Gestione utenti e appartenenza ai gruppi. Solo superuser."""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session, select

from app.core.security import hash_password
from app.db.session import get_session
from app.deps.auth import get_current_user, require_observer, require_superuser
from app.models import User, Group, UserGroupLink
from app.schemas.models import UserOut, UserCreate, UserUpdate
from app.services import audit, sessions
from app.services.masking import mask_email
from app.services.permissions import ensure_still_admin, is_admin
from app.core.routing import RottaCheRilascia

# Guardia per ROTTA e non per router: l'elenco è una lettura amministrativa
# (la vede anche un osservatore, con i dati personali mascherati), tutto il
# resto resta da amministratore.
router = APIRouter(route_class=RottaCheRilascia, prefix="/users", tags=["users"])


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


def _observer_group_names(session: Session) -> set[str]:
    return set(session.exec(select(Group.name).where(Group.is_observer == True)).all())  # noqa: E712


def _to_out(
    user: User,
    groups: list[str] | None = None,
    admin_names: set[str] | None = None,
    observer_names: set[str] | None = None,
) -> UserOut:
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
        is_observer=user.is_observer,
        observer_groups=[g for g in (groups or []) if g in (observer_names or set())],
        is_service=user.is_service,
    )


@router.get("", response_model=list[UserOut], dependencies=[Depends(require_observer)])
def list_users(
    session: Session = Depends(get_session),
    chi_chiede: User = Depends(get_current_user),
):
    per_utente = _group_names(session)
    admin_names = _admin_group_names(session)
    observer_names = _observer_group_names(session)
    righe = [
        _to_out(u, per_utente.get(u.id, []), admin_names, observer_names)
        for u in session.exec(select(User)).all()
    ]
    # Un OSSERVATORE (non amministratore) vede l'elenco senza i dati personali:
    # gli serve sapere quanti account ci sono e com'è distribuito l'accesso, non
    # chi sono le persone né da dove entrano.
    if not is_admin(session, chi_chiede):
        for r in righe:
            r.email = mask_email(r.email)
            r.full_name = ""
    return righe


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_superuser)])
def create_user(
    body: UserCreate,
    request: Request = None,  # type: ignore[assignment]
    session: Session = Depends(get_session),
    current: User = Depends(require_superuser),
):
    # un utente con password è una credenziale che dura: non la crea un token API
    from app.routes.api_tokens import nega_se_da_token

    nega_se_da_token(request)
    if session.exec(select(User).where(User.email == body.email)).first():
        raise HTTPException(status_code=409, detail="Email già registrata")
    user = User(
        email=body.email,
        full_name=body.full_name,
        hashed_password=hash_password(body.password),
        is_superuser=body.is_superuser,
        is_observer=body.is_observer,
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
    if user.is_observer:
        audit.record_audit(
            session, actor=current, action=audit.OBSERVER_GRANT, request=request,
            target_type="user", target_id=user.id, target_label=user.email,
            detail={"at_creation": True},
        )
    return _to_out(user)  # appena creato: nessun gruppo ancora


def _get_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="Utente non trovato")
    return user


@router.patch("/{user_id}", response_model=UserOut, dependencies=[Depends(require_superuser)])
def update_user(
    user_id: int,
    body: UserUpdate,
    request: Request = None,  # type: ignore[assignment]
    session: Session = Depends(get_session),
    current: User = Depends(require_superuser),
):
    user = _get_user(session, user_id)
    if body.password is not None:
        # una password è una credenziale che dura: non la imposta un token API
        from app.routes.api_tokens import nega_se_da_token

        nega_se_da_token(request)
        if user.is_service:
            raise HTTPException(status_code=422, detail="Un account di servizio entra solo con un token: niente password")
    era_admin = user.is_superuser
    era_osservatore = user.is_observer
    if ((body.is_superuser and not era_admin) or (body.is_observer and not era_osservatore)
            or (body.is_active and not user.is_active)):
        # un ruolo in più o un account riattivato (con la sua password) sono vie
        # d'ingresso che resterebbero dopo la revoca di un token trapelato
        from app.routes.api_tokens import nega_se_da_token
        from app.services import api_tokens as _api_tokens

        nega_se_da_token(request, _api_tokens.NIENTE_RUOLI_DA_TOKEN)
    # una password nuova o un account disattivato chiudono le sessioni aperte e
    # ritirano i token API: chi conosceva la vecchia password, o ne aveva copiato
    # un token, resta fuori — anche se l'account viene riattivato dopo
    chiudi_sessioni = body.password is not None or body.is_active is False
    if body.full_name is not None:
        user.full_name = body.full_name
    if body.password is not None:
        user.hashed_password = hash_password(body.password)
    if body.is_active is not None:
        user.is_active = body.is_active
    if body.is_superuser is not None:
        user.is_superuser = body.is_superuser
    if body.is_observer is not None:
        user.is_observer = body.is_observer
    session.add(user)
    if user.id == current.id:
        # togliersi il flag o disattivarsi: ammesso solo se si resta admin per
        # un'altra via (un gruppo di amministratori)
        ensure_still_admin(session, current)
    chiuse = ritirati = 0
    if chiudi_sessioni:
        # nella stessa transazione della modifica: o tutte e due, o nessuna. Chi
        # cambia la propria password resta dentro con la sessione da cui lo fa
        tranne = sessions.sid_corrente(request) if request is not None and user.id == current.id else None
        chiuse = sessions.chiudi_tutte(session, user.id, tranne=tranne, commit=False)
        # anche i token API: chi aveva la password vecchia poteva essersene creato uno
        from app.services import api_tokens

        ritirati = api_tokens.revoca_tutti(session, user.id, commit=False)
    if user.is_observer and not era_osservatore:
        from app.services import api_tokens

        session.flush()
        ritirati += api_tokens.ritira_se_osservatore(session, [user.id])
    session.commit()
    if chiuse or ritirati:
        motivo = ("password" if body.password is not None else "deactivated" if body.is_active is False else "observer")
        audit.record_audit(
            session, actor=current, request=request, action=audit.SESSION_REVOKE,
            target_type="user", target_id=user.id, target_label=user.email,
            detail={"sessions": chiuse, "tokens": ritirati, "reason": motivo},
        )
    session.refresh(user)
    if user.is_superuser != era_admin:
        audit.record_audit(
            session, actor=current, request=request,
            action=audit.USER_PROMOTE if user.is_superuser else audit.USER_DEMOTE,
            target_type="user", target_id=user.id, target_label=user.email,
        )
    if user.is_observer != era_osservatore:
        audit.record_audit(
            session, actor=current, request=request,
            action=audit.OBSERVER_GRANT if user.is_observer else audit.OBSERVER_REVOKE,
            target_type="user", target_id=user.id, target_label=user.email,
        )
    return _to_out(
        user, _group_names(session).get(user.id, []),
        _admin_group_names(session), _observer_group_names(session),
    )


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_superuser)])
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
    _get_user(session, user_id)
    elimina_utente(session, user_id)


def elimina_utente(session: Session, user_id: int) -> None:
    """Toglie un utente (persona o account di servizio) e ciò che è solo suo;
    vedi `delete_user` per che cosa resta e perché."""
    user = _get_user(session, user_id)
    # statement bulk espliciti: l'ordine (prima i referenzianti, poi l'utente)
    # è garantito — stessa lezione dei delete di flussi/progetti
    from sqlalchemy import delete as sa_delete, update as sa_update

    from app.models import (
        AiChat, AiChatTurn, ApiToken, AuditLog, Connection, Datasource, Flow, Permission, Project, Run, Upload,
        UserSession, Webhook,
    )

    # le conversazioni con l'assistente sono personali: se ne vanno con l'account
    # (prima i turni, che referenziano la chat)
    chat_ids = [c.id for c in session.exec(select(AiChat).where(AiChat.user_id == user_id)).all()]
    if chat_ids:
        session.exec(sa_delete(AiChatTurn).where(AiChatTurn.chat_id.in_(chat_ids)))
        session.exec(sa_delete(AiChat).where(AiChat.id.in_(chat_ids)))
    session.exec(sa_delete(Permission).where(Permission.user_id == user_id))
    session.exec(sa_delete(UserGroupLink).where(UserGroupLink.user_id == user_id))
    session.exec(sa_delete(UserSession).where(UserSession.user_id == user_id))
    session.exec(sa_delete(ApiToken).where(ApiToken.user_id == user_id))
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
        # chi ha creato un token o un webhook: il token o il webhook restano
        (ApiToken, ApiToken.created_by),
        (Webhook, Webhook.created_by),
    ):
        session.exec(sa_update(model).where(col == user_id).values({col.key: None}))
    session.delete(user)
    session.commit()


@router.put("/{user_id}/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_superuser)])
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
    if exists is None and (group.is_admin or group.is_observer):
        # entrare in un gruppo admin od osservatore È ricevere quel ruolo
        from app.routes.api_tokens import nega_se_da_token
        from app.services import api_tokens

        nega_se_da_token(request, api_tokens.NIENTE_RUOLI_DA_TOKEN)
    if exists is None:
        session.add(UserGroupLink(user_id=user_id, group_id=group_id))
        if group.is_observer:
            from app.services import api_tokens

            session.flush()
            api_tokens.ritira_se_osservatore(session, [user_id])
        session.commit()
        if group.is_admin:
            # entrare in un gruppo admin È una promozione: deve restare traccia
            audit.record_audit(
                session, actor=current, action=audit.ADMIN_GROUP_JOIN, request=request,
                target_type="user", target_id=user.id, target_label=user.email,
                detail={"group": group.name},
            )


@router.delete("/{user_id}/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(require_superuser)])
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
