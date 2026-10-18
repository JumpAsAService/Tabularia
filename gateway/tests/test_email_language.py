"""The emails the product sends speak the RECIPIENT's language.

The flow-failure notice and the data-contract notice were Italian for everyone.
Now each recipient that is a user of the installation gets the language they last
chose in the interface (stored on the account: PUT /users/me/locale, sent by the
interface at sign-in and when the language changes); any other address gets
English. One message per language: the recipients of a notice are split by it.
"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.security import hash_password
from app.db.session import get_session
from app.models import Connection, User
from app.routes import auth as auth_routes
from app.routes import users as users_routes
from app.services import contract_notifier, contracts, email_texts, notifier
from tests.conftest import make_datasource, make_flow, make_project, make_run, make_user

LANGS = ["it", "en", "fr", "de", "es"]
# a word each language's subject must have: the notice is really in that language
FLOW_FAILED = {"it": "fallito", "en": "failed", "fr": "échoué", "de": "fehlgeschlagen", "es": "falló"}
CONTRACT_REFUSED = {"it": "aggiornamento rifiutato", "en": "update refused", "fr": "mise à jour refusée",
                    "de": "Aktualisierung abgelehnt", "es": "actualización rechazada"}


class _Engine:
    def __init__(self):
        self.sent = []

    async def post(self, url, json=None, timeout=None):
        self.sent.append({"url": url, **(json or {})})
        return SimpleNamespace(status_code=200, text="")


def _smtp(session, project_id, owner_id):
    c = Connection(name="mail", db_type="smtp", project_id=project_id, owner_id=owner_id, host="smtp.x.it", port=587,
                   username="u", extra='{"from_address": "tabularia@x.it", "allowed_domains": ["x.it"]}')
    session.add(c)
    session.commit()
    session.refresh(c)
    return c


def _people(session):
    """One user per language, one user who never chose, one plain address."""
    for lang in LANGS:
        make_user(session, email=f"{lang}@x.it", locale=lang)
    make_user(session, email="nolang@x.it")
    return ", ".join([f"{lang}@x.it" for lang in LANGS] + ["NoLang@x.it", "outside@x.it"])


def _by_language(sent):
    return {m["to"][0].split("@")[0]: m for m in sent}


# ── the flow-failure notice ──────────────────────────────────────────────────

def test_the_failure_notice_goes_out_in_each_recipient_s_language(session, monkeypatch):
    engine = _Engine()
    monkeypatch.setattr(notifier, "get_engine_client", lambda: engine)
    owner = make_user(session, email="owner@x.it")
    p = make_project(session, name="P")
    flow = make_flow(session, name="Revenue", project_id=p.id)
    flow.notify_emails = _people(session)
    flow.notify_connection_id = _smtp(session, p.id, owner.id).id
    session.add(flow)
    session.commit()
    run = make_run(session, kind="orchestration", flow_id=flow.id, status="FAILURE", trigger_type="schedule",
                   error="division by zero", task_id="")
    asyncio.run(notifier.notify_failure(session, run))

    # five languages: English gathers the English user, the one who never chose
    # (matched without case) and the address that is not a user
    assert len(engine.sent) == 5
    english = next(m for m in engine.sent if "en@x.it" in m["to"])
    assert english["to"] == ["en@x.it", "NoLang@x.it", "outside@x.it"]
    for m in engine.sent:
        lang = m["to"][0].split("@")[0]
        assert FLOW_FAILED[lang] in m["subject"] and "«Revenue»" in m["subject"], m["subject"]
        assert "division by zero" in m["body"] and f"#{run.id}" in m["body"]
        assert m["subject"].startswith("[Tabularia]")
    assert "non è andato a buon fine" in _by_language(engine.sent)["it"]["body"]
    assert "did not succeed" in english["body"]


# ── the data-contract notice ─────────────────────────────────────────────────

REFUSED = {"outcome": "failed", "rows": 50, "errors": 1, "warnings": 0, "rules": [
    {"id": "r1", "kind": "row_count", "severity": "error", "passed": False, "observed": 50, "expected": {"max": 10}}]}


def test_the_contract_notice_goes_out_in_each_recipient_s_language(session, monkeypatch):
    engine = _Engine()
    monkeypatch.setattr(contract_notifier, "get_engine_client", lambda: engine)
    boss = make_user(session, email="boss@x.it", is_superuser=True)
    p = make_project(session, name="data", owner_id=boss.id)
    ds = make_datasource(session, name="orders", project_id=p.id, key="datasets/1/v1.parquet", rows=3)
    c = contracts.save(session, ds, contracts.validate_document({"rules": [{"kind": "row_count", "max": 10, "severity": "error"}]}),
                       True, boss.id, notify=(_people(session), _smtp(session, p.id, boss.id).id))
    contracts.record_result(session, c, {"outcome": "passed", "rows": 3, "errors": 0, "warnings": 0, "rules": []}, trigger="save")
    contracts.record_result(session, c, REFUSED, trigger="refresh", blocked=True, run_id=41)
    session.commit()
    assert asyncio.run(contract_notifier.deliver_pending(session)) == 1

    assert len(engine.sent) == 5
    for m in engine.sent:
        lang = m["to"][0].split("@")[0]
        assert CONTRACT_REFUSED[lang] in m["subject"] and "«orders»" in m["subject"], m["subject"]
        assert "row_count" in m["body"] and "#41" in m["body"] and "50" in m["body"]
    by = _by_language(engine.sent)
    assert "NON è stato pubblicato" in by["it"]["body"]
    assert "was NOT published" in by["en"]["body"]


def test_every_language_has_every_text():
    """No text falls back to English by being forgotten in one language."""
    keys = set(email_texts.TEXTS["en"])
    for lang in LANGS:
        assert set(email_texts.TEXTS[lang]) == keys, lang


def test_an_unknown_language_is_english():
    assert email_texts.language_of(None) == "en" and email_texts.language_of("pt") == "en"


# ── the language on the account ──────────────────────────────────────────────

@pytest.fixture
def app(session):
    app = FastAPI()
    for r in (auth_routes.router, users_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _browser(app, session, email="a@x.it"):
    make_user(session, email=email, hashed_password=hash_password("a-long-password-1"))
    c = TestClient(app)
    assert c.post("/auth/login", json={"email": email, "password": "a-long-password-1"}).status_code == 200
    return c


CSRF = {"X-Tabularia-Csrf": "1"}


def test_the_interface_stores_the_language_the_user_chose(app, session):
    c = _browser(app, session)
    assert c.get("/auth/me").json()["locale"] is None
    assert c.put("/users/me/locale", json={"locale": "de"}, headers=CSRF).status_code == 204
    assert c.get("/auth/me").json()["locale"] == "de"
    session.expire_all()
    assert session.exec(__import__("sqlmodel").select(User).where(User.email == "a@x.it")).one().locale == "de"


def test_only_the_languages_of_the_interface(app, session):
    c = _browser(app, session)
    assert c.put("/users/me/locale", json={"locale": "pt"}, headers=CSRF).status_code == 422
    assert c.put("/users/me/locale", json={"locale": "it"}).status_code == 403  # the cookie alone needs the CSRF header
