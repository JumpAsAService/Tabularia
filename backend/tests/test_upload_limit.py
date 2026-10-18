"""The engine refuses an upload over UPLOAD__MAX_BYTES too: the gateway is the first
barrier (it answers 413 before calling the engine), this is the second, for a body
that reaches the engine some other way. Starlette spools a multipart file to disk
before the route runs, so the check sits in front of it, on the raw body."""
import asyncio
import json

from app.api.upload_limit import UploadLimit
from app.core.config import Settings


async def _inner(scope, receive, send):
    """A route that reads the whole body, as the multipart parser does."""
    total = 0
    while True:
        message = await receive()
        total += len(message.get("body", b""))
        if not message.get("more_body"):
            break
    body = json.dumps({"read": total}).encode()
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": body})


def _post(max_bytes, path, chunks, declare=True):
    size = sum(len(c) for c in chunks)
    headers = [(b"content-length", str(size).encode())] if declare else []
    scope = {"type": "http", "method": "POST", "path": path, "headers": headers}
    pending = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]
    sent = []

    async def receive():
        return pending.pop(0)

    async def send(message):
        sent.append(message)

    asyncio.run(UploadLimit(_inner, max_bytes=lambda: max_bytes)(scope, receive, send))
    status = sent[0]["status"]
    return status, json.loads(sent[1]["body"])


class _R:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class _Client:
    def __init__(self, max_bytes):
        self.max_bytes = max_bytes

    def post(self, path, content):
        if isinstance(content, bytes):
            return _R(*_post(self.max_bytes, path, [content]))
        return _R(*_post(self.max_bytes, path, list(content), declare=False))


def _client(max_bytes):
    return _Client(max_bytes)


def test_the_default_is_one_gibibyte(monkeypatch):
    monkeypatch.delenv("UPLOAD__MAX_BYTES", raising=False)
    assert Settings().upload.max_bytes == 1024 ** 3


def test_within_the_ceiling_it_passes():
    assert _client(100).post("/files", content=b"x" * 100).json() == {"read": 100}


def test_a_declared_size_over_the_ceiling_is_refused():
    r = _client(100).post("/files", content=b"x" * 101)
    assert r.status_code == 413 and "UPLOAD__MAX_BYTES" in r.json()["detail"]


def test_a_chunked_body_is_counted():
    r = _client(100).post("/files", content=(b"x" * 30 for _ in range(10)))
    assert r.status_code == 413


def test_other_routes_are_not_concerned():
    assert _client(100).post("/tasks/preview", content=b"x" * 500).json() == {"read": 500}


def test_zero_means_no_ceiling():
    assert _client(0).post("/files", content=b"x" * 500).json() == {"read": 500}


def test_the_engine_api_applies_it():
    from app.api.main import app

    assert any(m.cls is UploadLimit for m in app.user_middleware)
