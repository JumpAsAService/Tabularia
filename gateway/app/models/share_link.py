"""Share links: a report or a dashboard opened from outside Tabularia with a link and its own
password, or embedded in the pages of listed origins (docs/specs/share-links.md).

Only the Go gateway reads and writes these tables; the models are here so that the migrated
database and the models stay one and the same. The times are naive UTC, as the rest.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


class ShareLink(SQLModel, table=True):
    __tablename__ = "share_links"

    id: Optional[int] = Field(default=None, primary_key=True)
    # sha-256 of the token (32 random bytes): the token itself is never kept
    token_hash: str = Field(max_length=64, unique=True)
    token_hint: str = Field(max_length=8)
    kind: str = Field(max_length=16)  # report | dashboard
    object_id: int = Field(index=True)
    label: str = Field(default="", max_length=200)
    # a deleted creator takes their links with them
    creator_id: int = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    embeddable: bool = False
    origins: str = Field(default="[]", sa_column=Column(Text, nullable=False))  # JSON list
    password_hash: Optional[str] = Field(default=None, max_length=100)  # bcrypt; None when embeddable
    password_temporary: bool = True
    failures: int = 0
    params: str = Field(default="{}", sa_column=Column(Text, nullable=False))  # JSON, as `given`
    downloads: str = Field(default="[]", sa_column=Column(Text, nullable=False))  # JSON list
    expires_at: datetime = Field(index=True)
    revoked_at: Optional[datetime] = None
    created_at: datetime
    last_opened_at: Optional[datetime] = None
    openings: int = 0
    ai_hour: Optional[datetime] = None
    ai_calls: int = 0
    ai_last: Optional[str] = Field(default=None, sa_column=Column(Text))  # JSON


class ShareLinkSession(SQLModel, table=True):
    __tablename__ = "share_link_sessions"

    # sha-256 of the session's secret
    id: str = Field(primary_key=True, max_length=64)
    link_id: int = Field(foreign_key="share_links.id", index=True, ondelete="CASCADE")
    purpose: str = Field(max_length=10)  # view | change
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime = Field(index=True)
    ip: Optional[str] = Field(default=None, max_length=64)


class ShareLinkSettings(SQLModel, table=True):
    __tablename__ = "share_link_settings"

    id: int = Field(default=1, primary_key=True)
    enabled: bool = True
    max_days: int = 90
    ai_comments_per_hour: int = 20
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = Field(default=None, max_length=320)
