"""The home page: the grid of widgets each person arranges for themselves
(docs/specs/home.md).
Only the Go gateway reads and writes these tables; the models are here so that the migrated
database and the models stay one and the same. The times are naive UTC, as the rest.
- `UserHome`: one row per person who changed their home page; `definition` is JSON (the
  widgets, their place and their options). No row: the default layout, which the gateway
  holds in one place for every installation. Goes with the account.
- `UserFavourite`: an object a person pinned to their home page, by kind and id (as the
  objects of Explore are named), in the order they chose. Goes with the account; an
  object deleted or no longer readable is simply not listed.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class UserHome(SQLModel, table=True):
    __tablename__ = "user_home"

    user_id: int = Field(foreign_key="users.id", primary_key=True, ondelete="CASCADE")
    definition: str = Field(default="{}", sa_column=Column(Text, nullable=False))  # JSON
    updated_at: datetime = Field(default_factory=_now)


class UserFavourite(SQLModel, table=True):
    __tablename__ = "user_favourites"
    __table_args__ = (UniqueConstraint("user_id", "kind", "object_id", name="uq_user_favourite"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    kind: str = Field(max_length=32)
    object_id: int
    position: int = 0
    created_at: datetime = Field(default_factory=_now)
