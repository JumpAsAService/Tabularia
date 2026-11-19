"""The report of a hypothesis test: a layout preset per test, as a definition of the report
engine (docs/specs/reports.md), drawn from the rows the test wrote with `detail = report`.

    definition(test, params, locale, sample, step)   the definition: one dataset, `result`
    with_labels(test, rows, locale)                  the test's rows, each with the name of its number
                                                     (`label`) and, when it is very small, its text (`tiny`)
    samples_of(operations)                           the development samples a chain says of itself

The page does no computation of its own on the data: every number on it is read from the
test's rows, by their `key` (`LOOKUP("test|||p_value", [key], [value])`) or by their section
(a table's or a chart's `filter`). What the parameters decide is only what no row can: the
test's name in the page header, where no field may be read, and the names of the columns in
the charts' titles.

The words are in `report_texts.json`, one object per language, the same keys in each. Here
and not in the interface's catalogues: the page is drawn in the engine, and a PDF must not
depend on a browser. Only characters the PDF's font has (`app/reports/glyphs.py`).

A new test is an entry of `LAYOUTS` and its words under `tests.<id>`.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

from app.engine.stats.catalog import CURVE_POINTS, MAX_BINS, MAX_STAT_GROUPS, TESTS
from app.engine.stats.output import OUTPUT_COLUMNS

LOCALES = ("en", "it", "de", "es", "fr")
TEXTS = Path(__file__).with_name("report_texts.json")
DATASET = "result"
# the mark the editor puts on the operation that is a source's development sample
DEV_SAMPLE_MARK = "_dev_sample"

# The most rows a test writes for its report, which the route reads whole: the test's own
# (some 40), then for each of at most MAX_STAT_GROUPS groups its 12 numbers and up to
# MAX_BINS intervals, the three series of the curve, and 9 numbers for each of the 190 pairs
# of a post-hoc.
MAX_REPORT_ROWS = 64 + MAX_STAT_GROUPS * (12 + MAX_BINS) + 3 * CURVE_POINTS + 190 * 9

WIDTH = 186.0  # the body of an A4 page between its side margins, mm
ACCENT, INK, MUTED, RULE, PANEL, SAMPLE = "#3b5bdb", "#1f2933", "#6b7590", "#d9dde5", "#f1f3f5", "#8a5a00"
P4 = "0.0000"  # a number of the result: four decimals
EXACT = "0.##########"  # a number of the computation: ten decimals at most, none it does not have


@lru_cache(maxsize=1)
def all_texts() -> dict[str, dict]:
    return json.loads(TEXTS.read_text(encoding="utf-8"))


def texts(locale: str | None) -> dict:
    """The words of a language; English for one that is not there."""
    return all_texts().get(locale or "en") or all_texts()["en"]


# ── the names of the numbers ────────────────────────────────────────────────
_NOTE = re.compile(r"^note_(\d+)$")


def stat_label(test: str, section: str, stat: str, locale: str | None) -> str:
    """How a row's `stat` is called on the page: the test's own name for it, else the common
    one; its raw name when it has none (a number newer than the words)."""
    t = texts(locale)
    own = t["tests"].get(test, {}).get("stats", {}).get(section, {}).get(stat)
    if own:
        return own
    if section == "test" and (m := _NOTE.match(stat)):
        return t["note"].replace("{n}", m.group(1))
    return t["stats"].get(section, {}).get(stat) or stat


# below this a number that is not zero is written by its exponent: ten decimals would print 0
TINY = 1e-6


def tiny(value: Any, locale: str | None) -> str | None:
    """A very small number as the page writes it, «5.168702726e-12» (ten significant digits,
    the language's decimal mark), or nothing for a number ten decimals show. On a very
    large table a p-value is that small more often than not, and «0» would say it is zero."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value == 0 or abs(value) >= TINY:
        return None
    from app.reports import formats

    mantissa, exponent = f"{value:.9e}".split("e")
    mantissa = mantissa.rstrip("0").rstrip(".")
    return mantissa.replace(".", formats.locale(locale if locale in LOCALES else "en").decimal) + "e" + str(int(exponent))


def with_labels(test: str, rows: list[dict], locale: str | None) -> list[dict]:
    """The rows as the page reads them, with two more columns: `label`, the translated name
    of each `stat`, and `tiny`, a very small number written by its exponent (nothing for the
    others). Neither is a computation: they are how a row is said."""
    return [{**r, "label": stat_label(test, r["section"], r["stat"], locale), "tiny": tiny(r.get("value"), locale)} for r in rows]


def columns() -> list[dict[str, str]]:
    """The columns of the dataset `result`: the output's ten, `label` and `tiny`."""
    return [*({"name": name, "type": kind} for name, kind, _ in OUTPUT_COLUMNS),
            {"name": "label", "type": "String"}, {"name": "tiny", "type": "String"}]


# ── the development sample a chain says of itself ───────────────────────────
def samples_of(operations: Any) -> list[dict[str, Any]]:
    """The development samples of a chain, in the order they are met, in the main chain or
    in a branch (the right side of a join, a loop's driver): the operations the editor marked
    `_dev_sample` — a `limit` ({"first": n}) or a `sample` ({"fraction": f}). Nothing for a
    chain that reads its sources whole."""
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, list):
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            params = node.get("params")
            if isinstance(params, dict) and params.get(DEV_SAMPLE_MARK):
                n, fraction = params.get("n"), params.get("fraction")
                if node.get("type") == "limit" and _is_number(n):
                    found.append({"first": int(n)})
                elif node.get("type") == "sample" and _is_number(fraction):
                    found.append({"fraction": float(fraction)})
                else:  # marked, in a way this version does not know: still said
                    found.append({})
            for x in node.values():
                walk(x)

    walk(operations)
    out: list[dict[str, Any]] = []
    for s in found:  # two branches from the same source say it once
        if s not in out:
            out.append(s)
    return out


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def sample_line(sample: list[dict[str, Any]] | None, locale: str | None) -> str | None:
    """«Rows: a development sample of the source — the first 100,000 rows», or nothing."""
    if not sample:
        return None
    from app.reports import formats

    t, loc = texts(locale)["sample"], formats.locale(locale if locale in LOCALES else "en")
    parts = []
    for s in sample:
        if "first" in s:
            parts.append(t["first"].replace("{n}", formats.format_number(s["first"], "#,##0", loc)))
        elif "fraction" in s:
            parts.append(t["random"].replace("{percent}", formats.format_number(s["fraction"] * 100.0, "0.##", loc)))
    line = t["line"].replace("{what}", t["and"].join(dict.fromkeys(parts)))
    return line if parts else line.split(" — ")[0]


# ── expressions, written from the words ─────────────────────────────────────
def _q(text: str) -> str:
    """A text as the expressions write one."""
    return '"' + text.replace('"', '""') + '"'


def _value(key: str) -> str:
    """The number of the row `key`."""
    return f"LOOKUP({_q(key)}, [key], [value])"


def _text(key: str) -> str:
    return f"LOOKUP({_q(key)}, [key], [text])"


def _shown(expr: str, pattern: str | None = None) -> str:
    """A number as a text: with `pattern`, or as a result is written — four decimals, fewer
    for a large one, so that a difference of millions still fits its card."""
    if pattern:
        return f"FORMAT({expr}, {_q(pattern)})"
    return (f'IFS(ABS({expr}) >= 1000000, FORMAT({expr}, "#,##0"), ABS({expr}) >= 1000, FORMAT({expr}, "#,##0.00"), '
            f"FORMAT({expr}, {_q(P4)}))")


def _static(text: str) -> str:
    """A text that is not an expression, whatever it starts with (a step called «=1+1»)."""
    return "=" + _q(text) if text.startswith("=") else text


def _fit(text: str, size: float, width: float, weight: int = 400) -> str:
    """The text, cut with «…» to what a line `width` mm wide holds at `size` points: a text
    in the page header cannot grow, and a chart's title does not wrap."""
    from app.reports import glyphs

    if glyphs.width_mm(text, size, weight) <= width:
        return text
    while text and glyphs.width_mm(text + "…", size, weight) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def _join(template: str, **parts: str) -> str:
    """A sentence of the words as an expression: its text in quotes, and each `{name}`
    replaced by the expression that gives it."""
    out = []
    for i, piece in enumerate(re.split(r"\{(\w+)\}", template)):
        if i % 2:
            out.append(f"({parts[piece]})")
        elif piece:
            out.append(_q(piece))
    return " & ".join(out) or '""'


def _p_value(equal: str = "") -> str:
    """The p-value as a result is written: four decimals, and «< 0.0001» below that (never
    «0.0000»). `equal`: what goes before a p-value that is written whole («= » in a sentence)."""
    p = _value("test|||p_value")
    return f'IF({p} < 0.0001, "< " & {_shown("0.0001", P4)}, {_q(equal)} & {_shown(p, P4)})'


# ── the pieces of a page ────────────────────────────────────────────────────
class _Page:
    """The items of a body, written from the top: `y` is where the next one goes."""

    def __init__(self) -> None:
        self.items: list[dict] = []
        self.y = 0.0
        self.kept = ("", 0.0)

    def add(self, item: dict, advance: float = 0.0) -> None:
        self.items.append(item)
        self.y += advance

    def text(self, id: str, value: str, h: float = 5.0, x: float = 0.0, w: float = WIDTH, advance: bool = True,
             visible: str | None = None, **style: Any) -> None:
        item = {"id": id, "type": "text", "x": x, "y": self.y, "w": w, "h": h, "value": value, "style": style}
        if visible:
            item["visible"] = visible
        self.add(item, h if advance else 0.0)

    def heading(self, id: str, value: str) -> None:
        self.y += 3.0
        self.kept = (id, self.y)
        self.text(id, value, h=6.0, size=11, weight="bold", color=ACCENT)
        self.add({"id": id + "_rule", "type": "line", "x": 0, "y": self.y, "w": WIDTH, "h": 0,
                  "style": {"border": {"width": 0.25, "color": RULE}}}, 1.5)

    def keep(self) -> None:
        """Keeps on one page what was written since the last heading, the heading with it:
        the report engine never breaks a row of items, and items whose heights overlap are
        a row — so a rectangle nobody sees, as tall as all of them, makes them one. (Not
        over a text that grows unless it is the last: what is under it would not move.)"""
        id, top = self.kept
        self.items.append({"id": id + "_keep", "type": "rect", "x": 0, "y": top, "w": 0.1, "h": round(self.y - top, 2), "style": {}})


def _table(id: str, title: str, filter_: str, cols: list[dict], y: float, visible: str | None = None) -> dict:
    item = {"id": id, "type": "table", "dataset": DATASET, "x": 0, "y": y, "w": WIDTH, "h": 8, "title": title,
            "filter": filter_, "columns": cols, "sort": [{"by": "[ord]"}], "repeat_header": True,
            "style": {"size": 8}}
    if visible:
        item["visible"] = visible
    return item


def _group_number(stat: str) -> str:
    """A number of the group of the row at hand, with the decimals its column has room for
    (some twelve characters): eight, fewer for a large number (a mean near a billion keeps one)."""
    x = f'LOOKUP("group|" & [level] & "||{stat}", [key], [value])'
    return (f"IFS(ABS({x}) >= 1000000000, ROUND({x}, 1), ABS({x}) >= 1000000, ROUND({x}, 2), ABS({x}) >= 1000, ROUND({x}, 4), "
            f"ABS({x}) >= 10, ROUND({x}, 6), {x})")


def _series(t: dict, *stats: str) -> list[dict]:
    return [{"value": f'SUMIF([stat] = "{s}", [value])', "name": t["stats"]["group"][s]} for s in stats]


def _bands(name: str, subtitle: str, t: dict, loc_texts: dict, sample: str | None) -> tuple[dict, dict]:
    """The page header (the test, what it is about and the step's name, the sample line, the
    date, the page) and the page footer (who computed it, the sample line again, the time)."""
    page = _join(loc_texts["page"], page="PAGE()", pages="PAGES()")
    header_h = 19.0 if sample else 14.5
    header = [
        {"id": "h_test", "type": "text", "x": 0, "y": 0, "w": 136, "h": 7, "value": _static(_fit(name, 14, 135, 700)),
         "style": {"size": 14, "weight": "bold"}},
        {"id": "h_step", "type": "text", "x": 0, "y": 7.4, "w": 136, "h": 4.5, "value": _static(_fit(subtitle, 9, 135)),
         "style": {"size": 9, "color": MUTED}},
        {"id": "h_date", "type": "text", "x": 136, "y": 0.8, "w": 50, "h": 4.5, "value": '=FORMAT(NOW(), "d MMMM yyyy")',
         "style": {"size": 8, "align": "right", "color": MUTED}},
        {"id": "h_page", "type": "text", "x": 136, "y": 5.6, "w": 50, "h": 4.5, "value": "=" + page,
         "style": {"size": 8, "align": "right", "color": MUTED}},
        {"id": "h_rule", "type": "line", "x": 0, "y": header_h - 1.5, "w": WIDTH, "h": 0,
         "style": {"border": {"width": 0.4, "color": INK}}},
    ]
    if sample:
        header.insert(2, {"id": "h_sample", "type": "text", "x": 0, "y": 12.4, "w": WIDTH, "h": 4.5, "value": _static(_fit(sample, 8, WIDTH - 1, 600)),
                          "style": {"size": 8, "weight": "semibold", "color": SAMPLE}})
    left = t["footer"] + (" · " + sample if sample else "")
    footer = [
        {"id": "f_rule", "type": "line", "x": 0, "y": 1, "w": WIDTH, "h": 0, "style": {"border": {"width": 0.25, "color": RULE}}},
        {"id": "f_who", "type": "text", "x": 0, "y": 2.5, "w": 150, "h": 4.5, "value": _static(_fit(left, 7, 149)), "style": {"size": 7, "color": MUTED}},
        {"id": "f_when", "type": "text", "x": 150, "y": 2.5, "w": 36, "h": 4.5,
         "value": '=FORMAT(NOW(), "yyyy-MM-dd HH:mm") & " UTC"', "style": {"size": 7, "align": "right", "color": MUTED}},
    ]
    return {"height": header_h, "items": header}, {"height": 8, "items": footer}


def _document(header: dict, body: list[dict], footer: dict, locale: str) -> dict:
    return {
        "v": 1,
        "page": {"size": "A4", "orientation": "portrait", "margins": [12, 12, 12, 12]},
        "locale": locale,
        "timezone": "UTC",
        "theme": {"font": "IBM Plex Sans", "size": 9, "color": INK, "accent": ACCENT},
        "parameters": [],
        "datasets": [{"name": DATASET, "source": {}, "fields": columns()}],
        "sections": {"page_header": header, "body": {"items": body}, "page_footer": footer},
    }


# ── two groups: t_two_sample (and, with their lots, var_f, mann_whitney, ks_two_sample) ──
def _variant(test: str, params: dict[str, Any]) -> str | None:
    """The variant a test's parameters choose, as its `variant` row will say."""
    if test == "t_two_sample":
        return "student" if _param(test, params, "var_equal") else "welch"
    return None


def _param(test: str, params: dict[str, Any] | None, key: str) -> Any:
    """A parameter as the node has it, or the test's default."""
    value = (params or {}).get(key)
    return TESTS[test]["defaults"].get(key) if value is None else value


def test_name(test: str, params: dict[str, Any] | None, locale: str | None) -> str:
    """The test as the page's header names it: its variant's name when it has variants."""
    words = texts(locale)["tests"][test]
    return words.get("variants", {}).get(_variant(test, params or {}) or "", words["name"])


def subject(test: str, params: dict[str, Any] | None, locale: str | None) -> str:
    """What the test is about, from its columns: «amount by channel»."""
    p = params or {}
    if p.get("value") and p.get("group"):
        return texts(locale)["by"].replace("{value}", str(p["value"])).replace("{group}", str(p["group"]))
    return ", ".join(str(p[k]) for k in ("value", "x", "y") if p.get(k))


def title(test: str, params: dict[str, Any] | None, locale: str | None) -> str:
    """The report's name when the request gives none: «Welch's t test — amount by channel»."""
    about = subject(test, params, locale)
    return test_name(test, params, locale) + (" — " + about if about else "")


def _two_groups(test: str, params: dict[str, Any], locale: str, sample: str | None, step: str | None) -> dict:
    from app.reports import formats

    t = texts(locale)
    own = t["tests"][test]
    by = subject(test, params, locale)
    header, footer = _bands(test_name(test, params, locale), " · ".join(x for x in (by, step) if x), t,
                            formats.locale(locale).texts, sample)
    p = _Page()

    # the result: four cards and a sentence
    p.heading("s_result", t["sections"]["result"])
    cards = (
        ("k_statistic", own["kpi"]["statistic"], _shown(_value("test|||statistic")), None, 0, 40),
        ("k_df", t["kpi"]["df"], _value("test|||df"), "0.####", 44, 40),
        ("k_p", t["kpi"]["p_value"], _p_value(), None, 88, 40),
        ("k_estimate", own["kpi"]["estimate"], _shown(_value("test|||estimate")), None, 132, 54),
    )
    for id_, label, value, fmt, x, w in cards:
        card = {"id": id_, "type": "kpi", "dataset": DATASET, "x": x, "y": p.y, "w": w, "h": 20, "label": label, "value": value}
        if fmt:
            card["format"] = fmt
        p.add(card)
    low, high = _value("test|||ci_low"), _value("test|||ci_high")
    interval = _join(t["interval"], level=_shown(_value("test|||conf_level"), "0.##%"),
                     low=f'IF(ISNULL({low}), "−∞", {_shown(low)})', high=f'IF(ISNULL({high}), "+∞", {_shown(high)})')
    p.y += 14.2
    p.text("k_interval", "=" + interval, h=4.5, x=134.5, w=49, size=7.5, color=MUTED)
    p.y += 3.3
    p.keep()
    said = {d: _join(t["decision"][d], alpha=_shown(_value("test|||alpha"), "0.####"), p=_p_value("= "))
            for d in ("reject_h0", "fail_to_reject_h0")}
    p.text("decision", f'=IF({_text("test|||decision")} = "reject_h0", {said["reject_h0"]}, {said["fail_to_reject_h0"]})',
           h=6, size=10.5, weight="semibold")

    # the hypotheses
    p.heading("s_hypotheses", t["sections"]["hypotheses"])
    alternative = _text("test|||alternative")
    names = t["alternatives"]
    rows = (
        ("h0", t["stats"]["test"]["h0"], "=" + _text("test|||h0")),
        ("h1", t["stats"]["test"]["h1"], "=" + _text("test|||h1")),
        ("alternative", t["stats"]["test"]["alternative"],
         f'=IFS({alternative} = "less", {_q(names["less"])}, {alternative} = "greater", {_q(names["greater"])}, {_q(names["two_sided"])})'),
        ("alpha", t["stats"]["test"]["alpha"], "=" + _shown(_value("test|||alpha"), "0.####")),
    )
    for id_, label, value in rows:
        p.text(f"hy_{id_}_label", label, x=0, w=58, advance=False, color=MUTED)
        p.text(f"hy_{id_}", value, x=60, w=WIDTH - 60, can_grow=True)
        if id_ == "h0":
            p.keep()

    # how it is computed: the formula, what its symbols are, the distribution, every number
    p.heading("s_computed", t["sections"]["computed"])
    p.text("formula", "=" + _text("test|||formula"), h=6.5, size=9.5, weight="semibold", background=PANEL, padding=1.5,
           can_grow=True)
    p.keep()
    p.y += 1.5
    first, last = 'LOOKUP("group", [section], [level])', 'LOOKUP("group", [section], LAST([level]))'
    p.text("symbols", "=" + _join(own["symbols"], g1=first, g2=last), h=8.5, size=8, color=MUTED, can_grow=True)
    p.y += 0.5
    p.text("distribution", "=" + _join(t["distribution"], distribution=_text("test|||distribution")))
    c_low, c_high = _value("test|||critical_low"), _value("test|||critical_high")
    c = t["critical"]
    region = (f'IF(ISNULL({c_low}), "", {_join(c["low"], x=_shown(c_low))}) & '
              f'IF(ISNULL({c_low}) OR ISNULL({c_high}), "", {_q(c["or"])}) & '
              f'IF(ISNULL({c_high}), "", {_join(c["high"], x=_shown(c_high))})')
    p.text("critical", "=" + _join(c["region"], alpha=_shown(_value("test|||alpha"), "0.####"), region=region))
    p.y += 1.5
    numbers = t["tables"]["numbers"]
    p.add(_table("numbers", numbers["title"], '[section] = "test" AND NOT ISNULL([value])', [
        {"header": numbers["quantity"], "value": "[label]"},
        {"header": numbers["name"], "value": "[stat]", "width": 34, "style": {"color": MUTED}},
        {"header": numbers["value"], "value": "IF(ISNULL([tiny]), [value], [tiny])", "width": 40, "align": "right", "format": EXACT},
    ], p.y), 10)

    # the groups: the numbers of the formula, then what the box plot draws
    p.heading("s_groups", t["sections"]["groups"])
    g = t["stats"]["group"]
    one_row_a_group = '[section] = "group" AND [stat] = "n"'
    p.add(_table("groups", t["tables"]["groups"]["title"], one_row_a_group, [
        {"header": t["tables"]["groups"]["group"], "value": "[level]"},
        {"header": g["n"], "value": "[value]", "width": 14, "align": "right", "format": "#,##0"},
        *({"header": g[s], "value": _group_number(s), "width": 25, "align": "right", "format": "0.########"}
          for s in ("mean", "var", "sd", "se", "ci_low", "ci_high")),
    ], p.y), 10)
    p.add(_table("quartiles", t["tables"]["quartiles"]["title"], one_row_a_group, [
        {"header": t["tables"]["groups"]["group"], "value": "[level]"},
        *({"header": g[s], "value": _group_number(s), "width": 30, "align": "right", "format": "0.########"}
          for s in ("min", "q1", "median", "q3", "max")),
    ], p.y), 10)

    # the charts
    p.heading("s_charts", t["sections"]["charts"])
    half, gap, tall = 91.0, 4.0, 60.0
    ch = t["charts"]

    def chart(id_: str, kind: str, x: float, text: str, **more: Any) -> dict:
        # the title is drawn at 10 px, bold, on one line of the chart's width
        return {"id": id_, "type": "chart", "dataset": DATASET, "kind": kind, "x": x, "y": p.y, "w": half, "h": tall,
                "title": _fit(text, 7.5, half - 4, 700), **more}

    p.add(chart("c_box", "boxplot", 0, ch["boxplot"].replace("{by}", by), filter='[section] = "group"', category="[level]",
                series=_series(t, "min", "q1", "median", "q3", "max"), options={"format": "0.###"}))
    p.add(chart("c_means", "interval", half + gap, ch["interval"], filter='[section] = "group"', category="[level]",
                series=_series(t, "mean", "ci_low", "ci_high"), options={"labels": True, "format": "0.###"}), tall + 3)
    p.add(chart("c_hist", "column", 0, ch["histogram"].replace("{by}", by), filter='[section] = "hist"', category="[text]",
                split_by="[level]", series=[{"value": "SUM([value])", "name": t["stats"]["hist"]["count"]}],
                options={"legend": True, "format": "#,##0"}, visible='=COUNTIF([section] = "hist") > 0'))
    curve = '=COUNTIF([section] = "curve") > 0'
    # the first value of `split_by` in the rows' order is the line, the others the regions under it
    p.add(chart("c_curve", "density", half + gap, ch["density"], filter='[section] = "curve"', category="[x0]",
                split_by="[stat]", series=[{"value": "[value]", "name": t["stats"]["curve"]["density"]}],
                options={"legend": False, "target": _value("test|||statistic"), "format": "0.000"}, visible=curve), tall + 0.5)
    p.text("c_curve_caption", ch["density_caption"], h=4, x=half + gap, w=half, size=7, color=MUTED, visible=curve)
    p.keep()

    # the notes, the rows left out, the engine
    p.heading("s_notes", t["sections"]["notes"])
    p.text("engine", "=" + _join(t["engine"], engine=_text("test|||engine")))
    p.keep()
    p.text("left_out", "=" + _join(own["left_out"], n=_shown(_value("test|||n"), "#,##0"),
                                   missing=_shown(_value("test|||n_missing"), "#,##0")), can_grow=True)
    p.y += 1.5
    is_note = '[section] = "test" AND STARTSWITH([stat], "note_")'
    p.add(_table("notes", t["tables"]["notes"]["title"], is_note, [
        {"header": t["tables"]["notes"]["note"], "value": "[text]"},
    ], p.y, visible=f"=COUNTIF({is_note}) > 0"), 10)
    return _document(header, p.items, footer, locale)


LAYOUTS: dict[str, Callable[[str, dict[str, Any], str, str | None, str | None], dict]] = {
    "t_two_sample": _two_groups,
}


def definition(test: str, params: dict[str, Any] | None, locale: str | None = "en",
               sample: list[dict[str, Any]] | None = None, step: str | None = None) -> dict:
    """The report of the test `test` with the node's `params`, in a language: a definition
    of the report engine over one dataset, `result`, the test's rows with their labels.
    `sample`: what `samples_of` found in the chain; the page then says its rows are a
    development sample's, in its header and its footer. `step`: the name the person gave
    the step, written in the header after what the test is about."""
    code = locale if locale in LOCALES else "en"
    step = " ".join(str(step).split())[:120] if step else None
    return LAYOUTS[test](test, dict(params or {}), code, sample_line(sample, code), step)
