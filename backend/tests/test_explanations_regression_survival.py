"""The explanation of the method in the reports of the regressions and of the survival models (docs/specs/report-explanations.md,
lot B2): linear and logistic (also penalized), Kaplan–Meier, Cox, parametric survival. The readings their layouts choose
from the rows the runners wrote, said in five languages; a verdict only from a p-value against the node's α; the measures
without a word of judgement; «How this report was computed» before the notes, kept by Excel as its sheet «Reading».

The criteria every lot shares (`test_report_explanations.py`) run over these models too, as they are in `READINGS`; here
the variants and the branches a fixture alone does not reach."""
from __future__ import annotations

import copy
import re
import zipfile

import pytest

from app.engine.models import catalog, layouts
from app.engine.models.layouts import common
from app.engine.stats.layouts import LOCALES
from tests.test_models_layouts import CASES, draw, fixture
from tests.test_reports_render import chart_service  # noqa: F401 — the fake chart service

MODELS = ("linear", "logistic", "kaplan_meier", "cox", "parametric_survival")
B2_CASES = [c for c in CASES if c.split("-")[0] in MODELS]
# the ids whose sentence is chosen by a p-value against α: each says a verdict in words
VERDICTS = ("assumptions_rejected", "assumptions_none_rejected", "coef_some", "coef_all", "coef_none", "fit_f_reject",
            "fit_f_keep", "fit_lr_reject", "fit_lr_keep", "km_test_reject", "km_test_keep", "pairs_some", "pairs_none")


def _sentences(text) -> list[str]:
    return [text] if isinstance(text, str) else list(text.values())


def _has(word: str, sentence: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", sentence, re.I))


def words_of(case: str, locale: str) -> dict:
    model, params, _ = fixture(case)
    return common.words(model, catalog.variant(model, catalog.check_params(model, params)), locale)


def said(model: str, params: dict, rows: list[dict], locale: str = "en") -> list[dict]:
    return [r for r in layouts.with_labels(model, params, rows, locale) if r["section"] == "reading"]


def ids(model: str, params: dict, rows: list[dict]) -> list[str]:
    checked = catalog.check_params(model, params)
    return [line[2] for line in common.READINGS[model](rows, checked)]


def complete(model: str, params: dict, rows: list[dict]) -> None:
    """Every reading said in the five languages, with nothing of its template left."""
    for locale in LOCALES:
        out = said(model, params, rows, locale)
        assert out, (model, locale)
        for r in out:
            assert not re.search(r"@|\{|%2", r["text"]), (model, locale, r["text"])
            assert not re.search(r"=\s*[<>]", r["text"]), (model, locale, r["text"])
            assert r["label"] not in ("", r["stat"]), (model, locale, r)


# ── the words ───────────────────────────────────────────────────────────────
def test_the_five_models_register_their_readings():
    assert set(MODELS) <= set(common.READINGS)


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("case", B2_CASES)
def test_no_measure_holds_a_word_of_judgement_and_every_verdict_holds_one(case, locale):
    """Every variant (the shared test reads the first fixture of a model only: the penalized one for the regressions)."""
    w = words_of(case, locale)
    assert set(w["measure_readings"]) <= set(w["reading_texts"]), sorted(set(w["measure_readings"]) - set(w["reading_texts"]))
    for key in w["measure_readings"]:
        for sentence in _sentences(w["reading_texts"][key]):
            assert not [v for v in w["verdict_words"] if _has(v, sentence)], (case, locale, key, sentence)
    for key in (k for k in VERDICTS if k in w["reading_texts"]):
        for sentence in _sentences(w["reading_texts"][key]):
            assert [v for v in w["verdict_words"] if _has(v, sentence)], (case, locale, key, sentence)
            assert "α" in sentence, (case, locale, key)


@pytest.mark.parametrize("locale", LOCALES)
def test_every_method_and_formula_has_a_label_that_fits_the_column_part(locale):
    from app.reports import glyphs
    from app.reports.model import CELL_PAD

    room = 40 - 2 * CELL_PAD + 0.2  # the column «Part» of «How this report was computed» (common.how_computed)
    for case in B2_CASES:
        w = words_of(case, locale)
        for key in [*w["formula_texts"], *(k for k in w["reading_texts"] if k.startswith("method_"))]:
            label = w["reading_labels"].get(key)
            assert label, (case, locale, key)
            assert not [x for x in label.split() if glyphs.width_mm(x, 9, 600) > room], (case, locale, label)
        assert glyphs.missing(set("".join(s for v in (*w["reading_texts"].values(), *w["formula_texts"].values())
                                          for s in _sentences(v)))) == set(), (case, locale)


# ── the readings of each fixture and of the branches a fixture does not reach ──
@pytest.mark.parametrize("case", B2_CASES)
def test_every_fixture_reads_in_the_five_languages_with_its_method(case):
    model, params, rows = fixture(case)
    complete(model, params, rows)
    blocks = {line[0] for line in common.READINGS[model](rows, catalog.check_params(model, params))}
    assert {"summary", "method", "formulas"} <= blocks, (case, blocks)


def test_the_linear_regression_reads_r2_the_f_test_the_checks_the_terms_and_its_errors():
    model, params, rows = fixture("linear-unpenalized")
    got = ids(model, params, rows)
    assert got[:3] == ["summary_linear", "fit_f_reject", "assumptions_rejected"]
    assert {"threshold_vif", "coef_all", "coef_robust", "fit_errors", "method_se_hc", "formula_sandwich"} <= set(got)
    text = {r["key"]: r["text"] for r in said(model, params, rows)}
    assert text["reading|summary||line_1"].startswith("On 117 rows, the model explains 74.6 % of the variance of «quantity»")
    # the checks that reject, counted and named as the table names them (lot B4)
    assert "4 of 9 — «Homoscedasticity: Breusch–Pagan», «Homoscedasticity: White», «Independence: Durbin–Watson», " \
           "«Independence: Breusch–Godfrey, order 1»." in text["reading|assumptions||line_1"]
    # the VIF is a measure against its threshold: the number and the threshold, never «failed»
    assert "channel, price × channel, at 9.342 / 10.12" in text["reading|assumptions||line_2"]
    assert "failed" not in text["reading|assumptions||line_2"]
    classic = ids(model, {**params, "se": "classical"}, rows)
    assert "coef_robust" not in classic and "method_se_classical" in classic and "formula_sandwich" not in classic
    assert "method_wls" in ids(model, {**params, "estimation": "wls", "weights": "w"}, rows)


def test_a_penalized_fit_says_no_verdict_and_why():
    for case in ("linear-penalized", "logistic-penalized"):
        model, params, rows = fixture(case)
        got = ids(model, params, rows)
        assert not set(got) & set(VERDICTS), (case, got)
        assert {"summary_penalized", "penalized_measure", "penalized_lambda", "method_no_inference", "method_cv"} <= set(got)
        for lam in ("cv_min", 0.5):
            complete(model, {**params, "lambda": lam}, rows)
    model, params, rows = fixture("logistic-penalized")
    assert "confusion_at" in ids(model, params, rows)
    for measure in ("auc", "class"):
        complete(model, {**params, "measure": measure}, rows)
    complete(*fixture("linear-penalized")[:2], fixture("linear-penalized")[2])
    model, params, rows = fixture("linear-penalized")
    complete(model, {**params, "penalty": "ridge"}, rows)


def test_the_logistic_reads_the_first_significant_term_s_odds_ratio_and_the_classification():
    model, params, rows = fixture("logistic-unpenalized")
    text = {r["key"]: r["text"] for r in said(model, params, rows)}
    assert text["reading|coefficients||line_2"].startswith("Among them, «tenure»: each unit more multiplies the odds")
    assert "(its 95 % interval 0.9506 to 0.9826)" in text["reading|coefficients||line_2"]
    assert "classified as «churned = yes» when its predicted probability is at least 0.5" in text["reading|confusion||line_1"]
    probit = ids(model, {**params, "link": "probit"}, rows)
    assert "coef_odds" not in probit and "method_probit" in probit and "formula_probit" in probit
    firth = ids(model, {**params, "estimation": "firth", "se": "hc1"}, rows)
    assert {"method_firth", "coef_robust", "method_se_hc"} <= set(firth)
    complete(model, {**params, "se": "cluster", "cluster": "plan"}, rows)
    # without DeLong's interval, the summary without it
    bare = [r for r in rows if r["key"] not in ("model|||auc_ci_low", "model|||auc_ci_high")]
    assert ids(model, params, bare)[0] == "summary_logistic_no_ci"
    complete(model, params, bare)


def test_kaplan_meier_reads_its_medians_its_test_its_pairs_and_its_restricted_means():
    model, params, rows = fixture("kaplan_meier-default")
    got = ids(model, params, rows)
    assert {"summary_km", "km_medians", "km_test_reject", "pairs_some", "rmst", "threshold_few_at_risk", "method_logrank",
            "method_pairs", "formula_logrank"} <= set(got)
    text = " ".join(r["text"] for r in said(model, params, rows))
    assert "line=A, line=B, line=C, at 146 / 58 / 44" in text
    assert "3 of 3 — line=A – line=C, line=A – line=B, line=B – line=C" in text
    assert "method_peto" in ids(model, {**params, "test": "peto"}, rows)
    complete(model, {**params, "test": "peto"}, rows)
    # one stratum (no strata): its median with its interval, its restricted mean; no test, no pairs
    one = [{**r, "level": "all", "key": r["key"].replace("|line=A|", "|all|")} for r in rows
           if r["section"] not in ("pair",) and not (r["section"] == "stratum" and r["level"] != "line=A")]
    single = {k: v for k, v in params.items() if k not in ("strata", "pairwise")}
    got = ids(model, single, one)
    assert got[0] == "summary_km_one" and "km_median_one" in got and "rmst_one" in got
    assert not {"km_test_reject", "km_test_keep", "pairs_some", "pairs_none", "method_logrank"} & set(got)
    complete(model, single, one)
    # its interval open at an end, and a median never reached
    open_ = [r for r in one if r["key"] != "stratum|all||median_ci_high"]
    assert "km_median_one_open" in ids(model, single, open_)
    complete(model, single, open_)
    never = [r for r in one if not r["key"].startswith("stratum|all||median")]
    assert "km_median_not_reached" in ids(model, single, never)
    complete(model, single, never)
    # several strata, one median never reached
    half = [r for r in rows if not r["key"].startswith("stratum|line=C||median")]
    assert "km_not_reached" in ids(model, params, half)
    complete(model, params, half)


def test_cox_reads_the_concordance_schoenfeld_and_the_first_significant_hazard_ratio():
    model, params, rows = fixture("cox-default")
    text = {r["key"]: r["text"] for r in said(model, params, rows)}
    assert "The concordance is 0.6904" in text["reading|summary||line_1"]
    assert "2 of 4" in text["reading|assumptions||line_1"]  # Schoenfeld's test of «line» and the global one
    assert text["reading|coefficients||line_2"].startswith("Among them, «line»: the level «B» multiplies the hazard")
    for ties in ("breslow", "exact"):
        assert f"method_ties_{ties}" in ids(model, {**params, "ties": ties}, rows)
    robust = {**params, "se": "robust", "cluster": "line"}
    assert {"coef_robust", "method_se_robust"} <= set(ids(model, robust, rows))
    complete(model, robust, rows)


def test_a_parametric_survival_reads_its_distribution_its_choice_and_its_ratios():
    model, params, rows = fixture("parametric_survival-auto")
    text = " ".join(r["text"] for r in said(model, params, rows))
    assert "The distribution fitted, Weibull, on 236 rows and 170 events" in text
    assert "Of the 6 candidates fitted, Weibull has the lowest AIC" in text and "The next, Generalized gamma, is 1.338" in text
    assert "each unit more multiplies the time to the event by 1.024" in text
    got = ids(model, params, rows)
    assert {"method_aft", "method_ph", "method_weibull", "method_auto", "formula_aic", "formula_akaike_weight"} <= set(got)
    ph = fixture("parametric_survival-ph")
    assert {"coef_hazard_level", "method_ph", "method_gompertz"} <= set(ids(*ph)) and "method_aft" not in ids(*ph)
    assert "summary_parametric_strata" in ids(*fixture("parametric_survival-ph_aft"))
    # chosen by parsimony; a median never reached; no interval
    moved = [{**r, "value": 1.0} if r["key"] == "candidate|weibull||parsimony" else r for r in rows]
    assert "auto_parsimony" in ids(model, params, moved)
    complete(model, params, moved)
    never = [r for r in rows if not r["key"].startswith("stratum|all||median")]
    assert ids(model, params, never)[0] == "summary_parametric_never"
    complete(model, params, never)
    bare = [r for r in rows if r["key"] != "stratum|all||median_ci_low"]
    assert ids(model, params, bare)[0] == "summary_parametric_no_ci"


def test_many_rows_and_a_long_list_are_said_and_capped():
    model, params, rows = fixture("linear-unpenalized")
    many = [{**r, "value": 12000} if r["key"] == "model|||n" else r for r in rows]
    assert "assumptions_many_rows" in ids(model, params, many)
    complete(model, params, many)
    # nine checks that reject: five named in the table's order, «and 4 more»
    rejecting = [{**r, "value": 0.001} if r["section"] == "assumption" and r["stat"] == "p_value" else r for r in rows]
    checks = [x for x in common.READINGS[model](rejecting, catalog.check_params(model, params)) if x[0] == "assumptions"]
    assert checks[0][2] == "assumptions_rejected" and checks[0][3]["count"] == 9 and len(checks[0][3]["names"]) == 5
    assert checks[0][3]["names"][0] == "check:reset" and checks[1][2:] == ("more_in_table", {"more": 4})
    complete(model, params, rejecting)
    # seven significant terms: five named, «and 2 more»
    extra = []
    for i in range(4):
        for stat, v in (("estimate", 1.0), ("p_value", 0.001 * (i + 1))):
            extra.append({**rows[0], "section": "coef", "level": f"x{i}", "level_2": None, "stat": stat, "value": v,
                          "text": None, "key": f"coef|x{i}||{stat}"})
    lines = [r["text"] for r in said(model, params, rows + extra) if r["level"] == "coefficients"]
    assert "7 of 7" not in lines[0] and lines[0].count(",") >= 4 and lines[1] == "And 2 more: the table lists them all."
    complete(model, params, rows + extra)


# ── a verdict follows α (the spec's criterion 8) ────────────────────────────
def _alpha_around(p: float) -> tuple[float, float]:
    return min(p * 1.01, 0.999), p * 0.99


@pytest.mark.parametrize("case, key, line, yes, no", [
    ("linear-unpenalized", "model|||f_p_value", "summary", "fit_f_reject", "fit_f_keep"),
    ("logistic-unpenalized", "model|||lr_p_value", "summary", "fit_lr_reject", "fit_lr_keep"),
    ("cox-default", "model|||lr_p_value", "summary", "fit_lr_reject", "fit_lr_keep"),
    ("parametric_survival-aft", "model|||lr_p_value", "summary", "fit_lr_reject", "fit_lr_keep"),
    ("kaplan_meier-default", "model|||test_p_value", "curves", "km_test_reject", "km_test_keep"),
    ("kaplan_meier-default", "pair|line=B|line=C|p_adjusted", "pairs", "pairs_some", None),
    ("linear-unpenalized", "assumption|shapiro_wilk||p_value", "assumptions", "assumptions_rejected", None),
    ("parametric_survival-auto", "assumption|ph_cox|load|p_value", "assumptions", "assumptions_rejected", "assumptions_none_rejected"),
    ("logistic-unpenalized", "coef|fee||p_value", "coefficients", "coef_some", None),
    ("parametric_survival-aft", "coef|line|C|p_value", "coefficients", "coef_all", "coef_some"),
])
def test_a_verdict_follows_alpha(case, key, line, yes, no):
    model, params, rows = fixture(case)
    p = next(r["value"] for r in rows if r["key"] == key)
    above, below = _alpha_around(p)

    def chosen(alpha: float) -> list[tuple[str, dict]]:
        return [(i, v) for b, _, i, v in common.READINGS[model](rows, catalog.check_params(model, {**params, "alpha": alpha}))
                if b == line]

    def counted(alpha: float) -> int:
        return sum(v.get("count", 0) for i, v in chosen(alpha) if i == yes)

    assert yes in [i for i, _ in chosen(above)], (case, above)
    if no:
        assert no in [i for i, _ in chosen(below)], (case, below)
    else:  # one more row among those that reject when α is just above its p-value
        assert counted(above) == counted(below) + 1, (case, counted(above), counted(below))
    for alpha in (above, below):
        complete(model, {**params, "alpha": alpha}, rows)


# ── the page ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("case", B2_CASES)
def test_the_readings_are_under_their_blocks_and_the_method_before_the_notes(case):
    model, params, _ = fixture(case)
    body = [it["id"] for it in layouts.definition(model, params, "en")["sections"]["body"]["items"]]
    assert body.index("formula") < body.index("reading_summary") < body.index("s_assumptions")
    assert body.index("assumptions") < body.index("reading_assumptions")
    assert body.index("reading_assumptions") < body.index("s_how") < body.index("reading") < body.index("s_notes")
    if "s_coef" in body and model != "kaplan_meier" and "penalized" not in case:
        assert body.index("s_coef") < body.index("reading_coefficients") < body.index("s_fit")
    if case == "linear-unpenalized":
        assert body.index("fit") < body.index("reading_fit")
    if model == "logistic":
        assert body.index("confusion_table") < body.index("reading_confusion")
    if model == "kaplan_meier":
        assert body.index("at_risk") < body.index("reading_curves") < body.index("strata_rmst") < body.index("reading_rmst")
        assert body.index("pairs") < body.index("reading_pairs")


def test_excel_keeps_the_method_as_its_sheet_reading(tmp_path, chart_service, monkeypatch):  # noqa: F811
    out, _ = draw(tmp_path, chart_service, monkeypatch, "cox-default", fmt="xlsx")
    with zipfile.ZipFile(out.path) as z:
        sheets = re.findall(r'<sheet name="([^"]+)"', z.read("xl/workbook.xml").decode())
    assert sheets[:2] == ["Checks of the assumptions", "What to do"] and "Reading" in sheets, sheets
    assert sheets.index("Reading") > sheets.index("Estimates and tests"), sheets


def test_a_pdf_of_each_model_holds_its_readings_and_its_formulas(tmp_path, chart_service, monkeypatch):  # noqa: F811
    import json

    from tests.test_models_layouts import strings

    for case in ("linear-unpenalized", "logistic-penalized", "kaplan_meier-default", "cox-default", "parametric_survival-auto"):
        out, page = draw(tmp_path / case, chart_service, monkeypatch, case, locale="it")
        assert out.warnings == [] and out.pages <= 12, (case, out.pages)
        text = " ".join(strings(json.loads(page)))
        w = words_of(case, "it")
        assert w["sections"]["how"] in text and w["reading"]["lead"] in text, case
        model, params, rows = fixture(case)
        for r in said(model, params, rows, "it"):
            assert r["text"] in text, (case, r["text"])


def test_only_the_explanation_is_added_to_the_page_without_it(monkeypatch):
    from tests.test_report_explanations import only_additions

    for case in B2_CASES:
        only_additions(case, monkeypatch)


def test_the_words_left_by_the_runner_s_english_are_untouched_by_the_readings():
    """A model's own words (its checks, its notes) are as before: the readings only add their keys."""
    for case in B2_CASES:
        model, params, rows = fixture(case)
        w = copy.deepcopy(words_of(case, "en"))
        assert w["checks"] and "reading_texts" in w and "formula_texts" in w
        plain = [r for r in layouts.with_labels(model, params, rows, "en") if r["section"] != "reading"]
        assert len(plain) == len(rows)


def test_a_check_of_each_term_is_named_with_its_term():
    model, params, rows = fixture("cox-default")
    text = {r["key"]: r["text"] for r in said(model, params, rows, "it")}
    assert "2 su 4 — «Rischi proporzionali: Schoenfeld» (line), «Rischi proporzionali: test globale»." in \
        text["reading|assumptions||line_1"]
