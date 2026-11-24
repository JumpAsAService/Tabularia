"""The compile mode (lot K3): the program's hash, the compile's failure, the binary and compile.json uploaded to a folder standing in for the
bucket, and the termination message, written last. make and stanc are fakes (the real ones are checked by hand on the image)."""
import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parents[3] / "common" / "py"), str(HERE.parents[1] / "runner" / "py")]
GO_TESTDATA = HERE.parents[4] / "bayescontract" / "testdata"

import bayes_cmdstan  # noqa: E402
import bayes_compile  # noqa: E402
import bayes_main  # noqa: E402
import bayes_task  # noqa: E402
from tab_protocol import Refusal  # noqa: E402

PROGRAM = b"parameters { real mu; } model { mu ~ normal(0, 1); }\n"
BINARY = b"\x7fELF" + bytes(range(256)) * 40
INFO = {"distributions": ["normal"], "functions": [], "included_files": {}, "parameters": {"mu": {"type": "real", "dimensions": 0}}}
STANC_SCRIPT = f"""#!{sys.executable}
import json, sys
if sys.argv[1] == "--version":
    print("stanc3 v2.40.0 (Unix)")
else:
    sys.stderr.write("Warning in 'model.stan': stanc warning on stderr\\n")
    print(json.dumps({INFO!r}))
"""


def task_of(program=PROGRAM, **limits):
    raw = json.loads((GO_TESTDATA / "tasks" / "compile.json").read_text(encoding="utf-8"))
    raw["bayes"]["model"]["program_sha256"] = hashlib.sha256(program).hexdigest()
    raw["limits"].update(limits)
    return bayes_task.parse_task(raw)


def fake_make(status=0, log="Warning: a make warning\nInfo: an info\nmade it\n", binary=BINARY):
    def compile_program(source, name, workdir, extra=(), timeout=600):
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / f"{name}.stan").write_text(source, encoding="utf-8")
        (workdir / f"{name}.make.log").write_text(log, encoding="utf-8")
        exe = workdir / name
        if status == 0:
            exe.write_bytes(binary)
        return exe, bayes_cmdstan.Outcome(status, log, 1.5, 700.0)
    return compile_program


class CompileCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        (self.dir / "code").mkdir()
        (self.dir / "code" / "program.stan").write_bytes(PROGRAM)
        stanc = self.dir / "stanc"
        stanc.write_text(STANC_SCRIPT, encoding="utf-8")
        stanc.chmod(stanc.stat().st_mode | stat.S_IXUSR)
        self.bucket = self.dir / "bucket"
        self.message = self.dir / "termination-log"
        self.env = {"TABULARIA_BINARY_OUT": str(self.bucket / "bayes-compile/7-1/model"),
                    "TABULARIA_ARTIFACTS": str(self.bucket / "bayes-compile/7-1/artifacts") + "/"}
        patches = [mock.patch.object(bayes_compile, "code_dir", lambda: self.dir / "code"),
                   mock.patch.object(bayes_compile, "STANC", stanc),
                   mock.patch.object(bayes_compile, "WORKDIR", self.dir / "c"),
                   mock.patch.object(bayes_compile, "TERMINATION_LOG", self.message),
                   mock.patch.object(bayes_cmdstan, "compile_program", fake_make()),
                   mock.patch.dict(os.environ, self.env)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def uploaded(self):
        return sorted(str(p.relative_to(self.bucket)) for p in self.bucket.rglob("*") if p.is_file()) if self.bucket.exists() else []


class TestCompile(CompileCase):
    def test_binary_compile_json_and_message(self):
        self.assertEqual(bayes_compile.run(task_of()), 0)
        self.assertEqual(self.uploaded(), ["bayes-compile/7-1/artifacts/compile.json", "bayes-compile/7-1/model"])
        binary = (self.bucket / "bayes-compile/7-1/model").read_bytes()
        self.assertEqual(binary, BINARY)
        raw = (self.bucket / "bayes-compile/7-1/artifacts/compile.json").read_bytes()
        cj = json.loads(raw)
        self.assertEqual(sorted(cj), ["binary_bytes", "binary_sha256", "info", "program_sha256", "seconds", "stanc", "warnings"])
        self.assertEqual((cj["binary_sha256"], cj["binary_bytes"]), (hashlib.sha256(BINARY).hexdigest(), len(BINARY)))
        self.assertEqual(cj["program_sha256"], hashlib.sha256(PROGRAM).hexdigest())
        self.assertEqual(cj["info"], INFO)
        self.assertEqual(cj["stanc"], "stanc3 v2.40.0 (Unix)")
        self.assertEqual(cj["warnings"], ["Warning: a make warning", "Info: an info"])
        message = self.message.read_text(encoding="utf-8")
        self.assertLessEqual(len(message.encode()), 4096)
        self.assertEqual(json.loads(message), {"program_sha256": cj["program_sha256"], "binary_sha256": cj["binary_sha256"],
                                               "info_sha256": hashlib.sha256(raw).hexdigest()})

    def test_the_upload_is_in_chunks_and_hashed_as_read(self):
        big = os.urandom(300_000)
        src = self.dir / "big"
        src.write_bytes(big)
        sha, size = bayes_compile.upload_hashing(src, str(self.dir / "out" / "big"), chunk=65_536)
        self.assertEqual((sha, size), (hashlib.sha256(big).hexdigest(), len(big)))
        self.assertEqual((self.dir / "out" / "big").read_bytes(), big)

    def test_warnings_are_at_most_fifty(self):
        log = "".join(f"Warning: w{i}\n" for i in range(80)) + "x" * 10
        with mock.patch.object(bayes_cmdstan, "compile_program", fake_make(log=log)):
            bayes_compile.run(task_of())
        cj = json.loads((self.bucket / "bayes-compile/7-1/artifacts/compile.json").read_bytes())
        self.assertEqual(len(cj["warnings"]), 50)


class TestRefusals(CompileCase):
    def test_program_hash(self):
        (self.dir / "code" / "program.stan").write_bytes(PROGRAM + b"// altered\n")
        with self.assertRaises(Refusal) as cm:
            bayes_compile.run(task_of())
        self.assertEqual(cm.exception.code, "program_hash")
        self.assertEqual(self.uploaded(), [])
        self.assertFalse(self.message.exists())

    def test_program_missing_is_program_hash(self):
        (self.dir / "code" / "program.stan").unlink()
        with self.assertRaises(Refusal) as cm:
            bayes_compile.run(task_of())
        self.assertEqual(cm.exception.code, "program_hash")

    def test_compile_failed_prints_the_last_forty_lines_and_writes_nothing(self):
        log = "".join(f"line {i}\n" for i in range(100))
        out = []
        with mock.patch.object(bayes_cmdstan, "compile_program", fake_make(status=2, log=log)), \
                mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            with self.assertRaises(Refusal) as cm:
                bayes_compile.run(task_of())
        self.assertEqual(cm.exception.code, "compile_failed")
        self.assertEqual(out[0], "line 60")
        self.assertEqual(out[-1], "line 99")
        self.assertEqual(self.uploaded(), [])
        self.assertFalse(self.message.exists())

    def test_stanc_failing_is_compile_failed(self):
        (self.dir / "stanc").write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n", encoding="utf-8")
        with self.assertRaises(Refusal) as cm:
            bayes_compile.run(task_of())
        self.assertEqual(cm.exception.code, "compile_failed")
        self.assertEqual(self.uploaded(), [])

    def test_addresses_missing(self):
        with mock.patch.dict(os.environ, {"TABULARIA_BINARY_OUT": ""}):
            with self.assertRaises(Refusal) as cm:
                bayes_compile.run(task_of())
        self.assertEqual(cm.exception.code, "task_value")

    def test_dispatch_from_the_main(self):
        self.assertEqual(bayes_main.run_task(task_of(), self.dir), 0)
        self.assertTrue(self.message.exists())


if __name__ == "__main__":
    unittest.main()
