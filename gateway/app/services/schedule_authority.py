"""What a schedule may do, and when it stops being allowed to (review 2026-10-14,
quality-architecture-2).

A schedule runs with the authority of whoever set it. That authority was checked
piece by piece while the run went (RUN on each output's folder, the keys it reads),
but nothing looked at the whole before starting, and someone else's edit could make
the author's schedule read more than the author ever chose:

- `gaps_of_flow` / `gaps_of_group`: everything a scheduled run will need from its
  author, checked when it starts. A gap means the run is not started at all: it is
  recorded as failed, with the reason, and the flow's failure notice goes out.
- `reach_of` and `widens`: what a definition names (datasources, files, flows it
  runs, connections, publish folders). An edit or a version promotion by someone
  other than the schedule's author that ADDS to it switches the schedule off, and the
  audit log says why; so does moving the object to another folder.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session

from app.models import Connection, Datasource, Flow, FlowGroup, User
from app.models.permission import Capability
from app.services import permissions as perm_service
from app.services.objects import collect_storage_keys, ensure_can_read_keys

# the keys of a node that name another object, and what they name
_REFERENCES = {
    "datasourceId": "datasource",
    "flowId": "flow",
    "connectionId": "connection",
    "mirrorConnectionId": "connection",
    "projectId": "folder",
    "environmentId": "environment",
    "registryId": "registry",
}


def _parsed(definition) -> dict:
    if isinstance(definition, dict):
        return definition
    try:
        return json.loads(definition or "{}")
    except (TypeError, ValueError):
        return {}


def reach_of(definition) -> set[tuple[str, object]]:
    """Everything a flow definition names, however deep."""
    parsed = _parsed(definition)
    found: set[tuple[str, object]] = {("key", k) for k in collect_storage_keys(parsed)}

    def walk(node) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                kind = _REFERENCES.get(key)
                if kind and isinstance(value, (int, str)) and str(value).isdigit():
                    found.add((kind, int(value)))
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(parsed)
    # a Script node runs code: new code, or other code, is more than the author chose
    from app.services.script_nodes import code_fingerprint, script_nodes

    found |= {("script", code_fingerprint(n)) for n in script_nodes(parsed)}
    # and a Container node runs an image: another image, command or variables too
    from app.services.container_nodes import container_nodes, fingerprint

    found |= {("container", fingerprint(n)) for n in container_nodes(parsed)}
    return found


def widens(before, after) -> set[tuple[str, object]]:
    """What `after` names that `before` did not."""
    return reach_of(after) - reach_of(before)


def _nodes(parsed: dict):
    for node in parsed.get("nodes") or []:
        if isinstance(node, dict):
            yield node.get("type"), node.get("data") or {}


def _refresh_gaps(session: Session, user: User, ds: Datasource) -> list[str]:
    gaps = []
    if not perm_service.has_capability(session, user, ds.project_id, Capability.RUN):
        gaps.append(f"RUN on the folder of datasource '{ds.name}' (to refresh it)")
    conn = session.get(Connection, ds.connection_id) if ds.connection_id else None
    if conn is not None and not perm_service.has_capability(session, user, conn.project_id, Capability.CONNECT):
        gaps.append(f"CONNECT on connection '{conn.name}' (to refresh '{ds.name}')")
    return gaps


def gaps_of_flow(session: Session, user: User, flow: Flow, seen: Optional[set[int]] = None) -> list[str]:
    """What `user` lacks to run `flow` as its schedule would: RUN on its folder and on
    every flow it runs, read access to every source, RUN and CONNECT for every
    refresh, CONNECT for every connection it writes to, EDIT on every folder it
    publishes into. Empty = nothing missing."""
    seen = (seen or set()) | {flow.id}
    gaps: list[str] = []
    if not perm_service.has_capability(session, user, flow.project_id, Capability.RUN):
        gaps.append(f"RUN on the folder of flow '{flow.name}'")
    parsed = _parsed(flow.definition)
    readable = perm_service.readable_project_ids(session, user)
    refreshed = {int(d["datasourceId"]) for t, d in _nodes(parsed)
                 if t == "refresh" and str(d.get("datasourceId", "")).isdigit()}
    for kind, ref in sorted(reach_of(parsed), key=lambda r: (r[0], str(r[1]))):
        if kind == "datasource":
            ds = session.get(Datasource, ref)
            if ds is None:
                continue  # the run reports a missing datasource itself
            if ds.project_id not in readable:
                gaps.append(f"read access to datasource #{ref}")
            elif ref in refreshed:
                gaps.extend(_refresh_gaps(session, user, ds))
        elif kind == "connection":
            conn = session.get(Connection, ref)
            if conn is not None and not perm_service.has_capability(session, user, conn.project_id, Capability.CONNECT):
                gaps.append(f"CONNECT on connection #{ref}")
        elif kind == "environment":
            from app.models import ScriptEnvironment
            from app.services.script_nodes import can_use

            env = session.get(ScriptEnvironment, ref)
            if env is not None and not can_use(session, user, env):
                gaps.append(f"use of scripting environment '{env.name}'")
        elif kind == "registry":
            from app.models import ContainerRegistry
            from app.services import registry_access

            registry = session.get(ContainerRegistry, ref)
            if registry is not None and not registry_access.can_use(session, user, registry):
                gaps.append(f"use of container registry '{registry.name}'")
        elif kind == "flow" and ref not in seen:
            sub = session.get(Flow, ref)
            if sub is not None:
                gaps.extend(gaps_of_flow(session, user, sub, seen))
    for t, d in _nodes(parsed):
        target = d.get("projectId")
        if t == "output" and (d.get("destType") or "datasource") == "datasource" and str(target or "").isdigit():
            if not perm_service.has_capability(session, user, int(target), Capability.EDIT):
                gaps.append(f"EDIT on folder #{target} (flow '{flow.name}' publishes there)")
        if t == "container" and isinstance(d.get("registryId"), int) and d.get("repository"):
            from app.models import ContainerRegistry
            from app.services import registry_access

            registry = session.get(ContainerRegistry, d["registryId"])
            if (registry is not None and registry_access.can_use(session, user, registry)
                    and not registry_access.repository_allowed(session, user, registry, str(d["repository"]))):
                gaps.append(f"use of the image {d['repository']} of container registry '{registry.name}'")
    try:
        ensure_can_read_keys(session, user, {r for k, r in reach_of(parsed) if k == "key"})
    except HTTPException:
        gaps.append(f"read access to a file flow '{flow.name}' reads")
    return list(dict.fromkeys(gaps))


def gaps_of_group(session: Session, user: User, group: FlowGroup) -> list[str]:
    """As `gaps_of_flow`, for every cell of a flow group, plus RUN on its folder."""
    from app.services.flow_groups import definition_of

    gaps: list[str] = []
    if not perm_service.has_capability(session, user, group.project_id, Capability.RUN):
        gaps.append(f"RUN on the folder of flow group '{group.name}'")
    for row in definition_of(group).steps:
        for cell in row:
            if cell is None:
                continue
            if cell.kind == "flow":
                flow = session.get(Flow, cell.id)
                if flow is not None:
                    gaps.extend(gaps_of_flow(session, user, flow))
            else:
                ds = session.get(Datasource, cell.id)
                if ds is not None:
                    gaps.extend(_refresh_gaps(session, user, ds))
    return list(dict.fromkeys(gaps))


def not_started(user: User, gaps: list[str]) -> str:
    """The failure recorded on a scheduled run that was not started."""
    shown = "; ".join(gaps[:5]) + (f"; and {len(gaps) - 5} more" if len(gaps) > 5 else "")
    return (f"Scheduled run not started: the schedule runs with the rights of {user.email}, "
            f"who no longer has {shown}. Restore those rights, or set the schedule again as someone who has them.")
