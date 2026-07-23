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
import pyarrow.compute as pc
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


# ClickHouse esporta in Arrow i tipi temporali come INTERI grezzi (Date→uint16
# giorni, Date32→int32, DateTime→uint32 secondi, DateTime64→int64 in `scale`
# decimali): senza riconvertirli finirebbero numerici nel parquet (es. il datetime
# 1714546800). A livello Arrow un uint32 è indistinguibile da un intero vero,
# quindi usiamo i TIPI ClickHouse (nome della classe datatype, che ignora i
# wrapper Nullable/LowCardinality) per sapere quali colonne convertire.
def _clickhouse_temporal_targets(ch_types) -> dict:
    """Indice colonna → (tipo Arrow di destinazione, scala DateTime64 | None)."""
    targets: dict[int, tuple] = {}
    for i, t in enumerate(ch_types):
        cls = type(t).__name__
        if cls in ("Date", "Date32"):
            targets[i] = (pa.date32(), None)
        elif cls == "DateTime":
            targets[i] = (pa.timestamp("s"), None)  # NAIVE = istante UTC (come ADBC)
        elif cls == "DateTime64":
            scale = int(getattr(t, "scale", 3) or 0)
            unit = {0: "s", 3: "ms", 6: "us", 9: "ns"}.get(scale, "us")
            targets[i] = (pa.timestamp(unit), scale)
    return targets


def _cast_ch_temporal(arr, target):
    """Reinterpreta l'intero grezzo di ClickHouse come date32/timestamp."""
    arrow_t, scale = target
    if pa.types.is_date(arrow_t):
        return pc.cast(pc.cast(arr, pa.int32()), arrow_t)  # giorni dal 1970
    raw = pc.cast(arr, pa.int64())
    if scale is not None and scale not in (0, 3, 6, 9):
        # DateTime64 con precisione non standard → riscala all'unità scelta (us)
        raw = pc.multiply(raw, 10 ** (6 - scale)) if scale < 6 else pc.divide(raw, 10 ** (scale - 6))
        raw = pc.cast(raw, pa.int64())
    return pc.cast(raw, arrow_t)


def _fix_clickhouse_temporals(batch, targets: dict):
    """Riscrive il RecordBatch convertendo le colonne temporali intere."""
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
        # tipi ClickHouse da uno schema LIMIT 0: individuano le colonne temporali
        # (che l'export Arrow rende interi grezzi) da riconvertire.
        targets = _clickhouse_temporal_targets(
            client.query(f"SELECT * FROM ({query}) AS _s LIMIT 0").column_types
        )
        sent_schema = False
        with client.query_arrow_stream(query) as stream:
            for chunk in stream:  # a seconda della versione: Table o RecordBatch
                batches = chunk.to_batches() if isinstance(chunk, pa.Table) else [chunk]
                for batch in batches:
                    batch = _fix_clickhouse_temporals(batch, targets)
                    if not sent_schema:
                        yield batch.schema
                        sent_schema = True
                    yield batch
        if not sent_schema:
            # risultato vuoto: schema da LIMIT 0, con i tipi temporali corretti
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
