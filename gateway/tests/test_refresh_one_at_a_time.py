"""One refresh per datasource at a time (review 2026-10-14, correctness-core-6).

The manual refresh, the scheduler and a flow's Refresh node all looked at the last
import and then launched one: two of them at once (two gateway replicas, a click
during a scheduled slot) both saw nothing running and both launched. The later one
to finish could then put back older data, or, in append mode, append the same rows
twice. And two reconciles closing two imports of one datasource could let the older
snapshot win.

Now whoever starts a refresh queues on a Postgres advisory lock for that datasource
until its import is recorded; the second one finds it and gets a 409 (the scheduler
skips its slot, a flow waits for the import already running). The swap of the
snapshot reads the datasource FOR UPDATE, so the newer snapshot always wins.
"""
import asyncio
import json
import threading
import time

import httpx
import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.db import migra as m
from app.models import Connection, Datasource, PendingBlobDeletion, Run
from app.routes import datasources as ds_routes
from app.routes import runs as run_routes
from app.services import orchestrator as orch
from app.services import scheduler
from tests.conftest import make_datasource, make_project, make_user
from tests.test_migrazioni import database_temporaneo  # noqa: F401 — the fixture


class SlowEngine:
    """Answers an import after `delay` seconds, as a busy engine does."""

    def __init__(self, delay: float = 0.0):
        self.delay = delay
        self.ingests = 0
        self.lock = threading.Lock()

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/ingest"):
            await asyncio.sleep(self.delay)
            with self.lock:
                self.ingests += 1
                n = self.ingests
            return httpx.Response(200, json={"task_id": f"t-ingest-{n}"})
        if request.url.path.startswith("/tasks/"):
            return httpx.Response(200, json={"task_id": request.url.path.rsplit("/", 1)[-1], "status": "STARTED"})
        return httpx.Response(404, json={"detail": "not mocked"})


def _engine(monkeypatch, delay=0.0) -> SlowEngine:
    e = SlowEngine(delay)
    # a fresh client per call: each thread below has its own event loop
    monkeypatch.setattr("app.routes.runs.get_engine_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(e.handler), base_url="http://engine"))
    return e


def _world(session):
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    p = make_project(session, name="p")
    conn = Connection(name="erp", project_id=p.id, db_type="postgres", host="h", username="u")
    session.add(conn)
    session.commit()
    ds = make_datasource(session, name="orders", project_id=p.id, kind="database", connection_id=conn.id,
                         source_type="table", source_ref="public.orders")
    return admin.id, ds.id


def _refresh(eng, user_id, ds_id, results, name):
    with Session(eng) as s:
        from app.models import User

        user = s.get(User, user_id)
        try:
            run = asyncio.run(ds_routes.refresh_datasource(ds_id, request=None, user=user, session=s))
            results[name] = ("launched", run.id)
        except HTTPException as e:
            results[name] = (e.status_code, e.detail)


# ── two refreshes at once, on a real Postgres (throwaway database) ─────────────
def test_two_manual_refreshes_at_once_launch_one(database_temporaneo, monkeypatch):  # noqa: F811
    m.migra(database_temporaneo)
    with Session(database_temporaneo) as s:
        user_id, ds_id = _world(s)
    engine = _engine(monkeypatch, delay=0.6)
    results: dict = {}
    first = threading.Thread(target=_refresh, args=(database_temporaneo, user_id, ds_id, results, "first"))
    second = threading.Thread(target=_refresh, args=(database_temporaneo, user_id, ds_id, results, "second"))
    first.start()
    time.sleep(0.2)  # the first is inside the engine call: nothing recorded yet
    second.start()
    first.join(15)
    second.join(15)
    assert results["first"][0] == "launched"
    assert results["second"] == (409, "A refresh is already running for this datasource")
    assert engine.ingests == 1
    with Session(database_temporaneo) as s:
        assert len(s.exec(select(Run).where(Run.datasource_id == ds_id, Run.kind == "ingest")).all()) == 1


def test_the_newer_snapshot_wins_two_overlapping_closes(database_temporaneo):  # noqa: F811
    """The swap of run 10 must not overwrite the one of run 11 that committed while
    run 10 was looking (it read the datasource before 11's commit)."""
    m.migra(database_temporaneo)
    with Session(database_temporaneo) as s:
        _, ds_id = _world(s)
        for i in (10, 11):
            s.add(Run(id=i, kind="ingest", datasource_id=ds_id, task_id=f"t{i}", status="STARTED",
                      input_key="", output_bucket="data-prep", output_key=f"datasets/{ds_id}/k{i}.parquet"))
        ds = s.get(Datasource, ds_id)
        ds.key, ds.snapshot_run_id = f"datasets/{ds_id}/k9.parquet", 9
        s.add(ds)
        s.commit()

    def close(run_id, hold):
        with Session(database_temporaneo) as s:
            run = s.get(Run, run_id)
            run_routes._finalize_ingest(s, run, {"rows_written": 1, "columns": []})
            time.sleep(hold)
            s.commit()

    newer = threading.Thread(target=close, args=(11, 0.8))
    older = threading.Thread(target=close, args=(10, 0))
    newer.start()
    time.sleep(0.2)
    older.start()
    newer.join(10)
    older.join(10)
    with Session(database_temporaneo) as s:
        ds = s.get(Datasource, ds_id)
        assert (ds.key, ds.snapshot_run_id) == (f"datasets/{ds_id}/k11.parquet", 11)
        pending = sorted(p.key for p in s.exec(select(PendingBlobDeletion)).all())
        # the superseded k9 once, and the stale k10: nothing orphaned, nothing twice
        assert pending == [f"datasets/{ds_id}/k10.parquet", f"datasets/{ds_id}/k9.parquet"]


# ── the other starters ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_the_scheduler_skips_its_slot_while_a_refresh_runs(session, db_engine, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from app.models import User

    user_id, ds_id = _world(session)
    engine = _engine(monkeypatch)
    ds = session.get(Datasource, ds_id)
    ds.refresh_schedule, ds.refresh_scheduled_by = "0 3 * * *", user_id
    ds.next_refresh_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
    session.add(ds)
    session.add(Run(kind="ingest", datasource_id=ds_id, task_id="t-live", status="STARTED", input_key="",
                    output_bucket="data-prep", output_key="datasets/x.parquet"))
    session.commit()
    await scheduler._fire_ds(session, ds, datetime.now(timezone.utc))
    assert engine.ingests == 0
    assert session.get(Datasource, ds_id).next_refresh_at > datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.mark.anyio
async def test_a_flow_waits_for_the_refresh_already_running(session, monkeypatch):
    from app.models import User

    user_id, ds_id = _world(session)
    engine = _engine(monkeypatch)
    live = Run(kind="ingest", datasource_id=ds_id, task_id="t-live", status="STARTED", input_key="",
               output_bucket="data-prep", output_key="datasets/x.parquet")
    session.add(live)
    session.commit()
    waited = []

    async def reconcile(s, run):
        waited.append(run.id)
        if waited.count(run.id) >= 3:  # still running when looked at, done in the end
            run.status = "SUCCESS"
        return run

    monkeypatch.setattr(orch, "_reconcile", reconcile)
    monkeypatch.setattr(orch._orc(), "poll_interval_seconds", 0.01)
    await orch._refresh_and_wait(session, session.get(User, user_id), ds_id)
    assert engine.ingests == 0 and set(waited) == {live.id}


@pytest.mark.anyio
async def test_a_second_manual_refresh_gets_409(session, monkeypatch):
    from app.models import User

    user_id, ds_id = _world(session)
    _engine(monkeypatch)
    user = session.get(User, user_id)
    await ds_routes.refresh_datasource(ds_id, request=None, user=user, session=session)
    with pytest.raises(HTTPException) as e:
        await ds_routes.refresh_datasource(ds_id, request=None, user=user, session=session)
    assert e.value.status_code == 409
