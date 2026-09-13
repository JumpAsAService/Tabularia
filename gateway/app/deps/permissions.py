"""Helper per applicare i permessi sui progetti dentro le route."""
from fastapi import HTTPException, status
from sqlmodel import Session

from app.models import User
from app.models.permission import Capability
from app.services import permissions as perm_service


def ensure_can(session: Session, user: User, project_id: int, capability: Capability) -> None:
    """Solleva 403 se l'utente non ha la capability sul progetto (o suoi antenati)."""
    if not perm_service.has_capability(session, user, project_id, capability):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permesso '{capability.value}' mancante sul progetto {project_id}",
        )
