"""The report of the node «A/B test planning» (docs/specs/ab-testing.md, «The node `ab_plan`»): how many units and
days an experiment needs to detect a lift, or which lift a given size can detect, from the history of the metric.

Page one: the answer in large type (the units per variant and in total, the days and weeks; or the smallest
detectable lift), the baseline line, the CUPED line, the corrections' line, the history against the plan, the
reading. Then power against the lift (a line at the power asked, a vertical one at the lift), the smallest lift
against the days, the grid of lifts as a table, the histogram of the history, the notes, «How this report was
computed». Nothing here is a test: no checks, no verdict, every number a measure.
"""
from __future__ import annotations

from typing import Any

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import experiments as e
from app.engine.models.layouts import register
from app.engine.stats import layouts as stats

WORDS = "ab_plan"
GRID = '[section] = "plan" AND [stat] = "lift"'


def name(params: dict[str, Any], locale: str) -> str:
    return c.words(WORDS, "binary", locale)["name"]


def about(params: dict[str, Any], locale: str) -> str:
    """«converted · 2 variants»."""
    w = c.words(WORDS, "binary", locale)
    metric = e.metric_name(params)
    arms = params.get("arms") or catalog.MODELS["ab_plan"]["defaults"].get("arms", 2)
    return e.one_line(" · ".join(x for x in (metric, w["about_arms"].replace("{arms}", str(arms))) if x))


def _answer(w: dict, checked: dict[str, Any]) -> str:
    """The answer: units per variant and in total, with a date column the days and weeks; or the smallest lift."""
    a = w["answer"]
    n = e.model_number
    units, total = c.shown(n("n_per_variant"), "#,##0"), c.shown(n("n_total"), "#,##0")
    days = c.join(a["days"], days=c.shown(n("days"), "#,##0"), daily=c.shown(n("daily_units"), "#,##0"),
                  weeks=c.shown(n("weeks"), "#,##0"))
    with_days = f'IF(ISNULL({n("days")}), "", {days})'
    solve = checked.get("solve_for", "sample_size")
    if solve == "sample_size":
        return f'{c.join(a["sample_size"], units=units, total=total)} & {with_days}'
    mde = e.pct(n("mde"), "0.##")
    absolute = c.shown(n("mde_absolute"), "#,##0.####")
    if solve == "mde_for_units":
        return c.join(a["mde_for_units"], units=units, mde=mde, absolute=absolute)
    return c.join(a["mde_for_days"], days=c.shown(n("days"), "#,##0"), units=units, mde=mde, absolute=absolute)


def _lines(p: stats.Page, w: dict, kind: str, checked: dict[str, Any]) -> None:
    """The baseline, CUPED, the corrections, the history against the plan: one panel each."""
    n, ln = e.model_number, w["lines"]
    baseline = e.pct(n("baseline_mean"), "0.###") if kind == "binary" else c.shown(n("baseline_mean"), "#,##0.####")
    p.text("baseline", "=" + c.join(ln["baseline"][kind], mean=baseline, sd=c.shown(n("baseline_sd"), "#,##0.####"),
                                   units=c.shown(n("n_history"), "#,##0"), days=c.shown(n("history_days"), "#,##0"),
                                   daily=c.shown(n("daily_units"), "#,##0")),
           h=6, size=9, background=c.PANEL, padding=1.5, can_grow=True)
    p.y += 1.0
    if checked.get("covariate"):
        cuped_days = f'IF(ISNULL({n("days_cuped")}), "", {c.join(ln["cuped_days"], days=c.shown(n("days_cuped"), "#,##0"))})'
        p.text("cuped_line", "=" + c.join(ln["cuped"], covariate=c.q(str(checked["covariate"])),
                                          rho=c.shown(n("covariate_correlation"), "0.###"),
                                          reduction=e.pct(n("variance_reduction"), "0.#"),
                                          units=c.shown(n("n_per_variant_cuped"), "#,##0")) + f" & {cuped_days}",
               h=6, size=9, background=c.PANEL, padding=1.5, can_grow=True, visible=f'=NOT ISNULL({n("variance_reduction")})')
        p.y += 1.0
    said = c.q(w["values"]["correction"].get(str(checked.get("correction", "holm")), str(checked.get("correction"))))
    p.text("correction_line", "=" + c.join(ln["correction"], arms=c.shown(n("arms"), "0"), correction=said,
                                         alpha=c.shown(n("alpha"), "0.####"), per=c.shown(n("alpha_per_comparison"), "0.#####"),
                                         power=e.pct(n("power"), "0")),
           h=6, size=9, background=c.PANEL, padding=1.5, can_grow=True)
    p.y += 1.0
    short = e.model_text("history_short")
    p.text("history_short", f"={short}", h=5, size=8.5, color=c.MUTED, can_grow=True, visible=f"=NOT ISNULL({short})")


def _charts(p: stats.Page, w: dict, checked: dict[str, Any], lang: str) -> None:
    ch = w["charts"]
    power = float(checked.get("power", 0.8))
    lift = c.xy("power_lift", ch["power_lift"], '[section] = "curve" AND [level] = "power_lift"', [c.mark("y", "line")],
                {"legend": False, "format": "0.##", "x_format": "0.#%", "min": 0, "max": 1, "target": power,
                 "target_x": float(checked.get("min_lift", 0.02)), "log_x": True})
    days = c.xy("mde_days", ch["mde_days"], '[section] = "curve" AND [level] = "mde_days"', [c.mark("y", "line")],
                {"legend": False, "format": "0.##%", "x_format": "#,##0"})  # x: days, y: the lift
    history = c.histogram("history", ch["history"], "metric", w["series"]["units"])
    c.grid(p, w, (lift, days, history), ("s_charts", w["sections"]["charts"]))


def _grid(p: stats.Page, w: dict, checked: dict[str, Any]) -> None:
    t = w["tables"]
    p.heading("s_grid", w["sections"]["grid"])
    asked = f'{e.of_level("plan", "asked")} = 1'
    bold = [{"when": asked, "style": {"weight": "bold", "color": c.ACCENT}}]

    def number(header: str, stat: str, width: float = 22) -> dict:
        x = e.of_level("plan", stat)
        return {"header": header, "value": f'IF(ISNULL({x}), "", {c.shown(x, "#,##0")})', "width": width, "align": "right", "rules": bold}

    cols = [{"header": t["lift"], "value": e.pct("[value]", "0.##"), "width": 19, "align": "right", "rules": bold},
            {"header": t["mde_absolute"], "value": c.shown(e.of_level("plan", "mde_absolute"), "#,##0.######"), "width": 22,
             "align": "right", "rules": bold},
            number(t["n_per_variant"], "n_per_variant", 24), number(t["n_control"], "n_control", 22), number(t["n_total"], "n_total", 24)]
    if checked.get("date"):
        cols += [number(t["days"], "days", 14), number(t["weeks"], "weeks", 17)]
    if checked.get("covariate"):
        cols.append(number(t["n_per_variant_cuped"], "n_per_variant_cuped", 24))
        if checked.get("date"):
            cols.append(number(t["days_cuped"], "days_cuped", 18))
    table = stats.table("plan", t["plan"], GRID, cols, p.y, visible=c.exists(GRID))
    table["sort"] = [{"by": "[value]"}, {"by": "[ord]"}]
    p.add(table, 10)
    p.text("plan_note", w["plan_note"], h=4.5, size=7, color=c.MUTED, can_grow=True)


def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    from app.reports import formats

    lang = c.code(locale)
    checked = catalog.check_params(model, dict(params))
    kind = catalog.variant(model, checked)
    w = c.words(model, kind, lang)
    header, footer = stats.bands(name(params, lang), " · ".join(x for x in (about(params, lang), step) if x), w,
                                 formats.locale(lang).texts, stats.sample_line(sample, lang))
    p = stats.Page()
    p.heading("s_plan", w["sections"]["plan"])
    p.text("answer", "=" + _answer(w, checked), h=9, size=13, weight="bold", color=c.ACCENT, can_grow=True)
    p.y += 1.5
    p.keep()
    _lines(p, w, kind, checked)
    e.reading_under(p, w, "plan", "baseline", "correction", "cuped")
    _charts(p, w, checked, lang)
    _grid(p, w, checked)
    e.notes(p, w, "history")
    e.how_computed(p, w)
    return e.document(header, p.items, footer, lang)


register("ab_plan", layout, name, about)
