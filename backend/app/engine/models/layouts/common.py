"""The blocks every model's report has (docs/specs/statistical-models.md, «The layouts»): the
bands, the summary cards and the formula, the assumptions' table with R's stars, the
coefficients' tables, the numbers of the fit, the diagnostics' charts, the notes. Built on
the statistics' page (`app/engine/stats/layouts.py`: its page, bands, tables and
expressions), so the two reports look and read alike.

A model's own file (`linear.py`, `logistic.py`, …) says what is its: its name, its four
cards, its tables of coefficients, its charts and its own blocks, and calls `page`. The page
does no computation on the data: every number is read from the model's rows, by their key
(`num("model|||r_squared")`) or by their section (a table's or a chart's `filter`).

The words are in `report_texts/common.json` and in one file per model, five languages each,
the same keys in each; a model's words take precedence over the common ones. Only
characters the PDF's font has (`app/reports/glyphs.py`).
"""
from __future__ import annotations

import json
import math
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Mapping, NamedTuple
from urllib.parse import unquote

from app.engine.models import catalog
from app.engine.stats import layouts as stats
from app.reports import glyphs
from app.engine.stats.layouts import ACCENT, DATASET, EXACT, LOCALES, MUTED, P4, PANEL, WIDTH

TEXTS = Path(__file__).resolve().parent.parent / "report_texts"
# the most rows a model's results hold (`results.py` refuses more): the report reads them whole
MAX_REPORT_ROWS = catalog.LIMITS["max_result_rows"]
# above this many rows every test rejects very small departures: the assumptions say so
MANY_ROWS = 5000
GOOD, BAD, MID = "#2b8a3e", "#c92a2a", "#b06f00"
# p-values read from a table that stops at its ends (tseries' adf.test: 0.01 to 0.99; kpss.test:
# 0.01 to 0.1): at an end the table says «at most» or «at least», and so does the page
BOUNDED_P = {"adf": (0.01, 0.99), "kpss": (0.01, 0.1)}
VERDICT_COLOURS = {"ok": GOOD, "doubtful": MID, "failed": BAD, "not_computed": MUTED}
NUMBER = "0.######"  # a number of a table: six decimals at most, none it does not have
CHECK_NUMBER = "#,##0.####"  # a check's value and threshold: four decimals at most, none it does not have


# ── words ───────────────────────────────────────────────────────────────────
@lru_cache(maxsize=16)
def _file(name: str) -> dict[str, dict]:
    path = TEXTS / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _merged(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """`b` over `a`, object by object."""
    out = dict(a)
    for k, v in b.items():
        out[k] = _merged(out[k], v) if isinstance(v, Mapping) and isinstance(out.get(k), Mapping) else v
    return out


def _lang(name: str, locale: str | None) -> dict:
    texts = _file(name)
    return texts.get(locale or "en") or texts.get("en") or {}


@lru_cache(maxsize=64)
def _words(model: str, variant: str, locale: str) -> dict:
    own = _lang(model, locale)
    if variant == "penalized":  # the blocks both regressions share when a penalty is on
        own = _merged(own, _lang("penalized", locale))
    family = catalog.MODELS.get(model, {}).get("family")
    # the words XGBoost and CatBoost share; those `clv` and `bg_bb` share; those of the A/B test and its planning
    if family in ("boosting", "customer_lifetime", "experiments"):
        own = _merged(_lang(family, locale), own)
    return _merged(_lang("common", locale), own)


def words(model: str, variant: str, locale: str | None) -> dict:
    """The words of a model's report in a language (English for one that is not there): the
    common ones, the model's over them, and with a penalty the penalized ones over those."""
    return _words(model, variant, locale if locale in LOCALES else "en")


def code(locale: str | None) -> str:
    return locale if locale in LOCALES else "en"


# ── the names of the numbers, of the checks, of the settings ────────────────
_WILDCARDS = (
    (re.compile(r"^note_(\d+)$"), "note"),
    (re.compile(r"^quantile_(.+)_ci_low$"), "quantile_low"),
    (re.compile(r"^quantile_(.+)_ci_high$"), "quantile_high"),
    (re.compile(r"^quantile_(.+)$"), "quantile"),
    (re.compile(r"^surv_at_(.+)_low$"), "surv_at_low"),
    (re.compile(r"^surv_at_(.+)_high$"), "surv_at_high"),
    (re.compile(r"^surv_at_(.+)$"), "surv_at"),
    (re.compile(r"^ar_root_modulus_(\d+)$"), "ar_root"),
    (re.compile(r"^ma_root_modulus_(\d+)$"), "ma_root"),
    (re.compile(r"^orders_reason_(\d+)$"), "orders_reason"),
    (re.compile(r"^order_test_(.+)$"), "order_test"),
)


def stat_label(w: dict, section: str, stat: str) -> str:
    """How a row's `stat` is called on the page: its name in the words of its section, a
    numbered one by its pattern («Note 2», «Survival at 30»), else its raw name (a number
    newer than the words)."""
    named = w.get("stats", {}).get(section, {}).get(stat)
    if named:
        return named
    for pattern, key in _WILDCARDS:
        m = pattern.match(stat)
        if m and key in w.get("patterns", {}):
            return w["patterns"][key].replace("{x}", m.group(1))
    return stat


def check_name(w: dict, check: str) -> str:
    """What an assumption's check is called: «Homoscedasticity: Breusch–Pagan»."""
    return w.get("checks", {}).get(check, {}).get("name") or check


def with_labels(model: str, params: Mapping[str, Any] | None, rows: list[dict], locale: str | None) -> list[dict]:
    """The rows as the page reads them, with `label` (the translated name of each `stat`; of
    an assumption's `verdict` row, the check's name) and `tiny` (a very small number by its
    exponent). What the runner writes in English is said in the page's language where the
    words have it: an assumption's H0, its advice (only when the check failed, is doubtful or
    was not computed), a setting's value («hc3»). A histogram's interval gets its edges as
    text. None of it is a computation: it is how a row is said."""
    from app.reports import formats

    checked = catalog.check_params(model, dict(params or {}))
    w = words(model, catalog.variant(model, checked), locale)
    loc = formats.locale(code(locale))
    model_numbers = {r["stat"]: r["value"] for r in rows if r["section"] == "model" and isinstance(r.get("value"), (int, float))}
    verdicts = {(r["level"], r["level_2"]): r.get("text") for r in rows
                if r["section"] == "assumption" and r["stat"] == "verdict"}
    advice_ids = {(r["level"], r["level_2"]): r.get("text") for r in rows
                  if r["section"] == "assumption" and r["stat"] == "advice_id"}
    out = []
    for r in rows:
        section, stat, text = r["section"], r["stat"], r.get("text")
        label = stat_label(w, section, stat)
        if label != stat and loc.decimal != ".":  # «Quantile 0.25», «Survival at 0.5»: the language's decimal in a label made of a number
            label = re.sub(r"(\d)\.(\d)", lambda m: m.group(1) + loc.decimal + m.group(2), label)
        if section == "assumption":
            check = w.get("checks", {}).get(r["level"], {})
            verdict = verdicts.get((r["level"], r["level_2"]))
            if stat == "verdict":
                label = check_name(w, r["level"])
            elif stat == "h0" and check.get("h0"):
                text = by_params(check["h0"], checked)
            elif stat == "advice":
                said = check.get("not_computed") if verdict == "not_computed" else check.get("advice")
                if isinstance(said, dict):  # several advices: the one of the id the runner wrote (`advice_id`), else the general one
                    said = said.get(advice_ids.get((r["level"], r["level_2"])) or "") or said.get("")
                text = (said or text) if verdict in ("failed", "doubtful", "not_computed") else None
            elif stat == "count" and isinstance(r.get("value"), (int, float)):
                text = count_said(w, r["level"], r["value"], loc.group)
        elif section == "model" and isinstance(text, str):
            text = w.get("values", {}).get(stat, {}).get(text, text)
            if stat.startswith("note_") and text.startswith("@"):  # a note the runner sends as an id and its parameters
                text = structured_note(w, text, loc)
            elif stat.startswith("note_") and stat[5:] in w.get("note_texts", {}):  # a note by its id: the runner's wording may change, the page's does not
                text = note_said(w["note_texts"][stat[5:]], model_numbers, loc.group)
            elif stat == "profile":
                text = profile_said(w, text, loc)
            elif stat == "formula":
                text = structured_note(w, text, loc, "formula_texts") if text.startswith("@") else formula_said(w, text, loc)
            elif text.startswith("@") and text[1:].split(" ")[0] in w.get("note_texts", {}):  # a measure said as a note (A/B planning's history_short)
                text = structured_note(w, text, loc)
        elif section == "reading" and isinstance(text, str) and text.startswith("@"):
            text, label = reading_said(w, r["level"], text, loc, checked, label)
        elif section == "hist" and isinstance(r.get("x0"), (int, float)) and isinstance(r.get("x1"), (int, float)):
            text = stats.edges_text(r["x0"], r["x1"], False, loc.decimal, loc.group)
        out.append({**r, "text": text, "label": label, "tiny": stats.tiny(r.get("value"), code(locale))})
    return out


NUMBER_IN_TEXT = re.compile(r"-?\d+(?:\.\d+)?(?:e[-+]?\d+)?")
# The values of a note that are names (of a column, a stratum, a distribution, a purchase model, a unit, a variant of
# an experiment), never numbers: a stratum or a column called «2.5» stays «2.5» in every language, as in the tables
NOTE_NAMES = frozenset({"stratum", "pairs", "cluster", "weights", "names", "distribution", "other", "models", "unit",
                        "variant", "variants", "control", "metric", "covariate", "treatments"})
# The forms a template may ask a number in, «{x:pct}» (docs/specs/ab-testing.md, «Explaining the method in the report»):
# only the sentences that ask for one change, so every other report says its numbers as before
NUMBER_FORMS = ("pct", "spct", "int", "p", "num", "date")
MINUS = "−"


def _decimal(text: str, loc: Any) -> str:
    """Every number written with a point in a text, with the language's decimal."""
    if loc.decimal == ".":
        return text
    return re.sub(r"(\d)\.(\d)", lambda m: m.group(1) + loc.decimal + m.group(2), text)


def number_form(x: float, form: str, loc: Any) -> str:
    """A number in one of NUMBER_FORMS, in the language of `loc` (its decimal mark, its thousands' separator, its dates):
    `pct` a share as a percent, three significant digits and two decimals at most, none it does not need («4.1 %»,
    «20.6 %», «0.25 %», «< 0.01 %» for a share above zero the two decimals would write 0); `spct` the same with its
    sign («+4.1 %», «−3.4 %»: a lift); `int` a whole number with the thousands' separator («12,480»); `p` a p-value as
    the tables write it, four decimals and «< 0.0001» below; `num` four significant digits («41.2», «0.048»), a number of
    a thousand or more whole with two decimals at most («1,840.5»); `date` a day counted from 1970-01-01 as the language
    writes a date. A negative number takes the minus sign «−»."""
    from datetime import date, timedelta

    from app.reports import formats

    def plain(v: float, pattern: str) -> str:
        said = formats.format_number(abs(v), pattern, loc)
        return (MINUS + said) if v < 0 and said.strip("0" + loc.decimal + loc.group) else said

    if form == "date":
        return formats.format_date(date(1970, 1, 1) + timedelta(days=int(round(x))), loc.date, loc)
    if form == "int":
        return plain(x, "#,##0")
    if form == "p":
        return "< " + formats.format_number(0.0001, "0.0000", loc) if x < 0.0001 else formats.format_number(x, "0.0000", loc)
    if form == "num":
        if x == 0 or abs(x) >= 1000:
            return plain(x, "#,##0.##")
        decimals = min(10, max(0, 3 - math.floor(math.log10(abs(x)))))
        return plain(x, "#,##0" + ("." + "#" * decimals if decimals else ""))
    v = x * 100.0  # pct, spct
    if v != 0 and abs(v) < 0.005:
        said = "< " + formats.format_number(0.01, "0.00", loc)
    elif 99.995 <= abs(v) < 100:  # a probability just under 1 is not 100 %
        said = "> " + formats.format_number(99.99, "0.00", loc)
    else:
        said = formats.format_number(abs(v), "#,##0" if abs(v) >= 100 else "#,##0.#" if 9.995 <= abs(v) < 99 else "#,##0.##", loc)
    sign = (MINUS if v < 0 else "+" if v > 0 else "") if form == "spct" else (MINUS if v <= -0.005 else "")
    return f"{sign}{said} %"


def structured_note(w: dict, text: str, loc: Any, texts: str = "note_texts") -> str:
    """A note sent as «@id name=value name=value…» (a list is joined with «,»): the page's sentence
    of `note_texts.<id>`, each «{name}» replaced by its value, a number with the language's decimal, a name of a
    distribution, of a purchase model or of a unit of time in the page's words (`values.distribution`,
    `values.purchase_model`, `values.time_unit`). A formula sent the same way (BG/BB's «@bgbb_formula unit=year») is
    read from `formula_texts`. A value that is a name (NOTE_NAMES: a stratum, a column, a distribution…) is never
    read as a number: a stratum «2.5» stays «2.5». A value's «%», « » and «,» come encoded (%25, %20, %2C: the survival runner's
    surv_note, so that a stratum «North Side» or «a,b» does not split the note) and are decoded here, after the
    split on «,». «{name:pct}» and the other NUMBER_FORMS say a number in that form (number_form); a list of numbers in
    a form is joined with « / » (a split of the units, «50.1 % / 49.9 %»); a name is never put in a form."""
    head, *rest = text[1:].split(" ")
    said = w.get(texts, {}).get(head)
    if said is None:
        return text
    values = w.get("values", {})
    names = {**values.get("purchase_model", {}), **values.get("distribution", {}), **values.get("time_unit", {})}
    # a unit of time in another form: «{unit:one}» its singular («one opportunity a year»), «{unit:dative}» the plural
    # after a preposition (German's dative, «in Monaten»; the plural in the other languages)
    forms = {"one": values.get("time_unit_one", {}), "dative": values.get("time_unit_dative", {})}
    parts = dict(r.partition("=")[::2] for r in rest if "=" in r)

    def word(v: str, form: str = "", name: bool = False) -> str:
        v = unquote(v)
        if not name and NUMBER_IN_TEXT.fullmatch(v):
            return _decimal(v, loc)
        return forms[form].get(v, names.get(v, v)) if form else names.get(v, v)

    def shaped(v: str, form: str, name: bool) -> str:
        v = unquote(v)
        if name:
            return names.get(v, v)
        # a list of numbers sent with its commas encoded («0.5%2C0.5») is a list too: a number holds no comma
        items = v.split(",")
        if not all(NUMBER_IN_TEXT.fullmatch(x) for x in items):
            return names.get(v, v)
        return " / ".join(number_form(float(x), form, loc) for x in items)

    out = said
    for k, v in parts.items():
        for form in ("", *forms):
            out = out.replace("{" + k + (":" + form if form else "") + "}",
                              ", ".join(word(x, form, k in NOTE_NAMES) for x in v.split(",")))
        for form in NUMBER_FORMS:
            mark = "{" + k + ":" + form + "}"
            if mark in out:
                out = out.replace(mark, " / ".join(shaped(x, form, k in NOTE_NAMES) for x in v.split(",")))
    return out


def reading_said(w: dict, block: str | None, text: str, loc: Any, params: Mapping[str, Any], label: str) -> tuple[str, str]:
    """A row of an experiment's explanation (section `reading`, «@id name=value…», docs/specs/ab-testing.md,
    «Explaining the method in the report»): the sentence of `reading_texts.<id>` (of `formula_texts.<id>` in the
    block `formulas`) with its numbers in their forms, and what the row is about as its label — the id's own name
    (`reading_labels`), else its block's (`reading_blocks`). A sentence may depend on the node's settings, as a
    check's words do (by_params: «higher_is_better=false»). An id the words do not have stays as the runner wrote it."""
    kind = "formula_texts" if block == "formulas" else "reading_texts"
    head = text[1:].split(" ")[0]
    said = w.get(kind, {}).get(head)
    if said is None:
        return text, label
    sentence = structured_note({**w, kind: {head: by_params(said, params) or ""}}, text, loc, kind)
    return sentence, w.get("reading_labels", {}).get(head) or w.get("reading_blocks", {}).get(block or "") or label


def profile_said(w: dict, text: str, loc: Any) -> str:
    """«load = 52.3 (mean); line = A (reference)» in the page's words and decimals."""
    for key, word in w.get("profile_words", {}).items():
        text = text.replace(f"({key})", f"({word})")
    return _decimal(text, loc)


CLV_FORMULA = re.compile(r"^x: frequency, t_x: recency, T: age, in (day|week|month|quarter|year)s?$")


def formula_said(w: dict, text: str, loc: Any = None) -> str:
    """The runner's formula in the page's language: a customer lifetime's «x: frequency, t_x: recency, T: age,
    in weeks» (the sentence of `formula_texts.clv_formula`, its unit in the page's words) and a parametric
    survival's «· Weibull, accelerated failure time»; the user's column names stay."""
    m = CLV_FORMULA.match(text)
    if m and "clv_formula" in w.get("formula_texts", {}):
        return structured_note(w, f"@clv_formula unit={m.group(1)}", loc, "formula_texts")
    kinds = w.get("formula_kinds", {})
    names = w.get("values", {}).get("distribution", {})

    def survival(m: re.Match) -> str:
        key = re.sub(r"[^a-z]", "", m.group(1).lower())
        return f" · {names.get(key, m.group(1))}, {kinds.get(m.group(2), m.group(2))}"

    return re.sub(r" · ([A-Za-z -]+), (accelerated failure time|proportional hazards)$", survival, text) if kinds else text


def by_params(said: str | dict, params: Mapping[str, Any]) -> str | None:
    """A text that may change with the node's settings: a string, or a dict whose keys are
    conditions on the parameters («early_stopping=false», «class_weight=balanced», several joined
    by «&»; a boolean said true or false) and whose "" is the text of every other case. The first
    key whose conditions all hold wins: what the check reads is decided by the settings, which the
    page knows as the runner did."""
    if not isinstance(said, dict):
        return said

    def holds(condition: str) -> bool:
        key, _, want = condition.partition("=")
        v = params.get(key.strip())
        return (("true" if v else "false") if isinstance(v, bool) else str(v)) == want.strip()

    for key, text in said.items():
        if key and all(holds(c) for c in key.split("&")):
            return text
    return said.get("")


def note_said(template: str, numbers: Mapping[str, float], group: str) -> str:
    """A note's sentence, each `{stat}` in it replaced by that number of the model section
    («SHAP on {shap_rows} of {n} rows»), with the language's group separator."""
    def number(m: re.Match) -> str:
        x = numbers.get(m.group(1))
        if x is None:
            return m.group(0)
        return f"{int(x):,}".replace(",", group) if float(x).is_integer() else str(x)

    return re.sub(r"\{(\w+)\}", number, template)


def count_said(w: dict, check: str, n: float, group: str) -> str:
    """What an assumption's `count` counts, with its number: «9 rows over the threshold», «1 pair
    of curves that cross» — the check's words, else the common ones (singular for 1)."""
    said = w.get("checks", {}).get(check, {}).get("count") or w["assumptions"]["count"]
    number = f"{int(n):,}".replace(",", group) if float(n).is_integer() else str(n)
    return said["one" if n == 1 else "other"].replace("{n}", number)


def columns() -> list[dict[str, str]]:
    """The columns of the dataset `result`: the ten, `label` and `tiny` (the statistics')."""
    return stats.columns()


# ── expressions ─────────────────────────────────────────────────────────────
q = stats.quoted
num = stats.value_of
txt = stats.text_of
shown = stats.shown
join = stats.join


def model_key(stat: str) -> str:
    return f"model|||{stat}"


def row_key(section: str, stat: str) -> str:
    """The key of the row `stat` of the term (check, stratum, pair) of the row at hand: its
    section, its level and its level_2 (empty when null: `&` writes NULL as nothing)."""
    return f'"{section}|" & [level] & "|" & [level_2] & "|{stat}"'


def of_row(section: str, stat: str, column: str = "value") -> str:
    return f"LOOKUP({row_key(section, stat)}, [key], [{column}])"


# a number of a table at least this large, or this small and not zero, is written by its exponent with four
# significant digits («6.2e+150», «8.54e-10»): its digits would run out of its column
BY_EXPONENT_FROM, BY_EXPONENT_BELOW = 1e7, 1e-4
SCIENTIFIC = "0.###e+0"


def far(x: str) -> str:
    """The condition of a number written by its exponent (BY_EXPONENT_FROM, BY_EXPONENT_BELOW)."""
    return f"(ABS({x}) >= {BY_EXPONENT_FROM:.0f} OR ({x} <> 0 AND ABS({x}) < {BY_EXPONENT_BELOW:g}))"


def number_of_row(section: str, stat: str) -> str:
    """A number of the row at hand's term, with the decimals its column has room for; a very large or
    very small one by its exponent, four significant digits (a column of some twelve characters)."""
    x = of_row(section, stat)
    return f"IF(ISNULL({x}), {x}, IF({far(x)}, {shown(x, SCIENTIFIC)}, {stats.rounded(x)}))"


def p_said(x: str) -> str:
    """A p-value as a result is written: four decimals, «< 0.0001» below; nothing when there is none."""
    return f'IF(ISNULL({x}), "", IF({x} < 0.0001, "< " & {shown("0.0001", P4)}, {shown(x, P4)}))'


def p_of_check(x: str) -> str:
    """An assumption's p-value: as p_said, or «≤ 0.01», «≥ 0.1» at the ends of a check whose
    p-value is read from a table (BOUNDED_P)."""
    out = p_said(x)
    for check, (low, high) in BOUNDED_P.items():
        out = (f'IF([level] = "{check}" AND NOT ISNULL({x}) AND {x} <= {low}, "≤ " & {shown(str(low), "0.##")}, '
               f'IF([level] = "{check}" AND NOT ISNULL({x}) AND {x} >= {high}, "≥ " & {shown(str(high), "0.##")}, {out}))')
    return out


def p_with_stars(stat: str) -> str:
    """A p-value of the `model` section with its significance code after it: «0.0004 ***»."""
    sig = stat[: -len("p_value")] + "significance"
    return stats.with_stars(p_said(num(model_key(stat))), txt(model_key(sig)))


def term() -> str:
    """A coefficient's term as the page writes it: «price», «channel: web»."""
    return '[level] & IF(ISNULL([level_2]) OR [level_2] = "", "", ": " & [level_2])'


def exists(condition: str) -> str:
    return f"=COUNTIF({condition}) > 0"


# ── the parts of a page ─────────────────────────────────────────────────────
class Card(NamedTuple):
    """A card of the summary: its label, the expression of its value, its format, `visible`;
    `under`: a short line under it (an interval)."""

    id: str
    label: str
    value: str
    format: str | None = None
    visible: str | None = None
    under: str | None = None


class Chart(NamedTuple):
    """A chart of the diagnostics: its kind (`xy`, `column`, `interval`, `heatmap`…), its
    title, the rest of its item (`more`: filter, category, series, options, visible), the
    check it illustrates (`under`, a caption under it says its verdict), `wide`: across the
    page, `caption`: a sentence under it (how to read it), a few lines long at most."""

    id: str
    kind: str
    title: str
    more: dict
    under: str | None = None
    wide: bool = False
    caption: str | None = None
    measured: str | None = None  # a caption that is an expression: a text as long as what it says, to measure its lines


class CoefTable(NamedTuple):
    """A table of the coefficients: one row a term, a column for each stat."""

    id: str
    title: str
    stats: tuple[str, ...]
    width: float = 24.0
    fmt: str = NUMBER  # the numbers' pattern: six decimals at most; a model with small estimates asks for more


Block = Callable[["stats.Page", dict], None]


def xy(id_: str, title: str, filter_: str, series: list[dict], options: dict | None = None, under: str | None = None,
       split_by: str | None = None, wide: bool = False, visible: str | None = None) -> Chart:
    """An `xy` chart of the rows `filter_` keeps, x their `x0`."""
    more: dict[str, Any] = {"filter": filter_, "category": "[x0]", "series": series, "options": {"format": "0.###", **(options or {})}}
    if split_by:
        more["split_by"] = split_by
    more["visible"] = visible or exists(filter_)
    return Chart(id_, "xy", title, more, under, wide)


def mark(stat: str, as_: str, name: str = "", label: bool = False) -> dict:
    """A series of an `xy` chart: the rows whose `stat` is `stat`, drawn as `as_`."""
    s: dict[str, Any] = {"value": f'IF([stat] = "{stat}", [value], NULL)', "as": as_, "name": name}
    if label:
        s["label"] = "[text]"
    return s


def histogram(id_: str, title: str, level: str, name: str, under: str | None = None, split: bool = False) -> Chart:
    """The counts of a histogram's intervals (section `hist`), one bar an interval."""
    f = f'[section] = "hist" AND [level] = "{level}"'
    more: dict[str, Any] = {"filter": f, "category": "[text]", "series": [{"value": "SUM([value])", "name": name}],
                            "options": {"legend": split, "format": "#,##0"}, "visible": exists(f)}
    if split:
        more["split_by"] = "[level_2]"
    return Chart(id_, "column", title, more, under)


def coef_interval(id_: str, title: str, stats_: tuple[str, str, str], names: tuple[str, str, str], target: float,
                  log: bool = False, filter_: str = '[section] = "coef"') -> Chart:
    """The coefficients (or their ratios) with their intervals, a line at `target`."""
    more = {"filter": filter_, "category": term(), "options": {"labels": True, "format": "0.###", "target": target, "log": log},
            "series": [{"value": f'SUMIF([stat] = "{s}", [value])', "name": n} for s, n in zip(stats_, names)],
            "visible": exists(f'{filter_} AND [stat] = "{stats_[0]}"')}
    return Chart(id_, "interval", title, more)


# ── the page ────────────────────────────────────────────────────────────────
def subject(params: Mapping[str, Any], outcome: str = "outcome", predictors: str = "predictors") -> str:
    """«quantity ~ price + channel», from the node's columns."""
    left = str(params.get(outcome) or "")
    right = " + ".join(str(p) for p in params.get(predictors) or [])
    return f"{left} ~ {right}" if left and right else left or right


def page(model: str, params: Mapping[str, Any], locale: str, sample: list[dict] | None, step: str | None, *,
         name: str, about: str, cards: tuple[Card | tuple[Card, ...], ...], coef_tables: tuple[CoefTable, ...] = (),
         coef_note: str | tuple[str, str] | None = None, blocks: tuple[Block, ...] = (), charts: tuple[Chart, ...] = (),
         assumptions_note: str | None = None, p_values: bool = True, fit_skip: tuple[str, ...] = (),
         summary_lines: tuple[tuple[str, str], ...] = (), fit_format: str = EXACT, after_assumptions: tuple[Block, ...] = (), coef_first: tuple[Block, ...] = (),
         after_coefficients: tuple[Block, ...] = ()) -> dict:
    """A model's report: the summary (four cards, the formula), the assumptions (first page),
    the coefficients, the fit, the model's own blocks (strata, forecast…), the diagnostics,
    the notes. `name`: the model as the header says it; `about`: what it is about;
    `assumptions_note`: a sentence under the checks (a penalized fit's: diagnostics, not tests);
    `p_values`: False for a variant with no p-value (no statistic, df, p or R's codes in the
    checks and the fit, no warning about the tests on many rows); `fit_skip`: numbers of the
    `model` section the fit's table leaves out (the model's own block says them; a name ending in «_» is a prefix);
    `fit_format`: the pattern of the fit's numbers (all their digits by default); `summary_lines`: (id, expression) of lines under the formula (a customer lifetime's business
    settings); `coef_first`: blocks drawn under the coefficients' heading and note, before their tables
    and kept with the heading on one page; `after_coefficients`: blocks under the tables, before the fit;
    `coef_note` may be a (text, visible) pair."""
    from app.reports import formats

    lang = code(locale)
    checked = catalog.check_params(model, dict(params))
    variant = catalog.variant(model, checked)
    w = words(model, variant, lang)
    sample_text = stats.sample_line(sample, lang)
    header, footer = stats.bands(name, " · ".join(x for x in (about, step) if x), w, formats.locale(lang).texts, sample_text)
    p = stats.Page()
    _summary(p, w, cards, summary_lines)
    _assumptions(p, w, lang, assumptions_note, p_values)
    for block in after_assumptions:
        block(p, w)
    if coef_tables:
        _coefficients(p, w, lang, coef_tables, coef_note, coef_first)
    for block in after_coefficients:
        block(p, w)
    _fit(p, w, p_values, fit_skip, fit_format)
    for block in blocks:
        block(p, w)
    if charts:
        _diagnostics(p, w, charts)
    _notes(p, w)
    return stats.document(header, p.items, footer, lang)


def _summary(p: stats.Page, w: dict, cards: tuple[Card | tuple[Card, ...], ...],
             lines: tuple[tuple[str, str], ...] = ()) -> None:
    """Four places of cards; a place may hold alternatives, each `visible` when its rows say
    (the MAPE on the holdout, or in sample without one)."""
    p.heading("s_summary", w["sections"]["summary"])
    for place, x in zip(cards, (0, 47, 94, 141)):
        for card in ((place,) if isinstance(place, Card) else place):
            item: dict[str, Any] = {"id": card.id, "type": "kpi", "dataset": DATASET, "x": x, "y": p.y, "w": 45, "h": 20,
                                    "label": card.label, "value": card.value}
            if card.format:
                item["format"] = card.format
            if card.visible:
                item["visible"] = card.visible
            p.add(item)
            if card.under:
                p.text(card.id + "_under", "=" + card.under, h=4.5, x=x + 2, w=43, advance=False, size=7.5, color=MUTED,
                       visible=card.visible)
                p.items[-1]["y"] = p.y + 14.6
    p.y += 21.5
    p.keep()
    p.text("formula", "=" + txt(model_key("formula")), h=6.5, size=9.5, weight="semibold", background=PANEL, padding=1.5,
           can_grow=True, visible=exists('[key] = "model|||formula"'))
    for id_, expression in lines:
        p.text(id_, "=" + expression, h=6.5, size=9, background=PANEL, padding=1.5, can_grow=True)


def value_of_check(w: dict) -> str:
    """The column «Value» of the assumptions: the number the check reads (a VIF, an EPV, the
    largest Cook's distance), then what it counts in words («9 rows over the threshold»: the
    count row's text, with_labels), then the threshold of its verdict when it has no p-value —
    «0.31 · 2 rows over the threshold (threshold 0.79)». Never a bare count."""
    value, count, threshold = (of_row("assumption", s) for s in ("value", "count", "threshold"))
    counted = of_row("assumption", "count", "text")
    pv = of_row("assumption", "p_value")
    has_value, has_count = f"NOT ISNULL({value})", f"NOT ISNULL({count})"
    main = (f'IF({has_value}, {shown(value, CHECK_NUMBER)} & IF({has_count} AND {count} > 0, " · " & {counted}, ""), '
            f'IF({has_count}, {counted}, ""))')
    limit = (f'IF(NOT ISNULL({threshold}) AND ISNULL({pv}) AND ({has_value} OR {has_count}), '
             f'" (" & {q(w["assumptions"]["threshold"])} & " " & {shown(threshold, CHECK_NUMBER)} & ")", "")')
    return f"{main} & {limit}"


def _assumptions(p: stats.Page, w: dict, lang: str, note: str | None = None, p_values: bool = True) -> None:
    """One row a check: what it checks, H0, the statistic, df, p with R's stars, the value it
    reads (a VIF, an EPV, the largest Cook's distance) with what it counts and its threshold,
    the verdict in words and with a mark; then what to do about those that failed, are
    doubtful or were not computed."""
    a = w["assumptions"]
    p.heading("s_assumptions", w["sections"]["assumptions"])
    verdict = "LOOKUP(" + row_key("assumption", "verdict") + ", [key], [text])"
    said = "IFS(" + ", ".join(f'[text] = "{v}", {q(w["verdicts"][v])}' for v in catalog.VERDICTS) + ', "")'
    df = of_row("assumption", "df")
    df2 = of_row("assumption", "df_2")
    one_row = '[section] = "assumption" AND [stat] = "verdict"'
    cols = [
        {"header": a["check"], "value": '[label] & IF(ISNULL([level_2]) OR [level_2] = "", "", " — " & IF([level_2] = "all", ' + q(w["all"]) + ", [level_2]))", "width": 36},
        {"header": a["h0"], "value": of_row("assumption", "h0", "text"), "style": {"size": 7}},
        {"header": a["statistic"], "value": number_of_row("assumption", "statistic"), "width": 17, "align": "right",
         "format": "0.####"},
        {"header": a["df"], "value": f'IF(ISNULL({df}), "", FORMAT({df}, "0.##") & IF(ISNULL({df2}), "", "; " & FORMAT({df2}, "0.##")))',
         "width": 12, "align": "right"},
        {"header": a["p_value"], "value": p_of_check(of_row("assumption", "p_value")), "width": 17, "align": "right"},
        {"header": w["signif"]["column"], "value": of_row("assumption", "significance", "text"), "width": 10},
        {"header": a["value"], "value": value_of_check(w), "width": 34, "style": {"size": 7}},
        {"header": a["verdict"], "value": said, "width": 24,
         "rules": [{"when": f'[text] = "{v}"', "style": {"color": c, "weight": "semibold"}} for v, c in VERDICT_COLOURS.items()]},
    ]
    if not p_values:  # no statistic, df or p to show: the checks are read by their value and verdict
        cols = [cols[0], cols[1], cols[6], cols[7]]
    p.add(stats.table("assumptions", a["title"], one_row, cols, p.y, visible=exists(one_row)), 10)
    p.text("assumptions_none", a["none"], h=5, color=MUTED, visible=f"=COUNTIF({one_row}) = 0")
    if note:
        p.text("assumptions_note", note, h=5, size=8, color=MUTED, can_grow=True)
    if p_values:
        p.text("signif_legend", stats.static(stats.signif_legend(w, lang)), h=4.5, size=7, color=MUTED)
        p.text("many_rows", "=" + join(a["many_rows"], n=shown(num(model_key("n")), "#,##0")), h=5, size=8, color=MID,
               can_grow=True, visible=f"={num(model_key('n'))} > {MANY_ROWS}")
    p.y += 1.0
    advised = '[section] = "assumption" AND [stat] = "advice" AND NOT ISNULL([text]) AND [text] <> ""'
    p.add(stats.table("advice", a["advice_title"], advised, [
        {"header": a["check"], "value": f'LOOKUP({row_key("assumption", "verdict")}, [key], [label]) & '
                                        f'IF(ISNULL([level_2]) OR [level_2] = "", "", " — " & [level_2])', "width": 46},
        {"header": a["verdict"], "value": f'IFS({verdict} = "doubtful", {q(w["verdicts"]["doubtful"])}, '
                                          f'{verdict} = "failed", {q(w["verdicts"]["failed"])}, {q(w["verdicts"]["not_computed"])})',
         "width": 30},
        {"header": a["advice"], "value": "[text]"},
        {"header": a["rows"], "value": of_row("assumption", "rows", "text"), "width": 30, "style": {"size": 7}},
    ], p.y, visible=exists(advised)), 10)


def alpha_said(sentence: str, alpha: Any, locale: str) -> str:
    """The sentence that says the alpha the page's tests were read against («{alpha}» in it), in the language's decimals."""
    from app.reports import formats

    text = f"{float(alpha):g}".replace(".", formats.locale(locale).decimal)
    return sentence.replace("{alpha}", text)


def measures(ids: tuple[str, ...], counted: tuple[str, ...] = (), scientific: tuple[str, ...] = (),
             more: Mapping[str, str] | None = None):
    """A table of the checks that are numbers, with no verdict and no colour: those of `ids` whose
    rows have a `value` and no `verdict` (only those: a check the runner writes besides, which the
    page shows elsewhere, is not repeated here under its raw name); each says in a sentence of the
    model's words (`measure_texts.<id>`) what the number is. Those of `counted` add what their
    `count` row counts, in the check's words («· 174 repeat customers»). A check whose
    `measure_texts` entry is a dictionary says the sentence of the reading the runner wrote in the
    row's `advice_id` (the key of that id, else the general one under the key ""). Those of
    `scientific` write their value by its exponent («5E-5», «3E+5»): numbers far from 1, which a
    fixed number of decimals would round. `more`: per check, an expression of the row whose text is
    added after the sentence («· at t = 12.5, 2.3 standard errors»), nothing when it is empty."""
    def block(p: stats.Page, w: dict) -> None:
        m = w["measures"]
        declared = " OR ".join(f'[level] = "{i}"' for i in ids)
        one = ('[section] = "assumption" AND [stat] = "value" AND ISNULL(' + of_row("assumption", "verdict", "text") + ')'
               + f" AND ({declared})")
        reading = of_row("assumption", "advice_id", "text")

        def sentence(i: str) -> str:
            said = w["measure_texts"][i]
            if not isinstance(said, dict):
                return q(said)
            specific = [f"{reading} = {q(k)}, {q(v)}" for k, v in said.items() if k]
            return "IFS(" + ", ".join(specific) + f", TRUE, {q(said[''])})"

        what = "IFS(" + ", ".join(f'[level] = "{i}", {sentence(i)}' for i in ids) + ', "")'
        if counted:
            n = of_row("assumption", "count", "text")
            which = " OR ".join(f'[level] = "{i}"' for i in counted)
            what = f'{what} & IF(({which}) AND NOT ISNULL({n}), " · " & {n}, "")'
        for i, extra in (more or {}).items():
            what = f'{what} & IF([level] = "{i}" AND ({extra}) <> "", " · " & ({extra}), "")'
        names = "IFS(" + ", ".join(f'[level] = "{i}", {q(check_name(w, i))}' for i in ids) + ", [level])"
        value = {"value": number_of_row("assumption", "value"), "format": "0.####"}
        if scientific:
            far = " OR ".join(f'[level] = "{i}"' for i in scientific)
            raw = of_row("assumption", "value")
            value = {"value": f'IF({far}, {shown(raw, "0.###E+0")}, {shown(number_of_row("assumption", "value"), "0.####")})'}
        p.add(stats.table("measures", m["title"], one, [
            {"header": m["measure"], "value": names, "width": 52},
            {"header": m["group"], "value": 'IF(ISNULL([level_2]), "", IF([level_2] = "all", ' + q(w["all"]) + ", [level_2]))", "width": 24},
            {"header": m["value"], "width": 22, "align": "right", **value},
            {"header": m["what"], "value": what, "style": {"size": 7.5}},
        ], p.y, visible=exists(one)), 10)
    return block


def _coefficients(p: stats.Page, w: dict, lang: str, tables: tuple[CoefTable, ...], note: str | tuple[str, str] | None,
                  first: tuple[Block, ...] = ()) -> None:
    c = w["coef"]
    p.heading("s_coef", w["sections"]["coefficients"])
    for block in first:  # the charts: one band with the heading; the note, which grows, is under them
        block(p, w)
    if first:
        p.keep()
        p.y += 1.0
    if note:
        text, visible = note if isinstance(note, tuple) else (note, None)
        p.text("coef_note", text, h=5, color=MUTED, can_grow=True, visible=visible)
        if not first:
            p.keep()
        else:  # under the charts, the note is about the tables: it goes to their page with them
            p.items[-1]["keep_with_next"] = True
        p.y += 1.0
    for t in tables:
        one_row = f'[section] = "coef" AND [stat] = "{t.stats[0]}"'
        cols: list[dict] = [{"header": c["term"], "value": term()}]
        for s in t.stats:
            header = stat_label(w, "coef", s)
            if s == "p_value":
                cols.append({"header": header, "value": p_said(of_row("coef", "p_value")), "width": 18, "align": "right"})
                cols.append({"header": w["signif"]["column"], "value": of_row("coef", "significance", "text"), "width": 11})
            elif s == "selected":
                cols.append({"header": header, "value": f'IF({of_row("coef", "selected")} = 1, {q(c["yes"])}, {q(c["no"])})',
                             "width": t.width})
            else:
                cols.append({"header": header, "value": number_of_row("coef", s), "width": t.width, "align": "right",
                             "format": t.fmt})
        p.add(stats.table(t.id, t.title, one_row, cols, p.y, visible=exists(one_row)), 10)
    if any("p_value" in t.stats for t in tables):
        p.text("coef_signif_legend", stats.static(stats.signif_legend(w, lang)), h=4.5, size=7, color=MUTED)


def _fit(p: stats.Page, w: dict, p_values: bool = True, skip: tuple[str, ...] = (), pattern: str = EXACT) -> None:
    """Every number of the `model` section, then how it was fitted (its settings in words). The heading
    is kept with the table under it on one page (`keep_with_next`, stats.Page.introduce)."""
    f = w["fit"]
    p.heading("s_fit", w["sections"]["fit"])
    sig = f'LOOKUP("model|||" & LEFT([stat], LEN([stat]) - 7) & "significance", [key], [text])'
    numbers = '[section] = "model" AND NOT ISNULL([value])' + "".join(
        f' AND NOT STARTSWITH([stat], "{s}")' if s.endswith("_") else f' AND [stat] <> "{s}"' for s in skip)
    cols = [
        {"header": f["quantity"], "value": "[label]"},
        {"header": f["name"], "value": "[stat]", "width": 50, "style": {"color": MUTED}},
        # a p-value as everywhere else («< 0.0001», four decimals), not its ten digits; a number too large for the
        # column by its exponent; a very small one by its exponent with all its digits (`tiny`)
        {"header": f["value"], "value": f'IF(RIGHT([stat], 7) = "p_value", {p_said("[value]")}, IF(ISNULL([tiny]), '
                                         f'IF(ABS([value]) >= {BY_EXPONENT_FROM:.0f}, {shown("[value]", SCIENTIFIC)}, [value]), [tiny]))',
         "width": 34, "align": "right", "format": pattern},
        {"header": w["signif"]["column"], "value": f'IF(RIGHT([stat], 7) = "p_value", {sig}, "")', "width": 14},
    ]
    p.add(stats.table("fit", f["title"], numbers, cols if p_values else cols[:3], p.y), 10)
    hidden = ("model", "variant", "formula", "software", "group_version", "rows_from")
    settings = ('[section] = "model" AND ISNULL([value]) AND NOT ISNULL([text]) AND [text] <> "" AND '
                + " AND ".join(f'[stat] <> "{s}"' for s in hidden)
                + ' AND NOT STARTSWITH([stat], "note_") AND NOT STARTSWITH([stat], "orders_reason_") AND RIGHT([stat], 12) <> "significance"')
    p.add(stats.table("settings", f["settings_title"], settings, [
        {"header": f["setting"], "value": "[label]", "width": 70},
        {"header": f["value"], "value": "[text]"},
    ], p.y, visible=exists(settings)), 10)


def _diagnostics(p: stats.Page, w: dict, charts: tuple[Chart, ...]) -> None:
    grid(p, w, charts, ("s_diagnostics", w["sections"]["diagnostics"]))


def grid(p: stats.Page, w: dict, charts: tuple[Chart, ...], heading: tuple[str, str] | None, tall: float = 62.0) -> None:
    """The charts two by two (a wide one across the page), each with the verdict of the check
    it illustrates under it. A row of charts and its captions stay on one page (an invisible
    rectangle as tall as both makes them one row of the body), the heading with the first."""
    if heading:
        p.heading(*heading)
    half, gap, caption = 91.0, 4.0, 4.5
    column, top, first = 0, p.y, bool(heading)  # without a heading the first row keeps itself, like the others
    room = 0.0  # what the captions of the row at hand need under its charts (caption: a few lines of 7 pt)

    def close_row() -> None:
        nonlocal column, first, top, room
        below = max(caption, room)
        p.y = top + tall + below + 3
        if first:
            p.keep()
            first = False
        else:
            p.items.append({"id": f"row_{len(p.items)}_keep", "type": "rect", "x": 0, "y": top, "w": 0.1,
                            "h": round(tall + below, 2), "style": {}})
        column, top, room = 0, p.y, 0.0

    for chart in charts:
        if chart.wide and column == 1:  # a wide chart starts a row of its own
            close_row()
        x = 0.0 if column == 0 or chart.wide else half + gap
        width = WIDTH if chart.wide else half
        item: dict[str, Any] = {"id": chart.id, "type": "chart", "dataset": DATASET, "kind": chart.kind, "x": x, "y": top,
                                "w": width, "h": tall, "title": stats.fit(chart.title, 7.5, width - 4, 700), **chart.more}
        p.add(item)
        if chart.caption:  # how to read it: static words (or an expression, when it starts with «=»), as many lines of 7 pt as the width needs
            said = chart.caption.startswith("=")
            lines = -(-int(glyphs.width_mm(chart.measured or chart.caption.lstrip("="), 7)) // int(width - 2)) or 1
            height = round(3.1 * lines + 1.4, 2)
            room = max(room, height)
            p.items.append({"id": chart.id + "_how", "type": "text", "x": x, "y": top + tall + 0.3, "w": width, "h": height,
                            "value": chart.caption if said else stats.static(chart.caption), "style": {"size": 7, "color": MUTED},
                            "visible": chart.more.get("visible")})
        if chart.under:  # «check», or «check|term» for a check of each term
            key = f"assumption|{chart.under}|verdict" if "|" in chart.under else f"assumption|{chart.under}||verdict"
            said = "IFS(" + ", ".join(f'{txt(key)} = "{v}", {q(w["verdicts"][v])}' for v in catalog.VERDICTS) + ', "")'
            p.items.append({"id": chart.id + "_caption", "type": "text", "x": x, "y": top + tall + 0.3, "w": width, "h": caption,
                            "value": "=" + join(w["illustrates"], check=f'LOOKUP({q(key)}, [key], [label])', verdict=said),
                            "style": {"size": 7, "color": MUTED},
                            "visible": f'=COUNTIF([key] = {q(key)}) > 0 AND ' + (chart.more.get("visible") or "=TRUE").lstrip("=")})
        if chart.wide or column == 1:
            close_row()
        else:
            column = 1
    if column == 1:
        close_row()


def _notes(p: stats.Page, w: dict) -> None:
    n = w["notes"]
    p.heading("s_notes", w["sections"]["notes"])
    rows_from = txt(model_key("rows_from"))
    left_out = join(n["rows"], n=shown(num(model_key("n")), "#,##0"),
                    missing=shown(f"COALESCE({num(model_key('n_missing'))}, 0)", "#,##0"))
    software = join(n["software"], software=txt(model_key("software")), version=txt(model_key("group_version")))
    # the three lines are one text: a page never ends with the last of them alone
    p.text("notes_lines", f'=IF({rows_from} = "trial", {q(n["trial"])}, {q(n["run"])}) & "\n" & {left_out} & "\n" & {software}',
           h=14.5, can_grow=True)
    p.keep()  # the heading and the lines are words right above the notes' table: the report keeps them with it
    p.y += 1.5
    is_note = '[section] = "model" AND STARTSWITH([stat], "note_")'
    p.add(stats.table("notes", n["title"], is_note, [{"header": n["note"], "value": "[text]"}], p.y,
                      visible=exists(is_note)), 10)


# ── shared pieces of the models' own blocks ─────────────────────────────────
def heading(p: stats.Page, id_: str, title: str) -> None:
    p.heading(id_, title)


def text_rows(p: stats.Page, id_: str, title: str, filter_: str, header: str) -> None:
    """A table of the texts of the rows `filter_` keeps (the sentences of a choice)."""
    p.add(stats.table(id_, title, filter_, [{"header": header, "value": "[text]"}], p.y, visible=exists(filter_)), 10)


def accent() -> str:
    return ACCENT
