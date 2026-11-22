"""The reports of «Volatility (GARCH)» and «Risk simulation» (docs/specs/garch-risk.md, lot G4): the readings each layout
chooses from the rows its runner wrote (`common.READINGS`), every sentence in the five languages, the owner's rule on
verdicts (only the tests with a p-value against α: the weighted Ljung–Box, ARCH LM, Nyblom, the sign bias, Kupiec,
Christoffersen; VaR, ES, persistence, half-life and probabilities are measures, with no word of judgement), each verdict
following α, the backtest's sentences saying how many breaks were expected, the pages, and the memory of the report.

`garch-default` is the GARCH runner's own output (lot G1, on its reference prices), and so are the risk node's two fixtures
since lot G9 (a GARCH of prices with a position, a SARIMA's running total on its upper tail). READINGS lists every template with
the names a layout sends."""
from __future__ import annotations

import json
import re
import tracemalloc
import zipfile

import pytest

from app.engine.models import catalog, layouts
from app.engine.models.layouts import common
from app.engine.models.layouts import garch as garch_layout
from app.engine.models.layouts import risk_simulation as risk_layout
from app.engine.stats.layouts import LOCALES
from app.reports import definition as rd
from app.reports import formats
from tests.test_models_layouts import draw, fixture, strings
from tests.test_reports_render import chart_service  # noqa: F401 — the fake chart service

GARCH, RISK, RISK_ARIMA = "garch-default", "risk_simulation-garch", "risk_simulation-arima"
TEST = {"p_value": 0.0123, "alpha": 0.05}
# the lines of a GARCH's checks, written by the GARCH node (its blocks) and by the risk node (its block `model`)
CHECK_LINES: dict[str, dict] = {
    "lb_z_ok": {"lag": 13, **TEST}, "lb_z_failed": {"lag": 13, **TEST}, "lb_z2_ok": {"lag": 13, **TEST},
    "lb_z2_failed": {"lag": 13, **TEST}, "arch_lm_ok": {"lag": 12, **TEST}, "arch_lm_failed": {"lag": 12, **TEST},
    "z2_largest_acf": {"acf": 0.0812, "lag": 3},
    "nyblom_stable": TEST, "nyblom_moves": {**TEST, "parameter": "omega", "p_adjusted": 0.031}, "nyblom_moves_none": TEST,
    "sign_bias_supported": TEST, "sign_bias_not_supported": TEST,
}
BACKTEST_LINES: dict[str, dict] = {
    "backtest_coverage_ok": {"holdout": 250, "unit": "index", "level": 0.99, "violations": 4, "expected": 2.5, **TEST},
    "backtest_coverage_failed": {"holdout": 250, "unit": "index", "level": 0.99, "violations": 9, "expected": 2.5, **TEST},
    "backtest_independence_ok": {**TEST, "expected": 2.5}, "backtest_independence_failed": {**TEST, "expected": 2.5},
    "backtest_independence_not_computed": {"expected": 2.5}, "backtest_cc_ok": {**TEST, "expected": 2.5},
    "backtest_cc_failed": {**TEST, "expected": 2.5}, "backtest_expected": {"expected": 2.5}, "backtest_short": {"holdout": 1},
}
# every template the GARCH runner (lot G1) may write: block → id → names
GARCH_READINGS: dict[str, dict[str, dict]] = {
    "summary": {
        "summary": {"model": "GJR-GARCH(1,1)", "distribution": "sstd", "n": 763, "series": "prezzo", "first": 19542.0, "last": 20608.0},
        "summary_steps": {"model": "GARCH(1,1)", "distribution": "std", "n": 763, "series": "prezzo", "first": 2, "last": 764},
        "summary_holdout": {"model": "GJR-GARCH(1,1)", "distribution": "sstd", "n": 763, "series": "prezzo", "first": 19542.0,
                            "last": 20608.0, "holdout": 250},
        "summary_holdout_steps": {"model": "EGARCH(1,1)", "distribution": "norm", "n": 763, "series": "prezzo", "first": 2,
                                  "last": 764, "holdout": 250},
        "summary_prices": {}, "summary_scale": {"scale": 0.0152},
    },
    "persistence": {
        "persistence": {"persistence": 0.985, "half_life": 45.86, "unit": "index"}, "persistence_integrated": {"persistence": 1.0},
        "sigma_now_above": {"sigma": 0.0213, "uncond": 0.0161}, "sigma_now_below": {"sigma": 0.0097, "uncond": 0.0115},
        "sigma_now_equal": {"uncond": 0.0115},
    },
    "asymmetry": {
        "asymmetry_gjr": {"alpha": 0.04, "gamma": 0.09}, "asymmetry_gjr_falls_only": {"gamma": 0.12},
        "asymmetry_egarch": {"alpha": -0.08, "gamma": 0.15}, "asymmetry_none": {},
        "sign_bias_supported": TEST, "sign_bias_not_supported": TEST,
    },
    "distribution": {
        "distribution_norm": {}, "distribution_std": {"shape": 5.8, "low": 4.2, "high": 8.9, "level": 0.95},
        "distribution_sstd": {"shape": 5.8, "low": 4.2, "high": 8.9, "skew": 0.93, "skew_low": 0.85, "skew_high": 1.01, "level": 0.95},
    },
    "residuals": {k: v for k, v in CHECK_LINES.items() if k.startswith(("lb_", "arch_", "z2_"))},
    "stability": {k: v for k, v in CHECK_LINES.items() if k.startswith("nyblom_")},
    "backtest": BACKTEST_LINES,
    "method": {k: {} for k in ("method_garch", "method_scale", "method_estimation", "method_distribution", "method_checks",
                               "method_nyblom", "method_sign_bias", "method_backtest", "method_forecast")},
    "formulas": {k: {} for k in ("formula_mean", "formula_variance", "formula_distribution", "formula_likelihood",
                                 "formula_persistence", "formula_portmanteau", "formula_arch_lm", "formula_nyblom",
                                 "formula_sign_bias", "formula_var_1step", "formula_kupiec", "formula_christoffersen")},
}
SHAPE = {"p": 0.01, "variable": "variable:cumulative_return", "steps": 10, "unit": "index", "value": -0.0712, "low": -0.0753,
         "high": -0.067, "paths": 10000, "confidence": 0.95}
LEVEL = {**SHAPE, "variable": "variable:running_total", "unit": "day", "value": 1538.6, "low": 1537.1, "high": 1540.1}
ES = {"p": 0.025, "variable": "variable:cumulative_return", "value": -0.0868, "low": -0.0901, "high": -0.0835}
ES_LEVEL = {**ES, "variable": "variable:running_total", "value": 1557.1, "low": 1555.1, "high": 1559.0}
MONEY = {"amount": 100000, "money": -7120.4}
THRESHOLD = {"variable": "variable:cumulative_return", "threshold": -0.1, "steps": 10, "unit": "index", "prob_crossed": 0.073,
             "low": 0.068, "high": 0.078, "prob_beyond": 0.049}
# every template the risk runner (lot G3) may write: block → id → names
RISK_READINGS: dict[str, dict[str, dict]] = {
    "summary": {
        "summary_garch": {"paths": 10000, "steps": 10, "unit": "index", "model": "GJR-GARCH(1,1)", "distribution": "sstd",
                          "series": "prezzo", "seed": 1},
        "summary_arima": {"paths": 10000, "steps": 30, "unit": "day", "model": "SARIMA(1,0,1)(0,1,1)[7]", "series": "ordini", "seed": 1},
        "summary_bootstrap": {}, "summary_variable": {"variable": "variable:price", "steps": 10, "unit": "index"},
        "summary_holdout": {"holdout": 250, "unit": "index"},
    },
    "var": {"var_lower": SHAPE, "var_lower_level": LEVEL, "var_upper": SHAPE, "var_upper_level": LEVEL, "var_money": MONEY},
    "es": {"es_lower": ES, "es_lower_level": ES_LEVEL, "es_upper": ES, "es_upper_level": ES_LEVEL,
           "es_lower_quantile": {**ES, "quantile": -0.0731}, "es_lower_level_quantile": {**ES_LEVEL, "quantile": 1541.2},
           "es_upper_quantile": {**ES, "quantile": 0.0731}, "es_upper_level_quantile": {**ES_LEVEL, "quantile": 1541.2},
           "es_money": {**MONEY, "money": -8680.0}},
    "threshold": {"threshold_lower": THRESHOLD, "threshold_lower_level": {**THRESHOLD, "variable": "variable:price", "threshold": 280},
                  "threshold_upper": THRESHOLD, "threshold_upper_level": {**THRESHOLD, "variable": "variable:value", "threshold": 1000}},
    "drawdown": {"drawdown": {"median": 0.021, "tail": 0.087, "p": 0.01, "steps": 10, "unit": "index"}},
    "shape": {"shape": {"variable": "variable:relative_change", "steps": 10, "unit": "index", "skewness": -0.4, "kurtosis": 2.1}},
    "fan": {"fan": {"shares": [0.5, 0.9, 0.98], "error": 4e-06, "sample": 20}, "fan_bands": {"shares": [0.5, 0.9, 0.98], "error": 0.002}},
    "model": {**CHECK_LINES, "arima_checks_ok": {"total": 4, "alpha": 0.05},
              "arima_check_failed": {"check": "check:ljung_box_10", **TEST}},
    "backtest": BACKTEST_LINES,
    "method": {k: {} for k in ("method_simulation", "method_seed", "method_innovations", "method_variable", "method_var", "method_es",
                               "method_threshold", "method_drawdown", "method_passes", "method_backtest", "method_model_garch",
                               "method_model_arima")},
    "formulas": {k: {} for k in ("formula_garch_path", "formula_arima_path", "formula_variable", "formula_var", "formula_es",
                                 "formula_clopper_pearson", "formula_drawdown", "formula_moments", "formula_band_error",
                                 "formula_var_1step", "formula_kupiec", "formula_christoffersen")},
}
READINGS = {"garch": GARCH_READINGS, "risk_simulation": RISK_READINGS}
# the ids whose sentence is a test's verdict (chosen by a p-value against α); the method's and the formulas' are explanations
VERDICTS = {k for k in (*CHECK_LINES, *BACKTEST_LINES, "arima_checks_ok", "arima_check_failed")
            if k not in ("z2_largest_acf", "backtest_independence_not_computed", "backtest_expected", "backtest_short")}
# the measures the owner named: their ids must be declared measures (and so hold no word of judgement)
NAMED_MEASURES = {"garch": {"persistence", "persistence_integrated", "sigma_now_above", "sigma_now_below", "sigma_now_equal"},
                  "risk_simulation": {"var_lower", "var_lower_level", "var_upper", "var_upper_level", "es_lower",
                                      "es_lower_level", "es_upper", "es_upper_level", "threshold_lower",
                                      "threshold_lower_level", "threshold_upper", "threshold_upper_level", "drawdown",
                                      "var_money", "es_money", "es_lower_quantile", "es_lower_level_quantile",
                                      "es_upper_quantile", "es_upper_level_quantile"}}
# words of judgement besides common.json's `verdict_words`, for the measures of these two reports
JUDGEMENT = {"en": ("large", "small", "high risk", "low risk", "safe", "risky", "dangerous", "worrying", "acceptable", "too"),
             "it": ("grande", "piccolo", "piccola", "rischioso", "rischiosa", "sicuro", "pericoloso", "preoccupante", "accettabile", "troppo"),
             "de": ("groß", "klein", "riskant", "sicher", "gefährlich", "bedenklich", "akzeptabel", "zu hoch", "zu niedrig"),
             "fr": ("grand", "petit", "risqué", "sûr", "dangereux", "inquiétant", "acceptable", "trop"),
             "es": ("grande", "pequeño", "arriesgado", "seguro", "peligroso", "preocupante", "aceptable", "demasiado")}


def _said(text) -> list[str]:
    return [text] if isinstance(text, str) else list(text.values())


def _has(word: str, sentence: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", sentence, re.I))


def _words(model: str, locale: str, case: str | None = None) -> dict:
    _, params, _ = fixture(case or (GARCH if model == "garch" else RISK))
    return common.words(model, catalog.variant(model, catalog.check_params(model, params)), locale)


def full_rows(model: str, rows: list[dict], only: set[str] | None = None) -> list[dict]:
    """The fixture's rows with a line of every template (of those in `only`): with the layout's own readings left out
    (`no_readings`), every sentence is said and drawn."""
    lines = [(block, None, id_, values) for block, ids in READINGS[model].items() for id_, values in ids.items()
             if only is None or id_ in only]
    return rows + common.reading_rows(lines, rows)


def no_readings(monkeypatch, model: str) -> None:
    monkeypatch.delitem(common.READINGS, model)


def _with(monkeypatch, model: str, params: dict, rows: list[dict]) -> None:
    """`draw` and `fixture` of test_models_layouts on these parameters and rows."""
    import tests.test_models_layouts as me

    monkeypatch.setattr(me, "fixture", lambda case: (model, params, rows))


# ── the words ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("model", sorted(READINGS))
def test_every_template_a_layout_may_choose_has_its_sentence_its_names_and_a_label(model):
    words = common._file(model)
    ids = {i: v for block in READINGS[model].values() for i, v in block.items()}
    english = words["en"]
    templates = {**english["reading_texts"], **english["formula_texts"]}
    assert set(templates) == set(ids), sorted(set(templates) ^ set(ids))
    for id_, values in ids.items():
        for sentence in _said(templates[id_]):
            assert set(re.findall(r"\{(\w+)(?::\w+)?\}", sentence)) <= set(values), (id_, sentence)
    for code in LOCALES:
        for id_ in (i for i in ids if i.startswith(("method_", "formula_"))):
            assert common.words(model, "garch" if model == "risk_simulation" else "default", code)["reading_labels"].get(id_), (code, id_)
        assert set(words[code]["measure_readings"]) <= set(words[code]["reading_texts"])
    # the names that are numbers are never among the names a note keeps as they are (a position, a size are names)
    numbers = {n for values in ids.values() for n, v in values.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    assert not numbers & common.NOTE_NAMES, sorted(numbers & common.NOTE_NAMES)


@pytest.mark.parametrize("model", sorted(READINGS))
@pytest.mark.parametrize("locale", LOCALES)
def test_no_measure_holds_a_word_of_judgement_and_each_verdict_holds_one(model, locale):
    """The owner's rule: a verdict only from a test with a p-value against α. VaR, ES, persistence, half-life, the
    probabilities of the threshold, the drawdown are measures: their sentences, the measures' tables, the cards and the
    captions never say «significant», «rejects», «large», «safe»… (common.json's `verdict_words`, and JUDGEMENT); each
    verdict's sentence holds a verdict word."""
    w = _words(model, locale)
    measures = set(w["measure_readings"])
    assert NAMED_MEASURES[model] <= measures and not measures & VERDICTS
    ids = {i for block in READINGS[model].values() for i in block}
    assert ids - measures == VERDICTS & ids | {i for i in ids if i.startswith(("method_", "formula_"))}
    sentences = [s for key in measures for s in _said(w["reading_texts"][key])]
    sentences += [*w["measure_texts"].values(), *strings(w["kpi"]), *strings(w["captions"])]
    sentences += strings(w["measure_what"]) if model == "garch" else strings(w["risk_what"])
    for sentence in sentences:
        found = [v for v in (*w["verdict_words"], *JUDGEMENT[locale]) if _has(v, sentence)]
        assert not found, (locale, found, sentence)
    for key in VERDICTS & ids:
        for sentence in _said(w["reading_texts"][key]):
            assert any(_has(v, sentence) for v in w["verdict_words"]), (locale, key, sentence)
    # no p-value after «=» («p = < 0.0001»), in any template
    assert not [s for k in w["reading_texts"] for s in _said(w["reading_texts"][k]) if re.search(r"=\s*\{\w+:p\}", s)]


@pytest.mark.parametrize("model", sorted(READINGS))
def test_beside_each_verdict_of_the_backtest_the_sentence_says_how_many_breaks_were_expected(model):
    for code in LOCALES:
        texts = common._file(model)[code]["reading_texts"]
        for key in (k for k in BACKTEST_LINES if k in VERDICTS):
            assert all("{expected:num}" in s for s in _said(texts[key])), (code, key)


# ── the readings the layouts choose ─────────────────────────────────────
@pytest.mark.parametrize("case", (GARCH, RISK, RISK_ARIMA))
@pytest.mark.parametrize("locale", LOCALES)
def test_every_reading_is_a_sentence_of_the_page_s_language(case, locale, monkeypatch):
    model, params, rows = fixture(case)
    said = [r for r in layouts.with_labels(model, params, rows, locale) if r["section"] == "reading"]
    ids = {i for block in READINGS[model].values() for i in block}
    assert {r["level"] for r in said} >= {"summary", "method", "formulas"}, case
    chosen = [r for r in rows if r["section"] == "reading"]
    assert not chosen, "the runners write no reading: the layout chooses them"
    assert {line[2] for line in common.READINGS[model](rows, catalog.check_params(model, params))} <= ids
    no_readings(monkeypatch, model)
    for given in (said, [r for r in layouts.with_labels(model, params, full_rows(model, rows), locale) if r["section"] == "reading"]):
        for r in given:
            assert not re.search(r"@|\{|%2", r["text"]), (case, r["text"])
            assert not re.search(r"=\s*[<>]", r["text"]), (case, r["text"])
            assert r["label"] not in ("", r["stat"]), (case, r)
    if model == "garch":  # a parameter is said by its symbol, a distribution in the page's words
        lines = {r["text"] for r in layouts.with_labels(model, params, full_rows(model, rows, {"nyblom_moves", "summary"}), locale)
                 if r["section"] == "reading"}
        decimal = formats.locale(locale).decimal
        assert any(" ω " in x and f"0{decimal}031" in x for x in lines), lines
        assert any(_words(model, locale)["values"]["distribution"]["sstd"] in x for x in lines), lines


def test_the_garch_runner_s_own_results_are_read_as_the_contract_says():
    """`garch-default` is garch.R's output (a GJR-GARCH(1,1) with skewed t errors on 300 log returns of prices, 50 kept out):
    α₁ is at its bound, every test keeps its hypothesis at 0.05, two breaks against 0.5 expected."""
    model, params, rows = fixture(GARCH)
    lines = common.READINGS[model](rows, catalog.check_params(model, params))
    ids = [x[2] for x in lines]
    assert ids[:3] == ["summary_holdout", "summary_prices", "summary_scale"]
    assert {"persistence", "sigma_now_below", "asymmetry_gjr_falls_only", "sign_bias_not_supported", "distribution_sstd", "lb_z_ok",
            "lb_z2_ok", "arch_lm_ok", "nyblom_stable", "backtest_coverage_ok", "backtest_independence_ok", "backtest_cc_ok",
            "backtest_expected"} <= set(ids)
    by = {x[2]: x[3] for x in lines}
    assert by["summary_holdout"]["model"] == "GJR-GARCH(1,1)" and by["summary_holdout"]["holdout"] == 50
    assert by["backtest_coverage_ok"]["violations"] == 2 and by["backtest_coverage_ok"]["expected"] == pytest.approx(0.5)
    assert ids.index("method_backtest") < ids.index("method_forecast") and "formula_kupiec" in ids
    # the runner's notes are sentences: its scale, a parameter at a bound by its symbol
    notes = [r["text"] for r in layouts.with_labels(model, params, rows, "it") if r["stat"].startswith("note_")]
    assert notes and all("@" not in n for n in notes) and any(n.startswith("α₁ è a un limite") for n in notes), notes


# the checks whose verdict a reading says: the row of its p-value, the ids when it keeps and when it rejects
VERDICT_ROWS = {GARCH: [("assumption|ljung_box_z||p_value", "lb_z_ok", "lb_z_failed"),
                        ("assumption|ljung_box_z2||p_value", "lb_z2_ok", "lb_z2_failed"),
                        ("assumption|arch_lm||p_value", "arch_lm_ok", "arch_lm_failed"),
                        ("assumption|nyblom||p_value", "nyblom_stable", "nyblom_moves_none"),
                        ("assumption|sign_bias||p_value", "sign_bias_not_supported", "sign_bias_supported"),
                        ("backtest|kupiec||p_value", "backtest_coverage_ok", "backtest_coverage_failed"),
                        ("backtest|christoffersen_ind||p_value", "backtest_independence_ok", "backtest_independence_failed"),
                        ("backtest|christoffersen_cc||p_value", "backtest_cc_ok", "backtest_cc_failed")],
                # the SARIMA's checks all keep at 0.05 (the runner's output since lot G9): the smallest p-value, Ljung–Box at 2s
                # lags, decides between «all keep» and «one rejects»
                RISK_ARIMA: [("assumption|ljung_box_2s||p_value", "arima_checks_ok", "arima_check_failed")]}


@pytest.mark.parametrize("case", sorted(VERDICT_ROWS))
def test_a_verdict_follows_alpha_just_above_and_just_below_its_p_value(case):
    """The explanations spec's criterion 8: the same rows read with α just above a test's p-value say it rejects, just
    below say it does not; the measures do not change."""
    model, params, rows = fixture(case)
    keyed = {r["key"]: r for r in rows}
    for key, keeps, rejects in VERDICT_ROWS[case]:
        p = keyed[key]["value"]
        for alpha, want, other in ((p * 1.001, rejects, keeps), (p * 0.999, keeps, rejects)):
            checked = {**catalog.check_params(model, params), "alpha": alpha}
            ids = [x[2] for x in common.READINGS[model](rows, checked)]
            assert want in ids and other not in ids, (case, key, alpha, ids)


def test_nyblom_names_the_parameter_that_moves_most_when_its_adjusted_p_value_is_below_alpha():
    model, params, rows = fixture(GARCH)
    moved = [{**r, "value": 0.001} if r["key"] in ("assumption|nyblom||p_value", "assumption|nyblom|skew|p_adjusted") else r
             for r in rows]
    by = {x[2]: x[3] for x in common.READINGS[model](moved, catalog.check_params(model, params))}
    assert by["nyblom_moves"]["parameter"] == "skew" and by["nyblom_moves"]["p_adjusted"] == 0.001
    # a test of z² that rejects brings the size of the largest autocorrelation of z²
    rejected = [{**r, "value": 0.001} if r["key"] == "assumption|arch_lm||p_value" else r for r in rows]
    by = {x[2]: x[3] for x in common.READINGS[model](rejected, catalog.check_params(model, params))}
    acf = [r for r in rows if r["section"] == "lag" and r["level"] == "z2_acf" and r["stat"] == "acf"]
    assert abs(by["z2_largest_acf"]["acf"]) == max(abs(r["value"]) for r in acf) and "arch_lm_failed" in by


@pytest.mark.parametrize("locale", LOCALES)
def test_a_risk_variable_is_named_in_the_page_s_words_and_a_list_of_shares_in_percent(locale):
    model, params, rows = fixture(RISK)
    said = {r["level"]: r["text"] for r in layouts.with_labels(model, params, rows, locale) if r["section"] == "reading"
            and r["level"] in ("threshold", "fan")}
    assert _words(model, locale)["values"]["reading_names"]["variable:price"] in said["threshold"]
    assert "50 % / 90 % / 98 %" in said["fan"], said["fan"]


# ── the pages ───────────────────────────────────────────────────────────────
VARIANTS = {
    GARCH: [{}, {"variance_model": "egarch", "distribution": "norm", "holdout": 0, "input_kind": "returns", "returns_type": None},
            {"variance_model": "arch", "arch_order": 2, "ar_order": 1, "ma_order": 1, "mean": False, "se_type": "classical"},
            {"variance_model": "garch", "garch_order": 2, "arch_order": 3, "distribution": "std"}],
    RISK: [{}, {"holdout": 0, "threshold": None, "position": None, "input_kind": "returns", "variance_model": "egarch"},
           {"quantiles": [0.05, 0.5, 0.95], "tail": "upper", "innovations": "bootstrap", "paths": 10_000_000, "horizon": 1000}],
    RISK_ARIMA: [{}, {"aggregate": "relative_change", "tail": "lower", "holdout": 30, "position": 5000},
                 {"orders": "auto", "p": None, "d": None, "q": None, "P": None, "D": None, "Q": None, "threshold": None}],
}


@pytest.mark.parametrize("locale", LOCALES)
def test_every_variant_of_the_options_is_a_definition_the_engine_accepts(locale):
    columns = {"result": {c["name"]: c["type"] for c in layouts.columns()}}
    for case, variants in VARIANTS.items():
        model, params, _ = fixture(case)
        for v in variants:
            p = {k: x for k, x in {**params, **v}.items() if x is not None}
            d = layouts.definition(model, p, locale, None, "Prices")
            assert [str(x) for x in rd.check(d, columns)] == [], (case, v)
            ids = [it["id"] for band in d["sections"].values() for it in band["items"]]
            assert len(ids) == len(set(ids)), (case, v)
            body = {it["id"]: it for it in d["sections"]["body"]["items"]}
            assert ("backtest_tests" in body) == ((p.get("holdout") or 0) > 0), (case, v)


def test_the_formula_line_is_the_model_of_the_node_s_options():
    w = common.words("garch", "default", "en")
    node = {"time": "data", "value": "prezzo", "frequency": "index"}
    said = lambda **kw: garch_layout.formula(w, catalog.check_params("garch", {**node, **kw}))  # noqa: E731
    assert said() == "r_t = μ + ε_t,  σ²_t = ω + α₁ε²_(t−1) + β₁σ²_(t−1),  z ~ Student t"
    assert said(variance_model="gjr", distribution="sstd") == \
        "r_t = μ + ε_t,  σ²_t = ω + (α₁ + γ₁·I(ε_(t−1) < 0))·ε²_(t−1) + β₁σ²_(t−1),  z ~ skewed Student t"
    assert said(variance_model="egarch", ar_order=1, ma_order=1, distribution="norm") == (
        "r_t = μ + φ₁(r_(t−1) − μ) + θ₁ε_(t−1) + ε_t,  ln σ²_t = ω + α₁z_(t−1) + γ₁(|z_(t−1)| − E|z|) + β₁ ln σ²_(t−1),  z ~ normal")
    assert said(variance_model="arch", arch_order=2, mean=False).startswith("r_t = ε_t,  σ²_t = ω + α₁ε²_(t−1) + α₂ε²_(t−2),")
    it = common.words("garch", "default", "it")
    assert garch_layout.formula(it, catalog.check_params("garch", {**node, "distribution": "sstd"})).endswith("z ~ t di Student asimmetrica")
    assert layouts.title("garch", {"time": "data", "value": "prezzo", "frequency": "index", "variance_model": "gjr",
                                   "distribution": "sstd", "input_kind": "prices"}, "en") == \
        "GJR-GARCH(1,1), skewed Student t — «prezzo» over «data», prices"
    assert layouts.title("risk_simulation", fixture(RISK_ARIMA)[1], "it").startswith("Simulazione del rischio · SARIMA(1,1,1)(0,1,1)[12]")


def test_the_first_page_holds_the_checks_of_the_whole_model_the_backtest_and_the_measures():
    model, params, _ = fixture(GARCH)
    body = [it for it in layouts.definition(model, params, "en")["sections"]["body"]["items"]]
    by = {it["id"]: i for i, it in enumerate(body)}
    assert by["assumptions"] < by["backtest_tests"] < by["measures"] < by["facts"] < by["reading_persistence"] < by["s_volatility"]
    # Nyblom's test of each parameter is the stability table's, not a row of the checks
    assert "ISNULL([level_2])" in body[by["assumptions"]]["filter"] and body[by["nyblom"]]["filter"].endswith('[stat] = "verdict"')
    # the coefficients are named by their symbols
    assert '"omega", "ω"' in body[by["coef"]]["columns"][0]["value"]


def test_the_fan_nests_its_bands_from_the_widest_and_draws_the_sample_paths_apart():
    checked = catalog.check_params("risk_simulation", fixture(RISK)[1])
    pairs, median = risk_layout.bands(checked)
    assert [(lo, hi) for lo, hi, _ in pairs] == [("quantile_0.01", "quantile_0.99"), ("quantile_0.05", "quantile_0.95"),
                                                 ("quantile_0.25", "quantile_0.75")] and median == "quantile_0.5"
    assert [round(s, 2) for _, _, s in pairs] == [0.98, 0.9, 0.5]
    assert risk_layout.bands({**checked, "quantiles": [0.1, 0.9]}) == ([("quantile_0.1", "quantile_0.9", pytest.approx(0.8))], None)
    model, params, _ = fixture(RISK)
    body = {it["id"]: it for it in layouts.definition(model, params, "en")["sections"]["body"]["items"]}
    fan = body["fan"]
    assert fan["split_by"] == 'IF(STARTSWITH([level_2], "path_"), [level_2], "fan")'
    assert [s["as"] for s in fan["series"]][:6] == ["band_low", "band_high"] * 3 and len(fan["series"]) <= 12
    assert fan["options"]["target"] == common.model_number("threshold") and body["fan_steps"]["options"]["x_time"] is False
    # the threshold is on the outcome's scale for a cumulative return of returns, not of a price
    assert len(body["horizon"]["options"]["target_x"]) == 2
    returns = {**params, "input_kind": "returns", "threshold": -0.1}
    assert len({it["id"]: it for it in layouts.definition(model, returns, "en")["sections"]["body"]["items"]}["horizon"]["options"]
               ["target_x"]) == 3


@pytest.mark.parametrize("case", (GARCH, RISK, RISK_ARIMA))
@pytest.mark.parametrize("locale", LOCALES)
def test_the_report_with_every_reading_renders_in_twelve_pages_at_most(tmp_path, chart_service, monkeypatch, case, locale):  # noqa: F811
    model, params, rows = fixture(case)
    no_readings(monkeypatch, model)
    _with(monkeypatch, model, params, full_rows(model, rows))
    out, page = draw(tmp_path, chart_service, monkeypatch, case, locale=locale)
    assert out.path.read_bytes().startswith(b"%PDF-") and 4 <= out.pages <= 12 and out.warnings == [], (out.pages, out.warnings)
    said = set(strings(json.loads(page)))
    assert not [s for s in said if re.search(r"=\s*[<>]", s)]
    w = _words(model, locale, case)
    assert w["sections"]["how"] in said and w["alpha_line"].split("{")[0].strip() in " ".join(said)


def test_excel_keeps_the_checks_the_backtest_the_measures_and_the_reading(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """Each table is a sheet (its title cut to Excel's 31 characters, without the characters a sheet's name may not hold)."""
    for case, sheets in ((GARCH, ("Checks of the assumptions", "Backtest of the 1-step VaR", "Measures, not tests", "Facts of the fit",
                                  "Stability of the parameters", "Breaks of the VaR", "Reading")),
                         (RISK, ("Measures, not tests", "Checks of the assumptions", "Coefficients of the model", "Reading"))):
        out, _ = draw(tmp_path / case, chart_service, monkeypatch, case, fmt="xlsx")
        with zipfile.ZipFile(out.path) as z:
            names = set(re.findall(r'<sheet name="([^"]+)"', z.read("xl/workbook.xml").decode()))
        assert all(any(n.startswith(s) for n in names) for s in sheets), (case, sorted(names))


def test_the_report_of_the_largest_fixture_stays_well_under_200_mb(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """The report on the largest fixture of the two nodes (the GARCH runner's output, its readings chosen by the layout):
    `tracemalloc`'s peak, recorded in the spec's «As built / G4»."""
    model, params, rows = fixture(GARCH)
    tracemalloc.start()
    try:
        out, _ = draw(tmp_path, chart_service, monkeypatch, GARCH, locale="de")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert out.pages >= 4 and peak < 200 * 2**20, peak
    print(f"tracemalloc peak of the report on {GARCH}: {peak / 2**20:.1f} MiB")


# ── lot G9: the final review's corrections ─────────────────────────────────
def test_the_risk_runner_s_own_results_say_the_money_and_the_quantile_the_es_averages_beyond():
    """The risk fixtures are the runner's output: with a position the VaR's and the ES's money (`risk|…||money`, position ×
    the value) is read as the rows hold it, said by `var_money` and `es_money`; the ES's sentence names the quantile it
    averages beyond (the last step of `curve|term|es_quantile`), never a number the page computes."""
    model, params, rows = fixture(RISK)
    r = garch_layout.Rows(rows)
    lines = common.READINGS[model](rows, catalog.check_params(model, params))
    by = {x[2]: x[3] for x in lines}
    assert by["var_money"] == {"amount": 100000, "money": r.num("risk|var||money")}
    assert r.num("risk|var||money") == pytest.approx(100000 * r.num("risk|var||value"), rel=1e-12)
    assert by["es_money"]["money"] == pytest.approx(100000 * r.num("risk|es||value"), rel=1e-12)
    term = [x for x in rows if x["section"] == "curve" and x["level"] == "term" and x.get("level_2") == "es_quantile"]
    last = max(term, key=lambda x: x["x0"])["value"]
    # on the lower tail the ES averages the values at or below that quantile, and the 99 % VaR lies below the 97.5 % quantile
    assert by["es_lower_quantile"]["quantile"] == last and by["es_lower_quantile"]["value"] <= last and r.num("risk|var||value") <= last
    # an ARIMA's running total on its upper tail: in its units, no money, the ES above the quantile it averages beyond
    model, params, rows = fixture(RISK_ARIMA)
    by = {x[2]: x[3] for x in common.READINGS[model](rows, catalog.check_params(model, params))}
    assert "var_money" not in by and "es_money" not in by
    assert by["es_upper_level_quantile"]["value"] >= by["es_upper_level_quantile"]["quantile"]
    # results of a runner before lot G9 (no es_quantile row): the sentence without the quantile, never an empty «{quantile}»
    older = [x for x in rows if x.get("level_2") != "es_quantile"]
    assert "es_upper_level" in [x[2] for x in common.READINGS[model](older, catalog.check_params(model, params))]


@pytest.mark.parametrize("locale", LOCALES)
def test_the_risk_runner_s_notes_are_sentences_in_every_language(locale):
    """Lot G3's notes without words: the simple returns below −100 %, the values out of Box–Cox's range, and the ARIMA's own
    notes in the risk node (as the ARIMA report says them); the GARCH's notes of the fixture too. No «@», no «{»."""
    model, params, rows = fixture(RISK_ARIMA)
    last = max(r["ord"] for r in rows)
    notes = ["@returns_below_minus_one n=1234", "@box_cox_domain n=3", "@ml_only", "@gap n=1", "@gaps n=4", "@drift_dropped",
             "@not_converged code=1"]
    added = [{"section": "model", "level": None, "level_2": None, "stat": f"note_{90 + i}", "value": None, "text": t, "x0": None,
              "x1": None, "ord": last + 1 + i, "key": f"model|||note_{90 + i}"} for i, t in enumerate(notes)]
    said = {r["stat"]: r["text"] for r in layouts.with_labels(model, params, rows + added, locale) if r["stat"].startswith("note_")}
    for i, t in enumerate(notes):
        text = said[f"note_{90 + i}"]
        assert "@" not in text and "{" not in text and text != t, (locale, t, text)
    assert common.number_form(1234.0, "int", formats.locale(locale)) in said["note_90"] and "3" in said["note_91"], said
    arima = common.words("arima", catalog.variant("arima", {"p": 1}), locale)["note_texts"]
    assert said["note_92"] == arima["ml_only"] and said["note_95"] == arima["drift_dropped"]
    model, params, rows = fixture(RISK)
    garch_notes = [r["text"] for r in layouts.with_labels(model, params, rows, locale) if r["stat"].startswith("note_")]
    assert garch_notes and not [n for n in garch_notes if "@" in n or "{" in n], garch_notes


def test_with_a_position_the_measures_table_says_the_var_and_the_es_in_money_and_a_percent_keeps_its_sign_on_its_line(
        tmp_path, chart_service, monkeypatch):  # noqa: F811
    """The money rows of the measures' table: their names, the number as the runner wrote it (no %, no interval); a percent's
    «%» is held to its number by a no-break space, so the interval column never ends a line with «%» alone (G8's finding)."""
    model, params, rows = fixture(RISK)
    body = {it["id"]: it for it in layouts.definition(model, params, "it")["sections"]["body"]["items"]}
    measures = body["measures"]
    assert '[stat] = "money"' in measures["filter"] and " %" in measures["columns"][2]["value"]
    assert [c.get("width") for c in measures["columns"]] == [42, 21, 40, None]
    out, page = draw(tmp_path, chart_service, monkeypatch, RISK, fmt="svg", locale="en")
    said = " ".join(strings(json.loads(page)))
    w = _words(model, "en")
    money = garch_layout.Rows(rows).num("risk|var||money")
    assert w["risk_names"]["var_money"] in said and w["risk_names"]["es_money"] in said
    assert formats.format_number(abs(money), "#,##0.##", formats.locale("en")) in said
    assert out.warnings == []


def test_the_short_tables_of_both_reports_are_never_split_and_no_other_report_asks_it():
    """G8's finding: a table's header and a row or two left at a page's foot. The two layouts ask their short tables to stay
    whole (`keep_whole`, at most half a page: the template measures it); the other models' layouts ask nothing."""
    for case in (GARCH, RISK, RISK_ARIMA):
        model, params, _ = fixture(case)
        tables = [it for it in layouts.definition(model, params, "en")["sections"]["body"]["items"] if it["type"] == "table"]
        whole = {it["id"] for it in tables if it.get("keep_whole")}
        assert whole and whole <= set(garch_layout.WHOLE) and "reading" not in whole and "fit" not in whole, (case, whole)
        assert {"measures", "coef", "coef_ci", "notes"} <= whole, (case, whole)
    model, params, _ = fixture("arima-manual")
    assert not [it for it in layouts.definition(model, params, "en")["sections"]["body"]["items"] if it.get("keep_whole")]


def test_the_risk_report_puts_the_model_simulated_before_the_backtest_and_garch_draws_the_fitted_law_on_the_histogram():
    """The model's tables (they break by rows) fill the page the charts leave, before the backtest's 90 mm chart (G8: the
    third page half empty). The histogram of z has the counts the fitted law expects (`hist|std_residual|expected`) as a line."""
    model, params, _ = fixture(RISK)
    ids = [it["id"] for it in layouts.definition(model, params, "en")["sections"]["body"]["items"]]
    assert ids.index("probabilities") < ids.index("coef") < ids.index("coef_ci") < ids.index("s_backtest") < ids.index("backtest")
    model, params, rows = fixture(GARCH)
    hist = {it["id"]: it for it in layouts.definition(model, params, "en")["sections"]["body"]["items"]}["hist"]
    assert hist["kind"] == "combo" and [s.get("as") for s in hist["series"]] == [None, "line"]
    expected = [r for r in rows if r["section"] == "hist" and r.get("level_2") == "expected"]
    seen = [r for r in rows if r["section"] == "hist" and r["level"] == "std_residual" and not r.get("level_2")]
    assert len(expected) == len(seen) >= 5 and sum(r["value"] for r in expected) <= sum(r["value"] for r in seen)


@pytest.mark.parametrize("case", (GARCH, RISK, RISK_ARIMA))
def test_a_column_named_like_a_formula_or_markup_stays_text_in_the_pdf_and_in_excel(tmp_path, chart_service, monkeypatch, case):  # noqa: F811
    """Security (the decomposition's D9 test, for both nodes): the value column's name travels into the header, the
    summary's reading (`«{series}»`), an ARIMA's formula and the Excel workbook. It stays a text everywhere: no Excel formula,
    no report expression, no Typst markup."""
    hostile = '=HYPERLINK("http://x","y") #set ] 1% <b>'
    model, params, rows = fixture(case)
    old = params["value"]
    rows = [{**r, "text": r["text"].replace(old, hostile)} if r["key"] == "model|||formula" and isinstance(r.get("text"), str) else r
            for r in rows]
    _with(monkeypatch, model, {**params, "value": hostile}, rows)
    _, page = draw(tmp_path / "svg", chart_service, monkeypatch, case, fmt="svg")
    said = " ".join(strings(json.loads(page)))
    assert f"«{hostile}»" in said  # the header and the summary's reading
    out, _ = draw(tmp_path / "pdf", chart_service, monkeypatch, case)
    assert out.path.read_bytes().startswith(b"%PDF-") and out.warnings == []
    out, _ = draw(tmp_path / "xlsx", chart_service, monkeypatch, case, fmt="xlsx")
    with zipfile.ZipFile(out.path) as z:
        xml = " ".join(z.read(n).decode() for n in z.namelist() if n.startswith("xl/worksheets/") or n == "xl/sharedStrings.xml")
    assert "HYPERLINK" in xml and not re.search(r"<f>[^<]*HYPERLINK", xml)
    assert not [f for f in re.findall(r"<f>([^<]+)</f>", xml) if not f.startswith("SUBTOTAL(")]
