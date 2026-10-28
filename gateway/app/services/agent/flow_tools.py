"""The agent's flows: drafts to build or change one, and the flows' actions of a plan.

A DRAFT is a server-side copy of a flow being built (or of a saved flow being changed),
kept in `agent_drafts`: nothing saved changes while the agent works on it. Its tools are
the flow editor's assistant's (services/ai_flow_agent.py) — the same parameters' checks,
the same preview checked as the editor's — on the draft instead of the editor's canvas,
plus every kind of Output with the checks a run makes (services/agent/outputs.py).

Saving, running and scheduling are ACTIONS of a plan (services/agent/plans.py), applied
through the routes the interface uses once the person agrees.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Literal, Optional

from pydantic import BaseModel, Field
from pydantic_ai import ModelRetry
from sqlalchemy import delete
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import AgentDraft, Flow, Project
from app.models.permission import Capability
from app.services import ai_flow_agent
from app.services import permissions as perm_service
from app.services.agent import flow_diff, plans
from app.services.agent.common import fingerprint, flow_link, folder_paths, in_thread, now
from app.services.agent.outputs import OutputSettings, check_output, node_data
from app.services.agent.read_tools import describe_flow
from app.services.agent.registry import ToolContext, ToolError, tool

DRAFT_TTL = timedelta(hours=24)
MAX_DRAFTS = 20  # per person: opening one more lets the least recently used go
PLAN_PREVIEW_ROWS = 5

# the flow assistant's words name the editor's canvas and tools: here, the draft's
_IN_DRAFT_WORDS = (
    ("Call get_flow to see the nodes", "Call flow_draft_show to see the nodes"),
    ("on the canvas", "in the draft"),
    ("connect(..., input='right')", "flow_connect(..., input='right')"),
)


def _in_draft_words(message: str) -> str:
    for old, new in _IN_DRAFT_WORDS:
        message = message.replace(old, new)
    return message


# ── drafts ────────────────────────────────────────────────────────────────────
def own_draft(session: Session, ctx: ToolContext, draft_id: int) -> AgentDraft:
    d = session.get(AgentDraft, draft_id)
    if d is None or d.user_id != ctx.user.id or d.updated_at <= now() - DRAFT_TTL:
        raise ToolError(f"There is no draft {draft_id} of this user (a draft goes after a day unused): "
                        "start one with flow_draft_new or flow_draft_open.")
    return d


def _new_draft(session: Session, ctx: ToolContext, **fields: Any) -> AgentDraft:
    kept = session.exec(select(AgentDraft.id).where(AgentDraft.user_id == ctx.user.id)
                        .order_by(AgentDraft.updated_at.desc())).all()  # type: ignore[union-attr]
    if len(kept) >= MAX_DRAFTS:
        session.exec(delete(AgentDraft).where(AgentDraft.id.in_(kept[MAX_DRAFTS - 1:])))  # type: ignore[union-attr]
    d = AgentDraft(user_id=ctx.user.id, origin=ctx.origin, chat_id=ctx.chat_id if ctx.origin == "chat" else None,
                   token_name=ctx.token_name, **fields)
    session.add(d)
    session.commit()
    session.refresh(d)
    return d


def expired_drafts(session: Session, at: Optional[datetime] = None) -> int:
    """Deletes the drafts unused for a day (services/housekeeping.py)."""
    n = session.exec(delete(AgentDraft).where(AgentDraft.updated_at <= (at or now()) - DRAFT_TTL)).rowcount or 0
    session.commit()
    return n


def _snapshot(d: AgentDraft) -> dict[str, Any]:
    return {k: getattr(d, k) for k in ("id", "flow_id", "base_hash", "name", "project_id", "engine", "definition", "counters")}


def _flow_draft(snap: dict) -> ai_flow_agent.FlowDraft:
    try:
        return ai_flow_agent.FlowDraft(json.loads(snap["definition"] or "{}"), json.loads(snap["counters"] or "{}"))
    except (ValueError, TypeError) as e:
        raise ToolError(f"Draft {snap['id']} is not readable ({e}): start a new one.") from e


def _deps(ctx: ToolContext, snap: dict, draft: ai_flow_agent.FlowDraft) -> ai_flow_agent.FlowDeps:
    """The flow assistant's dependencies, on the draft."""
    return ai_flow_agent.FlowDeps(
        user=ctx.user, session_factory=ctx.session_factory, engine=snap["engine"], model_id=ctx.model_id,
        locale=ctx.locale, request=ctx.request, chat_id=ctx.chat_id if ctx.origin == "chat" else None,
        draft=draft, flow_id=snap["flow_id"], flow_name=snap["name"], project_id=snap["project_id"],
        preview_key=ctx.call_key,
    )


async def _with_draft(ctx: ToolContext, draft_id: int,
                      change: Callable[[ai_flow_agent.FlowDeps], Awaitable[dict]]) -> dict:
    """Runs `change` on the draft and keeps what it did."""
    with ctx.session_factory() as session:
        snap = _snapshot(own_draft(session, ctx, draft_id))
    draft = _flow_draft(snap)
    floors = dict(draft.floors)
    try:
        result = await change(_deps(ctx, snap, draft))
    except ModelRetry as e:
        raise ToolError(_in_draft_words(e.message)) from e
    text = json.dumps(draft.definition(), default=str)
    if len(text) > ai_flow_agent.MAX_DEFINITION_CHARS:
        raise ToolError("The flow would be too large: the change is not kept.")
    with ctx.session_factory() as session:
        d = session.get(AgentDraft, draft_id)
        if d is not None:
            if draft.changed() or draft.floors != floors:
                d.definition, d.counters = text, json.dumps(draft.floors)
            d.updated_at = now()
            session.add(d)
            session.commit()
    return {"draft_id": draft_id, **result}


def _ctx_of(deps: ai_flow_agent.FlowDeps) -> SimpleNamespace:
    """The flow assistant's tools take pydantic-ai's RunContext: only `deps` is read."""
    return SimpleNamespace(deps=deps)


class DraftArgs(BaseModel):
    draft_id: int = Field(description="The draft's id (flow_draft_new, flow_draft_open).")


class DraftNewArgs(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    folder_id: int = Field(description="Where the flow will be saved (list_folders: can_edit).")
    engine: Optional[str] = Field(None, max_length=20, description="polars (default), duckdb, chdb, clickhouse or bigquery, among those the administrator allows.")


@tool("flow_draft_new", title="Start a new flow", args=DraftNewArgs, level="write")
async def flow_draft_new(ctx: ToolContext, a: DraftNewArgs) -> dict:
    """Start a draft of a NEW flow. Nothing is saved: add sources, steps and Outputs to the draft, check them with flow_preview, then plan_save_flow puts its creation in the plan the user confirms."""
    from app.routes.flows import _check_flow_name, _validate_engine

    def work(session, user):
        if session.get(Project, a.folder_id) is None \
                or not perm_service.has_capability(session, user, a.folder_id, Capability.EDIT):
            raise ToolError(f"The user cannot create flows in folder {a.folder_id}: see list_folders (can_edit).")
        name = a.name.strip()
        _check_flow_name(name)
        engine = _validate_engine(a.engine, session)
        d = _new_draft(session, ctx, name=name, project_id=a.folder_id, engine=engine)
        return {"draft_id": d.id, "name": d.name, "folder": folder_paths(session).get(a.folder_id, ""), "engine": engine,
                "nodes": [], "next": "Add sources (flow_add_source), steps (flow_add_step) and an Output (flow_add_output), "
                                     "check them with flow_preview, then plan_save_flow."}

    return await in_thread(ctx, work)


class DraftOpenArgs(BaseModel):
    flow_id: int


@tool("flow_draft_open", title="Open a flow to change it", args=DraftOpenArgs, level="write")
async def flow_draft_open(ctx: ToolContext, a: DraftOpenArgs) -> dict:
    """Open a draft of a SAVED flow, to change it: the flow itself does not change until the plan with its save (plan_save_flow) is confirmed, and the plan shows the difference. Returns the draft id and the flow's nodes."""
    from app.routes import flows as flows_routes

    def work(session, user):
        flow = flows_routes.get_flow(a.flow_id, user=user, session=session)
        if not perm_service.has_capability(session, user, flow.project_id, Capability.EDIT):
            raise ToolError(f"The user can read flow {flow.id} but not change it (it needs EDIT on its folder).")
        d = _new_draft(session, ctx, flow_id=flow.id, base_hash=fingerprint(flow.definition), name=flow.name,
                       project_id=flow.project_id, engine=flow.engine,
                       definition=flow.definition or '{"nodes": [], "edges": []}')
        return {"draft_id": d.id, "flow_id": flow.id, "name": flow.name, "engine": flow.engine,
                "folder": folder_paths(session).get(flow.project_id, ""), **describe_flow(flow.definition)}

    return await in_thread(ctx, work)


class AddSourceArgs(DraftArgs):
    datasource_id: int


@tool("flow_add_source", title="Add a source", args=AddSourceArgs, level="write")
async def flow_add_source(ctx: ToolContext, a: AddSourceArgs) -> dict:
    """Put a datasource in the draft as a source node (an empty source node already there is filled instead). Returns the node id and its columns."""
    return await _with_draft(ctx, a.draft_id, lambda deps: ai_flow_agent.add_source(_ctx_of(deps), a.datasource_id))


class AddStepArgs(DraftArgs):
    after: str = Field(description="The node whose result the step reads.")
    operation: str = Field(description="One of: " + ", ".join(ai_flow_agent.OPERATIONS) + ".")
    params: dict[str, Any] = Field(default_factory=dict)
    right: Optional[str] = Field(None, description="join and union only: the node whose rows they bring in.")


async def flow_add_step(ctx: ToolContext, a: AddStepArgs) -> dict:
    return await _with_draft(ctx, a.draft_id, lambda deps: ai_flow_agent.add_step(
        _ctx_of(deps), a.after, a.operation, a.params, a.right))


flow_add_step.__doc__ = (
    "Add an operation that reads the result of node `after` (and, for join or union, of node `right`). Returns the new "
    "node id with its columns and first rows, or the error the preview gave.\n\n"
    + ai_flow_agent._REFERENCE.replace("{dialect}", "the SQL dialect of the draft's engine (DuckDB, ClickHouse, Polars SQL…)")
)
tool("flow_add_step", title="Add a step", args=AddStepArgs, level="write")(flow_add_step)


class ChangeStepArgs(DraftArgs):
    node: str
    params: dict[str, Any] = Field(default_factory=dict)
    operation: Optional[str] = Field(None, description="Changes the step's kind too.")


@tool("flow_change_step", title="Change a step", args=ChangeStepArgs, level="write")
async def flow_change_step(ctx: ToolContext, a: ChangeStepArgs) -> dict:
    """Replace the settings of a step (all its params, as flow_add_step takes them; `operation` changes its kind too). Returns its columns and first rows, or the error."""
    return await _with_draft(ctx, a.draft_id, lambda deps: ai_flow_agent.change_step(
        _ctx_of(deps), a.node, a.params, a.operation))


class ConnectArgs(DraftArgs):
    from_node: str
    to_node: str
    input: Literal["left", "right"] = "left"


@tool("flow_connect", title="Connect two nodes", args=ConnectArgs, level="write")
async def flow_connect(ctx: ToolContext, a: ConnectArgs) -> dict:
    """Make `to_node` read `from_node` on its left (main) or right (join/union) input, replacing what it read there."""
    return await _with_draft(ctx, a.draft_id, lambda deps: ai_flow_agent.connect(
        _ctx_of(deps), a.from_node, a.to_node, a.input))


class NodeArgs(DraftArgs):
    node: str


@tool("flow_remove_node", title="Remove a node", args=NodeArgs, level="write")
async def flow_remove_node(ctx: ToolContext, a: NodeArgs) -> dict:
    """Remove a node from the draft. The nodes that read it read what it read instead, so the chain stays connected."""
    return await _with_draft(ctx, a.draft_id, lambda deps: ai_flow_agent.remove_node(_ctx_of(deps), a.node))


class AddOutputArgs(OutputSettings):
    draft_id: int
    after: str = Field(description="The node whose result the Output writes.")


@tool("flow_add_output", title="Add an Output", args=AddOutputArgs, level="write")
async def flow_add_output(ctx: ToolContext, a: AddOutputArgs) -> dict:
    """Add an Output that writes the result of node `after` when the flow runs: a datasource in Tabularia, a database table, a file on S3 or SFTP, or an email with the result attached. Connections are referenced by id (list_connections) and checked as a run checks them — the user's permission, the connection's kind, the domains an SMTP connection may send to. Placeholders such as {date} or {datetime} are allowed in file names, keys and subjects."""

    async def change(deps: ai_flow_agent.FlowDeps) -> dict:
        draft = deps.draft
        parent = ai_flow_agent._data_node(draft, a.after, "after")
        if parent.get("parentNode"):
            raise ModelRetry("Steps inside a loop container are edited by hand.")
        data = node_data(a, deps.project_id)
        info = await check_output(ctx, data, deps.flow_name)
        node_id = draft.next_id("out-")
        pos = parent.get("position") or {"x": 60, "y": 140}
        draft.add({"id": node_id, "type": "output",
                   "position": draft.free_position(pos.get("x", 60) + ai_flow_agent.X_STEP, pos.get("y", 140)),
                   "data": data})
        draft.link(a.after, node_id, "left")
        return {"node": node_id, "writes": info["words"]}

    return await _with_draft(ctx, a.draft_id, change)


class PreviewArgs(DraftArgs):
    node: str
    rows: int = Field(10, ge=1, le=ai_flow_agent.MAX_PREVIEW_ROWS)


@tool("flow_preview", title="Preview a node", args=PreviewArgs, level="write")
async def flow_preview(ctx: ToolContext, a: PreviewArgs) -> dict:
    """Columns and the first rows (at most 30) of a node's result in the draft, on the development sample of the data."""
    return await _with_draft(ctx, a.draft_id, lambda deps: ai_flow_agent.preview(_ctx_of(deps), a.node, a.rows))


@tool("flow_draft_show", title="Show a draft", args=DraftArgs, level="write")
async def flow_draft_show(ctx: ToolContext, a: DraftArgs) -> dict:
    """The draft now: its name, folder and engine, every node with its id, kind, settings and inputs and, for a saved flow being changed, the differences from the saved version."""
    def work(session, user):
        d = own_draft(session, ctx, a.draft_id)
        out: dict[str, Any] = {"draft_id": d.id, "name": d.name, "folder_id": d.project_id,
                               "folder": folder_paths(session).get(d.project_id, ""), "engine": d.engine,
                               "flow_id": d.flow_id, **describe_flow(d.definition)}
        if d.flow_id is not None:
            flow = session.get(Flow, d.flow_id)
            if flow is None:
                out["note"] = "The saved flow has been deleted."
            else:
                out["link"] = flow_link(flow.id)
                out["changes"] = flow_diff.diff_lines(flow_diff.diff(flow.definition, d.definition))
                out["flow_changed_since_open"] = fingerprint(flow.definition) != d.base_hash
        return out

    return await in_thread(ctx, work)


# ── plan: save ────────────────────────────────────────────────────────────────
def _outputs(definition: dict, only: Optional[set[str]] = None) -> list[dict]:
    return [n for n in definition.get("nodes") or []
            if n.get("type") == "output" and (only is None or n.get("id") in only)]


async def _plan_preview(ctx: ToolContext, snap: dict, definition: dict) -> Optional[dict]:
    """The first rows reaching the first Output: what the person sees on the plan. A help,
    never a reason to refuse."""
    out = next(iter(_outputs(definition)), None)
    if out is None:
        return None
    draft = ai_flow_agent.FlowDraft(definition)
    upstream = draft.inputs(out["id"]).get("left")
    if not upstream:
        return None
    try:
        res = await ai_flow_agent.preview_node(_deps(ctx, snap, draft), upstream, PLAN_PREVIEW_ROWS)
    except Exception:  # noqa: BLE001
        return None
    if "error" in res:
        return {"node": upstream, "error": str(res["error"])[:500]}
    return {"node": upstream, "columns": res.get("columns", []), "rows": res.get("rows", [])[:PLAN_PREVIEW_ROWS]}


class PlanSaveArgs(BaseModel):
    draft_id: int


@tool("plan_save_flow", title="Plan saving a flow", args=PlanSaveArgs, level="write")
async def plan_save_flow(ctx: ToolContext, a: PlanSaveArgs) -> dict:
    """Add to the plan: save the draft — a new flow is created, a saved one changed (the plan shows what changes). The Outputs it adds or changes are checked again. Planning the same draft again replaces the earlier save. Nothing changes until the plan is confirmed."""
    with ctx.session_factory() as session:
        snap = _snapshot(own_draft(session, ctx, a.draft_id))
    definition = json.loads(snap["definition"] or "{}")
    nodes = definition.get("nodes") or []
    if not nodes:
        raise ToolError("The draft is empty: add a source, steps and an Output first.")

    def check(session, user):
        from app.routes.flows import _validate_engine

        paths = folder_paths(session)
        if snap["flow_id"] is None:
            if session.get(Project, snap["project_id"]) is None \
                    or not perm_service.has_capability(session, user, snap["project_id"], Capability.EDIT):
                raise ToolError(f"The user cannot create flows in folder {snap['project_id']}.")
            _validate_engine(snap["engine"], session)
            return None, paths.get(snap["project_id"], "")
        flow = session.get(Flow, snap["flow_id"])
        if flow is None:
            raise ToolError(f"Flow {snap['flow_id']} no longer exists.")
        if not perm_service.has_capability(session, user, flow.project_id, Capability.EDIT):
            raise ToolError(f"The user cannot change flow «{flow.name}» (it needs EDIT on its folder).")
        if fingerprint(flow.definition) != snap["base_hash"]:
            raise ToolError(f"Flow «{flow.name}» was changed in Tabularia after the draft was opened: open it again "
                            "with flow_draft_open and redo the changes on the current version.")
        return flow.definition, paths.get(flow.project_id, "")

    saved_definition, folder = await in_thread(ctx, check)
    action: dict[str, Any] = {
        "kind": "save_flow", "draft_id": snap["id"], "flow_id": snap["flow_id"], "name": snap["name"],
        "project_id": snap["project_id"], "folder": folder, "engine": snap["engine"],
        "definition": snap["definition"], "base_hash": snap["base_hash"],
    }
    if saved_definition is None:
        to_check = None
        whole = ai_flow_agent.FlowDraft(definition)
        action["node_lines"] = [flow_diff.node_line(whole, n) for n in nodes]
        words = f"Create the flow «{snap['name']}» in {folder or 'its folder'} (engine {snap['engine']}), {len(nodes)} nodes"
    else:
        changes = flow_diff.diff(saved_definition, definition)
        if flow_diff.is_empty(changes):
            raise ToolError(f"The draft changes nothing in flow «{snap['name']}».")
        to_check = flow_diff.changed_node_ids(saved_definition, definition)
        action["diff"] = changes
        action["diff_lines"] = flow_diff.diff_lines(changes)
        n = len(action["diff_lines"])
        words = f"Change the flow «{snap['name']}» in {folder} ({n} change{'s' if n != 1 else ''})"
    outputs = []
    for node in _outputs(definition, to_check):
        outputs.append({"node": node["id"], **await check_output(ctx, node.get("data") or {}, snap["name"])})
    action["outputs"] = outputs
    if outputs:
        words += ": " + "; ".join(f"{o['node']} {o['words']}" for o in outputs)
    action["words"] = words + "."
    preview = await _plan_preview(ctx, snap, definition)
    if preview:
        action["preview"] = preview
    return plans.add_action(ctx, action, key=f"save_flow:{snap['id']}")


@plans.action_kind("save_flow")
async def _apply_save_flow(ctx: ToolContext, a: dict, results: list[dict]) -> dict:
    from app.routes import flows as flows_routes
    from app.schemas.models import FlowCreate, FlowUpdate

    definition = json.loads(a["definition"])
    if a.get("flow_id") is None:
        to_check = None
    else:
        current = await in_thread(ctx, lambda session, user: getattr(session.get(Flow, a["flow_id"]), "definition", None))
        to_check = flow_diff.changed_node_ids(current or "{}", definition)
    # what it writes, checked again: a folder's permissions or a connection's domains may have changed
    for node in _outputs(definition, to_check):
        await check_output(ctx, node.get("data") or {}, a["name"])

    def work(session, user):
        if a.get("flow_id") is None:
            flow = flows_routes.create_flow(a["project_id"], FlowCreate(name=a["name"], definition=a["definition"],
                                                                        engine=a["engine"]),
                                            ctx.request, user=user, session=session)
            verb = "created"
        else:
            flow = session.get(Flow, a["flow_id"])
            if flow is None:
                raise ToolError(f"Flow {a['flow_id']} no longer exists: nothing saved.")
            if fingerprint(flow.definition) != a["base_hash"]:
                raise ToolError(f"Flow «{flow.name}» was changed in Tabularia after the plan was made: nothing saved. "
                                "Open it again and make a new plan.")
            flow = flows_routes.update_flow(flow.id, FlowUpdate(definition=a["definition"]), ctx.request,
                                            user=user, session=session)
            verb = "changed"
        return flow.id, flow.name, fingerprint(flow.definition), verb

    flow_id, name, new_hash, verb = await in_thread(ctx, work)
    # the draft goes on from the saved flow: a later change is planned against it
    with ctx.session_factory() as session:
        d = session.get(AgentDraft, a.get("draft_id"))
        if d is not None and d.user_id == ctx.user.id:
            d.flow_id, d.base_hash, d.updated_at = flow_id, new_hash, now()
            session.add(d)
            session.commit()
    return {"object": {"kind": "flow", "id": flow_id, "name": name}, "draft_id": a.get("draft_id"),
            "link": flow_link(flow_id), "words": f"Flow «{name}» {verb}."}


# ── plan: run and schedule ────────────────────────────────────────────────────
def _target(ctx: ToolContext, flow_id: Optional[int], draft_id: Optional[int]) -> dict:
    """The flow an action is about: a saved one, or the one a save in the plan makes."""
    if (flow_id is None) == (draft_id is None):
        raise ToolError("Give flow_id (a saved flow) or draft_id (a draft whose save is in the plan already).")
    if draft_id is not None:
        saved = plans.planned(ctx, "save_flow", draft_id=draft_id)
        if saved is None:
            raise ToolError(f"The save of draft {draft_id} is not in the plan: add plan_save_flow first, "
                            "or name the saved flow with flow_id.")
        return {"flow_id": saved.get("flow_id"), "draft_id": draft_id, "name": saved["name"],
                "project_id": saved["project_id"], "definition": saved["definition"]}
    return {"flow_id": flow_id, "draft_id": None}


def _check_target(session, user, target: dict, capability: Capability, what: str) -> dict:
    if target["draft_id"] is None:
        flow = session.get(Flow, target["flow_id"])
        if flow is None or not perm_service.has_capability(session, user, flow.project_id, Capability.VIEW):
            raise ToolError(f"No flow {target['flow_id']} is available to this user.")
        target = {**target, "name": flow.name, "project_id": flow.project_id, "definition": flow.definition,
                  "schedule": flow.run_schedule, "scheduled_by": flow.run_scheduled_by}
    if not perm_service.has_capability(session, user, target["project_id"], capability):
        raise ToolError(f"The user cannot {what} «{target['name']}» (it needs {capability.value.upper()} on its folder).")
    return target


def _runnable(target: dict) -> None:
    from app.routes.flows import _has_runnable_nodes

    if not _has_runnable_nodes(target["definition"]):
        raise ToolError(f"Flow «{target['name']}» has nothing to run: it needs an Output (or a refresh, run-flow, "
                        "script or container node).")


def _flow_of(a: dict, results: list[dict]) -> int:
    """At apply time: the saved flow, or the one the save of its draft made in this plan."""
    if a.get("draft_id") is not None:
        made = next((r for r in results if r.get("ok") and r.get("draft_id") == a["draft_id"]), None)
        if made is None:
            raise ToolError("The flow to save first was not saved.")
        return made["object"]["id"]
    return a["flow_id"]


class PlanRunArgs(BaseModel):
    flow_id: Optional[int] = Field(None, description="A saved flow.")
    draft_id: Optional[int] = Field(None, description="A draft whose save is in the plan already: the flow it makes.")
    mode: Literal["development", "production"] = Field(
        "development", description="development: the flow's editor engine, with its development samples; production: "
                                   "its production engine, every record, as a scheduled run.")


@tool("plan_run_flow", title="Plan running a flow", args=PlanRunArgs, level="write")
async def plan_run_flow(ctx: ToolContext, a: PlanRunArgs) -> dict:
    """Add to the plan: run a flow once, now — its Outputs write, send and publish what they say. Nothing runs until the plan is confirmed; then get_run follows the run."""
    target = _target(ctx, a.flow_id, a.draft_id)
    target = await in_thread(ctx, lambda session, user: _check_target(session, user, target, Capability.RUN, "run"))
    _runnable(target)
    engine = "its production engine, every record" if a.mode == "production" else "its development engine"
    action = {"kind": "run_flow", "flow_id": target["flow_id"], "draft_id": target["draft_id"], "name": target["name"],
              "mode": a.mode, "words": f"Run the flow «{target['name']}» now, on {engine}."}
    return plans.add_action(ctx, action, key=f"run_flow:{target['flow_id'] or 'd%s' % target['draft_id']}")


@plans.action_kind("run_flow")
async def _apply_run_flow(ctx: ToolContext, a: dict, results: list[dict]) -> dict:
    from app.routes import flows as flows_routes

    flow_id = _flow_of(a, results)
    res = await in_thread(ctx, lambda session, user: flows_routes.run_flow_now(
        flow_id, ctx.request, mode=a.get("mode") or "development", user=user, session=session))
    return {"object": {"kind": "run", "id": res["run_id"], "flow_id": flow_id}, "link": flow_link(flow_id),
            "words": f"Run {res['run_id']} of «{a['name']}» started."}


@plans.scheduler("flow")
async def _plan_flow_schedule(ctx: ToolContext, a: Any) -> dict:
    from app.services import notify_targets
    from app.services.schedule import ScheduleError, next_fire, validate_schedule

    cron = (a.cron or "").strip()
    if cron:
        try:
            cron = validate_schedule(cron)
        except ScheduleError as e:
            raise ToolError(str(e)) from e
    target = _target(ctx, a.id, a.draft_id)
    notify = a.notify_emails is not None or a.notify_connection_id is not None

    def check(session, user):
        t = _check_target(session, user, target, Capability.RUN, "schedule")
        if notify:
            if not (a.notify_emails or "").strip() or a.notify_connection_id is None:
                raise ToolError("A failure notice needs both notify_emails and notify_connection_id.")
            conn = notify_targets.smtp_connection(session, user, a.notify_connection_id)
            notify_targets.ensure_deliverable(conn, notify_targets.parse_recipients(a.notify_emails))
        return t

    target = await in_thread(ctx, check)
    tz = get_settings().app.timezone
    if cron:
        _runnable(target)
        nxt = next_fire(cron, datetime.now(timezone.utc))
        words = f"Schedule the flow «{target['name']}»: cron `{cron}` in {tz} (next run {nxt:%Y-%m-%d %H:%M} UTC)"
        if target.get("schedule") and target.get("scheduled_by") not in (None, ctx.user.id):
            words += f", replacing `{target['schedule']}` set by someone else: the runs will carry this user's authority"
    else:
        words = f"Turn off the schedule of the flow «{target['name']}»"
    if notify:
        words += f"; a failed run is notified to {a.notify_emails}"
    action = {"kind": "schedule_flow", "flow_id": target["flow_id"], "draft_id": target["draft_id"],
              "name": target["name"], "cron": cron or None, "timezone": tz,
              "notify_emails": a.notify_emails, "notify_connection_id": a.notify_connection_id, "words": words + "."}
    return plans.add_action(ctx, action, key=f"schedule_flow:{target['flow_id'] or 'd%s' % target['draft_id']}")


@plans.action_kind("schedule_flow")
async def _apply_schedule_flow(ctx: ToolContext, a: dict, results: list[dict]) -> dict:
    from app.routes import flows as flows_routes
    from app.schemas.models import FlowScheduleUpdate

    flow_id = _flow_of(a, results)
    body = FlowScheduleUpdate(cron=a.get("cron") or "", notify_emails=a.get("notify_emails"),
                              notify_connection_id=a.get("notify_connection_id"))

    def work(session, user):
        flow = flows_routes.set_flow_schedule(flow_id, body, ctx.request, user=user, session=session)
        return flow.name, flow.run_schedule, flow.next_run_at

    name, schedule, nxt = await in_thread(ctx, work)
    words = (f"«{name}» scheduled: `{schedule}` (next run {nxt:%Y-%m-%d %H:%M} UTC)." if schedule and nxt
             else f"The schedule of «{name}» is off.")
    return {"object": {"kind": "flow", "id": flow_id, "name": name}, "link": flow_link(flow_id), "words": words}

