"""Banner di avvertimento mostrati nell'Explore a TUTTI gli utenti.

Li gestisce un amministratore dalla pagina Admin: inserimento ed eliminazione.
Non c'è un interruttore attivo/sospeso di proposito — un banner esiste finché
non lo si toglie, così «cosa c'è in elenco» è esattamente «cosa vedono gli
utenti», senza stati intermedi da ricordare.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Banner(SQLModel, table=True):
    __tablename__ = "banners"

    id: Optional[int] = Field(default=None, primary_key=True)
    message: str = Field(sa_column=Column(Text, nullable=False))
    # info | warning | danger — decide solo icona e colore nell'interfaccia,
    # non il comportamento: un banner è sempre visibile a tutti.
    level: str = Field(default="warning", index=True)
    created_at: datetime = Field(default_factory=_now)
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
