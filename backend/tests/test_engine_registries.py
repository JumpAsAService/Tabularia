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
EXCEPTIONS: dict[str, dict[str, str]] = {
    "chdb": {"foreach": "documented: the loop runs on Polars and DuckDB only"},
    "bigquery": {"foreach": "documented: the loop runs on Polars and DuckDB only"},
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
    # engine-differences.md: "all 38" on DuckDB and Polars, 37 on chDB, ClickHouse and BigQuery
    assert len(REGISTRIES["duckdb"]) == len(REGISTRIES["polars"])
    assert len(REGISTRIES["chdb"]) == len(REGISTRIES["polars"]) - 1
    assert len(REGISTRIES["bigquery"]) == len(REGISTRIES["polars"]) - 1
