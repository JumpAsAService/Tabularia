"""The editor's question "which nodes are in the step cache" carries the same chains
as a preview, so the gateway checks them the same way before the engine sees them."""
import json

import httpx
import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.core.config import get_settings
from app.models.permission import Capability
from app.routes import proxy
from tests.conftest import make_datasource, make_permission, make_project, make_user

pytestmark = pytest.mark.anyio


def _request(body) -> Request:
    raw = json.dumps(body).encode()
    scope = {"type": "http", "method": "POST", "path": "/tasks/cache-status", "query_string": b"",
             "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())],
             "client": ("203.0.113.9", 4242)}

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    return Request(scope, receive)


@pytest.fixture
def engine(session, monkeypatch):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, session.in_transaction()))
        return httpx.Response(200, json={"cached": ["n1"], "too_big": []})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://engine")
    monkeypatch.setattr("app.core.engine_client._client", client)
    return seen


@pytest.fixture
def scene(session):
    bucket = get_settings().engine.bucket
    sales, finance = make_project(session, name="Sales"), make_project(session, name="Finance")
    mario = make_user(session, email="mario@x.local")
    make_permission(session, user_id=mario.id, project_id=sales.id, capability=Capability.VIEW)
    orders = make_datasource(session, name="orders", project_id=sales.id, bucket=bucket, key="datasets/orders.parquet")
    salaries = make_datasource(session, name="salaries", project_id=finance.id, bucket=bucket, key="datasets/salaries.parquet")
    return mario, orders, salaries


def _body(*sources):
    return {"engine": "duckdb", "chains": [
        {"node_id": f"n{i}", "bucket": s.bucket, "input_key": s.key, "operations": [{"type": "limit", "params": {"n": 1}}]}
        for i, s in enumerate(sources, start=1)]}


async def test_the_question_reaches_the_engine_without_holding_the_database(session, engine, scene):
    mario, orders, _ = scene
    await proxy.cache_status(_request(_body(orders)), user=mario, session=session)
    assert engine == [("/tasks/cache-status", False)]


async def test_a_chain_on_a_source_the_user_cannot_read_is_refused(session, engine, scene):
    mario, orders, salaries = scene
    with pytest.raises(HTTPException) as e:
        await proxy.cache_status(_request(_body(orders, salaries)), user=mario, session=session)
    assert e.value.status_code == 403 and engine == []


async def test_a_source_outside_the_engine_s_bucket_is_refused(session, engine, scene):
    mario, orders, _ = scene
    body = _body(orders)
    body["chains"][0]["bucket"] = "someone-elses"
    with pytest.raises(HTTPException) as e:
        await proxy.cache_status(_request(body), user=mario, session=session)
    assert e.value.status_code == 403 and engine == []
