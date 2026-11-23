"""The compiled model of a sampling Job (docs/specs/bayes-runner.md, «The binary»): fetched from TABULARIA_BINARY_IN `chunk` bytes at a time,
hashed as it lands, compared with `bayes.model.binary_sha256`, and made executable only when the digest is the recorded one.

The order is the whole point: a file is `0600` while it is written, a file whose sha256 differs is deleted and the step refused
(`binary_hash`) before any `chmod` and any `Popen`, and the dispatcher fetches the binary before it reads one row of the input, so a
poisoned binary is refused without touching the data. The size is not checked here (the engine's `place` copied at most `binary_max_bytes`).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from tab_protocol import Refusal, _fs, is_uri

ENV_BINARY_IN = "TABULARIA_BINARY_IN"
CHUNK = 8 * 1024 * 1024  # as bayes_compile.upload_hashing and bayes_draws.copy_in_chunks
NAME = "model"
_MESSAGE = "the compiled model is not the one Tabularia recorded: it is not run"


def _open_input(source: str):
    if is_uri(source):
        filesystem, where = _fs(source)
        return filesystem.open_input_stream(where)
    return open(source, "rb")


def _private(path: str, flags: int) -> int:
    return os.open(path, flags, 0o600)


def fetch_hashing(source: str, target: Path, expected: str, chunk: int = CHUNK) -> int:
    """Downloads the compiled model `chunk` bytes at a time, hashing as it writes; refuses (`binary_hash`) before any chmod when the digest is
    not the one Tabularia recorded or the object cannot be read whole. The bytes written."""
    digest, total = hashlib.sha256(), 0
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with _open_input(source) as src, open(target, "wb", opener=_private) as out:
            while True:
                piece = src.read(chunk)
                if not piece:
                    break
                digest.update(piece)
                out.write(piece)
                total += len(piece)
    except OSError as e:
        target.unlink(missing_ok=True)
        raise Refusal("binary_hash", _MESSAGE, {"reason": "the compiled model could not be read: " + (e.strerror or type(e).__name__)}) from e
    if digest.hexdigest() != expected:
        target.unlink(missing_ok=True)
        raise Refusal("binary_hash", _MESSAGE, {})
    os.chmod(target, 0o700)
    return total


def fetch_binary(workdir: Path, expected: str) -> Path:
    """The Job's binary under `workdir`, hash-checked and executable. `task_value` when the Job has no address (a fault of the gateway)."""
    source = os.environ.get(ENV_BINARY_IN, "")
    if not source:
        raise Refusal("task_value", f"{ENV_BINARY_IN} is empty: the Job was not made by Tabularia", {"what": ENV_BINARY_IN})
    if not expected:
        raise Refusal("task_value", "bayes.model.binary_sha256 is empty: the Job was not made by Tabularia", {"what": "bayes.model.binary_sha256"})
    target = workdir / NAME
    fetch_hashing(source, target, expected)
    return target
