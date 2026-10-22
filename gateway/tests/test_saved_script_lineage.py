"""Saved scripts, piece 5: the lineage and the Scripting tab's list (services/lineage.py,
routes/scripts.py; docs/specs/saved-scripts.md «Explore, search, Scripting tab»,
«Elsewhere»).

Fixed here:
- a script one sees is a node of the lineage: its declared inputs read into it, the
  datasource it publishes comes out of it, a flow that runs it by reference points
  to it; an input in a folder one cannot read is a restricted node, nothing more;
- a script one does not see is not in the graph at all;
- the list of scripts says how each one last ran.
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.db.session import get_session
from app.models import Run
from app.services import lineage
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_user
from tests.test_saved_script_runs import _script, _usable
from tests.test_script_runs import _environment, cluster  # noqa: F401 — the shared fixture


def test_a_script_reads_its_inputs_publishes_its_output_and_is_run_by_flows(session, cluster):
    user = make_user(session, email="u@x.local")
    stranger = make_user(session, email="s@x.local")
    env = _usable(session, user)
    mine = make_project(session, name="Mine")
    hidden = make_project(session, name="Hidden")
    for who in (user, stranger):
        make_permission(session, user_id=who.id, project_id=mine.id, capability="view")
    sales = make_datasource(session, name="Sales", project_id=mine.id, key="datasets/1/s.parquet")
    secret = make_datasource(session, name="Secret", project_id=hidden.id, key="datasets/2/x.parquet")
    published = make_datasource(session, name="Forecast", project_id=mine.id, key="datasets/3/f.parquet")
    script = _script(session, env, mine.id,
                     inputs=[{"datasource_id": sales.id, "alias": "SALES"}, {"datasource_id": secret.id, "alias": "SECRET"}],
                     output={"publish": True, "name": "Forecast", "project_id": mine.id, "overwrite": True})
    flow = make_flow(session, name="Uses it", project_id=mine.id, definition=json.dumps(
        {"nodes": [{"id": "s1", "type": "script", "data": {"label": "X", "scriptId": script.id}}], "edges": []}))
    g = lineage.build_full_graph(session, user)
    node = g.nodes[f"script:{script.id}"]
    assert node["type"] == "script" and node["label"] == "Totals" and node["project_id"] == mine.id
    edges = {(e["source"], e["target"], e["kind"]) for e in g.edges}
    assert (f"ds:{sales.id}", f"script:{script.id}", "read") in edges
    assert (f"script:{script.id}", f"ds:{published.id}", "publish") in edges
    assert (f"flow:{flow.id}", f"script:{script.id}", "orchestrate") in edges
    restricted = g.nodes[f"ds:{secret.id}"]
    assert restricted["restricted"] is True and restricted["label"] != "Secret"
    # someone who cannot use the environment does not see the script at all
    g2 = lineage.build_full_graph(session, stranger)
    assert f"script:{script.id}" not in g2.nodes
    assert not any(f"script:{script.id}" in (e["source"], e["target"]) for e in g2.edges)


def test_the_list_says_how_each_script_last_ran(session, cluster):
    from app.routes import auth as auth_routes
    from app.routes import scripts as scripts_routes
    from app.core.security import hash_password

    app = FastAPI()
    for r in (auth_routes.router, scripts_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    make_user(session, email="boss@x.local", hashed_password=hash_password("una-password-lunga"), is_superuser=True)
    env_id = _environment(session).id
    pid = make_project(session, name="p").id
    first = _script(session, env_id, pid)
    first.name = "Never ran"
    session.add(first)
    session.commit()
    never = first.id
    ran_id = _script(session, env_id, pid).id
    for status in ("FAILURE", "SUCCESS"):
        session.add(Run(kind="orchestration", saved_script_id=ran_id, task_id="", status=status, input_key="",
                        output_bucket="", output_key=""))
        session.commit()
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": "boss@x.local", "password": "una-password-lunga"}).status_code == 200
    rows = {r["id"]: r for r in c.get("/scripts").json()}
    assert rows[never]["last_run_status"] is None
    assert rows[ran_id]["last_run_status"] == "SUCCESS" and rows[ran_id]["last_run_at"]
