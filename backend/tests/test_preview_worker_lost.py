"""A preview whose worker process died while it worked: said in words, not a 500 «Internal Server
Error» as text (bug hunt on kind, 2026-11-03). The kernel's OOM killer at the worker's memory limit
is a 422 saying what to change; any other loss a 503, to try again."""
import pytest
from celery.exceptions import WorkerLostError
from fastapi import HTTPException

from app.api.models import PreviewRequest
from app.api.routes import db as db_routes
from app.api.routes import tasks
from app.ingest.db_source import DbConnectionSpec

KILLED = WorkerLostError("Worker exited prematurely: signal 9 (SIGKILL) Job: 0.")
GONE = WorkerLostError("Worker exited prematurely: exitcode 0 Job: 0.")


def _lost(error):
    def send(task, kwargs, slot, seconds=None, watch_worker=False):
        raise error
    return send


@pytest.mark.parametrize("error, status, words", [(KILLED, 422, "ran out of the worker's memory: give the source a "
                                                   "development sample"), (GONE, 503, "was lost: try again")])
def test_a_flow_preview(monkeypatch, error, status, words):
    monkeypatch.setattr(tasks, "on_preview_worker", _lost(error))
    with pytest.raises(HTTPException) as e:
        tasks.preview_flow(PreviewRequest(bucket="b", input_key="datasets/x.parquet"))
    assert e.value.status_code == status and words in e.value.detail


@pytest.mark.parametrize("error, status, words", [(KILLED, 422, "aggregate or filter it in the SQL"),
                                                  (GONE, 503, "was lost: try again")])
def test_a_query_on_a_connection(monkeypatch, error, status, words):
    monkeypatch.setattr(tasks, "on_preview_worker", _lost(error))
    spec = DbConnectionSpec(db_type="postgresql", host="h", port=5432, database="d", username="u", password="p")
    with pytest.raises(HTTPException) as e:
        db_routes.query(db_routes.DbQueryRequest(connection=spec, sql="select 1"))
    assert e.value.status_code == status and words in e.value.detail
