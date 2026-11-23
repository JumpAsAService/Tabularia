#!/usr/bin/env python3
"""Copies the distribution catalogue of the Bayesian editor to the components that read it, and
checks that the copies are the catalogue.

    python3 .github/scripts/bayes_catalog.py           # write the copies again
    python3 .github/scripts/bayes_catalog.py --check   # fail if one differs, write nothing

The catalogue is declared once, in Go, in gateway-go/internal/bayescatalog/declare.go; what the
other components read is its snapshot, gateway-go/internal/bayescatalog/catalog.json (embedded
by the gateway), written by:

    cd gateway-go && go run ./cmd/bayescatalog

(the Go suite fails when the snapshot is not what the declarations say:
gateway-go/internal/bayescatalog/catalog_test.go). Each component keeps a copy, because each is
built from its own folder alone:

  - frontend/assets/bayes-catalog.json: the browser's bindings, the expression language, the graph.

The AI service's copy (gateway/app/services/bayes_catalog.json) is added by the lot that builds
that service: add it to COPIES then.

--check fails when a copy is missing or differs from the snapshot by a byte, when the snapshot
is not a catalogue (no distribution, an id twice, a `uses` value or a constraint it does not
know), and when a distribution's id is not in the documentation
(docs/capabilities/bayesian-models.md, once that page exists).

Standard library only: this runs where nothing of Tabularia is installed (a bare Python
3.11+, in CI and on a laptop).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SNAPSHOT = "gateway-go/internal/bayescatalog/catalog.json"
COPIES = [
    "frontend/assets/bayes-catalog.json",
]
PAGE = "docs/capabilities/bayesian-models.md"
THIS = ".github/scripts/bayes_catalog.py"

USES = {"prior", "likelihood", "internal"}
CONSTRAINTS = {"real", "positive", "non_negative_integer", "vector", "cholesky_factor_cov"}


def distributions_of(snapshot: bytes) -> list[str]:
    """The ids of the distributions in a snapshot; ValueError when it is not a catalogue."""
    try:
        data = json.loads(snapshot.decode("utf-8"))
        dists = data["distributions"]
        ids = [d["id"] for d in dists]
        uses = [u for d in dists for u in d["uses"]]
        constraints = [p["constraint"] for d in dists for p in d["params"]]
        data["limits"], data["stan"], data["language"]
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as e:
        raise ValueError(f"{type(e).__name__}: {e}") from e
    if not ids or len(set(ids)) != len(ids) or not all(isinstance(i, str) and i for i in ids):
        raise ValueError("no distribution, or a distribution listed twice")
    if not set(uses) <= USES:
        raise ValueError(f"unknown use {sorted(set(uses) - USES)}")
    if not set(constraints) <= CONSTRAINTS:
        raise ValueError(f"unknown constraint {sorted(set(constraints) - CONSTRAINTS)}")
    return ids


def check(root: Path) -> list[str]:
    """What is wrong with the copies and the documentation, one sentence each."""
    errors: list[str] = []
    snapshot = (root / SNAPSHOT).read_bytes()
    ids = distributions_of(snapshot)
    for copy in COPIES:
        path = root / copy
        if not path.is_file():
            errors.append(f"{copy} is missing")
        elif path.read_bytes() != snapshot:
            errors.append(f"{copy} differs from {SNAPSHOT}")
    page = root / PAGE
    if page.is_file():
        text = page.read_text(encoding="utf-8")
        errors += [f"{PAGE} does not name the distribution `{i}`" for i in ids if f"`{i}`" not in text]
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if a copy differs from the snapshot, write nothing")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (tests)")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    hint = "write it with `go run ./cmd/bayescatalog` in gateway-go/"
    source = root / SNAPSHOT
    if not source.is_file():
        print(f"{SNAPSHOT} is missing: {hint}")
        return 1
    try:
        ids = distributions_of(source.read_bytes())
    except ValueError as e:
        print(f"{SNAPSHOT} is not a catalogue ({e}): {hint}")
        return 1

    noun = "distribution" if len(ids) == 1 else "distributions"
    if not args.check:
        snapshot = source.read_bytes()
        for copy in COPIES:
            path = root / copy
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(snapshot)
        print(f"{len(COPIES)} copies of {SNAPSHOT} written, {len(ids)} {noun}")

    errors = check(root)
    if errors:
        print("the distribution catalogue and what is made from it disagree:")
        for e in errors:
            print(f"  - {e}")
        print(f"fix with: python3 {THIS}" + ("" if all(PAGE not in e for e in errors) else f" (and name the distributions in {PAGE})"))
        return 1
    if args.check:
        noted = "" if (root / PAGE).is_file() else f"; {PAGE} is not there yet, the distributions' ids are not looked for in it"
        print(f"{len(COPIES)} copies of {SNAPSHOT}: up to date, {len(ids)} {noun}{noted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
