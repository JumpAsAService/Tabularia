"""The agent's datasources from connections that already exist: reading what a connection
offers (its tables, their columns, the files a pattern takes), creating a datasource,
refreshing it, scheduling its refresh.

The agent never creates nor changes a connection — credentials never reach a model nor a
conversation — it uses, by id, those the person may use (CONNECT on their folder), and
when the one needed is missing it says so with the link to the Connections page.

Creating, refreshing and scheduling are ACTIONS of a plan (services/agent/plans.py),
checked when they are planned as routes/datasources.py checks them — EDIT on the folder,
CONNECT on the connection, its kind, the name free, the engine's check of the
placeholders and of an API call — and applied once the person agrees through those very
routes, which check everything again (and start the first import at once).
"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Annotated, Any, Iterator, Literal, Optional, Union

from fastapi import HTTPException
from pydantic import BaseModel, Field, ValidationError
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import Connection, Datasource, Project, User
from app.models.permission import Capability
from app.schemas.models import (
    ApiCallSpec,
    ApiDatasourceCreate,
    DbDatasourceCreate,
    FileTemplatesSpec,
    S3DatasourceCreate,
    SftpCsvOptions,
    SftpDatasourceCreate,
    SharePointDatasourceCreate,
)
from app.services import metadata_only
from app.services import permissions as perm_service
from app.services.agent import plans
from app.services.agent.common import datasource_link, folder_paths, in_thread
from app.services.agent.planning import (
    checked_cron, cron_words, flows_only, made_by, no_connection, planned_creation, with_session,
)
from app.services.agent.registry import ToolContext, ToolError, _words, tool

MAX_TABLES = 500
MAX_FILES = 100


# ── the connection ────────────────────────────────────────────────────────────
def _is_database(conn: Connection) -> bool:
    from app.routes.connections import NOT_DATABASES

    return conn.db_type != "s3" and conn.db_type not in NOT_DATABASES


def usable_connection(session: Session, user: User, ctx: ToolContext, connection_id: int, what: str) -> Connection:
    conn = session.get(Connection, connection_id)
    if conn is None or not perm_service.has_capability(session, user, conn.project_id, Capability.CONNECT):
        raise no_connection(ctx, connection_id, what)
    return conn


@contextmanager
def _engine_words_withheld(ctx: ToolContext) -> Iterator[None]:
    """In metadata-only mode the words of the database or the server behind a connection
    stay out: they may quote a value."""
    try:
        yield
    except HTTPException as e:
        if ctx.withholds_data:
            raise ToolError(f"The connection refused ({e.status_code}). {metadata_only.ERROR_WITHHELD}") from e
        raise


# ── reading what a connection offers ─────────────────────────────────────────
class ConnectionIdArgs(BaseModel):
    connection_id: int = Field(description="A database connection (list_connections).")


@tool("list_tables", title="List a database's tables", args=ConnectionIdArgs, level="read", reads_data=False)
async def list_tables(ctx: ToolContext, a: ConnectionIdArgs) -> dict:
    """The tables (and views) a database connection reads, as its own credentials see them: what a datasource of type table can import, and what a SQL script on that connection can query. list_columns gives a table's columns."""
    from app.routes.connections import list_connection_tables

    async def work(session, user):
        conn = usable_connection(session, user, ctx, a.connection_id, "reading tables")
        if not _is_database(conn):
            raise ToolError(f"«{conn.name}» is a {conn.db_type} connection, not a database: it has no tables"
                            + (" (list_files shows its files)." if conn.db_type in ("s3", "sftp", "sharepoint") else "."))
        return conn.name, await list_connection_tables(conn.id, ctx.request, user=user, session=session)

    with _engine_words_withheld(ctx):
        name, res = await with_session(ctx, work)
    tables = list(res.get("tables") or [])
    out: dict[str, Any] = {"connection": name, "tables": tables[:MAX_TABLES]}
    if len(tables) > MAX_TABLES:
        out["note"] = f"Only the first {MAX_TABLES} of {len(tables)}."
    return out


class ListColumnsArgs(ConnectionIdArgs):
    table: str = Field(min_length=1, max_length=500, description="A table as list_tables names it.")


@tool("list_columns", title="List a table's columns", args=ListColumnsArgs, level="read", reads_data=False)
async def list_columns(ctx: ToolContext, a: ListColumnsArgs) -> dict:
    """The columns of a table of a database connection, with their types (read from one row)."""
    from app.routes.connections import list_connection_columns

    async def work(session, user):
        conn = usable_connection(session, user, ctx, a.connection_id, "reading tables")
        return await list_connection_columns(conn.id, a.table, ctx.request, user=user, session=session)

    with _engine_words_withheld(ctx):
        res = await with_session(ctx, work)
    return {"table": a.table, "columns": res.get("columns") or []}


class ListFilesArgs(BaseModel):
    connection_id: int = Field(description="An S3, SFTP or SharePoint connection (list_connections).")
    pattern: str = Field("", max_length=1000, description="s3, sftp: a glob of the file names (e.g. *.csv); placeholders like {date} allowed.")
    folder: str = Field("", max_length=1000, description="s3: the key prefix; sftp: the folder (empty: the login one).")
    bucket: str = Field("", max_length=255, description="s3: the bucket (empty: the connection's default).")
    path: str = Field("", max_length=1000, description="sharepoint: the files' path on the site, globs allowed.")
    format: Literal["auto", "excel", "csv"] = Field("auto", description="sharepoint: which files the path takes.")


@tool("list_files", title="List the files a pattern takes", args=ListFilesArgs, level="read", reads_data=False)
async def list_files(ctx: ToolContext, a: ListFilesArgs) -> dict:
    """The files an S3 folder and pattern, an SFTP folder and pattern, or a SharePoint path take now: what a datasource of type s3, sftp or sharepoint would import. Check it before planning one."""
    from app.routes import connections as conn_routes
    from app.schemas.models import S3FilesQuery, SftpFilesQuery, SharePointFilesQuery

    async def work(session, user):
        conn = usable_connection(session, user, ctx, a.connection_id, "listing files")
        try:
            if conn.db_type == "s3":
                q = S3FilesQuery(bucket=a.bucket, folder=a.folder, pattern=a.pattern)
                return await conn_routes.list_s3_files(conn.id, q, user=user, session=session)
            if conn.db_type == "sftp":
                q = SftpFilesQuery(folder=a.folder, pattern=a.pattern)
                return await conn_routes.list_sftp_files(conn.id, q, ctx.request, user=user, session=session)
            if conn.db_type == "sharepoint":
                q = SharePointFilesQuery(path=a.path, format=a.format)
                return await conn_routes.list_sharepoint_files(conn.id, q, user=user, session=session)
        except ValidationError as e:
            raise ToolError(_words(e)) from e
        raise ToolError(f"«{conn.name}» is a {conn.db_type} connection: list_files reads S3, SFTP and SharePoint ones"
                        + (" (list_tables reads a database)." if _is_database(conn) else "."))

    with _engine_words_withheld(ctx):
        res = await with_session(ctx, work)
    files = list(res.get("files") or [])
    out = {k: v for k, v in res.items() if k != "files"}
    out["files"] = files[:MAX_FILES]
    if len(files) > MAX_FILES:
        out["note"] = f"Only the first {MAX_FILES} files."
    return out


# ── creating a datasource ─────────────────────────────────────────────────────
class TableSource(BaseModel):
    type: Literal["table"]
    table: str = Field(min_length=1, max_length=500, description="The table, as list_tables names it.")
    sort_keys: list[str] = Field(default_factory=list, max_length=32, description="Columns the import orders the rows by (optional).")


class SqlSource(BaseModel):
    type: Literal["sql"]
    sql: str = Field(min_length=1, max_length=100_000, description="One SELECT in the database's own dialect, run at every refresh.")
    sort_keys: list[str] = Field(default_factory=list, max_length=32)


class S3Source(FileTemplatesSpec):
    type: Literal["s3"]
    bucket: str = Field("", max_length=255, description="Empty: the connection's default bucket.")
    folder: str = Field("", max_length=1000, description="The key prefix.")
    pattern: str = Field(min_length=1, max_length=500, description="A glob of the file names, e.g. *.csv or sales_{date}.parquet.")
    format: Literal["auto", "csv", "parquet", "excel", "ndjson"] = "auto"
    sheet: str = Field("", max_length=255, description="Excel: the sheet (empty: the first).")
    csv: SftpCsvOptions = Field(default_factory=SftpCsvOptions)


class SftpSource(FileTemplatesSpec):
    type: Literal["sftp"]
    folder: str = Field("", max_length=1000, description="The folder on the server (empty: the login one).")
    pattern: str = Field(min_length=1, max_length=500, description="A glob of the file names.")
    format: Literal["auto", "csv", "parquet", "excel", "ndjson"] = "auto"
    sheet: str = Field("", max_length=255)
    csv: SftpCsvOptions = Field(default_factory=SftpCsvOptions)


class SharePointSource(FileTemplatesSpec):
    type: Literal["sharepoint"]
    path: str = Field(min_length=1, max_length=1000, description="The file's path on the site; globs allowed.")
    format: Literal["auto", "excel", "csv"] = "auto"
    sheet: str = Field("", max_length=255)
    csv: SftpCsvOptions = Field(default_factory=SftpCsvOptions)


class ApiSource(ApiCallSpec):
    type: Literal["api"]


Source = Annotated[Union[TableSource, SqlSource, S3Source, SftpSource, SharePointSource, ApiSource],
                   Field(discriminator="type")]

# source type → (the route's body, the fields with placeholders the engine checks)
_FILE_BODIES = {"s3": S3DatasourceCreate, "sftp": SftpDatasourceCreate, "sharepoint": SharePointDatasourceCreate,
                "api": ApiDatasourceCreate}
_TEMPLATE_FIELDS = {"s3": ("folder", "pattern"), "sftp": ("folder", "pattern"), "sharepoint": ("path",)}


class PlanCreateDatasourceArgs(BaseModel):
    folder_id: int = Field(description="Where the datasource goes (list_folders: can_edit).")
    connection_id: int = Field(description="An existing connection the user may use (list_connections).")
    name: str = Field(min_length=1, max_length=255)
    description: str = Field("", max_length=4000, description="What it holds: people and the assistant read it to choose a table.")
    source: Source = Field(description="What it imports, by `type`: table or sql (a database connection), s3, sftp, sharepoint (files), api (an HTTP API connection).")


def route_body(source_type: str, data: dict) -> Any:
    """The body of the route that creates this kind of datasource."""
    try:
        if source_type in ("table", "sql"):
            return DbDatasourceCreate.model_validate(data)
        return _FILE_BODIES[source_type].model_validate(data)
    except ValidationError as e:
        raise ToolError(_words(e)) from e


def _body_data(a: PlanCreateDatasourceArgs) -> dict:
    s = a.source
    common = {"name": a.name.strip(), "description": a.description.strip(), "connection_id": a.connection_id}
    if isinstance(s, (TableSource, SqlSource)):
        return {**common, "source_type": s.type, "source_ref": s.table.strip() if isinstance(s, TableSource) else s.sql,
                "sort_keys": s.sort_keys}
    return {**common, **s.model_dump(exclude={"type"})}


def _engine_source(source_type: str, body: Any) -> Optional[tuple[dict, tuple[str, ...]]]:
    """What the engine checks before a file or API datasource is saved (as the routes)."""
    if source_type == "s3":
        return body.source_ref(), _TEMPLATE_FIELDS["s3"]
    if source_type == "sftp":
        return ({"folder": body.folder.strip(), "pattern": body.pattern.strip(), "format": body.format,
                 "sheet": body.sheet.strip(), "csv": body.csv.model_dump(), **body.template_fields()},
                _TEMPLATE_FIELDS["sftp"])
    if source_type == "sharepoint":
        return ({"path": body.path.strip(), "sheet": body.sheet.strip(), "format": body.format,
                 "csv": body.csv.model_dump(), **body.template_fields()}, _TEMPLATE_FIELDS["sharepoint"])
    if source_type == "api":
        ref = body.model_dump(exclude={"name", "description", "connection_id"})
        ref["keys"] = [k.strip() for k in ref.get("keys") or [] if k.strip()]
        return ref, ()
    return None


def _source_text(source_type: str, body: Any, conn: Connection) -> str:
    if source_type == "table":
        return body.source_ref.strip()
    if source_type == "sql":
        return body.source_ref.strip()
    if source_type == "s3":
        bucket = body.bucket.strip() or (conn.database or "").strip()
        folder = body.folder.strip().strip("/")
        return f"{bucket}/{folder + '/' if folder else ''}{body.pattern.strip()}"
    if source_type == "sftp":
        folder = body.folder.strip()
        return f"{folder.rstrip('/') + '/' if folder else ''}{body.pattern.strip()}"
    if source_type == "sharepoint":
        return body.path.strip() + (f" (sheet {body.sheet.strip()})" if body.sheet.strip() else "")
    return _api_call_text(body)


def _api_call_text(body: Any) -> str:
    """An API call as it will be made at every refresh, with the connection's credentials:
    method, path and query on the first line, then the headers it adds and the body it
    sends — the person confirming reads all of it, not only «POST /path»."""
    # as written, placeholders included ({today-30}): the person reads it, nothing is sent from here
    query = "&".join(f"{q.name}={q.value}" for q in body.query if q.name.strip())
    lines = [f"{body.method} {body.path or '/'}" + (f"?{query}" if query else "")]
    lines += [f"{h.name}: {h.value}" for h in body.headers if h.name.strip()]
    if body.method == "POST" and body.body.strip():
        lines += ["", body.body.strip()]
    return "\n".join(lines)


_SOURCE_WORDS = {"table": "the table {text}", "sql": "a SQL query", "s3": "the S3 files {text}",
                 "sftp": "the SFTP files {text}", "sharepoint": "the SharePoint files {text}",
                 "api": "the API call {text}"}


@tool("plan_create_datasource", title="Plan a new datasource", args=PlanCreateDatasourceArgs, level="write", reads_data=False)
async def plan_create_datasource(ctx: ToolContext, a: PlanCreateDatasourceArgs) -> dict:
    """Add to the plan: create a datasource that imports from an EXISTING connection — a table or a SQL query of a database, files on S3, SFTP or SharePoint, an HTTP API call. Its first import starts as soon as it is created; plan_schedule (kind datasource, planned_action = this action's n) refreshes it on a schedule. Checked now as the interface checks it: EDIT on the folder, CONNECT on the connection, the name free, the placeholders and the API call checked by the engine. The agent never creates a connection: when the one needed is missing, tell the user. Nothing changes until the plan is confirmed."""
    from app.routes.connections import _engine_inspect, engine_connection_payload
    from app.routes.datasources import is_dynamic_ref

    s = a.source
    body = route_body(s.type, _body_data(a))

    def check(session, user):
        if session.get(Project, a.folder_id) is None \
                or not perm_service.has_capability(session, user, a.folder_id, Capability.EDIT):
            raise ToolError(f"The user cannot create datasources in folder {a.folder_id}: see list_folders (can_edit).")
        conn = usable_connection(session, user, ctx, a.connection_id, f"a datasource of type {s.type}")
        if s.type in ("table", "sql"):
            if not _is_database(conn):
                raise ToolError(f"«{conn.name}» is a {conn.db_type} connection, not a database: a {s.type} source needs a "
                                "database connection" + (f" (its files are a source of type {conn.db_type})."
                                                         if conn.db_type in ("s3", "sftp", "sharepoint") else "."))
        elif conn.db_type != s.type:
            raise ToolError(f"A source of type {s.type} needs a {s.type} connection: «{conn.name}» is {conn.db_type}.")
        name = body.name.strip()
        if not name:
            raise ToolError("The datasource needs a name.")
        if session.exec(select(Datasource.id).where(Datasource.project_id == a.folder_id, Datasource.name == name)).first():
            raise ToolError(f"There is already a datasource '{name}' in that folder: choose another name, or refresh "
                            "that one (plan_refresh_datasource).")
        if s.type == "s3" and not body.bucket.strip() and not (conn.database or "").strip():
            raise ToolError(f"No bucket: set source.bucket, connection «{conn.name}» has no default one.")
        return (conn.name, conn.db_type, folder_paths(session).get(a.folder_id, ""), engine_connection_payload(conn),
                _source_text(s.type, body, conn))

    conn_name, conn_type, folder, payload, text = await in_thread(ctx, check)
    checked = _engine_source(s.type, body)
    if checked is not None and (s.type == "api" or is_dynamic_ref(*checked)):
        try:
            await _engine_inspect(payload, "validate", source=checked[0])
        except HTTPException as e:
            if ctx.withholds_data:
                raise ToolError(f"The engine refuses the source. {metadata_only.ERROR_WITHHELD}") from e
            if e.status_code == 400:  # the engine's words for whoever wrote the source
                raise ToolError(f"The engine refuses the source: {e.detail}") from e
            raise
    name = body.name.strip()
    words = (f"Create the datasource «{name}» in {folder or 'its folder'} from "
             + _SOURCE_WORDS[s.type].format(text=text.split("\n", 1)[0])  # an API call: its first line
             + f" through «{conn_name}» ({conn_type}); its first import starts at once.")
    action = {"kind": "create_datasource", "folder_id": a.folder_id, "folder": folder, "connection_id": a.connection_id,
              "connection": conn_name, "connection_type": conn_type, "name": name, "source_type": s.type,
              "source_text": text, "definition": body.model_dump(mode="json"), "words": words}
    return plans.add_action(ctx, action, key=f"create_datasource:{a.folder_id}:{name.lower()}")


@plans.action_kind("create_datasource")
async def _apply_create_datasource(ctx: ToolContext, a: dict, results: list[dict]) -> dict:
    from app.routes import datasources as ds_routes

    route = {"table": ds_routes.create_db_datasource, "sql": ds_routes.create_db_datasource,
             "s3": ds_routes.create_s3_datasource, "sftp": ds_routes.create_sftp_datasource,
             "sharepoint": ds_routes.create_sharepoint_datasource, "api": ds_routes.create_api_datasource}[a["source_type"]]
    body = route_body(a["source_type"], a["definition"])

    async def work(session, user):
        ds = await route(a["folder_id"], body, ctx.request, user=user, session=session)
        return ds.id, ds.name

    ds_id, name = await with_session(ctx, work)
    return {"object": {"kind": "datasource", "id": ds_id, "name": name}, "link": datasource_link(ds_id, name),
            "words": f"Datasource «{name}» created; its first import has started."}


# ── refreshing ────────────────────────────────────────────────────────────────
def _refreshable(session: Session, user: User, ctx: ToolContext, ds_id: int, what: str) -> tuple[Datasource, Connection]:
    """A datasource imported from a connection that the user may refresh (and schedule):
    RUN on its folder, CONNECT on its connection's."""
    ds = session.get(Datasource, ds_id)
    if ds is None or not perm_service.has_capability(session, user, ds.project_id, Capability.VIEW):
        raise ToolError(f"No datasource {ds_id} is available to this user.")
    if ds.kind != "database":
        raise ToolError(f"«{ds.name}» is not imported from a connection: it is written by the flow or the SQL script "
                        "that publishes it, when that runs. Plan that run instead.")
    if not perm_service.has_capability(session, user, ds.project_id, Capability.RUN):
        raise ToolError(f"The user cannot {what} «{ds.name}» (it needs RUN on its folder).")
    conn = session.get(Connection, ds.connection_id) if ds.connection_id else None
    if conn is None:
        raise ToolError(f"The connection of «{ds.name}» no longer exists: the user fixes it on the Connections page.")
    if not perm_service.has_capability(session, user, conn.project_id, Capability.CONNECT):
        raise ToolError(f"To {what} «{ds.name}» the user needs CONNECT on the folder of its connection «{conn.name}»: "
                        "ask whoever manages it.")
    return ds, conn


class DatasourceIdArgs(BaseModel):
    datasource_id: int


@tool("plan_refresh_datasource", title="Plan a refresh", args=DatasourceIdArgs, level="write", reads_data=False)
async def plan_refresh_datasource(ctx: ToolContext, a: DatasourceIdArgs) -> dict:
    """Add to the plan: refresh a datasource imported from a connection now — it reads its source again and replaces its data when the import succeeds. Nothing runs until the plan is confirmed; then get_run follows the import."""

    def check(session, user):
        ds, conn = _refreshable(session, user, ctx, a.datasource_id, "refresh")
        return ds.name, folder_paths(session).get(ds.project_id, ""), conn.name

    name, folder, conn_name = await in_thread(ctx, check)
    action = {"kind": "refresh_datasource", "datasource_id": a.datasource_id, "name": name, "folder": folder,
              "connection": conn_name,
              "words": f"Refresh the datasource «{name}» in {folder} now: it imports again through «{conn_name}»."}
    return plans.add_action(ctx, action, key=f"refresh_datasource:{a.datasource_id}")


@plans.action_kind("refresh_datasource")
async def _apply_refresh_datasource(ctx: ToolContext, a: dict, results: list[dict]) -> dict:
    from app.routes import datasources as ds_routes

    async def work(session, user):
        run = await ds_routes.refresh_datasource(a["datasource_id"], ctx.request, user=user, session=session)
        return run.id

    run_id = await with_session(ctx, work)
    return {"object": {"kind": "run", "id": run_id, "datasource_id": a["datasource_id"]},
            "link": datasource_link(a["datasource_id"], a["name"]),
            "words": f"Import {run_id} of «{a['name']}» started."}


# ── scheduling ────────────────────────────────────────────────────────────────
@plans.scheduler("datasource")
async def _plan_datasource_schedule(ctx: ToolContext, a: Any) -> dict:
    flows_only(a)
    cron = checked_cron(a.cron)
    if (a.id is None) == (a.planned_action is None):
        raise ToolError("Give id (a saved datasource) or planned_action (the n of this plan's plan_create_datasource).")
    if a.planned_action is not None:
        made = planned_creation(ctx, a.planned_action, "create_datasource", "datasource")

        def check_new(session, user):
            if not perm_service.has_capability(session, user, made["folder_id"], Capability.RUN):
                raise ToolError(f"The user cannot schedule «{made['name']}» (it needs RUN on {made['folder']}).")
            return made["name"], None, None

        name, current, by = await in_thread(ctx, check_new)
    else:
        def check(session, user):
            ds, _ = _refreshable(session, user, ctx, a.id, "schedule")
            return ds.name, ds.refresh_schedule, ds.refresh_scheduled_by

        name, current, by = await in_thread(ctx, check)
    if cron:
        words = f"Refresh the datasource «{name}» on a schedule: {cron_words(cron)}"
        if current and by not in (None, ctx.user.id):
            words += f", replacing `{current}` set by someone else: the refreshes will carry this user's authority"
    else:
        words = f"Turn off the scheduled refresh of the datasource «{name}»"
    action = {"kind": "schedule_datasource", "datasource_id": a.id, "planned_action": a.planned_action, "name": name,
              "cron": cron or None, "timezone": get_settings().app.timezone, "words": words + "."}
    return plans.add_action(ctx, action, key=f"schedule_datasource:{a.id if a.id is not None else 'a%s' % a.planned_action}")


@plans.action_kind("schedule_datasource")
async def _apply_schedule_datasource(ctx: ToolContext, a: dict, results: list[dict]) -> dict:
    from app.routes import datasources as ds_routes
    from app.schemas.models import ScheduleUpdate

    ds_id = a["datasource_id"] if a.get("datasource_id") is not None else made_by(results, a["planned_action"], "datasource")

    def work(session, user):
        out = ds_routes.set_schedule(ds_id, ScheduleUpdate(cron=a.get("cron") or ""), ctx.request, user=user,
                                     session=session)
        return out.name, out.refresh_schedule, out.next_refresh_at

    name, schedule, nxt = await in_thread(ctx, work)
    words = (f"«{name}» refreshes on schedule: `{schedule}` (next {nxt:%Y-%m-%d %H:%M} UTC)." if schedule and nxt
             else f"The scheduled refresh of «{name}» is off.")
    return {"object": {"kind": "datasource", "id": ds_id, "name": name}, "link": datasource_link(ds_id, name),
            "words": words}
