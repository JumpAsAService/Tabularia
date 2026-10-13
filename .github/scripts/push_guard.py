#!/usr/bin/env python3
"""Pre-push guard: stops a push that would make one of your secrets public.

Runs on the commits the remote does not have yet, before they leave the machine:

  1. your own secrets, by value: read from the files listed in the local git config
     (`git config --add tabularia.secretFiles PATH`, and `tabularia.secretStandard true`
     for the credential files of cloud tools and your SSH keys) and looked up literally
     in the lines and commit messages the push adds. The values never leave the machine,
     so short ones (from MIN_LOCAL characters) are checked too.
  2. TruffleHog on the same commits, with the release workflow's rules: any credential
     it verifies as live, or a cloud credential it cannot verify.

Install it as the pre-push hook of this clone:

    printf '#!/bin/sh\\nexec python3 "$(git rev-parse --show-toplevel)/.github/scripts/push_guard.py" "$@"\\n' \\
        > .git/hooks/pre-push && chmod +x .git/hooks/pre-push

A secret already on the remote does not stop the push (removing it must stay possible);
it is reported. In an emergency, `git push --no-verify` skips the guard.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from image_secret_scan import STANDARD_FILES, Collector  # noqa: E402

ZERO = "0" * 40
MIN_LOCAL = 10
HERE = Path(__file__).resolve().parent


def git(*args: str, check: bool = True) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=check).stdout


def git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], capture_output=True, check=True).stdout


def exists(sha: str) -> bool:
    return subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"], capture_output=True).returncode == 0


def your_secrets() -> tuple[dict[bytes, str], list[str]]:
    """Values from the files in the local git config. Returns them and the files read."""
    paths = [Path(p).expanduser() for p in git("config", "--get-all", "tabularia.secretFiles", check=False).split("\n") if p]
    if git("config", "--bool", "tabularia.secretStandard", check=False).strip() == "true":
        paths += [Path(p).expanduser() for p in STANDARD_FILES]
        ssh = Path("~/.ssh").expanduser()
        if ssh.is_dir():
            paths += [p for p in sorted(ssh.glob("id_*")) if p.suffix != ".pub"]
    collector = Collector()
    read = []
    for path in paths:
        if path.is_file():
            collector.file(path)
            read.append(path.name)
    values = dict(collector.values)
    for label, value in collector.too_short.items():
        if len(value) >= MIN_LOCAL:
            values.setdefault(value, label)
    return values, read


def new_commits(remote: str, local_sha: str, remote_sha: str) -> list[str]:
    exclude = [f"--remotes={remote}"]
    if remote_sha != ZERO and exists(remote_sha):
        exclude.append(f"^{remote_sha}")
    return git("rev-list", local_sha, "--not", *exclude).split()


def added_content(commit: str) -> bytes:
    """The commit message and every line the commit adds, binary files included."""
    message = git_bytes("log", "-1", "--format=%B", commit)
    patch = git_bytes("show", "--format=", "--no-color", "--no-ext-diff", "--text", "-p", commit)
    added = [line[1:] for line in patch.split(b"\n") if line.startswith(b"+") and not line.startswith(b"+++")]
    return message + b"\n" + b"\n".join(added)


def public_at(base: str | None, value: bytes) -> bool:
    if not base:
        return False
    try:
        return subprocess.run(["git", "grep", "-qF", "-e", value.decode(), base, "--"],
                              capture_output=True).returncode == 0
    except UnicodeDecodeError:
        return False


def check_your_secrets(pushes: list[tuple[str, str | None, list[str]]]) -> int:
    values, read = your_secrets()
    if not read:
        print("push guard: no secret files configured (git config --add tabularia.secretFiles PATH); "
              "only TruffleHog runs", file=sys.stderr)
        return 0
    stopped = 0
    for ref, base, commits in pushes:
        for commit in commits:
            content = added_content(commit)
            for value, label in values.items():
                if value not in content:
                    continue
                where = f"{label} in commit {commit[:10]} ({ref})"
                if public_at(base, value):
                    print(f"push guard: note, {where}: already on the remote, so it is public already",
                          file=sys.stderr)
                else:
                    print(f"push guard: SECRET {where}", file=sys.stderr)
                    stopped += 1
    return stopped


def trufflehog_settings() -> dict[str, str]:
    settings = {}
    for line in (HERE / "trufflehog.env").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, _, value = line.partition("=")
            settings[key.strip()] = value.strip()
    return settings


def check_trufflehog(pushes: list[tuple[str, str | None, list[str]]]) -> tuple[int, bool]:
    """Returns the findings, and whether TruffleHog could scan everything."""
    settings = trufflehog_settings()
    top = git("rev-parse", "--show-toplevel").strip()
    stopped = 0
    for ref, base, commits in pushes:
        since = [f"--since-commit={base}"] if base else []
        for rules in (["--results=verified"],
                      ["--results=verified,unknown", f"--include-detectors={settings['CLOUD_DETECTORS']}"]):
            command = ["docker", "run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{top}:/repo:ro",
                       settings["TRUFFLEHOG_IMAGE"], "git", "file:///repo", *since, f"--branch={commits[0]}",
                       *rules, "--fail", "--fail-on-scan-errors", "--no-update", "--json"]
            try:
                result = subprocess.run(command, capture_output=True, text=True)
            except OSError as e:
                print(f"push guard: cannot run TruffleHog ({e}): is Docker installed?", file=sys.stderr)
                return stopped, False
            if result.returncode == 0:
                continue
            if result.returncode != 183:
                print(f"push guard: TruffleHog could not scan ({result.returncode}): {result.stderr.strip()[-300:]}",
                      file=sys.stderr)
                return stopped, False
            for line in result.stdout.splitlines():
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                git_meta = r.get("SourceMetadata", {}).get("Data", {}).get("Git", {})
                state = "verified" if r.get("Verified") else "verification failed" if r.get("VerificationError") else "unverified"
                print(f"push guard: {r.get('DetectorName')} credential ({state}) in {git_meta.get('file')} "
                      f"line {git_meta.get('line')}, commit {str(git_meta.get('commit'))[:10]} ({ref})", file=sys.stderr)
                stopped += 1
    return stopped, True


def main(argv: list[str]) -> int:
    remote = argv[1] if len(argv) > 1 else "origin"
    pushes = []
    for line in sys.stdin:
        if not line.strip():
            continue
        local_ref, local_sha, _remote_ref, remote_sha = line.split()
        if local_sha == ZERO:  # deleting a ref publishes nothing
            continue
        commits = new_commits(remote, local_sha, remote_sha)
        if not commits:
            continue
        oldest = commits[-1]
        parent = git("rev-parse", "--verify", "--quiet", f"{oldest}^", check=False).strip()
        base = remote_sha if remote_sha != ZERO and exists(remote_sha) else (parent or None)
        pushes.append((local_ref, base, commits))
    if not pushes:
        return 0
    stopped = check_your_secrets(pushes)
    scanned = True
    if os.environ.get("PUSH_GUARD_TRUFFLEHOG", "1") != "0":
        found, scanned = check_trufflehog(pushes)
        stopped += found
    if stopped:
        print(f"push guard: push stopped, {stopped} finding(s). Remove the secret from the commits "
              "(and rotate it if it was ever pushed); `git push --no-verify` skips the guard.", file=sys.stderr)
        return 1
    if not scanned:
        # a check that did not run is not a pass
        print("push guard: push stopped, the TruffleHog scan did not complete. "
              "`git push --no-verify` skips the guard.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
