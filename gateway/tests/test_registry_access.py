"""Who may see and use a container registry (services/registry_access.py).

Fixed here:
- an administrator grants a registry to groups and users, with all its repositories
  or some of them; nothing else gives access, administrators have it all;
- the flow editor's list and tags show only what the person asking may use, and the
  repositories a grant leaves out are not even said to exist;
- a flow with a Container node of a registry, or an image, the person may not use does
  not open: nor do its versions, a copy, a save, a run, a schedule or a group of flows
  that would run it. It is still listed. A flow without such nodes opens as before;
- a save may add a Container node only with a registry in use and an image the person
  may use.
"""
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.db.session import get_session
from app.models import ContainerRegistry, ContainerRegistryAccess, Flow, Group, User, UserGroupLink
from app.routes import registries as routes
from app.services import container_registry as cr
from app.services import registry_access
from tests.conftest import make_permission, make_project, make_user
from tests.test_registries import CSRF, PASSWORD, TOKEN, FakeRegistry, _audit, _detail

FORECAST = {"id": "ctl-container-1", "type": "container", "position": {"x": 0, "y": 0},
            "data": {"label": "Forecast", "registryId": None, "repository": "team/forecast", "tag": "v1"}}


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("CONTAINERS__ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def app(session):
    from app.routes import auth as auth_routes
    from app.routes import flow_groups as groups_routes
    from app.routes import flows as flows_routes

    app = FastAPI()
    from app.routes import search as search_routes

    for r in (auth_routes.router, flows_routes.router, groups_routes.router, search_routes.router, routes.router,
              routes.editor_router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


@pytest.fixture
def registry(monkeypatch):
    fake = FakeRegistry()
    fake.tags["team/a/model"] = ["1.0"]
    monkeypatch.setattr(cr, "_transport", httpx.MockTransport(fake.handle))
    return fake


def _login(app, session, email, **kw) -> tuple[TestClient, int]:
    """The client, and the user's id (the instance is detached once the login commits)."""
    from app.core.security import hash_password

    uid = make_user(session, email=email, hashed_password=hash_password(PASSWORD), **kw).id
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF).status_code == 200
    return c, uid


def _group(session, name, *member_ids) -> int:
    g = Group(name=name)
    session.add(g)
    session.commit()
    gid = g.id
    for uid in member_ids:
        session.add(UserGroupLink(user_id=uid, group_id=gid))
    session.commit()
    return gid


def _uid(session, email) -> int:
    return session.exec(select(User.id).where(User.email == email)).one()


def _registry(boss, name="Acme", repositories=("team/*", "ml/forecast"), access=(), enabled=True):
    r = boss.post("/admin/registries", json={"name": name, "host": "reg.test", "username": "ada", "password": TOKEN,
                                             "repositories": list(repositories), "access": list(access),
                                             "enabled": enabled}, headers=CSRF)
    assert r.status_code == 201, r.text
    return r.json()


def _flow(session, project_id, *nodes) -> int:
    f = Flow(name="Forecast flow", project_id=project_id, definition=json.dumps({"nodes": list(nodes), "edges": []}))
    session.add(f)
    session.commit()
    return f.id


def _node(registry_id, repository="team/forecast"):
    return {**FORECAST, "data": {**FORECAST["data"], "registryId": registry_id, "repository": repository}}


# ── the grants ───────────────────────────────────────────────────────────────
def test_access_is_given_to_groups_and_users_and_audited(app, session, enabled):
    boss, _ = _login(app, session, "boss@x.local", is_superuser=True)
    _ana_client, ana = _login(app, session, "ana@x.local")
    finance = _group(session, "finance")
    reg = _registry(boss, access=[{"group_id": finance, "repositories": ["team/finance/*"]},
                                  {"user_id": ana}])
    assert [(a["name"], a["repositories"]) for a in reg["access"]] == [("finance", ["team/finance/*"]), ("ana@x.local", [])]
    created = _detail(_audit(session, "registry.create")[0])["access"]
    assert created == [{"group": "finance", "repositories": ["team/finance/*"]}, {"user": "ana@x.local", "repositories": "all"}]
    # replaced as a whole; the change is in the audit
    boss.put(f"/admin/registries/{reg['id']}", json={"access": [{"user_id": ana, "repositories": ["ml/forecast"]}]},
             headers=CSRF)
    change = _detail(_audit(session, "registry.update")[-1])["access"]
    assert change["to"] == [{"user": "ana@x.local", "repositories": ["ml/forecast"]}]
    assert len(session.exec(select(ContainerRegistryAccess)).all()) == 1


@pytest.mark.parametrize("access, message", [
    ([{}], "a group or a user"),
    ([{"group_id": 1, "user_id": 1}], "a group or a user"),
    ([{"group_id": 999}], "Unknown group"),
    ([{"user_id": 999}], "Unknown user"),
    ([{"user_id": 1}, {"user_id": 1}], "twice"),
    ([{"user_id": 1, "repositories": ["Bad Name"]}], "not a repository"),
])
def test_an_access_that_cannot_be_saved_says_why(app, session, enabled, access, message):
    boss, _ = _login(app, session, "boss@x.local", is_superuser=True)
    _group(session, "g")
    r = boss.post("/admin/registries", json={"name": "Acme", "host": "reg.test", "access": access}, headers=CSRF)
    assert r.status_code == 422 and message in json.dumps(r.json())


# ── what the flow editor offers ──────────────────────────────────────────────
def test_the_editor_shows_only_what_one_may_use(app, session, enabled, registry):
    boss, _ = _login(app, session, "boss@x.local", is_superuser=True)
    ana_client, ana = _login(app, session, "ana@x.local")
    ben_client, ben = _login(app, session, "ben@x.local")
    _cleo_client, _ = _login(app, session, "cleo@x.local")
    team_a = _group(session, "team a", ana)
    acme = _registry(boss, access=[{"group_id": team_a, "repositories": ["team/a/*"]},
                                   {"user_id": ben, "repositories": ["ml/*"]}])
    _registry(boss, name="Hub", repositories=["*"], access=[{"user_id": ben}])
    _registry(boss, name="Off", access=[{"user_id": ana}], enabled=False)
    assert [(r["name"], r["repositories"]) for r in ana_client.get("/registries").json()["registries"]] == [
        ("Acme", ["team/a/*"])]
    assert [(r["name"], r["repositories"]) for r in ben_client.get("/registries").json()["registries"]] == [
        ("Acme", ["ml/forecast"]), ("Hub", ["*"])]
    assert _cleo_client.get("/registries").json()["registries"] == []
    assert [r["name"] for r in boss.get("/registries").json()["registries"]] == ["Acme", "Hub"]
    # tags: only of what one may use; a registry not granted does not exist
    r = ana_client.get(f"/registries/{acme['id']}/tags", params={"repository": "team/a/model"})
    assert r.status_code == 200 and r.json()["tags"] == ["1.0"]
    assert ana_client.get(f"/registries/{acme['id']}/tags", params={"repository": "team/forecast"}).status_code == 403
    assert _cleo_client.get(f"/registries/{acme['id']}/tags", params={"repository": "team/forecast"}).status_code == 404


@pytest.mark.parametrize("a, b, both", [
    ("*", "team/x", "team/x"), ("team/*", "team/a/*", "team/a/*"), ("team/a/*", "team/*", "team/a/*"),
    ("team/*", "team/forecast", "team/forecast"), ("team/*", "ml/*", None), ("team/forecast", "team/other", None),
    ("team/*", "team", None),
])
def test_two_patterns_meet(a, b, both):
    assert registry_access._intersect(a, b) == both


# ── a flow that uses a registry ──────────────────────────────────────────────
def _setup(app, session, *, grant: dict | None):
    boss, _ = _login(app, session, "boss@x.local", is_superuser=True)
    ana_client, ana = _login(app, session, "ana@x.local")
    project = make_project(session, name="Forecasting").id
    for cap in ("view", "run", "edit"):
        make_permission(session, user_id=ana, project_id=project, capability=cap)
    access = [{"user_id": ana, **grant}] if grant is not None else []
    reg = _registry(boss, access=access)
    flow = _flow(session, project, _node(reg["id"]))
    return boss, ana_client, project, reg, flow


def test_a_flow_of_a_registry_one_has_no_access_to_does_not_open(app, session, enabled):
    boss, ana, project, reg, flow = _setup(app, session, grant=None)
    r = ana.get(f"/flows/{flow}")
    assert r.status_code == 403 and "«Acme»" in r.json()["detail"] and "no access" in r.json()["detail"]
    # still listed, greyed out with the reason: seeing that it exists is not opening it
    for listed in (ana.get(f"/projects/{project}/flows").json(), ana.get("/flows").json(),
                   ana.get("/flows/search", params={"q": "Forecast"}).json()["items"]):
        assert [(f["name"], "«Acme»" in (f["locked_reason"] or "")) for f in listed] == [("Forecast flow", True)]
    # nothing else shows or runs it on this person's behalf
    assert ana.get(f"/flows/{flow}/versions").status_code == 403
    assert ana.patch(f"/flows/{flow}", json={"name": "Mine"}, headers=CSRF).status_code == 403
    assert ana.post(f"/flows/{flow}/duplicate", headers=CSRF).status_code == 403
    assert ana.post(f"/flows/{flow}/run-now", headers=CSRF).status_code == 403
    assert ana.put(f"/flows/{flow}/schedule", json={"cron": "0 6 * * *"}, headers=CSRF).status_code == 403
    group = ana.post(f"/projects/{project}/flow-groups",
                     json={"name": "Nightly", "definition": {"steps": [[{"kind": "flow", "id": flow}]]}}, headers=CSRF)
    assert group.status_code == 422 and "images or scripts you have no access to" in group.text
    # an administrator opens it; so does whoever is granted the registry
    assert boss.get(f"/flows/{flow}").status_code == 200
    boss.put(f"/admin/registries/{reg['id']}", json={"access": [{"user_id": _uid(session, "ana@x.local")}]},
             headers=CSRF)
    assert ana.get(f"/flows/{flow}").status_code == 200
    assert ana.get(f"/projects/{project}/flows").json()[0]["locked_reason"] is None


def test_a_grant_without_the_flow_s_image_does_not_open_it(app, session, enabled):
    _boss, ana, _project, _reg, flow = _setup(app, session, grant={"repositories": ["ml/*"]})
    r = ana.get(f"/flows/{flow}")
    assert r.status_code == 403 and "team/forecast" in r.json()["detail"]


def test_a_flow_without_container_nodes_or_of_a_registry_gone_opens(app, session, enabled):
    boss, ana, project, reg, flow = _setup(app, session, grant=None)
    plain = _flow(session, project, {"id": "src-1", "type": "source", "position": {"x": 0, "y": 0}, "data": {}})
    assert ana.get(f"/flows/{plain}").status_code == 200
    assert boss.delete(f"/admin/registries/{reg['id']}", headers=CSRF).status_code == 204
    assert ana.get(f"/flows/{flow}").status_code == 200  # nothing left to protect


def test_a_save_adds_only_container_nodes_one_may_use(app, session, enabled):
    boss, ana, project, reg, _flow_ = _setup(app, session, grant={"repositories": ["team/*"]})
    plain = _flow(session, project)
    def save(*nodes):
        return ana.patch(f"/flows/{plain}", json={"definition": json.dumps({"nodes": list(nodes), "edges": []})},
                         headers=CSRF)
    assert save(_node(reg["id"], "ml/forecast")).status_code == 403  # allowed by the registry, not granted
    assert save(_node(reg["id"], "team/forecast")).status_code == 200
    other = _registry(boss, name="Other")
    r = save(_node(other["id"]))
    assert r.status_code == 403 and "«Other»" in r.json()["detail"]
    off = _registry(boss, name="Off", access=[{"user_id": _uid(session, "ana@x.local")}], enabled=False)
    assert save(_node(off["id"])).status_code == 422
    # a new flow follows the same rule
    r = ana.post(f"/projects/{project}/flows",
                 json={"name": "New", "definition": json.dumps({"nodes": [_node(other["id"])], "edges": []})},
                 headers=CSRF)
    assert r.status_code == 403


# ── a flow with a Script node: the same rule, for scripting environments ─────
def test_a_flow_of_a_scripting_environment_one_may_not_use_does_not_open(app, session, enabled):
    from app.models import ScriptEnvironment, ScriptEnvironmentGroup

    boss, _ = _login(app, session, "boss@x.local", is_superuser=True)
    ana, ana_id = _login(app, session, "ana@x.local")
    project = make_project(session, name="Scoring").id
    for cap in ("view", "run", "edit"):
        make_permission(session, user_id=ana_id, project_id=project, capability=cap)
    env = ScriptEnvironment(name="Python models", language="python")
    session.add(env)
    session.commit()
    env_id = env.id
    script = {"id": "ctl-script-1", "type": "script", "position": {"x": 0, "y": 0},
              "data": {"label": "Score", "environmentId": env_id, "files": [{"path": "main.py", "content": "print(1)"}],
                       "entrypoint": "main.py"}}
    flow = _flow(session, project, script)
    r = ana.get(f"/flows/{flow}")
    assert r.status_code == 403 and "«Python models»" in r.json()["detail"]
    assert ana.post(f"/flows/{flow}/run-now", headers=CSRF).status_code == 403
    listed = ana.get(f"/projects/{project}/flows").json()
    assert "«Python models»" in listed[0]["locked_reason"]
    hits = ana.get("/search", params={"q": "Forecast"}).json()["items"]
    assert [(h["kind"], "«Python models»" in (h["locked_reason"] or "")) for h in hits] == [("flow", True)]
    assert boss.get(f"/flows/{flow}").status_code == 200
    # a group of the environment: then it opens
    g = _group(session, "scorers", ana_id)
    session.add(ScriptEnvironmentGroup(environment_id=env_id, group_id=g))
    session.commit()
    assert ana.get(f"/flows/{flow}").status_code == 200
    assert ana.get("/search", params={"q": "Forecast"}).json()["items"][0]["locked_reason"] is None


def test_a_flow_of_a_scripting_environment_gone_opens(app, session, enabled):
    _boss, _ = _login(app, session, "boss@x.local", is_superuser=True)
    ana, ana_id = _login(app, session, "ana@x.local")
    project = make_project(session, name="Scoring").id
    make_permission(session, user_id=ana_id, project_id=project, capability="view")
    script = {"id": "ctl-script-1", "type": "script", "position": {"x": 0, "y": 0}, "data": {"environmentId": 999}}
    assert ana.get(f"/flows/{_flow(session, project, script)}").status_code == 200


def test_without_kubernetes_a_flow_with_script_and_container_nodes_is_kept(app, session):
    """Docker Compose: no scripting, no containers. A flow made on Kubernetes is saved and
    copied as it is; its nodes do not run there (the run says so)."""
    from app.core.security import hash_password

    make_user(session, email="boss@x.local", is_superuser=True, hashed_password=hash_password(PASSWORD))
    ana, ana_id = _login(app, session, "ana@x.local")
    project = make_project(session, name="Imported").id
    make_permission(session, user_id=ana_id, project_id=project, capability="edit")
    script = {"id": "s1", "type": "script", "position": {"x": 0, "y": 0},
              "data": {"label": "Score", "environmentId": 7, "files": [{"path": "main.py", "content": "print(1)"}],
                       "entrypoint": "main.py"}}
    container = _node(9)
    definition = json.dumps({"nodes": [script, container], "edges": []})
    r = ana.post(f"/projects/{project}/flows", json={"name": "From Kubernetes", "definition": definition}, headers=CSRF)
    assert r.status_code == 201, r.text
    assert ana.post(f"/flows/{r.json()['id']}/duplicate", headers=CSRF).status_code == 201
