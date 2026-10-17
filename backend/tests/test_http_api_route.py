"""The engine's side of the «API call» datasource reached by the gateway:
POST /http-api/inspect (test, validate, try — synchronous) and POST /http-api/ingest
(queues the task), plus the progress the task reports through /tasks/{id}."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.api.routes import http_api as route
from app.api.routes import tasks as rotta_tasks
from app.core.crypto import encrypt_secret
from tests.fake_api import FakeApi

SECRET = "route-S3CRET-99"


def _conn(base: str, **kw) -> dict:
    return {"base_url": base, "auth": "api_key", "key_name": "k", "key_in": "query",
            "secret_encrypted": encrypt_secret(SECRET), **kw}


def test_validate_answers_the_calls_and_refuses_with_a_400():
    ok = route.inspect(route.ApiInspectRequest(connection=_conn("https://api.example.com"), action="validate",
                                               source={"path": "/q/{s}", "variables": [{"name": "s", "values": ["a", "b"]}]}))
    assert ok["calls"] == 2
    with pytest.raises(HTTPException) as e:
        route.inspect(route.ApiInspectRequest(connection=_conn("https://api.example.com"), action="validate",
                                              source={"path": "/q/{tomorrow}"}))
    assert e.value.status_code == 400 and e.value.detail.startswith("API: ") and "tomorrow" in e.value.detail


def test_test_and_try_against_a_server():
    with FakeApi() as srv:
        srv.route("GET", "/v1", lambda req: (200, {}, {"ok": True}))
        srv.route("GET", "/v1/q", lambda req: (200, {}, [{"id": 1}]))
        t = route.inspect(route.ApiInspectRequest(connection=_conn(srv.url("/v1")), action="test"))
        assert t["ok"] and SECRET not in str(t)
        out = route.inspect(route.ApiInspectRequest(connection=_conn(srv.url("/v1")), action="try", source={"path": "/q"}))
        assert out["ok"] and out["rows"] == [{"id": 1}] and SECRET not in str(out)


def test_an_http_error_of_the_test_is_a_400_without_the_secret():
    with FakeApi() as srv:
        srv.route("GET", "/v1", lambda req: (401, {}, {"error": f"bad key {SECRET}"}))
        with pytest.raises(HTTPException) as e:
            route.inspect(route.ApiInspectRequest(connection=_conn(srv.url("/v1")), action="test"))
    assert e.value.status_code == 400 and "401" in e.value.detail and SECRET not in e.value.detail


def test_try_needs_a_call():
    with pytest.raises(HTTPException) as e:
        route.inspect(route.ApiInspectRequest(connection=_conn("https://api.example.com"), action="try"))
    assert e.value.status_code == 422


class _Spy:
    def __init__(self):
        self.kwargs = None

    def delay(self, **kwargs):
        self.kwargs = kwargs
        return type("R", (), {"id": "task-x"})()


def test_ingest_queues_the_task_and_sends_only_what_is_set(monkeypatch):
    from app.tasks import jobs

    spy = _Spy()
    monkeypatch.setattr(jobs, "ingest_api_task", spy)
    r = route.ingest(route.ApiIngestRequest(connection=_conn("https://api.example.com"), source={"path": "/q"},
                                            bucket="b", output_key="datasets/1/x.parquet"))
    assert r.task_id == "task-x"
    assert set(spy.kwargs) == {"connection", "source", "bucket", "output_key"}
    route.ingest(route.ApiIngestRequest(connection=_conn("https://api.example.com"), source={"path": "/q"},
                                        bucket="b", output_key="k", last_refresh="2026-10-16T06:00:00",
                                        previous={"bucket": "b", "key": "old"}, contract={"x": 1}))
    assert spy.kwargs["last_refresh"] == "2026-10-16T06:00:00" and spy.kwargs["previous"] == {"bucket": "b", "key": "old"}


def test_the_task_reads_the_api_and_reports_calls(monkeypatch):
    from app.tasks import jobs
    from tests.fakes import FakeStorage

    st = FakeStorage()
    monkeypatch.setattr(jobs, "get_storage_service", lambda: st)
    with FakeApi() as srv:
        srv.route("GET", "/v1/q", lambda req: (200, {}, [{"id": 1}, {"id": 2}]))
        res = jobs.ingest_api_task.run(connection=_conn(srv.url("/v1")), source={"path": "/q"}, bucket="b",
                                       output_key="datasets/1/x.parquet")
    assert res["rows_written"] == 2 and ("b", "datasets/1/x.parquet") in st.blobs


def test_the_engine_returns_the_progress_of_calls(monkeypatch):
    class _Result:
        def __init__(self, task_id, app=None):
            self.status, self.info = "STARTED", {"phase": "calls", "call": 2, "calls": 5, "pages": 7, "rows": 700, "pid": 1}

    monkeypatch.setattr(rotta_tasks, "AsyncResult", _Result)
    assert rotta_tasks.get_task_status("t").progress == {"phase": "calls", "call": 2, "calls": 5, "pages": 7, "rows": 700}
