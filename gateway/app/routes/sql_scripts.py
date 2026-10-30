"""SQL scripts: SQL kept as an object of a folder, with its versions, and run on the
catalog. docs/specs/sql-scripts.md; the rules in services/sql_scripts.py.

Permissions:
- seeing one (lists, search, opening it, its versions) and running it: VIEW on its
  folder; whoever lacks it gets 404, as if it did not exist;
- creating, changing, moving, renaming, duplicating, deleting, restoring a version:
  EDIT on the folder (both folders for a move);
- a run reads with the authority of whoever runs it (services/sql_scripts.resolve);
  on a database connection it needs CONNECT on the connection's folder, and so does
  choosing that connection for a script;
- writing the script's datasource («Refresh now») and scheduling it: RUN on the
  folder, EDIT on the datasource's folder, and what a run needs
  (services/sql_script_runs); choosing that folder needs EDIT on it;
- administrators see and change every script.

Unlike saved Python/R scripts, they need no Kubernetes: every installation has them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import update as sa_update
from sqlmodel import Session, select

from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Datasource, Project, Run, SqlScript, SqlScriptVersion, User
from app.models.permission import Capability
from app.models.run import TERMINAL_STATES
from app.schemas.models import UtcDateTime
from app.services import audit, openlineage, sql_script_runs, sql_scripts
from app.services import permissions as perm_service
from app.services.copy_names import commit_copy, free_copy_name
from app.services.schedule import ScheduleError, next_fire, validate_schedule

router = APIRouter(route_class=ReleasingRoute, tags=["sql-scripts"])


class ConnectionRef(BaseModel):
    id: int
    name: str  # empty when the connection no longer exists
    db_type: str = ""
    usable: bool  # CONNECT on its folder: this person can run the script


class SqlScriptOut(BaseModel):
    id: int
    name: str
    description: str
    project_id: int
    current_version: int
    engine: Optional[str] = None  # of the current version
    connection: Optional[ConnectionRef] = None  # of the current version; null = the catalog
    output: Optional[dict[str, Any]] = None  # the datasource it writes ({name, project_id}); null = none
    created_at: UtcDateTime
    updated_at: UtcDateTime
    editable: bool  # EDIT on its folder: the page offers saving, moving, deleting
    runnable: bool  # RUN on its folder: the page offers writing its datasource and scheduling it
    run_schedule: Optional[str] = None
    run_scheduled_by: Optional[int] = None
    next_run_at: Optional[UtcDateTime] = None


class DatasourceRef(BaseModel):
    """The datasource the script writes, once it exists, for whoever may read it."""
    id: int
    name: str
    project_id: int
    rows: Optional[int] = None
    updated_at: Optional[UtcDateTime] = None


class SqlScriptDetail(SqlScriptOut):
    content: dict[str, Any]
    datasource: Optional[DatasourceRef] = None


class SqlScriptRunOut(BaseModel):
    """A write of the script's datasource."""
    run_id: int
    status: str
    trigger_type: str
    started_at: Optional[UtcDateTime]
    finished_at: Optional[UtcDateTime]
    error: Optional[str]
    launched_by: Optional[str]
    rows: Optional[int] = None
    datasource_id: Optional[int] = None
    parent_run_id: Optional[int] = None  # a flow group's run, when a cell wrote it


class ScheduleUpdate(BaseModel):
    cron: Optional[str] = None  # five fields; empty or None clears it


class SqlScriptCreate(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    content: Optional[dict[str, Any]] = None


class SqlScriptUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    project_id: Optional[int] = None  # a move
    content: Optional[dict[str, Any]] = None


class VersionOut(BaseModel):
    version: int
    note: str
    created_at: UtcDateTime
    created_by: Optional[str]
    current: bool


class RunRequest(BaseModel):
    sql: str = Field(max_length=sql_scripts.MAX_SQL_CHARS)
    engine: Optional[str] = Field(default=None, max_length=40)
    limit: int = Field(default=sql_scripts.DEFAULT_ROWS, ge=1, le=sql_scripts.MAX_ROWS)
    # the saved script being run, for the audit and the preview slot; null = a draft
    script_id: Optional[int] = None
    connection_id: Optional[int] = None  # null = the catalog


class ExportRequest(BaseModel):
    sql: str = Field(max_length=sql_scripts.MAX_SQL_CHARS)
    engine: Optional[str] = Field(default=None, max_length=40)
    format: Literal["csv", "xlsx"] = "csv"
    script_id: Optional[int] = None
    connection_id: Optional[int] = None


class CatalogColumn(BaseModel):
    name: str
    type: str = ""


class CatalogItem(BaseModel):
    id: int
    name: str
    folder: str  # the folder's own name: how the SQL qualifies a name two folders share
    path: str  # the whole path, to tell them apart in the list
    rows: Optional[int] = None
    columns: list[CatalogColumn] = []
    has_data: bool


# ── helpers ───────────────────────────────────────────────────────────────────
def _name(session: Session, project_id: int, name: str, exclude_id: Optional[int] = None) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="The SQL script needs a name")
    clash = session.exec(select(SqlScript.id).where(SqlScript.project_id == project_id, SqlScript.name == name)).first()
    if clash is not None and clash != exclude_id:
        raise HTTPException(status_code=409, detail=f"There is already a SQL script '{name}' in this folder")
    return name


def _connection_ref(session: Session, user: User, connection_id: Optional[int]) -> Optional[ConnectionRef]:
    if connection_id is None:
        return None
    conn = sql_scripts.queryable_connection(session, connection_id)
    if conn is None:
        return ConnectionRef(id=connection_id, name="", usable=False)
    return ConnectionRef(id=conn.id, name=conn.name, db_type=conn.db_type,
                         usable=perm_service.has_capability(session, user, conn.project_id, Capability.CONNECT))


def _checked(session: Session, user: User, content: Any, current: Optional[dict] = None) -> dict:
    """The content to save. A connection the script did not have already must exist and
    be one this person may use: nobody points a script at a database they could not query
    themselves. (Its name is shown to the script's readers, to say why they cannot run it.)
    The same for a datasource it did not write already: EDIT on its folder, and not a
    database's or a file's snapshot under that name."""
    checked = sql_scripts.check_content(content)
    cid = checked["connection_id"]
    if cid is not None and cid != (current or {}).get("connection_id"):
        sql_scripts.usable_connection(session, user, cid)
    output = checked["output"]
    if output is not None and output != (current or {}).get("output"):
        sql_script_runs.check_output(session, user, output)
    return checked


def _not_in_groups(session: Session, script: SqlScript, message: str) -> None:
    """409 when flow groups run the script: `message` says what they stop."""
    groups = [g.name for g in sql_scripts.groups_using(session, script.id)]
    if groups:
        names = ", ".join(f"«{n}»" for n in groups[:10]) + (" …" if len(groups) > 10 else "")
        raise HTTPException(status_code=409, detail={"message": message.format(names=names), "flows": [],
                                                     "groups": groups})


def _keeps_its_datasource(session: Session, script: SqlScript, current: dict, content: dict) -> None:
    """A script that a flow group's cell writes keeps a datasource: without one the cell
    stops the group at its step."""
    if current.get("output") and not content.get("output"):
        _not_in_groups(session, script, "The flow groups {names} write the script's datasource: "
                                         "remove it from there first")


def _schedule_off(script: SqlScript, user: User, reasons: list[str]) -> Optional[dict]:
    """A schedule carries its author's authority: a change by someone else that makes the
    script read or write more switches it off. Returns the audit detail."""
    if not script.run_schedule or script.run_scheduled_by in (None, user.id) or not reasons:
        return None
    author = script.run_scheduled_by
    script.run_schedule = script.run_scheduled_by = script.next_run_at = None
    return {"turned_off": True, "reason": "; ".join(reasons), "author": author}


def _groups_off(session: Session, user: User, script: SqlScript, reasons: list[str]) -> list[tuple]:
    """The flow groups that run the script, or send a report that reads it, scheduled by
    someone else: a change that makes the script reach more switches their schedules off
    too, as a change of their own would."""
    from app.services import reports

    if not reasons:
        return []
    why = "; ".join(reasons)
    affected = [(g, f"the SQL script '{script.name}' it runs changed: {why}")
                for g in sql_scripts.groups_using(session, script.id)]
    affected += [(g, f"the SQL script '{script.name}' its report '{r.name}' reads changed: {why}")
                 for r, g in reports.groups_reading(session, "sql_script", script.id)]
    return reports.turn_off_groups(session, user, affected)


def _record_off(session: Session, user: User, script: SqlScript, mine: Optional[dict], groups: list[tuple],
                request) -> None:
    if mine:
        audit.record_audit(session, actor=user, action=audit.SQL_SCRIPT_SCHEDULE, target_type="sql_script",
                           target_id=script.id, target_label=script.name, detail=mine, request=request)
    for group_id, name, detail in groups:
        audit.record_audit(session, actor=user, action=audit.FLOW_GROUP_SCHEDULE, target_type="flow_group",
                           target_id=group_id, target_label=name, detail=detail, request=request)


def _out(session: Session, user: User, s: SqlScript, content: Optional[dict] = None) -> SqlScriptOut:
    content = content if content is not None else sql_scripts.content_of(session, s)
    return SqlScriptOut(id=s.id, name=s.name, description=s.description, project_id=s.project_id,
                        current_version=s.current_version, engine=content.get("engine"),
                        connection=_connection_ref(session, user, content.get("connection_id")),
                        output=sql_script_runs.output_of(content),
                        created_at=s.created_at, updated_at=s.updated_at,
                        editable=perm_service.has_capability(session, user, s.project_id, Capability.EDIT),
                        runnable=perm_service.has_capability(session, user, s.project_id, Capability.RUN),
                        run_schedule=s.run_schedule, run_scheduled_by=s.run_scheduled_by, next_run_at=s.next_run_at)


def _detail(session: Session, user: User, s: SqlScript) -> SqlScriptDetail:
    content = sql_scripts.content_of(session, s)
    output = sql_script_runs.output_of(content)
    target = sql_script_runs.target_of(session, output) if output is not None else None
    ref = None
    if target is not None and perm_service.has_capability(session, user, target.project_id, Capability.VIEW):
        ref = DatasourceRef(id=target.id, name=target.name, project_id=target.project_id, rows=target.rows,
                            updated_at=target.updated_at)
    return SqlScriptDetail(**_out(session, user, s, content).model_dump(), content=content, datasource=ref)


def _many(session: Session, user: User, stmt) -> list[SqlScriptOut]:
    if stmt is None:
        return []
    scripts = list(session.exec(stmt).all())
    contents = sql_scripts.current_contents(session, scripts)
    return [_out(session, user, s, contents[s.id]) for s in scripts]


# ── lists ─────────────────────────────────────────────────────────────────────
@router.get("/sql-scripts", response_model=list[SqlScriptOut])
def list_sql_scripts(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Every SQL script this person sees."""
    return _many(session, user, sql_scripts.visible_query(session, user))


@router.get("/projects/{project_id}/sql-scripts", response_model=list[SqlScriptOut])
def list_project_sql_scripts(project_id: int, user: User = Depends(get_current_user),
                             session: Session = Depends(get_session)):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.VIEW)
    return _many(session, user, sql_scripts.visible_query(session, user, {project_id}))


@router.get("/sql-scripts/catalog", response_model=list[CatalogItem])
def sql_catalog(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """The datasources this person may query, with their columns: the editor's list and
    its completion."""
    import json

    folders = sql_scripts.folder_names(session)
    paths = sql_scripts.folder_paths(session)
    out = []
    for ds in sql_scripts.readable_datasources(session, user):
        try:
            cols = [CatalogColumn(name=str(c.get("name", "")), type=str(c.get("dtype", "")))
                    for c in json.loads(ds.columns or "[]") if isinstance(c, dict)]
        except ValueError:
            cols = []
        out.append(CatalogItem(id=ds.id, name=ds.name, folder=folders.get(ds.project_id, ""), path=paths.get(ds.project_id, ""),
                               rows=ds.rows, columns=cols, has_data=bool(ds.key)))
    return out


@router.get("/sql-scripts/{script_id}", response_model=SqlScriptDetail)
def get_sql_script(script_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    return _detail(session, user, sql_scripts.get_visible(session, user, script_id))


# ── run ───────────────────────────────────────────────────────────────────────
@router.post("/sql-scripts/run")
async def run_sql(body: RunRequest, request: Request, user: User = Depends(get_current_user),
                  session: Session = Depends(get_session)) -> dict[str, Any]:
    """Runs the SQL in the editor (saved or not) on the catalog or on a connection: the
    first rows of the result. With `script_id` the run is recorded against that script,
    which the person must be able to see."""
    script = sql_scripts.get_visible(session, user, body.script_id) if body.script_id is not None else None
    return await sql_scripts.run(session, user, body.sql, body.engine, body.limit, script=script, request=request,
                                 connection_id=body.connection_id)


@router.post("/sql-scripts/export")
async def export_sql(body: ExportRequest, request: Request, user: User = Depends(get_current_user),
                     session: Session = Depends(get_session)) -> dict[str, Any]:
    """Starts the export of the whole result of the SQL in the editor as csv or xlsx; the
    person follows it and takes the file through /tasks/exports, as the Viewer's export."""
    script = sql_scripts.get_visible(session, user, body.script_id) if body.script_id is not None else None
    return await sql_scripts.export(session, user, body.sql, body.engine, body.format, script=script, request=request,
                                    connection_id=body.connection_id)


# ── create, change, copy, delete ──────────────────────────────────────────────
@router.post("/projects/{project_id}/sql-scripts", response_model=SqlScriptDetail, status_code=status.HTTP_201_CREATED)
def create_sql_script(project_id: int, body: SqlScriptCreate, request: Request,
                      user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.EDIT)
    name = _name(session, project_id, body.name)
    content = _checked(session, user, body.content if body.content is not None else sql_scripts.empty_content())
    script = SqlScript(name=name, description=body.description.strip(), project_id=project_id, owner_id=user.id,
                       current_version=0)
    session.add(script)
    session.flush()
    sql_scripts.add_version(session, script, content, "created", user)
    session.commit()
    session.refresh(script)
    audit.record_audit(session, actor=user, action=audit.SQL_SCRIPT_CREATE, target_type="sql_script",
                       target_id=script.id, target_label=script.name, request=request,
                       detail={"project_id": project_id})
    return _detail(session, user, script)


@router.patch("/sql-scripts/{script_id}", response_model=SqlScriptDetail)
def update_sql_script(script_id: int, body: SqlScriptUpdate, request: Request,
                      user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Name, description, folder (a move), content. A change of the content is a new
    version, which becomes the current one."""
    script = sql_scripts.get_visible(session, user, script_id)
    ensure_can(session, user, script.project_id, Capability.EDIT)
    lineage_names = openlineage.catalog_names(session, objects=[("sql_script", script)]) \
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
    reasons: list[str] = []
    if body.content is not None:
        current = sql_scripts.content_of(session, script)
        # what the request does not name stays as it is (null names it: «no datasource»)
        checked = _checked(session, user, {**current, **body.content}, current)
        _keeps_its_datasource(session, script, current, checked)
        if checked != current:
            reasons = sql_scripts.widens(current, checked)
            changes["version"] = sql_scripts.add_version(session, script, checked, "edited", user).version
    groups_off = _groups_off(session, user, script, reasons)
    if target != script.project_id:
        reasons.append("it was moved to another folder")
    schedule_off = _schedule_off(script, user, reasons)
    script.name, script.project_id = name, target
    if body.description is not None and body.description.strip() != script.description:
        script.description = body.description.strip()
        changes["description"] = True
    if not changes:
        return _detail(session, user, script)
    script.updated_at = datetime.now(timezone.utc)
    session.add(script)
    session.commit()
    session.refresh(script)
    openlineage.renamed(session, lineage_names)
    audit.record_audit(session, actor=user, action=audit.SQL_SCRIPT_UPDATE, target_type="sql_script",
                       target_id=script.id, target_label=script.name, request=request, detail=changes)
    _record_off(session, user, script, schedule_off, groups_off, request)
    return _detail(session, user, script)


@router.post("/sql-scripts/{script_id}/duplicate", response_model=SqlScriptDetail, status_code=status.HTTP_201_CREATED)
def duplicate_sql_script(script_id: int, request: Request, user: User = Depends(get_current_user),
                         session: Session = Depends(get_session)):
    """A copy in its folder, as «name (i)», from the current version."""
    src = sql_scripts.get_visible(session, user, script_id)
    ensure_can(session, user, src.project_id, Capability.EDIT)
    copy = SqlScript(name=free_copy_name(session, SqlScript, src.project_id, src.name), description=src.description,
                     project_id=src.project_id, owner_id=user.id, current_version=0)
    session.add(copy)
    session.flush()
    sql_scripts.add_version(session, copy, sql_scripts.content_of(session, src), "created", user)
    commit_copy(session)
    session.refresh(copy)
    audit.record_audit(session, actor=user, action=audit.SQL_SCRIPT_CREATE, target_type="sql_script",
                       target_id=copy.id, target_label=copy.name, request=request,
                       detail={"project_id": copy.project_id, "duplicate_of": src.id})
    return _detail(session, user, copy)


@router.delete("/sql-scripts/{script_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_sql_script(script_id: int, request: Request, user: User = Depends(get_current_user),
                      session: Session = Depends(get_session)):
    script = sql_scripts.get_visible(session, user, script_id)
    ensure_can(session, user, script.project_id, Capability.EDIT)
    # in line with «Refresh now» and the scheduler: nothing starts between the check and the delete
    sql_script_runs.in_line_for_the_script(session, script.id)
    if sql_script_runs.live_run(session, script.id) is not None:
        raise HTTPException(status_code=409, detail="The script is writing its datasource: delete it once it has finished")
    _not_in_groups(session, script, "The script is used by the flow groups {names}: remove it from there first")
    from app.services import reports

    reports.not_read_by_reports(session, user, "sql_script", script.id, "The script")
    label = script.name
    # its runs stay in the history, no longer attached to it; its datasource stays
    session.exec(sa_update(Run).where(Run.sql_script_id == script.id).values(sql_script_id=None))
    for row in session.exec(select(SqlScriptVersion).where(SqlScriptVersion.script_id == script.id)).all():
        session.delete(row)
    session.flush()
    session.delete(script)
    session.commit()
    audit.record_audit(session, actor=user, action=audit.SQL_SCRIPT_DELETE, target_type="sql_script",
                       target_id=script_id, target_label=label, request=request)


# ── versions ──────────────────────────────────────────────────────────────────
@router.get("/sql-scripts/{script_id}/versions", response_model=list[VersionOut])
def list_versions(script_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    script = sql_scripts.get_visible(session, user, script_id)
    rows = session.exec(select(SqlScriptVersion).where(SqlScriptVersion.script_id == script.id)
                        .order_by(SqlScriptVersion.version.desc())).all()  # type: ignore[attr-defined]
    authors = {u.id: u.email for u in session.exec(select(User).where(
        User.id.in_({r.created_by for r in rows if r.created_by}))).all()} if rows else {}  # type: ignore[attr-defined]
    return [VersionOut(version=r.version, note=r.note, created_at=r.created_at, created_by=authors.get(r.created_by),
                       current=r.version == script.current_version) for r in rows]


@router.get("/sql-scripts/{script_id}/versions/{version}", response_model=dict)
def get_version(script_id: int, version: int, user: User = Depends(get_current_user),
                session: Session = Depends(get_session)) -> dict:
    script = sql_scripts.get_visible(session, user, script_id)
    if sql_scripts.version_row(session, script, version) is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return sql_scripts.content_of(session, script, version)


@router.post("/sql-scripts/{script_id}/versions/{version}/restore", response_model=SqlScriptDetail)
def restore_version(script_id: int, version: int, request: Request, user: User = Depends(get_current_user),
                    session: Session = Depends(get_session)):
    """An older version becomes the newest, and the current one."""
    script = sql_scripts.get_visible(session, user, script_id)
    ensure_can(session, user, script.project_id, Capability.EDIT)
    if sql_scripts.version_row(session, script, version) is None:
        raise HTTPException(status_code=404, detail="Version not found")
    if version == script.current_version:
        return _detail(session, user, script)
    current = sql_scripts.content_of(session, script)
    content = _checked(session, user, sql_scripts.content_of(session, script, version), current)
    _keeps_its_datasource(session, script, current, content)
    reasons = sql_scripts.widens(current, content)
    groups_off = _groups_off(session, user, script, reasons)
    schedule_off = _schedule_off(script, user, reasons)
    row = sql_scripts.add_version(session, script, content, f"restored from v{version}", user)
    script.updated_at = datetime.now(timezone.utc)
    session.add(script)
    session.commit()
    session.refresh(script)
    audit.record_audit(session, actor=user, action=audit.SQL_SCRIPT_RESTORE, target_type="sql_script",
                       target_id=script.id, target_label=script.name, request=request,
                       detail={"from": version, "version": row.version})
    _record_off(session, user, script, schedule_off, groups_off, request)
    return _detail(session, user, script)


# ── its datasource: writing it now, its runs, its schedule (RUN on the folder) ─────
@router.post("/sql-scripts/{script_id}/refresh", response_model=SqlScriptRunOut, status_code=status.HTTP_202_ACCEPTED)
async def refresh_sql_script(script_id: int, request: Request, user: User = Depends(get_current_user),
                             session: Session = Depends(get_session)):
    """Writes the script's whole result into its datasource, from its current version and
    with the caller's authority. The run is followed through /sql-scripts/{id}/runs."""
    script = sql_scripts.get_visible(session, user, script_id)
    ensure_can(session, user, script.project_id, Capability.RUN)
    run = await sql_script_runs.launch(session, user, script, "manual", request=request)
    return _run_out(run, user.email)


def _run_out(run: Run, launched_by: Optional[str], sees_datasource: bool = True) -> SqlScriptRunOut:
    """`sees_datasource`: VIEW on the folder of the datasource it wrote. Who lacks it reads
    the script's writes without the datasource's id and rows, as the script's page shows
    it no link."""
    return SqlScriptRunOut(run_id=run.id, status=run.status, trigger_type=run.trigger_type, started_at=run.started_at,
                           finished_at=run.finished_at, error=run.error, launched_by=launched_by,
                           rows=run.rows_written if sees_datasource else None,
                           datasource_id=run.datasource_id if sees_datasource else None, parent_run_id=run.parent_run_id)


@router.get("/sql-scripts/{script_id}/runs", response_model=list[SqlScriptRunOut])
async def list_sql_script_runs(script_id: int, limit: int = Query(10, ge=1, le=100),
                               user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """The latest writes of the script's datasource, newest first; one still going is
    brought up to date with the engine (and published when it has succeeded)."""
    from app.routes.runs import _reconcile

    script = sql_scripts.get_visible(session, user, script_id)
    runs = list(session.exec(select(Run).where(Run.sql_script_id == script.id, Run.kind == "sql")
                             .order_by(Run.id.desc()).limit(limit)).all())  # type: ignore[union-attr]
    for i, r in enumerate(runs):
        if r.status not in TERMINAL_STATES:
            runs[i] = await _reconcile(session, r)
    names = {u.id: u.email for u in session.exec(select(User).where(
        User.id.in_({r.launched_by for r in runs if r.launched_by}))).all()} if runs else {}  # type: ignore[attr-defined]
    folder_of = {d.id: d.project_id for d in session.exec(select(Datasource).where(
        Datasource.id.in_({r.datasource_id for r in runs if r.datasource_id}))).all()} if runs else {}  # type: ignore[attr-defined]

    def sees(r: Run) -> bool:
        folder = folder_of.get(r.datasource_id)
        return folder is not None and perm_service.has_capability(session, user, folder, Capability.VIEW)

    return [_run_out(r, names.get(r.launched_by), sees(r)) for r in runs]


@router.put("/sql-scripts/{script_id}/schedule", response_model=SqlScriptDetail)
async def set_sql_script_schedule(script_id: int, body: ScheduleUpdate, request: Request,
                                  user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Sets (5-field cron) or clears the schedule that writes the script's datasource.
    Scheduled writes use the authority of whoever sets it, checked again every time."""
    script = sql_scripts.get_visible(session, user, script_id)
    ensure_can(session, user, script.project_id, Capability.RUN)
    cron = (body.cron or "").strip()
    if not cron:
        script.run_schedule = script.run_scheduled_by = script.next_run_at = None
    else:
        try:
            cron = validate_schedule(cron)
        except ScheduleError as e:
            raise HTTPException(status_code=422, detail=str(e))
        missing = sql_script_runs.gaps(session, user, script)
        if missing:
            raise HTTPException(status_code=422, detail=f"The schedule would not write: {missing[0]}")
        script.run_schedule, script.run_scheduled_by = cron, user.id
        script.next_run_at = next_fire(cron, datetime.now(timezone.utc)).replace(tzinfo=None)
    script.updated_at = datetime.now(timezone.utc)
    session.add(script)
    session.commit()
    session.refresh(script)
    audit.record_audit(session, actor=user, action=audit.SQL_SCRIPT_SCHEDULE, target_type="sql_script",
                       target_id=script.id, target_label=script.name, detail={"cron": script.run_schedule},
                       request=request)
    return _detail(session, user, script)
