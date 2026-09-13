"""Viste salvate: configurazioni del Viewer messe nel catalogo, in una cartella.

Permessi, identici a quelli dei flussi perché l'oggetto è della stessa natura:
- elenco e apertura: **VIEW** sulla cartella della vista;
- creazione, modifica, spostamento, eliminazione: **EDIT**;
- lo spostamento richiede EDIT sia sull'origine sia sulla destinazione.

La vista contiene solo un riferimento alla datasource e la configurazione: i
dati non passano mai di qui. L'RBAC sulla LETTURA del dato resta quella del data
plane (il Viewer chiama la preview, che autorizza le chiavi di storage), quindi
salvare una vista non concede l'accesso a nulla. Si verifica comunque VIEW sulla
datasource al momento del salvataggio, per fallire subito e con un messaggio
chiaro invece che più tardi, davanti a chi apre la vista.
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
from app.services import audit
from app.services import permissions as perm_service

router = APIRouter(tags=["saved-views"])


def _get_view(session: Session, view_id: int) -> SavedView:
    view = session.get(SavedView, view_id)
    if view is None:
        raise HTTPException(status_code=404, detail="Vista non trovata")
    return view


def _name_taken(session: Session, project_id: int, name: str, exclude_id: int | None = None) -> bool:
    stmt = select(SavedView).where(SavedView.project_id == project_id, SavedView.name == name)
    if exclude_id is not None:
        stmt = stmt.where(SavedView.id != exclude_id)
    return session.exec(stmt).first() is not None


def _valid_spec(spec: str) -> str:
    """La configurazione è testo opaco, ma dev'essere JSON: salvarne una
    malformata trasformerebbe un errore di salvataggio in una vista che non si
    apre più, cioè in un problema di chi la apre invece che di chi l'ha rotta."""
    try:
        json.loads(spec or "{}")
    except json.JSONDecodeError:
        raise HTTPException(status_code=422, detail="La configurazione della vista non è JSON valido")
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
    """Risolve i nomi delle datasource con UNA query per l'intero elenco."""
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
    """Tutte le viste nelle cartelle LEGGIBILI (per il picker del Viewer)."""
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
        raise HTTPException(status_code=404, detail="Progetto non trovato")
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
        raise HTTPException(status_code=404, detail="Progetto non trovato")
    ensure_can(session, user, project_id, Capability.EDIT)

    ds = session.get(Datasource, body.datasource_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="Datasource non trovata")
    # fallire QUI, con un messaggio chiaro, invece che davanti a chi apre la vista
    ensure_can(session, user, ds.project_id, Capability.VIEW)

    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="Il nome della vista è vuoto")
    if _name_taken(session, project_id, name):
        raise HTTPException(status_code=409, detail=f"Esiste già una vista '{name}' nella cartella")

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
            raise HTTPException(status_code=404, detail="Progetto di destinazione non trovato")
        ensure_can(session, user, body.project_id, Capability.EDIT)
        target_project = body.project_id

    new_name = body.name.strip() if body.name is not None else view.name
    if not new_name:
        raise HTTPException(status_code=422, detail="Il nome non può essere vuoto")
    if _name_taken(session, target_project, new_name, exclude_id=view.id):
        raise HTTPException(status_code=409, detail=f"Esiste già una vista '{new_name}' nella cartella")

    view.name = new_name
    view.project_id = target_project
    if body.description is not None:
        view.description = body.description
    if body.spec is not None:
        view.spec = _valid_spec(body.spec)
    view.updated_at = datetime.now(timezone.utc)
    session.add(view)
    session.commit()
    session.refresh(view)
    audit.record_audit(
        session, actor=user, action=audit.SAVED_VIEW_UPDATE, target_type="saved_view",
        target_id=view.id, target_label=view.name, request=request,
    )
    return _to_out(session, view)


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
