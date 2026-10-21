"""The cluster's side of the container registries (docs/container-registries.md): what
the nodes need to download an image, and a way to check that they can.

- The pull Secret of a registry (`tabularia-registry-<id>`, type dockerconfigjson) is
  written in the containers' namespaces right before a Job needs it, from the
  registry's credentials as they are then: a changed or removed password never
  lingers. A registry without credentials has none; a deleted registry's goes.
- «Try in the cluster» runs a Pod with the image, by digest, and a command that does
  not exist: the node downloads the image, then fails to start it, so nothing of the
  image runs. The download is what is checked; a failure is explained the way a run's
  is (kube_jobs.pull_failure): credentials refused, a certificate the nodes do not
  trust, plain HTTP, an image no longer there.
"""
from __future__ import annotations

import asyncio
import base64
import json
import uuid
from typing import Optional

from app.core.config import get_settings
from app.models import ContainerRegistry
from app.services import container_registry as cr
from app.services import kube_jobs
from app.services.kube_scaling import KubeError

# what the check runs instead of the image's own command: it does not exist anywhere
MISSING_COMMAND = "/tabularia-pull-check-does-not-exist"
# how long the node may keep failing to download before the check says so
PULL_FAILURE_SECONDS = 20
LABEL = "tabularia.io/registry"


def namespaces() -> list[str]:
    s = get_settings().containers
    return list(dict.fromkeys(x for x in (s.namespace, s.trial_namespace) if x))


def trial_namespace() -> str:
    s = get_settings().containers
    return s.trial_namespace or s.namespace


def pull_secret_name(registry_id: int) -> str:
    return f"tabularia-registry-{registry_id}"


def image_ref(registry: ContainerRegistry, repository: str, digest: str) -> str:
    """What a Pod names: the registry's host, the repository, the digest."""
    host = "docker.io" if registry.host in ("docker.io", "index.docker.io") else registry.host
    return f"{host}/{cr.repository_name(registry.host, repository)}@{digest}"


def _docker_config(registry: ContainerRegistry, password: str) -> str:
    # Docker Hub's credentials go under the address the runtimes look up for it
    key = "https://index.docker.io/v1/" if registry.host in ("docker.io", "index.docker.io") else registry.host
    auth = base64.b64encode(f"{registry.username}:{password}".encode()).decode()
    return json.dumps({"auths": {key: {"username": registry.username, "password": password, "auth": auth}}})


async def sync_pull_secret(registry: ContainerRegistry, password: str, namespace: str) -> Optional[str]:
    """The registry's pull Secret in the namespace, as its credentials are now; the
    Secret's name, or None for a registry without credentials (anonymous pulls)."""
    name = pull_secret_name(registry.id)
    if not registry.username:
        await kube_jobs.delete_secret(namespace, name)
        return None
    await kube_jobs.put_secret(namespace, {
        "apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
        "metadata": {"name": name, "labels": {"app.kubernetes.io/managed-by": "tabularia", LABEL: str(registry.id)}},
        "data": {".dockerconfigjson": base64.b64encode(_docker_config(registry, password).encode()).decode()},
    })
    return name


async def remove_pull_secrets(registry_id: int) -> None:
    for ns in namespaces():
        await kube_jobs.delete_secret(ns, pull_secret_name(registry_id))


def _pull_check_job(name: str, image: str, pull_secret: Optional[str]) -> dict:
    s = get_settings().containers
    labels = {"app.kubernetes.io/managed-by": "tabularia", "app.kubernetes.io/component": "pull-check"}
    pod: dict = {
        "restartPolicy": "Never", "automountServiceAccountToken": False,
        "securityContext": {"seccompProfile": {"type": "RuntimeDefault"}},
        "containers": [{
            "name": "check", "image": image, "imagePullPolicy": "Always", "command": [MISSING_COMMAND],
            "resources": {"requests": {"cpu": "10m", "memory": "16Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
            "securityContext": {"allowPrivilegeEscalation": False},
        }],
    }
    if pull_secret:
        pod["imagePullSecrets"] = [{"name": pull_secret}]
    return {
        "apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "labels": labels},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": s.pull_check_seconds,
                 "ttlSecondsAfterFinished": s.job_ttl_seconds,
                 "template": {"metadata": {"labels": labels}, "spec": pod}},
    }


async def _pulled(namespace: str, pods: list[dict]) -> bool:
    """The node has the image: it said so, or it went on to start the container."""
    for pod in pods:
        for st in (pod.get("status") or {}).get("containerStatuses") or []:
            state = st.get("state") or {}
            if state.get("terminated") or state.get("running") or (state.get("waiting") or {}).get("reason") in (
                    "CreateContainerError", "RunContainerError", "CreateContainerConfigError"):
                return True
        events = await kube_jobs.job_events(namespace, pod["metadata"]["name"])
        if any(e["reason"] == "Pulled" for e in events):
            return True
    return False


async def try_pull(registry: ContainerRegistry, password: str, repository: str, reference: str) -> dict:
    """{ok, message, image}: can a node of the cluster download this image?"""
    s = get_settings().containers
    client = cr.Registry(host=registry.host, protocol=registry.protocol, tls=registry.tls, ca_cert=registry.ca_cert,
                         username=registry.username, password=password)
    try:
        digest = await cr.digest(client, repository, reference)
    except cr.RegistryError as e:
        return {"ok": False, "message": f"The gateway could not resolve the image: {e}", "image": None}
    image = image_ref(registry, repository, digest)
    ns = trial_namespace()
    name = f"tabularia-pull-{registry.id}-{uuid.uuid4().hex[:8]}"
    loop = asyncio.get_running_loop()
    try:
        secret = await sync_pull_secret(registry, password, ns)
        await kube_jobs.create_job(ns, _pull_check_job(name, image, secret))
        started, failing_since = loop.time(), None
        while loop.time() - started < s.pull_check_seconds + 10:
            await asyncio.sleep(s.poll_seconds)
            pods = await kube_jobs.job_pods(ns, name)
            if await _pulled(ns, pods):
                return {"ok": True, "image": image,
                        "message": f"A node of the cluster downloaded {image}; nothing of it was run"}
            why = await kube_jobs.pull_failure(ns, name)
            now = loop.time()
            if not why:
                failing_since = None
            elif failing_since is None:
                failing_since = now
            elif now - failing_since >= PULL_FAILURE_SECONDS:
                return {"ok": False, "image": image, "message": why}
        return {"ok": False, "image": image,
                "message": f"No node downloaded {image} within {s.pull_check_seconds} s (a large image, or a busy cluster)"}
    except KubeError as e:
        return {"ok": False, "image": image, "message": f"The cluster refused the check: {e}"}
    finally:
        try:
            await kube_jobs.delete_job(ns, name)
        except KubeError:
            pass  # its TTL removes it
