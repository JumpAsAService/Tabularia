"""Saved views: Viewer configurations put in the catalog, in a folder.

Permissions, the same as the flows' because the object is of the same nature:
- list and open: **VIEW** on the view's folder;
- create, change, move, delete: **EDIT**;
- moving requires EDIT on both the origin and the destination.

The view holds only a reference to the datasource and the configuration: the data
never goes through here. The RBAC on READING the data stays the data plane's (the
Viewer calls the preview, which authorizes the storage keys), so saving a view grants
access to nothing. VIEW on the datasource is checked anyway at save time, to fail at
once and with a clear message instead of later, in front of whoever opens the view.
"""
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session, select

from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Datasource, Project, SavedView, User
from app.models.permission import Capability
from app.schemas.models import SavedViewCreate, SavedViewOut, SavedViewUpdate
from app.services import audit, reports
from app.services import permissions as perm_service
from app.services.copy_names import commit_copy, free_copy_name
from app.core.routing import ReleasingRoute

router = APIRouter(route_class=ReleasingRoute, tags=["saved-views"])


def _get_view(session: Session, view_id: int) -> SavedView:
    view = session.get(SavedView, view_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Saved view not found")
    return view


def _name_taken(session: Session, project_id: int, name: str, exclude_id: int | None = None) -> bool:
    stmt = select(SavedView).where(SavedView.project_id == project_id, SavedView.name == name)
    if exclude_id is not None:
        stmt = stmt.where(SavedView.id != exclude_id)
    return session.exec(stmt).first() is not None


def _valid_spec(spec: str) -> str:
    """The configuration is opaque text, but it must be JSON: saving a malformed one would
    turn a saving error into a view that no longer opens, that is into a problem of
    whoever opens it instead of whoever broke it."""
    try:
        json.loads(spec or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="The view configuration is not valid JSON")
    return spec or "{}"


def _to_out(session: Session, view: SavedView, ds_names: dict[int, str] | None = None) -> SavedViewOut:
    if ds_names is None:
        ds = session.get(Datasource, view.datasource_id)
        name = ds.name if ds else None
    else:
        name = ds_names.get(view.datasource_id)
    fields = {f: getattr(view, f) for f in SavedViewOut.model_fields if f != "datasource_name"}
    return SavedViewOut(**fields, datasource_name=name)


def _with_datasource_names(session: Session, views: list[SavedView]) -> list[SavedViewOut]:
    """Resolves the datasources' names with ONE query for the whole list."""
    ids = {v.datasource_id for v in views}
    names: dict[int, str] = {}
    if ids:
        for did, dname in session.exec(
            select(Datasource.id, Datasource.name).where(Datasource.id.in_(ids))
        ).all():
            names[did] = dname
    return [_to_out(session, v, names) for v in views]


@router.get("/saved-views", response_model=list[SavedViewOut])
def list_all_saved_views(
    user: User = Depends(get_current_user), session: Session = Depends(get_session)
):
    """All the views in the READABLE folders (for the Viewer's picker)."""
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return []
    views = session.exec(
        select(SavedView).where(SavedView.project_id.in_(readable)).order_by(SavedView.name)
    ).all()
    return _with_datasource_names(session, list(views))


@router.get("/projects/{project_id}/saved-views", response_model=list[SavedViewOut])
def list_project_saved_views(
    project_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.VIEW)
    views = session.exec(
        select(SavedView).where(SavedView.project_id == project_id).order_by(SavedView.name)
    ).all()
    return _with_datasource_names(session, list(views))


@router.get("/saved-views/{view_id}", response_model=SavedViewOut)
def get_saved_view(
    view_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    view = _get_view(session, view_id)
    ensure_can(session, user, view.project_id, Capability.VIEW)
    return _to_out(session, view)


@router.post(
    "/projects/{project_id}/saved-views",
    response_model=SavedViewOut,
    status_code=status.HTTP_201_CREATED,
)
def create_saved_view(
    project_id: int,
    body: SavedViewCreate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    ensure_can(session, user, project_id, Capability.EDIT)

    ds = session.get(Datasource, body.datasource_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="Datasource not found")
    # fail HERE, with a clear message, instead of in front of whoever opens the view
    ensure_can(session, user, ds.project_id, Capability.VIEW)

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="The saved view needs a name")
    if _name_taken(session, project_id, name):
        raise HTTPException(status_code=409, detail=f"There is already a saved view '{name}' in this folder")

    view = SavedView(
        name=name,
        description=body.description,
        project_id=project_id,
        owner_id=user.id,
        datasource_id=ds.id,
        spec=_valid_spec(body.spec),
    )
    session.add(view)
    session.commit()
    session.refresh(view)
    audit.record_audit(
        session, actor=user, action=audit.SAVED_VIEW_CREATE, target_type="saved_view",
        target_id=view.id, target_label=view.name,
        detail={"project_id": project_id, "datasource_id": ds.id}, request=request,
    )
    return _to_out(session, view)


@router.post("/saved-views/{view_id}/duplicate", response_model=SavedViewOut, status_code=status.HTTP_201_CREATED)
def duplicate_saved_view(
    view_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """A copy of the view in its folder, as «name (i)», on the same datasource."""
    src = _get_view(session, view_id)
    ensure_can(session, user, src.project_id, Capability.EDIT)
    ds = session.get(Datasource, src.datasource_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="Datasource not found")
    ensure_can(session, user, ds.project_id, Capability.VIEW)
    view = SavedView(
        name=free_copy_name(session, SavedView, src.project_id, src.name),
        description=src.description,
        project_id=src.project_id,
        owner_id=user.id,
        datasource_id=ds.id,
        spec=src.spec,
    )
    session.add(view)
    commit_copy(session)
    session.refresh(view)
    audit.record_audit(
        session, actor=user, action=audit.SAVED_VIEW_CREATE, target_type="saved_view",
        target_id=view.id, target_label=view.name,
        detail={"project_id": view.project_id, "datasource_id": ds.id, "duplicate_of": src.id}, request=request,
    )
    return _to_out(session, view)


@router.patch("/saved-views/{view_id}", response_model=SavedViewOut)
def update_saved_view(
    view_id: int,
    body: SavedViewUpdate,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    view = _get_view(session, view_id)
    ensure_can(session, user, view.project_id, Capability.EDIT)

    target_project = view.project_id
    if body.project_id is not None and body.project_id != view.project_id:
        if session.get(Project, body.project_id) is None:
            raise HTTPException(status_code=404, detail="Destination folder not found")
        ensure_can(session, user, body.project_id, Capability.EDIT)
        target_project = body.project_id

    new_name = body.name.strip() if body.name is not None else view.name
    if not new_name:
        raise HTTPException(status_code=422, detail="The name cannot be empty")
    if _name_taken(session, target_project, new_name, exclude_id=view.id):
        raise HTTPException(status_code=409, detail=f"There is already a saved view '{new_name}' in this folder")

    view.name = new_name
    view.project_id = target_project
    if body.description is not None:
        view.description = body.description
    groups_off: list[tuple] = []
    if body.spec is not None:
        spec = _valid_spec(body.spec)
        groups_off = _groups_off(session, user, view, view.spec, spec)
        view.spec = spec
    view.updated_at = datetime.now(timezone.utc)
    session.add(view)
    session.commit()
    session.refresh(view)
    audit.record_audit(
        session, actor=user, action=audit.SAVED_VIEW_UPDATE, target_type="saved_view",
        target_id=view.id, target_label=view.name, request=request,
    )
    reports.record_groups_off(session, user, groups_off, request)
    return _to_out(session, view)


def _groups_off(session: Session, user: User, view: SavedView, before: str, after: str) -> list[tuple]:
    """The flow groups that send a report reading the view, scheduled by someone else: a
    change of what the report reads through it (its filters, computed fields, pivot,
    engine) switches their schedules off — a schedule carries its author's rights, and
    those are not spent on someone else's choice. The rest of the view (its chart, its
    columns' order) the report does not read."""
    ds = session.get(Datasource, view.datasource_id)

    def read(spec: str) -> tuple:
        try:
            value = json.loads(spec or "{}")
        except ValueError:
            value = {}
        value = value if isinstance(value, dict) else {}
        return (reports.view_operations(value, ds), value.get("engine")) if ds is not None else ()

    if read(before) == read(after):
        return []
    return reports.turn_off_groups(session, user, [
        (g, f"the saved view '{view.name}' its report '{r.name}' reads changed: it reads something else")
        for r, g in reports.groups_reading(session, "view", view.id)])


@router.delete("/saved-views/{view_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_saved_view(
    view_id: int,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    view = _get_view(session, view_id)
    ensure_can(session, user, view.project_id, Capability.EDIT)
    label = view.name
    session.delete(view)
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.SAVED_VIEW_DELETE, target_type="saved_view",
        target_id=view_id, target_label=label, request=request,
    )
