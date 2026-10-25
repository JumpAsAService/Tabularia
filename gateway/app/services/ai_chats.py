"""Saving and resuming the conversations with the assistant.

The history is on the SERVER: the client sends only the chat's id. Besides making the
conversations survive closing the tab — and avoiding paying for the same question
again — this removes the vector by which a history forged by the client reached the
model intact (audit 2026-09-19, A8).

A chat belongs to one person and is not shared: it holds their questions and rows of
data only they could read. Every read goes through `_chat_of(user)`, which treats
someone else's chat as non-existent.
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from fastapi import HTTPException
from sqlmodel import Session, func, select

from app.models import AiChat, AiChatTurn, AiSpend, User
from app.models.ai_chat import _now

logger = logging.getLogger(__name__)

MAX_TITLE = 80
# the default title written before 1.2.0: a chat that still has it gets the title
# of its first real question. Now an empty title is the default, and the
# interface writes «New conversation» in its own language.
_OLD_DEFAULT_TITLE = "Nuova conversazione"


def _title(question: str) -> str:
    cleaned = " ".join((question or "").split())
    return (cleaned[: MAX_TITLE - 1] + "…") if len(cleaned) > MAX_TITLE else cleaned


def _chat_of(session: Session, user: User, chat_id: int) -> AiChat:
    """The chat, if it is this user's. Otherwise 404: someone else's chat does not exist,
    and cannot be told from a non-existent one."""
    chat = session.get(AiChat, chat_id)
    if chat is None or chat.user_id != user.id:
        raise HTTPException(status_code=404, detail="Conversation not found")
    return chat


def open_session(session: Session, user: User, chat_id: int, model_id: str, engine: Optional[str],
         kind: str = "assistant") -> AiChat:
    """Reopens an existing conversation, remembering its model and engine. A
    conversation of the other assistant is not found: each continues its own."""
    chat = _chat_of(session, user, chat_id)
    if (chat.kind or "assistant") != kind:
        raise HTTPException(status_code=404, detail="Conversation not found")
    chat.model_id, chat.engine = model_id, engine
    # in use again: the housekeeping (ORCHESTRATOR__AI_CHAT_RETENTION_DAYS) must not take
    # it away while the answer is being written, and the answer's cost with it
    chat.updated_at = _now()
    session.add(chat)
    session.commit()
    session.refresh(chat)
    return chat


def create(session: Session, user: User, question: str, model_id: str, engine: Optional[str],
         kind: str = "assistant", flow_id: Optional[int] = None) -> AiChat:
    """Opens a new conversation.

    It is called when the first turn is OVER, not when the question arrives: a request
    broken off halfway would otherwise leave an empty conversation in the list (seen live
    on 2026-09-19)."""
    chat = AiChat(user_id=user.id, title=_title(question), model_id=model_id, engine=engine, kind=kind, flow_id=flow_id)
    session.add(chat)
    session.commit()
    session.refresh(chat)
    return chat


def history(session: Session, chat: AiChat, max_messages: int):
    """The messages of the earlier turns, ready for `message_history`.

    An unreadable turn is SKIPPED instead of failing the chat: better a conversation that
    starts again maimed than one that no longer reopens."""
    from pydantic_ai import ModelMessagesTypeAdapter

    raw_items: list[Any] = []
    turns = session.exec(
        select(AiChatTurn).where(AiChatTurn.chat_id == chat.id).order_by(AiChatTurn.seq)
    ).all()
    for turn in turns:
        try:
            raw_items.extend(json.loads(turn.messages))
        except ValueError:
            logger.warning("chat %s: turn %s unreadable, skipped", chat.id, turn.seq)
    if max_messages:
        raw_items = raw_items[-max_messages:]
    if not raw_items:
        return []
    try:
        return ModelMessagesTypeAdapter.validate_python(raw_items)
    except Exception:  # noqa: BLE001 — incompatible history (e.g. after an upgrade): start again clean
        logger.warning("chat %s: history cannot be validated, the conversation starts again empty", chat.id)
        return []


def save_turn(
    session: Session, chat_id: int, *, question: str, messages: list[Any], model_id: str,
    input_tokens: int, output_tokens: int, requests: int, cost: Optional[Decimal],
) -> AiChatTurn:
    owned_chat = session.get(AiChat, chat_id)
    user_id_ = owned_chat.user_id if owned_chat is not None else 0
    next_ = session.exec(
        select(func.coalesce(func.max(AiChatTurn.seq), -1)).where(AiChatTurn.chat_id == chat_id)
    ).one() + 1
    turn = AiChatTurn(
        chat_id=chat_id, seq=next_, question=question[:2000],
        messages=json.dumps(messages, ensure_ascii=False, default=str),
        model_id=model_id, input_tokens=input_tokens or 0, output_tokens=output_tokens or 0,
        requests=requests or 0,
        # `None` = the cost cannot be worked out for this model, which is NOT zero
        cost_usd=(str(cost) if cost is not None else None),
    )
    session.add(turn)
    session.flush()  # the turn's id is needed to tie the spending row to it
    # the accounting goes in its own log: chats can be deleted, the bill cannot
    session.add(AiSpend(
        user_id=user_id_, turn_id=turn.id, model_id=model_id,
        input_tokens=input_tokens or 0, output_tokens=output_tokens or 0,
        cost_usd=(str(cost) if cost is not None else None),
    ))
    chat = session.get(AiChat, chat_id)
    if chat is not None:
        chat.updated_at = _now()
        if not chat.title or chat.title == _OLD_DEFAULT_TITLE:
            chat.title = _title(question)
        session.add(chat)
    session.commit()
    session.refresh(turn)
    return turn


def _sum_costs(values) -> Optional[str]:
    """The exact sum of the known costs. `None` when no turn has a cost."""
    total, seen = Decimal(0), False
    for v in values:
        if not v:
            continue
        try:
            total += Decimal(v)
            seen = True
        except InvalidOperation:
            continue
    return str(total) if seen else None


def summary(session: Session, chat: AiChat) -> dict[str, Any]:
    rows = session.exec(
        select(AiChatTurn.cost_usd, AiChatTurn.input_tokens, AiChatTurn.output_tokens)
        .where(AiChatTurn.chat_id == chat.id)
    ).all()
    return {
        "turns": len(rows),
        "input_tokens": sum(r[1] or 0 for r in rows),
        "output_tokens": sum(r[2] or 0 for r in rows),
        "cost_usd": _sum_costs(r[0] for r in rows),
    }


def listing(session: Session, user: User, limit: int) -> list[dict[str, Any]]:
    chats = session.exec(
        # the flow editor's conversations live in the editor, not in this list
        select(AiChat).where(AiChat.user_id == user.id, AiChat.kind == "assistant")
        .order_by(AiChat.updated_at.desc()).limit(limit)
    ).all()
    rows = [{"id": c.id, "title": c.title, "model_id": c.model_id, "engine": c.engine,
              "created_at": c.created_at, "updated_at": c.updated_at, **summary(session, c)} for c in chats]
    # A conversation without turns has nothing to show: it is not listed. The creation is
    # deferred to the first finished turn by now, but the safety net holds for the rows
    # left from before and for any race.
    return [r for r in rows if r["turns"]]


def detail(session: Session, user: User, chat_id: int) -> dict[str, Any]:
    """The conversation to put back on screen: questions, answers and the cost of each turn."""
    chat = _chat_of(session, user, chat_id)
    turns = session.exec(
        select(AiChatTurn).where(AiChatTurn.chat_id == chat.id).order_by(AiChatTurn.seq)
    ).all()
    return {
        "id": chat.id, "title": chat.title, "model_id": chat.model_id, "engine": chat.engine,
        "created_at": chat.created_at, "updated_at": chat.updated_at,
        **summary(session, chat),
        "messages": [_turn_for_ui(t) for t in turns],
    }


def follow_flow(session: Session, chat: AiChat, flow_id: Optional[int]) -> None:
    """A conversation started on a flow not saved yet takes the flow's id once it
    has one; a conversation about another flow is not continued here."""
    if chat.flow_id == flow_id:
        return
    if chat.flow_id is not None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    chat.flow_id = flow_id
    session.add(chat)
    session.commit()


def forget_flow(session: Session, flow_id: int) -> None:
    """The flow editor's conversations about a flow that is deleted go with it (the
    spending ledger stays: it is the bill, not the conversation). Does not commit."""
    from sqlalchemy import delete as sa_delete

    ids = list(session.exec(select(AiChat.id).where(AiChat.kind == "flow", AiChat.flow_id == flow_id)).all())
    if ids:
        session.exec(sa_delete(AiChatTurn).where(AiChatTurn.chat_id.in_(ids)))
        session.exec(sa_delete(AiChat).where(AiChat.id.in_(ids)))


def latest_flow_chat(session: Session, user: User, flow_id: int) -> Optional[AiChat]:
    """This user's latest conversation with the flow editor's assistant about a flow."""
    return session.exec(
        select(AiChat).where(AiChat.user_id == user.id, AiChat.kind == "flow", AiChat.flow_id == flow_id)
        .order_by(AiChat.updated_at.desc()).limit(1)
    ).first()


def _turn_for_ui(turn: AiChatTurn) -> dict[str, Any]:
    """A turn as the chat shows it: the question, the answer's text, the steps taken, and
    what it cost.

    It is rebuilt from the saved messages instead of keeping a second copy of the text: a
    single copy cannot diverge from the other."""
    text: list[str] = []
    steps: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    try:
        messages = json.loads(turn.messages)
    except ValueError:
        messages = []
    for m in messages:
        for part in m.get("parts", []) or []:
            kind = part.get("part_kind")
            if kind == "text" and part.get("content"):
                text.append(str(part["content"]))
            elif kind == "tool-call":
                step = {
                    "id": part.get("tool_call_id"),
                    "name": part.get("tool_name"),
                    "args": part.get("args"),
                }
                steps.append(step)
                if step["id"]:
                    by_id[str(step["id"])] = step
            elif kind == "tool-return":
                # the TABLE that backs the answer: without it, a reopened conversation would show the
                # steps and not the evidence
                step = by_id.get(str(part.get("tool_call_id")))
                content = part.get("content")
                if step is None or not isinstance(content, dict):
                    continue
                if isinstance(content.get("rows"), list) and "row_count" in content:
                    step["table"] = {
                        k: content.get(k)
                        for k in ("datasource", "columns", "rows", "row_count", "truncated")
                    }
                if isinstance(content.get("chart"), dict):
                    step["chart"] = content["chart"]
                if content.get("node"):  # the flow editor's tools: the node made or changed
                    step["node"] = str(content["node"])
    return {
        "seq": turn.seq,
        "question": turn.question,
        # each text part is a paragraph of its own: the model writes some before a
        # tool call and some after, and glued together they ran into one sentence
        "answer": "\n\n".join(text),
        "steps": steps,
        "model_id": turn.model_id,
        "usage": {
            "input_tokens": turn.input_tokens,
            "output_tokens": turn.output_tokens,
            "requests": turn.requests,
            "cost_usd": turn.cost_usd,
        },
        "created_at": turn.created_at,
    }


def remove(session: Session, user: User, chat_id: int) -> None:
    from sqlalchemy import delete as sa_delete

    chat = _chat_of(session, user, chat_id)
    session.exec(sa_delete(AiChatTurn).where(AiChatTurn.chat_id == chat.id))
    session.delete(chat)
    session.commit()


def add_cost(session: Session, turn_id: int, extra: Optional[Decimal]) -> None:
    """Adds a cost to a turn saved already (the call that writes the title).

    The conversation's total is a promise of the page: a paid call that does not end up
    in it makes it lie on the low side."""
    if extra is None:
        return
    turn = session.get(AiChatTurn, turn_id)
    if turn is None:
        return
    base = Decimal(turn.cost_usd) if turn.cost_usd else Decimal(0)
    turn.cost_usd = str(base + extra)
    session.add(turn)
    # and the same sum in the log, which is what the caps rest on
    row = session.exec(select(AiSpend).where(AiSpend.turn_id == turn_id)).first()
    if row is not None:
        b = Decimal(row.cost_usd) if row.cost_usd else Decimal(0)
        row.cost_usd = str(b + extra)
        session.add(row)
    session.commit()


def rename(session: Session, chat_id: int, title: str) -> None:
    """Replaces the fallback title with the one the model wrote."""
    chat = session.get(AiChat, chat_id)
    if chat is None:
        return
    clean_one = " ".join((title or "").split())[:MAX_TITLE]
    if not clean_one:
        return
    chat.title = clean_one
    session.add(chat)
    session.commit()


def step_query(session: Session, user: User, chat_id: int, seq: int, step_id: str) -> tuple[int, str]:
    """The datasource and the SQL of a saved step, to export it again.

    The query is read again from the TURN, it is not accepted from the client: whoever
    downloads must not be able to choose what runs. Returns `(datasource_id, sql)`."""
    _chat_of(session, user, chat_id)  # 404 if it is not theirs
    turn = session.exec(
        select(AiChatTurn).where(AiChatTurn.chat_id == chat_id, AiChatTurn.seq == seq)
    ).first()
    if turn is None:
        raise HTTPException(status_code=404, detail="Step not found")
    try:
        messages = json.loads(turn.messages)
    except ValueError:
        messages = []
    for m in messages:
        for part in m.get("parts", []) or []:
            if part.get("part_kind") != "tool-call" or str(part.get("tool_call_id")) != str(step_id):
                continue
            if part.get("tool_name") != "query_datasource":
                raise HTTPException(status_code=422, detail="This step is not a query")
            args = part.get("args")
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            if not isinstance(args, dict) or not args.get("sql"):
                raise HTTPException(status_code=422, detail="The step has no query to run again")
            return int(args.get("datasource_id") or 0), str(args["sql"])
    raise HTTPException(status_code=404, detail="Step not found")


def record_spend(
    session: Session, *, user_id: int, model_id: str,
    input_tokens: int, output_tokens: int, cost: Optional[Decimal],
) -> None:
    """Records what a round whose conversation is NOT saved cost.

    It is the OBSERVERS' accounting: their questions are written nowhere, but the bill is
    — otherwise the daily cap would not see the spending of whoever comes in from the
    shared account, that is exactly the one to keep an eye on."""
    session.add(AiSpend(
        user_id=user_id, turn_id=None, model_id=model_id,
        input_tokens=input_tokens or 0, output_tokens=output_tokens or 0,
        cost_usd=(str(cost) if cost is not None else None),
    ))
    session.commit()


def _sum(rows) -> Decimal:
    total = Decimal(0)
    for v in rows:
        if not v:
            continue
        try:
            total += Decimal(v)
        except InvalidOperation:
            continue
    return total


def _start_of_day():
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)


def spent_today(session: Session, user: User) -> Decimal:
    """How much this user spent TODAY with the assistant.

    It is read from the `ai_spend` log and not from the conversations: an observer's
    chats are deleted when they leave, and a cap that disappears together with the data
    it must count is not a cap."""
    rows = session.exec(
        select(AiSpend.cost_usd).where(AiSpend.user_id == user.id, AiSpend.ts >= _start_of_day())
    ).all()
    return _sum(rows)


def spent_today_all(session: Session) -> Decimal:
    """How much the whole installation spent TODAY.

    It is the cap that counts where the account is shared: on a public demo everyone
    signs in as the same guest and the bill is one."""
    rows = session.exec(select(AiSpend.cost_usd).where(AiSpend.ts >= _start_of_day())).all()
    return _sum(rows)
