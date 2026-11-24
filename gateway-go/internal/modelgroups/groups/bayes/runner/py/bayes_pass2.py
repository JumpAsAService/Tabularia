"""The second pass of a Bayesian model (docs/specs/bayes-diagnostics.md, «The second pass», lot X3; docs/specs/bayes-contract.md, «The
measures»): after CmdStan has exited, the long draws parquet is read back by BLOCKS OF QUANTITIES and every block is measured by
`bayes_diag.measures` in sub-blocks; the rows go to the results writer and the summary file as they come, and the charts' thinned
draws are taken from the block while it is in memory. Memory is bounded by the budgets, never by the number of quantities or draws:
`pass_bytes` for the block (8·S bytes a quantity), `compute_bytes` for a sub-block's working space (`bayes_diag.BYTES_PER_VALUE`
bytes a value), one row group's `value` column (at most `block_values` doubles), and the charts' thinned draws.

    report = second_pass(draws_path, layout, limits, writer, summary_path=..., prior=..., seed=..., seconds_left=...)

THE ORDER. A row group holds, quantity-major, the B draws of every quantity of `layout.draws` (sampler fields first): quantity k's
draws are the rows [k·B, (k+1)·B) and `value` is the column read at those offsets. The order is computed from the layout and
checked ONCE against the `quantity` column of the first row group; a difference is a fault of Tabularia (ManifestMismatch, exit 1).
After that check the only column ever read is `value`.

THE SCOPE. The report's scope is the first `bayes_max_summary_quantities` quantities in the report's order (the scalars, the
correlations, then each plate's quantities node by node); they get `summary` rows and the first `bayes_max_chart_quantities` of
them the trace, rank and density charts; the first `bayes_max_pair_quantities` scalars among them make the pairs. Every quantity of
the program is measured and goes to the summary file (`summary_path`), and the `quantity` rows' R-hat maximum and ESS minimum
read all of an entry's scalars. Everything written is a measure or a fact: no threshold, no reference value, no verdict word.

THE TIME. After the first block has been read and its first sub-block computed, the whole pass is projected from those two rates; a
projection past `seconds_left` is the refusal `too_slow`, raised before any row of the pass is written.

THE CONTRACTION. 1 − Var_post/Var_prior (Betancourt 2018), a number of the `summary` section and a column of the summary output, written
when `diagnostics` is. `prior_variance(position)` is a function the caller gives: the variance of the quantity's prior marginal (see
`prior_variance_of`), or the reason it has none, `NO_FINITE_VARIANCE` (the graph says the prior's marginal has no finite variance: a
Cauchy, a Student-t with ν ≤ 2) or `NO_PRIOR_DRAWS` (the run has no prior marginal for it); the contraction is then NULL and the
facts, counted, are the note `contraction_null`. Without the function every contraction is NULL for want of prior draws.

Not here: the original-scale pairs (the linear predictors'), the checks' prior marginals (`prior` is a function the caller gives:
the draws of a quantity's prior marginal, or None).
"""
from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

import bayes_charts
import bayes_diag
from bayes_card import summary_kind
from bayes_draws import read_metadata
from bayes_task import DEFAULT_LIMITS, Entry, Layout, ManifestMismatch, report_order
from tab_protocol import Refusal

NO_FINITE_VARIANCE = "no_finite_variance"
NO_PRIOR_DRAWS = "no_prior_draws"
NOT_DECIDED = "variance_not_decided"   # the task does not say which priors have a finite variance (bayes.finite_variance absent)
SUMMARY_FLUSH = 4096
SUMMARY_NUMBERS = (*bayes_diag.NUMBERS, "contraction")
SUMMARY_COLUMNS = ("quantity", "node", "label", "unit", "index", "index_2", "kind", "scale", *SUMMARY_NUMBERS)
DIAGNOSTIC_NUMBERS = ("mcse_mean", "mcse_sd", "rhat", "ess_bulk", "ess_tail")
SUMMARY_SCHEMA = pa.schema(
    [pa.field("quantity", pa.string(), nullable=False), pa.field("node", pa.string(), nullable=False), pa.field("label", pa.string()),
     pa.field("unit", pa.string()), pa.field("index", pa.int32()), pa.field("index_2", pa.int32()),
     pa.field("kind", pa.string(), nullable=False), pa.field("scale", pa.string(), nullable=False)]
    + [pa.field(name, pa.float64()) for name in SUMMARY_NUMBERS])


def limit(limits: Mapping[str, int], name: str) -> int:
    return int(limits.get(name, DEFAULT_LIMITS.get(name)))


@dataclass
class Budget:
    """The memory of the pass, in bytes: the block of quantities, a sub-block's working space, and a row group's `value` column."""
    pass_bytes: int
    compute_bytes: int
    block_values: int

    @classmethod
    def of(cls, limits: Mapping[str, int]) -> "Budget":
        memory = limit(limits, "memory_bytes")
        return cls(memory // 4, memory // 8, limit(limits, "block_values"))


@dataclass
class PassReport:
    """What the pass tells its caller (facts of the run)."""
    quantities: int = 0                  # the program's scalars measured
    in_summary: int = 0                  # of them, those of the report's scope (they have `summary` rows)
    passes: int = 0                      # blocks of quantities read
    sub_blocks: int = 0
    seconds: float = 0.0                 # the whole pass: `seconds_pass_2`
    seconds_read: float = 0.0
    seconds_compute: float = 0.0
    charts: dict[str, list[str]] = field(default_factory=dict)       # the chart ids written, by kind
    density_cut: dict[str, tuple[int, int]] = field(default_factory=dict)   # quantity → (posterior draws, prior draws) outside the range
    summary_rows: int = 0
    contraction: dict[str, int] = field(default_factory=dict)      # quantities by what the contraction is: "computed" or a reason
    not_finite_draws: int = 0            # the prior predictive: draws in which some quantity is not finite (left out of that quantity's measures)
    total_draws: int = 0                 # the draws of all the chains


# ── the order ─────────────────────────────────────────────────────────────────
def check_order(pf: pq.ParquetFile, layout: Layout, first_count: int) -> None:
    """The order computed from the layout against the `quantity` column of the first row group (its dictionary indices)."""
    p = len(layout.draws)
    column = pf.read_row_group(0, columns=["quantity"]).column(0).combine_chunks()
    if pa.types.is_dictionary(column.type):
        codes = column.indices.to_numpy(zero_copy_only=False).astype(np.int64)
        names = column.dictionary.to_pylist()
    else:
        names, codes = np.unique(np.array(column.to_pylist(), dtype=object), return_inverse=True)
        names = list(names)
    if codes.shape[0] != p * first_count:
        raise ManifestMismatch(f"the first row group has {codes.shape[0]} rows, not {p} quantities × {first_count} draws")
    grid = codes.reshape(p, first_count)
    if not (grid == grid[:, :1]).all():
        raise ManifestMismatch("the draws file is not quantity-major: a quantity's draws are not contiguous in its first row group")
    found = [names[i] for i in grid[:, 0]]
    expected = [e.quantity for e in layout.draws]
    if found != expected:
        i = next(i for i, (a, b) in enumerate(zip(found, expected)) if a != b)
        raise ManifestMismatch(f"the draws file and the manifest disagree on the order of the quantities: the quantity {i + 1} is "
                               f"«{found[i]}» in the file and «{expected[i]}» in the manifest")


# ── the scope ─────────────────────────────────────────────────────────────────
def scope_positions(layout: Layout, limits: Mapping[str, int]) -> list[int]:
    """The positions (in `layout.draws`) of the report's scope, in the report's order, up to `bayes_max_summary_quantities`."""
    cap = limit(limits, "bayes_max_summary_quantities")
    out: list[int] = []
    for e in report_order(layout.entries):
        out.extend(range(e.first, e.first + e.count))
        if len(out) >= cap:
            break
    return out[:cap]


def _entry_index(layout: Layout) -> np.ndarray:
    """For each position of `layout.draws` the index in `layout.entries` of its entry (−1 for the sampler's fields)."""
    index = np.full(len(layout.draws), -1, dtype=np.int64)
    for k, e in enumerate(layout.entries):
        index[e.first:e.first + e.count] = k
    return index


class _SummaryFile:
    """The summary output (one row a quantity), appended in row groups of at most SUMMARY_FLUSH rows."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._file = pq.ParquetWriter(path, SUMMARY_SCHEMA, compression="zstd")
        self._rows: dict[str, list[Any]] = {name: [] for name in SUMMARY_COLUMNS}
        self.total = 0

    def add(self, element: Any, entry: Entry | None, measures: dict[str, np.ndarray], i: int) -> None:
        r = self._rows
        r["quantity"].append(element.quantity)
        r["node"].append(element.node or element.quantity)
        r["label"].append(entry.label if entry else None)
        r["unit"].append(element.unit)
        r["index"].append(element.index)
        r["index_2"].append(element.index_2)
        r["kind"].append(summary_kind(entry.kind) if entry and entry.kind else "parameter")
        r["scale"].append("model")
        for name in SUMMARY_NUMBERS:
            v = float(measures[name][i]) if name in measures else math.nan
            r[name].append(v if math.isfinite(v) else None)
        if len(r["quantity"]) >= SUMMARY_FLUSH:
            self.flush()

    def flush(self) -> None:
        n = len(self._rows["quantity"])
        if n == 0:
            return
        self._file.write_table(pa.table(self._rows, schema=SUMMARY_SCHEMA), row_group_size=n)
        self.total += n
        self._rows = {name: [] for name in SUMMARY_COLUMNS}

    def close(self) -> None:
        self.flush()
        self._file.close()

    def abort(self) -> None:
        try:
            self._file.close()
        except Exception:  # noqa: BLE001 — it is being thrown away
            pass
        import os
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass


def prior_variance_of(draws: np.ndarray | None, finite_variance: bool) -> float | str:
    """What `prior_variance` returns for a quantity: `finite_variance` is the graph's decision (the catalogue's field `finite_variance` of its
    prior and of everything its arguments read), `draws` its prior marginal's draws (None: the run has none)."""
    if not finite_variance:
        return NO_FINITE_VARIANCE
    if draws is None or np.asarray(draws).size < 2:
        return NO_PRIOR_DRAWS
    v = float(np.var(np.asarray(draws, dtype=np.float64), ddof=1))
    return v if math.isfinite(v) and v > 0.0 else NO_PRIOR_DRAWS


def contraction(sd: np.ndarray, positions: Sequence[int], prior_variance: Callable[[int], float | str] | None,
                counts: dict[str, int]) -> np.ndarray:
    """1 − Var_post/Var_prior for the quantities at `positions` (NaN for NULL), the posterior variance the square of the summary's sd."""
    out = np.full(len(positions), np.nan)
    for j, position in enumerate(positions):
        got = prior_variance(position) if prior_variance is not None else NO_PRIOR_DRAWS
        if isinstance(got, str):
            counts[got] = counts.get(got, 0) + 1
            continue
        var_post = float(sd[j]) ** 2
        if not math.isfinite(var_post) or not math.isfinite(got) or got <= 0.0:
            counts[NO_PRIOR_DRAWS] = counts.get(NO_PRIOR_DRAWS, 0) + 1
            continue
        out[j] = 1.0 - var_post / got
        counts["computed"] = counts.get("computed", 0) + 1
    return out


# ── the pass ──────────────────────────────────────────────────────────────────
def second_pass(draws_path: str, layout: Layout, limits: Mapping[str, int], writer: Any, *, summary_path: str | None = None,
                budget: Budget | None = None, diagnostics: bool = True, prior: Callable[[int], np.ndarray | None] | None = None,
                seed: int = 0, seconds_left: float | None = None, quantity_entries: Sequence[Entry] | None = None,
                prior_variance: Callable[[int], float | str] | None = None, read_row_group: Callable[..., Any] | None = None,
                charts: bool | None = None) -> PassReport:
    """Reads the draws parquet back by blocks of quantities and writes the summary, the quantity measures and the charts.

    `writer`: a ResultsWriter (`number`, `points`, `note`); `diagnostics` False is the prior predictive mode (mean, sd, the
    quantiles); `prior(position)`: the draws of a quantity's prior marginal, or None; `seed`: the seeded sample of a `units:` chart;
    `seconds_left`: the time the pass may take (the projection above it is `too_slow`); `quantity_entries`: the entries that have a
    `quantity` row (default: the report's first `bayes_max_quantity_entries`); `prior_variance(position)`: the variance of a quantity's
    prior marginal or the reason it has none (the contraction). A refusal or an error leaves the summary file deleted.
    `charts` (default: `diagnostics`) False writes no chart at all: the prior predictive variant declares no `hist` and no `point`, and its
    report shows the priors' summary and the checks only (bayes-report.md, «The pages of `prior_predictive`»). Without `diagnostics` a draw in
    which a quantity is not finite (an ancestral draw that threw, or one with no mass) is left out of that quantity's measures, counted in
    the note `note_prior_dropped`.
    """
    budget = budget or Budget.of(limits)
    charts = diagnostics if charts is None else charts
    t_start = time.monotonic()
    meta = read_metadata(draws_path)
    chains, draws = int(meta["chains"]), int(meta["draws"])
    groups = [tuple(int(v) for v in g) for g in meta["row_groups"]]
    p_all = len(layout.draws)
    if int(meta.get("quantities", p_all)) != p_all:
        raise ManifestMismatch(f"the draws file holds {meta.get('quantities')} quantities and the manifest {p_all}")
    pf = pq.ParquetFile(draws_path)
    if pf.metadata.num_row_groups != len(groups) or not groups:
        raise ManifestMismatch("the draws file's row groups are not the ones its metadata lists")
    read = read_row_group or pf.read_row_group
    check_order(pf, layout, groups[0][2])

    s = chains * draws
    first = layout.n_sampler
    n_program = p_all - first
    report = PassReport(quantities=n_program)
    scope = scope_positions(layout, limits)
    scope_set = set(scope)
    report.in_summary = len(scope)
    chart_positions = scope[:limit(limits, "bayes_max_chart_quantities")] if charts else []
    chart_set = set(chart_positions)
    entry_of = _entry_index(layout)
    scalar_scope = ([p for p in scope if layout.entries[entry_of[p]].scalar][:limit(limits, "bayes_max_pair_quantities")] if charts else [])
    pair_set = set(scalar_scope) if len(scalar_scope) >= 2 else set()
    pair_index = bayes_charts.thin_indices(s, limit(limits, "bayes_pair_points"))
    pair_draws: dict[int, np.ndarray] = {}
    index_of = {id(e): k for k, e in enumerate(layout.entries)}
    unit_entries = [index_of[id(e)] for e in report_order(layout.entries) if e.plate is not None and not e.scalar and e.count >= 2
                    ][:limit(limits, "bayes_max_unit_charts")] if charts else []
    unit_store = {k: np.full((layout.entries[k].count, 3), np.nan) for k in unit_entries}
    if quantity_entries is None:
        quantity_entries = report_order(layout.entries)[:limit(limits, "bayes_max_quantity_entries")]
    extremes = {index_of[id(e)]: [np.nan, np.nan, np.nan] for e in quantity_entries if id(e) in index_of}      # rhat max, ess_bulk min, ess_tail min
    divergent_position = next((i for i, e in enumerate(layout.draws[:first]) if e.quantity == "divergent__"), None)
    divergent: np.ndarray | None = None

    numbers = SUMMARY_NUMBERS if diagnostics else bayes_diag.NUMBERS[:7]
    summary = _SummaryFile(summary_path) if summary_path else None
    per_block = max(1, budget.pass_bytes // (8 * s))
    per_sub = bayes_diag.sub_block(budget.compute_bytes, s)
    projected = False
    not_finite = np.zeros(s, dtype=bool)
    d_div = np.zeros((chains, draws)) if divergent_position is not None and pair_set else None
    try:
        for p0 in range(first, p_all, per_block):
            p1 = min(p_all, p0 + per_block)
            nq = p1 - p0
            block = np.empty((nq, chains, draws))
            t0 = time.monotonic()
            for i, (chain, first_draw, count) in enumerate(groups):
                values = read(i, columns=["value"]).column(0).to_numpy()
                if values.shape[0] != p_all * count:
                    raise ManifestMismatch(f"the row group {i + 1} holds {values.shape[0]} values, not {p_all} × {count}")
                block[:, chain - 1, first_draw - 1:first_draw - 1 + count] = values[p0 * count:p1 * count].reshape(nq, count)
                if d_div is not None and p0 == first:
                    d_div[chain - 1, first_draw - 1:first_draw - 1 + count] = values[divergent_position * count:
                                                                                    (divergent_position + 1) * count]
                del values
            read_seconds = time.monotonic() - t0
            if not diagnostics:
                not_finite |= ~np.isfinite(block).all(axis=0).reshape(-1)
            report.seconds_read += read_seconds
            report.passes += 1
            if d_div is not None and p0 == first:
                divergent = d_div.reshape(-1) > 0.5
                d_div = None
            for a in range(0, nq, per_sub):
                b = min(nq, a + per_sub)
                t1 = time.monotonic()
                m = bayes_diag.measures(block[a:b], diagnostics=diagnostics)
                if diagnostics:
                    m["contraction"] = contraction(m["sd"], range(p0 + a, p0 + b), prior_variance, report.contraction)
                compute_seconds = time.monotonic() - t1
                report.seconds_compute += compute_seconds
                report.sub_blocks += 1
                if not projected:
                    projected = True
                    blocks = math.ceil(n_program / per_block)
                    estimate = read_seconds * blocks + compute_seconds / (b - a) * n_program
                    if seconds_left is not None and estimate > seconds_left:
                        raise Refusal("too_slow", f"the second pass of the draws would take about {int(estimate)} seconds and "
                                                  f"{int(seconds_left)} are left: sample fewer draws or fewer quantities, or give the run more time",
                                      {"projected_seconds": int(estimate), "seconds_left": int(seconds_left), "quantities": n_program})
                for j in range(b - a):
                    position = p0 + a + j
                    element, k = layout.draws[position], int(entry_of[position])
                    entry = layout.entries[k]
                    if summary is not None:
                        summary.add(element, entry, m, j)
                    if position in scope_set:
                        for name in numbers:
                            v = float(m[name][j])
                            writer.number("summary", name, v if math.isfinite(v) else None, level=element.node, level_2=element.unit)
                    if k in unit_store:
                        unit_store[k][position - entry.first] = (m["q5"][j], m["q50"][j], m["q95"][j])
                    if position in chart_set:
                        _charts(writer, report, limits, element.quantity, block[a + j], prior(position) if prior else None)
                    if position in pair_set:
                        pair_draws[position] = block[a + j].reshape(-1)[pair_index].copy()
                if diagnostics:
                    _extremes(extremes, entry_of, p0 + a, b - a, m)
            del block
        if summary is not None:
            summary.close()
            report.summary_rows = summary.total
    except BaseException:
        if summary is not None:
            summary.abort()
        raise
    if not diagnostics:
        report.total_draws, report.not_finite_draws = s, int(not_finite.sum())
        if report.not_finite_draws:
            writer.note("prior_dropped", f"{report.not_finite_draws} of the {s} prior draws have a quantity that is not a finite number (an "
                                         "ancestral draw that threw, or one with no mass): each is left out of the summary of that quantity.")
    _finish(writer, report, layout, limits, seed, scope, numbers, diagnostics, pair_draws, scalar_scope, pair_index, divergent,
            unit_store, extremes)
    report.seconds = time.monotonic() - t_start
    return report


def _charts(writer: Any, report: PassReport, limits: Mapping[str, int], name: str, draws: np.ndarray, prior: np.ndarray | None) -> None:
    bayes_charts.write_trace(writer, name, draws, limit(limits, "bayes_trace_points"))
    bayes_charts.write_rank(writer, name, draws, limit(limits, "bayes_rank_bins"))
    d = bayes_charts.density(draws, prior, limit(limits, "bayes_density_points"))
    report.charts.setdefault("trace", []).append(name)
    report.charts.setdefault("rank", []).append(name)
    if d is not None:
        bayes_charts.write_density(writer, name, d)
        report.charts.setdefault("density", []).append(name)
        if d.cut_posterior or d.cut_prior:
            report.density_cut[name] = (d.cut_posterior, d.cut_prior)


def _extremes(extremes: dict[int, list[float]], entry_of: np.ndarray, p0: int, count: int, m: dict[str, np.ndarray]) -> None:
    """The R-hat maximum and the ESS minima of the entries whose scalars the sub-block holds (NaN where nothing is defined)."""
    ks = entry_of[p0:p0 + count]
    for k in np.unique(ks):
        if int(k) not in extremes:
            continue
        sel = ks == k
        cur = extremes[int(k)]
        for slot, (name, reduce) in enumerate((("rhat", np.fmax), ("ess_bulk", np.fmin), ("ess_tail", np.fmin))):
            cur[slot] = float(reduce.reduce(np.append(m[name][sel], cur[slot])))


def _finish(writer: Any, report: PassReport, layout: Layout, limits: Mapping[str, int], seed: int, scope: list[int],
            numbers: Sequence[str], diagnostics: bool, pair_draws: dict[int, np.ndarray], scalar_scope: list[int],
            pair_index: np.ndarray, divergent: np.ndarray | None, unit_store: dict[int, np.ndarray],
            extremes: dict[int, list[float]]) -> None:
    """What the blocks could not write: the pairs, the units' charts, the entries' extremes and the model section's facts."""
    writer.number("model", "n_in_summary", report.in_summary)
    all_scopes = len(scope)
    program = report.quantities
    if all_scopes < program:
        writer.note("summary_capped", f"The report lists {all_scopes} of {program} quantities; the node's summary output has them all.")
    flags = divergent[pair_index] if divergent is not None else np.zeros(pair_index.shape[0], dtype=bool)
    for x, a in enumerate(scalar_scope):
        for b in scalar_scope[x + 1:]:
            if a in pair_draws and b in pair_draws:
                bayes_charts.write_pair(writer, layout.draws[a].quantity, layout.draws[b].quantity, pair_draws[a], pair_draws[b], flags)
                report.charts.setdefault("pairs", []).append(f"{layout.draws[a].quantity}:{layout.draws[b].quantity}")
    cap = limit(limits, "bayes_unit_points")
    for k, table in unit_store.items():
        e = layout.entries[k]
        units = [layout.draws[e.first + i].unit for i in range(e.count)]
        bayes_charts.write_units(writer, e.stan, units, table[:, 0], table[:, 1], table[:, 2], cap, seed)
        report.charts.setdefault("units", []).append(e.stan)
    if diagnostics:
        for k, (rhat_max, bulk_min, tail_min) in extremes.items():
            e = layout.entries[k]
            for stat, v in (("rhat_max", rhat_max), ("ess_bulk_min", bulk_min), ("ess_tail_min", tail_min)):
                writer.number("quantity", stat, v if math.isfinite(v) else None, level=e.stan)
    nulls = {k: v for k, v in report.contraction.items() if k != "computed"}
    if nulls:
        said = {NO_FINITE_VARIANCE: "the prior has no finite variance", NO_PRIOR_DRAWS: "the run has no draws of the prior",
                NOT_DECIDED: "the model's version does not say whether the prior has a finite variance"}
        writer.note("contraction_null", "The contraction is empty for " + "; ".join(
            f"{n} quantities ({said.get(k, k)})" for k, n in sorted(nulls.items())) + ".")
    if report.density_cut:
        parts = [f"{name} ({a} posterior and {b} prior draws)" for name, (a, b) in report.density_cut.items()]
        writer.note("density_cut", "The density charts cut the draws outside their range: " + "; ".join(parts) + ".")


# ── the sampler's facts ───────────────────────────────────────────────────────
def sampler_section(w: Any, facts: Mapping[Any, Mapping[str, Any]], seconds: Mapping[int, tuple[float | None, float | None]]) -> None:
    """The `sampler` section from the accumulators' facts (`FactsAccumulator.facts`: a dict by chain 1…C and "all") and the
    follower's times (chain → (seconds of warmup, seconds of sampling)); `all` takes the slowest chain's times (the chains sample
    at once). A fact the run does not have (a column CmdStan did not write) is no row."""
    for chain, f in facts.items():
        level = str(chain)
        for stat in ("divergences", "treedepth_hits", "treedepth_max", "e_bfmi", "stepsize", "accept_stat_mean", "n_leapfrog_mean"):
            if f.get(stat) is not None:
                w.number("sampler", stat, f[stat], level=level)
        if level == "all":
            warm = [v[0] for v in seconds.values() if v[0] is not None]
            samp = [v[1] for v in seconds.values() if v[1] is not None]
            pair = (max(warm) if warm else None, max(samp) if samp else None)
        else:
            pair = seconds.get(int(chain), (None, None))
        for stat, v in (("seconds_warmup", pair[0]), ("seconds_sampling", pair[1])):
            if v is not None:
                w.number("sampler", stat, v, level=level)
        if f.get("draws") is not None:
            w.number("sampler", "draws", f["draws"], level=level)
