"""How documented a datasource is, and when it can be called «ready».

The assistant reads the TABLE's description to understand whether a question concerns
it, and the FIELDS' descriptions to write the right query. Without them, the model
guesses from the column's name — and that is exactly how on 2026-09-19 it made up the
content of a table it had never read.

The judgement sits here and not in the pages because three different places show it
(catalog, the assistant's rail, search): written three times, it would diverge at the
first change.
"""
from __future__ import annotations

from typing import Any


def count_described(columns: list[Any], descriptions: dict[str, str]) -> tuple[int, int]:
    """(described fields, total fields).

    A description may sit in the hand-curated dictionary or already inside the column:
    either holds, and spaces do not count as a description."""
    totals = 0
    described = 0
    for c in columns:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        totals += 1
        text = descriptions.get(c["name"]) or c.get("description") or ""
        if isinstance(text, str) and text.strip():
            described += 1
    return described, totals


def is_ai_ready(description: str | None, described: int, totals: int) -> bool:
    """Ready = the table says what it is, and EVERY field says what it means.

    Both are needed: a table with all the fields described but without a description of
    its own does not tell the assistant *what it is about*, and one described with half
    its fields anonymous leaves it guessing on the rest. Zero columns is not «ready»: it is
    a datasource we know nothing about yet."""
    return bool((description or "").strip()) and totals > 0 and described == totals
