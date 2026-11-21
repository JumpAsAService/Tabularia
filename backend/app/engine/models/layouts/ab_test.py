"""The report of the node «A/B test» (docs/specs/ab-testing.md, «The report (pixel perfect)»): the treatments
against the control, on one primary metric (binary, continuous or a ratio), with the split of the units (SRM), the
other checks, CUPED and the cap, the power at this size, the Bayesian measures and the always-valid p-values.

1. Decision: the band of a sample ratio mismatch when the SRM check failed; the headline, from verdicts only (the
   treatments' decisions, from their adjusted p-values against alpha); four cards; the formula line; the
   treatments' table; their lifts with their intervals; the checks with the alpha line; the measures, which no
   colour judges; the reading.
2. Variants: the units and their split, the metric per variant, the observed against the expected units, the daily
   share and the daily units, the histogram of a continuous metric, CUPED; the readings of the split, CUPED, the cap.
3. Comparisons: every number of each comparison, the power against the lift; the readings.
4. Bayesian measures (with `bayes`), 5. the sequential test (with `sequential`), 6. secondary metrics (with
`secondary_metrics`: every comparison of the guardrails, adjusted among themselves; those that came out worse are
listed on the first page too, under the treatments), 7. how this report was computed, 8. the notes.

Every number is read from the runner's rows (`results.py`'s sections `variant`, `comparison`, `bayes`, `sequential`,
`secondary`, `reading`, `assumption`, `curve`, `hist`); the words are in `report_texts/experiments.json` and
`ab_test.json`.
"""
from __future__ import annotations

from typing import Any

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import experiments as e
from app.engine.models.layouts import register
from app.engine.stats import layouts as stats

WORDS = "ab_test"
COMPARISON = '[section] = "comparison" AND [stat] = "lift"'
SECONDARY = '[section] = "secondary" AND [stat] = "decision"'  # one row per comparison of a secondary metric
SECONDARY_WORSE = SECONDARY + ' AND [text] = "worse"'
DECISION_COLOURS = {"better": c.GOOD, "worse": c.BAD}

# The dataset's calculated fields the page reads by: the stat of each row of the decided treatment's comparison
# (the headline's numbers: LOOKUP("lift", [decided], [value])), of the control's row, and the adjusted p-values (the
# cards: the treatment with the smallest one)
CALCULATED = [
    {"name": "decided", "expr": 'IF([section] = "comparison" AND [level] = REPLACE(REPLACE(REPLACE(LOOKUP("model|||decision_variants", '
                                '[key], [text]), "%2C", ","), "%20", " "), "%25", "%"), [stat], NULL)'},
    # the treatments of the decision as the headline says them («B, D»), on the row of decision_variants
    {"name": "said", "expr": f'IF([key] = "model|||decision_variants", {e.decoded("[text]")}, NULL)'},
    {"name": "of_control", "expr": 'IF([section] = "variant" AND [level] = LOOKUP("model|||control", [key], [text]), [stat], NULL)'},
    {"name": "best_p", "expr": 'IF([section] = "comparison" AND [stat] = "p_adjusted", [value], NULL)'},
]


def name(params: dict[str, Any], locale: str) -> str:
    return c.words(WORDS, "binary", locale)["name"]


def about(params: dict[str, Any], locale: str) -> str:
    """«converted by variant · control A»."""
    w = c.words(WORDS, "binary", locale)
    metric = e.metric_name(params)
    said = w["about"].replace("{metric}", metric).replace("{variant}", str(params.get("variant") or "")) if metric else ""
    if params.get("control") is not None and params.get("control") != "":
        said += (" · " if said else "") + w["about_control"].replace("{control}", str(params["control"]))
    return said


def _numbers(sentence: str, lang: str, **values: float) -> str:
    """A static sentence with the node's numbers in it, each in the language's decimals («α = 0,05»)."""
    from app.reports import formats

    loc = formats.locale(lang)
    for k, v in values.items():
        sentence = sentence.replace("{" + k + "}", formats.format_number(float(v), "0.####", loc))
    return sentence


# ── page 1: the decision ────────────────────────────────────────────────────
def _srm_band(p: stats.Page, w: dict) -> None:
    """A mismatch of the split is a fact to show, not to hide: a band of its own, first."""
    failed = f'={e.verdict_of("srm")} = "failed"'
    said = c.join(w["srm_band"], p=e.p_with_sign(c.num("assumption|srm||p_value")), srm_alpha=c.shown(e.model_number("srm_alpha"), "0.####"))
    # one millimetre high as designed: a band that is not drawn leaves no gap, one that is grows to its words
    p.text("srm_band", "=" + said, h=1.0, size=9.5, weight="semibold", color="#ffffff", background=c.BAD, padding=2.0,
           can_grow=True, visible=failed)
    p.y += 1.5


def _headline(w: dict, metric: str) -> list[tuple[str, str, str]]:
    """The decision in one sentence, from verdicts only: a treatment better, else worse, else no difference shown;
    «Sample ratio mismatch.» before it when the split failed its check. Five sentences at one place, one of them
    visible: (id, expression, when)."""
    h = w["headline"]
    count = 'COUNTIF([section] = "comparison" AND [stat] = "decision" AND [text] = "{0}")'
    better, worse = f'{count.format("better")} > 0', f'{count.format("worse")} > 0'
    variants = 'LOOKUP("model|||decision_variants", [key], [said])'
    one = f'(NOT CONTAINS({variants}, ",") AND {variants} <> "")'

    def decided(stat: str) -> str:
        return f'LOOKUP("{stat}", [decided], [value])'

    common = {"control": e.model_text("control"), "alpha": c.shown(e.model_number("alpha"), "0.####"), "metric": c.q(metric)}
    single = {**common, "variant": variants, "lift": e.signed_pct(decided("lift")),
              "interval": e.interval(decided("lift_ci_low"), decided("lift_ci_high")),
              "p": e.p_with_sign(decided("p_adjusted")), "correction": e.model_text("correction")}
    mde = 'MIN(IF([section] = "comparison" AND [stat] = "mde", [value], NULL))'
    srm = f'IF({e.verdict_of("srm")} = "failed", {c.q(h["srm"] + " ")}, "") & '
    return [
        ("headline_better", srm + c.join(h["better_one"], **single), f"{better} AND {one}"),
        ("headline_better_many", srm + c.join(h["better_many"], **common, variants=variants), f"{better} AND NOT {one}"),
        ("headline_worse", srm + c.join(h["worse_one"], **single), f"NOT ({better}) AND {worse} AND {one}"),
        ("headline_worse_many", srm + c.join(h["worse_many"], **common, variants=variants), f"NOT ({better}) AND {worse} AND NOT {one}"),
        ("headline", srm + c.join(h["no_difference"], **common, mde=e.pct(mde, "0.#"), power=e.pct(e.model_number("power"), "0")),
         f"NOT ({better}) AND NOT ({worse})"),
    ]


def _card(p: stats.Page, id_: str, x: float, label: str, value: str, under: str | None = None, fmt: str | None = None) -> None:
    item: dict[str, Any] = {"id": id_, "type": "kpi", "dataset": c.DATASET, "x": x, "y": p.y, "w": 45, "h": 20, "label": label,
                            "value": value}
    if fmt:
        item["format"] = fmt
    p.add(item)
    if under:
        p.text(id_ + "_under", "=" + under, h=4.5, x=x + 2, w=43, advance=False, size=7.5, color=c.MUTED)
        p.items[-1].update({"y": p.y + 14.6, "dataset": c.DATASET})


def _cards(p: stats.Page, w: dict, kind: str) -> None:
    """The units; the control's metric; the lift of the treatment with the smallest adjusted p-value, its interval;
    that p-value."""
    k = w["kpi"]
    _card(p, "k_units", 0, k["units"], e.model_number("n"), c.join(k["units_under"], assigned=c.shown(e.model_number("n_assigned"), "#,##0"),
                                                                  variants=c.shown(e.model_number("n_variants"), "0")), "#,##0")
    mean = 'LOOKUP("mean", [of_control], [value])'
    control_value = e.pct(mean, "0.##") if kind == "binary" else c.shown(mean, "#,##0.####")
    _card(p, "k_control", 47, k["control"][kind], control_value,
          c.join(k["control_under"], control=e.model_text("control"), units=c.shown('LOOKUP("n", [of_control], [value])', "#,##0")))
    strongest = "LOOKUP(MIN([best_p]), [best_p], [level])"

    def of_strongest(stat: str) -> str:
        return (f'LOOKUP("comparison|" & {e.key_part(strongest)} & "|" & {e.key_part(e.model_text("control"))} & "|{stat}", '
                f'[key], [value])')

    _card(p, "k_lift", 94, k["lift"], e.signed_pct(of_strongest("lift")),
          c.join(k["lift_under"], variant=strongest, interval=e.interval(of_strongest("lift_ci_low"), of_strongest("lift_ci_high"))))
    _card(p, "k_p", 141, k["p"], c.p_said(of_strongest("p_adjusted")),
          c.join(k["p_under"], variant=strongest, correction=e.model_text("correction")))
    p.y += 21.5


def _formula_line(w: dict, params: dict[str, Any], kind: str, checked: dict[str, Any], lang: str) -> str:
    """What was compared, said from the node's settings: ««converted»: conversion rate per user_id · control «A» ·
    Holm · CUPED on «revenue_pre» · α = 0.05»."""
    f = w["formula_line"]
    unit = str(params.get("unit") or f["row"])
    parts = [f[kind].replace("{metric}", str(params.get("metric") or params.get("metric_sum") or ""))
             .replace("{numerator}", str(params.get("numerator") or "")).replace("{denominator}", str(params.get("denominator") or ""))
             .replace("{unit}", unit),
             f["control"].replace("{control}", str(params.get("control", ""))),
             w["values"]["correction"].get(str(checked.get("correction")), str(checked.get("correction")))]
    if params.get("covariate"):
        parts.append(f["cuped"].replace("{covariate}", str(params["covariate"])))
    if params.get("cap_percentile"):
        parts.append(_numbers(f["cap"], lang, percentile=float(params["cap_percentile"]) * 100))
    parts.append(_numbers(f["alpha"], lang, alpha=checked.get("alpha", 0.05)))
    return " · ".join(parts)


def _difference(w: dict, kind: str, x: str) -> str:
    """A difference of the metric: in percentage points for a rate, else in the metric's own unit."""
    sign = f'IF({x} < 0, "−", IF({x} > 0, "+", ""))'
    if kind == "binary":
        return f'IF(ISNULL({x}), "", {sign} & FORMAT(ABS({x}) * 100, "0.##") & " " & {c.q(w["tables"]["points"])})'
    return f'IF(ISNULL({x}), "", {sign} & FORMAT(ABS({x}), "#,##0.####"))'


def _mean(kind: str, x: str) -> str:
    return e.pct(x, "0.##") if kind == "binary" else c.shown(x, "#,##0.####")


def _decision_column(w: dict, header: str, x: str) -> dict:
    return {"header": header, "value": e.said_by(w["decisions"], x), "width": 26,
            "rules": [{"when": f'{x} = "{d}"', "style": {"color": colour, "weight": "semibold"}} for d, colour in DECISION_COLOURS.items()]}


def _p_column(header: str, stat: str, stars: str, width: float = 22) -> dict:
    return {"header": header, "value": stats.with_stars(c.p_said(e.of_comparison(stat)), e.of_comparison(stars, "text")),
            "width": width, "align": "right"}


def _treatments(p: stats.Page, w: dict, kind: str) -> None:
    t = w["tables"]
    cols = [
        {"header": t["variant"], "value": "[level]", "width": 18, "style": {"weight": "semibold"}},
        {"header": t["units"], "value": e.of_variant("n"), "format": "#,##0", "width": 17, "align": "right"},
        {"header": t["mean"][kind], "value": _mean(kind, e.of_variant("mean")), "width": 21, "align": "right"},
        {"header": t["difference"], "value": _difference(w, kind, e.of_comparison("difference")), "width": 21, "align": "right"},
        {"header": t["lift_interval"], "value": f'{e.signed_pct("[value]")} & " " & {e.interval(e.of_comparison("lift_ci_low"), e.of_comparison("lift_ci_high"))}'},
        _p_column(t["p"], "p_value", "significance", 19),
        _p_column(t["p_adjusted"], "p_adjusted", "p_adjusted_significance", 21),
        _decision_column(w, t["decision"], e.of_comparison("decision", "text")),
    ]
    p.add(stats.table("treatments", t["treatments"], COMPARISON, cols, p.y, visible=c.exists(COMPARISON)), 10)


def _lifts(p: stats.Page, w: dict, lang: str, checked: dict[str, Any]) -> None:
    ch = w["charts"]
    more = {"filter": '[section] = "comparison"', "category": "[level]",
            "options": {"labels": True, "format": "0.#%", "target": 0},
            "series": [{"value": f'SUMIF([stat] = "{s}", [value])', "name": n}
                       for s, n in (("lift", w["series"]["lift"]), ("lift_ci_low", w["series"]["low"]), ("lift_ci_high", w["series"]["high"]))],
            "visible": c.exists(COMPARISON)}
    caption = _numbers(ch["lift_caption"], lang, min_lift=float(checked.get("min_lift", 0.02)) * 100)
    c.grid(p, w, (c.Chart("lift_interval", "interval", ch["lift_interval"], more, wide=True, caption=caption),), None, tall=52.0)


def _intervals_note(w: dict, checked: dict[str, Any], lang: str) -> str:
    said = w["intervals_note"].get(str(checked.get("correction")), "")
    return _numbers(said, lang, conf=(1 - float(checked.get("alpha", 0.05))) * 100)


def _measures(p: stats.Page, w: dict, checked: dict[str, Any]) -> None:
    """P(beats the control), the expected loss and where it stands against the threshold, the always-valid p-value:
    numbers, no colour."""
    m = w["measures_table"]
    bayes, sequential = bool(checked.get("bayes")), bool(checked.get("sequential"))
    if not (bayes or sequential):
        return
    one = ('[section] = "bayes" AND [stat] = "prob_beat_control"' if bayes else '[section] = "sequential" AND [stat] = "p_value"')
    cols: list[dict] = [{"header": w["tables"]["variant"], "value": "[level]", "width": 18, "style": {"weight": "semibold"}}]
    if bayes:
        cols += [{"header": m["prob_beat"], "value": e.pct(e.of_level("bayes", "prob_beat_control"), "0.#"), "width": 26, "align": "right"},
                 {"header": m["loss"], "value": e.pct(e.of_level("bayes", "expected_loss_relative"), "0.###"), "width": 26, "align": "right"},
                 {"header": m["threshold"], "value": e.said_by(w["loss_position"], e.of_level("bayes", "loss_position", "text"))}]
    if sequential:
        cols.append({"header": m["sequential_p"], "value": c.p_said(e.of_level("sequential", "p_adjusted")), "width": 30, "align": "right"})
    p.y += 1.0
    p.text("measures_title", m["title"], h=5, size=9, weight="semibold", visible=c.exists(one))
    p.add(stats.table("measures", m["title"], one, cols, p.y, visible=c.exists(one)), 10)
    p.text("measures_note", w["measures_note"], h=4.5, size=7, color=c.MUTED, can_grow=True, visible=c.exists(one))


def _decision(p: stats.Page, w: dict, lang: str, params: dict[str, Any], checked: dict[str, Any], kind: str) -> None:
    p.heading("s_decision", w["sections"]["decision"])
    _srm_band(p, w)
    for id_, said, when in _headline(w, e.metric_name(params)):
        p.text(id_, "=" + said, h=7, size=12, weight="bold", can_grow=True, advance=False, visible="=" + when)
        p.items[-1]["dataset"] = c.DATASET
    p.y += 8.5  # no `keep` over the band and the headline: one band of items would not move the headline under a band that grows
    _cards(p, w, kind)
    p.text("formula", stats.static(_formula_line(w, params, kind, checked, lang)), h=6.5, size=9, weight="semibold",
           background=c.PANEL, padding=1.5, can_grow=True)
    p.y += 1.0
    _treatments(p, w, kind)
    p.text("intervals_note", _intervals_note(w, checked, lang) or " ", h=4.5, size=7, color=c.MUTED, can_grow=True)
    if checked.get("secondary_metrics"):
        _secondary_worse(p, w)
    _lifts(p, w, lang, checked)
    alpha_line = _numbers(w["alpha_line"], lang, alpha=checked.get("alpha", 0.05), srm_alpha=checked.get("srm_alpha", 0.001))
    c._assumptions(p, w, lang, alpha_line, True)
    _measures(p, w, checked)
    e.reading_under(p, w, "decision")


# ── page 2: the variants ────────────────────────────────────────────────────
def _variants(p: stats.Page, w: dict, kind: str, checked: dict[str, Any]) -> None:
    t = w["tables"]
    p.heading("s_variants", w["sections"]["variants"])
    one = '[section] = "variant" AND [stat] = "n"'
    control = f'IF({e.of_variant("is_control")} = 1, " (" & {c.q(t["control"])} & ")", "")'
    name_col = {"header": t["variant"], "value": f"[level] & {control}", "style": {"weight": "semibold"}}  # the rest of the width

    def count(header: str, stat: str, width: float = 20) -> dict:
        return {"header": header, "value": e.of_variant(stat), "format": "#,##0", "width": width, "align": "right"}

    def share(header: str, stat: str) -> dict:
        return {"header": header, "value": e.pct(e.of_variant(stat), "0.##"), "width": 22, "align": "right"}

    cols = [name_col, count(t["assigned"], "n_assigned"), count(t["used"], "n"), count(t["missing"], "n_missing"),
            share(t["share"], "share"), share(t["expected_share"], "expected_share")]
    if kind == "binary":
        cols.append(count(t["conversions"], "conversions", 22))
    if checked.get("covariate"):
        cols.append(count(t["filled"] if checked.get("covariate_missing", "fill") == "fill" else t["excluded"],
                          "n_covariate_filled" if checked.get("covariate_missing", "fill") == "fill" else "n_covariate_excluded", 22))
    if kind == "continuous" and checked.get("cap_percentile"):
        cols.append(count(t["capped"], "n_capped", 20))
    p.add(stats.table("variants", t["variants"], one, cols, p.y, visible=c.exists(one)), 10)

    def number(header: str, stat: str, width: float = 22) -> dict:
        x = e.of_variant(stat)
        return {"header": header, "value": f'IF(ISNULL({x}), "", {_mean(kind, x) if stat in ("mean", "ci_low", "ci_high") else c.shown(x, "#,##0.####")})',
                "width": width, "align": "right"}

    low, high = e.of_variant("ci_low"), e.of_variant("ci_high")
    cols = [name_col, number(t["mean"][kind], "mean", 24), number(t["std_dev"], "std_dev"), number(t["std_error"], "std_error"),
            {"header": t["ci"], "value": f'"[" & {_mean(kind, low)} & ", " & {_mean(kind, high)} & "]"', "width": 36, "align": "right"}]
    if kind == "continuous":
        cols.append(number(t["skewness"], "skewness", 20))
    if kind == "ratio":
        cols += [number(t["numerator_sum"], "numerator_sum", 24), number(t["denominator_sum"], "denominator_sum", 24)]
    p.add(stats.table("variant_metric", t["variant_metric"], one, cols, p.y, visible=c.exists(one)), 10)

    ch, s = w["charts"], w["series"]
    split = '[section] = "variant" AND ([stat] = "n_assigned" OR [stat] = "expected_share")'
    units = c.Chart("srm_units", "column", ch["srm_units"], {
        "filter": split, "category": "[level]",
        "series": [{"value": 'SUMIF([stat] = "n_assigned", [value])', "name": s["observed"]},
                   {"value": f'SUMIF([stat] = "expected_share", [value]) * {e.model_number("n_assigned")}', "name": s["expected"]}],
        "options": {"legend": True, "format": "#,##0"}, "visible": c.exists(split)}, under="srm")
    shares = c.xy("srm_share", ch["srm_share"], '[section] = "curve" AND [level] = "srm_share"', [c.mark("y", "line")],
                  {"x_time": True, "legend": True, "format": "0.##%"}, under="srm", split_by="[level_2]")
    daily = c.xy("daily_units", ch["daily_units"], '[section] = "curve" AND [level] = "daily_units"', [c.mark("y", "line")],
                 {"x_time": True, "legend": True, "format": "#,##0"}, split_by="[level_2]")
    charts = [units, shares, daily]
    if kind == "continuous":
        charts.append(c.histogram("histogram", ch["histogram"], "metric", s["units"], split=True))
    c.grid(p, w, tuple(charts), None)
    e.reading_under(p, w, "srm")
    if checked.get("covariate"):
        _cuped(p, w, kind, checked)
    if kind == "continuous" and checked.get("cap_percentile"):
        e.reading_under(p, w, "cap")


def _cuped(p: stats.Page, w: dict, kind: str, checked: dict[str, Any]) -> None:
    """θ and ρ; for each variant the covariate's mean, the value an empty one was filled with (the mean of its own
    variant) or the units left out, the metric before and after, the variance it took away."""
    t, k = w["tables"], w["cuped"]
    p.y += 1.5
    p.text("cuped_line", "=" + c.join(k["line"], covariate=c.q(str(checked["covariate"])),
                                      theta=c.shown(e.model_number("theta"), "0.####"),
                                      rho=c.shown(e.model_number("covariate_correlation"), "0.###")),
           h=6, size=9, background=c.PANEL, padding=1.5, can_grow=True, visible=f'=NOT ISNULL({e.model_number("theta")})')
    p.y += 1.0
    one = '[section] = "variant" AND [stat] = "variance_reduction"'

    def number(header: str, stat: str, width: float = 22, form: str = "number") -> dict:
        x = e.of_variant(stat)
        said = e.pct(x, "0.#") if form == "pct" else _mean(kind, x) if form == "metric" else c.shown(x, "#,##0.####")
        return {"header": header, "value": f'IF(ISNULL({x}), "", {said})', "width": width, "align": "right"}

    cols = [{"header": t["variant"], "value": "[level]", "width": 18, "style": {"weight": "semibold"}},
            number(t["covariate_mean"], "covariate_mean", 24)]
    if checked.get("covariate_missing", "fill") == "fill":
        cols += [number(t["fill_value"], "covariate_fill_value", 24),
                 {"header": t["filled"], "value": e.of_variant("n_covariate_filled"), "format": "#,##0", "width": 18, "align": "right"}]
    else:
        cols.append({"header": t["excluded"], "value": e.of_variant("n_covariate_excluded"), "format": "#,##0", "width": 20, "align": "right"})
    cols += [number(t["mean_unadjusted"], "mean_unadjusted", 24, "metric"), number(t["mean_adjusted"], "mean", 24, "metric"),
             number(t["variance_reduction"], "variance_reduction", 24, "pct")]
    p.add(stats.table("cuped", t["cuped"], one, cols, p.y, visible=c.exists(one)), 10)
    e.reading_under(p, w, "cuped")


# ── page 3: the comparisons ─────────────────────────────────────────────────
def _comparisons(p: stats.Page, w: dict, kind: str, checked: dict[str, Any], lang: str) -> None:
    t = w["tables"]
    p.heading("s_comparisons", w["sections"]["comparisons"])
    low, high = e.of_comparison("difference_ci_low"), e.of_comparison("difference_ci_high")

    def number(header: str, stat: str, width: float = 18, pattern: str = "#,##0.####") -> dict:
        x = e.of_comparison(stat)
        return {"header": header, "value": f'IF(ISNULL({x}), "", {c.shown(x, pattern)})', "width": width, "align": "right"}

    cols = [{"header": t["variant"], "value": "[level]", "width": 16, "style": {"weight": "semibold"}},
            {"header": t["difference_interval"], "value": f'{_difference(w, kind, e.of_comparison("difference"))} & " [" & '
                                                         f'{_difference(w, kind, low)} & ", " & {_difference(w, kind, high)} & "]"'},
            number(t["std_error"], "difference_std_error", 18), number(t["lift_std_error"], "lift_std_error", 20),
            number(t["statistic"], "statistic", 18, "0.###")]
    if kind == "continuous":
        df = e.of_comparison("df")  # whole above a thousand: a million units give some 600,000 degrees of freedom
        cols.append({"header": t["df"], "value": f'IF(ISNULL({df}), "", IF({df} >= 1000, {c.shown(df, "#,##0")}, {c.shown(df, "#,##0.#")}))',
                     "width": 16, "align": "right"})
    mde, power = e.of_comparison("mde"), e.of_comparison("power_at_min_lift")
    cols += [{"header": t["mde"], "value": f'IF(ISNULL({mde}), "", {e.pct(mde, "0.##")})', "width": 20, "align": "right"},
             {"header": t["power_at_min_lift"], "value": f'IF(ISNULL({power}), "", {e.pct(power, "0.#")})', "width": 20, "align": "right"},
             {"header": t["min_lift_position"], "value": e.said_by(w["min_lift_position"], e.of_comparison("min_lift_position", "text")),
              "width": 23, "style": {"size": 7}}]
    p.add(stats.table("comparisons", t["comparisons"], COMPARISON, cols, p.y, visible=c.exists(COMPARISON)), 10)
    p.text("comparisons_note", _intervals_note(w, checked, lang) or " ", h=4.5, size=7, color=c.MUTED, can_grow=True)
    e.reading_table(p, w, "comparison", "reading_comparison", w["reading"]["comparisons"])
    ch = w["charts"]
    power_chart = c.xy("power", ch["power"], '[section] = "curve" AND [level] = "power"', [c.mark("y", "line")],
                       {"legend": True, "format": "0.##", "min": 0, "max": 1, "target": float(checked.get("power", 0.8)),
                        "target_x": float(checked.get("min_lift", 0.02))}, split_by="[level_2]", wide=True)
    c.grid(p, w, (power_chart._replace(caption=_numbers(ch["power_caption"], lang, power=float(checked.get("power", 0.8)) * 100,
                                                        min_lift=float(checked.get("min_lift", 0.02)) * 100)),), None, tall=56.0)
    e.reading_under(p, w, "power")


# ── page 4: the Bayesian measures ───────────────────────────────────────────
def _bayes(p: stats.Page, w: dict, kind: str, checked: dict[str, Any], lang: str) -> None:
    t, b = w["tables"], w["bayes"]
    p.heading("s_bayes", w["sections"]["bayes"])
    prior = (_numbers(b["prior_binary"], lang, a=checked.get("prior_alpha", 1.0), b=checked.get("prior_beta", 1.0))
             if kind == "binary" else b["prior_normal"])
    p.text("bayes_lead", prior + " " + b["not_a_test"], h=5, size=8.5, color=c.MUTED, can_grow=True)
    p.keep()
    posterior = c.xy("posterior", w["charts"]["posterior"], '[section] = "curve" AND [level] = "posterior"', [c.mark("y", "line")],
                     {"legend": True, "format": "0.####"}, split_by="[level_2]", wide=True)
    c.grid(p, w, (posterior,), None, tall=52.0)
    one = '[section] = "bayes" AND [stat] = "posterior_mean"'

    def share(header: str, stat: str, pattern: str = "0.#", width: float = 20) -> dict:
        x = e.of_level("bayes", stat)
        return {"header": header, "value": f'IF(ISNULL({x}), "", {e.pct(x, pattern)})', "width": width, "align": "right"}

    low, high = e.of_level("bayes", "cred_low"), e.of_level("bayes", "cred_high")
    cols = [{"header": t["variant"], "value": "[level]", "width": 16, "style": {"weight": "semibold"}},
            {"header": t["posterior_mean"], "value": _mean(kind, "[value]"), "width": 22, "align": "right"},
            {"header": t["credible"], "value": f'"[" & {_mean(kind, low)} & ", " & {_mean(kind, high)} & "]"', "width": 38, "align": "right"},
            share(w["measures_table"]["prob_beat"], "prob_beat_control"), share(t["prob_best"], "prob_best"),
            share(t["loss_choose"], "expected_loss_relative", "0.###", 22), share(t["loss_keep"], "expected_loss_control_relative", "0.###", 22),
            {"header": w["measures_table"]["threshold"], "value": e.said_by(w["loss_position"], e.of_level("bayes", "loss_position", "text")),
             "style": {"size": 7}}]
    p.add(stats.table("bayes", t["bayes"], one, cols, p.y, visible=c.exists(one)), 10)
    p.text("bayes_threshold", _numbers(b["threshold"], lang, threshold=float(checked.get("loss_threshold", 0.001)) * 100),
           h=4.5, size=7, color=c.MUTED, can_grow=True)
    e.reading_table(p, w, "bayes", "reading_bayes", w["reading"]["bayes"])


# ── page 5: the sequential test ─────────────────────────────────────────────
def _log_lambda(x: str) -> str:
    """log Λ with three decimals; whole from a thousand on (a strong effect on many units gives tens of thousands)."""
    return f'IF(ABS({x}) >= 1000, {c.shown(x, "#,##0")}, {c.shown(x, "0.###")})'


def _sequential(p: stats.Page, w: dict, checked: dict[str, Any], lang: str) -> None:
    from app.reports import formats

    t, s, ch = w["tables"], w["sequential"], w["charts"]
    p.heading("s_sequential", w["sections"]["sequential"])
    p.text("sequential_lead", s["which"], h=5, size=8.5, can_grow=True)
    p.keep()
    lift = c.xy("sequential_lift", ch["sequential_lift"], '[section] = "curve" AND [level] = "sequential_lift"',
                [c.mark("low", "band_low"), c.mark("high", "band_high"), c.mark("y", "line")],
                {"x_time": True, "legend": True, "format": "0.#%", "target": 0}, split_by="[level_2]")
    pvalue = c.xy("sequential_p", ch["sequential_p"], '[section] = "curve" AND [level] = "sequential_p"', [c.mark("y", "step")],
                  {"x_time": True, "legend": True, "format": "0.###", "min": 0, "max": 1, "target": float(checked.get("alpha", 0.05))},
                  split_by="[level_2]")
    c.grid(p, w, (lift._replace(caption=s["rebuilt"]), pvalue), None)
    one = '[section] = "sequential" AND [stat] = "p_value"'
    crossing = e.of_level("sequential", "first_crossing")
    day = f'FORMAT(DATEADD("d", {crossing}, DATE(1970, 1, 1)), {c.q(formats.locale(lang).date)})'
    cols = [{"header": t["variant"], "value": "[level]", "width": 16, "style": {"weight": "semibold"}},
            {"header": t["always_valid_p"], "value": c.p_said("[value]"), "width": 24, "align": "right"},
            {"header": t["p_adjusted"], "value": stats.with_stars(c.p_said(e.of_level("sequential", "p_adjusted")),
                                                                  e.of_level("sequential", "significance", "text")), "width": 26, "align": "right"},
            {"header": t["log_lambda"], "value": _log_lambda(e.of_level("sequential", "log_lambda")), "width": 18, "align": "right"},
            {"header": t["sequence"], "value": e.interval(e.of_level("sequential", "cs_low"), e.of_level("sequential", "cs_high")), "width": 36,
             "align": "right"},
            {"header": t["first_crossing"], "value": f'IF(ISNULL({crossing}), {c.q(t["never"])}, {day})', "width": 26},
            _decision_column(w, t["decision"], e.of_level("sequential", "decision", "text"))]
    p.add(stats.table("sequential", t["sequential"], one, cols, p.y, visible=c.exists(one)), 10)
    e.reading_table(p, w, "sequential", "reading_sequential", w["reading"]["sequential"])


# ── page 6: the secondary metrics ───────────────────────────────────────────
def of_secondary(stat: str, column: str = "value") -> str:
    """A number (or text) of the comparison at hand of a secondary metric: its row `secondary|<metric>|<treatment>|stat`."""
    return f'LOOKUP("secondary|" & {e.key_part("[level]")} & "|" & {e.key_part("[level_2]")} & "|{stat}", [key], [{column}])'


def _secondary_value(x: str) -> str:
    """A secondary metric's value: a rate as a percent, a mean as a number (the metric's own type)."""
    binary = f'{of_secondary("metric_type", "text")} = "binary"'
    return f'IF(ISNULL({x}), "", IF({binary}, {e.pct(x, "0.##")}, {c.shown(x, "#,##0.####")}))'


def _secondary_metric() -> str:
    """The metric's name with its direction: «↑» a higher value is better, «↓» a lower one (its own, or the primary's)."""
    return f'[level] & IF({of_secondary("direction", "text")} = "lower", " ↓", " ↑")'


def _secondary_lift(x: str = "lift") -> str:
    lift = of_secondary(x)
    return (f'IF(ISNULL({lift}), "", {e.signed_pct(lift)} & " " & '
            f'{e.interval(of_secondary("lift_ci_low"), of_secondary("lift_ci_high"))})')


def _secondary_p(stat: str, stars: str) -> str:
    return stats.with_stars(c.p_said(of_secondary(stat)), of_secondary(stars, "text"))


def _secondary_worse(p: stats.Page, w: dict) -> None:
    """The guardrails that came out worse than the control (their adjusted p against alpha), under the treatments: what
    a guardrail is for. Nothing when none did."""
    t = w["tables"]
    cols = [{"header": t["metric"], "value": _secondary_metric(), "width": 40, "style": {"weight": "semibold"}},
            {"header": t["variant"], "value": "[level_2]", "width": 22},
            {"header": t["lift_interval"], "value": _secondary_lift()},
            {"header": t["p_adjusted"], "value": _secondary_p("p_adjusted", "p_adjusted_significance"), "width": 26, "align": "right",
             "style": {"color": c.BAD, "weight": "semibold"}}]
    p.add(stats.table("secondary_worse", t["secondary_worse"], SECONDARY_WORSE, cols, p.y, visible=c.exists(SECONDARY_WORSE)), 10)
    p.text("secondary_worse_legend", w["secondary"]["legend"], h=4.5, size=7, color=c.MUTED, can_grow=True, visible=c.exists(SECONDARY_WORSE))


def _secondary(p: stats.Page, w: dict, checked: dict[str, Any], lang: str) -> None:
    """Every comparison of every secondary metric with the control: the two values, the lift with its interval, the p-value
    before and after the correction of the family, the decision; how the family was corrected and which came out worse."""
    t = w["tables"]
    p.heading("s_secondary", w["sections"]["secondary"])
    p.text("secondary_lead", w["secondary"]["lead"], h=5, size=8.5, color=c.MUTED, can_grow=True)
    p.y += 1.0
    cols = [{"header": t["metric"], "value": _secondary_metric(), "style": {"weight": "semibold"}},  # the rest of the width: a column's name
            {"header": t["variant"], "value": "[level_2]", "width": 15},
            {"header": t["control_value"], "value": _secondary_value(of_secondary("mean_control")), "width": 16, "align": "right"},
            {"header": t["treatment_value"], "value": _secondary_value(of_secondary("mean_treatment")), "width": 20, "align": "right"},
            {"header": t["lift_interval"], "value": _secondary_lift(), "width": 42},
            {"header": t["p"], "value": _secondary_p("p_value", "significance"), "width": 23, "align": "right"},
            {"header": t["p_adjusted"], "value": _secondary_p("p_adjusted", "p_adjusted_significance"), "width": 23, "align": "right"},
            _decision_column(w, t["decision"], "[text]")]
    p.add(stats.table("secondary", t["secondary"], SECONDARY, cols, p.y, visible=c.exists(SECONDARY)), 10)
    p.text("secondary_note", w["secondary"]["legend"] + " " + _intervals_note(w, checked, lang), h=4.5, size=7, color=c.MUTED, can_grow=True)
    e.reading_under(p, w, "secondary")


# ── the page ────────────────────────────────────────────────────────────────
def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    from app.reports import formats

    lang = c.code(locale)
    checked = catalog.check_params(model, dict(params))
    kind = catalog.variant(model, checked)
    w = c.words(model, kind, lang)
    header, footer = stats.bands(name(params, lang), " · ".join(x for x in (about(params, lang), step) if x), w,
                                 formats.locale(lang).texts, stats.sample_line(sample, lang))
    p = stats.Page()
    _decision(p, w, lang, params, checked, kind)
    _variants(p, w, kind, checked)
    _comparisons(p, w, kind, checked, lang)
    if checked.get("bayes"):
        _bayes(p, w, kind, checked, lang)
    if checked.get("sequential"):
        _sequential(p, w, checked, lang)
    if checked.get("secondary_metrics"):
        _secondary(p, w, checked, lang)
    e.how_computed(p, w)
    e.notes(p, w, "units")
    return e.document(header, p.items, footer, lang, CALCULATED)


register("ab_test", layout, name, about)
