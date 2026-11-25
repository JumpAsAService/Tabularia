"""The per-quantity measures (lot X1 of docs/specs/bayes-diagnostics.md): every case of the witnesses (reference/diagnostics/
witness.json: the R package posterior 1.8.0) within the spec's tolerances (criterion 1), AS241 against the standard
library's NormalDist, no leak across the quantities' axis, the NULLs, the refusal of a non-finite draw and the working memory
the second pass sizes its sub-blocks with. Small data only: the cases are a few thousand draws."""
import json
import math
import statistics
import sys
import tracemalloc
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py"), str(HERE)]
WITNESSES = HERE.parents[1] / "reference" / "diagnostics"

import numpy as np  # noqa: E402

import bayes_diag as D  # noqa: E402
from numeric import REORDER_RTOL  # noqa: E402

# the spec's tolerances («Witnesses»), relative to the witness's number; the mean and the sd, which the spec does not name,
# at the quantiles' (one formula, double precision: only the order of the sums differs)
TOLERANCE = {"rhat": 1e-8, "ess_bulk": 1e-8, "ess_tail": 1e-8, "mcse_mean": 1e-8, "mcse_sd": 1e-8,
             "q5": 1e-12, "q25": 1e-12, "q50": 1e-12, "q75": 1e-12, "q95": 1e-12, "mean": 1e-12, "sd": 1e-12}
DIAGNOSTICS = ("mcse_mean", "mcse_sd", "rhat", "ess_bulk", "ess_tail")
# where the runner gives NULL and posterior a number: R-hat of split chains of fewer than 3 draws (the contract's rule;
# posterior's .rhat has no minimum, its .ess one of 3)
CONTRACT_NULLS = {("too_short", "rhat")}


def load():
    cases = json.loads((WITNESSES / "cases.json").read_text(encoding="utf-8"))
    witness = json.loads((WITNESSES / "witness.json").read_text(encoding="utf-8"))
    return cases, witness


def relative_gap(ours: float, theirs: float) -> float:
    return abs(ours - theirs) / abs(theirs) if theirs != 0 else abs(ours)


class TestTheWitnesses(unittest.TestCase):
    """Criterion 1: every case of witness.json within its tolerance, ours equal to posterior's."""

    @classmethod
    def setUpClass(cls):
        cls.cases, cls.witness = load()
        cls.ours = {c["name"]: D.measures(np.asarray(c["draws"], dtype=float)[None]) for c in cls.cases["cases"]}

    def test_the_witness_is_posterior_1_8_0_on_every_case(self):
        self.assertIn("posterior@1.8.0", self.witness["r"])
        self.assertEqual(set(self.witness["posterior"]["cases"]), {c["name"] for c in self.cases["cases"]})
        self.assertGreaterEqual(len(self.cases["cases"]), 15)

    def test_every_case_within_the_declared_tolerances(self):
        worst = {k: 0.0 for k in TOLERANCE}
        for name, ours in self.ours.items():
            theirs = self.witness["posterior"]["cases"][name]
            for k, tol in TOLERANCE.items():
                a, b = float(ours[k][0]), theirs[k]
                with self.subTest(case=name, measure=k):
                    if (name, k) in CONTRACT_NULLS:
                        self.assertTrue(math.isnan(a), f"{a!r}: the contract's NULL")
                        self.assertIsNotNone(b)
                        continue
                    if b is None:
                        self.assertTrue(math.isnan(a), f"posterior NA, ours {a!r}")
                        continue
                    self.assertFalse(math.isnan(a), f"posterior {b!r}, ours NULL")
                    gap = relative_gap(a, b)
                    worst[k] = max(worst[k], gap)
                    self.assertLessEqual(gap, tol, f"ours {a!r}, posterior's {b!r}")
        # the largest gaps are far inside the tolerances (recorded in the lot's notes)
        for k, g in worst.items():
            self.assertLess(g, TOLERANCE[k] / 100, f"{k}: {g:.3g}")

    def test_the_parts_of_r_hat_and_of_the_tail_ess(self):
        # the bulk and the folded R-hat, the ESS of each tail's indicator, of the mean and of the squares, one by one
        for c in self.cases["cases"]:
            theirs = self.witness["posterior"]["cases"][c["name"]]
            x = np.asarray(c["draws"], dtype=float)[None]
            if D.is_constant(x)[0] or x.shape[2] // 2 < 3:
                continue
            split = D.split_chains(x)
            flat = np.sort(x.reshape(1, -1), axis=1)
            q = D.quantiles(flat, (0.05, 0.95))
            folded = np.abs(x - D.median(flat)[:, None, None])
            centred = x - x.mean()
            parts = {"rhat_bulk": D.rhat_basic(D.z_scale(split)),
                     "rhat_tail": D.rhat_basic(D.z_scale(D.split_chains(folded))),
                     "ess_q5": D.ess_basic(D.split_chains((x <= q[:, 0, None, None]).astype(float))),
                     "ess_q95": D.ess_basic(D.split_chains((x >= q[:, 1, None, None]).astype(float))),
                     "ess_mean": D.ess_basic(split),
                     "ess_sd": D.ess_basic(D.split_chains(centred ** 2))}
            for k, v in parts.items():
                with self.subTest(case=c["name"], part=k):
                    if theirs[k] is None:
                        self.assertTrue(math.isnan(v[0]))
                    else:
                        self.assertLessEqual(relative_gap(float(v[0]), theirs[k]), 1e-8, f"{v[0]!r} {theirs[k]!r}")

    def test_where_the_two_witnesses_differ_ours_is_posteriors(self):
        # every difference witness.json records between posterior and ArviZ has a reason; on each, ours is posterior's
        differences = self.witness["differences"]
        self.assertTrue(differences)
        for d in differences:
            self.assertTrue(d["reason"] and d["reason"] != "UNEXPLAINED", d)
            if d["measure"] not in TOLERANCE or (d["case"], d["measure"]) in CONTRACT_NULLS:
                continue
            a = float(self.ours[d["case"]][d["measure"]][0])
            with self.subTest(case=d["case"], measure=d["measure"]):
                if d["posterior"] is None:
                    self.assertTrue(math.isnan(a))
                else:
                    self.assertLessEqual(relative_gap(a, d["posterior"]), TOLERANCE[d["measure"]])
                    if d["arviz"] is not None and d["relative_gap"] and d["relative_gap"] > 1e-6:
                        self.assertGreater(relative_gap(a, d["arviz"]), 1e-7)

    def test_no_number_is_infinite(self):
        for name, ours in self.ours.items():
            for k in D.NUMBERS:
                v = float(ours[k][0])
                self.assertTrue(math.isnan(v) or math.isfinite(v), f"{name} {k} {v!r}")


class TestAS241(unittest.TestCase):
    def test_against_the_standard_librarys_normal_dist(self):
        # 10,000 points from 1e-300 to 1 - 1e-16: equal to the last bit, or within a relative 1e-15
        p = np.concatenate([np.logspace(-300, -1, 4000), np.linspace(0.075, 0.925, 2000), 1.0 - np.logspace(-16, -1, 4000)])
        self.assertEqual(len(p), 10_000)
        self.assertTrue(((p > 0) & (p < 1)).all())
        ours = D.inv_cdf(p)
        nd = statistics.NormalDist()
        theirs = np.array([nd.inv_cdf(float(v)) for v in p])
        same = ours == theirs
        close = np.abs(ours - theirs) <= 1e-15 * np.abs(theirs)
        self.assertTrue((same | close).all(), f"{p[~(same | close)][:5]}")
        self.assertGreater(same.mean(), 0.5)
        self.assertEqual(D.inv_cdf(np.array([0.5]))[0], 0.0)

    def test_the_branches(self):
        # the central rational function, the near and the far tails, and the symmetry
        for v in (0.3, 0.5 + 0.425, 0.5 - 0.425, 1e-3, 1e-10, 1e-12, 1e-100, 1 - 1e-3):
            self.assertAlmostEqual(D.inv_cdf(np.array([v]))[0], statistics.NormalDist().inv_cdf(v), delta=1e-15 * 40)
        p = np.array([1e-5, 0.01, 0.2])
        np.testing.assert_allclose(D.inv_cdf(p), -D.inv_cdf(1.0 - p), rtol=1e-11)


class TestTheBlocks(unittest.TestCase):
    def test_one_quantity_alone_gives_the_numbers_of_a_sub_block_of_500_holding_it(self):
        cases, _ = load()
        rng = np.random.default_rng(20261124)
        x = rng.normal(size=(500, 4, 200))
        x[:100] = np.round(x[:100] * 10) / 10                      # ties
        x[100:150] = 3.0                                           # constants
        x[150:200, 3] += 2.0                                       # a shifted chain
        x[200:250] = 1000.0 + np.round(1e-3 * x[200:250], 4)       # coarse ties around 1,000
        iid = np.asarray(cases["cases"][0]["draws"])[:, :200]
        x[499] = iid
        block = D.measures(x)
        for i in (0, 120, 160, 210, 300, 499):
            alone = D.measures(x[i:i + 1])
            for k in D.NUMBERS:
                with self.subTest(quantity=i, measure=k):
                    # another shape, another order of the sums: the same numbers up to what reordering changes (numeric.py)
                    np.testing.assert_allclose(alone[k], block[k][i:i + 1], rtol=REORDER_RTOL, atol=0, equal_nan=True)

    def test_the_input_is_not_changed(self):
        x = np.random.default_rng(3).normal(size=(3, 2, 50))
        before = x.copy()
        D.measures(x)
        np.testing.assert_array_equal(x, before)


class TestNulls(unittest.TestCase):
    def test_a_constant_quantity_has_its_summary_and_no_diagnostic(self):
        m = D.measures(np.full((1, 4, 100), 2.5))
        for k in ("mean", "q5", "q50", "q95"):
            self.assertEqual(m[k][0], 2.5)
        self.assertEqual(m["sd"][0], 0.0)
        for k in DIAGNOSTICS:
            self.assertTrue(np.isnan(m[k][0]), k)

    def test_fewer_than_three_draws_a_split_chain(self):
        for n in (1, 2, 4, 5):
            m = D.measures(np.random.default_rng(n).normal(size=(2, 4, n)))
            for k in DIAGNOSTICS:
                self.assertTrue(np.isnan(m[k]).all(), (n, k))
            self.assertTrue(np.isfinite(m["mean"]).all())
        m = D.measures(np.random.default_rng(6).normal(size=(2, 4, 6)))
        self.assertTrue(np.isfinite(m["ess_bulk"]).all())

    def test_a_constant_indicator_leaves_the_tail_ess_null(self):
        # 96 % of the draws at 0: q95 = 0, the upper indicator x >= q95 is every draw (posterior: NA), the bulk defined
        x = np.zeros((1, 4, 500))
        x[0, :, ::25] = 1.0
        m = D.measures(x)
        self.assertEqual(m["q95"][0], 0.0)
        self.assertTrue(np.isnan(m["ess_tail"][0]))
        self.assertTrue(np.isfinite(m["ess_bulk"][0]) and np.isfinite(m["rhat"][0]))

    def test_the_prior_predictive_mode_gives_the_summary_alone(self):
        x = np.random.default_rng(4).normal(size=(3, 1, 400))
        m = D.measures(x, diagnostics=False)
        full = D.measures(x)
        for k in ("mean", "sd", *D.QUANTILE_NAMES):
            np.testing.assert_array_equal(m[k], full[k])
        for k in DIAGNOSTICS:
            self.assertTrue(np.isnan(m[k]).all(), k)

    def test_a_non_finite_draw_is_refused_never_summed(self):
        for bad in (np.inf, -np.inf, np.nan):
            x = np.random.default_rng(5).normal(size=(2, 4, 50))
            x[1, 2, 7] = bad
            with self.assertRaisesRegex(ValueError, "not finite"):
                D.measures(x)
        with self.assertRaises(ValueError):
            D.measures(np.zeros((4, 50)))


class TestTheParts(unittest.TestCase):
    def test_the_split_leaves_an_odd_chains_middle_draw_out(self):
        x = np.arange(14, dtype=float).reshape(1, 2, 7)
        s = D.split_chains(x)
        self.assertEqual(s.shape, (1, 4, 3))
        np.testing.assert_array_equal(s[0], [[0, 1, 2], [7, 8, 9], [4, 5, 6], [11, 12, 13]])
        self.assertEqual(D.split_chains(x[:, :, :1]).shape, (1, 2, 1))   # one draw: not split

    def test_average_ranks_as_r(self):
        x = np.array([[3.0, 1.0, 3.0, 2.0, 3.0, 1.0], [5.0, 4.0, 3.0, 2.0, 1.0, 0.0]])
        np.testing.assert_array_equal(D.average_ranks(x), [[5.0, 1.5, 5.0, 3.0, 5.0, 1.5], [6, 5, 4, 3, 2, 1]])

    def test_type_7_quantiles_and_the_median_as_r(self):
        s = np.sort(np.random.default_rng(7).normal(size=(3, 101)), axis=1)
        np.testing.assert_allclose(D.quantiles(s, D.PROBS), np.quantile(s, D.PROBS, axis=1).T, rtol=1e-14, atol=1e-15)
        np.testing.assert_array_equal(D.median(s), s[:, 50])
        np.testing.assert_array_equal(D.median(s[:, :100]), (s[:, 49] + s[:, 50]) / 2)

    def test_the_next_fast_length(self):
        self.assertEqual([D._next_fast(n) for n in (1, 7, 11, 500, 999, 1001)], [1, 8, 12, 500, 1000, 1024])

    def test_the_ess_of_independent_draws_is_near_their_count(self):
        x = np.random.default_rng(8).normal(size=(20, 4, 1000))
        ess = D.measures(x)["ess_bulk"]
        # a sanity check only (the witnesses are the check): the ESS of i.i.d. draws varies by about 6 % around S
        self.assertTrue((np.abs(ess / 4000 - 1) < 0.25).all(), ess)
        self.assertLess(abs(ess.mean() / 4000 - 1), 0.05)


class TestTheWorkingMemory(unittest.TestCase):
    def test_the_peak_stays_below_the_declared_bytes_a_value(self):
        x = np.round(np.random.default_rng(9).normal(size=(40, 4, 1000)), 6)
        tracemalloc.start()
        try:
            D.measures(x)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertLessEqual(peak, D.BYTES_PER_VALUE * x.size, f"{peak / x.size:.1f} bytes a value")
        self.assertEqual(D.sub_block(256 << 20, 4000), (256 << 20) // (D.BYTES_PER_VALUE * 4000))
        self.assertEqual(D.sub_block(1, 4000), 1)


if __name__ == "__main__":
    unittest.main()
