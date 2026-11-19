"""From the engine's aggregates to a test's results: the statistic, its degrees of freedom,
the p-value, the critical values, the intervals, the effect sizes — arithmetic on a handful
of numbers, the same code for every engine. It reads the data through a source only
(`SqlStatSource`, `PolarsStatSource`): a row per group at most, never a row of the data.

A test is a function `(source, p, out)` named in `_TESTS`: `p` its checked parameters
(`catalog.check_params`), `out` the rows it writes (`output.Rows`). Sections and the order of
the rows inside them are the output's contract: texts first, then numbers.

With `detail = report` a test adds what its report's charts draw: the groups' quartiles (a
pass of the engine), the `hist` section (another), and the `curve` section, 201 points of a
formula. Only then: the exact quartiles are the expensive part on a very large table.

Critical values and one-sided results follow one rule, for a statistic T with quantile q:

    two_sided   p = 2·P(T ≥ |t|)   critical ∓q(1 − α/2)        estimate ± q(1 − α/2)·se
    less        p = P(T ≤ t)       critical_low = q(α)         ci_high = estimate + q(1 − α)·se
    greater     p = P(T ≥ t)       critical_high = q(1 − α)    ci_low = estimate − q(1 − α)·se

The side a one-sided result leaves open is NULL.
"""
from __future__ import annotations

import math
from typing import Any, Callable

from app.engine.stats import dist
from app.engine.stats.aggregates import Moments, Quartiles
from app.engine.stats.catalog import (
    CURVE_POINTS,
    MAX_BINS,
    MAX_STAT_GROUPS,
    MIN_BINS,
    check_params,
    label_of,
    refusal,
)
from app.engine.stats.output import Rows

_RELATION = {"two_sided": "≠", "less": "<", "greater": ">"}


def _num(x: float) -> str:
    """A parameter inside a hypothesis: 0, 2.5, 1e-07."""
    return format(x + 0.0, ".12g")


def _listed(labels: list[str], most: int = 10) -> str:
    return ", ".join(labels[:most]) + (", …" if len(labels) > most else "")


def _t_tails(t: float, df: float, alternative: str, alpha: float, estimate: float, se: float):
    """(p-value, critical_low, critical_high, ci_low, ci_high) of a t statistic."""
    if alternative == "two_sided":
        q = dist.t_isf(alpha / 2.0, df)
        return min(1.0, 2.0 * dist.t_sf(abs(t), df)), -q, q, estimate - q * se, estimate + q * se
    q = dist.t_isf(alpha, df)
    if alternative == "less":
        return dist.t_cdf(t, df), -q, None, None, estimate + q * se
    return dist.t_sf(t, df), None, q, estimate - q * se, None


def _write_head(out: Rows, test: str, variant: str, p: dict[str, Any], h0: str, h1: str, formula: str,
                distribution: str, p_value: float, engine_name: str, notes: list[str]) -> None:
    """The texts of the `test` section, in their order."""
    out.text("test", "test", test)
    out.text("test", "variant", variant)
    out.text("test", "alternative", p["alternative"])
    out.text("test", "h0", h0)
    out.text("test", "h1", h1)
    out.text("test", "formula", formula)
    out.text("test", "distribution", distribution)
    out.text("test", "decision", "reject_h0" if p_value < p["alpha"] else "fail_to_reject_h0")
    out.text("test", "engine", engine_name)
    for i, note in enumerate(notes, start=1):
        out.text("test", f"note_{i}", note)


def _write_group(out: Rows, label: str, m: Moments, alpha: float, quartiles: Quartiles | None = None) -> None:
    """A numeric group's rows: its numbers, and the two-sided interval of its mean; for a
    report its quartiles too, which a box plot draws between `min` and `max`."""
    sd = math.sqrt(m.var)
    se = sd / math.sqrt(m.n)
    q = dist.t_isf(alpha / 2.0, m.n - 1)
    for stat, value in (("n", m.n), ("mean", m.mean), ("var", m.var), ("sd", sd), ("se", se),
                        ("ci_low", m.mean - q * se), ("ci_high", m.mean + q * se), ("min", m.min), ("max", m.max)):
        out.number("group", stat, value, level=label)
    if quartiles is not None:
        for stat, value in (("q1", quartiles.q1), ("median", quartiles.median), ("q3", quartiles.q3)):
            out.number("group", stat, value, level=label)


# ── the rows of a report's charts (detail = report) ─────────────────────────
def _edge(x: float) -> str:
    """An interval's edge as its label shows it: six significant digits."""
    return format(x + 0.0, ".6g")


def _quartiles_by_label(source, value: str, group: str | None, levels: list | None = None) -> dict[str, Quartiles]:
    """A12 by label (the rows with no group left out): for every group, or with `levels`
    (typed values of the grouping column) for those groups only — a test of two groups asks
    for its two, so that the engine never keeps the values of the others to find quantiles
    nobody reads."""
    return {label_of(q.level): q for q in source.quartiles(value, group, levels) if q.level is not None}


def _write_histogram(out: Rows, source, value: str, group: str, labels: list[str], groups: list[Moments]) -> None:
    """The `hist` section: for each group the rows in each of the same equal intervals,
    between the smallest and the largest value of the groups together. Their number is
    Sturges' for the largest group, between `MIN_BINS` and `MAX_BINS`; the last interval
    holds its upper edge (its label closes with a square bracket); an empty one is written
    with 0."""
    lo, hi = min(m.min for m in groups), max(m.max for m in groups)
    n_max = max(m.n for m in groups)
    bins = min(MAX_BINS, max(MIN_BINS, (n_max - 1).bit_length() + 1))  # ceil(log2(n)) + 1
    counts = {label: [0] * bins for label in labels}
    for b in source.histogram(value, group, lo, hi, bins):
        label = None if b.level is None else label_of(b.level)
        if label in counts and 0 <= b.bin < bins:
            counts[label][b.bin] += b.rows
    width = (hi - lo) / bins
    edges = [lo + i * width for i in range(bins)] + [hi]
    for label in labels:
        for i, rows in enumerate(counts[label]):
            last = i == bins - 1
            out.point("hist", "count", i, rows, edges[i], edges[i + 1],
                      text=f"[{_edge(edges[i])}, {_edge(edges[i + 1])}{']' if last else ')'}", level=label)


def _write_curve(out: Rows, pdf: Callable[[float], float], statistic: float,
                 critical_low: float | None, critical_high: float | None) -> None:
    """The `curve` section of a statistic whose distribution is symmetric about 0 (t,
    normal): its density at `CURVE_POINTS` equally spaced abscissas that reach past the
    statistic and the critical values, then the same density again on the points inside
    the critical region, as `reject_low` and `reject_high`."""
    reach = max([4.0, 1.15 * abs(statistic)] + [1.15 * abs(c) for c in (critical_low, critical_high) if c is not None])
    step = 2.0 * reach / (CURVE_POINTS - 1)
    # the middle point is 0 exactly, and the two halves mirror each other
    xs = [(i - (CURVE_POINTS - 1) / 2.0) * step for i in range(CURVE_POINTS)]
    ys = [pdf(x) for x in xs]
    for i, (x, y) in enumerate(zip(xs, ys)):
        out.point("curve", "density", i, y, x)
    if critical_low is not None:
        for i, (x, y) in enumerate(zip(xs, ys)):
            if x <= critical_low:
                out.point("curve", "reject_low", i, y, x)
    if critical_high is not None:
        for i, (x, y) in enumerate(zip(xs, ys)):
            if x >= critical_high:
                out.point("curve", "reject_high", i, y, x)


# ── two groups of a numeric column ──────────────────────────────────────────
def _two_groups(source, test: str, p: dict[str, Any]) -> tuple[list[str], list[Moments], int, list]:
    """The two groups a two-sample test compares, the first minus the second: their labels,
    their moments, the rows left out (no group, or no value in one of the two), and the
    values of the grouping column that are those two groups, typed as the engine gave them
    (what an aggregate of the report keeps to: `quartiles`)."""
    value, group = p["value"], p["group"]
    wanted = p["groups"]
    # Two groups named: the engine reads those two (and counts the rows with no group),
    # however many the column holds — the limit on the groups is for a column whose groups
    # are all compared. Only when the column's type cannot be compared with a value written
    # from a label (a datetime, a decimal) are its groups listed, as without a choice.
    chosen = source.levels_of(group, wanted) if wanted is not None else None
    if chosen is not None:
        found = {label_of(lv.value): lv.value for lv in chosen if lv.value is not None}
        for g in wanted:
            if g not in found:
                labels = sorted(label_of(lv.value) for lv in source.levels(group, MAX_STAT_GROUPS + 2) if lv.value is not None)
                if not labels:
                    raise refusal(test, f"the column «{group}» has no group: the step has no rows, or every row is NULL there")
                has = f"more than {MAX_STAT_GROUPS} groups" if len(labels) > MAX_STAT_GROUPS else _listed(labels)
                raise refusal(test, f"the group «{g}» is not in the column «{group}» (it has{'' if len(labels) > MAX_STAT_GROUPS else ':'} {has})")
        typed = [found[g] for g in wanted]
    else:
        levels = [lv for lv in source.levels(group, MAX_STAT_GROUPS + 2) if lv.value is not None]
        if len(levels) > MAX_STAT_GROUPS:
            raise refusal(test, f"the column «{group}» has more than {MAX_STAT_GROUPS} groups: is it the right column?"
                          + ("" if wanted is None else " (the two groups chosen cannot be read alone from a column of this type)"))
        labels = sorted(label_of(lv.value) for lv in levels)
        if not labels:
            raise refusal(test, f"the column «{group}» has no group: the step has no rows, or every row is NULL there")
        if wanted is None:
            if len(labels) != 2:
                raise refusal(
                    test,
                    f"the test compares two groups and the column «{group}» has {len(labels)} ({_listed(labels)}): "
                    + ("it needs a second one" if len(labels) == 1 else "choose the two to compare in 'groups'"),
                )
            wanted = labels
        for g in wanted:
            if g not in labels:
                raise refusal(test, f"the group «{g}» is not in the column «{group}» (it has: {_listed(labels)})")
        typed = [lv.value for lv in levels if label_of(lv.value) in wanted]
    by_label: dict[str, Moments] = {}
    without_group = 0
    for m in source.moments(value, group, typed if chosen is not None else None):
        if m.level is None:
            without_group += m.rows
        else:
            by_label[label_of(m.level)] = m
    pair = [by_label[g] for g in wanted]
    for g, m in zip(wanted, pair):
        if m.n < 2:
            raise refusal(
                test,
                f"the group «{g}» has {m.n} usable value{'' if m.n == 1 else 's'} of «{value}» "
                "(a NULL or a NaN is left out): at least 2 are needed",
            )
        if not all(v is not None and math.isfinite(v) for v in (m.min, m.max, m.mean, m.var)):
            raise refusal(test, f"the column «{value}» holds infinite values in the group «{g}»: the test cannot be computed")
    missing = without_group + sum(m.rows - m.n for m in pair)
    return list(wanted), pair, missing, typed


def _t_two_sample(source, p: dict[str, Any], out: Rows, engine_name: str) -> None:
    """Student's and Welch's t for two independent samples. H0: mean₁ − mean₂ = mu."""
    test = "t_two_sample"
    (g1, g2), (a, b), missing, typed = _two_groups(source, test, p)
    value, mu, alpha, alternative = p["value"], p["mu"], p["alpha"], p["alternative"]
    if a.var == 0.0 and b.var == 0.0:
        raise refusal(test, f"all the values of «{value}» are equal inside each of the two groups (both variances are 0): there is no t statistic")
    estimate = a.mean_centred - b.mean_centred  # the two means less the same centre: see Moments
    pooled_var = ((a.n - 1) * a.var + (b.n - 1) * b.var) / (a.n + b.n - 2)
    notes: list[str] = []
    if p["var_equal"]:
        variant = "student"
        se = math.sqrt(pooled_var * (1.0 / a.n + 1.0 / b.n))
        df = float(a.n + b.n - 2)
        formula = "t = (m1 − m2 − mu) / √(sp²·(1/n1 + 1/n2)), sp² = ((n1 − 1)·s1² + (n2 − 1)·s2²) / (n1 + n2 − 2), df = n1 + n2 − 2"
        low, high = sorted((a.var, b.var))
        if low == 0.0 or high / low > 4.0:
            notes.append("The two sample variances differ by more than a factor of 4: Welch's test (var_equal = false) does not assume them equal.")
    else:
        variant = "welch"
        va, vb = a.var / a.n, b.var / b.n
        se = math.sqrt(va + vb)
        df = (va + vb) ** 2 / (va * va / (a.n - 1) + vb * vb / (b.n - 1))
        formula = "t = (m1 − m2 − mu) / √(s1²/n1 + s2²/n2), df = (s1²/n1 + s2²/n2)² / ((s1²/n1)²/(n1 − 1) + (s2²/n2)²/(n2 − 1))"
    t = (estimate - mu) / se
    p_value, critical_low, critical_high, ci_low, ci_high = _t_tails(t, df, alternative, alpha, estimate, se)
    cohen_d = estimate / math.sqrt(pooled_var)
    hedges_g = cohen_d * (1.0 - 3.0 / (4.0 * (a.n + b.n - 2) - 1.0))

    difference = f"mean({value} | {g1}) − mean({value} | {g2})"
    _write_head(out, test, variant, p, f"{difference} = {_num(mu)}", f"{difference} {_RELATION[alternative]} {_num(mu)}",
                formula, f"t({df:.6g})", p_value, engine_name, notes)
    for stat, number in (
        ("alpha", alpha), ("conf_level", 1.0 - alpha), ("n", a.n + b.n), ("n_missing", missing),
        ("pooled_var", pooled_var), ("estimate", estimate), ("null_value", mu), ("std_error", se),
        ("statistic", t), ("df", df), ("critical_low", critical_low), ("critical_high", critical_high),
        ("p_value", p_value), ("ci_low", ci_low), ("ci_high", ci_high),
        ("cohen_d", cohen_d), ("hedges_g", hedges_g),
    ):
        out.number("test", stat, number)
    report = p["detail"] == "report"
    quartiles = _quartiles_by_label(source, value, p["group"], typed) if report else {}
    _write_group(out, g1, a, alpha, quartiles.get(g1))
    _write_group(out, g2, b, alpha, quartiles.get(g2))
    if report:
        _write_histogram(out, source, value, p["group"], [g1, g2], [a, b])
        _write_curve(out, lambda x: dist.t_pdf(x, df), t, critical_low, critical_high)


_TESTS: dict[str, Callable[..., None]] = {
    "t_two_sample": _t_two_sample,
}

# The tests that write the rows of their report's charts. Until a test is here it refuses
# `detail = report` with a sentence: it is never answered with the results alone.
_WITH_REPORT_ROWS = frozenset({"t_two_sample"})


def run_hypothesis_test(source, test: str, params: dict[str, Any] | None, engine_name: str) -> list[tuple]:
    """The rows of the test `test` on `source`, as `output.OUTPUT_COLUMNS` orders them."""
    p = check_params(test, params, source.columns())
    if p.get("detail") == "report" and test not in _WITH_REPORT_ROWS:
        raise refusal(test, "the rows of the report's charts (detail = 'report') are not available yet: use detail = 'results'")
    out = Rows()
    _TESTS[test](source, p, out, engine_name)
    return out.rows
