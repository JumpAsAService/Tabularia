"""The exact distributions of the tests on counts (app/engine/stats/exact.py), and the beta's
quantiles under Clopper–Pearson's interval (dist.beta_ppf, beta_isf): no engine, only the
scalar step.

References: R 4.6.1 (`binom.test`, `fisher.test`), the spec's «Reference values», and mpmath
at 40 digits and SciPy 1.18.1 outside Tabularia (none of them is a dependency). Measured on
2026-11-19: on 300 binomial cases with n up to 999,999 the p-values within 1.5e-13 of R's
`pbinom` and `binom.test`, Clopper–Pearson's bounds within 6.1e-13 of R's `qbeta`; on 48 tables
Fisher's p-values within 2.5e-14 of R's, the odds ratio and its bounds within 1.5e-14 of
mpmath's (and of SciPy's `odds_ratio`). R finds the odds ratio and its interval by `uniroot`,
whose tolerance leaves them up to 1.4e-2 off on some tables: R's within 1e-4 only on the
spec's table, and nowhere else compared with R.
"""
from __future__ import annotations

import math
import time

import pytest

from app.engine.stats import dist, exact


def rel(got: float, want: float) -> float:
    return abs(got - want) / abs(want)


# ── the binomial ─────────────────────────────────────────────────────────────
def test_binomial_test_against_r():
    assert rel(exact.binomial_test(37, 120, 0.25, "two_sided"), 0.140826101479329) < 1e-12
    assert rel(exact.binomial_test(37, 120, 0.25, "greater"), 0.0873786876294322) < 1e-12
    assert rel(exact.binomial_test(37, 120, 0.25, "less"), 1 - exact.binom_sf(38, 120, 0.25)) < 1e-12
    # k = n·p: the most likely outcome, nothing is less likely than it on both sides
    assert exact.binomial_test(30, 120, 0.25, "two_sided") == 1.0
    # an outcome as likely as k on the other side counts (R's slack of 1e-7): 400,000 and 399,999 of 999,999 at 0.4
    assert exact.binomial_test(400_000, 999_999, 0.4, "two_sided") == 1.0
    # the two tails add up to 1 + P(X = k)
    for k, n, p in ((0, 10, 0.3), (10, 10, 0.3), (7, 50, 0.01), (500, 1000, 0.5)):
        both = exact.binom_cdf(k, n, p) + exact.binom_sf(k, n, p) - math.exp(exact.binom_logpmf(k, n, p))
        assert abs(both - 1.0) < 1e-12, (k, n, p)
        assert 0.0 < exact.binomial_test(k, n, p, "two_sided") <= 1.0


def test_clopper_pearson_against_r():
    low, high = exact.clopper_pearson(37, 120, 0.05, "two_sided")
    assert rel(low, 0.227255119633832) < 1e-12 and rel(high, 0.399136764531952) < 1e-12
    low, high = exact.clopper_pearson(37, 120, 0.05, "greater")
    assert rel(low, 0.2390137473904261) < 1e-12 and high == 1.0
    assert exact.clopper_pearson(0, 20, 0.05, "two_sided")[0] == 0.0 and exact.clopper_pearson(20, 20, 0.05, "two_sided")[1] == 1.0
    # the bounds are where the tails are α/2: one success in a million rows
    low, high = exact.clopper_pearson(1, 999_999, 0.05, "two_sided")
    assert rel(exact.binom_sf(1, 999_999, low), 0.025) < 1e-9 and rel(exact.binom_cdf(1, 999_999, high), 0.025) < 1e-9


def test_beta_quantiles_by_their_tails():
    for q, a, b in ((0.025, 37.0, 84.0), (1e-10, 3.0, 7.0), (0.5, 0.5, 0.5), (0.975, 500.0, 500.0), (0.025, 1.0, 999_999.0)):
        x = dist.beta_ppf(q, a, b)
        assert rel(dist.betainc(a, b, x, 1.0 - x), q) < 1e-12, (q, a, b)
        y = dist.beta_isf(q, a, b)
        assert rel(dist.betainc(b, a, 1.0 - y, y), q) < 1e-9, (q, a, b)
    assert dist.beta_ppf(0.0, 2.0, 3.0) == 0.0 and dist.beta_ppf(1.0, 2.0, 3.0) == 1.0 and math.isnan(dist.beta_ppf(1.5, 2.0, 3.0))


# ── Fisher ───────────────────────────────────────────────────────────────────
def test_fisher_exact_against_r():
    f = exact.fisher_exact(12, 5, 7, 16, "two_sided", 0.05)
    assert rel(f.p_value, 0.023836895850244) < 1e-12 and f.support == 18
    assert rel(f.odds_ratio, 5.22965523049642) < 1e-4 and rel(f.ci_low, 1.16935473342543) < 1e-4
    assert rel(f.ci_high, 27.30369774777703) < 1e-4
    g = exact.fisher_exact(12, 5, 7, 16, "greater", 0.05)
    assert rel(g.p_value, 0.0135692310775737) < 1e-12 and rel(g.ci_low, 1.42562760456438) < 1e-4 and g.ci_high == math.inf
    l_ = exact.fisher_exact(12, 5, 7, 16, "less", 0.05)
    assert rel(l_.p_value, 0.997986250989588) < 1e-12 and l_.ci_low == 0.0


def test_fisher_exact_its_ends_and_its_symmetries():
    # the count at an end of its support: the estimate is 0 or +∞, and so is a bound
    f = exact.fisher_exact(0, 5, 5, 3, "two_sided", 0.05)
    assert f.odds_ratio == 0.0 and f.ci_low == 0.0 and math.isfinite(f.ci_high)
    f = exact.fisher_exact(5, 0, 3, 5, "two_sided", 0.05)
    assert f.odds_ratio == math.inf and f.ci_high == math.inf
    # the rows the other way round: the odds ratio and its bounds inverted, the same p-value
    a, b = exact.fisher_exact(12, 5, 7, 16, "two_sided", 0.05), exact.fisher_exact(7, 16, 12, 5, "two_sided", 0.05)
    assert rel(a.p_value, b.p_value) < 1e-12 and rel(a.odds_ratio, 1 / b.odds_ratio) < 1e-12
    assert rel(a.ci_low, 1 / b.ci_high) < 1e-12 and rel(a.ci_high, 1 / b.ci_low) < 1e-12
    # transposed: the same table
    c = exact.fisher_exact(12, 7, 5, 16, "two_sided", 0.05)
    assert rel(a.p_value, c.p_value) < 1e-12 and rel(a.odds_ratio, c.odds_ratio) < 1e-12


@pytest.mark.parametrize("table", [(500_000, 499_999, 499_999, 500_001), (250_300, 249_700, 249_700, 250_300)])
def test_a_table_at_the_limit_of_the_support_is_computed(table):
    """A million outcomes: the sums walk out from the mode until what is left no longer counts
    (measured on this machine: 0.4 to 0.6 s for a test with its estimate and interval)."""
    start = time.perf_counter()
    f = exact.fisher_exact(*table, "two_sided", 0.05)
    assert exact.fisher_support(*table) >= 500_001
    assert 0.0 < f.p_value <= 1.0 and f.ci_low < f.odds_ratio < f.ci_high
    assert time.perf_counter() - start < 30.0  # a guard against a walk over every outcome, not a benchmark
    # far in the tail, the p-value is below a double's reach: 0, never a wrong small number
    assert exact.fisher_exact(500_000, 0, 0, 499_999, "two_sided", 0.05).p_value == 0.0


# ── Fisher of an r × c table (Freeman–Halton) ────────────────────────────────
@pytest.mark.parametrize("table, want", [
    ([[30, 20, 10], [15, 25, 20]], 0.012867278704384083),
    ([[8, 3, 1], [2, 9, 3], [1, 2, 7]], 0.0018916138067752856),
    ([[10, 6, 2, 1], [4, 8, 7, 2], [2, 3, 9, 6]], 0.0080095433958731511),
    ([[20, 12, 9], [15, 18, 11], [10, 14, 22]], 0.029197683863874373),
    ([[30, 25, 20], [28, 35, 22], [18, 24, 40]], 0.0086216993802620788),
    ([[12, 5], [7, 16]], 0.023836895850244),  # a 2 × 2 table: the same p-value as the other algorithm
])
def test_fisher_rxc_against_r(table, want):
    """R 4.6.1's fisher.test; a p-value is the sum of the tables no more likely than the one
    observed, each of them a product of factorials: within 1e-11."""
    p_value, nodes = exact.fisher_rxc(table, 2_000_000)
    assert rel(p_value, want) < 1e-11 and nodes > 0
    transposed = [list(c) for c in zip(*table)]
    assert rel(exact.fisher_rxc(transposed, 2_000_000)[0], want) < 1e-11


def test_fisher_rxc_has_a_ceiling():
    with pytest.raises(exact.TooMuchWork):
        exact.fisher_rxc([[20, 12, 6, 4], [9, 15, 12, 6], [5, 8, 16, 14]], 10_000)
