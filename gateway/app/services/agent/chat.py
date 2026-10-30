"""The registry's tools in the chat: the Assistant page's agent (docs/specs/mcp-agent.md).

The Assistant page's agent reads AND builds, with the tools an MCP client has: each tool
of `available("chat", level)` becomes a pydantic-ai tool — `read` for an observer, `write`
otherwise — and a tool's refusal (`ToolError`) a `ModelRetry` the model reads and corrects
itself with. Then:

- a tool that ends the turn (`ask_user`) is the agent's OUTPUT, not a call: the run stops
  there, and its questions go to the page in `done` (`turn_extras`), shown as a card;
- a write tool that touched a plan leaves the plan's id on the turn: `done` carries the
  plan as its page shows it, and the chat shows it as a card with Confirm, Change and
  Cancel. There is no tool to confirm: only the person's click applies a chat's plan
  (`POST /agent/plans/{id}/confirm`);
- the tools that change something run one at a time (`sequential`): two steps on one
  draft never overwrite each other; so do the reading ones that preview on the engine,
  which share the conversation's preview slot — there a newer preview knocks down the
  one running, and two calls of one answer would cancel each other;
- a new conversation is created before its first tool that changes something
  (`_ensure_chat`), so its drafts and its plan belong to it from the start and the next
  turn finds them. A conversation that only reads is created when its first turn ends,
  as before.

A tool's result carries the time it took in its `metadata`, which the model never sees:
the page shows it, also when the conversation is opened again (services/ai_chats.py).

In metadata-only mode (services/metadata_only.py) the tools that read data are not among
those offered, and a turn begun in the mode keeps to it (`metadata_turn`) to its end.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from pydantic import BaseModel
from pydantic_ai import Agent, FunctionToolset, ModelRetry, RunContext, Tool as AiTool, ToolOutput, ToolReturn
from sqlmodel import Session, select

from app.models import AgentPlan, User
from app.services import ai_agent, ai_chats
from app.services.agent import plans, registry
from app.services.agent.common import now
from app.services.agent.registry import REGISTRY, ToolContext, ToolError, UnknownTool

logger = logging.getLogger(__name__)

# failures in a row of one tool before the turn stops: building, the model often needs a
# try or two to get a step's parameters right
RETRIES = 3
MAX_PLANS_IN_CONTEXT = 5
MAX_RESULT_WORDS = 400
# the reading tools that preview on the engine, on the conversation's slot (ai_agent._engine_preview,
# sql_tools._preview_slot): one at a time
_ON_THE_SLOT = {"describe_datasource", "query_datasource", "run_sql_preview"}


def tool_context(deps: ai_agent.ChatDeps) -> ToolContext:
    """The registry's context of this turn: one for the whole turn, so the previews of the
    turn take each other's place, and a conversation created halfway is seen by the rest."""
    if deps.tools is None:
        deps.tools = ToolContext(user=deps.user, session_factory=deps.session_factory, request=deps.request,
                                 origin="chat", chat_id=deps.chat_id, locale=deps.locale, model_id=deps.model_id,
                                 engine=deps.engine, metadata_turn=deps.metadata_turn)
    return deps.tools


def _ensure_chat(deps: ai_agent.ChatDeps) -> None:
    """A new conversation exists before its first draft or plan, which belong to it."""
    if deps.chat_id is not None or deps.ephemeral:
        return
    with deps.session_factory() as session:
        deps.chat_id = ai_chats.create(session, deps.user, deps.question, deps.model_id, deps.engine).id
    tool_context(deps).chat_id = deps.chat_id


def _as_tool(t: registry.Tool, withheld: bool = False) -> AiTool:
    async def run(ctx: RunContext[ai_agent.ChatDeps], /, **arguments: Any) -> ToolReturn:
        deps = ctx.deps
        if t.level == "write":
            _ensure_chat(deps)
        started = time.monotonic()
        try:
            out = await registry.call(tool_context(deps), t.name, arguments, deps.level)
        except ToolError as e:
            raise ModelRetry(e.message) from e
        except UnknownTool as e:
            raise ModelRetry(f"There is no tool {t.name} here.") from e
        except Exception as e:  # noqa: BLE001 — one tool's fault must not end the conversation
            logger.exception("assistant: tool %s failed", t.name)
            raise ModelRetry(f"Internal error in {t.name} ({type(e).__name__}): tell the user it failed; "
                             "do not try it again in this turn.") from e
        if t.level == "write" and isinstance(out, dict) and isinstance(out.get("plan_id"), int):
            deps.plan_id = out["plan_id"]
        return ToolReturn(return_value=out, metadata={"ms": round((time.monotonic() - started) * 1000)})

    return AiTool.from_schema(run, name=t.name, description=t.described(withheld), json_schema=t.input_schema(),
                              takes_ctx=True, sequential=t.level == "write" or t.name in _ON_THE_SLOT)


# the tools that end the turn: the agent's outputs, beside its text
_ENDING = [t for t in REGISTRY.values() if t.ends_turn and t.only in (None, "chat")]

agent: Agent[ai_agent.ChatDeps, Any] = Agent(
    deps_type=ai_agent.ChatDeps,
    instructions=ai_agent._instructions,
    output_type=[str, *(ToolOutput(t.args, name=t.name, description=t.description) for t in _ENDING)],
    retries=RETRIES,
    name="tabularia-assistant",
    capabilities=[ai_agent.StopWhenWithheld()],
)


@agent.toolset(per_run_step=False)
def _registry_tools(ctx: RunContext[ai_agent.ChatDeps]) -> FunctionToolset:
    """The registry's tools for this person, read when the turn starts."""
    withheld = ctx.deps.withholds_data()
    return FunctionToolset([_as_tool(t, withheld) for t in registry.available("chat", ctx.deps.level, withheld)
                            if not t.ends_turn])


@agent.output_validator
async def _ended(ctx: RunContext[ai_agent.ChatDeps], output: Any) -> Any:
    """A turn ended by ask_user: its handler checks the questions, the page gets them."""
    if isinstance(output, BaseModel) and not ctx.partial_output:
        t = next((t for t in _ENDING if isinstance(output, t.args)), None)
        if t is not None:
            try:
                ended = await registry.call(tool_context(ctx.deps), t.name, output.model_dump(), ctx.deps.level)
            except ToolError as e:
                raise ModelRetry(e.message) from e
            ctx.deps.questions = ended.get("questions")
    return output


def request_limit(level: str, max_requests: int, max_flow_requests: int) -> int:
    """The model's rounds in a turn: building takes one per step it makes and checks, as in
    the flow editor; only reading, the data chat's."""
    return max(max_requests, max_flow_requests) if level == "write" else max_requests


# ── what the page gets ────────────────────────────────────────────────────────
def _current_status(session: Session, plan: AgentPlan) -> None:
    """A pending plan past its expiry is expired (as its page says)."""
    if plan.status == "pending" and plan.expires_at <= now():
        plan.status = "expired"
        session.add(plan)
        session.commit()
        session.refresh(plan)


def plan_cards(session: Session, user: User, plan_ids: set[int]) -> dict[int, dict]:
    """The person's plans as the chat's cards show them (the plan's page's view), by id."""
    from app.routes.agent_plans import plan_out

    if not plan_ids:
        return {}
    rows = session.exec(select(AgentPlan).where(AgentPlan.id.in_(plan_ids),  # type: ignore[union-attr]
                                                AgentPlan.user_id == user.id)).all()
    out = {}
    for plan in rows:
        _current_status(session, plan)
        out[plan.id] = plan_out(plan).model_dump(mode="json")
    return out


def turn_extras(deps: ai_agent.ChatDeps) -> dict[str, Any]:
    """What `done` (or `error`) carries besides the usage: the conversation (created halfway,
    for a new one), the questions of a card, the plan the turn touched."""
    out: dict[str, Any] = {}
    if deps.chat_id is not None:
        out["chat_id"] = deps.chat_id
    if deps.questions:
        out["questions"] = deps.questions
    if deps.plan_id is not None:
        with deps.session_factory() as session:
            card = plan_cards(session, deps.user, {deps.plan_id}).get(deps.plan_id)
        if card is not None:
            out["plan"] = card
    return out


def plans_in_words(session: Session, user: User, chat_id: Optional[int], withheld: bool = False) -> list[str]:
    """The conversation's latest plans as they stand, for the model's context: it does not
    see the person's clicks, and must not take a plan applied for one still waiting.
    `withheld` (metadata-only mode): that an action failed, not in whose words."""
    if chat_id is None:
        return []
    rows = session.exec(
        select(AgentPlan).where(AgentPlan.user_id == user.id, AgentPlan.origin == "chat", AgentPlan.chat_id == chat_id)
        .order_by(AgentPlan.id.desc()).limit(MAX_PLANS_IN_CONTEXT)  # type: ignore[union-attr]
    ).all()
    lines = []
    for plan in reversed(rows):
        status = "expired" if plan.status == "pending" and plan.expires_at <= now() else plan.status
        n = len(plans.actions_of(plan))
        words = f"plan {plan.id}: {status}, {n} action{'s' if n != 1 else ''}"
        if status == "pending":
            words += ", waiting for the user's click: plan tools add to it"
        results = plans.results_of(plan) or []
        if withheld:
            results = [plans.result_without_data(r) for r in results]
        done = [r.get("words") or (f"failed: {r['error']}" if r.get("error") else "") for r in results]
        done = [w for w in done if w]
        if done:
            words += " — " + " ".join(done)[:MAX_RESULT_WORDS]
        lines.append(words)
    return lines
