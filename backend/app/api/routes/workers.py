"""The workers as the panel shows them (Admin → Workers): the state each worker
writes to Valkey (tasks/worker_pool.py) and the tasks waiting in each queue. Only
the gateway calls it, like every route of this API."""
import logging

from fastapi import APIRouter

from app.tasks import worker_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workers", tags=["workers"])

QUEUES = ("celery", "preview")


def _queued() -> dict[str, int | None]:
    """Tasks in the broker not yet taken by a worker, per queue (every priority list)."""
    import redis

    from app.tasks.celery_app import broker_lists, celery_app

    out: dict[str, int | None] = {q: None for q in QUEUES}
    try:
        rc = redis.from_url(celery_app.conf.broker_url, socket_timeout=2, socket_connect_timeout=2)
        for q in QUEUES:
            out[q] = sum(int(rc.llen(lst)) for lst in broker_lists(q))
    except Exception:  # noqa: BLE001 — the panel shows the queues as unknown
        logger.warning("workers: queue lengths not readable", exc_info=True)
    return out


@router.get("")
def workers():
    try:
        live = worker_pool.live_workers()
    except Exception:  # noqa: BLE001 — Valkey down
        logger.warning("workers: states not readable", exc_info=True)
        live = None
    return {"workers": live, "queued": _queued(), "tick_seconds": worker_pool.TICK, "ttl_seconds": worker_pool.TTL}
