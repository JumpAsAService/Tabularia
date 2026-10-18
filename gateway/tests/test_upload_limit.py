"""A file upload has a ceiling: UPLOAD__MAX_BYTES (1 GiB by default, 0 = none).

Uploads stream through the gateway to the engine and were exempt from every limit:
anyone allowed to upload could send any size, filling the engine's disk (the file is
spooled there before it is converted) and holding a worker on its conversion. Now a
declared size over the ceiling is refused with 413 before the engine is called, and
a body without a declared size (chunked) is counted as it passes and cut at the
ceiling, with the same 413.
"""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.config import Settings, get_settings
from app.db.session import get_session
from app.routes import proxy as proxy_routes
from tests.conftest import make_user, token_di


class Engine:
    def __init__(self):
        self.received: list[int] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/files" and request.method == "POST":
            self.received.append(len(request.read()))
            return httpx.Response(200, json={"dataset_id": "d1", "parquet_key": "datasets/d1.parquet",
                                             "raw_key": "raw/d1.csv", "rows": 1, "columns": []})
        return httpx.Response(404, json={"detail": "not mocked"})


@pytest.fixture
def engine(monkeypatch):
    e = Engine()
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(e.handler), base_url="http://engine"))
    return e


@pytest.fixture
def client(session):
    app = FastAPI()
    app.include_router(proxy_routes.router)
    app.dependency_overrides[get_session] = lambda: session
    admin = make_user(session, email="admin@x.it", is_superuser=True)
    return TestClient(app, headers={"Authorization": f"Bearer {token_di(session, admin)}"})


def _limit(monkeypatch, n: int):
    monkeypatch.setenv("UPLOAD__MAX_BYTES", str(n))
    get_settings.cache_clear()


def test_the_default_ceiling_is_one_gibibyte(monkeypatch):
    monkeypatch.delenv("UPLOAD__MAX_BYTES", raising=False)
    assert Settings().upload.max_bytes == 1024 ** 3


def test_a_file_within_the_ceiling_goes_through(monkeypatch, client, engine):
    _limit(monkeypatch, 1000)
    r = client.post("/files", content=b"x" * 1000, headers={"content-type": "application/octet-stream"})
    assert r.status_code == 200, r.text
    assert engine.received == [1000]


def test_a_declared_size_over_the_ceiling_never_reaches_the_engine(monkeypatch, client, engine):
    _limit(monkeypatch, 1000)
    r = client.post("/files", content=b"x" * 1001, headers={"content-type": "application/octet-stream"})
    assert r.status_code == 413
    assert "UPLOAD__MAX_BYTES" in r.json()["detail"]
    assert engine.received == []


def test_an_undeclared_size_is_counted_and_cut(monkeypatch, client, engine):
    _limit(monkeypatch, 1000)

    def chunks():
        for _ in range(30):
            yield b"x" * 100  # 3000 bytes, chunked: no Content-Length

    r = client.post("/files", content=chunks(), headers={"content-type": "application/octet-stream"})
    assert r.status_code == 413
    assert "too large" in r.json()["detail"]


def test_zero_means_no_ceiling(monkeypatch, client, engine):
    _limit(monkeypatch, 0)
    r = client.post("/files", content=b"x" * 5000, headers={"content-type": "application/octet-stream"})
    assert r.status_code == 200 and engine.received == [5000]


def test_the_message_names_the_limit_and_the_setting(monkeypatch, client, engine):
    _limit(monkeypatch, 3 * 1024 ** 2)
    r = client.post("/files", content=b"x" * (3 * 1024 ** 2 + 1), headers={"content-type": "application/octet-stream"})
    assert r.status_code == 413
    assert r.json()["detail"] == "The file is too large: the limit is 3 MB (UPLOAD__MAX_BYTES)"
