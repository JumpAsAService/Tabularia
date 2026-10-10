from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DbtAiText(SQLModel, table=True):
    """Quello che l'AI ha scritto per un export dbt (una descrizione, una traduzione),
    ricordato per VERSIONE del flusso: riesportare la stessa versione lo riusa invece
    di richiederlo, e l'export resta deterministico (scelta dell'utente, 2026-10-10).

    `flow_id` senza chiave esterna: i testi si cancellano col flusso (delete_flow),
    e una FK trasformerebbe una cancellazione dimenticata in un 500."""

    __tablename__ = "dbt_ai_texts"
    __table_args__ = (UniqueConstraint("flow_id", "flow_version", "kind", "key", name="uq_dbt_ai_text"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    flow_id: int = Field(index=True)
    flow_version: int = 0                  # 0 = flusso senza versioni salvate
    kind: str                              # describe | translate
    key: str = Field(max_length=500)       # cosa descrive: flow:<id>@v<n>, col:ds:<id>:<colonna>:<tipo>, sql:<hash>
    text: str = Field(sa_column=Column(Text, nullable=False))
    model_id: str = ""
    created_at: datetime = Field(default_factory=_now)
