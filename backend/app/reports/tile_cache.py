"""The cache of a dashboard's numbers (docs/specs/dashboards.md, «The cache»): a hundred people
on one dashboard run one query.

Two levels, each named by what it holds, never by who asked:

- **the rows of a dataset**: a parquet under the storage's `cache/` prefix, written and read by
  the preview workers (`read_rows`, what `tiles.compute` takes as `read`). Never reached by a
  browser. Its entry in Valkey says it is there, when it was read, and the columns as the engine
  names them; a dataset over the cap or a source that failed is remembered there too, so that it
  is not asked again at every request. `RowsIndex` keeps them within DASHBOARDS__CACHE_BUCKET_BYTES
  and takes them away when they end.
- **the numbers of a tile**: JSON in Valkey (`numbers`), in the engine's API, before anything is
  queued: a full cache does not touch the workers. Within DASHBOARDS__CACHE_TILE_BYTES each.

**Rights are not here.** The gateway checks them with the reader's at every request, then sends
only what that reader may read: what stands behind a key is the source resolved for them (the
real parquet of a name on the catalog, the SQL with the values in it, the connection), so two
people share a result only because they read the same thing. A tile on a dataset refused has no
key at all: it says its refusal without looking at the cache.

One computation per key (`_once`): whoever finds nothing takes a lock in Valkey, kept alive by a
thread while it works; the others wait for the value, up to PREVIEW_TIMEOUT_SECONDS, after which
they say so, and do not query. A holder that dies leaves a lock that ends in LOCK_SECONDS, and one
of those waiting takes its place. A failure is kept for FAILED_SECONDS: those waiting get it, and a
source down is not asked by all of them in turn. Nothing here cancels a computation: others may be
waiting for it, so a person's newer request does not knock it down.

A refresh by hand (`claim_refresh`, at most every REFRESH_SECONDS per dashboard) gives its datasets
a new generation, which is part of the key of their numbers and of what their rows' entry must
say: everything read from them is read again, once, for everyone.

All of it is a courtesy: when Valkey or the bucket do not answer, the dashboard works without it.
When the broker is that same Valkey no worker can be reached, and the numbers are worked out
here, one request at a time (`on_worker`).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

import redis
from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from app.core.config import get_settings
from app.core.panel_settings import value_at_start
from app.engine.cache import StepCache
from app.reports import data as rdata
from app.reports import definition as rd
from app.reports import tiles as rtiles
from app.reports.errors import ReportError

logger = logging.getLogger(__name__)

PREFIX = "dataprep:dashboards:"
STATS = "dataprep:metrics:dashboard_cache"  # counters, for the metrics
VERSION = 1  # of what is kept: an answer of another shape is not to be read as this one
LOCK_SECONDS = 30  # a holder that stopped renewing has died
FAILED_SECONDS = 10
REFRESH_SECONDS = 10
GENERATION_SECONDS = 8 * 24 * 3600  # longer than anything it can tell apart (cache_seconds: 7 days at most)
WAIT_SECONDS = float(value_at_start("PREVIEW_TIMEOUT_SECONDS"))
DOWN_SECONDS = 5.0  # after a failure, Valkey is not asked again for this long
BUCKET_DOWN_SECONDS = 30.0
FILE_MARGIN_SECONDS = 60  # a file outlives its entry: whoever just read that is downloading it
BUSY = "These numbers are still being computed for someone else: try again in a moment"

_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
_RENEW = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('expire', KEYS[1], ARGV[2]) else return 0 end"


# ── Valkey, as a courtesy ────────────────────────────────────────────────────
_client: redis.Redis | None = None
_down_until = 0.0
_memory_seen = (0.0, False)  # (when it was read, past the limit)


def _r() -> redis.Redis:
    global _client
    if _client is None:
        # short: a Valkey that hangs must not hang a dashboard (the step cache's client waits for ever)
        _client = redis.Redis.from_url(get_settings().redis.url, decode_responses=True, socket_connect_timeout=1, socket_timeout=2)
    return _client


def _safely(ask: Callable[[redis.Redis], Any], otherwise: Any = None) -> Any:
    """What Valkey answers, or `otherwise`. After a failure it is left alone for DOWN_SECONDS:
    every call would wait for its timeout."""
    global _down_until
    if time.monotonic() < _down_until:
        return otherwise
    try:
        return ask(_r())
    except (redis.RedisError, OSError) as e:
        _down_until = time.monotonic() + DOWN_SECONDS
        logger.warning("dashboards: Valkey does not answer (%s): going on without the cache", e)
        return otherwise


def _is_down() -> bool:
    return time.monotonic() < _down_until


def _count(field: str, n: int = 1) -> None:
    _safely(lambda r: r.hincrby(STATS, field, n))


def stats() -> dict[str, int]:
    """The counters, for the metrics: tile_hits, tile_misses, rows_hits, rows_reads, and what was
    not kept (tile_too_large, rows_too_large, valkey_full)."""
    return {k: int(v) for k, v in (_safely(lambda r: r.hgetall(STATS), {}) or {}).items()}


def _hash(*parts: Any) -> str:
    """JSON, with its quotes and escapes: no value can pass for two, nor end a key. Only hex
    digits ever reach Valkey, and no credential: a connection's password is hashed as encrypted."""
    return hashlib.sha256(json.dumps(parts, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def lifetime(refresh_seconds: Any = 0) -> int:
    """DASHBOARDS__CACHE_SECONDS, and at least the dashboard's own interval."""
    own = refresh_seconds if isinstance(refresh_seconds, int) and not isinstance(refresh_seconds, bool) else 0
    return max(get_settings().dashboards.cache_seconds, own)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _age(at: Any) -> float | None:
    try:
        return (_now() - datetime.fromisoformat(at)).total_seconds()
    except (TypeError, ValueError):
        return None


# ── the keys ─────────────────────────────────────────────────────────────────
def rows_hash(spec: Any, cap: int) -> str:
    """What names the rows of a dataset: its source as it was resolved after the parameters, for
    the reader who asks (a chain's parquet, which is the datasource's snapshot, its operations, a
    catalog SQL's tables, a query with its values, the connection), its engine, and the cap."""
    return _hash("rows", VERSION, spec, cap)


def tile_hash(item: dict, datasets: dict[str, Any], params: dict, parameters: Any, page: int, state: Any, locale: Any, zone: Any,
              title: str, cap: int, seconds: int, clicks: Any = ()) -> str:
    """What names the numbers of a tile: its definition without its place, what it reads (`datasets`:
    by dataset, its fields and calculated ones, the hash of its rows, its generation), the
    parameters, the page and the state of this tile, the clicks, locale, zone, the title REPORT()
    reads. Not who asks, nor which dashboard."""
    own = {k: v for k, v in item.items() if k not in ("x", "y", "w", "h")}
    return _hash("tile", VERSION, own, datasets, params, parameters, page, state, locale, zone, title, cap, seconds, list(clicks))


def generation(root: str) -> str:
    return _safely(lambda r: r.get(f"{PREFIX}gen:{root}"), "") or ""


def _generations(roots: list[str], fresh: str | None) -> dict[str, str]:
    """The generation of each root: a new one, `fresh`, when this is a refresh."""
    if fresh:
        def write(r: redis.Redis) -> None:
            with r.pipeline(transaction=False) as p:
                for root in roots:
                    p.set(f"{PREFIX}gen:{root}", fresh, ex=GENERATION_SECONDS)
                p.execute()
        _safely(write)
        return dict.fromkeys(roots, fresh)
    found = _safely(lambda r: r.mget([f"{PREFIX}gen:{root}" for root in roots]), None) if roots else None
    return {root: g or "" for root, g in zip(roots, found or [None] * len(roots))}


def claim_refresh(dashboard: int) -> str | None:
    """A refresh by hand, if this dashboard had none in the last REFRESH_SECONDS, for everyone:
    the token that names the new generation, or None (answer with what there is)."""
    token = uuid.uuid4().hex
    taken = _safely(lambda r: r.set(f"{PREFIX}refresh:{dashboard}", token, nx=True, ex=REFRESH_SECONDS), True)
    return token if taken else None


# ── the lock ─────────────────────────────────────────────────────────────────
def _lock_key(h: str) -> str:
    return f"{PREFIX}lock:{h}"


def _take(h: str, token: str) -> bool:
    """Valkey down: true, there is nobody to wait for."""
    return bool(_safely(lambda r: r.set(_lock_key(h), token, nx=True, ex=LOCK_SECONDS), True))


@contextmanager
def _kept(hashes: list[str], token: str) -> Iterator[None]:
    """The locks taken stay ours for as long as the work goes on: nobody can know how long a query
    lasts, and a holder that dies must be found out in LOCK_SECONDS, not at the end of the longest
    query there can be. In a thread of its own, since the work blocks in a driver."""
    stop = threading.Event()

    def beat() -> None:
        while not stop.wait(LOCK_SECONDS / 3):
            _safely(lambda r: [r.eval(_RENEW, 1, _lock_key(h), token, LOCK_SECONDS) for h in hashes])

    threading.Thread(target=beat, daemon=True, name="dashboard-cache-lock").start()
    try:
        yield
    finally:
        stop.set()
        _safely(lambda r: [r.eval(_RELEASE, 1, _lock_key(h), token) for h in hashes])


# ── the rows: a file on the bucket ───────────────────────────────────────────
class RowsIndex(StepCache):
    """The step cache's index and sweep, on an index of its own and on the same prefix: `cache/`
    is managed, its size in the metrics. Here the score of an entry is when it ends, as its life
    is the dashboard's and not one for all (`evict_expired(0)`), and sizes are kept: the rows of
    one dashboard can be a large share of a bucket."""

    def __init__(self, storage: Any, client: Any):
        super().__init__(storage, redis_client=client, namespace="dashboards")
        self.sizes = f"{self.index_set}:bytes"

    def keep(self, h: str, size: int, ends_at: float) -> None:
        self.redis.sadd(self.index_set, h)
        self.redis.zadd(self.atime_zset, {h: ends_at})
        self.redis.hset(self.sizes, h, size)

    def make_room(self, size: int, budget: int) -> None:
        """Those about to end leave first, until `size` fits."""
        sizes = {h: int(v) for h, v in self.redis.hgetall(self.sizes).items()}
        total = sum(sizes.values())
        for h in self.redis.zrange(self.atime_zset, 0, -1):
            if total + size <= budget:
                return
            self.storage.delete_object(self.bucket, self.object_key(h))
            self.redis.srem(self.index_set, h)
            self.redis.zrem(self.atime_zset, h)
            self.redis.hdel(self.sizes, h)
            total -= sizes.get(h, 0)

    def evict_expired(self, ttl_seconds: int = 0) -> int:
        removed = super().evict_expired(ttl_seconds)
        known = self.redis.smembers(self.index_set)
        for h in set(self.redis.hkeys(self.sizes)) - set(known):
            self.redis.hdel(self.sizes, h)
        return removed


_quick_storage: Any = None
_bucket_down_until = 0.0


def _storage() -> Any:
    global _quick_storage
    if _quick_storage is None:
        from app.utils import StorageService

        _quick_storage = StorageService(quick=True)
    return _quick_storage


def _index() -> RowsIndex:
    return RowsIndex(_storage(), _r())


def sweep() -> int:
    """What ended. The beat's, with the step cache's (tasks/jobs.py)."""
    try:
        return _index().evict_expired(0)
    except Exception as e:  # noqa: BLE001 — the sweep must not stop the step cache's
        logger.warning("dashboards: the sweep of the rows failed: %s", e)
        return 0


def _bucket_failed(e: Exception, what: str) -> None:
    global _bucket_down_until
    from app.engine.query_tag import was_interrupted

    if was_interrupted(e):
        raise e
    if isinstance(e, FileNotFoundError):  # what the entry named is gone, not the bucket
        return
    if str((getattr(e, "response", None) or {}).get("Error", {}).get("Code")) not in ("404", "NoSuchKey"):
        _bucket_down_until = time.monotonic() + BUCKET_DOWN_SECONDS
    logger.warning("dashboards: %s: going on without the rows' file (%s)", what, e)


def _keep_file(h: str, df: Any, seconds: int) -> int | None:
    """The rows on the bucket: their size, or None when they are not kept."""
    budget = get_settings().dashboards.cache_bucket_bytes
    if not budget or _is_down() or time.monotonic() < _bucket_down_until:
        return None
    fd, path = tempfile.mkstemp(prefix="dashboard-rows-", suffix=".parquet")
    os.close(fd)
    try:
        df.write_parquet(path)  # a column of Python's objects cannot: read again, as without a cache
        size = os.path.getsize(path)
        if size > budget:
            _count("rows_too_large")
            logger.info("dashboards: rows of %s bytes are not kept: the room is %s", size, budget)
            return None
        index = _index()
        index.make_room(size, budget)
        index.storage.upload_file(path, index.bucket, index.object_key(h))
        index.keep(h, size, time.time() + seconds + FILE_MARGIN_SECONDS)
        return size
    except (redis.RedisError, OSError) as e:
        logger.warning("dashboards: the rows' file was not kept: %s", e)
        return None
    except Exception as e:  # noqa: BLE001 — a cache that fails is no cache, not a failure
        _bucket_failed(e, "the rows' file was not written")
        return None
    finally:
        os.unlink(path)


def _from_file(name: str, h: str, entry: dict, at: datetime, tz: Any) -> rdata.DataSet | None:
    """The rows on the bucket, or None when they cannot be had: they are read again."""
    if time.monotonic() < _bucket_down_until:
        return None
    fd, path = tempfile.mkstemp(prefix="dashboard-rows-", suffix=".parquet")
    os.close(fd)
    try:
        import polars as pl

        index = _index()
        index.storage.download_file(index.bucket, index.object_key(h), path)
        df = pl.read_parquet(path, memory_map=False)
    except Exception as e:  # noqa: BLE001 — as above: a file swept away, a bucket down
        _bucket_failed(e, "the rows' file was not read")
        _safely(lambda r: r.delete(f"{PREFIX}rows:{h}"))
        return None
    finally:
        os.unlink(path)
    ds = rdata.dataset_from(name, entry["columns"], df, tz)
    ds.read_at, ds.from_cache = at, True
    _count("rows_hits")
    return ds


def _rows_entry(h: str) -> dict | None:
    text = _safely(lambda r: r.get(f"{PREFIX}rows:{h}"))
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _rows_kept(name: str, h: str, version: str, seconds: int, cap: int, tz: Any) -> rdata.DataSet | None:
    """The dataset as it is kept, None when it is not; what was remembered of it is raised."""
    entry = _rows_entry(h)
    if entry is None or entry.get("gen") != version:
        return None
    age = _age(entry.get("at"))
    if age is None or age > (FAILED_SECONDS if "error" in entry else seconds):  # a dashboard with a shorter life than who wrote it
        return None
    if entry.get("over"):
        _count("rows_hits")
        raise rdata.over_cap(name, cap)
    if "error" in entry:
        if entry.get("name") != name:  # its message names the dataset, in another's dashboard
            return None
        raise ReportError(entry["error"], entry.get("tag") or "unprocessable")
    return _from_file(name, h, entry, datetime.fromisoformat(entry["at"]), tz)


def _read_and_keep(name: str, h: str, version: str, spec: dict, cap: int, tz: Any, seconds: int) -> rdata.DataSet:
    at = _now()
    base = {"gen": version, "at": at.isoformat()}

    def remember(entry: dict, ttl: int) -> None:
        _safely(lambda r: r.set(f"{PREFIX}rows:{h}", json.dumps({**base, **entry}), ex=ttl))

    _count("rows_reads")
    try:
        columns, rows, over = rdata._read_one(name, spec, cap)
    except ReportError as e:
        remember({"name": name, "error": e.message, "tag": e.tag}, FAILED_SECONDS)
        raise
    if over:
        remember({"over": True}, seconds)
        raise rdata.over_cap(name, cap)
    if not isinstance(rows, list):
        size = _keep_file(h, rows, seconds)  # before the columns: making them empties the frame
        if size is not None:
            remember({"columns": columns, "bytes": size}, seconds)
    ds = rdata.dataset_from(name, columns, rows, tz)
    ds.read_at = at
    return ds


def read_rows(name: str, spec: Any, cap: int, tz: Any, seconds: int, root: str | None = None) -> rdata.DataSet:
    """`rdata.read_dataset` with the cache in front: one read of a source for everyone, however
    many requests and whichever tiles they ask for. `root`: whose generation these rows follow,
    when they are not the dataset's whole (`_only_shown`, a list of choices). Rows given as they
    are have nothing to keep."""
    if not isinstance(spec, dict) or not ("connection" in spec or "input_key" in spec):
        return rdata.read_dataset(name, spec, cap, tz)
    h = rows_hash(spec, cap)
    version = generation(root or h)
    deadline = time.monotonic() + WAIT_SECONDS
    delay = 0.02
    while True:
        ds = _rows_kept(name, h, version, seconds, cap, tz)
        if ds is not None:
            return ds
        token = uuid.uuid4().hex
        if _take(h, token):
            with _kept([h], token):
                ds = _rows_kept(name, h, version, seconds, cap, tz)  # who held it may have just finished
                return ds if ds is not None else _read_and_keep(name, h, version, spec, cap, tz, seconds)
        if time.monotonic() >= deadline:
            raise ReportError(f"The dataset «{name}» is still being read for someone else: try again in a moment")
        time.sleep(delay)
        delay = min(delay * 1.5, 0.5)


def reader(seconds: int, root: str | None = None) -> Callable[[str, Any, int, Any], rdata.DataSet]:
    """What `tiles.compute` takes as `read`."""
    return lambda name, spec, cap, tz: read_rows(name, spec, cap, tz, seconds, root)


def choices_reading(spec: Any, seconds: int) -> dict:
    """What `render.choices` takes for a dashboard's list: the rows it needs follow the generation
    of the dataset's, and the cap is the dashboard's."""
    cap = rtiles.max_rows()
    return {"read": reader(seconds, rows_hash(spec, cap)), "max_rows": cap}


# ── the numbers: a value in Valkey ───────────────────────────────────────────
def _value_key(h: str) -> str:
    return f"{PREFIX}value:{h}"


def _values(hashes: dict[str, str]) -> dict[str, dict]:
    if not hashes:
        return {}
    texts = _safely(lambda r: r.mget([_value_key(h) for h in hashes.values()])) or []
    found = {}
    for name, text in zip(hashes, texts):
        try:
            if text:
                found[name] = json.loads(text)
        except ValueError:
            pass
    return found


def _valkey_full() -> bool:
    """Valkey carries the queues too and has no limit of its own: past DASHBOARDS__CACHE_VALKEY_BYTES,
    the cache steps back. Asked at most every few seconds."""
    global _memory_seen
    seen_at, full = _memory_seen
    if time.monotonic() - seen_at >= 5:
        used = _safely(lambda r: r.info("memory").get("used_memory", 0), 0)
        full = used > get_settings().dashboards.cache_valkey_bytes
        _memory_seen = (time.monotonic(), full)
    return full


def _keep_value(h: str, value: dict, ttl: float) -> None:
    text = json.dumps(value, separators=(",", ":"))
    if len(text.encode()) > get_settings().dashboards.cache_tile_bytes:
        _count("tile_too_large")  # those waiting work it out from the rows, which are shared
        return
    if _valkey_full():
        _count("valkey_full")
        return
    _safely(lambda r: r.set(_value_key(h), text, ex=max(int(ttl), 1)))


Work = Callable[[list[str], float], dict[str, tuple[dict, float]]]


def _worked_out(held: dict[str, str], token: str, work: Work, deadline: float) -> dict[str, tuple[dict, bool]]:
    with _kept(list(held.values()), token):
        # who held these may have finished between the look and the lock
        out = {name: (value, True) for name, value in _values(held).items()}
        todo = [name for name in held if name not in out]
        if todo:
            try:
                done = work(todo, max(deadline - time.monotonic(), 1.0))
            except Exception as e:
                failure = {"failed": {"status": getattr(e, "status_code", 500),
                                      "detail": str(getattr(e, "detail", "")) or "The numbers could not be read"}}
                for name in todo:
                    _keep_value(held[name], failure, FAILED_SECONDS)
                raise
            for name, (value, ttl) in done.items():
                _keep_value(held[name], value, ttl)
                out[name] = (value, False)
    return out


async def _once(hashes: dict[str, str], work: Work, wait: float | None = None) -> dict[str, tuple[dict | None, bool]]:
    """For each name its value, and whether someone else worked it out; (None, False) past the
    wait. `work(names, seconds left)` gives (value, seconds to keep it) for those of them this
    request holds the lock of, and what it raises goes on up to this request, and as a failure to
    those waiting. On the event loop, with no thread held while waiting: a hundred people waiting
    for a slow query must not take the engine API's threads from everybody else."""
    deadline = time.monotonic() + (WAIT_SECONDS if wait is None else wait)
    out: dict[str, tuple[dict | None, bool]] = {}
    left = dict(hashes)
    delay = 0.02
    while left:
        for name, value in (await run_in_threadpool(_values, left)).items():
            out[name] = (value, True)
            del left[name]
        if not left:
            break
        token = uuid.uuid4().hex
        held = await run_in_threadpool(lambda: {name: h for name, h in left.items() if _take(h, token)})
        if held:
            out.update(await run_in_threadpool(_worked_out, held, token, work, deadline))
            for name in held:
                left.pop(name, None)
            continue
        if time.monotonic() >= deadline:
            out.update(dict.fromkeys(left, (None, False)))
            break
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 0.5)
    return out


def _count_tiles(hits: int, misses: int) -> None:
    if hits:
        _count("tile_hits", hits)
    if misses:
        _count("tile_misses", misses)


def _as_answered(value: dict | None, by_another: bool) -> dict:
    """A tile as this request says it."""
    if value is None:
        return {"error": BUSY, "code": "error"}
    if "failed" in value:
        return {"error": value["failed"]["detail"], "code": "error"}
    tile = value["tile"]
    return {**tile, "cached": True} if by_another and "error" not in tile else tile


def _keep_for(tile: dict, seconds: int) -> float:
    """A number lasts until what it was worked out from ends, not `seconds` after a click on rows
    already old. A tile's error may be of the moment: only a dataset too large is not."""
    if "error" in tile:
        return seconds if tile.get("code") == "truncated" else FAILED_SECONDS
    age = _age(tile.get("computed_at"))
    return seconds if age is None else max(seconds - age, 1.0)


@dataclass
class _Asked:
    """A request for numbers, worked out: its tiles, what each reads, the refusals, which say
    everything of those tiles, the key of each of the others, and the clicks that count."""
    items: list
    needs: dict[str, list[str]]
    refusals: dict[str, dict]
    hashes: dict[str, str]
    clicks: list[dict]


def _asked(request: dict, seconds: int, fresh: str | None) -> _Asked:
    """Rights first: a tile refused has no key, and looks at nothing, and a click on it counts for
    nothing: neither it nor its values are in any key, nor reach a worker."""
    definition = request["definition"]
    report = rd.load(definition)
    only, refused, datasets = request.get("tiles"), request.get("refused") or {}, request.get("datasets") or {}
    items = [it for _, it in report.items() if only is None or it.id in only]
    needs = {it.id: rdata.used_datasets(report, [it]) for it in items}
    refusals = {it.id: stopped for it in items if (stopped := rtiles.refusal(needs[it.id], refused))}
    readable = [it for it in items if it.id not in refusals]
    usable = rtiles.clicks_of(report, request.get("clicks") or [], refused)
    reads = {it.id: rtiles.needing(report, it, needs[it.id], usable) for it in readable}

    cap = rtiles.max_rows()
    roots = {name: rows_hash(datasets.get(name), cap) for it in readable for name in reads[it.id]}
    generations = _generations(sorted(set(roots.values())), fresh)
    described = {d["name"]: {k: v for k, v in d.items() if k != "source"} for d in definition.get("datasets") or []}
    raw = {i.get("id"): i for i in definition["sections"]["body"]["items"]}
    states = (request.get("state") or {}).get("items") if isinstance(request.get("state"), dict) else None
    pages = request.get("pages") or {}
    hashes = {it.id: tile_hash(raw.get(it.id) or {}, {name: {"reads": roots[name], "generation": generations[roots[name]],
                                                             "described": described.get(name)} for name in reads[it.id]},
                               request.get("params") or {}, definition.get("parameters"), rtiles.page_of(pages.get(it.id)),
                               states.get(it.id) if isinstance(states, dict) else None, request.get("locale"),
                               definition.get("timezone"), request.get("title") or "", cap, seconds,
                               rtiles.click_marks(report, it, needs[it.id], usable))
              for it in readable}
    kept = [raw for raw in request.get("clicks") or [] if any(c.tile == raw.get("tile") for c in usable)]
    return _Asked(items, reads, refusals, hashes, kept)


async def numbers(request: dict, cache: dict, run: Callable[[dict, float], dict]) -> dict:
    """The answer of `tiles.compute` for `request` (the task's arguments). `cache`: the dashboard's
    `refresh_seconds`, and `fresh`, the token of a refresh claimed. `run(kwargs, seconds)` sends
    them to a worker and gives its result, or raises the HTTP failure."""
    seconds = lifetime(cache.get("refresh_seconds"))
    kwargs = {**request, "cache_seconds": seconds}
    try:  # parsing 40 tiles is not for the event loop
        asked = await run_in_threadpool(_asked, request, seconds, cache.get("fresh"))
    except rd.DefinitionError:
        return await run_in_threadpool(run, kwargs, WAIT_SECONDS)  # the worker says it, as it does without a cache

    def work(names: list[str], left: float) -> dict[str, tuple[dict, float]]:
        answer = run({**kwargs, "tiles": names, "refused": {}, "clicks": asked.clicks}, left)
        said = answer.get("truncated") or {}
        return {name: ({"tile": answer["tiles"][name], "truncated": {n: said[n] for n in asked.needs[name] if n in said}},
                       _keep_for(answer["tiles"][name], seconds)) for name in names}

    got = await _once(asked.hashes, work)
    hits = sum(1 for _, by_another in got.values() if by_another)
    await run_in_threadpool(_count_tiles, hits, len(got) - hits)
    answers, truncated = dict(asked.refusals), {}
    for name, (value, by_another) in got.items():
        answers[name] = _as_answered(value, by_another)
        truncated.update((value or {}).get("truncated") or {})
    return {"tiles": {it.id: answers[it.id] for it in asked.items}, "truncated": truncated}


async def choices(request: dict, cache: dict, run: Callable[[dict, float], dict]) -> dict:
    """The choices of a parameter, kept as a tile's numbers are, for what checks a value at every
    request and for the form at every opening. A failure is its status, as without a cache."""
    seconds = lifetime(cache.get("refresh_seconds"))
    definition, name = request["definition"], request["dataset"]
    spec = (request.get("datasets") or {}).get(name)
    root = rows_hash(spec, rtiles.max_rows())
    made = await run_in_threadpool(_generations, [root], cache.get("fresh"))  # Valkey: may wait for its timeout
    described = next(({k: v for k, v in d.items() if k != "source"} for d in definition.get("datasets") or []
                      if isinstance(d, dict) and d.get("name") == name), None)
    h = _hash("choices", VERSION, root, made[root], described, definition.get("parameters"), definition.get("timezone"),
              request.get("params"), request.get("value"), request.get("label"), request.get("limit"), request.get("locale"), seconds)

    def work(names: list[str], left: float) -> dict[str, tuple[dict, float]]:
        return {"choices": ({"answer": run({**request, "cache_seconds": seconds}, left)}, seconds)}

    value, _ = (await _once({"choices": h}, work))["choices"]
    if value is None:
        raise HTTPException(status_code=504, detail=BUSY)
    if "failed" in value:
        raise HTTPException(status_code=value["failed"]["status"], detail=value["failed"]["detail"])
    return value["answer"]


# ── on a worker, or here ─────────────────────────────────────────────────────
_here = threading.Semaphore(1)  # one at a time: the rows of a request in this process's memory


def _broker_is_valkey() -> bool:
    settings = get_settings()
    return settings.celery_broker_url == settings.redis.url


def on_worker(task_name: str, kwargs: dict, seconds: float) -> dict:
    """The task on a preview worker, as `on_preview_worker` does. Nobody can be told to do it when
    the broker does not answer: a dashboard keeps working, so it is done here, one at a time, for
    as long as the outage lasts. What the worker's memory limits are for does not hold here: this
    is only for an outage."""
    from kombu.exceptions import OperationalError

    from app.api.routes.tasks import on_preview_worker

    if not (_is_down() and _broker_is_valkey()):
        try:
            return on_preview_worker(task_name, kwargs, None, seconds=seconds, watch_worker=True)
        except (OperationalError, redis.RedisError, ConnectionError) as e:
            logger.warning("dashboards: no worker can be reached (%s): working here", e)
    from app.tasks import report_jobs  # noqa: F401 — the worker's: here it was never asked for
    from app.tasks.celery_app import celery_app

    with _here:
        return celery_app.tasks[task_name].run(**kwargs)
