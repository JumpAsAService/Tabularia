"""The pods of the marimo notebooks: started when someone opens a notebook, stopped when
nobody uses it. docs/specs/notebooks.md («Runtime»); the object in services/notebooks.py,
the proxy in routes/notebook_proxy.py, the data in routes/notebook_data.py.

A session is one live pod of a notebook in one mode — `app` (marimo run: the code does
not reach the browser) or `edit` (marimo's own editor) —, a row of `notebook_sessions`
that every gateway replica reads. At most one per notebook and mode: an app shared by
all its viewers, an editor by one person at a time.

- Whose rights: the app reads with those of whoever saved the version it runs (it must
  still exist, be active and see the notebook), the editor with the editor's. Checked at
  every start, and at every read by routes/notebook_data.py.
- The pod: a Job in the scripts namespace, with the image of the notebook's environment
  and the scripts' security context. The notebook's file goes in a ConfigMap and is
  copied into an emptyDir (`/work`), where the editor saves it; in edit mode a second
  container serves `/work` on FILES_PORT, so the gateway reads what marimo saved. The
  `tabularia` module (app/notebook_pod/tabularia.py) is mounted from the same ConfigMap.
  The pod's notebook token is only in its Secret; the row keeps its SHA-256. The
  environment's variables reach it as they reach a script (services/script_runs.py):
  the plain ones in its environment, the secret ones and those handed over from
  Tabularia in its Secret; they never replace the notebook's own (OWN_VARIABLES).
- Ready: the pod's readiness probe (marimo's /health) is the sign; the gateway then
  proxies to the pod's IP. A pod that cannot start (image not downloaded, no node,
  marimo missing from the image, marimo stopped) fails the session with the reason in
  words, and its Job goes.
- Stopping: a person (DELETE …/sessions/{mode}), or the reaper in the orchestrator role
  for a pod idle past NOTEBOOKS__IDLE_MINUTES, gone from the cluster, or not started in
  NOTEBOOKS__START_TIMEOUT_SECONDS. An editor's file becomes the notebook's draft first
  (and at every round of the reaper), so nothing is lost when its pod stops.
- Visitors: marimo gives every browser that opens an app a kernel of its own, with its
  own copy of the data, all in the app's one pod; ten people on a heavy app are ten
  copies, and the pod runs out of memory for everyone. So an app takes at most
  `visitors_cap` visitors at once — the notebook's own number, or
  NOTEBOOKS__MAX_VISITORS_PER_APP, never above NOTEBOOKS__MAX_VISITORS_CEILING. A visitor
  is marimo's WebSocket (`…/app/ws`) open through the proxy, a row of
  `notebook_visitors`, so that every replica counts the same ones: counted and taken
  under a lock per notebook, refreshed once a minute while it is open, deleted when it
  closes; one not refreshed for VISITOR_STALE (a replica that died) no longer counts,
  and the reaper clears it. The editor has no cap: one person at a time edits.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx
from fastapi import HTTPException, Request
from sqlalchemy import delete, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, func, select

from app.core.config import get_settings
from app.models import (
    Notebook, NotebookSession, NotebookVisitor, ScriptEnvironment, ScriptEnvironmentVersion, User,
)
from app.services import audit, kube_jobs, notebooks, script_builds
from app.services.kube_scaling import KubeError

logger = logging.getLogger(__name__)

APP_PORT = 2718  # marimo
FILES_PORT = 2719  # the editor's /work, read-only
LABEL_COMPONENT = "app.kubernetes.io/component"
COMPONENT = "notebook"  # the chart's NetworkPolicy selects the notebook pods by it
LABEL_NOTEBOOK = "tabularia.io/notebook"
LABEL_MODE = "tabularia.io/notebook-mode"
TOKEN_ENV = "TABULARIA_NOTEBOOK_TOKEN"
# a notebook token never looks like a session's JWT nor an API token (`tab_…`): no other
# route takes it
TOKEN_PREFIX = "nbk_"
LIB_DIR = "/tabularia/lib"  # the `tabularia` module, on PYTHONPATH
# what the pod needs to work: an environment's variable of the same name is left out
OWN_VARIABLES = frozenset({"HOME", "PYTHONPATH", "TABULARIA_URL", TOKEN_ENV})
SOURCE_DIR = "/tabularia/source"  # the notebook's file as it started, read-only
WORK_DIR = "/work"  # where marimo runs and saves it
POD_MODULE = Path(__file__).resolve().parents[1] / "notebook_pod" / "tabularia.py"
LIVE = ("starting", "ready")
LOCK = 7_402_140  # the reaper's round
START_LOCK = 7_402_141  # the cap of live pods, counted and taken together
VISITORS_LOCK = 7_402_142  # with the notebook's id: its visitors, counted and taken together
# a visitor refreshed once a minute (routes/notebook_proxy.py) that has missed three
# beats is gone: its replica died without deleting it
VISITOR_STALE = timedelta(minutes=3)
REAP_SECONDS = 30
TOUCH_EVERY = timedelta(seconds=60)
CHECK_EVERY = 1.0  # a starting pod is asked about at most this often per process
NO_JOB_SECONDS = 60  # a starting session whose Job never appeared (its process died)
FILE_TIMEOUT = 5.0  # reading the editor's file from its pod
MAX_FILE_BYTES = 4 * 1024 * 1024
ORCHESTRATOR = "orchestrator"  # the audit's actor when the reaper stops a pod

# the marimo container copies the file into /work, then becomes marimo: no shell needed
_COPY_AND_RUN = ("import os, shutil, sys; "
                 f"shutil.copyfile('{SOURCE_DIR}/notebook.py', '{WORK_DIR}/notebook.py'); "
                 "os.execv(sys.executable, [sys.executable, '-m', 'marimo', *sys.argv[1:]])")
_NO_MARIMO = re.compile(r"No module named '?marimo'?")
_MARIMO_LOCKED = re.compile(r"(?mi)^marimo\s*(==|@)")

# when this process last asked the cluster about a starting session (row id → monotonic)
_checked: dict[int, float] = {}


class BeingEdited(Exception):
    """Somebody else has the notebook's editor open."""

    def __init__(self, notebook: Notebook, editor: Optional[User]):
        self.notebook, self.editor = notebook, editor
        who = editor.email if editor else "someone else"
        super().__init__(f"«{notebook.name}» is being edited by {who}")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def _age(t: Optional[datetime], now: Optional[datetime] = None) -> float:
    if t is None:
        return 0.0
    return ((now or _now()) - _aware(t).replace(tzinfo=None)).total_seconds()


def fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def disabled_reason() -> Optional[str]:
    """Why notebooks cannot run here (no Kubernetes, scripting not configured); None when
    they can."""
    reason = script_builds.disabled_reason()
    if reason == "kubernetes":
        return "Notebooks run on Kubernetes, with the scripting environments: they are off on this installation"
    if reason:
        return "Notebooks need the scripting environments, which are not configured on this installation"
    return None


def namespace() -> str:
    return get_settings().scripts.namespace


def base_url(notebook_id: int, mode: str) -> str:
    """Where marimo answers, as the browser sees it (behind the ingress's prefix)."""
    return f"{get_settings().app.root_path}/notebooks/{notebook_id}/{mode}"


def row_of(session: Session, notebook_id: int, mode: str) -> Optional[NotebookSession]:
    return session.exec(select(NotebookSession).where(NotebookSession.notebook_id == notebook_id,
                                                      NotebookSession.mode == mode)).first()


def touch(session: Session, row: NotebookSession) -> None:
    """Somebody is using the pod: its idle time starts again (at most once a minute)."""
    now = _now()
    if _age(row.last_seen_at, now) < TOUCH_EVERY.total_seconds():
        return
    row.last_seen_at = now
    session.add(row)
    session.commit()


# ── who, with what ───────────────────────────────────────────────────────────
def _author(session: Session, notebook: Notebook) -> User:
    """Whose rights the app reads with: whoever saved the version it runs, still there,
    active and still able to open the notebook (its folder and its environment)."""
    row = notebooks.version_row(session, notebook)
    author = session.get(User, row.created_by) if row is not None and row.created_by else None
    fix = "someone who may edit the notebook must save or restore a version, which then runs with their rights"
    if author is None:
        raise HTTPException(status_code=422, detail=f"The author of the version the app runs no longer exists: {fix}")
    if not author.is_active:
        raise HTTPException(status_code=422, detail=f"The author of the version the app runs, {author.email}, "
                                                    f"is deactivated: {fix}")
    if not notebooks.can_see(session, author, notebook):
        raise HTTPException(status_code=422, detail=f"The author of the version the app runs, {author.email}, can no "
                                                    f"longer open this notebook (its folder or its environment): {fix}")
    return author


def _image(session: Session, notebook: Notebook) -> tuple[ScriptEnvironment, ScriptEnvironmentVersion]:
    env = session.get(ScriptEnvironment, notebook.environment_id)
    if env is None:
        raise HTTPException(status_code=422, detail="The notebook's scripting environment no longer exists: choose another")
    if not env.enabled:
        raise HTTPException(status_code=422, detail=f"The scripting environment «{env.name}» is disabled")
    version = session.get(ScriptEnvironmentVersion, env.current_version_id) if env.current_version_id else None
    if version is None or version.status != "ready" or not version.image_ref:
        raise HTTPException(status_code=422, detail=f"The scripting environment «{env.name}» has no image ready yet: "
                                                    "an administrator builds it in Administration → Scripting environments")
    if version.lock and not _MARIMO_LOCKED.search(version.lock):
        raise HTTPException(status_code=422, detail=no_marimo(env.name))
    return env, version


def no_marimo(env_name: str) -> str:
    return (f"marimo is not in the image of the scripting environment «{env_name}»: an administrator rebuilds it "
            "(Administration → Scripting environments → Build again). Every Python environment has marimo now, "
            "but the images built before it do not")


def _variables(session: Session, env: ScriptEnvironment) -> tuple[dict[str, str], dict[str, str]]:
    """(plain, secret): the environment's variables, with the same rules as a script's —
    the plain ones go in the pod's environment, the secret ones and those handed over
    from Tabularia in its Secret —, without the ones the notebook sets itself."""
    from app.services.script_runs import ScriptFailed, _variables as script_variables

    try:
        plain, secret, _snapshot = script_variables(session, env)
    except ScriptFailed as e:
        raise HTTPException(status_code=422, detail=f"The scripting environment «{env.name}» cannot start "
                                                    f"the notebook: {e}") from None
    return ({k: v for k, v in plain.items() if k not in OWN_VARIABLES},
            {k: v for k, v in secret.items() if k not in OWN_VARIABLES})


# ── the pod ──────────────────────────────────────────────────────────────────
def _security() -> dict:
    return {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True, "capabilities": {"drop": ["ALL"]}}


def labels(notebook_id: int, mode: str) -> dict:
    return {LABEL_COMPONENT: COMPONENT, LABEL_NOTEBOOK: str(notebook_id), LABEL_MODE: mode}


# the startup probe: up to 25 s polling marimo's health inside the pod (argv[1])
_WAIT_FOR_MARIMO = (
    "import sys, time, urllib.request\n"
    "end = time.monotonic() + 25\n"
    "while time.monotonic() < end:\n"
    "    try:\n"
    "        urllib.request.urlopen(sys.argv[1], timeout=2)\n"
    "        sys.exit(0)\n"
    "    except Exception:\n"
    "        time.sleep(0.25)\n"
    "sys.exit(1)\n"
)


def job_manifest(*, name: str, notebook_id: int, mode: str, env: ScriptEnvironment,
                 version: ScriptEnvironmentVersion, config_name: str, secret_name: str,
                 variables: Optional[dict[str, str]] = None) -> dict:
    """The notebook's Job: marimo, and in edit mode the container that serves its file.
    `variables`: the environment's plain ones (the secret ones are in `secret_name`)."""
    s = get_settings()
    base = base_url(notebook_id, mode)
    args = ["run" if mode == "app" else "edit", f"{WORK_DIR}/notebook.py", "--host", "0.0.0.0",
            "--port", str(APP_PORT), "--headless", "--base-url", base]
    if mode == "edit":
        args += ["--no-token", "--skip-update-check"]
    environment = {**{k: v for k, v in (variables or {}).items() if k not in OWN_VARIABLES},
                   "HOME": "/tmp", "PYTHONPATH": LIB_DIR}
    if s.notebooks.gateway_url.strip():
        environment["TABULARIA_URL"] = s.notebooks.gateway_url.strip().rstrip("/")
    marimo = {
        "name": "marimo", "image": version.image_ref, "command": ["python", "-c", _COPY_AND_RUN, *args],
        "workingDir": WORK_DIR,
        "env": [{"name": k, "value": v} for k, v in environment.items()],
        "envFrom": [{"secretRef": {"name": secret_name}}],
        "ports": [{"name": "marimo", "containerPort": APP_PORT}],
        "resources": {"requests": {"cpu": env.cpu_request, "memory": env.memory_request},
                      "limits": {"cpu": env.cpu_limit, "memory": env.memory_limit}},
        "securityContext": _security(),
        # marimo answers a few seconds after starting. Every failed probe is a Warning
        # event (in Administration → Cluster), so the startup probe is one command that
        # waits for marimo inside the pod and succeeds the first time
        "startupProbe": {"exec": {"command": ["python", "-c", _WAIT_FOR_MARIMO, f"http://127.0.0.1:{APP_PORT}{base}/health"]},
                         # short period: the kubelet waits up to one period before the first probe
                         "periodSeconds": 3, "timeoutSeconds": 28,
                         "failureThreshold": max(1, -(-s.notebooks.start_timeout_seconds // 28))},
        "readinessProbe": {"httpGet": {"path": f"{base}/health", "port": APP_PORT},
                           "periodSeconds": 2, "timeoutSeconds": 2, "failureThreshold": 3},
        "volumeMounts": [{"name": "work", "mountPath": WORK_DIR}, {"name": "tmp", "mountPath": "/tmp"},
                         {"name": "lib", "mountPath": LIB_DIR, "readOnly": True},
                         {"name": "source", "mountPath": SOURCE_DIR, "readOnly": True}],
    }
    containers = [marimo]
    if mode == "edit":
        containers.append({
            "name": "files", "image": version.image_ref,
            "command": ["python", "-m", "http.server", str(FILES_PORT), "--bind", "0.0.0.0", "--directory", WORK_DIR],
            "ports": [{"name": "files", "containerPort": FILES_PORT}],
            "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"cpu": "100m", "memory": "64Mi"}},
            "securityContext": _security(),
            "volumeMounts": [{"name": "work", "mountPath": WORK_DIR, "readOnly": True}],
        })
    pod: dict = {
        "restartPolicy": "Never", "automountServiceAccountToken": False, "enableServiceLinks": False,
        # nothing to finish when a notebook stops (the editor's file is read before), and
        # http.server ignores SIGTERM: 5 s instead of the default 30 in «Terminating»
        "terminationGracePeriodSeconds": 5,
        "securityContext": {"runAsNonRoot": True, "runAsUser": script_builds.SCRIPT_UID,
                            "runAsGroup": script_builds.SCRIPT_UID, "seccompProfile": {"type": "RuntimeDefault"}},
        "containers": containers, **script_builds.node_selector(version),
        "volumes": [{"name": "work", "emptyDir": {}}, {"name": "tmp", "emptyDir": {}},
                    {"name": "lib", "configMap": {"name": config_name,
                                                  "items": [{"key": "tabularia.py", "path": "tabularia.py"}]}},
                    {"name": "source", "configMap": {"name": config_name,
                                                     "items": [{"key": "notebook.py", "path": "notebook.py"}]}}],
    }
    if s.scripts.registry_pull_secret:
        pod["imagePullSecrets"] = [{"name": s.scripts.registry_pull_secret}]
    tags = labels(notebook_id, mode)
    return {
        "apiVersion": "batch/v1", "kind": "Job", "metadata": {"name": name, "labels": tags},
        "spec": {"backoffLimit": 0, "activeDeadlineSeconds": s.notebooks.max_lifetime_hours * 3600,
                 "ttlSecondsAfterFinished": s.scripts.job_ttl_seconds,
                 "template": {"metadata": {"labels": tags}, "spec": pod}},
    }


async def _create_pod(row: NotebookSession, env: ScriptEnvironment, version: ScriptEnvironmentVersion,
                      source: str, token: str, plain: dict[str, str], secret: dict[str, str]) -> None:
    config_name, secret_name = f"{row.job_name}-files", f"{row.job_name}-token"
    manifest = job_manifest(name=row.job_name, notebook_id=row.notebook_id, mode=row.mode, env=env,
                            version=version, config_name=config_name, secret_name=secret_name, variables=plain)
    tags = manifest["metadata"]["labels"]
    job = await kube_jobs.create_job(row.namespace, manifest)
    # owned by the Job: they go away with it. Its Pod waits for them to exist
    owners = kube_jobs.owned_by(job)
    await kube_jobs.create_object(row.namespace, "configmaps", {
        "apiVersion": "v1", "kind": "ConfigMap",
        "metadata": {"name": config_name, "labels": tags, "ownerReferences": owners},
        "data": {"notebook.py": source, "tabularia.py": POD_MODULE.read_text()}})
    await kube_jobs.create_object(row.namespace, "secrets", {
        "apiVersion": "v1", "kind": "Secret", "type": "Opaque",
        "metadata": {"name": secret_name, "labels": tags, "ownerReferences": owners},
        "stringData": {**secret, TOKEN_ENV: token}})


# ── starting ─────────────────────────────────────────────────────────────────
async def ensure_started(session: Session, notebook: Notebook, mode: str, user: User,
                         request: Optional[Request] = None) -> NotebookSession:
    """The live session of the notebook in `mode`, started when there is none. Whoever
    asks has been checked already: VIEW for the app, EDIT for the editor. An app whose
    version is no longer the current one starts again with it; a failed session is tried
    again; somebody else's editor is BeingEdited."""
    why = disabled_reason()
    if why:
        raise HTTPException(status_code=400, detail=why)
    row = row_of(session, notebook.id, mode)
    if row is not None and row.status != "failed":
        if mode == "edit" and row.user_id != user.id:
            raise BeingEdited(notebook, session.get(User, row.user_id))
        if mode == "app" and row.version != notebook.current_version:
            await stop(session, row, actor=None, reason="new version")
        else:
            touch(session, row)
            await refresh(session, row)
            return row
    elif row is not None:
        await stop(session, row, actor=None, reason="tried again", quiet=True)
    return await _start(session, notebook, mode, user, request)


async def _start(session: Session, notebook: Notebook, mode: str, user: User,
                 request: Optional[Request]) -> NotebookSession:
    from app.services.engine_panel_settings import lock

    holder = user if mode == "edit" else _author(session, notebook)
    env, version = _image(session, notebook)
    plain, secret = _variables(session, env)
    source = notebook.draft if mode == "edit" and notebook.draft is not None else \
        notebooks.content_of(session, notebook)["source"]
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    cap = get_settings().notebooks.max_live
    if not await lock(session, wait=5, key=START_LOCK):
        raise HTTPException(status_code=503, detail="Tabularia is busy starting other notebooks: try again in a moment")
    live = session.exec(select(func.count()).select_from(NotebookSession)
                        .where(NotebookSession.status.in_(LIVE))).one()  # type: ignore[attr-defined]
    if live >= cap:
        session.rollback()
        raise HTTPException(status_code=503, detail=f"{live} notebooks are running already, the most this installation "
                                                    "allows: try again later, or ask an administrator "
                                                    "(NOTEBOOKS__MAX_LIVE)")
    row = NotebookSession(notebook_id=notebook.id, mode=mode, user_id=holder.id, version=notebook.current_version,
                          namespace=namespace(), job_name=f"nb-{notebook.id}-{mode}-{secrets.token_hex(3)}",
                          token_fp=fingerprint(token))
    session.add(row)
    try:
        session.commit()
    except IntegrityError:  # another replica started it a moment ago
        session.rollback()
        existing = row_of(session, notebook.id, mode)
        if existing is None:
            raise HTTPException(status_code=409, detail="The notebook was being started and stopped at once: try again")
        if mode == "edit" and existing.user_id != user.id:
            raise BeingEdited(notebook, session.get(User, existing.user_id))
        return existing
    session.refresh(row)
    try:
        await _create_pod(row, env, version, source, token, plain, secret)
    except KubeError as e:
        await _fail(session, row, f"the cluster refused its pod: {e}")
    except Exception as e:  # noqa: BLE001 — never a session left starting with nothing behind it
        await _fail(session, row, f"its pod could not be created ({type(e).__name__})")
        raise
    audit.record_audit(session, actor=user, action=audit.NOTEBOOK_START, target_type="notebook",
                       target_id=notebook.id, target_label=notebook.name, request=request,
                       detail={"mode": mode, "version": row.version, "rights_of": holder.email,
                               "environment": env.name, **({"outcome": row.error} if row.status == "failed" else {})})
    return row


# ── following it ─────────────────────────────────────────────────────────────
def _container(pod: dict, name: str) -> dict:
    for st in (pod.get("status") or {}).get("containerStatuses") or []:
        if st.get("name") == name:
            return st
    return {}


def _ready(pod: dict) -> bool:
    status = pod.get("status") or {}
    return bool(status.get("podIP")) and any(
        c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions") or [])


def _latest(pods: list[dict]) -> Optional[dict]:
    pods = sorted(pods, key=lambda p: (p.get("metadata") or {}).get("creationTimestamp") or "")
    return pods[-1] if pods else None


async def _ended(session: Session, row: NotebookSession, pod: dict) -> Optional[str]:
    """Why marimo is no longer running in the pod, in words; None while it runs."""
    terminated = (_container(pod, "marimo").get("state") or {}).get("terminated")
    stopped = kube_jobs.stopped_by_cluster(pod)
    if not terminated and not stopped:
        return None
    if stopped:
        return kube_jobs.STOPPED_BY_CLUSTER
    log = ""
    try:
        log = await kube_jobs.pod_log(row.namespace, pod["metadata"]["name"], "marimo", 40)
    except KubeError:
        pass
    if _NO_MARIMO.search(log):
        notebook = session.get(Notebook, row.notebook_id)
        env = session.get(ScriptEnvironment, notebook.environment_id) if notebook else None
        return no_marimo(env.name if env else "?")
    limit = ""
    for c in ((pod.get("spec") or {}).get("containers") or []):
        if c.get("name") == "marimo":
            limit = ((c.get("resources") or {}).get("limits") or {}).get("memory") or ""
    why = kube_jobs.explain_exit(terminated.get("exitCode"), terminated.get("reason") or "", limit)
    last = "\n".join(log.strip().splitlines()[-8:])
    return f"marimo stopped: {why}" + (f"\n{last}" if last else "")


async def refresh(session: Session, row: NotebookSession, force: bool = False) -> NotebookSession:
    """Moves a starting session forward: ready when its pod answers, failed (saying why)
    when it cannot start. Asked at most once a second per process, unless `force`."""
    if row.status != "starting":
        return row
    clock = time.monotonic()
    if not force and clock - _checked.get(row.id, 0.0) < CHECK_EVERY:
        return row
    _checked[row.id] = clock
    try:
        job = await kube_jobs.get_job(row.namespace, row.job_name)
        if job is None:
            if _age(row.started_at) > NO_JOB_SECONDS:
                await _fail(session, row, "its Job is not in the cluster (the gateway that started it stopped)")
            return row
        if kube_jobs.job_outcome(job) is not None:
            await _fail(session, row, kube_jobs.job_failure(job) or "its Job ended before the notebook answered")
            return row
        pod = _latest(await kube_jobs.job_pods(row.namespace, row.job_name))
        if pod is not None and _ready(pod):
            row.status, row.pod_ip, row.ready_at, row.error = "ready", pod["status"]["podIP"], _now(), None
            session.add(row)
            session.commit()
            _checked.pop(row.id, None)
            return row
        if pod is not None:
            ended = await _ended(session, row, pod)
            if ended:
                await _fail(session, row, ended)
                return row
        cannot, unscheduled, _ran = await kube_jobs.start_state(row.namespace, row.job_name)
        if not cannot and not kube_jobs.has_pod(job) and _age(row.started_at) > 10:
            cannot = await kube_jobs.refused(row.namespace, row.job_name)
        if cannot:
            await _fail(session, row, cannot)
            return row
        timeout = get_settings().notebooks.start_timeout_seconds
        if _age(row.started_at) > timeout:
            why = f": no node could take it ({unscheduled})" if unscheduled else ""
            await _fail(session, row, f"it did not start within {timeout} s{why}")
    except KubeError as e:
        if e.status in (0, 429) or e.status >= 500:
            logger.warning("notebooks: session %s not checked (%s)", row.id, e)
            return row
        await _fail(session, row, str(e))
    return row


async def _fail(session: Session, row: NotebookSession, why: str) -> None:
    """The session failed: the row stays (the page shows why) until it is tried again or
    the reaper clears it; its Job goes now."""
    row.status, row.error, row.pod_ip = "failed", f"The notebook could not start: {why}"[:4000], None
    session.add(row)
    session.commit()
    _checked.pop(row.id, None)
    logger.info("notebooks: session %s of notebook %s failed: %s", row.id, row.notebook_id, why[:300])
    with contextlib.suppress(KubeError):
        await kube_jobs.delete_job(row.namespace, row.job_name)


async def alive(session: Session, row: NotebookSession) -> bool:
    """A ready session's marimo still runs (asked when the proxy cannot reach it): when it
    does not, the session is stopped — an editor's file kept as the draft first."""
    try:
        job = await kube_jobs.get_job(row.namespace, row.job_name)
        pod = _latest(await kube_jobs.job_pods(row.namespace, row.job_name)) if job else None
    except KubeError:
        return True  # the API not answering says nothing about the pod
    if job is not None and kube_jobs.job_outcome(job) is None and pod is not None \
            and not await _ended(session, row, pod):
        return True
    await stop(session, row, actor=None, reason="stopped")
    return False


# ── the editor's file ────────────────────────────────────────────────────────
async def read_file(row: NotebookSession) -> Optional[str]:
    """What marimo last saved in the editor's pod; None when it cannot be read."""
    if row.mode != "edit" or not row.pod_ip:
        return None
    url = f"http://{row.pod_ip}:{FILES_PORT}/notebook.py"
    try:
        async with httpx.AsyncClient(timeout=FILE_TIMEOUT) as c:
            async with c.stream("GET", url) as r:
                if r.status_code != 200:
                    return None
                data = bytearray()
                async for chunk in r.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_FILE_BYTES:
                        logger.warning("notebooks: the editor's file of session %s is too large", row.id)
                        return None
        return bytes(data).decode("utf-8", errors="replace")
    except httpx.HTTPError as e:
        logger.info("notebooks: the editor's file of session %s not read (%s)", row.id, type(e).__name__)
        return None


async def sync_draft(session: Session, row: NotebookSession) -> Optional[str]:
    """The editor's file becomes the notebook's draft when it differs from the current
    version (none when it is the same). Returns the file, None when it cannot be read."""
    source = await read_file(row)
    notebook = session.get(Notebook, row.notebook_id)
    if source is None or notebook is None:
        return source
    draft = None if source == notebooks.content_of(session, notebook)["source"] else source
    if draft is not None and len(draft.encode()) > notebooks.MAX_SOURCE_BYTES:
        logger.warning("notebooks: the editor's file of notebook %s is over the size of a version: not kept", notebook.id)
        return source
    if draft != notebook.draft:
        notebook.draft = draft
        notebook.draft_by = row.user_id if draft is not None else None
        notebook.draft_at = datetime.now(timezone.utc) if draft is not None else None
        session.add(notebook)
        session.commit()
    return source


async def save_version(session: Session, notebook: Notebook, user: User, note: Optional[str],
                       request: Optional[Request] = None) -> Optional[int]:
    """A new version from the file of `user`'s editor, with `user` as its author; None when
    it is the current version already. The draft goes."""
    row = row_of(session, notebook.id, "edit")
    if row is None or row.user_id != user.id or row.status == "failed":
        raise HTTPException(status_code=409, detail="You do not have the editor of this notebook open")
    if row.status != "ready":
        raise HTTPException(status_code=409, detail="The editor is still starting: try again in a moment")
    source = await read_file(row)
    if source is None:
        raise HTTPException(status_code=502, detail="The editor's file could not be read from its pod: try again")
    content = notebooks.check_content(notebook.name, {"source": source})
    touch(session, row)
    if content == notebooks.content_of(session, notebook):
        if notebook.draft is not None:
            notebook.draft = notebook.draft_by = notebook.draft_at = None
            session.add(notebook)
            session.commit()
        return None
    version = notebooks.add_version(session, notebook, content, (note or "").strip()[:500] or "edited", user)
    notebook.draft = notebook.draft_by = notebook.draft_at = None
    notebook.updated_at = datetime.now(timezone.utc)
    session.add(notebook)
    session.commit()
    audit.record_audit(session, actor=user, action=audit.NOTEBOOK_SAVE, target_type="notebook",
                       target_id=notebook.id, target_label=notebook.name, request=request,
                       detail={"version": version.version})
    return version.version


# ── stopping ─────────────────────────────────────────────────────────────────
async def stop(session: Session, row: NotebookSession, *, actor: Optional[User], reason: str,
               request: Optional[Request] = None, quiet: bool = False) -> None:
    """Stops the pod and forgets the session; an editor's file becomes the draft first."""
    if row.mode == "edit" and row.status == "ready":
        await sync_draft(session, row)
    try:
        await kube_jobs.delete_job(row.namespace, row.job_name)
    except KubeError as e:  # the reaper deletes the Job without a session later
        logger.warning("notebooks: the Job %s not deleted now (%s)", row.job_name, e)
    notebook = session.get(Notebook, row.notebook_id)
    row_id, mode, was = row.id, row.mode, row.status
    session.exec(delete(NotebookSession).where(NotebookSession.id == row_id))  # type: ignore[arg-type]
    session.commit()
    _checked.pop(row_id, None)
    if quiet or was == "failed" or notebook is None:
        return
    audit.record_audit(session, actor=actor, actor_label=None if actor else ORCHESTRATOR, action=audit.NOTEBOOK_STOP,
                       target_type="notebook", target_id=notebook.id, target_label=notebook.name, request=request,
                       detail={"mode": mode, "reason": reason})


def forget(session: Session, notebook_id: int) -> None:
    """A notebook being deleted: its sessions and visitors go with it, their Jobs with the
    reaper's next round (no session holds them any more)."""
    session.exec(delete(NotebookVisitor).where(NotebookVisitor.notebook_id == notebook_id))  # type: ignore[arg-type]
    session.exec(delete(NotebookSession).where(NotebookSession.notebook_id == notebook_id))  # type: ignore[arg-type]


# ── visitors ─────────────────────────────────────────────────────────────────
def visitors_cap(notebook: Notebook) -> int:
    """How many people may have the notebook's app open at once: its own number or the
    installation's default, never above the installation's ceiling (lowered after a
    notebook chose more, it still holds)."""
    s = get_settings().notebooks
    return max(1, min(notebook.max_visitors or s.max_visitors_per_app, s.max_visitors_ceiling))


def _fresh_since() -> datetime:
    return _now() - VISITOR_STALE


def visitors(session: Session, notebook_id: int) -> int:
    """The people with the app open now, on every replica."""
    return session.exec(select(func.count()).select_from(NotebookVisitor).where(
        NotebookVisitor.notebook_id == notebook_id, NotebookVisitor.last_seen_at >= _fresh_since())).one()


def is_visiting(session: Session, notebook_id: int, user_id: int) -> bool:
    """This person has the app open already (another tab, or the page being reloaded)."""
    return session.exec(select(NotebookVisitor.id).where(
        NotebookVisitor.notebook_id == notebook_id, NotebookVisitor.user_id == user_id,
        NotebookVisitor.last_seen_at >= _fresh_since())).first() is not None


def take_visitor(session: Session, notebook: Notebook, user_id: int) -> tuple[Optional[int], int, int]:
    """A place among the app's visitors: (the visitor's id, or None when the app is full;
    the visitors counted; the cap). Counted and taken under a lock of the notebook, so two
    replicas opening at once cannot both take the last place."""
    if session.get_bind().dialect.name == "postgresql":  # the tests' SQLite: one process
        session.exec(text("SELECT pg_advisory_xact_lock(CAST(:k AS integer), CAST(:n AS integer))")
                     .bindparams(k=VISITORS_LOCK, n=notebook.id))
    cap = visitors_cap(notebook)
    count = visitors(session, notebook.id)
    if count >= cap:
        session.rollback()
        return None, count, cap
    row = NotebookVisitor(notebook_id=notebook.id, user_id=user_id)
    session.add(row)
    session.commit()
    return row.id, count + 1, cap


def keep_visitor(session: Session, visitor_id: int) -> None:
    """The visitor's WebSocket is still open (once a minute)."""
    row = session.get(NotebookVisitor, visitor_id)
    if row is None:
        return
    row.last_seen_at = _now()
    session.add(row)
    session.commit()


def drop_visitor(session: Session, visitor_id: int) -> None:
    """The visitor's WebSocket closed: their place is free."""
    session.exec(delete(NotebookVisitor).where(NotebookVisitor.id == visitor_id))  # type: ignore[arg-type]
    session.commit()


def clear_stale_visitors(session: Session) -> int:
    """The visitors nobody refreshed (their replica died): they count no more already,
    the rows go."""
    gone = session.exec(delete(NotebookVisitor).where(  # type: ignore[arg-type]
        NotebookVisitor.last_seen_at < _fresh_since())).rowcount
    session.commit()
    return gone or 0


# ── the reaper (orchestrator role) ───────────────────────────────────────────
@contextlib.contextmanager
def _round_lock(bind: Engine):
    """One reaper at a time across the orchestrators, for the whole round (the round
    commits as it goes: a transaction's lock would not last)."""
    if bind.dialect.name != "postgresql":
        yield True
        return
    with bind.connect() as conn:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:k)").bindparams(k=LOCK)).scalar()
        conn.commit()
        try:
            yield bool(got)
        finally:
            if got:
                conn.execute(text("SELECT pg_advisory_unlock(:k)").bindparams(k=LOCK))
                conn.commit()


async def one_round(bind: Engine) -> None:
    """Stops the idle pods and those gone from the cluster, fails the ones that do not
    start, keeps the editors' files as drafts, clears the visitors nobody refreshed,
    deletes the Jobs nobody holds."""
    with _round_lock(bind) as mine:
        if not mine:
            return
        with Session(bind) as session:
            jobs: Optional[list[dict]] = None
            try:  # the Jobs first: one created after this listing has its row already
                jobs = await kube_jobs.list_jobs(namespace(), f"{LABEL_COMPONENT}={COMPONENT}")
            except KubeError as e:
                logger.warning("notebooks: the Jobs not listed (%s)", e)
            by_name = {(j.get("metadata") or {}).get("name"): j for j in jobs or []}
            idle = get_settings().notebooks.idle_minutes * 60
            for row in session.exec(select(NotebookSession).order_by(NotebookSession.id)).all():
                try:
                    await _reap(session, row, by_name if jobs is not None else None, idle)
                except KubeError as e:
                    logger.warning("notebooks: session %s not checked (%s)", row.id, e)
            clear_stale_visitors(session)
            if jobs is not None:
                held = set(session.exec(select(NotebookSession.job_name)).all())
                for name, job in by_name.items():
                    if name and name not in held and kube_jobs.job_outcome(job) is None:
                        logger.info("notebooks: the Job %s has no session: deleted", name)
                        with contextlib.suppress(KubeError):
                            await kube_jobs.delete_job(namespace(), name)


async def _reap(session: Session, row: NotebookSession, jobs: Optional[dict[str, dict]], idle: float) -> None:
    now = _now()
    if row.status == "failed":
        if _age(row.started_at, now) > idle:
            await stop(session, row, actor=None, reason="failed", quiet=True)
        return
    if row.status == "starting":
        await refresh(session, row, force=True)
        return
    job = jobs.get(row.job_name) if jobs is not None else {}
    if job is None or (job and kube_jobs.job_outcome(job) is not None):
        await stop(session, row, actor=None, reason="ended")
        return
    if _age(row.last_seen_at, now) > idle:
        await stop(session, row, actor=None, reason="idle")
        return
    if not await alive(session, row):
        return
    if row.mode == "edit":
        await sync_draft(session, row)


async def reaper_loop(stop_event: asyncio.Event) -> None:
    if disabled_reason():
        return
    from app.db.session import engine

    while not stop_event.is_set():
        try:
            await one_round(engine)
        except Exception as e:  # noqa: BLE001 — database or cluster down: at the next round
            logger.warning("notebooks: the pods not checked (%s), again in %ss", e, REAP_SECONDS)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=REAP_SECONDS)
        except asyncio.TimeoutError:
            pass
