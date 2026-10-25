"""ClickHouse come sorgente: il tempo massimo di lettura dell'import.

ClickHouse non manda niente finché non ha la prima riga (con le chiavi di
ordinamento su colonne fuori dalla chiave della tabella legge e ordina tutto
prima): col default di clickhouse-connect, 300 s, gli import delle tabelle grandi
fallivano dopo cinque minuti con «controlla host, porta e firewall». Ora l'import e
la scrittura aspettano INGEST__DB_READ_TIMEOUT_SECONDS e l'errore dice cosa è
successo. I test dal vivo si attivano con CLICKHOUSE_TEST_HOST (come
test_clickhouse_engine.py); gli altri girano sempre."""
import os

import pyarrow as pa
import pytest

from app.core import config
from app.ingest import db_destination
from app.ingest.db_source import DbConnectionSpec, DbSourceError, DbSourceSpec, ingest_db_to_parquet, list_tables
from app.ingest.db_source import test_connection as prova_connessione  # non «test_»: pytest la raccoglierebbe


class _Fermo(Exception):
    pass


@pytest.fixture
def chiamate(monkeypatch):
    """Registra gli argomenti di clickhouse_connect.get_client e si ferma lì."""
    import clickhouse_connect

    viste: list[dict] = []

    def finto(**kw):
        viste.append(kw)
        raise _Fermo()

    monkeypatch.setattr(clickhouse_connect, "get_client", finto)
    return viste


def _conn(**kw) -> DbConnectionSpec:
    base = dict(db_type="clickhouse", host="ch.example", port=8443, username="u", password="p", database="d")
    base.update(kw)
    return DbConnectionSpec(**base)


def test_l_import_aspetta_quanto_dice_l_impostazione(chiamate, storage, monkeypatch):
    monkeypatch.setattr(config.get_settings().ingest, "db_read_timeout_seconds", 1234)
    with pytest.raises(DbSourceError):
        ingest_db_to_parquet(_conn(), DbSourceSpec(mode="table", ref="t", sort_keys=["k"]), "b", "k.parquet", storage=storage)
    assert chiamate[-1]["send_receive_timeout"] == 1234


def test_prova_ed_elenco_tengono_il_default(chiamate):
    for f in (prova_connessione, list_tables):
        with pytest.raises(_Fermo):
            f(_conn())
        assert "send_receive_timeout" not in chiamate[-1]


def test_la_scrittura_verso_clickhouse_aspetta_altrettanto(chiamate, monkeypatch):
    monkeypatch.setattr(config.get_settings().ingest, "db_read_timeout_seconds", 1234)
    schema = pa.schema([("a", pa.int64())])
    with pytest.raises(_Fermo):
        db_destination._write_clickhouse(_conn(), None, "`t`", ["t"], schema, iter([]))
    assert chiamate[-1]["send_receive_timeout"] == 1234


def test_il_messaggio_distingue_il_server_che_calcola_dalla_rete():
    from clickhouse_connect.driver.exceptions import OperationalError

    from app.ingest.db_errors import describe_db_error

    # testo vero di clickhouse-connect 1.4 (urllib3 ReadTimeoutError)
    slow = OperationalError("Error HTTPConnectionPool(host='chtest', port=8123): Read timed out. (read timeout=300) "
                             "executing HTTP request attempt 1 (http://chtest:8123)")
    assert describe_db_error(slow, "clickhouse", "chtest", 8123) == (
        "ClickHouse: chtest:8123 accepted the query but sent no data for 300 seconds: the database was still "
        "computing the result, as happens with large sorts and heavy queries."
    )
    # la connessione mai stabilita resta un problema di rete
    mai = OperationalError("Error HTTPConnectionPool(host='ch', port=8123): Max retries exceeded with url: /?q "
                           "(Caused by ConnectTimeoutError(<HTTPConnection(host='ch', port=8123) at 0x1>, "
                           "'Connection to ch timed out. (connect timeout=10)'))")
    assert describe_db_error(mai, "clickhouse", "ch", 8123) == (
        "ClickHouse: No response from ch:8123 within the time limit: check host, port and firewall."
    )


# ── dal vivo ──────────────────────────────────────────────────────────────────
dal_vivo = pytest.mark.skipif(not os.getenv("CLICKHOUSE_TEST_HOST"), reason="serve un ClickHouse (CLICKHOUSE_TEST_HOST)")


def _vero() -> DbConnectionSpec:
    return DbConnectionSpec(
        db_type="clickhouse", host=os.environ["CLICKHOUSE_TEST_HOST"], port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8123")),
        username=os.getenv("CLICKHOUSE_TEST_USER", "default"), password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""),
        database=os.getenv("CLICKHOUSE_TEST_DB", "default"),
    )


@dal_vivo
def test_un_server_che_tace_oltre_il_limite_e_un_server_che_calcola(storage, monkeypatch):
    # sleep(3): tre secondi senza un byte, come un ordinamento lungo in piccolo
    source = DbSourceSpec(mode="sql", ref="SELECT sleep(3) AS s")
    monkeypatch.setattr(config.get_settings().ingest, "db_read_timeout_seconds", 2)
    with pytest.raises(DbSourceError, match=r"accepted the query but sent no data for 2 seconds"):
        ingest_db_to_parquet(_vero(), source, "b", "k.parquet", storage=storage)
    monkeypatch.setattr(config.get_settings().ingest, "db_read_timeout_seconds", 10)
    assert ingest_db_to_parquet(_vero(), source, "b", "k.parquet", storage=storage)["rows_written"] == 1
