"""Administration → Cluster: the pods of Tabularia and of its users' code, their
state, CPU and memory, the warning events (services/cluster_view.py). Read-only, for
administrators and observers."""
from fastapi import APIRouter, Depends
from sqlmodel import Session

from app.core.routing import RottaCheRilascia
from app.db.session import get_session
from app.deps.auth import require_observer
from app.services import cluster_view
from app.services.kube_scaling import KubeError

router = APIRouter(route_class=RottaCheRilascia, prefix="/admin/cluster", tags=["cluster"],
                   dependencies=[Depends(require_observer)])


@router.get("")
async def cluster(session: Session = Depends(get_session)) -> dict:
    """What runs now. `disabled_reason`: "kubernetes" off Kubernetes (Docker Compose),
    "off" when the chart's clusterView is off; `error` when the API did not answer."""
    reason = cluster_view.disabled_reason()
    if reason:
        return {"disabled_reason": reason}
    try:
        return {"disabled_reason": None, **await cluster_view.snapshot(session)}
    except KubeError as e:
        return {"disabled_reason": None, "error": str(e), "namespaces": [], "events": [], "metrics": False}
