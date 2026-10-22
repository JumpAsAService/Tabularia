"""Progetti/cartelle, con permessi ereditati lungo l'albero.

- lista: solo i progetti visibili all'utente (frontend costruisce l'albero);
- creazione: MANAGE sul parent (root → solo superuser);
- lettura: VIEW; modifica: EDIT; cancellazione: MANAGE (rifiutata se ha figli).
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
from app.core.routing import RottaCheRilascia

router = APIRouter(route_class=RottaCheRilascia, prefix="/projects", tags=["projects"])


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
        # progetti root: solo il superuser può crearli (sono il livello di ingresso)
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
    prima = {"name": project.name, "description": project.description, "parent_id": project.parent_id}
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
        # Spostare = MANAGE su ENTRAMBI i capi. Su quello spostato perché i
        # permessi si ereditano verso il basso: chi ha solo EDIT su una cartella
        # e MANAGE sulla propria sandbox, spostandola dentro la sandbox si
        # ritroverebbe MANAGE (e quindi CONNECT, che EDIT non concede mai) su
        # tutto il sottoalbero — e potrebbe poi autoconcedersi un permesso
        # esplicito che sopravvive al ripristino dell'albero.
        # Audit 2026-09-19, A1: riprodotto, da 403 a 201.
        ensure_can(session, user, project_id, Capability.MANAGE)
        if session.get(Project, body.parent_id) is None:
            raise HTTPException(status_code=404, detail="New parent folder not found")
        # Niente cicli: non dentro sé stesso e nemmeno dentro un proprio
        # DISCENDENTE, che staccherebbe il sottoalbero da ogni radice e
        # toglierebbe l'accesso a chi l'aveva per eredità (A6/M6).
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
    cambiati = {k: {"from": v, "to": getattr(project, k)} for k, v in prima.items()
                if k != "parent_id" and getattr(project, k) != v}
    if cambiati:
        audit.record_audit(session, actor=user, action=audit.FOLDER_UPDATE, request=request, target_type="folder",
                           target_id=project_id, target_label=project.name, detail=cambiati)
    if project.parent_id != prima["parent_id"]:
        audit.record_audit(session, actor=user, action=audit.FOLDER_MOVE, request=request, target_type="folder",
                           target_id=project_id, target_label=project.name,
                           detail={"from_parent": prima["parent_id"], "to_parent": project.parent_id})
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
    # un webhook limitato a questa cartella: toglierla in silenzio lo allargherebbe
    # a tutte, e la chiave esterna lo impedirebbe comunque con un 500
    from app.models import Webhook

    osservata = session.exec(select(Webhook).where(Webhook.project_id == project_id)).first()
    if osservata:
        raise HTTPException(
            status_code=409,
            detail=f"The webhook «{osservata.name}» is limited to this folder: change its folder or delete it first",
        )

    # run storici che avevano questo progetto come DESTINAZIONE di publish
    # (vivono sotto flussi di altri progetti): staccali, la cronologia resta
    from sqlalchemy import update as sa_update

    from app.models import Run

    session.exec(sa_update(Run).where(Run.publish_project_id == project_id).values(publish_project_id=None))

    for perm in session.exec(select(Permission).where(Permission.project_id == project_id)).all():
        session.delete(perm)
    nome, genitore = project.name, project.parent_id
    session.delete(project)
    session.commit()
    audit.record_audit(session, actor=user, action=audit.FOLDER_DELETE, request=request, target_type="folder",
                       target_id=project_id, target_label=nome, detail={"parent_id": genitore})
