"""Who may see and use a container registry, and which of its images
(Administration → Container registries → Access). docs/container-registries.md.

An administrator grants a registry to a group or to a user, with all the repositories
the registry allows or only some of them (the same patterns: `team/forecast`,
`team/*`). Someone with no grant on a registry:
- does not see it, nor its repositories or tags, in the flow editor;
- cannot add or change a Container node that uses it;
- cannot open a flow that uses it, nor anything that would show or run that flow on
  their behalf (its versions, a copy, the dbt export, a run, a schedule, a group of
  flows).
Administrators see and use them all. A registry that no longer exists blocks nobody:
there is nothing left to protect, and the node says it cannot run.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from app.models import ContainerRegistry, ContainerRegistryAccess, User
from app.services import container_registry as cr
from app.services.permissions import _memo, is_admin, user_group_ids

CONTAINER_NODE = "container"


def patterns(text: str) -> list[str]:
    return [x.strip() for x in (text or "").splitlines() if x.strip()]


def _grants(session: Session, user: User) -> dict[int, Optional[list[str]]]:
    """registry id → the repository patterns this person was granted, None for all of
    them. Read once per request: a list of flows asks for every flow."""
    return _memo(session, ("registry_grants", user.id), lambda: _read_grants(session, user))


def _read_grants(session: Session, user: User) -> dict[int, Optional[list[str]]]:
    groups = user_group_ids(session, user)
    rows = session.exec(select(ContainerRegistryAccess)).all()
    out: dict[int, Optional[list[str]]] = {}
    for row in rows:
        if not (row.user_id == user.id or (row.group_id is not None and row.group_id in groups)):
            continue
        mine = patterns(row.repositories)
        if not mine or out.get(row.registry_id, []) is None:
            out[row.registry_id] = None  # one grant with all the repositories is enough
        else:
            out[row.registry_id] = [*(out.get(row.registry_id) or []), *mine]
    return out


def can_use(session: Session, user: User, registry: ContainerRegistry) -> bool:
    return is_admin(session, user) or registry.id in _grants(session, user)


def repository_allowed(session: Session, user: User, registry: ContainerRegistry, repository: str) -> bool:
    """Allowed by the registry and, for whoever is not an administrator, by their grants."""
    name = cr.repository_name(registry.host, repository)
    if not cr.allowed(patterns(registry.repositories), name):
        return False
    if is_admin(session, user):
        return True
    grants = _grants(session, user)
    if registry.id not in grants:
        return False
    mine = grants[registry.id]
    return mine is None or cr.allowed(mine, name)


def _intersect(a: str, b: str) -> Optional[str]:
    """The repositories two patterns both allow, as one pattern (or None)."""
    if a == "*":
        return b
    if b == "*":
        return a
    if a == b:
        return a
    for wide, narrow in ((a, b), (b, a)):
        if wide.endswith("/*") and (narrow.startswith(wide[:-1]) or narrow == wide[:-2]):
            return narrow if narrow != wide[:-2] else None
    return None


def visible_repositories(session: Session, user: User, registry: ContainerRegistry) -> list[str]:
    """What the editor offers this person: the registry's patterns cut to their grants."""
    allowed = patterns(registry.repositories)
    if is_admin(session, user):
        return allowed
    mine = _grants(session, user).get(registry.id, [])
    if mine is None:
        return allowed
    out: list[str] = []
    for a in allowed:
        for b in mine:
            both = _intersect(a, b)
            if both and both not in out:
                out.append(both)
    return out


def usable_registries(session: Session, user: User) -> list[ContainerRegistry]:
    """The registries this person may choose in a flow: enabled, and granted."""
    rows = session.exec(select(ContainerRegistry).where(ContainerRegistry.enabled == True)  # noqa: E712
                        .order_by(ContainerRegistry.name)).all()
    if is_admin(session, user):
        return list(rows)
    grants = _grants(session, user)
    return [r for r in rows if r.id in grants]


# ── the flows ────────────────────────────────────────────────────────────────
def references(definition: Optional[str]) -> list[tuple[Optional[int], str, str]]:
    """(registry id, repository, node label) of every Container node of a definition."""
    try:
        doc = json.loads(definition or "{}")
    except ValueError:
        return []
    if not isinstance(doc, dict) or not isinstance(doc.get("nodes"), list):
        return []
    out = []
    for node in doc["nodes"]:
        if not isinstance(node, dict) or node.get("type") != CONTAINER_NODE:
            continue
        d = node.get("data") or {}
        rid = d.get("registryId")
        out.append((rid if isinstance(rid, int) else None, str(d.get("repository") or ""),
                    str(d.get("label") or node.get("id") or "")))
    return out


def blocked(session: Session, user: User, definition: Optional[str]) -> Optional[str]:
    """Why this person may not open a flow with this definition, None when they may."""
    if not definition or f'"{CONTAINER_NODE}"' not in definition:
        return None  # most flows: no Container node, nothing to read
    refs = references(definition)
    if not refs or is_admin(session, user):
        return None
    for rid, repository, _label in refs:
        if rid is None:
            continue
        registry = session.get(ContainerRegistry, rid)
        if registry is None:
            continue
        if not can_use(session, user, registry):
            return (f"This flow runs images of the container registry «{registry.name}», which you have no "
                    "access to: an administrator can grant it")
        if repository and not repository_allowed(session, user, registry, repository):
            return (f"This flow runs the image {cr.repository_name(registry.host, repository)} of «{registry.name}», "
                    "which you have no access to: an administrator can grant it")
    return None


def ensure_can_open(session: Session, user: User, definition: Optional[str]) -> None:
    why = blocked(session, user, definition)
    if why:
        raise HTTPException(status_code=403, detail=why)


def check_definition(session: Session, user: User, before: Optional[str], after: Optional[str]) -> None:
    """On a save: the Container nodes added or changed must use a registry, enabled, and
    an image this person may use. The ones left as they were need nothing more (whoever
    could not use them could not have opened the flow). Every Container node must also
    be well formed (container_nodes.check)."""
    from app.core.config import get_settings
    from app.services import container_nodes

    if not get_settings().containers.enabled:
        # an installation without containers (Docker Compose): a flow that came from one
        # with them is kept as it is; its Container nodes do not run, and the run says so
        return
    container_nodes.check_definition(after)
    if is_admin(session, user):
        return
    old = {(rid, repo) for rid, repo, _ in references(before)}
    for rid, repository, label in references(after):
        if (rid, repository) in old or (rid is None and not repository):
            continue  # unchanged, or a node not written yet
        registry = session.get(ContainerRegistry, rid) if rid is not None else None
        if registry is None or not registry.enabled:
            raise HTTPException(status_code=422, detail=f"Container node «{label}»: choose a registry that is in use")
        if not can_use(session, user, registry):
            raise HTTPException(status_code=403, detail=f"Container node «{label}»: you have no access to the "
                                                        f"container registry «{registry.name}»")
        if not repository or not repository_allowed(session, user, registry, repository):
            raise HTTPException(status_code=403, detail=f"Container node «{label}»: the image «{repository}» is not "
                                                        f"among those of «{registry.name}» you may use")
