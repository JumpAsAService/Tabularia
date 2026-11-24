"""The sampling Job assembled (lot R1 of docs/specs/bayes-runner.md): the mode `sample` from the binary to the last copy, on the fake CmdStan
as a real child process fetched and hashed as the compiled model is. Criterion 2 (results the engine's check accepts, the output asked for,
a card without draws, the same draws file byte for byte at five chain speeds) on a version without the checks (generator 1) and on a
version with them turned off; criterion 4 (each refusal of a sampling Job tripped where it is checked, leaving /tmp/run gone and nothing
at the output addresses). Small data only: a few dozen rows, a few quantities, sixty iterations a chain."""
import hashlib
import json
import math
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py"), str(HERE)]
GO_TESTDATA = HERE.parents[4] / "bayescontract" / "testdata"
GOLDEN = HERE.parents[4] / "bayeschecks" / "testdata" / "golden"
REPO = HERE.parents[6]
FAKE = HERE / "fake_cmdstan.py"

import numpy as np  # noqa: E402
import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import bayes_follow  # noqa: E402
import bayes_main  # noqa: E402
import bayes_pass2  # noqa: E402
import bayes_run  # noqa: E402
import bayes_task  # noqa: E402
import tab_results  # noqa: E402
from tab_protocol import Refusal  # noqa: E402

SAMPLER = {"chains": 4, "warmup": 20, "draws": 40, "seed": 20261124, "adapt_delta": 0.8, "max_treedepth": 10, "init": None, "refresh": 5}
CONTRACT = json.loads((GO_TESTDATA / "contract.json").read_text(encoding="utf-8"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# ── the engine's check of a model's results (backend/app/engine/models/results.py: check_table), on the bayes declaration ──────────────
def engine_check(path, variant="sample"):
    """The rules of the engine's `check_table` applied to the file, with the declaration Go writes (bayescontract/testdata/contract.json)
    and the models' section shapes: the engine's module needs the backend's packages, which the image's pins do not hold."""
    shapes = {**json.loads((REPO / "backend" / "app" / "engine" / "models" / "catalog.json").read_text(encoding="utf-8"))["section_shapes"],
              **CONTRACT["shapes"]}
    sections = CONTRACT["variants"][variant]
    t = pq.read_table(path)
    assert t.schema.names == tab_results.schema().names, t.schema.names
    assert t.num_rows <= 100_000
    cols = t.to_pydict()
    assert sorted(cols["ord"]) == list(range(t.num_rows)), "ord"
    seen, texts = set(), {}

    def declared(stat_, names):
        return stat_ in (names or []) or any(n.endswith("*") and stat_.startswith(n[:-1]) for n in names or [])

    for i in range(t.num_rows):
        section, level, level_2, st, key = cols["section"][i], cols["level"][i], cols["level_2"][i], cols["stat"][i], cols["key"][i]
        for v in (cols["value"][i], cols["x0"][i], cols["x1"][i]):
            assert v is None or math.isfinite(v), key
        assert key not in seen, f"twice: {key}"
        seen.add(key)
        assert section in sections, f"{key}: no section {section}"
        shape, decl = shapes[section], sections[section]
        for part, v in (("level", level), ("level_2", level_2)):
            assert not (shape[part] == "null" and v is not None), f"{key}: {part} must be null"
            assert not (shape[part] == "required" and not v), f"{key}: {part} is required"
        base = "|".join((section, level or "", level_2 or "", st))
        if shape["indexed"] and st not in shape.get("unindexed", ()):
            assert key.startswith(base + "|") and key[len(base) + 1:].isdigit(), key
        else:
            assert key == base, key
        if declared(st, decl["numbers"]):
            assert cols["text"][i] is None or section in ("point", "curve"), key
        elif declared(st, decl["texts"]):
            assert cols["value"][i] is None, key
            texts[key] = cols["text"][i]
        else:
            raise AssertionError(f"{key}: {st} is not declared in {section}")
    for key in ("model|||model", "model|||variant", "model|||n"):
        assert key in seen, key
    assert texts["model|||model"] == "bayes_model" and texts["model|||variant"] == variant
    return cols


# ── the tasks ─────────────────────────────────────────────────────────────────
def v1_task():
    """The contract's example: a version stored by generator 1 (no predictive checks), correlated effects on a group plate."""
    raw = json.loads((GO_TESTDATA / "tasks" / "sample.json").read_text(encoding="utf-8"))
    raw["bayes"]["sampler"] = dict(SAMPLER)
    raw["limits"]["started_unix"] = 0
    return raw


def golden_task(name):
    """A golden program of bayeschecks with its extension, the columns bound by their Stan names, the checks off."""
    raw = v1_task()
    manifest = json.loads((GOLDEN / f"checks_{name}.manifest.json").read_text(encoding="utf-8"))
    mapping = json.loads((GOLDEN / f"checks_{name}.mapping.json").read_text(encoding="utf-8"))
    data = []
    for e in manifest["data"]:
        e = dict(e)
        if e["kind"] in ("real", "int", "group_index", "category"):
            e["column"] = e["stan"]
        data.append(e)
    b = raw["bayes"]
    b["data"], b["quantities"], b["mapping"] = data, manifest["quantities"], mapping
    b.pop("finite_variance", None)  # sample.json's is hier_slopes' decision: a golden task's says nothing (not decided)
    b["plates"] = [{"id": "rows", "kind": "rows", "label": "Rows"}] + [
        {"id": e["target"], "kind": "group", "label": e["target"]} for e in data if e["kind"] == "group_index"]
    b["model"]["codegen_version"] = manifest["codegen_version"]
    b["model"]["program_sha256"] = manifest["program_sha256"]
    b["model"]["semantic_hash"] = manifest["semantic_hash"]
    b["checks"] = {"format": 1, "on": False, "outcomes": 3, "value_bins": 40, "stat_bins": 30, "density_draws": 20, "max_units": 8,
                   "ppc_draws": 1000}
    return raw


def prepare(task, workdir, input_path):
    """The data passes as bayes_main runs them: with the checks' plan, the sampling run's extension values."""
    return bayes_main.prepare_run(task, workdir, input_path, checks=bayes_run.bayes_checks.plan(task), run="sample")


def v1_input(n=30):
    rng = np.random.default_rng(7)
    return {"col_g": [f"s{i % 3 + 1}" for i in range(n)], "col_x": rng.normal(size=n).tolist(), "col_y": rng.normal(size=n).tolist()}


def linear_input(n=30):
    rng = np.random.default_rng(8)
    x = rng.normal(size=n)
    return {"x": x.tolist(), "y": (1.5 + 0.5 * x + rng.normal(scale=0.3, size=n)).tolist()}


class RunCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "bucket"
        self.out.mkdir()
        self.work = self.dir / "run"

    def tearDown(self):
        self.tmp.cleanup()

    def program_header(self, raw, columns):
        """The program's columns of the CSV, as the compiled program would write them (the layout of a scratch preparation)."""
        scratch = self.dir / "scratch"
        prepared = prepare(bayes_task.parse_task(json.loads(json.dumps(raw))), scratch, self.write_input(columns, "scratch.parquet"))
        header = prepared.layout.header[len(bayes_task.SAMPLER_COLUMNS_NUTS):]
        shutil.rmtree(scratch)
        return header

    def write_input(self, columns, name="in.parquet"):
        path = self.dir / name
        pq.write_table(pa.table(columns), str(path))
        return str(path)

    def binary(self, spec, name="model"):
        """A fake compiled model in the bucket: a shell script handing CmdStan's arguments to fake_cmdstan.py, and its sha256."""
        spec_path = self.dir / f"{name}.spec.json"
        spec_path.write_text(json.dumps(spec))
        path = self.out / name
        path.write_text(f'#!/bin/sh\nexec "{sys.executable}" -I -B "{FAKE}" --cmdstan "{spec_path}" "$@"\n')
        return path, sha(path)

    def job(self, raw, columns, spec=None, output="summary", write_output=True, env=None, means=None, **limits):
        """Runs the whole sampling Job (bayes_main.run_task) and returns what it left at the addresses."""
        raw = json.loads(json.dumps(raw))
        raw["output"], raw["params"]["output"], raw["write_output"] = output, output, write_output
        raw["limits"].update(limits)
        header = self.program_header(raw, columns)
        spec = {"header": header, "tick": 0.0005, "means": means(header) if means else [0.0] * len(header), **(spec or {})}
        binary, digest = self.binary(spec)
        raw["bayes"]["model"]["binary_sha256"] = digest
        task = bayes_task.parse_task(raw)
        self.addresses = {"TABULARIA_INPUT": self.write_input(columns), "TABULARIA_BINARY_IN": str(binary),
                          "TABULARIA_RESULTS": str(self.out / "results" / "results.parquet"),
                          "TABULARIA_OUTPUT": str(self.out / "output" / "output.parquet"),
                          "TABULARIA_ARTIFACTS": str(self.out / "artifacts"), "OMP_NUM_THREADS": "2", **(env or {})}
        with mock.patch.dict(os.environ, self.addresses):
            return bayes_main.run_task(task, HERE, self.work)

    def left_behind(self):
        """Every file at the output addresses (the binary's own place in the bucket excepted)."""
        return sorted(str(p.relative_to(self.out)) for p in self.out.rglob("*") if p.is_file() and p.name != "model")

    def assert_nothing_left(self):
        self.assertFalse(self.work.exists(), "/tmp/run is left")
        self.assertEqual(self.left_behind(), [])


def means_by_name(header):
    """The fake's means: a quantity's mean is a number read from its name, so that the summary can be checked against it."""
    out = []
    for name in header:
        h = int(hashlib.sha256(name.encode()).hexdigest()[:4], 16)
        out.append(round((h % 2000) / 100.0 - 10.0, 2))
    return out


# ── criterion 2 ───────────────────────────────────────────────────────────────
class TestTheSampleMode(RunCase):
    def test_a_version_without_checks_writes_results_the_engine_accepts_the_summary_and_a_card_without_draws(self):
        self.assertEqual(self.job(v1_task(), v1_input(), means=means_by_name), 0)
        self.assertFalse(self.work.exists())
        self.assertEqual(self.left_behind(), ["artifacts/MLmodel", "artifacts/model.json", "artifacts/run.json", "output/output.parquet",
                                              "results/results.parquet"])
        cols = engine_check(self.out / "results" / "results.parquet")
        rows = dict(zip(cols["key"], zip(cols["value"], cols["text"])))
        self.assertIn("predictive checks", rows["model|||note_no_checks"][1])
        for key in ("model|||n", "model|||seconds_warmup", "model|||seconds_sampling", "model|||seconds_conversion", "model|||seconds_pass_2",
                    "model|||n_in_summary", "sampler|all||divergences", "sampler|1||e_bfmi", "sampler|4||draws", "quantity|mu_a||rhat_max",
                    "data|x||mean", "curve|trace:mu_a|1|value|0", "hist|rank:mu_a|1|count|0", "curve|density:mu_a|posterior|value|0"):
            self.assertIn(key, rows)
        self.assertEqual(rows["model|||n"][0], 30)
        self.assertEqual(rows["sampler|all||draws"][0], 160)
        # the summary output: the contract's columns exactly, one row a quantity, the means the fake drew around
        summary = pq.read_table(self.out / "output" / "output.parquet")
        self.assertEqual(summary.schema.names, [c["name"] for c in CONTRACT["output_columns"]["summary"]])
        self.assertTrue(set(summary.column("kind").to_pylist()) <= set(CONTRACT["summary_kinds"]))
        header = self.program_header(v1_task(), v1_input())
        expected = dict(zip(header, means_by_name(header)))
        s = summary.to_pydict()
        self.assertEqual(s["quantity"][0], "mu_a")
        self.assertAlmostEqual(s["mean"][0], expected["mu_a"], delta=0.5)
        # the card: the scalars' summary and the sampler's facts, never a draw
        card = json.loads((self.out / "artifacts" / "model.json").read_text(encoding="utf-8"))
        self.assertEqual([r["quantity"] for r in card["bayes"]["summary"]], ["mu_a", "mu_b", "sigma", "tau_a", "tau_b"])
        self.assertEqual(card["bayes"]["facts"]["divergences"], 0)
        self.assertNotIn("draws", json.dumps(card["bayes"]["summary"]))
        self.assertLess(len(json.dumps(card)), 20_000)
        run = json.loads((self.out / "artifacts" / "run.json").read_text(encoding="utf-8"))
        self.assertEqual(run["exit"], 0)
        self.assertTrue({"warmup", "sampling", "conversion", "pass_2", "data"} <= set(run["phases"]))

    def test_the_draws_output_is_the_same_file_byte_for_byte_at_five_chain_speeds_with_the_checks_off(self):
        speeds = ([1, 1, 1, 1], [3, 0.4, 1, 2], [0.3, 0.5, 4, 1], [1, 2, 0.25, 0.7], [5, 5, 0.2, 0.2])
        digests = set()
        for k, sp in enumerate(speeds):
            self.setUp()
            spec = {"speeds": sp, "start": [0, 3 * k, k, 0], "piece_seed": k, "max_piece": 97 + 50 * k}
            self.assertEqual(self.job(golden_task("glm_linear"), linear_input(), spec=spec, output="draws", means=means_by_name), 0)
            output = self.out / "output" / "output.parquet"
            digests.add(sha(output))
            if k == 0:
                t = pq.read_table(output)
                self.assertEqual(t.schema.names, [c["name"] for c in CONTRACT["output_columns"]["draws"]])
                self.assertEqual(t.num_rows, 4 * 40 * (7 + 3))      # the sampler's fields and alpha, b_x, sigma: no prior marginal
                cols = engine_check(self.out / "results" / "results.parquet")
                self.assertNotIn("model|||note_no_checks", cols["key"])
            self.tearDown()
        self.assertEqual(len(digests), 1)

    def test_the_contraction_from_the_prior_draws_of_the_same_run(self):
        raw = golden_task("glm_linear")
        raw["bayes"]["finite_variance"] = ["alpha", "bx"]          # sigma's prior said not to have one
        self.assertEqual(self.job(raw, linear_input(), means=means_by_name), 0)
        cols = engine_check(self.out / "results" / "results.parquet")
        rows = dict(zip(cols["key"], zip(cols["value"], cols["text"])))
        self.assertIsNotNone(rows["summary|alpha||contraction"][0])
        self.assertIsNotNone(rows["summary|b_x||contraction"][0])
        self.assertIsNone(rows["summary|sigma||contraction"][0])
        self.assertIn("no finite variance", rows["model|||note_contraction_null"][1])
        self.assertIn("curve|density:alpha|prior|value|0", rows)           # the prior's density from prior.parquet
        summary = pq.read_table(self.out / "output" / "output.parquet").to_pydict()
        self.assertIsNotNone(summary["contraction"][summary["quantity"].index("alpha")])

    def test_without_the_variance_decision_every_contraction_is_null_with_its_note(self):
        self.assertEqual(self.job(golden_task("glm_linear"), linear_input()), 0)
        cols = engine_check(self.out / "results" / "results.parquet")
        rows = dict(zip(cols["key"], zip(cols["value"], cols["text"])))
        self.assertIsNone(rows["summary|alpha||contraction"][0])
        self.assertIn("does not say whether the prior has a finite variance", rows["model|||note_contraction_null"][1])

    def test_the_output_results_writes_no_output_and_cmdstan_runs_with_path_alone(self):
        started = []
        real = bayes_follow.start

        def start(args, workdir):
            started.append(args)
            return real(args, workdir)

        with mock.patch.object(bayes_follow, "start", start), mock.patch("subprocess.Popen", wraps=bayes_follow.subprocess.Popen) as popen:
            self.assertEqual(self.job(v1_task(), v1_input(), output="results"), 0)
        self.assertNotIn("output/output.parquet", self.left_behind())
        self.assertEqual(len(started), 1)
        self.assertEqual(popen.call_args.kwargs["env"], {"PATH": bayes_follow.CMDSTAN_PATH})
        args = started[0]
        self.assertEqual(args[0], str(self.work / "model"))
        for a in ("num_samples=40", "num_warmup=20", "num_chains=4", "seed=20261124", "num_threads=2", "delta=0.8", "max_depth=10"):
            self.assertIn(a, args)


# ── criterion 4: the refusals, where they are checked ─────────────────────────
class TestRefusals(RunCase):
    def refused(self, code, raw=None, columns=None, **kw):
        with self.assertRaises(Refusal) as e:
            self.job(raw or v1_task(), columns or v1_input(), **kw)
        self.assertEqual(e.exception.code, code, e.exception.message)
        self.assert_nothing_left()
        return e.exception

    def test_a_changed_binary_is_never_run(self):
        raw = v1_task()
        with mock.patch.object(bayes_main.bayes_binary, "fetch_hashing", side_effect=Refusal("binary_hash", "not the one")), \
                mock.patch.object(bayes_follow, "start") as start:
            self.refused("binary_hash", raw)
        start.assert_not_called()

    def test_the_data(self):
        columns = v1_input()
        columns["col_x"] = [2.0] * 30
        with mock.patch.object(bayes_follow, "start") as start:
            self.refused("data_constant", columns=columns)
        start.assert_not_called()

    def test_the_disk_before_sampling(self):
        with mock.patch.object(bayes_follow, "start") as start:
            e = self.refused("too_much_disk", tmp_bytes=1_000_000)
        start.assert_not_called()
        self.assertIn("this Job's disk holds 1.0 MB", e.message)

    def test_the_disk_after_the_first_block_with_the_chains_own_bytes(self):
        # the constants say the CSVs take nothing; the first block of a chain says what its lines take
        raw = v1_task()
        task = bayes_task.parse_task(json.loads(json.dumps(raw)))
        scratch = self.dir / "s"
        prepared = prepare(task, scratch, self.write_input(v1_input(), "s.parquet"))
        job = bayes_run.SampleJob(task, prepared)
        with mock.patch.object(bayes_run, "CSV_BYTES_A_VALUE", 0.0), mock.patch.object(bayes_run, "CSV_HEAD_BYTES", 0), \
                mock.patch.object(bayes_run, "CSV_HEAD_BYTES_A_COLUMN", 0), mock.patch.object(bayes_run, "tree_bytes", lambda path: 0):
            before = bayes_run.Watch(job).disk_bytes()
            shutil.rmtree(scratch)
            started = []
            real = bayes_follow.start
            with mock.patch.object(bayes_follow, "start", lambda args, w: started.append(1) or real(args, w)):
                e = self.refused("too_much_disk", raw, tmp_bytes=int(before) + 1000)
        self.assertEqual(started, [1])
        self.assertGreater(e.params["projected_bytes"], before + 1000)

    def test_the_time_at_the_progress_lines(self):
        # once CmdStan has started the clock runs fifty thousand times faster than the chains: the first rates project past the budget
        real = bayes_run.Clock
        started = []

        def now():
            t = bayes_run.time.time()
            return t if not started else started[0] + (t - started[0]) * 50_000

        class Fast(real):
            def __init__(self, limits):
                super().__init__(limits, now=now)

        start = bayes_follow.start
        with mock.patch.object(bayes_run, "Clock", Fast), \
                mock.patch.object(bayes_follow, "start", lambda args, w: started.append(bayes_run.time.time()) or start(args, w)):
            e = self.refused("too_slow", spec={"tick": 0.002})
        self.assertEqual(len(started), 1)
        self.assertIn("chains of 60 iterations; the node's timeout is 30 minutes", e.message)

    def test_the_time_before_the_second_pass(self):
        clock = {"offset": 0.0}
        real = bayes_run.Clock

        class Jumping(real):
            def __init__(self, limits):
                super().__init__(limits, now=lambda: bayes_run.time.time() + clock["offset"])

        sample = bayes_follow.sample

        def sampled(*a, **k):
            out = sample(*a, **k)
            clock["offset"] = 1800.0           # the budget is spent when the chains end
            return out

        with mock.patch.object(bayes_run, "Clock", Jumping), mock.patch.object(bayes_follow, "sample", sampled), \
                mock.patch.object(bayes_pass2, "second_pass") as second_pass:
            self.refused("too_slow")
        second_pass.assert_not_called()

    def test_the_time_at_the_second_pass_first_measure(self):
        real = bayes_pass2.second_pass

        def second_pass(*a, **k):
            return real(*a, **{**k, "seconds_left": 1e-9})

        with mock.patch.object(bayes_pass2, "second_pass", second_pass):
            e = self.refused("too_slow")
        self.assertIn("second pass", e.message)

    def test_the_sampler_failing(self):
        self.refused("init_failed", spec={"init_fail": True})
        self.setUp()
        self.refused("sampler_failed", spec={"die": {"chain": 2, "after": 7}})

    def test_a_draw_that_is_not_finite(self):
        e = self.refused("not_finite", spec={"special": [[3, 11, 2, "nan"]]})
        self.assertEqual((e.params["chain"], e.params["draw"]), (3, 11))

    def test_a_prior_marginal_that_is_not_finite_is_no_refusal(self):
        raw = golden_task("glm_linear")
        header = self.program_header(raw, linear_input())
        column = header.index("Prior_m_1") + 1
        raw["bayes"]["finite_variance"] = ["alpha", "bx", "sigma"]
        self.assertEqual(self.job(raw, linear_input(), spec={"special": [[1, 5, column, "inf"]]}), 0)
        rows = dict(zip(*[engine_check(self.out / "results" / "results.parquet")[k] for k in ("key", "value")]))
        self.assertIsNone(rows["summary|alpha||contraction"])       # its variance is not finite: no contraction, never a refusal

    def test_the_addresses_of_the_job_are_checked_before_sampling(self):
        with mock.patch.object(bayes_follow, "start") as start:
            self.refused("task_value", env={"TABULARIA_RESULTS": ""})
        start.assert_not_called()

    def test_a_copy_that_fails_is_a_fault_not_a_refusal(self):
        real = bayes_run.bayes_draws.copy_in_chunks
        calls = []

        def copy(source, target, *a):
            calls.append(target)
            if len(calls) == 2:
                raise OSError("the bucket is gone")
            return real(source, target, *a)

        with mock.patch.object(bayes_run.bayes_draws, "copy_in_chunks", copy), self.assertRaises(OSError):
            self.job(v1_task(), v1_input())
        self.assertFalse(self.work.exists())
        self.assertEqual(self.left_behind(), ["results/results.parquet"])   # the gateway deletes the step's files by key (exit 1)


class TestThePriorPredictiveOfAProgramWithoutOne(RunCase):
    def test_it_is_refused_before_the_binary_is_fetched(self):
        raw = json.loads((GO_TESTDATA / "tasks" / "prior_predictive.json").read_text(encoding="utf-8"))
        raw["bayes"]["data"] = [e for e in raw["bayes"]["data"] if not e.get("kind", "").startswith(("prior_", "checks_"))]
        with mock.patch.object(bayes_main.bayes_binary, "fetch_binary") as fetch, self.assertRaises(Refusal) as e:
            bayes_main.run_task(bayes_task.parse_task(raw), HERE, self.work)
        self.assertEqual(e.exception.code, "task_value")
        self.assertIn("no prior predictive", e.exception.message)
        fetch.assert_not_called()


# ── the projections, alone ────────────────────────────────────────────────────
class FakeJob:
    def __init__(self, chains=4, warmup=100, draws=100, threads=4, budget=600.0, later=0.0, columns=10):
        self.t = 1000.0
        self.clock = bayes_run.Clock({"time_budget_seconds": budget, "started_unix": 0}, now=lambda: self.t)
        self.task = type("T", (), {"sampler": {"chains": chains, "warmup": warmup, "draws": draws}})()
        self.threads = threads
        self.layout = type("L", (), {"n_columns": columns})()
        self.block_rows = 1
        self.later = later

    def later_seconds(self, per_draw=None):
        return self.later

    def too_slow(self, projected):
        raise Refusal("too_slow", f"{projected:.0f}", {"projected_seconds": projected})


class TestTheProjections(unittest.TestCase):
    def progress(self, watch, job, chain, i, phase, t):
        job.t = t
        watch.chain_clock[chain].add(i, 200, phase, t)

    def test_warmup_and_sampling_apart_and_the_slowest_chain(self):
        job = FakeJob()
        watch = bayes_run.Watch(job)
        self.progress(watch, job, 1, 1, "warmup", 1000.0)
        self.progress(watch, job, 1, 51, "warmup", 1050.0)        # 1 s an iteration of warm-up
        self.progress(watch, job, 2, 1, "warmup", 1000.0)
        self.progress(watch, job, 2, 101, "warmup", 1050.0)       # 0.5 s
        job.t = 1050.0
        self.assertAlmostEqual(watch.sampling_seconds(), 49 * 1.0 + 100 * 1.0)
        self.progress(watch, job, 2, 101, "sampling", 1050.0)
        self.progress(watch, job, 2, 151, "sampling", 1060.0)     # 0.2 s an iteration of sampling: chain 1 uses it for its draws
        job.t = 1060.0
        self.assertAlmostEqual(watch.sampling_seconds(), 49 * 1.0 + 100 * 0.2 - 10.0)

    def test_the_chains_after_the_threads_run_in_later_waves(self):
        job = FakeJob(chains=4, threads=2)
        watch = bayes_run.Watch(job)
        self.progress(watch, job, 1, 1, "warmup", 1000.0)
        self.progress(watch, job, 1, 11, "warmup", 1010.0)
        self.progress(watch, job, 2, 1, "warmup", 1000.0)
        self.progress(watch, job, 2, 11, "warmup", 1010.0)
        self.assertAlmostEqual(watch.sampling_seconds(), (89 + 100) * 1.0 + 1 * 200 * 1.0)

    def test_too_slow_at_a_progress_line_and_at_an_idle_tick_past_the_budget(self):
        job = FakeJob(budget=100.0)
        watch = bayes_run.Watch(job)
        self.progress(watch, job, 1, 1, "warmup", 1000.0)
        watch.check_time()                                         # one line: no rate yet, nothing projected
        job.t = 1002.0
        with self.assertRaises(Refusal) as e:
            watch.on_progress(bayes_follow.Progress(1, 3, 200, "warmup"))   # 1 s an iteration × 197 left
        self.assertGreater(e.exception.params["projected_seconds"], 100)
        job2 = FakeJob(budget=10.0)
        watch2 = bayes_run.Watch(job2)
        watch2.on_idle()
        job2.t = 1011.0
        with self.assertRaises(Refusal):
            watch2.on_idle()

    def test_the_later_stages_count_before_any_line(self):
        job = FakeJob(budget=100.0, later=150.0)
        with self.assertRaises(Refusal):
            bayes_run.Watch(job).check_time()


class TestTheDataFileOffset(RunCase):
    def test_pass_b_records_where_the_extension_begins(self):
        raw = golden_task("glm_linear")
        prepared = prepare(bayes_task.parse_task(raw), self.work, self.write_input(linear_input()))
        text = prepared.data_path.read_bytes()
        offset = prepared.facts.extension_offset
        self.assertTrue(text[:offset].endswith(b",\n"))
        self.assertTrue(text[offset:].startswith(b'"Ppc_on": 0'))
        self.assertNotIn(b"Ppc_", text[:offset])
        v1 = prepare(bayes_task.parse_task(v1_task()), self.dir / "v1", self.write_input(v1_input(), "v1.parquet"))
        self.assertIsNone(v1.facts.extension_offset)

    def test_the_prior_marginals_go_to_their_own_file_of_the_layout(self):
        raw = golden_task("hier_intercepts")
        columns = {"g": ["a", "b", "c", "a", "b", "c"], **{k: v[:6] for k, v in linear_input().items()}}
        prepared = prepare(bayes_task.parse_task(raw), self.work, self.write_input(columns))
        layout = prepared.layout
        self.assertEqual([m.quantity for m in layout.marginals], ["Prior_m_1", "Prior_m_3", "Prior_m_4", "Prior_m_5", "Prior_m_6"])
        self.assertEqual([m.node for m in layout.marginals], ["bx", "mua", "sigma", "taua", "ag"])
        self.assertEqual([layout.header[c] for c in layout.marginal_columns], ["Prior_m_1", "Prior_m_3", "Prior_m_4", "Prior_m_5", "Prior_m_6"])
        self.assertNotIn("Prior_m_1", [e.quantity for e in layout.draws])


if __name__ == "__main__":
    unittest.main()
