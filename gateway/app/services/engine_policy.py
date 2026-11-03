"""Which run engines the installation allows to CHOOSE.

A company standardizing the fleet (e.g. «here only ClickHouse is used») needs to say
it once only, not flow by flow. This module is the single source of the answer: the
flows' validation (routes/flows.py) and the catalog served to the selector
(routes/proxy.py) consult it, so they cannot diverge.

Rule: the FORBIDDEN engines are stored. An empty table = everything allowed, that is
the historical behaviour.
"""
from typing import Callable, Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.engine_client import get_engine_client
from app.models import DisabledEngine

# Engines known to the gateway, in sync with the engine's catalog. They live here and
# not in routes/flows.py because two different routes need them: keeping them in one of
# the two would force the other to import from a routes module.
KNOWN_ENGINES: tuple[str, ...] = ("polars", "duckdb", "chdb", "clickhouse", "bigquery")

# The engine of what nobody chose one for — a new flow, the assistant's query, a SQL script — the
# first of these the administrator allows. DuckDB first: it spills to disk where Polars holds a
# group in memory (bug hunt 2026-11-03: «Margin by Category and Month» over 42 million rows took an
# 8 GiB worker down on Polars). The engine's own default is the same (backend app/engine DEFAULT_ENGINE).
PREFERRED_ENGINES: tuple[str, ...] = ("duckdb", "polars", "chdb", "clickhouse", "bigquery")
DEFAULT_ENGINE = PREFERRED_ENGINES[0]


def default_engine(allowed: Optional[set[str]] = None) -> str:
    """The first preferred engine among `allowed` (every one when None)."""
    return next((e for e in PREFERRED_ENGINES if allowed is None or e in allowed), DEFAULT_ENGINE)


def disabled_engines(session: Session) -> set[str]:
    """The ids of the engines the administrator disabled (often the empty set)."""
    return {r.engine_id for r in session.exec(select(DisabledEngine)).all()}


def allowed_engines(session: Session) -> set[str]:
    """The engines that may still be chosen for a flow."""
    return set(KNOWN_ENGINES) - disabled_engines(session)


async def pick_engine(session: Session, requested: Optional[str], before_asking: Optional[Callable[[], None]] = None) -> Optional[str]:
    """The engine of a query (the assistant's, a SQL script's): one AVAILABLE on the engine
    and ALLOWED by the administrator. When the client asks for one that is not, the error says which
    may be chosen; when it does not ask, the first usable one is taken (`default_engine`). `before_asking`: what to do between the policy read and the engine's answer, which is
    a wait: whoever has nothing pending hands the database connection back there."""
    allowed = allowed_engines(session)
    if before_asking is not None:
        before_asking()
    try:
        resp = await get_engine_client().get("/engines", timeout=15)
        resp.raise_for_status()
        usable = [e["id"] for e in resp.json() if e.get("available") and e.get("id") in allowed]
    except Exception:  # noqa: BLE001 — catalog not readable: trust the policy alone
        usable = sorted(allowed)
    if requested:
        if requested not in usable:
            raise HTTPException(status_code=422, detail=f"This engine cannot be used: {requested}. Available: {', '.join(usable) or 'none'}")
        return requested
    if not usable:
        raise HTTPException(status_code=422, detail="No engine is available")
    return default_engine(set(usable))
