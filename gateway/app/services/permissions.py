"""Risoluzione dei permessi con ereditarietà lungo l'albero dei progetti.

Regole (primo taglio, allow-only):
- il superuser può tutto;
- un permesso su un progetto vale anche per TUTTI i suoi discendenti (eredità
  verso il basso);
- un soggetto è un utente o uno dei suoi gruppi;
- la gerarchia delle capability è gestita da `grant_satisfies` (VIEW<RUN<EDIT<MANAGE).

Volutamente semplice e leggibile: niente CTE ricorsive. L'albero si legge come
coppie (id, padre) e si percorre in Python; dentro una richiesta le risposte si
riusano (vedi «Per-request reuse»).
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
    return _memo(session, ("admin", user.id), lambda: bool(admin_group_names(session, user)))


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


def observer_group_names(session: Session, user: User) -> list[str]:
    """Gruppi di osservatori a cui l'utente appartiene."""
    righe = session.exec(
        select(Group.name)
        .where(UserGroupLink.group_id == Group.id)
        .where(UserGroupLink.user_id == user.id)
        .where(Group.is_observer == True)  # noqa: E712
    ).all()
    return sorted(righe)


def is_observer(session: Session, user: User) -> bool:
    """Può LEGGERE i pannelli di amministrazione.

    Un amministratore lo è per definizione: chi comanda vede. Come per
    `is_admin`, il risultato non si scrive mai sull'oggetto utente — si ricalcola
    a ogni richiesta, così togliere il ruolo ha effetto subito."""
    if is_admin(session, user):
        return True
    if user.is_observer:
        return True
    return bool(observer_group_names(session, user))


def is_observer_only(session: Session, user: User) -> bool:
    """Osservatore e NIENT'ALTRO: legge i pannelli, non amministra.

    Serve a una domanda sola, e non è una domanda di autorizzazione: di chi NON
    teniamo l'indirizzo IP. Un amministratore è osservatore per definizione, ma
    di lui l'IP si tiene — è chi può cambiare le cose, ed è la traccia che serve
    quando qualcosa va storto. Un osservatore invece guarda e basta: il suo
    indirizzo non risponde a nessuna domanda che ci porremmo davvero, e un dato
    che non serve è meglio non averlo (scelta dell'utente, 2026-09-30)."""
    return is_observer(session, user) and not is_admin(session, user)


def _all_projects(session: Session) -> dict[int, Project]:
    """Every folder as a full row: for who needs names. The permission checks use
    `folder_parents`, seven to ten times cheaper."""
    return {p.id: p for p in session.exec(select(Project)).all()}


def folder_parents(session: Session) -> dict[int, Optional[int]]:
    """The folder tree as {id: parent id}."""
    return _memo(session, ("tree",), lambda: dict(session.exec(select(Project.id, Project.parent_id)).all()))


def ancestor_ids(parents: Mapping[int, Optional[int]], project_id: int) -> list[int]:
    """Catena progetto→root, incluso se stesso. Robusta a cicli accidentali."""
    chain: list[int] = []
    seen: set[int] = set()
    cur = project_id
    while cur is not None and cur in parents and cur not in seen:
        chain.append(cur)
        seen.add(cur)
        cur = parents[cur]
    return chain


def descendant_ids(parents: Mapping[int, Optional[int]], roots: set[int]) -> set[int]:
    """Tutti i discendenti (inclusi i root) dei progetti dati."""
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
        return frozenset(descendant_ids(parents, roots))  # eredità verso il basso

    return _memo(session, ("granted", user.id, needed.value), compute)


def _granted_project_ids(session: Session, user: User, needed: Capability) -> set[int]:
    if is_admin(session, user):
        return set(folder_parents(session))
    return set(_granted(session, user, needed))


def readable_project_ids(session: Session, user: User) -> set[int]:
    """Progetti di cui l'utente può LEGGERE il contenuto (VIEW o superiore).

    Differenza fondamentale con `visible_project_ids`: quella aggiunge gli
    antenati per rendere navigabile l'albero (ne mostra solo il NOME), ma il
    contenuto degli antenati NON è leggibile. Per filtrare contenuti
    (datasource, flussi, run) usare SEMPRE questa.
    """
    return _granted_project_ids(session, user, Capability.VIEW)


def runnable_project_ids(session: Session, user: User) -> set[int]:
    """Progetti dove l'utente può ESEGUIRE (RUN o superiore).

    Leggere e far girare non sono la stessa cosa: chi ha solo VIEW guarda
    anteprime, chi ha RUN mette al lavoro i worker e scrive risultati. Il piano
    dati deve chiederselo con lo stesso rigore del percorso ufficiale dei run."""
    return _granted_project_ids(session, user, Capability.RUN)


def connectable_project_ids(session: Session, user: User) -> set[int]:
    """Progetti dove l'utente può usare/gestire le CONNESSIONI dati (CONNECT,
    ortogonale a VIEW: chi ha solo VIEW non vede le connessioni)."""
    return _granted_project_ids(session, user, Capability.CONNECT)


def visible_project_ids(session: Session, user: User) -> set[int]:
    """Progetti che l'utente può vedere NELL'ALBERO: i leggibili più i loro
    antenati (solo per rendere navigabile il percorso fino alla radice — il
    CONTENUTO degli antenati non è leggibile: vedi `readable_project_ids`)."""
    parents = folder_parents(session)
    visible = readable_project_ids(session, user)
    for pid in list(visible):
        visible.update(ancestor_ids(parents, pid))  # mostra il percorso
    return visible


def visible_projects(session: Session, user: User) -> list[Project]:
    """Le cartelle di `visible_project_ids`, come righe intere: solo quelle."""
    visible = visible_project_ids(session, user)
    if not visible:
        return []
    return list(session.exec(select(Project).where(Project.id.in_(visible)).order_by(Project.id)).all())


def can_upload(session: Session, user: User) -> bool:
    """Può caricare un file chi ha EDIT da qualche parte.

    L'upload non appartiene a una cartella — il file arriva prima di sapere dove
    finirà — quindi non c'è un progetto su cui chiedere il permesso, e per questo
    la rotta finora chiedeva solo di essere autenticati: qualunque account, anche
    di sola lettura, poteva scrivere nel bucket dell'installazione.

    La domanda giusta non è «dove», è «questa persona crea contenuti o soltanto
    li guarda». Chi ha EDIT su almeno una cartella sta costruendo qualcosa e avrà
    dove metterlo; chi ha solo VIEW non ha un posto dove quel file possa andare.
    """
    if is_admin(session, user):
        return True
    return bool(_granted_project_ids(session, user, Capability.EDIT))
