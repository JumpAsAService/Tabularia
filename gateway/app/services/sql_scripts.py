"""SQL scripts: who sees them, what they hold, how a run on the catalog is prepared.
docs/specs/sql-scripts.md; the object in models/sql_script.py, the routes in
routes/sql_scripts.py.

Who sees a SQL script: VIEW on its folder (administrators: every one). Whoever cannot
see it gets 404, as if it did not exist; EDIT on the folder changes, moves and deletes it.

A run reads with the authority of whoever RUNS it, never of the author: a shared script
is a shared text, not shared access. On the catalog the SQL names datasources by their
name (or folder and name); the gateway resolves each among the datasources the runner may
read, and changes the text only where they are named — `"Vendite Nord 2024"` becomes
`ds_12 AS "Vendite Nord 2024"` — so the engine gets aliases it can check against an
allowlist, and never a bucket or a key the person wrote.

A version's `content`: {"sql": "SELECT …", "engine": "duckdb" | null, "connection_id": null}.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import sqlglot
from fastapi import HTTPException, Request
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlmodel import Session, select

from app.core.engine_client import get_engine_client
from app.models import Datasource, SqlScript, SqlScriptVersion, User
from app.models.permission import Capability
from app.services import audit
from app.services import permissions as perm_service
from app.services.engine_policy import KNOWN_ENGINES, pick_engine
from app.services.permissions import is_admin

MAX_SQL_CHARS = 100_000
MAX_ROWS = 1000  # the engine's preview cap
DEFAULT_ROWS = 200

# the dialect the SQL is READ in, to find the datasources it names; the engine runs it
DIALECTS = {"polars": "postgres", "duckdb": "duckdb", "chdb": "clickhouse", "clickhouse": "clickhouse",
            "bigquery": "bigquery"}


def empty_content() -> dict:
    return {"sql": "", "engine": None, "connection_id": None}


# ── who sees what ────────────────────────────────────────────────────────────
def can_see(session: Session, user: User, script: SqlScript) -> bool:
    return is_admin(session, user) or perm_service.has_capability(session, user, script.project_id, Capability.VIEW)


def visible_query(session: Session, user: User, project_ids: Optional[set[int]] = None):
    """A select of the SQL scripts this person sees (in `project_ids` when given), or None
    when there are none."""
    stmt = select(SqlScript)
    if not is_admin(session, user):
        readable = set(perm_service.readable_project_ids(session, user))
        if project_ids is not None:
            readable &= set(project_ids)
        if not readable:
            return None
        stmt = stmt.where(SqlScript.project_id.in_(readable))  # type: ignore[attr-defined]
    elif project_ids is not None:
        stmt = stmt.where(SqlScript.project_id.in_(project_ids))  # type: ignore[attr-defined]
    return stmt.order_by(SqlScript.name)


def get_visible(session: Session, user: User, script_id: int) -> SqlScript:
    """The script, or 404: one that cannot be seen does not exist for this person."""
    script = session.get(SqlScript, script_id)
    if script is None or not can_see(session, user, script):
        raise HTTPException(status_code=404, detail="SQL script not found")
    return script


# ── what a script may hold ───────────────────────────────────────────────────
def check_content(content: Any) -> dict:
    """The content as it will be saved, or a 422 saying what is wrong."""
    if not isinstance(content, dict):
        raise HTTPException(status_code=422, detail="The script's content is not an object")
    sql = content.get("sql", "")
    if not isinstance(sql, str):
        raise HTTPException(status_code=422, detail="The script's SQL is a text")
    if len(sql) > MAX_SQL_CHARS:
        raise HTTPException(status_code=422, detail=f"The SQL is too long: at most {MAX_SQL_CHARS:,} characters")
    engine = content.get("engine") or None
    if engine is not None and engine not in KNOWN_ENGINES:
        raise HTTPException(status_code=422, detail=f"Unknown engine: {engine}")
    if content.get("connection_id") is not None:
        raise HTTPException(status_code=422, detail="Running SQL on a connection is not available yet")
    return {"sql": sql, "engine": engine, "connection_id": None}


# ── versions ─────────────────────────────────────────────────────────────────
def version_row(session: Session, script: SqlScript, version: Optional[int] = None) -> Optional[SqlScriptVersion]:
    return session.exec(select(SqlScriptVersion).where(
        SqlScriptVersion.script_id == script.id,
        SqlScriptVersion.version == (version or script.current_version))).first()


def content_of(session: Session, script: SqlScript, version: Optional[int] = None) -> dict:
    row = version_row(session, script, version)
    if row is None:
        return empty_content()
    try:
        return {**empty_content(), **json.loads(row.content)}
    except ValueError:
        return empty_content()


def add_version(session: Session, script: SqlScript, content: dict, note: str, user: User) -> SqlScriptVersion:
    """The next version, which becomes the current one."""
    last = session.exec(select(SqlScriptVersion.version).where(SqlScriptVersion.script_id == script.id)
                        .order_by(SqlScriptVersion.version.desc())).first() or 0  # type: ignore[attr-defined]
    row = SqlScriptVersion(script_id=script.id, version=last + 1, content=json.dumps(content), note=note,
                           created_by=user.id)
    session.add(row)
    script.current_version = row.version
    session.add(script)
    return row


# ── the catalog a person may query ───────────────────────────────────────────
def folder_names(session: Session) -> dict[int, str]:
    return {i: p.name for i, p in perm_service._all_projects(session).items()}


def readable_datasources(session: Session, user: User) -> list[Datasource]:
    readable = perm_service.readable_project_ids(session, user)
    if not readable:
        return []
    return list(session.exec(select(Datasource).where(Datasource.project_id.in_(readable))  # type: ignore[attr-defined]
                             .order_by(Datasource.name)).all())


# ── from the person's SQL to the engine's ────────────────────────────────────
def strip_comments(sql: str) -> str:
    """The SQL without its comments (`-- …` to the end of the line, `/* … */`), leaving
    quoted text alone. The engines that run on a server refuse comments in a query: they
    could hide something from their checks."""
    out, i, n = [], 0, len(sql)
    while i < n:
        c = sql[i]
        if c in "'\"`":
            j = i + 1
            while j < n:
                if sql[j] == c:
                    if j + 1 < n and sql[j + 1] == c:  # a doubled quote inside the text
                        j += 2
                        continue
                    break
                if sql[j] == "\\" and c == "'":
                    j += 2
                    continue
                j += 1
            out.append(sql[i:j + 1])
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            out.append(" ")
            i = n if j < 0 else j + 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _quote(name: str, dialect: str) -> str:
    if dialect == "bigquery":
        return "`" + name.replace("\\", "\\\\").replace("`", "\\`") + "`"
    return '"' + name.replace('"', '""') + '"'


@dataclass
class Resolved:
    query: str  # the SQL the engine runs: no comments, datasources as their aliases
    tables: dict[str, Datasource] = field(default_factory=dict)  # alias → datasource, in the order named


def _refuse(message: str) -> HTTPException:
    return HTTPException(status_code=422, detail=message)


def resolve(session: Session, user: User, sql: str, engine: Optional[str]) -> Resolved:
    """Finds the datasources the SQL names, among those `user` may read, and puts their
    aliases in their place. A datasource the person cannot read is reported as one that
    does not exist."""
    text = strip_comments(sql or "").strip()
    while text.endswith(";"):
        text = text[:-1].rstrip()
    if not text:
        raise _refuse("The query is empty")
    dialect = DIALECTS.get(engine or "polars", "postgres")
    try:
        trees = [t for t in sqlglot.parse(text, read=dialect) if t is not None]
    except SqlglotError as e:
        raise _refuse(f"The SQL could not be read: {str(e).splitlines()[0][:300]}") from None
    if len(trees) != 1:
        raise _refuse("One query at a time: write a single SELECT")
    tree = trees[0]
    if not isinstance(tree, exp.Query):
        raise _refuse("Only a query that reads data can run: start with SELECT or WITH")
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}

    by_name: dict[str, list[Datasource]] = {}
    for ds in readable_datasources(session, user):
        by_name.setdefault(ds.name.lower(), []).append(ds)
    folders = folder_names(session)

    edits: list[tuple[int, int, str]] = []
    out = Resolved(query=text)
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            continue  # a table function: the engine's allowlist decides
        name, folder = table.name, table.db
        if table.catalog:
            raise _refuse(f"«{table.sql(dialect=dialect)}»: name a datasource by its name, or by its folder and its name")
        if not folder and name.lower() in ctes:
            continue
        matches = by_name.get(name.lower(), [])
        if folder:
            matches = [d for d in matches if folders.get(d.project_id, "").lower() == folder.lower()]
        if len(matches) > 1:
            exact = [d for d in matches if d.name == name]
            matches = exact if len(exact) == 1 else matches
        label = f"{folder}.{name}" if folder else name
        if not matches:
            raise _refuse(f"There is no datasource «{label}» you can read")
        if len(matches) > 1:
            where = ", ".join(sorted(folders.get(d.project_id, "?") for d in matches))
            raise _refuse(f"«{name}» is in more than one folder ({where}): write it as "
                          f"{_quote(folders.get(matches[0].project_id, ''), dialect)}.{_quote(name, dialect)}")
        ds = matches[0]
        if not ds.key:
            raise _refuse(f"The datasource «{ds.name}» has no data yet")
        start_node = table.args["db"] if folder else table.this
        start, end = start_node.meta.get("start"), table.this.meta.get("end")
        if start is None or end is None:
            raise _refuse(f"«{label}» could not be placed in the SQL: write its name in quotes")
        if start > 0 and text[start - 1] in "\"`[":
            start -= 1
        if end + 1 < len(text) and text[end + 1] in "\"`]":
            end += 1
        alias = f"ds_{ds.id}"
        out.tables[alias] = ds
        edits.append((start, end, alias if table.alias else f"{alias} AS {_quote(name, dialect)}"))
    query = text
    for start, end, replacement in sorted(edits, reverse=True):
        query = query[:start] + replacement + query[end + 1:]
    out.query = query
    return out


def _engine_message(detail: str, resolved: Resolved) -> str:
    """The engine's refusal in the words of a SQL script: the aliases back to the names,
    and no talk of the `sql` node's input."""
    for alias, ds in resolved.tables.items():
        detail = re.sub(rf"\b{alias}\b", ds.name, detail)
    detail = re.sub(r"^sql: ", "", detail)
    return detail.replace(
        "the query can read only the input of the node (`self` or `input`). ", "the query can read only datasources. "
    ).replace(": to combine other data use a Join or Union node.", ".")


async def run(session: Session, user: User, sql: str, engine: Optional[str], limit: int,
              script: Optional[SqlScript] = None, request: Optional[Request] = None) -> dict[str, Any]:
    """Runs the SQL on the catalog, as a preview: the first `limit` rows. Every run is
    audited with its SQL, whatever its outcome."""
    engine = await pick_engine(session, engine)
    resolved = resolve(session, user, sql, engine)
    if not resolved.tables:
        raise _refuse("The query reads no datasource: name one in FROM")
    first = next(iter(resolved.tables.values()))
    body = {
        "bucket": first.bucket, "input_key": first.key, "limit": max(1, min(int(limit or DEFAULT_ROWS), MAX_ROWS)),
        "no_cache": True, "engine": engine,
        "operations": [{"type": "sql", "params": {
            "query": resolved.query,
            "tables": {alias: {"source": {"bucket": ds.bucket, "key": ds.key}} for alias, ds in resolved.tables.items()},
        }}],
        # one slot per script and person: a new run of the same script knocks down the previous
        "slot": f"u{user.id}:sql:{script.id if script is not None else 'draft'}",
    }
    started = time.monotonic()
    outcome, rows, detail = "failure", 0, ""
    try:
        resp = await get_engine_client().post("/tasks/preview", json=body, timeout=300)
        if resp.status_code >= 400:
            try:
                detail = str(resp.json().get("detail"))
            except ValueError:
                detail = resp.text
            status = 422 if resp.status_code < 500 else 502
            raise HTTPException(status_code=status, detail=_engine_message(detail, resolved)[:2000])
        res = resp.json()
        outcome, rows = "success", len(res.get("rows") or [])
    finally:
        audit.record_audit(
            session, actor=user, action=audit.SQL_SCRIPT_RUN, outcome=outcome, target_type="sql_script",
            target_id=script.id if script is not None else None, target_label=script.name if script is not None else "",
            detail={"sql": sql[:2000], "engine": engine, "rows": rows,
                    "datasources": [{"id": ds.id, "name": ds.name} for ds in resolved.tables.values()]},
            request=request,
        )
    return {
        "columns": res.get("columns") or [], "rows": res.get("rows") or [], "row_count": rows,
        "truncated": bool(res.get("truncated")), "engine": engine,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "datasources": [{"id": ds.id, "name": ds.name} for ds in resolved.tables.values()],
    }
