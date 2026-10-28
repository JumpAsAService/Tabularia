"""Pixel-perfect reports: a page designed in the browser and kept in a folder with its
versions, rendered as PDF, Excel or HTML with its parameters. docs/specs/reports.md;
the rules in services/reports.py.

Permissions:
- seeing one (lists, search, opening it, its versions, its parameters, its fields) and
  rendering it: VIEW on its folder; whoever lacks it gets 404, as if it did not exist;
  the viewer notes every opening (services/report_views.py), and the Reports tab
  (GET /reports/search) puts the reports opened by the most people first;
- creating, changing, moving, renaming, duplicating, deleting, restoring a version, and
  the editor's preview and check of a definition not saved yet: EDIT on the folder (both
  folders for a move); a definition saved must point only to what its author may read;
- what a render reads is checked dataset by dataset with the rights of whoever asks
  for it (services/reports.py): a dataset they may not read stops it, with its name;
- the API (GET /reports/{id}/render) answers to an API token of either level, with its
  owner's rights, waits for the file and sends it;
- administrators see and change every report.

Every installation has them: no Kubernetes, no optional service but the engine's chart
renderer.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.engine_client import CHECK, get_engine_client
from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Project, Report, ReportVersion, User
from app.models.permission import Capability
from app.routes.search import _like as like_pattern
from app.schemas.models import Page, UtcDateTime
from app.services import audit, report_definition, report_params, report_views, reports
from app.services import permissions as perm_service
from app.services.copy_names import commit_copy, free_copy_name
from app.services.pagination import paginate
from app.services.report_definition import Problem

router = APIRouter(route_class=ReleasingRoute, tags=["reports"])


class ReportOut(BaseModel):
    id: int
    name: str
    description: str
    project_id: int
    current_version: int
    datasets: int  # of the current version
    parameters: int
    created_at: UtcDateTime
    updated_at: UtcDateTime
    editable: bool  # EDIT on its folder: the page offers saving, moving, deleting


class ReportDetail(ReportOut):
    content: dict[str, Any]


class ReportRow(ReportOut):
    """A row of the Reports tab: the report, where it lives, who made it, who reads it."""
    folder_path: list[str]  # from the root down to its folder
    owner_name: Optional[str] = None  # full name, else email; None when the account is gone
    # opened in the viewer in the last 30 days (services/report_views.py): by how many
    # people, and how many openings (a person once a day)
    people_30d: int = 0
    opens_30d: int = 0
    views_total: int = 0  # ever, a person once a day
    last_viewed_at: Optional[UtcDateTime] = None


class ReportCreate(BaseModel):
    name: str = Field(max_length=200)
    description: str = Field(default="", max_length=2000)
    content: Optional[dict[str, Any]] = None


class ReportUpdate(BaseModel):
    name: Optional[str] = Field(default=None, max_length=200)
    description: Optional[str] = Field(default=None, max_length=2000)
    project_id: Optional[int] = None  # a move
    content: Optional[dict[str, Any]] = None
    # the version the new content was written on: refused (409) when another became the
    # current one since, which the new content would undo without anyone seeing it
    base_version: Optional[int] = None


class VersionOut(BaseModel):
    version: int
    note: str
    created_at: UtcDateTime
    created_by: Optional[str]
    current: bool


class RenderRequest(BaseModel):
    format: Literal["pdf", "xlsx", "html", "svg"] = "pdf"
    # the parameters' values: a text, a number, true/false, or a list for a multi one
    params: dict[str, Any] = Field(default_factory=dict)
    state: Optional[dict[str, Any]] = None  # the on-screen view: a column sorted, groups toggled


class PreviewRequest(RenderRequest):
    """The editor's preview of a definition not saved yet: of a saved report
    (`report_id`), or of a new one in a folder (`project_id`)."""
    definition: dict[str, Any]
    report_id: Optional[int] = None
    project_id: Optional[int] = None


class CheckRequest(BaseModel):
    """A definition being edited, of a saved report (`report_id`) or of a new one in a
    folder (`project_id`)."""
    definition: dict[str, Any]
    report_id: Optional[int] = None
    project_id: Optional[int] = None


class ProblemOut(BaseModel):
    """A problem as the engine says it, and a refused save lists them: the item (its id,
    none for the report, a dataset or a parameter), the property (`columns[0].value`,
    `datasets[orders].source.sql`), the position in an expression (0 = its first
    character) and how many characters."""
    item: Optional[str] = None
    field: str = ""
    pos: Optional[int] = None
    length: int = 0
    message: str


class CheckOut(BaseModel):
    problems: list[ProblemOut]


class RenderStarted(BaseModel):
    task_id: str  # followed and downloaded as an export: /tasks/exports/{task_id}
    filename: str


class FieldsRequest(BaseModel):
    dataset: str = Field(max_length=64)
    # the editor's definition, not saved yet (EDIT on the folder); none = the saved one
    definition: Optional[dict[str, Any]] = None


class FieldOut(BaseModel):
    name: str
    type: str  # string | integer | float | date | datetime | time | boolean | other


class FieldsOut(BaseModel):
    dataset: str
    fields: list[FieldOut]
    # the rows with the parameters' defaults, counted up to the engine's cap (REPORTS__MAX_ROWS):
    # `rows` at most `max_rows`, `over` when there are more (a render would stop). Absent when
    # they could not be counted: a required parameter without a default, an engine that failed
    # or took longer than services/reports.COUNT_SECONDS
    rows: Optional[int] = None
    over: Optional[bool] = None
    max_rows: Optional[int] = None


class Choice(BaseModel):
    value: Any
    label: Optional[str] = None


class ParameterOut(BaseModel):
    name: str
    label: str
    type: str
    multi: bool
    required: bool
    default: Any = None  # resolved: a placeholder is its value today
    choices: Optional[list[Choice]] = None  # null: any value of its type
    choices_truncated: bool = False  # more than the list holds: the form takes any value
    choices_error: Optional[str] = None  # why the choices of a dataset could not be read


# ── helpers ───────────────────────────────────────────────────────────────────
def _name(session: Session, project_id: int, name: str, exclude_id: Optional[int] = None) -> str:
    name = (name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="The report needs a name")
    clash = session.exec(select(Report.id).where(Report.project_id == project_id, Report.name == name)).first()
    if clash is not None and clash != exclude_id:
        raise _taken(name)
    return name


def _taken(name: str) -> HTTPException:
    return HTTPException(status_code=409, detail=f"There is already a report '{name}' in this folder")


def _commit_named(session: Session, name: str) -> None:
    """Two saves at the same moment may give a folder the same name twice: the folder's
    unique name refuses the second one, which gets the 409 of a name taken (the Explore
    then tries the next «New report (n)») instead of a server error."""
    try:
        session.commit()
    except IntegrityError:
        session.rollback()
        raise _taken(name) from None


def _being_edited(session: Session, user: User, report_id: Optional[int],
                  project_id: Optional[int]) -> Optional[Report]:
    """A definition being edited is of a saved report, or of a new one in a folder: EDIT on
    that folder. The saved report, or None."""
    report = reports.get_visible(session, user, report_id) if report_id is not None else None
    folder = report.project_id if report is not None else project_id
    if folder is None or session.get(Project, folder) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, folder, Capability.EDIT)
    return report


def _out(session: Session, user: User, r: Report, content: Optional[dict] = None) -> ReportOut:
    content = content if content is not None else reports.content_of(session, r)
    return ReportOut(id=r.id, name=r.name, description=r.description, project_id=r.project_id,
                     current_version=r.current_version, datasets=len(content.get("datasets") or []),
                     parameters=len(content.get("parameters") or []), created_at=r.created_at, updated_at=r.updated_at,
                     editable=perm_service.has_capability(session, user, r.project_id, Capability.EDIT))


def _detail(session: Session, user: User, r: Report) -> ReportDetail:
    content = reports.content_of(session, r)
    return ReportDetail(**_out(session, user, r, content).model_dump(), content=content)


def _many(session: Session, user: User, stmt) -> list[ReportOut]:
    if stmt is None:
        return []
    found = list(session.exec(stmt).all())
    contents = reports.current_contents(session, found)
    return [_out(session, user, r, contents[r.id]) for r in found]


def _record(session: Session, user: User, action: str, report: Report, request: Request,
            detail: Optional[dict] = None, report_id: Optional[int] = None, label: Optional[str] = None) -> None:
    audit.record_audit(session, actor=user, action=action, target_type="report",
                       target_id=report_id if report_id is not None else report.id,
                       target_label=label if label is not None else report.name, detail=detail, request=request)


def _query_params(request: Request) -> dict[str, list]:
    """The parameters' values of a query string, `format` aside: a repeated name is a list."""
    given: dict[str, list] = {}
    for key, value in request.query_params.multi_items():
        if key != "format":
            given.setdefault(key, []).append(value)
    return given


# ── lists ─────────────────────────────────────────────────────────────────────
@router.get("/reports", response_model=list[ReportOut])
def list_reports(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Every report this person sees."""
    return _many(session, user, reports.visible_query(session, user))


# NB: registered BEFORE /reports/{report_id}, so that "search" is not taken for an id
@router.get("/reports/search", response_model=Page[ReportRow])
def search_reports(
    q: Optional[str] = Query(None, description="searches name, description and folder, over the WHOLE dataset"),
    sort: Literal["trending", "name", "updated"] = Query("trending"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The reports this person sees, PAGINATED, for the Reports tab. `trending` (the
    default) puts first those opened by the most people in the last 30 days, then by the
    most openings in them, then by the most ever, then the latest changed; `name` and
    `updated` (the latest changed first) as they say. The search matches the name, the
    description, and the name of the report's folder or of a folder above it."""
    visible = reports.visible_query(session, user, ordered=False)
    if visible is None:
        return Page(items=[], total=0)
    recent = report_views.recent_counts()
    people = func.coalesce(recent.c.people, 0)
    opens = func.coalesce(recent.c.opens, 0)
    stmt = select(Report, people, opens).outerjoin(recent, recent.c.report_id == Report.id)
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
        stmt = stmt.where(or_(Report.name.ilike(like, escape="\\"), Report.description.ilike(like, escape="\\"),
                              Report.project_id.in_(in_folders)))  # type: ignore[attr-defined]
    order = {
        "trending": (people.desc(), opens.desc(), Report.views_total.desc(), Report.updated_at.desc(), Report.id.desc()),
        "name": (func.lower(Report.name), Report.id),
        "updated": (Report.updated_at.desc(), Report.id.desc()),
    }[sort]
    rows, total = paginate(session, stmt, order, limit, offset)
    found = [r for r, _, _ in rows]
    contents = reports.current_contents(session, found)
    owners = {uid: full_name or email for uid, full_name, email in session.exec(
        select(User.id, User.full_name, User.email)
        .where(User.id.in_({r.owner_id for r in found if r.owner_id is not None}))).all()}  # type: ignore[attr-defined]
    items = []
    for r, n_people, n_opens in rows:
        path = [names[a] for a in reversed(perm_service.ancestor_ids(parents, r.project_id)) if a in names]
        items.append(ReportRow(**_out(session, user, r, contents[r.id]).model_dump(), folder_path=path,
                               owner_name=owners.get(r.owner_id), people_30d=n_people, opens_30d=n_opens,
                               views_total=r.views_total, last_viewed_at=r.last_viewed_at))
    return Page(items=items, total=total)


@router.post("/reports/{report_id}/views", status_code=status.HTTP_204_NO_CONTENT)
def report_opened(report_id: int, request: Request, user: User = Depends(get_current_user),
                  session: Session = Depends(get_session)):
    """The viewer opened the report (services/report_views.py): a person once a day
    counts for the Reports tab. A call with an API token counts nothing: a script is not
    a reader."""
    report = reports.get_visible(session, user, report_id)
    if getattr(request.state, "api_token", None) is None:
        report_views.record(session, report, user)


@router.get("/projects/{project_id}/reports", response_model=list[ReportOut])
def list_project_reports(project_id: int, user: User = Depends(get_current_user),
                         session: Session = Depends(get_session)):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.VIEW)
    return _many(session, user, reports.visible_query(session, user, {project_id}))


@router.get("/reports/{report_id}", response_model=ReportDetail)
def get_report(report_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    return _detail(session, user, reports.get_visible(session, user, report_id))


# ── create, change, copy, delete ──────────────────────────────────────────────
@router.post("/projects/{project_id}/reports", response_model=ReportDetail, status_code=status.HTTP_201_CREATED)
async def create_report(project_id: int, body: ReportCreate, request: Request,
                        user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.EDIT)
    name = _name(session, project_id, body.name)
    content = body.content if body.content is not None else \
        report_definition.empty(user.locale or "en", get_settings().app.timezone)
    content = await reports.checked(session, user, content)
    report = Report(name=name, description=body.description.strip(), project_id=project_id, owner_id=user.id,
                    current_version=0)
    session.add(report)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise _taken(name) from None
    reports.add_version(session, report, content, "created", user)
    _commit_named(session, name)
    session.refresh(report)
    _record(session, user, audit.REPORT_CREATE, report, request, {"project_id": project_id})
    return _detail(session, user, report)


def _groups_off(session: Session, user: User, report: Report, before: dict, after: dict) -> list[tuple]:
    """The flow groups that send the report, scheduled by someone else: a change that makes
    the report read more switches their schedules off, as for SQL scripts — a schedule
    carries its author's rights, and those are not spent on someone else's choice. More
    is a dataset that reads something else, or a new default of a parameter the group's
    cell gives no value."""
    reasons, defaults = reports.widens(before, after), reports.new_defaults(before, after)
    if not reasons and not defaults:
        return []
    affected = []
    for group in reports.groups_using(session, report.id):
        mine = reasons + [f"the default of «{name}» changed" for name in
                          sorted(defaults - reports.given_by(group, report.id))]
        if mine:
            affected.append((group, f"the report '{report.name}' it sends changed: {'; '.join(mine)}"))
    return reports.turn_off_groups(session, user, affected)


@router.patch("/reports/{report_id}", response_model=ReportDetail)
async def update_report(report_id: int, body: ReportUpdate, request: Request,
                        user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Name, description, folder (a move), definition. A change of the definition is a
    new version, which becomes the current one."""
    report = reports.get_visible(session, user, report_id)
    ensure_can(session, user, report.project_id, Capability.EDIT)
    target = report.project_id
    if body.project_id is not None and body.project_id != report.project_id:
        if session.get(Project, body.project_id) is None:
            raise HTTPException(status_code=404, detail="Destination folder not found")
        ensure_can(session, user, body.project_id, Capability.EDIT)
        target = body.project_id
    name = _name(session, target, body.name if body.name is not None else report.name, exclude_id=report.id)
    actions: list[tuple[str, dict]] = []
    if name != report.name:
        actions.append((audit.REPORT_RENAME, {"from": report.name, "to": name}))
    if target != report.project_id:
        actions.append((audit.REPORT_MOVE, {"from": report.project_id, "to": target}))
    changes: dict[str, Any] = {}
    groups_off: list[tuple] = []
    if body.content is not None:
        content = await reports.checked(session, user, body.content, reports.content_of(session, report))
        # read again and held until the commit: a save that landed while this one was
        # checked is the current version `base_version` is compared with
        session.refresh(report, with_for_update=True)
        current = reports.content_of(session, report)
        if content != current:
            if body.base_version is not None and body.base_version != report.current_version:
                raise HTTPException(status_code=409, detail=(
                    f"«{report.name}» changed since you opened it (now version {report.current_version}): "
                    "reload it, then make your change again"))
            changes["version"] = reports.add_version(session, report, content, "edited", user).version
            groups_off = _groups_off(session, user, report, current, content)
    if body.description is not None and body.description.strip() != report.description:
        report.description = body.description.strip()
        changes["description"] = True
    if changes:
        actions.append((audit.REPORT_UPDATE, changes))
    if not actions:
        return _detail(session, user, report)
    report.name, report.project_id = name, target
    report.updated_at = datetime.now(timezone.utc)
    session.add(report)
    _commit_named(session, name)
    session.refresh(report)
    for action, detail in actions:
        _record(session, user, action, report, request, detail)
    reports.record_groups_off(session, user, groups_off, request)
    return _detail(session, user, report)


@router.post("/reports/{report_id}/duplicate", response_model=ReportDetail, status_code=status.HTTP_201_CREATED)
def duplicate_report(report_id: int, request: Request, user: User = Depends(get_current_user),
                     session: Session = Depends(get_session)):
    """A copy in its folder, as «name (i)», from the current version."""
    src = reports.get_visible(session, user, report_id)
    ensure_can(session, user, src.project_id, Capability.EDIT)
    copy = Report(name=free_copy_name(session, Report, src.project_id, src.name), description=src.description,
                  project_id=src.project_id, owner_id=user.id, current_version=0)
    session.add(copy)
    session.flush()
    reports.add_version(session, copy, reports.content_of(session, src), "created", user)
    commit_copy(session)
    session.refresh(copy)
    _record(session, user, audit.REPORT_DUPLICATE, copy, request, {"project_id": copy.project_id, "duplicate_of": src.id})
    return _detail(session, user, copy)


@router.delete("/reports/{report_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_report(report_id: int, request: Request, user: User = Depends(get_current_user),
                  session: Session = Depends(get_session)):
    report = reports.get_visible(session, user, report_id)
    ensure_can(session, user, report.project_id, Capability.EDIT)
    # a flow group that sends it would fail at its Pushmail cell: it is taken out there first
    groups = [g.name for g in reports.groups_using(session, report.id)]
    if groups:
        names = ", ".join(f"«{n}»" for n in groups[:10]) + (" …" if len(groups) > 10 else "")
        raise HTTPException(status_code=409, detail={
            "message": f"The report is sent by the flow groups {names}: remove it from there first",
            "flows": [], "groups": groups})
    label = report.name
    for row in session.exec(select(ReportVersion).where(ReportVersion.report_id == report.id)).all():
        session.delete(row)
    report_views.forget_report(session, report.id)
    session.flush()
    session.delete(report)
    session.commit()
    _record(session, user, audit.REPORT_DELETE, report, request, report_id=report_id, label=label)


# ── versions ──────────────────────────────────────────────────────────────────
@router.get("/reports/{report_id}/versions", response_model=list[VersionOut])
def list_versions(report_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    report = reports.get_visible(session, user, report_id)
    rows = session.exec(select(ReportVersion).where(ReportVersion.report_id == report.id)
                        .order_by(ReportVersion.version.desc())).all()  # type: ignore[attr-defined]
    authors = {u.id: u.email for u in session.exec(select(User).where(
        User.id.in_({r.created_by for r in rows if r.created_by}))).all()} if rows else {}  # type: ignore[attr-defined]
    return [VersionOut(version=r.version, note=r.note, created_at=r.created_at, created_by=authors.get(r.created_by),
                       current=r.version == report.current_version) for r in rows]


@router.get("/reports/{report_id}/versions/{version}", response_model=dict)
def get_version(report_id: int, version: int, user: User = Depends(get_current_user),
                session: Session = Depends(get_session)) -> dict:
    report = reports.get_visible(session, user, report_id)
    if reports.version_row(session, report, version) is None:
        raise HTTPException(status_code=404, detail="Version not found")
    return reports.content_of(session, report, version)


@router.post("/reports/{report_id}/versions/{version}/restore", response_model=ReportDetail)
async def restore_version(report_id: int, version: int, request: Request, user: User = Depends(get_current_user),
                          session: Session = Depends(get_session)):
    """An older version becomes the newest, and the current one. What it points to that
    the current one does not must be readable by whoever restores it."""
    report = reports.get_visible(session, user, report_id)
    ensure_can(session, user, report.project_id, Capability.EDIT)
    if reports.version_row(session, report, version) is None:
        raise HTTPException(status_code=404, detail="Version not found")
    if version == report.current_version:
        return _detail(session, user, report)
    current = reports.content_of(session, report)
    content = await reports.checked(session, user, reports.content_of(session, report, version), current)
    row = reports.add_version(session, report, content, f"restored from v{version}", user)
    groups_off = _groups_off(session, user, report, current, content)
    report.updated_at = datetime.now(timezone.utc)
    session.add(report)
    session.commit()
    session.refresh(report)
    _record(session, user, audit.REPORT_RESTORE, report, request, {"from": version, "version": row.version})
    reports.record_groups_off(session, user, groups_off, request)
    return _detail(session, user, report)


# ── render ────────────────────────────────────────────────────────────────────
@router.post("/reports/{report_id}/render", response_model=RenderStarted)
async def render_report(report_id: int, body: RenderRequest, request: Request,
                        user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Starts the render of the report's current version with these parameters, read with
    the caller's rights. Followed and downloaded as an export (/tasks/exports/{task_id})."""
    report = reports.get_visible(session, user, report_id)
    started = await reports.render(session, user, report, reports.content_of(session, report), body.format,
                                   reports.given_params(body.params), state=body.state, request=request)
    return RenderStarted(task_id=started.task_id, filename=started.filename)


@router.post("/reports/preview", response_model=RenderStarted)
async def preview_report(body: PreviewRequest, request: Request, user: User = Depends(get_current_user),
                         session: Session = Depends(get_session)):
    """The editor's preview of a definition not saved yet: EDIT on the report's folder,
    or on the folder a new one goes in. Checked first, as a save is: problems are a 422
    that lists them by item and property, as a refused save does; what only reading the
    data finds (a dataset too large, one that cannot be read) is said as a render says it."""
    report = _being_edited(session, user, body.report_id, body.project_id)
    looked = await reports.review(session, user, body.definition)
    if looked.problems:
        raise looked.refusal()
    started = await reports.render(session, user, report, looked.definition, body.format,
                                   reports.given_params(body.params), state=body.state, origin="editor",
                                   request=request)
    return RenderStarted(task_id=started.task_id, filename=started.filename)


@router.post("/reports/check", response_model=CheckOut)
async def check_report(body: CheckRequest, user: User = Depends(get_current_user),
                       session: Session = Depends(get_session)):
    """The problems of a definition being edited, as a save would say them, without saving
    it: the gateway's and the engine's (the expressions against the datasets' fields). The
    editor asks while one types. EDIT on the report's folder, or on the folder a new one
    goes in."""
    report = _being_edited(session, user, body.report_id, body.project_id)
    current = reports.content_of(session, report) if report is not None else None
    return CheckOut(problems=(await reports.review(session, user, body.definition, current)).problems)


async def _stop(task_id: str) -> None:
    try:
        await get_engine_client().delete(f"/tasks/{task_id}", timeout=CHECK)
    except Exception:  # noqa: BLE001 — the hourly sweep removes what is left
        pass


async def _wait_for_file(task_id: str, owner: str, request: Optional[Request] = None) -> None:
    """Until the engine has written the file: 504 past REPORTS__API_WAIT_SECONDS, the
    engine's reason when it failed. A caller that stops waiting (its timeout, a script
    stopped) stops the render too: a script that tries again does not pile renders up
    on the export queue. Asked often at first, then once a second."""
    client = get_engine_client()
    deadline = time.monotonic() + get_settings().reports.api_wait_seconds
    delay = 0.25
    while True:
        resp = await client.get(f"/tasks/exports/{task_id}", params={"owner": owner}, timeout=CHECK)
        state = resp.json() if resp.status_code < 400 else {"status": "failed", "code": 502,
                                                             "detail": f"The engine answered {resp.status_code}"}
        if state.get("status") == "ready":
            return
        if state.get("status") == "failed":
            code = int(state.get("code") or 500)
            raise HTTPException(status_code=422 if code < 500 else 502,
                                detail=reports.sentence(str(state.get("detail") or "The render failed")))
        if request is not None and await request.is_disconnected():
            await _stop(task_id)
            raise HTTPException(status_code=499, detail="The caller stopped waiting: the render was stopped")
        if time.monotonic() + delay > deadline:
            await _stop(task_id)
            seconds = get_settings().reports.api_wait_seconds
            raise HTTPException(status_code=504, detail=f"The report was not ready within {seconds} s "
                                                        "(REPORTS__API_WAIT_SECONDS)")
        await asyncio.sleep(delay)
        delay = min(1.0, delay * 1.5)


@router.get("/reports/{report_id}/render")
async def render_report_file(report_id: int, request: Request,
                             format: Literal["pdf", "xlsx", "html"] = Query("pdf", description="pdf, xlsx or html"),
                             user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """The API: the report rendered with the parameters of the query string
    (`?region=North&since=2026-01-01`, a name repeated for a multi parameter), read with
    the rights of the token's owner. Waits for the file and sends it, with its name. 400
    naming the parameter whose value does not fit; 504 past REPORTS__API_WAIT_SECONDS."""
    from app.routes.proxy import _forward, _release_db, export_owner

    report = reports.get_visible(session, user, report_id)
    started = await reports.render(session, user, report, reports.content_of(session, report), format,
                                   _query_params(request), origin="api", request=request, audit_success=False)
    owner = export_owner(user)
    _release_db(session, user)  # nothing holds a database connection while the engine works
    try:
        await _wait_for_file(started.task_id, owner, request)
    except HTTPException as e:
        reports._audit_render(session, user, report, request, "failure", {**started.detail, "error": str(e.detail)[:500]})
        raise
    reports._audit_render(session, user, report, request, "success", started.detail)
    return await _forward(request, "GET", f"/tasks/exports/{started.task_id}/file", session=session, user=user,
                          params={"owner": owner})


# ── for the form and the editor ───────────────────────────────────────────────
@router.get("/reports/{report_id}/parameters", response_model=list[ParameterOut])
async def report_parameters(report_id: int, request: Request, user: User = Depends(get_current_user),
                            session: Session = Depends(get_session)):
    """The parameters as the form shows them: defaults resolved, choices read (those of a
    dataset with the caller's rights, and the values of the query string for the other
    parameters, so that a choice can follow another)."""
    report = reports.get_visible(session, user, report_id)
    definition = reports.content_of(session, report)
    given = _query_params(request)
    params = definition.get("parameters") or []
    for name in given:
        if name not in {p["name"] for p in params}:
            raise report_params.bad_value(name, "the report has no parameter by this name")
    async def value_of(p: dict, one: dict) -> Any:
        try:
            return (await report_params.values_for({**definition, "parameters": [p]}, one))[p["name"]]
        except HTTPException:
            return [] if p.get("multi") else None

    # the values given steer the choices (one can follow another); the default shown is the
    # report's own, whatever was given
    values = {p["name"]: await value_of(p, {p["name"]: given[p["name"]]} if p["name"] in given else {})
              for p in params}
    defaults = {p["name"]: values[p["name"]] if p["name"] not in given else await value_of(p, {}) for p in params}
    out = []
    for p in params:
        item = ParameterOut(name=p["name"], label=p.get("label") or p["name"], type=p["type"],
                            multi=bool(p.get("multi")), required=bool(p.get("required")),
                            default=report_params.to_json(defaults[p["name"]]))
        choices = p.get("choices") or {}
        if "values" in choices:
            item.choices = [Choice(value=v, label=str(v)) for v in choices["values"]]
        elif choices.get("dataset"):
            try:
                datasets = await reports.resolve_datasets(session, user, definition, values, only={choices["dataset"]})
                got = await reports.dataset_choices(definition, datasets, values, p)
                if got is None:
                    item.choices_error = "The engine cannot read choices from a dataset yet"
                else:
                    item.choices = [Choice(value=c.get("value"), label=None if c.get("label") is None else str(c["label"]))
                                    for c in got["choices"]]
                    item.choices_truncated = got["truncated"]
            except HTTPException as e:
                item.choices_error = reports._text(e)[:500]
        out.append(item)
    return out


@router.post("/reports/{report_id}/fields", response_model=FieldsOut, response_model_exclude_none=True)
async def report_fields(report_id: int, body: FieldsRequest, user: User = Depends(get_current_user),
                        session: Session = Depends(get_session)):
    """A dataset's columns («Refresh fields» in the editor), read with the caller's
    rights: of the saved definition (VIEW), or of the editor's (EDIT); and its rows,
    counted at the same time, when they can be (never an error of their own)."""
    report = reports.get_visible(session, user, report_id)
    if body.definition is not None:
        ensure_can(session, user, report.project_id, Capability.EDIT)
        try:
            definition = report_definition.check_dataset(body.definition, body.dataset)
        except Problem as p:
            raise p.http(body.definition) from None
    else:
        definition = reports.content_of(session, report)
    slot = f"u{user.id}:report:{report.id}"
    counting = asyncio.ensure_future(reports.count_rows(await reports.to_count(session, user, definition, body.dataset),
                                                        f"{slot}:count"))
    try:
        found = await reports.fields(session, user, definition, body.dataset, f"{slot}:fields")
    except BaseException:
        counting.cancel()
        raise
    return FieldsOut(dataset=body.dataset, fields=[FieldOut(**f) for f in found], **(await counting or {}))
