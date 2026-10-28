"""A SQL script writing its result into its datasource: «Refresh now» on its page, its
schedule, a cell of a flow group. docs/specs/sql-scripts.md («Saving as a datasource»).

A write is one run: a kind="sql" row with `sql_script_id` and the engine task that
writes the whole result as a parquet. Its output is published as a flow Output's is
(routes/runs._reconcile → _publish_datasource): the datasource is born at the first
write, keeps its id at every write after, and changes only when a write succeeds.

- On the catalog the run's chain (services/sql_scripts) goes to `/tasks/transform-data`,
  on the script's engine.
- On a connection the SQL goes to `/db/ingest`, as a SQL datasource's refresh: the
  database's whole result, streamed into the parquet.

Every write uses the script's current version, with the authority of whoever starts it
(the person on the page, the schedule's author, whoever runs the group): VIEW on the
script, RUN on its folder, EDIT on the datasource's folder, and what a run needs (the
datasources the SQL names, readable by them; CONNECT on the connection). One write at
a time per script.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from fastapi import HTTPException, Request
from sqlalchemy import text
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.engine_client import get_engine_client
from app.models import Datasource, Project, Run, SqlScript, User
from app.models.permission import Capability
from app.models.run import TERMINAL_STATES
from app.services import audit, sql_scripts
from app.services import contracts as contract_service
from app.services import permissions as perm_service
from app.services.engine_policy import pick_engine

logger = logging.getLogger(__name__)

# the advisory locks of «start a SQL script's write», in a namespace of their own
_SQL_SCRIPT_LOCK = 74610004
ALREADY_RUNNING = "The script is already writing its datasource: it can write again once it has finished"
# the engine's answer to a submitted task comes quickly; the task itself runs on
CHECK = 60


def output_of(content: dict) -> Optional[dict]:
    """The datasource the script writes ({"name", "project_id"}), or None."""
    out = content.get("output")
    return out if isinstance(out, dict) and out.get("name") and isinstance(out.get("project_id"), int) else None


def target_of(session: Session, output: dict) -> Optional[Datasource]:
    """The datasource already there under the output's name, in its folder."""
    return session.exec(select(Datasource).where(Datasource.project_id == output["project_id"],
                                                 Datasource.name == output["name"])).first()


def in_line_for_the_script(session: Session, script_id: int) -> None:
    """Two replicas, or a replica and the scheduler, cannot both see no write and both
    start one. Released by the commit that records the run."""
    if session.get_bind().dialect.name == "postgresql":
        session.connection().execute(
            text("SELECT pg_advisory_xact_lock(:space, :script)"), {"space": _SQL_SCRIPT_LOCK, "script": script_id}
        )


def live_run(session: Session, script_id: int) -> Optional[Run]:
    """The script's write still queued or running, if any."""
    return session.exec(select(Run).where(Run.sql_script_id == script_id, Run.kind == "sql",
                                          Run.status.not_in(TERMINAL_STATES))  # type: ignore[union-attr]
                        .order_by(Run.id.desc())).first()


def _refuse(status: int, detail: str) -> HTTPException:
    return HTTPException(status_code=status, detail=detail)


def check_output(session: Session, user: User, output: dict) -> Optional[Datasource]:
    """The output can be written by `user`: its folder exists and they have EDIT there, and
    a datasource already under that name is one a script or a flow wrote (a database's or
    a file's snapshot is not overwritten). Returns that datasource, if any."""
    if session.get(Project, output["project_id"]) is None:
        raise _refuse(404, "The folder of the script's datasource no longer exists: choose another")
    if not perm_service.has_capability(session, user, output["project_id"], Capability.EDIT):
        raise _refuse(403, f"{user.email} cannot write in the folder of the script's datasource «{output['name']}»")
    target = target_of(session, output)
    if target is not None and target.kind != "flow":
        raise _refuse(409, f"«{output['name']}» is a datasource of kind «{target.kind}»: the script cannot "
                           "overwrite it, choose another name")
    return target


def _checked_script(session: Session, user: User, script: SqlScript) -> tuple[dict, dict]:
    """The script's current content and output, if `user` may write it."""
    if not sql_scripts.can_see(session, user, script):
        raise _refuse(404, f"{user.email} can no longer see the SQL script «{script.name}»")
    if not perm_service.has_capability(session, user, script.project_id, Capability.RUN):
        raise _refuse(403, f"RUN missing on the folder of the SQL script «{script.name}»")
    content = sql_scripts.content_of(session, script)
    output = output_of(content)
    if output is None:
        raise _refuse(422, f"The SQL script «{script.name}» does not save its result as a datasource: "
                           "give it a name and a folder first")
    if not sql_scripts.strip_comments(content.get("sql") or "").strip().rstrip(";").strip():
        raise _refuse(422, f"The SQL script «{script.name}» has no SQL yet")
    return content, output


def gaps(session: Session, user: User, script: SqlScript) -> list[str]:
    """What `user` lacks to write the script's datasource now, as a schedule or a group
    would (empty = nothing): every check of a write, without writing. (The engine the
    administrator allows is chosen when the write starts.)"""
    try:
        content, output = _checked_script(session, user, script)
        check_output(session, user, output)
        if content.get("connection_id") is not None:
            sql_scripts.usable_connection(session, user, content["connection_id"])
        else:
            sql_scripts.resolve(session, user, content.get("sql") or "", content.get("engine"))
    except HTTPException as e:
        return [str(e.detail)]
    return []


async def launch(session: Session, user: User, script: SqlScript, trigger_type: str = "manual",
                 parent_run_id: Optional[int] = None, request: Optional[Request] = None) -> Run:
    """Starts the write of the script's current version with `user`'s authority and
    returns its run. Raises HTTPException: what is missing, or 409 while a write of the
    same script is going."""
    from app.routes.connections import engine_connection_payload  # routes → services: late import
    from app.routes.runs import snapshot_key

    content, output = _checked_script(session, user, script)
    target = check_output(session, user, output)
    sql = content.get("sql") or ""
    contract_doc = contract_service.payload(session, target.id) if target is not None else None
    contract = {"contract": contract_doc} if contract_doc else {}
    output_key = snapshot_key(target.id if target is not None else None)
    detail: dict = {"version": script.current_version, "trigger": trigger_type, "sql": sql[:2000],
                    "datasource": {"name": output["name"], "project_id": output["project_id"]}}
    if content.get("connection_id") is not None:
        conn = sql_scripts.usable_connection(session, user, content["connection_id"])
        bucket, input_key, engine = get_settings().engine.bucket, "", conn.name
        path, body = "/db/ingest", {"connection": engine_connection_payload(conn), "source": {"mode": "sql", "ref": sql},
                                    "bucket": bucket, "output_key": output_key, **contract}
        detail["connection"] = {"id": conn.id, "name": conn.name}
    else:
        engine = await pick_engine(session, content.get("engine"))
        resolved = sql_scripts.resolve(session, user, sql, engine)
        if not resolved.tables:
            raise _refuse(422, "The query reads no datasource: name one in FROM")
        chain = sql_scripts._catalog_chain(resolved)
        bucket, input_key = chain["bucket"], chain["input_key"]
        path, body = "/tasks/transform-data", {**chain, "output_key": output_key, "engine": engine, **contract}
        detail.update(engine=engine, datasources=[{"id": ds.id, "name": ds.name} for ds in resolved.tables.values()])

    in_line_for_the_script(session, script.id)  # the check and the start in line, across replicas
    if live_run(session, script.id) is not None:
        session.rollback()  # releases the lock
        raise _refuse(409, ALREADY_RUNNING)
    try:
        resp = await get_engine_client().post(path, json=body, timeout=CHECK)
        if resp.status_code >= 400:
            raise sql_scripts._engine_failure(resp, lambda d: d)
        task_id = resp.json().get("task_id")
        if not task_id:
            raise _refuse(502, "The engine did not return a task_id")
        # what this write reads, for its lineage (services/openlineage.py): the version
        # that ran, and the datasources the SQL named or the connection it ran on
        read = {"version": script.current_version, "connection_id": content.get("connection_id"),
                "datasources": [d["id"] for d in detail.get("datasources") or []]}
        run = Run(kind="sql", sql_script_id=script.id, task_id=task_id, launched_by=user.id,
                  trigger_type=trigger_type, parent_run_id=parent_run_id, engine=engine, input_key=input_key,
                  output_bucket=bucket, output_key=output_key, publish_name=output["name"],
                  publish_project_id=output["project_id"], publish_overwrite=True,
                  publish_description=f"Written by the SQL script «{script.name}»", publish_sort_keys="[]",
                  script=json.dumps(read))
        session.add(run)
        session.commit()
        session.refresh(run)
    except BaseException:
        session.rollback()
        raise
    audit.record_audit(session, actor=user, action=audit.SQL_SCRIPT_PUBLISH, target_type="sql_script",
                       target_id=script.id, target_label=script.name, detail={"run_id": run.id, **detail},
                       request=request)
    return run


def failed_run(session: Session, user: Optional[User], script: SqlScript, trigger_type: str, reason: str) -> Run:
    """A write that could not start, recorded as a failed run: a schedule that cannot
    write leaves its reason in the script's runs, not only in the server's log."""
    from datetime import datetime, timezone

    run = Run(kind="sql", sql_script_id=script.id, task_id="", launched_by=user.id if user else None,
              trigger_type=trigger_type, status="FAILURE", error=reason[:2000], input_key="", output_bucket="",
              output_key="", finished_at=datetime.now(timezone.utc))
    session.add(run)
    session.commit()
    session.refresh(run)
    return run
