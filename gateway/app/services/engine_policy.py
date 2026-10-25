"""Which run engines the installation allows to CHOOSE.

A company standardizing the fleet (e.g. «here only ClickHouse is used») needs to say
it once only, not flow by flow. This module is the single source of the answer: the
flows' validation (routes/flows.py) and the catalog served to the selector
(routes/proxy.py) consult it, so they cannot diverge.

Rule: the FORBIDDEN engines are stored. An empty table = everything allowed, that is
the historical behaviour.
"""
from sqlmodel import Session, select

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
