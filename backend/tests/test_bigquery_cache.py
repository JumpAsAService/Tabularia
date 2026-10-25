"""BigQuery's NATIVE step cache, without BigQuery: a fake client records the queries and
simulates the service (tables made by CTAS, description, expiry, NotFound). The contract
is checked: the intermediate steps materialized with an expiry and the original names,
reuse from the nearest ancestor, self-repair when the table is gone, no cache in the
Viewer, a graceful fallback without permissions.
"""
import io
import json
import re

import polars as pl

import pyarrow as pa
import pytest
from google.api_core.exceptions import Forbidden, NotFound

from app.core.config import BigQuerySettings
from app.engine.base import DataSource
from app.engine.bigquery_engine import BigQueryEngine, BigQueryStepCache
from tests.fakes import FakeRedis, FakeStorage

SAFE_SCHEMA = [("id", "INTEGER"), ("Ragione_Sociale", "STRING")]


class _Field:
    def __init__(self, name, t):
        self.name, self.field_type = name, t


class _Table:
    def __init__(self, ref, description):
        self.ref, self.description = ref, description
        self.schema = [_Field(n, t) for n, t in SAFE_SCHEMA]


class _Rows:
    total_rows = 2

    def to_arrow(self, create_bqstorage_client=False):
        return pa.table({"id": [1, 2], "Ragione_Sociale": ["a", "b"]})

    def to_arrow_iterable(self, bqstorage_client=None):
        yield from self.to_arrow().to_batches()

    def __iter__(self):
        return iter([])


class _Job:
    def __init__(self, dry_run):
        self.schema = [_Field(n, t) for n, t in SAFE_SCHEMA]
        self.total_bytes_billed = 0 if dry_run else 10
        self.total_bytes_processed = 0
        self.state = "DONE"
        self.job_id = "j"

    def result(self):
        return _Rows()


class FakeClient:
    def __init__(self, forbid_dataset=False):
        self.sql: list[tuple[str, bool]] = []
        self.tables: dict[str, _Table] = {}
        self.datasets: list[str] = []
        self.forbid_dataset = forbid_dataset

    def query(self, sql, job_config=None, location=None):
        dry = bool(getattr(job_config, "dry_run", False))
        self.sql.append((sql, dry))
        if not dry and sql.startswith("CREATE OR REPLACE TABLE"):
            ref = re.match(r"CREATE OR REPLACE TABLE `([^`]+)`", sql).group(1)
            desc = re.search(r"description = '((?:[^'\\]|\\.)*)'", sql).group(1).replace("\\'", "'")
            self.tables[ref] = _Table(ref, desc)
        return _Job(dry)

    def get_table(self, ref):
        if ref not in self.tables:
            raise NotFound(ref)
        return self.tables[ref]

    def delete_table(self, ref, not_found_ok=False):
        self.tables.pop(ref, None)

    def create_dataset(self, ds, exists_ok=False):
        if self.forbid_dataset:
            raise Forbidden("no")
        self.datasets.append(str(ds.dataset_id))

    def cancel_job(self, *a, **k):
        pass


class _RangeClient:
    """The minimal S3 client the engine uses to read the parquet's footer (HEAD + ranged
    GETs), over the FakeStorage's blobs."""

    def __init__(self, blobs):
        self.blobs = blobs

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.blobs[(Bucket, Key)])}

    def get_object(self, Bucket, Key, Range=None):
        data = self.blobs[(Bucket, Key)]
        a, b = (int(x) for x in Range.removeprefix("bytes=").split("-"))
        return {"Body": io.BytesIO(data[a : b + 1])}


class _Storage(FakeStorage):
    def __init__(self):
        super().__init__()
        self.client = _RangeClient(self.blobs)

    def object_exists(self, bucket, key):
        return self.exists(bucket, key)

    def bucket_location(self, bucket):
        return "europe-west8"


SRC = DataSource(bucket="data-prep", key="datasets/x.parquet")
FILTER = {"type": "filter", "params": {"column": "id", "operator": "gt", "value": 0}}
SORT = {"type": "sort", "params": {"by": ["Ragione Sociale"]}}
LIMIT = {"type": "limit", "params": {"n": 5}}


def _engine(forbid_dataset=False):
    fs = _Storage()
    # a REAL parquet with a column name BigQuery normalizes
    buf = io.BytesIO()
    pl.DataFrame({"id": [1, 2], "Ragione Sociale": ["a", "b"]}).write_parquet(buf)
    fs.blobs[("data-prep", "datasets/x.parquet")] = buf.getvalue()
    eng = BigQueryEngine(storage=fs, cfg=BigQuerySettings(project="p", credentials_b64="eA==", cache_dataset="tabularia_cache"))
    eng.cache = BigQueryStepCache(fs, eng, redis_client=FakeRedis())
    eng._client = FakeClient(forbid_dataset)
    eng._location = "europe-west8"
    return eng


def _ctas(client):
    return [s for s, dry in client.sql if not dry and s.startswith("CREATE OR REPLACE TABLE")]


def _real(client):
    return [s for s, dry in client.sql if not dry]


def _drain(eng):
    """What the deferred task does: materializes the steps left pending."""
    for src, ops in eng.take_pending():
        eng.materialize(src, ops)


def test_materializes_previous_steps_and_restarts_from_cache():
    eng = _engine()
    c = eng._client
    res = eng.preview(SRC, [FILTER, SORT], limit=10, use_cache=True)
    # 0) the preview does NOT wait for the CTAS: it leaves it pending and starts from the source
    assert _ctas(c) == [] and "FROM `_src1`" in _real(c)[-1]
    pending = eng.take_pending()
    assert len(pending) == 1 and pending[0][1] == [FILTER]
    assert eng.take_pending() == []  # drained once
    for src, ops in pending:
        eng.materialize(src, ops)
    # 1) the intermediate step ([filter]) is written to the cache with an expiry and the original names
    ctas = _ctas(c)
    assert len(ctas) == 1
    assert "`p.tabularia_cache.s_" in ctas[0] and "expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL" in ctas[0]
    desc = re.search(r"description = '((?:[^'\\]|\\.)*)'", ctas[0]).group(1)
    assert json.loads(desc.replace("\\'", "'")) == {"names": ["id", "Ragione Sociale"]}
    assert "FROM `_src1`" in ctas[0]  # the first materialization starts from the source
    assert [col.name for col in res.columns] == ["id", "Ragione Sociale"]  # ORIGINAL names
    assert c.datasets == ["tabularia_cache"]

    # 2) a second identical preview: starts from the cache table, nothing pending
    n = len(_real(c))
    res = eng.preview(SRC, [FILTER, SORT], limit=10, use_cache=True)
    final = _real(c)[-1]
    assert "tabularia_cache.s_" in final and "_src" not in final and "ORDER BY `Ragione_Sociale`" in final
    assert len(_real(c)) == n + 1 and eng.take_pending() == []
    assert [col.name for col in res.columns] == ["id", "Ragione Sociale"]

    # 3) one more step: the cache of [filter, sort] starts from that of [filter]
    eng.preview(SRC, [FILTER, SORT, LIMIT], limit=10, use_cache=True)
    _drain(eng)
    ctas = _ctas(c)
    assert len(ctas) == 2 and "tabularia_cache.s_" in ctas[1] and "_src" not in ctas[1]
    _drain(eng)
    assert len(_ctas(c)) == 2  # cached already: no CTAS repeated


def test_expired_table_is_forgotten_and_rebuilt():
    eng = _engine()
    c = eng._client
    eng.preview(SRC, [FILTER, SORT], limit=10, use_cache=True)
    _drain(eng)
    c.tables.clear()  # expired on the service, the index does not know
    eng.preview(SRC, [FILTER, SORT], limit=10, use_cache=True)
    assert "FROM `_src" in _real(c)[-1]  # the preview started again from the source
    _drain(eng)
    assert len(_ctas(c)) == 2 and "FROM `_src" in _ctas(c)[1]


def test_viewer_never_touches_the_cache():
    eng = _engine()
    eng.preview(SRC, [FILTER, SORT], limit=10, use_cache=False)
    assert _ctas(eng._client) == [] and eng._client.datasets == []
    assert "FROM `_src1`" in _real(eng._client)[-1]


def test_without_permissions_the_engine_degrades_to_no_cache():
    eng = _engine(forbid_dataset=True)
    res = eng.preview(SRC, [FILTER, SORT], limit=10, use_cache=True)
    assert res.row_count == 2 and _ctas(eng._client) == []
    assert eng._cache_ok is False and eng.take_pending() == []


def test_cache_dataset_empty_means_no_cache():
    eng = _engine()
    eng.cfg = BigQuerySettings(project="p", credentials_b64="eA==", cache_dataset="")
    eng.preview(SRC, [FILTER, SORT], limit=10, use_cache=True)
    assert _ctas(eng._client) == [] and eng.take_pending() == []


def test_evict_expired_drops_tables_and_index():
    eng = _engine()
    c = eng._client
    eng.preview(SRC, [FILTER, SORT], limit=10, use_cache=True)
    _drain(eng)
    (h,) = list(eng.cache.redis.smembers(eng.cache.index_set))
    assert eng.cache.table_ref(h) in c.tables
    eng.cache.redis.zadd(eng.cache.atime_zset, {h: 1.0})  # a very old access
    assert eng.cache.evict_expired(ttl_seconds=60) == 1
    assert c.tables == {} and not eng.cache.has(h)
    # an index in a space SEPARATE from the parquet engines'
    assert eng.cache.index_set.endswith(":bigquery")


def test_run_starts_from_cache_but_does_not_write_it():
    eng = _engine()
    c = eng._client
    eng.preview(SRC, [FILTER, SORT], limit=10, use_cache=True)
    _drain(eng)
    n = len(_ctas(c))
    out = eng.run(SRC, [FILTER, SORT], DataSource(bucket="data-prep", key="out/x.parquet"), use_cache=True)
    assert out.rows_written == 2 and len(_ctas(c)) == n
    assert "tabularia_cache.s_" in _real(c)[-1]


def test_preview_task_hands_pending_steps_to_the_deferred_task(monkeypatch):
    """The preview's task drains the pending materializations and queues them to the
    separate task, on the previews' queue: the answer does not wait for them."""
    from app.tasks import jobs

    eng = _engine()
    sent: list[dict] = []
    monkeypatch.setattr(jobs, "get_engine", lambda name=None: eng)
    monkeypatch.setattr(jobs.materialize_step_task, "apply_async",
                        lambda args, queue, priority: sent.append({"args": args, "queue": queue, "priority": priority}))
    out = jobs.preview_task.run(bucket="data-prep", input_key="datasets/x.parquet", operations=[FILTER, SORT], limit=10, engine="bigquery")
    assert out["ok"] and sent == [{"args": ["bigquery", "data-prep", "datasets/x.parquet", [FILTER]], "queue": "preview",
                                   "priority": jobs.CACHE_COPY_PRIORITY}]
    # and the deferred task really runs the materialization
    res = jobs.materialize_step_task.run("bigquery", "data-prep", "datasets/x.parquet", [FILTER])
    assert res == {"ok": True, "written": True} and len(_ctas(eng._client)) == 1
