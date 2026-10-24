"""The rollout of the components that read a changed setting only when they start
(Administration → Settings, the settings with the rollout icon; docs/specs/panel-settings.md).

Saving such a setting writes, per component that reads it, when it changed
(`SettingsRollout.changed_at`): its pods started before then run the old value. «Roll out»
asks for their restart (`requested_at`), and the orchestrator does it here, one pod at a
time: it evicts the oldest pod started before the request, waits until the component's
pods are all ready again, evicts the next, and so on, component after component. The new
pods read the panel's values when they start (the gateway's from its database, the
engine's from Valkey); nothing in the Deployments changes.

Eviction, through the Kubernetes API (`pods/eviction`), and not a patch of the
Deployments like `kubectl rollout restart`: the right to patch a Deployment is the right
to change its image or its service account, which the gateway, facing the internet, must
not hold. Eviction honours the chart's PodDisruptionBudgets (at most one pod of a
component down); the chart grants it in the release's namespace only
(`settingsRollout.enabled`).

The state is in the database, so a rollout survives the restart of the orchestrator that
runs it (it evicts itself last; the next one finds nothing left and closes the request).

On Docker Compose there are no pods to evict: the panel names the services to restart
(`COMPOSE`), and the administrator says when it is done.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import quote

from sqlalchemy.engine import Engine
from sqlalchemy import update
from sqlmodel import Session, select

from app.core.config import get_settings
from app.models import SettingsRollout
from app.services.kube_scaling import KubeError

logger = logging.getLogger(__name__)

# the order of a rollout: the orchestrator, which runs it, last
ORDER = ("worker", "preview-worker", "beat", "engine", "gateway", "orchestrator")
# the services of the Docker Compose file; its gateway does both roles, unless it answers
# as an API only (APP__ROLE=api), with the orchestrator in a service of its own
COMPOSE = {"worker": "worker", "preview-worker": "preview-worker", "beat": "beat", "engine": "backend",
           "gateway": "gateway", "orchestrator": "gateway"}
PASSO_OGNI = 5.0
LUCCHETTO = 7_402_121
# a pod on its way out for longer than this (its node lost) no longer holds the rollout
USCITA_MASSIMA = 15 * 60


def compose_service(component: str) -> str:
    if component == "orchestrator" and get_settings().app.role == "api":
        return "orchestrator"
    return COMPOSE[component]


def _ora() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def on_kubernetes() -> bool:
    from app.core.config import on_kubernetes as k8s

    return k8s()


def mode() -> str:
    """kubernetes: the panel restarts the pods; manual: on Kubernetes without the chart's
    right (settingsRollout.enabled=false); compose: Docker Compose."""
    k = get_settings().kubernetes
    if not on_kubernetes():
        return "compose"
    has_token = Path(k.token_file).exists()
    return "kubernetes" if k.rollout and k.release and has_token else "manual"


def mark_changed(session: Session, components: Iterable[str]) -> None:
    """In the transaction of the save: these components run an old value until they start again."""
    now = _ora()
    for c in set(components):
        if c not in ORDER:
            continue
        row = session.get(SettingsRollout, c) or SettingsRollout(component=c)
        row.changed_at = now
        session.add(row)


# ── the pods ─────────────────────────────────────────────────────────────────

def _created(pod: dict) -> Optional[datetime]:
    raw = (pod.get("metadata") or {}).get("creationTimestamp")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except (AttributeError, ValueError):
        return None


def _leaving(pod: dict) -> bool:
    """On its way out, and not for so long that its node must be gone."""
    raw = (pod.get("metadata") or {}).get("deletionTimestamp")
    if not raw:
        return False
    try:
        since = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc).replace(tzinfo=None)
    except ValueError:
        return True
    return (_ora() - since).total_seconds() < USCITA_MASSIMA


def _ready(pod: dict) -> bool:
    if (pod.get("metadata") or {}).get("deletionTimestamp"):
        return False
    status = pod.get("status") or {}
    if status.get("phase") != "Running":
        return False
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])


def _name(pod: dict) -> str:
    return (pod.get("metadata") or {}).get("name") or ""


def _namespace() -> str:
    from app.services.cluster_view import release_namespace

    return release_namespace()


async def pods(component: str) -> list[dict]:
    from app.services import kube_jobs

    k = get_settings().kubernetes
    selector = f"app.kubernetes.io/instance={k.release},app.kubernetes.io/component={component}"
    if k.app_name:
        selector += f",app.kubernetes.io/name={k.app_name}"
    out = await kube_jobs._call("GET", f"/api/v1/namespaces/{_namespace()}/pods?labelSelector={quote(selector)}")
    return [p for p in out.get("items") or [] if (p.get("status") or {}).get("phase") not in ("Succeeded", "Failed")]


def old(pod_list: list[dict], since: Optional[datetime]) -> list[dict]:
    """The pods started before `since`, oldest first, not already on their way out."""
    if since is None:
        return []
    out = [p for p in pod_list if not (p.get("metadata") or {}).get("deletionTimestamp")
           and (_created(p) or datetime.min) < since]
    return sorted(out, key=lambda p: _created(p) or datetime.min)


async def evict(name: str) -> bool:
    """True if evicted (or already gone); False if a PodDisruptionBudget says not now."""
    from app.services import kube_jobs

    ns = _namespace()
    try:
        await kube_jobs._call("POST", f"/api/v1/namespaces/{ns}/pods/{name}/eviction", {
            "apiVersion": "policy/v1", "kind": "Eviction", "metadata": {"name": name, "namespace": ns}})
    except KubeError as e:
        if e.status == 404:
            return True
        if e.status == 429:
            return False
        raise
    return True


# ── the state, for the panel ─────────────────────────────────────────────────

async def status(session: Session) -> dict[str, Any]:
    from app.services.regolazioni_motore import dal_pannello

    m = mode()
    panel = dal_pannello()
    rows = {r.component: r for r in session.exec(select(SettingsRollout)).all()}
    # the six components' pods read together, not one after the other
    letti: dict[str, Any] = {}
    if panel and m == "kubernetes":
        risposte = await asyncio.gather(*(pods(c) for c in ORDER), return_exceptions=True)
        letti = dict(zip(ORDER, risposte))
    items = []
    for c in ORDER:
        r = rows.get(c)
        changed, requested = (r.changed_at, r.requested_at) if r else (None, None)
        item: dict[str, Any] = {"component": c, "changed_at": changed, "requested_at": requested,
                                "requested_by": r.requested_by if r else None, "compose_service": compose_service(c)}
        if not panel:
            # ENGINE__SETTINGS_PANEL=false: the saved values do not apply, nothing to restart for
            item["pending"] = False
        elif m == "kubernetes":
            pl = letti.get(c)
            if isinstance(pl, BaseException):
                if not isinstance(pl, KubeError):
                    raise pl
                # not known: not offered for a restart (it would restart every pod), the error said
                item.update(error=str(pl), pending=False, pods=None)
            else:
                stale = old(pl, changed)
                item.update(pods=len(pl), ready=sum(1 for p in pl if _ready(p)), old=len(stale),
                            pending=bool(stale), waiting_for=[_name(p) for p in pl if not _ready(p)])
        else:
            # without pods to look at: pending from the change until the administrator says done
            item["pending"] = changed is not None and (requested is None or requested < changed)
        items.append(item)
    k = get_settings().kubernetes
    namespace = ""
    if m != "compose":
        try:
            namespace = _namespace()
        except Exception:  # noqa: BLE001 — not known without a namespace file: the command says <namespace>
            namespace = ""
    return {"mode": m, "components": items, "namespace": namespace, "release": k.release}


def request(session: Session, components: Iterable[str], who: str) -> list[str]:
    """Kubernetes: the orchestrator restarts them. Compose: the administrator restarted them."""
    now = _ora()
    done = []
    for c in ORDER:
        if c not in set(components):
            continue
        row = session.get(SettingsRollout, c) or SettingsRollout(component=c)
        row.requested_at, row.requested_by = now, who
        session.add(row)
        done.append(c)
    return done


def cancel(session: Session, component: str) -> bool:
    row = session.get(SettingsRollout, component)
    if row is None or row.requested_at is None:
        return False
    row.requested_at = None
    session.add(row)
    return True


# ── the rollout, in the orchestrator ─────────────────────────────────────────

async def un_passo(bind: Engine) -> Optional[str]:
    """One step of the rollouts asked for: evicts one pod, or closes a finished request,
    or waits. Returns what it did (for the log and the tests)."""
    from app.services.regolazioni_motore import blocca

    me = os.environ.get("HOSTNAME", "")
    with Session(bind) as session:
        if not await blocca(session, attesa=0, chiave=LUCCHETTO):
            return None
        rows = {r.component: r for r in session.exec(
            select(SettingsRollout).where(SettingsRollout.requested_at.is_not(None))).all()}
        for c in ORDER:
            row = rows.get(c)
            if row is None:
                continue
            pl = [p for p in await pods(c)
                  if not (p.get("metadata") or {}).get("deletionTimestamp") or _leaving(p)]
            stale = old(pl, row.requested_at)
            stale_names = {_name(p) for p in stale}
            # a pod on its way out, or a new one not ready yet: wait. A new pod that never
            # gets ready (a value that stops it from starting) stops the rollout there,
            # with the old pods still serving; the last pod's replacement is waited for too,
            # before the next component goes down
            busy = [_name(p) for p in pl if not _ready(p) and
                    ((p.get("metadata") or {}).get("deletionTimestamp") or _name(p) not in stale_names)]
            if busy:
                session.commit()
                return f"waiting {c}: {', '.join(busy)}"
            if not stale:
                # closed only if nobody asked again meanwhile (a new request, not under the lock)
                session.exec(update(SettingsRollout)
                             .where(SettingsRollout.component == c, SettingsRollout.requested_at == row.requested_at)
                             .values(requested_at=None))
                session.commit()
                logger.info("settings: rollout of %s done", c)
                return f"done {c}"
            # an old pod that is not ready first (it serves nothing), itself last: the
            # next orchestrator closes the request
            target = next((p for p in stale if not _ready(p)), None) or next(
                (p for p in stale if _name(p) != me), stale[0])
            name = _name(target)
            if not await evict(name):
                session.commit()
                return f"blocked {c}: {name}"
            logger.info("settings: rollout of %s, pod %s evicted (%d left)", c, name, len(stale) - 1)
            session.commit()
            return f"evicted {c}: {name}"
        session.commit()
    return None


async def rollout_loop(stop: asyncio.Event) -> None:
    if mode() != "kubernetes":
        return
    from app.db.session import engine

    while not stop.is_set():
        try:
            await un_passo(engine)
        except Exception as e:  # noqa: BLE001 — the API or the database down: at the next step
            logger.warning("settings: rollout step failed (%s), again in %ss", e, PASSO_OGNI)
        try:
            await asyncio.wait_for(stop.wait(), timeout=PASSO_OGNI)
        except asyncio.TimeoutError:
            pass
