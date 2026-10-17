#!/usr/bin/env python3
"""Certify that a release image carries no package whose licence an AGPL-3.0 image cannot carry.

  check   lists the packages inside a built image — the Python distributions (backend,
          gateway) or the npm packages the server bundle ships (frontend) — reads the
          licence each declares, and exits 1 if one is proprietary or incompatible with
          AGPL-3.0: Proprietary, SSPL, BUSL, Commons Clause, Elastic License,
          non-commercial, GPL-2.0-only without an alternative, npm's UNLICENSED or
          «SEE LICENSE IN». Exits 2 when it cannot certify (no package found, the image
          does not run).

            python3 .github/scripts/image_licence_check.py check --kind python jumpasaservice/tabularia-backend:1.2.0-amd64
            python3 .github/scripts/image_licence_check.py check --kind npm jumpasaservice/tabularia-frontend:1.2.0-amd64

Why: Microsoft's SQL Server driver (mssql-python-odbc) can be redistributed only under
terms an AGPL image cannot meet, so the published images must never contain it
(docs/sqlserver.md). This check makes that, and any similar slip, fail the release.

What it reads is what packages DECLARE (the PEP 639 License-Expression first, else the
License field and the trove classifiers; npm's `license`). A package that declares
nothing is listed as a notice, not a failure. A package already reviewed by hand goes in
REVIEWED with the exact licence it declares: if that changes, it is checked again.
System packages of the base image (Debian, Alpine) are out of scope.
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

UNDECLARED = "no declared licence"

# (kind, name) -> (the licence exactly as declared, why it is fine)
REVIEWED: dict[tuple[str, str], tuple[str, str]] = {
    ("npm", "node-forge"): ("(BSD-3-Clause OR GPL-2.0)", "dual-licensed: used under BSD-3-Clause"),
}

# What an AGPL-3.0 image cannot carry, as words in an identifier or a free-text licence.
_DENIED = [
    (re.compile(r"proprietary", re.I), "proprietary"),
    (re.compile(r"\bSSPL\b|server side public license", re.I), "SSPL"),
    (re.compile(r"\bBUSL\b|business source license", re.I), "Business Source License"),
    (re.compile(r"commons[ -]clause", re.I), "Commons Clause"),
    (re.compile(r"\bElastic-2\.0\b|elastic license", re.I), "Elastic License"),
    (re.compile(r"non-?commercial|\bCC-BY(-SA)?-NC\b|-NC-", re.I), "non-commercial"),
    (re.compile(r"^UNLICENSED$|^SEE LICEN[CS]E IN\b", re.I), "no licence granted (npm)"),
    # GPL-2.0 alone is GPL-2.0-only in SPDX; «-or-later» and «+» are compatible, LGPL is not GPL
    (re.compile(r"(?<![A-Za-z])GPL-2\.0(-only)?$", re.I), "GPL-2.0-only"),
    (re.compile(r"(?<![A-Za-z])GPLv2$|GNU General Public License v2 \(GPLv2\)$", re.I), "GPL-2.0-only"),
]


def _denied(text: str) -> str | None:
    text = text.strip()
    for pattern, why in _DENIED:
        if pattern.search(text):
            return why
    return None


# ── SPDX expressions ────────────────────────────────────────────────────────────
class _NotAnExpression(ValueError):
    pass


def _tokens(text: str) -> list[str]:
    return re.findall(r"\(|\)|[^\s()]+", text)


def _parse(tokens: list[str], i: int = 0):
    """or-expression: and-expression (OR and-expression)*; returns (tree, next index)."""
    left, i = _parse_and(tokens, i)
    terms = [left]
    while i < len(tokens) and tokens[i].upper() == "OR":
        right, i = _parse_and(tokens, i + 1)
        terms.append(right)
    return (("OR", terms) if len(terms) > 1 else left), i


def _parse_and(tokens, i):
    left, i = _parse_with(tokens, i)
    terms = [left]
    while i < len(tokens) and tokens[i].upper() == "AND":
        right, i = _parse_with(tokens, i + 1)
        terms.append(right)
    return (("AND", terms) if len(terms) > 1 else left), i


def _parse_with(tokens, i):
    if i >= len(tokens):
        raise _NotAnExpression("unexpected end")
    tok = tokens[i]
    if tok == "(":
        tree, i = _parse(tokens, i + 1)
        if i >= len(tokens) or tokens[i] != ")":
            raise _NotAnExpression("unbalanced parentheses")
        return tree, i + 1
    if tok == ")" or tok.upper() in ("AND", "OR", "WITH"):
        raise _NotAnExpression(f"unexpected {tok}")
    if i + 1 < len(tokens) and tokens[i + 1].upper() == "WITH":
        if i + 2 >= len(tokens):
            raise _NotAnExpression("WITH without an exception")
        return ("ID", tok), i + 3  # an exception does not change whether the base is denied
    return ("ID", tok), i + 1


def _evaluate(tree) -> tuple[bool, str]:
    kind, value = tree
    if kind == "ID":
        why = _denied(value)
        return (why is None, f"{value}: {why}" if why else value)
    results = [_evaluate(t) for t in value]
    if kind == "AND":
        bad = [why for ok, why in results if not ok]
        return (not bad, "; ".join(bad))
    good = [why for ok, why in results if ok]  # OR: one acceptable alternative is enough
    return (bool(good), good[0] if good else "; ".join(why for _, why in results))


def expression_ok(text: str) -> tuple[bool, str]:
    """An SPDX expression, or else free text read for the denied words (its first line)."""
    text = (text or "").strip()
    tokens = _tokens(text)
    try:
        tree, end = _parse(tokens)
        if end != len(tokens):
            raise _NotAnExpression("trailing words")
        return _evaluate(tree)
    except _NotAnExpression:
        first = text.splitlines()[0][:200] if text else ""
        why = _denied(first)
        return (why is None, f"{first!r}: {why}" if why else first)


# ── packages ────────────────────────────────────────────────────────────────────
def declared(package: dict) -> str:
    """The licence as the package declares it, as one string (for REVIEWED and the log)."""
    if package.get("kind") == "npm":
        lic = package.get("license")
        if isinstance(lic, dict):
            return str(lic.get("type") or "")
        if isinstance(lic, list):
            types = [str(x.get("type") if isinstance(x, dict) else x) for x in lic]
            return " OR ".join(types)
        return str(lic or "")
    if package.get("expression"):
        return str(package["expression"])
    parts = [str(package.get("license") or "").strip().splitlines()[0] if package.get("license") else ""]
    parts += [c for c in package.get("classifiers") or []]
    return " | ".join(p for p in parts if p)


def verdict(package: dict) -> tuple[bool, str]:
    """(may ship, why). A package that declares nothing may ship, with UNDECLARED."""
    if package.get("kind") == "npm":
        text = declared(package)
        return expression_ok(text) if text.strip() else (True, UNDECLARED)
    if package.get("expression"):  # PEP 639: authoritative
        return expression_ok(package["expression"])
    checks = []
    if (package.get("license") or "").strip():
        checks.append(expression_ok(package["license"]))
    for classifier in package.get("classifiers") or []:
        name = classifier.split("::")[-1].strip()
        why = _denied(name)
        checks.append((why is None, f"{name}: {why}" if why else name))
    if not checks:
        return True, UNDECLARED
    bad = [why for ok, why in checks if not ok]
    return (not bad, "; ".join(bad) if bad else checks[0][1])


def check(packages: list[dict], reviewed: dict | None = None) -> tuple[list[dict], list[dict]]:
    """(failures, undeclared), each a list of packages with a `why`."""
    reviewed = REVIEWED if reviewed is None else reviewed
    failures, undeclared = [], []
    for p in packages:
        known = reviewed.get((p.get("kind"), p.get("name")))
        if known and known[0] == declared(p):
            continue
        ok, why = verdict(p)
        if not ok:
            failures.append({**p, "why": why})
        elif why == UNDECLARED:
            undeclared.append({**p, "why": why})
    return failures, undeclared


# ── collecting from an image ────────────────────────────────────────────────────
PYTHON_COLLECTOR = r"""
import importlib.metadata as m, json
out = []
for d in m.distributions():
    md = d.metadata
    lic = md.get("License") or ""
    out.append({"kind": "python", "name": md.get("Name") or "", "version": d.version,
                "expression": md.get("License-Expression"), "license": lic[:2000],
                "classifiers": [c for c in (md.get_all("Classifier") or []) if c.startswith("License ::")]})
print(json.dumps(out))
"""

# the packages Nitro copies next to the server bundle: every package.json directly
# inside a node_modules folder (scoped too), under the roots given
NPM_COLLECTOR = r"""
const fs = require('fs'), path = require('path')
const out = [], seen = new Set()
function pkg(dir) {
  try {
    const j = JSON.parse(fs.readFileSync(path.join(dir, 'package.json'), 'utf8'))
    if (!j.name) return
    const key = j.name + '@' + (j.version || '')
    if (seen.has(key)) return
    seen.add(key)
    out.push({ kind: 'npm', name: j.name, version: j.version || '', license: j.license ?? j.licenses ?? null })
  } catch (e) {}
}
function walk(dir, depth) {
  let entries
  try { entries = fs.readdirSync(dir, { withFileTypes: true }) } catch (e) { return }
  for (const e of entries) {
    if (!e.isDirectory()) continue
    const full = path.join(dir, e.name)
    if (path.basename(dir) === 'node_modules') {
      if (e.name.startsWith('@')) {
        for (const s of fs.readdirSync(full, { withFileTypes: true })) if (s.isDirectory()) { pkg(path.join(full, s.name)); walk(path.join(full, s.name), depth + 1) }
      } else { pkg(full); walk(full, depth + 1) }
    } else if (depth < 12) walk(full, depth + 1)
  }
}
for (const root of process.argv.slice(1)) walk(root, 0)
console.log(JSON.stringify(out))
"""


def collect(image: str, kind: str, roots: list[str]) -> list[dict]:
    base = ["docker", "run", "--rm", "--read-only", "--network", "none", "--user", "10001:10001"]
    if kind == "python":
        cmd = base + ["--entrypoint", "python", image, "-I", "-c", PYTHON_COLLECTOR]
    else:
        cmd = base + ["--entrypoint", "node", image, "-e", NPM_COLLECTOR, *roots]
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if done.returncode != 0:
        raise RuntimeError(f"listing the packages of {image} failed: {done.stderr.strip()[-500:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("check", help="fail on a package an AGPL-3.0 image cannot carry")
    c.add_argument("image")
    c.add_argument("--kind", choices=("python", "npm"), default="python")
    c.add_argument("--root", action="append", default=None,
                   help="npm: folders to search for node_modules (default /app and /usr/local/lib/node_modules)")
    c.add_argument("--from-json", help="read a listing saved before instead of running the image (tests)")
    args = parser.parse_args(argv)

    if args.image.startswith("-"):
        print(f"::error::{args.image!r} is not an image name")
        return 2
    try:
        if args.from_json:
            with open(args.from_json, encoding="utf-8") as f:
                packages = json.load(f)
        else:
            packages = collect(args.image, args.kind, args.root or ["/app", "/usr/local/lib/node_modules"])
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as e:
        print(f"::error::cannot list the packages of {args.image}: {e}")
        return 2
    if not packages:
        print(f"::error::no package found in {args.image}: nothing to certify")
        return 2

    failures, undeclared = check(packages)
    for p in undeclared:
        print(f"::notice::{p['name']}: {UNDECLARED} (read its licence file by hand if it is new)")
    for p in failures:
        print(f"::error::{p['name']} {p.get('version', '')} cannot ship in an AGPL-3.0 image: {p['why']}")
    print(f"{args.image}: {len(packages)} packages, {len(failures)} refused, {len(undeclared)} without a declared licence")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
