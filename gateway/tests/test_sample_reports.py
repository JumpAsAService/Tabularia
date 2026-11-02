"""The reports of the sample data (infrastructure/sampledb/seed_tabularia.py), against the rules of a save: what
the seed sends is what the gateway takes. The engine's half, the expressions, is backend/tests/test_sample_reports.py."""
import importlib.util
from pathlib import Path

import pytest

from app.services import report_definition as rd


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


def _table(id_: int, names: dict[str, str]) -> dict:
    return {"id": id_, "name": str(id_), "project_id": 3, "columns": [{"name": k, "dtype": v} for k, v in names.items()]}


MONTHS = _table(12, {"categoria": "String", "anno_mese": "Int32", "ricavo": "Float64", "costo": "Float64", "margine": "Float64",
                     "chili": "Float64", "ordini": "UInt32", "margine_pct": "Float64"})
AGENTS = _table(18, {"area": "String", "agente": "String", "ricavo": "Float64", "ordini": "UInt32", "clienti": "UInt32",
                     "ricavo_per_ordine": "Float64"})
CRM = _table(15, {"area": "String", "agente": "String", "email": "UInt32", "reclamo": "UInt32", "sollecito": "UInt32",
                  "telefonata": "UInt32", "visita": "UInt32", "totale": "UInt32", "quota_visite_pct": "Float64"})


@pytest.mark.parametrize("definition", [seed.margin_review(MONTHS), seed.agent_scorecard(AGENTS, CRM)],
                         ids=["margin review", "agent scorecard"])
def test_the_gateway_takes_the_sample_report_as_the_seed_writes_it(definition):
    assert rd.check(definition)["v"] == 1
