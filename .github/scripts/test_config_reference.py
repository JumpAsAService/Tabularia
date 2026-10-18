"""Tests for config_reference.py: docs/configuration.md must match the code.

  python3 .github/scripts/test_config_reference.py -v

Standard library only (the generator reads the settings classes with `ast`, so it runs
on any Python 3.11+ without the apps' dependencies). The drift tests copy the files the
generator reads into a throwaway tree, change one, and expect --check to fail.
"""
import importlib.util
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
SCRIPT = HERE / "config_reference.py"
PAGE = ROOT / "docs" / "configuration.md"


def load():
    spec = importlib.util.spec_from_file_location("config_reference", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["config_reference"] = module  # dataclasses look their module up there
    spec.loader.exec_module(module)
    return module


def run(*args, root=None):
    cmd = [sys.executable, str(SCRIPT), *args]
    if root is not None:
        cmd += ["--root", str(root)]
    return subprocess.run(cmd, capture_output=True, text=True)


class TheCommittedPage(unittest.TestCase):
    def test_page_matches_the_code(self):
        self.assertTrue(PAGE.is_file(), "docs/configuration.md is missing: run .github/scripts/config_reference.py")
        r = run("--check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_page_is_wrapped_for_github_pages(self):
        lines = PAGE.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "<!-- {% raw %} GitHub Pages: no Liquid in this page -->")
        self.assertEqual(lines[-1], "<!-- {% endraw %} -->")

    def test_every_variable_of_the_code_is_on_the_page(self):
        cr = load()
        text = PAGE.read_text(encoding="utf-8")
        found = cr.collect(ROOT)
        self.assertGreater(len(found.variables), 150)
        for name in found.variables:
            self.assertIn(f"`{name}`", text, name)

    def test_no_secret_default_is_printed(self):
        cr = load()
        found = cr.collect(ROOT)
        text = PAGE.read_text(encoding="utf-8")
        # the Default cell (third) of each variable's row; `\|` is a pipe inside a cell
        rows = {m.group(1): re.split(r"(?<!\\)\|", m.group(0))[3]
                for m in re.finditer(r"^\| `([A-Z0-9_]+)` \|.*$", text, re.M)}
        checked = 0
        for name, var in found.variables.items():
            if not var.secret or not isinstance(var.default, str) or not var.default:
                continue
            checked += 1
            self.assertNotIn(var.default, rows[name], f"{name} shows its default")
            self.assertIn("hidden", rows[name], name)
        # JWT, DB and storage passwords (the administrator's has no default since 1.2.0)
        self.assertGreaterEqual(checked, 3)
        self.assertNotIn("change-me-in-production", text)


class TheCheckCatchesDrift(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        cr = load()
        for rel in cr.SOURCES + [cr.PAGE, cr.TEXTS_FILE]:
            src = ROOT / rel
            dst = self.root / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            elif src.exists():
                shutil.copy2(src, dst)
        r = run("--check", root=self.root)
        self.assertEqual(r.returncode, 0, r.stdout)

    def tearDown(self):
        self.tmp.cleanup()

    def edit(self, rel, old, new):
        path = self.root / rel
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    def test_a_new_field_without_a_description_fails(self):
        self.edit("backend/app/core/config.py", "    max_listed: int = Field(default=100_000, ge=1)\n",
                  "    max_listed: int = Field(default=100_000, ge=1)\n    brand_new_cap: int = 7\n")
        r = run("--check", root=self.root)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("S3_SOURCE__BRAND_NEW_CAP", r.stdout + r.stderr)

    def test_a_removed_field_fails(self):
        self.edit("gateway/app/core/config.py", "    retention_minutes: int = Field(default=0, ge=0)\n", "")
        r = run("--check", root=self.root)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("AUDIT__RETENTION_MINUTES", r.stdout + r.stderr)

    def test_a_changed_default_fails_until_regenerated(self):
        self.edit("backend/app/core/config.py", "max_files: int = Field(default=500, ge=1)",
                  "max_files: int = Field(default=600, ge=1)")
        self.assertNotEqual(run("--check", root=self.root).returncode, 0)
        self.assertEqual(run(root=self.root).returncode, 0)
        self.assertEqual(run("--check", root=self.root).returncode, 0)
        self.assertIn("| `SFTP__MAX_FILES` | integer, ≥ 1 | `600` |",
                      (self.root / "docs/configuration.md").read_text(encoding="utf-8"))

    def test_a_new_direct_environment_read_fails(self):
        self.edit("backend/app/engine/context.py", "MAX_CROSS_JOIN_ROWS = ",
                  'SOMETHING_NEW = os.getenv("TABULARIA_SOMETHING_NEW", "1")\nMAX_CROSS_JOIN_ROWS = ')
        r = run("--check", root=self.root)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("TABULARIA_SOMETHING_NEW", r.stdout + r.stderr)

    def test_a_new_compose_variable_fails(self):
        self.edit("infrastructure/docker-compose.yml", "${WORKER_MEM_LIMIT:-6g}", "${WORKER_MEM_CEILING:-6g}")
        r = run("--check", root=self.root)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("WORKER_MEM_CEILING", r.stdout + r.stderr)

    def test_a_hand_edit_of_the_page_fails(self):
        self.edit("docs/configuration.md", "`SFTP__MAX_FILES`", "`SFTP__MAX_FILEZ`")
        self.assertNotEqual(run("--check", root=self.root).returncode, 0)


if __name__ == "__main__":
    unittest.main()
