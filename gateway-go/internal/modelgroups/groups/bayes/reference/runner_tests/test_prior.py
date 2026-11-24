"""The prior predictive Job (lot R3 of docs/specs/bayes-runner.md): `fixed_param` with `Mode` 1 and the checks on, the draws' summary of the
ancestral draws (no diagnostics, no charts of the quantities), the `prior_check` rows and densities, the quantities without a prior draw left
out and said, a draw that threw summarised over the finite ones, the refusals before any result. On the fake CmdStan, small data only: a few
dozen rows, forty draws a chain."""
import json
import shutil
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py"), str(HERE)]

import numpy as np  # noqa: E402

import bayes_follow  # noqa: E402
import bayes_main  # noqa: E402
import bayes_run  # noqa: E402
import bayes_task  # noqa: E402
from tab_protocol import Refusal  # noqa: E402
from test_run import RunCase, engine_check, golden_task, linear_input, means_by_name  # noqa: E402

DRAWS = 40


def prior_task(name="glm_linear"):
    """A golden program as a prior predictive: its extension, the columns bound by their Stan names, 4 chains of 40 draws and no warm-up."""
    raw = golden_task(name)
    raw["mode"] = raw["variant"] = "prior_predictive"
    raw["params"] = {"mode": "prior_predictive", "output": "results"}
    raw["output"], raw["write_output"], raw["rows_from"] = "results", False, "trial"
    raw["bayes"]["sampler"].update({"warmup": 0, "draws": DRAWS})
    raw["bayes"]["checks"]["on"] = True
    raw["bayes"]["replicated"] = None
    return raw


def hier_input(n=30):
    rng = np.random.default_rng(9)
    return {"g": [f"s{i % 3 + 1}" for i in range(n)], "x": rng.normal(size=n).tolist(), "y": rng.normal(size=n).tolist()}


class PriorCase(RunCase):
    def program_header(self, raw, columns):
        """The program's columns of the prior predictive's CSV: the prior draws in place of the quantities', the checks', the marginals'."""
        scratch = self.dir / "scratch"
        task = bayes_task.parse_task(json.loads(json.dumps(raw)))
        prepared = bayes_main.prepare_run(task, scratch, self.write_input(columns, "scratch.parquet"),
                                          checks=bayes_run.bayes_checks.plan(task), run="prior")
        header = prepared.layout.header[len(bayes_task.SAMPLER_COLUMNS_FIXED_PARAM):]
        shutil.rmtree(scratch)
        return header

    def prior_job(self, raw, columns, spec=None, **kw):
        header = self.program_header(raw, columns)
        full = {"fixed_param": True, "warmup": 0, **(spec or {})}
        return self.job(raw, columns, spec=full, output="results", write_output=False,
                        means=lambda h: means_by_name(h), **kw), header

    def results(self):
        cols = engine_check(self.out / "results" / "results.parquet", "prior_predictive")
        return cols, dict(zip(cols["key"], zip(cols["value"], cols["text"])))


class TestThePriorPredictiveJob(PriorCase):
    def test_a_prior_predictive_writes_the_summary_the_checks_and_nothing_of_the_diagnostics(self):
        seen = {}
        real = bayes_follow.start

        def start(args, workdir):
            seen["args"] = list(args)
            seen["data"] = json.loads((workdir / "data.json").read_text())
            return real(args, workdir)

        with mock.patch.object(bayes_follow, "start", start):
            code, header = self.prior_job(prior_task(), linear_input())
        self.assertEqual(code, 0)
        self.assertFalse(self.work.exists())
        self.assertIn("algorithm=fixed_param", seen["args"])
        self.assertIn("num_warmup=0", seen["args"])
        self.assertEqual((seen["data"]["Mode"], seen["data"]["Ppc_on"]), (1, 1))
        self.assertGreater(seen["data"]["Prior_o1_n"], 0)
        # no output, no card: the results and run.json alone
        self.assertEqual(self.left_behind(), ["artifacts/run.json", "results/results.parquet"])
        cols, rows = self.results()
        keys = set(cols["key"])
        for q in ("alpha", "b_x", "sigma"):
            for stat in ("mean", "sd", "q5", "q25", "q50", "q75", "q95"):
                self.assertIn(f"summary|{q}||{stat}", keys)
        self.assertEqual({s for s in cols["section"]} - {"model", "quantity", "summary", "data", "prior_check", "curve"}, set())
        self.assertFalse([k for k in keys if "|rhat" in k or "ess_" in k or "mcse" in k or "contraction" in k])
        self.assertIn("prior_check|y:mean||observed", keys)
        self.assertIn("prior_check|y:mean||count|0", keys)
        self.assertIn("curve|prior_density:y|observed|value|0", keys)
        self.assertFalse([k for k in keys if k.startswith(("ppc|", "hist|", "point|", "sampler|")) or "trace:" in k or "density:" in k and "prior_density" not in k])
        self.assertEqual(rows["model|||variant"][1], "prior_predictive")
        self.assertEqual(rows["model|||sampler"][1], "fixed_param")
        self.assertNotIn("model|||metric", rows)
        self.assertGreaterEqual(rows["model|||seconds_checks"][0], 0.0)
        self.assertNotIn("model|||seconds_warmup", rows)
        self.assertEqual(rows["model|||n_in_summary"][0], 3)
        self.assertNotIn("model|||note_prior_undrawn", rows)
        self.assertNotIn("model|||note_prior_dropped", rows)
        run = json.loads((self.out / "artifacts" / "run.json").read_text(encoding="utf-8"))
        self.assertEqual((run["mode"], run["exit"]), ("prior_predictive", 0))

    def test_the_summary_is_of_the_prior_draws_the_checks_are_not_the_quantities(self):
        code, header = self.prior_job(prior_task(), linear_input())
        self.assertEqual(code, 0)
        cols, rows = self.results()
        expected = dict(zip(header, means_by_name(header)))
        # the fake draws each column around its mean: the summary's mean is that of the prior draw's column (Prior_d_1 → alpha)
        for q, prior in (("alpha", "Prior_d_1"), ("b_x", "Prior_d_2"), ("sigma", "Prior_d_3")):
            self.assertAlmostEqual(rows[f"summary|{q}||mean"][0], expected[prior], delta=0.6, msg=q)

    def test_a_quantity_without_a_prior_draw_is_left_out_and_said(self):
        columns = hier_input()
        raw = prior_task("hier_slopes")
        code, header = self.prior_job(raw, columns)
        self.assertEqual(code, 0)
        cols, rows = self.results()
        names = {k.split("|")[1] for k in cols["key"] if k.startswith(("summary|", "quantity|"))}
        self.assertNotIn("rho_g", names)
        self.assertFalse([n for n in names if n.startswith("rho_g[")])
        self.assertIn("a_g", names)
        self.assertEqual(rows["model|||n_quantities"][0], rows["model|||n_in_summary"][0])
        text = rows["model|||note_prior_undrawn"][1]
        self.assertIn("1 quantity has no ancestral draw", text)
        self.assertIn("rho_g", text)
        # the others are the prior draws of the group plate: a_g[s1]…
        self.assertIn("summary|a_g|s1|mean", cols["key"])

    def test_a_draw_that_threw_is_left_out_of_the_summary_of_its_quantities_and_counted(self):
        raw = prior_task()
        columns = linear_input()
        header = self.program_header(raw, columns)
        i = header.index("Prior_d_1") + 1
        spec = {"special": [[1, 3, i, "nan"], [2, 7, i, "inf"]]}
        code, _ = self.prior_job(raw, columns, spec=spec)
        self.assertEqual(code, 0)
        cols, rows = self.results()
        self.assertIn("2 of the 160 prior draws", rows["model|||note_prior_dropped"][1])
        self.assertIsNotNone(rows["summary|alpha||mean"][0])
        self.assertIsNotNone(rows["summary|alpha||sd"][0])

    def test_the_prior_predictive_of_a_program_without_one_is_refused_before_the_binary(self):
        raw = prior_task()
        raw["bayes"]["data"] = [e for e in raw["bayes"]["data"] if e["kind"] not in ("prior_count", "prior_rows")]
        with mock.patch.object(bayes_main.bayes_binary, "fetch_binary") as fetch, self.assertRaises(Refusal) as e:
            bayes_main.run_task(bayes_task.parse_task(raw), HERE, self.work)
        self.assertEqual(e.exception.code, "task_value")
        self.assertIn("no prior predictive", e.exception.message)
        fetch.assert_not_called()
        self.assertFalse(self.work.exists())

    def test_a_refusal_leaves_nothing(self):
        raw = prior_task()
        columns = linear_input()
        with mock.patch.object(bayes_run.SampleJob, "too_slow", side_effect=Refusal("too_slow", "x")), \
                mock.patch.object(bayes_run.SampleJob, "later_seconds", return_value=1e9):
            with self.assertRaises(Refusal) as e:
                self.prior_job(raw, columns)
        self.assertEqual(e.exception.code, "too_slow")
        self.assert_nothing_left()


if __name__ == "__main__":
    unittest.main()
