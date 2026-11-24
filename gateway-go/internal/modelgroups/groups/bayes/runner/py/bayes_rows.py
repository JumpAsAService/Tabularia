"""The results of the group «bayes»: the statistics' ten columns, written in a stream (docs/specs/bayes-contract.md, «The results»), and
the rows this lot owns: the sections `model`, `data` and `quantity` as far as the passes and the mapping know them. The summary, the
sampler's facts, the charts and the checks are the rows of the lots after this one (bayes-diagnostics, predictive-checks); they use
the same writer.

`tab_results.Rows` keeps every row in Python lists and `tab_results.write` writes one whole table: here a ResultsWriter holds at most
`flush_rows` rows, appends them to the file as one row group (`ord` continuing from the rows before) and starts again. The table is
bounded by construction (`max_result_rows`, 96,690 at the catalogue's caps): the writer refuses to pass it.

Keys are the section, level, level_2 and stat joined by «|», NOT escaped: the engine's check of a model's results
(app/engine/models/results.py) wants `key == "|".join(parts)` and finds a unit named «a|b» by its own label, which the spec's
«escaped part by part» would break (the keys of the statistics' nodes are escaped, the models' are not). A duplicate key is a bug of the
runner, never a person's error: the writer raises RuntimeError.

When it is given the variant's declaration (`declaration` = the catalogue's `variants[variant]`, and the section shapes) the writer
checks each flushed row the way the engine's `check_declared` does, so a runner's own tests see a wrong row first.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

import tab_results
from tab_card import versions
from tab_protocol import is_uri, _fs
from bayes_data import Facts
from bayes_task import Entry, Layout, Task, note_line, report_order

REQUIRED_KEYS = ("model|||model", "model|||variant", "model|||n")
FLUSH_ROWS = 4096
_ORD = 8  # the position of `ord` among the ten columns


def _declared(stat: str, names: Iterable[str] | None) -> bool:
    names = list(names or [])
    return stat in names or any(n.endswith("*") and stat.startswith(n[:-1]) for n in names)


class ResultsWriter:
    """Appends the ten columns' rows to `path` (a path, or an s3:// address) a row group at a time."""

    def __init__(self, path: str, max_rows: int = 100_000, flush_rows: int = FLUSH_ROWS,
                 declaration: dict[str, Any] | None = None, shapes: dict[str, Any] | None = None) -> None:
        self.path = path
        self.max_rows = max_rows
        self.flush_rows = flush_rows
        self.declaration = declaration
        self.shapes = shapes or {}
        self.rows = tab_results.Rows()
        self.total = 0
        self._keys: set[str] = set()
        self._sink: Any = None
        if is_uri(path):
            filesystem, where = _fs(path)
            self._sink = filesystem.open_output_stream(where)
            self._file = pq.ParquetWriter(self._sink, tab_results.schema(), compression="zstd")
        else:
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            self._file = pq.ParquetWriter(path, tab_results.schema(), compression="zstd")
        self._closed = False

    # the rows are added with tab_results' own functions on `self.rows`
    def number(self, section: str, stat: str, value: Any, level: Any = None, level_2: Any = None) -> None:
        tab_results.number(self.rows, section, stat, value, level, level_2)
        self._after()

    def text(self, section: str, stat: str, value: Any, level: Any = None, level_2: Any = None) -> None:
        tab_results.text(self.rows, section, stat, value, level, level_2)
        self._after()

    def points(self, section: str, level: str, stat: str, x: Any, y: Any, level_2: Any = None, labels: Any = None, x1: Any = None) -> None:
        tab_results.points(self.rows, section, level, stat, x, y, level_2, labels, x1)
        self._after()

    def note(self, note_id: str, sentence: str) -> None:
        tab_results.note(self.rows, note_id, sentence)
        self._after()

    def _after(self) -> None:
        if self.total + len(self.rows) > self.max_rows:
            raise RuntimeError(f"the results would pass {self.max_rows} rows: a cap of the catalogue is wrong")
        if len(self.rows) >= self.flush_rows:
            self.flush()

    def flush(self) -> None:
        n = len(self.rows)
        if n == 0:
            return
        self._check(self.rows)
        t = tab_results.table(self.rows)
        t = t.set_column(_ORD, tab_results.schema().field("ord"), pa.array(np.arange(self.total, self.total + n, dtype=np.int64)))
        self._file.write_table(t, row_group_size=n)
        self.total += n
        self.rows = tab_results.Rows()

    def _check(self, rows: tab_results.Rows) -> None:
        for i in range(len(rows)):
            key = rows.key[i]
            if key in self._keys:
                raise RuntimeError(f"the results hold the key {key} twice")
            self._keys.add(key)
            if self.declaration is None:
                continue
            section, stat, where = rows.section[i], rows.stat[i], f"the row {key}"
            decl = self.declaration.get(section)
            shape = self.shapes.get(section)
            if decl is None or shape is None:
                raise RuntimeError(f"{where}: the variant has no section {section}")
            for part, v in (("level", rows.level[i]), ("level_2", rows.level_2[i])):
                if shape[part] == "null" and v is not None:
                    raise RuntimeError(f"{where}: {part} must be null")
                if shape[part] == "required" and not v:
                    raise RuntimeError(f"{where}: {part} is required")
            if decl.get("levels") is not None and rows.level[i] not in decl["levels"]:
                raise RuntimeError(f"{where}: {rows.level[i]} is not one of the declared {section} rows")
            if _declared(stat, decl.get("numbers")):
                if rows.text[i] is not None and section not in ("point", "curve"):
                    raise RuntimeError(f"{where}: a number's text must be null")
            elif _declared(stat, decl.get("texts")):
                if not np.isnan(rows.value[i]):
                    raise RuntimeError(f"{where}: a text's value must be null")
            else:
                raise RuntimeError(f"{where}: the variant declares no {stat} in {section}")

    def close(self) -> int:
        """Flushes, checks the rows every model writes are there, closes; returns the number of rows."""
        self.flush()
        missing = [k for k in REQUIRED_KEYS if k not in self._keys]
        if missing:
            self.abort()
            raise RuntimeError(f"the row {missing[0]} is missing")
        self._file.close()
        if self._sink is not None:
            self._sink.close()
        self._closed = True
        return self.total

    def abort(self) -> None:
        if not self._closed:
            try:
                self._file.close()
                if self._sink is not None:
                    self._sink.close()
            except Exception:  # noqa: BLE001 — it is being thrown away
                pass
            self._closed = True
        if not is_uri(self.path):
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


# ── the sections this lot knows ───────────────────────────────────────────────
def software(task: Task) -> str:
    """«CmdStan 2.40.0; stanc3 v2.40.0; Python 3.14.x; numpy 2.5.5; pyarrow 26.0.1»."""
    v = versions(("numpy", "pyarrow"))
    cmdstan = task.model.get("cmdstan") or ""
    parts = [f"CmdStan {cmdstan}", f"stanc3 v{cmdstan}"] if cmdstan else []
    parts += [f"{k} {x}" for k, x in v.items()]
    return "; ".join(parts)


def model_section(w: ResultsWriter, task: Task, facts: Facts, layout: Layout, threads: int, seconds: dict[str, float] | None = None) -> None:
    """The `model` section: texts and numbers the passes know. `seconds`: warmup, sampling, conversion, pass_2 (the lots after this one
    fill them as they measure them; the data's own pass is facts')."""
    s, raw, m = task.sampler or {}, task.raw, task.model
    w.text("model", "model", raw.get("model"))
    w.text("model", "variant", task.variant)
    w.text("model", "rows_from", raw.get("rows_from") or "run")
    gv = raw.get("group_version")
    w.text("model", "group_version", "dev" if gv is None else str(gv))
    w.text("model", "software", software(task))
    w.text("model", "bayes_model", str(m.get("id")) if m.get("id") is not None else None)
    w.text("model", "bayes_version", str(m.get("version")) if m.get("version") is not None else None)
    w.text("model", "semantic_hash", m.get("semantic_hash"))
    w.text("model", "program_sha256", m.get("program_sha256"))
    w.text("model", "binary_sha256", m.get("binary_sha256"))
    w.text("model", "sampler", task.algorithm)
    if task.mode != "prior_predictive":      # fixed_param has no adaptation, so no metric
        w.text("model", "metric", "diag_e")
    w.text("model", "precision", "8 significant digits")
    w.number("model", "n", facts.n)
    w.number("model", "n_missing", facts.rows_missing)
    w.number("model", "n_not_finite", facts.rows_not_finite)
    for stat in ("chains", "warmup", "draws", "seed", "adapt_delta", "max_treedepth"):
        w.number("model", stat, s.get(stat))
    w.number("model", "threads", threads)
    w.number("model", "n_quantities", layout.n_quantities)
    w.number("model", "seconds_data", facts.seconds_pass_a + facts.seconds_pass_b)
    for stat, value in (seconds or {}).items():
        w.number("model", f"seconds_{stat}", value)


def data_section(w: ResultsWriter, task: Task, facts: Facts) -> None:
    """The `data` section: one level a bound variable (the data identifier)."""
    bound = [e for e in task.data if e.get("kind") in ("real", "int", "group_index", "category")]
    for e in bound:  # at most 200 nodes: their rows fit the caps by construction
        stan, kind = e["stan"], e["kind"]
        w.text("data", "column", e.get("column"), level=stan)
        w.text("data", "type", e.get("support") if kind == "int" else kind, level=stan)
        std = facts.standardized.get(stan)
        w.text("data", "standardized", "yes" if std else "no", level=stan)
        if std:
            w.number("data", "mean", std["mean"], level=stan)
            w.number("data", "sd", std["sd"], level=stan)
        if kind == "group_index":
            w.number("data", "n_units", len(facts.units.get(e["target"], [])), level=stan)
        if kind == "category":
            levels = facts.units.get("levels:" + e["node"], [])
            w.number("data", "n_levels", len(levels), level=stan)
            w.text("data", "reference", levels[0] if levels else None, level=stan)
        w.number("data", "n_missing", facts.missing.get(e.get("column"), 0), level=stan)


def quantity_section(w: ResultsWriter, task: Task, layout: Layout) -> list[Entry]:
    """The `quantity` section (answer 5's mapping): one level a quantity entry (a Stan variable of role quantity: a node, each component
    of an effect, a correlation), in the report's order, up to `bayes_max_quantity_entries`; the rest is counted in
    `note_quantities_capped`. Returns the entries written (the diagnostics add the measures to them)."""
    entries = report_order(layout.entries)
    cap = task.limit("bayes_max_quantity_entries")
    shown = entries[:cap]
    for e in shown:
        w.text("quantity", "label", e.label, level=e.stan)
        w.text("quantity", "name", e.name, level=e.stan)
        w.text("quantity", "node_id", e.node, level=e.stan)
        w.text("quantity", "kind", e.kind, level=e.stan)
        w.text("quantity", "plate", e.plate, level=e.stan)
        w.text("quantity", "plate_label", e.plate_label, level=e.stan)
        w.number("quantity", "n_quantities", e.count, level=e.stan)
    if len(entries) > len(shown):
        w.note("quantities_capped", note_line("quantities_capped", listed=len(shown), total=len(entries)))
    return shown
