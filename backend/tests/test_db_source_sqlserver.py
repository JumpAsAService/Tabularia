"""SQL Server come sorgente: la query, la stringa di connessione e i tipi Arrow.

In T-SQL la SQL dell'utente non si avvolge in `SELECT * FROM (…)` — una tabella
derivata non può contenere una CTE né un ORDER BY — ma si controlla che sia una
sola query di lettura: un batch potrebbe portare dietro `; DELETE …`. La prova
contro un SQL Server vero sta in test_sqlserver_live.py."""
import pyarrow as pa
import pytest

from app.ingest import db_source as ds
from app.ingest.db_source import DbConnectionSpec, DbSourceError, DbSourceSpec, build_query


def _conn(schema="", **kw):
    return DbConnectionSpec(db_type="sqlserver", host="h", database="vendite", db_schema=schema, **kw)


@pytest.mark.parametrize("ref, schema, atteso", [
    ("ordini", "", "SELECT * FROM [ordini]"),
    ("ordini", "dbo", "SELECT * FROM [dbo].[ordini]"),
    ("dbo.[conti 2024]", "", "SELECT * FROM [dbo].[conti 2024]"),
    ("dbo.a]b", "", "SELECT * FROM [dbo].[a]]b]"),
])
def test_tabella_fra_quadre(ref, schema, atteso):
    assert build_query(_conn(schema), DbSourceSpec(mode="table", ref=ref)) == atteso


def test_la_query_va_cosi_com_e_anche_con_cte_e_order_by():
    for sql in ("WITH c AS (SELECT 1 AS a) SELECT a FROM c", "SELECT TOP 3 id FROM t ORDER BY id DESC"):
        assert build_query(_conn(), DbSourceSpec(mode="sql", ref=sql + ";")) == sql


def test_chiavi_di_ordinamento_in_coda_alla_cte_o_fuori_dall_involucro():
    cte = "WITH c AS (SELECT 1 AS a) SELECT a FROM c"
    assert build_query(_conn(), DbSourceSpec(mode="sql", ref=cte, sort_keys=["a"])) == cte + " ORDER BY [a]"
    assert build_query(_conn(), DbSourceSpec(mode="sql", ref="SELECT a FROM t", sort_keys=["a"])) == "SELECT * FROM (SELECT a FROM t) AS _q ORDER BY [a]"


@pytest.mark.parametrize("sql", ["DELETE FROM t", "SELECT 1 AS a; DROP TABLE t", "UPDATE t SET a = 1"])
def test_solo_una_query_di_lettura(sql):
    with pytest.raises(DbSourceError, match="single SELECT"):
        build_query(_conn(), DbSourceSpec(mode="sql", ref=sql))


@pytest.mark.parametrize("sql", ["SELECT * INTO dbo.copia FROM t", "WITH c AS (SELECT 1 AS a) SELECT a INTO #t FROM c",
                                 "SELECT a INTO dbo.x FROM t UNION ALL SELECT b FROM u"])
def test_select_into_rifiutata(sql):
    # per sqlglot è una query, ma SQL Server crea una tabella
    with pytest.raises(DbSourceError, match="SELECT … INTO creates a table"):
        build_query(_conn(), DbSourceSpec(mode="sql", ref=sql))


def test_stringa_di_connessione(monkeypatch):
    # the driver is optional (the `sqlserver` extra): without it, nothing to test here
    mssql_python = pytest.importorskip("mssql_python")

    viste = []
    monkeypatch.setattr(mssql_python, "connect", lambda s, **kw: viste.append(s))
    ds._sqlserver_connect(_conn(username="u", password="p;w}d", extra={"tls": "trust"}))
    ds._sqlserver_connect(DbConnectionSpec(db_type="sqlserver", host="srv\\SQLEXPRESS", username="u", password="x", database="d"))
    assert viste[0] == "Server={h,1433};UID={u};PWD={p;w}}d};Database={vendite};Encrypt=yes;TrustServerCertificate=yes"
    assert viste[1].startswith("Server={srv\\SQLEXPRESS};") and viste[1].endswith("Encrypt=yes;TrustServerCertificate=no")
    with pytest.raises(DbSourceError, match="TLS"):
        ds._sqlserver_connect(_conn(extra={"tls": "boh"}))


@pytest.mark.parametrize("arrow, atteso", [
    (pa.decimal128(10, 0), pa.int64()),
    (pa.decimal128(18, 0), pa.int64()),
    (pa.decimal128(28, 0), pa.float64()),     # oltre 18 cifre
    (pa.decimal128(19, 4), pa.float64()),     # money
    (pa.timestamp("us", tz="UTC"), pa.timestamp("us")),   # datetimeoffset, già in UTC
    (pa.uint8(), pa.uint8()),                 # tinyint: 0..255, senza segno
    (pa.date32(), pa.date32()),
])
def test_tipi_allineati_agli_altri_driver(arrow, atteso):
    assert ds._sqlserver_type(arrow) == atteso


def test_porta_ed_elenco_tabelle():
    assert ds.DEFAULT_PORTS["sqlserver"] == 1433 and "INFORMATION_SCHEMA.TABLES" in ds._TABLES_QUERY["sqlserver"]
