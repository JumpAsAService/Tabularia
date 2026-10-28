"""DEFERRED deletion of the storage's blobs (grace period).

An overtaken snapshot / a replaced parquet / the blob of a deleted datasource are not
deleted at once: a run or a preview in progress may have resolved its key already and
be about to read it (it is the race that caused the 404 while reading). The deletion
is recorded with a grace BEYOND a run's maximum life and the scheduler's sweep runs it
when it expires.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.engine_client import CHECK, get_engine_client
from app.models.blob_deletion import PendingBlobDeletion

logger = logging.getLogger(__name__)

# A queued task that resolved the old key is failed by _reconcile after the run's stale
# timeout; past this window no run reads the overtaken blob any more. Grace = that cap
# + margin (it must stay ≥ the stale timeout).
def blob_deletion_grace_seconds() -> int:
    """Read at each use: ENGINE__RUN_STALE_TIMEOUT_SECONDS can change from the panel."""
    return get_settings().engine.run_stale_timeout_seconds + 600


def schedule_blob_deletion(
    session: Session, bucket: str, key: str, reason: str = "",
    grace_seconds: int | None = None,
) -> None:
    """Marks a blob for deferred deletion (after the grace). Adds to the session but does
    NOT commit: the commit is the caller's, so the marking is atomic with the swap / the
    publish that made the blob obsolete."""
    if not key:
        return
    grace = grace_seconds if grace_seconds is not None else blob_deletion_grace_seconds()
    session.add(
        PendingBlobDeletion(
            bucket=bucket,
            key=key,
            reason=reason,
            delete_after=datetime.now(timezone.utc) + timedelta(seconds=grace),
        )
    )


async def sweep_blob_deletions(session: Session, now: datetime | None = None) -> int:
    """Deletes the blobs whose grace expired (called by the scheduler's tick). Best effort:
    when the engine does not confirm the deletion the row stays and it retries at the
    next round; a 404 (gone already) counts as done."""
    now = now or datetime.now(timezone.utc)
    # plain values, not objects: after every commit below the objects would expire, and
    # reading again a row another process removed already is an error
    due = [
        (r.id, r.bucket, r.key)
        for r in session.exec(select(PendingBlobDeletion).where(PendingBlobDeletion.delete_after <= now)).all()
    ]
    session.rollback()  # reading done: no transaction open during the calls to the engine
    if not due:
        return 0
    client = get_engine_client()
    removed = 0
    for row_id, bucket, key in due:
        try:
            resp = await client.delete("/files/object", params={"bucket": bucket, "key": key}, timeout=CHECK)
        except Exception as e:  # engine unreachable → retry at the next tick
            logger.warning("sweep blob %s/%s postponed: %s", bucket, key, e)
            continue
        if resp.status_code < 400 or resp.status_code == 404:
            # By id and not `session.delete(row)`: with several processes making the same pass the
            # row may have been removed by another already, and the ORM would treat it as an error
            # (deleting the same blob twice is harmless: the second finds 404, which counts as
            # done). And commit AT ONCE: the DELETE holds a lock on the row until it is confirmed,
            # and holding it for the engine calls that follow would stop another process arrived at
            # the same row for all that time.
            deleted = session.exec(delete(PendingBlobDeletion).where(PendingBlobDeletion.id == row_id))
            session.commit()
            removed += deleted.rowcount or 0
        else:
            logger.warning("blob sweep %s/%s not deleted (%s): retrying", bucket, key, resp.status_code)
    return removed
