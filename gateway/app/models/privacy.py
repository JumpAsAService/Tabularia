"""Informativa sulla privacy, scritta dall'amministratore dell'installazione.

Riga SINGOLA (id=1) e non un elenco: è un documento che si modifica, non una
lista a cui si aggiunge. Chi installa Tabularia decide che cosa dire, perché è
lui il titolare del trattamento — noi mettiamo il posto dove dirlo e un testo di
partenza che descrive quello che il prodotto raccoglie davvero.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class PrivacyNotice(SQLModel, table=True):
    __tablename__ = "privacy_notice"

    id: Optional[int] = Field(default=1, primary_key=True)
    enabled: bool = True
    # la riga che compare nella barra: deve stare su una riga sola
    summary: str = Field(default="", sa_column=Column(Text, nullable=False))
    # il testo intero, che si apre da «Dettagli»
    body: str = Field(default="", sa_column=Column(Text, nullable=False))
    # collegamento facoltativo all'informativa completa dell'organizzazione
    url: str = ""
    updated_at: datetime = Field(default_factory=_now)
    updated_by: Optional[int] = Field(default=None, foreign_key="users.id")
