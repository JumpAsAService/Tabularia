"""Tests for model_catalog.py: the copies of the statistical models' catalogue must be the
snapshot.

  python3 .github/scripts/test_model_catalog.py -v

Standard library only. The drift tests copy the snapshot into a throwaway tree, write its
copies, change one thing, and expect --check to fail saying what.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SCRIPT = HERE / "model_catalog.py"
SNAPSHOT = "backend/app/engine/models/catalog.json"
COPIES = [
    "frontend/assets/model-catalog.json",
    "gateway-go/internal/modelcatalog/catalog.json",
    "gateway/app/services/model_catalog.json",
]
PAGE = "docs/capabilities/models.md"


def run(*args, root=None):
    cmd = [sys.executable, str(SCRIPT), *args]
    if root is not None:
        cmd += ["--root", str(root)]
    return subprocess.run(cmd, capture_output=True, text=True)


class TheCommittedCopies(unittest.TestCase):
    def test_the_copies_are_the_snapshot(self):
        r = run("--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_script_knows_every_copy(self):
        text = SCRIPT.read_text(encoding="utf-8")
        for path in [SNAPSHOT, *COPIES, PAGE]:
            self.assertIn(f'"{path}"', text)


class Drift(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        target = self.root / SNAPSHOT
        target.parent.mkdir(parents=True)
        target.write_bytes((ROOT / SNAPSHOT).read_bytes())
        self.assertEqual(run(root=self.root).returncode, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fresh_copies_pass(self):
        r = run("--check", root=self.root)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("is not there yet", r.stdout)
        for copy in COPIES:
            self.assertEqual((self.root / copy).read_bytes(), (ROOT / SNAPSHOT).read_bytes())

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
        (self.root / COPIES[1]).unlink()
        r = run("--check", root=self.root)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn(f"{COPIES[1]} is missing", r.stdout)
        self.assertFalse((self.root / COPIES[1]).exists())

    def test_a_changed_snapshot_fails_until_the_copies_are_written(self):
        path = self.root / SNAPSHOT
        data = json.loads(path.read_text(encoding="utf-8"))
        data["limits"]["max_terms"] = 201
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        r = run("--check", root=self.root)
        self.assertNotEqual(r.returncode, 0)
        for copy in COPIES:
            self.assertIn(f"{copy} differs", r.stdout)
        self.assertEqual(run(root=self.root).returncode, 0)
        self.assertEqual(run("--check", root=self.root).returncode, 0)

    def test_a_snapshot_that_is_not_a_catalogue_is_refused(self):
        path = self.root / SNAPSHOT
        for broken in ("{", '{"groups": [], "models": []}',
                       '{"groups": [{"id": "g", "models": []}], "models": [{"id": "a", "group": "g"}]}',
                       '{"groups": [{"id": "g", "models": ["a", "b"]}], "models": [{"id": "a", "group": "g"}]}'):
            path.write_text(broken, encoding="utf-8")
            for args in (("--check",), ()):
                r = run(*args, root=self.root)
                self.assertNotEqual(r.returncode, 0, broken)
                self.assertIn("is not a catalogue", r.stdout)
        path.unlink()
        self.assertIn("is missing", run("--check", root=self.root).stdout)

    def test_the_documentation_must_name_every_model_once_the_page_exists(self):
        page = self.root / PAGE
        page.parent.mkdir(parents=True)
        page.write_text("# Statistical models\n\nNothing yet.\n", encoding="utf-8")
        r = run("--check", root=self.root)
        self.assertNotEqual(r.returncode, 0)
        models = [m["id"] for m in json.loads((ROOT / SNAPSHOT).read_text(encoding="utf-8"))["models"]]
        for m in models:
            self.assertIn(f"{PAGE} does not name the model `{m}`", r.stdout)
        page.write_text("# Statistical models\n\n" + "\n".join(f"- `{m}`" for m in models) + "\n", encoding="utf-8")
        r = run("--check", root=self.root)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertNotIn("is not there yet", r.stdout)


if __name__ == "__main__":
    unittest.main()
