"""The orchestration of a flow run: interprets the canvas's CONTROL NODES (they are not
engine operations, as the Output nodes are) in the order the SEQUENCE EDGES define
(topological sort, see `flow_resolver.sequence_order`); the unconnected nodes use the
default refresh → output → runflow.

Kinds of action node:
  - refresh   — updates the datasource and WAITS for it to finish (when it fails, the
                whole run stops: better nothing than stale/partial data);
  - output    — launches an Output node of this flow (via flow_resolver) and WAITS for
                it to complete;
  - runflow   — runs another saved flow (a cycle/depth guard), waiting for its
                outputs.

Every step waits for the end of the previous one: so the order is REAL (an output
'downstream' sees the data a refresh or a flow 'upstream' has just written), not only
a launch order.

It tracks the whole run in an 'orchestration run' (kind=orchestration) visible in the
history. With the authority of `user`, RBAC checked again at every step (RUN/CONNECT
for the refresh, RUN + EDIT/CONNECT for the outputs).

WHO runs it. The scheduler and the manual run (`POST /flows/{id}/run-now`) do not run
it: they QUEUE it, that is they write the run's row in state PENDING. A process acting
as orchestrator takes it (`APP__ROLE` = all | orchestrator; there may be more than
one) and brings it to STARTED signing it — a conditional UPDATE, so only one takes it.
From there:

  - while running it, it updates its HEARTBEAT (`heartbeat_at`) — only of the runs
    that really have a live task in this process, not of all those it signed: a
    signed row whose task died must stop beating, or it would stay «running»
    forever;
  - a unique index on the database ensures only one of a flow runs at a time;
  - a STARTED orchestration whose heartbeat stopped belongs to a dead process: the
    first orchestrator that notices closes it saying so. It is NOT resumed: the
    steps already run have written, and redoing them would duplicate the Outputs in
    append;
  - before every step whoever runs it checks the row is still theirs: when it was
    closed meanwhile (it could not beat), it stops.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, or_, text, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import get_settings
from app.db.session import engine, heartbeat_engine
from app.models import Connection, Datasource, Flow, Run, User
from app.models.permission import Capability
from app.models.run import ORCHESTRATION_INTERRUPTED, STEP_INTERRUPTED, TERMINAL_STATES  # noqa: F401 — riesportata
from app.routes.runs import (
    _launch_flow_run, _reconcile, _revoke_task, close_if_queued_too_long, launch_ingest_run, live_refresh,
    resolve_run_engine, start_refresh_if_free,
)
from app.schemas.models import RunCreate
from app.services import permissions as perm_service
from app.services.flow_resolver import FlowResolveError, resolve_output_request, sequence_order

logger = logging.getLogger(__name__)

MAX_FLOW_DEPTH = 5  # a guard on nested runflows (besides the anti-cycle set)

# The wait caps and the queue's rhythms (ORCHESTRATOR__* in the env, or Administration
# → Settings): the orchestrator waits for refreshes and outputs to COMPLETE before the
# steps downstream, so the order is real; past the cap the flow aborts instead of
# reading stale data. Read at every use, not at start: a change from the panel holds
# within a few seconds, for the runs already going too.
def _orc():
    return get_settings().orchestrator


def concurrency() -> int:
    """ORCHESTRATOR__MAX_CONCURRENT, never beyond the connections this process's pool can
    give (each execution holds one while it asks the engine). The pool is sized when the
    process starts: raised together from the panel, the executions follow the pool only
    once the processes have restarted."""
    wanted = _orc().max_concurrent
    try:
        overflow = int(getattr(engine.pool, "_max_overflow", 0))
        if overflow < 0:  # no ceiling on the pool
            return wanted
        return max(1, min(wanted, engine.pool.size() + overflow))
    except Exception:  # noqa: BLE001 — a pool without a size (the tests' SQLite)
        return wanted


ORPHAN_SWEEP_S = 15.0  # how often the orchestrations of dead processes are looked for

# THIS process's signature on the orchestrations it runs. It changes at every start: a
# process that starts again does not inherit those of the previous one.
INSTANCE = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

ALREADY_RUNNING = "flow already running"


def _now() -> datetime:
    """The TIMESTAMP columns are naive-UTC: the heartbeat and the comparison use the same form."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class OrchestrationError(RuntimeError):
    pass


class NoLongerOurs(OrchestrationError):
    """The run no longer belongs to this process: another closed it (it no longer saw its
    heartbeat) or it no longer exists. The remaining steps do not run, and there is
    nothing to record: the outcome is written already."""


def _still_mine(session: Session, run_id: int) -> None:
    row = session.exec(select(Run.status, Run.claimed_by).where(Run.id == run_id)).first()
    if row is None or row[0] != "STARTED" or row[1] != INSTANCE:
        raise NoLongerOurs(f"orchestration {run_id} no longer belongs to this process")


class StopSequence(OrchestrationError):
    """Stops the SEQUENCE, not only the node: the email output with `stopOnFailure` raises
    it. It needs a type of its own because the block running the outputs has an
    `except Exception` that collects the errors and goes on — the wanted behaviour for
    the other outputs. Without a distinct type, the stop was collected by that very
    except and the sequence went on all the same."""


async def _refresh_and_wait(
    session: Session, user: User, ds_id: int, trigger_type: str = "manual", parent_run_id: int | None = None
) -> None:
    """Updates a database datasource and waits (bounded) for SUCCESS."""
    ds = session.get(Datasource, ds_id)
    if ds is None or ds.kind != "database":
        return  # not a DB datasource (nothing to refresh)
    if not perm_service.has_capability(session, user, ds.project_id, Capability.RUN):
        raise OrchestrationError(f"RUN missing for the refresh of datasource {ds_id}")
    conn = session.get(Connection, ds.connection_id) if ds.connection_id else None
    if conn is None:
        raise OrchestrationError(f"datasource {ds_id}: the connection does not exist")
    if not perm_service.has_capability(session, user, conn.project_id, Capability.CONNECT):
        raise OrchestrationError(f"CONNECT missing for the refresh of datasource {ds_id}")

    # when a refresh is running already it is waited for, otherwise one is launched — in
    # line with every other starter (see runs.start_refresh_if_free)
    run = None
    for _ in range(3):  # the running one may end between the two looks: then start ours
        run = await start_refresh_if_free(session, user, ds, conn, trigger_type=trigger_type,
                                          parent_run_id=parent_run_id, launch=launch_ingest_run)
        if run is None:
            live = live_refresh(session, ds_id)
            run = session.get(Run, live) if live is not None else None
        if run is not None:
            break
    if run is None:
        raise OrchestrationError(f"refresh of datasource {ds_id} could not be started")

    from app.services.waits import waits

    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(_orc().refresh_wait_seconds, _orc().poll_interval_seconds)
    for pause in waits(_orc().poll_interval_seconds):
        run = await _reconcile(session, run)
        if run.status in TERMINAL_STATES or loop.time() >= deadline:
            break
        # releases the pool's connection during the wait: while the state does not change
        # _reconcile does not commit, and the SELECT's transaction would hold a connection for
        # the whole sleep → with several slow orchestrations in parallel the pool runs out and
        # the gateway goes down.
        session.commit()
        await asyncio.sleep(pause)
    if run.status != "SUCCESS":
        raise OrchestrationError(f"refresh of datasource {ds_id} failed ({run.status})")
    logger.info("orchestrate: datasource %s updated (refresh completed)", ds_id)


async def _wait_run(session: Session, run: Run) -> Run:
    """Waits for an output run to reach a terminal state, so that the next steps in the
    order really see the result written already (without waiting, the ordering would be
    only cosmetic: the steps run in parallel and an output 'downstream' would read data
    not produced yet).

    No cap of its own: the run's life is already bounded, by the worker's hard time
    limit and by _reconcile, which revokes and fails a run older than
    ENGINE__RUN_STALE_TIMEOUT_SECONDS (counted from its real start, or from launch if
    it never started). A ten-minute cap here counted the time in the worker queue
    too, and on expiry let the flow move on and close while the output kept
    writing: steps downstream read old data, and the next slot could start the
    same flow again on top of it."""
    from app.services.waits import waits

    for pause in waits(_orc().poll_interval_seconds):
        run = await _reconcile(session, run)
        if run.status in TERMINAL_STATES:
            return run
        # see _refresh_and_wait: frees the pool's connection during the sleep
        session.commit()
        await asyncio.sleep(pause)


async def orchestrate(session: Session, user: User, flow: Flow, depth: int = 0, seen: set[int] | None = None, trigger_type: str = "manual", parent_run_id: int | None = None, engine_mode: str = "development", guarded: bool = False) -> list[str]:
    """Runs a flow: the 'action nodes' (refresh/output/runflow) in the order the sequence
    edges define (topological sort), with the unconnected ones in the default order
    refresh → output → runflow.

    A failed refresh RAISES (stops everything: better nothing than stale data); an
    output or a sub-flow in error is collected and it goes on. Returns the list of the
    non-fatal errors (empty = all fine) to mark the orchestration run.

    `engine_mode` ("development" | "production") decides each output's engine (see
    `_launch_flow_run`); the sub-flows (runflow) inherit it and each resolves ITS OWN
    production engine.

    `guarded`: before every step it checks the run `parent_run_id` still belongs to
    this process (see `NoLongerOurs`). Whoever runs from the queue passes it."""
    seen = seen or set()
    if flow.id in seen or depth > MAX_FLOW_DEPTH:
        logger.warning("orchestrate: cycle or excessive depth on flow %s (depth %d)", flow.id, depth)
        return [f"flow {flow.id}: cycle or excessive depth"]
    seen = seen | {flow.id}

    definition = json.loads(flow.definition or "{}")
    default_bucket = get_settings().engine.bucket
    errors: list[str] = []

    def resolve_ds(i: int):
        d = session.get(Datasource, i)
        return (d.bucket, d.key) if d and d.key else None

    # where the Script and Container nodes already run wrote their output: the nodes after them read it
    script_outputs: dict[str, tuple[str, str]] = {}

    for node in sequence_order(definition):
        t = node.get("type")
        d = node.get("data") or {}
        if guarded and parent_run_id is not None:
            _still_mine(session, parent_run_id)
        if t == "refresh":
            ds_id = d.get("datasourceId")
            if ds_id:
                await _refresh_and_wait(session, user, ds_id, trigger_type, parent_run_id)  # error → propagates (aborts everything)
        elif t == "output":
            try:
                # engine_mode decides the development SAMPLE too: only "development" injects it; in
                # production the chain reads every record
                req = resolve_output_request(definition, node, resolve_ds, default_bucket, engine_mode, script_outputs)
                run = await _launch_flow_run(session, user, flow, RunCreate(**req), trigger_type=trigger_type, parent_run_id=parent_run_id, engine_mode=engine_mode)
                run = await _wait_run(session, run)  # waits: the order must be real
                if run.status != "SUCCESS":
                    # An email output with `stopOnFailure` STOPS the sequence, as the refresh does. The
                    # other outputs stay collected and it goes on: the difference is wanted and holds only
                    # for the email, because the steps downstream of a notification often take it for
                    # done (marking "notified" without having notified is worse than stopping). The
                    # option is on the node, not implicit.
                    if d.get("destType") == "email" and d.get("stopOnFailure") is not False:
                        raise StopSequence(
                            f"email sending failed (run {run.id}): {run.error or run.status}. "
                            "The next steps of the flow were not run."
                        )
                    errors.append(f"output: run {run.id} {run.status} — {run.error or ''}".strip())
            except (StopSequence, NoLongerOurs):
                # they must get out: the except below would collect them as any error and the steps
                # downstream would run anyway — taking for done a notification that did not go, or
                # carrying on a run that is no longer ours
                raise
            except Exception as e:
                logger.warning("orchestrate: flow %s, output not run: %s", flow.id, e)
                if d.get("destType") == "email" and d.get("stopOnFailure") is not False:
                    # refused before its run existed (a template the engine rejects, a
                    # recipient outside the allowed domains): the email did not leave
                    # either, so the steps after it must not run
                    reason = getattr(e, "detail", None) or str(e)
                    raise StopSequence(
                        f"email not sent, refused at launch: {reason}. The next steps of the flow were not run."
                    ) from e
                errors.append(f"output: {e}")
        elif t == "script":
            # a failed script stops the flow, as a failed refresh does: the steps after
            # it would read nothing, or stale data
            from app.services import script_runs

            try:
                out = await script_runs.run_script(
                    session, user, flow, definition, node, parent_run_id=parent_run_id, trigger_type=trigger_type,
                    engine_mode=engine_mode, script_outputs=script_outputs, resolve_ds=resolve_ds,
                    default_bucket=default_bucket, launch=_launch_flow_run, wait=_wait_run)
            except (script_runs.ScriptFailed, FlowResolveError) as e:
                raise OrchestrationError(str(e)) from e
            if out is not None:
                script_outputs[node["id"]] = out
        elif t == "container":
            # as a Script node: a failure stops the flow
            from app.services import container_runs

            try:
                out = await container_runs.run_container(
                    session, user, flow, definition, node, parent_run_id=parent_run_id, trigger_type=trigger_type,
                    engine_mode=engine_mode, script_outputs=script_outputs, resolve_ds=resolve_ds,
                    default_bucket=default_bucket, launch=_launch_flow_run, wait=_wait_run)
            except (container_runs.ContainerFailed, FlowResolveError) as e:
                raise OrchestrationError(str(e)) from e
            if out is not None:
                script_outputs[node["id"]] = out
        elif t == "runflow":
            sub_id = d.get("flowId")
            if not sub_id:
                continue
            sub = session.get(Flow, sub_id)
            if sub is None:
                logger.warning("orchestrate: flow %s refers to a missing runflow %s", flow.id, sub_id)
                errors.append(f"runflow: flow {sub_id} does not exist")
                continue
            # the sub-flows' children refer to the same top orchestration run (there is no
            # separate tracer for the sub-flows)
            errors.extend(await orchestrate(session, user, sub, depth + 1, seen, trigger_type, parent_run_id, engine_mode, guarded))
    return errors


def create_orchestration_run(
    session: Session, user: User, flow: Flow, trigger_type: str = "manual",
    engine_mode: str = "development", commit: bool = True,
) -> Run:
    """QUEUES a run of the flow: creates the 'orchestration run' row (kind=orchestration)
    in state PENDING. It is the TRACER of the whole run (of flows without an Output node
    too, which would leave no trace otherwise) and at the same time the queue's item: an
    orchestrator takes it (`claim`). It has no Celery task, `_reconcile` skips it.

    `user` is the authority it will run with; `engine_mode`: "production" from the
    scheduler and from run-now ?mode=production. `commit=False` for whoever writes it in
    the same transaction as something else (the scheduler: the slot taken and the queued
    row are both there or neither)."""
    run = Run(
        kind="orchestration",
        flow_id=flow.id,
        task_id="",
        status="PENDING",
        launched_by=user.id,
        trigger_type=trigger_type,
        engine_mode=engine_mode,
        input_key="",
        output_bucket="",
        output_key="",
    )
    session.add(run)
    if commit:
        session.commit()
        session.refresh(run)
    return run


# the namespace of the «flow queue» advisory locks (the second number is the id)
_QUEUE_LOCK = 74610001
# the same for flow groups, in a namespace of their own (a group and a flow can share an id)
_GROUP_QUEUE_LOCK = 74610004


def create_group_run(session: Session, user: User, group, trigger_type: str = "manual", commit: bool = True) -> Run:
    """Queues a flow group's run: the same PENDING orchestration row a flow gets,
    with `flow_group_id` instead of `flow_id`. An orchestrator claims it and hands it
    to services/flow_group_runner.py. Its flows always run in production mode."""
    run = Run(
        kind="orchestration", flow_group_id=group.id, task_id="", status="PENDING",
        launched_by=user.id, trigger_type=trigger_type, engine_mode="production",
        input_key="", output_bucket="", output_key="",
    )
    session.add(run)
    if commit:
        session.commit()
        session.refresh(run)
    return run


def in_line_for_the_group(session: Session, group_id: int) -> None:
    """As `in_line_for_the_flow`, for a flow group: two replicas, or a replica and
    the scheduler, cannot both see no run and both queue one."""
    if session.get_bind().dialect.name == "postgresql":
        session.connection().execute(
            text("SELECT pg_advisory_xact_lock(:space, :group)"), {"space": _GROUP_QUEUE_LOCK, "group": group_id}
        )


def group_running(session: Session, group_id: int) -> bool:
    """The group has a run queued or in progress."""
    return session.exec(
        select(Run.id)
        .where(Run.kind == "orchestration", Run.flow_group_id == group_id, Run.status.in_(("PENDING", "STARTED")))  # type: ignore[union-attr]
        .limit(1)
    ).first() is not None


def in_line_for_the_flow(session: Session, flow_id: int) -> None:
    """Lines up, until the end of the transaction, whoever is about to look whether the
    flow has a run already and to queue one: «Run now» from several gateway replicas,
    and the scheduler. Without it, two requests together both see the queue empty and
    both queue (live: 2 launches in 20 got through). A Postgres advisory lock tied to
    the transaction: the commit releases it. On SQLite (the tests) it is not needed:
    there is one process only."""
    if session.get_bind().dialect.name == "postgresql":
        session.connection().execute(
            text("SELECT pg_advisory_xact_lock(:namespace, :flow)"), {"namespace": _QUEUE_LOCK, "flow": flow_id}
        )


def flow_running(session: Session, flow_id: int) -> bool:
    """There is a run of the flow already, going or queued."""
    return session.exec(
        select(Run.id)
        .where(Run.kind == "orchestration", Run.flow_id == flow_id, Run.status.in_(("PENDING", "STARTED")))  # type: ignore[union-attr]
        .limit(1)
    ).first() is not None


async def _finalize_orch_run(run_id: int, status: str, error: str | None = None, only_if_queued: bool = False) -> None:
    """Closes the orchestration run (its own session: it runs in the detached task).

    A conditional UPDATE: only whoever finds it still open closes it. Several processes
    may get there together (whoever runs it and whoever takes it for lost), and the
    failure notice must go out once only.

    `only_if_queued`: closes it only while still PENDING. For the launch refused because
    the flow was running already: between the refusal and this closing the other run
    may have finished and another process may have taken this one — which is running by
    then, and must not be closed as «never started»."""
    opened_one = Run.status == "PENDING" if only_if_queued else Run.status.not_in(TERMINAL_STATES)  # type: ignore[union-attr]
    with Session(engine) as session:
        closed_one = session.exec(
            update(Run)
            .where(Run.id == run_id, opened_one)
            .values(status=status, error=error[:2000] if error else None, finished_at=datetime.now(timezone.utc))
        )
        if closed_one.rowcount == 1:
            # the webhooks in the same transaction as the closing (see services/webhooks.py)
            from app.services import webhooks

            webhooks.enqueue_for_run(session, session.get(Run, run_id))
        session.commit()
        if closed_one.rowcount != 1:
            return
        run = session.get(Run, run_id)
        # the notice goes out AFTER the commit: a fact written already is announced, and it
        # does not hold the transaction open for the time of an SMTP send
        from app.services import openlineage
        from app.services.notifier import notify_failure

        openlineage.run_closed(session, run)
        await notify_failure(session, run)


async def stop_steps(run_id: int) -> int:
    """The steps still open of an orchestration closed as interrupted are stopped: an
    Output or a refresh already with a worker went on writing while the flow, closed,
    could start again on top of it (the same rows twice in an append Output, two
    writers on a replaced table). Their engine tasks are revoked, the Jobs of Script
    and Container nodes deleted, a flow group's flows closed the same way. Called
    after the orchestration is closed (a Script or Container run is an orphan only
    then); best effort, one step at a time: what fails is logged, the rest still go."""
    from app.services.script_runs import close_if_orphaned

    stopped = 0
    with Session(engine) as session:
        opened = session.exec(select(Run).where(
            Run.parent_run_id == run_id, Run.status.not_in(TERMINAL_STATES))).all()  # type: ignore[union-attr]
        for run in opened:
            try:
                if run.kind in ("script", "container"):
                    await close_if_orphaned(session, run)
                    continue
                if run.kind == "orchestration":  # a flow of a flow group
                    closed_one = session.exec(update(Run).where(Run.id == run.id, Run.status.not_in(TERMINAL_STATES))
                                          .values(status="FAILURE", error=ORCHESTRATION_INTERRUPTED,
                                                  finished_at=datetime.now(timezone.utc)))
                    session.commit()
                    if closed_one.rowcount == 1:
                        stopped += 1 + await stop_steps(run.id)
                    continue
                # first what the engine says: a step that ended meanwhile keeps its outcome
                # and its effects (a refresh's new parquet swapped in, an Output published),
                # instead of being failed as stopped
                run = await _reconcile(session, run)
                session.commit()
                if run.status in TERMINAL_STATES:
                    continue
                await _revoke_task(run)
                closed_one = session.exec(update(Run).where(Run.id == run.id, Run.status.not_in(TERMINAL_STATES))
                                      .values(status="FAILURE", error=STEP_INTERRUPTED,
                                              finished_at=datetime.now(timezone.utc)))
                session.commit()
                stopped += closed_one.rowcount
            except Exception:  # noqa: BLE001 — one step must not keep the others running
                session.rollback()
                logger.exception("orchestrate: step %s of the interrupted orchestration %s not stopped", run.id, run_id)
    if stopped:
        logger.warning("orchestrate: %d steps of the interrupted orchestration %s stopped", stopped, run_id)
    return stopped


async def interrupt(run_id: int) -> None:
    """Closes an orchestration whose process is stopping, then stops its steps."""
    await _finalize_orch_run(run_id, "FAILURE", ORCHESTRATION_INTERRUPTED)
    await stop_steps(run_id)


# ── Taking from the queue ─────────────────────────────────────────────────────
def claim(limit: int) -> tuple[list[int], list[int]]:
    """Takes from the queue up to `limit` orchestrations and signs them as this process's.
    Returns `(taken, refused)`: the latter are MANUAL launches of a flow that is running
    already, to close as failed.

    Every take is an UPDATE conditional on the state: when two processes try the same
    row only one gets it, the other finds zero rows and moves on. When a run of the flow
    is going already, the unique index refuses the UPDATE."""
    taken: list[int] = []
    refused: list[int] = []
    with Session(engine) as session:
        candidates = session.exec(
            select(Run.id, Run.trigger_type)
            .where(Run.kind == "orchestration", Run.status == "PENDING")
            .order_by(Run.id)
            .limit(limit + 20)  # another one will take some, or they will be refused
        ).all()
        session.rollback()
        for run_id, trigger in candidates:
            if len(taken) >= limit:
                break
            try:
                taken_at = session.exec(
                    update(Run)
                    .where(Run.id == run_id, Run.status == "PENDING")
                    .values(status="STARTED", claimed_by=INSTANCE, heartbeat_at=_now())
                )
                session.commit()
            except IntegrityError:
                session.rollback()
                if trigger == "schedule":
                    # a slot that fired while the previous round had not finished: skipped without
                    # leaving a trace, as it always did
                    session.exec(delete(Run).where(Run.id == run_id, Run.status == "PENDING"))
                    session.commit()
                    logger.info("orchestrate: run %s, flow already running: slot skipped", run_id)
                else:
                    refused.append(run_id)
                continue
            except Exception:
                # the database fell halfway: it stops here and returns what it has taken already, so
                # the caller runs it. Raising would lose it: signed rows with nobody carrying them on.
                logger.exception("orchestrate: taking from the queue interrupted at run %s", run_id)
                session.rollback()
                break
            if taken_at.rowcount == 1:
                taken.append(run_id)
    return taken, refused


async def run_it(run_id: int) -> None:
    """Carries to the end an orchestration this process claimed, and records the outcome on
    the run."""
    try:
        with Session(engine) as session:
            group_id, script_id = session.exec(
                select(Run.flow_group_id, Run.saved_script_id).where(Run.id == run_id)).first() or (None, None)
        if script_id is not None:
            # a saved script's run: same queue, claim and heartbeat, its own runner
            from app.services.saved_script_runs import run_queued

            await run_queued(run_id)
            return
        if group_id is not None:
            # a flow group's run: same queue, claim and heartbeat, its own runner
            from app.services.flow_group_runner import run_group  # imports this module

            await run_group(run_id)
            return
        with Session(engine) as session:
            run = session.get(Run, run_id)
            if run is None:
                return
            flow = session.get(Flow, run.flow_id) if run.flow_id else None
            user = session.get(User, run.launched_by) if run.launched_by else None
            if flow is None or user is None or not user.is_active:
                logger.warning("orchestrate: run %s, flow %s or user %s not valid", run_id, run.flow_id, run.launched_by)
                await _finalize_orch_run(run_id, "FAILURE", "invalid flow or user")
                return
            flow_id = flow.id
            if run.trigger_type == "schedule":
                # the schedule's author must still hold everything the run will need
                from app.services import schedule_authority

                gaps = schedule_authority.gaps_of_flow(session, user, flow)
                if gaps:
                    logger.warning("orchestrate: scheduled run %s of flow %s not started: %s", run_id, flow_id, gaps)
                    await _finalize_orch_run(run_id, "FAILURE", schedule_authority.not_started(user, gaps))
                    return
            # the engine the flow runs with in this mode, as its Outputs record it: the run
            # the API hands back (GET /runs/{id}) says it too
            run.engine = resolve_run_engine(flow, run.engine_mode or "development")
            session.commit()
            try:
                # the child runs (output/refresh) refer to the orchestration run: so the calendar
                # counts only the latter, not the child duplicates
                errors = await orchestrate(
                    session, user, flow, trigger_type=run.trigger_type, parent_run_id=run_id,
                    engine_mode=run.engine_mode or "development", guarded=True,
                )
            except NoLongerOurs as e:
                logger.warning("orchestrate: flow %s, stopping: %s", flow_id, e)
                return
            except (FlowResolveError, OrchestrationError) as e:
                logger.warning("orchestrate: flow %s interrupted: %s", flow_id, e)
                await _finalize_orch_run(run_id, "FAILURE", str(e))
                return
            status = "FAILURE" if errors else "SUCCESS"
            await _finalize_orch_run(run_id, status, "; ".join(errors) if errors else None)
            logger.info("orchestrate: flow %s completed (%s)", flow_id, status)
    except asyncio.CancelledError:
        # the process is stopping: the remaining steps will not run. It is written at once,
        # instead of leaving it for whoever looks for its heartbeat to find out, and the ones
        # started already are stopped
        await interrupt(run_id)
        raise
    except Exception:
        logger.exception("orchestrate: run %s failed", run_id)
        await _finalize_orch_run(run_id, "FAILURE", "internal error during the orchestration")


# ── Heartbeat, and who no longer beats ────────────────────────────────────────
def beat() -> int:
    """Says «here» on the orchestrations this process is REALLY running: those with a live
    task (`_alive`), not all those it signed.

    The difference counts when a task dies without closing its run — the database falls
    while writing the outcome, or the take from the queue breaks off halfway. Beating by
    signature, that row would keep showing alive as long as the process lives: nobody
    would close it, and the unique index would keep the flow blocked. This way it stops
    beating instead and is closed as orphaned.

    On a connection of its own (`heartbeat_engine`), which the requests' pool cannot take
    away from it."""
    with _alive_lock:
        ids = list(_alive)
    with Session(heartbeat_engine) as session:
        if not ids:
            session.exec(text("SELECT 1"))  # the database answers: see heartbeat_healthy
            return 0
        alive = session.exec(
            update(Run)
            .where(Run.id.in_(ids), Run.kind == "orchestration", Run.status == "STARTED", Run.claimed_by == INSTANCE)  # type: ignore[union-attr]
            .values(heartbeat_at=_now())
        )
        session.commit()
        return alive.rowcount


# since when (time.monotonic) this process's beats have gone through, without a miss
_heartbeat_ok_since: float | None = None


def heartbeat_healthy() -> bool:
    """This process has been beating for a whole `dead_after_seconds`. Right after the
    database comes back from an outage every heartbeat is old, the living ones too:
    the orphans' sweep waits until every live orchestrator has had the time to beat
    again, instead of closing (and now stopping the steps of) runs that are running."""
    return _heartbeat_ok_since is not None and time.monotonic() - _heartbeat_ok_since >= _orc().dead_after_seconds


def start_heartbeat(stop: threading.Event) -> threading.Thread:
    """In a THREAD of its own and not on the event loop: the heartbeat says the process is
    alive, and must go on while the loop is busy too. Beating from the loop, a long wait
    in there would make running orchestrations be taken for dead."""

    def run_round() -> None:
        # a second at a time, reading the interval each time: a beat made shorter from the
        # panel applies at once, not after the long wait already started
        last = time.monotonic()
        while not stop.wait(1.0):
            if time.monotonic() - last < _orc().heartbeat_seconds:
                continue
            last = time.monotonic()
            global _heartbeat_ok_since
            try:
                beat()
                if _heartbeat_ok_since is None:
                    _heartbeat_ok_since = time.monotonic()
            except Exception:
                _heartbeat_ok_since = None
                logger.exception("orchestrate: heartbeat failed")

    t = threading.Thread(target=run_round, name="orchestration-heartbeat", daemon=True)
    t.start()
    return t


# the dead-after threshold this process used before the last change, and since when
_first_threshold: tuple[float, float] | None = None
_last_threshold: float | None = None


def death_threshold() -> float:
    """`dead_after_seconds`, but for a while after it is lowered from the panel the old
    one: the other orchestrators read the change up to `REREAD_EVERY` seconds later, and
    meanwhile beat at the old pace. Their live runs are not orphans in that window."""
    global _first_threshold, _last_threshold
    from app.core.overrides import REREAD_EVERY

    current, now = _orc().dead_after_seconds, time.monotonic()
    if _last_threshold is not None and current != _last_threshold:
        _first_threshold = (_last_threshold, now)
    _last_threshold = current
    if _first_threshold is not None and now - _first_threshold[1] < _first_threshold[0] + REREAD_EVERY:
        return max(current, _first_threshold[0])
    return current


async def close_orphans() -> int:
    """Closes the STARTED orchestrations whose heartbeat stopped: the process running them
    died (crash, restart, machine lost) and nobody will finish them. `_reconcile` does
    not see them (they have no task on the engine): without this they would stay
    STARTED forever, and the flow could never start again.

    It holds for the rows without a heartbeat too, which come from an earlier version."""
    cutoff = _now() - timedelta(seconds=death_threshold())
    with _alive_lock:
        mine_alive = list(_alive)
    orphan = (
        (Run.kind == "orchestration")
        & (Run.status == "STARTED")
        & or_(Run.heartbeat_at.is_(None), Run.heartbeat_at < cutoff)  # type: ignore[union-attr]
        # what this very process is running is alive, whatever its beat says
        & ~((Run.claimed_by == INSTANCE) & Run.id.in_(mine_alive or [-1]))  # type: ignore[union-attr]
    )
    closed_ones = 0
    from app.services.notifier import notify_failure

    with Session(engine) as session:
        for run_id in session.exec(select(Run.id).where(orphan)).all():
            # the whole condition again: between the read and here another may have closed it, or
            # whoever runs it may have started beating again
            closed = session.exec(
                update(Run)
                .where(Run.id == run_id, orphan)
                .values(status="FAILURE", error=ORCHESTRATION_INTERRUPTED, finished_at=datetime.now(timezone.utc))
            )
            if closed.rowcount == 1:
                from app.services import webhooks

                webhooks.enqueue_for_run(session, session.get(Run, run_id))  # in the same transaction
            session.commit()
            if closed.rowcount != 1:
                continue
            closed_ones += 1
            await stop_steps(run_id)
            await notify_failure(session, session.get(Run, run_id))
        # and those left queued past the maximum time: they no longer start
        old_items = _now() - timedelta(seconds=_orc().queue_timeout_seconds)
        for run in session.exec(
            select(Run).where(Run.kind == "orchestration", Run.status == "PENDING", Run.started_at < old_items)
        ).all():
            await close_if_queued_too_long(session, run)
    if closed_ones:
        logger.warning("orchestrate: %d orchestrations of a process that is gone, closed", closed_ones)
    return closed_ones


def close_mine(only_earlier: bool) -> list[int]:
    """Closes as interrupted the runs this process signed.

    At START (`only_earlier`): those of an earlier incarnation of it — the same
    host and the same pid, another signature. It is the container starting again after a
    crash or a kill: that process is surely gone, and there is no need to wait for the
    heartbeat to expire to know it (meanwhile the flow would stay blocked and a
    scheduled slot would be lost). A process on another host, or with another pid, is
    not touched: it may be alive.

    At SHUTDOWN: all of its own. Those with a task were closed by the task already;
    what is left are any signed and never started."""
    mine = Run.claimed_by.startswith(INSTANCE.rsplit(":", 1)[0] + ":", autoescape=True) if only_earlier else Run.claimed_by == INSTANCE  # type: ignore[union-attr]
    other_signature = Run.claimed_by != INSTANCE if only_earlier else True
    from app.services import webhooks

    with Session(engine) as session:
        closed_ones = session.exec(
            update(Run)
            .where(Run.kind == "orchestration", Run.status == "STARTED", mine, other_signature)
            .values(status="FAILURE", error=ORCHESTRATION_INTERRUPTED, finished_at=datetime.now(timezone.utc))
            .returning(Run.id)
        ).all()
        for (run_id,) in closed_ones:
            webhooks.enqueue_for_run(session, session.get(Run, run_id))  # in the same transaction
        session.commit()
        return [run_id for (run_id,) in closed_ones]


# ── The worker ───────────────────────────────────────────────────────────────
_running: set[asyncio.Task] = set()
# the ids of the runs with a live task here: what the heartbeat is for (see `beat`).
# The heartbeat thread reads it, the event loop writes it.
_alive: set[int] = set()
_alive_lock = threading.Lock()
# the cells of flow groups running in this process: each holds a connection like an
# orchestration, so they count in the same budget (see services/flow_group_runner.py)
running_cells = 0
_wakeup: asyncio.Event | None = None


def _start(run_id: int) -> asyncio.Task:
    with _alive_lock:
        _alive.add(run_id)
    task = asyncio.create_task(run_it(run_id))
    _running.add(task)

    def finished(t: asyncio.Task) -> None:
        _running.discard(t)
        with _alive_lock:
            _alive.discard(run_id)
        if not t.cancelled() and t.exception() is not None:
            # it could not even write the outcome (database unreachable): from now on it no longer
            # beats, and will be closed as orphaned
            logger.error("orchestrate: run %s, the task died without closing the run: %r", run_id, t.exception())

    task.add_done_callback(finished)
    return task


def wake() -> None:
    """Whoever just queued a run tells this process's worker, if there is one: it takes it
    at once instead of at the next round. In a process that only answers
    (`APP__ROLE=api`) it does nothing, and an orchestrator finds the row within
    `ORCHESTRATOR__QUEUE_POLL_SECONDS`."""
    if _wakeup is not None:
        _wakeup.set()


async def queue_loop(stop: asyncio.Event) -> None:
    """Takes from the queue and runs, as long as the process lives."""
    global _wakeup
    _wakeup = asyncio.Event()
    stop_heartbeat = threading.Event()
    start_heartbeat(stop_heartbeat)
    logger.info(
        "orchestrator %s started: queue every %ss, up to %d runs together",
        INSTANCE, _orc().queue_poll_seconds, concurrency(),
    )
    last_search = 0.0
    try:
        previous_ones = await asyncio.to_thread(close_mine, True)
        if previous_ones:
            logger.warning("orchestrator: %d runs of an earlier start of this process, closed",
                           len(previous_ones))
        for run_id in previous_ones:
            await stop_steps(run_id)
    except Exception:
        logger.exception("orchestrator: closing the earlier runs failed")
    try:
        while not stop.is_set():
            _wakeup.clear()
            try:
                free = concurrency() - len(_running) - running_cells
                if free > 0:
                    taken, refused = await asyncio.to_thread(claim, free)
                    for run_id in taken:
                        _start(run_id)
                    for run_id in refused:
                        await _finalize_orch_run(run_id, "FAILURE", ALREADY_RUNNING, only_if_queued=True)
                if time.monotonic() - last_search >= ORPHAN_SWEEP_S and heartbeat_healthy():
                    last_search = time.monotonic()
                    await close_orphans()
            except Exception:
                logger.exception("orchestrator: error in the queue's round")
            try:
                await asyncio.wait_for(_wakeup.wait(), timeout=_orc().queue_poll_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        _wakeup = None
        stop_heartbeat.set()
        # the process is stopping: the running runs cannot finish, and each writes so on its
        # run (see `run_it`)
        for task in list(_running):
            task.cancel()
        await asyncio.gather(*list(_running), return_exceptions=True)
        try:
            close_mine(False)  # signed and never started: nobody will run them
        except Exception:
            logger.exception("orchestrator: final closing failed")
        logger.info("orchestrator %s stopped", INSTANCE)
