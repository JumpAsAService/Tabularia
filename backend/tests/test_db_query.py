"""A SQL script run on a connection (db_source.query_rows, list_columns, db_query_task) and
exported whole (db_export_task).

Fixed here: the database is asked for one row more than shown, and the read stops
there; the connection closes as soon as the rows are read; the cap is written in each
dialect; a table's columns come from a read of one row; the task answers tagged, with
the database's message. The export asks the database for no more rows than the format's
cap and one, writes the file from a snapshot that does not stay, and refuses over the cap.
"""
import io
import sqlite3

import polars as pl
import pytest

from app.ingest import db_source
from app.ingest.db_source import DbConnectionSpec, DbSourceError, _dbapi_batches, list_columns, query_rows, with_row_cap
from app.core.config import get_settings
from app.tasks import jobs
from app.tasks.jobs import db_query_task


def _conn(db_type="postgresql") -> DbConnectionSpec:
    return DbConnectionSpec(db_type=db_type, host="db.local", database="warehouse")


class _Driver:
    """A driver over sqlite that keeps the queries it got and whether it was closed."""

    def __init__(self, rows):
        self.rows, self.queries, self.closed = rows, [], False

    def __call__(self, conn, query):
        self.queries.append(query)
        c = sqlite3.connect(":memory:")
        c.execute("CREATE TABLE t (id INTEGER, name TEXT)")
        c.executemany("INSERT INTO t VALUES (?, ?)", self.rows)
        try:
            yield from _dbapi_batches(c.execute("SELECT id, name FROM t"), batch_rows=3)
        finally:
            self.closed = True
            c.close()


def test_the_first_rows_and_one_more_tell_the_result_goes_on(monkeypatch):
    driver = _Driver([(i, f"n{i}") for i in range(20)])
    monkeypatch.setitem(db_source._DRIVERS, "postgresql", driver)
    df, truncated = query_rows(_conn(), "SELECT * FROM t;", 5)
    assert df.height == 5 and truncated
    assert df.columns == ["id", "name"]
    assert driver.queries == ["SELECT * FROM (SELECT * FROM t) AS _q LIMIT 6"]
    assert driver.closed  # stopped after the rows it needed


def test_a_short_result_is_whole(monkeypatch):
    monkeypatch.setitem(db_source._DRIVERS, "postgresql", _Driver([(1, "a"), (2, "b")]))
    df, truncated = query_rows(_conn(), "SELECT * FROM t", 5)
    assert df.to_dicts() == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}] and not truncated


@pytest.mark.parametrize("db_type, query, expected", [
    ("mysql", "SELECT * FROM (x) AS _q", "SELECT * FROM (x) AS _q LIMIT 6"),
    ("clickhouse", "SELECT * FROM (x) AS _q", "SELECT * FROM (x) AS _q LIMIT 6"),
    ("oracle", "SELECT * FROM (x) q", "SELECT * FROM (x) q FETCH FIRST 6 ROWS ONLY"),
    ("sqlserver", "SELECT * FROM (x) AS _q", "SELECT TOP 6 * FROM (x) AS _q"),
    # SQL Server's SQL with a CTE goes as it is: the reader stops instead
    ("sqlserver", "WITH a AS (SELECT 1 AS n) SELECT n FROM a", "WITH a AS (SELECT 1 AS n) SELECT n FROM a"),
])
def test_the_cap_in_each_dialect(db_type, query, expected):
    assert with_row_cap(_conn(db_type), query, 6) == expected


def test_a_tables_columns_from_one_row(monkeypatch):
    driver = _Driver([(1, "a"), (2, "b")])
    monkeypatch.setitem(db_source._DRIVERS, "postgresql", driver)
    assert list_columns(_conn(), "sales.orders") == [{"name": "id", "dtype": "Int64"}, {"name": "name", "dtype": "String"}]
    assert driver.queries == ['SELECT * FROM "sales"."orders" LIMIT 1']


def test_the_task_answers_with_the_rows(monkeypatch):
    monkeypatch.setitem(db_source._DRIVERS, "postgresql", _Driver([(1, "a")]))
    out = db_query_task(_conn().model_dump(), "SELECT * FROM t", 10)
    assert out["ok"] and out["result"]["rows"] == [{"id": 1, "name": "a"}]
    assert out["result"]["truncated"] is False and out["result"]["cache_state"] == "off"


def test_the_task_says_the_databases_message(monkeypatch):
    def failing(conn, query):
        raise DbSourceError('PostgreSQL: relation "nope" does not exist (42P01)')
        yield  # a generator, as the drivers

    monkeypatch.setitem(db_source._DRIVERS, "postgresql", failing)
    out = db_query_task(_conn().model_dump(), "SELECT * FROM nope", 10)
    assert out == {"ok": False, "error": "unprocessable", "detail": 'PostgreSQL: relation "nope" does not exist (42P01)'}
    assert db_query_task(_conn().model_dump(), "   ", 10)["detail"] == "The SQL query is empty"


@pytest.fixture
def exporter(monkeypatch, engine, storage):
    """db_export_task on the test storage, with the Polars engine of the tests."""
    monkeypatch.setattr(jobs, "get_engine", lambda name="polars": engine)
    monkeypatch.setattr(jobs, "get_storage_service", lambda: storage)
    return lambda **kw: jobs.db_export_task.apply(kwargs={"connection": _conn().model_dump(), "sql": "SELECT * FROM t", **kw},
                                                  task_id="t-1").get()


def _exports(storage):
    return {k: data for (_, k), data in storage.blobs.items()}


def test_the_whole_result_goes_to_a_file_and_the_snapshot_does_not_stay(monkeypatch, exporter, storage):
    driver = _Driver([(i, f"n{i}") for i in range(20)])
    monkeypatch.setitem(db_source._DRIVERS, "postgresql", driver)
    monkeypatch.setattr(get_settings().export, "max_rows", 100)
    out = exporter(filename="Ordini.csv")
    assert out["ok"] and out["key"] == "exports/t-1.csv" and out["filename"] == "Ordini.csv"
    assert list(_exports(storage)) == ["exports/t-1.csv"]  # the snapshot went
    assert pl.read_csv(io.BytesIO(_exports(storage)["exports/t-1.csv"])).height == 20
    assert driver.queries == ["SELECT * FROM (SELECT * FROM t) AS _q LIMIT 101"]  # the cap and one


def test_an_excel_file_and_its_cap(monkeypatch, exporter, storage):
    driver = _Driver([(1, "a"), (2, "b")])
    monkeypatch.setitem(db_source._DRIVERS, "postgresql", driver)
    out = exporter(format="xlsx")
    assert out["ok"] and out["filename"] == "export.xlsx"
    assert pl.read_excel(io.BytesIO(_exports(storage)["exports/t-1.xlsx"])).to_dicts() == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    assert driver.queries[-1].endswith("LIMIT 1048576")  # Excel's rows and one


def test_over_the_cap_the_export_says_so_and_leaves_nothing(monkeypatch, exporter, storage):
    monkeypatch.setitem(db_source._DRIVERS, "postgresql", _Driver([(i, "x") for i in range(10)]))
    monkeypatch.setattr(get_settings().export, "max_rows", 4)
    out = exporter()
    assert not out["ok"] and out["error"] == "bad_request" and "more than 4 rows" in out["detail"]
    assert _exports(storage) == {}


def test_the_databases_refusal_is_the_exports_answer(monkeypatch, exporter, storage):
    def refuses(conn, query):
        raise DbSourceError('PostgreSQL: relation "t" does not exist')
        yield

    monkeypatch.setitem(db_source._DRIVERS, "postgresql", refuses)
    out = exporter()
    assert out == {"ok": False, "error": "unprocessable", "detail": 'PostgreSQL: relation "t" does not exist'}
    assert _exports(storage) == {}
