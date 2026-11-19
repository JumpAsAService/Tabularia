"""The report of a Kaplan–Meier estimate (docs/specs/statistical-models.md, `kaplan_meier`):
the survival curves by stratum with their bands and the censored times, the number at risk
under them, the strata's medians, survival at the times asked and restricted means, the
log-rank (or Peto) test and its pairs.

Cards: the strata, the events, the censored, the test's p-value. Diagnostics: the cumulative
hazard, the log-minus-log chart (proportional hazards: parallel lines).
"""
from __future__ import annotations

from typing import Any

from app.engine.models.layouts import common as c
from app.engine.models.layouts import register
from app.engine.stats import layouts as stats
from app.engine.stats.catalog import label_of


def name(params: dict[str, Any], locale: str) -> str:
    return c.words("kaplan_meier", "default", locale)["name"]


def about(params: dict[str, Any], locale: str) -> str:
    w = c.words("kaplan_meier", "default", locale)
    time, event, value = params.get("time"), params.get("event"), params.get("event_value")
    said = f"{time}, {event} = {label_of(value)}" if time and event and value is not None else str(time or "")
    return w["by"].replace("{what}", said).replace("{strata}", str(params["strata"])) if params.get("strata") else said


def _curves(p: stats.Page, w: dict) -> None:
    """The survival curves, a step each with its band and the censored times marked, across
    the page; the number at risk under them."""
    ch, s, t = w["charts"], w["series"], w["tables"]
    c.heading(p, "s_curves", w["sections"]["curves"])
    f = '([section] = "curve" AND [level] = "km") OR ([section] = "point" AND [level] = "censored")'
    p.add({"id": "km", "type": "chart", "dataset": c.DATASET, "kind": "xy", "x": 0, "y": p.y, "w": c.WIDTH, "h": 80,
           "title": stats.fit(ch["km"], 7.5, c.WIDTH - 4, 700), "filter": f, "category": "[x0]", "split_by": "[level_2]",
           "series": [{"value": 'IF([section] = "curve" AND [stat] = "surv", [value], NULL)', "as": "step", "name": s["survival"]},
                      {"value": 'IF([section] = "curve" AND [stat] = "lower", [value], NULL)', "as": "band_low"},
                      {"value": 'IF([section] = "curve" AND [stat] = "upper", [value], NULL)', "as": "band_high"},
                      {"value": 'IF([section] = "point", [value], NULL)', "as": "point", "name": s["censored"]}],
           "options": {"format": "0.##", "min": 0}, "visible": c.exists(f)}, 81)
    p.text("km_caption", w["km_caption"], h=4.5, size=7, color=c.MUTED)
    p.keep()
    at_risk = '[section] = "point" AND [level] = "at_risk"'
    p.add({"id": "at_risk", "type": "table", "dataset": c.DATASET, "x": 0, "y": p.y, "w": c.WIDTH, "h": 8,
           "title": t["at_risk"], "filter": at_risk, "groups": [{"by": "[level_2]"}],
           "columns": [{"header": t["stratum_time"], "width": 34}],
           "matrix": {"columns_by": "[x0]", "value": "SUM([value])", "total": False, "format": "#,##0"},
           "style": {"size": 7.5}, "visible": c.exists(at_risk)}, 10)


def _strata(p: stats.Page, w: dict) -> None:
    """Each stratum: its rows, events, median and restricted mean; observed against expected;
    survival at the times asked; the pairs."""
    t = w["tables"]
    c.heading(p, "s_strata", w["sections"]["strata"])
    one = '[section] = "stratum" AND [stat] = "n"'
    stratum = {"header": t["stratum"], "value": "[level]"}

    def numbers(*names: str, width: float = 22.0, counts: tuple[str, ...] = ()) -> list[dict]:
        return [{"header": c.stat_label(w, "stratum", n), "width": width, "align": "right",
                 **({"value": c.of_row("stratum", n), "format": "#,##0"} if n in counts else
                    {"value": c.number_of_row("stratum", n), "format": c.NUMBER})} for n in names]

    p.add(stats.table("strata", t["strata"], one, [stratum, *numbers("n", "events", "censored", counts=("n", "events", "censored")),
                                                 *numbers("median", "median_ci_low", "median_ci_high", width=24)], p.y,
                      visible=c.exists(one)), 10)
    p.add(stats.table("strata_rmst", t["rmst"], one, [stratum, *numbers("rmst", "rmst_se", "observed", "expected", width=28)],
                      p.y, visible=c.exists(one)), 10)
    at = ('[section] = "stratum" AND STARTSWITH([stat], "surv_at_") AND RIGHT([stat], 4) <> "_low" '
          'AND RIGHT([stat], 5) <> "_high"')
    p.add(stats.table("surv_at", t["surv_at"], at, [
        stratum, {"header": t["time"], "value": 'REPLACE([stat], "surv_at_", "")', "width": 30, "align": "right"},
        {"header": t["survival"], "value": "[value]", "width": 26, "align": "right", "format": "0.0000"},
        {"header": t["low"], "value": 'LOOKUP([key] & "_low", [key], [value])', "width": 26, "align": "right", "format": "0.0000"},
        {"header": t["high"], "value": 'LOOKUP([key] & "_high", [key], [value])', "width": 26, "align": "right", "format": "0.0000"},
    ], p.y, visible=c.exists(at)), 10)
    pair = '[section] = "pair" AND [stat] = "p_value"'
    p.add(stats.table("pairs", t["pairs"], pair, [
        {"header": t["pair"], "value": '[level] & " – " & [level_2]'},
        {"header": c.stat_label(w, "pair", "statistic"), "value": c.number_of_row("pair", "statistic"), "width": 26,
         "align": "right", "format": c.NUMBER},
        {"header": c.stat_label(w, "pair", "p_value"), "value": c.p_said(c.of_row("pair", "p_value")), "width": 24, "align": "right"},
        {"header": c.stat_label(w, "pair", "p_adjusted"), "value": c.p_said(c.of_row("pair", "p_adjusted")), "width": 28,
         "align": "right"},
        {"header": w["signif"]["column"], "value": c.of_row("pair", "significance", "text"), "width": 14},
    ], p.y, visible=c.exists(pair)), 10)


def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    w = c.words(model, "default", locale)
    k, ch, s = w["kpi"], w["charts"], w["series"]
    test = "peto" if params.get("test") == "peto" else "logrank"
    n = c.shown(c.num(c.model_key("n")), "#,##0")
    cards = (c.Card("k_strata", k["n_strata"], c.num(c.model_key("n_strata")), "#,##0"),
             c.Card("k_events", k["events"], c.num(c.model_key("events")), "#,##0", under=c.join(k["of_n"], n=n)),
             c.Card("k_censored", k["censored"], c.num(c.model_key("censored")), "#,##0"),
             c.Card("k_p", k[test], f'IF(ISNULL({c.num(c.model_key("test_p_value"))}), "—", {c.p_with_stars("test_p_value")})'))
    by_stratum = '[section] = "curve" AND [level] = "{}"'
    charts = (c.xy("cumhaz", ch["cumhaz"], by_stratum.format("cumhaz"), [c.mark("cumhaz", "step")], split_by="[level_2]"),
              c.xy("loglog", ch["loglog"], by_stratum.format("loglog"), [c.mark("y", "line")], split_by="[level_2]",
                   under="proportional_hazards"))
    return c.page(model, params, locale, sample, step, name=name(params, locale), about=about(params, locale), cards=cards,
                  blocks=(_curves, _strata), charts=charts)


register("kaplan_meier", layout, name, about)
