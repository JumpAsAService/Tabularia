"""The assistant's conversations, saved in the metadata database.

Before, the history lived ONLY in the browser: the client sent it back at every turn
and the server accepted it as it was. Two consequences, both solved here: closing the
tab threw the conversation away (and asking the same question again costs again), and
a forged history reached the model intact — «system» role parts included, that is
instructions (audit 2026-09-19, A8). Now the server keeps the history and the client
sends only the chat's id.

A TURN = one user question and all that followed (intermediate answers, tool calls,
final answer). It is the right unit for the cost too: `pydantic-ai` reports the cost
of the whole round, not of the single message. Replaying a conversation means
chaining the turns' messages in `seq` order.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive-UTC, as the rest


class AiChat(SQLModel, table=True):
    __tablename__ = "ai_chats"
    # the editor reopens a user's latest conversation about a flow
    __table_args__ = (Index("ix_ai_chats_user_id_flow_id", "user_id", "flow_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    # no FK towards users: a chat survives the account's deletion only if we decide so, and
    # today we do NOT want it — see routes/users.py, which deletes it explicitly together
    # with the user
    user_id: int = Field(index=True)
    # the first question shortened: the title the user recognizes in the list
    title: str = ""
    # the last used, to reopen the chat as it was
    model_id: str = ""
    engine: Optional[str] = None
    # «assistant» (the data assistant's page) or «flow» (the flow editor's helper,
    # about the flow `flow_id`; None for a flow not saved yet)
    kind: str = Field(default="assistant", sa_column_kwargs={"server_default": "assistant"})
    flow_id: Optional[int] = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now, index=True)


class AiChatTurn(SQLModel, table=True):
    """A question and its answer, with what it cost."""

    __tablename__ = "ai_chat_turns"

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: int = Field(index=True, foreign_key="ai_chats.id")
    seq: int = 0  # replay order
    question: str = ""
    # this turn's NEW messages, serialized by pydantic-ai
    # (`ModelMessagesTypeAdapter.dump_python(..., mode="json")`)
    messages: str = "[]"
    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    # The cost in dollars AS pydantic-ai REPORTS IT (`RunUsage.cost`), which works it out
    # from the model's price list. A string and not a float: it is an exact decimal and
    # summing it in floating point would bring in error. `None` = the cost cannot be worked
    # out for that model, which is not the same as «zero».
    cost_usd: Optional[str] = None
    created_at: datetime = Field(default_factory=_now)


class AiSpend(SQLModel, table=True):
    """What each round of the assistant cost. Only the bill, never the text.

    It is kept apart from the conversations for a precise reason: an OBSERVER's chats are
    deleted when they leave (it is a shared account on a public demo, and a stranger's
    questions must neither stay nor be readable by the next visitor). If the spending cap
    read from there, deleting a chat would reset the counter and opening a new one would
    be enough to start spending again. Here instead nobody deletes.

    It holds no question, answer or tools: model, tokens, cost and moment. `turn_id` is
    only a key to find the row when the cost arrives later — it stays even when the turn
    is no longer there.
    """

    __tablename__ = "ai_spend"

    id: Optional[int] = Field(default=None, primary_key=True)
    ts: datetime = Field(default_factory=_now, index=True)
    user_id: int = Field(index=True)
    turn_id: Optional[int] = Field(default=None, index=True)
    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    # as in AiChatTurn: an exact decimal string, `None` = cost cannot be worked out
    cost_usd: Optional[str] = None
