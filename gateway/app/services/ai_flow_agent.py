"""The flow editor's assistant (pydantic-ai): it builds and changes the flow on the
user's canvas, step by step, and checks every step on a preview.

It works on a DRAFT: the canvas the editor sends with each message, in the form a
save writes. The tools change the draft in memory; when the turn ends the editor
receives the new definition and applies it as one Undo step. Nothing is saved, run
or scheduled here: saving stays the user's act, with the checks a save makes.

Reads go through the permissions everything else uses: datasources the user can
read (VIEW on their folder), previews checked as the proxy checks the editor's
(pinned bucket, readable storage keys). A preview sends the model a few sample rows,
as the data assistant does (the user's choice of 2026-10-15), and is recorded in
the audit log like the assistant's queries.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

import httpx
from fastapi import HTTPException
from pydantic_ai import Agent, ModelRetry, RunContext

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.models import Datasource, Flow, Project
from app.models.permission import Capability
from app.services import ai_agent, ai_prompts, audit, metadata_only
from app.services import permissions as perm_service
from app.services.flow_resolver import _Resolver
from app.services.objects import collect_storage_keys, ensure_can_read_keys, ensure_reads_pinned

MAX_NODES = 300
MAX_DEFINITION_CHARS = 500_000
STEP_PREVIEW_ROWS = 5
MAX_PREVIEW_ROWS = 30
X_STEP, Y_STEP = 230, 150

# the operations the assistant may place: those the editor's parameter form can show
# and edit (foreach containers and the dev sample are built by hand)
OPERATIONS = ("select", "reorder", "drop", "rename", "cast", "filter", "sort", "limit", "unique", "fill_null",
              "drop_nulls", "group_by", "pivot", "unpivot", "compute", "sql", "join", "union")
FILTER_OPERATORS = ("eq", "ne", "gt", "ge", "lt", "le", "in", "not_in", "between", "contains", "starts_with",
                    "ends_with", "is_null", "is_not_null")
AGG_FUNCS = ("sum", "mean", "min", "max", "count", "median", "std", "var", "first", "last", "n_unique")
DTYPES = ("int", "float", "str", "bool", "date", "datetime")
JOIN_HOWS = ("inner", "left", "right", "full", "semi", "anti", "cross")
DATA_NODES = ("source", "operation")
_SELECT = re.compile(r"^\s*select\b", re.IGNORECASE)


# ── the draft ────────────────────────────────────────────────────────────────
class FlowDraft:
    """The canvas as the editor saves it, `{nodes, edges}`, changed in place."""

    def __init__(self, definition: dict, counters: Optional[dict[str, int]] = None):
        if not isinstance(definition, dict):
            raise ValueError("the flow definition must be an object")
        nodes, edges = definition.get("nodes") or [], definition.get("edges") or []
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise ValueError("the flow definition needs lists of nodes and edges")
        if len(nodes) > MAX_NODES:
            raise ValueError(f"the flow has more than {MAX_NODES} nodes")
        for n in nodes:
            if not isinstance(n, dict) or not isinstance(n.get("id"), str) or len(n["id"]) > 100:
                raise ValueError("every node needs a text id")
        self.nodes: list[dict] = [dict(n) for n in nodes]
        self.edges: list[dict] = [dict(e) for e in edges if isinstance(e, dict)]
        self.touched: list[str] = []  # nodes added or changed in this turn, in order
        # the editor's counters: an id it has handed out (and maybe taken back with
        # Undo or a delete) is never handed out again
        self.floors = {k: int(v) for k, v in (counters or {}).items()
                       if k in ("op-", "src-", "out-") and isinstance(v, int) and 0 <= v < 10**6}
        self._original = self._text()

    def _text(self) -> str:
        return json.dumps(self.definition(), sort_keys=True, default=str)

    def changed(self) -> bool:
        return self._text() != self._original

    def definition(self) -> dict:
        return {"nodes": self.nodes, "edges": self.edges}

    def get(self, node_id: str) -> dict:
        node = next((n for n in self.nodes if n["id"] == node_id), None)
        if node is None:
            raise ModelRetry(f"There is no node '{node_id}' on the canvas. Call get_flow to see the nodes.")
        return node

    def _touch(self, node_id: str) -> None:
        if node_id not in self.touched:
            self.touched.append(node_id)

    def inputs(self, node_id: str) -> dict[str, str]:
        """The node's data inputs, {left, right} (sequence edges are not data)."""
        out: dict[str, str] = {}
        for e in self.edges:
            if e.get("target") == node_id and e.get("targetHandle") != "seq-in" and e.get("sourceHandle") != "seq-out":
                out["right" if e.get("targetHandle") == "right" else "left"] = e.get("source")
        return out

    def consumers(self, node_id: str) -> list[dict]:
        return [e for e in self.edges if e.get("source") == node_id and e.get("targetHandle") != "seq-in"]

    def upstream(self, node_id: str) -> set[str]:
        seen: set[str] = set()
        stack = [node_id]
        while stack:
            cur = stack.pop()
            for src in self.inputs(cur).values():
                if src and src not in seen:
                    seen.add(src)
                    stack.append(src)
        return seen

    def next_id(self, prefix: str) -> str:
        """The next free id with the editor's prefixes, so its own counters go on from there."""
        used = [int(n["id"][len(prefix):]) for n in self.nodes if n["id"].startswith(prefix) and n["id"][len(prefix):].isdigit()]
        if prefix == "out-":  # the editor numbers Outputs and control nodes together
            used += [int(n["id"][4:]) for n in self.nodes if n["id"].startswith("ctl-") and n["id"][4:].isdigit()]
        n = max(max(used, default=0), self.floors.get(prefix, 0)) + 1
        self.floors[prefix] = n
        return f"{prefix}{n}"

    def free_position(self, x: float, y: float) -> dict:
        """(x, y), moved down until no top-level node sits there."""
        taken = [(n.get("position") or {}) for n in self.nodes if not n.get("parentNode")]
        while any(abs(p.get("x", 0) - x) < 120 and abs(p.get("y", 0) - y) < 90 for p in taken):
            y += Y_STEP
        return {"x": x, "y": y}

    def add(self, node: dict) -> None:
        if len(self.nodes) >= MAX_NODES:
            raise ModelRetry(f"The flow cannot have more than {MAX_NODES} nodes.")
        self.nodes.append(node)
        self._touch(node["id"])

    def link(self, source: str, target: str, handle: str = "left") -> None:
        self.edges = [e for e in self.edges if not (e.get("target") == target and (e.get("targetHandle") or "left") == handle)]
        self.edges.append({"id": f"e-{source}-{handle}-{target}", "source": source, "target": target,
                           "sourceHandle": None, "targetHandle": handle})


# ── dependencies ─────────────────────────────────────────────────────────────
@dataclass
class FlowDeps(ai_agent.ChatDeps):
    """What the tools may touch: the assistant's, plus the draft, the flow it
    belongs to (None for a flow not saved yet) and its folder."""

    draft: FlowDraft = field(default_factory=lambda: FlowDraft({}))
    flow_id: int | None = None
    flow_name: str = ""
    project_id: int | None = None
    selected: str | None = None
    preview_key: str = ""  # one per request: previews of one node supersede each other, not those of others


# ── parameters ───────────────────────────────────────────────────────────────
def _columns(value: Any, what: str, allow_empty: bool = False) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(c, str) and c for c in value):
        raise ModelRetry(f"{what} must be a list of column names.")
    if not value and not allow_empty:
        raise ModelRetry(f"{what} needs at least one column.")
    return list(dict.fromkeys(value))


def clean_params(operation: str, params: Any) -> dict:
    """The parameters of a step in the shape the engine runs and the editor's form
    shows, or a ModelRetry that says what is wrong."""
    if operation not in OPERATIONS:
        raise ModelRetry(f"Unknown operation '{operation}'. Use one of: {', '.join(OPERATIONS)}.")
    p = dict(params or {}) if isinstance(params, dict) else None
    if p is None:
        raise ModelRetry("params must be an object.")
    if operation in ("select", "drop", "reorder"):
        return {"columns": _columns(p.get("columns"), "columns")}
    if operation == "rename":
        mapping = p.get("mapping")
        if not isinstance(mapping, dict) or not mapping or not all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in mapping.items()):
            raise ModelRetry("rename needs mapping: {old name: new name}.")
        return {"mapping": mapping}
    if operation == "cast":
        cols = p.get("columns")
        if not isinstance(cols, dict) or not cols or any(t not in DTYPES for t in cols.values()):
            raise ModelRetry(f"cast needs columns: {{column: type}}, with types among {', '.join(DTYPES)}.")
        return {"columns": cols}
    if operation == "filter":
        column, op, value = p.get("column"), p.get("operator"), p.get("value")
        if not isinstance(column, str) or not column:
            raise ModelRetry("filter needs column.")
        if op not in FILTER_OPERATORS:
            raise ModelRetry(f"filter operator must be one of: {', '.join(FILTER_OPERATORS)}.")
        if op in ("is_null", "is_not_null"):
            return {"column": column, "operator": op}
        if value is None or (op in ("in", "not_in") and (not isinstance(value, list) or not value)):
            raise ModelRetry(f"filter with '{op}' needs a value" + (" list." if op in ("in", "not_in") else "."))
        if op == "between" and (not isinstance(value, list) or len(value) != 2):
            raise ModelRetry("filter with 'between' needs value: [from, to].")
        return {"column": column, "operator": op, "value": value}
    if operation == "sort":
        by = p.get("by")
        by = [by] if isinstance(by, str) else by
        if not isinstance(by, list) or not by or not all(isinstance(c, str) and c for c in by):
            raise ModelRetry("sort needs by: a list of column names.")
        desc = p.get("descending", False)
        desc = [desc] * len(by) if isinstance(desc, bool) else desc
        if not isinstance(desc, list) or len(desc) != len(by) or not all(isinstance(d, bool) for d in desc):
            raise ModelRetry("sort descending: one true/false for all, or a list with one per column in by.")
        return {"by": by, "descending": desc}
    if operation == "limit":
        n = p.get("n")
        if not isinstance(n, int) or isinstance(n, bool) or n < 1:
            raise ModelRetry("limit needs n, a whole number of rows of at least 1.")
        return {"n": n}
    if operation in ("unique", "drop_nulls"):
        subset = p.get("subset")
        return {"subset": _columns(subset, "subset")} if subset else {}
    if operation == "fill_null":
        cols = p.get("columns")
        if not isinstance(cols, dict) or not cols:
            raise ModelRetry("fill_null needs columns: {column: value to put where it is null}.")
        return {"columns": cols}
    if operation == "group_by":
        aggs = p.get("aggregations")
        if not isinstance(aggs, list) or not aggs:
            raise ModelRetry("group_by needs aggregations: [{column, func, alias?}].")
        out = []
        for a in aggs:
            if not isinstance(a, dict) or not isinstance(a.get("column"), str) or a.get("func") not in AGG_FUNCS:
                raise ModelRetry(f"each aggregation needs column and func among {', '.join(AGG_FUNCS)}.")
            out.append({"column": a["column"], "func": a["func"], **({"alias": str(a["alias"])} if a.get("alias") else {})})
        return {"by": _columns(p.get("by"), "by"), "aggregations": out}
    if operation == "pivot":
        values, func = p.get("values"), p.get("func") or "sum"
        if not isinstance(values, str) or not values or func not in AGG_FUNCS:
            raise ModelRetry(f"pivot needs values (one column) and func among {', '.join(AGG_FUNCS)}.")
        return {"index": _columns(p.get("index"), "index"), "on": _columns(p.get("on"), "on"), "values": values, "func": func}
    if operation == "unpivot":
        out: dict[str, Any] = {}
        for key in ("index", "on"):
            if p.get(key):
                out[key] = _columns(p[key], key)
        for key in ("variable_name", "value_name"):
            if p.get(key):
                out[key] = str(p[key])
        return out
    if operation == "compute":
        cols = p.get("columns")
        if not isinstance(cols, list) or not cols or not all(
                isinstance(c, dict) and str(c.get("name") or "").strip() and str(c.get("expr") or "").strip() for c in cols):
            raise ModelRetry("compute needs columns: [{name, expr}], each with a name and an expression.")
        return {"columns": [{"name": str(c["name"]).strip(), "expr": str(c["expr"]).strip()} for c in cols]}
    if operation == "sql":
        query = str(p.get("query") or "").strip().rstrip(";").strip()
        if not _SELECT.match(query) or ";" in query:
            raise ModelRetry("sql needs query: ONE statement that starts with SELECT and reads FROM self (no leading WITH, no ';').")
        return {"query": query}
    if operation == "join":
        how = p.get("how") or "inner"
        if how not in JOIN_HOWS:
            raise ModelRetry(f"join how must be one of: {', '.join(JOIN_HOWS)}.")
        if how == "cross":
            return {"how": how}
        if p.get("on"):
            return {"how": how, "on": _columns(p["on"], "on")}
        left, right = p.get("left_on"), p.get("right_on")
        if not left or not right:
            raise ModelRetry("join needs on (key columns with the same name on both sides) or left_on and right_on.")
        left, right = _columns(left, "left_on"), _columns(right, "right_on")
        if len(left) != len(right):
            raise ModelRetry("left_on and right_on must name as many columns each.")
        return {"how": how, "left_on": left, "right_on": right}
    strategy = p.get("strategy") or "relaxed"  # union
    if strategy not in ("relaxed", "strict"):
        raise ModelRetry("union strategy is relaxed (align columns by name) or strict.")
    return {"strategy": strategy}


# ── previews ─────────────────────────────────────────────────────────────────
def _slot(deps: FlowDeps, node_id: str) -> str:
    return f"u{deps.user.id}:fa{deps.preview_key}:{re.sub(r'[^A-Za-z0-9_-]', '_', node_id)[:40]}"


async def preview_node(deps: FlowDeps, node_id: str, rows: int, for_the_person: bool = False) -> dict[str, Any]:
    """The node's result on a preview of the draft, as the editor would show it
    (development sample included), checked as the proxy checks the editor's. In
    metadata-only mode only for a plan's card (`for_the_person`), which the model never
    reads: for a model there is no preview."""
    if deps.withholds_data() and not for_the_person:
        return {"error": metadata_only.PREVIEW_WITHHELD}
    draft = deps.draft
    with deps.session_factory() as session:
        readable = perm_service.readable_project_ids(session, deps.user)

        def resolve_ds(ds_id: Any) -> Optional[tuple[str, str]]:
            ds = session.get(Datasource, ds_id) if isinstance(ds_id, int) else None
            if ds is None or ds.project_id not in readable or not ds.key:
                return None
            return ds.bucket, ds.key

        source, operations = _Resolver(draft.nodes, draft.edges, resolve_ds, "development").chain(node_id)
        if source is None:
            return {"error": "Nothing to preview: no source with data upstream of this node (connect it to a source)."}
        bucket = get_settings().engine.bucket
        body: dict[str, Any] = {"bucket": source[0] or bucket, "input_key": source[1], "operations": operations,
                                "limit": rows, "principal": "ai", "slot": _slot(deps, node_id)}
        if deps.engine:
            body["engine"] = deps.engine
        try:
            ensure_reads_pinned(session, deps.user, body, bucket)
            ensure_can_read_keys(session, deps.user, collect_storage_keys(body))
        except HTTPException:
            return {"error": "This node reads data the user cannot read."}
    outcome, n_rows = "success", 0
    try:
        try:
            resp = await get_engine_client().post("/tasks/preview", json=body, timeout=180)
        except httpx.HTTPError as e:
            outcome = "failure"
            return {"error": f"The engine did not answer the preview ({type(e).__name__}): try again in a moment."}
        if resp.status_code >= 400:
            outcome = "failure"
            try:
                detail = resp.json().get("detail")
            except ValueError:
                detail = resp.text
            if resp.status_code >= 500:
                return {"error": f"The engine failed ({resp.status_code}): {str(detail)[:500]}"}
            return {"error": str(detail)[:1500]}
        res = resp.json()
        n_rows = len(res.get("rows") or [])
        return {"columns": res.get("columns", []), "rows": ai_agent._trim(res.get("rows") or [])[:rows],
                "row_count": n_rows, "truncated": bool(res.get("truncated"))}
    finally:
        with deps.session_factory() as session:
            audit.record_audit(
                session, actor=deps.user, action=audit.AI_QUERY, outcome=outcome,
                target_type="flow", target_id=deps.flow_id, target_label=deps.flow_name or "new flow",
                detail={"node": node_id, "operations": len(operations), "model": deps.model_id,
                        "engine": deps.engine or "default", "rows": n_rows, "via": "flow editor"},
                request=deps.request,
            )


async def _step_result(deps: FlowDeps, node_id: str) -> dict[str, Any]:
    """What a change returns: the node and a glance at its result, so a broken step is seen at once.

    In metadata-only mode, the node alone: the rows are values, the column names of a pivot
    too, and even whether a step runs answers a question about the data («fail if this
    value is there»)."""
    if deps.withholds_data():
        return {"node": node_id, "note": metadata_only.STEP_NOT_CHECKED}
    res = await preview_node(deps, node_id, STEP_PREVIEW_ROWS)
    if "error" in res:
        return {"node": node_id, "preview_error": res["error"]}
    return {"node": node_id, "columns": res["columns"], "first_rows": res["rows"]}


# ── instructions ─────────────────────────────────────────────────────────────
_REFERENCE = """Operations and their params (column names are case-sensitive):
- select {columns: [..]} keeps these columns; drop {columns: [..]} removes them; reorder {columns: [..]} puts them first.
- rename {mapping: {old: new}}.
- cast {columns: {col: type}}; types: int, float, str, bool, date, datetime. A value that does not convert becomes null.
- filter {column, operator, value}; operators: eq ne gt ge lt le (one value), in not_in (a list), between ([from, to]), contains starts_with ends_with (text), is_null is_not_null (no value). Numbers as JSON numbers, dates as ISO text ("2024-01-31").
- sort {by: [..], descending: [bool, ..]}: on the first column, then the next ones on equal values; one direction per column (true = descending). Nulls always go last.
- limit {n}. unique {subset?: [..]}. drop_nulls {subset?: [..]}. fill_null {columns: {col: value}}.
- group_by {by: [..], aggregations: [{column, func, alias?}]}; funcs: sum mean min max count median std var first last n_unique.
- pivot {index: [..], on: [..], values: col, func}; unpivot {index?: [..], on?: [..], variable_name?, value_name?}.
- compute {columns: [{name, expr}]}: new or replaced columns from SQL expressions ({dialect}), applied in order, each may use those before it; window functions with OVER are allowed.
- sql {query}: ONE SELECT ... FROM self in {dialect}; use it only when no operation above fits.
- join {how, on: [..]} or {how, left_on: [..], right_on: [..]}; how: inner left right full semi anti cross. Needs a right input. union {strategy: relaxed|strict} stacks the rows of the right input."""


# in metadata-only mode, after the administrators' text (services/metadata_only.py)
METADATA_ONLY_CONTEXT = (
    "- METADATA-ONLY MODE is on, by the administrator's choice: no value of a row of data reaches you, and these "
    "instructions come before anything above about previews and first rows. Your tools run no preview: a step is added "
    "without being checked and returns no rows and no columns, so reason from the columns of its sources and from the "
    "settings of the steps. Never state a value or a figure of the data, and do not write a value into a filter unless the "
    "user gave it to you. When you are done, ask the user to check the result on the canvas.")


def context(engine: Optional[str], locale: str, flow_name: str = "", selected: Optional[str] = None,
            withheld: bool = False, history_withheld: bool = False) -> str:
    """What Tabularia adds to the text at every request (services/ai_prompts.py)."""
    dialect, quote = ai_agent.dialect_of(engine)
    flow = f"the flow «{flow_name}»" if flow_name else "a new flow, not saved yet"
    lines = [
        ai_prompts.CONTEXT_HEADER,
        f"- {ai_prompts.today()}",
        f"- The user is editing {flow}, which runs on the {engine or 'default'} engine. "
        f"SQL expressions are in {dialect}; quote odd column names with {quote}.",
    ]
    if selected:
        lines.append(f"- The user has node {selected} selected: «this step» means that node.")
    if withheld:
        lines.append(METADATA_ONLY_CONTEXT)
        if history_withheld:
            lines.append(ai_agent.HISTORY_WITHHELD_CONTEXT)
    return ("\n".join(lines) + "\n\n" + _REFERENCE.replace("{dialect}", dialect)
            + "\n\n" + ai_prompts.language_line(locale))


def _instructions(ctx: RunContext[FlowDeps]) -> str:
    deps = ctx.deps
    return ai_prompts.compose(deps.prompt or ai_prompts.FLOW, context(deps.engine, deps.locale, deps.flow_name, deps.selected,
                                                                      deps.withholds_data(), deps.history_withheld))


flow_agent: Agent[FlowDeps, str] = Agent(
    deps_type=FlowDeps,
    instructions=_instructions,
    retries=2,
    name="tabularia-flow-assistant",
    capabilities=[ai_agent.StopWhenWithheld()],
)

# name → does it return values of the data? As the registry's `reads_data` (services/agent/
# registry.py): no default, and a tool of this agent is registered only through `flow_tool`.
FLOW_TOOLS: dict[str, bool] = {}


async def _not_in_metadata_only(ctx: RunContext[FlowDeps], tool_def: Any) -> Any:
    """A tool that reads data is not offered in metadata-only mode."""
    return None if ctx.deps.withholds_data() else tool_def


def flow_tool(*, reads_data: bool):
    def register(fn):
        FLOW_TOOLS[fn.__name__] = reads_data
        flow_agent.tool(fn, prepare=_not_in_metadata_only if reads_data else None)
        return fn

    return register


# reading the catalogue works as in the data assistant; describe_datasource leaves out its
# sample rows by itself in metadata-only mode
flow_tool(reads_data=False)(ai_agent.list_datasources)
flow_tool(reads_data=False)(ai_agent.describe_datasource)


def _columns_of(node: dict) -> list[str]:
    cols = (node.get("data") or {}).get("columns")
    if isinstance(cols, str):
        try:
            cols = json.loads(cols)
        except ValueError:
            cols = []
    return [str(c.get("name")) for c in cols if isinstance(c, dict)][:80] if isinstance(cols, list) else []


def _summary(draft: FlowDraft, node: dict) -> dict[str, Any]:
    d = node.get("data") or {}
    kind = node.get("type")
    inputs = draft.inputs(node["id"])
    if kind == "source":
        out: dict[str, Any] = {"id": node["id"], "kind": "source", "datasource_id": d.get("datasourceId"),
                               "datasource": d.get("filename") or None, "columns": _columns_of(node)}
        if d.get("filters"):
            out["filters"] = d["filters"]
        if d.get("datasourceId") is None and not d.get("parquetKey"):
            out["empty"] = True
        return out
    if kind == "operation":
        out = {"id": node["id"], "kind": "step", "operation": d.get("opType"), "params": d.get("params") or {},
               "input": inputs.get("left")}
        if inputs.get("right"):
            out["right_input"] = inputs["right"]
        if node.get("parentNode"):
            out["inside_loop"] = node["parentNode"]
        return out
    if kind == "output":
        dest = d.get("destType") or "datasource"
        writes = ({"datasource": d.get("name"), "folder_id": d.get("projectId"), "overwrite": bool(d.get("overwrite"))}
                  if dest == "datasource" else {"type": dest})
        return {"id": node["id"], "kind": "output", "input": inputs.get("left"), "writes": writes}
    if kind == "comment":
        return {"id": node["id"], "kind": "note", "text": str(d.get("text") or "")[:300]}
    return {"id": node["id"], "kind": kind, "note": "edited by hand only"}


@flow_tool(reads_data=False)
async def get_flow(ctx: RunContext[FlowDeps]) -> dict[str, Any]:
    """The flow on the canvas now: every node with its id, kind and settings, and what it reads (`input`, `right_input`)."""
    draft = ctx.deps.draft
    fed = {e.get("source") for e in draft.edges if e.get("targetHandle") != "seq-in"}
    ends = [n["id"] for n in draft.nodes if n.get("type") in DATA_NODES and n["id"] not in fed]
    return {"nodes": [_summary(draft, n) for n in draft.nodes], "ends_of_chains": ends, "selected": ctx.deps.selected}


@flow_tool(reads_data=False)
async def add_source(ctx: RunContext[FlowDeps], datasource_id: int) -> dict[str, Any]:
    """Put a datasource on the canvas as a source node (an empty source node already there is filled instead). Returns the node id and its columns."""
    deps = ctx.deps
    with deps.session_factory() as session:
        ds = ai_agent._readable_datasource(session, deps.user, datasource_id)
        if not ds.key:
            raise ModelRetry(f"The datasource {ds.name} has no data yet (never imported).")
        try:
            columns = json.loads(ds.columns or "[]")
        except ValueError:
            columns = []
        data = {"bucket": ds.bucket, "datasetId": None, "datasourceId": ds.id, "parquetKey": ds.key,
                "filename": ds.name, "rows": ds.rows, "columns": columns}
    draft = deps.draft
    empty = next((n for n in draft.nodes if n.get("type") == "source" and not n.get("parentNode")
                  and (n.get("data") or {}).get("datasourceId") is None and not (n.get("data") or {}).get("parquetKey")
                  and not draft.consumers(n["id"])), None)
    if empty is not None:
        empty["data"] = {**(empty.get("data") or {}), **data}
        draft._touch(empty["id"])
        node_id = empty["id"]
    else:
        lowest = max([(n.get("position") or {}).get("y", 0) for n in draft.nodes if not n.get("parentNode")], default=-Y_STEP)
        node_id = draft.next_id("src-")
        draft.add({"id": node_id, "type": "source", "position": draft.free_position(60, lowest + Y_STEP), "data": data})
    return {"node": node_id, "datasource": data["filename"], "rows": data["rows"],
            "columns": [{"name": c.get("name"), "type": c.get("dtype")} for c in columns if isinstance(c, dict)]}


def _data_node(draft: FlowDraft, node_id: str, role: str) -> dict:
    node = draft.get(node_id)
    if node.get("type") not in DATA_NODES:
        raise ModelRetry(f"{role} must be a source or a step, and '{node_id}' is a {node.get('type')}.")
    return node


@flow_tool(reads_data=False)
async def add_step(ctx: RunContext[FlowDeps], after: str, operation: str, params: dict[str, Any],
                   right: Optional[str] = None) -> dict[str, Any]:
    """Add an operation that reads the result of node `after` (and, for join or union, of node `right`). Returns the new node id with its columns and first rows, or the error the preview gave."""
    draft = ctx.deps.draft
    parent = _data_node(draft, after, "after")
    if parent.get("parentNode"):
        raise ModelRetry("Steps inside a loop container are edited by hand.")
    clean = clean_params(operation, params)
    if operation in ("join", "union"):
        if not right:
            raise ModelRetry(f"{operation} needs right: the node whose rows it brings in.")
        _data_node(draft, right, "right")
        if right == after:
            raise ModelRetry("right must be another node than after.")
    elif right:
        raise ModelRetry("Only join and union take a right input.")
    node_id = draft.next_id("op-")
    pos = parent.get("position") or {"x": 60, "y": 140}
    draft.add({"id": node_id, "type": "operation", "position": draft.free_position(pos.get("x", 60) + X_STEP, pos.get("y", 140)),
               "data": {"opType": operation, "params": clean}})
    draft.link(after, node_id, "left")
    if right:
        draft.link(right, node_id, "right")
    return await _step_result(ctx.deps, node_id)


@flow_tool(reads_data=False)
async def change_step(ctx: RunContext[FlowDeps], node: str, params: dict[str, Any], operation: Optional[str] = None) -> dict[str, Any]:
    """Replace the settings of a step (all its params; `operation` changes its kind too). Returns its columns and first rows, or the error."""
    draft = ctx.deps.draft
    target = draft.get(node)
    if target.get("type") != "operation":
        raise ModelRetry(f"'{node}' is not a step: only operation nodes have params.")
    if target.get("parentNode"):
        raise ModelRetry("Steps inside a loop container are edited by hand.")
    data = target.get("data") or {}
    op = operation or data.get("opType")
    clean = clean_params(op, params)
    if op in ("join", "union") and not draft.inputs(node).get("right"):
        raise ModelRetry(f"{op} needs a right input: connect one with connect(..., input='right').")
    target["data"] = {**data, "opType": op, "params": clean}
    draft._touch(node)
    return await _step_result(ctx.deps, node)


@flow_tool(reads_data=False)
async def connect(ctx: RunContext[FlowDeps], from_node: str, to_node: str, input: Literal["left", "right"] = "left") -> dict[str, Any]:
    """Make `to_node` read `from_node` on its left (main) or right (join/union) input, replacing what it read there."""
    draft = ctx.deps.draft
    source = _data_node(draft, from_node, "from_node")
    target = draft.get(to_node)
    if target.get("type") not in ("operation", "output"):
        raise ModelRetry("Only steps and Outputs read other nodes.")
    if source.get("parentNode") or target.get("parentNode"):
        raise ModelRetry("Loop containers and the steps inside them are edited by hand.")
    if input == "right" and (target.get("data") or {}).get("opType") not in ("join", "union"):
        raise ModelRetry("Only a join or a union has a right input.")
    if to_node == from_node or to_node in draft.upstream(from_node):
        raise ModelRetry("That connection would make a loop.")
    draft.link(from_node, to_node, input)
    draft._touch(to_node)
    return {"connected": f"{from_node} → {to_node} ({input})"}


@flow_tool(reads_data=False)
async def remove_node(ctx: RunContext[FlowDeps], node: str) -> dict[str, Any]:
    """Remove a node. The nodes that read it read what it read instead, so the chain stays connected."""
    draft = ctx.deps.draft
    target = draft.get(node)
    if target.get("type") == "foreach" or any(n.get("parentNode") == node for n in draft.nodes) or target.get("parentNode"):
        raise ModelRetry("Loop containers and the steps inside them are edited by hand.")
    upstream = draft.inputs(node).get("left")
    readers = draft.consumers(node)
    draft.nodes = [n for n in draft.nodes if n["id"] != node]
    draft.edges = [e for e in draft.edges if node not in (e.get("source"), e.get("target"))]
    reconnected = []
    if upstream:
        for e in readers:
            draft.link(upstream, e["target"], e.get("targetHandle") or "left")
            reconnected.append(e["target"])
    draft.touched = [t for t in draft.touched if t != node]
    return {"removed": node, "now_reading": {t: upstream for t in reconnected}}


def _writable_folders(session, user) -> dict[int, str]:
    projects = perm_service._all_projects(session)
    out = {}
    for pid in sorted(projects):
        if perm_service.has_capability(session, user, pid, Capability.EDIT):
            chain = perm_service.ancestor_ids({i: p.parent_id for i, p in projects.items()}, pid)
            out[pid] = "/".join(projects[i].name for i in reversed(chain) if i in projects)
    return out


@flow_tool(reads_data=False)
async def list_folders(ctx: RunContext[FlowDeps]) -> dict[str, Any]:
    """The folders where this user can save a datasource, with their path: where an Output may write."""
    with ctx.deps.session_factory() as session:
        folders = _writable_folders(session, ctx.deps.user)
    return {"folders": [{"id": i, "path": p} for i, p in list(folders.items())[:200]], "flow_folder_id": ctx.deps.project_id}


@flow_tool(reads_data=False)
async def add_output(ctx: RunContext[FlowDeps], after: str, datasource_name: str, folder_id: Optional[int] = None,
                     overwrite: bool = False) -> dict[str, Any]:
    """Add an Output that saves the result of `after` as a datasource when the flow runs. `folder_id` defaults to the flow's folder. With overwrite, a datasource of that name in the folder gets the new data at each run; without, a run makes a datasource with a numbered name."""
    deps, draft = ctx.deps, ctx.deps.draft
    parent = _data_node(draft, after, "after")
    name = (datasource_name or "").strip()
    if not name:
        raise ModelRetry("The Output needs datasource_name.")
    folder = folder_id or deps.project_id
    if folder is None:
        raise ModelRetry("This flow is not saved in a folder yet: pass folder_id (see list_folders).")
    with deps.session_factory() as session:
        if session.get(Project, folder) is None or not perm_service.has_capability(session, deps.user, folder, Capability.EDIT):
            raise ModelRetry(f"The user cannot save datasources in folder {folder}: see list_folders.")
    node_id = draft.next_id("out-")
    pos = parent.get("position") or {"x": 60, "y": 140}
    draft.add({"id": node_id, "type": "output", "position": draft.free_position(pos.get("x", 60) + X_STEP, pos.get("y", 140)),
               "data": {"destType": "datasource", "mode": "append", "name": name, "projectId": folder, "overwrite": bool(overwrite)}})
    draft.link(after, node_id, "left")
    return {"node": node_id, "writes": {"datasource": name, "folder_id": folder, "overwrite": bool(overwrite)}}


@flow_tool(reads_data=True)
async def preview(ctx: RunContext[FlowDeps], node: str, rows: int = 10) -> dict[str, Any]:
    """Columns and the first rows (at most 30) of a node's result, on the development sample of the data."""
    ctx.deps.draft.get(node)
    res = await preview_node(ctx.deps, node, max(1, min(int(rows or 10), MAX_PREVIEW_ROWS)))
    if "error" in res:
        return {"node": node, "error": res["error"]}
    return {"datasource": f"node {node}", **res}


def parse_definition(text: str, counters: Optional[dict[str, int]] = None) -> FlowDraft:
    """The draft from the editor's text, or a 422 that says why not."""
    if len(text or "") > MAX_DEFINITION_CHARS:
        raise HTTPException(status_code=422, detail="The flow is too large for the assistant")
    try:
        return FlowDraft(json.loads(text or "{}"), counters)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail=f"The flow definition is not readable: {e}")


def flow_of(session, flow_id: int | None) -> Optional[Flow]:
    return session.get(Flow, flow_id) if flow_id is not None else None
