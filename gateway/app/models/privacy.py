"""Privacy notice, written by the installation's administrator.

A SINGLE row (id=1) and not a list: it is a document that is edited, not a list that
is added to. Whoever installs Tabularia decides what to say, because they are the data
controller — we provide the place to say it and a starting text describing what the
product really collects.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PrivacyNotice(SQLModel, table=True):
    __tablename__ = "privacy_notice"

    id: Optional[int] = Field(default=1, primary_key=True)
    enabled: bool = True
    # the line that shows in the bar: it must fit on one line
    summary: str = Field(default="", sa_column=Column(Text, nullable=False))
    # the whole text, which opens from «Details»
    body: str = Field(default="", sa_column=Column(Text, nullable=False))
    # optional link to the organization's complete notice
    url: str = ""
    updated_at: datetime = Field(default_factory=_now)
    updated_by: Optional[int] = Field(default=None, foreign_key="users.id")
