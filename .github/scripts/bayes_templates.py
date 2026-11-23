#!/usr/bin/env python3
"""Copies the templates of the Bayesian editor to the components that read them, and checks that
the copies are the file.

    python3 .github/scripts/bayes_templates.py           # write the copies again
    python3 .github/scripts/bayes_templates.py --check   # fail if one differs, write nothing

The templates are one JSON file, gateway-go/internal/bayestemplates/templates.json (embedded by
the gateway; docs/specs/bayes-templates.md). Each component keeps a copy, because each is built
from its own folder alone:

  - frontend/assets/bayes-templates.json: the editor's empty canvas and bayesOpenTemplate.

--check fails when a copy is missing or differs from the file by a byte, and when the file is not
what the copies' readers expect: a templates list with unique ids, a version of at least 1 on
each, every `scaled` entry naming a node of the template's graph, every `sources` key a source of
the file (or `spec`).

Standard library only: this runs where nothing of Tabularia is installed (a bare Python
3.11+, in CI and on a laptop).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SOURCE = "gateway-go/internal/bayestemplates/templates.json"
COPIES = [
    "frontend/assets/bayes-templates.json",
]
THIS = ".github/scripts/bayes_templates.py"

FAMILIES = {"glm", "hierarchical", "series"}
AS = {"center", "spread", "rate"}


def templates_of(source: bytes) -> list[str]:
    """The ids of the templates in a file; ValueError when it is not a templates file."""
    try:
        data = json.loads(source.decode("utf-8"))
        sources = data["sources"]
        templates = data["templates"]
        ids = [t["id"] for t in templates]
        problems: list[str] = []
        for t in templates:
            node_ids = {n["id"] for n in t["graph"]["nodes"]}
            if not isinstance(t["version"], int) or t["version"] < 1:
                problems.append(f"{t['id']}: version below 1")
            if t["family"] not in FAMILIES:
                problems.append(f"{t['id']}: unknown family {t['family']!r}")
            for s in t["scaled"]:
                if s["node"] not in node_ids or s["as"] not in AS:
                    problems.append(f"{t['id']}: scaled entry {s!r} names no node or no kind")
            for node, key in t["sources"].items():
                if node not in node_ids or (key != "spec" and key not in sources):
                    problems.append(f"{t['id']}: source of {node!r} is {key!r}")
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"{type(e).__name__}: {e}") from e
    if not ids or len(set(ids)) != len(ids) or not all(isinstance(i, str) and i for i in ids):
        raise ValueError("no template, or a template listed twice")
    if problems:
        raise ValueError("; ".join(problems))
    return ids


def check(root: Path) -> list[str]:
    """What is wrong with the copies, one sentence each."""
    errors: list[str] = []
    source = (root / SOURCE).read_bytes()
    for copy in COPIES:
        path = root / copy
        if not path.is_file():
            errors.append(f"{copy} is missing")
        elif path.read_bytes() != source:
            errors.append(f"{copy} differs from {SOURCE}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if a copy differs from the file, write nothing")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (tests)")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    path = root / SOURCE
    if not path.is_file():
        print(f"{SOURCE} is missing")
        return 1
    try:
        ids = templates_of(path.read_bytes())
    except ValueError as e:
        print(f"{SOURCE} is not a templates file ({e})")
        return 1

    noun = "template" if len(ids) == 1 else "templates"
    if not args.check:
        source = path.read_bytes()
        for copy in COPIES:
            target = root / copy
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(source)
        print(f"{len(COPIES)} copies of {SOURCE} written, {len(ids)} {noun}")

    errors = check(root)
    if errors:
        print("the templates and their copies disagree:")
        for e in errors:
            print(f"  - {e}")
        print(f"fix with: python3 {THIS}")
        return 1
    if args.check:
        print(f"{len(COPIES)} copies of {SOURCE}: up to date, {len(ids)} {noun}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
