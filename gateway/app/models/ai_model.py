"""The AI models the administrator allows to use in the assistant.

The opposite rule from the engines': the ENABLED models are stored. An empty table =
no model = the assistant cannot be used. A model costs at every message and the
provider's catalog changes by itself: a new model must not become usable without
someone having decided it.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text
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


class AiPrompt(SQLModel, table=True):
    """The text of an AI agent's instructions as the administrators changed it
    (services/ai_prompts.py). One row per agent CHANGED: without it the default applies."""

    __tablename__ = "ai_prompts"

    # assistant | flow | script | title | dbt_describe | dbt_translate
    agent: str = Field(primary_key=True, max_length=40)
    text: str = Field(sa_column=Column(Text, nullable=False))
    updated_at: datetime = Field(default_factory=_now)
    # the e-mail of who changed it (it stays if the user goes; the audit has the rest)
    updated_by: Optional[str] = Field(default=None, max_length=320)
