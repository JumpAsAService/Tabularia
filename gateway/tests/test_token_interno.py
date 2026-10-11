"""Il gateway si presenta all'engine con il token derivato dalla chiave Fernet
(audit 2026-10-08, A4); lo stesso valore è fissato nel test dell'engine."""
from types import SimpleNamespace

import pytest

import app.core.engine_client as ec
from app.core import crypto
from app.core.crypto import INTESTAZIONE_INTERNA, token_interno

CHIAVE = "x" * 43 + "="
ATTESO = "2afa379a593fc05cd4acfa942cb246ca918ab43729da93df0a8d4a443013050f"

pytestmark = pytest.mark.anyio


@pytest.fixture
def chiave_nota(monkeypatch):
    monkeypatch.setattr(crypto, "get_settings", lambda: SimpleNamespace(security=SimpleNamespace(fernet_key=CHIAVE)))
    token_interno.cache_clear()
    yield
    token_interno.cache_clear()


def test_il_token_e_quello_che_si_aspetta_l_engine(chiave_nota):
    assert token_interno() == ATTESO


async def test_ogni_richiesta_all_engine_porta_il_token(chiave_nota, monkeypatch):
    monkeypatch.setattr(ec, "_client", None)
    try:
        assert ec.get_engine_client().headers[INTESTAZIONE_INTERNA] == ATTESO
    finally:
        await ec.close_engine_client()
