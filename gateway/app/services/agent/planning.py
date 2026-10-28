"""What the plans of datasources, SQL scripts, flow groups and notebooks share: their
links, the words for a connection that is missing, a route that awaits the engine, the
schedules' cron, an object a plan creates before it has an id, the difference of a text.
"""
from __future__ import annotations

import difflib
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional, TypeVar

from sqlmodel import Session

from app.core.config import get_settings
from app.models import User
from app.services.agent import plans
from app.services.agent.common import absolute
from app.services.agent.registry import ToolContext, ToolError

T = TypeVar("T")

CONNECTIONS_PAGE = "/connections"


# ── links to the interface ────────────────────────────────────────────────────
def sql_script_link(script_id: int) -> str:
    return f"/sql-script?id={script_id}"


def flow_group_link(group_id: int) -> str:
    return f"/flow-group?id={group_id}"


def notebook_link(notebook_id: int) -> str:
    return f"/notebook?id={notebook_id}"


def link_for(ctx: ToolContext, path: str) -> str:
    """A link in a tool's words: whole for an MCP client, the interface's path in the chat."""
    return absolute(ctx.request, path) if ctx.origin == "mcp" else path


def no_connection(ctx: ToolContext, connection_id: Any, what: str) -> ToolError:
    """A connection that does not exist, or one the user may not use (CONNECT on its
    folder): the same words, the agent does not tell them apart."""
    return ToolError(f"No connection {connection_id} is available to this user for {what}: see list_connections (the "
                     "connections whose folder gives the user CONNECT). The agent never creates or changes a connection: "
                     "if the one needed is missing, tell the user to add it, or to ask whoever manages it, on the "
                     f"Connections page: {link_for(ctx, CONNECTIONS_PAGE)}")


# ── running the routes ────────────────────────────────────────────────────────
async def with_session(ctx: ToolContext, work: Callable[[Session, User], Awaitable[T]]) -> T:
    """`await work(session, user)`: for the routes that await the engine, which are
    asynchronous and keep their session meanwhile, as they do for the interface. The user
    is read again in that session, so the permissions are those of now."""
    with ctx.session_factory() as session:
        user = session.get(User, ctx.user.id) or ctx.user
        return await work(session, user)


# ── schedules ─────────────────────────────────────────────────────────────────
def checked_cron(raw: Optional[str]) -> str:
    """The cron as it will be saved, "" to turn the schedule off."""
    from app.services.schedule import ScheduleError, validate_schedule

    cron = (raw or "").strip()
    if not cron:
        return ""
    try:
        return validate_schedule(cron)
    except ScheduleError as e:
        raise ToolError(str(e)) from e


def cron_words(cron: str) -> str:
    from app.services.schedule import next_fire

    nxt = next_fire(cron, datetime.now(timezone.utc))
    return f"cron `{cron}` in {get_settings().app.timezone} (next run {nxt:%Y-%m-%d %H:%M} UTC)"


def flows_only(a: Any) -> None:
    if a.notify_emails is not None or a.notify_connection_id is not None:
        raise ToolError("notify_emails and notify_connection_id are for flows only.")


# ── an object the plan creates ────────────────────────────────────────────────
def planned_creation(ctx: ToolContext, n: int, kind: str, what: str) -> dict:
    """Action `n` of the caller's pending plan, when it creates a `what` (an action of
    `kind` without an id)."""
    with ctx.session_factory() as session:
        plan = plans._current_plan(session, ctx)
        actions = plans.actions_of(plan) if plan is not None else []
    action = actions[n - 1] if 0 < n <= len(actions) else None
    if action is None or action.get("kind") != kind or action.get("id") is not None:
        raise ToolError(f"Action {n} of the pending plan does not create a {what}: give the saved {what}'s id, or "
                        "plan its creation first and give the number (n) of that action.")
    return action


def made_by(results: list[dict], n: int, what: str) -> int:
    """At apply time: the id of what action `n` of the plan created."""
    r = results[n - 1] if 0 < n <= len(results) else None
    if not r or not r.get("ok") or not (r.get("object") or {}).get("id"):
        raise ToolError(f"The {what} to create first was not created.")
    return r["object"]["id"]


def planned_save(ctx: ToolContext, kind: str, object_id: Optional[int]) -> Optional[dict]:
    """The pending plan's change of a saved object, when there is one."""
    return plans.planned(ctx, kind, id=object_id) if object_id is not None else None


# ── texts ─────────────────────────────────────────────────────────────────────
def text_diff(old: str, new: str) -> list[str]:
    """What changes in a text, line by line: «-» removed, «+» added, one line of context.
    Whole: the person confirms what will run, and no part of it may stay out of sight (the
    texts are capped already: a SQL script's, a notebook's source). The model reads its
    first lines (plans.plan_view)."""
    return list(difflib.unified_diff((old or "").splitlines(), (new or "").splitlines(), lineterm="", n=1))[2:]


def change_lines(changes: list[tuple[str, Any, Any]]) -> list[str]:
    """«~ name: a → b» for the settings that change."""
    return [f"~ {what}: {before if before not in (None, '') else '—'} → {after if after not in (None, '') else '—'}"
            for what, before, after in changes if before != after]
