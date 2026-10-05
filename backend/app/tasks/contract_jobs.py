"""Task dei data contracts chiesti a mano (verifica adesso, proposta dai dati).

Girano sulla coda delle anteprime: sono interattivi come un'anteprima, e come
un'anteprima non devono mettersi in fila dietro un run lungo. La valutazione che
accompagna un refresh o una pubblicazione NON passa da qui: avviene dentro il
task che scrive i dati (vedi `check_before_publish`)."""
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
