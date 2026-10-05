"""Data contracts lato engine: valutare un contratto su uno snapshot e profilare
i dati per proporne uno. Sincrone per chi chiama (invio + attesa), eseguite su un
worker: Polars non gira nel processo dell'API."""
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

# quanto si aspetta una verifica chiesta a mano prima di rinunciare
WAIT_SECONDS = 120.0


class EvaluateRequest(BaseModel):
    bucket: Optional[str] = None
    key: str
    document: dict[str, Any]
    snapshot_at: Optional[str] = Field(default=None, description="Quando lo snapshot è stato prodotto (ISO)")


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
        except Exception as e:  # il task è fallito: lo storage non risponde, il parquet non c'è
            logger.warning("%s non riuscito: %s", task, e)
            raise HTTPException(status_code=422, detail=str(e).splitlines()[0][:300] or type(e).__name__)


@router.post("/evaluate")
def evaluate_contract(request: EvaluateRequest) -> dict:
    """Il referto del contratto sullo snapshot indicato."""
    return _run(
        "app.tasks.contract_jobs.evaluate_contract_task",
        bucket=request.bucket or get_settings().storage.bucket,
        key=request.key, document=request.document, snapshot_at=request.snapshot_at,
    )


@router.post("/profile")
def profile_dataset(request: ProfileRequest) -> dict:
    """Il profilo dei dati e il contratto che se ne può proporre."""
    return _run(
        "app.tasks.contract_jobs.profile_dataset_task",
        bucket=request.bucket or get_settings().storage.bucket, key=request.key,
    )
