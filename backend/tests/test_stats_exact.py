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
from app.engine.stats.catalog import EXACT_NETWORK


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
    p_value, nodes = exact.fisher_rxc(table, EXACT_NETWORK)
    assert rel(p_value, want) < 1e-11 and nodes > 0
    transposed = [list(c) for c in zip(*table)]
    assert rel(exact.fisher_rxc(transposed, EXACT_NETWORK)[0], want) < 1e-11


def test_fisher_rxc_has_a_ceiling():
    with pytest.raises(exact.TooMuchWork):
        exact.fisher_rxc([[20, 12, 6, 4], [9, 15, 12, 6], [5, 8, 16, 14]], 10_000)


def test_count_vectors_counts_what_is_gone_through():
    for total, caps in ((7, (3, 5, 2)), (0, (4, 4)), (12, (12, 1, 6, 3)), (5, (1, 1)), (18, (18,) * 5)):
        assert exact._count_vectors(total, caps) == sum(1 for _ in exact._column_vectors(total, caps))


def _diagonal(k: int) -> list[list[int]]:
    """A k × k table with 2 in every cell and 4 on the diagonal: small counts, many cells."""
    return [[4 if i == j else 2 for j in range(k)] for i in range(k)]


def _sparse(k: int) -> list[list[int]]:
    """A k × k table of a few ones a row, and one more on the diagonal."""
    return [[(1 if (i + j) % 7 == 0 else 0) + (1 if i == j else 0) for j in range(k)] for i in range(k)]


_REFUSE = """
import re, sys, time
from app.engine.stats import exact
from app.engine.stats.catalog import EXACT_NETWORK
table = eval(sys.argv[1])


def hwm():  # the peak resident memory, MB (VmHWM: a forked process starts with its parent's, reset below)
    return int(re.search(r"VmHWM:\\s+(\\d+)", open("/proc/self/status").read()).group(1)) / 1024


with open("/proc/self/clear_refs", "w") as f:
    f.write("5")  # the peak back to what the process holds now
start = time.perf_counter()
try:
    exact.fisher_rxc(table, EXACT_NETWORK)
    print("computed")
except exact.TooMuchWork as e:
    print("refused", e.args[0] > EXACT_NETWORK, time.perf_counter() - start, hwm())
"""


@pytest.mark.parametrize("table, most_seconds", [
    (_diagonal(7), 15.0),  # 112 rows: 121 million steps (66 s) to the end; refused when the 4 million run out
    (_diagonal(8), 1.0),   # 144 rows: its own path costs 4.3 million, refused before the count
    (_diagonal(10), 1.0),
    (_sparse(50), 1.0),    # the largest table the node takes
])
def test_fisher_rxc_refuses_large_tables_in_seconds(table, most_seconds):
    """Every cell of a way of filling a column is a step, counted as it is gone through, and so
    is every path extended, node remembered and line bounded: the count stops at the ceiling
    whatever the table, and a table whose own path is past it is refused before the count.
    Measured on the development machine in the backend's image (2026-11-20, in a process of
    its own: the peak memory is the whole process's, 139 MB of it Python and the engine's
    modules): the 7 × 7 table refused after 2.4 s at a peak of 157 MB, the others at once at
    138 MB. Before, the 7 × 7 table
    computed in 74 s through 57 million ways of filling a column, the 8 × 8 one ran past 240 s
    and 434 MB with no refusal. The bounds here are guards against a count that does not stop
    (a busy machine is slower), not benchmarks."""
    import subprocess
    import sys

    got = subprocess.run([sys.executable, "-c", _REFUSE, repr(table)], capture_output=True, text=True, check=True,
                         cwd=str(__import__("pathlib").Path(__file__).resolve().parents[1])).stdout.split()
    assert got[:2] == ["refused", "True"], got
    seconds, megabytes = float(got[2]), float(got[3])
    assert seconds < most_seconds and megabytes < 400.0, (seconds, megabytes)


def test_fisher_rxc_is_not_refused_by_its_size_alone():
    """A 50 × 50 table of zeros and ones: no table with its margins is more likely (Π 1/x! is 1
    at most), the root of the network closes at once. Only the steps refuse, never the shape."""
    table = [[1 if (i + j) % 7 == 0 else 0 for j in range(50)] for i in range(50)]
    assert exact.fisher_rxc(table, EXACT_NETWORK)[0] == 1.0


# ── ranks: Mann–Whitney's U, the signed rank (lot 4) ─────────────────────────
def test_mann_whitney_exact_against_r():
    """R 4.6.1's `pwilcox` (and `wilcox.test` on the spec's samples), up to the largest samples
    the exact distribution takes (49 and 49): within 1e-13. R adds doubles; here they are whole
    numbers divided once."""
    assert exact.mann_whitney_exact(2, 8, 7, "two_sided") == pytest.approx(0.0012432012432012432, rel=1e-14)
    assert exact.mann_whitney_exact(2, 8, 7, "less") == pytest.approx(0.0006216006216006216, rel=1e-14)
    assert exact.mann_whitney_exact(2, 8, 7, "greater") == pytest.approx(0.99968919968919967, rel=1e-14)
    for got, want in ((exact.mann_whitney_exact(1000, 49, 49, "less"), 0.077968162023427601),
                      (exact.mann_whitney_exact(1201, 49, 49, "greater"), 0.50000000000000222),
                      (exact.mann_whitney_exact(300, 49, 49, "less"), 2.611566225232158e-12),
                      (exact.mann_whitney_exact(10, 3, 40, "less"), 0.0054290576128352653)):
        assert rel(got, want) < 1e-13, (got, want)
    counts = exact.mann_whitney_counts(8, 7)
    assert sum(counts) == math.comb(15, 8) and counts == counts[::-1] and len(counts) == 57
    assert exact.mann_whitney_exact(28, 8, 7, "two_sided") == 1.0  # the middle: never above 1


def test_signed_rank_exact_against_r():
    """R 4.6.1's `psignrank`, up to 49 differences: within 1e-13."""
    assert exact.signed_rank_exact(40, 9, "two_sided") == 0.0390625 and exact.signed_rank_exact(40, 9, "greater") == 0.01953125
    for got, want in ((exact.signed_rank_exact(400, 49, "less"), 0.017063075357777183),
                      (exact.signed_rank_exact(801, 49, "greater"), 0.030532955934383214),
                      (exact.signed_rank_exact(100, 49, "less"), 8.9142986325896444e-09),
                      (exact.signed_rank_exact(3, 12, "less"), 0.0012207031250000009)):
        assert rel(got, want) < 1e-13, (got, want)
    assert sum(exact.signed_rank_counts(20)) == 2 ** 20


def test_ks_exact_against_r():
    """R 4.6.1's `psmirnov` (exact, no ties) and `ks.test` on the spec's samples: the paths that
    stay inside the band, counted in whole numbers."""
    assert exact.ks_exact(49, 8, 7, "two_sided") == pytest.approx(0.0024864024864024864, rel=1e-14)
    assert exact.ks_exact(49, 8, 7, "greater") == pytest.approx(0.0012432012432012432, rel=1e-14)
    assert exact.ks_exact(0, 8, 7, "less") == 1.0  # D− = 0: every path reaches it
    assert exact.ks_exact(24, 8, 6, "two_sided") == pytest.approx(0.30169830169830159, rel=1e-14)
    for got, want in ((exact.ks_exact(3000, 99, 101, "two_sided"), 0.00017003811634117797),
                      (exact.ks_exact(360, 40, 60, "two_sided"), 0.61768965052950953),
                      (exact.ks_exact(150, 20, 30, "greater"), 0.20103771118846292)):
        assert rel(got, want) < 1e-13, (got, want)
    # the two directions mirror each other when the samples swap
    assert exact.ks_exact(150, 20, 30, "greater") == exact.ks_exact(150, 30, 20, "less")


def test_ks_asymptotic_and_kolmogorov_against_mpmath():
    """Kolmogorov's limit, both series summed to the end: within 1e-14 of mpmath at 40 digits
    (SciPy's `kstwobign.sf` agrees; its `pdf` is 5e-10 off near λ = 1, ours is not). R prints
    0.358087601395282 for the spec's (A, C): its series stops at 1e-6."""
    for lam, sf, pdf in ((0.3, 0.99999069419866543338, 0.00081939341969312957099), (0.9, 0.39273070794065434327, 1.3807270542377327318),
                         (0.99, 0.28087383922554892176, 1.102915687427821849), (1.0, 0.2699996716773545212, 1.0719485583569417625),
                         (1.6907, 0.0065795703308206933275, None), (5.0, 3.857499695927835566e-22, None)):
        assert rel(dist.kolmogorov_sf(lam), sf) < 1e-14, lam
        if pdf is not None:
            assert rel(dist.kolmogorov_pdf(lam), pdf) < 1e-13, lam
    assert rel(dist.kolmogorov_isf(0.05), 1.3580986393225507) < 1e-12 and rel(dist.kolmogorov_isf(0.01), 1.6276236115189504) < 1e-12
    assert dist.kolmogorov_isf(0.05, two_sided=False) == pytest.approx(math.sqrt(-0.5 * math.log(0.05)), rel=1e-15)
    assert exact.ks_asymptotic(0.5, 8, 6, "two_sided") == pytest.approx(0.358081192617015, rel=1e-13)
    assert exact.ks_asymptotic(0.875, 8, 7, "two_sided") == pytest.approx(0.0065813227912815799, rel=1e-13)
    assert exact.ks_asymptotic(0.875, 8, 7, "greater") == pytest.approx(0.0032906615128961425, rel=1e-14)
    assert exact.ks_asymptotic(0.0, 8, 7, "less") == 1.0


def test_the_exact_distributions_are_quick_at_their_ceilings():
    """At their ceilings (49 and 49 values; 49 differences; n1·n2 = 9,999), on any table: the
    support does not depend on the rows. Measured on 2026-11-19 (Python 3.12, one core of an
    old CPU): Mann–Whitney 0.02 s, the signed rank 0.005 s, Kolmogorov–Smirnov 0.003 s (99 × 101)
    and 0.005 s (1 × 9,999)."""
    for run in (lambda: exact.mann_whitney_exact(1200, 49, 49, "two_sided"), lambda: exact.signed_rank_exact(612, 49, "two_sided"),
                lambda: exact.ks_exact(5000, 99, 101, "two_sided"), lambda: exact.ks_exact(5000, 1, 9999, "two_sided")):
        start = time.perf_counter()
        run()
        assert time.perf_counter() - start < 2.0
