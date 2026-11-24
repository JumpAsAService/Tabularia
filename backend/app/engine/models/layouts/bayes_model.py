"""The report of the node «Bayesian model» (docs/specs/bayes-report.md): drawn from the ten-column rows a run of the group `bayes` wrote
(docs/specs/bayes-contract.md, «Sections») and from nothing else, in both variants, `sample` and `prior_predictive`.

The first lot (Q1) is the pages that need no chart and no reading; the second (Q2) the groups' page, the diagnostics and the charts.

1. Summary: four cards (the rows used, the draws, the divergences, the largest R-hat), the facts line (the saved model and its version, the
   sampler, the seed, the precision of the draws, the seconds). The divergences are a count and every diagnostic a measure or a fact: no
   mark, no colour that judges, no word of verdict (answer 11 of the map). The prior predictive has three cards (it has no sampler to
   report on) and its own facts line.
2. The posterior summary (`sample`): one row a scalar quantity or correlation, with its mean, sd, three quantiles, R-hat, bulk and tail ESS,
   the standard error of the mean and the contraction (empty where the prior has no finite variance), then the same coefficients of a
   linear predictor on the column's scale (`orig_*`). The prior predictive's summary: mean, sd and the five quantiles of each quantity's
   prior draws. A plate's quantities are the groups' page; the Excel file's sheet holds every row of the scope (`only`: «sheet»).
3. Data: each bound variable, its column, its type, whether it was standardized, with its mean and sd, the units or levels and the
   reference level of a category, the values left out.
4. «How this report was computed» (`common.how_computed`: its shell, the readings are lot Q4's) and the notes.

5. The groups (Q2): a plate's quantities as a caterpillar, each unit's median and its 5-95 % interval in the order of the medians.
6. The diagnostics (Q2): the divergences as a fact, the sampler's table of each chain, a paragraph for each measure saying what it measures
   (nothing is compared with a value of the literature: those are quoted in «How this report was computed»), the table of each node.
7. The charts (Q2): for each quantity of the scope its trace, its rank histograms and its density with the prior's; the pairs with the
   divergent draws marked. The charts' ids are data (a quantity's name, a unit's text): `SLOTS` gives each one its place in the scope
   (`trace:3`) and the layout defines the fixed number of places, each drawn only when its rows exist.

Every number is read from the rows by its key. A label, a unit, a column is data: it goes through the expressions as a value, never as a
template. The predictive checks are lot Q3's, the readings and the literature lot Q4's.
"""
from __future__ import annotations

import math
from typing import Any

from app.engine.models import catalog
from app.engine.models.layouts import common as c
from app.engine.models.layouts import register
from app.engine.stats import layouts as stats
from app.reports import glyphs

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
    # the pages print the scalars and the correlations (a plate's quantities are the groups' page); the Excel file's sheet holds every row
    # of the scope, with the unit of each (owner, 2026-11-24)
    pages = stats.table("posterior", t[key], scope, cols, p.y, visible=c.exists(scope))
    pages["only"] = "pages"
    sheet = stats.table("posterior_sheet", t[key + "_sheet"], SUMMARY, cols, p.y, visible=c.exists(SUMMARY))
    sheet["only"] = "sheet"
    p.add(pages, 10)
    p.items.append(sheet)
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


# ── the charts' places ──────────────────────────────────────────────────────
# The runner's charts have the quantity's name in their id (`trace:alpha_school[Liceo Galilei]`): data, which a definition written before
# the rows are seen cannot name. `slots` (common.SLOTS) gives each its place in the scope's order, as many as the contract's limits allow
# (`bayes_max_charts`, `bayes_max_pairs`, `bayes_max_unit_charts`), and says in the section `slot` what each place holds.
QUANTITY_SLOTS, PAIR_SLOTS, UNIT_SLOTS = 12, 6, 8
BY_QUANTITY = ("trace", "rank", "density")


def _label_of(name: str, labels: dict[str, str]) -> str:
    """A quantity as the page says it: the label of its node (its identifier when it has none), and an element's unit after it."""
    head, bracket, rest = name.partition("[")
    if bracket and rest.endswith("]") and head in labels:
        return f"{labels[head]} — {rest[:-1]}"
    return labels.get(name, name)


def slots(rows: list[dict]) -> list[dict]:
    """The rows with each chart's id replaced by its place (`trace:3`, `rank:3`, `density:3`: the k-th quantity of the scope, the same for
    the three; `pairs:2`; `units:1`), and a row `slot|<place>||label` (its text: what the place holds) after them, `slot|units:k||total`
    (the units of the plate: the chart may show a sample of them). A chart beyond the places keeps its id and is not drawn."""
    if not any(r["section"] in ("curve", "hist", "point") for r in rows):
        return rows
    labels = {r["level"]: r["text"] for r in rows if r["section"] == "quantity" and r["stat"] == "label" and r.get("text")}
    totals = {r["level"]: r["value"] for r in rows if r["section"] == "quantity" and r["stat"] == "n_quantities"}
    plates = {r["level"]: r["text"] for r in rows if r["section"] == "quantity" and r["stat"] == "plate_label" and r.get("text")}
    order = lambda r: r["ord"] if isinstance(r.get("ord"), (int, float)) else math.inf  # noqa: E731
    first: dict[str, float] = {}  # a quantity's place: where its first chart of the three kinds is
    pairs: dict[str, float] = {}
    units: dict[str, float] = {}
    for r in rows:
        kind, _, name = r["level"].partition(":") if r["section"] in ("curve", "hist", "point") else ("", "", "")
        if kind in BY_QUANTITY:
            first[name] = min(first.get(name, math.inf), order(r))
        elif kind == "pairs":
            pairs[name] = min(pairs.get(name, math.inf), order(r))
        elif kind == "units":
            units[name] = min(units.get(name, math.inf), order(r))
    place = {n: k for k, n in enumerate(sorted(first, key=first.__getitem__)[:QUANTITY_SLOTS], 1)}
    pair_place = {n: k for k, n in enumerate(sorted(pairs, key=pairs.__getitem__)[:PAIR_SLOTS], 1)}
    unit_place = {n: k for k, n in enumerate(sorted(units, key=units.__getitem__)[:UNIT_SLOTS], 1)}
    ids = set(labels) | set(first)

    def pair_said(rest: str) -> str:
        """«a:b» as «a × b» (a name may hold a colon: the split is at the colon after which a known name follows)."""
        for i, ch in enumerate(rest):
            if ch == ":" and rest[:i] in ids and rest[i + 1:] in ids:
                return f"{_label_of(rest[:i], labels)} × {_label_of(rest[i + 1:], labels)}"
        return rest.replace(":", " × ", 1)

    out: list[dict] = []
    for r in rows:
        level = r["level"]
        kind, _, name = level.partition(":") if r["section"] in ("curve", "hist", "point") else ("", "", "")
        new = (f"{kind}:{place[name]}" if kind in BY_QUANTITY and name in place else
               f"pairs:{pair_place[name]}" if kind == "pairs" and name in pair_place else
               f"units:{unit_place[name]}" if kind == "units" and name in unit_place else None)
        if new is None:
            out.append(r)
            continue
        key = r.get("key")
        if isinstance(key, str) and key.startswith(f"{r['section']}|{level}|"):
            key = f"{r['section']}|{new}|" + key[len(f"{r['section']}|{level}|"):]
        out.append({**r, "level": new, "key": key})
    last = max((r["ord"] for r in rows if isinstance(r.get("ord"), int)), default=-1)

    def slot(id_: str, stat: str, text: str | None, value: float | None = None) -> dict:
        nonlocal last
        last += 1
        return {"section": "slot", "level": id_, "level_2": None, "stat": stat, "value": value, "text": text, "x0": None, "x1": None,
                "ord": last, "key": f"slot|{id_}||{stat}"}

    for name, k in place.items():
        out.append(slot(f"trace:{k}", "label", _label_of(name, labels)))
    for name, k in pair_place.items():
        out.append(slot(f"pairs:{k}", "label", pair_said(name)))
    for name, k in unit_place.items():
        said = labels.get(name, name)
        out.append(slot(f"units:{k}", "label", f"{said} ({plates[name]})" if name in plates else said))
        out.append(slot(f"units:{k}", "total", None, totals.get(name)))
    return out


c.SLOTS["bayes_model"] = slots


# ── what a place of the charts says and draws ───────────────────────────────
GAP = 2.0  # mm between two charts of a row
LABEL = 4.8  # the line of text above a chart: what the place holds
CHAIN = '{chain} & " " & [level_2]'


def _para(p: stats.Page, id_: str, text: str, size: float = 8.0, color: str = c.MUTED, visible: str | None = None, x: float = 0.0,
          w: float = stats.WIDTH, **style: Any) -> float:
    """A paragraph of fixed words, as high as its lines are (the page measures them in the report's font): what follows it is placed
    under it, a text that grows would not move it. Returns its height."""
    lines = max(1, math.ceil(glyphs.width_mm(text, size) / (w - 1.5)))
    h = round(size * 0.4 * lines + 1.4, 2)
    item: dict[str, Any] = {"id": id_, "type": "text", "x": x, "y": p.y, "w": w, "h": h, "value": stats.static(text),
                            "style": {"size": size, "color": color, **style}}
    if visible:
        item["visible"] = visible
        item["collapse_hidden"] = True
    p.items.append(item)
    p.y += h
    return h


def _hide_unless(p: stats.Page, since: int, visible: str) -> None:
    """The items added since `since` (a heading and the words under it) are there only when `visible`, and take no room when they are not."""
    for item in p.items[since:]:
        item["visible"] = visible
        item["collapse_hidden"] = True
        item["keep_with_next"] = True  # a page never ends with the heading and its words: they go with the first row of charts


def _chart(p: stats.Page, chart: c.Chart, x: float, y: float, w: float, h: float) -> None:
    item: dict[str, Any] = {"id": chart.id, "type": "chart", "dataset": c.DATASET, "kind": chart.kind, "x": round(x, 2), "y": round(y, 2),
                            "w": round(w, 2), "h": h, "title": stats.fit(chart.title, 7.5, w - 4, 700), "collapse_hidden": True, **chart.more}
    p.items.append(item)


def _place_label(p: stats.Page, id_: str, place: str, y: float, h: float, visible: str, w: float = stats.WIDTH, x: float = 0.0) -> None:
    """What the place holds (the quantity's label and its unit, a pair, a plate), from the slot's row; the item is as tall as its row so
    that the charts under it are one row with it (the engine never breaks a row of items)."""
    p.items.append({"id": id_, "type": "text", "x": x, "y": round(y, 2), "w": w, "h": round(h, 2), "visible": visible, "collapse_hidden": True,
                    "value": "=" + c.txt(f"slot|{place}||label"), "style": {"size": 8, "weight": "semibold", "color": stats.INK}})


def _has(place: str) -> str:
    return c.exists(f'[key] = "slot|{place}||label"')


def _intro(p: stats.Page, w: dict, ids: tuple[str, ...], visible: str | None = None) -> None:
    """How to read each kind of chart of the page: a line each, in words that say what is drawn and never what it proves."""
    for kind in ids:
        _para(p, f"how_{kind}", w["charts"]["how"][kind], 7.5, visible=visible)
    p.y += 1.5


# ── page 3 (here after the posterior summary): the groups ───────────────────
def _units_chart(w: dict, k: int) -> c.Chart:
    f = f'[section] = "point" AND [level] = "units:{k}"'
    ch = w["charts"]
    series = [{"value": f'SUMIF([level_2] = "{s}", [value])', "name": ch[s]} for s in ("median", "low", "high")]
    more = {"filter": f, "category": "[text]", "series": series, "options": {"format": "0.###", "labels": False}, "visible": c.exists(f)}
    return c.Chart(f"units_{k}", "interval", ch["units"], more)


def _groups(p: stats.Page, w: dict) -> None:
    g = w["groups"]
    since = len(p.items)
    p.heading("s_groups", w["sections"]["groups"])
    _para(p, "groups_note", g["note"], 8.0)
    _para(p, "groups_how", w["charts"]["how"]["units"], 7.5)
    p.y += 1.0
    _hide_unless(p, since, c.exists('[section] = "point" AND STARTSWITH([level], "units:")'))
    tall, half = 54.0, (stats.WIDTH - GAP) / 2
    for k in range(1, UNIT_SLOTS + 1):
        column, top = (k - 1) % 2, p.y
        place = f"units:{k}"
        x = column * (half + GAP)
        row = LABEL + tall + 5.0
        _place_label(p, f"units_label_{k}", place, top, row, _has(place), w=half, x=x)
        _chart(p, _units_chart(w, k), x, top + LABEL, half, tall)
        f = f'[section] = "point" AND [level] = "{place}" AND [level_2] = "median"'
        shown, total = f"COUNTIF({f})", c.num(f"slot|{place}||total")
        p.items.append({"id": f"units_sampled_{k}", "type": "text", "x": round(x, 2), "y": round(top + LABEL + tall + 0.4, 2), "w": half, "h": 4.4,
                        "visible": f"={shown} < {total}", "collapse_hidden": True,
                        "value": "=" + c.join(g["sampled"], shown=c.shown(shown, "#,##0"), total=c.shown(total, "#,##0")),
                        "style": {"size": 7, "color": c.MUTED}})
        if column == 1 or k == UNIT_SLOTS:
            p.y = top + row + 1.0
        # a row of two: the next chart starts at the same height


# ── the diagnostics ─────────────────────────────────────────────────────────
def _sampler_value(stat: str, pattern: str, width: float, header: str) -> dict:
    x = c.of_row("sampler", stat)
    return {"header": header, "value": f'IF(ISNULL({x}), "", {c.shown(x, pattern)})', "width": width, "align": "right"}


def _diagnostics(p: stats.Page, w: dict) -> None:
    d, t = w["diagnostics"], w["tables"]
    p.heading("s_diagnostics", w["sections"]["diagnostics"])
    _para(p, "diagnostics_note", d["note"], 8.0)
    # the divergences are a fact, said first and plain: a count of the draws after the warm-up
    div, total = c.num("sampler|all||divergences"), f'({c.model_number("chains")} * {c.model_number("draws")})'
    hits = c.num("sampler|all||treedepth_hits")
    p.y += 1.0
    p.text("diagnostics_divergences", "=" + c.join(d["divergences_fact"], n=c.shown(div, "#,##0"), total=c.shown(total, "#,##0")), h=5.5,
           size=10, weight="semibold", color=stats.INK, visible=c.exists('[key] = "sampler|all||divergences"'))
    p.text("diagnostics_depth", "=" + c.join(d["treedepth_fact"], n=c.shown(hits, "#,##0"), depth=c.shown(c.model_number("max_treedepth"), "0")),
           h=5.0, size=8.5, color=stats.INK, visible=c.exists('[key] = "sampler|all||treedepth_hits"'))
    p.y += 1.0
    one = '[section] = "sampler" AND [stat] = "divergences"'
    chain = f'IF([level] = "all", {c.q(t["all_chains"])}, [level])'
    cols = [{"header": t["chain"], "value": chain, "width": 22, "style": {"weight": "semibold"}},
            _sampler_value("divergences", "#,##0", 20, t["divergences"]), _sampler_value("treedepth_hits", "#,##0", 22, t["treedepth_hits"]),
            _sampler_value("treedepth_max", "#,##0", 18, t["treedepth_max"]), _sampler_value("e_bfmi", "0.000", 16, t["e_bfmi"]),
            _sampler_value("stepsize", "0.####", 16, t["stepsize"]), _sampler_value("accept_stat_mean", "0.000", 20, t["accept_stat_mean"]),
            _sampler_value("n_leapfrog_mean", "#,##0.#", 18, t["n_leapfrog_mean"]),
            _sampler_value("seconds_warmup", "#,##0.#", 17, t["seconds_warmup"]), _sampler_value("seconds_sampling", "#,##0.#", 17, t["seconds_sampling"])]
    p.add(stats.table("diagnostics_sampler", t["sampler"], one, cols, p.y, visible=c.exists(one)), 10)
    # what each measure measures: a paragraph each, its name beside it
    p.y += 1.5
    for key in ("rhat", "ess", "mcse", "divergences", "treedepth", "ebfmi", "adaptation"):
        words = d["measures"][key]
        h = _para(p, f"measure_{key}", words["says"], 8.0, color=stats.INK, x=44.0, w=stats.WIDTH - 44.0)
        p.items.append({"id": f"measure_{key}_name", "type": "text", "x": 0, "y": round(p.y - h, 2), "w": 42, "h": h, "value": stats.static(words["name"]),
                        "style": {"size": 8, "weight": "semibold", "color": stats.INK}})
        p.y += 0.8
    nodes = '[section] = "quantity" AND [stat] = "n_quantities"'
    label = {**_quantity(), "header": t["quantity"], "width": 56}
    cols = [label, {"header": t["plate"], "value": 'COALESCE(LOOKUP("quantity|" & [level] & "||plate_label", [key], [text]), "")', "width": 30},
            {"header": t["count"], "value": c.shown(c.of_row("quantity", "n_quantities"), "#,##0"), "width": 20, "align": "right"}]
    for stat, header, pattern in (("rhat_max", t["rhat_max"], "0.000"), ("ess_bulk_min", t["ess_bulk_min"], "#,##0"), ("ess_tail_min", t["ess_tail_min"], "#,##0")):
        x = c.of_row("quantity", stat)
        cols.append({"header": header, "value": f'IF(ISNULL({x}), "", {c.shown(x, pattern)})', "width": 26.667, "align": "right"})
    p.y += 1.0
    p.add(stats.table("diagnostics_nodes", t["nodes"], nodes, cols, p.y, visible=c.exists(nodes)), 10)


# ── the charts ──────────────────────────────────────────────────────────────
def _trace_chart(w: dict, k: int) -> c.Chart:
    ch = w["charts"]
    f = f'[section] = "curve" AND [level] = "trace:{k}"'
    return c.xy(f"trace_{k}", ch["trace"], f, [c.mark("value", "line")], {"legend": True}, split_by=CHAIN.format(chain=c.q(ch["chain"])))


def _rank_chart(w: dict, k: int) -> c.Chart:
    ch = w["charts"]
    f = f'[section] = "hist" AND [level] = "rank:{k}"'
    return c.xy(f"rank_{k}", ch["rank"], f, [c.mark("count", "step")], {"legend": False, "format": "#,##0"},
                split_by=CHAIN.format(chain=c.q(ch["chain"])))


def _density_chart(w: dict, k: int) -> c.Chart:
    ch = w["charts"]
    f = f'[section] = "curve" AND [level] = "density:{k}"'
    series = [{"value": 'IF([level_2] = "posterior", [value], NULL)', "as": "line", "name": ch["posterior"]},
              {"value": 'IF([level_2] = "prior", [value], NULL)', "as": "dashed", "name": ch["prior"]}]
    return c.xy(f"density_{k}", ch["density"], f, series, {"legend": True})


def _pairs_chart(w: dict, k: int) -> c.Chart:
    ch = w["charts"]
    f = f'[section] = "curve" AND [level] = "pairs:{k}"'
    series = [{"value": 'IF([level_2] = "draw", [value], NULL)', "as": "point", "name": ch["draws"]},
              {"value": 'IF([level_2] = "divergent", [value], NULL)', "as": "point", "name": ch["divergent"]}]
    return c.xy(f"pairs_{k}", ch["pairs"], f, series, {"legend": True})


def _charts(p: stats.Page, w: dict) -> None:
    ch = w["charts"]
    since = len(p.items)
    p.heading("s_charts", w["sections"]["charts"])
    _intro(p, w, ("trace", "rank", "density"))
    _hide_unless(p, since, c.exists('[section] = "curve" AND STARTSWITH([level], "trace:")'))
    tall, third = 46.0, (stats.WIDTH - 2 * GAP) / 3
    for k in range(1, QUANTITY_SLOTS + 1):
        top, place = p.y, f"trace:{k}"
        row = LABEL + tall + 2.0
        _place_label(p, f"quantity_label_{k}", place, top, row, _has(place))
        for i, chart in enumerate((_trace_chart(w, k), _rank_chart(w, k), _density_chart(w, k))):
            _chart(p, chart, i * (third + GAP), top + LABEL, third, tall)
        p.y = top + row
    p.y += 2.0
    # the pairs: two a row
    since = len(p.items)
    p.heading("s_pairs", w["sections"]["pairs"])
    _para(p, "how_pairs", ch["how"]["pairs"], 7.5)
    p.y += 1.0
    _hide_unless(p, since, c.exists('[section] = "curve" AND STARTSWITH([level], "pairs:")'))
    tall, half = 58.0, (stats.WIDTH - GAP) / 2
    for k in range(1, PAIR_SLOTS + 1):
        column, top, place = (k - 1) % 2, p.y, f"pairs:{k}"
        x = column * (half + GAP)
        row = LABEL + tall + 2.0
        _place_label(p, f"pairs_label_{k}", place, top, row, _has(place), w=half, x=x)
        _chart(p, _pairs_chart(w, k), x, top + LABEL, half, tall)
        if column == 1 or k == PAIR_SLOTS:
            p.y = top + row + 1.0


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
    if variant == "sample":
        _groups(p, w)
    _data(p, w)
    if variant == "sample":
        _diagnostics(p, w)
        _charts(p, w)
    c.how_computed(p, w)
    c._notes(p, w)
    return c.document(header, p.items, footer, lang)


register("bayes_model", layout, name, about)
