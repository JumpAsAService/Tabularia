"""The blocks the two experiment nodes share (docs/specs/ab-testing.md, «The report (pixel perfect)»): `ab_test.py`
and `ab_plan.py` say what is their own and call these.

The explanation of the method (docs/specs/ab-testing.md, «Explaining the method in the report») comes as rows of the
section `reading`: the runner chooses, for each block that ran, the ids of the sentences that fit its verdicts and
options and writes them as «@id name=value…»; `common.with_labels` says each in the page's language
(`common.reading_said`: `reading_texts`, `formula_texts`, the numbers in their forms). The page only places them:
a reading of one to three sentences in italics under each block (`reading_under` for a block of the whole
experiment, `reading_table` for one with a sentence per treatment), and on the last pages «How this report was
computed», one paragraph per analysis that ran and the formula box (`how_computed`), which Excel keeps as its sheet
«Reading».

A measure's sentence never holds a verdict word: `measure_readings` and `verdict_words` of the words, checked by
the tests. No number is computed here: every one is read from the rows by its key.
"""
from __future__ import annotations

from typing import Any

from app.engine.models.layouts import common as c
from app.engine.stats import layouts as stats

READING_LINES = 8  # the sentences a block of the whole experiment may hold (the runner writes three at most)
FORMULA_PANEL = "#eef1f6"  # the formula box's ground: the panel's, a shade darker so that it reads as a box
FORMULA_RULE = "#c5ccd8"


# ── small expressions ───────────────────────────────────────────────────────
def key_part(x: str) -> str:
    """A label as a key holds it (a backslash doubled, a bar after a backslash: `output.key_part`)."""
    return f'REPLACE(REPLACE({x}, "\\", "\\\\"), "|", "\\|")'


def decoded(x: str) -> str:
    """A list the runner wrote as one text («B%2CD», «B,D»): its names, a comma and a space between them."""
    return f'REPLACE(REPLACE(REPLACE(REPLACE({x}, "%2C", ","), ",", ", "), "%20", " "), "%25", "%")'


def pct(x: str, pattern: str = "0.##") -> str:
    """A share as a percent: «4.1 %»."""
    return f'IF({x} < 0, "−", "") & FORMAT(ABS({x}) * 100, "{pattern}") & " %"'


def signed_pct(x: str, pattern: str = "0.#") -> str:
    """A lift as a percent with its sign: «+20.6 %», «−3.4 %»; nothing for none; «−∞» for an end Fieller leaves open
    is said by `end`."""
    return f'IF({x} < 0, "−", IF({x} > 0, "+", "")) & FORMAT(ABS({x}) * 100, "{pattern}") & " %"'


def end(x: str, side: str) -> str:
    """An end of an interval of a lift: «−∞» or «+∞» when it is open (no row: Fieller's interval is not bounded)."""
    return f'IF(ISNULL({x}), "{"−∞" if side == "low" else "+∞"}", {signed_pct(x)})'


def interval(low: str, high: str) -> str:
    """«[+6.8 %, +36.5 %]»."""
    return f'"[" & {end(low, "low")} & ", " & {end(high, "high")} & "]"'


def p_with_sign(x: str) -> str:
    """A p-value after «p» in a sentence: «= 0.0051», «< 0.0001» (the tables' four decimals)."""
    return f'IF({x} < 0.0001, "< " & {c.shown("0.0001", c.P4)}, "= " & {c.shown(x, c.P4)})'


def model_number(stat: str) -> str:
    return c.num(c.model_key(stat))


def model_text(stat: str) -> str:
    return c.txt(c.model_key(stat))


def of_variant(stat: str, variant: str = "[level]") -> str:
    """A number of a variant's row (section `variant`) by the variant's label (an expression)."""
    return f'LOOKUP("variant|" & {key_part(variant)} & "||{stat}", [key], [value])'


def of_level(section: str, stat: str, column: str = "value") -> str:
    """A number (or text) of the row at hand's level in another section with no level_2 (bayes, sequential)."""
    return f'LOOKUP("{section}|" & {key_part("[level]")} & "||{stat}", [key], [{column}])'


def of_comparison(stat: str, column: str = "value") -> str:
    """A number of the comparison of the row at hand's treatment with the control."""
    return (f'LOOKUP("comparison|" & {key_part("[level]")} & "|" & {key_part(model_text("control"))} & "|{stat}", '
            f'[key], [{column}])')


def said_by(w_values: dict, x: str = "[text]") -> str:
    """A runner's id said in the page's words (`values`), the id itself when the words do not have it."""
    return "IFS(" + ", ".join(f"{x} = {c.q(k)}, {c.q(v)}" for k, v in w_values.items()) + f", TRUE, {x})"


def verdict_of(check: str) -> str:
    return c.txt(f"assumption|{check}||verdict")


# ── the explanation ─────────────────────────────────────────────────────────
def reading_of(block: str, level_2: str | None = None, lines: int = READING_LINES) -> str:
    """The sentences of a block, one after the other: its rows line_1, line_2… («reading|<block>|<level_2>|line_k»)."""
    second = "" if level_2 is None else f'" & {key_part(level_2)} & "'
    parts = [f'LOOKUP("reading|{block}|{second}|line_{k}", [key], [text])' for k in range(1, lines + 1)]
    out = f'COALESCE({parts[0]}, "")'
    for x in parts[1:]:
        out += f' & IF(ISNULL({x}), "", " " & {x})'
    return out


def reading_under(p: stats.Page, w: dict, *blocks: str) -> None:
    """The reading of a block of the whole experiment (or of several, one after the other), under it: its sentences
    in italics, the word «Reading» first."""
    has = "=" + " OR ".join(c.exists(f'[key] = "reading|{b}||line_1"').lstrip("=") for b in blocks)
    said = ' & " " & '.join(reading_of(b, lines=READING_LINES if len(blocks) == 1 else 4) for b in blocks)
    p.y += 1.0
    p.text("reading_" + "_".join(blocks), "=" + c.q(w["reading"]["lead"] + " ") + " & TRIM(" + said + ")", h=5, size=8.5,
           italic=True, color=c.MUTED, can_grow=True, visible=has)
    p.y += 1.0


def reading_table(p: stats.Page, w: dict, block: str, id_: str, title: str) -> None:
    """The reading of a block that says a sentence or two for each treatment: one row a treatment."""
    one = f'[section] = "reading" AND [level] = "{block}" AND [stat] = "line_1"'
    lines = reading_of(block, "[level_2]", 4)
    t = stats.table(id_, title, one, [
        {"header": w["reading"]["variant"], "value": "[level_2]", "width": 24, "style": {"weight": "semibold"}},
        {"header": w["reading"]["text"], "value": lines, "style": {"italic": True, "size": 8}},
    ], p.y, visible=c.exists(one))
    t["style"] = {"size": 8}
    p.add(t, 10)


def how_computed(p: stats.Page, w: dict) -> None:
    """«How this report was computed»: a paragraph for each analysis that ran (block `method`, in the order the runner
    wrote them), then the formula box (block `formulas`): one table, which Excel keeps as the sheet «Reading»."""
    p.heading("s_how", w["sections"]["how"])
    p.text("how_lead", w["reading"]["how_lead"], h=5, size=8.5, color=c.MUTED, can_grow=True)
    p.keep()
    p.y += 1.0
    rows = '[section] = "reading" AND ([level] = "method" OR [level] = "formulas")'
    formula = '[level] = "formulas"'
    t = stats.table("reading", w["reading"]["title"], rows, [
        {"header": w["reading"]["part"], "value": "[label]", "width": 40, "style": {"weight": "semibold"},
         "rules": [{"when": formula, "style": {"background": FORMULA_PANEL, "size": 7.5, "weight": "normal"}}]},
        {"header": w["reading"]["text"], "value": "[text]", "style": {"size": 8.5},
         "rules": [{"when": formula, "style": {"background": FORMULA_PANEL, "size": 8, "border": {"width": 0.2, "color": FORMULA_RULE}}}]},
    ], p.y, visible=c.exists(rows))
    # the paragraphs first, then the formulas, each in the order the runner wrote them
    t["sort"] = [{"by": 'IF([level] = "formulas", 1, 0)'}, {"by": "[ord]"}]
    p.add(t, 10)


# ── the notes ────────────────────────────────────────────────────────────────
def notes(p: stats.Page, w: dict, used: str) -> None:
    """The notes: whose run, the units used and left out, the software; then the runner's notes in words."""
    n = w["notes"]
    p.heading("s_notes", w["sections"]["notes"])
    rows_from = model_text("rows_from")
    left_out = c.join(n[used], n=c.shown(model_number("n"), "#,##0"),
                      missing=c.shown(f"COALESCE({model_number('n_missing')}, 0)", "#,##0"))
    software = c.join(n["software"], software=model_text("software"), version=model_text("group_version"))
    p.text("notes_lines", f'=IF({rows_from} = "trial", {c.q(n["trial"])}, {c.q(n["run"])}) & "\n" & {left_out} & "\n" & {software}',
           h=14.5, can_grow=True)
    p.keep()
    p.y += 1.5
    is_note = '[section] = "model" AND STARTSWITH([stat], "note_")'
    p.add(stats.table("notes", n["title"], is_note, [{"header": n["note"], "value": "[text]"}], p.y, visible=c.exists(is_note)), 10)


def document(header: dict, items: list[dict], footer: dict, lang: str, calculated: list[dict] | None = None) -> dict:
    """The definition, with the dataset's calculated fields the page reads by (none by default)."""
    doc = stats.document(header, items, footer, lang)
    if calculated:
        doc["datasets"][0]["calculated"] = calculated
    return doc


def metric_name(params: dict[str, Any]) -> str:
    """The metric as the page names it: its column, or «clicks / impressions» for a ratio; with aggregated rows, the
    column of the sums."""
    if params.get("metric_type") == "ratio" and params.get("numerator") and params.get("denominator"):
        return f"{params['numerator']} / {params['denominator']}"
    return str(params.get("metric") or params.get("metric_sum") or "")
