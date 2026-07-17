import time
import logging
from typing import Any
from app.tasks.celery_app import celery_app
from app.utils import get_storage_service
from app.core.config import get_settings
from app.engine import DataSource, get_engine
from app.ingest import FileFormat, IngestOptions, get_ingest_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.jobs.test_task")
def test_task(message: str, delay: int = 5) -> dict:
    """
    Task di test per verificare che Celery funzioni.
    
    Args:
        message: Messaggio da processare
        delay: Secondi di attesa simulata
    
    Returns:
        Result del task
    """
    logger.info(f"🚀 Starting test_task: {message}")
    
    # Simula lavoro
    for i in range(delay):
        time.sleep(1)
        logger.info(f"⏳ Progress: {i+1}/{delay} seconds")
    
    result = {
        "status": "success",
        "message": message,
        "processed_at": time.time(),
    }
    
    logger.info(f"✅ Completed test_task: {message}")
    return result


@celery_app.task(name="app.tasks.jobs.process_file_task")
def process_file_task(bucket: str, input_key: str, output_key: str) -> dict:
    """
    Task dummy per processare un file dallo storage.
    
    Args:
        bucket: Nome del bucket S3
        input_key: Chiave del file di input
        output_key: Chiave del file di output
    
    Returns:
        Result del processing
    """
    logger.info(f"🚀 Starting process_file_task: {input_key} → {output_key}")
    
    storage = get_storage_service()
    
    # Simula download
    logger.info(f"⬇️ Downloading {input_key} from {bucket}")
    time.sleep(2)
    
    # Simula processing
    logger.info("🔄 Processing data...")
    time.sleep(3)
    
    # Simula upload
    logger.info(f"⬆️ Uploading {output_key} to {bucket}")
    time.sleep(2)
    
    result = {
        "status": "success",
        "bucket": bucket,
        "input_key": input_key,
        "output_key": output_key,
        "processed_at": time.time(),
    }
    
    logger.info(f"✅ Completed process_file_task: {input_key} → {output_key}")
    return result


@celery_app.task(name="app.tasks.jobs.transform_data_task")
def transform_data_task(
    bucket: str,
    input_key: str,
    operations: list[dict[str, Any]],
    output_key: str,
    destination: dict[str, Any] | None = None,
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