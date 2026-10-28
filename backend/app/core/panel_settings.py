"""Settings from the panel: the engine's parameters an administrator changes from
Administration → Settings, without touching the environment and without restarts.

Those the engine reads when it uses them (`ITEMS` without `rollout`): the row groups'
size, the imports' read timeout, the export's cap, the tasks' duration, the step cache,
ClickHouse, BigQuery, the caps of SFTP, S3 folders, HTTP APIs and placeholders. Worker concurrency is one of them: every worker resizes its own pool from
`workers.*` while it runs (tasks/worker_pool.py).

Those a process reads once when it starts (`rollout`: memory and recycling of the
workers, the API's threads, the embedded engines' memory, the beat's intervals) are
taken from here ONCE, at the process's first reading (`at_start`), and stay frozen
until it starts again: the panel saves them and the components named in `rollout` are
rolled out (docs/specs/panel-settings.md). A pod that starts later takes them as well.
A few are not settings fields but variables read at import (`env_name`).

Left out, and changed only in the environment: credentials, addresses, and the
switches tied to the chart's resources.

How they arrive: the gateway keeps them in its database (the true source, with the
audit) and writes them here with `PUT /settings`; the engine keeps them in Valkey
(`KEY`), which API and workers share, and every process reads them again at most every
`REREAD_EVERY` seconds. The gateway sends them again at intervals: if Valkey empties,
they come back by themselves. Every value is checked twice, when it arrives and when it
is read: an invalid one is discarded (the environment's holds) and written to the log.

`get_settings()` (core/config.py) returns the environment with these on top; whoever
keeps a section of the settings in an object made once (the ClickHouse and BigQuery
engines) passes it through `sovrapposte()` at every use.
"""
from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

KEY = "tabularia:regolazioni"
REREAD_EVERY = 10.0  # seconds: after a change, every process sees it within this time
SEVEN_DAYS = 7 * 24 * 3600


@dataclass(frozen=True)
class Item:
    key: str  # "<section>.<field>" of the engine's settings
    category: str  # groups the items in the panel (the frontend translates the labels)
    unit: str  # rows | seconds | bytes | threads | processes | files | keys | … | "" (a fraction, yes/no)
    minimum: float
    maximum: float
    kind: str = "int"  # int | float | bool
    # the components that read it only when they start (engine, worker, preview-worker,
    # beat): empty = applied live
    rollout: tuple[str, ...] = ()
    # not a settings field: the variable read at import, and its default
    env_name: str = ""
    default: Any = None

    @property
    def section(self) -> str:
        return self.key.split(".", 1)[0]

    @property
    def field(self) -> str:
        return self.key.split(".", 1)[1]

    @property
    def variable(self) -> str:
        """The environment variable that sets it (and stays the starting value)."""
        return self.env_name or f"{self.section.upper()}__{self.field.upper()}"


WORKERS = ("worker", "preview-worker")

# The ranges are narrower than what the environment accepts: a typo in the panel
# must not bring the system to its knees (a huge row group sends the import OOM, a
# timeout of a few seconds fails everything). Whoever really needs a value out of
# range sets it in the environment.
ITEMS: tuple[Item, ...] = (
    Item("ingest.parquet_row_group_rows", "import", "rows", 10_000, 5_000_000),
    Item("ingest.db_read_timeout_seconds", "import", "seconds", 30, 86_400),
    Item("export.max_rows", "export", "rows", 0, 100_000_000),
    Item("celery.task_time_limit", "execution", "seconds", 60, SEVEN_DAYS),
    Item("celery.task_soft_time_limit", "execution", "seconds", 30, SEVEN_DAYS),
    Item("cache.ttl_seconds", "cache", "seconds", 300, 30 * 24 * 3600),
    Item("cache.max_step_rows", "cache", "rows", 0, 100_000_000),
    Item("clickhouse_external.parquet_scan_max_threads", "clickhouse", "threads", 0, 64),
    Item("clickhouse_external.max_execution_time", "clickhouse", "seconds", 0, 86_400),
    Item("clickhouse_external.materialize_min_rows", "clickhouse", "rows", 0, 1_000_000_000),
    Item("clickhouse_external.materialize_ttl_seconds", "clickhouse", "seconds", 60, SEVEN_DAYS),
    Item("bigquery.maximum_bytes_billed", "bigquery", "bytes", 0, 100 * 1024**4),
    Item("clickhouse_external.connect_timeout", "clickhouse", "seconds", 1, 300),
    Item("sftp.max_file_bytes", "sftp", "bytes", 1024**2, 100 * 1024**3),
    Item("sftp.max_files", "sftp", "files", 1, 100_000),
    Item("sftp.max_total_bytes", "sftp", "bytes", 1024**2, 1024**4),
    Item("sftp.timeout_seconds", "sftp", "seconds", 1, 600, kind="float"),
    Item("s3_source.max_file_bytes", "s3", "bytes", 1024**2, 100 * 1024**3),
    Item("s3_source.max_files", "s3", "files", 1, 100_000),
    Item("s3_source.max_total_bytes", "s3", "bytes", 1024**2, 1024**4),
    Item("s3_source.max_listed", "s3", "keys", 100, 10_000_000),
    Item("s3_source.max_request_seconds", "s3", "seconds", 10, 86_400, kind="float"),
    Item("api.timeout_seconds", "api", "seconds", 1, 600, kind="float"),
    Item("api.max_pages", "api", "pages", 1, 1_000_000),
    Item("api.max_calls", "api", "calls", 1, 100_000),
    Item("api.max_rows", "api", "rows", 1, 1_000_000_000),
    Item("api.max_response_bytes", "api", "bytes", 1024, 10 * 1024**3),
    Item("api.max_total_bytes", "api", "bytes", 1024, 1024**4),
    Item("api.max_retries", "api", "retries", 0, 20),
    Item("api.max_retry_wait_seconds", "api", "seconds", 0, 3600, kind="float"),
    Item("api.max_redirects", "api", "redirects", 0, 30),
    Item("api.max_request_seconds", "api", "seconds", 1, 86_400, kind="float"),
    Item("api.min_interval_seconds", "api", "seconds", 0, 3600, kind="float"),
    Item("placeholders.max_values", "placeholders", "values", 1, 100_000),
    # set from the gateway's panel together with the gateway's own (the second barrier)
    Item("upload.max_bytes", "upload", "bytes", 1024**2, 1024**4),  # never 0 from the panel: /tmp has a limit
    # ── read when a process starts ──
    Item("app.engine_api_threads", "engine_api", "threads", 40, 2000, rollout=("engine",)),
    # at most under the chart's ingress timeout (proxy-read-timeout 300 s): past it people
    # would see the proxy's 504 while the engine keeps working; more goes in the environment
    Item("previews.timeout_seconds", "engine_api", "seconds", 10, 290, kind="float", rollout=("engine",),
         env_name="PREVIEW_TIMEOUT_SECONDS", default=120.0),
    Item("celery.max_tasks_per_child", "execution", "tasks", 1, 100_000, rollout=WORKERS),
    Item("celery.max_memory_per_child_kb", "execution", "kilobytes", 100 * 1024, 256 * 1024**2, rollout=WORKERS),
    Item("celery.max_memory_headroom_frac", "execution", "", 0.1, 0.95, kind="float", rollout=WORKERS),
    Item("celery.max_memory_per_child_fallback_kb", "execution", "kilobytes", 100 * 1024, 256 * 1024**2, rollout=WORKERS),
    Item("celery.max_memory_per_child_floor_kb", "execution", "kilobytes", 50 * 1024, 256 * 1024**2, rollout=WORKERS),
    Item("cache.sweep_interval_seconds", "cache", "seconds", 60, 7 * 86_400, rollout=("beat",)),
    Item("metrics.storage_stats_interval_seconds", "cache", "seconds", 10, 3600, rollout=("beat",)),
    Item("chdb.max_memory_usage", "embedded", "bytes", 128 * 1024**2, 1024**4, rollout=WORKERS,
         env_name="CHDB_MAX_MEMORY_USAGE", default=1_500_000_000),
    Item("chdb.max_bytes_before_external_group_by", "embedded", "bytes", 0, 1024**4, rollout=WORKERS,
         env_name="CHDB_MAX_BYTES_BEFORE_EXTERNAL_GROUP_BY", default=512 * 1024**2),
    Item("chdb.max_bytes_before_external_sort", "embedded", "bytes", 0, 1024**4, rollout=WORKERS,
         env_name="CHDB_MAX_BYTES_BEFORE_EXTERNAL_SORT", default=512 * 1024**2),
    Item("chdb.max_bytes_in_join", "embedded", "bytes", 0, 1024**4, rollout=WORKERS,
         env_name="CHDB_MAX_BYTES_IN_JOIN", default=512 * 1024**2),
    Item("engine.max_cross_join_rows", "execution", "rows", 1000, 10_000_000_000, rollout=WORKERS,
         env_name="ENGINE_MAX_CROSS_JOIN_ROWS", default=50_000_000),
    # processes per worker container: shown in Admin → Workers, not among the settings
    Item("workers.run_concurrency", "workers", "processes", 1, 32),
    Item("workers.preview_concurrency", "workers", "processes", 1, 32),
)
BY_KEY = {v.key: v for v in ITEMS}

# constraints between two items: (lower, higher) — the first must stay BELOW the second
#  - the soft limit lets a task close in order BEFORE its process is killed;
#  - whoever waits on an import must read the read timeout, not the killed task.
CONSTRAINTS = (
    ("celery.task_soft_time_limit", "celery.task_time_limit"),
    ("ingest.db_read_timeout_seconds", "celery.task_soft_time_limit"),
)


def _from_env(item: Item) -> Any:
    """A variable read at import (`Item.env_name`): what the environment gives it."""
    raw = (os.getenv(item.env_name) or "").strip()
    if not raw:
        return item.default
    try:
        return float(raw) if item.kind == "float" else int(raw)
    except ValueError:
        # as before these settings could come from the panel: a typing mistake in the
        # environment stops the process, it does not quietly become the default
        raise ValueError(f"{item.env_name}={raw!r} is not a {'number' if item.kind == 'float' else 'whole number'}") from None


def _value(s: Any, key: str) -> Any:
    item = BY_KEY.get(key)
    if item is not None and item.env_name:
        return _from_env(item)
    section, field = key.split(".", 1)
    return getattr(getattr(s, section), field)


def validate(values: Any, base: Any, hard_limit_max: Optional[int] = None,
           changed: Optional[set[str]] = None) -> tuple[dict[str, Any], list[dict]]:
    """(good values, errors). The errors have a code, not a sentence: the frontend writes
    the sentence in the reader's language.

    `base` is the environment's settings; the constraints between items are checked on
    the EFFECTIVE values (the environment with the good ones on top). The gateway gives
    `hard_limit_max`: it calls a run lost after a threshold of its own, and the hard
    limit must stay below it. `changed`: the items the writer just changed — when a
    constraint breaks between a changed one and one that was there already, the fault
    is the changed one's (the error must name what was touched)."""
    good: dict[str, Any] = {}
    errors: list[dict] = []
    if not isinstance(values, dict):  # valid JSON that is not an object (a list, a number)
        return {}, [{"key": "", "code": "unknown"}]
    for key, v in values.items():
        item = BY_KEY.get(key)
        if item is None:
            errors.append({"key": key, "code": "unknown"})
            continue
        if item.kind == "bool":
            if isinstance(v, bool):
                good[key] = v
            else:
                errors.append({"key": key, "code": "boolean"})
            continue
        if item.kind == "float":
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                errors.append({"key": key, "code": "number"})
                continue
            try:
                v = float(v)
            except OverflowError:  # an integer beyond any decimal (10**400)
                errors.append({"key": key, "code": "max", "limit": item.maximum})
                continue
            if not math.isfinite(v):
                errors.append({"key": key, "code": "number"})
                continue
        elif isinstance(v, bool) or not isinstance(v, int):
            errors.append({"key": key, "code": "integer"})
            continue
        maximum = item.maximum
        if key == "celery.task_time_limit" and hard_limit_max is not None:
            maximum = min(maximum, hard_limit_max)
        if v < item.minimum:
            errors.append({"key": key, "code": "min", "limit": item.minimum})
        elif v > maximum:
            errors.append({"key": key, "code": "max", "limit": maximum})
        else:
            good[key] = v
    changed = changed or set()
    # the hard limit taken back to the environment's: that value too must stay under the
    # gateway's lost-run threshold (else a run still alive would be closed as failed)
    hard = "celery.task_time_limit"
    if hard_limit_max is not None and hard not in good and hard in changed \
            and _value(base, hard) > hard_limit_max:
        errors.append({"key": hard, "code": "env_too_high", "limit": hard_limit_max})
    # as long as one is dropped: removing an item may break a constraint checked already
    # (the soft one removed → the environment's comes back, perhaps above the hard one set)
    touched = True
    while touched:
        touched = False
        for lower, higher in CONSTRAINTS:
            if lower not in good and higher not in good:
                continue  # both from the environment: nobody touched them here
            a = good.get(lower, _value(base, lower))
            b = good.get(higher, _value(base, higher))
            if a < b:
                continue
            # the fault is of one set here, and between two set here of the one just
            # changed; on a tie, of the lower
            set_values = [k for k in (lower, higher) if k in good]
            culprit = next((k for k in set_values if k in changed), None) if len(set_values) == 2 else None
            culprit = culprit or set_values[0]
            other = higher if culprit == lower else lower
            good.pop(culprit)
            errors.append({"key": culprit, "code": "below" if culprit == lower else "above",
                           "other": other, "limit": b if culprit == lower else a})
            touched = True
    return good, errors


def apply_values(base: Any, good: dict[str, Any]) -> Any:
    """`base` with the values on top, section by section (the others stay the same objects).
    The variables read at import are not settings: their modules ask `value_at_start`."""
    good = {k: v for k, v in good.items() if not BY_KEY[k].env_name}
    if not good:
        return base
    by_section: dict[str, dict[str, Any]] = {}
    for key, v in good.items():
        section, field = key.split(".", 1)
        by_section.setdefault(section, {})[field] = v
    return base.model_copy(update={
        section: getattr(base, section).model_copy(update=fields) for section, fields in by_section.items()
    })


# ── reading from Valkey, per process ─────────────────────────────────────────

_lock = threading.Lock()
_read_at = float("-inf")  # when it was last read (monotonic)
_values: dict[str, Any] = {}  # the last read and valid, live ones and rollout ones
# the rollout ones as they were at this process's first reading: frozen until it starts
# again (None = not read yet)
_start: Optional[dict[str, Any]] = None
_version = 0  # grows at every change: the key of the cached copies
_result: tuple[Any, int, Any] | None = None  # (base, version, settings)
# (the object's id, version) → (the object, its copy): the object stays here so that
# its id does not pass to another while the copy is cached
_copies: dict[tuple[int, int], tuple[Any, Any]] = {}
_redis = None


def _client():
    global _redis
    if _redis is None:
        import redis

        from app.core.config import settings_from_env

        # short timeouts: the read is on the path of whoever asks for the settings, and
        # if Valkey does not answer the last values read go on
        _redis = redis.Redis.from_url(settings_from_env().redis.url, decode_responses=True,
                                      socket_timeout=0.5, socket_connect_timeout=0.5)
    return _redis


def read_raw() -> dict:
    raw = _client().get(KEY)
    return json.loads(raw) if raw else {}


def _pin_start(values: dict[str, Any]) -> None:
    global _start
    if _start is None:
        _start = {k: v for k, v in values.items() if BY_KEY[k].rollout}
        if _start:
            logger.info("panel settings read when this process starts: %s", _start)


def _current() -> dict[str, Any]:
    global _read_at, _values, _version
    now = time.monotonic()
    if now - _read_at < REREAD_EVERY:
        return _values
    with _lock:
        if now - _read_at < REREAD_EVERY:  # another thread just read them
            return _values
        _read_at = now
        try:
            raw_values = read_raw()
        except Exception as e:  # noqa: BLE001 — Valkey down: the last values stay
            logger.warning("panel settings: Valkey does not answer, the last ones read stay (%s)", e)
            _pin_start(_values)
            return _values
        from app.core.config import settings_from_env

        if not isinstance(raw_values, dict):
            # written by someone who is not the gateway: the last valid ones stay, and the
            # gateway puts its own back within a minute
            logger.warning("panel settings: Valkey holds a %s instead of an object: the last ones read stay",
                           type(raw_values).__name__)
            _pin_start(_values)
            return _values
        good, errors = validate(raw_values, settings_from_env())
        _pin_start(good)
        if good != _values:
            if errors:
                logger.warning("panel settings: discarded as invalid: %s", errors)
            logger.info("settings from the panel: %s", good or "none (the environment's hold)")
            _values = good
            _version += 1
            _copies.clear()
    return _values


def _in_force() -> dict[str, Any]:
    """The live ones as they are now, the rollout ones as this process started with."""
    current = _current()
    return {**{k: v for k, v in current.items() if not BY_KEY[k].rollout}, **(_start or {})}


WAIT_AT_START = 45.0  # seconds: the gateway sends its values at least every minute


def wait_at_first_start(maximum: float = WAIT_AT_START) -> bool:
    """A worker or beat that starts on an empty Valkey (a restart without persistence, a
    new cluster) would freeze the environment's values of the settings read at start, for
    its whole life, while the panel shows the panel's. It waits for the gateway's values
    instead, up to `maximum` seconds; True if they came."""
    start = time.monotonic()
    deadline = start + maximum
    while True:
        try:
            if _client().get(KEY) is not None:
                wait = time.monotonic() - start
                if wait >= 2:  # a warning: Celery's logging is not set up yet at import
                    logger.warning("panel settings: waited %ds for the gateway's values (Valkey had none)", int(wait))
                return True
        except Exception:  # noqa: BLE001 — Valkey not up yet: the broker waits for it too
            pass
        if time.monotonic() >= deadline:
            logger.warning("panel settings: no values from the gateway after %ss: this process starts with the "
                           "environment's for the settings read at start", int(maximum))
            return False
        time.sleep(2)


def at_start(base: Any) -> Any:
    """`base` with the rollout ones this process started with (Celery's configuration,
    built from the environment's settings). Forces the first reading."""
    _current()
    return apply_values(base, _start or {})


def value_at_start(variable: str) -> Any:
    """A variable read at import (CHDB_MAX_MEMORY_USAGE…): the panel's value this process
    started with, else the environment's."""
    item = next(v for v in ITEMS if v.env_name == variable)
    _current()
    return (_start or {}).get(item.key, _from_env(item))


def start_values() -> dict[str, Any]:
    """The rollout ones this process started with (the panel's; the others: the environment's)."""
    _current()
    return dict(_start or {})


def applied(base: Any) -> Any:
    """The environment's settings with the current panel values on top. The path of every
    `get_settings()`: it costs a comparison until there is something to read again."""
    global _result
    values = _in_force()
    if not values:
        return base
    r = _result
    if r is not None and r[0] is base and r[1] == _version:
        return r[2]
    s = apply_values(base, values)
    _result = (base, _version, s)
    return s


def overlapping(obj: Any, section: str) -> Any:
    """A section of the settings held by an object made once (the ClickHouse engine keeps
    its own from the start): with that section's current panel values on top. The same
    object when there are none."""
    fields = {k.split(".", 1)[1]: v for k, v in _in_force().items()
             if k.startswith(section + ".") and not BY_KEY[k].env_name}
    if not fields:
        return obj
    key = (id(obj), _version)
    found = _copies.get(key)
    if found is not None and found[0] is obj:
        return found[1]
    if len(_copies) > 64:  # the live objects are few (one per engine): only a cap
        _copies.clear()
    copy = obj.model_copy(update=fields)
    _copies[key] = (obj, copy)
    return copy


def forget(start_too: bool = True) -> None:
    """Read again at the next request (tests, and `get_settings.cache_clear()`).
    `start_too=False` after a change: what this process started with stays."""
    global _read_at, _values, _result, _version, _start
    with _lock:
        _read_at = float("-inf")
        _values = {}
        if start_too:
            _start = None
        _result = None
        _version += 1
        _copies.clear()


def write(good: dict[str, Any]) -> None:
    """The new panel values for all the processes (the caller validated them already).
    This process uses them at once; the others within REREAD_EVERY."""
    # always written, `{}` too: a key present means the gateway has sent its values, which
    # the workers wait for when they start (`wait_at_first_start`)
    _client().set(KEY, json.dumps(good, sort_keys=True))
    forget(start_too=False)
