"""Webhooks: Tabularia calls an external address when a run ends.

`Webhook` is the subscription (where, on which events, for which folder);
`WebhookDelivery` is a delivery: it is born in the same transaction that closes the
run (it is not lost when the process dies right after) and a loop of the orchestrators
sends it, with new attempts at growing intervals. See services/webhooks.py.
"""
from datetime import datetime
from typing import Optional

from sqlalchemy import Column, Index, Text, text
from sqlmodel import Field, SQLModel


class Webhook(SQLModel, table=True):
    __tablename__ = "webhooks"
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=120)
    url: str = Field(max_length=2000)
    # the secret every delivery is signed with: encrypted (Fernet) because it is needed in
    # clear at every signature; it is shown only at creation and regeneration
    secret_enc: str = Field(sa_column=Column(Text, nullable=False))
    # the events it subscribes to, JSON (a list of names, see webhooks.EVENTI)
    events: str = Field(sa_column=Column(Text, nullable=False))
    # only the events of this folder and its subfolders; none = all
    project_id: Optional[int] = Field(default=None, foreign_key="projects.id")
    active: bool = True
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
    created_at: datetime


class WebhookDelivery(SQLModel, table=True):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # the queue: the ones to send, in order of when they are due
        Index(
            "ix_webhook_deliveries_due", "next_attempt_at",
            postgresql_where=text("status = 'pending'"), sqlite_where=text("status = 'pending'"),
        ),
    )
    id: Optional[int] = Field(default=None, primary_key=True)
    # without a foreign key, on purpose: the delivery is born in the transaction that closes
    # a run, and a webhook removed in that instant would fail the run's CLOSING. A delivery
    # of a webhook no longer there closes by itself («tolto»).
    webhook_id: int = Field(index=True)
    event: str = Field(max_length=64)
    # the exact body that is sent (and signed): the same at every attempt
    payload: str = Field(sa_column=Column(Text, nullable=False))
    status: str = Field(default="pending", max_length=16)  # pending | sending | delivered | failed
    attempts: int = 0
    next_attempt_at: datetime
    created_at: datetime = Field(index=True)
    delivered_at: Optional[datetime] = None
    last_status_code: Optional[int] = None
    last_error: Optional[str] = Field(default=None, max_length=500)
    # when a process took it to send it: if it dies halfway, past a time the delivery goes
    # back in the queue (at least once, never zero)
    claimed_at: Optional[datetime] = None
