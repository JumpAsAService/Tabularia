from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field
from sqlalchemy import Column, Text, UniqueConstraint


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Datasource(SQLModel, table=True):
    """
    A NAMED dataset in the catalog: a parquet in the storage with name, folder and
    permissions (inherited from the project, as flows are). Today it is born from
    publishing a run's output; tomorrow from uploads and DB connections too (the `kind`
    field).
    """
    __tablename__ = "datasources"
    __table_args__ = (UniqueConstraint("project_id", "name", name="uq_datasource_project_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    description: str = ""
    project_id: int = Field(foreign_key="projects.id", index=True)
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id")

    bucket: str
    key: str  # parquet in the storage (datasets/…)
    rows: Optional[int] = None
    columns: str = Field(default="[]", sa_column=Column(Text, nullable=False))  # JSON [{name, dtype}]
    # ORDER BY keys (JSON [column name]) the parquet was written sorted by: enables the
    # pruning downstream (viewer/ClickHouse). Empty = no order imposed, all as before. It
    # persists across refreshes.
    sort_keys: str = Field(default="[]", sa_column=Column(Text, nullable=False))
    # descriptions of the FIELDS (JSON {column name: text}), curated by hand: the meaning
    # of each column for whoever uses it — and for the AI integration (semantic context on
    # the dataset). Kept apart from `columns`, which is regenerated at every
    # refresh/publish: so the descriptions survive.
    column_descriptions: str = Field(default="{}", sa_column=Column(Text, nullable=False))

    kind: str = "flow"  # flow | database (| upload, future)
    flow_id: Optional[int] = Field(default=None, foreign_key="flows.id")  # provenance

    # for kind="database": the source's definition and the last refresh. The parquet is a
    # SNAPSHOT: refresh = a new ingest that replaces the blob.
    connection_id: Optional[int] = Field(default=None, foreign_key="connections.id")
    source_type: Optional[str] = None  # table | sql
    source_ref: Optional[str] = Field(default=None, sa_column=Column(Text))
    refreshed_at: Optional[datetime] = None

    # SCHEDULED refresh (kind="database" only): the gateway's scheduler runs the ingest
    # again when `next_refresh_at` is due, with the authority of `refresh_scheduled_by`
    # (RUN+CONNECT captured when the schedule was created). schedule=None → disabled.
    # Format: "interval:<min>" or "daily:<HH>:<MM>".
    refresh_schedule: Optional[str] = None
    refresh_scheduled_by: Optional[int] = Field(default=None, foreign_key="users.id")
    next_refresh_at: Optional[datetime] = Field(default=None, index=True)

    # the run that produced the current SNAPSHOT: orders the concurrent updates. Run ids
    # are monotonic and assigned at LAUNCH, so they order by the instant the source was
    # READ — not by the instant of writing, which would reward the run started first and
    # finished later, that is the stalest data. A swap from a run older than this is
    # refused. A plain integer, without a foreign key: `runs.datasource_id` points here
    # already and a reverse reference would make a cycle in creating the schema.
    # None = no baseline (a snapshot older than this field) → accepted.
    snapshot_run_id: Optional[int] = None

    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
