"""How much memory and how many threads one DuckDB connection may use.

By default DuckDB assumes it owns 80% of the memory it can see (inside a container:
the container's limit) and one thread per CPU. Every Celery child opens its own
connections, so two to four heavy previews at once each believed they had about 3 GB
in a 4 GB container, and the kernel killed them. A connection now gets its child's
share: the budget the worker already uses to recycle its children (container limit ×
headroom / concurrency, see config.resolve_max_memory_per_child_kb) minus room for
Python, Arrow and Polars, and the CPUs given to the container divided by the same
concurrency. Past the budget DuckDB spills to disk instead of growing.

The concurrency is the pool's as Admin → Workers sizes it (worker_pool.target_size for
the worker's queue), read again at every connection: with the environment's, a pool
grown from the panel from 2 to 6 processes gave each a third of the container
three times over, and the kernel killed the whole container.

Threads when the sources are read in place over the network. Up to DuckDB 1.x a thread
reading a remote Parquet file waits for its own request, one at a time, so a scan of an
object store is bound by the requests in flight and not by the CPUs: a billion rows
grouped by one column took 34.5 s with 4 threads, 17.7 with 8 and 9.5 with 16 (measured
from a worker of four CPUs' share; DuckDB's guide says the same of synchronous reads:
two to five times the cores). Such a connection gets its CPUs times
REMOTE_THREADS_PER_CPU, as far as the memory allows MIN_MEMORY_PER_THREAD_MB each (the
minimum DuckDB documents). The heavy shapes were run again inside the same memory with
the larger number (a join of 42 million rows with 12 million, 12 million groups, a whole
sort written out, the first row of each group): none gave up, all were as fast or
faster, and the process's peak memory grew by a tenth at most. From DuckDB 2 the reads
are asynchronous and have a pool of their own: there the CPUs' share is left as it is.
"""
from __future__ import annotations

import os
import re
import sys
from functools import lru_cache

from app.core.config import get_settings

PROCESS_OVERHEAD_MB = 160  # the interpreter, Arrow buffers, Polars, the task itself
MIN_MEMORY_MB = 256
REMOTE_THREADS_PER_CPU = 4
MIN_MEMORY_PER_THREAD_MB = 125
# the share of the pod's /tmp DuckDB may spill into: the sources a run downloads and the
# files it writes are there too, and past the emptyDir's sizeLimit the kubelet evicts
# the pod at once (every task of it lost)
SPILL_SHARE = 0.5
_UNITS = {"": 1, "k": 1000, "M": 1000**2, "G": 1000**3, "T": 1000**4,
          "Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}


def _container_cpus() -> int:
    cpus = os.cpu_count() or 1
    try:
        quota, period = open("/sys/fs/cgroup/cpu.max").read().split()  # cgroup v2
        if quota != "max":
            cpus = min(cpus, max(1, int(quota) // int(period)))
        return cpus
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        quota = int(open("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read())
        period = int(open("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read())
        if quota > 0 and period > 0:
            cpus = min(cpus, max(1, quota // period))
    except (OSError, ValueError):
        pass
    return cpus


def _queues(argv: list[str]) -> list[str]:
    """The queues of the worker this process belongs to (`-Q celery`, `--queues=preview`);
    a child of the pool has its worker's command line."""
    for i, arg in enumerate(argv):
        if arg in ("-Q", "--queues") and i + 1 < len(argv):
            return argv[i + 1].split(",")
        if arg.startswith("--queues="):
            return arg.split("=", 1)[1].split(",")
        if arg.startswith("-Q") and len(arg) > 2:
            return arg[2:].split(",")
    return []


def processes() -> int:
    """The processes of this worker's pool: the panel's size for its queue when it set
    one, else the environment's concurrency (what the pool started with)."""
    from app.tasks.worker_pool import kind_of, target_size

    started_with = max(1, get_settings().celery.worker_concurrency)
    return max(1, target_size(kind_of(_queues(sys.argv)), started_with))


@lru_cache(maxsize=8)
def _budget_for(processes: int) -> tuple[int, int]:
    from app.tasks.worker_pool import per_process_kb

    memory_mb = max(MIN_MEMORY_MB, per_process_kb(processes) // 1024 - PROCESS_OVERHEAD_MB)
    return memory_mb, max(1, _container_cpus() // processes)


def budget() -> tuple[int, int]:
    """(memory in MiB, threads) for one connection of this process."""
    return _budget_for(processes())


def quantity_bytes(text: str) -> int | None:
    """A Kubernetes quantity (`50Gi`, `500M`) in bytes, None when it is not one."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([kMGT]i?)?\s*", text or "")
    if not m:
        return None
    return int(float(m.group(1)) * _UNITS[m.group(2) or ""])


def spill_mib() -> int | None:
    """The most a connection may spill (MiB): its share of the pod's /tmp (TMP_SIZE_LIMIT,
    the chart's sizeLimit), None without one (DuckDB's own cap)."""
    limit = quantity_bytes(os.environ.get("TMP_SIZE_LIMIT", ""))
    if not limit:
        return None
    return max(64, int(limit * SPILL_SHARE / processes()) // 2**20)


def synchronous_remote_reads() -> bool:
    """Whether this DuckDB reads a remote Parquet file one request a thread (before 2.0)."""
    import duckdb

    try:
        return int(str(duckdb.__version__).split(".")[0]) < 2
    except ValueError:
        return False


def remote_threads(memory_mb: int, cpus: int) -> int:
    """The threads of a connection that reads its sources in place over the network: the
    CPUs' share times REMOTE_THREADS_PER_CPU, no more than the memory allows, never fewer
    than the CPUs' share."""
    by_memory = memory_mb // MIN_MEMORY_PER_THREAD_MB
    return max(cpus, min(cpus * REMOTE_THREADS_PER_CPU, by_memory))


def apply(con, remote: bool = False) -> None:
    """Sets the budget on a connection; call it before any lock_configuration. `remote`:
    the connection reads its sources in place on the object storage."""
    memory_mb, threads = budget()
    if remote and synchronous_remote_reads():
        threads = remote_threads(memory_mb, threads)
    con.execute(f"SET memory_limit = '{memory_mb}MiB'")
    con.execute(f"SET threads = {threads}")
    spill = spill_mib()
    if spill:
        con.execute(f"SET max_temp_directory_size = '{spill}MiB'")
