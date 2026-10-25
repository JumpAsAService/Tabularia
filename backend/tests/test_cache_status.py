"""The editor asks which nodes have their result in the step cache, to draw a halo on
them. The answer must use the very fingerprints a preview computes: a node shows as
cached exactly when a preview of a node after it would start from there."""
import importlib.util

import pytest

from app.api.models import CacheStatusRequest
from app.api.routes import tasks
from app.engine.cache import StepCache, plan_hashes
from app.engine.duckdb_engine import DuckDBEngine
from app.engine.polars_engine import PolarsEngine
from tests.conftest import drain_cache

FILTER = {"type": "filter", "params": {"column": "paese", "operator": "eq", "value": "IT"}}
SORT = {"type": "sort", "params": {"by": ["vendite"], "descending": True}}
LIMIT = {"type": "limit", "params": {"n": 1}}


@pytest.fixture(params=["polars", "duckdb"])
def engine(request, storage, fredis, monkeypatch):
    cls = {"polars": PolarsEngine, "duckdb": DuckDBEngine}[request.param]
    eng = cls(storage=storage, cache=StepCache(storage, redis_client=fredis))
    monkeypatch.setattr(tasks, "_step_cache", lambda name: eng.cache)
    return eng


def _status(engine, src, chains: dict[str, list[dict]]) -> dict:
    body = {"engine": engine.engine_name,
            "chains": [{"node_id": n, "bucket": src.bucket, "input_key": src.key, "operations": ops} for n, ops in chains.items()]}
    return tasks.cache_status(CacheStatusRequest(**body))


def test_a_node_is_cached_once_a_preview_after_it_has_copied_it(engine, sales):
    chains = {"filter": [FILTER], "sort": [FILTER, SORT], "limit": [FILTER, SORT, LIMIT]}
    assert _status(engine, sales, chains) == {"cached": [], "too_big": []}

    engine.preview(sales, [FILTER, SORT], limit=10)  # clicking "sort" asks for a copy of "filter"
    drain_cache(engine)
    assert _status(engine, sales, chains) == {"cached": ["filter"], "too_big": []}

    engine.preview(sales, [FILTER, SORT, LIMIT], limit=10)  # clicking "limit": "sort" now starts from "filter"
    drain_cache(engine)
    assert _status(engine, sales, chains)["cached"] == ["filter", "sort"]


def test_a_step_too_big_for_the_cache_says_so(engine, sales):
    final = plan_hashes(engine._source_id(sales), [FILTER])[-1]
    engine.cache.mark_skipped(final)
    assert _status(engine, sales, {"filter": [FILTER]}) == {"cached": [], "too_big": ["filter"]}


def test_another_engine_s_copy_is_not_this_engine_s(storage, fredis, sales, monkeypatch):
    polars = PolarsEngine(storage=storage, cache=StepCache(storage, redis_client=fredis))
    polars.preview(sales, [FILTER, SORT], limit=10)
    drain_cache(polars)
    monkeypatch.setattr(tasks, "_step_cache", lambda name: polars.cache)
    body = {"engine": "duckdb", "chains": [{"node_id": "filter", "bucket": sales.bucket, "input_key": sales.key, "operations": [FILTER]}]}
    assert tasks.cache_status(CacheStatusRequest(**body))["cached"] == []


def test_a_source_alone_has_nothing_to_ask(engine, sales):
    assert _status(engine, sales, {"source": []}) == {"cached": [], "too_big": []}


ENGINES = [("app.engine.polars_engine", "PolarsEngine"), ("app.engine.duckdb_engine", "DuckDBEngine"),
           ("app.engine.clickhouse_engine", "ClickHouseEngine"), ("app.engine.bigquery_engine", "BigQueryEngine")]
if importlib.util.find_spec("chdb"):
    ENGINES.append(("app.engine.chdb_engine", "ChdbEngine"))


@pytest.mark.parametrize("module, name", ENGINES)
def test_every_engine_names_its_sources_as_the_route_does(module, name):
    """The route writes `<engine>:<bucket>/<key>`, each engine's `_source_id`: should
    one engine change it, its haloes would never light up."""
    from app.engine.base import DataSource

    cls = getattr(importlib.import_module(module), name)
    engine = object.__new__(cls)  # no connection, no client: only the name is needed
    assert engine._source_id(DataSource(bucket="b", key="datasets/k.parquet")) == f"{cls.engine_name}:b/datasets/k.parquet"
