"""The data contracts on the parquet in the storage: evaluating, profiling, and the
decision that counts — whether the data just written can be published."""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime

import polars as pl

from app.contracts.evaluator import evaluate, profile, propose
from app.engine.temporal import naive_utc_lazy
from app.utils import get_storage_service

logger = logging.getLogger(__name__)


def _scan(bucket: str, key: str, workdir: str) -> pl.LazyFrame:
    local = os.path.join(workdir, "snapshot.parquet")
    get_storage_service().download_file(bucket, key, local)
    return naive_utc_lazy(pl.scan_parquet(local))


def evaluate_key(bucket: str, key: str, document: dict, snapshot_at: datetime | None = None) -> dict:
    """The contract's report on the parquet `bucket/key`."""
    with tempfile.TemporaryDirectory(prefix="contract-") as tmp:
        return evaluate(_scan(bucket, key, tmp), document, snapshot_at=snapshot_at)


def profile_key(bucket: str, key: str) -> dict:
    """The data's profile and the contract one can propose from it."""
    with tempfile.TemporaryDirectory(prefix="contract-") as tmp:
        prof = profile(_scan(bucket, key, tmp))
    return {"profile": prof, "proposal": propose(prof)}


def check_before_publish(bucket: str, key: str, contract: dict) -> dict:
    """Evaluates the data JUST WRITTEN, before the gateway publishes it.

    Returns the report; the gateway decides, and with a `failed` outcome it does not swap
    the snapshot. When the evaluation itself fails (the storage does not answer, the
    parquet cannot be read) one cannot know whether the contract holds: with even one
    blocking rule it does NOT publish — a contract never passes because it could not be
    checked — while a contract of warnings only, which by definition stops nothing,
    publishes and says so."""
    try:
        return evaluate_key(bucket, key, contract)
    except Exception as e:  # noqa: BLE001
        logger.exception("contract cannot be evaluated on %s/%s", bucket, key)
        return not_evaluated(contract, e)


def not_evaluated(contract: dict, e: BaseException) -> dict:
    """The report of a contract that could not be evaluated (see `check_before_publish`)."""
    blocking = any(r.get("severity") != "warning" for r in (contract.get("rules") or []) if isinstance(r, dict))
    return {
        "outcome": "failed" if blocking else "warning",
        "version": contract.get("version"),
        "rows": None,
        "errors": 1 if blocking else 0,
        "warnings": 0 if blocking else 1,
        "rules": [],
        "error": str(e).splitlines()[0][:300] if str(e) else type(e).__name__,
    }
