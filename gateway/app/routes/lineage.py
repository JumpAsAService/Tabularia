"""Lineage cross-flow: grafo di provenienza/impatto tra flussi, datasource,
connessioni e destinazioni esterne. Derivato on-demand e filtrato per RBAC
(progetti leggibili). Vedi app/services/lineage.py per il modello del grafo.
"""
import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlmodel import Session

from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.models import Flow, User
from app.models.permission import Capability
from app.services import lineage as lineage_service
from app.services import openlineage
from app.core.routing import RottaCheRilascia

logger = logging.getLogger(__name__)

router = APIRouter(route_class=RottaCheRilascia, tags=["lineage"])


class LineageNode(BaseModel):
    id: str
    type: str                       # flow | datasource | connection | db_sink | s3_sink
    label: str
    project_id: Optional[int] = None
    kind: Optional[str] = None      # datasource: flow | database
    restricted: bool = False        # riferito ma fuori dai progetti leggibili / rimosso
    meta: dict = {}


class LineageEdge(BaseModel):
    source: str
    target: str
    kind: str                       # read | publish | ingest | write | refresh | orchestrate


class LineageGraph(BaseModel):
    nodes: list[LineageNode]
    edges: list[LineageEdge]
    center: Optional[str] = None


def _serialize(g: lineage_service._Graph, center: Optional[str]) -> LineageGraph:
    return LineageGraph(
        nodes=[LineageNode(**n) for n in g.nodes.values()],
        edges=[LineageEdge(**e) for e in g.edges],
        center=center,
    )


@router.get("/flows/{flow_id}/openlineage")
def flow_openlineage(flow_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> list[dict]:
    """Il lineage del flusso nello standard OpenLineage: un `JobEvent` con ciò che
    legge e scrive, ricostruito dalla definizione (senza eseguire). Da scaricare
    come file o da dare a un collector."""
    flow = session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Flusso non trovato")
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    return openlineage.to_json(openlineage.job_events_for_flow(session, flow))


@router.post("/flows/{flow_id}/openlineage", status_code=status.HTTP_202_ACCEPTED)
def emit_flow_openlineage(flow_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
    """Manda al collector configurato il lineage statico del flusso (per popolare
    un catalogo con i flussi che esistono già, senza aspettare che girino)."""
    flow = session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Flusso non trovato")
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    if not openlineage.enabled():
        raise HTTPException(status_code=409, detail="OpenLineage non è configurato (OPENLINEAGE__URL o OPENLINEAGE__FILE)")
    try:
        inviati = openlineage.emit_flow(session, flow)
    except Exception as e:  # noqa: BLE001 — chi lo chiede a mano vuole sapere perché non è andata
        raise HTTPException(status_code=502, detail=f"Il collector OpenLineage ha rifiutato l'invio: {str(e)[:300]}")
    return {"events": inviati}


@router.get("/lineage", response_model=LineageGraph)
def get_lineage(
    type: Optional[Literal["flow", "datasource"]] = Query(None, description="tipo dell'oggetto centrale"),
    id: Optional[int] = Query(None, description="id dell'oggetto centrale"),
    direction: Literal["both", "upstream", "downstream"] = "both",
    depth: int = Query(3, ge=1, le=8),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Grafo di lineage. Senza `type`+`id` restituisce il grafo COMPLETO sugli
    oggetti leggibili; con essi, il sottografo centrato entro `depth` salti nella
    direzione richiesta (a monte / a valle / entrambe)."""
    full = lineage_service.build_full_graph(session, user)

    if type is None or id is None:
        return _serialize(full, None)

    center = f"flow:{id}" if type == "flow" else f"ds:{id}"
    if center not in full.nodes:
        # l'oggetto non è leggibile o non esiste: 404 (non riveliamo l'esistenza
        # di oggetti fuori dai progetti dell'utente)
        raise HTTPException(status_code=404, detail="Oggetto non trovato o non accessibile")
    return _serialize(lineage_service.subgraph(full, center, direction, depth), center)
