"""Warning banners shown in the Explore to ALL users.

An administrator manages them from the Admin page: adding and deleting. There is no
active/suspended switch on purpose — a banner exists until it is removed, so «what is
in the list» is exactly «what the users see», without intermediate states to
remember.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Banner(SQLModel, table=True):
    __tablename__ = "banners"

    id: Optional[int] = Field(default=None, primary_key=True)
    message: str = Field(sa_column=Column(Text, nullable=False))
    # info | warning | danger — decides only icon and colour in the interface, not the
    # behaviour: a banner is always visible to all.
    level: str = Field(default="warning", index=True)
    created_at: datetime = Field(default_factory=_now)
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
