"""The ceiling of a file upload (POST /files), checked on the raw body.

The gateway refuses an upload over UPLOAD__MAX_BYTES before calling the engine; this
is the second barrier. It has to sit in front of the route: Starlette writes a
multipart file to a temporary file on disk before the route sees it, so a check in
the route would come after the disk had filled.
"""
from __future__ import annotations

from typing import Callable

from starlette.types import ASGIApp, Message, Receive, Scope, Send


class _TooLarge(Exception):
    pass


def _describe(n: int) -> str:
    for unit, size in (("GB", 1024 ** 3), ("MB", 1024 ** 2), ("KB", 1024)):
        if n >= size and n % size == 0:
            return f"{n // size} {unit}"
    return f"{n} bytes"


def _from_settings() -> int:
    from app.core.config import get_settings

    return get_settings().upload.max_bytes


class UploadLimit:
    def __init__(self, app: ASGIApp, max_bytes: Callable[[], int] = _from_settings):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST" or scope.get("path") != "/files":
            await self.app(scope, receive, send)
            return
        limit = self.max_bytes()
        if not limit:
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length", b"")
        if declared.isdigit() and int(declared) > limit:
            await self._refuse(send, limit)
            return

        received = 0
        started = False

        async def counted() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise _TooLarge()
            return message

        async def tracked(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted, tracked)
        except _TooLarge:
            if not started:
                await self._refuse(send, limit)

    async def _refuse(self, send: Send, limit: int) -> None:
        import json

        body = json.dumps({"detail": f"The file is too large: the limit is {_describe(limit)} (UPLOAD__MAX_BYTES)"}).encode()
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
