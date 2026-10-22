"""Saved scripts: Python or R code kept as an object of a folder, with its versions.
docs/specs/saved-scripts.md; the rules in services/saved_scripts.py.

- `SavedScript`: the object — name, folder, scripting environment, the version that
  runs, and (later) its schedule. Who sees it: VIEW on its folder AND the use of its
  environment; whoever lacks either does not see it at all.
- `SavedScriptVersion`: every save that changes what the script runs is a version, as
  for flows; an older one can be restored (it becomes the newest). `content` is JSON:
  files, entrypoint, declared inputs, output settings, timeout and retries.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SavedScript(SQLModel, table=True):
    __tablename__ = "saved_scripts"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_saved_script_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")
    # no cascade: an environment in use by a script cannot be deleted
    environment_id: int = Field(foreign_key="script_environments.id", index=True)
    current_version: int = 1
    # scheduled runs (cron), with the authority of whoever set the schedule, as for
    # flows and flow groups; None = not scheduled
    run_schedule: Optional[str] = None
    run_scheduled_by: Optional[int] = Field(default=None, foreign_key="users.id")
    next_run_at: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class SavedScriptVersion(SQLModel, table=True):
    __tablename__ = "saved_script_versions"
    __table_args__ = (UniqueConstraint("script_id", "version", name="uq_saved_script_version"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    script_id: int = Field(foreign_key="saved_scripts.id", index=True)
    version: int  # 1, 2, 3… per script; the script's current_version is the one that runs
    content: str = Field(sa_column=Column(Text, nullable=False))
    note: str = ""  # a stable English code the interface translates: "created", "edited", "restored from v2"
    created_at: datetime = Field(default_factory=_now)
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
