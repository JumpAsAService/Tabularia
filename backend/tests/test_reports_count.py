"""How many rows a report's dataset gives, counted up to the cap (app/reports/count.py,
Engine.count_rows, db_source.count_rows, report_count_task, POST /reports/count).

Fixed here: the count stops one row past the cap and says «over» there, on every engine
of the catalog and on a connection; the chain counted is the dataset's (its filters, its
SQL); a connection's count is one query the database answers in its dialect, capped as
the read is, and no row travels; the task answers tagged; the route waits only as long as
it is told.
"""
import sqlite3

import polars as pl
import pytest

from app.engine.base import DataSource
from app.engine.cache import StepCache
from app.engine.exceptions import EngineError
from app.ingest import db_source
from app.ingest.db_source import DbConnectionSpec, DbSourceError, _dbapi_batches, count_query, count_rows
from app.reports import count as rcount
from app.reports.errors import ReportError
from app.tasks.report_jobs import report_count_task
from tests.conftest import BUCKET, upload_df
from tests.fakes import FakeRedis


# ── the engines of the catalog ───────────────────────────────────────────────
def _engine(name, storage):
    cache = StepCache(storage, redis_client=FakeRedis())
    if name == "polars":
        from app.engine.polars_engine import PolarsEngine

        return PolarsEngine(storage=storage, cache=cache)
    if name == "duckdb":
        from app.engine.duckdb_engine import DuckDBEngine

        return DuckDBEngine(storage=storage, cache=cache)
    from app.engine.chdb_engine import ChdbEngine

    return ChdbEngine(storage=storage, cache=cache)


@pytest.fixture
def orders(storage) -> DataSource:
    df = pl.DataFrame({
        "region": ["North", "South", "North", "East", "South", "North", "West"] * 30,  # 210 rows
        "amount": list(range(210)),
    })
    return upload_df(storage, df, "datasets/report_count_orders.parquet")


ENGINES = ["polars", "duckdb", "chdb"]


@pytest.mark.parametrize("name", ENGINES)
def test_the_rows_of_a_chain_counted_whole_under_the_cap(name, storage, orders):
    assert _engine(name, storage).count_rows(orders, [], 1000) == 210


@pytest.mark.parametrize("name", ENGINES)
def test_over_the_cap_the_count_stops_one_row_past_it(name, storage, orders):
    assert _engine(name, storage).count_rows(orders, [], 50) == 51
    assert _engine(name, storage).count_rows(orders, [], 210) == 210  # exactly the cap: not over


@pytest.mark.parametrize("name", ENGINES)
def test_the_chain_counted_is_the_datasets_with_its_filters(name, storage, orders):
    ops = [{"type": "filter", "params": {"column": "region", "operator": "eq", "value": "North"}}]
    assert _engine(name, storage).count_rows(orders, ops, 1000) == 90


@pytest.mark.parametrize("name", ENGINES)
def test_a_sql_on_the_catalog_aggregated_counts_its_groups(name, storage, orders):
    """The shape of a SQL dataset on the catalog (sql_scripts._catalog_chain): one `sql`
    step reading the datasources as tables; aggregated, it gives a row per group."""
    tables = {"ds_1": {"source": {"bucket": BUCKET, "key": orders.key}}}
    grouped = [{"type": "sql", "params": {"query": "SELECT region, SUM(amount) AS amount FROM ds_1 GROUP BY region",
                                          "tables": tables}}]
    every = [{"type": "sql", "params": {"query": "SELECT * FROM ds_1;", "tables": tables}}]
    engine = _engine(name, storage)
    assert engine.count_rows(orders, grouped, 1000) == 4
    assert engine.count_rows(orders, every, 100) == 101


@pytest.mark.parametrize("name", ["duckdb", "chdb"])
def test_a_sql_with_its_own_with_is_counted(name, storage, orders):
    """A query that starts with WITH: the SQL engines count it inside its own step."""
    tables = {"ds_1": {"source": {"bucket": BUCKET, "key": orders.key}}}
    query = "WITH big AS (SELECT * FROM ds_1 WHERE amount >= 200) SELECT region, amount FROM big ORDER BY amount"
    assert _engine(name, storage).count_rows(orders, [{"type": "sql", "params": {"query": query, "tables": tables}}],
                                             1000) == 10


def test_the_count_goes_inside_the_last_sql_step_or_after_it(storage, orders):
    """Inside on the SQL engines (a second `sql` step nested the first one's WITH in its
    own: ClickHouse does not resolve it); after it on Polars, whose SQL reads no WITH in a
    subquery."""
    from app.engine.base import Engine

    asked = []

    class _Sql(Engine):
        def preview(self, source, operations, limit=100, use_cache=True, sort_keys=None):
            asked.append((operations, limit, use_cache))
            from app.engine.base import PreviewResult

            return PreviewResult(columns=[], rows=[{"n": 7}], row_count=1, truncated=False)

        run = None

    sql = [{"type": "sql", "params": {"query": "SELECT * FROM ds_1; ", "tables": {"ds_1": {}}}}]
    assert _Sql().count_rows(orders, sql, 9) == 7
    ops, limit, use_cache = asked[-1]
    assert (limit, use_cache) == (1, False) and len(ops) == 1
    assert ops[0]["params"] == {"query": "SELECT COUNT(*) AS n FROM (SELECT * FROM (SELECT * FROM ds_1\n) AS _c LIMIT 10) AS _d",
                                "tables": {"ds_1": {}}}
    assert sql[0]["params"]["query"] == "SELECT * FROM ds_1; "  # the dataset's own chain is left as it was
    filters = [{"type": "filter", "params": {"column": "region", "operator": "eq", "value": "North"}}]
    _Sql().count_rows(orders, filters, 9)
    assert asked[-1][0] == filters + [{"type": "limit", "params": {"n": 10}},
                                      {"type": "sql", "params": {"query": "SELECT COUNT(*) AS n FROM self"}}]
    polars = _engine("polars", storage)
    assert polars.count_inside_sql is False and polars.count_rows(orders, [], 9) == 10


@pytest.mark.parametrize("name", ENGINES)
def test_a_chain_that_cannot_run_says_why(name, storage, orders):
    ops = [{"type": "select", "params": {"columns": ["nope"]}}]
    with pytest.raises(EngineError):
        _engine(name, storage).count_rows(orders, ops, 1000)


def test_duckdb_computes_only_the_rows_it_counts(storage, orders, monkeypatch):
    """The last `sql` step keeps a preview's limit: a count does not compute its whole
    result first (a SELECT * on a big table copied it all to disk)."""
    from app.engine import duckdb_ops

    seen = []
    real = duckdb_ops.op_sql

    def spy(rel, params, alias, ctx):
        seen.append(ctx.limit_output)
        return real(rel, params, alias, ctx)

    monkeypatch.setitem(duckdb_ops._REGISTRY, "sql", spy)
    tables = {"ds_1": {"source": {"bucket": BUCKET, "key": orders.key}}}
    ops = [{"type": "sql", "params": {"query": "SELECT * FROM ds_1", "tables": tables}}]
    assert _engine("duckdb", storage).count_rows(orders, ops, 20) == 21
    assert seen == [21]


# ── what the render's dataset holds ──────────────────────────────────────────
def test_the_count_of_a_dataset_says_rows_over_and_the_cap(storage, orders, monkeypatch):
    from app import engine as engines

    monkeypatch.setattr(engines, "get_engine", lambda name: _engine("polars", storage))
    spec = {"bucket": BUCKET, "input_key": orders.key, "operations": [], "engine": "polars"}
    assert rcount.count(spec, 1000) == {"rows": 210, "over": False, "max_rows": 1000}
    assert rcount.count(spec, 100) == {"rows": 100, "over": True, "max_rows": 100}


def test_rows_given_as_they_are_are_counted_as_they_are():
    spec = {"rows": [{"a": i} for i in range(5)], "columns": [{"name": "a", "type": "Int64"}]}
    assert rcount.count(spec, 5) == {"rows": 5, "over": False, "max_rows": 5}
    assert rcount.count(spec, 4) == {"rows": 4, "over": True, "max_rows": 4}


def test_a_dataset_the_engine_does_not_know_is_refused():
    with pytest.raises(ReportError) as e:
        rcount.count({"what": 1}, 10)
    assert e.value.tag == "bad_request"
    with pytest.raises(ReportError):
        rcount.count(None, 10)


def test_a_source_that_is_gone_is_not_found(monkeypatch):
    from app import engine as engines
    from app.engine.exceptions import SourceNotFoundError

    class _Gone:
        def count_rows(self, source, operations, at_most):
            raise SourceNotFoundError(source.bucket, source.key)

    monkeypatch.setattr(engines, "get_engine", lambda name: _Gone())
    with pytest.raises(ReportError) as e:
        rcount.count({"bucket": BUCKET, "input_key": "datasets/gone.parquet", "operations": []}, 10)
    assert e.value.tag == "not_found"


# ── a query on a connection ──────────────────────────────────────────────────
def _conn(db_type="postgresql") -> DbConnectionSpec:
    return DbConnectionSpec(db_type=db_type, host="db.local", database="warehouse")


class _Sqlite:
    """A driver over sqlite that runs the queries it gets on a table `t` of `n` rows."""

    def __init__(self, n):
        self.n, self.queries = n, []

    def __call__(self, conn, query):
        self.queries.append(query)
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE t (id INTEGER, region TEXT)")
        c.executemany("INSERT INTO t VALUES (?, ?)", [(i, "NS"[i % 2]) for i in range(self.n)])
        try:
            yield from _dbapi_batches(c.execute(query), batch_rows=1000)
        finally:
            c.close()


def test_the_database_counts_up_to_the_cap_and_no_row_travels(monkeypatch):
    driver = _Sqlite(50)
    monkeypatch.setitem(db_source._DRIVERS, "postgresql", driver)
    assert count_rows(_conn(), "SELECT * FROM t;", 10) == 11
    assert driver.queries == ["SELECT COUNT(*) FROM (SELECT * FROM (SELECT * FROM t) AS _q LIMIT 11) AS _c"]
    assert count_rows(_conn(), "SELECT * FROM t", 100) == 50
    assert count_rows(_conn(), "SELECT region, COUNT(*) AS n FROM t GROUP BY region", 100) == 2


@pytest.mark.parametrize("db_type, sql, expected", [
    ("mysql", "SELECT * FROM t", "SELECT COUNT(*) FROM (SELECT * FROM (SELECT * FROM t) AS _q LIMIT 6) AS _c"),
    ("clickhouse", "SELECT * FROM t", "SELECT COUNT(*) FROM (SELECT * FROM (SELECT * FROM t) AS _q LIMIT 6) AS _c"),
    ("oracle", "SELECT * FROM t", "SELECT COUNT(*) FROM (SELECT * FROM (SELECT * FROM t) q FETCH FIRST 6 ROWS ONLY) c"),
    # SQL Server: the SQL goes as it is, capped with TOP; a WITH in front of the count, an
    # ORDER BY that only orders dropped (a derived table cannot hold either)
    ("sqlserver", "SELECT id FROM t", "SELECT COUNT_BIG(*) FROM (SELECT TOP 6 id AS id FROM t) AS _c"),
    ("sqlserver", "WITH a AS (SELECT id FROM t) SELECT id FROM a ORDER BY id",
     "WITH a AS (SELECT id AS id FROM t) SELECT COUNT_BIG(*) FROM (SELECT TOP 6 id AS id FROM a) AS _c"),
    ("sqlserver", "SELECT TOP 3 id FROM t ORDER BY id", "SELECT COUNT_BIG(*) FROM (SELECT TOP 3 id AS id FROM t ORDER BY id) AS _c"),
])
def test_the_count_in_each_dialect(db_type, sql, expected):
    assert count_query(_conn(db_type), sql, 6) == expected


def test_a_sql_the_database_refuses_says_its_message(monkeypatch):
    def failing(conn, query):
        raise DbSourceError('PostgreSQL: relation "nope" does not exist (42P01)')
        yield  # a generator, as the drivers

    monkeypatch.setitem(db_source._DRIVERS, "postgresql", failing)
    with pytest.raises(ReportError) as e:
        rcount.count({"connection": _conn().model_dump(), "sql": "SELECT * FROM nope"}, 10)
    assert "does not exist" in e.value.message


# ── the task and the route ───────────────────────────────────────────────────
def test_the_task_answers_tagged(monkeypatch):
    monkeypatch.setitem(db_source._DRIVERS, "postgresql", _Sqlite(30))
    monkeypatch.setattr(rcount, "max_rows", lambda: 20)
    out = report_count_task({"connection": _conn().model_dump(), "sql": "SELECT * FROM t"})
    assert out == {"ok": True, "result": {"rows": 20, "over": True, "max_rows": 20}}
    out = report_count_task({"connection": _conn().model_dump(), "sql": "   "})
    assert out == {"ok": False, "error": "unprocessable", "detail": "The SQL query is empty"}


def test_the_cap_is_the_renders(monkeypatch):
    from app.core.config import get_settings

    assert rcount.max_rows() == get_settings().reports.max_rows == 3_000_000


def test_the_route_waits_only_as_long_as_it_is_told(monkeypatch):
    from fastapi import HTTPException

    from app.api.routes import reports as routes
    from app.api.routes import tasks

    got = {}

    def worker(name, kwargs, slot, seconds=None):
        got.update(name=name, kwargs=kwargs, slot=slot, seconds=seconds)
        return {"ok": True, "result": {"rows": 7, "over": False, "max_rows": 1_000_000}}

    monkeypatch.setattr(tasks, "on_preview_worker", worker)
    body = routes.ReportCount(dataset={"rows": []}, slot="u1:report:3:count", seconds=5)
    assert routes.count(body) == {"rows": 7, "over": False, "max_rows": 1_000_000}
    assert got == {"name": "app.tasks.report_jobs.report_count_task", "kwargs": {"dataset": {"rows": []}},
                   "slot": "u1:report:3:count", "seconds": 5}
    assert routes.ReportCount(dataset={}).seconds == 20  # a hint: not a preview's wait

    monkeypatch.setattr(tasks, "on_preview_worker",
                        lambda *a, **k: {"ok": False, "error": "unprocessable", "detail": "no such table"})
    with pytest.raises(HTTPException) as e:
        routes.count(routes.ReportCount(dataset={"rows": []}))
    assert e.value.status_code == 422 and e.value.detail == "no such table"


def test_the_wait_of_a_hint_is_shorter_than_a_previews(monkeypatch):
    """on_preview_worker with `seconds`: revoked past them (504), not past the preview's
    time."""
    import time

    from celery.exceptions import TimeoutError as CeleryTimeoutError
    from fastapi import HTTPException

    from app.api.routes import tasks

    revoked = []

    class _Pending:
        def get(self, timeout):
            time.sleep(timeout)
            raise CeleryTimeoutError()

        def revoke(self, terminate=False, **kw):
            revoked.append(terminate)

        def forget(self):
            pass

    monkeypatch.setattr(tasks.celery_app, "send_task", lambda *a, **k: _Pending())
    monkeypatch.setattr(tasks, "_PREVIEW_POLL_SECONDS", 0.01)
    started = time.monotonic()
    with pytest.raises(HTTPException) as e:
        tasks.on_preview_worker("app.tasks.report_jobs.report_count_task", {}, None, seconds=0.1)
    assert e.value.status_code == 504 and revoked == [True]
    assert time.monotonic() - started < 2
