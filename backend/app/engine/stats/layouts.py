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

from app.engine.stats.catalog import CURVE_POINTS, MAX_BINS, MAX_STAT_GROUPS, TESTS
from app.engine.stats.compute import paired_difference_label
from app.engine.stats.output import OUTPUT_COLUMNS

LOCALES = ("en", "it", "de", "es", "fr")
TEXTS = Path(__file__).with_name("report_texts.json")
DATASET = "result"
# the mark the editor puts on the operation that is a source's development sample
DEV_SAMPLE_MARK = "_dev_sample"

# The most rows a test writes for its report, which the route reads whole: the test's own
# (some 45), then for each of at most MAX_STAT_GROUPS groups its 13 numbers and up to
# MAX_BINS intervals, the three series of the curve, and the 10 rows of each of the 190 pairs
# of a post-hoc.
MAX_REPORT_ROWS = 64 + MAX_STAT_GROUPS * (13 + MAX_BINS) + 3 * CURVE_POINTS + 190 * 10

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
    if p.get("value") and p.get("group"):
        return texts(locale)["by"].replace("{value}", str(p["value"])).replace("{group}", str(p["group"]))
    if test == "t_paired" and p.get("x") and p.get("y"):
        return paired_difference_label(str(p["x"]), str(p["y"]))
    return ", ".join(str(p[k]) for k in ("value", "x", "y") if p.get(k))


def title(test: str, params: dict[str, Any] | None, locale: str | None) -> str:
    """The report's name when the request gives none: «Welch's t test — amount by channel»."""
    about = subject(test, params, locale)
    return test_name(test, params, locale) + (" — " + about if about else "")


# ── one page, made of the blocks a test has ─────────────────────────────────
class _Card(NamedTuple):
    id: str
    label: str
    value: str
    format: str | None = None


class _Table(NamedTuple):
    """A table of the groups: one row a group, a column for each stat (its header the stat's
    name on the page), `width` mm each."""

    id: str
    title: str
    stats: tuple[str, ...]
    width: float = 40.0
    n_first: bool = False  # the rows' count first, narrower


class _Chart(NamedTuple):
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
          cards: tuple[_Card, _Card, _Card, _Card], interval: bool, alternative: bool, region_low: bool,
          symbols: dict[str, str], tables: tuple[_Table, ...], charts: tuple, pairs: bool = False) -> dict:
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
    for (card, x, w) in zip(cards, (0, 44, 88, 132), (40, 40, 40, 54)):
        item = {"id": card.id, "type": "kpi", "dataset": DATASET, "x": x, "y": p.y, "w": w, "h": 20, "label": card.label,
                "value": card.value}
        if card.format:
            item["format"] = card.format
        p.add(item)
    p.y += 14.2
    if interval:
        low, high = _value("test|||ci_low"), _value("test|||ci_high")
        said = _join(t["interval"], level=_shown(_value("test|||conf_level"), "0.##%"),
                     low=f'IF(ISNULL({low}), "−∞", {_shown(low)})', high=f'IF(ISNULL({high}), "+∞", {_shown(high)})')
        p.text("k_interval", "=" + said, h=4.5, x=134.5, w=49, size=7.5, color=MUTED)
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
    c = t["critical"]
    c_high = _value("test|||critical_high")
    if region_low:
        c_low = _value("test|||critical_low")
        region = (f'IF(ISNULL({c_low}), "", {_join(c["low"], x=_shown(c_low))}) & '
                  f'IF(ISNULL({c_low}) OR ISNULL({c_high}), "", {_q(c["or"])}) & '
                  f'IF(ISNULL({c_high}), "", {_join(c["high"], x=_shown(c_high))})')
    else:
        region = _join(c["high"], x=_shown(c_high))
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
    g = {**t["stats"]["group"], **own.get("stats", {}).get("group", {})}
    one_row_a_group = '[section] = "group" AND [stat] = "n"'
    for table in tables:
        cols = [{"header": t["tables"]["groups"]["group"], "value": "[level]"}]
        stats = table.stats
        if table.n_first:
            cols.append({"header": g["n"], "value": "[value]", "width": 20, "align": "right", "format": "#,##0"})
            stats = stats[1:]
        cols += [{"header": g[s], "value": _group_number(s), "width": table.width, "align": "right", "format": "0.########"}
                 for s in stats]
        p.add(_table(table.id, table.title, one_row_a_group, cols, p.y), 10)

    if pairs:
        _comparisons(p, t, params)

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
            item = {"id": chart.id, "type": "chart", "dataset": DATASET, "kind": chart.kind, "x": x, "y": p.y, "w": width, "h": tall,
                    # the title is drawn at 10 px, bold, on one line of the chart's width
                    "title": _fit(chart.title, 7.5, width - 4, 700), **chart.more}
            last = j == len(alternatives) - 1
            p.add(item, (tall + 0.5 if last_row else tall + 3) if right and last else 0.0)
            if chart.kind == "density":
                caption_x = x
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


def _pair_value(stat: str, column: str = "value") -> str:
    return f'LOOKUP("pair|" & [level] & "|" & [level_2] & "|{stat}", [key], [{column}])'


def _pair_number(stat: str) -> str:
    """A number of the pair of the row at hand: eight decimals, fewer for a large one (as a
    group's), by its exponent when it is very small (a p-value)."""
    x = _pair_value(stat)
    rounded = (f"IFS(ABS({x}) >= 1000000000, ROUND({x}, 1), ABS({x}) >= 1000000, ROUND({x}, 2), ABS({x}) >= 1000, ROUND({x}, 4), "
               f"ABS({x}) >= 10, ROUND({x}, 6), {x})")
    return f"IF(ISNULL({_pair_value(stat, 'tiny')}), {rounded}, {_pair_value(stat, 'tiny')})"


def _comparisons(p: _Page, t: dict, params: dict[str, Any]) -> None:
    """The pairwise comparisons: what method and correction, then the pairs in three tables
    (the difference and its statistic; the p-values and the decision; the simultaneous
    intervals, when the method has them) and a chart of the intervals, with a line at 0."""
    w = t["posthoc"]
    posthoc, adjust = _param("anova", params, "posthoc"), _param("anova", params, "p_adjust")
    intervals = not (posthoc == "pairwise_t" and adjust == "holm")
    p.heading("s_pairs", t["sections"]["comparisons"])
    method, how = _text("test|||posthoc"), _text("test|||p_adjust")
    said_method = "IFS(" + ", ".join(f'{method} = {_q(k)}, {_q(v)}' for k, v in w["method"].items()) + ', TRUE, "")'
    said_adjust = "IFS(" + ", ".join(f'{how} = {_q(k)}, {_q(v)}' for k, v in w["adjust"].items()) + ', TRUE, "")'
    # (kept with its heading: a text that grows is the last of what is kept, or what is under it
    # would not move)
    p.text("pairs_method", "=" + _join(w["line"], method=said_method, adjust=said_adjust), h=5, can_grow=True)
    p.keep()
    p.y += 1.5
    p.text("pairs_formula", "=" + _text("test|||posthoc_formula"), h=6.5, size=9, weight="semibold", background=PANEL, padding=1.5,
           can_grow=True)
    p.y += 1.5
    ps = {**t["stats"]["pair"]}
    one_row_a_pair = '[section] = "pair" AND [stat] = "estimate"'
    pair = {"header": w["pair"], "value": '[level] & " – " & [level_2]'}

    def numeric(stat: str) -> dict:
        return {"header": ps[stat], "value": _pair_number(stat), "width": 36, "align": "right", "format": "0.########"}

    tables = t["tables"]
    p.add(_table("pairs", tables["pairs"]["title"], one_row_a_pair,
                 [pair, *(numeric(s) for s in ("estimate", "std_error", "statistic", "df"))], p.y), 10)
    decision = _pair_value("decision", "text")
    # the studentized range gives a pair no p-value of its own: only the adjusted one
    own_p = [numeric("p_value")] if posthoc == "pairwise_t" else []
    p.add(_table("pairs_p", tables["pairs_p"]["title"], one_row_a_pair, [
        pair, *own_p, numeric("p_adjusted"),
        {"header": ps["decision"], "value": f'IF({decision} = "reject_h0", {_q(w["reject"])}, {_q(w["keep"])})', "width": 50},
    ], p.y), 10)
    if intervals:
        p.add(_table("pairs_ci", tables["pairs_ci"]["title"], one_row_a_pair,
                     [pair, *(numeric(s) for s in ("critical", "ci_low", "ci_high"))], p.y), 10)
        # the differences with their intervals, while they are few enough to be read on one chart
        p.y += 2.0
        p.add({"id": "c_pairs", "type": "chart", "dataset": DATASET, "kind": "interval", "x": 0, "y": p.y, "w": WIDTH, "h": 62,
               "title": _fit(t["charts"]["pairs"], 7.5, WIDTH - 4, 700), "filter": '[section] = "pair"',
               "category": '[level] & " – " & [level_2]',
               "series": [{"value": f'SUMIF([stat] = "{s}", [value])', "name": ps[s]} for s in ("estimate", "ci_low", "ci_high")],
               "options": {"labels": True, "format": "0.###", "target": 0},
               "visible": f'=COUNTIF({one_row_a_pair}) <= {MAX_PAIRS_CHARTED}'}, 62.5)


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


def _density(t: dict) -> _Chart:
    # the first value of `split_by` in the rows' order is the line, the others the regions under it
    return _Chart("c_curve", "density", t["charts"]["density"],
                  {"filter": '[section] = "curve"', "category": "[x0]", "split_by": "[stat]",
                   "series": [{"value": "[value]", "name": t["stats"]["curve"]["density"]}],
                   "options": {"legend": False, "target": _value("test|||statistic"), "format": "0.000"},
                   "visible": '=COUNTIF([section] = "curve") > 0'})


def _cards(t: dict, own: dict, fourth: _Card | None = None, two_df: bool = False) -> tuple[_Card, _Card, _Card, _Card]:
    return (_Card("k_statistic", own["kpi"]["statistic"], _shown(_value("test|||statistic"))),
            _df_card(t, two_df),
            _Card("k_p", t["kpi"]["p_value"], _p_value()),
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
    pairs = test == "anova" and _param(test, params, "posthoc") != "none"
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


LAYOUTS: dict[str, Callable[[str, dict[str, Any], str, str | None, str | None], dict]] = {
    "t_one_sample": _one_sample,
    "t_two_sample": _two_groups,
    "t_paired": _paired,
    "anova": _several_groups,
    "var_f": _two_groups,
    "levene": _several_groups,
    "pearson": _correlation,
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
