"""Request bodies have a ceiling, enforced before they are read: one user saving a
flow of a hundred megabytes could take a gateway replica down while FastAPI read and
parsed it. Uploads stream to the engine and are exempt."""
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from app.core.body_limit import BodyLimitMiddleware


def _client(max_bytes=1000):
    app = FastAPI()
    app.add_middleware(BodyLimitMiddleware, max_bytes=max_bytes)

    @app.post("/flows")
    async def save(request: Request):
        return {"read": len(await request.body())}

    @app.post("/files")
    async def upload(request: Request):
        return {"read": len(await request.body())}

    return TestClient(app)


def test_a_body_within_the_limit_goes_through():
    assert _client().post("/flows", content=b"x" * 1000).json() == {"read": 1000}


def test_a_declared_size_over_the_limit_is_refused_before_reading():
    r = _client().post("/flows", content=b"x" * 1001)
    assert r.status_code == 413 and "too large" in r.json()["detail"]


def test_a_chunked_body_is_counted_as_it_arrives():
    def chunks():
        for _ in range(20):
            yield b"x" * 100  # 2000 bytes, no Content-Length

    r = _client().post("/flows", content=chunks())
    assert r.status_code == 413


def test_uploads_stream_through_without_the_ceiling():
    assert _client().post("/files", content=b"x" * 5000).json() == {"read": 5000}


def test_the_gateway_applies_it():
    from app.main import app

    assert any(m.cls is BodyLimitMiddleware for m in app.user_middleware)
    r = TestClient(app).post("/auth/login", content=b"{" + b" " * (9 * 1024 * 1024) + b"}",
                             headers={"content-type": "application/json"})
    assert r.status_code == 413
