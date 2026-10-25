"""Data contracts on the engine's side: evaluating a contract on a snapshot and profiling
the data to propose one. Synchronous for the caller (send + wait), run on a worker:
Polars does not run in the API's process."""
import logging
import time
from typing import Any, Optional

from celery.exceptions import TimeoutError as CeleryTimeoutError
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.tasks.celery_app import celery_app

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/contracts", tags=["contracts"])

# how long a check asked for by hand is awaited before giving up
WAIT_SECONDS = 120.0


class EvaluateRequest(BaseModel):
    bucket: Optional[str] = None
    key: str
    document: dict[str, Any]
    snapshot_at: Optional[str] = Field(default=None, description="When the snapshot was produced (ISO)")


class ProfileRequest(BaseModel):
    bucket: Optional[str] = None
    key: str


def _run(task: str, **kwargs) -> dict:
    result = celery_app.send_task(task, kwargs=kwargs, queue="preview")
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        try:
            return result.get(timeout=0.25)
        except CeleryTimeoutError:
            if time.monotonic() >= deadline:
                result.revoke(terminate=True)
                raise HTTPException(
                    status_code=504,
                    detail="The dataset is too large to be checked interactively: it will be checked at its next update.",
                )
        except HTTPException:
            raise
        except Exception as e:  # the task failed: the storage does not answer, the parquet is not there
            logger.warning("%s failed: %s", task, e)
            raise HTTPException(status_code=422, detail=str(e).splitlines()[0][:300] or type(e).__name__)


@router.post("/evaluate")
def evaluate_contract(request: EvaluateRequest) -> dict:
    """The contract's report on the given snapshot."""
    return _run(
        "app.tasks.contract_jobs.evaluate_contract_task",
        bucket=request.bucket or get_settings().storage.bucket,
        key=request.key, document=request.document, snapshot_at=request.snapshot_at,
    )


@router.post("/profile")
def profile_dataset(request: ProfileRequest) -> dict:
    """The data's profile and the contract one can propose from it."""
    return _run(
        "app.tasks.contract_jobs.profile_dataset_task",
        bucket=request.bucket or get_settings().storage.bucket, key=request.key,
    )
