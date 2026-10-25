"""Resolving the permissions with inheritance down the project tree.

Rules (first cut, allow-only):
- the superuser can do everything;
- a permission on a project holds for ALL its descendants too (inheritance
  downwards);
- a subject is a user or one of their groups;
- the capability hierarchy is handled by `grant_satisfies` (VIEW<RUN<EDIT<MANAGE).

Simple and readable on purpose: no recursive CTEs. The tree is read as (id, parent)
pairs and walked in Python; within a request the answers are reused (see
«Per-request reuse»).
"""
from collections.abc import Callable, Mapping
from typing import Optional, TypeVar

from sqlalchemy import event, or_
from sqlmodel import Session, select

from app.models import Group, Project, Permission, User, UserGroupLink
from app.models.permission import Capability, grant_satisfies

T = TypeVar("T")

# ── Per-request reuse ──────────────────────────────────────────────────────────
# One request asks the same questions several times: is this user an admin, which
# groups are they in, which folders can they read. Each answer used to reload the
# whole folder tree and every grant of every user (0.25 s for a preview at 2,000
# folders, review of 2026-10-14). The answers are kept in the session for as long
# as one transaction of ONE HTTP request: `get_session` marks the request's session,
# and any write flushed, any commit, rollback or close forgets them, so a check
# after a grant, a move or a long wait sees the database again. Sessions opened by
# background loops are not marked and keep asking every time.
REQUEST_SESSION = "request_session"
_CACHE = "permissions_cache"


def _memo(session: Session, key: tuple, compute: Callable[[], T]) -> T:
    if not session.info.get(REQUEST_SESSION):
        return compute()
    cache = session.info.setdefault(_CACHE, {})
    if key not in cache:
        cache[key] = compute()
    return cache[key]


@event.listens_for(Session, "after_flush")
@event.listens_for(Session, "after_transaction_end")
def _forget(session, *_args) -> None:
    session.info.pop(_CACHE, None)


def user_group_ids(session: Session, user: User) -> set[int]:
    def load() -> frozenset[int]:
        return frozenset(session.exec(select(UserGroupLink.group_id).where(UserGroupLink.user_id == user.id)).all())

    return set(_memo(session, ("groups", user.id), load))


def admin_group_names(session: Session, user: User) -> list[str]:
    """The administrators' groups the user belongs to (sorted by name)."""
    rows = session.exec(
        select(Group.name)
        .where(UserGroupLink.group_id == Group.id)
        .where(UserGroupLink.user_id == user.id)
        .where(Group.is_admin == True)  # noqa: E712 — an SQL expression, not a Python comparison
    ).all()
    return sorted(rows)


def is_admin(session: Session, user: User) -> bool:
    """EFFECTIVE admin: personal flag or membership of an admin group.

    It is the only question the rest of the gateway must ask: reading `user.is_superuser`
    alone would ignore the groups. The result is never written on the `user` object — it
    is an ORM row and the request's first commit (`last_seen` is enough) would make
    permanent a privilege that must disappear when one leaves the group."""
    if user.is_superuser:
        return True
    return _memo(session, ("admin", user.id), lambda: bool(admin_group_names(session, user)))


def ensure_still_admin(session: Session, current: User) -> None:
    """To call AFTER the changes and BEFORE the commit: when whoever is acting would no
    longer be an active admin, it cancels everything with a 409.

    One guard covers every way (removing one's own flag, disabling oneself, removing the
    flag from one's own group, leaving it, deleting it) and also ensures there is always
    at least one admin left: whoever acts is one, and stays one."""
    from fastapi import HTTPException

    session.flush()
    if not (current.is_active and is_admin(session, current)):
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="This would remove your own admin privileges: ask another administrator to do it",
        )


def observer_group_names(session: Session, user: User) -> list[str]:
    """The observers' groups the user belongs to."""
    rows = session.exec(
        select(Group.name)
        .where(UserGroupLink.group_id == Group.id)
        .where(UserGroupLink.user_id == user.id)
        .where(Group.is_observer == True)  # noqa: E712
    ).all()
    return sorted(rows)


def is_observer(session: Session, user: User) -> bool:
    """Can READ the administration panels.

    An administrator is one by definition: whoever is in charge sees. As for `is_admin`,
    the result is never written on the user object — it is recomputed at every request,
    so removing the role takes effect at once."""
    if is_admin(session, user):
        return True
    if user.is_observer:
        return True
    return bool(observer_group_names(session, user))


def is_observer_only(session: Session, user: User) -> bool:
    """Observer and NOTHING ELSE: reads the panels, does not administer.

    It serves one question only, and it is not an authorization question: whose IP
    address we do NOT keep. An administrator is an observer by definition, but their IP
    is kept — they are who can change things, and it is the trace needed when something
    goes wrong. An observer instead only looks: their address answers no question we
    would really ask, and data that is not needed is better not had (the user's choice,
    2026-09-30)."""
    return is_observer(session, user) and not is_admin(session, user)


def _all_projects(session: Session) -> dict[int, Project]:
    """Every folder as a full row: for who needs names. The permission checks use
    `folder_parents`, seven to ten times cheaper."""
    return {p.id: p for p in session.exec(select(Project)).all()}


def folder_parents(session: Session) -> dict[int, Optional[int]]:
    """The folder tree as {id: parent id}."""
    return _memo(session, ("tree",), lambda: dict(session.exec(select(Project.id, Project.parent_id)).all()))


def ancestor_ids(parents: Mapping[int, Optional[int]], project_id: int) -> list[int]:
    """The project→root chain, itself included. Robust to accidental cycles."""
    chain: list[int] = []
    seen: set[int] = set()
    cur = project_id
    while cur is not None and cur in parents and cur not in seen:
        chain.append(cur)
        seen.add(cur)
        cur = parents[cur]
    return chain


def descendant_ids(parents: Mapping[int, Optional[int]], roots: set[int]) -> set[int]:
    """All the descendants (the roots included) of the given projects."""
    children: dict[int, list[int]] = {}
    for pid, parent in parents.items():
        if parent is not None:
            children.setdefault(parent, []).append(pid)
    out: set[int] = set()
    stack = list(roots)
    while stack:
        pid = stack.pop()
        if pid in out:
            continue
        out.add(pid)
        stack.extend(children.get(pid, []))
    return out


def _subject_grants(session: Session, user: User) -> list[tuple[int, str]]:
    """(folder, capability) of the grants to the user and to their groups: filtered
    in SQL, not every grant of every user filtered in Python."""
    def load() -> list[tuple[int, str]]:
        gids = user_group_ids(session, user)
        subject = Permission.user_id == user.id
        if gids:
            subject = or_(subject, Permission.group_id.in_(gids))
        return list(session.exec(select(Permission.project_id, Permission.capability).where(subject)).all())

    return _memo(session, ("grants", user.id), load)


def has_capability(session: Session, user: User, project_id: int, capability: Capability | str) -> bool:
    if is_admin(session, user):
        return True
    needed = Capability(capability.value if isinstance(capability, Capability) else capability)
    return project_id in _granted(session, user, needed)


def _granted(session: Session, user: User, needed: Capability) -> frozenset[int]:
    """Folders where the user has `needed`, granted or inherited (the descendants of
    the grants). WITHOUT the ancestors: only where the capability really holds.
    The user is not an admin (the callers ask that first)."""
    def compute() -> frozenset[int]:
        parents = folder_parents(session)
        roots = {pid for pid, cap in _subject_grants(session, user)
                 if pid in parents and grant_satisfies(cap, needed.value)}
        return frozenset(descendant_ids(parents, roots))  # inheritance downwards

    return _memo(session, ("granted", user.id, needed.value), compute)


def _granted_project_ids(session: Session, user: User, needed: Capability) -> set[int]:
    if is_admin(session, user):
        return set(folder_parents(session))
    return set(_granted(session, user, needed))


def readable_project_ids(session: Session, user: User) -> set[int]:
    """The projects whose content the user can READ (VIEW or above).

    The fundamental difference from `visible_project_ids`: that one adds the ancestors to
    make the tree navigable (it shows only their NAME), but the ancestors' content is NOT
    readable. To filter content (datasources, flows, runs) ALWAYS use this one.
    """
    return _granted_project_ids(session, user, Capability.VIEW)


def runnable_project_ids(session: Session, user: User) -> set[int]:
    """The projects where the user can RUN (RUN or above).

    Reading and running are not the same thing: whoever has only VIEW looks at previews,
    whoever has RUN puts the workers to work and writes results. The data plane must ask
    it with the same rigour as the official run path."""
    return _granted_project_ids(session, user, Capability.RUN)


def connectable_project_ids(session: Session, user: User) -> set[int]:
    """The projects where the user can use/manage the data CONNECTIONS (CONNECT, orthogonal
    to VIEW: whoever has only VIEW does not see the connections)."""
    return _granted_project_ids(session, user, Capability.CONNECT)


def visible_project_ids(session: Session, user: User) -> set[int]:
    """The projects the user can see IN THE TREE: the readable ones plus their ancestors
    (only to make the path to the root navigable — the ancestors' CONTENT is not
    readable: see `readable_project_ids`)."""
    parents = folder_parents(session)
    visible = readable_project_ids(session, user)
    for pid in list(visible):
        visible.update(ancestor_ids(parents, pid))  # shows the path
    return visible


def visible_projects(session: Session, user: User) -> list[Project]:
    """The folders of `visible_project_ids`, as whole rows: only those."""
    visible = visible_project_ids(session, user)
    if not visible:
        return []
    return list(session.exec(select(Project).where(Project.id.in_(visible)).order_by(Project.id)).all())


def can_upload(session: Session, user: User) -> bool:
    """Whoever has EDIT somewhere may upload a file.

    The upload does not belong to a folder — the file arrives before knowing where it
    will end up — so there is no project to ask the permission on, and that is why the
    route so far asked only to be authenticated: any account, read-only ones too, could
    write into the installation's bucket.

    The right question is not «where», it is «does this person create content or only
    look at it». Whoever has EDIT on at least one folder is building something and will
    have somewhere to put it; whoever has only VIEW has no place that file could go.
    """
    if is_admin(session, user):
        return True
    return bool(_granted_project_ids(session, user, Capability.EDIT))
