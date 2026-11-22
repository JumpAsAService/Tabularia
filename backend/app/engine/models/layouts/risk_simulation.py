"""The report of the node «Risk simulation» (docs/specs/garch-risk.md, part 2, «The report (pixel perfect)»): N simulated
futures of a fitted GARCH or ARIMA, the risk measures read from them with their Monte Carlo intervals, and the backtest of
the model's 1-step value at risk on the holdout.

1. Summary: four cards (the simulation, the model, the VaR and the ES at their levels), the formula line, the checks of the
   model fitted (and the backtest's three tests with a holdout), the measures under «Measures, not tests» with their Monte
   Carlo intervals, the readings of the summary, of the VaR and of the ES.
2. The fan: the last observations, the bands of the paths nested from the widest (lighter outward), the median, the
   sample paths as thin grey lines, the threshold, the last observation marked; the readings of the fan and the threshold.
3. The outcome at the horizon: the histogram of the risk variable with the VaR and the ES (and the threshold on that
   scale), the histogram of the drawdown with its VaR; the readings of the shape and the drawdown.
4. Through the horizon: the VaR and the ES at each step with their intervals; with a threshold, its two probabilities.
5. Backtest (with a holdout): the page of the GARCH report (`garch.backtest_page`).
6. The model simulated: its coefficients, and the line that sends to the full report of the GARCH or ARIMA node.
7. How this report was computed (`common.how_computed`), the numbers of the simulation, the notes.

A measure of a relative change (a GARCH's cumulative return, an ARIMA's change relative to the last value) is written as a
percentage; the value or the running total of an ARIMA in the series' units. The explanation is the runner's (`reading`
rows; the contract is docs/specs/garch-risk.md, «As built / G4»). The words are in `report_texts/risk_simulation.json`.
"""
from __future__ import annotations

from typing import Any

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import garch as g
from app.engine.models.layouts import register
from app.engine.stats import layouts as stats

WORDS = "risk_simulation"
MEASURES = ("var", "es", "prob_beyond", "prob_crossed", "drawdown", "mean", "sd", "skewness", "kurtosis")
FAN = '[section] = "curve" AND [level] = "fan" AND [level_2] <> "band_error"'
BAND = "#3b5bdb"
HISTORY = "#495057"
PATH = "#adb5bd"
FIT_SKIP = ("first", "last")


def _checked(params: dict[str, Any]) -> dict[str, Any]:
    return catalog.check_params(WORDS, dict(params))


def model_name(params: dict[str, Any], w: dict) -> str:
    """«GJR-GARCH(1,1)», «SARIMA(1,0,1)(0,1,1)[7]», «ARIMA (automatic orders)» in the page's words."""
    checked = _checked(params)
    if checked.get("model") == "arima":
        return w["auto_name"] if checked.get("orders") == "auto" else catalog.arima_name(params)
    return g.core({k: params[k] for k in catalog.MODELS["garch"]["takes"] if k in params})


def name(params: dict[str, Any], locale: str) -> str:
    """«Risk simulation · GJR-GARCH(1,1)»."""
    w = c.words(WORDS, catalog.variant(WORDS, _checked(params)), locale)
    return f"{w['name']} · {model_name(params, w)}"


def about(params: dict[str, Any], locale: str) -> str:
    w = c.words(WORDS, catalog.variant(WORDS, _checked(params)), locale)
    value, time = params.get("value"), params.get("time")
    if not (value and time):
        return str(value or "")
    return w["series_of"].replace("{value}", str(value)).replace("{time}", str(time))


def relative(checked: dict[str, Any]) -> bool:
    """The risk variable is a relative change (a GARCH's cumulative return, an ARIMA's `relative_change`): its measures are
    percentages; else the series' own units."""
    return checked.get("model") == "garch" or checked.get("aggregate") == "relative_change"


def said(x: str, rel: bool) -> str:
    """A value of the risk variable as the page writes it, «–» for none."""
    return f'IF(ISNULL({x}), "–", {c.pct(x, "0.##") if rel else c.shown(x, "#,##0.##")})'


def unit_of(w: dict, checked: dict[str, Any]) -> str:
    return w["values"]["time_unit"].get(str(checked.get("frequency")), w["values"]["time_unit"]["index"])


def level_said(x: float, lang: str) -> str:
    return g.numbers("{x}", lang, x=x * 100)


# ── page 1 ──────────────────────────────────────────────────────────────────
def _cards(p: stats.Page, w: dict, lang: str, params: dict[str, Any], checked: dict[str, Any]) -> None:
    from app.reports import formats

    k = w["kpi"]
    n = c.model_number
    unit = unit_of(w, checked)
    value = c.join(k["paths"], paths=c.shown(n("paths"), "#,##0"), steps=c.shown(n("horizon"), "#,##0"))
    loc = formats.locale(lang)
    widest = k["paths"].replace("{paths}", formats.format_number(float(checked.get("paths") or 0), "#,##0", loc)).replace(
        "{steps}", formats.format_number(float(checked.get("horizon") or 0), "#,##0", loc))
    g.card(p, "k_simulation", 0, k["simulation"].replace("{unit}", unit), value,
           [(c.join(k["values"], n=c.shown(n("simulated_values"), "#,##0"), seed=c.shown(n("seed"), "0")), None)], widest=widest)
    model = model_name(params, w)
    under = c.q(g.law(w, checked)) if checked.get("model") == "garch" else c.q(w["values"]["aggregate"].get(
        str(checked.get("aggregate") or "last"), ""))
    g.card(p, "k_model", 47, k["model"], c.q(model), [(under, None)], widest=model)
    rel = relative(checked)
    for id_, x, key, level in (("k_var", 94, "var", checked.get("var_level")), ("k_es", 141, "es", checked.get("es_level"))):
        value = c.num(f"risk|{key}||value")
        low, high = c.num(f"risk|{key}||low"), c.num(f"risk|{key}||high")
        interval = f'IF(ISNULL({low}), "", {said(low, rel)} & " – " & {said(high, rel)})'
        g.card(p, id_, x, k[key].replace("{level}", level_said(float(level or 0), lang)), said(value, rel), [(interval, None)])
    p.y += 21.5


def _measures(p: stats.Page, w: dict, checked: dict[str, Any]) -> None:
    """The risk measures at the horizon, each with its Monte Carlo interval and what it is: no verdict, no colour."""
    m = w["measures"]
    rel = relative(checked)
    one = ('[section] = "risk" AND (ISNULL([level_2]) OR [level_2] = "") AND '
           '(([stat] = "value" AND [level] <> "moments") OR ([level] = "moments" AND ([stat] = "mean" OR [stat] = "sd" '
           'OR [stat] = "skewness" OR [stat] = "kurtosis")))')
    key = 'IF([level] = "moments", [stat], [level])'
    label = "IFS(" + ", ".join(f'{key} = "{s}", {c.q(w["risk_names"][s])}' for s in MEASURES) + ", TRUE, [level])"
    share = 'OR '.join(f'{key} = "{s}" ' for s in ("prob_beyond", "prob_crossed", "drawdown"))

    def shown(x: str) -> str:
        return (f'IF(ISNULL({x}), "", IFS({share}, {c.pct(x, "0.##")}, {key} = "skewness" OR {key} = "kurtosis", '
                f'FORMAT({x}, "0.###"), TRUE, {said(x, rel)}))')

    low = 'LOOKUP("risk|" & [level] & "||low", [key], [value])'
    high = 'LOOKUP("risk|" & [level] & "||high", [key], [value])'
    interval = f'IF([level] = "moments" OR ISNULL({low}), "", {shown(low)} & " – " & {shown(high)})'
    what = "IFS(" + ", ".join(f'{key} = "{s}", {c.q(w["risk_what"][s])}' for s in MEASURES) + ', TRUE, "")'
    t = stats.table("measures", m["title"], one, [
        {"header": m["measure"], "value": label, "width": 44},
        {"header": m["value"], "value": shown("[value]"), "width": 22, "align": "right"},
        {"header": m["interval"], "value": interval, "width": 34, "align": "right"},
        {"header": m["what"], "value": what, "style": {"size": 7.5}},
    ], p.y, visible=c.exists(one))
    t["sort"] = [{"by": "IFS(" + ", ".join(f'{key} = "{s}", {i}' for i, s in enumerate(MEASURES)) + ", TRUE, 99)"}]
    p.add(t, 10)


def _summary(p: stats.Page, w: dict, lang: str, params: dict[str, Any], checked: dict[str, Any]) -> None:
    p.heading("s_summary", w["sections"]["summary"])
    _cards(p, w, lang, params, checked)
    p.keep()
    if checked.get("model") == "garch":
        p.text("formula", stats.static(g.formula(w, checked)), h=6.5, size=9.5, weight="semibold", background=c.PANEL,
               padding=1.5, can_grow=True)
    else:
        p.text("formula", "=" + c.model_text("formula"), h=6.5, size=9.5, weight="semibold", background=c.PANEL, padding=1.5,
               can_grow=True, visible=c.exists('[key] = "model|||formula"'))
    c.reading_under(p, w, "summary")
    _measures(p, w, checked)
    c.reading_under(p, w, "var", "es")
    c._assumptions(p, w, lang, g.numbers(w["alpha_line"], lang, alpha=float(checked.get("alpha") or 0.05)), True)
    g.first_level_only(p)
    if (checked.get("holdout") or 0) > 0:
        g.backtest_table(p, w, lang, checked)


# ── page 2: the fan ─────────────────────────────────────────────────────────
def bands(checked: dict[str, Any]) -> tuple[list[tuple[str, str, float]], str | None]:
    """The fan's bands, nested from the widest: (low quantile's name, high quantile's name, the share of the paths between
    them), each quantile below 0.5 paired with its mirror above; and the median's name when 0.5 is asked."""
    levels = sorted({float(x) for x in checked.get("quantiles") or []})
    named = {x: catalog._number_label(x) for x in levels}
    low, high = [x for x in levels if x < 0.5], [x for x in levels if x > 0.5][::-1]
    pairs = [(f"quantile_{named[a]}", f"quantile_{named[b]}", b - a) for a, b in zip(low, high)]
    return pairs, (f"quantile_{named[0.5]}" if 0.5 in named else None)


def _fan(p: stats.Page, w: dict, lang: str, checked: dict[str, Any]) -> None:
    s = w["series"]
    g.heading(p, "s_fan", w["sections"]["fan"])
    pairs, median = bands(checked)
    series: list[dict] = []
    for lo, hi, share in pairs:
        series += [g.line(lo, s["band"].replace("{share}", level_said(share, lang)), "band_low", BAND), g.line(hi, "", "band_high", BAND)]
    if median:
        series.append(g.line(median, s["median"], colour=c.ACCENT))
    series.append(g.line("history", s["history"], colour=HISTORY))
    series.append({"value": 'IF(STARTSWITH([level_2], "path_"), [value], NULL)', "as": "line", "name": s["path"], "color": PATH})
    last = 'MAX(IF([section] = "curve" AND [level] = "fan" AND [level_2] = "history", [x0], NULL))'
    item = g.chart("fan", w["charts"]["fan"], FAN, series, p.y, 110,
                   {"legend": False, "target": c.model_number("threshold"), "target_x": last})
    item["split_by"] = 'IF(STARTSWITH([level_2], "path_"), [level_2], "fan")'
    g.twice(p, item, w)
    p.y += 111.5
    g.caption(p, "fan_how", w["captions"]["fan"], visible=c.exists(FAN))
    c.reading_under(p, w, "fan", "threshold")


# ── page 3: the outcome at the horizon ──────────────────────────────────────
def histogram(id_: str, title: str, level: str, name_: str, targets: list[str], tall: float, y: float) -> dict:
    f = f'[section] = "hist" AND [level] = "{level}"'
    item = g.chart(id_, title, f, [{"value": "[value]", "as": "stem", "name": name_}], y, tall,
                   {"legend": False, "format": "#,##0.####", "target_x": targets}, category="([x0] + [x1]) / 2")
    return item


def _outcome(p: stats.Page, w: dict, checked: dict[str, Any]) -> None:
    ch, s = w["charts"], w["series"]
    g.heading(p, "s_outcome", w["sections"]["outcome"])
    targets = [c.num("risk|var||value"), c.num("risk|es||value")]
    # the threshold is on the histogram's scale when the risk variable is what the fan shows (a GARCH's cumulative return of
    # returns given as returns, an ARIMA's value or running total)
    if (checked.get("model") == "garch" and checked.get("input_kind") != "prices") or (
            checked.get("model") == "arima" and checked.get("aggregate") in ("last", "sum")):
        targets.append(c.model_number("threshold"))
    p.add(histogram("horizon", ch["horizon"], "horizon", s["count"], targets, 80, p.y))
    p.y += 81.5
    g.caption(p, "outcome_how", w["captions"]["outcome"], visible=c.exists('[section] = "hist" AND [level] = "horizon"'))
    p.y += 1.5
    p.add(histogram("drawdown", ch["drawdown"], "drawdown", s["count"], [c.num("risk|drawdown||value")], 60, p.y))
    p.items[-1]["collapse_hidden"] = True
    p.y += 61.5
    c.reading_under(p, w, "shape", "drawdown")


# ── page 4: through the horizon ─────────────────────────────────────────────
def _term(p: stats.Page, w: dict) -> None:
    ch, s = w["charts"], w["series"]
    g.heading(p, "s_term", w["sections"]["term"])
    f = '[section] = "curve" AND [level] = "term" AND ([level_2] = "var" OR [level_2] = "es" OR [level_2] = "var_low" OR ' \
        '[level_2] = "var_high" OR [level_2] = "es_low" OR [level_2] = "es_high")'
    g.twice(p, g.chart("term", ch["term"], f, [
        g.line("var_low", s["var_band"], "band_low", c.ACCENT), g.line("var_high", "", "band_high", c.ACCENT),
        g.line("es_low", s["es_band"], "band_low", c.MID), g.line("es_high", "", "band_high", c.MID),
        g.line("var", s["var"], colour=c.ACCENT), g.line("es", s["es"], colour=c.MID)], p.y, 60), w)
    p.y += 61.5
    probabilities = '[section] = "curve" AND [level] = "term" AND ([level_2] = "prob_crossed" OR [level_2] = "prob_beyond")'
    g.twice(p, g.chart("probabilities", ch["probabilities"], probabilities, [
        g.line("prob_crossed", s["prob_crossed"], colour=c.ACCENT), g.line("prob_beyond", s["prob_beyond"], colour=c.MID)],
        p.y, 50, {"format": "0.###"}), w)
    p.y += 51.5


# ── page 6: the model ───────────────────────────────────────────────────────
def _model(p: stats.Page, w: dict, lang: str, checked: dict[str, Any]) -> None:
    table = c.CoefTable("coef", w["coef_title"], ("estimate", "std_error", "statistic", "p_value", "ci_low", "ci_high"), width=19)
    c._coefficients(p, {**w, "sections": {**w["sections"], "coefficients": w["sections"]["model"]}}, lang, (table,), None)
    next(it for it in p.items if it["id"] == "coef")["columns"][0]["value"] = g.symbol_of("[level]", w)
    p.text("full_report", stats.static(w["full_report"][str(checked.get("model") or "garch")]), h=5, size=8.5, color=c.MUTED,
           can_grow=True)
    c.reading_under(p, w, "model")


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
    _fan(p, w, lang, checked)
    _outcome(p, w, checked)
    _term(p, w)
    if (checked.get("holdout") or 0) > 0:
        g.backtest_page(p, w, w["series"]["observed"])
    _model(p, w, lang, checked)
    c.how_computed(p, w)
    c._fit(p, w, False, FIT_SKIP)
    c._notes(p, w)
    return c.document(header, p.items, footer, lang)


register("risk_simulation", layout, name, about)
