"""Reports (INTERNAL endpoints, reached by the gateway only; docs/specs/reports.md).

- POST /reports/render: starts the render of a report whose datasets the gateway has
  resolved with the reader's rights; the file is followed and taken, once, as the Viewer's
  exports (GET /tasks/exports/{id}, …/file), by its owner.
- POST /reports/dashboard-render: the same for a dashboard, as a PDF or an image
  (app/reports/dashboard_layout.py), with what this reader may read and the clicks of the moment.
- POST /reports/check: the mistakes of a definition, each with its item, its property and
  the position inside the expression — what the editor shows when a report is saved.
- POST /reports/choices: the choices of a parameter taken from a dataset, for the form; on
  the preview worker, waited for as a preview (a saved dashboard's: `cache`, as for its tiles).
- POST /reports/count: how many rows a dataset gives, up to REPORTS__MAX_ROWS (the
  designer's hint next to the dataset); on the preview worker, waited for `seconds` at most.
- POST /reports/tiles: a dashboard's numbers, tile by tile (app/reports/tiles.py); on the
  preview worker, waited for as a preview. With `cache`, a saved dashboard's: shared by whoever
  reads the same thing, and worked out once (app/reports/tile_cache.py).
- POST /reports/tiles/refresh: a saved dashboard's refresh by hand, at most one every 10 seconds
  for everyone: the token that makes its numbers be read again, or none.
"""
import uuid
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.api import exports
from app.api.models import TaskResponse
from app.reports import definition as rd
from app.reports import formats, tile_cache
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
    # what the gateway wrote for its AI comments, by item: {text, model, numbers_at} or {message}
    comments: Optional[dict[str, dict]] = None
    reading: Optional[str] = Field(default=None, max_length=64)  # the rows those were written on (tile_cache.reader_of)


class ReportStarted(TaskResponse):
    filename: str  # with the extension of what the file holds (.mhtml, .json for svg)


class DashboardCache(BaseModel):
    """A saved dashboard's: what the gateway knows that the cache needs. Rights were checked
    before this, with the reader's: what is asked here is theirs to read."""
    refresh_seconds: int = Field(default=0, ge=0)
    fresh: Optional[str] = Field(default=None, max_length=64)  # the token of a refresh claimed


class ReportChoices(BaseModel):
    definition: dict
    dataset: str
    datasets: dict[str, dict] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    value: str = Field(..., min_length=1, max_length=4000)  # expressions, as a parameter's `choices`
    label: Optional[str] = Field(default=None, max_length=4000)
    limit: int = Field(default=500, ge=1, le=5000)
    locale: Optional[Literal[formats.LOCALES]] = None  # type: ignore[valid-type]
    cache: Optional[DashboardCache] = None


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


class DashboardTiles(BaseModel):
    definition: dict  # a dashboard's, written as a report's, without its images
    datasets: dict[str, dict] = Field(default_factory=dict)  # those the reader may read, as a render's
    # those not resolved: dataset name → what its tiles say, {"error", "code"}
    refused: dict[str, dict] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    tiles: Optional[list[str]] = Field(default=None, max_length=500)  # none: every one
    pages: dict[str, int] = Field(default_factory=dict)  # a table's page, by tile, from 0
    state: Optional[dict] = None  # the screen's sort and open/closed groups, as a render's
    clicks: list[dict] = Field(default_factory=list, max_length=500)  # {"tile", "on", "values"}: the expressions are the definition's
    locale: Optional[Literal[formats.LOCALES]] = None  # type: ignore[valid-type]
    title: Optional[str] = Field(default=None, max_length=300)  # REPORT()
    slot: Optional[str] = Field(default=None, max_length=200)  # a newer request on the same slot knocks this one down
    cache: Optional[DashboardCache] = None  # then no slot: others may be waiting for this computation
    reading: Optional[str] = Field(default=None, max_length=64)  # for an AI comment in a document: the rows its pages will read
    of_report: bool = False  # a report's definition: a report's cap on the rows


class DashboardRender(BaseModel):
    definition: dict  # a dashboard's, written as a report's, with its images
    tiles: list[dict] = Field(default_factory=list, max_length=500)  # {"id", "title", "grid"} of each
    datasets: dict[str, dict] = Field(default_factory=dict)  # those the reader may read, as a render's
    refused: dict[str, dict] = Field(default_factory=dict)  # those not resolved: what their tiles say
    params: dict[str, Any] = Field(default_factory=dict)
    clicks: list[dict] = Field(default_factory=list, max_length=500)  # as a dashboard's numbers
    state: Optional[dict] = None
    format: Literal["pdf", "png"] = "pdf"
    filename: Optional[str] = Field(default=None, max_length=255)
    owner: str = Field(..., min_length=1, description="Who asked for the render, as the gateway names them")
    locale: Optional[Literal[formats.LOCALES]] = None  # type: ignore[valid-type]
    title: Optional[str] = Field(default=None, max_length=300)
    page: Optional[dict] = None  # the one chosen: A4 across otherwise
    cache: Optional[DashboardCache] = None  # a saved dashboard's: the rows are those its screen reads
    comments: Optional[dict[str, dict]] = None  # what the gateway wrote for its AI comments, by tile


class TilesRefresh(BaseModel):
    dashboard: int


@router.post("/check")
def check(request: ReportCheck) -> dict:
    return {"problems": [p.as_dict() for p in rd.check(request.definition, request.columns)]}


def _worker_said(payload: dict, otherwise: str) -> dict:
    """A task's tagged answer: its result, or its failure as the HTTP status."""
    from app.api.routes.tasks import _PREVIEW_ERROR_STATUS

    if not payload.get("ok"):
        status = {**_PREVIEW_ERROR_STATUS, "unavailable": 503}.get(payload.get("error"), 400)
        raise HTTPException(status_code=status, detail=payload.get("detail", otherwise))
    return payload["result"]


_CHOICES_TASK = "app.tasks.report_jobs.report_choices_task"
_TILES_TASK = "app.tasks.report_jobs.dashboard_tiles_task"


@router.post("/choices")
async def choices(request: ReportChoices) -> dict:
    from app.api.routes.tasks import on_preview_worker

    said = "The choices could not be read"
    kwargs = request.model_dump(exclude={"cache"})
    if request.cache is None:
        return _worker_said(await run_in_threadpool(on_preview_worker, _CHOICES_TASK, kwargs, None), said)
    return await tile_cache.choices(kwargs, request.cache.model_dump(),
                                    lambda task, left: _worker_said(tile_cache.on_worker(_CHOICES_TASK, task, left), said))


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


@router.post("/tiles")
async def tiles(request: DashboardTiles) -> dict:
    from app.api.routes.tasks import on_preview_worker

    said = "The numbers could not be read"
    # what only a document's comment asks does not go otherwise: a worker of the version before is still a worker
    kwargs = request.model_dump(exclude={"slot", "cache", *(k for k in ("reading", "of_report") if not getattr(request, k))})
    if request.cache is None:
        return _worker_said(await run_in_threadpool(on_preview_worker, _TILES_TASK, kwargs, request.slot), said)
    return await tile_cache.numbers(kwargs, request.cache.model_dump(),
                                    lambda task, left: _worker_said(tile_cache.on_worker(_TILES_TASK, task, left), said))


@router.post("/tiles/refresh")
def tiles_refresh(request: TilesRefresh) -> dict:
    return {"fresh": tile_cache.claim_refresh(request.dashboard)}


@router.post("/dashboard-render", response_model=ReportStarted)
def dashboard_render(request: DashboardRender):
    """Queues a dashboard's document on the export queue; followed and taken as a report's."""
    from app.reports.dashboard_layout import filename as dashboard_file

    task_id = str(uuid.uuid4())
    exports.remember_owner(task_id, request.owner)
    name = dashboard_file(request.filename, request.format)
    kwargs = request.model_dump(exclude={"owner", "cache", "filename", *(() if request.comments else ("comments",))})
    kwargs["filename"] = name
    if request.cache is not None:
        kwargs["cache_seconds"] = tile_cache.lifetime(request.cache.refresh_seconds)
    celery_app.send_task("app.tasks.report_jobs.render_dashboard_task", kwargs=kwargs, task_id=task_id)
    return ReportStarted(task_id=task_id, status="submitted", message=f"Rendering a dashboard as {request.format}", filename=name)


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
    kwargs.update({k: v for k, v in (("locale", request.locale), ("state", request.state), ("title", request.title),
                                     ("comments", request.comments), ("reading", request.reading)) if v is not None})
    celery_app.send_task("app.tasks.report_jobs.render_report_task", kwargs=kwargs, task_id=task_id)
    return ReportStarted(task_id=task_id, status="submitted", message=f"Rendering a report as {request.format}",
                         filename=name)
