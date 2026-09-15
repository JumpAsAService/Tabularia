"""
Engine "ClickHouse esterno": le trasformazioni girano su un server ClickHouse
REMOTO (cloud managed, es. Scaleway/ClickHouse Cloud, o self-hosted) invece che
nel processo worker. Opzionale: si attiva impostando `CLICKHOUSE_EXTERNAL__HOST`.

Riusa INTEGRALMENTE i builder SQL di chDB (`chdb_ops`: stesso dialetto, stesse
16+ operazioni): la catena di step è un'unica SELECT annidata che il server
ottimizza ed esegue. Cambia solo il TRASPORTO dei dati, scelto in configurazione:

- `s3` (produzione): il server legge le sorgenti e scrive i risultati DIRETTAMENTE
  sull'object storage con la table function `s3()` → nessun dato transita dal
  worker, che manda solo SQL. Richiede che lo storage sia raggiungibile dal
  server (tipico: ClickHouse + Object Storage dello stesso cloud, o MinIO
  esposto). Le credenziali S3 viaggiano nella query, salvo `s3_named_collection`.
- `push` (fallback universale): il worker carica la sorgente in una tabella di
  staging (`MergeTree`, per row-group Arrow) e riscarica il risultato in streaming
  (`FORMAT Parquet` via HTTP). Funziona con qualsiasi ClickHouse, anche se non
  vede lo storage, ma i dati passano dal worker.

Preview = `LIMIT n` in formato Parquet (piccolo, in RAM). Run = materializzazione
sul server (s3) o streaming su file (push). Step-cache incrementale identica a
chDB/DuckDB/Polars, namespacata con il tag `clickhouse`.

Fork-safe: clickhouse-connect è HTTP puro (niente thread nativi), quindi può
essere registrato eager nel padre Celery — a differenza di chDB.
"""
from __future__ import annotations

import io
import logging
import os
import tempfile
import uuid
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
from botocore.exceptions import ClientError

from app.core.config import ClickHouseExternalSettings, get_settings
from app.engine.base import DataSource, Engine, Operation, PreviewResult, RunResult
from app.engine.cache import StepCache, plan_hashes
from app.engine.matview import MatViewStore
from app.engine.chdb_ops import _lit, _qi, get_chdb_operation, temporal_safe_sql
from app.engine.exceptions import EngineError, OperationError, SourceNotFoundError
from app.engine.polars_engine import _coerce_ops, _columns_of
from app.engine.temporal import naive_utc, rewrite_parquet_naive_utc

logger = logging.getLogger(__name__)

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket"}

# impostazioni per le query che PRODUCONO parquet (preview e materializzazioni):
# senza `string_as_string` ClickHouse scrive le stringhe come BYTE_ARRAY senza
# tipo logico → Polars le leggerebbe come Binary.
_PARQUET_OUT = {"output_format_parquet_string_as_string": 1}

# operazioni che impongono una SCANSIONE piena (ClickHouse legge molte/tutte le
# righe): lì l'oversubscription dei thread paga. Le altre (filtro/proiezione con
# LIMIT) si fermano presto e con troppi thread rallenterebbero: restano al default.
_SCAN_HEAVY = frozenset({"group_by", "pivot", "unpivot", "sort", "join", "union", "unique", "sql"})


def _needs_scan_tuning(ops) -> bool:
    return any((o.type if hasattr(o, "type") else o.get("type")) in _SCAN_HEAVY for o in ops)


def _effective_scan_threads(target: int, server_threads: int) -> int:
    """Thread da imporre a una scansione: `target`, ma solo se ALZA rispetto al
    default del server (= n. core). 0 = non toccare (spento, o server già capiente)."""
    if target <= 0:
        return 0
    if server_threads and target <= server_threads:
        return 0
    return target


def _clean_error(e: Exception, host: str | None = None, port: int | None = None) -> str:
    """Messaggio del server ClickHouse (vedi app.ingest.db_errors): il testo di
    `DB::Exception` senza rumore HTTP, anche quando lo stream si interrompe a metà.
    Già etichettato «ClickHouse: …»: non va incastrato in un'altra frase che
    ripeta il nome del database."""
    from app.ingest.db_errors import describe_db_error

    return describe_db_error(e, "clickhouse", host, port)


def _source_exists(storage, source: DataSource) -> bool:
    """Esistenza dell'oggetto senza scaricarlo (HEAD); i doppioni di test espongono
    `exists`."""
    head = getattr(storage, "head_object", None)
    if head is None:
        exists = getattr(storage, "exists", None)
        return True if exists is None else bool(exists(source.bucket, source.key))
    try:
        head(source.bucket, source.key)
        return True
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code", ""))
        if code in _NOT_FOUND_CODES:
            return False
        raise


# ── tipi Arrow → ClickHouse (per le tabelle di staging in modalità push) ──────
def _ch_type(t: pa.DataType) -> str:
    if pa.types.is_boolean(t):
        return "Bool"
    if pa.types.is_signed_integer(t):
        return f"Int{t.bit_width}"
    if pa.types.is_unsigned_integer(t):
        return f"UInt{t.bit_width}"
    if pa.types.is_float16(t) or pa.types.is_float32(t):
        return "Float32"
    if pa.types.is_float64(t):
        return "Float64"
    if pa.types.is_decimal(t):
        return f"Decimal({t.precision}, {t.scale})"
    if pa.types.is_string(t) or pa.types.is_large_string(t) or pa.types.is_binary(t) or pa.types.is_large_binary(t):
        return "String"
    if pa.types.is_date(t):
        return "Date32"
    if pa.types.is_timestamp(t):
        scale = {"s": 0, "ms": 3, "us": 6, "ns": 9}[t.unit]
        return f"DateTime64({scale}, '{t.tz}')" if t.tz else f"DateTime64({scale})"
    if pa.types.is_null(t):
        return "String"
    raise EngineError(
        f"tipo di colonna non supportato dall'engine ClickHouse esterno: {t}. "
        "Converti la colonna (cast) o usa Polars/DuckDB per questo flusso."
    )


def _staging_ddl(schema: pa.Schema) -> str:
    return ", ".join(f"{_qi(f.name)} Nullable({_ch_type(f.type)})" for f in schema)


class ClickHouseContext:
    """Stato di esecuzione di una catena sul server: client, storage, trasporto,
    tabelle di staging e file temporanei da ripulire. Espone la stessa interfaccia
    di `ChdbContext` (columns_of/scalar/distinct_*/build_right) così `chdb_ops`
    funziona invariato."""

    def __init__(self, client, storage, cfg: ClickHouseExternalSettings, tmp: list[str],
                 preview_limit: int | None = None, matviews=None, allow_matview: bool = False,
                 sort_keys=None):
        self.client = client
        self.storage = storage
        self.cfg = cfg
        self.tmp = tmp
        self.staging: list[str] = []  # tabelle di staging (push) da droppare
        self._n = 0
        self.preview_limit = preview_limit
        # materializzazione della sorgente radice: attiva solo nel viewer (preview)
        self.matviews = matviews
        self.allow_matview = allow_matview
        # colonne di ORDER BY che la copia materializzata deve ereditare
        self.sort_keys = [k.strip() for k in (sort_keys or []) if k and k.strip()]
        self.settings: dict[str, Any] = {}
        if cfg.max_execution_time > 0:
            self.settings["max_execution_time"] = cfg.max_execution_time

    def uid(self, prefix: str) -> str:
        self._n += 1
        return f"_{prefix}{self._n}"

    def tempfile(self) -> str:
        path = tempfile.mkstemp(suffix=".parquet", prefix="chext_")[1]
        self.tmp.append(path)
        return path

    # ── query di servizio ────────────────────────────────────────────────
    def _rows(self, sql: str) -> list[list]:
        try:
            return [list(r) for r in self.client.query(sql, settings=self.settings).result_rows]
        except EngineError:
            raise
        except Exception as e:
            raise EngineError(_clean_error(e)) from e

    def command(self, sql: str, settings: dict[str, Any] | None = None) -> None:
        try:
            self.client.command(sql, settings={**self.settings, **(settings or {})})
        except Exception as e:
            raise EngineError(_clean_error(e)) from e

    def parquet_safe(self, sql: str) -> str:
        """Date/DateTime → Date32/DateTime64 prima di ogni uscita in Parquet
        (altrimenti escono come interi): vedi `chdb_ops.temporal_safe_sql`."""
        return temporal_safe_sql(self, sql)

    def parquet_bytes(self, sql: str) -> bytes:
        """Esegue `sql` e ritorna il risultato come parquet (in RAM: solo per
        risultati piccoli, es. preview)."""
        try:
            return self.client.raw_query(self.parquet_safe(sql), fmt="Parquet", settings={**self.settings, **_PARQUET_OUT})
        except Exception as e:
            raise EngineError(_clean_error(e)) from e

    def parquet_to_file(self, sql: str, path: str) -> None:
        """Esegue `sql` e scrive il parquet su file IN STREAMING (HTTP chunked)."""
        try:
            with self.client.raw_stream(self.parquet_safe(sql), fmt="Parquet", settings={**self.settings, **_PARQUET_OUT}) as stream, \
                    open(path, "wb") as fh:
                for chunk in stream:
                    fh.write(chunk)
        except Exception as e:
            raise EngineError(_clean_error(e)) from e

    def columns_of(self, sql: str) -> list[str]:
        return [row[0] for row in self._rows(f"DESCRIBE ({sql})")]

    def schema_of(self, sql: str) -> list[tuple[str, str]]:
        return [(row[0], row[1]) for row in self._rows(f"DESCRIBE ({sql})")]

    def scalar(self, sql: str) -> int:
        rows = self._rows(sql)
        return int(rows[0][0]) if rows and rows[0] else 0

    def distinct_values(self, base_sql: str, col: str) -> list:
        rows = self._rows(f"SELECT DISTINCT {_qi(col)} FROM {base_sql} ORDER BY {_qi(col)}")
        return [r[0] for r in rows]

    def distinct_rows(self, base_sql: str, cols: list[str]) -> list[list]:
        sel = ", ".join(_qi(c) for c in cols)
        return self._rows(f"SELECT DISTINCT {sel} FROM {base_sql} ORDER BY {sel}")

    # ── trasporto s3: table function sullo storage ───────────────────────
    def s3_fn(self, source: DataSource) -> str:
        """`s3(...)` che punta all'oggetto `source` (lettura E scrittura)."""
        endpoint = (self.cfg.s3_endpoint or get_settings().storage.endpoint).rstrip("/")
        url = f"{endpoint}/{source.bucket}/{source.key}"
        if self.cfg.s3_named_collection:
            return f"s3({_qi(self.cfg.s3_named_collection)}, url = {_lit(url)}, format = 'Parquet')"
        st = get_settings().storage
        return f"s3({_lit(url)}, {_lit(st.access_key)}, {_lit(st.secret_key.get_secret_value())}, 'Parquet')"

    # ── trasporto push: tabella di staging ───────────────────────────────
    def _push(self, source: DataSource) -> str:
        path = self.tempfile()
        try:
            self.storage.download_file(source.bucket, source.key, path)
        except ClientError as e:
            code = str(e.response.get("Error", {}).get("Code", ""))
            if code in _NOT_FOUND_CODES:
                raise SourceNotFoundError(source.bucket, source.key) from e
            raise
        except FileNotFoundError as e:
            raise SourceNotFoundError(source.bucket, source.key) from e
        table = f"_tabularia_{uuid.uuid4().hex[:16]}"
        qualified = f"{_qi(self.cfg.database)}.{_qi(table)}"
        pf = pq.ParquetFile(path)
        self.command(
            f"CREATE TABLE {qualified} ({_staging_ddl(pf.schema_arrow)}) ENGINE = MergeTree ORDER BY tuple()"
        )
        self.staging.append(qualified)
        try:
            for i in range(pf.num_row_groups):
                self.client.insert_arrow(table, pf.read_row_group(i), database=self.cfg.database)
        except Exception as e:
            raise EngineError(
                "Loading the source onto the server failed. "
                + _clean_error(e, self.cfg.host, self.cfg.port)
            ) from e
        return f"SELECT * FROM {qualified}"

    # ── sorgenti e catena ────────────────────────────────────────────────
    def scan(self, source: DataSource, root: bool = False) -> str:
        if self.cfg.transport == "push":
            return self._push(source)
        if not _source_exists(self.storage, source):
            raise SourceNotFoundError(source.bucket, source.key)
        # solo la SORGENTE radice del viewer (non i blob di cache, non i lati
        # join): se è grande, si legge dalla copia MergeTree invece che da s3()
        if root and self.allow_matview and self.matviews is not None:
            table = self.matviews.resolve(self, source, self.sort_keys)
            if table:
                return f"SELECT * FROM {table}"
        return f"SELECT * FROM {self.s3_fn(source)}"

    def apply(self, sql: str, ops, index_offset: int = 0) -> str:
        for i, op in enumerate(ops):
            op_type = op.type if isinstance(op, Operation) else op["type"]
            params = op.params if isinstance(op, Operation) else (op.get("params") or {})
            fn = get_chdb_operation(op_type)
            try:
                sql = fn(sql, params, self)
            except EngineError:
                raise
            except Exception as e:
                raise OperationError(op_type, i + index_offset, str(e)) from e
        return sql

    def build_right(self, ref: dict) -> str:
        if "source" in ref:
            sql = self.scan(DataSource(**ref["source"]))
            return self.apply(sql, ref.get("operations") or [])
        return self.scan(DataSource(**ref))

    # ── operazioni per la materializzazione (la politica sta in matview.py) ────
    def matview_count(self, source: DataSource) -> int:
        """Righe della sorgente senza leggerne i dati: ClickHouse conta dai
        metadati del parquet (footer), non scansiona."""
        return self.scalar(f"SELECT count() FROM {self.s3_fn(source)}")

    def matview_exists(self, db: str, table: str) -> bool:
        return self.scalar(
            f"SELECT count() FROM system.tables WHERE database = {_lit(db)} AND name = {_lit(table)}"
        ) > 0

    def matview_build(self, db: str, table: str, source: DataSource, sort_keys=None) -> None:
        """CREATE + INSERT + RENAME. Il database gestito di Scaleway è `Replicated`
        e rifiuta `CREATE AS SELECT`, quindi DDL esplicita (da DESCRIBE) e INSERT
        separato. Il RENAME rende la tabella visibile solo a INSERT COMPLETO: chi
        guarda `matview_exists` non vede mai una copia a metà. Se un altro worker
        l'ha già creata nel frattempo, la sua è valida quanto la mia."""
        s3 = self.s3_fn(source)
        cols = self.schema_of(f"SELECT * FROM {s3}")
        if not cols:
            raise EngineError("materializzazione: schema della sorgente vuoto")
        ddl = ", ".join(f"{_qi(name)} {typ}" for name, typ in cols)
        # ORDER BY solo sulle chiavi che esistono davvero nello schema (una chiave
        # sbagliata non deve far fallire la copia); nessuna → tuple() come prima
        present = {name for name, _ in cols}
        keys = [k for k in (sort_keys or []) if k in present]
        order = f"({', '.join(_qi(k) for k in keys)})" if keys else "tuple()"
        # le colonne del parquet sono Nullable: una sorting key nullable è
        # rifiutata (code 44) senza questo setting → senza, l'ORDER BY non
        # partirebbe MAI. Irrilevante con tuple().
        opts = " SETTINGS allow_nullable_key = 1" if keys else ""
        final = f"{_qi(db)}.{_qi(table)}"
        tmp = f"{_qi(db)}.{_qi(table + '_tmp_' + uuid.uuid4().hex[:8])}"
        self.command(f"CREATE TABLE {tmp} ({ddl}) ENGINE = MergeTree ORDER BY {order}{opts}")
        try:
            self.command(f"INSERT INTO {tmp} SELECT * FROM {s3}")
            try:
                self.command(f"RENAME TABLE {tmp} TO {final}")
            except Exception:
                if self.matview_exists(db, table):
                    self.command(f"DROP TABLE IF EXISTS {tmp} SYNC")
                else:
                    raise
        except Exception:
            self.command(f"DROP TABLE IF EXISTS {tmp} SYNC")
            raise

    def matview_drop(self, db: str, table: str) -> None:
        self.command(f"DROP TABLE IF EXISTS {_qi(db)}.{_qi(table)} SYNC")

    def matview_list(self, db: str, prefix: str) -> list[tuple[str, float]]:
        rows = self._rows(
            "SELECT name, toUnixTimestamp(metadata_modification_time) FROM system.tables "
            f"WHERE database = {_lit(db)} AND startsWith(name, {_lit(prefix)})"
        )
        return [(r[0], float(r[1])) for r in rows]

    def cleanup(self) -> None:
        for table in self.staging:
            try:
                self.client.command(f"DROP TABLE IF EXISTS {table}")
            except Exception:  # best effort: il server potrebbe essere già irraggiungibile
                logger.warning("impossibile eliminare la tabella di staging %s", table)
        self.staging.clear()
        for p in self.tmp:
            try:
                os.remove(p)
            except OSError:
                pass
        self.tmp.clear()


class ClickHouseEngine(Engine):
    engine_name = "clickhouse"

    def __init__(self, storage=None, cache=None, cfg: ClickHouseExternalSettings | None = None):
        if storage is None:
            from app.utils import get_storage_service

            storage = get_storage_service()
        self.storage = storage
        self.cache = cache or StepCache(storage)
        self.cfg = cfg or get_settings().clickhouse_external
        if not self.cfg.enabled:
            raise EngineError(
                "engine ClickHouse esterno non configurato: imposta CLICKHOUSE_EXTERNAL__HOST (e credenziali)."
            )
        self.matviews = MatViewStore(self.cache.redis, self.cfg)
        self._server_threads: int | None = None  # core del server (letto una volta)

    def _client(self):
        import clickhouse_connect

        try:
            return clickhouse_connect.get_client(
                host=self.cfg.host,
                port=self.cfg.port,
                username=self.cfg.username or "default",
                password=self.cfg.password.get_secret_value(),
                database=self.cfg.database or "default",
                secure=self.cfg.secure,
                connect_timeout=self.cfg.connect_timeout,
            )
        except Exception as e:
            raise EngineError(_clean_error(e, self.cfg.host, self.cfg.port)) from e

    def _source_id(self, source: DataSource) -> str:
        return f"{self.engine_name}:{source.bucket}/{source.key}"

    def _scan_max_threads(self, ctx) -> int:
        """max_threads da imporre a una scansione, o 0 per non toccare. Legge i
        core del server una volta per processo; su errore lascia perdere."""
        if self.cfg.parquet_scan_max_threads <= 0:
            return 0
        if self._server_threads is None:
            try:
                self._server_threads = int(
                    ctx.scalar("SELECT value FROM system.settings WHERE name = 'max_threads'")
                )
            except Exception:
                self._server_threads = 0
        return _effective_scan_threads(self.cfg.parquet_scan_max_threads, self._server_threads)

    # ── Cache incrementale (mirror di chDB) ───────────────────────────────
    def _sql_from_cache(self, ctx, source, operations, hashes, record=False, use_cache=True) -> str:
        start = self.cache.nearest(hashes) if use_cache else 0
        if record and operations and use_cache:
            (self.cache.record_hit if start > 0 else self.cache.record_miss)()
        if start == 0:
            sql = ctx.scan(source, root=True)
        else:
            self.cache.touch(hashes[start - 1])
            cached = DataSource(bucket=self.cache.bucket, key=self.cache.object_key(hashes[start - 1]))
            sql = ctx.scan(cached)
            logger.info("cache hit: riparto dallo step %d/%d", start, len(operations))
        return ctx.apply(sql, operations[start:], index_offset=start)

    def _materialize(self, ctx, source, operations) -> None:
        if not operations:
            return
        hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in operations])
        final = hashes[-1]
        if self.cache.has(final):
            return
        sql = self._sql_from_cache(ctx, source, operations, hashes)
        self._write(ctx, sql, DataSource(bucket=self.cache.bucket, key=self.cache.object_key(final)))
        self.cache.mark(final)
        logger.info("materializzato step %d in cache", len(operations))

    # ── scrittura dei risultati ───────────────────────────────────────────
    def _write(self, ctx: ClickHouseContext, sql: str, dest: DataSource) -> str | None:
        """Materializza `sql` nell'oggetto `dest`. In modalità s3 scrive il server
        (ritorna None); in push scarica su file locale, lo carica e ritorna il path."""
        if ctx.cfg.transport == "s3":
            ctx.command(
                f"INSERT INTO FUNCTION {ctx.s3_fn(dest)} SELECT * FROM ({ctx.parquet_safe(sql)})",
                settings={**_PARQUET_OUT, "s3_truncate_on_insert": 1, "s3_create_new_file_on_insert": 0},
            )
            return None
        path = ctx.tempfile()
        ctx.parquet_to_file(f"SELECT * FROM ({sql})", path)
        # standard cross-engine: datetime NAIVE (istante UTC). In modalità s3 il
        # file lo scrive il server (timestamp con fuso UTC): lo normalizzano i
        # lettori di ogni engine allo scan (vedi app.engine.temporal).
        rewrite_parquet_naive_utc(path)
        self.storage.upload_file(path, dest.bucket, dest.key)
        return path

    def _copy(self, ctx: ClickHouseContext, src: DataSource, dest: DataSource) -> None:
        """Copia server-side tra due oggetti (solo s3): rilegge il parquet appena
        scritto invece di rieseguire la catena."""
        ctx.command(
            f"INSERT INTO FUNCTION {ctx.s3_fn(dest)} SELECT * FROM {ctx.s3_fn(src)}",
            settings={**_PARQUET_OUT, "s3_truncate_on_insert": 1, "s3_create_new_file_on_insert": 0},
        )

    def _describe_object(self, ctx: ClickHouseContext, obj: DataSource, local_path: str | None) -> tuple[int, list]:
        """(righe, colonne) di un parquet scritto: dal file locale se c'è, altrimenti
        dal server (count via metadati parquet + schema da LIMIT 1)."""
        if local_path is not None:
            lf = pl.scan_parquet(local_path)
            n = lf.select(pl.len()).collect(engine="streaming").item()
            return int(n), _columns_of(lf.collect_schema())
        fn = ctx.s3_fn(obj)
        n = ctx.scalar(f"SELECT count() FROM {fn}")
        df = pl.read_parquet(io.BytesIO(ctx.parquet_bytes(f"SELECT * FROM {fn} LIMIT 1")))
        return n, _columns_of(df.schema)

    # ── Preview ───────────────────────────────────────────────────────────
    def preview(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        limit: int = 100,
        use_cache: bool = True,
        sort_keys: list[str] | None = None,
    ) -> PreviewResult:
        ops = _coerce_ops(operations)
        client = self._client()
        ctx = ClickHouseContext(client, self.storage, self.cfg, [], preview_limit=limit + 1,
                                matviews=self.matviews, allow_matview=True, sort_keys=sort_keys)
        if _needs_scan_tuning(ops):
            threads = self._scan_max_threads(ctx)
            if threads:
                ctx.settings["max_threads"] = threads
        try:
            if use_cache:
                self._materialize(ctx, source, ops[:-1])
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            sql = self._sql_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
            raw = ctx.parquet_bytes(f"SELECT * FROM ({sql}) LIMIT {limit + 1}")
            # standard cross-engine: datetime NAIVE (istante UTC), non "+00:00"
            df = naive_utc(pl.read_parquet(io.BytesIO(raw))) if raw else pl.DataFrame()
            truncated = df.height > limit
            if truncated:
                df = df.head(limit)
            return PreviewResult(
                columns=_columns_of(df.schema),
                rows=df.to_dicts(),
                row_count=df.height,
                truncated=truncated,
            )
        finally:
            ctx.cleanup()
            client.close()

    # ── Run ───────────────────────────────────────────────────────────────
    def run(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        destination: DataSource,
        use_cache: bool = True,
    ) -> RunResult:
        ops = _coerce_ops(operations)
        client = self._client()
        ctx = ClickHouseContext(client, self.storage, self.cfg, [])
        threads = self._scan_max_threads(ctx)
        if threads:
            ctx.settings["max_threads"] = threads
        try:
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            sql = self._sql_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
            cache_dest = None
            if use_cache and ops and not self.cache.has(hashes[-1]):
                cache_dest = DataSource(bucket=self.cache.bucket, key=self.cache.object_key(hashes[-1]))

            if ctx.cfg.transport == "s3" and cache_dest is not None:
                # una sola esecuzione della catena: scrive in cache, poi copia
                # server-side sulla destinazione
                self._write(ctx, sql, cache_dest)
                self.cache.mark(hashes[-1])
                self._copy(ctx, cache_dest, destination)
                local = None
            else:
                local = self._write(ctx, sql, destination)
                if cache_dest is not None:  # push: il file locale c'è, lo ricarica
                    self.storage.upload_file(local, cache_dest.bucket, cache_dest.key)
                    self.cache.mark(hashes[-1])

            rows_written, columns = self._describe_object(ctx, destination, local)
            return RunResult(destination=destination, rows_written=rows_written, columns=columns)
        finally:
            ctx.cleanup()
            client.close()

    # ── Manutenzione delle copie del viewer (chiamata dal task beat) ───────────
    def evict_matviews(self) -> int:
        """Droppa le tabelle materializzate scadute e le orfane. No-op se la
        materializzazione non è attiva."""
        if not self.cfg.materialize_enabled:
            return 0
        client = self._client()
        ctx = ClickHouseContext(client, self.storage, self.cfg, [])
        try:
            return self.matviews.evict(ctx, self.cfg.materialize_ttl_seconds)
        finally:
            client.close()
