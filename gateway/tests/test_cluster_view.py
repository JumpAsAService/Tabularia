"""Administration → Cluster (services/cluster_view.py, routes/cluster.py): what Tabularia
has running, read from the Kubernetes API.

Fixed here: CPU and memory quantities read as the API writes them; off Kubernetes the
page says why; the pods of the release's namespace and of the users' code with their
state (the reason a container waits or died), restarts, usage against limits; a Job's
pod names the flow that started it; without metrics-server everything else shows; the
warning events, newest first.
"""
import json

import httpx
import pytest

from app.core.config import get_settings
from app.models import Run
from app.services import cluster_view, kube_jobs
from tests.conftest import make_flow, make_project

pytestmark = pytest.mark.anyio


def test_quantities():
    assert cluster_view.cpu_millis("250m") == 250 and cluster_view.cpu_millis("2") == 2000
    assert cluster_view.cpu_millis("123456789n") == pytest.approx(123.456789)
    assert cluster_view.memory_bytes("512Mi") == 512 * 2**20 and cluster_view.memory_bytes("1Gi") == 2**30
    assert cluster_view.memory_bytes("1000") == 1000 and cluster_view.memory_bytes("bad") is None


def test_off_kubernetes_the_page_says_why(monkeypatch, tmp_path):
    monkeypatch.setenv("KUBERNETES__TOKEN_FILE", str(tmp_path / "none"))
    get_settings.cache_clear()
    assert cluster_view.disabled_reason() == "kubernetes"
    get_settings.cache_clear()


def _pod(name, *, phase="Running", waiting=None, restarts=0, labels=None, limits=None):
    status = {"phase": phase, "startTime": "2026-10-22T10:00:00Z",
              "containerStatuses": [{"ready": phase == "Running" and not waiting, "restartCount": restarts,
                                     "state": {"waiting": {"reason": waiting}} if waiting else {"running": {}}}]}
    return {"metadata": {"name": name, "labels": labels or {}}, "spec": {"nodeName": "node-1", "containers": [
        {"resources": {"limits": limits or {}}}]}, "status": status}


@pytest.fixture
def api(monkeypatch, tmp_path):
    (tmp_path / "token").write_text("t")
    (tmp_path / "ns").write_text("tab")
    for k, v in {"KUBERNETES__TOKEN_FILE": str(tmp_path / "token"), "KUBERNETES__NAMESPACE_FILE": str(tmp_path / "ns"),
                 "KUBERNETES__CA_FILE": "", "KUBERNETES__CLUSTER_VIEW": "true", "CONTAINERS__ENABLED": "true",
                 "CONTAINERS__NAMESPACE": "tab-containers", "CONTAINERS__TRIAL_NAMESPACE": "tab-containers"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    pages: dict[str, dict] = {}
    monkeypatch.setattr(kube_jobs, "_transport", httpx.MockTransport(
        lambda req: httpx.Response(200, json=pages[req.url.path]) if req.url.path in pages
        else httpx.Response(404, json={"message": "not found"})))
    yield pages
    get_settings.cache_clear()


async def test_the_pods_their_state_usage_and_who_started_them(session, api):
    flow = make_flow(session, name="Forecasting", project_id=make_project(session, name="p").id)
    run = Run(kind="container", flow_id=flow.id, task_id="", status="STARTED", input_key="", output_bucket="", output_key="")
    session.add(run)
    session.commit()
    api["/api/v1/namespaces/tab/pods"] = {"items": [
        _pod("tab-gateway-1", labels={"app.kubernetes.io/component": "gateway"}, limits={"cpu": "1", "memory": "1Gi"}),
        _pod("tab-worker-1", waiting="CrashLoopBackOff", restarts=7, labels={"app.kubernetes.io/component": "worker"})]}
    api["/apis/metrics.k8s.io/v1beta1/namespaces/tab/pods"] = {"items": [
        {"metadata": {"name": "tab-gateway-1"}, "containers": [{"usage": {"cpu": "250000000n", "memory": "300Mi"}}]}]}
    api["/api/v1/namespaces/tab/events"] = {"items": [
        {"involvedObject": {"kind": "Pod", "name": "tab-worker-1"}, "reason": "BackOff", "message": "Back-off restarting",
         "count": 7, "lastTimestamp": "2026-10-22T11:00:00Z"},
        {"involvedObject": {"kind": "Pod", "name": "x"}, "reason": "Old", "message": "", "lastTimestamp": "2026-10-21T11:00:00Z"}]}
    api["/api/v1/namespaces/tab-containers/pods"] = {"items": [
        _pod("tabularia-container-9-a1-x", labels={"tabularia.io/container-run": str(run.id), "tabularia.io/stage": "run"})]}
    out = await cluster_view.snapshot(session)
    tab, containers = out["namespaces"]
    assert (tab["name"], tab["role"], containers["role"]) == ("tab", "tabularia", "containers")
    gateway, worker = tab["pods"]
    assert gateway["state"] == "Running" and gateway["cpu_millis"] == pytest.approx(250)
    assert gateway["memory_bytes"] == 300 * 2**20 and gateway["cpu_limit_millis"] == 1000 and gateway["memory_limit_bytes"] == 2**30
    assert worker["state"] == "CrashLoopBackOff" and worker["restarts"] == 7 and worker["ready"] == "0/1"
    assert worker["cpu_millis"] is None and out["metrics"] is True
    job = containers["pods"][0]
    assert job["run_id"] == run.id and job["started_by"] == {"kind": "flow", "id": flow.id, "name": "Forecasting"}
    assert containers["pods"][0]["cpu_millis"] is None  # no metrics there: the rest still shows
    assert [e["reason"] for e in out["events"]] == ["BackOff", "Old"]
