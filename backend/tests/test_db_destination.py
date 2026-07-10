"""Test delle parti pure delle destinazioni database (niente DB reale):
qualificazione dei nomi, DDL generata, mappa dei tipi, chunking delle righe."""
import pyarrow as pa
import pytest

from app.ingest.db_destination import (
    DbDestinationError,
    _create_table_sql,
    _post_statements,
    _qualified_table,
    _row_chunks,
    _sql_type,
)
from app.ingest.db_source import DbConnectionSpec


def _conn(db_type="postgresql", db_schema=""):
    return DbConnectionSpec(db_type=db_type, host="h", database="d", db_schema=db_schema)


# ── nomi qualificati ─────────────────────────────────────────────────────────
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
    assert "`quando` Nullable(DateTime64(6))" in sql
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
