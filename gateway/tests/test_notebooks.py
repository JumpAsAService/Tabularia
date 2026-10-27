"""Notebooks, piece 1: the object, its versions, who sees it (routes/notebooks.py,
services/notebooks.py; docs/specs/notebooks.md).

Fixed here:
- a notebook is seen with VIEW on its folder AND the use of its environment, as a saved
  script; whoever lacks either gets 404 and does not find it in the lists nor in the
  search; administrators see them all; EDIT changes, moves (EDIT on both folders),
  duplicates, deletes;
- a change of the source is a new version, whose author is whoever saved it: the app
  reads with their rights; the same source makes none; a restore is a new version by
  whoever restores it; a duplicate's first version is its copier's;
- a new notebook starts from a valid marimo file; an empty source is refused; an R
  environment is refused;
- the editor's draft is shown only to whoever may edit, goes with a saved version, and
  can be discarded;
- without scripting (Docker Compose) the lists are empty and nothing is saved;
- a folder or an environment holding a notebook cannot be deleted; deleting the author
  of a version leaves the version without one.
"""
import ast
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.db.session import get_session
from app.models import (
    AuditLog, Group, Notebook, NotebookVersion, ScriptEnvironment, ScriptEnvironmentGroup, UserGroupLink,
)
from app.services.notebooks import starter_source
from tests.conftest import make_permission, make_project, make_user

CSRF = {"X-Tabularia-Csrf": "1"}
PASSWORD = "una-password-lunga"
SOURCE = "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    return\n"


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
    from app.routes import notebooks as notebooks_routes
    from app.routes import projects as projects_routes
    from app.routes import scripting as scripting_routes
    from app.routes import search as search_routes

    app = FastAPI()
    for r in (auth_routes.router, notebooks_routes.router, search_routes.router, projects_routes.router,
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
    """An administrator, ana and cleo (in «data science», which may use Python) and ben
    (not in it), a folder the three may edit, and the Python environment."""
    boss, _ = _login(app, session, "boss@x.local", is_superuser=True)
    ana, ana_id = _login(app, session, "ana@x.local")
    ben, ben_id = _login(app, session, "ben@x.local")
    cleo, cleo_id = _login(app, session, "cleo@x.local")
    env = ScriptEnvironment(name="Python", language="python")
    g = Group(name="data science")
    session.add_all([env, g])
    session.commit()
    env_id, g_id = env.id, g.id
    session.add_all([ScriptEnvironmentGroup(environment_id=env_id, group_id=g_id),
                     UserGroupLink(user_id=ana_id, group_id=g_id), UserGroupLink(user_id=cleo_id, group_id=g_id)])
    session.commit()
    folder = make_project(session, name="Analyses").id
    for uid in (ana_id, ben_id, cleo_id):
        make_permission(session, user_id=uid, project_id=folder, capability="edit")
    return boss, ana, ben, cleo, env_id, folder


def _create(c, folder, env_id, name="Sales app", source=SOURCE):
    body = {"name": name, "environment_id": env_id}
    if source is not None:
        body["content"] = {"source": source}
    return c.post(f"/projects/{folder}/notebooks", json=body, headers=CSRF)


def test_a_notebook_is_created_with_its_first_version_and_audited(app, session, scripting):
    _, ana, _, _, env_id, folder = _world(app, session)
    r = _create(ana, folder, env_id)
    assert r.status_code == 201, r.text
    n = r.json()
    assert n["current_version"] == 1 and n["environment_name"] == "Python" and n["language"] == "python"
    assert n["content"] == {"source": SOURCE} and n["editable"] is True
    assert n["author"]["email"] == "ana@x.local" and n["draft"] is None
    assert session.exec(select(AuditLog.action).where(AuditLog.action == "notebook.create")).one()
    assert _create(ana, folder, env_id).status_code == 409  # the same name in the folder


def test_a_new_notebook_starts_from_a_valid_marimo_file(app, session, scripting):
    _, ana, _, _, env_id, folder = _world(app, session)
    n = _create(ana, folder, env_id, name='Sales "Q3"', source=None).json()
    source = n["content"]["source"]
    assert source == starter_source('Sales "Q3"')
    tree = ast.parse(source)  # valid Python, whatever the name holds
    assert "app = marimo.App()" in source and source.count("@app.cell") == 3
    assert "mo.md('# Sales \"Q3\"')" in source
    # tabularia is shown, not imported: the module comes with the runtime
    assert not any(isinstance(node, ast.Import) and node.names[0].name == "tabularia" for node in ast.walk(tree))
    assert '# df = tabularia.read("Sample/orders")' in source


def test_the_source_is_checked(app, session, scripting):
    _, ana, _, _, env_id, folder = _world(app, session)
    assert _create(ana, folder, env_id, source="  \n").status_code == 422
    r = ana.post(f"/projects/{folder}/notebooks", json={"name": "X", "environment_id": env_id, "content": {"source": 3}},
                 headers=CSRF)
    assert r.status_code == 422
    assert _create(ana, folder, env_id, source="x" * (800 * 1024)).status_code == 422
    # what is not the source is not kept
    r = ana.post(f"/projects/{folder}/notebooks", json={"name": "Y", "environment_id": env_id,
                                                        "content": {"source": SOURCE, "inputs": [{"alias": "A"}]}},
                 headers=CSRF)
    assert r.json()["content"] == {"source": SOURCE}


def test_an_r_environment_is_refused(app, session, scripting):
    boss, _, _, _, env_id, folder = _world(app, session)
    r_env = ScriptEnvironment(name="R stats", language="r")
    session.add(r_env)
    session.commit()
    r = _create(boss, folder, r_env.id)
    assert r.status_code == 422 and "Python environment" in r.json()["detail"] and "R stats" in r.json()["detail"]
    nid = _create(boss, folder, env_id).json()["id"]
    assert boss.patch(f"/notebooks/{nid}", json={"environment_id": r_env.id}, headers=CSRF).status_code == 422


def test_who_sees_a_notebook(app, session, scripting):
    boss, ana, ben, _, env_id, folder = _world(app, session)
    nid = _create(ana, folder, env_id).json()["id"]
    # ben may edit the folder but not use the environment: the notebook does not exist for him
    assert ben.get(f"/notebooks/{nid}").status_code == 404
    assert ben.get(f"/projects/{folder}/notebooks").json() == []
    assert ben.get("/notebooks").json() == []
    assert ben.get("/search", params={"q": "sales"}).json()["counts"].get("notebook") is None
    assert ben.patch(f"/notebooks/{nid}", json={"name": "Mine"}, headers=CSRF).status_code == 404
    assert ben.get(f"/notebooks/{nid}/versions").status_code == 404
    assert ben.delete(f"/notebooks/{nid}", headers=CSRF).status_code == 404
    # ana sees it everywhere; the administrator too
    assert [x["id"] for x in ana.get(f"/projects/{folder}/notebooks").json()] == [nid]
    hits = ana.get("/search", params={"q": "sales"}).json()["items"]
    assert [(h["kind"], h["id"]) for h in hits if h["kind"] == "notebook"] == [("notebook", nid)]
    assert boss.get(f"/notebooks/{nid}").status_code == 200 and len(boss.get("/notebooks").json()) == 1
    # the environment, without the folder: not seen either
    dan, dan_id = _login(app, session, "dan@x.local")
    gid = session.exec(select(Group.id)).one()
    session.add(UserGroupLink(user_id=dan_id, group_id=gid))
    session.commit()
    assert dan.get(f"/notebooks/{nid}").status_code == 404 and dan.get("/notebooks").json() == []
    # VIEW only: seen, not changed
    make_permission(session, user_id=dan_id, project_id=folder, capability="view")
    assert dan.get(f"/notebooks/{nid}").json()["editable"] is False
    assert dan.patch(f"/notebooks/{nid}", json={"description": "x"}, headers=CSRF).status_code == 403
    assert dan.post(f"/notebooks/{nid}/duplicate", headers=CSRF).status_code == 403
    assert dan.delete(f"/notebooks/{nid}", headers=CSRF).status_code == 403


def test_versions_and_their_authors(app, session, scripting):
    _, ana, _, cleo, env_id, folder = _world(app, session)
    nid = _create(ana, folder, env_id).json()["id"]
    # a name or a description is no version
    r = ana.patch(f"/notebooks/{nid}", json={"description": "the sales", "content": {"source": SOURCE}}, headers=CSRF)
    assert r.json()["current_version"] == 1 and r.json()["description"] == "the sales"
    # cleo saves new code: the app now reads with her rights
    changed = SOURCE + "\n# more\n"
    r = cleo.patch(f"/notebooks/{nid}", json={"content": {"source": changed}}, headers=CSRF)
    assert r.json()["current_version"] == 2 and r.json()["author"]["email"] == "cleo@x.local"
    versions = ana.get(f"/notebooks/{nid}/versions").json()
    assert [(v["version"], v["note"], v["created_by"], v["current"]) for v in versions] == [
        (2, "edited", "cleo@x.local", True), (1, "created", "ana@x.local", False)]
    assert ana.get(f"/notebooks/{nid}/versions/1").json() == {"source": SOURCE}
    assert ana.get(f"/notebooks/{nid}/versions/9").status_code == 404
    # ana restores v1: a new version, hers, not cleo's nor the original's by right
    r = ana.post(f"/notebooks/{nid}/versions/1/restore", headers=CSRF)
    assert r.json()["current_version"] == 3 and r.json()["content"] == {"source": SOURCE}
    assert r.json()["author"]["email"] == "ana@x.local"
    assert ana.get(f"/notebooks/{nid}/versions").json()[0]["note"] == "restored from v1"
    # the current one restored: nothing new
    assert ana.post(f"/notebooks/{nid}/versions/3/restore", headers=CSRF).json()["current_version"] == 3
    actions = [a for a in session.exec(select(AuditLog.action).order_by(AuditLog.id)).all() if a.startswith("notebook.")]
    assert actions == ["notebook.create", "notebook.update", "notebook.update", "notebook.restore"]


def test_the_draft(app, session, scripting):
    _, ana, _, _, env_id, folder = _world(app, session)
    nid = _create(ana, folder, env_id).json()["id"]
    ana_id = session.exec(select(Notebook.owner_id)).one()
    nb = session.get(Notebook, nid)
    nb.draft, nb.draft_by = SOURCE + "# wip\n", ana_id
    session.add(nb)
    session.commit()
    detail = ana.get(f"/notebooks/{nid}").json()
    assert detail["draft"] == SOURCE + "# wip\n" and detail["draft_by"] == "ana@x.local"
    # a reader does not see the editor's unsaved work
    viewer, viewer_id = _login(app, session, "vera@x.local")
    session.add(UserGroupLink(user_id=viewer_id, group_id=session.exec(select(Group.id)).one()))
    session.commit()
    make_permission(session, user_id=viewer_id, project_id=folder, capability="view")
    assert viewer.get(f"/notebooks/{nid}").json()["draft"] is None
    assert viewer.delete(f"/notebooks/{nid}/draft", headers=CSRF).status_code == 403
    # discarded
    assert ana.delete(f"/notebooks/{nid}/draft", headers=CSRF).json()["draft"] is None
    # a saved version takes its place
    nb = session.get(Notebook, nid)
    nb.draft, nb.draft_by = "draft", ana_id
    session.add(nb)
    session.commit()
    r = ana.patch(f"/notebooks/{nid}", json={"content": {"source": SOURCE + "# saved\n"}}, headers=CSRF)
    assert r.json()["current_version"] == 2 and r.json()["draft"] is None


def test_move_duplicate_delete(app, session, scripting):
    _, ana, _, cleo, env_id, folder = _world(app, session)
    nid = _create(ana, folder, env_id).json()["id"]
    other = make_project(session, name="Elsewhere").id
    assert ana.patch(f"/notebooks/{nid}", json={"project_id": other}, headers=CSRF).status_code == 403  # no EDIT there
    ana_id = session.exec(select(Notebook.owner_id)).one()
    make_permission(session, user_id=ana_id, project_id=other, capability="edit")
    r = ana.patch(f"/notebooks/{nid}", json={"project_id": other, "name": "Moved"}, headers=CSRF)
    assert r.json()["project_id"] == other and r.json()["name"] == "Moved" and r.json()["current_version"] == 1
    # cleo has no rights on «Elsewhere»: there the notebook does not exist for her
    assert cleo.post(f"/notebooks/{nid}/duplicate", headers=CSRF).status_code == 404  # no VIEW there
    copy = ana.post(f"/notebooks/{nid}/duplicate", headers=CSRF).json()
    assert copy["name"] == "Moved (1)" and copy["current_version"] == 1 and copy["content"] == {"source": SOURCE}
    assert copy["author"]["email"] == "ana@x.local"
    assert ana.delete(f"/notebooks/{nid}", headers=CSRF).status_code == 204
    assert session.exec(select(NotebookVersion).where(NotebookVersion.notebook_id == nid)).all() == []
    assert ana.get(f"/notebooks/{nid}").status_code == 404
    actions = {a for a in session.exec(select(AuditLog.action)).all() if a.startswith("notebook.")}
    assert actions == {"notebook.create", "notebook.update", "notebook.delete"}


def test_a_copy_is_by_whoever_copies_it(app, session, scripting):
    _, ana, _, cleo, env_id, folder = _world(app, session)
    nid = _create(ana, folder, env_id).json()["id"]
    copy = cleo.post(f"/notebooks/{nid}/duplicate", headers=CSRF).json()
    assert copy["author"]["email"] == "cleo@x.local" and copy["name"] == "Sales app (1)"


def test_without_scripting_nothing_shows(app, session):
    _, ana, _, _, env_id, folder = _world(app, session)
    session.add(Notebook(name="Old", project_id=folder, environment_id=env_id))
    session.commit()
    assert ana.get("/notebooks").json() == [] and ana.get(f"/projects/{folder}/notebooks").json() == []
    assert _create(ana, folder, env_id).status_code == 409
    assert ana.get("/search", params={"q": "old"}).json()["counts"].get("notebook") is None


def test_a_folder_or_an_environment_holding_a_notebook_cannot_be_deleted(app, session, scripting):
    boss, ana, _, _, env_id, folder = _world(app, session)
    _create(ana, folder, env_id)
    r = boss.delete(f"/projects/{folder}", headers=CSRF)
    assert r.status_code == 409 and "notebooks" in r.text
    r = boss.delete(f"/admin/scripting/environments/{env_id}", headers=CSRF)
    assert r.status_code == 409 and json.loads(r.text)["detail"]["notebooks"] == ["Sales app"]


def test_deleting_the_author_of_a_version_leaves_it_without_one(fk_session):
    from app.routes.users import remove_user

    s = fk_session
    gone = make_user(s, email="gone@x.local")
    env = ScriptEnvironment(name="Python", language="python")
    s.add(env)
    s.commit()
    nb = Notebook(name="n", project_id=make_project(s, name="p").id, environment_id=env.id, owner_id=gone.id,
                  draft="x", draft_by=gone.id)
    s.add(nb)
    s.commit()
    s.add(NotebookVersion(notebook_id=nb.id, version=1, content="{}", created_by=gone.id))
    s.commit()
    nb_id = nb.id
    remove_user(s, gone.id)
    s.expunge_all()
    nb = s.get(Notebook, nb_id)
    assert nb.owner_id is None and nb.draft_by is None
    assert s.exec(select(NotebookVersion.created_by)).all() == [None]
