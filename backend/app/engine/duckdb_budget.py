"""How much memory and how many threads one DuckDB connection may use.

By default DuckDB assumes it owns 80% of the memory it can see (inside a container:
the container's limit) and one thread per CPU. Every Celery child opens its own
connections, so two to four heavy previews at once each believed they had about 3 GB
in a 4 GB container, and the kernel killed them. A connection now gets its child's
share: the budget the worker already uses to recycle its children (container limit ×
headroom / concurrency, see config.resolve_max_memory_per_child_kb) minus room for
Python, Arrow and Polars, and the CPUs given to the container divided by the same
concurrency. Past the budget DuckDB spills to disk instead of growing.
"""
from __future__ import annotations

import os
from functools import lru_cache

from app.core.config import get_settings, resolve_max_memory_per_child_kb

PROCESS_OVERHEAD_MB = 160  # the interpreter, Arrow buffers, Polars, the task itself
MIN_MEMORY_MB = 256


def _container_cpus() -> int:
    cpus = os.cpu_count() or 1
    try:
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()  # cgroup v2
        if quota != "max":
            cpus = min(cpus, max(1, int(quota) // int(period)))
    except (OSError, ValueError):
        pass
    return cpus


@lru_cache(maxsize=1)
def budget() -> tuple[int, int]:
    """(memory in MiB, threads) for one connection of this process."""
    celery = get_settings().celery
    per_child_mb = resolve_max_memory_per_child_kb(celery) // 1024
    memory_mb = max(MIN_MEMORY_MB, per_child_mb - PROCESS_OVERHEAD_MB)
    threads = max(1, _container_cpus() // max(1, celery.worker_concurrency))
    return memory_mb, threads


def apply(con) -> None:
    """Sets the budget on a connection; call it before any lock_configuration."""
    memory_mb, threads = budget()
    con.execute(f"SET memory_limit = '{memory_mb}MiB'")
    con.execute(f"SET threads = {threads}")
