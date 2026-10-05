"""I data contracts sul parquet che sta nello storage: valutare, profilare, e la
decisione che conta — se i dati appena scritti si possono pubblicare."""
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
    """Il referto del contratto sul parquet `bucket/key`."""
    with tempfile.TemporaryDirectory(prefix="contract-") as tmp:
        return evaluate(_scan(bucket, key, tmp), document, snapshot_at=snapshot_at)


def profile_key(bucket: str, key: str) -> dict:
    """Il profilo dei dati e il contratto che se ne può proporre."""
    with tempfile.TemporaryDirectory(prefix="contract-") as tmp:
        prof = profile(_scan(bucket, key, tmp))
    return {"profile": prof, "proposal": propose(prof)}


def check_before_publish(bucket: str, key: str, contract: dict) -> dict:
    """Valuta i dati APPENA SCRITTI, prima che il gateway li pubblichi.

    Torna il referto; chi decide è il gateway, che con un esito `failed` non
    scambia lo snapshot. Se la valutazione stessa non riesce (lo storage non
    risponde, il parquet non si legge) non si può sapere se il contratto regge:
    con anche una sola regola bloccante NON si pubblica — un contratto non passa
    perché non lo si è potuto controllare — mentre un contratto di soli avvisi,
    che per definizione non ferma niente, pubblica e lo dice."""
    try:
        return evaluate_key(bucket, key, contract)
    except Exception as e:  # noqa: BLE001
        logger.exception("contratto non valutabile su %s/%s", bucket, key)
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
