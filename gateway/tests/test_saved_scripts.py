"""Saved scripts, piece 1: the object, its versions, who sees it (routes/scripts.py,
services/saved_scripts.py; docs/specs/saved-scripts.md).

Fixed here:
- a script is seen with VIEW on its folder AND the use of its environment; whoever lacks
  either gets 404 and does not find it in the lists nor in the search; administrators
  see them all; EDIT changes, moves (EDIT on both folders), duplicates, deletes;
- a change of the content is a new version that becomes the one that runs; the same
  content makes none; an older version is restored as the newest;
- inputs are datasources by id with an alias: aliases well formed and unique, a
  datasource one may not read is refused, two homonymous datasources of two folders
  are two inputs; a datasource gone since stays listed;
- the output's publish folder needs EDIT; the Script node's checks hold for the files;
- without scripting (Docker Compose) the lists are empty and nothing is saved;
- a folder or an environment holding a script cannot be deleted.
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.db.session import get_session
from app.models import (
    AuditLog, Datasource, Group, SavedScript, SavedScriptVersion, ScriptEnvironment, ScriptEnvironmentGroup,
    UserGroupLink,
)
from app.services.saved_scripts import propose_alias
from tests.conftest import make_permission, make_project, make_user

CSRF = {"X-Tabularia-Csrf": "1"}
PASSWORD = "una-password-lunga"
MAIN = {"path": "main.py", "content": "import os\nprint(os.environ.get('TABULARIA_INPUT_SALES'))\n"}


@pytest.fixture
def scripting(monkeypatch):
    for k, v in {"SCRIPTS__ENABLED": "true", "SCRIPTS__NAMESPACE": "tab-scripts",
                 "SCRIPTS__REGISTRY": "registry.example.com/tab"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def app(session):
    from app.routes import auth as auth_routes
    from app.routes import projects as projects_routes
    from app.routes import scripting as scripting_routes
    from app.routes import scripts as scripts_routes
    from app.routes import search as search_routes

    app = FastAPI()
    for r in (auth_routes.router, scripts_routes.router, search_routes.router, projects_routes.router,
              scripting_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _login(app, session, email, **kw):
    from app.core.security import hash_password

    uid = make_user(session, email=email, hashed_password=hash_password(PASSWORD), **kw).id
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF).status_code == 200
    return c, uid


def _world(app, session):
    """An administrator, ana (in «data science», which may use Python) and ben (not in
    it), a folder both may edit, and the Python environment."""
    boss, _ = _login(app, session, "boss@x.local", is_superuser=True)
    ana, ana_id = _login(app, session, "ana@x.local")
    ben, ben_id = _login(app, session, "ben@x.local")
    env = ScriptEnvironment(name="Python", language="python")
    g = Group(name="data science")
    session.add_all([env, g])
    session.commit()
    env_id, g_id = env.id, g.id
    session.add_all([ScriptEnvironmentGroup(environment_id=env_id, group_id=g_id),
                     UserGroupLink(user_id=ana_id, group_id=g_id)])
    session.commit()
    folder = make_project(session, name="Forecasting").id
    for uid in (ana_id, ben_id):
        make_permission(session, user_id=uid, project_id=folder, capability="edit")
    return boss, ana, ben, env_id, folder


def _create(c, folder, env_id, name="Forecast", **content):
    r = c.post(f"/projects/{folder}/scripts", json={"name": name, "environment_id": env_id,
                                                    "content": {"files": [MAIN], "entrypoint": "main.py", **content}},
               headers=CSRF)
    return r


def test_a_script_is_created_with_its_first_version_and_audited(app, session, scripting):
    _, ana, _, env_id, folder = _world(app, session)
    r = _create(ana, folder, env_id)
    assert r.status_code == 201, r.text
    s = r.json()
    assert s["current_version"] == 1 and s["environment_name"] == "Python" and s["language"] == "python"
    assert s["content"]["files"] == [MAIN] and s["content"]["entrypoint"] == "main.py" and s["editable"] is True
    assert s["content"]["output"] == {"publish": False, "name": "", "project_id": None, "overwrite": True}
    assert session.exec(select(AuditLog.action).where(AuditLog.action == "script.create")).one()
    assert _create(ana, folder, env_id).status_code == 409  # the same name in the folder


def test_who_sees_a_script(app, session, scripting):
    boss, ana, ben, env_id, folder = _world(app, session)
    sid = _create(ana, folder, env_id).json()["id"]
    # ben may edit the folder but not use the environment: the script does not exist for him
    assert ben.get(f"/scripts/{sid}").status_code == 404
    assert ben.get(f"/projects/{folder}/scripts").json() == []
    assert ben.get("/scripts").json() == []
    assert ben.get("/search", params={"q": "fore"}).json()["counts"].get("script") is None
    assert ben.patch(f"/scripts/{sid}", json={"name": "Mine"}, headers=CSRF).status_code == 404
    assert ben.delete(f"/scripts/{sid}", headers=CSRF).status_code == 404
    # ana sees it everywhere; the administrator too
    assert [x["id"] for x in ana.get(f"/projects/{folder}/scripts").json()] == [sid]
    hits = ana.get("/search", params={"q": "fore"}).json()["items"]
    assert [(h["kind"], h["id"]) for h in hits if h["kind"] == "script"] == [("script", sid)]
    assert boss.get(f"/scripts/{sid}").status_code == 200 and len(boss.get("/scripts").json()) == 1
    # the environment, without the folder: not seen either
    carl, carl_id = _login(app, session, "carl@x.local")
    gid = session.exec(select(Group.id)).one()
    session.add(UserGroupLink(user_id=carl_id, group_id=gid))
    session.commit()
    assert carl.get(f"/scripts/{sid}").status_code == 404 and carl.get("/scripts").json() == []
    # VIEW only: seen, not changed
    make_permission(session, user_id=carl_id, project_id=folder, capability="view")
    assert carl.get(f"/scripts/{sid}").json()["editable"] is False
    assert carl.patch(f"/scripts/{sid}", json={"description": "x"}, headers=CSRF).status_code == 403


def test_versions(app, session, scripting):
    _, ana, _, env_id, folder = _world(app, session)
    sid = _create(ana, folder, env_id).json()["id"]
    v2 = {"files": [{"path": "main.py", "content": "print(2)\n"}], "entrypoint": "main.py"}
    r = ana.patch(f"/scripts/{sid}", json={"content": v2}, headers=CSRF)
    assert r.json()["current_version"] == 2
    # the same content again: no version
    assert ana.patch(f"/scripts/{sid}", json={"content": v2}, headers=CSRF).json()["current_version"] == 2
    # a rename is not a version
    assert ana.patch(f"/scripts/{sid}", json={"name": "Forecast 2"}, headers=CSRF).json()["current_version"] == 2
    versions = ana.get(f"/scripts/{sid}/versions").json()
    assert [(v["version"], v["note"], v["current"]) for v in versions] == [(2, "edited", True), (1, "created", False)]
    assert versions[0]["created_by"] == "ana@x.local"
    assert ana.get(f"/scripts/{sid}/versions/1").json()["files"] == [MAIN]
    r = ana.post(f"/scripts/{sid}/versions/1/restore", headers=CSRF)
    assert r.json()["current_version"] == 3 and r.json()["content"]["files"] == [MAIN]
    assert ana.get(f"/scripts/{sid}/versions").json()[0]["note"] == "restored from v1"


def test_move_duplicate_delete(app, session, scripting):
    _, ana, _, env_id, folder = _world(app, session)
    sid = _create(ana, folder, env_id).json()["id"]
    other = make_project(session, name="Elsewhere").id
    assert ana.patch(f"/scripts/{sid}", json={"project_id": other}, headers=CSRF).status_code == 403  # no EDIT there
    ana_id = session.exec(select(UserGroupLink.user_id)).first()
    make_permission(session, user_id=ana_id, project_id=other, capability="edit")
    assert ana.patch(f"/scripts/{sid}", json={"project_id": other}, headers=CSRF).json()["project_id"] == other
    copy = ana.post(f"/scripts/{sid}/duplicate", headers=CSRF).json()
    assert copy["name"] == "Forecast (1)" and copy["current_version"] == 1 and copy["content"]["files"] == [MAIN]
    assert ana.delete(f"/scripts/{sid}", headers=CSRF).status_code == 204
    assert session.exec(select(SavedScriptVersion).where(SavedScriptVersion.script_id == sid)).all() == []
    assert ana.get(f"/scripts/{sid}").status_code == 404


def test_inputs_are_datasources_by_id_with_an_alias(app, session, scripting):
    _, ana, _, env_id, folder = _world(app, session)
    north = make_project(session, name="North").id
    south = make_project(session, name="South").id
    secret = make_project(session, name="Secret").id
    ana_id = session.exec(select(UserGroupLink.user_id)).first()
    for p in (north, south):
        make_permission(session, user_id=ana_id, project_id=p, capability="view")
    ids = {}
    for p, name in ((north, "Vendite Nord 2024"), (south, "Vendite Nord 2024"), (secret, "Salaries")):
        d = Datasource(name=name, project_id=p, bucket="b", key=f"datasets/{p}.parquet", kind="flow")
        session.add(d)
        session.commit()
        ids[p] = d.id
    # two homonymous datasources of two folders: two inputs, two aliases
    two = [{"datasource_id": ids[north], "alias": "VENDITE_NORD_2024"},
           {"datasource_id": ids[south], "alias": "VENDITE_NORD_2024_SOUTH"}]
    r = _create(ana, folder, env_id, inputs=two)
    assert r.status_code == 201 and r.json()["content"]["inputs"] == two
    sid = r.json()["id"]
    bad = [
        ([{"datasource_id": ids[north], "alias": "vendite nord"}], 422, "is not an alias"),
        ([{"datasource_id": ids[north], "alias": "A"}, {"datasource_id": ids[south], "alias": "A"}], 422, "two inputs are called A"),
        ([{"datasource_id": ids[north], "alias": "A"}, {"datasource_id": ids[north], "alias": "B"}], 422, "twice"),
        ([{"datasource_id": 9999, "alias": "A"}], 422, "does not exist"),
        ([{"datasource_id": ids[secret], "alias": "PAY"}], 403, "cannot read the datasource of PAY"),
        ([{"datasource_id": "12", "alias": "A"}], 422, "by its id"),
    ]
    for inputs, code, message in bad:
        r = ana.patch(f"/scripts/{sid}", json={"content": {"files": [MAIN], "entrypoint": "main.py", "inputs": inputs}},
                      headers=CSRF)
        assert r.status_code == code and message in r.text, (inputs, r.text)
    # a datasource gone since stays: the page shows it missing
    session.delete(session.get(Datasource, ids[south]))
    session.commit()
    r = ana.patch(f"/scripts/{sid}", json={"description": "kept", "content": {
        "files": [MAIN], "entrypoint": "main.py", "inputs": two}}, headers=CSRF)
    assert r.status_code == 200 and r.json()["content"]["inputs"] == two


def test_an_alias_is_proposed_from_the_name():
    assert propose_alias("Vendite Nord 2024", "Sales", set()) == "VENDITE_NORD_2024"
    assert propose_alias("Vendite Nord 2024", "Sales / South", {"VENDITE_NORD_2024"}) == "VENDITE_NORD_2024_SALES_SOUTH"
    assert propose_alias("Vendite", "S", {"VENDITE", "VENDITE_S"}) == "VENDITE_S_2"
    assert propose_alias("Città già 2024", "", set()) == "CITTA_GIA_2024"
    assert propose_alias("2024 sales", "", set()) == "DS_2024_SALES"
    assert propose_alias("!!!", "", set()) == "INPUT"


def test_the_output_published_needs_edit_on_its_folder(app, session, scripting):
    _, ana, _, env_id, folder = _world(app, session)
    readonly = make_project(session, name="Reports").id
    ana_id = session.exec(select(UserGroupLink.user_id)).first()
    make_permission(session, user_id=ana_id, project_id=readonly, capability="view")
    out = {"publish": True, "name": "Forecast", "project_id": readonly, "overwrite": True}
    r = _create(ana, folder, env_id, output=out)
    assert r.status_code == 403 and "publish into that folder" in r.text
    r = _create(ana, folder, env_id, output={**out, "project_id": folder})
    assert r.status_code == 201 and r.json()["content"]["output"]["project_id"] == folder
    assert _create(ana, folder, env_id, name="B", output={**out, "name": ""}).status_code == 422


def test_the_script_node_s_checks_hold(app, session, scripting):
    _, ana, _, env_id, folder = _world(app, session)
    r = _create(ana, folder, env_id, files=[{"path": "../main.py", "content": "x"}])
    assert r.status_code == 422 and "is not a file name" in r.text
    r = _create(ana, folder, env_id, name="B", entrypoint="main.R")
    assert r.status_code == 422 and "is not among its files" in r.text
    r = _create(ana, folder, env_id, name="C", timeout_seconds=5)
    assert r.status_code == 422 and "timeout" in r.text
    other = ScriptEnvironment(name="R", language="r")
    session.add(other)
    session.commit()
    assert _create(ana, folder, other.id, name="D").status_code == 422  # an environment ana may not use


def test_without_scripting_nothing_shows(app, session):
    _, ana, _, env_id, folder = _world(app, session)
    session.add(SavedScript(name="Old", project_id=folder, environment_id=env_id))
    session.commit()
    assert ana.get("/scripts").json() == [] and ana.get(f"/projects/{folder}/scripts").json() == []
    assert _create(ana, folder, env_id).status_code == 409
    assert ana.get("/search", params={"q": "old"}).json()["counts"].get("script") is None


def test_a_folder_or_an_environment_holding_a_script_cannot_be_deleted(app, session, scripting):
    boss, ana, _, env_id, folder = _world(app, session)
    _create(ana, folder, env_id)
    r = boss.delete(f"/projects/{folder}", headers=CSRF)
    assert r.status_code == 409 and "scripts" in r.text
    r = boss.delete(f"/admin/scripting/environments/{env_id}", headers=CSRF)
    assert r.status_code == 409 and json.loads(r.text)["detail"]["scripts"] == ["Forecast"]
