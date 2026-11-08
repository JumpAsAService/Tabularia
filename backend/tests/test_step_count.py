"""How many rows a step gives, for the editor's and the Viewer's «First 100 of N rows»
(app/engine/rowcount.py, count_task, POST /tasks/count, POST /tasks/preview/cancel).

Fixed here: the rows arrive first and the count is asked apart; a source, or a step the
cache holds, followed only by operations that keep every row is answered from the
parquet footer without reading data (a `limit` after it counted in); anything else is
counted by the engine up to the cap, «more than» past it, on every engine of the
catalog; a count is remembered under the step cache's fingerprint; «Cancel» knocks down
what holds a slot.
"""
import polars as pl
import pytest

from app.api import preview_slots
from app.api.models import CancelSlotRequest, CountRequest
from app.api.routes import tasks
from app.engine import rowcount
from app.engine.base import DataSource
from app.engine.cache import StepCache, plan_hashes
from app.tasks.jobs import count_task
from tests.conftest import BUCKET, drain_cache, upload_df
from tests.fakes import FakeRedis

N = 2_500
FILTER = {"type": "filter", "params": {"column": "region", "operator": "eq", "value": "North"}}  # 1,000 rows
RENAME = {"type": "rename", "params": {"mapping": {"amount": "value"}}}
CAST = {"type": "cast", "params": {"columns": {"amount": "float"}}}
COMPUTE = {"type": "compute", "params": {"columns": [{"name": "double", "expr": "amount * 2"}]}}
SORT = {"type": "sort", "params": {"by": ["amount"], "descending": True}}


def limit(n):
    return {"type": "limit", "params": {"n": n}}


@pytest.fixture
def orders(storage) -> DataSource:
    df = pl.DataFrame({"region": ["North", "South", "East", "West", "South"] * (N // 5), "amount": list(range(N))})
    return upload_df(storage, df, "datasets/step_count_orders.parquet")


@pytest.fixture
def valkey(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(rowcount, "_client", fake)
    monkeypatch.setattr(rowcount, "_ttl", lambda: 3600)
    return fake


def _engine(name, storage, fredis):
    cache = StepCache(storage, redis_client=fredis)
    if name == "polars":
        from app.engine.polars_engine import PolarsEngine

        return PolarsEngine(storage=storage, cache=cache)
    if name == "duckdb":
        from app.engine.duckdb_engine import DuckDBEngine

        return DuckDBEngine(storage=storage, cache=cache)
    from app.engine.chdb_engine import ChdbEngine

    return ChdbEngine(storage=storage, cache=cache)


ENGINES = ["polars", "duckdb", "chdb"]


# ── read, not counted ────────────────────────────────────────────────────────
def test_a_source_s_rows_come_from_its_footer(storage, orders, valkey):
    assert rowcount.parquet_rows(storage, orders.bucket, orders.key) == N
    assert rowcount.known("polars", orders.bucket, orders.key, [], 10_000, storage, None) == (N, False, "metadata")


@pytest.mark.parametrize("chain", [[RENAME], [CAST, COMPUTE, SORT], [SORT, RENAME, COMPUTE]])
def test_operations_that_keep_every_row_keep_the_footer_s_answer(storage, orders, valkey, chain):
    assert rowcount.known("duckdb", orders.bucket, orders.key, chain, 10_000, storage, None) == (N, False, "metadata")


def test_a_limit_after_them_is_counted_in(storage, orders, valkey):
    assert rowcount.known("duckdb", orders.bucket, orders.key, [limit(100), RENAME], 10_000, storage, None)[:2] == (100, False)
    assert rowcount.known("duckdb", orders.bucket, orders.key, [RENAME, limit(N * 2)], 10_000, storage, None)[:2] == (N, False)
    # a footer is exact past the cap too: the cap bounds counting, and nothing was counted
    assert rowcount.known("duckdb", orders.bucket, orders.key, [], 1_000, storage, None)[:2] == (N, False)
    assert rowcount.known("duckdb", orders.bucket, orders.key, [limit(700)], 1_000, storage, None)[:2] == (700, False)
    assert rowcount.known("duckdb", orders.bucket, orders.key, [limit(2_000)], 1_000, storage, None)[:2] == (2_000, False)


def test_a_limit_under_the_cap_after_a_count_past_it_is_exact(storage, orders, valkey, fredis):
    eng = _engine("duckdb", storage, fredis)
    assert rowcount.count(eng, "duckdb", orders.bucket, orders.key, [FILTER], 300) == (300, True)
    assert rowcount.known("duckdb", orders.bucket, orders.key, [FILTER, limit(50)], 300, storage, None)[:2] == (50, False)
    assert rowcount.known("duckdb", orders.bucket, orders.key, [FILTER, limit(400)], 300, storage, None)[:2] == (300, True)


def test_a_filter_is_not_guessed(storage, orders, valkey):
    assert rowcount.known("duckdb", orders.bucket, orders.key, [FILTER], 10_000, storage, None) is None
    assert rowcount.known("duckdb", orders.bucket, orders.key, [RENAME, FILTER, RENAME], 10_000, storage, None) is None


def test_a_step_the_cache_holds_answers_for_the_steps_after_it(storage, orders, valkey, fredis):
    eng = _engine("polars", storage, fredis)
    eng.preview(orders, [FILTER, RENAME], limit=10)  # copies [FILTER] into the cache
    drain_cache(eng)
    assert eng.cache.has(plan_hashes(eng._source_id(orders), [FILTER])[-1])
    got = rowcount.known("polars", orders.bucket, orders.key, [FILTER, RENAME], 10_000, storage, eng.cache)
    assert got == (N // 5, False, "metadata")


def test_a_storage_that_cannot_read_a_range_counts_instead(orders, valkey):
    class NoRanges:
        pass

    assert rowcount.parquet_rows(NoRanges(), orders.bucket, orders.key) is None
    assert rowcount.known("duckdb", orders.bucket, orders.key, [], 10_000, NoRanges(), None) is None


def test_a_file_that_is_no_parquet_is_counted(storage, valkey):
    storage.blobs[(BUCKET, "datasets/x.csv")] = b"a,b\n1,2\n"
    assert rowcount.parquet_rows(storage, BUCKET, "datasets/x.csv") is None


# ── counted ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name", ENGINES)
def test_a_chain_is_counted_and_remembered(name, storage, orders, valkey, fredis):
    eng = _engine(name, storage, fredis)
    assert rowcount.count(eng, name, orders.bucket, orders.key, [FILTER], 10_000) == (N // 5, False)
    assert rowcount.known(name, orders.bucket, orders.key, [FILTER], 10_000, storage, None) == (N // 5, False, "remembered")
    # the remembered count answers for the steps after it that keep the rows
    assert rowcount.known(name, orders.bucket, orders.key, [FILTER, RENAME], 10_000, storage, None)[:2] == (N // 5, False)


@pytest.mark.parametrize("name", ENGINES)
def test_past_the_cap_it_stops_and_says_more(name, storage, orders, valkey, fredis):
    eng = _engine(name, storage, fredis)
    assert rowcount.count(eng, name, orders.bucket, orders.key, [FILTER], 300) == (300, True)
    # a count that stopped at 300 does not answer for a higher cap
    assert rowcount.known(name, orders.bucket, orders.key, [FILTER], 5_000, storage, None) is None
    assert rowcount.known(name, orders.bucket, orders.key, [FILTER], 200, storage, None) == (200, True, "remembered")


@pytest.mark.parametrize("name", ["polars", "duckdb"])
def test_counting_writes_nothing_to_the_step_cache(name, storage, orders, valkey, fredis):
    eng = _engine(name, storage, fredis)
    before = set(storage.blobs)
    rowcount.count(eng, name, orders.bucket, orders.key, [FILTER, SORT], 10_000)
    assert eng.take_pending() == []
    assert set(storage.blobs) == before


def test_the_count_starts_from_a_cached_step(storage, orders, valkey, fredis, monkeypatch):
    """The source can be gone: the cached copy of [FILTER] is enough to count after it."""
    eng = _engine("duckdb", storage, fredis)
    eng.preview(orders, [FILTER, SORT], limit=10)
    drain_cache(eng)
    del storage.blobs[(orders.bucket, orders.key)]
    assert rowcount.count(eng, "duckdb", orders.bucket, orders.key, [FILTER, SORT, {"type": "unique", "params": {}}], 10_000) == (N // 5, False)


# ── the task and the routes ──────────────────────────────────────────────────
def test_the_task_answers_tagged(storage, orders, valkey, fredis, monkeypatch):
    from app.tasks import jobs

    monkeypatch.setattr(jobs, "get_engine", lambda name=None: _engine("polars", storage, fredis))
    out = count_task(orders.bucket, orders.key, [FILTER], 10_000, engine="polars")
    assert out == {"ok": True, "result": {"rows": N // 5, "over": False, "at_most": 10_000, "how": "counted"}}
    bad = count_task(orders.bucket, orders.key, [{"type": "filter", "params": {"column": "nope", "operator": "eq", "value": 1}}],
                     10_000, engine="polars")
    assert bad["ok"] is False and bad["error"] in ("unprocessable", "bad_request")


def test_the_route_answers_from_the_footer_without_the_worker(storage, orders, valkey, monkeypatch):
    import app.utils

    monkeypatch.setattr(app.utils, "get_storage_service", lambda: storage)
    monkeypatch.setattr(tasks, "on_preview_worker", lambda *a, **k: pytest.fail("the worker was asked"))
    body = CountRequest(bucket=orders.bucket, input_key=orders.key, operations=[RENAME], engine="duckdb")
    assert tasks.count_step(body) == {"rows": N, "over": False, "at_most": 10_000_000, "how": "metadata"}


def test_the_route_sends_the_rest_to_the_worker_with_its_slot_and_time(storage, orders, valkey, monkeypatch):
    import app.utils

    monkeypatch.setattr(app.utils, "get_storage_service", lambda: storage)
    seen = {}

    def worker(task, kwargs, slot, seconds=None, watch_worker=False):
        seen.update(task=task, slot=slot, seconds=seconds, kwargs=kwargs)
        return {"ok": True, "result": {"rows": 7, "over": False, "at_most": kwargs["at_most"], "how": "counted"}}

    monkeypatch.setattr(tasks, "on_preview_worker", worker)
    body = CountRequest(bucket=orders.bucket, input_key=orders.key, operations=[FILTER], engine="duckdb", slot="u1:ed-x-count", at_most=500)
    assert tasks.count_step(body)["rows"] == 7
    assert seen["task"] == "app.tasks.jobs.count_task" and seen["slot"] == "u1:ed-x-count"
    assert seen["seconds"] == tasks.COUNT_TIMEOUT_SECONDS and seen["kwargs"]["at_most"] == 500


def test_a_failed_count_says_why(storage, orders, valkey, monkeypatch):
    import app.utils
    from fastapi import HTTPException

    monkeypatch.setattr(app.utils, "get_storage_service", lambda: storage)
    monkeypatch.setattr(tasks, "on_preview_worker", lambda *a, **k: {"ok": False, "error": "superseded", "detail": "x"})
    with pytest.raises(HTTPException) as e:
        tasks.count_step(CountRequest(bucket=orders.bucket, input_key=orders.key, operations=[FILTER]))
    assert e.value.status_code == 409


def test_cancel_knocks_down_what_holds_the_slot(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(preview_slots, "_client", fake)
    revoked = []
    monkeypatch.setattr(tasks.celery_app.control, "revoke", lambda tid, **kw: revoked.append((tid, kw.get("signal"))))
    assert tasks.cancel_slot(CancelSlotRequest(slot="u1:ed-x-preview")) == {"cancelled": False}
    preview_slots.claim("u1:ed-x-preview", "task-1", 60)
    assert tasks.cancel_slot(CancelSlotRequest(slot="u1:ed-x-preview")) == {"cancelled": True}
    assert revoked == [("task-1", "SIGUSR1")]
    # the waiting request sees the slot is no longer its own
    assert preview_slots.owner("u1:ed-x-preview") not in (None, "task-1")
    # twice: nothing more to stop
    assert tasks.cancel_slot(CancelSlotRequest(slot="u1:ed-x-preview")) == {"cancelled": False}
