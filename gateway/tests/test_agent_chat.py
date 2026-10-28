"""The Assistant page's agent (docs/specs/mcp-agent.md, piece 2), with a simulated model.

Pinned here, through the real routes: the chat is offered the registry's tools by level
(an observer only the reading ones) and none to confirm a plan; `ask_user` ends the turn
and its questions come with `done`, as a card, and the answers given in the next message
are found again when the conversation is reopened; a plan made in the chat comes with
`done`, belongs to the conversation (created before its first draft), changes nothing
until the person confirms it from its card, and is audited `via: "chat"` — as are the
chat's queries; a refused call and its retry are two steps, the first with its reason;
reopening a conversation shows the steps with their outcome and time, the questions with
their answers and the plan as it stands now, which the next turn's context states too.
"""
import json
from contextlib import nullcontext

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from sqlmodel import select

import app.core.config as config_mod
from app.core.security import hash_password
from app.db.session import get_session
from app.models import AgentDraft, AgentPlan, AiChat, AiModel, AuditLog, Flow
from app.routes import agent_plans as plans_routes
from app.routes import ai as ai_routes
from app.routes import auth as auth_routes
from app.services import ai_agent, ai_models, audit
from app.services.agent import available, common
from tests.conftest import make_datasource, make_permission, make_project, make_user

PASSWORD = "una-password-lunga"
CSRF = {"X-Tabularia-Csrf": "1"}
MODEL = "gpt-oss-120b"
COLUMNS = [{"name": "status", "dtype": "String"}, {"name": "amount", "dtype": "Float64"}]


class Script:
    """The model's moves, one per request to it: a text, or tool calls — given as
    (name, arguments), or as a function of the tools' latest results by name. It records
    the tools and the instructions each request offered."""

    def __init__(self, *moves):
        self.moves = list(moves)
        self.got: dict = {}
        self.offered: list[tuple[set, set]] = []
        self.instructions: list[str] = []

    async def __call__(self, messages, info):
        self.offered.append(({t.name for t in info.function_tools}, {t.name for t in info.output_tools}))
        self.instructions.append(info.instructions or "")
        last = messages[-1]
        if isinstance(last, ModelRequest):
            self.got.update({p.tool_name: p.content for p in last.parts if isinstance(p, ToolReturnPart)})
        move = self.moves.pop(0)
        if callable(move):
            move = move(self.got)
        if isinstance(move, str):
            yield move
            return
        text, calls = (move[0], move[1:]) if isinstance(move[0], str) else (None, move)
        if text:
            yield text
        n = len(self.offered)
        yield {i: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"c{n}-{i}")
               for i, (name, args) in enumerate(calls)}


@pytest.fixture
def ai_on(monkeypatch):
    settings = config_mod.Settings(ai={"base_url": "https://ai.example/v1", "secret_key": "k"})
    for mod in (ai_routes, ai_agent, ai_models):
        monkeypatch.setattr(mod, "get_settings", lambda: settings)

    async def offered(force: bool = False):
        return [MODEL]

    monkeypatch.setattr(ai_routes, "provider_models", offered)
    return settings


@pytest.fixture
def app(session, fake_engine, ai_on, monkeypatch):
    app = FastAPI()
    for r in (auth_routes.router, ai_routes.router, plans_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    monkeypatch.setattr(ai_routes, "_new_session", lambda: nullcontext(session))
    monkeypatch.setattr(common, "new_session", lambda: nullcontext(session))
    session.add(AiModel(model_id=MODEL, enabled=True))
    session.commit()
    return app


@pytest.fixture
def world(session):
    """Anna edits «Sales» and uses its connections; Oscar is an observer. Ids only."""
    anna = make_user(session, email="anna@x.local", hashed_password=hash_password(PASSWORD))
    oscar = make_user(session, email="oscar@x.local", hashed_password=hash_password(PASSWORD), is_observer=True)
    sales = make_project(session, name="Sales")
    make_permission(session, user_id=anna.id, project_id=sales.id, capability="edit")
    make_permission(session, user_id=anna.id, project_id=sales.id, capability="connect")
    make_permission(session, user_id=oscar.id, project_id=sales.id, capability="view")
    orders = make_datasource(session, name="orders", project_id=sales.id, key="datasets/1/o.parquet", rows=10,
                             columns=json.dumps(COLUMNS), description="Orders")
    return dict(anna=anna.id, oscar=oscar.id, sales=sales.id, orders=orders.id)


def signed_in(app, email) -> TestClient:
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF).status_code == 200
    return c


def turn(client, monkeypatch, script, message, chat_id=None):
    """One turn of the chat → its events [(name, data)]."""
    monkeypatch.setattr(ai_agent, "build_model", lambda model_id: FunctionModel(stream_function=script))
    r = client.post("/ai/chat", json={"message": message, "model": MODEL, "chat_id": chat_id, "locale": "en"},
                    headers=CSRF)
    assert r.status_code == 200, r.text
    events = []
    for block in r.text.strip().split("\n\n"):
        name = block.split("\n")[0].removeprefix("event: ")
        events.append((name, json.loads(block.split("\n", 1)[1].removeprefix("data: "))))
    return events


def done_of(events):
    assert events[-1][0] == "done", events
    return events[-1][1]


# ── the tools, by level ───────────────────────────────────────────────────────

def test_the_chat_is_offered_the_registrys_tools_by_level_and_none_to_confirm(app, world, monkeypatch):
    anna = signed_in(app, "anna@x.local")
    script = Script("Hello.")
    done_of(turn(anna, monkeypatch, script, "hi"))
    tools, outputs = script.offered[0]
    assert tools == {t.name for t in available("chat", "write") if not t.ends_turn}
    assert {"flow_draft_new", "plan_save_flow", "plan_schedule", "cancel_plan", "query_datasource"} <= tools
    assert "confirm_plan" not in tools and "ask_user" not in tools
    assert outputs == {"ask_user"}  # it ends the turn: an output, not a call

    oscar = signed_in(app, "oscar@x.local")
    script = Script("Hello.")
    done_of(turn(oscar, monkeypatch, script, "hi"))
    tools, outputs = script.offered[0]
    assert tools == {t.name for t in available("chat", "read") if not t.ends_turn}
    assert not any(n.startswith(("flow_", "plan_")) for n in tools) and "cancel_plan" not in tools
    assert outputs == {"ask_user"}
    assert "This user can only read" in script.instructions[0]


def test_an_observer_builds_nothing(app, world, session, monkeypatch):
    oscar = signed_in(app, "oscar@x.local")
    script = Script([("flow_draft_new", {"name": "Mine", "folder_id": world["sales"]})], "I cannot build here.")
    events = turn(oscar, monkeypatch, script, "build me a flow")
    results = [d for n, d in events if n == "tool_result"]
    assert results and results[0]["ok"] is False  # no such tool for an observer
    assert done_of(events)["chat_id"] is None  # an observer's conversation is saved nowhere
    assert session.exec(select(AgentDraft)).first() is None and session.exec(select(AgentPlan)).first() is None


# ── questions with choices ────────────────────────────────────────────────────

QUESTIONS = [
    {"question": "Which datasource?", "options": [
        {"label": "orders", "description": "The orders of the ERP, one row per order"},
        {"label": "orders_2024", "description": "Only 2024"}]},
    {"question": "When should it run?", "options": [
        {"label": "Every morning at 7", "description": "Before the office opens"},
        {"label": "Every Monday", "description": "Once a week"},
        {"label": "Only now", "description": "No schedule"}]},
]


def test_a_question_card_ends_the_turn_and_its_answers_are_found_again(app, world, session, monkeypatch):
    anna = signed_in(app, "anna@x.local")
    one_choice = [{"question": "Which one?", "options": [{"label": "orders"}]}]
    script = Script(
        [("ask_user", {"questions": one_choice})],  # refused: two choices at least
        ["Two things first.", ("ask_user", {"questions": QUESTIONS})],
    )
    events = turn(anna, monkeypatch, script, "send me the open orders")
    names = [n for n, _ in events]
    assert "tool_call" not in names  # a card, not a step
    done = done_of(events)
    assert done["usage"]["requests"] == 2  # the refused card, then the card: the turn stopped there
    assert [q["question"] for q in done["questions"]] == ["Which datasource?", "When should it run?"]
    assert [o["recommended"] for o in done["questions"][1]["options"]] == [True, False, False]
    assert "".join(d["delta"] for n, d in events if n == "text") == "Two things first."
    chat_id = done["chat_id"]

    opened = anna.get(f"/ai/chats/{chat_id}").json()["messages"]
    assert opened[0]["questions"] == done["questions"] and opened[0]["answers"] is None and opened[0]["steps"] == []

    answer = "Which datasource? → orders\nWhen should it run? → at 6:30, on weekdays"
    script = Script("Fine: orders, at 6:30 on weekdays.")
    done_of(turn(anna, monkeypatch, script, answer, chat_id))
    opened = anna.get(f"/ai/chats/{chat_id}").json()["messages"]
    assert opened[0]["answers"] == ["orders", "at 6:30, on weekdays"]
    assert opened[1]["question"] == answer and "questions" not in opened[1]


# ── a plan, made in the chat and confirmed by the person ─────────────────────

def _building(w, *then):
    """The model's moves to build orders → filter → Output and plan its save."""
    return Script(
        [("flow_draft_new", {"name": "Open orders", "folder_id": w["sales"]})],
        lambda got: [("flow_add_source", {"draft_id": got["flow_draft_new"]["draft_id"], "datasource_id": w["orders"]})],
        lambda got: [("flow_add_step", {"draft_id": got["flow_draft_new"]["draft_id"], "after": got["flow_add_source"]["node"],
                                        "operation": "filter",
                                        "params": {"column": "status", "operator": "eq", "value": "open"}})],
        lambda got: [("flow_add_output", {"draft_id": got["flow_draft_new"]["draft_id"], "after": got["flow_add_step"]["node"],
                                          "kind": "datasource", "datasource_name": "open orders", "overwrite": True})],
        lambda got: [("plan_save_flow", {"draft_id": got["flow_draft_new"]["draft_id"]})],
        *then,
    )


def test_a_plan_made_in_the_chat_waits_for_the_persons_click(app, world, session, monkeypatch):
    anna = signed_in(app, "anna@x.local")
    script = _building(
        world,
        lambda got: [("confirm_plan", {"plan_id": got["plan_save_flow"]["plan_id"]})],  # not the chat's
        "Here is the plan: confirm it below.",
    )
    events = turn(anna, monkeypatch, script, "make me a flow of the open orders")
    done = done_of(events)
    chat_id, plan = done["chat_id"], done["plan"]
    assert chat_id and plan["status"] == "pending" and plan["can_confirm"] and plan["chat_id"] == chat_id
    assert plan["origin"] == "chat" and [a["kind"] for a in plan["actions"]] == ["save_flow"]
    action = plan["actions"][0]
    assert action["node_lines"] and action["outputs"][0]["target"] == "open orders" and "definition" not in action
    # the conversation was created before its first draft: the draft and the plan are its own
    assert session.get(AgentDraft, plan["actions"][0]["draft_id"]).chat_id == chat_id
    assert session.get(AiChat, chat_id).title.startswith("make me a flow")
    calls = {d["id"]: d for n, d in events if n == "tool_call"}
    results = [d for n, d in events if n == "tool_result"]
    assert calls[results[0]["id"]]["title"] == "Start a new flow"
    assert all(isinstance(r["duration_ms"], int) for r in results)
    assert results[-1]["name"] == "confirm_plan" and results[-1]["ok"] is False
    # nothing has changed: the model cannot confirm
    assert session.exec(select(Flow)).first() is None
    session.expire_all()
    assert session.get(AgentPlan, plan["id"]).status == "pending"

    applied = anna.post(f"/agent/plans/{plan['id']}/confirm", headers=CSRF).json()
    assert applied["status"] == "applied" and applied["result"][0]["link"].startswith("/editor?flow=")
    flow = session.exec(select(Flow)).one()
    detail = json.loads(session.exec(select(AuditLog).where(AuditLog.action == "flow.create")).one().detail)
    assert detail["via"] == "chat" and detail["plan"] == plan["id"]

    # opened again: the steps with their outcome and time, the plan as it is NOW
    opened = anna.get(f"/ai/chats/{chat_id}").json()["messages"][0]
    assert opened["plan_id"] == plan["id"] and opened["plan"]["status"] == "applied"
    steps = opened["steps"]
    assert [s["name"] for s in steps][:5] == ["flow_draft_new", "flow_add_source", "flow_add_step", "flow_add_output",
                                              "plan_save_flow"]
    assert all(s["ok"] and isinstance(s["duration_ms"], int) for s in steps[:5])
    assert steps[0]["title"] == "Start a new flow" and steps[0]["subject"] == "Open orders"
    assert steps[5]["name"] == "confirm_plan" and steps[5]["ok"] is False and "confirm_plan" in steps[5]["error"]

    # the next turn knows: the plan's state is in its context
    script = Script("It is created.")
    done_of(turn(anna, monkeypatch, script, "is it done?", chat_id))
    assert f"plan {plan['id']}: applied" in script.instructions[0] and f"Flow «{flow.name}» created." in script.instructions[0]


def test_the_next_turn_changes_the_same_plan(app, world, session, monkeypatch):
    anna = signed_in(app, "anna@x.local")
    done = done_of(turn(anna, monkeypatch, _building(world, "The plan is below."), "make me a flow"))
    chat_id, plan_id = done["chat_id"], done["plan"]["id"]
    draft_id = done["plan"]["actions"][0]["draft_id"]
    script = Script([("plan_schedule", {"kind": "flow", "draft_id": draft_id, "cron": "0 7 * * *"})], "Scheduled too.")
    done = done_of(turn(anna, monkeypatch, script, "and every morning at 7", chat_id))
    assert done["plan"]["id"] == plan_id and [a["kind"] for a in done["plan"]["actions"]] == ["save_flow", "schedule_flow"]
    assert f"plan {plan_id}: pending, 1 action" in script.instructions[0]  # what it was when the turn began
    cancelled = anna.post(f"/agent/plans/{plan_id}/cancel", headers=CSRF).json()
    assert cancelled["status"] == "cancelled"
    opened = anna.get(f"/ai/chats/{chat_id}").json()["messages"]
    assert [m["plan"]["status"] for m in opened] == ["cancelled", "cancelled"]


# ── the steps and the audit of the chat's queries ────────────────────────────

def test_a_refused_call_and_its_retry_are_two_steps_and_the_query_is_audited_as_the_chats(
        app, world, session, monkeypatch, fake_engine):
    fake_engine.preview_response = (200, {"columns": [{"name": "n", "dtype": "Int64"}], "rows": [{"n": 3}],
                                          "row_count": 1, "truncated": False})
    anna = signed_in(app, "anna@x.local")
    script = Script(
        [("query_datasource", {"datasource_id": world["orders"], "sql": "DELETE FROM self"})],
        [("query_datasource", {"datasource_id": world["orders"], "sql": "SELECT COUNT(*) AS n FROM self"})],
        "There are 3.",
    )
    events = turn(anna, monkeypatch, script, "how many orders?")
    results = [d for n, d in events if n == "tool_result"]
    assert [r["ok"] for r in results] == [False, True] and "SELECT" in results[0]["error"]
    assert results[1]["table"]["rows"] == [{"n": 3}]
    chat_id = done_of(events)["chat_id"]
    steps = anna.get(f"/ai/chats/{chat_id}").json()["messages"][0]["steps"]
    assert [(s["name"], s["ok"]) for s in steps] == [("query_datasource", False), ("query_datasource", True)]
    assert "SELECT" in steps[0]["error"] and steps[1]["table"]["rows"] == [{"n": 3}]
    entry = session.exec(select(AuditLog).where(AuditLog.action == audit.AI_QUERY)).one()
    assert json.loads(entry.detail)["via"] == "chat" and entry.actor_id == world["anna"]


def test_a_card_is_checked_and_its_answers_are_read_back_by_question():
    from app.services.agent.questions import AskUserArgs, answers_of, card
    from app.services.agent.registry import ToolError

    asked = card(AskUserArgs.model_validate({"questions": QUESTIONS}))["questions"]
    assert answers_of(asked, "When should it  run? → Only now\nsomething else") == [None, "Only now"]
    assert answers_of(asked, "just do it") == [None, None]
    with pytest.raises(ToolError):  # the answer is told by its label: two alike cannot be told apart
        card(AskUserArgs.model_validate({"questions": [{"question": "Which one?", "options": [{"label": "a"}, {"label": "A"}]}]}))


# ── bug hunt 2026-10-28 ───────────────────────────────────────────────────────

def test_deleting_a_conversation_cancels_its_pending_plan_and_drops_its_drafts(app, world, session, monkeypatch):
    """A plan whose conversation is gone has no card to look at it with any more: it must
    not stay confirmable from its page for the rest of its 30 minutes."""
    anna = signed_in(app, "anna@x.local")
    done = done_of(turn(anna, monkeypatch, _building(world, "The plan is below."), "make me a flow"))
    chat_id, plan_id = done["chat_id"], done["plan"]["id"]
    other = done_of(turn(anna, monkeypatch, _building(world, "Another."), "and another one"))
    assert other["chat_id"] != chat_id
    assert anna.delete(f"/ai/chats/{chat_id}", headers=CSRF).status_code == 204
    session.expire_all()
    assert session.get(AgentPlan, plan_id).status == "cancelled"
    assert session.get(AgentPlan, other["plan"]["id"]).status == "pending"  # another conversation's: untouched
    assert {d.chat_id for d in session.exec(select(AgentDraft)).all()} == {other["chat_id"]}
    r = anna.post(f"/agent/plans/{plan_id}/confirm", headers=CSRF)
    assert r.status_code == 409 and session.exec(select(Flow)).first() is None


def test_reading_tools_that_preview_on_the_engine_take_turns(app, world, session, monkeypatch):
    """The conversation's queries share one preview slot on the engine, where a newer one
    knocks down the one running (409): the calls of one answer must not overlap."""
    import asyncio

    active, overlapped = {"n": 0}, []

    async def preview(ds, operations, limit, deps):
        active["n"] += 1
        overlapped.append(active["n"] > 1)
        await asyncio.sleep(0.05)
        active["n"] -= 1
        return {"columns": [{"name": "n", "dtype": "Int64"}], "rows": [{"n": 1}], "row_count": 1, "truncated": False}

    monkeypatch.setattr(ai_agent, "_engine_preview", preview)
    anna = signed_in(app, "anna@x.local")
    chat_id = done_of(turn(anna, monkeypatch, Script("Hello."), "hi"))["chat_id"]
    script = Script([("describe_datasource", {"datasource_id": world["orders"]}),
                     ("describe_datasource", {"datasource_id": world["orders"]}),
                     ("query_datasource", {"datasource_id": world["orders"], "sql": "SELECT COUNT(*) AS n FROM self"})],
                    "Done.")
    events = turn(anna, monkeypatch, script, "describe it twice and count", chat_id)
    assert [d["ok"] for n, d in events if n == "tool_result"] == [True, True, True]
    assert len(overlapped) == 3 and not any(overlapped)


def test_the_datasource_tools_union_reaches_an_openai_compatible_provider(app, world, session, monkeypatch):
    """plan_create_datasource takes `source` as a union told apart by `type` (JSON Schema
    oneOf + pydantic's `discriminator`). Through pydantic-ai's OpenAI model the schema goes
    out with its $defs, as oneOf, without the non-standard `discriminator`, and the call
    the model makes comes back to the tool, which plans the datasource."""
    import httpx
    from pydantic_ai.models.openai import OpenAIChatModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from app.models import Connection

    db = Connection(name="Warehouse", project_id=world["sales"], db_type="postgresql", host="db.example",
                    password_encrypted="x", database="dwh")
    session.add(db)
    session.commit()
    db_id = db.id
    sent: list[dict] = []
    arguments = json.dumps({"folder_id": world["sales"], "connection_id": db_id, "name": "customers",
                            "source": {"type": "table", "table": "public.customers"}})

    def chunk(delta, finish=None):
        return "data: " + json.dumps({"id": "c", "object": "chat.completion.chunk", "created": 0, "model": MODEL,
                                      "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"

    def provider(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            body = chunk({"role": "assistant", "tool_calls": [{"index": 0, "id": "call_1", "type": "function", "function": {
                "name": "plan_create_datasource", "arguments": arguments}}]}) + chunk({}, "tool_calls")
        else:
            body = chunk({"role": "assistant", "content": "Planned."}) + chunk({}, "stop")
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=(body + "data: [DONE]\n\n").encode())

    model = OpenAIChatModel(MODEL, provider=OpenAIProvider(
        base_url="https://ai.example/v1", api_key="k", http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider))))
    monkeypatch.setattr(ai_agent, "build_model", lambda model_id: model)
    anna = signed_in(app, "anna@x.local")
    r = anna.post("/ai/chat", json={"message": "import the customers", "model": MODEL, "locale": "en"}, headers=CSRF)
    assert r.status_code == 200 and "event: done" in r.text, r.text
    tool = next(t["function"] for t in sent[0]["tools"] if t["function"]["name"] == "plan_create_datasource")
    params = tool["parameters"]
    assert "discriminator" not in json.dumps(params)
    refs = {x["$ref"] for x in params["properties"]["source"]["oneOf"]}
    assert {"#/$defs/TableSource", "#/$defs/SqlSource", "#/$defs/ApiSource"} <= refs
    assert all(ref.removeprefix("#/$defs/") in params["$defs"] for ref in refs)
    returned = next(m for m in sent[1]["messages"] if m["role"] == "tool")
    assert '"plan_id"' in returned["content"] and "create_datasource" in returned["content"]
    plan = session.exec(select(AgentPlan)).one()
    action = json.loads(plan.actions)[0]
    assert action["kind"] == "create_datasource" and action["source_type"] == "table" and action["connection_id"] == db_id
