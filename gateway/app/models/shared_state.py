"""Short-lived state several gateway processes must see the same.

It sat in in-memory dictionaries while the gateway was one process. With several
replicas each would have its own: five wrong sign-in attempts per replica instead of
in all, and two people on the same flow not seeing each other because they talk to
different replicas. It sits on Postgres because it is the store the gateway has
already: Valkey is the engine's, and in production the gateway does not even get its
password. The scheduler removes the expired rows.
"""
from datetime import datetime
from typing import Optional

from sqlmodel import Field, SQLModel


class LoginAttempt(SQLModel, table=True):
    """Failed sign-in attempts in a row, by (address, email)."""
    __tablename__ = "login_attempts"
    ip: str = Field(primary_key=True)
    email: str = Field(primary_key=True)
    failures: int = 0
    last_at: datetime  # naive UTC, as the other columns


class FlowPresence(SQLModel, table=True):
    """An editor tab open on a flow. No foreign key: it is a row that lives forty seconds,
    and must not prevent deleting a flow."""
    __tablename__ = "flow_presence"
    flow_id: int = Field(primary_key=True)
    instance: str = Field(primary_key=True)
    user_id: int
    email: str
    full_name: str = ""
    since: float  # seconds since the epoch: when it opened, and the last heartbeat
    seen: float = Field(index=True)
