"""The agent's SQL scripts: reading one, trying SQL, and the plan's actions that create or
change a script, write its datasource and schedule that write.

The rules are the SQL scripts' own (services/sql_scripts.py, routes/sql_scripts.py): a
run reads with the authority of whoever runs it — on the catalog only the datasources
they may read, on a connection only with CONNECT on its folder; a script is changed with
EDIT on its folder; its datasource is written with RUN on the script's folder and EDIT on
the datasource's. They are checked when an action is planned and again when it is
applied, through the routes the interface uses.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlmodel import select

from app.core.config import get_settings
from app.models import Project, Run, SqlScript
from app.models.permission import Capability
from app.services import permissions as perm_service
from app.services import sql_script_runs, sql_scripts
from app.services.agent import plans
from app.services.agent.common import datasource_link, folder_paths, in_thread, trim_rows
from app.services.agent.planning import (
    change_lines, checked_cron, cron_words, flows_only, made_by, no_connection, planned_creation,
    planned_save, sql_script_link, text_diff, with_session,
)
from app.services.agent.registry import ToolContext, ToolError, tool

PLAN_PREVIEW_ROWS = 5


def _visible(session, user, script_id: int) -> SqlScript:
    try:
        return sql_scripts.get_visible(session, user, script_id)
    except HTTPException as e:
        raise ToolError(f"No SQL script {script_id} is available to this user.") from e


def _connection(session, user, ctx: ToolContext, connection_id: int):
    """A database connection a script may run on, for this user (CONNECT)."""
    conn = sql_scripts.queryable_connection(session, connection_id)
    if conn is None or not perm_service.has_capability(session, user, conn.project_id, Capability.CONNECT):
        raise no_connection(ctx, connection_id, "SQL (a database connection)")
    return conn


def _preview_slot(ctx: ToolContext) -> str:
    # one per call: a client's parallel calls, and the person's own editor, never knock each other down
    return f"u{ctx.user.id}:agent:{ctx.call_key}"


# ── reading ───────────────────────────────────────────────────────────────────
class SqlScriptIdArgs(BaseModel):
    sql_script_id: int


@tool("get_sql_script", title="Read a SQL script", args=SqlScriptIdArgs, level="read")
async def get_sql_script(ctx: ToolContext, a: SqlScriptIdArgs) -> dict:
    """A saved SQL script: its folder, its SQL, where it runs (the catalog with an engine, or a database connection), the datasource it writes, its schedule and its last write. To change it, plan_sql_script with its id."""
    from app.routes import sql_scripts as sql_routes

    def work(session, user):
        _visible(session, user, a.sql_script_id)
        d = sql_routes.get_sql_script(a.sql_script_id, user=user, session=session)
        paths = folder_paths(session)
        out: dict[str, Any] = {
            "id": d.id, "name": d.name, "description": d.description, "folder_id": d.project_id,
            "folder": paths.get(d.project_id, ""), "version": d.current_version, "sql": d.content.get("sql") or "",
            "runs_on": "connection" if d.connection else "catalog", "engine": d.engine,
            "schedule": d.run_schedule, "next_run_at": d.next_run_at,
            "can_edit": d.editable, "can_run": d.runnable, "link": sql_script_link(d.id),
        }
        if d.connection:
            out["connection"] = {"id": d.connection.id, "name": d.connection.name, "type": d.connection.db_type,
                                 "usable": d.connection.usable}
        if d.output:
            out["output"] = {"name": d.output["name"], "folder_id": d.output["project_id"],
                             "folder": paths.get(d.output["project_id"], "")}
        if d.datasource:
            out["datasource"] = {"id": d.datasource.id, "name": d.datasource.name, "rows": d.datasource.rows,
                                 "link": datasource_link(d.datasource.id, d.datasource.name)}
        last = session.exec(select(Run).where(Run.sql_script_id == d.id, Run.kind == "sql")
                            .order_by(Run.id.desc())).first()  # type: ignore[union-attr]
        if last is not None:
            out["last_write"] = {"run_id": last.id, "status": last.status, "at": last.started_at,
                                 **({"error": last.error[:500]} if last.error else {})}
        return out

    return await in_thread(ctx, work)


class RunSqlArgs(BaseModel):
    sql: str = Field(min_length=1, max_length=sql_scripts.MAX_SQL_CHARS, description="ONE SELECT (or WITH).")
    engine: Optional[str] = Field(None, max_length=40, description="On the catalog: the engine (default: the installation's).")
    connection_id: Optional[int] = Field(None, description="Run it on this database connection (its own SQL) instead of the catalog.")
    sql_script_id: Optional[int] = Field(None, description="The saved script this SQL is of, for its history.")
    limit: int = Field(50, ge=1, le=1000)


@tool("run_sql_preview", title="Try SQL", args=RunSqlArgs, level="read")
async def run_sql_preview(ctx: ToolContext, a: RunSqlArgs) -> dict:
    """Run ONE SELECT as a SQL script runs and return its first rows, without saving anything. On the catalog the SQL names datasources by their name — "Folder"."name" when two folders share one — among those the user may read, and reads them with the chosen engine's dialect (DuckDB, ClickHouse…). With connection_id it runs on that database, in its own dialect, with the user's CONNECT. Every run is recorded in the audit with its SQL."""
    cap = get_settings().ai.max_result_rows
    limit = max(1, min(a.limit, cap))

    async def work(session, user):
        script = _visible(session, user, a.sql_script_id) if a.sql_script_id is not None else None
        if a.connection_id is not None:
            _connection(session, user, ctx, a.connection_id)
        return await sql_scripts.run(session, user, a.sql, a.engine, limit, script=script, request=ctx.request,
                                     connection_id=a.connection_id, slot=_preview_slot(ctx))

    res = await with_session(ctx, work)
    out = {"columns": res.get("columns") or [], "rows": trim_rows(res.get("rows") or [], limit),
           "row_count": res.get("row_count"), "truncated": res.get("truncated"), "engine": res.get("engine"),
           "elapsed_ms": res.get("elapsed_ms")}
    if res.get("datasources"):
        out["datasources"] = res["datasources"]
    if res.get("connection"):
        out["connection"] = res["connection"]
    return out


# ── plan: create or change ────────────────────────────────────────────────────
class SqlOutputArgs(BaseModel):
    name: str = Field(min_length=1, max_length=200, description="The datasource's name.")
    folder_id: int = Field(description="Its folder (list_folders: can_edit).")


class PlanSqlScriptArgs(BaseModel):
    sql_script_id: Optional[int] = Field(None, description="A saved script to change; leave it out to create one.")
    folder_id: Optional[int] = Field(None, description="A new script: its folder (list_folders: can_edit).")
    name: Optional[str] = Field(None, max_length=200, description="A new script: its name (required). A saved one: a new name.")
    description: Optional[str] = Field(None, max_length=2000)
    sql: Optional[str] = Field(None, max_length=sql_scripts.MAX_SQL_CHARS, description="ONE SELECT (or WITH). On the catalog it names datasources by name; on a connection it is the database's own SQL. A new script: required; a saved one: leave it out to keep its SQL.")
    engine: Optional[str] = Field(None, max_length=40, description="Run on the catalog with this engine (polars, duckdb, chdb, clickhouse, bigquery, as allowed). Leave engine and connection_id out to keep where a saved script runs (a new one: the catalog, the default engine).")
    connection_id: Optional[int] = Field(None, description="Run on this database connection instead of the catalog (list_connections; the user needs CONNECT).")
    output: Optional[SqlOutputArgs] = Field(None, description="The datasource the script writes its whole result into, when plan_run_sql_script or its schedule runs it.")
    remove_output: bool = Field(False, description="A saved script: it no longer writes a datasource.")


def _words_of_target(content: dict, conn_name: Optional[str]) -> str:
    if content.get("connection_id") is not None:
        return f"on the database «{conn_name or content['connection_id']}»"
    return f"on the catalog ({content.get('engine') or 'default engine'})"


def _output_words(content: dict, paths: dict[int, str]) -> Optional[str]:
    out = sql_script_runs.output_of(content)
    return f"«{out['name']}» in {paths.get(out['project_id'], out['project_id'])}" if out else None


async def _plan_preview(ctx: ToolContext, content: dict) -> Optional[dict]:
    """The first rows of the script's SQL: what the person sees on the plan. A help,
    never a reason to refuse."""
    if not sql_scripts.strip_comments(content.get("sql") or "").strip().rstrip(";").strip():
        return None

    async def work(session, user):
        return await sql_scripts.run(session, user, content["sql"], content.get("engine"), PLAN_PREVIEW_ROWS,
                                     request=ctx.request, connection_id=content.get("connection_id"),
                                     slot=_preview_slot(ctx))

    try:
        res = await with_session(ctx, work)
    except HTTPException as e:
        return {"error": str(e.detail)[:500]}
    except Exception:  # noqa: BLE001
        return None
    return {"columns": res.get("columns") or [], "rows": trim_rows(res.get("rows") or [], PLAN_PREVIEW_ROWS)}


@tool("plan_sql_script", title="Plan a SQL script", args=PlanSqlScriptArgs, level="write")
async def plan_sql_script(ctx: ToolContext, a: PlanSqlScriptArgs) -> dict:
    """Add to the plan: create a SQL script (folder_id, name, sql) or change a saved one (sql_script_id and what changes: the plan shows the difference of the SQL). `output` makes it write its whole result into a datasource: plan_run_sql_script writes it now, plan_schedule (kind sql_script) on a schedule. Checked as the interface checks it — EDIT on the folder, CONNECT on a connection, EDIT on the datasource's folder — and its first rows are read for the plan. Planning the same script again replaces the earlier action. Nothing changes until the plan is confirmed."""
    from app.routes import sql_scripts as sql_routes

    if a.engine and a.connection_id is not None:
        raise ToolError("Give engine (the catalog) or connection_id (a database), not both.")
    if a.output is not None and a.remove_output:
        raise ToolError("Give output or remove_output, not both.")
    new = a.sql_script_id is None
    if new and (a.folder_id is None or not (a.name or "").strip() or a.sql is None):
        raise ToolError("A new SQL script needs folder_id, name and sql (or give sql_script_id to change a saved one).")
    # a change planned already for this script: the new one goes on from it, in its place
    pending = None if new else planned_save(ctx, "save_sql_script", a.sql_script_id)

    def check(session, user):
        paths = folder_paths(session)
        if new:
            if session.get(Project, a.folder_id) is None \
                    or not perm_service.has_capability(session, user, a.folder_id, Capability.EDIT):
                raise ToolError(f"The user cannot create SQL scripts in folder {a.folder_id}: see list_folders (can_edit).")
            script, current, folder_id = None, sql_scripts.empty_content(), a.folder_id
        else:
            script = _visible(session, user, a.sql_script_id)
            if not perm_service.has_capability(session, user, script.project_id, Capability.EDIT):
                raise ToolError(f"The user can read the SQL script «{script.name}» but not change it (it needs EDIT on its folder).")
            current, folder_id = sql_scripts.content_of(session, script), script.project_id
        content = dict(pending["definition"]) if pending else dict(current)
        if a.sql is not None:
            content["sql"] = a.sql
        if a.connection_id is not None:
            content["connection_id"], content["engine"] = a.connection_id, None
        elif a.engine:
            content["connection_id"], content["engine"] = None, a.engine.strip()
        if a.output is not None:
            content["output"] = {"name": a.output.name.strip(), "project_id": a.output.folder_id}
        elif a.remove_output:
            content["output"] = None
        if content.get("connection_id") is not None and content["connection_id"] != current.get("connection_id"):
            _connection(session, user, ctx, content["connection_id"])
        out = content.get("output")
        if out and out != current.get("output") and (
                session.get(Project, out["project_id"]) is None
                or not perm_service.has_capability(session, user, out["project_id"], Capability.EDIT)):
            raise ToolError(f"The user cannot write datasources in folder {out['project_id']}: see list_folders (can_edit).")
        name = sql_routes._name(session, folder_id, a.name if a.name is not None
                                else pending["name"] if pending else script.name,
                                exclude_id=script.id if script else None)
        checked = sql_routes._checked(session, user, content, None if new else current)
        conn = sql_scripts.queryable_connection(session, checked["connection_id"]) if checked["connection_id"] else None
        old_conn = sql_scripts.queryable_connection(session, current["connection_id"]) if current.get("connection_id") else None
        info: dict[str, Any] = {
            "folder": paths.get(folder_id, ""), "folder_id": folder_id, "name": name, "content": checked,
            "connection": conn.name if conn else None, "output": _output_words(checked, paths),
            "output_ref": ({"name": checked["output"]["name"], "folder": paths.get(checked["output"]["project_id"], "")}
                           if checked.get("output") else None),
        }
        if script is None:
            return info
        description = (a.description.strip() if a.description is not None
                       else pending["description"] if pending and pending.get("description") is not None
                       else script.description)
        changed = checked != current or name != script.name or description != script.description
        if not changed:
            raise ToolError(f"The plan would change nothing in the SQL script «{script.name}».")
        reasons = sql_scripts.widens(current, checked)
        notes = []
        if reasons and script.run_schedule and script.run_scheduled_by not in (None, user.id):
            notes.append("its schedule, set by someone else, is turned off")
        if reasons and any(g.run_schedule and g.run_scheduled_by not in (None, user.id)
                           for g in sql_scripts.groups_using(session, script.id)):
            notes.append("the schedules of the flow groups that run it, set by someone else, are turned off")
        lines = text_diff(current.get("sql") or "", checked.get("sql") or "")
        lines += change_lines([
            ("name", script.name, name),
            ("runs", _words_of_target(current, old_conn.name if old_conn else None),
             _words_of_target(checked, conn.name if conn else None)),
            ("writes the datasource", _output_words(current, paths), _output_words(checked, paths)),
        ])
        if description != script.description:
            lines.append("~ description changed")
        info.update(old_name=script.name, base=f"v{script.current_version}", diff_lines=lines, notes=notes,
                    description=description if description != script.description else None)
        return info

    info = await in_thread(ctx, check)
    content = info["content"]
    target = _words_of_target(content, info["connection"])
    if new:
        words = f"Create the SQL script «{info['name']}» in {info['folder']}, running {target}"
    else:
        n = len([x for x in info["diff_lines"] if x[:1] in "+-~"])
        words = f"Change the SQL script «{info['old_name']}» in {info['folder']} ({n} changed line{'s' if n != 1 else ''}), running {target}"
    if info["output"]:
        words += f"; it writes the datasource {info['output']}"
    if info.get("notes"):
        words += "; " + "; ".join(info["notes"])
    preview = await _plan_preview(ctx, content)
    if preview and preview.get("error"):
        words += f". Its SQL fails now: {preview['error'][:300]}"
    action: dict[str, Any] = {
        "kind": "save_sql_script", "id": a.sql_script_id, "folder_id": info["folder_id"], "folder": info["folder"],
        "name": info["name"], "engine": content.get("engine"), "connection": info["connection"],
        "output": info["output_ref"], "definition": content, "words": words + ".",
    }
    if new:
        action["description"] = (a.description or "").strip()
        action["code"] = content.get("sql") or ""  # whole: it is what will run
    else:
        action.update(base_hash=info["base"], diff_lines=info["diff_lines"], description=info["description"])
    if preview:
        action["preview"] = preview
    key = f"save_sql_script:{a.sql_script_id}" if not new else f"save_sql_script:new:{info['folder_id']}:{info['name'].lower()}"
    return plans.add_action(ctx, action, key=key)


@plans.action_kind("save_sql_script")
async def _apply_save_sql_script(ctx: ToolContext, a: dict, results: list[dict]) -> dict:
    from app.routes import sql_scripts as sql_routes

    def work(session, user):
        if a.get("id") is None:
            d = sql_routes.create_sql_script(a["folder_id"], sql_routes.SqlScriptCreate(
                name=a["name"], description=a.get("description") or "", content=a["definition"]),
                ctx.request, user=user, session=session)
            return d.id, d.name, "created"
        script = session.get(SqlScript, a["id"])
        if script is None:
            raise ToolError(f"SQL script {a['id']} no longer exists: nothing saved.")
        if f"v{script.current_version}" != a.get("base_hash"):
            raise ToolError(f"The SQL script «{script.name}» was changed in Tabularia after the plan was made: nothing "
                            "saved. Read it again (get_sql_script) and make a new plan.")
        d = sql_routes.update_sql_script(a["id"], sql_routes.SqlScriptUpdate(
            name=a["name"], description=a.get("description"), content=a["definition"]),
            ctx.request, user=user, session=session)
        return d.id, d.name, "changed"

    script_id, name, verb = await in_thread(ctx, work)
    return {"object": {"kind": "sql_script", "id": script_id, "name": name}, "link": sql_script_link(script_id),
            "words": f"SQL script «{name}» {verb}."}


# ── plan: write its datasource now, on a schedule ─────────────────────────────
def _target(ctx: ToolContext, script_id: Optional[int], planned_action: Optional[int]) -> dict:
    """The script an action is about: a saved one, or the one this plan creates."""
    if (script_id is None) == (planned_action is None):
        raise ToolError("Give sql_script_id (a saved script) or planned_action (the n of this plan's plan_sql_script "
                        "that creates it).")
    if planned_action is not None:
        made = planned_creation(ctx, planned_action, "save_sql_script", "SQL script")
        return {"id": None, "planned_action": planned_action, "name": made["name"], "folder_id": made["folder_id"],
                "content": made["definition"]}
    return {"id": script_id, "planned_action": None, "pending": planned_save(ctx, "save_sql_script", script_id)}


def _check_target(ctx: ToolContext, session, user, t: dict, what: str) -> dict:
    """RUN on the script's folder and what a write needs: with the content the plan gives
    it when the plan creates or changes it (checked again when it is applied), with what
    is saved otherwise."""
    paths = folder_paths(session)
    if t["id"] is None:
        folder_id, name, content, script = t["folder_id"], t["name"], t["content"], None
    else:
        script = _visible(session, user, t["id"])
        folder_id, name = script.project_id, script.name
        content = t["pending"]["definition"] if t.get("pending") else sql_scripts.content_of(session, script)
    if not perm_service.has_capability(session, user, folder_id, Capability.RUN):
        raise ToolError(f"The user cannot {what} the SQL script «{name}» (it needs RUN on its folder).")
    out = sql_script_runs.output_of(content)
    if out is None:
        raise ToolError(f"The SQL script «{name}» writes no datasource: give it an output first (plan_sql_script with output).")
    if script is not None and not t.get("pending"):
        missing = sql_script_runs.gaps(session, user, script)
        if missing:
            raise ToolError(f"The SQL script «{name}» cannot write its datasource: {missing[0]}")
    return {**t, "name": name, "output": {"name": out["name"], "folder": paths.get(out["project_id"], "")},
            "schedule": script.run_schedule if script else None, "scheduled_by": script.run_scheduled_by if script else None}


class PlanRunSqlArgs(BaseModel):
    sql_script_id: Optional[int] = Field(None, description="A saved script.")
    planned_action: Optional[int] = Field(None, ge=1, description="A script this plan creates: the n of its plan_sql_script action.")


@tool("plan_run_sql_script", title="Plan writing a SQL script's datasource", args=PlanRunSqlArgs, level="write")
async def plan_run_sql_script(ctx: ToolContext, a: PlanRunSqlArgs) -> dict:
    """Add to the plan: run a SQL script once, now, writing its whole result into its datasource (created at the first write, replaced at every one after). Nothing runs until the plan is confirmed; then get_run follows the write."""
    target = _target(ctx, a.sql_script_id, a.planned_action)
    t = await in_thread(ctx, lambda session, user: _check_target(ctx, session, user, target, "run"))
    action = {"kind": "run_sql_script", "sql_script_id": t["id"], "planned_action": t["planned_action"], "name": t["name"],
              "output": t["output"],
              "words": f"Run the SQL script «{t['name']}» now: it writes the datasource «{t['output']['name']}» in "
                       f"{t['output']['folder']}."}
    return plans.add_action(ctx, action, key=f"run_sql_script:{t['id'] if t['id'] is not None else 'a%s' % t['planned_action']}")


def _script_of(a: dict, results: list[dict]) -> int:
    if a.get("sql_script_id") is not None:
        return a["sql_script_id"]
    return made_by(results, a["planned_action"], "SQL script")


@plans.action_kind("run_sql_script")
async def _apply_run_sql_script(ctx: ToolContext, a: dict, results: list[dict]) -> dict:
    from app.routes import sql_scripts as sql_routes

    script_id = _script_of(a, results)

    async def work(session, user):
        return await sql_routes.refresh_sql_script(script_id, ctx.request, user=user, session=session)

    run = await with_session(ctx, work)
    return {"object": {"kind": "run", "id": run.run_id, "sql_script_id": script_id}, "link": sql_script_link(script_id),
            "words": f"Write {run.run_id} of «{a['name']}» started."}


@plans.scheduler("sql_script")
async def _plan_sql_script_schedule(ctx: ToolContext, a: Any) -> dict:
    flows_only(a)
    cron = checked_cron(a.cron)
    target = _target(ctx, a.id, a.planned_action)
    if cron:
        t = await in_thread(ctx, lambda session, user: _check_target(ctx, session, user, target, "schedule"))
        words = (f"Schedule the SQL script «{t['name']}», writing the datasource «{t['output']['name']}» in "
                 f"{t['output']['folder']}: {cron_words(cron)}")
        if t.get("schedule") and t.get("scheduled_by") not in (None, ctx.user.id):
            words += f", replacing `{t['schedule']}` set by someone else: the writes will carry this user's authority"
    else:
        def check_off(session, user):
            if target["id"] is None:
                return target
            script = _visible(session, user, target["id"])
            if not perm_service.has_capability(session, user, script.project_id, Capability.RUN):
                raise ToolError(f"The user cannot schedule the SQL script «{script.name}» (it needs RUN on its folder).")
            return {**target, "name": script.name}

        t = await in_thread(ctx, check_off)
        words = f"Turn off the schedule of the SQL script «{t['name']}»"
    action = {"kind": "schedule_sql_script", "sql_script_id": t["id"], "planned_action": t["planned_action"],
              "name": t["name"], "cron": cron or None, "timezone": get_settings().app.timezone, "words": words + "."}
    return plans.add_action(ctx, action,
                            key=f"schedule_sql_script:{t['id'] if t['id'] is not None else 'a%s' % t['planned_action']}")


@plans.action_kind("schedule_sql_script")
async def _apply_schedule_sql_script(ctx: ToolContext, a: dict, results: list[dict]) -> dict:
    from app.routes import sql_scripts as sql_routes

    script_id = _script_of(a, results)

    async def work(session, user):
        return await sql_routes.set_sql_script_schedule(script_id, sql_routes.ScheduleUpdate(cron=a.get("cron") or ""),
                                                        ctx.request, user=user, session=session)

    d = await with_session(ctx, work)
    words = (f"«{d.name}» scheduled: `{d.run_schedule}` (next run {d.next_run_at:%Y-%m-%d %H:%M} UTC)."
             if d.run_schedule and d.next_run_at else f"The schedule of «{d.name}» is off.")
    return {"object": {"kind": "sql_script", "id": script_id, "name": d.name}, "link": sql_script_link(script_id),
            "words": words}
