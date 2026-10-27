"""Keeping the database from growing without end. The data itself lives in the object
storage; what piles up in Postgres is history, and every hour the orchestrators trim it:

- the runs' logs, past ORCHESTRATOR__LOG_RETENTION_DAYS (services/run_log_retention.py);
- the data contracts' reports: the latest ORCHESTRATOR__CONTRACT_RESULTS_KEPT per
  datasource (a refresh every five minutes adds 288 a day), never one whose notice is
  still to be sent;
- the versions of a flow, of a saved script and of a SQL script: the latest ORCHESTRATOR__VERSIONS_KEPT,
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
BATCH = 1000
MAX_BATCHES = 200


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC columns


def _in_batches(session: Session, sql: str, **params) -> int:
    """Runs a DELETE … WHERE id IN (… LIMIT :batch) batch after batch."""
    removed_items = 0
    for _ in range(MAX_BATCHES):
        n = session.exec(text(sql).bindparams(batch=BATCH, **params)).rowcount or 0
        session.commit()
        removed_items += n
        if n < BATCH:
            break
    return removed_items


def contract_results(session: Session, kept: int) -> int:
    if kept <= 0:
        return 0
    return _in_batches(session, """
        DELETE FROM data_contract_results WHERE id IN (
            SELECT id FROM (
                SELECT id, notify, ROW_NUMBER() OVER (PARTITION BY datasource_id ORDER BY id DESC) AS n
                FROM data_contract_results) t
            WHERE t.n > :kept AND (t.notify IS NULL OR t.notify NOT IN ('pending', 'sending'))
            LIMIT :batch)""", kept=kept)


def flow_versions(session: Session, kept: int) -> int:
    if kept <= 0:
        return 0
    # the highest version is the flow's current one: always among those kept
    return _in_batches(session, """
        DELETE FROM flow_versions WHERE id IN (
            SELECT id FROM (
                SELECT id, ROW_NUMBER() OVER (PARTITION BY flow_id ORDER BY version DESC) AS n
                FROM flow_versions) t
            WHERE t.n > :kept LIMIT :batch)""", kept=kept)


def script_versions(session: Session, kept: int) -> int:
    if kept <= 0:
        return 0
    return _in_batches(session, """
        DELETE FROM saved_script_versions WHERE id IN (
            SELECT t.id FROM (
                SELECT id, script_id, version,
                       ROW_NUMBER() OVER (PARTITION BY script_id ORDER BY version DESC) AS n
                FROM saved_script_versions) t
            WHERE t.n > :kept AND NOT EXISTS (
                SELECT 1 FROM saved_scripts s WHERE s.id = t.script_id AND s.current_version = t.version)
            LIMIT :batch)""", kept=kept)


def sql_script_versions(session: Session, kept: int) -> int:
    if kept <= 0:
        return 0
    return _in_batches(session, """
        DELETE FROM sql_script_versions WHERE id IN (
            SELECT t.id FROM (
                SELECT id, script_id, version,
                       ROW_NUMBER() OVER (PARTITION BY script_id ORDER BY version DESC) AS n
                FROM sql_script_versions) t
            WHERE t.n > :kept AND NOT EXISTS (
                SELECT 1 FROM sql_scripts s WHERE s.id = t.script_id AND s.current_version = t.version)
            LIMIT :batch)""", kept=kept)


def ai_chats(session: Session, days: int, at: datetime | None = None) -> int:
    if days <= 0:
        return 0
    cutoff = (at or _now()) - timedelta(days=days)
    removed_ones = 0
    for _ in range(MAX_BATCHES):
        ids = [i for (i,) in session.execute(
            text("SELECT id FROM ai_chats WHERE updated_at < :cutoff LIMIT :batch"),
            {"cutoff": cutoff, "batch": BATCH}).all()]
        if not ids:
            break
        session.execute(text("DELETE FROM ai_chat_turns WHERE chat_id IN :ids").bindparams(
            bindparam("ids", expanding=True)), {"ids": ids})
        session.execute(text("DELETE FROM ai_chats WHERE id IN :ids").bindparams(
            bindparam("ids", expanding=True)), {"ids": ids})
        session.commit()
        removed_ones += len(ids)
        if len(ids) < BATCH:
            break
    return removed_ones


def vacuum(session: Session, tables: list[str]) -> None:
    """Marks the space the pass freed for reuse (Postgres only; outside a transaction)."""
    bind = session.get_bind()
    if not tables or bind.dialect.name != "postgresql":
        return
    with bind.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        for table in tables:
            conn.exec_driver_sql(f"VACUUM (ANALYZE) {table}")  # names of this module, never input


def one_pass(session: Session, at: datetime | None = None) -> dict[str, int]:
    """Trims every kind once; returns how many rows each touched, and vacuums those.
    A kind that fails (a deadlock with another replica's pass, a lost connection) is
    logged and left to the next pass: the others are trimmed all the same."""
    o = get_settings().orchestrator
    steps = {
        "runs": lambda: run_log_retention.empty_out(session, o.log_retention_days, at=at),
        "data_contract_results": lambda: contract_results(session, o.contract_results_kept),
        "flow_versions": lambda: flow_versions(session, o.versions_kept),
        "saved_script_versions": lambda: script_versions(session, o.versions_kept),
        "sql_script_versions": lambda: sql_script_versions(session, o.versions_kept),
        "ai_chats": lambda: ai_chats(session, o.ai_chat_retention_days, at=at),
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


def _round() -> dict[str, int]:
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
            done = await asyncio.to_thread(_round)
            if any(done.values()):
                logger.info("housekeeping: %s", ", ".join(f"{t} {n}" for t, n in done.items() if n))
        except Exception:  # noqa: BLE001 — the cleaning must never stop the gateway
            logger.exception("housekeeping: a pass failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=EVERY_SECONDS)
        except asyncio.TimeoutError:
            pass
