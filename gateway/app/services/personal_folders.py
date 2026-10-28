"""Every person's own folder: an area of their own, born with the account.

When an account is created (by an administrator, by SSO at the first sign-in, by the
start-up or the first-run page) a folder at the top level is born with it, named after
the part of the email before the @ («leonardtriv@esempio.it» → «leonardtriv»; «leonardtriv
(1)» when the name is taken, as copies are named). The person gets MANAGE on it, which
holds every capability; nobody else gets anything: a top-level folder inherits from no
one, so only the administrators see it, as they see every folder. The person shares it
as any other folder.

What makes it personal is not a column, it is the shape the creation leaves: a folder at
the top level, owned by the person (`owner_id`), on which the person holds a grant of
their own (a user grant, not a group's) to MANAGE. Nothing else gives that shape: only an
administrator creates top-level folders, and an administrator needs no grant. Should one
match more than once (an administrator who granted themselves MANAGE on a folder of
theirs, and later lost the role), the oldest is the personal one.

An account that predates this, or whose folder an administrator took away, gets one at
its next sign-in. Service accounts never get one (they sign in with a token, and work in
the folders they are given), nor do accounts that only observe: a public demo's guest is
one such account shared by many, and a folder of its own would let every visitor write.

What the person cannot do to it (an administrator can, on purpose): delete it, move it
under another folder, take their own grant away. Deactivating the account changes
nothing; deleting it takes the grant away, as for every grant, and the folder stays with
its content as an ordinary folder only the administrators see: the content belongs to the
organization (routes/users.py, `delete_user`)."""
import logging
from typing import Any, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from app.models import Permission, Project, User
from app.models.permission import Capability
from app.services import audit
from app.services.copy_names import copy_name
from app.services.permissions import is_admin, is_observer_only

logger = logging.getLogger(__name__)


def folder_name(email: str) -> str:
    """The part of the email before the @; the whole address when there is none."""
    local = email.split("@", 1)[0].strip()
    return local or email.strip() or "personal"


def _free_root_name(session: Session, name: str) -> str:
    taken = session.exec(
        select(Project.name).where(Project.parent_id.is_(None), Project.name.startswith(name, autoescape=True))
    ).all()
    return name if name not in taken else copy_name(name, taken)


def _personal_query():
    return (
        select(Project.id, Project.owner_id)
        .join(Permission, Permission.project_id == Project.id)
        .where(
            Project.parent_id.is_(None),
            Project.owner_id.is_not(None),
            Permission.user_id == Project.owner_id,
            Permission.capability == Capability.MANAGE.value,
        )
        .order_by(Project.id)
    )


def personal_folders(session: Session) -> dict[int, int]:
    """{folder id: user id} of every personal folder (one per person at most)."""
    first: dict[int, int] = {}
    for project_id, user_id in session.exec(_personal_query()).all():
        first.setdefault(user_id, project_id)
    return {project_id: user_id for user_id, project_id in first.items()}


def personal_folder_id(session: Session, user_id: int) -> Optional[int]:
    """The personal folder of the person, if they have one."""
    row = session.exec(_personal_query().where(Project.owner_id == user_id)).first()
    return row[0] if row else None


def personal_of(session: Session, project_id: int) -> Optional[int]:
    """Whose personal folder this is, if it is one."""
    project = session.get(Project, project_id)
    if project is None or project.parent_id is not None or project.owner_id is None:
        return None
    return project.owner_id if personal_folder_id(session, project.owner_id) == project_id else None


def guard(session: Session, actor: User, project_id: int, what: str) -> None:
    """Refuses `what` («delete», «move») on a personal folder unless an administrator asks."""
    if is_admin(session, actor) or personal_of(session, project_id) is None:
        return
    raise HTTPException(
        status_code=409,
        detail=f"This is a personal folder: only an administrator can {what} it",
    )


def guard_grant(session: Session, actor: User, perm: Permission) -> None:
    """Refuses to take the person's own grant off their personal folder, unless an
    administrator asks: without it the person would be locked out of their own area."""
    if perm.user_id is None or perm.capability != Capability.MANAGE.value or is_admin(session, actor):
        return
    if personal_of(session, perm.project_id) == perm.user_id:
        raise HTTPException(
            status_code=409,
            detail="This is the access of the person to their personal folder: only an administrator can remove it",
        )


def wants_one(session: Session, user: User) -> bool:
    """A person signing in with their own credentials, not one who only observes."""
    return user.id is not None and not user.is_service and user.is_active and not is_observer_only(session, user)


def ensure(
    session: Session,
    user: User,
    *,
    actor: Optional[User] = None,
    actor_label: Optional[str] = None,
    via: Optional[str] = None,
    request: Optional[Any] = None,
) -> Optional[int]:
    """The person's personal folder, created when they have none (and should). It never
    raises: a failure here must not stop a sign-in or the creation of an account, and the
    next sign-in tries again.

    `via`, in the audit, says what created it when it was not the administration panel:
    «sso», «startup», «first-run», «sign-in»."""
    try:
        if not wants_one(session, user):
            return None
        existing = personal_folder_id(session, user.id)
        if existing is not None:
            return existing
        # two sign-ins of the same account at the same moment: the second waits for the
        # first and finds its folder (Postgres; SQLite has one writer anyway)
        session.exec(select(User.id).where(User.id == user.id).with_for_update()).first()
        existing = personal_folder_id(session, user.id)
        if existing is not None:
            session.commit()
            return existing
        project = Project(name=_free_root_name(session, folder_name(user.email)), owner_id=user.id)
        session.add(project)
        session.flush()
        session.add(Permission(project_id=project.id, user_id=user.id, capability=Capability.MANAGE.value))
        session.commit()
        session.refresh(project)
    except Exception:  # noqa: BLE001 — never in the way of the account (see above)
        session.rollback()
        logger.exception("Could not create the personal folder of user %s", user.id)
        return None
    audit.record_audit(
        session, actor=actor, actor_label=actor_label, action=audit.FOLDER_CREATE, request=request,
        target_type="folder", target_id=project.id, target_label=project.name,
        detail={"parent_id": None, "personal": True, "user_id": user.id, **({"via": via} if via else {})},
    )
    return project.id
