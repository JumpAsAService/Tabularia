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

from app.models import Group, Project, Permission, User, UserGroupLink
from app.models.permission import Capability, grant_satisfies


def user_group_ids(session: Session, user: User) -> set[int]:
    rows = session.exec(select(UserGroupLink.group_id).where(UserGroupLink.user_id == user.id)).all()
    return set(rows)


def admin_group_names(session: Session, user: User) -> list[str]:
    """Gruppi di amministratori a cui l'utente appartiene (ordinati per nome)."""
    righe = session.exec(
        select(Group.name)
        .where(UserGroupLink.group_id == Group.id)
        .where(UserGroupLink.user_id == user.id)
        .where(Group.is_admin == True)  # noqa: E712 — espressione SQL, non un confronto Python
    ).all()
    return sorted(righe)


def is_admin(session: Session, user: User) -> bool:
    """Admin EFFETTIVO: flag personale oppure appartenenza a un gruppo admin.

    È l'unica domanda che il resto del gateway deve fare: leggere
    `user.is_superuser` da solo ignorerebbe i gruppi. Non si scrive mai il
    risultato sull'oggetto `user` — è una riga ORM e il primo commit della
    richiesta (basta `last_seen`) renderebbe permanente un privilegio che deve
    sparire quando si esce dal gruppo."""
    if user.is_superuser:
        return True
    return bool(admin_group_names(session, user))


def ensure_still_admin(session: Session, current: User) -> None:
    """Da chiamare DOPO le modifiche e PRIMA del commit: se chi sta agendo non
    sarebbe più un admin attivo, annulla tutto con un 409.

    Una guardia sola copre ogni strada (togliersi il flag, disattivarsi, togliere
    il flag al proprio gruppo, uscirne, eliminarlo) e garantisce anche che resti
    sempre almeno un admin: chi agisce lo è, e lo resta."""
    from fastapi import HTTPException

    session.flush()
    if not (current.is_active and is_admin(session, current)):
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="Così perderesti i tuoi privilegi di admin: fallo fare a un altro amministratore",
        )


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
    if is_admin(session, user):
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


def _granted_project_ids(session: Session, user: User, needed: Capability) -> set[int]:
    """Progetti dove l'utente ha `needed` (concesso o ereditato: i discendenti
    dei grant). SENZA gli antenati — solo dove la capability vale davvero."""
    projects = _all_projects(session)
    if is_admin(session, user):
        return set(projects.keys())
    gids = user_group_ids(session, user)
    granted_roots = {
        perm.project_id
        for perm in session.exec(select(Permission)).all()
        if (perm.user_id == user.id or perm.group_id in gids)
        and grant_satisfies(perm.capability, needed.value)
        and perm.project_id in projects
    }
    return descendant_ids(projects, granted_roots)  # eredità verso il basso


def readable_project_ids(session: Session, user: User) -> set[int]:
    """Progetti di cui l'utente può LEGGERE il contenuto (VIEW o superiore).

    Differenza fondamentale con `visible_project_ids`: quella aggiunge gli
    antenati per rendere navigabile l'albero (ne mostra solo il NOME), ma il
    contenuto degli antenati NON è leggibile. Per filtrare contenuti
    (datasource, flussi, run) usare SEMPRE questa.
    """
    return _granted_project_ids(session, user, Capability.VIEW)


def connectable_project_ids(session: Session, user: User) -> set[int]:
    """Progetti dove l'utente può usare/gestire le CONNESSIONI dati (CONNECT,
    ortogonale a VIEW: chi ha solo VIEW non vede le connessioni)."""
    return _granted_project_ids(session, user, Capability.CONNECT)


def visible_project_ids(session: Session, user: User) -> set[int]:
    """Progetti che l'utente può vedere NELL'ALBERO: i leggibili più i loro
    antenati (solo per rendere navigabile il percorso fino alla radice — il
    CONTENUTO degli antenati non è leggibile: vedi `readable_project_ids`)."""
    projects = _all_projects(session)
    visible = readable_project_ids(session, user)
    for pid in list(visible):
        visible.update(ancestor_ids(projects, pid))  # mostra il percorso
    return visible
