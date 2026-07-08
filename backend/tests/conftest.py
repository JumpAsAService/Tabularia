"""Fixture condivise: le "preparazioni" che pytest inietta nei test.

Una fixture è una funzione il cui RISULTATO viene passato come argomento ai
test che ne dichiarano il nome. Esempio: un test con parametro `engine`
riceve un PolarsEngine già collegato a storage e cache finti.
"""
from __future__ import annotations

import polars as pl
import pytest

from app.engine.base import DataSource
from app.engine.cache import StepCache
from app.engine.polars_engine import PolarsEngine
from tests.fakes import FakeRedis, FakeStorage

BUCKET = "data-prep"


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
