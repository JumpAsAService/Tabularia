"""The gateway's in-process scheduler: refresh of the database datasources AND running
of the flows.

At every tick it launches the work due with the authority of whoever set the schedule
(capabilities captured at creation). For FLOWS the definition is RESOLVED AGAIN at
fire time (Option A): it always uses the datasources' current snapshots and follows
the changes to the flow.

The tick also reconciles the STATE of the non-terminal runs (besides the frontend's
lazy on-poll reconciliation): so an ingest/flow completed on the engine is applied
within a tick even when nobody is looking — otherwise a finished refresh would stay
invisible and the datasource would point at old data.

SEVERAL PROCESSES may run this scheduler together (`APP__ROLE`): each sees the same
due slots, and only ONE launches the work. Whoever wants a slot TAKES it before
launching, moving the next run forward with an UPDATE conditional on the value it
read: when another has moved it already, it finds zero rows and leaves it. Take
first and launch after: when the process dies in between the slot is lost (one time
less), never run twice.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import update
from sqlmodel import Session, select

from app.db.session import engine
from app.models import Connection, Datasource, Flow, FlowGroup, Run, SavedScript, User
from app.models.run import TERMINAL_STATES
from app.routes.runs import _reconcile, launch_ingest_run, start_refresh_if_free
from app.services.blobgc import sweep_blob_deletions
from app.services import contracts as contract_service
from app.services import api_tokens, flow_presence, login_throttle, orchestrator, sessions, webhooks
from app.models.permission import Capability
from app.services import permissions as perm_service
from app.services.schedule import next_fire

logger = logging.getLogger(__name__)

TICK_SECONDS = 60


# ── datasource: refresh schedulato ──────────────────────────────────────────
def recompute_all(session: Session) -> int:
    """The schedules' time zone changed (Administration → Settings, APP__TIMEZONE): the
    next slot of every schedule again, read in the new zone. Without it each one would
    fire once more at the old zone's hour (a monthly one, a month later)."""
    now = datetime.now(timezone.utc)
    n = 0
    for model, schedule, nxt in ((Datasource, "refresh_schedule", "next_refresh_at"), (Flow, "run_schedule", "next_run_at"),
                                 (FlowGroup, "run_schedule", "next_run_at"), (SavedScript, "run_schedule", "next_run_at")):
        for obj in session.exec(select(model).where(getattr(model, schedule).is_not(None))).all():
            try:
                setattr(obj, nxt, next_fire(getattr(obj, schedule), now).replace(tzinfo=None))
            except ValueError:
                continue  # a schedule no longer valid: the tick disables it with its reason
            session.add(obj)
            n += 1
    session.commit()
    return n


def _advance_ds(session: Session, ds: Datasource, now: datetime) -> None:
    try:
        ds.next_refresh_at = next_fire(ds.refresh_schedule, now).replace(tzinfo=None)
    except Exception:
        ds.refresh_schedule = None
        ds.next_refresh_at = None
    session.add(ds)
    session.commit()


def _take_ds_slot(session: Session, ds: Datasource, now: datetime, seen: datetime | None = None) -> bool:
    """Moves the next refresh forward ONLY WHEN it is still the one read: true for one
    process only, the one whose slot it is.

    `seen` is the value the due query read. Whoever did something else in between must
    pass it: every commit of the session expires the objects, and reading
    `ds.next_refresh_at` again here would give the value ALREADY moved by another
    process — with which the comparison would always succeed. For the same reason the
    UPDATE also asks that the slot is still due: one taken already is in the future."""
    seen = ds.next_refresh_at if seen is None else seen
    values: dict = {}
    try:
        values["next_refresh_at"] = next_fire(ds.refresh_schedule, now).replace(tzinfo=None)
    except Exception:
        values.update(refresh_schedule=None, next_refresh_at=None)
    taken_one = session.exec(
        update(Datasource)
        .where(Datasource.id == ds.id, Datasource.next_refresh_at == seen, Datasource.next_refresh_at <= now.replace(tzinfo=None))
        .values(**values)
    )
    session.commit()
    return taken_one.rowcount == 1


def _disable_ds(session: Session, ds: Datasource, reason: str) -> None:
    logger.warning("scheduler: refresh datasource %s disabilitato (%s)", ds.id, reason)
    ds.refresh_schedule = None
    ds.refresh_scheduled_by = None
    ds.next_refresh_at = None
    session.add(ds)
    session.commit()


async def _fire_ds(session: Session, ds: Datasource, now: datetime, seen: datetime | None = None) -> None:
    seen = ds.next_refresh_at if seen is None else seen  # before every commit: see _take_ds_slot
    user = session.get(User, ds.refresh_scheduled_by) if ds.refresh_scheduled_by else None
    if user is None or not user.is_active:
        _disable_ds(session, ds, "the schedule's author is missing or disabled")
        return
    conn = session.get(Connection, ds.connection_id) if ds.connection_id else None
    if conn is None:
        _disable_ds(session, ds, "connection does not exist")
        return
    # The permissions are checked again AT FIRING, not only when the schedule is created:
    # otherwise removing CONNECT from someone does not stop their cron, and whoever has
    # EDIT can move a datasource scheduled by others into their own place and get data
    # supplied with the victim's authority (audit 2026-09-19, A7). It is what the
    # orchestrator does already in `_refresh_and_wait`.
    if not perm_service.has_capability(session, user, ds.project_id, Capability.RUN):
        _disable_ds(session, ds, f"RUN missing for {user.email} on the datasource's folder")
        return
    if not perm_service.has_capability(session, user, conn.project_id, Capability.CONNECT):
        _disable_ds(session, ds, f"CONNECT missing for {user.email} on the connection's folder")
        return
    ds_id, name = ds.id, ds.name
    if not _take_ds_slot(session, ds, now, seen):
        return  # another process took the slot
    # in line with the manual refresh and the flows: one refresh at a time
    run = await start_refresh_if_free(session, user, ds, conn, trigger_type="schedule", launch=launch_ingest_run)
    if run is None:
        logger.info("scheduler: datasource %s (%s) is already refreshing, scheduled slot skipped", ds_id, name)
        return
    logger.info("scheduler: refresh schedulato lanciato per datasource %s (%s)", ds_id, name)


# ── flows: scheduled run ────────────────────────────────────────────────────
def _advance_flow(session: Session, flow: Flow, now: datetime) -> None:
    try:
        flow.next_run_at = next_fire(flow.run_schedule, now).replace(tzinfo=None)
    except Exception:
        flow.run_schedule = None
        flow.next_run_at = None
    session.add(flow)
    session.commit()


def _disable_flow(session: Session, flow: Flow, reason: str) -> None:
    logger.warning("scheduler: run of flow %s disabled (%s)", flow.id, reason)
    flow.run_schedule = None
    flow.run_scheduled_by = None
    flow.next_run_at = None
    session.add(flow)
    session.commit()


async def _fire_flow(session: Session, flow: Flow, now: datetime, seen: datetime | None = None) -> None:
    seen = flow.next_run_at if seen is None else seen  # before every commit: see _take_ds_slot
    user = session.get(User, flow.run_scheduled_by) if flow.run_scheduled_by else None
    if user is None or not user.is_active:
        _disable_flow(session, flow, "the schedule's author is missing or disabled")
        return
    flow_id, name = flow.id, flow.name
    values: dict = {}
    try:
        values["next_run_at"] = next_fire(flow.run_schedule, now).replace(tzinfo=None)
    except Exception:
        values.update(run_schedule=None, next_run_at=None)
    taken_one = session.exec(
        update(Flow)
        .where(Flow.id == flow_id, Flow.next_run_at == seen, Flow.next_run_at <= now.replace(tzinfo=None))
        .values(**values)
    )
    if taken_one.rowcount != 1:
        session.rollback()  # another process took the slot
        return
    orchestrator.in_line_for_the_flow(session, flow_id)  # in line with «Run now»
    if orchestrator.flow_running(session, flow_id):
        session.commit()
        logger.info("scheduler: flow %s (%s) still running, skipping the slot", flow_id, name)
        return
    # The slot taken and the run queued in the SAME transaction: both or neither. An
    # orchestrator runs it (refresh → output → runflow), this one or another; the scheduled
    # runs are "production": they use flow.production_engine.
    orchestrator.create_orchestration_run(session, user, flow, "schedule", "production", commit=False)
    session.commit()
    orchestrator.wake()
    logger.info("scheduler: run of flow %s (%s) queued", flow_id, name)


# ── flow groups: scheduled runs ─────────────────────────────────────────────
def _advance_group(session: Session, group: FlowGroup, now: datetime) -> None:
    try:
        group.next_run_at = next_fire(group.run_schedule, now).replace(tzinfo=None)
    except Exception:
        group.run_schedule = None
        group.next_run_at = None
    session.add(group)
    session.commit()


def _disable_group(session: Session, group: FlowGroup, reason: str) -> None:
    logger.warning("scheduler: schedule of flow group %s turned off (%s)", group.id, reason)
    group.run_schedule = None
    group.run_scheduled_by = None
    group.next_run_at = None
    session.add(group)
    session.commit()


async def _fire_group(session: Session, group: FlowGroup, now: datetime, seen: datetime | None = None) -> None:
    """As `_fire_flow`: take the slot, then queue the run in the same transaction.
    The author's rights are checked again here, as for datasources: a schedule must
    not outlive its author's RUN on the group's folder."""
    seen = group.next_run_at if seen is None else seen
    user = session.get(User, group.run_scheduled_by) if group.run_scheduled_by else None
    if user is None or not user.is_active:
        _disable_group(session, group, "the schedule's author is gone or deactivated")
        return
    if not perm_service.has_capability(session, user, group.project_id, Capability.RUN):
        _disable_group(session, group, f"RUN missing for {user.email} on the group's folder")
        return
    group_id, name = group.id, group.name
    values: dict = {}
    try:
        values["next_run_at"] = next_fire(group.run_schedule, now).replace(tzinfo=None)
    except Exception:
        values.update(run_schedule=None, next_run_at=None)
    taken = session.exec(
        update(FlowGroup)
        .where(FlowGroup.id == group_id, FlowGroup.next_run_at == seen, FlowGroup.next_run_at <= now.replace(tzinfo=None))
        .values(**values)
    )
    if taken.rowcount != 1:
        session.rollback()  # another process took the slot
        return
    orchestrator.in_line_for_the_group(session, group_id)  # in line with "run now"
    if orchestrator.group_running(session, group_id):
        session.commit()
        logger.info("scheduler: flow group %s (%s) still running, slot skipped", group_id, name)
        return
    orchestrator.create_group_run(session, user, group, "schedule", commit=False)
    session.commit()
    orchestrator.wake()
    logger.info("scheduler: run of flow group %s (%s) queued", group_id, name)


# ── saved scripts: scheduled runs ───────────────────────────────────────────
def _advance_script(session: Session, script: SavedScript, now: datetime) -> None:
    try:
        script.next_run_at = next_fire(script.run_schedule, now).replace(tzinfo=None)
    except Exception:
        script.run_schedule = None
        script.next_run_at = None
    session.add(script)
    session.commit()


def _disable_script(session: Session, script: SavedScript, reason: str) -> None:
    logger.warning("scheduler: schedule of saved script %s turned off (%s)", script.id, reason)
    script.run_schedule = script.run_scheduled_by = script.next_run_at = None
    session.add(script)
    session.commit()


async def _fire_script(session: Session, script: SavedScript, now: datetime, seen: datetime | None = None) -> None:
    """As `_fire_group`: the slot taken, the run queued in the same transaction; the
    author's rights checked again here and, in full, when the run starts."""
    from app.services import saved_script_runs, saved_scripts

    seen = script.next_run_at if seen is None else seen
    user = session.get(User, script.run_scheduled_by) if script.run_scheduled_by else None
    if user is None or not user.is_active:
        _disable_script(session, script, "the schedule's author is gone or deactivated")
        return
    if not saved_scripts.can_see(session, user, script) \
            or not perm_service.has_capability(session, user, script.project_id, Capability.RUN):
        _disable_script(session, script, f"{user.email} can no longer run the script")
        return
    script_id, name = script.id, script.name
    values: dict = {}
    try:
        values["next_run_at"] = next_fire(script.run_schedule, now).replace(tzinfo=None)
    except Exception:
        values.update(run_schedule=None, next_run_at=None)
    taken = session.exec(
        update(SavedScript)
        .where(SavedScript.id == script_id, SavedScript.next_run_at == seen,
               SavedScript.next_run_at <= now.replace(tzinfo=None))
        .values(**values)
    )
    if taken.rowcount != 1:
        session.rollback()  # another process took the slot
        return
    saved_script_runs.in_line_for_the_script(session, script_id)  # in line with «Run»
    if saved_script_runs.script_running(session, script_id):
        session.commit()
        logger.info("scheduler: saved script %s (%s) still running, slot skipped", script_id, name)
        return
    saved_script_runs.create_script_run(session, user, script, "schedule", commit=False)
    session.commit()
    orchestrator.wake()
    logger.info("scheduler: run of saved script %s (%s) queued", script_id, name)


# ── loop ────────────────────────────────────────────────────────────────────
async def _tick() -> None:
    now = datetime.now(timezone.utc)
    now_naive = now.replace(tzinfo=None)  # the TIMESTAMP columns are naive-UTC
    with Session(engine) as session:
        # SERVER-SIDE reconcile of the non-terminal runs: an ingest/flow completed on the
        # engine must be APPLIED (parquet swap + the datasource's schema) within a tick even
        # when nobody is polling from the frontend. Without it, a refresh finished while the
        # user left the view stays invisible and the datasource keeps pointing at the OLD data
        # (the user does not know what they are looking at). _reconcile skips the runs
        # terminal already and those without an engine task, and the claim to SUCCESS is
        # atomic → safe with the UI's poll too.
        try:
            pending = session.exec(
                select(Run).where(
                    Run.status.not_in(TERMINAL_STATES),  # type: ignore[union-attr]
                    Run.task_id != "",
                )
            ).all()
            for r in pending:
                try:
                    await _reconcile(session, r)
                except Exception:
                    logger.exception("scheduler: reconcile del run %s fallito", r.id)
        except Exception:
            logger.exception("scheduler: the runs' reconcile pass failed")

        due_ds = session.exec(
            select(Datasource).where(
                Datasource.refresh_schedule.is_not(None),  # type: ignore[union-attr]
                Datasource.next_refresh_at.is_not(None),  # type: ignore[union-attr]
                Datasource.next_refresh_at <= now_naive,
            )
        ).all()
        # the slot's value is fixed NOW, as it came out of the query: after the first commit
        # the objects expire and reading them again would give the one already moved by
        # another process
        for ds, seen in [(ds, ds.next_refresh_at) for ds in due_ds]:
            try:
                await _fire_ds(session, ds, now, seen)
            except Exception:
                logger.exception("scheduler: refresh datasource %s fallito", ds.id)
                _advance_ds(session, ds, now)

        due_flows = session.exec(
            select(Flow).where(
                Flow.run_schedule.is_not(None),  # type: ignore[union-attr]
                Flow.next_run_at.is_not(None),  # type: ignore[union-attr]
                Flow.next_run_at <= now_naive,
            )
        ).all()
        for flow, seen in [(flow, flow.next_run_at) for flow in due_flows]:
            try:
                await _fire_flow(session, flow, now, seen)
            except Exception:
                logger.exception("scheduler: run of flow %s failed", flow.id)
                _advance_flow(session, flow, now)

        due_groups = session.exec(
            select(FlowGroup).where(
                FlowGroup.run_schedule.is_not(None),  # type: ignore[union-attr]
                FlowGroup.next_run_at.is_not(None),  # type: ignore[union-attr]
                FlowGroup.next_run_at <= now_naive,
            )
        ).all()
        for group, seen in [(g, g.next_run_at) for g in due_groups]:
            try:
                await _fire_group(session, group, now, seen)
            except Exception:
                logger.exception("scheduler: run of flow group %s failed", group.id)
                _advance_group(session, group, now)

        from app.services import script_builds

        if script_builds.disabled_reason() is None:
            due_scripts = session.exec(
                select(SavedScript).where(
                    SavedScript.run_schedule.is_not(None),  # type: ignore[union-attr]
                    SavedScript.next_run_at.is_not(None),  # type: ignore[union-attr]
                    SavedScript.next_run_at <= now_naive,
                )
            ).all()
            for script, seen in [(x, x.next_run_at) for x in due_scripts]:
                try:
                    await _fire_script(session, script, now, seen)
                except Exception:
                    logger.exception("scheduler: run of saved script %s failed", script.id)
                    _advance_script(session, script, now)

        # data contracts: freshness is the only rule that breaks by itself, as time goes by —
        # here it is checked again
        try:
            expired_ones = contract_service.sweep_freshness(session, now)
            if expired_ones:
                logger.info("scheduler: %d data contracts changed state for freshness", expired_ones)
        except Exception:
            logger.exception("scheduler: the data contracts' freshness check failed")
        # …and whoever asked to be notified is, when a state changes
        from app.services import contract_notifier  # late: goes through the connections' routes

        sent_ones = await contract_notifier.deliver_pending(session)
        if sent_ones:
            logger.info("scheduler: %d data contract notices sent", sent_ones)

        # short-lived state shared among the replicas: away with the expired rows
        try:
            login_throttle.clean(session)
            flow_presence.clean(session)
            # sessions expired or still for too long: their addresses go with them
            sessions.clean(session)
            # expired API tokens, and the webhooks' delivery log past the window
            api_tokens.clean(session)
            webhooks.clean(session)
        except Exception:
            logger.exception("scheduler: cleanup of the shared state failed")

        # deferred deletion of the blobs whose grace expired (snapshots overtaken, parquets
        # replaced, datasources deleted)
        try:
            removed = await sweep_blob_deletions(session, now)
            if removed:
                logger.info("scheduler: %d obsolete blobs deleted", removed)
        except Exception:
            logger.exception("scheduler: sweep of the deferred blobs failed")


async def scheduler_loop(stop: asyncio.Event) -> None:
    logger.info("scheduler started (tick %ss): datasource refresh + flow runs", TICK_SECONDS)
    while not stop.is_set():
        try:
            await _tick()
        except Exception:
            logger.exception("scheduler: error in the tick")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
    logger.info("scheduler stopped")
