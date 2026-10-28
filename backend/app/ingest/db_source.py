"""Database sources: runs a query (or reads a table) and writes the result as
parquet WITHOUT ever holding all of it in memory.

Data path: driver → Arrow batches → ParquetWriter on a temporary file → upload to the
storage. Memory stays O(batch size); the disk is the buffer, as for file uploads.

One driver per database type, all with the same protocol (a generator that yields
FIRST the Arrow schema, THEN the RecordBatches):
- postgresql        → ADBC (native Arrow, server-side streaming)
- clickhouse        → clickhouse-connect (query_arrow_stream, native Arrow)
- mysql / mariadb   → PyMySQL with an unbuffered cursor (fetchmany → Arrow)
- trino             → the official DBAPI client (natural paging → Arrow)
- oracle            → python-oracledb in Thin mode (fetch_df_batches → Arrow)
- sqlserver         → mssql-python, Microsoft's driver (arrow_reader → Arrow)
"""
from __future__ import annotations

import importlib.util
import logging
import os
import re
import tempfile
from typing import Any, Callable, Iterator, Literal
from urllib.parse import quote as _urlquote

import polars as pl
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from pydantic import BaseModel

from app.core.config import get_settings
from app.ingest.converters import IngestError
from app.ingest.db_errors import describe_db_error

logger = logging.getLogger(__name__)

# rows per batch on the DBAPI path (Postgres and ClickHouse use their native batches)
BATCH_ROWS = 50_000

DEFAULT_PORTS = {
    "postgresql": 5432,
    "mysql": 3306,
    "mariadb": 3306,
    "clickhouse": 8123,  # the HTTP port (clickhouse-connect), not the native 9000
    "trino": 8080,
    "oracle": 1521,
    "sqlserver": 1433,
}

DbType = Literal["postgresql", "mysql", "mariadb", "clickhouse", "trino", "oracle", "sqlserver"]


class DbSourceError(IngestError):
    """A clear error to show the user (connection, query, types)."""


# Database drivers that are not in the published images: the module each needs. The
# SQL Server one depends on Microsoft's ODBC driver, under Microsoft's licence, which
# an AGPL image cannot carry: whoever needs it installs it (docs/sqlserver.md).
OPTIONAL_DRIVERS = {"sqlserver": "mssql_python"}
_DRIVER_LABELS = {"sqlserver": "SQL Server"}
DRIVER_DOCS = {"sqlserver": "docs/sqlserver.md"}


class DriverNotInstalledError(DbSourceError):
    """The driver of this database type is not installed in this image. It carries a
    code, so that the gateway can say it in the user's language. Built with
    `driver_not_installed(db_type)`; the message is its only argument, so that the
    exception rebuilt from its args (pickle, Celery's result backend) says the same."""

    code = "driver_not_installed"

    def __init__(self, message: str, db_type: str = ""):
        super().__init__(message)
        self.db_type = db_type


def driver_not_installed(db_type: str) -> DriverNotInstalledError:
    return DriverNotInstalledError(
        f"The {_DRIVER_LABELS.get(db_type, db_type)} driver is not installed in this installation: "
        f"the published images do not include it. See {DRIVER_DOCS.get(db_type, 'the documentation')} to add it.",
        db_type,
    )


def _module_installed(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def installed_drivers() -> dict[str, dict]:
    """Which optional drivers this image has (without importing them)."""
    return {db_type: {"installed": _module_installed(module)} for db_type, module in OPTIONAL_DRIVERS.items()}


class DbConnectionSpec(BaseModel):
    db_type: DbType
    host: str
    port: int | None = None
    username: str = ""
    # one of the two: `password_encrypted` (Fernet, as it comes from the gateway) or
    # `password` in clear (only for direct trials on the engine in development)
    password: str = ""
    password_encrypted: str = ""
    database: str = ""   # Oracle: the SERVICE NAME (FREEPDB1, ORCLPDB1)
    db_schema: str = ""  # Postgres schema / Trino schema / Oracle owner; empty = the default
    # options of the type, from the connection's `extra` field (SQL Server: {"tls": …})
    extra: dict = {}

    @property
    def port_or_default(self) -> int:
        return self.port or DEFAULT_PORTS[self.db_type]

    def resolve_password(self) -> str:
        if self.password_encrypted:
            from app.core.crypto import decrypt_secret

            return decrypt_secret(self.password_encrypted)
        return self.password


class DbSourceSpec(BaseModel):
    mode: Literal["table", "sql"]
    ref: str  # the table's name (schema.table too) or the SQL text
    # columns of the final ORDER BY: the parquet comes out sorted → pruning downstream.
    # Empty = no order (as before). Column names of the RESULT.
    sort_keys: list[str] = []


# ─────────────────────────────────────────────────────────────────────────────
# Building the query
# ─────────────────────────────────────────────────────────────────────────────
_IDENT_QUOTE = {"postgresql": '"', "trino": '"', "mysql": "`", "mariadb": "`", "clickhouse": "`", "oracle": '"'}


def _tsql_ident(part: str) -> str:
    """A T-SQL identifier in square brackets: `dbo` → `[dbo]`; one in brackets already
    (`[Orders 2024]`, as the listing returns it) stays as it is."""
    if len(part) >= 2 and part.startswith("[") and part.endswith("]"):
        inner = part[1:-1]
        if not inner or "]" in inner.replace("]]", ""):
            raise DbSourceError(f"Invalid name: {part}")
        return part
    return "[" + part.replace("]", "]]") + "]"


def _tsql_query(sql: str, conn: DbConnectionSpec, source: DbSourceSpec) -> str:
    """The user's SQL on SQL Server. Not wrapped in `SELECT * FROM (…)`: in T-SQL a
    derived table may hold neither a CTE nor an ORDER BY, and both are frequent. It is
    checked instead to be ONE read query — a T-SQL batch could carry a `; DELETE …` —
    and sent as it is. With sort keys: wrapped when possible, otherwise (a CTE) the
    ORDER BY goes at the end."""
    import sqlglot
    from sqlglot import exp

    try:
        statement = [s for s in sqlglot.parse(sql, read="tsql") if s is not None]
    except Exception:  # noqa: BLE001 — syntax sqlglot does not know: the server decides, in the wrapper
        statement = None
    if statement is not None:
        if len(statement) != 1 or not isinstance(statement[0], exp.Query):
            raise DbSourceError("SQL Server: the SQL source must be a single SELECT (a WITH … SELECT is fine).")
        if statement[0].find(exp.Into) is not None:  # a SELECT for sqlglot, a table creation for SQL Server
            raise DbSourceError("SQL Server: the SQL source must only read: SELECT … INTO creates a table, remove the INTO.")
    keys = [k.strip() for k in (source.sort_keys or []) if k and k.strip()]
    if not keys:
        return sql if statement is not None else f"SELECT * FROM ({sql}) AS _q"
    order = " ORDER BY " + ", ".join(_tsql_ident(k) for k in keys)
    if statement is not None and statement[0].find(exp.With) is not None:
        return sql + order
    return f"SELECT * FROM ({sql}) AS _q" + order


def _oracle_ident(part: str) -> str:
    """An Oracle identifier, with Oracle's rule: WITHOUT quotes it reads in uppercase
    (`hr.employees` is HR.EMPLOYEES, as in SQL Developer); IN quotes it stays as it is
    (`HR."accounts 2024"`). The table listing returns the names in this form already,
    so picking from the list works."""
    if len(part) >= 2 and part.startswith('"') and part.endswith('"'):
        inner = part[1:-1]
        if not inner or '"' in inner:
            raise DbSourceError(f"Invalid name: {part}")
        return f'"{inner}"'
    if '"' in part:
        raise DbSourceError(f"Invalid name: {part}")
    return f'"{part.upper()}"'


def build_query(conn: DbConnectionSpec, source: DbSourceSpec) -> str:
    """The final query to run on the database.

    - mode=sql: the user's text is wrapped in `SELECT * FROM (…) AS _q`. Not a security
      barrier (the barrier is the CONNECT capability and a read-only database user): it
      refuses non-SELECT statements and lets a LIMIT be added without touching the text.
    - mode=table: identifiers quoted per dialect; `schema.table` accepted, otherwise the
      connection's schema goes in front when set.
    """
    if source.mode == "sql":
        sql = source.ref.strip().rstrip(";").strip()
        if not sql:
            raise DbSourceError("The SQL query is empty")
        if "--" in sql.rsplit("\n", 1)[-1]:
            # a comment on the last line would swallow the wrapper's closing parenthesis
            sql += "\n"
        if conn.db_type == "sqlserver":
            return _tsql_query(sql, conn, source)
        # Oracle wants no AS before a table's alias, nor an alias with «_»
        alias = " q" if conn.db_type == "oracle" else " AS _q"
        return _with_order_by(f"SELECT * FROM ({sql}){alias}", conn, source)

    if conn.db_type == "oracle":
        parts = [p.strip() for p in source.ref.split(".") if p.strip()]
        if not parts:
            raise DbSourceError("The table name is empty")
        if len(parts) == 1 and conn.db_schema:
            parts = [conn.db_schema, parts[0]]
        return _with_order_by("SELECT * FROM " + ".".join(_oracle_ident(p) for p in parts), conn, source)

    if conn.db_type == "sqlserver":
        parts = [p.strip() for p in source.ref.split(".") if p.strip()]
        if not parts:
            raise DbSourceError("The table name is empty")
        if len(parts) == 1 and conn.db_schema:
            parts = [conn.db_schema, parts[0]]
        return _with_order_by("SELECT * FROM " + ".".join(_tsql_ident(p) for p in parts), conn, source)

    q = _IDENT_QUOTE[conn.db_type]
    parts = [p.strip() for p in source.ref.split(".") if p.strip()]
    if not parts:
        raise DbSourceError("The table name is empty")
    if any(q in p for p in parts):
        raise DbSourceError(f"Invalid table name: it contains {q!r}")
    if len(parts) == 1 and conn.db_schema:
        parts = [conn.db_schema, parts[0]]
    qualified = ".".join(f"{q}{p}{q}" for p in parts)
    return _with_order_by(f"SELECT * FROM {qualified}", conn, source)


def _with_order_by(base: str, conn: DbConnectionSpec, source: DbSourceSpec) -> str:
    """Appends `ORDER BY <keys>` (quoted per dialect) when there are sort_keys. The keys
    are column names of the result; the database gives a clear error when they do not
    exist. Empty = the query unchanged."""
    keys = [k.strip() for k in (source.sort_keys or []) if k and k.strip()]
    if not keys:
        return base
    if conn.db_type == "oracle":
        return f"{base} ORDER BY " + ", ".join(_oracle_ident(k) for k in keys)
    if conn.db_type == "sqlserver":
        return f"{base} ORDER BY " + ", ".join(_tsql_ident(k) for k in keys)
    q = _IDENT_QUOTE[conn.db_type]
    for k in keys:
        if q in k:
            raise DbSourceError(f"Invalid sort key: it contains {q!r}")
    cols = ", ".join(f"{q}{k}{q}" for k in keys)
    return f"{base} ORDER BY {cols}"


# ─────────────────────────────────────────────────────────────────────────────
# Driver: generatori "schema poi batch"
# ─────────────────────────────────────────────────────────────────────────────
def _batches_postgresql(conn: DbConnectionSpec, query: str) -> Iterator[Any]:
    from adbc_driver_postgresql import dbapi as adbc_pg

    uri = (
        f"postgresql://{_urlquote(conn.username, safe='')}:"
        f"{_urlquote(conn.resolve_password(), safe='')}@"
        f"{conn.host}:{conn.port_or_default}/{conn.database}"
    )
    with adbc_pg.connect(uri) as c:
        with c.cursor() as cur:
            cur.execute(query)
            reader = cur.fetch_record_batch()  # RecordBatchReader: true streaming
            yield _schema_without_extensions(reader.schema)
            for batch in reader:
                yield _batch_without_extensions(batch)


# Postgres's ADBC driver returns the types Arrow lacks (numeric, and Postgres's other
# «own» types) as an opaque EXTENSION (`arrow.opaque`) over a text. Written so in the
# parquet, Polars reads it as text, but its streaming reader PANICs as soon as a filter
# or a NULL check is pushed into the read of that column — and the worker quit half
# way. It is written without the wrapper (found by the dbt export oracle, 2026-10-10):
# · `numeric` is a NUMBER (Float64): amounts and prices add up, as those of a CSV or an
#   Excel file (the user's choice, 2026-10-10; they were text and `sum` refused them).
#   Float64 and not decimal: the driver gives no precision and scale, and the five
#   engines compute doubles alike (ClickHouse truncates divisions between decimals to
#   the column's scale, the others do not). NaN and ±Infinity stay so.
# · the other opaque types stay their text.
def _type_without_extension(t):
    if isinstance(t, pa.OpaqueType) and t.type_name == "numeric":
        return pa.float64()
    if isinstance(t, pa.BaseExtensionType):
        base = t.storage_type
        return pa.string() if (pa.types.is_string(base) or pa.types.is_large_string(base) or str(base) in ("string_view", "utf8_view")) else base
    return t


def _schema_without_extensions(schema):
    if not any(isinstance(f.type, pa.BaseExtensionType) for f in schema):
        return schema
    return pa.schema([pa.field(f.name, _type_without_extension(f.type), nullable=f.nullable) for f in schema])


def _batch_without_extensions(batch):
    if not any(isinstance(f.type, pa.BaseExtensionType) for f in batch.schema):
        return batch
    cols = []
    for arr in batch.columns:
        if isinstance(arr.type, pa.BaseExtensionType):
            arr = pc.cast(arr.storage, _type_without_extension(arr.type))
        cols.append(arr)
    return pa.RecordBatch.from_arrays(cols, schema=_schema_without_extensions(batch.schema))


# ClickHouse exports temporal types to Arrow as raw INTEGERS (Date→uint16 days,
# Date32→int32, DateTime→uint32 seconds, DateTime64→int64 in `scale` decimals):
# without converting them back they would land in the parquet as numbers (the datetime
# 1714546800). At the Arrow level a uint32 cannot be told from a true integer, so the
# ClickHouse TYPES (the datatype class name, which ignores the Nullable/LowCardinality
# wrappers) say which columns to convert.
def _clickhouse_temporal_targets(ch_types) -> dict:
    """Column index → (target Arrow type, DateTime64 scale | None)."""
    targets: dict[int, tuple] = {}
    for i, t in enumerate(ch_types):
        cls = type(t).__name__
        if cls in ("Date", "Date32"):
            targets[i] = (pa.date32(), None)
        elif cls == "DateTime":
            targets[i] = (pa.timestamp("s"), None)  # NAIVE = a UTC instant (as ADBC)
        elif cls == "DateTime64":
            scale = int(getattr(t, "scale", 3) or 0)
            unit = {0: "s", 3: "ms", 6: "us", 9: "ns"}.get(scale, "us")
            targets[i] = (pa.timestamp(unit), scale)
    return targets


def _cast_ch_temporal(arr, target):
    """ClickHouse's raw integer read again as a date32/timestamp."""
    arrow_t, scale = target
    if pa.types.is_date(arrow_t):
        return pc.cast(pc.cast(arr, pa.int32()), arrow_t)  # days since 1970
    raw = pc.cast(arr, pa.int64())
    if scale is not None and scale not in (0, 3, 6, 9):
        # DateTime64 with an unusual precision → rescaled to the chosen unit (us)
        raw = pc.multiply(raw, 10 ** (6 - scale)) if scale < 6 else pc.divide(raw, 10 ** (scale - 6))
        raw = pc.cast(raw, pa.int64())
    return pc.cast(raw, arrow_t)


def _fix_clickhouse_temporals(batch, targets: dict):
    """Writes the RecordBatch again with the integer temporal columns converted."""
    if not targets:
        return batch
    cols, fields = [], []
    for i, field in enumerate(batch.schema):
        arr, tgt = batch.column(i), targets.get(i)
        if tgt is not None:
            arr = _cast_ch_temporal(arr, tgt)
            field = pa.field(field.name, tgt[0], nullable=field.nullable)
        cols.append(arr)
        fields.append(field)
    return pa.RecordBatch.from_arrays(cols, schema=pa.schema(fields))


def _batches_clickhouse(conn: DbConnectionSpec, query: str, read_timeout: int | None = None) -> Iterator[Any]:
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=conn.host,
        port=conn.port_or_default,
        username=conn.username or "default",
        password=conn.resolve_password(),
        database=conn.database or "default",
        connect_timeout=10,
        # None = the library's default (300 s): connection test and table listing
        **({"send_receive_timeout": read_timeout} if read_timeout else {}),
    )
    try:
        # ClickHouse types from a LIMIT 0 schema: they find the temporal columns (which
        # the Arrow export makes raw integers) to convert back.
        targets = _clickhouse_temporal_targets(
            client.query(f"SELECT * FROM ({query}) AS _s LIMIT 0").column_types
        )
        sent_schema = False
        with client.query_arrow_stream(query) as stream:
            for chunk in stream:  # depending on the version: Table or RecordBatch
                batches = chunk.to_batches() if isinstance(chunk, pa.Table) else [chunk]
                for batch in batches:
                    batch = _fix_clickhouse_temporals(batch, targets)
                    if not sent_schema:
                        yield batch.schema
                        sent_schema = True
                    yield batch
        if not sent_schema:
            # an empty result: the schema from LIMIT 0, with the temporal types right
            empty = client.query_arrow(f"SELECT * FROM ({query}) AS _s LIMIT 0")
            yield pa.schema([
                pa.field(f.name, targets[i][0], nullable=f.nullable) if i in targets else f
                for i, f in enumerate(empty.schema)
            ])
    finally:
        client.close()


def _batches_mysql(conn: DbConnectionSpec, query: str) -> Iterator[Any]:
    import pymysql
    import pymysql.cursors

    c = pymysql.connect(
        host=conn.host,
        port=conn.port_or_default,
        user=conn.username,
        password=conn.resolve_password(),
        database=conn.database or None,
        cursorclass=pymysql.cursors.SSCursor,  # unbuffered: the server streams
        connect_timeout=10,
        charset="utf8mb4",
    )
    try:
        with c.cursor() as cur:
            cur.execute(query)
            yield from _dbapi_batches(cur)
    finally:
        c.close()


def _batches_trino(conn: DbConnectionSpec, query: str) -> Iterator[Any]:
    import trino

    password = conn.resolve_password()
    kwargs: dict[str, Any] = {
        "host": conn.host,
        "port": conn.port_or_default,
        "user": conn.username or "tabularia",
        "catalog": conn.database or None,
        "schema": conn.db_schema or "default",
    }
    if password:
        # the Trino client refuses basic auth over http: with a password, https
        kwargs["http_scheme"] = "https"
        kwargs["auth"] = trino.auth.BasicAuthentication(conn.username, password)
    c = trino.dbapi.connect(**kwargs)
    try:
        cur = c.cursor()
        cur.execute(query)
        yield from _dbapi_batches(cur)
    finally:
        c.close()


# ── Oracle ──────────────────────────────────────────────────────────────────
# python-oracledb in Thin mode: pure Python, no Oracle Instant Client in the image.
# `fetch_df_batches` reads one block of rows at a time from the server and hands it
# over as an Arrow data frame (PyCapsule): no Python rows in between.
# The query is DESCRIBED first (WHERE 1 = 0), for two things to decide before reading:
# the timestamps with a time zone, which the driver would hand over without it (they
# are brought to UTC on the server; the engines' standard is naive UTC instants), and
# the types that do not reach Arrow or Parquet, refused naming the columns.
_ORACLE_WITH_ZONE = {"DB_TYPE_TIMESTAMP_TZ", "DB_TYPE_TIMESTAMP_LTZ"}
_ORACLE_REFUSED = {
    "DB_TYPE_INTERVAL_DS", "DB_TYPE_INTERVAL_YM", "DB_TYPE_OBJECT", "DB_TYPE_XMLTYPE",
    "DB_TYPE_BFILE", "DB_TYPE_CURSOR",
}
# our final ORDER BY (quoted identifiers only, at the outer level: the user's SQL is
# always in parentheses), to put back outside the wrapper
_ORDER_BY_FINALE = re.compile(r'^(.*) ORDER BY ((?:"[^"]+")(?:, "[^"]+")*)$', re.S)


def _oracle_type(t) -> str:
    return getattr(t, "name", str(t))


def _oracle_connect(conn: DbConnectionSpec):
    import oracledb

    if not conn.database:
        raise DbSourceError("Oracle: the connection needs the service name in the Database field (e.g. FREEPDB1, ORCLPDB1).")
    return oracledb.connect(
        user=conn.username,
        password=conn.resolve_password(),
        host=conn.host,
        port=conn.port_or_default,
        service_name=conn.database,
        tcp_connect_timeout=10,
    )


def _oracle_arrow_type(column) -> pa.DataType:
    """A column's Arrow type from the description alone: for an empty result, with no
    batch to take it from. The driver's own map."""
    nome, precision, scala = _oracle_type(column[1]), column[4], column[5]
    if nome == "DB_TYPE_NUMBER":
        return pa.int64() if scala == 0 and precision and 0 < precision <= 18 else pa.float64()
    if nome == "DB_TYPE_BINARY_FLOAT":
        return pa.float32()
    if nome == "DB_TYPE_BINARY_DOUBLE":
        return pa.float64()
    if nome == "DB_TYPE_BINARY_INTEGER":
        return pa.int64()
    if nome in ("DB_TYPE_DATE", "DB_TYPE_TIMESTAMP") or nome in _ORACLE_WITH_ZONE:
        return pa.timestamp("us")
    if nome == "DB_TYPE_BOOLEAN":
        return pa.bool_()
    if nome in ("DB_TYPE_RAW", "DB_TYPE_LONG_RAW", "DB_TYPE_BLOB"):
        return pa.large_binary()
    if nome in ("DB_TYPE_CLOB", "DB_TYPE_NCLOB", "DB_TYPE_LONG"):
        return pa.large_string()
    return pa.string()


def _oracle_normalize(table: pa.Table) -> pa.Table:
    """DATE comes in seconds, TIMESTAMP(0..3) in seconds or milliseconds: all in
    microseconds as the other drivers (TIMESTAMP(7..9)'s nanoseconds stay)."""
    fields = []
    for f in table.schema:
        if pa.types.is_timestamp(f.type) and f.type.unit in ("s", "ms"):
            fields.append(pa.field(f.name, pa.timestamp("us"), f.nullable))
        else:
            fields.append(f)
    schema = pa.schema(fields)
    return table if schema.equals(table.schema) else table.cast(schema)


def _batches_oracle(conn: DbConnectionSpec, query: str) -> Iterator[Any]:
    c = _oracle_connect(conn)
    try:
        with c.cursor() as cur:
            cur.execute(f"SELECT * FROM ({query}) d WHERE 1 = 0")
            description = list(cur.description or [])
        names = [d[0] for d in description]
        if len(set(names)) != len(names):
            raise DbSourceError("Oracle: the query returns columns with the same name: give them distinct aliases (e.g. a.id AS a_id).")
        refused = [f"{d[0]} ({_oracle_type(d[1]).removeprefix('DB_TYPE_')})" for d in description if _oracle_type(d[1]) in _ORACLE_REFUSED]
        if refused:
            raise DbSourceError(
                "Oracle: these columns have a type that cannot be imported: " + ", ".join(refused)
                + ". Convert them in a SQL source, e.g. TO_CHAR(col) or CAST(col AS VARCHAR2(4000))."
            )
        finale = query
        with_zone = {d[0] for d in description if _oracle_type(d[1]) in _ORACLE_WITH_ZONE}
        if with_zone:
            base, order = query, None
            m = _ORDER_BY_FINALE.match(query)
            if m:
                base, order = m.group(1), m.group(2)
            columns = ", ".join(
                f'SYS_EXTRACT_UTC(o."{n}") AS "{n}"' if n in with_zone else f'o."{n}"' for n in names
            )
            finale = f"SELECT {columns} FROM ({base}) o" + (f" ORDER BY {order}" if order else "")

        first = True
        for block in c.fetch_df_batches(statement=finale, size=BATCH_ROWS):
            table = _oracle_normalize(pa.table(block))
            if table.num_rows == 0 and not first:
                continue
            if first:
                yield table.schema
                first = False
            yield from table.to_batches()
        if first:  # no rows: the schema from the description alone
            yield pa.schema([pa.field(d[0], _oracle_arrow_type(d)) for d in description])
    finally:
        c.close()


# ── SQL Server ──────────────────────────────────────────────────────────────
# mssql-python, Microsoft's official driver: it brings its ODBC layer (DDBC) along,
# no driver to install. It is OPTIONAL (the `sqlserver` extra): the published
# images do not have it, see OPTIONAL_DRIVERS. `arrow_reader` converts the result to
# Arrow in its C++ and hands it over one block at a time. The types match the other
# drivers: integer decimals up to 18 digits → Int64, the other decimals and money →
# Float64 (as Oracle's NUMBER and Postgres's numeric); datetimeoffset comes in UTC
# already and loses only the zone (the engines' standard: naive UTC instants).
_TLS_SQLSERVER = {
    "verify": "Encrypt=yes;TrustServerCertificate=no",   # default: encrypted, certificate verified
    "trust": "Encrypt=yes;TrustServerCertificate=yes",   # the server's self-signed certificate
    "none": "Encrypt=no",
}


def _odbc_value(v: str) -> str:
    """A value of an ODBC connection string: in braces, the closing braces doubled, so
    `;` `=` and `}` in a password break nothing."""
    return "{" + str(v).replace("}", "}}") + "}"


def _sqlserver_connect(conn: DbConnectionSpec):
    try:
        import mssql_python
    except ModuleNotFoundError as e:
        # only the driver itself missing means «not installed»; a module IT needs
        # missing is a broken installation, and says so with its own name
        if (e.name or "").split(".")[0] != "mssql_python":
            raise
        raise driver_not_installed("sqlserver") from e

    tls = (conn.extra or {}).get("tls") or "verify"
    if tls not in _TLS_SQLSERVER:
        raise DbSourceError(f"SQL Server: unknown TLS option {tls!r} (verify, trust, none).")
    # a named instance (`host\\SQLEXPRESS`) is reached without a port (SQL Browser)
    server = conn.host if ("\\" in conn.host and not conn.port) else f"{conn.host},{conn.port_or_default}"
    parts = [f"Server={_odbc_value(server)}", f"UID={_odbc_value(conn.username)}", f"PWD={_odbc_value(conn.resolve_password())}"]
    if conn.database:
        parts.append(f"Database={_odbc_value(conn.database)}")
    string = ";".join(parts) + ";" + _TLS_SQLSERVER[tls]
    return mssql_python.connect(string, attrs_before={mssql_python.SQL_ATTR_LOGIN_TIMEOUT: 10})


def _sqlserver_type(t: pa.DataType) -> pa.DataType:
    if pa.types.is_decimal(t):
        return pa.int64() if t.scale == 0 and t.precision <= 18 else pa.float64()
    if pa.types.is_timestamp(t) and t.tz is not None:
        return pa.timestamp(t.unit)
    return t


def _sqlserver_schema(schema: pa.Schema) -> pa.Schema:
    return pa.schema([pa.field(f.name, _sqlserver_type(f.type), f.nullable) for f in schema])


def _batches_sqlserver(conn: DbConnectionSpec, query: str) -> Iterator[Any]:
    c = _sqlserver_connect(conn)
    try:
        cur = c.cursor()
        cur.execute(query)
        names = [d[0] for d in (cur.description or [])]
        if len(set(names)) != len(names):
            raise DbSourceError("SQL Server: the query returns columns with the same name: give them distinct aliases (e.g. a.id AS a_id).")
        with cur.arrow_reader(batch_size=BATCH_ROWS) as reader:
            schema = _sqlserver_schema(reader.schema)
            yield schema
            for batch in reader:
                yield batch if batch.schema.equals(schema) else batch.cast(schema)
    finally:
        c.close()


_DRIVERS = {
    "postgresql": _batches_postgresql,
    "mysql": _batches_mysql,
    "mariadb": _batches_mysql,  # shared protocol: the same driver
    "clickhouse": _batches_clickhouse,
    "trino": _batches_trino,
    "oracle": _batches_oracle,
    "sqlserver": _batches_sqlserver,
}


def _open_batches(conn: DbConnectionSpec, query: str, read_timeout: int | None = None) -> Iterator[Any]:
    # of the sources' drivers only clickhouse-connect has a read timeout of its own
    # (see IngestSettings.db_read_timeout_seconds)
    driver = _DRIVERS[conn.db_type]
    if read_timeout and conn.db_type == "clickhouse":
        return driver(conn, query, read_timeout=read_timeout)
    return driver(conn, query)


# ─────────────────────────────────────────────────────────────────────────────
# The generic DBAPI path: Python rows → Arrow batches
# ─────────────────────────────────────────────────────────────────────────────
def _dbapi_batches(cursor, batch_rows: int = BATCH_ROWS) -> Iterator[Any]:
    """fetchmany → RecordBatch. The schema is inferred from the FIRST batch and then
    imposed on all the next (the ParquetWriter cannot change schema on the way):
    incompatible values further on give a clear error."""
    names = [d[0] for d in (cursor.description or [])]
    if len(set(names)) != len(names):
        raise DbSourceError(
            "The query returns columns with the same name: give them distinct aliases (e.g. a.id AS a_id)"
        )

    rows = cursor.fetchmany(batch_rows)
    if not rows:
        # no rows: DBAPI does not give the types reliably → all strings
        yield pa.schema([pa.field(n, pa.string()) for n in names])
        return

    first = _rows_to_table(rows, names, schema=None)
    schema = _with_declared_decimals(_null_types_to_string(first.schema), _declared_decimals(cursor))
    yield schema
    yield from first.cast(schema).to_batches()

    while True:
        rows = cursor.fetchmany(batch_rows)
        if not rows:
            break
        yield from _rows_to_table(rows, names, schema=schema).to_batches()


_MYSQL_DECIMAL = (0, 246)  # FIELD_TYPE.DECIMAL, FIELD_TYPE.NEWDECIMAL
_MYSQL_UNSIGNED = 32


def _declared_decimals(cursor) -> dict[str, tuple[int, int]]:
    """{column: (precision, scale)} of the DECIMAL columns as the database declares
    them. Inferring them from the first batch's values gives a type too narrow for
    larger values later on, and the writer cannot widen it mid-stream."""
    fields = getattr(getattr(cursor, "_result", None), "fields", None)
    if fields:
        # pymysql (MySQL/MariaDB): `length` is the display width = digits, plus the
        # decimal point when there is a scale, plus the sign unless UNSIGNED
        return {
            f.name: (f.length - (1 if f.scale else 0) - (0 if f.flags & _MYSQL_UNSIGNED else 1), f.scale)
            for f in fields if f.type_code in _MYSQL_DECIMAL and f.length
        }
    out = {}
    for d in cursor.description or []:
        precision, scale = (d[4], d[5]) if len(d) >= 6 else (None, None)
        if isinstance(precision, int) and isinstance(scale, int) and 0 < precision and 0 <= scale <= precision:
            out[d[0]] = (precision, scale)
    return out


def _with_declared_decimals(schema: pa.Schema, declared: dict[str, tuple[int, int]]) -> pa.Schema:
    """A declared DECIMAL column gets its declared precision, also when the first batch
    held only NULLs (typed as string by _null_types_to_string)."""
    fields = []
    for f in schema:
        if f.name in declared and (pa.types.is_decimal(f.type) or f.type == pa.string()):
            p, s = declared[f.name]
            f = pa.field(f.name, pa.decimal128(p, s) if p <= 38 else pa.decimal256(p, s), f.nullable)
        fields.append(f)
    return pa.schema(fields)


def _rows_to_table(rows, names: list[str], schema: pa.Schema | None) -> pa.Table:
    pylist = [dict(zip(names, row)) for row in rows]
    try:
        return pa.Table.from_pylist(pylist, schema=schema)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as e:
        raise DbSourceError(
            f"Inconsistent types in the result: {e}. "
            "Tip: force the type with an explicit CAST in the query."
        ) from e


def _null_types_to_string(schema: pa.Schema) -> pa.Schema:
    """Columns all NULL in the first batch → string (the null type cannot hold real
    values in the next batches)."""
    fields = [
        pa.field(f.name, pa.string()) if pa.types.is_null(f.type) else f for f in schema
    ]
    return pa.schema(fields)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point: the whole ingest to parquet on the storage
# ─────────────────────────────────────────────────────────────────────────────
def _guarded(gen: Iterator[Any], conn: DbConnectionSpec) -> Iterator[Any]:
    """Iterates the driver's generator, turning its errors into the SERVER's message (see
    db_errors). Only the calls to the driver: an error writing the parquet or the
    storage stays the original. The original cause stays attached (`from e`) and ends
    in the run's traceback."""
    while True:
        try:
            item = next(gen)
        except StopIteration:
            return
        except IngestError:
            raise
        except Exception as e:
            raise DbSourceError(describe_db_error(e, conn.db_type, conn.host, conn.port_or_default)) from e
        yield item


def ingest_db_to_parquet(
    conn: DbConnectionSpec,
    source: DbSourceSpec,
    bucket: str,
    key: str,
    storage=None,
    progress: Callable[..., None] | None = None,
    cap: int | None = None,
) -> dict:
    """Runs the source and writes `bucket/key` (parquet). Returns the rows and the schema.
    With `cap` the database is asked for at most that many rows (a SQL script's export).

    `progress(phase, rows=…)`, for whoever watches the import: «waiting» until the
    database sends the first row (connection, computing, sorting), «reading» at every
    batch with the row count, «saving» while the parquet goes to the storage."""
    advances = progress or (lambda *a, **k: None)
    if storage is None:
        from app.utils import get_storage_service

        storage = get_storage_service()

    query = build_query(conn, source)
    if cap:
        query = with_row_cap(conn, query, cap)
    logger.info("db-ingest %s@%s → %s (%s)", conn.db_type, conn.host, key, query[:200])

    # the driver's errors → the database server's message, not the Python exception
    gen = _guarded(_open_batches(conn, query, get_settings().ingest.db_read_timeout_seconds), conn)
    advances("waiting")
    try:
        schema = next(gen)
    except StopIteration:  # no driver gets here, but the contract is defended
        raise DbSourceError("The driver returned no schema")

    fd, tmp = tempfile.mkstemp(suffix=".parquet", prefix="db_ingest_")
    os.close(fd)
    try:
        rows = 0
        # The driver's batches are small (~8k rows) and `write_batch` writes ONE ROW
        # GROUP PER BATCH: a parquet of 25M rows ended with 3,063 row groups and a
        # 16.6 MB footer. With blocks that small the engine cannot read only the
        # columns it needs — thousands of requests of a few tens of KB — and ends up
        # downloading the whole file. Here the batches BUILD UP to the threshold and
        # are written together.
        # A block is written with EXACTLY `threshold` rows and the rest goes to the next:
        # a driver batch larger than the threshold (Postgres hands over tens of
        # thousands at once) came out as one row group, and one just above left a tiny
        # row group every time.
        threshold = max(1, get_settings().ingest.parquet_row_group_rows)
        with pq.ParquetWriter(tmp, schema, compression="zstd") as writer:
            block: list = []
            in_block = 0

            def _download(fine: bool = False) -> None:
                nonlocal block, in_block
                if not block:
                    return
                table = pa.Table.from_batches(block, schema)
                while table.num_rows >= threshold or (fine and table.num_rows):
                    n = min(threshold, table.num_rows)
                    writer.write_table(table.slice(0, n), row_group_size=n)
                    table = table.slice(n)
                block, in_block = table.to_batches(), table.num_rows

            for batch in gen:
                block.append(batch)
                in_block += batch.num_rows
                rows += batch.num_rows
                advances("reading", rows=rows)
                if in_block >= threshold:
                    _download()
            _download(fine=True)  # the last block, nearly always shorter than the threshold

        advances("saving", rows=rows)
        storage.create_bucket(bucket)
        storage.upload_file(tmp, bucket, key)

        # dtypes as Polars strings: consistent with the rest of the catalog and the frontend
        scan_schema = pl.scan_parquet(tmp).collect_schema()
        columns = [{"name": n, "dtype": str(t)} for n, t in scan_schema.items()]
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    logger.info("db-ingest done: %s (%d rows, %d columns)", key, rows, len(columns))
    return {"rows_written": rows, "columns": columns}


# ─────────────────────────────────────────────────────────────────────────────
# Inspection: connection test and table listing (for the UI)
# ─────────────────────────────────────────────────────────────────────────────
_TABLES_QUERY = {
    "postgresql": (
        "SELECT table_schema || '.' || table_name AS t FROM information_schema.tables "
        "WHERE table_type IN ('BASE TABLE', 'VIEW') "
        "AND table_schema NOT IN ('pg_catalog', 'information_schema') ORDER BY 1"
    ),
    "mysql": (
        "SELECT table_name AS t FROM information_schema.tables "
        "WHERE table_schema = DATABASE() ORDER BY 1"
    ),
    "mariadb": (
        "SELECT table_name AS t FROM information_schema.tables "
        "WHERE table_schema = DATABASE() ORDER BY 1"
    ),
    "clickhouse": "SELECT name AS t FROM system.tables WHERE database = currentDatabase() ORDER BY 1",
    "trino": (
        "SELECT table_name AS t FROM information_schema.tables "
        "WHERE table_schema = current_schema ORDER BY 1"
    ),
    # tables and views of the non-system schemas (Oracle 12c+: oracle_maintained), in
    # the form build_query reads back: a plain uppercase name bare, the rest quoted
    "oracle": (
        "SELECT CASE WHEN REGEXP_LIKE(owner, '^[A-Z][A-Z0-9_$#]*$') THEN owner ELSE '\"' || owner || '\"' END"
        " || '.' || CASE WHEN REGEXP_LIKE(nome, '^[A-Z][A-Z0-9_$#]*$') THEN nome ELSE '\"' || nome || '\"' END AS t"
        " FROM (SELECT owner, table_name AS nome FROM all_tables WHERE dropped = 'NO'"
        " UNION ALL SELECT owner, view_name FROM all_views)"
        " WHERE owner IN (SELECT username FROM all_users WHERE oracle_maintained = 'N') ORDER BY 1"
    ),
    # tables and views of the connection's database; a name with special characters
    # comes back in square brackets, the form build_query reads back
    "sqlserver": (
        "SELECT CASE WHEN TABLE_SCHEMA LIKE '%[^A-Za-z0-9_]%' THEN QUOTENAME(TABLE_SCHEMA) ELSE TABLE_SCHEMA END"
        " + '.' + CASE WHEN TABLE_NAME LIKE '%[^A-Za-z0-9_]%' OR TABLE_NAME LIKE '[0-9]%' THEN QUOTENAME(TABLE_NAME) ELSE TABLE_NAME END AS t"
        " FROM INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE IN ('BASE TABLE', 'VIEW')"
        " AND TABLE_SCHEMA NOT IN ('sys', 'INFORMATION_SCHEMA') ORDER BY 1"
    ),
}


def with_row_cap(conn: DbConnectionSpec, query: str, rows: int) -> str:
    """`query` (as build_query makes it) reading at most `rows` rows, so the database
    stops there instead of computing what nobody reads. SQL Server's user SQL that
    build_query left unwrapped (a CTE, an ORDER BY) gets no cap: the reader stops
    after `rows` rows."""
    if conn.db_type == "oracle":
        return f"{query} FETCH FIRST {rows} ROWS ONLY"
    if conn.db_type == "sqlserver":
        if query.startswith("SELECT * FROM "):
            return f"SELECT TOP {rows} * FROM " + query[len("SELECT * FROM "):]
        return query
    return f"{query} LIMIT {rows}"


def _read_some(conn: DbConnectionSpec, query: str, limit: int) -> tuple[pa.Schema, list]:
    """The schema and the batches of the first `limit` rows (and one more, when there
    is one: it tells a result that goes on). The connection closes as soon as they are
    read."""
    gen = _guarded(_open_batches(conn, query, get_settings().ingest.db_read_timeout_seconds), conn)
    try:
        schema = next(gen)
        batches, rows = [], 0
        for batch in gen:
            batches.append(batch)
            rows += batch.num_rows
            if rows > limit:
                break
        return schema, batches
    except StopIteration:
        raise DbSourceError("The driver returned no schema")
    finally:
        gen.close()


def query_rows(conn: DbConnectionSpec, sql: str, limit: int) -> tuple[pl.DataFrame, bool]:
    """A SQL script run on a connection: the first `limit` rows of `sql` and whether
    there are more. One read statement, wrapped as a SQL datasource's (build_query):
    not a security barrier, which is the CONNECT capability and a read-only user."""
    query = build_query(conn, DbSourceSpec(mode="sql", ref=sql))
    logger.info("db-query %s@%s (%s)", conn.db_type, conn.host, query[:200])
    schema, batches = _read_some(conn, with_row_cap(conn, query, limit + 1), limit)
    df = pl.from_arrow(pa.Table.from_batches(batches, schema))
    return df.head(limit), df.height > limit


def list_columns(conn: DbConnectionSpec, table: str) -> list[dict]:
    """The columns of a table, with their types, as the import would read them: from
    the schema of a read of one row."""
    query = build_query(conn, DbSourceSpec(mode="table", ref=table))
    schema, batches = _read_some(conn, with_row_cap(conn, query, 1), 1)
    df = pl.from_arrow(pa.Table.from_batches(batches, schema))
    return [{"name": n, "dtype": str(t)} for n, t in df.schema.items()]


def _collect_first_column(conn: DbConnectionSpec, query: str) -> list[str]:
    gen = _open_batches(conn, query)
    next(gen)  # schema
    out: list[str] = []
    for batch in gen:
        out.extend(str(v) for v in batch.column(0).to_pylist())
    return out


def test_connection(conn: DbConnectionSpec) -> None:
    """Opens the connection and runs a trivial query on the ingest's own path: if it does
    not raise, the connection is usable."""
    _collect_first_column(conn, "SELECT 1 AS ok FROM DUAL" if conn.db_type == "oracle" else "SELECT 1 AS ok")


def list_tables(conn: DbConnectionSpec) -> list[str]:
    return _collect_first_column(conn, _TABLES_QUERY[conn.db_type])
