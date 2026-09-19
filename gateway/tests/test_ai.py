"""Assistente AI: i contratti che contano.

1. L'assistente vede e interroga SOLO le datasource che l'utente puo' leggere
   (VIEW sulla cartella): quelle fuori dai permessi non compaiono nell'elenco e
   danno lo stesso errore di una datasource inesistente.
2. `query_datasource` manda all'engine un nodo `sql` sulla datasource giusta,
   con righe limitate, senza step-cache e nello slot dell'utente; accetta solo
   una SELECT; ogni query (anche fallita) lascia un evento di audit.
3. Le descrizioni dei campi arrivano al modello insieme alle colonne.
4. I modelli li abilita l'amministratore: nessuno e' usabile di default, gli
   embedding non sono abilitabili, ogni cambio finisce nell'audit.
5. La chat trasmette testo, chiamate agli strumenti, la TABELLA del risultato
   e la storia da rimandare al turno dopo — con un modello simulato.
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
    """Due cartelle: l'utente legge solo la prima."""
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


# ── 1. permessi ──────────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_the_assistant_lists_only_what_the_user_can_read(session, ai_on):
    admin, user, visible, hidden = _catalog(session)
    got = (await ai_agent.list_datasources(_ctx(session, user)))["datasources"]
    assert [d["name"] for d in got] == ["ordini"]
    assert got[0] == {"id": visible.id, "name": "ordini", "folder": "Vendite", "rows": 1204, "columns": 2, "description": "Ordini 2024"}
    assert {d["name"] for d in (await ai_agent.list_datasources(_ctx(session, admin)))["datasources"]} == {"ordini", "stipendi"}
    # cercare cio' che non si puo' leggere non lo rivela: torna solo cio' che e' leggibile, con una nota
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
    assert fake_engine.previews == []  # l'engine non viene nemmeno chiamato


# ── 2. descrizioni e query ───────────────────────────────────────────────────
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
    assert sent["limit"] == 7  # tetto AI__MAX_RESULT_ROWS
    assert sent["no_cache"] is True and sent["slot"] == f"u{user.id}:ai" and sent["engine"] == "clickhouse"
    assert sent["sort_keys"] == ["paese"]
    # l'engine ClickHouse le esegue con l'utenza dedicata di sola lettura, se c'è
    assert sent["principal"] == "ai"
    assert out["datasource"] == "ordini" and out["row_count"] == 2 and out["rows"][0] == {"paese": "IT", "n": 3}
    assert len(out["rows"][1]["paese"]) == ai_agent.MAX_CELL_CHARS + 1  # celle lunghe tagliate
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
    assert "Unknown column" in str(e.value)  # il modello puo' correggersi
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


# ── 4. modelli (pannello admin) ──────────────────────────────────────────────
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


# ── 5. la chat, con un modello simulato ──────────────────────────────────────
async def _scripted(messages, info):
    """Primo giro: chiede la query. Secondo giro: risponde in due pezzi."""
    last = messages[-1]
    if isinstance(last, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in last.parts):
        yield "In Italia "
        yield "sono 3 ordini."
    else:
        # l'id della datasource sta nella domanda dell'utente: "(datasource N)"
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
    # la datasource visibile ha id noto al modello simulato tramite la domanda
    body = ai_routes.ChatRequest(message=f"quanti ordini in Italia? (datasource {visible.id})", model="gpt-oss-120b", engine=None, locale="it")
    resp = await ai_routes.chat(body, request=None, user=user, session=session)
    events = await _events(resp)
    names = [n for n, _ in events]
    assert names == ["tool_call", "tool_result", "text", "text", "done"]
    assert events[0][1]["name"] == "query_datasource" and events[0][1]["args"]["datasource_id"] == visible.id
    table = events[1][1]["table"]
    assert events[1][1]["ok"] is True and table["rows"] == [{"paese": "IT", "n": 3}] and table["datasource"] == "ordini"
    assert "".join(d["delta"] for n, d in events if n == "text") == "In Italia sono 3 ordini."
    fine = events[-1][1]
    assert fine["usage"]["requests"] == 2
    # la conversazione e' SALVATA sul server e il client riceve solo il suo id
    chat_id = fine["chat_id"]
    assert chat_id and "history" not in fine
    salvata = ai_chats.dettaglio(session, user, chat_id)
    assert salvata["turns"] == 1
    turno = salvata["messages"][0]
    assert turno["answer"] == "In Italia sono 3 ordini."
    assert [p["name"] for p in turno["steps"]] == ["query_datasource"]
    assert salvata["title"].startswith("quanti ordini in Italia?")

    # turno successivo sulla STESSA chat: la storia la rilegge il server
    again = ai_routes.ChatRequest(message=f"e in Francia? (datasource {visible.id})", model="gpt-oss-120b", chat_id=chat_id, locale="it")
    dopo = await _events(await ai_routes.chat(again, request=None, user=user, session=session))
    assert dopo[-1][1]["chat_id"] == chat_id
    assert ai_chats.dettaglio(session, user, chat_id)["turns"] == 2  # si accoda, non si sovrascrive


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
    assert e.value.status_code == 422 and "Disponibili: " in e.value.detail and "duckdb" not in e.value.detail.split("Disponibili: ")[1]
    # la chat di un altro (o inesistente) e' 404: nessun oracolo di esistenza
    altrui = AiChat(user_id=admin.id, title="roba dell'admin")
    session.add(altrui); session.commit(); session.refresh(altrui)
    for chat_id in (altrui.id, 999999):
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
    """describe_datasource ha un campo `rows` (il NUMERO di righe): non e' una tabella."""
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
    """Breve nell'elenco (per scegliere la tabella), intera nel dettaglio."""
    _, user, visible, _ = _catalog(session)
    visible.description = "Ordini del gestionale. " + "Una riga per ordine evaso, IVA esclusa. " * 20
    session.add(visible); session.commit()
    listed = (await ai_agent.list_datasources(_ctx(session, user)))["datasources"][0]["description"]
    assert listed.startswith("Ordini del gestionale.") and listed.endswith("…") and len(listed) <= ai_agent.LIST_DESCRIPTION_CHARS + 1
    found = await ai_agent.list_datasources(_ctx(session, user), search="gestionale")  # si cerca anche nella descrizione
    assert found["datasources"] and "note" not in found
    full = (await ai_agent.describe_datasource(_ctx(session, user), visible.id))["description"]
    assert full == visible.description and len(full) > ai_agent.LIST_DESCRIPTION_CHARS
    assert "description written by its owners" in ai_agent._instructions(_ctx(session, user))


@pytest.mark.anyio
async def test_search_matches_words_not_exact_substrings(session, ai_on):
    """«anagrafica di test» deve trovare `anagrafica_test`: un modello che non
    trova nulla tende a inventare, quindi la ricerca e' per parole e, se proprio
    non c'e' nulla, torna tutto con una nota."""
    admin = make_user(session, email="admin@x.local", is_superuser=True)
    p = make_project(session, name="Dati")
    make_datasource(session, name="anagrafica_test", project_id=p.id, key="datasets/a.parquet")
    make_datasource(session, name="Vendite-2024", project_id=p.id, key="datasets/v.parquet", description="Fatturato mensile")
    names = lambda r: [d["name"] for d in r["datasources"]]
    assert names(await ai_agent.list_datasources(_ctx(session, admin), search="Anagrafica di test")) == ["anagrafica_test"]
    assert names(await ai_agent.list_datasources(_ctx(session, admin), search="vendite 2024")) == ["Vendite-2024"]
    assert names(await ai_agent.list_datasources(_ctx(session, admin), search="fatturato")) == ["Vendite-2024"]
    nothing = await ai_agent.list_datasources(_ctx(session, admin), search="magazzino")
    assert names(nothing) == ["anagrafica_test", "Vendite-2024"] and "ALL the datasources" in nothing["note"]  # ordine alfabetico senza maiuscole
    assert "from its name alone" in ai_agent._instructions(_ctx(session, admin))


@pytest.mark.anyio
async def test_the_engine_is_chosen_among_the_available_and_allowed_ones(session, ai_on, fake_engine):
    """Il motore non e' mai imposto: senza una scelta si prende il primo
    utilizzabile; il preferito di un utente puo' essere stato vietato dall'admin."""
    from app.models import DisabledEngine

    assert await ai_routes.pick_engine(session, None) == "polars"
    assert await ai_routes.pick_engine(session, "clickhouse") == "clickhouse"
    for e in ("polars", "chdb"):
        session.add(DisabledEngine(engine_id=e))
    session.commit()
    assert await ai_routes.pick_engine(session, None) == "duckdb"  # il primo rimasto
    with pytest.raises(HTTPException) as err:
        await ai_routes.pick_engine(session, "chdb")
    assert err.value.status_code == 422 and "duckdb" in err.value.detail
    # disponibile sull'engine ma NON consentito, o consentito ma non disponibile: fuori entrambi
    fake_engine.engines = [{"id": "duckdb", "label": "DuckDB", "available": False}, {"id": "clickhouse", "label": "CH", "available": True}]
    assert await ai_routes.pick_engine(session, None) == "clickhouse"
