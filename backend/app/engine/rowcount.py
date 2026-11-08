"""How many rows a step of a flow gives: the editor's and the Viewer's «First 100 of N rows».

A preview reads only the first rows of a step, so its `row_count` is the size of the
sample, never the step's. The real count is asked apart, after the rows are on screen,
and it is made as cheap as the step allows, in this order:

1. remembered: a count made before for the same step (the step cache's fingerprint, so
   the same engine, source and chain) is kept on Valkey as long as the step cache keeps
   its copies;
2. read: a parquet says how many rows it holds in its footer. When the chain after a
   parquet whose size is known only changes columns (select, rename, cast, a computed
   column, a sort…) the step has the same rows: the source's footer, or the footer of a
   step the cache holds, answers in a few milliseconds without reading any data. A
   `limit` after it is taken into account;
3. counted: the engine counts the chain, starting from the nearest step the cache
   holds, and stops one row past the cap (`Engine.count_rows`); the result is then
   remembered.

Past the cap a count says «more than `at_most` rows»: counting a join of billions of rows
to the last one costs more than the number is worth. A number read from a footer costs
nothing and is said as it is, however large.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.engine.cache import plan_hashes, source_fingerprint

logger = logging.getLogger(__name__)

# The operations that keep every row: they change columns, types or order only. Each
# engine follows the same semantics for them (an impossible cast is a NULL, never a
# dropped row), so the count after them is the count before them.
PRESERVING = frozenset({"select", "reorder", "drop", "rename", "cast", "sort", "fill_null", "compute"})

COUNT_PREFIX = "dataprep:stepcount:"
# the parquet footer: read with one request for most files (a footer is a few KB)
_TAIL_BYTES = 64 * 1024
_MAGIC = b"PAR1"

_client = None


def _redis():
    global _client
    if _client is None:
        import redis

        from app.core.config import get_settings

        _client = redis.Redis.from_url(get_settings().redis.url, decode_responses=True)
    return _client


def _ttl() -> int:
    from app.core.config import get_settings

    return int(get_settings().cache.ttl_seconds)


def step_hashes(engine_name: str, bucket: str, key: str, operations: list[dict[str, Any]]) -> list[str]:
    """The fingerprints of every step of the chain, as the step cache computes them."""
    return plan_hashes(source_fingerprint(engine_name, bucket, key), operations)


def count_key(engine_name: str, bucket: str, key: str, operations: list[dict[str, Any]]) -> str:
    """Where the count of the chain's last step is remembered."""
    hashes = step_hashes(engine_name, bucket, key, operations)
    return COUNT_PREFIX + (hashes[-1] if hashes else "src:" + source_fingerprint(engine_name, bucket, key))


def remembered(key: str, at_most: int, client=None) -> tuple[int, bool] | None:
    """(rows, over) remembered for `key`, or None. A count that stopped at a lower cap
    than the one asked for does not answer.

    Valkey that does not answer is a count not remembered: never an error."""
    try:
        raw = (client or _redis()).get(key)
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        doc = json.loads(raw)
        rows, over, cap = int(doc["rows"]), bool(doc["over"]), int(doc.get("cap") or 0)
    except (ValueError, KeyError, TypeError):
        return None
    if not over:
        return (rows, False)  # exact: said as it is, past the cap too
    return (at_most, True) if cap >= at_most else None


def remember(key: str, rows: int, over: bool, at_most: int, client=None) -> None:
    try:
        (client or _redis()).set(key, json.dumps({"rows": rows, "over": over, "cap": at_most}), ex=_ttl())
    except Exception:  # noqa: BLE001 — a courtesy: the next time it is counted again
        pass


def parquet_metadata(storage, bucket: str, key: str):
    """A parquet object's footer (pyarrow's FileMetaData: rows, schema, the row groups'
    statistics), read with one or two ranged reads of its last bytes, no data. None when
    the storage cannot read a range or the object is not a parquet file."""
    read_tail = getattr(storage, "read_tail", None)
    if read_tail is None:
        return None
    try:
        tail, size = read_tail(bucket, key, _TAIL_BYTES)
        if len(tail) < 12 or tail[-4:] != _MAGIC:
            return None
        footer_len = int.from_bytes(tail[-8:-4], "little")
        if footer_len + 8 > size:
            return None
        if footer_len + 8 > len(tail):
            tail, _ = read_tail(bucket, key, footer_len + 8)
        import pyarrow as pa
        import pyarrow.parquet as pq

        # the footer as a file of its own: the reader looks only at the end of it
        return pq.read_metadata(pa.BufferReader(_MAGIC + tail[-(footer_len + 8):]))
    except Exception as e:  # noqa: BLE001 — any doubt: count instead
        from app.engine.query_tag import was_interrupted

        if was_interrupted(e):
            raise
        logger.info("the footer of %s/%s was not read (%s)", bucket, key, e)
        return None


def parquet_rows(storage, bucket: str, key: str) -> int | None:
    """The rows a parquet object holds, read from its footer. None when it cannot be read:
    then the rows are counted."""
    meta = parquet_metadata(storage, bucket, key)
    return int(meta.num_rows) if meta is not None else None


def _after(rows: int, over: bool, tail: list[dict[str, Any]], at_most: int) -> tuple[int, bool]:
    """(rows, over) after operations that keep every row, and `limit`s. An exact count stays
    exact past the cap: the cap bounds the work of counting, and reading a footer is none."""
    for op in tail:
        if op.get("type") != "limit":
            continue
        try:
            n = int((op.get("params") or {}).get("n"))
        except (TypeError, ValueError):
            continue
        if n < 0:
            continue
        if not over:
            rows = min(rows, n)
        elif n <= at_most:  # more than the cap before, so more than n: exactly n
            rows, over = n, False
    return rows, over


def known(engine_name: str, bucket: str, key: str, operations: list[dict[str, Any]], at_most: int,
          storage, cache, client=None) -> tuple[int, bool, str] | None:
    """(rows, over, how) without counting, or None. `how`: «remembered» or «metadata».

    `cache` is the engine's StepCache index (only `has` and `object_key` are used) or None
    when the engine's step copies are no parquet files (BigQuery)."""
    hashes = step_hashes(engine_name, bucket, key, operations)
    final = count_key(engine_name, bucket, key, operations)
    got = remembered(final, at_most, client)
    if got is not None:
        return (*got, "remembered")
    # the first step from which everything after keeps the rows (or only cuts them)
    first = len(operations)
    while first > 0 and (operations[first - 1].get("type") in PRESERVING or operations[first - 1].get("type") == "limit"):
        first -= 1
    for k in range(len(operations), first - 1, -1):
        base: tuple[int, bool] | None = None
        if k == 0:
            n = parquet_rows(storage, bucket, key)
            base = (n, False) if n is not None else None
        else:
            base = remembered(COUNT_PREFIX + hashes[k - 1], at_most, client)
            if base is None and cache is not None and cache.has(hashes[k - 1]):
                n = parquet_rows(storage, cache.bucket, cache.object_key(hashes[k - 1]))
                base = (n, False) if n is not None else None
        if base is None:
            continue
        rows, over = _after(base[0], base[1], operations[k:], at_most)
        if k == len(operations) and k > 0:
            remember(final, rows, over, at_most, client)
        return rows, over, "metadata"
    return None


def count(engine, engine_name: str, bucket: str, key: str, operations: list[dict[str, Any]], at_most: int,
          use_cache: bool = True, client=None) -> tuple[int, bool]:
    """Counts the chain with its engine, up to `at_most` + 1, and remembers the answer."""
    from app.engine.base import DataSource

    n = engine.count_rows(DataSource(bucket=bucket, key=key), operations, at_most, use_cache=use_cache)
    rows, over = (at_most, True) if n > at_most else (n, False)
    remember(count_key(engine_name, bucket, key, operations), rows, over, at_most, client)
    return rows, over
