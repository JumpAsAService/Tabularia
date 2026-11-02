"""The reports of the sample data (infrastructure/sampledb/seed_tabularia.py) in the engine: their expressions, fields
and formats pass the check of a save, and they render, the same both ways, with no warning about a width or an edge.
The gateway's half, the structure, is gateway/tests/test_sample_reports.py; the numbers are proved on a running stack."""
import importlib.util
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.reports import definition as rd
from app.reports import render
from tests.reports_both_ways import both_ways  # noqa: F401 — every report, both ways
from tests.test_reports_render import chart_service  # noqa: F401 — a fake one


def _seed():
    # /sampledb in the CI image (.github/scripts/ci_tests.sh), the clone's folder on a laptop
    for folder in (Path("/sampledb"), Path(__file__).resolve().parents[2] / "infrastructure" / "sampledb"):
        if (folder / "seed_tabularia.py").exists():
            spec = importlib.util.spec_from_file_location("seed_tabularia", folder / "seed_tabularia.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            return module
    raise AssertionError("the seed of the sample data is not there")


seed = _seed()
NOW = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
CATEGORIES = ("Insaccati cotti", "Prosciutti", "Salami")
AREAS = ("Centro", "Nord-Est", "Nord-Ovest")


def _table(id_: int, names: dict[str, str]) -> dict:
    return {"id": id_, "name": str(id_), "project_id": 3, "columns": [{"name": k, "dtype": v} for k, v in names.items()]}


MONTHS = _table(12, {"categoria": "String", "anno_mese": "Int32", "ricavo": "Float64", "costo": "Float64", "margine": "Float64",
                     "chili": "Float64", "ordini": "UInt32", "margine_pct": "Float64"})
AGENTS = _table(18, {"area": "String", "agente": "String", "ricavo": "Float64", "ordini": "UInt32", "clienti": "UInt32",
                     "ricavo_per_ordine": "Float64"})
CRM = _table(15, {"area": "String", "agente": "String", "email": "UInt32", "reclamo": "UInt32", "sollecito": "UInt32",
                  "telefonata": "UInt32", "visita": "UInt32", "totale": "UInt32", "quota_visite_pct": "Float64"})


def _month_rows(first: int = 202401, last: int = 202512) -> list[dict]:
    months = [y * 100 + m for y in (2024, 2025) for m in range(1, 13) if first <= y * 100 + m <= last]
    return [{"categoria": c, "anno_mese": ym, "ricavo": 1000.0 + 10 * i + 100 * k, "costo": 700.0 + i, "margine": 300.0 + 9 * i + 100 * k,
             "chili": 50.0 + i, "ordini": 5 + i, "margine_pct": 30.0} for k, c in enumerate(CATEGORIES) for i, ym in enumerate(months)]


def _agent_rows() -> tuple[list[dict], list[dict]]:
    names = [(a, f"Agent {a} {i}") for a in AREAS for i in range(6)]
    agents = [{"area": a, "agente": n, "ricavo": 100_000.0 + 20_000 * i, "ordini": 100 + i, "clienti": 20 + i, "ricavo_per_ordine": 900.0 + i}
              for i, (a, n) in enumerate(names)]
    crm = [{"area": a, "agente": n, "email": 10, "reclamo": 10, "sollecito": 10, "telefonata": 10, "visita": 5 + i % 10,
            "totale": 45 + i % 10, "quota_visite_pct": 20.0} for i, (a, n) in enumerate(names)]
    return agents, crm


@pytest.mark.parametrize("definition", [seed.margin_review(MONTHS), seed.agent_scorecard(AGENTS, CRM)],
                         ids=["margin review", "agent scorecard"])
def test_the_engine_finds_no_mistake_in_the_sample_report(definition):
    columns = {d["name"]: {f["name"]: f["type"] for f in d["fields"]} for d in definition["datasets"]}
    assert [p.as_dict() for p in rd.check(definition, columns)] == []


def _render(tmp_path, chart_service, definition, datasets, params):  # noqa: F811 — the fixture
    datasets = {name: {"rows": rows} for name, rows in datasets.items()}
    return render.render(definition, datasets, params, "pdf", tmp_path, title="Sample", limits=render.Limits(chart_url=chart_service[0]), now=NOW)


def test_the_margin_review_renders_on_its_two_datasets_with_nothing_past_an_edge(tmp_path, chart_service, both_ways):  # noqa: F811
    definition = seed.margin_review(MONTHS)
    done = _render(tmp_path, chart_service, definition, {"months": _month_rows(202501, 202512), "history": _month_rows()},
                   {"category": None, "first_month": 202501, "last_month": 202512})
    assert done.pages >= 2 and done.warnings == []


def test_the_margin_review_says_nothing_of_a_year_it_cannot_compare_whole(tmp_path, chart_service, both_ways):  # noqa: F811
    definition = seed.margin_review(MONTHS)
    done = _render(tmp_path, chart_service, definition, {"months": _month_rows(202401, 202412), "history": _month_rows()},
                   {"category": None, "first_month": 202401, "last_month": 202412})
    assert done.warnings == []


def test_the_agent_scorecard_renders_on_its_two_datasets_with_nothing_past_an_edge(tmp_path, chart_service, both_ways):  # noqa: F811
    agents, crm = _agent_rows()
    done = _render(tmp_path, chart_service, seed.agent_scorecard(AGENTS, CRM), {"agents": agents, "crm": crm}, {"area": None, "agent": None})
    assert done.pages >= 1 and done.warnings == []


def test_the_agent_scorecard_of_one_agent_renders_too(tmp_path, chart_service, both_ways):  # noqa: F811
    agents, crm = _agent_rows()
    one = agents[:1]
    done = _render(tmp_path, chart_service, seed.agent_scorecard(AGENTS, CRM), {"agents": one, "crm": crm[:1]},
                   {"area": one[0]["area"], "agent": one[0]["agente"]})
    assert done.pages == 1 and done.warnings == []
