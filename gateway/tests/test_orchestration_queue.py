"""The flows' runs go through a queue on the database.

While the gateway was one process, a run was an asyncio task inside that process:
the list of the running flows sat in an in-memory `set`, and the orchestrations left
halfway were closed at start. With several processes neither holds — another process
does not see that `set`, and one's start would close the others' live runs.

Now whoever launches (run-now, the scheduler) writes a PENDING row; whoever runs takes
it with a conditional UPDATE, signs it and keeps its heartbeat alive. These tests pin
the guarantees the `set` gave for free:

  - a queued row is taken by ONE process;
  - ONE run of a flow goes at a time (the database says so);
  - a scheduler slot launches ONCE, even when two see it;
  - a run whose process died is closed, the others are not.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

import app.services.orchestrator as orch
from app.models import Flow, Run
from app.models.run import ORCHESTRATION_INTERRUPTED, STEP_INTERRUPTED
from app.routes.flows import run_flow_now
from app.services import scheduler
from tests.conftest import make_flow, make_project, make_run, make_user

pytestmark = pytest.mark.anyio


@pytest.fixture
def queue_(db_engine, session, monkeypatch):
    """Orchestrator and scheduler pointed at the test DB, with a user and a folder."""
    monkeypatch.setattr(orch, "engine", db_engine)
    monkeypatch.setattr(orch, "heartbeat_engine", db_engine)
    monkeypatch.setattr(orch, "_alive", set())
    monkeypatch.setattr(scheduler, "engine", db_engine)
    user_ = make_user(session, email="chi-lancia@x.local", is_superuser=True)
    folder = make_project(session, name="cartella", owner_id=user_.id)
    return user_, folder


def _flow(session, user_, folder, name="f", **kw):
    return make_flow(session, name=name, project_id=folder.id, owner_id=user_.id, definition="{}", **kw)


def _state(session, run_id):
    session.expire_all()
    return session.get(Run, run_id)


def _as(monkeypatch, sign):
    """From here on the orchestrator's functions act as the process `sign`."""
    monkeypatch.setattr(orch, "INSTANCE", sign)


# ── queueing ─────────────────────────────────────────────────────────────────
def test_a_launch_is_a_queued_row(session, queue_):
    user_, folder = queue_
    flow = _flow(session, user_, folder)
    run = orch.create_orchestration_run(session, user_, flow, "manual", "production")
    assert (run.kind, run.status, run.engine_mode, run.trigger_type) == ("orchestration", "PENDING", "production", "manual")
    assert run.claimed_by is None and run.heartbeat_at is None and run.launched_by == user_.id


async def test_run_now_refuses_a_flow_queued_or_running_already(session, queue_):
    """Every extra «Run now» cost a row in `runs` and a round of the queue, and repeated it
    pushed the other flows' runs to the back: now it is a 409, in the asker's language,
    and writes nothing."""
    user_, folder = queue_
    flow = _flow(session, user_, folder)

    async def run_now(request=None):
        return run_flow_now(flow.id, request=request, mode="development", user=user_, session=session)

    first = await run_now()
    with pytest.raises(HTTPException) as e:                       # in coda
        await run_now()
    assert e.value.status_code == 409 and "already queued or running" in e.value.detail
    orch.claim(5)
    session.expire_all()
    italian = SimpleNamespace(headers={"accept-language": "it-IT,it;q=0.9,en;q=0.8"})
    with pytest.raises(HTTPException) as e:                       # running
        await run_now(italian)
    assert e.value.status_code == 409 and "già in coda o in esecuzione" in e.value.detail
    assert len(session.exec(select(Run).where(Run.flow_id == flow.id)).all()) == 1
    await orch.run_it(first["run_id"])                            # finished: it launches again
    session.expire_all()
    second = await run_now()
    assert second["run_id"] != first["run_id"] and _state(session, second["run_id"]).status == "PENDING"


# ── taking from the queue ────────────────────────────────────────────────────
def test_whoever_takes_signs_and_beats(session, queue_, monkeypatch):
    user_, folder = queue_
    run = orch.create_orchestration_run(session, user_, _flow(session, user_, folder))
    _as(monkeypatch, "processo-a")
    assert orch.claim(5) == ([run.id], [])
    taken_one = _state(session, run.id)
    assert (taken_one.status, taken_one.claimed_by) == ("STARTED", "processo-a")
    assert taken_one.heartbeat_at is not None


def test_a_row_is_taken_by_one_process_only(session, queue_, monkeypatch):
    user_, folder = queue_
    run = orch.create_orchestration_run(session, user_, _flow(session, user_, folder))
    _as(monkeypatch, "processo-a")
    assert orch.claim(5) == ([run.id], [])
    _as(monkeypatch, "processo-b")
    assert orch.claim(5) == ([], [])
    assert _state(session, run.id).claimed_by == "processo-a"


def test_two_processes_seeing_the_same_row_the_first_takes_it(session, queue_, db_engine, monkeypatch):
    """The real case: both read the row as PENDING before one of the two took it. The
    conditional UPDATE decides, not the read."""
    from sqlalchemy import update

    user_, folder = queue_
    run = orch.create_orchestration_run(session, user_, _flow(session, user_, folder))

    def take(sign):
        with Session(db_engine) as s:
            r = s.exec(update(Run).where(Run.id == run.id, Run.status == "PENDING").values(status="STARTED", claimed_by=sign))
            s.commit()
            return r.rowcount

    assert [take("a"), take("b")] == [1, 0]


def test_takes_up_to_the_limit_and_in_order(session, queue_, monkeypatch):
    user_, folder = queue_
    runs = [orch.create_orchestration_run(session, user_, _flow(session, user_, folder, f"f{i}")).id for i in range(4)]
    taken, _ = orch.claim(2)
    assert taken == runs[:2]
    taken, _ = orch.claim(10)
    assert taken == runs[2:]


# ── one run per flow ─────────────────────────────────────────────────────────
def test_the_database_refuses_two_live_runs_of_the_same_flow(session, queue_):
    user_, folder = queue_
    flow = _flow(session, user_, folder)
    make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id)
    with pytest.raises(IntegrityError):
        make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id)
    session.rollback()
    # what must NOT refuse: another flow, finished runs, runs of another kind
    other = _flow(session, user_, folder, "altro")
    make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=other.id)
    make_run(session, kind="orchestration", status="SUCCESS", task_id="", flow_id=flow.id)
    make_run(session, kind="orchestration", status="PENDING", task_id="", flow_id=flow.id)
    make_run(session, kind="flow", status="STARTED", task_id="t-1", flow_id=flow.id)
    make_run(session, kind="flow", status="STARTED", task_id="t-2", flow_id=flow.id)


def test_a_manual_launch_of_a_running_flow_is_refused(session, queue_, monkeypatch):
    user_, folder = queue_
    flow = _flow(session, user_, folder)
    first = orch.create_orchestration_run(session, user_, flow)
    assert orch.claim(5) == ([first.id], [])
    second = orch.create_orchestration_run(session, user_, flow)
    _as(monkeypatch, "un-altro-processo")
    assert orch.claim(5) == ([], [second.id])
    assert _state(session, second.id).status == "PENDING"  # the caller closes it, with the notice


async def test_the_refused_one_is_closed_as_already_running(session, queue_):
    user_, folder = queue_
    flow = _flow(session, user_, folder)
    first = orch.create_orchestration_run(session, user_, flow)
    orch.claim(5)
    second = orch.create_orchestration_run(session, user_, flow)
    _, refused = orch.claim(5)
    for run_id in refused:
        await orch._finalize_orch_run(run_id, "FAILURE", orch.ALREADY_RUNNING)
    closed_one = _state(session, second.id)
    assert (closed_one.status, closed_one.error) == ("FAILURE", "flow already running")
    assert _state(session, first.id).status == "STARTED"  # the real one is not touched


def test_a_scheduled_slot_on_a_running_flow_vanishes_without_trace(session, queue_):
    user_, folder = queue_
    flow = _flow(session, user_, folder)
    orch.create_orchestration_run(session, user_, flow)
    orch.claim(5)
    slot = orch.create_orchestration_run(session, user_, flow, "schedule", "production")
    assert orch.claim(5) == ([], [])
    assert _state(session, slot.id) is None


# ── heartbeat and orphans ────────────────────────────────────────────────────
def test_the_heartbeat_touches_only_this_processs_runs(session, queue_, monkeypatch):
    user_, folder = queue_
    old = datetime(2020, 1, 1)
    mia = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flow(session, user_, folder, "a").id, claimed_by="io", heartbeat_at=old)
    others_ = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flow(session, user_, folder, "b").id, claimed_by="un-altro", heartbeat_at=old)
    finished = make_run(session, kind="orchestration", status="SUCCESS", task_id="", flow_id=_flow(session, user_, folder, "c").id, claimed_by="io", heartbeat_at=old)
    _as(monkeypatch, "io")
    monkeypatch.setattr(orch, "_alive", {mia.id, others_.id, finished.id})
    assert orch.beat() == 1
    assert _state(session, mia.id).heartbeat_at > old
    assert _state(session, others_.id).heartbeat_at == old
    assert _state(session, finished.id).heartbeat_at == old


async def test_whoever_beats_is_not_orphaned_whoever_stopped_is(session, queue_):
    user_, folder = queue_
    now = orch._now()
    viva = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flow(session, user_, folder, "a").id, claimed_by="vivo", heartbeat_at=now - timedelta(seconds=5))
    dead = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flow(session, user_, folder, "b").id, claimed_by="morto", heartbeat_at=now - timedelta(seconds=orch._orc().dead_after_seconds + 30))
    queued = make_run(session, kind="orchestration", status="PENDING", task_id="", flow_id=_flow(session, user_, folder, "c").id)

    assert await orch.close_orphans() == 1

    assert _state(session, viva.id).status == "STARTED"
    assert _state(session, queued.id).status == "PENDING"  # queued is not «dead»: it waits for someone to take it
    closed = _state(session, dead.id)
    assert (closed.status, closed.error) == ("FAILURE", ORCHESTRATION_INTERRUPTED)
    assert closed.finished_at is not None


async def test_with_the_orphan_closed_the_flow_can_start_again(session, queue_):
    user_, folder = queue_
    flow = _flow(session, user_, folder)
    make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id, claimed_by="morto", heartbeat_at=datetime(2020, 1, 1))
    new = orch.create_orchestration_run(session, user_, flow)
    assert orch.claim(5) == ([], [new.id])  # while the orphan is there, the flow shows busy
    await orch.close_orphans()
    assert orch.claim(5) == ([new.id], [])


# ── the steps of an interrupted orchestration ─────────────────────────────────
# An orchestrator that stops (rolling update, restart from the panel, eviction, OOM)
# closed the flow as interrupted but let the Output already handed to a worker write:
# the flow, closed, could start again on top of it (duplicate rows in append).
async def test_the_shutdown_stops_the_output_started_already_too(session, queue_, fake_engine, monkeypatch):
    user_, folder = queue_
    monkeypatch.setattr(orch._orc(), "poll_interval_seconds", 0.05)
    flow = _flow(session, user_, folder)
    run = orch.create_orchestration_run(session, user_, flow)
    assert orch.claim(5)[0] == [run.id]
    started_, child = asyncio.Event(), {}

    async def orchestrate(s, u, f, **kw):
        out = make_run(s, kind="flow", status="STARTED", task_id="t-output", flow_id=f.id,
                       parent_run_id=kw["parent_run_id"])
        child["id"] = out.id
        fake_engine.set_task("t-output", "STARTED")
        started_.set()
        await orch._wait_run(s, out)
        return []

    monkeypatch.setattr(orch, "orchestrate", orchestrate)
    task = asyncio.create_task(orch.run_it(run.id))
    await started_.wait()
    task.cancel()  # the lifespan's shutdown at SIGTERM
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (_state(session, run.id).status, _state(session, run.id).error) == ("FAILURE", ORCHESTRATION_INTERRUPTED)
    out = _state(session, child["id"])
    assert (out.status, out.error) == ("FAILURE", STEP_INTERRUPTED) and fake_engine.revoked == ["t-output"]


async def test_whoever_closes_the_orphan_stops_its_steps(session, queue_, fake_engine):
    user_, folder = queue_
    flow = _flow(session, user_, folder)
    dead = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id,
                     claimed_by="morto", heartbeat_at=datetime(2020, 1, 1))
    out = make_run(session, kind="flow", status="STARTED", task_id="t-out", flow_id=flow.id, parent_run_id=dead.id)
    queued = make_run(session, kind="flow", status="PENDING", task_id="t-dopo", flow_id=flow.id,
                       parent_run_id=dead.id)
    finished = make_run(session, kind="flow", status="SUCCESS", task_id="t-prima", flow_id=flow.id,
                      parent_run_id=dead.id)
    fake_engine.set_task("t-out", "STARTED")  # still running on the engine
    fake_engine.set_task("t-dopo", "PENDING")
    assert await orch.close_orphans() == 1
    assert sorted(fake_engine.revoked) == ["t-dopo", "t-out"]
    assert _state(session, out.id).status == _state(session, queued.id).status == "FAILURE"
    assert _state(session, finished.id).status == "SUCCESS"  # what has finished stays as it is


async def test_a_step_the_engine_finished_meanwhile_keeps_its_outcome(session, queue_, fake_engine):
    # the gateway pod killed while a flow waited on a refresh: the worker ended it with
    # success before the orphans' sweep. Its new parquet is swapped in, not failed as stopped
    from app.models import Datasource
    from tests.conftest import make_datasource

    user_, folder = queue_
    flow = _flow(session, user_, folder)
    ds = make_datasource(session, name="ordini", kind="database", key="datasets/1/v0.parquet", project_id=folder.id)
    dead = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id,
                     claimed_by="morto", heartbeat_at=datetime(2020, 1, 1))
    imp = make_run(session, kind="ingest", status="STARTED", task_id="t-imp", datasource_id=ds.id,
                   output_key="datasets/1/new.parquet", parent_run_id=dead.id)
    fake_engine.set_task("t-imp", "SUCCESS", result={"bucket": "data-prep", "rows_written": 5, "columns": []})
    assert await orch.close_orphans() == 1
    assert _state(session, imp.id).status == "SUCCESS" and fake_engine.revoked == []
    session.expire_all()
    assert session.get(Datasource, ds.id).key == "datasets/1/new.parquet"


async def test_what_this_process_runs_is_not_orphaned_even_with_an_old_heartbeat(session, queue_, fake_engine,
                                                                                      monkeypatch):
    # Postgres down past dead_after (a failover): when it comes back every heartbeat is old,
    # the one of whoever is still running too; the orphans' round does not close (nor stop)
    # its own
    user_, folder = queue_
    flow = _flow(session, user_, folder)
    mia = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id,
                   claimed_by=orch.INSTANCE, heartbeat_at=datetime(2020, 1, 1))
    out = make_run(session, kind="flow", status="STARTED", task_id="t-out", flow_id=flow.id, parent_run_id=mia.id)
    monkeypatch.setattr(orch, "_alive", {mia.id})
    assert await orch.close_orphans() == 0
    assert _state(session, mia.id).status == _state(session, out.id).status == "STARTED" and fake_engine.revoked == []


def test_the_orphans_round_waits_for_the_process_to_beat_for_a_whole_interval(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(orch.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(orch, "_heartbeat_ok_since", None)
    assert not orch.heartbeat_healthy()  # the database does not answer (or not yet)
    monkeypatch.setattr(orch, "_heartbeat_ok_since", 1000.0)
    clock[0] += orch._orc().dead_after_seconds - 1
    assert not orch.heartbeat_healthy()  # back a moment ago: the others have not beaten yet
    clock[0] += 1
    assert orch.heartbeat_healthy()


async def test_at_restart_the_earlier_runs_stop_their_steps(session, queue_, fake_engine, monkeypatch):
    user_, folder = queue_
    flow = _flow(session, user_, folder)
    guest = orch.INSTANCE.rsplit(":", 1)[0]
    before = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id,
                     claimed_by=f"{guest}:vecchia-firma", heartbeat_at=orch._now())
    out = make_run(session, kind="flow", status="STARTED", task_id="t-out", flow_id=flow.id, parent_run_id=before.id)
    fake_engine.set_task("t-out", "STARTED")
    stop = asyncio.Event()
    stop.set()  # the loop only does the start and stops
    monkeypatch.setattr(orch, "start_heartbeat", lambda ev: None)
    await orch.queue_loop(stop)
    assert _state(session, before.id).error == ORCHESTRATION_INTERRUPTED
    assert _state(session, out.id).status == "FAILURE" and fake_engine.revoked == ["t-out"]


# ── running ──────────────────────────────────────────────────────────────────
async def test_running_brings_the_run_to_the_end(session, queue_):
    user_, folder = queue_
    run = orch.create_orchestration_run(session, user_, _flow(session, user_, folder))
    orch.claim(5)
    await orch.run_it(run.id)
    done = _state(session, run.id)
    assert (done.status, done.error) == ("SUCCESS", None) and done.finished_at is not None


async def test_running_passes_the_mode_and_origin_written_on_the_row(session, queue_, monkeypatch):
    user_, folder = queue_
    run = orch.create_orchestration_run(session, user_, _flow(session, user_, folder), "schedule", "production")
    orch.claim(5)
    seen = {}

    async def fake_one(session, user, flow, **kw):
        seen.update(kw, user=user.id, flow=flow.id)
        return []

    monkeypatch.setattr(orch, "orchestrate", fake_one)
    await orch.run_it(run.id)
    assert seen["trigger_type"] == "schedule" and seen["engine_mode"] == "production"
    assert seen["parent_run_id"] == run.id and seen["guarded"] is True and seen["user"] == user_.id


async def test_a_disabled_user_runs_nothing(session, queue_):
    user_, folder = queue_
    run = orch.create_orchestration_run(session, user_, _flow(session, user_, folder))
    user_.is_active = False
    session.add(user_); session.commit()
    orch.claim(5)
    await orch.run_it(run.id)
    closed_one = _state(session, run.id)
    assert (closed_one.status, closed_one.error) == ("FAILURE", "invalid flow or user")


async def test_whoever_was_taken_for_dead_stops_before_the_next_step(session, queue_, monkeypatch):
    """A process that could not beat (database unreachable for a minute) is taken for lost
    and its run closed. If it then comes back, it must not go on as if nothing happened:
    the outcome is written already, and the flow may have been launched again already."""
    import json

    user_, folder = queue_
    definition = json.dumps({"nodes": [{"id": "r1", "type": "refresh", "data": {"datasourceId": 123}}], "edges": []})
    flow = make_flow(session, name="con-un-passo", project_id=folder.id, owner_id=user_.id, definition=definition)
    run = orch.create_orchestration_run(session, user_, flow)
    orch.claim(5)
    # another orchestrator closes it
    make = session.get(Run, run.id)
    make.status, make.error = "FAILURE", ORCHESTRATION_INTERRUPTED
    session.add(make); session.commit()

    executed = []

    async def step(*a, **kw):
        executed.append(a)

    monkeypatch.setattr(orch, "_refresh_and_wait", step)
    await orch.run_it(run.id)
    assert executed == []
    assert _state(session, run.id).error == ORCHESTRATION_INTERRUPTED  # not overwritten


async def test_an_outcome_written_already_is_not_written_again(session, queue_):
    user_, folder = queue_
    run = make_run(session, kind="orchestration", status="FAILURE", task_id="", flow_id=_flow(session, user_, folder).id, error="chiuso prima")
    await orch._finalize_orch_run(run.id, "SUCCESS", None)
    assert (_state(session, run.id).status, _state(session, run.id).error) == ("FAILURE", "chiuso prima")


async def test_stopping_the_process_closes_the_running_runs(session, queue_, monkeypatch):
    user_, folder = queue_
    run = orch.create_orchestration_run(session, user_, _flow(session, user_, folder))
    orch.claim(5)
    started_one = asyncio.Event()

    async def long_one(*a, **kw):
        started_one.set()
        await asyncio.sleep(60)

    monkeypatch.setattr(orch, "orchestrate", long_one)
    task = asyncio.create_task(orch.run_it(run.id))
    await started_one.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    closed_one = _state(session, run.id)
    assert (closed_one.status, closed_one.error) == ("FAILURE", ORCHESTRATION_INTERRUPTED)


# ── the worker, from start to end ────────────────────────────────────────────
async def test_the_worker_takes_runs_and_stops(session, queue_, monkeypatch):
    user_, folder = queue_
    monkeypatch.setattr(orch._orc(), "queue_poll_seconds", 0.05)
    stop = asyncio.Event()
    worker = asyncio.create_task(orch.queue_loop(stop))
    try:
        run = orch.create_orchestration_run(session, user_, _flow(session, user_, folder))
        orch.wake()
        for _ in range(100):
            if _state(session, run.id).status == "SUCCESS":
                break
            await asyncio.sleep(0.05)
        assert _state(session, run.id).status == "SUCCESS"
        assert _state(session, run.id).claimed_by == orch.INSTANCE
    finally:
        stop.set()
        await asyncio.wait_for(worker, timeout=5)
    assert orch._wakeup is None  # stopped: `wake()` goes back to doing nothing


def test_waking_where_there_is_no_worker_does_nothing():
    assert orch._wakeup is None
    orch.wake()  # an `api` process: no error, an orchestrator will find the row


# ── the scheduler: one slot, one launch ──────────────────────────────────────
def _schedulato(session, user_, folder, name="notturno"):
    return _flow(
        session, user_, folder, name, run_schedule="0 3 * * *", run_scheduled_by=user_.id,
        next_run_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1),
    )


def _queued(session, flow_id_):
    session.expire_all()
    return session.exec(select(Run).where(Run.kind == "orchestration", Run.flow_id == flow_id_)).all()


async def test_the_due_slot_enqueues_and_moves_on(session, queue_):
    user_, folder = queue_
    flow = _schedulato(session, user_, folder)
    was = flow.next_run_at
    await scheduler._fire_flow(session, flow, datetime.now(timezone.utc))
    (run,) = _queued(session, flow.id)
    assert (run.status, run.trigger_type, run.engine_mode, run.launched_by) == ("PENDING", "schedule", "production", user_.id)
    assert session.get(Flow, flow.id).next_run_at > was


async def test_two_processes_on_the_same_slot_launch_once(session, queue_, db_engine):
    """Both read the flow as due. The first moves the next run forward; the second finds it
    moved and leaves it."""
    user_, folder = queue_
    flow = _schedulato(session, user_, folder)
    now = datetime.now(timezone.utc)
    with Session(db_engine) as a, Session(db_engine) as b:
        seen_by_a = a.get(Flow, flow.id)
        seen_by_b = b.get(Flow, flow.id)
        assert seen_by_a.next_run_at == seen_by_b.next_run_at  # the same slot
        await scheduler._fire_flow(a, seen_by_a, now)
        await scheduler._fire_flow(b, seen_by_b, now)
    assert len(_queued(session, flow.id)) == 1


async def test_when_the_previous_round_is_not_over_the_slot_is_skipped(session, queue_):
    user_, folder = queue_
    flow = _schedulato(session, user_, folder)
    was = flow.next_run_at
    make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=flow.id, claimed_by="x", heartbeat_at=orch._now())
    await scheduler._fire_flow(session, flow, datetime.now(timezone.utc))
    assert len(_queued(session, flow.id)) == 1  # only the one that was there
    assert session.get(Flow, flow.id).next_run_at > was  # but the slot is used up


async def test_a_disabled_author_turns_off_the_schedule(session, queue_):
    user_, folder = queue_
    flow = _schedulato(session, user_, folder)
    user_.is_active = False
    session.add(user_); session.commit()
    await scheduler._fire_flow(session, flow, datetime.now(timezone.utc))
    assert _queued(session, flow.id) == []
    off = session.get(Flow, flow.id)
    assert off.run_schedule is None and off.next_run_at is None


# ── what this process does ───────────────────────────────────────────────────
async def test_an_answer_only_process_starts_no_background_work(monkeypatch):
    import app.main as main
    from app.core.config import get_settings

    started = []

    def fake(name):
        async def loop(stop):
            started.append(name)
        return loop

    loops = ["scheduler_loop", "queue_loop", "retention_loop", "housekeeping_loop", "backup_loop", "webhooks_loop", "engine_settings_loop", "siem_loop"]
    for name in loops:
        monkeypatch.setattr(main, name, fake(name))

    for role, expected_items in (("api", []), ("all", loops), ("orchestrator", loops)):
        started.clear()
        monkeypatch.setenv("APP__ROLE", role)
        get_settings.cache_clear()
        tasks = main.start_background_work(asyncio.Event())
        await asyncio.gather(*tasks)
        assert started == expected_items, role


# ── findings of the adversarial review (2026-10-05) ──────────────────────────
# Each of these tests pins a defect the tests above did not see.

async def test_a_signed_row_whose_task_died_stops_beating(session, queue_, monkeypatch):
    """The heartbeat belongs to the runs with a live task, not to the process. If the task
    dies without closing the run (the database falls while writing the outcome) and the
    process stays up, beating «by signature» would keep that row alive forever: nobody
    would close it and the unique index would block the flow."""
    user_, folder = queue_
    flow = _flow(session, user_, folder)
    run = orch.create_orchestration_run(session, user_, flow)
    orch.claim(5)  # signed by this process…
    assert orch._alive == set()  # …but no task is running it
    assert orch.beat() == 0
    # once the time has passed, it is an orphan like the others
    old = session.get(Run, run.id)
    old.heartbeat_at = orch._now() - timedelta(seconds=orch._orc().dead_after_seconds + 5)
    session.add(old); session.commit()
    assert await orch.close_orphans() == 1
    new = orch.create_orchestration_run(session, user_, flow)
    assert orch.claim(5) == ([new.id], [])  # and the flow starts again


async def test_the_live_tasks_register_follows_the_tasks(session, queue_, monkeypatch):
    user_, folder = queue_
    run = orch.create_orchestration_run(session, user_, _flow(session, user_, folder))
    orch.claim(5)
    inside = asyncio.Event(); via = asyncio.Event()

    async def long_one(*a, **kw):
        inside.set()
        await via.wait()
        return []

    monkeypatch.setattr(orch, "orchestrate", long_one)
    task = orch._start(run.id)
    await inside.wait()
    assert orch._alive == {run.id} and orch.beat() == 1
    via.set()
    await task
    await asyncio.sleep(0)  # lets the task's end callback run
    assert orch._alive == set() and orch.beat() == 0
    assert _state(session, run.id).status == "SUCCESS"


def test_a_take_broken_off_halfway_returns_those_taken_already(session, queue_, monkeypatch):
    """If it raised, the caller would not know it had them: signed, and nobody runs them."""
    user_, folder = queue_
    runs = [orch.create_orchestration_run(session, user_, _flow(session, user_, folder, f"f{i}")).id for i in range(3)]
    real = orch._now
    calls = []

    def now():
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("database caduto")
        return real()

    monkeypatch.setattr(orch, "_now", now)
    taken, refused = orch.claim(5)
    assert taken == runs[:1] and refused == []
    assert _state(session, runs[1]).status == "PENDING"


async def test_the_refused_one_that_started_meanwhile_is_not_closed(session, queue_):
    """Between the index's refusal and the closing as «already running», the other run may
    end and another process may take this one: it is running, and closing it would make
    it look never started."""
    user_, folder = queue_
    started_one = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flow(session, user_, folder).id, claimed_by="un-altro", heartbeat_at=orch._now())
    await orch._finalize_orch_run(started_one.id, "FAILURE", orch.ALREADY_RUNNING, only_if_queued=True)
    assert _state(session, started_one.id).status == "STARTED"
    queued = make_run(session, kind="orchestration", status="PENDING", task_id="", flow_id=_flow(session, user_, folder, "b").id)
    await orch._finalize_orch_run(queued.id, "FAILURE", orch.ALREADY_RUNNING, only_if_queued=True)
    assert _state(session, queued.id).status == "FAILURE"


def test_at_start_the_runs_of_an_earlier_incarnation_are_closed(session, queue_, monkeypatch):
    """The container starting again after a crash: same host, same pid, another signature.
    That process is surely gone, and there is no need to wait for the heartbeat."""
    user_, folder = queue_
    _as(monkeypatch, "host-a:1:nuova")
    now = orch._now()
    def row(name, sign):
        return make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flow(session, user_, folder, name).id, claimed_by=sign, heartbeat_at=now).id
    previous = row("a", "host-a:1:vecchia")
    other_host = row("b", "host-b:1:vecchia")       # another orchestrator: it may be alive
    other_pid = row("c", "host-a:7:vecchia")        # another process on the same machine
    simile = row("d", "host-a:12:vecchia")          # «host-a:1» is a prefix of «host-a:12»: it must not be enough
    mia = row("e", "host-a:1:nuova")
    assert len(orch.close_mine(True)) == 1
    assert _state(session, previous).status == "FAILURE" and _state(session, previous).error == ORCHESTRATION_INTERRUPTED
    assert [_state(session, r).status for r in (other_host, other_pid, simile, mia)] == ["STARTED"] * 4


def test_at_shutdown_all_its_own_are_closed(session, queue_, monkeypatch):
    user_, folder = queue_
    _as(monkeypatch, "host-a:1:io")
    now = orch._now()
    mia = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flow(session, user_, folder, "a").id, claimed_by="host-a:1:io", heartbeat_at=now).id
    others_ = make_run(session, kind="orchestration", status="STARTED", task_id="", flow_id=_flow(session, user_, folder, "b").id, claimed_by="host-b:1:x", heartbeat_at=now).id
    assert len(orch.close_mine(False)) == 1
    assert (_state(session, mia).status, _state(session, others_).status) == ("FAILURE", "STARTED")


# the scheduler: from the SECOND slot of the same round on
def _ds_schedulata(session, user_, folder, conn, name):
    from app.models import Permission
    from tests.conftest import make_datasource

    return make_datasource(
        session, name=name, project_id=folder.id, kind="database", connection_id=conn.id,
        refresh_schedule="0 3 * * *", refresh_scheduled_by=user_.id,
        next_refresh_at=datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1),
    )


async def test_the_rounds_second_slot_is_not_taken_on_a_reread_value(session, queue_, db_engine, monkeypatch):
    """The defect: `seen` was read from the object AFTER the first slot's commit, which
    expires everything — so it was the value already moved by the other process, and the
    comparison always succeeded. Here A works the first slot while B takes the second;
    when A gets to the second it must NOT launch it."""
    from app.models import Connection, Datasource

    user_, folder = queue_
    conn = Connection(name="c", db_type="postgresql", project_id=folder.id, owner_id=user_.id, host="h", port=5432, username="u", database="d")
    session.add(conn); session.commit(); session.refresh(conn)
    ds1 = _ds_schedulata(session, user_, folder, conn, "uno")
    ds2 = _ds_schedulata(session, user_, folder, conn, "due")
    launched = []

    async def fake_launch_(session, user, ds, conn, trigger_type="manual"):
        launched.append(ds.id)

    monkeypatch.setattr(scheduler, "launch_ingest_run", fake_launch_)
    now = datetime.now(timezone.utc)
    with Session(db_engine) as a, Session(db_engine) as b:
        # A runs the due query, as the tick does
        expired_at = [(d, d.next_refresh_at) for d in a.exec(select(Datasource).where(Datasource.id.in_([ds1.id, ds2.id])).order_by(Datasource.id)).all()]
        await scheduler._fire_ds(a, expired_at[0][0], now, expired_at[0][1])   # A: first slot (commit → objects expired)
        await scheduler._fire_ds(b, b.get(Datasource, ds2.id), now)          # B: takes the second
        await scheduler._fire_ds(a, expired_at[1][0], now, expired_at[1][1])   # A: gets to the second
    assert launched == [ds1.id, ds2.id]  # ds2 once only


async def test_even_without_the_passed_value_a_slot_moved_already_is_not_taken_again(session, queue_, db_engine, monkeypatch):
    """The second defence: the UPDATE asks that the slot is still due. One taken already is
    in the future, so it does not match even when read again."""
    user_, folder = queue_
    flow = _schedulato(session, user_, folder)
    now = datetime.now(timezone.utc)
    with Session(db_engine) as b:
        await scheduler._fire_flow(b, b.get(Flow, flow.id), now)
    session.expire_all()
    reread = session.get(Flow, flow.id)       # reads the value already moved by B
    # the first run has finished already: `flow_running` would not act as a net
    for r in _queued(session, flow.id):
        r.status = "SUCCESS"; session.add(r)
    session.commit()
    await scheduler._fire_flow(session, session.get(Flow, flow.id), now)
    assert len(_queued(session, flow.id)) == 1


# the queue does not wait forever
async def test_a_run_queued_too_long_no_longer_starts(session, queue_):
    from app.routes.runs import QUEUE_TIMED_OUT, _reconcile

    user_, folder = queue_
    old_one = make_run(session, kind="orchestration", status="PENDING", task_id="", flow_id=_flow(session, user_, folder, "a").id,
                       started_at=datetime.now(timezone.utc) - timedelta(seconds=orch._orc().queue_timeout_seconds + 60))
    fresh = make_run(session, kind="orchestration", status="PENDING", task_id="", flow_id=_flow(session, user_, folder, "b").id)
    # whoever looks at it (a process that only answers too) finds it closed
    read_one = await _reconcile(session, session.get(Run, old_one.id))
    assert (read_one.status, read_one.error) == ("FAILURE", QUEUE_TIMED_OUT)
    assert (await _reconcile(session, session.get(Run, fresh.id))).status == "PENDING"
    assert orch.claim(5) == ([fresh.id], [])


async def test_the_orchestrator_closes_the_expired_queue_before_taking_it(session, queue_):
    user_, folder = queue_
    old_one = make_run(session, kind="orchestration", status="PENDING", task_id="", flow_id=_flow(session, user_, folder).id,
                       started_at=datetime.now(timezone.utc) - timedelta(seconds=orch._orc().queue_timeout_seconds + 60))
    await orch.close_orphans()
    assert _state(session, old_one.id).status == "FAILURE"
    assert orch.claim(5) == ([], [])


def test_a_dead_after_lowered_from_the_panel_keeps_the_old_one_while_the_others_catch_up(monkeypatch):
    # 120 s → 20 s: another orchestrator reads it up to REREAD_EVERY later and beat at the
    # old pace until then; its live runs are not orphans meanwhile
    from app.core.overrides import REREAD_EVERY

    clock, dead = [1000.0], [120]
    monkeypatch.setattr(orch.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(orch, "_orc", lambda: SimpleNamespace(dead_after_seconds=dead[0]))
    monkeypatch.setattr(orch, "_first_threshold", None)
    monkeypatch.setattr(orch, "_last_threshold", None)
    assert orch.death_threshold() == 120
    dead[0] = 20
    assert orch.death_threshold() == 120
    clock[0] += 120 + REREAD_EVERY - 1
    assert orch.death_threshold() == 120
    clock[0] += 2
    assert orch.death_threshold() == 20
    dead[0] = 300  # raised: the new one at once
    assert orch.death_threshold() == 300
