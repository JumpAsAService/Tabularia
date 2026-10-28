"""The Reports tab (routes/reports.py, GET /reports/search) and who opens the reports
(services/report_views.py).

The rules these tests hold: an opening in the viewer counts a person once a day per
report, a token's counts nothing, and whoever may not see a report cannot count it; the
tab lists only the reports one sees, those opened by the most people in the last 30 days
first (then the most openings, then the most ever, then the latest changed), by name or
by the latest change on request, paginated, with a search over name, description and
folders; the housekeeping drops the openings past the window and the totals stay; the
openings go with their report and with their person.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db.session import get_session
from app.models import Report, ReportView, User
from app.routes import reports as routes
from app.services import api_tokens, housekeeping, report_views
from tests.conftest import make_permission, make_project, make_user
from tests.test_engine_settings import CSRF, _sign_in

NOW = datetime(2026, 10, 28, 9, 0, tzinfo=timezone.utc)


@pytest.fixture
def app(session, fake_engine):
    from app.routes import auth as auth_routes

    app = FastAPI()
    for r in (auth_routes.router, routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _report(session, name, project_id, description="", owner_id=None, updated_days_ago=0) -> Report:
    at = datetime.now(timezone.utc) - timedelta(days=updated_days_ago)
    r = Report(name=name, description=description, project_id=project_id, owner_id=owner_id, created_at=at,
               updated_at=at)
    session.add(r)
    session.commit()
    session.refresh(r)
    return r


def _people(session, n) -> list[User]:
    return [make_user(session, email=f"reader{i}@x.local") for i in range(n)]


def _uid(session, email) -> int:
    return session.exec(select(User.id).where(User.email == email)).one()


def _now_of(session, report_id) -> tuple[int, object]:
    """The report's total and latest opening, read again (a request lets go of what it loaded)."""
    session.expire_all()
    r = session.exec(select(Report).where(Report.id == report_id)).one()
    return r.views_total, r.last_viewed_at


def _names(page) -> list[str]:
    return [x["name"] for x in page["items"]]


# ── an opening ────────────────────────────────────────────────────────────────
def test_a_person_counts_once_a_day(session):
    p = make_project(session, name="Sales").id
    r = _report(session, "Monthly", p)
    alice = make_user(session, email="alice@x.local")
    assert report_views.record(session, r, alice, at=NOW)
    assert not report_views.record(session, r, alice, at=NOW + timedelta(hours=3))  # the same day
    assert report_views.record(session, r, alice, at=NOW + timedelta(days=1))
    session.refresh(r)
    assert r.views_total == 2
    assert r.last_viewed_at.replace(tzinfo=timezone.utc) == NOW + timedelta(days=1)
    assert len(session.exec(select(ReportView)).all()) == 2


def test_the_day_is_the_installations(session, monkeypatch):
    """23:30 and 00:30 in Rome are two days, though both are the same day in UTC."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings().app, "timezone", "Europe/Rome")
    p = make_project(session, name="Sales").id
    r = _report(session, "Monthly", p)
    alice = make_user(session, email="alice@x.local")
    late = datetime(2026, 10, 28, 21, 30, tzinfo=timezone.utc)  # 22:30 in Rome (CET)
    assert report_views.record(session, r, alice, at=late)
    assert report_views.record(session, r, alice, at=late + timedelta(hours=2))  # 00:30, the 29th
    assert report_views.today(late) != report_views.today(late + timedelta(hours=2))


def test_the_viewer_counts_its_openings_and_only_who_sees_the_report(app, session):
    sales = make_project(session, name="Sales").id
    reader = _sign_in(app, session, "reader@x.local")
    stranger = _sign_in(app, session, "stranger@x.local")
    make_permission(session, user_id=_uid(session, "reader@x.local"), project_id=sales, capability="view")
    rid = _report(session, "Monthly", sales).id
    assert reader.post(f"/reports/{rid}/views", headers=CSRF).status_code == 204
    assert reader.post(f"/reports/{rid}/views", headers=CSRF).status_code == 204
    assert stranger.post(f"/reports/{rid}/views", headers=CSRF).status_code == 404
    assert reader.post("/reports/999/views", headers=CSRF).status_code == 404
    total, last = _now_of(session, rid)
    assert total == 1 and last is not None


def test_a_token_counts_nothing(app, session):
    sales = make_project(session, name="Sales").id
    script = make_user(session, email="script@x.local")
    make_permission(session, user_id=script.id, project_id=sales, capability="view")
    rid = _report(session, "Monthly", sales).id
    _, secret = api_tokens.create(session, script, "nightly", "write", 30, script)
    api = TestClient(app)
    assert api.post(f"/reports/{rid}/views", headers={"Authorization": f"Bearer {secret}"}).status_code == 204
    assert _now_of(session, rid) == (0, None)
    assert session.exec(select(ReportView)).all() == []


# ── the tab ───────────────────────────────────────────────────────────────────
def test_the_most_read_come_first(app, session):
    sales = make_project(session, name="Sales").id
    boss = _sign_in(app, session, "boss@x.local", is_superuser=True)
    quiet = _report(session, "A quiet one", sales, updated_days_ago=5)
    fresh = _report(session, "B fresh, never opened", sales, updated_days_ago=0)
    popular = _report(session, "C popular", sales, updated_days_ago=9)
    busy = _report(session, "D one busy reader", sales, updated_days_ago=9)
    old_star = _report(session, "E read long ago", sales, updated_days_ago=9)
    readers = _people(session, 3)
    now = datetime.now(timezone.utc)
    for u in readers:  # three people today
        report_views.record(session, popular, u, at=now)
    for d in range(4):  # one person, four days
        report_views.record(session, busy, readers[0], at=now - timedelta(days=d))
    report_views.record(session, quiet, readers[1], at=now - timedelta(days=2))
    for d in range(40, 50):  # ten openings, all before the window
        report_views.record(session, old_star, readers[2], at=now - timedelta(days=d))

    page = boss.get("/reports/search").json()
    assert page["total"] == 5
    # people first; at one person, the openings; at none in the window, the openings ever
    # (old_star's ten); then the latest changed
    assert _names(page) == ["C popular", "D one busy reader", "A quiet one", "E read long ago",
                            "B fresh, never opened"]
    row = {x["name"]: x for x in page["items"]}
    assert (row["C popular"]["people_30d"], row["C popular"]["opens_30d"], row["C popular"]["views_total"]) == (3, 3, 3)
    assert (row["D one busy reader"]["people_30d"], row["D one busy reader"]["opens_30d"]) == (1, 4)
    assert (row["E read long ago"]["people_30d"], row["E read long ago"]["views_total"]) == (0, 10)
    assert row["B fresh, never opened"]["last_viewed_at"] is None and row["C popular"]["last_viewed_at"]
    assert _names(boss.get("/reports/search?sort=name").json())[0] == "A quiet one"
    assert _names(boss.get("/reports/search?sort=updated").json())[0] == "B fresh, never opened"
    assert boss.get("/reports/search?sort=popular").status_code == 422
    assert fresh.id and quiet.id and busy.id  # used above


def test_the_metadata_of_a_row(app, session):
    root = make_project(session, name="Finance").id
    sub = make_project(session, name="Monthly close", parent_id=root).id
    boss = _sign_in(app, session, "boss@x.local", is_superuser=True)
    author = make_user(session, email="ada@x.local", full_name="Ada Lovelace")
    content = {"v": 1, "parameters": [{"name": "region"}, {"name": "since"}],
               "datasets": [{"name": "orders"}], "sections": {}}
    created = boss.post(f"/projects/{sub}/reports", json={"name": "Margin", "description": "Per category"},
                        headers=CSRF).json()
    r = session.get(Report, created["id"])
    r.owner_id = author.id
    session.add(r)
    session.commit()
    from app.services import reports

    reports.add_version(session, r, content, "edited", author)
    session.commit()
    item = boss.get("/reports/search").json()["items"][0]
    assert item["folder_path"] == ["Finance", "Monthly close"]
    assert item["owner_name"] == "Ada Lovelace" and item["description"] == "Per category"
    assert (item["current_version"], item["datasets"], item["parameters"]) == (2, 1, 2)
    assert item["editable"] and item["updated_at"]


def test_only_the_reports_one_sees(app, session):
    sales, hr = make_project(session, name="Sales").id, make_project(session, name="HR").id
    reader = _sign_in(app, session, "reader@x.local")
    nobody = _sign_in(app, session, "nobody@x.local")
    make_permission(session, user_id=_uid(session, "reader@x.local"), project_id=sales, capability="view")
    _report(session, "Monthly", sales)
    _report(session, "Salaries", hr)
    page = reader.get("/reports/search").json()
    assert page == {"items": page["items"], "total": 1} and _names(page) == ["Monthly"]
    assert not page["items"][0]["editable"]
    assert nobody.get("/reports/search").json() == {"items": [], "total": 0}
    # the plain list and a report by id still answer as before
    assert [x["name"] for x in reader.get("/reports").json()] == ["Monthly"]
    rid = page["items"][0]["id"]
    assert reader.get(f"/reports/{rid}").json()["name"] == "Monthly"


def test_the_search_and_the_pages(app, session):
    finance = make_project(session, name="Finance")
    close = make_project(session, name="Close", parent_id=finance.id).id
    ops = make_project(session, name="Operations").id
    boss = _sign_in(app, session, "boss@x.local", is_superuser=True)
    _report(session, "Margin", close)
    _report(session, "Stock", ops, description="Warehouse stock by finance code")
    _report(session, "Shipments", ops)
    _report(session, "100%_done", ops)
    found = lambda q: sorted(_names(boss.get("/reports/search", params={"q": q}).json()))
    assert found("finance") == ["Margin", "Stock"]  # a folder above it, a description
    assert found("CLOSE") == ["Margin"]  # its own folder, any case
    assert found("ship") == ["Shipments"]
    assert found("%") == found("_") == ["100%_done"]  # LIKE's characters are literal
    assert found("  ") == ["100%_done", "Margin", "Shipments", "Stock"]

    first = boss.get("/reports/search?sort=name&limit=3").json()
    second = boss.get("/reports/search?sort=name&limit=3&offset=3").json()
    assert first["total"] == second["total"] == 4
    assert _names(first) + _names(second) == ["100%_done", "Margin", "Shipments", "Stock"]


# ── what goes ─────────────────────────────────────────────────────────────────
def test_the_housekeeping_drops_the_old_openings_and_keeps_the_totals(session):
    p = make_project(session, name="Sales").id
    r = _report(session, "Monthly", p)
    alice = make_user(session, email="alice@x.local")
    for d in (0, 29, 30, 45):
        report_views.record(session, r, alice, at=NOW - timedelta(days=d))
    assert housekeeping.report_views(session, at=NOW) == 2  # the 30th and the 45th day before
    days = sorted(v.day for v in session.exec(select(ReportView)).all())
    assert days == [(NOW - timedelta(days=29)).date(), NOW.date()]
    session.refresh(r)
    assert r.views_total == 4


def test_the_openings_go_with_their_report(app, session):
    sales = make_project(session, name="Sales").id
    boss = _sign_in(app, session, "boss@x.local", is_superuser=True)
    rid = boss.post(f"/projects/{sales}/reports", json={"name": "Monthly"}, headers=CSRF).json()["id"]
    assert boss.post(f"/reports/{rid}/views", headers=CSRF).status_code == 204
    assert boss.delete(f"/reports/{rid}", headers=CSRF).status_code == 204
    assert session.exec(select(ReportView)).all() == []
