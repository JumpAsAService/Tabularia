"""The report of the node «Forecast (Timestral)» (docs/specs/foundation-forecast.md, «The report (pixel perfect)»): the
forecast of one series or of many by a pre-trained model, its bands, its errors on the values kept out (the holdout)
against the seasonal naïve forecast, the one test that compares the two across the series, and the licence of the
weights.

1. Summary: four cards (the series and their points, the model and its CPU, the horizon and the holdout, the holdout's
   MASE beside the seasonal naïve's), the formula line, the licence line, the summary's reading, the check (Wilcoxon's
   signed-rank test across the series: the only verdict), the measures under «Measures, not tests» and the mean errors
   over the series, the readings of the holdout and of the comparison.
2. Forecasts: one chart a series drawn (at most `max_series_charts`, the runner's choice): the history, the 90 % and the
   80 % bands, the median, the seasonal naïve, a dashed line at the first instant forecast; the series' reading under it.
3. The holdout (when one was kept): two charts a row, the values observed and the forecast made before them with its
   bands; the table of every series; the relative MASE of the series drawn with a line at 1; the coverage's reading.
4. What the model cannot know: the reading of the limits and the columns the model read besides the series.
5. How this report was computed (`common.how_computed`), the numbers of the forecast, the notes.
6. The licence of the weights, last: its reading, the weights used (repository, revision, sha256) and the licence's text
   verbatim (`licence_text` of the words: the file LICENSE of the weights, never translated).

The layout does not know the series' labels (a definition is made from the node's settings, not from the rows): the
charts are slots, the k-th showing the k-th series the runner drew, found by calculated fields (`drawn_<k>`: the label
whose `median` line comes k-th in the rows). A slot with no series takes no room (`collapse_hidden`). The time's axis
follows the runner's `time_kind`, as ARIMA's and the decomposition's: each chart along time is drawn twice in the same
place, as dates and as steps, one visible. Every number is read from the runner's rows; the explanation is the
runner's (`reading` rows, said by `common.with_labels`), placed by `common.reading_under` and `common.how_computed`
(docs/specs/report-explanations.md). The words are in `report_texts/timestral.json`.
"""
from __future__ import annotations

from typing import Any

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import register
from app.engine.stats import layouts as stats

WORDS = "timestral"
SLOTS = int(catalog.LIMITS["max_series_charts"])  # the series the runner draws at most
FORECAST_TALL, HOLDOUT_TALL, NAME_TALL = 74.0, 52.0, 5.5
CURVE = '[section] = "series_curve"'
FORECAST_LINES = ("observed", "median", "baseline", "q0.05", "q0.1", "q0.9", "q0.95")
HOLDOUT_LINES = ("observed", "holdout_median", "holdout_baseline", "holdout_q0.05", "holdout_q0.1", "holdout_q0.9", "holdout_q0.95")
MEASURES = ("relative_mase", "coverage_80", "coverage_90")  # the assumption rows that are measures
ACROSS = ("mase", "baseline_mase", "smape", "baseline_smape", "wql", "n_measured")  # the model's numbers beside them
# what the means leave out, counted by the runner (lot F9): a row each under the mean errors, only when not 0
LEFT_OUT = ("n_smape_zero", "n_baseline_smape_zero", "n_relative_left_out")
WEIGHTS = ("weights", "weights_sha256", "licence", "licence_sha256")  # the weights' part of the last page
# the numbers the fit's table leaves out: the measures' tables say them, with what they are
FIT_SKIP = ("mase", "baseline_mase", "relative_mase_geomean", "smape", "baseline_smape", "wql", "coverage_80", "coverage_90",
            *LEFT_OUT)
BAND = "#3b5bdb"  # the bands: the accent, light (the engine's band opacity), darker where the 80 % lies over the 90 %
NAIVE = c.MID  # the seasonal naïve: the amber of what is compared, not of a verdict


def _checked(params: dict[str, Any]) -> dict[str, Any]:
    return catalog.check_params(WORDS, dict(params))


def _size(checked: dict[str, Any]) -> str:
    size = str(checked.get("size") or "28m")
    return c.words(WORDS, "default", "en")["values"]["size"].get(size, size)


def name(params: dict[str, Any], locale: str) -> str:
    """«Forecast (Timestral-28M)»."""
    return c.words(WORDS, "default", locale)["name"].replace("{model}", _size(_checked(params)))


def about(params: dict[str, Any], locale: str) -> str:
    """«orders» over «day» by «channel», daily series: the columns named as columns, the frequency in the page's words."""
    w = c.words(WORDS, "default", locale)
    value, time, series = params.get("value"), params.get("time"), params.get("series")
    if not (value and time):
        return str(value or "")
    by = w["by"].replace("{series}", str(series)) if series else ""
    frequency = w["frequency_series"].get(str(params.get("frequency")), "")
    said = w["series_of"].replace("{value}", str(value)).replace("{time}", str(time)).replace("{by}", by)
    return said.replace("{frequency}", frequency) if frequency else said.rsplit(",", 1)[0]


def _numbers(sentence: str, lang: str, **values: float) -> str:
    """A static sentence with the node's numbers in it, each in the language's decimals («α = 0,05»)."""
    from app.reports import formats

    loc = formats.locale(lang)
    for k, v in values.items():
        sentence = sentence.replace("{" + k + "}", formats.format_number(float(v), "0.####", loc))
    return sentence


def _unit(w: dict, checked: dict[str, Any]) -> str:
    return w["units"].get(str(checked.get("frequency")), w["units"]["index"])


# ── the series drawn: slots ─────────────────────────────────────────────────
def _calculated() -> list[dict]:
    """`drawn_<k>`: the label of the k-th series the runner drew (the k-th whose `median` line starts in the rows), none
    beyond the last; `drawn_ord_<k>`: where its line starts. An aggregate in a calculated field reads the whole dataset."""
    out: list[dict] = []
    for k in range(1, SLOTS + 1):
        others = "".join(f" AND [level] <> [drawn_{j}]" for j in range(1, k))
        out.append({"name": f"drawn_ord_{k}", "expr": f'MIN(IF({CURVE} AND [level_2] = "median"{others}, [ord], NULL))'})
        out.append({"name": f"drawn_{k}", "expr": f"LOOKUP([drawn_ord_{k}], [ord], [level])"})
    return out


def _has(k: int) -> str:
    return f"COUNTIF({CURVE} AND [level] = [drawn_{k}]) > 0"


def _of(k: int, lines: tuple[str, ...]) -> str:
    return f'{CURVE} AND [level] = [drawn_{k}] AND (' + " OR ".join(f'[level_2] = "{x}"' for x in lines) + ")"


def _line(level_2: str, name_: str, colour: str | None = None, as_: str = "line") -> dict:
    s: dict[str, Any] = {"value": f'IF([level_2] = "{level_2}", [value], NULL)', "as": as_, "name": name_}
    if colour:
        s["color"] = colour
    return s


def _bands(w: dict, prefix: str = "") -> list[dict]:
    """The 90 % band, then the 80 % band over it (a band is two series: its low edge, then its high edge)."""
    s = w["series"]
    return [_line(prefix + "q0.05", s["band90"], BAND, "band_low"), _line(prefix + "q0.95", "", BAND, "band_high"),
            _line(prefix + "q0.1", s["band80"], BAND, "band_low"), _line(prefix + "q0.9", "", BAND, "band_high")]


def _integer_time(w: dict) -> str:
    """True when the runner says the time is an integer (its word in the page's language: `with_labels` says a setting's
    value in it)."""
    return f'COUNTIF([key] = "model|||time_kind" AND [text] = {c.q(w["values"]["time_kind"]["integer"])}) > 0'


def _twice(p: stats.Page, item: dict, w: dict) -> None:
    """A chart along time, as dates and as integers, in the same place: one is visible; neither takes room without its
    series."""
    shown = item["visible"].lstrip("=")
    integer = _integer_time(w)
    for as_date in (True, False):
        p.add({**item, "id": item["id"] + ("" if as_date else "_steps"), "options": {**item["options"], "x_time": as_date},
               "visible": f"={shown} AND " + (f"NOT ({integer})" if as_date else integer), "collapse_hidden": True})


def _name(p: stats.Page, w: dict, id_: str, k: int, x: float, width: float) -> None:
    """The series' label over its chart."""
    said = c.join(w["series_name"], series=f"FIRST([drawn_{k}])")
    p.text(id_, "=" + said, h=NAME_TALL, x=x, w=width, advance=False, size=9.5, weight="semibold", visible="=" + _has(k))
    p.items[-1].update({"dataset": c.DATASET, "collapse_hidden": True})


def _start(k: int, level_2: str) -> str:
    """The first instant of a line of the k-th series drawn: where the dashed line goes."""
    return f'MIN(IF({CURVE} AND [level] = [drawn_{k}] AND [level_2] = "{level_2}", [x0], NULL))'


# ── page 1: the summary ─────────────────────────────────────────────────────
def _card(p: stats.Page, id_: str, x: float, label: str, value: str, under: str | None, fmt: str | None = None) -> None:
    item: dict[str, Any] = {"id": id_, "type": "kpi", "dataset": c.DATASET, "x": x, "y": p.y, "w": 45, "h": 20, "label": label,
                            "value": value}
    if fmt:
        item["format"] = fmt
    p.add(item)
    if under:
        p.text(id_ + "_under", "=" + under, h=4.5, x=x + 2, w=43, advance=False, size=7.5, color=c.MUTED)
        p.items[-1].update({"y": p.y + 14.6, "dataset": c.DATASET})


def _cards(p: stats.Page, w: dict, checked: dict[str, Any], holdout: bool) -> None:
    k = w["kpi"]
    n = c.model_number
    _card(p, "k_series", 0, k["series"], n("n_series"), c.join(k["points"], n=c.shown(n("n_points"), "#,##0")), "#,##0")
    cpu = c.model_text("cpu_capability")
    _card(p, "k_model", 47, k["model"], c.q(_size(checked)),
          c.join(k["cpu"], cpu=f'COALESCE({cpu}, "")', threads=c.shown(n("threads"), "0")))
    horizon = c.join(k["horizon_value"], h=c.shown(n("horizon"), "#,##0"), unit=c.q(_unit(w, checked)))
    kept = (c.join(k["holdout"], k=c.shown(n("holdout"), "#,##0")) if holdout else c.q(k["no_holdout"]))
    _card(p, "k_horizon", 94, k["horizon"], horizon, kept)
    mase, naive = n("mase"), n("baseline_mase")
    value = f'IF(ISNULL({mase}), "–", {c.shown(mase, "0.00")})'
    under = f'IF(ISNULL({naive}), {c.q(k["not_measured"])}, {c.join(k["mase_under"], b=c.shown(naive, "0.00"))})'
    _card(p, "k_mase", 141, k["mase"], value, under)
    p.y += 21.5


def _formula(w: dict, params: dict[str, Any], checked: dict[str, Any]) -> str:
    """«orders ~ Timestral-28M · 3 series by «channel» · daily series · 14 days ahead», in the page's words: the runner's
    `formula` is English, this line is the page's (its count of series is the runner's number)."""
    frequency = w["frequency_series"].get(str(checked.get("frequency")), "")
    parts = {"value": c.q(str(params.get("value") or "")), "model": c.q(_size(checked)), "frequency": c.q(frequency),
             "horizon": c.shown(c.model_number("horizon"), "#,##0"), "unit": c.q(_unit(w, checked))}
    if params.get("series"):
        return c.join(w["formula_many"], n=c.shown(c.model_number("n_series"), "#,##0"), series=c.q(str(params["series"])), **parts)
    return c.join(w["formula_one"], **parts)


def _verdict_words(item: dict, w: dict, verdict: str = "[text]") -> None:
    """The comparison says its verdict in its own words: not rejecting H0 is «not supported» (muted, never the red of a
    broken assumption), a rejection names the forecast ahead by the side of the test (the rank sums; the median of the
    differences, the row's value, when the runner did not write it).
    `verdict`: the expression of the row's verdict (the row's own text in the checks' table, a LOOKUP in the advice's)."""
    # the side is the test's: the sign of the rank-biserial correlation (W+ − W−) / (W+ + W−), which the runner writes
    # since lot F9; a result without it (older) says the median's sign
    side = c.of_row("assumption", "rank_biserial")
    own, value = w["comparison_verdicts"], f'IF(ISNULL({side}), {c.of_row("assumption", "value")}, {side})'

    header = w["assumptions"]["verdict"]
    mine = '[level] = "baseline_comparison"'
    for col in item["columns"]:
        if col["header"] != header:
            continue
        col["value"] = (f'IFS({mine} AND {verdict} = "ok", {c.q(own["ok"])}, '
                        f'{mine} AND {verdict} = "failed" AND {value} < 0, {c.q(own["ahead"])}, '
                        f'{mine} AND {verdict} = "failed", {c.q(own["behind"])}, TRUE, {col["value"]})')
        col.setdefault("rules", []).extend([
            {"when": f'{mine} AND {verdict} = "ok"', "style": {"color": c.MUTED, "weight": "normal"}},
            {"when": f'{mine} AND {verdict} = "failed"', "style": {"color": c.ACCENT, "weight": "semibold"}}])


def _across(p: stats.Page, w: dict) -> None:
    """The mean errors over the series (the model's numbers), each with what it is: measures, no verdict."""
    a = w["across"]
    left_out = " OR ".join(f'[stat] = "{s}"' for s in LEFT_OUT)
    one = ('[section] = "model" AND NOT ISNULL([value]) AND ((' + " OR ".join(f'[stat] = "{s}"' for s in ACROSS) + ")"
           + f" OR (({left_out}) AND [value] > 0))")
    shown = (f'IFS([stat] = "n_measured" OR {left_out}, FORMAT([value], "#,##0"), '
             f'[stat] = "smape" OR [stat] = "baseline_smape", FORMAT([value], "0.##") & " %", TRUE, FORMAT([value], "0.####"))')
    what = "IFS(" + ", ".join(f'[stat] = "{s}", {c.q(w["across_texts"][s])}' for s in (*ACROSS, *LEFT_OUT)) + ', TRUE, "")'
    p.add(stats.table("across", a["title"], one, [
        {"header": a["measure"], "value": "[label]", "width": 52},
        {"header": a["value"], "value": shown, "width": 24, "align": "right"},
        {"header": a["what"], "value": what, "style": {"size": 7.5}},
    ], p.y, visible=c.exists(one)), 10)


def clause(text: str) -> str:
    """Point 2 of the licence, word for word: the line of the vendored text that starts with «2.», found in it and never
    retyped. The report quotes it and never explains it: the official file is the authority for its meaning."""
    return next(ln.strip()[2:].strip() for ln in text.split("\n") if ln.strip().startswith("2. "))


def _summary(p: stats.Page, w: dict, lang: str, params: dict[str, Any], checked: dict[str, Any], holdout: bool) -> None:
    p.heading("s_summary", w["sections"]["summary"])
    _cards(p, w, checked, holdout)
    p.keep()
    p.text("formula", "=" + _formula(w, params, checked), h=6.5, size=9.5, weight="semibold", background=c.PANEL, padding=1.5,
           can_grow=True)
    p.items[-1]["dataset"] = c.DATASET
    line = w["licence_line"].replace("{neutral}", w["licence_neutral"]).replace("{clause}", clause(w["licence_text"]))
    p.text("licence_line", stats.static(line.replace("{model}", _size(checked))), h=5, size=8, color=c.MUTED, can_grow=True)
    c.reading_under(p, w, "summary")
    c._assumptions(p, w, lang, _numbers(w["alpha_line"], lang, alpha=float(checked.get("alpha", 0.05))), True)
    # the test compares series, one difference each: «many rows» says nothing about it
    p.items[:] = [it for it in p.items if it["id"] != "many_rows"]
    _verdict_words(next(it for it in p.items if it["id"] == "assumptions"), w)
    _verdict_words(next(it for it in p.items if it["id"] == "advice"), w, c.of_row("assumption", "verdict", "text"))
    if holdout:
        c.measures(MEASURES)(p, w)
        _across(p, w)
    c.reading_under(p, w, "holdout", "comparison")


# ── page 2: the forecasts ───────────────────────────────────────────────────
def _forecasts(p: stats.Page, w: dict) -> None:
    s = w["series"]
    p.heading("s_forecasts", w["sections"]["forecasts"])
    p.text("forecast_how", w["captions"]["forecast"], h=5, size=8, color=c.MUTED, can_grow=True)
    for it in p.items[-3:]:  # the heading, its rule and the caption go to the page of the first chart
        it["keep_with_next"] = True
    for k in range(1, SLOTS + 1):
        top = p.y + 1.5
        p.y = top
        _name(p, w, f"forecast_name_{k}", k, 0, c.WIDTH)
        series = [*_bands(w), _line("observed", s["observed"], c.MUTED), _line("baseline", s["baseline"], NAIVE),
                  _line("median", s["median"], c.ACCENT)]
        _twice(p, {"id": f"forecast_{k}", "type": "chart", "dataset": c.DATASET, "kind": "xy", "x": 0, "y": top + NAME_TALL,
                   "w": c.WIDTH, "h": FORECAST_TALL, "title": stats.fit(w["charts"]["forecast"], 7.5, c.WIDTH - 4, 700),
                   "filter": _of(k, FORECAST_LINES), "category": "[x0]", "series": series,
                   "options": {"format": "0.###", "legend": True, "target_x": _start(k, "median")}, "visible": "=" + _has(k)}, w)
        p.y = top + NAME_TALL + FORECAST_TALL + 0.8
        reading = c.reading_of("forecast", f"FIRST([drawn_{k}])", 4)
        p.text(f"forecast_reading_{k}", "=" + c.q(w["reading"]["lead"] + " ") + f" & TRIM({reading})", h=5, size=8.5, italic=True,
               color=c.MUTED, can_grow=True, visible=f'={_has(k)} AND TRIM({reading}) <> ""')
        p.items[-1].update({"dataset": c.DATASET, "collapse_hidden": True})
        # the name, the chart and its reading are one row of the body: never split over two pages
        p.items.append({"id": f"forecast_keep_{k}", "type": "rect", "x": 0, "y": top, "w": 0.1, "h": round(p.y - top, 2),
                        "style": {}, "visible": "=" + _has(k), "collapse_hidden": True})


# ── page 3: the holdout ─────────────────────────────────────────────────────
def _holdout(p: stats.Page, w: dict) -> None:
    s, t = w["series"], w["tables"]
    p.heading("s_holdout", w["sections"]["holdout"])
    p.text("holdout_how", w["captions"]["holdout"], h=5, size=8, color=c.MUTED, can_grow=True)
    for it in p.items[-3:]:
        it["keep_with_next"] = True
    half, gap = 91.0, 4.0
    for row in range(0, SLOTS, 2):
        top = p.y + 1.5
        for column, k in enumerate((row + 1, row + 2)):
            x = column * (half + gap)
            # the last series of an odd count is alone on its row: drawn across the whole width, not at half of it (lot F9);
            # the two forms of the left slot are in the same place, one visible
            forms = [(half, f"={_has(k)} AND {_has(k + 1)}", ""), (c.WIDTH, f"={_has(k)} AND NOT ({_has(k + 1)})", "_alone")] \
                if column == 0 else [(half, "=" + _has(k), "")]
            for width, shown, tail in forms:
                p.y = top
                _name(p, w, f"holdout_name_{k}{tail}", k, x, width)
                p.items[-1]["visible"] = shown
                series = [*_bands(w, "holdout_"), _line("observed", s["observed"], c.MUTED),
                          _line("holdout_baseline", s["baseline"], NAIVE), _line("holdout_median", s["median"], c.ACCENT)]
                _twice(p, {"id": f"holdout_{k}{tail}", "type": "chart", "dataset": c.DATASET, "kind": "xy", "x": x,
                           "y": top + NAME_TALL, "w": width, "h": HOLDOUT_TALL,
                           "title": stats.fit(w["charts"]["holdout"], 7.5, width - 4, 700),
                           "filter": _of(k, HOLDOUT_LINES), "category": "[x0]", "series": series,
                           "options": {"format": "0.###", "legend": False, "target_x": _start(k, "holdout_median")},
                           "visible": shown}, w)
        p.items.append({"id": f"holdout_keep_{row // 2 + 1}", "type": "rect", "x": 0, "y": top, "w": 0.1,
                        "h": NAME_TALL + HOLDOUT_TALL, "style": {}, "visible": "=" + _has(row + 1), "collapse_hidden": True})
        p.y = top + NAME_TALL + HOLDOUT_TALL
    p.y += 3
    one = '[section] = "series" AND [stat] = "n"'

    def number(header: str, stat: str, width: float, pattern: str, suffix: str = "") -> dict:
        x = c.of_row("series", stat)
        said = c.shown(x, pattern) + (f' & "{suffix}"' if suffix else "")
        return {"header": header, "value": f'IF(ISNULL({x}), "", {said})', "width": width, "align": "right"}

    def share(header: str, stat: str) -> dict:
        x = c.of_row("series", stat)
        return {"header": header, "value": f'IF(ISNULL({x}), "", {c.pct(x, "0.#")})', "width": 15, "align": "right"}

    reason = c.of_row("series", "not_measured", "text")
    why = ("IFS(" + ", ".join(f'{reason} = "{k}", {c.q(v)}' for k, v in w["not_measured"].items() if k != "other")
           + f', NOT ISNULL({reason}) AND {reason} <> "", {c.q(w["not_measured"]["other"])}, TRUE, "")')
    p.add(stats.table("series", t["title"], one, [
        {"header": t["series"], "value": "[level]", "style": {"weight": "semibold"}},
        number(t["points"], "n", 15, "#,##0"), number(t["read"], "n_used", 15, "#,##0"),
        number(t["mase"], "mase", 14, "0.###"), number(t["baseline_mase"], "baseline_mase", 16, "0.###"),
        # «58,55 %» on one line: a no-break space before the sign, and the room for «199,99 %» (lot F9: the cell broke before «%»)
        number(t["relative"], "relative_mase", 17, "0.###"), number(t["smape"], "smape", 19, "0.##", "\u00a0%"),
        number(t["wql"], "wql", 16, "0.####"), share(t["coverage_80"], "coverage_80"), share(t["coverage_90"], "coverage_90"),
        {"header": t["note"], "value": why, "width": 22, "style": {"size": 7}},
    ], p.y, visible=c.exists(one)), 10)
    drawn = " OR ".join(f"[level] = [drawn_{k}]" for k in range(1, SLOTS + 1))
    f = f'[section] = "series" AND [stat] = "relative_mase" AND ({drawn})'
    relative = c.coef_interval("relative_mase", w["charts"]["relative"], ("relative_mase",) * 3, (s["relative"], "", ""), 1.0,
                               log=True, filter_=f)
    relative.more.update({"category": "[level]", "series": [{"value": "SUMIF([stat] = \"relative_mase\", [value])", "name": n}
                                                             for n in (s["relative"], "", "")],
                          "visible": c.exists(f)})
    relative.more["options"]["format"] = "0.##"
    # a value written beside its dot crosses the line at 1 when it is near 1 («1,01»): above the dot instead (lot F9)
    relative.more["options"]["labels_above"] = True
    c.grid(p, w, (relative._replace(wide=True),), None, tall=56.0)
    c.reading_under(p, w, "coverage")


# ── page 4: the limits ──────────────────────────────────────────────────────
def _heading(p: stats.Page, id_: str, title: str) -> None:
    """A heading and its rule that go to the next page with what follows them: never alone at the foot of a page."""
    p.heading(id_, title)
    for item in p.items[-2:]:
        item["keep_with_next"] = True


def _limits(p: stats.Page, w: dict, checked: dict[str, Any]) -> None:
    v = w["covariates"]
    _heading(p, "s_limits", w["sections"]["limits"])
    c.reading_under(p, w, "limits")
    known, past = list(checked.get("known_covariates") or []), list(checked.get("past_covariates") or [])
    lines = [v["title"] + ":"]
    if known:
        lines.append(v["known"].replace("{names}", ", ".join(f"«{x}»" for x in known)))
    if past:
        lines.append(v["past"].replace("{names}", ", ".join(f"«{x}»" for x in past)))
    if not (known or past):
        lines.append(v["none"])
    p.text("covariates", stats.static(" ".join(lines)), h=5, size=8.5, background=c.PANEL, padding=1.5, can_grow=True)


# ── page 6: the licence, last ───────────────────────────────────────────────
def _licence(p: stats.Page, w: dict) -> None:
    lw = w["licence"]
    _heading(p, "s_licence", w["sections"]["licence"])
    c.reading_under(p, w, "licence")
    weights = '[section] = "model" AND (' + " OR ".join(f'[stat] = "{s}"' for s in WEIGHTS) + ")"
    p.add(stats.table("weights", lw["weights_title"], weights, [
        {"header": lw["item"], "value": "[label]", "width": 50},
        {"header": lw["value"], "value": "[text]", "style": {"size": 7.5}},
    ], p.y, visible=c.exists(weights)), 10)
    # the text verbatim, in one cell of a table of one row (every result has its `model` row): the PDF's last part and the
    # workbook's sheet; the official English text in every language, never translated
    t = stats.table("licence_text", lw["text_title"], '[key] = "model|||model"', [
        {"header": lw["text_header"], "value": c.q(w["licence_text"]), "style": {"size": 8}},
    ], p.y)
    t["repeat_header"] = False
    p.add(t, 10)


# ── the page ────────────────────────────────────────────────────────────────
def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    from app.reports import formats

    lang = c.code(locale)
    checked = _checked(params)
    w = c.words(model, catalog.variant(model, checked), lang)
    holdout = checked.get("holdout") != 0  # None: the horizon's (the runner's rule)
    header, footer = stats.bands(name(params, lang), " · ".join(x for x in (about(params, lang), step) if x), w,
                                 formats.locale(lang).texts, stats.sample_line(sample, lang))
    p = stats.Page()
    _summary(p, w, lang, params, checked, holdout)
    _forecasts(p, w)
    if holdout:
        _holdout(p, w)
    _limits(p, w, checked)
    c.how_computed(p, w)
    c._fit(p, w, False, FIT_SKIP)
    # the weights and the licence are the last page's
    settings = next(it for it in p.items if it["id"] == "settings")
    settings["filter"] += "".join(f' AND [stat] <> "{s}"' for s in WEIGHTS)
    settings["visible"] = c.exists(settings["filter"])
    c._notes(p, w)
    _licence(p, w)
    return c.document(header, p.items, footer, lang, _calculated())


register("timestral", layout, name, about)
