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
import re
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
        raise KubeError(0, "No service account token: is scripts.enabled or containers.enabled set in the Helm chart?")
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


async def put_secret(namespace: str, manifest: dict) -> None:
    """Creates the Secret, or replaces the one of that name: a registry's pull Secret
    follows its credentials. Two runs of one registry at once (parallel cells of a flow
    group) write it together: one that finds it already as wanted leaves it, one whose
    replacement lost the race to the other's reads it again and retries."""
    path = f"/api/v1/namespaces/{namespace}/secrets"
    name = manifest["metadata"]["name"]
    try:
        await _call("POST", path, manifest)
        return
    except KubeError as e:
        if e.status != 409:
            raise
    for attempt in range(3):
        current = await _call("GET", f"{path}/{name}")
        if current.get("data") == manifest.get("data") and current.get("type") == manifest.get("type"):
            return
        meta = {**manifest["metadata"], "resourceVersion": (current.get("metadata") or {}).get("resourceVersion")}
        try:
            await _call("PUT", f"{path}/{name}", {**manifest, "metadata": meta})
            return
        except KubeError as e:
            if e.status != 409 or attempt == 2:
                raise


async def delete_secret(namespace: str, name: str) -> None:
    """Deletes the Secret; one already gone is fine."""
    try:
        await _call("DELETE", f"/api/v1/namespaces/{namespace}/secrets/{name}")
    except KubeError as e:
        if e.status != 404:
            raise


async def job_pods(namespace: str, name: str) -> list[dict]:
    pods = await _call("GET", f"/api/v1/namespaces/{namespace}/pods?labelSelector={quote('job-name=' + name)}")
    return list(pods.get("items") or [])


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


async def refused(namespace: str, name: str) -> str:
    """Why the cluster does not create the Job's Pod (a full ResourceQuota, a Pod the
    namespace's Pod Security or LimitRange refuses), "" when it is not refusing."""
    try:
        events = await job_events(namespace, name)
    except KubeError:
        return ""
    for e in reversed(events):
        if e["reason"] == "FailedCreate":
            if "exceeded quota" in e["message"]:
                return f"the namespace {namespace} has no room left (its ResourceQuota is full): {e['message'][:300]}"
            return f"the cluster refused to create its Pod: {e['message'][:300]}"
    return ""


def has_pod(job: dict) -> bool:
    """The Job has, or had, a Pod."""
    status = job.get("status") or {}
    return any(status.get(k) for k in ("active", "ready", "succeeded", "failed"))


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


async def job_result(namespace: str, name: str, max_bytes: int) -> tuple[str, Optional[int], str]:
    """The end of the log of the Job's last Pod, its exit code (None when the container
    never ended, or there is no Pod) and the runtime's reason for the end (OOMKilled,
    Error, Completed; "" when none)."""
    pods = await _call("GET", f"/api/v1/namespaces/{namespace}/pods?labelSelector={quote('job-name=' + name)}")
    items = sorted(pods.get("items") or [], key=lambda p: (p.get("metadata") or {}).get("creationTimestamp") or "")
    if not items:
        return "", None, ""
    pod = items[-1]
    exit_code, reason = None, ""
    for st in (pod.get("status") or {}).get("containerStatuses") or []:
        terminated = (st.get("state") or {}).get("terminated") or {}
        if "exitCode" in terminated:
            exit_code, reason = terminated["exitCode"], terminated.get("reason") or ""
    stopped = stopped_by_cluster(pod)
    if stopped:
        reason = stopped
    try:
        log = await _call("GET", f"/api/v1/namespaces/{namespace}/pods/{pod['metadata']['name']}/log?tailLines=5000",
                          text=True)
    except KubeError as e:
        if e.status in (400, 404):  # the container never started
            return pod_waiting_reason(pod), exit_code, reason
        raise LogUnavailable(e.status, str(e), exit_code, reason) from e
    return tail(log, max_bytes), exit_code, reason


class LogUnavailable(KubeError):
    """The Pod's log could not be read (its node lost, the API failing): what its status
    said of the end comes with it."""

    def __init__(self, status: int, message: str, exit_code: Optional[int], reason: str):
        super().__init__(status, message)
        self.exit_code, self.reason = exit_code, reason


def stopped_by_cluster(pod: dict) -> str:
    """"Evicted" or "DisruptionTarget" when the cluster took the Pod away (a node drained or
    under pressure, a preemption, the autoscaler's consolidation), "" otherwise."""
    status = pod.get("status") or {}
    if status.get("reason") == "Evicted":
        return "Evicted"
    if any(c.get("type") == "DisruptionTarget" and c.get("status") == "True" for c in status.get("conditions") or []):
        return "DisruptionTarget"
    return ""


# when the cluster took a Pod away before its container ended
STOPPED_BY_CLUSTER = ("the cluster stopped its Pod before the end (a node drained or short of resources, "
                      "a preemption): run it again, or give the node Retries")


def explain_exit(exit_code: Optional[int], reason: str, memory_limit: str = "") -> str:
    """Why a container ended badly, for the person who wrote it. 137 is a SIGKILL: the
    runtime says OOMKilled when the memory limit did it, but not every one does (kind's
    nodes say Error), and the limit is still by far the likeliest cause. 143 is the
    SIGTERM of a Pod the cluster stops (a drained node)."""
    limit = f" ({memory_limit})" if memory_limit else ""
    if reason in ("Evicted", "DisruptionTarget"):
        return STOPPED_BY_CLUSTER
    if reason == "OOMKilled":
        return f"it ran out of memory: it went over its memory limit{limit} and the cluster stopped it (exit code {exit_code})"
    if exit_code == 137:
        return f"it was killed (exit code 137): most often because it went over its memory limit{limit}"
    if exit_code == 143:
        return "it was stopped (SIGTERM, exit code 143): most often the cluster stopped its Pod (a node drained)"
    return f"it ended with exit code {exit_code}"


# a container whose image its node could not download
_PULL_FAILURES = ("ErrImagePull", "ImagePullBackOff", "InvalidImageName", "ErrImageNeverPull")
# what the runtime said, in the words of whoever must fix it
_PULL_REASONS = (
    (re.compile(r"x509|certificate", re.I),
     "the nodes do not trust the registry's certificate: its authority must be installed on the nodes"),
    (re.compile(r"HTTP response to HTTPS client", re.I),
     "the registry answers in plain HTTP and the nodes expect HTTPS: the nodes must allow that registry over HTTP"),
    (re.compile(r"\b(401|403)\b|unauthorized|denied|no basic auth|authorization failed|authentication required", re.I),
     "the registry refused the cluster's credentials: the pull Secret is missing, wrong or expired"),
    (re.compile(r"no match for platform|no matching manifest", re.I),
     "the image has no version for the nodes' architecture (built for arm64 only, or amd64 only)"),
    (re.compile(r"manifest unknown|not found", re.I),
     "the image is not in the registry (any more)"),
    (re.compile(r"no such host|i/o timeout|connection refused|network is unreachable|dial tcp", re.I),
     "the nodes cannot reach the registry"),
)


# an image's digest: its hex digits hold 401 or 403 one time in thirty
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}", re.I)


def explain_pull(detail: str) -> str:
    said = _DIGEST.sub("sha256:…", detail)
    for pattern, why in _PULL_REASONS:
        if pattern.search(said):
            return f"the cluster could not download the image: {why} ({detail.strip()[:300]})"
    return f"the cluster could not download the image: {detail.strip()[:300] or 'no reason given'}"


async def pull_failure(namespace: str, name: str) -> str:
    """Why the node cannot download the image of the Job's Pod, "" when it is not
    failing to. While the node only retries, the Pod says nothing more than
    "Back-off pulling image": the reason is in the Pod's events."""
    return (await start_state(namespace, name))[0]


# a container its node has the image of but cannot create (no command in the image, a
# Secret or ConfigMap missing): the kubelet retries, saying the same
_CREATE_FAILURES = ("CreateContainerError", "CreateContainerConfigError")


async def start_state(namespace: str, name: str) -> tuple[str, str, bool]:
    """How the Job's Pod is starting: (why it cannot — its image not downloaded, its
    container not created —, why it is not scheduled yet — not enough CPU or memory
    free, a node selector, a taint: the autoscaler may still add a node —, whether its
    container has run)."""
    pods = await _call("GET", f"/api/v1/namespaces/{namespace}/pods?labelSelector={quote('job-name=' + name)}")
    unscheduled, ran = "", False
    for pod in pods.get("items") or []:
        for c in (pod.get("status") or {}).get("conditions") or []:
            if c.get("type") == "PodScheduled" and c.get("status") == "False":
                unscheduled = " — ".join(x for x in (c.get("reason"), c.get("message")) if x)
        for st in (pod.get("status") or {}).get("containerStatuses") or []:
            state = st.get("state") or {}
            ran = ran or bool(state.get("running") or state.get("terminated"))
            waiting = state.get("waiting") or {}
            if waiting.get("reason") in _CREATE_FAILURES:
                return (f"the cluster could not start its container: "
                        f"{waiting.get('message') or waiting['reason']}"[:400], unscheduled, ran)
            if waiting.get("reason") not in _PULL_FAILURES:
                continue
            detail = waiting.get("message") or ""
            if waiting.get("reason") == "ImagePullBackOff" or not detail:
                events = await job_events(namespace, pod["metadata"]["name"])
                said = [e["message"] for e in events if e["reason"] in ("Failed", "FailedToRetrieveImagePullSecret")
                        and "Back-off" not in e["message"] and e["message"] != "Error: ImagePullBackOff"]
                detail = " · ".join(dict.fromkeys(said)) or detail
            return explain_pull(detail), unscheduled, ran
    return "", unscheduled, ran


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
