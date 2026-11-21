"""The report of a statistical model (app/engine/models/layouts/, docs/specs/statistical-models.md,
«The report»): every model and variant drawn from its fixture (backend/tests/fixtures/models/,
the runners' output once they exist) in the five languages, the words complete, the blocks
every model has, the route and the task that draw it from the results a run kept."""
from __future__ import annotations

import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from fastapi import HTTPException

from app.api import exports
from app.api.routes import reports as report_routes
from app.api.routes import tasks
from app.engine.models import catalog, layouts
from app.engine.models.layouts import common
from app.engine.models.layouts import parametric_survival as ps_layout
from app.engine.models.results import read_fixture
from app.engine.stats import layouts as stats_layouts
from app.engine.stats.layouts import LOCALES
from app.engine.stats.output import OUTPUT_NAMES
from app.reports import definition as rd
from app.reports import charts, formats, glyphs, pdf, render
from app.tasks import jobs, report_jobs
from tests.fakes import FakeRedis
from tests.test_export_route import Result, Storage, _play
from tests.test_reports_render import chart_service  # noqa: F401 — the fake chart service

NOW = datetime(2026, 11, 19, 23, 30, tzinfo=timezone.utc)
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "models"
CASES = sorted(p.stem for p in FIXTURES.glob("*-*.json"))
DRAWN = dict(catalog.MODELS)
# the experiments' reports open with their decision, not with the checks, and the planning node has no check at all
# (docs/specs/ab-testing.md, «The report (pixel perfect)»)
EXPERIMENTS = {m for m, spec in catalog.MODELS.items() if spec.get("family") == "experiments"}
# the reports that explain their method in words (the A/B's mechanism: docs/specs/ts-decomposition.md, «Explaining the
# method in the report»): a page or two more, and no «= <» in a sentence
EXPLAINED = EXPERIMENTS | {"decomposition"}


def fixture(case: str) -> tuple[str, dict, list[dict]]:
    """The model, its parameters as the node has them, its rows."""
    data = json.loads((FIXTURES / f"{case}.json").read_text(encoding="utf-8"))
    return data["model"], data["params"], [dict(zip(OUTPUT_NAMES, r)) for r in data["rows"]]


def strings(node) -> list[str]:
    if isinstance(node, str):
        return [node]
    if isinstance(node, dict):
        return [s for v in node.values() for s in strings(v)]
    if isinstance(node, list):
        return [s for v in node for s in strings(v)]
    return []


def _keys(node, prefix=""):
    if isinstance(node, dict):
        return {k for name, v in node.items() for k in _keys(v, f"{prefix}{name}.")}
    return {prefix.rstrip(".")}


def draw(tmp_path, chart_service, monkeypatch, case, fmt="pdf", locale="en", sample=None, step=None, rows=None):  # noqa: F811
    """The report of a fixture (or of `rows` in its place), and what the template was handed (the page's texts)."""
    handed: list[str] = []
    run = pdf.run

    def spy(job, *args, **kwargs):
        if "inputs" in job:
            handed.append(job["inputs"]["model"])
        return run(job, *args, **kwargs)

    monkeypatch.setattr(pdf, "run", spy)
    model, params, fixed = fixture(case)
    datasets = {"result": {"rows": layouts.with_labels(model, params, rows if rows is not None else fixed, locale), "columns": layouts.columns()}}
    out = render.render(layouts.definition(model, params, locale, sample, step), datasets, {}, fmt, tmp_path,
                        title=layouts.title(model, params, locale), now=NOW,
                        limits=render.Limits(chart_url=chart_service[0], max_rows=layouts.MAX_REPORT_ROWS))
    return out, (handed[0] if handed else "")


# ── the catalogue, the fixtures, the words ──────────────────────────────────
def test_every_model_and_variant_has_a_layout_and_a_fixture():
    assert set(layouts.LAYOUTS) == set(DRAWN)
    assert set(CASES) == {f"{m}-{v['id']}" for m, spec in DRAWN.items() for v in spec["variants"]}
    for case in CASES:  # each fixture is what its variant writes (results.py)
        model, params, table = read_fixture(FIXTURES / f"{case}.json")
        assert f"{model}-{catalog.variant(model, params)}" == case


def test_the_words_are_complete_in_the_five_languages():
    for name in ("common", "penalized", "boosting", "customer_lifetime", "experiments", *DRAWN):
        words = common._file(name)
        assert tuple(words) == LOCALES, name
        english = _keys(words["en"])
        for code in LOCALES:
            assert _keys(words[code]) == english, f"{name}, {code}: {sorted(_keys(words[code]) ^ english)}"
            assert all(isinstance(s, str) and s.strip() for s in strings(words[code])), (name, code)
            assert glyphs.missing(set("".join(strings(words[code])))) == set(), (name, code)


@pytest.mark.parametrize("case", CASES)
def test_every_number_and_check_a_variant_writes_has_its_name_in_each_language(case):
    model, params, rows = fixture(case)
    variant = catalog.variant(model, catalog.check_params(model, params))
    spec = catalog.variant_spec(model, catalog.check_params(model, params))
    for code in LOCALES:
        w = common.words(model, variant, code)
        for section in ("model", "coef", "stratum", "pair", "candidate", "parameter", "customer"):
            declared = spec["sections"].get(section, {})
            for stat in [*declared.get("numbers", []), *(t for t in declared.get("texts", []) if section == "model")]:
                if stat.endswith("*") or stat.endswith("significance") or stat in ("model", "variant", "formula", "software",
                                                                                    "group_version", "rows_from"):
                    continue
                assert w["stats"].get(section, {}).get(stat), f"{code}: no word for {section}.{stat}"
        for check in spec["sections"].get("assumption", {}).get("levels", []):
            said = w["checks"].get(check, {})
            # a check that is a measure has no H0 and no advice: its sentence is in `measure_texts`
            assert said.get("name") and ((said.get("h0") and (said.get("advice") or said.get("not_computed")))
                                         or w.get("measure_texts", {}).get(check)), f"{code}: {check}"
        for row in layouts.with_labels(model, params, rows, code):  # the numbered ones too (note_1, surv_at_30)
            if row["section"] in ("model", "stratum") and row["stat"].startswith(("note_", "surv_at_", "quantile_", "ar_root", "ma_root")):
                assert row["label"] != row["stat"], (code, row["stat"])
    assert common.stat_label(common.words("kaplan_meier", "default", "it"), "stratum", "surv_at_30_low") == \
        "Sopravvivenza a 30, estremo inferiore"
    assert common.stat_label(common.words("linear", "unpenalized", "de"), "model", "newer_than_the_words") == "newer_than_the_words"


def test_the_runner_s_english_is_said_in_the_page_s_language():
    model, params, rows = fixture("linear-unpenalized")
    said = {(r["section"], r["level"], r["level_2"], r["stat"]): r for r in layouts.with_labels(model, params, rows, "it")}
    # H0 in the words of the page; the check's name on its verdict's row
    assert said[("assumption", "breusch_pagan", None, "h0")]["text"] == "La varianza dei residui non cambia con i predittori."
    assert said[("assumption", "breusch_pagan", None, "verdict")]["label"] == "Omoschedasticità: Breusch–Pagan"
    assert said[("assumption", "breusch_pagan", None, "verdict")]["text"] == "failed"
    # the advice of a check that failed, none for one that holds
    assert said[("assumption", "breusch_pagan", None, "advice")]["text"] == \
        "Usate errori standard robusti (se = hc3); se sono già robusti, l'inferenza non dipende da questa ipotesi."
    assert said[("assumption", "reset", None, "advice")]["text"] is None
    # a setting's value, a histogram's interval with the language's decimal
    assert said[("model", None, None, "se_type")]["text"] == "HC3 (robusti all'eteroschedasticità)"
    first = next(r for r in layouts.with_labels(model, params, rows, "it") if r["section"] == "hist")
    assert first["text"] == "[-10,7 – -8,2)"
    km = layouts.with_labels(*fixture("kaplan_meier-default")[:2], fixture("kaplan_meier-default")[2], "en")
    [informative] = [r for r in km if r["level"] == "censoring_informative" and r["stat"] == "advice"]
    assert informative["text"].startswith("It cannot be tested from the data")


def test_a_card_too_large_is_a_note_in_the_page_s_language():
    """The runner's note when the model card is over max_card_bytes (common/R/card.R): the fit
    went on, and the page says why the model is not in MLflow, in its language, whatever the model."""
    english = ("The model card would be larger than a card can be (1 MiB): the results and the report are complete, but the "
               "model is not logged to MLflow and no Predict node can apply it.")
    for case in ("kaplan_meier-default", "linear-unpenalized", "arima-manual", "xgboost-binary", "catboost-multiclass"):
        model, params, rows = fixture(case)
        note = dict(zip(OUTPUT_NAMES, ["model", None, None, "note_card_too_large", None, english, None, None, len(rows),
                                        "model|||note_card_too_large"]))
        for code, starts in (("en", "The model card"), ("it", "La scheda del modello"), ("de", "Die Modellkarte")):
            [said] = [r for r in layouts.with_labels(model, params, [*rows, note], code) if r["stat"] == "note_card_too_large"]
            assert said["text"].startswith(starts) and said["label"] != "note_card_too_large", (case, code, said)
    # a Kaplan–Meier's card thinned within its tolerance (kaplan_meier.R, km_card_steps)
    english = ("The curves have more steps than the model card keeps: the card keeps a step wherever the survival or an end of "
               "its interval has moved by 0.001 or more, so that a survival predicted from it differs from the fitted curve by "
               "less than 0.001.")
    model, params, rows = fixture("kaplan_meier-default")
    note = dict(zip(OUTPUT_NAMES, ["model", None, None, "note_card_thinned", None, english, None, None, len(rows),
                                    "model|||note_card_thinned"]))
    [said] = [r for r in layouts.with_labels(model, params, [*rows, note], "it") if r["stat"] == "note_card_thinned"]
    assert said["text"].startswith("Le curve hanno più gradini") and "0,001" in said["text"]


# ── the definition ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("locale", LOCALES)
def test_a_layout_is_a_definition_the_report_engine_accepts(case, locale):
    model, params, _ = fixture(case)
    columns = {"result": {c["name"]: c["type"] for c in layouts.columns()}}
    for sample in (None, [{"first": 100_000}]):
        d = layouts.definition(model, params, locale, sample, "Prices compared")
        assert [str(p) for p in rd.check(d, columns)] == []
        assert d["locale"] == locale and [x["name"] for x in d["datasets"]] == ["result"]
        assert d["page"]["size"] == "A4" and d["page"]["orientation"] == "portrait"
        ids = [it["id"] for band in d["sections"].values() for it in band["items"]]
        assert len(ids) == len(set(ids))
        body = d["sections"]["body"]["items"]
        first = {it["id"]: i for i, it in enumerate(body)}
        if model in EXPERIMENTS:  # the decision (or the plan's answer) first, the explanation and the notes last
            assert body[0]["id"] in ("s_decision", "s_plan") and first["reading"] > first["s_how"]
            assert ("assumptions" in first) == (model == "ab_test")
        else:  # the assumptions come before every coefficient, number of the fit and chart: the first page
            assert first["assumptions"] < min(i for i, it in enumerate(body) if it["type"] == "chart")
            assert first["assumptions"] < first["fit"] and first["assumptions"] < first.get("coef", 10**6)
        # no chart title is cut
        assert not [it["title"] for it in body if it["type"] == "chart" and it["title"].endswith("…")]


def test_the_name_of_a_report_is_its_model_and_its_columns():
    assert layouts.title("linear", {"outcome": "quantity", "predictors": ["price", "channel"]}, "en") == \
        "Linear regression — quantity ~ price + channel"
    assert layouts.title("linear", {"outcome": "q", "predictors": ["p"], "penalty": "lasso"}, "it") == "Regressione lineare — lasso — q ~ p"
    assert layouts.title("logistic", {"outcome": "churned", "event_value": True, "predictors": ["tenure"], "link": "probit"}, "en") == \
        "Logistic regression — probit — churned = true ~ tenure"
    assert layouts.title("arima", {"time": "month", "value": "boxes", "frequency": "month", "p": 1, "d": 1, "q": 1, "D": 1, "Q": 1}, "en") \
        == "SARIMA(1,1,1)(0,1,1)[12] — boxes over month"
    assert layouts.title("arima", {"time": "month", "value": "boxes", "frequency": "month", "orders": "auto"}, "de") == \
        "ARIMA (automatische Ordnungen) — boxes über month"
    assert layouts.title("kaplan_meier", {"time": "days", "event": "status", "event_value": "failed", "strata": "line"}, "fr") == \
        "Kaplan–Meier — days, status = failed par line"


def test_a_parametric_survival_with_delayed_entry_names_its_start_and_says_so_in_each_language():
    # the report's subject is the interval (start–time), as Cox's; the runner's note is a sentence of the page
    title = layouts.title("parametric_survival", {"time": "days", "event": "status", "event_value": "failed", "start": "entered"}, "en")
    assert "entered–days" in title and "status = failed" in title
    assert "entered" not in layouts.title("parametric_survival", {"time": "days", "event": "status", "event_value": "failed"}, "en")
    model, params, rows = fixture("parametric_survival-ph_aft")
    rows = [*rows, {"section": "model", "level": None, "level_2": None, "stat": "note_start_rows", "value": None,
                    "text": "@start_rows delayed=41 rows=106", "x0": None, "x1": None}]
    for locale, words in (("en", "41 of 106 rows enter the study after time 0"), ("it", "41 righe su 106 entrano nello studio dopo il tempo 0"),
                          ("de", "41 von 106 Zeilen treten nach dem Zeitpunkt 0"), ("es", "41 de 106 filas entran en el estudio después del tiempo 0"),
                          ("fr", "41 lignes sur 106 entrent dans l'étude après l'instant 0")):
        said = {(r["section"], r["stat"]): r for r in layouts.with_labels(model, params, rows, locale)}
        assert words in said[("model", "note_start_rows")]["text"], (locale, said[("model", "note_start_rows")]["text"])
        assert "Kaplan" in said[("model", "note_start_rows")]["text"]


# ── drawn ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("locale", LOCALES)
def test_every_report_renders_in_the_five_languages(tmp_path, chart_service, monkeypatch, case, locale):  # noqa: F811
    out, page = draw(tmp_path, chart_service, monkeypatch, case, locale=locale, sample=[{"first": 500}], step="Prices compared")
    model = case.split("-")[0]
    assert out.path.read_bytes().startswith(b"%PDF-") and 2 <= out.pages <= (12 if model in EXPLAINED else 8) and out.warnings == []
    said = set(strings(json.loads(page)))
    w = common.words(*case.split("-"), locale)
    assert any(s.startswith(w["footer"] + " · ") for s in said)
    assert w["sections"]["how" if model == "ab_plan" else "assumptions"] in said
    if model in EXPLAINED:  # a p-value below what four decimals write is «p < 0.0001», never «p = < 0.0001»
        assert not [s for s in said if re.search(r"=\s*[<>]", s)], [s for s in said if re.search(r"=\s*[<>]", s)]


def test_the_first_page_holds_the_assumptions_with_r_s_stars_and_the_verdicts(tmp_path, chart_service, monkeypatch):  # noqa: F811
    del chart_service[1][:]
    out, page = draw(tmp_path, chart_service, monkeypatch, "linear-unpenalized", fmt="svg")
    pages = json.loads(out.path.read_text())["pages"]
    first = pages[0]
    for text in ("Linear regression", "quantity ~ price + channel", "Breusch–Pagan", "× failed", "✓ ok", "0.0022", "***",
                 "Signif. codes:"):
        assert text in first or text in page, text
    said = set(strings(json.loads(page)))
    assert {"0.7463", "117", "F test: p < 0.0001 ***", "HC3 (heteroscedasticity-consistent)"} <= said
    # the charts the variant declares, with their marks
    asked = {spec["option"]["title"]["text"]: spec["option"] for spec in chart_service[1] if "title" in spec["option"]}
    resid = asked["Residuals against fitted values"]
    assert [s["type"] for s in resid["series"]][:2] == ["scatter", "line"]
    assert any(p.get("t") == "76" for p in resid["series"][0]["data"])  # the point the runner named
    observed = asked["Observed against fitted values"]
    assert any(isinstance(m, list) for s in observed["series"] for m in s.get("markLine", {}).get("data", []))  # the diagonal
    assert asked["Coefficients with their intervals (the intercept left out)"]["series"][0]["type"] == "boxplot"


def test_a_check_s_value_says_what_it_counts_and_its_threshold_and_a_table_p_value_its_bound(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """The column «Value» of the assumptions: the number read, what is counted in words, the
    threshold of the verdict (never a bare count: «9» could be Cook's distance or nine rows);
    a p-value read from tseries' tables at its end says «≥ 0.1» (KPSS) rather than 0.1."""
    model, params, rows = fixture("cox-default")
    said = {(r["level"], r["level_2"], r["stat"]): r for r in layouts.with_labels(model, params, rows, "it")}
    assert said[("epv", None, "count")]["text"] == "106 eventi"
    assert said[("dfbeta", "load", "count")]["text"] == "0 righe oltre la soglia"
    km = layouts.with_labels(*fixture("kaplan_meier-default")[:2], fixture("kaplan_meier-default")[2], "en")
    [few] = [r for r in km if r["level"] == "few_at_risk" and r["stat"] == "count"]
    assert few["text"] == "3 strata read where few are at risk"
    one = common.count_said(common.words("linear", "unpenalized", "en"), "cooks_distance", 1, ",")
    assert one == "1 row over the threshold"
    _, page = draw(tmp_path, chart_service, monkeypatch, "cox-default", fmt="svg")
    texts = set(strings(json.loads(page)))
    assert any("106 events (threshold 10)" in t for t in texts), sorted(t for t in texts if "event" in t or "threshold" in t)
    assert any(t.endswith("(threshold 1)") for t in texts)
    _, page = draw(tmp_path, chart_service, monkeypatch, "kaplan_meier-default", fmt="svg")
    assert any("3 strata read where few are at risk (threshold 10)" in t for t in set(strings(json.loads(page))))
    _, page = draw(tmp_path, chart_service, monkeypatch, "arima-manual", fmt="svg")
    texts = set(strings(json.loads(page)))
    assert "≥ 0.1" in texts and "0.2001" in texts  # KPSS at the end of its table; ADF inside it


def test_a_penalized_logistic_says_its_auc_out_of_fold_fold_by_fold(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """The folds' fits have different intercepts: the AUC of all the out-of-fold predictions
    together mixes their scales (0.45 for a ridge where the cross-validation said 0.75). The
    runner writes the mean of the folds' AUCs with its standard error between folds, as
    cv.glmnet's cvm; no DeLong interval of the pooled predictions is declared or drawn."""
    spec = catalog.variant_spec("logistic", catalog.check_params("logistic", {"outcome": "y", "predictors": ["x"],
                                                                             "penalty": "ridge", "event_value": 1}))
    numbers = spec["sections"]["model"]["numbers"]
    assert {"auc", "auc_se", "auc_folds", "brier_se"} <= set(numbers) and not {"auc_ci_low", "auc_ci_high"} & set(numbers)
    del chart_service[1][:]
    _, page = draw(tmp_path, chart_service, monkeypatch, "logistic-penalized", fmt="svg")
    texts = set(strings(json.loads(page)))
    assert "0.759" in texts and "mean of 10 folds · SE 0.030" in texts, sorted(t for t in texts if "fold" in t)
    asked = {spec["option"]["title"]["text"] for spec in chart_service[1] if "title" in spec["option"]}
    assert "ROC curve, out of fold: the mean of the folds' curves" in asked, asked


def test_a_survival_curve_is_a_step_with_its_band_and_the_number_at_risk_under_it(tmp_path, chart_service, monkeypatch):  # noqa: F811
    del chart_service[1][:]
    out, page = draw(tmp_path, chart_service, monkeypatch, "kaplan_meier-default", locale="it")
    asked = {spec["option"]["title"]["text"]: spec["option"] for spec in chart_service[1] if "title" in spec["option"]}
    km = next(o for t, o in asked.items() if t.startswith("Sopravvivenza per strato"))
    steps = [s for s in km["series"] if s.get("step") == "end" and "stack" not in s]
    strata = ["line=A", "line=B", "line=C"]
    assert [s["name"] for s in steps] == strata and km["legend"]["data"] == strata
    assert len([s for s in km["series"] if s.get("areaStyle")]) == 3  # a band each
    said = set(strings(json.loads(page)))
    # the number at risk at 0 and 50 of line=A, the median of line=B with its interval, a time asked, a pair
    assert {"Strato \\ tempo", "59", "39", "Tempo", "30", "58", "line=A – line=B", "p corretto (Holm)"} <= said
    assert "48 – 92" in said  # the interval of line=B's median


def test_a_forecast_draws_its_two_bands_along_dates_and_the_correlograms_their_bounds(tmp_path, chart_service, monkeypatch):  # noqa: F811
    del chart_service[1][:]
    draw(tmp_path, chart_service, monkeypatch, "arima-auto")
    asked = {spec["option"]["title"]["text"]: spec["option"] for spec in chart_service[1] if "title" in spec["option"]}
    forecast = asked["The series, the fitted values and the forecast with its bands"]
    assert forecast["xAxis"]["type"] == "time" and forecast["legend"]["data"] == ["Observed", "Fitted", "Forecast", "80% band",
                                                                                  "95% band"]
    # the bands' stack does not pull the axis to zero: the axis is the marks' own, rounded
    _, _, rows = fixture("arima-auto")
    ys = [r["value"] for r in rows if r["section"] == "curve" and r["level"] == "forecast" and r["value"] is not None]
    assert (forecast["yAxis"]["min"], forecast["yAxis"]["max"]) == tuple(charts._nice_bounds(min(ys), max(ys)).values())
    acf = asked["ACF of the residuals"]
    marks = next(s for s in acf["series"] if "markLine" in s)["markLine"]["data"]
    assert acf["series"][0]["type"] == "bar" and marks[0]["yAxis"] == -marks[1]["yAxis"] > 0


def test_automatic_orders_say_each_step_in_the_page_language_with_its_numbers(tmp_path, chart_service, monkeypatch):  # noqa: F811
    _, page = draw(tmp_path, chart_service, monkeypatch, "arima-auto", locale="it")
    said = set(strings(json.loads(page)))
    # the runner writes keys (D_seas_yes, d_diff_0…) and the tests' numbers; the page says them in Italian
    assert {"D = 1: la forza stagionale 0,906 supera 0,64.",
            "La serie dopo la differenza stagionale: il KPSS rifiuta la stazionarietà (p ≤ 0,01): un'altra differenza.",
            "La sua differenza prima: il KPSS non rifiuta la stazionarietà (p ≥ 0,1): d = 1.",
            "SARIMA(1,1,0)(0,1,1)[12] ha il valore di AICc più basso tra i 33 candidati della ricerca passo passo."} <= said
    assert not {"D_seas_yes", "d_diff_0", "search_stepwise"} & said
    # the candidates with their criterion, the chosen one marked
    assert {"ARIMA(1,1,0)(0,1,1)[12]", "ARIMA(2,1,2)(1,1,1)[12]", "-582,66", "✓"} <= said
    # with automatic orders the advice does not send the reader to orders = auto
    assert not any("orders = auto" in s for s in said)
    assert any(s.startswith("L'ADF mantiene una radice unitaria nella serie differenziata") for s in said)


# ── the boosting group (docs/specs/statistical-models.md, «Group `boosting` — Python», «The report») ──
def asked_options(chart_service) -> dict:  # noqa: F811
    return {spec["option"]["title"]["text"]: spec["option"] for spec in chart_service[1] if "title" in spec["option"]}


@pytest.mark.parametrize("case", ["xgboost-regression", "xgboost-binary", "catboost-multiclass"])
def test_a_boosting_first_page_holds_the_four_cards_the_diagnostics_and_the_sentence_that_they_are_not_tests(
        tmp_path, chart_service, monkeypatch, case):  # noqa: F811
    del chart_service[1][:]
    out, page = draw(tmp_path, chart_service, monkeypatch, case, fmt="svg")
    first = json.loads(out.path.read_text())["pages"][0]
    said = set(strings(json.loads(page)))
    w = common.words(*case.split("-"), "en")
    for text in (w["sections"]["assumptions"], w["sections"]["summary"], "Size of the validation part", "Leakage",
                 "Better than the baseline", "✓ ok"):
        # the leakage row names its first predictor: «Leakage — price»
        assert text in first or text in said or any(s.startswith(text + " — ") for s in said), text
    assert any(s.startswith("These are diagnostics read against rules of thumb, not statistical tests: they have no p-value")
               for s in said)
    # no p-value, statistic, df or stars: a boosting model has no test
    for text in ("p-value", "Statistic", "Signif.", "Signif. codes:", "df"):
        assert text not in said, text
    # the cards of the task
    expected = {"xgboost-regression": ("RMSE, validation", "R², validation", "Trees"),
                "xgboost-binary": ("Rows / events", "AUC, validation", "Log-loss, validation"),
                "catboost-multiclass": ("Rows / classes", "Accuracy, validation", "Log-loss, validation")}[case]
    _, _, rows = fixture(case)
    best = int(next(r["value"] for r in rows if r["key"] == "model|||best_iteration")) + 1
    assert set(expected) <= said and f"best round {best}" in said


def test_the_boosting_charts_of_each_task(tmp_path, chart_service, monkeypatch):  # noqa: F811
    titles = {"xgboost-regression": ["Observed against predicted (validation)", "Residuals against predicted (validation)",
                                     "Histogram of the residuals (validation)"],
              "xgboost-binary": ["ROC curve (validation)", "Calibration by deciles (validation)",
                                 "Predicted probabilities by observed class", "Confusion matrix (validation)"],
              "catboost-multiclass": ["Confusion matrix (validation)", "ROC curves, one class against the rest"]}
    for case, task_charts in titles.items():
        del chart_service[1][:]
        draw(tmp_path / case, chart_service, monkeypatch, case)
        asked = asked_options(chart_service)
        shap = "SHAP summary: the effect of each predictor on each row"
        assert all(t in asked for t in task_charts), (case, sorted(asked))
        assert (shap in asked) == (case != "catboost-multiclass")  # a multiclass model has no beeswarm: bars by class
        assert any(t.startswith("Learning curve: ") for t in asked)
    # the gain and the SHAP bars; a multiclass model stacks the SHAP bars by class
    asked = asked_options(chart_service)
    gain = asked["Importance by PredictionValuesChange (share of the total)"]
    _, _, rows = fixture("catboost-multiclass")
    by_gain = [r["level"] for r in sorted((r for r in rows if r["section"] == "importance" and r["stat"] == "gain_share"),
                                          key=lambda r: -r["value"])]
    assert gain["yAxis"]["data"] == by_gain[:20]
    classes = sorted({r["level_2"] for r in rows if r["section"] == "importance" and r["level_2"]})
    stacked = asked["Mean |SHAP| by class"]
    assert sorted(s["name"] for s in stacked["series"]) == classes and all(s["stack"] == "total" for s in stacked["series"])
    assert sorted(asked["ROC curves, one class against the rest"]["legend"]["data"]) == classes


def test_the_beeswarm_has_the_ramp_the_names_of_the_predictors_by_rank_and_how_to_read_it(tmp_path, chart_service, monkeypatch):  # noqa: F811
    del chart_service[1][:]
    out, page = draw(tmp_path, chart_service, monkeypatch, "xgboost-binary", locale="it")
    swarm = asked_options(chart_service)["Riepilogo SHAP: l'effetto di ogni predittore su ogni riga"]
    marks = [s for s in swarm["series"] if s["type"] == "scatter" and s.get("name")]
    assert [s["name"] for s in marks] == ["low", "mid_low", "mid", "mid_high", "high", "category", "missing"]
    assert [s["itemStyle"]["color"] for s in marks] == ["#2c5aa0", "#7fa7d6", "#8c8c8c", "#e3866b", "#b2182b", "#6b7280", "#c4c8cf"]
    # a vertical line at 0; the names at the whole numbers, the first of the ranking at the top
    assert any(m.get("xAxis") == 0 for s in swarm["series"] for m in s.get("markLine", {}).get("data", []))
    _, _, rows = fixture("xgboost-binary")
    first = next(r["level"] for r in rows if r["section"] == "importance" and r["stat"] == "rank" and r["value"] == 1)
    names = swarm["yAxis"]["axisLabel"]["formatter"]["names"]
    assert names[str(max(map(int, names)))] == first and swarm["yAxis"]["min"] == 0.5
    assert any(s.startswith("Ogni punto è una riga del campione SHAP. A destra dello 0") for s in strings(json.loads(page)))


def test_the_learning_curve_of_the_stop_metric_is_drawn_only_when_it_is_not_the_loss():
    model, params, _ = fixture("xgboost-binary")
    plain = layouts.definition(model, params, "en")
    assert not [it for it in plain["sections"]["body"]["items"] if it["id"] == "learning_stop"]
    stopped = layouts.definition(model, {**params, "stop_metric": "auc"}, "en")
    [stop] = [it for it in stopped["sections"]["body"]["items"] if it["id"] == "learning_stop"]
    assert stop["filter"] == '[section] = "curve" AND [level] = "learning_stop"'
    # the line at the best round: the model's own number, plus one (rounds are counted from 0 in the rows)
    assert stop["options"]["target_x"] == [common.num(common.model_key("best_iteration")) + " + 1"]


def test_the_hyperparameters_are_the_nodes_with_the_defaults_filled_in(tmp_path, chart_service, monkeypatch):  # noqa: F811
    _, page = draw(tmp_path, chart_service, monkeypatch, "xgboost-regression")
    line = next(s for s in strings(json.loads(page)) if s.startswith("Hyperparameters: "))
    assert "learning_rate = 0.05" in line and "max_depth = 6" in line and "min_child_weight = 1" in line and "early_stopping = true" in line
    _, page = draw(tmp_path, chart_service, monkeypatch, "catboost-regression")
    line = next(s for s in strings(json.loads(page)) if s.startswith("Hyperparameters: "))
    assert "l2_leaf_reg = 3" in line and "rsm = 1" in line and "min_child_weight" not in line
    # a number with the page's decimal mark
    _, page = draw(tmp_path, chart_service, monkeypatch, "xgboost-regression", locale="it")
    line = next(s for s in strings(json.loads(page)) if s.startswith("Iperparametri: "))
    assert "learning_rate = 0,05" in line and "subsample = 0,8" in line and "0.05" not in line


def test_a_boosting_advice_and_note_are_said_in_the_page_language_whatever_the_runner_wording():
    model, params, rows = fixture("xgboost-binary")
    notes = [
        ("shap_sampled", "SHAP on 5,000 of 812,340 rows, drawn with seed 1", 1),
        ("time_budget", "Whatever the runner's sentence says", 2),
        ("model_file_too_large", "The model file would be larger than a model file can be (64 MiB): whatever follows", 3),
    ]
    extra = [dict(zip(OUTPUT_NAMES, ["model", None, None, f"note_{stat}", None, text, None, None, len(rows) + i, f"model|||note_{stat}"]))
             for stat, text, i in notes]
    rows = [*rows, *extra]
    size = next(r for r in rows if r["stat"] == "n")
    size["value"] = 812340.0
    next(r for r in rows if r["stat"] == "shap_rows")["value"] = 5000.0
    said = {r["stat"]: r["text"] for r in layouts.with_labels(model, params, rows, "it") if r["section"] == "model" and r["stat"].startswith("note_")}
    assert said["note_shap_sampled"] == "I valori SHAP, e l'importanza secondo SHAP, sono calcolati su 5.000 righe di 812.340, estratte con seed 1."
    assert said["note_time_budget"].startswith("Il boosting si è fermato al 60 % del budget di tempo")
    assert said["note_model_file_too_large"].startswith("Il file del modello sarebbe più grande")
    assert said["note_validation_chose_trees"].startswith("La parte di validazione ha scelto anche il numero di alberi")
    # the advice: one of several the check can give, chosen by the id the runner wrote next to its sentence (`advice_id`),
    # whatever the sentence says; its words in the language; an id the words do not have yet: the check's general advice
    def advised(check: str, advice_id: str | None, locale: str, verdict: str = "doubtful") -> str:
        base = next(r for r in rows if r["level"] == check and r["stat"] == "verdict")
        changed = [{**r, "text": verdict} if r is base else r for r in rows
                   if not (r["level"] == check and r["stat"] in ("advice", "advice_id"))]
        changed.append({**base, "stat": "advice", "text": "Whatever the runner says: still falling, few trees, balanced",
                        "key": f"assumption|{check}|{base['level_2'] or ''}|advice"})
        if advice_id:
            changed.append({**base, "stat": "advice_id", "text": advice_id, "key": f"assumption|{check}|{base['level_2'] or ''}|advice_id"})
        [one] = [r for r in layouts.with_labels(model, params, changed, locale) if r["level"] == check and r["stat"] == "advice"]
        return one["text"]

    for advice_id, start in (("trees_more", "La perdita in validazione scendeva ancora"), ("trees_few", "Si è fermato dopo pochissimi alberi"),
                             ("stop_on_loss", "L'arresto anticipato seguiva una metrica che non è la perdita"),
                             ("trees_time", "Il budget di tempo ha fermato il boosting"),
                             ("trees_size", "Il boosting si è fermato perché il file del modello restasse entro 64 MiB"),
                             ("trees_no_stop", "L'ultimo albero è peggiore"), ("newer_than_the_words", "Il numero di alberi potrebbe"),
                             (None, "Il numero di alberi potrebbe")):
        assert advised("trees_limit", advice_id, "it").startswith(start), advice_id
    # an advice the words have one of for the check, and the calibration's two
    assert advised("calibration", "calibration", "de").startswith("Die vorhergesagten Wahrscheinlichkeiten stimmen nicht")
    assert advised("calibration", "calibration_balanced", "de").startswith("Erwartet: ausgewogene Gewichte")
    assert advised("beats_baseline", "beats_baseline", "fr").startswith("Le modèle ne prédit pas")
    # a check not computed for one of several reasons: the words of that reason
    assert advised("importance_stability", "importance_few_predictors", "it", "not_computed") == "Non calcolato: servono almeno 3 predittori."
    assert advised("overfitting", "overfitting_kept_best", "it", "not_computed").startswith(
        "Non calcolato: l'arresto anticipato sulla perdita tiene l'albero con la perdita in validazione più bassa")
    assert advised("calibration_slope", "slope_separated", "es", "not_computed").startswith("No calculado: la regresión logística")


def test_the_calibration_is_drawn_on_a_square_of_0_to_1_and_the_beeswarm_says_the_scale_of_its_task(
        tmp_path, chart_service, monkeypatch):  # noqa: F811
    del chart_service[1][:]
    draw(tmp_path, chart_service, monkeypatch, "xgboost-binary")
    cal = asked_options(chart_service)["Calibration by deciles (validation)"]
    assert (cal["xAxis"]["min"], cal["xAxis"]["max"], cal["yAxis"]["min"], cal["yAxis"]["max"]) == (0, 1, 0, 1)
    assert cal["grid"]["width"] == cal["grid"]["height"]  # a square plot: the diagonal at 45°
    [diagonal] = [m for s in cal["series"] for m in s.get("markLine", {}).get("data", []) if isinstance(m, list)]
    assert diagonal == [{"coord": [0.0, 0.0]}, {"coord": [1.0, 1.0]}]
    for case, scale, other in (("xgboost-regression", "in the outcome's units", "log-odds"), ("xgboost-binary", "in log-odds", "units")):
        model, params, _ = fixture(case)
        [said] = [s for s in strings(layouts.definition(model, params, "en")) if s.startswith("Each point is a row of the SHAP sample")]
        assert scale in said and other not in said, case


def test_what_a_boosting_check_reads_follows_the_nodes_settings():
    """A check's H0 may change with the node's settings (the runner reads them too): with
    class_weight = balanced the losses of beats_baseline are weighted as the fit, and the learning
    curve says that it is not the table's log-loss."""
    model, params, rows = fixture("xgboost-binary")

    def h0(p: dict, check: str, locale: str) -> str:
        [row] = [r for r in layouts.with_labels(model, p, rows, locale) if r["level"] == check and r["stat"] == "h0"]
        return row["text"]

    assert h0(params, "beats_baseline", "en") == "1 − validation loss / the baseline's"
    assert h0({**params, "class_weight": "balanced"}, "beats_baseline", "it").startswith(
        "1 − perdita in validazione / quella del riferimento, entrambe pesate come l'adattamento")
    # early stopping on the loss keeps the best tree: overfitting then reads the validation loss over the training loss
    assert h0(params, "overfitting", "en").startswith("the validation loss over the training loss at the tree kept")
    for other in ({"stop_metric": "auc"}, {"early_stopping": False}):
        assert h0({**params, **other}, "overfitting", "en").startswith("the share of the validation gain"), other
    # without early stopping trees_limit reads the last tree against the best, not whether it stopped
    assert h0(params, "trees_limit", "en") == "whether early stopping stopped, and the trees kept"
    assert h0({**params, "early_stopping": False}, "trees_limit", "it").startswith(
        "quanto la perdita in validazione all'ultimo albero è peggiore che al migliore")
    assert common.by_params({"a=1&b=true": "both", "b=false": "not b", "": "else"}, {"a": 1, "b": True}) == "both"
    assert common.by_params({"a=1&b=true": "both", "b=false": "not b", "": "else"}, {"a": 2, "b": False}) == "not b"
    assert common.by_params({"a=1": "a", "": "else"}, {}) == "else" and common.by_params("plain", {}) == "plain"

    def caption(p: dict) -> list[str]:
        return [s for s in strings(layouts.definition(model, p, "en")) if s.startswith("With class_weight = balanced the curves")]

    assert not caption(params) and len(caption({**params, "class_weight": "balanced"})) == 1
    # the binary error rate cuts at 0.5, whatever the node's threshold: its curve says so
    cut = "The error rate (XGBoost's error, CatBoost's ZeroOneLoss) counts a row as wrong"
    assert any(s.startswith(cut) for s in strings(layouts.definition(model, {**params, "stop_metric": "error"}, "en")))
    assert not any(s.startswith(cut) for s in strings(layouts.definition(model, {**params, "stop_metric": "auc"}, "en")))


def test_excel_holds_the_tables_one_sheet_each(tmp_path, chart_service, monkeypatch):  # noqa: F811
    out, _ = draw(tmp_path, chart_service, monkeypatch, "logistic-unpenalized", fmt="xlsx")
    with zipfile.ZipFile(out.path) as z:
        book = z.read("xl/workbook.xml").decode()
    sheets = re.findall(r'<sheet name="([^"]+)"', book)
    assert sheets[:2] == ["Checks of the assumptions", "What to do"]
    assert {"Estimates and tests", "Numbers of the fit", "Confusion matrix"} <= set(sheets)


# ── the route and the task ───────────────────────────────────────────────────
@pytest.fixture
def stack(monkeypatch, chart_service):  # noqa: F811
    """The engine's side of a request: the export routes, the task run in place, the results
    of runs in a fake storage."""
    files, results = Storage(), {}
    monkeypatch.setattr(jobs, "get_storage_service", lambda: files)
    monkeypatch.setattr(exports, "get_storage_service", lambda: files)
    monkeypatch.setattr(exports, "_client", FakeRedis())
    monkeypatch.setattr(render.Limits, "from_settings", classmethod(lambda cls: cls(chart_url=chart_service[0], max_rows=10)))

    def send_task(name, kwargs, task_id):
        assert name == "app.tasks.report_jobs.render_model_report_task"
        results[task_id] = Result(report_jobs.render_model_report_task.apply(kwargs=kwargs, task_id=task_id).get())
        return results[task_id]

    monkeypatch.setattr(report_routes.celery_app, "send_task", send_task)
    monkeypatch.setattr(tasks, "AsyncResult", lambda task_id, app=None: results.get(task_id) or Result(None, state="PENDING"))
    monkeypatch.setattr(exports, "AsyncResult", lambda task_id, app=None: results.get(task_id) or Result(None, state="PENDING"))
    for case in CASES:
        _, _, table = read_fixture(FIXTURES / f"{case}.json")
        buf = io.BytesIO()
        pq.write_table(table, buf)
        files.blobs[("data-prep", f"runs/7/{case}/results.parquet")] = buf.getvalue()
    return files


def ask(case="linear-unpenalized", **body):
    model, params, _ = fixture(case)
    return report_routes.model_report(report_routes.ModelReport(**{
        "bucket": "data-prep", "results_key": f"runs/7/{case}/results.parquet", "model": model, "params": params,
        "owner": "u1", **body}))


def test_a_model_s_report_is_drawn_from_its_results_and_taken_as_an_export(stack):
    started = ask(format="svg", locale="en", step="Prices", sample=[{"first": 1000}])
    assert started.filename == "Linear regression — quantity ~ price + channel.json" and started.status == "submitted"
    status = tasks.export_status(started.task_id, owner="u1")
    assert status["status"] == "ready" and status["pages"] >= 2
    with pytest.raises(HTTPException) as e:
        tasks.export_file(started.task_id, owner="u2")
    assert e.value.status_code == 404
    headers, body = _play(tasks.export_file(started.task_id, owner="u1"))
    assert headers["content-type"] == "application/json" and json.loads(body)["pages"][0].startswith("<svg")
    for fmt, kind in (("pdf", "application/pdf"), ("xlsx", "spreadsheetml")):
        done = ask(format=fmt, locale="it", filename="Prezzi")
        assert done.filename == f"Prezzi.{fmt}" and tasks.export_status(done.task_id, owner="u1")["status"] == "ready"
        headers, _ = _play(tasks.export_file(done.task_id, owner="u1"))
        assert kind in headers["content-type"]
    assert stack.keys("exports/") == []  # each file taken once


def test_what_has_no_report_is_refused_before_the_queue_and_results_of_another_shape_by_the_task(stack):
    for body, said in (({"model": "poisson"}, "has no report"), ({"params": {"outcome": "q"}}, "predictors")):
        with pytest.raises(HTTPException) as e:
            ask(**body)
        assert e.value.status_code == 422 and said in e.value.detail
    # the results of a penalized fit read as an unpenalized one: refused, a runner's bug, with its sentence
    wrong = ask(results_key="runs/7/linear-penalized/results.parquet")
    status = tasks.export_status(wrong.task_id, owner="u1")
    assert status["status"] == "failed" and status["code"] == 422 and status["detail"].startswith("results.parquet: ")
    gone = ask(results_key="runs/7/nothing/results.parquet")
    status = tasks.export_status(gone.task_id, owner="u1")
    assert status["status"] == "failed" and status["code"] == 404 and "run the node again" in status["detail"]


# ── the customer lifetime report: the holdout's classes, the edges as a measure ─────
HOLDOUT_CASES = ("clv-value", "clv-purchases", "bg_bb-value")


def _classes(rows, level, stat, level_2):
    return [r for r in rows if r["section"] == "hist" and r["level"] == level and r["stat"] == stat and r["level_2"] == level_2]


@pytest.mark.parametrize("case", HOLDOUT_CASES)
def test_the_holdout_classes_are_the_frequency_s_with_the_last_one_open_and_the_customers_apart(case):
    """The holdout chart's classes carry the bounds of the frequency chart (the upper end, none for the open last
    class, which the page prints «10+»), and the customers of each class are the rows of the stat `count`."""
    _, _, rows = fixture(case)
    frequency = _classes(rows, "frequency", "count", "observed")
    for which in ("observed", "expected"):
        holdout = _classes(rows, "holdout", "mean", which)
        assert [(r["x0"], r["x1"]) for r in holdout] == [(r["x0"], r["x1"]) for r in frequency]
    holdout = _classes(rows, "holdout", "mean", "observed")
    assert len(holdout) > 2 and holdout[-1]["x1"] is None and all(r["x1"] >= r["x0"] for r in holdout[:-1])
    # adjacent classes: a pooled class runs from x0 to x1, the next starts at x1 + 1
    assert all(b["x0"] == a["x1"] + 1 for a, b in zip(holdout[:-1], holdout[1:]))
    customers = _classes(rows, "holdout", "count", "customers")
    assert [(r["x0"], r["x1"]) for r in customers] == [(r["x0"], r["x1"]) for r in frequency]
    n = next(r["value"] for r in rows if r["section"] == "model" and r["stat"] == "n")
    assert sum(r["value"] for r in customers) == n


@pytest.mark.parametrize("case", HOLDOUT_CASES)
def test_the_holdout_chart_names_its_last_class_with_a_plus_and_its_means_only(tmp_path, chart_service, monkeypatch, case):  # noqa: F811
    del chart_service[1][:]
    out, page = draw(tmp_path, chart_service, monkeypatch, case, fmt="svg")
    model, params, rows = fixture(case)
    title = common.words(model, case.split("-")[1], "en")["charts"]["holdout"]
    [spec] = [s for s in chart_service[1] if s["option"].get("title", {}).get("text") == title]
    categories = spec["option"]["xAxis"]["data"]
    classes = _classes(rows, "holdout", "mean", "observed")
    one = [str(int(r["x0"])) if r["x0"] == r["x1"] else f"{int(r['x0'])}–{int(r['x1'])}" for r in classes[:-1]]
    assert categories == one + [f"{int(classes[-1]['x0'])}+"]
    # the means only: the customers' rows are not a series
    assert all(len(s["data"]) == len(classes) for s in spec["option"]["series"])


def _boundary(level_2: str, value: float, reading: str) -> list[dict]:
    row = {"section": "assumption", "level": "boundary", "level_2": level_2, "x0": None, "x1": None, "index": None}
    return [{**row, "stat": "value", "value": value, "text": None, "key": f"assumption|boundary|{level_2}|value"},
            {**row, "stat": "advice_id", "value": None, "text": reading, "key": f"assumption|boundary|{level_2}|advice_id"}]


@pytest.mark.parametrize("locale", LOCALES)
def test_an_estimate_at_an_edge_is_a_measure_with_the_sentence_of_its_reading_and_no_verdict(tmp_path, chart_service, monkeypatch, locale):  # noqa: F811
    """«boundary» is no verdict (no fixed threshold decides one): the table of measures says, in the page's language, what such
    an edge means for the model, by the reading the runner wrote (`advice_id`), and writes the value by its exponent; the
    convergence check says when the flat likelihood left no standard errors."""
    model, params, rows = fixture("clv-value")
    assert not [r for r in rows if r["level"] == "boundary"]  # a clean fit writes no row for it
    rows = [*rows, *_boundary("b", 20000.0, "boundary_no_dropout"), *_boundary("r", 3e5, "boundary_homogeneous"),
            *_boundary("spend_q", 5e-5, "boundary")]
    out, page = draw(tmp_path, chart_service, monkeypatch, "clv-value", locale=locale, rows=rows)
    assert out.warnings == []
    w = common.words(model, "value", locale)
    [table] = [i for i in _items(layouts.definition(model, params, locale, None, None)) if i.get("id") == "measures"]
    said = json.dumps(table, ensure_ascii=False)
    assert set(w["measure_texts"]["boundary"]) == {"", "boundary_no_dropout", "boundary_homogeneous"}
    for reading, sentence in w["measure_texts"]["boundary"].items():
        assert sentence in said, (locale, reading)
    assert w["checks"]["boundary"]["name"] in said and "0.###E+0" in said
    # no verdict row, so the check is not in the table of the verdicts
    assert not any(r["stat"] in ("verdict", "advice", "h0") and r["level"] == "boundary" for r in layouts.with_labels(model, params, rows, locale))


def _items(node):
    """Every block of a page definition, whatever its depth."""
    if isinstance(node, dict):
        if "id" in node and "type" in node:
            yield node
        for v in node.values():
            yield from _items(v)
    elif isinstance(node, list):
        for v in node:
            yield from _items(v)


@pytest.mark.parametrize("locale", LOCALES)
def test_the_spend_check_not_computed_says_why_in_the_page_s_language(locale):
    """When the repeat customers' frequencies or spends do not vary the HC3 slope has no test: the runner writes the
    verdict `not_computed`, and the page says why in its language, not the runner's English."""
    model, params, rows = fixture("clv-value")
    rows = [dict(r) for r in rows if not (r["level"] == "spend_independence" and r["stat"] in ("statistic", "p_value", "significance"))]
    for r in rows:
        if r["level"] == "spend_independence" and r["stat"] == "verdict":
            r["text"] = "not_computed"
    # an ok verdict has no advice: the runner writes one with not_computed
    rows.append({"section": "assumption", "level": "spend_independence", "level_2": None, "stat": "advice", "value": None,
                 "text": "not computed: the repeat customers' frequencies or spends do not vary", "x0": None, "x1": None,
                 "key": "assumption|spend_independence||advice"})
    w = common.words(model, "value", locale)
    why = w["checks"]["spend_independence"]["not_computed"]

    def said(rows: list[dict]) -> list[str]:
        return [r["text"] for r in layouts.with_labels(model, params, rows, locale)
                if r["level"] == "spend_independence" and r["stat"] == "advice"]

    assert said(rows) == [why[""]] and why[""] != w["checks"]["spend_independence"]["advice"]
    # one repeat customer alone has its frequency: the runner's advice_id says which reason, the page its sentence
    rows = [r for r in rows if not (r["level"] == "spend_independence" and r["stat"] == "advice_id")]
    rows.append({"section": "assumption", "level": "spend_independence", "level_2": None, "stat": "advice_id", "value": None,
                 "text": "spend_independence_leverage", "x0": None, "x1": None, "key": "assumption|spend_independence||advice_id"})
    assert said(rows) == [why["spend_independence_leverage"]] and why["spend_independence_leverage"] != why[""]


def test_the_gateways_english_bgbb_formula_for_mlflow_is_the_english_text_of_the_report():
    """The gateway (jobs_models_mlflow.go, formulaEnglish) sends BG/BB's formula to MLflow as this sentence, the
    English `formula_texts.bgbb_formula` with its unit in the singular («{unit:one}»)."""
    path = Path(__file__).resolve().parents[1] / "app" / "engine" / "models" / "report_texts" / "customer_lifetime.json"
    said = json.loads(path.read_text())["en"]["formula_texts"]["bgbb_formula"]
    assert said.replace("{unit:one}", "year") == "x: transactions, t_x: opportunity of the last, n: opportunities, one opportunity a year"


def test_bg_bb_s_formula_is_an_id_the_page_words_with_its_unit(monkeypatch):
    """BG/BB's runner sends its formula as «@bgbb_formula unit=year»: the page reads the sentence of that id in
    `formula_texts` of its language, the unit in its words; the continuous models' formula keeps its «in weeks»."""
    model, params, rows = fixture("bg_bb-value")
    assert [r["text"] for r in rows if r["section"] == "model" and r["stat"] == "formula"] == ["@bgbb_formula unit=year"]
    real = common.words

    def words(m: str, variant: str, locale: str) -> dict:
        return {**real(m, variant, locale), "formula_texts": {"bgbb_formula": "x, t_x, n · {unit}"}}

    monkeypatch.setattr(common, "words", words)
    for locale in LOCALES:
        unit = real(model, "value", locale)["values"]["time_unit"]["year"]
        said = [r["text"] for r in layouts.with_labels(model, params, rows, locale) if r["section"] == "model" and r["stat"] == "formula"]
        assert said == [f"x, t_x, n · {unit}"], (locale, said)


# ── parametric survival: the final review's corrections (lot P12) ───────────
@pytest.mark.parametrize("locale", LOCALES)
def test_the_parametric_survival_measures_are_the_declared_ones_and_kaplan_meier_s_gap_is_read_in_survival(
        tmp_path, chart_service, monkeypatch, locale):  # noqa: F811
    model, params, rows = fixture("parametric_survival-ph_aft")
    assert any(r["section"] == "assumption" and r["level"] == "vif" for r in rows)  # the runner writes VIF among its checks too
    _, page = draw(tmp_path, chart_service, monkeypatch, "parametric_survival-ph_aft", fmt="svg", locale=locale)
    said = set(strings(json.loads(page)))
    w = common.words(model, "ph_aft", locale)
    # VIF is in the coefficients' tables: the measures do not repeat it, under its raw name or another
    assert "vif" not in said and w["checks"]["vif"]["name"] not in said
    for check in ps_layout.MEASURES:
        assert w["checks"][check]["name"] in said, (locale, check)
    # the gap is in survival, from 0 to 1; the row adds where it is read and the gap in Kaplan–Meier's standard errors
    km = [s for s in said if s.startswith(w["measure_texts"]["km_agreement"])]
    assert km and all(" · " in s and re.search(r"\d", s.split(" · ")[1]) for s in km), km


def test_the_customer_lifetime_measures_are_still_all_shown(tmp_path, chart_service, monkeypatch):  # noqa: F811
    # the table of measures lists the ids the page declares: the customer lifetime page declares every measure its runner writes
    model, params, rows = fixture("clv-value")
    written = {r["level"] for r in rows if r["section"] == "assumption" and r["stat"] == "value"} - {
        r["level"] for r in rows if r["section"] == "assumption" and r["stat"] == "verdict"}
    _, page = draw(tmp_path, chart_service, monkeypatch, "clv-value", fmt="svg")
    said = set(strings(json.loads(page)))
    w = common.words(model, "value", "en")
    assert written and all(w["checks"][check]["name"] in said for check in written), written


@pytest.mark.parametrize("locale", LOCALES)
def test_the_candidates_say_a_tiny_akaike_weight_as_below_a_thousandth_and_every_status_in_words(
        tmp_path, chart_service, monkeypatch, locale):  # noqa: F811
    model, params, rows = fixture("parametric_survival-auto")
    tiny = [r["value"] for r in rows if r["section"] == "candidate" and r["stat"] == "akaike_weight" and 0 < r["value"] < 0.001]
    assert tiny  # the runner's output: the far candidates have weights of 1e-7 and less
    # a candidate with too few rows, as the runner writes it
    rows = [{**r, "text": "too_few_rows"} if r["key"] == "candidate|gengamma||status" else r for r in rows]
    _, page = draw(tmp_path, chart_service, monkeypatch, "parametric_survival-auto", fmt="svg", locale=locale, rows=rows)
    said = set(strings(json.loads(page)))
    w = common.words(model, "auto", locale)
    decimal = formats.locale(locale).decimal
    assert f"< 0{decimal}001" in said and not any(stats_layouts.tiny(v, locale) in said for v in tiny)
    assert tuple(w["candidates"]["status"]) == ps_layout.CANDIDATE_STATUS and w["candidates"]["status"]["too_few_rows"] in said
    assert not re.search("hessian|hesse", w["candidates"]["legend"] + w["note_texts"]["auto_not_converged"], re.I)


@pytest.mark.parametrize("locale", LOCALES)
def test_the_lr_checks_say_why_they_were_not_computed_and_the_measures_keep_no_h0(locale):
    model, params, rows = fixture("parametric_survival-ph_aft")
    w = common.words(model, "ph_aft", locale)
    # the node's own words: a measure has no verdict, so an H0 or an advice of its would never be shown (and they spoke of
    # thresholds); the common words of epv, dfbeta and vif stay, for the models where they are verdicts
    own = json.loads((Path(layouts.__file__).parent.parent / "report_texts" / "parametric_survival.json").read_text(encoding="utf-8"))[locale]
    for check in (*ps_layout.MEASURES, "vif"):
        assert not {"h0", "advice"} & set(own["checks"][check]), check
    base = {"section": "assumption", "level": "distribution_vs_gengamma", "level_2": None, "value": None, "x0": None, "x1": None}
    rows = [r for r in rows if not (r["section"] == "assumption" and r["level"] == "distribution_vs_gengamma")] + [
        {**base, "stat": "verdict", "text": "not_computed"},
        {**base, "stat": "advice", "text": "The generalized gamma took longer to fit than the model itself (12.3 s): …"},
        {**base, "stat": "advice_id", "text": "distribution_vs_gengamma_time"},
        {"section": "model", "level": None, "level_2": None, "stat": "note_lr_time", "value": None, "text": "@lr_time", "x0": None, "x1": None},
        {"section": "model", "level": None, "level_2": None, "stat": "note_exponential_lr_time", "value": None, "text": "@exponential_lr_time",
         "x0": None, "x1": None}]
    said = {(r["section"], r["level"], r["stat"]): r["text"] for r in layouts.with_labels(model, params, rows, locale)}
    check = w["checks"]["distribution_vs_gengamma"]
    assert said[("assumption", "distribution_vs_gengamma", "advice")] == check["not_computed"]["distribution_vs_gengamma_time"]
    assert said[("model", None, "note_lr_time")] == w["note_texts"]["lr_time"]
    assert said[("model", None, "note_exponential_lr_time")] == w["note_texts"]["exponential_lr_time"]
    # every reason the runner gives has its words, and auto's advice too
    for test, ids in (("distribution_vs_gengamma", ("no_time", "time", "candidate", "not_converged", "below")),
                      ("constant_hazard", ("no_time", "time", "candidate", "not_converged", "below"))):
        assert all(f"{test}_{i}" in w["checks"][test]["not_computed"] for i in ids) and f"{test}_auto" in w["checks"][test]["advice"]


# ── the report's lot D of the first bug hunt: notes as ids, units, numbers by their exponent, the pages ──────
def _model_row(stat: str, text: str | None = None, value: float | None = None) -> dict:
    return {"section": "model", "level": None, "level_2": None, "stat": stat, "value": value, "text": text, "x0": None,
            "x1": None, "key": f"model|||{stat}"}


# the ids the runners write as notes (kaplan_meier.R and cox.R: surv_note; arima.R: arima_note; linear.R: linear_note)
NOTE_IDS = {
    "kaplan_meier": {"stratum_no_events": "stratum=g=a", "median_not_reached": "stratum=g=a",
                     "survival_not_estimated": "stratum=g=b%20c times=11,20.5 last=10", "curves_cross": "pairs=a%20×%20b,a%20×%20c"},
    "cox": {"few_events": "epv=2.5 events=10 coefficients=4 least=10", "robust_se": "", "robust_se_cluster": "cluster=patient%2Cid",
            "robust_start_rows": "", "interaction_vif": "", "not_converged": "", "monotone_likelihood": "",
            "baseline_thinned": "times=1000"},
    "arima": {"ml_only": "", "holdout_fit": "n=12", "se_missing": "", "gap": "n=1", "gaps": "n=3", "future_dropped_one": "n=1",
              "future_dropped": "n=4", "no_forecast": "", "drift_dropped": "", "not_converged": "code=1"},
    "linear": {"interaction_vif": "", "weights_unused": "weights=peso%2050%25", "wls": "", "zero_weight": "n=1",
               "zero_weights": "n=3", "hac_order": "", "exact_fit": ""},
}
CASE_OF = {"kaplan_meier": "kaplan_meier-default", "cox": "cox-default", "arima": "arima-manual", "linear": "linear-unpenalized"}


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("model", sorted(NOTE_IDS))
def test_every_note_a_runner_sends_as_an_id_is_a_sentence_of_the_page_s_language(model, locale):
    """Kaplan–Meier's, Cox's, ARIMA's and the linear model's notes are «@id name=value …»: each id has its sentence in
    the five languages, every value in its place, decoded (%20, %25, %2C) and with the language's decimal."""
    _, params, rows = fixture(CASE_OF[model])
    notes = [_model_row(f"note_{i}", f"@{note} {values}".strip()) for i, (note, values) in enumerate(NOTE_IDS[model].items(), 1)]
    said = [r["text"] for r in layouts.with_labels(model, params, rows + notes, locale) if r["stat"] in {n["stat"] for n in notes}]
    w = common.words(model, catalog.variant(model, catalog.check_params(model, params)), locale)
    assert set(w["note_texts"]) == set(NOTE_IDS[model])
    decimal = formats.locale(locale).decimal
    for text in said:
        assert "@" not in text and "{" not in text and "%2" not in text, text
    joined = " ".join(said)
    if model == "kaplan_meier":
        assert "g=b c" in joined and f"11, 20{decimal}5" in joined and "a × b, a × c" in joined
    if model == "cox":
        assert f"2{decimal}5" in joined and "«patient,id»" in joined
    if model == "linear":
        assert "«peso 50%»" in joined


@pytest.mark.parametrize("locale", LOCALES)
def test_kaplan_meier_s_and_cox_s_checks_not_computed_say_why_in_the_page_s_language(locale):
    """The runners' reason for a check not computed is English (and R's error): the page says its own sentence."""
    for model, check, level_2 in (("kaplan_meier", "proportional_hazards", None), ("cox", "schoenfeld", "load"),
                                  ("cox", "schoenfeld_global", None), ("cox", "martingale_form", "load"), ("cox", "dfbeta", "load"),
                                  ("cox", "vif", "load")):
        _, params, rows = fixture(CASE_OF[model])
        base = {"section": "assumption", "level": check, "level_2": level_2, "value": None, "x0": None, "x1": None}
        rows = [r for r in rows if not (r["section"] == "assumption" and r["level"] == check and r["level_2"] == level_2)] + [
            {**base, "stat": "verdict", "text": "not_computed"},
            {**base, "stat": "advice", "text": "It could not be computed on these rows: Error in fitter(X, Y): NA/NaN/Inf"}]
        w = common.words(model, "default", locale)
        [said] = [r["text"] for r in layouts.with_labels(model, params, rows, locale)
                  if r["level"] == check and r["level_2"] == level_2 and r["stat"] == "advice"]
        assert said == w["checks"][check]["not_computed"], (model, check)


@pytest.mark.parametrize("locale", LOCALES)
def test_a_customer_lifetime_formula_and_its_units_are_the_page_s_words(locale):
    """CLV's formula («x: frequency, t_x: recency, T: age, in weeks») and BG/BB's («@bgbb_formula unit=year») are
    sentences of the page's language; a unit after one period is singular («over 1 year»), German's after a
    preposition dative («in 5 Jahren», «Holdout von 5 Jahren»)."""
    model, params, rows = fixture("clv-value")
    w = common.words(model, "value", locale)
    [formula] = [r["text"] for r in layouts.with_labels(model, params, rows, locale) if r["stat"] == "formula"]
    assert formula == w["formula_texts"]["clv_formula"].replace("{unit}", w["values"]["time_unit"]["week"]).replace(
        "{unit:dative}", w["values"]["time_unit_dative"]["week"])
    model, params, rows = fixture("bg_bb-value")
    w = common.words(model, "value", locale)
    [formula] = [r["text"] for r in layouts.with_labels(model, params, rows, locale) if r["stat"] == "formula"]
    assert formula == w["formula_texts"]["bgbb_formula"].replace("{unit:one}", w["values"]["time_unit_one"]["year"])
    if locale != "en":
        assert not re.search(r"\b(frequency|opportunit\w*|the|last)\b", formula), formula
    doc = " ".join(strings(layouts.definition(model, params, locale, None, None)))  # the expressions
    for unit in ("day", "week", "month", "quarter", "year"):
        assert f'"{w["values"]["time_unit_one"][unit]}"' in doc, unit
    if locale == "de":
        assert '"Jahren"' in doc and '"Monaten"' in doc


def test_a_customer_lifetime_page_says_one_period_in_the_singular(tmp_path, chart_service, monkeypatch):  # noqa: F811
    model, params, rows = fixture("bg_bb-value")
    rows = [{**r, "value": 1.0} if r["key"] in ("model|||horizon", "model|||holdout_length") else r for r in rows]
    for locale, one, many in (("en", "over 1 year", "1 years"), ("it", "in 1 anno", "1 anni"), ("de", "von 1 Jahr", "1 Jahre")):
        _, page = draw(tmp_path / locale, chart_service, monkeypatch, "bg_bb-value", fmt="svg", locale=locale, rows=rows)
        said = " ".join(strings(json.loads(page)))
        assert one in said and many not in said, locale
    _, page = draw(tmp_path / "de5", chart_service, monkeypatch, "bg_bb-value", fmt="svg", locale="de")
    assert "Holdout von 5 Jahren" in " ".join(strings(json.loads(page)))


def test_numbers_too_large_or_too_small_for_a_column_are_written_by_their_exponent_and_a_fit_s_p_value_as_everywhere(
        tmp_path, chart_service, monkeypatch):  # noqa: F811
    """A CLV's spend parameter with an interval up to 6.2e+150 (a real fit of the runner) is «6.204e+150», not 151
    digits; γ = 8.5e-10 is «8.54e-10»; a p-value of the fit is «< 0.0001», not its ten digits."""
    model, params, rows = fixture("clv-value")
    big = {("coef", "p", "spend", "ci_high"): 6.2039e150, ("coef", "p", "spend", "std_error"): 243446034.96,
           ("coef", "p", "spend", "estimate"): 8.540411785e-10}
    rows = [{**r, "value": big.get((r["section"], r["level"], r["level_2"], r["stat"]), r["value"])} for r in rows]
    assert sum(r["value"] in big.values() for r in rows) == 3
    rows.append(_model_row("chi_square_p_value", value=1.027462077e-23))
    out, page = draw(tmp_path, chart_service, monkeypatch, "clv-value", fmt="svg", rows=rows)
    said = set(strings(json.loads(page)))
    assert {"6.204e+150", "2.434e+8", "8.54e-10"} <= said
    assert not any(re.search(r"\d{16}", s) for s in said)
    assert "1.027462077e-23" not in said and "< 0.0001" in said
    assert out.warnings == []
    _, page = draw(tmp_path / "it", chart_service, monkeypatch, "clv-value", fmt="svg", rows=rows, locale="it")
    assert "6,204e+150" in set(strings(json.loads(page)))


def test_a_parametric_survival_without_predictors_has_no_ratio_charts_and_no_influence():
    params = {"time": "t", "event": "e", "event_value": 1, "distribution": "gompertz"}
    ids = {i["id"] for i in layouts.definition("parametric_survival", params, "en", None, None)["sections"]["body"]["items"]}
    assert not ids & {"time_ratio_interval", "hazard_ratio_interval", "deviance_linear", "s_influence"}
    ids = {i["id"] for i in layouts.definition("parametric_survival", {**params, "predictors": ["x"]}, "en", None, None)
           ["sections"]["body"]["items"]}
    assert {"hazard_ratio_interval", "deviance_linear", "s_influence"} <= ids


@pytest.mark.parametrize("locale", LOCALES)
def test_auto_counts_the_candidates_that_converged_and_a_median_never_reached_has_no_bare_interval(
        tmp_path, chart_service, monkeypatch, locale):  # noqa: F811
    model, params, rows = fixture("parametric_survival-auto")
    # a candidate that did not converge, as the runner writes it: `converged` 0 and its status
    rows = [{**r, "value": 0.0} if r["key"] == "candidate|gengamma||converged" else r for r in rows]
    rows = [{**r, "text": "not_converged"} if r["key"] == "candidate|gengamma||status" else r for r in rows]
    rows = [r for r in rows if not r["key"].startswith("stratum|all||median")]
    _, page = draw(tmp_path, chart_service, monkeypatch, "parametric_survival-auto", fmt="svg", locale=locale, rows=rows)
    texts = strings(json.loads(page))
    said = " ".join(texts)
    w = common.words(model, "auto", locale)
    head, tail = w["candidates"]["lowest"].split("{n}")
    assert re.search(re.escape(head.split("}")[-1]) + "5" + re.escape(tail), said), said[:400]  # the five that converged
    assert w["kpi"]["never"] in texts
    assert not any(t.strip() == w["kpi"]["ci"].replace("{ci}", "").strip() for t in texts)


def test_a_parametric_survival_with_strata_names_them_without_the_column_and_groups_its_tables():
    params = {"time": "t", "event": "e", "event_value": 1, "predictors": ["x"], "strata": "plant", "quantiles": [0.25]}
    items = {i["id"]: i for i in layouts.definition("parametric_survival", params, "en", None, None)["sections"]["body"]["items"]}
    assert items["quantiles"]["groups"][0]["by"].count('"plant="') == 1 and items["surv_at"]["groups"]
    assert items["profile_survival"]["w"] == items["hazard"]["w"] == common.WIDTH  # a curve a stratum: across the page
    assert '"plant="' in items["profile_survival"]["split_by"]
    items = {i["id"]: i for i in layouts.definition("parametric_survival", {**params, "strata": None}, "en", None, None)
             ["sections"]["body"]["items"]}
    assert "groups" not in items["quantiles"] and items["hazard"]["w"] < common.WIDTH


def test_a_note_s_names_stay_as_they_are_and_its_numbers_take_the_language_s_decimal():
    """A stratum, a column or a list of names called «2.5» is a name, not a number: «2.5» in every language, as in
    the tables; a number of the same note takes the page's decimal."""
    from app.reports import formats

    w = {"note_texts": {"x": "{stratum} | {cluster} | {weights} | {pairs} | {epv} | {times}"}}
    said = common.structured_note(w, "@x stratum=2.5 cluster=1.5 weights=0.5 pairs=2.5%20×%203.5 epv=2.5 times=1.5,2.5",
                                  formats.locale("it"))
    assert said == "2.5 | 1.5 | 0.5 | 2.5 × 3.5 | 2,5 | 1,5, 2,5"
    assert common.structured_note(w, "@x stratum=2.5 epv=2.5", formats.locale("en")).startswith("2.5 | ")


@pytest.mark.parametrize("locale", LOCALES)
def test_a_parametric_survival_candidate_auto_did_not_check_says_so(tmp_path, chart_service, monkeypatch, locale):  # noqa: F811
    """`auto` checks the convergence of the candidates its choice reaches: one fitted that it never reached has no row
    `converged`, and the table says «fitted, not checked» with a line under it saying why; it still counts as fitted."""
    model, params, rows = fixture("parametric_survival-auto")
    chosen = {r["level"] for r in rows if r["section"] == "candidate" and r["stat"] == "chosen" and r["value"] == 1}
    gone = {r["level"] for r in rows if r["section"] == "candidate" and r["stat"] == "converged" and r["level"] not in chosen}
    assert len(gone) == 5
    rows = [r for r in rows if not (r["section"] == "candidate" and r["stat"] == "converged" and r["level"] in gone)]
    w = common.words(model, "auto", locale)
    _, page = draw(tmp_path, chart_service, monkeypatch, "parametric_survival-auto", fmt="svg", locale=locale, rows=rows)
    texts = strings(json.loads(page))
    said = " ".join(texts)
    assert texts.count(w["candidates"]["not_checked"]["cell"]) == 5 and w["candidates"]["not_checked"]["legend"] in said
    head, tail = w["candidates"]["lowest"].split("{n}")
    assert re.search(re.escape(head.split("}")[-1]) + "6" + re.escape(tail), said), said[:400]  # the six fitted
    _, page = draw(tmp_path / "all", chart_service, monkeypatch, "parametric_survival-auto", fmt="svg", locale=locale)
    said = " ".join(strings(json.loads(page)))
    assert w["candidates"]["not_checked"]["cell"] not in said and w["candidates"]["not_checked"]["legend"] not in said


@pytest.mark.parametrize("locale", LOCALES)
def test_a_clv_candidate_auto_did_not_check_says_so(tmp_path, chart_service, monkeypatch, locale):  # noqa: F811
    """`auto` stops at the first candidate, by log-likelihood, whose convergence check passes: the others have no row
    `converged`, and the table says «not checked» with a line under it saying why."""
    model, params, rows = fixture("clv-value")
    converged = [r for r in rows if r["section"] == "candidate" and r["stat"] == "converged"]
    assert len(converged) == 3
    chosen = {r["level"] for r in rows if r["section"] == "candidate" and r["stat"] == "chosen" and r["value"] == 1}
    rows = [r for r in rows if not (r in converged and r["level"] not in chosen)]
    w = common.words(model, "value", locale)
    _, page = draw(tmp_path, chart_service, monkeypatch, "clv-value", fmt="svg", locale=locale, rows=rows)
    said = strings(json.loads(page))
    assert said.count(w["candidate_not_checked"]["cell"]) == 2 and w["candidate_not_checked"]["legend"] in said
    _, page = draw(tmp_path / "all", chart_service, monkeypatch, "clv-value", fmt="svg", locale=locale)
    said = strings(json.loads(page))
    assert w["candidate_not_checked"]["cell"] not in said and w["candidate_not_checked"]["legend"] not in said


# ── the experiments' reports (docs/specs/ab-testing.md, lot E6): the explanation, its numbers, the decision ────────
AB_CASES = [c for c in CASES if c.split("-")[0] in EXPERIMENTS]
TEMPLATES = ("reading_texts", "formula_texts", "note_texts")


def _ab_words(model: str, locale: str) -> dict:
    return common.words(model, "binary", locale)


def test_the_number_forms_say_a_number_as_each_language_writes_it():
    said = {code: [common.number_form(x, form, formats.locale(code)) for x, form in (
        (0.041, "pct"), (0.206464, "spct"), (-0.0189, "spct"), (0.000247, "pct"), (2.4e-5, "pct"), (0.99999996, "pct"),
        (12480, "int"), (0.0050979, "p"), (4.9e-6, "p"), (41.2, "num"), (1840.5, "num"), (0.048, "num"), (20523, "date"))]
        for code in LOCALES}
    assert said["en"] == ["4.1 %", "+20.6 %", "−1.89 %", "0.02 %", "< 0.01 %", "> 99.99 %", "12,480", "0.0051", "< 0.0001",
                          "41.2", "1,840.5", "0.048", "11 Mar 2026"]
    assert said["it"] == ["4,1 %", "+20,6 %", "−1,89 %", "0,02 %", "< 0,01 %", "> 99,99 %", "12.480", "0,0051", "< 0,0001",
                          "41,2", "1.840,5", "0,048", "11/03/2026"]
    assert said["de"][6] == "12.480" and said["de"][12] == "11.03.2026"
    assert said["es"][6] == "12.480" and common.number_form(4210, "int", formats.locale("es")) == "4210"  # CLDR: Spanish groups from five digits
    assert said["fr"][6] == "12 480" and said["fr"][1] == "+20,6 %"
    # a template asks for a form; one that does not says its numbers as before, and a name is never put in a form
    w = {"reading_texts": {"x": "{a} | {a:pct} | {variant:pct} | {s:pct}"}}
    assert common.structured_note(w, "@x a=0.5 variant=0.5 s=0.5%2C0.25,0.25", formats.locale("it"), "reading_texts") == \
        "0,5 | 50 % | 0.5 | 50 % / 25 % / 25 %"


@pytest.mark.parametrize("name", ("experiments", "ab_test", "ab_plan"))
def test_every_template_of_the_experiments_has_its_five_texts_with_the_same_numbers(name):
    words = common._file(name)
    for kind in TEMPLATES:
        english = words["en"].get(kind, {})
        for code in LOCALES:
            other = words[code].get(kind, {})
            assert set(other) == set(english), (name, kind, code)
            for key, text in english.items():
                for one, two in ((text, other[key]),) if isinstance(text, str) else ((text[k], other[key][k]) for k in text):
                    assert sorted(re.findall(r"\{[^}]+\}", one)) == sorted(re.findall(r"\{[^}]+\}", two)), (name, code, key)


@pytest.mark.parametrize("case", AB_CASES)
@pytest.mark.parametrize("locale", LOCALES)
def test_every_reading_of_an_experiment_is_a_sentence_of_the_page_s_language(case, locale):
    """Each row of the explanation the runner writes («@id name=value…») has its sentence in the five languages, every
    number in its place and form, and is labelled by what it is about."""
    model, params, rows = fixture(case)
    ids = {r["text"][1:].split(" ")[0] for r in rows if r["section"] == "reading"}
    w = _ab_words(model, locale)
    assert ids and all(i in w["formula_texts" if i.startswith("formula_") else "reading_texts"] for i in ids), \
        sorted(i for i in ids if i not in w["reading_texts"] and i not in w["formula_texts"])
    said = [r for r in layouts.with_labels(model, params, rows, locale) if r["section"] == "reading"]
    for r in said:
        assert not re.search(r"@|\{|%2", r["text"]), r["text"]
        assert r["label"] not in ("", r["stat"]), r
    if model == "ab_plan":  # the history against the plan, a measure said as a note
        [short] = [r["text"] for r in layouts.with_labels(model, params, rows, locale) if r["stat"] == "history_short"]
        assert "@" not in short and "56" in short


def test_a_reading_follows_the_direction_the_node_calls_better():
    model, params, rows = fixture("ab_test-binary")
    above = [r["text"] for r in layouts.with_labels(model, params, rows, "en") if r["key"] == "reading|bayes|B|line_1"]
    below = [r["text"] for r in layouts.with_labels(model, {**params, "higher_is_better": False}, rows, "en")
             if r["key"] == "reading|bayes|B|line_1"]
    assert above == ["Given these data and the prior, the probability that the true value of B is above that of A is 99.87 %."]
    assert below == ["Given these data and the prior, the probability that the true value of B is below that of A is 99.87 %."]


@pytest.mark.parametrize("locale", LOCALES)
def test_no_sentence_of_a_measure_holds_a_word_of_judgement(locale):
    """The owner's rule: verdicts come only from tests with a p-value against alpha. P(ahead of the control), the expected
    loss, the detectable lift, the power, the sizes are measures: their sentences say the number and what it means,
    never «better», «significant», «worse» (the words file's `verdict_words`, its `measure_readings`)."""
    shared = common._file("experiments")[locale]
    assert shared["measure_readings"] == common._file("experiments")["en"]["measure_readings"] and shared["verdict_words"]
    texts = {**_ab_words("ab_test", locale)["reading_texts"], **_ab_words("ab_plan", locale)["reading_texts"]}
    measures = {"bayes_beat", "bayes_loss", "power_detectable", "plan_size", "plan_mde_units", "plan_mde_days"}
    assert measures <= set(shared["measure_readings"]) <= set(texts)
    for key in shared["measure_readings"]:
        for sentence in strings(texts[key]):
            for word in shared["verdict_words"]:
                assert not re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", sentence, re.I), (locale, key, word)
    # and the words do catch a verdict: a test's sentence holds one (the secondary metrics' too, lot E8)
    verdicts = [texts[k] for k in ("decision_better", "comparison_significant", "secondary_worse", "secondary_worse_more", "secondary_none_worse")]
    assert all(any(re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", s, re.I) for v in shared["verdict_words"]) for s in verdicts), locale


def test_the_first_page_opens_with_the_srm_band_when_the_split_failed_and_says_the_decision(tmp_path, chart_service, monkeypatch):  # noqa: F811
    _, page = draw(tmp_path / "ratio", chart_service, monkeypatch, "ab_test-ratio", fmt="svg")
    said = set(strings(json.loads(page)))
    band = [s for s in said if s.startswith("Sample ratio mismatch: the units are not split as planned (p < 0.0001, below 0.001)")]
    assert band, sorted(s for s in said if "mismatch" in s)
    assert any(s.startswith("Sample ratio mismatch. B beats the control A: +6.1 % [+3.8 %, +8.3 %]") for s in said)
    _, page = draw(tmp_path / "binary", chart_service, monkeypatch, "ab_test-binary", fmt="svg")
    said = set(strings(json.loads(page)))
    assert not any("not split as planned" in s for s in said)
    assert "B beats the control A: +20.6 % [+6.8 %, +36.5 %] on «converted», corrected p = 0.0051 (Holm), α = 0.05" in said
    assert {"37,385", "3.62 %", "+20.6 %", "0.0051"} <= said  # the cards: units, the control's rate, the lift and p of B
    assert any(s.startswith("Reading: B beats the control A: its corrected p-value, 0.0051, is below α = 0.05") for s in said)
    _, page = draw(tmp_path / "continuous", chart_service, monkeypatch, "ab_test-continuous", fmt="svg", locale="it")
    said = set(strings(json.loads(page)))
    assert any(s.startswith("Nessuna variante differisce dal controllo A ad α = 0,05. Con queste unità il test avrebbe rilevato "
                            "un incremento di 6,6 % (potenza 80 %).") for s in said), sorted(s for s in said if s.startswith("Nessuna"))


def test_a_secondary_metric_worse_than_the_control_is_on_the_first_page_and_every_one_on_its_own(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """Lot E8: the guardrails that came out worse are listed under the treatments (their adjusted p against alpha); every
    comparison of every secondary metric has its row on the page of the secondary metrics, with how the family was
    corrected and the worse ones in its reading."""
    model, params, rows = fixture("ab_test-continuous")
    _, page = draw(tmp_path / "none", chart_service, monkeypatch, "ab_test-continuous", fmt="svg")
    listed = strings(json.loads(page))
    said = set(listed)
    w = _ab_words(model, "en")
    assert {w["sections"]["secondary"], w["secondary"]["lead"], "sessions ↓", "converted ↑"} <= said  # each in its own direction
    assert listed.count("sessions ↓") == 2 and listed.count("converted ↑") == 2  # one row per treatment, none on the first page
    assert any(s.startswith("Reading: The 4 comparisons of the secondary metrics (each metric against the control, for each treatment) were "
                            "corrected among themselves") and s.endswith("No secondary metric is worse than the control at α = 0.05, after the correction.")
               for s in said), sorted(s for s in said if s.startswith("Reading: The 4"))
    worse = [{**r, "text": "worse"} if r["key"] == "secondary|sessions|B|decision" else
             {**r, "text": "@secondary_worse variant=B metric=sessions p_adjusted=0.004"} if r["key"] == "reading|secondary||line_2" else r
             for r in rows]
    _, page = draw(tmp_path / "worse", chart_service, monkeypatch, "ab_test-continuous", fmt="svg", rows=worse, locale="it")
    listed = strings(json.loads(page))
    assert listed.count("sessions ↓") == 3 and listed.count("converted ↑") == 2  # B's sessions under the treatments too
    assert any(s.endswith("B è peggiore del controllo sulla metrica secondaria «sessions»: p-value corretto 0,0040.") for s in listed)


def test_the_last_pages_explain_the_method_and_excel_keeps_the_reading(tmp_path, chart_service, monkeypatch):  # noqa: F811
    _, page = draw(tmp_path, chart_service, monkeypatch, "ab_test-continuous", fmt="svg")
    said = " ".join(strings(json.loads(page)))
    w = _ab_words("ab_test", "en")
    for key in ("method_cuped", "method_cap", "method_bayes", "method_sequential"):
        assert w["reading_texts"][key] in said, key
    assert w["formula_texts"]["formula_welch"] in said and w["sections"]["how"] in said
    out, _ = draw(tmp_path / "x", chart_service, monkeypatch, "ab_test-binary", fmt="xlsx")
    with zipfile.ZipFile(out.path) as z:
        sheets = re.findall(r'<sheet name="([^"]+)"', z.read("xl/workbook.xml").decode())
    assert sheets[:2] == ["The treatments against the cont", "Checks of the experiment"]  # 31 characters at most
    assert {"Reading", "Reading of the comparisons", "Reading of the Bayesian measure", "Reading of the sequential test"} <= set(sheets)
    assert sheets[-2] == "Reading", sheets  # the explanation last, before the notes


# the notes the experiments' runners send (ab_data.R and the spec's «The output»), with values in their forms
AB_NOTES = {
    "metric_missing": "count=92 variants=A,B", "date_missing": "count=3 variants=C", "covariate_filled": "variant=B count=1402 share=0.1126 mean=12.3",
    "covariate_excluded": "variant=B count=205 share=0.0175", "crossover_excluded": "units=37",
    "cap_applied": "percentile=0.999 value=1840.5 count=96", "fieller_unbounded": "variant=B", "one_treatment": "",
    "looks_grouped": "days=1460 period=2", "control_zero": "", "history_short": "history_days=56 planned_days=401",
    "secondary_missing": "metric=revenue count=12040",
}


@pytest.mark.parametrize("locale", LOCALES)
def test_every_note_of_an_experiment_is_a_sentence_with_its_numbers(locale):
    model, params, rows = fixture("ab_test-continuous")
    notes = [_model_row(f"note_{i}", f"@{note} {values}".strip()) for i, (note, values) in enumerate(AB_NOTES.items(), 1)]
    said = {r["stat"]: r["text"] for r in layouts.with_labels(model, params, rows + notes, locale) if r["stat"].startswith("note_")}
    assert set(_ab_words(model, locale)["note_texts"]) == set(AB_NOTES)
    decimal = formats.locale(locale).decimal
    for text in said.values():
        assert "@" not in text and "{" not in text and "%2" not in text, text
    assert f"11{decimal}3 %" in said["note_3"] and f"12{decimal}3" in said["note_3"] and "B" in said["note_3"]
    assert f"99{decimal}9 %" in said["note_6"] and "96" in said["note_6"]
    assert "«revenue»" in said["note_12"] and common.number_form(12040, "int", formats.locale(locale)) in said["note_12"]


@pytest.mark.parametrize("case", AB_CASES + [c for c in CASES if c.startswith("decomposition-")])
@pytest.mark.parametrize("locale", LOCALES)
def test_no_header_of_an_experiment_s_table_runs_into_the_next_column(case, locale):
    """The report engine warns of a value wider than its column (model.py, the tables' widths), not of a header: each
    word of a header fits its column in bold, at the table's size, within the cell's padding — the engine's own
    measure. The checks' table is every model's (common.py) and is not the experiments' to change."""
    from app.reports.model import CELL_PAD

    model, params, _ = fixture(case)
    for item in layouts.definition(model, params, locale, None, None)["sections"]["body"]["items"]:
        if item["type"] != "table" or item["id"] in ("assumptions", "advice"):
            continue
        cols = item["columns"]
        left = item["w"] - sum(c.get("width") or 0 for c in cols)
        free = sum(1 for c in cols if not c.get("width")) or 1
        size = item.get("style", {}).get("size", 9)
        for c in cols:
            room = (c.get("width") or left / free) - 2 * CELL_PAD + 0.2
            wide = [word for word in c["header"].split() if glyphs.width_mm(word, size, 700) > room]
            assert not wide, (item["id"], c["header"], round(room, 1))


# ── the decomposition's report (docs/specs/ts-decomposition.md, lot D4): the explanation, the positions, the pages ─
DECOMPOSITION_CASES = [c for c in CASES if c.startswith("decomposition-")]
# every template id the runner may write (the spec's «Explaining the method in the report» and D4's «As built»)
DECOMPOSITION_READINGS = (
    "summary_stl", "summary_classical", "summary_mstl", "summary_emd", "transform_log", "transform_box_cox", "filled_linear",
    "filled_seasonal", "strength", "strength_not_computed", "seasonality_supported", "seasonality_not_supported",
    "seasonality_not_computed", "season_shape", "season_shape_ratio", "remainder_white", "remainder_structure", "remainder_large_n",
    "remainder_not_computed_emd", "stationarity_both_ok", "stationarity_adf_only", "stationarity_kpss_only", "stationarity_neither",
    "stationarity_both_ok_kpss_above", "stationarity_kpss_only_kpss_above", "stationarity_adf_only_kpss_below",
    "stationarity_neither_kpss_below",
    "kpss_table_end", "imfs_found", "imf_mode", "imfs_cut", "imfs_empty", "robust_weights", "method_stl", "method_classical",
    "method_mstl", "method_emd", "method_transform", "method_filled", "method_seasonality", "method_strength", "method_remainder",
    "method_stationarity")
DECOMPOSITION_FORMULAS = ("formula_stl", "formula_classical", "formula_mstl", "formula_emd", "formula_box_cox", "formula_strength",
                          "formula_friedman", "formula_kruskal_wallis", "formula_ljung_box", "formula_adf", "formula_kpss",
                          "formula_orthogonality")
DECOMPOSITION_VERDICTS = ("seasonality_supported", "seasonality_not_supported", "remainder_white", "remainder_structure")


def _said(text) -> list[str]:
    """A template's sentences: one, or one for each setting it depends on (by_params)."""
    return [text] if isinstance(text, str) else list(text.values())


def test_every_reading_of_a_decomposition_has_its_five_texts_with_the_same_numbers():
    words = common._file("decomposition")
    for kind, ids in (("reading_texts", DECOMPOSITION_READINGS), ("formula_texts", DECOMPOSITION_FORMULAS)):
        english = words["en"][kind]
        assert set(english) == set(ids), (kind, sorted(set(english) ^ set(ids)))
        for code in LOCALES:
            other = words[code][kind]
            assert set(other) == set(english), (kind, code)
            for key, text in english.items():
                assert isinstance(text, str) == isinstance(other[key], str) and (isinstance(text, str) or set(text) == set(other[key]))
                pairs = ((text, other[key]),) if isinstance(text, str) else ((text[k], other[key][k]) for k in text)
                for one, two in pairs:
                    assert sorted(re.findall(r"\{[^}]+\}", one)) == sorted(re.findall(r"\{[^}]+\}", two)), (code, key)
        labelled = set(words["en"]["reading_labels"])
        assert {i for i in ids if i.startswith(("method_", "formula_"))} <= labelled, kind


@pytest.mark.parametrize("case", DECOMPOSITION_CASES)
@pytest.mark.parametrize("locale", LOCALES)
def test_every_reading_of_a_decomposition_is_a_sentence_of_the_page_s_language(case, locale):
    model, params, rows = fixture(case)
    ids = {r["text"][1:].split(" ")[0] for r in rows if r["section"] == "reading"}
    assert ids and ids <= set(DECOMPOSITION_READINGS) | set(DECOMPOSITION_FORMULAS), sorted(ids)
    for r in (r for r in layouts.with_labels(model, params, rows, locale) if r["section"] == "reading"):
        assert not re.search(r"@|\{|%2|⟨", r["text"]), r["text"]
        assert r["label"] not in ("", r["stat"]), r


@pytest.mark.parametrize("locale", LOCALES)
def test_no_sentence_of_a_decomposition_s_measure_holds_a_word_of_judgement(locale):
    """The owner's rule: verdicts come only from tests with a p-value against alpha. The strengths F_T and F_S, the
    shape of a season, the IMFs' periods and shares, the largest autocorrelation are measures: their sentences never
    say «supported», «white noise», «structure left», «strong» (the words file's `verdict_words`, `measure_readings`)."""
    w = common._file("decomposition")[locale]
    assert w["measure_readings"] == common._file("decomposition")["en"]["measure_readings"] and w["verdict_words"]
    assert {"strength", "season_shape", "imf_mode", "imfs_found", "remainder_large_n"} <= set(w["measure_readings"]) <= set(w["reading_texts"])
    assert not set(w["measure_readings"]) & set(DECOMPOSITION_VERDICTS)
    sentences = [*(s for key in w["measure_readings"] for s in _said(w["reading_texts"][key])), *w["measure_texts"].values()]
    for sentence in sentences:
        for word in w["verdict_words"]:
            assert not re.search(r"(?<!\w)" + re.escape(word) + r"(?!\w)", sentence, re.I), (locale, word, sentence)
    # and the words do catch a verdict: each test's sentence holds one
    for key in DECOMPOSITION_VERDICTS:
        for sentence in _said(w["reading_texts"][key]):
            assert any(re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", sentence, re.I) for v in w["verdict_words"]), (locale, key)


def test_a_decomposition_says_its_positions_in_words_and_its_first_page_the_checks_and_the_measures(tmp_path, chart_service, monkeypatch):  # noqa: F811
    _, page = draw(tmp_path / "en", chart_service, monkeypatch, "decomposition-mstl", fmt="svg")
    said = set(strings(json.loads(page)))
    assert any(s.startswith("Reading: MSTL split «orders» (90 observations) into a trend, one seasonal pattern for each period (7 / 30 days) "
                            "and a remainder, the part none of them describes. 2 instants without a value were filled by linear "
                            "interpolation; the output marks them in its column «filled». The trend's strength is 0.9864") for s in said)
    assert any("The pattern that repeats every 7 days is highest on Wednesday, where it adds 5.85 to the trend, and lowest on Saturday, "
               "where it adds −5.85." in s for s in said), sorted(s for s in said if "repeats every 7" in s)
    assert any("is highest at position 6" in s for s in said)
    assert {"Wednesday", "Saturday", "position 6", "position 21"} <= said  # the table of the seasons
    assert not [s for s in said if re.search(r"(weekday|month|step|hour|day)_\d", s)]
    assert {"Checks", "What it is", "Trend strength F_T", "0.99", "0.98 · 0.92", "periods 7, 30",
            "periods 7, 30 · robust", "1 Jan 2026 – 31 Mar 2026", "Seasonality: rank test — 7", "≥ 0.1", "0.0040",
            "orders = trend + seasonal[7] + seasonal[30] + remainder · MSTL, robust · α = 0.05"} <= said
    assert "≤ 0.01" not in said  # ADF's p-value is MacKinnon's, continuous: only KPSS's is a table's
    # a seasonal pattern the test does not support is no broken assumption: its own words, not the red «× failed»;
    # ADF's good outcome is the rejection of its H0, said so
    assert {"✓ supported", "– not supported", "✓ rejected"} <= said and "× failed" not in said, sorted(s for s in said if s[:1] in "✓×–")
    assert "«orders» over «day», daily series" in said
    checks = next(it for it in layouts.definition("decomposition", fixture("decomposition-mstl")[1], "en")["sections"]["body"]["items"]
                  if it["id"] == "assumptions")
    quiet = [r for col in checks["columns"] for r in col.get("rules", []) if r["style"].get("color") == common.MUTED]
    assert quiet and '[level] = "seasonality"' in quiet[-1]["when"] and '"failed"' in quiet[-1]["when"]
    assert any(s.startswith("A seasonal pattern of period 7 is supported: if the positions of the cycle did not differ, rankings this "
                            "unequal would appear by chance with probability < 0.0001, below α = 0.05 (Friedman's test on 12 complete "
                            "cycles).") for s in said)
    _, page = draw(tmp_path / "it", chart_service, monkeypatch, "decomposition-classical", fmt="svg", locale="it")
    said = set(strings(json.loads(page)))
    assert any("L'andamento che si ripete ogni 12 mesi è massimo ad aprile, dove moltiplica il trend per 1,25, e minimo a ottobre, "
               "dove lo moltiplica per 0,75." in s for s in said), sorted(s for s in said if "si ripete" in s)
    assert {"aprile", "ottobre", "boxes = trend × stagionale[12] × residuo · classica, moltiplicativa · α = 0,05",
            "✓ sostenuta", "✓ rifiutata", "«boxes» nel tempo «month», serie mensile"} <= said
    for code in LOCALES:  # every frequency of the catalogue is said in each language, none left as the catalogue's word
        for frequency in catalog.KINDS["frequency"]["choices"]:
            said_about = layouts.title("decomposition", {"time": "t", "value": "v", "frequency": frequency, "period": 5}, code)
            assert not re.search(rf"\b{frequency}\b", said_about) and said_about.count("«") == 2, (code, said_about)


def test_a_decomposition_says_the_runner_s_codes_of_positions_kpss_s_table_ends_and_its_reasons_in_words(tmp_path, chart_service, monkeypatch):  # noqa: F811
    """ts_positions' codes (a weekday's hour, a day of the year), KPSS's p-value at an end of its table (D1's p_side: the
    runner chooses the id that says «≤ 0.01») and a check not computed for one of D1's reasons."""
    model, params, rows = fixture("decomposition-mstl")
    swapped = {
        "reading|season|7|line_1": "@season_shape period=7 peak=weekday_3_hour_14 trough=yearday_45 peak_value=5.8 trough_value=-5.8 "
                                   "position=weekday_hour",
        "reading|stationarity||line_1": "@stationarity_neither_kpss_below p_adf=0.3 alpha=0.05",
        "season|7||peak_label": "weekday_3_hour_14", "season|7||position_kind": "weekday_hour",
        "assumption|kpss||verdict": "not_computed",
    }
    rows = [{**r, "text": swapped.get(r["key"], r["text"])} for r in rows]
    rows.append({**next(r for r in rows if r["key"] == "assumption|kpss||verdict"), "stat": "advice_id", "text": "kpss_short",
                 "key": "assumption|kpss||advice_id"})
    rows.append({**next(r for r in rows if r["key"] == "assumption|kpss||verdict"), "stat": "advice", "text": "too short",
                 "key": "assumption|kpss||advice"})
    _, page = draw(tmp_path / "en", chart_service, monkeypatch, "decomposition-mstl", fmt="svg", rows=rows)
    said = set(strings(json.loads(page)))
    assert any("is highest on Wednesday, at hour 14, where it adds 5.8 to the trend, and lowest on day 45," in s for s in said),         sorted(s for s in said if "repeats every 7" in s)
    assert "Wednesday, hour 14" in said and any("1 = Monday from 00:00" in s for s in said)
    assert any("KPSS rejects stationarity (p-value ≤ 0.01), at α = 0.05" in s for s in said)
    assert "The remainder has fewer than 12 values: too few for the test." in said
    _, page = draw(tmp_path / "it", chart_service, monkeypatch, "decomposition-mstl", fmt="svg", rows=rows, locale="it")
    said = set(strings(json.loads(page)))
    assert any("è massimo di mercoledì, all'ora 14, dove aggiunge 5,8 al trend, e minimo il giorno 45," in s for s in said)
    assert any("(p-value ≤ 0,01)" in s for s in said)


def test_an_emd_decomposition_draws_its_oscillations_with_their_table_and_its_time_in_steps(tmp_path, chart_service, monkeypatch):  # noqa: F811
    model, params, rows = fixture("decomposition-emd")
    d = layouts.definition(model, params, "en")
    body = {it["id"]: it for it in d["sections"]["body"]["items"]}
    assert {"imf_1", "imf_10", "residue", "imf_observed", "imfs", "noise"} <= set(body) and "components" not in body
    assert body["imf_1_steps"]["options"]["x_time"] is False and body["imf_1"]["options"]["x_time"] is True
    _, page = draw(tmp_path, chart_service, monkeypatch, "decomposition-emd", fmt="svg")
    said = set(strings(json.loads(page)))
    assert {"Oscillations (EMD)", "IMF 1", "IMF 3", "Residue", "Largest oscillation", "65.1 %", "of the variance, IMF 3"} <= said, \
        sorted(said)
    assert any(s.startswith("Reading: EMD found 3 oscillations (IMFs) of the 10 allowed") and "IMF 3 repeats about every 21.33 steps." in s
               for s in said)


def test_the_decomposition_s_last_pages_explain_the_method_and_excel_keeps_the_checks_the_seasons_and_the_reading(
        tmp_path, chart_service, monkeypatch):  # noqa: F811
    _, page = draw(tmp_path, chart_service, monkeypatch, "decomposition-stl", fmt="svg")
    said = " ".join(strings(json.loads(page)))
    w = common.words("decomposition", "stl", "en")
    assert w["reading_texts"]["method_stl"] in said and w["formula_texts"]["formula_stl"] in said and w["sections"]["how"] in said
    out, _ = draw(tmp_path / "x", chart_service, monkeypatch, "decomposition-mstl", fmt="xlsx")
    with zipfile.ZipFile(out.path) as z:
        sheets = re.findall(r'<sheet name="([^"]+)"', z.read("xl/workbook.xml").decode())
    assert {"Checks of the decomposition", "Measures, not tests", "The seasonal patterns", "Mean pattern by position",
            "Reading of the seasonal pattern", "Checks of the remainder", "Reading"} <= set(sheets), sheets

