"""Client HTTP condiviso verso l'engine interno (data plane).

L'engine non è esposto pubblicamente: solo il gateway lo raggiunge, sulla rete
Docker privata (ENGINE__BASE_URL, es. http://backend:8000).
"""
import httpx

from app.core.config import get_settings

_client: httpx.AsyncClient | None = None


def get_engine_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        s = get_settings()
        # timeout=None sul singolo client: gli upload di file grandi (GB) non devono
        # scadere; i controlli di durata restano sull'engine/worker.
        _client = httpx.AsyncClient(base_url=s.engine.base_url, timeout=None)
    return _client


async def close_engine_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
