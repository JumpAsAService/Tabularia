"""A ceiling on request bodies, checked before they are read.

FastAPI reads a JSON body whole and parses it before any check in a route runs, so
a limit on a field arrives too late: a body of a hundred megabytes had already been
read and decoded, and a few of them at once took a 1 GiB gateway replica down. This
middleware answers 413 as soon as the declared size is too large, and when the size
is not declared (chunked transfer) it counts the bytes as they arrive.

Real bodies are small: the largest flow definition seen is a few tens of kilobytes,
and the limit is the one the proxy already applies to engine requests. Uploads are
exempt: they stream through to the engine.
"""
from __future__ import annotations

from starlette.types import ASGIApp, Message, Receive, Scope, Send

MAX_BODY_BYTES = 8 * 1024 * 1024  # 8 MB, as routes/proxy.py MAX_JSON_BODY_BYTES
EXEMPT = {("POST", "/files")}  # the upload, streamed to the engine


class _TooLarge(Exception):
    pass


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp, max_bytes: int = MAX_BODY_BYTES, exempt: set[tuple[str, str]] = EXEMPT):
        self.app = app
        self.max_bytes = max_bytes
        self.exempt = exempt

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        root = scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root):]  # behind a proxy that keeps the prefix
        if scope["type"] != "http" or (scope["method"], path) in self.exempt:
            await self.app(scope, receive, send)
            return
        declared = dict(scope["headers"]).get(b"content-length")
        if declared is not None:
            try:
                if int(declared) > self.max_bytes:
                    await self._refuse(send)
                    return
            except ValueError:
                pass

        received = 0
        started = False

        async def counted_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    raise _TooLarge()
            return message

        async def tracked_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, counted_receive, tracked_send)
        except _TooLarge:
            if not started:
                await self._refuse(send)

    async def _refuse(self, send: Send) -> None:
        body = b'{"detail":"Request body too large (limit 8 MB)"}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
        await send({"type": "http.response.body", "body": body})
