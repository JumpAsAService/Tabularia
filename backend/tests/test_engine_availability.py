"""Engine noti ma assenti nel deployment (chDB escluso dall'immagine leggera,
ClickHouse esterno non configurato): messaggio chiaro, non «sconosciuto»."""
import pytest

import app.engine as engines
from app.engine.exceptions import EngineError


@pytest.fixture
def senza_chdb(monkeypatch):
    engines.get_engine.cache_clear()
    monkeypatch.delitem(engines._ENGINES, "chdb", raising=False)
    yield
    engines.get_engine.cache_clear()


def test_engine_noto_ma_assente_ha_un_messaggio_chiaro(senza_chdb):
    with pytest.raises(EngineError) as e:
        engines.get_engine("chdb")
    assert str(e.value) == (
        "The chDB (ClickHouse) engine is not available in this deployment. "
        "Choose another engine for this flow, or ask the administrator to enable it."
    )


def test_engine_davvero_sconosciuto_resta_tale():
    engines.get_engine.cache_clear()
    with pytest.raises(EngineError, match="engine sconosciuto: 'spark'"):
        engines.get_engine("spark")
    engines.get_engine.cache_clear()
