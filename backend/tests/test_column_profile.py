"""A step's column profile, for the preview's header (app/engine/profile.py, profile_task,
POST /tasks/profile).

Fixed here: the same numbers on every engine of the catalog (nulls, a number's and a
date's min and max, a text's lengths and the distinct values, exactly); the nulls and the
numbers' and dates' range of a source, or of a cached step followed only by operations that
keep rows and values, come from the parquet footer, and never a text's range; a cast or a
filter after it is computed; past the sample the answer says it saw only the first rows;
a profile is remembered; a column's most frequent values come in order, a tie by value;
the sample is the first hundred
thousand rows and the route gives up after 20 seconds (the owner's decisions, 2026-11-08).
"""
import datetime as dt

import polars as pl
import pytest

from app.api.models import ProfileRequest
from app.api.routes import tasks
from app.engine import profile, rowcount
from app.engine.base import DataSource, Engine
from app.engine.cache import plan_hashes
from app.tasks.jobs import profile_task
from tests.conftest import drain_cache, upload_df
from tests.fakes import FakeRedis
from tests.test_step_count import _engine

N = 2_000
REGIONS = ["North", "North", "North", "North", "South", "South", "South", "East", "East", None]
CITIES = ["Forlì", "Città di Castello", "", None, "Bra"]
FILTER = {"type": "filter", "params": {"column": "id", "operator": "lt", "value": 1000}}
RENAME = {"type": "rename", "params": {"mapping": {"amount": "value", "region": "area"}}}
CAST = {"type": "cast", "params": {"columns": {"amount": "float"}}}
SELECT = {"type": "select", "params": {"columns": ["id", "region", "amount", "day"]}}
SORT = {"type": "sort", "params": {"by": ["id"], "descending": True}}
ENGINES = ["polars", "duckdb", "chdb"]


def _frame() -> pl.DataFrame:
    return pl.DataFrame({
        "id": list(range(N)),
        "region": [REGIONS[i % 10] for i in range(N)],
        "city": [CITIES[i % 5] for i in range(N)],
        "amount": [None if i % 10 == 3 else round(i * 1.5 - 700, 2) for i in range(N)],
        "day": [dt.date(2024, 1, 1) + dt.timedelta(days=i % 30) for i in range(N)],
        "at": [dt.datetime(2024, 3, 1, 8, 0) + dt.timedelta(minutes=i) for i in range(N)],
        "flag": [None if i % 7 == 0 else i % 2 == 0 for i in range(N)],
        "tags": [["a", "b"] if i % 4 else None for i in range(N)],
    })


def _columns(df: pl.DataFrame) -> list[dict[str, str]]:
    return [{"name": n, "dtype": str(t)} for n, t in df.schema.items()]


@pytest.fixture
def table(storage) -> DataSource:
    return upload_df(storage, _frame(), "datasets/profile_table.parquet")


@pytest.fixture
def valkey(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(rowcount, "_client", fake)
    monkeypatch.setattr(rowcount, "_ttl", lambda: 3600)
    return fake


def _profile(name, storage, fredis, src, ops, columns=None, use_cache=True):
    eng = _engine(name, storage, fredis)
    return profile.profile(eng, name, src.bucket, src.key, ops, columns or _columns(_frame()), storage,
                           use_cache=use_cache)


# ── the kinds and the plan ───────────────────────────────────────────────────
def test_what_each_type_is_profiled_for():
    assert profile.kind_of("Int64") == profile.kind_of("Decimal(precision=38, scale=2)") == "number"
    assert profile.kind_of("Datetime(time_unit='us', time_zone=None)") == profile.kind_of("Date") == "time"
    assert profile.kind_of("String") == "text" and profile.kind_of("Boolean") == "bool"
    assert profile.kind_of("List(String)") == profile.kind_of("Struct({'a': Int64})") == "other"
    specs = profile.plan([{"name": "a", "dtype": "Int64"}, {"name": "t", "dtype": "List(String)"}], {"a": {"nulls": 3, "range": True}})
    # the footer gave a's nulls and range: only its distinct values are asked; a list only its nulls
    assert [s[1:] for s in specs] == [("rows", None), ("distinct", "a"), ("count", "t")]


# ── the same numbers on every engine ─────────────────────────────────────────
@pytest.mark.parametrize("name", ENGINES)
def test_every_engine_says_the_same_numbers(name, storage, table, valkey, fredis):
    got = _profile(name, storage, fredis, table, [FILTER])  # a filter: nothing from a footer
    df = _frame().filter(pl.col("id") < 1000)
    assert got["rows"] == 1000 and got["sampled"] is False and got["footer"] is False and got["how"] == "computed"
    cols = got["columns"]
    for c in df.columns:
        # a ClickHouse array is never null (docs/engines/engine-differences.md): an empty one
        want = 0 if name == "chdb" and c == "tags" else df[c].null_count()
        assert cols[c]["nulls"] == want, c
        assert cols[c]["of"] == 1000
    assert (cols["id"]["min"], cols["id"]["max"]) == (0, 999)
    assert (cols["amount"]["min"], cols["amount"]["max"]) == (-700.0, 798.5)
    assert (cols["day"]["min"], cols["day"]["max"]) == ("2024-01-01", "2024-01-30")
    assert (cols["at"]["min"], cols["at"]["max"]) == ("2024-03-01T08:00:00", "2024-03-02T00:39:00")
    # a text: its length in characters, never an alphabetical range; an empty text is a value
    assert "min" not in cols["region"] and "max" not in cols["city"]
    assert (cols["city"]["min_len"], cols["city"]["max_len"]) == (0, 17)
    assert "min" not in cols["flag"] and "min" not in cols["tags"] and "distinct" not in cols["tags"]
    # the distinct values leave the nulls out, counted exactly
    assert got["distinct_exact"] is True
    assert cols["region"]["distinct"] == 3 and cols["city"]["distinct"] == 4 and cols["flag"]["distinct"] == 2
    assert cols["day"]["distinct"] == 30
    assert cols["id"]["distinct"] == 1000
    assert set(cols["id"]["whole"]) == {"nulls", "min", "max"}


@pytest.mark.parametrize("name", ENGINES)
def test_a_profile_writes_nothing_to_the_step_cache(name, storage, table, valkey, fredis):
    eng = _engine(name, storage, fredis)
    before = set(storage.blobs)
    profile.profile(eng, name, table.bucket, table.key, [FILTER, SORT], _columns(_frame()), storage)
    assert eng.take_pending() == []
    assert set(storage.blobs) == before


def test_the_sql_way_of_chdb_also_profiles_a_chain_ending_in_sql(storage, table, valkey, fredis):
    """chDB (and an external ClickHouse, BigQuery) aggregate through a last `sql` step: a
    chain that ends in one of its own has the aggregates inside it."""
    eng = _engine("chdb", storage, fredis)
    sql = {"type": "sql", "params": {"query": "SELECT id, region FROM self WHERE id < 10"}}
    row = Engine.profile_aggregates(eng, table, [sql], [("rows", "rows", None), ("n", "count", "region"),
                                                        ("lo", "min", "region")], 1000)
    assert (row["rows"], row["n"], row["lo"]) == (10, 9, "East")


# ── from the footer ──────────────────────────────────────────────────────────
def test_a_source_s_nulls_and_ranges_come_from_its_footer(storage, table, valkey, fredis):
    got = profile.from_footer("duckdb", table.bucket, table.key, [], _columns(_frame()), storage, None)
    assert got["rows"] == N and got["step"] == 0
    cols = got["columns"]
    assert cols["amount"] == {"nulls": 200, "range": True, "min": -700.0, "max": 2298.5}
    assert cols["day"]["range"] and (cols["day"]["min"], cols["day"]["max"]) == ("2024-01-01", "2024-01-30")
    # a text's range is never taken from a footer (writers may cut it), its nulls are
    assert cols["city"] == {"nulls": 400, "range": False, "min": None, "max": None}
    assert "tags" not in cols  # nested: no flat statistics


@pytest.mark.parametrize("chain, names", [
    ([RENAME], ["id", "area", "value"]),
    ([SELECT, SORT], ["id", "region", "amount"]),
    ([SORT, RENAME, {"type": "select", "params": {"columns": ["id", "area", "value"]}}], ["id", "area", "value"]),
])
def test_operations_that_keep_rows_and_values_keep_the_footer(storage, table, valkey, chain, names):
    cols = [{"name": names[0], "dtype": "Int64"}, {"name": names[1], "dtype": "String"}, {"name": names[2], "dtype": "Float64"}]
    got = profile.from_footer("duckdb", table.bucket, table.key, chain, cols, storage, None)
    assert got is not None and got["columns"][names[2]]["nulls"] == 200 and got["columns"][names[2]]["max"] == 2298.5
    assert got["columns"][names[1]]["nulls"] == 200 and got["columns"][names[1]]["range"] is False


@pytest.mark.parametrize("op", [CAST, FILTER, {"type": "limit", "params": {"n": 10}},
                                {"type": "compute", "params": {"columns": [{"name": "x", "expr": "amount * 2"}]}}])
def test_a_cast_a_compute_or_fewer_rows_read_no_footer(storage, table, valkey, op):
    assert profile.from_footer("duckdb", table.bucket, table.key, [op], _columns(_frame()), storage, None) is None


def test_a_cached_step_s_footer_answers_after_it(storage, table, valkey, fredis):
    eng = _engine("polars", storage, fredis)
    eng.preview(table, [FILTER, RENAME], limit=10)  # copies [FILTER] into the cache
    drain_cache(eng)
    assert eng.cache.has(plan_hashes(eng._source_id(table), [FILTER])[-1])
    cols = [{"name": "value", "dtype": "Float64"}, {"name": "area", "dtype": "String"}]
    got = profile.from_footer("polars", table.bucket, table.key, [FILTER, RENAME], cols, storage, eng.cache)
    assert got["step"] == 1 and got["rows"] == 1000
    assert got["columns"]["value"]["nulls"] == 100 and got["columns"]["value"]["max"] == 798.5


@pytest.mark.parametrize("name", ENGINES)
def test_a_source_s_profile_takes_the_footer_and_queries_the_rest(name, storage, table, valkey, fredis):
    got = _profile(name, storage, fredis, table, [])
    assert got["footer"] is True and got["total"] == N and got["sampled"] is False
    a = got["columns"]["amount"]
    assert (a["nulls"], a["min"], a["max"]) == (200, -700.0, 2298.5)
    assert set(a["whole"]) == {"nulls", "min", "max"} and a["distinct"] == 1800
    c = got["columns"]["city"]
    assert (c["nulls"], c["min_len"], c["max_len"]) == (400, 0, 17) and "min" not in c


# ── the sample ───────────────────────────────────────────────────────────────
def test_past_the_sample_it_says_it_saw_the_first_rows(storage, table, valkey, fredis, monkeypatch):
    monkeypatch.setattr(profile, "SAMPLE_ROWS", 500)
    got = _profile("duckdb", storage, fredis, table, [SORT])  # sort: the footer gives the nulls of every row
    assert got["rows"] == 500 and got["sampled"] is True and got["total"] == N
    assert got["columns"]["amount"]["whole"] == ["nulls", "min", "max"] and got["columns"]["amount"]["of"] == N
    # a text's lengths and every distinct count are of the first 500 rows only
    assert got["columns"]["city"]["whole"] == ["nulls"]
    filtered = _profile("duckdb", storage, fredis, table, [{"type": "filter", "params": {"column": "id", "operator": "ge", "value": 0}}])
    assert filtered["sampled"] is True and filtered["columns"]["amount"]["whole"] == [] and filtered["columns"]["amount"]["of"] == 500
    # a step whose rows are known to be the sample's is not sampled
    exact = _profile("duckdb", storage, fredis, table, [{"type": "limit", "params": {"n": 500}}])
    assert exact["total"] == 500 and exact["sampled"] is False


# ── remembered ───────────────────────────────────────────────────────────────
def test_a_profile_is_remembered_for_its_step_and_columns(storage, table, valkey, fredis, monkeypatch):
    first = _profile("duckdb", storage, fredis, table, [FILTER])
    monkeypatch.setattr(Engine, "profile_aggregates", lambda *a, **k: pytest.fail("computed again"))
    from app.engine.duckdb_engine import DuckDBEngine

    monkeypatch.setattr(DuckDBEngine, "profile_aggregates", lambda *a, **k: pytest.fail("computed again"))
    again = _profile("duckdb", storage, fredis, table, [FILTER])
    assert again["how"] == "remembered" and again["columns"] == first["columns"]
    # other columns asked (another step's shape): not the same answer
    k1 = profile.profile_key("duckdb", table.bucket, table.key, [FILTER], _columns(_frame()))
    k2 = profile.profile_key("duckdb", table.bucket, table.key, [FILTER], _columns(_frame())[:2])
    k3 = profile.profile_key("polars", table.bucket, table.key, [FILTER], _columns(_frame()))
    assert len({k1, k2, k3}) == 3


# ── the most frequent values ─────────────────────────────────────────────────
@pytest.mark.parametrize("name", ENGINES)
def test_the_most_frequent_values_in_order(name, storage, table, valkey, fredis):
    eng = _engine(name, storage, fredis)
    got = profile.top_values(eng, name, table.bucket, table.key, [], "region")
    assert got["values"] == [{"value": "North", "count": 800}, {"value": "South", "count": 600}, {"value": "East", "count": 400}]
    days = profile.top_values(eng, name, table.bucket, table.key, [FILTER], "day")["values"]
    # 1000 rows over 30 days: ten days hold 34 rows, a tie broken by the date
    assert [d["count"] for d in days] == [34] * 5 and [d["value"] for d in days] == [f"2024-01-0{i}" for i in range(1, 6)]
    assert profile.top_values(eng, name, table.bucket, table.key, [], "region")["how"] == "remembered"


# ── the task and the route ───────────────────────────────────────────────────
def test_the_task_answers_tagged(storage, table, valkey, fredis, monkeypatch):
    from app.tasks import jobs

    monkeypatch.setattr(jobs, "get_engine", lambda name=None: _engine("polars", storage, fredis))
    monkeypatch.setattr(jobs, "get_storage_service", lambda: storage)
    out = profile_task(table.bucket, table.key, [FILTER], _columns(_frame()), engine="polars")
    assert out["ok"] is True and out["result"]["rows"] == 1000
    top = profile_task(table.bucket, table.key, [FILTER], [], column="region", engine="polars")
    assert top["ok"] is True and top["result"]["values"][0] == {"value": "North", "count": 400}
    bad = profile_task(table.bucket, table.key, [{"type": "filter", "params": {"column": "nope", "operator": "eq", "value": 1}}],
                       _columns(_frame()), engine="polars")
    assert bad["ok"] is False and bad["error"] in ("unprocessable", "bad_request")


def test_the_route_sends_the_profile_to_the_worker_with_its_slot_and_time(storage, table, valkey, monkeypatch):
    seen = {}

    def worker(task, kwargs, slot, seconds=None, watch_worker=False):
        seen.update(task=task, slot=slot, seconds=seconds, kwargs=kwargs)
        return {"ok": True, "result": {"rows": 7, "columns": {}, "how": "computed"}}

    monkeypatch.setattr(tasks, "on_preview_worker", worker)
    body = ProfileRequest(bucket=table.bucket, input_key=table.key, operations=[FILTER], engine="duckdb",
                          slot="u1:ed-x-profile", columns=[{"name": "id", "dtype": "Int64"}])
    assert tasks.profile_step(body)["rows"] == 7
    assert seen["task"] == "app.tasks.jobs.profile_task" and seen["slot"] == "u1:ed-x-profile"
    assert seen["seconds"] == tasks.PROFILE_TIMEOUT_SECONDS and seen["kwargs"]["columns"] == [{"name": "id", "dtype": "Int64"}]


def test_the_route_answers_a_remembered_profile_without_the_worker(storage, table, valkey, monkeypatch):
    cols = [{"name": "id", "dtype": "Int64"}]
    profile.remember(profile.profile_key("duckdb", table.bucket, table.key, [], cols), {"rows": 3, "columns": {}})
    monkeypatch.setattr(tasks, "on_preview_worker", lambda *a, **k: pytest.fail("the worker was asked"))
    got = tasks.profile_step(ProfileRequest(bucket=table.bucket, input_key=table.key, engine="duckdb", columns=cols))
    assert got == {"rows": 3, "columns": {}, "how": "remembered"}


def test_a_failed_or_empty_profile_says_why(storage, table, valkey, monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(tasks, "on_preview_worker", lambda *a, **k: {"ok": False, "error": "superseded", "detail": "x"})
    with pytest.raises(HTTPException) as e:
        tasks.profile_step(ProfileRequest(bucket=table.bucket, input_key=table.key, columns=[{"name": "id"}]))
    assert e.value.status_code == 409
    with pytest.raises(HTTPException) as e:
        tasks.profile_step(ProfileRequest(bucket=table.bucket, input_key=table.key))
    assert e.value.status_code == 422


def test_a_duckdb_profile_stops_at_once_when_its_task_is_interrupted():
    """The task's interruption (SIGUSR1 → an exception in the main thread) used to reach a
    DuckDB query seconds later; the profile's query is interrupted at once."""
    import os
    import signal
    import threading
    import time

    import duckdb

    from app.engine.duckdb_engine import _interruptible

    class Stopped(Exception):
        pass

    def raise_stopped(signum, frame):
        raise Stopped

    con = duckdb.connect()
    previous = signal.signal(signal.SIGUSR1, raise_stopped)
    try:
        threading.Timer(0.3, lambda: os.kill(os.getpid(), signal.SIGUSR1)).start()
        t0 = time.monotonic()
        with pytest.raises(Stopped):
            _interruptible(con, lambda: con.sql("SELECT sum(a.range * b.range) FROM range(200000000) a, range(1000) b").fetchone())
        assert time.monotonic() - t0 < 2.0
    finally:
        signal.signal(signal.SIGUSR1, previous)
        con.close()


# ── the editor's footer-only profile: never a query ──────────────────────────
def _no_query(monkeypatch):
    from app.engine.duckdb_engine import DuckDBEngine

    monkeypatch.setattr(Engine, "profile_aggregates", lambda *a, **k: pytest.fail("a query was made"))
    monkeypatch.setattr(DuckDBEngine, "profile_aggregates", lambda *a, **k: pytest.fail("a query was made"))


def test_footer_only_says_a_source_s_nulls_and_ranges_without_a_query(storage, table, valkey, fredis, monkeypatch):
    _no_query(monkeypatch)
    eng = _engine("duckdb", storage, fredis)
    renamed = [{**c, "name": {"amount": "value", "region": "area"}.get(c["name"], c["name"])}
               for c in _columns(_frame())]
    got = profile.profile(eng, "duckdb", table.bucket, table.key, [RENAME], renamed, storage,
                          footer_only=True)
    assert got["partial"] and got["how"] == "footer" and got["rows"] == N and not got["sampled"]
    cols = got["columns"]
    assert cols["value"] == {"kind": "number", "nulls": 200, "of": N, "whole": ["nulls", "min", "max"],
                             "min": -700.0, "max": 2298.5}
    # a text: its nulls, never its range; nothing known of the distinct values
    assert cols["city"] == {"kind": "text", "nulls": 400, "of": N, "whole": ["nulls"]}
    assert "tags" not in cols and "distinct" not in cols["area"]
    # not remembered as the step's profile: the whole one is still made when asked
    assert profile.remembered(profile.profile_key("duckdb", table.bucket, table.key, [RENAME],
                                                  renamed)) is None


def test_footer_only_after_a_filter_says_nothing(storage, table, valkey, fredis, monkeypatch):
    _no_query(monkeypatch)
    eng = _engine("duckdb", storage, fredis)
    got = profile.profile(eng, "duckdb", table.bucket, table.key, [FILTER], _columns(_frame()), storage,
                          footer_only=True)
    assert got["partial"] and got["columns"] == {} and got["total"] is None and not got["footer"]


def test_footer_only_answers_a_whole_remembered_profile(storage, table, valkey, fredis, monkeypatch):
    whole = _profile("duckdb", storage, fredis, table, [FILTER])
    _no_query(monkeypatch)
    eng = _engine("duckdb", storage, fredis)
    got = profile.profile(eng, "duckdb", table.bucket, table.key, [FILTER], _columns(_frame()), storage,
                          footer_only=True)
    assert got["how"] == "remembered" and "partial" not in got and got["columns"] == whole["columns"]


def test_the_route_passes_footer_only_to_the_worker(storage, table, valkey, monkeypatch):
    seen = {}

    def worker(task, kwargs, slot, seconds=None, watch_worker=False):
        seen.update(kwargs=kwargs)
        return {"ok": True, "result": {"rows": 0, "columns": {}, "partial": True, "how": "footer"}}

    monkeypatch.setattr(tasks, "on_preview_worker", worker)
    body = ProfileRequest(bucket=table.bucket, input_key=table.key, operations=[FILTER], engine="duckdb",
                          footer_only=True, columns=[{"name": "id", "dtype": "Int64"}])
    assert tasks.profile_step(body)["partial"] is True
    assert seen["kwargs"]["footer_only"] is True
    # without it, the worker's kwargs stay as they were
    tasks.profile_step(ProfileRequest(bucket=table.bucket, input_key=table.key, operations=[FILTER],
                                      engine="duckdb", columns=[{"name": "id", "dtype": "Int64"}]))
    assert "footer_only" not in seen["kwargs"]


# ── the owner's prudence (2026-11-08) ────────────────────────────────────────
def test_the_sample_is_the_first_hundred_thousand_rows_and_the_route_gives_up_in_20_seconds():
    assert profile.SAMPLE_ROWS == 100_000
    assert tasks.PROFILE_TIMEOUT_SECONDS == 20.0
    # the distinct values counted exactly on every dialect, never estimated
    q = Engine._profile_quote
    assert profile.sql_aggregate("clickhouse", "distinct", "a", q) == "uniqExact(`a`)"
    assert profile.sql_aggregate("bigquery", "distinct", "a", q) == "COUNT(DISTINCT `a`)"


def test_a_text_asks_its_lengths_not_its_range():
    specs = profile.plan([{"name": "t", "dtype": "String"}], None)
    assert [s[1] for s in specs] == ["rows", "count", "distinct", "min_len", "max_len"]

