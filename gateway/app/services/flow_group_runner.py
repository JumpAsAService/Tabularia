"""Runs a flow group: its steps in order, the cells of a step at the same time.

A group run is a kind="orchestration" row with `flow_group_id` set: it is queued,
claimed, given a heartbeat and closed as an orphan exactly like a flow's
orchestration (services/orchestrator.py), which hands it here from `run_it`.

- A **flow** cell takes the flow's live slot first: a child orchestration row for
  the flow, claimed by this process and kept alive by its heartbeat, under which
  the flow runs as a scheduled run does (production engines). The flow's own
  schedule then cannot start a duplicate meanwhile. When the flow already has a
  live run, the database refuses the slot and the cell waits for that run: its
  outcome counts when it runs in production and started after the previous step
  ended (in the first step, any production run); otherwise it may have read data
  older than the earlier steps, or development samples, so the cell lets it end
  and then runs the flow itself.
- A **refresh** cell refreshes a database datasource, waiting for an import already
  in progress if there is one (`orchestrator._refresh_and_wait`).
- A **container** cell runs an image as a Container node with no edge does
  (services/container_runs.py): no data in, none out, a run under the group's.
- A **pushmail** cell sends one email with one .xlsx per datasource and one file per
  report through an SMTP connection, as an engine task under a run of its own
  (kind="pushmail"); every part of it — the connection, the recipients' domains, the
  datasources, the reports and what they read — is checked again with the rights of
  whoever runs the group (services/pushmail.py `pushmail_errors`), and the reports are
  resolved with those rights too (services/reports.py `prepare`), then rendered by the
  engine in the task that writes the email.
- A **script** cell runs a saved script's current version (services/saved_script_runs.py)
  under a child orchestration row that holds the script's live slot, as a flow cell
  does; a run of the script already in progress is waited for, and its outcome counts
  when it started after the previous step ended.
- A **sql** cell writes a SQL script's datasource from its current version
  (services/sql_script_runs.py), as «Refresh now» on its page does; a write of the
  script already going is waited for, and its outcome counts when it started after the
  previous step ended.
- When every cell of a step has finished, a failure among them stops the group:
  the following steps are skipped and the run ends FAILURE. The cells keep their
  own errors; the run's error only counts them, because the reader of a group may
  not be allowed to read every flow or datasource in it (see routes/flow_groups.py).

Every cell holds a database connection while it runs, as an orchestration does, so
cells share the process's budget of `ORCHESTRATOR__MAX_CONCURRENT`: past it they
wait, and the queue takes fewer runs meanwhile. Within that, how many cells really
run at once is up to the workers and the queue.

The state of every cell is kept in memory and written whole to the group run
(`runs.group_cells`) under a lock, so cells finishing together do not undo each
other's writes. Every name this module uses from the orchestrator is read through
the module (`orch.engine`, `orch.orchestrate`...), so tests can replace them.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.models import Datasource, Flow, FlowGroup, Run, SavedScript, SqlScript, User
from app.models.permission import Capability
from app.models.run import TERMINAL_STATES
from app.schemas.models import FlowGroupCell
from app.services import orchestrator as orch
from app.services import permissions as perm_service
from app.services.flow_groups import definition_of
from app.services.flow_resolver import FlowResolveError

logger = logging.getLogger(__name__)

# a group is an operations tool: its flows run on their production engines, as a
# scheduled run does, whoever starts the group and however
ENGINE_MODE = "production"

WAITING, RUNNING, SUCCEEDED, FAILED, SKIPPED = "waiting", "running", "succeeded", "failed", "skipped"
SLOT_ATTEMPTS = 3
CELL_SLOT_WAIT_S = 0.5
USER_GONE = "the user running the group is no longer active"


class _Cells:
    """The state of every cell, parallel to the grid, written whole to the group run.
    Each cell names what it ran, so a grid edited since still reads the run right."""

    def __init__(self, run_id: int, steps: list[list[FlowGroupCell | None]]):
        self.run_id = run_id
        self.grid = [[None if c is None else {"kind": c.kind, "id": c.id, "state": WAITING, "run_id": None,
                                              "error": None, "waited": False}
                      for c in row] for row in steps]
        self.lock = asyncio.Lock()

    def write(self) -> None:
        with Session(orch.engine) as s:
            s.exec(update(Run).where(Run.id == self.run_id).values(group_cells=json.dumps(self.grid)))
            s.commit()

    async def set(self, step: int, col: int, **fields) -> None:
        async with self.lock:
            self.grid[step][col].update(fields)
            self.write()

    async def skip_after(self, step: int) -> None:
        async with self.lock:
            for row in self.grid[step + 1:]:
                for cell in row:
                    if cell is not None:
                        cell["state"] = SKIPPED
            self.write()


async def run_group(run_id: int) -> None:
    """Carries a claimed group run to its end and records the outcome."""
    with Session(orch.engine) as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        group = session.get(FlowGroup, run.flow_group_id)
        user = session.get(User, run.launched_by) if run.launched_by else None
        if group is None or user is None or not user.is_active:
            await orch._finalize_orch_run(run_id, "FAILURE", "flow group or user no longer valid")
            return
        if not perm_service.has_capability(session, user, group.project_id, Capability.RUN):
            await orch._finalize_orch_run(run_id, "FAILURE", f"RUN missing on the folder of flow group '{group.name}'")
            return
        if run.trigger_type == "schedule":
            # every cell, before the first one starts: a schedule must not run half a group
            from app.services import schedule_authority

            gaps = schedule_authority.gaps_of_group(session, user, group)
            if gaps:
                await orch._finalize_orch_run(run_id, "FAILURE", schedule_authority.not_started(user, gaps))
                return
        steps = definition_of(group).steps
        trigger, user_id, name = run.trigger_type, user.id, group.name

    cells = _Cells(run_id, steps)
    cells.write()
    stopped: str | None = None
    taken_away = False
    try:
        stopped = await _run_steps(run_id, user_id, trigger, cells, steps)
    except* orch.NoLongerOurs as group_error:
        # the group is no longer ours (another process closed it): leave it be
        logger.warning("flow group %r, stopping: %s", name, group_error.exceptions[0])
        taken_away = True
    if taken_away:
        return
    if stopped:
        await orch._finalize_orch_run(run_id, "FAILURE", stopped)
        logger.info("flow group %r: %s", name, stopped)
        return
    await orch._finalize_orch_run(run_id, "SUCCESS")
    logger.info("flow group %r: completed", name)


async def _run_steps(run_id: int, user_id: int, trigger: str, cells: _Cells,
                     steps: list[list[FlowGroupCell | None]]) -> str | None:
    """Runs the steps; returns why the group stopped, or None when every step succeeded.
    A cell that raises cancels its siblings (a task group), so nothing of a closed
    group keeps running unseen."""
    for n, row in enumerate(steps):
        todo = [(col, cell) for col, cell in enumerate(row) if cell is not None]
        if not todo:
            continue
        with Session(orch.engine) as session:
            orch._still_mine(session, run_id)
        # what a flow's run in progress must have started after, to count for this step
        since = orch._now() if n else None
        async with asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(_run_cell(run_id, user_id, trigger, cells, n, col, cell, since))
                     for col, cell in todo]
        failed = sum(1 for t in tasks if t.result())
        if failed:
            await cells.skip_after(n)
            return f"step {n + 1}: {failed} of {len(todo)} cells failed"
    return None


async def _run_cell(run_id: int, user_id: int, trigger: str, cells: _Cells, step: int, col: int,
                    cell: FlowGroupCell, since: datetime | None) -> str | None:
    """Runs one cell and records its state; returns why it failed, or None."""
    while orch.running_cells >= orch.concurrency():
        await asyncio.sleep(CELL_SLOT_WAIT_S)  # the process's budget is full: the cell stays waiting
    orch.running_cells += 1
    try:
        await cells.set(step, col, state=RUNNING)
        try:
            if cell.kind == "flow":
                error = await _flow_cell(run_id, user_id, trigger, cell.id, cells, step, col, since)
            elif cell.kind == "script":
                error = await _script_cell(run_id, user_id, trigger, cell.id, cells, step, col, since)
            elif cell.kind == "container":
                error = await _container_cell(run_id, user_id, trigger, cell, cells, step, col)
            elif cell.kind == "pushmail":
                error = await _pushmail_cell(run_id, user_id, trigger, cell, cells, step, col)
            elif cell.kind == "sql":
                error = await _sql_cell(run_id, user_id, trigger, cell.id, cells, step, col, since)
            else:
                error = await _refresh_cell(run_id, user_id, trigger, cell.id, cells, step, col)
        except (orch.NoLongerOurs, asyncio.CancelledError):
            raise
        except Exception as e:  # one cell's bug must not leave the group hanging
            logger.exception("flow group run %s: cell %d/%d failed", run_id, step + 1, col + 1)
            error = f"internal error ({type(e).__name__})"
        await cells.set(step, col, state=FAILED if error else SUCCEEDED, error=error)
        return error
    finally:
        orch.running_cells -= 1


def _counts(live_mode: str | None, live_started: datetime | None, since: datetime | None) -> bool:
    """Whether a flow's run in progress can stand for this cell (see the module doc)."""
    if live_mode != ENGINE_MODE:
        return False
    return since is None or (live_started is not None and live_started >= since)


async def _flow_cell(run_id: int, user_id: int, trigger: str, flow_id: int, cells: _Cells, step: int, col: int,
                     since: datetime | None) -> str | None:
    with Session(orch.engine) as s:
        flow = s.get(Flow, flow_id)
        user = s.get(User, user_id)
        if flow is None:
            return f"flow {flow_id} no longer exists"
        if user is None or not user.is_active:
            return USER_GONE
        name = flow.name
        if not perm_service.has_capability(s, user, flow.project_id, Capability.RUN):
            return f"'{name}': RUN missing on its folder"
        for _ in range(SLOT_ATTEMPTS):
            child = Run(
                kind="orchestration", flow_id=flow.id, task_id="", status="STARTED", launched_by=user.id,
                trigger_type=trigger, engine_mode=ENGINE_MODE, parent_run_id=run_id,
                claimed_by=orch.INSTANCE, heartbeat_at=orch._now(),
                input_key="", output_bucket="", output_key="",
            )
            s.add(child)
            try:
                s.commit()
            except IntegrityError:
                # the flow already has a live run: never a second one at the same time
                s.rollback()
                live = s.exec(select(Run.id, Run.engine_mode, Run.started_at).where(
                    Run.kind == "orchestration", Run.flow_id == flow_id, Run.status == "STARTED",
                )).first()
                if live is None:
                    continue  # it ended in between: take the slot after all
                if _counts(live[1], live[2], since):
                    return await _wait_for(s, run_id, live[0], name, cells, step, col, take_outcome=True)
                await _wait_for(s, run_id, live[0], name, cells, step, col, take_outcome=False)
                continue  # it is over: now run the flow for this step
            break
        else:
            return f"'{name}': its slot could not be taken"

        child_id = child.id
        await cells.set(step, col, run_id=child_id, waited=False)
        with orch._alive_lock:
            orch._alive.add(child_id)  # the heartbeat keeps it alive, a crash closes it as orphan
        try:
            try:
                errors = await orch.orchestrate(
                    s, user, flow, trigger_type=trigger, parent_run_id=child_id,
                    engine_mode=ENGINE_MODE, guarded=True,
                )
            except orch.NoLongerOurs:
                return f"'{name}': interrupted"
            except (FlowResolveError, orch.OrchestrationError) as e:
                errors = [str(e)]
            except asyncio.CancelledError:
                await orch.interrupt(child_id)
                raise
            except Exception as e:
                # anything else (the engine answering an error, the network): the
                # flow's run ends here, not minutes later as an orphan
                logger.exception("flow group run %s: flow %s failed", run_id, flow_id)
                errors = [_describe(e)]
            await orch._finalize_orch_run(child_id, "FAILURE" if errors else "SUCCESS", "; ".join(errors) if errors else None)
            return f"'{name}': " + "; ".join(errors) if errors else None
        finally:
            with orch._alive_lock:
                orch._alive.discard(child_id)


async def _script_cell(run_id: int, user_id: int, trigger: str, script_id: int, cells: _Cells, step: int, col: int,
                       since: datetime | None) -> str | None:
    from app.services import saved_script_runs
    from app.services.script_runs import ScriptFailed

    with Session(orch.engine) as s:
        script = s.get(SavedScript, script_id)
        user = s.get(User, user_id)
        if script is None:
            return f"saved script {script_id} no longer exists"
        if user is None or not user.is_active:
            return USER_GONE
        name = script.name
        for _ in range(SLOT_ATTEMPTS):
            child = Run(
                kind="orchestration", saved_script_id=script.id, task_id="", status="STARTED", launched_by=user.id,
                trigger_type=trigger, engine_mode=ENGINE_MODE, parent_run_id=run_id,
                claimed_by=orch.INSTANCE, heartbeat_at=orch._now(),
                input_key="", output_bucket="", output_key="",
            )
            s.add(child)
            try:
                s.commit()
            except IntegrityError:
                # the script already has a live run: never a second one at the same time
                s.rollback()
                live = s.exec(select(Run.id, Run.started_at).where(
                    Run.kind == "orchestration", Run.saved_script_id == script_id, Run.status == "STARTED",
                )).first()
                if live is None:
                    continue  # it ended in between: take the slot after all
                if since is None or (live[1] is not None and live[1] >= since):
                    return await _wait_for(s, run_id, live[0], name, cells, step, col, take_outcome=True)
                await _wait_for(s, run_id, live[0], name, cells, step, col, take_outcome=False)
                continue  # it is over: now run the script for this step
            break
        else:
            return f"'{name}': its slot could not be taken"

        child_id = child.id
        await cells.set(step, col, run_id=child_id, waited=False)
        with orch._alive_lock:
            orch._alive.add(child_id)  # the heartbeat keeps it alive, a crash closes it as orphan
        try:
            try:
                await saved_script_runs.execute(s, user, script, parent_run_id=child_id, trigger_type=trigger)
                error = None
            except ScriptFailed as e:
                error = str(e)
            except asyncio.CancelledError:
                await orch.interrupt(child_id)
                raise
            except Exception as e:
                logger.exception("flow group run %s: saved script %s failed", run_id, script_id)
                error = _describe(e)
            await orch._finalize_orch_run(child_id, "FAILURE" if error else "SUCCESS", error)
            return f"'{name}': {error}" if error else None
        finally:
            with orch._alive_lock:
                orch._alive.discard(child_id)


async def _sql_cell(run_id: int, user_id: int, trigger: str, script_id: int, cells: _Cells, step: int, col: int,
                    since: datetime | None) -> str | None:
    from app.services import sql_script_runs

    with Session(orch.engine) as s:
        script = s.get(SqlScript, script_id)
        user = s.get(User, user_id)
        if script is None:
            return f"SQL script {script_id} no longer exists"
        if user is None or not user.is_active:
            return USER_GONE
        name = script.name
        for _ in range(SLOT_ATTEMPTS):
            live = sql_script_runs.live_run(s, script_id)
            if live is not None:
                # a write already going: its outcome counts when it read after the previous step
                if since is None or (live.started_at is not None and live.started_at >= since):
                    return await _wait_for(s, run_id, live.id, name, cells, step, col, take_outcome=True,
                                           engine_task=True)
                await _wait_for(s, run_id, live.id, name, cells, step, col, take_outcome=False, engine_task=True)
                continue
            try:
                run = await sql_script_runs.launch(s, user, script, trigger, parent_run_id=run_id)
            except HTTPException as e:
                if e.status_code == 409 and e.detail == sql_script_runs.ALREADY_RUNNING:
                    continue  # one started in between: wait for it
                return f"'{name}': {e.detail}"
            break
        else:
            return f"'{name}': its write could not be started"
        await cells.set(step, col, run_id=run.id, waited=False)
        try:
            run = await orch._wait_run(s, run)
        except asyncio.CancelledError:
            raise
        if run.status != "SUCCESS":
            return f"'{name}': {run.error or run.status}"
        return None


async def _container_cell(run_id: int, user_id: int, trigger: str, cell: FlowGroupCell, cells: _Cells,
                          step: int, col: int) -> str | None:
    from app.core.config import get_settings
    from app.services import container_runs

    node = {"id": f"container-cell-{cell.id}", "type": "container", "data": dict(cell.container or {})}
    with Session(orch.engine) as s:
        user = s.get(User, user_id)
        if user is None or not user.is_active:
            return USER_GONE
        error = None
        try:
            await container_runs.run_container(
                s, user, None, {"nodes": [node], "edges": []}, node, parent_run_id=run_id, trigger_type=trigger,
                engine_mode=ENGINE_MODE, script_outputs={}, resolve_ds=None,
                default_bucket=get_settings().engine.bucket, launch=None, wait=None)
        except container_runs.ContainerFailed as e:
            error = str(e)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.exception("flow group run %s: container cell %s failed", run_id, cell.id)
            error = _describe(e)
        # this cell's own run (its node id is the cell's): not the latest container run of
        # the group, which with parallel cells is a sister's, and none when it never started
        mine = next((r.id for r in s.exec(select(Run).where(Run.parent_run_id == run_id, Run.kind == "container")
                                          .order_by(Run.id.desc())).all()
                     if _node_id(r) == node["id"]), None)
        await cells.set(step, col, run_id=mine)
        return error


async def _pushmail_cell(run_id: int, user_id: int, trigger: str, cell: FlowGroupCell, cells: _Cells,
                         step: int, col: int) -> str | None:
    import uuid

    from app.core.engine_client import get_engine_client
    from app.models import Connection
    from app.routes.connections import engine_connection_payload
    from app.services.pushmail import pushmail_errors
    from app.routes.runs import PLACEHOLDER_TASK
    from app.services import audit

    with Session(orch.engine) as s:
        user = s.get(User, user_id)
        if user is None or not user.is_active:
            return USER_GONE
        cfg = cell.pushmail
        problems = pushmail_errors(s, user, cell)
        if problems or cfg is None:
            return "; ".join(problems) or "pushmail: the cell has no settings"
        name = cfg.label.strip() or "Pushmail"
        group_run = s.get(Run, run_id)
        group = s.get(FlowGroup, group_run.flow_group_id) if group_run else None
        conn = s.get(Connection, cfg.connectionId)
        datasources = {d.id: d for d in s.exec(select(Datasource).where(
            Datasource.id.in_([a.datasourceId for a in cfg.attachments]))).all()}
        empty = [datasources[a.datasourceId].name for a in cfg.attachments if not datasources[a.datasourceId].key]
        if empty:
            return f"«{name}»: no data yet in {', '.join(empty)} (never imported or published)"
        # the reports: resolved here, with the rights of whoever runs the group, before
        # anything is sent; the engine renders them in the worker that writes the email
        rendered, read, failed = await _cell_reports(s, user, cfg, group, trigger)
        if failed:
            return f"«{name}»: {failed}"
        to = [a.strip() for a in cfg.to if a.strip()]
        cc = [a.strip() for a in cfg.cc if a.strip()]
        # the run first: its number is the {run_id} of the names; a placeholder task
        # until the engine queues it (routes/runs.py closes it if it never is)
        child = Run(kind="pushmail", task_id=f"{PLACEHOLDER_TASK}{uuid.uuid4().hex}", status="PENDING",
                    launched_by=user.id, trigger_type=trigger, parent_run_id=run_id,
                    input_key="", output_bucket="", output_key="",
                    email=json.dumps({"connection_id": conn.id, "host": conn.host, "to": to + cc, "subject": cfg.subject,
                                      "attachments": [datasources[a.datasourceId].name for a in cfg.attachments],
                                      # by id too, for the lineage: a name can change
                                      "datasource_ids": [a.datasourceId for a in cfg.attachments],
                                      "reports": [{"id": r["id"], "name": r["name"], "format": r["format"],
                                                   "in_body": r["in_body"]} for r in rendered],
                                      # what the reports read, for the lineage too
                                      "report_datasource_ids": sorted(read),
                                      "cell": name, "cell_id": cell.id, "ok": None}))
        s.add(child)
        s.commit()
        s.refresh(child)
        await cells.set(step, col, run_id=child.id)
        spec = {
            "to": to, "cc": cc, "subject": cfg.subject, "body": cfg.body, "body_is_html": cfg.bodyIsHtml,
            "timezone": (cfg.timezone or "").strip() or "UTC", "run_id": child.id, "flow": group.name if group else "",
            "lang": user.locale if user.locale in ("en", "it", "fr", "de", "es") else "en",
            "attachments": [{"bucket": datasources[a.datasourceId].bucket, "key": datasources[a.datasourceId].key,
                             "label": datasources[a.datasourceId].name, "file_name": a.fileName, "max_rows": a.maxRows}
                            for a in cfg.attachments],
            "reports": [{k: r[k] for k in ("name", "definition", "datasets", "params", "format", "file_name", "in_body",
                                           "locale")} for r in rendered],
        }
        # who sent what to whom, written when it leaves, whatever the outcome
        audit.record_audit(
            s, actor=user, action=audit.EMAIL_SEND, target_type="flow_group",
            target_id=group.id if group else None, target_label=group.name if group else None,
            detail={"connection_id": conn.id, "host": conn.host, "to": to + cc, "subject": cfg.subject,
                    "cell": name, "datasources": [a.datasourceId for a in cfg.attachments],
                    "reports": [r["id"] for r in rendered], "trigger": trigger, "run_id": child.id},
        )
        try:
            resp = await get_engine_client().post("/db/pushmail", json={
                "connection": engine_connection_payload(conn), "spec": spec}, timeout=30)
            answer = resp.json() if resp.content else {}
            if resp.status_code >= 400:
                raise RuntimeError(str(answer.get("detail") or resp.status_code))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — refused or not reachable: the cell fails saying why
            await orch._finalize_orch_run(child.id, "FAILURE", f"the engine did not take the email: {e}")
            _audit_reports(s, user, rendered, "failure", child.id, f"the engine did not take the email: {e}")
            return f"«{name}»: the engine did not take the email: {e}"
        s.exec(update(Run).where(Run.id == child.id).values(task_id=answer["task_id"]))
        s.commit()
        s.refresh(child)
        done = await orch._wait_run(s, child)
        ok = done.status == "SUCCESS"
        _audit_reports(s, user, rendered, "success" if ok else "failure", child.id, None if ok else done.error)
        return None if ok else f"«{name}»: {done.error or 'the email was not sent'}"


async def _cell_reports(s: Session, user: User, cfg, group: FlowGroup | None,
                        trigger: str) -> tuple[list[dict], set[int], str | None]:
    """Every report of a Pushmail cell as the engine renders it — its current version, its
    datasets resolved and its parameters converted with the rights of whoever runs the
    group, the placeholders in the report's time zone — the datasources they read, and why
    one cannot be sent (audited as a refused render), or None."""
    from app.models import Report
    from app.services import reports
    from app.services.report_params import to_json

    out: list[dict] = []
    read: set[int] = set()
    for r in cfg.reports:
        report = s.get(Report, r.reportId) if r.reportId is not None else None
        if report is None or not reports.can_see(s, user, report):
            return out, read, f"report {r.reportId} not found"
        definition = reports.content_of(s, report)
        detail = {"format": r.format, "from": "flow_group", "flow_group": group.id if group else None,
                  "trigger": trigger, "parameters": {}}
        try:
            given = reports.given_params(r.params)
            detail["parameters"] = reports.given_detail(given)
            values, datasets = await reports.prepare(s, user, definition, given, detail, read)
        except HTTPException as e:
            detail["error"] = reports._text(e)[:500]
            reports._audit_render(s, user, report, None, "failure", detail)
            return out, read, f"the report «{report.name}»: {reports._text(e)}"
        out.append({"id": report.id, "name": report.name, "definition": definition, "datasets": datasets,
                    "params": {k: to_json(v) for k, v in values.items()}, "format": r.format,
                    "file_name": r.fileName, "in_body": r.inBody, "locale": definition.get("locale") or "en",
                    "report": report, "detail": detail})
    return out, read, None


def _audit_reports(s: Session, user: User, rendered: list[dict], outcome: str, run_id: int, error: str | None) -> None:
    """`report.render` for every report of a cell, once the email's outcome is known: its
    render and its send are one task of the engine."""
    from app.services import reports

    for r in rendered:
        detail = {**r["detail"], "run_id": run_id}
        if error:
            detail["error"] = error[:500]
        reports._audit_render(s, user, r["report"], None, outcome, detail)


def _node_id(run: Run) -> str | None:
    try:
        return json.loads(run.script or "{}").get("node_id")
    except ValueError:
        return None


async def _wait_for(s: Session, run_id: int, live_id: int, name: str, cells: _Cells, step: int, col: int,
                    take_outcome: bool, engine_task: bool = False) -> str | None:
    """Waits for a run already in progress; with `take_outcome`, its outcome is the cell's.
    `engine_task`: a run whose state is the engine's task (a SQL script's write), brought
    up to date at every look; an orchestration's row is kept up to date by its process."""
    await cells.set(step, col, run_id=live_id, waited=take_outcome)
    from app.services.waits import waits

    for pause in waits(orch._orc().poll_interval_seconds):
        if engine_task:
            live = s.get(Run, live_id)
            if live is not None and live.status not in TERMINAL_STATES:
                await orch._reconcile(s, live)
        row = s.exec(select(Run.status, Run.error).where(Run.id == live_id)).first()
        if row is None:
            return f"'{name}': the run in progress it waited for is gone" if take_outcome else None
        status, error = row
        if status in TERMINAL_STATES:
            if not take_outcome or status == "SUCCESS":
                return None
            return f"'{name}': the run already in progress failed" + (f" ({error})" if error else "")
        orch._still_mine(s, run_id)  # stop waiting once the group is no longer ours
        s.commit()  # do not hold a pooled connection through the sleep
        await asyncio.sleep(pause)


async def _refresh_cell(run_id: int, user_id: int, trigger: str, ds_id: int, cells: _Cells, step: int, col: int) -> str | None:
    with Session(orch.engine) as s:
        ds = s.get(Datasource, ds_id)
        user = s.get(User, user_id)
        if ds is None:
            return f"datasource {ds_id} no longer exists"
        if user is None or not user.is_active:
            return USER_GONE
        if ds.kind != "database":
            return f"'{ds.name}' cannot be refreshed"
        name = ds.name
        try:
            await orch._refresh_and_wait(s, user, ds_id, trigger, parent_run_id=run_id)
        except orch.OrchestrationError as e:
            return f"'{name}': {e}"
        except HTTPException as e:  # the engine refused the import: say why
            return f"'{name}': {e.detail}"
        # the import that ran, or the one already running that it waited for
        last = s.exec(select(Run.id).where(Run.datasource_id == ds_id, Run.kind == "ingest")
                      .order_by(Run.started_at.desc())).first()
        await cells.set(step, col, run_id=last)
        return None


def _describe(e: Exception) -> str:
    detail = getattr(e, "detail", None)
    return str(detail) if detail else f"{type(e).__name__}: {e}"
