"""The agent's plans in Tabularia: the page that shows one and the person's confirmation.

- `GET  /agent/plans/{id}`          the plan in words: its actions (with the differences of
                                     a changed flow and the first rows), state, outcome
- `POST /agent/plans/{id}/confirm`  apply it (the chat's button, the plan's page)
- `POST /agent/plans/{id}/cancel`   cancel it

A plan is its owner's only: anyone else gets a 404, administrators too. Confirming here
runs the same code as `confirm_plan` over MCP (services/agent/plans.py), for the plan as
the person saw it: the page and the chat's card send back its `revision`, and a plan the
agent changed since is refused (409) until it is looked at again. A plan made in the chat
is confirmed by a person signed in, never with an API token; with
MCP__CONFIRM_IN_TABULARIA_ONLY on, neither is one made over MCP.
"""
from __future__ import annotations

from contextlib import nullcontext
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlmodel import Session

from app.core.config import get_settings
from app.core.routing import ReleasingRoute
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.models import AgentPlan, User
from app.schemas.models import UtcDateTime
from app.services.agent import common, plans
from app.services.agent.registry import ToolContext, ToolError

router = APIRouter(route_class=ReleasingRoute, prefix="/agent", tags=["agent"])

# what an action carries for the applying, not for the person reading it
_INTERNAL = {"definition", "base_hash", "key", "diff"}


class PlanOut(BaseModel):
    id: int
    origin: str
    token_name: Optional[str] = None
    chat_id: Optional[int] = None
    status: str
    summary: str
    # [{n, kind, words, name?, folder?, engine?, mode?, cron?, timezone?, diff_lines?,
    #   node_lines?, outputs?, preview?}]
    actions: list[dict[str, Any]]
    # once applied: [{ok, words?, link?, object?, error?, skipped?}], one per action
    result: Optional[list[dict[str, Any]]] = None
    created_at: UtcDateTime
    expires_at: UtcDateTime
    applied_at: Optional[UtcDateTime] = None
    # the person may confirm or cancel it now
    can_confirm: bool
    # the actions as shown: confirming sends it back, and a plan changed since is not applied
    revision: str


class ConfirmIn(BaseModel):
    # the plan's revision the person saw; missing (a script with a token) = the plan as it is
    revision: Optional[str] = Field(None, max_length=64)


def _ctx(user: User, request: Request, plan: AgentPlan, session_factory=None) -> ToolContext:
    return ToolContext(user=user, session_factory=session_factory or (lambda: common.new_session()), request=request,
                       origin=plan.origin if plan.origin in ("chat", "mcp") else "chat",  # type: ignore[arg-type]
                       token_name=getattr(request.state, "api_token", None), chat_id=plan.chat_id)


def _own(session: Session, user: User, plan_id: int) -> AgentPlan:
    plan = session.get(AgentPlan, plan_id)
    if plan is None or plan.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plan not found")
    if plan.status == "pending" and plan.expires_at <= common.now():
        plan.status = "expired"
        session.add(plan)
        session.commit()
        session.refresh(plan)
    return plan


def plan_out(plan: AgentPlan) -> PlanOut:
    """The plan as its page and the chat's card show it (services/agent/chat.py, ai_chats.py)."""
    actions = [{"n": i, **{k: v for k, v in a.items() if k not in _INTERNAL}}
               for i, a in enumerate(plans.actions_of(plan), 1)]
    return PlanOut(id=plan.id, origin=plan.origin, token_name=plan.token_name, chat_id=plan.chat_id, status=plan.status,
                   summary=plan.summary, actions=actions, result=plans.results_of(plan), created_at=plan.created_at,
                   expires_at=plan.expires_at, applied_at=plan.applied_at, can_confirm=plan.status == "pending",
                   revision=plans.revision(plan))


@router.get("/plans/{plan_id}", response_model=PlanOut)
def read_plan(plan_id: int, user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> PlanOut:
    return plan_out(_own(session, user, plan_id))


@router.post("/plans/{plan_id}/confirm", response_model=PlanOut)
async def confirm_plan(plan_id: int, request: Request, body: Optional[ConfirmIn] = None,
                       user: User = Depends(get_current_user), session: Session = Depends(get_session)) -> PlanOut:
    with_token = bool(getattr(request.state, "api_token", None))
    if with_token and get_settings().mcp.confirm_in_tabularia_only:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="On this installation a plan is confirmed by a person signed in to Tabularia, "
                                   "from the plan's page: not with an API token")
    plan = _own(session, user, plan_id)
    if with_token and plan.origin == "chat":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                            detail="A plan made in the chat is confirmed by the person, from its card or its page: "
                                   "not with an API token")
    ctx = _ctx(user, request, plan)
    user.id  # noqa: B018 — loaded now: the actions read it without this session
    session.close()  # the actions take their own connections, and wait for the engine
    try:
        await plans.apply_plan(ctx, plan_id, seen=body.revision if body is not None else None)
    except ToolError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e
    with common.new_session() as fresh:
        return plan_out(fresh.get(AgentPlan, plan_id))


@router.post("/plans/{plan_id}/cancel", response_model=PlanOut)
def cancel_plan(plan_id: int, request: Request, user: User = Depends(get_current_user),
                session: Session = Depends(get_session)) -> PlanOut:
    plan = _own(session, user, plan_id)
    try:
        plans.cancel_plan_of(_ctx(user, request, plan, lambda: nullcontext(session)), plan_id)
    except ToolError as e:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=e.message) from e
    session.refresh(plan)
    return plan_out(plan)

