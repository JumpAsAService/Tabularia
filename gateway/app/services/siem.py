"""Forwarding the audit log to a SIEM: Splunk, with the HTTP Event Collector (HEC).

What goes out: every audit item (sign-ins, failed sign-ins, creations and changes,
runs, exports, permissions, tokens, settings…), one per HEC event, with the item's
fields (`event`). From when the forwarding is first turned on: the earlier history
stays in the Audit panel.

How, without losing any and without new queues: the audit table IS the queue. The
destination remembers the id of the last item delivered (`cursor_id`); a round takes
the items after it, sends them in batches and moves the cursor ONLY when Splunk said
yes (a 2xx answer, `code` 0). A fault (Splunk down, wrong token, network) leaves the
cursor where it is and it retries with growing waits up to `MAX_WAIT`: nothing is
skipped. So AT LEAST ONCE: a round broken off between the answer and saving the
cursor sends that batch again, and every event carries the item's `id` to recognize
duplicates (`| dedup id`).

Two cautions:
- the ids are assigned before the commit: an item with a lower id may become visible
  after one with a higher id. Only items older than `DELAY` are forwarded, so the
  cursor never passes over an item not written yet;
- several processes (the orchestrators) run the forwarding: a lock in the database
  (the same mechanism as the engine settings) makes only one send, batch by batch.

The log's expiry (AUDIT__RETENTION_MINUTES) stays the one promised in the privacy
notice: when Splunk does not receive for longer than that, the oldest items are
deleted before they go. The panel shows how far behind it is.

Connection: as the webhooks (services/webhooks.py): the name is resolved once, it
connects to the checked address, the certificate is checked against the name
(certifi's CA or SSL_CERT_FILE). `verify_tls` off is the administrator's explicit
choice for a Splunk with the factory certificate.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

import httpx
from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.models import AuditLog, SiemDestination
from app.services import webhooks

logger = logging.getLogger(__name__)

CYCLE = 5.0  # seconds between one round and the next
DELAY = timedelta(seconds=10)  # the items older than this are forwarded
BATCH = 500  # items per request
MAX_BYTE = 900_000  # and no more than these bytes (HEC has a cap per request)
MAX_BATCHES_PER_ROUND = 20
MAX_WAIT = 300  # seconds: the cap of the waits between one failed attempt and the next
READ_BYTES = 4096
LOCK = 7_402_122
SOURCETYPE, SOURCE, HOST = "tabularia:audit", "tabularia", "tabularia"
# the HEC channel: required when the token has indexer acknowledgement (useACK),
# ignored otherwise. One per process is enough
CHANNEL = str(uuid.uuid4())


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def endpoint(url: str) -> str:
    """The HEC's address: the administrator may paste the base (https://splunk:8088), the
    collector or the whole endpoint."""
    u = url.strip().rstrip("/")
    if u.endswith("/services/collector/event"):
        return u
    if u.endswith("/services/collector"):
        return u + "/event"
    if urlsplit(u).path in ("", "/"):  # the standard library: it does not raise on an odd address
        return u + "/services/collector/event"
    return u  # a path of its own (a proxy in front of Splunk): as it is


def _utc(ts: datetime) -> datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def event(item: AuditLog, d: SiemDestination) -> dict:
    """An audit item as a HEC event. The names follow the log; `user` and `src` are those
    Splunk expects (CIM) for who and from where."""
    try:
        detail = json.loads(item.detail) if item.detail else None
    except ValueError:
        detail = {"_raw": item.detail}
    body = {
        "id": item.id, "time": _utc(item.ts).isoformat().replace("+00:00", "Z"),
        "action": item.action, "outcome": item.outcome,
        "user": item.actor_label, "user_id": item.actor_id, "src": item.ip, "user_agent": item.user_agent,
        "target_type": item.target_type, "target_id": item.target_id, "target_label": item.target_label,
        "detail": detail, "app": "tabularia",
    }
    hec = {
        "time": round(_utc(item.ts).timestamp(), 3), "host": d.host or HOST, "source": d.source or SOURCE,
        "sourcetype": d.sourcetype or SOURCETYPE, "event": {k: v for k, v in body.items() if v is not None},
    }
    if d.index:
        hec["index"] = d.index
    return hec


def _row(e: dict) -> bytes:
    return json.dumps(e, ensure_ascii=False, default=str, separators=(",", ":")).encode()


async def send(d: SiemDestination, events: list[dict],
                   client: Optional[httpx.AsyncClient] = None) -> tuple[bool, Optional[int], str]:
    """(success, HTTP code, Splunk's or the error's message). It does not raise."""
    if not d.token_enc:
        return False, None, "no token configured"
    body = b"\n".join(_row(e) for e in events)
    try:
        u, host, ip = await webhooks._resolve(endpoint(d.url))
    except (webhooks._Forbidden, webhooks._Unresolved) as e:
        return False, None, str(e)[:500]
    destination = u.copy_with(host=ip)
    hdrs = {
        "Authorization": f"Splunk {decrypt_secret(d.token_enc)}", "Content-Type": "application/json",
        "X-Splunk-Request-Channel": CHANNEL, "Host": u.netloc.decode("ascii"),
    }
    extensions = {"sni_hostname": host} if u.scheme == "https" else {}
    own_one = client is None
    if own_one:
        client = httpx.AsyncClient(
            verify=webhooks._tls_context() if d.verify_tls else False,
            timeout=get_settings().webhooks.timeout_seconds, follow_redirects=False, trust_env=False,
        )
    try:
        async with client.stream("POST", destination, content=body, headers=hdrs, extensions=extensions) as r:
            read_ones = b""
            async for piece in r.aiter_bytes():
                read_ones += piece
                if len(read_ones) >= READ_BYTES:
                    break
    except Exception as e:  # noqa: BLE001 — network, TLS, timeout: it is this attempt's outcome
        return False, None, f"{type(e).__name__}: {e}"[:500]
    finally:
        if own_one:
            await client.aclose()
    text = read_ones[:500].decode("utf-8", "replace")
    try:
        answer = json.loads(read_ones)
    except ValueError:
        answer = None
    if 200 <= r.status_code < 300 and (not isinstance(answer, dict) or answer.get("code", 0) == 0):
        return True, r.status_code, (answer or {}).get("text", "Success") if isinstance(answer, dict) else "Success"
    if isinstance(answer, dict) and answer.get("text"):
        text = f"{answer['text']} (code {answer.get('code')})"
    return False, r.status_code, f"response {r.status_code}: {text}"[:500]


def _batch(session: Session, after: int) -> list[AuditLog]:
    return list(session.exec(
        select(AuditLog).where(AuditLog.id > after, AuditLog.ts <= now() - DELAY).order_by(AuditLog.id).limit(BATCH)
    ).all())


def _within_the_cap(events: list[dict]) -> int:
    """How many of the first events fit in one request (at least one)."""
    total = 0
    for i, e in enumerate(events):
        total += len(_row(e)) + 1
        if total > MAX_BYTE and i:
            return i
    return len(events)


def wait(failures: int) -> float:
    """5, 10, 20, 40… seconds, up to MAX_WAIT."""
    return min(MAX_WAIT, 5 * 2 ** max(0, failures - 1))


async def one_round(bind: Engine, client: Optional[httpx.AsyncClient] = None) -> int:
    """Sends what there is to send (up to MAX_BATCHES_PER_ROUND batches). Returns how many items
    went out. One batch at a time with the lock: another process that finds it taken
    skips the round."""
    from app.services.engine_panel_settings import lock

    started = 0
    for _ in range(MAX_BATCHES_PER_ROUND):
        with Session(bind) as session:
            if not await lock(session, wait=0, key=LOCK):
                return started
            d = session.get(SiemDestination, 1)
            if d is None or not d.enabled or not d.url or not d.token_enc:
                return started
            if d.next_attempt_at and d.next_attempt_at > now():
                return started
            items = _batch(session, d.cursor_id)
            if not items:
                return started
            events = [event(v, d) for v in items]
            n = _within_the_cap(events)
            ok, code, message = await send(d, events[:n], client)
            at = now()
            if ok:
                d.cursor_id, d.sent_total = items[n - 1].id, (d.sent_total or 0) + n
                d.last_ok_at, d.failures, d.next_attempt_at = at, 0, None
                started += n
            else:
                d.failures = (d.failures or 0) + 1
                d.last_error, d.last_error_at = message, at
                d.next_attempt_at = at + timedelta(seconds=wait(d.failures))
                logger.warning("siem: %s items not delivered (%s), retrying in %ss", n, message, wait(d.failures))
            session.add(d)
            session.commit()  # the lock is released with the cursor too
            if not ok:
                return started
    return started


async def forwarding_loop(stop: asyncio.Event) -> None:
    from app.db.session import engine

    logger.info("siem: audit forwarding started (every %ss)", CYCLE)
    while not stop.is_set():
        try:
            await one_round(engine)
        except Exception:  # noqa: BLE001 — forwarding must never stop the gateway
            logger.exception("siem: forwarding round failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=CYCLE)
        except asyncio.TimeoutError:
            pass


def backlog(session: Session, d: SiemDestination) -> tuple[int, Optional[datetime]]:
    """How many items are waiting to go out, and since when the oldest."""
    n, oldest = session.exec(
        select(func.count(), func.min(AuditLog.ts)).where(AuditLog.id > d.cursor_id)
    ).one()
    return int(n or 0), oldest


def last_id(session: Session) -> int:
    return int(session.exec(select(func.max(AuditLog.id))).one() or 0)
