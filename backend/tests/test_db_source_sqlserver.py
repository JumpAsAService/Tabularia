"""SQL Server as a source: the query, the connection string and the Arrow types.

In T-SQL the user's SQL is not wrapped in `SELECT * FROM (…)` — a derived table can hold
neither a CTE nor an ORDER BY — but it is checked to be one read query: a batch could
carry a `; DELETE …`. The test against a real SQL Server is in test_sqlserver_live.py."""
import pyarrow as pa
import pytest

from app.ingest import db_source as ds
from app.ingest.converters import IngestError
from app.ingest.db_source import DbConnectionSpec, DbSourceError, DbSourceSpec, build_query


def _conn(schema="", **kw):
    return DbConnectionSpec(db_type="sqlserver", host="h", database="vendite", db_schema=schema, **kw)


@pytest.mark.parametrize("ref, schema, expected", [
    ("ordini", "", "SELECT * FROM [ordini]"),
    ("ordini", "dbo", "SELECT * FROM [dbo].[ordini]"),
    ("dbo.[conti 2024]", "", "SELECT * FROM [dbo].[conti 2024]"),
    ("dbo.a]b", "", "SELECT * FROM [dbo].[a]]b]"),
])
def test_table_in_brackets(ref, schema, expected):
    assert build_query(_conn(schema), DbSourceSpec(mode="table", ref=ref)) == expected


def test_the_query_goes_as_it_is_even_with_cte_and_order_by():
    for sql in ("WITH c AS (SELECT 1 AS a) SELECT a FROM c", "SELECT TOP 3 id FROM t ORDER BY id DESC"):
        assert build_query(_conn(), DbSourceSpec(mode="sql", ref=sql + ";")) == sql


def test_sort_keys_at_the_end_of_the_cte_or_outside_the_wrapper():
    cte = "WITH c AS (SELECT 1 AS a) SELECT a FROM c"
    assert build_query(_conn(), DbSourceSpec(mode="sql", ref=cte, sort_keys=["a"])) == cte + " ORDER BY [a]"
    assert build_query(_conn(), DbSourceSpec(mode="sql", ref="SELECT a FROM t", sort_keys=["a"])) == "SELECT * FROM (SELECT a FROM t) AS _q ORDER BY [a]"


@pytest.mark.parametrize("sql", ["DELETE FROM t", "SELECT 1 AS a; DROP TABLE t", "UPDATE t SET a = 1"])
def test_only_one_read_query(sql):
    with pytest.raises(DbSourceError, match="single SELECT"):
        build_query(_conn(), DbSourceSpec(mode="sql", ref=sql))


@pytest.mark.parametrize("sql", ["SELECT * INTO dbo.copia FROM t", "WITH c AS (SELECT 1 AS a) SELECT a INTO #t FROM c",
                                 "SELECT a INTO dbo.x FROM t UNION ALL SELECT b FROM u"])
def test_select_into_refused(sql):
    # a query for sqlglot, but SQL Server creates a table
    with pytest.raises(DbSourceError, match="SELECT … INTO creates a table"):
        build_query(_conn(), DbSourceSpec(mode="sql", ref=sql))


def test_connection_string(monkeypatch):
    # the driver is optional (the `sqlserver` extra): without it, nothing to test here
    mssql_python = pytest.importorskip("mssql_python")

    seen_ones = []
    monkeypatch.setattr(mssql_python, "connect", lambda s, **kw: seen_ones.append(s))
    ds._sqlserver_connect(_conn(username="u", password="p;w}d", extra={"tls": "trust"}))
    ds._sqlserver_connect(DbConnectionSpec(db_type="sqlserver", host="srv\\SQLEXPRESS", username="u", password="x", database="d"))
    assert seen_ones[0] == "Server={h,1433};UID={u};PWD={p;w}}d};Database={vendite};Encrypt=yes;TrustServerCertificate=yes"
    assert seen_ones[1].startswith("Server={srv\\SQLEXPRESS};") and seen_ones[1].endswith("Encrypt=yes;TrustServerCertificate=no")
    with pytest.raises(IngestError, match="TLS"):
        ds._sqlserver_connect(_conn(extra={"tls": "boh"}))


@pytest.mark.parametrize("arrow, expected", [
    (pa.decimal128(10, 0), pa.int64()),
    (pa.decimal128(18, 0), pa.int64()),
    (pa.decimal128(28, 0), pa.float64()),     # beyond 18 digits
    (pa.decimal128(19, 4), pa.float64()),     # money
    (pa.timestamp("us", tz="UTC"), pa.timestamp("us")),   # datetimeoffset, in UTC already
    (pa.uint8(), pa.uint8()),                 # tinyint: 0..255, unsigned
    (pa.date32(), pa.date32()),
])
def test_types_aligned_with_the_other_drivers(arrow, expected):
    assert ds._sqlserver_type(arrow) == expected


def test_port_and_table_listing():
    assert ds.DEFAULT_PORTS["sqlserver"] == 1433 and "INFORMATION_SCHEMA.TABLES" in ds._TABLES_QUERY["sqlserver"]
