"""Engines known but missing in the deployment (chDB left out of the light image, the
external ClickHouse not configured): a clear message, not «unknown»."""
import pytest

import app.engine as engines
from app.engine.exceptions import EngineError


@pytest.fixture
def without_chdb(monkeypatch):
    engines.get_engine.cache_clear()
    monkeypatch.delitem(engines._ENGINES, "chdb", raising=False)
    yield
    engines.get_engine.cache_clear()


def test_a_known_but_missing_engine_has_a_clear_message(without_chdb):
    with pytest.raises(EngineError) as e:
        engines.get_engine("chdb")
    assert str(e.value) == (
        "The chDB (ClickHouse) engine is not available in this deployment. "
        "Choose another engine for this flow, or ask the administrator to enable it."
    )


def test_a_really_unknown_engine_stays_so():
    engines.get_engine.cache_clear()
    with pytest.raises(EngineError, match="unknown engine: 'spark'"):
        engines.get_engine("spark")
    engines.get_engine.cache_clear()
