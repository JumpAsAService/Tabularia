"""The report of an ARIMA model, from AR to SARIMAX (docs/specs/statistical-models.md,
`arima`): the orders given or chosen (`orders = auto`: the candidates and why), the
coefficients, the forecast with its two bands, the residuals' checks.

Cards: the observations, the AIC (AICc), σ², the MAPE on the holdout (in sample without
one). Blocks: ADF and KPSS read together (the four cases, this series' marked); the series
with its fitted values and forecast, and the residuals over time (ARCH). Diagnostics: the ACF
and PACF of the differenced series (stationarity), of the residuals (Ljung–Box), their
histogram and normal Q–Q (Jarque–Bera).

The time's axis follows the runner's `time_kind`: dates for a date or datetime column (x is
days since 1970-01-01), plain numbers for an integer one (a month counted 1, 2, 3… is not a
day of 1970). The definition is built before the rows are read, so each time chart is drawn
twice in the same place, one of the two visible.
"""
from __future__ import annotations

from typing import Any

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import register
from app.engine.stats import layouts as stats


def name(params: dict[str, Any], locale: str) -> str:
    w = c.words("arima", catalog.variant("arima", catalog.check_params("arima", params)), locale)
    return w["auto_name"] if params.get("orders") == "auto" else catalog.arima_name(params)


def about(params: dict[str, Any], locale: str) -> str:
    w = c.words("arima", "manual", locale)
    value, time = params.get("value"), params.get("time")
    said = w["series_of"].replace("{value}", str(value)).replace("{time}", str(time)) if value and time else str(value or "")
    exog = params.get("exog") or []
    return said + (" + " + ", ".join(str(e) for e in exog) if exog else "")


def _integer_time(w: dict) -> str:
    """True when the runner says the time is an integer (its word in the page's language:
    `with_labels` says a setting's value in it)."""
    word = w["values"]["time_kind"]["integer"]
    return f'COUNTIF([key] = "model|||time_kind" AND [text] = {c.q(word)}) > 0'


def _twice(p: stats.Page, item: dict, w: dict) -> None:
    """A chart along time, as dates and as integers, in the same place: one is visible."""
    shown = (item.get("visible") or c.exists("TRUE")).lstrip("=")
    integer = _integer_time(w)
    for as_date in (True, False):
        twin = {**item, "id": item["id"] + ("" if as_date else "_steps"),
                "options": {**item["options"], "x_time": as_date},
                "visible": f"={shown} AND " + (f"NOT ({integer})" if as_date else integer)}
        p.add(twin)


def _caption(p: stats.Page, w: dict, id_: str, check: str, x: float, y: float, width: float) -> None:
    """The verdict of the check a chart illustrates, under it (as the diagnostics write it)."""
    key = f"assumption|{check}||verdict"
    said = "IFS(" + ", ".join(f'{c.txt(key)} = "{v}", {c.q(w["verdicts"][v])}' for v in catalog.VERDICTS) + ', "")'
    p.add({"id": id_ + "_caption", "type": "text", "x": x, "y": y, "w": width, "h": 4.5,
           "value": "=" + c.join(w["illustrates"], check=f"LOOKUP({c.q(key)}, [key], [label])", verdict=said),
           "style": {"size": 7, "color": c.MUTED}, "visible": f"=COUNTIF([key] = {c.q(key)}) > 0"})


def _stationarity(params: dict[str, Any]):
    """ADF and KPSS read together: the four cases, this series' one marked."""
    alpha = float(params.get("alpha") or 0.05)

    def block(p: stats.Page, w: dict) -> None:
        cases = w["cases"]
        adf, kpss = c.num("assumption|adf||p_value"), c.num("assumption|kpss||p_value")
        both = '[key] = "assumption|adf||p_value" OR [key] = "assumption|kpss||p_value"'
        computed = f"COUNTIF({both}) = 2"
        c.heading(p, "s_cases", cases["title"])
        p.text("cases_alpha", "=" + c.join(cases["alpha"], alpha=c.shown(repr(alpha), "0.###")), h=5, size=8, color=c.MUTED,
               visible="=" + computed)
        p.keep()
        rule = {"1": (True, False), "2": (False, True), "3": (True, True), "4": (False, False)}
        for k, (adf_rejects, kpss_rejects) in rule.items():
            this = (f"({adf} {'<' if adf_rejects else '>='} {alpha!r}) AND ({kpss} {'<' if kpss_rejects else '>='} {alpha!r})")
            for marked in (True, False):
                value = ("» " if marked else "   ") + cases[k] + (" — " + cases["this_series"] if marked else "")
                p.text(f"case_{k}" + ("_this" if marked else ""), stats.static(value), h=5, advance=not marked, size=8.5,
                       weight="semibold" if marked else "normal", color=c.ACCENT if marked else c.MUTED,
                       visible=f"={computed} AND " + (this if marked else f"NOT ({this})"))
        p.text("cases_none", cases["none"], h=5, size=8, color=c.MUTED, visible=f"=NOT ({computed})")
    return block


def _forecast(params: dict[str, Any]):
    def block(p: stats.Page, w: dict) -> None:
        ch, s = w["charts"], w["series"]
        c.heading(p, "s_forecast", w["sections"]["forecast"])
        level = 1 - float(params.get("alpha") or 0.05)
        f = '[section] = "curve" AND [level] = "forecast"'
        _twice(p, {"id": "forecast", "type": "chart", "dataset": c.DATASET, "kind": "xy", "x": 0, "y": p.y, "w": c.WIDTH, "h": 78,
                   "title": stats.fit(ch["forecast"], 7.5, c.WIDTH - 4, 700), "filter": f, "category": "[x0]",
                   "series": [c.mark("observed", "line", s["observed"]), c.mark("fitted", "line", s["fitted"]),
                              c.mark("forecast", "line", s["forecast"]),
                              c.mark("lower_80", "band_low", s["band"].replace("{level}", "80")), c.mark("upper_80", "band_high"),
                              c.mark("lower", "band_low", s["band"].replace("{level}", f"{level * 100:g}")),
                              c.mark("upper", "band_high")],
                   "options": {"format": "0.###"}, "visible": c.exists(f)}, w)
        p.y += 79
        p.keep()
        t = w["tables"]
        holdout = c.num(c.model_key("holdout"))
        p.text("holdout_line", "=" + c.join(t["holdout"], k=c.shown(holdout, "#,##0"),
                                            rmse=c.shown(c.num(c.model_key("holdout_rmse"))),
                                            mape=c.shown(c.num(c.model_key("holdout_mape")), "0.00")),
               h=5, size=8, color=c.MUTED, can_grow=True, visible=f"={holdout} > 0")
        # the residuals over time, under the check of a changing variance
        r = '[section] = "curve" AND [level] = "residuals"'
        top, tall = p.y + 2, 58.0
        _twice(p, {"id": "residuals", "type": "chart", "dataset": c.DATASET, "kind": "xy", "x": 0, "y": top, "w": c.WIDTH,
                   "h": tall, "title": stats.fit(ch["residuals"], 7.5, c.WIDTH - 4, 700), "filter": r, "category": "[x0]",
                   "series": [c.mark("y", "line", w["series"]["residuals"])],
                   "options": {"format": "0.###", "target": 0, "legend": False}, "visible": c.exists(r)}, w)
        _caption(p, w, "residuals", "arch_lm", 0, top + tall + 0.3, c.WIDTH)
        p.items.append({"id": "residuals_keep", "type": "rect", "x": 0, "y": top, "w": 0.1, "h": tall + 4.8, "style": {}})
        p.y = top + tall + 4.8 + 3
    return block


def _candidates(params: dict[str, Any]):
    """With `orders = auto`: why these orders (one sentence a step), then every candidate,
    sorted by the criterion, the chosen one marked."""
    criterion = params.get("criterion") or "aicc"

    def block(p: stats.Page, w: dict) -> None:
        t = w["tables"]
        c.heading(p, "s_candidates", w["sections"]["candidates"])
        reasons = '[section] = "model" AND STARTSWITH([stat], "orders_reason_")'
        c.text_rows(p, "orders_reason", t["reasons"], reasons, t["reason"])
        one = f'[section] = "candidate" AND [stat] = "{criterion}"'
        chosen = c.of_row("candidate", "chosen")
        table = stats.table("candidates", t["candidates"], one, [
            {"header": t["candidate"], "value": "[level]",
             "rules": [{"when": f"{chosen} = 1", "style": {"weight": "bold", "color": c.ACCENT}}]},
            *({"header": c.stat_label(w, "candidate", s_), "value": c.number_of_row("candidate", s_), "width": 26,
               "align": "right", "format": "0.##"} for s_ in ("aicc", "aic", "bic")),
            {"header": c.stat_label(w, "candidate", "converged"),
             "value": f'IF({c.of_row("candidate", "converged")} = 1, {c.q(w["coef"]["yes"])}, {c.q(w["coef"]["no"])})', "width": 24},
            {"header": c.stat_label(w, "candidate", "chosen"), "value": f'IF({chosen} = 1, "✓", "")', "width": 18, "align": "center"},
        ], p.y, visible=c.exists(one))
        table["sort"] = [{"by": "[value]"}, {"by": "[ord]"}]
        p.add(table, 10)
    return block


def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    variant = catalog.variant(model, catalog.check_params(model, params))
    w = c.words(model, variant, locale)
    k, ch, s, ct = w["kpi"], w["charts"], w["series"], w["tables"]
    holdout = c.num(c.model_key("holdout"))
    cards = (c.Card("k_n", k["n"], c.num(c.model_key("n")), "#,##0"),
             c.Card("k_aic", k["aicc"], c.shown(c.num(c.model_key("aicc"))),
                    under=c.join(k["aic_bic"], aic=c.shown(c.num(c.model_key("aic"))), bic=c.shown(c.num(c.model_key("bic"))))),
             c.Card("k_sigma2", k["sigma2"], c.shown(c.num(c.model_key("sigma2")))),
             (c.Card("k_mape", k["holdout_mape"], c.shown(c.num(c.model_key("holdout_mape")), "0.00"), visible=f"={holdout} > 0"),
              c.Card("k_mape_in", k["mape"], c.shown(c.num(c.model_key("mape")), "0.00"), visible=f"=COALESCE({holdout}, 0) = 0")))
    tables = (c.CoefTable("coef", ct["estimates"], ("estimate", "std_error", "statistic", "p_value")),
              c.CoefTable("coef_ci", ct["intervals"], ("ci_low", "ci_high"), width=34))

    def lags(id_: str, under: str) -> c.Chart:
        f = f'[section] = "lag" AND [level] = "{id_}"'
        stat = "pacf" if id_.endswith("pacf") else "acf"
        return c.xy(id_, ch[id_], f + f' AND [stat] = "{stat}"', [c.mark(stat, "stem", s[stat])],
                    {"target": c.num(f"lag|{id_}||bound"), "mirror": True, "legend": False, "format": "0.##"}, under=under)

    qq = c.xy("qq", ch["qq"], '([section] = "point" OR [section] = "curve") AND [level] = "qq"', [
        {"value": 'IF([section] = "point", [value], NULL)', "as": "point", "name": s["residuals"]},
        {"value": 'IF([section] = "curve", [value], NULL)', "as": "line", "name": s["qq_line"]}], {"legend": False},
        under="jarque_bera")
    charts = (lags("acf", "stationarity"), lags("pacf", "stationarity"), lags("resid_acf", "ljung_box_10"),
              lags("resid_pacf", "ljung_box_10"), c.histogram("c_residual", ch["residual"], "residual", s["count"]), qq)
    blocks = (_stationarity(params), _forecast(params)) + ((_candidates(params),) if variant == "auto" else ())
    return c.page(model, params, locale, sample, step, name=name(params, locale), about=about(params, locale), cards=cards,
                  coef_tables=tables, blocks=blocks, charts=charts)


register("arima", layout, name, about)
