"""The engines the administrator DISABLED for the installation.

What is forbidden is recorded, not what is allowed: with an empty table everything
works exactly as before, so updating an existing installation changes nobody's
behaviour. It is the same idea as the banners — «what is in the list» is exactly «what
changes», without intermediate states to remember.

Disabling an engine does NOT stop the flows that use it already: it only forbids
choosing it from here on. The choice is deliberate — a switch in the admin panel must
not be able to stop a scheduled DAG the moment it is pressed. The flows left behind
show in the panel, and are migrated by hand (see routes/engine_policy.py).
"""
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DisabledEngine(SQLModel, table=True):
    __tablename__ = "disabled_engines"

    # the engine's id in the catalog ("polars", "duckdb", …) IS the key: no synthetic id is
    # needed, and the primary key guarantees uniqueness instead of a separate constraint.
    engine_id: str = Field(primary_key=True)
    disabled_at: datetime = Field(default_factory=_now)
    disabled_by: Optional[int] = Field(default=None, foreign_key="users.id")
