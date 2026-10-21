"""Kubernetes Jobs for the scripting environments: create one, follow it, read its
log, delete it. Used by services/script_builds.py (the image builds and checks) and
later by the Script node.

Like services/kube_scaling.py, no client library: a few JSON requests over httpx
with the pod's service account, which the Helm chart lets manage Jobs (and read
their Pods and logs) in the scripts namespaces only (`scripts.enabled`). The address
of the API and the token are the ones of KubernetesSettings.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional
from urllib.parse import quote

import httpx

from app.core.config import get_settings
from app.services.kube_scaling import KubeError, _ssl

# for the tests: a transport standing in for the cluster
_transport: Optional[httpx.AsyncBaseTransport] = None


async def _call(method: str, path: str, body: Optional[dict] = None, *, text: bool = False):
    k = get_settings().kubernetes
    try:
        # read at every call: the kubelet rotates the service account's token
        token = Path(k.token_file).read_text().strip()
    except OSError:
        token = ""
    if not token:
        raise KubeError(0, "No service account token: is scripts.enabled set in the Helm chart?")
    # a Pod's log is plain text, but the API server refuses `Accept: text/plain` (406)
    headers = {"Authorization": f"Bearer {token}", "Accept": "*/*" if text else "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    try:
        async with httpx.AsyncClient(base_url=k.api_url, verify=_ssl(k.ca_file), timeout=k.timeout_seconds,
                                     headers=headers, transport=_transport) as c:
            r = await c.request(method, path, content=json.dumps(body) if body is not None else None)
    except httpx.HTTPError as e:
        raise KubeError(0, f"The Kubernetes API is not reachable ({type(e).__name__})")
    if r.status_code >= 400:
        try:
            reason = r.json().get("message") or r.text
        except ValueError:
            reason = r.text
        raise KubeError(r.status_code, f"Kubernetes answered {r.status_code}: {str(reason)[:300]}")
    return r.text if text else r.json()


def _jobs(namespace: str) -> str:
    return f"/apis/batch/v1/namespaces/{namespace}/jobs"


async def create_job(namespace: str, manifest: dict) -> dict:
    """Creates the Job and returns it as the cluster has it (its uid for the objects
    it owns); one with the same name already there counts as created (a process
    that died between creating it and recording it)."""
    try:
        return await _call("POST", _jobs(namespace), manifest)
    except KubeError as e:
        if e.status != 409:
            raise
    return await _call("GET", f"{_jobs(namespace)}/{manifest['metadata']['name']}")


def owned_by(job: dict) -> list[dict]:
    """ownerReferences that make an object go away with the Job."""
    meta = job.get("metadata") or {}
    return [{"apiVersion": "batch/v1", "kind": "Job", "name": meta.get("name"), "uid": meta.get("uid")}]


async def create_object(namespace: str, kind: str, manifest: dict) -> None:
    """A ConfigMap or a Secret (kind: "configmaps" | "secrets") in the namespace."""
    try:
        await _call("POST", f"/api/v1/namespaces/{namespace}/{kind}", manifest)
    except KubeError as e:
        if e.status != 409:
            raise


async def get_job(namespace: str, name: str) -> Optional[dict]:
    try:
        return await _call("GET", f"{_jobs(namespace)}/{name}")
    except KubeError as e:
        if e.status == 404:
            return None
        raise


async def delete_job(namespace: str, name: str) -> None:
    """Deletes the Job and its Pods; one already gone is fine."""
    try:
        await _call("DELETE", f"{_jobs(namespace)}/{name}?propagationPolicy=Background")
    except KubeError as e:
        if e.status != 404:
            raise


async def delete_jobs(namespace: str, selector: str) -> None:
    await _call("DELETE", f"{_jobs(namespace)}?labelSelector={quote(selector)}&propagationPolicy=Background")


async def job_events(namespace: str, name: str) -> list[dict]:
    """What the cluster said about the Job (FailedCreate: a quota, a refused Pod)."""
    out = await _call("GET", f"/api/v1/namespaces/{namespace}/events?fieldSelector="
                             f"{quote('involvedObject.name=' + name)}")
    return [{"reason": e.get("reason") or "", "message": e.get("message") or ""} for e in out.get("items") or []]


def job_outcome(job: dict) -> Optional[str]:
    """"succeeded", "failed" or None while it runs."""
    status = job.get("status") or {}
    for c in status.get("conditions") or []:
        if c.get("status") == "True" and c.get("type") in ("Complete", "SuccessCriteriaMet"):
            return "succeeded"
        if c.get("status") == "True" and c.get("type") in ("Failed", "FailureTarget"):
            return "failed"
    if status.get("succeeded"):
        return "succeeded"
    return None


def job_failure(job: dict) -> str:
    """Why the Job failed, in the cluster's words (a deadline, a back-off)."""
    for c in (job.get("status") or {}).get("conditions") or []:
        if c.get("type") in ("Failed", "FailureTarget") and c.get("status") == "True":
            return " — ".join(x for x in (c.get("reason"), c.get("message")) if x)
    return ""


async def job_log(namespace: str, name: str, max_bytes: int) -> str:
    """The end of the log of the Job's last Pod ("" when there is no Pod)."""
    return (await job_result(namespace, name, max_bytes))[0]


async def job_result(namespace: str, name: str, max_bytes: int) -> tuple[str, Optional[int]]:
    """The end of the log of the Job's last Pod and its exit code (None when the
    container never ended, or there is no Pod)."""
    pods = await _call("GET", f"/api/v1/namespaces/{namespace}/pods?labelSelector={quote('job-name=' + name)}")
    items = sorted(pods.get("items") or [], key=lambda p: (p.get("metadata") or {}).get("creationTimestamp") or "")
    if not items:
        return "", None
    pod = items[-1]
    exit_code = None
    for st in (pod.get("status") or {}).get("containerStatuses") or []:
        terminated = (st.get("state") or {}).get("terminated") or {}
        if "exitCode" in terminated:
            exit_code = terminated["exitCode"]
    try:
        log = await _call("GET", f"/api/v1/namespaces/{namespace}/pods/{pod['metadata']['name']}/log?tailLines=5000",
                          text=True)
    except KubeError as e:
        if e.status in (400, 404):  # the container never started
            return pod_waiting_reason(pod), exit_code
        raise
    return tail(log, max_bytes), exit_code


def pod_waiting_reason(pod: dict) -> str:
    for s in (pod.get("status") or {}).get("containerStatuses") or []:
        waiting = (s.get("state") or {}).get("waiting") or {}
        if waiting:
            return " — ".join(x for x in (waiting.get("reason"), waiting.get("message")) if x)
    return ""


def tail(text: str, max_bytes: int) -> str:
    data = text.encode()
    if len(data) <= max_bytes:
        return text
    return "…\n" + data[-max_bytes:].decode(errors="ignore")
