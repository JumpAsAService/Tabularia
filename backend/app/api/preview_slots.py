"""Preview slots: in every slot only the LAST request counts.

Whoever draws a flow clicks one node after another; every click starts one or more
previews, and those of the nodes left behind stayed queued in front of the only one
that matters. The client says which slot the preview belongs to ("this editor, the
preview panel"): a new preview on the same slot knocks down the previous one,
EXPLICITLY. The HTTP disconnection is not relied upon to propagate: between browser and
worker there are ingress, gateway and a synchronous route, and none guarantees it.

All fail-open: when Valkey does not answer, the preview runs as if there were no slot.
A courtesy registry must not be able to break the preview.
"""
import logging

import redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)
_PREFIX = "dataprep:preview:slot:"
# compare-and-delete: the slot is freed only while it is still OURS
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
_client: redis.Redis | None = None


def _r() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(get_settings().redis.url, decode_responses=True)
    return _client


def claim(slot: str, task_id: str, ttl_seconds: int) -> str | None:
    """Takes the slot for `task_id` and returns who held it (or None). Atomic (SET … GET):
    two simultaneous requests do not lose each other."""
    try:
        return _r().set(_PREFIX + slot, task_id, ex=ttl_seconds, get=True)
    except Exception as e:
        logger.warning("preview slot not available (%s): going on without it", e)
        return None


def owner(slot: str) -> str | None:
    try:
        return _r().get(_PREFIX + slot)
    except Exception:
        return None


def release(slot: str, task_id: str) -> None:
    try:
        _r().eval(_RELEASE, 1, _PREFIX + slot, task_id)
    except Exception:
        pass
