"""Engine settings set from the panel (Administration → Settings).

One row per CHANGED parameter: with an empty table the environment's values hold, as
before. It is the true source: the gateway sends them to the engine, which keeps them
in Valkey for its processes, and sends them again at intervals (see
services/engine_panel_settings.py). Which parameters and with what limits the engine
decides (backend/app/core/panel_settings.py).
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Column, Text
from sqlmodel import Field, SQLModel


class EngineSetting(SQLModel, table=True):
    __tablename__ = "engine_settings"

    # "<section>.<field>" of the engine settings, e.g. "ingest.parquet_row_group_rows"
    key: str = Field(primary_key=True, max_length=80)
    # big integers: BigQuery's bytes cap reaches tens of TiB
    value: int = Field(sa_column=Column(BigInteger, nullable=False))
    # the value as JSON, for the settings that are not whole numbers (a decimal, true/false):
    # `value` keeps an integer for the previous version of the gateway during an upgrade
    value_json: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    updated_at: datetime
    # the email of whoever changed it (it stays even when the user is removed; the audit has the rest)
    updated_by: Optional[str] = Field(default=None, max_length=320)
