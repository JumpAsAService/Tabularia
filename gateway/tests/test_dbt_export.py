"""Il payload di export dbt che il gateway prepara per l'engine: un modello per
Output con la materializzazione giusta, i passi condivisi come modelli
intermedi referenziati, le sorgenti classificate (tabella → source, query →
modello, uscita di un altro flusso → i suoi modelli, file → seed), schema e
contratto della datasource pubblicata → colonne e test, e i rifiuti chiari."""
import json
from types import SimpleNamespace

import pytest

from app.models import Connection
from app.models.contract import DataContract
from app.services import dbt_export as d
from tests.conftest import make_datasource, make_flow, make_project, make_user


def _nodo_src(nid, ds_id):
    return {"id": nid, "type": "source", "data": {"datasourceId": ds_id}}


def _op(nid, op_type, params):
    return {"id": nid, "type": "operation", "data": {"opType": op_type, "params": params}}


def _edge(a, b, handle="left"):
    return {"source": a, "target": b, "targetHandle": handle}


@pytest.fixture
def scena(session):
    capo = make_user(session, email="capo@x.it", is_superuser=True)
    cartella = make_project(session, name="Flows", owner_id=capo.id)
    pg = Connection(name="gestionale", db_type="postgresql", project_id=cartella.id, owner_id=capo.id, host="db.x.it", port=None, database="erp", db_schema="")
    ch = Connection(name="crm", db_type="clickhouse", project_id=cartella.id, owner_id=capo.id, host="ch.x.it", port=8123, database="analytics", db_schema="")
    session.add(pg); session.add(ch); session.commit()
    ordini = make_datasource(session, name="orders", project_id=cartella.id, key="datasets/1/v3.parquet", kind="database", connection_id=pg.id,
                             source_type="table", source_ref="vendite.ordini", description="Every order",
                             columns=json.dumps([{"name": "id", "dtype": "Int64"}, {"name": "canale", "dtype": "String"}, {"name": "importo", "dtype": "Float64"}]),
                             column_descriptions=json.dumps({"canale": "Sales channel"}))
    clienti = make_datasource(session, name="customers", project_id=cartella.id, key="datasets/2/v1.parquet", kind="database", connection_id=pg.id,
                              source_type="sql", source_ref="SELECT id, nome FROM crm.clienti WHERE attivo;",
                              columns=json.dumps([{"name": "id", "dtype": "Int64"}, {"name": "nome", "dtype": "String"}]))
    return SimpleNamespace(capo=capo, cartella=cartella, pg=pg, ch=ch, ordini=ordini, clienti=clienti)


def _flusso_a_due_uscite(session, scena, **extra):
    """orders → filter → compute → [group_by → Output datasource «Margin»] e
    [→ Output tabella report.righe in append]: filter+compute sono condivisi."""
    definizione = {"nodes": [
        _nodo_src("src", scena.ordini.id),
        _op("f", "filter", {"column": "canale", "operator": "eq", "value": "web"}),
        _op("c", "compute", {"columns": [{"name": "netto", "expr": "importo * 0.8"}]}),
        _op("g", "group_by", {"by": ["canale"], "aggregations": [{"column": "netto", "func": "sum", "alias": "tot"}]}),
        {"id": "o1", "type": "output", "data": {"destType": "datasource", "name": "Margin", "projectId": scena.cartella.id, "overwrite": True}},
        {"id": "o2", "type": "output", "data": {"destType": "database", "connectionId": scena.pg.id, "table": "report.righe", "mode": "append", "postSql": "ANALYZE report.righe"}},
        {"id": "o3", "type": "output", "data": {"destType": "email", "connectionId": 99, "emailTo": ["a@x.it"]}},
    ], "edges": [_edge("src", "f"), _edge("f", "c"), _edge("c", "g"), _edge("g", "o1"), _edge("c", "o2"), _edge("c", "o3")]}
    return make_flow(session, name="Margin flow", project_id=scena.cartella.id, definition=json.dumps(definizione), description="The margin", **extra)


def test_due_uscite_condividono_i_passi_a_monte_in_un_modello_intermedio(session, scena):
    flow = _flusso_a_due_uscite(session, scena)
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    nomi = [m["name"] for m in p["models"]]
    assert nomi == ["int_c", "margin", "righe"]                     # l'intermedio prima di chi lo usa; niente modello per l'email
    inter = p["models"][0]
    assert inter["materialized"] == "ephemeral" and inter["source"] == {"key": scena.ordini.key}
    assert [o["type"] for o in inter["operations"]] == ["filter", "compute"]
    margin, righe = p["models"][1], p["models"][2]
    assert margin["source"] == {"model": "int_c"} and [o["type"] for o in margin["operations"]] == ["group_by"]
    assert margin["materialized"] == "table" and margin["publishes"] == ("Margin", scena.cartella.id)
    assert righe["source"] == {"model": "int_c"} and righe["operations"] == [] and righe["materialized"] == "incremental"
    assert "schema" not in righe                                      # in federazione la tabella resta nel file DuckDB
    assert any("post-SQL" in n for n in p["notes"])
    assert p["mode"] == "duckdb" and p["attachments"][0]["alias"] == f"db_{scena.pg.id}"
    assert p["source_map"][scena.ordini.key]["columns"] == ["id", "canale", "importo"]
    tab = p["sources"][0]["tables"][0]
    assert tab["name"] == "ordini" and tab["description"] == "Datasource «orders»: Every order"
    assert {"name": "canale", "description": "Sales channel"} in tab["columns"]


def test_in_nativo_la_tabella_di_destinazione_e_schema_e_alias_del_modello(session, scena):
    flow = _flusso_a_due_uscite(session, scena)
    p = d.build_export_payload(session, flow, "data-prep", "native")
    righe = next(m for m in p["models"] if m["name"] == "righe")
    assert righe["schema"] == "report" and righe["alias"] == "righe" and righe["materialized"] == "incremental"
    assert p["native"]["db_type"] == "postgresql" and p["native"]["conn"]["port"] == 5432
    assert p["source_map"][scena.ordini.key]["ident"] == "__s_0"


def test_lo_schema_e_il_contratto_della_datasource_pubblicata_diventano_colonne_e_test(session, scena):
    flow = _flusso_a_due_uscite(session, scena)
    pubblicata = make_datasource(session, name="Margin", project_id=scena.cartella.id, key="datasets/9/v1.parquet", kind="flow", flow_id=flow.id,
                                 columns=json.dumps([{"name": "canale", "dtype": "String"}, {"name": "tot", "dtype": "Float64"}]),
                                 column_descriptions=json.dumps({"tot": "Net revenue"}))
    regole = [{"id": "r1", "kind": "not_null", "column": "canale", "severity": "error"},
              {"id": "r0", "kind": "unique", "columns": ["canale"], "severity": "error"},
              {"id": "r7", "kind": "unique", "columns": ["canale", "tot"], "severity": "warning"},
              {"id": "r2", "kind": "accepted_values", "column": "canale", "values": ["web", "shop"], "severity": "warning"},
              {"id": "r3", "kind": "range", "column": "tot", "min": 0, "severity": "error"},
              {"id": "r4", "kind": "row_count", "min": 1, "max": 1000, "severity": "warning"},
              {"id": "r5", "kind": "freshness", "max_age_hours": 24, "severity": "error"},
              {"id": "r6", "kind": "column", "column": "tot", "dtype": "Float64", "severity": "error"}]
    session.add(DataContract(datasource_id=pubblicata.id, document=json.dumps({"description": "what we promise", "rules": regole}), enabled=True))
    session.commit()
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    margin = next(m for m in p["models"] if m["name"] == "margin")
    colonne = {c["name"]: c for c in margin["columns"]}
    assert colonne["canale"]["tests"] == [{"kind": "not_null", "severity": "error"}, {"kind": "unique", "severity": "error"}, {"kind": "accepted_values", "severity": "warn", "values": ["web", "shop"]}]
    assert colonne["tot"]["description"] == "Net revenue Contract: declared as Float64."
    assert margin["tests"] == [{"id": "r7", "kind": "unique_combination", "columns": ["canale", "tot"], "severity": "warn"},
                               {"id": "r3", "kind": "range", "column": "tot", "min": 0, "severity": "error"},
                               {"id": "r4", "kind": "row_count", "min": 1, "max": 1000, "severity": "warn"}]
    assert margin["description"].endswith("Contract: what we promise")
    assert any("freshness" in n for n in p["notes"])


def test_un_contratto_spento_non_produce_test(session, scena):
    flow = _flusso_a_due_uscite(session, scena)
    pubblicata = make_datasource(session, name="Margin", project_id=scena.cartella.id, key="datasets/9/v1.parquet", kind="flow", flow_id=flow.id,
                                 columns=json.dumps([{"name": "canale", "dtype": "String"}]))
    session.add(DataContract(datasource_id=pubblicata.id, document=json.dumps({"rules": [{"id": "r1", "kind": "not_null", "column": "canale", "severity": "error"}]}), enabled=False))
    session.commit()
    margin = next(m for m in d.build_export_payload(session, flow, "data-prep", "duckdb")["models"] if m["name"] == "margin")
    assert margin["columns"] == [{"name": "canale", "description": "", "tests": []}] and margin["tests"] == []


def test_una_datasource_da_query_e_un_modello_ephemeral_con_la_query(session, scena):
    definizione = {"nodes": [_nodo_src("s", scena.clienti.id), _op("l", "limit", {"n": 10}),
                             {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Top", "projectId": scena.cartella.id}}],
                   "edges": [_edge("s", "l"), _edge("l", "o")]}
    flow = make_flow(session, name="Top customers", project_id=scena.cartella.id, definition=json.dumps(definizione))
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    src, top = p["models"]
    assert src["name"] == "src_customers" and src["kind"] == "raw" and src["materialized"] == "ephemeral"
    assert src["raw_sql"] == f"SELECT * FROM postgres_query('db_{scena.pg.id}', 'SELECT id, nome FROM crm.clienti WHERE attivo')"
    assert [c["name"] for c in src["columns"]] == ["id", "nome"]
    assert top["source"] == {"model": "src_customers"}
    assert p["attachments"][0]["alias"] == f"db_{scena.pg.id}" and p["sources"] == []     # l'ATTACH serve anche senza tabelle
    n = d.build_export_payload(session, flow, "data-prep", "native")
    assert n["models"][0]["raw_sql"] == "SELECT id, nome FROM crm.clienti WHERE attivo" and any("source()" in x for x in n["notes"])


def test_l_uscita_di_un_altro_flusso_porta_dentro_i_suoi_modelli(session, scena):
    monte = _flusso_a_due_uscite(session, scena)
    margin_ds = make_datasource(session, name="Margin", project_id=scena.cartella.id, key="datasets/9/v1.parquet", kind="flow", flow_id=monte.id,
                                columns=json.dumps([{"name": "canale", "dtype": "String"}, {"name": "tot", "dtype": "Float64"}]))
    definizione = {"nodes": [_nodo_src("s", margin_ds.id), _op("f", "filter", {"column": "tot", "operator": "gt", "value": 0}),
                             {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Positive margin", "projectId": scena.cartella.id}}],
                   "edges": [_edge("s", "f"), _edge("f", "o")]}
    valle = make_flow(session, name="Downstream", project_id=scena.cartella.id, definition=json.dumps(definizione))
    p = d.build_export_payload(session, valle, "data-prep", "duckdb")
    nomi = [m["name"] for m in p["models"]]
    assert nomi == ["int_c", "margin", "righe", "positive_margin"]   # prima il flusso a monte, poi il nostro
    assert p["models"][-1]["source"] == {"model": "margin"}
    assert p["source_map"] and scena.ordini.key in p["source_map"]


def test_un_ciclo_fra_flussi_e_rifiutato(session, scena):
    a = make_flow(session, name="A", project_id=scena.cartella.id, definition="{}")
    b_out = make_datasource(session, name="B out", project_id=scena.cartella.id, key="datasets/b.parquet", kind="flow", flow_id=None)
    a_out = make_datasource(session, name="A out", project_id=scena.cartella.id, key="datasets/a.parquet", kind="flow", flow_id=a.id)
    b = make_flow(session, name="B", project_id=scena.cartella.id, definition=json.dumps({"nodes": [_nodo_src("s", a_out.id), {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "B out", "projectId": scena.cartella.id}}], "edges": [_edge("s", "o")]}))
    b_out.flow_id = b.id; session.add(b_out)
    a.definition = json.dumps({"nodes": [_nodo_src("s", b_out.id), {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "A out", "projectId": scena.cartella.id}}], "edges": [_edge("s", "o")]})
    session.add(a); session.commit()
    with pytest.raises(d.DbtExportError, match="ciclo"):
        d.build_export_payload(session, a, "data-prep", "duckdb")


def test_un_file_caricato_o_una_datasource_senza_origine_diventano_seed(session, scena):
    importata = make_datasource(session, name="price list", project_id=scena.cartella.id, key="datasets/7/v1.parquet", kind="flow", flow_id=None,
                                columns=json.dumps([{"name": "sku", "dtype": "String"}]))
    definizione = {"nodes": [
        _nodo_src("s1", importata.id),
        {"id": "s2", "type": "source", "data": {"parquetKey": "datasets/5bcf7e.parquet", "bucket": "data-prep", "filename": "Listino 2026.csv"}},
        _op("j", "join", {"how": "inner", "on": "sku"}),
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Joined", "projectId": scena.cartella.id}},
    ], "edges": [_edge("s1", "j"), _edge("s2", "j", "right"), _edge("j", "o")]}
    flow = make_flow(session, name="Seeds", project_id=scena.cartella.id, definition=json.dumps(definizione))
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert [s["name"] for s in p["seeds"]] == ["seed_price_list", "seed_listino_2026"]   # il file caricato prende il nome del file, non della chiave
    assert p["seeds"][1] == {"name": "seed_listino_2026", "bucket": "data-prep", "key": "datasets/5bcf7e.parquet",
                             "description": "File Listino 2026.csv, exported as a CSV seed: the data is a snapshot, not a live source.", "columns": []}
    assert p["models"][0]["source"] == {"seed": "seed_price_list"} and p["attachments"] == []
    destra = p["models"][0]["operations"][0]["params"]["right"]
    assert destra["source"] == {"seed": "seed_listino_2026"}            # anche nel ramo destro del join il file è il seed
    with pytest.raises(d.DbtExportError, match="solo file"):
        d.build_export_payload(session, flow, "data-prep", "native")


def test_i_rifiuti_sono_chiari(session, scena):
    crm = make_datasource(session, name="crm_activities", project_id=scena.cartella.id, key="datasets/5/v1.parquet", kind="database", connection_id=scena.ch.id,
                          source_type="table", source_ref="crm.activities", columns="[]")
    definizione = {"nodes": [_nodo_src("s1", scena.ordini.id), _nodo_src("s2", crm.id), _op("u", "union", {}),
                             {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "All", "projectId": scena.cartella.id}}],
                   "edges": [_edge("s1", "u"), _edge("s2", "u", "right"), _edge("u", "o")]}
    flow = make_flow(session, name="Mixed", project_id=scena.cartella.id, definition=json.dumps(definizione))
    with pytest.raises(d.DbtExportError, match="non federabile"):
        d.build_export_payload(session, flow, "data-prep", "duckdb")
    with pytest.raises(d.DbtExportError, match="STESSA connessione"):
        d.build_export_payload(session, flow, "data-prep", "native")
    solo_email = make_flow(session, name="Mail", project_id=scena.cartella.id, definition=json.dumps({"nodes": [_nodo_src("s", scena.ordini.id), {"id": "o", "type": "output", "data": {"destType": "email", "connectionId": 1}}], "edges": [_edge("s", "o")]}))
    with pytest.raises(d.DbtExportError, match="solo email"):
        d.build_export_payload(session, solo_email, "data-prep", "duckdb")
    with pytest.raises(d.DbtExportError, match="target sconosciuto"):
        d.build_export_payload(session, flow, "data-prep", "bigquery")
