from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Project(SQLModel, table=True):
    """
    Cartella stile Tableau. Albero via `parent_id` (self-reference); `parent_id`
    None = progetto root. I permessi si applicano al progetto e vengono ereditati
    dai discendenti (vedi services/permissions.py).
    """
    __tablename__ = "projects"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    parent_id: Optional[int] = Field(default=None, foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")
    created_at: datetime = Field(default_factory=_now)
