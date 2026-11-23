"""Tests for bayes_templates.py: the copies of the Bayesian editor's templates must be the file.

  python3 .github/scripts/test_bayes_templates.py -v

Standard library only. The drift tests copy the file into a throwaway tree, write its copies,
change one thing, and expect --check to fail saying what.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SCRIPT = HERE / "bayes_templates.py"
SOURCE = "gateway-go/internal/bayestemplates/templates.json"
COPIES = [
    "frontend/assets/bayes-templates.json",
]


def run(*args, root=None):
    cmd = [sys.executable, str(SCRIPT), *args]
    if root is not None:
        cmd += ["--root", str(root)]
    return subprocess.run(cmd, capture_output=True, text=True)


class TheCommittedCopies(unittest.TestCase):
    def test_the_copies_are_the_file(self):
        r = run("--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_script_knows_every_copy(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for path in [SOURCE, *COPIES]:
            self.assertIn(f'"{path}"', text)


class Drift(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        target = self.root / SOURCE
        target.parent.mkdir(parents=True)
        target.write_bytes((ROOT / SOURCE).read_bytes())
        self.assertEqual(run(root=self.root).returncode, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_copies_pass(self):
        r = run("--check", root=self.root)
        self.assertEqual(r.returncode, 0, r.stdout)
        for copy in COPIES:
            self.assertEqual((self.root / copy).read_bytes(), (ROOT / SOURCE).read_bytes())

    def test_a_copy_that_differs_by_a_byte_fails_and_is_named(self):
        for copy in COPIES:
            path = self.root / copy
            good = path.read_bytes()
            path.write_bytes(good + b" ")
            r = run("--check", root=self.root)
            self.assertNotEqual(r.returncode, 0, copy)
            self.assertIn(f"{copy} differs", r.stdout)
            path.write_bytes(good)
        self.assertEqual(run("--check", root=self.root).returncode, 0)

    def test_a_missing_copy_fails_and_check_writes_nothing(self):
        (self.root / COPIES[0]).unlink()
        r = run("--check", root=self.root)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"{COPIES[0]} is missing", r.stdout)
        self.assertFalse((self.root / COPIES[0]).exists())

    def test_a_changed_file_fails_until_the_copies_are_written(self):
        path = self.root / SOURCE
        data = json.loads(path.read_text(encoding="utf-8"))
        data["templates"][0]["version"] = 2
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        r = run("--check", root=self.root)
        self.assertNotEqual(r.returncode, 0)
        for copy in COPIES:
            self.assertIn(f"{copy} differs", r.stdout)
        self.assertEqual(run(root=self.root).returncode, 0)
        self.assertEqual(run("--check", root=self.root).returncode, 0)

    def test_a_file_that_is_not_a_templates_file_is_refused(self):
        path = self.root / SOURCE
        good = json.loads((ROOT / SOURCE).read_text(encoding="utf-8"))
        twice = dict(good, templates=good["templates"][:1] * 2)
        low = json.loads(json.dumps(good))
        low["templates"][0]["version"] = 0
        stray = json.loads(json.dumps(good))
        stray["templates"][0]["scaled"].append({"node": "nowhere", "slot": "prior.mu", "as": "center"})
        unsourced = json.loads(json.dumps(good))
        unsourced["templates"][0]["sources"]["alpha"] = "nobody"
        for broken in ("{", '{"templates": []}', json.dumps(dict(good, templates=[])),
                       json.dumps(twice), json.dumps(low), json.dumps(stray), json.dumps(unsourced)):
            path.write_text(broken, encoding="utf-8")
            for args in (("--check",), ()):
                r = run(*args, root=self.root)
                self.assertNotEqual(r.returncode, 0, broken[:60])
                self.assertIn("is not a templates file", r.stdout)
        path.unlink()
        self.assertIn("is missing", run("--check", root=self.root).stdout)


if __name__ == "__main__":
    unittest.main()
