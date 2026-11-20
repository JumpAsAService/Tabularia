"""The flow editor's assistant: it changes the canvas the editor sends, step by step,
checks each step on a preview made with the user's rights, and hands the new
canvas back for one Undo step. It never saves the flow. The model is a scripted
FunctionModel; the engine is the FakeEngine of conftest."""
import json
from contextlib import nullcontext
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic_ai import ModelRetry
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from sqlmodel import select

import app.core.config as config_mod
from app.models import AiChat, AiModel, AuditLog, Flow
from app.models.ai_chat import AiSpend
from app.routes import ai as ai_routes
from app.services import ai_agent, ai_chats, ai_flow_agent, ai_models
from app.services.ai_flow_agent import FlowDraft, clean_params
from tests.conftest import make_datasource, make_flow, make_permission, make_project, make_user

COLUMNS = [{"name": "country", "dtype": "String"}, {"name": "amount", "dtype": "Float64"}]
EMPTY_CANVAS = {"nodes": [{"id": "source", "type": "source", "position": {"x": 60, "y": 140},
                           "data": {"bucket": "data-prep", "datasetId": None, "parquetKey": None, "columns": []}}], "edges": []}


@pytest.fixture
def ai_on(monkeypatch, session):
    settings = config_mod.Settings(ai={"base_url": "https://ai.example/v1", "secret_key": "k"})
    for mod in (config_mod, ai_routes, ai_agent, ai_models):
        monkeypatch.setattr(mod, "get_settings", lambda: settings)

    async def offered(force: bool = False, provider=None):
        return ["gpt-oss-120b"]

    monkeypatch.setattr(ai_routes, "provider_models", offered)
    monkeypatch.setattr(ai_routes, "_new_session", lambda: nullcontext(session))
    return settings


@pytest.fixture
def world(session, ai_on):
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    editor = make_user(session, email="ed@x.local")
    sales = make_project(session, name="Sales")
    secret = make_project(session, name="Secret")
    make_permission(session, user_id=editor.id, project_id=sales.id, capability="edit")
    orders = make_datasource(session, name="orders", project_id=sales.id, key="datasets/1/o.parquet", rows=1204,
                             columns=json.dumps(COLUMNS), description="Orders of 2024")
    payroll = make_datasource(session, name="payroll", project_id=secret.id, key="datasets/2/p.parquet", columns=json.dumps(COLUMNS))
    flow = make_flow(session, name="Orders by country", project_id=sales.id, definition=json.dumps(EMPTY_CANVAS))
    session.add(AiModel(model_id="gpt-oss-120b", enabled=True))
    session.commit()
    return dict(admin=admin, editor=editor, sales=sales, secret=secret, orders=orders, payroll=payroll, flow=flow)


def script(*steps):
    """A model that plays the given steps, one per request: (tool, args) or text."""
    queue = list(steps)

    async def play(messages, info):
        step = queue.pop(0)
        if isinstance(step, str):
            yield step
        else:
            name, args = step
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=f"c{len(queue)}")}

    return play


async def _events(response):
    out = []
    async for chunk in response.body_iterator:
        for block in chunk.decode().strip().split("\n\n"):
            name = block.split("\n")[0].removeprefix("event: ")
            out.append((name, json.loads(block.split("\n", 1)[1].removeprefix("data: "))))
    return out


async def _turn(session, world, monkeypatch, user, steps, message="keep the Italian orders", canvas=EMPTY_CANVAS, **kw):
    monkeypatch.setattr(ai_agent, "build_model", lambda model_id: FunctionModel(stream_function=script(*steps)))
    body = ai_routes.FlowAssistRequest(message=message, model="gpt-oss-120b", flow_id=kw.pop("flow_id", world["flow"].id),
                                       definition=json.dumps(canvas), locale="en", **kw)
    return await _events(await ai_routes.flow_assist(body, request=None, user=user, session=session))


# ── a whole turn ─────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_a_turn_builds_on_the_canvas_checks_each_step_and_hands_the_canvas_back(session, world, fake_engine, monkeypatch):
    fake_engine.preview_response = (200, {"columns": COLUMNS, "rows": [{"country": "IT", "amount": 3.5}], "row_count": 1, "truncated": False})
    events = await _turn(session, world, monkeypatch, world["editor"], [
        ("get_flow", {}),
        ("list_datasources", {"search": "orders"}),
        ("describe_datasource", {"datasource_id": world["orders"].id}),
        ("add_source", {"datasource_id": world["orders"].id}),
        ("add_step", {"after": "source", "operation": "filter", "params": {"column": "country", "operator": "eq", "value": "IT"}}),
        "Added a filter on country (op-1). Undo takes it back; save the flow to keep it.",
    ])
    names = [n for n, _ in events]
    assert names[-1] == "done" and "error" not in names
    done = events[-1][1]
    canvas = json.loads(done["definition"])
    nodes = {n["id"]: n for n in canvas["nodes"]}
    # the empty source the editor starts with is filled, not doubled
    assert set(nodes) == {"source", "op-1"} and nodes["source"]["data"]["datasourceId"] == world["orders"].id
    assert nodes["source"]["data"]["parquetKey"] == "datasets/1/o.parquet" and nodes["source"]["data"]["filename"] == "orders"
    assert nodes["op-1"]["data"] == {"opType": "filter", "params": {"column": "country", "operator": "eq", "value": "IT"}}
    assert nodes["op-1"]["position"] == {"x": 290, "y": 140}
    assert canvas["edges"] == [{"id": "e-source-left-op-1", "source": "source", "target": "op-1", "sourceHandle": None, "targetHandle": "left"}]
    assert done["touched"] == ["source", "op-1"]
    # the step was checked on a preview of the chain, as the editor's, with the AI's identity
    sent = fake_engine.previews[-1]
    assert sent["input_key"] == "datasets/1/o.parquet" and sent["principal"] == "ai" and sent["slot"].endswith(":op-1")
    assert {"type": "filter", "params": {"column": "country", "operator": "eq", "value": "IT"}} in sent["operations"]
    step = next(d for n, d in events if n == "tool_result" and d["name"] == "add_step")
    assert step["ok"] is True and step["node"] == "op-1"
    # the catalogue tools are the data assistant's: the user sees only what they may read
    listed = next(d for n, d in events if n == "tool_result" and d["name"] == "list_datasources")
    assert listed["ok"] is True and listed["count"] == 1
    # the flow itself is untouched: saving is the user's
    assert json.loads(session.get(Flow, world["flow"].id).definition) == EMPTY_CANVAS
    # the conversation is kept as the editor's, about this flow, and out of the assistant's list
    chat = session.get(AiChat, done["chat_id"])
    assert (chat.kind, chat.flow_id) == ("flow", world["flow"].id)
    reopened = ai_routes.latest_flow_chat(world["flow"].id, user=world["editor"], session=session)
    assert reopened["id"] == chat.id
    # reopened, each step still names the node it made
    assert [s.get("node") for s in reopened["messages"][0]["steps"] if s["name"] in ("add_source", "add_step")] == ["source", "op-1"]
    assert ai_chats.listing(session, world["editor"], 50) == []
    audits = session.exec(select(AuditLog).where(AuditLog.action == "ai.query")).all()
    assert audits and json.loads(audits[-1].detail)["via"] == "flow editor"


@pytest.mark.anyio
async def test_a_turn_that_changes_nothing_sends_no_canvas(session, world, fake_engine, monkeypatch):
    events = await _turn(session, world, monkeypatch, world["editor"], [("get_flow", {}), "The flow reads nothing yet."])
    assert events[-1][1]["definition"] is None and events[-1][1]["touched"] == []


@pytest.mark.anyio
async def test_the_next_turn_continues_the_same_conversation(session, world, fake_engine, monkeypatch):
    first = await _turn(session, world, monkeypatch, world["editor"], ["Hello."])
    chat_id = first[-1][1]["chat_id"]
    again = await _turn(session, world, monkeypatch, world["editor"], ["Again."], chat_id=chat_id)
    assert again[-1][1]["chat_id"] == chat_id and ai_chats.detail(session, world["editor"], chat_id)["turns"] == 2
    # the data assistant does not continue the editor's conversation
    with pytest.raises(HTTPException) as e:
        await ai_routes.chat(ai_routes.ChatRequest(message="hi", model="gpt-oss-120b", chat_id=chat_id),
                             request=None, user=world["editor"], session=session)
    assert e.value.status_code == 404


# ── who may use it ───────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_it_needs_edit_on_the_flow_and_is_closed_to_observers(session, world, fake_engine, monkeypatch):
    reader = make_user(session, email="r@x.local")
    make_permission(session, user_id=reader.id, project_id=world["sales"].id, capability="view")
    observer = make_user(session, email="o@x.local", is_observer=True)
    for user in (reader, observer):
        with pytest.raises(HTTPException) as e:
            await _turn(session, world, monkeypatch, user, ["hi"])
        assert e.value.status_code == 403
    # a flow not saved yet belongs to nobody: anyone who edits may build it
    events = await _turn(session, world, monkeypatch, reader, ["hi"], flow_id=None)
    assert events[-1][0] == "done"


@pytest.mark.anyio
async def test_a_datasource_the_user_cannot_read_stays_off_the_canvas(session, world, fake_engine, monkeypatch):
    events = await _turn(session, world, monkeypatch, world["editor"], [
        ("add_source", {"datasource_id": world["payroll"].id}),
        "I cannot use it.",
    ])
    result = next(d for n, d in events if n == "tool_result")
    assert result["ok"] is False and "No datasource" in result["error"]
    assert events[-1][1]["definition"] is None


@pytest.mark.anyio
async def test_a_canvas_reading_a_hidden_file_is_not_previewed(session, world, fake_engine, monkeypatch):
    canvas = {"nodes": [{"id": "src-1", "type": "source", "position": {"x": 60, "y": 140},
                         "data": {"bucket": "data-prep", "parquetKey": "datasets/2/p.parquet"}}], "edges": []}
    before = len(fake_engine.previews)
    events = await _turn(session, world, monkeypatch, world["editor"], [("preview", {"node": "src-1"}), "Cannot."], canvas=canvas)
    result = next(d for n, d in events if n == "tool_result")
    assert len(fake_engine.previews) == before  # never reached the engine
    assert result["ok"] is False and "cannot read" in result["error"]


# ── the tools, one by one ────────────────────────────────────────────────────
@pytest.mark.parametrize("operation, params, message", [
    ("filter", {"column": "c", "operator": "like", "value": 1}, "operator must be"),
    ("filter", {"column": "c", "operator": "between", "value": [1]}, "between"),
    ("filter", {"column": "c", "operator": "in", "value": []}, "list"),
    ("group_by", {"by": ["c"], "aggregations": [{"column": "a", "func": "average"}]}, "func among"),
    ("sort", {"by": []}, "list of column names"),
    ("sort", {"by": ["a", "b"], "descending": [True]}, "one per column"),
    ("sql", {"query": "WITH x AS (SELECT 1) SELECT * FROM x"}, "starts with SELECT"),
    ("cast", {"columns": {"a": "integer"}}, "types among"),
    ("join", {"how": "outer", "on": ["id"]}, "how must be"),
    ("foreach", {}, "Unknown operation"),
])
def test_wrong_params_tell_the_model_what_to_fix(operation, params, message):
    with pytest.raises(ModelRetry) as e:
        clean_params(operation, params)
    assert message in str(e.value)


def test_params_come_out_as_the_editor_form_writes_them():
    assert clean_params("sort", {"by": ["amount"], "descending": True}) == {"by": ["amount"], "descending": [True]}
    assert clean_params("sort", {"by": ["a", "b"], "descending": [False, True]}) == {"by": ["a", "b"], "descending": [False, True]}
    assert clean_params("sort", {"by": "a"}) == {"by": ["a"], "descending": [False]}
    assert clean_params("filter", {"column": "x", "operator": "is_null", "value": 3}) == {"column": "x", "operator": "is_null"}
    assert clean_params("join", {"how": "left", "left_on": ["a"], "right_on": ["b"]}) == {"how": "left", "left_on": ["a"], "right_on": ["b"]}
    assert clean_params("sql", {"query": " SELECT * FROM self; "}) == {"query": "SELECT * FROM self"}
    assert clean_params("unique", {}) == {}


def test_new_ids_follow_the_editor_counters():
    draft = FlowDraft({"nodes": [{"id": "op-7", "type": "operation"}, {"id": "ctl-4", "type": "refresh"}, {"id": "source", "type": "source"}]})
    assert (draft.next_id("op-"), draft.next_id("out-"), draft.next_id("src-")) == ("op-8", "out-5", "src-1")


def test_a_definition_out_of_shape_is_refused():
    for text in ("[]", '{"nodes": "x"}', '{"nodes": [{"type": "source"}]}', "{" * 3):
        with pytest.raises(HTTPException) as e:
            ai_flow_agent.parse_definition(text)
        assert e.value.status_code == 422


@pytest.mark.anyio
async def test_removing_a_step_keeps_the_chain_connected_and_joins_need_a_right_input(session, world, fake_engine, monkeypatch):
    canvas = {"nodes": [
        {"id": "source", "type": "source", "position": {"x": 60, "y": 140}, "data": {"datasourceId": world["orders"].id, "parquetKey": "datasets/1/o.parquet"}},
        {"id": "op-1", "type": "operation", "position": {"x": 290, "y": 140}, "data": {"opType": "limit", "params": {"n": 5}}},
        {"id": "out-1", "type": "output", "position": {"x": 520, "y": 140}, "data": {"destType": "datasource", "name": "x", "projectId": world["sales"].id}},
    ], "edges": [
        {"id": "e1", "source": "source", "target": "op-1", "targetHandle": "left"},
        {"id": "e2", "source": "op-1", "target": "out-1", "targetHandle": "left"},
    ]}
    events = await _turn(session, world, monkeypatch, world["editor"], [
        ("remove_node", {"node": "op-1"}),
        ("add_step", {"after": "source", "operation": "join", "params": {"how": "inner", "on": ["id"]}}),
        "Done.",
    ], canvas=canvas)
    results = [d for n, d in events if n == "tool_result"]
    assert results[0]["ok"] is True and results[1]["ok"] is False and "needs right" in results[1]["error"]
    canvas_after = json.loads(events[-1][1]["definition"])
    assert [n["id"] for n in canvas_after["nodes"]] == ["source", "out-1"]
    assert [(e["source"], e["target"]) for e in canvas_after["edges"]] == [("source", "out-1")]


@pytest.mark.anyio
async def test_an_output_goes_where_the_user_may_save(session, world, fake_engine, monkeypatch):
    canvas = {"nodes": [{"id": "source", "type": "source", "position": {"x": 60, "y": 140},
                         "data": {"datasourceId": world["orders"].id, "parquetKey": "datasets/1/o.parquet"}}], "edges": []}
    events = await _turn(session, world, monkeypatch, world["editor"], [
        ("add_output", {"after": "source", "datasource_name": "IT orders", "folder_id": world["secret"].id}),
        ("add_output", {"after": "source", "datasource_name": "IT orders"}),
        "Done.",
    ], canvas=canvas)
    results = [d for n, d in events if n == "tool_result"]
    assert results[0]["ok"] is False and "cannot save" in results[0]["error"]
    out = next(n for n in json.loads(events[-1][1]["definition"])["nodes"] if n["type"] == "output")
    assert out["data"] == {"destType": "datasource", "mode": "append", "name": "IT orders", "projectId": world["sales"].id, "overwrite": False}


# ── what the review of 2026-10-15 found ──────────────────────────────────────
@pytest.mark.anyio
async def test_a_turn_stopped_by_its_cap_hands_back_what_it_built_and_is_counted(session, world, fake_engine, monkeypatch, ai_on):
    ai_on.ai.max_flow_requests = 3
    events = await _turn(session, world, monkeypatch, world["editor"], [
        ("add_source", {"datasource_id": world["orders"].id}),
        ("add_step", {"after": "source", "operation": "limit", "params": {"n": 5}}),
        ("add_step", {"after": "op-1", "operation": "limit", "params": {"n": 3}}),
        ("add_step", {"after": "op-2", "operation": "limit", "params": {"n": 2}}),
        "never reached",
    ])
    name, error = events[-1]
    assert name == "error" and error["code"] == "too_many_steps"
    built = json.loads(error["definition"])
    assert [n["id"] for n in built["nodes"]] == ["source", "op-1", "op-2"] and error["touched"] == ["source", "op-1", "op-2"]
    # not saved as a turn, but what it cost is on the ledger the daily caps read
    assert session.exec(select(AiChat)).all() == []
    spent = session.exec(select(AiSpend)).all()
    assert len(spent) == 1 and spent[0].user_id == world["editor"].id and spent[0].input_tokens > 0


@pytest.mark.anyio
async def test_an_engine_that_does_not_answer_is_a_step_warning_not_a_crash(session, world, fake_engine, monkeypatch):
    import httpx

    class Down:
        async def post(self, *a, **kw):
            raise httpx.ConnectError("engine down")

    monkeypatch.setattr(ai_flow_agent, "get_engine_client", lambda: Down())
    events = await _turn(session, world, monkeypatch, world["editor"], [
        ("add_source", {"datasource_id": world["orders"].id}),
        ("add_step", {"after": "source", "operation": "limit", "params": {"n": 5}}),
        "The engine is down.",
    ])
    step = next(d for n, d in events if n == "tool_result" and d["name"] == "add_step")
    assert step["ok"] is True and "did not answer" in step["warning"]
    assert events[-1][0] == "done"
    audit_row = session.exec(select(AuditLog).where(AuditLog.action == "ai.query")).all()[-1]
    assert audit_row.outcome == "failure"


@pytest.mark.anyio
async def test_a_new_flows_conversation_follows_it_once_saved(session, world, fake_engine, monkeypatch):
    first = await _turn(session, world, monkeypatch, world["editor"], ["Hi."], flow_id=None)
    chat_id = first[-1][1]["chat_id"]
    assert session.get(AiChat, chat_id).flow_id is None
    await _turn(session, world, monkeypatch, world["editor"], ["Again."], chat_id=chat_id)  # the flow is saved now
    assert session.get(AiChat, chat_id).flow_id == world["flow"].id
    assert ai_routes.latest_flow_chat(world["flow"].id, user=world["editor"], session=session)["id"] == chat_id
    other = make_flow(session, name="other", project_id=world["sales"].id, definition=json.dumps(EMPTY_CANVAS))
    with pytest.raises(HTTPException) as e:
        await _turn(session, world, monkeypatch, world["editor"], ["Elsewhere."], chat_id=chat_id, flow_id=other.id)
    assert e.value.status_code == 404


@pytest.mark.anyio
async def test_deleting_a_flow_deletes_its_conversations(session, world, fake_engine, monkeypatch):
    from app.models.ai_chat import AiChatTurn
    from app.routes.flows import delete_flow

    done = (await _turn(session, world, monkeypatch, world["editor"], ["Hi."]))[-1][1]
    delete_flow(world["flow"].id, request=None, user=world["admin"], session=session)
    assert session.get(AiChat, done["chat_id"]) is None
    assert session.exec(select(AiChatTurn).where(AiChatTurn.chat_id == done["chat_id"])).all() == []
    assert len(session.exec(select(AiSpend)).all()) == 1  # the bill stays


@pytest.mark.anyio
async def test_loops_are_left_alone_and_only_joins_take_a_right_input(session, world, fake_engine, monkeypatch):
    canvas = {"nodes": [
        {"id": "source", "type": "source", "position": {"x": 60, "y": 140}, "data": {"datasourceId": world["orders"].id, "parquetKey": "datasets/1/o.parquet"}},
        {"id": "op-1", "type": "operation", "position": {"x": 290, "y": 140}, "data": {"opType": "limit", "params": {"n": 5}}},
        {"id": "op-2", "type": "foreach", "position": {"x": 520, "y": 140}, "data": {"opType": "foreach", "params": {}}},
        {"id": "op-3", "type": "operation", "position": {"x": 20, "y": 40}, "parentNode": "op-2", "data": {"opType": "limit", "params": {"n": 1}}},
    ], "edges": [{"id": "e1", "source": "source", "target": "op-1", "targetHandle": "left"}]}
    events = await _turn(session, world, monkeypatch, world["editor"], [
        ("change_step", {"node": "op-3", "params": {"n": 2}}),
        ("connect", {"from_node": "op-1", "to_node": "op-3"}),
        ("connect", {"from_node": "source", "to_node": "op-1", "input": "right"}),
        "Left alone.",
    ], canvas=canvas)
    results = [d for n, d in events if n == "tool_result"]
    assert [r["ok"] for r in results] == [False, False, False]
    assert "by hand" in results[0]["error"] and "by hand" in results[1]["error"] and "right input" in results[2]["error"]
    assert events[-1][1]["definition"] is None


def test_new_ids_start_after_the_editors_counters():
    draft = FlowDraft({"nodes": [{"id": "op-2", "type": "operation"}]}, {"op-": 9, "bogus": 3, "src-": -1})
    assert (draft.next_id("op-"), draft.next_id("op-"), draft.next_id("src-")) == ("op-10", "op-11", "src-1")


@pytest.mark.anyio
async def test_a_flow_keeps_its_engine_after_the_administrator_disallows_it(session, world, fake_engine, monkeypatch):
    from app.models import DisabledEngine

    flow = session.get(Flow, world["flow"].id)
    flow.engine = "duckdb"
    session.add(flow)
    session.add(DisabledEngine(engine_id="duckdb"))
    session.commit()
    events = await _turn(session, world, monkeypatch, world["editor"], ["Hi."], engine="duckdb")
    assert events[-1][0] == "done"
    with pytest.raises(HTTPException) as e:  # a new flow cannot pick it
        await _turn(session, world, monkeypatch, world["editor"], ["Hi."], engine="duckdb", flow_id=None)
    assert e.value.status_code == 422


# ── the hypothesis tests, from the catalogue's copy ──────────────────────────
def test_every_test_of_the_catalogue_is_an_operation_with_a_line_of_reference_and_its_defaults_pass():
    catalogue = ai_flow_agent.STAT_CATALOG["tests"]
    assert catalogue and all(t["operation"] in ai_flow_agent.OPERATIONS for t in catalogue)
    reference = ai_flow_agent.context(None, "en")
    for t in catalogue:
        assert f"- {t['operation']} {{" in reference
        needed = {k: ("yes" if kind == "success" else "c") for k, kind in t["needs"].items()}
        defaults = {k: v for k, v in t["defaults"].items() if k not in needed and v is not None}
        assert clean_params(t["operation"], {**needed, **defaults}) == {**needed, **defaults}
    assert "stat_t_two_sample {value, group, groups?: [2 values], var_equal?: bool" in reference
    assert "detail" not in ai_flow_agent.stat_reference()  # the report's key is the node's, not the assistant's


def test_a_configured_test_comes_out_as_given_and_a_null_is_a_key_not_given():
    assert clean_params("stat_anova", {"value": "amount", "group": "country", "posthoc": "tukey", "p_adjust": None}) == \
        {"value": "amount", "group": "country", "posthoc": "tukey"}
    assert clean_params("stat_t_two_sample", {"value": "amount", "group": "country", "groups": ["IT", "FR"],
                                              "var_equal": False, "alternative": "greater", "alpha": 0.01})["groups"] == ["IT", "FR"]


@pytest.mark.parametrize("operation, params, message", [
    ("stat_anova", {"value": "amount"}, "stat_anova: 'group' is required"),
    ("stat_anova", {"value": "amount", "group": "c", "group_by": "x"}, "there is no parameter 'group_by'"),
    ("stat_anova", {"value": "amount", "group": "c", "posthoc": "scheffe"}, "'posthoc' must be one of none, tukey"),
    ("stat_anova", {"value": "amount", "group": "c", "var_equal": False, "posthoc": "tukey"}, "Games"),
    ("stat_anova", {"value": "amount", "group": "c", "var_equal": "no"}, "true or false"),
    ("stat_t_two_sample", {"value": "a", "group": "c", "groups": ["IT"]}, "a list of 2 values"),
    ("stat_t_two_sample", {"value": "a", "group": "c", "groups": ["IT", "IT"]}, "2 different values"),
    ("stat_t_two_sample", {"value": "a", "group": "c", "alpha": 1.5}, "a number above 0 and below 1"),
    ("stat_t_two_sample", {"value": "a", "group": "c", "mu": "zero"}, "must be a number"),
    ("stat_t_paired", {"x": "a"}, "'y' is required"),
    ("stat_chi2_gof", {"value": "c", "expected": {"a": 0.5, "b": 0.2}}, "summing to 1"),
    ("stat_prop_one_sample", {"value": "c", "success": "  "}, "one value of the column 'value'"),
])
def test_a_test_with_wrong_parameters_is_refused_with_what_to_fix(operation, params, message):
    with pytest.raises(ModelRetry) as e:
        clean_params(operation, params)
    assert message in str(e.value)


@pytest.mark.anyio
async def test_an_assistant_puts_a_configured_test_on_the_draft_and_a_wrong_one_is_sent_back(session, world, fake_engine, monkeypatch):
    fake_engine.preview_response = (200, {"columns": [{"name": "statistic", "dtype": "float"}], "rows": [{"statistic": 3.1}],
                                          "row_count": 1, "truncated": False})
    anova = {"value": "amount", "group": "country", "posthoc": "tukey", "p_adjust": "holm", "var_equal": True}
    events = await _turn(session, world, monkeypatch, world["editor"], [
        ("add_source", {"datasource_id": world["orders"].id}),
        ("add_step", {"after": "source", "operation": "stat_anova", "params": {**anova, "posthoc": "scheffe"}}),
        ("add_step", {"after": "source", "operation": "stat_anova", "params": anova}),
        "Added a one-way ANOVA with Tukey's post-hoc (op-1).",
    ], message="does the amount differ between countries?")
    assert "error" not in [n for n, _ in events]
    steps = [d for n, d in events if n == "tool_result" and d["name"] == "add_step"]
    assert [s["ok"] for s in steps] == [False, True] and "posthoc" in steps[0]["error"]
    canvas = json.loads(events[-1][1]["definition"])
    node = next(n for n in canvas["nodes"] if n["id"] == "op-1")
    assert node["data"] == {"opType": "stat_anova", "params": anova}
    assert fake_engine.previews[-1]["operations"][-1]["type"] == "stat_anova"


# ── the statistical models, from the catalogue's copy ────────────────────────
def _sample(kind: dict, key: str):
    """A value of that kind for a key: the columns differ from key to key."""
    control = kind["control"]
    return {"column": f"col_{key}", "columns": [f"col_{key}"], "level": "yes", "choice": (kind.get("choices") or [None])[0]}[control]


def test_every_model_of_the_catalogue_is_an_operation_with_a_line_of_reference_and_its_defaults_pass():
    catalogue = ai_flow_agent.MODEL_CATALOG
    assert ai_flow_agent.MODELS and len(ai_flow_agent.MODEL_OPERATIONS) == len(catalogue["models"])
    reference = ai_flow_agent.context(None, "en")
    assert f"- {ai_flow_agent.PREDICT_OPERATION} {{connectionId, experiment, registeredModel, version: number|latest" in reference
    for operation, model_id in ai_flow_agent.MODEL_OPERATIONS.items():
        m = ai_flow_agent.MODELS[model_id]
        assert operation in ai_flow_agent.OPERATIONS and f"- {operation} {{" in reference
        needed = {k: _sample(catalogue["kinds"][kind], k) for k, kind in m["needs"].items()}
        full = {**m["defaults"], **needed}
        defaults = {k: v for k, v in m["defaults"].items()
                    if k not in needed and ai_flow_agent._given(v) and ai_flow_agent.model_applies(model_id, k, full)}
        assert clean_params(operation, {**needed, **defaults}) == {**needed, **defaults}
    assert "penalty?: none|ridge|lasso|elastic_net" in reference  # the penalties of a regression
    assert "variants: unpenalized (penalty = none); penalized (penalty = ridge or lasso or elastic_net)" in reference
    assert "Kubernetes" in reference and "registerAs" in reference


def test_a_configured_model_comes_out_as_given_with_its_mlflow_block_apart():
    lasso = {"outcome": "amount", "predictors": ["quantity", "price"], "penalty": "lasso", "lambda": "cv_min", "folds": 5, "se": None}
    assert clean_params("model_linear", lasso) == {k: v for k, v in lasso.items() if v is not None}
    clean = clean_params("model_cox", {"time": "t", "event": "e", "event_value": 1, "predictors": ["age"], "se": "robust", "cluster": "site",
                                       "mlflow": {"connectionId": 3, "experiment": " Pasta ", "registerAs": "cox"}})
    assert clean["mlflow"] == {"connectionId": 3, "experiment": "Pasta", "registerAs": "cox"}
    data = ai_flow_agent.model_node_data("model_cox", clean)
    assert data["model"] == "cox" and "mlflow" not in data["params"] and data["mlflow"]["connectionId"] == 3
    assert clean_params("model_predict", {"connectionId": 3, "experiment": "Pasta", "registeredModel": "cox", "version": 7,
                                          "interval": False, "at_times": [24, 12]}) == \
        {"connectionId": 3, "experiment": "Pasta", "registeredModel": "cox", "version": "7", "at_times": [12, 24]}


_BASE = {"outcome": "amount", "predictors": ["quantity", "price"]}


@pytest.mark.parametrize("operation, params, message", [
    ("model_linear", {"outcome": "amount"}, "linear: 'predictors' is required"),
    ("model_linear", {**_BASE, "family": "gamma"}, "there is no parameter 'family'"),
    ("model_linear", {**_BASE, "penalty": "scad"}, "'penalty' must be one of none, ridge, lasso, elastic_net"),
    ("model_linear", {**_BASE, "penalty": "lasso", "se": "hc3"}, "'se' only applies when penalty = none"),
    ("model_linear", {**_BASE, "penalty": "ridge", "mixing": 0.3}, "'mixing' only applies when penalty = elastic_net"),
    ("model_linear", {**_BASE, "cluster": "site"}, "'cluster' only applies when se = cluster and penalty = none"),
    ("model_linear", {**_BASE, "order_by": "t", "penalty": "lasso"}, "only applies when penalty = none"),
    ("model_linear", {**_BASE, "estimation": "wls"}, "weighted least squares need the column of the weights"),
    ("model_linear", {**_BASE, "se": "cluster"}, "clustered standard errors need the column of the clusters"),
    ("model_linear", {**_BASE, "outcome": "quantity"}, "the column «quantity» is both 'outcome' and 'predictors'"),
    ("model_linear", {**_BASE, "alpha": 1.5}, "'alpha' must be a number above 0 and below 1"),
    ("model_linear", {**_BASE, "folds": 2, "penalty": "lasso"}, "'folds' must be a whole number from 3 to 20"),
    ("model_linear", {**_BASE, "predictors": []}, "'predictors' must name at least 1 column"),
    ("model_linear", {**_BASE, "predictors": ["a", "a"]}, "names a column twice"),
    ("model_linear", {**_BASE, "reference": {"month": "may"}}, "'reference' names «month», which is not one of the 'predictors'"),
    ("model_linear", {**_BASE, "lambda": "best", "penalty": "ridge"}, "'lambda' must be one of cv_1se, cv_min, or a number above 0"),
    ("model_logistic", {**_BASE, "event_value": 1, "link": "probit", "estimation": "firth"}, "Firth's penalized likelihood is for the logit link"),
    ("model_logistic", {**_BASE, "event_value": 1, "se": "hc1", "ci": "profile"}, "profile likelihood intervals go with the classical standard errors"),
    ("model_kaplan_meier", {"time": "t", "event": "e", "event_value": 1, "pairwise": True}, "'pairwise' only applies when 'strata' is given"),
    ("model_kaplan_meier", {"time": "t", "event": "e", "event_value": 1, "times": [3, 3]}, "holds a number twice"),
    ("model_arima", {"time": "t", "value": "v", "frequency": "month", "criterion": "bic"}, "'criterion' only applies when orders = auto"),
    ("model_arima", {"time": "t", "value": "v", "frequency": "month", "p": 9}, "'p' must be a whole number from 0 to 5"),
    ("model_arima", {"time": "t", "value": "v", "frequency": "month", "horizon": 6, "exog": ["promo"]}, "'horizon' only applies when 'exog' is not given"),
    ("model_arima", {"time": "t", "value": "v"}, "'frequency' is required"),
    ("model_linear", {**_BASE, "mlflow": {"experiment": "Pasta"}}, "'connectionId' must be the id of an MLflow connection"),
    ("model_linear", {**_BASE, "mlflow": {"connectionId": 3}}, "'experiment' is required with a connection"),
    ("model_predict", {"connectionId": 3, "experiment": "Pasta", "registeredModel": "x"}, "'version' must be a version number"),
    ("model_predict", {"connectionId": 3, "experiment": "Pasta", "registeredModel": "x", "version": "7.1"}, "'version' must be a version number"),
    ("model_predict", {"experiment": "Pasta", "registeredModel": "x", "version": "7"}, "'connectionId' must be the id"),
    ("model_predict", {"connectionId": 3, "experiment": "Pasta", "registeredModel": "x", "version": "7", "penalty": "lasso"}, "Predict has no option 'penalty'"),
    ("model_predict", {"connectionId": 3, "experiment": "Pasta", "registeredModel": "x", "version": "7", "threshold": 1}, "'threshold' must be a number above 0 and below 1"),
])
def test_a_model_with_wrong_parameters_is_refused_with_what_to_fix(operation, params, message):
    with pytest.raises(ModelRetry) as e:
        clean_params(operation, params)
    assert message in str(e.value)


def test_the_reference_tells_the_assistant_what_a_model_needs_to_run():
    reference = ai_flow_agent.context(None, "en")
    for sentence in ("Kubernetes", "group of models an administrator has enabled", "there is no preview",
                     "only when it has `mlflow:", "needs that registered version"):
        assert sentence in reference


@pytest.mark.anyio
async def test_an_assistant_puts_a_lasso_regression_on_the_draft_and_a_wrong_one_is_sent_back(session, world, fake_engine, monkeypatch):
    lasso = {"outcome": "amount", "predictors": ["quantity", "price"], "penalty": "lasso", "lambda": "cv_1se", "folds": 5}
    events = await _turn(session, world, monkeypatch, world["editor"], [
        ("add_source", {"datasource_id": world["orders"].id}),
        ("add_step", {"after": "source", "operation": "model_linear", "params": {**lasso, "se": "hc3"}}),
        ("add_step", {"after": "source", "operation": "model_linear", "params": lasso}),
        ("add_step", {"after": "ctl-1", "operation": "limit", "params": {"n": 5}}),
        "Added a lasso regression of the amount (ctl-1); it runs with the flow.",
    ], message="fit the amount on quantity and price with a lasso")
    assert "error" not in [n for n, _ in events]
    steps = [d for n, d in events if n == "tool_result" and d["name"] == "add_step"]
    assert [s["ok"] for s in steps] == [False, True, False]
    assert "'se' only applies when penalty = none" in steps[0]["error"] and "must be a source or a step" in steps[2]["error"]
    canvas = json.loads(events[-1][1]["definition"])
    node = next(n for n in canvas["nodes"] if n["id"] == "ctl-1")
    assert node["type"] == "model"
    assert node["data"] == {"model": "linear", "params": lasso, "timeoutSeconds": 900, "retries": 0}
    assert any(e["source"] == "source" and e["target"] == "ctl-1" and e["targetHandle"] == "left" for e in canvas["edges"])
    assert fake_engine.previews == []  # a model runs as a Job: nothing to preview


# ── the check is idempotent: what it keeps, given back, is kept as it is ─────
# The same test is in the engine (backend/tests/test_models_catalog.py), the gateway
# (internal/modelcatalog) and the form (frontend/tests/model-catalog.test.mjs).

def _twice(what, operation, params):
    """clean(clean(x)) == clean(x), in the shape and in the values. A set that is refused has nothing to repeat."""
    try:
        first = clean_params(operation, params)
    except ModelRetry:
        return
    second = clean_params(operation, first)
    assert json.dumps(second, sort_keys=True) == json.dumps(first, sort_keys=True), (what, operation)


def test_checking_a_model_twice_is_checking_it_once():
    catalogue = ai_flow_agent.MODEL_CATALOG
    for operation, model_id in ai_flow_agent.MODEL_OPERATIONS.items():
        m = ai_flow_agent.MODELS[model_id]
        needed = {k: _sample(catalogue["kinds"][kind], k) for k, kind in m["needs"].items()}
        full = {**m["defaults"], **needed}
        defaults = {k: v for k, v in m["defaults"].items()
                    if k not in needed and ai_flow_agent._given(v) and ai_flow_agent.model_applies(model_id, k, full)}
        _twice("the defaults", operation, {**needed, **defaults})
        _twice("only what it needs", operation, needed)
        picks = []
        for key, kind in m["takes"].items():
            control = catalogue["kinds"][kind]["control"]
            if control == "choice":
                picks += [(key, c) for c in catalogue["kinds"][kind]["choices"]]
            elif control == "boolean":
                picks += [(key, True), (key, False)]
        for i, a in enumerate(picks):
            _twice(a, operation, {**needed, a[0]: a[1]})
            for b in picks[i + 1:]:
                if a[0] != b[0]:
                    _twice((a, b), operation, {**needed, a[0]: a[1], b[0]: b[1]})


def test_checking_the_shared_cases_twice_is_checking_them_once():
    cases_file = Path(__file__).resolve().parents[2] / "backend" / "tests" / "fixtures" / "models" / "params_cases.json"
    if not cases_file.exists():
        pytest.skip("the engine's sources are not beside the gateway's")
    cases = json.loads(cases_file.read_text(encoding="utf-8"))["cases"]
    assert len(cases) >= 40
    for case in cases:
        operation = f"model_{case['model']}"
        if operation not in ai_flow_agent.OPERATIONS:
            continue  # «there is no model»
        _twice(case["what"], operation, case["params"])
        if "checked" in case:
            _twice(f"{case['what']} (its answer)", operation, case["checked"])


def test_an_empty_list_of_columns_stays_an_empty_list():
    out = clean_params("model_arima", {"time": "t", "value": "v", "frequency": "month", "exog": []})
    assert out.get("exog", []) == []
