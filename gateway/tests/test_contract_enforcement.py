"""Il data contract nel momento che conta: quando una datasource sta per ricevere
dati nuovi, da un refresh o da un flusso.

Il worker ha valutato i dati appena scritti e il referto arriva col risultato del
task. Con una regola BLOCCANTE violata lo snapshot NON si scambia: la datasource
resta com'era — stessa chiave, stesse righe — e il run fallisce dicendo perché.
Con soli avvisi i dati si pubblicano e lo stato lo dice.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.models import DataContractResult, Datasource, PendingBlobDeletion, Run
from app.routes.runs import _launch_flow_run, _reconcile, launch_ingest_run
from app.schemas.models import PublishSpec, RunCreate
from app.services import contracts
from tests.conftest import make_datasource, make_flow, make_project, make_run, make_user

pytestmark = pytest.mark.anyio

DOC = {"description": "", "rules": [
    {"id": "r1", "kind": "not_null", "column": "id", "severity": "error"},
    {"id": "r2", "kind": "accepted_values", "column": "stato", "values": ["a"], "severity": "warning"},
]}
ROTTO = {"outcome": "failed", "version": 1, "rows": 9, "errors": 1, "warnings": 0, "rules": [
    {"id": "r1", "kind": "not_null", "severity": "error", "column": "id", "passed": False, "violations": 4},
    {"id": "r2", "kind": "accepted_values", "severity": "warning", "column": "stato", "passed": True, "violations": 0}]}
AVVISO = {"outcome": "warning", "version": 1, "rows": 9, "errors": 0, "warnings": 1, "rules": [
    {"id": "r1", "kind": "not_null", "severity": "error", "column": "id", "passed": True, "violations": 0},
    {"id": "r2", "kind": "accepted_values", "severity": "warning", "column": "stato", "passed": False, "violations": 2}]}
PULITO = {"outcome": "passed", "version": 1, "rows": 9, "errors": 0, "warnings": 0, "rules": []}


def _risultato(referto=None, righe=9):
    r = {"bucket": "data-prep", "rows_written": righe, "columns": [{"name": "id", "dtype": "Int64"}]}
    return {**r, "contract": referto} if referto is not None else r


@pytest.fixture
def ds(session):
    d = make_datasource(session, name="ordini", kind="database", key="datasets/1/buono.parquet", rows=100)
    contracts.save(session, d, DOC, True, None)
    session.commit()
    return d


def _refresh(session, fake_engine, ds, referto, task="t-1", chiave="datasets/1/nuovo.parquet"):
    run = make_run(session, kind="ingest", status="STARTED", task_id=task, datasource_id=ds.id, output_key=chiave)
    fake_engine.set_task(task, "SUCCESS", result=_risultato(referto))
    return run


def _in_cancellazione(session):
    return {r.key for r in session.exec(select(PendingBlobDeletion)).all()}


# ── refresh ──────────────────────────────────────────────────────────────────
async def test_un_refresh_che_viola_una_regola_bloccante_non_tocca_la_datasource(session, fake_engine, ds):
    run = await _reconcile(session, _refresh(session, fake_engine, ds, ROTTO))
    session.refresh(ds)
    # la datasource è ESATTAMENTE quella di prima
    assert (ds.key, ds.rows, ds.snapshot_run_id) == ("datasets/1/buono.parquet", 100, None)
    # il run fallisce e dice quale regola
    assert run.status == "FAILURE" and run.finished_at is not None
    assert "Data contract violato" in run.error and "not_null su id (4 righe)" in run.error
    assert "NON sono stati pubblicati" in run.error
    # i dati rifiutati se ne vanno, quelli buoni restano
    assert _in_cancellazione(session) == {"datasets/1/nuovo.parquet"}
    # lo stato: rifiutato, con il run che lo ha causato
    c = contracts.get(session, ds.id)
    assert contracts.summary(c)["status"] == "failed" and c.blocked_run_id == run.id
    (storico,) = session.exec(select(DataContractResult)).all()
    assert (storico.trigger, storico.outcome, storico.blocked, storico.run_id) == ("refresh", "failed", True, run.id)


async def test_un_refresh_con_soli_avvisi_pubblica_e_lo_dice(session, fake_engine, ds):
    run = await _reconcile(session, _refresh(session, fake_engine, ds, AVVISO))
    session.refresh(ds)
    assert (run.status, ds.key, ds.rows, ds.snapshot_run_id) == ("SUCCESS", "datasets/1/nuovo.parquet", 9, run.id)
    c = contracts.get(session, ds.id)
    assert (contracts.summary(c)["status"], c.warnings, c.blocked_at) == ("warning", 1, None)
    assert json.loads(c.last_report)["rules"][1]["violations"] == 2
    assert _in_cancellazione(session) == {"datasets/1/buono.parquet"}   # lo snapshot superato, come sempre


async def test_un_refresh_riuscito_cancella_il_segno_del_rifiuto(session, fake_engine, ds):
    await _reconcile(session, _refresh(session, fake_engine, ds, ROTTO, "t-1", "datasets/1/rotto.parquet"))
    assert contracts.summary(contracts.get(session, ds.id))["blocked"] is True
    await _reconcile(session, _refresh(session, fake_engine, ds, PULITO, "t-2", "datasets/1/sano.parquet"))
    session.refresh(ds)
    c = contracts.get(session, ds.id)
    session.refresh(c)
    assert ds.key == "datasets/1/sano.parquet" and contracts.summary(c) | {"checked_at": None} == {
        "status": "passed", "checked_at": None, "errors": 0, "warnings": 0, "blocked": False, "version": 1}
    assert [(r.outcome, r.blocked) for r in session.exec(select(DataContractResult).order_by(DataContractResult.id)).all()] == [("failed", True), ("passed", False)]


async def test_una_verifica_a_mano_dopo_un_rifiuto_non_spegne_l_allarme(session, fake_engine, ds):
    """I dati correnti reggono il contratto, ma sono quelli vecchi: finché un
    aggiornamento non riesce, la datasource non è «a posto»."""
    await _reconcile(session, _refresh(session, fake_engine, ds, ROTTO))
    c = contracts.get(session, ds.id)
    contracts.record_result(session, c, PULITO, trigger="manual", snapshot_key=ds.key)
    session.commit()
    assert (c.status, contracts.summary(c)["status"], contracts.summary(c)["blocked"]) == ("passed", "failed", True)


async def test_senza_contratto_o_senza_referto_tutto_come_prima(session, fake_engine):
    libera = make_datasource(session, name="libera", kind="database", key="datasets/2/v0.parquet")
    run = make_run(session, kind="ingest", status="STARTED", task_id="t-9", datasource_id=libera.id, output_key="datasets/2/v1.parquet")
    fake_engine.set_task("t-9", "SUCCESS", result=_risultato())
    assert (await _reconcile(session, run)).status == "SUCCESS"
    session.refresh(libera)
    assert libera.key == "datasets/2/v1.parquet" and session.exec(select(DataContractResult)).all() == []


async def test_un_contratto_tolto_mentre_il_run_girava_non_rifiuta_piu_niente(session, fake_engine, ds):
    run = _refresh(session, fake_engine, ds, ROTTO)
    contracts.forget(session, ds.id, keep_history=True)
    session.commit()
    assert (await _reconcile(session, run)).status == "SUCCESS"
    session.refresh(ds)
    assert ds.key == "datasets/1/nuovo.parquet"


async def test_un_run_superato_non_scrive_lo_stato(session, fake_engine, ds):
    """A parte prima, B dopo e finisce prima: lo snapshot di A non viene pubblicato
    (c'è già quello di B, più fresco), quindi il suo referto non parla dei dati
    che la datasource serve."""
    a = _refresh(session, fake_engine, ds, AVVISO, "t-a", "datasets/1/a.parquet")
    b = _refresh(session, fake_engine, ds, PULITO, "t-b", "datasets/1/b.parquet")
    await _reconcile(session, b)
    await _reconcile(session, a)
    session.refresh(ds)
    c = contracts.get(session, ds.id)
    assert ds.key == "datasets/1/b.parquet" and c.status == "passed"
    assert len(session.exec(select(DataContractResult)).all()) == 1


async def test_il_contratto_parte_con_il_refresh_solo_se_c_e_ed_e_acceso(session, fake_engine, ds):
    from app.models import Connection

    capo = make_user(session, email="capo@x.it", is_superuser=True)
    conn = Connection(name="c", db_type="postgresql", project_id=1, owner_id=capo.id, host="h", port=5432, username="u", database="d")
    session.add(conn); session.commit(); session.refresh(conn)
    ds.connection_id, ds.source_type, ds.source_ref = conn.id, "table", "t"
    session.add(ds); session.commit()

    await launch_ingest_run(session, capo, ds, conn)
    inviato = fake_engine.ingests[-1][1]["contract"]
    assert inviato["version"] == 1 and [r["id"] for r in inviato["rules"]] == ["r1", "r2"]

    c = contracts.get(session, ds.id)
    c.enabled = False
    session.add(c); session.commit()
    await launch_ingest_run(session, capo, ds, conn)
    assert "contract" not in fake_engine.ingests[-1][1]


# ── pubblicazione di un flusso ───────────────────────────────────────────────
@pytest.fixture
def pubblicata(session):
    capo = make_user(session, email="capo@x.it", is_superuser=True)
    p = make_project(session, name="p")
    flusso = make_flow(session, name="f", project_id=p.id)
    d = make_datasource(session, name="vendite", kind="flow", project_id=p.id, key="datasets/5/buono.parquet", rows=50, flow_id=flusso.id)
    contracts.save(session, d, DOC, True, None)
    session.commit()
    corpo = RunCreate(bucket="data-prep", input_key="datasets/x.parquet", operations=[],
                      publish=PublishSpec(name="vendite", project_id=p.id, overwrite=True))
    return capo, flusso, d, corpo


async def test_il_contratto_parte_con_il_flusso_che_sovrascrive_la_datasource(session, fake_engine, pubblicata):
    capo, flusso, d, corpo = pubblicata
    await _launch_flow_run(session, capo, flusso, corpo)
    assert fake_engine.transforms[-1]["contract"]["version"] == 1
    # un nome nuovo crea una datasource: non c'è ancora nessun contratto da rispettare
    nuova = RunCreate(bucket="data-prep", input_key="datasets/x.parquet", operations=[], publish=PublishSpec(name="mai-vista", project_id=d.project_id))
    await _launch_flow_run(session, capo, flusso, nuova)
    assert "contract" not in fake_engine.transforms[-1]


async def test_un_flusso_che_viola_una_regola_bloccante_non_sovrascrive(session, fake_engine, pubblicata):
    capo, flusso, d, corpo = pubblicata
    run = await _launch_flow_run(session, capo, flusso, corpo)
    fake_engine.set_task(run.task_id, "SUCCESS", result=_risultato(ROTTO))
    run = await _reconcile(session, run)
    session.refresh(d)
    assert run.status == "FAILURE" and "Data contract violato" in run.error and run.datasource_id is None
    assert (d.key, d.rows) == ("datasets/5/buono.parquet", 50)
    assert run.output_key in _in_cancellazione(session) and "datasets/5/buono.parquet" not in _in_cancellazione(session)
    c = contracts.get(session, d.id)
    assert contracts.summary(c)["status"] == "failed" and c.blocked_run_id == run.id
    assert session.exec(select(DataContractResult)).one().trigger == "publish"


async def test_un_flusso_con_soli_avvisi_pubblica(session, fake_engine, pubblicata):
    capo, flusso, d, corpo = pubblicata
    run = await _launch_flow_run(session, capo, flusso, corpo)
    fake_engine.set_task(run.task_id, "SUCCESS", result=_risultato(AVVISO))
    run = await _reconcile(session, run)
    session.refresh(d)
    assert (run.status, run.datasource_id, d.rows, d.key) == ("SUCCESS", d.id, 9, run.output_key)
    assert contracts.summary(contracts.get(session, d.id))["status"] == "warning"


# ── il messaggio ─────────────────────────────────────────────────────────────
def test_il_messaggio_nomina_solo_le_regole_bloccanti_e_resta_breve():
    referto = {"rules": [{"id": f"r{i}", "kind": "not_null", "severity": "error", "column": f"c{i}", "passed": False, "violations": i + 1} for i in range(9)]
               + [{"id": "w", "kind": "pattern", "severity": "warning", "column": "avviso", "passed": False, "violations": 3}]}
    m = contracts.broken_rules_message(referto)
    assert "not_null su c0 (1 righe)" in m and "e altre 3" in m and "avviso" not in m
    assert "non valutabile: storage giù" in contracts.broken_rules_message({"rules": [], "error": "storage giù"})


# ── freschezza: si rompe senza che arrivi niente ─────────────────────────────
def _con_freschezza(session, ore=24, severita="warning"):
    d = make_datasource(session, name="fresca", kind="database", key="datasets/7/v.parquet")
    doc = {"description": "", "rules": [{"id": "f", "kind": "freshness", "max_age_hours": ore, "severity": severita},
                                        {"id": "n", "kind": "not_null", "column": "id", "severity": "error"}]}
    c = contracts.save(session, d, doc, True, None)
    contracts.record_result(session, c, {"outcome": "passed", "rows": 1, "errors": 0, "warnings": 0, "rules": [
        {"id": "f", "kind": "freshness", "severity": severita, "passed": True, "observed": 0.0, "expected": ore},
        {"id": "n", "kind": "not_null", "severity": "error", "column": "id", "passed": True, "violations": 0}]}, trigger="refresh")
    d.refreshed_at = datetime(2026, 10, 1, 8, 0, 0)
    session.add(d); session.commit()
    return d, c


@pytest.mark.parametrize("severita, esito", [("warning", "warning"), ("error", "failed")])
def test_dati_troppo_vecchi_cambiano_lo_stato_una_volta_sola(session, severita, esito):
    d, c = _con_freschezza(session, 24, severita)
    presto = datetime(2026, 10, 1, 20, 0, 0, tzinfo=timezone.utc)
    assert contracts.sweep_freshness(session, presto) == 0 and c.status == "passed"
    tardi = presto + timedelta(hours=20)                       # 32 ore dopo l'ultimo refresh
    assert contracts.sweep_freshness(session, tardi) == 1
    session.refresh(c)
    regola = json.loads(c.last_report)["rules"][0]
    assert (c.status, regola["passed"], regola["observed"]) == (esito, False, 32.0)
    assert json.loads(c.last_report)["rules"][1]["passed"] is True     # le altre regole restano com'erano
    # ai giri successivi lo stato è già quello: niente referti a ripetizione
    assert contracts.sweep_freshness(session, tardi + timedelta(hours=5)) == 0
    assert [r.trigger for r in session.exec(select(DataContractResult).order_by(DataContractResult.id)).all()] == ["refresh", "freshness"]


def test_un_refresh_riporta_la_freschezza_a_posto(session):
    d, c = _con_freschezza(session)
    contracts.sweep_freshness(session, datetime(2026, 10, 5, tzinfo=timezone.utc))
    session.refresh(c)
    assert c.status == "warning"
    d.refreshed_at = datetime(2026, 10, 5, 1, 0, 0)
    session.add(d); session.commit()
    assert contracts.sweep_freshness(session, datetime(2026, 10, 5, 2, tzinfo=timezone.utc)) == 1
    session.refresh(c)
    assert c.status == "passed"
