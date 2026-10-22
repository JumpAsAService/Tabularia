"""Administration → Cluster: what Tabularia has running on Kubernetes, read-only.

The pods of the release's namespace (gateway, orchestrator, engine, workers…) and of the
namespaces where users' code runs (scripts, their trials, image builds, containers and
their trials): their state, restarts, age, node, and CPU and memory against their
limits when the cluster has metrics-server; the warning events of those namespaces.
A Job of a script or a container names the run of Tabularia that started it, and the
flow, saved script or flow group behind it.

The gateway reads with its own service account (the chart's `clusterView`: pods and
events get/list, metrics.k8s.io pods get/list, in those namespaces only). Nothing is
written, no log is read, nobody gets into a container: for that, a Kubernetes client.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import Flow, FlowGroup, Run, SavedScript, ScriptEnvironment
from app.services import kube_jobs
from app.services.kube_scaling import KubeError

LABEL_SCRIPT_RUN = "tabularia.io/script-run"
LABEL_CONTAINER_RUN = "tabularia.io/container-run"
LABEL_ENV = "tabularia.io/environment"
MAX_EVENTS = 40

_CPU = re.compile(r"^(\d+(?:\.\d+)?)(n|u|m)?$")
_MEMORY = re.compile(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti|k|M|G|T|e\d+)?$")


def cpu_millis(q: Optional[str]) -> Optional[float]:
    """A Kubernetes CPU quantity in millicores ("250m", "1", "123456n")."""
    m = _CPU.match(str(q or "").strip())
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2)
    return value / 1e6 if unit == "n" else value / 1e3 if unit == "u" else value if unit == "m" else value * 1000


def memory_bytes(q: Optional[str]) -> Optional[int]:
    """A Kubernetes memory quantity in bytes ("512Mi", "1Gi", "1000000")."""
    m = _MEMORY.match(str(q or "").strip())
    if not m:
        return None
    value, unit = float(m.group(1)), m.group(2) or ""
    factor = {"Ki": 2**10, "Mi": 2**20, "Gi": 2**30, "Ti": 2**40, "k": 10**3, "M": 10**6, "G": 10**9, "T": 10**12}
    if unit.startswith("e"):
        return int(value * 10 ** int(unit[1:]))
    return int(value * factor.get(unit, 1))


def disabled_reason() -> Optional[str]:
    """Why the page has nothing to show: "kubernetes" off it (Docker Compose), "off"
    when the chart's clusterView is off."""
    k = get_settings().kubernetes
    if not Path(k.token_file).exists():
        return "kubernetes"
    if not k.cluster_view:
        return "off"
    return None


def release_namespace() -> str:
    k = get_settings().kubernetes
    if k.namespace:
        return k.namespace
    try:
        return Path(k.namespace_file).read_text().strip()
    except OSError:
        return ""


def namespaces() -> list[tuple[str, str]]:
    """(namespace, role) of every namespace the page looks at, Tabularia's first."""
    from app.services import container_runs, script_builds, script_runs

    s = get_settings()
    out = [(release_namespace(), "tabularia")]
    if s.scripts.enabled:
        out += [(script_runs.namespace_for(False), "scripts"), (script_runs.namespace_for(True), "script-trials"),
                (script_builds.build_namespace(), "script-builds")]
    if s.containers.enabled:
        out += [(container_runs.namespace_for(False), "containers"), (container_runs.namespace_for(True), "container-trials")]
    seen: set[str] = set()
    return [(ns, role) for ns, role in out if ns and not (ns in seen or seen.add(ns))]


def _state(pod: dict) -> tuple[str, int, str]:
    """(what the pod is doing, restarts, ready "x/y"): the reason a container waits or
    ended when there is one (CrashLoopBackOff, ImagePullBackOff, OOMKilled…), else
    the pod's phase."""
    status = pod.get("status") or {}
    statuses = status.get("containerStatuses") or []
    restarts = sum(int(c.get("restartCount") or 0) for c in statuses)
    ready = f"{sum(1 for c in statuses if c.get('ready'))}/{len(statuses) or len((pod.get('spec') or {}).get('containers') or [])}"
    for c in statuses:
        state = c.get("state") or {}
        if (state.get("waiting") or {}).get("reason") not in (None, "ContainerCreating", "PodInitializing"):
            return state["waiting"]["reason"], restarts, ready
        last = (c.get("lastState") or {}).get("terminated") or {}
        if (state.get("terminated") or {}).get("reason") in ("OOMKilled", "Error"):
            return state["terminated"]["reason"], restarts, ready
        if last.get("reason") == "OOMKilled" and restarts:
            return "OOMKilled", restarts, ready
    if status.get("phase") == "Pending":
        for cond in status.get("conditions") or []:
            if cond.get("type") == "PodScheduled" and cond.get("status") == "False":
                return "Unschedulable", restarts, ready
    return status.get("phase") or "Unknown", restarts, ready


def _limits(pod: dict) -> tuple[Optional[float], Optional[int]]:
    cpu, memory = 0.0, 0
    any_cpu = any_memory = False
    for c in (pod.get("spec") or {}).get("containers") or []:
        lim = (c.get("resources") or {}).get("limits") or {}
        if cpu_millis(lim.get("cpu")) is not None:
            cpu += cpu_millis(lim.get("cpu"))
            any_cpu = True
        if memory_bytes(lim.get("memory")) is not None:
            memory += memory_bytes(lim.get("memory"))
            any_memory = True
    return (cpu if any_cpu else None), (memory if any_memory else None)


async def _get(path: str) -> Optional[dict]:
    try:
        return await kube_jobs._call("GET", path)
    except KubeError as e:
        if e.status in (403, 404):
            return None  # no right there, or no metrics-server: the page shows less
        raise


async def _namespace(ns: str) -> tuple[list[dict], Optional[dict], list[dict], Optional[str]]:
    """(pods, metrics by pod name or None, warning events, error) of one namespace."""
    try:
        pods, metrics, events = await asyncio.gather(
            _get(f"/api/v1/namespaces/{ns}/pods"),
            _get(f"/apis/metrics.k8s.io/v1beta1/namespaces/{ns}/pods"),
            _get(f"/api/v1/namespaces/{ns}/events?fieldSelector=type%3DWarning"),
        )
    except KubeError as e:
        return [], None, [], str(e)
    if pods is None:
        return [], None, [], f"Tabularia may not read the pods of {ns}"
    usage = None
    if metrics is not None:
        usage = {}
        for item in metrics.get("items") or []:
            cpu = sum(cpu_millis((c.get("usage") or {}).get("cpu")) or 0 for c in item.get("containers") or [])
            mem = sum(memory_bytes((c.get("usage") or {}).get("memory")) or 0 for c in item.get("containers") or [])
            usage[(item.get("metadata") or {}).get("name")] = (cpu, mem)
    return pods.get("items") or [], usage, (events or {}).get("items") or [], None


def _who(session: Session, run_ids: set[int], env_ids: set[int]) -> tuple[dict, dict]:
    """What started the runs of the Jobs: {run id: {kind, id, name}}, and the
    environments' names."""
    runs: dict[int, dict] = {}
    if run_ids:
        rows = session.exec(select(Run).where(Run.id.in_(run_ids))).all()  # type: ignore[union-attr]
        parents = {r.parent_run_id for r in rows if r.parent_run_id}
        parent_rows = {p.id: p for p in session.exec(select(Run).where(Run.id.in_(parents))).all()} if parents else {}  # type: ignore[union-attr]
        for r in rows:
            head = parent_rows.get(r.parent_run_id) if r.parent_run_id else None
            flow_id = r.flow_id or (head.flow_id if head else None)
            script_id = r.saved_script_id or (head.saved_script_id if head else None)
            group_id = head.flow_group_id if head else None
            if script_id and (script := session.get(SavedScript, script_id)):
                runs[r.id] = {"kind": "script", "id": script.id, "name": script.name}
            elif flow_id and (flow := session.get(Flow, flow_id)):
                runs[r.id] = {"kind": "flow", "id": flow.id, "name": flow.name}
            elif group_id and (group := session.get(FlowGroup, group_id)):
                runs[r.id] = {"kind": "group", "id": group.id, "name": group.name}
    envs = {}
    if env_ids:
        envs = {e.id: e.name for e in session.exec(select(ScriptEnvironment).where(
            ScriptEnvironment.id.in_(env_ids))).all()}  # type: ignore[union-attr]
    return runs, envs


def _int(value) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def snapshot(session: Session) -> dict:
    """The page's content, read now."""
    spaces = namespaces()
    results = await asyncio.gather(*(_namespace(ns) for ns, _ in spaces))
    metrics_seen = any(usage is not None for _, usage, _, _ in results)
    run_ids: set[int] = set()
    env_ids: set[int] = set()
    for pods, _, _, _ in results:
        for p in pods:
            labels = (p.get("metadata") or {}).get("labels") or {}
            for key in (LABEL_SCRIPT_RUN, LABEL_CONTAINER_RUN):
                if _int(labels.get(key)) is not None:
                    run_ids.add(_int(labels[key]))
            if _int(labels.get(LABEL_ENV)) is not None:
                env_ids.add(_int(labels[LABEL_ENV]))
    started_by, env_names = _who(session, run_ids, env_ids)

    out_spaces, events = [], []
    now = datetime.now(timezone.utc)
    for (ns, role), (pods, usage, warnings, error) in zip(spaces, results):
        items = []
        for p in sorted(pods, key=lambda x: (x.get("metadata") or {}).get("name") or ""):
            meta, spec, status = p.get("metadata") or {}, p.get("spec") or {}, p.get("status") or {}
            labels = meta.get("labels") or {}
            state, restarts, ready = _state(p)
            cpu_limit, memory_limit = _limits(p)
            cpu, memory = (usage or {}).get(meta.get("name"), (None, None))
            run_id = _int(labels.get(LABEL_SCRIPT_RUN)) or _int(labels.get(LABEL_CONTAINER_RUN))
            env_id = _int(labels.get(LABEL_ENV))
            items.append({
                "name": meta.get("name"), "component": labels.get("app.kubernetes.io/component") or labels.get("app")
                or labels.get("tabularia.io/stage") or "", "state": state, "phase": status.get("phase"),
                "ready": ready, "restarts": restarts, "started_at": status.get("startTime"), "node": spec.get("nodeName"),
                "cpu_millis": cpu, "memory_bytes": memory, "cpu_limit_millis": cpu_limit, "memory_limit_bytes": memory_limit,
                "run_id": run_id, "started_by": started_by.get(run_id) if run_id else None,
                "environment": env_names.get(env_id) if env_id else None,
            })
        out_spaces.append({"name": ns, "role": role, "pods": items, "error": error})
        for e in warnings:
            obj = e.get("involvedObject") or {}
            events.append({"namespace": ns, "object": f"{(obj.get('kind') or '').lower()}/{obj.get('name') or ''}",
                           "reason": e.get("reason") or "", "message": (e.get("message") or "")[:500],
                           "count": e.get("count") or 1,
                           "last_seen": e.get("lastTimestamp") or e.get("eventTime") or (e.get("metadata") or {}).get("creationTimestamp")})
    events.sort(key=lambda e: e["last_seen"] or "", reverse=True)
    return {"generated_at": now.isoformat(), "metrics": metrics_seen, "namespaces": out_spaces, "events": events[:MAX_EVENTS]}
