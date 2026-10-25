from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, Text


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Flow(SQLModel, table=True):
    """
    A saved flow (the editor's DAG). It lives inside a project/folder and inherits its
    permissions: VIEW to see/open it, EDIT to save/move/delete it. `definition` is the
    canvas's serialized JSON (nodes+edges).
    """
    __tablename__ = "flows"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")
    definition: str = Field(default="{}", sa_column=Column(Text, nullable=False))

    # DEVELOPMENT engine (persisted): the editor's — previews, manual runs, run-now. Chosen
    # at creation, changeable afterwards.
    engine: str = Field(default="polars")
    # PRODUCTION engine: used by the SCHEDULED runs (and by run-now in production mode).
    # None = the same as development. It decouples "what I use while designing" (e.g.
    # Polars, fast locally) from "what the DAG uses in production" (e.g. an external
    # ClickHouse).
    production_engine: Optional[str] = None

    # SCHEDULED run (cron): the gateway's scheduler resolves the CURRENT definition again
    # and launches the Output nodes with the authority of `run_scheduled_by`.
    # schedule=None → disabled.
    run_schedule: Optional[str] = None
    run_scheduled_by: Optional[int] = Field(default=None, foreign_key="users.id")
    next_run_at: Optional[datetime] = Field(default=None, index=True)
    # A notice when a SCHEDULED run fails: «schedule it and forget it» works only if
    # something wakes you up. The addresses are a comma-separated list; empty or without a
    # connection = no notice. Manual runs do not notify: whoever launches them is watching.
    notify_emails: Optional[str] = None
    notify_connection_id: Optional[int] = Field(default=None, foreign_key="connections.id")

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
