"""The five engines register the same operation names, except for the documented ones.

An operation is registered by name in four Python registries (Polars `operations.py`,
`duckdb_ops.py`, `chdb_ops.py`, `bigquery_ops.py`); the external ClickHouse engine reuses
chDB's. Nothing else checks that they agree, and an operation missing on an engine is
only found when a flow with it runs. The exceptions below are the ones that
docs/engines/engine-differences.md lists ("Operation coverage"); a new difference has to
be written here AND in that page, or fixed.
"""
from __future__ import annotations

from app.engine import bigquery_ops, chdb_ops, duckdb_ops, operations

REGISTRIES = {
    "polars": set(operations.available_operations()),
    "duckdb": set(duckdb_ops._REGISTRY),
    "chdb": set(chdb_ops._REGISTRY),
    "bigquery": set(bigquery_ops._REGISTRY),
}

# Operations an engine really does not have: {engine: {operation: why}}.
# "documented" = listed in docs/engines/engine-differences.md.
#
# `foreach` IS in chDB's registry, for the external ClickHouse that shares it: the loop runs there
# on the s3 transport, where the server can keep each iteration's result apart. The operation
# itself refuses a context that cannot (embedded chDB, the push transport), which is checked below
# and in test_clickhouse_loop.py.
EXCEPTIONS: dict[str, dict[str, str]] = {
    "bigquery": {"foreach": "documented: the loop runs on Polars, DuckDB and the external ClickHouse (s3)"},
}


def test_every_engine_has_every_operation_but_the_documented_ones():
    everything = set().union(*REGISTRIES.values())
    problems = []
    for engine, names in REGISTRIES.items():
        missing = everything - names - set(EXCEPTIONS.get(engine, {}))
        if missing:
            problems.append(f"{engine} lacks {sorted(missing)}")
        # an exception that is no longer true must leave the list
        stale = set(EXCEPTIONS.get(engine, {})) & names
        if stale:
            problems.append(f"{engine} has {sorted(stale)}, which EXCEPTIONS says it lacks")
    assert not problems, "; ".join(problems)


def test_the_counts_match_the_documentation():
    # engine-differences.md: "all 41" on DuckDB, Polars and the external ClickHouse on s3 (the
    # registry it shares with chDB), one fewer on BigQuery; embedded chDB has the name and refuses it
    assert len(REGISTRIES["duckdb"]) == len(REGISTRIES["polars"])
    assert len(REGISTRIES["chdb"]) == len(REGISTRIES["polars"])
    assert len(REGISTRIES["bigquery"]) == len(REGISTRIES["polars"]) - 1


def test_the_loop_is_refused_where_nothing_can_keep_its_iterations_apart():
    # embedded chDB's context has no way to: the name is registered, the step says it is not supported
    import pytest

    from app.engine.exceptions import EngineError

    with pytest.raises(EngineError, match="not supported"):
        chdb_ops.get_chdb_operation("foreach")("SELECT 1", {"items": [{"a": 1}, {"a": 2}], "body": [{"type": "limit", "params": {"n": 1}}]}, object())
