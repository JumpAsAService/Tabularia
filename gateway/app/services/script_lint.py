"""The Python code of a Script node checked while it is typed, by Ruff: names used and
never defined or imported, syntax errors, imports and variables left unused. The
editor underlines them before anything runs.

One file at a time, on its own. Ruff does not know which packages the environment
holds: `missing_imports` adds the modules imported that are neither installed (the
list the build's check wrote), nor Python's own, nor a file of the node. The binary runs
with `--isolated` (no configuration file of anyone's applies) and reads the code from
its standard input; nothing is written to disk.

A check costs a few milliseconds. The editor sends one when typing pauses, and each
person gets at most `RATE` per second (the rest are skipped: the next pause checks
again); the same content in the same version is answered from a small cache.
"""
from __future__ import annotations

import ast
import hashlib
import json
import sys
import logging
import subprocess
import threading
import time
from collections import OrderedDict, deque
from typing import Optional

logger = logging.getLogger(__name__)

# Ruff's defaults: pyflakes (F) and the pycodestyle rules that are errors, not style
RULES = "E4,E7,E9,F"
# what stops the script; the rest (an unused import or variable) is a warning
ERRORS = {"F821", "F822", "F823", "F811", "F402", "F404", "F406", "F407", "F501", "F502", "F503",
          "F504", "F505", "F506", "F507", "F508", "F509", "F521", "F522", "F523", "F524", "F525",
          "F601", "F602", "F621", "F622", "F631", "F632", "F633", "F701", "F702", "F704", "F706", "F707"}
TIMEOUT_SECONDS = 5
RATE = 2  # checks per person per second
CACHE_SIZE = 128

_cache: OrderedDict[str, list[dict]] = OrderedDict()
_recent: dict[int, deque] = {}
_lock = threading.Lock()


def _ruff() -> str:
    from ruff.__main__ import find_ruff_bin

    return find_ruff_bin()


def allowed(user_id: int) -> bool:
    """False when this person already had RATE checks in the last second."""
    now = time.monotonic()
    with _lock:
        seen = _recent.setdefault(user_id, deque())
        while seen and now - seen[0] > 1:
            seen.popleft()
        if len(seen) >= RATE:
            return False
        seen.append(now)
        return True


def _diagnostic(d: dict) -> dict:
    code = d.get("code") or "invalid-syntax"
    start, end = d.get("location") or {}, d.get("end_location") or {}
    error = code == "invalid-syntax" or code.startswith("E9") or code in ERRORS
    return {
        "line": start.get("row") or 1, "column": start.get("column") or 1,
        "end_line": end.get("row") or start.get("row") or 1, "end_column": end.get("column") or start.get("column") or 1,
        "code": code, "message": d.get("message") or "", "severity": "error" if error else "warning",
    }


def lint_python(path: str, content: str, language_version: Optional[str]) -> list[dict]:
    """Ruff's findings on one file, in the order of the code; [] when Ruff cannot run
    (the editor then shows nothing, as before this check existed)."""
    target = f"py{(language_version or '').replace('.', '')}" if language_version else None
    key = hashlib.sha256(f"{target}\0{path}\0{content}".encode()).hexdigest()
    with _lock:
        if key in _cache:
            _cache.move_to_end(key)
            return _cache[key]
    command = [_ruff(), "check", "--isolated", "--no-cache", "--quiet", "--output-format", "json",
               "--select", RULES, "--stdin-filename", path]
    if target:
        command += ["--target-version", target]
    try:
        done = subprocess.run([*command, "-"], input=content.encode(), capture_output=True,
                              timeout=TIMEOUT_SECONDS, check=False)
        if done.returncode not in (0, 1):  # 1: something found; 2: Ruff itself failed
            raise ValueError(done.stderr.decode(errors="replace")[:300])
        found = json.loads(done.stdout or b"[]")
    except (OSError, subprocess.TimeoutExpired, ValueError) as e:
        logger.warning("scripting: Ruff did not check %s (%s)", path, e)
        return []
    out = sorted((_diagnostic(d) for d in found), key=lambda d: (d["line"], d["column"]))
    with _lock:
        _cache[key] = out
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return out


# Python's own modules of the versions the environments offer: the gateway's Python
# knows its own, these came later
_STDLIB_LATER = {"annotationlib", "compression"}


def node_modules(paths: list[str]) -> set[str]:
    """What the node's files can be imported as: main.py → main, lib/scoring.py → lib."""
    out = set()
    for p in paths:
        first = p.split("/", 1)[0]
        out.add(first[:-3] if first.endswith(".py") else first)
    return out


def missing_imports(content: str, installed: set[str], local: set[str], environment: str) -> list[dict]:
    """The `import x` / `from x import y` whose top module the script would not find."""
    try:
        tree = ast.parse(content)
    except (SyntaxError, ValueError):
        return []  # Ruff says where
    known = installed | local | set(sys.stdlib_module_names) | _STDLIB_LATER
    lines = content.splitlines()
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [(a.name, a.lineno, a.col_offset) for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            line = lines[node.lineno - 1] if node.lineno <= len(lines) else ""
            at = line.find(node.module, node.col_offset)
            names = [(node.module, node.lineno, at if at >= 0 else node.col_offset)]
        else:
            continue
        for name, line_no, col in names:
            top = name.split(".", 1)[0]
            if top in known:
                continue
            out.append({"line": line_no, "column": col + 1, "end_line": line_no, "end_column": col + 1 + len(name),
                        "code": "not-installed", "severity": "error",
                        "message": f"`{top}` is not installed in the environment «{environment}»"})
    return sorted(out, key=lambda d: (d["line"], d["column"]))
