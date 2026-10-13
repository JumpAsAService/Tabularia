"""Tests for push_guard.py against a throwaway clone and a bare remote: what a push adds
is checked, what the remote already has is not held against it.

  python3 .github/scripts/test_push_guard.py -v

TruffleHog is left out here (PUSH_GUARD_TRUFFLEHOG=0): it needs Docker and the network.
"""
import os
import secrets
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parent / "push_guard.py"
ZERO = "0" * 40


class PushGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.secret = "scw-" + secrets.token_hex(12)
        self.short = "pw" + secrets.token_hex(4)  # 10 characters: checked locally only
        (root / "secrets.env").write_text(f"STORAGE__SECRET_KEY={self.secret}\nDB__PASSWORD={self.short}\n")
        self.env = {**os.environ, "HOME": str(root), "PUSH_GUARD_TRUFFLEHOG": "0",
                    "GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "GIT_AUTHOR_NAME": "t",
                    "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(root / "remote.git")], check=True, env=self.env)
        subprocess.run(["git", "clone", "-q", str(root / "remote.git"), str(root / "work")], check=True, env=self.env,
                       capture_output=True)
        self.work = root / "work"
        self.git("config", "--add", "tabularia.secretFiles", str(root / "secrets.env"))
        self.commit("README.md", b"hello\n", "first")
        self.git("push", "-q", "origin", "HEAD:main")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.work, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, name, content: bytes, message):
        path = self.work / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        self.git("add", name)
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD")

    def guard(self, local_ref="refs/heads/main", local=None, remote_ref="refs/heads/main", remote=None):
        local = local or self.git("rev-parse", "HEAD")
        remote = remote or self.git("rev-parse", "origin/main")
        r = subprocess.run([sys.executable, str(GUARD), "origin", "unused-url"], cwd=self.work, env=self.env,
                           input=f"{local_ref} {local} {remote_ref} {remote}\n", capture_output=True, text=True)
        return r.returncode, r.stderr

    def test_clean_push(self):
        self.commit("app.py", b"print('ok')\n", "clean")
        self.assertEqual(self.guard()[0], 0)

    def test_secret_in_a_new_file(self):
        self.commit("config/settings.py", f"KEY = '{self.secret}'\n".encode(), "settings")
        code, log = self.guard()
        self.assertEqual(code, 1)
        self.assertIn("SECRET secrets.env:STORAGE__SECRET_KEY", log)
        self.assertNotIn(self.secret, log)

    def test_secret_in_the_commit_message(self):
        self.commit("app.py", b"x\n", f"use {self.secret} for now")
        self.assertEqual(self.guard()[0], 1)

    def test_secret_in_a_binary_file(self):
        self.commit("blob.bin", b"\x00\x01" + self.secret.encode() + b"\xff\x00", "binary")
        self.assertEqual(self.guard()[0], 1)

    def test_short_secret_is_checked_locally(self):
        self.commit("db.ini", f"password = {self.short}\n".encode(), "db")
        code, log = self.guard()
        self.assertEqual(code, 1)
        self.assertIn("DB__PASSWORD", log)

    def test_only_the_commits_the_remote_lacks(self):
        # a secret that reached the remote some other way is public already: reported, not blocking
        self.commit("old.env", f"S={self.secret}\n".encode(), "old leak")
        self.git("push", "-q", "--no-verify", "origin", "HEAD:main")
        self.commit("other.env", f"T={self.secret}\n".encode(), "again")
        code, log = self.guard()
        self.assertEqual(code, 0)
        self.assertIn("already on the remote", log)

    def test_removing_a_published_secret_is_allowed(self):
        self.commit("old.env", f"S={self.secret}\n".encode(), "old leak")
        self.git("push", "-q", "--no-verify", "origin", "HEAD:main")
        self.git("rm", "-q", "old.env")
        self.git("commit", "-q", "-m", "remove the leak")
        self.assertEqual(self.guard()[0], 0)

    def test_new_branch_checks_only_its_own_commits(self):
        self.git("checkout", "-q", "-b", "feature")
        self.commit("f.py", f"K='{self.secret}'\n".encode(), "feature work")
        self.assertEqual(self.guard("refs/heads/feature", remote_ref="refs/heads/feature", remote=ZERO)[0], 1)

    def test_tag_on_a_published_commit(self):
        self.git("tag", "-a", "v9.9.9", "-m", "release")
        self.assertEqual(self.guard("refs/tags/v9.9.9", self.git("rev-parse", "v9.9.9"), "refs/tags/v9.9.9", ZERO)[0], 0)

    def test_deleting_a_branch(self):
        self.assertEqual(self.guard("(delete)", ZERO, "refs/heads/old", self.git("rev-parse", "HEAD"))[0], 0)

    def test_without_secret_files_only_trufflehog_would_run(self):
        self.git("config", "--unset-all", "tabularia.secretFiles")
        self.commit("config/settings.py", f"KEY = '{self.secret}'\n".encode(), "settings")
        code, log = self.guard()
        self.assertEqual(code, 0)
        self.assertIn("no secret files configured", log)


if __name__ == "__main__":
    unittest.main()
