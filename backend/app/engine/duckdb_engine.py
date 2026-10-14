"""
Engine basato su DuckDB — alternativa OUT-OF-CORE a Polars (stessa IR).

DuckDB spilla su disco: aggregazioni/join molto grandi non fanno esplodere la
RAM. La catena è LAZY come in Polars — una relazione DuckDB è un piano, non dati.
Solo `preview` (LIMIT n) e `run` (`write_parquet`, streaming su disco)
materializzano. Sources and cached steps are read straight from the object
storage, only the parts a query needs (see storage_direct); without that, the
file is downloaded first. L'output è scritto in streaming e caricato sullo storage.

Operazioni in `duckdb_ops`: strutturali single-input + join/union (leggono il
lato destro via il context) + pivot/unpivot + compute + `sql` (in a sandbox
without network) + `foreach`. Step-cache incrementale come Polars: si riparte dal
passo in cache più vicino, e la copia del passo a monte si fa dopo la risposta.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from typing import Any

import duckdb
import polars as pl
from botocore.exceptions import ClientError

from app.engine import duckdb_budget, storage_direct
from app.engine.base import DataSource, Engine, Operation, PreviewResult, RunResult
from app.engine.cache import StepCache, plan_hashes
from app.engine.duckdb_ops import _qi, get_duck_operation
from app.engine.exceptions import EngineError, OperationError, SourceNotFoundError
from app.engine.polars_engine import _coerce_ops, _columns_of

logger = logging.getLogger(__name__)

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NoSuchBucket"}


class DuckContext:
    """Stato di esecuzione di una catena DuckDB: connessione, storage, file
    temporanei e generatore di nomi UNIVOCI (alias delle operazioni, viste
    registrate). Le operazioni multi-input (join/union) lo usano per costruire
    il lato destro dalla sorgente annidata."""

    def __init__(self, con, storage, tmp: list[str], preview_limit: int | None = None, direct: bool = False):
        self.con = con
        self.storage = storage
        self.tmp = tmp
        # sources read in place on the object storage (see storage_direct)
        self.direct = direct
        self._n = 0
        # in preview il nodo `sql` limita il risultato del sandbox a queste righe
        # (altrimenti calcolerebbe l'intero risultato solo per mostrarne N), but
        # only when it is the chain's last operation: see `apply`
        self.preview_limit = preview_limit
        self.limit_output: int | None = None
        # budget cumulativo delle iterazioni foreach (anti-esplosione annidata)
        self.foreach_iterations = 0

    def uid(self, prefix: str) -> str:
        self._n += 1
        return f"_{prefix}{self._n}"

    def tempfile(self) -> str:
        path = tempfile.mkstemp(suffix=".parquet", prefix="duckdb_")[1]
        self.tmp.append(path)
        return path

    def scan(self, source: DataSource):
        address = storage_direct.duckdb_url(source.bucket, source.key) if self.direct else None
        if address is not None:
            # by ranges: the footer, then only the columns and row groups the query needs
            try:
                rel = self.con.read_parquet(address)
            except duckdb.HTTPException as e:
                if getattr(e, "status_code", None) == 404:
                    raise SourceNotFoundError(source.bucket, source.key) from e
                raise EngineError(f"Could not read {source.key} from the storage: {e}") from e
        else:
            path = self.tempfile()
            try:
                self.storage.download_file(source.bucket, source.key, path)
            except ClientError as e:
                code = str(e.response.get("Error", {}).get("Code", ""))
                if code in _NOT_FOUND_CODES:
                    raise SourceNotFoundError(source.bucket, source.key) from e
                raise
            rel = self.con.read_parquet(path)
        # parquet scritti con fuso (es. run di ClickHouse esterno in modalità s3)
        # → TIMESTAMP naive UTC, lo standard di tutti gli engine (sessione in UTC)
        tz_cols = {c for c, t in zip(rel.columns, rel.types) if "WITH TIME ZONE" in str(t).upper()}
        if tz_cols:
            proj = ", ".join(f"CAST({_qi(c)} AS TIMESTAMP) AS {_qi(c)}" if c in tz_cols else _qi(c) for c in rel.columns)
            rel = rel.project(proj)
        return rel

    def apply(self, rel, ops, index_offset: int = 0, top: bool = False):
        """Applica una catena di operazioni (Operation o dict) a una relazione.
        `index_offset` allinea l'indice riportato negli errori a quello assoluto
        nella catena (le op iniziali possono venire dalla cache). `top`: the
        preview's own chain, not the right side of a join or a foreach body."""
        for i, op in enumerate(ops):
            op_type = op.type if isinstance(op, Operation) else op["type"]
            params = op.params if isinstance(op, Operation) else (op.get("params") or {})
            fn = get_duck_operation(op_type)
            # A preview may stop at the first rows only of what nobody else reads: the
            # last operation of its chain. An sql node cut short in the middle handed a
            # sample to the group_by after it, and the preview showed wrong totals.
            self.limit_output = self.preview_limit if top and i == len(ops) - 1 else None
            try:
                rel = fn(rel, params, self.uid("q"), self)
            except EngineError:
                raise
            except Exception as e:  # errore SQL/binder su questa operazione
                raise OperationError(op_type, i + index_offset, str(e)) from e
        return rel

    def build_right(self, ref: dict):
        """Lato destro di un join/union: sotto-flow {source, operations} o
        sorgente semplice {bucket, key}."""
        if "source" in ref:
            rel = self.scan(DataSource(**ref["source"]))
            return self.apply(rel, ref.get("operations") or [])
        return self.scan(DataSource(**ref))

    def register(self, rel) -> str:
        """Registra una relazione come vista con nome univoco (per il join/union)."""
        name = self.uid("reg")
        self.con.register(name, rel)
        return name


from contextlib import contextmanager

from app.engine.stepcache_defer import DeferredStepCache


class DuckDBEngine(DeferredStepCache, Engine):
    # tag che namespacea la step-cache: engine diversi non condividono i blob
    engine_name = "duckdb"

    def __init__(self, storage=None, cache=None):
        if storage is None:
            from app.utils import get_storage_service

            storage = get_storage_service()
        self.storage = storage
        self.cache = cache or StepCache(storage)
        from app.utils import StorageService

        # only the real object storage can be read in place (tests use an in-memory one)
        self.direct = isinstance(storage, StorageService) and storage_direct.duckdb_available()

    def _connection(self, tmp: list[str]) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(":memory:")  # spill su disco automatico (out-of-core)
        # Riversamenti su disco nella directory temporanea di SISTEMA (TMPDIR, di
        # default /tmp). Il default di DuckDB per i DB in memoria è `.tmp` nella
        # directory di lavoro: con l'immagine di produzione a filesystem in sola
        # lettura fallirebbe, e in sviluppo scriverebbe dentro i sorgenti montati.
        # One directory per connection: DuckDB names its spill files the same way in
        # every instance, so two connections spilling into a shared directory read
        # each other's blocks (wrong sums, IO errors). It goes with the request's
        # temporary files.
        spill_dir = tempfile.mkdtemp(prefix="duckdb-")
        tmp.append(spill_dir)
        con.execute(f"SET temp_directory = '{spill_dir}'")
        duckdb_budget.apply(con)  # this child's share of memory and CPUs, not the container's
        if self.direct:
            storage_direct.duckdb_enable(con)
        try:
            con.execute("SET TimeZone = 'UTC'")  # i TIMESTAMPTZ letti diventano naive UTC
        except Exception:  # senza estensione ICU DuckDB è comunque in UTC
            pass
        return con

    def _source_id(self, source: DataSource) -> str:
        return f"{self.engine_name}:{source.bucket}/{source.key}"

    # ── Cache incrementale (mirror di PolarsEngine) ───────────────────────
    def _rel_from_cache(self, ctx: "DuckContext", source, operations, hashes, record=False, use_cache=True):
        """Costruisce la relazione partendo dall'antenato in cache più vicino e
        applicando solo le operazioni rimanenti. `use_cache=False` ignora la cache
        (parte dalla sorgente): il Viewer non sporca la cache con query ad-hoc."""
        start = self.cache.nearest(hashes) if use_cache else 0  # step già in cache
        if record and operations and use_cache:
            (self.cache.record_hit if start > 0 else self.cache.record_miss)()
        if start == 0:
            rel = ctx.scan(source)
        else:
            self.cache.touch(hashes[start - 1])  # segna l'uso → posticipa il TTL
            cached = DataSource(bucket=self.cache.bucket, key=self.cache.object_key(hashes[start - 1]))
            rel = ctx.scan(cached)
            logger.info("cache hit: riparto dallo step %d/%d", start, len(operations))
        return ctx.apply(rel, operations[start:], index_offset=start, top=True)

    def _materialize(self, ctx: "DuckContext", source, operations) -> None:
        """Materializza in cache l'output di `operations` (se non già presente),
        così le prossime esecuzioni possono ripartire da lì."""
        if not operations:
            return
        hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in operations])
        final = hashes[-1]
        if self.cache.has(final):
            return
        rel = self._rel_from_cache(ctx, source, operations, hashes)
        if self._too_big(ctx, rel, final):
            return
        path = ctx.tempfile()
        try:
            rel.write_parquet(path)
        except EngineError:
            raise
        except Exception as e:
            raise EngineError(f"Errore durante l'esecuzione del flow: {e}") from e
        self.storage.upload_file(path, self.cache.bucket, self.cache.object_key(final))
        self.cache.mark(final)
        logger.info("materializzato step %d in cache", len(operations))

    @staticmethod
    def _cleanup(tmp: list[str]) -> None:
        for p in tmp:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
                continue
            try:
                os.remove(p)
            except OSError:
                pass

    @contextmanager
    def _materialize_session(self, source: DataSource):
        tmp: list[str] = []
        con = self._connection(tmp)
        try:
            yield DuckContext(con, self.storage, tmp, direct=self.direct)
        finally:
            con.close()
            self._cleanup(tmp)

    def _bounded_rows(self, ctx, rel, n: int) -> int:
        return int(rel.limit(n + 1).aggregate("count(*)").fetchone()[0])

    # ── Preview (sincrona, solo N righe in RAM) ───────────────────────────
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
        con = self._connection(tmp)
        try:
            ctx = DuckContext(con, self.storage, tmp, preview_limit=limit + 1, direct=self.direct)
            # materializza il PARENT: iterando sui parametri dell'ultimo nodo, le
            # anteprime successive ripartono dalla sua cache (Viewer: use_cache=False)
            if use_cache:
                self._defer_materialization(source, ops[:-1])
            cstate, ccap = self._cache_state(source, ops[:-1], use_cache)  # DOPO la risposta, in un task a parte
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            rel = self._rel_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
            try:
                df = rel.limit(limit + 1).pl()  # solo LIMIT+1 righe materializzate
            except EngineError:
                raise
            except Exception as e:
                raise EngineError(f"Errore durante l'esecuzione del flow: {e}") from e
            truncated = df.height > limit
            if truncated:
                df = df.head(limit)
            return PreviewResult(
                columns=_columns_of(df.schema),
                rows=df.to_dicts(),
                row_count=df.height,
                truncated=truncated,
                cache_state=cstate, cache_cap_rows=ccap,
            )
        finally:
            con.close()
            self._cleanup(tmp)

    # ── Run (materializza l'output in streaming su disco) ─────────────────
    def run(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        destination: DataSource,
        use_cache: bool = True,
    ) -> RunResult:
        ops = _coerce_ops(operations)
        tmp: list[str] = []
        con = self._connection(tmp)
        try:
            ctx = DuckContext(con, self.storage, tmp, direct=self.direct)
            hashes = plan_hashes(self._source_id(source), [op.model_dump() for op in ops])
            rel = self._rel_from_cache(ctx, source, ops, hashes, record=True, use_cache=use_cache)
            out_path = ctx.tempfile()
            try:
                rel.write_parquet(out_path)  # streaming, spill su disco
            except EngineError:
                raise
            except Exception as e:
                raise EngineError(f"Errore durante l'esecuzione del flow: {e}") from e

            self.storage.upload_file(out_path, destination.bucket, destination.key)
            written = pl.scan_parquet(out_path)
            rows_written = written.select(pl.len()).collect(engine="streaming").item()
            # l'output finale è anche il risultato dell'ultimo step: mettilo in
            # cache così ri-run e anteprime del nodo foglia sono immediati (nel tetto)
            if use_cache and ops and self._cache_output_allowed(int(rows_written)) and not self.cache.has(hashes[-1]):
                self.storage.upload_file(out_path, self.cache.bucket, self.cache.object_key(hashes[-1]))
                self.cache.mark(hashes[-1])
            return RunResult(
                destination=destination,
                rows_written=int(rows_written),
                columns=_columns_of(written.collect_schema()),
            )
        finally:
            con.close()
            self._cleanup(tmp)
