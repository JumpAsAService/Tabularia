"""Several AI providers, and Anthropic through its own SDK.

1. Providers from the panel: administrators write, observers read without secrets, the key
   never comes back nor reaches the log, the audit or an error; every change is audited.
2. The environment's provider stays as it was, read-only. The same model name on two providers
   stays two models.
3. Anthropic: the real SDK against a fake Messages API over HTTP (tests/fake_anthropic.py) that
   refuses what the real one refuses. Each known trap has its test: forced tool choice, sampling,
   thinking, an edited history, refusals, max_tokens, parallel results, typed errors, costs, caps,
   metadata-only mode.
"""
import json
import logging
from contextlib import nullcontext
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from pydantic_ai import Agent
from sqlmodel import select

import app.core.config as config_mod
from app.core.crypto import decrypt_secret
from app.core.security import hash_password
from app.db.session import get_session
from app.models import AiChat, AiChatTurn, AiModel, AiProvider, AiSpend, AuditLog
from app.routes import ai as ai_routes
from app.routes import ai_providers as providers_routes
from app.routes import auth as auth_routes
from app.services import ai_agent, ai_anthropic, ai_chats, ai_models, ai_pricing, audit, dbt_ai
from app.core import overrides as ov
from app.services.agent import common
from tests.conftest import make_datasource, make_permission, make_project, make_user
from tests.fake_anthropic import (API_KEY, DEFAULT_MODELS, OVERLOADED, RATE_LIMITED, FakeProvider, failure, refusal, text, thought,
                                  tools)

PASSWORD = "una-password-lunga"
HASHED = hash_password(PASSWORD)  # once: it is slow on purpose
CSRF = {"X-Tabularia-Csrf": "1"}
OPUS = "claude-opus-5-5"
COLUMNS = [{"name": "country", "dtype": "String"}, {"name": "amount", "dtype": "Float64"}]
ROWS = {"columns": COLUMNS, "rows": [{"country": "IT", "amount": 3.0}], "row_count": 1, "truncated": False}


@pytest.fixture(autouse=True)
def _clean_panel():
    ov.forget()
    ai_models.clear_cache()
    yield
    ov.forget()
    ai_models.clear_cache()


@pytest.fixture
def fake():
    server = FakeProvider().start()
    yield server
    server.stop()


@pytest.fixture
def app(session, fake_engine, monkeypatch):
    app = FastAPI()
    for r in (auth_routes.router, ai_routes.router, providers_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    monkeypatch.setattr(ai_routes, "_new_session", lambda: nullcontext(session))
    monkeypatch.setattr(ai_models, "_new_session", lambda: nullcontext(session))
    monkeypatch.setattr(common, "new_session", lambda: nullcontext(session))
    fake_engine.preview_response = (200, ROWS)
    return app


@pytest.fixture
def people(session):
    admin = make_user(session, email="admin@x.local", hashed_password=HASHED, is_superuser=True)
    anna = make_user(session, email="anna@x.local", hashed_password=HASHED)
    oscar = make_user(session, email="oscar@x.local", hashed_password=HASHED, is_observer=True)
    sales = make_project(session, name="Sales")
    make_permission(session, user_id=anna.id, project_id=sales.id, capability="edit")
    orders = make_datasource(session, name="orders", project_id=sales.id, key="datasets/1/o.parquet", rows=10,
                             columns=json.dumps(COLUMNS), description="Orders")
    return dict(admin=admin.id, anna=anna.id, oscar=oscar.id, orders=orders.id)


def signed_in(app, email) -> TestClient:
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": PASSWORD}, headers=CSRF).status_code == 200
    return c


@pytest.fixture
def admin(app, people):
    return signed_in(app, "admin@x.local")


@pytest.fixture
def anna(app, people):
    return signed_in(app, "anna@x.local")


def add_provider(client, fake, name="Claude", kind="anthropic", key=API_KEY) -> dict:
    r = client.post("/ai/providers", json={"name": name, "kind": kind, "base_url": fake.url + ("/v1" if kind != "anthropic" else ""),
                                           "api_key": key}, headers=CSRF)
    assert r.status_code == 201, r.text
    return r.json()


def enable(client, ref):
    r = client.put(f"/ai/models/{ref}", json={"enabled": True}, headers=CSRF)
    assert r.status_code == 200, r.text
    return r.json()


@pytest.fixture
def claude(admin, fake) -> str:
    """The reference of Opus 5.5 at the fake provider, enabled."""
    provider = add_provider(admin, fake)
    return enable(admin, f"{provider['key']}/{OPUS}")["id"]


def events_of(body: str) -> list[tuple[str, dict]]:
    out = []
    for block in body.strip().split("\n\n"):
        name = block.split("\n")[0].removeprefix("event: ")
        out.append((name, json.loads(block.split("\n", 1)[1].removeprefix("data: "))))
    return out


def ask(client, model, message="How much did we sell?", chat_id=None, **rest) -> list[tuple[str, dict]]:
    r = client.post("/ai/chat", json={"message": message, "model": model, "chat_id": chat_id, "locale": "en", **rest}, headers=CSRF)
    assert r.status_code == 200, r.text
    return events_of(r.text)


def done(events) -> dict:
    assert events[-1][0] == "done", events
    return events[-1][1]


def error(events) -> dict:
    assert events[-1][0] == "error", events
    return events[-1][1]


def model_requests(fake) -> list[dict]:
    """What reached the model, its own title's call aside."""
    return [r["body"] for r in fake.requests if r["body"].get("tools")]


# ── 1. the providers from the panel ───────────────────────────────────────────
def test_the_key_is_encrypted_and_never_comes_back(app, admin, fake, session, caplog):
    caplog.set_level(logging.DEBUG)
    made = add_provider(admin, fake)
    assert made["has_key"] is True and API_KEY not in json.dumps(made)
    row = session.get(AiProvider, made["id"])
    assert API_KEY not in row.secret_encrypted and decrypt_secret(row.secret_encrypted) == API_KEY
    assert API_KEY not in admin.get("/ai/providers").text
    changed = admin.patch(f"/ai/providers/{made['id']}", json={"name": "Claude EU", "api_key": "sk-new-one"}, headers=CSRF)
    assert changed.status_code == 200 and "sk-new-one" not in changed.text
    assert decrypt_secret(session.get(AiProvider, made["id"]).secret_encrypted) == "sk-new-one"
    # empty = the one saved stays
    assert admin.patch(f"/ai/providers/{made['id']}", json={"name": "Claude", "api_key": ""}, headers=CSRF).status_code == 200
    assert decrypt_secret(session.get(AiProvider, made["id"]).secret_encrypted) == "sk-new-one"
    records = [(r.action, r.target_label, r.detail) for r in session.exec(select(AuditLog)).all() if r.action.startswith("ai.provider")]
    assert [r[0] for r in records] == [audit.AI_PROVIDER_CREATE, audit.AI_PROVIDER_UPDATE, audit.AI_PROVIDER_UPDATE]
    assert '"key_changed": true' in records[1][2] and "key_changed" not in records[2][2]
    everything = json.dumps(records) + caplog.text
    assert API_KEY not in everything and "sk-new-one" not in everything


def test_only_administrators_write_and_observers_read_without_secrets(app, admin, fake, people):
    made = add_provider(admin, fake)
    anna = signed_in(app, "anna@x.local")
    oscar = signed_in(app, "oscar@x.local")
    new = {"name": "x", "kind": "anthropic", "api_key": "k"}
    for who in (anna, oscar):
        assert who.post("/ai/providers", json=new, headers=CSRF).status_code == 403
        assert who.patch(f"/ai/providers/{made['id']}", json={"active": False}, headers=CSRF).status_code == 403
        assert who.delete(f"/ai/providers/{made['id']}", headers=CSRF).status_code == 403
        assert who.post("/ai/providers/test", json={"key": made["key"]}, headers=CSRF).status_code == 403
    assert anna.get("/ai/providers").status_code == 403
    seen = oscar.get("/ai/providers")
    assert seen.status_code == 200
    item = seen.json()[0]
    assert item["name"] == "Claude" and item["kind"] == "anthropic" and item["base_url"] == ""
    assert API_KEY not in seen.text and fake.url not in seen.text
    assert [m["model_id"] for m in oscar.get(f"/ai/models?provider={made['key']}").json()] == sorted(DEFAULT_MODELS)


def test_a_new_address_needs_the_key_again(admin, fake, session):
    """The saved key would otherwise go wherever the new address says, in a change or in a test."""
    made = add_provider(admin, fake)
    other = "https://elsewhere.example"
    r = admin.patch(f"/ai/providers/{made['id']}", json={"base_url": other}, headers=CSRF)
    assert r.status_code == 422 and "API key" in r.text
    r = admin.post("/ai/providers/test", json={"key": made["key"], "base_url": other}, headers=CSRF)
    assert r.status_code == 422
    assert session.get(AiProvider, made["id"]).base_url == fake.url
    assert not fake.attempts and admin.patch(f"/ai/providers/{made['id']}", json={"base_url": fake.url}, headers=CSRF).status_code == 200


@pytest.mark.parametrize(("kind", "given", "stored"), [
    ("anthropic", "", ai_models.ANTHROPIC_BASE_URL),
    ("anthropic", "https://api.anthropic.com/v1/", "https://api.anthropic.com"),
    ("openai_compatible", "https://ai.example/v1/", "https://ai.example/v1"),
])
def test_the_address_is_stored_as_the_client_wants_it(kind, given, stored):
    assert ai_models.normalize_base_url(kind, given) == stored


@pytest.mark.parametrize("wrong", ["ftp://x.example", "not an address", "https://user:pass@x.example", "https://x.example?key=1"])
def test_an_address_that_cannot_be_used_is_refused(admin, wrong):
    r = admin.post("/ai/providers", json={"name": "x", "kind": "anthropic", "base_url": wrong, "api_key": "k"}, headers=CSRF)
    assert r.status_code == 422
    assert "user:pass" not in r.text and "key=1" not in r.text


def test_no_key_no_provider_and_names_do_not_repeat(admin, fake):
    assert admin.post("/ai/providers", json={"name": "x", "kind": "anthropic", "api_key": " "}, headers=CSRF).status_code == 422
    add_provider(admin, fake)
    r = admin.post("/ai/providers", json={"name": "claude", "kind": "anthropic", "api_key": "k"}, headers=CSRF)
    assert r.status_code == 409 and "already" in r.text


def test_the_test_says_what_is_wrong_in_words(admin, fake):
    good = admin.post("/ai/providers/test", json={"kind": "anthropic", "base_url": fake.url, "api_key": API_KEY}, headers=CSRF).json()
    assert good == {"ok": True, "models": len(DEFAULT_MODELS), "code": None, "message": None}
    wrong = admin.post("/ai/providers/test", json={"kind": "anthropic", "base_url": fake.url, "api_key": "sk-wrong"}, headers=CSRF).json()
    assert wrong["ok"] is False and wrong["code"] == "provider_key" and "sk-wrong" not in json.dumps(wrong)
    fake.catalog_error = OVERLOADED
    assert admin.post("/ai/providers/test", json={"kind": "anthropic", "base_url": fake.url, "api_key": API_KEY},
                      headers=CSRF).json()["code"] == "provider_overloaded"
    fake.catalog_error = None
    gone = admin.post("/ai/providers/test", json={"kind": "anthropic", "base_url": "http://127.0.0.1:9", "api_key": "k"}, headers=CSRF).json()
    assert gone["code"] == "provider_unreachable"
    # the OpenAI-compatible protocol, the same words
    assert admin.post("/ai/providers/test", json={"kind": "openai_compatible", "base_url": fake.url + "/v1", "api_key": "sk-wrong"},
                      headers=CSRF).json()["code"] == "provider_key"
    assert admin.post("/ai/providers/test", json={"kind": "openai_compatible", "base_url": fake.url + "/v1", "api_key": API_KEY},
                      headers=CSRF).json()["ok"] is True


def test_a_provider_that_fails_does_not_hide_the_others(admin, fake, session):
    made = add_provider(admin, fake)
    other = add_provider(admin, SimpleNamespace(url="http://127.0.0.1:9"), name="Down", key="k")
    assert admin.get(f"/ai/models?provider={other['key']}").status_code == 502
    listed = admin.get("/ai/models").json()
    assert {m["provider"] for m in listed} == {made["key"]} and all(m["provider_name"] == "Claude" for m in listed)


# ── 2. the environment's provider, and two with the same model ────────────────
@pytest.fixture
def from_the_environment(monkeypatch, fake):
    """As a deployment that only ever had AI__BASE_URL: a real read of its catalog."""
    settings = config_mod.Settings(ai={"base_url": fake.url + "/v1", "secret_key": API_KEY})
    for mod in (ai_routes, ai_agent, ai_models, dbt_ai):
        monkeypatch.setattr(mod, "get_settings", lambda: settings)
    return settings


def test_the_environment_is_a_provider_nobody_can_change_here(admin, from_the_environment, fake, session):
    listed = admin.get("/ai/providers").json()
    assert listed == [{"key": "env", "id": None, "name": "127.0.0.1", "kind": "openai_compatible", "base_url": fake.url + "/v1",
                       "active": True, "has_key": True, "read_only": True, "chats": 0, "enabled_models": 0}]
    assert API_KEY not in json.dumps(listed)
    assert admin.post("/ai/providers/test", json={"key": "env"}, headers=CSRF).json()["ok"] is True
    assert admin.patch("/ai/providers/env", json={"active": False}, headers=CSRF).status_code == 422
    assert admin.delete("/ai/providers/env", headers=CSRF).status_code == 422


def test_what_was_enabled_before_stays_enabled_and_goes_on(admin, anna, from_the_environment, fake, session):
    """Rows, a conversation and a ledger as the previous version left them: nothing to migrate."""
    session.add(AiModel(model_id="old-model", enabled=True))
    session.commit()
    fake.models = ["old-model", "another"]
    assert anna.get("/ai/status").json()["models"] == ["old-model"]
    assert [(m["id"], m["model_id"], m["enabled"]) for m in admin.get("/ai/models").json()] == [
        ("another", "another", False), ("old-model", "old-model", True)]
    first = done(ask(anna, "old-model", "hello"))
    again = done(ask(anna, "old-model", "and again", chat_id=first["chat_id"]))
    assert again["chat_id"] == first["chat_id"]
    assert [m["role"] for m in fake.chat_requests[-1]["messages"]][-3:] == ["user", "assistant", "user"]
    assert session.exec(select(AiSpend.model_id)).all()[0] == "old-model"
    assert session.exec(select(AiChat.model_id)).one() == "old-model"


def test_the_same_model_on_two_providers_is_two_models(admin, anna, from_the_environment, fake, session):
    second = FakeProvider().start()
    try:
        fake.models = second.models = [OPUS]
        first_provider = add_provider(admin, fake, name="Claude EU")
        second_provider = add_provider(admin, second, name="Claude US")
        one, other = f"{first_provider['key']}/{OPUS}", f"{second_provider['key']}/{OPUS}"
        enable(admin, one)
        assert anna.get("/ai/status").json()["options"] == [{"id": one, "model": OPUS, "provider": "Claude EU"}]
        enable(admin, other)
        status = anna.get("/ai/status").json()
        assert status["models"] == sorted([one, other])
        assert [o["provider"] for o in status["options"]] == ["Claude EU", "Claude US"]
        admin.put(f"/ai/models/{one}", json={"enabled": False}, headers=CSRF)
        assert anna.get("/ai/status").json()["models"] == [other]
        ask(anna, other)
        assert second.requests and not fake.requests
    finally:
        second.stop()


def test_the_default_model_can_be_a_bare_name(admin, anna, monkeypatch, fake):
    """`claude-opus-5-5` in Settings, whichever provider has it."""
    provider = add_provider(admin, fake)
    refs = [enable(admin, f"{provider['key']}/{m}")["id"] for m in (OPUS, "claude-sonnet-5-5")]
    settings = config_mod.Settings(ai={"default_model": "claude-sonnet-5-5"})
    monkeypatch.setattr(ai_routes, "get_settings", lambda: settings)
    assert anna.get("/ai/status").json()["default_model"] == refs[1]
    assert ai_models.preferred("nothing-like-it", refs) == refs[0]
    assert ai_models.preferred(refs[0], refs) == refs[0]
    # with nothing configured the environment's stays first: a new provider moves no one's data by itself
    assert ai_models.preferred("", ["provider:1/a", "zeta"]) == "zeta"


# ── a provider off or gone ────────────────────────────────────────────────────
def test_a_provider_turned_off_keeps_its_conversations_readable(app, admin, anna, claude, fake, session):
    provider_id = ai_models.split_ref(claude)[0]
    chat_id = done(ask(anna, claude, "first question"))["chat_id"]
    r = admin.patch(f"/ai/providers/{provider_id}", json={"active": False}, headers=CSRF)
    assert r.status_code == 200 and r.json()["chats"] == 1 and r.json()["enabled_models"] == 1
    assert anna.get("/ai/status").json()["models"] == []
    opened = anna.get(f"/ai/chats/{chat_id}").json()
    assert opened["messages"][0]["question"] == "first question"
    refused = anna.post("/ai/chat", json={"message": "next", "model": claude, "chat_id": chat_id, "locale": "en"}, headers=CSRF)
    assert refused.status_code == 403 and "Claude" in refused.text and "turned off" in refused.text
    # and on again, what was enabled is back
    admin.patch(f"/ai/providers/{provider_id}", json={"active": True}, headers=CSRF)
    assert anna.get("/ai/status").json()["models"] == [claude]


def test_a_provider_in_use_is_removed_only_after_saying_what_goes(app, admin, anna, claude, fake, session):
    provider_id = ai_models.split_ref(claude)[0]
    chat_id = done(ask(anna, claude, "first question"))["chat_id"]
    r = admin.delete(f"/ai/providers/{provider_id}", headers=CSRF)
    assert r.status_code == 409 and "1 conversations" in r.text and "stay readable" in r.text
    assert session.get(AiProvider, provider_id) is not None
    assert admin.delete(f"/ai/providers/{provider_id}?confirm=true", headers=CSRF).status_code == 204
    assert session.get(AiProvider, provider_id) is None and session.exec(select(AiModel)).all() == []
    opened = anna.get(f"/ai/chats/{chat_id}").json()
    assert opened["messages"][0]["answer"] == "ok" and opened["messages"][0]["model_id"] == claude
    refused = anna.post("/ai/chat", json={"message": "next", "model": claude, "chat_id": chat_id, "locale": "en"}, headers=CSRF)
    assert refused.status_code == 403 and "no longer exists" in refused.text
    assert [r.action for r in session.exec(select(AuditLog)).all()].count(audit.AI_PROVIDER_DELETE) == 1
    # a conversation about it goes on with another model
    other = add_provider(admin, fake, name="Other")
    ref = enable(admin, f"{other['key']}/{OPUS}")["id"]
    assert done(ask(anna, ref, "next", chat_id=chat_id))["chat_id"] == chat_id


def test_a_provider_with_nothing_depending_on_it_goes_without_asking(admin, fake, session):
    made = add_provider(admin, fake)
    assert admin.delete(f"/ai/providers/{made['id']}", headers=CSRF).status_code == 204


def test_a_key_that_cannot_be_read_any_more_is_said_not_a_500(admin, anna, claude, session):
    row = session.get(AiProvider, ai_models.split_ref(claude)[0])
    row.secret_encrypted = "not a fernet token"
    session.add(row)
    session.commit()
    r = anna.post("/ai/chat", json={"message": "x", "model": claude, "locale": "en"}, headers=CSRF)
    assert r.status_code == 409 and "enter it again" in r.text


# ── 3. the catalog through the SDK ────────────────────────────────────────────
def test_the_catalog_comes_from_the_sdk_through_every_page(admin, fake):
    made = add_provider(admin, fake)
    listed = admin.get(f"/ai/models?provider={made['key']}").json()
    assert [m["model_id"] for m in listed] == sorted(DEFAULT_MODELS)  # pages of two
    assert all(m["id"] == f"{made['key']}/{m['model_id']}" and m["chat"] for m in listed)


def test_credentials_only_from_the_provider_row(admin, anna, claude, fake, monkeypatch):
    """Whatever the machine has in its environment or on disk is not used."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-the-environment")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-from-the-environment")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:9")
    assert done(ask(anna, claude))  # the fake answers only to the provider's key, and is where the provider's address says
    assert fake.requests


# ── 4. Anthropic: what goes on the wire ───────────────────────────────────────
@pytest.mark.parametrize("model", [OPUS, "claude-sonnet-5-5", "claude-fable-5-1", "claude-haiku-5-5"])
def test_a_turn_sends_nothing_the_current_models_refuse(admin, anna, fake, people, model):
    provider = add_provider(admin, fake)
    ref = enable(admin, f"{provider['key']}/{model}")["id"]
    fake.script = [tools(("toolu_1", "list_datasources", {})), thought("Done.")]
    assert done(ask(anna, ref))
    assert not [a for a in fake.attempts if a.get("refused")]
    for body in model_requests(fake):
        assert not {"temperature", "top_p", "top_k", "thinking", "fallbacks"} & set(body)
        assert body["tool_choice"]["type"] == "auto"
        assert body["max_tokens"] == ai_anthropic.TURN_MAX_TOKENS
        assert body["output_config"]["effort"] == ai_anthropic.EFFORT
        assert body["cache_control"]["type"] == "ephemeral"
        assert body["stream"] is True


@pytest.mark.anyio
async def test_the_title_goes_at_low_effort_uncached_and_streamed_for_a_large_max_tokens(claude, fake):
    fake.script = [text("Sales by country")]
    title, usage = await ai_agent.suggest_title(claude, "How much did we sell by country?")
    assert title == "Sales by country" and usage.requests == 1
    body = fake.requests[-1]["body"]
    assert body["output_config"]["effort"] == "low" and "cache_control" not in body
    assert body["stream"] is True  # the SDK would not send it unstreamed: no timeout waiting for it


def test_a_model_the_library_does_not_know_gets_no_effort_and_no_forced_tool(admin, fake, session):
    """A model of tomorrow: its defaults, not a 400 for something it may not take."""
    assert "anthropic_effort" not in ai_anthropic._settings(ai_anthropic.profile_of("claude-future-9"))
    assert ai_anthropic.profile_of("claude-future-9").get("supports_forced_tool_choice") is False


class _Sql(BaseModel):
    sql: str


@pytest.mark.anyio
@pytest.mark.parametrize("model", [OPUS, "claude-sonnet-5-5", "claude-fable-5-1", "claude-haiku-5-5"])
async def test_a_structured_answer_needs_no_forced_tool(fake, model):
    """The dbt export's agent: on these `any` and `tool` are a 400."""
    ep = ai_models.Endpoint(provider_id=1, name="c", kind="anthropic", base_url=fake.url, active=True, stamp="", secret_plain=_key())
    fake.script = [text('{"sql": "select 1"}')]
    built = ai_anthropic.build(model, ep)
    out = await Agent(built, output_type=_Sql, instructions="i").run("q", model_settings=ai_agent.one_off_settings(built))
    await ai_agent.close_model(built)
    assert out.output.sql == "select 1"
    body = fake.requests[-1]["body"]
    assert "tool_choice" not in body and "tools" not in body and "cache_control" not in body
    assert body["output_config"]["format"]["type"] == "json_schema"


@pytest.mark.anyio
async def test_on_a_model_the_library_does_not_know_the_structured_answer_is_asked_softly(fake):
    fake.models.append("claude-future-9")
    ep = ai_models.Endpoint(provider_id=1, name="c", kind="anthropic", base_url=fake.url, active=True, stamp="", secret_plain=_key())
    fake.script = [tools(("toolu_1", "final_result", {"sql": "select 2"}), thinking=False)]
    built = ai_anthropic.build("claude-future-9", ep)
    out = await Agent(built, output_type=_Sql).run("q")
    await ai_agent.close_model(built)
    assert out.output.sql == "select 2" and fake.requests[-1]["body"]["tool_choice"]["type"] == "auto"


def _key():
    from pydantic import SecretStr

    return SecretStr(API_KEY)


def test_the_tools_of_every_assistant_are_taken(admin, anna, claude, fake, session, people):
    """The strict fake refuses a schema the API refuses (`anyOf` at the top, a bad name)."""
    from app.models import Group, ScriptEnvironment, ScriptEnvironmentGroup, ScriptEnvironmentVersion, UserGroupLink

    assert done(ask(anna, claude))
    r = anna.post("/ai/flow-assist", json={"message": "add a step", "model": claude, "locale": "en",
                                           "definition": json.dumps({"nodes": [], "edges": []})}, headers=CSRF)
    assert "event: done" in r.text, r.text
    env = ScriptEnvironment(name="Py", language="python")
    group = Group(name="g")
    session.add_all([env, group])
    session.commit()
    version = ScriptEnvironmentVersion(environment_id=env.id, number=1, language_version="3.12", base_image="python:3.12-slim",
                                       packages="", status="ready")
    session.add(version)
    session.add_all([UserGroupLink(user_id=people["anna"], group_id=group.id), ScriptEnvironmentGroup(environment_id=env.id, group_id=group.id)])
    session.commit()
    r = anna.post("/ai/script-assist", json={"message": "fix", "model": claude, "environment_id": env.id, "locale": "en",
                                             "files": [{"path": "main.py", "content": "print(1)"}], "entrypoint": "main.py"}, headers=CSRF)
    assert "event: done" in r.text, r.text
    assert not [a["refused"] for a in fake.attempts if a.get("refused")]
    assert len(model_requests(fake)) == 3


# ── 5. the history: only ever added to ────────────────────────────────────────
def test_parallel_tool_calls_and_their_results_go_back_in_one_message(anna, claude, fake, people):
    fake.script = [tools(("toolu_a", "list_datasources", {}), ("toolu_b", "describe_datasource", {"datasource_id": people["orders"]}),
                         words="Looking."), thought("Done.")]
    events = ask(anna, claude)
    assert done(events)["usage"]["requests"] == 2
    assert [e[1]["name"] for e in events if e[0] == "tool_result"] == ["list_datasources", "describe_datasource"]
    second = model_requests(fake)[1]["messages"]
    assert [b["type"] for b in second[1]["content"]] == ["thinking", "text", "tool_use", "tool_use"]
    assert [b["tool_use_id"] for b in second[2]["content"]] == ["toolu_a", "toolu_b"]
    assert all(b["type"] == "tool_result" for b in second[2]["content"])


def test_a_conversation_goes_back_exactly_as_it_was_sent(anna, claude, fake, people):
    """Thinking blocks untouched, every earlier message the same: the strict fake checks each
    signature against what came before it, here on a new account's terms."""
    fake.script = [tools(("toolu_a", "list_datasources", {})), thought("First answer."), thought("Second answer.")]
    chat_id = done(ask(anna, claude, "first"))["chat_id"]
    done(ask(anna, claude, "second", chat_id=chat_id))
    assert not [a["refused"] for a in fake.attempts if a.get("refused")]
    earlier, later = model_requests(fake)[1], model_requests(fake)[2]
    assert later["messages"][:len(earlier["messages"]) + 1][:-1] == earlier["messages"]
    assert later["system"] == earlier["system"]
    assert "thinking" not in later  # nothing was dropped: no `block_binding` asked for


def test_a_change_of_the_system_prompt_costs_a_retry_and_no_error(anna, claude, fake, people):
    """The date, the plans, the datasources in focus are in the system prompt, by design: the
    API refuses the old thinking blocks, the library asks it to drop them, the person sees nothing."""
    fake.script = [thought("First."), thought("Second."), thought("Third.")]
    chat_id = done(ask(anna, claude, "first"))["chat_id"]
    events = ask(anna, claude, "second", chat_id=chat_id, focus=[people["orders"]])
    assert done(events) and "Second." in "".join(e[1].get("delta", "") for e in events)
    refused = [a for a in fake.attempts if a.get("refused")]
    assert len(refused) == 1 and "bound to a different conversation" in refused[0]["refused"]
    last = fake.attempts[-1]
    assert last["body"]["thinking"]["block_binding"]["prefix_mismatch_behavior"] == "drop_block"
    assert "thinking-binding-controls-2026-08-01" in last["headers"]["anthropic-beta"]
    # the next turn asks for the drop straight away: no second refusal
    done(ask(anna, claude, "third", chat_id=chat_id, focus=[people["orders"]]))
    assert len([a for a in fake.attempts if a.get("refused")]) == 1


def test_a_history_cut_by_the_limit_is_cut_between_turns_without_thinking(anna, claude, fake, session, monkeypatch):
    """`AI__MAX_HISTORY_MESSAGES`: cut inside a turn, it begins with an answer or with a result
    without its call. With the limit below a turn with tools, the strict fake would refuse both."""
    settings = config_mod.Settings(ai={"max_history_messages": 3})
    monkeypatch.setattr(ai_routes, "get_settings", lambda: settings)
    fake.script = [tools(("toolu_a", "list_datasources", {})), thought("First."), tools(("toolu_b", "list_datasources", {})),
                   thought("Second."), thought("Third.")]
    chat_id = done(ask(anna, claude, "first"))["chat_id"]
    done(ask(anna, claude, "second", chat_id=chat_id))
    events = ask(anna, claude, "third", chat_id=chat_id)
    assert done(events)
    assert not [a["refused"] for a in fake.attempts if a.get("refused")]
    sent = model_requests(fake)[-1]["messages"]
    assert sent[0]["role"] == "user" and sent[0]["content"][0]["text"] == "second"
    assert not [b for m in sent for b in m["content"] if isinstance(b, dict) and b.get("type") == "thinking"]
    assert sent[-1]["content"][-1]["text"] == "third"


def test_the_newest_turn_is_kept_whole_even_over_the_limit(session, fake):
    chat = AiChat(user_id=1, model_id="x")
    session.add(chat)
    session.commit()
    many = [{"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "q"}]},
            {"kind": "response", "parts": [{"part_kind": "text", "content": "a"}]}] * 3
    session.add(AiChatTurn(chat_id=chat.id, seq=0, messages=json.dumps(many)))
    session.commit()
    assert len(ai_chats.history(session, chat, 2)) == len(many)


def test_a_conversation_moves_from_one_claude_to_another_keeping_its_blocks(admin, anna, claude, fake):
    """The API drops what the new model cannot read: blocks go back as they are, no 400."""
    provider = claude.split("/")[0]
    sonnet = enable(admin, f"{provider}/claude-sonnet-5-5")["id"]
    fake.script = [tools(("toolu_a", "list_datasources", {})), thought("First."), thought("Second.")]
    chat_id = done(ask(anna, claude, "first"))["chat_id"]
    assert done(ask(anna, sonnet, "second", chat_id=chat_id))
    assert not [a["refused"] for a in fake.attempts if a.get("refused")]
    last = model_requests(fake)[-1]
    assert last["model"] == "claude-sonnet-5-5"
    assert any(b.get("type") == "thinking" for m in last["messages"] for b in m["content"] if isinstance(b, dict))
    assert anna.get(f"/ai/chats/{chat_id}").json()["model_id"] == sonnet


def test_a_conversation_moves_from_claude_to_an_openai_compatible_provider_and_back(admin, anna, claude, fake, session):
    other = add_provider(admin, fake, name="Other", kind="openai_compatible")
    gpt = enable(admin, f"{other['key']}/{OPUS}")["id"]  # the same name, on purpose
    fake.script = [tools(("toolu_a", "list_datasources", {})), thought("From Claude."), thought("Claude again.")]
    # a provider whose tool ids the Messages API would not take
    fake.chat_script = [{"tool_calls": [{"id": "functions.list_datasources:0", "name": "list_datasources", "args": {}}]},
                        {"content": "From the other one."}]
    chat_id = done(ask(anna, claude, "first"))["chat_id"]
    assert done(ask(anna, gpt, "second", chat_id=chat_id))
    sent = json.dumps(fake.chat_requests[0]["messages"])
    assert "sig:" not in sent and "toolu_a" in sent  # no block of Claude's goes to anyone else
    assert fake.chat_requests[0]["model"] == OPUS
    assert done(ask(anna, claude, "third", chat_id=chat_id))
    assert not [a["refused"] for a in fake.attempts if a.get("refused")]
    back = model_requests(fake)[-1]["messages"]
    ids = [b["id"] for m in back for b in m["content"] if isinstance(b, dict) and b.get("type") == "tool_use"]
    assert "functions_list_datasources_0" in ids
    # and what is saved stays as it was: the export of a step reads it
    assert "functions.list_datasources:0" in session.exec(select(AiChatTurn).where(AiChatTurn.seq == 1)).one().messages


def test_a_listing_keeps_its_count_when_the_conversation_is_opened_again(anna, claude, fake, people):
    fake.script = [tools(("toolu_a", "list_datasources", {})), thought("One.")]
    chat_id = done(ask(anna, claude))["chat_id"]
    assert anna.get(f"/ai/chats/{chat_id}").json()["messages"][0]["steps"][0]["count"] == 1


def test_a_refused_call_saved_by_the_previous_library_still_reads_and_goes_on(anna, claude, fake, session, people):
    """pydantic-ai 2.46 saved a tool's refusal as a `retry-prompt`; today's as an outcome of the return."""
    old = [{"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "old question"}]},
           {"kind": "response", "parts": [{"part_kind": "tool-call", "tool_name": "list_datasources", "args": "{}", "tool_call_id": "call_1"}],
            "model_name": "m"},
           {"kind": "request", "parts": [{"part_kind": "retry-prompt", "content": "not allowed", "tool_name": "list_datasources",
                                          "tool_call_id": "call_1"}]},
           {"kind": "response", "parts": [{"part_kind": "text", "content": "old answer"}], "model_name": "m"}]
    chat = AiChat(user_id=people["anna"], title="old", model_id="m")
    session.add(chat)
    session.commit()
    session.add(AiChatTurn(chat_id=chat.id, seq=0, question="old question", messages=json.dumps(old), model_id="m"))
    session.commit()
    step = anna.get(f"/ai/chats/{chat.id}").json()["messages"][0]["steps"][0]
    assert step["ok"] is False and step["error"] == "not allowed"
    events = ask(anna, claude, "go on", chat_id=chat.id)
    assert not [a["refused"] for a in fake.attempts if a.get("refused")]
    assert done(events)


def test_a_call_refused_now_shows_its_reason_when_the_conversation_is_opened_again(anna, claude, fake, people):
    fake.script = [tools(("toolu_a", "describe_datasource", {"datasource_id": 999})), thought("Nothing there.")]
    chat_id = done(ask(anna, claude))["chat_id"]
    step = anna.get(f"/ai/chats/{chat_id}").json()["messages"][0]["steps"][0]
    assert step["ok"] is False and "No datasource" in step["error"]


def test_a_conversation_saved_before_this_version_goes_on_with_claude(admin, anna, claude, fake, session, people):
    """A turn as the previous version saved it, with an OpenAI-compatible model."""
    old = [{"kind": "request", "parts": [{"part_kind": "user-prompt", "content": "old question", "timestamp": "2026-10-01T00:00:00Z"}],
            "instructions": "old instructions"},
           {"kind": "response", "parts": [
               {"part_kind": "thinking", "content": "some reasoning", "id": "reasoning_content", "provider_name": "openai"},
               {"part_kind": "text", "content": "old answer"}],
            "model_name": "gpt-oss-120b", "provider_name": "openai"}]
    chat = AiChat(user_id=people["anna"], title="old", model_id="gpt-oss-120b")
    session.add(chat)
    session.commit()
    session.add(AiChatTurn(chat_id=chat.id, seq=0, question="old question", messages=json.dumps(old), model_id="gpt-oss-120b"))
    session.commit()
    assert anna.get(f"/ai/chats/{chat.id}").json()["messages"][0]["answer"] == "old answer"
    events = ask(anna, claude, "go on", chat_id=chat.id)
    assert not [a["refused"] for a in fake.attempts if a.get("refused")]
    assert done(events)["chat_id"] == chat.id
    assert "old answer" in json.dumps(model_requests(fake)[-1]["messages"])


# ── 6. refusals, the length, the errors ───────────────────────────────────────
def test_a_refusal_before_any_output_is_said_not_an_empty_answer(anna, claude, fake, session):
    fake.script = [refusal()]
    wrong = error(ask(anna, claude))
    assert wrong["code"] == "refused" and "declined" in wrong["message"] and wrong["discard"] is True
    assert session.exec(select(AiChat)).all() == [] and session.exec(select(AiChatTurn)).all() == []
    assert len(session.exec(select(AiSpend)).all()) == 1  # what it cost is counted


def test_a_refusal_after_some_text_is_not_an_answer(anna, claude, fake, session):
    fake.script = [refusal("The first half of")]
    events = ask(anna, claude)
    wrong = error(events)
    assert wrong["code"] == "refused" and wrong["discard"] is True
    assert session.exec(select(AiChatTurn)).all() == []
    # it does not stay in the history: the next question starts clean
    fake.script = [text("Fine.")]
    assert done(ask(anna, claude, "another"))
    assert "first half" not in json.dumps(model_requests(fake)[-1]["messages"])


def test_a_refusal_after_a_tool_call_does_not_run_the_tool(anna, claude, fake, session, people):
    fake.script = [{**tools(("toolu_a", "query_datasource", {"datasource_id": people["orders"], "sql": "SELECT 1 FROM self"})),
                    "stop_reason": "refusal", "stop_details": {"type": "refusal", "category": "bio", "explanation": None}}]
    wrong = error(ask(anna, claude))
    assert wrong["code"] == "refused"
    assert not [r for r in session.exec(select(AuditLog)).all() if r.action == audit.AI_QUERY]


def test_an_answer_cut_by_max_tokens_says_so(anna, claude, fake, session):
    fake.script = [{"content": [{"type": "text", "text": "The total is"}], "stop_reason": "max_tokens"}]
    assert done(ask(anna, claude))["truncated"] is True


def test_a_complete_answer_is_not_cut(anna, claude, fake):
    assert done(ask(anna, claude))["truncated"] is False


def test_max_tokens_inside_a_tool_call_is_said(anna, claude, fake):
    block = {"type": "tool_use", "id": "toolu_a", "name": "list_datasources", "input": {"search": "a long search"}, "cut": True}
    fake.script = [{"content": [block], "stop_reason": "max_tokens"}]
    fake.script.append(thought("Shorter, then."))
    events = ask(anna, claude)
    # the library tells the model the arguments are broken, and it tries again: the API takes that history
    assert done(events)["usage"]["requests"] == 2 and [e[1]["ok"] for e in events if e[0] == "tool_result"] == [False]
    assert not [a["refused"] for a in fake.attempts if a.get("refused")]


@pytest.mark.parametrize(("reply", "code", "words", "attempts"), [
    (failure(401, "authentication_error", "invalid x-api-key"), "provider_key", "refused the API key", 1),
    (failure(402, "billing_error", "credit balance too low"), "provider_billing", "billing", 1),
    (failure(403, "permission_error", "not allowed"), "provider_forbidden", "does not allow", 1),
    (failure(404, "not_found_error", f"model: {OPUS}"), "provider_model", "does not offer", 1),
    (failure(400, "invalid_request_error", "prompt is too long"), "provider_rejected", "refused the request", 1),
    (RATE_LIMITED, "provider_busy", "limiting requests", 3),
    (OVERLOADED, "provider_overloaded", "overloaded", 3),
    (failure(500, "api_error", "internal", {"retry-after": "0"}), "provider_error", "a problem of its own", 3),
])
def test_each_error_of_the_provider_has_its_words_and_its_retries(anna, claude, fake, reply, code, words, attempts):
    fake.script = [reply] * attempts
    wrong = error(ask(anna, claude))
    assert wrong["code"] == code and words in wrong["message"] and "Claude" in wrong["message"], wrong
    assert len(fake.requests) == attempts
    assert reply["error"]["message"] not in wrong["message"]  # what it said stays in the log


def test_the_sdk_gets_past_a_moment_of_overload_by_itself(anna, claude, fake):
    fake.script = [OVERLOADED, thought("Now it answers.")]
    assert done(ask(anna, claude))
    assert len(fake.requests) >= 2


def test_overload_in_the_middle_of_a_stream_is_said(anna, claude, fake):
    fake.script = [{**text("Part of"), "stream_error": {"type": "overloaded_error", "message": "Overloaded"}}]
    wrong = error(ask(anna, claude))
    assert wrong["code"] != "internal", wrong


def test_a_provider_that_cannot_be_reached_is_said(admin, anna, session):
    down = add_provider(admin, SimpleNamespace(url="http://127.0.0.1:9"), key="k")
    session.add(AiModel(model_id=f"{down['key']}/{OPUS}", enabled=True))
    session.commit()
    wrong = error(ask(anna, f"{down['key']}/{OPUS}"))
    assert wrong["code"] == "provider_unreachable" and "could not be reached" in wrong["message"]


def test_a_wrong_key_at_a_real_chat_goes_nowhere_in_the_log(admin, anna, fake, session, caplog):
    caplog.set_level(logging.DEBUG)
    made = add_provider(admin, fake, key="sk-this-is-wrong-and-secret")
    session.add(AiModel(model_id=f"{made['key']}/{OPUS}", enabled=True))
    session.commit()
    wrong = error(ask(anna, f"{made['key']}/{OPUS}"))
    assert wrong["code"] == "provider_key"
    audited = json.dumps([(r.action, r.detail) for r in session.exec(select(AuditLog)).all()])
    assert "sk-this-is-wrong-and-secret" not in caplog.text + audited + json.dumps(wrong)


# ── 7. what it costs, and the caps ────────────────────────────────────────────
def _usage(**fields):
    return SimpleNamespace(cost=None, requests=1, cache_read_tokens=0, cache_write_tokens=0, details={}, **fields)


@pytest.mark.parametrize(("model", "price_in", "price_out"), [
    (OPUS, "4", "20"), ("claude-sonnet-5-5", "2", "10"), ("claude-haiku-5-5", "0.10", "0.50"), ("claude-fable-5-1", "10", "50"),
])
def test_todays_prices_are_known(model, price_in, price_out):
    million = _usage(input_tokens=100_000, output_tokens=100_000)
    assert ai_pricing.cost_of(million, f"provider:3/{model}") == (Decimal(price_in) + Decimal(price_out)) / 10
    assert ai_pricing.is_priced(model)


def test_the_cost_of_a_turn_counts_the_cache_at_its_rates(anna, claude, fake, session):
    """Opus 5.5: 150 fresh at $4, 1000 written at $5, 2000 read at $0.20, 30 out at $20 per million."""
    fake.script = [text("x", usage={"input_tokens": 150, "output_tokens": 30, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 2000})]
    usage = done(ask(anna, claude))["usage"]
    assert usage["input_tokens"] == 3150 and Decimal(usage["cost_usd"]) == Decimal("0.0066")


def test_the_turn_cap_stops_a_turn_at_a_known_price(anna, claude, fake, monkeypatch, session):
    settings = config_mod.Settings(ai={"max_cost_per_turn_usd": 0.01})
    monkeypatch.setattr(ai_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(ai_agent, "get_settings", lambda: settings)
    fake.script = [tools(("toolu_a", "list_datasources", {}), usage={"input_tokens": 10_000, "output_tokens": 1000})]
    wrong = error(ask(anna, claude))
    assert wrong["code"] == "too_many_steps"
    assert len(model_requests(fake)) == 1  # not the next request
    assert Decimal(session.exec(select(AiSpend)).one().cost_usd) == Decimal("0.06")


def test_what_is_read_from_the_cache_does_not_count_against_the_input_cap(anna, claude, fake, monkeypatch, session):
    """A flow built in ten rounds reads its context ten times, at a tenth of the price: stopped at
    400,000 of them, it had spent $0.15 of a cap of $0.50, and every retry spent as much again."""
    settings = config_mod.Settings(ai={"max_input_tokens_per_turn": 1000})
    monkeypatch.setattr(ai_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(ai_agent, "get_settings", lambda: settings)
    fake.script = [tools(("toolu_a", "list_datasources", {}), usage={"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 5000}),
                   text("done", usage={"input_tokens": 100, "output_tokens": 10, "cache_read_input_tokens": 5000})]
    answer = done(ask(anna, claude))
    assert answer["usage"]["requests"] == 2 and answer["usage"]["input_tokens"] == 10_200


@pytest.mark.parametrize("usage", [
    {"input_tokens": 1500, "output_tokens": 10},
    {"input_tokens": 100, "output_tokens": 10, "cache_creation_input_tokens": 1500},  # written: paid more than fresh
], ids=["fresh", "written to the cache"])
def test_what_is_not_read_from_the_cache_still_meets_the_input_cap(anna, claude, fake, monkeypatch, session, usage):
    settings = config_mod.Settings(ai={"max_input_tokens_per_turn": 1000})
    monkeypatch.setattr(ai_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(ai_agent, "get_settings", lambda: settings)
    fake.script = [tools(("toolu_a", "list_datasources", {}), usage=usage)]
    assert error(ask(anna, claude))["code"] == "too_many_steps"
    assert len(model_requests(fake)) == 1  # not the next request
    assert session.exec(select(AiSpend)).one().input_tokens >= 1500


def test_a_model_with_no_known_price_still_meets_the_turn_cap(admin, anna, fake, monkeypatch, session):
    """Before: `cost_limit` read no price, so it did not protect at all, in silence."""
    fake.models.append("claude-unpriced-model")
    made = add_provider(admin, fake)
    ref = enable(admin, f"{made['key']}/claude-unpriced-model")
    assert ref["priced"] is False
    settings = config_mod.Settings(ai={"max_cost_per_turn_usd": 0.01})
    monkeypatch.setattr(ai_routes, "get_settings", lambda: settings)
    monkeypatch.setattr(ai_agent, "get_settings", lambda: settings)
    fake.script = [tools(("toolu_a", "list_datasources", {}), usage={"input_tokens": 10_000, "output_tokens": 1000})]
    assert error(ask(anna, ref["id"]))["code"] == "too_many_steps"
    assert len(model_requests(fake)) == 1
    assert session.exec(select(AiSpend)).one().cost_usd is None  # the record stays honest: not known


def test_a_model_with_no_known_price_counts_towards_the_daily_caps(session, people):
    from app.models import User

    user = session.get(User, people["anna"])
    session.add(AiSpend(user_id=user.id, model_id="unpriced", input_tokens=1_000_000, output_tokens=0, cost_usd=None))
    session.add(AiSpend(user_id=user.id, model_id="priced", input_tokens=1, output_tokens=1, cost_usd="0.5"))
    session.commit()
    assert ai_chats.spent_today(session, user) == Decimal("4.5") == ai_chats.spent_today_all(session)


def test_a_model_with_a_price_is_said_to_have_one(admin, fake):
    made = add_provider(admin, fake)
    assert all(m["priced"] for m in admin.get(f"/ai/models?provider={made['key']}").json())


# ── 8. metadata-only mode, on this provider too ───────────────────────────────
CANARY = "CANARY-7f3a"


def test_metadata_only_mode_keeps_the_values_from_anthropic(anna, claude, fake, fake_engine, session, people):
    """The mode's barrier is in the tools: whichever provider answers, nothing of a row is sent."""
    fake_engine.preview_response = (200, {**ROWS, "rows": [{"country": CANARY, "amount": 3.0}]})
    ov.set_values({"ai.metadata_only": True})
    fake.script = [tools(("toolu_a", "describe_datasource", {"datasource_id": people["orders"]}),
                         ("toolu_b", "query_datasource", {"datasource_id": people["orders"], "sql": "SELECT country FROM self"})),
                   thought("I cannot read the data.")]
    assert done(ask(anna, claude))
    sent = json.dumps([r["body"] for r in fake.requests])
    assert CANARY not in sent
    assert all("query_datasource" not in {t["name"] for t in r["body"].get("tools", [])} for r in fake.requests)
    assert "METADATA-ONLY MODE" in sent


def test_the_mode_leaves_earlier_turns_out_and_with_them_the_thinking_blocks(anna, claude, fake, fake_engine, session, people):
    """The earlier turn read rows. Left out, the turns after it would have blocks bound to a
    history that changed: they go too, and the API has nothing to refuse."""
    fake_engine.preview_response = (200, {**ROWS, "rows": [{"country": CANARY, "amount": 3.0}]})
    fake.script = [tools(("toolu_a", "query_datasource", {"datasource_id": people["orders"], "sql": "SELECT country FROM self"})),
                   thought("The country is CANARY.")]
    chat_id = done(ask(anna, claude, "first"))["chat_id"]
    assert CANARY in json.dumps(model_requests(fake)[-1]["messages"])  # the mode was off
    ov.set_values({"ai.metadata_only": True})
    fake.script = [thought("In the mode.")]
    done(ask(anna, claude, "second", chat_id=chat_id))
    fake.script = [thought("Still in the mode.")]
    done(ask(anna, claude, "third", chat_id=chat_id))
    assert not [a["refused"] for a in fake.attempts if a.get("refused")]
    last = model_requests(fake)[-1]
    assert CANARY not in json.dumps(last["messages"]) and "CANARY" not in json.dumps(last["messages"])
    assert not [b for m in last["messages"] for b in m["content"] if isinstance(b, dict) and b.get("type") == "thinking"]
    assert last["messages"][0]["content"][0]["text"] == "second"


# ── 9. the dbt export ─────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_the_dbt_export_runs_on_anthropic_and_names_the_model_in_words(admin, claude, fake, session, people):
    from app.models import Flow, User

    user = session.get(User, people["admin"])
    flow = Flow(name="f", project_id=1, definition="{}")
    session.add(flow)
    session.commit()
    assert dbt_ai.ai_model(session) == claude
    fake.script = [text(json.dumps({"sql": "SELECT 1"}))]
    outcome = await dbt_ai._run(claude, dbt_ai._Translation, "translate", "SELECT 1", session, user, dbt_ai.new_deadline())
    assert outcome.sql == "SELECT 1"
    body = fake.requests[-1]["body"]
    assert "tool_choice" not in body and "format" in body["output_config"] and body["stream"] is True
    dbt_ai._remember(session, flow.id, 0, "translate", {"k": "SELECT 1"}, claude)
    assert dbt_ai.version_texts(session, flow)["models"] == [f"{OPUS} (Claude)"]
    assert session.exec(select(AiSpend)).one().cost_usd is not None


@pytest.mark.anyio
async def test_the_dbt_export_says_what_the_provider_answered_in_words(admin, claude, fake, session, people):
    from app.models import User
    from app.services.dbt_export import DbtExportError

    user = session.get(User, people["admin"])
    fake.script = [failure(401, "authentication_error", "secret detail of the provider")]
    with pytest.raises(DbtExportError) as e:
        await dbt_ai._run(claude, dbt_ai._Translation, "translate", "SELECT 1", session, user, dbt_ai.new_deadline())
    assert "refused the API key" in str(e.value) and "secret detail" not in str(e.value)
    fake.script = [refusal()]
    with pytest.raises(DbtExportError) as e:
        await dbt_ai._run(claude, dbt_ai._Translation, "translate", "SELECT 1", session, user, dbt_ai.new_deadline())
    assert "declined" in str(e.value)


def test_the_fake_really_refuses_what_it_is_meant_to(fake):
    """A check of the checks: a request the code must never send gets its 400 from the fake."""
    import httpx

    def post(**body):
        return httpx.post(fake.url + "/v1/messages", headers={"x-api-key": API_KEY, "anthropic-version": "2026-10-21"},
                          json={"model": OPUS, "max_tokens": 10, "messages": [{"role": "user", "content": "x"}], **body}).status_code

    assert post() == 200
    assert post(temperature=0.5) == 400
    assert post(tool_choice={"type": "any"}) == 400
    assert post(tool_choice={"type": "tool", "name": "x"}) == 400
    assert post(thinking={"type": "disabled"}) == 400
    assert post(thinking={"type": "enabled", "budget_tokens": 1024}) == 400
    assert post(messages=[{"role": "assistant", "content": "x"}]) == 400
    assert post(messages=[{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "x"}]}]) == 400


def test_run_usage_is_counted_when_a_turn_stops(anna, claude, fake, session):
    fake.script = [tools(("toolu_a", "list_datasources", {})), refusal()]
    assert error(ask(anna, claude))["code"] == "refused"
    row = session.exec(select(AiSpend)).one()
    assert row.input_tokens == 200 and row.output_tokens == 20
