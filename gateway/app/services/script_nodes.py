"""The Script nodes of a flow definition: what they hold, who may write them, and the
checks made when a flow is saved. See docs/specs/script-node.md; the runs are in
services/script_runs.py.

A Script node in the definition:

    {"type": "script", "data": {
        "environmentId": 3,
        "files": [{"path": "main.py", "content": "…"}, {"path": "lib/scoring.py", "content": "…"}],
        "entrypoint": "main.py",
        "timeoutSeconds": 900, "retries": 0, "retryDelaySeconds": 60}}

Its input and output are optional: a data edge into the node gives it an input
(the step before it, written to the scripts bucket), a data edge out of it means the
script writes an output the next nodes read. A node with neither is a step of the
flow that runs something outside Tabularia.

Who may write one: an administrator, or someone in one of the environment's groups
(with EDIT on the flow's folder, which saving needs anyway). The people who write
scripts are trusted (they run code on the cluster); one check is still made on the
code itself: it may not name the scripts bucket, where the other runs' files are. A
script reads its input and writes its output through TABULARIA_INPUT and
TABULARIA_OUTPUT.
"""
from __future__ import annotations

import hashlib
import json
import re
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from app.models import ScriptEnvironment, ScriptEnvironmentGroup, ScriptStorage, User, UserGroupLink
from app.services.permissions import is_admin

MAX_FILES = 50
MAX_FILE_BYTES = 256 * 1024
# a ConfigMap holds at most 1 MiB; the code travels in one
MAX_CODE_BYTES = 768 * 1024
MAX_RETRIES = 5
_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}(/[A-Za-z0-9_][A-Za-z0-9_.-]{0,99}){0,7}$")
ENTRYPOINT_SUFFIX = {"python": (".py",), "r": (".R", ".r")}


def script_nodes(definition) -> list[dict]:
    parsed = definition if isinstance(definition, dict) else _parse(definition)
    return [n for n in parsed.get("nodes") or [] if isinstance(n, dict) and n.get("type") == "script"]


def _parse(definition) -> dict:
    try:
        parsed = json.loads(definition or "{}")
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def files_of(node: dict) -> list[dict]:
    files = (node.get("data") or {}).get("files") or []
    return [f for f in files if isinstance(f, dict)]


def saved_script_id(node: dict) -> Optional[int]:
    """The saved script a Script node runs by reference (docs/specs/saved-scripts.md,
    «The Script node, by reference»), None for a node with its own code."""
    sid = (node.get("data") or {}).get("scriptId")
    return sid if isinstance(sid, int) and not isinstance(sid, bool) else None


def code_fingerprint(node: dict) -> str:
    """What the node runs: the environment, the files and the entrypoint; or the
    saved script it names."""
    d = node.get("data") or {}
    if saved_script_id(node) is not None:
        return hashlib.sha256(json.dumps({"script": saved_script_id(node)}).encode()).hexdigest()
    body = json.dumps({"environment": d.get("environmentId"), "entrypoint": d.get("entrypoint"),
                       "files": sorted((str(f.get("path")), str(f.get("content"))) for f in files_of(node))},
                      sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def fingerprint(session: Session, node: dict) -> str:
    """As `code_fingerprint`, with the version a saved script is at: a run of an older
    version is stale once the script changes."""
    sid = saved_script_id(node)
    if sid is None:
        return code_fingerprint(node)
    from app.models import SavedScript

    script = session.get(SavedScript, sid)
    version = script.current_version if script else 0
    return hashlib.sha256(f"{code_fingerprint(node)}:v{version}".encode()).hexdigest()


def resolved(session: Session, node: dict):
    """(the node as it runs, its saved script or None): a node by reference takes the
    script's current files, entrypoint, environment, timeout and retries."""
    sid = saved_script_id(node)
    if sid is None:
        return node, None
    from app.models import SavedScript
    from app.services import saved_scripts

    script = session.get(SavedScript, sid)
    if script is None:
        return node, None
    content = saved_scripts.content_of(session, script)
    data = {**(node.get("data") or {}), "environmentId": script.environment_id, "files": content["files"],
            "entrypoint": content["entrypoint"], "timeoutSeconds": content["timeout_seconds"],
            "retries": content["retries"], "retryDelaySeconds": content["retry_delay_seconds"]}
    return {**node, "data": data}, script


def label(node: dict) -> str:
    return str((node.get("data") or {}).get("label") or node.get("id") or "Script")


def _refuse(node: dict, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"Script «{label(node)}»: {message}")


def can_use(session: Session, user: User, env: ScriptEnvironment) -> bool:
    if is_admin(session, user):
        return True
    return session.exec(
        select(ScriptEnvironmentGroup.group_id)
        .join(UserGroupLink, UserGroupLink.group_id == ScriptEnvironmentGroup.group_id)
        .where(ScriptEnvironmentGroup.environment_id == env.id, UserGroupLink.user_id == user.id)
    ).first() is not None


def check_files(node: dict, language: Optional[str], bucket: str = "", complete: bool = True) -> None:
    """The node's files: names that make a tree under one folder, sizes a ConfigMap
    takes, code that does not name the scripts bucket and, when `complete` (a run),
    an entrypoint among them. A node still being written is saved without one."""
    d = node.get("data") or {}
    files = files_of(node)
    if not files and not complete:
        return
    if not files:
        raise _refuse(node, "it has no code")
    if len(files) > MAX_FILES:
        raise _refuse(node, f"at most {MAX_FILES} files")
    seen: set[str] = set()
    total = 0
    for f in files:
        path, content = str(f.get("path") or ""), f.get("content")
        if not isinstance(content, str):
            raise _refuse(node, f"the file «{path}» has no text")
        if not _PATH.match(path) or ".." in path.split("/"):
            raise _refuse(node, f"«{path}» is not a file name: letters, digits, . _ - and / between folders")
        if path.lower() in seen:
            raise _refuse(node, f"two files are called «{path}»")
        seen.add(path.lower())
        size = len(content.encode())
        if size > MAX_FILE_BYTES:
            raise _refuse(node, f"«{path}» is larger than {MAX_FILE_BYTES // 1024} KB")
        total += size
        if bucket:
            check_paths(node, path, content, bucket)
    if total > MAX_CODE_BYTES:
        raise _refuse(node, f"the code is larger than {MAX_CODE_BYTES // 1024} KB")
    entry = str(d.get("entrypoint") or "")
    if not complete and not entry:
        return
    if entry not in {str(f.get("path")) for f in files}:
        raise _refuse(node, f"the file to run («{entry or '—'}») is not among its files")
    if language and not entry.endswith(ENTRYPOINT_SUFFIX[language]):
        raise _refuse(node, f"the file to run is a {'Python' if language == 'python' else 'R'} file "
                            f"({' or '.join(ENTRYPOINT_SUFFIX[language])})")


def check_paths(node: dict, path: str, content: str, bucket: str) -> None:
    """The path check: the scripts bucket holds the inputs and outputs of every run in
    progress, so the code may reach it only through TABULARIA_INPUT and
    TABULARIA_OUTPUT. Naming the bucket in the code is refused, with the line."""
    pattern = re.compile(rf"(s3a?://|gs://|//){re.escape(bucket)}\b|\b{re.escape(bucket)}/env-\d", re.I)
    for n, line in enumerate(content.splitlines(), start=1):
        if pattern.search(line):
            raise _refuse(node, f"{path}, line {n} names the scripts bucket: read the input from "
                                "TABULARIA_INPUT and write the output to TABULARIA_OUTPUT")


def check_settings(node: dict, env: Optional[ScriptEnvironment]) -> None:
    d = node.get("data") or {}
    timeout = d.get("timeoutSeconds", 900)
    retries = d.get("retries", 0)
    delay = d.get("retryDelaySeconds", 60)
    if not isinstance(timeout, int) or timeout < 10:
        raise _refuse(node, "the timeout is a number of seconds, at least 10")
    if env is not None and timeout > env.max_timeout_seconds:
        raise _refuse(node, f"the timeout is longer than its environment allows ({env.max_timeout_seconds} s)")
    if not isinstance(retries, int) or not 0 <= retries <= MAX_RETRIES:
        raise _refuse(node, f"the retries are between 0 and {MAX_RETRIES}")
    if not isinstance(delay, int) or not 0 <= delay <= 3600:
        raise _refuse(node, "the delay between retries is between 0 and 3600 seconds")


def check_definition(session: Session, user: User, before, after) -> None:
    """At every save of a flow (create, edit, duplicate, version promoted). Every
    Script node must be well formed; one that is new or whose code or environment
    changed must be written by someone who may use its environment."""
    nodes = script_nodes(after)
    if not nodes:
        return
    from app.services.script_builds import disabled_reason

    if disabled_reason() == "kubernetes":
        # an installation without scripting (Docker Compose): a flow that came from one
        # with it is kept as it is; its Script nodes do not run, and the run says so
        return
    old = {n.get("id"): code_fingerprint(n) for n in script_nodes(before or "{}")}
    storage = session.get(ScriptStorage, 1)
    bucket = (storage.bucket if storage else "").strip()
    for node in nodes:
        sid = saved_script_id(node)
        if sid is not None:
            # the script was checked when it was saved; here only whether it may be named
            from app.models import SavedScript
            from app.services import saved_scripts

            if old.get(node.get("id")) == code_fingerprint(node):
                continue  # unchanged: whoever could not see it could not have opened the flow
            script = session.get(SavedScript, sid)
            if script is None or not saved_scripts.can_see(session, user, script):
                raise _refuse(node, "choose a saved script you may see")
            continue
        env_id = (node.get("data") or {}).get("environmentId")
        env = session.get(ScriptEnvironment, env_id) if isinstance(env_id, int) else None
        if env_id is not None and env is None:
            raise _refuse(node, "its scripting environment no longer exists: choose another")
        changed = old.get(node.get("id")) != code_fingerprint(node)
        if changed and (env is not None or files_of(node)) and not is_admin(session, user):
            # code, or an environment, written by someone who may not use it
            if env is None or not can_use(session, user, env):
                raise HTTPException(status_code=403, detail=f"Script «{label(node)}»: you cannot write scripts "
                                                            f"for {'this environment' if env is None else f'the environment «{env.name}»'}")
        check_files(node, env.language if env else None, bucket, complete=False)
        check_settings(node, env)
