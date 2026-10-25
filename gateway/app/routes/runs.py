"""The flows' runs (run history) and the publishing of the output.

Who may do what (inherited down the project tree):
- launch a run: the RUN capability on the flow's project;
- publish the output as a datasource: EDIT on the destination folder as well;
- see the history: VIEW.

The gateway does NOT poll in the background: the state of the non-terminal runs is
reconciled with the engine each time someone reads them (lazily). So a run launched
and forgotten updates at the first visit to the history.
"""
import json
import logging
import uuid
from typing import Optional
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from starlette.concurrency import run_in_threadpool
from sqlalchemy import and_, case, func, literal_column, or_, text, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.engine_client import CHECK, get_engine_client
from app.db.session import get_session
from app.deps.auth import get_current_user
from app.deps.permissions import ensure_can
from app.services import permissions as perm_service
from app.models import Connection, Datasource, Flow, FlowGroup, Project, Run, SavedScript, User
from app.models.permission import Capability
from app.models.run import LIVE_INGEST, TERMINAL_STATES
from app.routes.connections import NOT_DATABASES, allowed_email_domains, engine_connection_payload
from app.services import audit
from app.services import contracts as contract_service
from app.services import openlineage, run_history, webhooks
from app.schemas.models import (
    ActivityBucket,
    Page,
    RunActivityOut,
    RunCreate,
    RunOut,
    RunProgress,
    RunSearchOut,
)
from app.services.blobgc import schedule_blob_deletion
from app.core.routing import ReleasingRoute

logger = logging.getLogger(__name__)

router = APIRouter(route_class=ReleasingRoute, tags=["runs"])


def _get_flow(session: Session, flow_id: int) -> Flow:
    flow = session.get(Flow, flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail="Flow not found")
    return flow


def _find_datasource(session: Session, project_id: int, name: str) -> Datasource | None:
    return session.exec(
        select(Datasource).where(Datasource.project_id == project_id, Datasource.name == name)
    ).first()


def _datasource_name_taken(session: Session, project_id: int, name: str) -> bool:
    return _find_datasource(session, project_id, name) is not None


def snapshot_key(datasource_id: int | None) -> str:
    """The key of a datasource's parquet: a FOLDER per datasource, with the timestamp in
    the file's name.

    It serves OPERATIONS, not correctness: one sees at a glance which datasource each
    parquet belongs to, the orphans' cleanup can list by prefix instead of scanning
    everything, and per-datasource lifecycle rules become possible. The order of the
    updates is NOT read from here (`Datasource.snapshot_run_id` says it): the timestamp
    is the WRITING one, and in a race it would reward the run that started first and
    finished later, that is the stale data.

    `datasource_id` is unknown at an output's FIRST publish (the datasource is born
    after the run): then the file goes in `datasets/new/`, and from the next overwrite
    on it ends up in its datasource's folder. The old keys (`datasets/<uuid>.parquet`)
    stay valid: they are only strings, nobody interprets them.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"datasets/{datasource_id or 'new'}/{stamp}-{uuid.uuid4().hex[:8]}.parquet"


@router.post("/flows/{flow_id}/runs", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def launch_run(
    flow_id: int,
    body: RunCreate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    flow = _get_flow(session, flow_id)
    return await _launch_flow_run(session, user, flow, body)


@router.post("/flows/{flow_id}/email-test", response_model=RunOut, status_code=status.HTTP_201_CREATED)
async def launch_email_test(
    flow_id: int,
    body: RunCreate,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """A dry run of the email node: makes the REAL attachment and sends it to WHOEVER ASKS,
    to see what it looks like before sending it to someone else.

    The server sets the recipient (`user.email`): the client cannot choose it, or this
    route would be the handiest way around the allowed domains. The rest of the path is
    the same as a normal run — the same RBAC (RUN on the flow, CONNECT on the
    connection), the same audit — so the trial really checks what will happen, not an
    imitation of it.

    Publishing, destination and copy are dropped: a trial must not write anything
    anywhere.
    """
    flow = _get_flow(session, flow_id)
    if not body.email:
        raise HTTPException(status_code=422, detail="This test needs an Output node of type email")
    if not (user.email or "").strip():
        raise HTTPException(status_code=422, detail="Your account has no email address")
    email_only = body.model_copy(update={"publish": None, "destination": None, "mirror": None})
    return await _launch_flow_run(session, user, flow, email_only, dry_run_to=user.email.strip())


def resolve_run_engine(flow: Flow, engine_mode: str) -> str:
    """The engine to run a flow's run with: in "production" the production one when set,
    otherwise (or in "development") the development one."""
    if engine_mode == "production" and flow.production_engine:
        return flow.production_engine
    return flow.engine


async def _launch_flow_run(
    session: Session, user: User, flow: Flow, body: RunCreate,
    trigger_type: str = "manual", parent_run_id: int | None = None,
    engine_mode: str = "development",
    dry_run_to: str | None = None,
    script_destination: tuple[dict, str] | None = None,
) -> Run:
    """The core of launching a flow run, reusable outside the HTTP context (e.g. the
    scheduler). It applies the whole RBAC — RUN on the flow, EDIT for the publish,
    CONNECT for the destinations — with the authority of `user`.

    `trigger_type`: "manual" (a user launches it) or "schedule" (started by the
    scheduler within a scheduled orchestration).
    `parent_run_id`: set when it is an output launched INSIDE an orchestration (a
    child) — so the calendar does not count it as a run of its own.
    `engine_mode`: "development" = the editor's engine (`flow.engine`); "production" =
    `flow.production_engine` (when set) — what the scheduler uses, so the DAG in
    production can run on an engine other than the one it was designed with.

    `script_destination`: (engine payload, summary) of a Script node's input, built by
    services/script_runs.py — the scripts bucket, never from the client."""
    ensure_can(session, user, flow.project_id, Capability.RUN)
    engine_name = resolve_run_engine(flow, engine_mode)

    # PRODUCTION = every record: any development sample (operations marked `_dev_sample`,
    # nested ones too) is removed. The gateway's resolver never injects them in
    # production: this is defence in depth (e.g. a client sending a chain the editor
    # built).
    if engine_mode != "development":
        from app.services.flow_resolver import strip_dev_sample_ops

        body.operations, removed = strip_dev_sample_ops(body.operations)
        if removed:
            logger.warning("production run of flow %s: %d development samples removed", flow.id, removed)

    # input_key and operations: the source (and every nested source: the right of
    # join/union, driver/body of the foreach) must be IN the engine's bucket and under a
    # managed prefix (pinning), AND every managed key must be readable by the user (data
    # plane RBAC). Without the pinning, a client-controlled `bucket`/`input_key` would make
    # the engine (with credentials that read everything) read any object — the same
    # constraint as the preview/transform proxy.
    from app.services.objects import (
        collect_storage_keys,
        ensure_can_read_keys,
        ensure_reads_pinned,
    )

    engine_bucket = get_settings().engine.bucket
    if not body.bucket:
        body.bucket = engine_bucket
    read_payload = {
        "bucket": body.bucket,
        "input_key": body.input_key,
        "operations": body.operations,
    }
    ensure_reads_pinned(session, user, read_payload, engine_bucket)
    ensure_can_read_keys(session, user, collect_storage_keys(read_payload))

    # publish with sort keys: sorts the RESULT before writing it, so the parquet is
    # physically sorted (pruning downstream). The `sort` op is standard on every engine;
    # the same keys end up on the datasource too.
    publish_keys = [k.strip() for k in body.publish.sort_keys if k and k.strip()] if body.publish else []
    if publish_keys:
        # `ignore_missing`: a key gone from the chain must not fail the run (the materialized
        # copy ignores it the same way already)
        body.operations = list(body.operations) + [
            {"type": "sort", "params": {"by": publish_keys, "ignore_missing": True}}
        ]

    # the parquet's destination folder: the overwritten datasource's, when it is known at
    # launch already (see `snapshot_key`)
    publish_target_id: int | None = None
    if body.publish:
        # publishing writes content in the destination folder → EDIT
        if session.get(Project, body.publish.project_id) is None:
            raise HTTPException(status_code=404, detail="Destination folder not found")
        ensure_can(session, user, body.publish.project_id, Capability.EDIT)
        pub_name = body.publish.name.strip()
        if not pub_name:
            raise HTTPException(status_code=422, detail="The datasource needs a name")
        existing = _find_datasource(session, body.publish.project_id, pub_name)
        if existing is not None:
            if not body.publish.overwrite:
                raise HTTPException(
                    status_code=409,
                    detail=f"There is already a datasource '{body.publish.name}' in this folder",
                )
            # overwriting a database snapshot (with schedule/connection) would distort it: allowed
            # only on datasources a flow produced
            if existing.kind != "flow":
                raise HTTPException(
                    status_code=409,
                    detail=f"'{body.publish.name}' is a datasource of kind «{existing.kind}»: "
                    "a flow cannot overwrite it, choose another name",
                )
            publish_target_id = existing.id  # the overwrite reuses its folder

    # the output's destination (Output node): the connection is referenced by id, the
    # gateway builds the payload with the (encrypted) secret — never the client
    destination_payload = None
    destination_summary = None
    if script_destination is not None:
        destination_payload, destination_summary = script_destination
    elif body.destination:
        conn = session.get(Connection, body.destination.connection_id)
        if conn is None:
            raise HTTPException(status_code=404, detail="Connection not found")
        ensure_can(session, user, conn.project_id, Capability.CONNECT)

        if body.destination.type == "sftp":
            if conn.db_type != "sftp":
                raise HTTPException(status_code=422, detail="The chosen connection is not SFTP")
            file_name = body.destination.file_name.strip()
            folder = body.destination.folder.strip()
            _check_sftp_file_name(file_name)
            if ".." in folder.replace("\\", "/").split("/"):
                raise HTTPException(status_code=422, detail="SFTP: '..' is not allowed in the folder: write the folder you mean")
            if body.destination.format not in ("csv", "parquet", "xlsx"):
                raise HTTPException(status_code=422, detail="SFTP outputs are CSV, parquet or Excel")
            await _check_output_templates({"file name": (file_name, "file"), "folder": (folder, "path")},
                                          body.destination.timezone, flow.name, "SFTP: ")
            destination_payload = {
                "type": "sftp",
                "connection": engine_connection_payload(conn),
                # run_id is set once the run row exists (see below)
                "target": {"folder": folder, "file_name": file_name, "format": body.destination.format,
                           "run_id": None, "flow": flow.name, **_zone(body.destination.timezone)},
            }
            destination_summary = json.dumps(
                {
                    "type": "sftp",
                    "connection_id": conn.id,
                    "host": f"{conn.host}:{conn.port or 22}",
                    "folder": folder,
                    "file_name": file_name,
                    "format": body.destination.format,
                    **_zone(body.destination.timezone),
                }
            )
        elif body.destination.type == "s3":
            if body.destination.format not in ("parquet", "csv"):
                raise HTTPException(status_code=422, detail="S3 outputs are parquet or CSV")
            if conn.db_type != "s3":
                raise HTTPException(
                    status_code=422, detail="The chosen connection is not S3/object storage"
                )
            key = body.destination.key.strip().strip("/")
            if not key:
                raise HTTPException(status_code=422, detail="The S3 key/path is empty")
            bucket_override = body.destination.bucket.strip()
            if not bucket_override and not (conn.database or "").strip():
                raise HTTPException(
                    status_code=422,
                    detail="No bucket: set one on the output or as the connection's default",
                )
            await _check_output_templates({"S3 key": (key, "path")}, body.destination.timezone, flow.name, "S3: ")
            destination_payload = {
                "type": "s3",
                "connection": engine_connection_payload(conn),
                "target": {
                    "bucket": bucket_override,
                    "key": key,
                    "format": body.destination.format,
                    "partition_by": body.destination.partition_by,
                    # only for a key with placeholders: the target of a plain key is as it was
                    **(_template_context(flow, body.destination.timezone) if _templated(key) else {}),
                },
            }
            destination_summary = json.dumps(
                {
                    "type": "s3",
                    "connection_id": conn.id,
                    "db_type": "s3",
                    "endpoint": conn.host or "aws",
                    "bucket": bucket_override or conn.database,
                    "key": key,
                    "format": body.destination.format,
                    "partition_by": body.destination.partition_by,
                    **_zone(body.destination.timezone),
                }
            )
        else:
            if conn.db_type == "s3":
                raise HTTPException(
                    status_code=422, detail="The chosen connection is S3: use an S3 destination"
                )
            if conn.db_type in NOT_DATABASES:
                # they are not databases: without this check the error would come from the driver,
                # with the run started already, and would talk about something else
                raise HTTPException(
                    status_code=422, detail=f"A {conn.db_type} connection cannot receive a table: a database connection is needed"
                )
            table = body.destination.table.strip()
            if not table:
                raise HTTPException(status_code=422, detail="The destination table name is empty")
            destination_payload = {
                "type": "database",
                "connection": engine_connection_payload(conn),
                "target": {
                    "table": table,
                    "mode": body.destination.mode,
                    "post_sql": body.destination.post_sql,
                },
            }
            destination_summary = json.dumps(
                {
                    "type": "database",
                    "connection_id": conn.id,
                    "db_type": conn.db_type,
                    "host": conn.host,
                    "database": conn.database,
                    "table": table,
                    "mode": body.destination.mode,
                }
            )

    # a copy to an external S3 AS WELL AS the datasource: the same resolution as the
    # destination (connection by id, secret encrypted by the gateway, CONNECT capability)
    # — the only change is that its outcome does not bind the run.
    mirror_payload = None
    mirror_summary = None
    if body.mirror:
        mconn = session.get(Connection, body.mirror.connection_id)
        if mconn is None:
            raise HTTPException(status_code=404, detail="S3 copy connection not found")
        ensure_can(session, user, mconn.project_id, Capability.CONNECT)
        if mconn.db_type != "s3":
            raise HTTPException(
                status_code=422, detail="The copy's connection is not S3/object storage"
            )
        mkey = body.mirror.key.strip().strip("/")
        mbucket = body.mirror.bucket.strip()
        # the copy never fails the publish (it is best effort by design): a copy that
        # cannot be made — no key, no bucket, a key that cannot be resolved — is
        # skipped and said on the run. (No connection, or no CONNECT, stays a refusal.)
        copy_refused = None
        if not mkey:
            copy_refused = "the copy's S3 path is empty: set it on the Output node"
        elif not mbucket and not (mconn.database or "").strip():
            copy_refused = "no bucket for the copy: set one on the output or as the connection's default"
        else:
            try:
                await _check_output_templates({"copy's S3 key": (mkey, "path")}, body.mirror.timezone, flow.name, "S3: ")
            except HTTPException as e:
                copy_refused = str(e.detail)
    if body.mirror and copy_refused is not None:
        mirror_summary = json.dumps({"connection_id": mconn.id, "endpoint": mconn.host or "aws",
                                     "bucket": mbucket or mconn.database, "key": mkey, "format": "parquet",
                                     "ok": False, "error": copy_refused[:500]})
    elif body.mirror:
        mirror_payload = {
            "connection": engine_connection_payload(mconn),
            "target": {
                **(_template_context(flow, body.mirror.timezone) if _templated(mkey) else {}),
                "bucket": mbucket,
                "key": mkey,
                # always parquet (a byte-for-byte copy of the internal snapshot) and never
                # partitioned: the copy is ONE object overwritten, at the path agreed with whoever
                # reads it
                "format": "parquet",
                "partition_by": [],
            },
        }
        # "pending" until the worker reports the outcome (see _reconcile)
        mirror_summary = json.dumps(
            {
                "connection_id": mconn.id,
                "endpoint": mconn.host or "aws",
                "bucket": mbucket or mconn.database,
                "key": mkey,
                "format": "parquet",
                "ok": None,
            }
        )

    # sending the output as an email attachment. The same resolution as destination and
    # copy (connection by id, secret encrypted by the gateway, CONNECT), plus the check
    # that is the real security boundary here: the recipients are free text in the flow's
    # definition, and the only barrier to sending to an arbitrary address is the
    # connection's allowed domains. It must be applied HERE: the worker receives resolved
    # recipients and can no longer tell them apart.
    email_payload = None
    email_summary = None
    if body.email:
        econn = session.get(Connection, body.email.connection_id)
        if econn is None:
            raise HTTPException(status_code=404, detail="SMTP connection not found")
        ensure_can(session, user, econn.project_id, Capability.CONNECT)
        if econn.db_type != "smtp":
            raise HTTPException(status_code=422, detail="The chosen connection is not SMTP")

        if dry_run_to:
            # DRY RUN: the recipient is the authenticated identity, set by the server and not
            # chosen by the client. The allowed domains do NOT apply here on purpose: sending to
            # oneself is not exfiltration, and with narrowed domains (typically the customers')
            # the trial button would never work.
            to_finale, cc_finale = [dry_run_to], []
            recipients = [dry_run_to]
        else:
            to_finale = [a.strip() for a in body.email.to if a.strip()]
            cc_finale = [a.strip() for a in body.email.cc if a.strip()]
            recipients = [*to_finale, *cc_finale]
            if not recipients:
                raise HTTPException(status_code=422, detail="The email has no recipients")

            allowed = allowed_email_domains(econn)
            if allowed:
                outside = [a for a in recipients if a.rsplit("@", 1)[-1].lower() not in allowed]
                if outside:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Recipients outside the domains this connection allows "
                            f"({', '.join(allowed)}): {', '.join(outside)}"
                        ),
                    )

        await _check_output_templates(
            {"subject": (body.email.subject, "text"), "attachment name": (body.email.attachment_name or "report", "file")},
            body.email.timezone, flow.name, "")
        email_templated = _templated(body.email.subject) or _templated(body.email.attachment_name)
        email_payload = {
            "connection": engine_connection_payload(econn),
            # a trial must never fail anything: it is a check, not a step
            "stop_on_failure": False if dry_run_to else body.email.stop_on_failure,
            "target": {
                "to": to_finale,
                "cc": cc_finale,
                "subject": body.email.subject,
                "body": body.email.body,
                "body_is_html": body.email.body_is_html,
                "attachment_name": body.email.attachment_name or "report",
                "attachment_format": body.email.attachment_format,
                **(_template_context(flow, body.email.timezone) if email_templated else {}),
            },
        }
        email_summary = json.dumps(
            {
                "connection_id": econn.id,
                "host": econn.host,
                "to": recipients,
                "subject": body.email.subject,
                "attachment": body.email.attachment_name or "report",
                "format": body.email.attachment_format,
                "ok": None,  # "pending" until the worker reports the outcome
            }
        )
        # Audit at LAUNCH, not at reconciliation: who asked to send, what and to whom is the
        # fact an investigation needs, and it exists even when the send fails later. The
        # technical outcome lives on the run's row.
        audit.record_audit(
            session, actor=user, action=audit.EMAIL_SEND, target_type="flow",
            target_id=flow.id, target_label=flow.name,
            detail={
                "connection_id": econn.id,
                "host": econn.host,
                "to": recipients,
                "subject": body.email.subject,
                "format": body.email.attachment_format,
                "trigger": "dry_run" if dry_run_to else trigger_type,
            },
        )

    # the published output lives in datasets/ (the sources' area); the others in out/
    output_key = (
        snapshot_key(publish_target_id) if body.publish else f"out/{uuid.uuid4().hex}.parquet"
    )

    def new_run(task_id: str) -> Run:
        return Run(
            flow_id=flow.id,
            task_id=task_id,
            launched_by=user.id,
            trigger_type=trigger_type,
            parent_run_id=parent_run_id,
            engine=engine_name,
            input_key=body.input_key,
            output_bucket=body.bucket,
            output_key=output_key,
            publish_name=body.publish.name.strip() if body.publish else None,
            publish_project_id=body.publish.project_id if body.publish else None,
            publish_description=body.publish.description if body.publish else "",
            publish_overwrite=body.publish.overwrite if body.publish else False,
            publish_sort_keys=json.dumps(publish_keys),
            destination=destination_summary,
            mirror=mirror_summary,
            email=email_summary,
        )

    # SFTP: the file name may hold {run_id}, the run's number — so the run row is
    # written BEFORE the task is queued, with a placeholder task id that the engine
    # answers PENDING for, and goes away if the engine refuses the task
    early_run: Run | None = None
    # the targets that name the run: the SFTP Output always (its file name has had
    # {run_id} from the start), the others when a template asks for it
    numbered = []
    if destination_payload is not None and (destination_payload["type"] == "sftp"
                                            or "run_id" in destination_payload["target"]):
        numbered.append(destination_payload["target"])
    numbered += [p["target"] for p in (mirror_payload, email_payload) if p is not None and "run_id" in p["target"]]
    if numbered:
        early_run = new_run(f"{PLACEHOLDER_TASK}{uuid.uuid4().hex}")
        session.add(early_run)
        session.commit()
        session.refresh(early_run)
        for target in numbered:
            target["run_id"] = early_run.id

    try:
        task_id = await _submit_transform(body, output_key, destination_payload, mirror_payload, email_payload,
                                          engine_name, publish_target_id, session)
    except BaseException:
        if early_run is not None:
            session.delete(early_run)
            session.commit()
        raise
    if early_run is not None:
        early_run.task_id = task_id
        session.add(early_run)
        session.commit()
        session.refresh(early_run)
        return early_run

    run = new_run(task_id)
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


def _check_sftp_file_name(file_name: str) -> None:
    """What the gateway can tell by itself before the run; the placeholders are
    checked by the engine's checker (_check_output_templates)."""
    import re as _re

    if not file_name:
        raise HTTPException(status_code=422, detail="SFTP: the file name is empty")
    outside = _re.sub(r"\{\{|\}\}|\{[^{}]*\}", "", file_name)  # the text around the placeholders
    if "/" in outside or "\\" in outside:
        raise HTTPException(status_code=422, detail="SFTP: the file name cannot contain a folder: put the folder in its own field")


def _templated(template: str | None) -> bool:
    return any(c in (template or "") for c in "{}")


def _zone(tz: str | None) -> dict:
    """The Output's time zone in a target, only when it is not the default UTC:
    the target of an Output without one stays as it was."""
    tz = (tz or "").strip()
    return {"timezone": tz} if tz and tz != "UTC" else {}


def _template_context(flow: Flow, tz: str | None) -> dict:
    """What an Output's templates need besides the date: the flow's name ({flow}),
    the run's number ({run_id}, set once the run row exists) and the zone."""
    return {"flow": flow.name, "run_id": None, **_zone(tz)}


async def _check_output_templates(templates: dict[str, tuple[str, str]], tz: str | None, flow_name: str,
                                  prefix: str) -> None:
    """The placeholders of an Output (file name, folder, key, subject, attachment
    name) checked by the ENGINE's checker before anything is queued: a template
    that cannot be resolved is refused here with the message the run would give.
    Templates without braces need no call. An engine that cannot answer (the
    previous version, not reachable) checks them at run time instead."""
    wanted = {name: t for name, (t, _) in templates.items() if _templated(t)}
    zoned = bool(_zone(tz))
    if not wanted and not zoned:  # nothing to resolve, and the default zone
        return
    payload = {"kind": "output", "templates": wanted, "timezone": (tz or "").strip() or "UTC",
               "targets": {name: templates[name][1] for name in wanted}, "flow": flow_name}
    try:
        resp = await get_engine_client().post("/placeholders/preview", json=payload, timeout=CHECK)
    except Exception as e:  # noqa: BLE001 — the run will check them
        logger.warning("output templates not checked before the run: %s", type(e).__name__)
        return
    if resp.status_code >= 400:
        logger.warning("output templates not checked before the run: engine answered %s", resp.status_code)
        return
    answer = resp.json()
    if answer.get("error"):  # the Output's time zone, before anything else
        raise HTTPException(status_code=422, detail=f"{prefix}{answer['error']}")
    for name in wanted:
        err = ((answer.get("fields") or {}).get(name) or {}).get("error")
        if err:
            raise HTTPException(status_code=422, detail=f"{prefix}{name}: {err}")


async def _submit_transform(body: RunCreate, output_key: str, destination_payload, mirror_payload, email_payload,
                            engine_name: str, publish_target_id, session: Session) -> str:
    """Queues the run's task on the engine and returns its id."""
    client = get_engine_client()
    resp = await client.post(
        "/tasks/transform-data",
        timeout=CHECK,
        json={
            "bucket": body.bucket,
            "input_key": body.input_key,
            "output_key": output_key,
            "operations": body.operations,
            "destination": destination_payload,
            "mirror": mirror_payload,
            "email": email_payload,
            "engine": engine_name,  # development or production engine (see engine_mode)
            # the data contract of the datasource about to be overwritten: the worker evaluates
            # it on the output, and the report decides whether to publish it
            **({"contract": contract} if (contract := contract_service.payload(session, publish_target_id)) else {}),
        },
    )
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
    task_id = resp.json().get("task_id")
    if not task_id:
        raise HTTPException(status_code=502, detail="The engine did not return a task_id")
    return task_id


# advisory-lock namespace of «one refresh per datasource» (the flow queue uses 74610001)
_REFRESH_LOCK = 74610002


def in_line_for_the_datasource(session: Session, ds_id: int) -> None:
    """Queues, until the end of the transaction, whoever is about to check whether a
    datasource has a refresh running and to start one: the manual refresh from any
    replica, the scheduler, a flow's Refresh node. Without it two of them at once
    both saw nothing running and both launched (review 2026-10-14,
    correctness-core-6). Released by the commit that records the import. On SQLite
    (the tests) there is one process and nothing to queue on."""
    if session.get_bind().dialect.name == "postgresql":
        session.connection().execute(
            text("SELECT pg_advisory_xact_lock(:space, :ds)"), {"space": _REFRESH_LOCK, "ds": ds_id}
        )


def live_refresh(session: Session, ds_id: int) -> Optional[int]:
    """The id of the datasource's import still running or queued, if any."""
    return session.exec(
        select(Run.id).where(Run.datasource_id == ds_id, text(LIVE_INGEST)).order_by(Run.id.desc()).limit(1)
    ).first()


async def start_refresh_if_free(
    session: Session, user: User, ds: Datasource, conn: Connection,
    trigger_type: str = "manual", parent_run_id: int | None = None, launch=None,
) -> Optional[Run]:
    """Starts a refresh unless one is running: returns the new run, or None.

    The last import is reconciled first (outside the lock: it may talk to the engine
    and commit), then the check and the launch happen in line with every other
    starter, the lock held until the new run is committed. `launch`: the function
    that starts the import (the caller's own reference, so tests can replace it)."""
    last = session.exec(
        select(Run).where(Run.datasource_id == ds.id, Run.kind == "ingest").order_by(Run.started_at.desc())
    ).first()
    if last is not None and last.status not in TERMINAL_STATES:
        await _reconcile(session, last)
    session.commit()  # a fresh transaction: the lock is held from here to the launch's commit
    in_line_for_the_datasource(session, ds.id)
    if live_refresh(session, ds.id) is not None:
        session.rollback()  # releases the lock
        return None
    try:
        extra = {"parent_run_id": parent_run_id} if parent_run_id is not None else {}
        return await (launch or launch_ingest_run)(session, user, ds, conn, trigger_type=trigger_type, **extra)
    except BaseException:
        session.rollback()
        raise


async def launch_ingest_run(
    session: Session, user: User, ds: Datasource, conn: Connection,
    trigger_type: str = "manual", parent_run_id: int | None = None,
) -> Run:
    """Queues on the engine the ingest of a database datasource and records the run.

    Every refresh writes a NEW parquet: the datasource points at it only at SUCCESS
    (snapshot swap in `_finalize_ingest`), so whoever reads the current snapshot is
    never disturbed by a refresh in progress.
    """
    bucket = get_settings().engine.bucket
    output_key = snapshot_key(ds.id)
    client = get_engine_client()
    # the datasource's data contract, when it has one turned on: the worker evaluates it
    # on the data just read and the report decides whether the snapshot is swapped
    document = contract_service.payload(session, ds.id)
    contract = {"contract": document} if document else {}
    if conn.db_type == "sharepoint":
        # the same round as the databases (run, snapshot swap, refresh, scheduler): only WHO
        # brings the data changes. `source_ref` is the JSON {path, sheet} (+ the
        # placeholders' zone, variables, refresh mode when the datasource has them).
        ref = json.loads(ds.source_ref or "{}")
        resp = await client.post(
            "/sharepoint/ingest",
            timeout=CHECK,
            json={
                "connection": engine_connection_payload(conn),
                # format and csv only when saved: one saved before CSV reads Excel only
                "source": {"path": ref.get("path", ""), "sheet": ref.get("sheet", ""),
                           **{k: ref[k] for k in ("format", "csv") if k in ref}, **_template_part(ref)},
                "bucket": bucket,
                "output_key": output_key,
                **_refresh_context(session, ds, ref, ("path",)),
                **contract,
            },
        )
    elif conn.db_type == "api":
        # same road again: `source_ref` is the call as JSON. {last_refresh} is the start
        # of the last successful refresh; in append mode the new rows are merged into
        # the current snapshot, which the engine reads
        ref = json.loads(ds.source_ref or "{}")
        last = _last_successful_refresh(session, ds.id)
        resp = await client.post(
            "/http-api/ingest",
            timeout=CHECK,
            json={
                "connection": engine_connection_payload(conn),
                "source": ref,
                "bucket": bucket,
                "output_key": output_key,
                **({"last_refresh": last} if last else {}),
                **({"previous": {"bucket": ds.bucket, "key": ds.key}} if ref.get("mode") == "append" and ds.key else {}),
                **contract,
            },
        )
    elif conn.db_type == "sftp":
        # same road again: `source_ref` is the JSON {folder, pattern, format, sheet, csv}
        ref = json.loads(ds.source_ref or "{}")
        resp = await client.post(
            "/sftp/ingest",
            timeout=CHECK,
            json={
                "connection": engine_connection_payload(conn),
                "source": {**{k: ref[k] for k in ("folder", "pattern", "format", "sheet", "csv") if k in ref},
                           **_template_part(ref)},
                "bucket": bucket,
                "output_key": output_key,
                **_refresh_context(session, ds, ref, ("folder", "pattern")),
                **contract,
            },
        )
    elif conn.db_type == "s3":
        # the «S3 folder» datasource: `source_ref` is the JSON {bucket, folder, pattern,
        # format, sheet, csv} (+ the placeholders' zone, variables, refresh mode)
        ref = json.loads(ds.source_ref or "{}")
        resp = await client.post(
            "/s3/ingest",
            timeout=CHECK,
            json={
                "connection": engine_connection_payload(conn),
                "source": {**{k: ref[k] for k in ("bucket", "folder", "pattern", "format", "sheet", "csv") if k in ref},
                           **_template_part(ref)},
                "bucket": bucket,
                "output_key": output_key,
                **_refresh_context(session, ds, ref, ("folder", "pattern")),
                **contract,
            },
        )
    else:
        resp = await client.post(
            "/db/ingest",
            timeout=CHECK,
            json={
                "connection": engine_connection_payload(conn),
                "source": {"mode": ds.source_type, "ref": ds.source_ref, "sort_keys": json.loads(ds.sort_keys or "[]")},
                "bucket": bucket,
                "output_key": output_key,
                **contract,
            },
        )
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text[:500])
    task_id = resp.json().get("task_id")
    if not task_id:
        raise HTTPException(status_code=502, detail="The engine did not return a task_id")

    run = Run(
        kind="ingest",
        flow_id=None,
        datasource_id=ds.id,
        task_id=task_id,
        launched_by=user.id,
        trigger_type=trigger_type,
        parent_run_id=parent_run_id,
        input_key=f"{conn.db_type}://{conn.host}/{conn.database}",  # descriptive
        output_bucket=bucket,
        output_key=output_key,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


# past this age a non-terminal run is lost: Celery answers PENDING even for task ids
# it NO longer knows (result expired, Redis emptied, worker dead) → without a cutoff
# the run would stay a zombie forever. Set through ENGINE__RUN_STALE_TIMEOUT_SECONDS.
def stale_after_seconds() -> int:
    """ENGINE__RUN_STALE_TIMEOUT_SECONDS, read at each use (Administration → Settings)."""
    return get_settings().engine.run_stale_timeout_seconds
SIDE_EFFECT_ATTEMPTS = 3  # IMMEDIATE attempts of the post-claim effect before putting it off

# the signatures of an OOM kill: the worker went over the container's memory limit
# and the kernel killed the process (signal 9). Celery's raw message
# (WorkerLostError / SIGKILL) is cryptic → we translate it into something clear.
_OOM_SIGNS = ("signal 9", "sigkill", "workerlosterror", "memoryerror", "out of memory")
_OOM_MESSAGE = (
    "The run was stopped: its worker process was killed, most often because it ran out of available "
    "memory. Reduce the data (filter/limit) or simplify the flow; if it needs more memory, raise the "
    "workers' limit (worker.resources.limits.memory in the Helm chart, WORKER_MEM_LIMIT with Compose). "
    "A worker stopped meanwhile (an upgrade, a drained node) ends a run the same way: run it again."
)
# what the engine already says in words, about a worker that died with its task
# (backend/app/tasks/worker_pool.py, LOST): not replaced by the message above
_ENGINE_TOLD = ("The worker running it stopped before the end",)


# Messages the backend already translated with the database SERVER's text
# (backend/app/ingest/db_errors.py, MESSAGE_PREFIXES): an «out of memory» there is the
# database's, not the worker's, and must not be replaced with the OOM message.
_DB_MESSAGE_PREFIXES = ("PostgreSQL:", "ClickHouse:", "MySQL:", "MariaDB:", "Trino:", "Oracle:", "SQL Server:", "S3:", "Database:", "SFTP:",
                        "API:")


def _sftp_written(summary: str | None, outcome) -> str | None:
    """The destination's summary with what was written: for SFTP the file's real
    name and size, for S3 the key its placeholders resolved to — the template
    alone does not say which file a run made."""
    if not summary or not isinstance(outcome, dict):
        return None
    try:
        d = json.loads(summary)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    if d.get("type") == "sftp":
        d.update({k: outcome[k] for k in ("path", "bytes") if k in outcome})
    elif d.get("type") == "s3" and outcome.get("key") and outcome.get("key") != d.get("key"):
        d["written_key"] = outcome["key"]
    else:
        return None
    return json.dumps(d)[:2000]


def _notes_of(result: dict) -> str | None:
    """A refresh's notes: the values of a range whose file was not there, and the
    keys of an append found on more than one new row (kept, but worth a look)."""
    missing = result.get("missing") if isinstance(result.get("missing"), list) else []
    repeated = result.get("duplicate_keys") if isinstance(result.get("duplicate_keys"), int) else 0
    if not missing and not repeated:
        return None
    notes: dict = {"missing": missing[:50], "missing_count": result.get("missing_count") or len(missing),
                   "expansions": result.get("expansions")}
    if repeated:
        notes["duplicate_keys"] = repeated
    return json.dumps(notes)[:20000]


def _friendly_error(error: str | None, error_detail: str | None) -> str | None:
    """Translates the OOM failures (a worker killed by the memory limit) into a clear
    message; the raw traceback stays in `error_detail`."""
    if error and error.startswith(_DB_MESSAGE_PREFIXES + _ENGINE_TOLD):
        return error
    blob = f"{error or ''}\n{error_detail or ''}".lower()
    if any(sign in blob for sign in _OOM_SIGNS):
        return _OOM_MESSAGE
    return error


# The progress the engine reports for running runs (phase, rows, files): it lives for
# the request only, in `session.info`, not in a table — it changes every two seconds
# and nobody reads it again. _reconcile puts it there, run_out uses it.
_PROGRESS = "avanzamento_run"


def run_out(session: Session, run: Run) -> RunOut:
    """The run for the browser; when not finished, with its progress: the phase (the
    engine's, or queued / running when it does not say), the counts and the seconds
    elapsed."""
    out = RunOut.model_validate(run.model_dump())
    if run.status in TERMINAL_STATES:
        return out
    if run.status == "PENDING":
        phase: dict = {"phase": "queued", "seconds": int(_age_seconds(run.started_at))}
    else:
        from_engine = session.info.get(_PROGRESS, {}).get(run.id) or {}
        phase = {k: from_engine[k] for k in ("phase", "rows", "file", "files", "call", "calls", "pages") if k in from_engine}
        phase.setdefault("phase", "running")
        phase["seconds"] = int(_age_seconds(run.engine_started_at or run.started_at))
    try:
        return out.model_copy(update={"progress": RunProgress(**phase)})
    except ValueError:  # an engine that sends something else: shown without progress
        return out


def _age_seconds(dt: datetime | None) -> float:
    if dt is None:
        return 0.0
    if dt.tzinfo is None:  # Postgres returns naive → it is UTC by construction
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds()


async def _revoke_task(run: Run) -> None:
    """Stops the task on the engine FOR REAL (revoke + terminate: the same command as the
    Queue panel).

    Best effort: when the engine does not answer, the run is marked failed anyway. But
    without this attempt the state would lie — the task would keep running, and
    writing, while the history calls it failed.
    """
    if not run.task_id:
        return
    try:
        await get_engine_client().delete(f"/tasks/{run.task_id}", timeout=CHECK)
        logger.info("run %s expired: task %s revoked on the engine", run.id, run.task_id)
    except Exception as e:
        logger.warning("run %s: revoking task %s failed: %s", run.id, run.task_id, e)


def _email_not_sent(summary: str | None) -> str | None:
    """The email summary of a run that did not succeed, still marked pending: the
    worker reports the outcome only when the task returns, so a run that fails
    (the email itself, with stop on failure, or anything before it) would show
    the email as pending forever. It was not sent; the run's error says why."""
    if not summary:
        return None
    try:
        data = json.loads(summary)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or data.get("ok") is not None:
        return None
    data.update(ok=False, error="not sent: the run did not succeed")
    return json.dumps(data)[:2000]


def _contract_target_id(session: Session, run: Run) -> int | None:
    """The datasource this run's data would end up in: the one a refresh updates, or the
    one a flow is about to overwrite."""
    if run.datasource_id is not None:
        return run.datasource_id
    if run.publish_name and run.publish_project_id:
        target = _find_datasource(session, run.publish_project_id, run.publish_name)
        return target.id if target is not None else None
    return None


def _refuse_snapshot(session: Session, run: Run, report: dict) -> None:
    """The data contract refused this run's data (a blocking rule is violated): the
    snapshot is NOT swapped. The datasource stays as it is — the same key, the same
    rows — and whoever reads it keeps seeing the last good data. The refused parquet
    goes to deferred deletion, and the report stays in the history. Does NOT commit:
    `_reconcile` does, together with the run's claim."""
    schedule_blob_deletion(session, run.output_bucket, run.output_key, reason=f"run {run.id} refused by the data contract")
    ds_id = _contract_target_id(session, run)
    contract = contract_service.get(session, ds_id) if ds_id is not None else None
    if contract is not None:
        contract_service.record_result(
            session, contract, report, trigger="refresh" if run.kind == "ingest" else "publish",
            run_id=run.id, snapshot_key=run.output_key, blocked=True,
        )
    logger.warning("run %s: data refused by the data contract of datasource %s", run.id, ds_id)


def _record_contract(session: Session, run: Run, report: dict | None, trigger: str) -> None:
    """The data was published: the report that came with it becomes the contract's state.
    Only when the swap really happened — a run overtaken by a newer one publishes
    nothing, and its report does not speak of the data the datasource is serving. Does
    NOT commit."""
    if report is None or run.datasource_id is None:
        return
    ds = session.get(Datasource, run.datasource_id)
    if ds is None or ds.snapshot_run_id != run.id:
        return
    contract = contract_service.get(session, ds.id)
    if contract is not None:
        contract_service.record_result(session, contract, report, trigger=trigger, run_id=run.id, snapshot_key=run.output_key)


def _is_stale_swap(ds: Datasource, run: Run) -> bool:
    """True when `run` is OLDER than the run that produced the current snapshot.

    Run ids are monotonic and assigned at launch, so they order by the instant the
    source was READ: a lower id read earlier, and its data is staler even when it
    finished later. Without a baseline (`snapshot_run_id` null, a snapshot older than
    this field) it is accepted.
    """
    return ds.snapshot_run_id is not None and run.id is not None and run.id < ds.snapshot_run_id


QUEUE_TIMED_OUT = (
    "The run stayed in the queue too long without an orchestrator picking it up: "
    "it did not run. Launch the flow again."
)


async def close_if_queued_too_long(session: Session, run: Run) -> Run:
    """A run queued for longer than `ORCHESTRATOR__QUEUE_TIMEOUT_SECONDS` does not start
    any more: it is closed as failed. Otherwise a launch made while there was no
    orchestrator would start hours later, when someone turns one back on — and in the
    meantime it would keep the flow busy for the scheduler.

    Whoever looks at it does it (a process that only answers too: that is how whoever
    launched sees the outcome even with the orchestrators off), and the orchestrator
    before taking from the queue. A conditional UPDATE: only one closes it."""
    created = run.started_at if run.started_at.tzinfo else run.started_at.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc) - created).total_seconds() <= get_settings().orchestrator.queue_timeout_seconds:
        return run
    closed = session.exec(
        update(Run)
        .where(Run.id == run.id, Run.status == "PENDING")
        .values(status="FAILURE", error=QUEUE_TIMED_OUT, finished_at=datetime.now(timezone.utc))
    )
    if closed.rowcount == 1:
        session.refresh(run)
        webhooks.enqueue_for_run(session, run)  # in the same transaction as the closing
    session.commit()
    session.refresh(run)
    if closed.rowcount == 1:
        from app.services.notifier import notify_failure

        await notify_failure(session, run)
    return run


def _let_go(session: Session) -> None:
    """Gives a request's database connection back before waiting for the engine, as
    the proxy does (routes/proxy.py, `_release_db`). A poll waited up to 30 s for a
    slow engine with its transaction open, holding a pooled connection that did
    nothing; with enough of them at once the next checkout froze the whole replica.

    Only a request's session with nothing pending: a background loop's transaction
    may hold locks it still needs. The loaded rows stay as they are (no expiry), so
    nothing is read again after the wait unless the run changes."""
    if not session.info.get(perm_service.REQUEST_SESSION) or not session.in_transaction():
        return
    if session.new or session.dirty or session.deleted:
        return
    expire, session.expire_on_commit = session.expire_on_commit, False
    try:
        session.commit()
    finally:
        session.expire_on_commit = expire


async def _reconcile(session: Session, run: Run) -> Run:
    """Aligns a non-terminal run with the state of the task on the engine.

    The transition to a TERMINAL state is an ATOMIC claim (an UPDATE conditional on not
    being terminal already): among concurrent reads of the same run — the editor's
    polling + the open history, or two tabs — only one wins, so the publishing cannot
    happen twice. Network errors towards the engine do not break the read: it tries
    again at the next one.
    """
    if run.status in TERMINAL_STATES:
        return run
    if run.kind == "orchestration":
        # not an engine task: the orchestrator handles its state. One thing only is done
        # here: noticing that it stayed queued past the maximum time
        if run.status == "PENDING":
            return await close_if_queued_too_long(session, run)
        return run
    if run.kind in ("script", "container"):
        # a Kubernetes Job the orchestration follows: here only an orphan is closed
        from app.services.script_runs import close_if_orphaned

        return await close_if_orphaned(session, run)
    task_id = run.task_id
    if task_id.startswith(PLACEHOLDER_TASK):
        # an SFTP Output's run, written before its task so that {run_id} exists: the
        # engine has never heard of it. Still being launched, or the gateway stopped
        # in between — then it is closed with that reason, nothing to revoke.
        return _close_if_never_queued(session, run)
    _let_go(session)
    client = get_engine_client()
    try:
        resp = await client.get(f"/tasks/{task_id}", timeout=CHECK)
        data = resp.json()
    except Exception as e:  # engine unreachable → we will reconcile later
        logger.warning("reconciling run %s put off: %s", run.id, e)
        return run

    new_status = data.get("status", run.status)
    if isinstance(data.get("progress"), dict):
        session.info.setdefault(_PROGRESS, {})[run.id] = data["progress"]
    result = data.get("result") or {}
    error = data.get("error")
    error_detail = data.get("error_detail")  # the engine's whole traceback

    # a job the admin stopped (Queue panel): Celery reports it REVOKED → for us a terminal
    # failure, not a state to poll again until the stale timeout
    if new_status == "REVOKED":
        new_status = "FAILURE"
        error = error or "Job stopped (revoked by an administrator)"
        error_detail = None

    # the stopwatch starts from the REAL execution, not from the row's birth: `started_at`
    # includes the wait in the queue, and with busy workers a run just started would be
    # declared expired while it is writing.
    _age = _age_seconds(run.engine_started_at or run.started_at)
    if new_status not in TERMINAL_STATES and _age > stale_after_seconds():
        # the task does NOT stop by itself: without a revoke it would keep running — and
        # WRITING — while the run shows as failed. On an Output in append this leads the user
        # to launch again and append the same rows twice.
        await _revoke_task(run)
        new_status = "FAILURE"
        _mins = stale_after_seconds() // 60
        error = (
            f"Timeout: the run exceeded the maximum time ({_mins} min) without completing and "
            "was stopped. If the flow is just slow, raise the limit "
            "(ENGINE__RUN_STALE_TIMEOUT_SECONDS) or reduce the data."
        )
        error_detail = None

    if new_status == run.status:
        return run

    if new_status not in TERMINAL_STATES:  # es. PENDING → STARTED
        # a conditional UPDATE, not the object: between the question to the engine and here
        # another process may have closed the run already, and writing it STARTED again would
        # reopen it (the next round would close it again, redoing the publishing)
        new_values: dict = {"status": new_status}
        if new_status == "STARTED" and run.engine_started_at is None:
            new_values["engine_started_at"] = datetime.now(timezone.utc)  # the real start
        session.exec(update(Run).where(Run.id == run.id, Run.status.not_in(TERMINAL_STATES)).values(**new_values))
        session.commit()
        session.refresh(run)
        return run

    # atomic claim of the terminal transition + side effect (the snapshot's swap / the
    # datasource's publish) in the SAME transaction: one commit at the end. When the
    # effect fails it rolls back and the run does NOT become terminal, so the next
    # _reconcile tries again — instead of staying SUCCESS with the swap/publish lost
    # forever.
    # Data contract: the worker evaluated the data just written (when the destination
    # datasource has one). With a BLOCKING rule violated the task succeeded but the run
    # did not: the data is not published.
    contract_report = result.get("contract") if new_status == "SUCCESS" and isinstance(result.get("contract"), dict) else None
    if contract_report is not None and contract_service.payload(session, _contract_target_id(session, run)) is None:
        # the contract was removed or turned off while the run was going: the report speaks of
        # a promise that is no longer there, and decides nothing
        contract_report = None
    refused = contract_report is not None and contract_report.get("outcome") == "failed"

    values: dict = {"status": new_status, "finished_at": datetime.now(timezone.utc)}
    if refused:
        values = {
            "status": "FAILURE",
            "finished_at": values["finished_at"],
            "rows_written": result.get("rows_written"),
            "error": contract_service.broken_rules_message(contract_report)[:2000],
        }
        not_sent = _email_not_sent(run.email)
        if not_sent:
            values["email"] = not_sent
    elif new_status == "SUCCESS":
        values["rows_written"] = result.get("rows_written")
        # the outcome of the best-effort copy: the run stays SUCCESS even when it failed, but
        # the error must be recorded in here — in the SAME atomic claim — or it would stay
        # only in the worker's logs, invisible in the history
        if result.get("mirror") is not None:
            values["mirror"] = json.dumps(result.get("mirror"))[:2000]
        if result.get("email") is not None:
            values["email"] = json.dumps(result.get("email"))[:2000]
        written = _sftp_written(run.destination, result.get("destination"))
        if written is not None:
            values["destination"] = written
        notes = _notes_of(result)
        if notes is not None:
            values["notes"] = notes
    else:
        values["error"] = (_friendly_error(error, error_detail) or "")[:2000]
        values["error_detail"] = error_detail[:20000] if error_detail else None
        not_sent = _email_not_sent(run.email)
        if not_sent:
            values["email"] = not_sent
    # with a few IMMEDIATE attempts: a transient error (a lock, a DB hiccup) is solved in
    # the same round instead of waiting for the next read; when it persists, after the
    # attempts the run stays non-terminal and is tried again later.
    for attempt in range(1, SIDE_EFFECT_ATTEMPTS + 1):
        claimed = session.exec(
            update(Run).where(Run.id == run.id, Run.status.not_in(TERMINAL_STATES)).values(**values)
        )
        if claimed.rowcount == 0:
            session.rollback()  # another request closed this run already
            session.refresh(run)
            return run
        try:
            if refused:
                _refuse_snapshot(session, run, contract_report)
            elif new_status == "SUCCESS" and run.kind == "ingest":
                _finalize_ingest(session, run, result)
                _record_contract(session, run, contract_report, "refresh")
            elif (
                new_status == "SUCCESS"
                and run.publish_name
                and run.publish_project_id
                and run.datasource_id is None
            ):
                _publish_datasource(session, run, result)
                _record_contract(session, run, contract_report, "publish")
            # the webhooks' deliveries are born in the same transaction: with the run closed, the
            # notice is there (read again here to have the new state and end)
            session.refresh(run)
            webhooks.enqueue_for_run(session, run)
            session.commit()  # claim + effect: atomic
            session.refresh(run)
            openlineage.run_closed(session, run)  # after the commit: tells a fact written already
            return run
        except Exception:
            session.rollback()  # the run goes back to non-terminal
            session.refresh(run)
            logger.warning(
                "run %s: post-claim effect failed (attempt %d/%d)", run.id, attempt, SIDE_EFFECT_ATTEMPTS
            )
    logger.error(
        "run %s: post-claim effect failed after %d attempts, it will be tried again at the next read",
        run.id,
        SIDE_EFFECT_ATTEMPTS,
    )
    return run


def _last_successful_refresh(session: Session, ds_id: int) -> str | None:
    """When the last successful refresh of a datasource STARTED (ISO, UTC): the
    value of {last_refresh} in an API call. The start, not the end: what changed
    while it ran is asked again next time rather than lost."""
    started = session.exec(
        select(Run.started_at)
        .where(Run.datasource_id == ds_id, Run.kind == "ingest", Run.status == "SUCCESS")
        .order_by(Run.started_at.desc())
        .limit(1)
    ).first()
    if started is None:
        return None
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)  # stored naive, UTC by construction
    return started.astimezone(timezone.utc).isoformat()


_TEMPLATE_KEYS = ("timezone", "variables", "last_refresh_initial", "mode", "keys")


def _template_part(ref: dict) -> dict:
    """The placeholders' zone, the variables and the refresh mode of a file
    datasource, as stored (only when it has them: a datasource without them is
    sent exactly as before)."""
    return {k: ref[k] for k in _TEMPLATE_KEYS if k in ref}


def _refresh_context(session: Session, ds: Datasource, ref: dict, fields: tuple[str, ...]) -> dict:
    """What a file datasource's refresh needs besides its definition: the start of
    the last successful refresh for {last_refresh} (only when its templates or
    variables could use it) and, in append mode, the current snapshot the new
    rows are merged into. Nothing for a datasource without placeholders — the
    worker of the previous version takes the same task."""
    out: dict = {}
    templated = any("{" in str(ref.get(f) or "") for f in fields) or bool(ref.get("variables"))
    if templated:
        last = _last_successful_refresh(session, ds.id)
        if last:
            out["last_refresh"] = last
    if ref.get("mode") == "append" and ds.key:
        out["previous"] = {"bucket": ds.bucket, "key": ds.key}
    return out


PLACEHOLDER_TASK = "pending-"
# far more than the launch takes (one request to the engine), far less than a stale run
NEVER_QUEUED_AFTER_SECONDS = 300


def _close_if_never_queued(session: Session, run: Run) -> Run:
    if _age_seconds(run.started_at) <= NEVER_QUEUED_AFTER_SECONDS:
        return run
    session.exec(update(Run).where(Run.id == run.id, Run.status.not_in(TERMINAL_STATES)).values(
        status="FAILURE", finished_at=datetime.now(timezone.utc),
        error="The run was never queued on the engine: the gateway stopped while launching it. Launch it again.",
    ))
    session.commit()
    session.refresh(run)
    return run


def _finalize_ingest(session: Session, run: Run, result: dict) -> None:
    """The refresh succeeded: the snapshot's swap (only the winner of the claim gets here).
    Does NOT commit — `_reconcile` does, so the swap and the run's claim are in the same
    transaction (both, or neither). The previous snapshot is marked for DEFERRED
    deletion.

    The datasource is read FOR UPDATE: two reconciles closing two imports of it at
    once would otherwise both read the snapshot of before, and the older one, if it
    committed last, put its data back over the newer one."""
    ds = session.exec(
        select(Datasource).where(Datasource.id == run.datasource_id)
        .with_for_update().execution_options(populate_existing=True)
    ).first() if run.datasource_id else None
    if ds is None:
        # the datasource disappeared during the refresh: the new blob is orphaned (no reader
        # references it) → deferred deletion, the same as always.
        schedule_blob_deletion(
            session, run.output_bucket, run.output_key, reason="datasource gone during the refresh"
        )
        return

    # a run OLDER than the one that produced the current snapshot read the source
    # earlier: its data is staler and must not replace the fresher just because it
    # finished later. Its parquet stays orphaned → deferred.
    if _is_stale_swap(ds, run):
        logger.warning(
            "run %s: swap refused, datasource %s has the snapshot of run %s already (newer)",
            run.id, ds.id, ds.snapshot_run_id,
        )
        schedule_blob_deletion(
            session, run.output_bucket, run.output_key,
            reason=f"refresh {run.id} overtaken by run {ds.snapshot_run_id}",
        )
        session.flush()
        return

    old_bucket, old_key = ds.bucket, ds.key
    now = datetime.now(timezone.utc)
    ds.bucket = result.get("bucket") or run.output_bucket
    ds.key = run.output_key
    ds.rows = result.get("rows_written")
    ds.columns = json.dumps(result.get("columns") or [])
    ds.refreshed_at = now
    ds.updated_at = now
    ds.snapshot_run_id = run.id
    session.add(ds)
    # the previous snapshot may still be read by a run/preview that resolved its key
    # already: DEFERRED deletion (grace).
    if old_key and old_key != ds.key:
        schedule_blob_deletion(
            session, old_bucket, old_key, reason=f"snapshot datasource {ds.id} superato"
        )
    session.flush()


def _publish_datasource(session: Session, run: Run, result: dict) -> None:
    """Creates — or OVERWRITES, when `run.publish_overwrite` — the datasource the run
    promised (only the winner of the claim gets here). A blob an overwrite replaces is
    marked for deferred deletion.

    The UNIQUE constraint (project, name) stays the safety net: at CREATION, on a
    conflict it tries ONCE more with a suffix; when that fails too (or the project was
    deleted) the output is not published — its parquet, orphaned now, is marked for
    deletion (no blob lost).
    """
    columns = json.dumps(result.get("columns") or [])

    # overwrite: replaces in place the datasource with the same name (kind="flow"),
    # keeping its id and name — so the flows that use it as a source do not break. When
    # it does not exist (or is not kind=flow) it falls back to creation.
    if run.publish_overwrite:
        existing = _find_datasource(session, run.publish_project_id, run.publish_name)
        if existing is not None and existing.kind == "flow":
            # as for the refresh: whoever read EARLIER does not overwrite whoever read LATER
            if _is_stale_swap(existing, run):
                logger.warning(
                    "run %s: publish refused, datasource %s has the snapshot of run %s already",
                    run.id, existing.id, existing.snapshot_run_id,
                )
                schedule_blob_deletion(
                    session, run.output_bucket, run.output_key,
                    reason=f"publish {run.id} overtaken by run {existing.snapshot_run_id}",
                )
                session.flush()
                return
            old_bucket, old_key = existing.bucket, existing.key
            existing.bucket = run.output_bucket
            existing.key = run.output_key
            existing.rows = result.get("rows_written")
            existing.columns = columns
            existing.sort_keys = run.publish_sort_keys
            existing.description = run.publish_description
            existing.flow_id = run.flow_id
            existing.owner_id = existing.owner_id or run.launched_by
            existing.snapshot_run_id = run.id
            run.datasource_id = existing.id
            session.add(existing)
            session.add(run)
            # the replaced parquet may still be read: DEFERRED deletion (grace), in the same
            # transaction as the overwrite.
            if old_key and old_key != run.output_key:
                schedule_blob_deletion(
                    session, old_bucket, old_key, reason=f"datasource {existing.id} sovrascritta"
                )
            session.flush()  # does NOT commit: _reconcile does (atomic with the claim)
            return

    def _make(name: str) -> Datasource:
        return Datasource(
            name=name,
            description=run.publish_description,
            project_id=run.publish_project_id,
            owner_id=run.launched_by,
            bucket=run.output_bucket,
            key=run.output_key,
            rows=result.get("rows_written"),
            columns=columns,
            sort_keys=run.publish_sort_keys,
            kind="flow",
            flow_id=run.flow_id,
            snapshot_run_id=run.id,  # baseline for the next publishes
        )

    first = run.publish_name
    if _datasource_name_taken(session, run.publish_project_id, first):
        first = f"{run.publish_name} ({run.task_id[:8]})"  # a conflict that came up meanwhile
    for candidate in dict.fromkeys([first, f"{run.publish_name} ({run.task_id[:8]})"]):
        ds = _make(candidate)
        try:
            # SAVEPOINT: isolates the attempt. On a UNIQUE conflict ONLY the savepoint is undone
            # (not the whole transaction, which carries the claim too) and the next name is
            # tried.
            with session.begin_nested():
                session.add(ds)
                session.flush()  # issues the INSERT now → IntegrityError here when taken
        except IntegrityError:
            continue
        run.datasource_id = ds.id
        session.add(run)
        session.flush()  # does NOT commit: _reconcile does (atomic with the claim)
        return
    # attempts used up: the output is not published → its parquet is orphaned. It is
    # marked for deferred deletion instead of being left lost.
    logger.error(
        "run %s: datasource '%s' not published (repeated conflicts or project deleted)",
        run.id,
        run.publish_name,
    )
    schedule_blob_deletion(
        session, run.output_bucket, run.output_key, reason=f"publish run {run.id} fallito"
    )
    session.flush()


@router.get("/flows/{flow_id}/runs", response_model=list[RunOut])
async def list_runs(
    flow_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    def read() -> list[Run]:  # in a worker thread: the event loop never waits for the database
        flow = _get_flow(session, flow_id)
        ensure_can(session, user, flow.project_id, Capability.VIEW)
        return list(session.exec(
            select(Run).where(Run.flow_id == flow_id).order_by(Run.started_at.desc()).limit(50)
        ).all())

    runs = await run_in_threadpool(read)
    return [run_out(session, await _reconcile(session, r)) for r in runs]


# NB: registered BEFORE /runs/{run_id} so that "activity" is not taken for an id
@router.get("/runs/activity", response_model=RunActivityOut)
def runs_activity(
    days: int = Query(180, ge=1, le=730, description="width of the daily window"),
    day: str | None = Query(None, description="YYYY-MM-DD (local): returns the day's HOURLY detail"),
    tz_offset: int = Query(0, description="getTimezoneOffset() del client, in minuti (local = utc - offset)"),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """The runs' activity for the calendar plot of the Flows page: counts by DAY (heatmap)
    or, with `day`, by HOUR (drill-down). Each run is an event; the breakdown tells the
    outcome (successes/failures) and the origin (manual/scheduled) apart. RBAC as the
    search: only the runs in readable projects (or launched by the user). The buckets
    are in the client's LOCAL time, worked out from `tz_offset`."""
    off = timedelta(minutes=tz_offset)  # local = utc - off ; utc = local + off
    now_local = datetime.now(timezone.utc).replace(tzinfo=None) - off

    if day:
        try:
            d0 = datetime.strptime(day, "%Y-%m-%d")
        except ValueError:
            raise HTTPException(status_code=422, detail="Invalid date (expected YYYY-MM-DD)")
        start_local, end_local, granularity = d0, d0 + timedelta(days=1), "hour"
    else:
        today0 = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        end_local, granularity = today0 + timedelta(days=1), "day"
        start_local = end_local - timedelta(days=days)

    start_utc, end_utc = start_local + off, end_local + off  # the UTC bounds of the local window

    # TOP-LEVEL runs only: the outputs/refreshes that are children of an orchestration are
    # the same run counted several times
    conds = [Run.started_at >= start_utc, Run.started_at < end_utc, Run.parent_run_id.is_(None)]
    if not perm_service.is_admin(session, user):
        readable = perm_service.readable_project_ids(session, user)
        conds.append(
            or_(
                Flow.project_id.in_(readable),
                Datasource.project_id.in_(readable),
                FlowGroup.project_id.in_(readable),
                # a saved script's runs to whoever sees the script (saved_scripts.can_see): its
                # folder AND its environment, as get_run
                and_(SavedScript.project_id.in_(readable),
                     SavedScript.environment_id.in_(_usable_envs(session, user) or [-1])),
                Run.launched_by == user.id,
            )
        )
    # counted by the database, one row per calendar cell: loading the runs to count
    # them in Python took 1.4 s and 74 MB per call with a year of history
    cell = run_history.local_bucket(session, off, granularity)
    stmt = (
        select(
            cell.label("cell"),
            func.count(),
            func.sum(case((Run.status == "SUCCESS", 1), else_=0)),
            func.sum(case((Run.status == "FAILURE", 1), else_=0)),
            func.sum(case((Run.trigger_type == "schedule", 1), else_=0)),
        )
        .select_from(Run)
        .join(Flow, Run.flow_id == Flow.id, isouter=True)
        .join(Datasource, Run.datasource_id == Datasource.id, isouter=True)
        .join(FlowGroup, Run.flow_group_id == FlowGroup.id, isouter=True)
        .join(SavedScript, Run.saved_script_id == SavedScript.id, isouter=True)
        .group_by(literal_column("cell"))  # the alias: the expression carries bound parameters
    )
    for c in conds:
        stmt = stmt.where(c)

    buckets: dict[str, dict] = {}
    for key, total, success, failure, scheduled in session.exec(stmt).all():
        buckets[key] = {"total": total, "success": success or 0, "failure": failure or 0,
                        "scheduled": scheduled or 0, "manual": total - (scheduled or 0)}

    items = [ActivityBucket(key=k, **v) for k, v in sorted(buckets.items())]
    if granularity == "hour":
        from_key, to_key = "00", "23"
    else:
        from_key = start_local.strftime("%Y-%m-%d")
        to_key = (end_local - timedelta(days=1)).strftime("%Y-%m-%d")
    return RunActivityOut(granularity=granularity, from_key=from_key, to_key=to_key, buckets=items)


@router.get("/runs/{run_id}", response_model=RunOut)
async def get_run(
    run_id: int,
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    def read() -> Run:  # in a worker thread: the event loop never waits for the database
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(status_code=404, detail="Run not found")
        if run.kind == "ingest":
            ds = session.get(Datasource, run.datasource_id) if run.datasource_id else None
            if ds is not None:
                ensure_can(session, user, ds.project_id, Capability.VIEW)
            elif not (perm_service.is_admin(session, user) or run.launched_by == user.id):
                # datasource deleted: the orphaned history stays visible only to whoever launched it
                raise HTTPException(status_code=404, detail="Run not found")
        elif run.flow_group_id is not None:
            # a flow group's run: read with VIEW on the group's folder
            group = session.get(FlowGroup, run.flow_group_id)
            if group is None:
                raise HTTPException(status_code=404, detail="Run not found")
            ensure_can(session, user, group.project_id, Capability.VIEW)
        elif run.saved_script_id is not None or (run.kind == "script" and run.flow_id is None):
            # a saved script's run (or one of a script since deleted): whoever sees the
            # script; an orphan only to whoever launched it
            from app.services import saved_scripts

            script = session.get(SavedScript, run.saved_script_id) if run.saved_script_id else None
            if script is None:
                if not (perm_service.is_admin(session, user) or run.launched_by == user.id):
                    raise HTTPException(status_code=404, detail="Run not found")
            elif not saved_scripts.can_see(session, user, script):
                raise HTTPException(status_code=404, detail="Run not found")
        elif run.flow_id is None and run.parent_run_id is not None:
            # a container cell's run: read as its flow group's run is
            parent = session.get(Run, run.parent_run_id)
            group = session.get(FlowGroup, parent.flow_group_id) if parent and parent.flow_group_id else None
            if group is None:
                if not (perm_service.is_admin(session, user) or run.launched_by == user.id):
                    raise HTTPException(status_code=404, detail="Run not found")
            else:
                ensure_can(session, user, group.project_id, Capability.VIEW)
        else:
            flow = _get_flow(session, run.flow_id)
            ensure_can(session, user, flow.project_id, Capability.VIEW)
            if run.kind in ("script", "container"):
                # its log is what the code printed: only to whoever may open the flow
                # (its environments and saved scripts), as the flow itself
                from app.services import flow_access

                if flow_access.blocked(session, user, flow.definition):
                    raise HTTPException(status_code=404, detail="Run not found")
        return run

    run = await run_in_threadpool(read)
    return run_out(session, await _reconcile(session, run))


def _usable_envs(session: Session, user: User) -> set[int]:
    from app.services import saved_scripts

    return saved_scripts.usable_environment_ids(session, user) or set()


@router.get("/runs", response_model=Page[RunSearchOut])
def search_runs(
    status: str | None = Query(None, description="filter by state, e.g. FAILURE"),
    q: str | None = Query(None, description="text searched in flow/source/author/error, over the WHOLE dataset"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: Session = Depends(get_session),
):
    """GLOBAL, PAGINATED search of the runs in the user's READABLE projects (all of them
    for a superuser), with a filter by state and a text search on error/error_detail
    (over the whole dataset, not only the page). Read only: it shows the historical
    state, it does NOT reconcile (a search must have no effects)."""
    conds = []
    if not perm_service.is_admin(session, user):
        readable = perm_service.readable_project_ids(session, user)
        conds.append(
            or_(
                Flow.project_id.in_(readable),
                Datasource.project_id.in_(readable),
                FlowGroup.project_id.in_(readable),
                # a saved script's runs to whoever sees the script (saved_scripts.can_see): its
                # folder AND its environment, as get_run
                and_(SavedScript.project_id.in_(readable),
                     SavedScript.environment_id.in_(_usable_envs(session, user) or [-1])),
                Run.launched_by == user.id,
            )
        )
    if status:
        conds.append(Run.status == status)
    if q:
        like = f"%{q}%"
        # search over several fields (not only the error, or the successful runs — which have
        # no error text — would NEVER show up): flow name, source, author and the error's
        # reason if any.
        conds.append(
            or_(
                Run.error.ilike(like),
                Run.error_detail.ilike(like),
                Flow.name.ilike(like),
                Datasource.name.ilike(like),
                FlowGroup.name.ilike(like),
                SavedScript.name.ilike(like),
                User.full_name.ilike(like),
                User.email.ilike(like),
            )
        )

    def _with_joins(stmt):
        return (
            stmt.join(Flow, Run.flow_id == Flow.id, isouter=True)
            .join(Datasource, Run.datasource_id == Datasource.id, isouter=True)
            .join(FlowGroup, Run.flow_group_id == FlowGroup.id, isouter=True)
            .join(SavedScript, Run.saved_script_id == SavedScript.id, isouter=True)
            .join(User, Run.launched_by == User.id, isouter=True)
        )

    items_stmt = _with_joins(select(Run, Flow, Datasource, FlowGroup, SavedScript, User))
    count_stmt = _with_joins(select(func.count()).select_from(Run))
    for c in conds:
        items_stmt = items_stmt.where(c)
        count_stmt = count_stmt.where(c)
    total = session.exec(count_stmt).one()
    rows = session.exec(
        items_stmt.order_by(Run.started_at.desc()).limit(limit).offset(offset)
    ).all()

    out: list[RunSearchOut] = []
    for run, flow, ds, group, script, launcher in rows:
        item = RunSearchOut.model_validate(run, from_attributes=True)
        item.flow_name = flow.name if flow else None
        item.source_name = ds.name if ds else None
        item.group_name = group.name if group else None
        item.script_name = script.name if script else None
        # who started it: for the scheduled ones the client shows "schedule"
        item.launched_by_name = (launcher.full_name or launcher.email) if launcher else None
        out.append(item)
    return Page(items=out, total=total)
