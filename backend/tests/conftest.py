"""Fixture condivise: le "preparazioni" che pytest inietta nei test.

Una fixture è una funzione il cui RISULTATO viene passato come argomento ai
test che ne dichiarano il nome. Esempio: un test con parametro `engine`
riceve un PolarsEngine già collegato a storage e cache finti.
"""
from __future__ import annotations

import base64
import os

# Connection secrets are encrypted with SECURITY__FERNET_KEY: a random key when none is
# set, so the suite runs on a fresh clone (and in CI) without configuring anything.
os.environ.setdefault("SECURITY__FERNET_KEY", base64.urlsafe_b64encode(os.urandom(32)).decode())

import polars as pl
import pytest

from app.engine.base import DataSource
from app.engine.cache import StepCache
from app.engine.polars_engine import PolarsEngine
from tests.fakes import FakeRedis, FakeStorage

# Il bucket dei test e' finto (FakeStorage) tranne nelle suite LIVE, dove i
# parquet vanno sullo storage vero: allora e' quello configurato.
BUCKET = os.getenv("TEST_BUCKET") or "data-prep"


@pytest.fixture(autouse=True)
def regolazioni_isolate(monkeypatch):
    """Ogni test parte senza regolazioni dal pannello, su un Valkey finto tutto suo:
    quelle messe sul Valkey di sviluppo cambierebbero i risultati (un row group o
    un tetto diversi). Chi le prova le scrive qui (`panel_settings.write`)."""
    from app.core import panel_settings

    finto = FakeRedis()
    monkeypatch.setattr(panel_settings, "_client", lambda: finto)
    panel_settings.forget()
    yield finto
    panel_settings.forget()


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def fredis() -> FakeRedis:
    return FakeRedis()


@pytest.fixture
def cache(storage, fredis) -> StepCache:
    return StepCache(storage, redis_client=fredis)


@pytest.fixture
def engine(storage, cache) -> PolarsEngine:
    return PolarsEngine(storage=storage, cache=cache)


def drain_cache(engine) -> int:
    """Esegue le materializzazioni lasciate in sospeso da una preview: e' cio'
    che fa `materialize_step_task` fuori dalla richiesta. Ritorna quante ne ha
    scritte davvero."""
    written = 0
    for src, ops in engine.take_pending():
        written += bool(engine.materialize(src, ops))
    return written


def upload_df(storage: FakeStorage, df: pl.DataFrame, key: str) -> DataSource:
    """Carica un DataFrame come parquet nel FakeStorage e ritorna la sorgente."""
    import tempfile, os

    fd, path = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    df.write_parquet(path)
    storage.upload_file(path, BUCKET, key)
    os.unlink(path)
    return DataSource(bucket=BUCKET, key=key)


@pytest.fixture
def vendite(storage) -> DataSource:
    """Sorgente di esempio: vendite per paese, con date e qualche null."""
    df = pl.DataFrame(
        {
            "paese": ["IT", "IT", "FR", "FR", "DE", "DE"],
            "vendite": [100, 300, 50, 400, 250, None],
            "data": ["2024-01-15", "2024-06-30", "2024-03-01", "2024-12-31", "2025-02-10", "2024-07-08"],
        }
    ).with_columns(pl.col("data").str.to_date())
    return upload_df(storage, df, "datasets/vendite.parquet")


@pytest.fixture
def anagrafica(storage) -> DataSource:
    """Seconda sorgente per i test di join: paese → nome esteso."""
    df = pl.DataFrame({"paese": ["IT", "FR"], "nome": ["Italia", "Francia"]})
    return upload_df(storage, df, "datasets/anagrafica.parquet")
