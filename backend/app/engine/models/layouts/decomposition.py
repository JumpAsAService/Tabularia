"""The report of the node «Decomposition» (docs/specs/ts-decomposition.md, «The report (pixel perfect)»): a series split
into a trend, one seasonal pattern per period and a remainder (STL, classical, MSTL), or into its oscillations and a
residue (EMD), with the tests of the seasons and of the remainder and the measures that are not tests.

1. Summary: four cards (the observations and their span, the method, the trend's strength, the seasonal strength of
   each period, or EMD's largest oscillation), the formula line, the checks (seasonality per period, Ljung–Box, ADF,
   KPSS, EMD's facts), the measures under «Measures, not tests», the readings of the summary, the strengths and the
   stationarity.
2. Components (STL, classical, MSTL): panels along time, one under the other — the series with its trend, one panel per
   seasonal period, the remainder as points — and the seasonally adjusted series.
3. Seasonal patterns: their table, the mean pattern by position (a short period), one chart per period (the mean pattern
   and the range of the cycles, position 1 named), the readings of each period.
4. Remainder: its histogram and its ACF with the bounds ±z/√n, its checks, its reading.
5. Oscillations (EMD): a panel per IMF and the residue, their table, the reading.
6. How this report was computed (`common.how_computed`), the numbers of the decomposition and the notes.

The time's axis follows the runner's `time_kind`, as ARIMA's report does: each chart along time is drawn twice in the
same place, as dates and as steps, one visible. The positions of a season (`weekday_3`, `month_4`, `step_6`) are said
in the page's words (`values.position`, `values.position_in_text`) by a calculated field: the readings and the table
of the seasons say «on Wednesday», «in April», «at position 6». Every number is read from the runner's rows; the words
are in `report_texts/decomposition.json`. The explanation's helpers are every report's (`common.py`: `reading_under`,
`how_computed`; docs/specs/report-explanations.md, docs/specs/ts-decomposition.md, Open question 12).
"""
from __future__ import annotations

from typing import Any

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import register
from app.engine.stats import layouts as stats
from app.reports import glyphs

WORDS = "decomposition"
COMPONENTS_TALL, PANEL_TALL, IMF_TALL, SEASON_TALL = 46.0, 32.0, 24.0, 62.0
# a period whose mean pattern by position is a table too (a week, a year of months, a day of hours, a year of weeks);
# a longer one is its chart only: 365 rows would be pages of numbers
SHAPE_TABLE_MAX = 53
# the kinds of positions the runner writes (`position_kind`, ts_positions of ts_checks.R, lot D1)
POSITION_KINDS = ("weekday", "month", "quarter", "hour", "weekday_hour", "yearday", "step")
# the codes of positions (ts_positions: `weekday_3`, `month_12`, `hour_14`, `weekday_3_hour_14`, `quarter_1`,
# `yearday_45`, `step_6`), in the order they are replaced: «month_12» before «month_1»; a weekday's hour is first
# marked «+hour_» so that the weekday's words and the hour's are joined by the words' «+»
POSITION_CODES = ([f"weekday_{i}" for i in range(1, 8)] + [f"month_{i}" for i in range(12, 0, -1)]
                  + [f"quarter_{i}" for i in range(1, 5)] + ["hour_", "yearday_", "step_", "+"])
SEASON_READING = '[section] = "reading" AND [level] = "season"'
SEASON_LABEL = '[section] = "season" AND ([stat] = "peak_label" OR [stat] = "trough_label")'
REMAINDER_CHECKS = ("ljung_box", "adf", "kpss")
MEASURES = ("trend_strength", "seasonal_strength", "largest_acf", "robust_weights", "orthogonality")


def _checked(params: dict[str, Any]) -> dict[str, Any]:
    return catalog.check_params(WORDS, dict(params))


def name(params: dict[str, Any], locale: str) -> str:
    method = str(_checked(params).get("method") or "stl")
    return c.words(WORDS, method, locale)["name"].get(method, method)


def about(params: dict[str, Any], locale: str) -> str:
    """«boxes» over «month», monthly series: the two columns named as columns, the frequency in the page's words."""
    w = c.words(WORDS, "stl", locale)
    value, time = params.get("value"), params.get("time")
    if not (value and time):
        return str(value or "")
    frequency = w["frequency_series"].get(str(params.get("frequency")), "")
    said = w["series_of"].replace("{value}", str(value)).replace("{time}", str(time)).replace("{frequency}", frequency)
    return said if frequency else said.rsplit(",", 1)[0]


def _numbers(sentence: str, lang: str, **values: float) -> str:
    """A static sentence with the node's numbers in it, each in the language's decimals («α = 0,05»)."""
    from app.reports import formats

    loc = formats.locale(lang)
    for k, v in values.items():
        sentence = sentence.replace("{" + k + "}", formats.format_number(float(v), "0.####", loc))
    return sentence


def periods_of(checked: dict[str, Any]) -> list[str]:
    """The seasonal periods the runner names its rows by («7», «365»), as the output's columns are named."""
    return catalog._seasonal_labels(checked)


# ── time along x: dates or steps ────────────────────────────────────────────
def _integer_time(w: dict) -> str:
    """True when the runner says the time is an integer (its word in the page's language: `with_labels` says a setting's
    value in it)."""
    return f'COUNTIF([key] = "model|||time_kind" AND [text] = {c.q(w["values"]["time_kind"]["integer"])}) > 0'


def _twice(p: stats.Page, item: dict, w: dict) -> None:
    """A chart along time, as dates and as integers, in the same place: one is visible."""
    shown = (item.get("visible") or c.exists("TRUE")).lstrip("=")
    integer = _integer_time(w)
    for as_date in (True, False):
        p.add({**item, "id": item["id"] + ("" if as_date else "_steps"), "options": {**item["options"], "x_time": as_date},
               "visible": f"={shown} AND " + (f"NOT ({integer})" if as_date else integer)})


def _panel(p: stats.Page, w: dict, id_: str, title: str, filter_: str, series: list[dict], tall: float,
           options: dict | None = None, collapse: bool = False) -> None:
    """A chart across the page along time, its own row of the body. `collapse`: a panel the run may leave empty (an IMF
    beyond those found) takes no room when hidden, instead of a blank band (`collapse_hidden`, lot D9)."""
    item = {"id": id_, "type": "chart", "dataset": c.DATASET, "kind": "xy", "x": 0, "y": p.y, "w": c.WIDTH, "h": tall,
            "title": stats.fit(title, 7.5, c.WIDTH - 4, 700), "filter": filter_, "category": "[x0]", "series": series,
            "options": {"format": "0.###", "legend": False, **(options or {})}, "visible": c.exists(filter_)}
    if collapse:
        item["collapse_hidden"] = True
    _twice(p, item, w)
    p.y += tall + 1.5


def _line(level_2: str, name_: str, colour: str | None = None, as_: str = "line") -> dict:
    s: dict[str, Any] = {"value": f'IF([level_2] = "{level_2}", [value], NULL)', "as": as_, "name": name_}
    if colour:
        s["color"] = colour
    return s


# ── the positions in words ──────────────────────────────────────────────────
def _positions(x: str, said: dict[str, str]) -> str:
    x = f'REPLACE({x}, "_hour_", "+hour_")'
    for code in POSITION_CODES:
        x = f"REPLACE({x}, {c.q(code)}, {c.q(said[code])})"
    return x


def _calculated(w: dict) -> list[dict]:
    """`text`: a season's reading and its highest and lowest positions with the positions in words; `imf_share`: an
    IMF's share of the variance (EMD's card: the largest one)."""
    v = w["values"]
    text = (f'IF({SEASON_READING}, {_positions("[text]", v["position_in_text"])}, '
            f'IF({SEASON_LABEL}, {_positions("[text]", v["position"])}, [text]))')
    share = 'IF([section] = "imf" AND [level] <> "residue" AND [stat] = "variance_share", [value], NULL)'
    return [{"name": "text", "expr": text}, {"name": "imf_share", "expr": share}]


# ── page 1: the summary ─────────────────────────────────────────────────────
def _card(p: stats.Page, id_: str, x: float, label: str, value: str, unders: list[tuple[str, str | None]],
          fmt: str | None = None) -> None:
    """A card; `unders`: the lines under its value, each with when it is shown (one at a time)."""
    item: dict[str, Any] = {"id": id_, "type": "kpi", "dataset": c.DATASET, "x": x, "y": p.y, "w": 45, "h": 20, "label": label,
                            "value": value}
    if fmt:
        item["format"] = fmt
    p.add(item)
    for i, (said, when) in enumerate(unders):
        p.text(f"{id_}_under" + (f"_{i}" if i else ""), "=" + said, h=4.5, x=x + 2, w=43, advance=False, size=7.5,
               color=c.MUTED, visible=when)
        p.items[-1].update({"y": p.y + 14.6, "dataset": c.DATASET})


def _span(lang: str, as_dates: bool) -> str:
    """The first and the last instant: «1 Jul 2023 – 15 Jul 2026», or «1 – 64» for a time counted in steps."""
    from app.reports import formats

    first, last = c.model_number("first"), c.model_number("last")
    if as_dates:
        day = lambda x: f'FORMAT(DATEADD("d", {x}, DATE(1970, 1, 1)), {c.q(formats.locale(lang).date)})'  # noqa: E731
        return f'{day(first)} & " – " & {day(last)}'
    return f'{c.shown(first, "#,##0")} & " – " & {c.shown(last, "#,##0")}'


def _cards(p: stats.Page, w: dict, lang: str, checked: dict[str, Any], periods: list[str]) -> None:
    k, method = w["kpi"], str(checked.get("method"))
    integer = _integer_time(w)
    _card(p, "k_n", 0, k["n"], c.model_number("n"),
          [(_span(lang, True), f"=NOT ({integer})"), (_span(lang, False), "=" + integer)], "#,##0")
    under = [k["periods" if len(periods) > 1 else "period"].replace("{p}", ", ".join(periods))] if periods else []
    if checked.get("robust") and method in ("stl", "mstl"):
        under.append(k["robust"])
    if method == "classical":
        under.append(w["values"]["type"].get(str(checked.get("type")), str(checked.get("type"))))
    said = c.q(" · ".join(under))
    if method == "emd":
        said = c.join(k["imfs"], n=c.shown(c.model_number("n_imfs"), "0")) + (f' & " · " & {said}' if under else "")
    word = w["values"]["method"].get(method, method)
    _card(p, "k_method", 47, k["method"], c.q(word[:1].upper() + word[1:]), [(said, None)])
    trend = c.num("assumption|trend_strength||value")
    _card(p, "k_trend", 94, k["trend"], f'IF(ISNULL({trend}), "–", {c.shown(trend, "0.00")})', [(c.q(k["strength_under"]), None)])
    if method == "emd":
        largest = "MAX([imf_share])"
        mode = c.join(w["modes"]["imf"], k='MID(LOOKUP(MAX([imf_share]), [imf_share], [level]), 5, 3)')
        _card(p, "k_seasonal", 141, k["largest"], f'IF(ISNULL({largest}), "–", {c.pct(largest, "0.#")})',
              [(c.join(k["largest_under"], mode=mode), f"=NOT ISNULL({largest})")])
    else:
        def strength(period: str) -> str:
            x = c.num(f"assumption|seasonal_strength|{period}|value")
            return f'IF(ISNULL({x}), "–", {c.shown(x, "0.00")})'

        value = ' & " · " & '.join(strength(period) for period in periods) or c.q("–")
        under_ = c.q(k["periods" if len(periods) > 1 else "period"].replace("{p}", ", ".join(periods)))
        _card(p, "k_seasonal", 141, k["seasonal"], value, [(under_, None)])
        widest = " · ".join(["0.00"] * len(periods))  # the strengths side by side: the size at which they fit the card
        size = next((pt for pt in (16, 14, 12, 11, 10) if glyphs.width_mm(widest, pt, 700) <= 45 - 5.5), 10)
        if size != 16:
            next(it for it in p.items if it["id"] == "k_seasonal")["style"] = {"size": size}
    p.y += 21.5


def _formula_line(w: dict, params: dict[str, Any], checked: dict[str, Any], periods: list[str], lang: str) -> str:
    """«boxes = trend + seasonal[12] + remainder · STL, robust · α = 0.05», from the node's settings."""
    f, method = w["formula_line"], str(checked.get("method"))
    value = str(params.get("value") or "")
    transform = str(checked.get("transform") or "none")
    left = f[transform].replace("{value}", value) if transform in ("log", "box_cox") else value
    if method == "emd":
        equation = f["emd"].replace("{left}", left)
    else:
        multiplicative = method == "classical" and checked.get("type") == "multiplicative"
        seasonal = (" × " if multiplicative else " + ").join(f["seasonal"].replace("{p}", p) for p in periods)
        equation = f["multiplicative" if multiplicative else "additive"].replace("{left}", left).replace("{seasonal}", seasonal)
    how = w["values"]["method"].get(method, method)
    if checked.get("robust") and method in ("stl", "mstl"):
        how += ", " + f["robust"]
    if method == "classical":
        how += ", " + w["values"]["type"].get(str(checked.get("type")), "")
    return " · ".join((equation, how, _numbers(f["alpha"], lang, alpha=float(checked.get("alpha", 0.05)))))


def _own_verdicts(item: dict, w: dict, verdict: str = "[text]") -> None:
    """The checks whose good outcome is the rejection of H0 (a seasonal pattern, ADF's unit root) say their verdict in
    their own words: not rejecting H0 is no evidence, not a broken assumption, so «– not supported» is drawn in the
    muted colour of what was not computed, not in the red of a failure (`check_verdicts`). `verdict`: the expression of
    the row's verdict (the row's own text in the checks' tables, a LOOKUP in the advice's)."""
    own = w["check_verdicts"]
    header = w["assumptions"]["verdict"]
    for col in item["columns"]:
        if col["header"] != header:
            continue
        cases = ", ".join(f'[level] = "{check}" AND {verdict} = "{v}", {c.q(said)}' for check, words in own.items()
                          for v, said in words.items())
        col["value"] = f"IFS({cases}, TRUE, {col['value']})"
        quiet = " OR ".join(f'[level] = "{check}"' for check in own)
        col.setdefault("rules", []).append({"when": f'({quiet}) AND {verdict} = "failed"',
                                            "style": {"color": c.MUTED, "weight": "normal"}})


def _p_column(item: dict, w: dict) -> None:
    """The decomposition's ADF p-value is MacKinnon's, continuous (urca's punitroot), not a table's: the checks' column
    of p-values keeps «≥ 0.1» and «≤ 0.01» for KPSS only, whose p-value tseries reads from its table."""
    x = c.of_row("assumption", "p_value")
    said = c.p_said(x)
    said = (f'IF([level] = "kpss" AND NOT ISNULL({x}) AND {x} <= 0.01, "≤ " & {c.shown("0.01", "0.##")}, '
            f'IF([level] = "kpss" AND NOT ISNULL({x}) AND {x} >= 0.1, "≥ " & {c.shown("0.1", "0.##")}, {said}))')
    header = w["assumptions"]["p_value"]
    for col in item["columns"]:
        if col["header"] == header:
            col["value"] = said


def _summary(p: stats.Page, w: dict, lang: str, params: dict[str, Any], checked: dict[str, Any], periods: list[str]) -> None:
    p.heading("s_summary", w["sections"]["summary"])
    _cards(p, w, lang, checked, periods)
    p.keep()
    p.text("formula", stats.static(_formula_line(w, params, checked, periods, lang)), h=6.5, size=9.5, weight="semibold",
           background=c.PANEL, padding=1.5, can_grow=True)
    p.y += 1.0
    alpha_line = _numbers(w["alpha_line"], lang, alpha=float(checked.get("alpha", 0.05)))
    c._assumptions(p, w, lang, alpha_line, True)
    _p_column(next(it for it in p.items if it["id"] == "assumptions"), w)
    _own_verdicts(next(it for it in p.items if it["id"] == "assumptions"), w)
    _own_verdicts(next(it for it in p.items if it["id"] == "advice"), w, c.of_row("assumption", "verdict", "text"))
    lag = c.of_row("assumption", "lag")
    c.measures(MEASURES, more={"largest_acf": f'IF(ISNULL({lag}), "", {c.join(w["at_lag"], lag=c.shown(lag, "0"))})'})(p, w)
    c.reading_under(p, w, "summary", "strength")
    c.reading_under(p, w, "stationarity")


# ── page 2: the components ──────────────────────────────────────────────────
def _components(p: stats.Page, w: dict, checked: dict[str, Any], periods: list[str]) -> None:
    ch, s = w["charts"], w["series"]
    ratio = checked.get("method") == "classical" and checked.get("type") == "multiplicative"
    centre = 1 if ratio else 0
    p.heading("s_components", w["sections"]["components"])
    one = '[section] = "curve" AND [level] = "components"'
    _panel(p, w, "components", ch["observed_trend"], f'{one} AND ([level_2] = "observed" OR [level_2] = "trend")',
           [_line("observed", s["observed"], c.MUTED), _line("trend", s["trend"], c.ACCENT)], COMPONENTS_TALL, {"legend": True})
    p.keep()  # the heading with the first panel
    for period in periods:
        _panel(p, w, f"seasonal_{period}", ch["seasonal"].replace("{p}", period), f'{one} AND [level_2] = "seasonal_{period}"',
               [_line(f"seasonal_{period}", s["seasonal"], c.ACCENT)], PANEL_TALL, {"target": centre})
    points = '[section] = "point" AND [level] = "remainder"'
    _panel(p, w, "remainder", ch["remainder"], points, [c.mark("y", "point", s["remainder"])], PANEL_TALL, {"target": centre})
    adjusted = '[section] = "curve" AND [level] = "adjusted"'
    _panel(p, w, "adjusted", ch["adjusted"], adjusted, [_line("deseasonalized", s["adjusted"], c.ACCENT)], COMPONENTS_TALL)


# ── page 3: the seasonal patterns ───────────────────────────────────────────
def _seasons(p: stats.Page, w: dict, checked: dict[str, Any], periods: list[str]) -> None:
    t, ch, s = w["tables"], w["charts"], w["series"]
    ratio = checked.get("method") == "classical" and checked.get("type") == "multiplicative"
    p.heading("s_seasons", w["sections"]["seasons"])
    one = '[section] = "season" AND [stat] = "cycles"'

    def number(header: str, stat: str, width: float, pattern: str = "#,##0.####") -> dict:
        x = c.of_row("season", stat)
        return {"header": header, "value": f'IF(ISNULL({x}), "", {c.shown(x, pattern)})', "width": width, "align": "right"}

    cols = [{"header": t["period"], "value": "[level]", "width": 18, "style": {"weight": "semibold"}},
            number(t["window"], "s_window_used", 18, "#,##0"), number(t["cycles"], "cycles", 16, "#,##0"),
            number(t["amplitude"], "amplitude", 22),
            {"header": t["peak"], "value": c.of_row("season", "peak_label", "text")}, number(t["value"], "peak_value", 22),
            {"header": t["trough"], "value": c.of_row("season", "trough_label", "text")}, number(t["value"], "trough_value", 22)]
    p.add(stats.table("seasons", t["seasons"], one, cols, p.y, visible=c.exists(one)), 10)
    charts = []
    for period in periods:
        f = f'[section] = "curve" AND [level] = "subseries" AND [level_2] = "subseries_{period}"'
        kind = c.txt(f"season|{period}||position_kind")
        pc = w["position_caption"]
        caption = "IFS(" + ", ".join(f'{kind} = "{k}", {c.q(pc[k])}' for k in POSITION_KINDS) + f', TRUE, {c.q(pc["step"])})'
        key = f"assumption|seasonality|{period}|verdict"
        words = {**w["verdicts"], **w["check_verdicts"]["seasonality"]}
        verdict = "IFS(" + ", ".join(f'{c.txt(key)} = "{v}", {c.q(words[v])}' for v in catalog.VERDICTS) + ', "")'
        said = c.join(w["illustrates"], check=f"LOOKUP({c.q(key)}, [key], [label])", verdict=verdict)
        measured = max(pc.values(), key=len) + " · " + w["checks"]["seasonality"]["name"] + " — " + w["verdicts"]["not_computed"]
        charts.append(c.xy(f"subseries_{period}", ch["subseries"].replace("{p}", period), f,
                           [c.mark("low", "band_low", s["range"]), c.mark("high", "band_high"), c.mark("mean", "line", s["mean"])],
                           {"legend": True, "target": 1 if ratio else 0, "format": "0.###"}, wide=True)
                      ._replace(caption=f'={caption} & IF(COUNTIF([key] = {c.q(key)}) > 0, " · " & {said}, "")',
                                measured=measured))
    c.grid(p, w, tuple(charts), None, tall=SEASON_TALL)
    read = f'{SEASON_READING} AND [stat] = "line_1"'
    lines = f'TRIM({c.reading_of("seasonality", "[level_2]", 4)} & " " & {c.reading_of("season", "[level_2]", 4)})'
    table = stats.table("reading_seasons", w["reading"]["seasons"], read, [
        {"header": w["reading"]["variant"], "value": "[level_2]", "width": 18, "style": {"weight": "semibold"}},
        {"header": w["reading"]["text"], "value": lines, "style": {"italic": True, "size": 8}},
    ], p.y, visible=c.exists(read))
    table["style"] = {"size": 8}
    p.add(table, 10)
    short = [period for period in periods if int(period) <= SHAPE_TABLE_MAX]
    if short:
        shape = ('[section] = "curve" AND [level] = "subseries" AND [stat] = "mean" AND ('
                 + " OR ".join(f'[level_2] = "subseries_{period}"' for period in short) + ")")

        def edge(stat: str) -> str:
            return f'LOOKUP(REPLACE([key], "|mean|", "|{stat}|"), [key], [value])'

        p.add(stats.table("season_shape", t["shape"], shape, [
            {"header": t["period"], "value": 'MID([level_2], 11, 10)', "width": 18, "style": {"weight": "semibold"}},
            {"header": t["position"], "value": "[x0]", "format": "0", "width": 18, "align": "right"},
            {"header": t["mean"], "value": c.shown("[value]", "#,##0.####"), "align": "right"},
            {"header": t["low"], "value": c.shown(edge("low"), "#,##0.####"), "align": "right"},
            {"header": t["high"], "value": c.shown(edge("high"), "#,##0.####"), "align": "right"},
        ], p.y, visible=c.exists(shape)), 10)


# ── page 4: the remainder ───────────────────────────────────────────────────
def _remainder(p: stats.Page, w: dict, checked: dict[str, Any]) -> None:
    t, ch, s = w["tables"], w["charts"], w["series"]
    p.heading("s_remainder", w["sections"]["remainder"])
    # the heading and its rule go to the next page with what follows them: never alone at the foot of a page
    for item in p.items[-2:]:
        item["keep_with_next"] = True
    emd = checked.get("method") == "emd"
    # EMD with noise_imfs = 0 separates no remainder: the runner writes none of its charts, and charts nobody sees would
    # still keep their room (a blank band of a quarter of a page before the checks)
    charts = not emd or int(checked.get("noise_imfs") or 0) > 0
    if emd and charts:  # the remainder of EMD (the IMFs read as noise) has no panel of components
        _panel(p, w, "noise", ch["noise"], '[section] = "point" AND [level] = "remainder"',
               [c.mark("y", "point", s["remainder"])], PANEL_TALL, {"target": 0})
    f = '[section] = "lag" AND [level] = "resid_acf"'
    acf = c.xy("resid_acf", ch["resid_acf"], f + ' AND [stat] = "acf"', [c.mark("acf", "stem", s["acf"])],
               {"target": c.num("lag|resid_acf||bound"), "mirror": True, "legend": False, "format": "0.##"}, under="ljung_box")
    if charts:
        c.grid(p, w, (c.histogram("histogram", ch["histogram"], "remainder", s["count"]), acf), None)
    one = ('[section] = "assumption" AND [stat] = "verdict" AND ('
           + " OR ".join(f'[level] = "{check}"' for check in REMAINDER_CHECKS) + ")")
    said = "IFS(" + ", ".join(f'[text] = "{v}", {c.q(w["verdicts"][v])}' for v in catalog.VERDICTS) + ', "")'
    df = c.of_row("assumption", "df")
    item = stats.table("remainder_checks", t["remainder_checks"], one, [
        {"header": t["check"], "value": "[label]"},
        {"header": t["statistic"], "value": c.number_of_row("assumption", "statistic"), "width": 22, "align": "right", "format": "0.####"},
        {"header": w["assumptions"]["df"], "value": f'IF(ISNULL({df}), "", FORMAT({df}, "0.##"))', "width": 12, "align": "right"},
        {"header": w["assumptions"]["p_value"], "value": "", "width": 20, "align": "right"},
        {"header": w["assumptions"]["verdict"], "value": said, "width": 26,
         "rules": [{"when": f'[text] = "{v}"', "style": {"color": colour, "weight": "semibold"}} for v, colour in c.VERDICT_COLOURS.items()]},
    ], p.y, visible=c.exists(one))
    _p_column(item, w)
    _own_verdicts(item, w)
    p.add(item, 10)
    c.reading_under(p, w, "remainder")


# ── page 5: the oscillations (EMD) ──────────────────────────────────────────
def _oscillations(p: stats.Page, w: dict, checked: dict[str, Any]) -> None:
    t, ch, s = w["tables"], w["charts"], w["series"]
    p.heading("s_oscillations", w["sections"]["oscillations"])
    one = '[section] = "curve" AND [level] = "imfs"'
    _panel(p, w, "imf_observed", ch["observed"], f'{one} AND [level_2] = "observed"', [_line("observed", s["observed"], c.MUTED)],
           IMF_TALL)
    p.keep()
    for k in catalog._imf_labels(checked):
        _panel(p, w, f"imf_{k}", ch["imf"].replace("{k}", k), f'{one} AND [level_2] = "imf_{k}"',
               [_line(f"imf_{k}", s["imf"], c.ACCENT)], IMF_TALL, {"target": 0}, collapse=True)
    _panel(p, w, "residue", ch["residue"], f'{one} AND [level_2] = "residue"', [_line("residue", s["residue"], c.ACCENT)], IMF_TALL)
    rows = '[section] = "imf" AND [stat] = "variance_share"'
    mode = f'IF([level] = "residue", {c.q(w["modes"]["residue"])}, {c.join(w["modes"]["imf"], k="MID([level], 5, 3)")})'

    def number(header: str, stat: str, width: float, pattern: str) -> dict:
        x = c.of_row("imf", stat)
        return {"header": header, "value": f'IF(ISNULL({x}), "", {c.shown(x, pattern)})', "width": width, "align": "right"}

    def share(header: str, stat: str) -> dict:
        x = c.of_row("imf", stat)
        return {"header": header, "value": f'IF(ISNULL({x}), "", {c.pct(x, "0.#")})', "width": 20, "align": "right"}

    key = 'LOOKUP("assumption|imf_condition|" & [level] & "|verdict", [key], [text])'
    verdict = "IFS(" + ", ".join(f'{key} = "{v}", {c.q(w["verdicts"][v])}' for v in catalog.VERDICTS) + ', "")'
    p.add(stats.table("imfs", t["imfs"], rows, [
        {"header": t["mode"], "value": mode, "style": {"weight": "semibold"}},
        number(t["mean_period"], "mean_period", 24, "#,##0.##"), share(t["variance_share"], "variance_share"),
        share(t["energy_share"], "energy_share"), number(t["extrema"], "extrema", 20, "#,##0"),
        number(t["zero_crossings"], "zero_crossings", 26, "#,##0"),
        {"header": t["imf_condition"], "value": verdict, "width": 26,
         "rules": [{"when": f'{key} = "{v}"', "style": {"color": colour, "weight": "semibold"}} for v, colour in c.VERDICT_COLOURS.items()]},
    ], p.y, visible=c.exists(rows)), 10)
    c.reading_under(p, w, "imfs")


# ── the page ────────────────────────────────────────────────────────────────
def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    from app.reports import formats

    lang = c.code(locale)
    checked = _checked(params)
    method = catalog.variant(model, checked)
    w = c.words(model, method, lang)
    periods = periods_of(checked)
    header, footer = stats.bands(name(params, lang), " · ".join(x for x in (about(params, lang), step) if x), w,
                                 formats.locale(lang).texts, stats.sample_line(sample, lang))
    p = stats.Page()
    _summary(p, w, lang, params, checked, periods)
    if method != "emd":
        _components(p, w, checked, periods)
    if periods:
        _seasons(p, w, checked, periods)
    _remainder(p, w, checked)
    if method == "emd":
        _oscillations(p, w, checked)
    c.how_computed(p, w)
    c._fit(p, w, False, ("first", "last"))
    # the periods are on the cards and in the formula line; «7,30» among the settings would read as a decimal in four languages
    settings = next(it for it in p.items if it["id"] == "settings")
    settings["filter"] += ' AND [stat] <> "periods"'
    settings["visible"] = c.exists(settings["filter"])
    c._notes(p, w)
    return c.document(header, p.items, footer, lang, _calculated(w))


register("decomposition", layout, name, about)
