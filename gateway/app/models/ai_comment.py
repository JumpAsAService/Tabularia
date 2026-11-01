"""What the AI wrote on the numbers of a dashboard or a report, kept for whoever reads the same
numbers (services/ai_comment.py, docs/specs/ai-comment.md). The times are naive UTC, as the rest."""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Column, Text
from sqlmodel import Field, SQLModel


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class AiComment(SQLModel, table=True):
    """One row per key, the hash of what was asked: the instructions, the language, the model and
    the numbers themselves. Whoever computes the key has read those numbers with their own rights,
    so the row names neither a person nor a dashboard. The row is also the lock: the one who
    inserts it writes, on any gateway, and a claim lasts as long as `expires_at` says."""

    __tablename__ = "ai_comments"

    key: str = Field(primary_key=True, max_length=64)
    state: str = Field(max_length=10)  # writing | ready | failed
    text: str = Field(default="", sa_column=Column(Text, nullable=False))
    # in words, as the page says it: the provider may be gone by the time it is read
    model: str = ""
    numbers_at: Optional[datetime] = None
    cost_usd: Optional[str] = None  # None = not known, which is not zero
    reason: str = Field(default="", max_length=40)  # what failed, as a code: never what the provider said
    written_at: datetime = Field(default_factory=_now)
    expires_at: datetime = Field(index=True)


class AiCommentThread(SQLModel, table=True):
    """An element as a screen keeps refreshing it: its definition, its parameters, no click. When a
    call was last made for it, on any gateway, and what it wrote: until the interval has passed the
    old text stays. `AI__COMMENT_MIN_SECONDS` is held by a conditional update of this row."""

    __tablename__ = "ai_comment_threads"

    thread: str = Field(primary_key=True, max_length=64)
    called_at: datetime
    comment_key: Optional[str] = Field(default=None, max_length=64)
    expires_at: datetime = Field(index=True)
