import logging
import os
import sys

from celery import Celery, Task
from celery.signals import before_task_publish, worker_init, worker_process_init, worker_ready
from app.core import panel_settings
from app.core.config import get_settings, resolve_max_memory_per_child_kb, settings_from_env
from app.tasks.worker_pool import PoolFromPanel

logger = logging.getLogger(__name__)

# the worker's configuration comes from the ENVIRONMENT: the one in force at start-up
# and, for the time limits, the fallback for a message that does not carry them (see
# `_time_limits_in_force` below, where the panel's values count)
settings = settings_from_env()
# a worker or beat (not the engine's API, which is how the values arrive) waits for the
# gateway's values when Valkey has none yet, rather than freezing the environment's
if os.path.basename(sys.argv[0]).startswith("celery") and {"worker", "beat"} & set(sys.argv[1:]):
    panel_settings.wait_at_first_start()
# what is read only when a process starts, with the panel's values of that moment
# (memory and recycling of the children, the beat's intervals): core/panel_settings.py
start_values = panel_settings.at_start(settings)
# the Fernet key is required everywhere: the worker that will decrypt the
# credentials must not even start without it
settings.check_required_secrets()

class _TaskThatSparesTheWorker(Task):
    """A safety net for EVERY task (runs, previews, contracts, exports): a Polars panic
    (PanicException, a child of BaseException and not of Exception) escaping the engine
    would make Celery's child process quit half way (WorkerLostError: the run fails with
    no why and the worker restarts). Here it becomes a task error as the others, with its
    message."""

    def __call__(self, *args, **kwargs):
        from polars.exceptions import PanicException

        if self.request.id and not self.request.called_directly and not self.request.is_eager:
            # Celery's STARTED meta, with when the task began: the engine tells a task
            # whose worker died from one still running (worker_pool.lost)
            try:
                from app.tasks.worker_pool import task_started_meta

                self.update_state(state="STARTED", meta=task_started_meta(self.request))
            except Exception:  # noqa: BLE001 — a missed note never stops the task
                logger.debug("STARTED meta not written", exc_info=True)
        try:
            return super().__call__(*args, **kwargs)
        except PanicException as e:
            from app.engine.exceptions import EngineError

            raise EngineError(f"Internal error in the compute engine (Polars): {str(e)[:400]}") from None


celery_app = Celery(
    "data_prep",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.jobs", "app.tasks.contract_jobs"],
    task_cls=_TaskThatSparesTheWorker,
)

celery_app.conf.update(
    task_serializer=settings.celery.task_serializer,
    result_serializer=settings.celery.result_serializer,
    timezone=settings.celery.timezone,
    enable_utc=settings.celery.enable_utc,
    task_track_started=True,
    # The longest a task may last: at the SOFT limit an exception arrives (an orderly
    # close), at the HARD limit the worker kills the process. They were two literals in
    # an otherwise configurable block. The gateway tunes its staleness threshold to it
    # (ENGINE__RUN_STALE_TIMEOUT_SECONDS), which must stay GREATER than the hard limit:
    # see the note on task_acks_late below.
    task_time_limit=settings.celery.task_time_limit,
    task_soft_time_limit=settings.celery.task_soft_time_limit,
    worker_concurrency=settings.celery.worker_concurrency,
    # FAIR distribution over several workers: without it (default 4) every child
    # "books" up to 4 queued tasks, so a busy worker hoards jobs a worker just added
    # could take → the other stays dry. With 1 each child reserves a single task beyond
    # the one running. Crucial for long runs when the pool is scaled.
    worker_prefetch_multiplier=1,
    # NB: task_acks_late stays at its default (False): the ack comes on receipt, not at
    # the end of the task. Not enabled because a redelivery after a crash would run the
    # task again, and the append Outputs would double their rows (tasks NOT
    # idempotent). A lost run is taken up by hand / marked FAILURE by the staleness
    # timeout. Enable acks_late only once the tasks are idempotent.
    # recycling the child process: gives the OS back the memory glibc malloc holds
    # after Polars/Arrow's native allocations (an RSS growing in steps that never comes
    # down). Recycled after N tasks AND above an RSS ceiling.
    worker_max_tasks_per_child=start_values.celery.max_tasks_per_child,
    # derived from the cgroup limit and the concurrency (or set explicitly from the environment)
    worker_max_memory_per_child=resolve_max_memory_per_child_kb(start_values.celery),
    # task events for celery-exporter (metrics: active tasks, durations, outcomes)
    worker_send_task_events=True,
    task_send_sent_event=True,
    # separate queues: the heavy runs on the default queue, the PREVIEWS on a dedicated
    # queue served by a worker of its own (interactive, low latency), so they do not
    # get stuck behind a long run holding the pool's slots.
    task_default_queue="celery",
    task_routes={
        "app.tasks.jobs.preview_task": {"queue": "preview"},
        # a contract's checks asked for by hand are as interactive as a preview
        "app.tasks.contract_jobs.*": {"queue": "preview"},
    },
)

# every worker sizes its pool from Admin → Workers and reports its state there
celery_app.steps["consumer"].add(PoolFromPanel)

# Periodic eviction of the step cache (Celery beat). The interval is set with
# CACHE__SWEEP_INTERVAL_SECONDS.
celery_app.conf.beat_schedule = {
    "evict-step-cache": {
        "task": "app.tasks.jobs.evict_cache_task",
        "schedule": float(start_values.cache.sweep_interval_seconds),
    },
    # samples the size of the storage prefixes for the metrics
    "storage-stats": {
        "task": "app.tasks.jobs.storage_stats_task",
        "schedule": float(start_values.metrics.storage_stats_interval_seconds),
    },
    # drops the viewer's materialized copies (external ClickHouse) no longer used; the
    # task skips itself when the feature is off
    "evict-matviews": {
        "task": "app.tasks.jobs.evict_matview_task",
        # every 5 minutes, fixed: the copies' lifetime is set from the panel while beat
        # reads its configuration once (with the environment's, 30 min, it passed every
        # 15 and a copy of 1 minute lasted 15)
        "schedule": 300.0,
    },
    # export files nobody came to take (each one goes as soon as it is sent)
    "sweep-exports": {
        "task": "app.tasks.jobs.sweep_exports_task",
        "schedule": 900.0,
    },
}


def broker_lists(queue: str) -> list[str]:
    """The Redis lists of a queue, highest priority first. kombu keeps one per
    priority step (`queue`, then `queue<sep>3`, … `queue<sep>9`) and a worker empties
    them in this order: a task sent with priority 9 waits behind every other one."""
    from kombu.transport.redis import PRIORITY_STEPS, Channel

    return [queue if step == 0 else f"{queue}{Channel.sep}{step}" for step in PRIORITY_STEPS]


@before_task_publish.connect
def _time_limits_in_force(headers=None, **_):
    """A task's longest duration travels in its message: the one in force WHEN it is sent,
    which the administrator sets from the panel without restarting the workers (Celery
    uses the message's, and its own configuration only when that is missing). A task
    declaring its own keeps them."""
    if not isinstance(headers, dict):
        return
    if any(x is not None for x in (headers.get("timelimit") or (None, None))):
        return
    c = get_settings().celery
    headers["timelimit"] = (c.task_time_limit, c.task_soft_time_limit)


@worker_init.connect
def _sweep_temporary_files(sender=None, **_):
    """The worker's main process, before any child exists: what earlier lives of this
    worker left in /tmp (dead children's directories, files of the old loose layout)."""
    from app.core import tempdirs

    try:
        tempdirs.sweep_dead()
        tempdirs.sweep_loose()
    except Exception:  # never stops the worker from starting
        logger.warning("temporary files: sweep at start failed", exc_info=True)


@worker_process_init.connect
def _own_temporary_directory(**_):
    """Each child writes its temporary files in a directory of its own; a child killed
    with SIGKILL leaves it behind, and the child Celery starts in its place removes it."""
    from app.core import tempdirs

    try:
        tempdirs.use_process_dir()
    except Exception:
        logger.warning("temporary files: no per-process directory, using the system one", exc_info=True)


@worker_ready.connect
def _ensure_storage_bucket(sender=None, **_):
    """At the worker's start, makes sure the storage's bucket exists.

    The upkeep tasks beat schedules (storage_stats, evict) run even before any upload: on
    a fresh deploy (or after a reset of the storage) the bucket would not exist yet →
    NoSuchBucket in a loop. `create_bucket` is idempotent. A lazy import to avoid cycles
    with app.utils."""
    from app.utils import get_storage_service

    bucket = get_settings().storage.bucket
    try:
        get_storage_service().create_bucket(bucket)
        logger.info("storage: bucket '%s' pronto", bucket)
    except Exception:  # must never stop the worker from starting
        logger.warning("storage: cannot make sure of the bucket '%s' at start-up", bucket, exc_info=True)