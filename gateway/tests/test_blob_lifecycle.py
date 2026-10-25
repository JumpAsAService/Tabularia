"""Blobs' lifecycle: deferred deletion (grace) and the truth of the runs.

Regression for:
- F12/F20: a refresh/overwrite/delete must NOT delete at once a blob a run or a
  preview in progress could still read → deferred deletion, run by the sweep when the
  grace expires.
- F17: when the publish fails (names used up), the orphaned parquet must not be lost →
  marked for deletion.
"""
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.models import Datasource, PendingBlobDeletion, Run
from app.routes.runs import _finalize_ingest, _publish_datasource, _reconcile
from app.services.blobgc import (
    blob_deletion_grace_seconds,
    schedule_blob_deletion,
    sweep_blob_deletions,
)
from tests.conftest import make_datasource, make_run

pytestmark = pytest.mark.anyio


def _pending(session) -> list[PendingBlobDeletion]:
    return session.exec(select(PendingBlobDeletion)).all()


# ── blobgc alone ──────────────────────────────────────────────────────────
async def test_schedule_adds_row_with_grace(session):
    schedule_blob_deletion(session, "data-prep", "datasets/x.parquet", reason="test")
    session.commit()
    rows = _pending(session)
    assert len(rows) == 1
    assert rows[0].key == "datasets/x.parquet"
    delta = rows[0].delete_after.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
    assert 0 < delta.total_seconds() <= blob_deletion_grace_seconds() + 5


async def test_schedule_empty_key_is_noop(session):
    schedule_blob_deletion(session, "data-prep", "", reason="test")
    session.commit()
    assert _pending(session) == []


async def test_sweep_deletes_only_due_rows(session, fake_engine):
    past = datetime.now(timezone.utc) - timedelta(seconds=10)
    future = datetime.now(timezone.utc) + timedelta(seconds=3600)
    session.add(PendingBlobDeletion(bucket="data-prep", key="datasets/due.parquet", delete_after=past))
    session.add(PendingBlobDeletion(bucket="data-prep", key="datasets/later.parquet", delete_after=future))
    session.commit()

    removed = await sweep_blob_deletions(session)
    assert removed == 1
    assert ("data-prep", "datasets/due.parquet") in fake_engine.deleted
    # the expired row is gone, the future one stays
    remaining = [r.key for r in _pending(session)]
    assert remaining == ["datasets/later.parquet"]


async def test_sweep_retries_when_engine_fails(session, fake_engine):
    fake_engine.delete_status = 500  # the engine cannot delete
    past = datetime.now(timezone.utc) - timedelta(seconds=10)
    session.add(PendingBlobDeletion(bucket="data-prep", key="datasets/x.parquet", delete_after=past))
    session.commit()

    removed = await sweep_blob_deletions(session)
    assert removed == 0
    assert _pending(session)  # the row stays → retried at the next tick


async def test_sweep_treats_404_as_done(session, fake_engine):
    fake_engine.delete_status = 404  # gone already
    past = datetime.now(timezone.utc) - timedelta(seconds=10)
    session.add(PendingBlobDeletion(bucket="data-prep", key="datasets/gone.parquet", delete_after=past))
    session.commit()

    removed = await sweep_blob_deletions(session)
    assert removed == 1
    assert _pending(session) == []


# ── _finalize_ingest: swap with DEFERRED deletion of the old snapshot ──
async def test_finalize_ingest_schedules_old_snapshot_not_immediate(session, fake_engine):
    ds = make_datasource(session, name="ordini", kind="database", bucket="data-prep", key="datasets/old.parquet")
    run = make_run(session, kind="ingest", datasource_id=ds.id, output_key="datasets/new.parquet")
    result = {"bucket": "data-prep", "rows_written": 42, "columns": []}

    _finalize_ingest(session, run, result)
    session.commit()

    session.refresh(ds)
    assert ds.key == "datasets/new.parquet"  # swapped to the new snapshot
    assert ds.rows == 42
    assert fake_engine.deleted == []  # the old blob was NOT deleted at once
    pend = _pending(session)
    assert len(pend) == 1 and pend[0].key == "datasets/old.parquet"  # deferred


async def test_finalize_ingest_datasource_deleted_defers_new_orphan(session, fake_engine):
    # datasource gone during the refresh: the new blob is orphaned WITHOUT readers →
    # deferred deletion anyway (uniform, it stays clean on a retry too)
    run = make_run(session, kind="ingest", datasource_id=999, output_key="datasets/new.parquet")
    _finalize_ingest(session, run, {"rows_written": 1})
    session.commit()
    assert fake_engine.deleted == []
    assert [r.key for r in _pending(session)] == ["datasets/new.parquet"]


# ── _publish_datasource ─────────────────────────────────────────────────────
async def test_publish_creates_new_datasource(session, fake_engine):
    run = make_run(
        session, kind="flow", status="SUCCESS",
        publish_name="vendite", publish_project_id=1, output_key="datasets/pub.parquet",
    )
    _publish_datasource(session, run, {"rows_written": 5, "columns": [{"name": "a", "dtype": "i64"}]})
    session.refresh(run)
    ds = session.exec(select(Datasource).where(Datasource.name == "vendite")).first()
    assert ds is not None and run.datasource_id == ds.id
    assert ds.key == "datasets/pub.parquet" and ds.rows == 5
    assert _pending(session) == []  # nothing to delete at creation


async def test_publish_overwrite_replaces_in_place_and_defers_old_blob(session, fake_engine):
    existing = make_datasource(
        session, name="vendite", project_id=1, kind="flow", bucket="data-prep", key="datasets/v1.parquet"
    )
    run = make_run(
        session, kind="flow", status="SUCCESS", publish_overwrite=True,
        publish_name="vendite", publish_project_id=1, output_key="datasets/v2.parquet",
    )
    _publish_datasource(session, run, {"rows_written": 9, "columns": []})
    session.refresh(existing)
    session.refresh(run)
    assert run.datasource_id == existing.id  # the SAME id (the flows using it do not break)
    assert existing.key == "datasets/v2.parquet" and existing.rows == 9
    assert fake_engine.deleted == []  # the v1 blob NOT deleted at once
    pend = _pending(session)
    assert len(pend) == 1 and pend[0].key == "datasets/v1.parquet"  # deferred


async def test_publish_overwrite_refused_for_database_kind_falls_to_create(session, fake_engine):
    # a database snapshot cannot be overwritten: it falls back to creation (name taken →
    # suffix)
    make_datasource(session, name="ordini", project_id=1, kind="database", key="datasets/db.parquet")
    run = make_run(
        session, kind="flow", status="SUCCESS", task_id="abcd1234ef", publish_overwrite=True,
        publish_name="ordini", publish_project_id=1, output_key="datasets/o.parquet",
    )
    _publish_datasource(session, run, {"columns": []})
    session.refresh(run)
    created = session.get(Datasource, run.datasource_id)
    assert created is not None and created.kind == "flow"
    assert created.name != "ordini"  # it took the name with the suffix, not the DB snapshot


async def test_publish_exhausted_schedules_orphan_blob(session, fake_engine):
    # F17: name taken AND the name-with-suffix taken too → publish used up → the output's
    # parquet is orphaned and must be marked for deletion
    make_datasource(session, name="dup", project_id=1, kind="flow", key="datasets/a.parquet")
    make_datasource(session, name="dup (deadbeef)", project_id=1, kind="flow", key="datasets/b.parquet")
    run = make_run(
        session, kind="flow", status="SUCCESS", task_id="deadbeef00",
        publish_name="dup", publish_project_id=1, output_key="datasets/orphan.parquet",
    )
    _publish_datasource(session, run, {"columns": []})
    session.refresh(run)
    assert run.datasource_id is None  # no datasource published
    pend = _pending(session)
    assert len(pend) == 1 and pend[0].key == "datasets/orphan.parquet"  # orphan marked


# ── _reconcile end to end (state machine) ───────────────────────────────────
async def test_reconcile_publishes_on_success_and_is_idempotent(session, fake_engine):
    run = make_run(
        session, kind="flow", status="STARTED", task_id="task-pub",
        publish_name="report", publish_project_id=1, output_key="datasets/r.parquet",
    )
    fake_engine.set_task("task-pub", "SUCCESS", result={"rows_written": 7, "columns": []})

    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "SUCCESS"
    ds_count = len(session.exec(select(Datasource).where(Datasource.name == "report")).all())
    assert ds_count == 1 and run.datasource_id is not None

    # a second reconciliation must not publish again (run terminal already)
    await _reconcile(session, run)
    ds_count2 = len(session.exec(select(Datasource).where(Datasource.name == "report")).all())
    assert ds_count2 == 1


async def test_reconcile_ingest_success_swaps_and_defers(session, fake_engine):
    ds = make_datasource(session, name="live", kind="database", key="datasets/s1.parquet")
    run = make_run(session, kind="ingest", status="STARTED", task_id="task-ing",
                   datasource_id=ds.id, output_key="datasets/s2.parquet")
    fake_engine.set_task("task-ing", "SUCCESS", result={"bucket": "data-prep", "rows_written": 3, "columns": []})

    await _reconcile(session, run)
    session.refresh(ds)
    assert ds.key == "datasets/s2.parquet"
    assert fake_engine.deleted == []  # old snapshot deferred, not deleted at once
    assert [r.key for r in _pending(session)] == ["datasets/s1.parquet"]


# ── F13: an ATOMIC and RETRYABLE post-claim effect ──────────────────────────
def _flaky(real, fail_times: int):
    """A wrapper that fails the first `fail_times` calls, then delegates to the real one."""
    state = {"n": 0}

    def wrapper(*a, **k):
        state["n"] += 1
        if state["n"] <= fail_times:
            raise RuntimeError("boom")
        return real(*a, **k)

    return wrapper


async def test_reconcile_publish_recovers_in_call_on_transient_failure(session, fake_engine, monkeypatch):
    import app.routes.runs as runs_mod

    run = make_run(
        session, kind="flow", status="STARTED", task_id="t-r1",
        publish_name="rep1", publish_project_id=1, output_key="datasets/r.parquet",
    )
    fake_engine.set_task("t-r1", "SUCCESS", result={"rows_written": 3, "columns": []})
    # fails ONCE: the IMMEDIATE retry in the same _reconcile recovers it
    monkeypatch.setattr(runs_mod, "_publish_datasource", _flaky(runs_mod._publish_datasource, 1))

    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "SUCCESS"
    assert session.exec(select(Datasource).where(Datasource.name == "rep1")).first() is not None


async def test_reconcile_publish_lazy_retry_when_attempts_exhausted(session, fake_engine, monkeypatch):
    import app.routes.runs as runs_mod

    run = make_run(
        session, kind="flow", status="STARTED", task_id="t-r2",
        publish_name="rep2", publish_project_id=1, output_key="datasets/r.parquet",
    )
    fake_engine.set_task("t-r2", "SUCCESS", result={"rows_written": 3, "columns": []})
    # fails ALL the immediate attempts → the round runs out and leaves it non-terminal
    monkeypatch.setattr(
        runs_mod, "_publish_datasource", _flaky(runs_mod._publish_datasource, runs_mod.SIDE_EFFECT_ATTEMPTS)
    )

    await _reconcile(session, run)
    session.refresh(run)
    assert run.status != "SUCCESS"
    assert session.exec(select(Datasource).where(Datasource.name == "rep2")).first() is None

    # stops failing → the next read reconciles (lazy fallback)
    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "SUCCESS"
    assert session.exec(select(Datasource).where(Datasource.name == "rep2")).first() is not None


async def test_reconcile_ingest_recovers_in_call_on_transient_failure(session, fake_engine, monkeypatch):
    import app.routes.runs as runs_mod

    ds = make_datasource(session, name="live3", kind="database", key="datasets/s1.parquet")
    run = make_run(session, kind="ingest", status="STARTED", task_id="t-r3",
                   datasource_id=ds.id, output_key="datasets/s2.parquet")
    fake_engine.set_task("t-r3", "SUCCESS", result={"rows_written": 1, "columns": []})
    monkeypatch.setattr(runs_mod, "_finalize_ingest", _flaky(runs_mod._finalize_ingest, 1))

    await _reconcile(session, run)  # one round only: the immediate retry recovers the swap
    session.refresh(ds)
    session.refresh(run)
    assert ds.key == "datasets/s2.parquet"
    assert run.status == "SUCCESS"


async def test_reconcile_failure_records_error_and_no_side_effect(session, fake_engine):
    run = make_run(
        session, kind="flow", status="STARTED", task_id="t-fail",
        publish_name="nope", publish_project_id=1, output_key="datasets/x.parquet",
    )
    fake_engine.set_task("t-fail", "FAILURE", error="task esploso")

    await _reconcile(session, run)
    session.refresh(run)
    assert run.status == "FAILURE"
    assert "esploso" in (run.error or "")
    assert session.exec(select(Datasource).where(Datasource.name == "nope")).first() is None


async def test_reconcile_ingest_failure_does_not_swap(session, fake_engine):
    ds = make_datasource(session, name="keep", kind="database", key="datasets/orig.parquet")
    run = make_run(session, kind="ingest", status="STARTED", task_id="t-if",
                   datasource_id=ds.id, output_key="datasets/nope.parquet")
    fake_engine.set_task("t-if", "FAILURE", error="ingest ko")

    await _reconcile(session, run)
    session.refresh(ds)
    assert ds.key == "datasets/orig.parquet"  # snapshot invariato
    assert _pending(session) == []
