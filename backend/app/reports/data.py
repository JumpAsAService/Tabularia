"""Reading a report's datasets and its parameters, as the gateway resolved them.

The gateway checks rights dataset by dataset and sends what to read (docs/specs/reports.md,
«Rendering (engine)»), one of:

  {"bucket", "input_key", "operations", "engine"}   a chain on the catalog, as a SQL script's
                                                     or a view's (read as a preview is, without
                                                     the step cache)
  {"connection", "sql"}                              a query on a database connection, the
                                                     password still encrypted
  {"rows": [...], "columns": [{"name", "type"}]}     rows given as they are (fixed data, tests)

At most REPORTS__MAX_ROWS rows per dataset are read, and one more to know when there are
more: then the render stops and names the dataset. The values are made plain for the
expressions — decimals as floats, date-times in the report's time zone and without it (one
without a zone is a UTC instant, the engines' standard: app/engine/temporal.py) — and the
report's calculated fields are added to every row.

A query's and a chain's rows are read as columns and stay so (app/reports/frames.py): the
dataset's `table`. Its `rows`, Python's dicts, are made only when the interpreter asks for
them (an item DuckDB leaves to it, the choices of a parameter).
"""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

from app.core import placeholders as ph
from app.reports import definition as rd
from app.reports import expressions as ex
from app.reports import frames
from app.reports.errors import ReportError


class DataSet:
    """A dataset read: its columns (name → type, as the engine names it), and its rows as
    Python's dicts, or in columns (`table`, whose rows are made when asked for)."""

    def __init__(self, name: str, columns: dict[str, str], rows: list[dict[str, Any]] | None = None,
                 table: frames.Table | None = None):
        self.name = name
        self.columns = columns
        self._rows = rows
        self.table = table

    @property
    def rows(self) -> list[dict[str, Any]]:
        if self._rows is None:
            self._rows = self.table.rows() if self.table is not None else []
        return self._rows

    @rows.setter
    def rows(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def __repr__(self) -> str:
        return f"DataSet({self.name!r}, {len(self.columns)} columns, {'columns' if self.table else 'rows'})"


def used_datasets(report: rd.Report) -> list[str]:
    """The datasets a render reads: the items', and those named inside aggregates."""
    names = {d.name for d in report.datasets}
    used: list[str] = []

    def add(name: str | None) -> None:
        if name and name in names and name not in used:
            used.append(name)

    def walk(text: str | None) -> None:
        if not text:
            return
        try:
            node = rd.parse(text)
        except ex.ExpressionError:
            return
        stack = [node]
        while stack:
            n = stack.pop()
            if isinstance(n, ex.Call):
                if n.name in ex.AGGREGATES:
                    add(ex.scope_dataset(n, names))
                stack.extend(n.args)
            elif isinstance(n, ex.Unary):
                stack.append(n.arg)
            elif isinstance(n, ex.Binary):
                stack.extend((n.left, n.right))

    for _, it in report.items():
        add(getattr(it, "dataset", None))
        if it.type == "text":
            text, is_expr = ex.text_value(it.value)
            if is_expr:
                if it.dataset is None and len(report.datasets) == 1:
                    add(report.datasets[0].name)
                walk(text)
        walk(it.visible)
        for value in _expressions_of(it):
            walk(value)
    for d in report.datasets:
        if d.name in used:
            for c in d.calculated:
                walk(c.expr)
    return used


def _expressions_of(it: Any) -> list[str | None]:
    if it.type == "table":
        out = [it.filter] + [c.value for c in it.columns] + [c.total for c in it.columns]
        out += [g.label for g in it.groups] + [r.when for c in it.columns for r in c.rules]
        if it.matrix:
            out.append(it.matrix.value)
        return out
    if it.type == "chart":
        return [s.value for s in it.series] + [it.options.target if isinstance(it.options.target, str) else None]
    if it.type == "kpi":
        return [it.value, it.compare, it.sparkline.value if it.sparkline else None]
    return []


_plain = frames.plain


def _non_finite(columns: dict[str, str], rows: list[dict]) -> None:
    """The preview writes NaN and infinity as text (JSON has neither): in a column of
    floats they are no number at all."""
    floats = [c for c, t in columns.items() if t.lower().startswith("float")]
    for row in rows:
        for c in floats:
            if isinstance(row.get(c), str):
                row[c] = None


def _preview(engine: Any, source: Any, operations: list, max_rows: int) -> Any:
    """The chain read as a preview without the cache, as a DataFrame when the engine gives
    one (preview(frame=True))."""
    import inspect

    if "frame" in inspect.signature(engine.preview).parameters:
        return engine.preview(source, operations, limit=max_rows, use_cache=False, frame=True)
    return engine.preview(source, operations, limit=max_rows, use_cache=False)


def _read_one(name: str, spec: dict, max_rows: int) -> tuple[dict[str, str], Any, bool]:
    """(columns, rows or a DataFrame, over the cap)."""
    from app.engine.exceptions import EngineError, SourceNotFoundError
    from app.ingest.converters import IngestError

    try:
        if "rows" in spec:
            rows = [dict(r) for r in spec.get("rows") or []]
            cols = spec.get("columns") or []
            columns = ({c["name"]: c.get("type", "") for c in cols} if isinstance(cols, list) else dict(cols))
            if not columns and rows:
                columns = {k: "" for k in rows[0]}
            return columns, rows[:max_rows], len(rows) > max_rows
        if "connection" in spec:
            from app.ingest.db_source import DbConnectionSpec, query_rows

            df, truncated = query_rows(DbConnectionSpec(**spec["connection"]), spec.get("sql") or "", max_rows)
            return {n: str(t) for n, t in df.schema.items()}, df, truncated
        if "input_key" in spec:
            from app.engine import DEFAULT_ENGINE, DataSource, get_engine

            engine = get_engine((spec.get("engine") or DEFAULT_ENGINE).lower())
            source = DataSource(bucket=spec["bucket"], key=spec["input_key"])
            result = _preview(engine, source, spec.get("operations") or [], max_rows)
            columns = {c.name: c.dtype for c in result.columns}
            if result.frame is not None:
                return columns, result.frame, result.truncated
            _non_finite(columns, result.rows)
            return columns, result.rows, result.truncated
    except SourceNotFoundError as e:
        raise ReportError(f"dataset «{name}»: {e}", "not_found") from None
    except (EngineError, IngestError) as e:
        raise ReportError(f"dataset «{name}»: {e}") from None
    raise ReportError(f"dataset «{name}»: the gateway sent nothing the engine knows how to read")


def read_dataset(name: str, spec: Any, max_rows: int, tz: ZoneInfo) -> DataSet:
    """One dataset, read: ReportError naming it when it is missing or over the cap."""
    if not isinstance(spec, dict):
        raise ReportError(f"the dataset «{name}» was not sent with the report: it cannot be read")
    columns, rows, truncated = _read_one(name, spec, max_rows)
    if truncated:
        raise ReportError(f"the dataset «{name}» has more than {max_rows:,} rows, the most a report reads: "
                          "aggregate it in SQL, or filter it")
    if not isinstance(rows, list):  # a DataFrame: in columns, its values made plain column by column
        table = frames.from_frame(rows, tz)
        if table is not None:
            return DataSet(name, columns, table=table)
        rows = rows.to_dicts()
    return DataSet(name, columns, [{k: _plain(v, tz) for k, v in r.items()} for r in rows])


def read(report: rd.Report, resolved: dict[str, dict], max_rows: int) -> dict[str, DataSet]:
    """Every dataset the report uses, read."""
    tz = ZoneInfo(report.timezone or "UTC")
    return {name: read_dataset(name, resolved.get(name), max_rows, tz) for name in used_datasets(report)}


def add_calculated(report: rd.Report, data: dict[str, DataSet], ctx: ex.Context, evaluate: ex.Evaluator) -> None:
    """The report's own fields, row by row, in their order (each may use the ones before);
    an aggregate in one works on the whole dataset."""
    for d in report.datasets:
        ds = data.get(d.name)
        if ds is None or not d.calculated:
            continue
        c_ctx = ctx.at(scopes=[ds.rows])
        for c in d.calculated:
            node = rd.parse(c.expr)
            for i, row in enumerate(ds.rows):
                c_ctx.row = row
                c_ctx.rownumber = i + 1
                try:
                    row[c.name] = evaluate(node, c_ctx)
                except ex.EvalError as e:
                    raise ReportError(f"dataset «{d.name}», calculated field «{c.name}», row {i + 1}: {e}") from None
            ds.columns[c.name] = ""


# ── parameters ───────────────────────────────────────────────────────────────
_TRUE, _FALSE = {"true", "1", "yes", "y", "on", "t"}, {"false", "0", "no", "n", "off", "f"}


def _one(p: rd.Parameter, value: Any, pctx: ph.Context) -> Any:
    if value is None or value == "":
        return None
    if p.type == "text":
        return value if isinstance(value, str) else str(value)
    if p.type == "number":
        if isinstance(value, bool):
            raise ValueError("a number is wanted")
        if isinstance(value, (int, float)):
            return value
        text = str(value).strip().replace(" ", "")
        try:
            n = float(text)
        except ValueError:
            raise ValueError(f"«{value}» is not a number") from None
        if not math.isfinite(n):
            raise ValueError(f"«{value}» is not a number")
        return int(n) if n.is_integer() and "." not in text and "e" not in text.lower() else n
    if p.type == "boolean":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise ValueError(f"«{value}» is not true or false")
    # date: a date, a datetime's date, an ISO text, or a placeholder of Tabularia's language
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if "{" in text or text in ph.BUILTINS:
        template = text if "{" in text else "{" + text + "}"
        try:
            parts = ph.parse(template)
            if len(parts) == 1 and isinstance(parts[0], ph.Placeholder):
                v = ph.evaluate(parts[0], pctx)
                return v.date() if isinstance(v, datetime) else v
            text = ph.render(template, pctx)
        except ph.PlaceholderError as e:
            raise ValueError(str(e)) from None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        raise ValueError(f"«{value}» is not a date (yyyy-mm-dd)") from None


def parameters(report: rd.Report, given: dict[str, Any], pctx: ph.Context, strict: bool = True) -> dict[str, Any]:
    """The parameters' values, converted to their types: the value given (text from a
    query string or a form, or JSON already typed), else the default; ReportError naming
    the parameter when one does not fit or a required one is missing. The choices that
    come from a dataset are the gateway's to check, before any SQL runs. Not `strict`
    (a form still being filled in): a value that does not fit, or is missing, is NULL."""
    out: dict[str, Any] = {}
    for p in report.parameters:
        raw = given.get(p.name, p.default) if given else p.default
        try:
            if p.multi:
                values = raw if isinstance(raw, list) else ([] if raw in (None, "") else [raw])
                value: Any = [v for v in (_one(p, x, pctx) for x in values) if v is not None]
                missing = not value
            else:
                if isinstance(raw, list):
                    raise ValueError("one value is wanted, not a list")
                value = _one(p, raw, pctx)
                missing = value is None
            if p.choices and p.choices.values is not None and not missing:
                allowed = [_one(p, c, pctx) for c in p.choices.values]
                for v in (value if isinstance(value, list) else [value]):
                    if v not in allowed:
                        raise ValueError(f"«{v}» is not one of its choices")
        except ValueError as e:
            if strict:
                raise ReportError(f"parameter «{p.name}»: {e}") from None
            value, missing = None, True
        if missing and p.required and strict:
            raise ReportError(f"parameter «{p.name}» is required")
        out[p.name] = value if not missing else ([] if p.multi else None)
    return out
