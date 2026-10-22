"""How long the runs keep their logs (ORCHESTRATOR__LOG_RETENTION_DAYS, 30 by default;
0 keeps them for ever).

A run keeps the end of what a Script or Container node printed (up to 256 KB, in its
`script` JSON) and, when it failed, the engine's full traceback (`error_detail`). The
runs themselves stay — the history, the calendar, the performance page and the
lineage read them — but past the window their logs are emptied: a chatty container
scheduled every five minutes otherwise adds tens of gigabytes a year to the database.
What remains says what happened: the outcome, the short error, the timings, the
attempts and the exit code. A run whose log was emptied says so (`log_expired`).
Run every hour by services/housekeeping.py, which vacuums the table afterwards.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlmodel import Session, select

from app.models import Run

logger = logging.getLogger(__name__)

# rows per transaction: a first pass over years of runs must not hold one long
# transaction while the application works
LOTTO = 500
MAX_LOTTI = 200
STEP_KINDS = ("script", "container")
# a JSON with a log (json.dumps writes `"log": "…"`), not emptied yet
_WITH_LOG = '%"log": "_%'
_EMPTIED = '%"log_expired": %'  # true, or false for a log that was empty already


def _adesso() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC columns


def svuota(session: Session, giorni: int, ora: datetime | None = None) -> int:
    """Empties the logs of the runs ended more than `giorni` days ago. Returns how
    many runs lost something."""
    if giorni <= 0:
        return 0
    taglio = (ora or _adesso()) - timedelta(days=giorni)
    vecchi = (Run.finished_at.is_not(None), Run.finished_at < taglio)  # type: ignore[union-attr]
    tolti = 0
    # the engine's tracebacks: one UPDATE per batch
    for _ in range(MAX_LOTTI):
        ids = select(Run.id).where(*vecchi, Run.error_detail.is_not(None)).limit(LOTTO).scalar_subquery()  # type: ignore[union-attr]
        n = session.exec(update(Run).where(Run.id.in_(ids)).values(error_detail=None)  # type: ignore[union-attr]
                         .execution_options(synchronize_session=False)).rowcount or 0
        session.commit()
        tolti += n
        if n < LOTTO:
            break
    # the logs of Script and Container runs, inside their JSON
    for _ in range(MAX_LOTTI):
        rows = session.exec(select(Run.id, Run.script).where(
            *vecchi, Run.kind.in_(STEP_KINDS), Run.script.like(_WITH_LOG),  # type: ignore[union-attr]
            ~Run.script.like(_EMPTIED)).limit(LOTTO)).all()  # type: ignore[union-attr]
        for run_id, raw in rows:
            try:
                info = json.loads(raw or "{}")
            except ValueError:
                continue
            # a run that printed nothing keeps saying so: marked only as looked at
            info["log"], info["log_expired"] = "", bool(info.get("log"))
            session.exec(update(Run).where(Run.id == run_id).values(script=json.dumps(info))
                         .execution_options(synchronize_session=False))
        session.commit()
        tolti += len(rows)
        if len(rows) < LOTTO:
            break
    return tolti
