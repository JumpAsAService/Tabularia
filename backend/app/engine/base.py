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

from pydantic import BaseModel, ConfigDict, Field, field_validator


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

    model_config = ConfigDict(arbitrary_types_allowed=True)

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
    # the rows as the engine's Polars DataFrame instead of `rows`, when the caller asked for
    # them so (preview(frame=True): a report reads its datasets in columns, app/reports/
    # frames.py); never serialized
    frame: Any = Field(default=None, exclude=True)

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


def preview_rows(df: Any, frame: bool) -> dict[str, Any]:
    """A PreviewResult's rows: Python's dicts, or with `frame` the DataFrame itself (a
    million rows as dicts weigh a gigabyte)."""
    return {"rows": [], "frame": df} if frame else {"rows": df.to_dicts()}


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
        frame: bool = False,
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
        use_cache: bool = False,
    ) -> int:
        """How many rows the chain gives, counted up to `at_most` + 1 and no further (one
        more says «more»): the chain capped and counted, read as a preview. Polars and the
        SQL engines stop reading at the cap. `use_cache`: start from the nearest step the
        step cache holds (the editor's counts); nothing is written to the cache."""
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
        result = self.preview(source, ops, limit=1, use_cache=use_cache)
        # the copies a preview leaves to make would be of the capped chain: none of them
        take_pending = getattr(self, "take_pending", None)
        if use_cache and take_pending is not None:
            take_pending()
        return int(next(iter(result.rows[0].values()), 0) or 0) if result.rows else 0

    # The column profile (app/engine/profile.py). The dialect its aggregates are written in
    # when they go through a `sql` step: ClickHouse's for chDB and an external ClickHouse,
    # GoogleSQL for BigQuery. DuckDB and Polars build them natively instead.
    profile_dialect = "clickhouse"

    @staticmethod
    def _profile_quote(name: str) -> str:
        """An identifier in backticks: ClickHouse's and GoogleSQL's quoting alike."""
        return "`" + str(name).replace("\\", "\\\\").replace("`", "\\`") + "`"

    def _through_sql(self, source: DataSource, operations, sample: int, query_of, limit: int,
                     use_cache: bool) -> list[dict[str, Any]]:
        """The rows of `query_of(self_name)` over the chain's first `sample` rows, as a last
        `sql` step read by a preview (or inside the chain's own last `sql` step, as a count
        does: see `count_inside_sql`). Nothing is left to copy into the step cache."""
        ops = [op.model_dump() if isinstance(op, Operation) else dict(op) for op in operations]
        last = ops[-1] if ops else None
        if self.count_inside_sql and last is not None and last.get("type") == "sql":
            params = dict(last.get("params") or {})
            inner = str(params.get("query") or "").strip().rstrip(";").strip()
            params["query"] = query_of(f"(SELECT * FROM ({inner}\n) AS _p LIMIT {sample}) AS _q")
            ops[-1] = {**last, "params": params}
        else:
            ops += [{"type": "limit", "params": {"n": sample}}, {"type": "sql", "params": {"query": query_of("self")}}]
        result = self.preview(source, ops, limit=limit, use_cache=use_cache)
        take_pending = getattr(self, "take_pending", None)
        if use_cache and take_pending is not None:
            take_pending()
        return result.rows

    def profile_aggregates(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        specs: list[tuple[str, str, str | None]],
        sample: int,
        use_cache: bool = False,
    ) -> dict[str, Any]:
        """One row of aggregates, `specs` = (alias, function, column) (profile.plan), over the
        chain's first `sample` rows, in ONE query; `use_cache`: from the nearest step the
        step cache holds, writing nothing to it."""
        from app.engine.profile import sql_aggregate

        select = ", ".join(f"{sql_aggregate(self.profile_dialect, f, c, self._profile_quote)} AS {a}" for a, f, c in specs)
        rows = self._through_sql(source, operations, sample, lambda t: f"SELECT {select} FROM {t}", 1, use_cache)
        return dict(rows[0]) if rows else {}

    def profile_top(
        self,
        source: DataSource,
        operations: list[Operation] | list[dict[str, Any]],
        column: str,
        sample: int,
        k: int,
        use_cache: bool = False,
    ) -> list[tuple[Any, int]]:
        """A column's `k` most frequent values that are not null, (value, rows), over the
        chain's first `sample` rows; the most frequent first, a tie by the value."""
        q = self._profile_quote(column)
        rows = self._through_sql(
            source, operations, sample,
            lambda t: f"SELECT {q} AS v, count(*) AS n FROM {t} WHERE {q} IS NOT NULL GROUP BY {q} ORDER BY n DESC, v LIMIT {k}",
            k, use_cache)
        return [(r.get("v"), int(r.get("n") or 0)) for r in rows]
