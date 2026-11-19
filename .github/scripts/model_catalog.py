#!/usr/bin/env python3
"""Copies the catalogue of the statistical models to the components that read it, and checks
that the copies are the catalogue.

    python3 .github/scripts/model_catalog.py           # write the copies again
    python3 .github/scripts/model_catalog.py --check   # fail if one differs, write nothing

The groups and models are declared once, in backend/app/engine/models/catalog.py; what the
other languages read is its snapshot, backend/app/engine/models/catalog.json, written in
the engine's image:

    docker run --rm --user "$(id -u)" -v "$PWD/backend:/app" -w /app tabularia-backend:ci \
        python -m app.engine.models.catalog

(the engine's suite fails when the snapshot is not what catalog.py says:
backend/tests/test_models_catalog.py). Each component keeps a copy, because each is built
from its own folder alone:

  - frontend/assets/model-catalog.json: the palette's group, the node's form, its variants;
  - gateway-go/internal/modelcatalog/catalog.json (embedded): the parameters' check, the
    variant, the copy the model Jobs receive in their ConfigMap;
  - gateway/app/services/model_catalog.json: the assistants'.

--check fails when a copy is missing or differs from the snapshot by a byte, when the
snapshot cannot be read as a catalogue, and when a model's id is not in the documentation
(docs/capabilities/statistical-models.md, once that page exists).

Standard library only: this runs where nothing of Tabularia is installed (a bare Python
3.11+, in CI and on a laptop).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

SNAPSHOT = "backend/app/engine/models/catalog.json"
COPIES = [
    "frontend/assets/model-catalog.json",
    "gateway-go/internal/modelcatalog/catalog.json",
    "gateway/app/services/model_catalog.json",
]
PAGE = "docs/capabilities/statistical-models.md"
THIS = ".github/scripts/model_catalog.py"


def models_of(snapshot: bytes) -> list[str]:
    """The ids of the models in a snapshot; ValueError when it is not a catalogue."""
    try:
        data = json.loads(snapshot.decode("utf-8"))
        models = [m["id"] for m in data["models"]]
        groups = {g["id"]: g["models"] for g in data["groups"]}
        of_model = {m["id"]: m["group"] for m in data["models"]}
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError) as e:
        raise ValueError(f"{type(e).__name__}: {e}") from e
    if not models or len(set(models)) != len(models) or not all(isinstance(m, str) and m for m in models):
        raise ValueError("no model, or a model listed twice")
    for model, group in of_model.items():
        if model not in groups.get(group, []):
            raise ValueError(f"the model {model} is not listed by its group {group}")
    if sorted(m for ms in groups.values() for m in ms) != sorted(models):
        raise ValueError("a group lists a model the catalogue does not have")
    return models


def check(root: Path) -> list[str]:
    """What is wrong with the copies and the documentation, one sentence each."""
    errors: list[str] = []
    snapshot = (root / SNAPSHOT).read_bytes()
    models = models_of(snapshot)
    for copy in COPIES:
        path = root / copy
        if not path.is_file():
            errors.append(f"{copy} is missing")
        elif path.read_bytes() != snapshot:
            errors.append(f"{copy} differs from {SNAPSHOT}")
    page = root / PAGE
    if page.is_file():
        text = page.read_text(encoding="utf-8")
        errors += [f"{PAGE} does not name the model `{m}`" for m in models if f"`{m}`" not in text]
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--check", action="store_true", help="fail if a copy differs from the snapshot, write nothing")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (tests)")
    args = parser.parse_args(argv)
    root = args.root.resolve()

    source = root / SNAPSHOT
    if not source.is_file():
        print(f"{SNAPSHOT} is missing: write it with `python -m app.engine.models.catalog` in the engine's image")
        return 1
    try:
        models = models_of(source.read_bytes())
    except ValueError as e:
        print(f"{SNAPSHOT} is not a catalogue ({e}): write it again with `python -m app.engine.models.catalog`")
        return 1

    if not args.check:
        snapshot = source.read_bytes()
        for copy in COPIES:
            path = root / copy
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(snapshot)
        print(f"{len(COPIES)} copies of {SNAPSHOT} written, {len(models)} model{'' if len(models) == 1 else 's'}")

    errors = check(root)
    if errors:
        print("the catalogue of the statistical models and what is made from it disagree:")
        for e in errors:
            print(f"  - {e}")
        print(f"fix with: python3 {THIS}" + ("" if all(PAGE not in e for e in errors) else f" (and name the models in {PAGE})"))
        return 1
    if args.check:
        noted = "" if (root / PAGE).is_file() else f"; {PAGE} is not there yet, the models' ids are not looked for in it"
        print(f"{len(COPIES)} copies of {SNAPSHOT}: up to date, {len(models)} model{'' if len(models) == 1 else 's'}{noted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
