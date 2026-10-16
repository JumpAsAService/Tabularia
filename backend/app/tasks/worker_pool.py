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

    celery = settings_dall_ambiente().celery
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

    celery = settings_dall_ambiente().celery
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
        self.stop(c)
        try:
            forget(c.hostname)
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
