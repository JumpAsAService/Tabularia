#!/usr/bin/env python3
"""Fails when a test was skipped for a reason the CI does not expect.

A test that skips because its server is missing passes silently: in CI every server the
suite can use is there, so a skip means a broken setup, not a missing service. Only the
reasons given here may skip (services the CI cannot have: BigQuery, Oracle, SQL Server).

    python3 ci_skips.py junit.xml [REGEX …]

Standard library only.
"""
from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET


def skipped(junit: str) -> list[tuple[str, str]]:
    out = []
    for case in ET.parse(junit).iter("testcase"):
        for s in case.findall("skipped"):
            out.append((f"{case.get('classname')}::{case.get('name')}", s.get("message") or ""))
    return out


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    junit, allowed = argv[0], [re.compile(a) for a in argv[1:]]
    found = skipped(junit)
    unexpected = [(t, m) for t, m in found if not any(a.search(m) for a in allowed)]
    print(f"{len(found)} skipped, {len(found) - len(unexpected)} for an expected reason")
    for test, message in unexpected:
        print(f"::error::{test} was skipped: {message}")
    return 1 if unexpected else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
