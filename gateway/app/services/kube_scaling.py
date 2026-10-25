"""Worker autoscaling bounds from Admin → Workers, on Kubernetes.

The panel sets the minimum and maximum number of pods of each kind of worker; the
autoscaler (an HPA, or a KEDA ScaledObject on the queue length) picks the number in
between. The gateway talks to the Kubernetes API with its pod's service account,
which the Helm chart lets read the two worker Deployments and change their two
autoscalers, and nothing else (`adminScaling.enabled`). No client library: a few
JSON requests over httpx.

What the administrator sets is kept in the database (models/worker_scaling.py) and
put back every `REAPPLY_EVERY` seconds: a `helm upgrade` restoring the chart's
values is undone within a minute. With GitOps (Argo CD, Flux) those two fields must
be ignored there, or the two keep undoing each other (see the chart's README).
Removing the bounds from the panel hands them back to the chart.

Off on docker compose and wherever KUBERNETES__SCALING is false: scaling a compose
service from the app would need the Docker socket, which is root on the host.
"""
from __future__ import annotations

import asyncio
import json
import logging
import ssl
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional

import httpx
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import WorkerScaling

logger = logging.getLogger(__name__)

KINDS = ("run", "preview")
REAPPLY_EVERY = 60.0
# the advisory lock of these bounds (pg_advisory_xact_lock), apart from the engine settings'
LOCK = 7_402_119
# for the tests: a transport standing in for the cluster
_transport: Optional[httpx.AsyncBaseTransport] = None


class KubeError(Exception):
    """The Kubernetes API did not do what was asked; `status` is its HTTP status, 0
    when it could not be reached."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Target:
    kind: str
    deployment: str
    autoscaler: str  # "hpa" | "scaledobject" | "" (none)
    autoscaler_name: str
    ceiling: int  # the highest maxReplicas the panel may set

    @property
    def floor(self) -> int:
        # KEDA can scale a Deployment to zero pods; an HPA keeps at least one
        return 0 if self.autoscaler == "scaledobject" else 1


def enabled() -> bool:
    return get_settings().kubernetes.scaling


def target(kind: str) -> Target:
    k = get_settings().kubernetes
    auto = getattr(k, f"{kind}_worker_autoscaler").strip()
    a_kind, _, a_name = auto.partition("/")
    a_kind, a_name = a_kind.strip().lower(), a_name.strip()
    if a_kind not in ("hpa", "scaledobject") or not a_name:
        a_kind, a_name = "", ""
    return Target(kind, getattr(k, f"{kind}_worker_deployment").strip(), a_kind, a_name,
                  getattr(k, f"{kind}_worker_max_replicas"))


def check(t: Target, lo: int, hi: int) -> Optional[dict]:
    """Why these bounds cannot be set, as a code the panel translates; None if they can."""
    if not t.autoscaler:
        return {"code": "no_autoscaler"}
    if lo < t.floor:
        return {"code": "min", "limit": t.floor}
    if hi < max(lo, 1):
        return {"code": "order", "limit": max(lo, 1)}
    if hi > t.ceiling:
        return {"code": "ceiling", "limit": t.ceiling}
    return None


def _namespace() -> str:
    k = get_settings().kubernetes
    if k.namespace:
        return k.namespace
    try:
        return Path(k.namespace_file).read_text().strip()
    except OSError:
        raise KubeError(0, "The pod's namespace is unknown: set KUBERNETES__NAMESPACE")


@lru_cache(maxsize=4)
def _ssl(ca_file: str):
    return ssl.create_default_context(cafile=ca_file) if ca_file and Path(ca_file).exists() else True


def _paths(t: Target) -> tuple[str, Optional[str]]:
    ns = _namespace()
    deployment = f"/apis/apps/v1/namespaces/{ns}/deployments/{t.deployment}"
    autoscaler = {
        "hpa": f"/apis/autoscaling/v2/namespaces/{ns}/horizontalpodautoscalers/{t.autoscaler_name}",
        "scaledobject": f"/apis/keda.sh/v1alpha1/namespaces/{ns}/scaledobjects/{t.autoscaler_name}",
    }.get(t.autoscaler)
    return deployment, autoscaler


async def _call(method: str, path: str, body: Optional[dict] = None) -> dict:
    k = get_settings().kubernetes
    try:
        # read at every call: the kubelet rotates the service account's token
        token = Path(k.token_file).read_text().strip()
    except OSError:
        token = ""
    if not token:
        raise KubeError(0, "The gateway has no service account token: is adminScaling enabled in the chart?")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/merge-patch+json"
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
    return r.json()


def _bounds(t: Target, obj: dict) -> tuple[int, int]:
    spec = obj.get("spec") or {}
    if t.autoscaler == "hpa":
        return int(spec.get("minReplicas", 1)), int(spec.get("maxReplicas", 1))
    # KEDA's defaults when the fields are absent
    return int(spec.get("minReplicaCount", 0)), int(spec.get("maxReplicaCount", 100))


def _patch(t: Target, lo: int, hi: int) -> dict:
    if t.autoscaler == "hpa":
        return {"spec": {"minReplicas": lo, "maxReplicas": hi}}
    return {"spec": {"minReplicaCount": lo, "maxReplicaCount": hi}}


async def read(kind: str) -> dict:
    """The worker Deployment's pods and its autoscaler's bounds, as the cluster has them."""
    t = target(kind)
    out: dict = {"deployment": t.deployment or None, "autoscaler": t.autoscaler or None,
                 "autoscaler_name": t.autoscaler_name or None, "ceiling": t.ceiling, "floor": t.floor}
    if not t.deployment:
        raise KubeError(0, f"No Deployment configured for the {kind} workers (KUBERNETES__{kind.upper()}_WORKER_DEPLOYMENT)")
    dep_path, auto_path = _paths(t)
    dep = await _call("GET", dep_path)
    status = dep.get("status") or {}
    out.update(replicas=(dep.get("spec") or {}).get("replicas"), current=status.get("replicas") or 0,
               ready=status.get("readyReplicas") or 0)
    if auto_path:
        obj = await _call("GET", auto_path)
        out["min"], out["max"] = _bounds(t, obj)
        out["desired"] = (obj.get("status") or {}).get("desiredReplicas") if t.autoscaler == "hpa" else None
    return out


async def apply(kind: str, lo: int, hi: int) -> None:
    t = target(kind)
    _, auto_path = _paths(t)
    if not auto_path:
        raise KubeError(0, f"No autoscaler configured for the {kind} workers")
    await _call("PATCH", auto_path, _patch(t, lo, hi))


async def one_round(bind: Engine) -> int:
    """Puts back the bounds set from the panel where the cluster has others (a helm
    upgrade, a hand edit). Skips if someone is saving: they apply them. Returns how
    many it put back."""
    from app.services.engine_panel_settings import lock

    fixed = 0
    with Session(bind) as session:
        if not await lock(session, wait=0, key=LOCK):
            return 0
        for row in session.exec(select(WorkerScaling)).all():
            t = target(row.kind)
            if not t.autoscaler:
                continue
            # a ceiling lowered in the environment since: it wins
            hi = min(row.max_replicas, t.ceiling)
            lo = min(row.min_replicas, hi)
            _, auto_path = _paths(t)
            if _bounds(t, await _call("GET", auto_path)) != (lo, hi):
                await _call("PATCH", auto_path, _patch(t, lo, hi))
                fixed += 1
                logger.info("workers: the %s workers' autoscaler had other bounds, put back to %s–%s", row.kind, lo, hi)
        session.commit()  # releases the lock
    return fixed


async def reapply_loop(stop: asyncio.Event) -> None:
    if not enabled():
        return
    from app.db.session import engine

    while not stop.is_set():
        try:
            await one_round(engine)
        except Exception as e:  # noqa: BLE001 — API server down or restarting: at the next round
            logger.warning("workers: autoscaling bounds not checked (%s), again in %ss", e, REAPPLY_EVERY)
        try:
            await asyncio.wait_for(stop.wait(), timeout=REAPPLY_EVERY)
        except asyncio.TimeoutError:
            pass
