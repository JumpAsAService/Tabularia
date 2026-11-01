"""Dashboards as objects of a folder (docs/specs/dashboards.md, piece 1): who sees and changes
them, their versions, the Explore's operations, the audit, what a definition may hold.

The rules these tests hold: VIEW on the folder sees and opens a dashboard, EDIT changes,
moves, renames, duplicates, deletes it and restores a version; whoever has no VIEW gets the
same answer as for one that does not exist, and finds it nowhere; `parameters` and `datasets`
take a report's schema and are checked by a report's code, with their limits and in the
dashboard's words; a dashboard holds on to what it reads as a report does.
"""
import base64
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db.session import get_session
from app.models import (
    AuditLog, Connection, Dashboard, DashboardVersion, DashboardView, Report, ReportVersion, SavedView, SqlScript,
    SqlScriptVersion, User,
)
from app.routes import dashboards as routes
from app.services import api_tokens, dashboards, housekeeping
from tests.conftest import make_datasource, make_permission, make_project, make_user
from tests.test_engine_settings import CSRF, _sign_in

NOW = datetime(2026, 10, 31, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def app(session, fake_engine):
    from app.routes import auth as auth_routes
    from app.routes import projects as project_routes
    from app.routes import search as search_routes

    app = FastAPI()
    for r in (auth_routes.router, routes.router, project_routes.router, search_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _ids(session) -> dict[str, int]:
    return {u.email: u.id for u in session.exec(select(User)).all()}


def _connection(session, project_id, db_type="postgresql", name="ERP") -> int:
    c = Connection(name=name, project_id=project_id, db_type=db_type, host="db", database="erp", username="reader",
                   password_encrypted="encrypted")
    session.add(c)
    session.commit()
    session.refresh(c)
    return c.id


def definition(datasets=(), parameters=(), tiles=(), **extra) -> dict:
    return {"v": 1, "timezone": "UTC", "parameters": list(parameters), "datasets": list(datasets), "tiles": list(tiles),
            **extra}


def sql_dataset(name="orders", sql="SELECT * FROM \"Orders\"", **source) -> dict:
    return {"name": name, "source": {"kind": "sql", "sql": sql, **source}}


def kpi(tile_id="revenue", dataset="orders", x=0, y=0, w=3, h=3, **extra) -> dict:
    return {"id": tile_id, "type": "kpi", "dataset": dataset, "grid": {"x": x, "y": y, "w": w, "h": h},
            "value": "SUM([ricavo])", **extra}


def chart(tile_id="by_area", dataset="orders", x=3, y=0, w=9, h=6, **extra) -> dict:
    return {"id": tile_id, "type": "chart", "kind": "bar", "dataset": dataset, "title": "By area",
            "grid": {"x": x, "y": y, "w": w, "h": h}, "category": "[area]", "series": [{"value": "SUM([ricavo])"}],
            **extra}


def _actions(session) -> list[str]:
    return [a.action for a in session.exec(select(AuditLog).order_by(AuditLog.id)).all() if a.action.startswith("dashboard.")]


def _create(client, folder, name="D", content=None):
    body = {"name": name} if content is None else {"name": name, "content": content}
    return client.post(f"/projects/{folder}/dashboards", json=body, headers=CSRF)


# ── who sees what ─────────────────────────────────────────────────────────────
def test_view_reads_edit_changes_and_whoever_has_neither_finds_nothing(app, session):
    sales = make_project(session, name="Sales").id
    boss, reader, stranger = (_sign_in(app, session, e) for e in ("boss@x.local", "reader@x.local", "other@x.local"))
    by = _ids(session)
    make_permission(session, user_id=by["boss@x.local"], project_id=sales, capability="edit")
    make_permission(session, user_id=by["reader@x.local"], project_id=sales, capability="view")

    r = _create(boss, sales, "Sales overview")
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    assert r.json()["current_version"] == 1 and r.json()["editable"] and r.json()["content"]["v"] == 1
    assert _create(boss, sales, "Sales overview").status_code == 409
    assert _create(boss, sales, "  ").status_code == 422

    seen = reader.get(f"/dashboards/{did}").json()
    assert seen["name"] == "Sales overview" and not seen["editable"]
    assert [x["name"] for x in reader.get(f"/projects/{sales}/dashboards").json()] == ["Sales overview"]
    assert [x["name"] for x in reader.get("/dashboards").json()] == ["Sales overview"]
    assert reader.get(f"/dashboards/{did}/versions").status_code == 200
    assert reader.get(f"/dashboards/{did}/versions/1").status_code == 200
    assert reader.patch(f"/dashboards/{did}", json={"name": "Mine"}, headers=CSRF).status_code == 403
    assert reader.post(f"/dashboards/{did}/duplicate", headers=CSRF).status_code == 403
    assert reader.post(f"/dashboards/{did}/versions/1/restore", headers=CSRF).status_code == 403
    assert reader.delete(f"/dashboards/{did}", headers=CSRF).status_code == 403
    assert _create(reader, sales, "Mine").status_code == 403
    assert session.get(Dashboard, did).name == "Sales overview"


def _a_dashboard_nobody_else_sees(app, session):
    vault = make_project(session, name="Vault").id
    owner = _sign_in(app, session, "owner@x.local", is_superuser=True)
    did = _create(owner, vault, "Payroll secrets", definition(tiles=[{"id": "t", "type": "text", "value": "Secret note",
                                                                      "grid": {"x": 0, "y": 0, "w": 4, "h": 2}}])).json()["id"]
    return vault, did


@pytest.mark.parametrize("method,path,body", [
    ("get", "/dashboards/{id}", None),
    ("get", "/dashboards/{id}/versions", None),
    ("get", "/dashboards/{id}/versions/1", None),
    ("patch", "/dashboards/{id}", {"name": "Mine"}),
    ("delete", "/dashboards/{id}", None),
    ("post", "/dashboards/{id}/duplicate", None),
    ("post", "/dashboards/{id}/versions/1/restore", None),
    ("post", "/dashboards/{id}/views", None),
])
def test_without_view_a_dashboard_answers_exactly_as_one_that_does_not_exist(app, session, method, path, body):
    """Not 403: that would say it is there. The body is the same word for word."""
    _, did = _a_dashboard_nobody_else_sees(app, session)
    stranger = _sign_in(app, session, "stranger@x.local")

    def ask(i):
        kwargs = {"headers": CSRF} if method != "get" else {}
        if body is not None:
            kwargs["json"] = body
        return getattr(stranger, method)(path.format(id=i), **kwargs)

    there, nowhere = ask(did), ask(987654)
    assert there.status_code == nowhere.status_code == 404
    assert there.json() == nowhere.json()
    assert "Payroll" not in there.text
    assert session.get(Dashboard, did).name == "Payroll secrets"
    assert _actions(session) == ["dashboard.create"]


def test_without_view_a_dashboard_is_in_no_list_and_no_search(app, session):
    vault, did = _a_dashboard_nobody_else_sees(app, session)
    stranger = _sign_in(app, session, "stranger@x.local")
    assert stranger.get("/dashboards").json() == []
    assert stranger.get(f"/projects/{vault}/dashboards").status_code == 403
    found = stranger.get("/search", params={"q": "payroll"}).json()
    assert found["items"] == [] and found["total"] == 0 and found["counts"] == {}
    assert stranger.get("/search", params={"q": "payroll", "kind": "dashboard"}).json()["items"] == []
    # its check says as little: the folder's own rule, and nothing of the dashboard
    r = stranger.post("/dashboards/check", json={"dashboard_id": did, "definition": definition()}, headers=CSRF)
    assert r.status_code == 404 and "Payroll" not in r.text
    assert stranger.post("/dashboards/check", json={"project_id": vault, "definition": definition()},
                         headers=CSRF).status_code == 403


def test_a_folder_read_by_someone_shows_them_its_dashboards_and_no_other(app, session):
    vault, _ = _a_dashboard_nobody_else_sees(app, session)
    sales = make_project(session, name="Sales").id
    owner = _sign_in(app, session, "boss@x.local", is_superuser=True)
    _create(owner, sales, "Sales overview")
    reader = _sign_in(app, session, "reader@x.local")
    make_permission(session, user_id=_ids(session)["reader@x.local"], project_id=sales, capability="view")
    assert [d["name"] for d in reader.get("/dashboards").json()] == ["Sales overview"]
    assert reader.get("/search", params={"q": "over"}).json()["counts"] == {"dashboard": 1}
    assert reader.get("/search", params={"q": "secrets"}).json()["items"] == []
    assert reader.get(f"/projects/{vault}/dashboards").status_code == 403


# ── the object ────────────────────────────────────────────────────────────────
def test_a_new_dashboard_starts_empty_in_its_author_s_language_and_the_installation_s_zone(app, session, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings().app, "timezone", "Europe/Rome")
    folder = make_project(session, name="F").id
    me = _sign_in(app, session, "ana@x.local", is_superuser=True, locale="it")
    r = _create(me, folder).json()
    assert r["content"] == {"v": 1, "locale": "it", "timezone": "Europe/Rome", "refresh_seconds": 0, "parameters": [],
                            "datasets": [], "tiles": []}
    assert (r["datasets"], r["parameters"], r["tiles"]) == (0, 0, 0)


def test_versions_restore_duplicate_rename_move_delete_and_the_audit(app, session):
    sales, other = make_project(session, name="Sales").id, make_project(session, name="Other").id
    me = _sign_in(app, session, "boss@x.local", is_superuser=True)
    first = definition(parameters=[{"name": "region", "type": "text"}])
    did = _create(me, sales, "D", first).json()["id"]
    second = {**first, "locale": "de"}
    assert me.patch(f"/dashboards/{did}", json={"content": second}, headers=CSRF).json()["current_version"] == 2
    # the same definition again, and a description: no new version
    r = me.patch(f"/dashboards/{did}", json={"content": second, "description": "For the board"}, headers=CSRF).json()
    assert r["current_version"] == 2 and r["description"] == "For the board"
    assert [(v["version"], v["note"], v["current"]) for v in me.get(f"/dashboards/{did}/versions").json()] == [
        (2, "edited", True), (1, "created", False)]
    assert me.get(f"/dashboards/{did}/versions/1").json() == first
    assert me.get(f"/dashboards/{did}/versions/9").status_code == 404
    assert me.post(f"/dashboards/{did}/versions/9/restore", headers=CSRF).status_code == 404
    restored = me.post(f"/dashboards/{did}/versions/1/restore", headers=CSRF).json()
    assert restored["current_version"] == 3 and restored["content"] == first and restored["parameters"] == 1
    # the current one restored: nothing happens
    assert me.post(f"/dashboards/{did}/versions/3/restore", headers=CSRF).json()["current_version"] == 3

    copy = me.post(f"/dashboards/{did}/duplicate", headers=CSRF).json()
    assert copy["name"] == "D (1)" and copy["content"] == first and copy["current_version"] == 1
    moved = me.patch(f"/dashboards/{did}", json={"project_id": other, "name": "Board pack"}, headers=CSRF).json()
    assert (moved["project_id"], moved["name"]) == (other, "Board pack")
    assert me.patch(f"/dashboards/{copy['id']}", json={"project_id": other, "name": "Board pack"},
                    headers=CSRF).status_code == 409
    r = me.delete(f"/projects/{other}", headers=CSRF)
    assert r.status_code == 409 and "dashboards" in r.json()["detail"]
    hits = me.get("/search", params={"q": "board"}).json()
    assert [(h["kind"], h["name"], h["project_name"]) for h in hits["items"]] == [("dashboard", "Board pack", "Other")]
    assert hits["counts"] == {"dashboard": 1}

    assert me.delete(f"/dashboards/{did}", headers=CSRF).status_code == 204
    session.expire_all()
    assert session.get(Dashboard, did) is None
    assert session.exec(select(DashboardVersion).where(DashboardVersion.dashboard_id == did)).all() == []
    assert _actions(session) == ["dashboard.create", "dashboard.update", "dashboard.update", "dashboard.restore",
                                 "dashboard.duplicate", "dashboard.rename", "dashboard.move", "dashboard.delete"]
    rows = {a.action: a for a in session.exec(select(AuditLog)).all()}
    assert rows["dashboard.create"].target_type == "dashboard"
    assert json.loads(rows["dashboard.rename"].detail) == {"from": "D", "to": "Board pack"}
    assert json.loads(rows["dashboard.move"].detail) == {"from": sales, "to": other}
    assert json.loads(rows["dashboard.duplicate"].detail) == {"project_id": sales, "duplicate_of": did}
    assert json.loads(rows["dashboard.restore"].detail) == {"from": 1, "version": 3}
    assert rows["dashboard.delete"].target_id == did and rows["dashboard.delete"].target_label == "Board pack"


def test_a_move_needs_edit_on_both_folders(app, session):
    a, b = make_project(session, name="A").id, make_project(session, name="B").id
    me = _sign_in(app, session, "ed@x.local")
    make_permission(session, user_id=_ids(session)["ed@x.local"], project_id=a, capability="edit")
    make_permission(session, user_id=_ids(session)["ed@x.local"], project_id=b, capability="view")
    did = _create(me, a).json()["id"]
    assert me.patch(f"/dashboards/{did}", json={"project_id": b}, headers=CSRF).status_code == 403
    assert me.patch(f"/dashboards/{did}", json={"project_id": 999}, headers=CSRF).status_code == 404
    assert session.get(Dashboard, did).project_id == a


def test_a_copy_belongs_to_whoever_made_it_and_starts_at_its_first_version(app, session):
    sales = make_project(session, name="Sales").id
    boss, helper = _sign_in(app, session, "boss@x.local"), _sign_in(app, session, "helper@x.local")
    by = _ids(session)
    for who in by:
        make_permission(session, user_id=by[who], project_id=sales, capability="edit")
    did = _create(boss, sales, "D").json()["id"]
    boss.patch(f"/dashboards/{did}", json={"content": definition(locale="fr")}, headers=CSRF)
    copy = helper.post(f"/dashboards/{did}/duplicate", headers=CSRF).json()
    assert copy["current_version"] == 1 and copy["content"]["locale"] == "fr"
    session.expire_all()
    assert session.get(Dashboard, copy["id"]).owner_id == by["helper@x.local"]
    assert session.get(Dashboard, did).owner_id == by["boss@x.local"]


def test_a_save_on_a_version_someone_replaced_says_so_and_changes_nothing(app, session):
    folder = make_project(session, name="F").id
    me = _sign_in(app, session, "boss@x.local", is_superuser=True)
    did = _create(me, folder, "D").json()["id"]
    assert me.patch(f"/dashboards/{did}", json={"content": definition(locale="de"), "base_version": 1},
                    headers=CSRF).json()["current_version"] == 2
    r = me.patch(f"/dashboards/{did}", json={"content": definition(locale="fr"), "base_version": 1}, headers=CSRF)
    assert r.status_code == 409 and "changed since you opened it" in r.json()["detail"]
    assert me.get(f"/dashboards/{did}").json()["content"]["locale"] == "de"


def test_a_list_says_the_sizes_without_loading_the_definitions(app, session):
    folder = make_project(session, name="F").id
    me = _sign_in(app, session, "boss@x.local", is_superuser=True)
    did = _create(me, folder, "D", definition(datasets=[sql_dataset(sql="SELECT 1")], parameters=[{"name": "region", "type": "text"}],
                                              tiles=[kpi(), chart()])).json()["id"]
    row = me.get(f"/projects/{folder}/dashboards").json()[0]
    assert row["id"] == did and "content" not in row
    assert (row["datasets"], row["parameters"], row["tiles"]) == (1, 1, 2)


# ── what a definition may hold ───────────────────────────────────────────────
def _refused(client, folder, content) -> dict:
    r = _create(client, folder, "Refused", content)
    assert r.status_code == 422, r.text
    return r.json()["detail"]


@pytest.fixture
def me(app, session):
    return _sign_in(app, session, "ana@x.local", is_superuser=True)


@pytest.fixture
def folder(session):
    return make_project(session, name="F").id


def test_the_spec_s_definition_is_accepted(me, folder, session):
    orders = make_datasource(session, name="Orders", project_id=folder, key="k.parquet",
                             columns=json.dumps([{"name": "area", "dtype": "String"}, {"name": "ricavo", "dtype": "Float64"}])).id
    content = {
        "v": 1, "locale": "en", "timezone": "Europe/Rome", "refresh_seconds": 300,
        "parameters": [{"name": "region", "label": "Region", "type": "text", "required": False,
                        "choices": {"dataset": "sales", "value": "[area]"}}],
        "datasets": [{"name": "sales",
                      "source": {"kind": "datasource", "datasource_id": orders,
                                 "filters": [{"column": "area", "op": "=", "value": "{region}"}]},
                      "fields": [{"name": "area", "type": "string"}, {"name": "ricavo", "type": "float"}],
                      "calculated": []}],
        "tiles": [
            {"id": "revenue", "type": "kpi", "dataset": "sales", "title": "Revenue",
             "grid": {"x": 0, "y": 0, "w": 3, "h": 3}, "value": "SUM([ricavo])", "format": "#,##0"},
            {"id": "by_area", "type": "chart", "kind": "bar", "dataset": "sales", "title": "By area",
             "grid": {"x": 3, "y": 0, "w": 9, "h": 6}, "category": "[area]", "series": [{"value": "SUM([ricavo])"}],
             "filters_others": True},
        ],
    }
    r = _create(me, folder, "Spec", content)
    assert r.status_code == 201, r.text
    assert r.json()["content"] == content


@pytest.mark.parametrize("seconds", [0, 60, 300, 86400])
def test_refresh_seconds_is_by_hand_or_at_least_a_minute(me, folder, seconds):
    assert _create(me, folder, "D", definition(refresh_seconds=seconds)).status_code == 201


@pytest.mark.parametrize("seconds", [1, 59, -60, 86401, 60.5, "300", True, None])
def test_a_shorter_or_strange_refresh_is_refused_saying_what_it_takes(me, folder, seconds):
    refused = _refused(me, folder, definition(refresh_seconds=seconds))
    assert refused["where"] == "refresh_seconds" and "60" in refused["message"] and "0" in refused["message"]


def test_the_limits_are_stated_in_the_error(me, folder):
    tiles = [kpi(f"t{i}", x=(i % 4) * 3, y=(i // 4) * 3) for i in range(41)]
    datasets = [sql_dataset(f"d{i}") for i in range(11)]
    parameters = [{"name": f"p{i}", "type": "text"} for i in range(21)]
    refused = _refused(me, folder, definition(datasets=[sql_dataset()], tiles=tiles))
    assert (refused["where"], refused["message"]) == ("tiles", "A dashboard holds at most 40 tiles: this one has 41")
    refused = _refused(me, folder, definition(datasets=datasets))
    assert (refused["where"], refused["message"]) == ("datasets", "A dashboard holds at most 10 datasets: this one has 11")
    refused = _refused(me, folder, definition(parameters=parameters))
    assert (refused["where"], refused["message"]) == ("parameters", "A dashboard holds at most 20 parameters: this one has 21")


def test_forty_tiles_ten_datasets_twenty_parameters_are_accepted(me, folder):
    r = _create(me, folder, "Full", definition(
        datasets=[sql_dataset(f"d{i}", sql="SELECT 1") for i in range(10)],
        parameters=[{"name": f"p{i}", "type": "text"} for i in range(20)],
        tiles=[kpi(f"t{i}", dataset="d0", x=(i % 4) * 3, y=(i // 4) * 3) for i in range(40)]))
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("change,where,words", [
    ({"v": 2}, "v", "version 1"),
    ({"sections": {}}, "", "unknown key «sections»"),
    ({"theme": {"size": 9}}, "", "unknown key «theme»"),
    ({"locale": "xx"}, "locale", "one of"),
    ({"timezone": "Mars/Olympus"}, "timezone", "Unknown time zone"),
    ({"tiles": "many"}, "tiles", "list"),
])
def test_a_definition_is_closed_and_says_what_is_wrong(me, folder, change, where, words):
    refused = _refused(me, folder, {**definition(), **change})
    assert refused["where"] == where and words in refused["message"], refused


@pytest.mark.parametrize("tile,where,words", [
    ("nope", "tiles[0]", "object"),
    ({"type": "kpi"}, "tiles[0].id", "an id"),
    ({"id": "a b", "type": "kpi"}, "tiles[0].id", "an id"),
    ({"id": "t", "type": "line", "grid": {"x": 0, "y": 0, "w": 3, "h": 3}}, "tiles[0].type", "kpi, chart, table, text, image"),
    ({"id": "t", "type": "rect", "grid": {"x": 0, "y": 0, "w": 3, "h": 3}}, "tiles[0].type", "kpi, chart, table, text, image"),
    ({"id": "t", "type": "text"}, "tiles[0].grid", "«grid» is missing"),
    ({"id": "t", "type": "text", "grid": {"x": 0, "y": 0, "w": 3}}, "tiles[0].grid.h", "missing"),
    ({"id": "t", "type": "text", "grid": {"x": 0, "y": 0, "w": 3, "h": 3, "z": 1}}, "tiles[0].grid", "unknown key «z»"),
    ({"id": "t", "type": "text", "x": 1, "grid": {"x": 0, "y": 0, "w": 3, "h": 3}}, "tiles[0].x", "«grid»"),
    ({"id": "t", "type": "text", "w": 50, "grid": {"x": 0, "y": 0, "w": 3, "h": 3}}, "tiles[0].w", "«grid»"),
    ({"id": "t", "type": "text", "grid": {"x": 1.5, "y": 0, "w": 3, "h": 3}}, "tiles[0].grid.x", "whole number"),
    ({"id": "t", "type": "text", "grid": {"x": True, "y": 0, "w": 3, "h": 3}}, "tiles[0].grid.x", "whole number"),
    ({"id": "t", "type": "text", "grid": {"x": -1, "y": 0, "w": 3, "h": 3}}, "tiles[0].grid.x", "between"),
    ({"id": "t", "type": "text", "grid": {"x": 10, "y": 0, "w": 3, "h": 3}}, "tiles[0].grid", "12 columns"),
    ({"id": "t", "type": "text", "grid": {"x": 0, "y": 0, "w": 13, "h": 3}}, "tiles[0].grid.w", "between"),
    ({"id": "t", "type": "text", "grid": {"x": 0, "y": 0, "w": 3, "h": 0}}, "tiles[0].grid.h", "between"),
    ({"id": "t", "type": "kpi", "dataset": "orders", "value": "1", "grid": {"x": 0, "y": 0, "w": 1, "h": 3}},
     "tiles[0].grid", "at least 2 columns wide"),
    ({"id": "t", "type": "chart", "kind": "bar", "dataset": "orders", "series": [{"value": "1"}],
      "grid": {"x": 0, "y": 0, "w": 4, "h": 2}}, "tiles[0].grid", "at least 4 rows high"),
    ({"id": "t", "type": "kpi", "dataset": "orders", "value": "1", "filters_others": True,
      "grid": {"x": 0, "y": 0, "w": 3, "h": 3}}, "tiles[0].filters_others", "does not filter"),
    ({"id": "t", "type": "chart", "kind": "bar", "dataset": "orders", "series": [{"value": "1"}], "filters_others": "yes",
      "grid": {"x": 0, "y": 0, "w": 4, "h": 4}}, "tiles[0].filters_others", "true or false"),
    ({"id": "t", "type": "text", "title": 5, "grid": {"x": 0, "y": 0, "w": 3, "h": 3}}, "tiles[0].title", "text"),
    ({"id": "t", "type": "text", "title": "x" * 201, "grid": {"x": 0, "y": 0, "w": 3, "h": 3}}, "tiles[0].title", "at most 200"),
])
def test_a_tile_that_does_not_fit_the_grid_or_its_type_says_where(me, folder, tile, where, words):
    refused = _refused(me, folder, definition(datasets=[sql_dataset(sql="SELECT 1")], tiles=[tile]))
    assert refused["where"] == where and words in refused["message"], refused


def test_a_tile_takes_the_properties_of_the_report_item_of_its_type_and_no_other(me, folder):
    grid = {"x": 0, "y": 0, "w": 4, "h": 4}
    base = {"id": "c", "type": "chart", "dataset": "orders", "grid": grid}
    ds = [sql_dataset(sql="SELECT 1")]
    refused = _refused(me, folder, definition(datasets=ds, tiles=[{**base, "kind": "radar", "series": [{"value": "1"}]}]))
    assert "kind is one of" in refused["message"] and refused["item"] == "c" and refused["where"] == "tiles[0].kind"
    refused = _refused(me, folder, definition(datasets=ds, tiles=[{**base, "kind": "bar", "series": []}]))
    assert "1 to 12 series" in refused["message"]
    refused = _refused(me, folder, definition(datasets=ds, tiles=[{**base, "kind": "bar", "series": [{"value": "1"}], "columns": []}]))
    assert refused["where"] == "tiles[0]" and "unknown key «columns»" in refused["message"]
    table = {"id": "t", "type": "table", "dataset": "orders", "grid": grid, "matrix": {"columns_by": "[a]", "value": "SUM([b])"}}
    assert _create(me, folder, "Matrix", definition(datasets=ds, tiles=[table])).status_code == 201


def test_a_tile_reads_a_dataset_the_dashboard_has_and_two_tiles_have_two_ids(me, folder):
    refused = _refused(me, folder, definition(tiles=[kpi()]))
    assert refused["item"] == "revenue" and refused["where"] == "tiles[0].dataset"
    assert refused["message"] == "The tile «revenue» reads the dataset «orders», which the dashboard does not have"
    refused = _refused(me, folder, definition(datasets=[sql_dataset(sql="SELECT 1")], tiles=[kpi("a"), kpi("a", x=3)]))
    assert refused["where"] == "tiles[1].id" and "Two tiles" in refused["message"]


def test_two_tiles_on_the_same_cells_are_refused_by_name(me, folder):
    ds = [sql_dataset(sql="SELECT 1")]
    refused = _refused(me, folder, definition(datasets=ds, tiles=[kpi("a"), kpi("b", x=2, y=2)]))
    assert refused["where"] == "tiles[1].grid" and refused["item"] == "b"
    assert "«b»" in refused["message"] and "«a»" in refused["message"]
    # side by side and one above the other: not an overlap
    assert _create(me, folder, "Fine", definition(datasets=ds, tiles=[kpi("a"), kpi("b", x=3), kpi("c", y=3)])).status_code == 201


def test_the_problems_of_parameters_and_datasets_are_a_report_s_said_of_a_dashboard(me, folder):
    refused = _refused(me, folder, definition(parameters=[{"name": "today", "type": "text"}]))
    assert refused["where"] == "parameters[0].name" and "placeholders" in refused["message"]
    refused = _refused(me, folder, definition(parameters=[{"name": "region", "type": "text"}, {"name": "region", "type": "text"}]))
    assert "Two parameters" in refused["message"]
    refused = _refused(me, folder, definition(datasets=[sql_dataset("a b")]))
    assert refused["where"] == "datasets[0].name"
    refused = _refused(me, folder, definition(datasets=[{"name": "d", "source": {"kind": "file"}}]))
    assert refused["where"] == "datasets[0].source.kind"
    refused = _refused(me, folder, definition(parameters=[{"name": "p", "type": "text", "choices": {"dataset": "gone", "value": "[a]"}}]))
    assert refused["message"] == "The parameter «p» takes its choices from «gone», which the dashboard does not have"
    refused = _refused(me, folder, definition(datasets=[{"name": "d", "source": {
        "kind": "datasource", "datasource_id": 1, "filters": [{"column": "a", "op": "=", "value": "{gone}"}]}}]))
    assert refused["message"] == "The dataset «d»'s filter: the dashboard has no parameter «gone»"
    refused = _refused(me, folder, definition(datasets=[sql_dataset(sql="SELECT 1 WHERE a = {regoin}")]))
    assert "neither a parameter of the dashboard" in refused["message"]
    assert "report" not in json.dumps(refused)


def _image(data: bytes, kind="svg+xml") -> str:
    return f"data:image/{kind};base64,{base64.b64encode(data).decode()}"


def test_an_image_as_in_reports(me, folder):
    grid = {"x": 0, "y": 0, "w": 3, "h": 3}
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 100).decode()
    # the base64 in one piece: what a report keeps
    wrapped = f"data:image/png;base64,{png[:20]}\n{png[20:]}"
    r = _create(me, folder, "Logo", definition(tiles=[{"id": "i", "type": "image", "asset": wrapped, "grid": grid}]))
    assert r.status_code == 201, r.text
    assert r.json()["content"]["tiles"][0]["asset"] == f"data:image/png;base64,{png}"
    refused = _refused(me, folder, definition(tiles=[{"id": "i", "type": "image", "grid": grid,
                                                      "asset": _image(b"<svg xmlns='http://www.w3.org/2000/svg'><script/></svg>")}]))
    assert "this SVG cannot be used" in refused["message"] and refused["where"] == "tiles[0].asset"
    refused = _refused(me, folder, definition(tiles=[{"id": "i", "type": "image", "asset": "https://x.test/a.png", "grid": grid}]))
    assert "uploaded into the dashboard" in refused["message"]


def test_what_a_save_refuses_changes_nothing(me, folder):
    did = _create(me, folder, "D", definition(locale="de")).json()["id"]
    assert me.patch(f"/dashboards/{did}", json={"content": definition(refresh_seconds=5)}, headers=CSRF).status_code == 422
    seen = me.get(f"/dashboards/{did}").json()
    assert seen["current_version"] == 1 and seen["content"]["locale"] == "de"


# ── what a definition points to ──────────────────────────────────────────────
def test_a_definition_points_only_to_what_its_author_may_read(app, session):
    sales, secret, vault = (make_project(session, name=n).id for n in ("Sales", "Secret", "Vault"))
    erp = _connection(session, secret)
    hidden = make_datasource(session, name="Salaries", project_id=vault,
                             columns=json.dumps([{"name": "who", "dtype": "String"}])).id
    shown = make_datasource(session, name="Orders", project_id=sales,
                            columns=json.dumps([{"name": "region", "dtype": "String"}])).id
    view = SavedView(name="V", project_id=sales, datasource_id=hidden, spec="{}")
    script = SqlScript(name="Q", project_id=vault, current_version=1)
    session.add_all([view, script])
    session.commit()
    session.add(SqlScriptVersion(script_id=script.id, version=1, content=json.dumps({"sql": "SELECT 1"})))
    session.commit()
    view_id, script_id = view.id, script.id
    boss, helper = _sign_in(app, session, "boss@x.local"), _sign_in(app, session, "helper@x.local")
    by = _ids(session)
    for who in ("boss@x.local", "helper@x.local"):
        make_permission(session, user_id=by[who], project_id=sales, capability="edit")

    def save(client, content, did=None):
        if did is None:
            return _create(client, sales, f"D{len(_actions(session))}", content)
        return client.patch(f"/dashboards/{did}", json={"content": content}, headers=CSRF)

    on_erp = definition([sql_dataset(sql="SELECT 1", connection_id=erp)])
    r = save(boss, on_erp)
    assert r.status_code == 422
    assert r.json()["detail"]["message"] == ("The dataset «orders»: it reads a database connection "
                                             f"({erp}) that does not exist or that you cannot use")
    assert r.json()["detail"]["where"] == "datasets[0].source"
    for source, words in ((({"kind": "datasource", "datasource_id": hidden}), "a datasource"),
                          (({"kind": "view", "view_id": view_id}), "a datasource"),
                          (({"kind": "view", "view_id": 999}), "a saved view (999)"),
                          (({"kind": "sql_script", "script_id": script_id}), "a SQL script")):
        r = save(boss, definition([{"name": "d", "source": source}]))
        assert r.status_code == 422 and words in r.json()["detail"]["message"], r.text
        assert "Salaries" not in r.text
    r = save(boss, definition([{"name": "d", "source": {"kind": "datasource", "datasource_id": shown,
                                                        "filters": [{"column": "nope", "op": "=", "value": 1}]}}]))
    assert "names the column «nope», which «Orders» does not have" in r.json()["detail"]["message"]

    # with CONNECT the boss may; the helper, without it, still changes the rest of the dashboard
    make_permission(session, user_id=by["boss@x.local"], project_id=secret, capability="connect")
    r = save(boss, on_erp)
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    assert save(helper, {**on_erp, "locale": "fr"}, did).status_code == 200
    r = save(helper, definition([sql_dataset(sql="SELECT 2", connection_id=erp)]), did)
    assert r.status_code == 422 and "cannot use" in r.json()["detail"]["message"]
    # and restoring a version needs the same right as saving it
    save(boss, definition(), did)
    assert helper.post(f"/dashboards/{did}/versions/1/restore", headers=CSRF).status_code == 422
    assert boss.post(f"/dashboards/{did}/versions/1/restore", headers=CSRF).status_code == 200


def test_two_lists_that_fill_each_other_are_refused_by_name(me, folder, session):
    orders = make_datasource(session, name="Orders", project_id=folder, key="k.parquet",
                             columns=json.dumps([{"name": "region", "dtype": "String"}, {"name": "agent", "dtype": "String"}])).id

    def on(name, column, value):
        return {"name": name, "source": {"kind": "datasource", "datasource_id": orders,
                                         "filters": [{"column": column, "op": "=", "value": value}]}}

    refused = _refused(me, folder, definition(
        datasets=[on("regions", "agent", "{agent}"), on("agents", "region", "{region}")],
        parameters=[{"name": "region", "type": "text", "choices": {"dataset": "regions", "value": "[region]"}},
                    {"name": "agent", "type": "text", "choices": {"dataset": "agents", "value": "[agent]"}}]))
    assert "each other" in refused["message"] and refused["where"].startswith("parameters[")


# ── the engine ────────────────────────────────────────────────────────────────
def test_the_engine_checks_the_expressions_of_the_tiles_when_it_can(me, folder, fake_engine):
    content = definition(datasets=[sql_dataset(sql="SELECT 1")], tiles=[kpi(value="SUMM([ricavo])")])
    # an engine without the route or out of order does not stop the save
    assert _create(me, folder, "A", content).status_code == 201
    fake_engine.report_check_response = (500, {"detail": "boom"})
    assert _create(me, folder, "B", content).status_code == 201
    # the engine reads the tiles as a report's items, in a report's words, and is said in ours
    error = {"item": "revenue", "field": "value", "pos": 0, "length": 4,
             "message": "the item of the report has an unknown function SUMM"}
    fake_engine.report_check_response = (200, {"problems": [error]})
    r = _create(me, folder, "C", content)
    assert r.status_code == 422
    said = {**error, "message": "The tile of the dashboard has an unknown function SUMM"}
    assert r.json()["detail"] == {"message": said["message"], "where": "value", "item": "revenue",
                                  "errors": [said], "problems": [said]}
    sent = fake_engine.report_checks[-1]["definition"]
    item = sent["sections"]["body"]["items"][0]
    assert item["value"] == "SUMM([ricavo])" and "grid" not in item and "tiles" not in sent
    fake_engine.report_check_response = (200, {"problems": []})
    assert _create(me, folder, "C", content).status_code == 201


def test_the_engine_is_not_told_about_images(me, folder, fake_engine):
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 100).decode()
    fake_engine.report_check_response = (200, {"problems": []})
    content = definition(tiles=[{"id": "i", "type": "image", "asset": f"data:image/png;base64,{png}",
                                 "grid": {"x": 0, "y": 0, "w": 3, "h": 3}}])
    assert _create(me, folder, "A", content).status_code == 201
    assert fake_engine.report_checks[-1]["definition"]["sections"]["body"]["items"][0]["asset"] == ""


def test_check_says_every_problem_without_saving_and_asks_for_edit(app, session, fake_engine):
    sales = make_project(session, name="Sales").id
    boss, reader = _sign_in(app, session, "boss@x.local"), _sign_in(app, session, "reader@x.local")
    by = _ids(session)
    make_permission(session, user_id=by["boss@x.local"], project_id=sales, capability="edit")
    make_permission(session, user_id=by["reader@x.local"], project_id=sales, capability="view")
    did = _create(boss, sales, "D").json()["id"]
    bad = definition(tiles=[kpi()])

    r = boss.post("/dashboards/check", json={"dashboard_id": did, "definition": bad}, headers=CSRF)
    assert r.status_code == 200
    assert r.json()["problems"][0] == {
        "item": "revenue", "field": "dataset", "pos": None, "length": 0,
        "message": "The tile «revenue» reads the dataset «orders», which the dashboard does not have"}
    assert boss.post("/dashboards/check", json={"project_id": sales, "definition": definition()},
                     headers=CSRF).json() == {"problems": []}
    assert boss.get(f"/dashboards/{did}").json()["current_version"] == 1
    assert reader.post("/dashboards/check", json={"dashboard_id": did, "definition": bad}, headers=CSRF).status_code == 403
    assert reader.post("/dashboards/check", json={"project_id": sales, "definition": bad}, headers=CSRF).status_code == 403
    assert boss.post("/dashboards/check", json={"definition": bad}, headers=CSRF).status_code == 404
    # a body that is not a definition at all is a problem, not a server error
    r = boss.post("/dashboards/check", json={"project_id": sales, "definition": {"tiles": [1, 2]}}, headers=CSRF)
    assert r.status_code == 200 and r.json()["problems"]


# ── what a dashboard reads, deleted ───────────────────────────────────────────
def _object(session, what, sales, orders) -> int:
    if what == "view":
        v = SavedView(name="V", project_id=sales, datasource_id=orders, spec="{}")
        session.add(v)
        session.commit()
        return v.id
    if what == "sql_script":
        s = SqlScript(name="S", project_id=sales, current_version=1)
        session.add(s)
        session.commit()
        return s.id
    return orders if what == "datasource" else _connection(session, sales)


def _reading(what, target) -> str:
    source = {"view": {"kind": "view", "view_id": target}, "sql_script": {"kind": "sql_script", "script_id": target},
              "datasource": {"kind": "datasource", "datasource_id": target},
              "connection": {"kind": "sql", "sql": "SELECT 1", "connection_id": target}}[what]
    return json.dumps(definition([{"name": "orders", "source": source}]))


@pytest.mark.parametrize("what", ["view", "sql_script", "datasource", "connection"])
def test_what_a_dashboard_reads_is_not_deleted_and_the_answer_says_who(session, fake_engine, what):
    from app.routes.connections import delete_connection
    from app.routes.datasources import delete_datasource
    from app.routes.saved_views import delete_saved_view
    from app.routes.sql_scripts import delete_sql_script

    sales, vault = (make_project(session, name=n).id for n in ("Sales", "Vault"))
    orders = make_datasource(session, name="Orders", project_id=sales, key="datasets/o.parquet").id
    target = _object(session, what, sales, orders)
    call = {"view": delete_saved_view, "sql_script": delete_sql_script, "datasource": delete_datasource,
            "connection": delete_connection}[what]
    name = {"view": "view_id", "sql_script": "script_id", "datasource": "ds_id", "connection": "conn_id"}[what]
    boss = make_user(session, email="boss@x.local", is_superuser=True)
    colleague = make_user(session, email="ada@x.local")
    make_permission(session, user_id=colleague.id, project_id=sales, capability="manage")
    make_permission(session, user_id=colleague.id, project_id=sales, capability="connect")
    seen, hidden = (Dashboard(name=n, project_id=p, current_version=1) for n, p in (("Margin", sales), ("Payroll", vault)))
    session.add_all([seen, hidden])
    session.commit()
    content = _reading(what, target)
    session.add_all([DashboardVersion(dashboard_id=d.id, version=1, content=content) for d in (seen, hidden)])
    session.commit()

    with pytest.raises(HTTPException) as e:
        call(**{name: target}, request=None, user=colleague, session=session)
    assert e.value.status_code == 409
    message = e.value.detail["message"]
    assert "the dashboards «Margin» and 1 you cannot see" in message and "Payroll" not in message
    assert e.value.detail["dashboards"] == ["Margin"]
    with pytest.raises(HTTPException) as e:  # an administrator sees them all
        call(**{name: target}, request=None, user=boss, session=session)
    assert "the dashboards «Margin», «Payroll»:" in e.value.detail["message"]
    with pytest.raises(HTTPException) as e:  # one who sees none of them: no name at all
        dashboards.not_read_by_dashboards(session, make_user(session, email="z@x.local"), what, target, "It")
    assert e.value.detail["message"] == "It is read by 2 dashboards you cannot see: change them first"
    assert e.value.detail["dashboards"] == []
    # an older version does not hold it back: only what the dashboard reads now
    for d in (seen, hidden):
        session.add(DashboardVersion(dashboard_id=d.id, version=2, content=json.dumps(definition())))
        d.current_version = 2
        session.add(d)
    session.commit()
    assert dashboards.dashboards_reading(session, what, target) == []
    call(**{name: target}, request=None, user=boss, session=session)


def test_a_report_and_a_dashboard_reading_one_datasource_each_hold_it(session, fake_engine):
    from app.routes.datasources import delete_datasource

    sales = make_project(session, name="Sales").id
    orders = make_datasource(session, name="Orders", project_id=sales, key="datasets/o.parquet").id
    boss = make_user(session, email="boss@x.local", is_superuser=True)
    report = Report(name="Monthly", project_id=sales, current_version=1)
    dashboard = Dashboard(name="Live", project_id=sales, current_version=1)
    session.add_all([report, dashboard])
    session.commit()
    content = _reading("datasource", orders)
    session.add_all([ReportVersion(report_id=report.id, version=1, content=content),
                     DashboardVersion(dashboard_id=dashboard.id, version=1, content=content)])
    session.commit()
    with pytest.raises(HTTPException) as e:
        delete_datasource(ds_id=orders, request=None, user=boss, session=session)
    assert e.value.status_code == 409 and "«Monthly»" in e.value.detail["message"]
    session.delete(session.exec(select(ReportVersion)).one())
    session.delete(report)
    session.commit()
    with pytest.raises(HTTPException) as e:
        delete_datasource(ds_id=orders, request=None, user=boss, session=session)
    assert e.value.status_code == 409 and "«Live»" in e.value.detail["message"]


def test_a_folder_that_holds_a_dashboard_is_not_deleted_and_says_why(app, session):
    folder = make_project(session, name="F").id
    me = _sign_in(app, session, "boss@x.local", is_superuser=True)
    did = _create(me, folder, "D").json()["id"]
    r = me.delete(f"/projects/{folder}", headers=CSRF)
    assert r.status_code == 409 and r.json()["detail"] == "The folder contains dashboards: move or delete them first"
    assert me.delete(f"/dashboards/{did}", headers=CSRF).status_code == 204
    assert me.delete(f"/projects/{folder}", headers=CSRF).status_code in (200, 204)


# ── openings ─────────────────────────────────────────────────────────────────
def test_an_opening_counts_a_person_once_a_day_and_is_always_in_the_audit(app, session):
    sales = make_project(session, name="Sales").id
    boss, reader = _sign_in(app, session, "boss@x.local", is_superuser=True), _sign_in(app, session, "reader@x.local")
    make_permission(session, user_id=_ids(session)["reader@x.local"], project_id=sales, capability="view")
    did = _create(boss, sales, "D").json()["id"]
    assert reader.post(f"/dashboards/{did}/views", headers=CSRF).status_code == 204
    assert reader.post(f"/dashboards/{did}/views", headers=CSRF).status_code == 204
    session.expire_all()
    row = session.get(Dashboard, did)
    assert row.views_total == 1 and row.last_viewed_at is not None
    assert len(session.exec(select(DashboardView)).all()) == 1
    assert _actions(session) == ["dashboard.create", "dashboard.open", "dashboard.open"]
    opened = [a for a in session.exec(select(AuditLog)).all() if a.action == "dashboard.open"][0]
    assert opened.target_type == "dashboard" and opened.target_id == did and opened.target_label == "D"


def test_reading_a_dashboard_is_not_opening_it(app, session):
    """The designer and the tab read it too: the viewer says when someone looks at it."""
    sales = make_project(session, name="Sales").id
    boss = _sign_in(app, session, "boss@x.local", is_superuser=True)
    did = _create(boss, sales, "D").json()["id"]
    boss.get(f"/dashboards/{did}")
    assert _actions(session) == ["dashboard.create"]


def test_a_person_counts_again_the_next_day_of_the_installation(session):
    p = make_project(session, name="Sales").id
    d = Dashboard(name="D", project_id=p)
    session.add(d)
    session.commit()
    alice = make_user(session, email="alice@x.local")
    assert dashboards.record_open(session, d, alice, at=NOW)
    assert not dashboards.record_open(session, d, alice, at=NOW + timedelta(hours=3))
    assert dashboards.record_open(session, d, alice, at=NOW + timedelta(days=1))
    session.refresh(d)
    assert d.views_total == 2


def test_a_token_counts_nothing_and_is_not_an_opening(app, session):
    sales = make_project(session, name="Sales").id
    script = make_user(session, email="script@x.local")
    make_permission(session, user_id=script.id, project_id=sales, capability="view")
    d = Dashboard(name="D", project_id=sales)
    session.add(d)
    session.commit()
    did = d.id
    _, secret = api_tokens.create(session, script, "nightly", "write", 30, script)
    api = TestClient(app)
    auth = {"Authorization": f"Bearer {secret}"}
    assert api.get(f"/dashboards/{did}", headers=auth).status_code == 200
    assert api.post(f"/dashboards/{did}/views", headers=auth).status_code == 204
    session.expire_all()
    assert session.get(Dashboard, did).views_total == 0
    assert session.exec(select(DashboardView)).all() == []
    assert _actions(session) == []


def test_the_housekeeping_drops_the_old_openings_and_keeps_the_totals(session):
    p = make_project(session, name="Sales").id
    d = Dashboard(name="D", project_id=p)
    session.add(d)
    session.commit()
    alice = make_user(session, email="alice@x.local")
    for days in (0, 29, 30, 45):
        dashboards.record_open(session, d, alice, at=NOW - timedelta(days=days))
    assert housekeeping.dashboard_views(session, at=NOW) == 2  # the 30th and the 45th day before
    seen = sorted(v.day for v in session.exec(select(DashboardView)).all())
    assert seen == [(NOW - timedelta(days=29)).date(), NOW.date()]
    session.refresh(d)
    assert d.views_total == 4


def test_the_housekeeping_keeps_the_latest_versions_and_the_current_one(session):
    d = Dashboard(name="D", project_id=1, current_version=3)
    session.add(d)
    session.commit()
    for v in range(1, 6):
        session.add(DashboardVersion(dashboard_id=d.id, version=v, content="{}"))
    session.commit()
    assert housekeeping.dashboard_versions(session, 2) == 2  # v1, v2: v3 is the current one
    kept = session.exec(select(DashboardVersion).order_by(DashboardVersion.version)).all()
    assert [v.version for v in kept] == [3, 4, 5]


def test_the_openings_go_with_their_dashboard_where_foreign_keys_hold(fk_session):
    from app.routes.dashboards import delete_dashboard

    s = fk_session
    boss = make_user(s, email="boss@x.local", is_superuser=True)
    p = make_project(s, name="Sales", owner_id=boss.id)
    d = Dashboard(name="D", project_id=p.id, owner_id=boss.id)
    s.add(d)
    s.commit()
    s.add(DashboardVersion(dashboard_id=d.id, version=1, content="{}", created_by=boss.id))
    s.add(DashboardView(dashboard_id=d.id, user_id=boss.id, day=date(2026, 10, 30)))
    s.commit()
    did = d.id
    delete_dashboard(dashboard_id=did, request=None, user=boss, session=s)
    assert s.get(Dashboard, did) is None
    assert s.exec(select(DashboardVersion)).all() == [] and s.exec(select(DashboardView)).all() == []


# ── an account that goes ──────────────────────────────────────────────────────
def test_an_account_that_goes_leaves_the_dashboards_it_made_and_its_openings_go(fk_session):
    from app.routes.users import remove_user

    s = fk_session
    admin = make_user(s, email="admin@x.local", is_superuser=True)
    gone = make_user(s, email="gone@x.local")
    p = make_project(s, name="p", owner_id=admin.id)
    d = Dashboard(name="D", project_id=p.id, owner_id=gone.id, views_total=1)
    s.add(d)
    s.commit()
    s.add(DashboardVersion(dashboard_id=d.id, version=1, content="{}", created_by=gone.id))
    s.add(DashboardView(dashboard_id=d.id, user_id=gone.id, day=date(2026, 10, 28)))
    s.commit()
    did, gone_id = d.id, gone.id
    remove_user(s, gone_id)
    s.commit()
    s.expunge_all()
    kept = s.get(Dashboard, did)
    assert kept.owner_id is None and kept.views_total == 1
    assert s.exec(select(DashboardVersion)).one().created_by is None
    assert s.exec(select(DashboardView)).all() == []


# ── MCP's search leads to it ──────────────────────────────────────────────────
def test_the_assistant_s_search_links_a_dashboard_to_its_page():
    from app.services.agent.read_tools import _link

    assert _link("dashboard", 7, "D") == "/dashboard?id=7"


# ── the Dashboards tab ────────────────────────────────────────────────────────
def _on_board(session, name, project_id, description="", owner_id=None, updated_days_ago=0) -> Dashboard:
    at = datetime.now(timezone.utc) - timedelta(days=updated_days_ago)
    d = Dashboard(name=name, description=description, project_id=project_id, owner_id=owner_id, created_at=at, updated_at=at)
    session.add(d)
    session.commit()
    session.refresh(d)
    return d


def _names(page) -> list[str]:
    return [x["name"] for x in page["items"]]


def test_the_tab_puts_first_the_dashboards_read_by_the_most_people(app, session):
    sales = make_project(session, name="Sales").id
    boss = _sign_in(app, session, "boss@x.local", is_superuser=True)
    quiet = _on_board(session, "A quiet one", sales, updated_days_ago=9)
    _on_board(session, "B fresh, never opened", sales)
    popular = _on_board(session, "C popular", sales, updated_days_ago=9)
    busy = _on_board(session, "D one busy reader", sales, updated_days_ago=9)
    old_star = _on_board(session, "E read long ago", sales, updated_days_ago=9)
    readers = [make_user(session, email=f"reader{i}@x.local") for i in range(3)]
    now = datetime.now(timezone.utc)
    for u in readers:  # three people today
        dashboards.record_open(session, popular, u, at=now)
    for d in range(4):  # one person, four days
        dashboards.record_open(session, busy, readers[0], at=now - timedelta(days=d))
    dashboards.record_open(session, quiet, readers[1], at=now - timedelta(days=2))
    for d in range(40, 50):  # ten openings, all before the window
        dashboards.record_open(session, old_star, readers[2], at=now - timedelta(days=d))

    page = boss.get("/dashboards/search").json()
    assert page["total"] == 5
    # people first; at one person, the openings; at none in the window, the openings ever
    # (old_star's ten); then the latest changed
    assert _names(page) == ["C popular", "D one busy reader", "A quiet one", "E read long ago", "B fresh, never opened"]
    row = {x["name"]: x for x in page["items"]}
    assert (row["C popular"]["people_30d"], row["C popular"]["opens_30d"], row["C popular"]["views_total"]) == (3, 3, 3)
    assert (row["D one busy reader"]["people_30d"], row["D one busy reader"]["opens_30d"]) == (1, 4)
    assert (row["E read long ago"]["people_30d"], row["E read long ago"]["views_total"]) == (0, 10)
    assert row["B fresh, never opened"]["last_viewed_at"] is None and row["C popular"]["last_viewed_at"]
    assert _names(boss.get("/dashboards/search?sort=name").json())[0] == "A quiet one"
    assert _names(boss.get("/dashboards/search?sort=updated").json())[0] == "B fresh, never opened"
    assert boss.get("/dashboards/search?sort=popular").status_code == 422


def test_the_tab_shows_only_the_dashboards_one_sees(app, session):
    sales, hr = make_project(session, name="Sales").id, make_project(session, name="HR").id
    reader = _sign_in(app, session, "reader@x.local")
    nobody = _sign_in(app, session, "nobody@x.local")
    make_permission(session, user_id=_ids(session)["reader@x.local"], project_id=sales, capability="view")
    _on_board(session, "Monthly", sales)
    _on_board(session, "Salaries", hr)
    page = reader.get("/dashboards/search").json()
    assert page["total"] == 1 and _names(page) == ["Monthly"]
    assert not page["items"][0]["editable"]
    assert nobody.get("/dashboards/search").json() == {"items": [], "total": 0}
    # a search over what cannot be seen finds nothing, its folder's name included
    assert reader.get("/dashboards/search", params={"q": "salaries"}).json() == {"items": [], "total": 0}
    assert reader.get("/dashboards/search", params={"q": "hr"}).json() == {"items": [], "total": 0}


def test_the_tab_searches_name_description_and_folders_a_page_at_a_time(app, session):
    finance = make_project(session, name="Finance")
    close = make_project(session, name="Close", parent_id=finance.id).id
    ops = make_project(session, name="Operations").id
    boss = _sign_in(app, session, "boss@x.local", is_superuser=True)
    _on_board(session, "Margin", close)
    _on_board(session, "Stock", ops, description="Warehouse stock by finance code")
    _on_board(session, "Shipments", ops)
    _on_board(session, "100%_done", ops)
    found = lambda q: sorted(_names(boss.get("/dashboards/search", params={"q": q}).json()))
    assert found("finance") == ["Margin", "Stock"]  # a folder above it, a description
    assert found("CLOSE") == ["Margin"]  # its own folder, any case
    assert found("ship") == ["Shipments"]
    assert found("%") == found("_") == ["100%_done"]  # LIKE's characters are literal
    assert found("  ") == ["100%_done", "Margin", "Shipments", "Stock"]
    first = boss.get("/dashboards/search?sort=name&limit=3").json()
    second = boss.get("/dashboards/search?sort=name&limit=3&offset=3").json()
    assert first["total"] == second["total"] == 4
    assert _names(first) + _names(second) == ["100%_done", "Margin", "Shipments", "Stock"]


def test_a_row_of_the_tab_says_where_it_lives_who_made_it_and_its_size(app, session):
    root = make_project(session, name="Finance").id
    child = make_project(session, name="Close", parent_id=root).id
    boss = _sign_in(app, session, "boss@x.local", is_superuser=True)
    content = definition(tiles=[kpi()], datasets=[sql_dataset(sql="SELECT 1")],
                         parameters=[{"name": "region", "type": "text"}, {"name": "year", "type": "number"}])
    assert _create(boss, child, "Margin", content).status_code == 201
    row = boss.get("/dashboards/search").json()["items"][0]
    assert row["folder_path"] == ["Finance", "Close"]
    assert row["owner_name"] == "boss@x.local"
    assert (row["datasets"], row["parameters"], row["tiles"]) == (1, 2, 1)
    assert row["editable"] is True
    guest = _sign_in(app, session, "guest@x.local", is_observer=True)
    make_permission(session, user_id=_ids(session)["guest@x.local"], project_id=root, capability="view")
    assert guest.get("/dashboards/search").json()["items"][0]["owner_name"].startswith("b***@")
