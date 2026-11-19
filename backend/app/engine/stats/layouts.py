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
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, NamedTuple

from app.engine.stats.catalog import CURVE_POINTS, MAX_BINS, MAX_STAT_GROUPS, MAX_STAT_LEVELS, TESTS, label_of
from app.engine.stats.compute import paired_difference_label
from app.engine.stats.output import OUTPUT_COLUMNS

LOCALES = ("en", "it", "de", "es", "fr")
TEXTS = Path(__file__).with_name("report_texts.json")
DATASET = "result"
# the mark the editor puts on the operation that is a source's development sample
DEV_SAMPLE_MARK = "_dev_sample"

# The most rows a test writes for its report, which the route reads whole: the test's own
# (some 45), then for each of at most MAX_STAT_GROUPS groups its 13 numbers and up to
# MAX_BINS intervals, the three series of the curve, and the 11 rows of each of the 190 pairs
# of a post-hoc; or, for a contingency table of MAX_STAT_LEVELS categories a side, the 9 rows
# of each of its 2,500 cells (with the post-hoc of the cells) and the 4 of each margin.
MAX_REPORT_ROWS = max(64 + MAX_STAT_GROUPS * (13 + MAX_BINS) + 3 * CURVE_POINTS + 190 * 11,
                      64 + MAX_STAT_LEVELS * MAX_STAT_LEVELS * 9 + 4 * MAX_STAT_LEVELS + 3 * CURVE_POINTS)

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


def _edges_text(low: float, high: float, last: bool, decimal: str, group: str) -> str:
    """A histogram interval as the page writes it, «[59,780 – 258,011)»: the decimals the
    interval's width needs to tell its two edges apart, the language's separators. The
    output's own text keeps six significant digits («[1.05094e+06, 1.24917e+06]»), which
    a chart's axis has no room for and no language writes."""
    width = abs(high - low)
    places = 0 if not math.isfinite(width) or width <= 0 else max(0, min(10, 1 - math.floor(math.log10(width))))

    def said(x: float) -> str:
        whole, _, fraction = f"{x + 0.0:,.{places}f}".partition(".")
        fraction = fraction.rstrip("0")
        return whole.replace(",", group) + (decimal + fraction if fraction else "")

    return f"[{said(low)} – {said(high)}{']' if last else ')'}"


def _said(test: str, rows: list[dict], locale: str | None) -> list[dict]:
    """The texts of the rows that hold a number written as text, in the language of the page:
    the histogram's intervals, and the reference distribution («t(12,7279)»; with a decimal
    comma its parameters are set apart by a semicolon, «F(2; 18,5)»)."""
    from app.reports import formats

    loc = formats.locale(locale if locale in LOCALES else "en")
    out = []
    for r in rows:
        text = r.get("text")
        if r["section"] == "hist" and isinstance(r.get("x0"), (int, float)) and isinstance(r.get("x1"), (int, float)):
            r = {**r, "text": _edges_text(r["x0"], r["x1"], str(text or "").endswith("]"), loc.decimal, loc.group)}
        elif r["section"] == "test" and r["stat"] == "distribution" and isinstance(text, str) and loc.decimal != ".":
            r = {**r, "text": text.replace(", ", "; ").replace(".", loc.decimal)}
        out.append(r)
    return out


def with_labels(test: str, rows: list[dict], locale: str | None) -> list[dict]:
    """The rows as the page reads them, with two more columns: `label`, the translated name
    of each `stat`, and `tiny`, a very small number written by its exponent (nothing for the
    others); the numbers written as text are in the page's language (`_said`). None of it is
    a computation: it is how a row is said."""
    return [{**r, "label": stat_label(test, r["section"], r["stat"], locale), "tiny": tiny(r.get("value"), locale)}
            for r in _said(test, rows, locale)]


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


def _with_stars(said: str, code: str) -> str:
    """A p-value as written, its significance code after it («0.0005 ***»): `code` the
    expression of the row's code, nothing when it is empty (p ≥ 0.1)."""
    return f'{said} & IF(ISNULL({code}) OR {code} = "", "", " " & {code})'


def _signif_legend(t: dict, locale: str) -> str:
    """R's legend of the significance codes, with the language's decimal mark:
    «Signif. codes: 0 ‘***’ 0.001 ‘**’ 0.01 ‘*’ 0.05 ‘.’ 0.1 ‘ ’ 1»."""
    from app.reports import formats

    d = formats.locale(locale if locale in LOCALES else "en").decimal
    codes = f"0 ‘***’ 0{d}001 ‘**’ 0{d}01 ‘*’ 0{d}05 ‘.’ 0{d}1 ‘ ’ 1"
    return t["signif"]["legend"].replace("{codes}", codes)


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


def _row_key(section: str, stat: str, two_levels: bool = False) -> str:
    """The key of the row `stat` of the group (or cell, pair) of the row at hand: its section,
    its `level` (and `level_2` for a pair or a cell of a contingency table)."""
    second = '"|" & [level_2] & "|' if two_levels else '"||'
    return f'"{section}|" & [level] & {second}{stat}"'


def _rounded(x: str) -> str:
    """A number with the decimals a column of some twelve characters has room for: eight,
    fewer for a large one (a mean near a billion keeps one)."""
    return (f"IFS(ABS({x}) >= 1000000000, ROUND({x}, 1), ABS({x}) >= 1000000, ROUND({x}, 2), ABS({x}) >= 1000, ROUND({x}, 4), "
            f"ABS({x}) >= 10, ROUND({x}, 6), {x})")


def _group_number(stat: str, section: str = "group", two_levels: bool = False) -> str:
    """A number of the group (or cell) of the row at hand, rounded to its column (`_rounded`);
    a very small one by its exponent, as the table of the numbers writes it."""
    key = _row_key(section, stat, two_levels)
    x = f"LOOKUP({key}, [key], [value])"
    if section == "group":
        return _rounded(x)
    tiny_ = f"LOOKUP({key}, [key], [tiny])"
    return f"IF(ISNULL({tiny_}), {_rounded(x)}, {tiny_})"


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


# ── the tests' names, what they are about ───────────────────────────────────
def _variant(test: str, params: dict[str, Any]) -> str | None:
    """The variant a test's parameters choose, as its `variant` row will say."""
    if test == "t_two_sample":
        return "student" if _param(test, params, "var_equal") else "welch"
    if test == "anova":
        return "one_way" if _param(test, params, "var_equal") else "welch"
    if test == "levene":
        return _param(test, params, "center")
    if test in ("prop_one_sample", "normality"):
        return _param(test, params, "method")
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
    """What the test is about, from its columns: «amount by channel», «before − after»."""
    p = params or {}
    if test.startswith("prop_") and p.get("value") and p.get("success") is not None:
        p = {**p, "value": f"{p['value']} = {p['success'] if isinstance(p['success'], str) else label_of(p['success'])}"}
    if p.get("value") and p.get("group"):
        return texts(locale)["by"].replace("{value}", str(p["value"])).replace("{group}", str(p["group"]))
    if test in ("t_paired", "wilcoxon_signed_rank") and p.get("x") and p.get("y"):
        return paired_difference_label(str(p["x"]), str(p["y"]))
    return ", ".join(str(p[k]) for k in ("value", "x", "y") if p.get(k))


def title(test: str, params: dict[str, Any] | None, locale: str | None) -> str:
    """The report's name when the request gives none: «Welch's t test — amount by channel»."""
    about = subject(test, params, locale)
    return test_name(test, params, locale) + (" — " + about if about else "")


# ── one page, made of the blocks a test has ─────────────────────────────────
class _Card(NamedTuple):
    """A card of the result; a place of the cards may hold alternatives, each `visible` when
    its rows say (a 2 × 2 table's odds ratio, a larger table's size)."""

    id: str
    label: str
    value: str
    format: str | None = None
    visible: str | None = None


class _Table(NamedTuple):
    """A table of the groups: one row a group, a column for each stat (its header the stat's
    name on the page), `width` mm each. A table of other rows says which: `section` (a
    contingency table's cells), `rows` (the filter that gives one row each), `labels` (the
    columns that name a row: (header, expression)), `two_levels` (a cell of two categories);
    `counts`: the stats that are whole numbers, narrower."""

    id: str
    title: str
    stats: tuple[str, ...]
    width: float = 40.0
    n_first: bool = False  # the rows' count first, narrower
    section: str = "group"
    rows: str | None = None
    labels: tuple[tuple[str, str], ...] | None = None
    two_levels: bool = False
    counts: tuple[str, ...] = ()
    extra: tuple[dict, ...] = ()  # columns of words after the numbers (a cell concordant or discordant)
    visible: str | None = None  # a table some variants of a test have no rows for


class _Comparisons(NamedTuple):
    """The comparisons of a post-hoc: of the pairs of groups (`section` «pair») or of the cells
    of a table («cell»). `numbers`: the stats of the first table (none: no such table);
    `own_p`: each row has a p-value of its own besides the adjusted one; `intervals`: the
    simultaneous intervals, a table and a chart; `method`, `adjust`: the keys of the words
    that say how (`posthoc.method`, `posthoc.adjust` or, without intervals, `posthoc.adjust_only`)."""

    method: str
    adjust: str
    numbers: tuple[str, ...]
    own_p: bool
    intervals: bool
    section: str = "pair"


class _Chart(NamedTuple):
    """A chart of a page; of kind «note», a text in place of a chart (its `title` the text)."""

    id: str
    kind: str
    title: str
    more: dict


def _df_card(t: dict, two: bool) -> _Card:
    """The degrees of freedom: one number, or the F's two («2, 18»; «2; 18» where the decimal
    mark is a comma)."""
    if not two:
        return _Card("k_df", t["kpi"]["df"], _value("test|||df"), "0.####")
    return _Card("k_df", t["kpi"]["df"], "=" + _join(t["df_pair"], d1=_shown(_value("test|||df_1"), "0.####"),
                                                    d2=_shown(_value("test|||df_2"), "0.####")))


def _page(test: str, params: dict[str, Any], locale: str, sample: str | None, step: str | None, *,
          cards: tuple, interval: bool | str, alternative: bool, region_low: bool,
          symbols: dict[str, str], tables: tuple[_Table, ...], charts: tuple, pairs: _Comparisons | None = None,
          groups_heading: str | None = None, critical: bool | str = True) -> dict:
    """A test's report: the result (four cards, the decision), the hypotheses, how it is
    computed (the formula, its symbols, the distribution, every number), the groups, the
    comparisons of the pairs, the charts, the notes. `interval`: the estimate's card says its
    interval; `alternative`: the test has one; `region_low`: it writes a lower critical value."""
    from app.reports import formats

    t = texts(locale)
    own = t["tests"][test]
    by = subject(test, params, locale)
    header, footer = _bands(test_name(test, params, locale), " · ".join(x for x in (by, step) if x), t,
                            formats.locale(locale).texts, sample)
    p = _Page()

    # the result: four cards and a sentence
    p.heading("s_result", t["sections"]["result"])
    for (place, x, w) in zip(cards, (0, 44, 88, 132), (40, 40, 40, 54)):
        for card in ((place,) if isinstance(place, _Card) else place):
            item = {"id": card.id, "type": "kpi", "dataset": DATASET, "x": x, "y": p.y, "w": w, "h": 20, "label": card.label,
                    "value": card.value}
            if card.format:
                item["format"] = card.format
            if card.visible:
                item["visible"] = card.visible
            p.add(item)
    p.y += 14.2
    if interval:
        low, high = _value("test|||ci_low"), _value("test|||ci_high")
        said = _join(t["interval"], level=_shown(_value("test|||conf_level"), "0.##%"),
                     low=f'IF(ISNULL({low}), "−∞", {_shown(low)})', high=f'IF(ISNULL({high}), "+∞", {_shown(high)})')
        p.text("k_interval", "=" + said, h=4.5, x=134.5, w=49, size=7.5, color=MUTED,
               visible=interval if isinstance(interval, str) else None)
    else:
        p.y += 4.5
    p.y += 3.3
    p.keep()
    said = {d: _join(t["decision"][d], alpha=_shown(_value("test|||alpha"), "0.####"), p=_p_value("= "))
            for d in ("reject_h0", "fail_to_reject_h0")}
    p.text("decision", f'=IF({_text("test|||decision")} = "reject_h0", {said["reject_h0"]}, {said["fail_to_reject_h0"]})',
           h=6, size=10.5, weight="semibold")

    # the hypotheses
    p.heading("s_hypotheses", t["sections"]["hypotheses"])
    names = t["alternatives"]
    rows = [("h0", t["stats"]["test"]["h0"], "=" + _text("test|||h0")),
            ("h1", t["stats"]["test"]["h1"], "=" + _text("test|||h1"))]
    if alternative:
        chosen = _text("test|||alternative")
        rows.append(("alternative", t["stats"]["test"]["alternative"],
                     f'=IFS({chosen} = "less", {_q(names["less"])}, {chosen} = "greater", {_q(names["greater"])}, {_q(names["two_sided"])})'))
    rows.append(("alpha", t["stats"]["test"]["alpha"], "=" + _shown(_value("test|||alpha"), "0.####")))
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
    p.text("symbols", "=" + _join(own["symbols"], **symbols), h=8.5, size=8, color=MUTED, can_grow=True)
    p.y += 0.5
    p.text("distribution", "=" + _join(t["distribution"], distribution=_text("test|||distribution")))
    c = {**t["critical"], **own.get("critical", {})}  # a test whose critical values are of z says so
    c_high = _value("test|||critical_high")
    if not critical:  # an exact test: no critical value of a statistic
        pass
    elif region_low:
        c_low = _value("test|||critical_low")
        region = (f'IF(ISNULL({c_low}), "", {_join(c["low"], x=_shown(c_low))}) & '
                  f'IF(ISNULL({c_low}) OR ISNULL({c_high}), "", {_q(c["or"])}) & '
                  f'IF(ISNULL({c_high}), "", {_join(c["high"], x=_shown(c_high))})')
    else:
        region = _join(c["high"], x=_shown(c_high))
    if critical:  # (a test whose variant may be exact says when the line is there)
        p.text("critical", "=" + _join(c["region"], alpha=_shown(_value("test|||alpha"), "0.####"), region=region),
               visible=critical if isinstance(critical, str) else None)
    p.y += 1.5
    numbers = t["tables"]["numbers"]
    p.add(_table("numbers", numbers["title"], '[section] = "test" AND NOT ISNULL([value])', [
        {"header": numbers["quantity"], "value": "[label]"},
        {"header": numbers["name"], "value": "[stat]", "width": 34, "style": {"color": MUTED}},
        {"header": numbers["value"], "value": "IF(ISNULL([tiny]), [value], [tiny])", "width": 40, "align": "right", "format": EXACT},
        # R's stars beside the p-value: a column of their own, so that the value stays a number in Excel
        {"header": t["signif"]["column"], "value": f'IF([stat] = "p_value", {_text("test|||significance")}, "")', "width": 14},
    ], p.y), 10)
    p.text("signif_legend", _static(_signif_legend(t, locale)), h=4.5, size=7, color=MUTED)

    # the groups: the numbers of the formula, then what the box plot draws
    p.heading("s_groups", groups_heading or t["sections"]["groups"])
    one_row_a_group = '[section] = "group" AND [stat] = "n"'
    for table in tables:
        words = {**t["stats"].get(table.section, {}), **own.get("stats", {}).get(table.section, {})}
        labels = table.labels or ((t["tables"]["groups"]["group"], "[level]"),)
        cols = [{"header": header, "value": value} for header, value in labels]
        stats = table.stats
        if table.n_first:
            cols.append({"header": words["n"], "value": "[value]", "width": 20, "align": "right", "format": "#,##0"})
            stats = stats[1:]
        for s_ in stats:
            if s_ in table.counts:
                cols.append({"header": words[s_], "value": f"LOOKUP({_row_key(table.section, s_, table.two_levels)}, [key], [value])",
                             "width": 24, "align": "right", "format": "#,##0"})
            else:
                cols.append({"header": words[s_], "value": _group_number(s_, table.section, table.two_levels), "width": table.width,
                             "align": "right", "format": "0.########"})
        cols += list(table.extra)
        p.add(_table(table.id, table.title, table.rows or one_row_a_group, cols, p.y, visible=table.visible), 10)

    if pairs is not None:
        _comparisons(p, t, own, pairs, locale)

    # the charts, two by two (one alone across the page); a place may hold alternatives, of
    # which the rows make one visible
    p.heading("s_charts", t["sections"]["charts"])
    half, gap, tall = 91.0, 4.0, 60.0
    width = WIDTH if len(charts) == 1 else half
    caption_x = None
    for i, place in enumerate(charts):
        x = 0.0 if i % 2 == 0 else half + gap
        last_row = i >= len(charts) - (2 if len(charts) % 2 == 0 else 1)
        right = i % 2 == 1 or i == len(charts) - 1
        alternatives = (place,) if isinstance(place, _Chart) else place
        for j, chart in enumerate(alternatives):
            if chart.kind == "note":  # in place of a chart the rows are too many for: why, in a few words
                item = {"id": chart.id, "type": "text", "x": x, "y": p.y + 6, "w": width, "h": 9, "value": chart.title,
                        "style": {"size": 8, "color": MUTED}, **chart.more}
            else:
                item = {"id": chart.id, "type": "chart", "dataset": DATASET, "kind": chart.kind, "x": x, "y": p.y, "w": width, "h": tall,
                        # the title is drawn at 10 px, bold, on one line of the chart's width
                        "title": _fit(chart.title, 7.5, width - 4, 700), **chart.more}
            last = j == len(alternatives) - 1
            if chart.kind == "density":
                caption_x = x
            if right and last and caption_x is not None and not last_row:
                # the curve's caption right under its row, the next row a line lower (a curve
                # that is not on the last row: a histogram drawn after it)
                p.add(item, tall + 0.5)
                p.text("c_curve_caption", t["charts"]["density_caption"], h=4, x=caption_x, w=width, size=7, color=MUTED,
                       visible='=COUNTIF([section] = "curve") > 0')
                p.y += 2.5
                caption_x = None
                continue
            p.add(item, (tall + 0.5 if last_row else tall + 3) if right and last else 0.0)
    if caption_x is not None:
        p.text("c_curve_caption", t["charts"]["density_caption"], h=4, x=caption_x, w=width, size=7, color=MUTED,
               visible='=COUNTIF([section] = "curve") > 0')
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


def _pair_value(stat: str, column: str = "value", section: str = "pair") -> str:
    return f'LOOKUP({_row_key(section, stat, True)}, [key], [{column}])'


def _pair_number(stat: str, section: str = "pair") -> str:
    """A number of the pair (or cell) of the row at hand: eight decimals, fewer for a large
    one (as a group's), by its exponent when it is very small (a p-value)."""
    return _group_number(stat, section, two_levels=True)


def _comparisons(p: _Page, t: dict, own: dict, c: _Comparisons, locale: str) -> None:
    """The comparisons of a post-hoc: what method and correction, the formulas of a pair,
    then the pairs (or cells) in up to three tables (the differences and their statistics;
    the p-values, their significance codes and the decision; the simultaneous intervals, when
    the method has them) and a chart of the intervals, with a line at 0."""
    w = t["posthoc"]
    p.heading("s_pairs", t["sections"]["comparisons_cells" if c.section == "cell" else "comparisons"])
    adjust_words = w["adjust"] if c.intervals or c.adjust == "studentized_range" else w["adjust_only"]
    # (kept with its heading: a text that grows is the last of what is kept, or what is under it
    # would not move)
    p.text("pairs_method", _static(w["line"].replace("{method}", w["method"][c.method]).replace("{adjust}", adjust_words[c.adjust])),
           h=5, can_grow=True)
    p.keep()
    p.y += 1.5
    p.text("pairs_formula", "=" + _text("test|||posthoc_formula"), h=6.5, size=9, weight="semibold", background=PANEL, padding=1.5,
           can_grow=True)
    p.y += 1.5
    ps = {**t["stats"][c.section], **own.get("stats", {}).get(c.section, {})}
    one_row = f'[section] = "{c.section}" AND [stat] = "p_adjusted"'
    pair = {"header": w["cell"] if c.section == "cell" else w["pair"], "value": '[level] & " – " & [level_2]'}

    def numeric(stat: str) -> dict:
        return {"header": ps[stat], "value": _pair_number(stat, c.section), "width": 36, "align": "right", "format": "0.########"}

    tables = t["tables"]
    if c.numbers:
        p.add(_table("pairs", tables["pairs"]["title"], one_row, [pair, *(numeric(s) for s in c.numbers)], p.y), 10)
    decision = _pair_value("decision", "text", c.section)
    p.add(_table("pairs_p", tables["pairs_p"]["title"], one_row, [
        pair, *([numeric("p_value")] if c.own_p else []), numeric("p_adjusted"),
        {"header": t["signif"]["column"], "value": _pair_value("significance", "text", c.section), "width": 14},
        {"header": ps["decision"], "value": f'IF({decision} = "reject_h0", {_q(w["reject"])}, {_q(w["keep"])})', "width": 50},
    ], p.y), 10)
    p.text("pairs_signif_legend", _static(_signif_legend(t, locale)), h=4.5, size=7, color=MUTED)
    if c.intervals:
        p.add(_table("pairs_ci", tables["pairs_ci"]["title"], one_row,
                     [pair, *(numeric(s) for s in ("critical", "ci_low", "ci_high"))], p.y), 10)
        # the differences with their intervals, while they are few enough to be read on one chart
        p.y += 2.0
        p.add({"id": "c_pairs", "type": "chart", "dataset": DATASET, "kind": "interval", "x": 0, "y": p.y, "w": WIDTH, "h": 62,
               "title": _fit(t["charts"]["pairs"], 7.5, WIDTH - 4, 700), "filter": '[section] = "pair"',
               "category": '[level] & " – " & [level_2]',
               "series": [{"value": f'SUMIF([stat] = "{s}", [value])', "name": ps[s]} for s in ("estimate", "ci_low", "ci_high")],
               "options": {"labels": True, "format": "0.###", "target": 0},
               "visible": f'=COUNTIF({one_row}) <= {MAX_PAIRS_CHARTED}'}, 62.5)


# past this many pairs the chart of the intervals is not drawn: the tables say them
MAX_PAIRS_CHARTED = 15


def _series(t: dict, *stats: str, own: dict | None = None) -> list[dict]:
    names = {**t["stats"]["group"], **((own or {}).get("stats", {}).get("group", {}))}
    return [{"value": f'SUMIF([stat] = "{s}", [value])', "name": names[s]} for s in stats]


def _box(t: dict, by: str, filter_: str = '[section] = "group"') -> _Chart:
    return _Chart("c_box", "boxplot", t["charts"]["boxplot"].replace("{by}", by),
                  {"filter": filter_, "category": "[level]", "series": _series(t, "min", "q1", "median", "q3", "max"),
                   "options": {"format": "0.###"}})


def _means(t: dict, own: dict, stats: tuple[str, str, str] = ("mean", "ci_low", "ci_high"), filter_: str = '[section] = "group"',
           target: str | None = None, labels: bool = True, id_: str = "c_means", visible: str | None = None) -> _Chart:
    options: dict[str, Any] = {"labels": labels, "format": "0.###"}
    if target is not None:
        options["target"] = target
    more = {"filter": filter_, "category": "[level]", "series": _series(t, *stats, own=own), "options": options}
    if visible:
        more["visible"] = visible
    return _Chart(id_, "interval", own.get("charts", {}).get("interval", t["charts"]["interval"]), more)


# past this many groups the values are not written beside the means: they would run into each other
MAX_GROUPS_LABELLED = 8
_GROUPS = 'COUNTIF([section] = "group" AND [stat] = "n")'


def _means_of_groups(t: dict, own: dict, stats: tuple[str, str, str] = ("mean", "ci_low", "ci_high")) -> tuple[_Chart, _Chart]:
    """The interval chart of several groups: its values written while the groups are few."""
    return (_means(t, own, stats, visible=f"={_GROUPS} <= {MAX_GROUPS_LABELLED}"),
            _means(t, own, stats, labels=False, id_="c_means_many", visible=f"={_GROUPS} > {MAX_GROUPS_LABELLED}"))


def _histogram(t: dict, by: str, legend: bool = True, at_most: int | None = None) -> _Chart:
    visible = '=COUNTIF([section] = "hist") > 0'
    if at_most is not None:
        visible += f' AND COUNTIF([section] = "group" AND [stat] = "n") <= {at_most}'
    return _Chart("c_hist", "column", t["charts"]["histogram"].replace("{by}", by),
                  {"filter": '[section] = "hist"', "category": "[text]", "split_by": "[level]",
                   "series": [{"value": "SUM([value])", "name": t["stats"]["hist"]["count"]}],
                   "options": {"legend": legend, "format": "#,##0"}, "visible": visible})


def _density(t: dict, target: str = "statistic") -> _Chart:
    # the first value of `split_by` in the rows' order is the line, the others the regions under it
    return _Chart("c_curve", "density", t["charts"]["density"],
                  {"filter": '[section] = "curve"', "category": "[x0]", "split_by": "[stat]",
                   "series": [{"value": "[value]", "name": t["stats"]["curve"]["density"]}],
                   "options": {"legend": False, "target": _value(f"test|||{target}"), "format": "0.000"},
                   "visible": '=COUNTIF([section] = "curve") > 0'})


def _density_or_exact(t: dict, target: str = "statistic") -> tuple[_Chart, _Chart]:
    """The curve of an approximation, or, in its place, why an exact test has none."""
    return (_density(t, target), _Chart("c_curve_exact", "note", t["charts"]["exact_note"],
                                        {"visible": '=COUNTIF([section] = "curve") = 0'}))


def _cards(t: dict, own: dict, fourth: _Card | None = None, two_df: bool = False) -> tuple[_Card, _Card, _Card, _Card]:
    return (_Card("k_statistic", own["kpi"]["statistic"], _shown(_value("test|||statistic"))),
            _df_card(t, two_df),
            _Card("k_p", t["kpi"]["p_value"], _with_stars(_p_value(), _text("test|||significance"))),
            fourth or _Card("k_estimate", own["kpi"]["estimate"], _shown(_value("test|||estimate"))))


_FIRST, _LAST = 'LOOKUP("group", [section], [level])', 'LOOKUP("group", [section], LAST([level]))'


def _tables_of_means(t: dict, own: dict, welch_weight: bool = False) -> tuple[_Table, ...]:
    # three tables, not one of eight columns: a variance of eleven digits ran into the mean
    # beside it, and a header of two words («Mean, CI low») broke on two lines
    tables = t["tables"]
    return (_Table("groups", tables["groups"]["title"], ("n", "mean", "var", "sd"), n_first=True),
            _Table("intervals", tables["intervals"]["title"], ("se", "ci_low", "ci_high") + (("weight",) if welch_weight else ())),
            _Table("quartiles", tables["quartiles"]["title"], ("min", "q1", "median", "q3", "max"), width=30))


# ── the layouts ─────────────────────────────────────────────────────────────
def _two_groups(test: str, params: dict[str, Any], locale: str, sample: str | None, step: str | None) -> dict:
    """Two groups of a column: t_two_sample, var_f (and, with their lots, mann_whitney,
    ks_two_sample)."""
    t = texts(locale)
    own = t["tests"][test]
    by = subject(test, params, locale)
    return _page(test, params, locale, sample, step, cards=_cards(t, own, two_df=test == "var_f"), interval=True,
                 alternative=True, region_low=True, symbols={"g1": _FIRST, "g2": _LAST}, tables=_tables_of_means(t, own),
                 charts=(_box(t, by), _means(t, own), _histogram(t, by), _density(t)))


def _one_sample(test: str, params: dict[str, Any], locale: str, sample: str | None, step: str | None) -> dict:
    """One column against a value: t_one_sample (and, with its lot, normality)."""
    t = texts(locale)
    own = t["tests"][test]
    by = subject(test, params, locale)
    return _page(test, params, locale, sample, step, cards=_cards(t, own), interval=True, alternative=True, region_low=True,
                 symbols={"column": _FIRST}, tables=_tables_of_means(t, own),
                 charts=(_box(t, by), _means(t, own, target=_value("test|||null_value")), _histogram(t, by, legend=False),
                         _density(t)))


def _paired(test: str, params: dict[str, Any], locale: str, sample: str | None, step: str | None) -> dict:
    """Two columns of the same rows, their differences tested: t_paired (and, with its lot,
    wilcoxon_signed_rank). The tables have the two columns and the differences; the charts
    the differences."""
    t = texts(locale)
    own = t["tests"][test]
    x, y = str(params.get("x") or "x"), str(params.get("y") or "y")
    difference = paired_difference_label(x, y)
    only = f'[section] = "group" AND [level] = {_q(difference)}'
    return _page(test, params, locale, sample, step, cards=_cards(t, own), interval=True, alternative=True, region_low=True,
                 symbols={"x": _q(x), "y": _q(y)}, tables=_tables_of_means(t, own),
                 charts=(_box(t, difference, only), _means(t, own, filter_=only, target=_value("test|||null_value")),
                         _histogram(t, difference, legend=False), _density(t)))


def _several_groups(test: str, params: dict[str, Any], locale: str, sample: str | None, step: str | None) -> dict:
    """Several groups of a column: anova (with its pairwise comparisons), levene (and, with
    its lot, kruskal_wallis)."""
    t = texts(locale)
    own = t["tests"][test]
    by = subject(test, params, locale)
    tables = t["tables"]
    if test == "levene":
        fourth = _Card("k_groups", own["kpi"]["groups"], _value("test|||k"), "#,##0")
        table_set = (_Table("groups", tables["groups"]["title"], ("n", "center", "dev_mean", "dev_var"), n_first=True),
                     _Table("intervals", own["tables"]["deviations"], ("dev_sd", "dev_se", "dev_ci_low", "dev_ci_high")),
                     _Table("quartiles", tables["quartiles"]["title"], ("min", "q1", "median", "q3", "max"), width=30))
        means = _means_of_groups(t, own, ("dev_mean", "dev_ci_low", "dev_ci_high"))
    else:
        fourth = _Card("k_effect", own["kpi"]["effect"], _shown(_value("test|||eta_squared"), P4))
        table_set = _tables_of_means(t, own, welch_weight=_variant(test, params) == "welch")
        means = _means_of_groups(t, own)
    pairs = None
    posthoc = _param(test, params, "posthoc") if test == "anova" else "none"
    if posthoc != "none":
        adjust = "studentized_range" if posthoc in ("tukey", "games_howell") else _param(test, params, "p_adjust")
        pairs = _Comparisons(posthoc, adjust, ("estimate", "std_error", "statistic", "df"), own_p=posthoc == "pairwise_t",
                             intervals=not (posthoc == "pairwise_t" and adjust == "holm"))
    # the histogram last: past six groups it is not drawn, and leaves no hole between the others
    return _page(test, params, locale, sample, step, cards=_cards(t, own, fourth, two_df=True), interval=False,
                 alternative=False, region_low=False, symbols={}, tables=table_set, pairs=pairs,
                 charts=(_box(t, by), means, _density(t), _histogram(t, by, at_most=6)))


def _correlation(test: str, params: dict[str, Any], locale: str, sample: str | None, step: str | None) -> dict:
    """Two columns of the same rows, how they move together: pearson (and, with its lot,
    spearman). No box plot nor histogram: a scatter plot is for a later release."""
    t = texts(locale)
    own = t["tests"][test]
    tables = t["tables"]
    return _page(test, params, locale, sample, step, cards=_cards(t, own), interval=True, alternative=True, region_low=True,
                 symbols={"x": _q(str(params.get("x") or "x")), "y": _q(str(params.get("y") or "y"))},
                 tables=(_Table("groups", tables["groups"]["title"], ("n", "mean", "var", "sd"), n_first=True),
                         _Table("extremes", own["tables"]["extremes"], ("min", "max"))),
                 charts=(_density(t),))


def _proportions(test: str, params: dict[str, Any], locale: str, sample: str | None, step: str | None) -> dict:
    """One or two proportions: prop_one_sample (score or exact), prop_two_sample. The groups'
    successes and proportions with their intervals; the chart of the proportions (against p0
    for one sample) and, for a score test, the normal curve."""
    t = texts(locale)
    own = t["tests"][test]
    exact_ = test == "prop_one_sample" and _param(test, params, "method") == "exact"
    if exact_:
        cards = (_Card("k_statistic", own["kpi"]["successes"], _value("test|||successes"), "#,##0"),
                 _Card("k_df", own["kpi"]["n"], _value("test|||n"), "#,##0"),
                 _Card("k_p", t["kpi"]["p_value"], _with_stars(_p_value(), _text("test|||significance"))),
                 _Card("k_estimate", own["kpi"]["estimate"], _shown(_value("test|||estimate"))))
    else:
        cards = (_Card("k_statistic", own["kpi"]["statistic"], _shown(_value("test|||statistic"))),
                 _Card("k_df", own["kpi"]["chi_square"], _shown(_value("test|||chi_square"))),
                 _Card("k_p", t["kpi"]["p_value"], _with_stars(_p_value(), _text("test|||significance"))),
                 _Card("k_estimate", own["kpi"]["estimate"], _shown(_value("test|||estimate"))))
    two = test == "prop_two_sample"
    tables = t["tables"]
    table_set = (_Table("groups", tables["groups"]["title"], ("n", "successes", "proportion"), counts=("successes",), n_first=True),
                 _Table("intervals", own["tables"]["intervals"], ("se", "ci_low", "ci_high")))
    target = None if two else _value("test|||null_value")
    charts: tuple = (_means(t, own, ("proportion", "ci_low", "ci_high"), target=target),)
    if not exact_:
        charts += (_density(t),)
    symbols = {"g1": _FIRST, "g2": _LAST} if two else {"column": _FIRST}
    return _page(test, params, locale, sample, step, cards=cards, interval=True, alternative=True, region_low=True,
                 symbols=symbols, tables=table_set, charts=charts, critical=not exact_)


# past this many cells the chart of the observed and expected counts is not drawn: the tables say them
MAX_CELLS_CHARTED = 40
# past this many categories of x or of y the heatmap writes no number in its cells
MAX_HEATMAP_LABELLED = 10
# a heatmap's rows are the categories of x, each a series of the chart: the report engine draws
# 24 at most (`charts.MAX_SERIES`); past them the tables say the residuals
MAX_HEATMAP_ROWS = 24


def _contingency(test: str, params: dict[str, Any], locale: str, sample: str | None, step: str | None) -> dict:
    """Counts of categories: chi2_independence and g_test (with the post-hoc), chi2_gof,
    fisher_exact (2 × 2 or larger), mcnemar (two columns of the same rows), cmh (within
    strata). The margins and the cells (observed, expected, residuals: every term of the
    statistic), the strata, the comparisons of the cells or of the pairs; the observed against
    the expected counts, the heatmap of the adjusted residuals, the chi-square curve."""
    t = texts(locale)
    own = t["tests"][test]
    words = t["tables"]
    gof = test == "chi2_gof"
    x, y = str(params.get("x") or "x"), str(params.get("y") or "y")
    value = str(params.get("value") or "value")
    stars = _Card("k_p", t["kpi"]["p_value"], _with_stars(_p_value(), _text("test|||significance")))
    shape = f'{_shown(_value("test|||rows"), "#,##0")} & " × " & {_shown(_value("test|||cols"), "#,##0")}'
    two_by_two = f'={_value("test|||rows")} = 2 AND {_value("test|||cols")} = 2'
    larger = f'=NOT ({two_by_two[1:]})'
    interval: bool | str = False
    if test == "fisher_exact":
        cards = (_Card("k_statistic", own["kpi"]["table"], "=" + shape), _Card("k_df", own["kpi"]["n"], _value("test|||n"), "#,##0"), stars,
                 (_Card("k_estimate", own["kpi"]["estimate"], _shown(_value("test|||estimate")), visible=two_by_two),
                  _Card("k_nodes", own["kpi"]["nodes"], _value("test|||network_nodes"), "#,##0", visible=larger)))
        interval = two_by_two
    elif test == "mcnemar":
        two = f'={_value("test|||categories")} = 2'
        stat = _value("test|||statistic")
        cards = (_Card("k_statistic", own["kpi"]["discordant"], _value("test|||discordant"), "#,##0"),
                 _Card("k_df", own["kpi"]["statistic"], f'=IF(ISNULL({stat}), "—", {_shown(stat)})'), stars,
                 (_Card("k_estimate", own["kpi"]["estimate"], _shown(_value("test|||estimate")), visible=two),
                  _Card("k_categories", own["kpi"]["categories"], _value("test|||categories"), "#,##0", visible=f"=NOT ({two[1:]})")))
        interval = two
    elif test == "cmh":
        cards = (_Card("k_statistic", own["kpi"]["statistic"], _shown(_value("test|||statistic"))), _df_card(t, False), stars,
                 (_Card("k_estimate", own["kpi"]["estimate"], _shown(_value("test|||estimate")), visible=two_by_two),
                  _Card("k_strata", own["kpi"]["strata"], _value("test|||strata"), "#,##0", visible=larger)))
        interval = two_by_two
    else:
        effect = "cohen_w" if gof else "cramers_v"
        cards = (_Card("k_statistic", own["kpi"]["statistic"], _shown(_value("test|||statistic"))), _df_card(t, False), stars, _Card("k_effect", own["kpi"]["effect"], _shown(_value(f"test|||{effect}"), P4)))
    cell_rows = '[section] = "cell" AND [stat] = "observed"'
    if gof:
        labels = ((value, "[level]"),)
        table_set = (_Table("cells", words["cells"]["title"], ("observed", "proportion", "expected_share"), section="cell",
                            rows=cell_rows, labels=labels, counts=("observed",)),
                     _Table("terms", words["terms"]["title"], ("expected", "residual", "contribution"), section="cell",
                            rows=cell_rows, labels=labels))
        category, symbols = "[level]", {"column": _q(value)}
    else:
        labels = ((x, "[level]"), (y, "[level_2]"))
        table_set = ()
        if test == "cmh":
            strata = str(params.get("strata") or "strata")
            two_rows = '[section] = "group" AND [stat] = "a"'
            table_set += (_Table("strata", own["tables"]["strata"], ("n", "a", "b", "c", "d"), rows=two_rows, labels=((strata, "[level]"),),
                                 counts=("n", "a", "b", "c", "d"), visible=two_by_two),
                          _Table("strata_terms", own["tables"]["terms"], ("expected_a", "variance_a", "odds_ratio"), rows=two_rows,
                                 labels=((strata, "[level]"),), visible=two_by_two),
                          _Table("strata_n", own["tables"]["strata"], ("n",), rows='[section] = "group" AND [stat] = "n"',
                                 labels=((strata, "[level]"),), counts=("n",), visible=larger))
        else:
            table_set += (_Table("rows", words["rows"]["title"], ("row_total", "row_share"), rows='[section] = "group" AND [stat] = "row_total"',
                                 labels=((x, "[level]"),), counts=("row_total",)),
                          _Table("cols", words["cols"]["title"], ("col_total", "col_share"), rows='[section] = "group" AND [stat] = "col_total"',
                                 labels=((y, "[level]"),), counts=("col_total",)))
        if test == "fisher_exact":
            table_set += (_Table("cells", words["cells"]["title"], ("observed", "expected"), section="cell", rows=cell_rows,
                                 labels=labels, two_levels=True, counts=("observed",)),)
        elif test == "mcnemar":
            agreement = _pair_value("agreement", "text", "cell")
            pair = own["tables"]["pair"]
            table_set += (_Table("cells", own["tables"]["cells"], ("observed", "expected"), section="cell", rows=cell_rows,
                                 labels=labels, two_levels=True, counts=("observed",),
                                 extra=({"header": pair["header"], "width": 34,
                                         "value": f'IF({agreement} = "discordant", {_q(pair["discordant"])}, {_q(pair["concordant"])})'},)),)
        elif test == "cmh":
            table_set += (_Table("cells", own["tables"]["pooled"], ("observed",), section="cell", rows=cell_rows, labels=labels,
                                 two_levels=True, counts=("observed",)),)
        else:
            table_set += (_Table("cells", words["cells"]["title"], ("observed", "expected", "residual"), section="cell", rows=cell_rows,
                                 labels=labels, two_levels=True, counts=("observed",)),
                          _Table("terms", words["terms"]["title"], ("adj_residual", "contribution"), section="cell", rows=cell_rows,
                                 labels=labels, two_levels=True))
        category, symbols = '[level] & " – " & [level_2]', {"x": _q(x), "y": _q(y)}
        if test == "cmh":
            symbols["strata"] = _q(str(params.get("strata") or "strata"))
    cells = 'COUNTIF([section] = "cell" AND [stat] = "observed")'
    rows_x, cols_y = 'COUNTIF([section] = "group" AND [stat] = "row_total")', 'COUNTIF([section] = "group" AND [stat] = "col_total")'
    cell = {**t["stats"]["cell"], **own.get("stats", {}).get("cell", {})}
    title = t["charts"]["observed"]

    def note(id_: str, words_: str, chart_title: str, count: str, most: int) -> _Chart:
        said = _join(t["charts"][words_], chart=_q(chart_title), n=_shown(count, "#,##0"), most=_q(str(most)))
        return _Chart(id_, "note", "=" + said, {"visible": f"={count} > {most}"})

    shown = ("observed",) if test == "cmh" else ("observed", "expected")
    observed = (_Chart("c_observed", "column", own.get("charts", {}).get("observed", title),
                       {"filter": '[section] = "cell"', "category": category,
                        "series": [{"value": f'SUMIF([stat] = "{s_}", [value])', "name": cell[s_]} for s_ in shown],
                        "options": {"legend": True, "format": "#,##0.#"}, "visible": f"={cells} <= {MAX_CELLS_CHARTED}"}),
                note("c_observed_note", "too_many_cells", title, cells, MAX_CELLS_CHARTED))
    charts: tuple = (observed,)
    if test in ("chi2_independence", "g_test"):
        heat = t["charts"]["residuals"]
        labelled = f"{rows_x} <= {MAX_HEATMAP_LABELLED} AND {cols_y} <= {MAX_HEATMAP_LABELLED}"
        plain = f"NOT ({labelled}) AND {rows_x} <= {MAX_HEATMAP_ROWS}"

        def heatmap(id_: str, labels_: bool, visible: str) -> _Chart:
            return _Chart(id_, "heatmap", heat, {
                "filter": '[section] = "cell" AND [stat] = "adj_residual"', "category": "[level_2]", "split_by": "[level]",
                "series": [{"value": "SUM([value])", "name": cell["adj_residual"]}],
                "options": {"labels": labels_, "format": "0.00"}, "visible": "=" + visible})

        charts += ((heatmap("c_residuals", True, labelled), heatmap("c_residuals_many", False, plain),
                    note("c_residuals_note", "too_many_rows", heat, rows_x, MAX_HEATMAP_ROWS)),)
    if test != "fisher_exact":
        charts += (_density(t),)
    pairs = None
    posthoc = _param(test, params, "posthoc") if test in ("chi2_independence", "g_test") else "none"
    if posthoc == "cells":
        pairs = _Comparisons("cells", _param(test, params, "p_adjust"), (), own_p=True, intervals=False, section="cell")
    elif posthoc == "pairs":
        pairs = _Comparisons("pairs", _param(test, params, "p_adjust"), ("statistic", "df"), own_p=True, intervals=False)
    exact_test = test == "fisher_exact"
    critical: bool | str = not exact_test
    if test == "mcnemar":  # the exact binomial has no critical value of a statistic
        critical = f'=NOT ISNULL({_value("test|||critical_high")})'
    return _page(test, params, locale, sample, step, cards=cards, interval=interval, alternative=test == "fisher_exact",
                 region_low=False, symbols=symbols, tables=table_set, charts=charts, pairs=pairs,
                 groups_heading=t["sections"]["strata" if test == "cmh" else "table"], critical=critical)

# ── ranks and distributions (lot 4) ─────────────────────────────────────────
def _or_dash(key: str, pattern: str | None = None) -> str:
    """A number of the test, or «—» where the variant has none (the z of an exact test)."""
    v = _value(key)
    return f'=IF(ISNULL({v}), "—", {_shown(v, pattern)})'


_NO_Z = f'=NOT ISNULL({_value("test|||z")})'


def _ranks_two(test: str, params: dict[str, Any], locale: str, sample: str | None, step: str | None) -> dict:
    """Two groups compared by their ranks or their distributions: mann_whitney, ks_two_sample.
    The rank sums (Mann–Whitney) or the extremes (Kolmogorov–Smirnov) and the five numbers of
    each group; the box plot, the histogram, the curve of the approximation (none for an exact
    test: a line says why)."""
    t = texts(locale)
    own = t["tests"][test]
    by = subject(test, params, locale)
    tables = t["tables"]
    stars = _Card("k_p", t["kpi"]["p_value"], _with_stars(_p_value(), _text("test|||significance")))
    if test == "mann_whitney":
        cards = (_Card("k_statistic", own["kpi"]["statistic"], _shown(_value("test|||statistic"))),
                 _Card("k_df", own["kpi"]["z"], _or_dash("test|||z")), stars,
                 _Card("k_estimate", own["kpi"]["effect"], _shown(_value("test|||rank_biserial"), P4)))
        table_set = (_Table("groups", tables["groups"]["title"], ("n", "rank_sum", "mean_rank"), n_first=True),
                     _Table("quartiles", tables["quartiles"]["title"], ("min", "q1", "median", "q3", "max"), width=30))
        critical: bool | str = _NO_Z
        target = "z"
    else:
        cards = (_Card("k_statistic", own["kpi"]["statistic"], _shown(_value("test|||statistic"))),
                 _Card("k_df", own["kpi"]["lambda"], _or_dash("test|||lambda")), stars,
                 _Card("k_estimate", own["kpi"]["n"], _value("test|||n"), "#,##0"))
        table_set = (_Table("groups", tables["groups"]["title"], ("n", "min", "q1", "median", "q3", "max"), n_first=True, width=26),)
        critical = f'=NOT ISNULL({_value("test|||critical_high")})'
        target = "statistic"
    return _page(test, params, locale, sample, step, cards=cards, interval=False, alternative=True, region_low=test == "mann_whitney",
                 symbols={"g1": _FIRST, "g2": _LAST}, tables=table_set, critical=critical,
                 charts=(_box(t, by), _histogram(t, by), _density_or_exact(t, target)))


def _signed_rank(test: str, params: dict[str, Any], locale: str, sample: str | None, step: str | None) -> dict:
    """Wilcoxon's signed-rank test: two columns of the same rows, their differences ranked (or
    one column against mu). The columns and the differences (their five numbers); the box plot
    and the histogram of the differences, the curve of the approximation."""
    t = texts(locale)
    own = t["tests"][test]
    x, y = str(params.get("x") or "x"), params.get("y")
    measured = paired_difference_label(x, str(y)) if y else x
    only = f'[section] = "group" AND [level] = {_q(measured)}'
    cards = (_Card("k_statistic", own["kpi"]["statistic"], _shown(_value("test|||statistic"))),
             _Card("k_df", own["kpi"]["z"], _or_dash("test|||z")),
             _Card("k_p", t["kpi"]["p_value"], _with_stars(_p_value(), _text("test|||significance"))),
             _Card("k_estimate", own["kpi"]["effect"], _shown(_value("test|||rank_biserial"), P4)))
    tables = t["tables"]
    table_set = (_Table("groups", tables["groups"]["title"], ("n", "min", "q1", "median", "q3", "max"), n_first=True, width=26),)
    return _page(test, params, locale, sample, step, cards=cards, interval=False, alternative=True, region_low=True,
                 symbols={"x": _q(x), "y": _q(str(y) if y else "—")}, tables=table_set, critical=_NO_Z,
                 charts=(_box(t, measured, only), _histogram(t, measured, legend=False), _density_or_exact(t, "z")))


def _kruskal(test: str, params: dict[str, Any], locale: str, sample: str | None, step: str | None) -> dict:
    """Kruskal–Wallis: several groups by their ranks, with Dunn's comparisons of the pairs."""
    t = texts(locale)
    own = t["tests"][test]
    by = subject(test, params, locale)
    tables = t["tables"]
    cards = (_Card("k_statistic", own["kpi"]["statistic"], _shown(_value("test|||statistic"))), _df_card(t, False),
             _Card("k_p", t["kpi"]["p_value"], _with_stars(_p_value(), _text("test|||significance"))),
             _Card("k_effect", own["kpi"]["effect"], _shown(_value("test|||epsilon_squared"), P4)))
    table_set = (_Table("groups", tables["groups"]["title"], ("n", "rank_sum", "mean_rank"), n_first=True),
                 _Table("quartiles", tables["quartiles"]["title"], ("min", "q1", "median", "q3", "max"), width=30))
    pairs = None
    if _param(test, params, "posthoc") == "dunn":
        pairs = _Comparisons("dunn", _param(test, params, "p_adjust"), ("estimate", "std_error", "statistic"), own_p=True,
                             intervals=False)
    return _page(test, params, locale, sample, step, cards=cards, interval=False, alternative=False, region_low=False, symbols={},
                 tables=table_set, pairs=pairs, charts=(_box(t, by), _density(t), _histogram(t, by, at_most=6)))


def _rank_correlation(test: str, params: dict[str, Any], locale: str, sample: str | None, step: str | None) -> dict:
    """Spearman's ρ: the two columns' extremes, the t curve. No interval (none in the first
    release), no scatter plot (a later one)."""
    t = texts(locale)
    own = t["tests"][test]
    tables = t["tables"]
    return _page(test, params, locale, sample, step, cards=_cards(t, own), interval=False, alternative=True, region_low=True,
                 symbols={"x": _q(str(params.get("x") or "x")), "y": _q(str(params.get("y") or "y"))},
                 tables=(_Table("groups", tables["groups"]["title"], ("n", "min", "max"), n_first=True),), charts=(_density(t),))


def _normality(test: str, params: dict[str, Any], locale: str, sample: str | None, step: str | None) -> dict:
    """A test of normality: the column's mean, variance and five numbers; its box plot and
    histogram; the chi-square curve with two degrees of freedom."""
    t = texts(locale)
    own = t["tests"][test]
    by = subject(test, params, locale)
    tables = t["tables"]
    cards = (_Card("k_statistic", own["kpi"]["statistic"], _shown(_value("test|||statistic"))),
             _Card("k_df", own["kpi"]["skewness"], _shown(_value("test|||skewness"), P4)),
             _Card("k_p", t["kpi"]["p_value"], _with_stars(_p_value(), _text("test|||significance"))),
             _Card("k_estimate", own["kpi"]["kurtosis"], _shown(_value("test|||kurtosis"), P4)))
    table_set = (_Table("groups", tables["groups"]["title"], ("n", "mean", "var", "sd"), n_first=True),
                 _Table("quartiles", tables["quartiles"]["title"], ("min", "q1", "median", "q3", "max"), width=30))
    return _page(test, params, locale, sample, step, cards=cards, interval=False, alternative=False, region_low=False,
                 symbols={"column": _FIRST}, tables=table_set,
                 charts=(_box(t, by), _histogram(t, by, legend=False), _density(t)))


LAYOUTS: dict[str, Callable[[str, dict[str, Any], str, str | None, str | None], dict]] = {
    "t_one_sample": _one_sample,
    "t_two_sample": _two_groups,
    "t_paired": _paired,
    "anova": _several_groups,
    "var_f": _two_groups,
    "levene": _several_groups,
    "pearson": _correlation,
    "prop_one_sample": _proportions,
    "prop_two_sample": _proportions,
    "chi2_independence": _contingency,
    "chi2_gof": _contingency,
    "fisher_exact": _contingency,
    "mcnemar": _contingency,
    "cmh": _contingency,
    "g_test": _contingency,
    "spearman": _rank_correlation,
    "mann_whitney": _ranks_two,
    "wilcoxon_signed_rank": _signed_rank,
    "kruskal_wallis": _kruskal,
    "ks_two_sample": _ranks_two,
    "normality": _normality,
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
