"""Who may open a flow, beyond VIEW on its folder: the code and the images some nodes
run belong to what the administration panel grants.

- a Container node: its container registry, and its image, must be granted to the
  person (services/registry_access.py);
- a Script node: its scripting environment must be one they may use
  (services/script_nodes.py `can_use`), or the saved script it runs one they may see.

Whoever lacks one cannot open the flow, nor anything that would show or run it on
their behalf (its versions, a copy, a save, a run, a schedule, a group of flows, the
trials of the editor). The lists still show it, greyed out with the reason
(`locked_reason`). Administrators open everything. What no longer exists (a registry
or an environment deleted) blocks nobody: there is nothing left to protect.
"""
from __future__ import annotations

import json
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session

from app.models import ScriptEnvironment, User
from app.services import registry_access
from app.services.permissions import is_admin


def _script_environments(definition: str) -> list[int]:
    try:
        doc = json.loads(definition)
    except ValueError:
        return []
    if not isinstance(doc, dict) or not isinstance(doc.get("nodes"), list):
        return []
    out = []
    for node in doc["nodes"]:
        if isinstance(node, dict) and node.get("type") == "script":
            eid = (node.get("data") or {}).get("environmentId")
            if isinstance(eid, int) and eid not in out:
                out.append(eid)
    return out


def blocked(session: Session, user: User, definition: Optional[str]) -> Optional[str]:
    """Why this person may not open a flow with this definition, None when they may."""
    if not definition or ('"container"' not in definition and '"script"' not in definition):
        return None  # most flows: nothing the panel grants, nothing to read
    if is_admin(session, user):
        return None
    why = registry_access.blocked(session, user, definition)
    if why:
        return why
    from app.services.script_nodes import can_use

    for eid in _script_environments(definition):
        env = session.get(ScriptEnvironment, eid)
        if env is not None and not can_use(session, user, env):
            return (f"This flow runs scripts in the environment «{env.name}», which you have no access to: "
                    "an administrator can grant it")
    if '"scriptId"' in definition:
        from app.models import SavedScript
        from app.services import saved_scripts
        from app.services.script_nodes import saved_script_id, script_nodes

        for node in script_nodes(definition):
            sid = saved_script_id(node)
            script = session.get(SavedScript, sid) if sid is not None else None
            if script is not None and not saved_scripts.can_see(session, user, script):
                # its name stays unsaid: for this person the script does not exist
                return "This flow runs a saved script you have no access to"
    return None


def ensure_can_open(session: Session, user: User, definition: Optional[str]) -> None:
    why = blocked(session, user, definition)
    if why:
        raise HTTPException(status_code=403, detail=why)
