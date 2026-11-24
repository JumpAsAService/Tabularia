"""The report of the node «Bayesian model» (docs/specs/bayes-report.md, lots Q1 and Q2: the pages that need no reading, the groups, the
diagnostics and the charts),
drawn from the two fixtures (`bayes_model-sample.json`, `bayes_model-prior_predictive.json`, made by the runner's writers on fake draws:
gateway-go/internal/modelgroups/groups/bayes/reference/make_report_fixtures.py), in the five languages and the three formats.
The predictive checks and the readings are the next lots' tests."""
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
    assert not [t for t in texts if t == "Liceo Galilei"]
    for column in ("Mean", "Sd", "5 %", "Median", "95 %", "R-hat", "ESS bulk", "ESS tail", "MCSE of the mean", "Contraction"):
        assert column in texts, column


def test_the_coefficient_of_a_linear_predictor_is_also_given_on_the_column_s_scale_and_nothing_else_is(tmp_path, chart_service, monkeypatch):  # noqa: F811
    texts = said(tmp_path, chart_service, monkeypatch, SAMPLE)
    note = common.words("bayes_model", "sample", "en")["original_note"]  # (the tables' titles are the workbook's sheets, not the page's text)
    assert note in texts
    _, _, rows = fixture(SAMPLE)
    assert {r["level"] for r in rows if r["stat"] == "orig_mean"} == {"b_hours"}
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
    # the spec's own list of verdict words (docs/specs/bayes-report.md, «The words»), in English here: the other languages' by lot Q4
    own = ["converged", "not converged", "good", "bad", "reliable", "unreliable", "passes", "fails", "acceptable", "problem"] if locale == "en" else []
    # what the runner and the person wrote (a label, a unit, a column, a note) is data: the layout's own words are what is checked
    # (the precision of the draws, «8 significant digits», is the runner's phrase said in the page's language: a count of digits, not a
    # statistical «significant»: the words of `values` are left out of the scan)
    own_words = {k: v for k, v in common._file("bayes_model")[locale].items() if k != "values"}
    ours = {t for t in strings(own_words) if not t.startswith("{")}
    checked = [t for t in ours if t in texts or any(t in s for s in texts)]
    assert checked, "none of the layout's words reached the page"
    for text in checked:
        for verdict in [*w["verdict_words"], *own]:
            assert not re.search(r"(?<!\w)" + re.escape(verdict) + r"(?!\w)", text, re.I), (locale, verdict, text)


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
    assert not [t for t in texts if t == "Liceo Galilei"]


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
