"""A Script node, run inside a flow's orchestration (services/orchestrator.py). See
docs/specs/script-node.md; what a node holds is in services/script_nodes.py.

1. Input, when a data edge enters the node: the chain before it is run by the
   engine like an Output, written as one parquet file to the scripts bucket at
   `env-<environment>/<step id>/input.parquet` (the step id is a random UUID).
2. The Job, in the scripts namespace (the editor's trials: in a namespace of their
   own, SCRIPTS__TRIAL_NAMESPACE): the environment's current image, by digest;
   the node's files in a ConfigMap mounted read-only at /tabularia/code, the working
   folder, so a module next to the entrypoint is imported (Python) or sourced (R) as
   it is; the bucket's key and the secret variables in a Secret. Both go away with
   the Job. The run waits for it, within the node's timeout, and reads its log.
   A failed attempt is run again as a new Job, `retries` times, after
   `retryDelaySeconds`.
3. Output, when a data edge leaves the node: what the script wrote at
   TABULARIA_OUTPUT is copied into Tabularia's storage (out/…), where the nodes after
   it read it. A script that wrote nothing there fails the node.
4. The step's two files are deleted from the scripts bucket, whatever happened.

The node is a run of its own (`kind="script"`, a child of the orchestration): the
run page shows its attempts, its exit code, its log and the variables it was given
(the secret ones by name only). A failure stops the flow, as a failed refresh does:
the steps after it would read nothing, or stale data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from sqlalchemy import update
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.core.engine_client import get_engine_client
from app.models import (
    Flow, Run, ScriptEnvironment, ScriptEnvironmentVariable, ScriptEnvironmentVersion, ScriptStorage, User,
)
from app.models.run import TERMINAL_STATES
from app.services import kube_jobs, script_builds, script_nodes
from app.services.flow_resolver import resolve_script_input, script_has_output, script_input_edge
from app.services.kube_scaling import KubeError

logger = logging.getLogger(__name__)

# what Tabularia sets in every script's environment (the editor lists them too)
TABULARIA_VARIABLES = (
    "TABULARIA_INPUT", "TABULARIA_OUTPUT", "TABULARIA_STEP_ID", "TABULARIA_RUN_ID", "TABULARIA_FLOW_ID",
    "TABULARIA_FLOW_NAME", "TABULARIA_NODE_ID", "TABULARIA_ATTEMPT",
)
CODE_DIR = "/tabularia/code"
LABEL_SCRIPT_RUN = "tabularia.io/script-run"
# how long an editor's trial may stay PENDING: its process marks it STARTED within
# seconds (a Container trial asks the registry for the digest first, 15 s at most)
TRIAL_START_SECONDS = 300
# an editor's trial says it is alive this often (Run.heartbeat_at), from the process
# running it; one silent this long is closed: that process stopped (a gateway's Pod gone)
TRIAL_BEAT_SECONDS = 20
TRIAL_DEAD_SECONDS = 120
LABEL_ORCHESTRATION = "tabularia.io/run"
# the most the run waits past the node's timeout before it gives up on the cluster
GRACE_SECONDS = 180


class ScriptFailed(Exception):
    """The node failed; the message is the one the run page shows."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def storage_payload(storage: ScriptStorage) -> dict:
    """The scripts bucket as the engine reads an S3 connection (secret still encrypted)."""
    from app.routes.connections import _s3_payload

    return _s3_payload(storage.endpoint, storage.access_key, storage.secret_key_enc or "", storage.bucket,
                       storage.region)


def _storage_env(storage: ScriptStorage) -> dict[str, str]:
    env = {"AWS_REGION": storage.region or "us-east-1"}
    if storage.endpoint:
        env["AWS_ENDPOINT_URL"] = storage.endpoint
        if storage.endpoint.startswith("http://"):
            env["AWS_ALLOW_HTTP"] = "true"  # Polars' object_store refuses plain HTTP otherwise
    return env


def _variables(session: Session, env: ScriptEnvironment) -> tuple[dict[str, str], dict[str, str], dict]:
    """(plain, secret, snapshot): what goes in the Job's environment, what goes in its
    Secret, and what the run records — secret values never, only when they changed."""
    plain, secret, snapshot = {}, {}, {}
    rows = session.exec(select(ScriptEnvironmentVariable)
                        .where(ScriptEnvironmentVariable.environment_id == env.id)).all()
    for v in rows:
        if v.from_tabularia:
            value = os.environ.get(v.value)
            if value is None:
                raise ScriptFailed(f"the variable {v.name} hands over Tabularia's {v.value}, which is not set here")
            secret[v.name] = value
            snapshot[v.name] = {"from_tabularia": v.value}
        elif v.secret:
            secret[v.name] = decrypt_secret(v.value_enc or "")
            snapshot[v.name] = {"secret": True, "changed_at": v.updated_at.isoformat() if v.updated_at else None}
        else:
            plain[v.name] = v.value
            snapshot[v.name] = {"value": v.value}
    return plain, secret, snapshot


def _code_items(node: dict) -> tuple[dict[str, str], list[dict]]:
    """A ConfigMap's keys cannot hold "/": the files go in as f0, f1… and are mounted
    at their paths."""
    data, items = {}, []
    for i, f in enumerate(script_nodes.files_of(node)):
        data[f"f{i}"] = f["content"]
        items.append({"key": f"f{i}", "path": f["path"]})
    return data, items


def job_manifest(*, name: str, env: ScriptEnvironment, version: ScriptEnvironmentVersion, node: dict,
                 environment: dict[str, str], labels: dict, timeout: int, code_items: list[dict],
                 code_name: str, secret_name: str) -> dict:
    s = get_settings().scripts
    entry = (node.get("data") or {}).get("entrypoint")
    if env.language == "python":
        # -P: the modules are found through PYTHONPATH, not through the entrypoint's real
        # directory: the ConfigMap's files are links into a dated folder of Kubernetes
        # (..2026_10_21_08_36_26.3942651677/utils.py), which tracebacks would show
        command = ["python", "-P", entry]
        environment = {"PYTHONPATH": CODE_DIR, **environment}
    else:
        command = ["Rscript", entry]
    container = {
        "name": "script", "image": version.image_ref, "command": command, "workingDir": CODE_DIR,
        "env": [{"name": k, "value": v} for k, v in environment.items()],
        "envFrom": [{"secretRef": {"name": secret_name}}],
        "resources": {"requests": {"cpu": env.cpu_request, "memory": env.memory_request},
                      "limits": {"cpu": env.cpu_limit, "memory": env.memory_limit}},
        "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                            "capabilities": {"drop": ["ALL"]}},
        "volumeMounts": [{"name": "code", "mountPath": CODE_DIR, "readOnly": True},
                         {"name": "tmp", "mountPath": "/tmp"}],
    }
    pod: dict = {
        "restartPolicy": "Never", "automountServiceAccountToken": False,
        "securityContext": {"runAsNonRoot": True, "runAsUser": script_builds.SCRIPT_UID,
                            "runAsGroup": script_builds.SCRIPT_UID, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [container], **script_builds.node_selector(version),
        "volumes": [{"name": "code", "configMap": {"name": code_name, "items": code_items}},
                    {"name": "tmp", "emptyDir": {}}],
    }
    if s.registry_pull_secret:
        pod["imagePullSecrets"] = [{"name": s.registry_pull_secret}]
    return {
        "apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "labels": labels},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": timeout, "ttlSecondsAfterFinished": s.job_ttl_seconds,
                 "template": {"metadata": {"labels": labels}, "spec": pod}},
    }


# how long a Job may go without a Pod, the cluster refusing to create it, before the
# attempt fails saying why (a full quota, a Pod the namespace's policy refuses)
NO_POD_SECONDS = 10
# how long a Job's Pod may keep failing to download its image (the node retries a
# couple of times in it) before the attempt fails saying why
PULL_FAILURE_SECONDS = 20
# how long a finished Job's log may stay unreadable (its node lost: the API server cannot
# reach the kubelet) before the attempt ends without it
LOG_RETRY_SECONDS = 30


def transient(e: KubeError) -> bool:
    """An answer of the Kubernetes API worth asking again: not reached, throttled (API
    Priority and Fairness), or failing on its side (an upgrade of the control plane)."""
    return e.status == 0 or e.status == 429 or e.status >= 500


def run_still_open(session: Session, run_id: Optional[int]) -> bool:
    """The run is still open in the database (no one closed it meanwhile, as interrupted
    for instance): a new attempt starts only then."""
    if run_id is None:
        return True
    session.commit()  # a fresh read, not the transaction's snapshot
    status = session.exec(select(Run.status).where(Run.id == run_id)).first()
    return status is not None and status not in TERMINAL_STATES


def namespace_for(trial: bool) -> str:
    s = get_settings().scripts
    return (s.trial_namespace or s.namespace) if trial else s.namespace


def _memory_limit(manifest: dict) -> str:
    try:
        return manifest["spec"]["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"]
    except (KeyError, IndexError, TypeError):
        return ""


async def _attempt(*, name: str, manifest: dict, code: Optional[dict[str, str]], secret: dict[str, str],
                   timeout: int, ns: str, on_tick: Optional[Callable[[], None]] = None,
                   poll_seconds: Optional[float] = None, log_max_bytes: Optional[int] = None,
                   ) -> tuple[bool, str, Optional[int], str]:
    """One Job: (succeeded, log, exit code, reason when it failed). `code`: the files
    of the ConfigMap its Pod mounts, None for a Pod without (a Container node);
    `secret`: what goes in the Secret its container reads, when it reads one. Also
    services/container_runs.py's."""
    s = get_settings().scripts
    poll_seconds = poll_seconds or s.poll_seconds
    log_max_bytes = log_max_bytes or s.log_max_bytes
    labels = manifest["metadata"]["labels"]
    pod = manifest["spec"]["template"]["spec"]
    code_name = pod["volumes"][0]["configMap"]["name"] if code is not None else None
    env_from = pod["containers"][0].get("envFrom") or []
    secret_name = env_from[0]["secretRef"]["name"] if env_from else None
    job = await kube_jobs.create_job(ns, manifest)
    started = asyncio.get_running_loop().time()
    deadline = started + timeout + GRACE_SECONDS
    pull_since: Optional[float] = None
    from app.services.waits import waits

    pauses = waits(poll_seconds)
    try:
        # owned by the Job: they go away with it. Its Pod waits for them to exist.
        owners = kube_jobs.owned_by(job)
        if code_name:
            await kube_jobs.create_object(ns, "configmaps", {
                "apiVersion": "v1", "kind": "ConfigMap",
                "metadata": {"name": code_name, "labels": labels, "ownerReferences": owners}, "data": code})
        if secret_name:
            await kube_jobs.create_object(ns, "secrets", {
                "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
                "metadata": {"name": secret_name, "labels": labels, "ownerReferences": owners}, "stringData": secret})
        log_since: Optional[float] = None
        unscheduled, ran = "", False  # why its Pod waits for a node; whether its container ran
        while True:
            await asyncio.sleep(next(pauses))
            if on_tick:
                on_tick()
            now = asyncio.get_running_loop().time()
            try:
                current = await kube_jobs.get_job(ns, name)
                if current is None:
                    return False, "", None, f"the Job {name} is gone from the cluster before it ended"
                outcome = kube_jobs.job_outcome(current)
                status = current.get("status") or {}
                if (outcome is None and not any(status.get(k) for k in ("active", "ready", "succeeded", "failed"))
                        and now - started > NO_POD_SECONDS):
                    why = await kube_jobs.refused(ns, name)
                    if why:
                        return False, "", None, why
                if outcome is None and status.get("active"):
                    why, waiting, has_run = await kube_jobs.start_state(ns, name)
                    unscheduled, ran = waiting or unscheduled, ran or has_run
                    if not why:
                        pull_since = None
                    elif pull_since is None:
                        pull_since = now
                    elif now - pull_since >= PULL_FAILURE_SECONDS:
                        return False, "", None, why
                if outcome is not None:
                    try:
                        log, exit_code, ended = await kube_jobs.job_result(ns, name, log_max_bytes)
                    except kube_jobs.LogUnavailable as e:
                        # the Job has ended, its log does not come: asked again for a while,
                        # then the outcome is told without it
                        if not transient(e):
                            raise
                        log_since = log_since if log_since is not None else now
                        if now - log_since < LOG_RETRY_SECONDS:
                            continue
                        log, exit_code, ended = f"(the cluster did not give the log: {e})", e.exit_code, e.reason
                    if outcome == "succeeded":
                        return True, log, exit_code, ""
                    reason = kube_jobs.job_failure(current)
                    if "DeadlineExceeded" in reason:
                        reason = (f"its Pod never started within its timeout ({timeout} s): {unscheduled}"
                                  if unscheduled and not ran else f"it ran longer than its timeout ({timeout} s)")
                    elif exit_code is not None or ended:
                        reason = kube_jobs.explain_exit(exit_code, ended, _memory_limit(manifest))
                    elif "BackoffLimitExceeded" in reason and ran:
                        # its Pod went without an exit code (deleted with it): taken away by the cluster
                        reason = kube_jobs.STOPPED_BY_CLUSTER
                    return False, log, exit_code, reason or "the Job failed"
            except KubeError as e:
                # one answer of the API that failed is not the Job failing: ask again at the
                # next look, until the node's own deadline
                if not transient(e) or now > deadline:
                    raise
                logger.warning("Job %s: the Kubernetes API answered %s, asking again (%s)", name, e.status, e)
                continue
            if now > deadline:
                try:
                    log, exit_code, _ = await kube_jobs.job_result(ns, name, log_max_bytes)
                except KubeError as e:
                    log, exit_code = f"(the cluster did not give the log: {e})", getattr(e, "exit_code", None)
                return False, log, exit_code, "the cluster did not end the Job within its timeout"
    finally:
        try:
            await kube_jobs.delete_job(ns, name)  # its Pod, ConfigMap and Secret go with it
        except KubeError as e:
            logger.warning("Job %s not deleted (%s); its TTL will", name, e)


async def run_script(session: Session, user: User, flow: Flow, definition: dict, node: dict, *,
                     parent_run_id: Optional[int], trigger_type: str, engine_mode: str,
                     script_outputs: dict[str, tuple[str, str]], resolve_ds, default_bucket: str,
                     launch, wait, trial_run: Optional[Run] = None) -> Optional[tuple[str, str]]:
    """Runs the node; returns where its output is in Tabularia's storage (bucket, key),
    None when it has none. `launch` and `wait` are the orchestrator's way of running
    a chain on the engine (routes/runs._launch_flow_run, orchestrator._wait_run).

    `trial_run`: the editor's «Run» (run_trial below): the run is that row, the output
    is asked for even without a node after it (it is what the editor previews), and a
    script that writes none is not a failure."""
    from app.schemas.models import RunCreate

    reason = script_builds.disabled_reason()
    if reason:
        raise ScriptFailed("scripting environments are off on this installation"
                           if reason == "kubernetes" else "scripting environments are not configured")
    label = script_nodes.label(node)
    # a node by reference runs the saved script's current version, with its declared inputs
    by_reference = script_nodes.saved_script_id(node) is not None
    fingerprint = script_nodes.fingerprint(session, node)
    node, saved = script_nodes.resolved(session, node)
    if by_reference:
        from app.services import saved_scripts

        if saved is None:
            raise ScriptFailed(f"script «{label}» runs a saved script that no longer exists")
        if not saved_scripts.can_see(session, user, saved):
            raise ScriptFailed(f"{user.email} cannot see the saved script that «{label}» runs")
    d = node.get("data") or {}
    env = session.get(ScriptEnvironment, d.get("environmentId")) if isinstance(d.get("environmentId"), int) else None
    if env is None:
        raise ScriptFailed(f"script «{label}» has no scripting environment")
    if not env.enabled:
        raise ScriptFailed(f"the scripting environment «{env.name}» is disabled")
    version = session.get(ScriptEnvironmentVersion, env.current_version_id) if env.current_version_id else None
    if version is None or version.status != "ready" or not version.image_ref:
        raise ScriptFailed(f"the scripting environment «{env.name}» has no version built yet")
    if not script_nodes.can_use(session, user, env):
        raise ScriptFailed(f"{user.email} cannot run scripts in the environment «{env.name}»")
    storage = session.get(ScriptStorage, 1)
    if storage is None or not (storage.bucket and storage.access_key and storage.secret_key_enc):
        raise ScriptFailed("the scripts bucket is not configured (Administration → Scripting environments)")
    try:
        script_nodes.check_files(node, env.language, storage.bucket, complete=True)
        script_nodes.check_settings(node, env)
    except Exception as e:  # HTTPException from the checks: its detail is the message
        raise ScriptFailed(getattr(e, "detail", str(e))) from None
    declared: list = []
    if saved is not None:
        from app.services import saved_script_runs, saved_scripts

        declared = saved_script_runs.check_inputs(session, user, saved_scripts.content_of(session, saved))

    ns = namespace_for(trial_run is not None)
    has_input = script_input_edge(definition, node["id"]) is not None
    wants_output = script_has_output(definition, node["id"])
    has_output = wants_output or trial_run is not None
    step_id = str(uuid.uuid4())
    prefix = f"env-{env.id}/{step_id}"
    in_key, out_key = f"{prefix}/input.parquet", f"{prefix}/output.parquet"
    engine_out_key = f"out/{uuid.uuid4().hex}.parquet" if has_output else ""
    plain, secret_vars, snapshot = _variables(session, env)

    info = {
        "node_id": node["id"], "label": label, "environment_id": env.id, "environment": env.name,
        "language": env.language, "version": version.number, "image": version.image_ref, "step_id": step_id,
        "input": has_input, "output": has_output, "entrypoint": d.get("entrypoint"),
        "files": [f["path"] for f in script_nodes.files_of(node)], "attempts": 0, "exit_code": None,
        "variables": snapshot, "log": "", "trial": trial_run is not None,
        "fingerprint": fingerprint,
        # the longest it can take, every attempt and wait included: past it, a run
        # still open was left behind by a process that died (close_if_orphaned)
        "budget_seconds": budget_seconds(d),
        "namespace": ns,
    }
    if saved is not None:
        info.update(saved_script_id=saved.id, script=saved.name, script_version=saved.current_version,
                    inputs=[{"alias": a, "datasource_id": ds.id, "name": ds.name} for a, ds in declared])
    child = trial_run or Run(kind="script", flow_id=flow.id, task_id="", launched_by=user.id,
                             trigger_type=trigger_type, parent_run_id=parent_run_id, input_key="")
    child.status, child.engine, child.script = "STARTED", f"{env.name} v{version.number}", json.dumps(info)
    child.output_bucket, child.output_key = (default_bucket if has_output else ""), engine_out_key
    session.add(child)
    session.commit()
    session.refresh(child)

    def save(**values) -> None:
        if values.get("status") in TERMINAL_STATES and not run_still_open(session, child.id):
            return  # closed meanwhile (an orphan's reconcile, an interruption): its outcome stays
        for k, v in values.items():
            setattr(child, k, v)
        child.script = json.dumps(info)
        session.add(child)
        session.commit()
        if values.get("status") in TERMINAL_STATES and not info.get("trial"):
            from app.services import openlineage

            openlineage.run_closed(session, child)  # after the commit, as every run's closing

    bucket_url = f"s3://{storage.bucket}"
    written = [in_key, out_key]
    try:
        input_env: dict[str, str] = {}
        if saved is not None:
            from app.services import saved_script_runs

            input_env, described, keys = await saved_script_runs.prepare_inputs(session, declared, storage, prefix)
            written += keys
            input_env.update(TABULARIA_INPUTS=json.dumps(described, ensure_ascii=False),
                             TABULARIA_SCRIPT_ID=str(saved.id), TABULARIA_SCRIPT_NAME=saved.name,
                             TABULARIA_SCRIPT_VERSION=str(saved.current_version))
        if has_input:
            req = resolve_script_input(definition, node, resolve_ds, default_bucket, engine_mode, script_outputs)
            target = {"type": "s3", "connection": storage_payload(storage),
                      "target": {"bucket": storage.bucket, "key": in_key, "format": "parquet", "partition_by": []}}
            summary = json.dumps({"type": "script_input", "script_run_id": child.id, "bucket": storage.bucket,
                                  "key": in_key})
            input_run = await launch(session, user, flow, RunCreate(**req), trigger_type=trigger_type,
                                     parent_run_id=child.id if trial_run else parent_run_id, engine_mode=engine_mode,
                                     script_destination=(target, summary))
            input_run = await wait(session, input_run)
            if input_run.status != "SUCCESS":
                raise ScriptFailed(f"the input of script «{label}» could not be prepared "
                                   f"(run {input_run.id}): {input_run.error or input_run.status}")
            save(input_key=input_run.output_key, rows_written=None)

        s = get_settings().scripts
        timeout = int(d.get("timeoutSeconds", 900))
        retries = int(d.get("retries", 0))
        delay = int(d.get("retryDelaySeconds", 60))
        code, items = _code_items(node)
        base_env = {
            "TABULARIA_INPUT": f"{bucket_url}/{in_key}" if has_input else "",
            "TABULARIA_OUTPUT": f"{bucket_url}/{out_key}" if has_output else "",
            "TABULARIA_STEP_ID": step_id, "TABULARIA_RUN_ID": str(parent_run_id or child.id),
            "TABULARIA_FLOW_ID": str(flow.id), "TABULARIA_FLOW_NAME": flow.name, "TABULARIA_NODE_ID": str(node["id"]),
            **input_env, "HOME": "/tmp", "TMPDIR": "/tmp", **_storage_env(storage), **plain,
        }
        secret = {"AWS_ACCESS_KEY_ID": storage.access_key,
                  "AWS_SECRET_ACCESS_KEY": decrypt_secret(storage.secret_key_enc), **secret_vars}
        labels = {"app.kubernetes.io/managed-by": "tabularia", "app.kubernetes.io/component": "scripting",
                  script_builds.LABEL_ENV: str(env.id), LABEL_SCRIPT_RUN: str(child.id),
                  LABEL_ORCHESTRATION: str(parent_run_id or child.id), "tabularia.io/stage": "run"}
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
                logger.info("script: run %s attempt %s failed (%s), again in %ss", child.id, attempt, reason, delay)
                await asyncio.sleep(delay)
                if not run_still_open(session, parent_run_id or child.id):
                    raise ScriptFailed(f"script «{label}» failed: {reason} (not run again: its run was closed)")
                if has_output:  # what the failed attempt wrote is not the next one's output
                    await _delete_objects(storage, [out_key])
        if not ok:
            raise ScriptFailed(f"script «{label}» failed: {reason}")

        if has_output:
            resp = await get_engine_client().post("/scripts/output/import", json={
                "connection": storage_payload(storage), "key": out_key, "bucket": default_bucket,
                "output_key": engine_out_key})
            if resp.status_code >= 400:
                raise ScriptFailed(f"script «{label}»: its output could not be read: {_detail(resp)}")
            out = resp.json()
            if not out.get("found") and not wants_output:
                # a trial of a script that writes nothing: nothing to preview, and that is fine
                info["output"] = False
                save(output_key="", output_bucket="", status="SUCCESS", finished_at=_now())
                return None
            if not out.get("found"):
                raise ScriptFailed(f"script «{label}» did not write its output: the nodes after it read the "
                                   "parquet file the script writes at TABULARIA_OUTPUT")
            save(rows_written=out.get("rows"))
        save(status="SUCCESS", finished_at=_now())
        return (default_bucket, engine_out_key) if has_output else None
    except ScriptFailed as e:
        save(status="FAILURE", error=str(e)[:2000], finished_at=_now())
        raise
    except Exception as e:
        message = getattr(e, "detail", None) or str(e) or type(e).__name__
        save(status="FAILURE", error=f"script «{label}»: {message}"[:2000], finished_at=_now())
        raise ScriptFailed(f"script «{label}»: {message}") from e
    finally:
        try:
            await get_engine_client().post("/scripts/objects/delete", json={
                "connection": storage_payload(storage), "keys": written})
        except Exception as e:  # noqa: BLE001 — the bucket's lifecycle rule removes them
            logger.warning("script: step %s not cleaned up (%s)", step_id, type(e).__name__)


async def _delete_objects(storage, keys: list[str]) -> None:
    """Removes these keys from the scripts bucket, as best it can."""
    try:
        await get_engine_client().post("/scripts/objects/delete", json={
            "connection": storage_payload(storage), "keys": keys})
    except Exception as e:  # noqa: BLE001 — the bucket's lifecycle rule removes them
        logger.warning("script: %s not removed from the scripts bucket (%s)", keys, type(e).__name__)


def budget_seconds(d: dict) -> int:
    timeout, retries, delay = int(d.get("timeoutSeconds", 900)), int(d.get("retries", 0)), int(d.get("retryDelaySeconds", 60))
    return (timeout + GRACE_SECONDS) * (retries + 1) + delay * retries + 900  # 900: preparing the input


def _detail(resp) -> str:
    try:
        return str(resp.json().get("detail") or resp.text)[:500]
    except ValueError:
        return resp.text[:500]


async def close_if_orphaned(session: Session, run: Run) -> Run:
    """A Script or Container run still open whose process died: its orchestration has ended, or —
    an editor's trial, which has none — it is older than it could ever last. Its Jobs
    are stopped and it is closed saying so. Called by the run page's reconcile."""
    if run.status in TERMINAL_STATES:
        return run
    if run.parent_run_id:
        parent = session.get(Run, run.parent_run_id)
        if parent is None or parent.status not in TERMINAL_STATES:
            return run
        reason = f"the flow's run ended before the {run.kind} did (its orchestrator stopped)"
    else:
        try:
            budget = int(json.loads(run.script or "{}").get("budget_seconds") or 0)
        except ValueError:
            budget = 0
        started = run.started_at.replace(tzinfo=timezone.utc) if run.started_at.tzinfo is None else run.started_at
        age = (_now() - started).total_seconds()
        beat = run.heartbeat_at.replace(tzinfo=timezone.utc) if run.heartbeat_at and run.heartbeat_at.tzinfo is None \
            else run.heartbeat_at
        if beat is not None:
            # a trial that beats is alive, however long its input waits in the workers' queue
            if (_now() - beat).total_seconds() < TRIAL_DEAD_SECONDS:
                return run
            reason = "the trial stopped: the process running it is gone (a gateway restarted or removed)"
        elif run.status == "PENDING" and age >= TRIAL_START_SECONDS:
            # its process took it and never got to say it started: it is gone
            reason = "the trial never started: the process that took it stopped"
        elif budget and age >= budget:
            reason = "the trial outlived its time: the process running it stopped"
        else:
            return run
    try:
        ns = json.loads(run.script or "{}").get("namespace")
    except ValueError:
        ns = None
    if run.kind == "container":
        from app.services import container_runs

        on, ns = get_settings().containers.enabled, ns or container_runs.namespace_for(not run.parent_run_id)
        selector = f"{container_runs.LABEL_CONTAINER_RUN}={run.id}"
    else:
        on, ns = not script_builds.disabled_reason(), ns or namespace_for(not run.parent_run_id)
        selector = f"{LABEL_SCRIPT_RUN}={run.id}"
    if on:
        try:
            await kube_jobs.delete_jobs(ns, selector)
        except KubeError as e:
            logger.warning("%s: Jobs of run %s not deleted (%s)", run.kind, run.id, e)
    run.status, run.finished_at = "FAILURE", _now()
    run.error = reason
    session.add(run)
    session.commit()
    return run


STEP_KINDS = ("script", "container")


def _runs_of_node(flow_id: int, node_id: str):
    """The runs of one node of a flow, newest first. A node's run always writes its
    `script` with `node_id` first (script_runs, container_runs, the trial's row), so the
    column's beginning finds them without reading the logs of every other run."""
    prefix = json.dumps({"node_id": node_id})[:-1] + ","
    return (select(Run).where(Run.flow_id == flow_id, Run.kind.in_(STEP_KINDS),  # type: ignore[attr-defined]
                              Run.script.startswith(prefix, autoescape=True))  # type: ignore[union-attr]
            .order_by(Run.started_at.desc(), Run.id.desc()))


def latest_outputs(session: Session, flow_id: int, node_ids: Optional[list[str]] = None) -> dict[str, dict]:
    """Per Script or Container node of the flow (those of its saved definition, or
    `node_ids`): its latest run (any outcome) and the output of its latest successful
    one, which the editor previews the nodes after it from. Two rows per node at most."""
    if node_ids is None:
        from app.services import container_nodes

        flow = session.get(Flow, flow_id)
        definition = flow.definition if flow else None
        node_ids = [n.get("id") for n in [*script_nodes.script_nodes(definition),
                                          *container_nodes.container_nodes(definition)] if n.get("id")]
    out: dict[str, dict] = {}
    for node_id in dict.fromkeys(node_ids):
        last = session.exec(_runs_of_node(flow_id, node_id).limit(1)).first()
        if last is None:
            continue
        ok = last if last.status == "SUCCESS" and last.output_key else session.exec(
            _runs_of_node(flow_id, node_id).where(Run.status == "SUCCESS", Run.output_key != "").limit(1)).first()
        _add_to_latest(out, node_id, last)
        if ok is not None and ok is not last:
            _add_to_latest(out, node_id, ok)
    return out


def _add_to_latest(out: dict[str, dict], node_id: str, run: Run) -> None:
    try:
        info = json.loads(run.script or "{}")
    except ValueError:
        return
    entry = out.setdefault(node_id, {"last": None, "output": None})
    if entry["last"] is None:
        entry["last"] = {"run_id": run.id, "status": run.status, "trial": bool(info.get("trial")),
                         "error": run.error, "started_at": run.started_at, "finished_at": run.finished_at,
                         "attempts": info.get("attempts"), "exit_code": info.get("exit_code"),
                         "log": info.get("log") or "", "log_expired": bool(info.get("log_expired")),
                         "fingerprint": info.get("fingerprint")}
    if entry["output"] is None and run.status == "SUCCESS" and run.output_key:
        entry["output"] = {"run_id": run.id, "bucket": run.output_bucket, "key": run.output_key,
                           "rows": run.rows_written, "trial": bool(info.get("trial")),
                           "finished_at": run.finished_at, "fingerprint": info.get("fingerprint")}


async def run_trial(run_id: int, user_id: int, node_id: str) -> None:
    """The editor's «Run» of a Script node (or a Container node, the run's kind): the flow as saved, the chain before the
    node on the sources' development sample, the nodes after it not run. A task of
    the gateway process that answered; its own database session. It beats while it runs
    (Run.heartbeat_at): close_if_orphaned tells a live trial from one whose process died."""
    beating = asyncio.create_task(_beat_trial(run_id))
    try:
        await _run_trial(run_id, user_id, node_id)
    finally:
        beating.cancel()


async def _beat_trial(run_id: int) -> None:
    """Says the trial is alive while its process runs it (close_if_orphaned reads it)."""
    from app.db.session import engine

    while True:
        try:
            with Session(engine) as s:
                s.exec(update(Run).where(Run.id == run_id, Run.status.not_in(TERMINAL_STATES))  # type: ignore[union-attr]
                       .values(heartbeat_at=_now()))
                s.commit()
        except Exception as e:  # noqa: BLE001 — the database away: the next beat
            logger.debug("trial %s: beat not written (%s)", run_id, e)
        await asyncio.sleep(TRIAL_BEAT_SECONDS)


async def _run_trial(run_id: int, user_id: int, node_id: str) -> None:
    from app.db.session import engine
    from app.routes.runs import _launch_flow_run
    from app.services.orchestrator import _wait_run

    with Session(engine) as session:
        run = session.get(Run, run_id)
        user = session.get(User, user_id)
        flow = session.get(Flow, run.flow_id) if run else None
        if run is None or user is None or flow is None:
            return
        definition = json.loads(flow.definition or "{}")
        node = next((n for n in definition.get("nodes") or [] if n.get("id") == node_id), None)

        def resolve_ds(i: int):
            from app.models import Datasource

            d = session.get(Datasource, i)
            return (d.bucket, d.key) if d and d.key else None

        try:
            if node is None or node.get("type") != run.kind:
                raise ScriptFailed("the node is no longer in the saved flow")
            if run.kind == "container":
                from app.services.container_runs import run_container as runner
            else:
                runner = run_script
            # the Script and Container nodes before this one: their latest output stands in for them
            upstream = {nid: (o["output"]["bucket"], o["output"]["key"])
                        for nid, o in latest_outputs(session, flow.id).items() if o["output"]}
            await runner(session, user, flow, definition, node, parent_run_id=None, trigger_type="trial",
                         engine_mode="development", script_outputs=upstream, resolve_ds=resolve_ds,
                         default_bucket=get_settings().engine.bucket, launch=_launch_flow_run, wait=_wait_run,
                         trial_run=run)
        except Exception as e:  # noqa: BLE001 — whatever it was, the run says it and ends
            session.rollback()
            run = session.get(Run, run_id)
            if run is not None and run.status not in TERMINAL_STATES:
                run.status, run.finished_at = "FAILURE", _now()
                run.error = (str(getattr(e, "detail", None) or e) or type(e).__name__)[:2000]
                session.add(run)
                session.commit()
            logger.info("trial %s ended with: %s", run_id, e)
        except asyncio.CancelledError:
            # the process is stopping (a rollout, a scale-down): the trial says so instead
            # of staying open; its Job goes with the runner's own clean-up
            session.rollback()
            run = session.get(Run, run_id)
            if run is not None and run.status not in TERMINAL_STATES:
                run.status, run.finished_at = "FAILURE", _now()
                run.error = "the gateway running the trial stopped: run it again"
                session.add(run)
                session.commit()
            raise
