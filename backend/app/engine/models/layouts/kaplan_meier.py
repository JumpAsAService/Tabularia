"""The report of a Kaplan–Meier estimate (docs/specs/statistical-models.md, `kaplan_meier`):
the survival curves by stratum with their bands and the censored times, the number at risk
under them, each stratum's rows and events, its median and quartiles with their intervals,
survival at the times asked, the restricted mean, the log-rank (or Peto) test and its pairs.

Cards: the strata, the events, the median (one stratum) or the censored (several), the
test's p-value (several strata) or the restricted mean (one). Diagnostics: the cumulative
hazard, the log-minus-log chart (proportional hazards: parallel lines).

Without strata the runner names the one stratum «all»: the page says it in its language.
"""
from __future__ import annotations

from typing import Any, Mapping

from app.engine.models.layouts import common as c
from app.engine.models.layouts import register
from app.engine.models.layouts import shared_readings as sr
from app.engine.stats import layouts as stats
from app.engine.stats.catalog import label_of

ALL = "all"  # the runner's name of the one stratum without strata


def name(params: dict[str, Any], locale: str) -> str:
    return c.words("kaplan_meier", "default", locale)["name"]


def about(params: dict[str, Any], locale: str) -> str:
    w = c.words("kaplan_meier", "default", locale)
    time, event, value = params.get("time"), params.get("event"), params.get("event_value")
    said = f"{time}, {event} = {label_of(value)}" if time and event and value is not None else str(time or "")
    return w["by"].replace("{what}", said).replace("{strata}", str(params["strata"])) if params.get("strata") else said


def _stratum(w: dict, field: str = "[level]") -> str:
    """A stratum as the page names it: «line=A», or the words for all the rows."""
    return f'IF({field} = "{ALL}", {c.q(w["all"])}, {field})'


def _interval(low: str, high: str) -> str:
    """«52 – 86» from two numbers; an end that is missing (not reached) is a dash."""
    def end(x: str) -> str:
        return f'IF(ISNULL({x}), "—", FORMAT({x}, "0.###"))'

    return f'IF(ISNULL({low}) AND ISNULL({high}), "", {end(low)} & " – " & {end(high)})'


def _curves(p: stats.Page, w: dict) -> None:
    """The survival curves, a step each with its band and the censored times marked, across
    the page; the number at risk under them."""
    ch, s, t = w["charts"], w["series"], w["tables"]
    c.heading(p, "s_curves", w["sections"]["curves"])
    f = '([section] = "curve" AND [level] = "km") OR ([section] = "point" AND [level] = "censored")'
    p.add({"id": "km", "type": "chart", "dataset": c.DATASET, "kind": "xy", "x": 0, "y": p.y, "w": c.WIDTH, "h": 80,
           "title": stats.fit(ch["km"], 7.5, c.WIDTH - 4, 700), "filter": f, "category": "[x0]",
           "split_by": _stratum(w, "[level_2]"),
           "series": [{"value": 'IF([section] = "curve" AND [stat] = "surv", [value], NULL)', "as": "step", "name": s["survival"]},
                      {"value": 'IF([section] = "curve" AND [stat] = "lower", [value], NULL)', "as": "band_low"},
                      {"value": 'IF([section] = "curve" AND [stat] = "upper", [value], NULL)', "as": "band_high"},
                      {"value": 'IF([section] = "point", [value], NULL)', "as": "point", "name": s["censored"]}],
           "options": {"format": "0.##", "min": 0, "max": 1}, "visible": c.exists(f)}, 81)
    p.text("km_caption", w["km_caption"], h=4.5, size=7, color=c.MUTED)
    p.keep()
    at_risk = '[section] = "point" AND [level] = "at_risk"'
    p.add({"id": "at_risk", "type": "table", "dataset": c.DATASET, "x": 0, "y": p.y, "w": c.WIDTH, "h": 8,
           "title": t["at_risk"], "filter": at_risk, "groups": [{"by": _stratum(w, "[level_2]")}],
           "columns": [{"header": t["stratum_time"], "width": 34}],
           "matrix": {"columns_by": "[x0]", "value": "SUM([value])", "total": False, "format": "#,##0"},
           "style": {"size": 7.5}, "visible": c.exists(at_risk)}, 10)
    c.reading_under(p, w, "curves")


def _strata(p: stats.Page, w: dict) -> None:
    """Each stratum: its rows and events (observed and expected under the test's H0); its
    quartiles and median with their intervals; survival at the times asked; the restricted
    mean; then the pairs."""
    t = w["tables"]
    c.heading(p, "s_strata", w["sections"]["strata"])
    one = '[section] = "stratum" AND [stat] = "n"'
    stratum = {"header": t["stratum"], "value": _stratum(w)}

    def numbers(*names: str, width: float = 22.0, counts: tuple[str, ...] = ()) -> list[dict]:
        return [{"header": c.stat_label(w, "stratum", n), "width": width, "align": "right",
                 **({"value": c.of_row("stratum", n), "format": "#,##0"} if n in counts else
                    {"value": c.number_of_row("stratum", n), "format": c.NUMBER})} for n in names]

    p.add(stats.table("strata", t["strata"], one, [stratum, *numbers("n", "events", "censored", counts=("n", "events", "censored")),
                                                 *numbers("observed", "expected", width=30)], p.y,
                      visible=c.exists(one)), 10)

    def quartile(stat: str) -> list[dict]:
        return [{"header": c.stat_label(w, "stratum", stat), "value": c.number_of_row("stratum", stat), "width": 20,
                 "align": "right", "format": "0.###"},
                {"header": t["ci"], "value": _interval(c.of_row("stratum", stat + "_ci_low"), c.of_row("stratum", stat + "_ci_high")),
                 "width": 27, "align": "right"}]

    p.add(stats.table("quartiles", t["quartiles"], one, [stratum, *quartile("q25"), *quartile("median"), *quartile("q75")], p.y,
                      visible=c.exists(one)), 10)
    p.text("quartiles_caption", w["quartiles_caption"], h=4.5, size=7, color=c.MUTED, visible=c.exists(one))
    at = ('[section] = "stratum" AND STARTSWITH([stat], "surv_at_") AND RIGHT([stat], 4) <> "_low" '
          'AND RIGHT([stat], 5) <> "_high"')
    p.add(stats.table("surv_at", t["surv_at"], at, [
        stratum, {"header": t["time"], "value": "[x0]", "width": 30, "align": "right", "format": "0.######"},
        {"header": t["survival"], "value": "[value]", "width": 26, "align": "right", "format": "0.0000"},
        {"header": t["low"], "value": 'LOOKUP([key] & "_low", [key], [value])', "width": 26, "align": "right", "format": "0.0000"},
        {"header": t["high"], "value": 'LOOKUP([key] & "_high", [key], [value])', "width": 26, "align": "right", "format": "0.0000"},
    ], p.y, visible=c.exists(at)), 10)
    p.add(stats.table("strata_rmst", t["rmst"], one, [
        stratum, {"header": t["horizon"], "value": c.num(c.model_key("rmst_tau")), "width": 30, "align": "right", "format": "0.###"},
        *numbers("rmst", "rmst_se", width=30)], p.y, visible=c.exists(one)), 10)
    c.reading_under(p, w, "rmst")
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
    c.reading_under(p, w, "pairs")


def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    w = c.words(model, "default", locale)
    k, ch = w["kpi"], w["charts"]
    test = "peto" if params.get("test") == "peto" else "logrank"
    n = c.shown(c.num(c.model_key("n")), "#,##0")
    strata = c.num(c.model_key("n_strata"))
    several, alone = f"={strata} > 1", f"=NOT ({strata} > 1)"
    median = c.num(f"stratum|{ALL}||median")
    median_ci = _interval(c.num(f"stratum|{ALL}||median_ci_low"), c.num(f"stratum|{ALL}||median_ci_high"))
    rmst = c.num(f"stratum|{ALL}||rmst")
    cards = (c.Card("k_strata", k["n_strata"], strata, "#,##0"),
             c.Card("k_events", k["events"], c.num(c.model_key("events")), "#,##0", under=c.join(k["of_n"], n=n)),
             (c.Card("k_censored", k["censored"], c.num(c.model_key("censored")), "#,##0", visible=several),
              c.Card("k_median", k["median"], f'IF(ISNULL({median}), {c.q(k["not_reached"])}, {c.shown(median, "0.###")})',
                     visible=alone, under=c.join(k["ci"], ci=median_ci))),
             (c.Card("k_p", k[test], f'IF(ISNULL({c.num(c.model_key("test_p_value"))}), "—", {c.p_with_stars("test_p_value")})',
                     visible=several),
              c.Card("k_rmst", k["rmst"], c.shown(rmst, "0.###"), visible=alone,
                     under=c.join(k["up_to"], tau=c.shown(c.num(c.model_key("rmst_tau")), "0.###")))))
    by_stratum = '[section] = "curve" AND [level] = "{}"'
    charts = (c.xy("cumhaz", ch["cumhaz"], by_stratum.format("cumhaz"), [c.mark("cumhaz", "step")], split_by=_stratum(w, "[level_2]")),
              c.xy("loglog", ch["loglog"], by_stratum.format("loglog"), [c.mark("y", "line")], split_by=_stratum(w, "[level_2]"),
                   under="proportional_hazards"))
    return c.page(model, params, locale, sample, step, name=name(params, locale), about=about(params, locale), cards=cards,
                  blocks=(_curves, _strata), charts=charts, explained=("summary", "assumptions"))


def readings(rows: list[dict], checked: Mapping[str, Any]) -> list[c.ReadingLine]:
    """The explanation (docs/specs/report-explanations.md, «B2»): the rows, events and censored; each stratum's median
    (five named at most) and the test of the curves; the pairs by their adjusted p-values; the restricted means; the
    checks; the method's paragraphs and formulas."""
    k, a = sr.keyed(rows), sr.alpha(checked)
    strata = [r["level"] for r in rows if r["section"] == "stratum" and r["stat"] == "n"]
    several = len(strata) > 1
    counts = {"n": sr.model(k, "n"), "events": sr.model(k, "events"), "censored": sr.model(k, "censored")}
    lines: list[c.ReadingLine] = [("summary", None, "summary_km" if several else "summary_km_one", {**counts, "strata": len(strata)})]
    lines += sr.assumption_lines(rows, checked)

    def of(stratum: str, stat: str) -> float | None:
        return sr.number(k, f"stratum|{stratum}||{stat}")

    if several:
        reached = [s for s in strata if of(s, "median") is not None]
        if reached:
            shown, more = sr.listed("curves", reached)
            lines += [("curves", None, "km_medians", {"names": shown, "values": [of(s, "median") for s in shown]}), *more]
        never = [s for s in strata if of(s, "median") is None]
        if never:
            shown, more = sr.listed("curves", never)
            lines += [("curves", None, "km_not_reached", {"names": shown}), *more]
        p = sr.model(k, "test_p_value")
        if p is not None:
            lines.append(("curves", None, "km_test_reject" if p < a else "km_test_keep", {
                "statistic": sr.model(k, "test_statistic"), "df": sr.model(k, "test_df"), "p": p, "alpha": a,
                "count": len(strata)}))
    elif strata:
        median, low, high = (of(strata[0], s) for s in ("median", "median_ci_low", "median_ci_high"))
        if median is None:
            lines.append(("curves", None, "km_median_not_reached", {}))
        else:
            lines.append(("curves", None, "km_median_one" if low is not None and high is not None else "km_median_one_open",
                          {"median": median, "low": low, "high": high, "conf": sr.conf(checked)}))
    pairs = [r for r in rows if r["section"] == "pair" and r["stat"] == "p_adjusted" and sr.is_number(r.get("value"))]
    if pairs:
        apart = sorted((r for r in pairs if r["value"] < a), key=lambda r: r["value"])
        if apart:
            shown, more = sr.listed("pairs", [f"{r['level']} – {r['level_2']}" for r in apart])
            lines += [("pairs", None, "pairs_some", {"count": len(apart), "total": len(pairs), "names": shown, "alpha": a}), *more]
        else:
            lines.append(("pairs", None, "pairs_none", {"total": len(pairs), "alpha": a}))
    tau = sr.model(k, "rmst_tau")
    means = [s for s in strata if of(s, "rmst") is not None]
    if tau is not None and means:
        if several:
            shown, more = sr.listed("rmst", means)
            lines += [("rmst", None, "rmst", {"tau": tau, "names": shown, "values": [of(s, "rmst") for s in shown]}), *more]
        else:
            lines.append(("rmst", None, "rmst_one", {"tau": tau, "value": of(means[0], "rmst"), "se": of(means[0], "rmst_se")}))
    peto = checked.get("test") == "peto"
    lines += sr.method_lines("method_km", "method_censoring", ("method_peto" if peto else "method_logrank") if several else "",
                             "method_pairs" if pairs else "", "method_rmst", "method_p_value")
    lines += sr.formula_lines("formula_km", "formula_greenwood", "formula_logrank" if several else "", "formula_rmst")
    return lines


register("kaplan_meier", layout, name, about)
c.READINGS["kaplan_meier"] = readings
