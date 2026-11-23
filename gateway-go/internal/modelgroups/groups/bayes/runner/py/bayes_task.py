"""The task reader of the group «bayes» and what the program's manifests say (docs/specs/bayes-contract.md, «`task.json`», «The data
manifest», «The quantity manifest», and its «Correction of 2026-11-24 from K0»).

`tab_protocol.read_task` knows the modes fit, predict and selftest only (changing them would change the runner hash of every Python
group), so this group reads its own task: the common keys of protocol 1, with the group `bayes`, the model `bayes_model`, the four
modes, and the object `bayes` checked for the ranges of «Correction» 7. A value out of range is a fault of the gateway, never of the
person: the refusal `task_value`.

The second half is the quantity manifest expanded: given the sizes the data passes found (the rows kept, the units of each group
plate, the levels of each category, the extension's integers) `build_layout` lists every column of CmdStan's CSV in order, which of
them go to the draws (and under which name, node, unit and indices), which to the checks, which are dropped (internal, a prior draw in
the sample mode, the elements above a correlation's diagonal), and `Layout.check_header` compares the CSV's own header, name for name.
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from tab_protocol import PROTOCOL, Refusal

GROUP = "bayes"
MODEL = "bayes_model"
MODES = ("compile", "prior_predictive", "sample", "selftest")
OUTPUTS = ("summary", "draws", "replicated", "results")
LEVELS_PREFIX = "levels:"
ROWS = "rows"

# The sampler's fields that start every line of CmdStan's CSV (measured on CmdStan 2.40.0 by lot K0: seven under NUTS, two under fixed_param).
SAMPLER_COLUMNS_NUTS = ("lp__", "accept_stat__", "stepsize__", "treedepth__", "n_leapfrog__", "divergent__", "energy__")
SAMPLER_COLUMNS_FIXED_PARAM = ("lp__", "accept_stat__")

MAX_ROW_GROUP = 1_048_576  # Arrow's longest row group (measured by the garch spec on Arrow 26.0.1)

# The limits of «Limits» and «The report's scope and the caps», with their visible defaults: the gateway writes them all in task.json;
# a task written by hand (a test) may leave some out.
DEFAULT_LIMITS: dict[str, int] = {
    "time_budget_seconds": 3600, "memory_bytes": 2 * 1024 ** 3, "tmp_bytes": 2 * 1024 ** 3, "started_unix": 0,
    "max_result_rows": 100_000, "max_chart_points": 2000, "block_values": 1_048_576, "max_quantities": 20_000, "max_units": 20_000,
    "max_check_columns": 200, "max_card_bytes": 1_048_576, "bayes_max_summary_quantities": 2000, "bayes_max_quantity_entries": 600,
    "bayes_max_chart_quantities": 12, "bayes_trace_points": 200, "bayes_rank_bins": 20, "bayes_density_points": 200,
    "bayes_max_unit_charts": 8, "bayes_unit_points": 1000, "bayes_max_pair_quantities": 4,
    "bayes_pair_points": 300, "bayes_max_check_rows": 5000,
}

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def task_value(what: str) -> Refusal:
    """A value of task.json out of range: a fault of the gateway, which checks every number before a Job."""
    return Refusal("task_value", f"task.json holds a value this runner cannot run ({what}): a fault of Tabularia, not of the model",
                   {"what": what})


class ManifestMismatch(RuntimeError):
    """A program and its manifest disagree (the CSV's header is not the one the manifest expands to): a bug of Tabularia, exit 1."""


# ── the task ──────────────────────────────────────────────────────────────────
@dataclass
class Task:
    """task.json read and checked. `raw` is the whole object (the card and run.json copy what they need from it)."""
    raw: dict[str, Any]
    mode: str
    variant: str
    output: str
    limits: dict[str, int]
    model: dict[str, Any]
    sampler: dict[str, Any] | None
    plates: list[dict[str, Any]]
    mapping: list[dict[str, Any]]
    data: list[dict[str, Any]]
    quantities: list[dict[str, Any]]
    checks: Any
    replicated: dict[str, Any] | None
    series: dict[str, Any] | None  # optional, not written by the gateway yet: {"by": a group plate id or null, "time": a column or null}
    # the `of` of every prior marginal whose prior has a finite variance (bayeschecks.FiniteVariance, written by flow-node); None when
    # the task does not say (every contraction NULL, with its note)
    finite_variance: frozenset[str] | None = None

    def limit(self, name: str) -> int:
        return int(self.limits[name])

    @property
    def algorithm(self) -> str:
        return "fixed_param" if self.mode == "prior_predictive" else "nuts"

    @property
    def sampler_columns(self) -> tuple[str, ...]:
        return SAMPLER_COLUMNS_FIXED_PARAM if self.mode == "prior_predictive" else SAMPLER_COLUMNS_NUTS

    @property
    def write_output(self) -> bool:
        return bool(self.raw.get("write_output"))

    def block_draws(self, n_columns: int) -> int:
        """B = max(1, floor(block_values / columns)): the draws of one chain a block holds («Following the CSV files»)."""
        return max(1, self.limit("block_values") // max(1, n_columns))


def _num(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _int_in(x: Any, low: int, high: int) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and low <= x <= high


def read_task(path: str | Path) -> Task:
    """Reads and checks task.json: `no_task` when it is missing, `protocol`, `mode`, and `task_value` for anything out of range."""
    path = Path(path)
    if not path.exists():
        raise Refusal("no_task", "task.json is missing: the Job was not made by Tabularia")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise task_value(f"task.json is not JSON: {e}") from e
    return parse_task(raw)


def parse_task(raw: Any) -> Task:
    if not isinstance(raw, dict):
        raise task_value("task.json is not an object")
    if raw.get("protocol") != PROTOCOL:
        raise Refusal("protocol", f"task.json speaks protocol {raw.get('protocol', 'none')}, this runner {PROTOCOL}: "
                                  "the image and Tabularia's version differ")
    mode = raw.get("mode")
    if mode not in MODES:
        raise Refusal("mode", f"unknown mode «{mode if isinstance(mode, str) else ''}»")
    if raw.get("group") != GROUP:
        raise task_value("group is not bayes")
    limits = dict(DEFAULT_LIMITS)
    for k, v in (raw.get("limits") or {}).items():
        if _num(v):
            limits[k] = int(v)
    limits["block_values"] = min(max(1, limits["block_values"]), MAX_ROW_GROUP)
    bayes = raw.get("bayes")
    if mode == "selftest":
        return Task(raw, mode, "", "", limits, {}, None, [], [], [], [], None, None, None)
    if raw.get("model") != MODEL:
        raise task_value("model is not bayes_model")
    if not isinstance(bayes, dict):
        raise task_value("bayes is missing")
    model = bayes.get("model")
    if not isinstance(model, dict) or not isinstance(model.get("program_sha256"), str) or not _HEX64.match(model["program_sha256"]):
        raise task_value("bayes.model.program_sha256")
    if mode == "compile":
        return Task(raw, mode, "compile", "", limits, model, None, [], [], [], [], None, None, None)
    if not isinstance(model.get("binary_sha256"), str) or not _HEX64.match(model["binary_sha256"]):
        raise task_value("bayes.model.binary_sha256")
    output = raw.get("output")
    if output not in OUTPUTS:
        raise task_value(f"output «{output if isinstance(output, str) else ''}»")
    if raw.get("variant") != mode:
        raise task_value("variant")
    if mode == "prior_predictive" and output != "results":
        raise task_value("the prior predictive writes results alone")
    sampler = _sampler(bayes.get("sampler"), mode)
    lists = {}
    for key in ("plates", "mapping", "data", "quantities"):
        value = bayes.get(key)
        if not isinstance(value, list) or not all(isinstance(e, dict) for e in value):
            raise task_value(f"bayes.{key}")
        lists[key] = value
    replicated = bayes.get("replicated")
    if (output == "replicated") != (replicated is not None):
        raise task_value("bayes.replicated is set exactly when the output is replicated")
    if replicated is not None:
        per_chain = replicated.get("draws_per_chain") if isinstance(replicated, dict) else None
        if not _int_in(per_chain, 1, sampler["draws"]):
            raise task_value("bayes.replicated.draws_per_chain")
    series = bayes.get("series")
    if series is not None and not (isinstance(series, dict) and (series.get("by") is None or isinstance(series.get("by"), str))
                                   and (series.get("time") is None or isinstance(series.get("time"), str))):
        raise task_value("bayes.series")
    finite = bayes.get("finite_variance")
    if finite is not None and not (isinstance(finite, list) and all(isinstance(x, str) for x in finite)):
        raise task_value("bayes.finite_variance")
    return Task(raw, mode, mode, output, limits, model, sampler, lists["plates"], lists["mapping"], lists["data"], lists["quantities"],
                bayes.get("checks"), replicated, series, frozenset(finite) if finite is not None else None)


def _sampler(s: Any, mode: str) -> dict[str, Any]:
    if not isinstance(s, dict):
        raise task_value("bayes.sampler")
    ok = (_int_in(s.get("chains"), 1, 8) and _int_in(s.get("warmup"), 0, 10_000) and _int_in(s.get("draws"), 1, 10_000)
          and _int_in(s.get("seed"), 0, 2 ** 32 - 1) and _num(s.get("adapt_delta")) and 0 < s["adapt_delta"] < 1
          and _int_in(s.get("max_treedepth"), 1, 15) and _int_in(s.get("refresh"), 1, 10_000))
    if not ok:
        raise task_value("bayes.sampler")
    if mode == "prior_predictive" and s["warmup"] != 0:
        raise task_value("the prior predictive runs without warm-up")
    init = s.get("init")
    if init is not None:
        if isinstance(init, dict):
            for name, v in init.items():
                values = v if isinstance(v, list) else [v]
                if not values or not all(_num(x) for x in values):
                    raise task_value(f"bayes.sampler.init.{name}")
        elif not (_num(init) and init >= 0):
            raise task_value("bayes.sampler.init")
    return s


# ── the manifests expanded ────────────────────────────────────────────────────
@dataclass(frozen=True)
class Element:
    """One scalar of a written variable, as the draws, the summary and the checks name it («Names of the quantities»)."""
    quantity: str            # alpha_school[Liceo Galilei], tau, rho[b_g,a_g]; the sampler's fields by their own name
    node: str | None         # the Stan identifier alone (null for a sampler field)
    unit: str | None         # the unit's text (a group plate's, a level, component names), null for a scalar or a row
    index: int | None
    index_2: int | None


@dataclass
class Entry:
    """A written variable of role `quantity` with what the mapping says about it, and where its scalars are in `Layout.draws`."""
    stan: str
    node: str
    label: str | None
    name: str | None
    kind: str
    plate: str | None
    plate_label: str | None
    first: int               # position of its first scalar among the draws' quantities (sampler fields included)
    count: int               # its scalars kept in the draws
    scalar: bool
    correlation: bool


@dataclass
class Layout:
    """The CSV's columns, in order, and where each goes."""
    mode: str
    header: list[str]
    n_columns: int
    draw_columns: np.ndarray            # CSV positions of the draws' quantities: the sampler's fields, then the program's
    draws: list[Element]
    check_columns: np.ndarray
    checks: list[Element]
    entries: list[Entry] = field(default_factory=list)
    n_sampler: int = 0
    marginal_columns: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    marginals: list[Element] = field(default_factory=list)   # the prior marginals (/tmp/run/prior.parquet): `node` is their `of`

    @property
    def n_quantities(self) -> int:
        """The program's scalars in the draws (the sampler's fields not counted): what `max_quantities` limits."""
        return len(self.draws) - self.n_sampler

    def check_header(self, header: list[str]) -> None:
        """The CSV's own header against the expansion, name for name and in order."""
        if list(header) != self.header:
            first = next((i for i, (a, b) in enumerate(zip(header, self.header)) if a != b), min(len(header), len(self.header)))
            raise ManifestMismatch(
                f"a program and its manifest disagree: the CSV's header has {len(header)} columns and the manifest expects "
                f"{len(self.header)}; the first difference is at column {first + 1} "
                f"(«{header[first] if first < len(header) else ''}» against «{self.header[first] if first < len(self.header) else ''}»)")


def _mapping_by_stan(task: Task) -> dict[str, dict[str, Any]]:
    return {m["stan"]: m for m in task.mapping if isinstance(m.get("stan"), str)}


def component_names(task: Task, effect: str) -> list[str]:
    """The Stan identifiers of an effect node's components, in the order of the mapping's `component` entries."""
    names = [m["stan"] for m in task.mapping if m.get("node_id") == effect and m.get("kind") == "component"]
    if names:
        return names
    # a task without a mapping (a test of the header alone): the quantities' own order
    return [q["stan"] for q in task.quantities if q.get("role") == "quantity" and (q.get("node") or "").startswith(effect + ".")
            and not (q.get("node") or "").endswith(".correlation")]


def _dim_texts(dim: dict[str, Any], sizes: dict[str, int], units: dict[str, list[str]], task: Task) -> tuple[int, list[str | None], int]:
    """(length, the unit texts or None, the first index) of a dimension."""
    if dim.get("component"):
        names = component_names(task, dim["component"])
        if not names:
            raise ManifestMismatch(f"the effect {dim['component']} has no components")
        return len(names), list(names), 1
    if dim.get("plate"):
        plate = dim["plate"]
        if plate.startswith(LEVELS_PREFIX):
            total = sizes.get(plate)
            if total is None:
                raise ManifestMismatch(f"no size for the plate {plate}")
            texts = units.get(plate)
            length = max(0, total - 1)
            return length, (list(texts[1:]) if texts else [None] * length), 2
        n = sizes.get(plate)
        if n is None:
            raise ManifestMismatch(f"no size for the plate {plate}")
        texts = units.get(plate) if plate != ROWS else None
        return n, (list(texts) if texts else [None] * n), 1
    n = sizes.get(dim.get("size", ""))
    if n is None:
        raise ManifestMismatch(f"no size for {dim}")
    return n, [None] * n, 1


def _elements(q: dict[str, Any], sizes: dict[str, int], units: dict[str, list[str]], task: Task) -> list[tuple[str, Element | None]]:
    """A written variable's scalars as (the CSV's name, its Element or None when it is dropped), the first index varying fastest."""
    prior_in_sample = q.get("role") == "prior_draw" and task.mode == "sample"
    dims = []
    for d in q.get("dims") or []:
        length, texts, first = _dim_texts(d, sizes, units, task)
        dims.append((0 if prior_in_sample else length, texts, first))
    lengths = [d[0] for d in dims]
    total = 1
    for n in lengths:
        total *= n
    stan, node = q["stan"], q.get("check") if q.get("role") == "check" else q["stan"]
    if not dims:
        return [(stan, Element(stan, node, None, None, None))]
    triangle = q.get("triangle") == "strict_lower"
    out: list[tuple[str, Element | None]] = []
    position = [0] * len(dims)
    for _ in range(total):
        csv_name = stan + "".join(f".{i + 1}" for i in position)
        parts = [dims[k][1][i] if dims[k][1][i] is not None else str(dims[k][2] + i) for k, i in enumerate(position)]
        unit = ",".join(parts) if any(dims[k][1][i] is not None for k, i in enumerate(position)) else None
        keep = not (triangle and len(position) == 2 and position[0] <= position[1])
        element = Element(f"{stan}[{','.join(parts)}]", node, unit, dims[0][2] + position[0],
                          dims[1][2] + position[1] if len(dims) > 1 else None) if keep else None
        out.append((csv_name, element))
        for k in range(len(position)):
            position[k] += 1
            if position[k] < lengths[k]:
                break
            position[k] = 0
    return out


def build_layout(task: Task, sizes: dict[str, int], units: dict[str, list[str]] | None = None) -> Layout:
    """Expands the quantity manifest with the sizes the data gave (`sizes`: «rows», a group plate's id, «levels:<category>» its levels
    K, the names of the data's other integers) and the units' texts (`units`: a group plate's id, or «levels:<category>» for all K
    levels, to their texts in order). A level plate has K - 1 elements, the levels after the reference («Correction» 1)."""
    units = units or {}
    mapping = _mapping_by_stan(task)
    sampler = list(task.sampler_columns)
    header = list(sampler)
    # first pass: every variable's elements and the CSV positions of its columns
    variables: list[dict[str, Any]] = []
    for q in task.quantities:
        elements = _elements(q, sizes, units, task)
        variables.append({"q": q, "cols": list(range(len(header), len(header) + len(elements))), "elements": elements})
        header.extend(name for name, _ in elements)
    # the prior draw of a quantity replaces the quantity's own columns in the prior predictive («Correction» 2)
    by_node = {v["q"].get("node"): v for v in variables if v["q"].get("role") == "quantity"}
    replaced: dict[int, list[int]] = {}
    for v in variables:
        if v["q"].get("role") == "prior_draw" and task.mode == "prior_predictive":
            target = by_node.get(v["q"].get("of"))
            if target is None or len(target["cols"]) != len(v["cols"]):
                raise ManifestMismatch(f"the prior draw {v['q']['stan']} does not match the quantity {v['q'].get('of')}")
            replaced[id(target)] = v["cols"]
    draw_cols = list(range(len(sampler)))
    draws = [Element(name, None, None, None, None) for name in sampler]
    check_cols: list[int] = []
    checks: list[Element] = []
    entries: list[Entry] = []
    marginal_cols: list[int] = []
    marginals: list[Element] = []
    for v in variables:
        q = v["q"]
        role = q.get("role")
        if role == "prior_marginal":
            # one column a quantity node, a component, a correlation (the checks spec): named by its Stan identifier, its node the `of`
            # the second pass looks it up by (the contraction, the prior's density)
            for col, (_, element) in zip(v["cols"], v["elements"]):
                if element is not None:
                    marginal_cols.append(col)
                    marginals.append(Element(element.quantity, q.get("of") or q["stan"], element.unit, element.index, element.index_2))
        elif role == "check":
            for col, (_, element) in zip(v["cols"], v["elements"]):
                if element is not None:
                    check_cols.append(col)
                    checks.append(element)
        elif role == "quantity":
            cols = replaced.get(id(v), v["cols"])
            first = len(draws)
            for col, (_, element) in zip(cols, v["elements"]):
                if element is not None:
                    draw_cols.append(col)
                    draws.append(element)
            m = mapping.get(q["stan"], {})
            plates = {p.get("id"): p.get("label") for p in task.plates}
            node = q.get("node") or ""
            entries.append(Entry(q["stan"], node, m.get("label"), m.get("name"), m.get("kind") or "", m.get("plate"),
                                 plates.get(m.get("plate")), first, len(draws) - first, not (q.get("dims")),
                                 node.endswith(".correlation")))
    names = [e.quantity for e in draws]
    if len(set(names)) != len(names):
        dup = next(n for i, n in enumerate(names) if n in names[:i])
        raise ManifestMismatch(f"two quantities are named {dup}")
    return Layout(task.mode, header, len(header), np.asarray(draw_cols, dtype=np.int64), draws, np.asarray(check_cols, dtype=np.int64),
                  checks, entries, len(sampler), np.asarray(marginal_cols, dtype=np.int64), marginals)


# the quantities' order in a report and in the capped sections: the scalars first (the hyperparameters and the population-level
# parameters), then the correlations, then each plate's quantities node by node in the program's order
def report_order(entries: list[Entry]) -> list[Entry]:
    return sorted(entries, key=lambda e: (0 if e.scalar else 1 if e.correlation else 2))
