import logging

from celery import Celery
from celery.signals import worker_ready
from app.core.config import get_settings, resolve_max_memory_per_child_kb

logger = logging.getLogger(__name__)

settings = get_settings()

celery_app = Celery(
    "data_prep",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks.jobs"],
)

celery_app.conf.update(
    task_serializer=settings.celery.task_serializer,
    result_serializer=settings.celery.result_serializer,
    timezone=settings.celery.timezone,
    enable_utc=settings.celery.enable_utc,
    task_track_started=True,
    task_time_limit=3600,
    task_soft_time_limit=3300,
    worker_concurrency=settings.celery.worker_concurrency,
    # riciclo del processo figlio: rilascia all'OS la memoria che glibc malloc
    # trattiene dopo le allocazioni native di Polars/Arrow (RSS a scalini che non
    # rientra). Ricicla dopo N task E oltre un tetto di RSS.
    worker_max_tasks_per_child=settings.celery.max_tasks_per_child,
    # derivato dal limite cgroup/concurrency (o override esplicito via env)
    worker_max_memory_per_child=resolve_max_memory_per_child_kb(settings.celery),
    # eventi task per celery-exporter (metriche: task attivi, durate, esiti)
    worker_send_task_events=True,
    task_send_sent_event=True,
    # code separate: i run pesanti sulla coda di default, le ANTEPRIME su una
    # coda dedicata servita da un worker suo (interattivo, bassa latenza) così
    # non restano bloccate dietro un run lungo che occupa gli slot del pool.
    task_default_queue="celery",
    task_routes={
        "app.tasks.jobs.preview_task": {"queue": "preview"},
    },
)

# Eviction periodica della step cache (Celery beat). Intervallo configurabile
# via CACHE__SWEEP_INTERVAL_SECONDS.
celery_app.conf.beat_schedule = {
    "evict-step-cache": {
        "task": "app.tasks.jobs.evict_cache_task",
        "schedule": float(settings.cache.sweep_interval_seconds),
    },
    # campiona la dimensione dei prefissi di storage per le metriche
    "storage-stats": {
        "task": "app.tasks.jobs.storage_stats_task",
        "schedule": float(settings.metrics.storage_stats_interval_seconds),
    },
}


@worker_ready.connect
def _ensure_storage_bucket(sender=None, **_):
    """All'avvio del worker garantisce che il bucket dello storage esista.

    I task di manutenzione schedulati da beat (storage_stats, evict) girano anche
    prima di qualsiasi upload: su un deploy fresco (o dopo un reset dello storage)
    il bucket non esisterebbe ancora → NoSuchBucket in loop. `create_bucket` è
    idempotente. Import lazy per evitare cicli con app.utils."""
    from app.utils import get_storage_service

    bucket = get_settings().storage.bucket
    try:
        get_storage_service().create_bucket(bucket)
        logger.info("storage: bucket '%s' pronto", bucket)
    except Exception:  # non deve mai impedire l'avvio del worker
        logger.warning("storage: impossibile garantire il bucket '%s' all'avvio", bucket, exc_info=True)