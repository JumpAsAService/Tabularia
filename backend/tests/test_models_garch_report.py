"""The reports of «Volatility (GARCH)» and «Risk simulation» (docs/specs/garch-risk.md, lot G4): every sentence the runners
may write in the five languages, the owner's rule on verdicts (only the tests with a p-value against α: the weighted
Ljung–Box, ARCH LM, Nyblom, the sign bias, Kupiec, Christoffersen; VaR, ES, persistence, half-life and probabilities are
measures, with no word of judgement), the backtest's sentences saying how many breaks were expected, the pages of each
report, and the memory of the report on the largest fixture.

The fixtures (`garch-default`, `risk_simulation-garch`, `risk_simulation-arima`, lot G0, their readings the contract of lot
G4) hold a few readings each; `full_rows` puts a line of every template in their place, with the names the runners (lots G1
and G3) write: the contract of «As built / G4»."""
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
        "asymmetry_gjr": {"ratio": 3.25, "gamma": 0.09}, "asymmetry_gjr_falls_only": {"gamma": 0.12},
        "asymmetry_egarch": {"shock": 2, "ratio": 1.4, "alpha": -0.08, "gamma": 0.15}, "asymmetry_none": {},
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
ES = {"p": 0.025, "variable": "variable:cumulative_return", "quantile": -0.0569, "value": -0.0868, "low": -0.0901, "high": -0.0835}
ES_LEVEL = {**ES, "variable": "variable:running_total", "quantile": 1520.5, "value": 1557.1, "low": 1555.1, "high": 1559.0}
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
    "var": {"var_lower": SHAPE, "var_lower_level": LEVEL, "var_upper": SHAPE, "var_upper_level": LEVEL,
            "var_money": {"amount": 100000, "money": -7115.18}},
    "es": {"es_lower": ES, "es_lower_level": ES_LEVEL, "es_upper": ES, "es_upper_level": ES_LEVEL,
           "es_money": {"amount": 100000, "money": -8680.52}},
    "threshold": {"threshold_lower": THRESHOLD, "threshold_lower_level": {**THRESHOLD, "variable": "variable:price", "threshold": 280},
                  "threshold_upper": THRESHOLD, "threshold_upper_level": {**THRESHOLD, "variable": "variable:value", "threshold": 1000}},
    "drawdown": {"drawdown": {"median": 0.021, "tail": 0.087, "p": 0.01, "steps": 10, "unit": "index"}},
    "shape": {"shape": {"variable": "variable:relative_change", "steps": 10, "unit": "index", "skewness": -0.4, "kurtosis": 2.1}},
    "fan": {"fan": {"shares": [0.5, 0.9, 0.98], "error": 4e-06, "sample": 20}},
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
                  "risk_simulation": {"var_lower", "var_lower_level", "var_upper", "var_upper_level", "var_money", "es_lower",
                                      "es_lower_level", "es_upper", "es_upper_level", "es_money", "threshold_lower",
                                      "threshold_lower_level", "threshold_upper", "threshold_upper_level", "drawdown"}}
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
    """The fixture's rows with a line of every template (of those in `only`) in place of its readings."""
    kept = [r for r in rows if r["section"] != "reading"]
    lines = [(block, None, id_, values) for block, ids in READINGS[model].items() for id_, values in ids.items()
             if only is None or id_ in only]
    return kept + common.reading_rows(lines, kept)


def _with(monkeypatch, model: str, params: dict, rows: list[dict]) -> None:
    """`draw` and `fixture` of test_models_layouts on these parameters and rows."""
    import tests.test_models_layouts as me

    monkeypatch.setattr(me, "fixture", lambda case: (model, params, rows))


# ── the words ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("model", sorted(READINGS))
def test_every_template_the_runner_may_write_has_its_sentence_its_names_and_a_label(model):
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


# ── the readings said ───────────────────────────────────────────────────────
@pytest.mark.parametrize("case", (GARCH, RISK, RISK_ARIMA))
@pytest.mark.parametrize("locale", LOCALES)
def test_every_reading_is_a_sentence_of_the_page_s_language(case, locale):
    model, params, rows = fixture(case)
    for given in (rows, full_rows(model, rows)):
        said = [r for r in layouts.with_labels(model, params, given, locale) if r["section"] == "reading"]
        assert said and {r["level"] for r in said} >= {"summary", "method", "formulas"}, case
        for r in said:
            assert not re.search(r"@|\{|%2", r["text"]), (case, r["text"])
            assert not re.search(r"=\s*[<>]", r["text"]), (case, r["text"])
            assert r["label"] not in ("", r["stat"]), (case, r)
    decimal = formats.locale(locale).decimal
    if model == "garch":  # a parameter is said by its symbol, a distribution in the page's words
        said = {r["text"] for r in layouts.with_labels(model, params, full_rows(model, rows, {"nyblom_moves", "summary"}), locale)
                if r["section"] == "reading"}
        assert any(" ω " in s and f"0{decimal}031" in s for s in said), said
        assert any(_words(model, locale)["values"]["distribution"]["sstd"] in s for s in said), said


@pytest.mark.parametrize("locale", LOCALES)
def test_a_risk_variable_is_named_in_the_page_s_words_and_a_list_of_shares_in_percent(locale):
    model, params, rows = fixture(RISK)
    said = {r["level"]: r["text"] for r in layouts.with_labels(model, params, full_rows(model, rows, {"summary_variable", "fan"}), locale)
            if r["section"] == "reading"}
    assert _words(model, locale)["values"]["reading_names"]["variable:price"] in said["summary"]
    decimal = formats.locale(locale).decimal
    assert "50 % / 90 % / 98 %" in said["fan"] and f"0{decimal}000004" in said["fan"], said["fan"]


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
    assert layouts.title("risk_simulation", fixture(RISK_ARIMA)[1], "it").startswith("Simulazione del rischio · SARIMA(1,0,1)(0,1,1)[7]")


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
    """The report task on the largest fixture of the two nodes, every reading in it: `tracemalloc`'s peak, recorded in the
    spec's «As built / G4»."""
    model, params, rows = fixture(GARCH)
    _with(monkeypatch, model, params, full_rows(model, rows))
    tracemalloc.start()
    try:
        out, _ = draw(tmp_path, chart_service, monkeypatch, GARCH, locale="de")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert out.pages >= 4 and peak < 200 * 2**20, peak
    print(f"tracemalloc peak of the report on {GARCH}: {peak / 2**20:.1f} MiB")
