"""Error messages from the database SERVER, not the transport's Python exceptions.

The drivers' texts are the REAL ones captured on 2026-09-12 (the ClickHouse playground,
a sample Postgres, the PyMySQL and Trino libraries): if a driver changes format, these
tests say so. The texts for the user are in ENGLISH, and a guard test checks it.
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
# a stream started already and broken by the server: the message is in the partial bytes
CH_PARTIAL = (
    b"exception__\nkevxuijesbdpghhn\nCode: 396. DB::Exception: Limit for result exceeded, "
    b"max rows: 1.00 million, current rows: 1.02 million. (TOO_MANY_ROWS_OR_BYTES)\n"
    b"130 kevxuijesbdpghhn\n__exception__\n"
)


def test_clickhouse_broken_stream_shows_the_servers_limit():
    exc = ProtocolError("Connection broken: IncompleteRead(198 bytes read)", IncompleteRead(CH_PARTIAL))
    msg = describe_db_error(exc, "clickhouse", "play.clickhouse.com", 443)
    assert msg == (
        "ClickHouse: Limit for result exceeded, max rows: 1.00 million, current rows: 1.02 million "
        "(TOO_MANY_ROWS_OR_BYTES, code 396). " + LIMIT_HINT
    )
    assert "IncompleteRead" not in msg and "__exception__" not in msg


def test_clickhouse_broken_stream_without_a_message():
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
        # syntax: the real message goes on over several lines with «Expected one of…»;
        # «(SELEC)» is a fragment of the query, not the error's name
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
def test_clickhouse_server_errors(text, expected):
    from clickhouse_connect.driver.exceptions import DatabaseError

    assert describe_db_error(DatabaseError(text), "clickhouse", "play.clickhouse.com", 443) == expected


def test_clickhouse_network_errors():
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
    """The same shape as ADBC's exceptions: the driver's text plus the `sqlstate` attribute.
    The real class is covered by the check on the sample Postgres."""

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


def test_postgres_out_of_memory_is_the_servers_not_the_workers():
    msg = describe_db_error(
        _AdbcLike("INTERNAL: Failed to execute: ERROR:  out of memory\nDETAIL: Failed on request of size 8.", "53200"),
        "postgresql", "db", 5432,
    )
    assert msg.startswith("PostgreSQL: out of memory (SQLSTATE 53200).")
    assert msg.startswith(MESSAGE_PREFIXES)  # the gateway recognizes it as a database error


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
    # a REAL connection error, a closed port
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


# ── General rules ───────────────────────────────────────────────────────────
def test_clear_errors_pass_unchanged():
    assert describe_db_error(DbSourceError("The table name is empty"), "clickhouse") == \
        "The table name is empty"


def test_message_never_longer_than_the_limit_and_never_a_crash():
    from clickhouse_connect.driver.exceptions import DatabaseError

    long_text = "server response: Code: 62. DB::Exception: " + "x" * 5000 + " (SYNTAX_ERROR)"
    assert len(describe_db_error(DatabaseError(long_text), "clickhouse")) <= 500
    assert describe_db_error(ValueError(""), None).startswith("Database: ")


def test_network_messages_without_host():
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


def test_clickhouse_engine_names_the_server_once():
    from clickhouse_connect.driver.exceptions import OperationalError

    from app.engine.clickhouse_engine import _clean_error

    msg = _clean_error(OperationalError("Connection to ch.example timed out. (connect timeout=10)"),
                       "ch.example", 8443)
    assert msg == ("ClickHouse: No response from ch.example:8443 within the time limit: "
                   "check host, port and firewall.")
    assert msg.count("ClickHouse") == 1


# guard: the texts WE WRITE for the user are in English (the server's messages stay as
# the database sends them)
_ITALIAN = re.compile(
    r"\b(il|lo|la|gli|le|della|delle|dei|del|controlla|riduci|non|è|utente|connessione|"
    r"codice|nessuna|impossibile|rifiutata|errore|limite)\b", re.I,
)


def test_texts_for_the_user_in_english_only():
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


def test_ingest_translates_the_drivers_error_and_keeps_the_cause(monkeypatch, storage):
    """The real path of ingest_db_to_parquet: the driver fails, the run receives the
    server's message and the traceback keeps the original exception."""
    from app.ingest import db_source

    original = ProtocolError("Connection broken: IncompleteRead(198 bytes read)", IncompleteRead(CH_PARTIAL))

    def failing_driver(conn, query, **_):  # the import passes read_timeout to ClickHouse too
        raise original
        yield  # a generator, as the real drivers

    monkeypatch.setitem(db_source._DRIVERS, "clickhouse", failing_driver)
    conn = DbConnectionSpec(db_type="clickhouse", host="play.clickhouse.com", port=443, username="explorer")
    src = db_source.DbSourceSpec(mode="table", ref="cell_towers")
    with pytest.raises(DbSourceError) as info:
        db_source.ingest_db_to_parquet(conn, src, "data-prep", "datasets/x.parquet", storage=storage)
    assert str(info.value).startswith("ClickHouse: Limit for result exceeded")
    assert info.value.__cause__ is original


# ── Oracle (python-oracledb, Thin) ────────────────────────────────────────────
# Messages captured from the real driver against Oracle Free 23ai on 2026-10-11.
class _OracleLike(Exception):
    """The shape of python-oracledb's exceptions: the first argument is an `_Error` object
    with `.message` (code, text and sometimes more lines)."""

    def __init__(self, message):
        super().__init__(type("_Error", (), {"message": message})())


_OracleLike.__module__ = "oracledb.exceptions"


@pytest.mark.parametrize("message, expected", [
    (
        "ORA-01017: invalid credential or not authorized; logon denied\n"
        "Help: https://docs.oracle.com/error-help/db/ora-01017/",
        "Oracle: invalid credential or not authorized; logon denied (ORA-01017). "
        "Check the connection's username and password.",
    ),
    (
        'ORA-00942: table or view "TABULARIA"."NON_ESISTE" does not exist\n'
        "Help: https://docs.oracle.com/error-help/db/ora-00942/",
        'Oracle: table or view "TABULARIA"."NON_ESISTE" does not exist (ORA-00942).',
    ),
    (
        # a «cannot connect» says nothing: the innermost cause counts
        "DPY-6005: cannot connect to database (CONNECTION_ID=IUJC5+wYMMcQXZy6IusEpA==).\n"
        'DPY-6001: Service "NONESISTE" is not registered with the listener at host "172.22.0.17" port 1521. (Similar to ORA-12514)',
        'Oracle: Service "NONESISTE" is not registered with the listener at host "172.22.0.17" port 1521. '
        "(Similar to ORA-12514) (DPY-6001). Check the service name in the connection's Database field.",
    ),
])
def test_oracle(message, expected):
    assert describe_db_error(_OracleLike(message), "oracle", "oracletest", 1521) == expected


def test_oracle_cannot_connect_for_a_network_reason_says_the_network_reason():
    e = _OracleLike("DPY-6005: cannot connect to database (CONNECTION_ID=x==).\n[Errno 111] Connection refused")
    e.__cause__ = ConnectionRefusedError(111, "Connection refused")
    assert describe_db_error(e, "oracle", "oracletest", 1599) == (
        "Oracle: Connection refused by oracletest:1599: no service is listening on that port."
    )


# ── SQL Server (mssql-python) ─────────────────────────────────────────────────
# Shapes captured from the real driver against SQL Server 2022 on 2026-10-11.
class _MssqlLike(Exception):
    """The shape of mssql-python's exceptions: `driver_error` (the ODBC class) and
    `ddbc_error` (the server's text, behind the [Microsoft][…] prefixes)."""

    def __init__(self, driver_error, ddbc_error):
        super().__init__(f"Driver Error: {driver_error}; DDBC Error: {ddbc_error}")
        self.driver_error, self.ddbc_error = driver_error, ddbc_error


_MssqlLike.__module__ = "mssql_python.exceptions"


@pytest.mark.parametrize("driver_error, ddbc_error, port, expected", [
    ("Invalid authorization specification", "[Microsoft][SQL Server]Login failed for user 'lettore'.", 1433,
     "SQL Server: Login failed for user 'lettore'. Check the connection's username, password and database: "
     "SQL Server reports a database that does not exist, or that the user cannot open, as a failed login."),
    ("Base table or view not found", "[Microsoft][SQL Server]Invalid object name 'dbo.non_esiste'.", 1433,
     "SQL Server: Invalid object name 'dbo.non_esiste'."),
    ("Syntax error or access violation", "[Microsoft][SQL Server]The SELECT permission was denied on the object 'segreta', database 'prova', schema 'dbo'.", 1433,
     "SQL Server: The SELECT permission was denied on the object 'segreta', database 'prova', schema 'dbo'. "
     "The connection's user lacks the required privileges on this object."),
    ("Client unable to establish connection", "[Microsoft]SSL Provider: [error:0A000086:SSL routines::certificate verify failed:self-signed certificate]", 1433,
     "SQL Server: the TLS certificate of mssqltest:1433 cannot be verified (certificate verify failed: self-signed certificate). "
     "Give the server a certificate that can be verified, or choose «trust the server's certificate» in the connection (self-signed certificates)."),
    ("Client unable to establish connection", "[Microsoft]TCP Provider: Error code 0x2AFA", 1433,
     "SQL Server: Cannot resolve host mssqltest: check the server name."),
    ("Client unable to establish connection", "[Microsoft]TCP Provider: Error code 0x2749", 1599,
     "SQL Server: Connection refused by mssqltest:1599: no service is listening on that port."),
])
def test_sqlserver(driver_error, ddbc_error, port, expected):
    assert describe_db_error(_MssqlLike(driver_error, ddbc_error), "sqlserver", "mssqltest", port) == expected


def test_sqlserver_type_arrow_cannot_carry():
    e = RuntimeError("Unsupported data type for Arrow batch fetch for column - v, Type - -150, column ID - 2")
    assert describe_db_error(e, "sqlserver", "mssqltest", 1433) == (
        "SQL Server: column v has a type that cannot be imported (sql_variant). "
        "Convert it in a SQL source, e.g. CAST(col AS NVARCHAR(4000))."
    )
