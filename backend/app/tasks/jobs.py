import time
import logging
from typing import Any
from app.tasks.celery_app import celery_app
from app.utils import get_storage_service
from app.core.config import get_settings
from app.engine import DataSource, get_engine
from app.ingest import FileFormat, IngestOptions, get_ingest_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.jobs.preview_task")
def preview_task(
    bucket: str,
    input_key: str,
    operations: list[dict[str, Any]],
    limit: int = 100,
    engine: str | None = None,
    no_cache: bool = False,
    sort_keys: list[str] | None = None,
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
    try:
        engine_impl = get_engine(engine)
        result = engine_impl.preview(
            source=DataSource(bucket=bucket, key=input_key),
            operations=operations,
            limit=limit,
            use_cache=not no_cache,
            sort_keys=sort_keys,
        )
        _engine_ms = (_time.perf_counter() - _t0) * 1000
        payload = {"ok": True, "result": result.model_dump()}
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
    except SourceNotFoundError as e:
        return {"ok": False, "error": "not_found", "detail": str(e)}
    except (UnknownOperationError, OperationError) as e:
        return {"ok": False, "error": "unprocessable", "detail": str(e)}
    except EngineError as e:
        return {"ok": False, "error": "bad_request", "detail": str(e)}


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
) -> dict:
    """
    Esegue un flow di trasformazione (run completo) su un parquet dello storage.

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
        destination: Destinazione opzionale (nodo Output)

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


@celery_app.task(name="app.tasks.jobs.ingest_database_task")
def ingest_database_task(
    connection: dict[str, Any],
    source: dict[str, Any],
    bucket: str,
    output_key: str,
) -> dict:
    """
    Ingest da database: esegue la sorgente (tabella o SQL) e scrive il risultato
    in parquet su storage, in streaming (batch Arrow → ParquetWriter).

    `connection.password_encrypted` è cifrata (Fernet, chiave condivisa col
    gateway): la password in chiaro non transita mai nel broker.
    """
    from app.ingest.db_source import DbConnectionSpec, DbSourceSpec, ingest_db_to_parquet

    conn = DbConnectionSpec(**connection)
    logger.info(f"🚀 Starting ingest_database_task: {conn.db_type}@{conn.host} → {output_key}")

    result = ingest_db_to_parquet(
        conn=conn,
        source=DbSourceSpec(**source),
        bucket=bucket,
        key=output_key,
    )

    logger.info(
        f"✅ Completed ingest_database_task: {output_key} "
        f"({result['rows_written']} righe, {len(result['columns'])} colonne)"
    )
    return {
        "status": "success",
        "bucket": bucket,
        "output_key": output_key,
        "rows_written": result["rows_written"],
        "columns": result["columns"],
        "processed_at": time.time(),
    }


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
    return {"removed": removed, "ttl_seconds": settings.cache.ttl_seconds}


@celery_app.task(name="app.tasks.jobs.evict_matview_task")
def evict_matview_task() -> dict:
    """Drop periodico delle tabelle materializzate per il viewer sul ClickHouse
    esterno (scadute per inutilizzo + orfane). Schedulato da Celery beat; si
    auto-salta se l'engine non c'è o la materializzazione è disattivata."""
    from app.engine import _CLICKHOUSE_AVAILABLE

    settings = get_settings()
    if not _CLICKHOUSE_AVAILABLE or not settings.clickhouse_external.materialize_enabled:
        return {"removed": 0, "skipped": True}
    removed = get_engine("clickhouse").evict_matviews()
    return {"removed": removed, "ttl_seconds": settings.clickhouse_external.materialize_ttl_seconds}