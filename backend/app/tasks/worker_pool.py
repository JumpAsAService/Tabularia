"""Worker pools sized from Admin → Workers, and the state every worker reports.

The administrator sets how many processes a worker container runs: one value for
the workers of the run queue, one for those of the preview queue
(`workers.run_concurrency`, `workers.preview_concurrency`, settings from the panel:
core/regolazioni.py). Every worker checks the value each `TICK` seconds and resizes
its own prefork pool. It grows at once; it shrinks by idle processes only, so a
busy one finishes its task and goes at a later check. No task is interrupted and
nothing restarts. With no value set, a worker keeps the size it started with
(CELERY__WORKER_CONCURRENCY).

Every worker also writes its state to Valkey at each check (`KEY_PREFIX` + its
name, expiring after `TTL` seconds): processes, busy ones, the memory of its
container. The engine API reads them for the panel (`live_workers`); a worker that
stops writing disappears by itself.
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import time
from typing import Any, Iterable

from celery import bootsteps

from app.core import regolazioni

logger = logging.getLogger(__name__)

TICK = 10.0  # seconds between two checks: the settings are re-read as often
TTL = 45  # seconds a worker's state lasts without being written again
REGISTRY = "tabularia:workers"  # the names of the workers that reported
KEY_PREFIX = "tabularia:worker:"
PREVIEW_QUEUE = "preview"


def kind_of(queues: Iterable[str]) -> str:
    """'preview' for a worker serving only the preview queue, 'run' otherwise (a
    worker on both queues is sized as a run worker)."""
    names = set(queues)
    return "preview" if names and names <= {PREVIEW_QUEUE} else "run"


def target_size(kind: str, started_with: int) -> int:
    from app.core.config import get_settings

    w = get_settings().workers
    value = w.preview_concurrency if kind == "preview" else w.run_concurrency
    return value if value > 0 else started_with


def per_process_kb(processes: int) -> int:
    """The memory ceiling after which a process is recycled, for a pool of this size:
    the container's share per process (config.resolve_max_memory_per_child_kb)."""
    from app.core.config import resolve_max_memory_per_child_kb, settings_dall_ambiente

    # with the panel's values this process started with, as Celery's own configuration
    celery = regolazioni.all_avvio(settings_dall_ambiente()).celery
    return resolve_max_memory_per_child_kb(celery.model_copy(update={"worker_concurrency": max(1, processes)}))


def resize(consumer: Any, target: int) -> int:
    """Brings the consumer's pool toward `target` processes and returns the size it
    reached. The processes started from now on recycle at the memory share of the
    new size; those already running keep theirs until they recycle."""
    pool = consumer.pool
    current = pool.num_processes
    if target == current or not (hasattr(pool, "grow") and hasattr(pool, "shrink")):
        return current  # at its size, or a pool that cannot resize (--pool=solo, threads)
    inner = getattr(pool, "_pool", None)
    if inner is not None and hasattr(inner, "_max_memory_per_child"):
        inner._max_memory_per_child = per_process_kb(target)
    if target > current:
        pool.grow(target - current)
    else:
        try:
            pool.shrink(current - target)
        except ValueError:
            pass  # fewer idle processes than needed: the busy ones go at the next checks
    reached = pool.num_processes
    if reached != current:
        # one task reserved per process (worker_prefetch_multiplier=1): follow the size
        consumer._update_prefetch_count(reached - current)
        logger.info("workers: pool %s → %s processes (target %s)", current, reached, target)
    return reached


def state(consumer: Any, kind: str, started_with: int, started_at: float) -> dict:
    from celery.worker import state as worker_state

    from app.core.config import cgroup_mem_limit_bytes, cgroup_mem_used_bytes, settings_dall_ambiente

    celery = regolazioni.all_avvio(settings_dall_ambiente()).celery
    busy = len(worker_state.active_requests)
    inner = getattr(consumer.pool, "_pool", None)
    per_process = getattr(inner, "_max_memory_per_child", None)
    return {
        "name": consumer.hostname,
        "kind": kind,
        "queues": sorted(consumer.app.amqp.queues.consume_from),
        "processes": consumer.pool.num_processes,
        "target": target_size(kind, started_with),
        "started_with": started_with,
        "busy": busy,
        "waiting": max(0, len(worker_state.reserved_requests) - busy),
        "memory_used": cgroup_mem_used_bytes(),
        "memory_limit": cgroup_mem_limit_bytes(),
        "memory_per_process": per_process * 1024 if per_process else None,
        # the panel warns when a size would leave each process less than the floor
        "memory_share": celery.max_memory_headroom_frac,
        "memory_floor": celery.max_memory_per_child_floor_kb * 1024,
        "autoscale": consumer.controller.autoscaler is not None,
        "pid": os.getpid(),
        "started_at": started_at,
        "seen_at": time.time(),
    }


def report(data: dict) -> None:
    r = regolazioni._client()
    r.set(KEY_PREFIX + data["name"], json.dumps(data), ex=TTL)
    r.sadd(REGISTRY, data["name"])


def forget(name: str) -> None:
    r = regolazioni._client()
    r.delete(KEY_PREFIX + name)
    r.srem(REGISTRY, name)


def draining(name: str, started_at: float, seconds: int) -> None:
    """A worker in its warm shutdown: out of the panel's list, but its state stays for
    as long as its tasks may still run (`seconds`), so `lost` does not call them lost
    while Celery lets them finish. The process forgets it when it exits; killed before,
    the state expires by itself."""
    r = regolazioni._client()
    r.set(KEY_PREFIX + name, json.dumps({"name": name, "started_at": started_at, "draining": True}), ex=seconds)
    r.srem(REGISTRY, name)


# a task the result backend says STARTED, whose worker is no longer there to finish it
LOST = ("The worker running it stopped before the end: killed at its memory limit (OOMKilled), or "
        "its pod restarted or removed (an upgrade, a drained node). Run it again; if it happens again, "
        "give the workers more memory or fewer processes each.")
SUSPECT_PREFIX = "tabularia:task-suspect:"
# a worker missing from the registry for this long after it was first seen missing is
# gone: two of its reports (`TICK`) would have come back meanwhile, after a blip of Valkey
CONFIRM_SECONDS = 2 * TICK + 5


def lost(task_id: str, meta: Any) -> str:
    """Why a task the result backend says STARTED will never end ("" while it may).
    A killed worker writes nothing: its container OOMKilled (the kernel kills every
    process of it on cgroup v2), its pod evicted or its node lost. Its tasks stayed
    STARTED until the gateway's stale timeout, an hour later, said the wrong thing.
    The STARTED meta names the worker and when the task began (task_started_meta):
    - the worker's state is no longer written (`TTL`), and still not some seconds
      later: it is gone;
    - its state is written by a worker started after the task began: the same pod's
      container started again, the task died with the old one.
    Without those facts (an older worker's meta), or without Valkey: "" (the task may run)."""
    if not isinstance(meta, dict) or not isinstance(meta.get("hostname"), str):
        return ""
    since = meta.get("since")
    try:
        r = regolazioni._client()
        raw = r.get(KEY_PREFIX + meta["hostname"])
        if raw is not None:
            r.delete(SUSPECT_PREFIX + task_id)  # back after a blip: the next absence starts afresh
            started_at = json.loads(raw).get("started_at")
            restarted = isinstance(since, (int, float)) and isinstance(started_at, (int, float)) and started_at > since
            return LOST if restarted else ""
        if not isinstance(since, (int, float)):
            return ""  # a worker of an older version: its meta says nothing of when
        now = time.time()
        r.set(SUSPECT_PREFIX + task_id, now, nx=True, ex=3600)
        first = float(r.get(SUSPECT_PREFIX + task_id) or now)
        return LOST if now - first >= CONFIRM_SECONDS else ""
    except Exception:  # noqa: BLE001 — Valkey down or a state unreadable: no verdict
        return ""


def _forget_quietly(name: str) -> None:
    try:
        forget(name)
    except Exception:  # noqa: BLE001 — it expires by itself
        pass


def task_started_meta(request: Any) -> dict:
    """The STARTED meta of a task: Celery's own (pid, worker) and when it began, for `lost`."""
    return {"pid": os.getpid(), "hostname": getattr(request, "hostname", None), "since": time.time()}


def live_workers() -> list[dict]:
    """The workers that reported within `TTL` seconds, by name."""
    r = regolazioni._client()
    out = []
    for name in sorted(r.smembers(REGISTRY) or ()):
        raw = r.get(KEY_PREFIX + name)
        if raw is None:  # stopped writing: gone
            r.srem(REGISTRY, name)
            continue
        try:
            out.append(json.loads(raw))
        except ValueError:
            continue
    return out


class PoolFromPanel(bootsteps.StartStopStep):
    """Consumer step: every `TICK` seconds, resize the pool and report the state."""

    requires = {"celery.worker.consumer.tasks:Tasks"}

    def __init__(self, parent, **kwargs):
        super().__init__(parent, **kwargs)
        self.tref = None
        self.started_with: int | None = None
        self.started_at = time.time()
        self.kind = "run"

    def start(self, c):
        # the consumer restarts its steps when the broker connection drops: the size
        # it started with is the first one, not the one reached since
        if self.started_with is None:
            self.started_with = c.pool.num_processes
        self.kind = kind_of(c.app.amqp.queues.consume_from)
        self.tick(c)
        self.tref = c.timer.call_repeatedly(TICK, self.tick, (c,), priority=10)

    def stop(self, c):
        if self.tref is not None:
            self.tref.cancel()
            self.tref = None

    def shutdown(self, c):
        # the consumer stops before the pool, which then waits for the running tasks:
        # they may last up to the hard time limit
        self.stop(c)
        try:
            draining(c.hostname, self.started_at, int(c.app.conf.task_time_limit or 7200) + TTL)
            atexit.register(_forget_quietly, c.hostname)
        except Exception:  # noqa: BLE001 — it expires by itself
            pass

    def tick(self, c):
        # never let a check stop the worker: the pool stays as it is, the state is
        # written at the next one
        try:
            if c.controller.autoscaler is None:  # with --autoscale Celery sizes the pool itself
                resize(c, target_size(self.kind, self.started_with))
        except Exception:  # noqa: BLE001
            logger.warning("workers: the pool was not resized", exc_info=True)
        try:
            report(state(c, self.kind, self.started_with, self.started_at))
        except Exception as e:  # noqa: BLE001 — Valkey down: the panel shows the worker as gone
            logger.debug("workers: state not written (%s)", e)
