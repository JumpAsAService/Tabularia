from datetime import datetime, timezone
from typing import Optional
from sqlmodel import SQLModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Upload(SQLModel, table=True):
    """
    The register of the files uploaded through `POST /files`: who uploaded which object.

    It serves the RBAC check on the data plane: a fresh upload lives in no project, so it
    is readable ONLY by its owner; it enters the sphere of the project permissions when a
    saved flow references it (see services/objects.py).
    """
    __tablename__ = "uploads"

    id: Optional[int] = Field(default=None, primary_key=True)
    dataset_id: str = Field(index=True)
    bucket: str
    parquet_key: str = Field(index=True)
    raw_key: str = ""
    filename: str = ""
    owner_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    created_at: datetime = Field(default_factory=_now)
