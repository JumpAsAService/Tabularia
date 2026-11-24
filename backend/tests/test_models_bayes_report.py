"""The report of the node «Bayesian model», first lot (docs/specs/bayes-report.md, lot Q1): the pages that need no chart and no reading,
drawn from the two fixtures (`bayes_model-sample.json`, `bayes_model-prior_predictive.json`, made by the runner's writers on fake draws:
gateway-go/internal/modelgroups/groups/bayes/reference/make_report_fixtures.py), in the five languages and the three formats.
The diagnostics' page and charts, the predictive checks and the readings are the next lots' tests."""
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
    # a plate's quantities are the groups' page: not here
    assert "Intercept of the school" not in texts and "Liceo Galilei" not in texts
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
