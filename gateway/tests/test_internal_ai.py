"""The AI service's internal API for the AI comments of a Pushmail cell (routes/internal_ai.py,
docs/specs/gateway-go.md «The internal API»): behind the internal token, `reports.comments_for`
for one document, as `flow_group_runner._cell_comments` calls it."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.crypto import INTERNAL_HEADER, internal_token
from app.models import User
from app.routes import internal_ai
from app.services import reports


@pytest.fixture
def client(db_engine, monkeypatch):
    monkeypatch.setattr(internal_ai, "engine", db_engine)
    app = FastAPI()
    app.include_router(internal_ai.router)
    return TestClient(app)


def _body(user_id, **more):
    return {"user_id": user_id, "definition": {"v": 1}, "datasets": {}, "params": {}, "given": {}, **more}


def test_without_the_token_the_route_does_not_exist(client):
    assert client.post("/internal/ai/comments", json=_body(1)).status_code == 404
    assert client.post("/internal/ai/comments", json=_body(1), headers={INTERNAL_HEADER: "0" * 64}).status_code == 404
    assert client.post("/internal/ai/comments", json={"nothing": True}).status_code == 404


def test_a_bad_body_or_no_such_user_is_a_422(client):
    headers = {INTERNAL_HEADER: internal_token()}
    assert client.post("/internal/ai/comments", json={"user_id": 1}, headers=headers).status_code == 422
    r = client.post("/internal/ai/comments", json=_body(999), headers=headers)
    assert r.status_code == 422 and r.json() == {"detail": "No such user"}


def test_it_answers_what_comments_for_says(client, db_engine, monkeypatch):
    from sqlmodel import Session

    with Session(db_engine) as s:
        u = User(email="a@x.test", hashed_password="x", is_active=True)
        s.add(u)
        s.commit()
        uid = u.id
    seen = {}

    async def comments_for(session, user, definition, datasets, params, given, **kw):
        seen.update(user=user.id, definition=definition, datasets=datasets, params=params, given=given, **kw)
        return {"ai": {"state": "no_provider"}}, "f" * 32

    monkeypatch.setattr(reports, "comments_for", comments_for)
    r = client.post("/internal/ai/comments", headers={INTERNAL_HEADER: internal_token()}, json=_body(
        uid, given={"region": ["Sud"]}, where=["dashboard", 4], title="Sales", of_report=False, words_of={"tiles": []}))
    assert r.status_code == 200 and r.json() == {"comments": {"ai": {"state": "no_provider"}}, "reading": "f" * 32}
    assert seen == {"user": uid, "definition": {"v": 1}, "datasets": {}, "params": {}, "given": {"region": ["Sud"]},
                    "where": ("dashboard", 4), "title": "Sales", "of_report": False, "words_of": {"tiles": []}}


def test_a_definition_without_a_comment_says_nothing(client, db_engine):
    from sqlmodel import Session

    with Session(db_engine) as s:
        u = User(email="b@x.test", hashed_password="x", is_active=True)
        s.add(u)
        s.commit()
        uid = u.id
    r = client.post("/internal/ai/comments", headers={INTERNAL_HEADER: internal_token()}, json=_body(uid))
    assert r.status_code == 200 and r.json() == {"comments": {}, "reading": None}
