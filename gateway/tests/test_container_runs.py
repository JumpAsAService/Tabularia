"""The Container node in a flow's run (services/container_runs.py, services/orchestrator.py):
a fake cluster, a fake registry, a fake engine.

Fixed here:
- the tag is resolved to its digest when the run starts, and the Pod names the digest;
- the registry's pull Secret is written in the containers' namespace, and the Pod uses it;
- the node's command, arguments, CPU and memory; the registry's variables (secret ones
  in a Secret, never in the Job nor in the run) and the node's; no root restriction
  (`baseline`), no service account token;
- input and output through the scripts bucket, as for a Script node; a node with
  neither is given no bucket credentials, and needs no bucket at all;
- retries, a failure that stops the flow, an image that cannot be resolved;
- whoever the run belongs to must still be allowed the registry and the image;
- what may be saved: a node well formed, within the installation's limits;
- a schedule set by someone else stops when the image or the command changes.
"""
import json

import httpx
import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.models import (
    ContainerRegistry, ContainerRegistryAccess, ContainerRegistryVariable, Run, ScriptStorage,
)
from app.services import container_nodes, container_registry as cr, kube_jobs, orchestrator as orch
from app.services import schedule_authority
from app.services.flow_resolver import sequence_order
from tests.conftest import make_flow, make_project, make_user
from tests.fake_kube import Cluster
from tests.test_registries import DIGEST, TOKEN, FakeRegistry
from tests.test_script_runs import OUTPUT, SECRET_KEY, SOURCE, Engine, Launches, _edge

pytestmark = pytest.mark.anyio
API_KEY = "api-s3cret-value"


@pytest.fixture
def cluster(monkeypatch, tmp_path):
    c = Cluster()
    (tmp_path / "token").write_text("sa-token\n")
    for k, v in {"KUBERNETES__TOKEN_FILE": str(tmp_path / "token"), "KUBERNETES__CA_FILE": "",
                 "CONTAINERS__ENABLED": "true", "CONTAINERS__NAMESPACE": "tab-containers",
                 "CONTAINERS__TRIAL_NAMESPACE": "tab-containers-dev", "CONTAINERS__POLL_SECONDS": "0.01",
                 "CONTAINERS__MAX_CPU": "2", "CONTAINERS__MAX_MEMORY": "4Gi"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    monkeypatch.setattr(kube_jobs, "_transport", httpx.MockTransport(c.handle))
    yield c
    get_settings.cache_clear()


@pytest.fixture
def registry(monkeypatch):
    fake = FakeRegistry()
    monkeypatch.setattr(cr, "_transport", httpx.MockTransport(fake.handle))
    return fake


@pytest.fixture
def engine(monkeypatch):
    e = Engine()
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(e.handle), base_url="http://engine"))
    return e


@pytest.fixture
def launches(monkeypatch):
    return Launches(monkeypatch)


def _registry(session, *, storage=True, enabled=True, repositories="team/*") -> ContainerRegistry:
    r = ContainerRegistry(name="Harbor", host="reg.test", username="ada", password_enc=encrypt_secret(TOKEN),
                          repositories=repositories, enabled=enabled)
    session.add(r)
    session.commit()
    session.add(ContainerRegistryVariable(registry_id=r.id, name="REGION", value="eu"))
    session.add(ContainerRegistryVariable(registry_id=r.id, name="API_KEY", secret=True,
                                          value_enc=encrypt_secret(API_KEY)))
    if storage:
        session.add(ScriptStorage(id=1, endpoint="http://minio.internal:9000", bucket="tab-scripts",
                                  access_key="scripts", secret_key_enc=encrypt_secret(SECRET_KEY)))
    session.commit()
    return r


def _container(registry_id, **data):
    return {"id": "c1", "type": "container", "data": {
        "label": "Forecast", "registryId": registry_id, "repository": "team/forecast", "tag": "v3",
        "command": ["python", "-m", "forecast"], "args": ["--days", "30"],
        "variables": [{"name": "COUNTRY", "value": "IT"}], "cpu": "500m", "memory": "1Gi",
        "timeoutSeconds": 600, **data}}


def _flow(session, nodes, edges):
    p = make_project(session, name="p")
    return make_flow(session, name="forecasting", project_id=p.id,
                     definition=json.dumps({"nodes": nodes, "edges": edges}))


async def _run(session, user, flow):
    head = Run(kind="orchestration", flow_id=flow.id, task_id="", status="STARTED", input_key="", output_bucket="",
               output_key="")
    session.add(head)
    session.commit()
    errors = await orch.orchestrate(session, user, flow, parent_run_id=head.id, engine_mode="production")
    return head, errors


def _container_run(session) -> Run:
    return session.exec(select(Run).where(Run.kind == "container")).one()


def _job(cluster) -> dict:
    return next(b for k, _, b in cluster.created if k == "jobs")


async def test_a_container_between_a_chain_and_an_output(session, cluster, registry, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    reg = _registry(session)
    flow = _flow(session, [OUTPUT, SOURCE, _container(reg.id)], [_edge("src", "c1"), _edge("c1", "out")])
    head, errors = await _run(session, admin, flow)
    assert errors == []
    # the input: the chain before the node, in the scripts bucket under the registry's prefix
    what, body, kw = launches.bodies[0]
    assert what == "script_input" and body["input_key"] == "datasets/sales.parquet"
    key = kw["script_destination"][0]["target"]["key"]
    assert key.startswith(f"container-{reg.id}/") and key.endswith("/input.parquet")
    # the pull Secret first, in the containers' namespace; then the Job and its Secret
    assert [(k, ns) for k, ns, _ in cluster.created] == [
        ("secrets", "tab-containers"), ("jobs", "tab-containers"), ("secrets", "tab-containers")]
    pull = cluster.created[0][2]
    assert pull["metadata"]["name"] == f"tabularia-registry-{reg.id}" and pull["type"] == "kubernetes.io/dockerconfigjson"
    job = _job(cluster)
    pod = job["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert c["image"] == f"reg.test/team/forecast@{DIGEST}" and c["imagePullPolicy"] == "IfNotPresent"
    assert c["command"] == ["python", "-m", "forecast"] and c["args"] == ["--days", "30"]
    assert c["resources"] == {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"cpu": "500m", "memory": "1Gi"}}
    assert pod["imagePullSecrets"] == [{"name": f"tabularia-registry-{reg.id}"}]
    assert pod["automountServiceAccountToken"] is False and "runAsNonRoot" not in pod["securityContext"]
    assert c["securityContext"] == {"allowPrivilegeEscalation": False}
    env = {e["name"]: e["value"] for e in c["env"]}
    step = key.split("/")[1]
    assert env["TABULARIA_INPUT"] == f"s3://tab-scripts/container-{reg.id}/{step}/input.parquet"
    assert env["TABULARIA_OUTPUT"] == f"s3://tab-scripts/container-{reg.id}/{step}/output.parquet"
    assert env["REGION"] == "eu" and env["COUNTRY"] == "IT" and env["AWS_ENDPOINT_URL"] == "http://minio.internal:9000"
    assert env["TABULARIA_RUN_ID"] == str(head.id) and env["TABULARIA_ATTEMPT"] == "1"
    assert "API_KEY" not in env and API_KEY not in json.dumps(job) and SECRET_KEY not in json.dumps(job)
    secret = cluster.created[2][2]
    assert secret["stringData"] == {"AWS_ACCESS_KEY_ID": "scripts", "AWS_SECRET_ACCESS_KEY": SECRET_KEY, "API_KEY": API_KEY}
    assert c["envFrom"] == [{"secretRef": {"name": secret["metadata"]["name"]}}]
    assert secret["metadata"]["ownerReferences"][0]["uid"] == job["metadata"]["uid"]
    # the Job is deleted, its Secret with it; the pull Secret stays for the next runs
    assert list(cluster.objects) == [("secrets", "tab-containers", f"tabularia-registry-{reg.id}")]
    # the output, read by the Output after the node
    imp = engine.imports[0]
    assert imp["key"] == f"container-{reg.id}/{step}/output.parquet"
    assert launches.bodies[1][0] == "output" and launches.bodies[1][1]["input_key"] == imp["output_key"]
    assert engine.deletes[0]["keys"] == [f"container-{reg.id}/{step}/input.parquet",
                                         f"container-{reg.id}/{step}/output.parquet"]
    run = _container_run(session)
    info = json.loads(run.script)
    assert run.status == "SUCCESS" and run.parent_run_id == head.id and run.rows_written == 42
    assert info["digest"] == DIGEST and info["reference"] == "v3" and info["repository"] == "team/forecast"
    assert info["attempts"] == 1 and info["exit_code"] == 0 and info["namespace"] == "tab-containers"
    assert info["variables"]["REGION"] == {"value": "eu"} and info["variables"]["API_KEY"]["secret"] is True
    assert info["variables"]["COUNTRY"] == {"value": "IT", "node": True} and API_KEY not in run.script


async def test_a_container_with_no_input_and_no_output_gets_no_bucket(session, cluster, registry, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    reg = _registry(session, storage=False)  # and needs none
    flow = _flow(session, [_container(reg.id, command=[], args=[], variables=[])], [])
    _, errors = await _run(session, admin, flow)
    assert errors == [] and launches.bodies == [] and engine.imports == [] and engine.deletes == []
    c = _job(cluster)["spec"]["template"]["spec"]["containers"][0]
    assert "command" not in c and "args" not in c  # the image's own
    env = {e["name"]: e["value"] for e in c["env"]}
    assert env["TABULARIA_INPUT"] == "" and env["TABULARIA_OUTPUT"] == "" and not any(k.startswith("AWS_") for k in env)
    secret = [b for k, _, b in cluster.created if k == "secrets"][-1]
    assert secret["stringData"] == {"API_KEY": API_KEY}
    assert _container_run(session).status == "SUCCESS"


async def test_an_input_without_the_scripts_bucket_says_so(session, cluster, registry, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    reg = _registry(session, storage=False)
    flow = _flow(session, [SOURCE, _container(reg.id)], [_edge("src", "c1")])
    with pytest.raises(orch.OrchestrationError, match="scripts bucket, which is not configured"):
        await _run(session, admin, flow)
    assert cluster.created == []


async def test_a_registry_without_credentials_has_no_pull_secret(session, cluster, registry, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    reg = _registry(session)
    reg.username, reg.password_enc = "", None
    session.add(reg)
    session.commit()
    registry.tags["library/busybox"] = ["1.36"]
    registry.scheme = "basic"
    registry._user_ok = lambda req: True  # an anonymous registry
    reg.repositories = "*"
    session.add(reg)
    session.commit()
    _, errors = await _run(session, admin, _flow(session, [_container(reg.id, repository="library/busybox",
                                                                      tag="1.36")], []))
    assert errors == [] and "imagePullSecrets" not in _job(cluster)["spec"]["template"]["spec"]


async def test_a_container_out_of_memory_says_so_with_its_limit(session, cluster, registry, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    reg = _registry(session)
    cluster.behave = lambda job: ("failed", "", 137)
    cluster.ended = "OOMKilled"
    with pytest.raises(orch.OrchestrationError, match=r"container «Forecast» failed: it ran out of memory: it went "
                                                      r"over its memory limit \(1Gi\)"):
        await _run(session, admin, _flow(session, [_container(reg.id)], []))


async def test_a_failure_is_retried_then_stops_the_flow(session, cluster, registry, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    reg = _registry(session)
    cluster.behave = lambda job: ("failed", "Traceback: boom", 2)
    flow = _flow(session, [_container(reg.id, retries=1, retryDelaySeconds=0), OUTPUT], [_edge("c1", "out")])
    with pytest.raises(orch.OrchestrationError, match="container «Forecast» failed: it ended with exit code 2"):
        await _run(session, admin, flow)
    run = _container_run(session)
    info = json.loads(run.script)
    assert run.status == "FAILURE" and info["attempts"] == 2 and "── attempt 2 ──\nTraceback: boom" in info["log"]
    assert [b["metadata"]["name"] for k, _, b in cluster.created if k == "jobs"] == [
        f"tabularia-container-{run.id}-a1", f"tabularia-container-{run.id}-a2"]
    # the Output never ran; what attempt 1 wrote went before attempt 2, the step cleaned up at the end
    assert launches.bodies == [] and len(engine.deletes) == 2
    assert [k.rsplit("/", 1)[1] for k in engine.deletes[0]["keys"]] == ["output.parquet"]


async def test_a_tag_that_is_not_there(session, cluster, registry, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    reg = _registry(session)
    with pytest.raises(orch.OrchestrationError, match="could not be resolved: team/forecast has no tag «v99»"):
        await _run(session, admin, _flow(session, [_container(reg.id, tag="v99")], []))
    assert cluster.created == []


async def test_a_node_may_not_redefine_a_registry_variable(session, cluster, registry, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    reg = _registry(session)
    flow = _flow(session, [_container(reg.id, variables=[{"name": "REGION", "value": "us"}])], [])
    with pytest.raises(orch.OrchestrationError, match="REGION is set by the registry «Harbor»"):
        await _run(session, admin, flow)


@pytest.mark.parametrize("change, message", [
    ({"enabled": False}, "is not in use"),
    ({"repositories": "other/*"}, "may not run team/forecast"),
])
async def test_a_registry_that_cannot_run_it(session, cluster, registry, engine, launches, change, message):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    reg = _registry(session, **change)
    with pytest.raises(orch.OrchestrationError, match=message):
        await _run(session, admin, _flow(session, [_container(reg.id)], []))


async def test_whoever_runs_it_must_be_allowed_the_image(session, cluster, registry, engine, launches):
    reg = _registry(session)
    ana = make_user(session, email="ana@x.local")
    session.add(ContainerRegistryAccess(registry_id=reg.id, user_id=ana.id, repositories="team/other"))
    session.commit()
    with pytest.raises(orch.OrchestrationError, match="ana@x.local may not run team/forecast"):
        await _run(session, ana, _flow(session, [_container(reg.id)], []))


async def test_containers_off(session, cluster, registry, engine, launches, monkeypatch):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    reg = _registry(session)
    monkeypatch.setenv("CONTAINERS__ENABLED", "false")
    get_settings.cache_clear()
    with pytest.raises(orch.OrchestrationError, match="containers are off"):
        await _run(session, admin, _flow(session, [_container(reg.id)], []))


def test_a_node_after_a_container_runs_after_it():
    definition = {"nodes": [OUTPUT, {"id": "c1", "type": "container", "data": {}}],
                  "edges": [_edge("c1", "out")]}
    assert [n["id"] for n in sequence_order(definition)] == ["c1", "out"]


@pytest.mark.parametrize("change, message", [
    ({"cpu": "3"}, "at most 2 CPU"),
    ({"cpu": "a lot"}, "not a CPU quantity"),
    ({"memory": "8Gi"}, "at most 4Gi of memory"),
    ({"tag": "v 1"}, "not a tag"),
    ({"repository": "Team/Forecast"}, "not a repository name"),
    ({"command": "python -m forecast"}, "a list of texts"),
    ({"variables": [{"name": "AWS_REGION", "value": "x"}]}, "set by Tabularia"),
    ({"variables": [{"name": "lower", "value": "x"}]}, "not a variable name"),
    ({"variables": [{"name": "A", "value": "1"}, {"name": "A", "value": "2"}]}, "there twice"),
    ({"timeoutSeconds": 5}, "between 10 and"),
    ({"retries": 9}, "between 0 and 5"),
])
def test_what_a_node_may_hold(cluster, change, message):
    with pytest.raises(HTTPException) as e:
        container_nodes.check(_container(1, **change), complete=True)
    assert e.value.status_code == 422 and message in e.value.detail


def test_a_node_still_being_written_is_saved(cluster):
    container_nodes.check({"id": "c1", "type": "container", "data": {"label": "New"}}, complete=False)
    with pytest.raises(HTTPException, match="choose its registry"):
        container_nodes.check({"id": "c1", "type": "container", "data": {"label": "New"}}, complete=True)


def test_another_image_or_command_widens_a_schedule():
    def definition(**data):
        return json.dumps({"nodes": [_container(1, **data)], "edges": []})

    assert ("registry", 1) in schedule_authority.reach_of(definition())
    assert schedule_authority.widens(definition(), definition()) == set()
    for change in ({"tag": "v4"}, {"repository": "team/other"}, {"args": ["--days", "90"]},
                   {"variables": [{"name": "COUNTRY", "value": "FR"}]}):
        assert any(k == "container" for k, _ in schedule_authority.widens(definition(), definition(**change)))
    assert schedule_authority.widens(definition(), definition(cpu="1")) == set()  # resources are not reach


# ── the editor's «Run» of a Container node ───────────────────────────────────
@pytest.fixture
def trial_env(session, cluster, engine, launches, monkeypatch):
    """run_trial opens its own session on the application's engine: the test's here."""
    import app.db.session as db_session
    import app.routes.runs as runs_routes

    monkeypatch.setattr(db_session, "engine", session.get_bind())
    monkeypatch.setattr(runs_routes, "_launch_flow_run", orch._launch_flow_run)
    return session


async def test_a_trial_runs_in_the_trials_namespace_and_keeps_its_output(trial_env, cluster, registry, engine, launches):
    from app.services import script_runs

    session = trial_env
    admin = make_user(session, email="a@x.local", is_superuser=True)
    reg = _registry(session)
    flow = _flow(session, [_container(reg.id)], [])
    run = Run(kind="container", flow_id=flow.id, task_id="", status="PENDING", launched_by=admin.id,
              trigger_type="trial", input_key="", output_bucket="", output_key="",
              script=json.dumps({"node_id": "c1", "trial": True}))
    session.add(run)
    session.commit()
    await script_runs.run_trial(run.id, admin.id, "c1")
    assert {ns for _, ns, _ in cluster.created} == {"tab-containers-dev"}
    session.expire_all()
    run = session.get(Run, run.id)
    assert run.status == "SUCCESS" and json.loads(run.script)["trial"] is True
    # with no node after it, a trial still asks for the output: the editor previews it
    out = script_runs.latest_outputs(session, flow.id)["c1"]
    assert out["output"]["key"] == run.output_key and out["output"]["rows"] == 42


def test_the_trial_route_and_the_outputs(trial_env, cluster, registry, engine, launches, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.security import hash_password
    from app.db.session import get_session
    from app.models import AuditLog
    from app.routes import auth as auth_routes
    from app.routes import scripting as scripting_routes
    from app.services import script_runs

    session = trial_env
    started: list = []

    async def noop(*args):
        started.append(args)

    monkeypatch.setattr(script_runs, "run_trial", noop)
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(scripting_routes.editor_router)
    app.dependency_overrides[get_session] = lambda: session
    make_user(session, email="a@x.local", hashed_password=hash_password("una-password-lunga"), is_superuser=True)
    c = TestClient(app)
    csrf = {"X-Tabularia-Csrf": "1"}
    c.post("/auth/login", json={"email": "a@x.local", "password": "una-password-lunga"}, headers=csrf)
    reg_id = _registry(session).id
    flow = _flow(session, [_container(reg_id), {"id": "c2", "type": "container", "data": {"label": "Empty"}}], [])
    flow_id = flow.id
    r = c.post(f"/scripting/flows/{flow_id}/nodes/c1/run", headers=csrf)
    assert r.status_code == 202 and started
    assert session.get(Run, r.json()["run_id"]).kind == "container"
    assert c.post(f"/scripting/flows/{flow_id}/nodes/c1/run", headers=csrf).json()["run_id"] == r.json()["run_id"]
    assert c.post(f"/scripting/flows/{flow_id}/nodes/c2/run", headers=csrf).status_code == 422
    audit = session.exec(select(AuditLog).where(AuditLog.action == "container.trial")).one()
    assert json.loads(audit.detail)["image"] == "team/forecast"
    run = session.get(Run, r.json()["run_id"])
    run.status, run.output_key, run.output_bucket = "SUCCESS", "out/t.parquet", "b"
    run.script = json.dumps({"node_id": "c1", "trial": True, "fingerprint": container_nodes.fingerprint(_container(reg_id))})
    session.add(run)
    session.commit()
    out = c.get(f"/scripting/flows/{flow_id}/outputs").json()
    assert out["c1"]["output"]["key"] == "out/t.parquet" and out["c1"]["output"]["stale"] is False
    flow = session.get(script_runs.Flow, flow_id)
    flow.definition = json.dumps({"nodes": [_container(reg_id, tag="v4")], "edges": []})
    session.add(flow)
    session.commit()
    assert c.get(f"/scripting/flows/{flow_id}/outputs").json()["c1"]["output"]["stale"] is True


async def test_an_orphaned_container_run_is_closed_and_its_jobs_go(session, cluster):
    from app.services import script_runs

    head = Run(kind="orchestration", task_id="", status="FAILURE", input_key="", output_bucket="", output_key="")
    session.add(head)
    session.commit()
    run = Run(kind="container", task_id="", status="STARTED", input_key="", output_bucket="", output_key="",
              parent_run_id=head.id, script=json.dumps({"node_id": "c1", "namespace": "tab-containers"}))
    session.add(run)
    session.commit()
    cluster.objects[("jobs", "tab-containers", "j")] = {"metadata": {"name": "j", "labels": {
        "tabularia.io/container-run": str(run.id)}}}
    run = await script_runs.close_if_orphaned(session, run)
    assert run.status == "FAILURE" and "before the container did" in run.error and cluster.objects == {}
