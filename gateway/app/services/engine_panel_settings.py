"""The engine's settings from the panel: where they live and how they reach the engine.

The gateway keeps them (the `engine_settings` table, with the audit of every change)
and sends them to the engine (`PUT /settings`), which checks them and keeps them in
Valkey for all its processes (backend/app/core/panel_settings.py: which parameters,
with what limits, and why only those). `push_loop` sends them again every
`PUSH_EVERY` seconds: if Valkey empties or the engine restarts, they come back by
themselves.

Whoever saves and the periodic round take a lock in the database (`lock`): reading,
sending and saving go in line. Without it, two administrators saving different keys
together found both in the database, but the engine applied only one (each sent the
set read before the other's) until the next round — found trying on the prodtest, 7
pairs in 8.

One constraint only the gateway knows: it declares a run lost after
ENGINE__RUN_STALE_TIMEOUT_SECONDS, and the tasks' hard limit must stay under it
(otherwise it would close as failed a run still alive). It passes it to the engine at
every send (`hard_limit_max`). Raising the limit beyond is done in the environment,
raising that threshold too.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

import httpx
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.engine_client import CHECK, get_engine_client
from app.models import EngineSetting

logger = logging.getLogger(__name__)

PUSH_EVERY = 60.0
# the lock's key (pg_advisory_xact_lock): any number, the same for everyone
LOCK = 7_402_118
# the same margin as ENGINE__RUN_STALE_TIMEOUT_SECONDS's default (hard limit + 5 min)
MARGIN = 300


async def lock(session: Session, wait: float = 10.0, key: int = LOCK) -> bool:
    """The settings' lock, until the end of `session`'s transaction; it holds across all the
    replicas (it is in the database). It is asked for without blocking the event loop:
    whoever holds it is talking to the engine, and a slow engine would hold up the whole
    process. False when it does not come within `wait` seconds."""
    if session.get_bind().dialect.name != "postgresql":
        return True  # the tests' SQLite: one process only, nothing to line up
    end = time.monotonic() + wait
    while True:
        if session.exec(text("SELECT pg_try_advisory_xact_lock(:k)").bindparams(k=key)).one()[0]:
            return True
        if time.monotonic() >= end:
            return False
        await asyncio.sleep(0.1)


def hard_limit_max() -> int:
    return get_settings().engine.run_stale_timeout_seconds - MARGIN


def from_panel() -> bool:
    return get_settings().engine.settings_panel


def saved(session: Session) -> dict[str, EngineSetting]:
    return {r.key: r for r in session.exec(select(EngineSetting)).all()}


# set from the gateway's own settings, sent to the engine with its ones: the engine's
# limit on uploads is the second barrier after the gateway's (routes/gateway_settings.py)
FROM_GATEWAY = ("upload.max_bytes",)


def value(row: EngineSetting) -> Any:
    """The saved value: the JSON one (a decimal, true/false), else the integer."""
    if row.value_json is not None:
        try:
            v = json.loads(row.value_json)
        except ValueError:
            return None
        # the previous version of the gateway (a rolling update) writes `value` only and
        # leaves the JSON as it was: when they disagree, its newer integer wins
        if isinstance(v, (int, float)) and not isinstance(v, bool) and row.value != int(round(v)):
            return row.value
        return v
    return row.value


def columns(v: Any) -> tuple[int, Optional[str]]:
    """(value, value_json) of a row: an integer in `value` for the previous version of
    the gateway during an upgrade; the rest in JSON."""
    if isinstance(v, int) and not isinstance(v, bool):
        return v, None
    return int(round(v)) if isinstance(v, float) else int(bool(v)), json.dumps(v)


def from_gateway() -> dict[str, Any]:
    """The gateway's values the engine applies too, from the table rather than from this
    process's copy (up to ten seconds old: a round could send back the previous one)."""
    from app.core import overrides

    try:
        from app.core.config import settings_from_env
        from app.db.session import engine

        current_ones, _ = overrides.check(overrides.read_rows(engine), settings_from_env())
    except Exception:  # noqa: BLE001 — the database not answering: this process's copy
        current_ones = overrides.current()
    return {k: current_ones[k] for k in FROM_GATEWAY if k in current_ones}


def in_force(session: Session) -> dict[str, Any]:
    """The ones to send to the engine: none when the panel is turned off by the environment."""
    if not from_panel():
        return {}
    return {k: value(r) for k, r in saved(session).items() if k not in FROM_GATEWAY}


async def catalog() -> dict:
    """The engine's catalog: the items, their limits, the environment's values and those it
    is applying now."""
    r = await get_engine_client().get("/settings", params={"max_task_time_limit": hard_limit_max()}, timeout=CHECK)
    r.raise_for_status()
    return r.json()


async def push(values: dict[str, Any], changed: Optional[list[str]] = None,
                 with_gateway: bool = True) -> httpx.Response:
    values = {**{k: v for k, v in values.items() if k not in FROM_GATEWAY},
              **(from_gateway() if with_gateway and from_panel() else {})}
    return await get_engine_client().put(
        "/settings", json={"values": values, "max_task_time_limit": hard_limit_max(), "changed": changed or []},
        timeout=CHECK,
    )


async def push_discarding(values: dict[str, Any]) -> dict[str, Any]:
    """Sends; when the engine refuses some (saved when they held, they no longer do: the
    environment or the gateway's threshold changed) it removes them and sends again,
    until the engine accepts, instead of leaving the old ones in force or sending
    nothing. Returns the ones the engine applies; the discarded stay saved (the panel
    shows them «not applied») and end up in the log."""
    current_ones = dict(values)
    with_gateway = True
    for _ in range(len(values) + 2):
        r = await push(current_ones, with_gateway=with_gateway)
        if r.status_code != 422:
            r.raise_for_status()
            return current_ones
        errors = (r.json().get("detail") or {}).get("errors") or []
        bad_ones = {e.get("key") for e in errors} & set(current_ones)
        if with_gateway and {e.get("key") for e in errors} & set(FROM_GATEWAY):
            # an engine older than the upload limit from the panel: without it
            logger.warning("engine settings: the engine refuses %s: sent without them", errors)
            with_gateway = False
            continue
        if not bad_ones:  # the engine refuses, but none of what is there: no insisting
            r.raise_for_status()
        logger.warning("engine settings: discarded because they no longer hold: %s", errors)
        current_ones = {k: v for k, v in current_ones.items() if k not in bad_ones}
    raise RuntimeError("engine settings: the engine keeps refusing them")


async def one_round(bind: Engine) -> bool:
    """Sends the saved ones again. When someone is saving, it skips: they send them."""
    with Session(bind) as session:
        if not await lock(session, wait=0):
            return False
        await push_discarding(in_force(session))
        session.commit()  # releases the lock
    return True


async def push_loop(stop: asyncio.Event) -> None:
    from app.db.session import engine

    while not stop.is_set():
        try:
            await one_round(engine)
        except Exception as e:  # noqa: BLE001 — engine down or restarting: retried at the next round
            logger.info("engine settings: not sent (%s), retrying in %ss", e, PUSH_EVERY)
        try:
            await asyncio.wait_for(stop.wait(), timeout=PUSH_EVERY)
        except asyncio.TimeoutError:
            pass
