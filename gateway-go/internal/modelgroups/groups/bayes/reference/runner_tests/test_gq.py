"""The `generate_quantities` runs of a sampling Job (lot R2 of docs/specs/bayes-runner.md): the thinned draws, the data file, the expected header,
the checks' and the replicated outputs, the time and the disk of the runs. On the fake CmdStan (`method=generate_quantities`: it reads the
follower's fitted files and writes the generated columns of the program, as lot P0 measured), small data only: a few dozen rows, forty draws
a chain, ten kept."""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py"), str(HERE)]

import numpy as np  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import bayes_data  # noqa: E402
import bayes_draws  # noqa: E402
import bayes_follow  # noqa: E402
import bayes_gq  # noqa: E402
import bayes_main  # noqa: E402
import bayes_run  # noqa: E402
import bayes_task  # noqa: E402
import fake_cmdstan  # noqa: E402
from tab_protocol import Refusal  # noqa: E402
from test_run import CONTRACT, SAMPLER, RunCase, engine_check, golden_task, linear_input, means_by_name, v1_input, v1_task  # noqa: E402

REPLICATED = {"draws_per_chain": 5}


def prepare(task, workdir, input_path):
    """The data passes as bayes_main.run_task runs them: with the checks' plan (kept on the result), the sampling run's extension values."""
    plan = bayes_run.bayes_checks.plan(task)
    prepared = bayes_main.prepare_run(task, workdir, input_path, checks=plan, run="sample")
    prepared.checks = plan
    return prepared


def checks_task(name="glm_linear", replicated=False, ppc_draws=40, on=True):
    raw = golden_task(name)
    raw["bayes"]["checks"].update({"on": on, "ppc_draws": ppc_draws})
    if replicated:
        raw["output"] = raw["params"]["output"] = "replicated"
        raw["bayes"]["replicated"] = dict(REPLICATED)
    return raw


def fake_lines(spec, chain):
    """The draw lines the fake sampler writes for a chain, in order (its generator is sequential: a line depends on the lines before)."""
    draws = fake_cmdstan.Draws(spec, chain)
    return [draws.line(d) for d in range(1, int(spec["draws"]) + 1)]


class TestTheKeptDraws(unittest.TestCase):
    def test_evenly_spaced_with_the_last_included(self):
        self.assertEqual(bayes_gq.kept_draws(500, 250), list(range(2, 501, 2)))
        self.assertEqual(bayes_gq.kept_draws(40, 10), [4, 8, 12, 16, 20, 24, 28, 32, 36, 40])
        self.assertEqual(bayes_gq.kept_draws(10, 3), [4, 7, 10])
        self.assertEqual(bayes_gq.kept_draws(7, 7), list(range(1, 8)))
        self.assertEqual(bayes_gq.kept_draws(5, 1), [5])
        for draws, kept in ((2000, 250), (333, 250), (10_000, 1), (97, 96)):
            k = bayes_gq.kept_draws(draws, kept)
            self.assertEqual((len(k), k[-1], len(set(k))), (kept, draws, kept))
            self.assertEqual(k, sorted(k))
        with self.assertRaises(ValueError):
            bayes_gq.kept_draws(5, 6)

    def test_the_draws_a_chain_keeps_for_the_checks(self):
        def task(chains, draws, ppc):
            raw = golden_task("glm_linear")
            raw["bayes"]["sampler"].update({"chains": chains, "draws": draws})
            raw["bayes"]["checks"]["ppc_draws"] = ppc
            return bayes_task.parse_task(raw)
        self.assertEqual(bayes_gq.checks_per_chain(task(4, 500, 1000)), 250)
        self.assertEqual(bayes_gq.checks_per_chain(task(3, 500, 1000)), 333)
        self.assertEqual(bayes_gq.checks_per_chain(task(4, 100, 1000)), 100)       # never more than the chain's draws
        self.assertEqual(bayes_gq.checks_per_chain(task(8, 500, 5)), 1)


class TestTheThinnedLines(unittest.TestCase):
    def test_the_files_hold_the_header_and_the_kept_lines_as_they_came(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            header = ["lp__", "accept_stat__", "a", "b"]
            thin = bayes_gq.ThinnedLines(work, header, 2, 10, {"fitted": 3, "fitted_rep": 10})
            lines = {(c, d): f"{c * 1000 + d}.5,0.9,{d}.25,-{d}.125".encode() for c in (1, 2) for d in range(1, 11)}
            for d in range(1, 11):                                  # block-major, chain-minor, as the follower reads
                for c in (1, 2):
                    thin.on_line(c, d, lines[(c, d)])
            thin.close()
            for c in (1, 2):
                text = (work / f"fitted_{c}.csv").read_bytes()
                self.assertEqual(text, b"lp__,accept_stat__,a,b\n" + b"".join(lines[(c, d)] + b"\n" for d in (4, 7, 10)))
                rep = (work / f"fitted_rep_{c}.csv").read_text().splitlines()
                self.assertEqual(len(rep), 11)
            thin.remove("fitted")
            self.assertEqual(sorted(p.name for p in work.iterdir()), ["fitted_rep_1.csv", "fitted_rep_2.csv"])

    def test_one_chain_writes_the_stem_itself_as_cmdstan_names_its_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            thin = bayes_gq.ThinnedLines(Path(tmp), ["x"], 1, 4, {"fitted": 2})
            for d in range(1, 5):
                thin.on_line(1, d, str(d).encode())
            thin.close()
            self.assertEqual((Path(tmp) / "fitted.csv").read_text(), "x\n2\n4\n")

    def test_an_incomplete_chain_is_a_fault_and_an_abort_leaves_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            thin = bayes_gq.ThinnedLines(Path(tmp), ["x"], 2, 4, {"fitted": 2})
            for d in range(1, 5):
                thin.on_line(1, d, b"1")
            with self.assertRaises(ValueError):
                thin.close()
            thin.abort()
            self.assertEqual(list(Path(tmp).iterdir()), [])


class TestTheRunsOfAProgram(RunCase):
    """The expansion of the generated quantities and the data files of the runs, on the golden programs with the checks."""

    def prepared(self, raw, columns, run="sample"):
        task = bayes_task.parse_task(json.loads(json.dumps(raw)))
        plan = bayes_run.bayes_checks.plan(task)
        scratch = self.dir / f"scratch_{run}"
        prepared = prepare(task, scratch, self.write_input(columns, f"in_{run}.parquet"))
        return task, plan, prepared

    def test_the_header_is_the_generated_quantities_of_the_mapping_with_the_run_sizes(self):
        task, plan, prepared = self.prepared(checks_task(replicated=True), linear_input())
        runs = bayes_gq.plan_runs(task, prepared.checks, prepared.facts)
        self.assertEqual([r.name for r in runs], ["checks", "replicated"])
        checks, rep = runs
        names = checks.layout.header
        slots = checks.values["Ppc_o1_slots"]
        prior = ["Prior_m_1", "Prior_d_1", "Prior_m_2", "Prior_d_2", "Prior_m_3", "Prior_d_3"]      # the program's own order; no yrep: Ppc_o1_rep is 0
        self.assertEqual(names[:6], prior)
        self.assertEqual(names[6:13], [f"Ppc_o1_{s}.1" for s in ("mean", "sd", "min", "max", "q10", "q50", "q90")])
        self.assertEqual(names[13:], [f"Ppc_o1_count.{i}" for i in range(1, slots + 1)])
        self.assertEqual(len(checks.layout.checks), 7 + slots)
        self.assertEqual(checks.layout.check_columns.tolist(), list(range(6, len(names))))
        self.assertEqual(checks.layout.checks[0].quantity, "Ppc_o1_mean[1]")
        self.assertEqual(checks.layout.checks[7].quantity, "Ppc_o1_count[1]")
        self.assertEqual(checks.layout.replicated, [])
        n = prepared.facts.n
        self.assertEqual(rep.layout.header, prior + [f"Ppc_o1_yrep.{i}" for i in range(1, n + 1)])
        self.assertEqual([(o, c.tolist()) for o, c in rep.layout.replicated], [("y", list(range(6, 6 + n)))])
        self.assertEqual(len(rep.layout.checks), 0)
        # the units of a hierarchical program: a sum a checked unit, and the group's own generated quantities
        raw = checks_task("hier_intercepts")
        columns = {"g": ["a", "b", "c", "a", "b", "c", "a", "b"], "x": linear_input(8)["x"], "y": linear_input(8)["y"]}
        task, plan, prepared = self.prepared(raw, columns)
        run = bayes_gq.plan_runs(task, prepared.checks, prepared.facts)[0]
        units = run.values["Ppc_o1_units"]
        self.assertEqual([m.quantity for m in run.layout.checks if m.node == "unit_sum"], [f"Ppc_o1_usum[{i}]" for i in range(1, units + 1)])
        self.assertIn("Ppc_o1_usum.1", run.layout.header)

    def test_a_header_that_differs_is_a_manifest_mismatch(self):
        task, plan, prepared = self.prepared(checks_task(), linear_input())
        layout = bayes_gq.plan_runs(task, prepared.checks, prepared.facts)[0].layout
        layout.check_header(list(layout.header))
        with self.assertRaises(bayes_task.ManifestMismatch) as e:
            layout.check_header(layout.header[:5] + ["other"] + layout.header[6:])
        self.assertIn("column 6", str(e.exception))

    def test_the_data_file_is_the_head_of_the_sampling_one_and_the_run_s_own_entries(self):
        raw = checks_task(replicated=True)
        columns = linear_input()
        task, plan, prepared = self.prepared(raw, columns)
        runs = bayes_gq.plan_runs(task, prepared.checks, prepared.facts)
        sample_text = prepared.data_path.read_bytes()
        for run in runs:
            target = self.dir / run.data_name
            bayes_gq.write_data_file(prepared.data_path, prepared.facts.extension_offset, run.entries, run.values, target)
            # the same pass B written with this run's values: the bytes are the file the run's own preparation writes
            task2 = bayes_task.parse_task(json.loads(json.dumps(raw)))
            plan2 = bayes_run.bayes_checks.plan(task2)
            other = self.dir / f"whole_{run.name}"
            bayes_data.prepare(task2, self.write_input(columns, f"again_{run.name}.parquet"), other, checks=plan2, run=run.name)
            self.assertEqual(target.read_bytes(), (other / "data.json").read_bytes(), run.name)
            data = json.loads(target.read_text())
            self.assertEqual(data["Ppc_on"], 1 if run.name == "checks" else 0)
            self.assertEqual(data["Ppc_o1_rep"], 0 if run.name == "checks" else len(columns["y"]))
        self.assertTrue(sample_text.startswith(target.read_bytes()[:prepared.facts.extension_offset]))

    def test_the_data_file_of_a_run_is_written_in_pieces(self):
        task, plan, prepared = self.prepared(checks_task(), linear_input())
        run = bayes_gq.plan_runs(task, prepared.checks, prepared.facts)[0]
        whole = self.dir / "whole.json"
        bayes_gq.write_data_file(prepared.data_path, prepared.facts.extension_offset, run.entries, run.values, whole)
        with mock.patch.object(bayes_gq, "COPY_CHUNK", 7):
            small = self.dir / "small.json"
            bayes_gq.write_data_file(prepared.data_path, prepared.facts.extension_offset, run.entries, run.values, small)
        self.assertEqual(whole.read_bytes(), small.read_bytes())

    def test_a_version_without_the_extension_has_no_run_and_cannot_be_asked_for_replicated_data(self):
        raw = v1_task()
        task = bayes_task.parse_task(raw)
        prepared = prepare(task, self.dir / "v1", self.write_input(v1_input(), "v1.parquet"))
        self.assertEqual(bayes_gq.plan_runs(task, prepared.checks, prepared.facts), [])
        raw["output"] = raw["params"]["output"] = "replicated"
        raw["bayes"]["replicated"] = dict(REPLICATED)
        task = bayes_task.parse_task(raw)
        with self.assertRaises(Refusal) as e:
            bayes_gq.plan_runs(task, prepared.checks, prepared.facts)
        self.assertEqual(e.exception.code, "task_value")

    def test_the_checks_off_have_no_checks_run_and_one_row_has_no_statistics(self):
        task, plan, prepared = self.prepared(checks_task(on=False), linear_input())
        self.assertEqual(bayes_gq.plan_runs(task, prepared.checks, prepared.facts), [])
        task, plan, prepared = self.prepared(checks_task(on=False, replicated=True), linear_input())
        self.assertEqual([r.name for r in bayes_gq.plan_runs(task, prepared.checks, prepared.facts)], ["replicated"])
        task, plan, prepared = self.prepared(checks_task(), linear_input())
        prepared.checks.outcomes[0].n = 1
        with self.assertRaises(Refusal) as e:                     # a refusal of the data costs no sampling
            bayes_gq.plan_runs(task, prepared.checks, prepared.facts)
        self.assertEqual(e.exception.code, "data_shape")

    def test_the_replicated_writer_with_two_outcomes(self):
        path = self.dir / "rep.parquet"
        w = bayes_draws.ReplicatedWriter(path, 5, 2, 3, 2, outcomes=[("y", np.array([0, 1, 2])), ("z", np.array([3, 4]))])
        block = np.arange(2 * 6, dtype=float).reshape(2, 6)
        for chain, first, count in w.schedule():
            w.write_block(chain, first, block[:count] + chain * 100)
        meta = w.close()
        self.assertEqual((meta["rows"], meta["outcomes"]), (5, ["y", "z"]))
        t = pq.read_table(path)
        self.assertEqual(t.schema.names, [c["name"] for c in CONTRACT["output_columns"]["replicated"]])
        self.assertEqual(t.schema, bayes_draws.REPLICATED_SCHEMA)
        first = pq.ParquetFile(path).read_row_group(0).to_pydict()          # block 1 of chain 1: two draws
        self.assertEqual(first["outcome"], ["y"] * 6 + ["z"] * 4)
        self.assertEqual(first["draw"], [1, 1, 1, 2, 2, 2, 1, 1, 2, 2])
        self.assertEqual(first["row"], [1, 2, 3, 1, 2, 3, 1, 2, 1, 2])
        self.assertEqual(first["value"], [100, 101, 102, 106, 107, 108, 103, 104, 109, 110])
        self.assertEqual(t.num_rows, 2 * 3 * 5)


# ── the whole Job ─────────────────────────────────────────────────────────────
class GqCase(RunCase):
    def gq_spec(self, raw, columns, **extra):
        """The binary's own knowledge of the generated columns: the layouts the runs expand to (the fake writes what the program would)."""
        task = bayes_task.parse_task(json.loads(json.dumps(raw)))
        prepared = prepare(task, self.dir / "gqscratch", self.write_input(columns, "gqscratch.parquet"))
        runs = bayes_gq.plan_runs(task, prepared.checks, prepared.facts)
        shutil.rmtree(self.dir / "gqscratch")
        spec = {"gq": {r.output_stem: {"header": r.layout.header, **extra.get(r.name, {})} for r in runs}}
        return spec, runs

    def run_job(self, raw, columns=None, spec=None, gq_extra=None, **kw):
        columns = columns or linear_input()
        gq, runs = self.gq_spec(raw, columns, **(gq_extra or {}))
        header = self.program_header(raw, columns)
        full = {"header": header, "means": means_by_name(header), **gq, **(spec or {})}
        code = self.job(raw, columns, spec=full, output=raw["output"], **kw)
        return code, runs, header

    def rows(self):
        cols = engine_check(self.out / "results" / "results.parquet")
        return cols, dict(zip(cols["key"], zip(cols["value"], cols["text"])))


class TestTheChecksRun(GqCase):
    def test_the_ppc_rows_the_densities_and_the_seconds_of_the_checks_run(self):
        raw = checks_task()
        code, runs, header = self.run_job(raw)
        self.assertEqual(code, 0)
        self.assertFalse(self.work.exists())
        cols, rows = self.rows()
        self.assertEqual(self.left_behind(), ["artifacts/MLmodel", "artifacts/model.json", "artifacts/run.json", "output/output.parquet",
                                              "results/results.parquet"])
        keys = set(cols["key"])
        for stat in ("mean", "sd", "min", "max", "q10", "q50", "q90"):
            self.assertIn(f"ppc|y:{stat}||observed", keys)
            self.assertIn(f"ppc|y:{stat}||count|0", keys)
        self.assertIn("curve|ppc_density:y|observed|value|0", keys)
        self.assertIn("curve|ppc_density:y|draw_1|value|0", keys)
        self.assertIn("curve|ppc_density:y|draw_20|value|0", keys)
        self.assertNotIn("model|||note_no_checks", keys)
        self.assertNotIn("model|||note_checks_dropped", keys)
        self.assertGreaterEqual(rows["model|||seconds_checks"][0], 0.0)
        self.assertNotIn("model|||seconds_replicated", keys)
        run = json.loads((self.out / "artifacts" / "run.json").read_text(encoding="utf-8"))
        self.assertIn("checks", run["phases"])
        # the checks are the statistics the draws of the program wrote, not the sampling's: the second pass saw 40 draws of the 160
        counts = [v for k, v in zip(cols["key"], cols["value"]) if k.startswith("ppc|y:mean||count|")]
        self.assertEqual(len(counts), 30)

    def test_the_fitted_files_the_data_file_and_the_join_by_position(self):
        raw = checks_task()
        seen = {}
        real = bayes_gq.generate

        def generate(binary, sampler, run, work, data, block_values, *sinks, **hooks):
            seen["fitted"] = {c: (work / f"fitted_{c}.csv").read_bytes() for c in range(1, 5)}
            seen["data"] = data.read_bytes()
            seen["header"] = run.layout.header
            return real(binary, sampler, run, work, data, block_values, *sinks, **hooks)

        spec_extra = {"checks": {"echo": "lp__"}}
        with mock.patch.object(bayes_gq, "generate", generate):
            code, runs, header = self.run_job(raw, gq_extra=spec_extra)
        self.assertEqual(code, 0)
        # the fitted files: the sampling header and the kept lines, byte for byte as the fake sampler wrote them
        fake = {"header": header, "seed": SAMPLER["seed"], "means": means_by_name(header), "chains": 4, "draws": 40}
        columns = ",".join(list(bayes_follow.SAMPLER_NUTS) + header) + "\n"
        for c in range(1, 5):
            lines = fake_lines(fake, c)
            self.assertEqual(seen["fitted"][c].decode(), columns + "".join(lines[d - 1] for d in bayes_gq.kept_draws(40, 10)), c)
        self.assertEqual(json.loads(seen["data"])["Ppc_on"], 1)
        self.assertTrue(seen["data"].startswith(b'{"N": '))
        # the rows of the checks' file are the draws the fake echoed: row i of chain c came from the i-th kept line (its lp__ + the column)
        cols, rows = self.rows()
        lp = {c: [float(fake_lines(fake, c)[d - 1].split(",")[0]) for d in bayes_gq.kept_draws(40, 10)] for c in range(1, 5)}
        mean_column = seen["header"].index("Ppc_o1_mean.1")
        observed_mean = rows["ppc|y:mean||observed"][0]
        self.assertIsNotNone(observed_mean)
        count = [v for k, v in zip(cols["key"], cols["x0"]) if k.startswith("ppc|y:mean||count|")]
        values = sorted(l + mean_column for chain in lp.values() for l in chain)       # the statistic's draws, as the bins saw them
        self.assertAlmostEqual(min(c for c in count), min(min(values), observed_mean), delta=1e-6 + abs(observed_mean) * 1e-6)

    def test_a_draw_whose_generated_quantities_are_nan_is_dropped_and_counted(self):
        raw = checks_task()
        code, runs, header = self.run_job(raw, gq_extra={"checks": {"nan_rows": [[2, 5]]}})
        self.assertEqual(code, 0)
        cols, rows = self.rows()
        self.assertIn("1 of the 40 draws", rows["model|||note_checks_dropped"][1])

    def test_a_program_whose_header_differs_is_a_fault_not_a_refusal(self):
        raw = checks_task()
        gq, runs = self.gq_spec(raw, linear_input())
        gq["gq"]["gq"]["header"] = gq["gq"]["gq"]["header"][:4] + ["Ppc_o1_other.1"] + gq["gq"]["gq"]["header"][5:]
        with self.assertRaises(bayes_task.ManifestMismatch):
            self.job(raw, linear_input(), spec={"header": self.program_header(raw, linear_input()), **gq}, output="summary")
        self.assertFalse(self.work.exists())
        self.assertEqual(self.left_behind(), [])

    def test_a_failing_run_leaves_nothing(self):
        raw = checks_task()
        with self.assertRaises(Refusal) as e:
            self.run_job(raw, spec={"gq_require": ["not_a_parameter"]})
        self.assertEqual(e.exception.code, "sampler_failed")
        self.assert_nothing_left()
        self.tearDown()
        self.setUp()
        with mock.patch.object(bayes_gq, "generate", side_effect=Refusal("sampler_failed", "stopped")), self.assertRaises(Refusal):
            self.run_job(raw)
        self.assert_nothing_left()

    def test_cmdstan_is_started_twice_and_the_second_time_with_the_generate_quantities_arguments(self):
        started = []
        real = bayes_follow.start

        def start(args, workdir):
            started.append(list(args))
            return real(args, workdir)

        with mock.patch.object(bayes_follow, "start", start):
            code, runs, header = self.run_job(checks_task())
        self.assertEqual(len(started), 2)
        gq = started[1]
        self.assertEqual(gq[1:4], ["method=generate_quantities", f"fitted_params={self.work}/fitted.csv", "num_chains=4"])
        self.assertEqual(gq[4:], ["data", f"file={self.work}/data_gq.json", "random", "seed=20261124", "output", f"file={self.work}/gq.csv",
                                  "refresh=5", "num_threads=1"])

    def test_the_checks_off_run_nothing_more(self):
        started = []
        real = bayes_follow.start
        with mock.patch.object(bayes_follow, "start", lambda args, w: started.append(1) or real(args, w)):
            code, runs, header = self.run_job(checks_task(on=False))
        self.assertEqual((code, len(started), runs), (0, 1, []))
        cols, rows = self.rows()
        self.assertFalse([k for k in cols["key"] if k.startswith("ppc|")])
        self.assertNotIn("model|||seconds_checks", rows)


class TestTheReplicatedRun(GqCase):
    def test_the_replicated_output_chain_draw_row_outcome_value(self):
        raw = checks_task(replicated=True)
        code, runs, header = self.run_job(raw, gq_extra={"replicated": {"echo": "lp__"}})
        self.assertEqual(code, 0)
        self.assertFalse(self.work.exists())
        self.assertEqual(self.left_behind(), ["artifacts/MLmodel", "artifacts/model.json", "artifacts/run.json", "output/output.parquet",
                                              "results/results.parquet"])
        t = pq.read_table(self.out / "output" / "output.parquet")
        self.assertEqual(t.schema.names, [c["name"] for c in CONTRACT["output_columns"]["replicated"]])
        n = len(linear_input()["y"])
        self.assertEqual(t.num_rows, 4 * 5 * n)
        d = t.to_pydict()
        self.assertEqual(set(d["outcome"]), {"y"})
        self.assertEqual((min(d["chain"]), max(d["chain"]), min(d["draw"]), max(d["draw"]), min(d["row"]), max(d["row"])), (1, 4, 1, 5, 1, n))
        # the 5 kept draws of 40, the values the fake echoed: lp__ of the kept line + the row's column among the generated ones
        fake = {"header": header, "seed": SAMPLER["seed"], "means": means_by_name(header), "chains": 4, "draws": 40}
        rep_header = runs[1].layout.header
        for i in (0, 7, t.num_rows // 2, t.num_rows - 1):
            chain, draw, row = d["chain"][i], d["draw"][i], d["row"][i]
            line = fake_lines(fake, chain)[bayes_gq.kept_draws(40, 5)[draw - 1] - 1]
            self.assertAlmostEqual(d["value"][i], float(line.split(",")[0]) + rep_header.index(f"Ppc_o1_yrep.{row}"), places=4)
        cols, rows = self.rows()
        self.assertGreaterEqual(rows["model|||seconds_replicated"][0], 0.0)
        self.assertGreaterEqual(rows["model|||seconds_checks"][0], 0.0)

    def test_the_replicated_run_alone_when_the_checks_are_off(self):
        raw = checks_task(replicated=True, on=False)
        code, runs, header = self.run_job(raw)
        self.assertEqual(code, 0)
        cols, rows = self.rows()
        self.assertEqual([r.name for r in runs], ["replicated"])
        self.assertFalse([k for k in cols["key"] if k.startswith("ppc|")])
        self.assertNotIn("model|||seconds_checks", rows)
        self.assertIn("model|||seconds_replicated", rows)
        self.assertNotIn("model|||note_no_checks", rows)

    def test_a_replicated_draw_that_threw_is_left_out_counted_and_noted_the_step_is_refused_only_when_none_is_left(self):
        raw = checks_task(replicated=True, on=False)
        code, runs, header = self.run_job(raw, gq_extra={"replicated": {"nan_rows": [[1, 3]]}})
        self.assertEqual(code, 0)
        t = pq.read_table(self.out / "output" / "output.parquet").to_pydict()
        n = len(linear_input()["y"])
        self.assertEqual(len(t["value"]), (4 * 5 - 1) * n)
        self.assertNotIn(3, [d for c, d in zip(t["chain"], t["draw"]) if c == 1])
        cols, rows = self.rows()
        self.assertIn("1 of the 20 replicated draws", rows["model|||note_replicated_dropped"][1])
        self.tearDown()
        self.setUp()
        every = [[c, r] for c in range(1, 5) for r in range(1, 6)]
        with self.assertRaises(Refusal) as e:
            self.run_job(raw, gq_extra={"replicated": {"nan_rows": every}})
        self.assertEqual(e.exception.code, "not_finite")
        self.assert_nothing_left()

    def test_the_replicated_output_of_a_version_without_the_checks_is_a_task_value(self):
        raw = v1_task()
        raw["output"] = raw["params"]["output"] = "replicated"
        raw["bayes"]["replicated"] = dict(REPLICATED)
        with self.assertRaises(Refusal) as e:
            self.job(raw, v1_input(), output="replicated")
        self.assertEqual(e.exception.code, "task_value")
        self.assert_nothing_left()

    def test_the_output_is_not_copied_when_the_node_writes_none(self):
        code, runs, header = self.run_job(checks_task(replicated=True), write_output=False)
        self.assertEqual(code, 0)
        self.assertNotIn("output/output.parquet", self.left_behind())


# ── the time and the disk ─────────────────────────────────────────────────────
class TestTheLimits(GqCase):
    def test_a_checks_run_that_passes_the_time_is_too_slow_for_the_whole_step_before_any_result(self):
        clock = {"offset": 0.0}
        real = bayes_run.Clock

        class Jumping(real):
            def __init__(self, limits):
                super().__init__(limits, now=lambda: bayes_run.time.time() + clock["offset"])

        write = bayes_gq.write_data_file

        def written(*a, **k):
            out = write(*a, **k)
            clock["offset"] = 1800.0                  # the budget is spent when the checks' data are ready
            return out

        with mock.patch.object(bayes_run, "Clock", Jumping), mock.patch.object(bayes_gq, "write_data_file", written), \
                mock.patch.object(bayes_gq, "generate") as generate:
            with self.assertRaises(Refusal) as e:
                self.run_job(checks_task())
        self.assertEqual(e.exception.code, "too_slow")
        generate.assert_not_called()
        self.assert_nothing_left()

    def test_the_projection_of_a_run_from_its_own_draws(self):
        class Job:
            chains = 4
            draw_seconds = 0.0

            def __init__(self, budget):
                self.t = 0.0
                self.clock = bayes_run.Clock({"time_budget_seconds": budget, "started_unix": 0}, now=lambda: self.t)

            def gq_seconds(self, run, per_draw):
                return 0.0

            def too_slow(self, projected):
                raise Refusal("too_slow", f"{projected:.0f}", {"projected_seconds": projected})

        run = type("Run", (), {"per_chain": 10})()
        job = Job(budget=1000.0)
        watch = bayes_run.GqWatch(job, run, after=100.0)
        job.t = 100.0                                                    # chain 1 has written its ten draws in 100 s: 300 s to come
        watch.on_block(bayes_follow.BlockInfo(1, 1, 1, 10, 10, 1000))
        self.assertAlmostEqual(watch.rest, 300.0)
        job.t = 150.0
        watch.on_idle = watch.check
        watch.check()                                                    # nothing grows while no block comes: 250 s remain, +100 < 850
        job.t = 950.0                                                    # the 300 s of the run are over, 50 s of the 1,000 are left: 100 s still to come
        with self.assertRaises(Refusal):
            watch.check()
        tight = Job(budget=350.0)
        watch = bayes_run.GqWatch(tight, run, after=0.0)
        tight.t = 100.0
        with self.assertRaises(Refusal):                                 # 100 s for 10 of 40 draws: 400 s in all
            watch.on_block(bayes_follow.BlockInfo(1, 1, 1, 10, 10, 1000))

    def test_the_runs_files_are_counted_in_the_disk_before_sampling(self):
        raw = checks_task(replicated=True)
        task = bayes_task.parse_task(json.loads(json.dumps(raw)))
        prepared = prepare(task, self.dir / "s", self.write_input(linear_input(), "s.parquet"))
        job = bayes_run.SampleJob(task, prepared)
        watch = bayes_run.Watch(job)
        without = watch.disk_bytes()
        job.gq_runs = bayes_gq.plan_runs(task, prepared.checks, prepared.facts)
        with_runs = watch.disk_bytes()
        self.assertEqual(without + watch.gq_bytes(), with_runs)
        lines = 4 * 10 + 4 * 5
        self.assertGreater(watch.gq_bytes(), lines * 10)
        shutil.rmtree(self.dir / "s")
        started = []
        real = bayes_follow.start
        with mock.patch.object(bayes_follow, "start", lambda args, w: started.append(1) or real(args, w)):
            with self.assertRaises(Refusal) as e:
                self.run_job(raw, tmp_bytes=int(with_runs) - 1)
        self.assertEqual((e.exception.code, started), ("too_much_disk", []))
        self.assert_nothing_left()

    def test_the_later_stages_count_the_runs_once_a_draw_s_seconds_are_known(self):
        raw = checks_task(replicated=True)
        task = bayes_task.parse_task(json.loads(json.dumps(raw)))
        prepared = prepare(task, self.dir / "s", self.write_input(linear_input(), "s.parquet"))
        job = bayes_run.SampleJob(task, prepared)
        job.gq_runs = bayes_gq.plan_runs(task, prepared.checks, prepared.facts)
        base = job.later_seconds()
        self.assertAlmostEqual(base, job.pass2_seconds() + job.copy_seconds() + job.gq_seconds(job.gq_runs[0], 0.0) + job.gq_seconds(job.gq_runs[1], 0.0))
        slow = job.later_seconds(2.0)
        self.assertAlmostEqual(slow - base, 2.0 * 0.08 * (4 * 10 + 4 * 5), places=6)


if __name__ == "__main__":
    unittest.main()
