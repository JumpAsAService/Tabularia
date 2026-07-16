from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class FlowVersion(SQLModel, table=True):
    """Uno snapshot della definizione di un flusso. Ogni salvataggio che cambia la
    definizione ne crea uno (auto-versione): così si può vedere lo storico e
    PROMUOVERE una versione vecchia (che diventa la nuova corrente)."""

    __tablename__ = "flow_versions"
    __table_args__ = (UniqueConstraint("flow_id", "version", name="uq_flowversion"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    flow_id: int = Field(foreign_key="flows.id", index=True)
    version: int  # progressivo per flusso (1, 2, 3, …); il massimo è la corrente
    definition: str = Field(sa_column=Column(Text, nullable=False))
    note: str = ""  # es. "creazione", "promossa dalla v2"
    created_at: datetime = Field(default_factory=_now)
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")
