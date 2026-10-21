"""The dbt export is honest about Script nodes: Python or R runs on Tabularia's cluster,
not in dbt. An Output that reads what a script writes, however far down (and the
datasource another flow reads from it), refuses the export, saying which; a script that
feeds no exported Output is left out, with a note."""
import json

import pytest

from app.services import dbt_export as d
from app.services.messaggi_dbt import MESSAGGI
from tests.conftest import make_datasource, make_flow
from tests.test_dbt_export import _edge, _nodo_src, _op, scena  # noqa: F401 — the shared scene

SCRIPT = {"id": "py", "type": "script", "data": {"label": "Score", "environmentId": 1, "entrypoint": "main.py",
                                                 "files": [{"path": "main.py", "content": "print(1)"}]}}


def _uscita(nid, scena, name):
    return {"id": nid, "type": "output", "data": {"destType": "datasource", "name": name, "projectId": scena.cartella.id}}


def test_an_output_after_a_script_refuses_the_export(session, scena):
    definition = {"nodes": [_nodo_src("src", scena.ordini.id), SCRIPT, _op("f", "filter", {"column": "x", "operator": "eq", "value": 1}),
                            _uscita("o", scena, "Scored")],
                  "edges": [_edge("src", "py"), _edge("py", "f"), _edge("f", "o")]}
    flow = make_flow(session, name="Scoring", project_id=scena.cartella.id, definition=json.dumps(definition))
    with pytest.raises(d.DbtExportError) as e:
        d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert e.value.code == "script_not_exportable"
    assert e.value.params == {"flow": "Scoring", "output": "Scored", "script": "Score"}
    assert set(MESSAGGI["script_not_exportable"]) == {"en", "it", "de", "es", "fr"}


def test_a_flow_reading_a_datasource_a_script_made_refuses_too(session, scena):
    made = {"nodes": [_nodo_src("src", scena.ordini.id), SCRIPT, _uscita("o", scena, "Scored")],
            "edges": [_edge("src", "py"), _edge("py", "o")]}
    upstream = make_flow(session, name="Scoring", project_id=scena.cartella.id, definition=json.dumps(made))
    scored = make_datasource(session, name="Scored", project_id=scena.cartella.id, key="datasets/9/v1.parquet", kind="flow",
                             columns="[]", flow_id=upstream.id)
    reader = make_flow(session, name="Report", project_id=scena.cartella.id, definition=json.dumps({
        "nodes": [_nodo_src("s", scored.id), _uscita("r", scena, "Report")], "edges": [_edge("s", "r")]}))
    with pytest.raises(d.DbtExportError) as e:
        d.build_export_payload(session, reader, "data-prep", "duckdb")
    assert e.value.code == "script_not_exportable" and e.value.params["flow"] == "Scoring"


def test_a_script_no_exported_output_reads_is_left_out_with_a_note(session, scena):
    definition = {"nodes": [_nodo_src("src", scena.ordini.id), _uscita("o", scena, "Orders copy"), SCRIPT],
                  "edges": [_edge("src", "o")]}
    flow = make_flow(session, name="Copy and notify", project_id=scena.cartella.id, definition=json.dumps(definition))
    payload = d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert any("Script node «Score»" in n and "not part of this dbt project" in n for n in payload["notes"])
    assert payload["models"]


# ── the Container node: an image is not dbt either ───────────────────────────
CONTAINER = {"id": "img", "type": "container", "data": {"label": "Forecast", "registryId": 1,
                                                        "repository": "team/forecast", "tag": "v1"}}


def test_an_output_after_a_container_refuses_the_export(session, scena):
    definition = {"nodes": [_nodo_src("src", scena.ordini.id), CONTAINER, _uscita("o", scena, "Forecast")],
                  "edges": [_edge("src", "img"), _edge("img", "o")]}
    flow = make_flow(session, name="Forecasting", project_id=scena.cartella.id, definition=json.dumps(definition))
    with pytest.raises(d.DbtExportError) as e:
        d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert e.value.code == "container_not_exportable"
    assert e.value.params == {"flow": "Forecasting", "output": "Forecast", "container": "Forecast"}
    assert set(MESSAGGI["container_not_exportable"]) == {"en", "it", "de", "es", "fr"}


def test_a_container_no_exported_output_reads_is_left_out_with_a_note(session, scena):
    definition = {"nodes": [_nodo_src("src", scena.ordini.id), _uscita("o", scena, "Orders copy"), CONTAINER],
                  "edges": [_edge("src", "o")]}
    flow = make_flow(session, name="Copy and train", project_id=scena.cartella.id, definition=json.dumps(definition))
    payload = d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert any("Container node «Forecast» (an image)" in n for n in payload["notes"])
