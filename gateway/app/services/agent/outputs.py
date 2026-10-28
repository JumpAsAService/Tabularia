"""The Outputs the agent puts in a flow: datasource, database table, S3, SFTP, email.

The node's settings are the ones the editor writes (services/flow_resolver.py turns them
into a run). They are checked when the agent adds the node, again when the plan is made
and again when it is applied, with the rules a run applies at launch (routes/runs.py,
`_launch_flow_run`) and its own helpers: EDIT on the folder a datasource is written to,
CONNECT on the connection and the right kind of connection, the SMTP connection's allowed
domains, the SFTP file name, the placeholders checked by the engine. The run checks them
once more when it starts: this is the same answer, earlier.
"""
from __future__ import annotations

from typing import Any, Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from app.models import Connection, Datasource, Project, User
from app.models.permission import Capability
from app.services import permissions as perm_service
from app.services.agent.common import folder_paths, in_thread
from app.services.agent.registry import ToolContext, ToolError

OutputKind = Literal["datasource", "database", "s3", "sftp", "email"]
# how much of a post-SQL the plan's words carry (its page shows it whole)
POST_SQL_WORDS = 400


class OutputSettings(BaseModel):
    """The settings of an Output, by kind (the fields of other kinds are ignored)."""

    kind: OutputKind = Field(description="datasource (a datasource in Tabularia), database (a table), s3 (a file on S3), sftp (a file on an SFTP server), email (an attachment sent by email).")
    # datasource
    datasource_name: Optional[str] = Field(None, max_length=200, description="datasource: its name.")
    folder_id: Optional[int] = Field(None, description="datasource: the folder (default: the flow's).")
    overwrite: bool = Field(False, description="datasource: replace the data of the datasource of that name at every run. Needed when one exists already, and for a scheduled flow (the second run would find it).")
    description: Optional[str] = Field(None, max_length=2000, description="datasource: what it holds.")
    # every other kind
    connection_id: Optional[int] = Field(None, description="database, s3, sftp, email: the connection (see list_connections).")
    # database
    table: Optional[str] = Field(None, max_length=300, description="database: the table, schema.table if needed.")
    mode: Literal["append", "replace"] = Field("append", description="database: add the rows, or replace the table's content.")
    post_sql: Optional[str] = Field(None, max_length=20_000, description="database: SQL run on the database after the write.")
    # s3 and sftp
    key: Optional[str] = Field(None, max_length=1000, description="s3: the object's key (or the prefix when partitioned); placeholders like {date} allowed.")
    bucket: Optional[str] = Field(None, max_length=255, description="s3: the bucket (default: the connection's).")
    partition_by: Optional[list[str]] = Field(None, description="s3: columns to partition by (hive style).")
    file_name: Optional[str] = Field(None, max_length=255, description="sftp: the file name; placeholders like {date} allowed.")
    folder: Optional[str] = Field(None, max_length=1000, description="sftp: the folder on the server (default: the login one).")
    format: Optional[Literal["parquet", "csv", "xlsx"]] = Field(None, description="s3: parquet (default) or csv; sftp: csv (default), parquet or xlsx.")
    # email
    to: Optional[list[str]] = Field(None, description="email: the recipients.")
    cc: Optional[list[str]] = None
    subject: Optional[str] = Field(None, max_length=500, description="email: the subject; placeholders like {date} allowed.")
    body: Optional[str] = Field(None, max_length=20_000)
    body_is_html: bool = False
    attachment_name: Optional[str] = Field(None, max_length=255, description="email: the attachment's name without extension (default: report).")
    attachment_format: Literal["xlsx", "csv"] = "xlsx"
    timezone: Optional[str] = Field(None, max_length=64, description="The time zone of the dates in the templates (IANA name, default UTC).")


def node_data(s: OutputSettings, flow_folder: Optional[int]) -> dict[str, Any]:
    """The node's `data`, as the editor writes it."""
    if s.kind == "datasource":
        d: dict[str, Any] = {"destType": "datasource", "mode": "append", "name": (s.datasource_name or "").strip(),
                             "projectId": s.folder_id or flow_folder, "overwrite": bool(s.overwrite)}
        if s.description:
            d["description"] = s.description
    elif s.kind == "database":
        d = {"destType": "database", "connectionId": s.connection_id, "table": (s.table or "").strip(), "mode": s.mode,
             "postSql": s.post_sql or ""}
    elif s.kind == "s3":
        d = {"destType": "s3", "connectionId": s.connection_id, "s3Bucket": (s.bucket or "").strip(),
             "s3Key": (s.key or "").strip(), "s3Format": s.format or "parquet", "partitionBy": s.partition_by or []}
    elif s.kind == "sftp":
        d = {"destType": "sftp", "connectionId": s.connection_id, "sftpFolder": (s.folder or "").strip(),
             "sftpFileName": (s.file_name or "").strip(), "sftpFormat": s.format or "csv"}
    else:
        d = {"destType": "email", "connectionId": s.connection_id,
             "emailTo": [a.strip() for a in s.to or [] if a.strip()], "emailCc": [a.strip() for a in s.cc or [] if a.strip()],
             "emailSubject": (s.subject or "").strip(), "emailBody": s.body or "", "emailHtml": bool(s.body_is_html),
             "attachmentName": (s.attachment_name or "").strip(), "attachmentFormat": s.attachment_format,
             "stopOnFailure": True}
    if s.timezone and s.timezone.strip():
        d["timezone"] = s.timezone.strip()
    return d


def _excerpt(text: str, chars: int = POST_SQL_WORDS) -> str:
    one = " ".join(text.split())
    return f"`{one}`" if len(one) <= chars else f"`{one[:chars]}…` ({len(one)} characters)"


def _connection(session: Session, user: User, connection_id: Any, kinds: str) -> Connection:
    conn = session.get(Connection, connection_id) if isinstance(connection_id, int) else None
    if conn is None or not perm_service.has_capability(session, user, conn.project_id, Capability.CONNECT):
        raise ToolError(f"No connection {connection_id} is available to this user for {kinds}: see list_connections. "
                        "The agent cannot create connections: if the right one is missing, the user adds it at /connections.")
    return conn


def _check(session: Session, user: User, d: dict) -> tuple[dict, dict[str, tuple[str, str]], str]:
    """Checks one Output's settings; returns what it writes ({kind, words, target, …}: the
    words for the model, the fields for the plan's page), the templates the engine must
    check and the prefix of their errors."""
    from app.routes.connections import NOT_DATABASES, allowed_email_domains
    from app.routes.runs import _check_sftp_file_name

    dest = d.get("destType") or "datasource"
    if dest == "datasource":
        name = str(d.get("name") or "").strip()
        folder = d.get("projectId")
        if not name:
            raise ToolError("A datasource Output needs datasource_name.")
        if not isinstance(folder, int) or session.get(Project, folder) is None \
                or not perm_service.has_capability(session, user, folder, Capability.EDIT):
            raise ToolError(f"The user cannot write datasources in folder {folder}: see list_folders (can_edit).")
        existing = session.exec(select(Datasource).where(Datasource.project_id == folder, Datasource.name == name)).first()
        if existing is not None and existing.kind != "flow":
            raise ToolError(f"'{name}' is a datasource of kind «{existing.kind}» in that folder: a flow cannot overwrite it, choose another name.")
        if existing is not None and not d.get("overwrite"):
            raise ToolError(f"There is already a datasource '{name}' in that folder: set overwrite to true to replace its data at every run, or choose another name.")
        path = folder_paths(session).get(folder, str(folder))
        words = f"writes the datasource «{name}» in {path}" + (" (its data replaced at every run)" if d.get("overwrite") else "")
        return {"kind": dest, "words": words, "target": name, "folder": path, "overwrite": bool(d.get("overwrite"))}, {}, ""
    if dest == "database":
        conn = _connection(session, user, d.get("connectionId"), "a database table")
        if conn.db_type == "s3" or conn.db_type in NOT_DATABASES:
            raise ToolError(f"Connection «{conn.name}» is {conn.db_type}, not a database: a table needs a database connection.")
        table = str(d.get("table") or "").strip()
        if not table:
            raise ToolError("A database Output needs table.")
        mode = "replacing its rows" if d.get("mode") == "replace" else "adding rows"
        # the post-SQL runs on the database at every run, with the connection's user: the
        # person confirming reads it whole (the plan's page), the model its beginning
        post_sql = str(d.get("postSql") or "").strip()
        words = f"writes the table {table} on «{conn.name}» ({conn.db_type}), {mode}" + \
            (f", then runs this post-SQL on the database: {_excerpt(post_sql)}" if post_sql else "")
        return {"kind": dest, "words": words, "target": table, "connection": conn.name,
                "mode": "replace" if d.get("mode") == "replace" else "append",
                **({"post_sql": post_sql} if post_sql else {})}, {}, ""
    if dest == "s3":
        conn = _connection(session, user, d.get("connectionId"), "S3")
        if conn.db_type != "s3":
            raise ToolError(f"Connection «{conn.name}» is not S3/object storage.")
        key = str(d.get("s3Key") or "").strip().strip("/")
        if not key:
            raise ToolError("An S3 Output needs key: the object's path.")
        if d.get("s3Format") not in ("parquet", "csv"):
            raise ToolError("S3 outputs are parquet or csv.")
        bucket = str(d.get("s3Bucket") or "").strip() or (conn.database or "").strip()
        if not bucket:
            raise ToolError(f"No bucket: set bucket, connection «{conn.name}» has no default one.")
        parts = d.get("partitionBy") or []
        words = f"writes {d['s3Format']} to S3 {bucket}/{key} on «{conn.name}»" + (f", partitioned by {', '.join(parts)}" if parts else "")
        return {"kind": dest, "words": words, "target": f"{bucket}/{key}", "connection": conn.name,
                "format": d["s3Format"]}, {"S3 key": (key, "path")}, "S3: "
    if dest == "sftp":
        conn = _connection(session, user, d.get("connectionId"), "SFTP")
        if conn.db_type != "sftp":
            raise ToolError(f"Connection «{conn.name}» is not SFTP.")
        file_name, folder = str(d.get("sftpFileName") or "").strip(), str(d.get("sftpFolder") or "").strip()
        try:
            _check_sftp_file_name(file_name)
        except HTTPException as e:
            raise ToolError(str(e.detail)) from e
        if ".." in folder.replace("\\", "/").split("/"):
            raise ToolError("SFTP: '..' is not allowed in the folder: write the folder you mean.")
        if d.get("sftpFormat") not in ("csv", "parquet", "xlsx"):
            raise ToolError("SFTP outputs are csv, parquet or xlsx.")
        where = f"{folder.rstrip('/')}/{file_name}" if folder else file_name
        return {"kind": dest, "words": f"writes {d['sftpFormat']} file {where} on «{conn.name}» (SFTP)", "target": where,
                "connection": conn.name, "format": d["sftpFormat"]}, \
            {"file name": (file_name, "file"), "folder": (folder, "path")}, "SFTP: "
    if dest == "email":
        conn = _connection(session, user, d.get("connectionId"), "email")
        if conn.db_type != "smtp":
            raise ToolError(f"Connection «{conn.name}» is not SMTP: email needs an SMTP connection.")
        to, cc = list(d.get("emailTo") or []), list(d.get("emailCc") or [])
        if not to:
            raise ToolError("An email Output needs at least one recipient in to.")
        for a in [*to, *cc]:
            if "@" not in a or a.startswith("@") or a.endswith("@") or any(c.isspace() for c in a):
                raise ToolError(f"Invalid address: {a}")
        if not str(d.get("emailSubject") or "").strip():
            raise ToolError("An email Output needs subject.")
        try:
            allowed = allowed_email_domains(conn)
        except HTTPException as e:
            raise ToolError(str(e.detail)) from e
        if allowed:
            outside = [a for a in [*to, *cc] if a.rsplit("@", 1)[-1].lower() not in allowed]
            if outside:
                raise ToolError(f"Recipients outside the domains connection «{conn.name}» allows "
                                f"({', '.join(allowed)}): {', '.join(outside)}")
        attachment = str(d.get("attachmentName") or "").strip() or "report"
        fmt = d.get("attachmentFormat") or "xlsx"
        words = (f"emails the result as {fmt} «{attachment}» to {', '.join(to)}" + (f" (cc {', '.join(cc)})" if cc else "")
                 + f", subject «{d.get('emailSubject')}», through «{conn.name}»")
        return {"kind": dest, "words": words, "target": ", ".join(to), "cc": ", ".join(cc), "connection": conn.name,
                "subject": str(d.get("emailSubject") or ""), "format": fmt, "body": str(d.get("emailBody") or ""),
                "body_is_html": bool(d.get("emailHtml"))}, \
            {"subject": (str(d.get("emailSubject") or ""), "text"), "attachment name": (attachment, "file")}, ""
    raise ToolError(f"Unknown Output kind {dest}: datasource, database, s3, sftp or email.")


async def check_output(ctx: ToolContext, data: dict, flow_name: str) -> dict:
    """One Output's settings checked for the person of `ctx`: what it writes ({kind, words,
    target, …})."""
    from app.routes.runs import _check_output_templates

    info, templates, prefix = await in_thread(ctx, lambda session, user: _check(session, user, data))
    tz = str(data.get("timezone") or "").strip() or None
    try:
        await _check_output_templates(templates, tz, flow_name, prefix)
    except HTTPException as e:
        raise ToolError(str(e.detail)) from e
    return info
