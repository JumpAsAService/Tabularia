import time
import logging
from typing import Any
from app.tasks.celery_app import celery_app
from app.utils import get_storage_service
from app.core.config import get_settings
from app.engine import DataSource, get_engine
from app.ingest import FileFormat, IngestOptions, get_ingest_service

import time as _time_mod

logger = logging.getLogger(__name__)


class _Avanzamento:
    """L'avanzamento di un import per chi lo guarda: fase e conteggi come meta
    dello stato STARTED nel backend dei risultati, che l'engine restituisce in
    `/tasks/{id}` e il gateway passa al browser. Al più ogni INTERVALLO secondi
    nella stessa fase, sempre al cambio di fase. Fuori da un worker (test,
    chiamata diretta) non fa niente; un guasto qui non deve mai fermare l'import."""

    INTERVALLO = 2.0

    def __init__(self) -> None:
        from celery import current_task

        self._task = current_task if current_task and current_task.request.id else None
        self._fase: str | None = None
        self._quando = 0.0

    def __call__(self, fase: str, **conti: int) -> None:
        if self._task is None:
            return
        ora = _time_mod.monotonic()
        if fase == self._fase and ora - self._quando < self.INTERVALLO:
            return
        self._fase, self._quando = fase, ora
        try:
            self._task.update_state(state="STARTED", meta={"phase": fase, **conti})
        except Exception as e:  # noqa: BLE001
            logger.warning("avanzamento non registrato: %s", e)


@celery_app.task(name="app.tasks.jobs.preview_task")
def preview_task(
    bucket: str,
    input_key: str,
    operations: list[dict[str, Any]],
    limit: int = 100,
    engine: str | None = None,
    no_cache: bool = False,
    sort_keys: list[str] | None = None,
    principal: str | None = None,
) -> dict:
    """Anteprima interattiva del flow (schema + prime N righe), eseguita su un
    worker dedicato invece che nel processo API: così l'engine (Polars/DuckDB)
    NON gira più in-process nel backend e la memoria delle preview è soggetta
    allo stesso riciclo/tetto dei run.

    Ritorna un dict TAGGATO (non solleva per gli errori attesi): col serializer
    JSON le eccezioni non si propagano col loro tipo, quindi la route mappa lei
    lo status HTTP leggendo `error`. Gli errori inattesi si propagano → 500."""
    # import locale: le eccezioni engine servono solo qui
    from app.engine.exceptions import (
        EngineError,
        OperationError,
        SourceNotFoundError,
        UnknownOperationError,
    )

    import time as _time

    _t0 = _time.perf_counter()
    # Le query di QUESTA preview portano l'id del task come etichetta: se la
    # preview viene superata da una piu' recente, l'engine le uccide sul server.
    from celery import current_task
    from app.engine.principal import query_principal
    from app.engine.query_tag import query_tag

    _tid = getattr(getattr(current_task, "request", None), "id", None)

    def _stats(outcome: str) -> None:
        # istogrammi + classifica delle più lente su Valkey (vedi preview_stats):
        # nessuna tabella, nessuna lista in memoria
        from app.observability import preview_stats

        fasi, fonte = preview_stats.take_phases()
        preview_stats.record(
            engine=engine or "default", outcome=outcome, total_ms=(_time.perf_counter() - _t0) * 1000,
            phases=fasi, dataset=input_key, ops=len(operations), source=fonte, cache=not no_cache,
        )

    try:
        engine_impl = get_engine(engine)
        with query_tag(f"tab-prev:{_tid}" if _tid else None), query_principal(principal):
            result = engine_impl.preview(
                source=DataSource(bucket=bucket, key=input_key),
                operations=operations,
                limit=limit,
                use_cache=not no_cache,
                sort_keys=sort_keys,
            )
        _engine_ms = (_time.perf_counter() - _t0) * 1000
        payload = {"ok": True, "result": result.model_dump()}
        _stats("ok")
        # step-cache differita (BigQuery): i passi da materializzare vanno a un
        # task separato, cosi' QUESTA risposta non aspetta i CTAS
        for src, pending_ops in getattr(engine_impl, "take_pending", lambda: [])():
            try:
                materialize_step_task.apply_async(args=[engine, src.bucket, src.key, pending_ops], queue="preview",
                                                  priority=CACHE_COPY_PRIORITY)
            except Exception as e:  # noqa: BLE001 — senza broker si perde solo la cache
                logger.warning("materializzazione differita non accodata: %s", e)
        # Il tempo DENTRO il worker, per ogni engine. Confrontalo con quello che
        # misura il chiamante: la differenza e' attesa in coda + trasporto del
        # risultato, non lavoro. Se `serializz` e' alto, pesa il payload (righe x
        # colonne), non la query.
        _ser = _time.perf_counter()
        _n_rows, _n_cols = result.row_count, len(result.columns)
        logger.info(
            "preview_task %.0f ms | engine=%s(%.0fms) serializz=%.0fms | righe=%d colonne=%d limit=%d ops=%d cache=%s chiavi=%d",
            (_time.perf_counter() - _t0) * 1000,
            engine or "default", _engine_ms,
            (_time.perf_counter() - _ser) * 1000,
            _n_rows, _n_cols, limit, len(operations),
            "off" if no_cache else "on",
            len(sort_keys or []),
        )
        return payload
    except Exception as e:
        # Superata da una preview piu' recente: NON e' un errore. Va riconosciuta
        # prima di ogni altro ramo, perche' arriva travestita — EngineError se il
        # task parlava con ClickHouse, HTTPClientError di botocore se parlava con
        # S3 — e come "unexpected" riempiva i log di traceback per lavoro che
        # nessuno aspettava piu'.
        from app.engine.query_tag import was_interrupted

        _stats("superseded" if was_interrupted(e) else "error")
        if was_interrupted(e):
            logger.info("preview_task superata da una piu' recente (%.0f ms)", (_time.perf_counter() - _t0) * 1000)
            return {"ok": False, "error": "superseded", "detail": "Preview superseded by a more recent request"}
        if isinstance(e, SourceNotFoundError):
            return {"ok": False, "error": "not_found", "detail": str(e)}
        if isinstance(e, (UnknownOperationError, OperationError)):
            return {"ok": False, "error": "unprocessable", "detail": str(e)}
        if isinstance(e, EngineError):
            return {"ok": False, "error": "bad_request", "detail": str(e)}
        raise


# A copy of a step into the cache is an optimisation nobody waits for, but it ran on
# the preview worker's slots in arrival order: during the review of 2026-10-14 copies
# took 39% of that worker's time, and clicks queued behind them. Now a copy goes to
# the preview queue at the lowest priority (9 on Redis), only one runs at a time in
# the whole installation, and a copy that finds interactive previews waiting gives up
# (the next preview of that node asks for it again).
CACHE_COPY_PRIORITY = 9
_COPY_LOCK = "dataprep:stepcache:copying"
_COPY_LOCK_SECONDS = 600  # a copy killed halfway does not hold the turn for ever
# compare-and-delete: the turn is given back only by the copy that holds it
_RELEASE = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"


def _broker():
    import redis

    return redis.Redis.from_url(celery_app.conf.broker_url, socket_timeout=2, socket_connect_timeout=2)


def _copy_turn(broker, token: str) -> str | None:
    """None when the copy may run now; otherwise why it gives up. Without the broker
    (tests, Valkey down) it runs, as it did before."""
    from app.tasks.celery_app import broker_lists

    try:
        if broker.llen(broker_lists("preview")[0]):
            return "previews are waiting"
        if not broker.set(_COPY_LOCK, token, nx=True, ex=_COPY_LOCK_SECONDS):
            return "another copy is running"
    except Exception:  # noqa: BLE001 — the copy is best-effort, its scheduling too
        return None
    return None


@celery_app.task(name="app.tasks.jobs.materialize_step_task", ignore_result=True, bind=True)
def materialize_step_task(self, engine: str, bucket: str, key: str, operations: list[dict]) -> dict:
    """Materializza in cache (BigQuery: tabella nativa) l'output di una catena
    di operazioni, FUORI dalla preview che l'ha chiesta. Best-effort: un errore
    qui costa solo la cache, mai la preview."""
    import uuid

    t0 = _time_mod.perf_counter()
    broker, token = _broker(), self.request.id or uuid.uuid4().hex
    reason = _copy_turn(broker, token)
    if reason:
        logger.info("materialize_step_task %s skipped: %s", engine, reason)
        return {"ok": False, "skipped": reason}
    try:
        impl = get_engine(engine)
        fn = getattr(impl, "materialize", None)
        if fn is None:
            return {"ok": False, "skipped": True}
        written = fn(DataSource(bucket=bucket, key=key), operations)
        logger.info("materialize_step_task %s ops=%d scritto=%s %.0f ms", engine, len(operations), written,
                    (_time_mod.perf_counter() - t0) * 1000)
        return {"ok": True, "written": bool(written)}
    except Exception as e:  # noqa: BLE001
        logger.warning("materialize_step_task %s fallito: %s", engine, e)
        return {"ok": False, "error": str(e)[:300]}
    finally:
        try:
            broker.eval(_RELEASE, 1, _COPY_LOCK, token)
        except Exception:  # noqa: BLE001 — the lock expires on its own
            pass


@celery_app.task(name="app.tasks.jobs.transform_data_task")
def transform_data_task(
    bucket: str,
    input_key: str,
    operations: list[dict[str, Any]],
    output_key: str,
    destination: dict[str, Any] | None = None,
    mirror: dict[str, Any] | None = None,
    email: dict[str, Any] | None = None,
    engine: str | None = None,
    contract: dict[str, Any] | None = None,
) -> dict:
    """
    Esegue un flow di trasformazione (run completo) su un parquet dello storage.

    `contract`: il data contract della datasource su cui l'output verrà
    pubblicato. Lo si valuta sul parquet appena scritto e il referto torna col
    risultato: è il gateway a decidere se pubblicare (vedi check_before_publish).

    Legge `input_key`, applica la catena di `operations` con l'engine Polars in
    streaming e scrive il risultato in `output_key`. Se `destination` è
    presente ({"type": "database"|"s3", "connection": …, "target": …}, secret
    Fernet-cifrata come per l'ingest), il parquet appena scritto viene POI
    riversato sulla destinazione: tabella di database oppure oggetto/dataset
    hive-partizionato su S3. Il parquet resta comunque su storage
    (cronologia/ispezione).

    Args:
        bucket: Nome del bucket S3
        input_key: Chiave del parquet di input
        operations: Lista di operazioni (IR: {"type": ..., "params": ...})
        output_key: Chiave del parquet di output
        destination: Destinazione opzionale (nodo Output): type database | s3 | sftp

    Returns:
        Metadati del run (righe scritte, colonne di output, esito destinazione).
    """
    logger.info(f"🚀 Starting transform_data_task: {input_key} → {output_key}")
    logger.info(f"📋 Operations: {operations}")

    engine_impl = get_engine(engine)
    result = engine_impl.run(
        source=DataSource(bucket=bucket, key=input_key),
        operations=operations,
        destination=DataSource(bucket=bucket, key=output_key),
    )

    out: dict[str, Any] = {
        "status": "success",
        "bucket": bucket,
        "input_key": input_key,
        "output_key": output_key,
        "operations_applied": len(operations),
        "rows_written": result.rows_written,
        "columns": [c.model_dump() for c in result.columns],
        "processed_at": time.time(),
    }

    if contract:
        from app.contracts.service import check_before_publish

        out["contract"] = check_before_publish(bucket, output_key, contract)
        if out["contract"]["outcome"] == "failed":
            # Una regola bloccante è violata: il gateway non pubblicherà questi
            # dati. Niente di ciò che segue deve portarli fuori — la copia su S3
            # e l'email consegnerebbero a qualcuno proprio ciò che il contratto
            # ha rifiutato.
            logger.warning("contratto violato su %s: output non consegnato", output_key)
            return out

    if destination:
        dest_type = destination.get("type", "database")
        if dest_type == "s3":
            from app.ingest.s3_destination import (
                S3ConnectionSpec,
                S3DestinationSpec,
                write_output_to_s3,
            )

            conn = S3ConnectionSpec(**destination["connection"])
            dest = S3DestinationSpec(**destination["target"])
            logger.info(f"📤 Writing output to s3 {conn.endpoint_url or 'aws'} key {dest.key}")
            out["destination"] = write_output_to_s3(
                conn=conn, dest=dest, bucket=bucket, key=output_key
            )
        elif dest_type == "sftp":
            from app.ingest import sftp as sftp_dest

            s_conn = sftp_dest.SftpConnectionSpec(**destination["connection"])
            s_dest = sftp_dest.SftpDestinationSpec(**destination["target"])
            logger.info(f"📤 Writing output to sftp {s_conn.address} folder {s_dest.folder or '~'} name {s_dest.file_name}")
            out["destination"] = sftp_dest.write_output_to_sftp(
                conn=s_conn, dest=s_dest, bucket=bucket, key=output_key
            )
        else:
            from app.ingest.db_destination import (
                DbDestinationSpec,
                write_parquet_to_db,
            )
            from app.ingest.db_source import DbConnectionSpec

            db_conn = DbConnectionSpec(**destination["connection"])
            db_dest = DbDestinationSpec(**destination["target"])
            logger.info(f"📤 Writing output to {db_conn.db_type}@{db_conn.host} table {db_dest.table}")
            out["destination"] = write_parquet_to_db(
                conn=db_conn, dest=db_dest, bucket=bucket, key=output_key
            )

    # Copia su S3 esterno, IN AGGIUNTA all'output: best-effort per scelta.
    # Arriva DOPO la destinazione perché il risultato primario viene prima; un
    # suo fallimento (credenziali scadute, bucket pieno, rete) non deve annullare
    # un run riuscito né impedire la pubblicazione della datasource, che il
    # gateway fa leggendo questo stesso risultato. L'esito torna in `out` e il
    # gateway lo registra sul run, così l'errore resta visibile in cronologia.
    if mirror:
        from app.ingest.s3_destination import (
            S3ConnectionSpec,
            S3DestinationSpec,
            write_output_to_s3,
        )

        m_target = dict(mirror.get("target") or {})
        try:
            m_conn = S3ConnectionSpec(**mirror["connection"])
            m_dest = S3DestinationSpec(**m_target)
            logger.info(f"📎 Copia su s3 {m_conn.endpoint_url or 'aws'} key {m_dest.key}")
            out["mirror"] = {
                "ok": True,
                **write_output_to_s3(conn=m_conn, dest=m_dest, bucket=bucket, key=output_key),
            }
        except Exception as e:
            logger.warning(f"⚠️ copia su S3 fallita (il run resta valido): {type(e).__name__}: {e}")
            out["mirror"] = {
                "ok": False,
                "bucket": m_target.get("bucket", ""),
                "key": m_target.get("key", ""),
                "error": f"{type(e).__name__}: {e}"[:500],
            }

    # Invio email dell'output come allegato (csv/xlsx). Arriva per ULTIMO: manda
    # ciò che è stato prodotto, quindi tutto il resto dev'essere già finito.
    #
    # A differenza della copia su S3 NON è best-effort per scelta dell'utente: un
    # report che non parte è il risultato che non c'è, e un run verde mentre
    # nessuno riceve nulla è peggio di un run rosso. `stop_on_failure` (default
    # acceso) decide se l'errore faccia fallire il run; l'esito torna comunque in
    # `out`, e il gateway lo registra sulla riga del run.
    if email:
        from app.ingest.email_destination import (
            EmailSpec,
            SmtpConnectionSpec,
            send_output_email,
        )

        spec_raw = dict(email.get("target") or {})
        ferma = bool(email.get("stop_on_failure", True))
        try:
            e_conn = SmtpConnectionSpec(**email["connection"])
            e_spec = EmailSpec(**spec_raw)
            logger.info("📧 Invio email via %s a %d destinatari", e_conn.host, len(e_spec.to))
            out["email"] = send_output_email(conn=e_conn, spec=e_spec, bucket=bucket, key=output_key)
        except Exception as e:
            out["email"] = {
                "ok": False,
                "recipients": len(spec_raw.get("to") or []),
                "attachment": spec_raw.get("attachment_name", ""),
                "error": f"{type(e).__name__}: {e}"[:500],
            }
            if ferma:
                logger.error("❌ invio email fallito, il run fallisce: %s", e)
                raise
            logger.warning("⚠️ invio email fallito (il run resta valido): %s", e)

    logger.info(
        f"✅ Completed transform_data_task: {output_key} "
        f"({result.rows_written} righe, {len(result.columns)} colonne)"
    )
    return out


@celery_app.task(name="app.tasks.jobs.ingest_sharepoint_task")
def ingest_sharepoint_task(
    connection: dict[str, Any],
    source: dict[str, Any],
    bucket: str,
    output_key: str,
    contract: dict[str, Any] | None = None,
) -> dict:
    """Scarica da SharePoint i file Excel che corrispondono al percorso (anche con
    glob), li impila e scrive il parquet. Stessa forma di `ingest_database_task`:
    il gateway tratta i due ingest allo stesso modo (run, snapshot swap, refresh).
    Il secret dell'app arriva cifrato e si decifra solo per chiedere il token."""
    from app.ingest.sharepoint_source import (
        SharePointConnectionSpec,
        SharePointSourceSpec,
        ingest_sharepoint_to_parquet,
    )

    conn = SharePointConnectionSpec(**connection)
    src = SharePointSourceSpec(**source)
    logger.info(f"🚀 Starting ingest_sharepoint_task: {conn.site_url} :: {src.path} [{src.sheet}] → {output_key}")
    avanza = _Avanzamento()
    result = ingest_sharepoint_to_parquet(conn, src, bucket=bucket, key=output_key, storage=get_storage_service(),
                                          progress=avanza)
    logger.info(
        f"✅ Completed ingest_sharepoint_task: {output_key} "
        f"({result['rows_written']} righe da {len(result['files'])} file)"
    )
    if contract:
        from app.contracts.service import check_before_publish

        avanza("checking", rows=result["rows_written"])

        result["contract"] = check_before_publish(bucket, output_key, contract)
    return result


@celery_app.task(name="app.tasks.jobs.ingest_sftp_task")
def ingest_sftp_task(
    connection: dict[str, Any],
    source: dict[str, Any],
    bucket: str,
    output_key: str,
    contract: dict[str, Any] | None = None,
) -> dict:
    """Reads the files of an SFTP folder that match the glob, stacks them and
    writes the parquet: the same shape as the SharePoint and database ingests, so
    the gateway treats it the same way (run, snapshot swap, refresh). The host key
    is checked before any credential leaves; secrets arrive encrypted."""
    from app.ingest.sftp import SftpConnectionSpec, SftpSourceSpec, ingest_sftp_to_parquet

    conn = SftpConnectionSpec(**connection)
    src = SftpSourceSpec(**source)
    logger.info(f"🚀 Starting ingest_sftp_task: {conn.address} :: {src.folder or '~'}/{src.pattern} → {output_key}")
    avanza = _Avanzamento()
    result = ingest_sftp_to_parquet(conn, src, bucket=bucket, key=output_key, storage=get_storage_service(),
                                    progress=avanza)
    logger.info(f"✅ Completed ingest_sftp_task: {output_key} ({result['rows_written']} rows from {len(result['files'])} files)")
    if contract:
        from app.contracts.service import check_before_publish

        avanza("checking", rows=result["rows_written"])
        result["contract"] = check_before_publish(bucket, output_key, contract)
    return result


@celery_app.task(name="app.tasks.jobs.ingest_api_task")
def ingest_api_task(
    connection: dict[str, Any],
    source: dict[str, Any],
    bucket: str,
    output_key: str,
    last_refresh: str | None = None,
    previous: dict[str, Any] | None = None,
    contract: dict[str, Any] | None = None,
) -> dict:
    """Calls an HTTP API (every combination of the variables, every page) and
    writes the parquet — in append mode merged with the previous snapshot: the
    same shape as the other ingests, so the gateway treats it the same way (run,
    snapshot swap, refresh). The secret arrives encrypted."""
    from app.ingest.http_api import ApiConnectionSpec, ApiSourceSpec, ingest_api_to_parquet

    conn = ApiConnectionSpec(**connection)
    src = ApiSourceSpec(**source)
    logger.info(f"🚀 Starting ingest_api_task: {src.method} {conn.base_url} {src.path} ({src.mode}) → {output_key}")
    avanza = _Avanzamento()
    result = ingest_api_to_parquet(conn, src, bucket=bucket, key=output_key, storage=get_storage_service(),
                                   progress=avanza, last_refresh=last_refresh, previous=previous)
    logger.info(f"✅ Completed ingest_api_task: {output_key} ({result['rows_written']} rows, {result['calls']} calls, "
                f"{result['pages']} pages)")
    if contract:
        from app.contracts.service import check_before_publish

        avanza("checking", rows=result["rows_written"])
        result["contract"] = check_before_publish(bucket, output_key, contract)
    return result


@celery_app.task(name="app.tasks.jobs.ingest_database_task")
def ingest_database_task(
    connection: dict[str, Any],
    source: dict[str, Any],
    bucket: str,
    output_key: str,
    contract: dict[str, Any] | None = None,
) -> dict:
    """
    Ingest da database: esegue la sorgente (tabella o SQL) e scrive il risultato
    in parquet su storage, in streaming (batch Arrow → ParquetWriter).

    `contract`: il data contract della datasource che si sta aggiornando,
    valutato sul parquet appena scritto (vedi transform_data_task).

    `connection.password_encrypted` è cifrata (Fernet, chiave condivisa col
    gateway): la password in chiaro non transita mai nel broker.
    """
    from app.ingest.db_source import DbConnectionSpec, DbSourceSpec, ingest_db_to_parquet

    conn = DbConnectionSpec(**connection)
    logger.info(f"🚀 Starting ingest_database_task: {conn.db_type}@{conn.host} → {output_key}")

    avanza = _Avanzamento()
    result = ingest_db_to_parquet(
        conn=conn,
        source=DbSourceSpec(**source),
        bucket=bucket,
        key=output_key,
        progress=avanza,
    )

    logger.info(
        f"✅ Completed ingest_database_task: {output_key} "
        f"({result['rows_written']} righe, {len(result['columns'])} colonne)"
    )
    out = {
        "status": "success",
        "bucket": bucket,
        "output_key": output_key,
        "rows_written": result["rows_written"],
        "columns": result["columns"],
        "processed_at": time.time(),
    }
    if contract:
        from app.contracts.service import check_before_publish

        avanza("checking", rows=result["rows_written"])
        out["contract"] = check_before_publish(bucket, output_key, contract)
    return out


@celery_app.task(name="app.tasks.jobs.convert_to_parquet_task")
def convert_to_parquet_task(
    dataset_id: str,
    raw_key: str,
    parquet_key: str,
    fmt: str,
    options: dict[str, Any],
) -> dict:
    """
    Conversione async di un file grande (raw già su storage) in parquet.

    Usata dall'ingest oltre la soglia dei 50MB; riusa la stessa logica di
    conversione del path sincrono. Ritorna i metadati del dataset (schema, righe).
    """
    logger.info(f"🚀 Starting convert_to_parquet_task: {raw_key} → {parquet_key}")

    service = get_ingest_service()
    info = service.convert_stored(
        dataset_id=dataset_id,
        raw_key=raw_key,
        parquet_key=parquet_key,
        fmt=FileFormat(fmt),
        options=IngestOptions(**options),
    )

    logger.info(f"✅ Completed convert_to_parquet_task: {parquet_key} ({info.rows} righe)")
    return {"status": "success", **info.model_dump()}


# prefissi di storage di cui tracciamo la dimensione (per le metriche)
STORAGE_PREFIXES = ["raw/", "datasets/", "cache/", "out/"]
STORAGE_BYTES_HASH = "dataprep:metrics:storage_bytes"
STORAGE_OBJECTS_HASH = "dataprep:metrics:storage_objects"


@celery_app.task(name="app.tasks.jobs.storage_stats_task")
def storage_stats_task() -> dict:
    """
    Campiona la dimensione dei prefissi di storage (cache/, datasets/, ...) e la
    scrive su Valkey, da dove il collector delle metriche la espone. Schedulato
    da Celery beat (intervallo: METRICS__STORAGE_STATS_INTERVAL_SECONDS).
    """
    settings = get_settings()
    storage = get_storage_service()
    r = get_engine().cache.redis  # riusa il client Valkey (decode_responses=True)

    result: dict[str, dict[str, int]] = {}
    for prefix in STORAGE_PREFIXES:
        total_bytes, count = storage.prefix_stats(settings.storage.bucket, prefix)
        name = prefix.rstrip("/")
        r.hset(STORAGE_BYTES_HASH, name, total_bytes)
        r.hset(STORAGE_OBJECTS_HASH, name, count)
        result[name] = {"bytes": total_bytes, "objects": count}
    return result


@celery_app.task(name="app.tasks.jobs.evict_cache_task")
def evict_cache_task() -> dict:
    """
    Eviction periodica della step cache: rimuove le voci non accedute da più di
    `cache.ttl_seconds`. Schedulata da Celery beat (vedi celery_app.py).
    """
    settings = get_settings()
    removed = get_engine().cache.evict_expired(settings.cache.ttl_seconds)
    # la cache nativa di BigQuery ha un indice suo (tabelle, non parquet): le
    # tabelle scadono da sole sul servizio, qui si allinea l'indice
    from app.engine import _BIGQUERY_AVAILABLE

    if _BIGQUERY_AVAILABLE:
        try:
            removed += get_engine("bigquery").cache.evict_expired(settings.cache.ttl_seconds)
        except Exception as e:  # noqa: BLE001 — lo sweep non deve fermarsi per un motore opzionale
            logger.warning("sweep della cache BigQuery non riuscito: %s", e)
    return {"removed": removed, "ttl_seconds": settings.cache.ttl_seconds}


@celery_app.task(name="app.tasks.jobs.evict_matview_task")
def evict_matview_task() -> dict:
    """Drop periodico delle tabelle materializzate per il viewer sul ClickHouse
    esterno (scadute per inutilizzo + orfane). Schedulato da Celery beat; si
    auto-salta solo se l'engine non c'è o ClickHouse non è configurato — NON se
    la materializzazione è spenta: i residui di quando era accesa (o di una
    build uccisa a metà) vanno comunque rimossi."""
    from app.engine import _CLICKHOUSE_AVAILABLE

    settings = get_settings()
    if not _CLICKHOUSE_AVAILABLE or not settings.clickhouse_external.matview_sweep_enabled:
        return {"removed": 0, "skipped": True}
    removed = get_engine("clickhouse").evict_matviews()
    return {"removed": removed, "ttl_seconds": settings.clickhouse_external.materialize_ttl_seconds}

# Where a worker leaves an export file until the API sends it (and deletes it).
EXPORT_PREFIX = "exports/"
# A file nobody came to take (the tab was closed while it was being written) is
# swept away after this long; one that is downloaded goes as soon as it is sent.
EXPORT_KEEP_SECONDS = 3600


@celery_app.task(name="app.tasks.jobs.export_task", bind=True)
def export_task(
    self,
    bucket: str,
    input_key: str,
    operations: list[dict],
    format: str = "csv",
    limit: int | None = None,
    filename: str | None = None,
    engine: str | None = None,
) -> dict:
    """Writes the result of a chain (even up to an intermediate node) as a csv or
    xlsx file and leaves it on the storage, under `exports/`, for the API to hand
    to whoever asked. On a worker, not in the API: an Excel file is built whole in
    memory, a million rows of it, and in the API process that put every other
    request at risk.

    With an engine other than Polars the chain (which may use that engine's SQL
    dialect) is computed by THAT engine into a temporary parquet snapshot, and
    Polars writes the file from it.

    Returns a tagged dict like `preview_task`: the expected errors (missing source,
    bad operation, over the row cap) are answers, not exceptions."""
    import os
    import tempfile
    import uuid

    from celery.exceptions import SoftTimeLimitExceeded

    from app.engine.exceptions import EngineError, OperationError, SourceNotFoundError, UnknownOperationError

    storage = get_storage_service()
    src = DataSource(bucket=bucket, key=input_key)
    fd, path = tempfile.mkstemp(suffix=f".{format}")
    os.close(fd)
    max_rows = get_settings().export.max_rows or None
    snapshot: DataSource | None = None
    try:
        if (engine or "polars").lower() != "polars" and operations:
            snapshot = DataSource(bucket=bucket, key=f"tmp/export_{uuid.uuid4().hex}.parquet")
            get_engine(engine.lower()).run(source=src, operations=operations, destination=snapshot, use_cache=False)
            get_engine("polars").export(source=snapshot, operations=[], fmt=format, out_path=path, limit=limit, max_rows=max_rows)
        else:
            get_engine("polars").export(source=src, operations=operations, fmt=format, out_path=path, limit=limit, max_rows=max_rows)
        out_bucket = get_settings().storage.bucket
        key = f"{EXPORT_PREFIX}{self.request.id or uuid.uuid4().hex}.{format}"
        storage.upload_file(path, out_bucket, key)
        return {"ok": True, "bucket": out_bucket, "key": key, "format": format,
                "filename": filename or f"export.{format}", "size": os.path.getsize(path)}
    except SourceNotFoundError as e:
        return {"ok": False, "error": "not_found", "detail": str(e)}
    except (UnknownOperationError, OperationError) as e:
        return {"ok": False, "error": "unprocessable", "detail": str(e)}
    except EngineError as e:
        return {"ok": False, "error": "bad_request", "detail": str(e)}
    except SoftTimeLimitExceeded:
        return {"ok": False, "error": "timeout", "detail": "The export took longer than the time limit of the workers"}
    finally:
        os.unlink(path)
        if snapshot is not None:  # it has fed the file (or failed): either way it goes
            try:
                storage.delete_object(snapshot.bucket, snapshot.key)
            except Exception as e:  # noqa: BLE001 — a leftover snapshot must not fail the export
                logger.warning("export snapshot %s not deleted: %s", snapshot.key, e)


@celery_app.task(name="app.tasks.jobs.sweep_exports_task", ignore_result=True)
def sweep_exports_task() -> dict:
    """Deletes the export files nobody came to take. Each file goes as soon as it
    has been sent; this only catches the ones left behind, an hour after they were
    written. Scheduled by Celery beat (see celery_app.py)."""
    from datetime import datetime, timedelta, timezone

    storage = get_storage_service()
    bucket = get_settings().storage.bucket
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=EXPORT_KEEP_SECONDS)
    stale = [o["Key"] for o in storage.list_objects(bucket, EXPORT_PREFIX) if o["LastModified"] < cutoff]
    if stale:
        storage.delete_objects(bucket, stale)
    return {"removed": len(stale)}
