"""The predictive checks' second pass (lot P3b of docs/specs/bayes-predictive-checks.md): the `ppc` / `prior_check` rows and the overlaid
densities from a synthetic checks file, on small tables. The statistics' histograms against numpy on the same draws, T(y) inside every
histogram, the units' means, the evenly spaced draws' densities, the draws that threw (all NaN) counted and left out, the blocks of the
file not changing a byte, the declaration's check, and no row that says more than a count or a curve."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py"), str(HERE)]

import numpy as np  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import bayes_charts  # noqa: E402
import bayes_ppc  # noqa: E402
import bayes_rows  # noqa: E402
import fake_checks  # noqa: E402
from bayes_task import ManifestMismatch  # noqa: E402
from tab_protocol import Refusal  # noqa: E402
from test_run import CONTRACT, engine_check  # noqa: E402

FORBIDDEN = ("p_value", "pvalue", "p-value", "probab", "share", "pass", "fail", "reject", "good", "bad", "fit", "verdict", "significan", "bayes_p")


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def writer(self, variant="sample"):
        shapes = {**json.loads((HERE.parents[6] / "backend" / "app" / "engine" / "models" / "catalog.json").read_text(encoding="utf-8"))
                  ["section_shapes"], **CONTRACT["shapes"]}
        return bayes_rows.ResultsWriter(str(self.dir / "results.parquet"), declaration=CONTRACT["variants"][variant], shapes=shapes)

    def run_rows(self, kind="continuous", units=True, chains=2, draws=40, block_draws=7, nan_draws=(), section="ppc", n=60, keep=True, **kw):
        task, plan, facts, layout = fake_checks.prepared(self.dir, n=n, kind=kind, units=units, run="checks" if section == "ppc" else "prior")
        path = self.dir / "checks.parquet"
        record = fake_checks.write(path, plan, layout, kind, chains, draws, block_draws, nan_draws=nan_draws, keep=keep)
        variant = "sample" if section == "ppc" else "prior_predictive"
        w = self.writer(variant)
        w.text("model", "model", "bayes_model")
        w.text("model", "variant", variant)
        w.number("model", "n", 60)
        report = plan.rows(str(path), w, section, **kw)
        w.close()
        rows = {}
        for r in pq.read_table(self.dir / "results.parquet").to_pylist():
            rows[r["key"]] = r
        return plan, report, rows, record, w


class TestRows(Case):
    def test_the_histograms_hold_the_draws_and_t_of_y_and_match_numpy(self):
        plan, report, rows, rec, _ = self.run_rows()
        o = plan.outcomes[0]
        self.assertEqual((report.draws, report.valid, report.dropped), (80, 80, 0))
        for s in o.statistics:
            self.assertAlmostEqual(rows[f"ppc|y:{s}||observed"]["value"], o.observed[s], places=12)
            draws = rec["stats"][s]
            lo, hi = min(draws.min(), o.observed[s]), max(draws.max(), o.observed[s])
            want, edges = np.histogram(draws, bins=30, range=(lo, hi))
            got = [rows[f"ppc|y:{s}||count|{i}"] for i in range(30)]
            self.assertNotIn(f"ppc|y:{s}||count|30", rows)
            self.assertEqual([int(g["value"]) for g in got], want.tolist(), s)
            np.testing.assert_allclose([g["x0"] for g in got], edges[:-1], atol=1e-12)
            np.testing.assert_allclose([g["x1"] for g in got], edges[1:], atol=1e-12)
            self.assertLessEqual(got[0]["x0"], o.observed[s])                          # the histogram holds T(y)
            self.assertGreaterEqual(got[-1]["x1"], o.observed[s])
            self.assertEqual(sum(g["value"] for g in got), 80)

    def test_the_checked_units_get_their_means(self):
        plan, report, rows, rec, _ = self.run_rows()
        o = plan.outcomes[0]
        self.assertEqual(len(o.unit_texts), 8)
        means = rec["usum"][:, :8] / o.unit_rows[:8]
        for j, unit in enumerate(o.unit_texts):
            self.assertAlmostEqual(rows[f"ppc|y:unit_mean|{unit}|observed"]["value"], o.unit_means()[j], places=12)
            lo, hi = min(means[:, j].min(), o.unit_means()[j]), max(means[:, j].max(), o.unit_means()[j])
            want, _ = np.histogram(means[:, j], bins=30, range=(lo, hi))
            got = [int(rows[f"ppc|y:unit_mean|{unit}|count|{i}"]["value"]) for i in range(30)]
            self.assertEqual(got, want.tolist(), unit)
        self.assertFalse(any(k.startswith("ppc|y:unit_mean|") and "|" + str(o.units) + "|" in k for k in rows))
        # the others' slot is never drawn: no row for it
        self.assertEqual(len({r["level_2"] for r in rows.values() if r["level"] == "y:unit_mean"}), 8)

    def test_the_densities_observed_and_twenty_evenly_spaced_draws(self):
        plan, report, rows, rec, _ = self.run_rows()
        o = plan.outcomes[0]
        slots = o.bins.slots
        series = {}
        for key, r in rows.items():
            if key.startswith("curve|ppc_density:y|"):
                series.setdefault(r["level_2"], []).append(r)
        self.assertEqual(sorted(series, key=lambda s: (s != "observed", len(s), s)), ["observed"] + [f"draw_{j}" for j in range(1, 21)])
        edges = o.bins.edges()
        x = [edges[0]] + list((edges[:-1] + edges[1:]) / 2) + [edges[-1]]
        scale = 1.0 / (o.n * o.bins.width)
        self.assertEqual(len(series["observed"]), slots)
        np.testing.assert_allclose([r["x0"] for r in series["observed"]], x)
        np.testing.assert_allclose([r["value"] for r in series["observed"]], o.counts * scale)
        chosen = bayes_charts.thin_indices(80, 20)
        for j, d in enumerate(chosen, 1):
            np.testing.assert_allclose([r["value"] for r in series[f"draw_{j}"]], rec["counts"][d] * scale, err_msg=f"draw_{j}")
        self.assertEqual(report.charts, ["ppc_density:y"])
        self.assertEqual(report.rows, len([k for k in rows if k.startswith(("ppc|", "curve|"))]))
        # the outcome's own number of rows, for the report's count of the drawn values outside the data's range (lot Q4)
        self.assertEqual(rows["ppc|y||n"]["value"], float(o.n))
        self.assertEqual(rows["ppc|y||n"]["level_2"], None)

    def test_draws_that_threw_are_counted_left_out_and_said(self):
        nan = ((1, 3), (2, 10), (2, 11))
        plan, report, rows, rec, _ = self.run_rows(nan_draws=nan)
        self.assertEqual((report.draws, report.valid, report.dropped), (80, 77, 3))
        ok = rec["valid"]
        for s in ("mean", "q90"):
            draws = rec["stats"][s][ok]
            lo, hi = min(draws.min(), plan.outcomes[0].observed[s]), max(draws.max(), plan.outcomes[0].observed[s])
            want, _ = np.histogram(draws, bins=30, range=(lo, hi))
            self.assertEqual([int(rows[f"ppc|y:{s}||count|{i}"]["value"]) for i in range(30)], want.tolist())
        # the evenly spaced draws are those of the valid draws, in the chain-major order
        scale = 1.0 / (plan.outcomes[0].n * plan.outcomes[0].bins.width)
        valid_counts = rec["counts"][ok]
        for j, d in enumerate(bayes_charts.thin_indices(77, 20), 1):
            np.testing.assert_allclose([r["value"] for k, r in rows.items() if k.startswith(f"curve|ppc_density:y|draw_{j}|")],
                                       valid_counts[d] * scale)
        note = rows["model|||note_checks_dropped"]["text"]
        self.assertEqual(note, "@bayes_checks_dropped dropped=3 draws=80 valid=77")

    def test_no_note_without_dropped_draws_and_all_dropped_leaves_the_observed_rows(self):
        _, _, rows, _, _ = self.run_rows()
        self.assertNotIn("model|||note_checks_dropped", rows)
        every = tuple((c, d) for c in (1, 2) for d in range(1, 11))
        _, report, rows, _, _ = self.run_rows(chains=2, draws=10, nan_draws=every)
        self.assertEqual((report.valid, report.dropped), (0, 20))
        self.assertIn("ppc|y:mean||observed", rows)
        self.assertFalse(any("|count|" in k for k in rows if k.startswith("ppc|")))
        self.assertIn("model|||note_checks_dropped", rows)
        self.assertEqual(len([k for k in rows if k.startswith("curve|") and "|observed|" in k]), 42)
        self.assertFalse(any("|draw_" in k for k in rows))

    def test_the_blocks_of_the_file_do_not_change_a_byte(self):
        outs = []
        for block_draws in (1, 7, 40):
            self.tearDown()
            self.setUp()
            _, _, rows, _, _ = self.run_rows(block_draws=block_draws, nan_draws=((2, 5),), keep=False)
            outs.append(json.dumps({k: [r["value"], r["x0"], r["x1"], r["level_2"], r["text"]] for k, r in rows.items()}, sort_keys=True))
        self.assertEqual(outs[0], outs[1])
        self.assertEqual(outs[1], outs[2])

    def test_counts_and_binary_outcomes_and_an_outcome_without_a_group(self):
        for kind in ("counts", "binary"):
            self.tearDown()
            self.setUp()
            plan, report, rows, rec, _ = self.run_rows(kind=kind, units=False, chains=2, draws=15, block_draws=4)
            o = plan.outcomes[0]
            engine_check(self.dir / "results.parquet")
            self.assertFalse(any("unit_mean" in k for k in rows), kind)
            for s in o.statistics:
                got = [rows.get(f"ppc|y:{s}||count|{i}") for i in range(30)]
                self.assertEqual(sum(g["value"] for g in got), 30, (kind, s))
                self.assertLessEqual(got[0]["x0"], o.observed[s])
                self.assertGreaterEqual(got[-1]["x1"], o.observed[s])
            curve = [r for k, r in rows.items() if k.startswith("curve|ppc_density:y|observed|")]
            self.assertEqual(len(curve), o.bins.slots)
        self.assertEqual(plan.outcomes[0].bins.slots, 4)                           # binary: the two values and the two outer slots

    def test_the_constant_statistic_is_widened_and_holds_t_of_y(self):
        task, plan, facts, layout = fake_checks.prepared(self.dir, n=30, kind="binary", units=False)
        path = self.dir / "checks.parquet"
        fake_checks.write(path, plan, layout, "binary", 1, 6, 6)
        o = plan.outcomes[0]
        o.observed["mean"] = 0.5
        w = self.writer()
        w.text("model", "model", "bayes_model")
        w.text("model", "variant", "sample")
        w.number("model", "n", 30)
        plan.rows(str(path), w, "ppc")
        w.close()
        rows = {r["key"]: r for r in pq.read_table(self.dir / "results.parquet").to_pylist()}
        counts = [rows[f"ppc|y:mean||count|{i}"] for i in range(30)]
        self.assertLessEqual(counts[0]["x0"], 0.5)
        self.assertGreaterEqual(counts[-1]["x1"], 0.5)
        self.assertEqual(sum(c["value"] for c in counts), 6)

    def test_the_prior_predictive_writes_prior_check_and_prior_density(self):
        plan, report, rows, rec, w = self.run_rows(section="prior_check", chains=4, draws=10)
        self.assertEqual(report.charts, ["prior_density:y"])
        self.assertIn("prior_check|y:mean||observed", rows)
        self.assertTrue(any(k.startswith("curve|prior_density:y|draw_20|") for k in rows))
        self.assertFalse(any(k.startswith("ppc|") or "ppc_density" in k for k in rows))
        engine_check(self.dir / "results.parquet", "prior_predictive")

    def test_the_results_pass_the_engines_check_and_say_nothing_but_counts_and_curves(self):
        self.tearDown()
        self.setUp()
        task, plan, facts, layout = fake_checks.prepared(self.dir)
        path = self.dir / "checks.parquet"
        fake_checks.write(path, plan, layout, "continuous", 2, 30, 8, nan_draws=((1, 2),))
        w = self.writer()
        w.text("model", "model", "bayes_model")
        w.text("model", "variant", "sample")
        w.number("model", "n", 60)
        plan.rows(str(path), w, "ppc")
        w.close()
        cols = engine_check(self.dir / "results.parquet")
        stats = {(s, st) for s, st in zip(cols["section"], cols["stat"]) if s in ("ppc", "curve")}
        self.assertEqual(stats, {("ppc", "observed"), ("ppc", "count"), ("ppc", "n"), ("curve", "value")})
        words = " ".join(str(v).lower() for column in ("level", "level_2", "stat", "text") for v in cols[column] if v)
        for word in FORBIDDEN:
            self.assertNotIn(word, words.replace("replicated values as missing", ""), word)
        self.assertLessEqual(len([s for s in cols["section"] if s == "ppc"]), 1347)

    def test_the_caps_arithmetic_of_the_spec(self):
        plan, report, rows, _, _ = self.run_rows()
        per_outcome = len([k for k in rows if k.startswith(("ppc|", "curve|"))])
        self.assertEqual(per_outcome, 1 + 7 * 31 + 8 * 31 + 21 * 42)                # 1,348 (the outcome's `n` row, lot Q4), three outcomes 4,044 under bayes_max_check_rows


class TestRefusals(Case):
    def test_a_file_whose_columns_are_not_the_plans_is_a_fault(self):
        task, plan, facts, layout = fake_checks.prepared(self.dir)
        path = self.dir / "checks.parquet"
        fake_checks.write(path, plan, layout, "continuous", 1, 4, 4)
        plan.outcomes[0].statistics.append("q99")
        plan.outcomes[0].observed["q99"] = 1.0
        with self.assertRaises(ManifestMismatch):
            plan.rows(str(path), self.writer(), "ppc")

    def test_too_slow_before_any_row(self):
        task, plan, facts, layout = fake_checks.prepared(self.dir)
        path = self.dir / "checks.parquet"
        fake_checks.write(path, plan, layout, "continuous", 2, 20, 2)
        w = self.writer()
        with self.assertRaises(Refusal) as e:
            plan.rows(str(path), w, "ppc", seconds_left=1e-9)
        self.assertEqual(e.exception.code, "too_slow")
        self.assertEqual(w.total + len(w.rows), 0)

    def test_a_plan_without_outcomes_writes_nothing_and_a_wrong_section_is_a_fault(self):
        task, plan, facts, layout = fake_checks.prepared(self.dir)
        w = self.writer()
        empty = type(plan)(task)
        empty.outcomes = []
        self.assertEqual(empty.rows("/nowhere", w, "ppc").rows, 0)
        with self.assertRaises(ValueError):
            plan.rows("/nowhere", w, "summary")


if __name__ == "__main__":
    unittest.main()
