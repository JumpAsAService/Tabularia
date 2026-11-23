"""The data CmdStan reads (docs/specs/bayes-contract.md, «The data file»), written in a stream from the input parquet in two passes.

Never `read_table`, never `tab_columns.read_input` (it reads the whole input into pandas, and pandas is not in the image): the input is
read batch by batch (`ParquetFile.iter_batches` over the bound columns alone), and nothing the size of the input is held. What is held:
one batch, the units of each group plate and the levels of each category (bounded by `max_units`), the standardized predictors'
accumulators, a value a unit for the group plates' own columns, and the series' counters.

Pass A reads the bound columns once: which rows are kept (every bound column present and finite; a NULL, a NaN or an infinity leaves a
row out and is counted per column, no imputation), the units and levels, the Welford accumulators of each standardized predictor
(sample standard deviation, n - 1), the checks of the data's shape (the refusals `data_*` and `too_many_units`), the series' counts.
Pass B reads them a second time and appends each variable's values to its own part file, standardized where the manifest says so,
indices as 1…J (units in text order: Python's order of the texts' code points), lags and subsets from the rows' order; then
`data.json` is written by concatenating the parts in the manifest's order, 8 MiB at a time, and the parts are deleted.

A series' rows must come sorted by (the series' unit, time): the gateway appends the engine's `sort`, the runner only checks. The
manifest does not say which plate is the series' unit nor which column is its time (the data entries of a series carry neither): this
reader takes them from the optional `bayes.series` = {"by": a group plate's id or null, "time": a column or null} (absent: one unit,
the time unchecked), and the order of the rows is what it is. Numbers are written as Python writes back a double exactly (`repr`),
integers as integers.
"""
from __future__ import annotations

import math
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from tab_protocol import Refusal, _fs, is_uri
from bayes_task import LEVELS_PREFIX, ROWS, Task, task_value

BATCH_ROWS = 65_536
COPY_CHUNK = 8 * 1024 * 1024
_START = object()  # the unit before the first row


@dataclass
class Facts:
    """What the passes learned: the sizes, the units, the standardization, the counts. Everything the report and the card say of the data."""
    n_input: int = 0
    n: int = 0                                          # the rows kept
    missing: dict[str, int] = field(default_factory=dict)       # column → NULLs and NaNs
    not_finite: dict[str, int] = field(default_factory=dict)    # column → infinities
    rows_missing: int = 0                               # rows left out for a missing value
    rows_not_finite: int = 0                            # rows left out for an infinity (and no missing value)
    units: dict[str, list[str]] = field(default_factory=dict)   # a group plate's id → its units; «levels:<node>» → all K levels
    standardized: dict[str, dict[str, float]] = field(default_factory=dict)   # Stan name → {"mean", "sd"}
    sizes: dict[str, int] = field(default_factory=dict)         # as bayescontract.Sizes, and the size entries' own names too
    series: dict[str, int] = field(default_factory=dict)        # first, next, lag, max_lag
    seconds_pass_a: float = 0.0
    seconds_pass_b: float = 0.0
    data_bytes: int = 0

    @property
    def n_dropped(self) -> int:
        return self.rows_missing + self.rows_not_finite


# ── reading ───────────────────────────────────────────────────────────────────
def iter_batches(path: str, columns: list[str], batch_rows: int = BATCH_ROWS) -> Iterator[pa.RecordBatch]:
    """The input's bound columns, a batch at a time (a path or an s3:// address)."""
    if is_uri(path):
        filesystem, where = _fs(path)
        with filesystem.open_input_file(where) as f:
            yield from pq.ParquetFile(f).iter_batches(batch_size=batch_rows, columns=columns)
    else:
        with pq.ParquetFile(path) as f:
            yield from f.iter_batches(batch_size=batch_rows, columns=columns)


def _numbers(col: pa.ChunkedArray | pa.Array, column: str) -> np.ndarray:
    """A column as float64 with NaN for NULL; a text column is a refusal (`data_shape`)."""
    t = col.type
    if not (pa.types.is_integer(t) or pa.types.is_floating(t) or pa.types.is_boolean(t) or pa.types.is_decimal(t) or pa.types.is_null(t)):
        raise Refusal("data_shape", f"`{column}` must be a number: it holds {t}", {"column": column})
    arr = pc.cast(col, pa.float64())
    return pc.fill_null(arr, math.nan).to_numpy(zero_copy_only=False)


def _texts(col: pa.Array) -> pa.Array:
    return pc.cast(col, pa.string())


def _times(col: pa.Array) -> np.ndarray:
    t = col.type
    if pa.types.is_temporal(t):
        col = pc.cast(pc.cast(col, pa.int64()), pa.float64())
    else:
        col = pc.cast(col, pa.float64())
    return pc.fill_null(col, math.nan).to_numpy(zero_copy_only=False)


@dataclass
class _Plan:
    """The manifest read for the passes."""
    entries: list[dict[str, Any]]
    columns: list[str]                    # the distinct input columns to read (the bound ones, the series' time)
    numeric: list[dict[str, Any]]         # real/int entries (rows or group plate)
    group_index: dict[str, dict[str, Any]]  # a group plate's id → its group_index entry
    group_entries: list[dict[str, Any]]
    categories: list[dict[str, Any]]
    lifts: list[dict[str, Any]]
    max_lag: int
    has_series: bool
    series_by: str | None
    series_time: str | None


def _plan(task: Task) -> _Plan:
    entries = task.data
    group_index = {e["target"]: e for e in entries if e.get("kind") == "group_index"}
    columns: list[str] = []
    for e in entries:
        c = e.get("column")
        if c and c not in columns:
            columns.append(c)
    series_kinds = {"rows_subset", "lag", "first", "next", "previous"}
    has_series = any(e.get("kind") in series_kinds for e in entries)
    lags = [int(e.get("lag") or 0) for e in entries if e.get("kind") == "lag"]
    by = (task.series or {}).get("by")
    time = (task.series or {}).get("time")
    if by is not None and by not in group_index:
        raise task_value(f"bayes.series.by «{by}» has no group index")
    if time and time not in columns and has_series:
        columns.append(time)
    for e in entries:
        if e.get("kind") in ("real", "int") and e.get("plate") != ROWS and e.get("plate") not in group_index:
            raise task_value(f"{e['stan']} is on the plate {e.get('plate')}, which has no group index")
    return _Plan(entries, columns, [e for e in entries if e.get("kind") in ("real", "int")], group_index,
                 [e for e in entries if e.get("kind") == "group_index"],
                 [e for e in entries if e.get("kind") == "category"], [e for e in entries if e.get("kind") == "lift"],
                 max(lags, default=0), has_series, by, time if has_series else None)


class _Parsed:
    """A batch's bound columns, parsed: which rows are valid and the values of the kept rows."""

    def __init__(self, rb: pa.RecordBatch, plan: _Plan, offset: int, count: dict[str, list[int]] | None) -> None:
        self.offset = offset
        n = rb.num_rows
        valid = np.ones(n, dtype=bool)
        missing_any = np.zeros(n, dtype=bool)
        inf_any = np.zeros(n, dtype=bool)
        self.numbers: dict[str, np.ndarray] = {}
        self.texts: dict[str, pa.Array] = {}
        numeric_columns = {e["column"] for e in plan.numeric}
        for c in plan.columns:
            col = rb.column(rb.schema.get_field_index(c))
            if c in numeric_columns or c == plan.series_time:
                x = _numbers(col, c) if c in numeric_columns else _times(col)
                miss, inf = np.isnan(x), np.isinf(x)
                if c in numeric_columns:
                    self.numbers[c] = x
                if c == plan.series_time:
                    self.numbers["\0time"] = x
            else:
                self.texts[c] = _texts(col)
                miss, inf = np.asarray(col.is_null().to_numpy(zero_copy_only=False), dtype=bool), np.zeros(n, dtype=bool)
            if count is not None:
                count.setdefault(c, [0, 0])
                count[c][0] += int(miss.sum())
                count[c][1] += int(inf.sum())
            missing_any |= miss
            inf_any |= inf
            valid &= ~(miss | inf)
        self.valid = valid
        self.rows_missing = int(missing_any.sum())
        self.rows_not_finite = int((inf_any & ~missing_any).sum())
        self.kept = np.flatnonzero(valid)
        self.k = int(valid.sum())

    def number(self, column: str) -> np.ndarray:
        return self.numbers[column][self.valid]

    def text(self, column: str) -> pa.Array:
        return self.texts[column].filter(pa.array(self.valid))

    def rows(self) -> np.ndarray:
        """The 1-based input rows of this batch's kept rows."""
        return self.offset + self.kept + 1

    def input_row(self, i: int) -> int:
        """The 1-based row of the input of the i-th kept row of this batch."""
        return self.offset + int(self.kept[i]) + 1


# ── the series' order ─────────────────────────────────────────────────────────
class _Series:
    """Position of each kept row within its unit, and the checks of the order («data_order», «data_time_twice»)."""

    def __init__(self, max_lag: int) -> None:
        self.max_lag = max_lag
        self.last_unit: Any = _START
        self.last_pos = -1
        self.last_time = math.nan
        self.closed: set[str] = set()
        self.counts = {"first": 0, "next": 0, "lag": 0}

    def step(self, units: np.ndarray | None, times: np.ndarray | None, k: int, rows: np.ndarray) -> np.ndarray:
        """The positions (0 for a unit's first row) of this batch's `k` kept rows; `rows` are their input rows, for the messages."""
        idx = np.arange(k)
        if units is None:
            nu = np.zeros(k, dtype=bool)
            nu[0] = self.last_unit is _START
            last_unit_now: Any = ""
        else:
            prev = np.empty(k, dtype=object)
            prev[0] = self.last_unit
            prev[1:] = units[:-1]
            nu = np.asarray(units != prev, dtype=bool)
            last_unit_now = units[-1]
            for i in np.flatnonzero(nu):
                if prev[i] is not _START:
                    self.closed.add(prev[i])
                if units[i] in self.closed:
                    raise Refusal("data_order", f"the unit «{units[i]}» comes back after another unit at row {int(rows[i])}: the rows "
                                                "must be sorted by unit, then by time", {"unit": str(units[i]), "row": int(rows[i])})
        starts = np.where(nu, idx, -1)
        run = np.maximum.accumulate(starts)
        pos = np.where(run >= 0, idx - run, self.last_pos + 1 + idx)
        if times is not None:
            before = np.concatenate([[self.last_time], times[:-1]])
            bad = np.flatnonzero(~nu & ~np.isnan(before) & (times <= before))
            if bad.size:
                i = int(bad[0])
                unit = str(units[i]) if units is not None else ""
                if times[i] == before[i]:
                    raise Refusal("data_time_twice", f"the unit «{unit}» has two rows at the time {times[i]:g} (row {int(rows[i])})",
                                  {"unit": unit, "row": int(rows[i])})
                raise Refusal("data_order", f"the unit «{unit}» is not in time order at row {int(rows[i])}: the rows must be sorted by "
                                            "unit, then by time", {"unit": unit, "row": int(rows[i])})
            self.last_time = float(times[-1])
        self.last_unit = last_unit_now
        self.last_pos = int(pos[-1])
        return pos


# ── pass A ────────────────────────────────────────────────────────────────────
class _Welford:
    """The mean and the sample variance of the values seen, a batch at a time (Chan's merge of Welford's accumulators)."""

    def __init__(self) -> None:
        self.n, self.mean, self.m2 = 0, 0.0, 0.0
        self.low, self.high = math.inf, -math.inf

    def add(self, x: np.ndarray) -> None:
        if x.size == 0:
            return
        nb, mb = x.size, float(x.mean())
        m2b = float(((x - mb) ** 2).sum())
        delta = mb - self.mean
        n = self.n + nb
        self.m2 += m2b + delta * delta * self.n * nb / n
        self.mean += delta * nb / n
        self.n = n
        self.low, self.high = min(self.low, float(x.min())), max(self.high, float(x.max()))

    @property
    def sd(self) -> float:
        return math.sqrt(self.m2 / (self.n - 1)) if self.n > 1 else 0.0


def _label(task: Task, plate: str | None) -> str:
    for p in task.plates:
        if p.get("id") == plate and p.get("label"):
            return str(p["label"])
    return plate or ""


def pass_a(task: Task, input_path: str, batch_rows: int = BATCH_ROWS, extension_sizes: dict[str, int] | None = None,
           checks: Any = None, run: str = "sample") -> tuple[Facts, dict[str, Any]]:
    """The first read. Returns the facts and the state pass B needs (the accumulators' results, the units of the group plates' columns).

    `checks` is a `bayes_checks.Plan` (lot P3a): it observes the kept rows of the checked outcomes batch by batch and, at the end of the
    pass, gives the extension's sizes and values of `run` (`state["extension_values"]`, which pass B writes unless it is given others)."""
    import time as _time

    t0 = _time.monotonic()
    plan = _plan(task)
    facts = Facts()
    most_units = task.limit("max_units")
    unit_sets: dict[str, set[str]] = {e["target"]: set() for e in plan.entries if e.get("kind") == "group_index"}
    level_sets: dict[str, set[str]] = {e["node"]: set() for e in plan.categories}
    welford = {e["stan"]: _Welford() for e in plan.numeric if e.get("standardize") and e.get("kind") == "real"}
    unit_values: dict[str, dict[str, float]] = {e["stan"]: {} for e in plan.numeric if e.get("plate") != ROWS}
    parents: dict[str, dict[str, str]] = {e["stan"]: {} for e in plan.lifts}
    series = _Series(plan.max_lag) if plan.has_series else None
    counts: dict[str, list[int]] = {}
    for c in plan.columns:
        counts[c] = [0, 0]
    kept_so_far = 0
    offset = 0
    series_units_col = plan.group_index[plan.series_by]["column"] if plan.series_by else None

    def too_many(column: str) -> Refusal:
        return Refusal("too_many_units", f"`{column}` has more than {most_units} values: is it the right column?",
                       {"column": column, "max_units": most_units})

    for rb in iter_batches(input_path, plan.columns, batch_rows):
        p = _Parsed(rb, plan, offset, counts)
        facts.n_input += rb.num_rows
        facts.rows_missing += p.rows_missing
        facts.rows_not_finite += p.rows_not_finite
        if p.k:
            for e in plan.numeric:
                x = p.number(e["column"])
                if e["kind"] == "int":
                    bad = np.flatnonzero(x != np.floor(x))
                    if bad.size == 0 and e.get("support") == "nonnegative":
                        bad = np.flatnonzero(x < 0)
                    if bad.size == 0 and e.get("support") == "binary":
                        bad = np.flatnonzero((x != 0) & (x != 1))
                    if bad.size:
                        what = {"binary": "0 or 1", "nonnegative": "a count"}.get(e.get("support"), "a whole number")
                        raise Refusal("data_shape", f"`{e['column']}` must be {what}: row {p.input_row(int(bad[0]))} holds {x[bad[0]]:g}",
                                      {"column": e["column"], "row": p.input_row(int(bad[0]))})
                elif e["stan"] in welford:
                    if e.get("plate") == ROWS:
                        welford[e["stan"]].add(x)
            for e in plan.group_entries:
                target = e["target"]
                unit_sets[target].update(pc.unique(p.text(e["column"])).to_pylist())
                if len(unit_sets[target]) > most_units:
                    raise too_many(e["column"])
            for e in plan.categories:
                level_sets[e["node"]].update(pc.unique(p.text(e["column"])).to_pylist())
                if len(level_sets[e["node"]]) > most_units:
                    raise too_many(e["column"])
            for e in plan.numeric:
                if e.get("plate") == ROWS:
                    continue
                gcol = plan.group_index[e["plate"]]["column"]
                pairs = pa.table({"u": p.text(gcol), "v": pa.array(p.number(e["column"]))}).group_by(["u", "v"]).aggregate([])
                known = unit_values[e["stan"]]
                for u, v in zip(pairs["u"].to_pylist(), pairs["v"].to_pylist()):
                    if known.setdefault(u, v) != v:
                        raise Refusal("data_unit_varies", f"`{e['column']}` changes within the unit «{u}» of {_label(task, e['plate'])}: "
                                                          "it must have one value for each", {"column": e["column"], "unit": u})
            for e in plan.lifts:
                inner, outer = plan.group_index[e["plate"]]["column"], plan.group_index[e["target"]]["column"]
                pairs = pa.table({"i": p.text(inner), "o": p.text(outer)}).group_by(["i", "o"]).aggregate([])
                known = parents[e["stan"]]
                for i, o in zip(pairs["i"].to_pylist(), pairs["o"].to_pylist()):
                    if known.setdefault(i, o) != o:
                        raise Refusal("data_nesting", f"the unit «{i}» of {_label(task, e['plate'])} is in two units of "
                                                      f"{_label(task, e['target'])}", {"unit": i})
            if series is not None:
                units = np.asarray(p.text(series_units_col).to_pylist(), dtype=object) if series_units_col else None
                times = p.number("\0time") if plan.series_time else None
                pos = series.step(units, times, p.k, p.rows())
                series.counts["first"] += int((pos == 0).sum())
                series.counts["next"] += int((pos > 0).sum())
                series.counts["lag"] += int((pos >= plan.max_lag).sum()) if plan.max_lag else 0
            if checks is not None:
                checks.observe(p, pos if series is not None else None)
        kept_so_far += p.k
        offset += rb.num_rows
    facts.n = kept_so_far
    for c, (m, i) in counts.items():
        facts.missing[c], facts.not_finite[c] = m, i
    if facts.n == 0:
        raise Refusal("data_empty", f"no row has every bound column: {facts.rows_missing} rows have a missing value"
                                    + (f" and {facts.rows_not_finite} an infinity" if facts.rows_not_finite else ""),
                      {"rows_missing": facts.rows_missing, "rows_not_finite": facts.rows_not_finite})
    for target, s in unit_sets.items():
        facts.units[target] = sorted(s)
    for node, s in level_sets.items():
        facts.units[LEVELS_PREFIX + node] = sorted(s)
    for e in plan.numeric:
        if e.get("standardize") and e.get("kind") == "real":
            if e.get("plate") == ROWS:
                w = welford[e["stan"]]
            else:
                w = _Welford()
                order = {u: i for i, u in enumerate(facts.units[e["plate"]])}
                w.add(np.array([v for _, v in sorted(unit_values[e["stan"]].items(), key=lambda kv: order[kv[0]])]))
            if w.high == w.low:
                raise Refusal("data_constant", f"`{e['column']}` has the same value on every row: it cannot be standardized",
                              {"column": e["column"]})
            facts.standardized[e["stan"]] = {"mean": w.mean, "sd": w.sd}
    facts.series = {"first": series.counts["first"], "next": series.counts["next"], "lag": series.counts["lag"],
                    "max_lag": plan.max_lag} if series else {}
    values = None
    if checks is not None:
        checks.finish(facts)
        sizes, values = checks.extension(run, facts)
        extension_sizes = {**(extension_sizes or {}), **sizes}
    facts.sizes = _sizes(task, facts, extension_sizes or {})
    facts.seconds_pass_a = _time.monotonic() - t0
    state = {"plan": plan, "unit_values": unit_values, "parents": parents, "extension_values": values}
    return facts, state


def _sizes(task: Task, facts: Facts, extension: dict[str, int]) -> dict[str, int]:
    """The sizes of `bayescontract.Sizes` («rows», a plate's id, «levels:<category>», the arrays of a series, the extension's integers)
    and, for the size entries, their Stan names too (a dimension may be sized by either)."""
    sizes: dict[str, int] = {ROWS: facts.n, **extension}
    for plate, units in facts.units.items():
        sizes[plate] = len(units)
    sizes["Rows_lag"], sizes["Rows_first"], sizes["Rows_next"] = (facts.series.get("lag", 0), facts.series.get("first", 0),
                                                                  facts.series.get("next", 0))
    for e in task.data:
        if e.get("kind") == "size" and e.get("of") in sizes:
            sizes[e["stan"]] = sizes[e["of"]]
    return sizes


# ── pass B and the data file ──────────────────────────────────────────────────
class _Part:
    """A variable's values, appended to a file as they come: «1,2,3» (no brackets)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.f = open(path, "wb")
        self.empty = True

    def add(self, items: list[str]) -> None:
        if not items:
            return
        self.f.write((("," if not self.empty else "") + ",".join(items)).encode("ascii"))
        self.empty = False

    def close(self) -> None:
        self.f.close()


def _codes(texts: pa.Array, levels: list[str]) -> np.ndarray:
    return pc.index_in(texts, value_set=pa.array(levels, pa.string())).to_numpy(zero_copy_only=False).astype(np.int64) + 1


def pass_b(task: Task, facts: Facts, state: dict[str, Any], input_path: str, workdir: Path, batch_rows: int = BATCH_ROWS,
           extension_values: dict[str, Any] | None = None) -> Path:
    """The second read: appends each variable's values to its part file, then writes data.json. Returns its path."""
    import time as _time

    t0 = _time.monotonic()
    plan: _Plan = state["plan"]
    parts_dir = workdir / "data"
    parts_dir.mkdir(parents=True, exist_ok=True)
    parts: dict[str, _Part] = {}
    series_entries = {k: [e for e in plan.entries if e.get("kind") == k] for k in ("rows_subset", "lag", "first", "next", "previous")}
    for e in plan.entries:
        if e.get("kind") in ("real", "int", "group_index", "category", "rows_subset", "lag", "first", "next", "previous"):
            if e.get("plate") not in (None, ROWS) and e["kind"] in ("real", "int"):
                continue  # one value a unit: written whole, from the units' dictionary
            parts[e["stan"]] = _Part(parts_dir / (e["stan"] + ".part"))
    series = _Series(plan.max_lag) if plan.has_series else None
    series_units_col = plan.group_index[plan.series_by]["column"] if plan.series_by else None
    k0 = 0
    offset = 0
    try:
        for rb in iter_batches(input_path, plan.columns, batch_rows):
            p = _Parsed(rb, plan, offset, None)
            offset += rb.num_rows
            if not p.k:
                continue
            for e in plan.numeric:
                if e["stan"] not in parts:
                    continue
                x = p.number(e["column"])
                if e["kind"] == "int":
                    parts[e["stan"]].add([str(v) for v in x.astype(np.int64).tolist()])
                else:
                    s = facts.standardized.get(e["stan"])
                    if s:
                        x = (x - s["mean"]) / s["sd"]
                    parts[e["stan"]].add([repr(v) for v in x.tolist()])
            for e in plan.group_entries:
                parts[e["stan"]].add([str(v) for v in _codes(p.text(e["column"]), facts.units[e["target"]]).tolist()])
            for e in plan.categories:
                parts[e["stan"]].add([str(v) for v in _codes(p.text(e["column"]), facts.units[LEVELS_PREFIX + e["node"]]).tolist()])
            if series is not None:
                units = np.asarray(p.text(series_units_col).to_pylist(), dtype=object) if series_units_col else None
                times = p.number("\0time") if plan.series_time else None
                pos = series.step(units, times, p.k, p.rows())
                g = k0 + 1 + np.arange(p.k)
                for e in series_entries["first"]:
                    parts[e["stan"]].add([str(v) for v in g[pos == 0].tolist()])
                for e in series_entries["next"]:
                    parts[e["stan"]].add([str(v) for v in g[pos > 0].tolist()])
                for e in series_entries["previous"]:
                    parts[e["stan"]].add([str(v) for v in (g[pos > 0] - 1).tolist()])
                if plan.max_lag:
                    kept = pos >= plan.max_lag
                    for e in series_entries["rows_subset"]:
                        parts[e["stan"]].add([str(v) for v in g[kept].tolist()])
                    for e in series_entries["lag"]:
                        parts[e["stan"]].add([str(v) for v in (g[kept] - int(e["lag"])).tolist()])
            k0 += p.k
    finally:
        for part in parts.values():
            part.close()
    path = workdir / "data.json"
    _assemble(task, facts, state, parts, path, extension_values if extension_values is not None else state.get("extension_values") or {})
    shutil.rmtree(parts_dir, ignore_errors=True)
    facts.seconds_pass_b = _time.monotonic() - t0
    facts.data_bytes = path.stat().st_size
    return path


def _scalar_json(v: Any) -> str:
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float) and math.isfinite(v):
        return repr(v)
    raise ValueError(f"not a number a data file can hold: {v!r}")


def _array_json(values: Any) -> str:
    return "[" + ",".join(_scalar_json(v) for v in values) + "]"


def _assemble(task: Task, facts: Facts, state: dict[str, Any], parts: dict[str, _Part], path: Path, extension_values: dict[str, Any]) -> None:
    """data.json: the variables in the manifest's order, the parts concatenated 8 MiB at a time."""
    plan: _Plan = state["plan"]
    with open(path, "wb") as out:
        out.write(b"{")
        first = True
        for e in plan.entries:
            kind, stan = e["kind"], e["stan"]
            if not first:
                out.write(b",\n")
            first = False
            out.write(f'"{stan}": '.encode("ascii"))
            if stan in parts:
                out.write(b"[")
                with open(parts[stan].path, "rb") as f:
                    while chunk := f.read(COPY_CHUNK):
                        out.write(chunk)
                out.write(b"]")
            elif kind == "size":
                if e["of"] not in facts.sizes:
                    raise Refusal("task_value", f"the size {stan} counts {e['of']}, which this runner does not know: a fault of Tabularia",
                                  {"what": stan})
                out.write(_scalar_json(facts.sizes[e["of"]]).encode("ascii"))
            elif kind == "mode":
                out.write(b"1" if task.mode == "prior_predictive" else b"0")
            elif kind in ("real", "int"):  # one value a unit
                values = state["unit_values"][stan]
                s = facts.standardized.get(stan)
                units = facts.units[e["plate"]]
                x = [values[u] for u in units]
                if kind == "int":
                    x = [int(v) for v in x]
                elif s:
                    x = [(v - s["mean"]) / s["sd"] for v in x]
                out.write(_array_json(x).encode("ascii"))
            elif kind == "lift":
                parents = state["parents"][stan]
                outer = {u: i + 1 for i, u in enumerate(facts.units[e["target"]])}
                out.write(_array_json([outer[parents[u]] for u in facts.units[e["plate"]]]).encode("ascii"))
            elif stan in extension_values:
                v = extension_values[stan]
                if hasattr(v, "pieces"):  # an array the size of the input (bayes_checks.Stream), written in pieces
                    out.write(b"[")
                    for piece in v.pieces(parts):
                        out.write(piece)
                    out.write(b"]")
                else:
                    out.write((_array_json(v) if isinstance(v, (list, tuple, np.ndarray)) else _scalar_json(v)).encode("ascii"))
            else:
                raise Refusal("task_value", f"the data variable {stan} (kind {kind}) is not one this runner can build: a fault of "
                                            "Tabularia, not of the model", {"what": stan})
        out.write(b"}\n")


def prepare(task: Task, input_path: str, workdir: Path, batch_rows: int = BATCH_ROWS, extension_sizes: dict[str, int] | None = None,
            extension_values: dict[str, Any] | None = None, checks: Any = None, run: str = "sample") -> Facts:
    """Both passes: the facts, and /<workdir>/data.json written. A refusal leaves whatever was written under `workdir` for the caller
    to delete («a refusal leaves no partial result»)."""
    facts, state = pass_a(task, input_path, batch_rows, extension_sizes, checks, run)
    pass_b(task, facts, state, input_path, workdir, batch_rows, extension_values)
    return facts
