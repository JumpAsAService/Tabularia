"""Lineage cross-flow: grafo di provenienza/impatto tra flussi, datasource,
connessioni e destinazioni esterne. Derivato on-demand e filtrato per RBAC
(progetti leggibili). Vedi app/services/lineage.py per il modello del grafo.
"""
import logging
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session

from app.db.session import get_session
from app.deps.auth import get_current_user
from app.models import User
from app.services import lineage as lineage_service

logger = logging.getLogger(__name__)

router = APIRouter(tags=["lineage"])


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
