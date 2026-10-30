"""SQL on a connection only reads: the `SELECT * FROM (…)` around it refuses a statement
that is not a query, but a function the query calls can still write. PostgreSQL and MySQL
run it in a read-only transaction, so that function fails too. A read-only database user
stays the barrier (docs/sql-scripts.md): this holds where one was not given."""
import os
from urllib.parse import unquote, urlsplit

import pytest

from app.ingest import db_source
from app.ingest.db_source import DbConnectionSpec, DbSourceError, count_rows, query_rows

URI = os.environ.get("POSTGRES_TEST_URI")
postgres = pytest.mark.skipif(not URI, reason="needs a PostgreSQL (POSTGRES_TEST_URI)")


def _spec() -> DbConnectionSpec:
    u = urlsplit(URI)
    return DbConnectionSpec(db_type="postgresql", host=u.hostname, port=u.port or 5432, username=unquote(u.username),
                            password=unquote(u.password or ""), database=u.path.lstrip("/"))


@pytest.fixture
def with_a_writing_function():
    import adbc_driver_postgresql.dbapi as pg

    c = pg.connect(URI, autocommit=True)
    cur = c.cursor()
    for statement in ("DROP TABLE IF EXISTS ro_t", "CREATE TABLE ro_t (a int)", "INSERT INTO ro_t VALUES (1), (2), (3)",
                      "CREATE OR REPLACE FUNCTION ro_wipe() RETURNS int LANGUAGE plpgsql AS "
                      "$$ BEGIN DELETE FROM ro_t; RETURN 1; END $$"):
        cur.execute(statement)

    def rows() -> int:
        cur.execute("SELECT count(*) FROM ro_t")
        return cur.fetchone()[0]

    yield rows
    cur.execute("DROP FUNCTION ro_wipe()")
    cur.execute("DROP TABLE ro_t")
    cur.close()
    c.close()


@postgres
def test_a_function_that_writes_fails_and_the_rows_stay(with_a_writing_function):
    with pytest.raises(DbSourceError) as e:
        query_rows(_spec(), "SELECT ro_wipe()", 5)
    assert "read-only" in str(e.value)
    assert with_a_writing_function() == 3


@postgres
def test_a_query_cannot_turn_the_transaction_back(with_a_writing_function):
    with pytest.raises(DbSourceError):
        query_rows(_spec(), "SELECT set_config('transaction_read_only', 'off', true), ro_wipe()", 5)
    assert with_a_writing_function() == 3


@postgres
def test_a_query_still_reads_and_counts(with_a_writing_function):
    df, more = query_rows(_spec(), "SELECT a FROM ro_t ORDER BY a", 5)
    assert df["a"].to_list() == [1, 2, 3] and not more
    assert count_rows(_spec(), "SELECT a FROM ro_t", 10) == 3


def test_mysql_opens_a_read_only_transaction_before_the_query(monkeypatch):
    said = []

    class Cursor:
        description = [("a", 3, None, None, None, None, None)]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, statement):
            said.append(statement)

        def fetchmany(self, _):
            return []

    class Connection:
        def cursor(self):
            return Cursor()

        def close(self):
            pass

    monkeypatch.setattr(db_source, "_mysql_connect", lambda conn, **_: Connection())
    list(db_source._batches_mysql(DbConnectionSpec(db_type="mysql", host="h", database="d"), "SELECT 1"))
    assert said == ["START TRANSACTION READ ONLY", "SELECT 1"]
