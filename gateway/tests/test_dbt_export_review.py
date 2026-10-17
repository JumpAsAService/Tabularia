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

from app.core.lingua import LINGUE
from app.models import Connection, DbtAiText
from app.services import dbt_ai
from app.services import dbt_export as d
from app.services.messaggi_dbt import messaggio_dell_engine, messaggio_dell_errore, traduci
from tests.conftest import make_datasource
from tests.test_dbt_export import _edge, _flusso_a_due_uscite, _nodo_src, _op, _richiesta, scena  # noqa: F401 (scena is a fixture)
from tests.test_dbt_export_ai import _rifiuto_traduzione, _versione, ai  # noqa: F401 (ai is a fixture)
from tests.test_dbt_export_resolution import _flusso, _modelli, _rifiuto, _uscita_db, _uscita_ds


def _piano(session, flow, lingua="en"):
    return {t["id"]: t for t in d.piano_export(session, flow, "data-prep", lingua)["targets"]}


def _connessione(session, scena, db_type, nome="contabilita", **kw):
    c = Connection(name=nome, db_type=db_type, project_id=scena.cartella.id, owner_id=scena.capo.id, host=kw.get("host", "x.it"),
                   port=None, database=kw.get("database", "DB"), db_schema=kw.get("db_schema", ""), username="u")
    session.add(c)
    session.commit()
    return c


def _tabella(session, scena, conn, ref, key, nome="ledger", tipo="table"):
    return make_datasource(session, name=nome, project_id=scena.cartella.id, key=key, kind="database", connection_id=conn.id,
                           source_type=tipo, source_ref=ref, columns=json.dumps([{"name": "id", "dtype": "Int64"}]))


def _da_una_sorgente(session, scena, ds, nome="Flow"):
    return _flusso(session, scena, nome, [_nodo_src("s", ds.id), _uscita_ds("o", f"{nome} out", scena.cartella.id)], [_edge("s", "o")])


# ── #49: a source no dbt target reads ─────────────────────────────────────────
@pytest.mark.parametrize("db_type", ["oracle", "sqlserver", "trino"])
def test_a_source_no_dbt_target_reads_never_points_to_a_target_that_refuses_too(session, scena, db_type):
    """The federated reason said «use the ClickHouse or the native target», the
    ClickHouse one «use another target»: for an Oracle flow every target refuses."""
    conn = _connessione(session, scena, db_type)
    flow = _da_una_sorgente(session, scena, _tabella(session, scena, conn, "FIN.LEDGER", "datasets/90/v1.parquet"))
    for target in ("duckdb", "native"):
        e = _rifiuto(session, flow, target)
        assert (e.code, e.params) == ("source_not_exportable", {"connection": "contabilita", "db_type": db_type}), target
    for lingua in LINGUE:
        piano = _piano(session, flow, lingua)
        assert not any(t["available"] for t in piano.values())
        for target in ("duckdb", "native"):
            assert piano[target]["reason"] == traduci("source_not_exportable", {"connection": "contabilita", "db_type": db_type}, lingua)
    piano = _piano(session, flow)
    assert "no dbt target reads" in piano["duckdb"]["reason"] and "PostgreSQL or MySQL table" in piano["duckdb"]["reason"]
    assert "Use another target" not in piano["clickhouse"]["reason"]
    assert "already declares" in piano["clickhouse"]["reason"] and "Folder for an existing project" in piano["clickhouse"]["reason"]


def test_with_an_oracle_and_a_postgres_source_the_native_reason_is_the_oracle_one(session, scena):
    """«Use the ClickHouse or the federated target» (one connection only) is no way
    out for a flow that also reads Oracle."""
    ora = _connessione(session, scena, "oracle", "ora")
    righe = _tabella(session, scena, ora, "FIN.LEDGER", "datasets/91/v1.parquet")
    flow = _flusso(session, scena, "Mixed", [_nodo_src("s", scena.ordini.id), _nodo_src("r", righe.id),
                                             _op("j", "join", {"how": "left", "on": ["id"]}), _uscita_ds("o", "Mixed out", scena.cartella.id)],
                   [_edge("s", "j"), _edge("r", "j", "right"), _edge("j", "o")])
    assert _rifiuto(session, flow, "native").code == "source_not_exportable"
    assert _rifiuto(session, flow, "duckdb").code == "source_not_exportable"


def test_a_clickhouse_source_on_the_federated_target_still_points_to_the_clickhouse_and_native_targets(session, scena):
    attivita = _tabella(session, scena, scena.ch, "crm.activities", "datasets/92/v1.parquet", "activities")
    e = _rifiuto(session, _da_una_sorgente(session, scena, attivita), "duckdb")
    assert e.code == "not_federable" and "Use the ClickHouse or the native target" in messaggio_dell_errore(e, "en")


def test_the_plan_says_when_clickhouse_takes_the_flow_only_into_a_project_that_declares_the_source(session, scena):
    """The download accepts an Oracle source the team's project already declares
    (folder package); the plan disabled ClickHouse without saying it, so the dialog
    could never get there."""
    ora = _connessione(session, scena, "oracle", "ora")
    flow = _da_una_sorgente(session, scena, _tabella(session, scena, ora, "FIN.LEDGER", "datasets/93/v1.parquet"))
    piano = _piano(session, flow)
    chiave = f"{ora.id}:FIN"
    ch = piano["clickhouse"]
    assert ch["available"] is False and ch["requires"] == {"package": "folder", "declared": [chiave]}
    assert [(s["key"], s["tables"]) for s in ch["sources"]] == [(chiave, ["LEDGER"])]
    assert "requires" not in piano["duckdb"] and "requires" not in piano["native"]
    assert [o["key"] for o in d.piano_export(session, flow, "data-prep")["outputs"]] == [f"{flow.id}:o"]   # its materializations too
    # what the plan says, the download keeps
    opz = d.OpzioniDbt(target="clickhouse", package="folder", sources={chiave: {"declared": True}})
    assert d.build_export_payload(session, flow, "data-prep", opzioni=opz)["sources"][0]["declared"] is True
    assert _rifiuto(session, flow, "clickhouse", package="project", sources={chiave: {"declared": True}}).code == "clickhouse_cannot_read"


def test_an_oracle_query_datasource_has_no_way_through_clickhouse_either(session, scena):
    """A SQL query on Oracle is not a table the team's project could declare."""
    ora = _connessione(session, scena, "oracle", "ora")
    query = _tabella(session, scena, ora, "SELECT id FROM FIN.LEDGER", "datasets/94/v1.parquet", "ledger q", tipo="sql")
    flow = _da_una_sorgente(session, scena, query)
    for target in ("clickhouse", "duckdb", "native"):
        assert _rifiuto(session, flow, target).code == "source_not_exportable", target
    assert "requires" not in _piano(session, flow)["clickhouse"]


# ── #7: where text_not_translatable sends the user ────────────────────────────
@pytest.mark.parametrize("lingua", LINGUE)
def test_only_text_written_for_clickhouse_is_sent_to_the_clickhouse_target(lingua):
    """The engine raises text_not_translatable for DuckDB and BigQuery text too, which
    the ClickHouse target translates as well: the advice to use it was wrong there."""
    def frase(dialetto):
        return messaggio_dell_engine({"code": "text_not_translatable", "message": "ENGINE",
                                      "params": {"step": "custom_sql", "dialect": dialetto, "detail": "x"}}, lingua)

    assert "ClickHouse" in frase("clickhouse")
    for dialetto in ("duckdb", "bigquery", "polars"):
        assert "ClickHouse" not in frase(dialetto) and frase(dialetto) != "ENGINE", (dialetto, frase(dialetto))


# ── #11: an incomplete step is not said to be unrunnable in Tabularia ─────────
@pytest.mark.parametrize("lingua", LINGUE)
def test_step_invalid_does_not_claim_tabularia_cannot_run_the_step(lingua):
    """The Polars engine runs a drop of no columns: «Tabularia cannot run it either»
    was false for it. The sentence asks to complete the step or remove it."""
    frase = traduci("step_invalid", {"step": "drop", "detail": "required parameter missing: 'columns'"}, lingua)
    assert "Tabularia" not in frase and "'columns'" in frase


# ── #46: a malformed definition is a reason, not a 500 ────────────────────────
@pytest.mark.parametrize("caso", ["node_without_id", "node_not_an_object", "data_not_an_object", "nodes_not_a_list", "not_json"])
def test_a_malformed_definition_is_a_coded_refusal_not_a_500(session, scena, caso):
    """Definitions written through the API: the editor always gives a node its id."""
    nodi = [_nodo_src("s", scena.ordini.id), _uscita_ds("o", "Broken out", scena.cartella.id)]
    definizione = {"nodes": nodi, "edges": [_edge("s", "o")]}
    if caso == "node_without_id":
        nodi.append({"type": "operation", "data": {"opType": "limit", "params": {"n": 1}}})
    elif caso == "node_not_an_object":
        nodi.append("limit")
    elif caso == "data_not_an_object":     # on the path of the Output: a node off it is never read
        nodi.append({"id": "x", "type": "operation", "data": ["limit"]})
        definizione["edges"] = [_edge("s", "x"), _edge("x", "o")]
    elif caso == "nodes_not_a_list":
        definizione["nodes"] = {"s": nodi[0]}
    flow = _flusso(session, scena, "Broken", [], [])
    flow.definition = "{not json" if caso == "not_json" else json.dumps(definizione)
    session.add(flow)
    session.commit()
    piano = _piano(session, flow)
    assert not any(t["available"] for t in piano.values())
    assert all(t["reason"].startswith("The flow «Broken» cannot be read") for t in piano.values()), piano
    e = _rifiuto(session, flow)
    assert e.code == "flow_resolve" and e.params["flow"] == "Broken"


@pytest.mark.anyio
async def test_a_malformed_definition_is_a_422_at_the_download(session, scena, fake_engine):
    from app.routes import flows as flows_routes

    flow = _flusso(session, scena, "Broken", [{"type": "output", "data": {"destType": "datasource", "name": "X"}}], [])
    with pytest.raises(HTTPException) as e:
        await flows_routes.export_flow_dbt(flow.id, _richiesta(), "duckdb", scena.capo, session)
    assert e.value.status_code == 422 and "cannot be read" in e.value.detail and fake_engine.dbt_exports == []


# ── #19 #20: a one-part name on a Postgres connection without Schema ──────────
def test_one_part_names_on_a_postgres_connection_without_schema_come_with_a_note(session, scena):
    """Tabularia reads `ordini` through the role's search_path, dbt reads public.ordini:
    the same table only with the default search_path and no schema named like the role."""
    scena.ordini.source_ref = "ordini"
    session.add(scena.ordini)
    session.commit()
    flow = _flusso(session, scena, "Bare", [_nodo_src("s", scena.ordini.id), _uscita_db("o", scena.pg.id, "righe")], [_edge("s", "o")])
    for target in ("duckdb", "native", "clickhouse"):
        p = d.build_export_payload(session, flow, "data-prep", target)
        assert p["sources"][0]["schema"] == "public"
        note = [n for n in p["notes"] if "search_path" in n]
        assert len(note) == 1 and "«gestionale»" in note[0] and "`ordini`" in note[0], (target, p["notes"])
        assert ("`righe`" in note[0]) is (target == "native"), (target, note)    # the output lands in public.righe only on native
    scena.pg.db_schema = "vendite"
    session.add(scena.pg)
    session.commit()
    assert not any("search_path" in n for n in d.build_export_payload(session, flow, "data-prep", "native")["notes"])


def test_two_part_names_need_no_search_path_note(session, scena):
    flow = _flusso_a_due_uscite(session, scena)
    assert not any("search_path" in n for n in d.build_export_payload(session, flow, "data-prep", "native")["notes"])


# ── #44: a native model never replaces the table Tabularia appends to ─────────
@pytest.mark.parametrize("mode, scelta, tiene", [
    ("append", "incremental", True), ("append", "table", False), ("append", "view", False),
    ("replace", "table", True), ("replace", "view", False),
])
def test_a_native_model_keeps_the_real_table_only_with_tabularia_s_own_write_mode(session, scena, mode, scelta, tiene):
    """`table` over an append destination rebuilt Tabularia's accumulated table with the
    last batch only; `view` dropped it for a view."""
    flow = _flusso(session, scena, "Load", [_nodo_src("s", scena.ordini.id), _uscita_db("o", scena.pg.id, "report.righe", mode)], [_edge("s", "o")])
    p = d.build_export_payload(session, flow, "data-prep", opzioni=d.OpzioniDbt(target="native", materializations={f"{flow.id}:o": scelta}))
    m = _modelli(p)["righe"]
    assert m["materialized"] == scelta
    nota = [n for n in p["notes"] if n.startswith("The model `righe`")]
    if tiene:
        assert (m["schema"], m["alias"]) == ("report", "righe") and not nota
    else:
        assert "schema" not in m and "alias" not in m
        assert len(nota) == 1 and "project's schema" in nota[0] and f"`{scelta}`" in nota[0], p["notes"]


# ── #22: a native destination longer than Postgres keeps ──────────────────────
@pytest.mark.parametrize("tabella, mode", [("righe_" + "x" * 70, "replace"), ("righe_" + "à" * 40, "append")])
def test_a_native_postgres_destination_over_63_bytes_is_the_table_postgres_created(session, scena, tabella, mode):
    """Tabularia writes such a table, and Postgres keeps the first 63 bytes of its name;
    dbt-postgres refused the longer alias."""
    schema = "s" * 70
    flow = _flusso(session, scena, "Long", [_nodo_src("s", scena.ordini.id), _uscita_db("o", scena.pg.id, f"{schema}.{tabella}", mode)],
                   [_edge("s", "o")])
    m = d.build_export_payload(session, flow, "data-prep", "native")["models"][-1]
    assert m["alias"] == tabella.encode()[:63].decode("utf-8", "ignore") and len(m["alias"].encode()) <= 63
    assert m["schema"] == "s" * 63


@pytest.mark.parametrize("mode, tiene", [("append", True), ("replace", False)])
def test_a_long_multibyte_destination_dbt_postgres_cannot_rebuild_lands_in_the_project_schema(session, scena, mode, tiene):
    """dbt-postgres cuts `<alias>__dbt_tmp` and `__dbt_backup` to 63 CHARACTERS, Postgres
    to 63 BYTES: with accents the suffix is cut away, and a `table` model failed with
    «relation … already exists» (seen with real dbt-postgres 1.11). An incremental model
    builds its temporary table elsewhere and appends: it keeps the real table."""
    tabella = "righe_del_mese_per_regione_e_categoria_" + "à" * 20
    flow = _flusso(session, scena, "Accents", [_nodo_src("s", scena.ordini.id), _uscita_db("o", scena.pg.id, f"report.{tabella}", mode)],
                   [_edge("s", "o")])
    p = d.build_export_payload(session, flow, "data-prep", "native")
    m = p["models"][-1]
    if tiene:
        assert (m["schema"], m["alias"]) == ("report", tabella.encode()[:63].decode("utf-8", "ignore"))
    else:
        assert "schema" not in m and "alias" not in m
        assert any(n.startswith(f"The model `{m['name']}`") and "63 bytes" in n for n in p["notes"]), p["notes"]


def test_a_native_clickhouse_destination_keeps_its_whole_name(session, scena):
    attivita = _tabella(session, scena, scena.ch, "crm.activities", "datasets/95/v1.parquet", "activities")
    tabella = "events_" + "x" * 70
    flow = _flusso(session, scena, "Ch long", [_nodo_src("s", attivita.id), _uscita_db("o", scena.ch.id, f"crm.{tabella}")], [_edge("s", "o")])
    assert d.build_export_payload(session, flow, "data-prep", "native")["models"][-1]["alias"] == tabella


# ── #2: a name the dbt adapter cannot quote ───────────────────────────────────
@pytest.mark.parametrize("target, tabella, adapter", [
    ("clickhouse", "o'brien\\x", "dbt-clickhouse"), ("clickhouse", "a`b", "dbt-clickhouse"),
    ("duckdb", 'say "hi"', "dbt-duckdb"), ("native", 'say "hi"', "dbt-postgres"),
])
def test_a_table_name_the_adapter_cannot_quote_is_refused_in_the_plan(session, scena, target, tabella, adapter):
    """dbt quotes a relation name without escaping it (BaseRelation.quoted): a backtick or
    a backslash breaks dbt-clickhouse, a double quote dbt-postgres and dbt-duckdb (seen
    with real dbt: ClickHouse «Cannot parse escape sequence», DuckDB «zero-length
    delimited identifier»). The plan says it, instead of the team's `dbt run`."""
    scena.ordini.source_ref = f"vendite.{tabella}"
    session.add(scena.ordini)
    session.commit()
    flow = _flusso_a_due_uscite(session, scena)
    e = _rifiuto(session, flow, target)
    assert e.code == "name_not_quotable" and e.params["name"] == tabella and e.params["adapter"] == adapter
    assert not _piano(session, flow)[target]["available"]


@pytest.mark.parametrize("target, tabella", [
    ("clickhouse", 'say "hi"'), ("duckdb", "o\\x"), ("duckdb", "a`b"), ("native", "o\\x"),
])
def test_names_the_adapter_quotes_stay_exportable(session, scena, target, tabella):
    scena.ordini.source_ref = f"vendite.{tabella}"
    session.add(scena.ordini)
    session.commit()
    assert _piano(session, _flusso_a_due_uscite(session, scena))[target]["available"]


def test_a_native_clickhouse_name_with_a_backslash_is_refused(session, scena):
    attivita = _tabella(session, scena, scena.ch, "crm.act\\ivities", "datasets/96/v1.parquet", "activities")
    e = _rifiuto(session, _da_una_sorgente(session, scena, attivita), "native")
    assert e.code == "name_not_quotable" and e.params["adapter"] == "dbt-clickhouse"
    flow = _flusso(session, scena, "Ch out", [_nodo_src("s", scena.ordini.id), _uscita_db("o", scena.pg.id, 'report."righe"')], [_edge("s", "o")])
    assert _rifiuto(session, flow, "native").params["name"] == '"righe"'


def _uscita_nativa(session, scena, db, tabella, mode="replace"):
    """A flow whose database output is on its source connection: on the native target the
    model keeps the output's real schema and table."""
    conn = scena.pg if db == "postgresql" else scena.ch
    sorgente = scena.ordini if db == "postgresql" else _tabella(session, scena, scena.ch, "crm.activities", "datasets/97/v1.parquet", "activities")
    return _flusso(session, scena, "Own table", [_nodo_src("s", sorgente.id), _uscita_db("o", conn.id, tabella, mode)], [_edge("s", "o")])


@pytest.mark.parametrize("mode", ["replace", "append"])
@pytest.mark.parametrize("db, tabella, nome, carattere", [
    ("postgresql", "zz.o'brien", "o'brien", "'"), ("postgresql", "zz'q.t", "zz'q", "'"), ("postgresql", "zz\\b.t", "zz\\b", "\\"),
    ("clickhouse", "crm.o'brien", "o'brien", "'"), ("clickhouse", "c'rm.t", "c'rm", "'"),
])
def test_a_quote_in_a_native_output_s_own_table_or_schema_is_refused_in_the_plan(session, scena, db, tabella, nome, carattere, mode):
    """A source is read only through source(); a model's OWN table and schema are also written
    between `'`, unescaped, in the queries the adapter runs to list a schema's relations and a
    table's columns, and dbt-postgres compares the schema with ILIKE, where a backslash escapes
    (real dbt 1.12.5: Postgres failed at the first `dbt run` for a quoted schema, at an
    incremental's second run and at `dbt docs generate` for a quoted table, at the second run for
    a backslash in the schema; ClickHouse at the first run). The engine refuses these names
    (name_not_quotable); the plan said «native available» and the download answered 422."""
    flow = _uscita_nativa(session, scena, db, tabella, mode)
    adapter = "dbt-postgres" if db == "postgresql" else "dbt-clickhouse"
    e = _rifiuto(session, flow, "native")
    assert (e.code, e.params) == ("name_not_quotable", {"name": nome, "character": carattere, "adapter": adapter})
    piano = _piano(session, flow)
    assert piano["native"]["available"] is False
    assert piano["native"]["reason"] == traduci("name_not_quotable", e.params, "en")
    if db == "postgresql":   # only the native target writes the output's own table: the others stay offered
        assert piano["duckdb"]["available"] and piano["clickhouse"]["available"]


@pytest.mark.parametrize("db, tabella", [("postgresql", "zz.o\\b"), ("postgresql", "zz.o`b"), ("clickhouse", 'crm.o"b')])
def test_a_native_output_name_the_adapter_writes_stays_exportable(session, scena, db, tabella):
    """What each adapter does write in a model's own name stays offered: a backslash in a
    Postgres table (not its schema), a backtick on Postgres, a double quote on ClickHouse."""
    flow = _uscita_nativa(session, scena, db, tabella)
    assert _piano(session, flow)["native"]["available"]
    [m] = [m for m in d.build_export_payload(session, flow, "data-prep", "native")["models"] if "alias" in m]
    assert (m["schema"], m["alias"]) == tuple(tabella.split("."))


# ── #45: an upstream datasource two Outputs publish ───────────────────────────
def test_an_upstream_datasource_two_outputs_publish_is_refused(session, scena):
    """Which of the two wrote it depends on the order they ran in: the project took the
    first in node order, maybe the other branch's logic, without a word."""
    monte = _flusso(session, scena, "Twin", [
        _nodo_src("s", scena.ordini.id), _op("f1", "filter", {"column": "importo", "operator": "gt", "value": 100}),
        _uscita_ds("oa", "Shared", scena.cartella.id), _op("f2", "filter", {"column": "importo", "operator": "gt", "value": 0}),
        _uscita_ds("ob", "Shared", scena.cartella.id),
    ], [_edge("s", "f1"), _edge("f1", "oa"), _edge("s", "f2"), _edge("f2", "ob")])
    condivisa = make_datasource(session, name="Shared", project_id=scena.cartella.id, key="datasets/97/v1.parquet", kind="flow", flow_id=monte.id)
    valle = _da_una_sorgente(session, scena, condivisa, "Reader")
    e = _rifiuto(session, valle)
    assert (e.code, e.params) == ("upstream_output_ambiguous", {"flow": "Twin", "source": "Shared"})
    assert not any(t["available"] for t in _piano(session, valle).values())
    # the flow itself still exports both outputs
    assert [m["name"] for m in d.build_export_payload(session, monte, "data-prep")["models"]] == ["shared", "shared_twin"]


# ── #55 #59: the plan says what the AI wrote for this version ─────────────────
def test_the_plan_says_which_ai_texts_are_kept_for_the_flow_version(session, scena, monkeypatch):
    """With the AI switched off the dialog hid the AI options, so a re-export dropped the
    texts kept for the version and the zip changed."""
    from app.routes import flows as flows_routes

    flow = _flusso_a_due_uscite(session, scena)
    _versione(session, flow)
    monkeypatch.setattr(dbt_ai, "modello_ai", lambda s: None)
    piano = lambda: flows_routes.export_flow_dbt_plan(flow.id, _richiesta(), scena.capo, session)["ai"]
    assert piano() == {"available": False, "model": None, "stored": {"descriptions": False, "translations": False, "models": []}}
    dbt_ai._ricorda(session, flow.id, 1, "describe", {"col:ds:1:id:Int64": "The id", "col:ds:1:x:Int64": ""}, "old-model")
    dbt_ai._ricorda(session, flow.id, 1, "claim", {"describe": "123"}, "")
    assert piano()["stored"] == {"descriptions": True, "translations": False, "models": ["old-model"]}
    dbt_ai._ricorda(session, flow.id, 1, "translate", {"sql:abc": "SELECT 1"}, "other-model")
    assert piano()["stored"] == {"descriptions": True, "translations": True, "models": ["old-model", "other-model"]}
    _versione(session, flow, 2)   # a new version starts with nothing kept
    assert piano()["stored"] == {"descriptions": False, "translations": False, "models": []}


# ── #52: the edges of the translation retry ───────────────────────────────────
def test_forgetting_a_rejected_proposal_keeps_the_one_another_export_stored_since(session, scena):
    flow = _flusso_a_due_uscite(session, scena)
    _versione(session, flow)
    chiave = dbt_ai.chiave_traduzione("SELECT 1", "duckdb", "clickhouse")
    dbt_ai._ricorda(session, flow.id, 1, "translate", {chiave: "SELECT fresh"}, "m")
    dbt_ai.dimentica(session, flow, chiave, "SELECT rejected")     # this export's proposal, replaced meanwhile
    testi = lambda: session.exec(select(DbtAiText.text).where(DbtAiText.kind == "translate")).all()
    assert testi() == ["SELECT fresh"]
    dbt_ai.dimentica(session, flow, chiave, "SELECT fresh")
    assert testi() == []


@pytest.mark.anyio
async def test_a_stored_empty_proposal_is_asked_again_and_an_empty_one_is_never_kept(session, scena, ai):
    flow = _flusso_a_due_uscite(session, scena)
    _versione(session, flow)
    params = _rifiuto_traduzione("SELECT id, canale FROM __s_0 QUALIFY strano")[1]["detail"]["params"]
    chiave = dbt_ai.chiave_traduzione(params["sql"], "duckdb", "clickhouse")
    dbt_ai._ricorda(session, flow.id, 1, "translate", {chiave: ""}, "old")
    proposta = ai.sql
    sql, k, modello = await dbt_ai.traduci(session, scena.capo, flow, params)
    assert (sql, k, modello, ai.calls) == (proposta, chiave, "finto-1", 1)
    testi = lambda: session.exec(select(DbtAiText.text).where(DbtAiText.kind == "translate")).all()
    assert testi() == [proposta]
    ai.sql = "  ;"      # an empty answer: the engine rejects it, and it is never kept to be served again
    assert (await dbt_ai.traduci(session, scena.capo, flow, {**params, "sql": "SELECT 2 QUALIFY strano"}))[0] == ""
    assert testi() == [proposta] and ai.calls == 2


# ── #57: the engine is not asked again past the export's budget ───────────────
@pytest.mark.anyio
async def test_the_engine_is_not_asked_again_once_the_export_s_budget_is_spent(session, scena, ai, fake_engine, monkeypatch):
    """The AI calls were bounded, the engine calls between them were not: up to eleven,
    each re-reading the seeds, and the ingress cut the request with a bare 504."""
    from app.routes import flows as flows_routes

    flow = _flusso_a_due_uscite(session, scena)
    _versione(session, flow)
    rifiuto = _rifiuto_traduzione("SELECT id, canale FROM __s_0 QUALIFY strano")
    chiave = dbt_ai.chiave_traduzione(rifiuto[1]["detail"]["params"]["sql"], "duckdb", "clickhouse")
    dbt_ai._ricorda(session, flow.id, 1, "translate", {chiave: "SELECT id, canale FROM __s_0"}, "finto-1")   # kept: no AI call
    monkeypatch.setattr(dbt_ai, "nuova_scadenza", lambda: time.monotonic() - 1)
    fake_engine.dbt_export_script = [rifiuto, None]
    with pytest.raises(HTTPException) as e:
        await flows_routes.export_flow_dbt_con_opzioni(flow.id, d.OpzioniDbt(target="clickhouse", ai_translations=True),
                                                       _richiesta(), scena.capo, session)
    assert e.value.status_code == 422 and "timed out" in e.value.detail and len(fake_engine.dbt_exports) == 1
    # an export that needs no translation is never stopped by the AI budget
    fake_engine.dbt_export_script = [None]
    r = await flows_routes.export_flow_dbt_con_opzioni(flow.id, d.OpzioniDbt(target="clickhouse", ai_translations=True),
                                                       _richiesta(), scena.capo, session)
    assert r.status_code == 200 and ai.calls == 0


# ── #48 (handoff of the dialog lane): the base each default source name is made from ──
def test_the_plan_gives_each_source_the_base_its_default_name_was_made_from(session, scena):
    """The dialog names untyped sources as the gateway does: from the UNSUFFIXED default,
    moved aside with `_2`, `_3`… once typed names change the collisions. default_name
    already carries the suffix of the collisions with nothing typed."""
    nodi, archi = [], []
    for i, schema in enumerate(("Sales", "sales", "altro")):
        ds = _tabella(session, scena, scena.pg, f"{schema}.t{i}", f"datasets/6{i}/v1.parquet", f"s{i}")
        nodi += [_nodo_src(f"s{i}", ds.id), _uscita_ds(f"o{i}", f"Out {i}", scena.cartella.id)]
        archi.append(_edge(f"s{i}", f"o{i}"))
    piano = _piano(session, _flusso(session, scena, "Three schemas", nodi, archi))
    g = scena.pg.id
    attesi = {"duckdb": f"db_{g}_", "clickhouse": f"db_{g}_", "native": "src_"}
    for target, testa in attesi.items():
        fonti = piano[target]["sources"]
        assert [s["default_name"] for s in fonti] == [f"{testa}sales", f"{testa}sales_2", f"{testa}altro"], target
        assert [s["default_base"] for s in fonti] == [f"{testa}sales", f"{testa}sales", f"{testa}altro"], target
