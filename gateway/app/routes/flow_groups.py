"""Flow groups in folders: a grid of steps, each step a set of flows to run and
datasources to refresh in parallel (see models/flow_group.py).

Permissions, as for flows:
- list and open: VIEW on the group's folder;
- create, change, delete: EDIT; moving needs EDIT on both folders;
- run now and set the schedule: RUN; read the runs: VIEW.

The cells a definition names must exist, be visible to whoever adds them and be
runnable by them (RUN on their folder), so a group never runs something its editor
could not. Cells added by someone other than the schedule's author switch the
schedule off (services/schedule_authority.py). A cell already in the
group stays accepted when someone with less sight edits it: their save must not
drop what others put there. Every cell is checked again, with the rights of whoever
runs the group, when it runs.
"""
import json
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import delete as sa_delete, update as sa_update
from sqlmodel import Session, select

from app.core.lingua import lingua_della_richiesta
from app.core.routing import RottaCheRilascia
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Datasource, Flow, FlowGroup, Project, Run, SavedScript, User
from app.models.permission import Capability
from app.models.run import TERMINAL_STATES
from app.schemas.models import (
    FlowGroupCellLabel,
    FlowGroupCreate,
    FlowGroupDefinition,
    FlowGroupDetail,
    FlowGroupOut,
    FlowGroupRunOut,
    FlowGroupCell,
    FlowGroupScheduleUpdate,
    FlowGroupUpdate,
)
from app.services import audit, orchestrator
from app.services import permissions as perm_service
from app.services.copy_names import commit_copy, free_copy_name
from app.services import flow_access, saved_scripts
from app.services.flow_groups import definition_of
from app.services.schedule import ScheduleError, next_fire, validate_schedule

router = APIRouter(route_class=RottaCheRilascia, tags=["flow-groups"])


def _get_group(session: Session, group_id: int) -> FlowGroup:
    group = session.get(FlowGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Flow group not found")
    return group


def _name_taken(session: Session, project_id: int, name: str, exclude_id: int | None = None) -> bool:
    stmt = select(FlowGroup).where(FlowGroup.project_id == project_id, FlowGroup.name == name)
    if exclude_id is not None:
        stmt = stmt.where(FlowGroup.id != exclude_id)
    return session.exec(stmt).first() is not None


def _cells(definition: FlowGroupDefinition):
    for row in definition.steps:
        for cell in row:
            if cell is not None:
                yield cell


_KIND_NAME = {"flow": "flow", "refresh": "datasource", "script": "saved script", "container": "container"}
_OBJECT_KINDS = ("flow", "refresh", "script")  # cells naming an object; a container cell holds its settings


def _container_node(cell: FlowGroupCell) -> dict:
    """A container cell as the Container node it runs like (services/container_nodes.py)."""
    return {"id": f"container-cell-{cell.id}", "type": "container", "data": dict(cell.container or {})}


def _cell_key(cell: FlowGroupCell) -> tuple:
    """What a cell runs: a container cell's settings count, not only its number."""
    if cell.kind == "container":
        from app.services import container_nodes

        return (cell.kind, cell.id, container_nodes.fingerprint(_container_node(cell)))
    return (cell.kind, cell.id)


def _container_blocked(session: Session, user: User, definition: FlowGroupDefinition) -> Optional[str]:
    """Why this person may not open a group with these container cells, as for a flow
    with Container nodes (services/registry_access.py `blocked`)."""
    import json as _json

    from app.services import registry_access

    nodes = [_container_node(c) for c in _cells(definition) if c.kind == "container"]
    if not nodes:
        return None
    return registry_access.blocked(session, user, _json.dumps({"nodes": nodes}))


def _objects(session: Session, definition: FlowGroupDefinition) -> dict[str, dict[int, Any]]:
    """The flows, datasources and saved scripts a definition names, by kind of cell,
    read with one query per kind."""
    ids = {k: {c.id for c in _cells(definition) if c.kind == k} for k in _OBJECT_KINDS}
    model = {"flow": Flow, "refresh": Datasource, "script": SavedScript}
    return {k: ({o.id: o for o in session.exec(select(model[k]).where(model[k].id.in_(ids[k]))).all()} if ids[k] else {})
            for k in _OBJECT_KINDS}


def _sees(session: Session, user: User, kind: str, obj, readable: set[int]) -> bool:
    """A saved script is seen by whoever may also use its environment: for anyone
    else it does not exist (docs/specs/saved-scripts.md)."""
    if kind == "script":
        return saved_scripts.can_see(session, user, obj)
    return obj.project_id in readable


def _checked(session: Session, user: User, definition: FlowGroupDefinition,
             before: FlowGroupDefinition | None = None) -> FlowGroupDefinition:
    readable = perm_service.readable_project_ids(session, user)
    objects = _objects(session, definition)
    already = {_cell_key(c) for c in _cells(before)} if before is not None else set()
    errors: list[str] = []
    for n, row in enumerate(definition.steps, 1):
        in_step: set[tuple[str, int]] = set()
        for cell in row:
            if cell is None:
                continue
            key = (cell.kind, cell.id)
            if key in in_step:
                # two runs of the same flow, or two refreshes of one datasource, at once
                errors.append(f"step {n}: the same {_KIND_NAME[cell.kind]} twice")
                continue
            in_step.add(key)
            if _cell_key(cell) in already:
                continue
            if cell.kind == "container":
                errors.extend(f"step {n}: {e}" for e in _container_errors(session, user, cell))
                continue
            obj = objects[cell.kind].get(cell.id)
            # not found and not visible read the same: a group does not reveal what exists
            if obj is None or not _sees(session, user, cell.kind, obj, readable):
                errors.append(f"step {n}: {_KIND_NAME[cell.kind]} {cell.id} not found")
            elif cell.kind == "refresh" and obj.kind != "database":
                errors.append(f"step {n}: '{obj.name}' is published by a flow, it cannot be refreshed")
            elif not perm_service.has_capability(session, user, obj.project_id, Capability.RUN):
                # a cell is something the group RUNS: seeing it is not enough to put it
                # in a group that someone's schedule will run
                errors.append(f"step {n}: adding '{obj.name}' needs RUN on its folder")
            elif cell.kind == "flow" and flow_access.blocked(session, user, obj.definition):
                # it would run, on this person's authority, images they may not use
                errors.append(f"step {n}: '{obj.name}' runs images or scripts you have no access to")
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors[:10]))
    return definition


def _container_errors(session: Session, user: User, cell: FlowGroupCell) -> list[str]:
    """A container cell, new or changed: well formed, its registry in use, its image one
    this person may run."""
    from app.models import ContainerRegistry
    from app.services import container_nodes, registry_access

    node = _container_node(cell)
    try:
        container_nodes.check(node, complete=True)
    except HTTPException as e:
        return [str(e.detail)]
    d = node["data"]
    registry = session.get(ContainerRegistry, d.get("registryId")) if isinstance(d.get("registryId"), int) else None
    if registry is None or not registry.enabled:
        return [f"container «{container_nodes.label(node)}»: choose a registry that is in use"]
    if not perm_service.is_admin(session, user) and (
            not registry_access.can_use(session, user, registry)
            or not registry_access.repository_allowed(session, user, registry, str(d.get("repository") or ""))):
        return [f"container «{container_nodes.label(node)}»: the image is not among those of «{registry.name}» you may use"]
    return []


def _labels(session: Session, user: User, definition: FlowGroupDefinition) -> list[list[Optional[FlowGroupCellLabel]]]:
    readable = perm_service.readable_project_ids(session, user)
    objects = _objects(session, definition)
    out: list[list[Optional[FlowGroupCellLabel]]] = []
    for row in definition.steps:
        labels: list[Optional[FlowGroupCellLabel]] = []
        for cell in row:
            if cell is None:
                labels.append(None)
                continue
            if cell.kind == "container":
                d = cell.container or {}
                image = f"{d.get('repository') or '?'}:{(d.get('tag') or 'latest')}"
                labels.append(FlowGroupCellLabel(name=str(d.get("label") or image)))
                continue
            obj = objects[cell.kind].get(cell.id)
            if obj is None:
                labels.append(FlowGroupCellLabel(missing=True))
            elif not _sees(session, user, cell.kind, obj, readable):
                labels.append(FlowGroupCellLabel(hidden=True))
            else:
                labels.append(FlowGroupCellLabel(name=obj.name, project_id=obj.project_id))
        out.append(labels)
    return out


def _to_out(group: FlowGroup) -> FlowGroupOut:
    fields = {f: getattr(group, f) for f in FlowGroupOut.model_fields if f != "definition"}
    return FlowGroupOut(**fields, definition=definition_of(group))


def _detail(session: Session, user: User, group: FlowGroup) -> FlowGroupDetail:
    out = _to_out(group)
    return FlowGroupDetail(
        **out.model_dump(), cells=_labels(session, user, out.definition),
        can_edit=perm_service.has_capability(session, user, group.project_id, Capability.EDIT),
        can_run=perm_service.has_capability(session, user, group.project_id, Capability.RUN),
    )


def _cell_count(definition: FlowGroupDefinition) -> int:
    return sum(1 for _ in _cells(definition))


@router.get("/flow-groups", response_model=list[FlowGroupOut])
def list_flow_groups(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return []
    groups = session.exec(select(FlowGroup).where(FlowGroup.project_id.in_(readable)).order_by(FlowGroup.name)).all()
    return [_to_out(g) for g in groups]


@router.get("/projects/{project_id}/flow-groups", response_model=list[FlowGroupOut])
def list_project_flow_groups(project_id: int, user: User = Depends(get_current_user),
                             session: Session = Depends(get_session)):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.VIEW)
    groups = session.exec(select(FlowGroup).where(FlowGroup.project_id == project_id).order_by(FlowGroup.name)).all()
    return [_to_out(g) for g in groups]


@router.get("/flow-groups/{group_id}", response_model=FlowGroupDetail)
def get_flow_group(group_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    group = _get_group(session, group_id)
    ensure_can(session, user, group.project_id, Capability.VIEW)
    why = _container_blocked(session, user, definition_of(group))
    if why:
        raise HTTPException(status_code=403, detail=why.replace("This flow", "This flow group"))
    return _detail(session, user, group)


@router.post("/projects/{project_id}/flow-groups", response_model=FlowGroupDetail, status_code=status.HTTP_201_CREATED)
def create_flow_group(project_id: int, body: FlowGroupCreate, request: Request,
                      user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.EDIT)
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="The flow group needs a name")
    if _name_taken(session, project_id, name):
        raise HTTPException(status_code=409, detail=f"There is already a flow group '{name}' in this folder")
    definition = _checked(session, user, body.definition)
    group = FlowGroup(name=name, description=body.description.strip(), project_id=project_id, owner_id=user.id,
                      definition=definition.model_dump_json())
    session.add(group)
    session.commit()
    session.refresh(group)
    audit.record_audit(
        session, actor=user, action=audit.FLOW_GROUP_CREATE, target_type="flow_group",
        target_id=group.id, target_label=group.name,
        detail={"project_id": project_id, "steps": len(definition.steps), "cells": _cell_count(definition)},
        request=request,
    )
    return _detail(session, user, group)


@router.post("/flow-groups/{group_id}/duplicate", response_model=FlowGroupDetail, status_code=status.HTTP_201_CREATED)
def duplicate_flow_group(group_id: int, request: Request,
                         user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """A copy of the group in its folder, as «name (i)». Its cells are checked as for
    a new group, with the rights of whoever makes the copy; the schedule stays with
    the original."""
    src = _get_group(session, group_id)
    ensure_can(session, user, src.project_id, Capability.EDIT)
    definition = _checked(session, user, definition_of(src))
    group = FlowGroup(name=free_copy_name(session, FlowGroup, src.project_id, src.name), description=src.description,
                      project_id=src.project_id, owner_id=user.id, definition=definition.model_dump_json())
    session.add(group)
    commit_copy(session)
    session.refresh(group)
    audit.record_audit(
        session, actor=user, action=audit.FLOW_GROUP_CREATE, target_type="flow_group",
        target_id=group.id, target_label=group.name,
        detail={"project_id": group.project_id, "steps": len(definition.steps), "cells": _cell_count(definition),
                "duplicate_of": src.id},
        request=request,
    )
    return _detail(session, user, group)


@router.patch("/flow-groups/{group_id}", response_model=FlowGroupDetail)
def update_flow_group(group_id: int, body: FlowGroupUpdate, request: Request,
                      user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    group = _get_group(session, group_id)
    ensure_can(session, user, group.project_id, Capability.EDIT)
    detail: dict = {}
    schedule_off: dict | None = None

    target = group.project_id
    if body.project_id is not None and body.project_id != group.project_id:
        if session.get(Project, body.project_id) is None:
            raise HTTPException(status_code=404, detail="Destination folder not found")
        ensure_can(session, user, body.project_id, Capability.EDIT)
        target = body.project_id
        detail["moved_to"] = target
        # a schedule carries the authority of whoever set it, valid in the old folder
        # only: it is switched off, and someone with rights in the new one sets it again
        if group.run_schedule and group.run_scheduled_by != user.id:
            schedule_off = {"turned_off": True, "reason": "the flow group was moved to another folder",
                            "author": group.run_scheduled_by}
            group.run_schedule = None
            group.run_scheduled_by = None
            group.next_run_at = None
            detail["schedule_cleared"] = True

    name = body.name.strip() if body.name is not None else group.name
    if not name:
        raise HTTPException(status_code=422, detail="The flow group needs a name")
    if _name_taken(session, target, name, exclude_id=group.id):
        raise HTTPException(status_code=409, detail=f"There is already a flow group '{name}' in this folder")

    if body.definition is not None:
        before = definition_of(group)
        definition = _checked(session, user, body.definition, before=before)
        new_cells = {_cell_key(c) for c in _cells(definition)} - {_cell_key(c) for c in _cells(before)}
        if new_cells and group.run_schedule and group.run_scheduled_by != user.id:
            # the schedule would run cells its author never chose, with the author's rights
            schedule_off = {"turned_off": True, "author": group.run_scheduled_by,
                            "reason": f"someone else added {len(new_cells)} cells"}
            group.run_schedule = None
            group.run_scheduled_by = None
            group.next_run_at = None
            detail["schedule_cleared"] = True
        group.definition = definition.model_dump_json()
        detail.update(steps=len(definition.steps), cells=_cell_count(definition))
    group.name = name
    group.project_id = target
    if body.description is not None:
        group.description = body.description.strip()
    group.updated_at = datetime.now(timezone.utc)
    session.add(group)
    session.commit()
    session.refresh(group)
    audit.record_audit(
        session, actor=user, action=audit.FLOW_GROUP_UPDATE, target_type="flow_group",
        target_id=group.id, target_label=group.name, detail=detail or None, request=request,
    )
    if schedule_off:
        audit.record_audit(
            session, actor=user, action=audit.FLOW_GROUP_SCHEDULE, target_type="flow_group",
            target_id=group.id, target_label=group.name, detail=schedule_off, request=request,
        )
    session.refresh(group)
    return _detail(session, user, group)


@router.delete("/flow-groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_flow_group(group_id: int, request: Request,
                      user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    group = _get_group(session, group_id)
    ensure_can(session, user, group.project_id, Capability.EDIT)
    # in line with run-now and the scheduler: nothing is queued between the check and the delete
    orchestrator.in_line_for_the_group(session, group.id)
    live = session.exec(
        select(Run.id).where(Run.flow_group_id == group_id, Run.status.not_in(TERMINAL_STATES))
    ).first()
    if live is not None:
        raise HTTPException(status_code=409, detail="The flow group is queued or running: delete it once it has finished")
    label = group.name
    # the runs of its cells belong to their flows and datasources: they stay, no
    # longer attached to the group's runs, which go with the group
    group_runs = select(Run.id).where(Run.flow_group_id == group_id)
    session.exec(sa_update(Run).where(Run.parent_run_id.in_(group_runs)).values(parent_run_id=None))
    session.exec(sa_delete(Run).where(Run.flow_group_id == group_id))
    session.delete(group)
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.FLOW_GROUP_DELETE, target_type="flow_group",
        target_id=group_id, target_label=label, request=request,
    )


# ── running and scheduling (RUN on the group's folder) ───────────────────────
_ALREADY_RUNNING = {
    "en": "The flow group is already queued or running: it can be run again once it has finished.",
    "it": "Il gruppo di flussi è già in coda o in esecuzione: si potrà rilanciare quando avrà finito.",
    "de": "Die Flow-Gruppe ist bereits in der Warteschlange oder läuft: Sie kann erneut gestartet werden, wenn sie fertig ist.",
    "es": "El grupo de flujos ya está en cola o en ejecución: se podrá lanzar de nuevo cuando termine.",
    "fr": "Le groupe de flux est déjà en file d'attente ou en cours : il pourra être relancé une fois terminé.",
}


def _has_cells(group: FlowGroup) -> bool:
    return any(True for _ in _cells(definition_of(group)))


@router.post("/flow-groups/{group_id}/run-now", status_code=status.HTTP_202_ACCEPTED)
def run_flow_group_now(group_id: int, request: Request,
                       user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Queues a run of the group, with the caller's authority; every cell checks
    its own rights again when it runs."""
    group = _get_group(session, group_id)
    ensure_can(session, user, group.project_id, Capability.RUN)
    if not _has_cells(group):
        raise HTTPException(status_code=422, detail="The flow group is empty: add a flow or a datasource first")
    orchestrator.in_line_for_the_group(session, group.id)  # check and queue in line, across replicas
    if session.exec(select(FlowGroup.id).where(FlowGroup.id == group.id)).first() is None:
        raise HTTPException(status_code=404, detail="Flow group not found")  # deleted while waiting in line
    if orchestrator.group_running(session, group.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail=_ALREADY_RUNNING.get(lingua_della_richiesta(request), _ALREADY_RUNNING["en"]))
    run = orchestrator.create_group_run(session, user, group)
    orchestrator.sveglia()
    audit.record_audit(
        session, actor=user, action=audit.FLOW_GROUP_RUN, target_type="flow_group",
        target_id=group.id, target_label=group.name, detail={"run_id": run.id, "trigger": "manual"}, request=request,
    )
    return {"status": "started", "flow_group_id": group.id, "run_id": run.id}


@router.put("/flow-groups/{group_id}/schedule", response_model=FlowGroupDetail)
def set_flow_group_schedule(group_id: int, body: FlowGroupScheduleUpdate, request: Request,
                            user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Sets (5-field cron) or clears the schedule. Scheduled runs use the authority
    of whoever sets it, checked again every time the schedule fires."""
    group = _get_group(session, group_id)
    ensure_can(session, user, group.project_id, Capability.RUN)
    cron = (body.cron or "").strip()
    if not cron:
        group.run_schedule = group.run_scheduled_by = group.next_run_at = None
    else:
        if not _has_cells(group):
            raise HTTPException(status_code=422, detail="The flow group is empty: add a flow or a datasource before scheduling it")
        try:
            cron = validate_schedule(cron)
        except ScheduleError as e:
            raise HTTPException(status_code=422, detail=str(e))
        group.run_schedule = cron
        group.run_scheduled_by = user.id
        group.next_run_at = next_fire(cron, datetime.now(timezone.utc)).replace(tzinfo=None)
    group.updated_at = datetime.now(timezone.utc)
    session.add(group)
    session.commit()
    session.refresh(group)
    audit.record_audit(
        session, actor=user, action=audit.FLOW_GROUP_SCHEDULE, target_type="flow_group",
        target_id=group.id, target_label=group.name, detail={"cron": group.run_schedule}, request=request,
    )
    return _detail(session, user, group)


def _runs_out(session: Session, user: User, runs: list[Run]) -> list[FlowGroupRunOut]:
    """The runs as the reader may see them. A cell's error names its flow or
    datasource and repeats what the engine said: for a cell whose object lives in a
    folder the reader cannot read, or no longer exists (then nobody can tell where it
    lived), only the state is shown, as its label is."""
    grids: list[list] = []
    for run in runs:
        try:
            grid = json.loads(run.group_cells) if run.group_cells else []
        except ValueError:
            grid = []
        grids.append(grid if isinstance(grid, list) else [])
    named = [c for grid in grids for row in grid if isinstance(row, list) for c in row if isinstance(c, dict)]
    script_ids = {c.get("id") for c in named if c.get("kind") == "script"}
    scripts = {x.id: x for x in session.exec(select(SavedScript).where(SavedScript.id.in_(script_ids))).all()} if script_ids else {}
    flow_ids = {c.get("id") for c in named if c.get("kind") == "flow"}
    ds_ids = {c.get("id") for c in named if c.get("kind") == "refresh"}
    folder_of = {
        ("flow", i): p for i, p in session.exec(select(Flow.id, Flow.project_id).where(Flow.id.in_(flow_ids))).all()
    } if flow_ids else {}
    if ds_ids:
        folder_of.update({("refresh", i): p for i, p in
                          session.exec(select(Datasource.id, Datasource.project_id).where(Datasource.id.in_(ds_ids))).all()})
    readable = perm_service.readable_project_ids(session, user)
    for cell in named:
        if cell.get("kind") == "container":
            continue  # its settings are the group's: whoever reads the group reads its outcome
        if cell.get("kind") == "script":
            script = scripts.get(cell.get("id"))
            if script is None or not saved_scripts.can_see(session, user, script):
                cell["error"] = None
                cell["run_id"] = None
            continue
        folder = folder_of.get((cell.get("kind"), cell.get("id")))
        if folder not in readable:
            cell["error"] = None
            cell["run_id"] = None
    return [
        FlowGroupRunOut(
            id=run.id, status=run.status, trigger_type=run.trigger_type, launched_by=run.launched_by,
            error=run.error, started_at=run.started_at, finished_at=run.finished_at, cells=grid,
        )
        for run, grid in zip(runs, grids)
    ]


@router.get("/flow-groups/{group_id}/runs", response_model=list[FlowGroupRunOut])
def list_flow_group_runs(group_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """The latest runs of the group, newest first."""
    group = _get_group(session, group_id)
    ensure_can(session, user, group.project_id, Capability.VIEW)
    runs = session.exec(
        select(Run).where(Run.flow_group_id == group_id).order_by(Run.id.desc()).limit(50)
    ).all()
    return _runs_out(session, user, list(runs))


@router.get("/flow-groups/{group_id}/runs/{run_id}", response_model=FlowGroupRunOut)
def get_flow_group_run(group_id: int, run_id: int,
                       user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    group = _get_group(session, group_id)
    ensure_can(session, user, group.project_id, Capability.VIEW)
    run = session.get(Run, run_id)
    if run is None or run.flow_group_id != group_id:
        raise HTTPException(status_code=404, detail="Run not found")
    return _runs_out(session, user, [run])[0]
