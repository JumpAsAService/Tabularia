"""The AI path of the dbt export. What the AI writes is kept per flow version, so a
re-export of the same version makes no call and gives the same zip: with keys longer
than their column, with answers that leave items out, with two exports at once, after
a model switch. Every AI call answers within the export's budget, and what it cost
reaches the spending ledger even when it fails. A query too long for the AI is not sent
cut, and the text stored is text YAML can carry."""
import asyncio
import json
import re
import time
from types import SimpleNamespace

import pytest
from sqlmodel import Session, SQLModel, create_engine, select

from app.models import AiSpend, AuditLog, DbtAiText, Flow, FlowVersion, User
from app.services import dbt_ai
from app.services import dbt_export as d
from tests.test_dbt_export import _flusso_a_due_uscite, _richiesta, scena  # noqa: F401 (scena is a fixture)


@pytest.fixture
def ai(monkeypatch):
    """A fake provider. `describe(item, call)` writes each description (None leaves the
    item out of the answer); `sql` answers a translation; `delay` makes each call slow;
    `hang` never answers; `broken` answers without `items`, so the output fails
    validation; `error` fails the call; `usage` = (input, output) tokens per request;
    `on_call(item_keys)` runs inside the call (another gateway at work). Counts calls."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai.usage import RequestUsage

    from app.services import ai_agent

    stato = SimpleNamespace(calls=0, prompts=[], model="finto-1", sql="SELECT id, canale FROM __s_0",
                            describe=lambda voce, n: f"About {voce.get('column') or voce.get('name')} call {n}",
                            delay=0.0, hang=False, broken=False, error=None, usage=None, on_call=None)

    async def rispondi(messages, info):
        stato.calls += 1
        n = stato.calls
        testo = next(p.content for m in messages for p in getattr(m, "parts", []) if type(p).__name__ == "UserPromptPart")
        stato.prompts.append(testo)
        if stato.hang:
            await asyncio.sleep(3600)
        if stato.delay:
            await asyncio.sleep(stato.delay)
        if stato.error:
            raise RuntimeError(stato.error)
        uso = RequestUsage(input_tokens=stato.usage[0], output_tokens=stato.usage[1]) if stato.usage else RequestUsage()
        strumento = info.output_tools[0].name
        if stato.broken:
            return ModelResponse(parts=[ToolCallPart(tool_name=strumento, args={"wrong": 1})], usage=uso)
        if testo.startswith("{"):
            voci = json.loads(testo)["items"]
            if stato.on_call:
                stato.on_call([v["key"] for v in voci])
            risposta = [{"key": v["key"], "description": t} for v in voci if (t := stato.describe(v, n)) is not None]
            return ModelResponse(parts=[ToolCallPart(tool_name=strumento, args={"items": risposta})], usage=uso)
        return ModelResponse(parts=[ToolCallPart(tool_name=strumento, args={"sql": stato.sql})], usage=uso)

    async def campioni(bucket, key):
        return {}

    monkeypatch.setattr(ai_agent, "build_model", lambda model_id: FunctionModel(rispondi))
    monkeypatch.setattr(dbt_ai, "modello_ai", lambda session: stato.model)
    monkeypatch.setattr(dbt_ai, "_campioni", campioni)
    return stato


def _ai_settings(monkeypatch, **valori):
    """AI settings from the environment, as a deployment sets them."""
    from app.core.config import get_settings

    for nome, valore in valori.items():
        monkeypatch.setenv(f"AI__{nome.upper()}", str(valore))
    get_settings.cache_clear()


def _versione(session, flow, n=1):
    session.add(FlowVersion(flow_id=flow.id, version=n, definition=flow.definition))
    session.commit()


async def _descrivi(session, user, flow, **kw):
    payload, progetto = d._costruisci(session, flow, "data-prep", "duckdb", None, d.OpzioniDbt(ai_descriptions=True))
    await asyncio.wait_for(dbt_ai.descrivi(session, user, flow, sorted(progetto.flussi_visti), payload, **kw), 20)
    return payload


def _colonne(payload):
    return {c["name"]: c.get("description") or "" for c in payload["sources"][0]["tables"][0]["columns"]}


def _rifiuto_traduzione(sql, detail="boh", code="translation_failed"):
    return (422, {"detail": {"code": code, "message": "x", "params": {
        "dialect": "clickhouse", "detail": detail, "model": "margin", "sql": sql,
        "source_dialect": "duckdb", "target_dialect": "clickhouse", "columns": ["id", "canale"]}}})


# ── #54: a key longer than the column that stores it ──────────────────────────
@pytest.mark.anyio
async def test_a_column_name_over_500_characters_is_remembered_and_found_again(session, scena, ai):
    """Survey exports name a column after the whole question, and a nested Struct's
    type runs to hundreds of characters: the key is longer than its column. It was
    stored cut, never found again (a paid call on every export), and the second insert
    broke the unique key, a 500 instead of the zip. Two questions that differ only at
    the end collided on the very first export."""
    domanda = "How satisfied are you with " + "the service you received today " * 18
    assert len(domanda) > 500
    colonne = json.loads(scena.ordini.columns) + [{"name": domanda + "[Price]", "dtype": "Int64"},
                                                    {"name": domanda + "[Quality]", "dtype": "Int64"}]
    scena.ordini.columns = json.dumps(colonne)
    session.add(scena.ordini)
    session.commit()
    ai.describe = lambda v, n: f"Answer about {(v.get('column') or v.get('name'))[-9:]}"   # the end tells the two apart
    flow = _flusso_a_due_uscite(session, scena)
    _versione(session, flow)

    p1 = await _descrivi(session, scena.capo, flow)
    c = _colonne(p1)
    assert c[domanda + "[Price]"] == "(AI) Answer about y [Price]"
    assert c[domanda + "[Quality]"] == "(AI) Answer about [Quality]"
    assert ai.calls == 1
    assert all(len(r.key) <= 500 for r in session.exec(select(DbtAiText)).all())

    p2 = await _descrivi(session, scena.capo, flow)
    assert ai.calls == 1                                 # found again: no new call
    assert p2["sources"] == p1["sources"] and p2["ai"] == p1["ai"]


# ── #55: an answer that leaves items out ──────────────────────────────────────
@pytest.mark.anyio
async def test_items_the_ai_leaves_out_are_asked_once_more_then_kept_as_without_description(session, scena, ai):
    """Models drop items from long lists, or answer some with nothing. Those items were
    asked again at every export of the version: a paid call each time, a different zip,
    and a refusal once the provider was down. Now they are asked once more in the same
    export, and what is still missing is remembered as «no description»."""
    def descrivi(voce, n):
        if voce.get("kind") == "flow":
            return "What the flow does"
        if voce["column"] == "importo":
            return '  ""  '                                # an empty answer, every time
        return None if n == 1 else f"About {voce['column']}"   # left out the first time

    ai.describe = descrivi
    flow = _flusso_a_due_uscite(session, scena)
    _versione(session, flow)

    p1 = await _descrivi(session, scena.capo, flow)
    assert ai.calls == 2
    secondo = [v["key"] for v in json.loads(ai.prompts[1])["items"]]
    assert secondo == [f"col:ds:{scena.ordini.id}:id:", f"col:ds:{scena.ordini.id}:importo:"]   # only the missing ones
    assert _colonne(p1) == {"id": "(AI) About id", "canale": "Sales channel", "importo": ""}

    p2 = await _descrivi(session, scena.capo, flow)
    assert ai.calls == 2 and p2 == p1                       # the same version: no call, the same payload
    ai.error = "503 upstream"
    p3 = await _descrivi(session, scena.capo, flow)          # the provider is down: nothing to ask
    assert ai.calls == 2 and p3 == p1


@pytest.mark.anyio
async def test_when_the_second_ask_fails_the_first_answer_is_kept(session, scena, ai):
    """The first answer was paid for: a failure on the second ask stops the export, but
    the next export asks only what is still missing."""
    ai.describe = lambda v, n: None if v.get("column") == "id" else "Described"
    flow = _flusso_a_due_uscite(session, scena)
    _versione(session, flow)
    ai.on_call = lambda chiavi: setattr(ai, "error", "503 upstream") if ai.calls == 1 else None
    with pytest.raises(d.DbtExportError) as e:
        await _descrivi(session, scena.capo, flow)
    assert e.value.code == "ai_failed" and ai.calls == 2
    ai.error, ai.on_call = None, None
    ai.describe = lambda v, n: "Described"
    p = await _descrivi(session, scena.capo, flow)
    assert ai.calls == 3 and [v["key"] for v in json.loads(ai.prompts[-1])["items"]] == [f"col:ds:{scena.ordini.id}:id:"]
    assert _colonne(p)["id"] == "(AI) Described"


# ── #57: a provider that does not answer ──────────────────────────────────────
@pytest.mark.anyio
async def test_a_provider_that_does_not_answer_stops_the_export_within_its_budget(session, scena, ai, monkeypatch):
    """A provider that accepts the connection and never answers held the download for
    up to 3 × 600 s, past the ingress' 300 s: the user got a generic 504 instead of
    «the AI did not answer»."""
    _ai_settings(monkeypatch, dbt_export_timeout_seconds=0.5)
    ai.hang = True
    flow = _flusso_a_due_uscite(session, scena)
    inizio = time.monotonic()
    with pytest.raises(d.DbtExportError) as e:
        await _descrivi(session, scena.capo, flow)
    assert time.monotonic() - inizio < 5
    assert e.value.code == "ai_failed" and "0.5 s" in e.value.params["detail"]


def test_the_default_ai_budget_ends_before_the_ingress_cuts_the_request():
    from app.core.config import AiSettings

    # infrastructure/helm/tabularia/values.yaml: proxy-read-timeout "300"; the engine needs time too
    assert AiSettings().dbt_export_timeout_seconds <= 200


@pytest.mark.anyio
async def test_the_budget_is_for_the_whole_export_not_for_each_call(session, scena, ai, fake_engine, monkeypatch):
    """Descriptions and translations are several calls in one request: each one under
    the budget can still add up past it."""
    from fastapi import HTTPException

    from app.routes import flows as flows_routes

    _ai_settings(monkeypatch, dbt_export_timeout_seconds=1.0)
    ai.delay = 0.6
    flow = _flusso_a_due_uscite(session, scena)
    fake_engine.dbt_export_script = [_rifiuto_traduzione("SELECT id, canale FROM __s_0 QUALIFY strano"), None]
    opz = d.OpzioniDbt(target="clickhouse", ai_descriptions=True, ai_translations=True)
    with pytest.raises(HTTPException) as e:
        await asyncio.wait_for(flows_routes.export_flow_dbt_con_opzioni(flow.id, opz, _richiesta(), scena.capo, session), 20)
    assert e.value.status_code == 422 and "did not answer" in e.value.detail and ai.calls == 2


# ── #56: what a failed call cost ──────────────────────────────────────────────
@pytest.mark.anyio
async def test_a_failed_ai_call_still_reaches_the_spending_ledger(session, scena, ai):
    """An answer that fails validation is retried by pydantic-ai: two billed requests,
    and nothing in ai_spend, so the daily caps never saw them."""
    ai.broken, ai.usage = True, (50_000, 4_000)
    flow = _flusso_a_due_uscite(session, scena)
    with pytest.raises(d.DbtExportError) as e:
        await _descrivi(session, scena.capo, flow)
    assert e.value.code == "ai_failed" and ai.calls == 2
    righe = session.exec(select(AiSpend)).all()
    assert sum(r.input_tokens for r in righe) == 100_000 and sum(r.output_tokens for r in righe) == 8_000

    prima = len(righe)
    with pytest.raises(d.DbtExportError):
        await dbt_ai.traduci(session, scena.capo, flow, _rifiuto_traduzione("SELECT 1")[1]["detail"]["params"])
    assert ai.calls == 4 and sum(r.input_tokens for r in session.exec(select(AiSpend)).all()[prima:]) == 100_000


@pytest.mark.anyio
async def test_the_export_s_ai_calls_have_the_assistant_s_per_turn_limits(session, scena, ai, monkeypatch):
    """The assistant stops a turn over its token or cost limit (audit A8); the export
    sent up to 400 columns in one call with only a limit on the number of requests."""
    _ai_settings(monkeypatch, max_input_tokens_per_turn=1000)
    ai.usage = (5_000, 10)
    flow = _flusso_a_due_uscite(session, scena)
    with pytest.raises(d.DbtExportError) as e:
        await _descrivi(session, scena.capo, flow)
    assert e.value.code == "ai_failed" and "input_tokens_limit" in e.value.params["detail"]
    assert sum(r.input_tokens for r in session.exec(select(AiSpend)).all()) == 5_000


# ── #58 #64: a query too long for the AI ──────────────────────────────────────
@pytest.mark.anyio
async def test_a_query_too_long_for_the_ai_is_refused_not_sent_cut(session, scena, ai, fake_engine):
    """The AI got the first 20,000 characters and nothing said the rest was missing: a
    proposal without the hidden steps passed the engine's checks, which compare columns,
    not steps. A proposal kept from such a cut prompt is not reused either."""
    from fastapi import HTTPException

    from app.routes import flows as flows_routes

    flow = _flusso_a_due_uscite(session, scena)
    _versione(session, flow)
    colonne = ",\n".join(f'    "attribute_{i:04d}"' for i in range(1000))
    lunga = f"SELECT\n{colonne}\nFROM __s_0\nWHERE secret_filter = 1"
    assert len(lunga) > 20_000
    session.add(DbtAiText(flow_id=flow.id, flow_version=1, kind="translate", key=dbt_ai.chiave_traduzione(lunga, "duckdb", "clickhouse"),
                          text="SELECT id, canale FROM __s_0", model_id="finto-1"))
    session.commit()
    fake_engine.dbt_export_script = [_rifiuto_traduzione(lunga), None]
    opz = d.OpzioniDbt(target="clickhouse", ai_translations=True)
    with pytest.raises(HTTPException) as e:
        await flows_routes.export_flow_dbt_con_opzioni(flow.id, opz, _richiesta(), scena.capo, session)
    assert ai.calls == 0 and len(fake_engine.dbt_exports) == 1
    assert e.value.status_code == 422
    assert "«margin»" in e.value.detail and f"{len(lunga):,}" in e.value.detail and "20,000" in e.value.detail

    # under the limit the AI gets the whole query, its last step included
    corta = "SELECT\n" + ",\n".join(f'    "attribute_{i:04d}"' for i in range(880)) + "\nFROM __s_0\nWHERE secret_filter = 1"
    assert 19_000 < len(corta) <= 20_000
    fake_engine.dbt_export_script = [_rifiuto_traduzione(corta), None]
    risposta = await flows_routes.export_flow_dbt_con_opzioni(flow.id, opz, _richiesta(), scena.capo, session)
    assert risposta.status_code == 200 and ai.calls == 1 and ai.prompts[0] == corta


@pytest.mark.anyio
@pytest.mark.parametrize("code", ["translation_failed", "query_not_translatable"])
async def test_a_model_over_clickhouse_max_query_size_is_not_sent_to_the_ai(session, scena, ai, fake_engine, code):
    """A model over ClickHouse's max_query_size came back as a translation failure, so
    with the AI option on the gateway paid for calls that could never pass the same
    size check. The refusal keeps the engine's reason."""
    from fastapi import HTTPException

    from app.routes import flows as flows_routes

    flow = _flusso_a_due_uscite(session, scena)
    enorme = "SELECT " + ", ".join(f"c{i}" for i in range(60_000)) + " FROM __s_0"
    assert len(enorme) > 262_144
    motivo = "Max query size exceeded (can be increased with the `max_query_size` setting): Syntax error: failed at position 262145"
    rifiuto = _rifiuto_traduzione(enorme, detail=motivo, code=code)
    rifiuto[1]["detail"]["params"]["datasource"] = "orders query"
    fake_engine.dbt_export_script = [rifiuto, None]
    with pytest.raises(HTTPException) as e:
        await flows_routes.export_flow_dbt_con_opzioni(flow.id, d.OpzioniDbt(target="clickhouse", ai_translations=True),
                                                       _richiesta(), scena.capo, session)
    assert ai.calls == 0 and e.value.status_code == 422 and "Max query size exceeded" in e.value.detail


# ── #52: two exports of the same version at once ──────────────────────────────
class TestOverlappingExports:
    """Two admins, or two tabs, export the same new version with AI descriptions. Both
    found nothing stored, both paid the provider, and the second zip carried texts that
    no later re-export gives back. Each export has its own session on a database file,
    as two requests do."""

    @pytest.fixture
    def db_engine(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'gateway.db'}", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(engine)
        yield engine
        engine.dispose()

    @pytest.mark.anyio
    async def test_overlapping_exports_ask_the_ai_once_and_give_the_same_texts(self, db_engine, session, scena, ai):
        flow = _flusso_a_due_uscite(session, scena)
        _versione(session, flow)
        ai.delay = 0.3
        with Session(db_engine) as altra:
            p1, p2 = await asyncio.gather(
                _descrivi(session, scena.capo, flow),
                _descrivi(altra, altra.get(User, scena.capo.id), altra.get(Flow, flow.id)))
        assert ai.calls == 1
        assert p1 == p2 and _colonne(p1)["id"] == "(AI) About id call 1"
        assert await _descrivi(session, scena.capo, flow) == p1 and ai.calls == 1
        assert session.exec(select(DbtAiText).where(DbtAiText.kind == "claim")).all() == []   # nothing left held

    @pytest.mark.anyio
    async def test_texts_another_gateway_stored_first_win(self, db_engine, session, scena, ai):
        """Another replica (an older one during a rolling update) stores its texts while
        this export waits for the AI: the insert must not fail and the zip must carry
        what is stored, the texts every later re-export gives."""
        flow = _flusso_a_due_uscite(session, scena)
        _versione(session, flow)

        def l_altro_gateway(chiavi):
            with Session(db_engine) as altro:
                for k in chiavi:
                    altro.add(DbtAiText(flow_id=flow.id, flow_version=1, kind="describe", key=k, text="From the other gateway", model_id="m-2"))
                altro.commit()

        ai.on_call = l_altro_gateway
        p1 = await _descrivi(session, scena.capo, flow)
        assert _colonne(p1)["id"] == "(AI) From the other gateway" and p1["ai"]["model"] == "m-2"
        ai.on_call = None
        assert await _descrivi(session, scena.capo, flow) == p1 and ai.calls == 1


@pytest.mark.anyio
async def test_a_claim_left_by_an_export_that_died_expires(session, scena, ai):
    """A gateway that dies while asking the AI leaves its claim: the version is not
    blocked past that export's own deadline."""
    flow = _flusso_a_due_uscite(session, scena)
    _versione(session, flow)
    session.add(DbtAiText(flow_id=flow.id, flow_version=1, kind="claim", key="describe", text=f"{time.time() - 1:.3f}"))
    session.commit()
    p = await _descrivi(session, scena.capo, flow)
    assert ai.calls == 1 and _colonne(p)["id"] == "(AI) About id call 1"


# ── #59: the model the README names ───────────────────────────────────────────
@pytest.mark.anyio
async def test_after_a_model_switch_the_readme_names_the_model_that_wrote_the_texts(session, scena, ai):
    """The texts are kept per version; the README named the model configured now, so the
    zip changed and named a model that wrote nothing. With every text stored, a re-export
    needs no AI at all."""
    flow = _flusso_a_due_uscite(session, scena)
    _versione(session, flow)
    p1 = await _descrivi(session, scena.capo, flow)
    assert p1["ai"]["model"] == "finto-1"
    ai.model = "other-model-2"
    assert await _descrivi(session, scena.capo, flow) == p1 and ai.calls == 1
    ai.model = None                                       # the AI switched off since
    assert await _descrivi(session, scena.capo, flow) == p1


@pytest.mark.anyio
async def test_texts_of_two_models_name_both(session, scena, ai):
    ai.describe = lambda v, n: None if v.get("column") == "id" else "Described"
    ai.on_call = lambda chiavi: setattr(ai, "error", "503 upstream") if ai.calls == 1 else None
    flow = _flusso_a_due_uscite(session, scena)
    _versione(session, flow)
    with pytest.raises(d.DbtExportError):
        await _descrivi(session, scena.capo, flow)
    ai.error, ai.on_call, ai.model = None, None, "other-model-2"
    ai.describe = lambda v, n: "Described"
    p = await _descrivi(session, scena.capo, flow)
    assert p["ai"]["model"] == "finto-1, other-model-2"


@pytest.mark.anyio
async def test_the_audit_names_the_model_that_wrote_the_translation(session, scena, ai, fake_engine):
    from app.routes import flows as flows_routes

    flow = _flusso_a_due_uscite(session, scena)
    _versione(session, flow)
    opz = d.OpzioniDbt(target="clickhouse", ai_translations=True)
    for modello in ("finto-1", "other-model-2"):
        ai.model = modello
        fake_engine.dbt_export_script = [_rifiuto_traduzione("SELECT id, canale FROM __s_0 QUALIFY strano"), None]
        await flows_routes.export_flow_dbt_con_opzioni(flow.id, opz, _richiesta(), scena.capo, session)
    assert ai.calls == 1
    dettagli = [json.loads(r.detail)["ai"] for r in session.exec(select(AuditLog).where(AuditLog.action == "export.download")).all()]
    assert dettagli == [{"model": "finto-1", "translated": ["margin"]}] * 2


# ── #61: text YAML can carry ──────────────────────────────────────────────────
_NON_YAML = re.compile("[^\t\n\r\x20-\x7e\x85\xa0-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


@pytest.mark.anyio
async def test_ai_text_is_stored_without_characters_yaml_cannot_carry(session, scena, ai):
    """Sample values decoded with the wrong code page carry C1 controls, and the AI
    echoes them: one of them in schema.yml and `dbt parse` refuses the whole project."""
    import yaml

    ai.describe = lambda v, n: "Name,\x00 e.g. Smith\x96Jones\x7f;\x01 ok\ufffe\x85next\u2028line\x0bend"
    flow = _flusso_a_due_uscite(session, scena)
    _versione(session, flow)
    p = await _descrivi(session, scena.capo, flow)
    assert _colonne(p)["id"] == "(AI) Name, e.g. SmithJones; ok next line end"
    for r in session.exec(select(DbtAiText)).all():
        assert not _NON_YAML.search(r.text), repr(r.text)
        # as the engine writes it (a JSON string is a YAML double-quoted scalar)
        assert yaml.safe_load(f"description: {json.dumps(r.text, ensure_ascii=False)}") == {"description": r.text}


# ── handed over by the engine lane (E1 #53 #2): the AI's refusals, the source() macro ──
@pytest.mark.parametrize("motivo, params, inglese, italiano", [
    ("function_not_allowed", {"function": "dictGet"}, "it calls dictGet, which reads data or settings outside the model",
     "chiama dictGet, che legge dati o impostazioni fuori dal modello"),
    ("settings", {}, "it changes the server's settings (SETTINGS)", "cambia le impostazioni del server (SETTINGS)"),
])
def test_the_engine_s_new_reasons_to_refuse_an_ai_translation_are_sentences(motivo, params, inglese, italiano):
    """The engine refuses a proposal that reads outside the model or changes the server's
    settings: without a sentence the user read the bare reason code."""
    from app.services.messaggi_dbt import messaggio_dell_engine

    detail = {"code": "ai_translation_invalid", "message": "x", "params": {"model": "margin", "reason": motivo, **params}}
    assert messaggio_dell_engine(detail, "en") == (f"The AI's translation of «margin» did not pass the checks: {inglese}. "
                                                  "Export with another target, or without the AI translation.")
    assert italiano in messaggio_dell_engine(detail, "it")
    for lingua in ("de", "es", "fr"):
        assert motivo not in messaggio_dell_engine(detail, lingua)


def _come_legge_dbt(argomento: str) -> tuple[str, str | None]:
    """A source() argument as dbt reads it: (what Jinja renders, what dbt's static parser
    takes, None when it cannot read it and leaves it to Jinja). The static parser takes a
    string literal as written, escapes included (seen with dbt 1.12.5); Jinja reads a
    string literal as Python does."""
    import ast

    pezzi = argomento.split(" ~ ")
    jinja = "".join(ast.literal_eval(x) for x in pezzi)
    statico = argomento[1:-1] if len(pezzi) == 1 and argomento[0] == argomento[-1] and argomento[0] in "'\"" else None
    return jinja, statico


@pytest.mark.parametrize("tabella", ["ordini", "o'brien", 'say "hi"', "o'brien\\x", "both ' and \"", "\\", "'"])
@pytest.mark.parametrize("target", ["duckdb", "native", "clickhouse"])
def test_a_table_name_with_a_quote_or_a_backslash_is_read_by_dbt_as_written(session, scena, target, tabella):
    """`{{ source('…', '<table>') }}` was written with the raw table name: a quote in it
    ended the Jinja string, and dbt could not parse the model. An escaped literal is no
    answer either: dbt's static parser would read the escapes as part of the name.
    The adapter then quotes the name without escaping it: a double quote breaks
    dbt-duckdb and dbt-postgres, a backslash dbt-clickhouse, so those are refused
    (review of the #2 fix: this test asserted their payload was built)."""
    for nome in ("src", "db_1_vendite"):
        m = re.fullmatch(r"\{\{ source\('(\w+)', (.+)\) \}\}", d._macro_source(nome, tabella))
        assert m and m.group(1) == nome and _come_legge_dbt(m.group(2))[0] == tabella
    scena.ordini.source_ref = f"vendite.{tabella}"
    session.add(scena.ordini)
    session.commit()
    if ('"' in tabella and target != "clickhouse") or ("\\" in tabella and target == "clickhouse"):
        with pytest.raises(d.DbtExportError) as e:
            d.build_export_payload(session, _flusso_a_due_uscite(session, scena), "data-prep", target)
        assert e.value.code == "name_not_quotable" and e.value.params["name"] == tabella
        return
    p = d.build_export_payload(session, _flusso_a_due_uscite(session, scena), "data-prep", target)
    voce = p["source_map"][scena.ordini.key]
    macro = voce.get("ref") or voce["macro"]
    m = re.fullmatch(r"\{\{ source\('(\w+)', (.+)\) \}\}", macro)
    assert m, macro
    jinja, statico = _come_legge_dbt(m.group(2))
    assert jinja == tabella and statico in (tabella, None), macro
    assert voce["label"] == tabella
