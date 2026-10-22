"""The Script node in a flow's run (services/script_runs.py, services/orchestrator.py):
a fake cluster and a fake engine.

Fixed here:
- input and output are optional: a node with both, with neither, with only one;
- the input is the chain before the node, written by the engine to the scripts
  bucket; the nodes after the node read what it wrote, copied into Tabularia's
  storage; a node downstream runs after the script even without a sequence edge;
- the Job: the environment's image by digest, the code mounted with its folders, the
  bucket's key and the secret variables in a Secret (not in the Job), TABULARIA_*;
- retries, a failure that stops the flow, an output that was not written;
- the step's files deleted from the scripts bucket whatever happens;
- the run records attempts, exit code, log and variables, never a secret's value;
- what may be saved: who may write a script, the path check, a node still empty;
- a schedule set by someone else stops when the code changes.
"""
import json

import httpx
import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.core.config import get_settings
from app.core.crypto import encrypt_secret
from app.models import (
    Group, Run, ScriptEnvironment, ScriptEnvironmentGroup, ScriptEnvironmentVariable, ScriptEnvironmentVersion,
    ScriptStorage, UserGroupLink,
)
from app.services import kube_jobs, orchestrator as orch, schedule_authority, script_nodes, script_runs
from app.services.flow_resolver import sequence_order
from tests.conftest import make_flow, make_project, make_user
from tests.fake_kube import Cluster

pytestmark = pytest.mark.anyio
SECRET_KEY = "bucket-s3cret-key"
API_KEY = "api-s3cret-value"
DIGEST = "registry.example.com/tab/tabularia-env-1@sha256:" + "ab" * 32


@pytest.fixture
def cluster(monkeypatch, tmp_path):
    c = Cluster()
    (tmp_path / "token").write_text("sa-token\n")
    for k, v in {"KUBERNETES__TOKEN_FILE": str(tmp_path / "token"), "KUBERNETES__CA_FILE": "",
                 "SCRIPTS__ENABLED": "true", "SCRIPTS__NAMESPACE": "tab-scripts",
                 "SCRIPTS__REGISTRY": "registry.example.com/tab", "SCRIPTS__POLL_SECONDS": "0.01",
                 "SCRIPTS__REGISTRY_PULL_SECRET": "registry-pull"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    monkeypatch.setattr(kube_jobs, "_transport", httpx.MockTransport(c.handle))
    yield c
    get_settings.cache_clear()


class Engine:
    """The engine's /scripts endpoints."""

    def __init__(self):
        self.imports: list[dict] = []
        self.deletes: list[dict] = []
        self.found = True

    def handle(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if req.url.path == "/scripts/output/import":
            self.imports.append(body)
            return httpx.Response(200, json={"found": True, "rows": 42, "bytes": 1000} if self.found else {"found": False})
        if req.url.path == "/scripts/objects/delete":
            self.deletes.append(body)
            return httpx.Response(200, json={"deleted": len(body["keys"]), "failed": []})
        return httpx.Response(404, json={"detail": "not mocked"})


@pytest.fixture
def engine(monkeypatch):
    e = Engine()
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(e.handle), base_url="http://engine"))
    return e


class Launches:
    """The orchestrator's way of running a chain on the engine, recorded."""

    def __init__(self, monkeypatch):
        self.bodies: list[tuple[str, dict, dict]] = []  # (what, body, kwargs)
        self.fail = set()

        async def launch(session, user, flow, body, **kw):
            what = "script_input" if kw.get("script_destination") else "output"
            self.bodies.append((what, body.model_dump(), kw))
            run = Run(flow_id=flow.id, status="PENDING", kind="flow", task_id=what, input_key=body.input_key,
                      output_bucket="b", output_key=f"out/{what}-{len(self.bodies)}.parquet",
                      parent_run_id=kw.get("parent_run_id"))
            session.add(run)
            session.commit()
            session.refresh(run)
            return run

        async def wait(session, run):
            run.status = "FAILURE" if run.task_id in self.fail else "SUCCESS"
            run.error = "engine refused" if run.status == "FAILURE" else None
            return run

        monkeypatch.setattr(orch, "_launch_flow_run", launch)
        monkeypatch.setattr(orch, "_wait_run", wait)
        monkeypatch.setattr(orch, "resolve_output_request",
                            lambda definition, node, resolve_ds, default_bucket, engine_mode, script_outputs=None:
                            _resolve(definition, node, resolve_ds, default_bucket, engine_mode, script_outputs))


def _resolve(*args):
    from app.services.flow_resolver import resolve_output_request

    return resolve_output_request(*args)


@pytest.fixture
def launches(monkeypatch):
    return Launches(monkeypatch)


def _environment(session, *, language="python", ready=True, enabled=True, groups=()):
    env = ScriptEnvironment(name=f"{language} models", language=language, enabled=enabled, max_timeout_seconds=1800)
    session.add(env)
    session.commit()
    v = ScriptEnvironmentVersion(environment_id=env.id, number=1, language_version="3.12", base_image="python:3.12-slim",
                                 status="ready" if ready else "failed", image_ref=DIGEST if ready else None)
    session.add(v)
    session.commit()
    env.current_version_id = v.id if ready else None
    session.add(env)
    for g in groups:
        session.add(ScriptEnvironmentGroup(environment_id=env.id, group_id=g))
    session.add(ScriptEnvironmentVariable(environment_id=env.id, name="THRESHOLD", value="100"))
    session.add(ScriptEnvironmentVariable(environment_id=env.id, name="API_KEY", secret=True,
                                          value_enc=encrypt_secret(API_KEY)))
    session.add(ScriptStorage(id=1, endpoint="http://minio.internal:9000", bucket="tab-scripts", access_key="scripts",
                              secret_key_enc=encrypt_secret(SECRET_KEY)))
    session.commit()
    return env


def _script(env_id, **data):
    return {"id": "s1", "type": "script", "data": {
        "label": "Score", "environmentId": env_id, "entrypoint": "main.py", "timeoutSeconds": 600,
        "files": [{"path": "main.py", "content": "import lib.scoring\nprint('ok')\n"},
                  {"path": "lib/scoring.py", "content": "def score(x):\n    return x * 2\n"}], **data}}


SOURCE = {"id": "src", "type": "source", "data": {"parquetKey": "datasets/sales.parquet", "bucket": "b"}}
FILTER = {"id": "f", "type": "operation", "data": {"opType": "filter", "params": {"column": "x"}}}
OUTPUT = {"id": "out", "type": "output", "data": {"destType": "datasource", "name": "scored", "projectId": 1}}


def _edge(s, t):
    return {"id": f"{s}-{t}", "source": s, "target": t, "sourceHandle": "right", "targetHandle": "left"}


def _flow(session, nodes, edges):
    p = make_project(session, name="p")
    return make_flow(session, name="scoring", project_id=p.id, definition=json.dumps({"nodes": nodes, "edges": edges}))


async def _run(session, user, flow):
    head = Run(kind="orchestration", flow_id=flow.id, task_id="", status="STARTED", input_key="", output_bucket="",
               output_key="")
    session.add(head)
    session.commit()
    errors = await orch.orchestrate(session, user, flow, parent_run_id=head.id, engine_mode="production")
    return head, errors


def _script_run(session) -> Run:
    return session.exec(select(Run).where(Run.kind == "script")).one()


async def test_a_script_between_a_chain_and_an_output(session, cluster, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    flow = _flow(session, [OUTPUT, SOURCE, FILTER, _script(env.id)],  # the Output declared first
                 [_edge("src", "f"), _edge("f", "s1"), _edge("s1", "out")])
    head, errors = await _run(session, admin, flow)
    assert errors == []
    # 1. the input: the chain up to the node, written to the scripts bucket
    what, body, kw = launches.bodies[0]
    assert what == "script_input" and body["input_key"] == "datasets/sales.parquet"
    assert [op["type"] for op in body["operations"]] == ["filter"]
    target, summary = kw["script_destination"]
    assert target["type"] == "s3" and target["target"]["bucket"] == "tab-scripts" and target["target"]["format"] == "parquet"
    key = target["target"]["key"]
    assert key.startswith(f"env-{env.id}/") and key.endswith("/input.parquet")
    assert target["connection"]["secret_key_encrypted"] and SECRET_KEY not in json.dumps(target)
    # 2. the Job, its code and its Secret
    kinds = [k for k, _, _ in cluster.created]
    assert kinds == ["jobs", "configmaps", "secrets"]
    job = cluster.created[0][2]
    pod = job["spec"]["template"]["spec"]
    c = pod["containers"][0]
    assert c["image"] == DIGEST and c["command"] == ["python", "-P", "main.py"] and c["workingDir"] == "/tabularia/code"
    env_vars = {e["name"]: e["value"] for e in c["env"]}
    step = key.split("/")[1]
    assert env_vars["TABULARIA_INPUT"] == f"s3://tab-scripts/env-{env.id}/{step}/input.parquet"
    assert env_vars["TABULARIA_OUTPUT"] == f"s3://tab-scripts/env-{env.id}/{step}/output.parquet"
    # the modules come from the code's folder as mounted, not from Kubernetes' dated folder behind it
    assert env_vars["PYTHONPATH"] == "/tabularia/code"
    assert env_vars["THRESHOLD"] == "100" and env_vars["AWS_ENDPOINT_URL"] == "http://minio.internal:9000"
    assert env_vars["AWS_ALLOW_HTTP"] == "true" and env_vars["TABULARIA_RUN_ID"] == str(head.id)
    assert "API_KEY" not in env_vars and "AWS_SECRET_ACCESS_KEY" not in env_vars and SECRET_KEY not in json.dumps(job)
    assert job["spec"]["activeDeadlineSeconds"] == 600 and pod["securityContext"]["runAsNonRoot"] is True
    assert c["securityContext"]["readOnlyRootFilesystem"] is True and pod["imagePullSecrets"] == [{"name": "registry-pull"}]
    code = cluster.created[1][2]
    items = pod["volumes"][0]["configMap"]["items"]
    assert {i["path"] for i in items} == {"main.py", "lib/scoring.py"}
    assert {code["data"][i["key"]] for i in items if i["path"] == "lib/scoring.py"} == {"def score(x):\n    return x * 2\n"}
    secret = cluster.created[2][2]
    assert secret["stringData"] == {"AWS_ACCESS_KEY_ID": "scripts", "AWS_SECRET_ACCESS_KEY": SECRET_KEY, "API_KEY": API_KEY}
    assert code["metadata"]["ownerReferences"][0]["uid"] == job["metadata"]["uid"]
    assert cluster.objects == {}  # the Job is deleted, its code and Secret with it
    # 3. the output: copied into Tabularia's storage, read by the Output after the node
    imp = engine.imports[0]
    assert imp["key"] == f"env-{env.id}/{step}/output.parquet" and imp["output_key"].startswith("out/")
    what, body, _ = launches.bodies[1]
    assert what == "output" and body["input_key"] == imp["output_key"] and body["operations"] == []
    # 4. both files of the step are deleted
    assert engine.deletes[0]["keys"] == [f"env-{env.id}/{step}/input.parquet", f"env-{env.id}/{step}/output.parquet"]
    run = _script_run(session)
    info = json.loads(run.script)
    assert run.status == "SUCCESS" and run.parent_run_id == head.id and run.rows_written == 42
    assert run.output_key == imp["output_key"] and info["attempts"] == 1 and info["exit_code"] == 0
    assert info["variables"]["THRESHOLD"] == {"value": "100"} and info["variables"]["API_KEY"]["secret"] is True
    assert API_KEY not in run.script and info["files"] == ["main.py", "lib/scoring.py"]


async def test_a_script_with_no_input_and_no_output_only_runs(session, cluster, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    flow = _flow(session, [_script(env.id)], [])
    _, errors = await _run(session, admin, flow)
    assert errors == [] and launches.bodies == [] and engine.imports == []
    env_vars = {e["name"]: e["value"] for e in cluster.created[0][2]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env_vars["TABULARIA_INPUT"] == "" and env_vars["TABULARIA_OUTPUT"] == ""
    assert _script_run(session).status == "SUCCESS"


async def test_a_retry_after_a_failure(session, cluster, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    cluster.behave = lambda job: (("failed", "Traceback: boom", 1) if job["metadata"]["name"].endswith("-a1")
                                  else ("succeeded", "done", 0))
    flow = _flow(session, [_script(env.id, retries=2, retryDelaySeconds=0)], [])
    _, errors = await _run(session, admin, flow)
    info = json.loads(_script_run(session).script)
    assert errors == [] and info["attempts"] == 2 and info["exit_code"] == 0
    assert "── attempt 1 ──\nTraceback: boom" in info["log"] and "── attempt 2 ──\ndone" in info["log"]
    names = [b["metadata"]["name"] for k, _, b in cluster.created if k == "jobs"]
    assert names == [f"tabularia-script-{_script_run(session).id}-a1", f"tabularia-script-{_script_run(session).id}-a2"]


async def test_a_failing_script_stops_the_flow_and_cleans_up(session, cluster, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    cluster.behave = lambda job: ("failed", "ZeroDivisionError", 1)
    flow = _flow(session, [SOURCE, _script(env.id), OUTPUT], [_edge("src", "s1"), _edge("s1", "out")])
    with pytest.raises(orch.OrchestrationError, match="script «Score» failed: it ended with exit code 1"):
        await _run(session, admin, flow)
    assert [w for w, _, _ in launches.bodies] == ["script_input"]  # the Output never ran
    run = _script_run(session)
    assert run.status == "FAILURE" and "exit code 1" in run.error and "ZeroDivisionError" in json.loads(run.script)["log"]
    assert len(engine.deletes) == 1 and cluster.objects == {}


async def test_a_timeout_says_so(session, cluster, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    cluster.behave = lambda job: ("failed", "", None)  # the deadline: the container never ended
    with pytest.raises(orch.OrchestrationError, match="longer than its timeout"):
        await _run(session, admin, _flow(session, [_script(env.id)], []))


async def test_a_script_that_writes_no_output(session, cluster, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    engine.found = False
    flow = _flow(session, [_script(env.id), OUTPUT], [_edge("s1", "out")])
    with pytest.raises(orch.OrchestrationError, match="did not write its output"):
        await _run(session, admin, flow)
    assert launches.bodies == [] and len(engine.deletes) == 1


async def test_an_input_that_cannot_be_prepared(session, cluster, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    launches.fail.add("script_input")
    flow = _flow(session, [SOURCE, _script(env.id)], [_edge("src", "s1")])
    with pytest.raises(orch.OrchestrationError, match="input of script «Score» could not be prepared"):
        await _run(session, admin, flow)
    assert cluster.created == []


@pytest.mark.parametrize("change, message", [
    ({"enabled": False}, "is disabled"),
    ({"ready": False}, "no version built yet"),
])
async def test_an_environment_that_cannot_run(session, cluster, engine, launches, change, message):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session, **change)
    with pytest.raises(orch.OrchestrationError, match=message):
        await _run(session, admin, _flow(session, [_script(env.id)], []))


async def test_whoever_runs_it_must_be_allowed_to_use_the_environment(session, cluster, engine, launches):
    g = Group(name="data science")
    session.add(g)
    session.commit()
    env = _environment(session, groups=[g.id])
    outsider = make_user(session, email="o@x.local")
    with pytest.raises(orch.OrchestrationError, match="cannot run scripts"):
        await _run(session, outsider, _flow(session, [_script(env.id)], []))
    for head in session.exec(select(Run).where(Run.kind == "orchestration")).all():
        head.status = "FAILURE"  # one live run per flow
        session.add(head)
    member = make_user(session, email="m@x.local")
    session.add(UserGroupLink(user_id=member.id, group_id=g.id))
    session.commit()
    flow = session.exec(select(script_runs.Flow)).first()
    _, errors = await _run(session, member, flow)
    assert errors == []


def test_downstream_nodes_run_after_the_script_without_a_sequence_edge():
    definition = {"nodes": [OUTPUT, SOURCE, _script(1)], "edges": [_edge("src", "s1"), _edge("s1", "out")]}
    assert [n["id"] for n in sequence_order(definition)] == ["s1", "out"]


# ── saving ───────────────────────────────────────────────────────────────────
def test_who_may_save_a_script(session, cluster):
    g = Group(name="data science")
    session.add(g)
    session.commit()
    env = _environment(session, groups=[g.id])
    member = make_user(session, email="m@x.local")
    outsider = make_user(session, email="o@x.local")
    session.add(UserGroupLink(user_id=member.id, group_id=g.id))
    session.commit()
    new = json.dumps({"nodes": [_script(env.id)], "edges": []})
    script_nodes.check_definition(session, member, None, new)
    with pytest.raises(HTTPException) as e:
        script_nodes.check_definition(session, outsider, None, new)
    assert e.value.status_code == 403
    # an unchanged script saved by someone else (who moved another node) is fine
    moved = json.dumps({"nodes": [_script(env.id), {"id": "x", "type": "comment", "data": {}}], "edges": []})
    script_nodes.check_definition(session, outsider, new, moved)
    # a node just dropped on the canvas, with nothing in it yet
    empty = json.dumps({"nodes": [{"id": "s2", "type": "script", "data": {"label": "Script"}}], "edges": []})
    script_nodes.check_definition(session, outsider, None, empty)


@pytest.mark.parametrize("files, entry, message", [
    ([{"path": "../etc/passwd", "content": ""}], "main.py", "is not a file name"),
    ([{"path": "/abs.py", "content": ""}], "/abs.py", "is not a file name"),
    ([{"path": "main.py", "content": "pl.scan_parquet('s3://tab-scripts/env-1/other/input.parquet')"}], "main.py",
     "main.py, line 1 names the scripts bucket"),
    ([{"path": "main.py", "content": "x = 1\nkey = 'tab-scripts/env-2/x'"}], "main.py", "line 2 names the scripts bucket"),
    ([{"path": "a.py", "content": ""}, {"path": "A.py", "content": ""}], "a.py", "two files are called"),
])
def test_the_files_and_the_path_check(session, cluster, files, entry, message):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    bad = json.dumps({"nodes": [_script(env.id, files=files, entrypoint=entry)], "edges": []})
    with pytest.raises(HTTPException, match=message) as e:
        script_nodes.check_definition(session, admin, None, bad)
    assert e.value.status_code == 422


def test_reading_its_own_input_and_other_buckets_is_fine(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    code = ("import os, polars as pl\n"
            "df = pl.scan_parquet(os.environ['TABULARIA_INPUT'])\n"
            "ref = pl.read_parquet('s3://company-reference/regions.parquet')\n")
    ok = json.dumps({"nodes": [_script(env.id, files=[{"path": "main.py", "content": code}])], "edges": []})
    script_nodes.check_definition(session, admin, None, ok)


async def test_the_run_refuses_an_incomplete_node(session, cluster, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    node = _script(env.id, entrypoint="missing.py")
    with pytest.raises(orch.OrchestrationError, match="the file to run"):
        await _run(session, admin, _flow(session, [node], []))


def test_a_changed_script_widens_a_schedule():
    before = {"nodes": [_script(1)], "edges": []}
    after = {"nodes": [_script(1, files=[{"path": "main.py", "content": "print('other')"}])], "edges": []}
    assert any(kind == "script" for kind, _ in schedule_authority.widens(before, after))
    assert schedule_authority.widens(before, before) == set()
    assert ("environment", 1) in schedule_authority.reach_of(before)


async def test_an_orphaned_script_run_is_closed(session, cluster):
    head = Run(kind="orchestration", task_id="", status="FAILURE", input_key="", output_bucket="", output_key="")
    session.add(head)
    session.commit()
    run = Run(kind="script", task_id="", status="STARTED", parent_run_id=head.id, input_key="", output_bucket="",
              output_key="")
    session.add(run)
    session.commit()
    run = await script_runs.close_if_orphaned(session, run)
    assert run.status == "FAILURE" and "ended before the script" in run.error


async def test_a_job_whose_code_cannot_be_created_is_not_left_behind(session, cluster, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    real = cluster.handle

    def no_configmaps(req):
        if req.method == "POST" and "/configmaps" in req.url.path:
            return httpx.Response(403, json={"message": "configmaps is forbidden"})
        return real(req)
    cluster.handle = no_configmaps
    import app.services.kube_jobs as kj
    kj._transport = httpx.MockTransport(no_configmaps)
    with pytest.raises(orch.OrchestrationError, match="configmaps is forbidden"):
        await _run(session, admin, _flow(session, [_script(env.id)], []))
    assert cluster.jobs() == []  # the Job was deleted


# ── the editor's «Run» of a Script node ─────────────────────────────────────
@pytest.fixture
def trial_env(session, cluster, engine, launches, monkeypatch):
    """run_trial opens its own session on the application's engine: the test's here."""
    import app.db.session as db_session
    import app.routes.runs as runs_routes

    monkeypatch.setattr(db_session, "engine", session.get_bind())
    monkeypatch.setattr(runs_routes, "_launch_flow_run", orch._launch_flow_run)
    return session


def _trial(session, user, flow, node_id="s1") -> Run:
    run = Run(kind="script", flow_id=flow.id, task_id="", status="PENDING", launched_by=user.id, trigger_type="trial",
              input_key="", output_bucket="", output_key="", script=json.dumps({"node_id": node_id, "trial": True}))
    session.add(run)
    session.commit()
    return run


async def test_a_trial_runs_the_saved_node_on_the_sample_and_keeps_its_output(trial_env, cluster, engine, launches):
    session = trial_env
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    flow = _flow(session, [SOURCE, _script(env.id), OUTPUT], [_edge("src", "s1"), _edge("s1", "out")])
    run = _trial(session, admin, flow)
    await script_runs.run_trial(run.id, admin.id, "s1")
    session.expire_all()
    run = session.get(Run, run.id)
    assert run.status == "SUCCESS" and run.output_key.startswith("out/")
    what, body, kw = launches.bodies[0]
    assert what == "script_input" and kw["engine_mode"] == "development" and kw["parent_run_id"] == run.id
    assert [w for w, _, _ in launches.bodies] == ["script_input"]  # the Output after it is not run
    env_vars = {e["name"]: e["value"] for e in cluster.created[0][2]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env_vars["TABULARIA_RUN_ID"] == str(run.id)
    out = script_runs.latest_outputs(session, flow.id)["s1"]
    assert out["output"]["key"] == run.output_key and out["output"]["trial"] is True


async def test_a_trial_of_a_script_that_writes_nothing_is_fine(trial_env, cluster, engine, launches):
    session = trial_env
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    engine.found = False
    flow = _flow(session, [_script(env.id)], [])
    run = _trial(session, admin, flow)
    await script_runs.run_trial(run.id, admin.id, "s1")
    session.expire_all()
    run = session.get(Run, run.id)
    assert run.status == "SUCCESS" and run.output_key == ""
    # the Job still had somewhere to write: the editor previews what it writes
    env_vars = {e["name"]: e["value"] for e in cluster.created[0][2]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env_vars["TABULARIA_OUTPUT"].endswith("/output.parquet")


async def test_a_trial_reads_the_latest_output_of_the_script_before_it(trial_env, cluster, engine, launches):
    session = trial_env
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    first = _script(env.id)
    second = {**_script(env.id), "id": "s2"}
    flow = _flow(session, [first, second], [_edge("s1", "s2")])
    await script_runs.run_trial(_trial(session, admin, flow, "s1").id, admin.id, "s1")
    await script_runs.run_trial(_trial(session, admin, flow, "s2").id, admin.id, "s2")
    session.expire_all()
    s1 = script_runs.latest_outputs(session, flow.id)["s1"]["output"]
    what, body, _ = launches.bodies[-1]
    assert what == "script_input" and body["input_key"] == s1["key"]


def test_latest_outputs_reads_two_rows_per_node(session):
    """The latest run of each node and its latest output, wherever they are in the
    history; a node whose id begins like another's (s1, s10, s_1) is not confused."""
    from datetime import datetime, timedelta, timezone

    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    flow = _flow(session, [_script(env.id), {**_script(env.id), "id": "s10"}, {**_script(env.id), "id": "s_1"}], [])
    t0 = datetime.now(timezone.utc) - timedelta(hours=1)

    def run(node_id, minutes, status, key=""):
        r = Run(kind="script", flow_id=flow.id, task_id="", status=status, launched_by=admin.id, input_key="",
                output_bucket="b", output_key=key, started_at=t0 + timedelta(minutes=minutes),
                script=json.dumps({"node_id": node_id, "log": f"{node_id}@{minutes}"}))
        session.add(r)

    run("s1", 0, "SUCCESS", "out/old.parquet")
    for m in range(1, 30):
        run("s10", m, "SUCCESS", f"out/s10-{m}.parquet")
    run("s1", 40, "FAILURE")
    run("s1x", 50, "SUCCESS", "out/other.parquet")  # not a node of the flow
    session.commit()
    out = script_runs.latest_outputs(session, flow.id)
    assert set(out) == {"s1", "s10"}
    assert out["s1"]["last"]["status"] == "FAILURE" and out["s1"]["last"]["log"] == "s1@40"
    assert out["s1"]["output"]["key"] == "out/old.parquet"
    assert out["s10"]["output"]["key"] == "out/s10-29.parquet" and out["s10"]["last"]["log"] == "s10@29"


async def test_a_trial_without_the_script_before_it_says_what_to_run(trial_env, cluster, engine, launches):
    session = trial_env
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    flow = _flow(session, [_script(env.id), {**_script(env.id), "id": "s2"}], [_edge("s1", "s2")])
    run = _trial(session, admin, flow, "s2")
    await script_runs.run_trial(run.id, admin.id, "s2")
    session.expire_all()
    run = session.get(Run, run.id)
    assert run.status == "FAILURE" and "no source with data upstream" in run.error


def test_the_trial_route_and_the_outputs(trial_env, cluster, engine, launches, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.security import hash_password
    from app.db.session import get_session
    from app.routes import auth as auth_routes
    from app.routes import scripting as scripting_routes

    session = trial_env
    started: list = []
    monkeypatch.setattr(script_runs, "run_trial", lambda *a: _noop(started, a))
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(scripting_routes.editor_router)
    app.dependency_overrides[get_session] = lambda: session
    make_user(session, email="a@x.local", hashed_password=hash_password("una-password-lunga"), is_superuser=True)
    c = TestClient(app)
    c.post("/auth/login", json={"email": "a@x.local", "password": "una-password-lunga"}, headers={"X-Tabularia-Csrf": "1"})
    env_id = _environment(session).id
    flow = _flow(session, [SOURCE, _script(env_id)], [_edge("src", "s1")])
    flow_id, csrf = flow.id, {"X-Tabularia-Csrf": "1"}
    r = c.post(f"/scripting/flows/{flow_id}/nodes/s1/run", headers=csrf)
    assert r.status_code == 202 and started
    again = c.post(f"/scripting/flows/{flow_id}/nodes/s1/run", headers=csrf)
    assert again.json()["run_id"] == r.json()["run_id"]  # one trial per node at a time
    assert c.post(f"/scripting/flows/{flow_id}/nodes/nope/run", headers=csrf).status_code == 404
    # its output, and «stale» once the saved code changes
    run = session.get(Run, r.json()["run_id"])
    run.status, run.output_key, run.output_bucket = "SUCCESS", "out/t.parquet", "b"
    run.script = json.dumps({"node_id": "s1", "trial": True,
                             "fingerprint": script_nodes.code_fingerprint(_script(env_id))})
    session.add(run)
    session.commit()
    out = c.get(f"/scripting/flows/{flow_id}/outputs").json()
    assert out["s1"]["output"]["key"] == "out/t.parquet" and out["s1"]["output"]["stale"] is False
    flow = session.get(script_runs.Flow, flow_id)
    flow.definition = json.dumps({"nodes": [SOURCE, _script(env_id, files=[{"path": "main.py", "content": "print(2)"}])],
                                  "edges": [_edge("src", "s1")]})
    session.add(flow)
    session.commit()
    assert c.get(f"/scripting/flows/{flow_id}/outputs").json()["s1"]["output"]["stale"] is True


async def _noop(started, args):
    started.append(args)


async def test_an_orphaned_trial_is_closed_after_its_budget(session, cluster):
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(hours=3)
    run = Run(kind="script", task_id="", status="STARTED", trigger_type="trial", input_key="", output_bucket="",
              output_key="", started_at=old, script=json.dumps({"node_id": "s1", "budget_seconds": 3600}))
    session.add(run)
    session.commit()
    run = await script_runs.close_if_orphaned(session, run)
    assert run.status == "FAILURE" and "outlived its time" in run.error
    young = Run(kind="script", task_id="", status="STARTED", trigger_type="trial", input_key="", output_bucket="",
                output_key="", script=json.dumps({"node_id": "s1", "budget_seconds": 3600}))
    session.add(young)
    session.commit()
    assert (await script_runs.close_if_orphaned(session, young)).status == "STARTED"


# ── the trials' namespace ────────────────────────────────────────────────────
async def test_trials_run_in_their_own_namespace_and_flows_in_theirs(trial_env, cluster, engine, launches, monkeypatch):
    monkeypatch.setenv("SCRIPTS__TRIAL_NAMESPACE", "tab-scripts-dev")
    get_settings.cache_clear()
    session = trial_env
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    flow = _flow(session, [_script(env.id)], [])
    run = _trial(session, admin, flow)
    await script_runs.run_trial(run.id, admin.id, "s1")
    assert {ns for kind, ns, _ in cluster.created} == {"tab-scripts-dev"}
    session.expire_all()
    assert json.loads(session.get(Run, run.id).script)["namespace"] == "tab-scripts-dev"
    cluster.created.clear()
    await _run(session, admin, flow)
    assert {ns for kind, ns, _ in cluster.created} == {"tab-scripts"}


async def test_a_full_quota_is_said_at_once(trial_env, cluster, engine, launches, monkeypatch):
    monkeypatch.setenv("SCRIPTS__TRIAL_NAMESPACE", "tab-scripts-dev")
    get_settings.cache_clear()
    monkeypatch.setattr(script_runs, "NO_POD_SECONDS", 0)
    session = trial_env
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    cluster.behave = lambda job: (None, "", None)  # no Pod: the Job never starts
    flow = _flow(session, [_script(env.id)], [])
    run = _trial(session, admin, flow)
    name = f"tabularia-script-{run.id}-a1"
    cluster.events[name] = [{"reason": "FailedCreate", "message": 'pods "x" is forbidden: exceeded quota: trials, '
                                                                  "requested: limits.memory=4Gi, used: limits.memory=16Gi"}]
    await script_runs.run_trial(run.id, admin.id, "s1")
    session.expire_all()
    run = session.get(Run, run.id)
    assert run.status == "FAILURE" and "tab-scripts-dev has no room left" in run.error and "exceeded quota" in run.error
    assert cluster.jobs() == []


@pytest.mark.parametrize("reason, message, events, expected", [
    ("ImagePullBackOff", 'Back-off pulling image "registry.example.com/tab/env-1@sha256:1"',
     [{"reason": "Failed", "message": "Failed to pull image: pull access denied, insufficient_scope: authorization failed"},
      {"reason": "Failed", "message": "Error: ImagePullBackOff"}],
     "registry refused the cluster's credentials"),
    ("ErrImagePull", "failed to resolve reference: tls: failed to verify certificate: x509: certificate signed by "
                     "unknown authority", [], "nodes do not trust the registry's certificate"),
    ("ErrImagePull", "rpc error: registry.example.com/tab/env-1@sha256:1: not found", [], "not in the registry"),
])
async def test_an_image_the_nodes_cannot_download_fails_at_once_saying_why(session, cluster, engine, launches,
                                                                           monkeypatch, reason, message, events, expected):
    # without this, the attempt waited for the node's whole timeout and said it ran too long
    monkeypatch.setattr(script_runs, "PULL_FAILURE_SECONDS", 0)
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    cluster.behave = lambda job: (None, "", None)  # the Job never ends: its Pod waits for its image
    cluster.pull = (reason, message, events)
    with pytest.raises(orch.OrchestrationError, match=expected):
        await _run(session, admin, _flow(session, [_script(env.id)], []))
    run = _script_run(session)
    assert run.status == "FAILURE" and "could not download the image" in run.error and "timeout" not in run.error
    assert cluster.jobs() == []


async def test_a_script_runs_on_nodes_of_its_image_s_architecture(session, cluster, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    v = session.get(ScriptEnvironmentVersion, env.current_version_id)
    v.arch = "arm64"  # built on an arm64 node: an amd64 node could not run it
    session.add(v)
    session.commit()
    await _run(session, admin, _flow(session, [_script(env.id)], []))
    pod = next(body for kind, _, body in cluster.created if kind == "jobs")["spec"]["template"]["spec"]
    assert pod["nodeSelector"] == {"kubernetes.io/arch": "arm64"}


async def test_a_version_built_before_the_architecture_was_kept_runs_anywhere(session, cluster, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    await _run(session, admin, _flow(session, [_script(env.id)], []))
    pod = next(body for kind, _, body in cluster.created if kind == "jobs")["spec"]["template"]["spec"]
    assert "nodeSelector" not in pod


# ── a trial never stays open for ever ────────────────────────────────────────
async def test_a_trial_left_pending_is_closed_as_never_started(session, cluster):
    from datetime import datetime, timedelta, timezone

    old = datetime.now(timezone.utc) - timedelta(seconds=script_runs.TRIAL_START_SECONDS + 5)
    run = Run(kind="script", task_id="", status="PENDING", trigger_type="trial", input_key="", output_bucket="",
              output_key="", started_at=old, script=json.dumps({"node_id": "s1", "trial": True}))  # no budget: before the fix, never closed
    session.add(run)
    session.commit()
    run = await script_runs.close_if_orphaned(session, run)
    assert run.status == "FAILURE" and "never started" in run.error
    fresh = Run(kind="script", task_id="", status="PENDING", trigger_type="trial", input_key="", output_bucket="",
                output_key="", script=json.dumps({"node_id": "s1", "trial": True}))
    session.add(fresh)
    session.commit()
    assert (await script_runs.close_if_orphaned(session, fresh)).status == "PENDING"


async def test_a_trial_stopped_by_its_gateway_says_so(trial_env, cluster, engine, launches, monkeypatch):
    import asyncio

    session = trial_env
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    flow = _flow(session, [_script(env.id)], [])
    run = _trial(session, admin, flow)
    run_id = run.id

    async def forever(*a, **kw):
        await asyncio.sleep(3600)

    monkeypatch.setattr(script_runs, "run_script", forever)
    task = asyncio.create_task(script_runs.run_trial(run_id, admin.id, "s1"))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    session.expire_all()
    run = session.get(Run, run_id)
    assert run.status == "FAILURE" and "gateway running the trial stopped" in run.error


def test_a_dead_trial_gives_way_to_a_new_one(trial_env, cluster, engine, launches, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.core.security import hash_password
    from app.db.session import get_session
    from app.routes import auth as auth_routes
    from app.routes import scripting as scripting_routes

    session = trial_env
    started: list = []
    monkeypatch.setattr(script_runs, "run_trial", lambda *a: _noop(started, a))
    app = FastAPI()
    app.include_router(auth_routes.router)
    app.include_router(scripting_routes.editor_router)
    app.dependency_overrides[get_session] = lambda: session
    make_user(session, email="a@x.local", hashed_password=hash_password("una-password-lunga"), is_superuser=True)
    c = TestClient(app)
    csrf = {"X-Tabularia-Csrf": "1"}
    c.post("/auth/login", json={"email": "a@x.local", "password": "una-password-lunga"}, headers=csrf)
    env_id = _environment(session).id
    flow_id = _flow(session, [_script(env_id)], []).id
    first = c.post(f"/scripting/flows/{flow_id}/nodes/s1/run", headers=csrf).json()["run_id"]
    assert json.loads(session.get(Run, first).script)["budget_seconds"] > 0  # the budget from the start
    # its process died before marking it started
    dead = session.get(Run, first)
    dead.started_at = datetime.now(timezone.utc) - timedelta(seconds=script_runs.TRIAL_START_SECONDS + 5)
    session.add(dead)
    session.commit()
    second = c.post(f"/scripting/flows/{flow_id}/nodes/s1/run", headers=csrf).json()["run_id"]
    assert second != first and session.get(Run, first).status == "FAILURE"


def test_without_scripting_a_flow_keeps_its_script_nodes(session):
    """Docker Compose: the checks of the Script node do not run (nothing runs them there)."""
    user = make_user(session, email="u@x.local")
    broken = json.dumps({"nodes": [{"id": "s1", "type": "script", "data": {"environmentId": 99,
                                                                           "files": [{"path": "../x.py", "content": ""}]}}]})
    script_nodes.check_definition(session, user, None, broken)  # kept as it is
