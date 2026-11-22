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

The explanation (docs/specs/report-explanations.md, lot B3): a reading under the summary, the stationarity, the
assumptions, the coefficients and the forecast, and «How this report was computed», chosen by `readings` from the rows.
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import register
from app.engine.models.layouts import shared_readings as shared
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


def _verdict_of_table(check: str, p: str, alpha: float) -> tuple[str, str]:
    """When ADF or KPSS rejects and when it keeps, as expressions of its p-value `p`: `table_rejects` said for the page
    (alpha is the node's, known here). Beyond the end of its table neither holds: the test gives no verdict."""
    low, high = c.BOUNDED_P[check]
    inside = f"{p} > {low!r} AND {p} < {high!r}"
    rejects = [f"({inside} AND {p} < {alpha!r})"] + ([f"{p} <= {low!r}"] if alpha >= low else [])
    keeps = [f"({inside} AND {p} >= {alpha!r})"] + ([f"{p} >= {high!r}"] if alpha <= high else [])
    return "(" + " OR ".join(rejects) + ")", "(" + " OR ".join(keeps) + ")"


def _stationarity(params: dict[str, Any]):
    """ADF and KPSS read together: the four cases, this series' one marked, each test read by the side of its table as
    the reading reads it (`table_rejects`); with a test that gives no verdict, none is marked."""
    alpha = float(params.get("alpha") or 0.05)

    def block(p: stats.Page, w: dict) -> None:
        cases = w["cases"]
        adf, kpss = c.num("assumption|adf||p_value"), c.num("assumption|kpss||p_value")
        both = '[key] = "assumption|adf||p_value" OR [key] = "assumption|kpss||p_value"'
        computed = f"COUNTIF({both}) = 2"
        start = len(p.items)
        c.heading(p, "s_cases", cases["title"])
        p.text("cases_alpha", "=" + c.join(cases["alpha"], alpha=c.shown(repr(alpha), "0.###")), h=5, size=8, color=c.MUTED,
               visible="=" + computed)
        p.keep()
        rule = {"1": (True, False), "2": (False, True), "3": (True, True), "4": (False, False)}
        sides = {"adf": _verdict_of_table("adf", adf, alpha), "kpss": _verdict_of_table("kpss", kpss, alpha)}
        for k, (adf_rejects, kpss_rejects) in rule.items():
            this = f"{sides['adf'][0 if adf_rejects else 1]} AND {sides['kpss'][0 if kpss_rejects else 1]}"
            for marked in (True, False):
                value = ("» " if marked else "   ") + cases[k] + (" — " + cases["this_series"] if marked else "")
                p.text(f"case_{k}" + ("_this" if marked else ""), stats.static(value), h=5, advance=not marked, size=8.5,
                       weight="semibold" if marked else "normal", color=c.ACCENT if marked else c.MUTED,
                       visible=f"={computed} AND " + (this if marked else f"NOT ({this})"))
        # the heading, its introduction and the first three cases go to the page of the fourth (`keep_with_next`): never
        # the title and «The four cases…» at the foot of a page and the cases on the next (lot F9, seen on kind)
        for item in p.items[start:]:
            if not item["id"].startswith("case_4"):
                item["keep_with_next"] = True
        p.text("cases_none", cases["none"], h=5, size=8, color=c.MUTED, visible=f"=NOT ({computed})")
        c.reading_under(p, w, "stationarity")
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
        c.reading_under(p, w, "forecast")
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


def _p_of_table(p: str) -> str:
    """A p-value read from urca's table, which stops at 0.01 and 0.10: «≤ 0.01», «≥ 0.1», else
    three decimals."""
    return (f'IF({p} <= 0.01, "≤ " & FORMAT(0.01, "0.##"), IF({p} >= 0.1, "≥ " & FORMAT(0.1, "0.#"), '
            f'FORMAT({p}, "0.###")))')


REASON_FIELDS = ("reason_seasonal", "reason_differences", "reason_search")


def _reasons(params: dict[str, Any], w: dict) -> list[dict]:
    """The sentences of the steps of the choice, as calculated fields of the dataset (one
    expression each would be too long for one column): the runner writes each step's key
    (`D_seas_yes`, `d_diff_0`, `search_stepwise`…) and the numbers of its test
    (`order_test_*`); the words say it in the page's language with those numbers. A field is
    empty on every other row; a key without words is shown as it is."""
    a, r = w["auto"], w["auto"]["reasons"]
    test = params.get("unit_root_test") or "kpss"
    kind = "kpss" if test == "kpss" else "unit"
    test_name = c.q(a["tests"].get(test, test))
    criterion = c.q(w["values"]["criterion"].get(params.get("criterion") or "aicc", "AICc"))
    D = c.num(c.model_key("D"))
    three = lambda key: c.shown(c.num(c.model_key(key)), "0.###")  # noqa: E731
    threshold = 'FORMAT(0.64, "0.##")'
    seasonal = [
        ("exog_residuals", c.join(r["exog_residuals"])),
        ("D_short", c.join(r["D_short"], two_s=c.shown(f"2 * {c.num(c.model_key('s'))}", "#,##0"))),
        ("D_error", c.join(r["D_error"])),
        ("D_dropped", c.join(r["D_dropped"])),
    ]
    for case in ("yes", "no", "capped"):
        seasonal.append((f"D_seas_{case}", c.join(r[f"D_seas_{case}"], strength=three("order_test_seasonal_strength"),
                                                  threshold=threshold)))
        seasonal.append((f"D_ocsb_{case}", c.join(r[f"D_ocsb_{case}"], statistic=three("order_test_ocsb_statistic"),
                                                  critical=three("order_test_ocsb_critical"))))
    # the steps of d: «d_diff_1» is the test at the first difference; k is the key's last character
    k = "RIGHT([text], 1)"
    series = (f'IFS({k} = "0", IF({D} > 0, {c.q(a["series_seasonal"])}, {c.q(a["series"][0])}), {k} = "1", '
              f'{c.q(a["series"][1])}, {c.q(a["series"][2])})')
    p_said = _p_of_table(f'LOOKUP("model|||order_test_{test}_d" & {k} & "_p_value", [key], [value])')
    parts = {"series": series, "test": test_name, "p": p_said, "k": k}
    steps = [(f"d_{case}", c.join(r[f"d_{case}_{kind}"], **parts)) for case in ("diff", "keep", "max")]
    steps += [(f"d_{case}", c.join(r[f"d_{case}"], **parts)) for case in ("constant", "error", "dropped")]
    # the chosen model: its name, and its drift or its mean when it has one
    has = lambda term: f'NOT ISNULL(LOOKUP("coef|{term}||estimate", [key], [value]))'  # noqa: E731
    model = (f'{c.txt(c.model_key("model_name"))} & IF({has("drift")}, {c.q(" " + a["with_drift"])}, '
             f'IF({has("intercept")}, {c.q(" " + a["with_mean"])}, ""))')
    n = c.shown(c.num(c.model_key("candidates")), "#,##0")
    search = [(key, c.join(r[key], model=model, criterion=criterion, n=n))
              for key in ("search_stepwise", "search_all", "search_stepwise_approx", "search_all_approx")]

    def field(name: str, pairs: list[tuple[str, str]], of: str) -> dict:
        said = ", ".join(f"{of} = {c.q(key)}, {e}" for key, e in pairs)
        return {"name": name, "expr": f'IF(STARTSWITH([stat], "orders_reason_"), IFS({said}, NULL), NULL)'}

    return [field(REASON_FIELDS[0], seasonal, "[text]"), field(REASON_FIELDS[1], steps, "LEFT([text], LEN([text]) - 2)"),
            field(REASON_FIELDS[2], search, "[text]"), _auto_advice(w)]


def _auto_advice(w: dict) -> dict:
    """With automatic orders, the advice that would send the reader to `orders = auto` (ADF,
    KPSS, Ljung–Box) says instead what to do when the automatic choice leaves the check
    failed: the column `text` again, those four sentences replaced."""
    swaps = ", ".join(f"[text] = {c.q(w['checks'][check]['advice'])}, {c.q(said)}" for check, said in w["auto"]["advice"].items())
    return {"name": "text", "expr": f'IF([section] = "assumption" AND [stat] = "advice", IFS({swaps}, [text]), [text])'}


def _candidates(params: dict[str, Any]):
    """With `orders = auto`: why these orders (one sentence a step, in the page's language,
    with the numbers of its test), then every candidate auto.arima fitted, sorted by the
    criterion, the chosen one marked; under it, what an empty criterion means and, when
    the search used the approximation, that its criteria are the approximate ones."""
    criterion = params.get("criterion") or "aicc"

    def block(p: stats.Page, w: dict) -> None:
        t, a = w["tables"], w["auto"]
        c.heading(p, "s_candidates", w["sections"]["candidates"])
        reasons = '[section] = "model" AND STARTSWITH([stat], "orders_reason_")'
        p.add(stats.table("orders_reason", t["reasons"], reasons, [
            {"header": t["reason"], "value": 'MID([stat], 15, 3)', "width": 12, "align": "right"},
            {"header": a["decision"], "value": "COALESCE(" + ", ".join(f"[{f}]" for f in REASON_FIELDS) + ", [text])"},
        ], p.y, visible=c.exists(reasons)), 10)
        one = f'[section] = "candidate" AND [stat] = "{criterion}"'
        chosen = c.of_row("candidate", "chosen")
        said = (f'REPLACE(REPLACE([level], " with drift", {c.q(" " + a["with_drift"])}), " with mean", '
                f'{c.q(" " + a["with_mean"])})')
        table = stats.table("candidates", t["candidates"], one, [
            {"header": t["candidate"], "value": said,
             "rules": [{"when": f"{chosen} = 1", "style": {"weight": "bold", "color": c.ACCENT}}]},
            *({"header": c.stat_label(w, "candidate", s_), "value": c.number_of_row("candidate", s_), "width": 26,
               "align": "right", "format": "0.##"} for s_ in ("aicc", "aic", "bic")),
            {"header": c.stat_label(w, "candidate", "converged"),
             "value": f'IF({c.of_row("candidate", "converged")} = 1, {c.q(w["coef"]["yes"])}, {c.q(w["coef"]["no"])})', "width": 24},
            {"header": c.stat_label(w, "candidate", "chosen"), "value": f'IF({chosen} = 1, "✓", "")', "width": 18, "align": "center"},
        ], p.y, visible=c.exists(one))
        table["sort"] = [{"by": "[value]"}, {"by": "[ord]"}]
        p.add(table, 10)
        empty = f'{one} AND ISNULL([value])'
        p.text("candidates_left_out", a["left_out"], h=5, size=7.5, color=c.MUTED, can_grow=True,
               visible=f"=COUNTIF({empty}) > 0")
        approx = f'{reasons} AND RIGHT([text], 7) = "_approx"'
        p.text("candidates_approx", a["approx_note"], h=5, size=7.5, color=c.MUTED, can_grow=True,
               visible=f"=COUNTIF({approx}) > 0")
    return block


def _adf_words(doc: dict, w: dict) -> None:
    """ADF's good outcome is the rejection of its H0 (a unit root): its row says «✓ rejected» / «× not rejected», the
    decomposition's words for the same test (lot D9), not «✓ ok» / «× failed». Only the words: a unit root left after
    the differencing is still a failed assumption of the fit, in the colour of a failure."""
    own = w["check_verdicts"]["adf"]
    header = w["assumptions"]["verdict"]
    for item in doc["sections"]["body"]["items"]:
        if item.get("id") not in ("assumptions", "advice"):
            continue
        verdict = "[text]" if item["id"] == "assumptions" else c.of_row("assumption", "verdict", "text")
        for col in item["columns"]:
            if col["header"] == header:
                cases = ", ".join(f'[level] = "adf" AND {verdict} = "{v}", {c.q(said)}' for v, said in own.items())
                col["value"] = f"IFS({cases}, TRUE, {col['value']})"


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
    doc = c.page(model, params, locale, sample, step, name=name(params, locale), about=about(params, locale), cards=cards,
                 coef_tables=tables, blocks=blocks, charts=charts, explained=("summary", "assumptions", "coefficients"))
    if variant == "auto":
        doc["datasets"][0]["calculated"] = _reasons(params, w)
    _adf_words(doc, w)
    return doc


# ── the explanation (docs/specs/report-explanations.md, lot B3) ─────────────
# The lines are chosen from the rows the runner wrote: a verdict only where a row has a p-value, read against the
# node's alpha (ADF's and KPSS's at the ends of their tables by the side of the statistic, as the runner does: lot
# D7); every other number is said as a measure. The words are the decomposition's for the same tests (ADF, KPSS,
# Ljung–Box), said of the differenced series and of the residuals.
RESIDUAL_TESTS = ("ljung_box_10", "ljung_box_2s", "arch_lm", "variance_ratio", "jarque_bera")
NAMED = 5  # the most terms a sentence names (the table has them all)


def _number(x: Any) -> float | None:
    return float(x) if isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) else None


def table_rejects(check: str, p: float, alpha: float) -> bool | None:
    """Whether ADF or KPSS rejects at alpha, with a p-value read from a table that stops at its ends (common.BOUNDED_P):
    at the lower end p is at most that end and rejects at any alpha from it on, at the upper end p is at least that end
    and keeps at any alpha up to it; beyond, the table cannot say (None). Inside, p against alpha."""
    low, high = c.BOUNDED_P[check]
    if p <= low:
        return True if alpha >= low else None
    if p >= high:
        return False if alpha <= high else None
    return p < alpha


def _unit_root(check: str, row: dict, alpha: float) -> tuple[c.ReadingLine | None, bool | None]:
    """The line of ADF or KPSS on the differenced series, and whether it rejects (None: no verdict)."""
    low, high = c.BOUNDED_P[check]
    p, statistic = _number(row.get("p_value")), _number(row.get("statistic"))
    if p is None:
        if row.get("verdict") == "not_computed" and row.get("advice_id") == "table_end" and statistic is not None:
            # no p-value is written: alpha says which end it was (below the lower end, or above the upper one)
            bound = low if alpha < low else high
            return ("stationarity", None, f"{check}_table_end", {"bound": bound, "alpha": alpha, "statistic": statistic}), None
        return None, None
    rejects = table_rejects(check, p, alpha)
    if rejects is None:
        bound = low if p <= low else high
        return ("stationarity", None, f"{check}_table_end", {"bound": bound, "alpha": alpha, "statistic": statistic}), None
    if rejects:
        line = (f"{check}_reject_end", {"bound": low, "alpha": alpha}) if p <= low else (f"{check}_reject", {"p": p, "alpha": alpha})
    else:
        line = (f"{check}_keep_end", {"bound": high, "alpha": alpha}) if p >= high else (f"{check}_keep", {"p": p, "alpha": alpha})
    return ("stationarity", None, line[0], line[1]), rejects


# the four cases of ADF and KPSS read together (the decomposition's words): (ADF rejects, KPSS rejects) → id
CASES = {(True, False): "stationarity_both_ok", (True, True): "stationarity_adf_only", (False, False): "stationarity_kpss_only",
         (False, True): "stationarity_neither"}


def readings(rows: list[dict], checked: Mapping[str, Any]) -> list[c.ReadingLine]:
    alpha = float(checked.get("alpha") or 0.05)
    keyed = {r["key"]: r for r in rows if r.get("key")}

    def num(key: str) -> float | None:
        return _number(keyed.get(key, {}).get("value"))

    def text(key: str) -> str | None:
        return keyed.get(key, {}).get("text")

    def check(name: str) -> dict:
        return {s: (keyed.get(f"assumption|{name}||{s}") or {}).get("value" if s not in ("verdict", "advice_id") else "text")
                for s in ("p_value", "statistic", "value", "threshold", "verdict", "advice_id")}

    out: list[c.ReadingLine] = []
    model = {k: num(c.model_key(k)) for k in ("n", "p", "d", "q", "P", "D", "Q", "s", "holdout", "horizon", "box_cox_lambda",
                                              "candidates", "holdout_mape", "holdout_mase", "mape", "mase")}
    n = int(model["n"] or 0)
    seasonal = (model["s"] or 1) > 1 and any(model[k] for k in ("P", "D", "Q"))
    orders = {k: int(model[k] or 0) for k in ("p", "d", "q", "P", "D", "Q")}
    # summary: the orders in words, the transformation, the exogenous columns, the automatic choice
    if seasonal:
        out.append(("summary", None, "summary_sarima", {"n": n, **orders, "s": int(model["s"] or 0)}))
    else:
        out.append(("summary", None, "summary_arima", {"n": n, "p": orders["p"], "d": orders["d"], "q": orders["q"]}))
    transform = text(c.model_key("transform"))
    if transform == "log":
        out.append(("summary", None, "summary_transform_log", {}))
    elif transform == "box_cox" and model["box_cox_lambda"] is not None:
        out.append(("summary", None, "summary_transform_box_cox", {"lambda": model["box_cox_lambda"]}))
    exog = [str(e) for e in checked.get("exog") or []]
    if len(exog) > NAMED:
        out.append(("summary", None, "summary_exog_more", {"names": exog[:NAMED], "more": len(exog) - NAMED}))
    elif exog:
        out.append(("summary", None, "summary_exog", {"names": exog}))
    if checked.get("orders") == "auto" and model["candidates"] is not None:
        out.append(("summary", None, "auto_orders", {"n": int(model["candidates"])}))
    # stationarity: ADF and KPSS on the differenced series, then their case read together
    adf, kpss = check("adf"), check("kpss")
    if adf["verdict"] == "not_computed" and not adf["advice_id"] and kpss["verdict"] == "not_computed" and not kpss["advice_id"]:
        out.append(("stationarity", None, "stationarity_short", {}))
    else:
        adf_line, adf_rejects = _unit_root("adf", adf, alpha)
        kpss_line, kpss_rejects = _unit_root("kpss", kpss, alpha)
        out += [x for x in (adf_line, kpss_line) if x]
        if adf_rejects is not None and kpss_rejects is not None:
            out.append(("stationarity", None, CASES[(adf_rejects, kpss_rejects)], {}))
        elif adf_line or kpss_line:
            out.append(("stationarity", None, "stationarity_no_case", {}))
    # the residuals' tests: each one that rejects by its own sentence, the others counted
    tested = [(name, check(name)) for name in RESIDUAL_TESTS]
    tested = [(name, x) for name, x in tested if _number(x["p_value"]) is not None]
    rejected = [(name, x) for name, x in tested if x["p_value"] < alpha]
    for name, x in rejected:
        out.append(("assumptions", None, f"{name}_reject", {"p": x["p_value"], "alpha": alpha}))
    if tested and not rejected:
        out.append(("assumptions", None, "residuals_keep_all", {"total": len(tested), "alpha": alpha}))
    elif len(tested) > len(rejected):  # named as the table names them, in its order (five tests at most)
        others = [f"check:{name}" for name, x in tested if x["p_value"] >= alpha]
        out.append(("assumptions", None, "residuals_keep_others", {"k": len(others), "names": others, "alpha": alpha}))
    roots = check("roots")
    if roots["verdict"] in ("doubtful", "failed") and _number(roots["value"]) is not None and _number(roots["threshold"]) is not None:
        out.append(("assumptions", None, "roots_threshold", {"value": roots["value"], "threshold": roots["threshold"]}))
    if n > c.MANY_ROWS:
        out.append(("assumptions", None, "assumptions_many_rows", {"n": n}))
    # the coefficients whose p-value is below alpha, by their p-value: the lines every model shares (common.json's words)
    out += shared.coefficient_lines(rows, checked)[0]
    # the forecast and the errors on the holdout (in sample without one)
    forecast = any(r["section"] == "curve" and r["level"] == "forecast" and r["stat"] == "forecast" for r in rows)
    if forecast and model["horizon"]:
        out.append(("forecast", None, "forecast_horizon", {"horizon": int(model["horizon"]), "level": 1 - alpha}))
    if (model["holdout"] or 0) > 0 and model["holdout_mape"] is not None and model["holdout_mase"] is not None:
        out.append(("forecast", None, "holdout_errors", {"holdout": int(model["holdout"]), "mape": model["holdout_mape"],
                                                         "mase": model["holdout_mase"]}))
    elif not model["holdout"] and model["mape"] is not None and model["mase"] is not None:
        out.append(("forecast", None, "sample_errors", {"mape": model["mape"], "mase": model["mase"]}))
    # «How this report was computed»: the paragraphs, then the formulas
    method = ["method_arima"]
    if seasonal:
        method.append("method_seasonal")
    if orders["d"] or orders["D"]:
        method.append("method_differencing")
    if transform in ("log", "box_cox"):
        method.append(f"method_transform_{transform}")
    if exog:
        method.append("method_exog")
    if checked.get("orders") == "auto":
        method.append("method_auto")
    method += ["method_stationarity", "method_checks"]
    if (model["holdout"] or 0) > 0:
        method.append("method_holdout")
    if forecast:
        method.append("method_bands")
    formulas = ["formula_sarima" if seasonal else "formula_arima", "formula_aicc"]
    if transform == "box_cox":
        formulas.append("formula_box_cox")
    formulas += ["formula_adf", "formula_kpss", "formula_ljung_box", "formula_mape", "formula_mase"]
    out += [("method", None, m, {}) for m in method]
    out += [("formulas", None, f, {}) for f in formulas]
    return out


c.READINGS["arima"] = readings
register("arima", layout, name, about)
