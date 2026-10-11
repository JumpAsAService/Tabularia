"""Oracle come sorgente, contro un Oracle VERO: si attiva con ORACLE_TEST_HOST (e
ORACLE_TEST_USER, ORACLE_TEST_PASSWORD, ORACLE_TEST_SERVICE), come i test di
ClickHouse con chtest; senza, si salta. Va bene un Oracle Free in container:

  docker run -d --name oracletest -e ORACLE_PASSWORD=… -e APP_USER=tabularia \\
    -e APP_USER_PASSWORD=… gvenzl/oracle-free:23-slim-faststart

Il test crea tabelle sue (ZZT_*), importa con la funzione vera e le toglie."""
import os
import tempfile
import uuid

import polars as pl
import pyarrow.parquet as pq
import pytest

from app.ingest.db_source import (
    DbConnectionSpec, DbSourceError, DbSourceSpec, ingest_db_to_parquet, list_tables,
)
from app.ingest.db_source import test_connection as prova_connessione  # non «test_»: pytest la raccoglierebbe

HOST = os.environ.get("ORACLE_TEST_HOST")
pytestmark = pytest.mark.skipif(not HOST, reason="serve un Oracle (ORACLE_TEST_HOST)")
P = f"ZZT_{uuid.uuid4().hex[:6].upper()}"


def _conn(**kw) -> DbConnectionSpec:
    base = dict(db_type="oracle", host=HOST, port=int(os.environ.get("ORACLE_TEST_PORT", "1521")),
                username=os.environ.get("ORACLE_TEST_USER", "tabularia"), password=os.environ.get("ORACLE_TEST_PASSWORD", ""),
                database=os.environ.get("ORACLE_TEST_SERVICE", "FREEPDB1"))
    base.update(kw)
    return DbConnectionSpec(**base)


@pytest.fixture(scope="module")
def tabelle():
    import oracledb

    c = _conn()
    db = oracledb.connect(user=c.username, password=c.resolve_password(), host=c.host, port=c.port_or_default, service_name=c.database)
    cur = db.cursor()
    cur.execute(f"""CREATE TABLE {P}_TIPI (id NUMBER(10,0), n_libero NUMBER, importo NUMBER(10,2), testo VARCHAR2(100),
        lungo CLOB, grezzo RAW(8), giorno DATE, ts9 TIMESTAMP(9), ts_fuso TIMESTAMP WITH TIME ZONE, vero BOOLEAN)""")
    cur.execute(f"""INSERT INTO {P}_TIPI VALUES (1, 12345.678, 1234.56, 'Caffè «già» 日本', TO_CLOB(RPAD('x', 4000, 'y')) || TO_CLOB(RPAD('z', 4000, 'z')),
        HEXTORAW('00FF10'), TO_DATE('2024-03-15 13:45:30','YYYY-MM-DD HH24:MI:SS'), TIMESTAMP '2024-03-15 13:45:30.123456789',
        TIMESTAMP '2024-03-15 13:45:30 +02:00', TRUE)""")
    cur.execute(f"INSERT INTO {P}_TIPI (id) VALUES (2)")
    cur.execute(f"CREATE TABLE {P}_RIFIUTI (id NUMBER(5), durata INTERVAL DAY TO SECOND)")
    cur.execute(f'CREATE TABLE "{P.lower()} conti" (id NUMBER(5))')
    cur.execute(f'INSERT INTO "{P.lower()} conti" VALUES (7)')
    cur.execute(f"""CREATE TABLE {P}_GRANDE AS SELECT (a.n - 1) * 1000 + b.n AS id, MOD(b.n, 7) AS gruppo
        FROM (SELECT LEVEL n FROM dual CONNECT BY LEVEL <= 250) a CROSS JOIN (SELECT LEVEL n FROM dual CONNECT BY LEVEL <= 1000) b""")
    db.commit()
    yield
    for t in (f"{P}_TIPI", f"{P}_RIFIUTI", f'"{P.lower()} conti"', f"{P}_GRANDE"):
        cur.execute(f"DROP TABLE {t} PURGE")
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
    utente = _conn().username.upper()
    assert f"{utente}.{P}_TIPI" in nomi and f'{utente}."{P.lower()} conti"' in nomi


def test_tipi_e_valori(tabelle, storage):
    r, p = _importa(storage, DbSourceSpec(mode="table", ref=f"{P}_TIPI", sort_keys=["id"]))
    df = pl.read_parquet(p)
    assert dict(zip(df.columns, map(str, df.dtypes))) == {
        "ID": "Int64", "N_LIBERO": "Float64", "IMPORTO": "Float64", "TESTO": "String", "LUNGO": "String",
        "GREZZO": "Binary", "GIORNO": "Datetime(time_unit='us', time_zone=None)",
        "TS9": "Datetime(time_unit='ns', time_zone=None)", "TS_FUSO": "Datetime(time_unit='us', time_zone=None)", "VERO": "Boolean",
    }
    riga = df.row(0, named=True)
    assert riga["TESTO"] == "Caffè «già» 日本" and len(riga["LUNGO"]) == 8000 and riga["GREZZO"] == b"\x00\xff\x10"
    assert str(riga["TS_FUSO"]) == "2024-03-15 11:45:30"     # +02:00 → UTC
    assert riga["N_LIBERO"] == 12345.678 and riga["VERO"] is True
    assert all(v is None for k, v in df.row(1, named=True).items() if k != "ID")


def test_streaming_a_row_group_pieni(tabelle, storage, monkeypatch):
    from app.core import config

    monkeypatch.setattr(config.get_settings().ingest, "parquet_row_group_rows", 100_000)
    r, p = _importa(storage, DbSourceSpec(mode="table", ref=f"{P}_GRANDE"))
    md = pq.ParquetFile(p).metadata
    assert r["rows_written"] == 250_000
    assert [md.row_group(i).num_rows for i in range(md.num_row_groups)] == [100_000, 100_000, 50_000]


def test_query_ordinata_e_nome_fra_virgolette(tabelle, storage):
    q = f"SELECT id, ts_fuso FROM {P}_TIPI UNION ALL SELECT 0, TIMESTAMP '2024-01-01 00:00:00 -05:00' FROM dual"
    _, p = _importa(storage, DbSourceSpec(mode="sql", ref=q, sort_keys=["id"]))
    df = pl.read_parquet(p)
    assert df["ID"].to_list() == [0, 1, 2] and str(df["TS_FUSO"][0]) == "2024-01-01 05:00:00"
    _, p = _importa(storage, DbSourceSpec(mode="table", ref=f'"{P.lower()} conti"'))
    assert pl.read_parquet(p).to_dicts() == [{"ID": 7}]


def test_tipi_rifiutati_nominati(tabelle, storage):
    with pytest.raises(DbSourceError, match=r"DURATA \(INTERVAL_DS\).*TO_CHAR"):
        _importa(storage, DbSourceSpec(mode="table", ref=f"{P}_RIFIUTI"))


def test_errori_parlanti(tabelle, storage):
    with pytest.raises(DbSourceError, match=r"ORA-00942"):
        _importa(storage, DbSourceSpec(mode="table", ref="ZZT_NON_ESISTE"))
    with pytest.raises(DbSourceError, match=r"ORA-01017.*username and password"):
        _importa(storage, DbSourceSpec(mode="table", ref=f"{P}_TIPI"), password="sbagliata")
    with pytest.raises(DbSourceError, match=r"not registered with the listener.*service name"):
        _importa(storage, DbSourceSpec(mode="table", ref=f"{P}_TIPI"), database="NONESISTE")
