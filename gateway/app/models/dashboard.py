"""Dashboards: a screen of live tiles on a grid, kept as an object of a folder, with its
versions. docs/specs/dashboards.md; the rules in services/dashboards.py.

- `Dashboard`: the object — name, folder, the version shown. Who sees it: VIEW on its
  folder; what a tile reads is checked dataset by dataset with the reader's rights, never
  the author's.
- `DashboardVersion`: every save that changes the definition is a version, as for reports;
  an older one can be restored (it becomes the newest). `content` is the dashboard's
  definition, JSON (`v: 1`: parameters, datasets, tiles), images included as base64.
- `DashboardView`: who opened a dashboard in the viewer, at most one row per person and day
  (the installation's day, APP__TIMEZONE), as `ReportView`. The housekeeping removes the
  rows past the window it counts; the dashboard keeps how many there have ever been
  (`views_total`) and when it was last opened.
"""
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Dashboard(SQLModel, table=True):
    __tablename__ = "dashboards"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_dashboard_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")
    current_version: int = 1
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    # the openings in the viewer ever counted (a person once a day), and the latest one:
    # they outlive the `dashboard_views` rows the housekeeping removes
    views_total: int = Field(default=0, sa_column_kwargs={"server_default": "0"})
    last_viewed_at: Optional[datetime] = None


class DashboardVersion(SQLModel, table=True):
    __tablename__ = "dashboard_versions"
    __table_args__ = (UniqueConstraint("dashboard_id", "version", name="uq_dashboard_version"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    dashboard_id: int = Field(foreign_key="dashboards.id", index=True)
    version: int  # 1, 2, 3… per dashboard; its current_version is the one shown
    content: str = Field(sa_column=Column(Text, nullable=False))
    note: str = ""  # a stable English code the interface translates: "created", "edited", "restored from v2"
    created_at: datetime = Field(default_factory=_now)
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")


class DashboardView(SQLModel, table=True):
    __tablename__ = "dashboard_views"
    # a person opening the dashboard again the same day changes nothing: one row
    __table_args__ = (UniqueConstraint("dashboard_id", "user_id", "day", name="uq_dashboard_view_day"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    # no index of its own: the unique constraint's starts with it
    dashboard_id: int = Field(foreign_key="dashboards.id", ondelete="CASCADE")
    user_id: int = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    day: date = Field(index=True)  # the installation's day (APP__TIMEZONE): the housekeeping trims by it
    at: datetime = Field(default_factory=_now)  # the first opening of that day
