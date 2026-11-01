"""Dashboards: a screen of live tiles on a grid, kept in a folder with its versions.
docs/specs/dashboards.md; the rules in services/dashboards.py.

Permissions:
- seeing one (lists, search, opening it, its versions): VIEW on its folder; whoever lacks it
  gets 404, as if it did not exist; the viewer notes every opening, in the audit and in the
  count of who reads it, and the Dashboards tab (GET /dashboards/search) puts the dashboards
  opened by the most people first;
- creating, changing, moving, renaming, duplicating, deleting, restoring a version, and the
  check of a definition not saved yet: EDIT on the folder (both folders for a move); a
  definition saved must point only to what its author may read;
- its numbers (POST /dashboards/{id}/data, on the engine's preview queue) and its parameters:
  VIEW on the folder, read with the caller's rights at every request, dataset by dataset: a
  tile whose dataset they may not read says so inside the tile, the others work (an API token
  of either level reads them as it reads a report; a click and a refresh are not audited, they
  would drown the log). What comes out is shared by whoever reads the same source, for the
  dashboard's cache's life (the engine's: app/reports/tile_cache.py), after that check;
- the numbers of a definition not saved yet (POST /dashboards/preview): EDIT on the folder;
- a document of it (POST /dashboards/{id}/render for the viewer, GET for an API token, as a PDF or
  an image): as its numbers, with the clicks of the moment, and audited;
- administrators see and change every dashboard.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, delete, select

from app.core.config import get_settings
from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Dashboard, DashboardVersion, DashboardView, Project, User
from app.models.permission import Capability
from app.routes.reports import (
    CheckOut,
    FieldOut,
    FieldsOut,
    ParameterOut,
    ParametersRequest,
    RenderStarted,
    VersionOut,
    _parameters_out,
    _query_params,
    _wait_for_file,
)
from app.routes.search import _like as like_pattern
from app.schemas.models import Page, UtcDateTime
from app.services import audit, dashboards, reports
from app.services import permissions as perm_service
from app.services.copy_names import commit_copy, free_copy_name
from app.services.masking import mask_email
from app.services.pagination import paginate
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


class DashboardRow(DashboardOut):
    """A row of the Dashboards tab: the dashboard, where it lives, who made it, who reads it."""
    folder_path: list[str]  # from the root down to its folder
    owner_name: Optional[str] = None  # full name, else email; None when the account is gone
    # opened in the viewer in the last 30 days: by how many people, and how many openings (a
    # person once a day)
    people_30d: int = 0
    opens_30d: int = 0
    views_total: int = 0  # ever, a person once a day
    last_viewed_at: Optional[UtcDateTime] = None


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
    (a column sorted, groups toggled); `clicks` what is chosen on a tile, which filters the others
    on its dataset: {"tile", "values", "on"}, a matrix's `on` being "columns", and never an expression
    (services/dashboards.py). `refresh` reads the data again, at most every 10 seconds per dashboard
    for everyone: past that, it answers with what is kept and when it was read, as any request does."""
    params: dict[str, Any] = Field(default_factory=dict)
    clicks: list[Any] = Field(default_factory=list, max_length=2 * dashboards.MAX_TILES)  # a matrix: its rows and its columns
    tiles: Optional[list[str]] = Field(default=None, max_length=dashboards.MAX_TILES)
    page: dict[str, int] = Field(default_factory=dict, max_length=dashboards.MAX_TILES)
    state: Optional[dict[str, Any]] = None
    refresh: bool = False


class PreviewRequest(DataRequest):
    """The numbers of a definition being edited, of a saved dashboard (`dashboard_id`) or of a
    new one in a folder (`project_id`). `tab` tells one designer from another of the same person: a
    newer request knocks down an older one of its own tab, and two tabs on one dashboard both answer."""
    definition: dict[str, Any]
    dashboard_id: Optional[int] = None
    project_id: Optional[int] = None
    tab: str = Field(default="", max_length=32)


class FieldsRequest(BaseModel):
    dataset: str = Field(max_length=64)
    definition: dict[str, Any]  # the editor's, not saved yet


class RenderRequest(BaseModel):
    """A document of the dashboard: its format; the parameters, the clicks and the on-screen view, as
    for its numbers."""
    format: Literal["pdf", "png"] = "pdf"
    params: dict[str, Any] = Field(default_factory=dict)
    clicks: list[Any] = Field(default_factory=list, max_length=2 * dashboards.MAX_TILES)
    state: Optional[dict[str, Any]] = None


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
                            clicks=body.clicks, refresh=body.refresh)


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


# NB: registered BEFORE /dashboards/{dashboard_id} routes of the same method, so that "search",
# "check" and "preview" are not taken for an id
@router.get("/dashboards/search", response_model=Page[DashboardRow])
def search_dashboards(
    q: Optional[str] = Query(None, description="searches name, description and folder, over the WHOLE dataset"),
    sort: Literal["trending", "name", "updated"] = Query("trending"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The dashboards this person sees, PAGINATED, for the Dashboards tab. `trending` (the
    default) puts first those opened by the most people in the last 30 days, then by the most
    openings in them, then by the most ever, then the latest changed; `name` and `updated` (the
    latest changed first) as they say. The search matches the name, the description, and the
    name of the dashboard's folder or of a folder above it."""
    visible = dashboards.visible_query(session, user, ordered=False)
    if visible is None:
        return Page(items=[], total=0)
    recent = dashboards.recent_counts()
    people = func.coalesce(recent.c.people, 0)
    opens = func.coalesce(recent.c.opens, 0)
    stmt = select(Dashboard, people, opens).outerjoin(recent, recent.c.dashboard_id == Dashboard.id)
    if visible.whereclause is not None:
        stmt = stmt.where(visible.whereclause)
    names = {pid: p.name for pid, p in perm_service._all_projects(session).items()}
    parents = perm_service.folder_parents(session)
    if q and q.strip():
        text = q.strip()
        needle = text.casefold()
        in_folders = [pid for pid in names if any(needle in names[a].casefold()
                                                  for a in perm_service.ancestor_ids(parents, pid) if a in names)]
        like = like_pattern(text)
        stmt = stmt.where(or_(Dashboard.name.ilike(like, escape="\\"), Dashboard.description.ilike(like, escape="\\"),
                              Dashboard.project_id.in_(in_folders)))  # type: ignore[attr-defined]
    order = {
        "trending": (people.desc(), opens.desc(), Dashboard.views_total.desc(), Dashboard.updated_at.desc(), Dashboard.id.desc()),
        "name": (func.lower(Dashboard.name), Dashboard.id),
        "updated": (Dashboard.updated_at.desc(), Dashboard.id.desc()),
    }[sort]
    rows, total = paginate(session, stmt, order, limit, offset)
    found = [d for d, _, _ in rows]
    sizes = dashboards.sizes_of(session, [d.id for d in found])
    # an account that is only an observer is shared, and reads the emails masked (services/masking.py)
    shown = mask_email if perm_service.is_observer_only(session, user) else (lambda email: email)
    owners = {uid: full_name or shown(email) for uid, full_name, email in session.exec(
        select(User.id, User.full_name, User.email)
        .where(User.id.in_({d.owner_id for d in found if d.owner_id is not None}))).all()}  # type: ignore[attr-defined]
    items = []
    for d, n_people, n_opens in rows:
        path = [names[a] for a in reversed(perm_service.ancestor_ids(parents, d.project_id)) if a in names]
        items.append(DashboardRow(**_out(session, user, d, *sizes.get(d.id, (0, 0, 0))).model_dump(), folder_path=path,
                                  owner_name=owners.get(d.owner_id), people_30d=n_people, opens_30d=n_opens,
                                  views_total=d.views_total, last_viewed_at=d.last_viewed_at))
    return Page(items=items, total=total)


@router.get("/dashboards/folders", response_model=list[int])
def folders_for_a_new_dashboard(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """The folders where this person can make one: EDIT."""
    return sorted(perm_service.editable_project_ids(session, user))


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
    editing = f"preview-{dashboard.id if dashboard is not None else 'new'}{f'-{body.tab}' if body.tab else ''}"
    title = dashboard.name if dashboard is not None else ""
    return await dashboards.numbers(session, user, looked.definition, _asked(body), title, editing=editing)


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
    return await dashboards.numbers(session, user, definition, _asked(body), dashboard.name, saved=dashboard.id)


@router.post("/dashboards/{dashboard_id}/render", response_model=RenderStarted)
async def render_dashboard(dashboard_id: int, body: RenderRequest, request: Request, user: User = Depends(get_current_user),
                           session: Session = Depends(get_session)):
    """Starts a document of the current version with these parameters and clicks, read with the
    caller's rights. Followed and downloaded as an export (/tasks/exports/{task_id})."""
    dashboard = dashboards.get_visible(session, user, dashboard_id)
    started = await dashboards.render(session, user, dashboard, body.format, reports.given_params(body.params), clicks=body.clicks,
                                      state=body.state, request=request)
    return RenderStarted(task_id=started.task_id, filename=started.filename)


@router.get("/dashboards/{dashboard_id}/render")
async def render_dashboard_file(dashboard_id: int, request: Request, format: Literal["pdf", "png"] = Query("pdf", description="pdf or png"),
                                user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """The API: the dashboard as a file, with the parameters and the clicks of the query string as
    the viewer's address holds them (`?region=North&click.by_area=Nord-Est`), read with the rights of the
    token's owner. Waits for the file and sends it, with its name. 400 naming the parameter whose
    value does not fit, 422 for a click that cannot be; 504 past REPORTS__API_WAIT_SECONDS."""
    from app.routes.proxy import _forward, _release_db, export_owner

    dashboard = dashboards.get_visible(session, user, dashboard_id)
    given, clicks = dashboards.in_the_address(_query_params(request))
    started = await dashboards.render(session, user, dashboard, format, given, clicks=clicks, origin="api", request=request, audit_success=False)
    owner = export_owner(user)
    _release_db(session, user)  # nothing holds a database connection while the engine works
    try:
        await _wait_for_file(started.task_id, owner, request)
    except HTTPException as e:
        dashboards.audit_render(session, user, dashboard, request, "failure", {**started.detail, "error": str(e.detail)[:500]})
        raise dashboards.in_our_words(e) from None
    dashboards.audit_render(session, user, dashboard, request, "success", started.detail)
    return await _forward(request, "GET", f"/tasks/exports/{started.task_id}/file", session=session, user=user, params={"owner": owner})


@router.get("/dashboards/{dashboard_id}/parameters", response_model=list[ParameterOut])
async def dashboard_parameters(dashboard_id: int, request: Request, user: User = Depends(get_current_user),
                               session: Session = Depends(get_session)):
    """The parameters as the form shows them, as a report's: defaults resolved, choices read (those
    of a dataset with the caller's rights, and the values of the query string for the other
    parameters, so that a choice can follow another)."""
    dashboard = dashboards.get_visible(session, user, dashboard_id)
    content = dashboards.content_of(session, dashboard)
    definition = dashboards.as_report(content)
    try:
        return await _parameters_out(session, user, definition, _query_params(request), lambda _: definition,
                                     await dashboards.cache_of(content, dashboard.id))
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


@router.post("/dashboards/{dashboard_id}/fields", response_model=FieldsOut, response_model_exclude_none=True)
async def dashboard_fields(dashboard_id: int, body: FieldsRequest, user: User = Depends(get_current_user),
                           session: Session = Depends(get_session)):
    """A dataset's columns of the editor's definition («Refresh fields»), read with the caller's
    rights (EDIT on the folder), and its rows, counted when they can be, as a report's."""
    dashboard = _being_edited(session, user, dashboard_id, None)
    try:
        definition = dashboards.check_dataset(body.definition, body.dataset)
    except Problem as p:
        raise p.http(body.definition) from None
    slot = f"u{user.id}:dashboard:{dashboard.id}"
    counting = asyncio.ensure_future(reports.count_rows(await reports.to_count(session, user, definition, body.dataset),
                                                        f"{slot}:count"))
    try:
        found = await reports.fields(session, user, definition, body.dataset, f"{slot}:fields")
    except HTTPException as e:
        counting.cancel()
        raise dashboards.in_our_words(e) from None
    except BaseException:
        counting.cancel()
        raise
    return FieldsOut(dataset=body.dataset, fields=[FieldOut(**f) for f in found], **(await counting or {}))


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
    groups_off: list[tuple] = []
    if body.content is not None:
        content = await dashboards.checked(session, user, body.content, dashboards.content_of(session, dashboard))
        # read again and held until the commit: a save that landed while this one was checked
        # is the current version `base_version` is compared with
        session.refresh(dashboard, with_for_update=True)
        current = dashboards.content_of(session, dashboard)
        if content != current:
            if body.base_version is not None and body.base_version != dashboard.current_version:
                raise HTTPException(status_code=409, detail=(
                    f"«{dashboard.name}» changed since you opened it (now version {dashboard.current_version}): "
                    "reload it, then make your change again"))
            changes["version"] = dashboards.add_version(session, dashboard, content, "edited", user).version
            groups_off = dashboards.groups_off(session, user, dashboard, current, content)
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
    reports.record_groups_off(session, user, groups_off, request)
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
    # a flow group that sends it would fail at its Pushmail cell: it is taken out there first
    groups = [g.name for g in dashboards.groups_using(session, dashboard.id)]
    if groups:
        names = ", ".join(f"«{n}»" for n in groups[:10]) + (" …" if len(groups) > 10 else "")
        raise HTTPException(status_code=409, detail={
            "message": f"The dashboard is sent by the flow groups {names}: remove it from there first", "flows": [], "groups": groups})
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
    groups_off = dashboards.groups_off(session, user, dashboard, current, content)
    dashboard.updated_at = datetime.now(timezone.utc)
    session.add(dashboard)
    session.commit()
    session.refresh(dashboard)
    _record(session, user, audit.DASHBOARD_RESTORE, dashboard, request, {"from": version, "version": row.version})
    reports.record_groups_off(session, user, groups_off, request)
    return _detail(session, user, dashboard)
