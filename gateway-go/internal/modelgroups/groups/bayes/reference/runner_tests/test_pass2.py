"""The second pass and the charts (lot X3 of docs/specs/bayes-diagnostics.md, criteria 2 and 3's logic) on the fake draws of
fake_draws.py, a few thousand values: the summary equal to `measures` on the whole in-memory array, whatever the blocks; the order
checked once, a shuffled file refused; `value` alone read after that check; the charts' rows and caps; a unit named «a|b»; the
projection of the time; the prior predictive mode; no verdict word in any row."""
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py"), str(HERE)]

import numpy as np  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import bayes_charts  # noqa: E402
import bayes_diag  # noqa: E402
import bayes_pass2 as P2  # noqa: E402
import bayes_rows  # noqa: E402
import fake_draws  # noqa: E402
from bayes_task import ManifestMismatch  # noqa: E402
from tab_protocol import Refusal  # noqa: E402

CHAINS, DRAWS, Q = 4, 120, 14


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.layout = fake_draws.make_layout(Q)
        self.path = self.dir / "draws.parquet"
        self.full = fake_draws.write(self.path, self.layout, CHAINS, DRAWS, 50, divergent_every=17, keep=True)

    def tearDown(self):
        self.tmp.cleanup()

    def run_pass(self, name="a", limits=None, budget=None, **kw):
        out = self.dir / name
        out.mkdir(exist_ok=True)
        w = bayes_rows.ResultsWriter(str(out / "results.parquet"), flush_rows=64)
        w.text("model", "model", "m")
        w.text("model", "variant", "sample")
        w.number("model", "n", 1)
        report = P2.second_pass(str(self.path), self.layout, limits or {}, w, summary_path=str(out / "summary.parquet"), budget=budget, **kw)
        w.close()
        return report, pq.read_table(out / "results.parquet").to_pydict(), pq.read_table(out / "summary.parquet").to_pydict()


class TestTheNumbers(Base):
    def test_the_summary_is_what_measures_gives_on_the_whole_array(self):
        want = bayes_diag.measures(self.full[3:])
        report, rows, summary = self.run_pass()
        self.assertEqual(report.quantities, Q)
        for name in bayes_diag.NUMBERS:
            got = np.array([np.nan if v is None else v for v in summary[name]])
            np.testing.assert_array_equal(got, want[name], err_msg=name)
        self.assertEqual(summary["quantity"], [e.quantity for e in self.layout.draws[3:]])
        self.assertEqual(summary["scale"], ["model"] * Q)

    def test_the_results_rows_are_the_same_with_one_three_or_all_the_quantities_a_block(self):
        s = CHAINS * DRAWS
        results = []
        for name, per_block, per_sub in (("one", 1, 1), ("three", 3, 2), ("all", 100, 100)):
            budget = P2.Budget(8 * s * per_block, bayes_diag.BYTES_PER_VALUE * s * per_sub, 1 << 20)
            report, rows, summary = self.run_pass(name, budget=budget)
            results.append((rows, summary))
            self.assertEqual(report.passes, -(-Q // per_block))
        for rows, summary in results[1:]:
            self.assertEqual(rows, results[0][0])
            self.assertEqual(summary, results[0][1])

    def test_the_results_rows_hold_the_summary_of_the_scope_and_the_extremes_of_the_entries(self):
        want = bayes_diag.measures(self.full[3:])
        report, rows, _ = self.run_pass()
        by_key = dict(zip(rows["key"], rows["value"]))
        self.assertEqual(by_key["summary|mu||mean"], want["mean"][0])
        self.assertEqual(by_key["summary|rho|b,a|rhat"], want["rhat"][4])
        alpha = slice(5, Q)
        self.assertEqual(by_key["quantity|alpha||rhat_max"], np.nanmax(want["rhat"][alpha]))
        self.assertEqual(by_key["quantity|alpha||ess_bulk_min"], np.nanmin(want["ess_bulk"][alpha]))
        self.assertEqual(by_key["summary|alpha|s2|q50"], want["q50"][7])
        self.assertEqual(by_key["model|||n_in_summary"], Q)
        self.assertNotIn("model|||note_summary_capped", by_key)
        self.assertEqual(report.in_summary, Q)

    def test_the_scope_is_capped_and_the_cap_noted(self):
        report, rows, summary = self.run_pass(limits={"bayes_max_summary_quantities": 6})
        self.assertEqual(report.in_summary, 6)
        self.assertEqual(len(summary["quantity"]), Q)              # the summary output has them all
        in_report = {(l, l2) for s, l, l2 in zip(rows["section"], rows["level"], rows["level_2"]) if s == "summary"}
        self.assertEqual(len(in_report), 6)
        self.assertIn("model|||note_summary_capped", rows["key"])
        text = rows["text"][rows["key"].index("model|||note_summary_capped")]
        self.assertIn("6 of 14", text)

    def test_the_prior_predictive_mode_writes_the_mean_the_sd_and_the_quantiles_only(self):
        report, rows, summary = self.run_pass(diagnostics=False)
        stats = {s for sec, s in zip(rows["section"], rows["stat"]) if sec == "summary"}
        self.assertEqual(stats, set(bayes_diag.NUMBERS[:7]))
        self.assertTrue(all(v is None for v in summary["rhat"]))
        self.assertFalse([k for k in rows["key"] if k.startswith("quantity|") and "rhat_max" in k])


class TestTheContraction(Base):
    """Lot X4: 1 − Var_post/Var_prior (Betancourt 2018), NULL when the prior has no finite variance or no draws."""

    def prior_variance(self, position):
        name = self.layout.draws[position].quantity
        if name == "tau":
            return P2.NO_FINITE_VARIANCE                       # a Cauchy's marginal
        if name == "beta":
            return P2.NO_PRIOR_DRAWS
        return 25.0

    def test_the_contraction_is_one_minus_the_variance_ratio_and_null_where_the_prior_has_none(self):
        want = bayes_diag.measures(self.full[3:])
        report, rows, summary = self.run_pass(prior_variance=self.prior_variance)
        names = summary["quantity"]
        for i, name in enumerate(names):
            got = summary["contraction"][i]
            if name in ("tau", "beta"):
                self.assertIsNone(got, name)
            else:
                self.assertEqual(got, 1.0 - want["sd"][i] ** 2 / 25.0, name)
        by_key = dict(zip(rows["key"], rows["value"]))
        self.assertIsNone(by_key["summary|tau||contraction"])
        self.assertEqual(by_key["summary|mu||contraction"], 1.0 - want["sd"][0] ** 2 / 25.0)
        self.assertEqual(report.contraction, {"computed": Q - 2, P2.NO_FINITE_VARIANCE: 1, P2.NO_PRIOR_DRAWS: 1})
        text = rows["text"][rows["key"].index("model|||note_contraction_null")]
        self.assertIn("the prior has no finite variance", text)
        self.assertIn("no draws of the prior", text)

    def test_without_prior_variances_every_contraction_is_null_with_its_note(self):
        report, rows, summary = self.run_pass()
        self.assertEqual(summary["contraction"], [None] * Q)
        self.assertEqual(report.contraction, {P2.NO_PRIOR_DRAWS: Q})
        self.assertIn("model|||note_contraction_null", rows["key"])

    def test_the_variance_of_the_priors_draws_and_the_graphs_decision(self):
        draws = np.array([0.0, 1.0, 2.0, 3.0])
        self.assertEqual(P2.prior_variance_of(draws, True), float(np.var(draws, ddof=1)))
        self.assertEqual(P2.prior_variance_of(draws, False), P2.NO_FINITE_VARIANCE)
        self.assertEqual(P2.prior_variance_of(None, True), P2.NO_PRIOR_DRAWS)
        self.assertEqual(P2.prior_variance_of(np.ones(5), True), P2.NO_PRIOR_DRAWS)

    def test_the_prior_predictive_mode_has_no_contraction(self):
        report, rows, summary = self.run_pass(diagnostics=False, prior_variance=self.prior_variance)
        self.assertEqual(summary["contraction"], [None] * Q)
        self.assertFalse([k for k in rows["key"] if k.endswith("|contraction")])
        self.assertNotIn("model|||note_contraction_null", rows["key"])


class TestTheOrderAndTheReads(Base):
    def test_a_file_in_another_order_than_the_manifest_is_refused_and_leaves_no_summary(self):
        self.layout = fake_draws.make_layout(Q, shuffle=True)
        w = bayes_rows.ResultsWriter(str(self.dir / "r.parquet"))
        with self.assertRaises(ManifestMismatch):
            P2.second_pass(str(self.path), self.layout, {}, w, summary_path=str(self.dir / "s.parquet"))
        self.assertFalse((self.dir / "s.parquet").exists())
        w.abort()

    def test_after_the_orders_check_only_the_value_column_is_read(self):
        calls = []
        real = pq.ParquetFile.read_row_group

        def spy(self_, i, columns=None, **kw):
            calls.append((i, tuple(columns or ())))
            return real(self_, i, columns=columns, **kw)
        with mock.patch.object(pq.ParquetFile, "read_row_group", spy):
            self.run_pass(budget=P2.Budget(8 * CHAINS * DRAWS * 4, 1 << 20, 1 << 20))
        self.assertEqual(calls[0], (0, ("quantity",)))
        self.assertTrue(all(c == ("value",) for _, c in calls[1:]))
        self.assertGreater(len(calls), 1 + 3 * len(pq.ParquetFile(self.path).metadata.to_dict()["row_groups"]) - 1)

    def test_too_slow_is_raised_before_any_row_of_the_pass_and_removes_the_summary(self):
        w = bayes_rows.ResultsWriter(str(self.dir / "r.parquet"))
        with self.assertRaises(Refusal) as caught:
            P2.second_pass(str(self.path), self.layout, {}, w, summary_path=str(self.dir / "s.parquet"), seconds_left=0.0)
        self.assertEqual(caught.exception.code, "too_slow")
        self.assertEqual(len(w.rows) + w.total, 0)
        self.assertFalse((self.dir / "s.parquet").exists())
        w.abort()


class TestTheCharts(Base):
    def rows(self, **kw):
        report, rows, _ = self.run_pass(**kw)
        return report, [dict(zip(rows, v)) for v in zip(*rows.values())]

    def test_trace_rank_and_density_for_the_first_twelve_quantities(self):
        report, rows = self.rows()
        self.assertEqual(len(report.charts["trace"]), 12)
        self.assertEqual(report.charts["trace"][:5], ["mu", "tau", "beta", "delta", "rho[b,a]"])
        trace = [r for r in rows if r["level"] == "trace:mu" and r["level_2"] == "3"]
        self.assertEqual(len(trace), DRAWS)                      # 120 draws a chain are under the 200 points: all of them
        self.assertEqual(trace[-1]["x0"], DRAWS)
        np.testing.assert_array_equal([r["value"] for r in trace], self.full[3, 2])
        capped = self.rows(limits={"bayes_trace_points": 25})[1]
        t = [r for r in capped if r["level"] == "trace:mu" and r["level_2"] == "1"]
        self.assertEqual(len(t), 25)
        self.assertEqual((t[0]["x0"], t[-1]["x0"]), (1, DRAWS))
        hist = [r for r in rows if r["level"] == "rank:tau"]
        self.assertEqual({r["level_2"] for r in hist}, {"1", "2", "3", "4"})
        self.assertEqual(sum(r["value"] for r in hist), CHAINS * DRAWS)
        for chain in "1234":
            self.assertEqual(len([r for r in hist if r["level_2"] == chain]), 20)
        self.assertEqual(hist[0]["x0"], 1.0)
        self.assertEqual(hist[19]["x1"], CHAINS * DRAWS)
        dens = [r for r in rows if r["level"] == "density:mu"]
        self.assertEqual({r["level_2"] for r in dens}, {"posterior"})       # no prior given: no prior series
        self.assertEqual(len(dens), 200)
        x, y = np.array([r["x0"] for r in dens]), np.array([r["value"] for r in dens])
        self.assertTrue(0.9 < np.trapezoid(y, x) < 1.0)                       # the 0.5-99.5 % range holds about 99 % of the mass
        self.assertTrue(all(r["level"].split(":")[1] in report.charts["trace"] for r in rows if r["section"] == "hist"))

    def test_the_prior_series_and_the_cut(self):
        prior = lambda position: np.random.default_rng(3).normal(size=500) * 5 + 3.5 if position == 3 else None   # noqa: E731
        report, rows = self.rows(prior=prior)
        dens = [r for r in rows if r["level"] == "density:mu"]
        self.assertEqual({r["level_2"] for r in dens}, {"posterior", "prior"})
        self.assertEqual(report.charts["density"].count("mu"), 1)
        self.assertEqual([r for r in rows if r["level"] == "density:tau" and r["level_2"] == "prior"], [])
        self.assertIn("mu", report.density_cut)
        self.assertTrue([r for r in rows if r["key"] == "model|||note_density_cut"])

    def test_the_pairs_hold_the_divergent_draws_in_their_own_series(self):
        report, rows = self.rows()
        self.assertEqual(sorted(report.charts["pairs"]), sorted(f"{a}:{b}" for i, a in enumerate(["mu", "tau", "beta", "delta"])
                                                                for b in ["mu", "tau", "beta", "delta"][i + 1:]))
        pair = [r for r in rows if r["level"] == "pairs:mu:tau"]
        n_draw = len([r for r in pair if r["level_2"] == "draw"])
        n_div = len([r for r in pair if r["level_2"] == "divergent"])
        self.assertEqual(n_draw + n_div, 300)                                 # S = 480 pooled draws, 300 evenly spaced
        index = bayes_charts.thin_indices(CHAINS * DRAWS, 300)
        flags = np.array([(d % 17) == 0 for _ in range(CHAINS) for d in range(1, DRAWS + 1)])[index]
        self.assertEqual(n_div, int(flags.sum()))
        self.assertGreater(n_div, 0)
        mu, tau = self.full[3].reshape(-1)[index], self.full[4].reshape(-1)[index]
        got = sorted((r["x0"], r["value"]) for r in pair if r["level_2"] == "divergent")
        self.assertEqual(got, sorted(zip(mu[flags], tau[flags])))
        self.assertEqual(len(self.rows(limits={"bayes_max_pair_quantities": 2})[0].charts["pairs"]), 1)

    def test_the_units_chart_from_the_summary_and_a_unit_named_with_a_bar(self):
        layout = fake_draws.make_layout(Q)
        layout.draws[10] = type(layout.draws[10])("alpha[a|b]", "alpha", "a|b", 3, None)
        self.layout = layout
        fake_draws.write(self.path, layout, CHAINS, DRAWS, 50)
        want = bayes_diag.measures(fake_draws.write(self.path, layout, CHAINS, DRAWS, 50, keep=True)[3:])
        report, rows = self.rows()
        self.assertEqual(report.charts["units"], ["alpha"])
        med = [r for r in rows if r["level"] == "units:alpha" and r["level_2"] == "median"]
        self.assertEqual(len(med), Q - 5)
        by_text = {r["text"]: r for r in med}
        self.assertEqual(by_text["a|b"]["value"], want["q50"][7])
        self.assertEqual(by_text["a|b"]["key"].rsplit("|", 1)[0], "point|units:alpha|median|y")
        self.assertEqual(sorted(r["x0"] for r in med), list(range(1, Q - 4)))
        self.assertTrue([r for r in rows if r["key"].startswith("summary|alpha|a|b|mean")])      # the key as the engine joins it
        low = {r["text"]: r["value"] for r in rows if r["level"] == "units:alpha" and r["level_2"] == "low"}
        self.assertEqual(low["a|b"], want["q5"][7])

    def test_no_row_holds_a_threshold_or_a_verdict_word(self):
        _, rows, summary = self.run_pass(prior=lambda position: np.random.default_rng(3).normal(size=500) if position == 3 else None)
        words = re.compile(r"\b(good|bad|pass(ed|es)?|fail(ed|s)?|ok|converge[sd]?|convergence|warning|threshold|problem|healthy|"
                           r"reliable|acceptable|too high|too low|should|must)\b", re.I)
        for column in ("section", "level", "level_2", "stat", "text", "key"):
            for v in rows[column]:
                self.assertIsNone(words.search(v or ""), f"{column}: {v}")
        self.assertFalse(set(summary) & {"verdict", "threshold", "flag"})


class TestTheChartFunctions(unittest.TestCase):
    def test_thinning_is_even_and_keeps_the_last(self):
        self.assertEqual(bayes_charts.thin_indices(5, 10).tolist(), [0, 1, 2, 3, 4])
        i = bayes_charts.thin_indices(1000, 200)
        self.assertEqual((len(i), i[0], i[-1]), (200, 0, 999))
        self.assertTrue(np.all(np.diff(i) > 0))
        self.assertEqual(bayes_charts.thin_indices(1000, 1).tolist(), [999])

    def test_the_rank_histogram_counts_every_draw_once(self):
        x = np.random.default_rng(0).normal(size=(3, 50))
        x[0, :10] = 1.0                                       # ties
        counts, edges = bayes_charts.rank_histogram(x, 7)
        self.assertEqual(counts.sum(), 150)
        self.assertEqual((edges[0], edges[-1]), (1.0, 150.0))
        one, _ = bayes_charts.rank_histogram(np.arange(20.0).reshape(1, 20), 4)
        self.assertEqual(one[0].tolist(), [5, 5, 5, 5])

    def test_the_kernel_density_is_silvermans_sum_over_all_draws(self):
        x = np.random.default_rng(1).normal(size=400)
        d = bayes_charts.density(x, None, 50)
        s = np.sort(x)
        h = 0.9 * min(np.std(x, ddof=1), (np.percentile(x, 75) - np.percentile(x, 25)) / 1.34) * 400 ** -0.2
        direct = np.exp(-0.5 * ((d.grid[:, None] - x[None, :]) / h) ** 2).sum(axis=1) / (400 * h * np.sqrt(2 * np.pi))
        np.testing.assert_allclose(d.posterior, direct, rtol=1e-12)
        self.assertIsNone(bayes_charts.density(np.full(30, 2.0), None, 50))

    def test_the_units_selection_keeps_the_ten_most_extreme_and_is_seeded(self):
        m = np.random.default_rng(2).normal(size=500)
        m[17], m[301] = 50.0, -60.0
        a = bayes_charts.unit_selection(m, 100, 7)
        self.assertEqual(len(a), 100)
        self.assertTrue({17, 301} <= set(a.tolist()))
        np.testing.assert_array_equal(a, bayes_charts.unit_selection(m, 100, 7))
        self.assertFalse(np.array_equal(a, bayes_charts.unit_selection(m, 100, 8)))
        self.assertEqual(len(bayes_charts.unit_selection(m, 500, 7)), 500)


class TestTheSamplerSection(unittest.TestCase):
    def test_the_rows_of_the_facts_and_the_times(self):
        with tempfile.TemporaryDirectory() as d:
            w = bayes_rows.ResultsWriter(str(Path(d) / "r.parquet"))
            facts = {1: {"draws": 10, "divergences": 2, "treedepth_hits": 0, "treedepth_max": 4, "e_bfmi": 0.9, "stepsize": 0.2,
                         "accept_stat_mean": 0.8, "n_leapfrog_mean": 7.0},
                     "all": {"draws": 10, "divergences": 2, "treedepth_hits": 0, "treedepth_max": 4, "e_bfmi": None, "stepsize": 0.2,
                             "accept_stat_mean": 0.8, "n_leapfrog_mean": 7.0}}
            P2.sampler_section(w, facts, {1: (1.5, 3.5), 2: (2.0, 3.0)})
            by = dict(zip(w.rows.key, w.rows.value))
            self.assertEqual(by["sampler|1||divergences"], 2)
            self.assertEqual(by["sampler|all||seconds_warmup"], 2.0)
            self.assertEqual(by["sampler|1||seconds_sampling"], 3.5)
            self.assertNotIn("sampler|all||e_bfmi", by)
            w.abort()


if __name__ == "__main__":
    unittest.main()
