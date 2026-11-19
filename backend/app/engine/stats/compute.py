"""From the engine's aggregates to a test's results: the statistic, its degrees of freedom,
the p-value, the critical values, the intervals, the effect sizes — arithmetic on a handful
of numbers, the same code for every engine. It reads the data through a source only
(`SqlStatSource`, `PolarsStatSource`): a row per group at most, never a row of the data.

A test is a function `(source, p, out)` named in `_TESTS`: `p` its checked parameters
(`catalog.check_params`), `out` the rows it writes (`output.Rows`). Sections and the order of
the rows inside them are the output's contract: texts first, then numbers.

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
from app.engine.stats.aggregates import Moments
from app.engine.stats.catalog import MAX_STAT_GROUPS, check_params, label_of, refusal
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


def _write_group(out: Rows, label: str, m: Moments, alpha: float) -> None:
    """A numeric group's rows: its numbers, and the two-sided interval of its mean."""
    sd = math.sqrt(m.var)
    se = sd / math.sqrt(m.n)
    q = dist.t_isf(alpha / 2.0, m.n - 1)
    for stat, value in (("n", m.n), ("mean", m.mean), ("var", m.var), ("sd", sd), ("se", se),
                        ("ci_low", m.mean - q * se), ("ci_high", m.mean + q * se), ("min", m.min), ("max", m.max)):
        out.number("group", stat, value, level=label)


# ── two groups of a numeric column ──────────────────────────────────────────
def _two_groups(source, test: str, p: dict[str, Any]) -> tuple[list[str], list[Moments], int]:
    """The two groups a two-sample test compares, the first minus the second: their labels,
    their moments, and the rows left out (no group, or no value in one of the two)."""
    value, group = p["value"], p["group"]
    levels = [lv for lv in source.levels(group, MAX_STAT_GROUPS + 2) if lv.value is not None]
    if len(levels) > MAX_STAT_GROUPS:
        raise refusal(test, f"the column «{group}» has more than {MAX_STAT_GROUPS} groups: is it the right column?")
    labels = sorted(label_of(lv.value) for lv in levels)
    if not labels:
        raise refusal(test, f"the column «{group}» has no group: the step has no rows, or every row is NULL there")
    wanted = p["groups"]
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
    by_label: dict[str, Moments] = {}
    without_group = 0
    for m in source.moments(value, group):
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
    return list(wanted), pair, missing


def _t_two_sample(source, p: dict[str, Any], out: Rows, engine_name: str) -> None:
    """Student's and Welch's t for two independent samples. H0: mean₁ − mean₂ = mu."""
    test = "t_two_sample"
    (g1, g2), (a, b), missing = _two_groups(source, test, p)
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
    _write_group(out, g1, a, alpha)
    _write_group(out, g2, b, alpha)


_TESTS: dict[str, Callable[..., None]] = {
    "t_two_sample": _t_two_sample,
}


def run_hypothesis_test(source, test: str, params: dict[str, Any] | None, engine_name: str) -> list[tuple]:
    """The rows of the test `test` on `source`, as `output.OUTPUT_COLUMNS` orders them."""
    p = check_params(test, params, source.columns())
    if p.get("detail") == "report":
        raise refusal(test, "the rows of the report's charts (detail = 'report') are not available yet: use detail = 'results'")
    out = Rows()
    _TESTS[test](source, p, out, engine_name)
    return out.rows
