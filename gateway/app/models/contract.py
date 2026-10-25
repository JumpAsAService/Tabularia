"""Data contracts: what whoever produces a datasource promises to whoever consumes it.

One contract per datasource. No foreign keys towards `datasources`: the rows are
removed by hand when the datasource goes (`services.contracts.forget`), because in the
tests SQLite does not enforce them and a constraint forgotten in one of the ways that
delete a datasource would break only in production.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Index, Text, UniqueConstraint, text
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DataContract(SQLModel, table=True):
    __tablename__ = "data_contracts"
    __table_args__ = (UniqueConstraint("datasource_id", name="uq_contract_datasource"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    datasource_id: int = Field(index=True)
    version: int = 1
    document: str = Field(sa_column=Column(Text, nullable=False))  # JSON {description, rules: [...]}
    enabled: bool = True

    # The state on the data the datasource serves NOW: pending (never evaluated on this
    # data) | passed | warning | failed.
    status: str = "pending"
    checked_at: Optional[datetime] = None
    errors: int = 0
    warnings: int = 0
    # the last report, whole (JSON): what is shown opening the contract, and the basis on
    # which freshness is recomputed as time goes by
    last_report: Optional[str] = Field(default=None, sa_column=Column("last_report", Text))
    # An update was REFUSED by a blocking rule: the datasource still serves the previous
    # snapshot. It stays set until an update succeeds: the data is good but no longer the
    # current data.
    blocked_at: Optional[datetime] = None
    blocked_run_id: Optional[int] = None

    # Whom to notify when the state changes: addresses separated by commas and the SMTP
    # connection to send with (as for the flows' failure notice).
    notify_emails: Optional[str] = None
    notify_connection_id: Optional[int] = None

    created_by: Optional[int] = None
    updated_by: Optional[int] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class DataContractVersion(SQLModel, table=True):
    """Every save of the contract: who, when, and the document as it was."""
    __tablename__ = "data_contract_versions"
    __table_args__ = (UniqueConstraint("datasource_id", "version", name="uq_contract_version"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    datasource_id: int = Field(index=True)
    version: int
    document: str = Field(sa_column=Column(Text, nullable=False))
    created_by: Optional[int] = None
    created_at: datetime = Field(default_factory=_now)


class DataContractResult(SQLModel, table=True):
    """An evaluation of the contract: the history."""
    __tablename__ = "data_contract_results"
    __table_args__ = (
        # the queue of the notices to send: only the waiting rows
        Index("ix_contract_results_to_notify", "id",
              postgresql_where=text("notify = 'pending'"), sqlite_where=text("notify = 'pending'")),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    datasource_id: int = Field(index=True)
    contract_version: int
    # what started it: refresh | publish (new data arriving), save | manual (on the current
    # data), freshness (time passing)
    trigger: str
    run_id: Optional[int] = None
    snapshot_key: str = ""
    outcome: str  # passed | warning | failed
    blocked: bool = False  # the new data was NOT published
    errors: int = 0
    warnings: int = 0
    rows: Optional[int] = None
    report: str = Field(sa_column=Column(Text, nullable=False))  # JSON
    # The notice to send for this evaluation: None = none (the state did not change, or
    # nobody asked to be notified) | pending → sending → sent | failed | skipped. Whoever
    # records the report writes it, the scheduler consumes it
    # (services.contract_notifier): no email inside the transaction that publishes the
    # data.
    notify: Optional[str] = None
    evaluated_at: datetime = Field(default_factory=_now, index=True)
