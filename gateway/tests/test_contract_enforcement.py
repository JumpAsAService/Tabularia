"""The data contract at the moment that counts: when a datasource is about to receive new
data, from a refresh or from a flow.

The worker evaluated the data just written and the report arrives with the task's
result. With a BLOCKING rule violated the snapshot is NOT swapped: the datasource stays
as it was — the same key, the same rows — and the run fails saying why. With warnings
only the data is published and the state says so.
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
BROKEN = {"outcome": "failed", "version": 1, "rows": 9, "errors": 1, "warnings": 0, "rules": [
    {"id": "r1", "kind": "not_null", "severity": "error", "column": "id", "passed": False, "violations": 4},
    {"id": "r2", "kind": "accepted_values", "severity": "warning", "column": "stato", "passed": True, "violations": 0}]}
WARNING = {"outcome": "warning", "version": 1, "rows": 9, "errors": 0, "warnings": 1, "rules": [
    {"id": "r1", "kind": "not_null", "severity": "error", "column": "id", "passed": True, "violations": 0},
    {"id": "r2", "kind": "accepted_values", "severity": "warning", "column": "stato", "passed": False, "violations": 2}]}
CLEAN = {"outcome": "passed", "version": 1, "rows": 9, "errors": 0, "warnings": 0, "rules": []}


def _result(report=None, rows=9):
    r = {"bucket": "data-prep", "rows_written": rows, "columns": [{"name": "id", "dtype": "Int64"}]}
    return {**r, "contract": report} if report is not None else r


@pytest.fixture
def ds(session):
    d = make_datasource(session, name="ordini", kind="database", key="datasets/1/buono.parquet", rows=100)
    contracts.save(session, d, DOC, True, None)
    session.commit()
    return d


def _refresh(session, fake_engine, ds, report, task="t-1", key="datasets/1/nuovo.parquet"):
    run = make_run(session, kind="ingest", status="STARTED", task_id=task, datasource_id=ds.id, output_key=key)
    fake_engine.set_task(task, "SUCCESS", result=_result(report))
    return run


def _being_deleted(session):
    return {r.key for r in session.exec(select(PendingBlobDeletion)).all()}


# ── refresh ──────────────────────────────────────────────────────────────────
async def test_a_refresh_violating_a_blocking_rule_does_not_touch_the_datasource(session, fake_engine, ds):
    run = await _reconcile(session, _refresh(session, fake_engine, ds, BROKEN))
    session.refresh(ds)
    # the datasource is EXACTLY the one of before
    assert (ds.key, ds.rows, ds.snapshot_run_id) == ("datasets/1/buono.parquet", 100, None)
    # the run fails and says which rule
    assert run.status == "FAILURE" and run.finished_at is not None
    assert "Data contract violated" in run.error and "not_null on id (4 rows)" in run.error
    assert "was NOT published" in run.error
    # the refused data goes, the good data stays
    assert _being_deleted(session) == {"datasets/1/nuovo.parquet"}
    # the state: refused, with the run that caused it
    c = contracts.get(session, ds.id)
    assert contracts.summary(c)["status"] == "failed" and c.blocked_run_id == run.id
    (history,) = session.exec(select(DataContractResult)).all()
    assert (history.trigger, history.outcome, history.blocked, history.run_id) == ("refresh", "failed", True, run.id)


async def test_a_refresh_with_warnings_only_publishes_and_says_so(session, fake_engine, ds):
    run = await _reconcile(session, _refresh(session, fake_engine, ds, WARNING))
    session.refresh(ds)
    assert (run.status, ds.key, ds.rows, ds.snapshot_run_id) == ("SUCCESS", "datasets/1/nuovo.parquet", 9, run.id)
    c = contracts.get(session, ds.id)
    assert (contracts.summary(c)["status"], c.warnings, c.blocked_at) == ("warning", 1, None)
    assert json.loads(c.last_report)["rules"][1]["violations"] == 2
    assert _being_deleted(session) == {"datasets/1/buono.parquet"}   # the overtaken snapshot, as always


async def test_a_successful_refresh_clears_the_refusals_mark(session, fake_engine, ds):
    await _reconcile(session, _refresh(session, fake_engine, ds, BROKEN, "t-1", "datasets/1/rotto.parquet"))
    assert contracts.summary(contracts.get(session, ds.id))["blocked"] is True
    await _reconcile(session, _refresh(session, fake_engine, ds, CLEAN, "t-2", "datasets/1/sano.parquet"))
    session.refresh(ds)
    c = contracts.get(session, ds.id)
    session.refresh(c)
    assert ds.key == "datasets/1/sano.parquet" and contracts.summary(c) | {"checked_at": None} == {
        "status": "passed", "checked_at": None, "errors": 0, "warnings": 0, "blocked": False, "version": 1}
    assert [(r.outcome, r.blocked) for r in session.exec(select(DataContractResult).order_by(DataContractResult.id)).all()] == [("failed", True), ("passed", False)]


async def test_a_check_by_hand_after_a_refusal_does_not_silence_the_alarm(session, fake_engine, ds):
    """The current data holds the contract, but it is the old data: until an update succeeds,
    the datasource is not «fine»."""
    await _reconcile(session, _refresh(session, fake_engine, ds, BROKEN))
    c = contracts.get(session, ds.id)
    contracts.record_result(session, c, CLEAN, trigger="manual", snapshot_key=ds.key)
    session.commit()
    assert (c.status, contracts.summary(c)["status"], contracts.summary(c)["blocked"]) == ("passed", "failed", True)


async def test_without_contract_or_report_everything_as_before(session, fake_engine):
    free = make_datasource(session, name="libera", kind="database", key="datasets/2/v0.parquet")
    run = make_run(session, kind="ingest", status="STARTED", task_id="t-9", datasource_id=free.id, output_key="datasets/2/v1.parquet")
    fake_engine.set_task("t-9", "SUCCESS", result=_result())
    assert (await _reconcile(session, run)).status == "SUCCESS"
    session.refresh(free)
    assert free.key == "datasets/2/v1.parquet" and session.exec(select(DataContractResult)).all() == []


async def test_a_contract_removed_while_the_run_went_refuses_nothing_any_more(session, fake_engine, ds):
    run = _refresh(session, fake_engine, ds, BROKEN)
    contracts.forget(session, ds.id, keep_history=True)
    session.commit()
    assert (await _reconcile(session, run)).status == "SUCCESS"
    session.refresh(ds)
    assert ds.key == "datasets/1/nuovo.parquet"


async def test_an_overtaken_run_does_not_write_the_state(session, fake_engine, ds):
    """A starts first, B later and finishes first: A's snapshot is not published (B's,
    fresher, is there already), so its report does not speak of the data the datasource
    serves."""
    a = _refresh(session, fake_engine, ds, WARNING, "t-a", "datasets/1/a.parquet")
    b = _refresh(session, fake_engine, ds, CLEAN, "t-b", "datasets/1/b.parquet")
    await _reconcile(session, b)
    await _reconcile(session, a)
    session.refresh(ds)
    c = contracts.get(session, ds.id)
    assert ds.key == "datasets/1/b.parquet" and c.status == "passed"
    assert len(session.exec(select(DataContractResult)).all()) == 1


async def test_the_contract_goes_with_the_refresh_only_when_there_and_on(session, fake_engine, ds):
    from app.models import Connection

    boss = make_user(session, email="capo@x.it", is_superuser=True)
    conn = Connection(name="c", db_type="postgresql", project_id=1, owner_id=boss.id, host="h", port=5432, username="u", database="d")
    session.add(conn); session.commit(); session.refresh(conn)
    ds.connection_id, ds.source_type, ds.source_ref = conn.id, "table", "t"
    session.add(ds); session.commit()

    await launch_ingest_run(session, boss, ds, conn)
    sent = fake_engine.ingests[-1][1]["contract"]
    assert sent["version"] == 1 and [r["id"] for r in sent["rules"]] == ["r1", "r2"]

    c = contracts.get(session, ds.id)
    c.enabled = False
    session.add(c); session.commit()
    await launch_ingest_run(session, boss, ds, conn)
    assert "contract" not in fake_engine.ingests[-1][1]


# ── a flow's publishing ──────────────────────────────────────────────────────
@pytest.fixture
def published(session):
    boss = make_user(session, email="capo@x.it", is_superuser=True)
    p = make_project(session, name="p")
    flow = make_flow(session, name="f", project_id=p.id)
    d = make_datasource(session, name="vendite", kind="flow", project_id=p.id, key="datasets/5/buono.parquet", rows=50, flow_id=flow.id)
    contracts.save(session, d, DOC, True, None)
    session.commit()
    body = RunCreate(bucket="data-prep", input_key="datasets/x.parquet", operations=[],
                      publish=PublishSpec(name="vendite", project_id=p.id, overwrite=True))
    return boss, flow, d, body


async def test_the_contract_goes_with_the_flow_overwriting_the_datasource(session, fake_engine, published):
    boss, flow, d, body = published
    await _launch_flow_run(session, boss, flow, body)
    assert fake_engine.transforms[-1]["contract"]["version"] == 1
    # a new name creates a datasource: there is no contract to respect yet
    new_one = RunCreate(bucket="data-prep", input_key="datasets/x.parquet", operations=[], publish=PublishSpec(name="mai-vista", project_id=d.project_id))
    await _launch_flow_run(session, boss, flow, new_one)
    assert "contract" not in fake_engine.transforms[-1]


async def test_a_flow_violating_a_blocking_rule_does_not_overwrite(session, fake_engine, published):
    boss, flow, d, body = published
    run = await _launch_flow_run(session, boss, flow, body)
    fake_engine.set_task(run.task_id, "SUCCESS", result=_result(BROKEN))
    run = await _reconcile(session, run)
    session.refresh(d)
    assert run.status == "FAILURE" and "Data contract violated" in run.error and run.datasource_id is None
    assert (d.key, d.rows) == ("datasets/5/buono.parquet", 50)
    assert run.output_key in _being_deleted(session) and "datasets/5/buono.parquet" not in _being_deleted(session)
    c = contracts.get(session, d.id)
    assert contracts.summary(c)["status"] == "failed" and c.blocked_run_id == run.id
    assert session.exec(select(DataContractResult)).one().trigger == "publish"


async def test_a_flow_with_warnings_only_publishes(session, fake_engine, published):
    boss, flow, d, body = published
    run = await _launch_flow_run(session, boss, flow, body)
    fake_engine.set_task(run.task_id, "SUCCESS", result=_result(WARNING))
    run = await _reconcile(session, run)
    session.refresh(d)
    assert (run.status, run.datasource_id, d.rows, d.key) == ("SUCCESS", d.id, 9, run.output_key)
    assert contracts.summary(contracts.get(session, d.id))["status"] == "warning"


# ── the message ──────────────────────────────────────────────────────────────
def test_the_message_names_only_the_blocking_rules_and_stays_short():
    report = {"rules": [{"id": f"r{i}", "kind": "not_null", "severity": "error", "column": f"c{i}", "passed": False, "violations": i + 1} for i in range(9)]
               + [{"id": "w", "kind": "pattern", "severity": "warning", "column": "avviso", "passed": False, "violations": 3}]}
    m = contracts.broken_rules_message(report)
    assert "not_null on c0 (1 rows)" in m and "and 3 more" in m and "avviso" not in m
    assert "could not be evaluated: storage giù" in contracts.broken_rules_message({"rules": [], "error": "storage giù"})


# ── freshness: it breaks without anything arriving ───────────────────────────
def _with_freshness(session, hours=24, severity="warning"):
    d = make_datasource(session, name="fresca", kind="database", key="datasets/7/v.parquet")
    doc = {"description": "", "rules": [{"id": "f", "kind": "freshness", "max_age_hours": hours, "severity": severity},
                                        {"id": "n", "kind": "not_null", "column": "id", "severity": "error"}]}
    c = contracts.save(session, d, doc, True, None)
    contracts.record_result(session, c, {"outcome": "passed", "rows": 1, "errors": 0, "warnings": 0, "rules": [
        {"id": "f", "kind": "freshness", "severity": severity, "passed": True, "observed": 0.0, "expected": hours},
        {"id": "n", "kind": "not_null", "severity": "error", "column": "id", "passed": True, "violations": 0}]}, trigger="refresh")
    d.refreshed_at = datetime(2026, 10, 1, 8, 0, 0)
    session.add(d); session.commit()
    return d, c


@pytest.mark.parametrize("severity, outcome", [("warning", "warning"), ("error", "failed")])
def test_data_too_old_changes_the_state_once_only(session, severity, outcome):
    d, c = _with_freshness(session, 24, severity)
    presto = datetime(2026, 10, 1, 20, 0, 0, tzinfo=timezone.utc)
    assert contracts.sweep_freshness(session, presto) == 0 and c.status == "passed"
    later = presto + timedelta(hours=20)                       # 32 hours after the last refresh
    assert contracts.sweep_freshness(session, later) == 1
    session.refresh(c)
    rule = json.loads(c.last_report)["rules"][0]
    assert (c.status, rule["passed"], rule["observed"]) == (outcome, False, 32.0)
    assert json.loads(c.last_report)["rules"][1]["passed"] is True     # the other rules stay as they were
    # at the next rounds the state is that already: no repeated reports
    assert contracts.sweep_freshness(session, later + timedelta(hours=5)) == 0
    assert [r.trigger for r in session.exec(select(DataContractResult).order_by(DataContractResult.id)).all()] == ["refresh", "freshness"]


def test_a_refresh_puts_freshness_right(session):
    d, c = _with_freshness(session)
    contracts.sweep_freshness(session, datetime(2026, 10, 5, tzinfo=timezone.utc))
    session.refresh(c)
    assert c.status == "warning"
    d.refreshed_at = datetime(2026, 10, 5, 1, 0, 0)
    session.add(d); session.commit()
    assert contracts.sweep_freshness(session, datetime(2026, 10, 5, 2, tzinfo=timezone.utc)) == 1
    session.refresh(c)
    assert c.status == "passed"
