"""Keeping the database from growing without end. The data itself lives in the object
storage; what piles up in Postgres is history, and every hour the orchestrators trim it:

- the runs' logs, past ORCHESTRATOR__LOG_RETENTION_DAYS (services/run_log_retention.py);
- the data contracts' reports: the latest ORCHESTRATOR__CONTRACT_RESULTS_KEPT per
  datasource (a refresh every five minutes adds 288 a day), never one whose notice is
  still to be sent;
- the versions of a flow and of a saved script: the latest ORCHESTRATOR__VERSIONS_KEPT,
  the one in use always (a flow with a Script node keeps its code in every version);
- the assistant's conversations untouched for ORCHESTRATOR__AI_CHAT_RETENTION_DAYS, the
  spending ledger aside (it is the bill, not the conversation).

0 keeps everything of that kind. Postgres does not give back what an UPDATE or a DELETE
frees: the old rows stay until a vacuum marks their space for reuse. So a pass that
removed something ends with `VACUUM (ANALYZE)` of the tables it touched, which does not
block reads nor writes (`VACUUM FULL`, which gives the space back to the disk but locks
the table, is left to a maintenance window).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import bindparam, text
from sqlmodel import Session

from app.core.config import get_settings
from app.services import run_log_retention

logger = logging.getLogger(__name__)

EVERY_SECONDS = 3600
LOTTO = 1000
MAX_LOTTI = 200


def _adesso() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC columns


def _a_lotti(session: Session, sql: str, **params) -> int:
    """Runs a DELETE … WHERE id IN (… LIMIT :lotto) batch after batch."""
    tolti = 0
    for _ in range(MAX_LOTTI):
        n = session.exec(text(sql).bindparams(lotto=LOTTO, **params)).rowcount or 0
        session.commit()
        tolti += n
        if n < LOTTO:
            break
    return tolti


def contract_results(session: Session, kept: int) -> int:
    if kept <= 0:
        return 0
    return _a_lotti(session, """
        DELETE FROM data_contract_results WHERE id IN (
            SELECT id FROM (
                SELECT id, notify, ROW_NUMBER() OVER (PARTITION BY datasource_id ORDER BY id DESC) AS n
                FROM data_contract_results) t
            WHERE t.n > :kept AND (t.notify IS NULL OR t.notify NOT IN ('pending', 'sending'))
            LIMIT :lotto)""", kept=kept)


def flow_versions(session: Session, kept: int) -> int:
    if kept <= 0:
        return 0
    # the highest version is the flow's current one: always among those kept
    return _a_lotti(session, """
        DELETE FROM flow_versions WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY flow_id ORDER BY version DESC) AS n
                FROM flow_versions) t
            WHERE t.n > :kept LIMIT :lotto)""", kept=kept)


def script_versions(session: Session, kept: int) -> int:
    if kept <= 0:
        return 0
    return _a_lotti(session, """
        DELETE FROM saved_script_versions WHERE id IN (
            SELECT t.id FROM (
                SELECT id, script_id, version,
                       ROW_NUMBER() OVER (PARTITION BY script_id ORDER BY version DESC) AS n
                FROM saved_script_versions) t
            WHERE t.n > :kept AND NOT EXISTS (
                SELECT 1 FROM saved_scripts s WHERE s.id = t.script_id AND s.current_version = t.version)
            LIMIT :lotto)""", kept=kept)


def ai_chats(session: Session, days: int, ora: datetime | None = None) -> int:
    if days <= 0:
        return 0
    taglio = (ora or _adesso()) - timedelta(days=days)
    tolte = 0
    for _ in range(MAX_LOTTI):
        ids = [i for (i,) in session.execute(
            text("SELECT id FROM ai_chats WHERE updated_at < :taglio LIMIT :lotto"),
            {"taglio": taglio, "lotto": LOTTO}).all()]
        if not ids:
            break
        session.execute(text("DELETE FROM ai_chat_turns WHERE chat_id IN :ids").bindparams(
            bindparam("ids", expanding=True)), {"ids": ids})
        session.execute(text("DELETE FROM ai_chats WHERE id IN :ids").bindparams(
            bindparam("ids", expanding=True)), {"ids": ids})
        session.commit()
        tolte += len(ids)
        if len(ids) < LOTTO:
            break
    return tolte


def vacuum(session: Session, tables: list[str]) -> None:
    """Marks the space the pass freed for reuse (Postgres only; outside a transaction)."""
    bind = session.get_bind()
    if not tables or bind.dialect.name != "postgresql":
        return
    with bind.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for table in tables:
            conn.exec_driver_sql(f"VACUUM (ANALYZE) {table}")  # names of this module, never input


def one_pass(session: Session, ora: datetime | None = None) -> dict[str, int]:
    """Trims every kind once; returns how many rows each touched, and vacuums those.
    A kind that fails (a deadlock with another replica's pass, a lost connection) is
    logged and left to the next pass: the others are trimmed all the same."""
    o = get_settings().orchestrator
    steps = {
        "runs": lambda: run_log_retention.svuota(session, o.log_retention_days, ora=ora),
        "data_contract_results": lambda: contract_results(session, o.contract_results_kept),
        "flow_versions": lambda: flow_versions(session, o.versions_kept),
        "saved_script_versions": lambda: script_versions(session, o.versions_kept),
        "ai_chats": lambda: ai_chats(session, o.ai_chat_retention_days, ora=ora),
    }
    done: dict[str, int] = {}
    for table, step in steps.items():
        try:
            done[table] = step()
        except Exception:  # noqa: BLE001 — one kind must not stop the others
            session.rollback()
            logger.exception("housekeeping: %s not trimmed this time", table)
            done[table] = 0
    touched = [t for t, n in done.items() if n]
    if "ai_chats" in touched:
        touched.append("ai_chat_turns")
    vacuum(session, touched)
    return done


def _giro() -> dict[str, int]:
    from app.db.session import engine

    with Session(engine) as session:
        return one_pass(session)


async def housekeeping_loop(stop: asyncio.Event) -> None:
    o = get_settings().orchestrator
    logger.info("housekeeping: run logs %s days, contract reports %s per datasource, %s versions, "
                "assistant conversations %s days (0 = kept), every %ss", o.log_retention_days,
                o.contract_results_kept, o.versions_kept, o.ai_chat_retention_days, EVERY_SECONDS)
    while not stop.is_set():
        try:
            done = await asyncio.to_thread(_giro)
            if any(done.values()):
                logger.info("housekeeping: %s", ", ".join(f"{t} {n}" for t, n in done.items() if n))
        except Exception:  # noqa: BLE001 — the cleaning must never stop the gateway
            logger.exception("housekeeping: a pass failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=EVERY_SECONDS)
        except asyncio.TimeoutError:
            pass
