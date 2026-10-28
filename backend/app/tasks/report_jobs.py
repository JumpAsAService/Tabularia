"""The render of a report, on the export queue (docs/specs/reports.md, «Rendering (engine)»).

The file is left under `exports/` as the Viewer's exports are, and its owner was recorded
by the route that started it: the gateway follows it and takes it, once, through
/tasks/exports/{id} and /tasks/exports/{id}/file. Cancelling it is killing the worker's
process (DELETE /tasks/{id}): the Typst child dies with it (app/reports/typst_child.py).
"""
from __future__ import annotations

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
) -> dict:
    """Renders the report and leaves the file for its owner. Tagged as export_task's
    answer: what stopped the render is an answer with its reason, not an exception."""
    from celery.exceptions import SoftTimeLimitExceeded

    from app.reports import render
    from app.reports.errors import ReportError
    from app.tasks.jobs import _deliver_export

    name = render.filename(filename, format)
    with tempfile.TemporaryDirectory(prefix="report-") as work:
        try:
            result = render.render(definition, datasets, params, format, Path(work), locale=locale, state=state,
                                   title=title or name.rsplit(".", 1)[0])
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
) -> dict:
    """The choices of a parameter from a dataset, for the form a person is filling in: on
    the preview queue, tagged as preview_task's answer (the route maps `error` to the HTTP
    status)."""
    from app.reports import render
    from app.reports.errors import ReportError

    try:
        return {"ok": True, "result": render.choices(definition, dataset, datasets, params, value, label, limit, locale)}
    except ReportError as e:
        return {"ok": False, "error": e.tag, "detail": e.message}
