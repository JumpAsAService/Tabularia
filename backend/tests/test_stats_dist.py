"""The distributions of the hypothesis tests (app/engine/stats/dist.py), without an engine.

The expected values are not Tabularia's: the quantiles and tails are R 4.6.1's (`qt`, `pt`,
`qnorm`), the accuracy table was computed with 60-digit arithmetic (mpmath: the series of the
incomplete beta in x far in the tail, in 1 − x elsewhere, summed with as many more digits as
its alternating terms cancel). Neither is a dependency of Tabularia.
"""
from __future__ import annotations

import math

import pytest

from app.engine.stats import dist


def rel(got: float, want: float) -> float:
    return abs(got - want) / abs(want)


# ── quantiles ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("p, df, want", [
    (0.975, 7, 2.36462425159278),
    (0.975, 13, 2.16036865646279),
    (0.975, 1e9, 1.9599639869123249),
    (0.95, 12.727892707548934, 1.7738331494085396),
    (0.995, 1, 63.656741162871524),
])
def test_t_quantiles(p, df, want):
    assert rel(dist.t_ppf(p, df), want) < 1e-12


def test_normal_quantile_and_tails():
    assert rel(dist.norm_ppf(0.975), 1.95996398454005) < 1e-12
    assert dist.norm_ppf(0.5) == 0.0
    assert rel(dist.norm_sf(1.959963984540054), 0.025) < 1e-12
    assert rel(dist.norm_cdf(-12.0), 1.776482112077679e-33) < 1e-12
    assert dist.norm_sf(-3.0) == pytest.approx(1.0 - dist.norm_sf(3.0), rel=1e-15)


def test_a_quantile_is_symmetric_and_asked_by_its_tail():
    assert dist.t_ppf(0.025, 7) == -dist.t_ppf(0.975, 7)
    assert dist.t_ppf(0.5, 3) == 0.0
    assert dist.t_isf(0.025, 7) == pytest.approx(dist.t_ppf(0.975, 7), rel=1e-14)
    # a small alpha keeps its digits: 1 − 1e-18 would be 1 in a double
    q = dist.t_isf(1e-18, 30)
    assert rel(dist.t_sf(q, 30), 1e-18) < 1e-12
    assert dist.t_isf(0.0, 5) == math.inf and dist.t_isf(1.0, 5) == -math.inf
    assert math.isnan(dist.t_isf(1.5, 5))


@pytest.mark.parametrize("df", [0.7, 1, 4, 12.3, 250, 1e5, 4e6, 1e9])
@pytest.mark.parametrize("q", [0.4, 0.05, 0.001, 1e-12, 1e-100])
def test_the_quantile_inverts_the_tail(df, q):
    assert rel(dist.t_sf(dist.t_isf(q, df), df), q) < 1e-10


# ── tails ────────────────────────────────────────────────────────────────────
def test_t_tails():
    assert rel(dist.t_cdf(-4.5, 12.3), 0.000341926886228589) < 1e-12
    assert rel(dist.t_sf(40, 5), 9.20598108588648e-08) < 1e-12
    assert dist.t_sf(0.0, 9) == 0.5
    assert dist.t_sf(-2.0, 9) == pytest.approx(1.0 - dist.t_sf(2.0, 9), rel=1e-15)
    assert dist.t_sf(math.inf, 9) == 0.0 and dist.t_sf(-math.inf, 9) == 1.0
    assert dist.t_sf(1e200, 9) == 0.0  # t² overflows: no error, no NaN
    assert math.isnan(dist.t_sf(math.nan, 9))
    # one degree of freedom is the Cauchy distribution: P(T ≥ 1) = 1/4
    assert dist.t_sf(1.0, 1) == pytest.approx(0.25, rel=1e-15)


def test_large_degrees_of_freedom():
    """A billion rows are a billion degrees of freedom: a textbook incomplete beta is wrong
    at the eighth digit there."""
    assert rel(dist.t_cdf(-2.5, 1e9), 0.0062096654052012585) < 1e-12
    assert rel(dist.t_cdf(-12, 1e9), 1.7764914488705686e-33) < 1e-12
    assert rel(dist.t_cdf(-2.5, 1e6), 0.0062097447510816167) < 1e-12
    # and it tends to the normal
    assert rel(dist.t_sf(2.5, 1e15), dist.norm_sf(2.5)) < 1e-12


# (degrees of freedom, t, P(T ≥ t) to 25 digits)
ACCURACY = [
    (1, 1, 0.25),
    (1, 40, 0.007956089912025813318262717),
    (5, 40, 9.205981085886477177645303e-8),
    (2, 0.001, 0.4996464466977950407331417),
    (12.3, 4.5, 0.0003419268862285892352390044),
    (30, 2, 0.02731252248149155196025902),
    (100, 9, 7.680385257375206850072579e-15),
    (1000.0, 37, 8.650394404956530658814717e-190),
    (10000.0, 2.5, 0.006217609775291848433046724),
    (99999, 6, 9.899647616217659628042184e-10),
    (100000.0, 2.5, 0.00621045959627952269102333),
    (300000.0, 12, 1.807868799334052380803359e-33),
    (1000000.0, 2.5, 0.006209744751081623103865219),
    (2900000.0, 4.5, 0.000003397804889205360480930651),
    (5900000.0, 2, 0.02275015482570923258830575),
    (6100000.0, 2, 0.02275015407562622252821607),
    (10000000.0, 9, 1.128778082695371508094177e-19),
    (100000000.0, 2.5, 0.006209666120027270669488901),
    (1000000000.0, 2.5, 0.006209665405201246972337408),
    (1000000000.0, 12, 1.776491448870571793832872e-33),
    (10000000000.0, 30, 4.90681350979029003253732e-198),
    (10000000000.0, 0.1, 0.4601721627239733215134329),
]


@pytest.mark.parametrize("df, t, want", ACCURACY)
def test_accuracy_contract(df, t, want):
    """Every p-value within a relative 1e-12 of its 60-digit value, up to 1e10 degrees of
    freedom (the far tails too: 1e-198 is a number, not zero)."""
    assert rel(dist.t_sf(t, df), want) < 1e-12
    assert rel(dist.t_cdf(-t, df), want) < 1e-12


# ── the functions under them ─────────────────────────────────────────────────
@pytest.mark.parametrize("a, b", [(0.5, 0.5), (1, 1), (3.3, 9.9), (0.5, 50), (9.99, 10), (10, 10.1), (50, 1e4),
                                  (0.5, 5e8), (1e6, 1e7), (5e9, 0.5)])
def test_lbeta_keeps_its_digits_when_a_parameter_is_large(a, b):
    """ln B(a, b) = ln B(a, b + 1) + ln((a + b)/b): the identity holds to the last digits
    only when no large numbers were subtracted to get either side."""
    assert dist.lbeta(a, b) == dist.lbeta(b, a)
    assert dist.lbeta(a, b) == pytest.approx(dist.lbeta(a, b + 1.0) + math.log1p(a / b), abs=4e-15 * max(1.0, abs(dist.lbeta(a, b))))


def test_lbeta_known_values():
    assert dist.lbeta(0.5, 0.5) == pytest.approx(math.log(math.pi), rel=1e-15)
    assert dist.lbeta(1, 7) == pytest.approx(-math.log(7), rel=1e-15)
    # B(½, b) ~ √(π/b) for a large b, with its first correction
    b = 5e8
    assert dist.lbeta(0.5, b) == pytest.approx(0.5 * math.log(math.pi / b) + 1 / (8 * b), abs=1e-14)


def test_betainc_closed_forms_and_mirror():
    # I_x(1, b) = 1 − (1 − x)^b ; I_x(a, 1) = x^a ; I_x(½, ½) = (2/π)·asin(√x)
    assert dist.betainc(1, 3, 0.2, 0.8) == pytest.approx(1 - 0.8**3, rel=1e-14)
    assert dist.betainc(2.5, 1, 0.3, 0.7) == pytest.approx(0.3**2.5, rel=1e-14)
    assert dist.betainc(0.5, 0.5, 0.25, 0.75) == pytest.approx(2 / math.pi * math.asin(0.5), rel=1e-14)
    assert dist.betainc(2, 3, 0.0, 1.0) == 0.0 and dist.betainc(2, 3, 1.0, 0.0) == 1.0
    for a, b, x in [(0.3, 0.7, 0.1), (4, 9, 0.6), (120, 3, 0.99), (0.5, 2000, 1e-3)]:
        assert dist.betainc(a, b, x, 1 - x) + dist.betainc(b, a, 1 - x, x) == pytest.approx(1.0, rel=1e-13)


def test_betainc_reads_y_from_the_caller():
    """Near 1 the caller's y is the whole information: x alone, rounded, would give the
    tail of another t. Here y is the exact one, and x the double next to 1 − y."""
    df, t = 1e9, 2.5
    y = t * t / (df + t * t)
    exact = 0.5 * dist.betainc(df / 2, 0.5, df / (df + t * t), y)
    assert rel(exact, 0.006209665405201246972337408) < 1e-12
    off = 0.5 * dist.betainc(df / 2, 0.5, df / (df + t * t), y * (1 + 1e-9))
    assert rel(off, 0.006209665405201246972337408) > 1e-10


# ── the density, for the report's curve ──────────────────────────────────────
def test_t_density_closed_forms():
    assert rel(dist.t_pdf(0.0, 1), 1.0 / math.pi) < 1e-14
    assert rel(dist.t_pdf(-2.0, 1), 1.0 / (5.0 * math.pi)) < 1e-14
    assert rel(dist.t_pdf(1.0, 2), 3.0 ** -1.5) < 1e-14
    assert rel(dist.t_pdf(2.0, 3), 6.0 * math.sqrt(3.0) / (49.0 * math.pi)) < 1e-14
    assert dist.t_pdf(-1.3, 12.7) == dist.t_pdf(1.3, 12.7)
    assert dist.t_pdf(math.inf, 5) == 0.0 and dist.t_pdf(1e200, 5) == 0.0
    assert math.isnan(dist.t_pdf(math.nan, 5))


def test_t_density_with_many_degrees_of_freedom():
    """A billion rows: the density is the normal's less (1 + 2t² − t⁴)/(4·df) of it, and two
    `lgamma` subtracted would have lost that and more."""
    for t in (0.0, 1.0, 2.5):
        normal = math.exp(-0.5 * t * t) / math.sqrt(2.0 * math.pi)
        assert rel(dist.t_pdf(t, 1e9), normal * (1.0 - (1.0 + 2.0 * t * t - t**4) / 4e9)) < 1e-13


@pytest.mark.parametrize("df", [1, 2.5, 12.727892707548934, 300, 1e6])
def test_the_density_is_the_slope_of_the_tail(df):
    """Simpson's rule on the density between two points gives the tail's difference."""
    a, b, n = -1.5, 2.25, 2000
    h = (b - a) / n
    area = dist.t_pdf(a, df) + dist.t_pdf(b, df) + sum((4 if i % 2 else 2) * dist.t_pdf(a + i * h, df) for i in range(1, n))
    assert rel(area * h / 3.0, dist.t_cdf(b, df) - dist.t_cdf(a, df)) < 1e-10


# ── lot 2: F, chi-square, the incomplete gamma ───────────────────────────────
# (f, d1, d2, P(F ≥ f), P(F ≤ f)), 22 digits: I_x(a, b) by its continued fraction in 60-digit
# arithmetic (mpmath, outside Tabularia), on the side where it converges, with y = 1 − x exact
F_ACCURACY = [
    (9.1, 2, 18, 0.001858127073281917747706, 0.9981418729267180822523),
    (0.3, 1, 1, 0.680994279960022896267, 0.319005720039977103733),
    (40, 4.5, 7, 0.00005846903573304164243268, 0.9999415309642669583576),
    (1.2, 199, 1e9, 0.02816097759139650750764, 0.9718390224086034924924),
    (3, 2, 1e9, 0.04978706881594755851416, 0.9502129311840524414858),
    (9.1, 199, 1e10, 1.251857436410541609593e-257, 1.0),
    (0.01, 19, 2.5, 0.9999999999774882235952, 2.251177640484010929704e-11),
    (300, 3, 100, 7.63430134696628343782e-50, 1.0),
    (1.0, 1, 1e6, 0.3173107498335781292817, 0.6826892501664218707183),
    (1.2, 7, 6, 0.4200382517897833260456, 0.5799617482102166739544),
    (0.4978, 7, 6, 0.8086590330557826344379, 0.1913409669442173655621),
]


@pytest.mark.parametrize("f, d1, d2, sf, cdf", F_ACCURACY)
def test_f_accuracy_contract(f, d1, d2, sf, cdf):
    """The t's contract for the F: within a relative 1e-12 of the 60-digit value, both tails,
    up to 1e10 degrees of freedom (measured on a grid of 432 points: 4.6e-13 at worst)."""
    assert rel(dist.f_sf(f, d1, d2), sf) < 1e-12
    assert rel(dist.f_cdf(f, d1, d2), cdf) < 1e-12


# (x, df, P(X ≥ x), P(X ≤ x)): the incomplete gamma by its series and its continued fraction
# in 50-digit arithmetic (mpmath)
CHI2_ACCURACY = [
    (200, 3, 4.218541107192042337657e-43, 1.0),
    (5.99146454710798, 2, 0.05000000000000005136801, 0.949999999999999948632),
    (0.5, 0.5, 0.2563220552685389583291, 0.7436779447314610416709),
    (50, 0.7, 6.566681496366442100421e-13, 0.9999999999993433318504),
    (0.001, 1, 0.9747728793699603882798, 0.02522712063003961172023),
    (2955.371716450253, 2401, 4.154740452111646281888e-14, 0.9999999999999584525955),
    (179.05006265674, 199, 0.8417812774757455872319, 0.1582187225242544127681),
    (100894.42719099992, 1e5, 0.02299104517063033456323, 0.9770089548293696654368),
    (99957573.5931288, 1e8, 0.9986517728355051893628, 0.00134822716449481063721),
    (1e10, 1e10, 0.4999981193680548387228, 0.5000018806319451612772),
    (10001131370.8499, 1e10, 6.235981885872548576213e-16, 0.9999999999999993764018),
]


@pytest.mark.parametrize("x, df, sf, cdf", CHI2_ACCURACY)
def test_chi_square_accuracy_contract(x, df, sf, cdf):
    """The same contract for the chi-square (measured on 178 points up to df 1e10: 6.1e-13 at
    worst). Far from zero the front factor of the incomplete gamma is taken with its large
    terms cancelled (Stirling), or a billion degrees of freedom would leave five digits."""
    assert rel(dist.chi2_sf(x, df), sf) < 1e-12
    assert rel(dist.chi2_cdf(x, df), cdf) < 1e-12


def test_quantiles_of_f_and_chi_square():
    assert rel(dist.f_isf(0.05, 2, 18), 3.55455714566179) < 1e-12
    assert rel(dist.chi2_isf(0.05, 2), 5.99146454710798) < 1e-12
    # the lower quantile of F as 1 / the upper one of F(d2, d1): a small p keeps its digits
    assert rel(dist.f_cdf(dist.f_ppf(1e-9, 7, 6), 7, 6), 1e-9) < 1e-10
    assert rel(dist.f_ppf(0.025, 7, 6) * dist.f_isf(0.025, 6, 7), 1.0) < 1e-15
    assert dist.f_isf(0.0, 2, 3) == math.inf and dist.f_isf(1.0, 2, 3) == 0.0 and dist.f_ppf(0.0, 2, 3) == 0.0
    assert math.isnan(dist.chi2_isf(-0.1, 3))


@pytest.mark.parametrize("q", [0.4, 0.05, 1e-3, 1e-12, 1e-100])
@pytest.mark.parametrize("d1, d2", [(1, 1), (2, 18), (7, 6), (199, 1e9), (3, 2.5)])
def test_the_f_quantile_inverts_the_tail(q, d1, d2):
    assert rel(dist.f_sf(dist.f_isf(q, d1, d2), d1, d2), q) < 1e-10
    assert rel(dist.f_cdf(dist.f_ppf(q, d1, d2), d1, d2), q) < 1e-10


@pytest.mark.parametrize("q", [0.4, 0.05, 1e-3, 1e-12, 1e-100])
@pytest.mark.parametrize("df", [1, 2, 7, 199, 2401, 1e5])
def test_the_chi_square_quantile_inverts_the_tail(q, df):
    assert rel(dist.chi2_sf(dist.chi2_isf(q, df), df), q) < 1e-10


def test_densities_closed_forms():
    assert rel(dist.norm_pdf(0.0), 1 / math.sqrt(2 * math.pi)) < 1e-15
    assert rel(dist.chi2_pdf(3.0, 2), 0.5 * math.exp(-1.5)) < 1e-14  # an exponential of mean 2
    assert rel(dist.chi2_pdf(2.0, 1), math.exp(-1.0) / math.sqrt(4 * math.pi)) < 1e-14
    assert dist.chi2_pdf(0.0, 1) == math.inf and dist.chi2_pdf(0.0, 2) == 0.5 and dist.chi2_pdf(0.0, 3) == 0.0
    # F(2, 2): 1 / (1 + f)²
    assert rel(dist.f_pdf(3.0, 2, 2), 1 / 16) < 1e-14
    assert dist.f_pdf(0.0, 1, 5) == math.inf and dist.f_pdf(0.0, 2, 5) == 1.0 and dist.f_pdf(-1.0, 3, 5) == 0.0


@pytest.mark.parametrize("d1, d2", [(1, 1), (2, 18), (7, 6), (5, 1e6)])
def test_the_f_density_is_the_slope_of_the_tail(d1, d2):
    a, b, n = 0.5, 4.0, 4000
    h = (b - a) / n
    area = dist.f_pdf(a, d1, d2) + dist.f_pdf(b, d1, d2) + sum((4 if i % 2 else 2) * dist.f_pdf(a + i * h, d1, d2) for i in range(1, n))
    assert rel(area * h / 3.0, dist.f_cdf(b, d1, d2) - dist.f_cdf(a, d1, d2)) < 1e-10


@pytest.mark.parametrize("df", [1, 3, 20, 2401])
def test_the_chi_square_density_is_the_slope_of_the_tail(df):
    a, b, n = df * 0.5, df * 1.5 + 2, 4000
    h = (b - a) / n
    area = dist.chi2_pdf(a, df) + dist.chi2_pdf(b, df) + sum((4 if i % 2 else 2) * dist.chi2_pdf(a + i * h, df) for i in range(1, n))
    assert rel(area * h / 3.0, dist.chi2_cdf(b, df) - dist.chi2_cdf(a, df)) < 1e-10


def test_incomplete_gamma_closed_forms():
    # Q(1, x) = e^(−x); Q(½, x) = erfc(√x); P + Q = 1
    for x in (1e-3, 0.5, 2.0, 30.0, 700.0):
        assert rel(dist.gammainc_q(1.0, x), math.exp(-x)) < 1e-12  # e^(−700) itself is known to 700 ulp
        assert rel(dist.gammainc_q(0.5, x), math.erfc(math.sqrt(x))) < 1e-12
    for a, x in ((3.3, 1.0), (3.3, 9.0), (50, 49), (1e4, 1.01e4)):
        assert dist.gammainc_p(a, x) + dist.gammainc_q(a, x) == pytest.approx(1.0, abs=1e-15)
    assert dist.gammainc_q(2.0, 0.0) == 1.0 and dist.gammainc_p(2.0, math.inf) == 1.0


# ── the studentized range ────────────────────────────────────────────────────
# P(Q ≤ q; k, df), SciPy 1.18.1's studentized_range (the spec's references)
SRANGE = [
    (3.5, 3, 18, 0.94177531240136), (1, 2, 5, 0.48891591956972), (4.2, 5, 10, 0.91785442462170),
    (2, 10, 3, 0.13287846948945), (6, 20, 60, 0.98966494238124), (3.3, 3, 11.867, 0.91040711139581),
    (0.3, 3, 2, 0.02420479824609), (12, 8, 4, 0.99135198682789), (4, 12, 30, 0.78158394507050),
    (3.9, 20, 1000, 0.61295939541927), (50, 3, 1, 0.97300040851523), (7, 30, 15, 0.97610409142616),
]


@pytest.mark.parametrize("q, k, df, want", SRANGE)
def test_studentized_range_against_scipy(q, k, df, want):
    """Within an absolute 2e-10 (measured on 880 points, k from 2 to 20, df from 1 to 20,000,
    q from 0.2 to 40: 3e-11 at worst, where SciPy itself is the one off — see below)."""
    assert abs(dist.srange_cdf(q, k, df) - want) < 2e-10
    assert abs(dist.srange_sf(q, k, df) - (1.0 - want)) < 2e-10


@pytest.mark.parametrize("p, k, df, want", [
    (0.95, 3, 18, 3.609303828706538), (0.95, 5, 10, 4.6542929978545375), (0.99, 3, 11.867, 5.058114579318116),
    (0.95, 20, 4.4, 8.752534417194683),
])
def test_studentized_range_quantiles(p, k, df, want):
    assert rel(dist.srange_ppf(p, k, df), want) < 1e-9
    assert rel(dist.srange_isf(1.0 - p, k, df), want) < 1e-9


@pytest.mark.parametrize("df", [1, 2.5, 7, 19.5, 20, 60, 1000, 20000, 1e6, 1e9])
@pytest.mark.parametrize("q", [0.2, 1.0, 2.5, 4.0, 8.0, 12.0])
def test_the_range_of_two_is_a_t(df, q):
    """Two means: Q = √2·|T|, so P(Q > q) = 2·P(T ≥ q/√2), to a relative 5e-12 even far in the
    tail (1.8e-12 at worst, at df 1e9) — the tail is a sum of small numbers, never 1 less one
    near 1. (SciPy's own is 8e-4 off at q = 8, df = 20,000, and returns the infinite-df value
    past 1e5.)"""
    want = 2.0 * dist.t_sf(q / math.sqrt(2.0), df)
    assert rel(dist.srange_sf(q, 2, df), want) < 5e-12
    assert rel(dist.srange_isf(want, 2, df), q) < 1e-10


def test_studentized_range_small_q_against_sixty_digits():
    """A small q, where the probability is the difference of the tail from 1: 9.27e-5 for
    k = 5, df = 60 (mpmath, a double integral at 30 digits; SciPy agrees to 2e-11)."""
    assert abs(dist.srange_cdf(0.2, 5, 60) - 0.0000927184584636183038469134044867) < 1e-10


def test_studentized_range_far_in_the_tail_is_a_number():
    tail = dist.srange_sf(20.0, 3, 1500.0)
    assert 0.0 < tail < 1e-40
    # smooth in q: two engines whose means differ in the last digit give the same p-value
    assert rel(dist.srange_sf(20.0 * (1 + 1e-14), 3, 1500.0), tail) < 1e-11
    assert dist.srange_sf(0.0, 4, 10) == 1.0 and dist.srange_sf(math.inf, 4, 10) == 0.0
    assert math.isnan(dist.srange_sf(math.nan, 4, 10))

