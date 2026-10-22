"""Saved scripts, piece 4: the Script node by reference (services/script_runs.py,
services/script_nodes.py, services/flow_access.py, services/schedule_authority.py,
routes/scripts.py; docs/specs/saved-scripts.md «The Script node, by reference»).

Fixed here:
- a node with `scriptId` runs the script's current version: its files, environment,
  timeout; its declared inputs come too (TABULARIA_INPUT_<ALIAS>, TABULARIA_INPUTS),
  next to the edge's TABULARIA_INPUT; the run records which script and version;
- a script deleted since, or one the runner may no longer see, fails the node;
- only a script one sees can be named in a flow; whoever cannot see it cannot open
  the flow, and is not told its name;
- a script used by a flow cannot be deleted; a widening change by someone else
  switches off the schedules of the flows that run it, set by others;
- the preview of a node by reference is stale once the script has a new version.
"""
import json

import pytest
from fastapi import HTTPException
from sqlmodel import select

from app.models import AuditLog, Run, SavedScript
from app.services import flow_access, saved_scripts, schedule_authority, script_nodes
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_user
from tests.test_saved_script_runs import CSRF, Engine, _login, _script, _usable, app, queue  # noqa: F401
from tests.test_script_runs import (  # noqa: F401 — the shared fixtures
    SOURCE, _edge, _environment, _run, cluster, launches,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
def engine(monkeypatch):
    import httpx

    e = Engine()
    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(e.handle), base_url="http://engine"))
    return e


def _ref(script_id, node_id="s1"):
    return {"id": node_id, "type": "script", "data": {"label": "Score", "scriptId": script_id}}


def _flow(session, project_id, nodes, edges=()):
    return make_flow(session, name="scoring", project_id=project_id,
                     definition=json.dumps({"nodes": nodes, "edges": list(edges)}))


def _env_of(job: dict) -> dict:
    return {e["name"]: e["value"] for e in job["spec"]["template"]["spec"]["containers"][0]["env"]}


async def test_a_node_by_reference_runs_the_scripts_current_version_with_its_inputs(session, cluster, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    p = make_project(session, name="p")
    ds = make_datasource(session, name="Prices", project_id=p.id, key="datasets/3/prices.parquet")
    script = _script(session, env, p.id, inputs=[{"datasource_id": ds.id, "alias": "PRICES"}],
                     code="print('from the saved script')\n")
    out = {"id": "out", "type": "output", "data": {"destType": "datasource", "name": "scored", "projectId": p.id}}
    flow = _flow(session, p.id, [SOURCE, _ref(script.id), out], [_edge("src", "s1"), _edge("s1", "out")])
    head, errors = await _run(session, admin, flow)
    assert errors == []
    job = cluster.created[0][2]
    vars_ = _env_of(job)
    # the edge's input and the declared one, side by side
    assert vars_["TABULARIA_INPUT"].endswith("/input.parquet")
    assert vars_["TABULARIA_INPUT_PRICES"].endswith("/in-PRICES.parquet") and engine.exports[0]["key"] == ds.key
    assert set(json.loads(vars_["TABULARIA_INPUTS"])) == {"PRICES"} and vars_["TABULARIA_SCRIPT_VERSION"] == "1"
    # the script's code, mounted
    configmap = next(body for kind, _, body in cluster.created if kind == "configmaps")
    assert "from the saved script" in json.dumps(configmap["data"])
    run = session.exec(select(Run).where(Run.kind == "script")).one()
    info = json.loads(run.script)
    assert info["saved_script_id"] == script.id and info["script_version"] == 1 and run.flow_id == flow.id
    assert run.saved_script_id is None  # a flow's node: read with the flow's rights, not the script's
    assert engine.exports[0]["target_key"] in engine.deletes[0]["keys"]


async def test_a_script_deleted_since_fails_the_node(session, cluster, engine, launches):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    p = make_project(session, name="p")
    flow = _flow(session, p.id, [_ref(9999)])
    from app.services.orchestrator import OrchestrationError

    with pytest.raises(OrchestrationError, match="runs a saved script that no longer exists"):
        await _run(session, admin, flow)
    assert cluster.created == []


def test_only_a_visible_script_can_be_named_and_its_flow_is_closed_to_others(session, cluster):
    user = make_user(session, email="u@x.local")
    stranger = make_user(session, email="s@x.local")
    env = _usable(session, user)
    p = make_project(session, name="p")
    for who in (user, stranger):
        make_permission(session, user_id=who.id, project_id=p.id, capability="view")
    script = _script(session, env, p.id)
    definition = json.dumps({"nodes": [_ref(script.id)], "edges": []})
    script_nodes.check_definition(session, user, None, definition)  # it sees it
    with pytest.raises(HTTPException) as e:
        script_nodes.check_definition(session, stranger, None, definition)
    assert e.value.status_code == 422 and "choose a saved script you may see" in e.value.detail
    assert flow_access.blocked(session, user, definition) is None
    why = flow_access.blocked(session, stranger, definition)
    assert why == "This flow runs a saved script you have no access to" and "Totals" not in why
    # unchanged on a later save by someone who sees the flow: not checked again
    script_nodes.check_definition(session, stranger, definition, definition)


def test_a_flows_schedule_needs_what_its_saved_script_reads(session):
    user = make_user(session, email="u@x.local")
    env = _usable(session, user)
    p = make_project(session, name="p")
    other = make_project(session, name="other")
    for cap in ("view", "run"):
        make_permission(session, user_id=user.id, project_id=p.id, capability=cap)
    ds = make_datasource(session, name="Secret prices", project_id=other.id, key="datasets/1/x.parquet")
    script = _script(session, env, p.id, inputs=[{"datasource_id": ds.id, "alias": "PRICES"}])
    flow = _flow(session, p.id, [_ref(script.id)])
    gaps = schedule_authority.gaps_of_flow(session, user, flow)
    assert any("read access to 'Secret prices', input PRICES" in g for g in gaps)
    assert not any("RUN on the folder of the saved script" in g for g in gaps)  # the flow's folder gives it
    # naming another saved script widens what a schedule reaches
    assert ("saved_script", script.id) in schedule_authority.widens(json.dumps({"nodes": []}), flow.definition)


def test_the_preview_is_stale_once_the_script_changes(session):
    admin = make_user(session, email="a@x.local", is_superuser=True)
    env = _environment(session)
    p = make_project(session, name="p")
    script = _script(session, env, p.id)
    node = _ref(script.id)
    before = script_nodes.fingerprint(session, node)
    saved_scripts.add_version(session, script, {**saved_scripts.content_of(session, script),
                                                "files": [{"path": "main.py", "content": "print(2)\n"}]}, "edited", admin)
    session.commit()
    assert script_nodes.fingerprint(session, node) != before


async def test_used_by_a_flow_it_stays_and_a_widening_change_stops_others_schedules(app, session, cluster):
    boss = _login(app, session, "boss@x.local", is_superuser=True)
    from app.models import User

    planner = make_user(session, email="planner@x.local", is_superuser=True)
    planner_id = planner.id
    env_id = _environment(session).id
    pid = make_project(session, name="p").id
    script_id = _script(session, env_id, pid).id
    flow = _flow(session, pid, [_ref(script_id)])
    flow.run_schedule, flow.run_scheduled_by = "0 6 * * *", planner_id
    session.add(flow)
    session.commit()
    flow_id = flow.id
    r = boss.delete(f"/scripts/{script_id}", headers=CSRF)
    assert r.status_code == 409 and r.json()["detail"]["flows"] == ["scoring"]
    code = {"files": [{"path": "main.py", "content": "print('other code')\n"}], "entrypoint": "main.py"}
    assert boss.patch(f"/scripts/{script_id}", json={"content": code}, headers=CSRF).status_code == 200
    session.expire_all()
    from app.models import Flow

    assert session.get(Flow, flow_id).run_schedule is None
    off = session.exec(select(AuditLog).where(AuditLog.action == "flow.schedule")).all()[-1]
    detail = json.loads(off.detail) if isinstance(off.detail, str) else off.detail
    assert detail["author"] == planner_id and "the saved script 'Totals' it runs changed: its code changed" in detail["reason"]
    assert session.get(SavedScript, script_id) is not None
    assert session.exec(select(User).where(User.email == "boss@x.local")).one()
