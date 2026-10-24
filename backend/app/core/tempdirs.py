"""Temporary files that outlive a killed process.

Every engine removes its temporary files in a `finally`, but a process killed with
SIGKILL (the kernel's OOM killer, a pod over its memory limit) never runs it: the
local copy of a source parquet (hundreds of MB) stayed in /tmp for good. So each
process writes its temporary files in a directory of its own, `<tmp>/tabularia/<pid>`,
and every process that starts removes the directories of processes that no longer
exist. Celery replaces a dead child at once (and recycles children every N tasks),
so the files of a killed child go within seconds, also on workers that run for weeks.
Nothing is removed by age: a long run in progress always keeps its files.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

# the system temporary directory, read before any process points tempfile elsewhere
_BASE = Path(tempfile.gettempdir())

# the prefixes the backend gives its temporary files (mkstemp/mkdtemp), before they had
# a directory per process: what a worker removes from <tmp> when it starts
_LOOSE_PREFIXES = ("dataprep_", "duckdb_", "duckdb-", "chdb_", "chext_", "bigquery_", "db_dest_", "db_ingest_",
                   "dbt_seed_", "ingest_out_", "ingest_raw_", "s3_dest_", "upload_")


def _root(base: Path | None) -> Path:
    return Path(base or _BASE) / "tabularia"


def _alive(pid: int) -> bool:
    """True while the process exists and is not a zombie (killed, not reaped yet)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        # /proc/<pid>/stat: "pid (comm) state …"; the command may hold spaces or brackets
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def sweep_dead(base: Path | None = None) -> int:
    """Removes the directories of processes that no longer exist; returns how many."""
    root = _root(base)
    removed, freed = 0, 0
    try:
        entries = list(root.iterdir())
    except FileNotFoundError:
        return 0
    for d in entries:
        if not d.name.isdigit() or int(d.name) == os.getpid() or not d.is_dir() or _alive(int(d.name)):
            continue
        freed += sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
        shutil.rmtree(d, ignore_errors=True)
        removed += 1
    if removed:
        logger.info("temporary files: removed %d directories of dead processes (%.1f MB)", removed, freed / 1e6)
    return removed


def use_process_dir(base: Path | None = None) -> Path:
    """Points this process's temporary files (tempfile, and TMPDIR for what it starts)
    at a directory of its own, then removes those of dead processes. A directory already
    there under this pid is a dead process's of the same pid: a container started again
    in its Pod (OOMKilled, its liveness probe) keeps /tmp, an emptyDir, and gives the
    same pids again (the engine's API is pid 1 every time). It is emptied first."""
    d = _root(base) / str(os.getpid())
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        logger.info("temporary files: %s was left by an earlier process with this pid, emptied", d)
    d.mkdir(parents=True, exist_ok=True)
    tempfile.tempdir = str(d)
    os.environ["TMPDIR"] = str(d)
    sweep_dead(base)
    return d


def sweep_loose(base: Path | None = None) -> int:
    """Removes the backend's temporary files left loose in <tmp> by the layout before the
    per-process directories. Only where no process of ours is running yet (a worker's
    main process at start); returns how many entries it removed."""
    base = Path(base or _BASE)
    removed = 0
    for p in base.iterdir():
        if not p.name.startswith(_LOOSE_PREFIXES):
            continue
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
        removed += 1
    if removed:
        logger.info("temporary files: removed %d left from before the per-process directories", removed)
    return removed
