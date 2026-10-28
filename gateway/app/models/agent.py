"""The agent's work areas and plans (docs/specs/mcp-agent.md), in the metadata database.

Postgres only, nothing in a process's memory: the gateway runs several replicas without
session affinity, and an MCP client's next call — or the person's click on «Confirm» —
may reach any of them.

- `AgentDraft`: a flow being built or changed by the agent. Nothing saved changes while
  the agent works on it: a draft becomes a flow only through a plan the person confirms.
  It goes after a day unused (services/housekeeping.py).
- `AgentPlan`: the changes the agent proposes, in order, with the words that describe
  them. A write tool never changes anything by itself: it adds an action to the pending
  plan, and the plan is applied once, by its owner, while it has not expired.

No foreign key towards users, as for the assistant's conversations: the rows are the
person's, and go with the account (routes/users.py, `remove_user`).
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)  # naive-UTC, as the rest


# where the agent was used from: the chat in Tabularia, or an MCP client with an API token
ORIGINS = ("chat", "mcp")
# pending → applied | failed (one of its actions refused) | cancelled | expired.
# `applying` lasts while the actions are being applied: whoever finds it so (another
# replica, a second click) does not apply the plan again.
PLAN_STATES = ("pending", "applying", "applied", "failed", "cancelled", "expired")


class AgentDraft(SQLModel, table=True):
    __tablename__ = "agent_drafts"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    origin: str = Field(default="mcp", max_length=8)
    chat_id: Optional[int] = None
    token_name: Optional[str] = Field(default=None, max_length=80)
    # the flow the draft changes; None for a new one
    flow_id: Optional[int] = None
    # the fingerprint of that flow's definition when the draft was opened: a plan that
    # saves the draft is refused if someone changed the flow in the meantime
    base_hash: str = Field(default="", max_length=64)
    name: str = Field(default="", max_length=200)
    project_id: Optional[int] = None  # the folder
    engine: str = Field(default="polars", max_length=20)
    # {nodes, edges}, as the editor saves a flow
    definition: str = Field(default='{"nodes": [], "edges": []}', sa_column=Column(Text, nullable=False))
    # the ids handed out already ({"op-": 4, ...}): a removed node's id is not handed out again
    counters: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now, index=True)


class AgentPlan(SQLModel, table=True):
    __tablename__ = "agent_plans"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(index=True)
    origin: str = Field(default="mcp", max_length=8)
    chat_id: Optional[int] = Field(default=None, index=True)
    token_name: Optional[str] = Field(default=None, max_length=80)
    # [{kind, words, ...its arguments, checked when added}], applied in this order
    actions: str = Field(default="[]", sa_column=Column(Text, nullable=False))
    # the plan in words, one line per action (English: what the model reads)
    summary: str = Field(default="", sa_column=Column(Text, nullable=False))
    status: str = Field(default="pending", max_length=16, index=True)
    # once applied: [{ok, object?, link?, error?}], one per action
    result: Optional[str] = Field(default=None, sa_column=Column(Text, nullable=True))
    created_at: datetime = Field(default_factory=_now)
    expires_at: datetime = Field(default_factory=_now, index=True)
    applied_at: Optional[datetime] = None
