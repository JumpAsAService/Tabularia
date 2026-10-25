"""Shared HTTP client towards the internal engine (data plane).

The engine is not exposed publicly: only the gateway reaches it, on the private Docker
network (ENGINE__BASE_URL, e.g. http://backend:8000).
"""
import httpx

from app.core.config import get_settings
from app.core.crypto import INTERNAL_HEADER, internal_token

_client: httpx.AsyncClient | None = None

# For the background work's CONTROL calls: launching a task, reading its state,
# revoking it, deleting an object. The engine serves them in milliseconds (it queues or
# reads Celery), and one that does not come back — engine stuck, a half-open TCP
# connection — would hold the run waiting for it, with the heartbeat declaring it
# alive forever. The client stays without a cap for everything else: streaming uploads
# and exports, previews (their cap is on the engine).
CHECK = httpx.Timeout(30.0, connect=5.0)


def get_engine_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        s = get_settings()
        # timeout=None on the single client: the uploads of big files (GB) must not expire; the
        # duration checks stay on the engine/worker.
        # every request introduces itself: the engine answers only whoever carries the token
        _client = httpx.AsyncClient(base_url=s.engine.base_url, timeout=None, headers={INTERNAL_HEADER: internal_token()})
    return _client


async def close_engine_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
