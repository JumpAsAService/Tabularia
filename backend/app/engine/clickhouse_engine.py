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
import threading
import time
import logging
import os
import re
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
from app.engine.principal import AI, current_principal
from app.engine.exceptions import EngineError, OperationError, SourceNotFoundError
from app.engine.polars_engine import _coerce_ops, _columns_of
from app.engine.temporal import naive_utc, rewrite_parquet_naive_utc
from app.engine.query_tag import current_query_tag, is_safe_tag, was_interrupted
from app.observability import preview_stats

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

# La copia gira DENTRO la richiesta di preview, che l'API abbandona a 120s
# (PREVIEW_TIMEOUT_SECONDS) revocando il task: il worker viene terminato con un
# segnale, il `except` che droppa la tabella temporanea NON gira e resta un
# orfano. Con un tetto più basso la copia fallisce da sola, in modo pulito:
# cleanup eseguito, backoff registrato, preview servita da s3().
_MATERIALIZE_MAX_SECONDS = 60

# un client ClickHouse per thread (vedi _client): l'handshake costa piu' della query
_CLIENTI_PER_THREAD = threading.local()
# quanto puo' restare inattivo un client prima di rivalidarlo (secondi)
_VALIDA_CLIENT_DOPO = 30.0


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


# Esiti POSITIVI del controllo di esistenza, per processo: (bucket, key) -> scadenza.
# Un HEAD verso l'object storage costa ~11 ms — un sesto di una preview veloce —
# e si paga per OGNI sorgente di OGNI preview (i lati join compresi), sempre sulla
# stessa chiave. Le chiavi sono immutabili (ogni refresh ne crea una nuova), quindi
# un file che esiste non cambia sotto di noi. Si cachea SOLO il positivo: un file
# assente puo' comparire da un momento all'altro, e ricordarselo assente farebbe
# fallire una sorgente appena caricata.
_ESISTENZA_TTL = 60.0
_ESISTENZA: dict[tuple[str, str], float] = {}


def _source_exists(storage, source: DataSource) -> bool:
    """Esistenza dell'oggetto senza scaricarlo (HEAD); i doppioni di test espongono
    `exists`. L'esito positivo resta valido per qualche decina di secondi."""
    chiave = (source.bucket, source.key)
    scadenza = _ESISTENZA.get(chiave)
    adesso = time.monotonic()
    if scadenza is not None and scadenza > adesso:
        return True
    if scadenza is not None:
        _ESISTENZA.pop(chiave, None)
        if len(_ESISTENZA) > 4096:  # potatura: non deve crescere all'infinito
            for k, v in list(_ESISTENZA.items()):
                if v <= adesso:
                    _ESISTENZA.pop(k, None)

    def _ricorda(esito: bool) -> bool:
        if esito:
            _ESISTENZA[chiave] = time.monotonic() + _ESISTENZA_TTL
        return esito

    head = getattr(storage, "head_object", None)
    if head is None:
        exists = getattr(storage, "exists", None)
        return True if exists is None else _ricorda(bool(exists(source.bucket, source.key)))
    try:
        head(source.bucket, source.key)
        return _ricorda(True)
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
        # da dove e' stata letta la sorgente RADICE: la copia MergeTree copre
        # un caso stretto (radice del viewer, sopra soglia, use_cache=False,
        # transport s3), quindi il caso NORMALE resta s3(). Senza questo, un
        # `query=5000ms` nei log non si sa se e' una scansione del parquet o
        # una lettura dalla copia: diagnosi opposte.
        self.fonte_radice = "?"
        self.settings: dict[str, Any] = {}
        # etichetta del lavoro in corso (vedi query_tag.py): permette di uccidere
        # sul server le query di una preview interrotta
        _tag = current_query_tag()
        if _tag:
            self.settings["log_comment"] = _tag
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
                if root:
                    self.fonte_radice = "matview"
                return f"SELECT * FROM {table}"
        if root:
            self.fonte_radice = "s3" if self.cfg.transport != "push" else "push"
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
        cap = {"max_execution_time": _MATERIALIZE_MAX_SECONDS}
        self.command(f"CREATE TABLE {tmp} ({ddl}) ENGINE = MergeTree ORDER BY {order}{opts}", settings=cap)
        try:
            self.command(f"INSERT INTO {tmp} SELECT * FROM {s3}", settings=cap)
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


_was_interrupted = was_interrupted  # definita in query_tag: la usa anche il task


from contextlib import contextmanager

from app.engine.stepcache_defer import DeferredStepCache


class ClickHouseEngine(DeferredStepCache, Engine):
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

    def _credentials(self) -> tuple[str, str]:
        """Le query dell'assistente AI girano con l'utenza dedicata, se c'è
        (vedi engine/principal.py). Il client è in cache PER UTENTE (la chiave
        lo contiene): le due identità non si scambiano mai la connessione."""
        if self._as_ai():
            return self.cfg.ai_username, self.cfg.ai_password.get_secret_value()
        return self.cfg.username or "default", self.cfg.password.get_secret_value()

    def _as_ai(self) -> bool:
        """True se QUESTA query gira con l'utenza dell'assistente. Solo col
        trasporto `s3`: in `push` la preview crea tabelle di appoggio sul server,
        e un'utenza di sola lettura non può — lì resta l'utenza principale."""
        return bool(
            current_principal() == AI
            and self.cfg.transport == "s3"
            and self.cfg.ai_username
            and self.cfg.ai_password.get_secret_value()
        )

    def _client(self):
        """Client RIUSATO per thread. Crearlo costa tre round-trip (version+
        timezone, l'intera system.settings ~424 KiB, un ping): nei log valeva
        113-130 ms, PIU' della query stessa. Su rete remota e' il costo fisso
        piu' grande di una preview, pagato a ogni richiesta.

        Per thread e non globale: i worker Celery sono processi separati (nessuna
        condivisione), ma l'API FastAPI e' multi-thread e due thread sullo stesso
        client si pesterebbero i piedi.

        Prima del riuso si valida con un `SELECT 1` (~10 ms): senza, una
        connessione morta — server riavviato, rete caduta, idle chiuso dal
        bilanciatore — resterebbe in cache e farebbe fallire OGNI preview
        successiva. Dieci millisecondi per non barattare velocita' con fragilita'.
        """
        import clickhouse_connect

        utente, password = self._credentials()
        chiave = (self.cfg.host, self.cfg.port, self.cfg.database or "default",
                  utente, bool(self.cfg.secure))
        cache = getattr(_CLIENTI_PER_THREAD, "per_chiave", None)
        if cache is None:
            cache = _CLIENTI_PER_THREAD.per_chiave = {}

        esistente, ultimo_uso = cache.get(chiave, (None, 0.0))
        if esistente is not None:
            # La validazione costa un round-trip (~11 ms): su preview consecutive
            # sarebbe la voce piu' grande dopo la query, spesa per chiedere "sei
            # vivo?" a una connessione usata due secondi fa. Si valida solo dopo
            # una pausa, quando un bilanciatore o il server possono davvero aver
            # chiuso la connessione da sotto.
            if time.monotonic() - ultimo_uso < _VALIDA_CLIENT_DOPO:
                cache[chiave] = (esistente, time.monotonic())
                return esistente
            try:
                esistente.command("SELECT 1")
                cache[chiave] = (esistente, time.monotonic())
                return esistente
            except Exception as e:
                cache.pop(chiave, None)
                if was_interrupted(e):  # preview superata durante la validazione
                    try:
                        esistente.close()
                    except Exception:
                        pass
                    raise
                try:
                    esistente.close()
                except Exception:
                    pass  # gia' morto: buttarlo e basta

        try:
            c = clickhouse_connect.get_client(
                host=self.cfg.host,
                port=self.cfg.port,
                username=utente,
                password=password,
                database=self.cfg.database or "default",
                secure=self.cfg.secure,
                connect_timeout=self.cfg.connect_timeout,
                # NIENTE sessione: l'engine non usa tabelle temporanee ne' SET, e
                # una sessione e' un lucchetto — finche' una query interrotta gira
                # ancora sul server, ogni altra query dello stesso client fallisce
                # con "Session … is locked by a concurrent client". Con il client
                # riusato per thread, una preview annullata avvelenava il processo.
                autogenerate_session_id=False,
            )
        except Exception as e:
            raise EngineError(_clean_error(e, self.cfg.host, self.cfg.port)) from e
        cache[chiave] = (c, time.monotonic())
        return c

    def _forget_client(self) -> None:
        """Butta il client in cache di questo thread. Dopo un'interruzione a
        meta' risposta la connessione e' in uno stato ignoto: meglio pagare un
        handshake che servire la prossima preview con un socket a meta' lettura."""
        cache = getattr(_CLIENTI_PER_THREAD, "per_chiave", None) or {}
        for chiave, (c, _) in list(cache.items()):
            cache.pop(chiave, None)
            try:
                c.close()
            except Exception:
                pass

    def kill_tagged(self, tag: str) -> None:
        """Uccide sul server le query con questa etichetta. Best-effort e con un
        client NUOVO (quello del lavoro interrotto non e' affidabile). ASYNC: non
        si aspetta la fine, basta che il server smetta di lavorarci."""
        if not is_safe_tag(tag):
            return
        try:
            import clickhouse_connect  # import locale, come in _client(): dipendenza opzionale

            c = clickhouse_connect.get_client(
                host=self.cfg.host, port=self.cfg.port, username=self.cfg.username or "default",
                password=self.cfg.password.get_secret_value(), database=self.cfg.database or "default",
                secure=self.cfg.secure, connect_timeout=self.cfg.connect_timeout,
                autogenerate_session_id=False,
            )
            try:
                c.command(f"KILL QUERY WHERE Settings['log_comment'] = '{tag}' ASYNC")
            finally:
                c.close()
        except Exception as e:  # non deve mai far fallire chi sta pulendo
            logger.warning("KILL QUERY per %s non riuscita: %s", tag, _clean_error(e))

    def _source_id(self, source: DataSource) -> str:
        return f"{self.engine_name}:{source.bucket}/{source.key}"

    def _scan_max_threads(self, ctx) -> int:
        """max_threads da imporre a una scansione, o 0 per non toccare. Legge i
        core del server una volta per processo; su errore lascia perdere."""
        # solo con transport s3: in push il server non tocca l'object storage,
        # non c'è latenza di rete da nascondere e l'oversubscription danneggia
        if self.cfg.transport != "s3" or self.cfg.parquet_scan_max_threads <= 0:
            return 0
        if self._server_threads is None:
            try:
                # il valore può essere "8" oppure "auto(8)": int() da solo
                # fallirebbe, azzerando il guard «non abbassare un server grande»
                raw = str(ctx._rows("SELECT value FROM system.settings WHERE name = 'max_threads'")[0][0])
                digits = re.search(r"\d+", raw)
                self._server_threads = int(digits.group()) if digits else 0
            except Exception as e:
                if was_interrupted(e):  # non e' il server: e' la preview superata
                    raise
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
        if self._too_big(ctx, sql, final):
            return
        self._write(ctx, sql, DataSource(bucket=self.cache.bucket, key=self.cache.object_key(final)))
        self.cache.mark(final)
        logger.info("materializzato step %d in cache", len(operations))

    @contextmanager
    def _materialize_session(self, source: DataSource):
        client = self._client()
        ctx = ClickHouseContext(client, self.storage, self.cfg, [])
        threads = self._scan_max_threads(ctx)
        if threads:
            ctx.settings["max_threads"] = threads
        try:
            yield ctx
        finally:
            ctx.cleanup()

    def _bounded_rows(self, ctx, sql: str, n: int) -> int:
        return ctx.scalar(f"SELECT count() FROM (SELECT 1 FROM ({sql}) LIMIT {n + 1})")

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
        # Cronometro per FASI: quando una preview "ci mette troppo" serve sapere
        # QUALE pezzo, non il totale. Le fasi sono disgiunte; cio' che manca
        # rispetto al tempo visto dal chiamante e' coda o trasporto.
        _t0 = time.perf_counter()
        _fasi: dict[str, float] = {}

        def _fase(nome, da):
            ora = time.perf_counter()
            _fasi[nome] = (ora - da) * 1000
            return ora

        ops = _coerce_ops(operations)
        _t = _t0
        client = self._client()
        _t = _fase("client", _t)
        # `allow_matview` SOLO per le query esplorative del viewer (use_cache=False).
        # L'editor usa la step-cache: lì la copia (a) è sincrona dentro la richiesta
        # e fa "appendere" la preview, (b) nascerebbe con un'identità diversa (senza
        # sort_keys) duplicando l'intero dataset, e (c) i blob di step-cache scritti
        # LEGGENDO dalla copia finirebbero nello stesso namespace di hash usato dai
        # run, che la copia non ce l'hanno.
        ctx = ClickHouseContext(client, self.storage, self.cfg, [], preview_limit=limit + 1,
                                matviews=self.matviews,
                                # la copia materializzata si CREA sul server: l'utenza
                                # `ai` è di sola lettura, quindi legge dal parquet
                                allow_matview=not use_cache and not self._as_ai(),
                                sort_keys=sort_keys)
        if _needs_scan_tuning(ops):
            threads = self._scan_max_threads(ctx)
            if threads:
                ctx.settings["max_threads"] = threads
        try:
            if use_cache:
                self._defer_materialization(source, ops[:-1])  # DOPO la risposta, in un task a parte
                _t = _fase("stepcache", _t)
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            # include la risoluzione della copia materializzata: se qui il tempo
            # e' alto, la copia si sta CREANDO adesso (prima volta o dopo un drop)
            sql = self._sql_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
            _t = _fase("sql", _t)
            raw = ctx.parquet_bytes(f"SELECT * FROM ({sql}) LIMIT {limit + 1}")
            _t = _fase("query", _t)
            # standard cross-engine: datetime NAIVE (istante UTC), non "+00:00"
            df = naive_utc(pl.read_parquet(io.BytesIO(raw))) if raw else pl.DataFrame()
            truncated = df.height > limit
            if truncated:
                df = df.head(limit)
            cstate, ccap = self._cache_state(source, ops[:-1], use_cache)
            res = PreviewResult(
                cache_state=cstate, cache_cap_rows=ccap,
                columns=_columns_of(df.schema),
                rows=df.to_dicts(),
                row_count=df.height,
                truncated=truncated,
            )
            _fase("decode", _t)
            preview_stats.set_phases(_fasi, ctx.fonte_radice)  # le raccoglie preview_task
            logger.info(
                "preview clickhouse %.0f ms | %s | righe=%d colonne=%d limit=%d ops=%d cache=%s scaricati=%.0fKB fonte=%s",
                (time.perf_counter() - _t0) * 1000,
                " ".join("%s=%.0fms" % kv for kv in _fasi.items()),
                res.row_count, len(res.columns), limit, len(ops),
                "on" if use_cache else "off",
                (len(raw) / 1024) if raw else 0,
                ctx.fonte_radice,
            )
            return res
        except BaseException as e:
            # Preview INTERROTTA (superata da una piu' recente: il worker riceve
            # SIGUSR1 e qui arriva SoftTimeLimitExceeded, di solito avvolta in un
            # EngineError da _rows). Due pulizie che nessun altro puo' fare:
            # il server sta ANCORA eseguendo la query, e la connessione e' rimasta
            # a meta' risposta. Misurato: senza, il server lavorava a vuoto e le
            # preview successive di questo processo fallivano.
            if _was_interrupted(e):
                self._forget_client()
                tag = ctx.settings.get("log_comment")
                if tag:
                    self.kill_tagged(tag)
            raise
        finally:
            ctx.cleanup()
            # NIENTE client.close(): e' condiviso col prossimo task di questo
            # thread. Muore col processo (i figli Celery si riciclano), e se la
            # connessione cade la validazione in _client() la sostituisce.

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

            # una sola esecuzione della catena, sulla destinazione; la copia in
            # cache viene DOPO, e solo se l'output sta nel tetto (un output da
            # gigabyte non vale una seconda scrittura)
            local = self._write(ctx, sql, destination)
            rows_written, columns = self._describe_object(ctx, destination, local)
            if cache_dest is not None and self._cache_output_allowed(rows_written):
                if local is None:
                    self._copy(ctx, destination, cache_dest)  # s3: copia server-side
                else:
                    self.storage.upload_file(local, cache_dest.bucket, cache_dest.key)
                self.cache.mark(hashes[-1])
            return RunResult(destination=destination, rows_written=rows_written, columns=columns)
        finally:
            ctx.cleanup()
            # NIENTE client.close(): e' condiviso col prossimo task di questo
            # thread. Muore col processo (i figli Celery si riciclano), e se la
            # connessione cade la validazione in _client() la sostituisce.

    # ── Manutenzione delle copie del viewer (chiamata dal task beat) ───────────
    def evict_matviews(self) -> int:
        """Droppa le tabelle materializzate scadute e le orfane. No-op solo se
        ClickHouse non è configurato (o il trasporto è push): con la
        materializzazione SPENTA lo sweep deve girare lo stesso, per rimuovere
        ciò che è rimasto da quando era accesa."""
        if not self.cfg.matview_sweep_enabled:
            return 0
        client = self._client()
        ctx = ClickHouseContext(client, self.storage, self.cfg, [])
        try:
            return self.matviews.evict(ctx, self.cfg.materialize_ttl_seconds)
        finally:
            ctx.cleanup()  # il client resta al thread (vedi _client)
