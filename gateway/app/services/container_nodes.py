"""The Container nodes of a flow definition: what they hold and the checks made when a
flow is saved or run. docs/container-node.md; the runs are in
services/container_runs.py, who may use which registry and image in
services/registry_access.py.

A Container node in the definition:

    {"type": "container", "data": {
        "registryId": 1, "repository": "team/forecast", "tag": "v1",
        "command": ["python", "-m", "forecast"],   # empty: the image's ENTRYPOINT
        "args": ["--days", "30"],                  # empty: the image's CMD
        "variables": [{"name": "COUNTRY", "value": "IT"}],
        "cpu": "1", "memory": "2Gi",
        "timeoutSeconds": 900, "retries": 0, "retryDelaySeconds": 60}}

As for the Script node, its input and output are optional: a data edge into the node
gives it TABULARIA_INPUT, a data edge out of it asks it for TABULARIA_OUTPUT (both in
the scripts bucket), neither is a step that runs something elsewhere.
"""
from __future__ import annotations

import hashlib
import json
import re
from fastapi import HTTPException

from app.core.config import get_settings
from app.services import container_registry as cr

CONTAINER_NODE = "container"
MAX_ITEMS = 100  # command and arguments, each
MAX_ITEM_CHARS = 4000
MAX_VARIABLES = 100
MAX_VALUE_CHARS = 20000
MAX_RETRIES = 5
DEFAULT_CPU = "1"
DEFAULT_MEMORY = "1Gi"
VARIABLE_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# set by Tabularia in every container
RESERVED_PREFIXES = ("TABULARIA_", "AWS_")
_CPU = re.compile(r"^(\d+(?:\.\d+)?)(m?)$")
_MEMORY = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|K|M|G|T)?$")
_BYTES = {"": 1, "K": 10**3, "M": 10**6, "G": 10**9, "T": 10**12,
          "Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40}


def container_nodes(definition) -> list[dict]:
    if isinstance(definition, dict):
        parsed = definition
    else:
        try:
            parsed = json.loads(definition or "{}")
        except (TypeError, ValueError):
            return []
    if not isinstance(parsed, dict) or not isinstance(parsed.get("nodes"), list):
        return []
    return [n for n in parsed["nodes"] if isinstance(n, dict) and n.get("type") == CONTAINER_NODE]


def label(node: dict) -> str:
    return str((node.get("data") or {}).get("label") or node.get("id") or "Container")


def _refuse(node: dict, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=f"Container «{label(node)}»: {message}")


def strings(value) -> list[str]:
    return [str(x) for x in value] if isinstance(value, list) else []


def variables_of(node: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for v in (node.get("data") or {}).get("variables") or []:
        if isinstance(v, dict) and v.get("name"):
            out[str(v["name"])] = str(v.get("value") or "")
    return out


def fingerprint(node: dict) -> str:
    """What the node runs: the image (as named, not its digest), the command, the
    arguments and the variables."""
    d = node.get("data") or {}
    body = json.dumps({"registry": d.get("registryId"), "repository": d.get("repository"), "tag": d.get("tag"),
                       "command": strings(d.get("command")), "args": strings(d.get("args")),
                       "variables": sorted(variables_of(node).items())}, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()


def cores(cpu: str) -> float:
    m = _CPU.match(cpu)
    return float(m[1]) / 1000 if m[2] else float(m[1])


def memory_bytes(memory: str) -> float:
    m = _MEMORY.match(memory)
    return float(m[1]) * _BYTES[m[2] or ""]


def resources(node: dict) -> tuple[str, str]:
    d = node.get("data") or {}
    return str(d.get("cpu") or DEFAULT_CPU).strip(), str(d.get("memory") or DEFAULT_MEMORY).strip()


def check(node: dict, *, complete: bool) -> None:
    """The node is well formed. `complete` (a run): it names an image to run; a node
    still being written is saved without one."""
    s = get_settings().containers
    d = node.get("data") or {}
    repository, tag = str(d.get("repository") or "").strip(), str(d.get("tag") or "").strip()
    if complete and not (isinstance(d.get("registryId"), int) and repository):
        raise _refuse(node, "choose its registry and its image")
    if repository and not cr.REPOSITORY.match(repository):
        raise _refuse(node, f"«{repository}» is not a repository name")
    if tag and not (cr.TAG.match(tag) or cr.DIGEST.match(tag)):
        raise _refuse(node, f"«{tag}» is not a tag (letters, digits, . _ -) or a digest (sha256:…)")
    for key, what in (("command", "the command"), ("args", "the arguments")):
        value = d.get(key, [])
        if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
            raise _refuse(node, f"{what} are a list of texts")
        if len(value) > MAX_ITEMS or any(len(x) > MAX_ITEM_CHARS for x in value):
            raise _refuse(node, f"{what}: at most {MAX_ITEMS} items of {MAX_ITEM_CHARS} characters")
    variables = d.get("variables", [])
    if not isinstance(variables, list) or len(variables) > MAX_VARIABLES:
        raise _refuse(node, f"at most {MAX_VARIABLES} variables")
    seen: set[str] = set()
    for v in variables:
        name = str((v or {}).get("name") or "") if isinstance(v, dict) else ""
        if not VARIABLE_NAME.match(name) or len(name) > 128:
            raise _refuse(node, f"«{name}» is not a variable name: capitals, digits and _, not starting with a digit")
        if name.startswith(RESERVED_PREFIXES):
            raise _refuse(node, f"{name}: names starting with {' or '.join(RESERVED_PREFIXES)} are set by Tabularia")
        if name in seen:
            raise _refuse(node, f"the variable {name} is there twice")
        seen.add(name)
        if not isinstance(v.get("value", ""), str) or len(v.get("value") or "") > MAX_VALUE_CHARS:
            raise _refuse(node, f"the value of {name} is a text of at most {MAX_VALUE_CHARS} characters")
    cpu, memory = resources(node)
    if not _CPU.match(cpu) or cores(cpu) <= 0:
        raise _refuse(node, f"«{cpu}» is not a CPU quantity: 1, 0.5, 500m")
    if cores(cpu) > cores(s.max_cpu):
        raise _refuse(node, f"at most {s.max_cpu} CPU on this installation")
    if not _MEMORY.match(memory) or memory_bytes(memory) < 2**20:
        raise _refuse(node, f"«{memory}» is not a memory quantity: 512Mi, 2Gi")
    if memory_bytes(memory) > memory_bytes(s.max_memory):
        raise _refuse(node, f"at most {s.max_memory} of memory on this installation")
    timeout = d.get("timeoutSeconds", 900)
    retries = d.get("retries", 0)
    delay = d.get("retryDelaySeconds", 60)
    if not isinstance(timeout, int) or not 10 <= timeout <= s.max_timeout_seconds:
        raise _refuse(node, f"the timeout is a number of seconds between 10 and {s.max_timeout_seconds}")
    if not isinstance(retries, int) or not 0 <= retries <= MAX_RETRIES:
        raise _refuse(node, f"the retries are between 0 and {MAX_RETRIES}")
    if not isinstance(delay, int) or not 0 <= delay <= 3600:
        raise _refuse(node, "the delay between retries is between 0 and 3600 seconds")


def check_definition(definition) -> None:
    """At every save of a flow: every Container node well formed. Who may use its
    registry and image is registry_access.check_definition."""
    for node in container_nodes(definition):
        check(node, complete=False)
