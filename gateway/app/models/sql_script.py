"""SQL scripts: SQL kept as an object of a folder, with its versions.
docs/specs/sql-scripts.md; the rules in services/sql_scripts.py.

- `SqlScript`: the object — name, folder, the version shown and run, the schedule that
  writes its result into its datasource. Who sees it: VIEW on its folder.
- `SqlScriptVersion`: every save that changes what the script runs is a version, as for
  flows; an older one can be restored (it becomes the newest). `content` is JSON: the
  SQL, the engine, the connection (null = the catalog), the datasource it writes.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class SqlScript(SQLModel, table=True):
    __tablename__ = "sql_scripts"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_sql_script_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")
    current_version: int = 1
    # the schedule that writes the result into the script's datasource, run with the
    # authority of `run_scheduled_by` (5-field cron, as a flow group's)
    run_schedule: Optional[str] = None
    run_scheduled_by: Optional[int] = Field(default=None, foreign_key="users.id")
    next_run_at: Optional[datetime] = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class SqlScriptVersion(SQLModel, table=True):
    __tablename__ = "sql_script_versions"
    __table_args__ = (UniqueConstraint("script_id", "version", name="uq_sql_script_version"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    script_id: int = Field(foreign_key="sql_scripts.id", index=True)
    version: int  # 1, 2, 3… per script; the script's current_version is the one shown and run
    content: str = Field(sa_column=Column(Text, nullable=False))
    note: str = ""  # a stable English code the interface translates: "created", "edited", "restored from v2"
    created_at: datetime = Field(default_factory=_now)
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
