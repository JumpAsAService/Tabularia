"""Tabularia's AI assistant (pydantic-ai): chat over the catalog's data.

The Assistant page's agent takes its tools from the agent's registry (services/agent:
the same as over MCP, built in services/agent/chat.py): it reads, and builds through
plans the person confirms. Its dependencies, its instructions and these three reading
tools are here — the registry's `list_datasources`, `describe_datasource` and
`query_datasource` are these functions, and the flow editor's assistant uses the first
two as well.

The three, all under the SAME access control as the rest of the product — the
assistant sees and queries only the datasources the user can READ (VIEW capability on
the folder, inherited):

- `list_datasources`     what is in the catalog, for this user;
- `describe_datasource`  columns, types and the FIELD DESCRIPTIONS curated by hand
                         (the semantic context that gets the right query written),
                         plus a few sample rows;
- `query_datasource`     a SELECT on the datasource, run by the engine with the
                         `sql` node (table = `self`): the same protections as an
                         editor's SQL node, limited rows, and an audit event for
                         every query — it is a data access.

The numbers in the answers must come from a query: the model does not make them up,
and the chat shows the table the tool returned anyway.
"""
from __future__ import annotations

import json
import logging
import os
import re
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from fastapi import Request

os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")  # no banner in the gateway's logs

from pydantic_ai import ModelRetry, RunContext  # noqa: E402
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.models import Datasource, Project, User
from app.services import ai_prompts, audit
from app.services.permissions import readable_project_ids

logger = logging.getLogger(__name__)

MAX_LISTED = 60
LIST_DESCRIPTION_CHARS = 280  # the beginning is enough in the listing: the whole text comes from describe_datasource
MAX_CELL_CHARS = 300
SAMPLE_ROWS = 5

# the `sql` node's SQL dialect per engine, and how "difficult" names are quoted
_DIALECTS = {
    "polars": ("Polars SQL (a PostgreSQL-like subset: no window frames, keep it simple)", '"'),
    "duckdb": ("DuckDB SQL", '"'),
    "chdb": ("ClickHouse SQL", "`"),
    "clickhouse": ("ClickHouse SQL", "`"),
    "bigquery": ("GoogleSQL (BigQuery)", "`"),
}
# SELECT only: the `sql` node wraps the query in a WITH of its own already
# (input/self), so a query that STARTS with WITH is not valid on any engine
_SELECT_ONLY = re.compile(r"^\s*select\b", re.IGNORECASE)


@dataclass
class ChatDeps:
    """What the tools may touch: the user (for the permissions), a DB session factory (one
    per call: the stream lives longer than the request), the chosen engine and the HTTP
    request for the audit."""

    user: User
    session_factory: Callable[[], AbstractContextManager[Session]]
    engine: str | None
    model_id: str
    locale: str = "en"
    request: Request | None = None
    # the conversation going on: identifies the preview's slot (see _engine_preview).
    # `None` on a new chat.
    chat_id: int | None = None
    # the preview slot's key when the chat does NOT exist (observers: their conversations
    # are not saved). Without it, two visitors of the shared account would end up on the
    # same slot and cancel each other's queries.
    slot_key: str | None = None
    # the datasources the user put in focus on the page (filtered on their permissions
    # already): [{id, name}]. Only a context hint: every tool checks the access again
    # anyway.
    focus: list[dict[str, Any]] | None = None
    # the text of the instructions in force (services/ai_prompts.py), read by the route
    prompt: str = ""
    # ── the Assistant page's agent (services/agent/chat.py) ──
    # the registry's tools it is given: `read` (an observer) or `write`
    level: Literal["read", "write"] = "read"
    # an observer's conversation is saved nowhere: no chat is ever created for it
    ephemeral: bool = False
    # the person's message: the title of a conversation created before its turn ends
    question: str = ""
    # the conversation's plans in words, for the context (oldest first)
    plans: list[str] | None = None
    # the turn so far: the registry's context, the plan it touched, the questions it asked
    tools: Any = None
    plan_id: int | None = None
    questions: list[dict[str, Any]] | None = None


def _columns(ds: Datasource) -> list[dict[str, Any]]:
    try:
        cols = json.loads(ds.columns or "[]")
        descr = json.loads(ds.column_descriptions or "{}")
    except ValueError:
        cols, descr = [], {}
    out = []
    for c in cols if isinstance(cols, list) else []:
        name = str(c.get("name", ""))
        item = {"name": name, "type": str(c.get("dtype", ""))}
        if isinstance(descr, dict) and descr.get(name):
            item["description"] = str(descr[name])
        out.append(item)
    return out


def _readable_datasource(session: Session, user: User, datasource_id: int) -> Datasource:
    """The datasource, IF the user can read it. A datasource that does not exist and one
    they cannot see give the same error: the assistant must not reveal what is outside
    their permissions."""
    ds = session.get(Datasource, datasource_id)
    if ds is None or ds.project_id not in readable_project_ids(session, user):
        raise ModelRetry(f"No datasource with id {datasource_id} is available to this user. Call list_datasources first.")
    return ds


def readable_focus(session: Session, user: User, ids: list[int]) -> list[dict[str, Any]]:
    """The datasources the user put in focus, narrowed to those they can read."""
    if not ids:
        return []
    readable = readable_project_ids(session, user)
    out = []
    for ds_id in ids[:8]:
        ds = session.get(Datasource, ds_id)
        if ds is not None and ds.project_id in readable and ds.key:
            out.append({"id": ds.id, "name": ds.name})
    return out


def _short(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= LIST_DESCRIPTION_CHARS else text[:LIST_DESCRIPTION_CHARS].rstrip() + "…"


def _trim(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        out.append({k: (v[:MAX_CELL_CHARS] + "…" if isinstance(v, str) and len(v) > MAX_CELL_CHARS else v) for k, v in r.items()})
    return out


async def _engine_preview(ds: Datasource, operations: list[dict], limit: int, deps: ChatDeps) -> dict[str, Any]:
    """A preview on the engine, as the Viewer would make it (without step cache).

    The slot is per CONVERSATION, not per user: it serves to let the newest question
    win *in the same chat*, when someone rephrases while the answer is coming. Tied to
    the user, two conversations in parallel killed each other — two tabs open by the
    same person, or two people on a shared account, as in a public demo. New chat: no
    slot, because there is nothing to overtake."""
    body: dict[str, Any] = {
        "bucket": ds.bucket, "input_key": ds.key, "operations": operations,
        "limit": limit, "no_cache": True,
        # the ClickHouse engine runs these queries with the dedicated `ai` user, when
        # configured (CLICKHOUSE_EXTERNAL__AI_USERNAME/__AI_PASSWORD)
        "principal": "ai",
    }
    key = deps.chat_id or deps.slot_key
    if key:
        body["slot"] = f"u{deps.user.id}:c{key}:ai"
    if deps.engine:
        body["engine"] = deps.engine
    try:
        sort_keys = json.loads(ds.sort_keys or "[]")
        if sort_keys:
            body["sort_keys"] = sort_keys
    except ValueError:
        pass
    resp = await get_engine_client().post("/tasks/preview", json=body, timeout=180)
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail")
        except ValueError:
            detail = resp.text
        raise _EngineRefusal(resp.status_code, str(detail)[:1500])
    return resp.json()


class _EngineRefusal(Exception):
    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def dialect_of(engine: Optional[str]) -> tuple[str, str]:
    return _DIALECTS.get((engine or "polars").lower(), _DIALECTS["polars"])


def context(engine: Optional[str], locale: str, focus: list[dict[str, Any]] | None = None,
            level: str = "write", plans: list[str] | None = None) -> str:
    """What Tabularia adds to the text at every request (services/ai_prompts.py)."""
    dialect, quote = dialect_of(engine)
    lines = [
        ai_prompts.CONTEXT_HEADER,
        f"- {ai_prompts.today()}",
        f"- SQL dialect: {dialect}. Quote column names that contain spaces or symbols with {quote}.",
        f"- Query results are capped at {get_settings().ai.max_result_rows} rows.",
    ]
    if level != "write":
        lines.append("- This user can only read: the tools that build, change, run or schedule are not available. "
                     "If they ask for a change, say they need an account that may make changes.")
    if focus:
        names = ", ".join(f"{d['name']} (id {d['id']})" for d in focus)
        lines.append(f"- The user has selected these datasources as the scope of the conversation: {names}. "
                     "Work on them, skipping list_datasources, unless the user asks about something else.")
    if plans:
        lines.append("- The plans of this conversation, as they stand now (the user confirms or cancels them with the "
                     "buttons of their cards, outside your turns):")
        lines += [f"  - {p}" for p in plans]
    return "\n".join(lines) + "\n\n" + ai_prompts.language_line(locale)


def _instructions(ctx: RunContext[ChatDeps]) -> str:
    deps = ctx.deps
    return ai_prompts.compose(deps.prompt or ai_prompts.ASSISTANT,
                              context(deps.engine, deps.locale, deps.focus, deps.level, deps.plans))


def _words(text: str) -> str:
    """Text reduced to lowercase words: `anagrafica_test`, «Anagrafica di test» and
    `ANAGRAFICA-TEST` must meet."""
    return " ".join(re.sub(r"[\W_]+", " ", text.lower()).split())


_STOPWORDS = {"di", "del", "della", "dei", "the", "of", "de", "la", "le", "il", "lo", "der", "die", "das", "el", "los", "las", "du", "des"}


async def list_datasources(ctx: RunContext[ChatDeps], search: str = "") -> dict[str, Any]:
    """List the datasources this user can read, each with the description its owners wrote. `search` is matched word by word against name and description, ignoring case, underscores and dashes; leave it empty to list everything. If nothing matches, everything available is returned with a note."""
    deps = ctx.deps
    with deps.session_factory() as session:
        readable = readable_project_ids(session, deps.user)
        if not readable:
            return {"datasources": [], "note": "This user has no readable datasource."}
        rows = session.exec(select(Datasource).where(Datasource.project_id.in_(readable))).all()  # type: ignore[attr-defined]
        projects = {p.id: p.name for p in session.exec(select(Project)).all()}
        items = []
        for ds in sorted(rows, key=lambda d: d.name.lower()):
            if not ds.key:  # a database datasource never ingested: no data to query
                continue
            items.append((_words(f"{ds.name} {ds.description or ''}"), {
                "id": ds.id, "name": ds.name, "folder": projects.get(ds.project_id, ""),
                "rows": ds.rows, "columns": len(_columns(ds)), "description": _short(ds.description or ""),
            }))
    tokens = [t for t in _words(search).split() if len(t) > 1 and t not in _STOPWORDS]
    if not tokens:
        return {"datasources": [item for _, item in items][:MAX_LISTED]}
    hits = [item for hay, item in items if all(t in hay for t in tokens)] or [item for hay, item in items if any(t in hay for t in tokens)]
    if hits:
        return {"datasources": hits[:MAX_LISTED]}
    # no match: better to show everything than to leave the model guessing
    return {"datasources": [item for _, item in items][:MAX_LISTED],
            "note": f"Nothing matches '{search}'. These are ALL the datasources available to the user: pick from them or say none fits."}


async def describe_datasource(ctx: RunContext[ChatDeps], datasource_id: int) -> dict[str, Any]:
    """Columns (name, type, and the curated description of what each field means), row count and a few sample rows of one datasource."""
    deps = ctx.deps
    with deps.session_factory() as session:
        ds = _readable_datasource(session, deps.user, datasource_id)
        info: dict[str, Any] = {
            "id": ds.id, "name": ds.name, "description": ds.description or "", "rows": ds.rows,
            "columns": _columns(ds),
        }
        snapshot = Datasource(id=ds.id, name=ds.name, project_id=ds.project_id, bucket=ds.bucket, key=ds.key, sort_keys=ds.sort_keys)
    try:
        res = await _engine_preview(snapshot, [], SAMPLE_ROWS, deps)
        info["sample_rows"] = _trim(res.get("rows", []))
    except Exception as e:  # noqa: BLE001 — the sample rows are a help, not a requirement
        logger.info("assistant: sample rows of %s not available (%s)", datasource_id, e)
    return info


def _keys(text: Optional[str]) -> list[str]:
    return [c.strip() for c in (text or "").replace(";", ",").split(",") if c.strip()]


def _join_operation(session, user, ds_id, left_on, right_on, how):
    """The join operation towards a SECOND datasource, or `(None, None)`.

    The whole security is here: the right side arrives as an ID and is resolved with
    `_readable_datasource`, that is with the same permissions as everything else. The
    model sees neither buckets nor keys, and the SQL keeps reading only `self` — which
    after this operation is the result joined already. The sql node's allowlist stays
    what it is."""
    if ds_id is None:
        return None, None
    left, right = _keys(left_on), _keys(right_on)
    if not left or not right:
        raise ModelRetry("To join, give both join_left_on and join_right_on: the key columns on each side.")
    if len(left) != len(right):
        raise ModelRetry(
            f"The join keys must match in number: {len(left)} on the left, {len(right)} on the right."
        )
    other_one = _readable_datasource(session, user, ds_id)
    params = {
        "right": {"source": {"bucket": other_one.bucket, "key": other_one.key}, "operations": []},
        "left_on": left,
        "right_on": right,
        "how": how or "inner",
    }
    return {"type": "join", "params": params}, other_one.name


CHART_TYPES = ("bar", "hbar", "line", "area", "pie", "donut", "scatter")




def _chart_spec(chart: Optional[Any], columns: list[dict[str, Any]]) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Checks the chart's spec against the columns the query really returned.

    A chart that names a column that does not exist is worse than no chart: the page
    would draw emptiness and the user would not know why. It is refused with a reason,
    which goes back to the model."""
    if not chart:
        return None, None
    kind = str(chart.get("type") or "").lower()
    if kind not in CHART_TYPES:
        return None, f"Unknown chart type {kind!r}. Use one of: {', '.join(CHART_TYPES)}."
    names = {str(c.get("name")) for c in columns if isinstance(c, dict)}
    x, y = str(chart.get("x") or ""), str(chart.get("y") or "")
    missing = [n for n in (x, y) if n not in names]
    if missing:
        return None, (
            f"The chart names columns the query does not return: {', '.join(missing)}. "
            f"Available: {', '.join(sorted(names))}."
        )
    series = chart.get("series")
    if series is not None and str(series) not in names:
        return None, f"The series column {series!r} is not in the result."
    spec = {"type": kind, "x": x, "y": y}
    if series is not None:
        spec["series"] = str(series)
    title = chart.get("title")
    if title:
        spec["title"] = str(title)[:120]
    return spec, None


async def query_datasource(
    ctx: RunContext[ChatDeps], datasource_id: int, sql: str, limit: int = 50,
    join_datasource_id: Optional[int] = None,
    join_left_on: Optional[str] = None,
    join_right_on: Optional[str] = None,
    join_how: Optional[Literal["inner", "left", "full"]] = None,
    chart_type: Optional[Literal["bar", "hbar", "line", "area", "pie", "donut", "scatter"]] = None,
    chart_x: Optional[str] = None,
    chart_y: Optional[str] = None,
    chart_series: Optional[str] = None,
    chart_title: Optional[str] = None,
) -> dict[str, Any]:
    """Run ONE read-only SELECT on a datasource. The table is named `self` (e.g. `SELECT paese, COUNT(*) AS n FROM self GROUP BY paese ORDER BY n DESC`). Returns columns, rows (capped) and whether the result was truncated.

    To answer a question that spans TWO datasources, set `join_datasource_id` to the second one together with `join_left_on` and `join_right_on` (the key columns on each side, comma-separated if the key has several parts). The two are joined before your SQL runs, and `self` is the joined result — so write the query against the columns of both. `join_how` is inner by default; use left to keep rows that have no match. A column that exists on both sides and is not a key gets the suffix `_right`. Call `describe_datasource` on both first, so you join on columns that exist.

    To draw the result as a chart under the table, set `chart_type` together with `chart_x` (the categories) and `chart_y` (the numeric column) — both must be columns your query actually returns. `chart_series` optionally splits the data into several series, `chart_title` is a short title in the user's language. Use bar for a ranking, hbar when the labels are long or the categories many, line or area for a series over time, pie or donut for shares of a total, scatter for two numeric measures. Leave them unset for a single number, for a couple of rows that already read fine, or for a non-numeric result: a chart of three rows is noise."""
    deps = ctx.deps
    cap = get_settings().ai.max_result_rows
    limit = max(1, min(int(limit or 50), cap))
    query = sql.strip().rstrip(";").strip()
    if not _SELECT_ONLY.match(query) or ";" in query:
        raise ModelRetry("Only ONE statement starting with SELECT is allowed (no leading WITH, no ';'). Use subqueries in FROM instead of CTEs.")
    if not re.search(r"\bfrom\s+(self|input)\b", query, re.IGNORECASE):
        raise ModelRetry("The query must read from the datasource: use `FROM self`.")
    with deps.session_factory() as session:
        ds = _readable_datasource(session, deps.user, datasource_id)
        snapshot = Datasource(id=ds.id, name=ds.name, project_id=ds.project_id, bucket=ds.bucket, key=ds.key, sort_keys=ds.sort_keys)
        # The second datasource is resolved from an ID under the SAME permissions: the model
        # never names a bucket or a key, so the join is not a way to read something outside
        # the asker's permissions.
        join_op, join_name = _join_operation(
            session, deps.user, join_datasource_id, join_left_on, join_right_on, join_how,
        )
    outcome, n_rows = "success", 0
    try:
        operations = ([join_op] if join_op else []) + [{"type": "sql", "params": {"query": query}}]
        res = await _engine_preview(snapshot, operations, limit, deps)
        rows = _trim(res.get("rows", []))
        n_rows = len(rows)
        columns = res.get("columns", [])
        payload: dict[str, Any] = {
            "datasource": f"{snapshot.name} ⋈ {join_name}" if join_name else snapshot.name,
            "columns": columns, "rows": rows,
            "row_count": n_rows, "truncated": bool(res.get("truncated")),
        }
        # the chart is the user's: it is validated, nothing new runs
        spec, reason = _chart_spec(
            {"type": chart_type, "x": chart_x, "y": chart_y, "series": chart_series, "title": chart_title}
            if chart_type else None,
            columns,
        )
        if spec:
            payload["chart"] = spec
        elif reason:
            # the model is told WHY, so at the next question it chooses better
            payload["chart_error"] = reason
        return payload
    except _EngineRefusal as e:
        outcome = "failure"
        if e.status >= 500:
            return {"error": f"The engine failed ({e.status}): {e.detail}"}
        # an SQL or column error: the model can correct itself
        raise ModelRetry(f"The engine refused the query: {e.detail}") from e
    finally:
        with deps.session_factory() as session:
            audit.record_audit(
                session, actor=deps.user, action=audit.AI_QUERY, outcome=outcome,
                target_type="datasource", target_id=snapshot.id, target_label=snapshot.name,
                detail={"sql": query[:2000], "model": deps.model_id, "engine": deps.engine or "default", "rows": n_rows},
                request=deps.request,
            )


def build_model(model_id: str):
    """The pydantic-ai model for a provider id. Kept apart (and replaceable) because the
    tests must not talk to a real provider."""
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    cfg = get_settings().ai
    provider = OpenAIProvider(base_url=cfg.base_url, api_key=cfg.secret_key.get_secret_value())
    return OpenAIChatModel(model_id, provider=provider)


# ── the conversation's title ─────────────────────────────────────────────────
TITLE_MAX_CHARS = 60


async def suggest_title(model_id: str, question: str, locale: str = "en", prompt: str = "") -> tuple[str | None, Any]:
    """A short title for the conversation, asked of the model.

    It is worth a tiny call once only, at the first turn: the title then stays forever
    in the list, and «How many rows does the 2025 orders table have split by...» cut at
    60 characters helps find nothing.

    Returns `(title, usage)`; the title is `None` at any hitch and the caller keeps the
    fallback it has already (the shortened question). A title is not worth an error in
    the user's face. The usage comes back with it because this too is a paid call: the
    conversation's total must include it."""
    from pydantic_ai import Agent
    from pydantic_ai.usage import UsageLimits

    question_text = (question or "").strip()
    if not question_text:
        return None, None
    try:
        agent = Agent(
            build_model(model_id),
            instructions=ai_prompts.compose(prompt or ai_prompts.TITLE, ai_prompts.language_line(locale, "the title")),
        )
        payload = await agent.run(question_text[:1000], usage_limits=UsageLimits(request_limit=1))
        usage = payload.usage
        usage = usage() if callable(usage) else usage
        title = " ".join(str(payload.output or "").split()).strip(" \"'«»`.")
        if not title:
            return None, usage
        return title[:TITLE_MAX_CHARS].rstrip(" ,;:-"), usage
    except Exception:  # noqa: BLE001 — an extra: it must never disturb the answer
        logger.info("assistant: title not generated for the chat (model %s)", model_id, exc_info=True)
        return None, None
