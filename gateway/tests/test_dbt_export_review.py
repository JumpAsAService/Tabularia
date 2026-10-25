"""What the independent review of the dbt export fix found in the gateway: reasons that
sent the user to a target that refuses too, a target the plan disabled although the
download allows it, definitions that gave a 500, one-part Postgres names and the
search_path, a native model that would replace the table Tabularia appends to, names
dbt cannot quote, an upstream datasource two Outputs publish, the AI texts the plan
did not mention, and the edges of the AI translation loop."""
import json
import time

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.core.language import LANGUAGES
from app.models import Connection, DbtAiText
from app.services import dbt_ai
from app.services import dbt_export as d
from app.services.dbt_messages import engine_message, error_message, translate
from tests.conftest import make_datasource
from tests.test_dbt_export import _edge, _flow_with_two_outputs, _src_node, _op, _request, scene  # noqa: F401 (scena is a fixture)
from tests.test_dbt_export_ai import _translation_refusal, _version, ai  # noqa: F401 (ai is a fixture)
from tests.test_dbt_export_resolution import _flow, _models, _refusal, _db_output, _ds_output


def _piano(session, flow, language="en"):
    return {t["id"]: t for t in d.export_plan(session, flow, "data-prep", language)["targets"]}


def _connection(session, scene, db_type, name="contabilita", **kw):
    c = Connection(name=name, db_type=db_type, project_id=scene.folder.id, owner_id=scene.boss.id, host=kw.get("host", "x.it"),
                   port=None, database=kw.get("database", "DB"), db_schema=kw.get("db_schema", ""), username="u")
    session.add(c)
    session.commit()
    return c


def _table(session, scene, conn, ref, key, name="ledger", kind="table"):
    return make_datasource(session, name=name, project_id=scene.folder.id, key=key, kind="database", connection_id=conn.id,
                           source_type=kind, source_ref=ref, columns=json.dumps([{"name": "id", "dtype": "Int64"}]))


def _from_a_source(session, scene, ds, name="Flow"):
    return _flow(session, scene, name, [_src_node("s", ds.id), _ds_output("o", f"{name} out", scene.folder.id)], [_edge("s", "o")])


# ── #49: a source no dbt target reads ─────────────────────────────────────────
@pytest.mark.parametrize("db_type", ["oracle", "sqlserver", "trino"])
def test_a_source_no_dbt_target_reads_never_points_to_a_target_that_refuses_too(session, scene, db_type):
    """The federated reason said «use the ClickHouse or the native target», the
    ClickHouse one «use another target»: for an Oracle flow every target refuses."""
    conn = _connection(session, scene, db_type)
    flow = _from_a_source(session, scene, _table(session, scene, conn, "FIN.LEDGER", "datasets/90/v1.parquet"))
    for target in ("duckdb", "native"):
        e = _refusal(session, flow, target)
        assert (e.code, e.params) == ("source_not_exportable", {"connection": "contabilita", "db_type": db_type}), target
    for language in LANGUAGES:
        piano = _piano(session, flow, language)
        assert not any(t["available"] for t in piano.values())
        for target in ("duckdb", "native"):
            assert piano[target]["reason"] == translate("source_not_exportable", {"connection": "contabilita", "db_type": db_type}, language)
    piano = _piano(session, flow)
    assert "no dbt target reads" in piano["duckdb"]["reason"] and "PostgreSQL or MySQL table" in piano["duckdb"]["reason"]
    assert "Use another target" not in piano["clickhouse"]["reason"]
    assert "already declares" in piano["clickhouse"]["reason"] and "Folder for an existing project" in piano["clickhouse"]["reason"]


def test_with_an_oracle_and_a_postgres_source_the_native_reason_is_the_oracle_one(session, scene):
    """«Use the ClickHouse or the federated target» (one connection only) is no way
    out for a flow that also reads Oracle."""
    at = _connection(session, scene, "oracle", "ora")
    rows = _table(session, scene, at, "FIN.LEDGER", "datasets/91/v1.parquet")
    flow = _flow(session, scene, "Mixed", [_src_node("s", scene.orders.id), _src_node("r", rows.id),
                                             _op("j", "join", {"how": "left", "on": ["id"]}), _ds_output("o", "Mixed out", scene.folder.id)],
                   [_edge("s", "j"), _edge("r", "j", "right"), _edge("j", "o")])
    assert _refusal(session, flow, "native").code == "source_not_exportable"
    assert _refusal(session, flow, "duckdb").code == "source_not_exportable"


def test_a_clickhouse_source_on_the_federated_target_still_points_to_the_clickhouse_and_native_targets(session, scene):
    activity = _table(session, scene, scene.ch, "crm.activities", "datasets/92/v1.parquet", "activities")
    e = _refusal(session, _from_a_source(session, scene, activity), "duckdb")
    assert e.code == "not_federable" and "Use the ClickHouse or the native target" in error_message(e, "en")


def test_the_plan_says_when_clickhouse_takes_the_flow_only_into_a_project_that_declares_the_source(session, scene):
    """The download accepts an Oracle source the team's project already declares
    (folder package); the plan disabled ClickHouse without saying it, so the dialog
    could never get there."""
    at = _connection(session, scene, "oracle", "ora")
    flow = _from_a_source(session, scene, _table(session, scene, at, "FIN.LEDGER", "datasets/93/v1.parquet"))
    piano = _piano(session, flow)
    key = f"{at.id}:FIN"
    ch = piano["clickhouse"]
    assert ch["available"] is False and ch["requires"] == {"package": "folder", "declared": [key]}
    assert [(s["key"], s["tables"]) for s in ch["sources"]] == [(key, ["LEDGER"])]
    assert "requires" not in piano["duckdb"] and "requires" not in piano["native"]
    assert [o["key"] for o in d.export_plan(session, flow, "data-prep")["outputs"]] == [f"{flow.id}:o"]   # its materializations too
    # what the plan says, the download keeps
    opz = d.DbtOptions(target="clickhouse", package="folder", sources={key: {"declared": True}})
    assert d.build_export_payload(session, flow, "data-prep", options=opz)["sources"][0]["declared"] is True
    assert _refusal(session, flow, "clickhouse", package="project", sources={key: {"declared": True}}).code == "clickhouse_cannot_read"


def test_an_oracle_query_datasource_has_no_way_through_clickhouse_either(session, scene):
    """A SQL query on Oracle is not a table the team's project could declare."""
    at = _connection(session, scene, "oracle", "ora")
    query = _table(session, scene, at, "SELECT id FROM FIN.LEDGER", "datasets/94/v1.parquet", "ledger q", kind="sql")
    flow = _from_a_source(session, scene, query)
    for target in ("clickhouse", "duckdb", "native"):
        assert _refusal(session, flow, target).code == "source_not_exportable", target
    assert "requires" not in _piano(session, flow)["clickhouse"]


# ── #7: where text_not_translatable sends the user ────────────────────────────
@pytest.mark.parametrize("language", LANGUAGES)
def test_only_text_written_for_clickhouse_is_sent_to_the_clickhouse_target(language):
    """The engine raises text_not_translatable for DuckDB and BigQuery text too, which
    the ClickHouse target translates as well: the advice to use it was wrong there."""
    def sentence(dialect):
        return engine_message({"code": "text_not_translatable", "message": "ENGINE",
                                      "params": {"step": "custom_sql", "dialect": dialect, "detail": "x"}}, language)

    assert "ClickHouse" in sentence("clickhouse")
    for dialect in ("duckdb", "bigquery", "polars"):
        assert "ClickHouse" not in sentence(dialect) and sentence(dialect) != "ENGINE", (dialect, sentence(dialect))


# ── #11: an incomplete step is not said to be unrunnable in Tabularia ─────────
@pytest.mark.parametrize("language", LANGUAGES)
def test_step_invalid_does_not_claim_tabularia_cannot_run_the_step(language):
    """The Polars engine runs a drop of no columns: «Tabularia cannot run it either»
    was false for it. The sentence asks to complete the step or remove it."""
    sentence = translate("step_invalid", {"step": "drop", "detail": "required parameter missing: 'columns'"}, language)
    assert "Tabularia" not in sentence and "'columns'" in sentence


# ── #46: a malformed definition is a reason, not a 500 ────────────────────────
@pytest.mark.parametrize("case", ["node_without_id", "node_not_an_object", "data_not_an_object", "nodes_not_a_list", "not_json"])
def test_a_malformed_definition_is_a_coded_refusal_not_a_500(session, scene, case):
    """Definitions written through the API: the editor always gives a node its id."""
    nodes = [_src_node("s", scene.orders.id), _ds_output("o", "Broken out", scene.folder.id)]
    definition = {"nodes": nodes, "edges": [_edge("s", "o")]}
    if case == "node_without_id":
        nodes.append({"type": "operation", "data": {"opType": "limit", "params": {"n": 1}}})
    elif case == "node_not_an_object":
        nodes.append("limit")
    elif case == "data_not_an_object":     # on the path of the Output: a node off it is never read
        nodes.append({"id": "x", "type": "operation", "data": ["limit"]})
        definition["edges"] = [_edge("s", "x"), _edge("x", "o")]
    elif case == "nodes_not_a_list":
        definition["nodes"] = {"s": nodes[0]}
    flow = _flow(session, scene, "Broken", [], [])
    flow.definition = "{not json" if case == "not_json" else json.dumps(definition)
    session.add(flow)
    session.commit()
    piano = _piano(session, flow)
    assert not any(t["available"] for t in piano.values())
    assert all(t["reason"].startswith("The flow «Broken» cannot be read") for t in piano.values()), piano
    e = _refusal(session, flow)
    assert e.code == "flow_resolve" and e.params["flow"] == "Broken"


@pytest.mark.anyio
async def test_a_malformed_definition_is_a_422_at_the_download(session, scene, fake_engine):
    from app.routes import flows as flows_routes

    flow = _flow(session, scene, "Broken", [{"type": "output", "data": {"destType": "datasource", "name": "X"}}], [])
    with pytest.raises(HTTPException) as e:
        await flows_routes.export_flow_dbt(flow.id, _request(), "duckdb", scene.boss, session)
    assert e.value.status_code == 422 and "cannot be read" in e.value.detail and fake_engine.dbt_exports == []


# ── #19 #20: a one-part name on a Postgres connection without Schema ──────────
def test_one_part_names_on_a_postgres_connection_without_schema_come_with_a_note(session, scene):
    """Tabularia reads `orders` through the role's search_path, dbt reads public.ordini:
    the same table only with the default search_path and no schema named like the role."""
    scene.orders.source_ref = "ordini"
    session.add(scene.orders)
    session.commit()
    flow = _flow(session, scene, "Bare", [_src_node("s", scene.orders.id), _db_output("o", scene.pg.id, "righe")], [_edge("s", "o")])
    for target in ("duckdb", "native", "clickhouse"):
        p = d.build_export_payload(session, flow, "data-prep", target)
        assert p["sources"][0]["schema"] == "public"
        note = [n for n in p["notes"] if "search_path" in n]
        assert len(note) == 1 and "«gestionale»" in note[0] and "`ordini`" in note[0], (target, p["notes"])
        assert ("`righe`" in note[0]) is (target == "native"), (target, note)    # the output lands in public.righe only on native
    scene.pg.db_schema = "vendite"
    session.add(scene.pg)
    session.commit()
    assert not any("search_path" in n for n in d.build_export_payload(session, flow, "data-prep", "native")["notes"])


def test_two_part_names_need_no_search_path_note(session, scene):
    flow = _flow_with_two_outputs(session, scene)
    assert not any("search_path" in n for n in d.build_export_payload(session, flow, "data-prep", "native")["notes"])


# ── #44: a native model never replaces the table Tabularia appends to ─────────
@pytest.mark.parametrize("mode, choice, holds", [
    ("append", "incremental", True), ("append", "table", False), ("append", "view", False),
    ("replace", "table", True), ("replace", "view", False),
])
def test_a_native_model_keeps_the_real_table_only_with_tabularia_s_own_write_mode(session, scene, mode, choice, holds):
    """`table` over an append destination rebuilt Tabularia's accumulated table with the
    last batch only; `view` dropped it for a view."""
    flow = _flow(session, scene, "Load", [_src_node("s", scene.orders.id), _db_output("o", scene.pg.id, "report.righe", mode)], [_edge("s", "o")])
    p = d.build_export_payload(session, flow, "data-prep", options=d.DbtOptions(target="native", materializations={f"{flow.id}:o": choice}))
    m = _models(p)["righe"]
    assert m["materialized"] == choice
    note = [n for n in p["notes"] if n.startswith("The model `righe`")]
    if holds:
        assert (m["schema"], m["alias"]) == ("report", "righe") and not note
    else:
        assert "schema" not in m and "alias" not in m
        assert len(note) == 1 and "project's schema" in note[0] and f"`{choice}`" in note[0], p["notes"]


# ── #22: a native destination longer than Postgres keeps ──────────────────────
@pytest.mark.parametrize("table, mode", [("righe_" + "x" * 70, "replace"), ("righe_" + "à" * 40, "append")])
def test_a_native_postgres_destination_over_63_bytes_is_the_table_postgres_created(session, scene, table, mode):
    """Tabularia writes such a table, and Postgres keeps the first 63 bytes of its name;
    dbt-postgres refused the longer alias."""
    schema = "s" * 70
    flow = _flow(session, scene, "Long", [_src_node("s", scene.orders.id), _db_output("o", scene.pg.id, f"{schema}.{table}", mode)],
                   [_edge("s", "o")])
    m = d.build_export_payload(session, flow, "data-prep", "native")["models"][-1]
    assert m["alias"] == table.encode()[:63].decode("utf-8", "ignore") and len(m["alias"].encode()) <= 63
    assert m["schema"] == "s" * 63


@pytest.mark.parametrize("mode, holds", [("append", True), ("replace", False)])
def test_a_long_multibyte_destination_dbt_postgres_cannot_rebuild_lands_in_the_project_schema(session, scene, mode, holds):
    """dbt-postgres cuts `<alias>__dbt_tmp` and `__dbt_backup` to 63 CHARACTERS, Postgres
    to 63 BYTES: with accents the suffix is cut away, and a `table` model failed with
    «relation … already exists» (seen with real dbt-postgres 1.11). An incremental model
    builds its temporary table elsewhere and appends: it keeps the real table."""
    table = "righe_del_mese_per_regione_e_categoria_" + "à" * 20
    flow = _flow(session, scene, "Accents", [_src_node("s", scene.orders.id), _db_output("o", scene.pg.id, f"report.{table}", mode)],
                   [_edge("s", "o")])
    p = d.build_export_payload(session, flow, "data-prep", "native")
    m = p["models"][-1]
    if holds:
        assert (m["schema"], m["alias"]) == ("report", table.encode()[:63].decode("utf-8", "ignore"))
    else:
        assert "schema" not in m and "alias" not in m
        assert any(n.startswith(f"The model `{m['name']}`") and "63 bytes" in n for n in p["notes"]), p["notes"]


def test_a_native_clickhouse_destination_keeps_its_whole_name(session, scene):
    activity = _table(session, scene, scene.ch, "crm.activities", "datasets/95/v1.parquet", "activities")
    table = "events_" + "x" * 70
    flow = _flow(session, scene, "Ch long", [_src_node("s", activity.id), _db_output("o", scene.ch.id, f"crm.{table}")], [_edge("s", "o")])
    assert d.build_export_payload(session, flow, "data-prep", "native")["models"][-1]["alias"] == table


# ── #2: a name the dbt adapter cannot quote ───────────────────────────────────
@pytest.mark.parametrize("target, table, adapter", [
    ("clickhouse", "o'brien\\x", "dbt-clickhouse"), ("clickhouse", "a`b", "dbt-clickhouse"),
    ("duckdb", 'say "hi"', "dbt-duckdb"), ("native", 'say "hi"', "dbt-postgres"),
])
def test_a_table_name_the_adapter_cannot_quote_is_refused_in_the_plan(session, scene, target, table, adapter):
    """dbt quotes a relation name without escaping it (BaseRelation.quoted): a backtick or
    a backslash breaks dbt-clickhouse, a double quote dbt-postgres and dbt-duckdb (seen
    with real dbt: ClickHouse «Cannot parse escape sequence», DuckDB «zero-length
    delimited identifier»). The plan says it, instead of the team's `dbt run`."""
    scene.orders.source_ref = f"vendite.{table}"
    session.add(scene.orders)
    session.commit()
    flow = _flow_with_two_outputs(session, scene)
    e = _refusal(session, flow, target)
    assert e.code == "name_not_quotable" and e.params["name"] == table and e.params["adapter"] == adapter
    assert not _piano(session, flow)[target]["available"]


@pytest.mark.parametrize("target, table", [
    ("clickhouse", 'say "hi"'), ("duckdb", "o\\x"), ("duckdb", "a`b"), ("native", "o\\x"),
])
def test_names_the_adapter_quotes_stay_exportable(session, scene, target, table):
    scene.orders.source_ref = f"vendite.{table}"
    session.add(scene.orders)
    session.commit()
    assert _piano(session, _flow_with_two_outputs(session, scene))[target]["available"]


def test_a_native_clickhouse_name_with_a_backslash_is_refused(session, scene):
    activity = _table(session, scene, scene.ch, "crm.act\\ivities", "datasets/96/v1.parquet", "activities")
    e = _refusal(session, _from_a_source(session, scene, activity), "native")
    assert e.code == "name_not_quotable" and e.params["adapter"] == "dbt-clickhouse"
    flow = _flow(session, scene, "Ch out", [_src_node("s", scene.orders.id), _db_output("o", scene.pg.id, 'report."righe"')], [_edge("s", "o")])
    assert _refusal(session, flow, "native").params["name"] == '"righe"'


def _native_output(session, scene, db, table, mode="replace"):
    """A flow whose database output is on its source connection: on the native target the
    model keeps the output's real schema and table."""
    conn = scene.pg if db == "postgresql" else scene.ch
    source = scene.orders if db == "postgresql" else _table(session, scene, scene.ch, "crm.activities", "datasets/97/v1.parquet", "activities")
    return _flow(session, scene, "Own table", [_src_node("s", source.id), _db_output("o", conn.id, table, mode)], [_edge("s", "o")])


@pytest.mark.parametrize("mode", ["replace", "append"])
@pytest.mark.parametrize("db, table, name, character", [
    ("postgresql", "zz.o'brien", "o'brien", "'"), ("postgresql", "zz'q.t", "zz'q", "'"), ("postgresql", "zz\\b.t", "zz\\b", "\\"),
    ("clickhouse", "crm.o'brien", "o'brien", "'"), ("clickhouse", "c'rm.t", "c'rm", "'"),
])
def test_a_quote_in_a_native_output_s_own_table_or_schema_is_refused_in_the_plan(session, scene, db, table, name, character, mode):
    """A source is read only through source(); a model's OWN table and schema are also written
    between `'`, unescaped, in the queries the adapter runs to list a schema's relations and a
    table's columns, and dbt-postgres compares the schema with ILIKE, where a backslash escapes
    (real dbt 1.12.5: Postgres failed at the first `dbt run` for a quoted schema, at an
    incremental's second run and at `dbt docs generate` for a quoted table, at the second run for
    a backslash in the schema; ClickHouse at the first run). The engine refuses these names
    (name_not_quotable); the plan said «native available» and the download answered 422."""
    flow = _native_output(session, scene, db, table, mode)
    adapter = "dbt-postgres" if db == "postgresql" else "dbt-clickhouse"
    e = _refusal(session, flow, "native")
    assert (e.code, e.params) == ("name_not_quotable", {"name": name, "character": character, "adapter": adapter})
    piano = _piano(session, flow)
    assert piano["native"]["available"] is False
    assert piano["native"]["reason"] == translate("name_not_quotable", e.params, "en")
    if db == "postgresql":   # only the native target writes the output's own table: the others stay offered
        assert piano["duckdb"]["available"] and piano["clickhouse"]["available"]


@pytest.mark.parametrize("db, table", [("postgresql", "zz.o\\b"), ("postgresql", "zz.o`b"), ("clickhouse", 'crm.o"b')])
def test_a_native_output_name_the_adapter_writes_stays_exportable(session, scene, db, table):
    """What each adapter does write in a model's own name stays offered: a backslash in a
    Postgres table (not its schema), a backtick on Postgres, a double quote on ClickHouse."""
    flow = _native_output(session, scene, db, table)
    assert _piano(session, flow)["native"]["available"]
    [m] = [m for m in d.build_export_payload(session, flow, "data-prep", "native")["models"] if "alias" in m]
    assert (m["schema"], m["alias"]) == tuple(table.split("."))


# ── #45: an upstream datasource two Outputs publish ───────────────────────────
def test_an_upstream_datasource_two_outputs_publish_is_refused(session, scene):
    """Which of the two wrote it depends on the order they ran in: the project took the
    first in node order, maybe the other branch's logic, without a word."""
    monte = _flow(session, scene, "Twin", [
        _src_node("s", scene.orders.id), _op("f1", "filter", {"column": "importo", "operator": "gt", "value": 100}),
        _ds_output("oa", "Shared", scene.folder.id), _op("f2", "filter", {"column": "importo", "operator": "gt", "value": 0}),
        _ds_output("ob", "Shared", scene.folder.id),
    ], [_edge("s", "f1"), _edge("f1", "oa"), _edge("s", "f2"), _edge("f2", "ob")])
    shared = make_datasource(session, name="Shared", project_id=scene.folder.id, key="datasets/97/v1.parquet", kind="flow", flow_id=monte.id)
    downstream = _from_a_source(session, scene, shared, "Reader")
    e = _refusal(session, downstream)
    assert (e.code, e.params) == ("upstream_output_ambiguous", {"flow": "Twin", "source": "Shared"})
    assert not any(t["available"] for t in _piano(session, downstream).values())
    # the flow itself still exports both outputs
    assert [m["name"] for m in d.build_export_payload(session, monte, "data-prep")["models"]] == ["shared", "shared_twin"]


# ── #55 #59: the plan says what the AI wrote for this version ─────────────────
def test_the_plan_says_which_ai_texts_are_kept_for_the_flow_version(session, scene, monkeypatch):
    """With the AI switched off the dialog hid the AI options, so a re-export dropped the
    texts kept for the version and the zip changed."""
    from app.routes import flows as flows_routes

    flow = _flow_with_two_outputs(session, scene)
    _version(session, flow)
    monkeypatch.setattr(dbt_ai, "ai_model", lambda s: None)
    piano = lambda: flows_routes.export_flow_dbt_plan(flow.id, _request(), scene.boss, session)["ai"]
    assert piano() == {"available": False, "model": None, "stored": {"descriptions": False, "translations": False, "models": []}}
    dbt_ai._remember(session, flow.id, 1, "describe", {"col:ds:1:id:Int64": "The id", "col:ds:1:x:Int64": ""}, "old-model")
    dbt_ai._remember(session, flow.id, 1, "claim", {"describe": "123"}, "")
    assert piano()["stored"] == {"descriptions": True, "translations": False, "models": ["old-model"]}
    dbt_ai._remember(session, flow.id, 1, "translate", {"sql:abc": "SELECT 1"}, "other-model")
    assert piano()["stored"] == {"descriptions": True, "translations": True, "models": ["old-model", "other-model"]}
    _version(session, flow, 2)   # a new version starts with nothing kept
    assert piano()["stored"] == {"descriptions": False, "translations": False, "models": []}


# ── #52: the edges of the translation retry ───────────────────────────────────
def test_forgetting_a_rejected_proposal_keeps_the_one_another_export_stored_since(session, scene):
    flow = _flow_with_two_outputs(session, scene)
    _version(session, flow)
    key = dbt_ai.translation_key("SELECT 1", "duckdb", "clickhouse")
    dbt_ai._remember(session, flow.id, 1, "translate", {key: "SELECT fresh"}, "m")
    dbt_ai.forget(session, flow, key, "SELECT rejected")     # this export's proposal, replaced meanwhile
    texts = lambda: session.exec(select(DbtAiText.text).where(DbtAiText.kind == "translate")).all()
    assert texts() == ["SELECT fresh"]
    dbt_ai.forget(session, flow, key, "SELECT fresh")
    assert texts() == []


@pytest.mark.anyio
async def test_a_stored_empty_proposal_is_asked_again_and_an_empty_one_is_never_kept(session, scene, ai):
    flow = _flow_with_two_outputs(session, scene)
    _version(session, flow)
    params = _translation_refusal("SELECT id, canale FROM __s_0 QUALIFY strano")[1]["detail"]["params"]
    key = dbt_ai.translation_key(params["sql"], "duckdb", "clickhouse")
    dbt_ai._remember(session, flow.id, 1, "translate", {key: ""}, "old")
    proposal = ai.sql
    sql, k, model = await dbt_ai.translate(session, scene.boss, flow, params)
    assert (sql, k, model, ai.calls) == (proposal, key, "finto-1", 1)
    texts = lambda: session.exec(select(DbtAiText.text).where(DbtAiText.kind == "translate")).all()
    assert texts() == [proposal]
    ai.sql = "  ;"      # an empty answer: the engine rejects it, and it is never kept to be served again
    assert (await dbt_ai.translate(session, scene.boss, flow, {**params, "sql": "SELECT 2 QUALIFY strano"}))[0] == ""
    assert texts() == [proposal] and ai.calls == 2


# ── #57: the engine is not asked again past the export's budget ───────────────
@pytest.mark.anyio
async def test_the_engine_is_not_asked_again_once_the_export_s_budget_is_spent(session, scene, ai, fake_engine, monkeypatch):
    """The AI calls were bounded, the engine calls between them were not: up to eleven,
    each re-reading the seeds, and the ingress cut the request with a bare 504."""
    from app.routes import flows as flows_routes

    flow = _flow_with_two_outputs(session, scene)
    _version(session, flow)
    refusal = _translation_refusal("SELECT id, canale FROM __s_0 QUALIFY strano")
    key = dbt_ai.translation_key(refusal[1]["detail"]["params"]["sql"], "duckdb", "clickhouse")
    dbt_ai._remember(session, flow.id, 1, "translate", {key: "SELECT id, canale FROM __s_0"}, "finto-1")   # kept: no AI call
    monkeypatch.setattr(dbt_ai, "new_deadline", lambda: time.monotonic() - 1)
    fake_engine.dbt_export_script = [refusal, None]
    with pytest.raises(HTTPException) as e:
        await flows_routes.export_flow_dbt_with_options(flow.id, d.DbtOptions(target="clickhouse", ai_translations=True),
                                                       _request(), scene.boss, session)
    assert e.value.status_code == 422 and "timed out" in e.value.detail and len(fake_engine.dbt_exports) == 1
    # an export that needs no translation is never stopped by the AI budget
    fake_engine.dbt_export_script = [None]
    r = await flows_routes.export_flow_dbt_with_options(flow.id, d.DbtOptions(target="clickhouse", ai_translations=True),
                                                       _request(), scene.boss, session)
    assert r.status_code == 200 and ai.calls == 0


# ── #48 (handoff of the dialog lane): the base each default source name is made from ──
def test_the_plan_gives_each_source_the_base_its_default_name_was_made_from(session, scene):
    """The dialog names untyped sources as the gateway does: from the UNSUFFIXED default,
    moved aside with `_2`, `_3`… once typed names change the collisions. default_name
    already carries the suffix of the collisions with nothing typed."""
    nodes, edges = [], []
    for i, schema in enumerate(("Sales", "sales", "altro")):
        ds = _table(session, scene, scene.pg, f"{schema}.t{i}", f"datasets/6{i}/v1.parquet", f"s{i}")
        nodes += [_src_node(f"s{i}", ds.id), _ds_output(f"o{i}", f"Out {i}", scene.folder.id)]
        edges.append(_edge(f"s{i}", f"o{i}"))
    piano = _piano(session, _flow(session, scene, "Three schemas", nodes, edges))
    g = scene.pg.id
    expected_items = {"duckdb": f"db_{g}_", "clickhouse": f"db_{g}_", "native": "src_"}
    for target, head in expected_items.items():
        sources = piano[target]["sources"]
        assert [s["default_name"] for s in sources] == [f"{head}sales", f"{head}sales_2", f"{head}altro"], target
        assert [s["default_base"] for s in sources] == [f"{head}sales", f"{head}sales", f"{head}altro"], target
