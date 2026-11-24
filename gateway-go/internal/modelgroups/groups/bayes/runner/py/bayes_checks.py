"""The predictive checks' side of the data passes (docs/specs/bayes-predictive-checks.md, «The runner's side»; lot P3a).

Two things, both read from the program's data manifest (`Ppc_o<k>_*`, `Prior_o<k>_*`, `Prior_first…`: the kinds `checks_on`, `post_count`, …),
never guessed:

1. **The observations** of each checked outcome, taken in pass A: `plan.observe(batch, pos)` is called with every batch of kept rows (a
   `bayes_data._Parsed` and the rows' positions in their series), `plan.finish(facts)` at the end of the pass. What is held meanwhile is
   what the spec's exception allows: the outcome's values as float64 (8 bytes a row) and each row's unit as a code (4 bytes a row); the
   quantiles need the values. `finish` computes what the checks compare with, then **frees both**: T(y) (`Outcome.observed`), the bins
   (`Outcome.bins`: the edges, the scale and the shift Stan reads), the observed counts of each slot, the checked units' rows and sums, and
   each row's unit slot (uint16, 2 bytes a row) which the data files of the runs that bin write.
2. **The extension's values** of each run: `sizes, values = plan.extension(run, facts)`, `run` in `sample` (the checks off: the sampling
   run), `checks` (the posterior's `generate_quantities`), `replicated` (its second run, the output `replicated`), `prior` (the prior
   predictive). `sizes` are the integers a dimension may be sized by (`Ppc_on`, `Ppc_o1_slots`, …: the manifests' `{"size": …}`), `values`
   every extension entry of the data manifest by Stan name: a number, or an object with `pieces(parts)` for the arrays the size of the
   input (`Counting`, `Slots`, `CopyOf`), which `bayes_data` writes in pieces, never as a list. The invariants of the program (P2's notes)
   hold: `Ppc_on` 1 ⇒ at least 2 rows replicated and `slots` = bins + 2; `binned` > 0 only with `Ppc_on` 1; `rep` > 0 only with `Ppc_on` 0.

The positions of `Ppc_o<k>_rows` are the statement's rows: 1…N, or 1…N_lag when the outcome's likelihood runs on the rows that have every
lag. The manifest does not say which likelihood is lagged: this reader takes every outcome as lagged when the data manifest holds a
`rows_subset` entry (a model's lagged likelihoods and its others on the same plate are not told apart yet: the codegen's `lagged` map is not
in the manifests).

The bins (the spec's «Decisions»): slot 1 is «below», slots 2…B+1 the bins, slot B+2 «above»; slot = clamp(floor(y · scale + shift), 1, B + 2)
with scale = 1 / width, shift = 2 − lo / width, **computed here with the same expression** so that the observed counts and Stan's agree to the
bit. Continuous: `value_bins` (40) equal bins over the observed range, widened by a millionth on each side so that the minimum and the maximum
fall inside (a constant outcome gets one unit of range). Counts: bins of a whole width from −0.5 covering the maximum in at most 40. Binary:
two bins (0 and 1). The checked units: the `max_units` (8) of the outcome's first group plate with the most rows (ties in text order), in that
order, slots 1…U; the others together in slot U + 1 (present only when there are others).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc

from tab_protocol import Refusal
from bayes_task import ROWS, Task, task_value

RUNS = ("sample", "checks", "replicated", "prior")
EXTENSION_KINDS = ("checks_on", "post_count", "post_rows", "prior_count", "prior_rows", "replicated_count", "bin_slots", "bin_scale",
                   "bin_shift", "binned_count", "unit_count", "unit_slot", "replicated_rows", "first_count", "prior_first", "next_count",
                   "prior_next", "prior_previous")
_OUTCOME_OF = re.compile(r"^(?:Ppc|Prior)_o([0-9]+)_")
_NOT_STATISTICS = ("bin_count", "unit_sum", "replicated")
_QUANTILES = {"q10": 0.1, "q50": 0.5, "q90": 0.9}
PIECE = 1 << 18          # values in a piece of an array written to the data file
COPY_CHUNK = 8 * 1024 * 1024
DEFAULT_VALUE_BINS = 40
DEFAULT_MAX_UNITS = 8


# ── arrays the size of the input, written in pieces ───────────────────────────
class Stream:
    """An integer array of the data file, never held as a list: `pieces(parts)` yields the bytes between the brackets."""
    length = 0

    def pieces(self, parts: dict[str, Any]) -> Iterator[bytes]:  # pragma: no cover - abstract
        raise NotImplementedError

    def to_list(self, parts: dict[str, Any] | None = None) -> list[int]:
        """The whole array (for tests and for small ones)."""
        text = b"".join(self.pieces(parts or {})).decode("ascii")
        return [int(v) for v in text.split(",")] if text else []


class Counting(Stream):
    """1, 2, …, n: the positions of every statement's row."""

    def __init__(self, n: int) -> None:
        self.length = n

    def pieces(self, parts: dict[str, Any]) -> Iterator[bytes]:
        for start in range(1, self.length + 1, PIECE):
            yield (("," if start > 1 else "") + ",".join(map(str, range(start, min(start + PIECE, self.length + 1))))).encode("ascii")


class Slots(Stream):
    """Each statement row's unit slot, held as uint16 (2 bytes a row)."""

    def __init__(self, slots: np.ndarray) -> None:
        self.slots, self.length = slots, int(slots.size)

    def pieces(self, parts: dict[str, Any]) -> Iterator[bytes]:
        for start in range(0, self.length, PIECE):
            yield (("," if start else "") + ",".join(map(str, self.slots[start:start + PIECE].tolist()))).encode("ascii")


class CopyOf(Stream):
    """A copy of another array of the data file (the series' rows the prior predictive's states read), from its part file."""

    def __init__(self, stan: str, length: int) -> None:
        self.stan, self.length = stan, length

    def pieces(self, parts: dict[str, Any]) -> Iterator[bytes]:
        part = parts.get(self.stan)
        if part is None:
            raise Refusal("task_value", f"the array {self.stan} is not one this run holds: a fault of Tabularia", {"what": self.stan})
        with open(part.path, "rb") as f:
            while chunk := f.read(COPY_CHUNK):
                yield chunk


# ── what the plan knows of an outcome ─────────────────────────────────────────
@dataclass(frozen=True)
class Bins:
    """The replicated values' bins: slot 1 below the range, 2…count + 1 the bins, count + 2 above."""
    lo: float
    width: float
    count: int

    @property
    def slots(self) -> int:
        return self.count + 2

    @property
    def scale(self) -> float:
        return 1.0 / self.width

    @property
    def shift(self) -> float:
        return 2.0 - self.lo / self.width

    def edges(self) -> np.ndarray:
        """The count + 1 edges of the bins (the outer slots are open)."""
        return self.lo + self.width * np.arange(self.count + 1)

    def slot_of(self, y: np.ndarray) -> np.ndarray:
        """Stan's `to_int(fmin(fmax(floor(y * scale + shift), 1.0), slots))`, 1-based."""
        return np.minimum(np.maximum(np.floor(y * self.scale + self.shift), 1.0), float(self.slots)).astype(np.int64)


@dataclass
class Outcome:
    """A checked outcome (`Ppc_o<k>_*`), what the manifests say of it and, after `finish`, what was observed."""
    k: int
    node: str                      # the observed data node (the entries' `node`)
    stan: str                      # its Stan identifier: the `<y>` of the checks' keys
    column: str
    kind: str                      # continuous | counts | binary, from the statistics the program writes
    statistics: list[str]
    standardize: bool
    lagged: bool
    unit_node: str | None = None   # the group index (a data node) of the outcome's first group plate
    unit_column: str | None = None
    unit_plate: str | None = None
    # observed, after finish
    n: int = 0                              # the statement's rows
    bins: Bins | None = None
    observed: dict[str, float] = field(default_factory=dict)       # T(y), by statistic
    counts: np.ndarray | None = None                               # the observed rows in each slot (length bins.slots)
    unit_texts: list[str] = field(default_factory=list)            # the checked units, slot 1…U
    unit_others: int = 0                                           # how many other units share the slot U + 1
    unit_rows: np.ndarray | None = None                            # the rows of each slot 1…units
    unit_sums: np.ndarray | None = None                            # the observed sums of y by slot
    slots: np.ndarray | None = None                                # each statement row's unit slot (uint16)
    _y: list[np.ndarray] = field(default_factory=list, repr=False)
    _codes: list[np.ndarray] = field(default_factory=list, repr=False)
    _seen: dict[str, int] = field(default_factory=dict, repr=False)

    @property
    def units(self) -> int:
        """`Ppc_o<k>_units`: the checked units, and the slot of the others when there are any."""
        return len(self.unit_texts) + (1 if self.unit_others else 0)

    def unit_means(self) -> np.ndarray:
        """The observed mean of each checked unit (the others' slot included)."""
        return self.unit_sums / np.maximum(self.unit_rows, 1)


def _kind_of(statistics: list[str]) -> str:
    return "continuous" if "min" in statistics else "counts" if "zeros" in statistics else "binary"


# ── the plan ──────────────────────────────────────────────────────────────────
def plan(task: Task) -> "Plan":
    """The checks' plan of a task: empty (no outcome) for a program without the extension (a version stored by generator 1)."""
    return Plan(task)


class Plan:
    def __init__(self, task: Task) -> None:
        self.task = task
        checks = task.checks if isinstance(task.checks, dict) else {}
        self.value_bins = int(checks.get("value_bins") or DEFAULT_VALUE_BINS)
        self.max_units = int(checks.get("max_units") or DEFAULT_MAX_UNITS)
        self.entries = [e for e in task.data if e.get("kind") in EXTENSION_KINDS]
        self.has_extension = any(e["kind"] == "checks_on" for e in self.entries)
        self.has_prior = any(e["kind"] == "prior_count" for e in self.entries)
        self.on = bool(checks.get("on")) or task.mode == "prior_predictive"
        self.observing = self.has_extension and self.on
        self.max_lag = max([int(e.get("lag") or 0) for e in task.data if e.get("kind") == "lag"], default=0)
        self.lagged = any(e.get("kind") == "rows_subset" for e in task.data)
        self.outcomes: list[Outcome] = self._outcomes() if self.has_extension else []
        self._by_k = {o.k: o for o in self.outcomes}
        self._finished = False
        self._series = {kind: next((e["stan"] for e in task.data if e.get("kind") == kind), None) for kind in ("first", "next", "previous")}

    # the outcomes, from the manifests
    def _outcomes(self) -> list[Outcome]:
        task = self.task
        ks = sorted({int(m.group(1)) for e in self.entries if e["kind"] == "post_count" and (m := _OUTCOME_OF.match(e["stan"]))})
        by_check: dict[str, list[str]] = {}
        for q in task.quantities:
            if q.get("role") == "check" and q.get("check") not in _NOT_STATISTICS:
                by_check.setdefault(q.get("of"), []).append(q["check"])
        out = []
        for k in ks:
            count = next(e for e in self.entries if e["kind"] == "post_count" and _OUTCOME_OF.match(e["stan"]).group(1) == str(k))
            node = count.get("node")
            y = next((e for e in task.data if e.get("kind") in ("real", "int") and e.get("node") == node and e.get("plate") == ROWS), None)
            if y is None or not node or not y.get("column"):
                raise task_value(f"the checked outcome {node} is not a data node on the rows")
            stats = by_check.get(node, [])
            if not stats:
                raise task_value(f"the checked outcome {node} has no statistic")
            o = Outcome(k, node, y["stan"], y["column"], _kind_of(stats), stats, bool(y.get("standardize")) and y["kind"] == "real",
                        self.lagged)
            slot = next((e for e in self.entries if e["kind"] == "unit_slot" and _OUTCOME_OF.match(e["stan"]).group(1) == str(k)), None)
            if slot is not None:
                index = next((e for e in task.data if e.get("kind") == "group_index" and e.get("node") == slot.get("node")), None)
                if index is None or not index.get("column"):
                    raise task_value(f"the units of the outcome {node}: {slot.get('node')} is not a group index")
                o.unit_node, o.unit_column, o.unit_plate = slot["node"], index["column"], index.get("target")
            out.append(o)
        return out

    # ── pass A ────────────────────────────────────────────────────────────────
    def observe(self, batch: Any, pos: np.ndarray | None = None) -> None:
        """One batch of kept rows: `batch.number(column)` and `batch.text(column)` give the kept rows' values (`bayes_data._Parsed`), `pos` their
        positions in their unit of the series (None without a series). Held: the values and the unit codes, nothing else."""
        if not self.observing or self._finished:
            return
        for o in self.outcomes:
            y = batch.number(o.column)
            keep = None
            if o.lagged and pos is not None and self.max_lag:
                keep = pos >= self.max_lag
                y = y[keep]
            o._y.append(np.array(y, dtype=np.float64))
            if o.unit_column is not None:
                texts = batch.text(o.unit_column)
                if keep is not None:
                    texts = texts.filter(pa.array(keep))
                for u in pc.unique(texts).to_pylist():
                    o._seen.setdefault(u, len(o._seen))
                codes = pc.index_in(texts, value_set=pa.array(list(o._seen), pa.string()))
                o._codes.append(codes.to_numpy(zero_copy_only=False).astype(np.uint32))

    def finish(self, facts: Any) -> None:
        """The end of pass A: T(y), the bins, the observed counts, the units; then the values and the codes are freed."""
        if not self.observing or self._finished:
            return
        self._finished = True
        for o in self.outcomes:
            y = np.concatenate(o._y) if o._y else np.zeros(0)
            o._y.clear()
            s = facts.standardized.get(o.stan) if o.standardize else None
            if s:
                y = (y - s["mean"]) / s["sd"]
            o.n = int(y.size)
            if o.n:
                self._observe(o, y)
                if o.unit_column is not None:
                    self._units(o, y)
            o._codes.clear()
            o._seen.clear()

    def _bins(self, o: Outcome, y: np.ndarray) -> Bins:
        if o.kind == "binary":
            return Bins(-0.5, 1.0, 2)
        if o.kind == "counts":
            reach = int(y.max()) + 1                   # the whole values 0…max
            width = 1 if reach <= self.value_bins else math.ceil(reach / self.value_bins)
            return Bins(-0.5, float(width), math.ceil(reach / width))
        low, high = float(y.min()), float(y.max())
        if high == low:
            return Bins(low - 0.5, 1.0 / self.value_bins, self.value_bins)
        pad = (high - low) * 1e-6
        return Bins(low - pad, (high - low + 2 * pad) / self.value_bins, self.value_bins)

    def _observe(self, o: Outcome, y: np.ndarray) -> None:
        stats: dict[str, float] = {}
        for name in o.statistics:
            if name == "mean":
                stats[name] = float(y.mean())
            elif name == "sd":
                stats[name] = float(y.std(ddof=1)) if y.size > 1 else 0.0
            elif name == "min":
                stats[name] = float(y.min())
            elif name == "max":
                stats[name] = float(y.max())
            elif name == "zeros":
                stats[name] = float((y == 0).sum())
            elif name in _QUANTILES:
                stats[name] = float(np.quantile(y, _QUANTILES[name]))        # type 7, as Stan's quantile
        o.observed = stats
        o.bins = self._bins(o, y)
        o.counts = np.bincount(o.bins.slot_of(y) - 1, minlength=o.bins.slots).astype(np.int64)

    def _units(self, o: Outcome, y: np.ndarray) -> None:
        codes = np.concatenate(o._codes) if o._codes else np.zeros(0, dtype=np.uint32)
        texts = list(o._seen)
        per_unit = np.bincount(codes, minlength=len(texts))
        order = sorted(range(len(texts)), key=lambda c: (-int(per_unit[c]), texts[c]))
        kept = order[:self.max_units]
        o.unit_texts = [texts[c] for c in kept]
        o.unit_others = len(texts) - len(kept)
        table = np.full(len(texts), len(kept) + 1, dtype=np.uint16)         # the others share the slot after the checked ones
        for slot, c in enumerate(kept, 1):
            table[c] = slot
        o.slots = table[codes]
        n_slots = o.units
        o.unit_rows = np.bincount(o.slots, minlength=n_slots + 1)[1:n_slots + 1].astype(np.int64)
        o.unit_sums = np.bincount(o.slots, weights=y, minlength=n_slots + 1)[1:n_slots + 1]

    # ── the extension's values ────────────────────────────────────────────────
    def statement_rows(self, o: Outcome, facts: Any) -> int:
        """The rows of the outcome's statement: those observed in pass A, else the rows kept (N) or, lagged, the rows with every lag."""
        if self._finished and self.observing:
            return o.n
        return int(facts.series.get("lag", 0)) if o.lagged else int(facts.n)

    def extension(self, run: str, facts: Any) -> tuple[dict[str, int], dict[str, Any]]:
        """The extension's data of a run: ({size name: integer}, {Stan name: value}). `sample` is the checks off (nothing is drawn, no check
        column is written); `checks` the posterior's statistics and bins; `replicated` the posterior's rows written; `prior` the prior
        predictive's. A program without the extension has none: ({}, {})."""
        if run not in RUNS:
            raise ValueError(f"run {run!r}")
        if not self.has_extension:
            return {}, {}
        binning = run in ("checks", "prior")
        if run == "prior" and not self.has_prior:
            raise task_value("this program has no prior predictive")
        if binning:
            if not self.observing or not self._finished:
                raise task_value("the checks were not observed in pass A")
            for o in self.outcomes:
                if o.n < 2:
                    raise Refusal("data_shape", f"the predictive checks need at least 2 rows in the statement of `{o.column}`: it has {o.n}",
                                  {"column": o.column, "rows": o.n})
        values: dict[str, Any] = {}
        for e in self.entries:
            m = _OUTCOME_OF.match(e["stan"])
            o = self._by_k[int(m.group(1))] if m else None
            values[e["stan"]] = self._value(e["kind"], o, run, binning, facts)
        sizes = {name: v for name, v in values.items() if isinstance(v, int) and not isinstance(v, bool)}
        return sizes, values

    def _value(self, kind: str, o: Outcome | None, run: str, binning: bool, facts: Any) -> Any:
        if o is None:                                  # Ppc_on and the series' copies
            if kind == "checks_on":
                return 1 if binning else 0
            if kind in ("first_count", "next_count"):
                which = "first" if kind == "first_count" else "next"
                return int(facts.series.get(which, 0)) if run == "prior" else 0
            which = {"prior_first": "first", "prior_next": "next", "prior_previous": "previous"}[kind]
            if run != "prior":
                return []
            stan = self._series[which]
            if stan is None:
                raise task_value(f"the prior predictive's {which} rows: the data has no such array")
            return CopyOf(stan, int(facts.series.get("first" if which == "first" else "next", 0)))
        n = self.statement_rows(o, facts)
        post = run in ("checks", "replicated")
        if kind == "post_count":
            return n if post else 0
        if kind == "post_rows":
            return Counting(n) if post else []
        if kind == "prior_count":
            return n if run == "prior" else 0
        if kind == "prior_rows":
            return Counting(n) if run == "prior" else []
        if kind == "replicated_count":
            return n if run != "sample" else 0
        if kind == "bin_slots":
            return o.bins.slots if binning else 0
        if kind == "bin_scale":
            return o.bins.scale if binning else 0.0
        if kind == "bin_shift":
            return o.bins.shift if binning else 0.0
        if kind == "binned_count":
            return n if binning else 0
        if kind == "unit_count":
            return o.units if binning else 0
        if kind == "unit_slot":
            return Slots(o.slots) if binning else []
        if kind == "replicated_rows":
            return n if run == "replicated" else 0
        raise ValueError(kind)

    # ── the second pass ───────────────────────────────────────────────────────
    def rows(self, checks_path: str, writer: Any, section: str, seconds_left: float | None = None) -> Any:
        """The `ppc` (or, in the prior predictive, `prior_check`) rows and the overlaid densities from the checks file (lot P3b: see
        `bayes_ppc`); returns its report (the draws, those dropped for being NaN, the rows). Only charts: no p-value, no probability."""
        import bayes_ppc
        return bayes_ppc.second_pass(self, checks_path, writer, section, seconds_left)
