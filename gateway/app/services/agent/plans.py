"""The agent's plans: what it proposes to change, applied only once a person agrees.

A write tool never changes a saved object. It checks what it is asked with the person's
permissions, adds an ACTION to the pending plan (the caller's: of this chat, or of this
token over MCP) and returns the plan in words, with its id and the link to its page.
Applying it — the chat's «Confirm» button, `POST /agent/plans/{id}/confirm`, or
`confirm_plan` over MCP — is the same code (`apply_plan`):

- only the plan's owner, only while pending and not expired (30 minutes from its last
  change); a plan is claimed with a conditional UPDATE (pending → applying), so a second
  click or another replica never applies it again;
- each action is checked AGAIN and applied in order through the routes the interface
  uses, so every rule holds (permissions, schedules' authority, allowed domains, audit,
  with `via` and the plan's id); the first failure stops the rest;
- each action's outcome is kept on the plan, with the link to what it made.

The kinds of action are a registry (`action_kind`): datasources, SQL scripts, flow groups
and notebooks add theirs next to the flows' (services/agent/flow_tools.py).
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import update
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import AgentPlan
from app.services import audit
from app.services.agent.common import absolute, now, plan_link
from app.services.agent.registry import ToolContext, ToolError, tool

logger = logging.getLogger(__name__)

PLAN_TTL = timedelta(minutes=30)
MAX_ACTIONS = 20
# a plan left `applying` by a process that stopped half way: closed as failed after this
APPLYING_STUCK_AFTER = timedelta(hours=1)


# ── kinds of action ───────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ActionKind:
    kind: str
    # (ctx, the action, the outcomes of the actions before it) → {object?, link?, words?}
    apply: Callable[[ToolContext, dict, list[dict]], Awaitable[dict]]


ACTIONS: dict[str, ActionKind] = {}


def action_kind(kind: str):
    def register(fn):
        ACTIONS[kind] = ActionKind(kind, fn)
        return fn

    return register


# ── reading and writing plans ─────────────────────────────────────────────────
def own_plan(session: Session, ctx: ToolContext, plan_id: int) -> AgentPlan:
    """The plan, when it is this person's (anyone else's is as if it did not exist).
    A pending plan past its expiry is marked expired here."""
    plan = session.get(AgentPlan, plan_id)
    if plan is None or plan.user_id != ctx.user.id:
        raise ToolError(f"There is no plan {plan_id} of this user.")
    if plan.status == "pending" and plan.expires_at <= now():
        plan.status = "expired"
        session.add(plan)
        session.commit()
        session.refresh(plan)
    return plan


def actions_of(plan: AgentPlan) -> list[dict]:
    try:
        out = json.loads(plan.actions or "[]")
    except ValueError:
        return []
    return out if isinstance(out, list) else []


def results_of(plan: AgentPlan) -> Optional[list[dict]]:
    if not plan.result:
        return None
    try:
        return json.loads(plan.result)
    except ValueError:
        return None


def _summary(actions: list[dict]) -> str:
    return "\n".join(f"{i}. {a.get('words', a.get('kind'))}" for i, a in enumerate(actions, 1))


def _next_step(ctx: ToolContext, plan: AgentPlan) -> str:
    if plan.status != "pending":
        return ""
    if plan.origin == "chat":
        return "Nothing has changed yet: the user confirms the plan with the button of its card."
    if get_settings().mcp.confirm_in_tabularia_only:
        return ("Nothing has changed yet. Show the plan to the user: on this installation they confirm it themselves "
                "in Tabularia, at review_link.")
    return ("Nothing has changed yet. Show the plan to the user in your own words; once they agree, call "
            "confirm_plan with this plan_id (or they confirm it in Tabularia, at review_link).")


def plan_view(ctx: ToolContext, plan: AgentPlan) -> dict:
    """The plan as a tool returns it."""
    actions = actions_of(plan)
    out: dict[str, Any] = {
        "plan_id": plan.id, "status": plan.status,
        "actions": [{"n": i, "kind": a.get("kind"), "words": a.get("words", "")} for i, a in enumerate(actions, 1)],
        "summary": plan.summary,
        "review_link": absolute(ctx.request, plan_link(plan.id)) if ctx.origin == "mcp" else plan_link(plan.id),
    }
    if plan.status == "pending":
        out["expires_at"] = plan.expires_at.isoformat() + "Z"
    for a, view in zip(actions, out["actions"]):
        if a.get("diff_lines"):
            view["changes"] = a["diff_lines"]
    results = results_of(plan)
    if results is not None:
        out["result"] = [
            {**r, **({"link": absolute(ctx.request, r["link"])} if r.get("link") and ctx.origin == "mcp" else {})}
            for r in results
        ]
    hint = _next_step(ctx, plan)
    if hint:
        out["next"] = hint
    return out


def _current_plan(session: Session, ctx: ToolContext) -> Optional[AgentPlan]:
    """The caller's pending plan: of this chat, or of this token over MCP."""
    q = select(AgentPlan).where(AgentPlan.user_id == ctx.user.id, AgentPlan.origin == ctx.origin,
                                AgentPlan.status == "pending", AgentPlan.expires_at > now())
    q = q.where(AgentPlan.chat_id == ctx.chat_id) if ctx.origin == "chat" else q.where(AgentPlan.token_name == ctx.token_name)
    return session.exec(q.order_by(AgentPlan.id.desc())).first()  # type: ignore[union-attr]


def add_action(ctx: ToolContext, action: dict, key: str) -> dict:
    """Adds `action` ({kind, words, …}) to the caller's pending plan (a new one when there
    is none); an action with the same `key` (the same object) is replaced in its place.
    Each change gives the person another 30 minutes to look at it."""
    if action.get("kind") not in ACTIONS:
        raise RuntimeError(f"unknown action kind {action.get('kind')}")
    action = {**action, "key": key}
    with ctx.session_factory() as session:
        plan = _current_plan(session, ctx)
        if plan is None:
            plan = AgentPlan(user_id=ctx.user.id, origin=ctx.origin, chat_id=ctx.chat_id, token_name=ctx.token_name)
        actions = actions_of(plan)
        at = next((i for i, a in enumerate(actions) if a.get("key") == key), None)
        if at is not None:
            actions[at] = action
        elif len(actions) >= MAX_ACTIONS:
            raise ToolError(f"A plan holds at most {MAX_ACTIONS} actions: have this one confirmed first.")
        else:
            actions.append(action)
        plan.actions = json.dumps(actions, ensure_ascii=False, default=str)
        plan.summary = _summary(actions)
        plan.expires_at = now() + PLAN_TTL
        session.add(plan)
        session.commit()
        session.refresh(plan)
        return plan_view(ctx, plan)


def planned(ctx: ToolContext, kind: str, **match: Any) -> Optional[dict]:
    """An action of the caller's pending plan, of `kind`, whose arguments match."""
    with ctx.session_factory() as session:
        plan = _current_plan(session, ctx)
        actions = actions_of(plan) if plan is not None else []
    return next((a for a in actions if a.get("kind") == kind and all(a.get(k) == v for k, v in match.items())), None)


# ── applying ──────────────────────────────────────────────────────────────────
def _words_of(e: Exception) -> str:
    if isinstance(e, ToolError):
        return e.message
    if isinstance(e, HTTPException):
        return e.detail if isinstance(e.detail, str) else json.dumps(e.detail, default=str)
    return str(e) or type(e).__name__


async def apply_plan(ctx: ToolContext, plan_id: int, only_origin: Optional[str] = None) -> dict:
    """Applies the plan, once, for its owner. Returns the plan's view with the outcome
    of each action. `only_origin`: confirm_plan applies only the plans made over MCP (a
    chat's plan waits for the person's click)."""
    with ctx.session_factory() as session:
        plan = own_plan(session, ctx, plan_id)
        if only_origin and plan.origin != only_origin:
            raise ToolError(f"Plan {plan_id} was made in Tabularia's chat: the user confirms it there.")
        if plan.status != "pending":
            raise ToolError(f"Plan {plan_id} is {plan.status}: it cannot be applied. Make a new plan if needed.")
        claimed = session.exec(
            update(AgentPlan)
            .where(AgentPlan.id == plan_id, AgentPlan.status == "pending", AgentPlan.expires_at > now())
            .values(status="applying")
        ).rowcount
        session.commit()
        if claimed != 1:
            raise ToolError(f"Plan {plan_id} is being applied or no longer pending.")
        actions = actions_of(plan)
        origin, token_name = plan.origin, plan.token_name

    state = getattr(ctx.request, "state", None) if ctx.request is not None else None
    before = (getattr(state, "via", None), getattr(state, "agent_plan", None)) if state is not None else None
    if state is not None:
        state.via = origin  # every route the actions call records it (services/audit.py)
        state.agent_plan = plan_id
    results: list[dict] = []
    failed = False
    try:
        for action in actions:
            if failed:
                results.append({"ok": False, "skipped": True, "error": "Not applied: an earlier action failed."})
                continue
            kind = ACTIONS.get(action.get("kind"))
            try:
                if kind is None:
                    raise ToolError(f"Unknown action {action.get('kind')}.")
                outcome = await kind.apply(ctx, action, results)
                results.append({"ok": True, **outcome})
            except (ToolError, HTTPException) as e:
                failed = True
                results.append({"ok": False, "error": _words_of(e)})
            except Exception as e:  # noqa: BLE001 — recorded on the plan; the rest stops
                logger.exception("agent plan %s: action %s failed", plan_id, action.get("kind"))
                failed = True
                results.append({"ok": False, "error": f"Internal error ({type(e).__name__}): see the gateway's log."})
    finally:
        with ctx.session_factory() as session:
            plan = session.get(AgentPlan, plan_id)
            plan.status = "failed" if failed or len(results) < len(actions) else "applied"
            plan.result = json.dumps(results, ensure_ascii=False, default=str)
            plan.applied_at = now()
            session.add(plan)
            session.commit()
            session.refresh(plan)
            audit.record_audit(
                session, actor=ctx.user, action=audit.AGENT_PLAN_APPLY,
                outcome="failure" if plan.status == "failed" else "success",
                target_type="agent_plan", target_id=plan_id, target_label=f"plan {plan_id}",
                detail={"origin": origin, **({"plan_token": token_name} if token_name else {}),
                        "actions": [a.get("kind") for a in actions], "ok": [r.get("ok") for r in results]},
                request=ctx.request,
            )
            view = plan_view(ctx, plan)
        if state is not None and before is not None:
            state.via, state.agent_plan = before
    return view


def cancel_plan_of(ctx: ToolContext, plan_id: int) -> dict:
    with ctx.session_factory() as session:
        plan = own_plan(session, ctx, plan_id)
        if plan.status != "pending":
            raise ToolError(f"Plan {plan_id} is {plan.status}: only a pending plan can be cancelled.")
        claimed = session.exec(
            update(AgentPlan).where(AgentPlan.id == plan_id, AgentPlan.status == "pending").values(status="cancelled")
        ).rowcount
        session.commit()
        if claimed != 1:
            raise ToolError(f"Plan {plan_id} is no longer pending.")
        session.refresh(plan)
        audit.record_audit(session, actor=ctx.user, action=audit.AGENT_PLAN_CANCEL, target_type="agent_plan",
                           target_id=plan_id, target_label=f"plan {plan_id}", detail={"origin": plan.origin},
                           request=ctx.request)
        return plan_view(ctx, plan)


# ── housekeeping ──────────────────────────────────────────────────────────────
def expire_and_trim(session: Session, at=None, retention_days: int = 0) -> int:
    """Pending plans past their expiry become expired; plans left applying by a process
    that stopped become failed; plans no longer pending older than `retention_days`
    (0 = kept) go. Returns how many rows changed."""
    at = at or now()
    n = session.exec(update(AgentPlan).where(AgentPlan.status == "pending", AgentPlan.expires_at <= at)
                     .values(status="expired")).rowcount or 0
    n += session.exec(update(AgentPlan).where(AgentPlan.status == "applying",
                                              AgentPlan.expires_at <= at - APPLYING_STUCK_AFTER)
                      .values(status="failed", applied_at=at,
                              result=json.dumps([{"ok": False, "error": "Interrupted while being applied."}]))).rowcount or 0
    if retention_days > 0:
        from sqlalchemy import delete

        n += session.exec(delete(AgentPlan).where(AgentPlan.status.notin_(("pending", "applying")),  # type: ignore[attr-defined]
                                                  AgentPlan.created_at < at - timedelta(days=retention_days))).rowcount or 0
    session.commit()
    return n


# ── the tools ─────────────────────────────────────────────────────────────────
class PlanIdArgs(BaseModel):
    plan_id: int = Field(description="The id a plan tool returned.")


@tool("get_plan", title="Read a plan", args=PlanIdArgs, level="read")
async def get_plan(ctx: ToolContext, a: PlanIdArgs) -> dict:
    """A plan of this user: its actions in words, its state (pending, applied, failed, cancelled, expired) and, once applied, the outcome of each action with the link to what it made."""
    with ctx.session_factory() as session:
        return plan_view(ctx, own_plan(session, ctx, a.plan_id))


@tool("cancel_plan", title="Cancel a plan", args=PlanIdArgs, level="write")
async def cancel_plan(ctx: ToolContext, a: PlanIdArgs) -> dict:
    """Cancel a pending plan: nothing of it will be applied. The drafts stay, to plan again."""
    return cancel_plan_of(ctx, a.plan_id)


@tool("confirm_plan", title="Apply a plan", args=PlanIdArgs, level="write", only="mcp", destructive=True)
async def confirm_plan(ctx: ToolContext, a: PlanIdArgs) -> dict:
    """Apply a pending plan: create, change, run and schedule what it lists, in order, with the user's permissions checked again. Call it ONLY after the user has seen the plan and agreed to it. The first action that fails stops the rest; the result says what was done, with links."""
    if get_settings().mcp.confirm_in_tabularia_only:
        raise ToolError("On this installation a plan is confirmed only in Tabularia, by the user: "
                        f"ask them to open {absolute(ctx.request, plan_link(a.plan_id))} and confirm it there.")
    return await apply_plan(ctx, a.plan_id, only_origin="mcp")


# ── schedules: one tool, a planner per kind of object ───────────────────────
# kind → (ctx, the tool's arguments) → the plan's view. The flows' is in flow_tools.py;
# datasources, SQL scripts and flow groups add theirs.
SCHEDULERS: dict[str, Callable[[ToolContext, Any], Awaitable[dict]]] = {}


def scheduler(kind: str):
    def register(fn):
        SCHEDULERS[kind] = fn
        return fn

    return register


class PlanScheduleArgs(BaseModel):
    kind: str = Field("flow", description="What to schedule: flow.")
    id: Optional[int] = Field(None, description="The saved object's id.")
    draft_id: Optional[int] = Field(None, description="flow: a draft whose save is in the plan already (plan_save_flow).")
    cron: Optional[str] = Field(None, max_length=100, description="5-field cron in the installation's time zone, e.g. '0 7 * * 1-5' (07:00 on weekdays); null or empty turns the schedule off.")
    notify_emails: Optional[str] = Field(None, max_length=2000, description="flow: addresses told when a scheduled run fails, comma-separated (with notify_connection_id).")
    notify_connection_id: Optional[int] = Field(None, description="flow: the SMTP connection that sends the failure notice.")


@tool("plan_schedule", title="Plan a schedule", args=PlanScheduleArgs, level="write")
async def plan_schedule(ctx: ToolContext, a: PlanScheduleArgs) -> dict:
    """Add to the plan: schedule an object to run by itself (cron), or turn its schedule off (cron null). The scheduled runs carry the authority of the user who schedules. Nothing changes until the plan is confirmed."""
    planner = SCHEDULERS.get(a.kind)
    if planner is None:
        raise ToolError(f"Unknown kind {a.kind}: one of {', '.join(sorted(SCHEDULERS))}.")
    return await planner(ctx, a)
