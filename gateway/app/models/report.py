"""Pixel-perfect reports: a page designed in the browser, kept as an object of a folder,
with its versions. docs/specs/reports.md; the rules in services/reports.py.

- `Report`: the object — name, folder, the version shown and rendered. Who sees it:
  VIEW on its folder; what a render reads is checked dataset by dataset with the
  reader's rights, never the author's.
- `ReportVersion`: every save that changes the definition is a version, as for SQL
  scripts; an older one can be restored (it becomes the newest). `content` is the
  report's definition, JSON (`v: 1`: page, parameters, datasets, sections), images
  included as base64.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Report(SQLModel, table=True):
    __tablename__ = "reports"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_report_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")
    current_version: int = 1
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ReportVersion(SQLModel, table=True):
    __tablename__ = "report_versions"
    __table_args__ = (UniqueConstraint("report_id", "version", name="uq_report_version"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    report_id: int = Field(foreign_key="reports.id", index=True)
    version: int  # 1, 2, 3… per report; the report's current_version is the one shown and rendered
    content: str = Field(sa_column=Column(Text, nullable=False))
    note: str = ""  # a stable English code the interface translates: "created", "edited", "restored from v2"
    created_at: datetime = Field(default_factory=_now)
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
