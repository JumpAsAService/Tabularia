"""Risoluzione dei permessi con ereditarietà lungo l'albero dei progetti.

Regole (primo taglio, allow-only):
- il superuser può tutto;
- un permesso su un progetto vale anche per TUTTI i suoi discendenti (eredità
  verso il basso);
- un soggetto è un utente o uno dei suoi gruppi;
- la gerarchia delle capability è gestita da `grant_satisfies` (VIEW<RUN<EDIT<MANAGE).

Volutamente semplice e leggibile: query in Python, niente CTE ricorsive. Gli
alberi di progetti restano piccoli, quindi va benissimo.
"""
from sqlmodel import Session, select

from app.models import Project, Permission, User, UserGroupLink
from app.models.permission import Capability, grant_satisfies


def user_group_ids(session: Session, user: User) -> set[int]:
    rows = session.exec(select(UserGroupLink.group_id).where(UserGroupLink.user_id == user.id)).all()
    return set(rows)


def _all_projects(session: Session) -> dict[int, Project]:
    return {p.id: p for p in session.exec(select(Project)).all()}


def ancestor_ids(projects: dict[int, Project], project_id: int) -> list[int]:
    """Catena progetto→root, incluso se stesso. Robusta a cicli accidentali."""
    chain: list[int] = []
    seen: set[int] = set()
    cur = project_id
    while cur is not None and cur in projects and cur not in seen:
        chain.append(cur)
        seen.add(cur)
        cur = projects[cur].parent_id
    return chain


def descendant_ids(projects: dict[int, Project], roots: set[int]) -> set[int]:
    """Tutti i discendenti (inclusi i root) dei progetti dati."""
    children: dict[int, list[int]] = {}
    for p in projects.values():
        if p.parent_id is not None:
            children.setdefault(p.parent_id, []).append(p.id)
    out: set[int] = set()
    stack = list(roots)
    while stack:
        pid = stack.pop()
        if pid in out:
            continue
        out.add(pid)
        stack.extend(children.get(pid, []))
    return out


def has_capability(session: Session, user: User, project_id: int, capability: Capability | str) -> bool:
    if user.is_superuser:
        return True
    needed = capability.value if isinstance(capability, Capability) else capability
    projects = _all_projects(session)
    if project_id not in projects:
        return False
    scope = set(ancestor_ids(projects, project_id))  # permesso su antenato → vale qui
    gids = user_group_ids(session, user)
    perms = session.exec(select(Permission).where(Permission.project_id.in_(scope))).all()
    for perm in perms:
        subject_matches = perm.user_id == user.id or (perm.group_id in gids)
        if subject_matches and grant_satisfies(perm.capability, needed):
            return True
    return False


def visible_project_ids(session: Session, user: User) -> set[int]:
    """Progetti che l'utente può vedere: quelli su cui ha (o eredita) VIEW, più i
    loro antenati (per rendere l'albero navigabile fino alla radice)."""
    projects = _all_projects(session)
    if user.is_superuser:
        return set(projects.keys())
    gids = user_group_ids(session, user)
    granted_roots = {
        perm.project_id
        for perm in session.exec(select(Permission)).all()
        if (perm.user_id == user.id or perm.group_id in gids)
        and grant_satisfies(perm.capability, Capability.VIEW.value)
        and perm.project_id in projects
    }
    visible = descendant_ids(projects, granted_roots)  # eredità verso il basso
    for pid in list(visible):
        visible.update(ancestor_ids(projects, pid))  # mostra il percorso
    return visible
