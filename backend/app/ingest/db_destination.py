"""Destinazioni database: scrive un parquet dello storage in una tabella,
in streaming (batch Arrow → INSERT), senza mai materializzare tutto in memoria.

Semantica UNIFORME su tutti i dialetti (è il contratto del nodo Output):
1. CREATE TABLE IF NOT EXISTS con tipi mappati dallo schema Arrow;
2. mode="replace" → TRUNCATE (la definizione della tabella resta, i dati no);
   mode="append"  → si accoda a ciò che c'è;
3. insert in streaming a batch;
4. eventuali statement post-insert (separati da `;`), sulla stessa connessione.

Columns are matched by NAME on every database: a column the existing table lacks
stops the write, columns the flow lacks take their DEFAULT. On ClickHouse "replace"
loads a staging copy of the target's own definition and swaps it in atomically, so
engine, ORDER BY, partitions, TTL and defaults stay the customer's (materialized
views on the target see inserts, not swaps: they do not see replaced data).

Driver per dialetto (gli stessi delle sorgenti):
- postgresql        → ADBC (adbc_ingest: bulk COPY, Arrow nativo)
- clickhouse        → clickhouse-connect (insert_arrow)
- mysql / mariadb   → PyMySQL executemany su chunk
- trino             → INSERT parametrico multi-riga (lento per natura: Trino
                      non è pensato per OLTP, ma funziona)
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Any, Iterator, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel

from app.core.config import get_settings
from app.ingest.converters import IngestError
from app.ingest.db_errors import describe_db_error
from app.ingest.db_source import _IDENT_QUOTE, BATCH_ROWS, DbConnectionSpec

logger = logging.getLogger(__name__)

# righe per singolo INSERT nei percorsi senza bulk nativo
MYSQL_CHUNK_ROWS = 5_000  # multi-row VALUES: sotto il max_allowed_packet
TRINO_CHUNK_ROWS = 500  # ogni INSERT è una query Trino: chunk piccoli


class DbDestinationError(IngestError):
    """Errore parlante da mostrare all'utente (tabella, tipi, permessi DB)."""


class DbDestinationSpec(BaseModel):
    table: str  # nome tabella (anche schema.tabella)
    mode: Literal["append", "replace"] = "append"
    post_sql: str = ""  # statement post-insert separati da `;` (facoltativi)


def _qualified_table(conn: DbConnectionSpec, table_ref: str) -> tuple[str, list[str]]:
    """Nome tabella quotato per dialetto + parti non quotate (schema, tabella)."""
    q = _IDENT_QUOTE[conn.db_type]
    parts = [p.strip() for p in table_ref.split(".") if p.strip()]
    if not parts:
        raise DbDestinationError("The destination table name is empty")
    if any(q in p for p in parts):
        raise DbDestinationError(f"Invalid table name: it contains {q!r}")
    if len(parts) == 1 and conn.db_schema:
        parts = [conn.db_schema, parts[0]]
    return ".".join(f"{q}{p}{q}" for p in parts), parts


def _post_statements(post_sql: str) -> list[str]:
    return [s.strip() for s in (post_sql or "").split(";") if s.strip()]


# ─────────────────────────────────────────────────────────────────────────────
# Mappa tipi Arrow → SQL per dialetto
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
    else:  # stringhe e tutto il resto (tempo, binario…): testo
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
# Writer per dialetto: stessa firma, stessa semantica
# ─────────────────────────────────────────────────────────────────────────────
def _write_postgresql(
    conn: DbConnectionSpec, dest: DbDestinationSpec, qualified: str, parts: list[str],
    schema: pa.Schema, batches: Iterator[pa.RecordBatch],
) -> int:
    from urllib.parse import quote as _urlquote

    from adbc_driver_postgresql import dbapi as adbc_pg

    uri = (
        f"postgresql://{_urlquote(conn.username, safe='')}:"
        f"{_urlquote(conn.resolve_password(), safe='')}@"
        f"{conn.host}:{conn.port_or_default}/{conn.database}"
    )
    rows = [0]
    with adbc_pg.connect(uri) as c:
        with c.cursor() as cur:
            cur.execute(_create_table_sql(conn, qualified, schema))
            if dest.mode == "replace":
                cur.execute(f"TRUNCATE TABLE {qualified}")
            reader = pa.RecordBatchReader.from_batches(schema, _counted(batches, rows))
            cur.adbc_ingest(
                parts[-1], reader, mode="append",
                db_schema_name=parts[0] if len(parts) > 1 else None,
            )
            for stmt in _post_statements(dest.post_sql):
                cur.execute(stmt)
        c.commit()
    return rows[0]


def _write_clickhouse(
    conn: DbConnectionSpec, dest: DbDestinationSpec, qualified: str, parts: list[str],
    schema: pa.Schema, batches: Iterator[pa.RecordBatch],
) -> int:
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=conn.host,
        port=conn.port_or_default,
        username=conn.username or "default",
        password=conn.resolve_password(),
        database=conn.database or "default",
        connect_timeout=10,
        # INSERT … SELECT dalla staging e post-SQL (un OPTIMIZE) possono tacere a lungo
        send_receive_timeout=get_settings().ingest.db_read_timeout_seconds,
    )
    # ClickHouse non ha transazioni sull'INSERT in streaming: scrivere direttamente
    # nella tabella (TRUNCATE + insert a batch) la lascerebbe in stato PARZIALE se
    # qualcosa fallisce a metà — con replace, addirittura vuota (dati persi). Quindi
    # si scrive tutto in una tabella di STAGING; poi lo scambio è ATOMICO.
    import uuid

    q = _IDENT_QUOTE["clickhouse"]
    stg_parts = parts[:-1] + [f"{parts[-1]}__tab_stg_{uuid.uuid4().hex[:8]}"]
    stg_qualified = ".".join(f"{q}{p}{q}" for p in stg_parts)
    stg_name = stg_parts[-1] if len(stg_parts) == 1 else ".".join(stg_parts)
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
            # insert_arrow names the columns: they land by name in the staging
            client.insert_arrow(stg_name, pa.Table.from_batches([batch], schema=schema))
            rows += batch.num_rows
        if dest.mode == "replace":
            # swap ATOMICO: la destinazione passa dai vecchi ai nuovi dati in un colpo
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
            client.command(f"DROP TABLE IF EXISTS {stg_qualified}")  # staging (dati vecchi o già copiati)
        except Exception:
            pass
        client.close()
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
    import pymysql

    c = pymysql.connect(
        host=conn.host,
        port=conn.port_or_default,
        user=conn.username,
        password=conn.resolve_password(),
        database=conn.database or None,
        connect_timeout=10,
        charset="utf8mb4",
        # naive timestamps are UTC: a TIMESTAMP column would otherwise read them in
        # the server's zone and store an instant one or two hours off
        init_command="SET time_zone = '+00:00'",
    )
    placeholders = ", ".join(["%s"] * len(schema))
    insert = f"INSERT INTO {qualified} ({_quoted_columns(conn, schema)}) VALUES ({placeholders})"
    rows = 0
    try:
        with c.cursor() as cur:
            cur.execute(_create_table_sql(conn, qualified, schema))
            if dest.mode == "replace":
                # DELETE (DML, transazionale) e NON TRUNCATE: in MySQL/MariaDB
                # TRUNCATE è DDL e fa un COMMIT implicito, quindi svuoterebbe la
                # tabella FUORI dalla transazione degli INSERT — un errore a metà
                # stream lascerebbe la tabella permanentemente vuota. Con DELETE
                # lo svuotamento e gli insert committano (o rollbackano) insieme.
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

    password = conn.resolve_password()
    kwargs: dict[str, Any] = {
        "host": conn.host,
        "port": conn.port_or_default,
        "user": conn.username or "tabularia",
        "catalog": conn.database or None,
        "schema": conn.db_schema or "default",
    }
    if password:
        kwargs["http_scheme"] = "https"
        kwargs["auth"] = trino.auth.BasicAuthentication(conn.username, password)
    # transazione esplicita: DELETE + INSERT devono essere atomici, altrimenti un
    # errore a metà lascia la tabella svuotata/parziale. NB: la transazionalità in
    # Trino dipende dal CONNETTORE (Iceberg/Delta la supportano, Hive puro no); dove
    # non è supportata il commit/rollback degenera nel comportamento precedente.
    kwargs["isolation_level"] = trino.transaction.IsolationLevel.READ_UNCOMMITTED
    c = trino.dbapi.connect(**kwargs)

    def _run(cur, sql: str, params=None) -> None:
        cur.execute(sql, params) if params else cur.execute(sql)
        cur.fetchall()  # il client Trino completa la query solo consumandola

    one_row = "(" + ", ".join(["?"] * len(schema)) + ")"
    prefix = f"INSERT INTO {qualified} ({_quoted_columns(conn, schema)}) VALUES "
    rows = 0
    try:
        cur = c.cursor()
        _run(cur, _create_table_sql(conn, qualified, schema))
        c.commit()  # la DDL fuori dalla transazione dei dati
        if dest.mode == "replace":
            _run(cur, f"DELETE FROM {qualified}")  # Trino non ha TRUNCATE ovunque
        for batch in batches:
            for chunk in _row_chunks(batch, schema, TRINO_CHUNK_ROWS):
                sql = prefix + ", ".join([one_row] * len(chunk))
                _run(cur, sql, [v for row in chunk for v in row])
                rows += len(chunk)
        for stmt in _post_statements(dest.post_sql):
            _run(cur, stmt)
        c.commit()  # DELETE + INSERT insieme: o tutto o niente
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
}


def _counted(batches: Iterator[pa.RecordBatch], counter: list[int]) -> Iterator[pa.RecordBatch]:
    for b in batches:
        counter[0] += b.num_rows
        yield b


def _row_chunks(batch: pa.RecordBatch, schema: pa.Schema, size: int) -> Iterator[list[tuple]]:
    """RecordBatch → liste di tuple Python (ordine colonne = schema)."""
    names = [f.name for f in schema]
    for start in range(0, batch.num_rows, size):
        piece = batch.slice(start, size)
        cols = [piece.column(n).to_pylist() for n in names]
        yield list(zip(*cols)) if cols else []


# ─────────────────────────────────────────────────────────────────────────────
# Entry point: parquet su storage → tabella
# ─────────────────────────────────────────────────────────────────────────────
def write_parquet_to_db(
    conn: DbConnectionSpec,
    dest: DbDestinationSpec,
    bucket: str,
    key: str,
    storage=None,
) -> dict:
    """Scrive `bucket/key` (parquet) nella tabella di destinazione. Ritorna righe
    scritte e nome qualificato. Il parquet passa dal disco locale (buffer), i
    batch dallo streaming del ParquetFile: memoria O(dimensione batch)."""
    if storage is None:
        from app.utils import get_storage_service

        storage = get_storage_service()

    qualified, parts = _qualified_table(conn, dest.table)
    writer = _WRITERS[conn.db_type]

    fd, tmp = tempfile.mkstemp(suffix=".parquet", prefix="db_dest_")
    os.close(fd)
    try:
        storage.download_file(bucket, key, tmp)
        pf = pq.ParquetFile(tmp)
        schema = pf.schema_arrow
        logger.info(
            "db-dest %s@%s ← %s → %s (%s, %d colonne)",
            conn.db_type, conn.host, key, qualified, dest.mode, len(schema),
        )
        try:
            rows = writer(conn, dest, qualified, parts, schema, pf.iter_batches(batch_size=BATCH_ROWS))
        except IngestError:
            raise
        except Exception as e:  # errore del driver → messaggio del server del database
            raise DbDestinationError(
                describe_db_error(e, conn.db_type, conn.host, conn.port_or_default)
            ) from e
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    logger.info("db-dest completato: %s (%d righe, %s)", qualified, rows, dest.mode)
    return {"rows_written": rows, "table": qualified, "mode": dest.mode}
