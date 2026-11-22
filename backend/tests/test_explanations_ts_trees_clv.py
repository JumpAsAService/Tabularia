"""The explanation of the method in the reports of ARIMA, XGBoost and CatBoost, the customer lifetime value and BG/BB
(docs/specs/report-explanations.md, lot B3). The criteria every explained model shares run in
test_report_explanations.py over `common.READINGS`; here what is these models' own: a verdict that follows alpha (ADF and
KPSS at the ends of their tables as the runner reads them), no verdict at all for the boosting diagnostics, the
decomposition's words for the same tests, the lists capped, the labels that fit their column, Excel's sheet «Reading»."""
from __future__ import annotations

import copy
import re
import zipfile

import pytest

from app.engine.models import catalog, layouts
from app.engine.models.layouts import arima, common
from app.engine.stats.layouts import LOCALES
from app.reports import glyphs
from tests.test_models_layouts import CASES, draw, fixture
from tests.test_reports_render import chart_service  # noqa: F401 — the fake chart service

MODELS = ("arima", "xgboost", "catboost", "clv", "bg_bb")
B3_CASES = [c for c in CASES if c.split("-")[0] in MODELS]


def lines(case: str, rows: list[dict] | None = None, **params) -> list[tuple[str, str, dict]]:
    """The lines a model's layout chooses: (block, id, values)."""
    model, fixed, original = fixture(case)
    checked = catalog.check_params(model, {**fixed, **params})
    return [(block, id_, dict(values)) for block, _, id_, values in common.READINGS[model](rows or original, checked)]


def ids(case: str, rows: list[dict] | None = None, **params) -> list[str]:
    return [id_ for _, id_, _ in lines(case, rows, **params)]


def with_value(rows: list[dict], key: str, value) -> list[dict]:
    out = copy.deepcopy(rows)
    for r in out:
        if r["key"] == key:
            r["value"] = value
    return out


def without(rows: list[dict], key: str) -> list[dict]:
    return [r for r in copy.deepcopy(rows) if r["key"] != key]


def test_every_model_of_the_lot_registers_its_readings_and_every_fixture_gets_a_method():
    assert set(MODELS) <= set(common.READINGS)
    for case in B3_CASES:
        chosen = lines(case)
        assert {"method", "formulas"} <= {b for b, _, _ in chosen}, case
        assert any(b not in ("method", "formulas") for b, _, _ in chosen), case


# ── a verdict only from a p-value against alpha (criterion 8) ───────────────
def test_arima_s_residual_test_reads_rejected_or_not_by_alpha_around_its_p_value():
    _, _, rows = fixture("arima-manual")
    p = next(r["value"] for r in rows if r["key"] == "assumption|ljung_box_10||p_value")
    assert "ljung_box_10_reject" in ids("arima-manual", alpha=round(p + 0.001, 4))
    below = ids("arima-manual", alpha=round(p - 0.001, 4))
    assert "ljung_box_10_reject" not in below and "residuals_keep_all" not in below and "residuals_keep_others" in below
    assert "residuals_keep_all" in ids("arima-manual", alpha=0.05)


def test_arima_s_coefficients_read_significant_or_not_by_alpha_and_name_five_at_most():
    _, _, rows = fixture("arima-manual")
    p = next(r["value"] for r in rows if r["key"] == "coef|ma1||p_value")
    assert [x for x in ids("arima-manual", alpha=round(p + 0.001, 4)) if x.startswith("coef_")] == ["coef_some"]
    assert [x for x in ids("arima-manual", alpha=1e-12) if x.startswith("coef_")] == ["coef_none"]
    many = copy.deepcopy(rows)
    template = [r for r in rows if r["section"] == "coef" and r["level"] == "ar1"]
    for k in range(2, 9):
        for r in template:
            many.append({**r, "level": f"ar{k}", "key": r["key"].replace("|ar1|", f"|ar{k}|"),
                         "value": 1e-12 * k if r["stat"] == "p_value" else r["value"]})
    said = next(v for _, id_, v in lines("arima-manual", many) if id_ == "coef_some_more")
    assert said["names"] == ["ar2", "ar3", "ar4", "ar5", "ar6"]  # by their p-value
    assert said["k"] == 9 and said["more"] == 4 and said["total"] == 10


def test_adf_and_kpss_follow_the_side_of_their_table_as_the_runner_reads_it():
    """tseries' tables stop at their ends (common.BOUNDED_P): a p-value at the lower end rejects at any alpha from it on,
    one at the upper end keeps at any alpha up to it, and beyond the end the table cannot say (lot D7)."""
    _, _, rows = fixture("arima-manual")
    assert arima.table_rejects("kpss", 0.1, 0.05) is False and arima.table_rejects("kpss", 0.1, 0.2) is None
    assert arima.table_rejects("adf", 0.01, 0.01) is True and arima.table_rejects("adf", 0.01, 0.005) is None
    assert arima.table_rejects("adf", 0.2001, 0.2) is False and arima.table_rejects("adf", 0.2001, 0.25) is True
    # the fixture: ADF p 0.2001 keeps a unit root, KPSS p 0.1 (its upper end) keeps stationarity: neither rejects
    chosen = [x for x in ids("arima-manual") if x.startswith(("adf", "kpss", "stationarity"))]
    assert chosen == ["adf_keep", "kpss_keep_end", "stationarity_kpss_only"]
    # alpha just above ADF's p: it rejects, and the case is the good one
    chosen = [x for x in ids("arima-manual", alpha=0.21) if x.startswith(("adf", "kpss", "stationarity"))]
    assert chosen == ["adf_reject", "kpss_table_end", "stationarity_no_case"]  # 0.21 is beyond KPSS's table (0.10)
    low = with_value(with_value(rows, "assumption|adf||p_value", 0.01), "assumption|kpss||p_value", 0.01)
    chosen = [x for x in ids("arima-manual", low, alpha=0.05) if x.startswith(("adf", "kpss", "stationarity"))]
    assert chosen == ["adf_reject_end", "kpss_reject_end", "stationarity_adf_only"]
    # not computed by the runner (no p-value): alpha says which end
    nc = without(rows, "assumption|kpss||p_value")
    for r in nc:
        if r["key"] == "assumption|kpss||verdict":
            r["text"] = "not_computed"
    nc.append({**next(r for r in nc if r["key"] == "assumption|kpss||verdict"), "stat": "advice_id", "text": "table_end",
               "key": "assumption|kpss||advice_id"})
    said = {id_: v for _, id_, v in lines("arima-manual", nc, alpha=0.2)}
    assert said["kpss_table_end"]["bound"] == 0.1 and "stationarity_no_case" in said


def test_arima_says_adf_and_kpss_in_the_decomposition_s_words():
    """The same tests in the two time series reports: ARIMA's sentences start as the decomposition's do."""
    for locale in LOCALES:
        mine = common.words("arima", "manual", locale)["reading_texts"]
        theirs = common.words("decomposition", "stl", locale)["reading_texts"]
        for own, other in (("adf_reject", "stationarity_both_ok"), ("adf_keep", "stationarity_neither")):
            head = theirs[other].split(" (")[0].split()
            words = iter(mine[own].split(" (")[0].split())
            assert all(x in words for x in head), (locale, own, head)  # their words, in their order
        assert mine["kpss_table_end"].split(";")[0] == theirs["kpss_table_end"].split(";")[0], locale


def test_the_customer_lifetime_tests_read_rejected_or_not_by_alpha_and_the_facts_hold_no_verdict():
    _, _, rows = fixture("clv-value")
    p = next(r["value"] for r in rows if r["key"] == "assumption|frequency_fit||p_value")
    assert "frequency_fit_reject" in ids("clv-value", alpha=round(p + 0.001, 4))
    assert "frequency_fit_keep" in ids("clv-value", alpha=round(p - 0.001, 4))
    p = next(r["value"] for r in rows if r["key"] == "assumption|spend_independence||p_value")
    assert "spend_independence_reject" in ids("clv-value", alpha=round(p + 0.001, 4))
    assert "spend_independence_keep" in ids("clv-value", alpha=round(p - 0.001, 4))
    assert not [x for x in ids("clv-purchases") if x.startswith("spend_")]
    # facts and measures: convergence at an edge, the spend's tail, a horizon past the ages learned
    facts = copy.deepcopy(rows)
    for r in facts:
        if r["key"] in ("assumption|convergence||verdict", "assumption|spend_tail||verdict"):
            r["text"] = "doubtful"
        if r["key"] == "assumption|spend_tail||value":
            r["value"] = 1.8
        if r["key"] == "assumption|horizon_length||value":
            r["value"] = 2.5
    facts.append({**next(r for r in facts if r["key"] == "assumption|convergence||verdict"), "stat": "advice_id",
                  "text": "convergence_edge", "key": "assumption|convergence||advice_id"})
    chosen = ids("clv-value", facts)
    assert {"convergence_edge", "spend_tail_fact", "horizon_beyond"} <= set(chosen)
    for locale in LOCALES:
        w = common.words("clv", "value", locale)
        for key in ("convergence_edge", "convergence_not_reached", "spend_tail_fact", "horizon_beyond"):
            assert key in w["measure_readings"]
            assert not [v for v in w["verdict_words"] if re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", w["reading_texts"][key], re.I)]


@pytest.mark.parametrize("locale", LOCALES)
def test_no_boosting_reading_is_a_verdict(locale):
    """No boosting diagnostic is a test: every sentence a boosting report reads is a measure, said with its threshold."""
    for case in [c for c in B3_CASES if c.split("-")[0] in ("xgboost", "catboost")]:
        model, params, rows = fixture(case)
        w = common.words(model, catalog.variant(model, catalog.check_params(model, params)), locale)
        chosen = [id_ for block, id_, _ in lines(case) if block not in ("method", "formulas")]
        assert chosen and set(chosen) <= set(w["measure_readings"]), (case, sorted(set(chosen) - set(w["measure_readings"])))
        said = [r["text"] for r in layouts.with_labels(model, params, rows, locale)
                if r["section"] == "reading" and r["level"] not in ("method", "formulas")]
        for text in said:
            assert not [v for v in w["verdict_words"] if re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", text, re.I)], (case, text)


def test_a_boosting_diagnostic_past_its_threshold_says_its_number_and_the_threshold():
    _, _, rows = fixture("xgboost-binary")
    said = {id_: v for _, id_, v in lines("xgboost-binary")}
    assert said["diag_validation_size"] == {"value": 15.0, "threshold": 30.0}
    assert said["diag_class_imbalance"]["threshold"] == 4.0
    assert "diagnostics_none" not in said and "summary_boosting_binary" in said and said["importance_top"]["k"] == 3
    # predictors no split uses: named together, five at most
    extra = copy.deepcopy(rows)
    base = next(r for r in rows if r["key"] == "assumption|zero_importance||verdict")
    extra = [r for r in extra if not r["key"].startswith("assumption|zero_importance|")]
    for k in range(7):
        extra.append({**base, "level_2": f"p{k}", "text": "doubtful", "key": f"assumption|zero_importance|p{k}|verdict"})
    zero = next(v for _, id_, v in lines("xgboost-binary", extra) if id_.startswith("diag_zero_importance"))
    assert zero == {"names": ["p0", "p1", "p2", "p3", "p4"], "more": 2}
    # nothing past its threshold: one sentence says so
    calm = [{**r, "text": "ok"} if r["stat"] == "verdict" else r for r in rows]
    assert [x for x in ids("xgboost-binary", calm) if x.startswith("diag")] == ["diagnostics_none"]


def test_many_rows_are_said_in_words_where_the_page_says_them_in_amber():
    _, _, rows = fixture("arima-manual")
    assert "assumptions_many_rows" in ids("arima-manual", with_value(rows, "model|||n", 6000))
    assert "assumptions_many_rows" not in ids("arima-manual")


# ── the page and the workbook ───────────────────────────────────────────────
def test_every_label_of_the_explanation_fits_the_column_part():
    """«How this report was computed» names each paragraph and formula in a column of 40 mm (common.how_computed): each
    word of a label fits it in bold."""
    from app.reports.model import CELL_PAD

    room = 40 - 2 * CELL_PAD + 0.2
    for case in B3_CASES:
        model, params, _ = fixture(case)
        for locale in LOCALES:
            w = common.words(model, catalog.variant(model, catalog.check_params(model, params)), locale)
            labels = [*w["reading_labels"].values(), *w["reading_blocks"].values()]
            assert not [x for label in labels for x in label.split() if glyphs.width_mm(x, 9, 700) > room], (case, locale)


@pytest.mark.parametrize("case", ["arima-auto", "xgboost-binary", "catboost-multiclass", "clv-value", "bg_bb-purchases"])
def test_the_last_pages_explain_the_method_and_excel_keeps_the_reading(tmp_path, chart_service, monkeypatch, case):  # noqa: F811
    model, params, rows = fixture(case)
    w = common.words(model, catalog.variant(model, catalog.check_params(model, params)), "en")
    out, page = draw(tmp_path, chart_service, monkeypatch, case, fmt="svg")
    assert w["sections"]["how"] in page and w["reading"]["lead"] in page
    method = [id_ for block, id_, _ in lines(case) if block == "method"]
    assert w["reading_texts"][method[0]][:40] in page if isinstance(w["reading_texts"][method[0]], str) else True
    out, _ = draw(tmp_path / "x", chart_service, monkeypatch, case, fmt="xlsx")
    with zipfile.ZipFile(out.path) as z:
        sheets = re.findall(r'<sheet name="([^"]+)"', z.read("xl/workbook.xml").decode())
    assert "Reading" in sheets and sheets.index("Reading") > 0, sheets
