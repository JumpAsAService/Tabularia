"""AI assistant: state, models (admin panel) and the streaming chat.

- `GET  /ai/status`            for everyone: is the assistant on? with which models?
- `GET  /ai/models`            superuser: the provider's catalog + the admin's choice
- `PUT  /ai/models/{id}`       superuser: enables/disables a model (audit)
- `GET  /ai/prompts`           observer: the agents' instructions, the text and the context added
- `PUT  /ai/prompts/{agent}`   superuser: changes an agent's text (audit)
- `DELETE /ai/prompts/{agent}` superuser: back to the default text (audit)
- `POST /ai/chat`              for everyone: one conversation turn, over SSE

The conversation is NOT saved on the server: the history goes back to the client at
the end of the turn (`done.history`) and the client sends it back at the next turn.
A forged history can only confuse the conversation of whoever sends it: every tool
checks the permissions again at every call.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from decimal import Decimal
from uuid import uuid4
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Callable, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.db.session import engine as db_engine, get_session
from app.deps.auth import get_current_user, require_observer, require_superuser
from app.deps.permissions import ensure_can
from app.models.permission import Capability
from app.models import AiChat, AiModel, AiPrompt, Datasource, User
from app.services.permissions import is_observer_only
from app.services import ai_agent, ai_chats, ai_flow_agent, ai_pricing, ai_prompts, ai_script_agent, audit
from app.services.ai_models import enabled_model_ids, ensure_configured, is_chat_model, provider_models
from app.services.engine_policy import pick_engine
from app.services.permissions import readable_project_ids
from app.core.routing import ReleasingRoute

logger = logging.getLogger(__name__)
router = APIRouter(route_class=ReleasingRoute, prefix="/ai", tags=["ai"])


def _new_session() -> Session:
    """DB session for the agent's tools (one per call): the stream lives longer than the
    request's session. The tests replace it."""
    return Session(db_engine)


# ── state and models ─────────────────────────────────────────────────────────
class AiStatusOut(BaseModel):
    enabled: bool
    models: list[str] = []
    default_model: Optional[str] = None


@router.get("/status", response_model=AiStatusOut)
def ai_status(user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> AiStatusOut:
    cfg = get_settings().ai
    if not cfg.enabled:
        return AiStatusOut(enabled=False)
    models = enabled_model_ids(session)
    default = cfg.default_model if cfg.default_model in models else (models[0] if models else None)
    return AiStatusOut(enabled=True, models=models, default_model=default)


class AiModelOut(BaseModel):
    model_id: str
    enabled: bool
    chat: bool  # False = embedding/audio/reranker: cannot be enabled for the chat
    available: bool  # False = enabled in the past, but the provider no longer lists it


class AiModelUpdate(BaseModel):
    enabled: bool


@router.get("/models", response_model=list[AiModelOut])
async def list_ai_models(user: User = Depends(require_observer), session: Session = Depends(get_session)) -> list[AiModelOut]:
    offered = await provider_models()
    chosen = {m.model_id: m.enabled for m in session.exec(select(AiModel)).all()}
    out = [AiModelOut(model_id=m, enabled=bool(chosen.get(m)), chat=is_chat_model(m), available=True) for m in offered]
    # enabled in the past and gone from the catalog: visible, so they can be turned off
    out += [AiModelOut(model_id=m, enabled=e, chat=is_chat_model(m), available=False)
            for m, e in sorted(chosen.items()) if m not in offered and e]
    return out


@router.put("/models/{model_id:path}", response_model=AiModelOut)
async def set_ai_model(
    model_id: str,
    body: AiModelUpdate,
    request: Request = None,  # type: ignore[assignment]
    user: User = Depends(require_superuser),
    session: Session = Depends(get_session),
) -> AiModelOut:
    offered = await provider_models()
    row = session.get(AiModel, model_id)
    if model_id not in offered and row is None:
        raise HTTPException(status_code=404, detail=f"Model unknown to the provider: {model_id}")
    if body.enabled and not is_chat_model(model_id):
        raise HTTPException(status_code=422, detail="This model is not a chat model (embedding, audio or reranker)")
    if body.enabled and model_id not in offered:
        raise HTTPException(status_code=422, detail="The provider no longer offers this model")
    if row is None:
        row = AiModel(model_id=model_id)
    row.enabled = body.enabled
    row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    row.updated_by = user.id
    session.add(row)
    session.commit()
    audit.record_audit(
        session, actor=user, action=audit.AI_MODEL_ENABLE if body.enabled else audit.AI_MODEL_DISABLE,
        target_type="ai_model", target_label=model_id, request=request,
    )
    return AiModelOut(model_id=model_id, enabled=body.enabled, chat=is_chat_model(model_id), available=model_id in offered)


# ── the agents' instructions ─────────────────────────────────────────────────
class AiPromptOut(BaseModel):
    agent: str
    text: str
    default: str
    custom: bool
    # what Tabularia adds at every request, with example values: shown, not editable
    context: str
    updated_at: Optional[datetime] = None
    updated_by: Optional[str] = None


class AiPromptUpdate(BaseModel):
    text: str = Field(min_length=1, max_length=ai_prompts.MAX_CHARS)


def _example_context(agent: str, locale: str) -> str:
    """The context with example values in place of the request's ones."""
    if agent == "assistant":
        return ai_agent.context(None, locale)
    if agent == "flow":
        return ai_flow_agent.context(None, locale, "‹flow name›", "op-3")
    if agent == "script":
        return ai_script_agent.context(ai_script_agent.ScriptDeps(
            user=None, session_factory=_new_session, engine=None, model_id="", locale=locale,  # type: ignore[arg-type]
            language_version="3.12", environment="‹environment›", packages=["‹packages›"], variables=["‹variables›"],
            files=[{"path": "main.py", "content": "‹the files' content›"}], entrypoint="main.py",
        ))
    if agent == "title":
        return ai_prompts.language_line(locale, "the title")
    if agent == "dbt_translate":
        from app.services.dbt_ai import translation_context

        return translation_context("‹source dialect›", "‹target dialect›", ["‹the model's columns›"])
    return ""


def _prompt_out(session: Session, user: User, agent: str, locale: str) -> AiPromptOut:
    from app.services.masking import mask_email
    from app.services.permissions import is_admin

    row = session.get(AiPrompt, agent)
    custom = row is not None and bool(row.text.strip())
    by = row.updated_by if custom else None
    return AiPromptOut(
        agent=agent, text=row.text if custom else ai_prompts.DEFAULTS[agent], default=ai_prompts.DEFAULTS[agent],
        custom=custom, context=_example_context(agent, locale),
        updated_at=row.updated_at if custom else None,
        updated_by=(by if is_admin(session, user) else mask_email(by)) if by else None,
    )


def _agent(agent: str) -> str:
    if agent not in ai_prompts.AGENTS:
        raise HTTPException(status_code=404, detail=f"Unknown agent: {agent}")
    return agent


@router.get("/prompts", response_model=list[AiPromptOut])
def list_ai_prompts(
    locale: str = "en", user: User = Depends(require_observer), session: Session = Depends(get_session),
) -> list[AiPromptOut]:
    return [_prompt_out(session, user, a, locale[:10]) for a in ai_prompts.AGENTS]


@router.put("/prompts/{agent}", response_model=AiPromptOut)
def set_ai_prompt(
    agent: str,
    body: AiPromptUpdate,
    locale: str = "en",
    request: Request = None,  # type: ignore[assignment]
    user: User = Depends(require_superuser),
    session: Session = Depends(get_session),
) -> AiPromptOut:
    _agent(agent)
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="The instructions cannot be empty: restore the default instead")
    ai_prompts.save(session, agent, text, user.email)
    audit.record_audit(
        session, actor=user, action=audit.AI_PROMPT_UPDATE, target_type="ai_prompt", target_label=agent,
        detail={"chars": len(text)}, request=request,
    )
    return _prompt_out(session, user, agent, locale[:10])


@router.delete("/prompts/{agent}", response_model=AiPromptOut)
def reset_ai_prompt(
    agent: str,
    locale: str = "en",
    request: Request = None,  # type: ignore[assignment]
    user: User = Depends(require_superuser),
    session: Session = Depends(get_session),
) -> AiPromptOut:
    _agent(agent)
    if ai_prompts.reset(session, agent):
        audit.record_audit(
            session, actor=user, action=audit.AI_PROMPT_RESET, target_type="ai_prompt", target_label=agent, request=request,
        )
    return _prompt_out(session, user, agent, locale[:10])


# ── chat ─────────────────────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    model: str = Field(min_length=1, max_length=200)
    # The conversation to continue. The SERVER keeps the history: the client no longer
    # sends it (and so can no longer forge it — audit A8). Missing = a new conversation.
    chat_id: Optional[int] = None
    engine: Optional[str] = Field(default=None, max_length=40)
    locale: str = Field(default="en", max_length=10)
    # datasources in focus on the page: context for the assistant, not a permission
    focus: list[int] = Field(default_factory=list, max_length=8)


def _sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n".encode()


def _tool_result_payload(part: Any) -> dict[str, Any]:
    """What the chat shows of a result: the outcome, and the TABLE when the tool returned
    one (the true numbers, not the model's transcription)."""
    content = getattr(part, "content", None)
    ok = type(part).__name__ != "RetryPromptPart"
    out: dict[str, Any] = {"id": getattr(part, "tool_call_id", None), "name": getattr(part, "tool_name", None), "ok": ok}
    # a TABLE is the result of a query: `rows` is a list. (describe_datasource has `rows`
    # too, but there it is the number of rows.)
    if isinstance(content, dict) and isinstance(content.get("rows"), list) and isinstance(content.get("columns"), list) and "row_count" in content:
        out["table"] = {k: content.get(k) for k in ("datasource", "columns", "rows", "row_count", "truncated")}
        if isinstance(content.get("chart"), dict):
            out["chart"] = content["chart"]
    elif isinstance(content, dict) and content.get("error"):
        out["ok"], out["error"] = False, str(content["error"])[:500]
    elif isinstance(content, dict) and content.get("node"):
        # the flow editor's tools: which node they made or changed, and a step whose preview failed
        out["node"] = str(content["node"])
        if content.get("preview_error"):
            out["warning"] = str(content["preview_error"])[:500]
    elif isinstance(content, dict) and isinstance(content.get("datasources"), list):
        out["count"] = len(content["datasources"])
    elif isinstance(content, list):
        out["count"] = len(content)
    elif not ok:
        out["error"] = str(content)[:500]
    return out


# ── saved conversations ──────────────────────────────────────────────────────
@router.get("/chats")
def list_chats(user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """The conversations of WHOEVER ASKS, newest first. Never those of others.

    For an OBSERVER the list is empty by construction — their conversations are not
    saved — but it is said out loud here: if one day something wrote a chat in their
    name, it would not be served to anyone signing in with the shared account anyway."""
    if is_observer_only(session, user):
        return []
    return ai_chats.listing(session, user, get_settings().ai.max_chats_listed)


@router.get("/chats/{chat_id}")
def get_chat(chat_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """A whole conversation, ready to put back on screen: questions, answers, steps and the
    cost of each turn."""
    return ai_chats.detail(session, user, chat_id)


@router.delete("/chats/{chat_id}", status_code=204)
def delete_chat(chat_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    ai_chats.remove(session, user, chat_id)


@router.get("/chats/{chat_id}/turns/{seq}/steps/{step_id}/export")
async def export_step(
    chat_id: int, seq: int, step_id: str, fmt: str = "csv",
    request: Request = None,  # type: ignore[assignment]
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """Downloads the COMPLETE result of an assistant query.

    The chat shows at most `AI__MAX_RESULT_ROWS` rows; the file holds them all, because
    the engine runs the query again on the normal export path — streaming, format cap,
    audit. The SQL is read again from the saved turn: the client sends only the step's
    id, never a query."""
    if fmt not in ("csv", "xlsx"):
        raise HTTPException(status_code=422, detail="Unsupported format: use csv or xlsx")
    datasource_id, sql = ai_chats.step_query(session, user, chat_id, seq, step_id)
    # the permissions are checked NOW: the conversation may be old and whoever wrote it may
    # have lost access meanwhile
    ds = session.get(Datasource, datasource_id)
    if ds is None or ds.project_id not in readable_project_ids(session, user):
        raise HTTPException(status_code=404, detail="Datasource not available")
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", ds.name)[:60] or "result"
    payload = {
        "bucket": ds.bucket, "input_key": ds.key,
        "operations": [{"type": "sql", "params": {"query": sql}}],
        "format": fmt, "filename": f"{name}.{fmt}",
    }
    engine = await pick_engine(session, None)
    if engine:
        payload["engine"] = engine
    audit.record_audit(
        session, actor=user, action=audit.EXPORT_DOWNLOAD, request=request,
        target_type="datasource", target_id=ds.id, target_label=ds.name,
        detail={"format": fmt, "via": "ai", "chat_id": chat_id, "sql": sql[:2000]},
    )
    client = get_engine_client()
    req = client.build_request("POST", "/tasks/export", json=payload, timeout=600)
    resp = await client.send(req, stream=True)
    if resp.status_code >= 400:
        text = (await resp.aread()).decode(errors="replace")[:500]
        await resp.aclose()
        raise HTTPException(status_code=resp.status_code, detail=f"Export failed: {text}")
    return StreamingResponse(
        resp.aiter_bytes(),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
        headers={"Content-Disposition": resp.headers.get("content-disposition", f'attachment; filename="{name}.{fmt}"')},
        background=BackgroundTask(resp.aclose),
    )


def _check_spending(session: Session, user: User) -> None:
    """The daily caps, checked BEFORE the stream opens: a refusal must come as an
    error of the request, not as an event halfway through an answer. The
    installation's cap first: where the account is shared — a demo open to
    strangers — the per-user cap protects nothing, because the user is one and
    so is the key that pays."""
    cfg = get_settings().ai
    if cfg.max_cost_per_day_total_usd:
        spent_all = ai_chats.spent_today_all(session)
        if spent_all >= Decimal(str(cfg.max_cost_per_day_total_usd)):
            raise HTTPException(
                status_code=429,
                detail=(
                    "The assistant is not available for the rest of the day: "
                    "this installation has reached its spending cap. "
                    "Try again tomorrow."
                ),
            )
    if cfg.max_cost_per_day_usd:
        spent = ai_chats.spent_today(session, user)
        if spent >= Decimal(str(cfg.max_cost_per_day_usd)):
            raise HTTPException(
                status_code=429,
                detail=(
                    f"You have reached today's spending cap for the assistant "
                    f"({spent:.4f} $ of {cfg.max_cost_per_day_usd} $). Try again tomorrow."
                ),
            )


async def _turn_stream(
    *, agent: Any, message: str, history: list, deps: Any, model: Any, model_id: str, user: User,
    engine: Optional[str], ephemeral: bool, chat_id: Optional[int], locale: str,
    kind: str = "assistant", flow_id: Optional[int] = None,
    extra_done: Optional[Callable[[], dict[str, Any]]] = None,
    request_limit: Optional[int] = None,
) -> AsyncIterator[bytes]:
    """One turn of either assistant, as Server-Sent Events: text, tool calls and
    results, then `done` with the usage — after the turn is saved on the server (a
    new conversation is born here, at the end of its first turn) — or `error`.

    A turn stopped halfway (a cap, the provider, an unexpected error) has still
    cost what its requests cost: that goes to the spending ledger, which the daily
    caps read. `extra_done` goes with `error` too: the flow editor applies what was
    built before the stop."""
    from pydantic_ai import (
        AgentRunResultEvent, FunctionToolCallEvent, FunctionToolResultEvent, ModelMessagesTypeAdapter,
        PartDeltaEvent, PartStartEvent, TextPart, TextPartDelta,
    )
    from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded
    from pydantic_ai.usage import RunUsage, UsageLimits

    cfg = get_settings().ai
    # ours, passed in: pydantic-ai counts into it, so a turn that stops halfway still says what it spent
    spent = RunUsage()
    accounted = False

    def unfinished() -> dict[str, Any]:
        if not accounted and spent.requests:
            try:
                with _new_session() as s:
                    ai_chats.record_spend(s, user_id=user.id, model_id=model_id, input_tokens=spent.input_tokens,
                                            output_tokens=spent.output_tokens, cost=ai_pricing.cost_of(spent, model_id))
            except Exception:  # noqa: BLE001 — the user already has the error: the ledger must not hide it
                logger.exception("assistant: cost of the interrupted turn not recorded")
        try:
            return extra_done() if extra_done is not None else {}
        except Exception:  # noqa: BLE001
            return {}

    try:
        async with agent.run_stream_events(
            message, message_history=history, deps=deps, model=model, usage=spent,
            usage_limits=UsageLimits(
                request_limit=request_limit or cfg.max_requests,
                # the rounds do not cap the tokens: without these two, an inflated history spends as
                # much as it likes (audit A8)
                cost_limit=Decimal(str(cfg.max_cost_per_turn_usd)) if cfg.max_cost_per_turn_usd else None,
                input_tokens_limit=cfg.max_input_tokens_per_turn,
            ),
        ) as events:
            async for ev in events:
                if isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart):
                    if ev.part.content:
                        yield _sse("text", {"delta": ev.part.content})
                elif isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, TextPartDelta):
                    if ev.delta.content_delta:
                        yield _sse("text", {"delta": ev.delta.content_delta})
                elif isinstance(ev, FunctionToolCallEvent):
                    try:
                        args = ev.part.args_as_dict()
                    except Exception:  # noqa: BLE001 — malformed arguments: the tool will see them
                        args = {}
                    yield _sse("tool_call", {"id": ev.part.tool_call_id, "name": ev.part.tool_name, "args": args})
                elif isinstance(ev, FunctionToolResultEvent):
                    yield _sse("tool_result", _tool_result_payload(ev.part))
                elif isinstance(ev, AgentRunResultEvent):
                    usage = ev.result.usage
                    usage = usage() if callable(usage) else usage  # a method in 1.x, a property in 2.x
                    # pydantic-ai prices under the PROVIDER: with our OpenAI-compatible endpoint many
                    # models are not found. The fallback looks by name (see ai_pricing).
                    cost = ai_pricing.cost_of(usage, model_id)
                    # this turn's NEW messages: the earlier history is saved already and must not be
                    # written again
                    try:
                        new_items = ev.result.new_messages()
                    except AttributeError:  # pragma: no cover — older pydantic-ai
                        new_items = ev.result.all_messages()[len(history):]
                    turn = None
                    accounted = True  # from here the turn is saved, or its spending recorded
                    try:
                        # an OWN session: the request's is closed already, the stream lives longer
                        with _new_session() as s_save:
                            if ephemeral:
                                ai_chats.record_spend(
                                    s_save, user_id=user.id, model_id=model_id,
                                    input_tokens=usage.input_tokens,
                                    output_tokens=usage.output_tokens, cost=cost,
                                )
                                raise _NothingToSave
                            if chat_id is None:
                                chat_id = ai_chats.create(s_save, user, message, model_id, engine, kind=kind, flow_id=flow_id).id
                            turn = ai_chats.save_turn(
                                s_save, chat_id, question=message,
                                messages=ModelMessagesTypeAdapter.dump_python(new_items, mode="json"),
                                model_id=model_id, input_tokens=usage.input_tokens,
                                output_tokens=usage.output_tokens, requests=usage.requests, cost=cost,
                            )
                            summary = ai_chats.summary(s_save, s_save.get(AiChat, chat_id))
                    except _NothingToSave:
                        summary = None  # observer: there is nothing to summarize
                    except Exception:  # noqa: BLE001 — the answer was given already: it is not lost to a saving error
                        logger.exception("assistant: turn not saved (chat %s)", chat_id)
                        summary = None
                    yield _sse("done", {
                        "chat_id": chat_id,
                        "seq": turn.seq if turn is not None else None,
                        "usage": {
                            "requests": usage.requests, "input_tokens": usage.input_tokens,
                            "output_tokens": usage.output_tokens,
                            "cost_usd": str(cost) if cost is not None else None,
                        },
                        "chat_total": summary,
                        **(extra_done() if extra_done is not None else {}),
                    })
                    # The model writes the title, but AFTER closing the turn: doing it earlier would have
                    # kept the status line on «writing the answer» while it was writing the title. Its
                    # cost is added to the turn. (The flow editor shows no list of conversations: no
                    # title there.)
                    if turn is not None and turn.seq == 0 and kind == "assistant":
                        _in_flight.add(asyncio.create_task(
                            _name_chat(chat_id, turn.id, model_id, message, locale)
                        ))
    except UsageLimitExceeded as e:
        # the same event for steps, tokens and cost: the user needs to know the question was
        # too big, not which counter stopped it
        logger.info("assistant: usage cap reached (chat %s): %s", chat_id, e)
        yield _sse("error", {"code": "too_many_steps", "message": "The question went beyond the limits set for a single turn. Try narrowing it down.", **unfinished()})
    except ModelHTTPError as e:
        logger.warning("assistant: the provider answered %s for %s", e.status_code, model_id)
        yield _sse("error", {"code": "provider", "message": f"The AI provider refused the request ({e.status_code}).", **unfinished()})
    except Exception:  # noqa: BLE001 — the stream has started already: the error goes INSIDE the stream
        logger.exception("assistant: error during the conversation")
        yield _sse("error", {"code": "internal", "message": "Unexpected error in the assistant.", **unfinished()})


_SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@router.post("/chat")
async def chat(
    body: ChatRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    ensure_configured()
    cfg = get_settings().ai
    if body.model not in enabled_model_ids(session):
        raise HTTPException(status_code=403, detail="This model is not enabled by the administrator")
    engine = await pick_engine(session, body.engine)
    # A NEW conversation: the row is created when the turn is over (see ai_chats.crea), so
    # an interrupted request leaves no empty chat.
    _check_spending(session, user)
    # OBSERVER: the conversation is saved nowhere. It is a shared account on an open
    # installation — a public demo — and a stranger's questions must neither stay nor be
    # readable by the next visitor. Only the accounting remains, without text, because
    # the spending cap must keep seeing what whoever comes in from there costs.
    ephemeral = is_observer_only(session, user)
    chat = (ai_chats.open_session(session, user, body.chat_id, body.model, engine)
            if body.chat_id and not ephemeral else None)
    chat_id = chat.id if chat is not None else None
    history = ai_chats.history(session, chat, cfg.max_history_messages) if chat is not None else []

    # without a chat there is no slot key: two visitors of the shared guest would cancel
    # each other's previews
    slot_key = uuid4().hex[:12] if ephemeral else None
    deps = ai_agent.ChatDeps(
        user=user, session_factory=_new_session, engine=engine, model_id=body.model,
        slot_key=slot_key,
        locale=body.locale, request=request, chat_id=chat_id,
        focus=ai_agent.readable_focus(session, user, body.focus),
        prompt=ai_prompts.text(session, "assistant"),
    )
    stream = _turn_stream(
        agent=ai_agent.agent, message=body.message, history=history, deps=deps,
        model=ai_agent.build_model(body.model), model_id=body.model, user=user, engine=engine,
        ephemeral=ephemeral, chat_id=chat_id, locale=body.locale,
    )
    return StreamingResponse(stream, media_type="text/event-stream", headers=_SSE_HEADERS)


# ── the flow editor's assistant ──────────────────────────────────────────────
class FlowAssistRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    model: str = Field(min_length=1, max_length=200)
    chat_id: Optional[int] = None
    # the flow being edited, None while it is not saved yet
    flow_id: Optional[int] = None
    # the canvas as the editor saves it: the assistant changes THIS, not the saved flow
    definition: str = Field(max_length=ai_flow_agent.MAX_DEFINITION_CHARS)
    engine: Optional[str] = Field(default=None, max_length=40)
    locale: str = Field(default="en", max_length=10)
    selected: Optional[str] = Field(default=None, max_length=100)
    # the editor's id counters ({"op-": 12, ...}): new nodes never take back the id of
    # one the user just deleted, whose columns and counts the editor may still hold
    counters: dict[str, int] = Field(default_factory=dict, max_length=8)


@router.post("/flow-assist")
async def flow_assist(
    body: FlowAssistRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """One turn of the flow editor's assistant. It changes the canvas the editor
    sent and returns it in `done.definition` (null when nothing changed), with the
    nodes it added or changed in `done.touched`: the editor applies it as one Undo
    step. It never saves, runs or schedules the flow."""
    ensure_configured()
    cfg = get_settings().ai
    if body.model not in enabled_model_ids(session):
        raise HTTPException(status_code=403, detail="This model is not enabled by the administrator")
    if is_observer_only(session, user):
        raise HTTPException(status_code=403, detail="Observers do not change flows")
    flow = ai_flow_agent.flow_of(session, body.flow_id)
    if body.flow_id is not None and flow is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    if flow is not None:
        ensure_can(session, user, flow.project_id, Capability.EDIT)
    # an existing flow keeps the engine it has even if the administrator no longer
    # offers it for new choices: the editor still previews it
    engine = body.engine if flow is not None and body.engine and body.engine == flow.engine else await pick_engine(session, body.engine)
    _check_spending(session, user)
    draft = ai_flow_agent.parse_definition(body.definition, body.counters)
    chat = ai_chats.open_session(session, user, body.chat_id, body.model, engine, kind="flow") if body.chat_id else None
    if chat is not None:
        # a conversation started on a new flow follows it once saved; another flow's is not continued here
        ai_chats.follow_flow(session, chat, flow.id if flow is not None else None)
    chat_id = chat.id if chat is not None else None
    history = ai_chats.history(session, chat, cfg.max_history_messages) if chat is not None else []
    deps = ai_flow_agent.FlowDeps(
        user=user, session_factory=_new_session, engine=engine, model_id=body.model, locale=body.locale,
        request=request, chat_id=chat_id, draft=draft,
        flow_id=flow.id if flow is not None else None, flow_name=flow.name if flow is not None else "",
        project_id=flow.project_id if flow is not None else None, selected=body.selected,
        preview_key=uuid4().hex[:10], prompt=ai_prompts.text(session, "flow"),
    )

    def changes() -> dict[str, Any]:
        return {"definition": json.dumps(draft.definition()) if draft.changed() else None,
                "touched": [n for n in draft.touched if any(x["id"] == n for x in draft.nodes)]}

    stream = _turn_stream(
        agent=ai_flow_agent.flow_agent, message=body.message, history=history, deps=deps,
        model=ai_agent.build_model(body.model), model_id=body.model, user=user, engine=engine,
        ephemeral=False, chat_id=chat_id, locale=body.locale, kind="flow",
        flow_id=flow.id if flow is not None else None, extra_done=changes, request_limit=cfg.max_flow_requests,
    )
    return StreamingResponse(stream, media_type="text/event-stream", headers=_SSE_HEADERS)


# ── the script editor's assistant ────────────────────────────────────────────
class ScriptFileIn(BaseModel):
    path: str = Field(min_length=1, max_length=900)
    content: str = Field(max_length=300_000)


class ScriptColumnIn(BaseModel):
    name: str = Field(max_length=300)
    dtype: str = Field(default="", max_length=100)


class ScriptInputIn(BaseModel):
    variable: str = Field(min_length=1, max_length=200)
    columns: list[ScriptColumnIn] = Field(default_factory=list, max_length=2000)


class ScriptAssistRequest(BaseModel):
    message: str = Field(min_length=1, max_length=8000)
    model: str = Field(min_length=1, max_length=200)
    chat_id: Optional[int] = None
    environment_id: int
    files: list[ScriptFileIn] = Field(default_factory=list, max_length=60)
    entrypoint: str = Field(default="", max_length=900)
    # names only: the script's variables, as the editor lists them
    variables: list[str] = Field(default_factory=list, max_length=300)
    # names and types of the inputs' columns: no rows
    inputs: list[ScriptInputIn] = Field(default_factory=list, max_length=50)
    output: Literal["required", "optional", "none"] = "optional"
    saved: bool = False
    # the last run's log, when it failed: only its error part reaches the model
    run_log: str = Field(default="", max_length=300_000)
    run_exit_code: Optional[int] = None
    locale: str = Field(default="en", max_length=10)


@router.post("/script-assist")
async def script_assist(
    body: ScriptAssistRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
) -> StreamingResponse:
    """One turn of the script editor's assistant. Its proposal of files comes back in
    `done.proposal` (null when it proposed nothing): the editor applies it if the person
    wants. No data reaches the model: the code, the environment, the names of the
    variables and of the inputs' columns, and the error of a failed run."""
    from app.models import ScriptEnvironment, ScriptEnvironmentVersion
    from app.services import script_nodes

    ensure_configured()
    cfg = get_settings().ai
    if body.model not in enabled_model_ids(session):
        raise HTTPException(status_code=403, detail="This model is not enabled by the administrator")
    if is_observer_only(session, user):
        raise HTTPException(status_code=403, detail="Observers do not write scripts")
    env = session.get(ScriptEnvironment, body.environment_id)
    if env is None or not script_nodes.can_use(session, user, env):
        raise HTTPException(status_code=404, detail="Environment not found")
    if sum(len(f.content) for f in body.files) > script_nodes.MAX_CODE_BYTES:
        raise HTTPException(status_code=422, detail="The code is too large for the assistant")
    _check_spending(session, user)
    version = session.get(ScriptEnvironmentVersion, env.current_version_id) if env.current_version_id else None
    if version is None:  # never built: the latest declared
        version = session.exec(select(ScriptEnvironmentVersion).where(ScriptEnvironmentVersion.environment_id == env.id)
                               .order_by(ScriptEnvironmentVersion.number.desc())).first()
    error = ""
    if body.run_log or body.run_exit_code is not None:
        error = ai_script_agent.error_excerpt(body.run_log, env.language)
        if not error and body.run_exit_code is not None:
            error = f"(no error message in the log; the process ended with exit code {body.run_exit_code})"
    chat = ai_chats.open_session(session, user, body.chat_id, body.model, None, kind="script") if body.chat_id else None
    chat_id = chat.id if chat is not None else None
    history = ai_chats.history(session, chat, cfg.max_history_messages) if chat is not None else []
    deps = ai_script_agent.ScriptDeps(
        user=user, session_factory=_new_session, engine=None, model_id=body.model, locale=body.locale,
        request=request, chat_id=chat_id, language=env.language,
        language_version=version.language_version if version else "", environment=env.name,
        packages=[p.strip() for p in (version.packages if version else "").splitlines() if p.strip()],
        variables=[v for v in body.variables if v], files=[f.model_dump() for f in body.files],
        entrypoint=body.entrypoint, inputs=[i.model_dump() for i in body.inputs], output=body.output,
        saved=body.saved, error=error, prompt=ai_prompts.text(session, "script"),
    )
    stream = _turn_stream(
        agent=ai_script_agent.script_agent, message=body.message, history=history, deps=deps,
        model=ai_agent.build_model(body.model), model_id=body.model, user=user, engine=None,
        ephemeral=False, chat_id=chat_id, locale=body.locale, kind="script",
        extra_done=lambda: {"proposal": deps.proposal},
    )
    return StreamingResponse(stream, media_type="text/event-stream", headers=_SSE_HEADERS)


@router.get("/flow-chats/latest")
def latest_flow_chat(flow_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)):
    """The user's latest conversation with the flow editor's assistant about this
    flow, to show again when the editor opens it; null when there is none."""
    if is_observer_only(session, user):
        return None
    chat = ai_chats.latest_flow_chat(session, user, flow_id)
    return ai_chats.detail(session, user, chat.id) if chat is not None else None


# Background tasks must be kept by reference: asyncio keeps only a weak reference to
# them, and without this the title would vanish halfway at the garbage collector's
# discretion.
_in_flight: set = set()


class _NothingToSave(Exception):
    """Not an error: it only says there is no conversation to write for this turn
    (observer). It serves to leave the saving block without going through the `except`
    that logs a fault."""


async def _name_chat(chat_id: int, turn_id: int, model_id: str, question: str, locale: str) -> None:
    """Names the conversation just born, outside the user's turn."""
    try:
        with _new_session() as session:
            prompt = ai_prompts.text(session, "title")
        title, usage = await ai_agent.suggest_title(model_id, question, locale, prompt)
        with _new_session() as session:
            if title:
                ai_chats.rename(session, chat_id, title)
            ai_chats.add_cost(session, turn_id, ai_pricing.cost_of(usage, model_id) if usage else None)
    except Exception:  # noqa: BLE001 — an extra: the conversation keeps the fallback title
        logger.info("assistant: chat %s not renamed", chat_id, exc_info=True)
    finally:
        _in_flight.discard(asyncio.current_task())
