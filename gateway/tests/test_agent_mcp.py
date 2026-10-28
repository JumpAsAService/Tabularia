"""The agent's core and the MCP server (docs/specs/mcp-agent.md, piece 1).

Pinned here, with real JSON-RPC requests on `/mcp`: a read token sees and calls only the
reading tools, a write token all of them; a write tool changes nothing by itself and adds
an action to a plan; the plan is applied once, by its owner, before it expires, with the
permissions checked AGAIN (a permission taken away in between stops it); every applied
action is audited with `via: "mcp"`, the token and the plan; an email Output outside the
connection's domains is refused when it is added; a saved flow is changed through a draft
whose plan shows the difference; the connections' listing carries no secret; the
administrator's switch turns the endpoint off; with «confirm in Tabularia only» a plan is
confirmed by a person from its page.
"""
import json
from contextlib import nullcontext
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_session
from app.models import AgentDraft, AgentPlan, AuditLog, Connection, Flow, FlowVersion, Permission
from app.routes import agent_plans as plans_routes
from app.routes import auth as auth_routes
from app.routes import mcp as mcp_routes
from app.services import api_tokens, housekeeping
from app.services.agent import REGISTRY, available, common
from app.services.agent.common import now
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_user

PASSWORD = "una-password-lunga"
CSRF = {"X-Tabularia-Csrf": "1"}
COLUMNS = [{"name": "status", "dtype": "String"}, {"name": "amount", "dtype": "Float64"}]


@pytest.fixture
def app(session, fake_engine, monkeypatch):
    app = FastAPI()
    for r in (auth_routes.router, mcp_routes.router, plans_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    monkeypatch.setattr(common, "new_session", lambda: nullcontext(session))
    return app


@pytest.fixture
def world(session):
    """Anna edits «Sales» and uses its connections; Bruno is someone else. Ids only: the
    endpoint hands the shared session back, and objects loaded here would go stale."""
    anna = make_user(session, email="anna@x.local", hashed_password=hash_password(PASSWORD))
    bruno = make_user(session, email="bruno@x.local", hashed_password=hash_password(PASSWORD))
    sales = make_project(session, name="Sales")
    secret = make_project(session, name="Secret")
    make_permission(session, user_id=anna.id, project_id=sales.id, capability="edit")
    make_permission(session, user_id=anna.id, project_id=sales.id, capability="connect")
    orders = make_datasource(session, name="orders", project_id=sales.id, key="datasets/1/o.parquet", rows=10,
                             columns=json.dumps(COLUMNS), description="Orders")
    db = Connection(name="Warehouse", project_id=sales.id, db_type="postgresql", host="db.internal.example",
                    username="loader", password_encrypted="gAAAA-very-secret", database="dwh")
    mail = Connection(name="Mail", project_id=sales.id, db_type="smtp", host="smtp.internal.example",
                      username="mailer", password_encrypted="gAAAA-mail-secret",
                      extra=json.dumps({"allowed_domains": ["acme.com"]}))
    hidden = Connection(name="Hidden", project_id=secret.id, db_type="postgresql", host="h", password_encrypted="x")
    session.add_all([db, mail, hidden])
    session.commit()
    ids = dict(anna=anna.id, bruno=bruno.id, sales=sales.id, secret=secret.id, orders=orders.id, db=db.id,
               mail=mail.id, hidden=hidden.id)
    _, ids["write"] = api_tokens.create(session, anna, "claude desktop", "write", 30, anna)
    _, ids["read"] = api_tokens.create(session, anna, "reader", "read", 30, anna)
    _, ids["bruno_write"] = api_tokens.create(session, bruno, "bruno's", "write", 30, bruno)
    return ids


def rpc(client, token, method, params=None, msg_id=1, headers=None):
    body = {"jsonrpc": "2.0", "id": msg_id, "method": method, **({"params": params} if params is not None else {})}
    h = {"Authorization": f"Bearer {token}", "MCP-Protocol-Version": "2025-06-18", **(headers or {})}
    return client.post("/mcp", json=body, headers=h)


def call(client, token, tool_name, /, **arguments):
    """tools/call → (is_error, the tool's answer: a dict, or the error's words)."""
    r = rpc(client, token, "tools/call", {"name": tool_name, "arguments": arguments})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "result" in body, body
    text = body["result"]["content"][0]["text"]
    return body["result"]["isError"], (text if body["result"]["isError"] else json.loads(text))


def ok(client, token, tool_name, /, **arguments):
    is_error, out = call(client, token, tool_name, **arguments)
    assert not is_error, out
    return out


def _new_flow_plan(client, token, w, **output):
    """A draft with orders → filter → an Output, and its save planned."""
    draft = ok(client, token, "flow_draft_new", name="Open orders", folder_id=w["sales"])["draft_id"]
    src = ok(client, token, "flow_add_source", draft_id=draft, datasource_id=w["orders"])["node"]
    step = ok(client, token, "flow_add_step", draft_id=draft, after=src, operation="filter",
              params={"column": "status", "operator": "eq", "value": "open"})["node"]
    out = output or {"kind": "datasource", "datasource_name": "open orders", "overwrite": True}
    ok(client, token, "flow_add_output", draft_id=draft, after=step, **out)
    plan = ok(client, token, "plan_save_flow", draft_id=draft)
    return draft, plan


# ── the protocol ──────────────────────────────────────────────────────────────

def test_initialize_and_the_tools_each_level_sees(app, world):
    c = TestClient(app)
    r = rpc(c, world["read"], "initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                              "clientInfo": {"name": "test", "version": "1"}})
    res = r.json()["result"]
    assert res["protocolVersion"] == "2025-06-18" and res["serverInfo"]["name"] == "tabularia"
    assert res["capabilities"] == {"tools": {"listChanged": False}} and "reads only" in res["instructions"]
    # a version it does not speak: it offers its newest
    r = rpc(c, world["read"], "initialize", {"protocolVersion": "1999-01-01"})
    assert r.json()["result"]["protocolVersion"] == mcp_routes.PROTOCOL_VERSIONS[0]
    # a notification: accepted, nothing to answer
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
               headers={"Authorization": f"Bearer {world['read']}"})
    assert r.status_code == 202 and r.content == b""
    assert rpc(c, world["read"], "ping").json()["result"] == {}

    reading = {t["name"]: t for t in rpc(c, world["read"], "tools/list").json()["result"]["tools"]}
    assert {"search", "list_datasources", "query_datasource", "list_connections", "get_flow", "get_plan"} <= set(reading)
    assert not {"flow_draft_new", "plan_save_flow", "confirm_plan", "cancel_plan"} & set(reading)
    assert all(t["annotations"]["readOnlyHint"] for t in reading.values())
    writing = {t["name"]: t for t in rpc(c, world["write"], "tools/list").json()["result"]["tools"]}
    assert set(reading) < set(writing)
    assert writing["confirm_plan"]["annotations"]["destructiveHint"] is True
    assert writing["confirm_plan"]["annotations"]["readOnlyHint"] is False
    assert writing["plan_save_flow"]["annotations"]["destructiveHint"] is False
    schema = writing["flow_add_output"]["inputSchema"]
    assert schema["type"] == "object" and {"draft_id", "after", "kind"} <= set(schema["required"])
    assert "ask_user" not in writing  # the chat's only


def test_a_read_token_calls_no_tool_that_changes_things(app, world, session):
    c = TestClient(app)
    r = rpc(c, world["read"], "tools/call", {"name": "flow_draft_new", "arguments": {"name": "x", "folder_id": world["sales"]}})
    assert r.json()["error"]["code"] == -32602 and "write token" in r.json()["error"]["message"]
    assert session.exec(select(AgentDraft)).first() is None
    # reading works
    found = ok(c, world["read"], "search", query="ord")
    assert [(h["kind"], h["name"]) for h in found["results"]] == [("datasource", "orders")]
    assert found["results"][0]["link"].startswith("/datasources?")


def test_unknown_methods_tools_and_versions(app, world):
    c = TestClient(app)
    assert rpc(c, world["write"], "resources/list").json()["error"]["code"] == -32601
    assert rpc(c, world["write"], "tools/call", {"name": "nope", "arguments": {}}).json()["error"]["code"] == -32602
    assert rpc(c, world["write"], "ping", headers={"MCP-Protocol-Version": "1999-01-01"}).status_code == 400
    r = c.post("/mcp", content=b"{not json", headers={"Authorization": f"Bearer {world['write']}"})
    assert r.status_code == 400 and r.json()["error"]["code"] == -32700
    # wrong arguments: the tool says why, as a result the model can read
    is_error, words = call(c, world["write"], "flow_draft_new", name="x")
    assert is_error and "folder_id" in words


def test_only_an_api_token_opens_it(app, world):
    c = TestClient(app)
    assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401
    # the browser's session cookie does not
    assert c.post("/auth/login", json={"email": "anna@x.local", "password": PASSWORD}, headers=CSRF).status_code == 200
    assert c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers=CSRF).status_code == 401
    assert rpc(TestClient(app), "tab_invented", "ping").status_code == 401
    # a page of another site
    r = rpc(TestClient(app), world["read"], "ping", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_the_administrators_switch_turns_it_off(app, world):
    c = TestClient(app)
    assert c.get("/mcp", headers={"Authorization": f"Bearer {world['read']}"}).status_code == 405
    get_settings().mcp.enabled = False
    assert rpc(c, world["write"], "ping").status_code == 404
    assert c.get("/mcp").status_code == 404


# ── reading ───────────────────────────────────────────────────────────────────

def test_the_connections_are_listed_without_any_secret(app, world):
    c = TestClient(app)
    r = rpc(c, world["read"], "tools/call", {"name": "list_connections", "arguments": {}})
    text = r.json()["result"]["content"][0]["text"]
    listed = json.loads(text)["connections"]
    assert {x["name"] for x in listed} == {"Warehouse", "Mail"}  # not the one of a folder without CONNECT
    mail = next(x for x in listed if x["name"] == "Mail")
    assert mail["allowed_domains"] == ["acme.com"] and mail["type"] == "smtp" and mail["folder"] == "Sales"
    for secret in ("gAAAA", "loader", "mailer", "db.internal.example", "smtp.internal.example", "password", "dwh"):
        assert secret not in text
    assert set(listed[0]) <= {"id", "name", "type", "folder", "description", "allowed_domains", "has_default_bucket"}


def test_reading_tools_keep_to_the_users_permissions(app, world, session):
    c = TestClient(app)
    other = make_datasource(session, name="payroll", project_id=world["secret"], key="datasets/2/p.parquet")
    other_id = other.id
    is_error, words = call(c, world["read"], "describe_datasource", datasource_id=other_id)
    assert is_error and "No datasource" in words
    assert [f["path"] for f in ok(c, world["read"], "list_folders")["folders"]] == ["Sales"]


def test_a_query_over_mcp_is_audited_as_such(app, world, session, fake_engine):
    fake_engine.preview_response = (200, {"columns": [{"name": "n", "dtype": "Int64"}], "rows": [{"n": 3}],
                                          "row_count": 1, "truncated": False})
    c = TestClient(app)
    out = ok(c, world["read"], "query_datasource", datasource_id=world["orders"], sql="SELECT COUNT(*) AS n FROM self")
    assert out["rows"] == [{"n": 3}]
    assert "slot" not in fake_engine.previews[-1]  # parallel calls of a client never cancel each other
    detail = json.loads(session.exec(select(AuditLog).where(AuditLog.action == "ai.query")).one().detail)
    assert detail["via"] == "mcp" and detail["token"] == "reader" and detail["model"] == "mcp"


# ── a new flow, planned and confirmed ─────────────────────────────────────────

def test_a_new_flow_planned_scheduled_and_confirmed_over_mcp(app, world, session, fake_engine):
    c = TestClient(app)
    draft, plan = _new_flow_plan(c, world["write"], world)
    assert plan["status"] == "pending" and plan["review_link"].endswith(f"/agent-plan?id={plan['plan_id']}")
    assert plan["review_link"].startswith("http")  # an MCP client needs the whole address
    assert "Create the flow «Open orders» in Sales" in plan["actions"][0]["words"]
    assert "open orders" in plan["actions"][0]["words"]
    plan = ok(c, world["write"], "plan_schedule", kind="flow", draft_id=draft, cron="0 7 * * *")
    assert [a["kind"] for a in plan["actions"]] == ["save_flow", "schedule_flow"] and "0 7 * * *" in plan["summary"]
    # planning the same draft again replaces its save, in its place
    plan = ok(c, world["write"], "plan_save_flow", draft_id=draft)
    assert [a["kind"] for a in plan["actions"]] == ["save_flow", "schedule_flow"]
    assert session.exec(select(Flow)).first() is None  # nothing changed yet

    done = ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert done["status"] == "applied" and [r["ok"] for r in done["result"]] == [True, True]
    flow = session.exec(select(Flow)).one()
    assert flow.name == "Open orders" and flow.project_id == world["sales"] and flow.run_schedule == "0 7 * * *"
    assert flow.run_scheduled_by == world["anna"]
    nodes = {n["type"]: n for n in json.loads(flow.definition)["nodes"]}
    assert nodes["output"]["data"]["name"] == "open orders" and nodes["operation"]["data"]["opType"] == "filter"
    assert done["result"][0]["link"].endswith(f"/editor?flow={flow.id}")

    # the audit: each action by its own route, saying it came over MCP, with which token and plan
    created = session.exec(select(AuditLog).where(AuditLog.action == "flow.create")).one()
    detail = json.loads(created.detail)
    assert detail["via"] == "mcp" and detail["token"] == "claude desktop" and detail["plan"] == plan["plan_id"]
    assert json.loads(session.exec(select(AuditLog).where(AuditLog.action == "flow.schedule")).one().detail)["via"] == "mcp"
    applied = session.exec(select(AuditLog).where(AuditLog.action == "agent.plan_apply")).one()
    assert applied.outcome == "success" and json.loads(applied.detail)["actions"] == ["save_flow", "schedule_flow"]

    # never twice
    is_error, words = call(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert is_error and "applied" in words
    assert len(session.exec(select(Flow)).all()) == 1


def test_a_run_planned_after_the_save_runs_the_flow_it_made(app, world, session):
    c = TestClient(app)
    draft, _ = _new_flow_plan(c, world["write"], world)
    plan = ok(c, world["write"], "plan_run_flow", draft_id=draft)
    done = ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert [r["ok"] for r in done["result"]] == [True, True]
    flow = session.exec(select(Flow)).one()
    assert done["result"][1]["object"]["flow_id"] == flow.id
    run = json.loads(session.exec(select(AuditLog).where(AuditLog.action == "flow.run")).one().detail)
    assert run["via"] == "mcp" and run["run_id"] == done["result"][1]["object"]["id"]
    # a run of a draft whose save is not planned is refused
    other = ok(c, world["write"], "flow_draft_new", name="Other", folder_id=world["sales"])["draft_id"]
    is_error, words = call(c, world["write"], "plan_run_flow", draft_id=other)
    assert is_error and "plan_save_flow" in words


def test_an_email_output_outside_the_allowed_domains_is_refused(app, world, session):
    c = TestClient(app)
    draft = ok(c, world["write"], "flow_draft_new", name="Mail it", folder_id=world["sales"])["draft_id"]
    src = ok(c, world["write"], "flow_add_source", draft_id=draft, datasource_id=world["orders"])["node"]
    is_error, words = call(c, world["write"], "flow_add_output", draft_id=draft, after=src, kind="email",
                           connection_id=world["mail"], to=["boss@gmail.com"], subject="Open orders")
    assert is_error and "outside the domains" in words and "boss@gmail.com" in words
    assert "output" not in session.get(AgentDraft, draft).definition  # nothing added
    # a connection of a folder without CONNECT: as if it did not exist
    is_error, words = call(c, world["write"], "flow_add_output", draft_id=draft, after=src, kind="email",
                           connection_id=world["hidden"], to=["a@acme.com"], subject="x")
    assert is_error and "No connection" in words
    out = ok(c, world["write"], "flow_add_output", draft_id=draft, after=src, kind="email",
             connection_id=world["mail"], to=["boss@acme.com"], subject="Open orders")
    assert "boss@acme.com" in out["writes"]


def test_the_permissions_are_checked_again_when_the_plan_is_applied(app, world, session):
    c = TestClient(app)
    _, plan = _new_flow_plan(c, world["write"], world)
    for p in session.exec(select(Permission).where(Permission.user_id == world["anna"],
                                                   Permission.capability == "edit")).all():
        session.delete(p)
    session.commit()
    done = ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert done["status"] == "failed" and done["result"][0]["ok"] is False
    assert "folder" in done["result"][0]["error"]
    assert session.exec(select(Flow)).first() is None


def test_a_plan_is_applied_only_by_its_owner_and_before_it_expires(app, world, session):
    c = TestClient(app)
    _, plan = _new_flow_plan(c, world["write"], world)
    is_error, words = call(c, world["bruno_write"], "confirm_plan", plan_id=plan["plan_id"])
    assert is_error and "no plan" in words
    is_error, _ = call(c, world["bruno_write"], "get_plan", plan_id=plan["plan_id"])
    assert is_error
    row = session.get(AgentPlan, plan["plan_id"])
    row.expires_at = now() - timedelta(seconds=1)
    session.add(row)
    session.commit()
    is_error, words = call(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert is_error and "expired" in words
    assert session.get(AgentPlan, plan["plan_id"]).status == "expired"
    assert session.exec(select(Flow)).first() is None


def test_a_cancelled_plan_is_never_applied(app, world, session):
    c = TestClient(app)
    _, plan = _new_flow_plan(c, world["write"], world)
    assert ok(c, world["write"], "cancel_plan", plan_id=plan["plan_id"])["status"] == "cancelled"
    is_error, words = call(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert is_error and "cancelled" in words
    # the next plan tool starts a new plan
    draft = session.exec(select(AgentDraft)).first().id
    assert ok(c, world["write"], "plan_save_flow", draft_id=draft)["plan_id"] != plan["plan_id"]


# ── a saved flow, changed through a draft ─────────────────────────────────────

def _saved_flow(session, w):
    definition = {"nodes": [
        {"id": "src-1", "type": "source", "position": {"x": 0, "y": 0},
         "data": {"datasourceId": w["orders"], "parquetKey": "datasets/1/o.parquet", "bucket": "data-prep",
                  "filename": "orders", "columns": COLUMNS}},
        {"id": "op-1", "type": "operation", "position": {"x": 200, "y": 0},
         "data": {"opType": "filter", "params": {"column": "status", "operator": "eq", "value": "open"}}},
        {"id": "out-1", "type": "output", "position": {"x": 400, "y": 0},
         "data": {"destType": "datasource", "mode": "append", "name": "open orders", "projectId": w["sales"], "overwrite": True}},
    ], "edges": [
        {"id": "e1", "source": "src-1", "target": "op-1", "targetHandle": "left"},
        {"id": "e2", "source": "op-1", "target": "out-1", "targetHandle": "left"},
    ]}
    return make_flow(session, name="Orders", project_id=w["sales"], definition=json.dumps(definition), owner_id=w["anna"]).id


def test_a_saved_flow_is_changed_with_its_difference_in_the_plan(app, world, session):
    c = TestClient(app)
    flow_id = _saved_flow(session, world)
    opened = ok(c, world["write"], "flow_draft_open", flow_id=flow_id)
    assert [n["id"] for n in opened["nodes"]] == ["src-1", "op-1", "out-1"]
    draft = opened["draft_id"]
    ok(c, world["write"], "flow_change_step", draft_id=draft, node="op-1",
       params={"column": "status", "operator": "eq", "value": "late"})
    shown = ok(c, world["write"], "flow_draft_show", draft_id=draft)
    assert any(line.startswith("~ op-1") and "late" in line for line in shown["changes"])
    plan = ok(c, world["write"], "plan_save_flow", draft_id=draft)
    assert "Change the flow «Orders»" in plan["actions"][0]["words"]
    assert any("late" in line for line in plan["actions"][0]["changes"])
    assert "open" in json.loads(session.get(Flow, flow_id).definition)["nodes"][1]["data"]["params"]["value"]

    done = ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert done["status"] == "applied"
    session.expire_all()
    assert json.loads(session.get(Flow, flow_id).definition)["nodes"][1]["data"]["params"]["value"] == "late"
    assert session.exec(select(FlowVersion).where(FlowVersion.flow_id == flow_id)).first() is not None
    assert json.loads(session.exec(select(AuditLog).where(AuditLog.action == "flow.update")).one().detail)["via"] == "mcp"


def test_a_flow_changed_by_someone_after_the_draft_was_opened_is_not_overwritten(app, world, session):
    c = TestClient(app)
    flow_id = _saved_flow(session, world)
    draft = ok(c, world["write"], "flow_draft_open", flow_id=flow_id)["draft_id"]
    ok(c, world["write"], "flow_remove_node", draft_id=draft, node="op-1")
    flow = session.get(Flow, flow_id)
    flow.definition = flow.definition.replace('"open"', '"closed"')
    session.add(flow)
    session.commit()
    is_error, words = call(c, world["write"], "plan_save_flow", draft_id=draft)
    assert is_error and "changed in Tabularia" in words


# ── confirming in Tabularia ───────────────────────────────────────────────────

def test_the_plans_page_shows_and_confirms_a_plan_for_its_owner(app, world, session):
    c = TestClient(app)
    _, plan = _new_flow_plan(c, world["write"], world)
    browser = TestClient(app)
    assert browser.post("/auth/login", json={"email": "anna@x.local", "password": PASSWORD}, headers=CSRF).status_code == 200
    page = browser.get(f"/agent/plans/{plan['plan_id']}").json()
    assert page["can_confirm"] and page["origin"] == "mcp" and page["token_name"] == "claude desktop"
    action = page["actions"][0]
    assert action["kind"] == "save_flow" and "definition" not in action and action["node_lines"]
    assert action["outputs"][0]["words"].startswith("writes the datasource «open orders»")
    other = TestClient(app)
    other.post("/auth/login", json={"email": "bruno@x.local", "password": PASSWORD}, headers=CSRF)
    assert other.get(f"/agent/plans/{plan['plan_id']}").status_code == 404
    assert other.post(f"/agent/plans/{plan['plan_id']}/confirm", headers=CSRF).status_code == 404

    done = browser.post(f"/agent/plans/{plan['plan_id']}/confirm", headers=CSRF).json()
    assert done["status"] == "applied" and not done["can_confirm"] and done["result"][0]["link"].startswith("/editor?flow=")
    # confirmed by the person, made over MCP: the audit says so
    detail = json.loads(session.exec(select(AuditLog).where(AuditLog.action == "flow.create")).one().detail)
    assert detail["via"] == "mcp" and "token" not in detail
    assert browser.post(f"/agent/plans/{plan['plan_id']}/confirm", headers=CSRF).status_code == 409


def test_with_confirm_in_tabularia_only_a_person_confirms_from_the_page(app, world, session):
    get_settings().mcp.confirm_in_tabularia_only = True
    c = TestClient(app)
    _, plan = _new_flow_plan(c, world["write"], world)
    assert "in Tabularia" in plan["next"]
    is_error, words = call(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert is_error and f"/agent-plan?id={plan['plan_id']}" in words
    r = c.post(f"/agent/plans/{plan['plan_id']}/confirm", headers={"Authorization": f"Bearer {world['write']}"})
    assert r.status_code == 403
    assert session.exec(select(Flow)).first() is None
    browser = TestClient(app)
    browser.post("/auth/login", json={"email": "anna@x.local", "password": PASSWORD}, headers=CSRF)
    assert browser.post(f"/agent/plans/{plan['plan_id']}/confirm", headers=CSRF).json()["status"] == "applied"


def test_the_page_cancels_a_plan(app, world):
    c = TestClient(app)
    _, plan = _new_flow_plan(c, world["write"], world)
    browser = TestClient(app)
    browser.post("/auth/login", json={"email": "anna@x.local", "password": PASSWORD}, headers=CSRF)
    assert browser.post(f"/agent/plans/{plan['plan_id']}/cancel", headers=CSRF).json()["status"] == "cancelled"
    assert browser.post(f"/agent/plans/{plan['plan_id']}/cancel", headers=CSRF).status_code == 409


# ── the registry and the housekeeping ─────────────────────────────────────────

def test_every_tool_is_described_for_the_model():
    for t in REGISTRY.values():
        assert t.description and t.title and t.level in ("read", "write"), t.name
        assert t.input_schema()["type"] == "object", t.name
    assert {t.name for t in available("mcp", "read")} == {t.name for t in REGISTRY.values()
                                                         if t.level == "read" and t.only in (None, "mcp")}
    assert "confirm_plan" not in {t.name for t in available("chat")}  # only the person's click applies a chat's plan


def test_the_drafts_and_plans_go_with_the_account(session):
    from app.routes.users import remove_user

    gone = make_user(session, email="gone@x.local")
    session.add_all([AgentDraft(user_id=gone.id), AgentPlan(user_id=gone.id), AgentDraft(user_id=gone.id + 1)])
    session.commit()
    remove_user(session, gone.id)
    session.commit()
    assert [d.user_id for d in session.exec(select(AgentDraft)).all()] == [gone.id + 1]
    assert session.exec(select(AgentPlan)).first() is None


def test_old_drafts_go_and_pending_plans_expire(session):
    old = AgentDraft(user_id=1, updated_at=now() - timedelta(hours=25))
    fresh = AgentDraft(user_id=1)
    late = AgentPlan(user_id=1, expires_at=now() - timedelta(minutes=1))
    waiting = AgentPlan(user_id=1, expires_at=now() + timedelta(minutes=10))
    session.add_all([old, fresh, late, waiting])
    session.commit()
    done = housekeeping.one_pass(session)
    assert done["agent_drafts"] == 1 and done["agent_plans"] == 1
    assert [d.id for d in session.exec(select(AgentDraft)).all()] == [fresh.id]
    session.expire_all()
    assert session.get(AgentPlan, late.id).status == "expired" and session.get(AgentPlan, waiting.id).status == "pending"


# ── what the person confirms is what they saw (bug hunt 2026-10-28) ───────────

def _browser(app, email="anna@x.local"):
    browser = TestClient(app)
    assert browser.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF).status_code == 200
    return browser


def test_a_plan_changed_after_the_person_saw_it_is_not_applied(app, world, session):
    """The agent keeps adding to a pending plan while its page (or the chat's card) is
    open: «Confirm» applies the plan as the person saw it, or nothing."""
    c = TestClient(app)
    draft, plan = _new_flow_plan(c, world["write"], world)
    browser = _browser(app)
    seen = browser.get(f"/agent/plans/{plan['plan_id']}").json()
    assert seen["revision"]
    # meanwhile the agent adds a run the person has not seen
    ok(c, world["write"], "plan_run_flow", draft_id=draft)
    r = browser.post(f"/agent/plans/{plan['plan_id']}/confirm", json={"revision": seen["revision"]}, headers=CSRF)
    assert r.status_code == 409 and "changed" in r.json()["detail"]
    assert session.exec(select(Flow)).first() is None
    session.expire_all()
    assert session.get(AgentPlan, plan["plan_id"]).status == "pending"
    # looked at again, it is confirmed as it is now
    again = browser.get(f"/agent/plans/{plan['plan_id']}").json()
    assert again["revision"] != seen["revision"] and len(again["actions"]) == 2
    done = browser.post(f"/agent/plans/{plan['plan_id']}/confirm", json={"revision": again["revision"]}, headers=CSRF)
    assert done.json()["status"] == "applied"


def test_an_action_is_never_written_into_a_plan_already_being_applied(app, world, session, monkeypatch):
    """A plan tool reads the pending plan, the person's click claims it, the tool writes:
    the action must not land on the plan being applied (it would never run, while the
    agent says it is in the plan) — it goes to a new plan."""
    from app.services.agent import plans as plans_service

    c = TestClient(app)
    draft, plan = _new_flow_plan(c, world["write"], world)
    real = plans_service._current_plan

    def claimed_meanwhile(s, ctx):
        found = real(s, ctx)
        if found is not None and found.id == plan["plan_id"]:
            s.exec(plans_service.update(AgentPlan).where(AgentPlan.id == found.id).values(status="applying"))
            s.commit()
            monkeypatch.setattr(plans_service, "_current_plan", real)
        return found

    monkeypatch.setattr(plans_service, "_current_plan", claimed_meanwhile)
    out = ok(c, world["write"], "plan_run_flow", flow_id=_saved_flow(session, world))
    assert out["plan_id"] != plan["plan_id"] and [a["kind"] for a in out["actions"]] == ["run_flow"]
    session.expire_all()
    assert [a["kind"] for a in json.loads(session.get(AgentPlan, plan["plan_id"]).actions)] == ["save_flow"]


def test_what_an_output_runs_and_sends_is_on_the_plan(app, world, session):
    """A database Output's post-SQL runs on the database at every run, an email's cc and
    body go out with it: the plan shows them, not only the table and the recipients."""
    c = TestClient(app)
    _, plan = _new_flow_plan(c, world["write"], world, kind="database", connection_id=world["db"],
                             table="public.open_orders", post_sql="DELETE FROM public.customers")
    assert "DELETE FROM public.customers" in plan["actions"][0]["words"]
    page = _browser(app).get(f"/agent/plans/{plan['plan_id']}").json()
    assert page["actions"][0]["outputs"][0]["post_sql"] == "DELETE FROM public.customers"
    mail = Connection(name="Open mail", project_id=world["sales"], db_type="smtp", host="smtp.example", password_encrypted="x")
    session.add(mail)
    session.commit()
    _, plan = _new_flow_plan(c, world["write"], world, kind="email", connection_id=mail.id, to=["boss@acme.com"],
                             cc=["someone@elsewhere.example"], subject="Open orders", body="See the file.")
    page = _browser(app).get(f"/agent/plans/{plan['plan_id']}").json()
    out = [o for a in page["actions"] for o in a.get("outputs") or [] if o["kind"] == "email"][0]
    assert out["cc"] == "someone@elsewhere.example" and out["body"] == "See the file."


def test_an_api_token_does_not_confirm_a_plan_made_in_the_chat(app, world, session):
    """Only the person's click applies a chat's plan: not a script with a token on the
    page's route."""
    plan = AgentPlan(user_id=world["anna"], origin="chat", chat_id=1, expires_at=now() + timedelta(minutes=10))
    session.add(plan)
    session.commit()
    c = TestClient(app)
    r = c.post(f"/agent/plans/{plan.id}/confirm", headers={"Authorization": f"Bearer {world['write']}"})
    assert r.status_code == 403
    session.expire_all()
    assert session.get(AgentPlan, plan.id).status == "pending"


def test_a_batch_has_a_ceiling(app, world):
    c = TestClient(app)
    body = [{"jsonrpc": "2.0", "id": i, "method": "ping"} for i in range(mcp_routes.MAX_BATCH + 1)]
    r = c.post("/mcp", json=body, headers={"Authorization": f"Bearer {world['read']}"})
    assert r.status_code == 400 and "batch" in r.json()["error"]["message"].lower()
    r = c.post("/mcp", json=body[:mcp_routes.MAX_BATCH], headers={"Authorization": f"Bearer {world['read']}"})
    assert r.status_code == 200 and len(r.json()) == mcp_routes.MAX_BATCH


def test_a_changed_output_shows_what_it_wrote_and_what_it_will_write(app, world, session):
    """The difference of a saved flow names an Output's target before and after: an email
    sent elsewhere, a post-SQL added, are a visible change, not «output to email» twice."""
    c = TestClient(app)
    flow_id = _saved_flow(session, world)
    draft = ok(c, world["write"], "flow_draft_open", flow_id=flow_id)["draft_id"]
    row = session.get(AgentDraft, draft)
    definition = json.loads(row.definition)
    definition["nodes"][2]["data"] = {"destType": "database", "connectionId": world["db"], "table": "public.x",
                                      "mode": "append", "postSql": "DELETE FROM public.customers"}
    row.definition = json.dumps(definition)
    session.add(row)
    session.commit()
    plan = ok(c, world["write"], "plan_save_flow", draft_id=draft)
    line = next(x for x in plan["actions"][0]["changes"] if x.startswith("~ out-1"))
    assert "output to datasource «open orders»" in line and "DELETE FROM public.customers" in line
    assert "DELETE FROM public.customers" in plan["actions"][0]["words"]
