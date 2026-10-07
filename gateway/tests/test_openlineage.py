"""OpenLineage: che cosa Tabularia racconta a un catalogo esterno di ogni run.

Il client è quello ufficiale con un trasporto che cattura gli eventi: si prova
la MAPPA (job, dataset, facet, nomi secondo le convenzioni dello standard), non
la rete. E che, spento, non succeda niente.
"""
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from openlineage.client import OpenLineageClient
from openlineage.client.transport import Transport

from app.core.config import get_settings
from app.models import Connection
from app.models.permission import Capability
from app.routes import lineage as lineage_routes
from app.services import openlineage as ol
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_run, make_user


class _Cattura(Transport):
    kind = "cattura"

    def __init__(self):
        self.eventi = []

    def emit(self, event):
        from openlineage.client.serde import Serde

        self.eventi.append(json.loads(Serde.to_json(event)))


@pytest.fixture(autouse=True)
def _namespace_fisso(monkeypatch):
    # il contenitore di sviluppo può avere un namespace suo nell'ambiente: i nomi attesi qui no
    monkeypatch.setattr(get_settings().openlineage, "namespace", "tabularia")


@pytest.fixture
def cattura(monkeypatch):
    t = _Cattura()
    ol.set_client(OpenLineageClient(transport=t))
    monkeypatch.setattr(get_settings().openlineage, "url", "http://collector:5000")
    yield t
    ol.wait_idle()
    ol.set_client(None)


@pytest.fixture
def scena(session):
    capo = make_user(session, email="capo@x.it", is_superuser=True)
    radice = make_project(session, name="Sample", owner_id=capo.id)
    cartella = make_project(session, name="Flows", owner_id=capo.id, parent_id=radice.id)
    pg = Connection(name="gestionale", db_type="postgresql", project_id=cartella.id, owner_id=capo.id, host="db.x.it", port=None, database="erp", db_schema="")
    session.add(pg)
    session.commit()
    ordini = make_datasource(session, name="orders", project_id=cartella.id, key="datasets/41/v3.parquet", rows=12, kind="database",
                             connection_id=pg.id, source_type="table", source_ref="vendite.ordini", columns=json.dumps([{"name": "id", "dtype": "Int64"}, {"name": "canale", "dtype": "String"}]))
    clienti = make_datasource(session, name="customers", project_id=cartella.id, key="datasets/43/v1.parquet", rows=3, kind="database",
                              connection_id=pg.id, source_type="sql", source_ref="SELECT c.id, r.nome FROM crm.clienti c JOIN crm.regioni r ON r.id = c.regione_id WHERE c.attivo")
    definizione = {"nodes": [
        {"id": "s1", "type": "source", "data": {"datasourceId": ordini.id}},
        {"id": "s2", "type": "source", "data": {"datasourceId": clienti.id}},
        {"id": "s3", "type": "source", "data": {"datasourceId": ordini.id}},
        {"id": "o1", "type": "output", "data": {"destType": "datasource", "name": "Margin", "projectId": cartella.id, "overwrite": True}},
        {"id": "o2", "type": "output", "data": {"destType": "database", "connectionId": pg.id, "table": "report.margini", "mode": "replace"}},
        {"id": "o3", "type": "output", "data": {"destType": "email", "connectionId": 99}},
    ], "edges": []}
    flow = make_flow(session, name="Margin by Category", project_id=cartella.id, definition=json.dumps(definizione), description="Margine per categoria")
    lettore = make_user(session, email="lettore@x.it")
    make_permission(session, user_id=lettore.id, project_id=cartella.id, capability=Capability.VIEW)
    return SimpleNamespace(capo=capo, cartella=cartella, pg=pg, ordini=ordini, clienti=clienti, flow=flow, lettore=lettore)


# ── nomi secondo le convenzioni ──────────────────────────────────────────────
def test_i_nomi_dei_dataset_seguono_le_convenzioni_di_openlineage(session, scena):
    assert ol.table_dataset_name(scena.pg, "ordini") == "erp.public.ordini"
    assert ol.table_dataset_name(scena.pg, "vendite.ordini") == "erp.vendite.ordini"
    assert ol.table_dataset_name(scena.pg, "altro.vendite.ordini") == "altro.vendite.ordini"
    ch = Connection(name="dwh", db_type="clickhouse", host="ch.x.it", port=8443, database="analytics", project_id=1)
    assert ol.table_dataset_name(ch, "fatti") == "analytics.fatti"
    d = ol.table_dataset(scena.pg, "vendite.ordini")
    assert (d.namespace, d.name) == ("postgres://db.x.it:5432", "erp.vendite.ordini")
    ds = ol.datasource_dataset(session, scena.ordini)
    assert (ds.namespace, ds.name) == ("tabularia://tabularia", "/Sample/Flows/orders")   # il nome di Tabularia, sotto la cartella
    assert ds.facets["datasetVersion"].datasetVersion == "datasets/41/v3.parquet"
    assets = ds.facets["symlinks"].identifiers
    assert (assets[0].namespace, assets[0].name, assets[0].type) == ("s3://" + scena.ordini.bucket, f"/datasets/{scena.ordini.id}", "LOCATION")   # la cartella, stabile: non il file dello snapshot
    assert (ds.facets["tabularia"].datasourceId, ds.facets["tabularia"].folder, ds.facets["tabularia"].kind) == (scena.ordini.id, "Sample/Flows", "database")
    assert [f.name for f in ds.facets["schema"].fields] == ["id", "canale"]
    serializzato = ol.to_json([ol.job_events_for_flow(session, scena.flow)[0]])[0]["inputs"][0]["facets"]["tabularia"]
    assert serializzato["datasourceId"] == scena.ordini.id and serializzato["_schemaURL"].endswith("TabulariaDatasetFacet.json")
    assert ol.job_name_for_flow(session, scena.flow) == "Sample/Flows/Margin by Category"
    assert ol.run_uuid(7) == ol.run_uuid(7) and ol.run_uuid(7) != ol.run_uuid(8)


def test_una_query_sql_porta_le_tabelle_che_legge_e_il_testo(session, scena):
    inputs, facets = ol.origin_inputs(session, scena.clienti)
    assert sorted(i.name for i in inputs) == ["erp.crm.clienti", "erp.crm.regioni"]
    assert facets["sql"].query.startswith("SELECT c.id") and facets["sql"].dialect == "postgres"
    scena.clienti.source_ref = "SELECT ??? FROM"   # una query che sqlglot non capisce: niente tabelle, ma il testo resta
    inputs, facets = ol.origin_inputs(session, scena.clienti)
    assert inputs == [] and "sql" in facets


# ── il flusso: lineage statico dalla definizione ─────────────────────────────
def test_il_lineage_statico_del_flusso_legge_la_definizione(session, scena):
    (ev,) = ol.to_json(ol.job_events_for_flow(session, scena.flow))
    assert ev["job"] == {"namespace": "tabularia", "name": "Sample/Flows/Margin by Category", "facets": ev["job"]["facets"]}
    assert ev["job"]["facets"]["documentation"]["description"] == "Margine per categoria"
    assert [i["name"] for i in ev["inputs"]] == ["/Sample/Flows/orders", "/Sample/Flows/customers"]   # senza doppioni
    assert [(o["namespace"], o["name"]) for o in ev["outputs"]] == [
        ("tabularia://tabularia", "/Sample/Flows/Margin"),              # non esiste ancora: ha già il nome che avrà
        ("postgres://db.x.it:5432", "erp.report.margini"),
    ]
    assert ev["outputs"][1]["facets"]["lifecycleStateChange"]["lifecycleStateChange"] == "OVERWRITE"
    assert "schemaURL" in ev and ev["producer"] == ol.PRODUCER


def test_l_export_e_di_chi_legge_e_l_invio_vuole_un_collector(session, scena, cattura, monkeypatch):
    eventi = lineage_routes.flow_openlineage(scena.flow.id, scena.lettore, session)
    assert eventi[0]["job"]["name"] == "Sample/Flows/Margin by Category"
    assert lineage_routes.emit_flow_openlineage(scena.flow.id, scena.lettore, session) == {"events": 1}
    assert cattura.eventi[-1]["job"]["name"] == "Sample/Flows/Margin by Category"
    monkeypatch.setattr(get_settings().openlineage, "url", "")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        lineage_routes.emit_flow_openlineage(scena.flow.id, scena.lettore, session)
    assert e.value.status_code == 409
    estraneo = make_user(session, email="fuori@x.it")
    with pytest.raises(HTTPException) as e:
        lineage_routes.flow_openlineage(scena.flow.id, estraneo, session)
    assert e.value.status_code == 403


# ── i run: eventi START e COMPLETE/FAIL ──────────────────────────────────────
def _t(min):
    return datetime(2026, 10, 7, 8, min, tzinfo=timezone.utc).replace(tzinfo=None)


def test_un_run_di_output_racconta_cosa_ha_scritto_e_da_chi_dipende(session, scena):
    orch = make_run(session, kind="orchestration", flow_id=scena.flow.id, status="SUCCESS", task_id="", started_at=_t(0), finished_at=_t(5))
    pubblicata = make_datasource(session, name="Margin", project_id=scena.cartella.id, key="datasets/70/v1.parquet", rows=185, kind="flow", flow_id=scena.flow.id)
    figlio = make_run(session, kind="flow", flow_id=scena.flow.id, parent_run_id=orch.id, status="SUCCESS", task_id="t1", rows_written=185,
                      publish_name="Margin", publish_project_id=scena.cartella.id, datasource_id=pubblicata.id, started_at=_t(1), finished_at=_t(2))
    tabella = make_run(session, kind="flow", flow_id=scena.flow.id, parent_run_id=orch.id, status="FAILURE", task_id="t2", error="tabella bloccata",
                       destination=json.dumps({"type": "database", "connection_id": scena.pg.id, "table": "report.margini", "mode": "replace"}), started_at=_t(2), finished_at=_t(3))

    start, fine = ol.to_json(ol.run_events(session, figlio))
    assert (start["eventType"], fine["eventType"]) == ("START", "COMPLETE")
    assert start["eventTime"] == "2026-10-07T08:01:00.000Z" and fine["eventTime"] == "2026-10-07T08:02:00.000Z"
    assert fine["job"]["name"] == "Sample/Flows/Margin by Category.datasource Margin"
    assert fine["run"]["runId"] == ol.run_uuid(figlio.id)
    assert fine["run"]["facets"]["parent"]["run"]["runId"] == ol.run_uuid(orch.id)
    assert fine["run"]["facets"]["parent"]["job"]["name"] == "Sample/Flows/Margin by Category"
    assert [i["name"] for i in fine["inputs"]] == ["/Sample/Flows/orders", "/Sample/Flows/customers"]
    (out,) = fine["outputs"]
    assert out["name"] == "/Sample/Flows/Margin" and out["outputFacets"]["outputStatistics"]["rowCount"] == 185
    assert out["facets"]["tabularia"]["datasourceId"] == pubblicata.id
    assert [o["name"] for o in start["outputs"]] == [out["name"]] and start["outputs"][0]["outputFacets"] == {}   # dichiarati, senza statistiche

    _, fallito = ol.to_json(ol.run_events(session, tabella))
    assert fallito["eventType"] == "FAIL" and fallito["run"]["facets"]["errorMessage"]["message"] == "tabella bloccata"
    assert fallito["outputs"][0]["name"] == "erp.report.margini"

    _, orch_fine = ol.to_json(ol.run_events(session, orch))
    assert orch_fine["job"]["name"] == "Sample/Flows/Margin by Category" and "parent" not in orch_fine["run"]["facets"]
    assert sorted(o["name"] for o in orch_fine["outputs"]) == ["/Sample/Flows/Margin", "erp.report.margini"]   # l'unione dei figli


def test_il_refresh_di_una_datasource_e_un_job_dalla_tabella_al_parquet(session, scena):
    run = make_run(session, kind="ingest", datasource_id=scena.ordini.id, status="SUCCESS", task_id="t3", rows_written=12, started_at=_t(0), finished_at=_t(1))
    _, fine = ol.to_json(ol.run_events(session, run))
    assert fine["job"]["name"] == "Sample/Flows/orders (refresh)" and fine["job"]["facets"]["jobType"]["jobType"] == "REFRESH"
    assert fine["run"]["facets"]["tabularia"]["runId"] == run.id and "parent" not in fine["run"]["facets"]
    assert [(i["namespace"], i["name"]) for i in fine["inputs"]] == [("postgres://db.x.it:5432", "erp.vendite.ordini")]
    assert fine["outputs"][0]["name"] == "/Sample/Flows/orders" and fine["outputs"][0]["outputFacets"]["outputStatistics"]["rowCount"] == 12


# ── spento / acceso ──────────────────────────────────────────────────────────
def test_spento_non_fa_niente_acceso_manda_dopo_la_chiusura(session, scena, cattura, monkeypatch):
    run = make_run(session, kind="ingest", datasource_id=scena.ordini.id, status="SUCCESS", task_id="t4", rows_written=12)
    monkeypatch.setattr(get_settings().openlineage, "url", "")
    monkeypatch.delenv("OPENLINEAGE_URL", raising=False)
    monkeypatch.delenv("OPENLINEAGE_CONFIG", raising=False)
    assert ol.enabled() is False
    ol.run_closed(session, run)
    ol.wait_idle()
    assert cattura.eventi == []
    monkeypatch.setattr(get_settings().openlineage, "url", "http://collector:5000")
    assert ol.enabled() is True
    ol.run_closed(session, run)
    ol.wait_idle()
    assert [e["eventType"] for e in cattura.eventi] == ["START", "COMPLETE"]


def test_un_collector_che_rifiuta_non_tocca_il_run(session, scena, monkeypatch):
    class _Rotto(Transport):
        kind = "rotto"

        def emit(self, event):
            raise RuntimeError("502 dal collector")

    ol.set_client(OpenLineageClient(transport=_Rotto()))
    monkeypatch.setattr(get_settings().openlineage, "url", "http://collector:5000")
    run = make_run(session, kind="ingest", datasource_id=scena.ordini.id, status="SUCCESS", task_id="t5")
    ol.run_closed(session, run)   # non solleva
    ol.wait_idle()
    ol.set_client(None)
    session.refresh(run)
    assert run.status == "SUCCESS"


def test_il_client_http_si_costruisce_con_e_senza_chiave(monkeypatch):
    ol.set_client(None)
    monkeypatch.setattr(get_settings().openlineage, "url", "http://collector:5000")
    monkeypatch.setattr(get_settings().openlineage, "api_key", "")
    c = ol._get_client()
    assert c.transport.kind == "http" and c.transport._auth_headers(c.transport.config.auth) == {}
    ol.set_client(None)
    monkeypatch.setattr(get_settings().openlineage, "api_key", "segreto")
    c = ol._get_client()
    assert c.transport._auth_headers(c.transport.config.auth) == {"Authorization": "Bearer segreto"}
    ol.set_client(None)
    monkeypatch.setattr(get_settings().openlineage, "file", "/tmp/ol.jsonl")
    assert ol._get_client().transport.kind == "composite"     # http + file insieme
    ol.set_client(None)


# ── tutti i tipi di run e di dataset ─────────────────────────────────────────
def test_un_flusso_annidato_ha_per_padre_l_orchestrazione_della_radice(session, scena):
    radice = make_flow(session, name="Radice", project_id=scena.cartella.id, definition=json.dumps({"nodes": [{"id": "r", "type": "runflow", "data": {"flowId": scena.flow.id}}], "edges": []}))
    orch = make_run(session, kind="orchestration", flow_id=radice.id, status="SUCCESS", task_id="", started_at=_t(0), finished_at=_t(9))
    # dentro un runflow il figlio porta il parent_run_id della RADICE, ma il suo flow_id è quello del sotto-flusso
    figlio = make_run(session, kind="flow", flow_id=scena.flow.id, parent_run_id=orch.id, status="SUCCESS", task_id="t9",
                      destination=json.dumps({"type": "database", "connection_id": scena.pg.id, "table": "report.x", "mode": "append"}), started_at=_t(1), finished_at=_t(2))
    _, fine = ol.to_json(ol.run_events(session, figlio))
    assert fine["job"]["name"] == "Sample/Flows/Margin by Category.table report.x"
    # niente ParentRunFacet: Marquez lo rinominerebbe «Radice.Margin….table…», un job diverso da
    # quello che lo stesso Output ha quando il sotto-flusso gira da solo; il legame sta nel facet tabularia
    assert "parent" not in fine["run"]["facets"]
    assert fine["run"]["facets"]["tabularia"]["parentRunId"] == ol.run_uuid(orch.id)
    assert (fine["run"]["facets"]["tabularia"]["runId"], fine["run"]["facets"]["tabularia"]["kind"]) == (figlio.id, "flow")
    assert fine["outputs"][0]["facets"]["lifecycleStateChange"]["lifecycleStateChange"] == "ALTER"   # append


def test_un_refresh_da_query_sql_legge_le_tabelle_della_query(session, scena):
    run = make_run(session, kind="ingest", datasource_id=scena.clienti.id, status="SUCCESS", task_id="t6", rows_written=3, started_at=_t(0), finished_at=_t(1))
    _, fine = ol.to_json(ol.run_events(session, run))
    assert sorted(i["name"] for i in fine["inputs"]) == ["erp.crm.clienti", "erp.crm.regioni"]
    assert fine["job"]["facets"]["sql"]["query"].startswith("SELECT c.id")
    assert fine["outputs"][0]["name"] == "/Sample/Flows/customers"


def test_un_refresh_da_sharepoint_e_un_file_del_sito(session, scena):
    sp = Connection(name="intranet", db_type="sharepoint", project_id=scena.cartella.id, owner_id=scena.capo.id, host="https://acme.sharepoint.com/sites/vendite", port=None)
    session.add(sp)
    session.commit()
    ds = make_datasource(session, name="budget", project_id=scena.cartella.id, key="datasets/9/v1.parquet", rows=10, kind="database",
                         connection_id=sp.id, source_type="sharepoint", source_ref=json.dumps({"path": "Documenti/budget 2026.xlsx", "sheet": "Q1"}))
    run = make_run(session, kind="ingest", datasource_id=ds.id, status="SUCCESS", task_id="t7", rows_written=10)
    _, fine = ol.to_json(ol.run_events(session, run))
    (i,) = fine["inputs"]
    assert (i["namespace"], i["name"]) == ("sharepoint://acme.sharepoint.com/sites/vendite", "/Documenti/budget 2026.xlsx#Q1")


def test_output_su_s3_copia_specchio_ed_email(session, scena):
    s3 = make_run(session, kind="flow", flow_id=scena.flow.id, status="SUCCESS", task_id="t8", rows_written=5,
                  destination=json.dumps({"type": "s3", "connection_id": 77, "db_type": "s3", "endpoint": "https://storage.googleapis.com", "bucket": "lake", "key": "exports/margini.parquet", "format": "parquet"}))
    _, fine = ol.to_json(ol.run_events(session, s3))
    assert fine["job"]["name"].endswith(".file exports/margini.parquet")
    assert (fine["outputs"][0]["namespace"], fine["outputs"][0]["name"]) == ("gs://lake", "/exports/margini.parquet")
    assert fine["outputs"][0]["outputFacets"]["outputStatistics"]["rowCount"] == 5

    pubblicata = make_datasource(session, name="Margin", project_id=scena.cartella.id, key="datasets/70/v1.parquet", rows=185, kind="flow", flow_id=scena.flow.id)
    specchio = make_run(session, kind="flow", flow_id=scena.flow.id, status="SUCCESS", task_id="t10", rows_written=185, publish_name="Margin", publish_project_id=scena.cartella.id,
                        datasource_id=pubblicata.id, mirror=json.dumps({"connection_id": 5, "endpoint": "s3.fr-par.scw.cloud", "bucket": "mirror", "key": "margin/latest.parquet", "format": "parquet", "ok": True}))
    _, fine = ol.to_json(ol.run_events(session, specchio))
    assert [(o["namespace"], o["name"]) for o in fine["outputs"]] == [("tabularia://tabularia", "/Sample/Flows/Margin"), ("s3://mirror", "/margin/latest.parquet")]

    email = make_run(session, kind="flow", flow_id=scena.flow.id, status="SUCCESS", task_id="t11", email=json.dumps({"to": ["a@x.it"], "sent": True}))
    _, fine = ol.to_json(ol.run_events(session, email))
    assert fine["job"]["name"].endswith(".email") and fine["outputs"] == []      # un'email non è un dataset


def test_un_orchestrazione_fallita_porta_il_messaggio_e_un_file_caricato_e_un_input(session, scena):
    caricato = {"nodes": [{"id": "s", "type": "source", "data": {"bucket": "tabularia", "parquetKey": "uploads/u1/vendite.parquet", "filename": "vendite.csv"}},
                          {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "Vendite", "projectId": scena.cartella.id}}], "edges": []}
    flow = make_flow(session, name="Da file", project_id=scena.cartella.id, definition=json.dumps(caricato))
    orch = make_run(session, kind="orchestration", flow_id=flow.id, status="FAILURE", task_id="", error="output: run 9 FAILURE — colonna mancante", started_at=_t(0), finished_at=_t(1))
    start, fine = ol.to_json(ol.run_events(session, orch))
    assert fine["eventType"] == "FAIL" and fine["run"]["facets"]["errorMessage"]["message"].startswith("output: run 9")
    assert [(i["namespace"], i["name"]) for i in fine["inputs"]] == [("s3://tabularia", "/uploads/u1/vendite.parquet")]
    assert fine["outputs"] == []     # nessun figlio riuscito: niente scritto


def test_il_collector_di_datahub_vuole_un_altro_endpoint(monkeypatch):
    ol.set_client(None)
    monkeypatch.setattr(get_settings().openlineage, "url", "https://datahub.acme.it")
    monkeypatch.setattr(get_settings().openlineage, "endpoint", "openapi/openlineage/api/v1/lineage")
    monkeypatch.setattr(get_settings().openlineage, "api_key", "tok")
    c = ol._get_client()
    assert c.transport.config.endpoint == "openapi/openlineage/api/v1/lineage" and c.transport.config.url == "https://datahub.acme.it"
    ol.set_client(None)
    assert "datahub.acme.it" in ol.describe() and "openapi/openlineage" in ol.describe()
