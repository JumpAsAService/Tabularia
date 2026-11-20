"""The results (the twin of common/R/results.R): the statistics' ten columns (section, level,
level_2, stat, value, text, x0, x1, ord, key), one row per number or text, in the order written.
A runner adds rows with add, number, text, p_value (a p-value and R's stars after it), points
(the points of a chart) and note; frame gives a pandas frame, table the pyarrow table with the
exact types (string, double, int64) and write the Parquet file. The engine checks the file
(app/engine/models/results.py) against the catalogue's declaration for the variant;
check_declared runs the same check here, so a runner's own tests see it first.
"""
from __future__ import annotations

import os
import sys
from typing import Any, Iterable, Sequence

import numpy as np

from tab_protocol import ENV_RESULTS, catalog, variant_spec

COLUMNS = ["section", "level", "level_2", "stat", "value", "text", "x0", "x1", "ord", "key"]
_SMALLEST_NORMAL = sys.float_info.min


class Rows:
    """The rows of a results table, as they are added."""

    def __init__(self) -> None:
        self.section: list[str] = []
        self.level: list[str | None] = []
        self.level_2: list[str | None] = []
        self.stat: list[str] = []
        self.value: list[float] = []
        self.text: list[str | None] = []
        self.x0: list[float] = []
        self.x1: list[float] = []
        self.key: list[str] = []

    def __len__(self) -> int:
        return len(self.key)


def finite(v: Any) -> np.ndarray:
    """Numbers as the table holds them (tab_finite): NaN for what is not finite (written NULL);
    −0 is 0; below the smallest normal double, 0 (the engine's rule: some SQL parsers refuse a
    subnormal literal)."""
    a = np.array([np.nan if x is None else x for x in np.atleast_1d(np.asarray(v, dtype=object)).ravel()], dtype=float)
    a[~np.isfinite(a)] = np.nan
    a[np.abs(a) < _SMALLEST_NORMAL] = 0.0
    return a + 0.0


def _seq(x: Any) -> list[Any] | None:
    """A vector argument as a list, None for a scalar (repeated)."""
    if isinstance(x, (str, bytes)) or x is None or np.isscalar(x):
        return None
    return list(np.asarray(x, dtype=object).ravel()) if isinstance(x, np.ndarray) else list(x)


def _text(x: Any) -> str | None:
    if x is None:
        return None
    if isinstance(x, float) and np.isnan(x):
        return None
    return str(x)


def add(rows: Rows, section: str, stat: str | Sequence[str], value: Any = None, text: Any = None, level: Any = None,
        level_2: Any = None, x0: Any = None, x1: Any = None, index: Any = None) -> Rows:
    """Adds rows (sequences of equal length, or one value repeated). `index`: the points of a
    chart, whose key ends with «|index»."""
    args = {"stat": stat, "value": value, "text": text, "level": level, "level_2": level_2, "x0": x0, "x1": x1, "index": index}
    seqs = {k: _seq(v) for k, v in args.items()}
    lengths = {len(s) for s in seqs.values() if s is not None}
    if len(lengths) > 1:
        raise ValueError(f"rows of different lengths: {sorted(lengths)}")
    n = lengths.pop() if lengths else 1
    if n == 0:
        return rows

    def col(name: str) -> list[Any]:
        s = seqs[name]
        return s if s is not None else [args[name]] * n

    stats, levels, levels_2, texts = col("stat"), col("level"), col("level_2"), col("text")
    values = finite(col("value"))
    xs0, xs1 = finite(col("x0")), finite(col("x1"))
    indices = col("index")
    for i in range(n):
        lv, lv2, st = _text(levels[i]), _text(levels_2[i]), str(stats[i])
        key = "|".join((section, lv or "", lv2 or "", st))
        if index is not None:
            key += f"|{int(indices[i])}"
        rows.section.append(section)
        rows.level.append(lv)
        rows.level_2.append(lv2)
        rows.stat.append(st)
        rows.value.append(float(values[i]))
        rows.text.append(_text(texts[i]))
        rows.x0.append(float(xs0[i]))
        rows.x1.append(float(xs1[i]))
        rows.key.append(key)
    return rows


def number(rows: Rows, section: str, stat: Any, value: Any, level: Any = None, level_2: Any = None) -> Rows:
    return add(rows, section, stat, value=value, level=level, level_2=level_2)


def text(rows: Rows, section: str, stat: Any, value: Any, level: Any = None, level_2: Any = None) -> Rows:
    """A text row; nothing for a missing text."""
    first = value[0] if _seq(value) else value
    if _text(first) is None:
        return rows
    return add(rows, section, stat, text=value, level=level, level_2=level_2)


def significance(p: float | None) -> str | None:
    """R's significance code of a p-value (summary.lm's stars); None for no p-value."""
    if p is None or not np.isfinite(p):
        return None
    return "***" if p < 0.001 else "**" if p < 0.01 else "*" if p < 0.05 else "." if p < 0.1 else ""


def p_value(rows: Rows, section: str, value: Any, stat: str = "p_value", level: Any = None, level_2: Any = None) -> Rows:
    """A p-value and, after it, its significance: `stat` = "p_value" → "significance",
    "lr_p_value" → "lr_significance". With sequences each p-value is followed by its own code."""
    values = _seq(value) or [value]
    lv, lv2 = _seq(level), _seq(level_2)
    for i, p in enumerate(values):
        a = lv[i] if lv is not None else level
        b = lv2[i] if lv2 is not None else level_2
        number(rows, section, stat, p, a, b)
        code = significance(float(finite(p)[0]))
        if code is not None:
            add(rows, section, stat[: -len("p_value")] + "significance", text=code, level=a, level_2=b)
    return rows


def points(rows: Rows, section: str, level: str, stat: Any, x: Any, y: Any, level_2: Any = None, labels: Any = None,
           x1: Any = None) -> Rows:
    """The points of a chart: one row each, `x` in x0, `y` in value, an optional label in text (a
    row number), `x1` for a histogram's right edges. The index counts from 0 per call."""
    n = len(np.atleast_1d(np.asarray(y, dtype=object)))
    if n == 0:
        return rows
    return add(rows, section, stat, value=y, text=labels, level=level, level_2=level_2, x0=x, x1=x1, index=list(range(n)))


def rows_start(task: dict[str, Any]) -> Rows:
    """The rows every model starts with: its id, its variant, where the rows came from, the
    group's version."""
    rows = Rows()
    text(rows, "model", "model", task.get("model"))
    text(rows, "model", "variant", task.get("variant"))
    text(rows, "model", "rows_from", task.get("rows_from") or "run")
    gv = task.get("group_version")
    text(rows, "model", "group_version", "dev" if gv is None else str(gv))
    return rows


def note(rows: Rows, note_id: str, sentence: str) -> Rows:
    """A note: the row `note_<id>` of the model section, an English sentence that names the way
    out (the id names the doubt: note_time_budget, note_shap_sampled…)."""
    return text(rows, "model", f"note_{note_id}", sentence)


# ── the table ─────────────────────────────────────────────────────────────────
def frame(rows: Rows):
    """The rows as a pandas frame of the ten columns (NaN where a number is NULL)."""
    import pandas as pd

    return pd.DataFrame({
        "section": pd.Series(rows.section, dtype=object), "level": pd.Series(rows.level, dtype=object),
        "level_2": pd.Series(rows.level_2, dtype=object), "stat": pd.Series(rows.stat, dtype=object),
        "value": np.asarray(rows.value, dtype=float), "text": pd.Series(rows.text, dtype=object),
        "x0": np.asarray(rows.x0, dtype=float), "x1": np.asarray(rows.x1, dtype=float),
        "ord": np.arange(len(rows), dtype=np.int64), "key": pd.Series(rows.key, dtype=object),
    })[COLUMNS]


def schema():
    """tab_results_schema: section, level, level_2, stat, text, key utf8; value, x0, x1 float64;
    ord int64."""
    import pyarrow as pa

    return pa.schema([("section", pa.string()), ("level", pa.string()), ("level_2", pa.string()), ("stat", pa.string()),
                      ("value", pa.float64()), ("text", pa.string()), ("x0", pa.float64()), ("x1", pa.float64()),
                      ("ord", pa.int64()), ("key", pa.string())])


def table(rows: Rows):
    """The rows as a pyarrow table with the exact types, NULL where a number is not finite."""
    import pyarrow as pa

    def nums(xs: list[float]) -> Any:
        a = np.asarray(xs, dtype=float)
        return pa.array(a, type=pa.float64(), mask=np.isnan(a))

    return pa.table([pa.array(rows.section, pa.string()), pa.array(rows.level, pa.string()),
                     pa.array(rows.level_2, pa.string()), pa.array(rows.stat, pa.string()), nums(rows.value),
                     pa.array(rows.text, pa.string()), nums(rows.x0), nums(rows.x1),
                     pa.array(np.arange(len(rows), dtype=np.int64), pa.int64()), pa.array(rows.key, pa.string())],
                    schema=schema())


def write(rows: Rows, path: str | None = None) -> int:
    """The results' Parquet file: TABULARIA_RESULTS, a path or an s3:// address."""
    import pyarrow.parquet as pq

    path = path if path is not None else os.environ.get(ENV_RESULTS, "")
    if not path:
        raise RuntimeError("TABULARIA_RESULTS is empty")
    pq.write_table(table(rows), path)
    return len(rows)


# ── the catalogue's declaration, checked here as the engine checks it ──────────
def _declared(stat: str, names: Iterable[str] | None) -> bool:
    names = list(names or [])
    return stat in names or any(n.endswith("*") and stat.startswith(n[:-1]) for n in names)


def check_declared(rows: Rows, model: str, variant: str) -> list[str]:
    """What is wrong with `rows` for the model's variant, one sentence each (empty: nothing)."""
    cat = catalog()
    spec = variant_spec(model, variant)
    if spec is None:
        return [f"the catalogue has no variant {variant} of {model}"]
    problems: list[str] = []
    seen: set[str] = set()
    for i in range(len(rows)):
        key = rows.key[i]
        if key in seen:
            problems.append(f"the key {key} is there twice")
        seen.add(key)
        section, stat = rows.section[i], rows.stat[i]
        where = f"the row {key}"
        shape = cat["section_shapes"].get(section)
        decl = spec["sections"].get(section)
        if shape is None or decl is None:
            problems.append(f"{where}: the variant {variant} of {model} has no section {section}")
            continue
        for part, v in (("level", rows.level[i]), ("level_2", rows.level_2[i])):
            if shape[part] == "null" and v is not None:
                problems.append(f"{where}: {part} must be null")
            if shape[part] == "required" and not v:
                problems.append(f"{where}: {part} is required")
        if decl.get("levels") is not None and rows.level[i] not in decl["levels"]:
            problems.append(f"{where}: {rows.level[i]} is not one of the declared {section} rows")
        if _declared(stat, decl.get("numbers")):
            if rows.text[i] is not None and section not in ("point", "curve"):
                problems.append(f"{where}: a number's text must be null")
        elif _declared(stat, decl.get("texts")):
            if not np.isnan(rows.value[i]):
                problems.append(f"{where}: a text's value must be null")
            if section == "assumption" and stat == "verdict" and rows.text[i] not in cat["verdicts"]:
                problems.append(f"{where}: the verdict «{rows.text[i]}» is not one of {', '.join(cat['verdicts'])}")
        else:
            problems.append(f"{where}: {model} ({variant}) declares no {stat} in {section}")
    for key in ("model|||model", "model|||variant", "model|||n"):
        if key not in seen:
            problems.append(f"the row {key} is missing")
    return problems
