"""Tests of the pure parts of the database destinations (no real database): qualifying
the names, the DDL generated, the type map, chunking the rows."""
import pyarrow as pa
import pytest

from app.ingest.db_destination import (
    DbDestinationError,
    DbDestinationSpec,
    _create_table_sql,
    _post_statements,
    _qualified_table,
    _row_chunks,
    _sql_type,
    _write_mysql,
)
from app.ingest.db_source import DbConnectionSpec


def _conn(db_type="postgresql", db_schema=""):
    return DbConnectionSpec(db_type=db_type, host="h", database="d", db_schema=db_schema)


# ── qualified names ─────────────────────────────────────────────────────────
def test_qualified_table_plain():
    q, parts = _qualified_table(_conn(), "vendite")
    assert q == '"vendite"' and parts == ["vendite"]


def test_qualified_table_uses_connection_schema():
    q, parts = _qualified_table(_conn(db_schema="analytics"), "vendite")
    assert q == '"analytics"."vendite"' and parts == ["analytics", "vendite"]


def test_qualified_table_explicit_schema_overrides():
    q, parts = _qualified_table(_conn(db_schema="analytics"), "public.vendite")
    assert q == '"public"."vendite"' and parts == ["public", "vendite"]


def test_qualified_table_mysql_backticks():
    q, _ = _qualified_table(_conn(db_type="mysql"), "vendite")
    assert q == "`vendite`"


def test_qualified_table_empty_rejected():
    with pytest.raises(DbDestinationError):
        _qualified_table(_conn(), "  ")


def test_qualified_table_quote_char_rejected():
    with pytest.raises(DbDestinationError):
        _qualified_table(_conn(), 'ven"dite')


# ── DDL e tipi ───────────────────────────────────────────────────────────────
SCHEMA = pa.schema(
    [
        pa.field("nome", pa.string()),
        pa.field("totale", pa.float64()),
        pa.field("n", pa.int64()),
        pa.field("quando", pa.timestamp("us")),
        pa.field("giorno", pa.date32()),
        pa.field("ok", pa.bool_()),
    ]
)


def test_create_sql_postgresql():
    sql = _create_table_sql(_conn(), '"t"', SCHEMA)
    assert sql.startswith('CREATE TABLE IF NOT EXISTS "t" (')
    assert '"nome" TEXT' in sql
    assert '"totale" DOUBLE PRECISION' in sql
    assert '"n" BIGINT' in sql
    assert '"quando" TIMESTAMP' in sql
    assert '"giorno" DATE' in sql
    assert '"ok" BOOLEAN' in sql


def test_create_sql_clickhouse_nullable_mergetree():
    sql = _create_table_sql(_conn(db_type="clickhouse"), "`t`", SCHEMA)
    assert sql.endswith("ENGINE = MergeTree ORDER BY tuple()")
    assert "`nome` Nullable(String)" in sql
    assert "`quando` Nullable(DateTime64(6, 'UTC'))" in sql  # naive timestamps are UTC
    assert "`ok` Nullable(Bool)" in sql


def test_create_sql_mysql_datetime():
    sql = _create_table_sql(_conn(db_type="mysql"), "`t`", SCHEMA)
    assert "`quando` DATETIME(6)" in sql and "`totale` DOUBLE" in sql


def test_create_sql_rejects_bad_column_name():
    bad = pa.schema([pa.field('col"cattiva', pa.string())])
    with pytest.raises(DbDestinationError):
        _create_table_sql(_conn(), '"t"', bad)


def test_sql_type_decimal_and_small_int():
    assert _sql_type("postgresql", pa.decimal128(10, 2)) == "DECIMAL(10, 2)"
    assert _sql_type("postgresql", pa.int32()) == "INTEGER"
    assert _sql_type("clickhouse", pa.int32()) == "Nullable(Int32)"
    assert _sql_type("trino", pa.string()) == "VARCHAR"


# ── post-SQL e chunking ──────────────────────────────────────────────────────
def test_post_statements_split_and_trim():
    assert _post_statements(" ANALYZE t ; ; CREATE INDEX i ON t(c);") == [
        "ANALYZE t",
        "CREATE INDEX i ON t(c)",
    ]
    assert _post_statements("") == []
    assert _post_statements(None) == []


def test_row_chunks_order_and_size():
    batch = pa.record_batch(
        [pa.array([1, 2, 3, 4, 5]), pa.array(["a", "b", "c", "d", "e"])],
        schema=pa.schema([pa.field("n", pa.int64()), pa.field("s", pa.string())]),
    )
    chunks = list(_row_chunks(batch, batch.schema, 2))
    assert [len(c) for c in chunks] == [2, 2, 1]
    assert chunks[0] == [(1, "a"), (2, "b")]
    assert chunks[2] == [(5, "e")]


# ── replace on MySQL/MariaDB must empty with DELETE (transactional), not TRUNCATE ──
class _RecordingCursor:
    def __init__(self, log): self.log = log
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def execute(self, sql, *a): self.log.append(sql)
    def executemany(self, sql, rows): self.log.append(("many", sql, len(rows)))


class _RecordingConn:
    def __init__(self, log): self.log = log
    def cursor(self): return _RecordingCursor(self.log)
    def commit(self): self.log.append("COMMIT")
    def close(self): pass


def _mysql_conn(**kw):
    return DbConnectionSpec(db_type="mysql", host="h", username="u", password="p", database="d", **kw)


def test_mysql_replace_uses_delete_not_truncate(monkeypatch):
    import pymysql
    log: list = []
    monkeypatch.setattr(pymysql, "connect", lambda **kw: _RecordingConn(log))

    conn = _mysql_conn()
    dest = DbDestinationSpec(table="vendite", mode="replace")
    schema = pa.schema([pa.field("a", pa.int64())])
    batch = pa.record_batch([pa.array([1, 2, 3])], schema=schema)
    qualified, parts = _qualified_table(conn, "vendite")

    rows = _write_mysql(conn, dest, qualified, parts, schema, iter([batch]))

    assert rows == 3
    stmts = [s if isinstance(s, str) else s[1] for s in log]
    joined = " ".join(stmts)
    assert "DELETE FROM" in joined, f"atteso DELETE, ottenuto: {log}"
    assert "TRUNCATE" not in joined, "TRUNCATE (DDL, auto-commit) non deve più essere usato"
    # the emptying comes before the INSERTs and everything commits once at the end
    assert log[-1] == "COMMIT"


def test_mysql_writes_in_utc(monkeypatch):
    """A customer TIMESTAMP column reads naive values in the session's zone: in the
    server's local zone it stored an instant one or two hours off."""
    import pymysql
    seen: dict = {}

    def connect(**kw):
        seen.update(kw)
        return _RecordingConn([])

    monkeypatch.setattr(pymysql, "connect", connect)
    conn = _mysql_conn()
    schema = pa.schema([pa.field("a", pa.int64())])
    qualified, parts = _qualified_table(conn, "vendite")
    _write_mysql(conn, DbDestinationSpec(table="vendite", mode="append"), qualified, parts, schema,
                 iter([pa.record_batch([pa.array([1])], schema=schema)]))
    assert seen["init_command"] == "SET time_zone = '+00:00'"


def test_mysql_append_does_not_empty(monkeypatch):
    import pymysql
    log: list = []
    monkeypatch.setattr(pymysql, "connect", lambda **kw: _RecordingConn(log))
    conn = _mysql_conn()
    dest = DbDestinationSpec(table="vendite", mode="append")
    schema = pa.schema([pa.field("a", pa.int64())])
    batch = pa.record_batch([pa.array([1])], schema=schema)
    qualified, parts = _qualified_table(conn, "vendite")
    _write_mysql(conn, dest, qualified, parts, schema, iter([batch]))
    joined = " ".join(s if isinstance(s, str) else s[1] for s in log)
    assert "DELETE FROM" not in joined and "TRUNCATE" not in joined


# ── Oracle and SQL Server: names by their own rules ────────────────────────────
def test_qualified_table_oracle_plain_names_are_uppercase():
    q, parts = _qualified_table(_conn(db_type="oracle", db_schema="sales"), "vendite")
    assert q == '"SALES"."VENDITE"' and parts == ["SALES", "VENDITE"]


def test_qualified_table_oracle_quoted_names_keep_their_case():
    q, parts = _qualified_table(_conn(db_type="oracle"), '"conti 2024"')
    assert q == '"conti 2024"' and parts == ["conti 2024"]


def test_qualified_table_sqlserver_brackets():
    q, parts = _qualified_table(_conn(db_type="sqlserver", db_schema="dbo"), "[conti 2024]")
    assert q == "[dbo].[conti 2024]" and parts == ["dbo", "conti 2024"]


def test_oracle_new_table_columns_take_the_uppercase_convention():
    from app.ingest.db_destination import _oracle_column_names

    schema = pa.schema([("regione", pa.string()), ("ricavo netto", pa.float64())])
    assert _oracle_column_names(schema, [], '"T"') == ["REGIONE", "ricavo netto"]


def test_oracle_existing_columns_are_matched_ignoring_case():
    from app.ingest.db_destination import _oracle_column_names

    schema = pa.schema([("amount", pa.float64()), ("customer", pa.string())])
    assert _oracle_column_names(schema, ["CUSTOMER", "Amount"], '"T"') == ["Amount", "CUSTOMER"]
    with pytest.raises(DbDestinationError, match="region"):
        _oracle_column_names(pa.schema([("region", pa.string())]), ["CUSTOMER"], '"T"')


def test_an_unknown_dialect_is_a_clear_error(storage):
    from app.ingest.db_destination import write_parquet_to_db

    conn = DbConnectionSpec.model_construct(db_type="sybase", host="h", database="d", db_schema="")
    with pytest.raises(DbDestinationError, match="cannot write into a sybase"):
        write_parquet_to_db(conn, DbDestinationSpec(table="t"), "b", "k", storage=storage)


# ── the output's types, plain for every writer ──────────────────────────────────
def test_plain_schema_makes_zones_naive_utc_dictionaries_plain_and_nulls_text():
    import datetime as dt

    from app.ingest.db_destination import _plain_batches, _plain_schema

    utc = pa.array([dt.datetime(2024, 3, 15, 12, 0)], pa.timestamp("us", tz="UTC"))
    batch = pa.record_batch(
        [utc, utc.cast(pa.timestamp("us", tz="Europe/Rome")), pa.array(["x"]).dictionary_encode(),
         pa.array([None], pa.null()), pa.array([1])],
        names=["utc", "rome", "cat", "empty", "n"],
    )
    schema = _plain_schema(batch.schema)
    assert schema.types == [pa.timestamp("us"), pa.timestamp("us"), pa.string(), pa.string(), pa.int64()]
    (out,) = _plain_batches(iter([batch]), schema)
    # the instant stays the same: the zone's label goes, never the value
    assert out.to_pylist() == [{"utc": dt.datetime(2024, 3, 15, 12, 0), "rome": dt.datetime(2024, 3, 15, 12, 0),
                                "cat": "x", "empty": None, "n": 1}]


def test_plain_schema_leaves_a_plain_schema_alone():
    from app.ingest.db_destination import _plain_schema

    assert _plain_schema(SCHEMA).equals(SCHEMA)


# ── Oracle: the post-SQL ───────────────────────────────────────────────────────
def test_oracle_post_sql_keeps_a_plsql_block_whole():
    from app.ingest.db_destination import _oracle_post_statements

    block = "BEGIN\n  DBMS_STATS.GATHER_TABLE_STATS(USER, 'VENDITE');\n  DBMS_MVIEW.REFRESH('MV_VENDITE');\nEND;"
    assert _oracle_post_statements(block) == [block]
    assert _oracle_post_statements(block + "\n/\n") == [block]  # SQL*Plus's terminator goes
    assert _oracle_post_statements("declare n number; begin n := 1; end;") == ["declare n number; begin n := 1; end;"]
    assert _oracle_post_statements("UPDATE t SET a = 1; DELETE FROM t WHERE b = 2;") == [
        "UPDATE t SET a = 1", "DELETE FROM t WHERE b = 2"]


class _OracleCursor:
    """Oracle's transaction state as python-oracledb shows it: DML opens a transaction,
    DDL commits it; a statement on a missing table fails."""

    def __init__(self, conn): self.conn = conn
    def execute(self, sql, *a):
        self.conn.log.append(sql)
        if "no_such_table" in sql:
            raise RuntimeError("ORA-00942: table or view does not exist")
        self.conn.transaction_in_progress = not sql.upper().startswith(("CREATE", "GRANT", "CALL"))


class _OracleConn:
    def __init__(self, in_progress=True):
        self.log: list = []
        self.transaction_in_progress = in_progress


@pytest.mark.parametrize("post_sql", [
    "CREATE INDEX ix ON t (id); UPDATE no_such_table SET x = 1",  # the DDL committed, then a failure
    "GRANT SELECT ON t TO reporting; UPDATE t SET x = 1; UPDATE no_such_table SET x = 1",  # a DML after the DDL
])
def test_oracle_post_sql_after_a_ddl_says_the_rows_are_written(post_sql):
    from app.ingest.db_destination import _oracle_post_sql

    c = _OracleConn()
    with pytest.raises(DbDestinationError, match="The data was written: 3 rows are in .*Do not run the flow again"):
        _oracle_post_sql(c, _OracleCursor(c), _conn("oracle"), DbDestinationSpec(table="t", post_sql=post_sql), '"T"', 3)


def test_oracle_post_sql_failing_without_a_ddl_is_a_plain_error():
    from app.ingest.db_destination import _oracle_post_sql

    c = _OracleConn()
    with pytest.raises(RuntimeError, match="ORA-00942"):  # rolled back by the writer: nothing changed
        _oracle_post_sql(c, _OracleCursor(c), _conn("oracle"),
                         DbDestinationSpec(table="t", post_sql="UPDATE t SET x = 1; UPDATE no_such_table SET x = 1"), '"T"', 3)


# ── SQL Server: one connection, one transaction, no temporary table ──────────────
def test_sqlserver_binary_columns_are_varbinary():
    from app.ingest.db_destination import _sqlserver_sql_type

    assert _sqlserver_sql_type(pa.large_binary()) == "VARBINARY(MAX)"
    assert _sqlserver_sql_type(pa.large_string()) == "NVARCHAR(MAX)"


class _TsqlCursor:
    def __init__(self, log): self.log = log
    def execute(self, sql, *a): self.log.append(sql)
    def fetchone(self): return (None,)
    def executemany(self, sql, rows): self.log.append(("many", sql, len(rows)))


class _TsqlConn:
    def __init__(self, log): self.log, self.autocommit = log, True
    def cursor(self): return _TsqlCursor(self.log)
    def commit(self): self.log.append(("COMMIT", self.autocommit))
    def rollback(self): self.log.append("ROLLBACK")
    def close(self): pass


def test_sqlserver_writes_in_one_transaction_without_a_global_temporary_table(monkeypatch):
    """The driver's bulk copy needs a GLOBAL temporary table (it runs on a connection of
    its own), which every login of the server can list, read and fill: the rows go
    straight into the table instead, inside the one transaction."""
    import app.ingest.db_destination as mod

    connections: list = []

    def connect(conn):
        connections.append(_TsqlConn(log))
        return connections[-1]

    log: list = []
    monkeypatch.setattr(mod, "_sqlserver_connect", connect)
    conn = _conn(db_type="sqlserver", db_schema="dbo")
    schema = pa.schema([pa.field("a", pa.int64()), pa.field("b", pa.string())])
    batch = pa.record_batch([pa.array(range(25_000)), pa.array(["x"] * 25_000)], schema=schema)
    qualified, parts = _qualified_table(conn, "vendite")
    rows = mod._write_sqlserver(conn, DbDestinationSpec(table="vendite", mode="replace", post_sql="EXEC sp_x"),
                                qualified, parts, schema, iter([batch]))

    assert rows == 25_000 and len(connections) == 1
    assert not any("#" in str(e) for e in log)  # no temporary table
    assert log[1] == "CREATE TABLE [dbo].[vendite] ([a] BIGINT, [b] NVARCHAR(MAX))"
    assert log[2] == "DELETE FROM [dbo].[vendite]"
    assert [e[2] for e in log if e[0] == "many"] == [10_000, 10_000, 5_000]
    assert log[-3][1] == "INSERT INTO [dbo].[vendite] ([a], [b]) VALUES (?, ?)"
    assert log[-2:] == ["EXEC sp_x", ("COMMIT", False)]  # one commit, outside autocommit
