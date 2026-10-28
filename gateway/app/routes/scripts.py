"""Saved scripts: Python or R code kept as an object of a folder, with its versions.
docs/specs/saved-scripts.md; the rules in services/saved_scripts.py.

Permissions:
- seeing one (lists, search, opening it, its versions): VIEW on its folder **and** the
  use of its environment; whoever lacks either gets 404, as if it did not exist;
- creating, changing, moving, renaming, duplicating, deleting, restoring a version:
  EDIT on the folder (both folders for a move), and the use of the environment;
- running it: RUN on the folder; the run checks again, with the runner's authority,
  every input and the publish folder (services/saved_script_runs.py);
- administrators see and change every script.

On an installation without scripting (the Docker Compose stack) the lists are empty
and nothing can be saved.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import update as sa_update
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.core.engine_client import get_engine_client
from app.models import Datasource, Project, Run, SavedScript, SavedScriptVersion, ScriptEnvironment, User
from app.models.run import TERMINAL_STATES
from app.models.permission import Capability
from app.schemas.models import UtcDateTime
from app.services import audit, openlineage, orchestrator, saved_script_runs, saved_scripts, script_builds, script_nodes
from app.services import permissions as perm_service
from app.services.copy_names import commit_copy, free_copy_name
from app.services.schedule import ScheduleError, next_fire, validate_schedule

router = APIRouter(route_class=ReleasingRoute, tags=["scripts"])


class ScriptOut(BaseModel):
    id: int
    name: str
    description: str
    project_id: int
    environment_id: int
    environment_name: Optional[str]
    language: Optional[str]
    current_version: int
    run_schedule: Optional[str]
    run_scheduled_by: Optional[int] = None
    next_run_at: Optional[UtcDateTime]
    created_at: UtcDateTime
    updated_at: UtcDateTime
    editable: bool  # EDIT on its folder: the page offers saving, moving, deleting
    runnable: bool  # RUN on its folder: the page offers «Run»
    last_run_status: Optional[str] = None  # of its own runs (on its page, scheduled, in a group)
    last_run_at: Optional[UtcDateTime] = None


class InputInfo(BaseModel):
    """A declared input as this reader may know it: a datasource they cannot read, or
    that no longer exists, keeps only its alias."""
    datasource_id: int
    alias: str
    name: Optional[str] = None
    folder: Optional[str] = None
    project_id: Optional[int] = None
    columns: list[str] = []
    state: str  # "ok" | "missing" (deleted) | "hidden" (not readable by this reader) | "empty" (no data yet)


class ScriptDetail(ScriptOut):
    content: dict[str, Any]
    inputs: list[InputInfo] = []


class ScriptRunOut(BaseModel):
    """A run of the script: its orchestration row, with what its script run says."""
    run_id: int
    status: str
    trigger_type: str
    started_at: Optional[UtcDateTime]
    finished_at: Optional[UtcDateTime]
    error: Optional[str]
    launched_by: Optional[str]
    script_run_id: Optional[int] = None
    script_version: Optional[int] = None
    attempts: Optional[int] = None
    exit_code: Optional[int] = None
    log: str = ""
    log_expired: bool = False  # emptied after ORCHESTRATOR__LOG_RETENTION_DAYS
    rows: Optional[int] = None
    output: bool = False
    columns: list[dict] = []
    datasource_id: Optional[int] = None


class ScriptCreate(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    environment_id: int
    content: Optional[dict[str, Any]] = None


class ScriptUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    project_id: Optional[int] = None  # a move
    environment_id: Optional[int] = None
    content: Optional[dict[str, Any]] = None


class VersionOut(BaseModel):
    version: int
    note: str
    created_at: UtcDateTime
    created_by: Optional[str]
    current: bool


# ── helpers ───────────────────────────────────────────────────────────────────
def _on() -> bool:
    return script_builds.disabled_reason() is None


def _need_on() -> None:
    if not _on():
        raise HTTPException(status_code=409, detail="Scripts need the scripting environments (Kubernetes)")


def _environment(session: Session, user: User, environment_id: int) -> ScriptEnvironment:
    env = session.get(ScriptEnvironment, environment_id)
    if env is None or not script_nodes.can_use(session, user, env):
        raise HTTPException(status_code=422, detail="Choose a scripting environment you may use")
    return env


def _name(session: Session, project_id: int, name: str, exclude_id: Optional[int] = None) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="The script needs a name")
    stmt = select(SavedScript.id).where(SavedScript.project_id == project_id, SavedScript.name == name)
    clash = session.exec(stmt).first()
    if clash is not None and clash != exclude_id:
        raise HTTPException(status_code=409, detail=f"There is already a script '{name}' in this folder")
    return name


def _schedule_off(script: SavedScript, user: User, reasons: list[str]) -> Optional[dict]:
    """A schedule carries its author's authority: a change by someone else that makes
    the script reach more (other code, other inputs, another publish folder, another
    environment, another folder) switches it off. Returns the audit detail."""
    if not script.run_schedule or script.run_scheduled_by in (None, user.id) or not reasons:
        return None
    author = script.run_scheduled_by
    script.run_schedule = script.run_scheduled_by = script.next_run_at = None
    return {"turned_off": True, "reason": "; ".join(reasons), "author": author}


def _schedules_off_elsewhere(session: Session, user: User, script: SavedScript, reasons: list[str]) -> list[tuple]:
    """The flows and groups that run the script, scheduled by someone else: a change
    that makes the script reach more switches their schedules off too, as a change of
    their own would. Returns what to record once the change is committed."""
    if not reasons:
        return []
    flows, groups = saved_scripts.using(session, script.id)
    off = []
    for obj, action, kind in [(f, audit.FLOW_SCHEDULE, "flow") for f in flows] + \
                             [(g, audit.FLOW_GROUP_SCHEDULE, "flow_group") for g in groups]:
        if obj.run_schedule and obj.run_scheduled_by not in (None, user.id):
            detail = {"turned_off": True, "author": obj.run_scheduled_by,
                      "reason": f"the saved script '{script.name}' it runs changed: {'; '.join(reasons)}"}
            obj.run_schedule = obj.run_scheduled_by = obj.next_run_at = None
            session.add(obj)
            off.append((action, kind, obj.id, obj.name, detail))
    return off


def _record_schedules_off(session: Session, user: User, off: list[tuple], request) -> None:
    for action, kind, obj_id, name, detail in off:
        audit.record_audit(session, actor=user, action=action, target_type=kind, target_id=obj_id, target_label=name,
                           detail=detail, request=request)


def _record_schedule_off(session: Session, user: User, script: SavedScript, detail: Optional[dict], request) -> None:
    if detail:
        audit.record_audit(session, actor=user, action=audit.SCRIPT_SCHEDULE, target_type="saved_script",
                           target_id=script.id, target_label=script.name, detail=detail, request=request)


def _out(session: Session, user: User, s: SavedScript, envs: Optional[dict[int, ScriptEnvironment]] = None) -> ScriptOut:
    env = (envs or {}).get(s.environment_id) or session.get(ScriptEnvironment, s.environment_id)
    return ScriptOut(id=s.id, name=s.name, description=s.description, project_id=s.project_id,
                     environment_id=s.environment_id, environment_name=env.name if env else None,
                     language=env.language if env else None, current_version=s.current_version,
                     run_schedule=s.run_schedule, run_scheduled_by=s.run_scheduled_by, next_run_at=s.next_run_at,
                     created_at=s.created_at,
                     updated_at=s.updated_at,
                     editable=perm_service.has_capability(session, user, s.project_id, Capability.EDIT),
                     runnable=perm_service.has_capability(session, user, s.project_id, Capability.RUN))


def _many(session: Session, user: User, rows: list[SavedScript]) -> list[ScriptOut]:
    ids = {r.environment_id for r in rows}
    envs = {e.id: e for e in session.exec(select(ScriptEnvironment).where(ScriptEnvironment.id.in_(ids))).all()} if ids else {}  # type: ignore[attr-defined]
    out = [_out(session, user, r, envs) for r in rows]
    if rows:
        # the latest of each script's own runs: one row per script, from the index
        from sqlalchemy import func

        latest = select(func.max(Run.id)).where(Run.kind == "orchestration", Run.saved_script_id.in_(
            [r.id for r in rows])).group_by(Run.saved_script_id)  # type: ignore[union-attr]
        last = {r.saved_script_id: r for r in session.exec(select(Run).where(Run.id.in_(latest))).all()}  # type: ignore[union-attr]
        for item in out:
            run = last.get(item.id)
            if run is not None:
                item.last_run_status, item.last_run_at = run.status, run.started_at
    return out


def _inputs(session: Session, user: User, content: dict) -> list[InputInfo]:
    out = []
    for item in content.get("inputs") or []:
        ds_id, alias = item.get("datasource_id"), str(item.get("alias") or "")
        ds = session.get(Datasource, ds_id) if isinstance(ds_id, int) else None
        if ds is None:
            out.append(InputInfo(datasource_id=ds_id if isinstance(ds_id, int) else 0, alias=alias, state="missing"))
        elif not perm_service.has_capability(session, user, ds.project_id, Capability.VIEW):
            out.append(InputInfo(datasource_id=ds.id, alias=alias, state="hidden"))
        else:
            out.append(InputInfo(datasource_id=ds.id, alias=alias, name=ds.name, project_id=ds.project_id,
                                 folder=saved_script_runs.folder_path(session, ds.project_id),
                                 columns=saved_script_runs._columns(ds), state="ok" if ds.key else "empty"))
    return out


def _detail(session: Session, user: User, s: SavedScript) -> ScriptDetail:
    content = saved_scripts.content_of(session, s)
    return ScriptDetail(**_out(session, user, s).model_dump(), content=content, inputs=_inputs(session, user, content))


# ── lists ─────────────────────────────────────────────────────────────────────
@router.get("/scripts", response_model=list[ScriptOut])
def list_scripts(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Every script this person sees (the Scripting tab, the pickers of flows and groups)."""
    if not _on():
        return []
    stmt = saved_scripts.visible_query(session, user)
    return _many(session, user, list(session.exec(stmt).all())) if stmt is not None else []


@router.get("/projects/{project_id}/scripts", response_model=list[ScriptOut])
def list_project_scripts(project_id: int, user: User = Depends(get_current_user),
                         session: Session = Depends(get_session)):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.VIEW)
    if not _on():
        return []
    stmt = saved_scripts.visible_query(session, user, {project_id})
    return _many(session, user, list(session.exec(stmt).all())) if stmt is not None else []


@router.get("/scripts/{script_id}", response_model=ScriptDetail)
def get_script(script_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if not _on():
        raise HTTPException(status_code=404, detail="Script not found")
    return _detail(session, user, saved_scripts.get_visible(session, user, script_id))


# ── create, change, copy, delete ──────────────────────────────────────────────
@router.post("/projects/{project_id}/scripts", response_model=ScriptDetail, status_code=status.HTTP_201_CREATED)
def create_script(project_id: int, body: ScriptCreate, request: Request, user: User = Depends(get_current_user),
                  session: Session = Depends(get_session)):
    _need_on()
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.EDIT)
    env = _environment(session, user, body.environment_id)
    name = _name(session, project_id, body.name)
    content = saved_scripts.check_content(session, user, name, body.content or saved_scripts.empty_content(), env)
    script = SavedScript(name=name, description=body.description.strip(), project_id=project_id, owner_id=user.id,
                         environment_id=env.id, current_version=0)
    session.add(script)
    session.flush()
    saved_scripts.add_version(session, script, content, "created", user)
    session.commit()
    session.refresh(script)
    audit.record_audit(session, actor=user, action=audit.SCRIPT_CREATE, target_type="saved_script",
                       target_id=script.id, target_label=script.name, request=request,
                       detail={"project_id": project_id, "environment": env.name})
    return _detail(session, user, script)


@router.patch("/scripts/{script_id}", response_model=ScriptDetail)
def update_script(script_id: int, body: ScriptUpdate, request: Request, user: User = Depends(get_current_user),
                  session: Session = Depends(get_session)):
    """Name, description, folder (a move), environment, content. A change of the content
    is a new version, which becomes the one that runs."""
    _need_on()
    script = saved_scripts.get_visible(session, user, script_id)
    ensure_can(session, user, script.project_id, Capability.EDIT)
    lineage_names = openlineage.catalog_names(session, objects=[("saved_script", script)]) \
        if body.name is not None or body.project_id is not None else {}
    changes: dict[str, Any] = {}
    target = script.project_id
    if body.project_id is not None and body.project_id != script.project_id:
        if session.get(Project, body.project_id) is None:
            raise HTTPException(status_code=404, detail="Destination folder not found")
        ensure_can(session, user, body.project_id, Capability.EDIT)
        target = body.project_id
    name = _name(session, target, body.name if body.name is not None else script.name, exclude_id=script.id)
    if name != script.name:
        changes["name"] = {"from": script.name, "to": name}
    if target != script.project_id:
        changes["project_id"] = {"from": script.project_id, "to": target}
    env = session.get(ScriptEnvironment, script.environment_id)
    if body.environment_id is not None and body.environment_id != script.environment_id:
        env = _environment(session, user, body.environment_id)
        changes["environment"] = env.name
    if env is None:
        raise HTTPException(status_code=422, detail="Its scripting environment no longer exists: choose another")
    current = saved_scripts.content_of(session, script)
    new_content = None
    if body.content is not None or "environment" in changes:
        checked = saved_scripts.check_content(session, user, name, body.content if body.content is not None else current,
                                              env, previous=current)
        if checked != current:
            new_content = checked
    reasons = saved_scripts.widens(current, new_content) if new_content is not None else []
    if env.id != script.environment_id:
        reasons.append("it runs in another environment")
    if target != script.project_id:
        reasons.append("it was moved to another folder")
    schedule_off = _schedule_off(script, user, reasons)
    elsewhere = _schedules_off_elsewhere(session, user, script, [r for r in reasons if "moved" not in r])
    script.name, script.project_id, script.environment_id = name, target, env.id
    if body.description is not None and body.description.strip() != script.description:
        script.description = body.description.strip()
        changes["description"] = True
    if new_content is not None:
        row = saved_scripts.add_version(session, script, new_content, "edited", user)
        changes["version"] = row.version
    if not changes:
        return _detail(session, user, script)
    script.updated_at = datetime.now(timezone.utc)
    session.add(script)
    session.commit()
    session.refresh(script)
    openlineage.renamed(session, lineage_names)
    audit.record_audit(session, actor=user, action=audit.SCRIPT_UPDATE, target_type="saved_script",
                       target_id=script.id, target_label=script.name, request=request, detail=changes)
    _record_schedule_off(session, user, script, schedule_off, request)
    _record_schedules_off(session, user, elsewhere, request)
    return _detail(session, user, script)


@router.post("/scripts/{script_id}/duplicate", response_model=ScriptDetail, status_code=status.HTTP_201_CREATED)
def duplicate_script(script_id: int, request: Request, user: User = Depends(get_current_user),
                     session: Session = Depends(get_session)):
    """A copy in its folder, as «name (i)», starting from the version that runs."""
    _need_on()
    src = saved_scripts.get_visible(session, user, script_id)
    ensure_can(session, user, src.project_id, Capability.EDIT)
    copy = SavedScript(name=free_copy_name(session, SavedScript, src.project_id, src.name),
                       description=src.description, project_id=src.project_id, owner_id=user.id,
                       environment_id=src.environment_id, current_version=0)
    session.add(copy)
    session.flush()
    saved_scripts.add_version(session, copy, saved_scripts.content_of(session, src), "created", user)
    commit_copy(session)
    session.refresh(copy)
    audit.record_audit(session, actor=user, action=audit.SCRIPT_CREATE, target_type="saved_script",
                       target_id=copy.id, target_label=copy.name, request=request,
                       detail={"project_id": copy.project_id, "duplicate_of": src.id})
    return _detail(session, user, copy)


@router.delete("/scripts/{script_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_script(script_id: int, request: Request, user: User = Depends(get_current_user),
                  session: Session = Depends(get_session)):
    script = saved_scripts.get_visible(session, user, script_id)
    ensure_can(session, user, script.project_id, Capability.EDIT)
    # in line with «Run» and the scheduler: nothing is queued between the check and the delete
    saved_script_runs.in_line_for_the_script(session, script.id)
    live = session.exec(select(Run.id).where(Run.saved_script_id == script.id,
                                             Run.status.not_in(TERMINAL_STATES))).first()  # type: ignore[union-attr]
    if live is not None:
        raise HTTPException(status_code=409, detail="The script is queued or running: delete it once it has finished")
    used = saved_scripts.references(session, script.id)
    if any(used.values()):
        names = [f"flow «{n}»" for n in used["flows"]] + [f"flow group «{n}»" for n in used["groups"]]
        raise HTTPException(status_code=409, detail={
            "message": f"The script is used by {', '.join(names[:10])}{' …' if len(names) > 10 else ''}: "
                       "remove it from there first", **used})
    label = script.name
    # its runs stay in the history, no longer attached to it
    session.exec(sa_update(Run).where(Run.saved_script_id == script.id).values(saved_script_id=None))
    for row in session.exec(select(SavedScriptVersion).where(SavedScriptVersion.script_id == script.id)).all():
        session.delete(row)
    session.flush()
    session.delete(script)
    session.commit()
    audit.record_audit(session, actor=user, action=audit.SCRIPT_DELETE, target_type="saved_script",
                       target_id=script_id, target_label=label, request=request)


# ── versions ──────────────────────────────────────────────────────────────────
@router.get("/scripts/{script_id}/versions", response_model=list[VersionOut])
def list_versions(script_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    script = saved_scripts.get_visible(session, user, script_id)
    rows = session.exec(select(SavedScriptVersion).where(SavedScriptVersion.script_id == script.id)
                        .order_by(SavedScriptVersion.version.desc())).all()  # type: ignore[attr-defined]
    authors = {u.id: u.email for u in session.exec(select(User).where(
        User.id.in_({r.created_by for r in rows if r.created_by}))).all()} if rows else {}  # type: ignore[attr-defined]
    return [VersionOut(version=r.version, note=r.note, created_at=r.created_at, created_by=authors.get(r.created_by),
                       current=r.version == script.current_version) for r in rows]


@router.get("/scripts/{script_id}/versions/{version}", response_model=dict)
def get_version(script_id: int, version: int, user: User = Depends(get_current_user),
                session: Session = Depends(get_session)) -> dict:
    script = saved_scripts.get_visible(session, user, script_id)
    if saved_scripts.version_row(session, script, version) is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return saved_scripts.content_of(session, script, version)


@router.post("/scripts/{script_id}/versions/{version}/restore", response_model=ScriptDetail)
def restore_version(script_id: int, version: int, request: Request, user: User = Depends(get_current_user),
                    session: Session = Depends(get_session)):
    """An older version becomes the newest, and the one that runs. Its inputs and its
    publish folder are checked again for the person restoring it."""
    _need_on()
    script = saved_scripts.get_visible(session, user, script_id)
    ensure_can(session, user, script.project_id, Capability.EDIT)
    if saved_scripts.version_row(session, script, version) is None:
        raise HTTPException(status_code=404, detail="Version not found")
    if version == script.current_version:
        return _detail(session, user, script)
    env = session.get(ScriptEnvironment, script.environment_id)
    if env is None:
        raise HTTPException(status_code=422, detail="Its scripting environment no longer exists: choose another")
    content = saved_scripts.check_content(session, user, script.name, saved_scripts.content_of(session, script, version),
                                          env, previous=saved_scripts.content_of(session, script))
    widened = saved_scripts.widens(saved_scripts.content_of(session, script), content)
    schedule_off = _schedule_off(script, user, widened)
    elsewhere = _schedules_off_elsewhere(session, user, script, widened)
    row = saved_scripts.add_version(session, script, content, f"restored from v{version}", user)
    script.updated_at = datetime.now(timezone.utc)
    session.add(script)
    session.commit()
    session.refresh(script)
    audit.record_audit(session, actor=user, action=audit.SCRIPT_RESTORE, target_type="saved_script",
                       target_id=script.id, target_label=script.name, request=request,
                       detail={"from": version, "version": row.version})
    _record_schedule_off(session, user, script, schedule_off, request)
    _record_schedules_off(session, user, elsewhere, request)
    return _detail(session, user, script)


# ── running (RUN on the script's folder) ──────────────────────────────────────
_ALREADY_RUNNING = "The script is already queued or running: it can be run again once it has finished"


@router.post("/scripts/{script_id}/run", status_code=status.HTTP_202_ACCEPTED)
def run_script_now(script_id: int, request: Request, user: User = Depends(get_current_user),
                   session: Session = Depends(get_session)):
    """Queues a run of the current version, with the caller's authority: an
    orchestrator runs it on the whole data, in the scripts' namespace."""
    _need_on()
    script = saved_scripts.get_visible(session, user, script_id)
    ensure_can(session, user, script.project_id, Capability.RUN)
    if not saved_scripts.content_of(session, script).get("files"):
        raise HTTPException(status_code=422, detail="The script has no code yet: write it and save it first")
    saved_script_runs.in_line_for_the_script(session, script.id)  # check and queue in line, across replicas
    if session.exec(select(SavedScript.id).where(SavedScript.id == script.id)).first() is None:
        raise HTTPException(status_code=404, detail="Script not found")  # deleted while waiting in line
    if saved_script_runs.script_running(session, script.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_ALREADY_RUNNING)
    run = saved_script_runs.create_script_run(session, user, script)
    orchestrator.wake()
    audit.record_audit(session, actor=user, action=audit.SCRIPT_RUN, target_type="saved_script", target_id=script.id,
                       target_label=script.name, detail={"run_id": run.id, "trigger": "manual",
                                                         "version": script.current_version}, request=request)
    return {"status": "started", "script_id": script.id, "run_id": run.id}


@router.get("/scripts/{script_id}/runs", response_model=list[ScriptRunOut])
def list_script_runs(script_id: int, limit: int = Query(20, ge=1, le=100), user: User = Depends(get_current_user),
                     session: Session = Depends(get_session)):
    """The script's latest runs, newest first."""
    script = saved_scripts.get_visible(session, user, script_id)
    heads = session.exec(select(Run).where(Run.saved_script_id == script.id, Run.kind == "orchestration")
                         .order_by(Run.started_at.desc(), Run.id.desc()).limit(limit)).all()  # type: ignore[attr-defined]
    if not heads:
        return []
    children = {r.parent_run_id: r for r in session.exec(select(Run).where(
        Run.parent_run_id.in_([h.id for h in heads]), Run.kind == "script")).all()}  # type: ignore[union-attr]
    names = {u.id: u.email for u in session.exec(select(User).where(
        User.id.in_({h.launched_by for h in heads if h.launched_by}))).all()}  # type: ignore[attr-defined]
    out = []
    for h in heads:
        item = ScriptRunOut(run_id=h.id, status=h.status, trigger_type=h.trigger_type, started_at=h.started_at,
                            finished_at=h.finished_at, error=h.error, launched_by=names.get(h.launched_by))
        child = children.get(h.id)
        if child is not None:
            try:
                info = json.loads(child.script or "{}")
            except ValueError:
                info = {}
            item.script_run_id, item.script_version = child.id, info.get("script_version")
            item.attempts, item.exit_code, item.log = info.get("attempts"), info.get("exit_code"), info.get("log") or ""
            item.log_expired = bool(info.get("log_expired"))
            item.rows, item.columns, item.datasource_id = child.rows_written, info.get("columns") or [], child.datasource_id
            item.output = bool(child.status == "SUCCESS" and child.output_key)
        out.append(item)
    return out


@router.get("/scripts/{script_id}/runs/{run_id}/preview")
async def preview_script_output(script_id: int, run_id: int, limit: int = Query(100, ge=1, le=1000),
                                user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """The first rows of what a run of the script wrote at TABULARIA_OUTPUT."""
    script = saved_scripts.get_visible(session, user, script_id)
    child = session.exec(select(Run).where(Run.parent_run_id == run_id, Run.kind == "script",
                                           Run.saved_script_id == script.id)).first()
    if child is None or child.status != "SUCCESS" or not child.output_key:
        raise HTTPException(status_code=404, detail="This run left no output")
    resp = await get_engine_client().post("/tasks/preview", json={
        "bucket": child.output_bucket, "input_key": child.output_key, "operations": [], "limit": limit,
        "no_cache": True}, timeout=120)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text
        raise HTTPException(status_code=502, detail=f"The output could not be read: {str(detail)[:500]}")
    return resp.json()


class ScheduleUpdate(BaseModel):
    cron: Optional[str] = None  # five fields; empty or None clears it


@router.put("/scripts/{script_id}/schedule", response_model=ScriptDetail)
def set_script_schedule(script_id: int, body: ScheduleUpdate, request: Request, user: User = Depends(get_current_user),
                        session: Session = Depends(get_session)):
    """Sets (5-field cron) or clears the schedule. Scheduled runs use the authority of
    whoever sets it, checked again every time the schedule fires."""
    _need_on()
    script = saved_scripts.get_visible(session, user, script_id)
    ensure_can(session, user, script.project_id, Capability.RUN)
    cron = (body.cron or "").strip()
    if not cron:
        script.run_schedule = script.run_scheduled_by = script.next_run_at = None
    else:
        if not saved_scripts.content_of(session, script).get("files"):
            raise HTTPException(status_code=422, detail="The script has no code yet: write it before scheduling it")
        try:
            cron = validate_schedule(cron)
        except ScheduleError as e:
            raise HTTPException(status_code=422, detail=str(e))
        script.run_schedule, script.run_scheduled_by = cron, user.id
        script.next_run_at = next_fire(cron, datetime.now(timezone.utc)).replace(tzinfo=None)
    script.updated_at = datetime.now(timezone.utc)
    session.add(script)
    session.commit()
    session.refresh(script)
    audit.record_audit(session, actor=user, action=audit.SCRIPT_SCHEDULE, target_type="saved_script",
                       target_id=script.id, target_label=script.name, detail={"cron": script.run_schedule},
                       request=request)
    return _detail(session, user, script)
