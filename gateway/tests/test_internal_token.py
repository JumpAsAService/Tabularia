"""The gateway introduces itself to the engine with the token derived from the Fernet key
(audit 2026-10-08, A4); the engine's test pins the same value."""
from types import SimpleNamespace

import pytest

import app.core.engine_client as ec
from app.core import crypto
from app.core.crypto import INTERNAL_HEADER, internal_token

KEY = "x" * 43 + "="
EXPECTED = "2afa379a593fc05cd4acfa942cb246ca918ab43729da93df0a8d4a443013050f"

pytestmark = pytest.mark.anyio


@pytest.fixture
def known_key(monkeypatch):
    monkeypatch.setattr(crypto, "get_settings", lambda: SimpleNamespace(security=SimpleNamespace(fernet_key=KEY)))
    internal_token.cache_clear()
    yield
    internal_token.cache_clear()


def test_the_token_is_the_one_the_engine_expects(known_key):
    assert internal_token() == EXPECTED


async def test_every_request_to_the_engine_carries_the_token(known_key, monkeypatch):
    monkeypatch.setattr(ec, "_client", None)
    try:
        assert ec.get_engine_client().headers[INTERNAL_HEADER] == EXPECTED
    finally:
        await ec.close_engine_client()
