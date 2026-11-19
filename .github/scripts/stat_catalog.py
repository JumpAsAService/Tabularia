#!/usr/bin/env python3
"""Copies the catalogue of the hypothesis tests to the components that read it, and checks
that the copies are the catalogue.

    python3 .github/scripts/stat_catalog.py           # write the copies again
    python3 .github/scripts/stat_catalog.py --check   # fail if one differs, write nothing

The tests are declared once, in backend/app/engine/stats/catalog.py; what the other languages
read is its snapshot, backend/app/engine/stats/catalog.json, written in the engine's image:

    docker run --rm --user "$(id -u)" -v "$PWD/backend:/app" -w /app tabularia-backend:ci \
        python -m app.engine.stats.catalog

(the engine's suite fails when the snapshot is not what catalog.py says:
backend/tests/test_stats_catalog.py). Each component keeps a copy of the snapshot, because
each is built from its own folder alone:

  - frontend/assets/stat-catalog.json: the palette's group, the node's form, its defaults;
  - gateway-go/internal/statcatalog/catalog.json (embedded): which operations are statistics;
  - gateway/app/services/stat_catalog.json: the AI service's.

--check fails when a copy is missing or differs from the snapshot by a byte, when the
snapshot cannot be read as a catalogue, and when a test's id is not in the documentation
(docs/capabilities/statistics.md, once that page exists).

Standard library only: this runs where nothing of Tabularia is installed (a bare Python
3.11+, in CI and on a laptop).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SNAPSHOT = "backend/app/engine/stats/catalog.json"
COPIES = [
    "frontend/assets/stat-catalog.json",
    "gateway-go/internal/statcatalog/catalog.json",
    "gateway/app/services/stat_catalog.json",
]
PAGE = "docs/capabilities/statistics.md"
THIS = ".github/scripts/stat_catalog.py"


def tests_of(snapshot: bytes) -> list[str]:
    """The ids of the tests in a snapshot; ValueError when it is not a catalogue."""
    try:
        data = json.loads(snapshot.decode("utf-8"))
        tests = [t["id"] for t in data["tests"]]
        operations = [t["operation"] for t in data["tests"]]
        prefix = data["prefix"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as e:
        raise ValueError(f"{type(e).__name__}: {e}") from e
    if not tests or len(set(tests)) != len(tests) or not all(isinstance(t, str) and t for t in tests):
        raise ValueError("no test, or a test listed twice")
    if operations != [prefix + t for t in tests]:
        raise ValueError("an operation is not the prefix and its test's id")
    return tests


def check(root: Path) -> list[str]:
    """What is wrong with the copies and the documentation, one sentence each."""
    errors: list[str] = []
    snapshot = (root / SNAPSHOT).read_bytes()
    tests = tests_of(snapshot)
    for copy in COPIES:
        path = root / copy
        if not path.is_file():
            errors.append(f"{copy} is missing")
        elif path.read_bytes() != snapshot:
            errors.append(f"{copy} differs from {SNAPSHOT}")
    page = root / PAGE
    if page.is_file():
        text = page.read_text(encoding="utf-8")
        errors += [f"{PAGE} does not name the test `{t}`" for t in tests if f"`{t}`" not in text and f"`stat_{t}`" not in text]
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if a copy differs from the snapshot, write nothing")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (tests)")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    source = root / SNAPSHOT
    if not source.is_file():
        print(f"{SNAPSHOT} is missing: write it with `python -m app.engine.stats.catalog` in the engine's image")
        return 1
    try:
        tests = tests_of(source.read_bytes())
    except ValueError as e:
        print(f"{SNAPSHOT} is not a catalogue ({e}): write it again with `python -m app.engine.stats.catalog`")
        return 1

    if not args.check:
        snapshot = source.read_bytes()
        for copy in COPIES:
            path = root / copy
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(snapshot)
        print(f"{len(COPIES)} copies of {SNAPSHOT} written, {len(tests)} test{'' if len(tests) == 1 else 's'}")

    errors = check(root)
    if errors:
        print("the catalogue of the hypothesis tests and what is made from it disagree:")
        for e in errors:
            print(f"  - {e}")
        print(f"fix with: python3 {THIS}" + ("" if all(PAGE not in e for e in errors) else f" (and name the tests in {PAGE})"))
        return 1
    if args.check:
        noted = "" if (root / PAGE).is_file() else f"; {PAGE} is not there yet, the tests' ids are not looked for in it"
        print(f"{len(COPIES)} copies of {SNAPSHOT}: up to date, {len(tests)} test{'' if len(tests) == 1 else 's'}{noted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
