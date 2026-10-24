"""The automatic backup of this database (Administration → Backups).

On its schedule (BACKUP__SCHEDULE, in APP__TIMEZONE; the administrators can change it, and
whether it runs, and how many backups stay) an orchestrator runs `pg_dump` of Tabularia's
own database and streams what it writes to the engine, which stores it in the object
storage: the engine holds the storage credentials, the gateway does not. Nothing is
written on the orchestrator's disk; a pg_dump that fails halfway breaks the upload, and
the engine stores nothing. Where:
BACKUP__BUCKET / BACKUP__PREFIX, by default Tabularia's bucket under backups/postgres/ —
a bucket of its own is advised in production, so that losing one does not lose both.
Then the oldest beyond the number to keep are removed.

One backup at a time across the orchestrators: the one that moves the row's `next_at`
(or takes the request of «Back up now») makes it; the others find nothing due. The one at
work writes a heartbeat (`alive_at`) every `BATTITO` seconds; a claim without one for
`VIVO` seconds is ignored: a process that died while backing up, or a database restored
from a backup (the dump was taken while its own backup was marked running: without the
heartbeat the restored claim blocked the backups for the longest a backup may take).

The file is PostgreSQL's custom format: `pg_restore` reads it (docs/deploy/upgrading.md).
It holds the users' e-mail addresses and the connections' passwords (encrypted with the
Fernet key): the bucket must stay private, as it must for the data.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import time
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Optional

import httpx
from sqlalchemy import func, or_, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.models import DatabaseBackup
from app.services.schedule import next_fire

logger = logging.getLogger(__name__)

TICK_SECONDS = 15
NAME = re.compile(r"^tabularia-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z\.dump$")
CHUNK = 1024 * 1024
# who made a scheduled backup, in the audit log
SCHEDULE_ACTOR = "backup schedule"


class BackupFailed(RuntimeError):
    """A backup that did not make it, with the reason to show."""


def _adesso() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive UTC columns


def disabled_reason(session: Session) -> Optional[str]:
    """Why backups cannot run here: "kubernetes" off it (Docker Compose: the PostgreSQL
    of the compose file is backed up with its own tools, and the panel shows the section
    greyed out, as the other features of a cluster), "postgres" (another database:
    development, tests), "pg_dump" (the program is not in this image)."""
    from app.core.config import on_kubernetes

    if not on_kubernetes():
        return "kubernetes"
    if session.get_bind().dialect.name != "postgresql":
        return "postgres"
    if shutil.which(get_settings().backup.pg_dump) is None:
        return "pg_dump"
    return None


def row(session: Session) -> DatabaseBackup:
    r = session.get(DatabaseBackup, 1)
    if r is not None:
        return r
    try:
        session.add(DatabaseBackup(id=1))
        session.commit()
    except IntegrityError:  # another process made it first
        session.rollback()
    return session.get(DatabaseBackup, 1)


def effective(r: DatabaseBackup) -> dict[str, Any]:
    """What is in force: the administrators' choice, or the installation's (BACKUP__*)."""
    b = get_settings().backup
    return {
        "enabled": b.enabled if r.enabled is None else r.enabled,
        "schedule": r.schedule or b.schedule,
        "keep": r.keep or b.keep,
        "defaults": {"enabled": b.enabled, "schedule": b.schedule, "keep": b.keep},
        "changed": {"enabled": r.enabled is not None, "schedule": r.schedule is not None, "keep": r.keep is not None},
    }


def _next(schedule: str, after: datetime) -> datetime:
    return next_fire(schedule, after).replace(tzinfo=None)


def replan(session: Session, ora: Optional[datetime] = None) -> None:
    """After a change of the settings: the next backup from the schedule now in force,
    so that the page shows it at once (the orchestrators would see it within seconds)."""
    ora = ora or _adesso()
    r = row(session)
    eff = effective(r)
    nxt, per = (_next(eff["schedule"], ora), eff["schedule"]) if eff["enabled"] else (None, None)
    session.exec(update(DatabaseBackup).where(DatabaseBackup.id == 1).values(next_at=nxt, next_for=per))
    session.commit()
    session.refresh(r)


def claim(session: Session, ora: Optional[datetime] = None) -> Optional[dict[str, Any]]:
    """Plans the next backup and takes it when due. Returns what to make (the number to
    keep, who asked) when this process won it, None otherwise."""
    ora = ora or _adesso()
    r = row(session)
    eff = effective(r)
    if eff["enabled"] and (r.next_at is None or r.next_for != eff["schedule"]):
        nxt = _next(eff["schedule"], ora)
        session.exec(update(DatabaseBackup).where(DatabaseBackup.id == 1).values(next_at=nxt, next_for=eff["schedule"]))
        session.commit()
        session.refresh(r)
    elif not eff["enabled"] and r.next_at is not None:
        session.exec(update(DatabaseBackup).where(DatabaseBackup.id == 1).values(next_at=None, next_for=None))
        session.commit()
        session.refresh(r)
    due = r.requested_at is not None or (eff["enabled"] and r.next_at is not None and r.next_at <= ora)
    if not due:
        return None
    who = r.requested_by if r.requested_at is not None else None
    from app.services.orchestrator import ISTANZA

    taken = session.exec(
        update(DatabaseBackup)
        .where(DatabaseBackup.id == 1, ferma(ora),
               or_(DatabaseBackup.requested_at.is_not(None), DatabaseBackup.next_at <= ora))  # type: ignore[union-attr]
        .values(running_since=ora, alive_at=ora, claimed_by=ISTANZA, requested_at=None, requested_by=None,
                next_at=_next(eff["schedule"], ora) if eff["enabled"] else None,
                next_for=eff["schedule"] if eff["enabled"] else None)
    ).rowcount
    session.commit()
    return {"keep": eff["keep"], "who": who} if taken == 1 else None


BATTITO = 30.0  # seconds between two heartbeats of the running backup
VIVO = 120.0  # seconds without a heartbeat after which a running backup is taken for dead


def ferma(ora: Optional[datetime] = None):
    """The condition of a row with no live backup running."""
    ora = ora or _adesso()
    morto = ora - timedelta(seconds=VIVO)
    return or_(DatabaseBackup.running_since.is_(None),  # type: ignore[union-attr]
               func.coalesce(DatabaseBackup.alive_at, DatabaseBackup.running_since) < morto)


def in_corso(r: DatabaseBackup, ora: Optional[datetime] = None) -> bool:
    if r.running_since is None:
        return False
    return (r.alive_at or r.running_since) >= (ora or _adesso()) - timedelta(seconds=VIVO)


# where the image keeps pg_dump of each PostgreSQL version (gateway/Dockerfile)
VERSIONS_DIR = "/usr/lib/postgresql"


def _server_major() -> Optional[int]:
    from sqlalchemy import text

    from app.db.session import engine

    try:
        with engine.connect() as conn:
            return int(conn.execute(text("SHOW server_version_num")).scalar()) // 10000
    except Exception:  # noqa: BLE001 — pg_dump will say what is wrong with the connection
        return None


def pg_dump_for(major: Optional[int]) -> str:
    """pg_dump of the server's own version when the image has it: its file is then read by
    the server's own pg_restore (a newer pg_dump writes a format an older pg_restore
    refuses). Otherwise BACKUP__PG_DUMP (18, which reads older servers too)."""
    if major is not None:
        own = os.path.join(VERSIONS_DIR, str(major), "bin", "pg_dump")
        if os.access(own, os.X_OK):
            return own
    return get_settings().backup.pg_dump


def _pg_env_and_args(major: Optional[int] = None) -> tuple[dict[str, str], list[str]]:
    """pg_dump's command line for this database; the password goes in the environment,
    never on the command line."""
    url = make_url(get_settings().db.dsn)
    env = {**os.environ, "PGPASSWORD": url.password or "", "PGCONNECT_TIMEOUT": "10"}
    for key, value in url.query.items():  # sslmode, sslrootcert… as libpq reads them
        if key.startswith("ssl") and isinstance(value, str):
            env["PG" + key.upper()] = value
    args = [pg_dump_for(major), "--format=custom",
            "--host", url.host or "localhost", "--port", str(url.port or 5432),
            "--username", url.username or "", url.database or ""]
    return env, args


async def _pg_dump() -> AsyncIterator[bytes]:
    """What pg_dump writes, as it writes it; a failure raises at the end, after the last
    chunk, so that the upload it feeds breaks instead of completing."""
    env, args = _pg_env_and_args(await asyncio.to_thread(_server_major))
    proc = await asyncio.create_subprocess_exec(*args, env=env, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE)
    errors = asyncio.ensure_future(proc.stderr.read())  # read alongside: a full pipe would stall it
    try:
        while chunk := await proc.stdout.read(CHUNK):
            yield chunk
        await proc.wait()
    finally:
        if proc.returncode is None:  # the upload stopped first (a timeout, the engine)
            proc.kill()
            await proc.wait()
    err = await errors
    if proc.returncode != 0:
        raise BackupFailed(f"pg_dump failed: {(err or b'').decode(errors='replace').strip()[-800:]}")


def _engine_error(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail") or resp.text)[:800]
    except ValueError:
        return resp.text[:800]


async def _upload(name: str, chunks: AsyncIterator[bytes]) -> int:
    """Streams the dump to the engine. Returns its size."""
    size = 0

    async def counted() -> AsyncIterator[bytes]:
        nonlocal size
        async for chunk in chunks:
            size += len(chunk)
            yield chunk

    limit = get_settings().backup.timeout_seconds
    try:
        resp = await asyncio.wait_for(get_engine_client().put(
            f"/backups/objects/{name}", content=counted(), headers={"Content-Type": "application/octet-stream"},
            timeout=httpx.Timeout(limit, connect=10.0)), timeout=limit)
    except asyncio.TimeoutError:
        raise BackupFailed(f"the backup did not end within {limit} s (BACKUP__TIMEOUT_SECONDS)")
    except httpx.HTTPError as e:
        raise BackupFailed(f"the engine could not be reached to store the backup ({type(e).__name__})")
    if resp.status_code >= 400:
        raise BackupFailed(_engine_error(resp))
    return size


async def listing() -> dict[str, Any]:
    """Where the backups go and the ones there (the engine's GET /backups)."""
    try:
        resp = await get_engine_client().get("/backups", timeout=httpx.Timeout(30.0, connect=5.0))
    except httpx.HTTPError as e:
        return {"bucket": None, "prefix": None, "same_as_data": None, "items": [],
                "error": f"The engine could not be reached ({type(e).__name__})"}
    if resp.status_code >= 400:
        return {"bucket": None, "prefix": None, "same_as_data": None, "items": [], "error": _engine_error(resp)}
    return resp.json()


async def _rotate(keep: int) -> int:
    """Removes the oldest backups beyond `keep`. Returns how many went."""
    found = await listing()
    if found.get("error"):
        raise BackupFailed(f"the backup was stored, but the old ones could not be listed: {found['error']}")
    old = [i["name"] for i in found.get("items") or [] if NAME.match(i.get("name") or "")][keep:]
    for name in old:
        resp = await get_engine_client().delete(f"/backups/objects/{name}", timeout=httpx.Timeout(30.0, connect=5.0))
        if resp.status_code >= 400:
            raise BackupFailed(f"the backup was stored, but {name} could not be removed: {_engine_error(resp)}")
    return len(old)


async def _batti() -> None:
    """While this process backs up: it is alive (`alive_at`), every BATTITO seconds."""
    from app.db.session import engine
    from app.services.orchestrator import ISTANZA

    def uno() -> None:
        with Session(engine) as session:
            session.exec(update(DatabaseBackup).where(DatabaseBackup.id == 1, DatabaseBackup.claimed_by == ISTANZA)
                         .values(alive_at=_adesso()))
            session.commit()

    while True:
        await asyncio.sleep(BATTITO)
        try:
            await asyncio.to_thread(uno)
        except Exception as e:  # noqa: BLE001 — the database not answering: the backup fails on its own
            logger.info("backup: heartbeat not written (%s)", e)


def _close(**values) -> None:
    from app.db.session import engine

    with Session(engine) as session:
        session.exec(update(DatabaseBackup).where(DatabaseBackup.id == 1)
                     .values(running_since=None, alive_at=None, claimed_by=None, **values))
        session.commit()


def _audit(outcome: str, who: Optional[str], detail: dict) -> None:
    from app.db.session import engine
    from app.services import audit

    with Session(engine) as session:
        audit.record_audit(session, actor_label=who or SCHEDULE_ACTOR, action=audit.BACKUP_RUN, outcome=outcome,
                           target_type="database_backup", target_label=detail.get("name"), detail=detail)


async def make(keep: int, who: Optional[str] = None) -> dict[str, Any]:
    """One backup: dump, store, rotate; the outcome written on the row and in the audit log."""
    started = time.monotonic()
    name = datetime.now(timezone.utc).strftime("tabularia-%Y-%m-%dT%H-%M-%SZ.dump")
    battito = asyncio.create_task(_batti())
    try:
        size = await _upload(name, _pg_dump())
        removed = await _rotate(keep)
    except Exception as e:  # noqa: BLE001 — every failure is written down, none stops the loop
        battito.cancel()
        reason = str(e) if isinstance(e, BackupFailed) else f"{type(e).__name__}: {str(e)[:500]}"
        logger.warning("backup: failed (%s)", reason)
        _close(last_error=reason[:2000], last_error_at=_adesso())
        _audit("failure", who, {"name": name, "error": reason[:500]})
        return {"ok": False, "error": reason}
    except asyncio.CancelledError:
        # the orchestrator stopping (a drain, an upgrade at 03:00): said, not lost in silence,
        # as the next scheduled time has already moved on to tomorrow
        battito.cancel()
        reason = "Stopped before the end: the process running it stopped (a restart or an upgrade). Run it now"
        logger.warning("backup: %s", reason)
        try:
            _close(last_error=reason, last_error_at=_adesso())
            _audit("failure", who, {"name": name, "error": reason})
        except Exception:  # noqa: BLE001 — stopping anyway
            logger.exception("backup: the interruption not recorded")
        raise
    battito.cancel()
    seconds = round(time.monotonic() - started, 1)
    logger.info("backup: %s stored (%s bytes, %s s), %s old removed", name, size, seconds, removed)
    _close(last_ok_at=_adesso(), last_name=name, last_size=size, last_seconds=seconds)
    _audit("success", who, {"name": name, "size": size, "removed": removed})
    return {"ok": True, "name": name, "size": size, "removed": removed}


async def un_giro() -> Optional[dict[str, Any]]:
    from app.db.session import engine

    with Session(engine) as session:
        if disabled_reason(session):
            return None
        todo = claim(session)
    if todo is None:
        return None
    return await make(todo["keep"], todo["who"])


async def backup_loop(stop: asyncio.Event) -> None:
    from app.db.session import engine

    with Session(engine) as session:
        reason = disabled_reason(session)
    if reason:
        logger.info("backup: not available here (%s)", {
            "kubernetes": "not on Kubernetes", "postgres": "the database is not PostgreSQL"}.get(
            reason, f"{get_settings().backup.pg_dump} is not installed"))
        return
    while not stop.is_set():
        try:
            await un_giro()
        except Exception:  # noqa: BLE001 — the backups must never stop the gateway
            logger.exception("backup: a round failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=TICK_SECONDS)
        except asyncio.TimeoutError:
            pass
