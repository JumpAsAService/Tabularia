"""Progetti/cartelle, con permessi ereditati lungo l'albero.

- lista: solo i progetti visibili all'utente (frontend costruisce l'albero);
- creazione: MANAGE sul parent (root → solo superuser);
- lettura: VIEW; modifica: EDIT; cancellazione: MANAGE (rifiutata se ha figli).
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Project, User
from app.models.permission import Capability
from app.services import openlineage
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
):
    if body.parent_id is None:
        # progetti root: solo il superuser può crearli (sono il livello di ingresso)
        if not perm_service.is_admin(session, user):
            raise HTTPException(status_code=403, detail="Solo un admin può creare progetti root")
    else:
        if session.get(Project, body.parent_id) is None:
            raise HTTPException(status_code=404, detail="Progetto parent non trovato")
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
    return project


def _get_project(session: Session, project_id: int) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="Progetto non trovato")
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
):
    project = _get_project(session, project_id)
    ensure_can(session, user, project_id, Capability.EDIT)
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
            raise HTTPException(status_code=404, detail="Nuovo parent non trovato")
        # Niente cicli: non dentro sé stesso e nemmeno dentro un proprio
        # DISCENDENTE, che staccherebbe il sottoalbero da ogni radice e
        # toglierebbe l'accesso a chi l'aveva per eredità (A6/M6).
        if body.parent_id == project_id or body.parent_id in descendant_ids(folder_parents(session), {project_id}):
            raise HTTPException(
                status_code=422,
                detail="Un progetto non può essere spostato dentro sé stesso o una sua sottocartella",
            )
        ensure_can(session, user, body.parent_id, Capability.MANAGE)
        project.parent_id = body.parent_id
    session.add(project)
    session.commit()
    session.refresh(project)
    openlineage.renamed(session, lineage_names)
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    project = _get_project(session, project_id)
    ensure_can(session, user, project_id, Capability.MANAGE)
    has_children = session.exec(select(Project).where(Project.parent_id == project_id)).first()
    if has_children:
        raise HTTPException(status_code=409, detail="Il progetto contiene sotto-cartelle: svuotalo prima")
    from app.models import Permission, Flow, Datasource

    has_flows = session.exec(select(Flow).where(Flow.project_id == project_id)).first()
    if has_flows:
        raise HTTPException(status_code=409, detail="Il progetto contiene flussi: spostali o eliminali prima")
    has_ds = session.exec(select(Datasource).where(Datasource.project_id == project_id)).first()
    if has_ds:
        raise HTTPException(status_code=409, detail="Il progetto contiene datasource: spostale o eliminale prima")
    from app.models import Connection

    has_conn = session.exec(select(Connection).where(Connection.project_id == project_id)).first()
    if has_conn:
        raise HTTPException(status_code=409, detail="Il progetto contiene connessioni: spostale o eliminale prima")
    # saved views and flow groups hold a foreign key to their folder: without this
    # check the delete ends in a 500 instead of saying what is in the way
    from app.models import FlowGroup, SavedView

    if session.exec(select(SavedView).where(SavedView.project_id == project_id)).first():
        raise HTTPException(status_code=409, detail="The folder contains saved views: move or delete them first")
    if session.exec(select(FlowGroup).where(FlowGroup.project_id == project_id)).first():
        raise HTTPException(status_code=409, detail="The folder contains flow groups: move or delete them first")
    # un webhook limitato a questa cartella: toglierla in silenzio lo allargherebbe
    # a tutte, e la chiave esterna lo impedirebbe comunque con un 500
    from app.models import Webhook

    osservata = session.exec(select(Webhook).where(Webhook.project_id == project_id)).first()
    if osservata:
        raise HTTPException(
            status_code=409,
            detail=f"La cartella è quella del webhook «{osservata.name}»: cambiane la cartella o toglilo prima",
        )

    # run storici che avevano questo progetto come DESTINAZIONE di publish
    # (vivono sotto flussi di altri progetti): staccali, la cronologia resta
    from sqlalchemy import update as sa_update

    from app.models import Run

    session.exec(sa_update(Run).where(Run.publish_project_id == project_id).values(publish_project_id=None))

    for perm in session.exec(select(Permission).where(Permission.project_id == project_id)).all():
        session.delete(perm)
    session.delete(project)
    session.commit()
