"""Runs' integrity: the truth of the state, the order of the snapshots, orphaned
orchestrations.

Regression for the three HIGH findings of the 2026-09-13 audit (docs/audit/):

- **C1** a run declared expired was marked FAILURE without stopping the task, which
  kept running and WRITING: on an Output in append the user's relaunch appended the
  same rows twice. Also the stopwatch started from the row's birth, so it counted the
  wait in the QUEUE and could declare expired a run just started.
- **C2** the snapshot's swap was «whoever finishes wins»: a slow refresh started
  EARLIER but finished LATER replaced fresher data and scheduled its deletion.
- **C3** the orchestrations stayed STARTED forever after a gateway restart (they run
  as asyncio tasks in the process, not as Celery tasks, and `_reconcile` skips them).
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.models import Datasource, PendingBlobDeletion, Run
from app.routes.runs import (
    _finalize_ingest,
    _publish_datasource,
    _reconcile,
    snapshot_key,
)
from app.services.objects import MANAGED_PREFIXES
from app.services.orchestrator import (
    ORCHESTRATION_INTERRUPTED,
    close_orphans,
)
from tests.conftest import make_datasource, make_run

pytestmark = pytest.mark.anyio


def _pending_keys(session) -> list[str]:
    return [r.key for r in session.exec(select(PendingBlobDeletion)).all()]


def _ago(seconds: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


# ── C1: the state tells the truth ───────────────────────────────────────────
async def test_stale_run_is_revoked_not_just_marked_failed(session, fake_engine, monkeypatch):
    """The heart of the finding: marking FAILURE without revoking leaves a task that writes
    on the quiet. Now the task is really stopped."""
    import app.routes.runs as runs_mod

    monkeypatch.setattr(runs_mod, "stale_after_seconds", lambda: 60)
    run = make_run(
        session, kind="flow", status="STARTED", task_id="t-stale", flow_id=1,
        started_at=_ago(600), engine_started_at=_ago(600),
    )
    fake_engine.set_task("t-stale", "STARTED")  # the engine still says it is running

    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "FAILURE"
    assert fake_engine.revoked == ["t-stale"]  # stopped on the engine, not only in the history
    assert "stopped" in (run.error or "")


async def test_queue_wait_is_not_counted_as_execution_time(session, fake_engine, monkeypatch):
    """A run long left in the QUEUE and just started is NOT expired: the stopwatch starts
    from the real run, not from the row's birth."""
    import app.routes.runs as runs_mod

    monkeypatch.setattr(runs_mod, "stale_after_seconds", lambda: 60)
    run = make_run(
        session, kind="flow", status="STARTED", task_id="t-queued", flow_id=1,
        started_at=_ago(3600),      # queued for an hour…
        engine_started_at=_ago(5),  # …but running for 5 seconds
    )
    fake_engine.set_task("t-queued", "STARTED")

    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "STARTED"  # no false failure
    assert fake_engine.revoked == []


async def test_execution_start_recorded_on_first_started_observation(session, fake_engine):
    run = make_run(session, kind="flow", status="PENDING", task_id="t-start", flow_id=1)
    assert run.engine_started_at is None
    fake_engine.set_task("t-start", "STARTED")

    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "STARTED"
    assert run.engine_started_at is not None  # the stopwatch's baseline


async def test_completed_run_is_never_revoked(session, fake_engine):
    run = make_run(session, kind="flow", status="STARTED", task_id="t-done", flow_id=1)
    fake_engine.set_task("t-done", "SUCCESS", result={"rows_written": 1, "columns": []})

    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "SUCCESS"
    assert fake_engine.revoked == []


# ── C2: whoever read last wins, not whoever finishes last ───────────────────
async def test_refresh_started_earlier_does_not_clobber_fresher_snapshot(session, fake_engine):
    """The finding's exact scenario, end to end: A starts first, B starts later and finishes
    first. When A finishes it must NOT take the datasource back."""
    ds = make_datasource(session, name="ordini", kind="database", key="datasets/1/v0.parquet")
    run_a = make_run(session, kind="ingest", status="STARTED", task_id="t-a",
                     datasource_id=ds.id, output_key="datasets/1/a.parquet")
    run_b = make_run(session, kind="ingest", status="STARTED", task_id="t-b",
                     datasource_id=ds.id, output_key="datasets/1/b.parquet")
    assert run_a.id < run_b.id  # A read the source EARLIER
    ok = {"bucket": "data-prep", "rows_written": 1, "columns": []}
    fake_engine.set_task("t-a", "SUCCESS", result=ok)
    fake_engine.set_task("t-b", "SUCCESS", result=ok)

    await _reconcile(session, run_b)  # B finishes first
    await _reconcile(session, run_a)  # A finishes later, but it is older

    session.refresh(ds)
    assert ds.key == "datasets/1/b.parquet"       # the freshest data stays
    assert ds.snapshot_run_id == run_b.id
    assert "datasets/1/a.parquet" in _pending_keys(session)  # the stale parquet is orphaned
    assert "datasets/1/b.parquet" not in _pending_keys(session)  # the fresh one is NOT deleted


async def test_finalize_ingest_accepts_newer_run_and_records_baseline(session, fake_engine):
    ds = make_datasource(session, name="live", kind="database", key="datasets/1/old.parquet")
    older = make_run(session, kind="ingest", datasource_id=ds.id, task_id="t-old")
    ds.snapshot_run_id = older.id
    session.add(ds)
    session.commit()
    newer = make_run(session, kind="ingest", datasource_id=ds.id, task_id="t-new",
                     output_key="datasets/1/new.parquet")

    _finalize_ingest(session, newer, {"bucket": "data-prep", "rows_written": 5, "columns": []})
    session.commit()
    session.refresh(ds)
    assert ds.key == "datasets/1/new.parquet"
    assert ds.snapshot_run_id == newer.id


async def test_finalize_ingest_without_baseline_is_accepted(session, fake_engine):
    """The datasources created before this field have no baseline: accepted (otherwise the
    first refresh after the update would be refused)."""
    ds = make_datasource(session, name="legacy", kind="database", key="datasets/old.parquet")
    assert ds.snapshot_run_id is None
    run = make_run(session, kind="ingest", datasource_id=ds.id, output_key="datasets/2/x.parquet")

    _finalize_ingest(session, run, {"bucket": "data-prep", "rows_written": 2, "columns": []})
    session.commit()
    session.refresh(ds)
    assert ds.key == "datasets/2/x.parquet" and ds.snapshot_run_id == run.id


async def test_publish_overwrite_rejects_older_run(session, fake_engine):
    existing = make_datasource(session, name="vendite", project_id=1, kind="flow",
                               key="datasets/3/fresh.parquet")
    older = make_run(session, kind="flow", status="SUCCESS", task_id="t-o1",
                     publish_overwrite=True, publish_name="vendite", publish_project_id=1,
                     output_key="datasets/3/stale.parquet")
    newer = make_run(session, kind="flow", status="SUCCESS", task_id="t-o2")
    existing.snapshot_run_id = newer.id  # a newer publish has gone through already
    session.add(existing)
    session.commit()

    _publish_datasource(session, older, {"rows_written": 9, "columns": []})
    session.commit()
    session.refresh(existing)
    assert existing.key == "datasets/3/fresh.parquet"  # not overwritten by the older one
    assert "datasets/3/stale.parquet" in _pending_keys(session)


async def test_publish_creation_records_baseline(session, fake_engine):
    run = make_run(session, kind="flow", status="SUCCESS", task_id="t-crea",
                   publish_name="nuova", publish_project_id=1, output_key="datasets/new/x.parquet")
    _publish_datasource(session, run, {"rows_written": 3, "columns": []})
    session.commit()
    ds = session.exec(select(Datasource).where(Datasource.name == "nuova")).first()
    assert ds is not None and ds.snapshot_run_id == run.id


# ── Keys' layout (operations) ───────────────────────────────────────────────
def test_snapshot_key_is_foldered_and_timestamped():
    key = snapshot_key(42)
    assert key.startswith("datasets/42/") and key.endswith(".parquet")
    # the data plane's pinning accepts only the managed prefixes: the subfolder must not take
    # the key out of them
    assert key.startswith(MANAGED_PREFIXES)


def test_snapshot_key_without_datasource_uses_neutral_folder():
    """At the FIRST publish the datasource does not exist yet: no id."""
    assert snapshot_key(None).startswith("datasets/new/")


def test_snapshot_keys_do_not_collide_within_the_same_second():
    keys = {snapshot_key(1) for _ in range(50)}
    assert len(keys) == 50  # the random suffix avoids the collision


# ── C3: orphaned orchestrations closed ──────────────────────────────────────
# Born as a check at the gateway's start (one process). With several processes one's
# start says nothing about the others: an orchestration is orphaned when its HEARTBEAT
# has stopped, and the first orchestrator that notices closes it. The rest is in
# test_orchestration_queue.py.
@pytest.fixture
def orch_db(db_engine, monkeypatch):
    """The orchestrator's functions open a session of their own (they run outside a
    request): it is pointed at the test DB."""
    import app.services.orchestrator as orch_mod

    monkeypatch.setattr(orch_mod, "engine", db_engine)
    return db_engine


async def test_orphaned_orchestrations_are_closed(session, orch_db):
    # STARTED without a heartbeat: it is from a version that did not write it, that is from
    # a process no longer there
    stuck = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=1)
    session.commit()

    assert await close_orphans() == 1

    session.expire_all()
    reloaded = session.get(Run, stuck.id)
    assert reloaded.status == "FAILURE"
    assert reloaded.finished_at is not None
    assert reloaded.error == ORCHESTRATION_INTERRUPTED


async def test_orphan_sweep_leaves_finished_and_engine_backed_runs_alone(session, orch_db):
    done = make_run(session, kind="orchestration", status="SUCCESS", task_id="", flow_id=1)
    # a flow run has a task on the engine: `_reconcile` reconciles it
    flow_run = make_run(session, kind="flow", status="STARTED", task_id="t-live", flow_id=1)
    session.commit()

    await close_orphans()

    session.expire_all()
    assert session.get(Run, done.id).status == "SUCCESS"
    assert session.get(Run, flow_run.id).status == "STARTED"


async def test_orphan_sweep_is_idempotent(session, orch_db):
    make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=1)
    session.commit()

    assert await close_orphans() == 1
    assert await close_orphans() == 0  # a second round, or another process, has nothing to do


# ── several processes reconciling together ──────────────────────────────────
async def test_a_stale_started_answer_does_not_reopen_a_closed_run(session, db_engine, fake_engine):
    """Two processes reconcile the same run. One asks the engine for the state and gets
    STARTED; before it writes it, the other has closed it already. Writing STARTED on the
    object would reopen it (and the next round would redo the closing, with its effects):
    the UPDATE is conditional on the non-terminal state."""
    from sqlmodel import Session

    run = make_run(session, kind="flow", status="PENDING", task_id="t-gara", flow_id=1)
    fake_engine.task_states["t-gara"] = {"status": "STARTED", "result": {}, "error": None}
    with Session(db_engine) as other:  # the other process closes it meanwhile
        r = other.get(Run, run.id)
        r.status = "SUCCESS"
        other.add(r); other.commit()
    # this process still holds the object read before (PENDING)
    assert run.status == "PENDING"
    after = await _reconcile(session, run)
    assert after.status == "SUCCESS"
    session.expire_all()
    assert session.get(Run, run.id).status == "SUCCESS"


async def test_blob_sweep_tolerates_a_row_another_process_already_removed(session, db_engine, fake_engine):
    from datetime import datetime, timedelta, timezone

    from sqlmodel import Session, select

    from app.models import PendingBlobDeletion
    from app.services.blobgc import sweep_blob_deletions

    past = datetime.now(timezone.utc) - timedelta(hours=1)
    for key in ("a", "b"):
        session.add(PendingBlobDeletion(bucket="data-prep", key=f"datasets/{key}.parquet", reason="t", delete_after=past))
    session.commit()

    real = fake_engine._handler

    def another_arrives_first(request):
        # while this process calls the engine for the first row, another finishes its pass and
        # removes all the rows
        with Session(db_engine) as other:
            for row in other.exec(select(PendingBlobDeletion)).all():
                other.delete(row)
            other.commit()
        return real(request)

    import httpx

    import app.core.engine_client as ec

    ec._client = httpx.AsyncClient(transport=httpx.MockTransport(another_arrives_first), base_url="http://engine")
    assert await sweep_blob_deletions(session) == 0  # no error: the other removed them
    assert session.exec(select(PendingBlobDeletion)).all() == []
