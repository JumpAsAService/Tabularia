"""The chat's questions: the agent asks, the person clicks (docs/specs/mcp-agent.md).

`ask_user` is the chat's only: MCP has no elicitation in v1 (it needs a session pinned to
one replica). It ENDS the turn — in the chat it is the agent's output, not a call whose
result goes back to the model (services/agent/chat.py) — and the page shows a card: each
question with its choices, the recommended one first, and a free answer («Other»).

The person's answers come back as their next message, in words anyone reads, a line per
question: «question → answer». The same lines tell, when a conversation is opened again,
which choice was clicked (`answers_of`).
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError

from app.services.agent.registry import ToolContext, ToolError, tool

ARROW = " → "
MAX_ANSWER_CHARS = 300


def one_line(text: Any) -> str:
    return " ".join(str(text or "").split())


class Choice(BaseModel):
    label: str = Field(min_length=1, max_length=80, description="What the user clicks: a few words.")
    description: str = Field("", max_length=240, description="What choosing it means or implies, in one sentence.")


class Question(BaseModel):
    question: str = Field(min_length=3, max_length=240, description="One question, ending with a question mark.")
    options: list[Choice] = Field(min_length=2, max_length=4,
                                  description="2–4 choices, the one you recommend FIRST. Do not add «Other»: "
                                              "the card always offers a free answer.")


class AskUserArgs(BaseModel):
    questions: list[Question] = Field(min_length=1, max_length=4, description="1–4 questions, shown together.")


def card(a: AskUserArgs) -> dict:
    """The questions as the page shows them: one line each, the first choice recommended."""
    out = []
    for q in a.questions:
        labels = [one_line(o.label) for o in q.options]
        if len({label.lower() for label in labels}) != len(labels):
            raise ToolError(f"Two choices of «{one_line(q.question)}» have the same label: make each one different.")
        out.append({"question": one_line(q.question),
                    "options": [{"label": labels[i], "description": one_line(o.description), "recommended": i == 0}
                                for i, o in enumerate(q.options)]})
    if len({q["question"].lower() for q in out}) != len(out):
        raise ToolError("The same question is asked twice.")
    return {"questions": out}


def card_of(raw: Any) -> Optional[dict]:
    """The card of a saved call (its arguments as the model wrote them), or None."""
    import json

    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return None
    try:
        return card(AskUserArgs.model_validate(raw))
    except (ValidationError, ToolError):
        return None


def answers_of(questions: list[dict], message: Optional[str]) -> list[Optional[str]]:
    """The answer to each question in the person's next message («question → answer» per
    line), None for one not answered there (they wrote something else)."""
    found: dict[str, str] = {}
    for line in (message or "").splitlines():
        head, sep, answer = line.partition(ARROW)
        if sep:
            found.setdefault(one_line(head).lower(), one_line(answer))
    return [found.get(q["question"].lower()) for q in questions]


@tool("ask_user", title="Ask the user", args=AskUserArgs, level="read", reads_data=False, only="chat", ends_turn=True)
async def ask_user(ctx: ToolContext, a: AskUserArgs) -> dict:
    """Ask the user 1–4 questions at once, each with 2–4 choices to click (a label and a sentence on what it means), the one you recommend FIRST; the card always adds a free answer. Use it when a choice is the user's and you are not sure — which datasource, which recipients, what time, what to do with rows that do not fit — instead of guessing, and ask everything you need in one card. It ENDS your turn: write at most one short sentence before it, then stop. The answers come as the user's next message, one line per question: «question → answer»."""
    return card(a)
