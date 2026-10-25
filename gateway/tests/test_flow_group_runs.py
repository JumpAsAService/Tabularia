"""Running a flow group: steps in order, the cells of a step at the same time, the
first failed step stops the group, a flow already running is waited for, and the
flows inside take their slot so they cannot be started twice meanwhile.

The flows and refreshes themselves are replaced by fakes that record who starts
and when; the queue, the slots and the cell states are the real ones.
"""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from sqlmodel import Session, select

from app.models import FlowGroup, Run
from app.models.permission import Capability
from app.services import flow_group_runner as runner
from app.services import orchestrator as orch
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_user

pytestmark = pytest.mark.anyio


@pytest.fixture
def world(session, db_engine, monkeypatch):
    monkeypatch.setattr(orch, "engine", db_engine)
    monkeypatch.setattr(orch, "heartbeat_engine", db_engine)
    monkeypatch.setattr(orch._orc(), "poll_interval_seconds", 0.01)

    async def no_side_effects(session, run):
        return None
    # closing a run queues webhooks and may send a failure email: not here
    monkeypatch.setattr("app.services.notifier.notify_failure", no_side_effects)
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    p = make_project(session, name="Pipelines")
    flows = {n: make_flow(session, name=n, project_id=p.id) for n in ("A", "B", "C")}
    ds = make_datasource(session, name="erp", project_id=p.id, kind="database")
    return dict(admin=admin, p=p, flows=flows, ds=ds, session=session)


class Fakes:
    """Stands in for orchestrate() and the refresh: records the order, holds the
    flows named in `hold_until_all_started` until every one of them has started."""

    def __init__(self, monkeypatch, fail=(), hold_until_all_started=()):
        self.started: list[str] = []
        self.finished: list[str] = []
        self.fail = set(fail)
        self.hold = set(hold_until_all_started)
        self.all_held_started = asyncio.Event()
        self.parents: dict[str, int] = {}
        monkeypatch.setattr(orch, "orchestrate", self.orchestrate)
        monkeypatch.setattr(orch, "_refresh_and_wait", self.refresh)

    async def _step(self, name):
        self.started.append(name)
        if self.hold and self.hold <= set(self.started):
            self.all_held_started.set()
        if name in self.hold:
            # sequential execution would never get here for the second one: time out
            await asyncio.wait_for(self.all_held_started.wait(), 2)
        await asyncio.sleep(0)
        self.finished.append(name)

    async def orchestrate(self, session, user, flow, **kw):
        self.parents[flow.name] = kw["parent_run_id"]
        assert kw["engine_mode"] == "production" and kw["guarded"] is True
        await self._step(flow.name)
        return ["boom"] if flow.name in self.fail else []

    async def refresh(self, session, user, ds_id, trigger_type="manual", parent_run_id=None):
        name = f"refresh:{ds_id}"
        self.parents[name] = parent_run_id
        await self._step(name)
        if name in self.fail:
            raise orch.OrchestrationError("the source did not answer")


def _group(world, *rows) -> FlowGroup:
    s = world["session"]
    def cell(c):
        if c is None:
            return None
        if c == "R":
            return {"kind": "refresh", "id": world["ds"].id}
        return {"kind": "flow", "id": world["flows"][c].id}
    g = FlowGroup(name="nightly", project_id=world["p"].id,
                  definition=json.dumps({"steps": [[cell(c) for c in row] for row in rows]}))
    s.add(g)
    s.commit()
    s.refresh(g)
    return g


def _claimed_run(world, group: FlowGroup, user=None) -> int:
    s = world["session"]
    run = Run(kind="orchestration", flow_group_id=group.id, task_id="", status="STARTED",
              launched_by=(user or world["admin"]).id, trigger_type="manual",
              claimed_by=orch.INSTANCE, heartbeat_at=orch._now(),
              input_key="", output_bucket="", output_key="")
    s.add(run)
    s.commit()
    return run.id


def _read(db_engine, run_id):
    with Session(db_engine) as s:
        run = s.get(Run, run_id)
        return run.status, run.error, json.loads(run.group_cells or "[]")


async def test_cells_of_a_step_run_together_and_steps_in_order(world, db_engine, monkeypatch):
    fakes = Fakes(monkeypatch, hold_until_all_started={"A", "B"})
    group = _group(world, ["A", "B"], ["C"])
    run_id = _claimed_run(world, group)
    await orch.run_it(run_id)
    status, error, cells = _read(db_engine, run_id)
    assert status == "SUCCESS", error
    assert set(fakes.started[:2]) == {"A", "B"} and fakes.started[2] == "C"
    assert set(fakes.finished[:2]) == {"A", "B"}  # C started only after both ended
    assert [[c["state"] for c in row] for row in cells] == [["succeeded", "succeeded"], ["succeeded"]]
    with Session(db_engine) as s:
        children = s.exec(select(Run).where(Run.parent_run_id == run_id)).all()
    # every flow cell took its flow's slot with a child orchestration, now closed
    assert {c.flow_id for c in children} == {f.id for f in world["flows"].values()}
    assert all(c.kind == "orchestration" and c.status == "SUCCESS" and c.engine_mode == "production" for c in children)
    assert {fakes.parents[n] for n in "ABC"} == {c.id for c in children}
    assert cells[0][0]["run_id"] in {c.id for c in children}
    assert (cells[1][0]["kind"], cells[1][0]["id"]) == ("flow", world["flows"]["C"].id)  # what the cell ran


async def test_the_first_failed_step_stops_the_group(world, db_engine, monkeypatch):
    fakes = Fakes(monkeypatch, fail={"B"})
    group = _group(world, ["A", "B"], ["C"])
    run_id = _claimed_run(world, group)
    await orch.run_it(run_id)
    status, error, cells = _read(db_engine, run_id)
    assert status == "FAILURE" and error == "step 1: 1 of 2 cells failed"  # the details stay in the cells
    assert "C" not in fakes.started
    assert [[c["state"] for c in row] for row in cells] == [["succeeded", "failed"], ["skipped"]]
    assert cells[0][1]["error"] == "'B': boom"


async def test_a_refresh_cell_and_its_failure(world, db_engine, monkeypatch):
    fakes = Fakes(monkeypatch)
    run_id = _claimed_run(world, _group(world, ["R", "A"]))
    await orch.run_it(run_id)
    assert _read(db_engine, run_id)[0] == "SUCCESS"
    assert fakes.parents[f"refresh:{world['ds'].id}"] == run_id

    Fakes(monkeypatch, fail={f"refresh:{world['ds'].id}"})
    with Session(db_engine) as s:
        g2 = FlowGroup(name="second", project_id=world["p"].id,
                       definition=json.dumps({"steps": [[{"kind": "refresh", "id": world["ds"].id}]]}))
        s.add(g2)
        s.commit()
        s.refresh(g2)
    run2 = _claimed_run(world, g2)
    await orch.run_it(run2)
    status, error, cells = _read(db_engine, run2)
    assert status == "FAILURE" and "the source did not answer" in cells[0][0]["error"]


async def test_a_flow_already_running_is_waited_for_not_duplicated(world, db_engine, monkeypatch):
    fakes = Fakes(monkeypatch)
    s = world["session"]
    live = Run(kind="orchestration", flow_id=world["flows"]["A"].id, task_id="", status="STARTED",
               launched_by=world["admin"].id, claimed_by="someone-else", heartbeat_at=orch._now(),
               engine_mode="production", input_key="", output_bucket="", output_key="")
    s.add(live)
    s.commit()
    run_id = _claimed_run(world, _group(world, ["A"]))

    async def finish_it_later():
        await asyncio.sleep(0.05)
        with Session(db_engine) as s2:
            r = s2.get(Run, live.id)
            r.status = "SUCCESS"
            s2.add(r)
            s2.commit()

    await asyncio.gather(orch.run_it(run_id), finish_it_later())
    status, error, cells = _read(db_engine, run_id)
    assert status == "SUCCESS", error
    assert "A" not in fakes.started  # no duplicate run of A
    assert cells[0][0]["waited"] is True and cells[0][0]["run_id"] == live.id


async def test_a_run_in_progress_that_fails_fails_the_cell(world, db_engine, monkeypatch):
    Fakes(monkeypatch)
    s = world["session"]
    live = Run(kind="orchestration", flow_id=world["flows"]["A"].id, task_id="", status="STARTED",
               launched_by=world["admin"].id, claimed_by="someone-else", heartbeat_at=orch._now(),
               engine_mode="production", input_key="", output_bucket="", output_key="")
    s.add(live)
    s.commit()
    run_id = _claimed_run(world, _group(world, ["A"]))

    async def fail_it_later():
        await asyncio.sleep(0.05)
        with Session(db_engine) as s2:
            r = s2.get(Run, live.id)
            r.status, r.error = "FAILURE", "the warehouse is down"
            s2.add(r)
            s2.commit()

    await asyncio.gather(orch.run_it(run_id), fail_it_later())
    status, error, cells = _read(db_engine, run_id)
    assert status == "FAILURE" and "the warehouse is down" in cells[0][0]["error"]


async def test_while_the_group_runs_a_flow_its_slot_is_taken(world, db_engine, monkeypatch):
    seen: list[bool] = []
    fakes = Fakes(monkeypatch)

    async def look(session, user, flow, **kw):
        with Session(db_engine) as s:
            seen.append(orch.flow_running(s, flow.id))  # what the flow's own schedule would see
        with orch._alive_lock:
            seen.append(kw["parent_run_id"] in orch._alive)  # kept alive by the heartbeat
        return []

    monkeypatch.setattr(orch, "orchestrate", look)
    run_id = _claimed_run(world, _group(world, ["A"]))
    await orch.run_it(run_id)
    assert seen == [True, True]
    with orch._alive_lock:
        assert not any(i for i in orch._alive if i != run_id)  # released when done
    assert fakes.started == []


async def test_rights_are_checked_again_when_the_group_runs(world, db_engine, monkeypatch):
    fakes = Fakes(monkeypatch)
    s = world["session"]
    runner_user = make_user(s, email="ops@x.local")
    make_permission(s, user_id=runner_user.id, project_id=world["p"].id, capability=Capability.RUN)
    secret = make_project(s, name="Secret")
    hidden = make_flow(s, name="H", project_id=secret.id)
    g = FlowGroup(name="mixed", project_id=world["p"].id,
                  definition=json.dumps({"steps": [[{"kind": "flow", "id": world["flows"]["A"].id}, {"kind": "flow", "id": hidden.id}]]}))
    s.add(g)
    s.commit()
    s.refresh(g)
    run_id = _claimed_run(world, g, user=runner_user)
    await orch.run_it(run_id)
    status, error, cells = _read(db_engine, run_id)
    assert status == "FAILURE" and "'H': RUN missing on its folder" in cells[0][1]["error"]
    assert fakes.started == ["A"]


async def test_without_run_on_the_groups_folder_nothing_starts(world, db_engine, monkeypatch):
    fakes = Fakes(monkeypatch)
    nobody = make_user(world["session"], email="nobody@x.local")
    run_id = _claimed_run(world, _group(world, ["A"]), user=nobody)
    await orch.run_it(run_id)
    status, error, _ = _read(db_engine, run_id)
    assert status == "FAILURE" and "RUN missing on the folder" in error and fakes.started == []


async def test_a_flow_deleted_since_fails_its_cell(world, db_engine, monkeypatch):
    Fakes(monkeypatch)
    group = _group(world, ["A"])
    s = world["session"]
    s.delete(s.get(type(world["flows"]["A"]), world["flows"]["A"].id))
    s.commit()
    run_id = _claimed_run(world, group)
    await orch.run_it(run_id)
    status, error, cells = _read(db_engine, run_id)
    assert status == "FAILURE" and "no longer exists" in cells[0][0]["error"]


async def test_empty_cells_and_rows_are_skipped_over(world, db_engine, monkeypatch):
    fakes = Fakes(monkeypatch)
    run_id = _claimed_run(world, _group(world, [None, "A", None], [None], ["B"]))
    await orch.run_it(run_id)
    status, _, cells = _read(db_engine, run_id)
    assert status == "SUCCESS" and fakes.started == ["A", "B"]
    assert cells[0][0] is None and cells[1] == [None]


def _live(world, flow, mode, started_at=None):
    s = world["session"]
    run = Run(kind="orchestration", flow_id=world["flows"][flow].id, task_id="", status="STARTED",
              launched_by=world["admin"].id, claimed_by="someone-else", heartbeat_at=orch._now(),
              engine_mode=mode, input_key="", output_bucket="", output_key="")
    if started_at is not None:
        run.started_at = started_at
    s.add(run)
    s.commit()
    return run.id


def _finish_later(db_engine, run_id, delay=0.05):
    async def finish():
        await asyncio.sleep(delay)
        with Session(db_engine) as s2:
            r = s2.get(Run, run_id)
            r.status = "SUCCESS"
            s2.add(r)
            s2.commit()
    return finish()


async def test_a_development_run_in_progress_is_let_end_then_the_flow_runs(world, db_engine, monkeypatch):
    """Someone's run-now on the development engine (with its samples) is no stand-in
    for a group, which promises production: the cell waits, then runs the flow."""
    fakes = Fakes(monkeypatch)
    live = _live(world, "A", "development")
    run_id = _claimed_run(world, _group(world, ["A"]))
    await asyncio.gather(orch.run_it(run_id), _finish_later(db_engine, live))
    status, error, cells = _read(db_engine, run_id)
    assert status == "SUCCESS", error
    assert fakes.started == ["A"] and cells[0][0]["waited"] is False and cells[0][0]["run_id"] != live


async def test_a_run_started_before_its_step_does_not_count_for_it(world, db_engine, monkeypatch):
    """B in step 2 reads what step 1 writes: a run of B that started before step 1
    ended may have read the old data, so step 2 lets it end and runs B again."""
    fakes = Fakes(monkeypatch)
    live = _live(world, "B", "production", started_at=orch._now() - timedelta(minutes=5))
    run_id = _claimed_run(world, _group(world, ["A"], ["B"]))
    await asyncio.gather(orch.run_it(run_id), _finish_later(db_engine, live, 0.1))
    status, error, cells = _read(db_engine, run_id)
    assert status == "SUCCESS", error
    assert fakes.started == ["A", "B"] and cells[1][0]["waited"] is False


async def test_cells_share_the_process_budget(world, db_engine, monkeypatch):
    """Every cell holds a database connection: past ORCHESTRATOR__MAX_CONCURRENT they wait their turn."""
    monkeypatch.setattr(orch._orc(), "max_concurrent", 2)
    monkeypatch.setattr(runner, "CELL_SLOT_WAIT_S", 0.01)
    at_once: list[int] = []

    async def count(session, user, flow, **kw):
        at_once.append(orch.running_cells)
        await asyncio.sleep(0.02)
        return []

    Fakes(monkeypatch)
    monkeypatch.setattr(orch, "orchestrate", count)
    s = world["session"]
    more = [make_flow(s, name=f"P{i}", project_id=world["p"].id) for i in range(3)]
    world["flows"].update({f.name: f for f in more})
    run_id = _claimed_run(world, _group(world, ["A", "B", "C", "P0", "P1"]))
    await orch.run_it(run_id)
    assert _read(db_engine, run_id)[0] == "SUCCESS"
    assert len(at_once) == 5 and max(at_once) <= 2 and orch.running_cells == 0


async def test_an_unexpected_error_closes_the_flows_run_at_once(world, db_engine, monkeypatch):
    """The engine answering an error (an HTTPException) is not left for the orphan
    sweep: the flow's run ends now, so its schedule and run-now are not blocked."""
    from fastapi import HTTPException

    async def refuses(session, user, flow, **kw):
        raise HTTPException(status_code=502, detail="the engine refused the import")

    Fakes(monkeypatch)
    monkeypatch.setattr(orch, "orchestrate", refuses)
    run_id = _claimed_run(world, _group(world, ["A"]))
    await orch.run_it(run_id)
    status, _, cells = _read(db_engine, run_id)
    assert status == "FAILURE" and "the engine refused the import" in cells[0][0]["error"]
    with Session(db_engine) as s:
        child = s.exec(select(Run).where(Run.parent_run_id == run_id)).one()
    assert child.status == "FAILURE" and "refused" in child.error


async def test_a_group_taken_away_stops_every_cell(world, db_engine, monkeypatch):
    """B waits on a run in progress while A works. When the group stops being this
    process's, B's wait notices and A is cancelled too: its flow's run is closed as
    interrupted, nothing keeps running unseen."""
    Fakes(monkeypatch)
    a_started = asyncio.Event()

    async def slow(session, user, flow, **kw):
        a_started.set()
        await asyncio.sleep(30)
        return []

    monkeypatch.setattr(orch, "orchestrate", slow)
    _live(world, "B", "production")
    run_id = _claimed_run(world, _group(world, ["A", "B"]))

    async def take_it_away():
        await a_started.wait()
        with Session(db_engine) as s2:
            r = s2.get(Run, run_id)
            r.claimed_by = "another-process"
            s2.add(r)
            s2.commit()

    await asyncio.wait_for(asyncio.gather(orch.run_it(run_id), take_it_away()), 5)
    with Session(db_engine) as s:
        a_child = s.exec(select(Run).where(Run.parent_run_id == run_id, Run.flow_id == world["flows"]["A"].id)).one()
        group_run = s.get(Run, run_id)
    assert a_child.status == "FAILURE" and a_child.error == orch.ORCHESTRATION_INTERRUPTED
    assert group_run.status == "STARTED" and group_run.claimed_by == "another-process"  # left to its new owner


async def test_a_launcher_deactivated_meanwhile_stops_the_next_steps(world, db_engine, monkeypatch):
    fakes = Fakes(monkeypatch)
    s = world["session"]
    ops = make_user(s, email="ops@x.local")
    make_permission(s, user_id=ops.id, project_id=world["p"].id, capability=Capability.RUN)
    original = fakes.orchestrate

    async def deactivate_after_a(session, user, flow, **kw):
        out = await original(session, user, flow, **kw)
        if flow.name == "A":
            with Session(db_engine) as s2:
                u = s2.get(type(ops), ops.id)
                u.is_active = False
                s2.add(u)
                s2.commit()
        return out

    monkeypatch.setattr(orch, "orchestrate", deactivate_after_a)
    run_id = _claimed_run(world, _group(world, ["A"], ["B"]), user=ops)
    await orch.run_it(run_id)
    status, _, cells = _read(db_engine, run_id)
    assert status == "FAILURE" and fakes.started == ["A"]
    assert cells[1][0]["error"] == runner.USER_GONE

