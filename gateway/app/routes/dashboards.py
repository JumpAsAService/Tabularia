"""Dashboards: a screen of live tiles on a grid, kept in a folder with its versions.
docs/specs/dashboards.md; the rules in services/dashboards.py.

Permissions:
- seeing one (lists, search, opening it, its versions): VIEW on its folder; whoever lacks it
  gets 404, as if it did not exist; the viewer notes every opening, in the audit and in the
  count of who reads it;
- creating, changing, moving, renaming, duplicating, deleting, restoring a version, and the
  check of a definition not saved yet: EDIT on the folder (both folders for a move); a
  definition saved must point only to what its author may read;
- its numbers (POST /dashboards/{id}/data, on the engine's preview queue) and its parameters:
  VIEW on the folder, read with the caller's rights at every request, dataset by dataset: a
  tile whose dataset they may not read says so inside the tile, the others work (an API token
  of either level reads them as it reads a report; a click and a refresh are not audited, they
  would drown the log);
- the numbers of a definition not saved yet (POST /dashboards/preview): EDIT on the folder;
- administrators see and change every dashboard.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, delete, select

from app.core.config import get_settings
from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Dashboard, DashboardVersion, DashboardView, Project, User
from app.models.permission import Capability
from app.routes.reports import CheckOut, ParameterOut, ParametersRequest, VersionOut, _parameters_out, _query_params
from app.schemas.models import UtcDateTime
from app.services import audit, dashboards, reports
from app.services import permissions as perm_service
from app.services.copy_names import commit_copy, free_copy_name
from app.services.masking import mask_email
from app.services.report_definition import Problem

router = APIRouter(route_class=ReleasingRoute, tags=["dashboards"])


class DashboardOut(BaseModel):
    id: int
    name: str
    description: str
    project_id: int
    current_version: int
    datasets: int  # of the current version
    parameters: int
    tiles: int
    created_at: UtcDateTime
    updated_at: UtcDateTime
    editable: bool  # EDIT on its folder: the page offers saving, moving, deleting


class DashboardDetail(DashboardOut):
    content: dict[str, Any]


class DashboardCreate(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    content: Optional[dict[str, Any]] = None


class DashboardUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    project_id: Optional[int] = None  # a move
    content: Optional[dict[str, Any]] = None
    # the version the new content was written on: refused (409) when another became the
    # current one since, which the new content would undo without anyone seeing it
    base_version: Optional[int] = None


class CheckRequest(BaseModel):
    """A definition being edited, of a saved dashboard (`dashboard_id`) or of a new one in a
    folder (`project_id`)."""
    definition: dict[str, Any]
    dashboard_id: Optional[int] = None
    project_id: Optional[int] = None


class DataRequest(BaseModel):
    """What to read: the parameters' values, as for a report; `tiles` only some (none: every
    one); `page` a table's page by tile, from 0; `state` the on-screen view, as a report's
    (a column sorted, groups toggled). `clicks` waits for the click's filter, and `refresh`
    for the cache: there is nothing yet for it to go around."""
    params: dict[str, Any] = Field(default_factory=dict)
    clicks: list[Any] = Field(default_factory=list, max_length=dashboards.MAX_TILES)
    tiles: Optional[list[str]] = Field(default=None, max_length=dashboards.MAX_TILES)
    page: dict[str, int] = Field(default_factory=dict, max_length=dashboards.MAX_TILES)
    state: Optional[dict[str, Any]] = None
    refresh: bool = False


class PreviewRequest(DataRequest):
    """The numbers of a definition being edited, of a saved dashboard (`dashboard_id`) or of a
    new one in a folder (`project_id`)."""
    definition: dict[str, Any]
    dashboard_id: Optional[int] = None
    project_id: Optional[int] = None


class TilesOut(BaseModel):
    """docs/specs/dashboards.md, «The answer, tile by tile»."""
    tiles: dict[str, dict[str, Any]]
    truncated: dict[str, bool]  # of the datasets read: past DASHBOARDS__MAX_ROWS


# ── helpers ───────────────────────────────────────────────────────────────────
def _name(session: Session, project_id: int, name: str, exclude_id: Optional[int] = None) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="The dashboard needs a name")
    clash = session.exec(select(Dashboard.id).where(Dashboard.project_id == project_id, Dashboard.name == name)).first()
    if clash is not None and clash != exclude_id:
        raise _taken(name)
    return name


def _taken(name: str) -> HTTPException:
    return HTTPException(status_code=409, detail=f"There is already a dashboard '{name}' in this folder")


def _commit_named(session: Session, name: str) -> None:
    """Two saves at the same moment may give a folder the same name twice: the folder's unique
    name refuses the second one, which gets the 409 of a name taken (the Explore then tries the
    next «New dashboard (n)») instead of a server error."""
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise _taken(name) from None


def _being_edited(session: Session, user: User, dashboard_id: Optional[int], project_id: Optional[int]) -> Optional[Dashboard]:
    """A definition being edited is of a saved dashboard, or of a new one in a folder: EDIT on
    that folder. The saved dashboard, or None."""
    dashboard = dashboards.get_visible(session, user, dashboard_id) if dashboard_id is not None else None
    folder = dashboard.project_id if dashboard is not None else project_id
    if folder is None or session.get(Project, folder) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, folder, Capability.EDIT)
    return dashboard


def _asked(body: DataRequest) -> dashboards.Asked:
    return dashboards.Asked(given=reports.given_params(body.params), tiles=body.tiles, pages=body.page, state=body.state,
                            clicks=body.clicks)


def _out(session: Session, user: User, d: Dashboard, datasets: int, parameters: int, tiles: int) -> DashboardOut:
    return DashboardOut(id=d.id, name=d.name, description=d.description, project_id=d.project_id,
                        current_version=d.current_version, datasets=datasets, parameters=parameters, tiles=tiles,
                        created_at=d.created_at, updated_at=d.updated_at,
                        editable=perm_service.has_capability(session, user, d.project_id, Capability.EDIT))


def _detail(session: Session, user: User, d: Dashboard) -> DashboardDetail:
    content = dashboards.content_of(session, d)
    out = _out(session, user, d, len(content.get("datasets") or []), len(content.get("parameters") or []),
               len(content.get("tiles") or []))
    return DashboardDetail(**out.model_dump(), content=content)


def _many(session: Session, user: User, stmt) -> list[DashboardOut]:
    if stmt is None:
        return []
    found = list(session.exec(stmt).all())
    sizes = dashboards.sizes_of(session, [d.id for d in found])
    return [_out(session, user, d, *sizes.get(d.id, (0, 0, 0))) for d in found]


def _record(session: Session, user: User, action: str, dashboard: Dashboard, request: Optional[Request],
            detail: Optional[dict] = None, dashboard_id: Optional[int] = None, label: Optional[str] = None) -> None:
    audit.record_audit(session, actor=user, action=action, target_type="dashboard",
                       target_id=dashboard_id if dashboard_id is not None else dashboard.id,
                       target_label=label if label is not None else dashboard.name, detail=detail, request=request)


# ── lists ─────────────────────────────────────────────────────────────────────
@router.get("/dashboards", response_model=list[DashboardOut])
def list_dashboards(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Every dashboard this person sees."""
    return _many(session, user, dashboards.visible_query(session, user))


@router.get("/projects/{project_id}/dashboards", response_model=list[DashboardOut])
def list_project_dashboards(project_id: int, user: User = Depends(get_current_user),
                            session: Session = Depends(get_session)):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.VIEW)
    return _many(session, user, dashboards.visible_query(session, user, {project_id}))


# NB: registered BEFORE /dashboards/{dashboard_id} routes of the same method, so that "check"
# is not taken for an id
@router.post("/dashboards/check", response_model=CheckOut)
async def check_dashboard(body: CheckRequest, user: User = Depends(get_current_user),
                          session: Session = Depends(get_session)):
    """The problems of a definition being edited, as a save would say them, without saving it.
    EDIT on the dashboard's folder, or on the folder a new one goes in."""
    dashboard = _being_edited(session, user, body.dashboard_id, body.project_id)
    current = dashboards.content_of(session, dashboard) if dashboard is not None else None
    return CheckOut(problems=(await dashboards.review(session, user, body.definition, current)).problems)


@router.post("/dashboards/preview", response_model=TilesOut)
async def preview_dashboard(body: PreviewRequest, user: User = Depends(get_current_user),
                            session: Session = Depends(get_session)):
    """The numbers of a definition not saved yet: EDIT on the dashboard's folder, or on the folder
    a new one goes in. Checked first, as a save is: problems are a 422 that lists them by tile
    and property. A dataset it reads as the saved dashboard does and this person may not read
    refuses in its tiles; one it now points to must be theirs to read, as at a save."""
    dashboard = _being_edited(session, user, body.dashboard_id, body.project_id)
    current = dashboards.content_of(session, dashboard) if dashboard is not None else None
    looked = await dashboards.review(session, user, body.definition, current)
    if looked.problems:
        raise looked.refusal()
    where = f"preview-{dashboard.id if dashboard is not None else 'new'}"
    title = dashboard.name if dashboard is not None else ""
    return await dashboards.numbers(session, user, looked.definition, _asked(body), where, title)


@router.get("/dashboards/{dashboard_id}", response_model=DashboardDetail)
def get_dashboard(dashboard_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    return _detail(session, user, dashboards.get_visible(session, user, dashboard_id))


@router.post("/dashboards/{dashboard_id}/views", status_code=status.HTTP_204_NO_CONTENT)
def dashboard_opened(dashboard_id: int, request: Request, user: User = Depends(get_current_user),
                     session: Session = Depends(get_session)):
    """The viewer opened the dashboard: in the audit every time, and a person once a day in the
    count of who reads it. A call with an API token counts nothing: a script is not a reader."""
    dashboard = dashboards.get_visible(session, user, dashboard_id)
    if getattr(request.state, "api_token", None) is None:
        dashboards.record_open(session, dashboard, user)
        _record(session, user, audit.DASHBOARD_OPEN, dashboard, request)


# ── its numbers and its parameters ────────────────────────────────────────────
@router.post("/dashboards/{dashboard_id}/data", response_model=TilesOut)
async def dashboard_data(dashboard_id: int, body: DataRequest, user: User = Depends(get_current_user),
                         session: Session = Depends(get_session)):
    """The numbers of the current version, ready to draw, tile by tile, read with the caller's
    rights. 400 naming the parameter whose value does not fit. An error of a tile, a dataset it
    may not read included, stays in that tile."""
    dashboard = dashboards.get_visible(session, user, dashboard_id)
    definition = dashboards.content_of(session, dashboard)
    return await dashboards.numbers(session, user, definition, _asked(body), str(dashboard.id), dashboard.name)


@router.get("/dashboards/{dashboard_id}/parameters", response_model=list[ParameterOut])
async def dashboard_parameters(dashboard_id: int, request: Request, user: User = Depends(get_current_user),
                               session: Session = Depends(get_session)):
    """The parameters as the form shows them, as a report's: defaults resolved, choices read (those
    of a dataset with the caller's rights, and the values of the query string for the other
    parameters, so that a choice can follow another)."""
    definition = dashboards.as_report(dashboards.content_of(session, dashboards.get_visible(session, user, dashboard_id)))
    try:
        return await _parameters_out(session, user, definition, _query_params(request), lambda _: definition)
    except HTTPException as e:
        raise dashboards.in_our_words(e) from None


@router.post("/dashboards/{dashboard_id}/parameters", response_model=list[ParameterOut])
async def dashboard_parameters_being_edited(dashboard_id: int, body: ParametersRequest, user: User = Depends(get_current_user),
                                            session: Session = Depends(get_session)):
    """The same for the editor's definition, not saved yet (EDIT on the folder): only the
    parameters and the dataset of each list must be whole, the rest may be half done."""
    _being_edited(session, user, dashboard_id, None)
    try:
        definition = dashboards.check_parameters(body.definition)
    except Problem as p:
        raise p.http(body.definition) from None
    try:
        return await _parameters_out(session, user, definition, reports.given_params(body.params),
                                     lambda name: dashboards.check_dataset(body.definition, name))
    except HTTPException as e:
        raise dashboards.in_our_words(e) from None


# ── create, change, copy, delete ──────────────────────────────────────────────
@router.post("/projects/{project_id}/dashboards", response_model=DashboardDetail, status_code=status.HTTP_201_CREATED)
async def create_dashboard(project_id: int, body: DashboardCreate, request: Request,
                           user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.EDIT)
    name = _name(session, project_id, body.name)
    content = body.content if body.content is not None else \
        dashboards.empty(user.locale or "en", get_settings().app.timezone)
    content = await dashboards.checked(session, user, content)
    dashboard = Dashboard(name=name, description=body.description.strip(), project_id=project_id, owner_id=user.id,
                          current_version=0)
    session.add(dashboard)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise _taken(name) from None
    dashboards.add_version(session, dashboard, content, "created", user)
    _commit_named(session, name)
    session.refresh(dashboard)
    _record(session, user, audit.DASHBOARD_CREATE, dashboard, request, {"project_id": project_id})
    return _detail(session, user, dashboard)


@router.patch("/dashboards/{dashboard_id}", response_model=DashboardDetail)
async def update_dashboard(dashboard_id: int, body: DashboardUpdate, request: Request,
                           user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Name, description, folder (a move), definition. A change of the definition is a new
    version, which becomes the current one."""
    dashboard = dashboards.get_visible(session, user, dashboard_id)
    ensure_can(session, user, dashboard.project_id, Capability.EDIT)
    target = dashboard.project_id
    if body.project_id is not None and body.project_id != dashboard.project_id:
        if session.get(Project, body.project_id) is None:
            raise HTTPException(status_code=404, detail="Destination folder not found")
        ensure_can(session, user, body.project_id, Capability.EDIT)
        target = body.project_id
    name = _name(session, target, body.name if body.name is not None else dashboard.name, exclude_id=dashboard.id)
    actions: list[tuple[str, dict]] = []
    if name != dashboard.name:
        actions.append((audit.DASHBOARD_RENAME, {"from": dashboard.name, "to": name}))
    if target != dashboard.project_id:
        actions.append((audit.DASHBOARD_MOVE, {"from": dashboard.project_id, "to": target}))
    changes: dict[str, Any] = {}
    if body.content is not None:
        content = await dashboards.checked(session, user, body.content, dashboards.content_of(session, dashboard))
        # read again and held until the commit: a save that landed while this one was checked
        # is the current version `base_version` is compared with
        session.refresh(dashboard, with_for_update=True)
        if content != dashboards.content_of(session, dashboard):
            if body.base_version is not None and body.base_version != dashboard.current_version:
                raise HTTPException(status_code=409, detail=(
                    f"«{dashboard.name}» changed since you opened it (now version {dashboard.current_version}): "
                    "reload it, then make your change again"))
            changes["version"] = dashboards.add_version(session, dashboard, content, "edited", user).version
    if body.description is not None and body.description.strip() != dashboard.description:
        dashboard.description = body.description.strip()
        changes["description"] = True
    if changes:
        actions.append((audit.DASHBOARD_UPDATE, changes))
    if not actions:
        return _detail(session, user, dashboard)
    dashboard.name, dashboard.project_id = name, target
    dashboard.updated_at = datetime.now(timezone.utc)
    session.add(dashboard)
    _commit_named(session, name)
    session.refresh(dashboard)
    for action, detail in actions:
        _record(session, user, action, dashboard, request, detail)
    return _detail(session, user, dashboard)


@router.post("/dashboards/{dashboard_id}/duplicate", response_model=DashboardDetail, status_code=status.HTTP_201_CREATED)
def duplicate_dashboard(dashboard_id: int, request: Request, user: User = Depends(get_current_user),
                        session: Session = Depends(get_session)):
    """A copy in its folder, as «name (i)», from the current version."""
    src = dashboards.get_visible(session, user, dashboard_id)
    ensure_can(session, user, src.project_id, Capability.EDIT)
    copy = Dashboard(name=free_copy_name(session, Dashboard, src.project_id, src.name), description=src.description,
                     project_id=src.project_id, owner_id=user.id, current_version=0)
    session.add(copy)
    session.flush()
    dashboards.add_version(session, copy, dashboards.content_of(session, src), "created", user)
    commit_copy(session)
    session.refresh(copy)
    _record(session, user, audit.DASHBOARD_DUPLICATE, copy, request, {"project_id": copy.project_id, "duplicate_of": src.id})
    return _detail(session, user, copy)


@router.delete("/dashboards/{dashboard_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_dashboard(dashboard_id: int, request: Request, user: User = Depends(get_current_user),
                     session: Session = Depends(get_session)):
    dashboard = dashboards.get_visible(session, user, dashboard_id)
    ensure_can(session, user, dashboard.project_id, Capability.EDIT)
    label = dashboard.name
    session.exec(delete(DashboardVersion).where(DashboardVersion.dashboard_id == dashboard.id))
    # its openings too: SQLite runs the tests without the cascade
    session.exec(delete(DashboardView).where(DashboardView.dashboard_id == dashboard.id))
    session.flush()
    session.delete(dashboard)
    session.commit()
    _record(session, user, audit.DASHBOARD_DELETE, dashboard, request, dashboard_id=dashboard_id, label=label)


# ── versions ──────────────────────────────────────────────────────────────────
@router.get("/dashboards/{dashboard_id}/versions", response_model=list[VersionOut])
def list_versions(dashboard_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    dashboard = dashboards.get_visible(session, user, dashboard_id)
    rows = session.exec(select(DashboardVersion).where(DashboardVersion.dashboard_id == dashboard.id)
                        .order_by(DashboardVersion.version.desc())).all()  # type: ignore[attr-defined]
    # an account that is only an observer is shared, and reads the emails masked (services/masking.py)
    shown = mask_email if perm_service.is_observer_only(session, user) else (lambda email: email)
    authors = {u.id: shown(u.email) for u in session.exec(select(User).where(
        User.id.in_({r.created_by for r in rows if r.created_by}))).all()} if rows else {}  # type: ignore[attr-defined]
    return [VersionOut(version=r.version, note=r.note, created_at=r.created_at, created_by=authors.get(r.created_by),
                       current=r.version == dashboard.current_version) for r in rows]


@router.get("/dashboards/{dashboard_id}/versions/{version}", response_model=dict)
def get_version(dashboard_id: int, version: int, user: User = Depends(get_current_user),
                session: Session = Depends(get_session)) -> dict:
    dashboard = dashboards.get_visible(session, user, dashboard_id)
    if dashboards.version_row(session, dashboard, version) is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return dashboards.content_of(session, dashboard, version)


@router.post("/dashboards/{dashboard_id}/versions/{version}/restore", response_model=DashboardDetail)
async def restore_version(dashboard_id: int, version: int, request: Request, user: User = Depends(get_current_user),
                          session: Session = Depends(get_session)):
    """An older version becomes the newest, and the current one. What it points to that the
    current one does not must be readable by whoever restores it."""
    dashboard = dashboards.get_visible(session, user, dashboard_id)
    ensure_can(session, user, dashboard.project_id, Capability.EDIT)
    if dashboards.version_row(session, dashboard, version) is None:
        raise HTTPException(status_code=404, detail="Version not found")
    if version == dashboard.current_version:
        return _detail(session, user, dashboard)
    current = dashboards.content_of(session, dashboard)
    content = await dashboards.checked(session, user, dashboards.content_of(session, dashboard, version), current)
    row = dashboards.add_version(session, dashboard, content, f"restored from v{version}", user)
    dashboard.updated_at = datetime.now(timezone.utc)
    session.add(dashboard)
    session.commit()
    session.refresh(dashboard)
    _record(session, user, audit.DASHBOARD_RESTORE, dashboard, request, {"from": version, "version": row.version})
    return _detail(session, user, dashboard)
