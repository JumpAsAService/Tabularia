"""Flow groups: flows and datasource refreshes laid out as a grid of steps.

Each row of the grid is a step; the cells of a row run in parallel, the rows one
after another, and the first step with a failure stops the group. A group lives in
a folder like a flow, with the folder's permissions: VIEW to see it and its runs,
RUN to run or schedule it, EDIT to change, move or delete it.

`definition` holds the grid as JSON: {"steps": [[cell, cell, null], ...]} where a
cell is {"kind": "flow" | "refresh", "id": <flow or datasource id>} and null keeps
an empty cell in its column, so the grid comes back as it was drawn. The gateway
checks it on save (see routes/flow_groups.py); the objects it names may disappear
later, and the editor and the run say so instead of failing silently.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

EMPTY_DEFINITION = '{"steps": []}'


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FlowGroup(SQLModel, table=True):
    __tablename__ = "flow_groups"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_flow_group_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")
    definition: str = Field(default=EMPTY_DEFINITION, sa_column=Column(Text, nullable=False))

    # scheduled runs (cron), with the authority of whoever set the schedule, as for
    # flows; None = not scheduled
    run_schedule: Optional[str] = None
    run_scheduled_by: Optional[int] = Field(default=None, foreign_key="users.id")
    next_run_at: Optional[datetime] = Field(default=None, index=True)

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
