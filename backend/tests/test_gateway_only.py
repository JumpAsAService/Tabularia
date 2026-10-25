"""The engine's API answers only the gateway (audit 2026-10-08, A4): the network closes it,
and the token derived from the Fernet key is the second lock. The middleware is tested as
an ASGI application, without an HTTP client."""
import asyncio
from types import SimpleNamespace

import pytest

from app.api.main import app
from app.api.gateway_only import GatewayOnly
from app.core import crypto
from app.core.crypto import INTERNAL_HEADER, internal_token

# the same value is pinned in the gateway's test: the two services must compute it alike
KEY = "x" * 43 + "="
EXPECTED = "2afa379a593fc05cd4acfa942cb246ca918ab43729da93df0a8d4a443013050f"


@pytest.fixture
def known_key(monkeypatch):
    monkeypatch.setattr(crypto, "get_settings", lambda: SimpleNamespace(security=SimpleNamespace(fernet_key=KEY)))
    internal_token.cache_clear()
    yield
    internal_token.cache_clear()


def _state(path_: str, headers: dict) -> int:
    """The status the guard answers with, in front of an app that always says 200."""
    async def behind(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    events = []

    async def receive():
        return {"type": "http.request", "body": b""}

    async def send(message):
        events.append(message)

    scope = {"type": "http", "method": "GET", "path": path_,
             "headers": [(k.lower().encode(), v.encode()) for k, v in headers.items()]}
    asyncio.run(GatewayOnly(behind)(scope, receive, send))
    return events[0]["status"]


def test_the_token_is_the_one_the_gateway_computes(known_key):
    assert internal_token() == EXPECTED


@pytest.mark.parametrize("headers", [{}, {INTERNAL_HEADER: "sbagliato"}, {INTERNAL_HEADER: EXPECTED[:-1]}])
@pytest.mark.parametrize("path_", ["/engines", "/tasks/preview", "/files/object", "/docs", "/"])
def test_without_the_right_token_it_is_a_401(known_key, path_, headers):
    assert _state(path_, headers) == 401


def test_with_the_token_it_passes(known_key):
    assert _state("/engines", {INTERNAL_HEADER: EXPECTED}) == 200


@pytest.mark.parametrize("path_", ["/metrics", "/healthcheck", "/healthcheck/", "/healthcheck/redis"])
def test_health_and_metrics_stay_open(known_key, path_):
    assert _state(path_, {}) == 200


def test_the_guard_is_mounted_on_the_app():
    assert any(m.cls is GatewayOnly for m in app.user_middleware)
