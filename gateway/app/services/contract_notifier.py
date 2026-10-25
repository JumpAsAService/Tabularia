"""Notifies when a data contract's state changes.

Whoever records a report (a refresh, a publish, the freshness check) only marks that a
notice is due (`DataContractResult.notify = 'pending'`, see
`contracts.record_result`); here it is sent. So no email goes out inside the
transaction that publishes the data, and the notice survives a restart between the
report and the sending.

The rules of the flows' failure notice hold (`services.notifier`): only at a change of
state — whoever records the report decides it — and it never raises. With several
processes every notice belongs to whoever takes it first; when that process dies
halfway the notice stays «sending» and does not start again: better one email less
than two.
"""
from __future__ import annotations

import json
import logging
import time

from sqlalchemy import update
from sqlmodel import Session, select

from app.core.engine_client import get_engine_client
from app.models import Connection, DataContractResult, Datasource
from app.routes.connections import engine_connection_payload
from app.services import audit, contracts, email_texts

logger = logging.getLogger(__name__)

MAX_RULES_LISTED = 10
# The scheduler's round launches the scheduled flows too: a mail server that does not
# answer (30 s per attempt) must not hold it for minutes. Past this time the notices
# left wait for the next round.
BUDGET_SECONDS = 30.0

def _row(x: dict, lang: str = email_texts.DEFAULT) -> str:
    t = email_texts.text
    where = x.get("column") or ", ".join(x.get("columns") or []) or x.get("name") or ""
    severity = t(lang, "severity_warning" if x.get("severity") == "warning" else "severity_error")
    head = f"[{severity}] {x.get('kind')}" + (f" {t(lang, 'rule_on', where=where)}" if where else "")
    if x.get("error"):
        return f"{head}: {t(lang, 'rule_unevaluable', error=str(x['error'])[:200])}"
    if x.get("violations"):
        return f"{head}: {t(lang, 'rule_rows', n=x['violations'])}"
    if x.get("observed") is not None:
        return f"{head}: {t(lang, 'rule_observed', value=x['observed'])}"
    return head


def message(ds: Datasource, r: DataContractResult, lang: str = email_texts.DEFAULT) -> tuple[str, str]:
    """(subject, body) of the notice for this evaluation, in `lang` (services/email_texts.py)."""
    t = email_texts.text
    state = "refused" if r.blocked else r.outcome
    try:
        report = json.loads(r.report)
    except json.JSONDecodeError:
        report = {}
    routes = [x for x in report.get("rules", []) if not x.get("passed")]
    title = t(lang, f"title_{state}") if f"title_{state}" in email_texts.TEXTS["en"] else state
    sentence = t(lang, f"phrase_{state}") if f"phrase_{state}" in email_texts.TEXTS["en"] else ""
    origin = t(lang, f"origin_{r.trigger}") if f"origin_{r.trigger}" in email_texts.TEXTS["en"] else r.trigger
    fields = [
        ("when", r.evaluated_at.strftime("%Y-%m-%d %H:%M UTC")),
        ("origin", origin + (f" {t(lang, 'contract_run', run=r.run_id)}" if r.run_id else "")),
        ("contract", t(lang, "contract_version", version=r.contract_version)),
    ]
    if r.rows is not None:
        fields.append(("rows", str(r.rows)))
    rows = [t(lang, "contract_intro", ds=ds.name, title=title), "", sentence, ""] + email_texts.labelled(lang, fields)
    if routes:
        rows += ["", t(lang, "contract_broken")] + [f"  - {_row(x, lang)}" for x in routes[:MAX_RULES_LISTED]]
        if len(routes) > MAX_RULES_LISTED:
            rows.append(f"  {t(lang, 'contract_more', n=len(routes) - MAX_RULES_LISTED)}")
    elif report.get("error"):
        rows += ["", t(lang, "contract_unevaluable", error=str(report["error"])[:300])]
    rows += ["", "—", t(lang, "contract_footer")]
    return t(lang, "contract_subject", ds=ds.name, title=title), "\n".join(rows)


async def _send(session: Session, result_id: int) -> str:
    """Sends the notice of an evaluation: 'sent' | 'failed' | 'skipped'."""
    r = session.get(DataContractResult, result_id)
    ds = session.get(Datasource, r.datasource_id) if r else None
    c = contracts.get(session, r.datasource_id) if r else None
    if ds is None or c is None:
        return "skipped"  # datasource or contract removed meanwhile
    to = [x.strip() for x in (c.notify_emails or "").split(",") if x.strip()]
    conn = session.get(Connection, c.notify_connection_id) if c.notify_connection_id else None
    if not to or conn is None or conn.db_type != "smtp":
        return "skipped"
    # one message per language: each recipient reads it in the language chosen in
    # the interface, English for an address that is not a user
    ok = True
    for lang, group in email_texts.by_language(session, to):
        obj, body = message(ds, r, lang)
        resp = await get_engine_client().post(
            "/db/notify", json={"connection": engine_connection_payload(conn), "to": group, "subject": obj, "body": body},
            timeout=60,
        )
        if resp.status_code >= 400:
            ok = False
            logger.warning("data contract notice of datasource %s refused by the engine: %s", ds.id, resp.text[:300])
    audit.record_audit(
        session, actor=None, actor_label="scheduler", action=audit.EMAIL_SEND, outcome="success" if ok else "failure",
        target_type="datasource", target_id=ds.id, target_label=ds.name,
        detail={"reason": "contract_state", "state": "refused" if r.blocked else r.outcome, "result_id": r.id, "recipients": len(to)},
    )
    return "sent" if ok else "failed"


def _mark(session: Session, result_id: int, from_state: str | None, to: str) -> bool:
    cond = [DataContractResult.id == result_id] + ([DataContractResult.notify == from_state] if from_state else [])
    done = session.exec(update(DataContractResult).where(*cond).values(notify=to))  # type: ignore[call-overload]
    session.commit()
    return done.rowcount == 1


async def deliver_pending(session: Session, limit: int = 20, budget_seconds: float = BUDGET_SECONDS) -> int:
    """Sends the due notices. The scheduler calls it at every round. Returns how many went
    out. It does not raise."""
    sent_ones, start = 0, time.monotonic()
    try:
        ids = session.exec(
            select(DataContractResult.id).where(DataContractResult.notify == "pending").order_by(DataContractResult.id).limit(limit)
        ).all()
    except Exception:  # noqa: BLE001
        session.rollback()
        logger.exception("data contract notices: the queue cannot be read")
        return 0
    for rid in ids:
        if time.monotonic() - start > budget_seconds:
            logger.warning("data contract notices: the round's time is up, the others at the next")
            break
        try:
            if not _mark(session, rid, "pending", "sending"):
                continue  # another process took it
            outcome = await _send(session, rid)
        except Exception:  # noqa: BLE001 — a broken notice stops neither the others nor the scheduler
            session.rollback()
            logger.exception("data contract notice not sent (evaluation %s)", rid)
            outcome = "failed"
        try:
            _mark(session, rid, None, outcome)
        except Exception:  # noqa: BLE001
            session.rollback()
            logger.exception("data contract notice: outcome not recorded (evaluation %s)", rid)
        sent_ones += outcome == "sent"
    return sent_ones
