"""The AI models the administrator allows to use in the assistant.

The opposite rule from the engines': the ENABLED models are stored. An empty table =
no model = the assistant cannot be used. A model costs at every message and the
provider's catalog changes by itself: a new model must not become usable without
someone having decided it.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AiModel(SQLModel, table=True):
    __tablename__ = "ai_models"

    # the model's id at the provider ("gpt-oss-120b", …) IS the key
    model_id: str = Field(primary_key=True)
    enabled: bool = True
    updated_at: datetime = Field(default_factory=_now)
    updated_by: Optional[int] = Field(default=None, foreign_key="users.id")
