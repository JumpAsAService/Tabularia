"""
Engine basato su Google BigQuery (serverless, a consumo).

I parquet restano dove sono: ogni sorgente diventa una TABELLA ESTERNA
TEMPORANEA del job (`gs://bucket/chiave`, formato PARQUET), quindi serve che lo
storage di Tabularia sia Google Cloud Storage. La catena di operazioni e' una
sola query (`bigquery_ops`, SELECT annidate) che BigQuery esegue in una volta;
le colonne di ogni passo si conoscono con un DRY RUN, che e' gratuito. Il
risultato torna al worker con la Storage Read API (Arrow, streaming) e viene
scritto in parquet come per gli altri motori.

Costi: BigQuery fattura i byte LETTI dalle colonne toccate, anche con LIMIT.
Il campione automatico di sviluppo e il tetto `maximum_bytes_billed` (rifiuto
PRIMA di eseguire) sono le due difese. Ogni query e' etichettata con il tag
della preview, cosi' una preview superata viene annullata sul servizio.

Step-cache NATIVA (mirror di quella parquet degli altri motori): l'output dei
passi intermedi dell'editor viene scritto con CREATE TABLE … AS SELECT in un
dataset del progetto (`cache_dataset`), con scadenza, e le preview successive
ripartono da quella tabella nativa: piccola, colonnare, quasi gratis da
leggere. La scrittura e' DIFFERITA: la preview risponde subito dall'antenato
in cache piu' vicino e lascia in `take_pending` i passi da materializzare, che
il task della preview affida a un task separato (`materialize`). L'indice degli hash sta su Valkey in uno spazio separato
(`BigQueryStepCache`), i nomi originali delle colonne nella descrizione della
tabella. Il Viewer (`use_cache=False`) non la tocca.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from app.core.config import BigQuerySettings, get_settings
from app.engine.base import DataSource, Engine, Operation, PreviewResult, RunResult
from app.engine.cache import StepCache, plan_hashes
from app.engine.bigquery_ops import _qi, get_bigquery_operation
from app.engine.exceptions import EngineError, OperationError, SourceNotFoundError
from app.engine.polars_engine import _coerce_ops, _columns_of
from app.engine.query_tag import current_query_tag, is_safe_tag, was_interrupted
from app.engine.temporal import naive_utc, rewrite_parquet_naive_utc

logger = logging.getLogger(__name__)

USD_PER_TIB = 6.25  # listino on-demand, per il log dei costi


def _label(tag: str) -> str:
    """Le etichette dei job ammettono solo [a-z0-9_-], max 63: 'tab-prev:<id>' → 'tab-prev-<id>'."""
    return re.sub(r"[^a-z0-9_-]", "-", tag.lower())[:63]


_SAFE_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,299}$")


def _sanitize(name: str) -> str:
    """Nome ammesso da BigQuery: solo [A-Za-z0-9_], non inizia per cifra, non vuoto."""
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(name))[:120]
    if not s:
        s = "_col"
    if s[0].isdigit():
        s = "_" + s
    return s


class _RangeFile:
    """File in sola lettura su un oggetto S3/GCS con GET a intervalli: pyarrow
    legge cosi' SOLO il footer del parquet (due richieste piccole), niente
    download del file."""

    def __init__(self, client, bucket: str, key: str, size: int):
        self._c, self._b, self._k, self._size, self._pos = client, bucket, key, size, 0
        self.closed = False

    def size(self) -> int:
        return self._size

    def tell(self) -> int:
        return self._pos

    def seek(self, pos: int, whence: int = 0) -> int:
        self._pos = {0: pos, 1: self._pos + pos, 2: self._size + pos}[whence]
        return self._pos

    def read(self, n: int = -1) -> bytes:
        if n is None or n < 0:
            n = self._size - self._pos
        if n <= 0 or self._pos >= self._size:
            return b""
        end = min(self._pos + n, self._size) - 1
        body = self._c.get_object(Bucket=self._b, Key=self._k, Range=f"bytes={self._pos}-{end}")["Body"].read()
        self._pos += len(body)
        return body

    def close(self) -> None:
        self.closed = True

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False


def parquet_column_names(storage, bucket: str, key: str) -> list[str] | None:
    """Nomi colonna VERI del parquet (dal footer, letto a intervalli). None se lo
    storage non espone un client S3 (finti dei test)."""
    client = getattr(storage, "client", None)
    if client is None:
        return None
    size = int(client.head_object(Bucket=bucket, Key=key)["ContentLength"])
    f = pa.PythonFile(_RangeFile(client, bucket, key, size), mode="r")
    try:
        return list(pq.read_schema(f).names)
    finally:
        f.close()


def _bq_string(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _cached_names(table) -> list[str] | None:
    """I nomi ORIGINALI delle colonne di una tabella di cache (descrizione JSON)."""
    if table is None:
        return None
    try:
        names = json.loads(table.description or "").get("names")
    except (ValueError, AttributeError):
        return None
    return names if isinstance(names, list) and len(names) == len(table.schema) else None


class BigQueryStepCache(StepCache):
    """Indice (Valkey, spazio `bigquery`) delle tabelle di cache: il «blob» e'
    una tabella nativa `<progetto>.<dataset>.s_<hash>` che scade da sola sul
    servizio; qui si verifica che esista ancora e si allinea l'indice."""

    def __init__(self, storage, engine: "BigQueryEngine", redis_client=None):
        super().__init__(storage, redis_client, namespace="bigquery")
        self.engine = engine

    def table_ref(self, h: str) -> str:
        cfg = self.engine.cfg
        return f"{cfg.project}.{cfg.cache_dataset}.s_{h}"

    def get_table(self, h: str):
        """La tabella di cache, o None se non c'e' (piu'). Errori diversi dal
        «non trovato» risalgono: non vanno confusi con una cache vuota."""
        from google.api_core.exceptions import NotFound

        try:
            return self.engine._client_or_raise().get_table(self.table_ref(h))
        except NotFound:
            return None

    def blob_exists(self, h: str) -> bool:
        try:
            return self.get_table(h) is not None
        except Exception as e:  # noqa: BLE001
            from app.engine.query_tag import was_interrupted

            if was_interrupted(e):
                raise
            return True  # non si sa: la query successiva lo dira' in modo esplicito

    def evict_expired(self, ttl_seconds: int) -> int:
        try:
            self._reconcile()
            cutoff = time.time() - ttl_seconds
            expired = self.redis.zrangebyscore(self.atime_zset, "-inf", cutoff)
            alive = list(self.redis.zrangebyscore(self.atime_zset, cutoff, "+inf"))
        except Exception:  # noqa: BLE001 — Valkey giu': niente da fare
            return 0
        client = self.engine._client_or_raise()
        for h in expired:
            client.delete_table(self.table_ref(h), not_found_ok=True)
            self.forget(h)
        orphans = [h for h in alive if not self.blob_exists(h)]
        for h in orphans:
            self.forget(h)
        if expired or orphans:
            logger.info("cache BigQuery: rimosse %d tabelle scadute e %d voci orfane", len(expired), len(orphans))
        return len(expired) + len(orphans)

    def clear(self) -> int:
        try:
            hashes = list(self.redis.smembers(self.index_set))
        except Exception:  # noqa: BLE001
            return 0
        client = self.engine._client_or_raise()
        for h in hashes:
            client.delete_table(self.table_ref(h), not_found_ok=True)
            self.forget(h)
        return len(hashes)


def _clean_error(e: Exception) -> str:
    """Il messaggio di BigQuery senza il rumore dell'API (job id, URL, elenco errori doppio)."""
    errors = getattr(e, "errors", None)
    if errors and isinstance(errors, list) and isinstance(errors[0], dict) and errors[0].get("message"):
        msg = str(errors[0]["message"])
    else:
        msg = str(e)
    msg = re.sub(r"^\d{3}\s+(?:GET|POST|PUT)\s+\S+:\s*", "", msg)
    return msg.strip()


class BigQueryContext:
    """Stato di esecuzione di una catena: client, tabelle esterne registrate,
    schema dei frammenti gia' chiesti (dry run), job lanciati, contatori."""

    def __init__(self, client, storage, cfg: BigQuerySettings, location: str | None, tmp: list[str],
                 preview_limit: int | None = None):
        self.client = client
        self.storage = storage
        self.cfg = cfg
        self.location = location
        self.tmp = tmp
        self.preview_limit = preview_limit
        self._n = 0
        self.tables: dict[str, Any] = {}  # alias → ExternalConfig
        self._schemas: dict[str, list[tuple[str, str]]] = {}
        # nomi colonna: originale → sicuro (quello che BigQuery vede) e ritorno
        self._safe: dict[str, str] = {}
        self._orig: dict[str, str] = {}
        self.jobs: list[Any] = []
        self.queries = 0
        self.dry_runs = 0
        self.bytes_billed = 0
        self.bytes_processed = 0
        self.phases: dict[str, float] = {}

    def uid(self, prefix: str) -> str:
        self._n += 1
        return f"_{prefix}{self._n}"

    # ── nomi colonna ─────────────────────────────────────────────────────
    def register(self, original: str, safe: str) -> None:
        self._safe[original] = safe
        self._orig[safe] = original

    def safe(self, name: str) -> str:
        """Nome sicuro di una colonna (registrato al primo uso, univoco)."""
        name = str(name)
        s = self._safe.get(name)
        if s is None:
            base = _sanitize(name)
            s, i = base, 1
            while s in self._orig and self._orig[s] != name:
                i += 1
                s = f"{base}_{i}"
            self.register(name, s)
        return s

    def qi(self, name: str) -> str:
        return _qi(self.safe(name))

    def original(self, safe: str) -> str:
        return self._orig.get(safe, safe)

    def map_quoted(self, expr: str) -> str:
        """Nelle espressioni libere (compute/sql) gli identificatori quotati con
        i backtick che sono colonne note vengono tradotti nel nome sicuro."""
        return re.sub(r"`([^`]+)`", lambda m: self.qi(m.group(1)) if m.group(1) in self._safe else m.group(0), expr)

    def tempfile(self) -> str:
        path = tempfile.mkstemp(suffix=".parquet", prefix="bigquery_")[1]
        self.tmp.append(path)
        return path

    # ── job ──────────────────────────────────────────────────────────────
    def _job_config(self, dry_run: bool):
        from google.cloud import bigquery

        jc = bigquery.QueryJobConfig(dry_run=dry_run, use_legacy_sql=False, use_query_cache=not dry_run)
        jc.table_definitions = dict(self.tables)
        if not dry_run and self.cfg.maximum_bytes_billed > 0:
            jc.maximum_bytes_billed = int(self.cfg.maximum_bytes_billed)
        tag = current_query_tag()
        labels = {"app": "tabularia"}
        if tag and is_safe_tag(tag):
            labels["tabularia_tag"] = _label(tag)
        jc.labels = labels
        return jc

    def query(self, sql: str, dry_run: bool = False):
        """Lancia una query e (se non e' un dry run) ne aspetta la fine. Errori
        di BigQuery → EngineError con il suo messaggio; un'interruzione (preview
        superata) annulla il job e viene rilanciata."""
        t0 = time.perf_counter()
        try:
            job = self.client.query(sql, job_config=self._job_config(dry_run), location=self.location)
            if dry_run:
                self.dry_runs += 1
                self.phases["dry_run"] = self.phases.get("dry_run", 0.0) + (time.perf_counter() - t0) * 1000
                return job
            self.jobs.append(job)
            job.result()
        except BaseException as e:
            if was_interrupted(e) or not isinstance(e, Exception):
                self.cancel_all()
                raise
            raise EngineError(f"BigQuery: {_clean_error(e)}") from e
        self.queries += 1
        self.bytes_billed += int(getattr(job, "total_bytes_billed", 0) or 0)
        self.bytes_processed += int(getattr(job, "total_bytes_processed", 0) or 0)
        self.phases["query"] = self.phases.get("query", 0.0) + (time.perf_counter() - t0) * 1000
        return job

    def cancel_all(self) -> None:
        """Annulla i job ancora in corso (best-effort: chi pulisce non deve fallire)."""
        for job in self.jobs:
            try:
                if job.state != "DONE":
                    self.client.cancel_job(job.job_id, location=self.location)
            except Exception as e:  # noqa: BLE001
                logger.warning("annullamento job BigQuery %s non riuscito: %s", getattr(job, "job_id", "?"), e)

    # ── introspezione (dry run: gratuita) ────────────────────────────────
    def schema_of(self, sql: str) -> list[tuple[str, str]]:
        cached = self._schemas.get(sql)
        if cached is not None:
            return cached
        job = self.query(sql, dry_run=True)
        schema = [(self.original(f.name), str(f.field_type).upper()) for f in (job.schema or [])]
        self._schemas[sql] = schema
        return schema

    def columns_of(self, sql: str) -> list[str]:
        return [name for name, _ in self.schema_of(sql)]

    def scalar(self, sql: str) -> int:
        rows = list(self.query(sql).result())
        return int(rows[0][0]) if rows and rows[0] and rows[0][0] is not None else 0

    def distinct_rows(self, base_sql: str, cols: list[str]) -> list[list]:
        sel = ", ".join(_qi(c) for c in cols)
        rows = self.query(f"SELECT DISTINCT {sel} FROM {base_sql} ORDER BY {sel}").result()
        return [list(r.values()) for r in rows]

    # ── sorgenti e catena ────────────────────────────────────────────────
    def scan(self, source: DataSource) -> str:
        from google.cloud import bigquery

        # BigQuery direbbe "Not found: Files gs://…" solo a query lanciata: un
        # HEAD sul bucket (gratuito) da' subito l'errore giusto
        if not self.storage.object_exists(source.bucket, source.key):
            raise SourceNotFoundError(source.bucket, source.key)
        alias = self.uid("src")
        ext = bigquery.ExternalConfig("PARQUET")
        ext.source_uris = [f"gs://{source.bucket}/{source.key}"]
        self.tables[alias] = ext
        sql = f"SELECT * FROM {_qi(alias)}"
        # BigQuery normalizza da solo i nomi del parquet non ammessi ("Ragione
        # Sociale" → Ragione_Sociale): si abbinano per POSIZIONE i nomi veri
        # (footer del file) con quelli che BigQuery espone (dry run, gratuito)
        names = parquet_column_names(self.storage, source.bucket, source.key)
        if names is not None:
            if all(_SAFE_NAME.match(n) for n in names):
                for n in names:
                    self.register(n, n)
            else:
                job = self.query(sql, dry_run=True)
                exposed = [f.name for f in (job.schema or [])]
                if len(exposed) != len(names):
                    raise EngineError(f"BigQuery espone {len(exposed)} colonne per {source.key}, il parquet ne ha {len(names)}")
                for n, e in zip(names, exposed):
                    self.register(n, e)
        return sql

    def scan_cached(self, table_ref: str, safe_names: list[str], originals: list[str]) -> str:
        """Radice della catena = una tabella di cache nativa. Le sue colonne
        portano i nomi SICURI: si ripristina la mappa verso gli originali."""
        for safe, orig in zip(safe_names, originals):
            self.register(orig, safe)
        return f"SELECT * FROM {_qi(table_ref)}"

    def apply(self, sql: str, ops, index_offset: int = 0) -> str:
        for i, op in enumerate(ops):
            op_type = op.type if isinstance(op, Operation) else op["type"]
            params = op.params if isinstance(op, Operation) else (op.get("params") or {})
            fn = get_bigquery_operation(op_type)
            try:
                sql = fn(sql, params, self)
            except EngineError:
                raise
            except Exception as e:
                if was_interrupted(e):
                    raise
                raise OperationError(op_type, i + index_offset, str(e)) from e
        return sql

    def build_right(self, ref: dict) -> str:
        if "source" in ref:
            sql = self.scan(DataSource(**ref["source"]))
            return self.apply(sql, ref.get("operations") or [])
        return self.scan(DataSource(**ref))

    def cost_line(self) -> str:
        usd = self.bytes_billed / 1024**4 * USD_PER_TIB
        return (f"query={self.queries} dry_run={self.dry_runs} letti={self.bytes_processed / 1e6:.1f}MB "
                f"fatturati={self.bytes_billed / 1e6:.1f}MB (~{usd:.4f}$)")


class BigQueryEngine(Engine):
    engine_name = "bigquery"

    def __init__(self, storage=None, cache=None, cfg: BigQuerySettings | None = None):
        if storage is None:
            from app.utils import get_storage_service

            storage = get_storage_service()
        self.storage = storage
        self.cfg = cfg or get_settings().bigquery
        self.cache = cache or BigQueryStepCache(storage, self)
        self._cache_ok: bool | None = None  # None = dataset non ancora verificato
        # passi da materializzare DOPO la risposta (il CTAS costa 2-3 s: il primo
        # click su un nodo non deve pagarlo; chi esegue la preview li drena con
        # `take_pending` e li affida a un task separato → `materialize`)
        self._pending: list[tuple[DataSource, list[dict]]] = []
        self._client = None
        self._credentials = None
        self._location: str | None = None
        self._bqstorage = None
        self._bqstorage_tried = False

    # ── client ───────────────────────────────────────────────────────────
    def _creds(self):
        if self._credentials is None:
            from google.oauth2 import service_account

            info = self.cfg.credentials_info()
            self._credentials = service_account.Credentials.from_service_account_info(
                info, scopes=["https://www.googleapis.com/auth/cloud-platform"]
            )
        return self._credentials

    def _client_or_raise(self):
        if self._client is None:
            try:
                from google.cloud import bigquery
            except ImportError as e:  # pragma: no cover
                raise EngineError("BigQuery engine: pacchetto google-cloud-bigquery non installato") from e
            try:
                self._client = bigquery.Client(project=self.cfg.project, credentials=self._creds())
            except Exception as e:
                raise EngineError(f"BigQuery: credenziali non valide ({_clean_error(e)})") from e
        return self._client

    def _bucket_location(self, bucket: str) -> str | None:
        """Regione del bucket (la query DEVE girare li'). Letta una volta."""
        if self.cfg.location.strip():
            return self.cfg.location.strip()
        if self._location is None:
            loc = ""
            fn = getattr(self.storage, "bucket_location", None)
            if callable(fn):
                loc = str(fn(bucket) or "")
            if not loc:
                logger.warning("regione del bucket %s non leggibile: lascio decidere BigQuery (default US)", bucket)
            self._location = loc.lower()
        return self._location or None

    def _bqstorage_client(self):
        """Storage Read API per scaricare i risultati grandi; senza il pacchetto
        si ripiega sull'API REST (lenta, ma funziona)."""
        if not self._bqstorage_tried:
            self._bqstorage_tried = True
            try:
                from google.cloud import bigquery_storage

                self._bqstorage = bigquery_storage.BigQueryReadClient(credentials=self._creds())
            except Exception as e:  # noqa: BLE001
                logger.info("Storage Read API non disponibile (%s): risultati via REST", e)
        return self._bqstorage

    # ── step-cache nativa ────────────────────────────────────────────────
    def _cache_enabled(self, location: str | None) -> bool:
        """La cache e' usabile se il dataset esiste o si lascia creare. Deciso
        una volta per processo; senza permessi si va avanti SENZA cache."""
        if not self.cfg.cache_dataset.strip():
            return False
        if self._cache_ok is None:
            try:
                from google.cloud import bigquery

                ds = bigquery.Dataset(f"{self.cfg.project}.{self.cfg.cache_dataset}")
                ds.location = location
                ds.default_table_expiration_ms = int(get_settings().cache.ttl_seconds) * 1000
                ds.description = "Tabularia: step-cache dell'editor (tabelle a scadenza automatica)"
                self._client_or_raise().create_dataset(ds, exists_ok=True)
                self._cache_ok = True
            except Exception as e:  # noqa: BLE001
                logger.warning(
                    "step-cache BigQuery disattivata: il dataset %s.%s non esiste e non si puo' creare (%s). "
                    "Concedi «BigQuery Data Editor» al service account, o crea il dataset nella regione %s.",
                    self.cfg.project, self.cfg.cache_dataset, _clean_error(e), location,
                )
                self._cache_ok = False
        return self._cache_ok

    def _source_id(self, source: DataSource) -> str:
        return f"{self.engine_name}:{source.bucket}/{source.key}"

    def _sql_from_cache(self, ctx: BigQueryContext, source, operations, hashes, record=False, use_cache=True) -> str:
        """Costruisce la catena partendo dall'antenato in cache piu' vicino e
        applicando solo le operazioni rimanenti (mirror degli altri motori)."""
        start = self.cache.nearest(hashes) if use_cache else 0
        table = None
        if start > 0:
            h = hashes[start - 1]
            table = self.cache.get_table(h)
            names = _cached_names(table)
            if table is None or names is None:
                self.cache.forget(h)  # tabella scaduta o senza mappa dei nomi: come una miss
                start, table = 0, None
        if record and operations and use_cache:
            (self.cache.record_hit if start > 0 else self.cache.record_miss)()
        if start == 0:
            sql = ctx.scan(source)
        else:
            self.cache.touch(hashes[start - 1])
            sql = ctx.scan_cached(self.cache.table_ref(hashes[start - 1]), [f.name for f in table.schema], names)
            logger.info("cache hit: riparto dallo step %d/%d", start, len(operations))
        return ctx.apply(sql, operations[start:], index_offset=start)

    def _materialize(self, ctx: BigQueryContext, source, operations) -> None:
        """Scrive in cache l'output di `operations` (se non c'e' gia'): CREATE
        TABLE AS SELECT con scadenza, nomi originali nella descrizione."""
        if not operations:
            return
        hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in operations])
        final = hashes[-1]
        if self.cache.has(final) and self.cache.blob_exists(final):
            return
        sql = self._sql_from_cache(ctx, source, operations, hashes)
        originals = ctx.columns_of(sql)
        description = json.dumps({"names": originals}, ensure_ascii=False)
        if len(description) > 15_000:  # limite della descrizione: questo passo resta senza cache
            return
        ttl = int(get_settings().cache.ttl_seconds)
        t0 = time.perf_counter()
        ctx.query(
            f"CREATE OR REPLACE TABLE {_qi(self.cache.table_ref(final))} "
            f"OPTIONS (expiration_timestamp = TIMESTAMP_ADD(CURRENT_TIMESTAMP(), INTERVAL {ttl} SECOND), "
            f"description = {_bq_string(description)}) AS {sql}"
        )
        ctx.phases["materialize"] = ctx.phases.get("materialize", 0.0) + (time.perf_counter() - t0) * 1000
        self.cache.mark(final)
        logger.info("materializzato step %d in cache BigQuery (%s)", len(operations), final[:12])

    def _defer_materialization(self, source: DataSource, ops: list[Operation]) -> None:
        """Mette in coda (in memoria) la materializzazione di `ops`, se non e'
        gia' in cache: il chiamante la esegue fuori dalla richiesta."""
        if not ops:
            return
        final = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])[-1]
        if self.cache.has(final) and self.cache.blob_exists(final):
            return
        self._pending.append((source, [op.model_dump() for op in ops]))

    def take_pending(self) -> list[tuple[DataSource, list[dict]]]:
        """Le materializzazioni rimaste da fare (e le dimentica)."""
        out, self._pending = self._pending, []
        return out

    def materialize(self, source: DataSource, operations: list[dict]) -> bool:
        """Materializza in cache l'output di `operations` (chiamata dal task
        differito). True se ha scritto qualcosa."""
        ops = _coerce_ops(operations)
        if not ops:
            return False
        final = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])[-1]
        if not self.cache.try_lock(final):  # due click sullo stesso passo: un solo CTAS
            return False
        tmp: list[str] = []
        ctx = self._context(source, tmp)
        try:
            if not self._cache_enabled(ctx.location):
                return False
            before = ctx.queries
            self._materialize(ctx, source, ops)
            if ctx.queries > before:
                logger.info("bigquery materialize | %s", ctx.cost_line())
            return ctx.queries > before
        except BaseException:
            ctx.cancel_all()
            raise
        finally:
            self.cache.unlock(final)
            self._cleanup(tmp)

    def _context(self, source: DataSource, tmp: list[str], preview_limit: int | None = None) -> BigQueryContext:
        return BigQueryContext(
            self._client_or_raise(), self.storage, self.cfg, self._bucket_location(source.bucket), tmp,
            preview_limit=preview_limit,
        )

    @staticmethod
    def _cleanup(tmp: list[str]) -> None:
        for p in tmp:
            try:
                os.remove(p)
            except OSError:
                pass

    def _record_phases(self, ctx: BigQueryContext) -> None:
        try:
            from app.observability import preview_stats

            preview_stats.set_phases(dict(ctx.phases), "bigquery")
        except Exception:  # noqa: BLE001 — la telemetria non deve mai rompere la preview
            pass

    # ── Preview ──────────────────────────────────────────────────────────
    def preview(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        limit: int = 100,
        use_cache: bool = True,
        sort_keys: list[str] | None = None,
    ) -> PreviewResult:
        ops = _coerce_ops(operations)
        tmp: list[str] = []
        ctx = self._context(source, tmp, preview_limit=limit + 1)
        try:
            cache_on = use_cache and self._cache_enabled(ctx.location)
            if cache_on:
                self._defer_materialization(source, ops[:-1])
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            sql = self._sql_from_cache(ctx, source, ops, hashes, record=True, use_cache=cache_on)
            job = ctx.query(f"SELECT * FROM ({sql}) LIMIT {limit + 1}")
            t0 = time.perf_counter()
            tbl = job.result().to_arrow(create_bqstorage_client=False)
            ctx.phases["fetch"] = (time.perf_counter() - t0) * 1000
            df = pl.from_arrow(tbl)
            if isinstance(df, pl.Series):
                df = df.to_frame()
            df = naive_utc(df).rename({c: ctx.original(c) for c in df.columns if ctx.original(c) != c})
            truncated = df.height > limit
            if truncated:
                df = df.head(limit)
            logger.info("bigquery preview | %s | righe=%d", ctx.cost_line(), df.height)
            self._record_phases(ctx)
            parent = ops[:-1]
            if not parent:
                cstate = None
            elif not cache_on:
                cstate = "off"
            else:
                pf = plan_hashes(self._source_id(source), [op.model_dump() for op in parent])[-1]
                cstate = "hit" if (self.cache.has(pf) and self.cache.blob_exists(pf)) else "pending"
            return PreviewResult(
                columns=_columns_of(df.schema), rows=df.to_dicts(), row_count=df.height, truncated=truncated,
                cache_state=cstate,
            )
        except BaseException:
            ctx.cancel_all()
            raise
        finally:
            self._cleanup(tmp)

    # ── Run ──────────────────────────────────────────────────────────────
    def _write_parquet(self, ctx: BigQueryContext, job, path: str) -> int:
        """Scarica il risultato del job in un parquet locale, in streaming, con
        row group grandi (INGEST__PARQUET_ROW_GROUP_ROWS) come gli altri motori."""
        rows = job.result()
        total = int(rows.total_rows or 0)
        group_rows = max(1, int(get_settings().ingest.parquet_row_group_rows))
        bqs = self._bqstorage_client()
        try:
            batches = rows.to_arrow_iterable(bqstorage_client=bqs) if bqs else rows.to_arrow_iterable()
        except TypeError:  # versioni vecchie senza il kwarg
            batches = rows.to_arrow_iterable()
        writer: pq.ParquetWriter | None = None
        pending: list[pa.RecordBatch] = []
        pending_rows = 0
        schema = None

        def flush() -> None:
            nonlocal writer, pending, pending_rows
            if not pending:
                return
            table = pa.Table.from_batches(pending, schema=schema)
            if writer is None:
                writer = pq.ParquetWriter(path, table.schema, compression="zstd")
            writer.write_table(table, row_group_size=group_rows)
            pending, pending_rows = [], 0

        try:
            for batch in batches:
                names = [ctx.original(c) for c in batch.schema.names]
                if names != batch.schema.names:
                    batch = batch.rename_columns(names)
                if schema is None:
                    schema = batch.schema
                pending.append(batch)
                pending_rows += batch.num_rows
                if pending_rows >= group_rows:
                    flush()
            flush()
            if writer is None:  # nessuna riga: parquet vuoto con lo schema del job
                schema = schema or self._arrow_schema(rows.schema)
                schema = pa.schema([f.with_name(ctx.original(f.name)) for f in schema], metadata=schema.metadata)
                writer = pq.ParquetWriter(path, schema, compression="zstd")
        finally:
            if writer is not None:
                writer.close()
        rewrite_parquet_naive_utc(path)  # TIMESTAMP (UTC) → naive, standard cross-engine
        return total

    @staticmethod
    def _arrow_schema(fields) -> pa.Schema:
        from google.cloud.bigquery import _pandas_helpers  # schema vuoto: solo per il file senza righe

        return _pandas_helpers.bq_to_arrow_schema(fields) or pa.schema([])

    def run(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        destination: DataSource,
        use_cache: bool = True,
    ) -> RunResult:
        ops = _coerce_ops(operations)
        tmp: list[str] = []
        ctx = self._context(source, tmp)
        try:
            cache_on = use_cache and self._cache_enabled(ctx.location)
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            sql = self._sql_from_cache(ctx, source, ops, hashes, record=True, use_cache=cache_on)
            job = ctx.query(sql)
            out_path = ctx.tempfile()
            t0 = time.perf_counter()
            rows_written = self._write_parquet(ctx, job, out_path)
            ctx.phases["fetch"] = (time.perf_counter() - t0) * 1000
            self.storage.upload_file(out_path, destination.bucket, destination.key)
            logger.info("bigquery run | %s | righe=%d", ctx.cost_line(), rows_written)
            written = pl.scan_parquet(out_path)
            return RunResult(
                destination=destination, rows_written=int(rows_written), columns=_columns_of(written.collect_schema()),
            )
        except BaseException:
            ctx.cancel_all()
            raise
        finally:
            self._cleanup(tmp)

    # ── annullamento delle query di una preview superata ─────────────────
    def kill_tagged(self, tag: str) -> None:
        """Annulla i job ancora in corso etichettati con `tag` (best-effort)."""
        if not is_safe_tag(tag):
            return
        want = _label(tag)
        try:
            client = self._client_or_raise()
            since = datetime.now(timezone.utc) - timedelta(minutes=15)
            for job in client.list_jobs(state_filter="running", min_creation_time=since, max_results=50):
                if (getattr(job, "labels", None) or {}).get("tabularia_tag") == want:
                    client.cancel_job(job.job_id, location=job.location)
        except Exception as e:  # noqa: BLE001
            logger.warning("annullamento dei job BigQuery %s non riuscito: %s", tag, e)
