"""Il payload di export dbt che il gateway prepara per l'engine: un modello per
Output con la materializzazione giusta, i passi condivisi come modelli
intermedi referenziati, le sorgenti classificate (tabella → source, query →
modello, uscita di un altro flusso → i suoi modelli, file → seed), schema e
contratto della datasource pubblicata → colonne e test, e i rifiuti chiari."""
import json
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlmodel import select

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
    assert p["seeds"][1] == {"name": "seed_listino_2026", "bucket": "data-prep", "key": "datasets/5bcf7e.parquet", "datasource_id": None,
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


# ── chi può esportare: solo gli amministratori ────────────────────────────────
def _richiesta():
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": "/", "headers": [], "client": ("10.0.0.1", 1234), "query_string": b""})


@pytest.mark.anyio
async def test_l_export_dbt_e_solo_per_gli_amministratori(session, scena, fake_engine):
    from fastapi import HTTPException

    from app.models import Group, UserGroupLink
    from app.routes import flows as flows_routes
    from tests.conftest import make_permission

    flow = _flusso_a_due_uscite(session, scena)
    lettore = make_user(session, email="lettore@x.it")
    editore = make_user(session, email="editore@x.it")
    osservatore = make_user(session, email="occhio@x.it", is_observer=True)
    for u, cap in ((lettore, "view"), (editore, "manage"), (osservatore, "view")):
        make_permission(session, user_id=u.id, project_id=scena.cartella.id, capability=cap)
    for u in (lettore, editore, osservatore):
        with pytest.raises(HTTPException) as e:
            await flows_routes.export_flow_dbt(flow.id, _richiesta(), "duckdb", u, session)
        assert e.value.status_code == 403 and "amministratori" in e.value.detail, u.email

    # admin personale e admin per gruppo (il team data): passano
    risposta = await flows_routes.export_flow_dbt(flow.id, _richiesta(), "duckdb", scena.capo, session)
    assert risposta.status_code == 200 and risposta.media_type == "application/zip"
    team = Group(name="data team", is_admin=True); session.add(team); session.commit(); session.refresh(team)
    dati = make_user(session, email="dati@x.it")
    session.add(UserGroupLink(user_id=dati.id, group_id=team.id)); session.commit()
    risposta = await flows_routes.export_flow_dbt(flow.id, _richiesta(), "native", dati, session)
    assert risposta.status_code == 200
    risposta = await flows_routes.export_flow_dbt(flow.id, _richiesta(), "clickhouse", dati, session)
    assert risposta.status_code == 200
    with pytest.raises(HTTPException) as e:
        await flows_routes.export_flow_dbt(flow.id, _richiesta(), "clickhouse", osservatore, session)
    assert e.value.status_code == 403


def test_una_datasource_sharepoint_diventa_un_seed_e_le_sorgenti_portano_i_tipi(session, scena):
    sp = Connection(name="sp", db_type="sharepoint", project_id=scena.cartella.id, owner_id=scena.capo.id, host="contoso.sharepoint.com", database="", db_schema="")
    session.add(sp); session.commit()
    excel = make_datasource(session, name="budget 2026", project_id=scena.cartella.id, key="datasets/8/v1.parquet", kind="database", connection_id=sp.id,
                            source_type="sharepoint", source_ref=json.dumps({"path": "/Finanza/budget.xlsx", "sheet": "2026"}),
                            columns=json.dumps([{"name": "voce", "dtype": "String"}, {"name": "importo", "dtype": "Float64"}]))
    definizione = {"nodes": [_nodo_src("s1", scena.ordini.id), _nodo_src("s2", excel.id), _op("u", "union", {}),
                             {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Con budget", "projectId": scena.cartella.id}}],
                   "edges": [_edge("s1", "u"), _edge("s2", "u", "right"), _edge("u", "o")]}
    flow = make_flow(session, name="Budget", project_id=scena.cartella.id, definition=json.dumps(definizione))
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    assert [s["name"] for s in p["seeds"]] == ["seed_budget_2026"]                       # non una «tabella» fatta di JSON
    assert p["models"][0]["operations"][0]["params"]["right"]["source"] == {"seed": "seed_budget_2026"}
    assert all(t["name"] != json.dumps({"path": "/Finanza/budget.xlsx", "sheet": "2026"}) for s in p["sources"] for t in s["tables"])
    assert p["source_map"][scena.ordini.key]["types"] == {"id": "Int64", "canale": "String", "importo": "Float64"}


def test_ogni_modello_porta_da_dove_viene(session, scena):
    """Tracciabilità: flusso, versione (e quando è stata salvata), cartella, autore,
    uscita e nodo; i tag per selezionarli in dbt (`tag:tabularia`). Chi esporta va nel
    solo README: i modelli restano uguali da un export all'altro."""
    from app.models import FlowVersion

    autrice = make_user(session, email="anna@x.it")
    flow = _flusso_a_due_uscite(session, scena, owner_id=autrice.id)
    for v in (1, 2, 3):
        session.add(FlowVersion(flow_id=flow.id, version=v, definition=flow.definition))
    session.commit()
    p = d.build_export_payload(session, flow, "data-prep", "duckdb", esportato_da="capo@x.it")
    inter, margin, righe = p["models"]
    comune = {"flow": "Margin flow", "flow_id": flow.id, "flow_version": 3, "folder": "Flows", "owner": "anna@x.it"}
    assert p["exported_by"] == "capo@x.it"
    for m in (inter, margin, righe):
        assert comune.items() <= m["meta"].items() and m["meta"]["version_saved_at"].endswith("UTC")
        assert not any(k.startswith("exported") for k in m["meta"])
        assert m["tags"] == ["tabularia", "margin_flow"]
    assert margin["meta"]["output"] == "output «Margin» (datasource)" and margin["meta"]["node"] == "o1"
    assert righe["meta"]["output"] == "output table `report.righe` (append)" and righe["meta"]["node"] == "o2"
    assert inter["meta"]["output"].startswith("shared steps up to the node") and "node" not in inter["meta"]
    assert "_uscita" not in margin and "_uscita" not in righe


def test_la_query_di_una_datasource_porta_la_datasource_e_il_flusso_a_monte_il_suo(session, scena):
    a_monte = make_flow(session, name="Upstream", project_id=scena.cartella.id, definition=json.dumps({"nodes": [
        _nodo_src("s", scena.clienti.id),
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Clean customers", "projectId": scena.cartella.id, "overwrite": True}},
    ], "edges": [_edge("s", "o")]}))
    pubblicata = make_datasource(session, name="Clean customers", project_id=scena.cartella.id, key="datasets/9/v1.parquet", kind="flow", flow_id=a_monte.id,
                                 columns=json.dumps([{"name": "id", "dtype": "Int64"}]))
    flow = make_flow(session, name="Downstream", project_id=scena.cartella.id, definition=json.dumps({"nodes": [
        _nodo_src("s", pubblicata.id),
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Final", "projectId": scena.cartella.id, "overwrite": True}},
    ], "edges": [_edge("s", "o")]}))
    p = d.build_export_payload(session, flow, "data-prep", "duckdb")
    per_nome = {m["name"]: m for m in p["models"]}
    query = next(m for m in p["models"] if m["kind"] == "raw")
    assert query["meta"]["datasource"] == "customers" and query["meta"]["datasource_id"] == scena.clienti.id and query["tags"] == ["tabularia"]
    assert per_nome["clean_customers"]["meta"]["flow"] == "Upstream" and per_nome["clean_customers"]["tags"] == ["tabularia", "upstream"]
    assert per_nome["final"]["meta"]["flow"] == "Downstream" and per_nome["final"]["meta"]["flow_version"] is None
    assert "exported_by" not in per_nome["final"]["meta"] and p["exported_by"] is None


def test_il_target_clickhouse_accetta_piu_connessioni_e_porta_quello_che_serve_all_engine(session, scena):
    """ClickHouse legge Postgres/MySQL dal vivo: le sorgenti possono stare su più
    connessioni (al contrario del nativo), le query vanno tradotte, e all'engine
    arrivano le connessioni per nominare i database federati — senza password."""
    my = Connection(name="negozio", db_type="mysql", project_id=scena.cartella.id, owner_id=scena.capo.id, host="my.x.it", port=None, database="shop", db_schema="", username="web")
    session.add(my); session.commit()
    prodotti = make_datasource(session, name="products", project_id=scena.cartella.id, key="datasets/5/v1.parquet", kind="database", connection_id=my.id,
                               source_type="table", source_ref="prodotti", columns=json.dumps([{"name": "id", "dtype": "Int64"}, {"name": "canale", "dtype": "String"}]))
    flow = make_flow(session, name="Mix", project_id=scena.cartella.id, definition=json.dumps({"nodes": [
        _nodo_src("a", scena.ordini.id), _nodo_src("b", scena.clienti.id), _nodo_src("c", prodotti.id),
        _op("j", "join", {"how": "left", "on": ["id"]}),
        _op("u", "union", {"strategy": "by_name"}),
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Mix out", "projectId": scena.cartella.id, "overwrite": True}},
    ], "edges": [_edge("a", "j"), _edge("b", "j", "right"), _edge("j", "u"), _edge("c", "u", "right"), _edge("u", "o")]}))
    with pytest.raises(d.DbtExportError, match="STESSA connessione"):
        d.build_export_payload(session, flow, "data-prep", "native")
    p = d.build_export_payload(session, flow, "data-prep", "clickhouse")
    assert p["mode"] == "clickhouse" and p["clickhouse"]["target_schema"] == "dbt_tabularia"
    conns = {c["id"]: c for c in p["clickhouse"]["connections"]}
    assert set(conns) == {scena.pg.id, my.id}
    assert conns[scena.pg.id]["port"] == 5432 and conns[my.id]["port"] == 3306 and conns[my.id]["pw_env"] == f"TABULARIA_DB_{my.id}_PASSWORD"
    assert not any("password" in k for c in conns.values() for k in c if k != "pw_env")
    sorgenti = {(s["connection_id"], s["schema"]) for s in p["sources"]}
    assert sorgenti == {(scena.pg.id, "vendite"), (my.id, "shop")}
    query = next(m for m in p["models"] if m["kind"] == "raw")
    assert query["translate"] == {"connection_id": scena.pg.id, "label": "customers"} and query["raw_sql"].startswith("SELECT id, nome FROM crm.clienti")
    assert not any("as written" in n for n in p["notes"])
    assert p["source_map"][scena.ordini.key]["macro"] == "{{ source('db_%d_vendite', 'ordini') }}" % scena.pg.id


def test_negli_altri_target_la_query_non_si_traduce(session, scena):
    flow = make_flow(session, name="Q", project_id=scena.cartella.id, definition=json.dumps({"nodes": [
        _nodo_src("b", scena.clienti.id),
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Q out", "projectId": scena.cartella.id, "overwrite": True}},
    ], "edges": [_edge("b", "o")]}))
    for target in ("duckdb", "native"):
        query = next(m for m in d.build_export_payload(session, flow, "data-prep", target)["models"] if m["kind"] == "raw")
        assert query["translate"] is None


# ── opzioni del download (dialogo degli amministratori) ─────────────────────────
def _senza_ora(payload):
    for m in payload["models"]:
        (m.get("meta") or {}).pop("exported_at", None)
    return payload


def test_le_opzioni_di_default_danno_lo_stesso_payload_del_download_senza_opzioni(session, scena):
    flow = _flusso_a_due_uscite(session, scena)
    for target in ("duckdb", "native", "clickhouse"):
        senza = _senza_ora(d.build_export_payload(session, flow, "data-prep", target, esportato_da="a@x.it"))
        con = _senza_ora(d.build_export_payload(session, flow, "data-prep", target, esportato_da="a@x.it", opzioni=d.OpzioniDbt(target=target)))
        senza.pop("layout", None); con.pop("layout", None)
        assert senza == con, target


def test_prefisso_materializzazioni_test_email_schema_e_nomi_delle_sorgenti(session, scena):
    autrice = make_user(session, email="anna@x.it")
    flow = _flusso_a_due_uscite(session, scena, owner_id=autrice.id)
    session.add(DataContract(datasource_id=make_datasource(session, name="Margin", project_id=scena.cartella.id, key="datasets/9/v1.parquet",
                                                           kind="flow", flow_id=flow.id, columns=json.dumps([{"name": "canale", "dtype": "String"}])).id,
                             document=json.dumps({"rules": [{"id": "r1", "kind": "not_null", "column": "canale", "severity": "error"},
                                                            {"id": "r2", "kind": "column", "column": "canale", "dtype": "string", "severity": "error"}]}),
                             enabled=True))
    session.commit()
    o = d.OpzioniDbt(target="native", prefix="tab_", schema="analisi", tests=False, emails=False,
                     sources={f"{scena.pg.id}:vendite": {"name": "erp", "declared": True}},
                     materializations={f"{flow.id}:o1": "view", f"{flow.id}:o2": "table", "999:x": "view"})
    p = d.build_export_payload(session, flow, "data-prep", esportato_da="capo@x.it", opzioni=o)
    assert [m["name"] for m in p["models"]] == ["tab_int_c", "tab_margin", "tab_righe"]
    assert p["models"][1]["source"] == {"model": "tab_int_c"}
    assert p["models"][1]["materialized"] == "view" and p["models"][2]["materialized"] == "table"
    margin = p["models"][1]
    assert margin["tests"] == [] and all(not c["tests"] for c in margin["columns"])                 # niente test del contratto…
    assert any("declared as string" in c["description"] for c in margin["columns"])                  # …ma il tipo dichiarato resta descritto
    assert "owner" not in margin["meta"] and p["exported_by"] is None and margin["meta"]["flow"] == "Margin flow"
    assert p["native"]["target_schema"] == "analisi" and p["layout"] == {"package": "project", "folder": "", "layers": False}
    src = p["sources"][0]
    assert src["name"] == "erp" and src["declared"] is True and src["key"] == f"{scena.pg.id}:vendite"
    assert p["source_map"][scena.ordini.key]["macro"] == "{{ source('erp', 'ordini') }}"


def test_una_materializzazione_non_ammessa_e_rifiutata_e_le_forme_libere_pure(session, scena):
    from pydantic import ValidationError

    flow = _flusso_a_due_uscite(session, scena)
    with pytest.raises(d.DbtExportError, match="incremental"):
        d.build_export_payload(session, flow, "data-prep", opzioni=d.OpzioniDbt(materializations={f"{flow.id}:o1": "incremental"}))
    for male in ({"prefix": "Tab-"}, {"folder": "../x"}, {"schema": "a b"}, {"sources": {"1:x": {"name": "x'); drop"}}},
                 {"materializations": {"1:o": "ephemeral"}}, {"target": "bigquery"}, {"sconosciuta": 1}):
        with pytest.raises(ValidationError):
            d.OpzioniDbt(**male)


def test_il_piano_dice_al_dialogo_target_sorgenti_e_uscite(session, scena):
    flow = _flusso_a_due_uscite(session, scena)
    piano = d.piano_export(session, flow, "data-prep")
    per_id = {t["id"]: t for t in piano["targets"]}
    assert [t["id"] for t in piano["targets"]] == ["clickhouse", "duckdb", "native"] and all(t["available"] for t in piano["targets"])
    assert per_id["duckdb"]["sources"] == [{"key": f"{scena.pg.id}:vendite", "connection": "gestionale", "schema": "vendite",
                                            "default_name": f"db_{scena.pg.id}_vendite", "tables": ["ordini"]}]
    assert per_id["native"]["sources"][0]["default_name"] == "src_vendite"
    uscite = {u["key"]: u for u in piano["outputs"]}
    assert uscite[f"{flow.id}:o1"]["materializations"] == ["table", "view"] and uscite[f"{flow.id}:o1"]["materialized"] == "table"
    assert uscite[f"{flow.id}:o2"]["materializations"] == ["incremental", "table", "view"]
    assert piano["default_folder"] == "margin_flow"
    # un pivot: nativo e ClickHouse non possono, e il piano dice perché
    pivot = make_flow(session, name="Piv", project_id=scena.cartella.id, definition=json.dumps({"nodes": [
        _nodo_src("s", scena.ordini.id), _op("p", "pivot", {"index": ["id"], "on": "canale", "values": "importo"}),
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "P", "projectId": scena.cartella.id, "overwrite": True}},
    ], "edges": [_edge("s", "p"), _edge("p", "o")]}))
    per_id = {t["id"]: t for t in d.piano_export(session, pivot, "data-prep")["targets"]}
    assert per_id["duckdb"]["available"] and not per_id["native"]["available"] and "pivot" in per_id["clickhouse"]["reason"]


@pytest.mark.anyio
async def test_il_post_con_le_opzioni_e_il_piano_sono_solo_per_gli_amministratori(session, scena, fake_engine):
    from fastapi import HTTPException

    from app.routes import flows as flows_routes
    from tests.conftest import make_permission

    flow = _flusso_a_due_uscite(session, scena)
    lettore = make_user(session, email="lettore@x.it")
    make_permission(session, user_id=lettore.id, project_id=scena.cartella.id, capability="manage")
    for chiama in (lambda u: flows_routes.export_flow_dbt_con_opzioni(flow.id, d.OpzioniDbt(target="clickhouse"), _richiesta(), u, session),):
        with pytest.raises(HTTPException) as e:
            await chiama(lettore)
        assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        flows_routes.export_flow_dbt_plan(flow.id, _richiesta(), lettore, session)
    assert e.value.status_code == 403
    risposta = await flows_routes.export_flow_dbt_con_opzioni(flow.id, d.OpzioniDbt(target="clickhouse", package="folder", folder="mf"), _richiesta(), scena.capo, session)
    assert risposta.status_code == 200 and risposta.media_type == "application/zip"
    assert fake_engine.dbt_exports[-1]["layout"] == {"package": "folder", "folder": "mf", "layers": False}
    assert fake_engine.dbt_exports[-1]["mode"] == "clickhouse"
    piano = flows_routes.export_flow_dbt_plan(flow.id, _richiesta(), scena.capo, session)
    assert {t["id"] for t in piano["targets"]} == {"clickhouse", "duckdb", "native"}


# ── gli errori nella lingua di chi li legge ────────────────────────────────────
def _richiesta_in(lingua):
    from starlette.requests import Request

    return Request({"type": "http", "method": "GET", "path": "/", "headers": [(b"accept-language", lingua.encode())],
                    "client": ("10.0.0.1", 1234), "query_string": b""})


def test_la_lingua_della_richiesta():
    from app.core.lingua import lingua_di

    assert lingua_di("it") == "it" and lingua_di("de-CH,de;q=0.9,en;q=0.8") == "de" and lingua_di("pt-BR,fr;q=0.7") == "fr"
    assert lingua_di(None) == "en" and lingua_di("") == "en" and lingua_di("zh-CN,ja") == "en"


def test_ogni_messaggio_ha_le_cinque_lingue_con_gli_stessi_parametri():
    import string

    from app.core.lingua import LINGUE
    from app.services.messaggi_dbt import MESSAGGI

    campi = lambda t: {f for _, f, _, _ in string.Formatter().parse(t) if f}
    for code, per_lingua in MESSAGGI.items():
        assert set(per_lingua) == set(LINGUE), code
        assert len({frozenset(campi(t)) for t in per_lingua.values()}) == 1, code


def test_traduzione_e_ripieghi():
    from app.services.messaggi_dbt import messaggio_dell_engine, traduci

    assert traduci("upstream_missing", {"source": "ordini"}, "de") == "Die Quelle «ordini» ist die Ausgabe eines Flows, den es nicht mehr gibt."
    assert traduci("upstream_missing", {"source": "ordini"}, "pt") .startswith("The source «ordini»")         # lingua che non c'è: inglese
    assert traduci("codice_nuovo", {}, "it", "testo dell'engine") == "testo dell'engine"                     # codice sconosciuto: il testo
    assert traduci("upstream_missing", {}, "it", "originale") == "originale"                                 # parametro mancante: il testo
    assert traduci("internal", {}, "fr", "boh") == "L'export dbt a échoué : boh"
    assert messaggio_dell_engine({"code": "seed_too_large", "params": {"source": "s", "rows": "60,000", "max": "50,000"}, "message": "x"}, "es").startswith("La fuente «s» tiene 60,000 filas")
    assert messaggio_dell_engine("un engine vecchio", "it") == "un engine vecchio"


@pytest.mark.anyio
async def test_i_rifiuti_e_il_piano_parlano_la_lingua_dell_interfaccia(session, scena, fake_engine):
    from fastapi import HTTPException

    from app.routes import flows as flows_routes

    # un rifiuto del gateway: il nativo con sorgenti su due connessioni
    my = Connection(name="negozio", db_type="mysql", project_id=scena.cartella.id, owner_id=scena.capo.id, host="my.x.it", database="shop", db_schema="")
    session.add(my); session.commit()
    prodotti = make_datasource(session, name="products", project_id=scena.cartella.id, key="datasets/5/v1.parquet", kind="database", connection_id=my.id,
                               source_type="table", source_ref="prodotti", columns=json.dumps([{"name": "id", "dtype": "Int64"}]))
    flow = make_flow(session, name="Mix", project_id=scena.cartella.id, definition=json.dumps({"nodes": [
        _nodo_src("a", scena.ordini.id), _nodo_src("c", prodotti.id), _op("u", "union", {"strategy": "by_name"}),
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Mix out", "projectId": scena.cartella.id, "overwrite": True}},
    ], "edges": [_edge("a", "u"), _edge("c", "u", "right"), _edge("u", "o")]}))
    attesi = {"it": "Il target nativo vuole tutte le sorgenti sulla STESSA connessione", "de": "Das native Ziel braucht alle Quellen auf DERSELBEN Verbindung",
              "en": "The native target needs every source on the SAME connection"}
    for lingua, inizio in attesi.items():
        with pytest.raises(HTTPException) as e:
            await flows_routes.export_flow_dbt(flow.id, _richiesta_in(lingua), "native", scena.capo, session)
        assert e.value.status_code == 422 and e.value.detail.startswith(inizio), (lingua, e.value.detail)
        piano = flows_routes.export_flow_dbt_plan(flow.id, _richiesta_in(lingua), scena.capo, session)
        assert next(t for t in piano["targets"] if t["id"] == "native")["reason"].startswith(inizio)
    # un rifiuto dell'engine: arriva come codice e parametri, il gateway lo traduce
    fake_engine.dbt_export_response = (422, {"detail": {"code": "other_clickhouse", "params": {"connection": "crm", "host": "ch2"}, "message": "testo italiano"}})
    with pytest.raises(HTTPException) as e:
        await flows_routes.export_flow_dbt(flow.id, _richiesta_in("fr"), "clickhouse", scena.capo, session)
    assert e.value.status_code == 422 and e.value.detail.startswith("La connexion «crm» est un AUTRE ClickHouse (ch2)")


# ── l'AI nell'export (opzionale): un modello finto al posto del provider ───────
@pytest.fixture
def ai_finta(monkeypatch):
    """Un modello che risponde a ogni voce con una descrizione (con un po' di Jinja da
    ripulire) e a ogni richiesta di traduzione con `sql`; conta le chiamate."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel

    from app.services import ai_agent, dbt_ai

    stato = SimpleNamespace(chiamate=0, prompt=[], sql="SELECT id, canale FROM __s_0", errore=None, campioni=[])

    def rispondi(messages, info):
        stato.chiamate += 1
        if stato.errore:
            raise RuntimeError(stato.errore)
        testo = next(p.content for m in messages for p in getattr(m, "parts", []) if type(p).__name__ == "UserPromptPart")
        stato.prompt.append(testo)
        strumento = info.output_tools[0].name
        if "items" in strumento or testo.startswith("{"):
            voci = json.loads(testo)["items"]
            return ModelResponse(parts=[ToolCallPart(tool_name=strumento, args={"items": [
                {"key": v["key"], "description": f"About {v.get('column') or v.get('name')} {{{{ evil }}}}"} for v in voci]})])
        return ModelResponse(parts=[ToolCallPart(tool_name=strumento, args={"sql": stato.sql})])

    async def campioni(bucket, key):
        stato.campioni.append(key)
        return {"id": ["1", "2", "3"], "importo": ["9.5"]}

    monkeypatch.setattr(ai_agent, "build_model", lambda model_id: FunctionModel(rispondi))
    monkeypatch.setattr(dbt_ai, "modello_ai", lambda session: "finto-1")
    monkeypatch.setattr(dbt_ai, "_campioni", campioni)
    return stato


@pytest.mark.anyio
async def test_le_descrizioni_dell_ai_vanno_dove_mancano_e_si_ricordano_per_versione(session, scena, ai_finta):
    from app.models import AiSpend, DbtAiText, FlowVersion
    from app.services import dbt_ai

    flow = _flusso_a_due_uscite(session, scena)
    session.add(FlowVersion(flow_id=flow.id, version=1, definition=flow.definition)); session.commit()

    async def esporta():
        payload, progetto = d._costruisci(session, flow, "data-prep", "duckdb", "capo@x.it", d.OpzioniDbt(ai_descriptions=True))
        await dbt_ai.descrivi(session, scena.capo, flow, sorted(progetto.flussi_visti), payload)
        return payload

    p = await esporta()
    colonne = {c["name"]: c["description"] for c in p["sources"][0]["tables"][0]["columns"]}
    assert colonne["canale"] == "Sales channel"                                   # quella scritta da una persona resta
    assert colonne["id"].startswith("(AI) About id") and "{{" not in colonne["id"] and "{ {" in colonne["id"]
    assert p["ai"]["model"] == "finto-1" and p["ai"]["summaries"][0]["flow"] == "Margin flow"
    assert all("(AI) About Margin flow" in m["description"] for m in p["models"] if m["kind"] == "output")
    voci = json.loads(ai_finta.prompt[0])["items"]
    assert next(v for v in voci if v.get("column") == "id")["samples"] == ["1", "2", "3"]   # al più 3 valori per colonna
    assert not any(v.get("column") == "canale" for v in voci)                               # già descritta: non si chiede
    assert ai_finta.chiamate == 1 and session.exec(select(AiSpend)).all()
    righe = session.exec(select(DbtAiText).where(DbtAiText.flow_id == flow.id)).all()
    assert {r.kind for r in righe} == {"describe"} and {r.flow_version for r in righe} == {1}

    # la stessa versione: nessuna chiamata, le stesse parole
    p2 = await esporta()
    assert ai_finta.chiamate == 1 and p2["sources"] == p["sources"] and p2["ai"] == p["ai"]
    # una versione nuova si richiede
    session.add(FlowVersion(flow_id=flow.id, version=2, definition=flow.definition)); session.commit()
    await esporta()
    assert ai_finta.chiamate == 2


@pytest.mark.anyio
async def test_tetto_raggiunto_e_provider_che_non_risponde_fermano_l_export(session, scena, ai_finta, monkeypatch):
    from app.services import ai_chats, dbt_ai

    flow = _flusso_a_due_uscite(session, scena)
    payload, progetto = d._costruisci(session, flow, "data-prep", "duckdb", None, d.OpzioniDbt(ai_descriptions=True))
    ai_finta.errore = "503 upstream"
    with pytest.raises(d.DbtExportError) as e:
        await dbt_ai.descrivi(session, scena.capo, flow, [flow.id], payload)
    assert e.value.code == "ai_failed" and "503" in e.value.params["detail"]
    monkeypatch.setattr(dbt_ai.get_settings().ai, "max_cost_per_day_usd", 0.01)
    monkeypatch.setattr(ai_chats, "speso_oggi", lambda s, u: Decimal("1"))
    with pytest.raises(d.DbtExportError) as e:
        await dbt_ai.descrivi(session, scena.capo, flow, [flow.id], payload)
    assert e.value.code == "ai_cap_reached"
    monkeypatch.setattr(dbt_ai, "modello_ai", lambda s: None)
    with pytest.raises(d.DbtExportError) as e:
        await dbt_ai.descrivi(session, scena.capo, flow, [flow.id], payload)
    assert e.value.code == "ai_unavailable"


@pytest.mark.anyio
async def test_la_traduzione_dell_ai_entra_quando_l_engine_non_traduce_e_si_ricorda(session, scena, ai_finta, fake_engine):
    from app.models import DbtAiText
    from app.routes import flows as flows_routes

    flow = _flusso_a_due_uscite(session, scena)
    rifiuto = (422, {"detail": {"code": "translation_failed", "message": "x", "params": {
        "dialect": "clickhouse", "detail": "boh", "model": "margin", "sql": "SELECT id, canale FROM __s_0 QUALIFY strano",
        "source_dialect": "duckdb", "target_dialect": "clickhouse", "columns": ["id", "canale"]}}})
    fake_engine.dbt_export_script = [rifiuto, None]
    opz = d.OpzioniDbt(target="clickhouse", ai_translations=True)
    risposta = await flows_routes.export_flow_dbt_con_opzioni(flow.id, opz, _richiesta(), scena.capo, session)
    assert risposta.status_code == 200 and ai_finta.chiamate == 1
    assert fake_engine.dbt_exports[-1]["overrides"] == {"margin": "SELECT id, canale FROM __s_0"}
    assert session.exec(select(DbtAiText).where(DbtAiText.kind == "translate")).first().text == "SELECT id, canale FROM __s_0"
    # di nuovo: la proposta ricordata, nessuna chiamata
    fake_engine.dbt_export_script = [rifiuto, None]
    await flows_routes.export_flow_dbt_con_opzioni(flow.id, opz, _richiesta(), scena.capo, session)
    assert ai_finta.chiamate == 1
    # senza l'opzione: il rifiuto arriva com'era
    fake_engine.dbt_export_script = [rifiuto]
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        await flows_routes.export_flow_dbt_con_opzioni(flow.id, d.OpzioniDbt(target="clickhouse"), _richiesta(), scena.capo, session)
    assert e.value.status_code == 422 and "translation to clickhouse failed" in e.value.detail.lower()


@pytest.mark.anyio
async def test_una_proposta_scartata_dall_engine_si_dimentica_e_il_motivo_e_nella_lingua(session, scena, ai_finta, fake_engine):
    from fastapi import HTTPException

    from app.models import DbtAiText
    from app.routes import flows as flows_routes

    flow = _flusso_a_due_uscite(session, scena)
    rifiuto = (422, {"detail": {"code": "translation_failed", "message": "x", "params": {
        "dialect": "clickhouse", "detail": "boh", "model": "margin", "sql": "SELECT 1", "source_dialect": "duckdb",
        "target_dialect": "clickhouse", "columns": ["id"]}}})
    scartata = (422, {"detail": {"code": "ai_translation_invalid", "message": "x", "params": {
        "model": "margin", "reason": "columns", "got": "a", "expected": "id"}}})
    # scartata due volte: il rifiuto, nella lingua di chi esporta; la seconda richiesta all'AI porta il motivo
    fake_engine.dbt_export_script = [rifiuto, scartata, scartata]
    with pytest.raises(HTTPException) as e:
        await flows_routes.export_flow_dbt_con_opzioni(flow.id, d.OpzioniDbt(target="clickhouse", ai_translations=True),
                                                       _richiesta_in("it"), scena.capo, session)
    assert e.value.detail == ("La traduzione dell'AI di «margin» non passa i controlli: le sue colonne sono a invece di id. "
                              "Esporta con un altro target, o senza la traduzione dell'AI.")
    assert ai_finta.chiamate == 2 and "Your previous translation was rejected: The AI's translation" in ai_finta.prompt[-1]
    assert session.exec(select(DbtAiText).where(DbtAiText.kind == "translate")).first() is None
    # scartata una volta: il secondo tentativo passa
    fake_engine.dbt_export_script = [rifiuto, scartata, None]
    risposta = await flows_routes.export_flow_dbt_con_opzioni(flow.id, d.OpzioniDbt(target="clickhouse", ai_translations=True),
                                                              _richiesta_in("it"), scena.capo, session)
    assert risposta.status_code == 200 and ai_finta.chiamate == 4


def test_il_piano_dice_se_l_ai_c_e(session, scena, monkeypatch):
    from app.routes import flows as flows_routes
    from app.services import dbt_ai

    flow = _flusso_a_due_uscite(session, scena)
    monkeypatch.setattr(dbt_ai, "modello_ai", lambda s: None)
    assert flows_routes.export_flow_dbt_plan(flow.id, _richiesta(), scena.capo, session)["ai"] == {"available": False, "model": None}
    monkeypatch.setattr(dbt_ai, "modello_ai", lambda s: "m-1")
    assert flows_routes.export_flow_dbt_plan(flow.id, _richiesta(), scena.capo, session)["ai"] == {"available": True, "model": "m-1"}
