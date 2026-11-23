"""The compile mode of the group «bayes» (docs/specs/bayes-contract.md, «The program and the binary»; docs/specs/bayes-stan-compile.md):
the Job that turns the program Tabularia generated into CmdStan's executable, once, so that a sampling Job never compiles.

  1. /tabularia/code/program.stan is read and its sha256 compared with `bayes.model.program_sha256` (`program_hash` otherwise: the
     gateway checked it too, so a difference is a fault of the Job's ConfigMap, never a user's);
  2. the program is compiled under the image's own conditions (bayes_cmdstan.compile_program: `make -C /opt/cmdstan /tmp/c/model
     PRECOMPILED_HEADERS=false`, an environment of PATH alone: the same call as the self-test's), its output in /tmp/c/model.make.log, read
     for its tail and its warnings, never held whole (`compile_failed` when make fails: the last 40 lines go to the log);
  3. `stanc --info` for the cross-check of stan-codegen and `stanc --version`;
  4. the binary is hashed as it is read and uploaded in chunks to TABULARIA_BINARY_OUT, then `compile.json` to the artifacts' prefix:
     {program_sha256, binary_sha256, binary_bytes, info, stanc, seconds, warnings}. It holds no hash of itself: a file cannot hold its
     own, the termination message carries it;
  5. last, the termination message (/dev/termination-log, 4,096 bytes at most): {program_sha256, binary_sha256, info_sha256}, where
     info_sha256 is the sha256 of compile.json's bytes as uploaded. The gateway trusts only this message (the bucket can be written by any
     user script), so it is written after everything it vouches for, and never when anything failed.

A refusal leaves nothing behind that the gateway could take for a result: no message, no binary uploaded before the program's hash and the
compile passed.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import bayes_cmdstan
from bayes_task import Task
from tab_protocol import ENV_ARTIFACTS, Refusal, _fs, code_dir, is_uri, to_json, write_bytes

ENV_BINARY_OUT = "TABULARIA_BINARY_OUT"
TERMINATION_LOG = Path("/dev/termination-log")
WORKDIR = Path("/tmp/c")
NAME = "model"
CHUNK = 8 * 1024 * 1024  # the chunk of an upload (as garch's paths file and bayes_draws.copy_in_chunks)
MAX_WARNINGS = 50
MAX_WARNING_CHARS = 500
TAIL_LINES = 40
TAIL_BYTES = 64 * 1024
MAX_MESSAGE_BYTES = 4096
STANC = bayes_cmdstan.CMDSTAN / "bin" / "stanc"


def read_program(task: Task, path: Path | None = None) -> bytes:
    """program.stan beside task.json, its sha256 checked against the task's."""
    path = path if path is not None else code_dir() / "program.stan"
    try:
        program = path.read_bytes()
    except OSError as e:
        raise Refusal("program_hash", "the program is not the one this version saved", {"reason": f"program.stan: {e.strerror or e}"}) from e
    if hashlib.sha256(program).hexdigest() != task.model["program_sha256"]:
        raise Refusal("program_hash", "the program is not the one this version saved")
    return program


def tail_lines(path: Path, lines: int = TAIL_LINES, window: int = TAIL_BYTES) -> list[str]:
    """The last `lines` lines of a file, read from its last `window` bytes alone."""
    with open(path, "rb") as f:
        size = f.seek(0, os.SEEK_END)
        f.seek(max(0, size - window))
        data = f.read()
    return data.decode("utf-8", errors="replace").splitlines()[-lines:]


def warnings_of(path: Path) -> list[str]:
    """The warning lines stanc (and make) printed, at most MAX_WARNINGS, read line by line."""
    found: list[str] = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith(("Warning", "Info")):
                found.append(line.rstrip("\n")[:MAX_WARNING_CHARS])
                if len(found) >= MAX_WARNINGS:
                    break
    return found


def stanc_text(*args: str) -> str:
    """One call of stanc, its stdout (stderr apart: its warnings never mix into the JSON), the image's environment."""
    done = subprocess.run([str(STANC), *args], env={"PATH": bayes_cmdstan.PATH}, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=120)
    if done.returncode != 0:
        raise Refusal("compile_failed", "the generated program did not compile; this is a fault of Tabularia, not of the model: the log says where",
                      {"stage": "stanc " + args[0]})
    return done.stdout


def upload_hashing(source: Path, target: str, chunk: int = CHUNK) -> tuple[str, int]:
    """Uploads a file to a path or an s3:// address `chunk` bytes at a time, hashing as it reads; (sha256, bytes)."""
    digest, total = hashlib.sha256(), 0
    if is_uri(target):
        filesystem, where = _fs(target)
        out = filesystem.open_output_stream(where)
    else:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        out = open(target, "wb")
    with open(source, "rb") as src, out:
        while True:
            piece = src.read(chunk)
            if not piece:
                break
            digest.update(piece)
            out.write(piece)
            total += len(piece)
    return digest.hexdigest(), total


def termination_message(program_sha256: str, binary_sha256: str, info_sha256: str) -> str:
    message = to_json({"program_sha256": program_sha256, "binary_sha256": binary_sha256, "info_sha256": info_sha256})
    if len(message.encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise RuntimeError("the termination message is larger than Kubernetes keeps")  # three hex digests: a bug
    return message


def run(task: Task) -> int:
    t0 = time.monotonic()
    binary_out = os.environ.get(ENV_BINARY_OUT, "")
    artifacts = os.environ.get(ENV_ARTIFACTS, "")
    if not binary_out or not artifacts:
        raise Refusal("task_value", f"{ENV_BINARY_OUT} or {ENV_ARTIFACTS} is empty: the Job was not made by Tabularia",
                      {"what": ENV_BINARY_OUT if not binary_out else ENV_ARTIFACTS})
    program_sha = task.model["program_sha256"]
    program = read_program(task)

    WORKDIR.mkdir(parents=True, exist_ok=True)
    exe, outcome = bayes_cmdstan.compile_program(program.decode("utf-8"), NAME, WORKDIR, timeout=float(task.limit("time_budget_seconds")))
    log = WORKDIR / f"{NAME}.make.log"
    if outcome.missing or outcome.status != 0 or not exe.is_file():
        for line in tail_lines(log) if log.exists() else [outcome.missing]:
            print(line, flush=True)
        raise Refusal("compile_failed", "the generated program did not compile; this is a fault of Tabularia, not of the model: the log says where",
                      {"status": outcome.status})
    print(f"compiled in {outcome.seconds:.1f} s, peak {outcome.peak_mib:.0f} MiB", flush=True)

    info = json.loads(stanc_text("--info", str(WORKDIR / f"{NAME}.stan")))
    version = stanc_text("--version").strip()
    binary_sha, size = upload_hashing(exe, binary_out)
    compile_json = to_json({
        "program_sha256": program_sha, "binary_sha256": binary_sha, "binary_bytes": size, "info": info, "stanc": version,
        "seconds": round(time.monotonic() - t0, 3), "warnings": warnings_of(log),
    }).encode("utf-8")
    write_bytes(artifacts.rstrip("/") + "/compile.json", compile_json)
    message = termination_message(program_sha, binary_sha, hashlib.sha256(compile_json).hexdigest())
    TERMINATION_LOG.write_text(message, encoding="utf-8")
    print(f"binary {binary_sha[:12]}… {size} bytes uploaded in {time.monotonic() - t0:.1f} s", flush=True)
    return 0
