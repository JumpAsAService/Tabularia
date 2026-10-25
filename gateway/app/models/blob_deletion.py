from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PendingBlobDeletion(SQLModel, table=True):
    """Storage blobs to delete DEFERRED.

    A snapshot overtaken by a refresh, a parquet replaced by an overwrite, or the blob of
    a deleted datasource must NOT be deleted at once: a run or a preview in progress may
    have resolved its key and still be about to read them. They are recorded here with
    `delete_after` = now + grace (beyond a run's maximum life) and the scheduler's sweep
    deletes them when it expires.
    """

    __tablename__ = "pending_blob_deletions"

    id: Optional[int] = Field(default=None, primary_key=True)
    bucket: str
    key: str = Field(index=True)
    delete_after: datetime = Field(index=True)
    reason: str = ""
    created_at: datetime = Field(default_factory=_now)
