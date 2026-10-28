"""A saved script run on its own: «Run» on its page, its schedule, a cell of a flow
group. See docs/specs/saved-scripts.md («Inputs», «Output», «Runs»).

The run is queued as a flow's: a kind="orchestration" row with `saved_script_id`,
claimed by an orchestrator, which hands it here from `orchestrator.run_it`, with the
heartbeat and the closing of orphans every orchestration has. Under it a
kind="script" run, as a Script node's, holds the attempts, the log, the exit code
and the output.

1. The checks, with the run's authority (who ran it, or the schedule's author): the
   script still visible to them, RUN on its folder, its environment usable and
   built, the scripts bucket configured.
2. Each declared input, read again now: the reader may still read it, and its
   current parquet file is copied to the scripts bucket at
   `script-<id>/<step id>/in-<ALIAS>.parquet`. The script keeps the datasource's id,
   which a refresh never changes, while the file is a new one at every refresh; the
   copy also shields the run from a refresh that lands while it works.
3. The Job in the scripts namespace, as a Script node's (`script_runs._attempt`),
   with TABULARIA_INPUT_<ALIAS>, TABULARIA_INPUTS and TABULARIA_OUTPUT.
4. What the script wrote at TABULARIA_OUTPUT, if anything, copied into Tabularia's
   storage: the script's page previews it, and with `output.publish` it becomes a
   datasource as an Output node's result does (EDIT on that folder, again with the
   run's authority). Over a datasource that is there, as every writer's data: its
   data contract is checked on the copy first (a blocking rule broken leaves it as it
   was and fails the run), and the description someone wrote on it stays. Writing
   nothing is fine: the script may send emails or load another system.
5. The step's files deleted from the scripts bucket, whatever happened.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Optional

from sqlalchemy import text
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.core.engine_client import get_engine_client
from app.models import Datasource, Run, SavedScript, ScriptEnvironment, ScriptEnvironmentVersion, ScriptStorage, User
from app.models.permission import Capability
from app.services import contracts as contract_service
from app.services import kube_jobs, saved_scripts, script_builds, script_nodes
from app.services import permissions as perm_service
from app.services.script_runs import (
    LABEL_ORCHESTRATION, LABEL_SCRIPT_RUN, ScriptFailed, _attempt, _code_items, _delete_objects, _detail, _now,
    _storage_env, _variables, job_manifest, run_still_open, storage_payload,
)

logger = logging.getLogger(__name__)

# what Tabularia sets in a saved script's environment (TABULARIA_INPUT_<ALIAS> besides)
SAVED_SCRIPT_VARIABLES = (
    "TABULARIA_INPUTS", "TABULARIA_OUTPUT", "TABULARIA_STEP_ID", "TABULARIA_RUN_ID", "TABULARIA_SCRIPT_ID",
    "TABULARIA_SCRIPT_NAME", "TABULARIA_SCRIPT_VERSION", "TABULARIA_ATTEMPT",
)
# the advisory locks of «queue a saved script's run», in a namespace of their own
_SCRIPT_QUEUE_LOCK = 74610003


# ── the queue ─────────────────────────────────────────────────────────────────
def in_line_for_the_script(session: Session, script_id: int) -> None:
    """As the flows' and the groups': two replicas, or a replica and the scheduler,
    cannot both see no run and both queue one."""
    if session.get_bind().dialect.name == "postgresql":
        session.connection().execute(
            text("SELECT pg_advisory_xact_lock(:space, :script)"), {"space": _SCRIPT_QUEUE_LOCK, "script": script_id}
        )


def script_running(session: Session, script_id: int) -> bool:
    """The script has a run queued or in progress."""
    return session.exec(
        select(Run.id)
        .where(Run.kind == "orchestration", Run.saved_script_id == script_id,
               Run.status.in_(("PENDING", "STARTED")))  # type: ignore[union-attr]
        .limit(1)
    ).first() is not None


def create_script_run(session: Session, user: User, script: SavedScript, trigger_type: str = "manual",
                      commit: bool = True) -> Run:
    """Queues a run of the script: the PENDING orchestration row a flow gets, with
    `saved_script_id` instead of `flow_id`, run with `user`'s authority."""
    run = Run(kind="orchestration", saved_script_id=script.id, task_id="", status="PENDING", launched_by=user.id,
              trigger_type=trigger_type, engine_mode="production", input_key="", output_bucket="", output_key="")
    session.add(run)
    if commit:
        session.commit()
        session.refresh(run)
    return run


async def run_queued(run_id: int) -> None:
    """Carries a claimed run of a saved script to its end and records the outcome."""
    from app.services import orchestrator as orch

    with Session(orch.engine) as session:
        run = session.get(Run, run_id)
        if run is None:
            return
        script = session.get(SavedScript, run.saved_script_id)
        user = session.get(User, run.launched_by) if run.launched_by else None
        if script is None or user is None or not user.is_active:
            await orch._finalize_orch_run(run_id, "FAILURE", "saved script or user no longer valid")
            return
        if run.trigger_type == "schedule":
            # the schedule's author must still hold everything the run will need
            from app.services import schedule_authority

            gaps = schedule_authority.gaps_of_script(session, user, script)
            if gaps:
                await orch._finalize_orch_run(run_id, "FAILURE", schedule_authority.not_started(user, gaps))
                return
        try:
            await execute(session, user, script, parent_run_id=run_id, trigger_type=run.trigger_type)
        except ScriptFailed as e:
            await orch._finalize_orch_run(run_id, "FAILURE", str(e))
            return
        await orch._finalize_orch_run(run_id, "SUCCESS")


# ── inputs ────────────────────────────────────────────────────────────────────
def folder_path(session: Session, project_id: Optional[int]) -> str:
    if project_id is None:
        return ""
    projects = perm_service._all_projects(session)
    ids = perm_service.ancestor_ids({i: p.parent_id for i, p in projects.items()}, project_id)
    return " / ".join(projects[i].name for i in reversed(ids) if i in projects) if ids else ""


def check_inputs(session: Session, user: User, content: dict) -> list[tuple[str, Datasource]]:
    """(alias, datasource) of every declared input, each still there, readable by
    `user` and with data: the first that is not fails the run, saying which."""
    out = []
    for item in content.get("inputs") or []:
        alias = str(item.get("alias") or "")
        ds = session.get(Datasource, item.get("datasource_id")) if isinstance(item.get("datasource_id"), int) else None
        if ds is None:
            raise ScriptFailed(f"the input {alias} is a datasource that no longer exists: choose another one")
        if not perm_service.has_capability(session, user, ds.project_id, Capability.VIEW):
            raise ScriptFailed(f"{user.email} can no longer read the datasource «{ds.name}» (input {alias})")
        if not ds.key:
            raise ScriptFailed(f"the datasource «{ds.name}» (input {alias}) has no data yet")
        out.append((alias, ds))
    return out


def _columns(ds: Datasource) -> list[str]:
    try:
        return [c.get("name") for c in json.loads(ds.columns or "[]") if isinstance(c, dict)]
    except ValueError:
        return []


async def prepare_inputs(session: Session, inputs: list[tuple[str, Datasource]], storage: ScriptStorage,
                         prefix: str) -> tuple[dict[str, str], dict, list[str]]:
    """Copies each input to the scripts bucket: (its TABULARIA_INPUT_<ALIAS>
    variables, TABULARIA_INPUTS, the keys written)."""
    env, described, keys = {}, {}, []
    bucket_url = f"s3://{storage.bucket}"
    for alias, ds in inputs:
        key = f"{prefix}/in-{alias}.parquet"
        resp = await get_engine_client().post("/scripts/input/export", json={
            "connection": storage_payload(storage), "bucket": ds.bucket, "key": ds.key, "target_key": key})
        if resp.status_code >= 400:
            raise ScriptFailed(f"the datasource «{ds.name}» (input {alias}) could not be copied for the script: "
                               f"{_detail(resp)}")
        keys.append(key)
        env[f"TABULARIA_INPUT_{alias}"] = f"{bucket_url}/{key}"
        described[alias] = {"datasource_id": ds.id, "name": ds.name, "folder": folder_path(session, ds.project_id),
                            "uri": f"{bucket_url}/{key}", "columns": _columns(ds)}
    return env, described, keys


# ── output ────────────────────────────────────────────────────────────────────
def replaced_datasource(session: Session, publish: dict) -> Optional[Datasource]:
    """The datasource the output will replace, as `_publish_datasource` decides: the one
    under its name in its folder, written by a flow or a script, when the script
    overwrites (otherwise the output is a datasource of its own)."""
    from app.routes.runs import _find_datasource

    if not publish.get("overwrite", True):
        return None
    ds = _find_datasource(session, publish["project_id"], str(publish["name"]))
    return ds if ds is not None and ds.kind == "flow" else None


def _publish(session: Session, child: Run, script: SavedScript, publish: dict, target: Optional[Datasource],
             out: dict) -> None:
    """The output becomes the datasource, as a flow Output's at SUCCESS (routes/runs.py
    `_reconcile`): with a blocking rule of the replaced datasource's contract broken it
    does not, and the refusal goes to the contract's history. Does NOT commit."""
    from app.routes.runs import _publish_datasource, _record_contract

    report = out.get("contract") if isinstance(out.get("contract"), dict) and target is not None else None
    if report is not None and contract_service.payload(session, target.id) is None:
        report = None  # the contract was removed or turned off meanwhile: it decides nothing
    if report is not None and report.get("outcome") == "failed":
        contract_service.record_result(session, contract_service.get(session, target.id), report, trigger="publish",
                                       run_id=child.id, snapshot_key=child.output_key, blocked=True)
        raise ScriptFailed(contract_service.broken_rules_message(report))
    child.publish_name, child.publish_project_id = str(publish["name"]), publish["project_id"]
    child.publish_overwrite = bool(publish.get("overwrite", True))
    # a description someone wrote on the datasource stays: people and the AI assistant
    # read it to choose the table, and the script has no field for it
    child.publish_description = target.description if target is not None and target.description \
        else f"Written by the saved script «{script.name}»"
    _publish_datasource(session, child, {"columns": out.get("columns") or [], "rows_written": out.get("rows")})
    _record_contract(session, child, report, "publish")


# ── a run ─────────────────────────────────────────────────────────────────────
def _environment(session: Session, user: User, script: SavedScript) -> tuple[ScriptEnvironment, ScriptEnvironmentVersion]:
    reason = script_builds.disabled_reason()
    if reason:
        raise ScriptFailed("scripting environments are off on this installation"
                           if reason == "kubernetes" else "scripting environments are not configured")
    env = session.get(ScriptEnvironment, script.environment_id)
    if env is None:
        raise ScriptFailed(f"the saved script «{script.name}» has no scripting environment")
    if not env.enabled:
        raise ScriptFailed(f"the scripting environment «{env.name}» is disabled")
    version = session.get(ScriptEnvironmentVersion, env.current_version_id) if env.current_version_id else None
    if version is None or version.status != "ready" or not version.image_ref:
        raise ScriptFailed(f"the scripting environment «{env.name}» has no version built yet")
    if not script_nodes.can_use(session, user, env):
        raise ScriptFailed(f"{user.email} cannot run scripts in the environment «{env.name}»")
    return env, version


async def execute(session: Session, user: User, script: SavedScript, *, parent_run_id: int,
                  trigger_type: str) -> Run:
    """Runs the script's current version with `user`'s authority under the
    orchestration `parent_run_id`; returns its script run. Raises ScriptFailed."""
    if not saved_scripts.can_see(session, user, script):
        raise ScriptFailed(f"{user.email} can no longer see the saved script «{script.name}»")
    if not perm_service.has_capability(session, user, script.project_id, Capability.RUN):
        raise ScriptFailed(f"RUN missing on the folder of the saved script «{script.name}»")
    env, version = _environment(session, user, script)
    storage = session.get(ScriptStorage, 1)
    if storage is None or not (storage.bucket and storage.access_key and storage.secret_key_enc):
        raise ScriptFailed("the scripts bucket is not configured (Administration → Scripting environments)")
    content = saved_scripts.content_of(session, script)
    node = saved_scripts._as_node(script.name, content)
    try:
        script_nodes.check_files(node, env.language, storage.bucket, complete=True)
        script_nodes.check_settings(node, env)
    except Exception as e:  # HTTPException from the checks: its detail is the message
        raise ScriptFailed(getattr(e, "detail", str(e))) from None
    inputs = check_inputs(session, user, content)
    publish = content.get("output") or {}
    if publish.get("publish"):
        if not publish.get("name") or not isinstance(publish.get("project_id"), int):
            raise ScriptFailed(f"the saved script «{script.name}» publishes its output, but without a name or a folder")
        if not perm_service.has_capability(session, user, publish["project_id"], Capability.EDIT):
            raise ScriptFailed(f"{user.email} cannot write in the folder where «{script.name}» publishes its output")

    ns = get_settings().scripts.namespace
    default_bucket = get_settings().engine.bucket
    step_id = str(uuid.uuid4())
    prefix = f"script-{script.id}/{step_id}"
    out_key = f"{prefix}/output.parquet"
    engine_out_key = f"out/{uuid.uuid4().hex}.parquet"
    plain, secret_vars, snapshot = _variables(session, env)
    info = {
        "saved_script_id": script.id, "label": script.name, "script_version": script.current_version,
        "environment_id": env.id, "environment": env.name, "language": env.language, "version": version.number,
        "image": version.image_ref, "step_id": step_id, "output": True, "entrypoint": content.get("entrypoint"),
        "files": [f["path"] for f in script_nodes.files_of(node)], "attempts": 0, "exit_code": None,
        "inputs": [{"alias": a, "datasource_id": ds.id, "name": ds.name} for a, ds in inputs],
        "publish": bool(publish.get("publish")), "variables": snapshot, "log": "", "namespace": ns,
    }
    child = Run(kind="script", saved_script_id=script.id, task_id=uuid.uuid4().hex, launched_by=user.id,
                trigger_type=trigger_type, parent_run_id=parent_run_id, input_key="", status="STARTED",
                engine=f"{env.name} v{version.number}", script=json.dumps(info), output_bucket=default_bucket,
                output_key=engine_out_key)
    session.add(child)
    session.commit()
    session.refresh(child)

    def save(**values) -> None:
        for k, v in values.items():
            setattr(child, k, v)
        child.script = json.dumps(info)
        session.add(child)
        session.commit()

    written: list[str] = [out_key]
    try:
        input_env, described, keys = await prepare_inputs(session, inputs, storage, prefix)
        written += keys
        s = get_settings().scripts
        timeout = int(content.get("timeout_seconds", 900))
        retries = int(content.get("retries", 0))
        delay = int(content.get("retry_delay_seconds", 60))
        code, items = _code_items(node)
        base_env = {
            **input_env, "TABULARIA_INPUTS": json.dumps(described, ensure_ascii=False),
            "TABULARIA_OUTPUT": f"s3://{storage.bucket}/{out_key}", "TABULARIA_STEP_ID": step_id,
            "TABULARIA_RUN_ID": str(parent_run_id), "TABULARIA_SCRIPT_ID": str(script.id),
            "TABULARIA_SCRIPT_NAME": script.name, "TABULARIA_SCRIPT_VERSION": str(script.current_version),
            "HOME": "/tmp", "TMPDIR": "/tmp", **_storage_env(storage), **plain,
        }
        secret = {"AWS_ACCESS_KEY_ID": storage.access_key,
                  "AWS_SECRET_ACCESS_KEY": decrypt_secret(storage.secret_key_enc), **secret_vars}
        labels = {"app.kubernetes.io/managed-by": "tabularia", "app.kubernetes.io/component": "scripting",
                  script_builds.LABEL_ENV: str(env.id), LABEL_SCRIPT_RUN: str(child.id),
                  LABEL_ORCHESTRATION: str(parent_run_id), "tabularia.io/stage": "run"}
        ok, reason, logs = False, "", []
        for attempt in range(1, retries + 2):
            name = f"tabularia-script-{child.id}-a{attempt}"
            manifest = job_manifest(name=name, env=env, version=version, node=node,
                                    environment={**base_env, "TABULARIA_ATTEMPT": str(attempt)}, labels=labels,
                                    timeout=timeout, code_items=items, code_name=f"{name}-code",
                                    secret_name=f"{name}-env")
            info["attempts"] = attempt
            save()
            ok, log, exit_code, reason = await _attempt(name=name, manifest=manifest, code=code, secret=secret,
                                                        timeout=timeout, ns=ns, on_tick=session.commit)
            logs.append(f"── attempt {attempt} ──\n{log}" if retries else log)
            info["log"] = kube_jobs.tail("\n".join(logs), s.log_max_bytes)
            info["exit_code"] = exit_code
            save()
            if ok:
                break
            if attempt <= retries:
                logger.info("saved script: run %s attempt %s failed (%s), again in %ss", child.id, attempt, reason, delay)
                await asyncio.sleep(delay)
                # as a Script node's retries (script_runs): a run closed meanwhile is not run
                # again, and what the failed attempt wrote is not the next one's output
                if not run_still_open(session, parent_run_id):
                    raise ScriptFailed(f"the saved script «{script.name}» failed: {reason} (not run again: its run was closed)")
                await _delete_objects(storage, [out_key])
        if not ok:
            raise ScriptFailed(f"the saved script «{script.name}» failed: {reason}")

        # the datasource the output will replace, read now (the script may have run for
        # hours): its data contract is checked on the copy, before it replaces anything
        target = replaced_datasource(session, publish) if publish.get("publish") else None
        contract = contract_service.payload(session, target.id) if target is not None else None
        resp = await get_engine_client().post("/scripts/output/import", json={
            "connection": storage_payload(storage), "key": out_key, "bucket": default_bucket,
            "output_key": engine_out_key, **({"contract": contract} if contract else {})})
        if resp.status_code >= 400:
            raise ScriptFailed(f"the saved script «{script.name}»: its output could not be read: {_detail(resp)}")
        out = resp.json()
        if not out.get("found"):
            info["output"] = False
            save(output_key="", output_bucket="", status="SUCCESS", finished_at=_now())
            return child
        info["columns"] = out.get("columns") or []
        save(rows_written=out.get("rows"))
        if publish.get("publish"):
            _publish(session, child, script, publish, target, out)
        save(status="SUCCESS", finished_at=_now())
        return child
    except ScriptFailed as e:
        save(status="FAILURE", error=str(e)[:2000], finished_at=_now())
        raise
    except Exception as e:
        message = getattr(e, "detail", None) or str(e) or type(e).__name__
        save(status="FAILURE", error=f"the saved script «{script.name}»: {message}"[:2000], finished_at=_now())
        raise ScriptFailed(f"the saved script «{script.name}»: {message}") from e
    finally:
        try:
            await get_engine_client().post("/scripts/objects/delete", json={
                "connection": storage_payload(storage), "keys": written})
        except Exception as e:  # noqa: BLE001 — the bucket's lifecycle rule removes them
            logger.warning("saved script: step %s not cleaned up (%s)", step_id, type(e).__name__)
