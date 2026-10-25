from datetime import datetime, timezone
from typing import Optional

from sqlmodel import SQLModel, Field
from sqlalchemy import Column, Text


def _now() -> datetime:
    return datetime.now(timezone.utc)


class AuditLog(SQLModel, table=True):
    """Audit log: who did what, when, from where.

    Append-only: every significant event (login, CRUD of flows/datasources/connections,
    runs, downloads, permission changes…) is a row. The actor's and target's identifiers
    are textual SNAPSHOTS (`actor_label`, `target_label`) besides the ids, so the audit
    stays readable even when the referenced object is later renamed or deleted.
    """
    __tablename__ = "audit_logs"

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=_now, index=True)

    actor_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    actor_label: str = ""  # email/name at the time of the action (survives the deletion)

    action: str = Field(index=True)  # es. auth.login, flow.create, export.download
    outcome: str = "success"         # success | failure

    target_type: Optional[str] = Field(default=None, index=True)  # flow | datasource | connection | user | permission | export
    target_id: Optional[int] = None
    target_label: Optional[str] = None

    # extra context (JSON): export format, rows, engine, cron, error reason…
    detail: Optional[str] = Field(default=None, sa_column=Column(Text))
    ip: Optional[str] = None
    user_agent: Optional[str] = Field(default=None, sa_column=Column(Text))
