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
