"""Saved scripts, piece 2: a saved script run on its own (services/saved_script_runs.py,
routes/scripts.py; docs/specs/saved-scripts.md «Inputs», «Output», «Runs»). A fake
cluster and a fake engine.

Fixed here:
- «Run» queues an orchestration row with the script, claimed and carried as a flow's;
  RUN on the folder is needed, one live run at a time, an empty script is refused;
- every declared input is read again at the run: deleted, no longer readable or empty
  fails it before any Job, saying which; each is copied to the scripts bucket and
  reaches the code as TABULARIA_INPUT_<ALIAS> and in TABULARIA_INPUTS, keyed by alias,
  with the real name and folder (two homonymous datasources stay apart);
- the output is optional: written, it is kept and previewed, and published as a
  datasource when the script says so (EDIT on that folder, with the runner's
  authority); not written, the run succeeds all the same;
- the step's files are deleted from the scripts bucket whatever happens;
- the run page lets read a script's run only to whoever sees the script; a deleted
  script leaves its runs in the history.
"""
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db.session import get_session
from app.models import Datasource, Group, Run, SavedScript, SavedScriptVersion, User, UserGroupLink
from app.services import orchestrator as orch
from app.services import saved_script_runs
from tests.conftest import make_datasource, make_permission, make_project, make_user
from tests.test_script_runs import DIGEST, _environment, cluster  # noqa: F401 — the shared fixtures

pytestmark = pytest.mark.anyio
CSRF = {"X-Tabularia-Csrf": "1"}
PASSWORD = "una-password-lunga"
CODE = "import os\nprint(os.environ['TABULARIA_INPUTS'])\n"


class Engine:
    """The engine's /scripts endpoints and its preview."""

    def __init__(self):
        self.exports: list[dict] = []
        self.imports: list[dict] = []
        self.deletes: list[dict] = []
        self.found = True
        self.missing: set[str] = set()  # datasource keys whose file is gone

    def handle(self, req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if req.url.path == "/scripts/input/export":
            if body["key"] in self.missing:
                return httpx.Response(404, json={"detail": "The datasource's file could not be read (404: Not Found)"})
            self.exports.append(body)
            return httpx.Response(200, json={"bytes": 100})
        if req.url.path == "/scripts/output/import":
            self.imports.append(body)
            if not self.found:
                return httpx.Response(200, json={"found": False})
            return httpx.Response(200, json={"found": True, "rows": 3, "bytes": 500,
                                             "columns": [{"name": "region", "dtype": "String"},
                                                         {"name": "total", "dtype": "Int64"}]})
        if req.url.path == "/scripts/objects/delete":
            self.deletes.append(body)
            return httpx.Response(200, json={"deleted": len(body["keys"]), "failed": []})
        if req.url.path == "/tasks/preview":
            return httpx.Response(200, json={"columns": [{"name": "region"}], "rows": [{"region": "north"}]})
        return httpx.Response(404, json={"detail": "not mocked"})


@pytest.fixture
def engine(monkeypatch):
    e = Engine()
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(e.handle), base_url="http://engine"))
    return e


@pytest.fixture
def queue(session, monkeypatch):
    """The orchestrator's sessions on the test database."""
    monkeypatch.setattr(orch, "engine", session.get_bind())
    monkeypatch.setattr(orch, "sveglia", lambda: None)
    return session


def _script(session, env, project_id, *, inputs=(), output=None, owner=None, code=CODE, retries=0) -> SavedScript:
    env_id = env if isinstance(env, int) else env.id
    s = SavedScript(name="Totals", project_id=project_id, environment_id=env_id, owner_id=owner, current_version=1)
    session.add(s)
    session.commit()
    content = {"files": [{"path": "main.py", "content": code}], "entrypoint": "main.py", "inputs": list(inputs),
               "output": output or {"publish": False, "name": "", "project_id": None, "overwrite": True},
               "timeout_seconds": 600, "retries": retries, "retry_delay_seconds": 0}
    session.add(SavedScriptVersion(script_id=s.id, version=1, content=json.dumps(content), note="created"))
    session.commit()
    return s


def _usable(session, user):
    """An environment `user` may use, through a group of theirs."""
    g = Group(name=f"scripters-{user.id}")
    session.add(g)
    session.commit()
    session.add(UserGroupLink(user_id=user.id, group_id=g.id))
    session.commit()
    return _environment(session, groups=(g.id,))


async def _run(session, user, script) -> Run:
    head = saved_script_runs.create_script_run(session, user, script)
    await orch.esegui(head.id)
    session.expire_all()
    return session.get(Run, head.id)


def _child(session) -> Run:
    return session.exec(select(Run).where(Run.kind == "script")).one()


def _env_of(job: dict) -> dict:
    return {e["name"]: e["value"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}


async def test_two_homonymous_inputs_reach_the_code_by_alias_and_the_output_is_published(session, cluster, engine, queue):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    north = make_project(session, name="North")
    south = make_project(session, name="South")
    cols = json.dumps([{"name": "region", "dtype": "String"}, {"name": "amount", "dtype": "Int64"}])
    a = make_datasource(session, name="Vendite 2024", project_id=north.id, key="datasets/1/a.parquet", columns=cols)
    b = make_datasource(session, name="Vendite 2024", project_id=south.id, key="datasets/2/b.parquet", columns=cols)
    script = _script(session, env, north.id, inputs=[{"datasource_id": a.id, "alias": "VENDITE_2024_NORTH"},
                                                     {"datasource_id": b.id, "alias": "VENDITE_2024_SOUTH"}],
                     output={"publish": True, "name": "Totals by region", "project_id": south.id, "overwrite": True})
    head = await _run(session, admin, script)
    assert head.status == "SUCCESS", head.error
    child = _child(session)
    assert child.saved_script_id == script.id and child.parent_run_id == head.id and child.flow_id is None
    # the inputs: copied from their current file to the scripts bucket, one per alias
    assert [(e["key"], e["target_key"].rsplit("/", 1)[1]) for e in engine.exports] == [
        ("datasets/1/a.parquet", "in-VENDITE_2024_NORTH.parquet"), ("datasets/2/b.parquet", "in-VENDITE_2024_SOUTH.parquet")]
    assert all(e["target_key"].startswith(f"script-{script.id}/") for e in engine.exports)
    # the Job: in the scripts namespace, with the inputs by alias and the output's address
    job = cluster.created[0][2]
    assert cluster.created[0][1] == "tab-scripts"
    vars_ = _env_of(job)
    assert vars_["TABULARIA_INPUT_VENDITE_2024_NORTH"] == f"s3://tab-scripts/{engine.exports[0]['target_key']}"
    described = json.loads(vars_["TABULARIA_INPUTS"])
    assert set(described) == {"VENDITE_2024_NORTH", "VENDITE_2024_SOUTH"}
    assert described["VENDITE_2024_SOUTH"] == {"datasource_id": b.id, "name": "Vendite 2024", "folder": "South",
                                               "uri": vars_["TABULARIA_INPUT_VENDITE_2024_SOUTH"],
                                               "columns": ["region", "amount"]}
    assert vars_["TABULARIA_OUTPUT"].endswith("/output.parquet") and vars_["TABULARIA_SCRIPT_NAME"] == "Totals"
    assert "TABULARIA_INPUT" not in vars_  # the step before: inside a flow only
    # the output: kept, and published as a datasource with its columns
    ds = session.exec(select(Datasource).where(Datasource.name == "Totals by region")).one()
    assert ds.project_id == south.id and ds.key == child.output_key and ds.rows == 3
    assert [c["name"] for c in json.loads(ds.columns)] == ["region", "total"]
    assert child.datasource_id == ds.id
    info = json.loads(child.script)
    assert info["script_version"] == 1 and [i["alias"] for i in info["inputs"]] == ["VENDITE_2024_NORTH", "VENDITE_2024_SOUTH"]
    # the step's files, gone
    assert set(engine.deletes[0]["keys"]) == {e["target_key"] for e in engine.exports} | {vars_["TABULARIA_OUTPUT"].split("tab-scripts/")[1]}


async def test_a_script_that_writes_nothing_succeeds(session, cluster, engine, queue):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    p = make_project(session, name="p")
    engine.found = False
    head = await _run(session, admin, _script(session, env, p.id))
    assert head.status == "SUCCESS"
    child = _child(session)
    assert child.status == "SUCCESS" and child.output_key == "" and json.loads(child.script)["output"] is False
    assert session.exec(select(Datasource)).all() == []


@pytest.mark.parametrize("case", ["deleted", "unreadable", "empty", "file_gone"])
async def test_an_input_that_cannot_be_read_fails_the_run_saying_which(session, cluster, engine, queue, case):
    user = make_user(session, email="u@x.local")
    env = _usable(session, user)
    mine = make_project(session, name="Mine")
    theirs = make_project(session, name="Theirs")
    for cap in ("view", "run"):
        make_permission(session, user_id=user.id, project_id=mine.id, capability=cap)
    ds = make_datasource(session, name="Orders", project_id=theirs.id if case == "unreadable" else mine.id,
                         key="" if case == "empty" else "datasets/9/o.parquet")
    if case == "file_gone":
        engine.missing.add("datasets/9/o.parquet")
    script = _script(session, env, mine.id, inputs=[{"datasource_id": ds.id, "alias": "ORDERS"}])
    if case == "deleted":
        session.delete(ds)
        session.commit()
    head = await _run(session, user, script)
    assert head.status == "FAILURE"
    expected = {"deleted": "the input ORDERS is a datasource that no longer exists",
                "unreadable": "u@x.local can no longer read the datasource «Orders» (input ORDERS)",
                "empty": "the datasource «Orders» (input ORDERS) has no data yet",
                "file_gone": "the datasource «Orders» (input ORDERS) could not be copied"}[case]
    assert expected in head.error
    assert cluster.created == []  # nothing ran


async def test_publishing_needs_edit_on_the_folder_with_the_runners_authority(session, cluster, engine, queue):
    user = make_user(session, email="u@x.local")
    env = _usable(session, user)
    mine = make_project(session, name="Mine")
    other = make_project(session, name="Other")
    for cap in ("view", "run"):
        make_permission(session, user_id=user.id, project_id=mine.id, capability=cap)
    script = _script(session, env, mine.id, output={"publish": True, "name": "X", "project_id": other.id})
    head = await _run(session, user, script)
    assert head.status == "FAILURE" and "cannot write in the folder" in head.error and cluster.created == []


async def test_a_failed_script_fails_the_run_and_still_cleans_up(session, cluster, engine, queue):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    p = make_project(session, name="p")
    cluster.behave = lambda job: ("failed", "Traceback: boom", 1)
    head = await _run(session, admin, _script(session, env, p.id))
    assert head.status == "FAILURE" and "it ended with exit code 1" in head.error
    child = _child(session)
    assert child.status == "FAILURE" and "boom" in json.loads(child.script)["log"]
    assert engine.deletes and engine.imports == []


# ── the routes ────────────────────────────────────────────────────────────────
@pytest.fixture
def app(session, queue):
    from app.routes import auth as auth_routes
    from app.routes import runs as runs_routes
    from app.routes import scripts as scripts_routes

    app = FastAPI()
    for r in (auth_routes.router, scripts_routes.router, runs_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _login(app, session, email, **kw) -> TestClient:
    from app.core.security import hash_password

    make_user(session, email=email, hashed_password=hash_password(PASSWORD), **kw)
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": PASSWORD}).status_code == 200
    return c


async def test_run_route_rights_one_live_run_and_the_history(app, session, cluster, engine):
    viewer = _login(app, session, "v@x.local")
    v_user = session.exec(select(User).where(User.email == "v@x.local")).one()
    env_id = _usable(session, v_user).id
    pid = make_project(session, name="p").id
    make_permission(session, user_id=v_user.id, project_id=pid, capability="view")
    script_id = _script(session, env_id, pid).id
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    stranger = _login(app, session, "s@x.local")
    # VIEW sees it but may not run it
    assert viewer.get(f"/scripts/{script_id}").json()["runnable"] is False
    assert viewer.post(f"/scripts/{script_id}/run", headers=CSRF).status_code == 403
    assert stranger.post(f"/scripts/{script_id}/run", headers=CSRF).status_code == 404
    r = boss.post(f"/scripts/{script_id}/run", headers=CSRF)
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    assert boss.post(f"/scripts/{script_id}/run", headers=CSRF).status_code == 409  # one at a time
    assert boss.delete(f"/scripts/{script_id}", headers=CSRF).status_code == 409  # not while queued
    await orch.esegui(run_id)
    session.expire_all()
    runs = viewer.get(f"/scripts/{script_id}/runs").json()
    assert [(x["run_id"], x["status"], x["output"], x["rows"]) for x in runs] == [(run_id, "SUCCESS", True, 3)]
    assert viewer.get(f"/scripts/{script_id}/runs/{run_id}/preview").json()["rows"] == [{"region": "north"}]
    child_id = runs[0]["script_run_id"]
    assert viewer.get(f"/runs/{child_id}").status_code == 200
    assert stranger.get(f"/runs/{child_id}").status_code == 404 and stranger.get(f"/runs/{run_id}").status_code == 404
    # deleted, its runs stay in the history without it
    assert boss.delete(f"/scripts/{script_id}", headers=CSRF).status_code == 204
    session.expire_all()
    assert session.get(Run, run_id).saved_script_id is None and session.get(Run, child_id) is not None


async def test_an_empty_script_cannot_run(app, session, cluster, engine):
    env_id = _environment(session).id
    pid = make_project(session, name="p").id
    script_id = _script(session, env_id, pid, code="").id
    row = session.exec(select(SavedScriptVersion)).one()
    row.content = json.dumps({"files": [], "entrypoint": "", "inputs": []})
    session.commit()
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    assert boss.post(f"/scripts/{script_id}/run", headers=CSRF).status_code == 422


async def test_the_runs_list_leaves_out_a_script_its_reader_may_not_see(app, session, cluster, engine):
    # VIEW on the folder without the use of the environment: the script does not exist for
    # them (GET /scripts/{id} and GET /runs/{id} are 404), and the runs list and the activity
    # calendar leave its runs out too — their log is what the script printed
    outsider = _login(app, session, "o@x.local")
    o_user = session.exec(select(User).where(User.email == "o@x.local")).one()
    pid = make_project(session, name="p").id
    make_permission(session, user_id=o_user.id, project_id=pid, capability="view")
    script_id = _script(session, _environment(session).id, pid).id  # no group: only admins use it
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    run_id = boss.post(f"/scripts/{script_id}/run", headers=CSRF).json()["run_id"]
    await orch.esegui(run_id)
    session.expire_all()
    child_id = session.exec(select(Run.id).where(Run.kind == "script")).one()
    assert outsider.get(f"/scripts/{script_id}").status_code == 404
    assert outsider.get(f"/runs/{child_id}").status_code == 404
    assert outsider.get("/runs").json()["total"] == 0
    assert sum(b["total"] for b in outsider.get("/runs/activity").json()["buckets"]) == 0
    assert sum(b["total"] for b in boss.get("/runs/activity").json()["buckets"]) >= 1
    assert {i["id"] for i in boss.get("/runs").json()["items"]} >= {run_id, child_id}


async def test_a_retry_starts_without_what_the_failed_attempt_wrote(session, cluster, engine, queue):
    # attempt 1 writes TABULARIA_OUTPUT and fails; attempt 2 succeeds: its output, not the
    # failed one's, is imported — the object is deleted between the two, as for a Script node
    admin = make_user(session, email="a@x.local", is_superuser=True)
    cluster.behave = lambda job: (("failed", "Traceback: boom", 1) if job["metadata"]["name"].endswith("-a1")
                                  else ("succeeded", "done", 0))
    script = _script(session, _environment(session), make_project(session, name="p").id, retries=1)
    run = await _run(session, admin, script)
    info = json.loads(_child(session).script)
    assert run.status == "SUCCESS" and info["attempts"] == 2
    out_key = _env_of(cluster.created[0][2])["TABULARIA_OUTPUT"].split("/", 3)[3]
    # once between the attempts, once with the step's files at the end
    assert sum(out_key in d["keys"] for d in engine.deletes) == 2
