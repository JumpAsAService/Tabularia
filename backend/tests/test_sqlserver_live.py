"""SQL Server come sorgente, contro un SQL Server VERO: si attiva con
SQLSERVER_TEST_HOST (e SQLSERVER_TEST_USER, SQLSERVER_TEST_PASSWORD,
SQLSERVER_TEST_DATABASE, SQLSERVER_TEST_TLS); senza, si salta. Va bene il container
ufficiale (edizione Developer):

  docker run -d --name mssqltest -e ACCEPT_EULA=Y -e MSSQL_PID=Developer \\
    -e MSSQL_SA_PASSWORD=… mcr.microsoft.com/mssql/server:2022-latest

L'utente deve poter creare tabelle: il test crea le sue (zzt_*) e le toglie."""
import os
import tempfile
import uuid

import polars as pl
import pyarrow.parquet as pq
import pytest

from app.ingest.db_source import DbConnectionSpec, DbSourceError, DbSourceSpec, ingest_db_to_parquet, list_tables
from app.ingest.db_source import test_connection as prova_connessione  # non «test_»: pytest la raccoglierebbe

HOST = os.environ.get("SQLSERVER_TEST_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="serve un SQL Server (SQLSERVER_TEST_HOST)")
P = f"zzt_{uuid.uuid4().hex[:6]}"


def _conn(**kw) -> DbConnectionSpec:
    base = dict(db_type="sqlserver", host=HOST, port=int(os.environ.get("SQLSERVER_TEST_PORT", "1433")),
                username=os.environ.get("SQLSERVER_TEST_USER", "sa"), password=os.environ.get("SQLSERVER_TEST_PASSWORD", ""),
                database=os.environ.get("SQLSERVER_TEST_DATABASE", "master"), extra={"tls": os.environ.get("SQLSERVER_TEST_TLS", "trust")})
    base.update(kw)
    return DbConnectionSpec(**base)


@pytest.fixture(scope="module")
def tabelle():
    from app.ingest.db_source import _sqlserver_connect

    db = _sqlserver_connect(_conn())
    db.autocommit = True
    cur = db.cursor()
    cur.execute(f"""CREATE TABLE dbo.{P}_tipi (id INT, piccolo TINYINT, d_intero DECIMAL(10,0), importo DECIMAL(10,2), soldi MONEY,
        vero BIT, ntesto NVARCHAR(100), lungo NVARCHAR(MAX), giorno DATE, quando2 DATETIME2(7), con_fuso DATETIMEOFFSET, guid UNIQUEIDENTIFIER, bin VARBINARY(8))""")
    cur.execute(f"""INSERT INTO dbo.{P}_tipi VALUES (1, 255, 1234567890, 1234.56, 12.5, 1, N'Straße 日本', REPLICATE(CAST(N'y' AS NVARCHAR(MAX)), 8000),
        '2024-03-15', '2024-03-15T13:45:30.1234567', '2024-03-15T13:45:30+02:00', 'A1B2C3D4-0000-0000-0000-000000000001', 0x00FF10)""")
    cur.execute(f"INSERT INTO dbo.{P}_tipi (id) VALUES (2)")
    cur.execute(f"CREATE TABLE dbo.{P}_varianti (id INT, v SQL_VARIANT)")
    cur.execute(f"INSERT INTO dbo.{P}_varianti VALUES (1, 42)")
    cur.execute(f"CREATE TABLE dbo.[{P} conti] (id INT)")
    cur.execute(f"INSERT INTO dbo.[{P} conti] VALUES (7)")
    cur.execute(f"""SELECT TOP (250000) ROW_NUMBER() OVER (ORDER BY (SELECT NULL)) AS id INTO dbo.{P}_grande
        FROM sys.all_objects a CROSS JOIN sys.all_objects b""")
    yield
    for t in (f"dbo.{P}_tipi", f"dbo.{P}_varianti", f"dbo.[{P} conti]", f"dbo.{P}_grande"):
        cur.execute(f"DROP TABLE {t}")
    db.close()


def _importa(storage, source, **kw):
    r = ingest_db_to_parquet(_conn(**kw), source, "b", "k.parquet", storage=storage)
    fd, p = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    storage.download_file("b", "k.parquet", p)
    return r, p


def test_connessione_ed_elenco(tabelle):
    prova_connessione(_conn())
    nomi = list_tables(_conn())
    assert f"dbo.{P}_tipi" in nomi and f"dbo.[{P} conti]" in nomi


def test_tipi_e_valori(tabelle, storage):
    _, p = _importa(storage, DbSourceSpec(mode="table", ref=f"dbo.{P}_tipi", sort_keys=["id"]))
    df = pl.read_parquet(p)
    assert dict(zip(df.columns, map(str, df.dtypes))) == {
        "id": "Int32", "piccolo": "UInt8", "d_intero": "Int64", "importo": "Float64", "soldi": "Float64", "vero": "Boolean",
        "ntesto": "String", "lungo": "String", "giorno": "Date", "quando2": "Datetime(time_unit='us', time_zone=None)",
        "con_fuso": "Datetime(time_unit='us', time_zone=None)", "guid": "String", "bin": "Binary",
    }
    riga = df.row(0, named=True)
    assert riga["piccolo"] == 255 and riga["ntesto"] == "Straße 日本" and len(riga["lungo"]) == 8000
    assert str(riga["con_fuso"]) == "2024-03-15 11:45:30" and riga["bin"] == b"\x00\xff\x10"   # +02:00 → UTC
    assert all(v is None for k, v in df.row(1, named=True).items() if k != "id")


def test_streaming_a_row_group_pieni(tabelle, storage, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.get_settings().ingest, "parquet_row_group_rows", 100_000)
    r, p = _importa(storage, DbSourceSpec(mode="table", ref=f"dbo.{P}_grande"))
    md = pq.ParquetFile(p).metadata
    assert r["rows_written"] == 250_000
    assert [md.row_group(i).num_rows for i in range(md.num_row_groups)] == [100_000, 100_000, 50_000]


def test_cte_ordinata_e_nome_fra_quadre(tabelle, storage):
    q = f"WITH c AS (SELECT id FROM dbo.{P}_tipi UNION ALL SELECT 0) SELECT * FROM c"
    _, p = _importa(storage, DbSourceSpec(mode="sql", ref=q, sort_keys=["id"]))
    assert pl.read_parquet(p)["id"].to_list() == [0, 1, 2]
    _, p = _importa(storage, DbSourceSpec(mode="table", ref=f"dbo.[{P} conti]"))
    assert pl.read_parquet(p).to_dicts() == [{"id": 7}]


def test_rifiuti_ed_errori_parlanti(tabelle, storage):
    with pytest.raises(DbSourceError, match=r"column v .*sql_variant"):
        _importa(storage, DbSourceSpec(mode="table", ref=f"dbo.{P}_varianti"))
    with pytest.raises(DbSourceError, match="single SELECT"):
        _importa(storage, DbSourceSpec(mode="sql", ref=f"SELECT 1 AS a; DROP TABLE dbo.{P}_tipi"))
    with pytest.raises(DbSourceError, match="Invalid object name"):
        _importa(storage, DbSourceSpec(mode="table", ref="dbo.zzt_non_esiste"))
    with pytest.raises(DbSourceError, match="Login failed.*username, password and database"):
        _importa(storage, DbSourceSpec(mode="table", ref=f"dbo.{P}_tipi"), password="sbagliata")
