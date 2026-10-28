"""Projects/folders, with permissions inherited down the tree.

- list: only the projects visible to the user (the frontend builds the tree);
- creation: MANAGE on the parent (root → superuser only);
- read: VIEW; change: EDIT; deletion: MANAGE (refused when it has children).
"""
from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlmodel import Session, select

from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Project, User
from app.models.permission import Capability
from app.services import audit, openlineage
from app.services import permissions as perm_service
from app.services.permissions import descendant_ids, folder_parents
from app.schemas.models import ProjectOut, ProjectCreate, ProjectUpdate
from app.core.routing import ReleasingRoute

router = APIRouter(route_class=ReleasingRoute, prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
def list_projects(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    return perm_service.visible_projects(session, user)


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    body: ProjectCreate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
    request: Request = None,  # type: ignore[assignment]
):
    if body.parent_id is None:
        # root projects: only the superuser may create them (they are the entry level)
        if not perm_service.is_admin(session, user):
            raise HTTPException(status_code=403, detail="Only an admin can create top-level folders")
    else:
        if session.get(Project, body.parent_id) is None:
            raise HTTPException(status_code=404, detail="Parent folder not found")
        ensure_can(session, user, body.parent_id, Capability.MANAGE)

    project = Project(
        name=body.name,
        description=body.description,
        parent_id=body.parent_id,
        owner_id=user.id,
    )
    session.add(project)
    session.commit()
    session.refresh(project)
    audit.record_audit(session, actor=user, action=audit.FOLDER_CREATE, request=request, target_type="folder",
                       target_id=project.id, target_label=project.name, detail={"parent_id": project.parent_id})
    session.refresh(project)
    return project


def _get_project(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Folder not found")
    return project


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    project = _get_project(session, project_id)
    ensure_can(session, user, project_id, Capability.VIEW)
    return project


@router.patch("/{project_id}", response_model=ProjectOut)
def update_project(
    project_id: int,
    body: ProjectUpdate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
    request: Request = None,  # type: ignore[assignment]
):
    project = _get_project(session, project_id)
    ensure_can(session, user, project_id, Capability.EDIT)
    before = {"name": project.name, "description": project.description, "parent_id": project.parent_id}
    # the folder path is part of the names its flows and datasources have in an
    # external lineage catalog: renaming or moving it renames them all there
    moved = body.parent_id is not None and body.parent_id != project.parent_id
    lineage_names = openlineage.folder_names(session, project_id) if body.name is not None or moved else {}
    if body.name is not None:
        name = body.name.strip()
        if not name:
            raise HTTPException(status_code=422, detail="The folder needs a name")
        project.name = name
    if body.description is not None:
        project.description = body.description
    if body.parent_id is not None and body.parent_id != project.parent_id:
        # Moving = MANAGE on BOTH ends. On the moved one because permissions are inherited
        # downwards: whoever has only EDIT on a folder and MANAGE on their own sandbox, moving
        # it into the sandbox, would find themselves with MANAGE (and so CONNECT, which EDIT
        # never grants) on the whole subtree — and could then grant themselves an explicit
        # permission that survives restoring the tree.
        # Audit 2026-09-19, A1: reproduced, from 403 to 201.
        ensure_can(session, user, project_id, Capability.MANAGE)
        if session.get(Project, body.parent_id) is None:
            raise HTTPException(status_code=404, detail="New parent folder not found")
        # No cycles: not inside itself and not inside one of its own DESCENDANTS either, which
        # would detach the subtree from every root and take access away from whoever had it by
        # inheritance (A6/M6).
        if body.parent_id == project_id or body.parent_id in descendant_ids(folder_parents(session), {project_id}):
            raise HTTPException(
                status_code=422,
                detail="A folder cannot be moved into itself or into one of its subfolders",
            )
        ensure_can(session, user, body.parent_id, Capability.MANAGE)
        project.parent_id = body.parent_id
    session.add(project)
    session.commit()
    session.refresh(project)
    openlineage.renamed(session, lineage_names)
    changed_ones = {k: {"from": v, "to": getattr(project, k)} for k, v in before.items()
                if k != "parent_id" and getattr(project, k) != v}
    if changed_ones:
        audit.record_audit(session, actor=user, action=audit.FOLDER_UPDATE, request=request, target_type="folder",
                           target_id=project_id, target_label=project.name, detail=changed_ones)
    if project.parent_id != before["parent_id"]:
        audit.record_audit(session, actor=user, action=audit.FOLDER_MOVE, request=request, target_type="folder",
                           target_id=project_id, target_label=project.name,
                           detail={"from_parent": before["parent_id"], "to_parent": project.parent_id})
    session.refresh(project)
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(
    project_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
    request: Request = None,  # type: ignore[assignment]
):
    project = _get_project(session, project_id)
    ensure_can(session, user, project_id, Capability.MANAGE)
    has_children = session.exec(select(Project).where(Project.parent_id == project_id)).first()
    if has_children:
        raise HTTPException(status_code=409, detail="The folder contains subfolders: empty it first")
    from app.models import Permission, Flow, Datasource

    has_flows = session.exec(select(Flow).where(Flow.project_id == project_id)).first()
    if has_flows:
        raise HTTPException(status_code=409, detail="The folder contains flows: move or delete them first")
    has_ds = session.exec(select(Datasource).where(Datasource.project_id == project_id)).first()
    if has_ds:
        raise HTTPException(status_code=409, detail="The folder contains datasources: move or delete them first")
    from app.models import Connection

    has_conn = session.exec(select(Connection).where(Connection.project_id == project_id)).first()
    if has_conn:
        raise HTTPException(status_code=409, detail="The folder contains connections: move or delete them first")
    # saved views and flow groups hold a foreign key to their folder: without this
    # check the delete ends in a 500 instead of saying what is in the way
    from app.models import FlowGroup, SavedView

    if session.exec(select(SavedView).where(SavedView.project_id == project_id)).first():
        raise HTTPException(status_code=409, detail="The folder contains saved views: move or delete them first")
    if session.exec(select(FlowGroup).where(FlowGroup.project_id == project_id)).first():
        raise HTTPException(status_code=409, detail="The folder contains flow groups: move or delete them first")
    from app.models import SavedScript

    if session.exec(select(SavedScript).where(SavedScript.project_id == project_id)).first():
        raise HTTPException(status_code=409, detail="The folder contains scripts: move or delete them first")
    from app.models import SqlScript

    if session.exec(select(SqlScript).where(SqlScript.project_id == project_id)).first():
        raise HTTPException(status_code=409, detail="The folder contains SQL scripts: move or delete them first")
    from app.models import Notebook

    if session.exec(select(Notebook).where(Notebook.project_id == project_id)).first():
        raise HTTPException(status_code=409, detail="The folder contains notebooks: move or delete them first")
    from app.models import Report

    if session.exec(select(Report).where(Report.project_id == project_id)).first():
        raise HTTPException(status_code=409, detail="The folder contains reports: move or delete them first")
    # a webhook limited to this folder: removing it silently would widen it to all, and the
    # foreign key would prevent it anyway with a 500
    from app.models import Webhook

    observed = session.exec(select(Webhook).where(Webhook.project_id == project_id)).first()
    if observed:
        raise HTTPException(
            status_code=409,
            detail=f"The webhook «{observed.name}» is limited to this folder: change its folder or delete it first",
        )

    # historical runs that had this project as publish DESTINATION (they live under flows
    # of other projects): detach them, the history stays
    from sqlalchemy import update as sa_update

    from app.models import Run

    session.exec(sa_update(Run).where(Run.publish_project_id == project_id).values(publish_project_id=None))

    for perm in session.exec(select(Permission).where(Permission.project_id == project_id)).all():
        session.delete(perm)
    name, parent = project.name, project.parent_id
    session.delete(project)
    session.commit()
    audit.record_audit(session, actor=user, action=audit.FOLDER_DELETE, request=request, target_type="folder",
                       target_id=project_id, target_label=name, detail={"parent_id": parent})
