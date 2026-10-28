"""The tasks of the data contracts asked for by hand (check now, propose from the data).

They run on the previews' queue: they are as interactive as a preview, and like a preview
they must not queue behind a long run. The evaluation that comes with a refresh or a
publication does NOT go through here: it happens inside the task that writes the data
(see `check_before_publish`) — but for a saved script's output, which no task writes (the
API copies it, api/routes/scripts.py), and is evaluated by `check_before_publish_task` on
the runs' queue."""
from datetime import datetime
from typing import Any

from app.tasks.celery_app import celery_app


@celery_app.task(name="app.tasks.contract_jobs.evaluate_contract_task")
def evaluate_contract_task(bucket: str, key: str, document: dict[str, Any], snapshot_at: str | None = None) -> dict:
    from app.contracts.service import evaluate_key

    return evaluate_key(bucket, key, document, datetime.fromisoformat(snapshot_at) if snapshot_at else None)


@celery_app.task(name="app.tasks.contract_jobs.profile_dataset_task")
def profile_dataset_task(bucket: str, key: str) -> dict:
    from app.contracts.service import profile_key

    return profile_key(bucket, key)


@celery_app.task(name="app.tasks.contract_jobs.check_before_publish_task")
def check_before_publish_task(bucket: str, key: str, contract: dict[str, Any]) -> dict:
    from app.contracts.service import check_before_publish

    return check_before_publish(bucket, key, contract)
