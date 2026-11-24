"""The report of the node «Bayesian model» (docs/specs/bayes-report.md): drawn from the ten-column rows a run of the group `bayes` wrote
(docs/specs/bayes-contract.md, «Sections») and from nothing else, in both variants, `sample` and `prior_predictive`.

This is the report's first lot (Q1): the pages that need no chart and no reading.

1. Summary: four cards (the rows used, the draws, the divergences, the largest R-hat), the facts line (the saved model and its version, the
   sampler, the seed, the precision of the draws, the seconds). The divergences are a count and every diagnostic a measure or a fact: no
   mark, no colour that judges, no word of verdict (answer 11 of the map). The prior predictive has three cards (it has no sampler to
   report on) and its own facts line.
2. The posterior summary (`sample`): one row a scalar quantity or correlation, with its mean, sd, three quantiles, R-hat, bulk and tail ESS,
   the standard error of the mean and the contraction (empty where the prior has no finite variance), then the same coefficients of a
   linear predictor on the column's scale (`orig_*`). The prior predictive's summary: mean, sd and the five quantiles of each quantity's
   prior draws. A plate's quantities are the groups' page (lot Q2).
3. Data: each bound variable, its column, its type, whether it was standardized, with its mean and sd, the units or levels and the
   reference level of a category, the values left out.
4. «How this report was computed» (`common.how_computed`: its shell, the readings are lot Q4's) and the notes.

Every number is read from the rows by its key. A label, a unit, a column is data: it goes through the expressions as a value, never as a
template. The diagnostics page and the charts are lot Q2's, the predictive checks lot Q3's, the readings and the literature lot Q4's.
"""
from __future__ import annotations

from typing import Any

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import register
from app.engine.stats import layouts as stats

WORDS = "bayes_model"
SUMMARY = '[section] = "summary" AND [stat] = "mean"'
# the quantities that are not a plate's: the scalars and the correlations (a plate's are the groups' page, lot Q2)
NOT_A_PLATE = 'ISNULL(LOOKUP("quantity|" & [level] & "||plate", [key], [text]))'


def _variant(params: dict[str, Any]) -> str:
    return "prior_predictive" if params.get("mode") == "prior_predictive" else "sample"


def name(params: dict[str, Any], locale: str) -> str:
    """«Bayesian model»."""
    return c.words(WORDS, _variant(params), locale)["name"]


def about(params: dict[str, Any], locale: str) -> str:
    """What the run did: «posterior sampling», «prior predictive check»."""
    return c.words(WORDS, _variant(params), locale)["about"][_variant(params)]


# ── page 1: the summary ─────────────────────────────────────────────────────
def _largest(stat: str) -> str:
    return f'MAX(IF([section] = "quantity" AND [stat] = "{stat}", [value], NULL))'


def _smallest(stat: str) -> str:
    return f'MIN(IF([section] = "quantity" AND [stat] = "{stat}", [value], NULL))'


def _cards(w: dict, variant: str) -> tuple[c.Card, ...]:
    k = w["kpi"]
    n, chains, draws = c.model_number("n"), c.model_number("chains"), c.model_number("draws")
    left = f'COALESCE({c.model_number("n_missing")}, 0) + COALESCE({c.model_number("n_not_finite")}, 0)'
    rows = c.Card("k_n", k["rows"], n, "#,##0", under=c.join(k["rows_under"], left=c.shown(f"({left})", "#,##0")))
    drawn = f'{c.shown(chains, "0")} & " × " & {c.shown(draws, "#,##0")}'
    if variant == "prior_predictive":
        return (rows,
                c.Card("k_draws", k["draws_prior"], drawn, under=c.q(k["draws_prior_under"])),
                c.Card("k_quantities", k["quantities"], c.model_number("n_quantities"), "#,##0"))
    return (rows,
            c.Card("k_draws", k["draws"], drawn, under=c.join(k["draws_under"], warmup=c.shown(c.model_number("warmup"), "#,##0"))),
            c.Card("k_divergences", k["divergences"], c.num("sampler|all||divergences"), "#,##0",
                   under=c.join(k["divergences_under"], total=c.shown(f"({chains} * {draws})", "#,##0"))),
            c.Card("k_rhat", k["rhat"], _largest("rhat_max"), "0.000",
                   under=c.join(k["ess_under"], bulk=c.shown(_smallest("ess_bulk_min"), "#,##0"), tail=c.shown(_smallest("ess_tail_min"), "#,##0"))))


def _facts(w: dict, variant: str) -> str:
    """The facts line, an expression: the saved model and its version (their ids, as the rows hold them), the sampler, the seed, the
    precision of the draws and the seconds of each stage."""
    t, number = c.model_text, c.model_number
    parts = dict(model=t("bayes_model"), version=t("bayes_version"), sampler=t("sampler"), metric=t("metric"), seed=c.shown(number("seed"), "0"),
                 precision=t("precision"), delta=c.shown(number("adapt_delta"), "0.##"),
                 depth=c.shown(number("max_treedepth"), "0"))
    if variant == "sample":
        parts.update(warmup=c.shown(number("seconds_warmup"), "#,##0.#"), sampling=c.shown(number("seconds_sampling"), "#,##0.#"))
    return c.join(w["facts"][variant], **parts)


def _summary(p: stats.Page, w: dict, variant: str) -> None:
    p.heading("s_summary", w["sections"]["summary"])
    for card, x in zip(_cards(w, variant), (0, 47, 94, 141)):
        item: dict[str, Any] = {"id": card.id, "type": "kpi", "dataset": c.DATASET, "x": x, "y": p.y, "w": 45, "h": 20,
                                "label": card.label, "value": card.value}
        if card.format:
            item["format"] = card.format
        p.add(item)
        if card.under:
            p.text(card.id + "_under", "=" + card.under, h=4.5, x=x + 2, w=43, advance=False, size=7.5, color=c.MUTED)
            p.items[-1]["y"] = p.y + 14.6
    p.y += 21.5
    p.keep()
    p.text("facts", "=" + _facts(w, variant), h=6.5, size=9, background=c.PANEL, padding=1.5, can_grow=True)


# ── page 2: the summary of each quantity ────────────────────────────────────
def _number(stat: str, width: float, header: str, pattern: str | None = None) -> dict:
    """A number of the row at hand: as the models' tables write one (`c.number_of_row`), or by `pattern`: the effective sample sizes whole,
    R-hat to three decimals."""
    x = c.of_row("summary", stat)
    value = c.number_of_row("summary", stat) if pattern is None else f'IF(ISNULL({x}), "", {c.shown(x, pattern)})'
    return {"header": header, "value": value, "width": width, "align": "right"}


def _quantity() -> dict:
    """The quantity as the person named it: its label (its identifier when it has none), and the unit's text."""
    label = 'LOOKUP("quantity|" & [level] & "||label", [key], [text])'
    return {"header": "", "value": f"COALESCE({label}, [level])", "style": {"weight": "semibold"}}


def _posterior(p: stats.Page, w: dict, variant: str) -> None:
    t = w["tables"]
    key = "posterior" if variant == "sample" else "prior"
    p.heading("s_posterior", w["sections"][key])
    p.text("posterior_note", w["posterior_note"][variant], h=5, size=8, color=c.MUTED, can_grow=True)
    p.y += 1.0
    scope = f"{SUMMARY} AND {NOT_A_PLATE}"
    quantity = {**_quantity(), "header": t["quantity"], "width": 38}
    unit = {"header": t["unit"], "value": '[level_2]', "width": 16}
    if variant == "prior_predictive":
        cols = [quantity, unit, _number("mean", 17, t["mean"]), _number("sd", 17, t["sd"]), _number("q5", 17, t["q5"]),
                _number("q25", 17, t["q25"]), _number("q50", 17, t["q50"]), _number("q75", 17, t["q75"]), _number("q95", 17, t["q95"])]
    else:
        cols = [quantity, unit, _number("mean", 13, t["mean"]), _number("sd", 12, t["sd"]), _number("q5", 13, t["q5"]),
                _number("q50", 13, t["q50"]), _number("q95", 13, t["q95"]), _number("rhat", 12, t["rhat"], "0.000"),
                _number("ess_bulk", 13, t["ess_bulk"], "#,##0"), _number("ess_tail", 13, t["ess_tail"], "#,##0"), _number("mcse_mean", 13, t["mcse_mean"]),
                _contraction(t)]
    p.add(stats.table("posterior", t[key], scope, cols, p.y, visible=c.exists(scope)), 10)
    if variant == "sample":
        _original(p, w)


def _contraction(t: dict) -> dict:
    """The contraction, or «—» where the prior has no finite variance (the note of the run says why)."""
    x = c.of_row("summary", "contraction")
    return {"header": t["contraction"], "value": f'IF(ISNULL({x}), "—", {c.shown(x, "0.###")})', "width": 14, "align": "right"}


def _original(p: stats.Page, w: dict) -> None:
    """The coefficients of a linear predictor on the column's scale: the coefficient over the predictor's sd, a second table under the
    first, only when the run wrote them."""
    t = w["tables"]
    one = '[section] = "summary" AND [stat] = "orig_mean"'
    p.y += 1.0
    p.text("original_note", w["original_note"], h=5, size=8, color=c.MUTED, can_grow=True, visible=c.exists(one))
    p.y += 1.0
    quantity = {**_quantity(), "header": t["quantity"], "width": 60}
    cols = [quantity] + [_number(s, 25, t[s]) for s in ("orig_mean", "orig_sd", "orig_q5", "orig_q50", "orig_q95")]
    p.add(stats.table("original", t["original"], one, cols, p.y, visible=c.exists(one)), 10)


# ── page 3: the data ────────────────────────────────────────────────────────
def _data(p: stats.Page, w: dict) -> None:
    t, kinds = w["tables"], w["data_types"]
    p.heading("s_data", w["sections"]["data"])
    p.text("data_note", w["data_note"], h=5, size=8, color=c.MUTED, can_grow=True)
    p.y += 1.0
    one = '[section] = "data" AND [stat] = "column"'
    kind = c.of_row("data", "type", "text")
    said = "IFS(" + ", ".join(f'{kind} = "{k}", {c.q(v)}' for k, v in kinds.items()) + f", TRUE, {kind})"
    standardized = c.of_row("data", "standardized", "text")
    units, levels = c.of_row("data", "n_units"), c.of_row("data", "n_levels")
    count = f'IF(NOT ISNULL({units}), {c.shown(units, "#,##0")}, IF(NOT ISNULL({levels}), {c.shown(levels, "#,##0")}, ""))'

    def number(header: str, stat: str, width: float) -> dict:
        x = c.of_row("data", stat)
        return {"header": header, "value": f'IF(ISNULL({x}), "", {c.shown(x, "#,##0.####")})', "width": width, "align": "right"}

    cols = [
        {"header": t["variable"], "value": "[level]", "width": 22, "style": {"color": c.MUTED}},
        {"header": t["column"], "value": c.of_row("data", "column", "text"), "style": {"weight": "semibold"}},
        {"header": t["type"], "value": said, "width": 24},
        {"header": t["standardized"], "value": f'IF({standardized} = "yes", {c.q(w["coef"]["yes"])}, {c.q(w["coef"]["no"])})', "width": 17},
        number(t["mean"], "mean", 16), number(t["sd"], "sd", 16),
        {"header": t["count"], "value": count, "width": 18, "align": "right"},
        {"header": t["reference"], "value": f'COALESCE({c.of_row("data", "reference", "text")}, "")', "width": 22},
        number(t["missing"], "n_missing", 16),
    ]
    p.add(stats.table("data", t["data"], one, cols, p.y, visible=c.exists(one)), 10)


# ── the page ────────────────────────────────────────────────────────────────
def layout(model: str, params: dict[str, Any], locale: str, sample: list[dict] | None, step: str | None) -> dict:
    from app.reports import formats

    lang = c.code(locale)
    checked = catalog.check_params(model, dict(params))
    variant = catalog.variant(model, checked)
    w = c.words(model, variant, lang)
    header, footer = stats.bands(name(params, lang), " · ".join(x for x in (about(params, lang), step) if x), w,
                                 formats.locale(lang).texts, stats.sample_line(sample, lang))
    p = stats.Page()
    _summary(p, w, variant)
    _posterior(p, w, variant)
    _data(p, w)
    c.how_computed(p, w)
    c._notes(p, w)
    return c.document(header, p.items, footer, lang)


register("bayes_model", layout, name, about)
