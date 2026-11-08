"""A task that may outgrow its worker process's share of the container's memory stops
itself, alone, before the kernel stops the whole container.

The kernel's OOM killer acts at the container's limit, and Kubernetes asks it to kill
every process of the container then (memory.oom.group): one task past the limit restarts
the whole worker, its other tasks and the next previews with it. Measured on kind
(2026-11-08): a column profile on Polars of flow 25's «evasi», a join of 42 million rows
with 12 million at full width, took a preview worker's child past 4 GiB in seven seconds,
and the container restarted.

DuckDB keeps to its share by itself (duckdb_budget: it spills to disk); Polars has no such
limit. Around such a task a thread reads the process's resident memory every 100 ms, and
past `share × MARGIN` the process kills itself with SIGKILL: the pool starts another child
at once, the other children go on, and the task's caller hears «out of memory»
(routes/tasks.said_by_preview_worker, a WorkerLostError with signal 9) in seconds, not
at its time cap.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# the share a process may pass before it stops itself: its own share and a quarter, so
# that it, the other children at their share and the main process stay under the limit
MARGIN = 1.25
POLL_SECONDS = 0.1


def resident_bytes() -> int:
    """This process's resident memory (RSS), from /proc; 0 when it cannot be read."""
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        return 0


def share_bytes() -> int | None:
    """This process's share of the container's memory (the pool's size as Admin → Workers
    sets it), None outside a container with a limit."""
    from app.core.config import cgroup_mem_limit_bytes

    if not cgroup_mem_limit_bytes():
        return None
    from app.engine.duckdb_budget import processes
    from app.tasks.worker_pool import per_process_kb

    return per_process_kb(processes()) * 1024


def _kill_self() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


@contextmanager
def memory_guard(what: str, limit: int | None = None, rss: Callable[[], int] = resident_bytes,
                 kill: Callable[[], None] = _kill_self, poll: float = POLL_SECONDS) -> Iterator[None]:
    """Runs the block with a watch on this process's memory: past `limit` bytes (by default
    this process's share × MARGIN) it is killed. No limit known: no watch."""
    if limit is None:
        try:
            share = share_bytes()
        except Exception:  # noqa: BLE001 — no share known: no watch, as outside a container
            share = None
        limit = int(share * MARGIN) if share else None
    if not limit:
        yield
        return
    done = threading.Event()

    def watch() -> None:
        while not done.wait(poll):
            used = rss()
            if used > limit:
                logger.warning("%s: %d MiB resident, past this process's %d MiB: stopped before the "
                               "container's limit", what, used >> 20, limit >> 20)
                kill()
                return

    t = threading.Thread(target=watch, name="memory-guard", daemon=True)
    t.start()
    try:
        yield
    finally:
        done.set()
