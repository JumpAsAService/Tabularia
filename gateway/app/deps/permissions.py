"""Helpers to apply the projects' permissions inside the routes."""
from fastapi import HTTPException, status
from sqlmodel import Session

from app.models import User
from app.models.permission import Capability
from app.services import permissions as perm_service


def ensure_can(session: Session, user: User, project_id: int, capability: Capability) -> None:
    """Raises 403 when the user lacks the capability on the project (or its ancestors)."""
    if not perm_service.has_capability(session, user, project_id, capability):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Missing '{capability.value}' permission on folder {project_id}",
        )
