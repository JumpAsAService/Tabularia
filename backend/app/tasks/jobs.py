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


class _Progress:
    """An import's progress for whoever watches it: phase and counts as the meta of the
    STARTED state in the result backend, which the engine returns in `/tasks/{id}` and
    the gateway passes to the browser. At most every INTERVAL seconds within a phase,
    always at a change of phase. Outside a worker (tests, a direct call) it does
    nothing; a fault here must never stop the import."""

    INTERVAL = 2.0

    def __init__(self) -> None:
        from celery import current_task

        self._task = current_task if current_task and current_task.request.id else None
        self._phase: str | None = None
        self._when = 0.0
        # the worker and the start stay in the meta: the engine tells a lost task by them
        self._chi: dict = {}
        if self._task is not None:
            from app.tasks.worker_pool import task_started_meta

            self._chi = task_started_meta(self._task.request)

    def __call__(self, phase: str, **counts: int) -> None:
        if self._task is None:
            return
        ora = _time_mod.monotonic()
        if phase == self._phase and ora - self._when < self.INTERVAL:
            return
        self._phase, self._when = phase, ora
        try:
            self._task.update_state(state="STARTED", meta={**self._chi, "phase": phase, **counts})
        except Exception as e:  # noqa: BLE001
            logger.warning("progress not recorded: %s", e)


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
    """The flow's interactive preview (schema + first N rows), run on a dedicated worker
    instead of in the API process: the engine (Polars/DuckDB) no longer runs in the
    backend's process, and the previews' memory is under the same recycling and cap as
    the runs.

    Returns a TAGGED dict (it does not raise for the expected errors): with the JSON
    serializer exceptions do not travel with their type, so the route maps the HTTP
    status itself, reading `error`. Unexpected errors propagate → 500."""
    # a local import: the engine's exceptions are needed only here
    from app.engine.exceptions import (
        EngineError,
        OperationError,
        SourceNotFoundError,
        UnknownOperationError,
    )

    import time as _time

    _t0 = _time.perf_counter()
    # The queries of THIS preview carry the task's id as a label: when the preview is
    # superseded by a newer one, the engine kills them on the server.
    from celery import current_task
    from app.engine.principal import query_principal
    from app.engine.query_tag import query_tag

    _tid = getattr(getattr(current_task, "request", None), "id", None)

    def _stats(outcome: str) -> None:
        # histograms + a ranking of the slowest on Valkey (see preview_stats): no table,
        # no list in memory
        from app.observability import preview_stats

        phases, source_ = preview_stats.take_phases()
        preview_stats.record(
            engine=engine or "default", outcome=outcome, total_ms=(_time.perf_counter() - _t0) * 1000,
            phases=phases, dataset=input_key, ops=len(operations), source=source_, cache=not no_cache,
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
        # deferred step cache (BigQuery): the steps to materialize go to a separate
        # task, so THIS answer does not wait for the CTAS
        for src, pending_ops in getattr(engine_impl, "take_pending", lambda: [])():
            try:
                materialize_step_task.apply_async(args=[engine, src.bucket, src.key, pending_ops], queue="preview",
                                                  priority=CACHE_COPY_PRIORITY)
            except Exception as e:  # noqa: BLE001 — without a broker only the cache is lost
                logger.warning("deferred materialization not queued: %s", e)
        # The time INSIDE the worker, for every engine. Compare it with what the caller
        # measures: the difference is waiting in the queue + carrying the result, not
        # work. When `serializz` is high, the payload weighs (rows x columns), not the
        # query.
        _ser = _time.perf_counter()
        _n_rows, _n_cols = result.row_count, len(result.columns)
        logger.info(
            "preview_task %.0f ms | engine=%s(%.0fms) serialize=%.0fms | rows=%d columns=%d limit=%d ops=%d cache=%s keys=%d",
            (_time.perf_counter() - _t0) * 1000,
            engine or "default", _engine_ms,
            (_time.perf_counter() - _ser) * 1000,
            _n_rows, _n_cols, limit, len(operations),
            "off" if no_cache else "on",
            len(sort_keys or []),
        )
        return payload
    except Exception as e:
        # Superseded by a newer preview: NOT an error. It must be told before any other
        # branch, because it comes in disguise — EngineError when the task talked to
        # ClickHouse, botocore's HTTPClientError when it talked to S3 — and as
        # "unexpected" it filled the logs with tracebacks for work nobody waited for.
        from app.engine.query_tag import was_interrupted

        _stats("superseded" if was_interrupted(e) else "error")
        if was_interrupted(e):
            logger.info("preview_task superseded by a newer one (%.0f ms)", (_time.perf_counter() - _t0) * 1000)
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


@celery_app.task(name="app.tasks.jobs.db_query_task")
def db_query_task(connection: dict[str, Any], sql: str, limit: int = 200) -> dict:
    """A SQL script run on a database connection: the first `limit` rows, tagged as
    preview_task's answer (the route maps `error` to the HTTP status). On the preview
    queue: a person waits for it, as for a preview."""
    from app.engine.base import ColumnInfo, PreviewResult
    from app.engine.query_tag import was_interrupted
    from app.ingest.converters import IngestError
    from app.ingest.db_source import DbConnectionSpec, query_rows

    try:
        df, truncated = query_rows(DbConnectionSpec(**connection), sql, limit)
        result = PreviewResult(columns=[ColumnInfo(name=n, dtype=str(t)) for n, t in df.schema.items()],
                               rows=df.to_dicts(), row_count=df.height, truncated=truncated, cache_state="off")
        return {"ok": True, "result": result.model_dump()}
    except Exception as e:
        if was_interrupted(e):
            return {"ok": False, "error": "superseded", "detail": "Preview superseded by a more recent request"}
        if isinstance(e, IngestError):  # the database's message, clear already (db_errors)
            return {"ok": False, "error": "unprocessable", "detail": str(e)}
        raise
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
    """Materializes a chain's output in the cache (BigQuery: a native table), OUTSIDE the
    preview that asked for it. Best effort: an error here costs only the cache, never the
    preview."""
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
        logger.info("materialize_step_task %s ops=%d written=%s %.0f ms", engine, len(operations), written,
                    (_time_mod.perf_counter() - t0) * 1000)
        return {"ok": True, "written": bool(written)}
    except Exception as e:  # noqa: BLE001
        logger.warning("materialize_step_task %s failed: %s", engine, e)
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
    Runs a transformation flow (a whole run) on a parquet of the storage.

    `contract`: the data contract of the datasource the output will be published to.
    It is evaluated on the parquet just written and the report comes back with the
    result: the gateway decides whether to publish (see check_before_publish).

    Reads `input_key`, applies the chain of `operations` with the Polars engine,
    streaming, and writes the result to `output_key`. When `destination` is given
    ({"type": "database"|"s3", "connection": …, "target": …}, the secret
    Fernet-encrypted as for the ingest), the parquet just written is THEN poured into
    the destination: a database table, or an object / hive-partitioned dataset on S3.
    The parquet stays on the storage anyway (history, inspection).

    Args:
        bucket: the S3 bucket's name
        input_key: the input parquet's key
        operations: the operations (IR: {"type": ..., "params": ...})
        output_key: the output parquet's key
        destination: an optional destination (Output node): type database | s3 | sftp

    Returns:
        The run's metadata (rows written, output columns, the destination's outcome).
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
            # A blocking rule is violated: the gateway will not publish this data.
            # Nothing that follows may take it out — the S3 copy and the email would
            # hand someone exactly what the contract refused.
            logger.warning("contract violated on %s: output not delivered", output_key)
            return out

    # one moment for every template of the run's outputs (file name, key, subject):
    # the SFTP file and the email of the same run say the same date
    from datetime import datetime as _dt, timezone as _tz

    moment = _dt.now(_tz.utc)

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
                conn=conn, dest=dest, bucket=bucket, key=output_key, now=moment
            )
        elif dest_type == "sftp":
            from app.ingest import sftp as sftp_dest

            s_conn = sftp_dest.SftpConnectionSpec(**destination["connection"])
            s_dest = sftp_dest.SftpDestinationSpec(**destination["target"])
            logger.info(f"📤 Writing output to sftp {s_conn.address} folder {s_dest.folder or '~'} name {s_dest.file_name}")
            out["destination"] = sftp_dest.write_output_to_sftp(
                conn=s_conn, dest=s_dest, bucket=bucket, key=output_key, now=moment
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

    # A copy to an external S3, IN ADDITION to the output: best effort by choice. It
    # comes AFTER the destination because the primary result comes first; its failure
    # (expired credentials, a full bucket, the network) must not undo a successful run
    # nor stop the datasource being published, which the gateway does reading this
    # same result. The outcome goes back in `out` and the gateway records it on the
    # run, so the error stays visible in the history.
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
            logger.info(f"📎 Copy to s3 {m_conn.endpoint_url or 'aws'} key {m_dest.key}")
            out["mirror"] = {
                "ok": True,
                **write_output_to_s3(conn=m_conn, dest=m_dest, bucket=bucket, key=output_key, now=moment),
            }
        except Exception as e:
            logger.warning(f"⚠️ copy to S3 failed (the run stays valid): {type(e).__name__}: {e}")
            out["mirror"] = {
                "ok": False,
                "bucket": m_target.get("bucket", ""),
                "key": m_target.get("key", ""),
                "error": f"{type(e).__name__}: {e}"[:500],
            }

    # Emailing the output as an attachment (csv/xlsx). It comes LAST: it sends what was
    # produced, so everything else must be done already.
    #
    # Unlike the S3 copy it is NOT best effort, by the user's choice: a report that
    # does not go out is the missing result, and a green run while nobody receives
    # anything is worse than a red one. `stop_on_failure` (on by default) decides
    # whether the error fails the run; the outcome goes back in `out` anyway, and the
    # gateway records it on the run's row.
    if email:
        from app.ingest.email_destination import (
            EmailSpec,
            SmtpConnectionSpec,
            send_output_email,
        )

        spec_raw = dict(email.get("target") or {})
        stop = bool(email.get("stop_on_failure", True))
        try:
            e_conn = SmtpConnectionSpec(**email["connection"])
            e_spec = EmailSpec(**spec_raw)
            logger.info("📧 Sending email through %s to %d recipients", e_conn.host, len(e_spec.to))
            out["email"] = send_output_email(conn=e_conn, spec=e_spec, bucket=bucket, key=output_key, now=moment)
        except Exception as e:
            out["email"] = {
                "ok": False,
                "recipients": len(spec_raw.get("to") or []),
                "attachment": spec_raw.get("attachment_name", ""),
                "error": f"{type(e).__name__}: {e}"[:500],
            }
            if stop:
                logger.error("❌ sending the email failed, the run fails: %s", e)
                raise
            logger.warning("⚠️ sending the email failed (the run stays valid): %s", e)

    logger.info(
        f"✅ Completed transform_data_task: {output_key} "
        f"({result.rows_written} rows, {len(result.columns)} columns)"
    )
    return out


@celery_app.task(name="app.tasks.jobs.pushmail_task")
def pushmail_task(connection: dict[str, Any], spec: dict[str, Any]) -> dict:
    """The Pushmail cell of a flow group: one email, one .xlsx per datasource and one file
    per report, rendered here (app.ingest.pushmail). A refused send fails the task with
    the reason."""
    from app.ingest.email_destination import SmtpConnectionSpec
    from app.ingest.pushmail import PushmailSpec, send_pushmail

    pm = PushmailSpec(**spec)
    logger.info("📧 Starting pushmail_task: %d datasources, %d reports to %d recipients", len(pm.attachments),
                len(pm.reports), len(pm.to) + len(pm.cc))
    result = send_pushmail(SmtpConnectionSpec(**connection), pm)
    return {"rows_written": result["rows"], "email": result}


@celery_app.task(name="app.tasks.jobs.ingest_sharepoint_task")
def ingest_sharepoint_task(
    connection: dict[str, Any],
    source: dict[str, Any],
    bucket: str,
    output_key: str,
    contract: dict[str, Any] | None = None,
    last_refresh: str | None = None,
    previous: dict[str, Any] | None = None,
) -> dict:
    """Downloads from SharePoint the Excel files that match the path (globs too), stacks
    them and writes the parquet. The same shape as `ingest_database_task`: the gateway
    treats the two ingests alike (run, snapshot swap, refresh). The app's secret arrives
    encrypted and is decrypted only to ask for the token."""
    from app.ingest.sharepoint_source import (
        SharePointConnectionSpec,
        SharePointSourceSpec,
        ingest_sharepoint_to_parquet,
    )

    conn = SharePointConnectionSpec(**connection)
    src = SharePointSourceSpec(**source)
    logger.info(f"🚀 Starting ingest_sharepoint_task: {conn.site_url} :: {src.path} [{src.sheet}] → {output_key}")
    advances = _Progress()
    result = ingest_sharepoint_to_parquet(conn, src, bucket=bucket, key=output_key, storage=get_storage_service(),
                                          progress=advances, last_refresh=last_refresh, previous=previous)
    logger.info(
        f"✅ Completed ingest_sharepoint_task: {output_key} "
        f"({result['rows_written']} rows from {len(result['files'])} files)"
    )
    if contract:
        from app.contracts.service import check_before_publish

        advances("checking", rows=result["rows_written"])

        result["contract"] = check_before_publish(bucket, output_key, contract)
    return result


@celery_app.task(name="app.tasks.jobs.ingest_sftp_task")
def ingest_sftp_task(
    connection: dict[str, Any],
    source: dict[str, Any],
    bucket: str,
    output_key: str,
    contract: dict[str, Any] | None = None,
    last_refresh: str | None = None,
    previous: dict[str, Any] | None = None,
) -> dict:
    """Reads the files of an SFTP folder that match the glob, stacks them and
    writes the parquet: the same shape as the SharePoint and database ingests, so
    the gateway treats it the same way (run, snapshot swap, refresh). The host key
    is checked before any credential leaves; secrets arrive encrypted."""
    from app.ingest.sftp import SftpConnectionSpec, SftpSourceSpec, ingest_sftp_to_parquet

    conn = SftpConnectionSpec(**connection)
    src = SftpSourceSpec(**source)
    logger.info(f"🚀 Starting ingest_sftp_task: {conn.address} :: {src.folder or '~'}/{src.pattern} → {output_key}")
    advances = _Progress()
    result = ingest_sftp_to_parquet(conn, src, bucket=bucket, key=output_key, storage=get_storage_service(),
                                    progress=advances, last_refresh=last_refresh, previous=previous)
    logger.info(f"✅ Completed ingest_sftp_task: {output_key} ({result['rows_written']} rows from {len(result['files'])} files)")
    if contract:
        from app.contracts.service import check_before_publish

        advances("checking", rows=result["rows_written"])
        result["contract"] = check_before_publish(bucket, output_key, contract)
    return result


@celery_app.task(name="app.tasks.jobs.ingest_gsheets_task")
def ingest_gsheets_task(
    connection: dict[str, Any],
    source: dict[str, Any],
    bucket: str,
    output_key: str,
    contract: dict[str, Any] | None = None,
) -> dict:
    """Reads one tab of a Google Sheets spreadsheet (or its range) and writes the
    parquet: the same shape as the other ingests, so the gateway treats it the same
    way (run, snapshot swap, refresh). The service account's key arrives encrypted and
    is decrypted only to sign the token request."""
    from app.ingest.gsheets import GsheetsConnectionSpec, GsheetsSourceSpec, ingest_gsheets_to_parquet

    conn = GsheetsConnectionSpec(**connection)
    src = GsheetsSourceSpec(**source)
    logger.info(f"🚀 Starting ingest_gsheets_task: {src.spreadsheet_id} [{src.sheet or src.sheet_id}] → {output_key}")
    advances = _Progress()
    result = ingest_gsheets_to_parquet(conn, src, bucket=bucket, key=output_key, storage=get_storage_service(),
                                       progress=advances)
    logger.info(f"✅ Completed ingest_gsheets_task: {output_key} ({result['rows_written']} rows)")
    if contract:
        from app.contracts.service import check_before_publish

        advances("checking", rows=result["rows_written"])
        result["contract"] = check_before_publish(bucket, output_key, contract)
    return result


@celery_app.task(name="app.tasks.jobs.ingest_s3_task")
def ingest_s3_task(
    connection: dict[str, Any],
    source: dict[str, Any],
    bucket: str,
    output_key: str,
    contract: dict[str, Any] | None = None,
    last_refresh: str | None = None,
    previous: dict[str, Any] | None = None,
) -> dict:
    """Reads the objects of a bucket's folder that match the glob, stacks them and
    writes the parquet: the same shape as the SFTP ingest. The secret arrives
    encrypted."""
    from app.ingest.s3_destination import S3ConnectionSpec
    from app.ingest.s3_source import S3SourceSpec, ingest_s3_to_parquet

    conn = S3ConnectionSpec(**connection)
    src = S3SourceSpec(**source)
    logger.info(f"🚀 Starting ingest_s3_task: {conn.endpoint_url or 'aws'} {src.bucket or conn.bucket}/{src.folder}/{src.pattern} → {output_key}")
    advances = _Progress()
    result = ingest_s3_to_parquet(conn, src, bucket=bucket, key=output_key, storage=get_storage_service(),
                                  progress=advances, last_refresh=last_refresh, previous=previous)
    logger.info(f"✅ Completed ingest_s3_task: {output_key} ({result['rows_written']} rows from {len(result['files'])} files)")
    if contract:
        from app.contracts.service import check_before_publish

        advances("checking", rows=result["rows_written"])
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
    advances = _Progress()
    result = ingest_api_to_parquet(conn, src, bucket=bucket, key=output_key, storage=get_storage_service(),
                                   progress=advances, last_refresh=last_refresh, previous=previous)
    logger.info(f"✅ Completed ingest_api_task: {output_key} ({result['rows_written']} rows, {result['calls']} calls, "
                f"{result['pages']} pages)")
    if contract:
        from app.contracts.service import check_before_publish

        advances("checking", rows=result["rows_written"])
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
    Ingest from a database: runs the source (table or SQL) and writes the result as
    parquet on the storage, streaming (Arrow batches → ParquetWriter).

    `contract`: the data contract of the datasource being refreshed, evaluated on the
    parquet just written (see transform_data_task).

    `connection.password_encrypted` is encrypted (Fernet, the key shared with the
    gateway): the clear password never goes through the broker.
    """
    from app.ingest.db_source import DbConnectionSpec, DbSourceSpec, ingest_db_to_parquet

    conn = DbConnectionSpec(**connection)
    logger.info(f"🚀 Starting ingest_database_task: {conn.db_type}@{conn.host} → {output_key}")

    advances = _Progress()
    result = ingest_db_to_parquet(
        conn=conn,
        source=DbSourceSpec(**source),
        bucket=bucket,
        key=output_key,
        progress=advances,
    )

    logger.info(
        f"✅ Completed ingest_database_task: {output_key} "
        f"({result['rows_written']} rows, {len(result['columns'])} columns)"
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

        advances("checking", rows=result["rows_written"])
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
    Asynchronous conversion of a large file (raw already on the storage) to parquet.

    Used by the ingest above the 50 MB threshold; it reuses the synchronous path's
    conversion. Returns the dataset's metadata (schema, rows).
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

    logger.info(f"✅ Completed convert_to_parquet_task: {parquet_key} ({info.rows} rows)")
    return {"status": "success", **info.model_dump()}


# storage prefixes whose size is tracked (for the metrics)
STORAGE_PREFIXES = ["raw/", "datasets/", "cache/", "out/"]
STORAGE_BYTES_HASH = "dataprep:metrics:storage_bytes"
STORAGE_OBJECTS_HASH = "dataprep:metrics:storage_objects"


@celery_app.task(name="app.tasks.jobs.storage_stats_task")
def storage_stats_task() -> dict:
    """
    Samples the size of the storage prefixes (cache/, datasets/, ...) and writes it to
    Valkey, where the metrics collector exposes it. Scheduled by Celery beat (interval:
    METRICS__STORAGE_STATS_INTERVAL_SECONDS).
    """
    settings = get_settings()
    storage = get_storage_service()
    r = get_engine().cache.redis  # reuses the Valkey client (decode_responses=True)

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
    Periodic eviction of the step cache: removes the entries not accessed for more than
    `cache.ttl_seconds`. Scheduled by Celery beat (see celery_app.py).
    """
    settings = get_settings()
    removed = get_engine().cache.evict_expired(settings.cache.ttl_seconds)
    # BigQuery's native cache has an index of its own (tables, not parquet): the tables
    # expire by themselves on the service, here the index is aligned
    from app.engine import _BIGQUERY_AVAILABLE

    if _BIGQUERY_AVAILABLE:
        try:
            removed += get_engine("bigquery").cache.evict_expired(settings.cache.ttl_seconds)
        except Exception as e:  # noqa: BLE001 — the sweep must not stop for an optional engine
            logger.warning("sweep of the BigQuery cache failed: %s", e)
    return {"removed": removed, "ttl_seconds": settings.cache.ttl_seconds}


@celery_app.task(name="app.tasks.jobs.evict_matview_task")
def evict_matview_task() -> dict:
    """Periodic drop of the viewer's materialized tables on the external ClickHouse
    (expired unused + orphaned). Scheduled by Celery beat; it skips itself only when
    the engine is missing or ClickHouse is not configured — NOT when the materialization
    is off: what is left from when it was on (or from a build killed half way) must be
    removed anyway."""
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
        return _deliver_export(self.request.id, path, format, filename)
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


def _deliver_export(task_id: str | None, path: str, format: str, filename: str | None) -> dict:
    """Leaves the written file under `exports/`, where the API takes it from."""
    import os
    import uuid

    out_bucket = get_settings().storage.bucket
    key = f"{EXPORT_PREFIX}{task_id or uuid.uuid4().hex}.{format}"
    get_storage_service().upload_file(path, out_bucket, key)
    return {"ok": True, "bucket": out_bucket, "key": key, "format": format,
            "filename": filename or f"export.{format}", "size": os.path.getsize(path)}


@celery_app.task(bind=True, name="app.tasks.jobs.db_export_task")
def db_export_task(self, connection: dict[str, Any], sql: str, format: str = "csv",
                   filename: str | None = None) -> dict:
    """A SQL script's whole result on a database connection, as a csv or xlsx file: the
    database's rows go to a temporary parquet snapshot (the import's reader, asked for no
    more rows than the format's cap and one), and Polars writes the file from it, with
    the caps and the errors of `export_task`. Tagged as export_task's answer."""
    import os
    import tempfile
    import uuid

    from celery.exceptions import SoftTimeLimitExceeded

    from app.engine.exceptions import EngineError
    from app.engine.polars_engine import PolarsEngine
    from app.ingest.converters import IngestError
    from app.ingest.db_source import DbConnectionSpec, DbSourceSpec, ingest_db_to_parquet

    storage = get_storage_service()
    snapshot = DataSource(bucket=get_settings().storage.bucket, key=f"tmp/export_{uuid.uuid4().hex}.parquet")
    max_rows = get_settings().export.max_rows or None
    cap = max_rows if format == "csv" else PolarsEngine.XLSX_MAX_ROWS
    fd, path = tempfile.mkstemp(suffix=f".{format}")
    os.close(fd)
    written = False
    try:
        ingest_db_to_parquet(DbConnectionSpec(**connection), DbSourceSpec(mode="sql", ref=sql),
                             snapshot.bucket, snapshot.key, storage=storage, cap=cap + 1 if cap else None)
        written = True
        get_engine("polars").export(source=snapshot, operations=[], fmt=format, out_path=path, max_rows=max_rows)
        return _deliver_export(self.request.id, path, format, filename)
    except IngestError as e:  # the database's message, clear already (db_errors)
        return {"ok": False, "error": "unprocessable", "detail": str(e)}
    except EngineError as e:
        return {"ok": False, "error": "bad_request", "detail": str(e)}
    except SoftTimeLimitExceeded:
        return {"ok": False, "error": "timeout", "detail": "The export took longer than the time limit of the workers"}
    finally:
        os.unlink(path)
        if written:
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
