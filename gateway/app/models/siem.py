"""Forwarding the audit log to a SIEM (Administration → SIEM): Splunk for now, with the
HTTP Event Collector. One row only (id 1): the destination and how far the forwarding
has got (`cursor_id`, the id of the last item delivered). services/siem.py does the
work."""
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Column, Text
from sqlmodel import Field, SQLModel


class SiemDestination(SQLModel, table=True):
    __tablename__ = "siem_destinations"

    id: int = Field(default=1, primary_key=True)
    kind: str = Field(default="splunk_hec", max_length=32)
    enabled: bool = False
    url: str = Field(default="", max_length=2000)
    token_enc: Optional[str] = Field(default=None, sa_column=Column(Text))  # encrypted (Fernet), never returned
    index: Optional[str] = Field(default=None, max_length=120)  # empty = the token's default index
    source: Optional[str] = Field(default=None, max_length=120)
    sourcetype: Optional[str] = Field(default=None, max_length=120)
    host: Optional[str] = Field(default=None, max_length=255)
    verify_tls: bool = True
    # the last audit item delivered: it starts again from here, after a fault or a restart
    cursor_id: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, server_default="0"))
    sent_total: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, server_default="0"))
    last_ok_at: Optional[datetime] = None
    last_error: Optional[str] = Field(default=None, sa_column=Column(Text))
    last_error_at: Optional[datetime] = None
    failures: int = 0  # failed attempts in a row: they decide the wait before the next
    next_attempt_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = Field(default=None, max_length=320)
