"""Progetti/cartelle stile Tableau, con permessi ereditati lungo l'albero.

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
from app.services import permissions as perm_service
from app.schemas.models import ProjectOut, ProjectCreate, ProjectUpdate

router = APIRouter(prefix="/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
def list_projects(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    visible = perm_service.visible_project_ids(session, user)
    if not visible:
        return []
    return session.exec(select(Project).where(Project.id.in_(visible))).all()


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
def create_project(
    body: ProjectCreate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if body.parent_id is None:
        # progetti root: solo il superuser può crearli (sono il livello di ingresso)
        if not user.is_superuser:
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
    if body.name is not None:
        project.name = body.name
    if body.description is not None:
        project.description = body.description
    if body.parent_id is not None and body.parent_id != project.parent_id:
        # spostare un progetto = MANAGE sulla nuova destinazione; niente cicli
        if body.parent_id == project_id:
            raise HTTPException(status_code=422, detail="Un progetto non può essere figlio di sé stesso")
        if session.get(Project, body.parent_id) is None:
            raise HTTPException(status_code=404, detail="Nuovo parent non trovato")
        ensure_can(session, user, body.parent_id, Capability.MANAGE)
        project.parent_id = body.parent_id
    session.add(project)
    session.commit()
    session.refresh(project)
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    project = _get_project(session, project_id)
    ensure_can(session, user, project_id, Capability.MANAGE)
    has_children = session.exec(select(Project).where(Project.parent_id == project_id)).first()
    if has_children:
        raise HTTPException(status_code=409, detail="Il progetto contiene sotto-cartelle: svuotalo prima")
    from app.models import Permission, Flow

    has_flows = session.exec(select(Flow).where(Flow.project_id == project_id)).first()
    if has_flows:
        raise HTTPException(status_code=409, detail="Il progetto contiene flussi: spostali o eliminali prima")

    for perm in session.exec(select(Permission).where(Permission.project_id == project_id)).all():
        session.delete(perm)
    session.delete(project)
    session.commit()
