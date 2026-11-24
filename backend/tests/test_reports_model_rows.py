"""POST /reports/model/rows: only the sampler's facts and the nodes' extremes of a model's results leave the engine."""
import pytest
from fastapi import HTTPException

from app.api.routes import reports
from app.tasks import report_jobs

ROWS = [
    {"section": "summary", "level": "mu", "level_2": None, "stat": "mean", "value": 3.5, "text": None, "x0": None, "ord": 0},
    {"section": "sampler", "level": "all", "level_2": None, "stat": "divergences", "value": 2.0, "text": None, "x0": None, "ord": 1},
    {"section": "quantity", "level": "mu", "level_2": None, "stat": "rhat_max", "value": 1.01, "text": None, "x0": None, "ord": 2},
    {"section": "data", "level": "y", "level_2": None, "stat": "mean", "value": 9.0, "text": None, "x0": None, "ord": 3},
    {"section": "hist", "level": "mu", "level_2": None, "stat": "count", "value": 4.0, "text": None, "x0": 0.1, "ord": 4},
]


def ask(monkeypatch, sections, rows=ROWS):
    monkeypatch.setattr(report_jobs, "model_rows", lambda bucket, key, model, params: rows)
    monkeypatch.setattr("app.engine.models.catalog.check_params", lambda model, params: None)
    return reports.model_rows_route(reports.ModelRows(bucket="b", results_key="k", model="bayes_model", sections=sections))


def test_only_the_asked_sections_are_given_in_their_order(monkeypatch):
    got = ask(monkeypatch, ["sampler", "quantity"])["rows"]
    assert [r["section"] for r in got] == ["sampler", "quantity"]
    assert set(got[0]) == {"section", "level", "level_2", "stat", "value", "text", "x0"}  # no `ord`


@pytest.mark.parametrize("sections", [["summary"], ["sampler", "data"], ["hist"], ["model"]])
def test_a_section_that_holds_the_posterior_or_the_data_is_refused(monkeypatch, sections):
    with pytest.raises(HTTPException) as e:
        ask(monkeypatch, sections)
    assert e.value.status_code == 422


def test_results_that_are_gone_are_a_404(monkeypatch):
    def gone(*a):
        raise report_jobs.ModelResultsGone(report_jobs.MODEL_RESULTS_GONE)
    monkeypatch.setattr(report_jobs, "model_rows", gone)
    monkeypatch.setattr("app.engine.models.catalog.check_params", lambda model, params: None)
    with pytest.raises(HTTPException) as e:
        reports.model_rows_route(reports.ModelRows(bucket="b", results_key="k", model="bayes_model", sections=["sampler"]))
    assert e.value.status_code == 404
