"""A dashboard as a PDF or an image, through the gateway (docs/specs/dashboards.md, «Going out»; the
document itself is the engine's: backend/tests/test_dashboard_render.py).

What these tests hold: the rights are the reader's, at every request, dataset by dataset: a dataset
they may not read goes to the engine only as its refusal, a click on its tile counts for nothing there,
and no VIEW is a 404; a token gives no more than its owner has; the clicks of an address are read as
the viewer writes them, and refused as the data refuses them; the image of a definition keeps its
pictures and its place on the grid; every render is audited, with what it showed.
"""
import json

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.models import AuditLog, User
from app.services import api_tokens, dashboards
from tests.conftest import make_permission
from tests.test_dashboards_clicks import bars
from tests.test_dashboards_data import (  # noqa: F401 — `app` is a fixture
    CSRF,
    PNG,
    _dataset,
    _reader,
    _saved,
    _tile,
    _uid,
    _world,
    app,
    definition,
)
from tests.test_engine_settings import _sign_in


def _bearer(token) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _token(session, email, scope="read") -> str:
    user = session.exec(select(User).where(User.email == email)).one()
    return api_tokens.create(session, user, "nightly", scope, 30, user)[1]


def _renders(session) -> list[AuditLog]:
    return list(session.exec(select(AuditLog).where(AuditLog.action == "dashboard.render").order_by(AuditLog.id)).all())


def _board(session, w, *tiles, extra=()) -> int:
    content = definition(w, *extra)
    content["tiles"] += list(tiles)
    return _saved(session, w, content)


def _start(client, board, **body):
    return client.post(f"/dashboards/{board}/render", json=body, headers=CSRF)


# ── what the engine receives ──────────────────────────────────────────────────
def test_a_render_goes_to_the_engine_with_the_tiles_on_their_grid_and_their_pictures(app, session, fake_engine):
    w = _world(session)
    board = _board(session, w, _tile("logo", "image", 10, asset=PNG, title="Logo"), bars(title="By region", y=20))
    r = _start(_reader(app, session, w), board, format="png")
    assert r.status_code == 200, r.text
    assert r.json() == {"task_id": "t-dashboard-1", "filename": "Board.png"}
    (body,) = fake_engine.dashboard_renders
    assert body["format"] == "png" and body["filename"] == "Board.png" and body["title"] == "Board" and body["locale"] == "it"
    items = {i["id"]: i for i in body["definition"]["sections"]["body"]["items"]}
    assert items["logo"]["asset"] == PNG  # the numbers go without their pictures: a document has them
    assert {t["id"]: t["grid"] for t in body["tiles"]}["bars"] == {"x": 0, "y": 20, "w": 6, "h": 6}
    assert {t["id"]: t["title"] for t in body["tiles"]}["logo"] == "Logo"
    assert list(body["datasets"]) == ["orders"] and body["refused"] == {}
    assert body["owner"] == f"user:{_uid(session, 'reader@x.local')}"
    assert body["cache"] == {"refresh_seconds": 0}  # the rows the screen reads, so the numbers it shows


def test_the_page_chosen_goes_with_it(app, session, fake_engine):
    w = _world(session)
    content = definition(w)
    content["page"] = {"size": "Letter", "orientation": "portrait"}
    _start(_reader(app, session, w), _saved(session, w, content), format="pdf")
    assert fake_engine.dashboard_renders[0]["page"] == {"size": "Letter", "orientation": "portrait"}


def test_a_page_that_is_none_is_refused_when_it_is_saved():
    with pytest.raises(dashboards.Problem) as e:
        dashboards.check({"v": 1, "page": {"size": "Napkin"}, "tiles": []})
    assert "The page's size" in e.value.message and e.value.where == "page.size"
    assert dashboards.check({"v": 1, "page": {"size": "A3", "orientation": "landscape"}, "tiles": []})["page"]["size"] == "A3"


@pytest.mark.parametrize("fmt", ["xlsx", "svg", "html", ""])
def test_only_a_pdf_or_an_image(app, session, fake_engine, fmt):
    w = _world(session)
    assert _start(_reader(app, session, w), _saved(session, w, definition(w)), format=fmt).status_code == 422
    assert fake_engine.dashboard_renders == []


def test_a_parameter_goes_converted_and_one_that_does_not_fit_stops_it_by_name(app, session, fake_engine):
    w = _world(session)
    content = definition(w, parameters=[{"name": "since", "type": "date"}])
    board = _saved(session, w, content)
    client = _reader(app, session, w)
    assert _start(client, board, params={"since": "2026-01-01"}).status_code == 200
    assert fake_engine.dashboard_renders[0]["params"] == {"since": "2026-01-01"}
    r = _start(client, board, params={"since": "not a date"})
    assert r.status_code == 400 and r.json()["detail"]["parameter"] == "since"
    assert len(fake_engine.dashboard_renders) == 1
    (_, refused) = _renders(session)
    assert refused.outcome == "failure" and "error" in json.loads(refused.detail)


# ── the rights ────────────────────────────────────────────────────────────────
def test_without_view_there_is_no_such_dashboard(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w))
    stranger = _sign_in(app, session, "stranger@x.local")
    assert _start(stranger, board).status_code == 404
    assert stranger.get(f"/dashboards/{board}/render").status_code == 404
    assert fake_engine.dashboard_renders == [] and _renders(session) == []


def test_a_dataset_the_reader_may_not_read_goes_only_as_its_refusal(app, session, fake_engine):
    """The engine is never told where it is: a person who has VIEW on the dashboard gets the tile that says no."""
    w = _world(session)
    board = _saved(session, w, definition(w, _dataset("pay", w["salaries"])))
    r = _start(_reader(app, session, w), board)
    assert r.status_code == 200, r.text
    (body,) = fake_engine.dashboard_renders
    assert list(body["datasets"]) == ["orders"] and body["refused"]["pay"]["code"] == "no_access"
    assert "datasets/s.parquet" not in json.dumps(body["datasets"])


def test_a_dataset_the_reader_may_read_goes_whole(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w, _dataset("pay", w["salaries"])))
    client = _reader(app, session, w)
    make_permission(session, user_id=_uid(session, "reader@x.local"), project_id=w["vault"], capability="view")
    _start(client, board)
    assert sorted(fake_engine.dashboard_renders[0]["datasets"]) == ["orders", "pay"]


def test_a_token_gives_no_more_than_its_owner_has(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w, _dataset("pay", w["salaries"])))
    _reader(app, session, w)
    r = TestClient(app).get(f"/dashboards/{board}/render", headers=_bearer(_token(session, "reader@x.local")))
    assert r.status_code == 200, r.text
    assert list(fake_engine.dashboard_renders[0]["datasets"]) == ["orders"]
    assert fake_engine.dashboard_renders[0]["refused"]["pay"]["code"] == "no_access"


def test_which_tokens_the_api_answers(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w))
    _reader(app, session, w)
    read, write = _token(session, "reader@x.local"), _token(session, "reader@x.local", "write")
    api = TestClient(app)
    url = f"/dashboards/{board}/render"
    assert api.get(url, headers=_bearer(read)).status_code == 200
    assert api.get(url, headers=_bearer(write)).status_code == 200
    # a read-only token starts a render as it starts a report's, and changes nothing
    assert api.post(url, json={}, headers=_bearer(read)).status_code == 200
    assert api.patch(f"/dashboards/{board}", json={"name": "x"}, headers=_bearer(read)).status_code == 403
    _sign_in(app, session, "stranger@x.local")
    assert api.get(url, headers=_bearer(_token(session, "stranger@x.local"))).status_code == 404
    assert api.get(url).status_code == 401


# ── the API ───────────────────────────────────────────────────────────────────
def test_the_api_waits_for_the_file_and_sends_it(app, session, fake_engine):
    w = _world(session)
    content = definition(w, parameters=[{"name": "region", "type": "text"}, {"name": "regions", "type": "text", "multi": True}])
    content["tiles"].append(bars())
    board = _saved(session, w, content)
    _reader(app, session, w)
    fake_engine.export_states["t-dashboard-1"] = [{"status": "running"}, {"status": "ready", "filename": "Board.png", "size": 15}]
    r = TestClient(app).get(f"/dashboards/{board}/render?format=png&region=North&regions=N&regions=S&click.bars=Nord-Est",
                            headers=_bearer(_token(session, "reader@x.local")))
    assert r.status_code == 200, r.text
    (body,) = fake_engine.dashboard_renders
    assert body["format"] == "png" and body["params"] == {"region": "North", "regions": ["N", "S"]}
    assert body["clicks"] == [{"tile": "bars", "on": "rows", "values": ["Nord-Est"]}]
    (entry,) = _renders(session)
    detail = json.loads(entry.detail)
    assert entry.outcome == "success" and detail["format"] == "png" and detail["from"] == "api" and detail["token"] == "nightly"
    assert detail["clicks"] == [{"tile": "bars", "on": "rows", "values": ["Nord-Est"]}]


def test_the_format_is_a_pdf_when_nothing_is_said(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w))
    _reader(app, session, w)
    TestClient(app).get(f"/dashboards/{board}/render", headers=_bearer(_token(session, "reader@x.local")))
    assert fake_engine.dashboard_renders[0]["format"] == "pdf"


@pytest.mark.parametrize("query, values", [
    ("click.bars=Nord-Est&click.bars=Sud", ["Nord-Est", "Sud"]),
    ("click.bars=%7Bnone%7D", [None]),
    ("click.bars=%5C%7Bnone%7D", ["{none}"]),  # a text that would read as something else
    ('click.split=%5B%22Nord%22%2C2023%5D', [["Nord", 2023]]),
    ("click.bars=2023", ["2023"]),  # an address has only texts: the engine finds the number
])
def test_the_clicks_of_an_address_are_read_as_the_viewer_writes_them(app, session, fake_engine, query, values):
    w = _world(session)
    board = _board(session, w, bars(), bars("split", y=30, split_by="[amount]"))
    _reader(app, session, w)
    r = TestClient(app).get(f"/dashboards/{board}/render?{query}", headers=_bearer(_token(session, "reader@x.local")))
    assert r.status_code == 200, r.text
    assert fake_engine.dashboard_renders[0]["clicks"][0]["values"] == values


def test_the_columns_of_a_matrix_are_named_in_the_address(app, session, fake_engine):
    from tests.test_dashboards_clicks import crossed

    w = _world(session)
    board = _board(session, w, crossed())
    _reader(app, session, w)
    r = TestClient(app).get(f"/dashboards/{board}/render?click.cross.columns=North", headers=_bearer(_token(session, "reader@x.local")))
    assert r.status_code == 200, r.text
    assert fake_engine.dashboard_renders[0]["clicks"] == [{"tile": "cross", "on": "columns", "values": ["North"]}]


@pytest.mark.parametrize("query, says", [("click.nope=1", "no tile «nope»"), ("click.mine=1", "cannot filter")])
def test_a_click_that_cannot_be_is_refused_as_for_the_numbers(app, session, fake_engine, query, says):
    w = _world(session)
    board = _board(session, w, bars())
    _reader(app, session, w)
    r = TestClient(app).get(f"/dashboards/{board}/render?{query}", headers=_bearer(_token(session, "reader@x.local")))
    assert r.status_code == 422 and says in r.json()["detail"]
    assert fake_engine.dashboard_renders == []


@pytest.mark.parametrize("clicks", [[{"tile": "bars", "values": ["x"], "expr": "1"}], [{"tile": "nope", "values": ["x"]}], [{"tile": "bars", "values": [{"expr": "1"}]}]])
def test_a_hostile_click_goes_nowhere(app, session, fake_engine, clicks):
    w = _world(session)
    r = _start(_reader(app, session, w), _board(session, w, bars()), clicks=clicks)
    assert r.status_code == 422 and fake_engine.dashboard_renders == []


def test_the_api_says_a_failed_render_and_stops_a_slow_one(app, session, fake_engine, monkeypatch):
    w = _world(session)
    board = _saved(session, w, definition(w))
    _reader(app, session, w)
    token = _token(session, "reader@x.local")
    api = TestClient(app)
    fake_engine.export_states["t-dashboard-1"] = [{"status": "failed", "code": 422, "detail": "the dashboard would have 512 pages"}]
    r = api.get(f"/dashboards/{board}/render", headers=_bearer(token))
    assert r.status_code == 422 and "512 pages" in r.json()["detail"]
    monkeypatch.setattr(get_settings().reports, "api_wait_seconds", 0.3)
    fake_engine.export_states["t-dashboard-2"] = [{"status": "running"}]
    r = api.get(f"/dashboards/{board}/render", headers=_bearer(token))
    assert r.status_code == 504 and "REPORTS__API_WAIT_SECONDS" in r.json()["detail"]
    assert fake_engine.revoked == ["t-dashboard-2"]
    assert [e.outcome for e in _renders(session)] == ["failure", "failure"]


# ── the audit ─────────────────────────────────────────────────────────────────
def test_a_render_is_audited_with_what_it_showed(app, session, fake_engine):
    w = _world(session)
    board = _board(session, w, bars())
    _start(_reader(app, session, w), board, format="pdf", clicks=[{"tile": "bars", "values": ["Nord-Est"]}])
    (entry,) = _renders(session)
    detail = json.loads(entry.detail)
    assert (entry.outcome, entry.target_id, entry.target_label, entry.target_type) == ("success", board, "Board", "dashboard")
    assert detail["format"] == "pdf" and detail["from"] == "interface" and detail["clicks"][0]["tile"] == "bars"


def test_a_render_the_engine_refuses_is_audited_as_failed(app, session, fake_engine):
    w = _world(session)
    fake_engine.dashboard_render_response = (503, {"detail": "no"})
    r = _start(_reader(app, session, w), _saved(session, w, definition(w)))
    assert r.status_code >= 500
    (entry,) = _renders(session)
    assert entry.outcome == "failure"
