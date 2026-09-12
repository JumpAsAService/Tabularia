"""Messaggi d'errore dal SERVER del database, non eccezioni Python di trasporto.

I testi dei driver sono quelli VERI catturati il 2026-09-12 (playground
ClickHouse, Postgres di esempio, librerie PyMySQL e Trino): se un driver cambia
formato, questi test lo segnalano. I testi per l'utente sono in INGLESE, e un
test di guardia lo verifica.
"""
import re
from http.client import IncompleteRead

import pytest
from urllib3.exceptions import ProtocolError

from app.ingest.db_errors import MESSAGE_PREFIXES, describe_db_error
from app.ingest.db_source import DbConnectionSpec, DbSourceError

LIMIT_HINT = "This is a limit set by the server: reduce the data with a filter or a LIMIT."
AUTH_HINT = "Check the connection's username and password."


# ── ClickHouse ────────────────────────────────────────────────────────────────
# stream già partito e interrotto dal server: il messaggio sta nei byte parziali
CH_PARTIAL = (
    b"exception__\nkevxuijesbdpghhn\nCode: 396. DB::Exception: Limit for result exceeded, "
    b"max rows: 1.00 million, current rows: 1.02 million. (TOO_MANY_ROWS_OR_BYTES)\n"
    b"130 kevxuijesbdpghhn\n__exception__\n"
)


def test_clickhouse_stream_interrotto_mostra_il_limite_del_server():
    exc = ProtocolError("Connection broken: IncompleteRead(198 bytes read)", IncompleteRead(CH_PARTIAL))
    msg = describe_db_error(exc, "clickhouse", "play.clickhouse.com", 443)
    assert msg == (
        "ClickHouse: Limit for result exceeded, max rows: 1.00 million, current rows: 1.02 million "
        "(TOO_MANY_ROWS_OR_BYTES, code 396). " + LIMIT_HINT
    )
    assert "IncompleteRead" not in msg and "__exception__" not in msg


def test_clickhouse_stream_interrotto_senza_messaggio():
    exc = ProtocolError("Connection broken: IncompleteRead(0 bytes read)", IncompleteRead(b""))
    msg = describe_db_error(exc, "clickhouse", "ch.example", 8443)
    assert msg.startswith("ClickHouse: The server closed the connection mid-response")


@pytest.mark.parametrize("text, expected", [
    (
        "Received ClickHouse exception, code: 60, server response: Code: 60. DB::Exception: Unknown "
        "table expression identifier 'tabella_che_non_esiste' in scope SELECT * FROM tabella_che_non_esiste. "
        "(UNKNOWN_TABLE) (for url https://play.clickhouse.com:443)",
        "ClickHouse: Unknown table expression identifier 'tabella_che_non_esiste' in scope "
        "SELECT * FROM tabella_che_non_esiste (UNKNOWN_TABLE, code 60).",
    ),
    (
        "Received ClickHouse exception, code: 47, server response: Code: 47. DB::Exception: Unknown "
        "expression identifier `colonna_x` in scope SELECT colonna_x FROM cell_towers LIMIT 1. "
        "(UNKNOWN_IDENTIFIER) (for url https://play.clickhouse.com:443)",
        "ClickHouse: Unknown expression identifier `colonna_x` in scope SELECT colonna_x FROM "
        "cell_towers LIMIT 1 (UNKNOWN_IDENTIFIER, code 47).",
    ),
    (
        # sintassi: il messaggio vero continua su più righe con «Expected one of…»;
        # «(SELEC)» è un frammento della query, non il nome dell'errore
        "Received ClickHouse exception, code: 62, server response: Code: 62. DB::Exception: Syntax "
        "error: failed at position 1 (SELEC) (line 1, col 1): SELEC 1\n FORMAT Native. Expected one "
        "of: Query, SELECT query, WITH. (SYNTAX_ERROR) (for url https://play.clickhouse.com:443)",
        "ClickHouse: Syntax error: failed at position 1 (SELEC) (line 1, col 1): SELEC 1 "
        "(SYNTAX_ERROR, code 62).",
    ),
    (
        "Received ClickHouse exception, code: 516, server response: Code: 516. DB::Exception: nessuno: "
        "Authentication failed: password is incorrect, or there is no user with such name. "
        "(AUTHENTICATION_FAILED) (for url https://play.clickhouse.com:443)",
        "ClickHouse: nessuno: Authentication failed: password is incorrect, or there is no user with "
        "such name (AUTHENTICATION_FAILED, code 516). " + AUTH_HINT,
    ),
])
def test_clickhouse_errori_del_server(text, expected):
    from clickhouse_connect.driver.exceptions import DatabaseError

    assert describe_db_error(DatabaseError(text), "clickhouse", "play.clickhouse.com", 443) == expected


def test_clickhouse_errori_di_rete():
    from clickhouse_connect.driver.exceptions import OperationalError

    dns = OperationalError(
        "Error HTTPSConnectionPool(host='non-esiste.clickhouse.example', port=443): Max retries exceeded "
        "with url: /?query_id=ef10 (Caused by NameResolutionError(\"HTTPSConnection(host='non-esiste."
        "clickhouse.example', port=443): Failed to resolve 'non-esiste.clickhouse.example' ([Errno -2] "
        "Name or service not known)\")) executing HTTP request attempt 1"
    )
    assert describe_db_error(dns, "clickhouse", "non-esiste.clickhouse.example", 443) == (
        "ClickHouse: Cannot resolve host non-esiste.clickhouse.example: check the server name."
    )
    timeout = OperationalError(
        "Error HTTPConnectionPool(host='play.clickhouse.com', port=8123): Max retries exceeded with url: "
        "/?query_id=2a7b (Caused by ConnectTimeoutError(<HTTPConnection(host='play.clickhouse.com', "
        "port=8123) at 0x7153>, 'Connection to play.clickhouse.com timed out. (connect timeout=5)')) "
        "executing HTTP request attempt 1 (http://play.clickhouse.com:8123)"
    )
    assert describe_db_error(timeout, "clickhouse", "play.clickhouse.com", 8123) == (
        "ClickHouse: No response from play.clickhouse.com:8123 within the time limit: "
        "check host, port and firewall."
    )


# ── PostgreSQL (ADBC) ─────────────────────────────────────────────────────────
class _AdbcLike(Exception):
    """Stessa forma delle eccezioni ADBC: testo del driver più l'attributo
    `sqlstate`. La classe vera è coperta dalla verifica sul Postgres di esempio."""

    def __init__(self, text, sqlstate=None):
        super().__init__(text)
        self.sqlstate = sqlstate


@pytest.mark.parametrize("text, sqlstate, expected", [
    (
        'NOT_FOUND: Failed to prepare query: ERROR:  relation "vendite.ordinix" does not exist\n'
        "LINE 1: SELECT * FROM vendite.ordinix\n                      ^\n\n"
        "Query was:SELECT * FROM vendite.ordinix. SQLSTATE: 42P01",
        "42P01",
        'PostgreSQL: relation "vendite.ordinix" does not exist (SQLSTATE 42P01).',
    ),
    (
        'INVALID_ARGUMENT: Failed to prepare query: ERROR:  syntax error at or near "SELEC"\n'
        "LINE 1: SELEC 1\n        ^\n\nQuery was:SELEC 1. SQLSTATE: 42601",
        "42601",
        'PostgreSQL: syntax error at or near "SELEC" (SQLSTATE 42601).',
    ),
    (
        'IO: [libpq] Failed to connect: connection to server at "sampledb-postgres" (172.22.0.17), port '
        '5432 failed: FATAL:  password authentication failed for user "shop"\n',
        None,
        'PostgreSQL: password authentication failed for user "shop". ' + AUTH_HINT,
    ),
    (
        'IO: [libpq] Failed to connect: could not translate host name "non-esiste-pg" to address: '
        "Temporary failure in name resolution\n",
        None,
        "PostgreSQL: Cannot resolve host non-esiste-pg: check the server name.",
    ),
])
def test_postgresql(text, sqlstate, expected):
    host = "non-esiste-pg" if "non-esiste-pg" in text else "sampledb-postgres"
    assert describe_db_error(_AdbcLike(text, sqlstate), "postgresql", host, 5432) == expected


def test_postgres_out_of_memory_e_del_server_non_del_worker():
    msg = describe_db_error(
        _AdbcLike("INTERNAL: Failed to execute: ERROR:  out of memory\nDETAIL: Failed on request of size 8.", "53200"),
        "postgresql", "db", 5432,
    )
    assert msg.startswith("PostgreSQL: out of memory (SQLSTATE 53200).")
    assert msg.startswith(MESSAGE_PREFIXES)  # il gateway lo riconosce come errore del database


# ── MySQL (PyMySQL) ───────────────────────────────────────────────────────────
def test_mysql():
    pymysql = pytest.importorskip("pymysql")
    assert describe_db_error(
        pymysql.err.ProgrammingError(1146, "Table 'shop.ordinix' doesn't exist"), "mysql", "db", 3306,
    ) == "MySQL: Table 'shop.ordinix' doesn't exist (code 1146)."
    assert describe_db_error(
        pymysql.err.OperationalError(1045, "Access denied for user 'u'@'172.22.0.5' (using password: YES)"),
        "mariadb", "db", 3306,
    ) == "MariaDB: Access denied for user 'u'@'172.22.0.5' (using password: YES) (code 1045). " + AUTH_HINT
    # errore di connessione VERO, porta chiusa
    try:
        pymysql.connect(host="127.0.0.1", port=1, user="u", password="p", connect_timeout=3)
    except pymysql.err.OperationalError as e:
        msg = describe_db_error(e, "mysql", "127.0.0.1", 1)
    assert msg.startswith("MySQL: Can't connect to MySQL server on '127.0.0.1'") and "(code 2003)" in msg
    assert msg.endswith("The server is unreachable: check host, port and network.")


# ── Trino ─────────────────────────────────────────────────────────────────────
def test_trino():
    pytest.importorskip("trino")
    from trino.exceptions import TrinoConnectionError, TrinoUserError

    err = TrinoUserError({"message": "line 1:15: Table 'hive.default.ordinix' does not exist",
                          "errorCode": 46, "errorName": "TABLE_NOT_FOUND", "errorType": "USER_ERROR"},
                         "20260912_000000_00001_abcde")
    assert describe_db_error(err, "trino", "trino", 8080) == (
        "Trino: line 1:15: Table 'hive.default.ordinix' does not exist (TABLE_NOT_FOUND)."
    )
    conn_err = TrinoConnectionError(
        "failed to execute: HTTPConnectionPool(host='127.0.0.1', port=1): Max retries exceeded with url: "
        "/v1/statement (Caused by NewConnectionError(\"HTTPConnection(host='127.0.0.1', port=1): Failed to "
        "establish a new connection: [Errno 111] Connection refused\"))"
    )
    assert describe_db_error(conn_err, "trino", "127.0.0.1", 1) == (
        "Trino: Connection refused by 127.0.0.1:1: no service is listening on that port."
    )


# ── Regole generali ───────────────────────────────────────────────────────────
def test_errori_gia_parlanti_passano_invariati():
    assert describe_db_error(DbSourceError("Il nome della tabella è vuoto"), "clickhouse") == \
        "Il nome della tabella è vuoto"


def test_messaggio_mai_piu_lungo_del_limite_e_mai_un_crash():
    from clickhouse_connect.driver.exceptions import DatabaseError

    long_text = "server response: Code: 62. DB::Exception: " + "x" * 5000 + " (SYNTAX_ERROR)"
    assert len(describe_db_error(DatabaseError(long_text), "clickhouse")) <= 500
    assert describe_db_error(ValueError(""), None).startswith("Database: ")


def test_messaggi_di_rete_senza_host():
    from clickhouse_connect.driver.exceptions import OperationalError

    timeout = OperationalError("Error HTTPConnectionPool: Connection to ch timed out. (connect timeout=5)")
    assert describe_db_error(timeout, "clickhouse") == (
        "ClickHouse: No response from the server within the time limit: check host, port and firewall."
    )
    dns = OperationalError("Failed to resolve 'ch' ([Errno -2] Name or service not known)")
    assert describe_db_error(dns, "clickhouse") == (
        "ClickHouse: Cannot resolve the server name: check the connection's host."
    )
    refused = OperationalError("Failed to establish a new connection: [Errno 111] Connection refused")
    assert describe_db_error(refused, "clickhouse") == (
        "ClickHouse: Connection refused by the server: no service is listening on that port."
    )


def test_engine_clickhouse_nomina_il_server_una_volta_sola():
    from clickhouse_connect.driver.exceptions import OperationalError

    from app.engine.clickhouse_engine import _clean_error

    msg = _clean_error(OperationalError("Connection to ch.example timed out. (connect timeout=10)"),
                       "ch.example", 8443)
    assert msg == ("ClickHouse: No response from ch.example:8443 within the time limit: "
                   "check host, port and firewall.")
    assert msg.count("ClickHouse") == 1


# guardia: i testi SCRITTI DA NOI per l'utente sono in inglese (i messaggi del
# server restano come li manda il database)
_ITALIAN = re.compile(
    r"\b(il|lo|la|gli|le|della|delle|dei|del|controlla|riduci|non|è|utente|connessione|"
    r"codice|nessuna|impossibile|rifiutata|errore|limite)\b", re.I,
)


def test_testi_per_l_utente_solo_in_inglese():
    from clickhouse_connect.driver.exceptions import OperationalError

    from app.ingest import db_errors

    ours = [v for k, v in vars(db_errors).items() if k.endswith("_HINT") and isinstance(v, str)]
    for text in ("timed out", "Name or service not known", "Connection refused",
                 "CERTIFICATE_VERIFY_FAILED", "IncompleteRead"):
        for host, port in (("db.example", 5432), (None, None)):
            ours.append(describe_db_error(OperationalError(text), "clickhouse", host, port))
    assert len(ours) >= 17
    for text in ours:
        assert not _ITALIAN.search(text), f"testo per l'utente non in inglese: {text!r}"


def test_ingest_traduce_l_errore_del_driver_e_conserva_la_causa(monkeypatch, storage):
    """Percorso vero di ingest_db_to_parquet: il driver fallisce, il run riceve il
    messaggio del server e il traceback conserva l'eccezione originale."""
    from app.ingest import db_source

    original = ProtocolError("Connection broken: IncompleteRead(198 bytes read)", IncompleteRead(CH_PARTIAL))

    def failing_driver(conn, query):
        raise original
        yield  # generatore, come i driver veri

    monkeypatch.setitem(db_source._DRIVERS, "clickhouse", failing_driver)
    conn = DbConnectionSpec(db_type="clickhouse", host="play.clickhouse.com", port=443, username="explorer")
    src = db_source.DbSourceSpec(mode="table", ref="cell_towers")
    with pytest.raises(DbSourceError) as info:
        db_source.ingest_db_to_parquet(conn, src, "data-prep", "datasets/x.parquet", storage=storage)
    assert str(info.value).startswith("ClickHouse: Limit for result exceeded")
    assert info.value.__cause__ is original
