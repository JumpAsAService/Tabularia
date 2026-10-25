"""Notifies when a scheduled run fails.

The product says «schedule it and forget it», but until now forgetting was the only
option: a flow failing at three in the morning stayed failed silently, and one found
out opening the history or noticing the data was old. This module is the second half
of the promise.

Three rules worth saying:

- **Only the scheduled runs.** Whoever launches by hand is looking at the screen
  already; notifying them would be noise, and noise makes the real notices ignored
  too.
- **Only at the MOVE to failure.** A broken flow firing every five minutes would send
  three hundred emails a day, and by the third nobody would read them any more. It
  notifies when the previous run had gone well.
- **It never raises.** A notice that fails must not change the outcome of anything:
  the run has failed already, and an error here would only make it harder to
  understand.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Session, select

from app.core.engine_client import get_engine_client
from app.models import Connection, Flow, Run
from app.models.run import TERMINAL_STATES
from app.services import audit, email_texts
from app.routes.connections import engine_connection_payload

logger = logging.getLogger(__name__)

MAX_ERROR_CHARS = 1200


def recipients(flow: Flow) -> list[str]:
    raw_items = (flow.notify_emails or "").replace(";", ",").split(",")
    return [a.strip() for a in raw_items if a.strip()]


def _was_failing_already(session: Session, flow_id: int, run_id: int) -> bool:
    """Had the PREVIOUS run of the same flow failed already?

    If so it keeps quiet: the first notice said what there was to say, and repeating it
    at every firing turns the notification into noise to filter."""
    previous = session.exec(
        select(Run)
        .where(
            Run.flow_id == flow_id, Run.kind == "orchestration", Run.id != run_id,
            Run.status.in_(TERMINAL_STATES),  # type: ignore[union-attr]
            # a launch refused because the flow was running already is not a run
            # (the Italian text is the one recorded on runs before the English one)
            (Run.error.is_(None)) | (Run.error.not_in(("flow already running", "flusso già in esecuzione"))),  # type: ignore[union-attr]
        )
        .order_by(Run.started_at.desc(), Run.id.desc())
    ).first()
    return previous is not None and previous.status == "FAILURE"


def _body(flow: Flow, run: Run, lang: str = email_texts.DEFAULT) -> str:
    """The body of the notice, in `lang` (see services/email_texts.py)."""
    when = (run.finished_at or datetime.now(timezone.utc)).strftime("%Y-%m-%d %H:%M UTC")
    fields = [("when", when), ("run", f"#{run.id}")]
    if run.engine:
        fields.append(("engine", run.engine))
    rows = [email_texts.text(lang, "flow_intro", flow=flow.name), ""] + email_texts.labelled(lang, fields)
    if run.error:
        rows += ["", email_texts.text(lang, "error"), run.error[:MAX_ERROR_CHARS]]
    rows += ["", "—", email_texts.text(lang, "flow_footer")]
    return "\n".join(rows)


async def notify_failure(session: Session, run: Run) -> None:
    """Sends the notice, when the flow asked for it and the conditions are there."""
    try:
        # A flow's scheduled run IS the orchestration run (the "flow" runs are its children,
        # one per Output node). While "flow" was written here the notice never went out: the
        # caller passes the orchestration.
        if run.kind != "orchestration" or run.status != "FAILURE" or run.trigger_type != "schedule":
            return
        flow = session.get(Flow, run.flow_id) if run.flow_id else None
        if flow is None:
            return
        a = recipients(flow)
        if not a or not flow.notify_connection_id:
            return
        conn = session.get(Connection, flow.notify_connection_id)
        if conn is None or conn.db_type != "smtp":
            logger.info("notice for flow %s skipped: SMTP connection missing", flow.id)
            return
        if _was_failing_already(session, flow.id, run.id):
            logger.info("notice for flow %s skipped: it was failing already", flow.id)
            return

        # one message per language: each recipient reads it in the language chosen
        # in the interface, English for an address that is not a user
        ok = True
        for lang, group in email_texts.by_language(session, a):
            payload = {
                "connection": engine_connection_payload(conn),
                "to": group,
                "subject": email_texts.text(lang, "flow_subject", flow=flow.name),
                "body": _body(flow, run, lang),
            }
            resp = await get_engine_client().post("/db/notify", json=payload, timeout=60)
            if resp.status_code >= 400:
                ok = False
                logger.warning("notice for flow %s refused by the engine: %s", flow.id, resp.text[:300])
        audit.record_audit(
            session, actor=None, actor_label="scheduler", action=audit.EMAIL_SEND,
            outcome="success" if ok else "failure",
            target_type="flow", target_id=flow.id, target_label=flow.name,
            detail={"reason": "run_failed", "run_id": run.id, "recipients": len(a)},
        )
    except Exception:  # noqa: BLE001 — the run has failed already: a broken notice must not make it worse
        logger.exception("failure notice not sent (run %s)", getattr(run, "id", "?"))
