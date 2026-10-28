"""Reports (INTERNAL endpoints, reached by the gateway only; docs/specs/reports.md).

- POST /reports/render: starts the render of a report whose datasets the gateway has
  resolved with the reader's rights; the file is followed and taken, once, as the Viewer's
  exports (GET /tasks/exports/{id}, …/file), by its owner.
- POST /reports/check: the mistakes of a definition, each with its item, its property and
  the position inside the expression — what the editor shows when a report is saved.
- POST /reports/choices: the choices of a parameter taken from a dataset, for the form; on
  the preview worker, waited for as a preview.
- POST /reports/count: how many rows a dataset gives, up to REPORTS__MAX_ROWS (the
  designer's hint next to the dataset); on the preview worker, waited for `seconds` at most.
"""
import uuid
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api import exports
from app.api.models import TaskResponse
from app.reports import definition as rd
from app.reports import formats
from app.reports.render import filename as file_name
from app.tasks.celery_app import celery_app

router = APIRouter(prefix="/reports", tags=["reports"])


class ReportRender(BaseModel):
    definition: dict
    # dataset name → what to read: a catalog chain {bucket, input_key, operations, engine},
    # a query {connection, sql}, or rows given as they are {rows, columns}
    datasets: dict[str, dict] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    format: Literal["pdf", "xlsx", "html", "svg"] = "pdf"
    filename: Optional[str] = Field(default=None, max_length=255)
    owner: str = Field(..., min_length=1, description="Who asked for the render, as the gateway names them")
    locale: Optional[Literal[formats.LOCALES]] = None  # type: ignore[valid-type]
    state: Optional[dict] = None  # the screen's sort and open/closed groups
    title: Optional[str] = Field(default=None, max_length=300)  # REPORT(), the PDF's title


class ReportStarted(TaskResponse):
    filename: str  # with the extension of what the file holds (.mhtml, .json for svg)


class ReportChoices(BaseModel):
    definition: dict
    dataset: str
    datasets: dict[str, dict] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    value: str = Field(..., min_length=1, max_length=4000)  # expressions, as a parameter's `choices`
    label: Optional[str] = Field(default=None, max_length=4000)
    limit: int = Field(default=500, ge=1, le=5000)
    locale: Optional[Literal[formats.LOCALES]] = None  # type: ignore[valid-type]


class ReportCheck(BaseModel):
    definition: dict
    # the columns of the datasets as the gateway knows them, name → {column: type}; without
    # them the `fields` listed in the definition are used
    columns: Optional[dict[str, dict[str, str]]] = None


class ReportCount(BaseModel):
    # what to count, as one of a render's `datasets`
    dataset: dict
    slot: Optional[str] = Field(default=None, max_length=200)  # a newer count on the same slot knocks this one down
    seconds: float = Field(default=20, gt=0, le=300)  # past it the count is stopped (504): it is only a hint


@router.post("/check")
def check(request: ReportCheck) -> dict:
    return {"problems": [p.as_dict() for p in rd.check(request.definition, request.columns)]}


@router.post("/choices")
def choices(request: ReportChoices) -> dict:
    from app.api.routes.tasks import _PREVIEW_ERROR_STATUS, on_preview_worker

    payload = on_preview_worker("app.tasks.report_jobs.report_choices_task", request.model_dump(), None)
    if not payload.get("ok"):
        status = {**_PREVIEW_ERROR_STATUS, "unavailable": 503}.get(payload.get("error"), 400)
        raise HTTPException(status_code=status, detail=payload.get("detail", "The choices could not be read"))
    return payload["result"]


@router.post("/count")
def count(request: ReportCount) -> dict:
    """{"rows", "over", "max_rows"}: the rows of the dataset counted up to the cap a render
    reads (`rows` at most `max_rows`, `over` when there are more), without reading them."""
    from app.api.routes.tasks import _PREVIEW_ERROR_STATUS, on_preview_worker

    payload = on_preview_worker("app.tasks.report_jobs.report_count_task", {"dataset": request.dataset}, request.slot,
                                seconds=request.seconds)
    if not payload.get("ok"):
        raise HTTPException(status_code=_PREVIEW_ERROR_STATUS.get(payload.get("error"), 400),
                            detail=payload.get("detail", "The rows could not be counted"))
    return payload["result"]


@router.post("/render", response_model=ReportStarted)
def render(request: ReportRender):
    """Checks the definition at once (a broken one is refused here, not by a worker later)
    and queues the render on the export queue."""
    # the fields are checked by the worker against the columns the data really has
    problems = rd.check(request.definition, any_field=True)
    if problems:
        raise HTTPException(status_code=422, detail=f"the report's definition: {problems[0]}")
    task_id = str(uuid.uuid4())
    exports.remember_owner(task_id, request.owner)
    name = file_name(request.filename, request.format)
    kwargs: dict[str, Any] = {"definition": request.definition, "datasets": request.datasets, "params": request.params,
                              "format": request.format, "filename": name}
    kwargs.update({k: v for k, v in (("locale", request.locale), ("state", request.state), ("title", request.title))
                   if v is not None})
    celery_app.send_task("app.tasks.report_jobs.render_report_task", kwargs=kwargs, task_id=task_id)
    return ReportStarted(task_id=task_id, status="submitted", message=f"Rendering a report as {request.format}",
                         filename=name)
