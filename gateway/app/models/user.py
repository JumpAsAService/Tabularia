from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserGroupLink(SQLModel, table=True):
    """Appartenenza utente↔gruppo (many-to-many)."""
    __tablename__ = "user_groups"
    user_id: int = Field(foreign_key="users.id", primary_key=True)
    group_id: int = Field(foreign_key="groups.id", primary_key=True)


class User(SQLModel, table=True):
    __tablename__ = "users"
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    full_name: str = ""
    hashed_password: str
    is_active: bool = True
    is_superuser: bool = False
    created_at: datetime = Field(default_factory=_now)
    # ultima attività autenticata (per le "sessioni attive" dell'audit): il JWT è
    # stateless, quindi tracciamo l'ultimo istante/IP visti (aggiornati con
    # throttling nel dependency di auth, non a ogni richiesta).
    last_seen_at: Optional[datetime] = Field(default=None, index=True)
    last_seen_ip: Optional[str] = None


class Group(SQLModel, table=True):
    __tablename__ = "groups"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    description: str = ""
    created_at: datetime = Field(default_factory=_now)
