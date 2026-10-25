from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc)


class DbtAiText(SQLModel, table=True):
    """What the AI wrote for a dbt export (a description, a translation), remembered per flow
    VERSION: exporting the same version again reuses it instead of asking again, and the
    export stays deterministic (the user's choice, 2026-10-10).

    `flow_id` without a foreign key: the texts are deleted with the flow (delete_flow),
    and an FK would turn a forgotten deletion into a 500."""

    __tablename__ = "dbt_ai_texts"
    __table_args__ = (UniqueConstraint("flow_id", "flow_version", "kind", "key", name="uq_dbt_ai_text"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    flow_id: int = Field(index=True)
    flow_version: int = 0                  # 0 = a flow without saved versions
    # describe | translate | claim (an export asking the AI right now: services/dbt_ai._prenotazione)
    kind: str
    # what it describes: flow:<id>@v<n>, col:ds:<id>:<column>:<type>, sql:<hash>; a longer key
    # than the column is stored as sha256:<digest> (services/dbt_ai._chiave)
    key: str = Field(max_length=500)
    text: str = Field(sa_column=Column(Text, nullable=False))
    model_id: str = ""
    created_at: datetime = Field(default_factory=_now)
