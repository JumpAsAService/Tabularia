"""The original scale (lot R4, docs/specs/bayes-runner.md): the coefficients of a linear predictor on the columns' own scale, draw by
draw. A regression with a flat prior has an exact posterior (a multivariate normal around the least squares), so synthetic draws of it
on the standardized columns must come out, on the original scale, at the regression made on the raw columns within the Monte Carlo
error; a vector intercept (a_g[g]) shifts every element by the same sum; the budgets do not change a number; the cap is kept."""
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py"), str(HERE)]

import numpy as np  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import bayes_diag  # noqa: E402
import bayes_draws  # noqa: E402
import bayes_pass2 as P2  # noqa: E402
import bayes_rows  # noqa: E402
import bayes_scale  # noqa: E402
from bayes_task import Element, Entry, Layout, ManifestMismatch  # noqa: E402

CHAINS, DRAWS = 4, 1500
SAMPLER = ("lp__", "accept_stat__", "divergent__")
MEANS, SDS = {"x1": 50.0, "x2": -3.0}, {"x1": 10.0, "x2": 0.5}
GROUPS = 3


def layout_of(names, plate=None):
    draws = [Element(n, None, None, None, None) for n in SAMPLER]
    entries = []
    for n in names:
        entries.append(Entry(n, n, n, n, "parameter", None, None, len(draws), 1, True, False))
        draws.append(Element(n, n, None, None, None))
    if plate:
        entries.append(Entry(plate, plate, plate, plate, "effect", "g", "G", len(draws), GROUPS, False, False))
        draws.extend(Element(f"{plate}[{i + 1}]", plate, f"g{i + 1}", i + 1, None) for i in range(GROUPS))
    p = len(draws)
    return Layout("sample", [e.quantity for e in draws], p, np.arange(p, dtype=np.int64), draws, np.zeros(0, dtype=np.int64), [],
                  entries, len(SAMPLER))


def write(path, layout, values):
    """values: (quantities of the program, chains, draws) -> the long draws file."""
    p = len(layout.draws)
    w = bayes_draws.DrawsWriter(path, layout.draws, layout.draw_columns, CHAINS, DRAWS, 250, {"seed": 1}, n_unchecked=layout.n_sampler)
    for chain, first, count in w.schedule():
        b = np.zeros((count, p))
        b[:, layout.n_sampler:] = values[:, chain - 1, first - 1:first - 1 + count].T
        w.write_block(chain, first, b)
    w.close()


class Regression:
    """y = 2 + 3 x1 - 1.5 x2 + noise on raw columns; the flat-prior posterior on the standardized columns, drawn exactly."""

    def __init__(self):
        rng = np.random.default_rng(7)
        n = 400
        self.n = n
        x1, x2 = rng.normal(50, 10, n), rng.normal(-3, 0.5, n)
        self.sigma = 2.0
        y = 2.0 + 3.0 * x1 - 1.5 * x2 + rng.normal(0, self.sigma, n)
        self.m = {"x1": x1.mean(), "x2": x2.mean()}
        self.s = {"x1": x1.std(ddof=1), "x2": x2.std(ddof=1)}
        z = np.column_stack([np.ones(n), (x1 - self.m["x1"]) / self.s["x1"], (x2 - self.m["x2"]) / self.s["x2"]])
        raw = np.column_stack([np.ones(n), x1, x2])
        self.raw_fit = np.linalg.solve(raw.T @ raw, raw.T @ y)
        self.raw_se = self.sigma * np.sqrt(np.diag(np.linalg.inv(raw.T @ raw)))
        beta = np.linalg.solve(z.T @ z, z.T @ y)
        cov = self.sigma ** 2 * np.linalg.inv(z.T @ z)
        draws = rng.multivariate_normal(beta, cov, size=CHAINS * DRAWS)
        self.draws = draws.T.reshape(3, CHAINS, DRAWS)      # alpha, b_x1, b_x2
        self.standardized = {k: {"mean": self.m[k], "sd": self.s[k]} for k in ("x1", "x2")}


def run(path, layout, limits=None, budget=None, **kw):
    out = Path(tempfile.mkdtemp(dir=path))
    w = bayes_rows.ResultsWriter(str(out / "results.parquet"), flush_rows=64)
    w.text("model", "model", "m")
    w.text("model", "variant", "sample")
    w.number("model", "n", 1)
    report = P2.second_pass(str(path / "draws.parquet"), layout, limits or {}, w, summary_path=str(out / "summary.parquet"),
                            budget=budget, **kw)
    w.close()
    return report, pq.read_table(out / "results.parquet").to_pydict(), pq.read_table(out / "summary.parquet").to_pydict()


class TestTheOriginalScale(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.reg = Regression()
        self.layout = layout_of(["alpha", "b_x1", "b_x2"])
        write(self.dir / "draws.parquet", self.layout, self.reg.draws)
        self.pairs = [{"coef": "b_x1", "data": "x1"}, {"coef": "b_x2", "data": "x2"},
                      {"intercept": "alpha", "lp": ["b_x1", "b_x2"], "data": ["x1", "x2"]}]

    def tearDown(self):
        self.tmp.cleanup()

    def scale(self, pairs=None, cap=600):
        return bayes_scale.plan(self.pairs if pairs is None else pairs, self.layout, self.reg.standardized, cap)

    def original_rows(self, summary):
        return {q: i for i, (q, s) in enumerate(zip(summary["quantity"], summary["scale"])) if s == "original"}

    def test_the_original_scale_is_the_regression_on_the_raw_columns_within_the_monte_carlo_error(self):
        report, rows, summary = run(self.dir, self.layout, scale=self.scale())
        got = self.original_rows(summary)
        self.assertEqual(sorted(got), ["alpha", "b_x1", "b_x2"])
        self.assertEqual(report.original, 3)
        s = CHAINS * DRAWS
        for name, k in (("alpha", 0), ("b_x1", 1), ("b_x2", 2)):
            i = got[name]
            self.assertLess(abs(summary["mean"][i] - self.reg.raw_fit[k]), 4 * self.reg.raw_se[k] / np.sqrt(s) * 1.5, name)
            self.assertAlmostEqual(summary["sd"][i] / self.reg.raw_se[k], 1.0, delta=0.05, msg=name)
            self.assertIsNone(summary["rhat"][i])
            self.assertIsNone(summary["contraction"][i])
        # the model-scale rows are untouched
        model = {q: i for i, (q, s_) in enumerate(zip(summary["quantity"], summary["scale"])) if s_ == "model"}
        self.assertEqual(sorted(model), ["alpha", "b_x1", "b_x2"])
        self.assertAlmostEqual(summary["mean"][model["alpha"]], self.reg.draws[0].mean(), places=9)

    def test_the_numbers_are_those_of_a_hand_calculation_draw_by_draw(self):
        _, rows, summary = run(self.dir, self.layout, scale=self.scale())
        got = self.original_rows(summary)
        b1, b2, a = self.reg.draws[1], self.reg.draws[2], self.reg.draws[0]
        hand = {"b_x1": b1 / self.reg.s["x1"], "b_x2": b2 / self.reg.s["x2"],
                "alpha": a - b1 * self.reg.m["x1"] / self.reg.s["x1"] - b2 * self.reg.m["x2"] / self.reg.s["x2"]}
        for name, d in hand.items():
            want = bayes_diag.measures(d[None], diagnostics=False)
            for stat in ("mean", "sd", "q5", "q25", "q50", "q75", "q95"):
                self.assertAlmostEqual(summary[stat][got[name]], float(want[stat][0]), places=9, msg=f"{name} {stat}")
        by_key = dict(zip(rows["key"], rows["value"]))
        for stat in ("mean", "sd", "q5", "q50", "q95"):
            self.assertAlmostEqual(by_key[f"summary|alpha||orig_{stat}"], summary[stat][got["alpha"]], places=9)
        self.assertNotIn("summary|alpha||orig_q25", by_key)

    def test_the_budgets_do_not_change_a_number(self):
        s = CHAINS * DRAWS
        _, rows_a, sum_a = run(self.dir, self.layout, scale=self.scale())
        small = P2.Budget(8 * s, bayes_diag.BYTES_PER_VALUE * s, 1 << 20)
        _, rows_b, sum_b = run(self.dir, self.layout, budget=small, scale=self.scale())
        self.assertEqual(sum_a, sum_b)
        self.assertEqual(rows_a, rows_b)

    def test_a_vector_intercept_shifts_each_element_by_the_same_sum(self):
        layout = layout_of(["b_x1", "b_x2"], plate="a_g")
        rng = np.random.default_rng(3)
        a_g = rng.normal([1.0, 2.0, 3.0], 0.3, size=(CHAINS, DRAWS, GROUPS)).transpose(2, 0, 1)
        write(self.dir / "draws.parquet", layout, np.concatenate([self.reg.draws[1:], a_g]))
        pairs = [{"coef": "b_x1", "data": "x1"}, {"coef": "b_x2", "data": "x2"},
                 {"intercept": "a_g", "lp": ["b_x1", "b_x2"], "data": ["x1", "x2"]}]
        plan = bayes_scale.plan(pairs, layout, self.reg.standardized, 600)
        report, _, summary = run(self.dir, layout, scale=plan)
        got = self.original_rows(summary)
        self.assertEqual(report.original, 5)
        shift = (self.reg.draws[1] * self.reg.m["x1"] / self.reg.s["x1"] + self.reg.draws[2] * self.reg.m["x2"] / self.reg.s["x2"])
        for g in range(GROUPS):
            want = bayes_diag.measures((a_g[g] - shift)[None], diagnostics=False)
            self.assertAlmostEqual(summary["mean"][got[f"a_g[{g + 1}]"]], float(want["mean"][0]), places=9)
            self.assertAlmostEqual(summary["q95"][got[f"a_g[{g + 1}]"]], float(want["q95"][0]), places=9)

    def test_a_predictor_without_an_intercept_slot_has_its_terms_alone(self):
        plan = self.scale([{"coef": "b_x1", "data": "x1"}, {"intercept": None, "lp": ["b_x1"], "data": ["x1"]}])
        report, _, summary = run(self.dir, self.layout, scale=plan)
        self.assertEqual(sorted(self.original_rows(summary)), ["b_x1"])
        self.assertEqual(report.original, 1)

    def test_the_cap_keeps_the_first_quantities_and_says_so(self):
        plan = self.scale(cap=2)
        self.assertEqual((plan.count, plan.capped), (2, 1))
        report, rows, summary = run(self.dir, self.layout, scale=plan)
        self.assertEqual((report.original, report.original_capped), (2, 1))
        self.assertEqual(sorted(self.original_rows(summary)), ["b_x1", "b_x2"])
        self.assertIn("model|||note_original_capped", rows["key"])

    def test_only_the_scope_has_results_rows_and_a_pair_that_is_not_in_the_manifest_is_a_fault(self):
        _, rows, _ = run(self.dir, self.layout, limits={"bayes_max_summary_quantities": 1}, scale=self.scale())
        origs = [k for k in rows["key"] if "|orig_" in k]
        self.assertEqual(len(origs), 5)
        with self.assertRaises(ManifestMismatch):
            self.scale([{"coef": "nope", "data": "x1"}])
        with self.assertRaises(ManifestMismatch):
            self.scale([{"coef": "b_x1", "data": "z"}])

    def test_the_prior_predictive_mode_has_no_original_scale(self):
        report, rows, summary = run(self.dir, self.layout, diagnostics=False, scale=self.scale())
        self.assertEqual(report.original, 0)
        self.assertEqual(set(summary["scale"]), {"model"})


if __name__ == "__main__":
    unittest.main()
