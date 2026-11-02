"""A worker process killed while it read a dashboard's or a report's rows (the kernel's OOM killer at the
container's memory limit) is said in words, where whoever reads the dashboard or the report looks: on the
tiles, in the list of a parameter, in the state of a document. Before, the tiles said «Internal Server
Error» and a PDF «Worker exited prematurely: signal 9 (SIGKILL) Job: 0.»."""
from __future__ import annotations

import asyncio

import pytest
from celery.exceptions import WorkerLostError
from fastapi import HTTPException

from app.api import exports
from app.api.routes import reports as routes
from app.api.routes import tasks
from app.reports import errors
from app.tasks.celery_app import celery_app
from tests.fakes import FakeRedis
from tests.test_export_route import Result

KILLED = "Worker exited prematurely: signal 9 (SIGKILL) Job: 0."
SALES = {"bucket": "b", "input_key": "datasets/1/sales.parquet", "operations": [], "engine": "duckdb"}
STOCK = {"bucket": "b", "input_key": "datasets/2/stock.parquet", "operations": [], "engine": "duckdb"}


def said(name: str | None, what: str = "a dashboard") -> str:
    if name is None:
        return (f"The data is too big for {what} to read: the worker ran out of memory. Aggregate it first, "
                f"in its queries or in a flow — {what} works best on summarized data.")
    return (f"«{name}» is too big for {what} to read: the worker ran out of memory. Aggregate it first, "
            f"in its query or in a flow — {what} works best on summarized data.")


def kpi(tile_id: str, dataset: str) -> dict:
    return {"id": tile_id, "type": "kpi", "dataset": dataset, "x": 0, "y": 0, "w": 100, "h": 10, "value": "SUM([amount])"}


def definition(*items: dict, names=("Vendite",)) -> dict:
    return {"v": 1, "locale": "en", "timezone": "UTC", "page": {"size": {"w": 2000, "h": 2000}, "margins": [0, 0, 0, 0]},
            "parameters": [{"name": "region", "type": "text", "choices": {"dataset": names[0], "value": "[region]"}}],
            "datasets": [{"name": n, "source": {"kind": "sql", "sql": "SELECT 1"}} for n in names],
            "sections": {"body": {"items": list(items) or [kpi("k", names[0])]}}}


@pytest.fixture
def killed(monkeypatch) -> list:
    """A preview worker whose process the kernel kills at every task, as Celery says it."""
    asked: list = []

    def on_preview_worker(task, kwargs, slot, seconds=None, watch_worker=False):
        asked.append(task)
        raise WorkerLostError(KILLED)

    monkeypatch.setattr(tasks, "on_preview_worker", on_preview_worker)
    return asked


def refused(call) -> HTTPException:
    with pytest.raises(HTTPException) as e:
        asyncio.run(call)
    return e.value


# ── the numbers of a dashboard ───────────────────────────────────────────────
def test_tiles_whose_worker_was_killed_say_their_dataset_is_too_big_for_a_dashboard(killed):
    e = refused(routes.tiles(routes.DashboardTiles(definition=definition(), datasets={"Vendite": SALES}, slot="s")))
    assert (e.status_code, e.detail) == (422, said("Vendite")) and killed == [routes._TILES_TASK]


def test_a_report_s_numbers_say_it_of_a_report(killed):
    e = refused(routes.tiles(routes.DashboardTiles(definition=definition(), datasets={"Vendite": SALES}, of_report=True)))
    assert (e.status_code, e.detail) == (422, said("Vendite", "a report"))


def test_the_dataset_is_named_only_when_the_tiles_asked_read_one(killed):
    d = definition(kpi("a", "Vendite"), kpi("b", "Magazzino"), names=("Vendite", "Magazzino"))
    both = {"Vendite": SALES, "Magazzino": STOCK}
    one = refused(routes.tiles(routes.DashboardTiles(definition=d, datasets=both, tiles=["b"])))
    every = refused(routes.tiles(routes.DashboardTiles(definition=d, datasets=both)))
    assert one.detail == said("Magazzino") and every.detail == said(None)


def test_a_dataset_the_reader_may_not_read_is_never_named(killed):
    """It was not read: the tiles of the other one were."""
    d = definition(kpi("a", "Vendite"), kpi("b", "Magazzino"), names=("Vendite", "Magazzino"))
    e = refused(routes.tiles(routes.DashboardTiles(definition=d, datasets={"Vendite": SALES},
                                                   refused={"Magazzino": {"error": "no", "code": "no_access"}})))
    assert e.detail == said("Vendite")


def test_a_worker_lost_for_another_reason_is_not_called_out_of_memory(monkeypatch):
    def lost(task, kwargs, slot, seconds=None, watch_worker=False):
        raise WorkerLostError("Worker exited prematurely: exitcode 70 Job: 0.")

    monkeypatch.setattr(tasks, "on_preview_worker", lost)
    with pytest.raises(WorkerLostError):
        asyncio.run(routes.tiles(routes.DashboardTiles(definition=definition(), datasets={"Vendite": SALES})))


def test_what_celery_keeps_of_the_kill_is_still_known_as_one():
    """The API reads the failure back from the result backend, not from the worker."""
    backend = celery_app.backend
    back = backend.exception_to_python(backend.prepare_exception(WorkerLostError(KILLED)))
    assert errors.worker_killed(back) and not errors.worker_killed(RuntimeError(KILLED))


# ── the list of a parameter ──────────────────────────────────────────────────
@pytest.mark.parametrize("of_dashboard, what", [(False, "a report"), (True, "a dashboard")])
def test_the_choices_of_a_parameter_say_it_of_their_dataset(killed, of_dashboard, what):
    e = refused(routes.choices(routes.ReportChoices(definition=definition(), dataset="Vendite", datasets={"Vendite": SALES},
                                                    value="[region]", of_dashboard=of_dashboard)))
    assert (e.status_code, e.detail) == (422, said("Vendite", what)) and killed == [routes._CHOICES_TASK]


def test_the_worker_is_not_told_whose_choices_they_are(monkeypatch):
    """A worker of the version before has no such argument: it would refuse the task."""
    sent = []
    monkeypatch.setattr(tasks, "on_preview_worker", lambda task, kwargs, slot, **kw: sent.append(kwargs) or {"ok": True, "result": {}})
    asyncio.run(routes.choices(routes.ReportChoices(definition=definition(), dataset="Vendite", datasets={"Vendite": SALES},
                                                    value="[region]", of_dashboard=True)))
    assert "of_dashboard" not in sent[0]


# ── a document ───────────────────────────────────────────────────────────────
@pytest.fixture
def export_queue(monkeypatch) -> dict:
    """The export queue, where every document's worker process is killed."""
    results: dict[str, Result] = {}
    monkeypatch.setattr(exports, "_client", FakeRedis())
    monkeypatch.setattr(routes.celery_app, "send_task",
                        lambda name, kwargs, task_id: results.setdefault(task_id, Result(WorkerLostError(KILLED), state="FAILURE")))
    monkeypatch.setattr(tasks, "AsyncResult", lambda task_id, app=None: results.get(task_id) or Result(None, state="PENDING"))
    return results


def test_a_dashboard_s_document_whose_worker_was_killed_says_its_dataset_is_too_big(export_queue):
    for fmt in ("pdf", "png"):
        started = routes.dashboard_render(routes.DashboardRender(definition=definition(), datasets={"Vendite": SALES},
                                                                 format=fmt, owner="u1", cache={"refresh_seconds": 0}))
        assert tasks.export_status(started.task_id, owner="u1") == {"status": "failed", "code": 422, "detail": said("Vendite")}


def test_a_report_s_document_and_its_pages_on_screen_say_it_of_a_report(export_queue):
    for fmt in ("pdf", "svg"):
        started = routes.render(routes.ReportRender(definition=definition(), datasets={"Vendite": SALES}, format=fmt, owner="u1"))
        assert tasks.export_status(started.task_id, owner="u1") == {"status": "failed", "code": 422,
                                                                     "detail": said("Vendite", "a report")}


def test_a_document_on_two_datasets_says_it_without_a_name(export_queue):
    d = definition(kpi("a", "Vendite"), kpi("b", "Magazzino"), names=("Vendite", "Magazzino"))
    started = routes.dashboard_render(routes.DashboardRender(definition=d, datasets={"Vendite": SALES, "Magazzino": STOCK}, owner="u1"))
    assert tasks.export_status(started.task_id, owner="u1")["detail"] == said(None)


def test_a_document_that_failed_otherwise_says_what_it_said(export_queue):
    started = routes.render(routes.ReportRender(definition=definition(), datasets={"Vendite": SALES}, owner="u1"))
    export_queue[started.task_id] = Result(RuntimeError("the chart service is down"), state="FAILURE")
    assert tasks.export_status(started.task_id, owner="u1") == {"status": "failed", "code": 500, "detail": "the chart service is down"}
