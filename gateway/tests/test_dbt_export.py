"""The dbt export payload the gateway prepares for the engine: one model per Output with
the right materialization, the shared steps as referenced intermediate models, the
sources classified (table → source, query → model, another flow's output → its
models, file → seed), schema and contract of the published datasource → columns and
tests, and the clear refusals."""
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.models import Connection
from app.models.contract import DataContract
from app.services import dbt_export as d
from tests.conftest import make_datasource, make_flow, make_project, make_user


def _src_node(nid, ds_id):
    return {"id": nid, "type": "source", "data": {"datasourceId": ds_id}}


def _op(nid, op_type, params):
    return {"id": nid, "type": "operation", "data": {"opType": op_type, "params": params}}


def _edge(a, b, handle="left"):
    return {"source": a, "target": b, "targetHandle": handle}


@pytest.fixture
def scene(session):
    boss = make_user(session, email="capo@x.it", is_superuser=True)
    folder = make_project(session, name="Flows", owner_id=boss.id)
    pg = Connection(name="gestionale", db_type="postgresql", project_id=folder.id, owner_id=boss.id, host="db.x.it", port=None, database="erp", db_schema="")
    ch = Connection(name="crm", db_type="clickhouse", project_id=folder.id, owner_id=boss.id, host="ch.x.it", port=8123, database="analytics", db_schema="")
    session.add(pg); session.add(ch); session.commit()
    orders = make_datasource(session, name="orders", project_id=folder.id, key="datasets/1/v3.parquet", kind="database", connection_id=pg.id,
                             source_type="table", source_ref="vendite.ordini", description="Every order",
                             columns=json.dumps([{"name": "id", "dtype": "Int64"}, {"name": "canale", "dtype": "String"}, {"name": "importo", "dtype": "Float64"}]),
                             column_descriptions=json.dumps({"canale": "Sales channel"}))
    customers = make_datasource(session, name="customers", project_id=folder.id, key="datasets/2/v1.parquet", kind="database", connection_id=pg.id,
                              source_type="sql", source_ref="SELECT id, nome FROM crm.clienti WHERE attivo;",
                              columns=json.dumps([{"name": "id", "dtype": "Int64"}, {"name": "nome", "dtype": "String"}]))
    return SimpleNamespace(boss=boss, folder=folder, pg=pg, ch=ch, orders=orders, customers=customers)


def _flow_with_two_outputs(session, scene, **extra):
    """orders → filter → compute → [group_by → Output datasource «Margin»] and
    [→ Output table report.righe in append]: filter+compute are shared."""
    definition = {"nodes": [
        _src_node("src", scene.orders.id),
        _op("f", "filter", {"column": "canale", "operator": "eq", "value": "web"}),
        _op("c", "compute", {"columns": [{"name": "netto", "expr": "importo * 0.8"}]}),
        _op("g", "group_by", {"by": ["canale"], "aggregations": [{"column": "netto", "func": "sum", "alias": "tot"}]}),
        {"id": "o1", "type": "output", "data": {"destType": "datasource", "name": "Margin", "projectId": scene.folder.id, "overwrite": True}},
        {"id": "o2", "type": "output", "data": {"destType": "database", "connectionId": scene.pg.id, "table": "report.righe", "mode": "append", "postSql": "ANALYZE report.righe"}},
        {"id": "o3", "type": "output", "data": {"destType": "email", "connectionId": 99, "emailTo": ["a@x.it"]}},
    ], "edges": [_edge("src", "f"), _edge("f", "c"), _edge("c", "g"), _edge("g", "o1"), _edge("c", "o2"), _edge("c", "o3")]}
    return make_flow(session, name="Margin flow", project_id=scene.folder.id, definition=json.dumps(definition), description="The margin", **extra)


def test_two_outputs_share_the_upstream_steps_in_an_intermediate_model(session, scene):
    flow = _flow_with_two_outputs(session, scene)
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    names = [m["name"] for m in p["models"]]
    assert names == ["int_c", "margin", "righe"]                     # the intermediate before whoever uses it; no model for the email
    inter = p["models"][0]
    assert inter["materialized"] == "ephemeral" and inter["source"] == {"key": scene.orders.key}
    assert [o["type"] for o in inter["operations"]] == ["filter", "compute"]
    margin, rows = p["models"][1], p["models"][2]
    assert margin["source"] == {"model": "int_c"} and [o["type"] for o in margin["operations"]] == ["group_by"]
    assert margin["materialized"] == "table" and margin["publishes"] == ("Margin", scene.folder.id)
    assert rows["source"] == {"model": "int_c"} and rows["operations"] == [] and rows["materialized"] == "incremental"
    assert "schema" not in rows                                      # in federation the table stays in the DuckDB file
    assert any("post-SQL" in n for n in p["notes"])
    assert p["mode"] == "duckdb" and p["attachments"][0]["alias"] == f"db_{scene.pg.id}"
    assert p["source_map"][scene.orders.key]["columns"] == ["id", "canale", "importo"]
    tab = p["sources"][0]["tables"][0]
    assert tab["name"] == "ordini" and tab["description"] == "Datasource «orders»: Every order"
    assert {"name": "canale", "description": "Sales channel"} in tab["columns"]


def test_in_native_the_destination_table_is_schema_and_alias_of_the_model(session, scene):
    flow = _flow_with_two_outputs(session, scene)
    p = d.build_export_payload(session, flow, "data-prep", "native")
    rows = next(m for m in p["models"] if m["name"] == "righe")
    assert rows["schema"] == "report" and rows["alias"] == "righe" and rows["materialized"] == "incremental"
    assert p["native"]["db_type"] == "postgresql" and p["native"]["conn"]["port"] == 5432
    assert p["source_map"][scene.orders.key]["ident"] == "__s_0"


def test_the_published_datasources_schema_and_contract_become_columns_and_tests(session, scene):
    flow = _flow_with_two_outputs(session, scene)
    published = make_datasource(session, name="Margin", project_id=scene.folder.id, key="datasets/9/v1.parquet", kind="flow", flow_id=flow.id,
                                 columns=json.dumps([{"name": "canale", "dtype": "String"}, {"name": "tot", "dtype": "Float64"}]),
                                 column_descriptions=json.dumps({"tot": "Net revenue"}))
    rules = [{"id": "r1", "kind": "not_null", "column": "canale", "severity": "error"},
              {"id": "r0", "kind": "unique", "columns": ["canale"], "severity": "error"},
              {"id": "r7", "kind": "unique", "columns": ["canale", "tot"], "severity": "warning"},
              {"id": "r2", "kind": "accepted_values", "column": "canale", "values": ["web", "shop"], "severity": "warning"},
              {"id": "r3", "kind": "range", "column": "tot", "min": 0, "severity": "error"},
              {"id": "r4", "kind": "row_count", "min": 1, "max": 1000, "severity": "warning"},
              {"id": "r5", "kind": "freshness", "max_age_hours": 24, "severity": "error"},
              {"id": "r6", "kind": "column", "column": "tot", "dtype": "Float64", "severity": "error"}]
    session.add(DataContract(datasource_id=published.id, document=json.dumps({"description": "what we promise", "rules": rules}), enabled=True))
    session.commit()
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    margin = next(m for m in p["models"] if m["name"] == "margin")
    columns = {c["name"]: c for c in margin["columns"]}
    assert columns["canale"]["tests"] == [{"kind": "not_null", "severity": "error"}, {"kind": "unique", "severity": "error"}, {"kind": "accepted_values", "severity": "warn", "values": ["web", "shop"]}]
    assert columns["tot"]["description"] == "Net revenue Contract: declared as Float64."
    assert margin["tests"] == [{"id": "r7", "kind": "unique_combination", "columns": ["canale", "tot"], "severity": "warn"},
                               {"id": "r3", "kind": "range", "column": "tot", "min": 0, "severity": "error"},
                               {"id": "r4", "kind": "row_count", "min": 1, "max": 1000, "severity": "warn"}]
    assert margin["description"].endswith("Contract: what we promise")
    assert any("freshness" in n for n in p["notes"])


def test_a_contract_off_produces_no_tests(session, scene):
    flow = _flow_with_two_outputs(session, scene)
    published = make_datasource(session, name="Margin", project_id=scene.folder.id, key="datasets/9/v1.parquet", kind="flow", flow_id=flow.id,
                                 columns=json.dumps([{"name": "canale", "dtype": "String"}]))
    session.add(DataContract(datasource_id=published.id, document=json.dumps({"rules": [{"id": "r1", "kind": "not_null", "column": "canale", "severity": "error"}]}), enabled=False))
    session.commit()
    margin = next(m for m in d.build_export_payload(session, flow, "data-prep", "duckdb")["models"] if m["name"] == "margin")
    assert margin["columns"] == [{"name": "canale", "description": "", "tests": []}] and margin["tests"] == []


def test_a_query_datasource_is_an_ephemeral_model_with_the_query(session, scene):
    definition = {"nodes": [_src_node("s", scene.customers.id), _op("l", "limit", {"n": 10}),
                             {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Top", "projectId": scene.folder.id}}],
                   "edges": [_edge("s", "l"), _edge("l", "o")]}
    flow = make_flow(session, name="Top customers", project_id=scene.folder.id, definition=json.dumps(definition))
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    src, top = p["models"]
    assert src["name"] == "src_customers" and src["kind"] == "raw" and src["materialized"] == "ephemeral"
    assert src["raw_sql"] == f"SELECT * FROM postgres_query('db_{scene.pg.id}', 'SELECT id, nome FROM crm.clienti WHERE attivo')"
    assert [c["name"] for c in src["columns"]] == ["id", "nome"]
    assert top["source"] == {"model": "src_customers"}
    assert p["attachments"][0]["alias"] == f"db_{scene.pg.id}" and p["sources"] == []     # the ATTACH is needed even without tables
    n = d.build_export_payload(session, flow, "data-prep", "native")
    assert n["models"][0]["raw_sql"] == "SELECT id, nome FROM crm.clienti WHERE attivo" and any("source()" in x for x in n["notes"])


def test_another_flows_output_brings_its_models_in(session, scene):
    monte = _flow_with_two_outputs(session, scene)
    margin_ds = make_datasource(session, name="Margin", project_id=scene.folder.id, key="datasets/9/v1.parquet", kind="flow", flow_id=monte.id,
                                columns=json.dumps([{"name": "canale", "dtype": "String"}, {"name": "tot", "dtype": "Float64"}]))
    definition = {"nodes": [_src_node("s", margin_ds.id), _op("f", "filter", {"column": "tot", "operator": "gt", "value": 0}),
                             {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Positive margin", "projectId": scene.folder.id}}],
                   "edges": [_edge("s", "f"), _edge("f", "o")]}
    downstream = make_flow(session, name="Downstream", project_id=scene.folder.id, definition=json.dumps(definition))
    p = d.build_export_payload(session, downstream, "data-prep", "duckdb")
    names = [m["name"] for m in p["models"]]
    # only the branch read here, upstream first (decision of 2026-10-16, finding #37): the model publishing
    # «Margin» with its own steps. The upstream flow's other output (the append into report.righe) stays
    # out, so the shared steps are no longer an intermediate model: exporting a flow never writes into
    # another flow's tables. This test asserted the old behaviour, all the upstream outputs.
    assert names == ["margin", "positive_margin"]
    assert [o["type"] for o in p["models"][0]["operations"]] == ["filter", "compute", "group_by"]
    assert p["models"][-1]["source"] == {"model": "margin"}
    assert p["source_map"] and scene.orders.key in p["source_map"]


def test_a_cycle_between_flows_is_refused(session, scene):
    a = make_flow(session, name="A", project_id=scene.folder.id, definition="{}")
    b_out = make_datasource(session, name="B out", project_id=scene.folder.id, key="datasets/b.parquet", kind="flow", flow_id=None)
    a_out = make_datasource(session, name="A out", project_id=scene.folder.id, key="datasets/a.parquet", kind="flow", flow_id=a.id)
    b = make_flow(session, name="B", project_id=scene.folder.id, definition=json.dumps({"nodes": [_src_node("s", a_out.id), {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "B out", "projectId": scene.folder.id}}], "edges": [_edge("s", "o")]}))
    b_out.flow_id = b.id; session.add(b_out)
    a.definition = json.dumps({"nodes": [_src_node("s", b_out.id), {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "A out", "projectId": scene.folder.id}}], "edges": [_edge("s", "o")]})
    session.add(a); session.commit()
    with pytest.raises(d.DbtExportError, match="cycle"):
        d.build_export_payload(session, a, "data-prep", "duckdb")


def test_an_uploaded_file_or_a_datasource_without_origin_become_seeds(session, scene):
    imported = make_datasource(session, name="price list", project_id=scene.folder.id, key="datasets/7/v1.parquet", kind="flow", flow_id=None,
                                columns=json.dumps([{"name": "sku", "dtype": "String"}]))
    definition = {"nodes": [
        _src_node("s1", imported.id),
        {"id": "s2", "type": "source", "data": {"parquetKey": "datasets/5bcf7e.parquet", "bucket": "data-prep", "filename": "Listino 2026.csv"}},
        _op("j", "join", {"how": "inner", "on": "sku"}),
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Joined", "projectId": scene.folder.id}},
    ], "edges": [_edge("s1", "j"), _edge("s2", "j", "right"), _edge("j", "o")]}
    flow = make_flow(session, name="Seeds", project_id=scene.folder.id, definition=json.dumps(definition))
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert [s["name"] for s in p["seeds"]] == ["seed_price_list", "seed_listino_2026"]   # the uploaded file takes the file's name, not the key's
    assert p["seeds"][1] == {"name": "seed_listino_2026", "bucket": "data-prep", "key": "datasets/5bcf7e.parquet", "datasource_id": None,
                             "description": "File Listino 2026.csv, exported as a CSV seed: the data is a snapshot, not a live source.", "columns": []}
    assert p["models"][0]["source"] == {"seed": "seed_price_list"} and p["attachments"] == []
    right = p["models"][0]["operations"][0]["params"]["right"]
    assert right["source"] == {"seed": "seed_listino_2026"}            # in the join's right branch too the file is the seed
    with pytest.raises(d.DbtExportError, match="only files"):
        d.build_export_payload(session, flow, "data-prep", "native")


def test_the_refusals_are_clear(session, scene):
    crm = make_datasource(session, name="crm_activities", project_id=scene.folder.id, key="datasets/5/v1.parquet", kind="database", connection_id=scene.ch.id,
                          source_type="table", source_ref="crm.activities", columns="[]")
    definition = {"nodes": [_src_node("s1", scene.orders.id), _src_node("s2", crm.id), _op("u", "union", {}),
                             {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "All", "projectId": scene.folder.id}}],
                   "edges": [_edge("s1", "u"), _edge("s2", "u", "right"), _edge("u", "o")]}
    flow = make_flow(session, name="Mixed", project_id=scene.folder.id, definition=json.dumps(definition))
    with pytest.raises(d.DbtExportError, match="cannot federate"):
        d.build_export_payload(session, flow, "data-prep", "duckdb")
    with pytest.raises(d.DbtExportError, match="SAME connection"):
        d.build_export_payload(session, flow, "data-prep", "native")
    email_only = make_flow(session, name="Mail", project_id=scene.folder.id, definition=json.dumps({"nodes": [_src_node("s", scene.orders.id), {"id": "o", "type": "output", "data": {"destType": "email", "connectionId": 1}}], "edges": [_edge("s", "o")]}))
    with pytest.raises(d.DbtExportError, match="only email"):
        d.build_export_payload(session, email_only, "data-prep", "duckdb")
    with pytest.raises(d.DbtExportError, match="unknown target"):
        d.build_export_payload(session, flow, "data-prep", "bigquery")


# ── who may export: administrators only ───────────────────────────────────────
def _request():
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "client": ("10.0.0.1", 1234), "query_string": b""})


@pytest.mark.anyio
async def test_the_dbt_export_is_for_administrators_only(session, scene, fake_engine):
    from fastapi import HTTPException

    from app.models import Group, UserGroupLink
    from app.routes import flows as flows_routes
    from tests.conftest import make_permission

    flow = _flow_with_two_outputs(session, scene)
    reader = make_user(session, email="lettore@x.it")
    editor = make_user(session, email="editore@x.it")
    observer = make_user(session, email="occhio@x.it", is_observer=True)
    for u, cap in ((reader, "view"), (editor, "manage"), (observer, "view")):
        make_permission(session, user_id=u.id, project_id=scene.folder.id, capability=cap)
    for u in (reader, editor, observer):
        with pytest.raises(HTTPException) as e:
            await flows_routes.export_flow_dbt(flow.id, _request(), "duckdb", u, session)
        assert e.value.status_code == 403 and "administrators" in e.value.detail, u.email

    # personal admin and admin by group (the data team): they pass
    answer = await flows_routes.export_flow_dbt(flow.id, _request(), "duckdb", scene.boss, session)
    assert answer.status_code == 200 and answer.media_type == "application/zip"
    team = Group(name="data team", is_admin=True); session.add(team); session.commit(); session.refresh(team)
    data = make_user(session, email="dati@x.it")
    session.add(UserGroupLink(user_id=data.id, group_id=team.id)); session.commit()
    answer = await flows_routes.export_flow_dbt(flow.id, _request(), "native", data, session)
    assert answer.status_code == 200
    answer = await flows_routes.export_flow_dbt(flow.id, _request(), "clickhouse", data, session)
    assert answer.status_code == 200
    with pytest.raises(HTTPException) as e:
        await flows_routes.export_flow_dbt(flow.id, _request(), "clickhouse", observer, session)
    assert e.value.status_code == 403


def test_a_sharepoint_datasource_becomes_a_seed_and_the_sources_carry_the_types(session, scene):
    sp = Connection(name="sp", db_type="sharepoint", project_id=scene.folder.id, owner_id=scene.boss.id, host="contoso.sharepoint.com", database="", db_schema="")
    session.add(sp); session.commit()
    excel = make_datasource(session, name="budget 2026", project_id=scene.folder.id, key="datasets/8/v1.parquet", kind="database", connection_id=sp.id,
                            source_type="sharepoint", source_ref=json.dumps({"path": "/Finanza/budget.xlsx", "sheet": "2026"}),
                            columns=json.dumps([{"name": "voce", "dtype": "String"}, {"name": "importo", "dtype": "Float64"}]))
    definition = {"nodes": [_src_node("s1", scene.orders.id), _src_node("s2", excel.id), _op("u", "union", {}),
                             {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Con budget", "projectId": scene.folder.id}}],
                   "edges": [_edge("s1", "u"), _edge("s2", "u", "right"), _edge("u", "o")]}
    flow = make_flow(session, name="Budget", project_id=scene.folder.id, definition=json.dumps(definition))
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert [s["name"] for s in p["seeds"]] == ["seed_budget_2026"]                       # not a «table» made of JSON
    assert p["models"][0]["operations"][0]["params"]["right"]["source"] == {"seed": "seed_budget_2026"}
    assert all(t["name"] != json.dumps({"path": "/Finanza/budget.xlsx", "sheet": "2026"}) for s in p["sources"] for t in s["tables"])
    assert p["source_map"][scene.orders.key]["types"] == {"id": "Int64", "canale": "String", "importo": "Float64"}


def test_an_sftp_folder_becomes_a_seed_and_an_sftp_output_a_table(session, scene):
    """Files on a server, like SharePoint's: no database to read them from in dbt, so a
    seed. A file written to a server: in dbt a table, to export from the warehouse."""
    ftp = Connection(name="drop", db_type="sftp", project_id=scene.folder.id, owner_id=scene.boss.id, host="sftp.example.com", database="", db_schema="")
    session.add(ftp); session.commit()
    files = make_datasource(session, name="vendite sftp", project_id=scene.folder.id, key="datasets/81/v1.parquet", kind="database", connection_id=ftp.id,
                            source_type="sftp", source_ref=json.dumps({"folder": "/in", "pattern": "vendite_*.csv"}),
                            columns=json.dumps([{"name": "id", "dtype": "Int64"}]))
    definition = {"nodes": [_src_node("s1", files.id),
                             {"id": "o", "type": "output", "data": {"destType": "sftp", "connectionId": ftp.id, "sftpFolder": "/out",
                                                                     "sftpFileName": "export_{date}", "sftpFormat": "csv"}}],
                   "edges": [_edge("s1", "o")]}
    flow = make_flow(session, name="Sftp", project_id=scene.folder.id, definition=json.dumps(definition))
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert [s["name"] for s in p["seeds"]] == ["seed_vendite_sftp"]
    (m,) = p["models"]
    assert m["materialized"] == "table" and m["name"] == "export" and "export_{date}" in m["description"]


def test_an_s3_folder_becomes_a_seed_and_a_dated_s3_key_a_model_named_by_its_text(session, scene):
    lake = Connection(name="lake", db_type="s3", project_id=scene.folder.id, owner_id=scene.boss.id,
                      host="http://minio:9000", database="lake", db_schema="")
    session.add(lake); session.commit()
    files = make_datasource(session, name="vendite s3", project_id=scene.folder.id, key="datasets/83/v1.parquet",
                            kind="database", connection_id=lake.id, source_type="s3",
                            source_ref=json.dumps({"bucket": "", "folder": "in", "pattern": "*.csv"}),
                            columns=json.dumps([{"name": "id", "dtype": "Int64"}]))
    definition = {"nodes": [_src_node("s1", files.id),
                             {"id": "o", "type": "output", "data": {"destType": "s3", "connectionId": lake.id,
                                                                     "s3Key": "exp/vendite_{date}.parquet"}}],
                   "edges": [_edge("s1", "o")]}
    flow = make_flow(session, name="S3", project_id=scene.folder.id, definition=json.dumps(definition))
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert [s["name"] for s in p["seeds"]] == ["seed_vendite_s3"]
    (m,) = p["models"]
    assert m["name"] == "vendite" and "vendite_{date}" in m["description"]


def test_an_api_datasource_becomes_a_seed(session, scene):
    """An API has no database to read from in dbt: the snapshot is a seed, as for files."""
    broker = Connection(name="broker", db_type="api", project_id=scene.folder.id, owner_id=scene.boss.id,
                        host="https://api.example.com/v1", database="", db_schema="")
    session.add(broker); session.commit()
    fx = make_datasource(session, name="cambi bce", project_id=scene.folder.id, key="datasets/82/v1.parquet", kind="database",
                         connection_id=broker.id, source_type="api", source_ref=json.dumps({"method": "GET", "path": "/rates"}),
                         columns=json.dumps([{"name": "_key", "dtype": "String"}, {"name": "USD", "dtype": "Float64"}]))
    definition = {"nodes": [_src_node("s1", fx.id), {"id": "o", "type": "output", "data": {"name": "out"}}],
                   "edges": [_edge("s1", "o")]}
    flow = make_flow(session, name="Fx", project_id=scene.folder.id, definition=json.dumps(definition))
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert [s["name"] for s in p["seeds"]] == ["seed_cambi_bce"]


def test_every_model_carries_where_it_comes_from(session, scene):
    """Traceability: flow, version (and when it was saved), folder, author, output and node;
    the tags to select them in dbt (`tag:tabularia`). Who exports goes in the README only:
    the models stay the same from one export to the next."""
    from app.models import FlowVersion

    autrice = make_user(session, email="anna@x.it")
    flow = _flow_with_two_outputs(session, scene, owner_id=autrice.id)
    for v in (1, 2, 3):
        session.add(FlowVersion(flow_id=flow.id, version=v, definition=flow.definition))
    session.commit()
    p = d.build_export_payload(session, flow, "data-prep", "duckdb", exported_by="capo@x.it")
    inter, margin, rows = p["models"]
    common = {"flow": "Margin flow", "flow_id": flow.id, "flow_version": 3, "folder": "Flows", "owner": "anna@x.it"}
    assert p["exported_by"] == "capo@x.it"
    for m in (inter, margin, rows):
        assert common.items() <= m["meta"].items() and m["meta"]["version_saved_at"].endswith("UTC")
        assert not any(k.startswith("exported") for k in m["meta"])
        assert m["tags"] == ["tabularia", "margin_flow"]
    assert margin["meta"]["output"] == "output «Margin» (datasource)" and margin["meta"]["node"] == "o1"
    assert rows["meta"]["output"] == "output table `report.righe` (append)" and rows["meta"]["node"] == "o2"
    assert inter["meta"]["output"].startswith("shared steps up to the node") and "node" not in inter["meta"]
    assert "_uscita" not in margin and "_uscita" not in rows


def test_a_datasources_query_carries_the_datasource_and_an_upstream_flow_its_own(session, scene):
    a_monte = make_flow(session, name="Upstream", project_id=scene.folder.id, definition=json.dumps({"nodes": [
        _src_node("s", scene.customers.id),
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Clean customers", "projectId": scene.folder.id, "overwrite": True}},
    ], "edges": [_edge("s", "o")]}))
    published = make_datasource(session, name="Clean customers", project_id=scene.folder.id, key="datasets/9/v1.parquet", kind="flow", flow_id=a_monte.id,
                                 columns=json.dumps([{"name": "id", "dtype": "Int64"}]))
    flow = make_flow(session, name="Downstream", project_id=scene.folder.id, definition=json.dumps({"nodes": [
        _src_node("s", published.id),
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Final", "projectId": scene.folder.id, "overwrite": True}},
    ], "edges": [_edge("s", "o")]}))
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    by_name = {m["name"]: m for m in p["models"]}
    query = next(m for m in p["models"] if m["kind"] == "raw")
    assert query["meta"]["datasource"] == "customers" and query["meta"]["datasource_id"] == scene.customers.id and query["tags"] == ["tabularia"]
    assert by_name["clean_customers"]["meta"]["flow"] == "Upstream" and by_name["clean_customers"]["tags"] == ["tabularia", "upstream"]
    assert by_name["final"]["meta"]["flow"] == "Downstream" and by_name["final"]["meta"]["flow_version"] is None
    assert "exported_by" not in by_name["final"]["meta"] and p["exported_by"] is None


def test_the_clickhouse_target_accepts_several_connections_and_carries_what_the_engine_needs(session, scene):
    """ClickHouse reads Postgres/MySQL live: the sources may sit on several connections
    (unlike native), the queries must be translated, and the engine gets the connections
    to name the federated databases — without passwords."""
    my = Connection(name="negozio", db_type="mysql", project_id=scene.folder.id, owner_id=scene.boss.id, host="my.x.it", port=None, database="shop", db_schema="", username="web")
    session.add(my); session.commit()
    products = make_datasource(session, name="products", project_id=scene.folder.id, key="datasets/5/v1.parquet", kind="database", connection_id=my.id,
                               source_type="table", source_ref="prodotti", columns=json.dumps([{"name": "id", "dtype": "Int64"}, {"name": "canale", "dtype": "String"}]))
    flow = make_flow(session, name="Mix", project_id=scene.folder.id, definition=json.dumps({"nodes": [
        _src_node("a", scene.orders.id), _src_node("b", scene.customers.id), _src_node("c", products.id),
        _op("j", "join", {"how": "left", "on": ["id"]}),
        _op("u", "union", {"strategy": "by_name"}),
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Mix out", "projectId": scene.folder.id, "overwrite": True}},
    ], "edges": [_edge("a", "j"), _edge("b", "j", "right"), _edge("j", "u"), _edge("c", "u", "right"), _edge("u", "o")]}))
    with pytest.raises(d.DbtExportError, match="SAME connection"):
        d.build_export_payload(session, flow, "data-prep", "native")
    p = d.build_export_payload(session, flow, "data-prep", "clickhouse")
    assert p["mode"] == "clickhouse" and p["clickhouse"]["target_schema"] == "dbt_tabularia"
    conns = {c["id"]: c for c in p["clickhouse"]["connections"]}
    assert set(conns) == {scene.pg.id, my.id}
    assert conns[scene.pg.id]["port"] == 5432 and conns[my.id]["port"] == 3306 and conns[my.id]["pw_env"] == f"TABULARIA_DB_{my.id}_PASSWORD"
    assert not any("password" in k for c in conns.values() for k in c if k != "pw_env")
    sources = {(s["connection_id"], s["schema"]) for s in p["sources"]}
    assert sources == {(scene.pg.id, "vendite"), (my.id, "shop")}
    query = next(m for m in p["models"] if m["kind"] == "raw")
    assert query["translate"] == {"connection_id": scene.pg.id, "label": "customers"} and query["raw_sql"].startswith("SELECT id, nome FROM crm.clienti")
    assert not any("as written" in n for n in p["notes"])
    assert p["source_map"][scene.orders.key]["macro"] == "{{ source('db_%d_vendite', 'ordini') }}" % scene.pg.id


def test_in_the_other_targets_the_query_is_not_translated(session, scene):
    flow = make_flow(session, name="Q", project_id=scene.folder.id, definition=json.dumps({"nodes": [
        _src_node("b", scene.customers.id),
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Q out", "projectId": scene.folder.id, "overwrite": True}},
    ], "edges": [_edge("b", "o")]}))
    for target in ("duckdb", "native"):
        query = next(m for m in d.build_export_payload(session, flow, "data-prep", target)["models"] if m["kind"] == "raw")
        assert query["translate"] is None


# ── download options (the administrators' dialog) ───────────────────────────────
def _without_time(payload):
    for m in payload["models"]:
        (m.get("meta") or {}).pop("exported_at", None)
    return payload


def test_the_default_options_give_the_same_payload_as_the_download_without_options(session, scene):
    flow = _flow_with_two_outputs(session, scene)
    for target in ("duckdb", "native", "clickhouse"):
        without = _without_time(d.build_export_payload(session, flow, "data-prep", target, exported_by="a@x.it"))
        with_ = _without_time(d.build_export_payload(session, flow, "data-prep", target, exported_by="a@x.it", options=d.DbtOptions(target=target)))
        without.pop("layout", None); with_.pop("layout", None)
        assert without == with_, target


def test_prefix_materializations_tests_email_schema_and_source_names(session, scene):
    autrice = make_user(session, email="anna@x.it")
    flow = _flow_with_two_outputs(session, scene, owner_id=autrice.id)
    session.add(DataContract(datasource_id=make_datasource(session, name="Margin", project_id=scene.folder.id, key="datasets/9/v1.parquet",
                                                           kind="flow", flow_id=flow.id, columns=json.dumps([{"name": "canale", "dtype": "String"}])).id,
                             document=json.dumps({"rules": [{"id": "r1", "kind": "not_null", "column": "canale", "severity": "error"},
                                                            {"id": "r2", "kind": "column", "column": "canale", "dtype": "string", "severity": "error"}]}),
                             enabled=True))
    session.commit()
    o = d.DbtOptions(target="native", prefix="tab_", schema="analisi", tests=False, emails=False,
                     sources={f"{scene.pg.id}:vendite": {"name": "erp", "declared": True}},
                     materializations={f"{flow.id}:o1": "view", f"{flow.id}:o2": "table", "999:x": "view"})
    p = d.build_export_payload(session, flow, "data-prep", exported_by="capo@x.it", options=o)
    assert [m["name"] for m in p["models"]] == ["tab_int_c", "tab_margin", "tab_righe"]
    assert p["models"][1]["source"] == {"model": "tab_int_c"}
    assert p["models"][1]["materialized"] == "view" and p["models"][2]["materialized"] == "table"
    margin = p["models"][1]
    assert margin["tests"] == [] and all(not c["tests"] for c in margin["columns"])                 # no contract tests…
    assert any("declared as string" in c["description"] for c in margin["columns"])                  # …but the declared type stays described
    assert "owner" not in margin["meta"] and p["exported_by"] is None and margin["meta"]["flow"] == "Margin flow"
    assert p["native"]["target_schema"] == "analisi" and p["layout"] == {"package": "project", "folder": "", "layers": False}
    src = p["sources"][0]
    # «already declared» exists only for the folder package (decision #70): the complete project drops it
    assert src["name"] == "erp" and src["declared"] is False and src["key"] == f"{scene.pg.id}:vendite"
    assert p["source_map"][scene.orders.key]["macro"] == "{{ source('erp', 'ordini') }}"


def test_a_materialization_not_allowed_is_refused_and_free_forms_too(session, scene):
    from pydantic import ValidationError

    flow = _flow_with_two_outputs(session, scene)
    with pytest.raises(d.DbtExportError, match="incremental"):
        d.build_export_payload(session, flow, "data-prep", options=d.DbtOptions(materializations={f"{flow.id}:o1": "incremental"}))
    for badly in ({"prefix": "Tab-"}, {"folder": "../x"}, {"schema": "a b"}, {"sources": {"1:x": {"name": "x'); drop"}}},
                 {"materializations": {"1:o": "ephemeral"}}, {"target": "bigquery"}, {"sconosciuta": 1}):
        with pytest.raises(ValidationError):
            d.DbtOptions(**badly)


def test_the_plan_tells_the_dialog_targets_sources_and_outputs(session, scene):
    flow = _flow_with_two_outputs(session, scene)
    piano = d.piano_export(session, flow, "data-prep")
    by_id = {t["id"]: t for t in piano["targets"]}
    assert [t["id"] for t in piano["targets"]] == ["clickhouse", "duckdb", "native"] and all(t["available"] for t in piano["targets"])
    assert by_id["duckdb"]["sources"] == [{"key": f"{scene.pg.id}:vendite", "connection": "gestionale", "schema": "vendite",
                                            "default_name": f"db_{scene.pg.id}_vendite", "tables": ["ordini"],
                                            "default_base": f"db_{scene.pg.id}_vendite"}]   # the base, for the dialog (#48)
    assert by_id["native"]["sources"][0]["default_name"] == "src_vendite"
    outputs = {u["key"]: u for u in piano["outputs"]}
    assert outputs[f"{flow.id}:o1"]["materializations"] == ["table", "view"] and outputs[f"{flow.id}:o1"]["materialized"] == "table"
    assert outputs[f"{flow.id}:o2"]["materializations"] == ["incremental", "table", "view"]
    assert piano["default_folder"] == "margin_flow"
    # a pivot: native and ClickHouse cannot, and the plan says why
    pivot = make_flow(session, name="Piv", project_id=scene.folder.id, definition=json.dumps({"nodes": [
        _src_node("s", scene.orders.id), _op("p", "pivot", {"index": ["id"], "on": "canale", "values": "importo"}),
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "P", "projectId": scene.folder.id, "overwrite": True}},
    ], "edges": [_edge("s", "p"), _edge("p", "o")]}))
    by_id = {t["id"]: t for t in d.piano_export(session, pivot, "data-prep")["targets"]}
    assert by_id["duckdb"]["available"] and not by_id["native"]["available"] and "pivot" in by_id["clickhouse"]["reason"]


@pytest.mark.anyio
async def test_the_post_with_options_and_the_plan_are_for_administrators_only(session, scene, fake_engine):
    from fastapi import HTTPException

    from app.routes import flows as flows_routes
    from tests.conftest import make_permission

    flow = _flow_with_two_outputs(session, scene)
    reader = make_user(session, email="lettore@x.it")
    make_permission(session, user_id=reader.id, project_id=scene.folder.id, capability="manage")
    for call in (lambda u: flows_routes.export_flow_dbt_with_options(flow.id, d.DbtOptions(target="clickhouse"), _request(), u, session),):
        with pytest.raises(HTTPException) as e:
            await call(reader)
        assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        flows_routes.export_flow_dbt_plan(flow.id, _request(), reader, session)
    assert e.value.status_code == 403
    answer = await flows_routes.export_flow_dbt_with_options(flow.id, d.DbtOptions(target="clickhouse", package="folder", folder="mf"), _request(), scene.boss, session)
    assert answer.status_code == 200 and answer.media_type == "application/zip"
    assert fake_engine.dbt_exports[-1]["layout"] == {"package": "folder", "folder": "mf", "layers": False}
    assert fake_engine.dbt_exports[-1]["mode"] == "clickhouse"
    piano = flows_routes.export_flow_dbt_plan(flow.id, _request(), scene.boss, session)
    assert {t["id"] for t in piano["targets"]} == {"clickhouse", "duckdb", "native"}


# ── the errors in the reader's language ────────────────────────────────────────
def _request_in(language):
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": "/", "headers": [(b"accept-language", language.encode())],
                    "client": ("10.0.0.1", 1234), "query_string": b""})


def test_the_requests_language():
    from app.core.language import language_of

    assert language_of("it") == "it" and language_of("de-CH,de;q=0.9,en;q=0.8") == "de" and language_of("pt-BR,fr;q=0.7") == "fr"
    assert language_of(None) == "en" and language_of("") == "en" and language_of("zh-CN,ja") == "en"


def test_every_message_has_the_five_languages_with_the_same_parameters():
    import string

    from app.core.language import LANGUAGES
    from app.services.dbt_messages import MESSAGES

    fields = lambda t: {f for _, f, _, _ in string.Formatter().parse(t) if f}
    for code, by_language in MESSAGES.items():
        assert set(by_language) == set(LANGUAGES), code
        assert len({frozenset(fields(t)) for t in by_language.values()}) == 1, code


def test_translation_and_fallbacks():
    from app.services.dbt_messages import engine_message, translate

    assert translate("upstream_missing", {"source": "ordini"}, "de") == "Die Quelle «ordini» ist die Ausgabe eines Flows, den es nicht mehr gibt."
    assert translate("upstream_missing", {"source": "ordini"}, "pt") .startswith("The source «ordini»")         # a language that is not there: English
    assert translate("codice_nuovo", {}, "it", "testo dell'engine") == "testo dell'engine"                     # unknown code: the text
    assert translate("upstream_missing", {}, "it", "originale") == "originale"                                 # missing parameter: the text
    assert translate("internal", {}, "fr", "boh") == "L'export dbt a échoué : boh"
    assert engine_message({"code": "seed_too_large", "params": {"source": "s", "rows": "60,000", "max": "50,000"}, "message": "x"}, "es").startswith("La fuente «s» tiene 60,000 filas")
    assert engine_message("un engine vecchio", "it") == "un engine vecchio"


@pytest.mark.anyio
async def test_the_refusals_and_the_plan_speak_the_interfaces_language(session, scene, fake_engine):
    from fastapi import HTTPException

    from app.routes import flows as flows_routes

    # a gateway refusal: native with sources on two connections
    my = Connection(name="negozio", db_type="mysql", project_id=scene.folder.id, owner_id=scene.boss.id, host="my.x.it", database="shop", db_schema="")
    session.add(my); session.commit()
    products = make_datasource(session, name="products", project_id=scene.folder.id, key="datasets/5/v1.parquet", kind="database", connection_id=my.id,
                               source_type="table", source_ref="prodotti", columns=json.dumps([{"name": "id", "dtype": "Int64"}]))
    flow = make_flow(session, name="Mix", project_id=scene.folder.id, definition=json.dumps({"nodes": [
        _src_node("a", scene.orders.id), _src_node("c", products.id), _op("u", "union", {"strategy": "by_name"}),
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Mix out", "projectId": scene.folder.id, "overwrite": True}},
    ], "edges": [_edge("a", "u"), _edge("c", "u", "right"), _edge("u", "o")]}))
    expected_items = {"it": "Il target nativo vuole tutte le sorgenti sulla STESSA connessione", "de": "Das native Ziel braucht alle Quellen auf DERSELBEN Verbindung",
              "en": "The native target needs every source on the SAME connection"}
    for language, start in expected_items.items():
        with pytest.raises(HTTPException) as e:
            await flows_routes.export_flow_dbt(flow.id, _request_in(language), "native", scene.boss, session)
        assert e.value.status_code == 422 and e.value.detail.startswith(start), (language, e.value.detail)
        piano = flows_routes.export_flow_dbt_plan(flow.id, _request_in(language), scene.boss, session)
        assert next(t for t in piano["targets"] if t["id"] == "native")["reason"].startswith(start)
    # an engine refusal: it arrives as code and parameters, the gateway translates it
    fake_engine.dbt_export_response = (422, {"detail": {"code": "other_clickhouse", "params": {"connection": "crm", "host": "ch2"}, "message": "testo italiano"}})
    with pytest.raises(HTTPException) as e:
        await flows_routes.export_flow_dbt(flow.id, _request_in("fr"), "clickhouse", scene.boss, session)
    assert e.value.status_code == 422 and e.value.detail.startswith("La connexion «crm» est un AUTRE ClickHouse (ch2)")


# ── the AI in the export (optional): a fake model in place of the provider ───────
@pytest.fixture
def fake_ai(monkeypatch):
    """A model that answers every item with a description (with a bit of Jinja to clean) and
    every translation request with `sql`; it counts the calls."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from app.services import ai_agent, dbt_ai

    state = SimpleNamespace(calls=0, prompt=[], sql="SELECT id, canale FROM __s_0", error=None, samples=[])

    def answer(messages, info):
        state.calls += 1
        if state.error:
            raise RuntimeError(state.error)
        text = next(p.content for m in messages for p in getattr(m, "parts", []) if type(p).__name__ == "UserPromptPart")
        state.prompt.append(text)
        tool = info.output_tools[0].name
        if "items" in tool or text.startswith("{"):
            items = json.loads(text)["items"]
            return ModelResponse(parts=[ToolCallPart(tool_name=tool, args={"items": [
                {"key": v["key"], "description": f"About {v.get('column') or v.get('name')} {{{{ evil }}}}"} for v in items]})])
        return ModelResponse(parts=[ToolCallPart(tool_name=tool, args={"sql": state.sql})])

    async def samples(bucket, key):
        state.samples.append(key)
        return {"id": ["1", "2", "3"], "importo": ["9.5"]}

    monkeypatch.setattr(ai_agent, "build_model", lambda model_id: FunctionModel(answer))
    monkeypatch.setattr(dbt_ai, "ai_model", lambda session: "finto-1")
    monkeypatch.setattr(dbt_ai, "_samples", samples)
    return state


@pytest.mark.anyio
async def test_the_ai_descriptions_go_where_missing_and_are_remembered_per_version(session, scene, fake_ai):
    from app.models import AiSpend, DbtAiText, FlowVersion
    from app.services import dbt_ai

    flow = _flow_with_two_outputs(session, scene)
    session.add(FlowVersion(flow_id=flow.id, version=1, definition=flow.definition)); session.commit()

    async def export():
        payload, project = d._build(session, flow, "data-prep", "duckdb", "capo@x.it", d.DbtOptions(ai_descriptions=True))
        await dbt_ai.describe(session, scene.boss, flow, sorted(project.flows_seen), payload)
        return payload

    p = await export()
    columns = {c["name"]: c["description"] for c in p["sources"][0]["tables"][0]["columns"]}
    assert columns["canale"] == "Sales channel"                                   # the one a person wrote stays
    assert columns["id"].startswith("(AI) About id") and "{{" not in columns["id"] and "{ {" in columns["id"]
    assert p["ai"]["model"] == "finto-1" and p["ai"]["summaries"][0]["flow"] == "Margin flow"
    assert all("(AI) About Margin flow" in m["description"] for m in p["models"] if m["kind"] == "output")
    items = json.loads(fake_ai.prompt[0])["items"]
    assert next(v for v in items if v.get("column") == "id")["samples"] == ["1", "2", "3"]   # at most 3 values per column
    assert not any(v.get("column") == "canale" for v in items)                               # described already: not asked
    assert fake_ai.calls == 1 and session.exec(select(AiSpend)).all()
    rows = session.exec(select(DbtAiText).where(DbtAiText.flow_id == flow.id)).all()
    assert {r.kind for r in rows} == {"describe"} and {r.flow_version for r in rows} == {1}

    # the same version: no call, the same words
    p2 = await export()
    assert fake_ai.calls == 1 and p2["sources"] == p["sources"] and p2["ai"] == p["ai"]
    # a new version is asked again
    session.add(FlowVersion(flow_id=flow.id, version=2, definition=flow.definition)); session.commit()
    await export()
    assert fake_ai.calls == 2


@pytest.mark.anyio
async def test_cap_reached_and_provider_not_answering_stop_the_export(session, scene, fake_ai, monkeypatch):
    from app.services import ai_chats, dbt_ai

    flow = _flow_with_two_outputs(session, scene)
    payload, project = d._build(session, flow, "data-prep", "duckdb", None, d.DbtOptions(ai_descriptions=True))
    fake_ai.error = "503 upstream"
    with pytest.raises(d.DbtExportError) as e:
        await dbt_ai.describe(session, scene.boss, flow, [flow.id], payload)
    assert e.value.code == "ai_failed" and "503" in e.value.params["detail"]
    monkeypatch.setattr(dbt_ai.get_settings().ai, "max_cost_per_day_usd", 0.01)
    monkeypatch.setattr(ai_chats, "spent_today", lambda s, u: Decimal("1"))
    with pytest.raises(d.DbtExportError) as e:
        await dbt_ai.describe(session, scene.boss, flow, [flow.id], payload)
    assert e.value.code == "ai_cap_reached"
    monkeypatch.setattr(dbt_ai, "ai_model", lambda s: None)
    with pytest.raises(d.DbtExportError) as e:
        await dbt_ai.describe(session, scene.boss, flow, [flow.id], payload)
    assert e.value.code == "ai_unavailable"


@pytest.mark.anyio
async def test_the_ai_translation_comes_in_when_the_engine_does_not_translate_and_is_remembered(session, scene, fake_ai, fake_engine):
    from app.models import DbtAiText
    from app.routes import flows as flows_routes

    flow = _flow_with_two_outputs(session, scene)
    refusal = (422, {"detail": {"code": "translation_failed", "message": "x", "params": {
        "dialect": "clickhouse", "detail": "boh", "model": "margin", "sql": "SELECT id, canale FROM __s_0 QUALIFY strano",
        "source_dialect": "duckdb", "target_dialect": "clickhouse", "columns": ["id", "canale"]}}})
    fake_engine.dbt_export_script = [refusal, None]
    opz = d.DbtOptions(target="clickhouse", ai_translations=True)
    answer = await flows_routes.export_flow_dbt_with_options(flow.id, opz, _request(), scene.boss, session)
    assert answer.status_code == 200 and fake_ai.calls == 1
    assert fake_engine.dbt_exports[-1]["overrides"] == {"margin": "SELECT id, canale FROM __s_0"}
    assert session.exec(select(DbtAiText).where(DbtAiText.kind == "translate")).first().text == "SELECT id, canale FROM __s_0"
    # again: the remembered proposal, no call
    fake_engine.dbt_export_script = [refusal, None]
    await flows_routes.export_flow_dbt_with_options(flow.id, opz, _request(), scene.boss, session)
    assert fake_ai.calls == 1
    # without the option: the refusal arrives as it was
    fake_engine.dbt_export_script = [refusal]
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        await flows_routes.export_flow_dbt_with_options(flow.id, d.DbtOptions(target="clickhouse"), _request(), scene.boss, session)
    assert e.value.status_code == 422 and "translation to clickhouse failed" in e.value.detail.lower()


@pytest.mark.anyio
async def test_a_proposal_the_engine_discarded_is_forgotten_and_the_reason_is_in_the_language(session, scene, fake_ai, fake_engine):
    from fastapi import HTTPException

    from app.models import DbtAiText
    from app.routes import flows as flows_routes

    flow = _flow_with_two_outputs(session, scene)
    refusal = (422, {"detail": {"code": "translation_failed", "message": "x", "params": {
        "dialect": "clickhouse", "detail": "boh", "model": "margin", "sql": "SELECT 1", "source_dialect": "duckdb",
        "target_dialect": "clickhouse", "columns": ["id"]}}})
    discarded = (422, {"detail": {"code": "ai_translation_invalid", "message": "x", "params": {
        "model": "margin", "reason": "columns", "got": "a", "expected": "id"}}})
    # discarded twice: the refusal, in the exporter's language; the second request to the AI carries the reason
    fake_engine.dbt_export_script = [refusal, discarded, discarded]
    with pytest.raises(HTTPException) as e:
        await flows_routes.export_flow_dbt_with_options(flow.id, d.DbtOptions(target="clickhouse", ai_translations=True),
                                                       _request_in("it"), scene.boss, session)
    assert e.value.detail == ("La traduzione dell'AI di «margin» non passa i controlli: le sue colonne sono a invece di id. "
                              "Esporta con un altro target, o senza la traduzione dell'AI.")
    assert fake_ai.calls == 2 and "Your previous translation was rejected: The AI's translation" in fake_ai.prompt[-1]
    assert session.exec(select(DbtAiText).where(DbtAiText.kind == "translate")).first() is None
    # discarded once: the second attempt passes
    fake_engine.dbt_export_script = [refusal, discarded, None]
    answer = await flows_routes.export_flow_dbt_with_options(flow.id, d.DbtOptions(target="clickhouse", ai_translations=True),
                                                              _request_in("it"), scene.boss, session)
    assert answer.status_code == 200 and fake_ai.calls == 4


def test_the_plan_says_whether_the_ai_is_there(session, scene, monkeypatch):
    from app.routes import flows as flows_routes
    from app.services import dbt_ai

    flow = _flow_with_two_outputs(session, scene)
    nothing = {"descriptions": False, "translations": False, "models": []}   # what is kept for the version (#55 #59)
    monkeypatch.setattr(dbt_ai, "ai_model", lambda s: None)
    assert flows_routes.export_flow_dbt_plan(flow.id, _request(), scene.boss, session)["ai"] == {"available": False, "model": None, "stored": nothing}
    monkeypatch.setattr(dbt_ai, "ai_model", lambda s: "m-1")
    assert flows_routes.export_flow_dbt_plan(flow.id, _request(), scene.boss, session)["ai"] == {"available": True, "model": "m-1", "stored": nothing}


@pytest.mark.parametrize("db_type", ["oracle", "sqlserver"])
def test_a_flow_reading_from_oracle_or_sql_server_is_refused_with_a_reason(session, scene, db_type):
    """Oracle and SQL Server are sources for Tabularia, not for dbt: DuckDB does not federate
    them, there is no native adapter, ClickHouse does not read them. Never a 500. The
    gateway now refuses ClickHouse too (finding #49: the plan showed it available and
    the engine refused it at the download); this test asserted the payload was built.
    The federated and the native targets now say that no dbt target reads the database
    (source_not_exportable), instead of pointing to targets that refuse it too (review
    of the #49 fix): this test asserted not_federable and no_native_adapter."""
    at = Connection(name="contabilita", db_type=db_type, project_id=scene.folder.id, owner_id=scene.boss.id,
                     host="ora.x.it", port=None, database="ORCLPDB1", db_schema="", username="fin")
    session.add(at); session.commit()
    rows = make_datasource(session, name="ledger", project_id=scene.folder.id, key="datasets/9/v1.parquet", kind="database", connection_id=at.id,
                            source_type="table", source_ref="FIN.LEDGER", columns=json.dumps([{"name": "ID", "dtype": "Int64"}]))
    flow = make_flow(session, name="Ledger", project_id=scene.folder.id, definition=json.dumps({"nodes": [
        _src_node("a", rows.id),
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Ledger out", "projectId": scene.folder.id, "overwrite": True}},
    ], "edges": [_edge("a", "o")]}))
    for target, code in (("duckdb", "source_not_exportable"), ("native", "source_not_exportable"), ("clickhouse", "clickhouse_cannot_read")):
        with pytest.raises(d.DbtExportError) as e:
            d.build_export_payload(session, flow, "data-prep", target)
        assert e.value.code == code
