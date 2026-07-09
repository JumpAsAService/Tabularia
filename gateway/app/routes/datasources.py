"""Datasource nominate: il catalogo dei dataset riusabili come sorgenti.

Permessi ereditati dall'albero come flussi e progetti:
- lista/uso: VIEW sul progetto della datasource;
- rinomina/spostamento/eliminazione: EDIT (spostare richiede EDIT anche a destinazione).

Il gateway non tocca MAI lo storage direttamente (le credenziali S3 vivono solo
nell'engine): l'eliminazione del blob passa dall'endpoint interno dell'engine.
"""
import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, select

from app.core.engine_client import get_engine_client
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Datasource, Project, Run, User
from app.models.permission import Capability
from app.schemas.models import DatasourceOut, DatasourceUpdate
from app.services import permissions as perm_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["datasources"])


def _to_out(ds: Datasource) -> DatasourceOut:
    try:
        cols = json.loads(ds.columns or "[]")
    except json.JSONDecodeError:
        cols = []
    return DatasourceOut(**ds.model_dump(exclude={"columns"}), columns=cols)


def _get_ds(session: Session, ds_id: int) -> Datasource:
    ds = session.get(Datasource, ds_id)
    if ds is None:
        raise HTTPException(status_code=404, detail="Datasource non trovata")
    return ds


@router.get("/datasources", response_model=list[DatasourceOut])
def list_all_datasources(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """Tutte le datasource nei progetti LEGGIBILI (per il picker delle sorgenti).

    `readable_project_ids`, non `visible_project_ids`: gli antenati mostrati per
    navigazione non danno accesso al loro contenuto.
    """
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return []
    rows = session.exec(
        select(Datasource).where(Datasource.project_id.in_(readable)).order_by(Datasource.name)
    ).all()
    return [_to_out(d) for d in rows]


@router.get("/projects/{project_id}/datasources", response_model=list[DatasourceOut])
def list_project_datasources(
    project_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    if session.get(Project, project_id) is None:
        raise HTTPException(status_code=404, detail="Progetto non trovato")
    ensure_can(session, user, project_id, Capability.VIEW)
    rows = session.exec(
        select(Datasource).where(Datasource.project_id == project_id).order_by(Datasource.name)
    ).all()
    return [_to_out(d) for d in rows]


@router.patch("/datasources/{ds_id}", response_model=DatasourceOut)
def update_datasource(
    ds_id: int,
    body: DatasourceUpdate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    ds = _get_ds(session, ds_id)
    ensure_can(session, user, ds.project_id, Capability.EDIT)

    target_project = ds.project_id
    if body.project_id is not None and body.project_id != ds.project_id:
        if session.get(Project, body.project_id) is None:
            raise HTTPException(status_code=404, detail="Progetto di destinazione non trovato")
        ensure_can(session, user, body.project_id, Capability.EDIT)
        target_project = body.project_id

    new_name = body.name.strip() if body.name is not None else ds.name
    if not new_name:
        raise HTTPException(status_code=422, detail="Il nome non può essere vuoto")
    conflict = session.exec(
        select(Datasource).where(
            Datasource.project_id == target_project,
            Datasource.name == new_name,
            Datasource.id != ds.id,
        )
    ).first()
    if conflict:
        raise HTTPException(status_code=409, detail=f"Esiste già una datasource '{new_name}' nella cartella")

    ds.name = new_name
    ds.project_id = target_project
    if body.description is not None:
        ds.description = body.description
    ds.updated_at = datetime.now(timezone.utc)
    session.add(ds)
    session.commit()
    session.refresh(ds)
    return _to_out(ds)


@router.delete("/datasources/{ds_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_datasource(
    ds_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Elimina la voce di catalogo E il blob parquet (via engine).

    Ordine deliberato: PRIMA il catalogo (staccando i run che la referenziano),
    POI il blob best-effort. Un blob orfano nello storage è innocuo (spazio);
    una voce di catalogo che punta a un blob morto è un bug per l'utente.
    I flussi che la referenziano falliranno in preview con l'errore standard di
    sorgente mancante — comportamento coerente con la cancellazione dei dataset.
    """
    ds = _get_ds(session, ds_id)
    ensure_can(session, user, ds.project_id, Capability.EDIT)

    bucket, key = ds.bucket, ds.key
    # i run storici che l'hanno pubblicata restano, senza il riferimento
    for run in session.exec(select(Run).where(Run.datasource_id == ds.id)).all():
        run.datasource_id = None
        session.add(run)
    session.delete(ds)
    session.commit()

    client = get_engine_client()
    try:
        resp = await client.delete("/files/object", params={"bucket": bucket, "key": key})
        if resp.status_code >= 400:
            logger.warning("blob %s/%s non eliminato: %s", bucket, key, resp.text[:200])
    except Exception as e:  # engine giù: blob orfano, lo segnaliamo soltanto
        logger.warning("blob %s/%s non eliminato (engine irraggiungibile): %s", bucket, key, e)
