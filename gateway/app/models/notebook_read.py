"""What a notebook has read: one row per notebook, mode and datasource, written the first
time a session of the notebook reads that datasource with `tabularia.read`
(routes/notebook_data.py). Per mode because a notebook has at most one live session per
mode, and its app and its editor may run at once: with one row per notebook and
datasource, two sessions reading the same datasource took it from each other at every
read, and the first to stop lost it from its closing event.

The Lineage page draws the notebook as a reader of these datasources; OpenLineage tells a
session's reads as the inputs of its run (services/openlineage.py). See
services/notebook_reads.py.

Notebooks only read (docs/specs/notebooks.md): there is nothing to record about writing.
Naive UTC times, as the sessions.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


def _naive_now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class NotebookRead(SQLModel, table=True):
    __tablename__ = "notebook_reads"
    __table_args__ = (UniqueConstraint("notebook_id", "datasource_id", "mode", name="uq_notebook_read"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    # no index of its own: the unique constraint's starts with it
    notebook_id: int = Field(foreign_key="notebooks.id", ondelete="CASCADE")
    datasource_id: int = Field(foreign_key="datasources.id", index=True, ondelete="CASCADE")
    # the latest session of this mode (app | edit) that read it: its Job's name, unique per
    # pod (the session's row is deleted when the pod stops, and its id could come back)
    last_session: str = Field(max_length=63)
    mode: str = Field(max_length=8)
    first_read_at: datetime = Field(default_factory=_naive_now)
    read_at: datetime = Field(default_factory=_naive_now)  # that session's first read
