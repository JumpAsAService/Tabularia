"""How many exports a person has running or waiting: a cap, so that one person cannot hold
the whole export queue (EXPORT__MAX_RUNNING_PER_PERSON).

The exports, the SQL scripts' results and the documents (reports, dashboards) are
written on the queue of the flows' runs, each for up to the tasks' time limit. Each one
takes a place when it is queued (`admit`, from the engine's API) and gives it back when
its task ends (`task_ended`, from the worker: Celery's task_postrun and task_revoked). A
place a killed worker never gave back expires with the time the task could have lasted.

One rule for everyone, by owner (the gateway's export_owner: `user:<id>`, a person or a
service account): what the interface asks past the cap is refused with a sentence (429);
what code asks — the REST API with a token, a notebook's tabularia.report(), the
synchronous export — waits for a place (`wait_for_place`, or the gateway's waiting turn)
within the time it would wait for the document anyway. A script sending ten reports
by email gets them, one place at a time.

Valkey holds one sorted set per owner: the tasks and when they took their place. Taking
one is atomic (a script): two requests at the same instant do not both get the last place.
"""
from __future__ import annotations

import logging
import time

import redis

from app.core.config import get_settings

logger = logging.getLogger(__name__)

_PREFIX = "dataprep:export:running:"
# who each export belongs to, written when it starts (app/api/exports.py)
OWNER_PREFIX = "dataprep:export:owner:"
# the tasks that take a place: the export queue's
TASKS = frozenset({
    "app.tasks.jobs.export_task",
    "app.tasks.jobs.db_export_task",
    "app.tasks.report_jobs.render_report_task",
    "app.tasks.report_jobs.render_dashboard_task",
})
# waiting in the queue and running: past this a place is given back by itself
_QUEUE_WAIT_SECONDS = 600

# KEYS[1] the owner's set; ARGV: task id, now, oldest still held, cap, the key's life
_ADMIT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[3])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[4]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[5])
return 1
"""

_client: redis.Redis | None = None


def _r() -> redis.Redis:
    global _client
    if _client is None:
        _client = redis.Redis.from_url(get_settings().redis.url, decode_responses=True)
    return _client


def counted(owner: str | None) -> bool:
    return bool(owner) and owner.startswith("user:")


def _held_for() -> int:
    return get_settings().celery.task_time_limit + _QUEUE_WAIT_SECONDS


def admit(owner: str | None, task_id: str) -> bool:
    """Takes a place for `task_id`: False when the owner has the cap's worth already."""
    if not counted(owner):
        return True
    now = time.time()
    held = _held_for()
    cap = get_settings().export.max_running_per_person
    return bool(_r().eval(_ADMIT, 1, _PREFIX + owner, task_id, now, now - held, cap, held))


def wait_for_place(owner: str | None, task_id: str, seconds: float) -> bool:
    """For code: tries until a place frees up, at most `seconds`. False if none did."""
    deadline = time.monotonic() + seconds
    pause = 0.25
    while not admit(owner, task_id):
        if time.monotonic() + pause > deadline:
            return False
        time.sleep(pause)
        pause = min(pause * 2, 2.0)
    return True


def release(owner: str | None, task_id: str) -> None:
    """Gives the place back. Never raises: the task has ended already."""
    if not counted(owner):
        return
    try:
        _r().zrem(_PREFIX + owner, task_id)
    except Exception as e:  # noqa: BLE001 — Valkey down: the place expires by itself
        logger.warning("export slots: the place of %s was not given back (%s)", task_id, e)


def task_ended(task_name: str | None, task_id: str | None) -> None:
    """From the worker, when a task ends or is revoked: an export's place back to its owner."""
    if task_name not in TASKS or not task_id:
        return
    try:
        owner = _r().get(OWNER_PREFIX + task_id)
    except Exception as e:  # noqa: BLE001 — Valkey down: the place expires by itself
        logger.warning("export slots: the owner of %s cannot be read (%s)", task_id, e)
        return
    release(owner, task_id)


def refusal() -> str:
    cap = get_settings().export.max_running_per_person
    return (f"You already have as many exports and documents being prepared as one person may at once ({cap}): "
            "wait for one to finish and try again (EXPORT__MAX_RUNNING_PER_PERSON).")
