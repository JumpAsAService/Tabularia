"""How the gateway resolves a flow for the dbt export: where a native output lands,
which schema a one-part table name means, what an upstream flow brings into the
project, how model and folder names are made, the dialect of each model's free
text, and the refusals the plan shows before the download."""
import json
from urllib.parse import quote

import pytest
from sqlmodel import select

from app.models import Connection
from app.services import dbt_export as d
from app.services.flow_resolver import FlowResolveError, resolve_output_request
from app.services.dbt_messages import error_message
from tests.conftest import make_datasource, make_flow
from tests.test_dbt_export import _edge, _flow_with_two_outputs, _src_node, _op, _request, scene  # noqa: F401 (scena is a fixture)


def _ds_output(nid, name, folder):
    return {"id": nid, "type": "output", "data": {"destType": "datasource", "name": name, "projectId": folder, "overwrite": True}}


def _db_output(nid, conn_id, table, mode="append"):
    return {"id": nid, "type": "output", "data": {"destType": "database", "connectionId": conn_id, "table": table, "mode": mode}}


def _flow(session, scene, name, nodes, edges, **extra):
    return make_flow(session, name=name, project_id=scene.folder.id, definition=json.dumps({"nodes": nodes, "edges": edges}), **extra)


def _models(payload):
    return {m["name"]: m for m in payload["models"]}


def _refusal(session, flow, target="duckdb", **options):
    with pytest.raises(d.DbtExportError) as e:
        d.build_export_payload(session, flow, "data-prep", target, options=d.DbtOptions(target=target, **options) if options else None)
    return e.value


# ── #44 #19: where a database output lands on the native target ──────────────
def test_native_output_on_another_connection_is_an_ordinary_model_in_the_target_schema(session, scene):
    """The usual ERP → DW flow: dbt runs on the source warehouse, so an output that
    Tabularia writes on ANOTHER connection must not become a table of the source
    (here with the very name of the source table it reads)."""
    dw = Connection(name="dw", db_type="postgresql", project_id=scene.folder.id, owner_id=scene.boss.id,
                    host="dw.x.it", database="warehouse", db_schema="mart")
    session.add(dw); session.commit()
    flow = _flow(session, scene, "Erp to dw", [
        _src_node("s", scene.orders.id),
        _db_output("o1", dw.id, "vendite.ordini", "replace"),
        _db_output("o2", dw.id, "facts"),
        _db_output("o3", scene.pg.id, "report.righe"),
    ], [_edge("s", "o1"), _edge("s", "o2"), _edge("s", "o3")])
    p = d.build_export_payload(session, flow, "data-prep", "native")
    m = _models(p)
    assert p["native"]["conn"]["host"] == "db.x.it"
    for name in ("ordini", "facts"):
        assert "schema" not in m[name] and "alias" not in m[name], name
        assert any(f"The model `{name}`" in n and "«dw»" in n and "data team" in n for n in p["notes"]), name
    assert (m["righe"]["schema"], m["righe"]["alias"]) == ("report", "righe")       # on the source connection: its real table
    assert not any("The model `righe`" in n for n in p["notes"])
    assert next(t for t in d.export_plan(session, flow, "data-prep")["targets"] if t["id"] == "native")["available"]


@pytest.mark.parametrize("db_schema, expected", [("vendite", "vendite"), ("", "public")])
def test_native_output_with_a_one_part_table_lands_where_tabularia_writes_it(session, scene, db_schema, expected):
    scene.pg.db_schema = db_schema
    session.add(scene.pg); session.commit()
    flow = _flow(session, scene, "Bare", [_src_node("s", scene.orders.id), _db_output("o", scene.pg.id, "report")], [_edge("s", "o")])
    m = _models(d.build_export_payload(session, flow, "data-prep", "native"))["report"]
    assert (m["schema"], m["alias"]) == (expected, "report")


def test_native_clickhouse_output_with_a_one_part_table_lands_in_the_connection_database(session, scene):
    activity = make_datasource(session, name="activities", project_id=scene.folder.id, key="datasets/31/v1.parquet", kind="database",
                               connection_id=scene.ch.id, source_type="table", source_ref="crm.activities",
                               columns=json.dumps([{"name": "id", "dtype": "Int64"}]))
    flow = _flow(session, scene, "Ch", [_src_node("s", activity.id), _db_output("o", scene.ch.id, "events")], [_edge("s", "o")])
    m = _models(d.build_export_payload(session, flow, "data-prep", "native"))["events"]
    assert (m["schema"], m["alias"]) == ("analytics", "events")


# ── #20: a one-part source table is read from the connection's schema ─────────
@pytest.mark.parametrize("target", ["duckdb", "native", "clickhouse"])
def test_a_one_part_source_table_is_read_from_the_connection_schema(session, scene, target):
    scene.pg.db_schema = "vendite"
    session.add(scene.pg); session.commit()
    bare = make_datasource(session, name="bare orders", project_id=scene.folder.id, key="datasets/30/v1.parquet", kind="database",
                           connection_id=scene.pg.id, source_type="table", source_ref="ordini",
                           columns=json.dumps([{"name": "id", "dtype": "Int64"}]))
    flow = _flow(session, scene, "Bare src", [_src_node("s", bare.id), _ds_output("o", "Bare out", scene.folder.id)], [_edge("s", "o")])
    p = d.build_export_payload(session, flow, "data-prep", target)
    assert [(s["schema"], [t["name"] for t in s["tables"]]) for s in p["sources"]] == [("vendite", ["ordini"])]


@pytest.mark.parametrize("db_schema, expected", [("", "shop"), ("archivio", "archivio")])
def test_a_one_part_mysql_table_is_read_from_the_schema_or_the_database(session, scene, db_schema, expected):
    my = Connection(name="negozio", db_type="mysql", project_id=scene.folder.id, owner_id=scene.boss.id, host="my.x.it",
                    database="shop", db_schema=db_schema)
    session.add(my); session.commit()
    products = make_datasource(session, name="products", project_id=scene.folder.id, key="datasets/5/v1.parquet", kind="database",
                               connection_id=my.id, source_type="table", source_ref="prodotti", columns=json.dumps([{"name": "id", "dtype": "Int64"}]))
    flow = _flow(session, scene, "My", [_src_node("s", products.id), _ds_output("o", "My out", scene.folder.id)], [_edge("s", "o")])
    assert [s["schema"] for s in d.build_export_payload(session, flow, "data-prep", "duckdb")["sources"]] == [expected]


# ── #37 #45 #47: from an upstream flow, only the branch read ─────────────────
def test_an_upstream_flow_brings_in_only_the_outputs_read(session, scene):
    """Two outputs of the upstream flow are read: both come in (each with its own
    steps), and the third one — an append into a database table — stays out."""
    monte = _flow(session, scene, "Upstream", [
        _src_node("s", scene.orders.id), _op("f", "filter", {"column": "canale", "operator": "eq", "value": "web"}),
        _ds_output("o1", "Web", scene.folder.id), _ds_output("o2", "All", scene.folder.id), _db_output("o3", scene.pg.id, "report.log"),
    ], [_edge("s", "f"), _edge("f", "o1"), _edge("s", "o2"), _edge("f", "o3")])
    web = make_datasource(session, name="Web", project_id=scene.folder.id, key="datasets/40/v1.parquet", kind="flow", flow_id=monte.id)
    everyone = make_datasource(session, name="All", project_id=scene.folder.id, key="datasets/41/v1.parquet", kind="flow", flow_id=monte.id)
    downstream = _flow(session, scene, "Downstream", [
        _src_node("a", web.id), _src_node("b", everyone.id), _op("u", "union", {"strategy": "by_name"}), _ds_output("o", "Both", scene.folder.id),
    ], [_edge("a", "u"), _edge("b", "u", "right"), _edge("u", "o")])
    for target in ("duckdb", "native"):
        p = d.build_export_payload(session, downstream, "data-prep", target)
        assert [m["name"] for m in p["models"]] == ["web", "all", "both"], target
        assert not any("schema" in m or "alias" in m for m in p["models"]), target
    assert [o["model"] for o in d.export_plan(session, downstream, "data-prep")["outputs"]] == ["web", "all", "both"]


def test_the_upstream_model_is_the_one_of_the_flow_that_published_the_datasource(session, scene):
    """B and its copy «B (1)» both have an Output publishing «X» in the same folder;
    X was last written by B. A flow reading X must get B's logic, not the copy's."""
    b = _flow(session, scene, "B", [
        _src_node("s", scene.orders.id), _op("f", "filter", {"column": "importo", "operator": "gt", "value": 0}), _ds_output("o", "X", scene.folder.id),
    ], [_edge("s", "f"), _edge("f", "o")])
    b1 = _flow(session, scene, "B (1)", [
        _src_node("s", scene.orders.id), _op("f", "filter", {"column": "importo", "operator": "gt", "value": 15}),
        _ds_output("o", "X", scene.folder.id), _ds_output("z", "Z", scene.folder.id),
    ], [_edge("s", "f"), _edge("f", "o"), _edge("s", "z")])
    x = make_datasource(session, name="X", project_id=scene.folder.id, key="datasets/42/v1.parquet", kind="flow", flow_id=b.id)
    z = make_datasource(session, name="Z", project_id=scene.folder.id, key="datasets/43/v1.parquet", kind="flow", flow_id=b1.id)
    a = _flow(session, scene, "A", [
        _src_node("sz", z.id), _ds_output("oz", "AZ", scene.folder.id), _src_node("sx", x.id), _ds_output("ox", "AX", scene.folder.id),
    ], [_edge("sz", "oz"), _edge("sx", "ox")])
    m = _models(d.build_export_payload(session, a, "data-prep", "duckdb"))
    source = m[m["ax"]["source"]["model"]]
    assert source["meta"]["flow_id"] == b.id and source["operations"][0]["params"]["value"] == 0


def test_two_flows_feeding_each_other_through_different_outputs_are_not_a_cycle(session, scene):
    """B: orders → X1, and Y → X2; C: X1 → Y. The data path X1 → Y → X2 has no cycle."""
    b = _flow(session, scene, "B", [], [])
    x1 = make_datasource(session, name="X1", project_id=scene.folder.id, key="datasets/44/v1.parquet", kind="flow", flow_id=b.id)
    c = _flow(session, scene, "C", [
        _src_node("s", x1.id), _op("f", "filter", {"column": "importo", "operator": "gt", "value": 0}), _ds_output("o", "Y", scene.folder.id),
    ], [_edge("s", "f"), _edge("f", "o")])
    y = make_datasource(session, name="Y", project_id=scene.folder.id, key="datasets/45/v1.parquet", kind="flow", flow_id=c.id)
    b.definition = json.dumps({"nodes": [_src_node("s1", scene.orders.id), _ds_output("o1", "X1", scene.folder.id),
                                         _src_node("s2", y.id), _ds_output("o2", "X2", scene.folder.id)],
                               "edges": [_edge("s1", "o1"), _edge("s2", "o2")]})
    session.add(b); session.commit()
    pb = d.build_export_payload(session, b, "data-prep", "duckdb")
    assert [m["name"] for m in pb["models"]] == ["x1", "y", "x2"]
    m = _models(pb)
    assert m["y"]["source"] == {"model": "x1"} and m["x2"]["source"] == {"model": "y"}
    assert [m["name"] for m in d.build_export_payload(session, c, "data-prep", "duckdb")["models"]] == ["x1", "y"]


# ── #46 #51: a flow that cannot be read says which one, and why ──────────────
def test_a_loop_through_a_join_right_input_is_refused_with_a_reason(session, scene):
    definition = {"nodes": [_src_node("s", scene.orders.id), _op("j", "join", {"how": "inner", "on": ["id"]}),
                             _op("k", "filter", {"column": "importo", "operator": "gt", "value": 0}), _ds_output("o", "Loop", scene.folder.id)],
                   "edges": [_edge("s", "j"), _edge("j", "k"), _edge("k", "o"), _edge("k", "j", "right")]}
    flow = _flow(session, scene, "Loopy", definition["nodes"], definition["edges"])
    e = _refusal(session, flow)
    assert e.code == "flow_loop" and e.params["flow"] == "Loopy" and e.params["node"] in ("j", "k")
    piano = d.export_plan(session, flow, "data-prep", "it")
    assert all(not t["available"] and t["reason"].startswith("Il flusso «Loopy» ha un ciclo") for t in piano["targets"])
    # the scheduler uses the same resolver: a coded error, not a RecursionError
    with pytest.raises(FlowResolveError) as fe:
        resolve_output_request(definition, definition["nodes"][-1], lambda i: ("data-prep", scene.orders.key), "data-prep")
    assert fe.value.code == "flow_loop"


def test_a_broken_draft_output_upstream_does_not_block_and_one_here_names_its_flow(session, scene):
    monte = _flow(session, scene, "Upstream B", [
        _src_node("s", scene.orders.id), _ds_output("o", "X", scene.folder.id), _ds_output("draft", "Draft", scene.folder.id),
    ], [_edge("s", "o")])
    x = make_datasource(session, name="X", project_id=scene.folder.id, key="datasets/46/v1.parquet", kind="flow", flow_id=monte.id)
    downstream = _flow(session, scene, "Down", [_src_node("s", x.id), _ds_output("o", "AX", scene.folder.id)], [_edge("s", "o")])
    assert [m["name"] for m in d.build_export_payload(session, downstream, "data-prep", "duckdb")["models"]] == ["x", "ax"]
    # the broken Output in the exported flow itself: the flow and the Output named, all in the reader's language
    e = _refusal(session, monte)
    assert e.code == "output_no_source" and (e.params["flow"], e.params["output"]) == ("Upstream B", "Draft")
    text = error_message(e, "it")
    assert text.startswith("Nel flusso «Upstream B», l'Output «Draft»") and "no source" not in text


# ── #50: a renamed datasource is not fixed by running the flow again ──────────
def test_after_a_rename_the_refusal_says_what_happened(session, scene):
    monte = _flow(session, scene, "Up", [_src_node("s", scene.orders.id), _ds_output("o", "Pub", scene.folder.id)], [_edge("s", "o")])
    pub = make_datasource(session, name="Pub renamed", project_id=scene.folder.id, key="datasets/47/v1.parquet", kind="flow", flow_id=monte.id)
    downstream = _flow(session, scene, "Down", [_src_node("s", pub.id), _ds_output("o", "Down out", scene.folder.id)], [_edge("s", "o")])
    e = _refusal(session, downstream)
    assert e.code == "upstream_no_output" and e.params["published"] == "«Pub»"
    text = error_message(e, "en")
    assert "run it again" not in text and "renamed or moved" in text and "«Pub»" in text and "«Pub renamed»" in text


# ── #48: two sources dbt cannot tell apart ────────────────────────────────────
def _two_schemas(session, scene, schema_a="sales", schema_b="crm", table_b="orders"):
    a = make_datasource(session, name="a", project_id=scene.folder.id, key="datasets/50/v1.parquet", kind="database", connection_id=scene.pg.id,
                        source_type="table", source_ref=f"{schema_a}.orders", columns=json.dumps([{"name": "id", "dtype": "Int64"}]))
    b = make_datasource(session, name="b", project_id=scene.folder.id, key="datasets/51/v1.parquet", kind="database", connection_id=scene.pg.id,
                        source_type="table", source_ref=f"{schema_b}.{table_b}", columns=json.dumps([{"name": "id", "dtype": "Int64"}]))
    return _flow(session, scene, "Two schemas", [_src_node("a", a.id), _src_node("b", b.id), _op("u", "union", {}),
                                                    _ds_output("o", "U", scene.folder.id)],
                   [_edge("a", "u"), _edge("b", "u", "right"), _edge("u", "o")])


def test_two_sources_with_the_same_name_and_a_table_in_common_are_refused(session, scene):
    flow = _two_schemas(session, scene)
    names = {f"{scene.pg.id}:sales": {"name": "erp"}, f"{scene.pg.id}:crm": {"name": "erp"}}
    for target in ("duckdb", "native", "clickhouse"):
        e = _refusal(session, flow, target, sources=names)
        assert e.code == "source_name_duplicate" and (e.params["name"], e.params["table"]) == ("erp", "orders"), target
        assert "gestionale.sales" in e.params["sources"] and "gestionale.crm" in e.params["sources"]


def test_one_name_for_two_schemas_without_a_table_in_common_is_allowed(session, scene):
    flow = _two_schemas(session, scene, table_b="customers")
    names = {f"{scene.pg.id}:sales": {"name": "erp"}, f"{scene.pg.id}:crm": {"name": "erp"}}
    p = d.build_export_payload(session, flow, "data-prep", options=d.DbtOptions(sources=names))
    assert [(s["name"], s["schema"]) for s in p["sources"]] == [("erp", "sales"), ("erp", "crm")]


def test_default_source_names_that_slug_alike_get_a_suffix(session, scene):
    flow = _two_schemas(session, scene, schema_a="Sales", schema_b="sales")
    for target, base in (("duckdb", f"db_{scene.pg.id}_sales"), ("native", "src_sales")):
        p = d.build_export_payload(session, flow, "data-prep", target)
        assert [s["name"] for s in p["sources"]] == [base, f"{base}_2"], target
        assert len({e.get("ref") or e.get("macro") for e in p["source_map"].values()}) == 2


# ── #49: the plan refuses what the download would refuse ──────────────────────
@pytest.mark.parametrize("db_type", ["oracle", "sqlserver", "trino"])
def test_clickhouse_does_not_read_oracle_sql_server_or_trino(session, scene, db_type):
    other_one = Connection(name="contabilita", db_type=db_type, project_id=scene.folder.id, owner_id=scene.boss.id, host="ora.x.it",
                       database="ORCLPDB1", db_schema="", username="fin")
    session.add(other_one); session.commit()
    rows = make_datasource(session, name="ledger", project_id=scene.folder.id, key="datasets/9/v1.parquet", kind="database",
                            connection_id=other_one.id, source_type="table", source_ref="FIN.LEDGER", columns=json.dumps([{"name": "ID", "dtype": "Int64"}]))
    flow = _flow(session, scene, "Ledger", [_src_node("a", rows.id), _ds_output("o", "Ledger out", scene.folder.id)], [_edge("a", "o")])
    e = _refusal(session, flow, "clickhouse")
    assert e.code == "clickhouse_cannot_read" and e.params == {"connection": "contabilita", "db_type": db_type}
    assert not any(t["available"] for t in d.export_plan(session, flow, "data-prep")["targets"])


def test_a_seed_over_the_engine_cap_is_refused_before_the_download(session, scene):
    big = make_datasource(session, name="big list", project_id=scene.folder.id, key="datasets/52/v1.parquet", kind="flow", flow_id=None,
                             rows=1_000_000_000, columns=json.dumps([{"name": "sku", "dtype": "String"}]))
    flow = _flow(session, scene, "Big", [_src_node("s", scene.orders.id), _src_node("g", big.id), _op("j", "join", {"how": "left", "on": ["id"]}),
                                           _ds_output("o", "Big out", scene.folder.id)],
                   [_edge("s", "j"), _edge("g", "j", "right"), _edge("j", "o")])
    e = _refusal(session, flow)
    assert e.code == "seed_too_large" and e.params == {"source": "big list", "rows": "1,000,000,000", "max": "50,000"}
    assert not any(t["available"] for t in d.export_plan(session, flow, "data-prep")["targets"])
    big.rows = d.SEED_MAX_ROWS
    session.add(big); session.commit()
    assert d.build_export_payload(session, flow, "data-prep", "duckdb")["seeds"][0]["name"] == "seed_big_list"


# ── #12: no native target for MySQL/MariaDB ──────────────────────────────────
@pytest.mark.parametrize("db_type", ["mysql", "mariadb"])
def test_mysql_and_mariadb_have_no_native_target_and_the_reason_points_to_the_others(session, scene, db_type):
    my = Connection(name="negozio", db_type=db_type, project_id=scene.folder.id, owner_id=scene.boss.id, host="my.x.it", port=None,
                    database="shop", db_schema="")
    session.add(my); session.commit()
    products = make_datasource(session, name="products", project_id=scene.folder.id, key="datasets/5/v1.parquet", kind="database",
                               connection_id=my.id, source_type="table", source_ref="prodotti", columns=json.dumps([{"name": "id", "dtype": "Int64"}]))
    flow = _flow(session, scene, "Shop", [_src_node("s", products.id), _ds_output("o", "Shop out", scene.folder.id)], [_edge("s", "o")])
    e = _refusal(session, flow, "native")
    assert e.code == "no_native_adapter" and e.params == {"db_type": db_type}
    by_id = {t["id"]: t for t in d.export_plan(session, flow, "data-prep", "en")["targets"]}
    assert by_id["duckdb"]["available"] and by_id["clickhouse"]["available"] and not by_id["native"]["available"]
    assert "federated target" in by_id["native"]["reason"] and "ClickHouse target" in by_id["native"]["reason"]
    assert d.build_export_payload(session, flow, "data-prep", "clickhouse")["clickhouse"]["connections"][0]["port"] == 3306


# ── #7: the dialect of each model's free text ────────────────────────────────
@pytest.mark.parametrize("engine, production, expected", [
    ("polars", None, "polars"), ("duckdb", None, "duckdb"), ("chdb", None, "clickhouse"), ("clickhouse", None, "clickhouse"),
    ("bigquery", None, "bigquery"), ("polars", "chdb", "clickhouse"), ("duckdb", "polars", "polars"),
])
def test_every_model_of_a_flow_carries_the_dialect_of_its_engine(session, scene, engine, production, expected):
    flow = _flow_with_two_outputs(session, scene, engine=engine, production_engine=production)
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert [(m["name"], m["dialect"]) for m in p["models"]] == [("int_c", expected), ("margin", expected), ("righe", expected)]


def test_upstream_models_carry_the_upstream_flow_dialect_and_a_query_none(session, scene):
    monte = _flow(session, scene, "Upstream", [_src_node("s", scene.customers.id), _ds_output("o", "Clean customers", scene.folder.id)],
                    [_edge("s", "o")], engine="duckdb", production_engine="chdb")
    published = make_datasource(session, name="Clean customers", project_id=scene.folder.id, key="datasets/9/v1.parquet", kind="flow", flow_id=monte.id)
    downstream = _flow(session, scene, "Downstream", [_src_node("s", published.id), _ds_output("o", "Final", scene.folder.id)], [_edge("s", "o")])
    m = _models(d.build_export_payload(session, downstream, "data-prep", "duckdb"))
    assert (m["clean_customers"]["dialect"], m["final"]["dialect"]) == ("clickhouse", "polars")
    assert "dialect" not in m["src_customers"]       # the datasource's own query is written for its database, not for a flow engine


# ── #73 #22 #43: names ───────────────────────────────────────────────────────
def test_a_prefixed_name_is_the_prefix_plus_the_name_the_plan_shows(session, scene):
    flow = _flow(session, scene, "Year", [_src_node("s", scene.orders.id), _ds_output("o", "2024 margin", scene.folder.id)], [_edge("s", "o")])
    name = d.export_plan(session, flow, "data-prep")["outputs"][0]["model"]
    assert name == "m_2024_margin"
    p = d.build_export_payload(session, flow, "data-prep", options=d.DbtOptions(prefix="tab_"))
    assert p["models"][-1]["name"] == "tab_" + name


def test_names_fit_the_postgres_identifier_limit_whatever_the_prefix(session, scene):
    long_ = "monthly margin by customer region and product category for the board 2026"
    label = "a very long label for the step that every output of this flow shares"
    flow = _flow(session, scene, "Board", [
        _src_node("s", scene.orders.id),
        {"id": "f", "type": "operation", "data": {"opType": "filter", "label": label, "params": {"column": "importo", "operator": "gt", "value": 0}}},
        _ds_output("o1", long_, scene.folder.id), _ds_output("o2", long_ + " restated", scene.folder.id),
        _op("g", "limit", {"n": 5}), _ds_output("o3", long_ + " sample", scene.folder.id),
    ], [_edge("s", "f"), _edge("f", "o1"), _edge("f", "o2"), _edge("f", "g"), _edge("g", "o3")])
    piano = [o["model"] for o in d.export_plan(session, flow, "data-prep")["outputs"]]
    prefix = "abcdefghijklmnopqrst"                       # the longest prefix the dialog accepts
    for target in ("duckdb", "native", "clickhouse"):
        p = d.build_export_payload(session, flow, "data-prep", options=d.DbtOptions(target=target, prefix=prefix))
        names = [m["name"] for m in p["models"]]
        assert len(set(names)) == len(names), target
        for m in p["models"]:
            cte = len("__dbt__cte__") if m["materialized"] == "ephemeral" else 0
            assert len(m["name"].encode()) + cte <= 63, (target, m["name"])
        assert [m["name"] for m in p["models"] if m["kind"] == "output"] == [prefix + n for n in piano], target
    assert piano[0].startswith("monthly_margin_by_customer") and len(set(piano)) == 3


@pytest.mark.parametrize("name, wait", [("Città vendite", "citta_vendite"), ("销售", "tabularia"), ("Margin flow", "margin_flow"),
                                          ("zz dialog monthly sales report by geographic area", "zz_dialog_monthly_sales_report_by_geogra")])
def test_the_default_folder_is_one_the_options_accept(session, scene, name, wait):
    flow = _flow(session, scene, name, [_src_node("s", scene.orders.id), _ds_output("o", "Out", scene.folder.id)], [_edge("s", "o")])
    folder = d.export_plan(session, flow, "data-prep")["default_folder"]
    assert folder == wait and d.DbtOptions(package="folder", folder=folder).folder == folder


# ── decisions 9 and 10: what the package makes meaningless is dropped ─────────
def test_the_folder_package_ignores_the_schema_and_the_project_ignores_declared_sources(session, scene):
    flow = _flow_with_two_outputs(session, scene)
    folder = {"package": "folder", "folder": "mf", "schema": "analisi"}
    assert d.build_export_payload(session, flow, "data-prep", options=d.DbtOptions(target="duckdb", **folder))["schema"] is None
    assert d.build_export_payload(session, flow, "data-prep", options=d.DbtOptions(target="native", **folder))["native"]["target_schema"] == "dbt_tabularia"
    assert d.build_export_payload(session, flow, "data-prep", options=d.DbtOptions(target="clickhouse", **folder))["clickhouse"]["target_schema"] == "dbt_tabularia"
    declared = {f"{scene.pg.id}:vendite": {"name": "erp", "declared": True}}
    project = d.build_export_payload(session, flow, "data-prep", options=d.DbtOptions(sources=declared))["sources"][0]
    assert (project["name"], project["declared"]) == ("erp", False)
    in_the_folder = d.build_export_payload(session, flow, "data-prep", options=d.DbtOptions(package="folder", sources=declared))["sources"][0]
    assert (in_the_folder["name"], in_the_folder["declared"]) == ("erp", True)


# ── #38: a flow name outside Latin-1 downloads ───────────────────────────────
@pytest.mark.anyio
async def test_a_flow_name_outside_latin_1_downloads_with_an_ascii_and_a_utf8_file_name(session, scene, fake_engine):
    from app.models import AuditLog
    from app.routes import flows as flows_routes

    flow = _flow_with_two_outputs(session, scene)
    flow.name = "zzhunt Łódź 销售"
    session.add(flow); session.commit()
    answer = await flows_routes.export_flow_dbt(flow.id, _request(), "duckdb", scene.boss, session)
    assert answer.status_code == 200
    assert answer.headers["content-disposition"] == (
        "attachment; filename=\"zzhunt__odz_dbt_duckdb.zip\"; filename*=UTF-8''" + quote("zzhunt_Łódź_销售_dbt_duckdb.zip", safe=""))
    assert len(session.exec(select(AuditLog).where(AuditLog.action == "export.download")).all()) == 1


def test_the_browser_can_read_the_file_name_across_origins():
    """In development the interface (:3000) calls the gateway (:8000): unless CORS
    exposes Content-Disposition, the browser hides it and the dialog cannot name the
    zip with the flow's own letters."""
    from fastapi.testclient import TestClient

    from app.main import app

    r = TestClient(app).get("/health", headers={"Origin": "http://localhost:3000"})
    assert "content-disposition" in r.headers.get("access-control-expose-headers", "").lower()


@pytest.mark.anyio
async def test_the_folder_package_downloads_with_the_folder_suffix_the_dialog_uses(session, scene, fake_engine):
    """The dialog names the zip from the header whenever it can read it, and falls back to
    `<flow>_dbt_<target>_folder.zip` for the folder package: the two must agree (#75)."""
    from app.routes import flows as flows_routes

    flow = _flow_with_two_outputs(session, scene)
    flow.name = "Città vendite"
    session.add(flow); session.commit()
    folder = await flows_routes.export_flow_dbt_with_options(
        flow.id, d.DbtOptions(target="clickhouse", package="folder", folder="vendite"), _request(), scene.boss, session)
    assert folder.headers["content-disposition"] == (
        "attachment; filename=\"Citta_vendite_dbt_clickhouse_folder.zip\"; filename*=UTF-8''"
        + quote("Città_vendite_dbt_clickhouse_folder.zip", safe=""))
    project = await flows_routes.export_flow_dbt_with_options(flow.id, d.DbtOptions(target="clickhouse"), _request(), scene.boss, session)
    assert project.headers["content-disposition"].endswith(quote("Città_vendite_dbt_clickhouse.zip", safe=""))


# ── every code the engine raises reads in the reader's language ───────────────
# The engine's codes added by the fixes, with the parameters it sends (backend/app/engine/dbt_export.py);
# e2e/dbt_export/codici.py checks every raise site against the catalogue
_ENGINE_CODES = {
    "filter_value_missing": {"column": "importo", "operator": "between"},
    "step_invalid": {"step": "join", "detail": "join needs 'on'"},
    "cast_type_unknown": {"column": "valore"},
    "text_not_translatable": {"step": "compute_anno", "dialect": "clickhouse", "detail": "toYear(d)"},
    "columns_differ_only_by_case": {"columns": "Codice, codice", "step": "source_ordini"},
    "column_names_too_long": {"columns": "a…1, a…2", "limit": 63},
    "model_too_large": {"model": "wide", "size": "17,301,024", "max": "16,777,216"},
    # a table/incremental model on dbt-clickhouse whose column name has a double quote (handoff address-engine 1a)
    "column_name_unsupported": {"model": "uno", "column": 'quote"col', "character": '"', "adapter": "dbt-clickhouse"},
    # the exporter's own error on a well-formed node: the step and the exception, not «complete it in the editor» (1b)
    "internal": {"detail": "join: KeyError: 'canale'"},
}


@pytest.mark.parametrize("code", sorted(_ENGINE_CODES))
def test_the_engine_s_new_refusals_read_in_every_language(code):
    from app.core.language import LANGUAGES
    from app.services.dbt_messages import engine_message

    params = _ENGINE_CODES[code]
    for language in LANGUAGES:
        sentence = engine_message({"code": code, "params": params, "message": "ENGINE TEXT"}, language)
        assert sentence != "ENGINE TEXT" and "{" not in sentence, (code, language, sentence)
        assert all(str(v) in sentence for v in params.values()), (code, language, sentence)


# ── #11: an incomplete node is refused in the plan, not only at the download ──
@pytest.mark.parametrize("node, right, code", [
    (_op("j", "join", {"how": "left", "on": ["id"]}), False, "step_invalid"),                                   # nothing on the right handle
    (_op("j", "union", {"strategy": "by_name"}), False, "step_invalid"),
    (_op("j", "join", {"how": "inner", "left_on": ["id", "canale"], "right_on": ["id"]}), True, "step_invalid"),
    (_op("j", "join", {"how": "inner"}), True, "step_invalid"),                                                # no keys
    (_op("j", "filter", {"operator": "eq", "value": "web"}), False, "step_invalid"),                            # no column
    (_op("j", "filter", {"column": "canale", "operator": "eq"}), False, "filter_value_missing"),
    (_op("j", "filter", {"column": "canale", "operator": "between", "value": ["a"]}), False, "filter_value_missing"),
    (_op("j", "filter", {"column": "canale", "operator": "in", "value": []}), False, "filter_value_missing"),
    (_op("j", "group_by", {"by": ["canale"], "aggregations": [{"func": "sum"}]}), False, "step_invalid"),
    (_op("j", "sort", {"descending": False}), False, "step_invalid"),
], ids=["join_no_right", "union_no_right", "join_keys_unequal", "join_no_keys", "filter_no_column", "eq_no_value",
        "between_one_value", "in_empty", "aggregation_no_column", "sort_no_by"])
def test_an_incomplete_node_makes_every_target_unavailable_in_the_plan(session, scene, node, right, code):
    nodes = [_src_node("s", scene.orders.id), _src_node("r", scene.customers.id), node, _ds_output("o", "Out", scene.folder.id)]
    edges = [_edge("s", "j"), _edge("j", "o")] + ([_edge("r", "j", "right")] if right else [])
    flow = _flow(session, scene, "Half done", nodes, edges)
    piano = d.export_plan(session, flow, "data-prep", "it")
    assert [t["available"] for t in piano["targets"]] == [False, False, False], piano["targets"]
    with pytest.raises(d.DbtExportError) as e:
        d.build_export_payload(session, flow, "data-prep")
    assert e.value.code == code and e.value.params.get("step" if code == "step_invalid" else "operator")
    assert "{" not in error_message(e.value, "it")


def test_complete_joins_filters_and_aggregations_stay_available(session, scene):
    nodes = [_src_node("s", scene.orders.id), _src_node("r", scene.customers.id),
            _op("j", "join", {"how": "left", "left_on": ["id"], "right_on": ["id"]}),
            _op("f", "filter", {"column": "nome", "operator": "is_null"}),
            _op("g", "group_by", {"by": ["canale"], "aggregations": [{"column": "importo", "func": "sum"}]}),
            _op("t", "sort", {"by": ["canale"]}), _ds_output("o", "Out", scene.folder.id)]
    edges = [_edge("s", "j"), _edge("r", "j", "right"), _edge("j", "f"), _edge("f", "g"), _edge("g", "t"), _edge("t", "o")]
    flow = _flow(session, scene, "Done", nodes, edges)
    assert all(t["available"] for t in d.export_plan(session, flow, "data-prep")["targets"])
