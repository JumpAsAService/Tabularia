"""The report of the node «Volatility (GARCH)» (docs/specs/garch-risk.md, part 1, «The report (pixel perfect)»): the
conditional volatility of a series of returns, its persistence and forecast, the asymmetry of falls and rises, the checks of
the standardized residuals and of the parameters' stability, and the backtest of the 1-step value at risk on the holdout.

1. Summary: four cards (the returns and their span, the model and its distribution, the persistence and its half-life, the
   volatility of the next step and its long-run level), the formula line from the node's options, the checks (the two
   weighted portmanteau tests, ARCH LM, Nyblom's joint test, the sign bias; the backtest's three tests with a holdout),
   the measures under «Measures, not tests», the facts of the fit, the readings of the summary and of the persistence.
2. Volatility: the returns with the band μ ± z σ, σ with its forecast and its long-run level, the standardized residuals;
   the holdout's first instant marked.
3. Coefficients (in the series' units) and the news impact curve; the readings of the asymmetry and of the distribution.
4. Checks: the ACF of z and of z², the histogram of z, the Q–Q of z, Nyblom's table; the readings
   of the residuals and of the stability.
5. Backtest (with a holdout): the held-out observations, their 1-step VaR and the breaks, the counts, the reading.
6. How this report was computed (`common.how_computed`), the numbers of the fit, the notes.

The time's axis follows the runner's `time_kind`, as ARIMA's report does: each chart along time is drawn twice in the same
place, as dates and as steps, one visible. Every number is read from the runner's rows; the explanation is the runner's
(`reading` rows, «@id name=value», said by `common.with_labels`: the contract is docs/specs/garch-risk.md, «As built / G4»),
placed by `common.reading_under` and `common.how_computed`. The words are in `report_texts/garch.json`. The backtest's
pieces are the risk simulation's too (`layouts/risk_simulation.py`).
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import register
from app.engine.stats import layouts as stats
from app.reports import glyphs

WORDS = "garch"
SUB = "₀₁₂₃₄₅₆₇₈₉"
# the parameters a fit may have, in the coefficients' order: their symbols are the words' `values.reading_names`
PARAMETERS = ("mu", *(f"ar{i}" for i in range(1, 6)), *(f"ma{i}" for i in range(1, 6)), "omega", *(f"alpha{i}" for i in range(1, 4)),
              *(f"beta{i}" for i in range(1, 4)), *(f"gamma{i}" for i in range(1, 4)), "shape", "skew")
BACKTESTS = ("kupiec", "christoffersen_ind", "christoffersen_cc")
COUNTS = ("violations", "expected_violations", "violation_rate", "longest_run", "n00", "n01", "n10", "n11")
MODEL_MEASURES = ("persistence", "half_life", "uncond_sigma", "sigma_next", "sigma_last", "kappa")
FACTS = ("variance_stationary", "at_bound")
# the numbers the fit's table leaves out: the cards and the measures say them, with their words
FIT_SKIP = ("first", "last")
BAD = c.BAD
BAND = "#3b5bdb"


def _checked(params: dict[str, Any]) -> dict[str, Any]:
    return catalog.check_params(WORDS, dict(params))


def law(w: dict, checked: dict[str, Any]) -> str:
    """The distribution of z in the page's words: «skewed Student t»."""
    dist = str(checked.get("distribution") or "std")
    return w["values"]["distribution"].get(dist, dist)


def core(params: dict[str, Any]) -> str:
    """The model without its law: «GJR-GARCH(1,1)», «AR(1)-EGARCH(1,1)» (`catalog.garch_name` before its comma)."""
    return catalog.garch_name(params).rsplit(", ", 1)[0]


def name(params: dict[str, Any], locale: str) -> str:
    """«GJR-GARCH(1,1), skewed Student t»: the catalogue's name, its distribution in the page's words."""
    garch = {k: params[k] for k in catalog.MODELS["garch"]["takes"] if k in params}
    return f"{core(garch)}, {law(c.words(WORDS, 'default', locale), _checked(params))}"


def about(params: dict[str, Any], locale: str) -> str:
    """«prezzo» over «data», prices."""
    w = c.words(WORDS, "default", locale)
    value, time = params.get("value"), params.get("time")
    if not (value and time):
        return str(value or "")
    kind = w["kind"].get(str(_checked(params).get("input_kind") or "returns"), "")
    return w["series_of"].replace("{value}", str(value)).replace("{time}", str(time)).replace("{kind}", kind)


def numbers(sentence: str, lang: str, **values: float) -> str:
    """A static sentence with the node's numbers in it, each in the language's decimals («α = 0,05»)."""
    from app.reports import formats

    loc = formats.locale(lang)
    for k, v in values.items():
        sentence = sentence.replace("{" + k + "}", formats.format_number(float(v), "0.####", loc))
    return sentence


def integer_time(w: dict) -> str:
    """True when the runner says the time is an integer (its word in the page's language: `with_labels` says a setting's
    value in it)."""
    return f'COUNTIF([key] = "model|||time_kind" AND [text] = {c.q(w["values"]["time_kind"]["integer"])}) > 0'


def twice(p: stats.Page, item: dict, w: dict) -> None:
    """A chart along time, as dates and as integers, in the same place: one is visible; neither takes room without its rows."""
    shown = item["visible"].lstrip("=")
    integer = integer_time(w)
    for as_date in (True, False):
        p.add({**item, "id": item["id"] + ("" if as_date else "_steps"), "options": {**item["options"], "x_time": as_date},
               "visible": f"={shown} AND " + (f"NOT ({integer})" if as_date else integer), "collapse_hidden": True})


def line(level_2: str, name_: str, as_: str = "line", colour: str | None = None) -> dict:
    s: dict[str, Any] = {"value": f'IF([level_2] = "{level_2}", [value], NULL)', "as": as_, "name": name_}
    if colour:
        s["color"] = colour
    return s


def chart(id_: str, title: str, filter_: str, series: list[dict], y: float, tall: float, options: dict | None = None,
          category: str = "[x0]", x: float = 0.0, width: float = c.WIDTH) -> dict:
    return {"id": id_, "type": "chart", "dataset": c.DATASET, "kind": "xy", "x": x, "y": y, "w": width, "h": tall,
            "title": stats.fit(title, 7.5, width - 4, 700), "filter": filter_, "category": category, "series": series,
            "options": {"format": "0.####", "legend": True, **(options or {})}, "visible": c.exists(filter_)}


def heading(p: stats.Page, id_: str, title: str) -> None:
    """A heading and its rule that go to the next page with what follows them: never alone at the foot of a page."""
    p.heading(id_, title)
    for item in p.items[-2:]:
        item["keep_with_next"] = True


def caption(p: stats.Page, id_: str, text: str, visible: str | None = None) -> None:
    p.text(id_, stats.static(text), h=4.5, size=7.5, color=c.MUTED, can_grow=True, visible=visible)


def sigma_said(x: str, prices: bool) -> str:
    """A volatility as the page writes it: a share of the price in % («0.97 %») when the node computed the returns from
    prices, the series' own number otherwise (returns given in percent stay as they were given)."""
    return f'IF(ISNULL({x}), "–", {c.pct(x, "0.##") if prices else c.shown(x, c.NUMBER)})'


def symbol_of(column: str, w: dict) -> str:
    """The symbol of the parameter named by `column` («[level]», «[level_2]»): «ω», «α₁»; the name itself for another."""
    names = w["values"]["reading_names"]
    cases = ", ".join(f'{column} = "{k}", {c.q(names[k])}' for k in PARAMETERS if k in names)
    return f"IFS({cases}, TRUE, {column})"


# ── the formula line ────────────────────────────────────────────────────────
def _s(i: int) -> str:
    return "".join(SUB[int(d)] for d in str(i))


def mean_equation(checked: dict[str, Any]) -> str:
    """«r_t = μ + φ₁(r_(t−1) − μ) + θ₁ε_(t−1) + ε_t», from the node's orders."""
    mean = checked.get("mean", True) is not False
    m, n = int(checked.get("ar_order") or 0), int(checked.get("ma_order") or 0)
    terms = ["μ"] if mean else []
    terms += [f"φ{_s(i)}(r_(t−{i}) − μ)" if mean else f"φ{_s(i)}r_(t−{i})" for i in range(1, m + 1)]
    terms += [f"θ{_s(j)}ε_(t−{j})" for j in range(1, n + 1)]
    return "r_t = " + " + ".join([*terms, "ε_t"])


def variance_equation(checked: dict[str, Any]) -> str:
    """«σ²_t = ω + α₁ε²_(t−1) + β₁σ²_(t−1)», GJR's indicator and EGARCH's logarithm, from the node's orders."""
    kind = str(checked.get("variance_model") or "garch")
    q = int(checked.get("arch_order") or 1)
    p = 0 if kind == "arch" else int(checked.get("garch_order") or 0)
    if kind == "egarch":
        shocks = [f"α{_s(i)}z_(t−{i}) + γ{_s(i)}(|z_(t−{i})| − E|z|)" for i in range(1, q + 1)]
        return "ln σ²_t = ω + " + " + ".join([*shocks, *(f"β{_s(j)} ln σ²_(t−{j})" for j in range(1, p + 1))])
    if kind == "gjr":
        shocks = [f"(α{_s(i)} + γ{_s(i)}·I(ε_(t−{i}) < 0))·ε²_(t−{i})" for i in range(1, q + 1)]
    else:
        shocks = [f"α{_s(i)}ε²_(t−{i})" for i in range(1, q + 1)]
    return "σ²_t = ω + " + " + ".join([*shocks, *(f"β{_s(j)}σ²_(t−{j})" for j in range(1, p + 1))])


def formula(w: dict, checked: dict[str, Any]) -> str:
    """The model in symbols, its distribution in the page's words: the runner's `formula` is the English of MLflow."""
    return f"{mean_equation(checked)},  {variance_equation(checked)},  " + w["formula_law"].replace("{law}", law(w, checked))


# ── the cards ───────────────────────────────────────────────────────────────
def card(p: stats.Page, id_: str, x: float, label: str, value: str, unders: list[tuple[str, str | None]],
         fmt: str | None = None, widest: str | None = None) -> None:
    """A card; `unders`: the lines under its value, each with when it is shown; `widest`: a value the card must hold,
    the size of its digits brought down until it fits."""
    item: dict[str, Any] = {"id": id_, "type": "kpi", "dataset": c.DATASET, "x": x, "y": p.y, "w": 45, "h": 20, "label": label,
                            "value": value}
    if fmt:
        item["format"] = fmt
    if widest:
        size = next((pt for pt in (16, 14, 12, 11, 10, 9) if glyphs.width_mm(widest, pt, 700) <= 45 - 5.5), 9)
        if size != 16:
            item["style"] = {"size": size}
    p.add(item)
    for i, (said, when) in enumerate(unders):
        p.text(f"{id_}_under" + (f"_{i}" if i else ""), "=" + said, h=4.5, x=x + 2, w=43, advance=False, size=7.5,
               color=c.MUTED, visible=when)
        p.items[-1].update({"y": p.y + 14.6, "dataset": c.DATASET})


def span(lang: str, as_dates: bool) -> str:
    """The first and the last instant: «3 Jul 2023 – 15 Jul 2026», or «1 – 764» for a time counted in steps."""
    from app.reports import formats

    first, last = c.model_number("first"), c.model_number("last")
    if as_dates:
        day = lambda x: f'FORMAT(DATEADD("d", {x}, DATE(1970, 1, 1)), {c.q(formats.locale(lang).date)})'  # noqa: E731
        return f'{day(first)} & " – " & {day(last)}'
    return f'{c.shown(first, "#,##0")} & " – " & {c.shown(last, "#,##0")}'


def _cards(p: stats.Page, w: dict, lang: str, params: dict[str, Any], checked: dict[str, Any]) -> None:
    k = w["kpi"]
    integer = integer_time(w)
    card(p, "k_n", 0, k["n"], c.model_number("n"), [(span(lang, True), f"=NOT ({integer})"), (span(lang, False), "=" + integer)],
         "#,##0")
    # the law alone under the model: the holdout is the summary's reading's («the last 250 were kept out»)
    model = core(params)
    card(p, "k_model", 47, k["model"], c.q(model), [(c.q(law(w, checked)), None)], widest=model)
    persistence, half = c.model_number("persistence"), c.model_number("half_life")
    unit = w["values"]["time_unit"].get(str(checked.get("frequency")), w["values"]["time_unit"]["index"])
    half_said = c.join(k["half_life"], h=c.shown(half, "#,##0.#"), unit=c.q(unit))
    card(p, "k_persistence", 94, k["persistence"], f'IF(ISNULL({persistence}), "–", {c.shown(persistence, "0.###")})',
         [(f'IF(ISNULL({half}), {c.q(k["no_half_life"])}, {half_said})', None)])
    prices = checked.get("input_kind") == "prices"
    uncond = c.model_number("uncond_sigma")
    card(p, "k_sigma", 141, k["sigma"], sigma_said(c.model_number("sigma_next"), prices),
         [(f'IF(ISNULL({uncond}), {c.q(k["no_uncond"])}, {c.join(k["uncond"], u=sigma_said(uncond, prices))})', None)])
    p.y += 21.5


# ── the tables of the first page ────────────────────────────────────────────
def first_level_only(p: stats.Page) -> None:
    """The checks' and the advice's tables keep the tests of the whole model: Nyblom's tests of each parameter are the
    stability table's (page 4)."""
    for it in p.items:
        if it["id"] in ("assumptions", "advice"):
            it["filter"] = f'({it["filter"]}) AND ISNULL([level_2])'
            it["visible"] = c.exists(it["filter"])


def backtest_table(p: stats.Page, w: dict, lang: str, checked: dict[str, Any]) -> None:
    """The backtest's three tests with their H0 in the page's words (the VaR level's rate in it), their statistic, df,
    p-value, R's stars and verdict, then what to do about those that failed."""
    from app.reports import formats

    b, a = w["backtest"], w["assumptions"]
    rate = formats.format_number((1 - float(checked.get("var_level") or 0.99)) * 100, "0.##", formats.locale(lang)) + " %"
    one = '[section] = "backtest" AND [stat] = "verdict"'
    named = "IFS(" + ", ".join(f'[level] = "{t}", {c.q(b["names"][t])}' for t in BACKTESTS) + ", TRUE, [level])"
    h0 = "IFS(" + ", ".join(f'[level] = "{t}", {c.q(b["h0"][t].replace("{rate}", rate))}' for t in BACKTESTS) + ', TRUE, "")'
    said = "IFS(" + ", ".join(f'[text] = "{v}", {c.q(w["verdicts"][v])}' for v in catalog.VERDICTS) + ', TRUE, "")'
    df = c.of_row("backtest", "df")
    p.add(stats.table("backtest_tests", numbers(b["title"], lang, alpha=float(checked.get("alpha") or 0.05)), one, [
        {"header": b["test"], "value": named, "width": 40},
        {"header": a["h0"], "value": h0, "style": {"size": 7}},
        {"header": a["statistic"], "value": c.number_of_row("backtest", "statistic"), "width": 17, "align": "right", "format": "0.####"},
        {"header": a["df"], "value": f'IF(ISNULL({df}), "", FORMAT({df}, "0"))', "width": 10, "align": "right"},
        {"header": a["p_value"], "value": c.p_said(c.of_row("backtest", "p_value")), "width": 17, "align": "right"},
        {"header": w["signif"]["column"], "value": c.of_row("backtest", "significance", "text"), "width": 10},
        {"header": a["verdict"], "value": said, "width": 24,
         "rules": [{"when": f'[text] = "{v}"', "style": {"color": col, "weight": "semibold"}} for v, col in c.VERDICT_COLOURS.items()]},
    ], p.y, visible=c.exists(one)), 10)
    failed = '[section] = "backtest" AND [stat] = "verdict" AND [text] = "failed"'
    advice = "IFS(" + ", ".join(f'[level] = "{t}", {c.q(b["advice"][t])}' for t in BACKTESTS) + ', TRUE, "")'
    p.add(stats.table("backtest_advice", a["advice_title"], failed, [
        {"header": b["test"], "value": named, "width": 46},
        {"header": a["advice"], "value": advice},
    ], p.y, visible=c.exists(failed)), 10)


def counts_table(p: stats.Page, w: dict) -> None:
    """The breaks of the VaR on the holdout: their count, the count expected, the rate, the longest run, the transitions."""
    b = w["backtest"]
    one = '[section] = "backtest" AND [level] = "counts" AND (' + " OR ".join(f'[stat] = "{s}"' for s in COUNTS) + ")"
    value = ('IFS([stat] = "violation_rate", ' + c.pct("[value]", "0.##") + ', [stat] = "expected_violations", '
             'FORMAT([value], "#,##0.##"), TRUE, FORMAT([value], "#,##0"))')
    t = stats.table("backtest_counts", b["counts_title"], one, [
        {"header": b["quantity"], "value": "[label]"},
        {"header": b["value"], "value": f'IF(ISNULL([value]), "", {value})', "width": 30, "align": "right"},
    ], p.y, visible=c.exists(one))
    t["sort"] = [{"by": "IFS(" + ", ".join(f'[stat] = "{s}", {i}' for i, s in enumerate(COUNTS)) + ", TRUE, 99)"}]
    p.add(t, 10)


def _measures(p: stats.Page, w: dict, checked: dict[str, Any]) -> None:
    """The measures of the fit, each with what it is: no verdict, no colour."""
    names, what = w["measure_names"], w["measure_what"]
    prices = checked.get("input_kind") == "prices"
    unit = w["values"]["time_unit"].get(str(checked.get("frequency")), w["values"]["time_unit"]["index"])
    model = '[section] = "model" AND NOT ISNULL([value]) AND (' + " OR ".join(f'[stat] = "{s}"' for s in MODEL_MEASURES) + ")"
    coef = '[section] = "coef" AND [stat] = "estimate" AND ([level] = "shape" OR [level] = "skew")'
    one = f"({model}) OR ({coef})"
    key = 'IF([section] = "coef", [level], [stat])'
    label = "IFS(" + ", ".join(f'{key} = "{s}", {c.q(names[s])}' for s in (*MODEL_MEASURES, "shape", "skew")) + ", TRUE, [label])"
    sigma = " OR ".join(f'[stat] = "{s}"' for s in ("uncond_sigma", "sigma_next", "sigma_last"))
    value = (f'IFS([section] = "model" AND ({sigma}), {sigma_said("[value]", prices)}, [stat] = "half_life", '
             f'FORMAT([value], "#,##0.#") & " " & {c.q(unit)}, TRUE, FORMAT([value], "#,##0.####"))')
    said = "IFS(" + ", ".join(f'{key} = "{s}", {c.q(what[s])}' for s in (*MODEL_MEASURES, "shape", "skew")) + ', TRUE, "")'
    m = w["measures"]
    t = stats.table("measures", w["measures_title"], one, [
        {"header": m["measure"], "value": label, "width": 46},
        {"header": m["value"], "value": value, "width": 28, "align": "right"},
        {"header": m["what"], "value": said, "style": {"size": 7.5}},
    ], p.y, visible=c.exists(one))
    order = (*MODEL_MEASURES, "shape", "skew")
    t["sort"] = [{"by": "IFS(" + ", ".join(f'{key} = "{s}", {i}' for i, s in enumerate(order)) + ", TRUE, 99)"}]
    p.add(t, 10)
    c.measures(FACTS, counted=("at_bound",))(p, w)
    facts = p.items[-1]
    # the facts of the whole fit: the parameters at a bound are named by the runner's notes, not as rows of their own
    facts["filter"] = f'({facts["filter"]}) AND ISNULL([level_2])'
    facts.update({"id": "facts", "title": w["facts_title"], "visible": c.exists(facts["filter"])})


def _summary(p: stats.Page, w: dict, lang: str, params: dict[str, Any], checked: dict[str, Any]) -> None:
    p.heading("s_summary", w["sections"]["summary"])
    _cards(p, w, lang, params, checked)
    p.keep()
    p.text("formula", stats.static(formula(w, checked)), h=6.5, size=9.5, weight="semibold", background=c.PANEL, padding=1.5,
           can_grow=True)
    c.reading_under(p, w, "summary")
    c._assumptions(p, w, lang, numbers(w["alpha_line"], lang, alpha=float(checked.get("alpha") or 0.05)), True)
    first_level_only(p)
    if (checked.get("holdout") or 0) > 0:
        backtest_table(p, w, lang, checked)
    _measures(p, w, checked)
    c.reading_under(p, w, "persistence")


# ── page 2: the volatility ──────────────────────────────────────────────────
def holdout_start() -> str:
    """The first instant kept out: where the dashed vertical line goes (none without a holdout)."""
    return 'MIN(IF([section] = "curve" AND [level] = "backtest" AND [level_2] = "return", [x0], NULL))'


def _volatility(p: stats.Page, w: dict, checked: dict[str, Any]) -> None:
    s, ch = w["series"], w["charts"]
    heading(p, "s_volatility", w["sections"]["volatility"])
    level = numbers("{x}", "en", x=(1 - float(checked.get("alpha") or 0.05)) * 100)
    returns = '[section] = "curve" AND [level] = "returns"'
    twice(p, chart("returns", ch["returns"], returns, [
        line("return", s["return"], "point", c.MUTED), line("band_low", s["band"].replace("{level}", level), "band_low", BAND),
        line("band_high", "", "band_high", BAND)], p.y, 70, {"target_x": holdout_start()}), w)
    p.y += 71.5
    sigma = '[section] = "curve" AND [level] = "sigma" AND ([level_2] = "sigma" OR [level_2] = "forecast")'
    twice(p, chart("sigma", ch["sigma"], sigma, [line("sigma", s["sigma"], colour=c.ACCENT), line("forecast", s["forecast"], colour=c.MID)],
                   p.y, 60, {"target": c.model_number("uncond_sigma"), "target_x": holdout_start()}), w)
    p.y += 61.5
    z = '[section] = "point" AND [level] = "std_residual"'
    twice(p, chart("std_residual", ch["std_residual"], z, [{"value": "[value]", "as": "point", "name": s["z"]}], p.y, 40,
                   {"target": 0, "legend": False, "target_x": holdout_start()}), w)
    p.y += 41.5
    caption(p, "volatility_how", w["captions"]["volatility"])


# ── page 3: the coefficients and the news impact ────────────────────────────
def coefficients(p: stats.Page, w: dict, lang: str, checked: dict[str, Any], title: str) -> None:
    """The coefficients in the series' units, each named by its symbol («ω», «α₁»), with their intervals at 1 − α; the
    note says whether the standard errors are robust or classical."""
    co = w["coef"]
    se = co["classical"] if checked.get("se_type") == "classical" else co["robust"]
    level = numbers("{x}", lang, x=(1 - float(checked.get("alpha") or 0.05)) * 100)
    tables = (c.CoefTable("coef", title, ("estimate", "std_error", "statistic", "p_value")),
              c.CoefTable("coef_ci", w["coef"]["intervals"].replace("{level}", level), ("ci_low", "ci_high"), width=34))
    c._coefficients(p, w, lang, tables, se)
    for it in p.items:
        if it["id"] in ("coef", "coef_ci"):
            it["columns"][0]["value"] = symbol_of("[level]", w)


def _news(p: stats.Page, w: dict) -> None:
    f = '[section] = "curve" AND [level] = "news_impact"'
    p.y += 1.5
    p.add(chart("news_impact", w["charts"]["news_impact"], f, [line("sigma2", w["series"]["news"], colour=c.ACCENT)], p.y, 60,
                {"legend": False, "format": "0.######"}))
    p.y += 61.5
    caption(p, "news_how", w["captions"]["news"], visible=c.exists(f))
    c.reading_under(p, w, "asymmetry", "distribution")


# ── page 4: the checks ──────────────────────────────────────────────────────
def _checks(p: stats.Page, w: dict) -> None:
    ch, s = w["charts"], w["series"]

    def acf(id_: str, under: str) -> c.Chart:
        f = f'[section] = "lag" AND [level] = "{id_}"'
        return c.xy(id_, ch[id_], f + ' AND [stat] = "acf"', [c.mark("acf", "stem", s["acf"])],
                    {"target": c.num(f"lag|{id_}||bound"), "mirror": True, "legend": False, "format": "0.##"}, under=under)

    # the histogram of z: its counts only. The runner writes the fitted density as a density (`curve|density`), which an xy
    # chart cannot draw on its own axis beside counts, and the page computes nothing (docs/specs/garch-risk.md, «As built / G4»)
    histogram = c.histogram("hist", ch["hist"], "std_residual", s["count"], under="arch_lm")
    qq = '[section] = "point" AND [level] = "qq"'
    quantiles = c.Chart("qq", "xy", ch["qq"], {"filter": qq, "category": "[x0]",
                                               "series": [{"value": "[value]", "as": "point", "name": s["qq"]}],
                                               "options": {"format": "0.##", "legend": False, "diagonal": True}, "visible": c.exists(qq)})
    c.grid(p, w, (acf("z_acf", "ljung_box_z"), acf("z2_acf", "ljung_box_z2"), histogram, quantiles),
           ("s_checks", w["sections"]["checks"]), tall=56.0)
    _nyblom(p, w)
    c.reading_under(p, w, "residuals", "stability")


def _nyblom(p: stats.Page, w: dict) -> None:
    """Nyblom's joint test, then one row a parameter with its Holm-adjusted p-value."""
    n = w["nyblom"]
    one = '[section] = "assumption" AND [level] = "nyblom" AND [stat] = "verdict"'
    said = "IFS(" + ", ".join(f'[text] = "{v}", {c.q(w["verdicts"][v])}' for v in catalog.VERDICTS) + ', TRUE, "")'
    who = f'IF(ISNULL([level_2]) OR [level_2] = "", {c.q(n["joint"])}, {symbol_of("[level_2]", w)})'
    t = stats.table("nyblom", n["title"], one, [
        {"header": n["parameter"], "value": who, "style": {"weight": "semibold"}},
        {"header": n["statistic"], "value": c.number_of_row("assumption", "statistic"), "width": 24, "align": "right", "format": "0.####"},
        {"header": n["p_value"], "value": c.p_said(c.of_row("assumption", "p_value")), "width": 24, "align": "right"},
        {"header": n["p_adjusted"], "value": c.p_said(c.of_row("assumption", "p_adjusted")), "width": 30, "align": "right"},
        {"header": n["verdict"], "value": said, "width": 26,
         "rules": [{"when": f'[text] = "{v}"', "style": {"color": col, "weight": "semibold"}} for v, col in c.VERDICT_COLOURS.items()]},
    ], p.y, visible=c.exists(one))
    t["sort"] = [{"by": 'IF(ISNULL([level_2]) OR [level_2] = "", 0, 1)'}, {"by": "[ord]"}]
    p.add(t, 10)


# ── page 5: the backtest ────────────────────────────────────────────────────
def backtest_page(p: stats.Page, w: dict, observed: str) -> None:
    """The held-out observations, their 1-step VaR and the breaks; the counts; the reading. `observed`: the name of the
    observations' series (a return, or an ARIMA's value)."""
    s = w["series"]
    heading(p, "s_backtest", w["sections"]["backtest"])
    f = '([section] = "curve" AND [level] = "backtest") OR ([section] = "point" AND [level] = "violation")'
    # the breaks: the GARCH node flags each held-out day (`point|violation`, 1 on a break, its index the return's), the risk
    # node writes the observation of each break (`curve|backtest|violation`)
    flagged = f'LOOKUP("curve|backtest|return|y|" & MID([key], {len("point|violation||y|") + 1}, 12), [key], [value])'
    breaks = {"value": f'IF([section] = "point", IF([value] = 1, {flagged}, NULL), IF([level_2] = "violation", [value], NULL))',
              "as": "point", "name": s["violation"], "color": BAD}
    twice(p, chart("backtest", w["charts"]["backtest"], f, [
        line("return", observed, "point", c.MUTED), line("var", s["var"], colour=c.ACCENT), breaks], p.y, 90), w)
    p.y += 91.5
    caption(p, "backtest_how", w["captions"]["backtest"], visible=c.exists(f))
    counts_table(p, w)
    c.reading_under(p, w, "backtest")


# ── the page ────────────────────────────────────────────────────────────────
def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    from app.reports import formats

    lang = c.code(locale)
    checked = _checked(params)
    w = c.words(model, catalog.variant(model, checked), lang)
    header, footer = stats.bands(name(params, lang), " · ".join(x for x in (about(params, lang), step) if x), w,
                                 formats.locale(lang).texts, stats.sample_line(sample, lang))
    p = stats.Page()
    _summary(p, w, lang, params, checked)
    _volatility(p, w, checked)
    coefficients(p, w, lang, checked, w["coef"]["title"])
    _news(p, w)
    _checks(p, w)
    if (checked.get("holdout") or 0) > 0:
        backtest_page(p, w, w["series"]["return"])
    c.how_computed(p, w)
    c._fit(p, w, False, FIT_SKIP)
    c._notes(p, w)
    return c.document(header, p.items, footer, lang)


# ── the explanation (docs/specs/garch-risk.md, «As built / G4») ─────────────
# The lines are chosen from the rows the runner wrote (lot G1 writes no reading), as ARIMA's are: a verdict only where a row
# has a p-value, read against the node's alpha (a test the runner could not compute has none, and no line); every other
# number is a measure, said as the row holds it. The risk simulation's model and backtest lines are these functions'.
def number(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


class Rows:
    """The rows by their key, a number or a text of one."""

    def __init__(self, rows: list[dict]):
        self.keyed = {r["key"]: r for r in rows if r.get("key")}
        self.rows = rows

    def num(self, key: str) -> float | None:
        return number(self.keyed.get(key, {}).get("value"))

    def text(self, key: str) -> str | None:
        return self.keyed.get(key, {}).get("text")

    def p(self, check: str, level_2: str = "") -> float | None:
        return self.num(f"assumption|{check}|{level_2}|p_value")


def check_lines(rows: Rows, alpha: float, residuals: str = "residuals", asymmetry: str = "asymmetry",
                stability: str = "stability") -> list[c.ReadingLine]:
    """The checks of a fitted GARCH: the weighted Ljung–Box on z and z², ARCH LM, the size of the largest autocorrelation
    of z² when a test on z² rejects, Nyblom's joint test and the parameter that moves most, the sign bias."""
    out: list[c.ReadingLine] = []
    rejects_z2 = False
    for check, id_ in (("ljung_box_z", "lb_z"), ("ljung_box_z2", "lb_z2"), ("arch_lm", "arch_lm")):
        p = rows.p(check)
        if p is None:
            continue
        lag = rows.num(f"assumption|{check}||lag")
        out.append((residuals, None, f"{id_}_failed" if p < alpha else f"{id_}_ok", {"lag": lag, "p_value": p, "alpha": alpha}))
        rejects_z2 |= check != "ljung_box_z" and p < alpha
    if rejects_z2:  # the size behind the verdict: the largest autocorrelation of z², by its absolute value
        acf = [(abs(v), v, r.get("x0")) for r in rows.rows if r["section"] == "lag" and r["level"] == "z2_acf" and r["stat"] == "acf"
               and (v := number(r.get("value"))) is not None]
        if acf:
            _, v, lag = max(acf, key=lambda a: a[0])
            out.append((residuals, None, "z2_largest_acf", {"acf": v, "lag": lag}))
    p = rows.p("nyblom")
    if p is not None:
        if p >= alpha:
            out.append((stability, None, "nyblom_stable", {"p_value": p, "alpha": alpha}))
        else:
            adjusted = [(a, r["level_2"]) for r in rows.rows if r["section"] == "assumption" and r["level"] == "nyblom"
                        and r.get("level_2") and r["stat"] == "p_adjusted" and (a := number(r.get("value"))) is not None]
            low = min(adjusted, default=None)
            if low is not None and low[0] < alpha:
                out.append((stability, None, "nyblom_moves", {"p_value": p, "alpha": alpha, "parameter": low[1], "p_adjusted": low[0]}))
            else:
                out.append((stability, None, "nyblom_moves_none", {"p_value": p, "alpha": alpha}))
    p = rows.p("sign_bias")
    if p is not None:
        out.append((asymmetry, None, "sign_bias_supported" if p < alpha else "sign_bias_not_supported", {"p_value": p, "alpha": alpha}))
    return out


def backtest_lines(rows: Rows, checked: Mapping[str, Any], alpha: float) -> list[c.ReadingLine]:
    """The backtest of the 1-step VaR on the holdout: each test against alpha, the breaks expected beside each verdict."""
    holdout = rows.num(c.model_key("holdout")) or 0
    if holdout <= 0:
        return []
    if holdout < 2:
        return [("backtest", None, "backtest_short", {"holdout": holdout})]
    expected = rows.num("backtest|counts||expected_violations")
    violations = rows.num("backtest|counts||violations")
    out: list[c.ReadingLine] = []
    p = rows.num("backtest|kupiec||p_value")
    if p is not None:
        out.append(("backtest", None, "backtest_coverage_failed" if p < alpha else "backtest_coverage_ok",
                    {"holdout": holdout, "unit": checked.get("frequency") or "index", "level": checked.get("var_level"),
                     "violations": violations, "expected": expected, "p_value": p, "alpha": alpha}))
    p = rows.num("backtest|christoffersen_ind||p_value")
    if p is not None:
        out.append(("backtest", None, "backtest_independence_failed" if p < alpha else "backtest_independence_ok",
                    {"p_value": p, "alpha": alpha, "expected": expected}))
    elif violations == 0:
        out.append(("backtest", None, "backtest_independence_not_computed", {"expected": expected}))
    p = rows.num("backtest|christoffersen_cc||p_value")
    if p is not None:
        out.append(("backtest", None, "backtest_cc_failed" if p < alpha else "backtest_cc_ok", {"p_value": p, "alpha": alpha, "expected": expected}))
    if expected is not None:
        out.append(("backtest", None, "backtest_expected", {"expected": expected}))
    return out


def readings(rows: list[dict], checked: Mapping[str, Any]) -> list[c.ReadingLine]:
    alpha = float(checked.get("alpha") or 0.05)
    r = Rows(rows)
    num = lambda stat: r.num(c.model_key(stat))  # noqa: E731
    out: list[c.ReadingLine] = []
    # summary: the model, its data, the holdout, the returns from prices, the scale
    steps = r.text(c.model_key("time_kind")) == "integer"
    holdout = int(num("holdout") or 0)
    said = {"model": core(dict(checked)), "distribution": checked.get("distribution") or "std", "n": num("n"),
            "series": checked.get("value"), "first": num("first"), "last": num("last")}
    if holdout > 0:
        said["holdout"] = holdout
    out.append(("summary", None, "summary" + ("_holdout" if holdout > 0 else "") + ("_steps" if steps else ""), said))
    if checked.get("input_kind") == "prices":
        out.append(("summary", None, "summary_prices", {}))
    if num("scale") is not None:
        out.append(("summary", None, "summary_scale", {"scale": num("scale")}))
    # persistence: a measure, and the volatility now against its long-run level
    persistence, half, uncond, sigma = num("persistence"), num("half_life"), num("uncond_sigma"), num("sigma_next")
    if persistence is not None and persistence >= 1:
        out.append(("persistence", None, "persistence_integrated", {"persistence": persistence}))
    elif persistence is not None and half is not None:
        out.append(("persistence", None, "persistence", {"persistence": persistence, "half_life": half,
                                                         "unit": checked.get("frequency") or "index"}))
    if uncond is not None and sigma is not None:
        side = "above" if sigma > uncond else "below" if sigma < uncond else "equal"
        out.append(("persistence", None, f"sigma_now_{side}", {"uncond": uncond} if side == "equal" else {"sigma": sigma, "uncond": uncond}))
    # asymmetry: the model's own terms, then the sign bias test
    kind = checked.get("variance_model") or "garch"
    alpha1, gamma1 = r.num("coef|alpha1||estimate"), r.num("coef|gamma1||estimate")
    if kind == "gjr" and gamma1 is not None:
        if "assumption|at_bound|alpha1|value" in r.keyed:  # α₁ at its bound, 0: only falls move the variance
            out.append(("asymmetry", None, "asymmetry_gjr_falls_only", {"gamma": gamma1}))
        else:
            out.append(("asymmetry", None, "asymmetry_gjr", {"alpha": alpha1, "gamma": gamma1}))
    elif kind == "egarch" and alpha1 is not None and gamma1 is not None:
        out.append(("asymmetry", None, "asymmetry_egarch", {"alpha": alpha1, "gamma": gamma1}))
    elif kind in ("arch", "garch"):
        out.append(("asymmetry", None, "asymmetry_none", {}))
    lines = check_lines(r, alpha)
    out += [x for x in lines if x[0] == "asymmetry"]
    # the distribution of z: ν and ξ with their intervals at 1 − alpha
    dist = checked.get("distribution") or "std"
    shape = {"shape": r.num("coef|shape||estimate"), "low": r.num("coef|shape||ci_low"), "high": r.num("coef|shape||ci_high"),
             "level": 1 - alpha}
    if dist == "norm":
        out.append(("distribution", None, "distribution_norm", {}))
    elif dist == "std" and shape["shape"] is not None:
        out.append(("distribution", None, "distribution_std", shape))
    elif dist == "sstd" and shape["shape"] is not None:
        out.append(("distribution", None, "distribution_sstd", {**shape, "skew": r.num("coef|skew||estimate"),
                                                                "skew_low": r.num("coef|skew||ci_low"),
                                                                "skew_high": r.num("coef|skew||ci_high")}))
    out += [x for x in lines if x[0] != "asymmetry"]
    out += backtest_lines(r, checked, alpha)
    # «How this report was computed»
    method = ["method_garch", "method_scale", "method_estimation", "method_distribution", "method_checks", "method_nyblom",
              "method_sign_bias"]
    formulas = ["formula_mean", "formula_variance", "formula_distribution", "formula_likelihood", "formula_persistence",
                "formula_portmanteau", "formula_arch_lm", "formula_nyblom", "formula_sign_bias"]
    if holdout > 0:
        method.append("method_backtest")
        formulas += ["formula_var_1step", "formula_kupiec", "formula_christoffersen"]
    method.append("method_forecast")
    out += [("method", None, m, {}) for m in method]
    out += [("formulas", None, x, {}) for x in formulas]
    return out


c.READINGS["garch"] = readings

register("garch", layout, name, about)
