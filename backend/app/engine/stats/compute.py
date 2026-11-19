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
from typing import Any, Callable, NamedTuple

from app.engine.stats import dist, exact
from app.engine.stats.aggregates import Difference, Moments, PairMoments, Paired, Quartiles, is_value, typed_value
from app.engine.stats.catalog import (
    CURVE_POINTS,
    EXACT_SUPPORT,
    MAX_BINS,
    MAX_POSTHOC_GROUPS,
    MAX_STAT_GROUPS,
    MAX_STAT_LEVELS,
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


def _f_tails(f: float, d1: float, d2: float, alpha: float) -> tuple[float, float]:
    """(p-value, critical_high) of a statistic that only grows with the departure from H0
    (an ANOVA's F, Levene's): p = P(F ≥ f), the critical region above q(1 − α)."""
    return dist.f_sf(f, d1, d2), dist.f_isf(alpha, d1, d2)


def _write_head(out: Rows, test: str, variant: str, p: dict[str, Any], h0: str, h1: str, formula: str,
                distribution: str, p_value: float, engine_name: str, notes: list[str],
                pairs: tuple[str, str, str] | None = None) -> None:
    """The texts of the `test` section, in their order; `pairs`: the post-hoc, how its
    p-values are adjusted and the formulas of a pair, for a test that compares pairs of groups. A test without an
    alternative (its statistic only grows with the departure from H0) says «two_sided»:
    its H1 is «not all equal»."""
    out.text("test", "test", test)
    out.text("test", "variant", variant)
    out.text("test", "alternative", p.get("alternative", "two_sided"))
    out.text("test", "h0", h0)
    out.text("test", "h1", h1)
    out.text("test", "formula", formula)
    out.text("test", "distribution", distribution)
    out.text("test", "decision", "reject_h0" if p_value < p["alpha"] else "fail_to_reject_h0")
    out.text("test", "engine", engine_name)
    if pairs is not None:
        out.text("test", "posthoc", pairs[0])
        out.text("test", "p_adjust", pairs[1])
        out.text("test", "posthoc_formula", pairs[2])
    for i, note in enumerate(notes, start=1):
        out.text("test", f"note_{i}", note)


# R's significance codes: the thresholds of a p-value, from the smallest (`summary.lm`'s stars)
SIGNIFICANCE = ((0.001, "***"), (0.01, "**"), (0.05, "*"), (0.1, "."))


def significance(p_value: float | None) -> str | None:
    """R's significance code of a p-value: «***» below 0.001, «**» below 0.01, «*» below 0.05,
    «.» below 0.1, nothing (an empty text) above. It says R's threshold, not the decision:
    that one is the node's alpha's. None for a p-value there is not."""
    if p_value is None or not math.isfinite(p_value):
        return None
    return next((code for threshold, code in SIGNIFICANCE if p_value < threshold), "")


def _write_numbers(out: Rows, numbers) -> None:
    """The numbers of the `test` section, in their order; after the p-value its significance
    code (a text, R's stars)."""
    for stat, number in numbers:
        out.number("test", stat, number)
        if stat == "p_value" and (code := significance(number)) is not None:
            out.text("test", "significance", code)


def _write_group(out: Rows, label: str, m: Moments, alpha: float, quartiles: Quartiles | None = None,
                 more: tuple = ()) -> None:
    """A numeric group's rows: its numbers, and the two-sided interval of its mean; for a
    report its quartiles too, which a box plot draws between `min` and `max`. `more`: (stat,
    number) a test adds to each group."""
    sd = math.sqrt(m.var)
    se = sd / math.sqrt(m.n)
    q = dist.t_isf(alpha / 2.0, m.n - 1)
    for stat, value in (("n", m.n), ("mean", m.mean), ("var", m.var), ("sd", sd), ("se", se),
                        ("ci_low", m.mean - q * se), ("ci_high", m.mean + q * se), ("min", m.min), ("max", m.max), *more):
        out.number("group", stat, value, level=label)
    _write_quartiles(out, label, quartiles)


def _write_quartiles(out: Rows, label: str, quartiles: Quartiles | None) -> None:
    if quartiles is not None:
        for stat, value in (("q1", quartiles.q1), ("median", quartiles.median), ("q3", quartiles.q3)):
            out.number("group", stat, value, level=label)


def _refuse_unusable(test: str, value: str, label: str | None, m: Moments, least: int = 2) -> None:
    """A group (or the column, `label` None) with too few values, or infinite ones."""
    where = f"the group «{label}»" if label is not None else f"the column «{value}»"
    if m.n < least:
        raise refusal(
            test,
            f"{where} has {m.n} usable value{'' if m.n == 1 else 's'}"
            + (f" of «{value}»" if label is not None else "")
            + f" (a NULL or a NaN is left out): at least {least} are needed",
        )
    if not all(v is not None and math.isfinite(v) for v in (m.min, m.max, m.mean, m.var)):
        raise refusal(test, f"the column «{value}» holds infinite values" + (f" in the group «{label}»" if label is not None else "")
                      + ": the test cannot be computed")


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


def _write_histogram(out: Rows, source, value: Any, group: str | None, labels: list[str], groups: list,
                     lo: float | None = None, hi: float | None = None) -> None:
    """The `hist` section: for each group the rows in each of the same equal intervals,
    between the smallest and the largest value of the groups together. Their number is
    Sturges' for the largest group, between `MIN_BINS` and `MAX_BINS`; the last interval
    holds its upper edge (its label closes with a square bracket); an empty one is written
    with 0. Without a group (`group` None) the one measure is written under `labels[0]`;
    `groups` give the counts (`n`), and the extremes when `lo` and `hi` are not given."""
    lo = min(m.min for m in groups) if lo is None else lo
    hi = max(m.max for m in groups) if hi is None else hi
    n_max = max(m.n for m in groups)
    bins = min(MAX_BINS, max(MIN_BINS, (n_max - 1).bit_length() + 1))  # ceil(log2(n)) + 1
    counts = {label: [0] * bins for label in labels}
    for b in source.histogram(value, group, lo, hi, bins):
        label = labels[0] if group is None else None if b.level is None else label_of(b.level)
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


def _write_positive_curve(out: Rows, pdf: Callable[[float], float], isf: Callable[[float], float], statistic: float,
                          critical_low: float | None, critical_high: float | None) -> None:
    """The `curve` section of a statistic that is never negative (F, chi-square): its
    density from 0 to max(q(0.999), 1.15·statistic), the critical region as for t. Where the
    density is infinite (at 0, for one degree of freedom) its row is NULL."""
    reach = max(isf(0.001), 1.15 * statistic)
    step = reach / (CURVE_POINTS - 1)
    xs = [i * step for i in range(CURVE_POINTS)]
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


# ── adjusting for many comparisons ──────────────────────────────────────────
def p_adjusted(p_values: list[float], method: str) -> list[float]:
    """The p-values of m comparisons adjusted for their number: Holm's step-down (the
    running maximum of min(1, (m − i + 1)·p₍ᵢ₎) over the sorted ones), Bonferroni's min(1, m·p),
    Šidák's 1 − (1 − p)^m (as −expm1(m·log1p(−p))), or as they are."""
    m = len(p_values)
    if method == "bonferroni":
        return [min(1.0, m * x) for x in p_values]
    if method == "sidak":
        return [min(1.0, -math.expm1(m * math.log1p(-x))) if x < 1.0 else 1.0 for x in p_values]
    if method == "holm":
        order = sorted(range(m), key=lambda i: p_values[i])
        out = [0.0] * m
        running = 0.0
        for rank, i in enumerate(order):
            running = max(running, min(1.0, (m - rank) * p_values[i]))
            out[i] = running
        return out
    return list(p_values)


def _per_pair_alpha(alpha: float, m: int, method: str) -> float | None:
    """The level of each pair's interval, so that the m intervals hold together at 1 − α
    (Bonferroni: α/m; Šidák: 1 − (1 − α)^(1/m)); α for none; None for Holm, which has no
    simultaneous interval."""
    if method == "bonferroni":
        return alpha / m
    if method == "sidak":
        return -math.expm1(math.log1p(-alpha) / m)
    if method == "none":
        return alpha
    return None


def _welch_df(va: float, na: int, vb: float, nb: int) -> float:
    """Welch–Satterthwaite's degrees of freedom of a difference of two means, va = s²/n."""
    return (va + vb) ** 2 / (va * va / (na - 1) + vb * vb / (nb - 1))


# ── two groups of a numeric column ──────────────────────────────────────────
def _choose_two(source, test: str, p: dict[str, Any]) -> tuple[list[str], list, bool]:
    """The two groups a two-sample test compares, the first minus the second: their labels,
    the values of the grouping column that are those two, typed as the engine gave them, and
    whether an aggregate may keep to them by those values (`chosen`: two named, of a column a
    literal compares with; otherwise the groups were listed)."""
    group = p["group"]
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
    return list(wanted), typed, chosen is not None


def _two_groups(source, test: str, p: dict[str, Any]) -> tuple[list[str], list[Moments], int, list]:
    """The two groups a two-sample test compares, the first minus the second: their labels,
    their moments, the rows left out (no group, or no value in one of the two), and the
    values of the grouping column that are those two groups, typed as the engine gave them
    (what an aggregate of the report keeps to: `quartiles`)."""
    value, group = p["value"], p["group"]
    wanted, typed, chosen = _choose_two(source, test, p)
    by_label: dict[str, Moments] = {}
    without_group = 0
    for m in source.moments(value, group, typed if chosen else None):
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
    _write_numbers(out, (
        ("alpha", alpha), ("conf_level", 1.0 - alpha), ("n", a.n + b.n), ("n_missing", missing),
        ("pooled_var", pooled_var), ("estimate", estimate), ("null_value", mu), ("std_error", se),
        ("statistic", t), ("df", df), ("critical_low", critical_low), ("critical_high", critical_high),
        ("p_value", p_value), ("ci_low", ci_low), ("ci_high", ci_high),
        ("cohen_d", cohen_d), ("hedges_g", hedges_g),
    ))
    report = p["detail"] == "report"
    quartiles = _quartiles_by_label(source, value, p["group"], typed) if report else {}
    _write_group(out, g1, a, alpha, quartiles.get(g1))
    _write_group(out, g2, b, alpha, quartiles.get(g2))
    if report:
        _write_histogram(out, source, value, p["group"], [g1, g2], [a, b])
        _write_curve(out, lambda x: dist.t_pdf(x, df), t, critical_low, critical_high)

# ── one sample ──────────────────────────────────────────────────────────────
def _t_one_sample(source, p: dict[str, Any], out: Rows, engine_name: str) -> None:
    """Student's t for one sample. H0: mean = mu."""
    test = "t_one_sample"
    value, mu, alpha, alternative = p["value"], p["mu"], p["alpha"], p["alternative"]
    [m] = source.moments(value)
    _refuse_unusable(test, value, None, m)
    if m.var == 0.0:
        raise refusal(test, f"all the values of «{value}» are equal (the variance is 0): there is no t statistic")
    sd = math.sqrt(m.var)
    se = sd / math.sqrt(m.n)
    df = float(m.n - 1)
    # the mean less mu, from the centred mean: no digit lost to a centre near 1e9
    departure = (m.centre - mu) + m.mean_centred
    t = departure / se
    estimate = m.mean
    p_value, critical_low, critical_high, ci_low, ci_high = _t_tails(t, df, alternative, alpha, estimate, se)
    mean = f"mean({value})"
    _write_head(out, test, "one_sample", p, f"{mean} = {_num(mu)}", f"{mean} {_RELATION[alternative]} {_num(mu)}",
                "t = (m − mu) / (s / √n), df = n − 1", f"t({df:.6g})", p_value, engine_name, [])
    _write_numbers(out, (
        ("alpha", alpha), ("conf_level", 1.0 - alpha), ("n", m.n), ("n_missing", m.rows - m.n),
        ("mean", m.mean), ("var", m.var), ("sd", sd),
        ("estimate", estimate), ("null_value", mu), ("std_error", se), ("statistic", t), ("df", df),
        ("critical_low", critical_low), ("critical_high", critical_high), ("p_value", p_value),
        ("ci_low", ci_low), ("ci_high", ci_high), ("cohen_d", departure / sd),
    ))
    report = p["detail"] == "report"
    quartiles = source.quartiles(value) if report else []
    _write_group(out, value, m, alpha, quartiles[0] if quartiles else None)
    if report:
        _write_histogram(out, source, value, None, [value], [m])
        _write_curve(out, lambda x: dist.t_pdf(x, df), t, critical_low, critical_high)


# ── two columns of the same rows ────────────────────────────────────────────
def _two_columns(source, test: str, p: dict[str, Any], least: int) -> PairMoments:
    """The moments of two columns over the rows where both are numbers, refused when the two
    are one column, when there are fewer than `least` pairs, or when a value is infinite."""
    x, y = p["x"], p["y"]
    if x == y:
        raise refusal(test, f"'x' and 'y' are both the column «{x}»: choose two different columns")
    pm = source.pair_moments(x, y)
    if pm.n < least:
        raise refusal(
            test,
            f"the columns «{x}» and «{y}» are both numbers on {pm.n} row{'' if pm.n == 1 else 's'} "
            f"(a NULL or a NaN in either leaves the row out): at least {least} are needed",
        )
    for column, extremes in ((x, (pm.min_x, pm.max_x)), (y, (pm.min_y, pm.max_y))):
        if not all(v is not None and math.isfinite(v) for v in extremes):
            raise refusal(test, f"the column «{column}» holds infinite values: the test cannot be computed")
    return pm


def _column_group(out: Rows, label: str, n: int, mean: float, var: float, lo: float, hi: float, alpha: float,
                  quartiles: Quartiles | None = None) -> None:
    """A column of a paired test as a group: the same rows as a group's."""
    _write_group(out, label, Moments(label, n, n, mean, var, lo, hi, 0.0, mean), alpha, quartiles)


def _t_paired(source, p: dict[str, Any], out: Rows, engine_name: str) -> None:
    """Student's t for paired values: the one-sample t on the differences x − y.
    H0: mean(x − y) = mu."""
    test = "t_paired"
    x, y, mu, alpha, alternative = p["x"], p["y"], p["mu"], p["alpha"], p["alternative"]
    pm = _two_columns(source, test, p, 2)
    if pm.var_d == 0.0:
        raise refusal(test, f"the differences «{x}» − «{y}» are all equal (their variance is 0): there is no t statistic")
    n = pm.n
    sd_d = math.sqrt(pm.var_d)
    se = sd_d / math.sqrt(n)
    df = float(n - 1)
    departure = ((pm.centre_x - pm.centre_y) - mu) + pm.mean_cd  # mean(x − y) − mu, from the centred means
    mean_diff = (pm.centre_x - pm.centre_y) + pm.mean_cd
    t = departure / se
    p_value, critical_low, critical_high, ci_low, ci_high = _t_tails(t, df, alternative, alpha, mean_diff, se)
    difference = f"mean({x} − {y})"
    _write_head(out, test, "paired", p, f"{difference} = {_num(mu)}", f"{difference} {_RELATION[alternative]} {_num(mu)}",
                "t = (d − mu) / (s_d / √n), d = mean(x − y), s_d² = var(x − y), df = n − 1", f"t({df:.6g})",
                p_value, engine_name, [])
    _write_numbers(out, (
        ("alpha", alpha), ("conf_level", 1.0 - alpha), ("n", n), ("n_missing", pm.rows - n),
        ("mean_diff", mean_diff), ("var_diff", pm.var_d), ("sd_diff", sd_d),
        ("estimate", mean_diff), ("null_value", mu), ("std_error", se), ("statistic", t), ("df", df),
        ("critical_low", critical_low), ("critical_high", critical_high), ("p_value", p_value),
        ("ci_low", ci_low), ("ci_high", ci_high), ("cohen_dz", departure / sd_d),
    ))
    report = p["detail"] == "report"
    label_d = paired_difference_label(x, y)
    q = source.quartiles_many([Paired(x, y), Paired(y, x), Difference(x, y)]) if report else [None] * 3
    _column_group(out, x, n, pm.centre_x + pm.mean_cx, pm.var_x, pm.min_x, pm.max_x, alpha, q[0])
    _column_group(out, y, n, pm.centre_y + pm.mean_cy, pm.var_y, pm.min_y, pm.max_y, alpha, q[1])
    _column_group(out, label_d, n, mean_diff, pm.var_d, pm.min_d, pm.max_d, alpha, q[2])
    if report:
        _write_histogram(out, source, Difference(x, y), None, [label_d],
                         [Moments(label_d, n, n, mean_diff, pm.var_d, pm.min_d, pm.max_d, 0.0, mean_diff)])
        _write_curve(out, lambda v: dist.t_pdf(v, df), t, critical_low, critical_high)


def paired_difference_label(x: str, y: str) -> str:
    """The group a paired test writes for the differences: «before − after»."""
    return f"{x} − {y}"


def _pearson(source, p: dict[str, Any], out: Rows, engine_name: str) -> None:
    """Pearson's correlation, by its t. H0: correlation = 0."""
    test = "pearson"
    x, y, alpha, alternative = p["x"], p["y"], p["alpha"], p["alternative"]
    pm = _two_columns(source, test, p, 3)
    for column, var in ((x, pm.var_x), (y, pm.var_y)):
        if var == 0.0:
            raise refusal(test, f"all the values of «{column}» are equal on the rows used (the variance is 0): there is no correlation")
    n = pm.n
    r = max(-1.0, min(1.0, pm.cov / math.sqrt(pm.var_x * pm.var_y)))
    df = float(n - 2)
    one_minus = (1.0 - r) * (1.0 + r)
    se = math.sqrt(one_minus / df)
    t = r / se if se > 0.0 else math.copysign(math.inf, r)
    p_value, critical_low, critical_high, _, _ = _t_tails(t, df, alternative, alpha, 0.0, 0.0)
    # the interval of ρ by Fisher's z: tanh(atanh(r) ± z·/√(n − 3)), n ≥ 4
    fisher_z = math.atanh(r) if abs(r) < 1.0 else math.copysign(math.inf, r)
    fisher_se = 1.0 / math.sqrt(n - 3) if n > 3 else None
    ci_low = ci_high = z_quantile = None
    if fisher_se is not None:
        z_quantile = dist.norm_isf(alpha / 2.0 if alternative == "two_sided" else alpha)
        if alternative in ("two_sided", "greater"):
            ci_low = math.tanh(fisher_z - z_quantile * fisher_se)
        if alternative in ("two_sided", "less"):
            ci_high = math.tanh(fisher_z + z_quantile * fisher_se)
    cor = f"cor({x}, {y})"
    _write_head(out, test, "t", p, f"{cor} = 0", f"{cor} {_RELATION[alternative]} 0",
                "r = cov(x, y) / √(var(x)·var(y)), t = r·√((n − 2) / (1 − r²)), df = n − 2, "
                "CI = tanh(atanh(r) ± z / √(n − 3))", f"t({df:.6g})", p_value, engine_name, [])
    _write_numbers(out, (
        ("alpha", alpha), ("conf_level", 1.0 - alpha), ("n", n), ("n_missing", pm.rows - n),
        ("covariance", pm.cov), ("fisher_z", fisher_z), ("fisher_se", fisher_se), ("z_quantile", z_quantile),
        ("estimate", r), ("null_value", 0.0), ("std_error", se), ("statistic", t), ("df", df),
        ("critical_low", critical_low), ("critical_high", critical_high), ("p_value", p_value),
        ("ci_low", ci_low), ("ci_high", ci_high), ("r_squared", r * r),
    ))
    for label, mean, var, lo, hi in ((x, pm.centre_x + pm.mean_cx, pm.var_x, pm.min_x, pm.max_x),
                                     (y, pm.centre_y + pm.mean_cy, pm.var_y, pm.min_y, pm.max_y)):
        for stat, number in (("n", n), ("mean", mean), ("var", var), ("sd", math.sqrt(var)), ("min", lo), ("max", hi)):
            out.number("group", stat, number, level=label)
    if p["detail"] == "report":
        _write_curve(out, lambda v: dist.t_pdf(v, df), t, critical_low, critical_high)


# ── several groups of a numeric column ──────────────────────────────────────
def _several_groups(source, test: str, p: dict[str, Any]) -> tuple[list[str], dict[str, Any], int]:
    """The groups of a test that compares them all: their labels in text order, each with
    its typed value, and the rows with no group. Refused: fewer than two groups, more than
    `MAX_STAT_GROUPS`."""
    group = p["group"]
    levels = source.levels(group, MAX_STAT_GROUPS + 2)
    named = [lv for lv in levels if lv.value is not None]
    if len(named) > MAX_STAT_GROUPS:
        raise refusal(test, f"the column «{group}» has more than {MAX_STAT_GROUPS} groups: is it the right column?")
    labels = sorted(label_of(lv.value) for lv in named)
    if not labels:
        raise refusal(test, f"the column «{group}» has no group: the step has no rows, or every row is NULL there")
    if len(labels) < 2:
        raise refusal(test, f"the test compares groups and the column «{group}» has 1 ({labels[0]}): it needs at least two")
    typed = {label_of(lv.value): lv.value for lv in named}
    return labels, typed, sum(lv.rows for lv in levels if lv.value is None)


def _list_equal(fn: str, value: str, labels: list[str]) -> str:
    """mean(v | a) = mean(v | b) = mean(v | c), with «...» in the middle past three groups."""
    shown = labels if len(labels) <= 3 else [labels[0], labels[1], None, labels[-1]]
    return " = ".join("..." if g is None else f"{fn}({value} | {g})" for g in shown)


def _anova_table(ns: list[int], means_c: list[float], variances: list[float]) -> dict[str, float]:
    """The classic one-way ANOVA of groups given by their n, mean (any centre, the same for
    all) and variance."""
    k, total = len(ns), sum(ns)
    grand_c = sum(n * m for n, m in zip(ns, means_c)) / total
    ss_between = sum(n * (m - grand_c) ** 2 for n, m in zip(ns, means_c))
    ss_within = sum((n - 1) * v for n, v in zip(ns, variances))
    return {"k": k, "N": total, "grand_c": grand_c, "ss_between": ss_between, "ss_within": ss_within,
            "ms_between": ss_between / (k - 1), "ms_within": ss_within / (total - k) if total > k else math.nan}


def _anova(source, p: dict[str, Any], out: Rows, engine_name: str) -> None:
    """The one-way ANOVA, classic or Welch's, and its pairwise comparisons. H0: all the
    means are equal."""
    test = "anova"
    value, group, alpha, posthoc = p["value"], p["group"], p["alpha"], p["posthoc"]
    labels, typed, without_group = _several_groups(source, test, p)
    if posthoc != "none" and len(labels) > MAX_POSTHOC_GROUPS:
        raise refusal(test, f"the pairwise comparisons ('posthoc') take {MAX_POSTHOC_GROUPS} groups at most, "
                            f"and the column «{group}» has {len(labels)}: filter the groups first, or set 'posthoc' to none")
    by_label = {label_of(m.level): m for m in source.moments(value, group) if m.level is not None}
    groups = [by_label[g] for g in labels]
    for g, m in zip(labels, groups):
        _refuse_unusable(test, value, g, m)
    ns = [m.n for m in groups]
    table = _anova_table(ns, [m.mean_centred for m in groups], [m.var for m in groups])
    k, total = table["k"], table["N"]
    if table["ss_within"] == 0.0:
        raise refusal(test, f"all the values of «{value}» are equal inside every group (the variance within the groups is 0): "
                            "there is no F statistic")
    ms_within = table["ms_within"]
    ss_total = table["ss_between"] + table["ss_within"]
    eta_squared = table["ss_between"] / ss_total
    omega_squared = (table["ss_between"] - (k - 1) * ms_within) / (ss_total + ms_within)
    notes: list[str] = []
    welch: tuple = ()
    if p["var_equal"]:
        variant = "one_way"
        f = table["ms_between"] / ms_within
        d1, d2 = float(k - 1), float(total - k)
        formula = ("F = ms_between / ms_within, ms_between = ss_between / (k − 1), ms_within = ss_within / (N − k), "
                   "ss_between = ∑ n·(m − M)², ss_within = ∑ (n − 1)·s², M = ∑ n·m / N, df = k − 1, N − k")
        variances = [m.var for m in groups]
        if min(variances) == 0.0 or max(variances) / min(variances) > 4.0:
            notes.append("The largest variance of a group is more than 4 times the smallest: Welch's ANOVA "
                         "(var_equal = false) does not assume them equal.")
    else:
        variant = "welch"
        for g, m in zip(labels, groups):
            if m.var == 0.0:
                raise refusal(test, f"all the values of «{value}» are equal in the group «{g}» (its variance is 0): "
                                    "Welch's ANOVA weighs each group by 1 / variance; use the classic ANOVA (var_equal = true)")
        w = [m.n / m.var for m in groups]
        sum_w = sum(w)
        mw_c = sum(wi * m.mean_centred for wi, m in zip(w, groups)) / sum_w
        lam = sum((1.0 - wi / sum_w) ** 2 / (m.n - 1) for wi, m in zip(w, groups))
        f = (sum(wi * (m.mean_centred - mw_c) ** 2 for wi, m in zip(w, groups)) / (k - 1)) / (1.0 + 2.0 * (k - 2) * lam / (k * k - 1))
        d1, d2 = float(k - 1), (k * k - 1) / (3.0 * lam)
        formula = ("F = [∑ w·(m − m_w)² / (k − 1)] / [1 + 2·(k − 2)·Λ / (k² − 1)], w = n / s², m_w = ∑ w·m / ∑ w, "
                   "Λ = ∑ (1 − w / ∑ w)² / (n − 1), df = k − 1, (k² − 1) / (3·Λ)")
        welch = (("sum_weights", sum_w), ("weighted_mean", groups[0].centre + mw_c), ("welch_lambda", lam))
    p_value, critical_high = _f_tails(f, d1, d2, alpha)
    pairs_rows: list[tuple] = []
    extra: tuple = ()
    pairs_texts = None
    if posthoc != "none":
        if p_value >= alpha:
            notes.append(f"The ANOVA does not reject H0 at α = {_num(alpha)}: the pairwise comparisons are shown all the same, "
                         "and a difference they find is to be read with care.")
        pairs_rows, extra, adjust = _anova_pairs(p, labels, groups, ms_within, total)
        pairs_texts = (posthoc, adjust, _posthoc_formula(posthoc, adjust, p["var_equal"]))
    _write_head(out, test, variant, p, _list_equal("mean", value, labels), f"max |mean({value} | i) − mean({value} | j)| > 0",
                formula, f"F({d1:.6g}, {d2:.6g})", p_value, engine_name, notes, pairs_texts)
    _write_numbers(out, (
        ("alpha", alpha), ("conf_level", 1.0 - alpha), ("n", total), ("n_missing", without_group + sum(m.rows - m.n for m in groups)),
        ("k", k), ("grand_mean", groups[0].centre + table["grand_c"]), ("ss_between", table["ss_between"]),
        ("ss_within", table["ss_within"]), ("ms_between", table["ms_between"]), ("ms_within", ms_within), *welch,
        ("statistic", f), ("df_1", d1), ("df_2", d2), ("critical_high", critical_high), ("p_value", p_value),
        ("eta_squared", eta_squared), ("omega_squared", omega_squared), *extra,
    ))
    report = p["detail"] == "report"
    quartiles = _quartiles_by_label(source, value, group) if report else {}
    for g, m in zip(labels, groups):
        _write_group(out, g, m, alpha, quartiles.get(g), (("weight", m.n / m.var),) if variant == "welch" else ())
    for row in pairs_rows:
        _write_pair(out, *row)
    if report:
        _write_histogram(out, source, value, group, labels, groups)
        _write_positive_curve(out, lambda x: dist.f_pdf(x, d1, d2), lambda q: dist.f_isf(q, d1, d2), f, None, critical_high)


def _anova_pairs(p: dict[str, Any], labels: list[str], groups: list[Moments], ms_within: float, total: int):
    """The pairwise comparisons after an ANOVA: for each pair (i, j), in the groups' order,
    (first, second, estimate, std_error, statistic, df, critical, p_value, p_adjusted, ci_low,
    ci_high); the numbers of the `test` section they add; how the p-values are adjusted."""
    posthoc, alpha, k = p["posthoc"], p["alpha"], len(groups)
    index = [(i, j) for i in range(k) for j in range(i + 1, k)]
    m = len(index)
    rows = []
    if posthoc in ("tukey", "games_howell"):
        df_all = float(total - k)
        critical_all = dist.srange_isf(alpha, k, df_all) if posthoc == "tukey" else None
        for i, j in index:
            a, b = groups[i], groups[j]
            d = a.mean_centred - b.mean_centred
            if posthoc == "tukey":
                se = math.sqrt(ms_within / 2.0 * (1.0 / a.n + 1.0 / b.n))
                df, q = df_all, abs(d) / se
                critical = critical_all
            else:
                va, vb = a.var / a.n, b.var / b.n
                se = math.sqrt(va + vb)
                df = _welch_df(va, a.n, vb, b.n)
                q = math.sqrt(2.0) * abs(d) / se
                critical = dist.srange_isf(alpha, k, df) / math.sqrt(2.0)
            rows.append([labels[i], labels[j], d, se, q, df, critical, None, dist.srange_sf(q, k, df),
                         d - critical * se, d + critical * se])
        extra = (("n_pairs", m),) + ((("posthoc_critical", critical_all),) if posthoc == "tukey" else ())
        return [(r, alpha) for r in rows], extra, "studentized_range"
    # pairwise t tests, each with its own p-value, adjusted for the number of pairs
    adjust = p["p_adjust"]
    pair_alpha = _per_pair_alpha(alpha, m, adjust)
    for i, j in index:
        a, b = groups[i], groups[j]
        d = a.mean_centred - b.mean_centred
        if p["var_equal"]:
            se = math.sqrt(ms_within * (1.0 / a.n + 1.0 / b.n))
            df = float(total - k)
        else:
            va, vb = a.var / a.n, b.var / b.n
            se = math.sqrt(va + vb)
            df = _welch_df(va, a.n, vb, b.n)
        t = d / se
        critical = dist.t_isf(pair_alpha / 2.0, df) if pair_alpha is not None else None
        rows.append([labels[i], labels[j], d, se, t, df, critical, min(1.0, 2.0 * dist.t_sf(abs(t), df)), None,
                     None if critical is None else d - critical * se, None if critical is None else d + critical * se])
    for row, adjusted in zip(rows, p_adjusted([r[7] for r in rows], adjust)):
        row[8] = adjusted
    extra = (("n_pairs", m),) + ((("alpha_adjusted", pair_alpha),) if adjust in ("bonferroni", "sidak") else ())
    return [(r, alpha) for r in rows], extra, adjust


_WELCH_PAIR = "df = (s_i²/n_i + s_j²/n_j)² / ((s_i²/n_i)²/(n_i − 1) + (s_j²/n_j)²/(n_j − 1))"


def _posthoc_formula(posthoc: str, adjust: str, var_equal: bool) -> str:
    """The formulas of a pair (i, j) of the comparisons, in symbols: the `posthoc_formula`
    row, beside the test's own `formula`."""
    if posthoc == "tukey":
        return ("se = √(ms_within / 2 · (1/n_i + 1/n_j)), q = |m_i − m_j| / se, df = N − k, p_adj = P(Q(k, df) ≥ q), "
                "CI = (m_i − m_j) ± q(1 − α; k, df) · se")
    if posthoc == "games_howell":
        return (f"se = √(s_i²/n_i + s_j²/n_j), q = √2 · |m_i − m_j| / se, {_WELCH_PAIR}, p_adj = P(Q(k, df) ≥ q), "
                "CI = (m_i − m_j) ± q(1 − α; k, df) / √2 · se")
    head = ("se = √(ms_within · (1/n_i + 1/n_j)), t = (m_i − m_j) / se, df = N − k" if var_equal
            else f"se = √(s_i²/n_i + s_j²/n_j), t = (m_i − m_j) / se, {_WELCH_PAIR}")
    tail = {
        "holm": "p_adj = Holm's: the p sorted, p(1) ≤ ... ≤ p(n_pairs), p_adj(r) = max over r' ≤ r of min(1, (n_pairs − r' + 1) · p(r'))",
        "bonferroni": "p_adj = min(1, n_pairs · p), CI = (m_i − m_j) ± t(1 − α'/2; df) · se, α' = α / n_pairs",
        "sidak": "p_adj = 1 − (1 − p)^n_pairs, CI = (m_i − m_j) ± t(1 − α'/2; df) · se, α' = 1 − (1 − α)^(1/n_pairs)",
        "none": "p_adj = p, CI = (m_i − m_j) ± t(1 − α/2; df) · se",
    }[adjust]
    return f"{head}, p = 2 · P(T(df) ≥ |t|), {tail}"


def _write_pair(out: Rows, row: list, alpha: float) -> None:
    """A pair's rows: the same eleven for every method, NULL where the method has none; the
    significance code of the adjusted p-value after it."""
    first, second, estimate, se, statistic, df, critical, p_value, adjusted, ci_low, ci_high = row
    for stat, number in (("estimate", estimate), ("std_error", se), ("statistic", statistic), ("df", df),
                         ("critical", critical), ("p_value", p_value), ("p_adjusted", adjusted)):
        out.number("pair", stat, number, level=first, level_2=second)
    out.text("pair", "significance", significance(adjusted) or "", level=first, level_2=second)
    for stat, number in (("ci_low", ci_low), ("ci_high", ci_high)):
        out.number("pair", stat, number, level=first, level_2=second)
    out.text("pair", "decision", "reject_h0" if adjusted < alpha else "fail_to_reject_h0", level=first, level_2=second)


# ── variances ───────────────────────────────────────────────────────────────
def _var_f(source, p: dict[str, Any], out: Rows, engine_name: str) -> None:
    """The F test of two variances. H0: variance₁ / variance₂ = ratio."""
    test = "var_f"
    (g1, g2), (a, b), missing, typed = _two_groups(source, test, p)
    value, ratio, alpha, alternative = p["value"], p["ratio"], p["alpha"], p["alternative"]
    for g, m in ((g1, a), (g2, b)):
        if m.var == 0.0:
            raise refusal(test, f"all the values of «{value}» are equal in the group «{g}» (its variance is 0): there is no F statistic")
    estimate = a.var / b.var
    f = estimate / ratio
    d1, d2 = float(a.n - 1), float(b.n - 1)
    if alternative == "two_sided":
        p_value = min(1.0, 2.0 * min(dist.f_cdf(f, d1, d2), dist.f_sf(f, d1, d2)))
        critical_low, critical_high = dist.f_ppf(alpha / 2.0, d1, d2), dist.f_isf(alpha / 2.0, d1, d2)
        ci_low, ci_high = estimate / critical_high, estimate / critical_low
    elif alternative == "less":
        p_value = dist.f_cdf(f, d1, d2)
        critical_low, critical_high = dist.f_ppf(alpha, d1, d2), None
        ci_low, ci_high = None, estimate / critical_low
    else:
        p_value = dist.f_sf(f, d1, d2)
        critical_low, critical_high = None, dist.f_isf(alpha, d1, d2)
        ci_low, ci_high = estimate / critical_high, None
    quotient = f"var({value} | {g1}) / var({value} | {g2})"
    _write_head(out, test, "f", p, f"{quotient} = {_num(ratio)}", f"{quotient} {_RELATION[alternative]} {_num(ratio)}",
                "F = (s1² / s2²) / ratio, df = n1 − 1, n2 − 1; CI = (s1² / s2²) / q", f"F({d1:.6g}, {d2:.6g})",
                p_value, engine_name, [])
    _write_numbers(out, (
        ("alpha", alpha), ("conf_level", 1.0 - alpha), ("n", a.n + b.n), ("n_missing", missing),
        ("estimate", estimate), ("null_value", ratio), ("statistic", f), ("df_1", d1), ("df_2", d2),
        ("critical_low", critical_low), ("critical_high", critical_high), ("p_value", p_value),
        ("ci_low", ci_low), ("ci_high", ci_high),
    ))
    report = p["detail"] == "report"
    quartiles = _quartiles_by_label(source, value, p["group"], typed) if report else {}
    _write_group(out, g1, a, alpha, quartiles.get(g1))
    _write_group(out, g2, b, alpha, quartiles.get(g2))
    if report:
        _write_histogram(out, source, value, p["group"], [g1, g2], [a, b])
        _write_positive_curve(out, lambda x: dist.f_pdf(x, d1, d2), lambda q: dist.f_isf(q, d1, d2), f,
                              critical_low, critical_high)


def _levene(source, p: dict[str, Any], out: Rows, engine_name: str) -> None:
    """Levene's test of equal variances (Brown–Forsythe's with the median): the classic
    ANOVA F on the absolute deviations of each value from the centre of its group.
    H0: all the variances are equal."""
    test = "levene"
    value, group, alpha, center = p["value"], p["group"], p["alpha"], p["center"]
    labels, typed, without_group = _several_groups(source, test, p)
    by_label = {label_of(d.level): d for d in source.abs_dev_moments(value, group, center) if d.level is not None}
    devs = [by_label[g] for g in labels]
    for g, d in zip(labels, devs):
        _refuse_unusable(test, value, g, Moments(d.level, d.rows, d.n, d.mean, d.var, d.min, d.max, 0.0, d.mean))
    table = _anova_table([d.n for d in devs], [d.mean for d in devs], [d.var for d in devs])
    k, total = table["k"], table["N"]
    if table["ss_within"] == 0.0:
        raise refusal(test, f"every value of «{value}» is as far from the {center} of its group as the others of the group "
                            "(the variance within the groups is 0): there is no F statistic")
    f = table["ms_between"] / table["ms_within"]
    d1, d2 = float(k - 1), float(total - k)
    p_value, critical_high = _f_tails(f, d1, d2, alpha)
    formula = (f"z = |v − {center} of its group|; F = ms_between / ms_within of z, ms_between = ss_between / (k − 1), "
               "ms_within = ss_within / (N − k), ss_between = ∑ n·(z̄ − Z)², ss_within = ∑ (n − 1)·s_z², Z = ∑ n·z̄ / N, "
               "df = k − 1, N − k")
    _write_head(out, test, center, p, _list_equal("var", value, labels), f"max |var({value} | i) − var({value} | j)| > 0",
                formula, f"F({d1:.6g}, {d2:.6g})", p_value, engine_name, [])
    _write_numbers(out, (
        ("alpha", alpha), ("conf_level", 1.0 - alpha), ("n", total), ("n_missing", without_group + sum(d.rows - d.n for d in devs)),
        ("k", k), ("grand_mean", table["grand_c"]), ("ss_between", table["ss_between"]), ("ss_within", table["ss_within"]),
        ("ms_between", table["ms_between"]), ("ms_within", table["ms_within"]),
        ("statistic", f), ("df_1", d1), ("df_2", d2), ("critical_high", critical_high), ("p_value", p_value),
    ))
    report = p["detail"] == "report"
    quartiles = _quartiles_by_label(source, value, group) if report else {}
    for g, d in zip(labels, devs):
        sd = math.sqrt(d.var)
        se = sd / math.sqrt(d.n)
        q = dist.t_isf(alpha / 2.0, d.n - 1)
        for stat, number in (("n", d.n), ("center", d.center), ("dev_mean", d.mean), ("dev_var", d.var), ("dev_sd", sd),
                             ("dev_se", se), ("dev_ci_low", d.mean - q * se), ("dev_ci_high", d.mean + q * se),
                             ("min", d.min), ("max", d.max)):
            out.number("group", stat, number, level=g)
        _write_quartiles(out, g, quartiles.get(g))
    if report:
        _write_histogram(out, source, value, group, labels, devs)
        _write_positive_curve(out, lambda x: dist.f_pdf(x, d1, d2), lambda q: dist.f_isf(q, d1, d2), f, None, critical_high)



# ── proportions ─────────────────────────────────────────────────────────────
def _success_value(source, test: str, value: str, success: Any) -> Any:
    """The success as the outcome column holds it, or a refusal that says why it cannot be."""
    kind = source.kind_of(value)
    if kind is None:
        dtype = source.columns()[value].dtype
        raise refusal(test, f"the column «{value}» ({dtype}) cannot be compared with a value written as text: "
                            "a test of proportions reads a column of texts, whole or float numbers, dates or true/false; cast it first")
    typed = typed_value(success, kind)
    if typed is None:
        said = {"integer": "a whole number", "float": "a number", "boolean": "true or false", "date": "a date (2024-03-01)"}[kind]
        raise refusal(test, f"'success' («{label_of(success) if not isinstance(success, str) else success}») is not a value the column "
                            f"«{value}» can hold: it holds {said}")
    return typed


def _success_label(success: Any) -> str:
    return success if isinstance(success, str) else label_of(success)


def _norm_tails(z: float, alternative: str, alpha: float) -> tuple[float, float | None, float | None, float]:
    """(p-value, critical_low, critical_high, the quantile of the interval) of a normal z."""
    if alternative == "two_sided":
        q = dist.norm_isf(alpha / 2.0)
        return min(1.0, 2.0 * dist.norm_sf(abs(z))), -q, q, q
    q = dist.norm_isf(alpha)
    if alternative == "less":
        return dist.norm_cdf(z), -q, None, q
    return dist.norm_sf(z), None, q, q


def _wilson(k: int, n: int, z: float, yates: float) -> tuple[float, float]:
    """Wilson's score interval of k successes in n with the quantile z, and the continuity
    correction `yates` (R's `prop.test`): each bound from p ± yates/n, kept inside [0, 1]."""
    p_hat = k / n
    z22n = z * z / (2.0 * n)

    def bound(pc: float, sign: float) -> float:
        return (pc + z22n + sign * z * math.sqrt(pc * (1.0 - pc) / n + z22n / (2.0 * n))) / (1.0 + 2.0 * z22n)

    upper = 1.0 if p_hat + yates / n >= 1.0 else min(1.0, bound(p_hat + yates / n, 1.0))
    lower = 0.0 if p_hat - yates / n <= 0.0 else max(0.0, bound(p_hat - yates / n, -1.0))
    return lower, upper


def _cohen_h(p1: float, p2: float) -> float:
    return 2.0 * math.asin(math.sqrt(p1)) - 2.0 * math.asin(math.sqrt(p2))


_PROP_INTERVAL_TEXT = "CI = (p_c + z²/(2n) ± z·√(p_c·(1 − p_c)/n + z²/(4n²))) / (1 + z²/n), p_c = k/n ± Y/n"


def _write_proportion_group(out: Rows, label: str, rows: int, n: int, k: int, interval: tuple[float, float]) -> None:
    """A group of a test of proportions: its values, successes, proportion, the proportion's
    standard error and its two-sided interval at 1 − alpha."""
    p_hat = k / n
    for stat, number in (("n", n), ("rows", rows), ("successes", k), ("proportion", p_hat),
                         ("se", math.sqrt(p_hat * (1.0 - p_hat) / n)), ("ci_low", interval[0]), ("ci_high", interval[1])):
        out.number("group", stat, number, level=label)


def _prop_one_sample(source, p: dict[str, Any], out: Rows, engine_name: str) -> None:
    """The test of one proportion, by the score (Wilson's interval, with or without the
    continuity correction) or exact (the binomial, Clopper–Pearson's interval).
    H0: P(value = success) = p0."""
    test = "prop_one_sample"
    value, p0, alpha, alternative = p["value"], p["p0"], p["alpha"], p["alternative"]
    success = _success_value(source, test, value, p["success"])
    said = _success_label(p["success"])
    [s] = source.successes(value, success)
    n, k = s.n, s.k
    if n == 0:
        raise refusal(test, f"the column «{value}» has no value on the rows that reach the step (a NULL or a NaN is left out)")
    notes = []
    if k == 0:
        notes.append(f"No row of «{value}» holds «{said}»: no row counts as a success. Is it the value the column holds?")
    p_hat = k / n
    share = f"P({value} = {said})"
    h0, h1 = f"{share} = {_num(p0)}", f"{share} {_RELATION[alternative]} {_num(p0)}"
    report = p["detail"] == "report"
    if p["method"] == "exact":
        if n + 1 > EXACT_SUPPORT:
            raise refusal(test, f"the exact binomial goes through n + 1 = {n + 1:,} outcomes, more than {EXACT_SUPPORT:,}: "
                                "use method = 'score', which is as good on so many rows")
        p_value = exact.binomial_test(k, n, p0, alternative)
        ci_low, ci_high = exact.clopper_pearson(k, n, alpha, alternative)
        tails = {"two_sided": "p = ∑ P(X = i), P(X = i) ≤ P(X = k)·(1 + 1e-7)", "less": "p = P(X ≤ k)", "greater": "p = P(X ≥ k)"}
        bounds = {"two_sided": "P(X ≥ k | p_L) = α/2, P(X ≤ k | p_U) = α/2", "less": "p_L = 0, P(X ≤ k | p_U) = α",
                  "greater": "P(X ≥ k | p_L) = α, p_U = 1"}
        formula = f"{tails[alternative]}, P(X = i) = C(n, i)·p0^i·(1 − p0)^(n − i); CI (Clopper–Pearson): {bounds[alternative]}"
        _write_head(out, test, "exact", p, h0, h1, formula, f"binomial({n}, {_num(p0)})", p_value, engine_name, notes)
        _write_numbers(out, (
            ("alpha", alpha), ("conf_level", 1.0 - alpha), ("n", n), ("n_missing", s.rows - n), ("successes", k),
            ("estimate", p_hat), ("null_value", p0), ("p_value", p_value), ("ci_low", ci_low), ("ci_high", ci_high),
            ("cohen_h", _cohen_h(p_hat, p0)),
        ))
        _write_proportion_group(out, value, s.rows, n, k, exact.clopper_pearson(k, n, alpha, "two_sided"))
        return
    yates = min(0.5, abs(k - n * p0)) if p["correction"] else 0.0
    chi_square = (abs(k - n * p0) - yates) ** 2 / (n * p0 * (1.0 - p0))
    z = math.copysign(math.sqrt(chi_square), p_hat - p0) if p_hat != p0 else 0.0
    p_value, critical_low, critical_high, z_quantile = _norm_tails(z, alternative, alpha)
    low, high = _wilson(k, n, z_quantile, yates)
    ci_low = low if alternative != "less" else 0.0
    ci_high = high if alternative != "greater" else 1.0
    formula = (f"z = ±√χ², χ² = (|k − n·p0| − Y)² / (n·p0·(1 − p0)), Y = {'min(0.5, |k − n·p0|)' if p['correction'] else '0'}; "
               + _PROP_INTERVAL_TEXT)
    _write_head(out, test, "score", p, h0, h1, formula, "normal", p_value, engine_name, notes)
    _write_numbers(out, (
        ("alpha", alpha), ("conf_level", 1.0 - alpha), ("n", n), ("n_missing", s.rows - n), ("successes", k),
        ("expected_successes", n * p0), ("yates", yates), ("chi_square", chi_square), ("z_quantile", z_quantile),
        ("estimate", p_hat), ("null_value", p0), ("std_error", math.sqrt(p0 * (1.0 - p0) / n)), ("statistic", z),
        ("critical_low", critical_low), ("critical_high", critical_high), ("p_value", p_value),
        ("ci_low", ci_low), ("ci_high", ci_high), ("cohen_h", _cohen_h(p_hat, p0)),
    ))
    _write_proportion_group(out, value, s.rows, n, k, _wilson(k, n, dist.norm_isf(alpha / 2.0), yates))
    if report:
        _write_curve(out, dist.norm_pdf, z, critical_low, critical_high)


def _prop_two_sample(source, p: dict[str, Any], out: Rows, engine_name: str) -> None:
    """The test of two proportions (R's `prop.test` of two groups): the chi-square of the 2 × 2
    table of successes and failures, as a z with the sign of the difference, with or without
    the continuity correction. H0: P(value = success | 1) = P(value = success | 2)."""
    test = "prop_two_sample"
    value, group, alpha, alternative = p["value"], p["group"], p["alpha"], p["alternative"]
    success = _success_value(source, test, value, p["success"])
    said = _success_label(p["success"])
    wanted, typed, chosen = _choose_two(source, test, p)
    by_label, without_group = {}, 0
    for s in source.successes(value, success, group, typed if chosen else None):
        if s.level is None:
            without_group += s.rows
        else:
            by_label[label_of(s.level)] = s
    a, b = (by_label[g] for g in wanted)
    for g, s in zip(wanted, (a, b)):
        if s.n == 0:
            raise refusal(test, f"the group «{g}» has no value of «{value}» (a NULL or a NaN is left out)")
    n1, n2, k1, k2 = a.n, b.n, a.k, b.k
    p1, p2 = k1 / n1, k2 / n2
    pooled = (k1 + k2) / (n1 + n2)
    if pooled in (0.0, 1.0):
        raise refusal(test, f"{'no' if pooled == 0.0 else 'every'} value of «{value}» in the two groups is «{said}»: "
                            "the two proportions are both " + ("0" if pooled == 0.0 else "1") + ", and there is nothing to test")
    estimate = p1 - p2
    inverse = 1.0 / n1 + 1.0 / n2
    yates = min(0.5, abs(estimate) / inverse) if p["correction"] else 0.0
    chi_square = (abs(estimate) - yates * inverse) ** 2 / (pooled * (1.0 - pooled) * inverse)
    z = math.copysign(math.sqrt(chi_square), estimate) if estimate != 0.0 else 0.0
    p_value, critical_low, critical_high, z_quantile = _norm_tails(z, alternative, alpha)
    width = z_quantile * math.sqrt(p1 * (1.0 - p1) / n1 + p2 * (1.0 - p2) / n2) + yates * inverse
    ci_low = max(estimate - width, -1.0) if alternative != "less" else -1.0
    ci_high = min(estimate + width, 1.0) if alternative != "greater" else 1.0
    difference = f"P({value} = {said} | {wanted[0]}) − P({value} = {said} | {wanted[1]})"
    formula = ("z = ±√χ², χ² = (|p1 − p2| − Y·I)² / (p·(1 − p)·I), p = (k1 + k2) / (n1 + n2), I = 1/n1 + 1/n2, "
               f"Y = {'min(0.5, |p1 − p2| / I)' if p['correction'] else '0'}; "
               "CI = (p1 − p2) ± (z_q·√(p1·(1 − p1)/n1 + p2·(1 − p2)/n2) + Y·I)")
    _write_head(out, test, "score", p, f"{difference} = 0", f"{difference} {_RELATION[alternative]} 0", formula, "normal",
                p_value, engine_name, [])
    _write_numbers(out, (
        ("alpha", alpha), ("conf_level", 1.0 - alpha), ("n", n1 + n2), ("n_missing", without_group + a.rows - n1 + b.rows - n2),
        ("pooled_proportion", pooled), ("inverse_sum", inverse), ("yates", yates), ("chi_square", chi_square),
        ("z_quantile", z_quantile), ("estimate", estimate), ("null_value", 0.0),
        ("std_error", math.sqrt(pooled * (1.0 - pooled) * inverse)), ("statistic", z),
        ("critical_low", critical_low), ("critical_high", critical_high), ("p_value", p_value),
        ("ci_low", ci_low), ("ci_high", ci_high), ("cohen_h", _cohen_h(p1, p2)),
    ))
    quantile = dist.norm_isf(alpha / 2.0)
    for g, s in zip(wanted, (a, b)):
        _write_proportion_group(out, g, s.rows, s.n, s.k, _wilson(s.k, s.n, quantile, 0.5 if p["correction"] else 0.0))
    if p["detail"] == "report":
        _write_curve(out, dist.norm_pdf, z, critical_low, critical_high)


# ── contingency tables ──────────────────────────────────────────────────────
class _Crosstab(NamedTuple):
    """A contingency table read from A7: the categories of x (rows) and y (columns) in text
    order, the counts, and the rows left out (no value in one of the two)."""

    rows: list[str]
    cols: list[str]
    counts: list[list[int]]
    missing: int


def _contingency(source, test: str, x: str, y: str) -> _Crosstab:
    if x == y:
        raise refusal(test, f"'x' and 'y' are both the column «{x}»: choose two different columns")
    limit = MAX_STAT_LEVELS * MAX_STAT_LEVELS + 2 * MAX_STAT_LEVELS + 2
    cells = source.crosstab(x, y, limit)
    counts: dict[tuple[str, str], int] = {}
    missing = 0
    for c in cells:
        if not (is_value(c.x) and is_value(c.y)):
            missing += c.rows
            continue
        key = (label_of(c.x), label_of(c.y))  # −0.0 and 0.0 one category, on every engine
        counts[key] = counts.get(key, 0) + c.rows
    rows = sorted({a for a, _ in counts})
    cols = sorted({b for _, b in counts})
    # at most 50 categories each: with as many, the pairs (NULLs too) are one fewer than the limit
    for column, found in ((x, rows), (y, cols)):
        if len(found) > MAX_STAT_LEVELS or (len(cells) >= limit and len(found) == max(len(rows), len(cols))):
            raise refusal(test, f"the column «{column}» has more than {MAX_STAT_LEVELS} categories: a contingency table takes "
                                f"{MAX_STAT_LEVELS} a column at most (is it the right column?)")
    if not counts:
        raise refusal(test, f"no row has a value in both «{x}» and «{y}» (a NULL or a NaN is left out)")
    for column, found in ((x, rows), (y, cols)):
        if len(found) < 2:
            raise refusal(test, f"the column «{column}» has 1 category ({found[0]}) on the rows used: "
                                "the table needs two at least in each column")
    return _Crosstab(rows, cols, [[counts.get((a, b), 0) for b in cols] for a in rows], missing)


def _chi_square(counts: list[list[int]], correction: bool) -> tuple[float, float, float, list[list[float]], list[int], list[int], int]:
    """(statistic, the correction subtracted, statistic without it, expected counts, row totals,
    column totals, N) of a table: Σ (|O − E| − Y)² / E, Y = min(0.5, |O − E|) for a 2 × 2 table
    with the correction (R's `chisq.test`), else 0."""
    row_totals = [sum(r) for r in counts]
    col_totals = [sum(c) for c in zip(*counts)]
    total = sum(row_totals)
    expected = [[r * c / total for c in col_totals] for r in row_totals]
    pairs = [(o, e) for orow, erow in zip(counts, expected) for o, e in zip(orow, erow)]
    yates = min(0.5, min(abs(o - e) for o, e in pairs)) if correction and len(counts) == 2 and len(counts[0]) == 2 else 0.0
    plain = math.fsum((o - e) ** 2 / e for o, e in pairs)
    statistic = math.fsum((abs(o - e) - yates) ** 2 / e for o, e in pairs) if yates else plain
    return statistic, yates, plain, expected, row_totals, col_totals, total


def _sparse_note(expected: list[float]) -> list[str]:
    small = sum(1 for e in expected if e < 5.0)
    if small > 0.2 * len(expected) or min(expected) < 1.0:
        return [f"{small} of the {len(expected)} expected counts are below 5{' (one below 1)' if min(expected) < 1.0 else ''}: "
                "the chi-square distribution is a poor approximation there. Merge rare categories"
                + (", or use Fisher's exact test (2 × 2)." if len(expected) == 4 else ".")]
    return []


def _write_margins(out: Rows, table: _Crosstab, row_totals: list[int], col_totals: list[int]) -> None:
    """The margins of a contingency table, as groups: each category of x with its row total and
    share, each of y with its column total and share."""
    total = sum(row_totals)
    for label, n in zip(table.rows, row_totals):
        out.number("group", "row_total", n, level=label)
        out.number("group", "row_share", n / total, level=label)
    for label, n in zip(table.cols, col_totals):
        out.number("group", "col_total", n, level=label)
        out.number("group", "col_share", n / total, level=label)


def _chi2_independence(source, p: dict[str, Any], out: Rows, engine_name: str) -> None:
    """Pearson's chi-square test of independence of two categorical columns, with Yates'
    correction on a 2 × 2 table; its post-hoc: the cells (adjusted residuals) or the pairs of
    categories of x (a chi-square of each 2 × c table). H0: x and y are independent."""
    test = "chi2_independence"
    x, y, alpha, posthoc = p["x"], p["y"], p["alpha"], p["posthoc"]
    table = _contingency(source, test, x, y)
    r, c = len(table.rows), len(table.cols)
    if posthoc == "pairs" and r > MAX_POSTHOC_GROUPS:
        raise refusal(test, f"the comparisons of pairs ('posthoc') take {MAX_POSTHOC_GROUPS} categories of «{x}» at most, and it has "
                            f"{r}: filter the categories first, or set 'posthoc' to cells or none")
    statistic, yates, plain, expected, row_totals, col_totals, total = _chi_square(table.counts, p["correction"])
    df = float((r - 1) * (c - 1))
    p_value, critical_high = dist.chi2_sf(statistic, df), dist.chi2_isf(alpha, df)
    notes = _sparse_note([e for row in expected for e in row])
    cramers_v = math.sqrt(plain / (total * min(r - 1, c - 1)))
    adjusted = [[(o - e) / math.sqrt(e * (1.0 - rt / total) * (1.0 - ct / total))
                 for o, e, ct in zip(orow, erow, col_totals)]
                for orow, erow, rt in zip(table.counts, expected, row_totals)]
    pairs_texts, extra, cell_p, pair_rows = None, (), None, []
    adjust = p["p_adjust"]
    if posthoc == "cells":
        raw = [min(1.0, 2.0 * dist.norm_sf(abs(z))) for row in adjusted for z in row]
        cell_p = list(zip(raw, p_adjusted(raw, adjust)))
        extra = (("n_cells", r * c),)
        pairs_texts = (posthoc, adjust, "z = (O − E) / √(E·(1 − R/N)·(1 − C/N)), p = 2·P(Z ≥ |z|), " + _ADJUST_TEXT[adjust].format(m="n_cells"))
    elif posthoc == "pairs":
        for i in range(r):
            for j in range(i + 1, r):
                sub = [table.counts[i], table.counts[j]]
                keep = [k for k in range(c) if sub[0][k] + sub[1][k] > 0]
                sub = [[row[k] for k in keep] for row in sub]
                if len(keep) < 2:  # the two categories have their rows in one column alone: nothing tells them apart
                    stat_ij, df_ij, p_ij = 0.0, 0.0, 1.0
                else:
                    stat_ij = _chi_square(sub, p["correction"])[0]
                    df_ij = float(len(keep) - 1)
                    p_ij = dist.chi2_sf(stat_ij, df_ij)
                pair_rows.append([table.rows[i], table.rows[j], None, None, stat_ij, df_ij, None, p_ij, None, None, None])
        for row, adj in zip(pair_rows, p_adjusted([row[7] for row in pair_rows], adjust)):
            row[8] = adj
        extra = (("n_pairs", len(pair_rows)),)
        pairs_texts = (posthoc, adjust, "χ²_ij = ∑ (|O − E| − Y)² / E over the rows i, j of x and the columns where O > 0, "
                                        "df = c − 1, p = P(χ²(df) ≥ χ²_ij), "
                                        + _ADJUST_TEXT[adjust].format(m="n_pairs"))
        if p_value >= alpha:
            notes.append(f"The chi-square test does not reject H0 at α = {_num(alpha)}: the comparisons are shown all the same, "
                         "and a difference they find is to be read with care.")
    formula = ("χ² = ∑ (|O − E| − Y)² / E, E = R·C / N, Y = " + ("min(0.5, |O − E|)" if yates or (p["correction"] and r == c == 2) else "0")
               + ", df = (r − 1)·(c − 1); V = √(χ²₀ / (N·min(r − 1, c − 1))), χ²₀ = ∑ (O − E)² / E")
    _write_head(out, test, "yates" if p["correction"] and r == c == 2 else "pearson", p,
                f"P({x}, {y}) = P({x})·P({y})", f"P({x}, {y}) ≠ P({x})·P({y})", formula, f"chi-square({df:.6g})",
                p_value, engine_name, notes, pairs_texts)
    phi = ()
    if r == c == 2:
        (a_, b_), (c_, d_) = table.counts
        phi = (("phi", (a_ * d_ - b_ * c_) / math.sqrt(row_totals[0] * row_totals[1] * col_totals[0] * col_totals[1])),)
    _write_numbers(out, (
        ("alpha", alpha), ("conf_level", 1.0 - alpha), ("n", total), ("n_missing", table.missing), ("rows", r), ("cols", c),
        ("yates", yates), ("chi_square_plain", plain), ("statistic", statistic), ("df", df), ("critical_high", critical_high),
        ("p_value", p_value), ("cramers_v", cramers_v), *phi, *extra,
    ))
    _write_margins(out, table, row_totals, col_totals)
    for i, a in enumerate(table.rows):
        for j, b in enumerate(table.cols):
            o, e = table.counts[i][j], expected[i][j]
            for stat, number in (("observed", o), ("expected", e), ("residual", (o - e) / math.sqrt(e)),
                                 ("adj_residual", adjusted[i][j]), ("contribution", (abs(o - e) - yates) ** 2 / e)):
                out.number("cell", stat, number, level=a, level_2=b)
            if cell_p is not None:
                raw_p, adj = cell_p[i * c + j]
                out.number("cell", "p_value", raw_p, level=a, level_2=b)
                out.number("cell", "p_adjusted", adj, level=a, level_2=b)
                out.text("cell", "significance", significance(adj) or "", level=a, level_2=b)
                out.text("cell", "decision", "reject_h0" if adj < alpha else "fail_to_reject_h0", level=a, level_2=b)
    for row in pair_rows:
        _write_pair(out, row, alpha)
    if p["detail"] == "report":
        _write_positive_curve(out, lambda v: dist.chi2_pdf(v, df), lambda q: dist.chi2_isf(q, df), statistic, None, critical_high)


_ADJUST_TEXT = {
    "holm": "p_adj = Holm's: the p sorted, p(1) ≤ ... ≤ p({m}), p_adj(r) = max over r' ≤ r of min(1, ({m} − r' + 1) · p(r'))",
    "bonferroni": "p_adj = min(1, {m} · p)",
    "sidak": "p_adj = 1 − (1 − p)^{m}",
    "none": "p_adj = p",
}


def _chi2_gof(source, p: dict[str, Any], out: Rows, engine_name: str) -> None:
    """The chi-square test of goodness of fit: do the categories of a column have the expected
    proportions (all equal, or given)? H0: P(value = i) = p_i for every category."""
    test = "chi2_gof"
    value, alpha = p["value"], p["alpha"]
    counts: dict[str, int] = {}
    missing = 0
    levels = source.levels(value, MAX_STAT_GROUPS + 2)
    for lv in levels:
        if not is_value(lv.value):
            missing += lv.rows
        else:
            counts[label_of(lv.value)] = counts.get(label_of(lv.value), 0) + lv.rows
    if len(counts) > MAX_STAT_GROUPS or len(levels) > MAX_STAT_GROUPS + 1:
        raise refusal(test, f"the column «{value}» has more than {MAX_STAT_GROUPS} categories: is it the right column?")
    labels = sorted(counts)
    if not labels:
        raise refusal(test, f"the column «{value}» has no value on the rows that reach the step (a NULL or a NaN is left out)")
    if len(labels) < 2:
        raise refusal(test, f"the column «{value}» has 1 category ({labels[0]}) on the rows used: the test needs two at least")
    given = p["expected"]
    if given is None:
        shares = {g: 1.0 / len(labels) for g in labels}
        variant = "equal"
    else:
        absent, unknown = [g for g in labels if g not in given], sorted(g for g in given if g not in counts)
        if absent or unknown:
            raise refusal(test, "'expected' must give a proportion to exactly the categories of «" + value + "» on the rows used"
                          + (f"; it lacks {_listed(absent)}" if absent else "")
                          + (f"; {_listed(unknown)} {'is' if len(unknown) == 1 else 'are'} not in the column" if unknown else ""))
        shares = {g: given[g] for g in labels}
        variant = "given"
    total = sum(counts.values())
    expected = {g: total * shares[g] for g in labels}
    statistic = math.fsum((counts[g] - expected[g]) ** 2 / expected[g] for g in labels)
    df = float(len(labels) - 1)
    p_value, critical_high = dist.chi2_sf(statistic, df), dist.chi2_isf(alpha, df)
    shown = labels if len(labels) <= 3 else [labels[0], labels[1], None, labels[-1]]
    h0 = ", ".join("..." if g is None else f"P({value} = {g}) = {_num(shares[g])}" for g in shown)
    _write_head(out, test, variant, p, h0, f"max |P({value} = i) − p_i| > 0",
                "χ² = ∑ (O − E)² / E, E = N·p_i, df = k − 1; w = √(χ² / N)", f"chi-square({df:.6g})", p_value, engine_name,
                _sparse_note(list(expected.values())))
    _write_numbers(out, (
        ("alpha", alpha), ("conf_level", 1.0 - alpha), ("n", total), ("n_missing", missing), ("k", len(labels)),
        ("statistic", statistic), ("df", df), ("critical_high", critical_high), ("p_value", p_value),
        ("cohen_w", math.sqrt(statistic / total)),
    ))
    for g in labels:
        o, e = counts[g], expected[g]
        for stat, number in (("observed", o), ("proportion", o / total), ("expected_share", shares[g]), ("expected", e),
                             ("residual", (o - e) / math.sqrt(e)), ("contribution", (o - e) ** 2 / e)):
            out.number("cell", stat, number, level=g)
    if p["detail"] == "report":
        _write_positive_curve(out, lambda v: dist.chi2_pdf(v, df), lambda q: dist.chi2_isf(q, df), statistic, None, critical_high)


def _fisher_exact(source, p: dict[str, Any], out: Rows, engine_name: str) -> None:
    """Fisher's exact test of a 2 × 2 table, with the conditional odds ratio and its interval
    (R's `fisher.test`). H0: odds ratio = 1."""
    test = "fisher_exact"
    x, y, alpha, alternative = p["x"], p["y"], p["alpha"], p["alternative"]
    table = _contingency(source, test, x, y)
    if len(table.rows) != 2 or len(table.cols) != 2:
        raise refusal(test, f"Fisher's exact test takes a 2 × 2 table, and «{x}» has {len(table.rows)} categories and «{y}» "
                            f"{len(table.cols)} on the rows used: use the chi-square test of independence")
    (a, b), (c, d) = table.counts
    support = exact.fisher_support(a, b, c, d)
    if support > EXACT_SUPPORT:
        raise refusal(test, f"the exact distribution of this table goes through {support:,} outcomes, more than {EXACT_SUPPORT:,}: "
                            "use the chi-square test of independence, which is as good on so many rows")
    f = exact.fisher_exact(a, b, c, d, alternative, alpha)
    row_totals, col_totals, total = [a + b, c + d], [a + c, b + d], a + b + c + d
    sample = a * d / (b * c) if b * c else None
    odds = f"OR({x}, {y})"
    tails = {"two_sided": "p = ∑ P(X = i), P(X = i) ≤ P(X = a)·(1 + 1e-7)", "less": "p = P(X ≤ a)", "greater": "p = P(X ≥ a)"}
    bounds = {"two_sided": "P(X ≥ a | OR_L) = α/2, P(X ≤ a | OR_U) = α/2", "less": "OR_L = 0, P(X ≤ a | OR_U) = α",
              "greater": "P(X ≥ a | OR_L) = α, OR_U = ∞"}
    formula = (f"{tails[alternative]}, P(X = i | OR) = C(C1, i)·C(C2, R1 − i)·OR^i / ∑ C(C1, j)·C(C2, R1 − j)·OR^j, P(X = i) = P(X = i | 1); OR: E(X | OR) = a; "
               f"CI: {bounds[alternative]}; sample OR = a·d / (b·c)")
    _write_head(out, test, "exact", p, f"{odds} = 1", f"{odds} {_RELATION[alternative]} 1", formula,
                f"hypergeometric({col_totals[0]}, {col_totals[1]}, {row_totals[0]})", f.p_value, engine_name, [])
    _write_numbers(out, (
        ("alpha", alpha), ("conf_level", 1.0 - alpha), ("n", total), ("n_missing", table.missing), ("support", f.support),
        ("sample_odds_ratio", sample), ("estimate", f.odds_ratio), ("null_value", 1.0), ("p_value", f.p_value),
        ("ci_low", f.ci_low), ("ci_high", f.ci_high),
    ))
    _write_margins(out, table, row_totals, col_totals)
    for i, ra in enumerate(table.rows):
        for j, cb in enumerate(table.cols):
            out.number("cell", "observed", table.counts[i][j], level=ra, level_2=cb)
            out.number("cell", "expected", row_totals[i] * col_totals[j] / total, level=ra, level_2=cb)


_TESTS: dict[str, Callable[..., None]] = {
    "t_one_sample": _t_one_sample,
    "t_two_sample": _t_two_sample,
    "t_paired": _t_paired,
    "anova": _anova,
    "var_f": _var_f,
    "levene": _levene,
    "pearson": _pearson,
    "prop_one_sample": _prop_one_sample,
    "prop_two_sample": _prop_two_sample,
    "chi2_independence": _chi2_independence,
    "chi2_gof": _chi2_gof,
    "fisher_exact": _fisher_exact,
}

# The tests that write the rows of their report's charts. Until a test is here it refuses
# `detail = report` with a sentence: it is never answered with the results alone.
_WITH_REPORT_ROWS = frozenset({"t_one_sample", "t_two_sample", "t_paired", "anova", "var_f", "levene", "pearson",
                               "prop_one_sample", "prop_two_sample", "chi2_independence", "chi2_gof", "fisher_exact"})


def run_hypothesis_test(source, test: str, params: dict[str, Any] | None, engine_name: str) -> list[tuple]:
    """The rows of the test `test` on `source`, as `output.OUTPUT_COLUMNS` orders them."""
    p = check_params(test, params, source.columns())
    if p.get("detail") == "report" and test not in _WITH_REPORT_ROWS:
        raise refusal(test, "the rows of the report's charts (detail = 'report') are not available yet: use detail = 'results'")
    out = Rows()
    _TESTS[test](source, p, out, engine_name)
    return out.rows
