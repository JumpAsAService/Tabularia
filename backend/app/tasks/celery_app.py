from celery import Celery
from app.core.config import get_settings

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
    # eventi task per celery-exporter (metriche: task attivi, durate, esiti)
    worker_send_task_events=True,
    task_send_sent_event=True,
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