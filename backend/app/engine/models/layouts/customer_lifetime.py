"""The blocks the two customer lifetime nodes share (docs/specs/customer-lifetime-models.md, «The
report»): `clv.py` and `bg_bb.py` say what is their own (the name, what the page is about, the
purchase models) and call `build`.

Page: the header band; four cards (the customers, the purchase model, the expected purchases over
the horizon, the total CLV or the mean P(alive)); the formula and under it the business settings the
node ran with (horizon, discount, margin); the checks and their advice. Then the model (its
parameters per part, the comparison of the purchase models with `auto` or `compare`), the numbers of
the fit, the fit to the data (repeat purchases observed and expected, the holdout), P(alive) (the
map by frequency and recency on a fixed scale from 0 to 1, the curves, the distribution), the spend
(variant `value`), the CLV (its distribution, its concentration, the top customers) and the notes.

Without a holdout column the sentence that says how to add one stands in place of the holdout chart.
In the `purchases` variant the CLV's page is drawn on the expected transactions, under the same ids.

The `hist|clv` edges are read as the numbers the page prints (the CLV, or the expected transactions):
the runner chooses them equal on a logarithmic scale, with one bin for zero (`x0 = x1 = 0`), and the
page labels each bin by its two edges.
"""
from __future__ import annotations

from typing import Any

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.stats import layouts as stats

MEASURES = ("sample_size", "holdout", "horizon_length", "boundary")  # numbers with no p-value: no verdict, no colour
COUNTED = ("sample_size",)  # the measures that say what their count counts (the repeat customers)
SCIENTIFIC = ("boundary",)  # the measures written by their exponent: an estimate far from 1 on the log scale
SMALL = "0.#####"  # the tables' numbers: five decimals at most
PURCHASE_MODELS = ("pareto_nbd", "bg_nbd", "mbg_nbd")
# the numbers the blocks below say apart from the fit's table: its rows leave them out (a name ending
# in «_» stands for every number that starts with it)
FIT_SKIP = ("param_", "spend_param_", "holdout_", "spend_n", "spend_log_lik", "mean_spend_population", "discount_rate",
            "discount_per_period", "margin", "clv_", "discounted_transactions")
PARAMETER_ORDER = {"purchases": ("r", "alpha", "s", "beta", "a", "b", "gamma", "delta"), "spend": ("p", "q", "gamma")}


# ── small expressions ───────────────────────────────────────────────────────
def _percent(x: str, decimals: str = "0.#") -> str:
    return f'FORMAT({x} * 100, "{decimals}") & " %"'


def _signed_percent(x: str) -> str:
    """«−12.1 %», «+3.4 %» from a share."""
    return f'IF({x} < 0, "−", "+") & FORMAT(ABS({x}) * 100, "0.0") & " %"'


def _model_of_level(w: dict) -> str:
    """The purchase model of a candidate's row, in the page's words."""
    names = w["values"]["purchase_model"]
    return "IFS(" + ", ".join(f'[level] = "{m}", {c.q(names[m])}' for m in PURCHASE_MODELS) + ", [level])"


def _edges(x0: str = "[x0]", x1: str = "[x1]", pattern: str = "#,##0.##") -> str:
    """A bin of a histogram by its two edges, «5 – 9»; the last, open at the top, «81+»; one of a single
    value (the zero bin), that value."""
    one = f'FORMAT({x0}, "{pattern}")'
    return f'IF(ISNULL({x1}), {one} & "+", IF({x0} = {x1}, {one}, {one} & " – " & FORMAT({x1}, "{pattern}")))'


def _class(x0: str = "[x0]", x1: str = "[x1]") -> str:
    """A class of counts: 0, 1, 2…; adjacent counts pooled so that each class expects five customers, «5–7»; the
    last, open at the top, «12+»."""
    return (f'IF(ISNULL({x1}), FORMAT({x0}, "0") & "+", IF({x0} = {x1}, FORMAT({x0}, "0"), '
            f'FORMAT({x0}, "0") & "–" & FORMAT({x1}, "0")))')


def _series_name(w: dict, group: str = "[level_2]") -> str:
    """«observed» and «expected» in the page's words."""
    s = w["series"]
    return f'IF({group} = "observed", {c.q(s["observed"])}, IF({group} = "expected", {c.q(s["expected"])}, {group}))'


def _verdict_key(check: str) -> str:
    return f"assumption|{check}||verdict"


# ── the model: parameters, comparison of the purchase models ────────────────
def _parameters(variant_has_spend: bool):
    def block(p: stats.Page, w: dict) -> None:
        t = w["tables"]
        c.heading(p, "s_model", w["sections"]["model"])
        one = '[section] = "coef" AND [stat] = "estimate"'
        pairs = []
        for part, names in w["parameters"].items():
            for name, said in names.items():
                pairs.append(f'[level_2] = "{part}" AND [level] = "{name}", {c.q(said)}')
        parts = f'IFS([level_2] = "purchases", {c.q(t["part_purchases"])}, [level_2] = "spend", {c.q(t["part_spend"])}, "")'
        cols = [{"header": t["part"], "value": parts, "width": 26},
                {"header": t["parameter"], "value": "IFS(" + ", ".join(pairs) + ", [level])"},
                *({"header": c.stat_label(w, "coef", s), "value": c.number_of_row("coef", s), "width": 22, "align": "right",
                   "format": SMALL} for s in ("estimate", "std_error", "ci_low", "ci_high"))]
        p.add(stats.table("parameters", t["parameters"], one, cols, p.y, visible=c.exists(one)), 10)
        p.text("parameters_note", w["parameters_note"], h=4.5, size=7, color=c.MUTED, can_grow=True, visible=c.exists(one))
        p.y += 1.5
    return block


def _comparison(p: stats.Page, w: dict) -> None:
    """With `auto` or `compare`: the three purchase models side by side, the chosen one marked."""
    t = w["tables"]
    one = '[section] = "candidate" AND [stat] = "log_lik"'
    chosen = c.of_row("candidate", "chosen")
    same = ('=COUNTIF([section] = "candidate" AND [stat] = "n_params" AND [value] <> '
            'LOOKUP("candidate|pareto_nbd||n_params", [key], [value])) = 0')

    def number(stat: str, pattern: str, width: float = 24.0) -> dict:
        return {"header": c.stat_label(w, "candidate", stat), "value": c.number_of_row("candidate", stat), "width": width,
                "align": "right", "format": pattern}

    holdout = c.of_row("candidate", "holdout_rel_error")
    table = stats.table("candidates", t["candidates"], one, [
        {"header": t["purchase_model"], "value": _model_of_level(w),
         "rules": [{"when": f"{chosen} = 1", "style": {"weight": "bold", "color": c.ACCENT}}]},
        number("log_lik", "#,##0.##", 30), number("aic", "#,##0.##"), number("bic", "#,##0.##"),
        {"header": c.stat_label(w, "candidate", "chi_square_p_value"), "value": c.p_said(c.of_row("candidate", "chi_square_p_value")),
         "width": 22, "align": "right"},
        {"header": c.stat_label(w, "candidate", "holdout_rel_error"),
         "value": f'IF(ISNULL({holdout}), "", {_signed_percent(holdout)})', "width": 26, "align": "right"},
        {"header": c.stat_label(w, "candidate", "converged"),
         "value": f'IF({c.of_row("candidate", "converged")} = 1, {c.q(w["coef"]["yes"])}, {c.q(w["coef"]["no"])})', "width": 22},
        {"header": c.stat_label(w, "candidate", "chosen"), "value": f'IF({chosen} = 1, "✓", "")', "width": 16, "align": "center"},
    ], p.y, visible=c.exists(one))
    table["sort"] = [{"by": c.of_row("candidate", "log_lik"), "dir": "desc"}, {"by": "[ord]"}]
    p.add(table, 10)
    p.text("candidates_note", w["candidates_note"], h=4.5, size=7, color=c.MUTED, can_grow=True,
           visible="=" + c.exists(one).lstrip("=") + " AND " + same.lstrip("="))


# ── the fit to the data ─────────────────────────────────────────────────────
def _column(id_: str, title: str, filter_: str, category: str, stat: str, w: dict, fmt: str = "#,##0", under: str | None = None) -> c.Chart:
    """Two series, observed and expected, one column a class."""
    more = {"filter": filter_, "category": category, "split_by": _series_name(w),
            "series": [{"value": "SUM([value])", "name": ""}],
            "options": {"legend": True, "format": fmt}, "visible": c.exists(filter_)}
    return c.Chart(id_, "column", title, more, under)


def _fit_to_the_data(p: stats.Page, w: dict) -> None:
    ch = w["charts"]
    frequency = _column("frequency", ch["frequency"], '[section] = "hist" AND [level] = "frequency"', _class(), "count", w,
                        under="frequency_fit")
    holdout_filter = '[section] = "hist" AND [level] = "holdout" AND [stat] = "mean"'
    holdout = _column("holdout", ch["holdout"], holdout_filter, _class(), "mean", w, "0.##", under="holdout")
    top = p.y + 10.5  # under the heading, which the grid writes
    c.grid(p, w, (frequency, holdout), ("s_fit_data", w["sections"]["fit_data"]))
    # without a holdout column, in the holdout chart's place, how to have one
    p.items.append({"id": "holdout_how", "type": "text", "x": 95.0, "y": top + 24.0, "w": 91.0, "h": 24.0,
                    "value": stats.static(w["holdout_how"]), "style": {"size": 8.5, "color": c.MUTED},
                    "visible": f"=COUNTIF({holdout_filter}) = 0"})
    share = c.num(c.model_key("holdout_rel_error"))
    p.text("holdout_totals", "=" + c.join(w["holdout_totals"], expected=c.shown(c.num(c.model_key("holdout_expected")), "#,##0"),
                                         observed=c.shown(c.num(c.model_key("holdout_observed")), "#,##0"),
                                         error=_signed_percent(share), length=c.shown(c.num(c.model_key("holdout_length")), "#,##0"),
                                         unit=c.txt(c.model_key("time_unit"))),
           h=5, size=8, color=c.MUTED, can_grow=True, visible=f"=NOT ISNULL({share})")


# ── P(alive) ────────────────────────────────────────────────────────────────
def _p_alive(p: stats.Page, w: dict) -> None:
    ch, s = w["charts"], w["series"]
    median_age = c.shown(c.num(c.model_key("median_age")), "0.#")
    how = "=" + c.join(w["p_alive_how"], age=median_age, unit=c.txt(c.model_key("time_unit")))
    f = '[section] = "cell" AND [stat] = "p_alive"'
    cell = c.Chart("p_alive_cell", "heatmap", ch["p_alive_cell"], {
        "filter": f, "category": "[level_2]", "split_by": "[level]", "series": [{"value": "SUM([value])", "name": s["p_alive"]}],
        "options": {"labels": True, "format": "0.00", "min": 0, "max": 1}, "visible": c.exists(f)}, caption=how)
    curves = c.xy("p_alive_recency", ch["p_alive_recency"], '[section] = "curve" AND [level] = "p_alive_recency"',
                  [{"value": "[value]", "as": "line", "name": ""}], {"min": 0, "max": 1, "legend": True, "format": "0.##"},
                  split_by="[level_2]")
    distribution = c.histogram("p_alive", ch["p_alive"], "p_alive", s["customers"])._replace(wide=True)
    c.grid(p, w, (cell, curves, distribution), ("s_p_alive", w["sections"]["p_alive"]), tall=56.0)


# ── the spend (variant `value`) ─────────────────────────────────────────────
def _spend(p: stats.Page, w: dict) -> None:
    ch, s = w["charts"], w["series"]
    bins = _column("spend", ch["spend"], '[section] = "hist" AND [level] = "spend"', _edges(), "count", w, under="spend_tail")
    f = '([section] = "point" OR [section] = "curve") AND [level] = "spend_frequency"'
    against = c.xy("spend_frequency", ch["spend_frequency"], f, [
        {"value": 'IF([section] = "point", [value], NULL)', "as": "point", "name": s["repeaters"], "label": "[text]"},
        {"value": 'IF([section] = "curve", [value], NULL)', "as": "line", "name": s["lowess"]}],
        {"legend": False, "format": "0.##"}, under="spend_independence")
    c.grid(p, w, (bins, against), ("s_spend", w["sections"]["spend"]))
    r, hc3 = c.num("assumption|spend_independence||value"), c.num("assumption|spend_independence||p_value")
    p.text("spend_reading", "=" + c.join(w["spend_reading"], r=c.shown(r, "0.000"), p=c.p_said(hc3)), h=5, size=8, color=c.MUTED,
           can_grow=True, visible=f"=NOT ISNULL({r})")
    mean = c.num(c.model_key("mean_spend_population"))
    p.text("spend_mean", "=" + c.join(w["spend_mean"], mean=c.shown(mean, "#,##0.##")), h=5, size=8.5, can_grow=True,
           visible=f"=NOT ISNULL({mean})")


# ── the CLV, or the expected purchases ──────────────────────────────────────
def _clv(value: bool):
    def block(p: stats.Page, w: dict) -> None:
        ch, s, t = w["charts"], w["series"], w["tables"]
        f = '[section] = "hist" AND [level] = "clv"'
        more = {"filter": f, "category": _edges(), "series": [{"value": "SUM([value])", "name": s["customers"]}],
                "options": {"legend": False, "format": "#,##0"}, "visible": c.exists(f)}
        spread = c.Chart("clv", "column", ch["clv" if value else "transactions"], more)
        g = '[section] = "curve" AND [level] = "concentration"'
        concentration = c.xy("concentration", ch["concentration" if value else "concentration_transactions"], g, [{"value": "[value]", "as": "line", "name": ""}],
                             {"legend": False, "diagonal": True, "unit_square": True, "format": "0.##"})
        c.grid(p, w, (spread, concentration), ("s_clv", w["sections"]["clv" if value else "transactions"]))
        top10 = c.num(c.model_key("clv_top10_share"))
        p.text("concentration_reading", "=" + c.join(w["concentration" if value else "concentration_transactions"],
                                                     share=_percent(top10, "0")), h=5, size=8.5, can_grow=True,
               visible=f"=NOT ISNULL({top10})")
        one = '[section] = "customer" AND [stat] = "rank"'
        stat = lambda name, pattern, width: {"header": c.stat_label(w, "customer", name), "value": c.number_of_row("customer", name),  # noqa: E731
                                              "width": width, "align": "right", "format": pattern}
        cols = [{"header": c.stat_label(w, "customer", "rank"), "value": c.of_row("customer", "rank"), "width": 14, "align": "right",
                 "format": "#,##0"},
                {"header": t["customer"], "value": "[level]"},
                stat("frequency", "#,##0", 16), stat("recency", "0.###", 20), stat("age", "0.###", 18),
                stat("p_alive", "0.000", 22), stat("expected_transactions", "0.##", 30)]
        if value:
            cols += [stat("expected_spend", "#,##0.##", 26), stat("clv", "#,##0.##", 24)]
        p.add(stats.table("top_customers", t["top_customers"], one, cols, p.y, visible=c.exists(one)), 10)
    return block


# ── the page ────────────────────────────────────────────────────────────────
def build(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None, *, name: str,
          about: str) -> dict:
    checked = catalog.check_params(model, params)
    variant = catalog.variant(model, checked)
    value = variant == "value"
    w = c.words(model, variant, locale)
    k = w["kpi"]
    unit = c.txt(c.model_key("time_unit"))
    repeaters = c.shown(c.num(c.model_key("n_repeaters")), "#,##0")
    cards = (c.Card("k_n", k["customers"], c.num(c.model_key("n")), "#,##0", under=c.join(k["repeaters"], n=repeaters)),
             c.Card("k_model", k["model"], c.txt(c.model_key("purchase_model")),
                    under=c.join(k["log_lik"], ll=c.shown(c.num(c.model_key("log_lik")), "#,##0.#"))),
             c.Card("k_expected", k["expected"], c.shown(c.num(c.model_key("expected_transactions")), "#,##0"),
                    under=c.join(k["over"], horizon=c.shown(c.num(c.model_key("horizon")), "#,##0.##"), unit=unit)),
             c.Card("k_clv", k["clv"], c.shown(c.num(c.model_key("clv_total")), "#,##0"),
                    under=c.join(k["clv_mean"], mean=c.shown(c.num(c.model_key("clv_mean")), "#,##0.##"))) if value else
             c.Card("k_alive", k["alive"], c.shown(c.num(c.model_key("mean_p_alive")), "0.000"),
                    under=c.join(k["alive_share"], share=_percent(c.num(c.model_key("share_repeaters")), "0")))
             )
    asked_auto = f'{c.txt(c.model_key("purchase_model_asked"))} = {c.q(w["values"]["purchase_model_asked"]["auto"])}'
    model_said = c.txt(c.model_key("purchase_model")) + f' & IF({asked_auto}, {c.q(w["business"]["chosen_by"])}, "")'
    horizon = c.shown(c.num(c.model_key("horizon")), "#,##0.##")
    if value:
        margin = c.num(c.model_key("margin"))
        line = c.join(w["business"]["value"], model=model_said, unit=unit, horizon=horizon,
                      discount=_percent(c.num(c.model_key("discount_rate")), "0.##"), margin=_percent(margin, "0.##"),
                      kind=f'IF({margin} = 1, {c.q(w["business"]["revenue"])}, {c.q(w["business"]["margin"])})')
    else:
        line = c.join(w["business"]["purchases"], model=model_said, unit=unit, horizon=horizon)
    blocks = (_fit_to_the_data, _p_alive, *((_spend,) if value else ()), _clv(value))
    doc = c.page(model, params, locale, sample, step, name=name, about=about, cards=cards,
                 assumptions_note=c.alpha_said(w["alpha_note"], checked.get("alpha", 0.05), locale) + " " + w["assumptions_note"],
                 after_assumptions=(c.measures(MEASURES, COUNTED, SCIENTIFIC),), fit_skip=FIT_SKIP, fit_format=SMALL, room=12.0, summary_lines=(("business", line),),
                 after_coefficients=(_parameters(value), *((_comparison,) if model == "clv" else ())), blocks=blocks)
    return doc
