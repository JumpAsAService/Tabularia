"""The agent's reading tools: every token level and the chat may use them.

They change nothing. Each reads with the permissions of the person it runs for, through
the same functions the interface uses (the search, the runs' routes, the data
assistant's tools): what someone cannot see in the interface, the agent cannot see either.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field
from sqlmodel import select

from app.models import Connection, User
from app.models.permission import Capability
from app.services import ai_agent, ai_flow_agent
from app.services import permissions as perm_service
from app.services.agent.common import datasource_link, flow_link, folder_paths, in_thread
from app.services.agent.registry import ToolContext, tool

MAX_FOLDERS = 300
MAX_CONNECTIONS = 200
MAX_ERROR_DETAIL = 3000

SearchKind = Literal["folder", "flow", "group", "datasource", "view", "script", "sql", "notebook", "report", "connection"]


def _link(kind: str, item_id: int, name: str) -> str:
    """Where the interface opens an object (as the global search does)."""
    from urllib.parse import quote

    return {
        "flow": flow_link(item_id), "group": f"/flow-group?id={item_id}", "view": f"/viewer?view={item_id}",
        "script": f"/script?id={item_id}", "sql": f"/sql-script?id={item_id}", "notebook": f"/notebook?id={item_id}",
        "report": f"/report?id={item_id}",
        "datasource": datasource_link(item_id, name), "connection": f"/connections?q={quote(name)}&conn={item_id}",
        "folder": f"/?folder={item_id}",
    }.get(kind, "/")


def _assistant(ctx: ToolContext) -> SimpleNamespace:
    """The data assistant's tools take pydantic-ai's RunContext: only `deps` is read."""
    return SimpleNamespace(deps=ctx.chat_deps())


# ── search and folders ────────────────────────────────────────────────────────
class SearchArgs(BaseModel):
    query: str = Field(min_length=1, max_length=200, description="Text searched in the names.")
    kinds: Optional[list[SearchKind]] = Field(None, description="Only these kinds of object; all when omitted.")
    limit: int = Field(30, ge=1, le=100)


@tool("search", title="Search the catalog", args=SearchArgs, level="read")
async def search(ctx: ToolContext, a: SearchArgs) -> dict:
    """Find objects by name across the catalog: folders, flows, flow groups (group), datasources, saved views (view), Python/R scripts (script), SQL scripts (sql), notebooks, reports and connections. Only what this user can see. Each result has its id, its folder and the link that opens it in Tabularia."""
    from app.routes.search import search_everything

    def work(session, user):
        return search_everything(q=a.query, kind=None, limit=200, offset=0, user=user, session=session)

    found = await in_thread(ctx, work)
    items = [h for h in found.items if not a.kinds or h.kind in a.kinds]
    out = []
    for h in items[:a.limit]:
        item: dict[str, Any] = {"kind": h.kind, "id": h.id, "name": h.name, "folder": h.project_name,
                                "link": _link(h.kind, h.id, h.name)}
        if h.rows is not None:
            item["rows"] = h.rows
        if h.detail:
            item["detail"] = h.detail
        if h.locked_reason:
            item["locked"] = h.locked_reason
        out.append(item)
    return {"results": out, "total": len(items), "counts": found.counts}


class NoArgs(BaseModel):
    pass


@tool("list_folders", title="List folders", args=NoArgs, level="read")
async def list_folders(ctx: ToolContext, a: NoArgs) -> dict:
    """The folders this user can read, with their path and what the user may do there: `can_run` (run and schedule flows), `can_edit` (create and change flows, write datasources)."""

    def work(session, user):
        readable = perm_service.readable_project_ids(session, user)
        paths = folder_paths(session)
        out = []
        for pid in sorted(readable, key=lambda i: paths.get(i, "").lower())[:MAX_FOLDERS]:
            out.append({"id": pid, "path": paths.get(pid, ""),
                        "can_run": perm_service.has_capability(session, user, pid, Capability.RUN),
                        "can_edit": perm_service.has_capability(session, user, pid, Capability.EDIT)})
        return out

    folders = await in_thread(ctx, work)
    return {"folders": folders, **({"note": f"Only the first {MAX_FOLDERS}."} if len(folders) >= MAX_FOLDERS else {})}


# ── datasources: the data assistant's tools, as they are ────────────────────
class ListDatasourcesArgs(BaseModel):
    search: str = Field("", max_length=200, description="Words matched against name and description; empty lists everything.")


@tool("list_datasources", title="List datasources", args=ListDatasourcesArgs, level="read")
async def list_datasources(ctx: ToolContext, a: ListDatasourcesArgs) -> dict:
    """List the datasources this user can read, each with the description its owners wrote. `search` is matched word by word against name and description, ignoring case, underscores and dashes; leave it empty to list everything. If nothing matches, everything available is returned with a note."""
    res = await ai_agent.list_datasources(_assistant(ctx), a.search)
    for d in res.get("datasources") or []:
        d["link"] = datasource_link(d["id"], d["name"])
    return res


class DescribeDatasourceArgs(BaseModel):
    datasource_id: int


@tool("describe_datasource", title="Describe a datasource", args=DescribeDatasourceArgs, level="read")
async def describe_datasource(ctx: ToolContext, a: DescribeDatasourceArgs) -> dict:
    """Columns (name, type, and the curated description of what each field means), row count and a few sample rows of one datasource."""
    return await ai_agent.describe_datasource(_assistant(ctx), a.datasource_id)


class QueryDatasourceArgs(BaseModel):
    datasource_id: int
    sql: str = Field(min_length=1, max_length=20_000)
    limit: int = Field(50, ge=1, le=2000)
    join_datasource_id: Optional[int] = None
    join_left_on: Optional[str] = None
    join_right_on: Optional[str] = None
    join_how: Optional[Literal["inner", "left", "full"]] = None
    chart_type: Optional[Literal["bar", "hbar", "line", "area", "pie", "donut", "scatter"]] = None
    chart_x: Optional[str] = None
    chart_y: Optional[str] = None
    chart_series: Optional[str] = None
    chart_title: Optional[str] = None


@tool("query_datasource", title="Query a datasource", args=QueryDatasourceArgs, level="read")
async def query_datasource(ctx: ToolContext, a: QueryDatasourceArgs) -> dict:
    """Run ONE read-only SELECT on a datasource. The table is named `self` (e.g. `SELECT paese, COUNT(*) AS n FROM self GROUP BY paese ORDER BY n DESC`). Returns columns, rows (capped) and whether the result was truncated.

    To answer a question that spans TWO datasources, set `join_datasource_id` to the second one together with `join_left_on` and `join_right_on` (the key columns on each side, comma-separated if the key has several parts). The two are joined before your SQL runs, and `self` is the joined result. `join_how` is inner by default; use left to keep rows that have no match. A column on both sides that is not a key gets the suffix `_right`.

    `chart_type` with `chart_x` (the categories) and `chart_y` (the numeric column) draws the result as a chart in the chat; leave them unset otherwise."""
    return await ai_agent.query_datasource(_assistant(ctx), **a.model_dump())


# ── connections: what they are, never what opens them ───────────────────────
class ListConnectionsArgs(BaseModel):
    kind: Optional[str] = Field(None, max_length=20, description="A connection type: postgresql, mysql, clickhouse, oracle, sqlserver, s3, sftp, smtp, sharepoint, api…")


@tool("list_connections", title="List connections", args=ListConnectionsArgs, level="read")
async def list_connections(ctx: ToolContext, a: ListConnectionsArgs) -> dict:
    """The connections this user may use (CONNECT permission on their folder): id, name, type and folder — never an address, a user name or a secret. An SMTP connection says the domains it may send to (empty = any). The agent never creates or changes a connection: when the right one is missing, tell the user and give them `add_link`."""

    def work(session, user):
        from app.routes.connections import allowed_email_domains

        allowed = perm_service.connectable_project_ids(session, user)
        if not allowed:
            return []
        q = select(Connection).where(Connection.project_id.in_(allowed))  # type: ignore[attr-defined]
        if a.kind:
            q = q.where(Connection.db_type == a.kind.strip().lower())
        paths = folder_paths(session)
        out = []
        for c in session.exec(q.order_by(Connection.name)).all()[:MAX_CONNECTIONS]:
            item: dict[str, Any] = {"id": c.id, "name": c.name, "type": c.db_type, "folder": paths.get(c.project_id, "")}
            if c.description:
                item["description"] = c.description[:280]
            if c.db_type == "smtp":
                try:
                    item["allowed_domains"] = allowed_email_domains(c)
                except Exception:  # noqa: BLE001 — malformed: nothing can be sent, the output says so
                    item["allowed_domains"] = "malformed: no email can be sent until it is fixed"
            if c.db_type == "s3":
                item["has_default_bucket"] = bool((c.database or "").strip())
            out.append(item)
        return out

    items = await in_thread(ctx, work)
    return {"connections": items, "add_link": "/connections",
            **({} if items else {"note": "This user may use no connection" + (f" of type {a.kind}" if a.kind else "") + "."})}


# ── scripting environments ────────────────────────────────────────────────────
@tool("list_environments", title="List scripting environments", args=NoArgs, level="read")
async def list_environments(ctx: ToolContext, a: NoArgs) -> dict:
    """The Python and R environments this user may run scripts and notebooks with: language, version, whether it is ready, the packages it holds and the names of its variables."""
    from app.routes.scripting import editor_environments

    def work(session, user):
        return editor_environments(session=session, user=user)

    found = await in_thread(ctx, work)
    if found.disabled_reason:
        return {"environments": [], "note": "Scripts and notebooks are not available on this installation."}
    return {"environments": [
        {"id": e.id, "name": e.name, "description": e.description, "language": e.language,
         "language_version": e.language_version, "ready": e.ready, "packages": e.packages[:80],
         "variables": [v.name for v in e.variables]}
        for e in found.environments
    ]}


# ── flows and runs ────────────────────────────────────────────────────────────
class FlowIdArgs(BaseModel):
    flow_id: int


def describe_flow(definition: str) -> dict:
    """The nodes of a flow as the flow assistant shows them: id, kind, settings, inputs."""
    try:
        draft = ai_flow_agent.FlowDraft(json.loads(definition or "{}"))
    except (ValueError, TypeError):
        return {"nodes": [], "note": "The definition is not readable."}
    fed = {e.get("source") for e in draft.edges if e.get("targetHandle") != "seq-in"}
    ends = [n["id"] for n in draft.nodes if n.get("type") in ai_flow_agent.DATA_NODES and n["id"] not in fed]
    return {"nodes": [ai_flow_agent._summary(draft, n) for n in draft.nodes], "ends_of_chains": ends}


@tool("get_flow", title="Read a flow", args=FlowIdArgs, level="read")
async def get_flow(ctx: ToolContext, a: FlowIdArgs) -> dict:
    """A saved flow: its folder, engines, schedule, last run and every node with its id, kind, settings and what it reads (`input`, `right_input`). To change it, open a draft with flow_draft_open."""
    from app.routes import flows as flows_routes
    from app.services import run_history

    def work(session, user):
        flow = flows_routes.get_flow(a.flow_id, user=user, session=session)
        last = run_history.latest_flow_runs(session, [flow.id]).get(flow.id)
        return {
            "id": flow.id, "name": flow.name, "description": flow.description,
            "folder_id": flow.project_id, "folder": folder_paths(session).get(flow.project_id, ""),
            "engine": flow.engine, "production_engine": flow.production_engine,
            "schedule": flow.run_schedule, "next_run_at": flow.next_run_at,
            "last_run": {"status": last[0], "at": last[1]} if last else None,
            "can_edit": perm_service.has_capability(session, user, flow.project_id, Capability.EDIT),
            "can_run": perm_service.has_capability(session, user, flow.project_id, Capability.RUN),
            "link": flow_link(flow.id), **describe_flow(flow.definition),
        }

    return await in_thread(ctx, work)


def run_summary(r: Any) -> dict:
    out: dict[str, Any] = {"id": r.id, "kind": r.kind, "status": r.status, "trigger": r.trigger_type,
                           "started_at": r.started_at, "finished_at": r.finished_at}
    for key in ("flow_id", "datasource_id", "flow_group_id", "sql_script_id", "saved_script_id", "rows_written",
                "engine", "publish_name", "parent_run_id"):
        value = getattr(r, key, None)
        if value is not None:
            out[key] = value
    for key in ("flow_name", "source_name", "group_name", "script_name", "sql_script_name", "launched_by_name"):
        value = getattr(r, key, None)
        if value:
            out[key] = value
    if r.error:
        out["error"] = r.error[:500]
    progress = getattr(r, "progress", None)
    if progress is not None:
        out["progress"] = progress.model_dump(exclude_none=True)
    return out


class ListRunsArgs(BaseModel):
    flow_id: Optional[int] = Field(None, description="Only the runs of this flow.")
    status: Optional[Literal["PENDING", "STARTED", "SUCCESS", "FAILURE"]] = None
    search: Optional[str] = Field(None, max_length=200, description="Text searched in the object's name, the author and the error.")
    limit: int = Field(20, ge=1, le=100)


@tool("list_runs", title="List runs", args=ListRunsArgs, level="read")
async def list_runs(ctx: ToolContext, a: ListRunsArgs) -> dict:
    """The latest runs this user can see, newest first: flows, refreshes, scripts, SQL scripts, flow groups. Filter by flow, status (FAILURE to find what broke) or text. get_run gives one run's whole error."""
    from app.routes import runs as runs_routes

    if a.flow_id is not None:
        # the flow's runs are reconciled with the engine, as the editor's history does
        with ctx.session_factory() as session:
            user = session.get(User, ctx.user.id) or ctx.user
            runs = await runs_routes.list_runs(a.flow_id, user=user, session=session)
            items = [run_summary(r) for r in runs if not a.status or r.status == a.status]
        return {"runs": items[:a.limit], "link": flow_link(a.flow_id)}

    def search(session, user):
        return runs_routes.search_runs(status=a.status, q=a.search or None, limit=a.limit, offset=0, user=user,
                                       session=session)

    page = await in_thread(ctx, search)
    return {"runs": [run_summary(r) for r in page.items], "total": page.total, "link": "/runs"}


class RunIdArgs(BaseModel):
    run_id: int


@tool("get_run", title="Read a run", args=RunIdArgs, level="read")
async def get_run(ctx: ToolContext, a: RunIdArgs) -> dict:
    """One run: its state (with its progress while it runs), what it wrote, where it sent it, and the whole error when it failed."""
    from app.routes import runs as runs_routes

    with ctx.session_factory() as session:
        user = session.get(User, ctx.user.id) or ctx.user
        r = await runs_routes.get_run(a.run_id, user=user, session=session)
    out = run_summary(r)
    if r.error_detail:
        out["error_detail"] = r.error_detail[-MAX_ERROR_DETAIL:]
    for key in ("destination", "email", "mirror", "notes"):
        raw = getattr(r, key, None)
        if raw:
            try:
                out[key] = json.loads(raw)
            except ValueError:
                out[key] = raw
    if r.flow_id:
        out["link"] = flow_link(r.flow_id)
    return out

