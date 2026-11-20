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
from app.engine.models.results import read_fixture
from app.engine.stats.layouts import LOCALES
from app.engine.stats.output import OUTPUT_NAMES
from app.reports import definition as rd
from app.reports import glyphs, pdf, render
from app.tasks import jobs, report_jobs
from tests.fakes import FakeRedis
from tests.test_export_route import Result, Storage, _play
from tests.test_reports_render import chart_service  # noqa: F401 — the fake chart service

NOW = datetime(2026, 11, 19, 23, 30, tzinfo=timezone.utc)
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "models"
CASES = sorted(p.stem for p in FIXTURES.glob("*-*.json"))


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


def draw(tmp_path, chart_service, monkeypatch, case, fmt="pdf", locale="en", sample=None, step=None):  # noqa: F811
    """The report of a fixture, and what the template was handed (the page's texts)."""
    handed: list[str] = []
    run = pdf.run

    def spy(job, *args, **kwargs):
        if "inputs" in job:
            handed.append(job["inputs"]["model"])
        return run(job, *args, **kwargs)

    monkeypatch.setattr(pdf, "run", spy)
    model, params, rows = fixture(case)
    datasets = {"result": {"rows": layouts.with_labels(model, params, rows, locale), "columns": layouts.columns()}}
    out = render.render(layouts.definition(model, params, locale, sample, step), datasets, {}, fmt, tmp_path,
                        title=layouts.title(model, params, locale), now=NOW,
                        limits=render.Limits(chart_url=chart_service[0], max_rows=layouts.MAX_REPORT_ROWS))
    return out, (handed[0] if handed else "")


# ── the catalogue, the fixtures, the words ──────────────────────────────────
def test_every_model_and_variant_has_a_layout_and_a_fixture():
    assert set(layouts.LAYOUTS) == set(catalog.MODELS)
    assert set(CASES) == {f"{m}-{v['id']}" for m, spec in catalog.MODELS.items() for v in spec["variants"]}
    for case in CASES:  # each fixture is what its variant writes (results.py)
        model, params, table = read_fixture(FIXTURES / f"{case}.json")
        assert f"{model}-{catalog.variant(model, params)}" == case


def test_the_words_are_complete_in_the_five_languages():
    for name in ("common", "penalized", *catalog.MODELS):
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
        for section in ("model", "coef", "stratum", "pair", "candidate"):
            declared = spec["sections"].get(section, {})
            for stat in [*declared.get("numbers", []), *(t for t in declared.get("texts", []) if section == "model")]:
                if stat.endswith("*") or stat.endswith("significance") or stat in ("model", "variant", "formula", "software",
                                                                                    "group_version", "rows_from"):
                    continue
                assert w["stats"].get(section, {}).get(stat), f"{code}: no word for {section}.{stat}"
        for check in spec["sections"].get("assumption", {}).get("levels", []):
            said = w["checks"].get(check, {})
            assert said.get("name") and said.get("h0") and (said.get("advice") or said.get("not_computed")), f"{code}: {check}"
        for row in layouts.with_labels(model, params, rows, code):  # the numbered ones too (note_1, surv_at_30)
            if row["section"] in ("model", "stratum") and row["stat"].startswith(("note_", "surv_at_", "ar_root", "ma_root")):
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
        # the assumptions come before every coefficient, number of the fit and chart: the first page
        first = {it["id"]: i for i, it in enumerate(body)}
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


# ── drawn ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("case", CASES)
@pytest.mark.parametrize("locale", LOCALES)
def test_every_report_renders_in_the_five_languages(tmp_path, chart_service, monkeypatch, case, locale):  # noqa: F811
    out, page = draw(tmp_path, chart_service, monkeypatch, case, locale=locale, sample=[{"first": 500}], step="Prices compared")
    assert out.path.read_bytes().startswith(b"%PDF-") and 2 <= out.pages <= 8 and out.warnings == []
    said = set(strings(json.loads(page)))
    w = common.words(*case.split("-"), locale)
    assert w["sections"]["assumptions"] in said and any(s.startswith(w["footer"] + " · ") for s in said)


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
    assert forecast["yAxis"]["min"] > 0  # the bands' stack does not pull the axis to zero
    acf = asked["ACF of the residuals"]
    marks = next(s for s in acf["series"] if "markLine" in s)["markLine"]["data"]
    assert acf["series"][0]["type"] == "bar" and marks[0]["yAxis"] == -marks[1]["yAxis"] > 0


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
