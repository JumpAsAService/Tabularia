"""Secrets kept in a directory the services share (SECURITY__KEYS_DIR), created at the
first start: what lets the try-it Compose stack start with no configuration at all.

The gateway, the engine API, the workers and beat all mount the same volume and may
start in the same second: a secret is written to a file of its own and published
with os.link, which fails if the name already exists, so exactly one value wins and
every process reads that one. The file is readable by its owner only.

Only for one machine (Compose): on Kubernetes the pods do not share a volume, and the
secrets come from the chart (SECURITY__FERNET_KEY, AUTH__ADMIN_PASSWORD).
The same module is in gateway/app/core/keys.py.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable


def read_or_create(directory: str, name: str, make: Callable[[], str]) -> str:
    """The secret `name` in `directory`; created with `make()` if it is not there yet."""
    path = Path(directory) / name
    found = _read(path)
    if found:
        return found
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{name}.{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(make() + "\n")
        try:
            os.link(tmp, path)  # fails if another process published first: its value wins
        except FileExistsError:
            pass
        finally:
            tmp.unlink(missing_ok=True)
    except PermissionError as e:
        raise RuntimeError(f"cannot write {path} ({e}): SECURITY__KEYS_DIR must be writable by the services") from e
    found = _read(path)
    if not found:
        raise RuntimeError(f"{path} is empty: remove it and start again")
    return found


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except FileNotFoundError:
        return ""
