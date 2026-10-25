import enum
from typing import Optional
from sqlmodel import SQLModel, Field


class Capability(str, enum.Enum):
    """
    What a subject may do on a project. Hierarchy (whoever may do more may do less):
    VIEW < RUN < EDIT < MANAGE. CONNECT is orthogonal (using/creating data connections).
    MANAGE includes everything.
    """
    VIEW = "view"       # see the project and its content
    RUN = "run"         # run the flows
    EDIT = "edit"       # create/change content
    CONNECT = "connect" # use/manage data connections
    MANAGE = "manage"   # manage the project and its permissions


# rank for the linear implication; CONNECT stays out (orthogonal)
_RANK = {Capability.VIEW: 0, Capability.RUN: 1, Capability.EDIT: 2, Capability.MANAGE: 3}


def grant_satisfies(granted: str, needed: str) -> bool:
    """Does a `granted` permission satisfy the `needed` capability?"""
    g, n = Capability(granted), Capability(needed)
    if g is Capability.MANAGE:
        return True
    if n is Capability.CONNECT:
        return g is Capability.CONNECT
    if g is Capability.CONNECT:
        return n is Capability.CONNECT
    return _RANK[g] >= _RANK[n]


class Permission(SQLModel, table=True):
    """
    A permission grants a `capability` on a `project` to a subject: a user (`user_id`) OR
    a group (`group_id`). Grants only (allow-only) in this first cut; no deny rules.
    """
    __tablename__ = "permissions"
    id: Optional[int] = Field(default=None, primary_key=True)
    project_id: int = Field(foreign_key="projects.id", index=True)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    group_id: Optional[int] = Field(default=None, foreign_key="groups.id", index=True)
    capability: str  # a Capability value
