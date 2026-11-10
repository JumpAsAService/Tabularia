"""What a release computed is never served by another one.

A step's fingerprint was engine, source and chain: after an upgrade that changes what a step
gives under the same operations (a cast to an instant on the ClickHouse engines, 1.4.3), the
step cache, the counts and the column profiles kept on answering with the previous release's
results, for as long as somebody went on reading them. The release is part of every
fingerprint now; what the release before left is never touched again, so the sweep and the
keys' expiry remove it.
"""
from __future__ import annotations

import importlib
import importlib.util
import tomllib
from pathlib import Path

import polars as pl
import pytest

from app.core import release
from app.engine import profile, rowcount
from app.engine.base import DataSource
from app.engine.cache import plan_hashes, source_fingerprint
from tests.conftest import drain_cache, upload_df

BACKEND = Path(__file__).resolve().parents[1]
FILTER_IT = [{"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "IT"}}]
ENGINES = ["polars", "duckdb", "clickhouse", "bigquery", "chdb"]


@pytest.fixture
def before(monkeypatch):
    """Runs a block as another release would."""
    class Before:
        def __enter__(self):
            monkeypatch.setattr(release, "current", lambda: "0.9.0")

        def __exit__(self, *exc):
            monkeypatch.undo()

    return Before()


# ── the release ──────────────────────────────────────────────────────────────
def test_the_release_is_the_one_the_project_declares():
    declared = tomllib.loads((BACKEND / "pyproject.toml").read_text())["project"]["version"]
    assert release.current() == declared and declared != release.UNKNOWN


def test_the_image_carries_the_file_the_release_is_read_from():
    """The image holds `app/` only: without this line every release would be «unknown», and
    one would read the other's copies again."""
    lines = [line.split() for line in (BACKEND / "Dockerfile").read_text().splitlines()]
    assert any(words[:1] == ["COPY"] and "pyproject.toml" in words[-2] and words[-1] == "./" for words in lines if len(words) > 2)
    assert release.PYPROJECT == BACKEND / "pyproject.toml"


def test_a_project_file_that_is_not_there_is_an_unknown_release_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(release, "PYPROJECT", tmp_path / "pyproject.toml")
    assert release.read() == release.UNKNOWN
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'backend'\n")
    assert release.read() == release.UNKNOWN
    (tmp_path / "pyproject.toml").write_text("[project]\nversion = '9.9.9'\n")
    assert release.read() == "9.9.9"


# ── the fingerprints ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("engine_name", ENGINES)
def test_a_fingerprint_names_the_release_that_computed_the_step(engine_name, before):
    now = source_fingerprint(engine_name, "b", "datasets/k.parquet")
    assert release.current() in now
    with before:
        earlier = source_fingerprint(engine_name, "b", "datasets/k.parquet")
    assert earlier != now and plan_hashes(earlier, FILTER_IT) != plan_hashes(now, FILTER_IT)
    # and none is the one the releases without it wrote
    assert f"{engine_name}:b/datasets/k.parquet" not in (now, earlier)


def test_a_count_and_a_profile_remembered_by_another_release_are_not_found(before):
    columns = [{"name": "paese", "dtype": "String"}]
    asked = [("polars", "b", "k", []), ("polars", "b", "k", FILTER_IT), ("clickhouse", "b", "k", FILTER_IT)]
    now = [(rowcount.count_key(*a), profile.profile_key(*a, columns), profile.profile_key(*a, columns, column="paese")) for a in asked]
    with before:
        earlier = [(rowcount.count_key(*a), profile.profile_key(*a, columns), profile.profile_key(*a, columns, column="paese")) for a in asked]
    assert not {k for keys in now for k in keys} & {k for keys in earlier for k in keys}


def test_the_dashboards_numbers_kept_by_another_release_are_not_found(before):
    from app.reports import tile_cache

    spec = {"engine": "polars", "bucket": "b", "key": "k", "operations": FILTER_IT}
    now = (tile_cache.rows_hash(spec, 1000), tile_cache.tile_hash({"id": "k"}, {}, {}, None, 0, None, "en", "UTC", "t", 1000, 60))
    with before:
        earlier = (tile_cache.rows_hash(spec, 1000), tile_cache.tile_hash({"id": "k"}, {}, {}, None, 0, None, "en", "UTC", "t", 1000, 60))
    assert now[0] != earlier[0] and now[1] != earlier[1]


# ── the engines ──────────────────────────────────────────────────────────────
def _poisoned(storage, cache, fingerprint: str) -> str:
    """A copy of the filter's step that holds what no release should answer with."""
    h = plan_hashes(fingerprint, FILTER_IT)[-1]
    upload_df(storage, pl.DataFrame({"paese": ["XX"], "vendite": [-1]}), cache.object_key(h))
    cache.mark(h)
    return h


def test_a_preview_does_not_start_from_a_step_another_release_copied(engine, sales, cache, storage, before):
    with before:
        versioned = _poisoned(storage, cache, engine._source_id(sales))
    unversioned = _poisoned(storage, cache, f"polars:{sales.bucket}/{sales.key}")  # as up to 1.4.3
    ops = FILTER_IT + [{"type": "select", "params": {"columns": ["paese", "vendite"]}}]
    res = engine.preview(sales, ops, limit=10)
    assert sorted(r["vendite"] for r in res.rows) == [100, 300]
    # the copy this release makes lives beside them
    assert drain_cache(engine) == 1
    mine = plan_hashes(engine._source_id(sales), FILTER_IT)[-1]
    assert cache.has(mine) and mine not in (versioned, unversioned)
    assert sorted(r["vendite"] for r in engine.preview(sales, ops, limit=10).rows) == [100, 300]


def test_what_another_release_copied_leaves_with_the_sweep(engine, sales, cache, storage, fredis, before):
    """Nobody reads them any more, so their last access stays where it was: the cache's TTL
    takes blob and index entry, with no pass of its own."""
    import time

    with before:
        old = _poisoned(storage, cache, engine._source_id(sales))
    fredis.zadd(cache.atime_zset, {old: time.time() - 7200})
    engine.preview(sales, FILTER_IT + [{"type": "limit", "params": {"n": 1}}], limit=10)
    drain_cache(engine)
    mine = plan_hashes(engine._source_id(sales), FILTER_IT)[-1]
    assert cache.evict_expired(3600) == 1
    assert not cache.has(old) and not storage.exists(cache.bucket, cache.object_key(old))
    assert cache.has(mine) and storage.exists(cache.bucket, cache.object_key(mine))


MODULES = [("app.engine.polars_engine", "PolarsEngine"), ("app.engine.duckdb_engine", "DuckDBEngine"),
           ("app.engine.clickhouse_engine", "ClickHouseEngine"), ("app.engine.bigquery_engine", "BigQueryEngine")]
if importlib.util.find_spec("chdb"):
    MODULES.append(("app.engine.chdb_engine", "ChdbEngine"))


@pytest.mark.parametrize("module, name", MODULES)
def test_every_engine_starts_its_fingerprints_from_the_release(module, name, before):
    cls = getattr(importlib.import_module(module), name)
    engine = object.__new__(cls)  # no connection, no client: only the name is needed
    source = DataSource(bucket="b", key="datasets/k.parquet")
    now = engine._source_id(source)
    with before:
        assert engine._source_id(source) != now
