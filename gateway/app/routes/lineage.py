"""Cross-flow lineage: the provenance/impact graph among flows, datasources, connections
and external destinations. Derived on demand and filtered by RBAC (readable
projects). See app/services/lineage.py for the graph's model.
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
from app.core.routing import ReleasingRoute

logger = logging.getLogger(__name__)

router = APIRouter(route_class=ReleasingRoute, tags=["lineage"])


class LineageNode(BaseModel):
    id: str
    type: str                       # flow | datasource | connection | script | db_sink | s3_sink | sftp_sink
    label: str
    project_id: Optional[int] = None
    kind: Optional[str] = None      # datasource: flow | database
    restricted: bool = False        # referenced but outside the readable projects / removed
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
    """The flow's lineage in the OpenLineage standard: a `JobEvent` with what it reads and
    writes, rebuilt from the definition (without running). To download as a file or to
    give to a collector."""
    flow = session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    return openlineage.to_json(openlineage.job_events_for_flow(session, flow))


@router.post("/flows/{flow_id}/openlineage", status_code=status.HTTP_202_ACCEPTED)
def emit_flow_openlineage(flow_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> dict:
    """Sends the flow's static lineage to the configured collector (to fill a catalog with
    the flows that exist already, without waiting for them to run)."""
    flow = session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    ensure_can(session, user, flow.project_id, Capability.VIEW)
    if not openlineage.enabled():
        raise HTTPException(status_code=409, detail="OpenLineage is not configured (OPENLINEAGE__URL or OPENLINEAGE__FILE)")
    try:
        sent = openlineage.emit_flow(session, flow)
    except Exception as e:  # noqa: BLE001 — whoever asks for it by hand wants to know why it did not go
        raise HTTPException(status_code=502, detail=f"The OpenLineage collector rejected the events: {str(e)[:300]}")
    return {"events": sent}


@router.get("/lineage", response_model=LineageGraph)
def get_lineage(
    type: Optional[Literal["flow", "datasource", "connection", "script"]] = Query(None, description="kind of the central object"),
    id: Optional[int] = Query(None, description="id of the central object"),
    direction: Literal["both", "upstream", "downstream"] = "both",
    depth: int = Query(3, ge=1, le=8),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The lineage graph. Without `type`+`id` it returns the WHOLE graph over the readable
    objects; with them, the subgraph centred within `depth` hops in the requested
    direction (upstream / downstream / both)."""
    full = lineage_service.build_full_graph(session, user)

    if type is None or id is None:
        return _serialize(full, None)

    center = {"flow": "flow", "datasource": "ds", "connection": "conn", "script": "script"}[type] + f":{id}"
    if center not in full.nodes:
        # the object is not readable or does not exist: 404 (we do not reveal the existence of
        # objects outside the user's projects)
        raise HTTPException(status_code=404, detail="Object not found or not accessible")
    return _serialize(lineage_service.subgraph(full, center, direction, depth), center)
