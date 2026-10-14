"""The browser's export: started, followed, its file taken once. The gateway checks
the reads and audits the download as for `/tasks/export`, and it alone names who
asked: the engine shows an export's state and file only to that name."""
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlmodel import select
from starlette.requests import Request

from app.models import AuditLog
from app.models.permission import Capability
from app.routes import proxy
from app.services import api_tokens
from tests.conftest import make_datasource, make_permission, make_project, make_user

pytestmark = pytest.mark.anyio

TASK = "0f8c2a5e-3b1d-4c7a-9e2f-1a2b3c4d5e6f"


def _request(body=None, method="POST", query=b"") -> Request:
    raw = json.dumps(body).encode() if body is not None else b""
    scope = {
        "type": "http", "method": method, "path": "/x", "query_string": query,
        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())],
        "client": ("203.0.113.9", 4242),
    }

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(scope, receive)


@pytest.fixture
def engine(session, monkeypatch):
    """A fake engine that keeps what it was asked, and whether the gateway still
    held a database transaction at that moment."""
    calls: list[SimpleNamespace] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(SimpleNamespace(path=request.url.path, query=dict(request.url.params),
                                     body=json.loads(request.content) if request.content else None,
                                     in_transaction=session.in_transaction()))
        return httpx.Response(200, json={"task_id": TASK, "status": "submitted", "message": ""})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://engine")
    monkeypatch.setattr("app.core.engine_client._client", client)
    return calls


@pytest.fixture
def scene(session):
    from app.core.config import get_settings

    sales = make_project(session, name="Sales")
    finance = make_project(session, name="Finance")
    mario = make_user(session, email="mario@x.local")
    make_permission(session, user_id=mario.id, project_id=sales.id, capability=Capability.VIEW)
    bucket = get_settings().engine.bucket
    orders = make_datasource(session, name="orders", project_id=sales.id, bucket=bucket, key="datasets/orders.parquet")
    salaries = make_datasource(session, name="salaries", project_id=finance.id, bucket=bucket, key="datasets/salaries.parquet")
    return SimpleNamespace(mario=mario, orders=orders, salaries=salaries)


def _body(ds, **extra):
    return {"bucket": ds.bucket, "input_key": ds.key, "operations": [], "format": "xlsx", "filename": "orders.xlsx", **extra}


async def test_the_gateway_names_the_requester_and_audits_the_download(session, engine, scene):
    await proxy.start_export(_request(_body(scene.orders, owner="user:999")), user=scene.mario, session=session)
    [call] = engine
    assert call.path == "/tasks/exports" and call.body["owner"] == f"user:{scene.mario.id}"
    assert call.body["input_key"] == "datasets/orders.parquet" and not call.in_transaction
    [entry] = session.exec(select(AuditLog).where(AuditLog.action == "export.download")).all()
    assert entry.actor_id == scene.mario.id and entry.target_label == "orders.xlsx"


async def test_a_source_the_requester_cannot_read_is_refused_before_the_engine(session, engine, scene):
    with pytest.raises(HTTPException) as e:
        await proxy.start_export(_request(_body(scene.salaries)), user=scene.mario, session=session)
    assert e.value.status_code == 403 and engine == []


async def test_state_and_file_are_asked_in_the_requester_s_name_only(session, engine, scene):
    forged = b"owner=user:999"
    await proxy.export_status(_request(method="GET", query=forged), task_id=TASK, user=scene.mario)
    await proxy.export_file(_request(method="GET", query=forged), task_id=TASK, user=scene.mario)
    assert [(c.path, c.query) for c in engine] == [
        (f"/tasks/exports/{TASK}", {"owner": f"user:{scene.mario.id}"}),
        (f"/tasks/exports/{TASK}/file", {"owner": f"user:{scene.mario.id}"}),
    ]


@pytest.mark.parametrize("task_id", ["..", "x", TASK + "0", "../../tasks/queue"])
async def test_an_id_that_is_not_a_task_s_does_not_reach_the_engine(session, engine, scene, task_id):
    for route in (proxy.export_status, proxy.export_file):
        with pytest.raises(HTTPException) as e:
            await route(_request(method="GET"), task_id=task_id, user=scene.mario)
        assert e.value.status_code == 404
    assert engine == []


def test_a_read_only_api_token_may_start_an_export():
    token = SimpleNamespace(scope="read")
    request = SimpleNamespace(method="POST", scope={"route": SimpleNamespace(path="/tasks/exports")}, headers={})
    api_tokens.controlla_livello(token, request)
