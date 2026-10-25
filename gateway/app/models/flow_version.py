from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FlowVersion(SQLModel, table=True):
    """A snapshot of a flow's definition. Every save that changes the definition creates one
    (auto-version): so the history can be seen and an old version PROMOTED (it becomes the
    new current one)."""

    __tablename__ = "flow_versions"
    __table_args__ = (UniqueConstraint("flow_id", "version", name="uq_flowversion"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    flow_id: int = Field(foreign_key="flows.id", index=True)
    version: int  # progressive per flow (1, 2, 3, …); the highest is the current one
    definition: str = Field(sa_column=Column(Text, nullable=False))
    note: str = ""  # a stable English code the interface translates: "created", "edited", "promoted from v2"
    created_at: datetime = Field(default_factory=_now)
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
