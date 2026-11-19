"""The render of a report, on the export queue (docs/specs/reports.md, «Rendering (engine)»),
and of the report of a hypothesis test, drawn from the rows the test wrote
(docs/specs/statistics.md, «The report»).

The file is left under `exports/` as the Viewer's exports are, and its owner was recorded
by the route that started it: the gateway follows it and takes it, once, through
/tasks/exports/{id} and /tasks/exports/{id}/file. Cancelling it is killing the worker's
process (DELETE /tasks/{id}): the Typst child dies with it (app/reports/typst_child.py).
"""
from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="app.tasks.report_jobs.render_report_task", bind=True)
def render_report_task(
    self,
    definition: dict,
    datasets: dict[str, dict],
    params: dict[str, Any] | None = None,
    format: str = "pdf",
    filename: str | None = None,
    locale: str | None = None,
    state: dict | None = None,
    title: str | None = None,
    comments: dict[str, dict] | None = None,
    reading: str | None = None,
) -> dict:
    """Renders the report and leaves the file for its owner. Tagged as export_task's
    answer: what stopped the render is an answer with its reason, not an exception.
    `comments`: what the gateway wrote for its AI comments; `reading`: the rows they were
    written on (app/reports/tile_cache.py)."""
    from celery.exceptions import SoftTimeLimitExceeded

    from app.reports import render, tile_cache
    from app.reports.errors import ReportError
    from app.tasks.jobs import _deliver_export

    name = render.filename(filename, format)
    extra = {"comments": comments, "read": tile_cache.reader_of(reading) if reading else None}
    with tempfile.TemporaryDirectory(prefix="report-") as work:
        try:
            result = render.render(definition, datasets, params, format, Path(work), locale=locale, state=state,
                                   title=title or name.rsplit(".", 1)[0], **extra)
        except ReportError as e:
            return {"ok": False, "error": e.tag, "detail": e.message}
        except SoftTimeLimitExceeded:
            return {"ok": False, "error": "timeout", "detail": "The report took longer than the time limit of the workers"}
        payload = _deliver_export(self.request.id, str(result.path), format, name)
    if result.warnings:
        payload["warnings"] = result.warnings
    if result.pages is not None:
        payload["pages"] = result.pages
    logger.info("report rendered: %s, %s bytes, %s pages", format, payload.get("size"), result.pages)
    return payload


# ── the report of a hypothesis test (docs/specs/statistics.md, «The report») ──
STAT_ROWS_PREFIX = "dataprep:statreport:"
# past this the rows are not remembered: a test's are some hundreds, a few thousand with 200 groups
STAT_ROWS_MAX_BYTES = 2 * 1024 * 1024
STAT_TIMEOUT = ("The test took longer than the workers allow: give the source a development sample, "
                "or filter before the test")


def stat_rows_key(engine_name: str, bucket: str, input_key: str, operations: list[dict]) -> str:
    """Where the rows of a chain that ends with a test are remembered: under the chain's
    fingerprint, as its count and its column profile are (a datasource refreshed is another
    object, so another key)."""
    from app.engine import rowcount

    return STAT_ROWS_PREFIX + rowcount.count_key(engine_name, bucket, input_key, operations)[len(rowcount.COUNT_PREFIX):]


def _stat_rows_remembered(key: str) -> list[dict] | None:
    from app.engine import rowcount
    from app.engine.stats.output import OUTPUT_NAMES

    try:
        raw = rowcount._redis().get(key)
        rows = json.loads(raw)["rows"] if raw else None
    except Exception:  # noqa: BLE001 — Valkey that does not answer, or something else there: not remembered
        return None
    if not isinstance(rows, list) or not all(isinstance(r, list) and len(r) == len(OUTPUT_NAMES) for r in rows):
        return None
    return [dict(zip(OUTPUT_NAMES, r)) for r in rows]


def _stat_rows_remember(key: str, rows: list[dict]) -> None:
    from app.engine import rowcount
    from app.engine.stats.output import OUTPUT_NAMES

    try:
        raw = json.dumps({"rows": [[r.get(name) for name in OUTPUT_NAMES] for r in rows]}, ensure_ascii=False)
        if len(raw.encode("utf-8")) < STAT_ROWS_MAX_BYTES:
            rowcount._redis().set(key, raw, ex=rowcount._ttl())
    except Exception:  # noqa: BLE001, S110 — a courtesy: the next time they are read again
        pass


def stat_rows(engine: str | None, bucket: str, input_key: str, operations: list[dict]) -> list[dict]:
    """The rows a test writes for its report, in their order: the chain with its last step's
    `detail` set to `report`, read once as a preview reads it (the step cache on for what is
    before the test) and remembered — the PDF and the Excel asked after the pages, another
    language, a second person on the same flow reach no data again."""
    from app.engine import DEFAULT_ENGINE, DataSource, get_engine
    from app.engine.exceptions import EngineError
    from app.engine.stats import layouts
    from app.engine.stepcache_defer import wait_for_running_copy

    name = (engine or DEFAULT_ENGINE).lower()
    last = operations[-1]
    chain = [*operations[:-1], {**last, "params": {**(last.get("params") or {}), "detail": "report"}}]
    key = stat_rows_key(name, bucket, input_key, chain)
    rows = _stat_rows_remembered(key)
    if rows is not None:
        return rows
    engine_impl, source = get_engine(name), DataSource(bucket=bucket, key=input_key)
    wait_for_running_copy(engine_impl, source, chain)
    result = engine_impl.preview(source=source, operations=chain, limit=layouts.MAX_REPORT_ROWS, use_cache=True)
    # the copies of the steps before the test are written later, as after a preview (stepcache_defer)
    for src, pending in getattr(engine_impl, "take_pending", lambda: [])():
        try:
            from app.tasks.jobs import CACHE_COPY_PRIORITY, materialize_step_task

            materialize_step_task.apply_async(args=[engine, src.bucket, src.key, pending], queue="preview",
                                              priority=CACHE_COPY_PRIORITY)
        except Exception as e:  # noqa: BLE001 — without a broker only the cache is lost
            logger.warning("deferred materialization not queued: %s", e)
    if result.truncated:
        raise EngineError(f"{last.get('type')}: the test wrote more than {layouts.MAX_REPORT_ROWS:,} rows, the most its report reads")
    rows = sorted(result.rows, key=lambda r: r["ord"])
    _stat_rows_remember(key, rows)
    return rows


@celery_app.task(name="app.tasks.report_jobs.render_stat_report_task", bind=True)
def render_stat_report_task(
    self,
    bucket: str,
    input_key: str,
    operations: list[dict],
    engine: str | None = None,
    format: str = "svg",
    filename: str | None = None,
    locale: str | None = None,
    step: str | None = None,
) -> dict:
    """The report of the hypothesis test a chain ends with, left for its owner as a report's
    render: the test's rows (`stat_rows`), the layout preset for that test in the language
    asked (app/engine/stats/layouts.py), then the report engine as for any report. The rows
    are those that reach the node: the chain is read as it came, and when it carries a
    development sample the page says so. Tagged as export_task's answer."""
    import dataclasses

    from celery.exceptions import SoftTimeLimitExceeded

    from app.engine.exceptions import EngineError, OperationError, SourceNotFoundError, UnknownOperationError
    from app.engine.stats import layouts
    from app.engine.stats.catalog import stat_operations
    from app.reports import render
    from app.reports.errors import ReportError
    from app.tasks.jobs import _deliver_export

    last = operations[-1] if operations else {}
    test = stat_operations().get(last.get("type"))
    if test is None or test not in layouts.LAYOUTS:
        return {"ok": False, "error": "unprocessable", "detail": "The last step of the chain is not a statistical test with a report"}
    params = {k: v for k, v in (last.get("params") or {}).items() if k != "detail"}
    name = render.filename(filename or layouts.title(test, params, locale), format)
    try:
        rows = stat_rows(engine, bucket, input_key, operations)
        definition = layouts.definition(test, params, locale, layouts.samples_of(operations), step)
        datasets = {layouts.DATASET: {"rows": layouts.with_labels(test, rows, locale), "columns": layouts.columns()}}
        limits = render.Limits.from_settings()
        limits = dataclasses.replace(limits, max_rows=max(limits.max_rows, layouts.MAX_REPORT_ROWS))
        with tempfile.TemporaryDirectory(prefix="stat-report-") as work:
            result = render.render(definition, datasets, {}, format, Path(work), locale=locale,
                                   title=name.rsplit(".", 1)[0], limits=limits)
            payload = _deliver_export(self.request.id, str(result.path), format, name)
    except ReportError as e:
        return {"ok": False, "error": e.tag, "detail": e.message}
    except SourceNotFoundError as e:
        return {"ok": False, "error": "not_found", "detail": str(e)}
    except (UnknownOperationError, OperationError) as e:
        return {"ok": False, "error": "unprocessable", "detail": str(e)}
    except EngineError as e:  # a test's refusal: three groups for a two-sample test, a text column as a measure
        return {"ok": False, "error": "bad_request", "detail": str(e)}
    except SoftTimeLimitExceeded:
        return {"ok": False, "error": "timeout", "detail": STAT_TIMEOUT}
    if result.warnings:
        payload["warnings"] = result.warnings
    if result.pages is not None:
        payload["pages"] = result.pages
    logger.info("statistics report rendered: %s, %s, %s bytes, %s pages", test, format, payload.get("size"), result.pages)
    return payload


@celery_app.task(name="app.tasks.report_jobs.render_dashboard_task", bind=True)
def render_dashboard_task(
    self,
    definition: dict,
    tiles: list[dict],
    datasets: dict[str, dict],
    params: dict[str, Any] | None = None,
    refused: dict[str, dict] | None = None,
    clicks: list[dict] | None = None,
    format: str = "pdf",
    filename: str | None = None,
    locale: str | None = None,
    state: dict | None = None,
    title: str | None = None,
    page: dict | None = None,
    cache_seconds: int | None = None,
    comments: dict[str, dict] | None = None,
) -> dict:
    """A dashboard as a document (app/reports/dashboard_layout.py), left for its owner as a
    report's render. `cache_seconds`: its rows are the ones the screen reads (app/reports/
    tile_cache.py), so the numbers too, and no source is asked twice. `comments`: what the
    gateway wrote on them for its AI comments."""
    from celery.exceptions import SoftTimeLimitExceeded

    from app.reports import dashboard_layout, tile_cache
    from app.reports.errors import ReportError
    from app.tasks.jobs import _deliver_export

    name = dashboard_layout.filename(filename, format)
    extra = {"read": tile_cache.reader(cache_seconds)} if cache_seconds else {}
    with tempfile.TemporaryDirectory(prefix="dashboard-") as work:
        try:
            result = dashboard_layout.render(definition, tiles, datasets, format, Path(work), params=params, refused=refused, clicks=clicks,
                                             state=state, locale=locale, page=page, title=title or name.rsplit(".", 1)[0], comments=comments, **extra)
        except ReportError as e:
            return {"ok": False, "error": e.tag, "detail": e.message}
        except SoftTimeLimitExceeded:
            return {"ok": False, "error": "timeout", "detail": "The dashboard took longer than the time limit of the workers"}
        payload = _deliver_export(self.request.id, str(result.path), format, name)
    if result.warnings:
        payload["warnings"] = result.warnings
    if result.pages is not None:
        payload["pages"] = result.pages
    logger.info("dashboard rendered: %s, %s bytes, %s pages", format, payload.get("size"), result.pages)
    return payload


@celery_app.task(name="app.tasks.report_jobs.report_choices_task")
def report_choices_task(
    definition: dict,
    dataset: str,
    datasets: dict[str, dict],
    params: dict[str, Any] | None = None,
    value: str = "",
    label: str | None = None,
    limit: int = 500,
    locale: str | None = None,
    cache_seconds: int | None = None,
) -> dict:
    """The choices of a parameter from a dataset, for the form a person is filling in: on
    the preview queue, tagged as preview_task's answer (the route maps `error` to the HTTP
    status). `cache_seconds`: a dashboard's, whose rows are shared (app/reports/tile_cache.py)."""
    from app.reports import render, tile_cache
    from app.reports.errors import ReportError

    extra = tile_cache.choices_reading(datasets.get(dataset), cache_seconds) if cache_seconds else {}
    try:
        return {"ok": True, "result": render.choices(definition, dataset, datasets, params, value, label, limit, locale, **extra)}
    except ReportError as e:
        return {"ok": False, "error": e.tag, "detail": e.message}


@celery_app.task(name="app.tasks.report_jobs.report_count_task")
def report_count_task(dataset: dict) -> dict:
    """How many rows a dataset gives, up to REPORTS__MAX_ROWS (app/reports/count.py): the
    designer's hint next to the dataset. On the preview queue, tagged as preview_task's
    answer; its queries carry the task's id, so that one knocked down is stopped on the
    server too."""
    from celery import current_task

    from app.engine.query_tag import query_tag, was_interrupted
    from app.reports import count
    from app.reports.errors import ReportError

    task_id = getattr(getattr(current_task, "request", None), "id", None)
    try:
        with query_tag(f"tab-prev:{task_id}" if task_id else None):
            return {"ok": True, "result": count.count(dataset, count.max_rows())}
    except ReportError as e:
        return {"ok": False, "error": e.tag, "detail": e.message}
    except Exception as e:
        if was_interrupted(e):
            return {"ok": False, "error": "superseded", "detail": "The count was superseded or stopped"}
        raise


@celery_app.task(name="app.tasks.report_jobs.dashboard_tiles_task")
def dashboard_tiles_task(
    definition: dict,
    datasets: dict[str, dict],
    params: dict[str, Any] | None = None,
    refused: dict[str, dict] | None = None,
    tiles: list[str] | None = None,
    pages: dict[str, int] | None = None,
    state: dict | None = None,
    locale: str | None = None,
    title: str | None = None,
    cache_seconds: int | None = None,
    clicks: list[dict] | None = None,
    reading: str | None = None,
    of_report: bool = False,
) -> dict:
    """A dashboard's numbers (app/reports/tiles.py): on the preview queue, tagged as
    preview_task's answer; its queries carry the task's id, so that one knocked down is
    stopped on the server too. `cache_seconds`: a saved dashboard's, whose rows are shared
    (app/reports/tile_cache.py). `reading`: those of one document, which its pages read after
    this, for its AI comment; `of_report`: a report's, under a report's cap."""
    from celery import current_task

    from app.engine.query_tag import query_tag, was_interrupted
    from app.reports import tile_cache
    from app.reports import tiles as rtiles
    from app.reports.errors import ReportError
    from app.reports.render import Limits

    task_id = getattr(getattr(current_task, "request", None), "id", None)
    extra: dict[str, Any] = {"read": tile_cache.reader(cache_seconds)} if cache_seconds else {}
    if reading:
        extra["read"] = tile_cache.reader_of(reading)
    if of_report:
        extra["cap"] = Limits.from_settings().max_rows
    try:
        with query_tag(f"tab-prev:{task_id}" if task_id else None):
            return {"ok": True, "result": rtiles.compute(definition, datasets, params, refused=refused, only=tiles, pages=pages,
                                                         state=state, locale=locale, title=title or "", clicks=clicks, **extra)}
    except ReportError as e:
        return {"ok": False, "error": e.tag, "detail": e.message}
    except Exception as e:
        if was_interrupted(e):
            return {"ok": False, "error": "superseded", "detail": "The request was superseded or stopped"}
        raise
