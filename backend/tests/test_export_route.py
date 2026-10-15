"""Exports are written by a worker and left on the storage; the API hands the file,
once, to whoever asked for it and deletes it (review of 2026-10-14: an Excel file
was built whole in memory inside the API process). API callers keep the
synchronous route, with the same contract as before."""
import io
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import polars as pl
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from celery.exceptions import TimeoutError as CeleryTimeoutError
import anyio
from fastapi import HTTPException

from app.api import exports
from app.api.models import ExportRequest, ExportStart
from app.api.routes import tasks
from app.core import regolazioni
from app.core.config import get_settings
from app.engine.exceptions import EngineError
from app.tasks import jobs
from tests.fakes import FakeRedis, FakeStorage

ONLY_IT = [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "IT"}}]


class Storage(FakeStorage):
    """FakeStorage plus what the export path uses: boto3's get_object, a listing
    with dates, a batch delete."""

    def __init__(self):
        super().__init__()
        self.client = self
        self.written: dict[tuple[str, str], datetime] = {}

    def upload_file(self, local_path, bucket, key):
        super().upload_file(local_path, bucket, key)
        self.written[(bucket, key)] = datetime.now(timezone.utc)

    def download_file(self, bucket, key, local_path):
        if (bucket, key) not in self.blobs:  # as S3 says it, which the engine reads as a missing source
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        super().download_file(bucket, key, local_path)

    def get_object(self, Bucket, Key):
        data = self.blobs.get((Bucket, Key))
        if data is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": StreamingBody(io.BytesIO(data), len(data)), "ContentLength": len(data)}

    def list_objects(self, bucket, prefix=""):
        return [{"Key": k, "LastModified": self.written[(b, k)]} for (b, k) in self.blobs if b == bucket and k.startswith(prefix)]

    def delete_objects(self, bucket, keys):
        for k in keys:
            self.blobs.pop((bucket, k), None)

    def keys(self, prefix):
        return [k for (_, k) in self.blobs if k.startswith(prefix)]


@pytest.fixture
def storage():
    return Storage()


class Result:
    """What the routes read of a Celery task."""

    def __init__(self, result, state="SUCCESS"):
        self.result = self.info = result
        self.state = state
        self.forgotten = self.revoked = False

    def get(self, timeout=None):
        if self.state == "PENDING":
            raise CeleryTimeoutError()
        return self.result

    def forget(self):
        self.forgotten = True

    def revoke(self, terminate=False):
        self.revoked = True


@pytest.fixture
def worker(monkeypatch, engine, storage):
    """A worker that runs each export as soon as it is sent, on the test storage,
    and the result store the routes read."""
    results: dict[str, Result] = {}
    monkeypatch.setattr(jobs, "get_engine", lambda name="polars": engine)
    monkeypatch.setattr(jobs, "get_storage_service", lambda: storage)
    monkeypatch.setattr(exports, "get_storage_service", lambda: storage)
    monkeypatch.setattr(exports, "_client", FakeRedis())

    def send_task(name, kwargs, task_id):
        assert name == "app.tasks.jobs.export_task"
        results[task_id] = Result(jobs.export_task.apply(kwargs=kwargs, task_id=task_id).get())
        return results[task_id]

    def lookup(task_id, app=None):
        return results.get(task_id) or Result(None, state="PENDING")

    monkeypatch.setattr(tasks.celery_app, "send_task", send_task)
    monkeypatch.setattr(tasks, "AsyncResult", lookup)
    monkeypatch.setattr(exports, "AsyncResult", lookup)
    return results


def _play(response) -> tuple[dict, bytes]:
    """Sends a response as the server would, background task included: the
    headers and the body that reach the browser."""
    sent = []

    async def receive():
        await anyio.sleep_forever()

    async def send(message):
        sent.append(message)

    anyio.run(response, {"type": "http", "method": "GET", "headers": []}, receive, send)
    headers = {k.decode(): v.decode() for k, v in sent[0]["headers"]}
    return headers, b"".join(m.get("body", b"") for m in sent[1:])


def _status_code(call, *args, **kw) -> int:
    with pytest.raises(HTTPException) as e:
        call(*args, **kw)
    return e.value.status_code


def _body(source, **extra):
    return {"bucket": source.bucket, "input_key": source.key, "operations": ONLY_IT, "format": "csv", **extra}


def _start(body) -> str:
    return tasks.start_export(ExportStart(**body)).task_id


def test_the_requester_follows_the_export_and_takes_the_file_once(worker, storage, vendite):
    task_id = _start(_body(vendite, filename="sales.csv", owner="u1"))

    [key] = storage.keys("exports/")
    size = len(next(data for (_, k), data in storage.blobs.items() if k == key))
    assert tasks.export_status(task_id, owner="u1") == {"status": "ready", "filename": "sales.csv", "size": size}
    for route in (tasks.export_status, tasks.export_file):
        assert _status_code(route, task_id, owner="u2") == 404
        assert _status_code(route, task_id, owner=None) == 404

    headers, body = _play(tasks.export_file(task_id, owner="u1"))
    assert headers["content-type"].startswith("text/csv")
    assert headers["content-disposition"] == 'attachment; filename="sales.csv"'
    assert headers["content-length"] == str(size)
    assert set(pl.read_csv(io.BytesIO(body))["paese"]) == {"IT"}

    # sent: the file, the result and the owner are gone; a second download finds nothing
    assert storage.keys("exports/") == [] and worker[task_id].forgotten
    assert _status_code(tasks.export_file, task_id, owner="u1") == 404


def test_an_excel_file_comes_through_whole(worker, vendite):
    task_id = _start(_body(vendite, operations=[], format="xlsx", owner="u1"))
    headers, body = _play(tasks.export_file(task_id, owner="u1"))
    assert headers["content-type"].startswith("application/vnd.openxmlformats")
    assert headers["content-disposition"] == 'attachment; filename="export.xlsx"'
    assert pl.read_excel(io.BytesIO(body)).height == 6


def test_a_name_outside_ascii_is_encoded_for_the_browser(worker, vendite):
    task_id = _start(_body(vendite, filename="vendite più.csv", owner="u1"))
    headers, _ = _play(tasks.export_file(task_id, owner="u1"))
    assert headers["content-disposition"] == "attachment; filename*=utf-8''vendite%20pi%C3%B9.csv"


def test_a_failed_export_says_why_and_leaves_no_file(worker, storage, vendite):
    missing = _start(_body(vendite, input_key="datasets/gone.parquet", owner="u1"))
    assert tasks.export_status(missing, owner="u1")["code"] == 404

    regolazioni.scrivi({"export.max_rows": 1})
    over = _start(_body(vendite, owner="u1"))
    status = tasks.export_status(over, owner="u1")
    assert status["status"] == "failed" and status["code"] == 400 and "CSV" in status["detail"]
    assert _status_code(tasks.export_file, over, owner="u1") == 409
    assert storage.keys("exports/") == []


def test_a_running_export_has_no_file_yet(worker, monkeypatch):
    monkeypatch.setattr(tasks.celery_app, "send_task", lambda name, kwargs, task_id: None)
    task_id = _start({"bucket": "b", "input_key": "k", "operations": [], "owner": "u1"})
    assert tasks.export_status(task_id, owner="u1") == {"status": "running"}
    assert _status_code(tasks.export_file, task_id, owner="u1") == 409


def test_a_worker_that_died_is_a_failure_with_its_reason(worker, monkeypatch):
    monkeypatch.setattr(tasks.celery_app, "send_task", lambda name, kwargs, task_id: None)
    task_id = _start({"bucket": "b", "input_key": "k", "operations": [], "owner": "u1"})
    worker[task_id] = Result(RuntimeError("Worker exited prematurely: signal 9 (SIGKILL)"), state="FAILURE")
    assert tasks.export_status(task_id, owner="u1") == {
        "status": "failed", "code": 500, "detail": "Worker exited prematurely: signal 9 (SIGKILL)"}


def test_api_callers_still_get_the_file_in_the_response(worker, storage, vendite):
    headers, body = _play(tasks.export_flow(ExportRequest(**_body(vendite, filename="it.csv"))))
    assert headers["content-disposition"] == 'attachment; filename="it.csv"'
    assert pl.read_csv(io.BytesIO(body)).height == 2
    assert storage.keys("exports/") == [] and all(res.forgotten for res in worker.values())


def test_the_synchronous_route_gives_the_reason_of_a_refusal(worker, vendite):
    with pytest.raises(HTTPException) as e:
        tasks.export_flow(ExportRequest(**_body(vendite, input_key="datasets/gone.parquet")))
    assert e.value.status_code == 404 and "gone" in e.value.detail
    assert all(res.forgotten for res in worker.values())


def test_a_synchronous_export_no_worker_finishes_is_revoked(monkeypatch):
    pending = Result(None, state="PENDING")
    monkeypatch.setattr(tasks.celery_app, "send_task", lambda name, kwargs, task_id: pending)
    assert _status_code(tasks.export_flow, ExportRequest(bucket="b", input_key="k")) == 504
    assert pending.revoked and pending.forgotten


def test_with_another_engine_the_snapshot_goes_once_the_file_is_written(worker, storage, vendite):
    _, body = _play(tasks.export_flow(ExportRequest(**_body(vendite, engine="duckdb"))))
    assert pl.read_csv(io.BytesIO(body)).height == 2
    assert storage.keys("tmp/") == [] and storage.keys("exports/") == []


def test_only_the_optional_arguments_that_are_set_travel_to_the_worker(monkeypatch):
    sent = {}
    monkeypatch.setattr(tasks.celery_app, "send_task", lambda name, kwargs, task_id: sent.update(kwargs))
    monkeypatch.setattr(exports, "_client", FakeRedis())
    _start({"bucket": "b", "input_key": "k", "operations": [], "owner": "u1"})
    assert set(sent) == {"bucket", "input_key", "operations", "format"}


@pytest.mark.parametrize("set_cap, passed", [(7, 7), (0, None)])
def test_the_worker_passes_the_row_cap_of_the_settings(monkeypatch, storage, set_cap, passed):
    seen = {}

    class Engine:
        def export(self, **kw):
            seen.update(kw)
            with open(kw["out_path"], "w") as f:
                f.write("a\n1\n")

    monkeypatch.setattr(jobs, "get_engine", lambda name: Engine())
    monkeypatch.setattr(jobs, "get_storage_service", lambda: storage)
    monkeypatch.setattr(jobs, "get_settings", lambda: SimpleNamespace(
        export=SimpleNamespace(max_rows=set_cap), storage=SimpleNamespace(bucket="b")))
    assert jobs.export_task.apply(kwargs={"bucket": "b", "input_key": "k", "operations": []}).get()["ok"]
    assert seen["max_rows"] == passed and seen["fmt"] == "csv"
    assert not os.path.exists(seen["out_path"])  # the local copy goes once it is on the storage


def test_over_the_cap_the_worker_answers_with_the_reason_and_no_file(monkeypatch, storage):
    seen = {}

    class Engine:
        def export(self, **kw):
            seen.update(kw)
            raise EngineError("the result has more than 7 rows: over the cap of the CSV export.")

    monkeypatch.setattr(jobs, "get_engine", lambda name: Engine())
    monkeypatch.setattr(jobs, "get_storage_service", lambda: storage)
    payload = jobs.export_task.apply(kwargs={"bucket": "b", "input_key": "k", "operations": []}).get()
    assert payload == {"ok": False, "error": "bad_request", "detail": "the result has more than 7 rows: over the cap of the CSV export."}
    assert not os.path.exists(seen["out_path"]) and storage.keys("exports/") == []


def test_the_sweep_takes_only_the_export_files_left_behind(monkeypatch, storage, tmp_path):
    bucket = get_settings().storage.bucket
    f = tmp_path / "x"
    f.write_text("x")
    for key in ("exports/old.csv", "exports/new.csv", "datasets/old.parquet"):
        storage.upload_file(str(f), bucket, key)
    two_hours_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    storage.written[(bucket, "exports/old.csv")] = storage.written[(bucket, "datasets/old.parquet")] = two_hours_ago
    monkeypatch.setattr(jobs, "get_storage_service", lambda: storage)

    assert jobs.sweep_exports_task() == {"removed": 1}
    assert sorted(k for (_, k) in storage.blobs) == ["datasets/old.parquet", "exports/new.csv"]
