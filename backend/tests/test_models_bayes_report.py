"""The report of the node «Bayesian model» (docs/specs/bayes-report.md, lots Q1 to Q4: the pages that need no reading, the groups, the
diagnostics and the charts, the predictive checks, «How this report was computed», the notes of the runner and the literature),
drawn from the two fixtures (`bayes_model-sample.json`, `bayes_model-prior_predictive.json`, made by the runner's writers on fake draws:
gateway-go/internal/modelgroups/groups/bayes/reference/make_report_fixtures.py), in the five languages and the three formats."""
from __future__ import annotations

import json
import re
import zipfile

import pytest

import tests.test_models_layouts as me
from app.engine.models import catalog, layouts
from app.engine.models.layouts import common
from app.engine.stats.layouts import LOCALES
from tests.test_models_layouts import draw, fixture, strings
from tests.test_reports_render import chart_service  # noqa: F401 — the fake chart service

SAMPLE, PRIOR = "bayes_model-sample", "bayes_model-prior_predictive"
QUOTATION = re.compile(r"“[^”]*”")  # what the literature says, quoted as its source writes it


def said(tmp_path, chart_service, monkeypatch, case, locale="en"):  # noqa: F811
    _, page = draw(tmp_path, chart_service, monkeypatch, case, fmt="svg", locale=locale)
    return set(strings(json.loads(page)))


def test_the_summary_holds_the_rows_the_draws_the_divergences_and_the_measures_of_the_chains(tmp_path, chart_service, monkeypatch):  # noqa: F811
    texts = said(tmp_path, chart_service, monkeypatch, SAMPLE)
    w = common.words("bayes_model", "sample", "en")
    k = w["kpi"]
    for text in (w["sections"]["summary"], k["rows"], "588", "12 left out", k["draws"], "4 × 250", "chains × draws, after 500 warm-up iterations",
                 k["divergences"], "2", "of 1,000 draws", k["rhat"]):
        assert any(text in s for s in texts), text
    # the facts line: the saved model and its version, the sampler, the seed, the precision, the seconds
    [facts] = [s for s in texts if s.startswith("Saved model 17, version 4")]
    for part in ("sampler NUTS", "diagonal metric", "target acceptance 0.8", "tree depth up to 10", "seed 20261123", "8 significant digits",
                 "15 s of warm-up", "18 s of sampling"):
        assert part in facts, part
    # R-hat and the ESS are measures, plain: a number under each card, nothing that judges
    assert any(s.startswith("smallest ESS: bulk ") and "tail " in s for s in texts)


def test_the_posterior_table_holds_the_scalars_and_the_correlations_with_their_labels_and_an_empty_contraction_said_by_a_dash(
        tmp_path, chart_service, monkeypatch):  # noqa: F811
    texts = said(tmp_path, chart_service, monkeypatch, SAMPLE)
    for label in ("Mean of the intercepts", "Spread of the intercepts", "Effect of the hours studied", "Residual spread",
                  "Correlation of the intercepts and the slopes"):
        assert label in texts, label
    assert "b_g,a_g" in texts and "—" in texts  # the correlation's unit, the contraction the run left empty
    # a plate's quantities are the groups' page (and the Excel sheet's): not in this table (the groups' page names them as charts' captions)
    for column in ("Mean", "Sd", "5 %", "Median", "95 %", "R-hat", "ESS bulk", "ESS tail", "MCSE of the mean", "Contraction"):
        assert column in texts, column


def test_the_coefficient_of_a_linear_predictor_is_also_given_on_the_column_s_scale_and_nothing_else_is(tmp_path, chart_service, monkeypatch):  # noqa: F811
    texts = said(tmp_path, chart_service, monkeypatch, SAMPLE)
    note = common.words("bayes_model", "sample", "en")["original_note"]  # (the tables' titles are the workbook's sheets, not the page's text)
    assert note in texts
    _, _, rows = fixture(SAMPLE)
    assert {r["level"] for r in rows if r["stat"] == "orig_mean"} == {"b_hours", "alpha_school"}  # the runner's own: the coefficient and the intercepts
    # the standardization is the data's: the mean and the sd of the predictor, said once in the table of the bound variables
    assert {"hours_studied", "exam_score", "school_name", "track"} <= texts and "Scientific" in texts and "6.21" in texts and "2.4" in texts
    # without the rows there is no such table
    without = [r for r in rows if not r["stat"].startswith("orig_")]
    _, page = draw(tmp_path / "x", chart_service, monkeypatch, SAMPLE, fmt="svg", rows=without)
    assert note not in set(strings(json.loads(page)))


def test_the_prior_predictive_has_three_cards_the_priors_summary_and_no_sampler_to_report_on(tmp_path, chart_service, monkeypatch):  # noqa: F811
    texts = said(tmp_path, chart_service, monkeypatch, PRIOR)
    w = common.words("bayes_model", "prior_predictive", "en")
    k = w["kpi"]
    for text in (k["rows"], k["draws_prior"], "4 × 250", k["draws_prior_under"], k["quantities"], "11", w["posterior_note"]["prior_predictive"]):
        assert text in texts, text
    assert not {k["divergences"], k["rhat"], "R-hat", "ESS bulk", "Contraction", w["original_note"]} & texts
    [facts] = [s for s in texts if s.startswith("Saved model 17, version 4")]
    assert "draws from the priors alone, before the data are seen" in facts and "fixed parameters" in facts and "warm-up," in facts
    assert "Mean of the intercepts" in texts and "25 %" in texts and "75 %" in texts


@pytest.mark.parametrize("case", [SAMPLE, PRIOR])
@pytest.mark.parametrize("locale", LOCALES)
def test_no_word_of_judgement_in_any_language_and_the_words_are_the_pages(tmp_path, chart_service, monkeypatch, case, locale):  # noqa: F811
    """A diagnostic is a measure or a fact: no verdict word in what the layout writes (the common list, and the Bayesian model's own
    words of the spec: «converged», «reliable», «acceptable»…, in the five languages by lot Q4)."""
    texts = said(tmp_path, chart_service, monkeypatch, case, locale)
    w = common.words("bayes_model", case.split("-")[1], locale)
    # the spec's own list of verdict words (docs/specs/bayes-report.md, «The words») is in the model's `verdict_words`, over the common list
    assert {"converged", "reliable", "acceptable", "problem"} <= set(common.words("bayes_model", "sample", "en")["verdict_words"])
    own: list[str] = []
    # what the runner and the person wrote (a label, a unit, a column, a note) is data: the layout's own words are what is checked
    # (the precision of the draws, «8 significant digits», is the runner's phrase said in the page's language: a count of digits, not a
    # statistical «significant»: the words of `values` are left out of the scan)
    own_words = {k: v for k, v in common._file("bayes_model")[locale].items() if k not in ("values", "verdict_words", "measure_readings")}
    ours = {t for t in strings(own_words) if not t.startswith("{")}
    checked = [t for t in ours if t in texts or any(t in s for s in texts)]
    assert checked, "none of the layout's words reached the page"
    for text in checked:
        # a quotation of the literature is its authors' words, in English in every language (docs/specs/bayes-report.md, «The words»): the
        # sentences around it are the layout's
        mine = QUOTATION.sub("", text)
        for verdict in [*w["verdict_words"], *own]:
            assert not re.search(r"(?<!\w)" + re.escape(verdict) + r"(?!\w)", mine, re.I), (locale, verdict, text)


def test_a_label_a_unit_and_a_column_that_look_like_a_formula_or_markup_stay_text_in_the_pdf_and_in_excel(tmp_path, chart_service, monkeypatch):  # noqa: F811
    hostile = '=HYPERLINK("http://x","y") #set ] 1% <b>'
    model, params, rows = fixture(SAMPLE)
    out = []
    for r in rows:
        if r["section"] == "quantity" and r["level"] == "mu_a" and r["stat"] == "label":
            r = {**r, "text": hostile}
        elif r["section"] == "summary" and r["level"] == "rho_g" and r["level_2"]:
            r = {**r, "level_2": hostile, "key": r["key"].replace("b_g,a_g", hostile)}
        elif r["section"] == "data" and r["level"] == "hours" and r["stat"] == "column":
            r = {**r, "text": hostile}
        out.append(r)
    monkeypatch.setattr(me, "fixture", lambda case: (model, params, out))
    texts = said(tmp_path / "svg", chart_service, monkeypatch, SAMPLE)
    assert hostile in texts
    pdf, _ = draw(tmp_path / "pdf", chart_service, monkeypatch, SAMPLE)
    # (a name of forty characters is wider than its column: the engine says so, and that is all it says)
    assert pdf.path.read_bytes().startswith(b"%PDF-") and all("wider than their column" in x for x in pdf.warnings)
    xlsx, _ = draw(tmp_path / "xlsx", chart_service, monkeypatch, SAMPLE, fmt="xlsx")
    with zipfile.ZipFile(xlsx.path) as z:
        xml = " ".join(z.read(n).decode() for n in z.namelist() if n.startswith("xl/worksheets/") or n == "xl/sharedStrings.xml")
    assert "HYPERLINK" in xml and not re.search(r"<f>[^<]*HYPERLINK", xml)
    assert not [f for f in re.findall(r"<f>([^<]+)</f>", xml) if not f.startswith("SUBTOTAL(")]


def test_the_node_is_a_model_of_the_catalogue_with_a_report_and_the_title_says_the_run(tmp_path):
    assert "bayes_model" in layouts.LAYOUTS and catalog.MODELS["bayes_model"]["group"] == "bayes"
    assert layouts.title("bayes_model", {}, "en") == "Bayesian model — posterior sampling"
    assert layouts.title("bayes_model", {"mode": "prior_predictive"}, "it") == "Modello bayesiano — estrazioni dalle sole distribuzioni a priori"
    # the rows are the contract's: the checks accept both fixtures, the keys are what the layout reads
    for case in (SAMPLE, PRIOR):
        model, params, rows = fixture(case)
        keys = {r["key"] for r in rows}
        assert {"model|||n", "model|||chains", "model|||draws"} <= keys
        assert ("sampler|all||divergences" in keys) == (case == SAMPLE)


# ── lot Q2: the charts' places, the groups, the diagnostics, the charts, the Excel sheet ────────────────────────────────────────
def _row(section, level, level_2, stat, value=None, text=None, ord_=0, x0=None):
    return {"section": section, "level": level, "level_2": level_2, "stat": stat, "value": value, "text": text, "x0": x0, "x1": None, "ord": ord_,
            "key": f"{section}|{level}|{level_2 or ''}|{stat}|{ord_}"}


def test_the_slots_give_each_chart_its_place_in_the_scope_and_say_what_the_place_holds():
    _, _, rows = fixture(SAMPLE)
    out = common.SLOTS["bayes_model"](rows)
    levels = {r["level"] for r in out if r["section"] in ("curve", "hist", "point")}
    # the places: 12 quantities (trace, rank, density the same k), pairs 1…6, units 1…
    n = len({r["level"] for r in rows if r["section"] == "curve" and r["level"].startswith("trace:")})
    assert {f"{k}:{i}" for k in ("trace", "rank", "density") for i in range(1, min(n, 12) + 1)} <= levels
    assert {f"pairs:{i}" for i in range(1, 7)} <= levels and {"units:1"} <= levels
    assert not [lv for lv in levels if lv.count(":") > 1 or "[" in lv or lv in ("trace:mu_a",)]
    slot = {r["level"] + "|" + r["stat"]: r for r in out if r["section"] == "slot"}
    assert slot["trace:1|label"]["text"] == "Mean of the intercepts" and slot["trace:1|label"]["key"] == "slot|trace:1||label"
    assert slot["pairs:1|label"]["text"] == "Mean of the intercepts × Spread of the intercepts"
    assert slot["units:1|label"]["text"] == "Intercept of the school (School)" and slot["units:1|total"]["value"] == 6
    # the chart's rows keep everything else; the key follows the id
    [first] = [r for r in out if r["level"] == "trace:1" and r["level_2"] == "1" and r["x0"] == 1.0]
    assert first["key"].startswith("curve|trace:1|1|value|")
    # an element of a plate says its unit after the quantity's label
    assert any(r["text"].startswith("Intercept of the school — ") for r in out if r["section"] == "slot" and r["stat"] == "label")


def test_a_chart_beyond_the_places_keeps_its_id_and_a_name_with_a_colon_is_a_pair_all_the_same():
    rows = [_row("quantity", "a:b", None, "label", text="Alpha"), _row("quantity", "c", None, "label", text="Gamma")]
    ordn = 1
    for k in range(14):
        rows.append(_row("curve", f"trace:q{k}", "1", "value", 1.0, ord_=ordn, x0=1.0))
        ordn += 1
    rows.append(_row("curve", "trace:a:b", "1", "value", 1.0, ord_=ordn, x0=1.0))
    rows.append(_row("curve", "trace:c", "1", "value", 1.0, ord_=ordn + 1, x0=1.0))
    rows.append(_row("curve", "pairs:a:b:c", "draw", "value", 1.0, ord_=ordn + 2, x0=1.0))
    out = common.SLOTS["bayes_model"](rows)
    assert {r["level"] for r in out if r["section"] == "curve" and r["level"].startswith("trace:")} >= {"trace:12", "trace:a:b", "trace:c"}
    [pair] = [r for r in out if r["section"] == "slot" and r["level"] == "pairs:1"]
    assert pair["text"] == "Alpha × Gamma"
    assert not [r for r in out if r["section"] == "slot" and r["level"] == "trace:13"]


def test_the_groups_the_diagnostics_and_the_charts_are_pages_of_the_report(tmp_path, chart_service, monkeypatch):  # noqa: F811
    texts = said(tmp_path, chart_service, monkeypatch, SAMPLE)
    w = common.words("bayes_model", "sample", "en")
    for text in (w["sections"]["groups"], w["groups"]["note"], w["sections"]["diagnostics"], w["diagnostics"]["note"],
                 "2 of the 1,000 draws after the warm-up were divergent transitions.", w["sections"]["charts"], w["sections"]["pairs"],
                 w["charts"]["how"]["trace"], w["charts"]["how"]["rank"], w["charts"]["how"]["density"], w["charts"]["how"]["pairs"],
                 w["charts"]["how"]["units"], "Intercept of the school (School)", "Mean of the intercepts"):
        assert text in texts, text
    for measure in w["diagnostics"]["measures"].values():
        assert measure["name"] in texts and measure["says"] in texts
    # the sampler's table: a row a chain and the whole run, the measures as numbers
    for header in ("Chain", "Divergences", "At largest depth", "Deepest tree", "E-BFMI", "Step size", "Mean acceptance", "Mean leapfrog steps"):
        assert header in texts, header
    assert "All chains" in texts and "0.88" not in texts  # (a number is a cell of its own: 0.880)
    # nothing of the diagnostics is compared with a reference value
    assert not [t for t in texts if re.search(r"1\.01|\b400\b|0\.3\b", t) and t in strings(w["diagnostics"])]


def test_the_charts_are_drawn_a_line_a_chain_the_prior_dashed_and_the_divergent_draws_in_their_own_colour(tmp_path, chart_service, monkeypatch):  # noqa: F811
    model, params, rows = fixture(SAMPLE)
    # two draws of the pair are divergent (the fixture's pairs carry none)
    pair = [r for r in rows if r["section"] == "curve" and r["level"] == "pairs:mu_a:tau_a" and r["level_2"] == "draw"]
    divergent = [{**r, "level_2": "divergent", "key": r["key"].replace("|draw|", "|divergent|")} for r in pair[:2]]
    monkeypatch.setattr(me, "fixture", lambda case: (model, params, [*rows, *divergent]))
    draw(tmp_path, chart_service, monkeypatch, SAMPLE, fmt="svg")
    options = [c["option"] for c in chart_service[1]]
    series = lambda o: [x for x in o.get("series", []) if x.get("data")]  # noqa: E731
    traces = [o for o in options if len(series(o)) == 4 and all(x["type"] == "line" for x in series(o))]
    assert len(traces) >= 12  # a line a chain (the rank histograms are steps, also a line a chain)
    dashed = [o for o in options if any(x.get("lineStyle", {}).get("type") == "dashed" for x in series(o))]
    assert len(dashed) >= 1  # the prior on the density
    names = [{x.get("name") for x in o.get("series", [])} for o in options]
    assert any({"Draws", "Divergent draws"} <= n for n in names)
    assert any(o.get("series") and o["series"][0].get("type") == "boxplot" for o in options)  # the caterpillar of the units


def test_a_plate_s_quantities_are_in_the_excel_sheet_and_the_pages_table_is_not(tmp_path, chart_service, monkeypatch):  # noqa: F811
    xlsx, _ = draw(tmp_path, chart_service, monkeypatch, SAMPLE, fmt="xlsx")
    with zipfile.ZipFile(xlsx.path) as z:
        book = z.read("xl/workbook.xml").decode()
        strings_ = " ".join(z.read(n).decode() for n in z.namelist() if n.startswith("xl/worksheets/"))
    sheets = re.findall(r'<sheet name="([^"]+)"', book)
    w = common.words("bayes_model", "sample", "en")["tables"]
    assert w["posterior_sheet"] in sheets and w["posterior"][:31] not in sheets
    assert "Liceo Galilei" in strings_ and "Istituto Marconi" in strings_  # every quantity of the scope, with its unit
    assert w["sampler"][:31] in sheets and w["nodes"][:31] in sheets
    # the pages' PDF and the screen have the other table
    texts = said(tmp_path / "svg", chart_service, monkeypatch, SAMPLE)


def test_the_layout_places_the_pages_in_their_order(tmp_path):
    body = layouts.definition("bayes_model", {}, "en", None, None)["sections"]["body"]["items"]
    first = {it["id"]: i for i, it in enumerate(body)}
    assert first["s_summary"] < first["s_posterior"] < first["s_groups"] < first["s_data"] < first["s_diagnostics"] < first["s_charts"] \
        < first["s_pairs"] < first["s_how"] < first["s_notes"]
    # a place with no rows takes no room: every item of a place says so
    hidden = [it for it in body if it["id"].startswith(("quantity_label_", "trace_", "rank_", "density_", "pairs_", "units_"))]
    assert hidden and all(it.get("collapse_hidden") for it in hidden)
    prior = layouts.definition("bayes_model", {"mode": "prior_predictive"}, "en", None, None)["sections"]["body"]["items"]
    assert not {"s_groups", "s_diagnostics", "s_charts"} & {it["id"] for it in prior}


# ── lot Q3: the predictive checks ─────────────────────────────────────────────────────────────────────────────────────────────
def _options(chart_service):  # noqa: F811
    """The distinct charts the fake service was handed (it is asked again for a chart the page measures twice)."""
    return list({json.dumps(c["option"], sort_keys=True): c["option"] for c in chart_service[1]}.values())


def _checked(case):
    section = "ppc" if case == SAMPLE else "prior_check"
    curve = "ppc_density" if case == SAMPLE else "prior_density"
    _, _, rows = fixture(case)
    return section, curve, rows


def _outside(rows, curve):
    """The count the page says, from the rows by the arithmetic of the runner: the outer slots' densities times the rows and the bin's width."""
    pts = [r for r in rows if r["section"] == "curve" and r["level"] == f"{curve}:score" and r["level_2"] == "observed"]
    width = pts[2]["x0"] - pts[1]["x0"]
    n = next(r["value"] for r in rows if r["key"] == f"{'ppc' if curve == 'ppc_density' else 'prior_check'}|score||n")  # the outcome's own rows
    draws = sorted({r["level_2"] for r in rows if r["section"] == "curve" and r["level"] == f"{curve}:score" and r["level_2"].startswith("draw_")})
    low = sum(r["value"] for r in rows if r["level"] == f"{curve}:score" and r["level_2"] in draws and r["x0"] == pts[0]["x0"])
    high = sum(r["value"] for r in rows if r["level"] == f"{curve}:score" and r["level_2"] in draws and r["x0"] == pts[-1]["x0"])
    return len(draws), round(low * n * width), round(high * n * width)


@pytest.mark.parametrize("case", [SAMPLE, PRIOR])
def test_the_predictive_checks_are_charts_of_the_density_the_statistics_and_the_units_with_the_data_marked_and_one_count_outside(
        tmp_path, chart_service, monkeypatch, case):  # noqa: F811
    section, curve, rows = _checked(case)
    sample = case == SAMPLE
    texts = said(tmp_path, chart_service, monkeypatch, case)
    w = common.words("bayes_model", "sample" if sample else "prior_predictive", "en")
    k = w["predictive"]
    assert w["sections"]["checks" if sample else "prior_checks"] in texts and k["note"]["sample" if sample else "prior_predictive"] in texts
    assert k["how"]["density"] in texts and k["how"]["statistic"] in texts and k["how"]["units"] in texts
    # the outcome is named by its column, a unit by its text; a statistic by its name (the kind of the outcome decides which are drawn)
    assert "exam_score" in texts and k["units"] in texts and {"Liceo Galilei", "Istituto Marconi"} <= texts
    titles = {o["title"]["text"] for o in _options(chart_service)}  # (a chart's title is its own, not a text of the page)
    for stat in ("mean", "sd", "min", "max", "q10", "q50", "q90"):
        assert k["statistic"][stat] in titles, stat
    assert k["statistic"]["zeros"] not in titles  # (a continuous outcome has no zeros' count: its place takes no room)
    assert k["density"] in titles and k["unit_mean"] in titles
    # the only number the page adds: the replicated values that fell outside the data's range, in the drawn sets
    drawn, below, above = _outside(rows, curve)
    assert drawn == 20 and below > 0 and above > 0
    assert f"In the {drawn} data sets drawn, {below} drawn values lie below the range of the data and {above:,} above it." in texts
    # a drawing: no p-value, no probability, no share, no verdict anywhere in the words of the checks
    from tests.test_models_layouts import strings
    for text in strings(w["predictive"]) + [w["sections"]["checks"], w["sections"]["prior_checks"]]:
        assert not re.search(r"p-value|probab|share|%|pass|fail|reproduce", text, re.I), text


@pytest.mark.parametrize("case", [SAMPLE, PRIOR])
def test_each_histogram_marks_the_data_where_it_stands_and_the_density_is_thick_and_black_over_thin_and_grey(tmp_path, chart_service, monkeypatch, case):  # noqa: F811
    section, curve, rows = _checked(case)
    draw(tmp_path, chart_service, monkeypatch, case, fmt="svg")
    options = _options(chart_service)
    w = common.words("bayes_model", "sample" if case == SAMPLE else "prior_predictive", "en")["predictive"]
    names = {*w["statistic"].values(), w["unit_mean"]}
    bars = [o for o in options if o["title"]["text"] in names and any(x.get("step") == "end" and "areaStyle" in x for x in o.get("series", []))]
    # seven statistics and the six schools' means, a histogram each
    bars = list({(o["title"]["text"], json.dumps(o["series"][0]["data"][:3])): o for o in bars}.values())  # (one chart is handed in several sizes)
    assert len(bars) == 7 + 6
    observed = {round(r["value"], 6) for r in rows if r["section"] == section and r["stat"] == "observed"}
    for o in bars:
        [marks] = [x["markLine"]["data"] for x in o["series"] if "markLine" in x]
        [line] = [m["xAxis"] for m in marks]
        assert round(line, 6) in observed
        bar = o["series"][0]
        assert bar["data"] and all(y >= 0 for _, y in bar["data"]) and "scale" not in o["yAxis"]
        # the bins are those of the rows: the line of the data lies inside the drawn range
        assert bar["data"][0][0] <= line <= bar["data"][-1][0]
    density = [o for o in options if any(x.get("lineStyle", {}).get("width") == 2.5 for x in o.get("series", []))][0]
    thick = [x for x in density["series"] if x.get("lineStyle", {}).get("width") == 2.5 and x["data"]]
    thin = [x for x in density["series"] if x.get("lineStyle", {}).get("width") == 0.6 and x["data"]]
    assert len(thick) == 1 and len(thin) == 20 and thick[0]["z"] > thin[0]["z"]
    assert thick[0]["lineStyle"]["color"] == "#1f2933" and {x["lineStyle"]["color"] for x in thin} == {"#9aa3af"}
    assert density["legend"]["show"] is False and not any("markLine" in x for x in density["series"])


def test_the_slots_of_the_checks_give_an_outcome_its_place_its_statistics_and_its_units_and_a_key_the_charts_find(tmp_path):
    _, _, rows = fixture(SAMPLE)
    out = common.SLOTS["bayes_model"](rows)
    levels = {r["level"] for r in out if r["section"] == "ppc"}
    assert levels == {"o1", "o1:mean", "o1:sd", "o1:min", "o1:max", "o1:q10", "o1:q50", "o1:q90"} | {f"o1:u{j}" for j in range(1, 7)}
    assert "ppc|o1||n" in {r["key"] for r in out}  # the outcome's own row count follows its place
    assert {r["level"] for r in out if r["section"] == "curve" and "density" in r["level"] and "ppc" in r["level"]} == {"ppc_density:o1"}
    slot = {r["key"]: r for r in out if r["section"] == "slot"}
    assert slot["slot|o1||label"]["text"] == "exam_score" and slot["slot|o1:u1||label"]["text"] == "Liceo Galilei"
    # the key follows the id, and a unit's text is not in its key (the chart finds T(y) by the key alone)
    assert "ppc|o1:mean||observed" in {r["key"] for r in out} and "ppc|o1:u1||observed" in {r["key"] for r in out}
    assert {r["level_2"] for r in out if r["level"] == "o1:u1" and r["section"] == "ppc"} == {"Liceo Galilei"}
    # the outer slots of a replicated density say which they are, the observed one is not marked: nothing is computed
    edges = [r for r in out if r["section"] == "curve" and r["level"] == "ppc_density:o1" and r["text"]]
    assert {r["text"] for r in edges} == {"below", "above"} and len(edges) == 40 and all(r["level_2"].startswith("draw_") for r in edges)
    assert min(r["x0"] for r in edges if r["text"] == "above") > max(r["x0"] for r in edges if r["text"] == "below")
    # a fourth outcome is not drawn (its rows keep their id), and a page without checks has no such slots
    base = [r for r in rows if r["section"] == "ppc" and r["level"] == "score:mean"]
    many = [{**r, "level": f"{y}:mean", "key": r["key"].replace("score:", f"{y}:"), "ord": r["ord"] + 1000 * i} for i, y in enumerate("abcd") for r in base]
    levels = {r["level"] for r in common.SLOTS["bayes_model"]([r for r in rows if r["section"] != "ppc"] + many) if r["section"] == "ppc"}
    assert levels == {"o1:mean", "o2:mean", "o3:mean", "d:mean"}
    none = common.SLOTS["bayes_model"]([r for r in rows if r["section"] not in ("ppc", "curve")])
    assert not [r for r in none if r["section"] == "slot" and r["level"].startswith("o")]


def test_a_version_without_checks_has_no_predictive_page_and_the_note_of_left_out_draws_is_the_runner_s(tmp_path, chart_service, monkeypatch):  # noqa: F811
    _, _, rows = fixture(SAMPLE)
    without = [r for r in rows if r["section"] != "ppc" and not (r["level"] or "").startswith("ppc_density")]
    _, page = draw(tmp_path, chart_service, monkeypatch, SAMPLE, fmt="svg", rows=without)
    got = set(strings(json.loads(page)))
    w = common.words("bayes_model", "sample", "en")
    assert w["sections"]["checks"] not in got and w["predictive"]["note"]["sample"] not in got
    # the prior predictive's checks file had draws the program threw: the runner's note is in the notes, the counts are the runner's
    prior = said(tmp_path / "p", chart_service, monkeypatch, PRIOR)
    assert any(t.startswith("3 of the 1,000 draws of the predictive checks were left out: CmdStan wrote") and t.endswith("use the other 997.") for t in prior)


def test_the_checks_come_after_the_pairs_and_before_how_the_report_was_computed():
    for params, before in (({}, "s_pairs"), ({"mode": "prior_predictive"}, "s_data")):
        body = layouts.definition("bayes_model", params, "en", None, None)["sections"]["body"]["items"]
        first = {it["id"]: i for i, it in enumerate(body)}
        assert first[before] < first["s_checks"] < first["s_how"] < first["s_notes"]
        # a place with no rows takes no room, and nothing of the page is past the engine's 2000 mm
        places = [it for it in body if re.match(r"(ppc|prior_check)_\d_|check_(label|unit_label|outside)_|(ppc|prior)_density_", it["id"])]
        assert places and all(it.get("collapse_hidden") for it in places)
        assert max(it["y"] + it["h"] for it in body) < 2000


# ── lot Q4: «How this report was computed», the literature, the notes of the runner ──────────────────────────────────────────────
def _quotes(text: str) -> list[str]:
    return QUOTATION.findall(text)


def test_every_quotation_of_the_literature_is_the_same_bytes_in_the_five_languages_and_in_english():
    words = {lang: common._file("bayes_model")[lang]["reading_texts"] for lang in LOCALES}
    for key, english in words["en"].items():
        quotes = _quotes(english)
        for lang in LOCALES:
            assert _quotes(words[lang][key]) == quotes, (lang, key)
        # a quotation is English, with its author's source said around it in the page's language
        assert all(not re.search(r"[àèéìòùäöüß]", q) for q in quotes), key
    cited = [k for k, v in words["en"].items() if _quotes(v)]
    assert {"method_nuts", "method_rhat_ess", "method_divergences", "method_treedepth", "method_ebfmi", "method_contraction",
            "method_checks", "method_prior_checks"} == set(cited)


def test_the_literature_s_values_are_only_in_the_methods_quotations_and_never_beside_a_number_of_the_run():
    value = re.compile(r"(?<![\d.,])(1\.01|400|0\.3)(?![\d])")
    for lang in LOCALES:
        words = common._file("bayes_model")[lang]
        for path, text in _walk(words):
            if path.startswith("reading_texts.method_"):
                continue
            assert not value.search(text), (lang, path, text)
        # in the method, a cited value is inside a quotation: outside it no sentence compares anything
        for key, text in words["reading_texts"].items():
            assert not value.search(QUOTATION.sub("", text)), (lang, key)
            assert not re.search(r"[<>]", QUOTATION.sub("", text)), (lang, key)


def _walk(node, path=""):
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, dict):
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}.{i}")


@pytest.mark.parametrize("case", [SAMPLE, PRIOR])
@pytest.mark.parametrize("locale", LOCALES)
def test_the_method_is_said_in_the_page_s_language_with_the_runs_numbers_and_the_quotations_in_english(case, locale):
    model, params, rows = fixture(case)
    said = [r for r in layouts.with_labels(model, params, rows, locale) if r["section"] == "reading"]
    method = {r["text"] for r in said if r["level"] == "method"}
    assert said and method
    for r in said:
        assert not re.search(r"@|\{|%2", r["text"]), r["text"]
        assert r["label"] not in ("", r["stat"])
    joined = " ".join(method)
    # the run's own numbers and names, and the program's hashes as the rows hold them
    assert "20261123" in joined and "1f32881e15ac9cf9aa1a3f6ea635a7ccbc03bb678874e7ee220478cd7d1bb6a0" in joined
    assert "CmdStan 2.40.0; stanc3 v2.40.0" in joined and "17" in joined
    english = common._file("bayes_model")["en"]["reading_texts"]
    if case == SAMPLE:
        assert {r["level"] for r in said} == {"method", "formulas"}
        for key in ("method_nuts", "method_rhat_ess", "method_divergences", "method_treedepth", "method_ebfmi", "method_contraction", "method_checks"):
            for q in _quotes(english[key]):
                assert q in joined, key
        assert not [q for q in _quotes(english["method_prior_checks"]) if q in joined]
    else:
        assert {r["level"] for r in said} == {"method"}
        for q in _quotes(english["method_prior_checks"]):
            assert q in joined
        assert not [r for r in said if "R-hat" in r["text"] and "[R-hat]" in r["text"]]


def test_the_method_s_paragraphs_follow_the_rows_and_the_formulas_the_scale():
    model, params, rows = fixture(SAMPLE)
    raw = common.READINGS["bayes_model"](rows, catalog.check_params(model, dict(params)))
    got = [(b, i) for b, _, i, _ in raw]
    assert [i for b, i in got if b == "method"] == ["method_nuts", "method_precision", "method_standardized", "method_original", "method_rhat_ess",
                                                    "method_divergences", "method_treedepth", "method_ebfmi", "method_contraction", "method_checks",
                                                    "method_program"]
    assert [i for b, i in got if b == "formulas"] == ["formula_rhat", "formula_ess", "formula_mcse", "formula_ebfmi", "formula_contraction",
                                                      "formula_original"]
    # no scale rows, no checks, no contraction: their paragraphs go
    bare = [r for r in rows if not r["stat"].startswith("orig_") and r["section"] != "ppc" and r["stat"] != "contraction"]
    raw = [i for _, _, i, _ in common.READINGS["bayes_model"](bare, catalog.check_params(model, dict(params)))]
    assert not {"method_original", "method_checks", "method_contraction", "formula_original", "formula_contraction"} & set(raw)
    pm, pp, prows = fixture(PRIOR)
    raw = [(b, i) for b, _, i, _ in common.READINGS["bayes_model"](prows, catalog.check_params(pm, dict(pp)))]
    assert raw == [("method", "method_prior"), ("method", "method_precision"), ("method", "method_standardized"), ("method", "method_prior_checks"),
                   ("method", "method_program")]


NOTES = {
    "summary_capped": dict(listed=2000, total=6120), "quantities_capped": dict(listed=600, total=900),
    "contraction_null": dict(no_finite_variance=2, no_prior_draws=1, not_decided=0), "original_capped": dict(original=3, capped=2),
    "density_cut": dict(names=["a b", "tau[Liceo, 1]"], posterior=[0, 12], prior=[3, 0]), "no_checks": {},
    "checks_dropped": dict(dropped=3, draws=1000, valid=997), "prior_undrawn_one": dict(n=1, names=["rho"]),
    "prior_undrawn": dict(n=3, names=["a", "b", "c"]), "prior_undrawn_more": dict(n=9, names=list("abcde")),
    "prior_dropped": dict(dropped=2, total=1000), "replicated_dropped": dict(dropped=1, total=4000),
}


@pytest.mark.parametrize("locale", LOCALES)
def test_every_note_of_the_runner_is_a_sentence_of_the_page_s_language_with_its_numbers(locale):
    import importlib.util
    import sys
    from pathlib import Path

    runner = Path(__file__).resolve().parents[2] / "gateway-go/internal/modelgroups/groups/bayes/runner/py/bayes_task.py"
    if not runner.exists():  # (the engine's image holds only the backend)
        pytest.skip("the runner's files are not in this tree")
    sys.path[:0] = [str(runner.parents[4] / "common" / "py")]  # (the runner's own imports)
    spec = importlib.util.spec_from_file_location("bayes_task_q4", runner)
    try:
        module = importlib.util.module_from_spec(spec)
        sys.modules["bayes_task_q4"] = module
        spec.loader.exec_module(module)
    except ImportError:
        pytest.skip("numpy is the runner's")
    note_line = module.note_line
    model, params, rows = fixture(SAMPLE)
    base = [r for r in rows if not r["stat"].startswith("note_")]
    texts = {k: note_line(k, **v) for k, v in NOTES.items()}
    made = [{"section": "model", "level": None, "level_2": None, "stat": f"note_{k}", "value": None, "text": t, "x0": None, "x1": None,
             "ord": 99000 + i, "key": f"model|||note_{k}"} for i, (k, t) in enumerate(texts.items())]
    said = {r["stat"]: r["text"] for r in layouts.with_labels(model, params, base + made, locale) if r["stat"].startswith("note_")}
    assert set(said) == {f"note_{k}" for k in NOTES}
    for stat, text in said.items():
        assert not re.search(r"@|\{|%2", text), (stat, text)
        mine = QUOTATION.sub("", text)
        for verdict in common.words("bayes_model", "sample", locale)["verdict_words"]:
            assert not re.search(r"(?<!\w)" + re.escape(verdict) + r"(?!\w)", mine, re.I), (locale, verdict, text)
    d = said["note_density_cut"]
    assert "a b, tau[Liceo, 1]" in d and "0 / 12" in d and "3 / 0" in d
    assert "2,000" in said["note_summary_capped"].replace(".", ",").replace(" ", ",").replace("\xa0", ",") or "2000" in said["note_summary_capped"] or "2" in said["note_summary_capped"]
    assert "rho" in said["note_prior_undrawn_one"] and "a, b, c" in said["note_prior_undrawn"]


def test_a_note_in_the_old_form_is_still_shown_as_the_runner_wrote_it():
    model, params, rows = fixture(SAMPLE)
    old = {"section": "model", "level": None, "level_2": None, "stat": "note_prior_dropped", "value": None,
           "text": "2 of the 480 prior draws have a quantity that is not a finite number.", "x0": None, "x1": None, "ord": 99999,
           "key": "model|||note_prior_dropped"}
    said = [r["text"] for r in layouts.with_labels(model, params, [*rows, old], "it") if r["stat"] == "note_prior_dropped"]
    assert said == [old["text"]]


def test_the_scale_tables_hold_the_scalars_on_the_pages_and_every_row_with_its_unit_in_excel(tmp_path, chart_service, monkeypatch):  # noqa: F811
    xlsx, _ = draw(tmp_path, chart_service, monkeypatch, SAMPLE, fmt="xlsx")
    with zipfile.ZipFile(xlsx.path) as z:
        book = z.read("xl/workbook.xml").decode()
    sheets = re.findall(r'<sheet name="([^"]+)"', book)
    t = common.words("bayes_model", "sample", "en")["tables"]
    assert t["original"] in sheets
    body = layouts.definition("bayes_model", {}, "en", None, None)["sections"]["body"]["items"]
    by_id = {it["id"]: it for it in body}
    assert by_id["original"]["only"] == "pages" and by_id["original_sheet"]["only"] == "sheet"
    assert any(c["header"] == t["unit"] for c in by_id["original_sheet"]["columns"])
