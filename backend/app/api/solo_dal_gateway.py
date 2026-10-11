"""Solo il gateway parla con l'API dell'engine (audit 2026-10-08, A4).

Middleware ASGI puro, non quello di Starlette: non tocca lo streaming degli
upload e degli export, né l'annullamento delle anteprime. Restano aperti i
controlli di salute (sonde di Kubernetes, healthcheck del compose) e /metrics
(lo legge VictoriaMetrics); tutto il resto vuole il token del gateway."""
from __future__ import annotations

import hmac

from starlette.responses import JSONResponse

from app.core.crypto import INTESTAZIONE_INTERNA, token_interno

_NOME = INTESTAZIONE_INTERNA.lower().encode("latin-1")


def aperto(percorso: str) -> bool:
    return percorso in ("/metrics", "/healthcheck") or percorso.startswith("/healthcheck/")


class SoloDalGateway:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or aperto(scope.get("path", "")):
            return await self.app(scope, receive, send)
        presentato = next((v for k, v in scope.get("headers") or [] if k == _NOME), b"").decode("latin-1")
        if not hmac.compare_digest(presentato, token_interno()):
            risposta = JSONResponse({"detail": "the engine API answers the gateway only"}, status_code=401)
            return await risposta(scope, receive, send)
        return await self.app(scope, receive, send)
