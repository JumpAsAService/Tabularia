"""Schemi di request/response del gateway. Separati dai modelli DB per non
esporre mai `hashed_password` e per validare gli input."""
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field

from app.models.permission import Capability


# Nota: l'email è un semplice `str`, non `EmailStr`. È uno strumento interno: gli
# admin usano spesso domini riservati (es. *.local, *.internal) che il validatore
# di deliverability rifiuterebbe. Qui l'email è solo un identificativo di login.


# ── Auth ──────────────────────────────────────────────────────────────────────
class LoginRequest(BaseModel):
    email: str
    password: str


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


# ── Users ─────────────────────────────────────────────────────────────────────
class UserOut(BaseModel):
    id: int
    email: str
    full_name: str
    is_active: bool
    is_superuser: bool


class MeOut(UserOut):
    groups: list[str] = []


class UserCreate(BaseModel):
    email: str
    password: str = Field(min_length=6)
    full_name: str = ""
    is_superuser: bool = False


class UserUpdate(BaseModel):
    full_name: Optional[str] = None
    password: Optional[str] = Field(default=None, min_length=6)
    is_active: Optional[bool] = None
    is_superuser: Optional[bool] = None


# ── Groups ────────────────────────────────────────────────────────────────────
class GroupOut(BaseModel):
    id: int
    name: str
    description: str


class GroupCreate(BaseModel):
    name: str
    description: str = ""


# ── Projects ──────────────────────────────────────────────────────────────────
class ProjectOut(BaseModel):
    id: int
    name: str
    description: str
    parent_id: Optional[int]
    owner_id: Optional[int]


class ProjectCreate(BaseModel):
    name: str
    description: str = ""
    parent_id: Optional[int] = None


class ProjectUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    parent_id: Optional[int] = None


# ── Flows ─────────────────────────────────────────────────────────────────────
class FlowOut(BaseModel):
    """Voce di lista: senza `definition` (può pesare, serve solo all'editor)."""
    id: int
    name: str
    description: str
    project_id: int
    owner_id: Optional[int]
    updated_at: Optional[datetime] = None


class FlowDetail(FlowOut):
    definition: str


class FlowCreate(BaseModel):
    name: str
    description: str = ""
    definition: str = "{}"


class FlowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    definition: Optional[str] = None
    project_id: Optional[int] = None  # valorizzato = sposta in un'altra cartella


# ── Permissions ───────────────────────────────────────────────────────────────
class PermissionOut(BaseModel):
    id: int
    project_id: int
    user_id: Optional[int]
    group_id: Optional[int]
    capability: str


class PermissionCreate(BaseModel):
    capability: Capability
    user_id: Optional[int] = None
    group_id: Optional[int] = None
