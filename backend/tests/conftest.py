"""Shared fixtures: the "preparations" pytest injects into the tests.

A fixture is a function whose RESULT is passed as an argument to the tests that declare
its name. For example, a test with a parameter `engine` receives a PolarsEngine wired to
a fake storage and cache already.
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

# The tests' bucket is fake (FakeStorage) except in the LIVE suites, where the parquet
# files go to the real storage: then it is the configured one.
BUCKET = os.getenv("TEST_BUCKET") or "data-prep"


@pytest.fixture(autouse=True)
def regolazioni_isolate(monkeypatch):
    """Every test starts without panel values, on a fake Valkey of its own: those set on the
    development Valkey would change the results (another row group or cap). Whoever tests
    them writes them here (`panel_settings.write`)."""
    from app.core import panel_settings

    fake = FakeRedis()
    monkeypatch.setattr(panel_settings, "_client", lambda: fake)
    panel_settings.forget()
    yield fake
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
    """Runs the materializations a preview left pending: what `materialize_step_task` does
    outside the request. Returns how many it really wrote."""
    written = 0
    for src, ops in engine.take_pending():
        written += bool(engine.materialize(src, ops))
    return written


def upload_df(storage: FakeStorage, df: pl.DataFrame, key: str) -> DataSource:
    """Loads a DataFrame as parquet into the FakeStorage and returns the source."""
    import tempfile, os

    fd, path = tempfile.mkstemp(suffix=".parquet")
    os.close(fd)
    df.write_parquet(path)
    storage.upload_file(path, BUCKET, key)
    os.unlink(path)
    return DataSource(bucket=BUCKET, key=key)


@pytest.fixture
def sales(storage) -> DataSource:
    """A sample source: sales by country, with dates and some nulls."""
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
    """A second source for the join tests: country → full name."""
    df = pl.DataFrame({"paese": ["IT", "FR"], "nome": ["Italia", "Francia"]})
    return upload_df(storage, df, "datasets/anagrafica.parquet")
