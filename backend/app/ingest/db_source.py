"""Sorgenti database: esegue una query (o legge una tabella) e scrive il
risultato in parquet SENZA mai materializzare tutto in memoria.

Percorso dati: driver → batch Arrow → ParquetWriter su file temporaneo → upload
su storage. La memoria resta O(dimensione batch); il disco fa da buffer, come
per gli upload di file.

Un driver per tipo di database, tutti con lo stesso protocollo (un generatore
che produce PRIMA lo schema Arrow, POI i RecordBatch):
- postgresql        → ADBC (Arrow nativo, streaming lato server)
- clickhouse        → clickhouse-connect (query_arrow_stream, Arrow nativo)
- mysql / mariadb   → PyMySQL con cursore unbuffered (fetchmany → Arrow)
- trino             → client DBAPI ufficiale (paginazione naturale → Arrow)
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import Any, Iterator, Literal
from urllib.parse import quote as _urlquote

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import BaseModel

from app.ingest.converters import IngestError

logger = logging.getLogger(__name__)

# righe per batch nel percorso DBAPI (Postgres/ClickHouse usano i batch nativi)
BATCH_ROWS = 50_000

DEFAULT_PORTS = {
    "postgresql": 5432,
    "mysql": 3306,
    "mariadb": 3306,
    "clickhouse": 8123,  # porta HTTP (clickhouse-connect), non la nativa 9000
    "trino": 8080,
}

DbType = Literal["postgresql", "mysql", "mariadb", "clickhouse", "trino"]


class DbSourceError(IngestError):
    """Errore parlante da mostrare all'utente (connessione, query, tipi)."""


class DbConnectionSpec(BaseModel):
    db_type: DbType
    host: str
    port: int | None = None
    username: str = ""
    # una delle due: `password_encrypted` (Fernet, come arriva dal gateway) o
    # `password` in chiaro (solo per prove dirette sull'engine in sviluppo)
    password: str = ""
    password_encrypted: str = ""
    database: str = ""
    db_schema: str = ""  # schema Postgres / schema Trino; vuoto = default

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
    ref: str  # nome tabella (anche schema.tabella) oppure il testo SQL


# ─────────────────────────────────────────────────────────────────────────────
# Costruzione della query
# ─────────────────────────────────────────────────────────────────────────────
_IDENT_QUOTE = {"postgresql": '"', "trino": '"', "mysql": "`", "mariadb": "`", "clickhouse": "`"}


def build_query(conn: DbConnectionSpec, source: DbSourceSpec) -> str:
    """Query finale da eseguire sul database.

    - mode=sql: il testo dell'utente viene avvolto in `SELECT * FROM (…) AS _q`.
      Non è una barriera di sicurezza (la barriera è la capability CONNECT e
      un'utenza DB read-only): serve a rifiutare statement non-SELECT e a poter
      aggiungere LIMIT senza toccare il testo.
    - mode=table: identificatori quotati per dialetto; `schema.tabella` accettato,
      altrimenti si antepone lo schema della connessione se impostato.
    """
    if source.mode == "sql":
        sql = source.ref.strip().rstrip(";").strip()
        if not sql:
            raise DbSourceError("La query SQL è vuota")
        return f"SELECT * FROM ({sql}) AS _q"

    q = _IDENT_QUOTE[conn.db_type]
    parts = [p.strip() for p in source.ref.split(".") if p.strip()]
    if not parts:
        raise DbSourceError("Il nome della tabella è vuoto")
    if any(q in p for p in parts):
        raise DbSourceError(f"Nome tabella non valido: contiene {q!r}")
    if len(parts) == 1 and conn.db_schema:
        parts = [conn.db_schema, parts[0]]
    qualified = ".".join(f"{q}{p}{q}" for p in parts)
    return f"SELECT * FROM {qualified}"


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
            reader = cur.fetch_record_batch()  # RecordBatchReader: streaming vero
            yield reader.schema
            for batch in reader:
                yield batch


def _batches_clickhouse(conn: DbConnectionSpec, query: str) -> Iterator[Any]:
    import clickhouse_connect

    client = clickhouse_connect.get_client(
        host=conn.host,
        port=conn.port_or_default,
        username=conn.username or "default",
        password=conn.resolve_password(),
        database=conn.database or "default",
        connect_timeout=10,
    )
    try:
        sent_schema = False
        with client.query_arrow_stream(query) as stream:
            for chunk in stream:  # a seconda della versione: Table o RecordBatch
                batches = chunk.to_batches() if isinstance(chunk, pa.Table) else [chunk]
                for batch in batches:
                    if not sent_schema:
                        yield batch.schema
                        sent_schema = True
                    yield batch
        if not sent_schema:
            # risultato vuoto: lo schema si ricava da un LIMIT 0
            empty = client.query_arrow(f"SELECT * FROM ({query}) AS _s LIMIT 0")
            yield empty.schema
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
        cursorclass=pymysql.cursors.SSCursor,  # unbuffered: il server streama
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
        # il client Trino rifiuta la basic auth su http: con password si va in https
        kwargs["http_scheme"] = "https"
        kwargs["auth"] = trino.auth.BasicAuthentication(conn.username, password)
    c = trino.dbapi.connect(**kwargs)
    try:
        cur = c.cursor()
        cur.execute(query)
        yield from _dbapi_batches(cur)
    finally:
        c.close()


_DRIVERS = {
    "postgresql": _batches_postgresql,
    "mysql": _batches_mysql,
    "mariadb": _batches_mysql,  # protocollo condiviso: stesso driver
    "clickhouse": _batches_clickhouse,
    "trino": _batches_trino,
}


def _open_batches(conn: DbConnectionSpec, query: str) -> Iterator[Any]:
    return _DRIVERS[conn.db_type](conn, query)


# ─────────────────────────────────────────────────────────────────────────────
# Percorso DBAPI generico: righe Python → batch Arrow
# ─────────────────────────────────────────────────────────────────────────────
def _dbapi_batches(cursor, batch_rows: int = BATCH_ROWS) -> Iterator[Any]:
    """fetchmany → RecordBatch. Lo schema viene inferito dal PRIMO batch e poi
    imposto a tutti i successivi (il ParquetWriter non può cambiare schema in
    corsa): valori incompatibili più avanti producono un errore parlante."""
    names = [d[0] for d in (cursor.description or [])]
    if len(set(names)) != len(names):
        raise DbSourceError(
            "La query restituisce colonne con lo stesso nome: usa alias distinti (es. a.id AS a_id)"
        )

    rows = cursor.fetchmany(batch_rows)
    if not rows:
        # nessuna riga: il DBAPI non dà i tipi in modo affidabile → tutte stringhe
        yield pa.schema([pa.field(n, pa.string()) for n in names])
        return

    first = _rows_to_table(rows, names, schema=None)
    schema = _null_types_to_string(first.schema)
    yield schema
    yield from first.cast(schema).to_batches()

    while True:
        rows = cursor.fetchmany(batch_rows)
        if not rows:
            break
        yield from _rows_to_table(rows, names, schema=schema).to_batches()


def _rows_to_table(rows, names: list[str], schema: pa.Schema | None) -> pa.Table:
    pylist = [dict(zip(names, row)) for row in rows]
    try:
        return pa.Table.from_pylist(pylist, schema=schema)
    except (pa.ArrowInvalid, pa.ArrowTypeError) as e:
        raise DbSourceError(
            f"Tipi non uniformi nel risultato: {e}. "
            "Suggerimento: forza il tipo con un CAST esplicito nella query."
        ) from e


def _null_types_to_string(schema: pa.Schema) -> pa.Schema:
    """Colonne tutte-NULL nel primo batch → string (il tipo null non regge
    valori veri nei batch successivi)."""
    fields = [
        pa.field(f.name, pa.string()) if pa.types.is_null(f.type) else f for f in schema
    ]
    return pa.schema(fields)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point: ingest completo verso parquet su storage
# ─────────────────────────────────────────────────────────────────────────────
def ingest_db_to_parquet(
    conn: DbConnectionSpec,
    source: DbSourceSpec,
    bucket: str,
    key: str,
    storage=None,
) -> dict:
    """Esegue la sorgente e scrive `bucket/key` (parquet). Ritorna righe e schema."""
    if storage is None:
        from app.utils import get_storage_service

        storage = get_storage_service()

    query = build_query(conn, source)
    logger.info("db-ingest %s@%s → %s (%s)", conn.db_type, conn.host, key, query[:200])

    gen = _open_batches(conn, query)
    try:
        schema = next(gen)
    except StopIteration:  # nessun driver arriva qui, ma il contratto va difeso
        raise DbSourceError("Il driver non ha restituito uno schema")

    fd, tmp = tempfile.mkstemp(suffix=".parquet", prefix="db_ingest_")
    os.close(fd)
    try:
        rows = 0
        with pq.ParquetWriter(tmp, schema, compression="zstd") as writer:
            for batch in gen:
                writer.write_batch(batch)
                rows += batch.num_rows

        storage.create_bucket(bucket)
        storage.upload_file(tmp, bucket, key)

        # dtype come stringhe Polars: coerenti col resto del catalogo/frontend
        scan_schema = pl.scan_parquet(tmp).collect_schema()
        columns = [{"name": n, "dtype": str(t)} for n, t in scan_schema.items()]
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass

    logger.info("db-ingest completato: %s (%d righe, %d colonne)", key, rows, len(columns))
    return {"rows_written": rows, "columns": columns}


# ─────────────────────────────────────────────────────────────────────────────
# Ispezione: test di connessione e lista tabelle (per la UI)
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
}


def _collect_first_column(conn: DbConnectionSpec, query: str) -> list[str]:
    gen = _open_batches(conn, query)
    next(gen)  # schema
    out: list[str] = []
    for batch in gen:
        out.extend(str(v) for v in batch.column(0).to_pylist())
    return out


def test_connection(conn: DbConnectionSpec) -> None:
    """Apre la connessione ed esegue una query banale sullo stesso percorso
    dell'ingest: se non solleva, la connessione è utilizzabile."""
    _collect_first_column(conn, "SELECT 1 AS ok")


def list_tables(conn: DbConnectionSpec) -> list[str]:
    return _collect_first_column(conn, _TABLES_QUERY[conn.db_type])
