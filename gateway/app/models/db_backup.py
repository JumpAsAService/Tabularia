"""The automatic backup of this database (Administration → Backups). One row (id 1): what
the administrators changed — None keeps the installation's value (BACKUP__*) — and how the
backups are going. The work is done by services/db_backup.py."""
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Column, Text
from sqlmodel import Field, SQLModel


class DatabaseBackup(SQLModel, table=True):
    __tablename__ = "database_backups"

    id: int = Field(default=1, primary_key=True)
    # the administrators' choices; None = BACKUP__ENABLED / BACKUP__SCHEDULE / BACKUP__KEEP
    enabled: Optional[bool] = None
    schedule: Optional[str] = Field(default=None, max_length=120)
    keep: Optional[int] = None
    # the next scheduled backup, and the schedule it was computed from
    next_at: Optional[datetime] = None
    next_for: Optional[str] = Field(default=None, max_length=120)
    # «Back up now»: taken by the first orchestrator that looks
    requested_at: Optional[datetime] = None
    requested_by: Optional[str] = Field(default=None, max_length=320)
    # a backup in progress, and the process making it
    running_since: Optional[datetime] = None
    claimed_by: Optional[str] = Field(default=None, max_length=200)
    # how the last ones went
    last_ok_at: Optional[datetime] = None
    last_name: Optional[str] = Field(default=None, max_length=120)
    last_size: Optional[int] = Field(default=None, sa_column=Column(BigInteger))
    last_seconds: Optional[float] = None
    last_error: Optional[str] = Field(default=None, sa_column=Column(Text))
    last_error_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = Field(default=None, max_length=320)
