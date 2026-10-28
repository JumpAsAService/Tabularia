"""Database destinations: writes a parquet of the storage into a table, streaming
(Arrow batches → INSERT), never holding all of it in memory.

UNIFORM semantics on every dialect (the Output node's contract):
1. CREATE TABLE IF NOT EXISTS with types mapped from the Arrow schema;
2. mode="replace" → the table emptied (its definition stays, the data does not);
   mode="append"  → appended to what is there;
3. streaming inserts in batches;
4. any post-insert statements (separated by `;`), on the same connection.

Columns are matched by NAME on every database: a column the existing table lacks
stops the write, columns the flow lacks take their DEFAULT, and the values take the
table's types as on any INSERT. On ClickHouse "replace" loads a staging copy of the
target's own definition and swaps it in atomically, so engine, ORDER BY, partitions,
TTL and defaults stay the customer's (materialized views on the target see inserts,
not swaps: they do not see replaced data).

A driver per dialect (the sources' own):
- postgresql        → ADBC (adbc_ingest: bulk COPY, native Arrow) into a temporary
                      table of the session, then one INSERT … SELECT by column name
- clickhouse        → clickhouse-connect (insert_arrow)
- mysql / mariadb   → PyMySQL executemany in chunks
- trino             → a parametric multi-row INSERT (slow by nature: Trino is not meant
                      for OLTP, but it works)
- oracle            → python-oracledb (Thin) executemany: array DML in chunks
- sqlserver         → mssql-python executemany: column-wise parameter arrays in chunks
                      (the optional driver, as for reading)

Oracle and SQL Server have no CREATE TABLE IF NOT EXISTS (before Oracle 23ai): the table
is looked up first and created only when missing. Names follow each one's rules, as when
reading: on Oracle an unquoted name is UPPERCASE (`vendite` is VENDITE), on SQL Server
names go in square brackets.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import re
import tempfile
from typing import Iterator, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel

from app.core.config import get_settings
from app.ingest.converters import IngestError
from app.ingest.db_errors import describe_db_error
from app.ingest.db_source import (
    _IDENT_QUOTE, BATCH_ROWS, DbConnectionSpec, _clickhouse_client, _mysql_connect, _oracle_connect, _oracle_ident,
    _pg_connect, _sqlserver_connect, _trino_connect, _tsql_ident,
)

logger = logging.getLogger(__name__)

# rows per INSERT on the paths without a native bulk load
MYSQL_CHUNK_ROWS = 5_000  # multi-row VALUES: under max_allowed_packet
TRINO_CHUNK_ROWS = 500  # every INSERT is a Trino query: small chunks
ORACLE_CHUNK_ROWS = 10_000  # one array DML round trip per chunk
SQLSERVER_CHUNK_ROWS = 10_000  # the same, with the driver's column-wise parameter arrays


class DbDestinationError(IngestError):
    """A clear error to show the user (table, types, database permissions)."""


class DbDestinationSpec(BaseModel):
    table: str  # the table's name (schema.table too)
    mode: Literal["append", "replace"] = "append"
    post_sql: str = ""  # post-insert statements separated by `;` (optional)


def _qualified_table(conn: DbConnectionSpec, table_ref: str) -> tuple[str, list[str]]:
    """The table name quoted per dialect + the unquoted parts (schema, table)."""
    parts = [p.strip() for p in table_ref.split(".") if p.strip()]
    if not parts:
        raise DbDestinationError("The destination table name is empty")
    if conn.db_type in ("oracle", "sqlserver"):
        # each with the rules of reading (db_source): Oracle's uppercase, T-SQL's brackets
        if len(parts) == 1 and conn.db_schema:
            parts = [conn.db_schema, parts[0]]
        quoted = [(_oracle_ident if conn.db_type == "oracle" else _tsql_ident)(p) for p in parts]
        return ".".join(quoted), [p[1:-1] for p in quoted]
    q = _IDENT_QUOTE[conn.db_type]
    if any(q in p for p in parts):
        raise DbDestinationError(f"Invalid table name: it contains {q!r}")
    if len(parts) == 1 and conn.db_schema:
        parts = [conn.db_schema, parts[0]]
    return ".".join(f"{q}{p}{q}" for p in parts), parts


def _post_statements(post_sql: str) -> list[str]:
    return [s.strip() for s in (post_sql or "").split(";") if s.strip()]


# ─────────────────────────────────────────────────────────────────────────────
# Arrow types → SQL, per dialect
# ─────────────────────────────────────────────────────────────────────────────
def _sql_type(db_type: str, t: pa.DataType) -> str:
    ch = db_type == "clickhouse"
    if pa.types.is_boolean(t):
        base = "Bool" if ch else "BOOLEAN"
    elif pa.types.is_integer(t):
        wide = t.bit_width >= 64 or pa.types.is_unsigned_integer(t)
        base = ("Int64" if wide else "Int32") if ch else ("BIGINT" if wide else "INTEGER")
    elif pa.types.is_floating(t):
        base = {"postgresql": "DOUBLE PRECISION", "clickhouse": "Float64"}.get(db_type, "DOUBLE")
    elif pa.types.is_decimal(t):
        base = f"Decimal({t.precision}, {t.scale})" if ch else f"DECIMAL({t.precision}, {t.scale})"
    elif pa.types.is_timestamp(t):
        base = {
            "postgresql": "TIMESTAMP",
            "mysql": "DATETIME(6)",
            "mariadb": "DATETIME(6)",
            # naive timestamps are UTC (the project's standard): said so, or the
            # column shows and parses them in the server's own time zone
            "clickhouse": "DateTime64(6, 'UTC')",
            "trino": "TIMESTAMP(6)",
        }[db_type]
    elif pa.types.is_date(t):
        base = "Date32" if ch else "DATE"
    else:  # strings and everything else (time, binary…): text
        base = {"postgresql": "TEXT", "mysql": "TEXT", "mariadb": "TEXT",
                "clickhouse": "String", "trino": "VARCHAR"}[db_type]
    return f"Nullable({base})" if ch else base


def _create_table_sql(conn: DbConnectionSpec, qualified: str, schema: pa.Schema) -> str:
    q = _IDENT_QUOTE[conn.db_type]
    for f in schema:
        if q in f.name:
            raise DbDestinationError(f"Invalid column name for {conn.db_type}: {f.name!r}")
    cols = ", ".join(f"{q}{f.name}{q} {_sql_type(conn.db_type, f.type)}" for f in schema)
    if conn.db_type == "clickhouse":
        return f"CREATE TABLE IF NOT EXISTS {qualified} ({cols}) ENGINE = MergeTree ORDER BY tuple()"
    return f"CREATE TABLE IF NOT EXISTS {qualified} ({cols})"


def _quoted_columns(conn: DbConnectionSpec, schema: pa.Schema) -> str:
    q = _IDENT_QUOTE[conn.db_type]
    return ", ".join(f"{q}{f.name}{q}" for f in schema)


# ─────────────────────────────────────────────────────────────────────────────
# A writer per dialect: the same signature, the same semantics
# ─────────────────────────────────────────────────────────────────────────────
def _write_postgresql(
    conn: DbConnectionSpec, dest: DbDestinationSpec, qualified: str, parts: list[str],
    schema: pa.Schema, batches: Iterator[pa.RecordBatch],
) -> int:
    import uuid

    staging = f"tabularia_stg_{uuid.uuid4().hex[:12]}"  # lowercase: the same name quoted or not
    columns = _quoted_columns(conn, schema)
    rows = [0]
    with _pg_connect(conn) as c:
        with c.cursor() as cur:
            # naive timestamps are UTC: a customer's timestamptz column reads them in the
            # session's zone, which would otherwise be the server's
            cur.execute("SET TIME ZONE 'UTC'")
            cur.execute(_create_table_sql(conn, qualified, schema))
            if dest.mode == "replace":
                cur.execute(f"TRUNCATE TABLE {qualified}")
            # Binary COPY wants each column in the table's exact type: the flow's BIGINT
            # into a customer's INTEGER column, or a float into a NUMERIC, fails. So COPY
            # fills a temporary table of this session (no one else sees it, it goes with
            # the connection) and one INSERT … SELECT by column name moves the rows,
            # converting them as any INSERT does.
            reader = pa.RecordBatchReader.from_batches(schema, _counted(batches, rows))
            cur.adbc_ingest(staging, reader, mode="create", temporary=True)
            cur.execute(f"INSERT INTO {qualified} ({columns}) SELECT {columns} FROM {staging}")
            cur.execute(f"DROP TABLE {staging}")
            for stmt in _post_statements(dest.post_sql):
                cur.execute(stmt)
        c.commit()
    return rows[0]


def _write_clickhouse(
    conn: DbConnectionSpec, dest: DbDestinationSpec, qualified: str, parts: list[str],
    schema: pa.Schema, batches: Iterator[pa.RecordBatch],
) -> int:
    # INSERT … SELECT from the staging and the post-SQL (an OPTIMIZE) may stay silent for long
    with _clickhouse_client(conn, send_receive_timeout=get_settings().ingest.db_read_timeout_seconds) as client:
        return _write_clickhouse_with(client, conn, dest, qualified, parts, schema, batches)


def _write_clickhouse_with(
    client, conn: DbConnectionSpec, dest: DbDestinationSpec, qualified: str, parts: list[str],
    schema: pa.Schema, batches: Iterator[pa.RecordBatch],
) -> int:
    # ClickHouse has no transactions on a streaming INSERT: writing straight into the
    # table (TRUNCATE + batched inserts) would leave it PARTIAL if something fails half
    # way — with replace, even empty (data lost). So everything is written to a STAGING
    # table; then the swap is ATOMIC.
    import uuid

    q = _IDENT_QUOTE["clickhouse"]
    stg_parts = parts[:-1] + [f"{parts[-1]}__tab_stg_{uuid.uuid4().hex[:8]}"]
    stg_qualified = ".".join(f"{q}{p}{q}" for p in stg_parts)
    columns = _quoted_columns(conn, schema)
    rows = 0
    try:
        existing = _clickhouse_columns(client, parts)
        if not existing:
            # the first write: the table takes the flow's shape
            client.command(_create_table_sql(conn, qualified, schema))
        else:
            # the customer's table: columns are matched by name, as on every other
            # database, so one the table lacks would land nowhere (or, by position, in
            # the wrong column). Columns the flow lacks take their DEFAULT.
            missing = [f.name for f in schema if f.name not in existing]
            if missing:
                raise DbDestinationError(
                    f"Column(s) {', '.join(missing)} of the output are not in the table {qualified}: "
                    "add them to the table or rename them in the flow"
                )
        if dest.mode == "replace":
            # the staging copies the target's own definition (engine, ORDER BY,
            # partitions, TTL, defaults, comment): the swap changes the data only
            client.command(f"CREATE TABLE {stg_qualified} AS {qualified}")
        else:
            client.command(_create_table_sql(conn, stg_qualified, schema))
        for batch in batches:
            # insert_arrow names the columns: they land by name in the staging. It puts the
            # table's name in the INSERT as it gets it: quoted, as everywhere else
            client.insert_arrow(stg_qualified, pa.Table.from_batches([batch], schema=schema))
            rows += batch.num_rows
        if dest.mode == "replace":
            # ATOMIC swap: the destination goes from the old data to the new in one stroke
            client.command(f"EXCHANGE TABLES {qualified} AND {stg_qualified}")
        else:
            # append: one server-side INSERT, by column name
            client.command(f"INSERT INTO {qualified} ({columns}) SELECT {columns} FROM {stg_qualified}")
        for stmt in _post_statements(dest.post_sql):
            try:
                client.command(stmt)
            except Exception as e:
                # ClickHouse has no transaction: the rows are in the table already, and
                # a plain "failed" made people run the flow again, appending them twice
                raise DbDestinationError(_post_sql_failed(rows, qualified, dest.mode, describe_db_error(
                    e, conn.db_type, conn.host, conn.port_or_default))) from e
    finally:
        try:
            client.command(f"DROP TABLE IF EXISTS {stg_qualified}")  # staging (old data, or copied already)
        except Exception:
            pass
    return rows


def _post_sql_failed(rows: int, qualified: str, mode: str, error: str) -> str:
    again = ("Do not run the flow again as it is: it would add the same rows a second time. "
             "Fix the post-SQL and run it on the database yourself."
             if mode == "append" else
             "Running the flow again replaces the data again, so it is safe once the post-SQL is fixed.")
    return f"The data was written: {rows} rows are in {qualified}. Only the post-SQL failed ({error}). {again}"


def _clickhouse_columns(client, parts: list[str]) -> list[str]:
    """The target's column names, or [] when the table does not exist yet."""
    if len(parts) > 1:
        where, params = "database = {db:String}", {"db": parts[-2], "t": parts[-1]}
    else:
        where, params = "database = currentDatabase()", {"t": parts[-1]}
    result = client.query(
        f"SELECT name FROM system.columns WHERE {where} AND table = {{t:String}} ORDER BY position",
        parameters=params,
    )
    return [row[0] for row in result.result_rows]


def _write_mysql(
    conn: DbConnectionSpec, dest: DbDestinationSpec, qualified: str, parts: list[str],
    schema: pa.Schema, batches: Iterator[pa.RecordBatch],
) -> int:
    # naive timestamps are UTC: a TIMESTAMP column would otherwise read them in the
    # server's zone and store an instant one or two hours off
    c = _mysql_connect(conn, init_command="SET time_zone = '+00:00'")
    placeholders = ", ".join(["%s"] * len(schema))
    insert = f"INSERT INTO {qualified} ({_quoted_columns(conn, schema)}) VALUES ({placeholders})"
    rows = 0
    try:
        with c.cursor() as cur:
            cur.execute(_create_table_sql(conn, qualified, schema))
            if dest.mode == "replace":
                # DELETE (DML, transactional) and NOT TRUNCATE: in MySQL/MariaDB TRUNCATE
                # is DDL and makes an implicit COMMIT, so it would empty the table OUTSIDE
                # the INSERTs' transaction — an error half way through the stream would
                # leave the table empty for good. With DELETE the emptying and the inserts
                # commit (or roll back) together.
                cur.execute(f"DELETE FROM {qualified}")
            for batch in batches:
                for chunk in _row_chunks(batch, schema, MYSQL_CHUNK_ROWS):
                    cur.executemany(insert, chunk)
                    rows += len(chunk)
            for stmt in _post_statements(dest.post_sql):
                cur.execute(stmt)
        c.commit()
    finally:
        c.close()
    return rows


def _write_trino(
    conn: DbConnectionSpec, dest: DbDestinationSpec, qualified: str, parts: list[str],
    schema: pa.Schema, batches: Iterator[pa.RecordBatch],
) -> int:
    import trino

    def _run(cur, sql: str, params=None) -> None:
        cur.execute(sql, params) if params else cur.execute(sql)
        cur.fetchall()  # the Trino client completes the query only by consuming it

    one_row = "(" + ", ".join(["?"] * len(schema)) + ")"
    prefix = f"INSERT INTO {qualified} ({_quoted_columns(conn, schema)}) VALUES "
    rows = 0
    # an explicit transaction: DELETE + INSERT must be atomic, otherwise an error half
    # way leaves the table emptied or partial. NB: transactions in Trino depend on the
    # CONNECTOR (Iceberg/Delta support them, plain Hive does not); where they are not
    # supported commit/rollback degrade to the previous behavior.
    with _trino_connect(conn, isolation_level=trino.transaction.IsolationLevel.READ_UNCOMMITTED) as c:
        try:
            cur = c.cursor()
            _run(cur, _create_table_sql(conn, qualified, schema))
            c.commit()  # the DDL outside the data's transaction
            if dest.mode == "replace":
                _run(cur, f"DELETE FROM {qualified}")  # Trino does not have TRUNCATE everywhere
            for batch in batches:
                for chunk in _row_chunks(batch, schema, TRINO_CHUNK_ROWS):
                    sql = prefix + ", ".join([one_row] * len(chunk))
                    _run(cur, sql, [v for row in chunk for v in row])
                    rows += len(chunk)
            for stmt in _post_statements(dest.post_sql):
                _run(cur, stmt)
            c.commit()  # DELETE + INSERT together: all or nothing
        except Exception:
            try:
                c.rollback()
            except Exception:
                pass
            raise
    return rows


# ── Oracle ───────────────────────────────────────────────────────────────────
_ORACLE_SIMPLE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]*$")


def _oracle_sql_type(t: pa.DataType) -> str:
    if pa.types.is_boolean(t):
        return "NUMBER(1)"
    if pa.types.is_integer(t):
        if pa.types.is_unsigned_integer(t) and t.bit_width >= 64:
            return "NUMBER(20)"
        # as the other databases: unsigned 32-bit does not fit a signed one
        return "NUMBER(19)" if t.bit_width >= 64 or pa.types.is_unsigned_integer(t) else "NUMBER(10)"
    if pa.types.is_floating(t):
        return "BINARY_DOUBLE"
    if pa.types.is_decimal(t):
        return f"NUMBER({t.precision}, {t.scale})" if t.precision <= 38 else "NUMBER"
    if pa.types.is_timestamp(t):
        return "TIMESTAMP(6)"
    if pa.types.is_date(t):
        return "DATE"
    # the longest text a standard Oracle keeps in a VARCHAR2: a longer value stops the
    # write with ORA-12899, and needs a table created beforehand with a CLOB column
    return "VARCHAR2(4000)"


def _oracle_column_names(schema: pa.Schema, existing: list[str], qualified: str) -> list[str]:
    """The table's own name for each output column. A new table takes Oracle's
    convention (a plain name in UPPERCASE, so `SELECT regione` works); an existing one is
    matched by name ignoring case, as an unquoted name would be."""
    for f in schema:
        if '"' in f.name:
            raise DbDestinationError(f"Invalid column name for oracle: {f.name!r}")
    if not existing:
        return [f.name.upper() if _ORACLE_SIMPLE_NAME.match(f.name) else f.name for f in schema]
    by_upper: dict[str, str] = {}
    for name in existing:
        by_upper.setdefault(name.upper(), name)
    names, missing = [], []
    for f in schema:
        if f.name in existing:
            names.append(f.name)
        elif f.name.upper() in by_upper:
            names.append(by_upper[f.name.upper()])
        else:
            missing.append(f.name)
    if missing:
        raise DbDestinationError(
            f"Column(s) {', '.join(missing)} of the output are not in the table {qualified}: "
            "add them to the table or rename them in the flow"
        )
    return names


_ORACLE_COLUMNS = "SELECT column_name FROM all_tab_columns WHERE owner = {} AND table_name = :t ORDER BY column_id"


def _oracle_existing_columns(cur, parts: list[str]) -> list[str]:
    """The columns of the table the name reaches, [] when there is none. A synonym counts
    as Oracle resolves it (the schema's own, then a PUBLIC one for a name without schema):
    the rows go to its table, whose columns are the ones to match. Without this, writing
    through a synonym tried to create a table of that name (ORA-00955)."""
    owner = ":o" if len(parts) > 1 else "SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA')"
    params = {"t": parts[-1], **({"o": parts[-2]} if len(parts) > 1 else {})}
    cur.execute(_ORACLE_COLUMNS.format(owner), params)
    found = [r[0] for r in cur.fetchall()]
    if found:
        return found
    owners = owner if len(parts) > 1 else f"{owner}, 'PUBLIC'"
    cur.execute(
        f"SELECT table_owner, table_name FROM all_synonyms WHERE owner IN ({owners}) AND synonym_name = :t "
        "ORDER BY CASE owner WHEN 'PUBLIC' THEN 1 ELSE 0 END",
        params,
    )
    target = cur.fetchone()
    if target is None:
        return []
    cur.execute(_ORACLE_COLUMNS.format(":o"), {"o": target[0], "t": target[1]})
    return [r[0] for r in cur.fetchall()]


def _oracle_post_statements(post_sql: str) -> list[str]:
    """The post-SQL statements, split on `;`, except a PL/SQL block (`BEGIN … END;`,
    `DECLARE …`, the usual way to call DBMS_STATS or refresh a materialized view), which
    is ONE statement whose `;` are its own: it runs whole, without SQL*Plus's `/`."""
    text = re.sub(r"\n\s*/\s*$", "", (post_sql or "").strip())
    if re.match(r"(?:BEGIN|DECLARE)\b", text, re.IGNORECASE):
        return [text]
    return _post_statements(text)


def _oracle_post_sql(c, cur, conn: DbConnectionSpec, dest: DbDestinationSpec, qualified: str, rows: int) -> None:
    """Runs the post-SQL in the data's transaction. Oracle commits around every DDL
    (CREATE INDEX, GRANT, DBMS_STATS…): after one the rows are in the table for good, so a
    later failure must not read as «nothing was changed», which made people run an
    append again and get its rows twice."""
    committed = False
    for stmt in _oracle_post_statements(dest.post_sql):
        try:
            cur.execute(stmt)
        except Exception as e:
            if (committed or not c.transaction_in_progress) and (rows or dest.mode == "replace"):
                raise DbDestinationError(_post_sql_failed(rows, qualified, dest.mode, describe_db_error(
                    e, conn.db_type, conn.host, conn.port_or_default))) from e
            raise
        committed = committed or not c.transaction_in_progress


def _write_oracle(
    conn: DbConnectionSpec, dest: DbDestinationSpec, qualified: str, parts: list[str],
    schema: pa.Schema, batches: Iterator[pa.RecordBatch],
) -> int:
    import oracledb

    c = _oracle_connect(conn)
    rows = 0
    try:
        cur = c.cursor()
        existing = _oracle_existing_columns(cur, parts)
        names = _oracle_column_names(schema, existing, qualified)
        columns = ", ".join(f'"{n}"' for n in names)
        if not existing:
            # DDL commits by itself in Oracle: the table is made before the data's transaction
            cols = ", ".join(f'"{n}" {_oracle_sql_type(f.type)}' for n, f in zip(names, schema))
            cur.execute(f"CREATE TABLE {qualified} ({cols})")
        if dest.mode == "replace":
            # DELETE and not TRUNCATE: TRUNCATE is DDL and commits, so a failure half way
            # through the inserts would leave the table empty; DELETE rolls back with them
            cur.execute(f"DELETE FROM {qualified}")
        insert = f"INSERT INTO {qualified} ({columns}) VALUES ({', '.join(f':{i + 1}' for i in range(len(schema)))})"
        # values the driver cannot bind as they come: a boolean goes as 1/0 (NUMBER(1):
        # before 23ai Oracle has no boolean to bind), a time of day as text (Oracle has
        # no such type, a VARCHAR2 column holds it)
        convert = {i: int if pa.types.is_boolean(f.type) else dt.time.isoformat
                   for i, f in enumerate(schema) if pa.types.is_boolean(f.type) or pa.types.is_time(f.type)}
        # a datetime binds as DATE by default, which drops the fractional seconds
        sizes = [oracledb.DB_TYPE_TIMESTAMP if pa.types.is_timestamp(f.type) else None for f in schema]
        for batch in batches:
            for chunk in _row_chunks(batch, schema, ORACLE_CHUNK_ROWS):
                if convert:
                    chunk = [tuple(convert[i](v) if v is not None and i in convert else v for i, v in enumerate(r))
                             for r in chunk]
                cur.setinputsizes(*sizes)
                cur.executemany(insert, chunk)
                rows += len(chunk)
        _oracle_post_sql(c, cur, conn, dest, qualified, rows)
        c.commit()
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        raise
    finally:
        c.close()
    return rows


# ── SQL Server ───────────────────────────────────────────────────────────────
def _sqlserver_sql_type(t: pa.DataType) -> str:
    if pa.types.is_boolean(t):
        return "BIT"
    if pa.types.is_integer(t):
        if pa.types.is_unsigned_integer(t) and t.bit_width >= 64:
            return "DECIMAL(20, 0)"
        return "BIGINT" if t.bit_width >= 64 or pa.types.is_unsigned_integer(t) else "INT"
    if pa.types.is_floating(t):
        return "FLOAT"
    if pa.types.is_decimal(t):
        return f"DECIMAL({t.precision}, {t.scale})" if t.precision <= 38 else "FLOAT"
    if pa.types.is_timestamp(t):
        return "DATETIME2(6)"
    if pa.types.is_date(t):
        return "DATE"
    if pa.types.is_binary(t) or pa.types.is_large_binary(t) or pa.types.is_fixed_size_binary(t):
        return "VARBINARY(MAX)"  # bytes bound into NVARCHAR would turn into garbage text
    return "NVARCHAR(MAX)"


def _write_sqlserver(
    conn: DbConnectionSpec, dest: DbDestinationSpec, qualified: str, parts: list[str],
    schema: pa.Schema, batches: Iterator[pa.RecordBatch],
) -> int:
    # One transaction on one connection: the table made if missing, emptied for
    # «replace», the rows inserted by column name with array-bound executemany, the
    # post-SQL, one commit. NOT the driver's Arrow bulk copy: it runs on a connection of
    # its own, so it can only load a GLOBAL temporary table, which every login of the
    # server can list, read and insert into while the copy runs (GRANT and DENY do not
    # apply to temporary tables): the output would be open to all of them.
    for f in schema:
        if "]" in f.name:
            raise DbDestinationError(f"Invalid column name for sqlserver: {f.name!r}")
    columns = ", ".join(_tsql_ident(f.name) for f in schema)
    shape = ", ".join(f"{_tsql_ident(f.name)} {_sqlserver_sql_type(f.type)}" for f in schema)
    insert = f"INSERT INTO {qualified} ({columns}) VALUES ({', '.join('?' * len(schema))})"
    c = _sqlserver_connect(conn)
    rows = 0
    try:
        c.autocommit = False
        cur = c.cursor()
        cur.execute("SELECT OBJECT_ID(?, 'U')", (qualified,))
        if cur.fetchone()[0] is None:
            cur.execute(f"CREATE TABLE {qualified} ({shape})")
        if dest.mode == "replace":
            # DELETE rather than TRUNCATE: it needs the DELETE permission only (TRUNCATE
            # wants ALTER) and works on a table other tables reference
            cur.execute(f"DELETE FROM {qualified}")
        for batch in batches:
            for chunk in _row_chunks(batch, schema, SQLSERVER_CHUNK_ROWS):
                cur.executemany(insert, chunk)
                rows += len(chunk)
        for stmt in _post_statements(dest.post_sql):
            cur.execute(stmt)
        c.commit()  # T-SQL DDL is transactional: a table made here goes if anything fails
    except Exception:
        try:
            c.rollback()
        except Exception:
            pass
        raise
    finally:
        c.close()
    return rows


_WRITERS = {
    "postgresql": _write_postgresql,
    "mysql": _write_mysql,
    "mariadb": _write_mysql,
    "clickhouse": _write_clickhouse,
    "trino": _write_trino,
    "oracle": _write_oracle,
    "sqlserver": _write_sqlserver,
}


def _counted(batches: Iterator[pa.RecordBatch], counter: list[int]) -> Iterator[pa.RecordBatch]:
    for b in batches:
        counter[0] += b.num_rows
        yield b


def _plain_schema(schema: pa.Schema) -> pa.Schema:
    """The output's schema in the types every writer takes. A timestamp with a zone
    becomes the naive UTC instant, the project's standard (the ClickHouse engine with the
    s3 transport writes them with the UTC zone; a zone other than UTC reached Oracle,
    MySQL and SQL Server as its wall clock): the integer underneath does not change. A
    dictionary column (a Polars Categorical) becomes its values; a column of nulls only,
    text."""
    fields = []
    for f in schema:
        t = f.type.value_type if pa.types.is_dictionary(f.type) else f.type
        if pa.types.is_timestamp(t) and t.tz:
            t = pa.timestamp(t.unit)
        elif pa.types.is_null(t):
            t = pa.string()
        fields.append(f if t == f.type else pa.field(f.name, t, f.nullable))
    return pa.schema(fields)


def _plain_batches(batches: Iterator[pa.RecordBatch], schema: pa.Schema) -> Iterator[pa.RecordBatch]:
    for b in batches:
        yield b if b.schema.equals(schema) else b.cast(schema)


def _row_chunks(batch: pa.RecordBatch, schema: pa.Schema, size: int) -> Iterator[list[tuple]]:
    """RecordBatch → lists of Python tuples (column order = schema)."""
    names = [f.name for f in schema]
    for start in range(0, batch.num_rows, size):
        piece = batch.slice(start, size)
        cols = [piece.column(n).to_pylist() for n in names]
        yield list(zip(*cols)) if cols else []


# ─────────────────────────────────────────────────────────────────────────────
# Entry point: parquet on the storage → table
# ─────────────────────────────────────────────────────────────────────────────
def write_parquet_to_db(
    conn: DbConnectionSpec,
    dest: DbDestinationSpec,
    bucket: str,
    key: str,
    storage=None,
) -> dict:
    """Writes `bucket/key` (parquet) into the destination table. Returns the rows written and
    the qualified name. The parquet goes through the local disk (buffer), the batches
    through the ParquetFile's streaming: memory O(batch size)."""
    if storage is None:
        from app.utils import get_storage_service

        storage = get_storage_service()

    writer = _WRITERS.get(conn.db_type)
    if writer is None:
        raise DbDestinationError(f"Tabularia cannot write into a {conn.db_type} database")
    qualified, parts = _qualified_table(conn, dest.table)

    fd, tmp = tempfile.mkstemp(suffix=".parquet", prefix="db_dest_")
    os.close(fd)
    try:
        storage.download_file(bucket, key, tmp)
        pf = pq.ParquetFile(tmp)
        schema = _plain_schema(pf.schema_arrow)
        logger.info(
            "db-dest %s@%s ← %s → %s (%s, %d columns)",
            conn.db_type, conn.host, key, qualified, dest.mode, len(schema),
        )
        try:
            batches = _plain_batches(pf.iter_batches(batch_size=BATCH_ROWS), schema)
            rows = writer(conn, dest, qualified, parts, schema, batches)
        except IngestError:
            raise
        except Exception as e:  # the driver's error → the database server's message
            raise DbDestinationError(
                describe_db_error(e, conn.db_type, conn.host, conn.port_or_default)
            ) from e
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    logger.info("db-dest done: %s (%d rows, %s)", qualified, rows, dest.mode)
    return {"rows_written": rows, "table": qualified, "mode": dest.mode}
