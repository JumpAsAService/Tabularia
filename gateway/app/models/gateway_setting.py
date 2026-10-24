"""The gateway's settings changed from Administration → Settings (app/core/overrides.py).

One row per setting CHANGED: with the table empty the environment's apply, as before. The
source: every gateway and orchestrator process reads it again every few seconds.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


class GatewaySetting(SQLModel, table=True):
    __tablename__ = "gateway_settings"

    # "<section>.<field>" of the gateway's settings, e.g. "orchestrator.max_concurrent"
    key: str = Field(primary_key=True, max_length=80)
    # JSON: a number, true/false or a text, as the setting holds it
    value: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: datetime
    # the e-mail of who changed it (it stays if the user goes; the audit has the rest)
    updated_by: Optional[str] = Field(default=None, max_length=320)


class SettingsRollout(SQLModel, table=True):
    """Per component (gateway, orchestrator, engine, worker, preview-worker, beat): when a
    setting it reads only at start last changed, and the rollout asked for it
    (services/settings_rollout.py). Its pods started before `changed_at` run the old value."""

    __tablename__ = "settings_rollouts"

    component: str = Field(primary_key=True, max_length=40)
    changed_at: Optional[datetime] = None
    # set by «Roll out»; cleared when no pod of the component started before it
    requested_at: Optional[datetime] = None
    requested_by: Optional[str] = Field(default=None, max_length=320)
