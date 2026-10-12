"""Inoltro del registro di audit a un SIEM (Amministrazione → SIEM): per ora Splunk,
con l'HTTP Event Collector. Una riga sola (id 1): la destinazione e il punto a cui
è arrivato l'inoltro (`cursor_id`, l'id dell'ultima voce consegnata). Il lavoro lo
fa services/siem.py."""
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
    token_enc: Optional[str] = Field(default=None, sa_column=Column(Text))  # cifrato (Fernet), mai restituito
    index: Optional[str] = Field(default=None, max_length=120)  # vuoto = l'indice di default del token
    source: Optional[str] = Field(default=None, max_length=120)
    sourcetype: Optional[str] = Field(default=None, max_length=120)
    host: Optional[str] = Field(default=None, max_length=255)
    verify_tls: bool = True
    # l'ultima voce dell'audit consegnata: si riparte da qui, dopo un guasto o un riavvio
    cursor_id: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, server_default="0"))
    sent_total: int = Field(default=0, sa_column=Column(BigInteger, nullable=False, server_default="0"))
    last_ok_at: Optional[datetime] = None
    last_error: Optional[str] = Field(default=None, sa_column=Column(Text))
    last_error_at: Optional[datetime] = None
    failures: int = 0  # tentativi falliti di fila: decidono l'attesa prima del prossimo
    next_attempt_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = Field(default=None, max_length=320)
