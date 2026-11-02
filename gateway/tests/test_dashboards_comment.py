"""An AI comment through a dashboard's route (docs/specs/ai-comment.md), with the fake engine and the fake
provider over real HTTP: no test here reaches a real model, and every request is recorded. What the comment
itself does is in test_ai_comment.py.

What these tests hold: the route reads the same numbers as the screen, from the same place in the cache, and
only those of the elements read; the browser chooses neither what is read nor the instruction; rights are the
reader's at every request, and a reader who may not read one element learns nothing, with a comment kept and
with none; a click on what the reader may not read changes nothing; a read-only token spends nothing; 100
readers make one call; a screen refreshing every minute makes one call an interval; and what is being edited
needs EDIT and makes a call only when it is asked for.
"""
import asyncio
import json

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.core.config import get_settings
from app.db.session import get_session
from app.models import AiComment, AiCommentThread, AiSpend, AuditLog
from app.routes import ai as ai_routes
from app.routes import auth as auth_routes
from app.routes import dashboards as routes
from app.services import ai_comment, api_tokens, metadata_only
from tests.conftest import make_permission, make_user, token_of
from tests.fake_anthropic import text
from tests.test_ai_comment import add_provider, clock, db, fake, model, sent, setting  # noqa: F401 — some are fixtures
from tests.test_dashboards_data import _dataset, _saved, _uid, _world
from tests.test_engine_settings import CSRF, _sign_in

AT = "2026-11-01T10:00:00+00:00"


@pytest.fixture
def app(db, fake_engine):
    app = FastAPI()
    for r in (auth_routes.router, routes.router, ai_routes.router):
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


def _at(x, y, w=4, h=4) -> dict:
    return {"x": x, "y": y, "w": w, "h": h}


def board_of(w, **story) -> dict:
    """Orders, which everyone here may read, and salaries, which only an administrator may: «story» reads the
    first, «secret» reads both."""
    return {
        "v": 1, "locale": "en", "timezone": "UTC", "refresh_seconds": 60, "parameters": [],
        "datasets": [_dataset("orders", w["orders"]), _dataset("pay", w["salaries"])],
        "tiles": [
            {"id": "rev", "type": "kpi", "dataset": "orders", "label": "Revenue", "value": "SUM([amount])", "grid": _at(0, 0, 3, 3)},
            {"id": "by", "type": "chart", "kind": "column", "dataset": "orders", "title": "By region", "category": "[region]",
             "series": [{"value": "SUM([amount])"}], "grid": _at(3, 0)},
            {"id": "wages", "type": "chart", "kind": "column", "dataset": "pay", "title": "Wages", "category": "[pay_grade]",
             "series": [{"value": "COUNT()"}], "grid": _at(7, 0)},
            {"id": "story", "type": "comment", "title": "What changed", "instruction": "Say what moved most.", "reads": ["rev", "by"],
             "grid": _at(0, 5, 6, 4), **story},
            {"id": "secret", "type": "comment", "instruction": "Compare them.", "reads": ["rev", "wages"], "grid": _at(6, 5, 6, 4)},
        ],
    }


def numbers_say(fake_engine, revenue="1.234,50", **more) -> None:
    """What the engine answers from now on, as the screen writes it."""
    fake_engine.report_tiles_response = (200, {"tiles": {
        "rev": {"type": "kpi", "label": "Revenue", "value": 1234.5, "text": revenue, "computed_at": AT, "cached": True},
        "by": {"type": "chart", "kind": "column", "computed_at": AT, "cached": True, "data": {"cats": ["Nord"], "series": [{"name": "Sum", "texts": ["10"]}]}},
        **more}, "truncated": {}})


def _ask(client, board, tile="story", **body):
    return client.post(f"/dashboards/{board}/comment", json={"tile": tile, **body}, headers=CSRF)


def _preview(client, definition, board, tile="story", **body):
    return client.post("/dashboards/preview/comment", json={"tile": tile, "definition": definition, "dashboard_id": board, **body}, headers=CSRF)


def _reader(app, s, w, email="reader@x.local", capability="view", **kw) -> TestClient:
    client = _sign_in(app, s, email, **kw)
    make_permission(s, user_id=_uid(s, email), project_id=w["sales"], capability=capability)
    return client


def _kept(db) -> list[AiComment]:
    with Session(db) as session:
        return list(session.exec(select(AiComment)).all())


# ── what is read, and how ────────────────────────────────────────────────────
def test_the_route_reads_only_what_the_comment_reads_as_the_screen_reads_it(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    fake.script = [text("Revenue is up.")]
    reader = _reader(app, s, w)
    numbers_say(fake_engine)

    on_screen = reader.post(f"/dashboards/{board}/data", json={"tiles": ["rev", "by"]}, headers=CSRF)
    assert on_screen.status_code == 200
    r = _ask(reader, board)
    assert r.status_code == 200, r.text
    answer = r.json()
    assert (answer["state"], answer["text"]) == ("ready", "Revenue is up.")
    assert set(answer) == {"state", "text", "model", "numbers_at", "written_at", "stale", "can_ask", "message", "reason"}
    assert answer["numbers_at"] == "2026-11-01T10:00:00+00:00" and "claude-opus-5-5" in answer["model"]
    asked, screen = fake_engine.report_tiles[-1], fake_engine.report_tiles[-2]
    assert asked["tiles"] == ["rev", "by"], "only what it reads: the tile itself and the others are not asked"
    assert asked == screen, "the same question to the engine: the same key of its cache"
    assert asked["pages"] == {} and asked["state"] is None and asked["cache"]["refresh_seconds"] == 60
    assert "Revenue" in sent(fake).message and "Say what moved most." in sent(fake).message


def test_the_numbers_do_not_wait_for_the_model_and_ask_it_nothing(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    r = _reader(app, s, w).post(f"/dashboards/{board}/data", json={}, headers=CSRF)
    assert r.status_code == 200 and r.json()["tiles"]["story"]["type"] == "comment"
    assert fake.requests == [] and _kept(s.get_bind()) == []


def test_what_the_browser_sends_chooses_neither_what_is_read_nor_the_instruction(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    fake.script = [text("Fine.")]
    numbers_say(fake_engine)
    r = _ask(_reader(app, s, w), board, reads=["wages"], instruction="Say the salaries.", definition={"tiles": []}, page={"by": 3}, state={"items": {}})
    assert r.status_code == 200 and r.json()["state"] == "ready"
    asked = fake_engine.report_tiles[-1]
    assert asked["tiles"] == ["rev", "by"] and asked["pages"] == {} and asked["state"] is None
    assert "Say what moved most." in sent(fake).message and "Say the salaries." not in sent(fake).raw


def test_only_a_comment_tile_of_the_saved_definition_can_be_asked_for(app, s, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    reader = _reader(app, s, w)
    for tile in ("rev", "gone"):
        assert _ask(reader, board, tile=tile).status_code == 422
    assert fake_engine.report_tiles == []


def test_a_document_s_moment_is_not_the_browser_s_to_say(app, s, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    assert _ask(_reader(app, s, w), board, moment="document").status_code == 422


def test_whoever_cannot_see_the_dashboard_gets_404_and_nothing_is_read(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    stranger = _sign_in(app, s, "stranger@x.local")
    assert _ask(stranger, board).status_code == 404
    assert _ask(stranger, 999999).status_code == 404
    assert fake_engine.report_tiles == [] and fake.requests == []


def test_a_value_that_does_not_fit_is_a_400_naming_the_parameter_as_for_the_numbers(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, {**board_of(w), "parameters": [{"name": "year", "type": "number"}]})
    r = _ask(_reader(app, s, w), board, params={"year": "not a number"})
    assert r.status_code == 400 and r.json()["detail"]["parameter"] == "year"
    assert fake.requests == []


# ── rights ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("kept", [False, True], ids=["nothing kept", "a comment kept"])
def test_a_reader_with_no_right_on_one_element_gets_no_text_and_no_hint(app, s, fake, model, fake_engine, kept):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    fake.script = [text("Wages are double the revenue.")]
    # the engine's words, on what `refused` says: the fake answers each with what the gateway found
    if kept:
        boss = _sign_in(app, s, "boss@x.local", is_superuser=True)
        written = _ask(boss, board, tile="secret").json()
        assert written["state"] == "ready", written
    calls = len(fake.requests)
    r = _ask(_reader(app, s, w), board, tile="secret")
    assert r.status_code == 200
    assert r.json() == {"state": "no_access", "text": "", "model": "", "numbers_at": None, "written_at": None, "stale": False, "can_ask": False,
                        "message": ai_comment.MESSAGES["no_access"]["en"], "reason": ""}
    assert len(fake.requests) == calls, "no call for who may not read it"


def test_a_click_on_a_tile_the_reader_may_not_read_changes_nothing(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    fake.script = [text("Up.")]
    numbers_say(fake_engine)
    reader = _reader(app, s, w)
    # nothing kept: as an opening with no click, it writes
    before_click = _ask(reader, board, clicks=[{"tile": "wages", "values": [3]}]).json()
    assert before_click["state"] == "ready" and not before_click["can_ask"]
    assert len(fake.requests) == 1
    # and with the text kept, the same text, with no second call
    after_click = _ask(reader, board, clicks=[{"tile": "wages", "values": [4]}]).json()
    assert after_click["text"] == "Up." and len(fake.requests) == 1
    assert _ask(reader, board).json()["text"] == "Up." and len(fake.requests) == 1


def test_a_click_that_cannot_be_is_refused_as_for_the_numbers(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    reader = _reader(app, s, w)
    assert _ask(reader, board, clicks=[{"tile": "rev", "values": ["x"]}]).status_code == 422
    assert _ask(reader, board, clicks=[{"tile": "by", "values": ["x"], "expression": "1=1"}]).status_code == 422
    assert fake.requests == []


def test_only_who_may_do_something_about_it_is_told_what_failed(app, s, db, fake, fake_engine):
    add_provider(db, fake.url, key="a wrong key")
    w = _world(s)
    board = _saved(s, w, board_of(w))
    numbers_say(fake_engine)
    reader = _ask(_reader(app, s, w), board).json()
    assert reader["state"] == "failed" and reader["reason"] == ""
    boss = _ask(_sign_in(app, s, "boss@x.local", is_superuser=True), board).json()
    assert boss["state"] == "failed" and boss["reason"] == "provider_key"


def test_an_observer_reads_what_is_kept_and_causes_no_call(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    numbers_say(fake_engine)
    guest = _reader(app, s, w, email="guest@x.local", is_observer=True)
    for moment in ("open", "refresh", "asked"):
        assert _ask(guest, board, moment=moment).json()["state"] == "not_asked"
    assert fake.requests == []
    fake.script = [text("Written by someone else.")]
    assert _ask(_reader(app, s, w), board).json()["state"] == "ready"
    assert _ask(guest, board).json()["text"] == "Written by someone else."
    assert len(fake.requests) == 1


# ── a token ──────────────────────────────────────────────────────────────────
def _bearer(token) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_a_read_only_token_causes_no_call_and_a_token_that_may_write_pays_on_its_owner(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    numbers_say(fake_engine)
    owner = make_user(s, email="script@x.local")
    make_permission(s, user_id=owner.id, project_id=w["sales"], capability="view")
    _, read = api_tokens.create(s, owner, "nightly", "read", 30, owner)
    _, write = api_tokens.create(s, owner, "writer", "write", 30, owner)
    api = TestClient(app)
    fake.script = [text("By a script.")]
    refused = api.post(f"/dashboards/{board}/comment", json={"tile": "story"}, headers=_bearer(read))
    assert refused.status_code == 403 and fake.requests == []
    assert "/dashboards/{dashboard_id}/comment" not in api_tokens.READING_POSTS
    # its numbers it still reads
    assert api.post(f"/dashboards/{board}/data", json={}, headers=_bearer(read)).status_code == 200
    assert api.post("/dashboards/preview/comment", json={"tile": "story", "definition": board_of(w), "dashboard_id": board}, headers=_bearer(read)).status_code == 403
    r = api.post(f"/dashboards/{board}/comment", json={"tile": "story"}, headers=_bearer(write))
    assert r.status_code == 200 and r.json()["text"] == "By a script."
    with Session(s.get_bind()) as session:
        assert [row.user_id for row in session.exec(select(AiSpend)).all()] == [owner.id]
        audited = session.exec(select(AuditLog).where(AuditLog.action == "ai.comment")).one()
    assert audited.target_type == "dashboard" and audited.target_id == board and audited.target_label == "story"
    assert "By a script" not in audited.detail and "1.234" not in audited.detail


# ── a click ──────────────────────────────────────────────────────────────────
def test_a_click_makes_no_call_until_it_is_asked_for_and_going_back_brings_the_text_back(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    fake.script = [text("Whole year."), text("Only the North.")]
    reader = _reader(app, s, w)
    numbers_say(fake_engine)
    assert _ask(reader, board).json()["text"] == "Whole year."

    click = [{"tile": "by", "values": ["Nord"]}]
    numbers_say(fake_engine, revenue="99,00")  # what the click filters
    r = _ask(reader, board, clicks=click).json()
    assert (r["state"], r["text"], r["can_ask"]) == ("not_asked", "", True)
    assert _ask(reader, board, clicks=click, moment="refresh").json()["can_ask"] is True
    assert len(fake.requests) == 1

    r = _ask(reader, board, clicks=click, moment="asked").json()
    assert (r["state"], r["text"]) == ("ready", "Only the North.")
    assert len(fake.requests) == 2

    # Back, Clear all: the numbers it wrote on, what it wrote
    numbers_say(fake_engine)
    assert _ask(reader, board).json()["text"] == "Whole year."
    # and the click again: its own text, with no new call
    numbers_say(fake_engine, revenue="99,00")
    assert _ask(reader, board, clicks=click).json()["text"] == "Only the North."
    assert len(fake.requests) == 2


def test_what_a_click_chose_goes_to_the_model_as_the_tile_says_it(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    fake.script = [text("North.")]
    numbers_say(fake_engine, by={"type": "chart", "kind": "column", "computed_at": AT, "cached": True, "chosen": {"rows": ["Nord"]},
                                 "data": {"cats": ["Nord"], "series": [{"name": "Sum", "texts": ["10"]}]}})
    assert _ask(_reader(app, s, w), board, clicks=[{"tile": "by", "values": ["Nord"]}], moment="asked").json()["state"] == "ready"
    assert '"selected":{"rows":["Nord"]}' in sent(fake).message


# ── how much it holds ────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["data", "comment"])
def test_no_database_connection_is_held_while_the_engine_answers(app, s, db, fake, model, fake_engine, monkeypatch, path):
    """A hundred readers at once, 33 on each gateway: with 22 of them holding a connection while the engine
    answers, the 23rd waits for one on the event loop, which those 22 need in order to finish. Seen on the 8088
    stack: 30 seconds of stall per request, gateways unhealthy. routes/proxy.py says why."""
    from app.core import engine_client

    w = _world(s)
    content = board_of(w)
    # what makes the gateway ask the engine before the numbers: a catalog's SQL, a placeholder, a list of choices
    content["datasets"].append({"name": "sql", "source": {"kind": "sql", "sql": 'SELECT * FROM "Orders"', "connection_id": None, "filters": [{"column": "day", "op": ">=", "value": "{today-30}"}]}})
    content["parameters"] = [{"name": "region", "type": "text", "choices": {"dataset": "orders", "value": "[region]"}}]
    board = _saved(s, w, content)
    fake.script = [text("Fine.")]
    reader = _reader(app, s, w)
    numbers_say(fake_engine)
    live: list[Session] = []

    def one_session():
        with Session(db) as session:
            live.append(session)
            yield session

    app.dependency_overrides[get_session] = one_session
    held: list[str] = []

    async def look(request):
        held.extend(request.url.path for session in live if session.in_transaction())

    engine_client._client.event_hooks["request"].append(look)
    asked_for = {"data": {"tiles": ["rev"]}, "comment": {"tile": "story"}}[path]
    r = reader.post(f"/dashboards/{board}/{path}", json={"params": {"region": "Nord"}, **asked_for}, headers=CSRF)
    assert r.status_code == 200, r.text
    assert held == []
    assert len(fake_engine.report_tiles) == 1


# ── how much it costs ────────────────────────────────────────────────────────
@pytest.mark.anyio
async def test_100_readers_through_the_route_make_one_call(app, s, db, fake, model, fake_engine, monkeypatch):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    numbers_say(fake_engine)
    people = []
    for i in range(5):
        user = make_user(s, email=f"r{i}@x.local")
        make_permission(s, user_id=user.id, project_id=w["sales"], capability="view")
        people.append(token_of(s, user))
    real = fake._answer

    def slowly(body):
        import threading
        threading.Event().wait(0.3)
        return real(body)

    monkeypatch.setattr(fake, "_answer", slowly)
    fake.script = [text("For all.")]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://gateway", timeout=120) as client:
        answers = await asyncio.gather(*(client.post(f"/dashboards/{board}/comment", json={"tile": "story"}, headers=_bearer(people[i % 5])) for i in range(100)))
    assert {a.status_code for a in answers} == {200}
    assert {a.json()["text"] for a in answers} == {"For all."}
    assert len(fake.requests) == 1
    with Session(db) as session:
        assert len(session.exec(select(AiSpend)).all()) == 1
        assert len(session.exec(select(AuditLog).where(AuditLog.action == "ai.comment")).all()) == 1


def _a_wall(app, s, clock, fake, fake_engine, ticks, every=60):
    """A screen that refreshes, with new numbers at every tick: how many calls it made."""
    w = _world(s)
    board = _saved(s, w, board_of(w))
    numbers_say(fake_engine, revenue="0")
    fake.script = [text(f"Comment {i}.") for i in range(400)]
    reader = _reader(app, s, w)
    assert _ask(reader, board).json()["text"] == "Comment 0."
    seen = []
    for tick in range(1, ticks + 1):
        clock.after(every)
        numbers_say(fake_engine, revenue=str(tick))
        seen.append(_ask(reader, board, moment="refresh").json())
    return seen


def test_a_screen_refreshing_every_minute_makes_one_call_an_interval(app, s, clock, fake, model, fake_engine):
    seen = _a_wall(app, s, clock, fake, fake_engine, ticks=125)
    assert len(fake.requests) == 3, "the opening, then at 60 and at 120 minutes"
    assert [a["state"] for a in seen] == ["ready"] * 125, "the old text stays: nothing goes blank"
    assert seen[0]["stale"] and seen[0]["text"] == "Comment 0." and seen[0]["numbers_at"] == "2026-11-01T10:00:00+00:00"
    assert not seen[59]["stale"] and seen[59]["text"] == "Comment 1."


def test_with_a_shorter_interval_the_screen_follows_it_and_never_more(app, s, clock, fake, model, fake_engine, monkeypatch):
    setting(monkeypatch, comment_min_seconds=300)
    try:
        _a_wall(app, s, clock, fake, fake_engine, ticks=60)
    finally:
        monkeypatch.delenv("AI__COMMENT_MIN_SECONDS")
        get_settings.cache_clear()
    assert len(fake.requests) == 1 + 12


def test_a_refresh_of_the_same_numbers_makes_no_call_and_says_the_same(app, s, clock, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    numbers_say(fake_engine)
    fake.script = [text("Same.")]
    reader = _reader(app, s, w)
    first = _ask(reader, board).json()
    for _ in range(10):
        clock.after(3600)
        again = _ask(reader, board, moment="refresh").json()
        assert again["text"] == first["text"] and not again["stale"]
    assert len(fake.requests) == 1


def test_another_value_of_a_parameter_gets_no_old_text(app, s, clock, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, {**board_of(w), "parameters": [{"name": "region", "type": "text"}]})
    numbers_say(fake_engine)
    fake.script = [text("North.")]
    reader = _reader(app, s, w)
    assert _ask(reader, board, params={"region": "North"}).json()["text"] == "North."
    clock.after(60)
    numbers_say(fake_engine, revenue="7")
    r = _ask(reader, board, params={"region": "South"}, moment="refresh").json()
    assert r["text"] != "North."


# ── metadata-only, no provider ───────────────────────────────────────────────
def test_in_metadata_only_mode_nothing_is_read_nor_sent(app, s, fake, model, fake_engine, monkeypatch):
    monkeypatch.setattr(metadata_only, "on", lambda where: where == "chat")
    w = _world(s)
    board = _saved(s, w, board_of(w))
    fake.script = [text("Never.")]
    reader = _reader(app, s, w)
    for moment in ("open", "refresh", "asked"):
        r = _ask(reader, board, moment=moment).json()
        assert (r["state"], r["text"]) == ("off", "")
    assert fake.requests == [] and fake_engine.report_tiles == []
    assert _preview(_sign_in(app, s, "boss@x.local", is_superuser=True), board_of(w), board).json()["state"] == "off"
    assert fake.requests == []


def test_with_no_provider_only_who_may_add_one_is_told(app, s, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    numbers_say(fake_engine)
    r = _ask(_reader(app, s, w), board).json()
    assert (r["state"], r["message"]) == ("no_provider", "")
    boss = _ask(_sign_in(app, s, "boss@x.local", is_superuser=True), board).json()
    assert boss["state"] == "no_provider" and "add a provider" in boss["message"]


def test_the_status_says_which_model_would_write_or_that_none_would(app, s, db, fake, fake_engine, monkeypatch):
    reader = _sign_in(app, s, "reader@x.local")
    assert reader.get("/ai/status").json()["comment_model"] is None
    add_provider(db, fake.url)
    assert "claude-opus-5-5" in reader.get("/ai/status").json()["comment_model"]
    setting(monkeypatch, comment_model="no-such-model")
    try:
        assert reader.get("/ai/status").json()["comment_model"] is None, "the numbers go where the administrator said, or nowhere"
    finally:
        monkeypatch.delenv("AI__COMMENT_MODEL")
        get_settings.cache_clear()


# ── the designer: Try it ─────────────────────────────────────────────────────
def test_try_it_reads_the_definition_being_edited_with_edit_and_saves_nothing(app, s, db, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    numbers_say(fake_engine)
    fake.script = [text("A try.")]
    author = _reader(app, s, w, email="author@x.local", capability="edit")
    edited = board_of(w, instruction="An instruction not saved yet.")
    r = _preview(author, edited, board, tab="t1")
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["text"]) == ("ready", "A try.")
    assert "An instruction not saved yet." in sent(fake).message
    asked = fake_engine.report_tiles[-1]
    assert asked["tiles"] == ["rev", "by"] and "cache" not in asked and asked["slot"], "what is being edited shares nothing"
    with Session(db) as session:
        assert session.exec(select(AiCommentThread)).all() == [], "no interval to hold: nothing refreshes it"
        audited = session.exec(select(AuditLog).where(AuditLog.action == "ai.comment")).one()
    assert audited.target_id is None and json.loads(audited.detail)["moment"] == "asked"


def test_try_it_twice_on_the_same_numbers_pays_once_and_another_instruction_pays_again(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    numbers_say(fake_engine)
    fake.script = [text("First."), text("Second.")]
    author = _reader(app, s, w, email="author@x.local", capability="edit")
    assert _preview(author, board_of(w), board).json()["text"] == "First."
    assert _preview(author, board_of(w), board).json()["text"] == "First."
    assert len(fake.requests) == 1
    assert _preview(author, board_of(w, instruction="Other words."), board).json()["text"] == "Second."
    assert len(fake.requests) == 2


def test_try_it_needs_edit_and_who_only_reads_is_refused(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    assert _preview(_reader(app, s, w), board_of(w), board).status_code == 403
    assert _preview(_sign_in(app, s, "stranger@x.local"), board_of(w), board).status_code == 404
    assert fake.requests == [] and fake_engine.report_tiles == []


def test_try_it_on_a_definition_with_a_mistake_lists_it_and_makes_no_call(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    author = _reader(app, s, w, email="author@x.local", capability="edit")
    r = _preview(author, board_of(w, reads=["gone"]), board)
    assert r.status_code == 422 and fake.requests == []
    assert "gone" in r.text


def test_try_it_goes_with_the_reader_s_rights_too(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    author = _reader(app, s, w, email="author@x.local", capability="edit")
    r = _preview(author, board_of(w), board, tile="secret")
    assert r.status_code == 200 and r.json()["state"] == "no_access" and fake.requests == []


def test_a_comment_hidden_or_reading_only_rows_makes_no_call_through_the_route(app, s, fake, model, fake_engine):
    w = _world(s)
    board = _saved(s, w, board_of(w))
    fake_engine.report_tiles_response = (200, {"tiles": {
        "rev": {"type": "kpi", "hidden": True, "computed_at": AT, "cached": True},
        "by": {"type": "chart", "kind": "scatter", "computed_at": AT, "cached": True, "data": {"series": [{"name": "s", "points": [[1, 2, "a"]]}], "aggregated": False}},
    }, "truncated": {}})
    assert _ask(_reader(app, s, w), board).json()["state"] == "not_asked"
    assert fake.requests == []
