"""Webhooks: Tabularia calls an external address when a run ends.

Events (`EVENTS`): a flow ended well or badly (both from the orchestrators' queue and
launched directly), a datasource's refresh ended well or badly, and `ping` to try
the address.

Delivery:
- the delivery (`WebhookDelivery`) is born in the SAME transaction that closes the
  run (`enqueue_for_run`, called by whoever closes): when the process dies right
  after, the delivery is there already. The body is built reading with a connection
  of its own: a fault here is logged and that is all, it never prevents closing a
  run;
- `delivery_loop` sends it, in the orchestrators: it fills the free slots
  (`PARALLEL`) with the due deliveries as soon as one frees up (and every `CYCLE`
  seconds anyway), without waiting for those in flight already, and at most
  `PER_WEBHOOK` for the same webhook: a receiver that does not answer, with a long
  queue, does not hold back the others' deliveries, and a long queue towards a fast
  receiver empties at the receiver's speed. Several processes take it with a
  conditional UPDATE (only one wins); when whoever took it dies halfway, after
  `RECLAIM_AFTER` it goes back in the queue. So AT LEAST ONCE delivery: the receiver drops
  duplicates by id (`X-Tabularia-Delivery`, and `id` in the body);
- an error or a non-2xx answer retries after `WAITS`, then it is taken as failed;
- every delivery is signed: `X-Tabularia-Signature: t=<seconds>,v1=<hex>`, where
  v1 = HMAC-SHA256(secret, "<t>.<body>"). The receiver redoes the sum and checks that
  t is recent (docs/api.md has the example).

Addresses: http or https. Loopback, link-local (169.254.169.254 of the clouds'
metadata included), multicast and reserved addresses are not called, nor the other
known metadata addresses (`METADATA_ADDRESSES`). The name is resolved ONCE, with the
international names' encoding httpx uses, and the connection goes to the checked
address (the name stays in the Host header and in TLS, where the certificate is
checked against the name): a DNS that changes its answer between the check and the
connection does not lead elsewhere. Every delivery has a client of its own: a shared
pool, which sees only the address, would reuse the TLS connection checked for one
name for the request of another name on the same address (found by trying it live).
The CAs are certifi's, or `SSL_CERT_FILE`/`SSL_CERT_DIR` for a receiver with a
certificate of the internal CA. No system proxy, no redirects. Private networks are
allowed, because an internal integration (Airflow in the same network) is the normal
case. Only an administrator creates webhooks.

Sending: the database reads and writes sit in a thread, off the event loop and
without open transactions during the HTTP call; every call has an overall maximum
time, and at most `READ_BYTES` bytes of the answer are read. A delivery never brings the
others down: any error becomes that delivery's outcome.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
import functools
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import ssl
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, func, update
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.crypto import decrypt_secret, encrypt_secret
from app.models import Datasource, Flow, Project, Run, Webhook, WebhookDelivery

logger = logging.getLogger(__name__)

EVENTS = (
    "flow.run.succeeded",
    "flow.run.failed",
    "datasource.refresh.succeeded",
    "datasource.refresh.failed",
)
PING = "ping"
CYCLE = 2.0  # seconds between one round of sending and the next
RECLAIM_AFTER = timedelta(minutes=5)  # a delivery taken and never closed goes back in the queue after
# the wait before attempt n+1: six attempts in all, the last after ~7 and a half hours
WAITS = (30, 120, 600, 3600, 21600)
MAX_ATTEMPTS = len(WAITS) + 1
BATCH = 20
PARALLEL = 16  # calls in flight together, per process
PER_WEBHOOK = 2  # …of which at most this many towards the same webhook
READ_BYTES = 4096  # bytes of the answer read (for the log)
# the clouds' metadata addresses outside link-local, which ipaddress's categories do
# not recognize: Alibaba, AWS in IPv6, Azure (wireserver)
METADATA_ADDRESSES = tuple(ipaddress.ip_network(n) for n in ("100.100.100.200/32", "fd00:ec2::254/128", "168.63.129.16/32"))


def now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(d: Optional[datetime]) -> Optional[str]:
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat().replace("+00:00", "Z")


# ── the subscription ────────────────────────────────────────────────────────

def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(32)


def encrypt(secret: str) -> str:
    return encrypt_secret(secret)


def events_of(w: Webhook) -> list[str]:
    try:
        return [e for e in json.loads(w.events or "[]") if isinstance(e, str)]
    except ValueError:
        return []


def validate_url(url: str) -> str:
    """The address's form, at saving. Where it points is checked at every send
    (`_resolve`): a name may change address."""
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("The address must be http:// or https:// with a host")
    if len(url) > 2000:
        raise ValueError("Address too long (at most 2000 characters)")
    try:
        _read_host(httpx.URL(url))
    except (httpx.InvalidURL, ValueError) as e:  # UnicodeError is a ValueError
        raise ValueError(f"Invalid address: {e}") from None
    return url


def _read_host(u: httpx.URL) -> str:
    """The host as httpx uses it to connect, checked as the resolver will check it (an
    empty or too long label makes it fail), and the port."""
    host = u.raw_host.decode("ascii")
    try:
        ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        host.encode("idna")
    if u.port is not None and not 0 < u.port < 65536:
        raise ValueError(f"port out of range: {u.port}")
    return host


def _forbidden_ip(ip: ipaddress._BaseAddress) -> bool:
    if ip.version == 6 and ip.ipv4_mapped is not None:  # ::ffff:127.0.0.1 is 127.0.0.1
        ip = ip.ipv4_mapped
    if ip.version == 6 and ip.sixtofour is not None and _forbidden_ip(ip.sixtofour):  # 2002:7f00:1:: (6to4)
        return True
    return (ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified or ip.is_reserved
            or any(ip in network for network in METADATA_ADDRESSES))


class _Forbidden(Exception):
    """The address is not called: retrying would change nothing."""


class _Unresolved(Exception):
    """The name does not resolve now (a DNS restarting): it retries."""


async def _resolve(url: str) -> tuple[httpx.URL, str, str]:
    """(url, host as httpx uses it, the checked address to connect to). When any of the
    name's addresses is forbidden, the name is forbidden: with several addresses there
    is no knowing which the resolver would pick next time."""
    try:
        u = httpx.URL(url)
        host = _read_host(u)  # IDNA as httpx encodes it to connect
    except (httpx.InvalidURL, ValueError) as e:  # saved before the check, or httpx changed
        raise _Forbidden(f"invalid address: {e}") from None
    try:
        addresses = [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        try:
            info = await asyncio.get_running_loop().getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80),
                                                                type=socket.SOCK_STREAM)
        except UnicodeError as e:  # the name does not encode: it will not change by retrying
            raise _Forbidden(f"invalid address: {e}") from None
        except OSError as e:
            raise _Unresolved(f"name not resolved: {e}") from None
        addresses = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in info]
    if not addresses:
        raise _Unresolved("name without addresses")
    for ip in addresses:
        if _forbidden_ip(ip):
            raise _Forbidden(f"address not allowed for a webhook: {ip}")
    return u, host, str(addresses[0])


# ── the events ───────────────────────────────────────────────────────────────

def run_event(run: Run) -> Optional[str]:
    """Which event this run's closing tells, if it tells one. The runs of the single
    outputs of an orchestration do not: the orchestration tells it."""
    outcome = {"SUCCESS": "succeeded", "FAILURE": "failed"}.get(run.status)
    if outcome is None:
        return None
    if getattr(run, "flow_group_id", None) is not None or getattr(run, "saved_script_id", None) is not None:
        # a flow group's own run, a saved script's: no event of their own yet; the
        # flows and the refreshes inside a group send theirs
        return None
    if run.kind == "orchestration" or (run.kind == "flow" and run.parent_run_id is None):
        return f"flow.run.{outcome}"
    if run.kind == "ingest":
        return f"datasource.refresh.{outcome}"
    return None


def _run_data(reading: Session, run: Run) -> tuple[dict, Optional[int]]:
    """The event's content, and the folder it comes from (for the filters)."""
    duration = None
    if run.started_at and run.finished_at:
        without_zone = lambda d: d.replace(tzinfo=None) if d.tzinfo else d  # noqa: E731 — they come back naive UTC from the DB
        duration = round((without_zone(run.finished_at) - without_zone(run.started_at)).total_seconds(), 1)
    data: dict = {"run": {
        "id": run.id, "kind": run.kind, "status": run.status, "trigger": run.trigger_type,
        "started_at": _iso(run.started_at), "finished_at": _iso(run.finished_at),
        "duration_seconds": duration, "rows_written": run.rows_written,
        "error": (run.error or None) and run.error[:1000],
    }}
    folder = None
    if run.flow_id is not None and run.kind != "ingest":
        flow = reading.get(Flow, run.flow_id)
        if flow is not None:
            data["flow"] = {"id": flow.id, "name": flow.name}
            folder = flow.project_id
    if run.kind == "ingest" and run.datasource_id is not None:
        ds = reading.get(Datasource, run.datasource_id)
        if ds is not None:
            data["datasource"] = {"id": ds.id, "name": ds.name}
            folder = ds.project_id
    if folder is not None:
        p = reading.get(Project, folder)
        data["folder"] = {"id": folder, "name": p.name if p else None}
    return data, folder


def _body(event: str, data: dict) -> str:
    return json.dumps(
        {"id": str(uuid.uuid4()), "event": event, "created_at": _iso(now()), "data": data},
        ensure_ascii=False, separators=(",", ":"),
    )


def _in_the_folder(reading: Session, w: Webhook, folder: Optional[int]) -> bool:
    if w.project_id is None:
        return True
    if folder is None:
        return False
    from app.services.permissions import ancestor_ids, folder_parents

    return w.project_id == folder or w.project_id in ancestor_ids(folder_parents(reading), folder)


def enqueue_for_run(session: Session, run: Run) -> int:
    """To call BEFORE the commit that closes the run, with `run` read again in the same
    transaction (new state and end): the deliveries go into that commit. It does not
    raise: a broken webhook never prevents closing a run."""
    try:
        event = run_event(run)
        if event is None:
            return 0
        from app.db.session import read_engine

        bind = session.get_bind()
        engine_read = read_engine(bind) if isinstance(bind, Engine) else bind
        with Session(engine_read) as reading:
            subscribed = [
                w for w in reading.exec(select(Webhook).where(Webhook.active == True)).all()  # noqa: E712
                if event in events_of(w)
            ]
            if not subscribed:
                return 0
            data, folder = _run_data(reading, run)
            chosen = [w.id for w in subscribed if _in_the_folder(reading, w, folder)]
        at = now()
        for wid in chosen:
            session.add(WebhookDelivery(
                webhook_id=wid, event=event, payload=_body(event, data), next_attempt_at=at, created_at=at,
            ))
        return len(chosen)
    except Exception:  # noqa: BLE001
        logger.exception("webhook: deliveries of run %s not prepared", getattr(run, "id", "?"))
        return 0


def enqueue_ping(session: Session, w: Webhook) -> WebhookDelivery:
    at = now()
    d = WebhookDelivery(
        webhook_id=w.id, event=PING, next_attempt_at=at, created_at=at,
        payload=_body(PING, {"webhook": {"id": w.id, "name": w.name}, "message": "Tabularia webhook test"}),
    )
    session.add(d)
    session.commit()
    session.refresh(d)
    return d


def resend(session: Session, d: WebhookDelivery) -> WebhookDelivery:
    """A new delivery with the same content and a new id (the receiver drops duplicates by
    id: a wanted resend must not look like a duplicate to it)."""
    body = json.loads(d.payload)
    body["id"] = str(uuid.uuid4())
    at = now()
    new_one = WebhookDelivery(
        webhook_id=d.webhook_id, event=d.event, next_attempt_at=at, created_at=at,
        payload=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
    )
    session.add(new_one)
    session.commit()
    session.refresh(new_one)
    return new_one


# ── sending ──────────────────────────────────────────────────────────────────

def sign(secret: str, instant: int, body: str) -> str:
    return hmac.new(secret.encode(), f"{instant}.{body}".encode(), hashlib.sha256).hexdigest()


def headers(secret: str, event: str, body: str, instant: Optional[int] = None) -> dict[str, str]:
    instant = instant or int(time.time())
    return {
        "Content-Type": "application/json",
        "User-Agent": f"Tabularia-Webhooks/{get_settings().app.version}",
        "X-Tabularia-Event": event,
        "X-Tabularia-Delivery": json.loads(body)["id"],
        "X-Tabularia-Signature": f"t={instant},v1={sign(secret, instant, body)}",
    }


@dataclass
class _Job:
    """A delivery taken, with all it needs to send it: after the take no transaction is
    kept open."""
    id: int
    webhook: int
    url: str
    secret: str
    event: str
    body: str
    attempts: int
    taken_at: datetime


@functools.lru_cache(maxsize=1)
def _tls_context() -> ssl.SSLContext:
    """One per process: loading the CAs costs, and the context is shared among the
    connections (the name to check is each one's own). Reads SSL_CERT_FILE and
    SSL_CERT_DIR when they are there: an internal receiver with the company's CA."""
    return httpx.create_ssl_context(trust_env=True)


def _client() -> httpx.AsyncClient:
    """A client for ONE delivery: it connects to the address, not to the name, and a
    shared pool would reuse for name b the TLS connection checked for name a on the same
    address — skipping the check of b's certificate."""
    return httpx.AsyncClient(
        verify=_tls_context(), timeout=get_settings().webhooks.timeout_seconds, follow_redirects=False,
        trust_env=False,  # no system proxy: the checked address is the one called
    )


async def _ship(job: _Job, client: httpx.AsyncClient) -> tuple[bool, Optional[int], Optional[str]]:
    """(success, HTTP code, error). Raises `_Forbidden` for an address not allowed or not
    valid; otherwise it does not raise."""
    try:
        u, host, ip = await _resolve(job.url)
    except _Unresolved as e:
        return False, None, str(e)[:500]
    # to the checked address: the name is not resolved a second time
    destination = u.copy_with(host=ip)
    hdrs = {**headers(job.secret, job.event, job.body), "Host": u.netloc.decode("ascii")}
    extensions = {"sni_hostname": host} if u.scheme == "https" else {}
    try:
        async with client.stream("POST", destination, content=job.body.encode(), headers=hdrs,
                                 extensions=extensions) as r:
            read_ones = b""
            async for piece in r.aiter_bytes():
                read_ones += piece
                if len(read_ones) >= READ_BYTES:
                    break
    except httpx.HTTPError as e:
        return False, None, f"{type(e).__name__}: {e}"[:500]
    except Exception as e:  # noqa: BLE001
        return False, None, f"{type(e).__name__}: {e}"[:500]
    if 200 <= r.status_code < 300:
        return True, r.status_code, None
    return False, r.status_code, f"response {r.status_code}: {read_ones[:300].decode('utf-8', 'replace')}"


def _take_due(bind: Engine, limit: int, busy: Optional[Counter] = None) -> list[_Job]:
    """Takes up to `limit` due deliveries (in the thread: the database off the event
    loop), the oldest first but at most `PER_WEBHOOK` per webhook, counting those in
    flight in this process already (`busy`). Each belongs to whoever takes it first,
    and only while still due."""
    busy = Counter(busy or {})
    jobs: list[_Job] = []
    with Session(bind) as session:
        at = now()
        # whoever had taken it died halfway: it goes back in the queue
        session.exec(
            update(WebhookDelivery)
            .where(WebhookDelivery.status == "sending", WebhookDelivery.claimed_at < at - RECLAIM_AFTER)
            .values(status="pending")
        )
        session.commit()
        # the first PER_WEBHOOK due ones of each webhook: the long queue of a dead receiver
        # does not hide the others' deliveries behind it
        numbered = (
            select(
                WebhookDelivery.id.label("id"), WebhookDelivery.webhook_id.label("webhook_id"),
                WebhookDelivery.next_attempt_at.label("when"),
                func.row_number().over(
                    partition_by=WebhookDelivery.webhook_id,
                    order_by=(WebhookDelivery.next_attempt_at, WebhookDelivery.id),
                ).label("n"),
            )
            .where(WebhookDelivery.status == "pending", WebhookDelivery.next_attempt_at <= at)
            .subquery()
        )
        candidate = session.exec(
            select(numbered.c.id, numbered.c.webhook_id)
            .where(numbered.c.n <= PER_WEBHOOK)
            .order_by(numbered.c.when, numbered.c.id)
            .limit(limit + PER_WEBHOOK * len(busy))
        ).all()
        for did, wid in candidate:
            if len(jobs) >= limit:
                break
            if busy[wid] >= PER_WEBHOOK:
                continue
            taken_at = now()
            taken_one = session.exec(
                update(WebhookDelivery)
                .where(WebhookDelivery.id == did, WebhookDelivery.status == "pending",
                       WebhookDelivery.next_attempt_at <= taken_at)
                .values(status="sending", claimed_at=taken_at, attempts=WebhookDelivery.attempts + 1)
            )
            session.commit()
            if (taken_one.rowcount or 0) != 1:
                continue  # another process took it, or it was put off
            d = session.get(WebhookDelivery, did)
            w = session.get(Webhook, d.webhook_id)
            if w is None or not w.active:
                _close_delivery(session, did, taken_at, status="failed", last_error="webhook turned off or removed")
                continue
            jobs.append(_Job(did, w.id, w.url, decrypt_secret(w.secret_enc), d.event, d.payload, d.attempts, taken_at))
            busy[wid] += 1
            session.commit()  # end of the read: no transaction open during the call
    return jobs


def _close_delivery(session: Session, did: int, taken_at: datetime, **values) -> None:
    """Writes the outcome only while the delivery still belongs to whoever took it: a late
    process does not overwrite the one that took it again later."""
    session.exec(
        update(WebhookDelivery)
        .where(WebhookDelivery.id == did, WebhookDelivery.status == "sending", WebhookDelivery.claimed_at == taken_at)
        .values(**values)
    )
    session.commit()


def _record(bind: Engine, job: _Job, ok: bool, code: Optional[int], error: Optional[str], final: bool) -> None:
    values: dict = {"last_status_code": code, "last_error": error}
    if ok:
        values.update(status="delivered", delivered_at=now())
    elif final or job.attempts >= MAX_ATTEMPTS:
        values.update(status="failed")
    else:
        values.update(status="pending", next_attempt_at=now() + timedelta(seconds=WAITS[job.attempts - 1]))
    with Session(bind) as session:
        _close_delivery(session, job.id, job.taken_at, **values)


async def _one_row(bind: Engine, job: _Job, client: Optional[httpx.AsyncClient] = None) -> None:
    """Sends a delivery and writes its outcome. It never raises: an unexpected error is
    THIS delivery's outcome (retried, then failed), not the end of the round for all the
    others."""
    ok, code, error, final = False, None, None, False
    c = client or _client()
    try:
        ok, code, error = await asyncio.wait_for(
            _ship(job, c), timeout=get_settings().webhooks.timeout_seconds * 2,
        )
    except _Forbidden as e:
        error, final = str(e), True
    except asyncio.TimeoutError:
        # the maximum time is for the whole call: a receiver that answers drop by drop does
        # not hold the delivery past RECLAIM_AFTER (and does not make it be sent twice)
        error = "no complete response in time"
    except asyncio.CancelledError:
        raise  # shutdown: the delivery stays taken and goes back in the queue after RECLAIM_AFTER
    except Exception as e:  # noqa: BLE001
        logger.exception("webhook: delivery %s not sent", job.id)
        error = f"internal error: {type(e).__name__}"
    finally:
        if client is None:
            await c.aclose()
    try:
        await asyncio.to_thread(_record, bind, job, ok, code, error, final)
    except Exception:  # noqa: BLE001
        logger.exception("webhook: outcome of delivery %s not recorded", job.id)


class _InFlight:
    """This process's deliveries in flight: each round fills only the free slots, without
    waiting for those started earlier to finish."""

    def __init__(self, bind: Engine, client: Optional[httpx.AsyncClient] = None):
        self.bind, self.client = bind, client
        self.tasks: dict[asyncio.Task, int] = {}  # task → webhook

    async def round_(self, limit: int = PARALLEL) -> int:
        for t in [t for t in self.tasks if t.done()]:
            self.tasks.pop(t, None)
        free = min(limit, PARALLEL - len(self.tasks))
        if free <= 0:
            return 0
        try:
            jobs = await asyncio.to_thread(_take_due, self.bind, free, Counter(self.tasks.values()))
        except Exception:  # noqa: BLE001
            logger.exception("webhook: the deliveries' queue cannot be read")
            return 0
        for x in jobs:
            t = asyncio.create_task(_one_row(self.bind, x, self.client))
            self.tasks[t] = x.webhook
            t.add_done_callback(lambda done: self.tasks.pop(done, None))
        return len(jobs)

    async def finish(self, seconds: Optional[float] = None) -> None:
        """Waits for those in flight (at most `seconds`), then stops the others: they stay
        taken and go back in the queue after RECLAIM_AFTER."""
        if not self.tasks:
            return
        _, rest_ones = await asyncio.wait(list(self.tasks), timeout=seconds)
        for t in rest_ones:
            t.cancel()
        if rest_ones:
            await asyncio.gather(*rest_ones, return_exceptions=True)


async def deliver_due(bind: Engine, client: Optional[httpx.AsyncClient] = None, limit: int = BATCH) -> int:
    """One round only, waiting for it to end (for the tests and for whoever wants to send
    now). Returns how many deliveries it tried. It does not raise."""
    flight = _InFlight(bind, client)
    n = 0
    while n < limit:
        taken_ones = await flight.round_(limit - n)
        if not taken_ones:
            break
        n += taken_ones
        if n < limit:
            await flight.finish()
    await flight.finish()
    return n


async def delivery_loop(stop: asyncio.Event) -> None:
    from app.db.session import engine

    logger.info("webhook: sending started (every %ss)", CYCLE)
    flight = _InFlight(engine)
    still = asyncio.create_task(stop.wait())
    try:
        while not stop.is_set():
            try:
                await flight.round_()
            except Exception:  # noqa: BLE001 — sending must never stop the gateway
                logger.exception("webhook: sending round failed")
            # it starts again as soon as a delivery ends (its slot is free), or after CYCLE for
            # those that became due meanwhile, or at shutdown. Before, it always waited CYCLE:
            # with at most PER_WEBHOOK in flight per webhook, 300 deliveries towards a fast
            # receiver went one per second
            await asyncio.wait([still, *flight.tasks], timeout=CYCLE, return_when=asyncio.FIRST_COMPLETED)
    finally:
        still.cancel()
        await flight.finish(5)


def clean(session: Session) -> int:
    """The deliveries' log past WEBHOOKS__RETENTION_DAYS (the scheduler calls it)."""
    cutoff = now() - timedelta(days=get_settings().webhooks.retention_days)
    n = session.exec(
        delete(WebhookDelivery).where(WebhookDelivery.created_at < cutoff, WebhookDelivery.status.in_(("delivered", "failed")))
    ).rowcount or 0
    session.commit()
    return n
