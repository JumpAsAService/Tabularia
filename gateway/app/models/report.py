"""Pixel-perfect reports: a page designed in the browser, kept as an object of a folder,
with its versions. docs/specs/reports.md; the rules in services/reports.py.

- `Report`: the object — name, folder, the version shown and rendered. Who sees it:
  VIEW on its folder; what a render reads is checked dataset by dataset with the
  reader's rights, never the author's.
- `ReportVersion`: every save that changes the definition is a version, as for SQL
  scripts; an older one can be restored (it becomes the newest). `content` is the
  report's definition, JSON (`v: 1`: page, parameters, datasets, sections), images
  included as base64.
- `ReportView`: who opened a report in the viewer, at most one row per person and day
  (the installation's day, APP__TIMEZONE), for the Reports tab's «trending»
  (services/report_views.py). The housekeeping removes the rows past the window it
  counts; the report keeps how many there have ever been (`views_total`) and when it was
  last opened.
"""
from datetime import date, datetime, timezone
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
    # the openings in the viewer ever counted (a person once a day), and the latest one:
    # they outlive the `report_views` rows the housekeeping removes
    views_total: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    last_viewed_at: Optional[datetime] = None


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


class ReportView(SQLModel, table=True):
    __tablename__ = "report_views"
    # a person opening the report again the same day changes nothing: one row
    __table_args__ = (UniqueConstraint("report_id", "user_id", "day", name="uq_report_view_day"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    # no index of its own: the unique constraint's starts with it
    report_id: int = Field(foreign_key="reports.id", ondelete="CASCADE")
    user_id: int = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    day: date = Field(index=True)  # the installation's day (APP__TIMEZONE): the housekeeping trims by it
    at: datetime = Field(default_factory=_now)  # the first opening of that day
