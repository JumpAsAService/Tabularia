"""The sizes of the sample data (SAMPLES_SCALE), for the generator and the seed alike.

Standard library only: the seed's tests and the CI read it without the generator's
numpy and pandas.
"""
from __future__ import annotations

# (area managers, inspectors, agents, customers, orders, CRM activities). massive puts a
# billion rows where Tabularia reads them best, the ClickHouse CRM log; the order system
# stays at robust's size (Postgres at a billion rows would take most of a day and
# hundreds of GB).
PRESETS = {
    "small":   dict(managers=3,  inspectors=12, agents=90,    customers=3_000,   orders=30_000,     activities=60_000),
    "medium":  dict(managers=5,  inspectors=30, agents=300,   customers=30_000,  orders=1_000_000,  activities=2_000_000),
    "robust":  dict(managers=8,  inspectors=56, agents=700,   customers=150_000, orders=12_000_000, activities=60_000_000),
    "massive": dict(managers=10, inspectors=80, agents=1_000, customers=500_000, orders=12_000_000, activities=1_000_000_000),
}
# robust's earlier name: an .env written for 1.3.0 or before keeps working
ALIASES = {"robusta": "robust"}

# how long the seed waits for the tables' import and for each flow's first run, in
# minutes: at massive a billion CRM activities are copied into Tabularia, and grouped
# by the CRM flow
WAIT_IMPORT = {"robust": 120, "massive": 480}
WAIT_RUN = {"robust": 60, "massive": 180}

# the engines the seed tries for the flows, the first the engine policy allows (unless
# SAMPLES_ENGINE names one). DuckDB first, at every scale: it spills to disk, while Polars
# holds a group in memory — «Top 20 Products by Revenue» counts the distinct orders of
# each product over 42 million lines (robust), and on Polars passed 7 GB (killed on a
# 15 GB machine), on DuckDB it took 18 s and some 300 MB (measured on kind, 2026-11-03)
ENGINES = ("duckdb", "polars", "chdb", "clickhouse", "bigquery")


def resolve(name: str | None) -> str:
    """The preset's name from SAMPLES_SCALE (the default small, the earlier names too);
    ValueError for one that does not exist."""
    scale = (name or "small").strip().lower()
    scale = ALIASES.get(scale, scale)
    if scale not in PRESETS:
        raise ValueError(f"SAMPLES_SCALE is not valid: {name!r} (use {'|'.join(PRESETS)})")
    return scale
