"""A Container node, run inside a flow's orchestration (services/orchestrator.py): an
image of a container registry, run as a Kubernetes Job. docs/container-node.md; what
a node holds is in services/container_nodes.py.

1. The image: the node's tag is resolved to its digest when the run starts, and the
   Pod names the digest, so every attempt runs the same image and the run records
   which. Whoever the run belongs to must still be allowed the registry and the image
   (services/registry_access.py).
2. The registry's pull Secret is written in the namespace (services/container_cluster.py),
   with its credentials as they are now.
3. Input, when a data edge enters the node, and output, when one leaves it: as for a
   Script node, through the scripts bucket (`container-<registry>/<step id>/…`),
   TABULARIA_INPUT and TABULARIA_OUTPUT, and the bucket's credentials as the usual
   AWS_* variables. A node with neither is given no credentials at all.
4. The Job, in the containers' namespace (the editor's trials: in their own,
   CONTAINERS__TRIAL_NAMESPACE, with a quota): the node's command and arguments, or
   the image's own; the registry's variables (secret ones through a Secret) and the
   node's. Pod Security `baseline`: root is allowed inside, privileges are not. A
   failed attempt is run again as a new Job, `retries` times.

The node is a run of its own (`kind="container"`, a child of the orchestration). A
failure stops the flow, as a failed Script node does.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlmodel import Session, select

from app.core.config import get_settings
from app.core.crypto import decrypt_secret
from app.core.engine_client import get_engine_client
from app.models import ContainerRegistry, ContainerRegistryVariable, Flow, Run, ScriptStorage, User
from app.services import container_cluster, container_nodes, kube_jobs, registry_access
from app.services import container_registry as cr
from app.services.flow_resolver import resolve_script_input, script_has_output, script_input_edge
from app.services.kube_scaling import KubeError
from app.services.script_runs import (
    GRACE_SECONDS, _attempt, _detail, _storage_env, storage_payload,
)

logger = logging.getLogger(__name__)

LABEL_CONTAINER_RUN = "tabularia.io/container-run"
LABEL_ORCHESTRATION = "tabularia.io/run"


class ContainerFailed(Exception):
    """The node failed; the message is the one the run page shows."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def namespace_for(trial: bool) -> str:
    s = get_settings().containers
    return (s.trial_namespace or s.namespace) if trial else s.namespace


def budget_seconds(d: dict) -> int:
    timeout, retries, delay = int(d.get("timeoutSeconds", 900)), int(d.get("retries", 0)), int(d.get("retryDelaySeconds", 60))
    return (timeout + GRACE_SECONDS) * (retries + 1) + delay * retries + 900  # 900: preparing the input


def storage_ready(storage: Optional[ScriptStorage]) -> bool:
    return bool(storage and storage.bucket and storage.access_key and storage.secret_key_enc)


def _variables(session: Session, registry: ContainerRegistry, node: dict) -> tuple[dict[str, str], dict[str, str], dict]:
    """(plain, secret, snapshot): the registry's variables, then the node's. A node may
    not redefine one of the registry's: those are the administrator's."""
    plain, secret, snapshot = {}, {}, {}
    rows = session.exec(select(ContainerRegistryVariable)
                        .where(ContainerRegistryVariable.registry_id == registry.id)).all()
    for v in rows:
        if v.secret:
            secret[v.name] = decrypt_secret(v.value_enc or "")
            snapshot[v.name] = {"secret": True, "changed_at": v.updated_at.isoformat() if v.updated_at else None}
        else:
            plain[v.name] = v.value
            snapshot[v.name] = {"value": v.value}
    for name, value in container_nodes.variables_of(node).items():
        if name in snapshot:
            raise ContainerFailed(f"container «{container_nodes.label(node)}»: the variable {name} is set by the "
                                  f"registry «{registry.name}»: give the node's another name")
        plain[name] = value
        snapshot[name] = {"value": value, "node": True}
    return plain, secret, snapshot


def job_manifest(*, name: str, image: str, node: dict, environment: dict[str, str], labels: dict, timeout: int,
                 secret_name: Optional[str], pull_secret: Optional[str]) -> dict:
    s = get_settings().containers
    d = node.get("data") or {}
    cpu, memory = container_nodes.resources(node)
    container: dict = {
        "name": "container", "image": image, "imagePullPolicy": "IfNotPresent",  # by digest: the same image
        "env": [{"name": k, "value": v} for k, v in environment.items()],
        "resources": {"requests": {"cpu": cpu, "memory": memory}, "limits": {"cpu": cpu, "memory": memory}},
        "securityContext": {"allowPrivilegeEscalation": False},
    }
    command, args = container_nodes.strings(d.get("command")), container_nodes.strings(d.get("args"))
    if command:
        container["command"] = command
    if args:
        container["args"] = args
    if secret_name:
        container["envFrom"] = [{"secretRef": {"name": secret_name}}]
    pod: dict = {
        "restartPolicy": "Never", "automountServiceAccountToken": False, "enableServiceLinks": False,
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [container],
    }
    if pull_secret:
        pod["imagePullSecrets"] = [{"name": pull_secret}]
    return {
        "apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "labels": labels},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": timeout, "ttlSecondsAfterFinished": s.job_ttl_seconds,
                 "template": {"metadata": {"labels": labels}, "spec": pod}},
    }


async def run_container(session: Session, user: User, flow: Optional[Flow], definition: dict, node: dict, *,
                        parent_run_id: Optional[int], trigger_type: str, engine_mode: str,
                        script_outputs: dict[str, tuple[str, str]], resolve_ds, default_bucket: str,
                        launch, wait, trial_run: Optional[Run] = None) -> Optional[tuple[str, str]]:
    """Runs the node; returns where its output is in Tabularia's storage (bucket, key),
    None when it has none. The arguments are script_runs.run_script's. Without `flow`,
    a container cell of a flow group: no input, no output, a run under the group's."""
    from app.routes.registries import client_of
    from app.schemas.models import RunCreate

    s = get_settings().containers
    if not s.enabled or not s.namespace:
        raise ContainerFailed("containers are off on this installation (containers.enabled in the Helm chart)")
    d = node.get("data") or {}
    label = container_nodes.label(node)
    try:
        container_nodes.check(node, complete=True)
    except HTTPException as e:
        raise ContainerFailed(str(e.detail)) from None
    registry = session.get(ContainerRegistry, d["registryId"])
    if registry is None:
        raise ContainerFailed(f"container «{label}»: its container registry no longer exists")
    if not registry.enabled:
        raise ContainerFailed(f"container «{label}»: the container registry «{registry.name}» is not in use")
    repository = cr.repository_name(registry.host, str(d["repository"]).strip())
    if not registry_access.repository_allowed(session, user, registry, repository):
        raise ContainerFailed(f"container «{label}»: {user.email} may not run {repository} of «{registry.name}»")
    reference = str(d.get("tag") or "").strip() or "latest"
    client = client_of(registry)
    try:
        digest = await cr.digest(client, repository, reference)
    except cr.RegistryError as e:
        raise ContainerFailed(f"container «{label}»: the image {repository}:{reference} could not be resolved: {e}") from None
    image = container_cluster.image_ref(registry, repository, digest)

    ns = namespace_for(trial_run is not None)
    storage = session.get(ScriptStorage, 1)
    has_input = script_input_edge(definition, node["id"]) is not None
    wants_output = script_has_output(definition, node["id"])
    if (has_input or wants_output) and not storage_ready(storage):
        raise ContainerFailed(f"container «{label}»: its input and output go through the scripts bucket, which is not "
                              "configured (Administration → Scripting environments)")
    # a trial asks for the output even with no node after it: the editor previews it
    has_output = wants_output or (trial_run is not None and storage_ready(storage))
    uses_bucket = has_input or has_output
    step_id = str(uuid.uuid4())
    prefix = f"container-{registry.id}/{step_id}"
    in_key, out_key = f"{prefix}/input.parquet", f"{prefix}/output.parquet"
    engine_out_key = f"out/{uuid.uuid4().hex}.parquet" if has_output else ""
    plain, secret_vars, snapshot = _variables(session, registry, node)
    cpu, memory = container_nodes.resources(node)

    info = {
        "node_id": node["id"], "label": label, "registry_id": registry.id, "registry": registry.name,
        "repository": repository, "reference": reference, "digest": digest, "image": image, "step_id": step_id,
        "input": has_input, "output": has_output, "command": container_nodes.strings(d.get("command")),
        "args": container_nodes.strings(d.get("args")), "cpu": cpu, "memory": memory, "attempts": 0,
        "exit_code": None, "variables": snapshot, "log": "", "trial": trial_run is not None,
        "fingerprint": container_nodes.fingerprint(node), "budget_seconds": budget_seconds(d), "namespace": ns,
    }
    child = trial_run or Run(kind="container", flow_id=flow.id if flow else None, task_id="", launched_by=user.id,
                             trigger_type=trigger_type, parent_run_id=parent_run_id, input_key="")
    child.status, child.engine, child.script = "STARTED", registry.name, json.dumps(info)
    child.output_bucket, child.output_key = (default_bucket if has_output else ""), engine_out_key
    session.add(child)
    session.commit()
    session.refresh(child)

    def save(**values) -> None:
        for k, v in values.items():
            setattr(child, k, v)
        child.script = json.dumps(info)
        session.add(child)
        session.commit()

    try:
        if has_input:
            req = resolve_script_input(definition, node, resolve_ds, default_bucket, engine_mode, script_outputs)
            target = {"type": "s3", "connection": storage_payload(storage),
                      "target": {"bucket": storage.bucket, "key": in_key, "format": "parquet", "partition_by": []}}
            summary = json.dumps({"type": "container_input", "container_run_id": child.id, "bucket": storage.bucket,
                                  "key": in_key})
            input_run = await launch(session, user, flow, RunCreate(**req), trigger_type=trigger_type,
                                     parent_run_id=child.id if trial_run else parent_run_id, engine_mode=engine_mode,
                                     script_destination=(target, summary))
            input_run = await wait(session, input_run)
            if input_run.status != "SUCCESS":
                raise ContainerFailed(f"the input of container «{label}» could not be prepared "
                                      f"(run {input_run.id}): {input_run.error or input_run.status}")
            save(input_key=input_run.output_key, rows_written=None)

        try:
            pull_secret = await container_cluster.sync_pull_secret(registry, client.password, ns)
        except KubeError as e:
            raise ContainerFailed(f"container «{label}»: the registry's pull Secret could not be written: {e}") from None
        timeout = int(d.get("timeoutSeconds", 900))
        retries = int(d.get("retries", 0))
        delay = int(d.get("retryDelaySeconds", 60))
        bucket_url = f"s3://{storage.bucket}" if uses_bucket else ""
        base_env = {
            "TABULARIA_INPUT": f"{bucket_url}/{in_key}" if has_input else "",
            "TABULARIA_OUTPUT": f"{bucket_url}/{out_key}" if has_output else "",
            "TABULARIA_STEP_ID": step_id, "TABULARIA_RUN_ID": str(parent_run_id or child.id),
            "TABULARIA_FLOW_ID": str(flow.id) if flow else "", "TABULARIA_FLOW_NAME": flow.name if flow else "",
            "TABULARIA_NODE_ID": str(node["id"]), **(_storage_env(storage) if uses_bucket else {}), **plain,
        }
        secret = {**({"AWS_ACCESS_KEY_ID": storage.access_key,
                      "AWS_SECRET_ACCESS_KEY": decrypt_secret(storage.secret_key_enc)} if uses_bucket else {}),
                  **secret_vars}
        labels = {"app.kubernetes.io/managed-by": "tabularia", "app.kubernetes.io/component": "container",
                  container_cluster.LABEL: str(registry.id), LABEL_CONTAINER_RUN: str(child.id),
                  LABEL_ORCHESTRATION: str(parent_run_id or child.id), "tabularia.io/stage": "run"}
        ok, reason, logs = False, "", []
        for attempt in range(1, retries + 2):
            name = f"tabularia-container-{child.id}-a{attempt}"
            manifest = job_manifest(name=name, image=image, node=node,
                                    environment={**base_env, "TABULARIA_ATTEMPT": str(attempt)}, labels=labels,
                                    timeout=timeout, secret_name=f"{name}-env" if secret else None,
                                    pull_secret=pull_secret)
            info["attempts"] = attempt
            save()
            ok, log, exit_code, reason = await _attempt(name=name, manifest=manifest, code=None, secret=secret,
                                                        timeout=timeout, ns=ns, on_tick=session.commit,
                                                        poll_seconds=s.poll_seconds, log_max_bytes=s.log_max_bytes)
            logs.append(f"── attempt {attempt} ──\n{log}" if retries else log)
            info["log"] = kube_jobs.tail("\n".join(logs), s.log_max_bytes)
            info["exit_code"] = exit_code
            save()
            if ok:
                break
            if attempt <= retries:
                logger.info("container: run %s attempt %s failed (%s), again in %ss", child.id, attempt, reason, delay)
                await asyncio.sleep(delay)
        if not ok:
            raise ContainerFailed(f"container «{label}» failed: {reason}")

        if has_output:
            resp = await get_engine_client().post("/scripts/output/import", json={
                "connection": storage_payload(storage), "key": out_key, "bucket": default_bucket,
                "output_key": engine_out_key})
            if resp.status_code >= 400:
                raise ContainerFailed(f"container «{label}»: its output could not be read: {_detail(resp)}")
            out = resp.json()
            if not out.get("found") and not wants_output:
                # a trial of an image that writes nothing: nothing to preview, and that is fine
                info["output"] = False
                save(output_key="", output_bucket="", status="SUCCESS", finished_at=_now())
                return None
            if not out.get("found"):
                raise ContainerFailed(f"container «{label}» did not write its output: the nodes after it read the "
                                      "parquet file the container writes at TABULARIA_OUTPUT")
            save(rows_written=out.get("rows"))
        save(status="SUCCESS", finished_at=_now())
        return (default_bucket, engine_out_key) if has_output else None
    except ContainerFailed as e:
        save(status="FAILURE", error=str(e)[:2000], finished_at=_now())
        raise
    except Exception as e:
        message = getattr(e, "detail", None) or str(e) or type(e).__name__
        save(status="FAILURE", error=f"container «{label}»: {message}"[:2000], finished_at=_now())
        raise ContainerFailed(f"container «{label}»: {message}") from e
    finally:
        if uses_bucket:
            try:
                await get_engine_client().post("/scripts/objects/delete", json={
                    "connection": storage_payload(storage), "keys": [in_key, out_key]})
            except Exception as e:  # noqa: BLE001 — the bucket's lifecycle rule removes them
                logger.warning("container: step %s not cleaned up (%s)", step_id, type(e).__name__)
