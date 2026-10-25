"""The dbt export is honest about Script nodes: Python or R runs on Tabularia's cluster,
not in dbt. An Output that reads what a script writes, however far down (and the
datasource another flow reads from it), refuses the export, saying which; a script that
feeds no exported Output is left out, with a note."""
import json

import pytest

from app.services import dbt_export as d
from app.services.dbt_messages import MESSAGES
from tests.conftest import make_datasource, make_flow
from tests.test_dbt_export import _edge, _src_node, _op, scene  # noqa: F401 — the shared scene

SCRIPT = {"id": "py", "type": "script", "data": {"label": "Score", "environmentId": 1, "entrypoint": "main.py",
                                                 "files": [{"path": "main.py", "content": "print(1)"}]}}


def _output(nid, scene, name):
    return {"id": nid, "type": "output", "data": {"destType": "datasource", "name": name, "projectId": scene.folder.id}}


def test_an_output_after_a_script_refuses_the_export(session, scene):
    definition = {"nodes": [_src_node("src", scene.orders.id), SCRIPT, _op("f", "filter", {"column": "x", "operator": "eq", "value": 1}),
                            _output("o", scene, "Scored")],
                  "edges": [_edge("src", "py"), _edge("py", "f"), _edge("f", "o")]}
    flow = make_flow(session, name="Scoring", project_id=scene.folder.id, definition=json.dumps(definition))
    with pytest.raises(d.DbtExportError) as e:
        d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert e.value.code == "script_not_exportable"
    assert e.value.params == {"flow": "Scoring", "output": "Scored", "script": "Score"}
    assert set(MESSAGES["script_not_exportable"]) == {"en", "it", "de", "es", "fr"}


def test_a_flow_reading_a_datasource_a_script_made_refuses_too(session, scene):
    made = {"nodes": [_src_node("src", scene.orders.id), SCRIPT, _output("o", scene, "Scored")],
            "edges": [_edge("src", "py"), _edge("py", "o")]}
    upstream = make_flow(session, name="Scoring", project_id=scene.folder.id, definition=json.dumps(made))
    scored = make_datasource(session, name="Scored", project_id=scene.folder.id, key="datasets/9/v1.parquet", kind="flow",
                             columns="[]", flow_id=upstream.id)
    reader = make_flow(session, name="Report", project_id=scene.folder.id, definition=json.dumps({
        "nodes": [_src_node("s", scored.id), _output("r", scene, "Report")], "edges": [_edge("s", "r")]}))
    with pytest.raises(d.DbtExportError) as e:
        d.build_export_payload(session, reader, "data-prep", "duckdb")
    assert e.value.code == "script_not_exportable" and e.value.params["flow"] == "Scoring"


def test_a_script_no_exported_output_reads_is_left_out_with_a_note(session, scene):
    definition = {"nodes": [_src_node("src", scene.orders.id), _output("o", scene, "Orders copy"), SCRIPT],
                  "edges": [_edge("src", "o")]}
    flow = make_flow(session, name="Copy and notify", project_id=scene.folder.id, definition=json.dumps(definition))
    payload = d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert any("Script node «Score»" in n and "not part of this dbt project" in n for n in payload["notes"])
    assert payload["models"]


def test_an_email_after_a_script_does_not_refuse_the_export(session, scene):
    """An email has no model: what it sends may come from a script, the export goes on."""
    email = {"id": "m", "type": "output", "data": {"destType": "email", "connectionId": 1, "emailTo": ["a@b.it"]}}
    definition = {"nodes": [_src_node("src", scene.orders.id), _output("o", scene, "Orders copy"), SCRIPT, email],
                  "edges": [_edge("src", "o"), _edge("src", "py"), _edge("py", "m")]}
    flow = make_flow(session, name="Copy and mail", project_id=scene.folder.id, definition=json.dumps(definition))
    payload = d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert payload["models"]
    assert any("Script node «Score»" in n for n in payload["notes"])


# ── the Container node: an image is not dbt either ───────────────────────────
CONTAINER = {"id": "img", "type": "container", "data": {"label": "Forecast", "registryId": 1,
                                                        "repository": "team/forecast", "tag": "v1"}}


def test_an_output_after_a_container_refuses_the_export(session, scene):
    definition = {"nodes": [_src_node("src", scene.orders.id), CONTAINER, _output("o", scene, "Forecast")],
                  "edges": [_edge("src", "img"), _edge("img", "o")]}
    flow = make_flow(session, name="Forecasting", project_id=scene.folder.id, definition=json.dumps(definition))
    with pytest.raises(d.DbtExportError) as e:
        d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert e.value.code == "container_not_exportable"
    assert e.value.params == {"flow": "Forecasting", "output": "Forecast", "container": "Forecast"}
    assert set(MESSAGES["container_not_exportable"]) == {"en", "it", "de", "es", "fr"}


def test_a_container_no_exported_output_reads_is_left_out_with_a_note(session, scene):
    definition = {"nodes": [_src_node("src", scene.orders.id), _output("o", scene, "Orders copy"), CONTAINER],
                  "edges": [_edge("src", "o")]}
    flow = make_flow(session, name="Copy and train", project_id=scene.folder.id, definition=json.dumps(definition))
    payload = d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert any("Container node «Forecast» (an image)" in n for n in payload["notes"])
