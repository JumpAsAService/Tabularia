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
        # a key the node needs once the others are chosen (the experiments' metric, which a metric's type and an input shape
        # make needed): a column each, as the nodes' own `requires` say
        for rule in m["requires"]:
            if ai_flow_agent._holds_all(rule["when"], full) and not ai_flow_agent._given(full.get(rule["key"])):
                needed[rule["key"]] = full[rule["key"]] = _sample(catalogue["kinds"][{**m["needs"], **m["takes"]}[rule["key"]]], rule["key"])
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


_BOOST = {"outcome": "churned", "predictors": ["quantity", "price"]}


@pytest.mark.parametrize("model_id", ["xgboost", "catboost"])
def test_a_boosted_model_has_its_line_of_reference_with_the_variants_by_task_and_its_defaults_pass(model_id):
    reference = ai_flow_agent.context(None, "en")
    line = next(l for l in reference.splitlines() if l.startswith(f"- model_{model_id} {{task: regression|binary|multiclass"))
    assert line.endswith("(boosting)") and "stop_metric?: loss|mae|auc|error" in line and "validation?: random|time" in line
    assert ("min_child_weight?: number" in line) == (model_id == "xgboost") and ("l2_leaf_reg?: number" in line) == (model_id == "catboost")
    assert "  variants: regression (task = regression); binary (task = binary); multiclass (task = multiclass)." in reference
    assert "event_value, threshold only when task = binary" in reference
    for task, extra in (("regression", {}), ("binary", {"event_value": "yes", "threshold": 0.3}), ("multiclass", {"class_weight": "balanced"})):
        given = {**_BOOST, "task": task, **extra}
        assert clean_params(f"model_{model_id}", given) == given
        defaults = {k: v for k, v in ai_flow_agent.MODELS[model_id]["defaults"].items() if ai_flow_agent._given(v) and k not in given
                    and ai_flow_agent.model_applies(model_id, k, {**ai_flow_agent.MODELS[model_id]["defaults"], **given})}
        assert clean_params(f"model_{model_id}", {**given, **defaults}) == {**given, **defaults}
    assert "never put in `predictors` a column computed from the outcome" in reference


@pytest.mark.parametrize("operation, params, message", [
    ("model_xgboost", _BOOST, "'task' is required"),
    ("model_xgboost", {**_BOOST, "task": "tree"}, "'task' must be one of regression, binary, multiclass"),
    ("model_xgboost", {**_BOOST, "task": "binary"}, "a binary task needs the outcome's value whose probability is predicted ('event_value')"),
    ("model_xgboost", {**_BOOST, "task": "regression", "threshold": 0.3}, "'threshold' only applies when task = binary"),
    ("model_xgboost", {**_BOOST, "task": "regression", "class_weight": "balanced"}, "'class_weight' only applies when task = binary or multiclass"),
    ("model_xgboost", {**_BOOST, "task": "regression", "validation": "time"}, "a validation part in time needs the column of the time ('order_by')"),
    ("model_xgboost", {**_BOOST, "task": "regression", "stop_metric": "auc"}, "the AUC is a binary task's metric"),
    ("model_xgboost", {**_BOOST, "task": "binary", "event_value": "yes", "stop_metric": "mae"}, "the mean absolute error is a regression's metric"),
    ("model_xgboost", {**_BOOST, "task": "regression", "early_stopping": False, "early_stopping_rounds": 10}, "'early_stopping_rounds' only applies when early_stopping = true"),
    ("model_xgboost", {**_BOOST, "task": "regression", "max_depth": 13}, "'max_depth' must be a whole number from 1 to 12"),
    ("model_catboost", {**_BOOST, "task": "regression", "max_depth": 11}, "'max_depth' must be a whole number from 1 to 10"),
    ("model_xgboost", {**_BOOST, "task": "regression", "learning_rate": 0}, "'learning_rate' must be a number above 0 and at most 1"),
    ("model_xgboost", {**_BOOST, "task": "regression", "l2_leaf_reg": 1}, "there is no parameter 'l2_leaf_reg'"),
    ("model_catboost", {**_BOOST, "task": "regression", "reg_lambda": 1}, "there is no parameter 'reg_lambda'"),
    ("model_xgboost", {**_BOOST, "task": "regression", "outcome": "quantity"}, "the column «quantity» is both 'outcome' and 'predictors'"),
])
def test_a_boosted_model_with_wrong_parameters_is_refused_with_what_to_fix(operation, params, message):
    with pytest.raises(ModelRetry) as e:
        clean_params(operation, params)
    assert message in str(e.value)


@pytest.mark.anyio
async def test_an_assistant_puts_a_binary_xgboost_on_the_draft_and_a_binary_without_its_event_is_sent_back(session, world, fake_engine, monkeypatch):
    churn = {"task": "binary", "event_value": "yes", "outcome": "churned", "predictors": ["quantity", "price"],
             "validation": "time", "order_by": "ordered_on", "threshold": 0.3}
    events = await _turn(session, world, monkeypatch, world["editor"], [
        ("add_source", {"datasource_id": world["orders"].id}),
        ("add_step", {"after": "source", "operation": "model_xgboost", "params": {k: v for k, v in churn.items() if k != "event_value"}}),
        ("add_step", {"after": "source", "operation": "model_xgboost", "params": churn}),
        "Added an XGBoost model of the churn (ctl-1); it runs with the flow.",
    ], message="predict which customers will leave, on the later orders")
    assert "error" not in [n for n, _ in events]
    steps = [d for n, d in events if n == "tool_result" and d["name"] == "add_step"]
    assert [s["ok"] for s in steps] == [False, True]
    assert "a binary task needs the outcome's value" in steps[0]["error"]
    canvas = json.loads(events[-1][1]["definition"])
    node = next(n for n in canvas["nodes"] if n["id"] == "ctl-1")
    assert node["type"] == "model"
    assert node["data"] == {"model": "xgboost", "params": churn, "timeoutSeconds": 900, "retries": 0}
    assert any(e["source"] == "source" and e["target"] == "ctl-1" and e["targetHandle"] == "left" for e in canvas["edges"])
    assert fake_engine.previews == []


_SURVIVAL = {"time": "days", "event": "status", "event_value": "lost"}
_CUSTOMER = {"customer": "customer_id", "frequency": "frequency", "recency": "recency", "age": "age", "time_unit": "week"}


def test_the_parametric_survival_node_has_its_line_of_reference_and_the_prompt_says_when_to_choose_it_over_cox():
    reference = ai_flow_agent.context(None, "en")
    line = next(l for l in reference.splitlines() if l.startswith("- model_parametric_survival {time, event, event_value"))
    assert "distribution?: weibull|exponential|lognormal|loglogistic|gengamma|gompertz|auto" in line
    assert all(key in line for key in ("start?", "alive_at?", "aic_margin?: number", "quantiles?: [numbers]", "times?: [numbers]"))
    assert "aic_margin only when distribution = auto" in reference
    assert "choose it over model_cox when the person wants a time" in reference and "`distribution: auto`" in reference
    assert "`alive_at` is how long each unit to be predicted has already lasted" in reference
    assert "quantiles for model_parametric_survival; alive_at for model_parametric_survival" in reference
    for given in ({}, {"distribution": "auto", "aic_margin": 0}, {"distribution": "gompertz", "predictors": ["load"], "strata": "line",
                  "start": "entry", "alive_at": "age", "quantiles": [0.1, 0.9], "times": [30, 90], "output": "rows"}):
        assert clean_params("model_parametric_survival", {**_SURVIVAL, **given}) == {**_SURVIVAL, **given}


@pytest.mark.parametrize("params, message", [
    ({}, "'event_value' is required"),
    ({"aic_margin": 2}, "'aic_margin' only applies when distribution = auto"),
    ({"distribution": "auto", "aic_margin": 20}, "'aic_margin' must be a number at least 0 and at most 10"),
    ({"distribution": "gamma"}, "'distribution' must be one of weibull, exponential, lognormal, loglogistic, gengamma, gompertz, auto"),
    ({"start": "days"}, "the column «days» is both 'time' and 'start': choose another"),
    ({"quantiles": [0.5, 1.2]}, "'quantiles' must be a list of numbers, each above 0 and below 1"),
    ({"reference": {"line": "A"}}, "'reference' only applies when 'predictors' is given"),
])
def test_a_parametric_survival_node_with_wrong_parameters_is_refused_with_what_to_fix(params, message):
    given = {k: v for k, v in _SURVIVAL.items() if not (message.startswith("'event_value'") and k == "event_value")}
    with pytest.raises(ModelRetry) as e:
        clean_params("model_parametric_survival", {**given, **params})
    assert message in str(e.value)


@pytest.mark.parametrize("model_id, unit_key", [("clv", "time_unit"), ("bg_bb", "period_unit")])
def test_the_customer_lifetime_nodes_have_their_line_of_reference_and_the_prompt_says_one_row_per_customer(model_id, unit_key):
    reference = ai_flow_agent.context(None, "en")
    line = next(l for l in reference.splitlines() if l.startswith(f"- model_{model_id} {{customer, frequency, recency"))
    assert f"{unit_key}: day|week|month|quarter|year" in line and line.endswith("(customer_lifetime)")
    assert ("purchase_model?: auto|pareto_nbd|bg_nbd|mbg_nbd" in line) == (model_id == "clv")
    assert "one row per customer, never transactions" in reference and "the CLV is of revenue unless `margin` is set" in reference
    assert "a probability from the model, not an observed churn" in reference
    assert "horizon for model_arima, model_clv, model_bg_bb" in reference
    key = "age" if model_id == "clv" else "opportunities"
    given = {**{k: v for k, v in _CUSTOMER.items() if k not in ("age", "time_unit")}, key: "n", unit_key: "month"}
    assert clean_params(f"model_{model_id}", given) == given
    full = {**given, "spend": "avg_spend", "margin": 0.4, "discount_rate": 0.08, "horizon": 12, "holdout_transactions": "later",
            "holdout_length": 6, "output": "results"}
    assert clean_params(f"model_{model_id}", full) == full


@pytest.mark.parametrize("operation, params, message", [
    ("model_clv", {k: v for k, v in _CUSTOMER.items() if k != "time_unit"}, "'time_unit' is required"),
    ("model_clv", {**_CUSTOMER, "holdout_transactions": "later"}, "the holdout's purchases need the holdout's length ('holdout_length')"),
    ("model_clv", {**_CUSTOMER, "compare": True}, "'compare' only applies when purchase_model = pareto_nbd or bg_nbd or mbg_nbd"),
    ("model_clv", {**_CUSTOMER, "margin": 0.4}, "'margin' only applies when 'spend' is given"),
    ("model_clv", {**_CUSTOMER, "spend": "m", "margin": 0}, "'margin' must be a number above 0 and at most 1"),
    ("model_clv", {**_CUSTOMER, "recency": "frequency"}, "the column «frequency» is both 'frequency' and 'recency': choose another"),
    ("model_clv", {**_CUSTOMER, "time_unit": "fortnight"}, "'time_unit' must be one of day, week, month, quarter, year"),
    ("model_bg_bb", {"customer": "c", "frequency": "f", "recency": "r", "opportunities": "n", "period_unit": "month", "purchase_model": "bg_nbd"},
     "there is no parameter 'purchase_model'"),
    ("model_bg_bb", {"customer": "c", "frequency": "f", "recency": "r", "opportunities": "n", "period_unit": "month",
                     "holdout_transactions": "h", "holdout_length": 2.5}, "'holdout_length' must be a whole number from 1 to 1000"),
])
def test_a_customer_lifetime_node_with_wrong_parameters_is_refused_with_what_to_fix(operation, params, message):
    with pytest.raises(ModelRetry) as e:
        clean_params(operation, params)
    assert message in str(e.value)


@pytest.mark.anyio
async def test_an_assistant_puts_a_customer_lifetime_node_after_a_summary_built_with_group_by(session, world, fake_engine, monkeypatch):
    fake_engine.preview_response = (200, {"columns": COLUMNS, "rows": [{"country": "IT", "amount": 3.5}], "row_count": 1, "truncated": False})
    summary = {"by": ["country"], "aggregations": [{"column": "amount", "func": "count", "alias": "days"}]}
    clv = {**_CUSTOMER, "customer": "country", "frequency": "days", "recency": "days", "spend": "amount", "purchase_model": "auto"}
    events = await _turn(session, world, monkeypatch, world["editor"], [
        ("add_source", {"datasource_id": world["orders"].id}),
        ("add_step", {"after": "source", "operation": "group_by", "params": summary}),
        ("add_step", {"after": "op-1", "operation": "model_clv", "params": clv}),
        ("add_step", {"after": "op-1", "operation": "model_clv", "params": {**clv, "recency": "recency"}}),
        "Added the customer lifetime value of each customer (ctl-1); it runs with the flow.",
    ], message="how much will each customer buy next year, and are they still active")
    assert "error" not in [n for n, _ in events]
    steps = [d for n, d in events if n == "tool_result" and d["name"] == "add_step"]
    assert [s["ok"] for s in steps] == [True, False, True]
    assert "the column «days» is both 'frequency' and 'recency'" in steps[1]["error"]
    canvas = json.loads(events[-1][1]["definition"])
    node = next(n for n in canvas["nodes"] if n["id"] == "ctl-1")
    assert node["type"] == "model" and node["data"]["model"] == "clv" and node["data"]["params"]["time_unit"] == "week"
    assert any(e["source"] == "op-1" and e["target"] == "ctl-1" and e["targetHandle"] == "left" for e in canvas["edges"])


def test_the_reference_tells_the_assistant_what_a_model_needs_to_run():
    reference = ai_flow_agent.context(None, "en")
    for sentence in ("Kubernetes", "group of models an administrator has enabled", "there is no preview",
                     "only when it has `mlflow:", "needs that registered version"):
        assert sentence in reference


def test_the_reference_names_the_task_of_a_boosted_model_not_an_operation_that_does_not_exist():
    reference = ai_flow_agent.context(None, "en")
    assert "threshold for model_logistic, and for the binary task of model_xgboost and model_catboost" in reference
    assert "model_xgboost:binary" not in reference and "model_catboost:binary" not in reference


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
    assert [s["ok"] for s in steps] == [False, True, True]
    assert "'se' only applies when penalty = none" in steps[0]["error"]
    canvas = json.loads(events[-1][1]["definition"])
    node = next(n for n in canvas["nodes"] if n["id"] == "ctl-1")
    assert node["type"] == "model"
    assert node["data"] == {"model": "linear", "params": lasso, "timeoutSeconds": 900, "retries": 0}
    assert any(e["source"] == "source" and e["target"] == "ctl-1" and e["targetHandle"] == "left" for e in canvas["edges"])
    # a step after the model reads its output: the results table, worked out without a preview
    assert any(e["source"] == "ctl-1" and e["target"] == "op-1" for e in canvas["edges"])
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
        # a key the node needs once the others are chosen (the experiments' metric, which a metric's type and an input shape
        # make needed): a column each, as the nodes' own `requires` say
        for rule in m["requires"]:
            if ai_flow_agent._holds_all(rule["when"], full) and not ai_flow_agent._given(full.get(rule["key"])):
                needed[rule["key"]] = full[rule["key"]] = _sample(catalogue["kinds"][{**m["needs"], **m["takes"]}[rule["key"]]], rule["key"])
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


# ── after a model: the columns, worked out; no rows until it runs ────────────
# The same cases as the engine's (backend/tests/test_models_output_columns.py) and the editor's
# (frontend/tests/model-catalog.test.mjs): what the runners really write.
OUTPUT_CASES = Path(__file__).resolve().parents[2] / "backend" / "tests" / "fixtures" / "models" / "output_columns.json"


def test_the_columns_a_model_hands_on_are_the_runner_s_on_the_shared_cases():
    if not OUTPUT_CASES.exists():
        pytest.skip("the engine's sources are not beside the gateway's")
    import fnmatch
    cases = json.loads(OUTPUT_CASES.read_text(encoding="utf-8"))["cases"]
    assert len(cases) >= 20
    for case in cases:
        inputs = [{"name": n, "dtype": t} for n, t in case["input"]]
        columns, later = ai_flow_agent.model_output_columns(case["model"], case["params"], inputs)
        written = [{"name": n, "dtype": t} for n, t in case["columns"]]
        known = [c for c in written if not any(fnmatch.fnmatchcase(c["name"], p) for p in later)]
        assert columns == known, case["what"]
        assert (len(known) < len(written)) == bool(later), case["what"]


def test_a_model_s_own_columns_are_known_without_its_input():
    columns, later = ai_flow_agent.model_output_columns("linear", {"output": "rows"}, None)
    assert [c["name"] for c in columns] == ["pred_fitted", "pred_residual", "pred_std_residual", "pred_leverage",
                                            "pred_cooks_distance", "pred_low", "pred_high"] and later == []
    columns, later = ai_flow_agent.model_output_columns("xgboost", {"task": "multiclass", "output": "rows"}, [])
    assert [c["name"] for c in columns] == ["pred_class", "pred_prob", "pred_part"] and later == ["pred_prob_*"]
    columns, _ = ai_flow_agent.model_output_columns("arima", {"time": "t", "output": "forecast"}, None)
    assert columns[0] == {"name": "time", "dtype": "Unknown"}


def test_the_steps_that_only_keep_rename_cast_or_filter_columns_say_them_after_a_model():
    cols = [{"name": "a", "dtype": "Int32"}, {"name": "b", "dtype": "Float64"}]
    after = ai_flow_agent.columns_after_step
    assert after("filter", {"column": "a", "operator": "gt", "value": 1}, cols) == cols
    assert after("select", {"columns": ["b"]}, cols) == [cols[1]]
    assert after("drop", {"columns": ["b"]}, cols) == [cols[0]]
    assert after("reorder", {"columns": ["b"]}, cols) == [cols[1], cols[0]]
    assert after("rename", {"mapping": {"a": "x"}}, cols)[0] == {"name": "x", "dtype": "Int32"}
    assert after("cast", {"columns": {"a": "str"}}, cols)[0] == {"name": "a", "dtype": "String"}
    for op in ("group_by", "join", "compute", "sql", "pivot", "stat_t_test"):
        assert after(op, {}, cols) is None, op


def _deps(session, user, canvas):
    from types import SimpleNamespace
    deps = ai_flow_agent.FlowDeps(user=user, session_factory=lambda: nullcontext(session), engine=None, model_id="gpt-oss-120b",
                                  draft=ai_flow_agent.FlowDraft(canvas))
    return SimpleNamespace(deps=deps)


def _source_canvas(world):
    return {"nodes": [{"id": "source", "type": "source", "position": {"x": 60, "y": 140},
                       "data": {"bucket": "data-prep", "datasourceId": world["orders"].id, "parquetKey": "datasets/1/o.parquet",
                                "filename": "orders", "columns": COLUMNS}}], "edges": []}


@pytest.mark.anyio
async def test_a_model_returns_its_columns_and_a_step_after_it_is_added_with_its_own(session, world, fake_engine):
    fake_engine.preview_response = (200, {"columns": COLUMNS, "rows": [{"country": "IT", "amount": 3.5}], "row_count": 1, "truncated": False})
    ctx = _deps(session, world["editor"], _source_canvas(world))
    model = await ai_flow_agent.add_step(ctx, after="source", operation="model_linear",
                                         params={"outcome": "amount", "predictors": ["country"], "output": "rows"})
    # the rows: the input's columns (one preview row of the step before), then the model's, no rows
    assert model["node"] == "ctl-1" and "rows" not in model and "first_rows" not in model
    assert model["columns"][:3] == [*COLUMNS, {"name": "pred_fitted", "dtype": "Float64"}]
    assert "no rows until the model runs" in model["note"] and "not an error" in model["note"]
    assert fake_engine.previews[-1]["limit"] == 1 and fake_engine.previews[-1]["slot"].endswith(":source")
    previews = len(fake_engine.previews)
    # a step after it: allowed, its columns worked out, no preview of it (the engine has no rows to give)
    kept = await ai_flow_agent.add_step(ctx, after="ctl-1", operation="filter",
                                        params={"column": "pred_fitted", "operator": "gt", "value": 0})
    assert kept["node"] == "op-1" and kept["columns"] == model["columns"]
    assert "ctl-1" in kept["note"] and "not an error" in kept["note"] and "preview_error" not in kept
    picked = await ai_flow_agent.add_step(ctx, after="op-1", operation="select", params={"columns": ["country", "pred_fitted"]})
    assert [c["name"] for c in picked["columns"]] == ["country", "pred_fitted"]
    assert all(p["slot"].endswith(":source") for p in fake_engine.previews[previews:])
    # a step whose columns only its rows say: added all the same, with the note
    grouped = await ai_flow_agent.add_step(ctx, after="op-2", operation="group_by",
                                           params={"by": ["country"], "aggregations": [{"column": "pred_fitted", "func": "mean"}]})
    assert "columns" not in grouped and "known only after that run" in grouped["note"]
    # the preview tool says the same, with no rows
    seen = await ai_flow_agent.preview(ctx, node="op-2")
    assert seen["rows"] == [] and [c["name"] for c in seen["columns"]] == ["country", "pred_fitted"]
    flow = await ai_flow_agent.get_flow(ctx)
    assert flow["ends_of_chains"] == ["op-3"]


@pytest.mark.anyio
async def test_a_model_s_results_table_and_a_probability_per_class_are_said_as_they_are(session, world, fake_engine):
    ctx = _deps(session, world["editor"], _source_canvas(world))
    results = await ai_flow_agent.add_step(ctx, after="source", operation="model_linear",
                                           params={"outcome": "amount", "predictors": ["country"]})
    assert [c["name"] for c in results["columns"]] == ["section", "level", "level_2", "stat", "value", "text", "x0", "x1", "ord", "key"]
    assert fake_engine.previews == []  # the results table needs nothing of the input
    multi = await ai_flow_agent.add_step(ctx, after="source", operation="model_xgboost",
                                         params={"task": "multiclass", "outcome": "country", "predictors": ["amount"], "output": "rows"})
    assert multi["columns_after_the_run"] == ["pred_prob_*"] and "one column for each value in the data" in multi["note"]
    changed = await ai_flow_agent.change_step(ctx, node="ctl-2", params={"task": "regression", "outcome": "amount",
                                                                        "predictors": ["country"], "output": "rows"})
    assert "columns_after_the_run" not in changed and changed["columns"][-3:] == [
        {"name": "pred_value", "dtype": "Float64"}, {"name": "pred_residual", "dtype": "Float64"}, {"name": "pred_part", "dtype": "String"}]
    # a model nothing reads yet is the end of its chain: the place to add the next step
    assert (await ai_flow_agent.get_flow(ctx))["ends_of_chains"] == ["ctl-1", "ctl-2"]
    # an Output may save what a model hands on
    saved = await ai_flow_agent.add_output(ctx, after="ctl-2", datasource_name="scores", folder_id=world["sales"].id)
    assert saved["node"].startswith("out-") and any(e["source"] == "ctl-2" and e["target"] == saved["node"] for e in ctx.deps.draft.edges)


@pytest.mark.anyio
async def test_in_metadata_only_mode_a_model_says_only_its_own_columns(session, world, fake_engine):
    ctx = _deps(session, world["editor"], _source_canvas(world))
    ctx.deps.metadata_turn = True
    model = await ai_flow_agent.add_step(ctx, after="source", operation="model_linear",
                                         params={"outcome": "amount", "predictors": ["country"], "output": "rows"})
    assert [c["name"] for c in model["columns"]][0] == "pred_fitted" and "were not read here" in model["note"]
    after = await ai_flow_agent.add_step(ctx, after="ctl-1", operation="limit", params={"n": 5})
    assert after == {"node": "op-1", "note": ai_flow_agent.metadata_only.STEP_NOT_CHECKED}
    assert fake_engine.previews == []


def test_the_reference_says_steps_can_follow_a_model():
    reference = ai_flow_agent.context(None, "en")
    assert "no step can be added after it" not in reference
    assert "Steps can be added after a model" in reference and "that is not an error" in reference
