"""Saved scripts: who sees them, what they may hold, their versions.
docs/specs/saved-scripts.md; the object in models/saved_script.py, the routes in
routes/scripts.py.

Who sees a script: VIEW on its folder **and** the use of its environment
(script_nodes.can_use). Whoever lacks either does not see it at all, anywhere: the
routes answer 404 as if it did not exist, the lists and the search leave it out.
Administrators see every script. EDIT on the folder changes, moves and deletes it.

A version's `content`:

    {"files": [{"path": "main.py", "content": "…"}], "entrypoint": "main.py",
     "inputs": [{"datasource_id": 12, "alias": "VENDITE_NORD_2024"}],
     "output": {"publish": false, "name": "", "project_id": null, "overwrite": true},
     "timeout_seconds": 900, "retries": 0, "retry_delay_seconds": 60}

An input is its datasource's **id**: a name is unique only in its folder, and a refresh
gives the datasource a new parquet file but never a new id. The code reaches an input
by its alias.
"""
from __future__ import annotations

import json
import re
import unicodedata
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from app.models import (
    Datasource, Project, SavedScript, SavedScriptVersion, ScriptEnvironment, ScriptEnvironmentGroup, ScriptStorage,
    User, UserGroupLink,
)
from app.models.permission import Capability
from app.services import permissions as perm_service
from app.services import script_nodes
from app.services.permissions import _memo, is_admin

ALIAS = re.compile(r"^[A-Z][A-Z0-9_]{0,62}$")
MAX_INPUTS = 20
NAME_MAX = 200


def empty_content() -> dict:
    return {"files": [], "entrypoint": "", "inputs": [],
            "output": {"publish": False, "name": "", "project_id": None, "overwrite": True},
            "timeout_seconds": 900, "retries": 0, "retry_delay_seconds": 60}


# ── who sees what ────────────────────────────────────────────────────────────
def usable_environment_ids(session: Session, user: User) -> Optional[set[int]]:
    """The environments this person may use; None for an administrator (all of them).
    Read once per request: a list asks for every script."""
    if is_admin(session, user):
        return None

    def read() -> set[int]:
        return set(session.exec(
            select(ScriptEnvironmentGroup.environment_id)
            .join(UserGroupLink, UserGroupLink.group_id == ScriptEnvironmentGroup.group_id)
            .where(UserGroupLink.user_id == user.id)
        ).all())

    return _memo(session, ("usable_environments", user.id), read)


def can_see(session: Session, user: User, script: SavedScript) -> bool:
    if is_admin(session, user):
        return True
    envs = usable_environment_ids(session, user)
    return (script.environment_id in envs
            and perm_service.has_capability(session, user, script.project_id, Capability.VIEW))


def visible_query(session: Session, user: User, project_ids: Optional[set[int]] = None):
    """A select of the scripts this person sees (in `project_ids` when given), or None
    when there are none."""
    stmt = select(SavedScript)
    if not is_admin(session, user):
        envs = usable_environment_ids(session, user)
        readable = perm_service.readable_project_ids(session, user)
        if project_ids is not None:
            readable = set(readable) & set(project_ids)
        if not envs or not readable:
            return None
        stmt = stmt.where(SavedScript.environment_id.in_(envs), SavedScript.project_id.in_(readable))  # type: ignore[attr-defined]
    elif project_ids is not None:
        stmt = stmt.where(SavedScript.project_id.in_(project_ids))  # type: ignore[attr-defined]
    return stmt.order_by(SavedScript.name)


def get_visible(session: Session, user: User, script_id: int) -> SavedScript:
    """The script, or 404: one that cannot be seen does not exist for this person."""
    script = session.get(SavedScript, script_id)
    if script is None or not can_see(session, user, script):
        raise HTTPException(status_code=404, detail="Script not found")
    return script


# ── what a script may hold ───────────────────────────────────────────────────
def _refuse(name: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"Script «{name}»: {message}")


def propose_alias(name: str, folder: str, taken: set[str]) -> str:
    """An alias from a datasource's name (`Vendite Nord 2024` → `VENDITE_NORD_2024`),
    with its folder's name added when it is taken (two datasources of the same name in
    two folders), then a number."""
    def clean(text: str) -> str:
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
        text = re.sub(r"[^A-Za-z0-9]+", "_", text).strip("_").upper()
        return text

    base = clean(name) or "INPUT"
    if not base[0].isalpha():
        base = f"DS_{base}"
    base = base[:50]
    candidates = [base]
    if folder and clean(folder):
        candidates.append(f"{base}_{clean(folder)}"[:60])
    for candidate in candidates:
        if candidate not in taken:
            return candidate
    n = 2
    while f"{candidates[-1]}_{n}"[:63] in taken:
        n += 1
    return f"{candidates[-1]}_{n}"[:63]


def _as_node(name: str, content: dict) -> dict:
    """The Script node's checks take a node: the same rules, the same messages."""
    return {"id": name, "data": {"label": name, "files": content["files"], "entrypoint": content["entrypoint"],
                                 "timeoutSeconds": content["timeout_seconds"], "retries": content["retries"],
                                 "retryDelaySeconds": content["retry_delay_seconds"]}}


def check_content(session: Session, user: User, name: str, content: dict,
                  env: ScriptEnvironment, previous: Optional[dict] = None) -> dict:
    """The content as it will be saved, or a 422/403 saying what is wrong. The inputs
    and the publish folder new in this save must be readable / editable by the person
    saving; those already there were checked when they were added (and are checked
    again at every run, with the run's authority)."""
    if not isinstance(content, dict):
        raise _refuse(name, "its content is not an object")
    out = empty_content()
    for key in ("files", "entrypoint", "timeout_seconds", "retries", "retry_delay_seconds"):
        if key in content:
            out[key] = content[key]
    if not isinstance(out["files"], list) or not isinstance(out["entrypoint"], str):
        raise _refuse(name, "its files are a list and its entrypoint a file name")
    out["files"] = [{"path": str(f.get("path") or ""), "content": f.get("content")}
                    for f in out["files"] if isinstance(f, dict)]
    storage = session.get(ScriptStorage, 1)
    try:
        node = _as_node(name, out)
        script_nodes.check_files(node, env.language, (storage.bucket if storage else "").strip(), complete=False)
        script_nodes.check_settings(node, env)
    except HTTPException as e:  # the node's message says «Script «name»: …» already
        raise HTTPException(status_code=422, detail=e.detail) from None

    before = {i["datasource_id"] for i in (previous or {}).get("inputs") or []}
    inputs = content.get("inputs") or []
    if not isinstance(inputs, list) or len(inputs) > MAX_INPUTS:
        raise _refuse(name, f"at most {MAX_INPUTS} inputs")
    seen_alias: set[str] = set()
    seen_ds: set[int] = set()
    for item in inputs:
        if not isinstance(item, dict) or not isinstance(item.get("datasource_id"), int):
            raise _refuse(name, "an input names a datasource by its id")
        alias = str(item.get("alias") or "")
        if not ALIAS.match(alias):
            raise _refuse(name, f"«{alias}» is not an alias: capitals, digits and _, starting with a letter")
        if alias in seen_alias:
            raise _refuse(name, f"two inputs are called {alias}")
        if item["datasource_id"] in seen_ds:
            raise _refuse(name, "the same datasource is an input twice")
        seen_alias.add(alias)
        seen_ds.add(item["datasource_id"])
        ds = session.get(Datasource, item["datasource_id"])
        if ds is None:
            if item["datasource_id"] in before:
                out["inputs"].append({"datasource_id": item["datasource_id"], "alias": alias})
                continue  # gone since: the page shows it missing, a run says so
            raise _refuse(name, f"the datasource of {alias} does not exist")
        if item["datasource_id"] not in before and not perm_service.has_capability(
                session, user, ds.project_id, Capability.VIEW):
            raise HTTPException(status_code=403, detail=f"Script «{name}»: you cannot read the datasource of {alias}")
        out["inputs"].append({"datasource_id": ds.id, "alias": alias})

    output = content.get("output") or {}
    if not isinstance(output, dict):
        raise _refuse(name, "its output settings are an object")
    publish = bool(output.get("publish"))
    out_name = str(output.get("name") or "").strip()
    target = output.get("project_id")
    out["output"] = {"publish": publish, "name": out_name[:NAME_MAX],
                     "project_id": target if isinstance(target, int) else None,
                     "overwrite": output.get("overwrite") is not False}
    if publish:
        if not out_name:
            raise _refuse(name, "the datasource it publishes needs a name")
        if not isinstance(target, int) or session.get(Project, target) is None:
            raise _refuse(name, "choose the folder of the datasource it publishes")
        old = (previous or {}).get("output") or {}
        if (target != old.get("project_id") or not old.get("publish")) and not perm_service.has_capability(
                session, user, target, Capability.EDIT):
            raise HTTPException(status_code=403, detail=f"Script «{name}»: you cannot publish into that folder")
    return out


# ── versions ─────────────────────────────────────────────────────────────────
def version_row(session: Session, script: SavedScript, version: Optional[int] = None) -> Optional[SavedScriptVersion]:
    return session.exec(select(SavedScriptVersion).where(
        SavedScriptVersion.script_id == script.id,
        SavedScriptVersion.version == (version or script.current_version))).first()


def content_of(session: Session, script: SavedScript, version: Optional[int] = None) -> dict:
    row = version_row(session, script, version)
    if row is None:
        return empty_content()
    try:
        return {**empty_content(), **json.loads(row.content)}
    except ValueError:
        return empty_content()


def add_version(session: Session, script: SavedScript, content: dict, note: str, user: User) -> SavedScriptVersion:
    """The next version, which becomes the one that runs."""
    last = session.exec(select(SavedScriptVersion.version).where(SavedScriptVersion.script_id == script.id)
                        .order_by(SavedScriptVersion.version.desc())).first() or 0  # type: ignore[attr-defined]
    row = SavedScriptVersion(script_id=script.id, version=last + 1, content=json.dumps(content), note=note,
                             created_by=user.id)
    session.add(row)
    script.current_version = row.version
    session.add(script)
    return row


def scripts_using_environment(session: Session, environment_id: int) -> list[str]:
    return list(session.exec(select(SavedScript.name).where(SavedScript.environment_id == environment_id)
                             .order_by(SavedScript.name).limit(10)).all())


# ── schedules and references ─────────────────────────────────────────────────
def widens(before: dict, after: dict) -> list[str]:
    """What a change makes the script reach that it did not: other code, other inputs,
    a publish it did not do or in another folder. A schedule set by someone else does
    not spend its author's authority on it (routes/scripts.py switches it off)."""
    reasons = []
    if (before.get("files"), before.get("entrypoint")) != (after.get("files"), after.get("entrypoint")):
        reasons.append("its code changed")
    old = {i.get("datasource_id") for i in before.get("inputs") or []}
    if {i.get("datasource_id") for i in after.get("inputs") or []} - old:
        reasons.append("it reads other datasources")
    b, a = before.get("output") or {}, after.get("output") or {}
    if a.get("publish") and (not b.get("publish") or a.get("project_id") != b.get("project_id")):
        reasons.append("it publishes its output elsewhere")
    return reasons


def using(session: Session, script_id: int) -> tuple[list, list]:
    """(the flows, the flow groups) that run the script: a Script node by reference, a
    cell of a group."""
    from app.models import Flow, FlowGroup
    from app.services.flow_groups import definition_of
    from app.services.script_nodes import saved_script_id, script_nodes

    flows = [f for f in session.exec(select(Flow).where(Flow.definition.contains('"scriptId"'))).all()  # type: ignore[union-attr]
             if any(saved_script_id(n) == script_id for n in script_nodes(f.definition))]
    groups = [g for g in session.exec(select(FlowGroup).where(FlowGroup.definition.contains('"script"'))).all()  # type: ignore[union-attr]
              if any(c is not None and c.kind == "script" and c.id == script_id for row in definition_of(g).steps for c in row)]
    return flows, groups


def references(session: Session, script_id: int) -> dict[str, list[str]]:
    """The names of the flows and flow groups that use the script: it cannot be
    deleted while they do."""
    flows, groups = using(session, script_id)
    return {"flows": sorted(f.name for f in flows), "groups": sorted(g.name for g in groups)}
