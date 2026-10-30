"""The AI models the administrator allows to use in the assistant.

The opposite rule from the engines': the ENABLED models are stored. An empty table =
no model = the assistant cannot be used. A model costs at every message and the
provider's catalog changes by itself: a new model must not become usable without
someone having decided it.

A model lives at a provider, and two providers can offer the same name, so `model_id`
says which: the bare name for the provider of the environment (AI__BASE_URL, the only
one before 0035, whose rows, conversations and bills stay as they are), and
`provider:<id>/<name>` for one of the table below. services/ai_models.py reads and
writes that form; the same string goes everywhere a model is named (`ai.default_model`,
conversations, the spending ledger, dbt's kept texts).
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Index, Text
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AiModel(SQLModel, table=True):
    __tablename__ = "ai_models"

    # "gpt-oss-120b" (the environment's provider), "provider:3/claude-opus-5-5" (see above)
    model_id: str = Field(primary_key=True)
    enabled: bool = True
    updated_at: datetime = Field(default_factory=_now)
    updated_by: Optional[int] = Field(default=None, foreign_key="users.id")


class AiProvider(SQLModel, table=True):
    """A model provider added from the panel. Who answers, and who gets the data of the
    conversations that choose its models."""

    __tablename__ = "ai_providers"
    __table_args__ = (Index("ix_ai_providers_name", "name", unique=True),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=80)
    # openai_compatible | anthropic
    kind: str = Field(max_length=40)
    base_url: str = Field(max_length=500)
    # the API key, encrypted (core/crypto.py). It never leaves the gateway.
    secret_encrypted: str = Field(sa_column=Column(Text, nullable=False))
    # off: its models cannot be chosen, its conversations stay readable
    active: bool = True
    created_at: datetime = Field(default_factory=_now)
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
