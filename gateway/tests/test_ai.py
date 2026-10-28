"""AI assistant: the contracts that count.

1. The assistant sees and queries ONLY the datasources the user can read (VIEW on the
   folder): those outside the permissions do not show in the list and give the same
   error as a datasource that does not exist.
2. `query_datasource` sends the engine a `sql` node on the right datasource, with
   limited rows, without step cache and in the user's slot; it accepts only a
   SELECT; every query (failed ones too) leaves an audit event.
3. The fields' descriptions reach the model together with the columns.
4. The administrator enables the models: none is usable by default, the embeddings
   cannot be enabled, every change ends up in the audit.
5. The chat streams text, tool calls, the result's TABLE and the history to send back
   at the next turn — with a simulated model.
"""
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelRequest, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from sqlmodel import select

import app.core.config as config_mod
from app.models import AiModel, AuditLog
from app.routes import ai as ai_routes
from app.models import AiChat
from app.services import ai_agent, ai_chats, ai_models, audit
from tests.conftest import make_datasource, make_permission, make_project, make_user

PROVIDER = ["gpt-oss-120b", "mistral-small-3.2-24b-instruct-2506", "qwen3-embedding-8b", "whisper-large-v3"]


@pytest.fixture
def ai_on(monkeypatch):
    settings = config_mod.Settings(ai={"base_url": "https://ai.example/v1", "secret_key": "k", "max_result_rows": 7})
    for mod in (config_mod, ai_routes, ai_agent, ai_models):
        monkeypatch.setattr(mod, "get_settings", lambda: settings)

    async def offered(force: bool = False):
        return list(PROVIDER)

    monkeypatch.setattr(ai_routes, "provider_models", offered)
    return settings


def _catalog(session):
    """Two folders: the user reads only the first."""
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    user = make_user(session, email="ana@x.local")
    p1 = make_project(session, name="Vendite")
    p2 = make_project(session, name="Riservato")
    make_permission(session, user_id=user.id, project_id=p1.id, capability="view")
    cols = json.dumps([{"name": "paese", "dtype": "String"}, {"name": "imp", "dtype": "Float64"}])
    visible = make_datasource(session, name="ordini", project_id=p1.id, key="datasets/1/o.parquet", rows=1204,
                              columns=cols, column_descriptions=json.dumps({"imp": "Importo netto in euro, IVA esclusa"}),
                              description="Ordini 2024", sort_keys=json.dumps(["paese"]))
    hidden = make_datasource(session, name="stipendi", project_id=p2.id, key="datasets/2/s.parquet", rows=50, columns=cols)
    return admin, user, visible, hidden


def _ctx(session, user, engine=None):
    deps = ai_agent.ChatDeps(user=user, session_factory=lambda: nullcontext(session), engine=engine, model_id="gpt-oss-120b", locale="it")
    return SimpleNamespace(deps=deps)


# ── 1. permissions ───────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_the_assistant_lists_only_what_the_user_can_read(session, ai_on):
    admin, user, visible, hidden = _catalog(session)
    got = (await ai_agent.list_datasources(_ctx(session, user)))["datasources"]
    assert [d["name"] for d in got] == ["ordini"]
    assert got[0] == {"id": visible.id, "name": "ordini", "folder": "Vendite", "rows": 1204, "columns": 2, "description": "Ordini 2024"}
    assert {d["name"] for d in (await ai_agent.list_datasources(_ctx(session, admin)))["datasources"]} == {"ordini", "stipendi"}
    # searching for what cannot be read does not reveal it: only what is readable comes back, with a note
    hidden_search = await ai_agent.list_datasources(_ctx(session, user), search="STIPENDI")
    assert [d["name"] for d in hidden_search["datasources"]] == ["ordini"] and "Nothing matches" in hidden_search["note"]
    nobody = make_user(session, email="nessuno@x.local")
    assert (await ai_agent.list_datasources(_ctx(session, nobody)))["datasources"] == []


@pytest.mark.anyio
async def test_a_datasource_outside_the_permissions_looks_nonexistent(session, ai_on, fake_engine):
    _, user, _, hidden = _catalog(session)
    with pytest.raises(ModelRetry) as unreadable:
        await ai_agent.describe_datasource(_ctx(session, user), hidden.id)
    with pytest.raises(ModelRetry) as missing:
        await ai_agent.describe_datasource(_ctx(session, user), 9999)
    assert str(unreadable.value).replace(str(hidden.id), "N") == str(missing.value).replace("9999", "N")
    with pytest.raises(ModelRetry):
        await ai_agent.query_datasource(_ctx(session, user), hidden.id, "SELECT * FROM self")
    assert fake_engine.previews == []  # the engine is not even called


# ── 2. descriptions and query ────────────────────────────────────────────────
@pytest.mark.anyio
async def test_describe_carries_the_field_descriptions_and_sample_rows(session, ai_on, fake_engine):
    _, user, visible, _ = _catalog(session)
    fake_engine.preview_response = (200, {"columns": [], "rows": [{"paese": "IT", "imp": 10.5}], "row_count": 1, "truncated": True})
    info = await ai_agent.describe_datasource(_ctx(session, user), visible.id)
    assert info["columns"] == [
        {"name": "paese", "type": "String"},
        {"name": "imp", "type": "Float64", "description": "Importo netto in euro, IVA esclusa"},
    ]
    assert info["sample_rows"] == [{"paese": "IT", "imp": 10.5}] and info["rows"] == 1204
    assert fake_engine.previews[0]["operations"] == [] and fake_engine.previews[0]["limit"] == ai_agent.SAMPLE_ROWS


@pytest.mark.anyio
async def test_query_runs_a_sql_node_on_the_right_source_and_is_audited(session, ai_on, fake_engine):
    _, user, visible, _ = _catalog(session)
    fake_engine.preview_response = (200, {"columns": [{"name": "paese", "dtype": "String"}, {"name": "n", "dtype": "Int64"}],
                                          "rows": [{"paese": "IT", "n": 3}, {"paese": "x" * 900, "n": 1}], "row_count": 2, "truncated": False})
    out = await ai_agent.query_datasource(_ctx(session, user, engine="clickhouse"), visible.id, "SELECT paese, count(*) AS n FROM self GROUP BY paese;", limit=5000)
    sent = fake_engine.previews[-1]
    assert sent["bucket"] == visible.bucket and sent["input_key"] == "datasets/1/o.parquet"
    assert sent["operations"] == [{"type": "sql", "params": {"query": "SELECT paese, count(*) AS n FROM self GROUP BY paese"}}]
    assert sent["limit"] == 7  # the AI__MAX_RESULT_ROWS cap
    # no slot on a new chat: there is no previous question to overtake
    assert sent["no_cache"] is True and "slot" not in sent and sent["engine"] == "clickhouse"
    assert sent["sort_keys"] == ["paese"]
    # the ClickHouse engine runs them with the dedicated read-only user, when there is one
    assert sent["principal"] == "ai"
    assert out["datasource"] == "ordini" and out["row_count"] == 2 and out["rows"][0] == {"paese": "IT", "n": 3}
    assert len(out["rows"][1]["paese"]) == ai_agent.MAX_CELL_CHARS + 1  # long cells cut
    ev = session.exec(select(AuditLog).where(AuditLog.action == audit.AI_QUERY)).one()
    detail = json.loads(ev.detail)
    assert ev.target_id == visible.id and ev.outcome == "success" and ev.actor_id == user.id
    assert detail["sql"].startswith("SELECT paese") and detail["model"] == "gpt-oss-120b" and detail["rows"] == 2


@pytest.mark.anyio
@pytest.mark.parametrize("sql", [
    "DROP TABLE self", "INSERT INTO self VALUES (1)", "SELECT 1 FROM self; DELETE FROM self",
    "SELECT 1", "  ", "UPDATE self SET a = 1", "WITH t AS (SELECT 1) SELECT * FROM self",
])
async def test_only_one_select_from_self_is_accepted(session, ai_on, fake_engine, sql):
    _, user, visible, _ = _catalog(session)
    with pytest.raises(ModelRetry):
        await ai_agent.query_datasource(_ctx(session, user), visible.id, sql)
    assert fake_engine.previews == []


@pytest.mark.anyio
async def test_engine_errors_are_fed_back_or_reported_and_audited_as_failures(session, ai_on, fake_engine):
    _, user, visible, _ = _catalog(session)
    fake_engine.preview_response = (422, {"detail": "Unknown column 'pais'"})
    with pytest.raises(ModelRetry) as e:
        await ai_agent.query_datasource(_ctx(session, user), visible.id, "SELECT pais FROM self")
    assert "Unknown column" in str(e.value)  # the model can correct itself
    fake_engine.preview_response = (504, {"detail": "timeout"})
    out = await ai_agent.query_datasource(_ctx(session, user), visible.id, "SELECT paese FROM self")
    assert "error" in out and "504" in out["error"]
    outcomes = [r.outcome for r in session.exec(select(AuditLog).where(AuditLog.action == audit.AI_QUERY)).all()]
    assert outcomes == ["failure", "failure"]


def test_instructions_name_the_dialect_and_the_language(session, ai_on):
    _, user, _, _ = _catalog(session)
    text = ai_agent._instructions(_ctx(session, user, engine="clickhouse"))
    assert "ClickHouse SQL" in text and "Italian" in text and "capped at 7 rows" in text and "`self`" in text
    assert "Polars SQL" in ai_agent._instructions(_ctx(session, user))


# ── 4. models (admin panel) ──────────────────────────────────────────────────
@pytest.mark.anyio
async def test_no_model_is_usable_until_the_admin_enables_it(session, ai_on):
    admin, user, _, _ = _catalog(session)
    assert ai_routes.ai_status(user=user, session=session).model_dump() == {"enabled": True, "models": [], "default_model": None}
    listed = await ai_routes.list_ai_models(user=admin, session=session)
    assert [(m.model_id, m.enabled, m.chat) for m in listed] == [
        ("gpt-oss-120b", False, True), ("mistral-small-3.2-24b-instruct-2506", False, True),
        ("qwen3-embedding-8b", False, False), ("whisper-large-v3", False, False),
    ]
    await ai_routes.set_ai_model("gpt-oss-120b", ai_routes.AiModelUpdate(enabled=True), user=admin, session=session)
    st = ai_routes.ai_status(user=user, session=session)
    assert st.models == ["gpt-oss-120b"] and st.default_model == "gpt-oss-120b"
    await ai_routes.set_ai_model("gpt-oss-120b", ai_routes.AiModelUpdate(enabled=False), user=admin, session=session)
    assert ai_routes.ai_status(user=user, session=session).models == []
    actions = [r.action for r in session.exec(select(AuditLog)).all()]
    assert actions == [audit.AI_MODEL_ENABLE, audit.AI_MODEL_DISABLE]


@pytest.mark.anyio
async def test_embeddings_and_unknown_models_cannot_be_enabled(session, ai_on):
    admin, _, _, _ = _catalog(session)
    with pytest.raises(HTTPException) as e:
        await ai_routes.set_ai_model("qwen3-embedding-8b", ai_routes.AiModelUpdate(enabled=True), user=admin, session=session)
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:
        await ai_routes.set_ai_model("mai-visto", ai_routes.AiModelUpdate(enabled=True), user=admin, session=session)
    assert e.value.status_code == 404
    assert session.exec(select(AiModel)).all() == []


def test_status_says_off_when_not_configured(session):
    user = make_user(session)
    assert ai_routes.ai_status(user=user, session=session).enabled is False
    assert ai_models.is_chat_model("llama-3.3-70b-instruct") and not ai_models.is_chat_model("bge-multilingual-gemma2")


# ── 5. the chat, with a simulated model ──────────────────────────────────────
async def _scripted(messages, info):
    """First round: asks for the query. Second round: answers in two pieces."""
    last = messages[-1]
    if isinstance(last, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in last.parts):
        yield "In Italia "
        yield "sono 3 ordini."
    else:
        # the datasource's id is in the user's question: "(datasource N)"
        prompt = " ".join(str(getattr(p, "content", "")) for m in messages for p in m.parts)
        ds_id = int(prompt.split("(datasource ")[1].split(")")[0])
        yield {0: DeltaToolCall(name="query_datasource", json_args=json.dumps({"datasource_id": ds_id, "sql": "SELECT paese, count(*) AS n FROM self GROUP BY paese"}), tool_call_id="c1")}


async def _events(response):
    out = []
    async for chunk in response.body_iterator:
        for block in chunk.decode().strip().split("\n\n"):
            name = block.split("\n")[0].removeprefix("event: ")
            data = json.loads(block.split("\n", 1)[1].removeprefix("data: "))
            out.append((name, data))
    return out


@pytest.mark.anyio
async def test_chat_streams_text_tool_calls_the_result_table_and_the_history(session, ai_on, fake_engine, monkeypatch):
    admin, user, visible, _ = _catalog(session)
    await ai_routes.set_ai_model("gpt-oss-120b", ai_routes.AiModelUpdate(enabled=True), user=admin, session=session)
    monkeypatch.setattr(ai_routes, "_new_session", lambda: nullcontext(session))
    monkeypatch.setattr(ai_agent, "build_model", lambda model_id: FunctionModel(stream_function=_scripted))
    fake_engine.preview_response = (200, {"columns": [{"name": "paese", "dtype": "String"}, {"name": "n", "dtype": "Int64"}],
                                          "rows": [{"paese": "IT", "n": 3}], "row_count": 1, "truncated": False})
    # the visible datasource has an id the simulated model knows through the question
    body = ai_routes.ChatRequest(message=f"quanti ordini in Italia? (datasource {visible.id})", model="gpt-oss-120b", engine=None, locale="it")
    resp = await ai_routes.chat(body, request=None, user=user, session=session)
    events = await _events(resp)
    names = [n for n, _ in events]
    assert names == ["tool_call", "tool_result", "text", "text", "done"]
    assert events[0][1]["name"] == "query_datasource" and events[0][1]["args"]["datasource_id"] == visible.id
    table = events[1][1]["table"]
    assert events[1][1]["ok"] is True and table["rows"] == [{"paese": "IT", "n": 3}] and table["datasource"] == "ordini"
    assert "".join(d["delta"] for n, d in events if n == "text") == "In Italia sono 3 ordini."
    end = events[-1][1]
    assert end["usage"]["requests"] == 2
    # the conversation is SAVED on the server and the client receives only its id
    chat_id = end["chat_id"]
    assert chat_id and "history" not in end
    saved = ai_chats.detail(session, user, chat_id)
    assert saved["turns"] == 1
    turn = saved["messages"][0]
    assert turn["answer"] == "In Italia sono 3 ordini."
    assert [p["name"] for p in turn["steps"]] == ["query_datasource"]
    assert saved["title"].startswith("quanti ordini in Italia?")

    # next turn on the SAME chat: the server reads the history again
    again = ai_routes.ChatRequest(message=f"e in Francia? (datasource {visible.id})", model="gpt-oss-120b", chat_id=chat_id, locale="it")
    after = await _events(await ai_routes.chat(again, request=None, user=user, session=session))
    assert after[-1][1]["chat_id"] == chat_id
    assert ai_chats.detail(session, user, chat_id)["turns"] == 2  # it is appended, not overwritten


@pytest.mark.anyio
async def test_chat_refuses_disabled_models_disallowed_engines_and_bad_history(session, ai_on, monkeypatch, fake_engine):
    admin, user, _, _ = _catalog(session)
    with pytest.raises(HTTPException) as e:
        await ai_routes.chat(ai_routes.ChatRequest(message="ciao", model="gpt-oss-120b"), request=None, user=user, session=session)
    assert e.value.status_code == 403
    await ai_routes.set_ai_model("gpt-oss-120b", ai_routes.AiModelUpdate(enabled=True), user=admin, session=session)
    from app.models import DisabledEngine

    session.add(DisabledEngine(engine_id="duckdb")); session.commit()
    with pytest.raises(HTTPException) as e:
        await ai_routes.chat(ai_routes.ChatRequest(message="ciao", model="gpt-oss-120b", engine="duckdb"), request=None, user=user, session=session)
    assert e.value.status_code == 422 and "Available: " in e.value.detail and "duckdb" not in e.value.detail.split("Available: ")[1]
    # someone else's chat (or one that does not exist) is 404: no existence oracle
    others_ = AiChat(user_id=admin.id, title="roba dell'admin")
    session.add(others_); session.commit(); session.refresh(others_)
    for chat_id in (others_.id, 999999):
        with pytest.raises(HTTPException) as e:
            await ai_routes.chat(
                ai_routes.ChatRequest(message="ciao", model="gpt-oss-120b", chat_id=chat_id),
                request=None, user=user, session=session,
            )
        assert e.value.status_code == 404


@pytest.mark.anyio
async def test_chat_is_unavailable_when_ai_is_not_configured(session):
    user = make_user(session)
    with pytest.raises(HTTPException) as e:
        await ai_routes.chat(ai_routes.ChatRequest(message="ciao", model="x"), request=None, user=user, session=session)
    assert e.value.status_code == 503


def test_only_query_results_are_shown_as_tables():
    """describe_datasource has a `rows` field (the NUMBER of rows): it is not a table."""
    from pydantic_ai.messages import ToolReturnPart

    describe = ToolReturnPart(tool_name="describe_datasource", tool_call_id="a", content={"rows": 1204, "columns": [{"name": "x"}]})
    assert "table" not in ai_routes._tool_result_payload(describe)
    query = ToolReturnPart(tool_name="query_datasource", tool_call_id="b", content={"datasource": "o", "columns": [], "rows": [{"n": 1}], "row_count": 1, "truncated": False})
    assert ai_routes._tool_result_payload(query)["table"]["rows"] == [{"n": 1}]
    listed = ToolReturnPart(tool_name="list_datasources", tool_call_id="c", content=[{"id": 1}, {"id": 2}])
    assert ai_routes._tool_result_payload(listed)["count"] == 2


def test_focus_is_context_not_permission(session, ai_on):
    _, user, visible, hidden = _catalog(session)
    assert ai_agent.readable_focus(session, user, [visible.id, hidden.id, 9999]) == [{"id": visible.id, "name": "ordini"}]
    ctx = _ctx(session, user)
    ctx.deps.focus = ai_agent.readable_focus(session, user, [visible.id, hidden.id])
    text = ai_agent._instructions(ctx)
    assert f"ordini (id {visible.id})" in text and "stipendi" not in text


@pytest.mark.anyio
async def test_the_datasource_description_reaches_the_agent(session, ai_on, fake_engine):
    """Short in the list (to choose the table), whole in the detail."""
    _, user, visible, _ = _catalog(session)
    visible.description = "Ordini del gestionale. " + "Una riga per ordine evaso, IVA esclusa. " * 20
    session.add(visible); session.commit()
    listed = (await ai_agent.list_datasources(_ctx(session, user)))["datasources"][0]["description"]
    assert listed.startswith("Ordini del gestionale.") and listed.endswith("…") and len(listed) <= ai_agent.LIST_DESCRIPTION_CHARS + 1
    found = await ai_agent.list_datasources(_ctx(session, user), search="gestionale")  # the description is searched too
    assert found["datasources"] and "note" not in found
    full = (await ai_agent.describe_datasource(_ctx(session, user), visible.id))["description"]
    assert full == visible.description and len(full) > ai_agent.LIST_DESCRIPTION_CHARS
    assert "description written by its owners" in ai_agent._instructions(_ctx(session, user))


@pytest.mark.anyio
async def test_search_matches_words_not_exact_substrings(session, ai_on):
    """«anagrafica di test» must find `anagrafica_test`: a model that finds nothing tends to
    make things up, so the search is by words and, if there really is nothing, everything
    comes back with a note."""
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    p = make_project(session, name="Dati")
    make_datasource(session, name="anagrafica_test", project_id=p.id, key="datasets/a.parquet")
    make_datasource(session, name="Vendite-2024", project_id=p.id, key="datasets/v.parquet", description="Fatturato mensile")
    names = lambda r: [d["name"] for d in r["datasources"]]
    assert names(await ai_agent.list_datasources(_ctx(session, admin), search="Anagrafica di test")) == ["anagrafica_test"]
    assert names(await ai_agent.list_datasources(_ctx(session, admin), search="vendite 2024")) == ["Vendite-2024"]
    assert names(await ai_agent.list_datasources(_ctx(session, admin), search="fatturato")) == ["Vendite-2024"]
    nothing = await ai_agent.list_datasources(_ctx(session, admin), search="magazzino")
    assert names(nothing) == ["anagrafica_test", "Vendite-2024"] and "ALL the datasources" in nothing["note"]  # alphabetical order ignoring case
    assert "from its name alone" in ai_agent._instructions(_ctx(session, admin))


@pytest.mark.anyio
async def test_the_engine_is_chosen_among_the_available_and_allowed_ones(session, ai_on, fake_engine):
    """The engine is never forced: without a choice the first usable one is taken; a user's
    favourite may have been forbidden by the admin."""
    from app.models import DisabledEngine

    assert await ai_routes.pick_engine(session, None) == "polars"
    assert await ai_routes.pick_engine(session, "clickhouse") == "clickhouse"
    for e in ("polars", "chdb"):
        session.add(DisabledEngine(engine_id=e))
    session.commit()
    assert await ai_routes.pick_engine(session, None) == "duckdb"  # the first one left
    with pytest.raises(HTTPException) as err:
        await ai_routes.pick_engine(session, "chdb")
    assert err.value.status_code == 422 and "duckdb" in err.value.detail
    # available on the engine but NOT allowed, or allowed but not available: both out
    fake_engine.engines = [{"id": "duckdb", "label": "DuckDB", "available": False}, {"id": "clickhouse", "label": "CH", "available": True}]
    assert await ai_routes.pick_engine(session, None) == "clickhouse"


# ── join between two datasources ─────────────────────────────────────────────

@pytest.mark.anyio
async def test_a_join_resolves_the_second_datasource_under_the_same_permissions(session, ai_on, fake_engine):
    """The right side arrives as an ID and goes through the same permissions: the model never
    names a bucket nor a key."""
    _, user, visible, hidden = _catalog(session)
    other_one = make_datasource(session, name="clienti", project_id=visible.project_id, key="datasets/9/c.parquet")
    fake_engine.preview_response = (200, {"columns": [{"name": "n", "dtype": "Int64"}], "rows": [{"n": 7}],
                                          "row_count": 1, "truncated": False})

    out = await ai_agent.query_datasource(
        _ctx(session, user), visible.id, "SELECT count(*) AS n FROM self",
        join_datasource_id=other_one.id, join_left_on="cliente_id", join_right_on="id",
    )
    ops = fake_engine.previews[-1]["operations"]
    assert ops[0]["type"] == "join"
    assert ops[0]["params"]["right"]["source"]["key"] == "datasets/9/c.parquet"
    assert ops[0]["params"]["left_on"] == ["cliente_id"] and ops[0]["params"]["right_on"] == ["id"]
    assert ops[0]["params"]["how"] == "inner"
    assert ops[1]["type"] == "sql"  # the SQL runs AFTER, on `self` = the joined result
    assert out["datasource"] == "ordini ⋈ clienti"


@pytest.mark.anyio
async def test_you_cannot_join_a_datasource_you_cannot_read(session, ai_on, fake_engine):
    _, user, visible, hidden = _catalog(session)
    with pytest.raises(ModelRetry):
        await ai_agent.query_datasource(
            _ctx(session, user), visible.id, "SELECT 1 AS n FROM self",
            join_datasource_id=hidden.id, join_left_on="a", join_right_on="b",
        )
    assert fake_engine.previews == []  # the engine is not even called


@pytest.mark.anyio
async def test_mismatched_keys_are_refused_with_a_reason(session, ai_on, fake_engine):
    _, user, visible, _ = _catalog(session)
    other_one = make_datasource(session, name="clienti", project_id=visible.project_id)
    for left, right in (("a,b", "x"), ("", "x"), ("a", "")):
        with pytest.raises(ModelRetry):
            await ai_agent.query_datasource(
                _ctx(session, user), visible.id, "SELECT 1 AS n FROM self",
                join_datasource_id=other_one.id, join_left_on=left, join_right_on=right,
            )


@pytest.mark.anyio
async def test_without_a_join_nothing_changes(session, ai_on, fake_engine):
    _, user, visible, _ = _catalog(session)
    fake_engine.preview_response = (200, {"columns": [], "rows": [], "row_count": 0, "truncated": False})
    await ai_agent.query_datasource(_ctx(session, user), visible.id, "SELECT 1 AS n FROM self")
    assert [o["type"] for o in fake_engine.previews[-1]["operations"]] == ["sql"]


@pytest.mark.anyio
async def test_two_conversations_do_not_cancel_each_other(session, ai_on, fake_engine):
    """The slot is per CHAT: on a shared account — a public demo — one person's question must
    not interrupt another's answer."""
    _, user, visible, _ = _catalog(session)
    fake_engine.preview_response = (200, {"columns": [], "rows": [], "row_count": 0, "truncated": False})
    for chat_id in (7, 9):
        ctx = SimpleNamespace(deps=ai_agent.ChatDeps(
            user=user, session_factory=lambda: nullcontext(session), engine=None,
            model_id="m", locale="it", chat_id=chat_id,
        ))
        await ai_agent.query_datasource(ctx, visible.id, "SELECT 1 AS n FROM self")
    slot_a, slot_b = fake_engine.previews[-2]["slot"], fake_engine.previews[-1]["slot"]
    assert slot_a != slot_b
    assert slot_a.endswith(":c7:ai") and slot_b.endswith(":c9:ai")


# ── the chart under the answer ───────────────────────────────────────────────
COLUMNS = [{"name": n, "dtype": d} for n, d in
           (("mese", "Int32"), ("canale", "String"), ("ricavo", "Float64"), ("obiettivo", "Float64"))]


@pytest.mark.parametrize("chart, expected", [
    # the kinds of before keep the spec they had
    ({"type": "bar", "x": "canale", "y": "ricavo", "series": "mese"},
     {"type": "bar", "x": "canale", "y": "ricavo", "series": "mese"}),
    # one number: no categories, a target and the end of the scale
    ({"type": "gauge", "y": "ricavo", "y2": "obiettivo", "max": "100"},
     {"type": "gauge", "x": "", "y": "ricavo", "y2": "obiettivo", "max": 100.0}),
    ({"type": "kpi", "x": "mese", "y": "ricavo"}, {"type": "kpi", "x": "mese", "y": "ricavo"}),
    ({"type": "kpi", "y": "ricavo", "y2": "obiettivo"}, {"type": "kpi", "x": "", "y": "ricavo", "y2": "obiettivo"}),
    ({"type": "combo", "x": "mese", "y": "ricavo", "y2": "obiettivo"},
     {"type": "combo", "x": "mese", "y": "ricavo", "y2": "obiettivo"}),
    ({"type": "bullet", "x": "canale", "y": "ricavo", "y2": "obiettivo", "max": 5e6},
     {"type": "bullet", "x": "canale", "y": "ricavo", "y2": "obiettivo", "max": 5e6}),
    ({"type": "heatmap", "x": "mese", "y": "ricavo", "series": "canale"},
     {"type": "heatmap", "x": "mese", "y": "ricavo", "series": "canale"}),
    # what a kind does not read is dropped, not refused
    ({"type": "funnel", "x": "canale", "y": "ricavo", "y2": "obiettivo", "series": "mese", "max": 9},
     {"type": "funnel", "x": "canale", "y": "ricavo"}),
    ({"type": "gauge", "y": "ricavo", "max": float("inf")}, {"type": "gauge", "x": "", "y": "ricavo"}),
    ({"type": "Waterfall", "x": "mese", "y": "ricavo", "title": "t" * 300},
     {"type": "waterfall", "x": "mese", "y": "ricavo", "title": "t" * 120}),
    ({"type": "treemap", "x": "canale", "y": "ricavo"}, {"type": "treemap", "x": "canale", "y": "ricavo"}),
])
def test_every_chart_kind_keeps_what_it_reads(chart, expected):
    spec, reason = ai_agent._chart_spec(chart, COLUMNS)
    assert reason is None and spec == expected


@pytest.mark.parametrize("chart, says", [
    ({"type": "combo", "x": "mese", "y": "ricavo"}, "chart_y2"),
    ({"type": "bullet", "x": "canale", "y": "ricavo"}, "chart_y2"),
    ({"type": "heatmap", "x": "mese", "y": "ricavo"}, "chart_series"),
    ({"type": "gauge", "y": "ricavo", "y2": "budget"}, "budget"),
    ({"type": "kpi", "x": "mese"}, "(none given)"),
    ({"type": "funnel", "y": "ricavo"}, "(none given)"),  # only a gauge and a KPI go without categories
    ({"type": "gauge", "y": "ricavo", "max": "lots"}, "chart_max"),
    ({"type": "radar", "x": "mese", "y": "ricavo"}, "bullet"),  # the reason lists the kinds there are
])
def test_a_chart_missing_what_it_needs_is_refused_with_the_reason(chart, says):
    spec, reason = ai_agent._chart_spec(chart, COLUMNS)
    assert spec is None and says in reason


def _enum(prop: dict) -> list:
    """The values of an optional Literal in a JSON schema."""
    return next(p["enum"] for p in prop.get("anyOf", [prop]) if "enum" in p)


@pytest.mark.anyio
async def test_the_model_is_offered_every_kind_and_a_gauge_reaches_the_page(session, ai_on, fake_engine, monkeypatch):
    admin, user, visible, _ = _catalog(session)
    await ai_routes.set_ai_model("gpt-oss-120b", ai_routes.AiModelUpdate(enabled=True), user=admin, session=session)
    monkeypatch.setattr(ai_routes, "_new_session", lambda: nullcontext(session))
    offered: dict = {}

    async def model(messages, info):
        last = messages[-1]
        if isinstance(last, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in last.parts):
            yield "Siamo all'87,5%."
            return
        tool = next(t for t in info.function_tools if t.name == "query_datasource")
        offered.update(tool.parameters_json_schema["properties"])
        yield {0: DeltaToolCall(name="query_datasource", tool_call_id="g1", json_args=json.dumps({
            "datasource_id": visible.id, "sql": "SELECT sum(imp) AS ricavo, 100 AS obiettivo FROM self",
            "chart_type": "gauge", "chart_y": "ricavo", "chart_y2": "obiettivo", "chart_max": 150,
        }))}

    monkeypatch.setattr(ai_agent, "build_model", lambda model_id: FunctionModel(stream_function=model))
    fake_engine.preview_response = (200, {"columns": [{"name": "ricavo", "dtype": "Float64"}, {"name": "obiettivo", "dtype": "Int32"}],
                                          "rows": [{"ricavo": 87.5, "obiettivo": 100}], "row_count": 1, "truncated": False})
    body = ai_routes.ChatRequest(message="a che punto siamo con l'obiettivo?", model="gpt-oss-120b", engine=None, locale="it")
    events = await _events(await ai_routes.chat(body, request=None, user=user, session=session))

    assert _enum(offered["chart_type"]) == list(ai_agent.CHART_TYPES)
    assert {"chart_y2", "chart_max"} <= set(offered)
    result = next(d for n, d in events if n == "tool_result")
    assert result["ok"] is True
    assert result["chart"] == {"type": "gauge", "x": "", "y": "ricavo", "y2": "obiettivo", "max": 150.0}


def test_the_assistant_page_agent_offers_the_same_kinds():
    from app.services.agent import read_tools

    props = read_tools.QueryDatasourceArgs.model_json_schema()["properties"]
    assert _enum(props["chart_type"]) == list(ai_agent.CHART_TYPES)
    assert {"chart_y2", "chart_max"} <= set(props)
