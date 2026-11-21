"""The blocks the two experiment nodes share (docs/specs/ab-testing.md, «The report (pixel perfect)»): `ab_test.py`
and `ab_plan.py` say what is their own and call these.

The explanation of the method (docs/specs/ab-testing.md, «Explaining the method in the report») comes as rows of the
section `reading`, which the runner writes («@id name=value…»); the pieces that place it on the page (`reading_under`,
`reading_table`, `how_computed`) are every report's and live in `common.py` (docs/specs/report-explanations.md).
No number is computed here: every one is read from the rows by its key.
"""
from __future__ import annotations

from typing import Any

from app.engine.models.layouts import common as c
from app.engine.stats import layouts as stats

ABOUT_CHARS = 160  # the header's line of what a report is about: a column's name or a control's value of any length is cut


# ── small expressions ───────────────────────────────────────────────────────
def decoded(x: str) -> str:
    """A list the runner wrote as one text («B%2CD», «B,D»): its names, a comma and a space between them."""
    return f'REPLACE(REPLACE(REPLACE(REPLACE({x}, "%2C", ","), ",", ", "), "%20", " "), "%25", "%")'


def end(x: str, side: str) -> str:
    """An end of an interval of a lift: «−∞» or «+∞» when it is open (no row: Fieller's interval is not bounded)."""
    return f'IF(ISNULL({x}), "{"−∞" if side == "low" else "+∞"}", {c.signed_pct(x)})'


def interval(low: str, high: str) -> str:
    """«[+6.8 %, +36.5 %]»."""
    return f'"[" & {end(low, "low")} & ", " & {end(high, "high")} & "]"'


def of_variant(stat: str, variant: str = "[level]") -> str:
    """A number of a variant's row (section `variant`) by the variant's label (an expression)."""
    return f'LOOKUP("variant|" & {c.key_part(variant)} & "||{stat}", [key], [value])'


def of_level(section: str, stat: str, column: str = "value") -> str:
    """A number (or text) of the row at hand's level in another section with no level_2 (bayes, sequential)."""
    return f'LOOKUP("{section}|" & {c.key_part("[level]")} & "||{stat}", [key], [{column}])'


def of_comparison(stat: str, column: str = "value") -> str:
    """A number of the comparison of the row at hand's treatment with the control."""
    return (f'LOOKUP("comparison|" & {c.key_part("[level]")} & "|" & {c.key_part(c.model_text("control"))} & "|{stat}", '
            f'[key], [{column}])')


def said_by(w_values: dict, x: str = "[text]") -> str:
    """A runner's id said in the page's words (`values`), the id itself when the words do not have it."""
    return "IFS(" + ", ".join(f"{x} = {c.q(k)}, {c.q(v)}" for k, v in w_values.items()) + f", TRUE, {x})"


def verdict_of(check: str) -> str:
    return c.txt(f"assumption|{check}||verdict")


# ── the notes ────────────────────────────────────────────────────────────────
def notes(p: stats.Page, w: dict, used: str) -> None:
    """The notes: whose run, the units used and left out, the software; then the runner's notes in words."""
    n = w["notes"]
    p.heading("s_notes", w["sections"]["notes"])
    rows_from = c.model_text("rows_from")
    left_out = c.join(n[used], n=c.shown(c.model_number("n"), "#,##0"),
                      missing=c.shown(f"COALESCE({c.model_number('n_missing')}, 0)", "#,##0"))
    software = c.join(n["software"], software=c.model_text("software"), version=c.model_text("group_version"))
    p.text("notes_lines", f'=IF({rows_from} = "trial", {c.q(n["trial"])}, {c.q(n["run"])}) & "\n" & {left_out} & "\n" & {software}',
           h=14.5, can_grow=True)
    p.keep()
    p.y += 1.5
    is_note = '[section] = "model" AND STARTSWITH([stat], "note_")'
    p.add(stats.table("notes", n["title"], is_note, [{"header": n["note"], "value": "[text]"}], p.y, visible=c.exists(is_note)), 10)


def one_line(text: str) -> str:
    """What a report is about, on one line and at most ABOUT_CHARS long: the header's line is fitted to its width
    character by character (stats.fit), and a name of thousands of characters would take seconds."""
    text = " ".join(text.split())
    return text if len(text) <= ABOUT_CHARS else text[: ABOUT_CHARS - 1].rstrip() + "…"


def metric_name(params: dict[str, Any]) -> str:
    """The metric as the page names it: its column, or «clicks / impressions» for a ratio; with aggregated rows, the
    column of the sums."""
    if params.get("metric_type") == "ratio" and params.get("numerator") and params.get("denominator"):
        return f"{params['numerator']} / {params['denominator']}"
    return str(params.get("metric") or params.get("metric_sum") or "")
