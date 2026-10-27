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
- administrators see and change every script.

Unlike saved Python/R scripts, they need no Kubernetes: every installation has them.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Project, SqlScript, SqlScriptVersion, User
from app.models.permission import Capability
from app.schemas.models import UtcDateTime
from app.services import audit, sql_scripts
from app.services import permissions as perm_service
from app.services.copy_names import commit_copy, free_copy_name

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
    created_at: UtcDateTime
    updated_at: UtcDateTime
    editable: bool  # EDIT on its folder: the page offers saving, moving, deleting


class SqlScriptDetail(SqlScriptOut):
    content: dict[str, Any]


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
    themselves. (Its name is shown to the script's readers, to say why they cannot run it.)"""
    checked = sql_scripts.check_content(content)
    cid = checked["connection_id"]
    if cid is not None and cid != (current or {}).get("connection_id"):
        sql_scripts.usable_connection(session, user, cid)
    return checked


def _out(session: Session, user: User, s: SqlScript, content: Optional[dict] = None) -> SqlScriptOut:
    content = content if content is not None else sql_scripts.content_of(session, s)
    return SqlScriptOut(id=s.id, name=s.name, description=s.description, project_id=s.project_id,
                        current_version=s.current_version, engine=content.get("engine"),
                        connection=_connection_ref(session, user, content.get("connection_id")),
                        created_at=s.created_at, updated_at=s.updated_at,
                        editable=perm_service.has_capability(session, user, s.project_id, Capability.EDIT))


def _detail(session: Session, user: User, s: SqlScript) -> SqlScriptDetail:
    content = sql_scripts.content_of(session, s)
    return SqlScriptDetail(**_out(session, user, s, content).model_dump(), content=content)


def _many(session: Session, user: User, stmt) -> list[SqlScriptOut]:
    return [_out(session, user, s) for s in session.exec(stmt).all()] if stmt is not None else []


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

    from app.services.saved_script_runs import folder_path

    folders = sql_scripts.folder_names(session)
    paths: dict[int, str] = {}
    out = []
    for ds in sql_scripts.readable_datasources(session, user):
        if ds.project_id not in paths:
            paths[ds.project_id] = folder_path(session, ds.project_id)
        try:
            cols = [CatalogColumn(name=str(c.get("name", "")), type=str(c.get("dtype", "")))
                    for c in json.loads(ds.columns or "[]") if isinstance(c, dict)]
        except ValueError:
            cols = []
        out.append(CatalogItem(id=ds.id, name=ds.name, folder=folders.get(ds.project_id, ""), path=paths[ds.project_id],
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
    if body.content is not None:
        current = sql_scripts.content_of(session, script)
        checked = _checked(session, user, body.content, current)
        if checked != current:
            changes["version"] = sql_scripts.add_version(session, script, checked, "edited", user).version
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
    audit.record_audit(session, actor=user, action=audit.SQL_SCRIPT_UPDATE, target_type="sql_script",
                       target_id=script.id, target_label=script.name, request=request, detail=changes)
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
    label = script.name
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
    content = sql_scripts.check_content(sql_scripts.content_of(session, script, version))
    row = sql_scripts.add_version(session, script, content, f"restored from v{version}", user)
    script.updated_at = datetime.now(timezone.utc)
    session.add(script)
    session.commit()
    session.refresh(script)
    audit.record_audit(session, actor=user, action=audit.SQL_SCRIPT_RESTORE, target_type="sql_script",
                       target_id=script.id, target_label=script.name, request=request,
                       detail={"from": version, "version": row.version})
    return _detail(session, user, script)
