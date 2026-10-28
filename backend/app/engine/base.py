"""
The transformation engine's interface and the domain models.

The idea is to keep the *logical representation of the flow* (a list of Operations, the
declarative IR) apart from the *execution engine* (Polars, DuckDB, ClickHouse, BigQuery).
Routes and Celery tasks talk only to this interface, never to an engine directly.
"""
from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────────────────────────────────────
# Domain models (IR + results)
# ─────────────────────────────────────────────────────────────────────────────
class DataSource(BaseModel):
    """A reference to a parquet on the S3 storage."""

    bucket: str
    key: str


class Operation(BaseModel):
    """One step of the flow: a type + its parameters."""

    type: str = Field(..., description="The operation's type (filter, select, group_by, join, ...)")
    params: dict[str, Any] = Field(default_factory=dict)


class ColumnInfo(BaseModel):
    name: str
    dtype: str


class PreviewResult(BaseModel):
    """A preview's result: a sample of rows + the schema."""

    columns: list[ColumnInfo]
    rows: list[dict[str, Any]]
    row_count: int = Field(..., description="How many rows the sample returned holds")
    truncated: bool = Field(..., description="True when the real result has more rows than the limit")
    # The step-cache state of the step UPSTREAM of this node (the one the next previews
    # would start from): "hit" cached, "pending" being written later, "skipped" over the
    # cap (computed again at every preview), "off" cache not used (Viewer). None = no
    # upstream step to cache.
    cache_state: str | None = Field(default=None)
    cache_cap_rows: int | None = Field(default=None, description="The cache's row cap, when it matters")

    @field_validator("rows")
    @classmethod
    def _non_finite_as_text(cls, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """A division by zero gives inf or NaN: in JSON they became null, an empty cell the
        preview showed as a missing value — that «is null» did not find and «fill nulls»
        did not fill, and the export wrote as inf. Shown as what they are."""
        for row in rows:
            for k, v in row.items():
                if isinstance(v, float) and not math.isfinite(v):
                    row[k] = "NaN" if math.isnan(v) else ("inf" if v > 0 else "-inf")
        return rows


class RunResult(BaseModel):
    """A whole run's result: where it wrote and what."""

    destination: DataSource
    rows_written: int
    columns: list[ColumnInfo]


# ─────────────────────────────────────────────────────────────────────────────
# The engine interface
# ─────────────────────────────────────────────────────────────────────────────
class Engine(ABC):
    """
    The contract every execution backend implements.

    - `preview`: runs the flow on a sample, *synchronously and fast* (the interactive
      feedback while the user builds the flow).
    - `run`: runs the whole flow and materializes the output parquet (usually started
      asynchronously by a Celery worker).
    - `export`: runs the flow (partially too) and writes the result to a local csv/xlsx
      file, for the editor's direct download.
    """

    @abstractmethod
    def preview(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        limit: int = 100,
        use_cache: bool = True,
        sort_keys: list[str] | None = None,
    ) -> PreviewResult: ...

    @abstractmethod
    def run(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        destination: DataSource,
        use_cache: bool = True,
    ) -> RunResult: ...

    def export(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        fmt: str,
        out_path: str,
        limit: int | None = None,
        max_rows: int | None = None,
    ) -> None:
        # not abstract: an engine may not support the direct export
        raise NotImplementedError(f"{type(self).__name__} does not support export")

    # Where a count goes when the chain ends with a `sql` step: inside it, around its query
    # (the SQL engines: a second step nests the first one's WITH in its own, and ClickHouse
    # does not resolve the names), or after it (Polars, whose SQL reads no WITH inside a
    # subquery and runs one step after another lazily).
    count_inside_sql = True

    def count_rows(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        at_most: int,
    ) -> int:
        """How many rows the chain gives, counted up to `at_most` + 1 and no further (one
        more says «more»): the chain capped and counted, read as a preview without the
        cache. Polars and the SQL engines stop reading at the cap."""
        ops = [op.model_dump() if isinstance(op, Operation) else dict(op) for op in operations]
        rows = at_most + 1
        last = ops[-1] if ops else None
        if self.count_inside_sql and last is not None and last.get("type") == "sql":
            params = dict(last.get("params") or {})
            query = str(params.get("query") or "").strip().rstrip(";").strip()
            params["query"] = f"SELECT COUNT(*) AS n FROM (SELECT * FROM ({query}\n) AS _c LIMIT {rows}) AS _d"
            ops[-1] = {**last, "params": params}
        else:
            ops += [{"type": "limit", "params": {"n": rows}},
                    {"type": "sql", "params": {"query": "SELECT COUNT(*) AS n FROM self"}}]
        result = self.preview(source, ops, limit=1, use_cache=False)
        return int(next(iter(result.rows[0].values()), 0) or 0) if result.rows else 0
