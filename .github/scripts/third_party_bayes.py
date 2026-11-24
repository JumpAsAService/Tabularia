#!/usr/bin/env python3
"""Assemble, and check, the notices of the Bayesian group's CmdStan family.

The group ships the licence files of CmdStan, Stan, Stan Math, stanc3 and the libraries
Stan Math bundles in gateway-go/internal/modelgroups/groups/bayes/licence/. The site's
third-party-notices.txt takes them as one `--extra` entry, frontend/assets/third-party/cmdstan.txt,
which this script writes from those files, verbatim (a CRLF line end becomes LF): each distinct text once, under the
path(s) it ships at and its sha256. Nothing is added to a text and nothing is explained.

  python3 .github/scripts/third_party_bayes.py write    # (re)writes cmdstan.txt
  python3 .github/scripts/third_party_bayes.py check    # exits 1 if it is stale, or if a
                                                        # sha256 in docs/specs/bayes-image.md
                                                        # is not that of a shipped file
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHIPPED = ROOT / "gateway-go/internal/modelgroups/groups/bayes/licence"
OUTPUT = ROOT / "frontend/assets/third-party/cmdstan.txt"
SPEC = ROOT / "docs/specs/bayes-image.md"
LINE = "-" * 78


def shipped() -> dict[str, list[str]]:
    """sha256 -> the paths (relative to licence/) that hold that text, sorted."""
    by_hash: dict[str, list[str]] = {}
    for path in sorted(p for p in SHIPPED.rglob("*") if p.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        by_hash.setdefault(digest, []).append(path.relative_to(SHIPPED).as_posix())
    return by_hash


def assemble() -> str:
    by_hash = shipped()
    # the project's own licences first (CmdStan, Stan, Stan Math, stanc3), then what they bundle
    def rank(item: tuple[str, list[str]]) -> tuple[int, str]:
        first = item[1][0]
        own = first in ("cmdstan-2.40.0/LICENSE", "cmdstan-2.40.0/stan/LICENSE.md",
                        "cmdstan-2.40.0/stan/lib/stan_math/LICENSE.md", "stanc3-v2.40.0/LICENSE.txt")
        return (0 if own else 1, first)

    parts = [
        "CmdStan 2.40.0 and the stanc3 2.40.0 compiler, with the libraries bundled in them, as the",
        "Bayesian models group ships them. Every text below is the file of the source, unchanged;",
        "the path is where the group ships it and the digest is the sha256 of the file.",
        "",
    ]
    for digest, paths in sorted(by_hash.items(), key=rank):
        parts.append(LINE)
        parts.extend(paths)
        parts.append(f"sha256 {digest}")
        parts.append("")
        parts.append((SHIPPED / paths[0]).read_text(encoding="utf-8").strip())
        parts.append("")
    return "\n".join(parts)


def check() -> int:
    problems = []
    if not OUTPUT.exists() or OUTPUT.read_text(encoding="utf-8") != assemble():
        problems.append(f"{OUTPUT.relative_to(ROOT)} is not what `write` produces from licence/")
    known = set(shipped())
    for digest in sorted(set(re.findall(r"\b[0-9a-f]{64}\b", SPEC.read_text(encoding="utf-8")))):
        # a digest of the spec that names a licence file must be one the group ships
        if digest not in known:
            context = [line for line in SPEC.read_text(encoding="utf-8").splitlines() if digest in line]
            if any("licence" in line.lower() or "LICENSE" in line for line in context):
                problems.append(f"{digest} in {SPEC.name} is not the sha256 of a shipped licence file")
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    if mode == "write":
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(assemble(), encoding="utf-8")
        print(f"{OUTPUT.relative_to(ROOT)}: {len(shipped())} texts, {sum(len(v) for v in shipped().values())} files")
        return 0
    if mode == "check":
        return check()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
