"""An AI comment in a document (docs/specs/ai-comment.md): a report's pages, PDF and summary, a dashboard's PDF and
image, a Pushmail cell, with the fake engine and the fake provider over real HTTP. No test here reaches a real model,
and every request is recorded. What the comment itself does is in test_ai_comment.py; how it is drawn in
backend/tests/test_report_comment.py.

What these tests hold: a report without the element takes no step more; a document's text is on that document's
numbers, and its pages read the same rows; a reader with no right on an element gets nothing of it in the paper,
with a comment kept and with none; what fails costs the comment, and the document goes out with its line; the same
numbers twice are one call, on whoever caused it; the designer, a read-only token and a notebook cause none; Excel
asks nothing; a cell of a flow group goes out with the provider down, on the bill of whoever runs it.
"""
import json
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import get_settings
from app.db.session import get_session
from app.models import AiSpend, AuditLog, Report, ReportVersion, User
from app.routes import auth as auth_routes
from app.routes import dashboards as board_routes
from app.routes import reports as routes
from app.services import ai_comment, api_tokens, metadata_only, reports
from tests.conftest import make_permission
from tests.fake_anthropic import OVERLOADED, text
from tests.test_ai_comment import add_provider, db, fake, model, sent, setting  # noqa: F401 — some are fixtures
from tests.test_dashboards_comment import numbers_say
from tests.test_dashboards_comment import board_of as _board_of
from tests.test_dashboards_data import _dataset, _saved, _uid, _world
from tests.test_engine_settings import CSRF, _sign_in

AT = "2026-11-01T10:00:00+00:00"
FAILED_EN = ai_comment.MESSAGES["failed"]["en"]


@pytest.fixture
def app(db, fake_engine):
    app = FastAPI()
    for r in (auth_routes.router, routes.router, board_routes.router):
        app.include_router(r)

    def one_session():
        with Session(db) as s:
            yield s

    app.dependency_overrides[get_session] = one_session
    return app


@pytest.fixture
def s(db):
    with Session(db) as session:
        yield session


def report_of(w, *more, parameters=(), locale="en") -> dict:
    """Orders, which everyone here may read: a KPI, a chart, a table that only lists rows, and «story»."""
    return {
        "v": 1, "locale": locale, "timezone": "UTC", "parameters": list(parameters),
        "page": {"size": "A4", "orientation": "portrait", "margins": [10, 10, 10, 10]},
        "datasets": [_dataset("orders", w["orders"]), *more],
        "sections": {"body": {"items": [
            {"id": "rev", "type": "kpi", "x": 0, "y": 0, "w": 50, "h": 20, "dataset": "orders", "label": "Revenue", "value": "SUM([amount])"},
            {"id": "by", "type": "chart", "kind": "column", "x": 0, "y": 25, "w": 100, "h": 40, "dataset": "orders", "title": "By region", "category": "[region]",
             "series": [{"value": "SUM([amount])"}]},
            {"id": "story", "type": "comment", "x": 0, "y": 70, "w": 120, "h": 30, "title": "What changed", "instruction": "Say what moved most.", "reads": ["rev", "by"]},
        ]}},
    }


def without_comment(d: dict) -> dict:
    d = json.loads(json.dumps(d))
    d["sections"]["body"]["items"] = [it for it in d["sections"]["body"]["items"] if it["type"] != "comment"]
    return d


def report_saved(s, w, content, name="Monthly") -> int:
    r = Report(name=name, project_id=w["sales"], current_version=1)
    s.add(r)
    s.commit()
    s.add(ReportVersion(report_id=r.id, version=1, content=json.dumps(content)))
    s.commit()
    return r.id


def reader_of(app, s, w, email="reader@x.local", capability="view", **kw) -> TestClient:
    client = _sign_in(app, s, email, **kw)
    make_permission(s, user_id=_uid(s, email), project_id=w["sales"], capability=capability)
    return client


def render(client, report, fmt="pdf", **body):
    return client.post(f"/reports/{report}/render", json={"format": fmt, **body}, headers=CSRF)


def token_of_level(s, user_id: int, level: str) -> str:
    owner = s.get(User, user_id)
    return api_tokens.create(s, owner, "script", level, 30, owner)[1]


def spent(s) -> list[AiSpend]:
    s.expire_all()
    return list(s.exec(select(AiSpend)).all())


def audited(s) -> list[AuditLog]:
    s.expire_all()
    return list(s.exec(select(AuditLog).where(AuditLog.action == "ai.comment")).all())


# ── a report without the element ─────────────────────────────────────────────
def test_a_report_without_the_element_asks_for_no_numbers_and_no_model_and_sends_what_it_always_sent(app, s, fake, model, fake_engine):
    w = _world(s)
    report = report_saved(s, w, without_comment(report_of(w)))
    r = render(reader_of(app, s, w), report)
    assert r.status_code == 200, r.text
    assert fake_engine.report_tiles == [] and fake.requests == []
    assert not {"comments", "reading"} & set(fake_engine.report_renders[0])


# ── the text goes with the render ────────────────────────────────────────────
@pytest.mark.parametrize("fmt, moment", [("pdf", "document"), ("html", "document"), ("svg", "open")], ids=["pdf", "email's body", "viewer"])
def test_the_text_goes_with_the_render_on_the_numbers_it_read_and_on_whoever_asked(app, s, fake, model, fake_engine, fmt, moment):
    w = _world(s)
    report = report_saved(s, w, report_of(w))
    fake.script = [text("Revenue is up.")]
    numbers_say(fake_engine, story={"type": "comment", "computed_at": AT, "cached": False})
    r = render(reader_of(app, s, w), report, fmt)
    assert r.status_code == 200, r.text
    (asked,), (body,) = fake_engine.report_tiles, fake_engine.report_renders
    assert asked["tiles"] == ["rev", "by", "story"] and asked["of_report"] is True
    assert body["comments"]["story"]["text"] == "Revenue is up."
    assert body["comments"]["story"]["numbers_at"] == AT and "claude-opus-5-5" in body["comments"]["story"]["model"]
    assert asked["reading"] and body["reading"] == asked["reading"], "the pages read the rows it read"
    assert "1.234,50" in sent(fake).message and "Say what moved most." in sent(fake).message
    (row,) = spent(s)
    assert row.user_id == _uid(s, "reader@x.local")
    assert json.loads(audited(s)[0].detail)["moment"] == moment


def test_excel_asks_for_no_numbers_and_no_model(app, s, fake, model, fake_engine):
    w = _world(s)
    report = report_saved(s, w, report_of(w))
    assert render(reader_of(app, s, w), report, "xlsx").status_code == 200
    assert fake_engine.report_tiles == [] and fake.requests == []
    assert not {"comments", "reading"} & set(fake_engine.report_renders[0])


def test_two_documents_on_the_same_numbers_are_one_call_and_each_reads_its_own_rows(app, s, fake, model, fake_engine):
    w = _world(s)
    report = report_saved(s, w, report_of(w))
    fake.script = [text("Revenue is up."), text("A second call.")]
    numbers_say(fake_engine)
    client = reader_of(app, s, w)
    for fmt in ("pdf", "pdf", "svg", "html"):
        assert render(client, report, fmt).status_code == 200
    assert len(fake.requests) == 1
    assert {b["comments"]["story"]["text"] for b in fake_engine.report_renders} == {"Revenue is up."}
    readings = [b["reading"] for b in fake_engine.report_renders]
    assert len(set(readings)) == 4, "nothing of one document's rows is another's"


def test_other_numbers_are_another_call_and_other_values_of_a_parameter_read_those(app, s, fake, model, fake_engine):
    w = _world(s)
    d = report_of(w, parameters=[{"name": "region", "type": "text"}])
    report = report_saved(s, w, d)
    fake.script = [text("First."), text("Second.")]
    client = reader_of(app, s, w)
    numbers_say(fake_engine, revenue="100,00")
    render(client, report, params={"region": "North"})
    numbers_say(fake_engine, revenue="200,00")
    render(client, report, params={"region": "South"})
    assert len(fake.requests) == 2
    assert "100,00" in sent(fake, 0).message and "200,00" not in sent(fake, 0).message
    assert "200,00" in sent(fake, 1).message and "100,00" not in sent(fake, 1).message
    assert [t["params"] for t in fake_engine.report_tiles] == [{"region": "North"}, {"region": "South"}]
    assert [b["comments"]["story"]["text"] for b in fake_engine.report_renders] == ["First.", "Second."]


def test_the_rows_of_a_table_never_reach_the_model_from_a_document(app, s, fake, model, fake_engine):
    w = _world(s)
    d = report_of(w)
    d["sections"]["body"]["items"].insert(0, {"id": "rows", "type": "table", "x": 0, "y": 110, "w": 100, "h": 10, "dataset": "orders", "columns": [{"header": "Order", "value": "[region]"}]})
    d["sections"]["body"]["items"][-1]["reads"] = ["rev", "rows"]
    report = report_saved(s, w, d)
    fake.script = [text("Fine.")]
    numbers_say(fake_engine, rows={"type": "table", "matrix": False, "empty": False, "computed_at": AT, "columns": [{"header": "Order"}], "rows": [{"kind": "d", "cells": [{"text": "ROW-SECRET", "value": "x"}]}], "total_rows": 1})
    assert render(reader_of(app, s, w), report).status_code == 200
    assert "ROW-SECRET" not in sent(fake).raw and "1.234,50" in sent(fake).message


def test_a_comment_hidden_by_its_own_condition_makes_no_call(app, s, fake, model, fake_engine):
    w = _world(s)
    report = report_saved(s, w, report_of(w))
    numbers_say(fake_engine, story={"hidden": True, "type": "comment", "computed_at": AT})
    assert render(reader_of(app, s, w), report).status_code == 200
    assert fake.requests == [] and "story" not in (fake_engine.report_renders[0].get("comments") or {})


# ── rights ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kept", [False, True], ids=["nothing kept", "a comment kept"])
def test_who_may_not_read_a_dataset_of_a_report_gets_no_document_and_no_text(app, s, fake, model, fake_engine, kept):
    """A report stops at a dataset its reader may not read, comment or no comment."""
    w = _world(s)
    d = report_of(w, _dataset("pay", w["salaries"]))
    report = report_saved(s, w, d)
    fake.script = [text("WRITTEN-FOR-WHO-READS-MORE")]
    numbers_say(fake_engine)
    if kept:
        boss = _sign_in(app, s, "boss@x.local", is_superuser=True)
        assert render(boss, report).status_code == 200
        assert len(fake.requests) == 1
    before = len(fake.requests), len(fake_engine.report_renders), len(fake_engine.report_tiles)
    r = render(reader_of(app, s, w), report)
    assert r.status_code == 422 and "WRITTEN-FOR-WHO-READS-MORE" not in r.text
    assert (len(fake.requests), len(fake_engine.report_renders), len(fake_engine.report_tiles)) == before


# ── what fails costs the comment ─────────────────────────────────────────────
def answering_late(fake, monkeypatch, seconds: float) -> None:
    real = fake._answer

    def slowly(body):
        threading.Event().wait(seconds)
        return real(body)

    monkeypatch.setattr(fake, "_answer", slowly)


def failed_with(fake, model) -> None:
    fake.script = [OVERLOADED, OVERLOADED, OVERLOADED, OVERLOADED, OVERLOADED, OVERLOADED]


def test_a_provider_that_is_down_costs_the_comment_and_the_document_goes_out_with_its_line(app, s, fake, model, fake_engine):
    w = _world(s)
    report = report_saved(s, w, report_of(w))
    failed_with(fake, model)
    numbers_say(fake_engine)
    r = render(reader_of(app, s, w), report)
    assert r.status_code == 200, r.text
    assert fake_engine.report_renders[0]["comments"] == {"story": {"message": FAILED_EN}}


def test_a_wrong_key_costs_the_comment_too(app, s, fake, fake_engine, db):
    add_provider(db, fake.url, key="a-wrong-key")
    w = _world(s)
    report = report_saved(s, w, report_of(w))
    numbers_say(fake_engine)
    assert render(reader_of(app, s, w), report).status_code == 200
    assert fake_engine.report_renders[0]["comments"]["story"] == {"message": FAILED_EN}


def test_an_answer_with_no_text_costs_the_comment(app, s, fake, model, fake_engine):
    w = _world(s)
    report = report_saved(s, w, report_of(w))
    fake.script = [text("   ")]
    numbers_say(fake_engine)
    assert render(reader_of(app, s, w), report).status_code == 200
    assert fake_engine.report_renders[0]["comments"]["story"] == {"message": FAILED_EN}


def test_past_the_wait_the_document_goes_out_with_the_line(app, s, fake, model, fake_engine, monkeypatch):
    setting(monkeypatch, comment_wait_seconds=1)
    try:
        w = _world(s)
        report = report_saved(s, w, report_of(w))
        fake.script = [text("Written late.")]
        answering_late(fake, monkeypatch, 2.5)
        numbers_say(fake_engine)
        started = time.monotonic()
        assert render(reader_of(app, s, w), report).status_code == 200
        assert time.monotonic() - started < 2.4, "it does not wait for the model"
        assert fake_engine.report_renders[0]["comments"]["story"] == {"message": FAILED_EN}, "paper does not say a text is on its way"
    finally:
        get_settings.cache_clear()


def test_a_ceiling_reached_says_so_and_the_document_goes_out(app, s, fake, model, fake_engine, monkeypatch):
    w = _world(s)
    report = report_saved(s, w, report_of(w))
    monkeypatch.setattr(ai_comment, "_over_the_ceiling", lambda user: "the day's")
    numbers_say(fake_engine)
    assert render(reader_of(app, s, w), report).status_code == 200
    assert fake_engine.report_renders[0]["comments"]["story"] == {"message": ai_comment.MESSAGES["budget"]["en"]} and fake.requests == []


def test_no_provider_is_a_comment_not_available_on_paper_even_to_an_administrator(app, s, db, fake_engine):
    w = _world(s)
    report = report_saved(s, w, report_of(w))
    numbers_say(fake_engine)
    assert render(_sign_in(app, s, "boss@x.local", is_superuser=True), report).status_code == 200
    assert fake_engine.report_renders[0]["comments"]["story"] == {"message": FAILED_EN}, "no address in an email of where to add a model"


@pytest.mark.parametrize("answer", [(500, {"detail": "boom"}), (504, {"detail": "too long"})], ids=["an error", "too long"])
def test_numbers_the_engine_cannot_give_cost_the_comment_and_not_the_document(app, s, fake, model, fake_engine, answer):
    w = _world(s)
    report = report_saved(s, w, report_of(w))
    fake_engine.report_tiles_response = answer
    assert render(reader_of(app, s, w), report).status_code == 200
    assert fake.requests == []
    assert fake_engine.report_renders[0]["comments"]["story"] == {"message": FAILED_EN}, "nothing to say of rights: the numbers did not come"


def test_in_metadata_only_mode_nothing_is_read_for_it_nothing_is_sent_and_the_paper_says_so(app, s, fake, model, fake_engine, monkeypatch):
    w = _world(s)
    report = report_saved(s, w, report_of(w))
    monkeypatch.setattr(metadata_only, "on", lambda *a: True)
    assert render(reader_of(app, s, w), report).status_code == 200
    assert fake.requests == [] and fake_engine.report_tiles == []
    assert fake_engine.report_renders[0]["comments"]["story"] == {"message": ai_comment.MESSAGES["off"]["en"]}


@pytest.mark.parametrize("locale", ["en", "it", "de", "es", "fr"])
def test_the_line_is_in_the_language_of_the_report(app, s, fake, model, fake_engine, locale):
    w = _world(s)
    report = report_saved(s, w, report_of(w, locale=locale))
    failed_with(fake, model)
    numbers_say(fake_engine)
    render(reader_of(app, s, w), report)
    assert fake_engine.report_renders[0]["comments"]["story"]["message"] == ai_comment.MESSAGES["failed"][locale]


def test_no_database_connection_waits_for_the_model(app, s, fake, model, fake_engine, db, monkeypatch):
    """A slow provider and one request: while the provider answers, the request's session holds none."""
    w = _world(s)
    report = report_saved(s, w, report_of(w))
    numbers_say(fake_engine)
    seen = []
    real = ai_comment._ask

    async def watching(job, spent_):
        seen.append(("asked", [x.in_transaction() for x in live]))
        return await real(job, spent_)

    live: list[Session] = []

    def tracked():
        with Session(db) as session:
            live.append(session)
            yield session

    app.dependency_overrides[get_session] = tracked
    fake.script = [text("Fine.")]
    monkeypatch.setattr(ai_comment, "_ask", watching)
    assert render(reader_of(app, s, w), report).status_code == 200
    (_, held), = seen
    assert not any(held), "a session held a connection during the call"


# ── who may cause a call ─────────────────────────────────────────────────────
def test_a_read_only_token_causes_no_call_and_gets_what_is_kept(app, s, fake, model, fake_engine):
    w = _world(s)
    report = report_saved(s, w, report_of(w))
    client = reader_of(app, s, w)
    uid = _uid(s, "reader@x.local")
    numbers_say(fake_engine)
    token = token_of_level(s, uid, "read")
    headers = {"Authorization": f"Bearer {token}"}
    plain = TestClient(app)
    assert plain.post(f"/reports/{report}/render", json={"format": "pdf"}, headers=headers).status_code == 200
    assert fake.requests == []
    assert fake_engine.report_renders[-1]["comments"]["story"] == {"message": ai_comment.MESSAGES["not_asked"]["en"]}
    fake.script = [text("Kept for it.")]
    assert render(client, report).status_code == 200
    assert plain.post(f"/reports/{report}/render", json={"format": "pdf"}, headers=headers).status_code == 200
    assert len(fake.requests) == 1 and fake_engine.report_renders[-1]["comments"]["story"]["text"] == "Kept for it."


def test_a_token_that_may_write_pays_on_its_owner(app, s, fake, model, fake_engine):
    w = _world(s)
    report = report_saved(s, w, report_of(w))
    reader_of(app, s, w)
    uid = _uid(s, "reader@x.local")
    numbers_say(fake_engine)
    fake.script = [text("Written.")]
    token = token_of_level(s, uid, "write")
    assert TestClient(app).post(f"/reports/{report}/render", json={"format": "pdf"}, headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert spent(s)[0].user_id == uid


def test_an_observer_causes_no_call_and_the_document_goes_out(app, s, fake, model, fake_engine):
    w = _world(s)
    report = report_saved(s, w, report_of(w))
    numbers_say(fake_engine)
    assert render(reader_of(app, s, w, email="guest@x.local", is_observer=True), report).status_code == 200
    assert fake.requests == [] and "text" not in fake_engine.report_renders[0]["comments"]["story"]


@pytest.mark.parametrize("fmt, origin, scope, moment", [("pdf", "interface", None, "document"), ("svg", "interface", None, "open"), ("svg", "editor", None, "kept"),
                                                        ("pdf", "notebook", None, "kept"), ("pdf", "api", "read", "kept"), ("pdf", "api", "write", "document")])
def test_what_a_render_may_cause_goes_by_where_it_comes_from(fmt, origin, scope, moment):
    assert reports.comment_moment(fmt, origin, SimpleNamespace(state=SimpleNamespace(api_token_scope=scope))) == moment


# ── the designer ─────────────────────────────────────────────────────────────
def _try(client, d, report, item="story", **body):
    return client.post("/reports/preview/comment", json={"item": item, "definition": d, "report_id": report, **body}, headers=CSRF)


def _preview(client, d, report, **body):
    return client.post("/reports/preview", json={"definition": d, "report_id": report, "format": "svg", **body}, headers=CSRF)


def test_a_preview_makes_no_call_and_says_how_to_see_one(app, s, fake, model, fake_engine):
    w = _world(s)
    d = report_of(w)
    report = report_saved(s, w, d)
    numbers_say(fake_engine)
    r = _preview(reader_of(app, s, w, capability="edit"), d, report)
    assert r.status_code == 200, r.text
    assert fake.requests == []
    assert fake_engine.report_renders[0]["comments"]["story"] == {"message": ai_comment.MESSAGES["not_tried"]["en"]}


def test_try_it_makes_one_call_when_pressed_and_the_preview_then_shows_what_it_wrote(app, s, fake, model, fake_engine):
    w = _world(s)
    d = report_of(w)
    report = report_saved(s, w, d)
    numbers_say(fake_engine)
    fake.script = [text("Tried."), text("Again.")]
    client = reader_of(app, s, w, capability="edit")
    r = _try(client, d, report)
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["text"]) == ("ready", "Tried.")
    assert fake_engine.report_tiles[-1]["tiles"] == ["rev", "by", "story"]
    assert _try(client, d, report).json()["text"] == "Tried." and len(fake.requests) == 1
    assert _preview(client, d, report).status_code == 200
    assert fake_engine.report_renders[-1]["comments"]["story"]["text"] == "Tried." and len(fake.requests) == 1
    assert spent(s)[0].user_id == _uid(s, "reader@x.local")


def test_try_it_reads_the_definition_being_edited_not_the_saved_one(app, s, fake, model, fake_engine):
    w = _world(s)
    d = report_of(w)
    report = report_saved(s, w, d)
    numbers_say(fake_engine)
    fake.script = [text("Fine.")]
    edited = json.loads(json.dumps(d))
    edited["sections"]["body"]["items"][-1]["instruction"] = "Say it as edited."
    assert _try(reader_of(app, s, w, capability="edit"), edited, report).status_code == 200
    assert "Say it as edited." in sent(fake).message


def test_try_it_needs_edit_on_the_folder(app, s, fake, model, fake_engine):
    w = _world(s)
    d = report_of(w)
    report = report_saved(s, w, d)
    assert _try(reader_of(app, s, w), d, report).status_code == 403
    assert _try(_sign_in(app, s, "stranger@x.local"), d, report).status_code == 404
    assert fake.requests == [] and fake_engine.report_tiles == []


def test_try_it_on_a_definition_with_a_problem_lists_them_and_makes_no_call(app, s, fake, model, fake_engine):
    w = _world(s)
    d = report_of(w)
    report = report_saved(s, w, d)
    d["sections"]["body"]["items"][-1]["reads"] = ["nobody"]
    r = _try(reader_of(app, s, w, capability="edit"), d, report)
    assert r.status_code == 422 and r.json()["detail"]["problems"][0]["item"] == "story"
    assert fake.requests == []


def test_try_it_with_no_numbers_from_the_engine_says_it_is_not_available(app, s, fake, model, fake_engine):
    w = _world(s)
    d = report_of(w)
    report = report_saved(s, w, d)
    fake_engine.report_tiles_response = (500, {"detail": "boom"})
    r = _try(reader_of(app, s, w, capability="edit"), d, report)
    assert r.status_code == 200 and (r.json()["state"], r.json()["message"]) == ("failed", FAILED_EN) and fake.requests == []


def test_try_it_on_what_is_no_comment_is_refused(app, s, fake, model, fake_engine):
    w = _world(s)
    d = report_of(w)
    report = report_saved(s, w, d)
    assert _try(reader_of(app, s, w, capability="edit"), d, report, item="rev").status_code == 422
    assert fake.requests == []


def test_a_read_only_token_cannot_press_try_it(app, s, fake, model, fake_engine):
    w = _world(s)
    d = report_of(w)
    report = report_saved(s, w, d)
    reader_of(app, s, w, capability="edit")
    token = token_of_level(s, _uid(s, "reader@x.local"), "read")
    r = TestClient(app).post("/reports/preview/comment", json={"item": "story", "definition": d, "report_id": report}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403 and fake.requests == []


def test_a_comment_narrower_than_a_page_can_hold_is_refused_when_saved(app, s, fake_engine):
    w = _world(s)
    d = report_of(w)
    d["sections"]["body"]["items"][-1]["w"] = 30
    r = _sign_in(app, s, "boss@x.local", is_superuser=True).post(f"/projects/{w['sales']}/reports", json={"name": "Narrow", "content": d}, headers=CSRF)
    assert r.status_code == 422 and "40 mm" in r.text


# ── a schedule pays with its author's budget ─────────────────────────────────
def test_a_comment_added_or_changed_is_something_a_schedule_s_author_did_not_choose():
    d = {"sections": {"body": {"items": [{"id": "k", "type": "kpi"}]}}}
    added = {"sections": {"body": {"items": [{"id": "k", "type": "kpi"}, {"id": "c", "type": "comment", "instruction": "Say", "reads": ["k"]}]}}}
    changed = {"sections": {"body": {"items": [{"id": "k", "type": "kpi"}, {"id": "c", "type": "comment", "instruction": "Say more", "reads": ["k"]}]}}}
    moved = {"sections": {"body": {"items": [{"id": "k", "type": "kpi"}, {"id": "c", "type": "comment", "instruction": "Say", "reads": ["k"], "x": 9}]}}}
    assert reports.widens(d, added) and reports.widens(added, changed)
    assert not reports.widens(added, moved), "its place costs nobody anything"
    assert reports.widens({"tiles": []}, {"tiles": [{"id": "c", "type": "comment", "instruction": "Say", "reads": ["k"]}]})


# ── a dashboard's document ───────────────────────────────────────────────────
def board_render(client, board, fmt="pdf", **body):
    return client.post(f"/dashboards/{board}/render", json={"format": fmt, **body}, headers=CSRF)


def test_a_dashboard_without_the_element_sends_what_it_always_sent(app, s, fake, model, fake_engine):
    w = _world(s)
    d = _board_of(w)
    d["tiles"] = [t for t in d["tiles"] if t["type"] != "comment"]
    board = _saved(s, w, d)
    assert board_render(reader_of(app, s, w), board).status_code == 200
    assert fake_engine.report_tiles == [] and fake.requests == [] and "comments" not in fake_engine.dashboard_renders[0]


@pytest.mark.parametrize("fmt", ["pdf", "png"])
def test_a_dashboard_s_document_has_its_text_on_the_numbers_its_screen_reads(app, s, fake, model, fake_engine, fmt):
    w = _world(s)
    board = _saved(s, w, _board_of(w))
    fake.script = [text("Revenue is up.")]
    numbers_say(fake_engine)
    r = board_render(reader_of(app, s, w), board, fmt)
    assert r.status_code == 200, r.text
    (body,) = fake_engine.dashboard_renders
    assert body["comments"]["story"]["text"] == "Revenue is up."
    (asked,) = fake_engine.report_tiles
    assert asked["cache"] == body["cache"], "the same rows as the screen's"
    assert "1.234,50" in sent(fake).message
    assert json.loads(audited(s)[0].detail)["moment"] == "document"


def test_a_comment_on_what_the_reader_may_not_read_says_only_that_with_or_without_one_kept(app, s, fake, model, fake_engine):
    """«secret» reads the salaries, «story» only the orders. The text written for an administrator is never in
    the reader's document, and nothing there says why."""
    w = _world(s)
    board = _saved(s, w, _board_of(w))
    wages = {"type": "chart", "kind": "column", "computed_at": AT, "cached": True, "data": {"cats": ["G1"], "series": [{"name": "N", "texts": ["5"]}]}}
    fake.script = [text("A first text."), text("A second text.")]  # they are written at the same time, in either order
    numbers_say(fake_engine, wages=wages)
    assert board_render(_sign_in(app, s, "boss@x.local", is_superuser=True), board).status_code == 200
    kept = fake_engine.dashboard_renders[0]["comments"]
    on_wages, on_orders = kept["secret"]["text"], kept["story"]["text"]
    numbers_say(fake_engine, wages={"code": "no_access", "error": "You cannot read the dataset «pay»"})
    before = len(fake.requests)
    assert board_render(reader_of(app, s, w), board).status_code == 200
    said = fake_engine.dashboard_renders[-1]["comments"]
    assert said["secret"] == {"message": ai_comment.MESSAGES["no_access"]["en"]}
    assert on_wages not in json.dumps(said) and "pay" not in said["secret"]["message"]
    assert said["story"]["text"] == on_orders and len(fake.requests) == before


def test_a_document_with_clicks_comments_those_numbers_and_makes_its_call(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, _board_of(w))
    fake.script = [text("All of it."), text("The North alone.")]
    client = reader_of(app, s, w)
    numbers_say(fake_engine)
    board_render(client, board)
    numbers_say(fake_engine, revenue="300,00")
    click = {"tile": "by", "on": "rows", "values": ["Nord"]}
    assert board_render(client, board, clicks=[click]).status_code == 200
    assert fake_engine.report_tiles[-1]["clicks"] == [click]
    assert "300,00" in sent(fake).message and "1.234,50" not in sent(fake).message
    assert fake_engine.dashboard_renders[-1]["comments"]["story"]["text"] == "The North alone."


def test_a_provider_that_is_down_costs_the_dashboard_s_comment_and_not_its_document(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, _board_of(w))
    failed_with(fake, model)
    numbers_say(fake_engine)
    assert board_render(reader_of(app, s, w), board).status_code == 200
    assert fake_engine.dashboard_renders[0]["comments"]["story"] == {"message": FAILED_EN}


def test_numbers_the_engine_cannot_give_cost_the_dashboard_s_comment_and_not_its_document(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, _board_of(w))
    fake_engine.report_tiles_response = (500, {"detail": "boom"})
    assert board_render(reader_of(app, s, w), board).status_code == 200
    assert fake.requests == []
    assert fake_engine.dashboard_renders[0]["comments"]["story"] == {"message": FAILED_EN}


def test_a_read_only_token_on_a_dashboard_s_document_causes_no_call(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, _board_of(w))
    reader_of(app, s, w)
    numbers_say(fake_engine)
    token = token_of_level(s, _uid(s, "reader@x.local"), "read")
    r = TestClient(app).post(f"/dashboards/{board}/render", json={"format": "pdf"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and fake.requests == []


def test_in_metadata_only_mode_a_dashboard_s_document_reads_nothing_for_it(app, s, fake, model, fake_engine, monkeypatch):
    w = _world(s)
    board = _saved(s, w, _board_of(w))
    monkeypatch.setattr(metadata_only, "on", lambda *a: True)
    assert board_render(reader_of(app, s, w), board).status_code == 200
    assert fake.requests == [] and fake_engine.report_tiles == []
    assert fake_engine.dashboard_renders[0]["comments"]["story"] == {"message": ai_comment.MESSAGES["off"]["en"]}


def test_past_the_wait_every_comment_of_a_document_waits_together_not_in_turn(app, s, fake, model, fake_engine, monkeypatch):
    setting(monkeypatch, comment_wait_seconds=1)
    try:
        w = _world(s)
        d = _board_of(w)
        d["tiles"][-1]["reads"] = ["rev", "by"]  # two comments, no refusal in between
        d["tiles"][-1]["instruction"] = "Another."
        board = _saved(s, w, d)
        answering_late(fake, monkeypatch, 3)
        fake.script = [text("Late."), text("Late too.")]
        numbers_say(fake_engine)
        client = reader_of(app, s, w)
        started = time.monotonic()
        assert board_render(client, board).status_code == 200
        assert time.monotonic() - started < 2.5, "one wait for both"
        assert fake_engine.dashboard_renders[0]["comments"]["story"] == {"message": FAILED_EN}
    finally:
        get_settings.cache_clear()
