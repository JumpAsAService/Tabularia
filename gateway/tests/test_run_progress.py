"""The progress of the running runs towards the browser: the phase the engine reports (or
queued / running when it does not say), the counts, and the seconds counted by the
gateway — from the start of the run, not from the launch. It lives in the request
only: no columns, no writes."""
from datetime import datetime, timedelta, timezone

import pytest

from app.routes.datasources import list_datasource_runs
from app.routes.runs import _reconcile, run_out
from tests.conftest import make_datasource, make_run, make_user

pytestmark = pytest.mark.anyio


def _fa(seconds: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


async def test_queued_counts_from_the_launch(session, fake_engine):
    run = make_run(session, kind="ingest", status="PENDING", task_id="t-coda", started_at=_fa(40))
    fake_engine.set_task("t-coda", "PENDING")
    p = run_out(session, await _reconcile(session, run)).progress
    assert p.phase == "queued" and 39 <= p.seconds <= 42


async def test_running_carries_phase_and_rows_and_counts_from_the_real_start(session, fake_engine):
    # 5 minutes queued, 2 running: the time shown is the run's
    run = make_run(session, kind="ingest", status="STARTED", task_id="t-legge",
                   started_at=_fa(420), engine_started_at=_fa(120))
    fake_engine.task_states["t-legge"] = {"status": "STARTED", "progress": {"phase": "reading", "rows": 1_250_000}}
    p = run_out(session, await _reconcile(session, run)).progress
    assert (p.phase, p.rows) == ("reading", 1_250_000) and 119 <= p.seconds <= 122


async def test_an_engine_not_saying_the_phase_is_a_running_run(session, fake_engine):
    run = make_run(session, kind="ingest", status="STARTED", task_id="t-vecchio", engine_started_at=_fa(5))
    fake_engine.set_task("t-vecchio", "STARTED")  # an engine of before: no `progress`
    p = run_out(session, await _reconcile(session, run)).progress
    assert p.phase == "running" and p.rows is None


async def test_an_odd_progress_does_not_break_the_answer(session, fake_engine):
    run = make_run(session, kind="ingest", status="STARTED", task_id="t-strano", engine_started_at=_fa(5))
    fake_engine.task_states["t-strano"] = {"status": "STARTED", "progress": {"phase": "reading", "rows": "tante"}}
    out = run_out(session, await _reconcile(session, run))
    assert out.status == "STARTED" and out.progress is None


async def test_a_finished_run_has_no_progress(session, fake_engine):
    run = make_run(session, kind="ingest", status="SUCCESS", task_id="t-fatto")
    assert run_out(session, run).progress is None


async def test_the_datasources_history_returns_it(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    ds = make_datasource(session, name="ordini", kind="database")
    make_run(session, kind="ingest", status="STARTED", task_id="t-ds", datasource_id=ds.id, engine_started_at=_fa(3))
    fake_engine.task_states["t-ds"] = {"status": "STARTED", "progress": {"phase": "files", "file": 2, "files": 5, "rows": 900}}
    [r] = await list_datasource_runs(ds.id, user=admin, session=session)
    assert r.progress.model_dump(exclude={"seconds"}) == {"phase": "files", "rows": 900, "file": 2, "files": 5,
                                                          "call": None, "calls": None, "pages": None}


async def test_an_api_import_says_its_calls_and_pages(session, fake_engine):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    ds = make_datasource(session, name="fx", kind="database")
    make_run(session, kind="ingest", status="STARTED", task_id="t-api", datasource_id=ds.id, engine_started_at=_fa(3))
    fake_engine.task_states["t-api"] = {"status": "STARTED",
                                        "progress": {"phase": "calls", "call": 2, "calls": 3, "pages": 7, "rows": 700}}
    [r] = await list_datasource_runs(ds.id, user=admin, session=session)
    assert r.progress.model_dump(exclude={"seconds", "file", "files"}) == {"phase": "calls", "rows": 700, "call": 2, "calls": 3, "pages": 7}
