"""Only the gateway talks to the engine's API (audit 2026-10-08, A4).

A pure ASGI middleware, not Starlette's: it does not touch the streaming of uploads and
exports, nor the cancelling of previews. The health checks stay open (Kubernetes probes,
the compose healthcheck) and /metrics (VictoriaMetrics reads it); everything else wants
the gateway's token."""
from __future__ import annotations

import hmac

from starlette.responses import JSONResponse

from app.core.crypto import INTERNAL_HEADER, internal_token

_NOME = INTERNAL_HEADER.lower().encode("latin-1")


def open_(path_: str) -> bool:
    return path_ in ("/metrics", "/healthcheck") or path_.startswith("/healthcheck/")


class GatewayOnly:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or open_(scope.get("path", "")):
            return await self.app(scope, receive, send)
        presented = next((v for k, v in scope.get("headers") or [] if k == _NOME), b"").decode("latin-1")
        if not hmac.compare_digest(presented, internal_token()):
            answer = JSONResponse({"detail": "the engine API answers the gateway only"}, status_code=401)
            return await answer(scope, receive, send)
        return await self.app(scope, receive, send)
