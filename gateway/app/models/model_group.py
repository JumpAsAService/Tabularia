"""The groups of statistical models an administrator enabled (Administration → Statistical
models, docs/specs/statistical-models.md). Only the Go gateway reads and writes this table;
the model is here so that the migrated database and the models stay one and the same.
Times are naive UTC, as in the other tables of the gateway.

- `ModelGroup`: one row per group enabled once (its id is the catalogue's: regression,
  survival, timeseries). Its image is the managed scripting environment `environment_id`
  (`managed_by = model_group:<id>`); `enabled` false: disabled since, its nodes do not run.
  `everyone`: whoever can edit flows may use it; false: only the user groups listed in
  `script_environment_groups` for its environment. `max_rows`: NULL is the recipe's.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import BigInteger, Column, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class ModelGroup(SQLModel, table=True):
    __tablename__ = "model_groups"
    __table_args__ = (UniqueConstraint("environment_id", name="uq_model_group_environment"),)

    id: str = Field(primary_key=True, max_length=64)
    environment_id: Optional[int] = Field(default=None, foreign_key="script_environments.id")
    enabled: bool = Field(default=False, sa_column_kwargs={"server_default": "false"})
    everyone: bool = Field(default=True, sa_column_kwargs={"server_default": "true"})
    max_rows: Optional[int] = Field(default=None, sa_column=Column(BigInteger))
    enabled_by: Optional[str] = Field(default=None, max_length=320)
    enabled_at: Optional[datetime] = None
    updated_by: Optional[str] = Field(default=None, max_length=320)
    updated_at: datetime = Field(default_factory=_now)
