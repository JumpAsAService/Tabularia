"""Users management and group membership. Superuser only."""
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.security import hash_password, verify_password
from app.db.session import get_session
from app.deps.auth import get_current_user, require_observer, require_superuser
from app.models import ContainerRegistryAccess, User, Group, UserGroupLink
from app.schemas.models import LocaleChange, PasswordChange, UserOut, UserCreate, UserUpdate
from app.services import audit, login_throttle, sessions
from app.services.masking import mask_email
from app.services.permissions import ensure_still_admin, is_admin, is_observer_only
from app.core.routing import ReleasingRoute

# A guard per ROUTE and not per router: the list is an administrative read (an observer
# sees it too, with the personal data masked), everything else stays administrator's.
router = APIRouter(route_class=ReleasingRoute, prefix="/users", tags=["users"])


def _group_names(session: Session) -> dict[int, list[str]]:
    """Group names per user, in ONE query: the admin list shows them all and a query per
    user would be a needless N+1."""
    rows = session.exec(
        select(UserGroupLink.user_id, Group.name).where(UserGroupLink.group_id == Group.id)
    ).all()
    per_user: dict[int, list[str]] = {}
    for user_id, name in rows:
        per_user.setdefault(user_id, []).append(name)
    return {k: sorted(v) for k, v in per_user.items()}


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
        # no local password ⇒ the account signs in ONLY from the IdP (see services/sso.py)
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
    asker: User = Depends(get_current_user),
):
    per_user = _group_names(session)
    admin_names = _admin_group_names(session)
    observer_names = _observer_group_names(session)
    rows = [
        _to_out(u, per_user.get(u.id, []), admin_names, observer_names)
        for u in session.exec(select(User)).all()
    ]
    # An OBSERVER (not an administrator) sees the list without the personal data: they
    # need to know how many accounts there are and how access is spread, not who the
    # people are nor where they come in from.
    if not is_admin(session, asker):
        for r in rows:
            r.email = mask_email(r.email)
            r.full_name = ""
    return rows


@router.post("", response_model=UserOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_superuser)])
def create_user(
    body: UserCreate,
    request: Request = None,  # type: ignore[assignment]
    session: Session = Depends(get_session),
    current: User = Depends(require_superuser),
):
    # a user with a password is a lasting credential: an API token does not create it
    from app.routes.api_tokens import deny_if_from_token

    deny_if_from_token(request)
    if session.exec(select(User).where(User.email == body.email)).first():
        raise HTTPException(status_code=409, detail="This email is already registered")
    user = User(
        email=body.email,
        full_name=body.full_name,
        hashed_password=hash_password(body.password),
        is_superuser=body.is_superuser,
        is_observer=body.is_observer,
    )
    session.add(user)
    try:
        session.commit()
    except IntegrityError:
        # two creations of the same email at once: both passed the check above, the unique
        # index stops the second one, which gets the same answer as the check
        session.rollback()
        raise HTTPException(status_code=409, detail="This email is already registered") from None
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
    return _to_out(user)  # just created: no group yet


@router.put("/me/locale", status_code=status.HTTP_204_NO_CONTENT)
def set_my_locale(
    body: LocaleChange,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """The language chosen in the interface. The interface sends it at sign-in and
    when it changes; the emails the product sends this user speak it."""
    if user.locale != body.locale:
        user.locale = body.locale
        session.add(user)
        session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/password-rules")
def password_rules(_: User = Depends(get_current_user)) -> dict:
    """The rule of a new password (core/passwords.py), for the interface to check
    a form before sending it: the same numbers and list the gateway applies."""
    from app.core import passwords

    return {
        "min_length": passwords.PASSWORD_MIN_LENGTH,
        "max_bytes": passwords.PASSWORD_MAX_BYTES,
        "known": sorted(passwords.KNOWN_PASSWORDS),
    }


def _password_change_key(user: User) -> str:
    """The throttle key of a password change: the account, from any address. Its
    own key, apart from sign-in's: a stolen session that guesses must not lock the
    owner out of signing in."""
    return f"password-change:{user.id}"


@router.post("/me/password", status_code=status.HTTP_204_NO_CONTENT)
def change_my_password(
    body: PasswordChange,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(get_current_user),
):
    """One's own password, from the settings menu. The current password is asked
    again (whoever finds a session open does not get the account for good); the
    rules are those of user creation. As when an administrator sets it, the
    other sessions and the API tokens close; this one stays."""
    from app.routes.api_tokens import deny_if_from_token
    from app.services import api_tokens

    # a password is a credential that lasts: a token does not set one
    deny_if_from_token(request)
    if user.hashed_password is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This account signs in with the identity provider: its password is managed there",
        )
    if is_observer_only(session, user):
        # the guest of a public demo is one account many people share
        raise HTTPException(status_code=403, detail="An observer account cannot change its password")
    key = _password_change_key(user)
    wait = login_throttle.request_wait(session, None, key)
    if wait > 0:
        audit.record_audit(
            session, actor=user, action=audit.PASSWORD_CHANGE, outcome="failure", request=request,
            target_type="user", target_id=user.id, target_label=user.email, detail={"reason": "throttled"},
        )
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many attempts. Try again in {int(wait) + 1} seconds.",
            headers={"Retry-After": str(int(wait) + 1)},
        )
    if not verify_password(body.current_password, user.hashed_password):
        audit.record_audit(
            session, actor=user, action=audit.PASSWORD_CHANGE, outcome="failure", request=request,
            target_type="user", target_id=user.id, target_label=user.email,
            detail={"reason": "wrong current password"},
        )
        login_throttle.record_failure(session, None, key)
        # 400 and not 401: a 401 would sign the browser out
        raise HTTPException(status_code=400, detail="The current password is not correct")
    login_throttle.record_success(session, None, key)
    user.hashed_password = hash_password(body.new_password)
    session.add(user)
    # in the same transaction as the change: whoever knew the old password, or
    # had made a token with it, is out
    closed_ones = sessions.close_all(session, user.id, except_=sessions.current_sid(request), commit=False)
    withdrawn = api_tokens.revoke_all(session, user.id, commit=False)
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.PASSWORD_CHANGE, request=request,
        target_type="user", target_id=user.id, target_label=user.email,
        detail={"sessions": closed_ones, "tokens": withdrawn},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _get_user(session: Session, user_id: int) -> User:
    user = session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
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
        # a password is a lasting credential: an API token does not set it
        from app.routes.api_tokens import deny_if_from_token

        deny_if_from_token(request)
        if user.is_service:
            raise HTTPException(status_code=422, detail="A service account signs in only with a token: no password")
    was_admin = user.is_superuser
    was_observer = user.is_observer
    if ((body.is_superuser and not was_admin) or (body.is_observer and not was_observer)
            or (body.is_active and not user.is_active)):
        # one more role or a reactivated account (with its password) are ways in that would
        # stay after a leaked token is revoked
        from app.routes.api_tokens import deny_if_from_token
        from app.services import api_tokens as _api_tokens

        deny_if_from_token(request, _api_tokens.NO_ROLES_FROM_TOKEN)
    # a new password or a disabled account close the open sessions and withdraw the API
    # tokens: whoever knew the old password, or had copied a token of it, stays out — even
    # if the account is reactivated later
    close_sessions = body.password is not None or body.is_active is False
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
        # removing one's own flag or disabling oneself: allowed only when one stays admin
        # another way (an administrators' group)
        ensure_still_admin(session, current)
    closed_ones = withdrawn = 0
    if close_sessions:
        # in the same transaction as the change: both, or neither. Whoever changes their own
        # password stays in with the session they do it from
        except_ = sessions.current_sid(request) if request is not None and user.id == current.id else None
        closed_ones = sessions.close_all(session, user.id, except_=except_, commit=False)
        # the API tokens too: whoever had the old password could have created one
        from app.services import api_tokens

        withdrawn = api_tokens.revoke_all(session, user.id, commit=False)
    if user.is_observer and not was_observer:
        from app.services import api_tokens

        session.flush()
        withdrawn += api_tokens.withdraw_if_observer(session, [user.id])
    session.commit()
    if closed_ones or withdrawn:
        reason = ("password" if body.password is not None else "deactivated" if body.is_active is False else "observer")
        audit.record_audit(
            session, actor=current, request=request, action=audit.SESSION_REVOKE,
            target_type="user", target_id=user.id, target_label=user.email,
            detail={"sessions": closed_ones, "tokens": withdrawn, "reason": reason},
        )
    if body.password is not None:
        audit.record_audit(
            session, actor=current, request=request, action=audit.PASSWORD_CHANGE,
            target_type="user", target_id=user.id, target_label=user.email,
            detail={"by_admin": True},
        )
    session.refresh(user)
    if user.is_superuser != was_admin:
        audit.record_audit(
            session, actor=current, request=request,
            action=audit.USER_PROMOTE if user.is_superuser else audit.USER_DEMOTE,
            target_type="user", target_id=user.id, target_label=user.email,
        )
    if user.is_observer != was_observer:
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
    request: Request = None,  # type: ignore[assignment]
):
    """Deletes the user. Their CONTENT stays (the data belongs to the organization, not to
    the person): flows/datasources/connections/runs lose only the reference to the owner.
    The personal permissions, the group memberships and the ownership of uploads not in a
    flow yet disappear with them.

    The AUDIT instead is not touched: it is append-only and must stay readable after the
    person has gone, otherwise deleting an account would delete the evidence of what they
    did. Only the `actor_id` reference is reset, while `actor_label` keeps the email of
    then — exactly the reason that field exists (see models/audit.py).
    """
    if user_id == current.id:
        raise HTTPException(status_code=409, detail="You cannot delete your own account")
    email = _get_user(session, user_id).email
    remove_user(session, user_id)
    audit.record_audit(session, actor=current, action=audit.USER_DELETE, request=request,
                       target_type="user", target_id=user_id, target_label=email)


def remove_user(session: Session, user_id: int) -> None:
    """Removes a user (person or service account) and what is only theirs; see
    `delete_user` for what stays and why."""
    user = _get_user(session, user_id)
    # explicit bulk statements: the order (the referencing first, then the user) is
    # guaranteed — the same lesson as the deletes of flows/projects
    from sqlalchemy import delete as sa_delete, update as sa_update

    from app.models import (
        AiChat, AiChatTurn, AiModel, ApiToken, AuditLog, Banner, Connection, Datasource, DisabledEngine, Flow,
        FlowGroup, FlowVersion, Permission, PrivacyNotice, Project, Run, SavedScript, SavedScriptVersion, SavedView,
        SqlScript, SqlScriptVersion, Notebook, NotebookSession, NotebookVersion,
        Upload, UserSession, Webhook,
    )

    # the conversations with the assistant are personal: they go with the account (the
    # turns first, which reference the chat)
    chat_ids = [c.id for c in session.exec(select(AiChat).where(AiChat.user_id == user_id)).all()]
    if chat_ids:
        session.exec(sa_delete(AiChatTurn).where(AiChatTurn.chat_id.in_(chat_ids)))
        session.exec(sa_delete(AiChat).where(AiChat.id.in_(chat_ids)))
    session.exec(sa_delete(Permission).where(Permission.user_id == user_id))
    session.exec(sa_delete(ContainerRegistryAccess).where(ContainerRegistryAccess.user_id == user_id))
    session.exec(sa_delete(UserGroupLink).where(UserGroupLink.user_id == user_id))
    session.exec(sa_delete(UserSession).where(UserSession.user_id == user_id))
    session.exec(sa_delete(ApiToken).where(ApiToken.user_id == user_id))
    session.exec(sa_delete(Upload).where(Upload.owner_id == user_id))
    # the notebook pods reading with the account's rights: no session holds their Jobs
    # any more, and the reaper deletes them at its next round
    session.exec(sa_delete(NotebookSession).where(NotebookSession.user_id == user_id))
    # the client addresses of the account go with it (services/ip_retention.py);
    # the rows stay, with the email of the time
    from app.services import ip_retention

    ip_retention.forget_user(session, user)
    # `audit_logs.actor_id` is an FK towards users: without resetting it the DELETE
    # violates the constraint and the API answers 500. Not an edge case — `auth.login` is
    # audited, so having signed in once was enough to be no longer deletable. It did not
    # show in the tests because SQLite has foreign keys off.
    session.exec(sa_update(AuditLog).where(AuditLog.actor_id == user_id).values(actor_id=None))
    for model, col in (
        (Project, Project.owner_id),
        (Flow, Flow.owner_id),
        (Datasource, Datasource.owner_id),
        (Connection, Connection.owner_id),
        (Run, Run.launched_by),
        # whoever created a token or a webhook: the token or the webhook stays
        (ApiToken, ApiToken.created_by),
        (Webhook, Webhook.created_by),
        # every other reference to the account, each a foreign key that would refuse
        # the delete: who saved a flow version, a view or a flow group, who changed
        # an administration setting. A schedule left without its author turns itself
        # off the next time it is due (services/scheduler.py).
        (FlowVersion, FlowVersion.created_by),
        (SavedView, SavedView.owner_id),
        (FlowGroup, FlowGroup.owner_id),
        (Flow, Flow.run_scheduled_by),
        (Datasource, Datasource.refresh_scheduled_by),
        (FlowGroup, FlowGroup.run_scheduled_by),
        (SavedScript, SavedScript.owner_id),
        (SavedScript, SavedScript.run_scheduled_by),
        (SavedScriptVersion, SavedScriptVersion.created_by),
        (SqlScript, SqlScript.owner_id),
        (SqlScript, SqlScript.run_scheduled_by),
        (SqlScriptVersion, SqlScriptVersion.created_by),
        # a notebook version left without its author: the app no longer starts (it reads
        # with that author's rights) until someone saves or restores a version
        (Notebook, Notebook.owner_id),
        (Notebook, Notebook.draft_by),
        (NotebookVersion, NotebookVersion.created_by),
        (AiModel, AiModel.updated_by),
        (Banner, Banner.created_by),
        (DisabledEngine, DisabledEngine.disabled_by),
        (PrivacyNotice, PrivacyNotice.updated_by),
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
        raise HTTPException(status_code=404, detail="Group not found")
    exists = session.get(UserGroupLink, (user_id, group_id))
    if exists is None and (group.is_admin or group.is_observer):
        # joining an admin or observer group IS receiving that role
        from app.routes.api_tokens import deny_if_from_token
        from app.services import api_tokens

        deny_if_from_token(request, api_tokens.NO_ROLES_FROM_TOKEN)
    if exists is None:
        session.add(UserGroupLink(user_id=user_id, group_id=group_id))
        if group.is_observer:
            from app.services import api_tokens

            session.flush()
            api_tokens.withdraw_if_observer(session, [user_id])
        session.commit()
        if group.is_admin:
            # joining an admin group IS a promotion: a trace must stay
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
