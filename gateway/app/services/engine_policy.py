"""Which run engines the installation allows to CHOOSE.

A company standardizing the fleet (e.g. «here only ClickHouse is used») needs to say
it once only, not flow by flow. This module is the single source of the answer: the
flows' validation (routes/flows.py) and the catalog served to the selector
(routes/proxy.py) consult it, so they cannot diverge.

Rule: the FORBIDDEN engines are stored. An empty table = everything allowed, that is
the historical behaviour.
"""
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.engine_client import get_engine_client
from app.models import DisabledEngine

# Engines known to the gateway, in sync with the engine's catalog. They live here and
# not in routes/flows.py because two different routes need them: keeping them in one of
# the two would force the other to import from a routes module.
KNOWN_ENGINES: tuple[str, ...] = ("polars", "duckdb", "chdb", "clickhouse", "bigquery")


def disabled_engines(session: Session) -> set[str]:
    """The ids of the engines the administrator disabled (often the empty set)."""
    return {r.engine_id for r in session.exec(select(DisabledEngine)).all()}


def allowed_engines(session: Session) -> set[str]:
    """The engines that may still be chosen for a flow."""
    return set(KNOWN_ENGINES) - disabled_engines(session)


async def pick_engine(session: Session, requested: Optional[str]) -> Optional[str]:
    """The engine of a query (the assistant's, a SQL script's): one AVAILABLE on the engine
    and ALLOWED by the administrator. When the client asks for one that is not, the error says which
    may be chosen; when it does not ask, the first usable one is taken (Polars, if
    there)."""
    allowed = allowed_engines(session)
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
    return "polars" if "polars" in usable else usable[0]
