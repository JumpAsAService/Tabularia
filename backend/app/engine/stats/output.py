"""The table a statistics node returns: ten columns, the same on every engine, one row per
number or text of the test. `Rows` collects them while a test is computed; the three
converters hand them to an engine as it wants them (a Polars frame, an Arrow table for
DuckDB, a SELECT of literals for the ClickHouse engines and BigQuery).

A number that is not finite (the open side of a one-sided interval, a statistic that does
not exist) is NULL, never inf or NaN; a count is a Float64 as the rest (exact up to 2^53).
No name is a reserved word of an engine's SQL: a `sql` or `compute` node after the test
names the columns without quotes.
"""
from __future__ import annotations

import math
from typing import Any, Callable

import polars as pl

# (name, type as a preview names it, nullable)
OUTPUT_COLUMNS: tuple[tuple[str, str, bool], ...] = (
    ("section", "String", False),
    ("level", "String", True),
    ("level_2", "String", True),
    ("stat", "String", False),
    ("value", "Float64", True),
    ("text", "String", True),
    ("x0", "Float64", True),
    ("x1", "Float64", True),
    ("ord", "Int64", False),
    ("key", "String", False),
)
OUTPUT_NAMES = tuple(name for name, _, _ in OUTPUT_COLUMNS)

_POLARS = {"String": pl.String, "Float64": pl.Float64, "Int64": pl.Int64}
_CLICKHOUSE = {"String": "String", "Float64": "Float64", "Int64": "Int64"}
_BIGQUERY = {"String": "STRING", "Float64": "FLOAT64", "Int64": "INT64"}


def _finite(v: Any) -> float | None:
    if v is None:
        return None
    v = float(v)
    return v + 0.0 if math.isfinite(v) else None  # + 0.0: a −0.0 is written 0.0


class Rows:
    """The rows of an output, in the order they are added: `ord` is their place, `key` their
    section, level, level_2 and stat joined by a bar, nothing for a part that is null (and
    the index after one more bar, for the rows of a report's charts: `point`)."""

    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def _add(self, section: str, level: str | None, level_2: str | None, stat: str, value: Any, text: str | None) -> None:
        key = "|".join((section, level or "", level_2 or "", stat))
        self.rows.append((section, level, level_2, stat, _finite(value), text, None, None, len(self.rows), key))

    def number(self, section: str, stat: str, value: Any, level: str | None = None, level_2: str | None = None) -> None:
        self._add(section, level, level_2, stat, value, None)

    def text(self, section: str, stat: str, text: str, level: str | None = None, level_2: str | None = None) -> None:
        self._add(section, level, level_2, stat, None, text)

    def point(self, section: str, stat: str, index: int, value: Any, x0: Any, x1: Any = None,
              text: str | None = None, level: str | None = None) -> None:
        """A row of a report's chart, one of many with the same stat: an interval of a
        histogram (`x0`, `x1` its edges), a point of a curve (`x0` its abscissa). Its key
        ends with its index."""
        key = "|".join((section, level or "", "", stat, str(index)))
        self.rows.append((section, level, None, stat, _finite(value), text, _finite(x0), _finite(x1), len(self.rows), key))


def to_polars(rows: list[tuple]) -> pl.LazyFrame:
    schema = {name: _POLARS[kind] for name, kind, _ in OUTPUT_COLUMNS}
    return pl.LazyFrame(rows, schema=schema, orient="row")


def to_arrow(rows: list[tuple]):
    """The rows as an Arrow table (DuckDB reads it as a relation)."""
    import pyarrow as pa

    kinds = {"String": pa.string(), "Float64": pa.float64(), "Int64": pa.int64()}
    return pa.table({
        name: pa.array([row[j] for row in rows], type=kinds[kind])
        for j, (name, kind, _) in enumerate(OUTPUT_COLUMNS)
    })


def values_sql(dialect: str, rows: list[tuple], lit: Callable[[Any], str]) -> str:
    """The rows as a SELECT of literals, «clickhouse» (chDB and the server) or «bigquery»;
    `lit` writes a text in the engine's way. A number is written by `repr`, the shortest
    text that reads back as the same double."""

    def cell(v: Any) -> str:
        if v is None:
            return "NULL"
        return lit(v) if isinstance(v, str) else repr(v)

    tuples = [", ".join(cell(v) for v in row) for row in rows]
    if dialect == "clickhouse":
        structure = ", ".join(
            f"{name} Nullable({_CLICKHOUSE[kind]})" if nullable else f"{name} {_CLICKHOUSE[kind]}"
            for name, kind, nullable in OUTPUT_COLUMNS
        )
        return f"SELECT * FROM values('{structure}', {', '.join(f'({t})' for t in tuples)})"
    if dialect == "bigquery":
        # every row a STRUCT of the declared type: nothing is left to guess from a NULL
        struct = "STRUCT<" + ", ".join(f"{name} {_BIGQUERY[kind]}" for name, kind, _ in OUTPUT_COLUMNS) + ">"
        return f"SELECT * FROM UNNEST([{', '.join(f'{struct}({t})' for t in tuples)}])"
    raise ValueError(f"unknown dialect: {dialect}")
