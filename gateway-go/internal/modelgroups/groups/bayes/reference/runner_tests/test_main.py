"""The dispatcher and the preparation of a sampling Job (lot K1): the task read, the data passes, the layout, the refusals and what they
leave behind, and run.py as a Job starts it."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py")]
GO_TESTDATA = HERE.parents[4] / "bayescontract" / "testdata"

import pyarrow as pa  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

import bayes_binary  # noqa: E402
import bayes_main  # noqa: E402
import bayes_task  # noqa: E402
from tab_protocol import Refusal  # noqa: E402


def go_task(mode="sample", **limits):
    raw = json.loads((GO_TESTDATA / "tasks" / f"{mode}.json").read_text(encoding="utf-8"))
    raw["limits"].update(limits)
    return raw


INPUT = {"col_g": ["s2", "s1", "s3", "s1", "s2", "s3"], "col_x": [1.0, 2.0, 3.0, 4.0, 5.0, 6.5], "col_y": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]}


class MainCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.input = str(self.dir / "in.parquet")
        pq.write_table(pa.table(INPUT), self.input)

    def tearDown(self):
        self.tmp.cleanup()


class TestPrepareRun(MainCase):
    def test_the_data_passes_the_layout_and_data_json(self):
        task = bayes_task.parse_task(go_task())
        prepared = bayes_main.prepare_run(task, self.dir / "run", self.input)
        self.assertEqual(prepared.data_path, self.dir / "run" / "data.json")
        data = json.loads(prepared.data_path.read_text(encoding="utf-8"))
        self.assertEqual((data["N"], data["J_grp"], data["g"], data["Mode"]), (6, 3, [2, 1, 3, 1, 2, 3], 0))
        self.assertEqual(prepared.facts.units["grp"], ["s1", "s2", "s3"])
        # three groups, not twenty: a header of its own size, which the follower checks against the CSV's
        self.assertEqual(prepared.layout.header[:7], list(bayes_task.SAMPLER_COLUMNS_NUTS))
        self.assertEqual(prepared.layout.n_quantities, 5 + 3 + 3 + 1)
        self.assertEqual(prepared.layout.n_columns, len(prepared.layout.header))

    def test_too_many_quantities_before_pass_b(self):
        task = bayes_task.parse_task(go_task(max_quantities=10))
        with self.assertRaises(Refusal) as e:
            bayes_main.prepare_run(task, self.dir / "run", self.input)
        self.assertEqual(e.exception.code, "too_many_quantities")
        self.assertIn("12 quantities; this installation allows 10", e.exception.message)
        self.assertFalse((self.dir / "run" / "data.json").exists())

    def test_an_empty_input_variable_is_a_task_value(self):
        task = bayes_task.parse_task(go_task())
        with mock.patch.dict(os.environ, {"TABULARIA_INPUT": ""}), self.assertRaises(Refusal) as e:
            bayes_main.prepare_run(task, self.dir / "run")
        self.assertEqual(e.exception.code, "task_value")


class TestDispatch(MainCase):
    def test_the_compile_mode_is_handed_to_bayes_compile(self):
        import bayes_compile
        task = bayes_task.parse_task(go_task("compile"))
        with mock.patch.object(bayes_compile, "run", return_value=0) as run:
            self.assertEqual(bayes_main.run_task(task, self.dir), 0)
        run.assert_called_once_with(task)

    def test_a_missing_module_is_not_built(self):
        task = bayes_task.parse_task(go_task("compile"))
        with mock.patch.object(bayes_main.importlib, "import_module", side_effect=ModuleNotFoundError(name="bayes_compile")):
            with self.assertRaises(Refusal) as e:
                bayes_main.run_task(task, self.dir)
        self.assertEqual(e.exception.code, "not_built")

    def test_the_follower_is_given_the_task_and_what_the_passes_made(self):
        calls = []
        fake = types.ModuleType("bayes_run")
        fake.begin = lambda task: (None, "sample")
        fake.run = lambda task, prepared: calls.append((task.mode, prepared.facts.n, prepared.data_path.exists())) or 0
        task = bayes_task.parse_task(go_task())
        with mock.patch.dict(sys.modules, {"bayes_run": fake}), mock.patch.dict(os.environ, {"TABULARIA_INPUT": self.input}), \
                    mock.patch.object(bayes_binary, "fetch_binary", lambda workdir, expected: workdir / "model"):
            self.assertEqual(bayes_main.run_task(task, self.dir, self.dir / "run"), 0)
        self.assertEqual(calls, [("sample", 6, True)])

    def test_a_refusal_after_the_passes_leaves_no_run_folder_and_a_crash_too(self):
        for error in (Refusal("too_slow", "about 41 minutes"), RuntimeError("a bug")):
            fake = types.ModuleType("bayes_run")

            def run(task, prepared, error=error):
                (prepared.workdir / "draws.parquet").write_bytes(b"x")
                raise error

            fake.begin = lambda task: (None, "sample")
            fake.run = run
            task = bayes_task.parse_task(go_task())
            with mock.patch.dict(sys.modules, {"bayes_run": fake}), mock.patch.dict(os.environ, {"TABULARIA_INPUT": self.input}), \
                    mock.patch.object(bayes_binary, "fetch_binary", lambda workdir, expected: workdir / "model"):
                with self.assertRaises(type(error)):
                    bayes_main.run_task(task, self.dir, self.dir / "run")
            self.assertFalse((self.dir / "run").exists())

    def test_main_writes_the_refusal_and_exits_3(self):
        code = self.dir / "code"
        code.mkdir()
        (code / "task.json").write_text(json.dumps({"protocol": 2, "mode": "sample"}), encoding="utf-8")
        with mock.patch.object(bayes_main, "code_dir", lambda: code), mock.patch.dict(os.environ, {"TABULARIA_ARTIFACTS": str(self.dir / "art")}), \
                mock.patch.object(sys, "argv", ["run.py"]):
            self.assertEqual(bayes_main.main(code), 3)
        error = json.loads((self.dir / "art" / "error.json").read_text(encoding="utf-8"))
        self.assertEqual(error["code"], "protocol")
        with mock.patch.object(bayes_main, "code_dir", lambda: self.dir / "nowhere"), mock.patch.dict(os.environ, {"TABULARIA_ARTIFACTS": str(self.dir / "art2")}), \
                mock.patch.object(sys, "argv", ["run.py"]):
            self.assertEqual(bayes_main.main(code), 3)
        self.assertEqual(json.loads((self.dir / "art2" / "error.json").read_text(encoding="utf-8"))["code"], "no_task")

    def test_run_py_as_a_job_starts_it(self):
        # the Job's tree: /tabularia/code holds runner/ and common/py/ side by side
        code = self.dir / "code"
        (code / "runner").mkdir(parents=True)
        shutil.copy(HERE.parents[1] / "runner" / "run.py", code / "runner" / "run.py")
        shutil.copytree(HERE.parents[1] / "runner" / "py", code / "runner" / "py", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copytree(HERE.parents[3] / "common" / "py", code / "common" / "py", ignore=shutil.ignore_patterns("__pycache__"))
        env = {"PATH": os.environ.get("PATH", ""), "TABULARIA_ARTIFACTS": ""}
        done = subprocess.run([sys.executable, "-I", "-B", str(code / "runner" / "run.py")], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertIn('TABULARIA_ERROR {"code":"no_task"', done.stdout)
        (code / "task.json").write_text(json.dumps(go_task("compile")), encoding="utf-8")
        done = subprocess.run([sys.executable, "-I", "-B", str(code / "runner" / "run.py")], capture_output=True, text=True, timeout=60, env=env)
        self.assertEqual(done.returncode, 3, done.stderr)
        # the compile mode is built (lot K3): without the Job's addresses it refuses as a fault of the Job
        self.assertIn('"code":"task_value"', done.stdout)


if __name__ == "__main__":
    unittest.main()
