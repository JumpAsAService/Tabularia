"""The AI agents' instructions from Administration → AI (services/ai_prompts.py).

The rules these tests hold: every agent's instructions are the text (the default, or the
administrators' one) followed by the context Tabularia adds; the language of the user's
interface is always the LAST line, whatever the text says; an administrator changes and
restores a text, with the audit; the default's own text is no change; observers read with
the e-mails masked and write nothing; the other users do not even read.
"""
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from sqlmodel import select

from app.db.session import get_session
from app.models import AiPrompt, AuditLog
from app.routes import ai as ai_routes
from app.services import ai_agent, ai_flow_agent, ai_prompts, ai_script_agent, audit
from app.services.dbt_ai import translation_context
from tests.conftest import make_user
from tests.test_engine_settings import CSRF, _sign_in

URL = "/ai/prompts"


@pytest.fixture
def app(session):
    from app.routes import auth as auth_routes

    app = FastAPI()
    for r in (auth_routes.router, ai_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _deps(cls, session, user, **kw):
    return cls(user=user, session_factory=lambda: nullcontext(session), engine="duckdb", model_id="m", locale="it", **kw)


def _instructions(module, deps):
    return module._instructions(SimpleNamespace(deps=deps))


def test_the_language_is_the_last_line_of_every_agent(session):
    user = make_user(session)
    texts = {
        "assistant": _instructions(ai_agent, _deps(ai_agent.ChatDeps, session, user)),
        "flow": _instructions(ai_flow_agent, _deps(ai_flow_agent.FlowDeps, session, user, flow_name="Vendite", selected="op-2")),
        "script": _instructions(ai_script_agent, _deps(ai_script_agent.ScriptDeps, session, user, environment="py",
                                                       files=[{"path": "main.py", "content": "print(1)"}], entrypoint="main.py")),
    }
    for agent, text in texts.items():
        assert text.startswith(ai_prompts.DEFAULTS[agent].strip()), agent
        assert ai_prompts.CONTEXT_HEADER in text, agent
        assert text.splitlines()[-1].startswith("Language: write every answer in Italian"), agent
    assert "DuckDB SQL" in texts["assistant"] and "«Vendite»" in texts["flow"] and "op-2" in texts["flow"]
    assert "print(1)" in texts["script"] and "Operations and their params" in texts["flow"]


def test_a_changed_text_replaces_the_default_but_never_the_context(session):
    admin = make_user(session, email="boss@x.local", is_superuser=True)
    ai_prompts.save(session, "assistant", "Reply like a pirate. Answer in English.", admin.email)
    deps = _deps(ai_agent.ChatDeps, session, admin, prompt=ai_prompts.text(session, "assistant"))
    text = _instructions(ai_agent, deps)
    assert text.startswith("Reply like a pirate.") and "How to work" not in text
    # the administrators' text cannot take away the dialect, the cap or the interface's language
    assert "DuckDB SQL" in text and text.splitlines()[-1].startswith("Language: write every answer in Italian")
    assert ai_prompts.text(session, "flow") == ai_prompts.FLOW  # the others untouched


def test_the_title_and_the_translation_take_their_context_too():
    assert ai_prompts.language_line("de", "the title").startswith("Language: write the title in German")
    ctx = translation_context("duckdb", "clickhouse", ["a", "b"])
    assert "Source dialect: duckdb." in ctx and "Target dialect: clickhouse." in ctx and "in order: a, b." in ctx


def test_the_administrator_changes_and_restores_a_text(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    items = boss.get(URL, params={"locale": "it"}).json()
    assert [i["agent"] for i in items] == list(ai_prompts.AGENTS)
    assistant = items[0]
    assert not assistant["custom"] and assistant["text"] == assistant["default"] == ai_prompts.ASSISTANT
    assert assistant["context"].splitlines()[-1].startswith("Language: write every answer in Italian")

    r = boss.put(f"{URL}/flow", params={"locale": "it"}, json={"text": "  Build flows, briefly.  "}, headers=CSRF)
    assert r.status_code == 200, r.text
    flow = r.json()
    assert (flow["custom"], flow["text"], flow["updated_by"]) == (True, "Build flows, briefly.", "capo@x.local")
    assert session.get(AiPrompt, "flow").text == "Build flows, briefly."

    # the default's own text is no change
    assert not boss.put(f"{URL}/flow", json={"text": ai_prompts.FLOW}, headers=CSRF).json()["custom"]
    boss.put(f"{URL}/flow", json={"text": "Again."}, headers=CSRF)
    restored = boss.delete(f"{URL}/flow", headers=CSRF).json()
    assert not restored["custom"] and restored["text"] == ai_prompts.FLOW
    session.expire_all()
    assert session.exec(select(AiPrompt)).all() == []
    # restoring what is the default already writes nothing
    assert boss.delete(f"{URL}/flow", headers=CSRF).status_code == 200
    entries = session.exec(select(AuditLog).where(AuditLog.action.in_([audit.AI_PROMPT_UPDATE, audit.AI_PROMPT_RESET])).order_by(AuditLog.id)).all()
    assert [(e.action, e.target_label) for e in entries] == [
        (audit.AI_PROMPT_UPDATE, "flow"), (audit.AI_PROMPT_UPDATE, "flow"),
        (audit.AI_PROMPT_UPDATE, "flow"), (audit.AI_PROMPT_RESET, "flow"),
    ]
    assert json.loads(entries[0].detail) == {"chars": len("Build flows, briefly.")}


def test_refusals(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    assert boss.put(f"{URL}/nobody", json={"text": "x"}, headers=CSRF).status_code == 404
    assert boss.put(f"{URL}/assistant", json={"text": "   "}, headers=CSRF).status_code == 422
    too_long = "x" * (ai_prompts.MAX_CHARS + 1)
    assert boss.put(f"{URL}/assistant", json={"text": too_long}, headers=CSRF).status_code == 422
    assert session.exec(select(AiPrompt)).all() == []


def test_the_observer_reads_masked_and_does_not_write(app, session):
    boss = _sign_in(app, session, "capo@x.local", is_superuser=True)
    boss.put(f"{URL}/script", json={"text": "Short answers."}, headers=CSRF)
    eye = _sign_in(app, session, "occhio@x.local", is_observer=True)
    script = next(i for i in eye.get(URL).json() if i["agent"] == "script")
    assert script["text"] == "Short answers." and script["updated_by"] != "capo@x.local"
    assert eye.put(f"{URL}/script", json={"text": "Mine."}, headers=CSRF).status_code == 403
    assert eye.delete(f"{URL}/script", headers=CSRF).status_code == 403
    nobody = _sign_in(app, session, "ana@x.local")
    assert nobody.get(URL).status_code == 403
    assert session.get(AiPrompt, "script").text == "Short answers."
