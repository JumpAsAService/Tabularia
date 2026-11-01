"""A dashboard's numbers through the gateway (docs/specs/dashboards.md, «Reading the data»): the
numbers themselves are the engine's (backend/tests/test_dashboard_tiles.py). What these tests hold:
rights are the reader's, dataset by dataset, at every request, and one they may not read refuses
in its tiles only, saying nothing of its data, whichever way it reads; a report still stops at
the first one, as before; the parameters are a report's, values, `{none}`, choices and cascade;
`clicks` is refused until it filters; a read-only token reads a dashboard as
it reads a report; the preview needs EDIT and takes a definition not saved.
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db.session import get_session
from app.models import AuditLog, Connection, Dashboard, DashboardVersion, SavedView, SqlScript, SqlScriptVersion, User
from app.routes import dashboards as routes
from app.routes import reports as report_routes
from app.services import api_tokens, reports
from tests.conftest import make_datasource, make_permission, make_project, make_user
from tests.test_engine_settings import CSRF, _sign_in

PNG = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAEAQH/7yMK/gAAAABJRU5ErkJggg=="


@pytest.fixture
def app(session, fake_engine):
    from app.routes import auth as auth_routes

    fake_engine.placeholder_values = {"{today-30}": "2026-09-28"}
    app = FastAPI()
    for r in (auth_routes.router, routes.router, report_routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _tile(tile_id, type_, y, dataset=None, **props) -> dict:
    w, h = {"kpi": (3, 3), "chart": (6, 6), "table": (6, 6), "text": (3, 2), "image": (2, 2)}[type_]
    return {"id": tile_id, "type": type_, **({"dataset": dataset} if dataset else {}), "grid": {"x": 0, "y": y, "w": w, "h": h}, **props}


def kpi(tile_id, dataset, y=0, **props) -> dict:
    return _tile(tile_id, "kpi", y, dataset, value="COUNT()", **props)


def _world(session) -> dict:
    """«Sales» has the dashboard and the orders it reads; «Vault» the salaries; «Secret» a database;
    «Other» a view on the salaries, a script and a datasource of its own."""
    sales, vault, secret, other = (make_project(session, name=n).id for n in ("Sales", "Vault", "Secret", "Other"))
    cols = json.dumps([{"name": "region", "dtype": "String"}, {"name": "amount", "dtype": "Float64"}])
    orders = make_datasource(session, name="Orders", project_id=sales, key="datasets/o.parquet", columns=cols).id
    salaries = make_datasource(session, name="Salaries", project_id=vault, key="datasets/s.parquet",
                               columns=json.dumps([{"name": "pay_grade", "dtype": "Float64"}])).id
    conn = Connection(name="ERP", project_id=secret, db_type="postgresql", host="db", database="erp", username="reader",
                      password_encrypted="encrypted")
    view = SavedView(name="Low", project_id=vault, datasource_id=salaries, spec="{}")
    script = SqlScript(name="Pay", project_id=vault, current_version=1)
    session.add_all([conn, view, script])
    session.commit()
    session.add(SqlScriptVersion(script_id=script.id, version=1, content=json.dumps(
        {"sql": "SELECT * FROM \"Salaries\"", "engine": "polars"})))
    session.commit()
    return {"sales": sales, "vault": vault, "secret": secret, "other": other, "orders": orders, "salaries": salaries,
            "conn": conn.id, "view": view.id, "script": script.id}


def _dataset(name, datasource_id) -> dict:
    return {"name": name, "source": {"kind": "datasource", "datasource_id": datasource_id}}


def definition(w, *extra, parameters=()) -> dict:
    return {
        "v": 1, "locale": "it", "timezone": "Europe/Rome", "parameters": list(parameters),
        "datasets": [_dataset("orders", w["orders"]), *extra],
        "tiles": [kpi("mine", "orders"), *[kpi(f"on_{d['name']}", d["name"], y=(i + 1) * 3) for i, d in enumerate(extra)]],
    }


def _saved(session, w, content, name="Board") -> int:
    d = Dashboard(name=name, project_id=w["sales"], current_version=1)
    session.add(d)
    session.commit()
    session.add(DashboardVersion(dashboard_id=d.id, version=1, content=json.dumps(content)))
    session.commit()
    return d.id


def _uid(session, email) -> int:
    return session.exec(select(User).where(User.email == email)).one().id


def _reader(app, session, w, email="reader@x.local", capability="view") -> TestClient:
    client = _sign_in(app, session, email)
    make_permission(session, user_id=_uid(session, email), project_id=w["sales"], capability=capability)
    return client


def _data(client, dashboard, **body):
    return client.post(f"/dashboards/{dashboard}/data", json=body, headers=CSRF)


# ── what the engine receives, what comes back ─────────────────────────────────
def test_the_numbers_go_on_the_preview_way_with_the_definition_as_a_report_s(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w))
    r = _data(_reader(app, session, w), board)
    assert r.status_code == 200, r.text
    assert r.json()["tiles"]["mine"]["value"] == 1 and r.json()["truncated"] == {"orders": False}
    body = fake_engine.report_tiles[-1]
    assert body["definition"]["sections"]["body"]["items"][0]["id"] == "mine"
    assert "tiles" not in body["definition"] and body["locale"] == "it" and body["title"] == "Board"
    assert body["datasets"]["orders"]["input_key"] == "datasets/o.parquet" and body["refused"] == {}
    assert body["cache"] == {"refresh_seconds": 0} and "slot" not in body  # a saved one's numbers are shared


def test_a_tile_keeps_its_title_and_an_image_its_picture_which_the_engine_never_sees(app, session, fake_engine):
    w = _world(session)
    content = definition(w)
    content["tiles"] += [_tile("logo", "image", 10, asset=PNG, fit="cover", title="Our logo")]
    content["tiles"][0]["label"] = "Revenue"
    fake_engine.report_tiles_response = (200, {"tiles": {"mine": {"type": "kpi", "value": 1}, "logo": {"type": "image", "fit": "cover"}},
                                               "truncated": {}})
    tiles = _data(_reader(app, session, w), _saved(session, w, content)).json()["tiles"]
    assert tiles["mine"]["title"] == "Revenue"
    assert tiles["logo"]["src"] == PNG and tiles["logo"]["title"] == "Our logo" and tiles["logo"]["fit"] == "cover"
    assert PNG not in json.dumps(fake_engine.report_tiles[-1])


def test_tiles_asks_for_some_and_a_page_goes_with_its_tile(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w, _dataset("more", w["orders"])))
    reader = _reader(app, session, w)
    state = {"items": {"on_more": {"sort": {"column": 0, "dir": "desc"}}}}
    r = _data(reader, board, tiles=["on_more"], page={"on_more": 3}, state=state)
    assert list(r.json()["tiles"]) == ["on_more"]
    body = fake_engine.report_tiles[-1]
    assert body["tiles"] == ["on_more"] and body["pages"] == {"on_more": 3} and body["state"] == state


def test_a_page_for_some_tiles_does_not_knock_down_the_opening_of_all_of_them(app, session, fake_engine):
    w = _world(session)
    draft = definition(w, _dataset("more", w["orders"]))
    reader = _reader(app, session, w, capability="edit")
    for body in ({}, {"tiles": ["on_more"]}, {"tiles": ["on_more"], "page": {"on_more": 1}}):
        assert reader.post("/dashboards/preview", headers=CSRF, json={"definition": draft, "project_id": w["sales"], **body}).status_code == 200
    slots = [b["slot"] for b in fake_engine.report_tiles]
    assert slots[0] != slots[1] and slots[1] == slots[2]


@pytest.mark.parametrize("body, says", [
    ({"tiles": ["nope"]}, "no tile «nope»"), ({"page": {"nope": 1}}, "no tile «nope»"),
    ({"page": {"mine": -1}}, "a page is 0 or more"), ({"clicks": [{"tile": "mine", "values": ["x"]}]}, "cannot filter"),
    ({"state": {"items": "x"}}, "state"), ({"state": {"items": {"mine": {"x": "y" * 70000}}}}, "too large"),
])
def test_what_a_browser_may_not_ask_is_a_422_before_anything_is_read(app, session, fake_engine, body, says):
    w = _world(session)
    r = _data(_reader(app, session, w), _saved(session, w, definition(w)), **body)
    assert r.status_code == 422 and says in r.text
    assert fake_engine.report_tiles == []


def test_a_list_of_tiles_longer_than_a_dashboard_holds_is_refused(app, session, fake_engine):
    w = _world(session)
    r = _data(_reader(app, session, w), _saved(session, w, definition(w)), tiles=["mine"] * 41)
    assert r.status_code == 422


def test_a_click_and_a_read_of_the_numbers_are_not_audited(app, session, fake_engine):
    w = _world(session)
    _data(_reader(app, session, w), _saved(session, w, definition(w)))
    assert [a for a in session.exec(select(AuditLog)).all() if a.action.startswith("dashboard.")] == []


# ── failures of the whole request ─────────────────────────────────────────────
def test_a_request_knocked_down_says_so(app, session, fake_engine):
    w = _world(session)
    fake_engine.report_tiles_response = (409, {"detail": "Preview superseded by a more recent request"})
    assert _data(_reader(app, session, w), _saved(session, w, definition(w))).status_code == 409


def test_an_engine_that_takes_too_long_is_a_504(app, session, fake_engine):
    w = _world(session)
    fake_engine.report_tiles_response = (504, {"detail": "Preview timed out"})
    assert _data(_reader(app, session, w), _saved(session, w, definition(w))).status_code == 504


def test_an_engine_older_than_the_gateway_says_so(app, session, fake_engine):
    w = _world(session)
    fake_engine.report_tiles_response = (404, {"detail": "Not Found"})
    r = _data(_reader(app, session, w), _saved(session, w, definition(w)))
    assert r.status_code == 503 and "update it" in r.text


# ── rights, tile by tile ──────────────────────────────────────────────────────
def _no_leak(tile: dict, *words) -> None:
    assert set(tile) == {"error", "code"} and tile["code"] == "no_access"
    assert not any(c.isdigit() for c in tile["error"])
    for word in words:
        assert word not in tile["error"]


def test_a_dataset_the_reader_may_not_read_refuses_only_in_its_tile(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w, _dataset("pay", w["salaries"])))
    r = _data(_reader(app, session, w), board)
    assert r.status_code == 200
    tiles = r.json()["tiles"]
    _no_leak(tiles["on_pay"], "pay_grade", "Salaries", "datasets/s.parquet")
    assert tiles["mine"]["value"] == 1
    body = fake_engine.report_tiles[-1]
    # nothing of it reaches the engine, and the answer does not even say how large it is
    assert "pay" not in body["datasets"] and body["refused"]["pay"]["code"] == "no_access"
    assert "datasets/s.parquet" not in json.dumps(body["datasets"])
    assert r.json()["truncated"] == {"orders": False}


@pytest.mark.parametrize("source", [
    lambda w: {"kind": "view", "view_id": w["view"]},
    lambda w: {"kind": "sql_script", "script_id": w["script"]},
    lambda w: {"kind": "sql", "sql": "SELECT pay_grade FROM \"Salaries\""},
    lambda w: {"kind": "sql", "connection_id": w["conn"], "sql": "SELECT pay_grade FROM payroll"},
    lambda w: {"kind": "datasource", "datasource_id": 999999},
], ids=["view", "script", "sql-on-the-catalog", "sql-on-a-connection", "gone"])
def test_every_way_of_reading_refuses_the_same_way(app, session, fake_engine, source):
    w = _world(session)
    board = _saved(session, w, definition(w, {"name": "pay", "source": source(w)}))
    tiles = _data(_reader(app, session, w), board).json()["tiles"]
    _no_leak(tiles["on_pay"], "pay_grade", "Salaries", "payroll")
    assert tiles["mine"]["value"] == 1


def test_with_the_right_the_same_tile_reads(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w, _dataset("pay", w["salaries"])))
    reader = _reader(app, session, w)
    assert _data(reader, board).json()["tiles"]["on_pay"]["code"] == "no_access"
    make_permission(session, user_id=_uid(session, "reader@x.local"), project_id=w["vault"], capability="view")
    assert _data(reader, board).json()["tiles"]["on_pay"]["value"] == 1


def test_rights_are_checked_at_every_request_taking_one_away_refuses_the_next(app, session, fake_engine):
    from app.models import Permission

    w = _world(session)
    board = _saved(session, w, definition(w, _dataset("pay", w["salaries"])))
    reader = _reader(app, session, w)
    uid = _uid(session, "reader@x.local")
    make_permission(session, user_id=uid, project_id=w["vault"], capability="view")
    assert _data(reader, board).json()["tiles"]["on_pay"]["value"] == 1
    for p in session.exec(select(Permission).where(Permission.user_id == uid, Permission.project_id == w["vault"])).all():
        session.delete(p)
    session.commit()
    assert _data(reader, board).json()["tiles"]["on_pay"]["code"] == "no_access"


def test_a_lookup_on_a_refused_dataset_reaches_the_engine_as_refused(app, session, fake_engine):
    """The tile's reach through its expressions is the engine's: it needs to be told the dataset is refused."""
    w = _world(session)
    content = definition(w, _dataset("pay", w["salaries"]))
    content["tiles"][0]["value"] = 'LOOKUP("x", [a], [b], "pay")'
    _data(_reader(app, session, w), _saved(session, w, content))
    assert fake_engine.report_tiles[-1]["refused"]["pay"]["code"] == "no_access"


def test_trouble_of_another_kind_is_said_in_the_tile_as_its_own(app, session, fake_engine):
    w = _world(session)
    empty = make_datasource(session, name="Empty", project_id=w["sales"], key="").id
    tiles = _data(_reader(app, session, w), _saved(session, w, definition(w, _dataset("void", empty)))).json()["tiles"]
    assert tiles["on_void"]["code"] == "dataset" and "no data yet" in tiles["on_void"]["error"]
    assert tiles["mine"]["value"] == 1


def test_who_cannot_see_the_dashboard_gets_what_does_not_exist(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w))
    stranger = _sign_in(app, session, "stranger@x.local")
    assert _data(stranger, board).status_code == 404
    assert _data(stranger, 999999).status_code == 404
    assert stranger.get(f"/dashboards/{board}/parameters").status_code == 404
    assert fake_engine.report_tiles == []


async def _report_still_stops(session, user, definition_) -> str:
    try:
        await reports.resolve_datasets(session, user, definition_, {})
    except Exception as e:  # noqa: BLE001
        return f"{e.status_code}: {e.detail}"
    return "read"


@pytest.mark.anyio
async def test_a_report_still_stops_at_the_first_dataset_it_cannot_read(session, fake_engine):
    w = _world(session)
    user = make_user(session, email="r@x.local")
    make_permission(session, user_id=user.id, project_id=w["sales"], capability="view")
    said = await _report_still_stops(session, user, {"datasets": [_dataset("orders", w["orders"]), _dataset("pay", w["salaries"])]})
    assert said.startswith("422: The dataset «pay»: it reads a datasource") and "cannot read" in said


# ── parameters, as a report's ─────────────────────────────────────────────────
PARAMETERS = [{"name": "region", "type": "text", "required": True, "choices": {"values": ["North", "South"]}},
              {"name": "since", "type": "date", "default": "{today-30}"},
              {"name": "note", "type": "text", "default": "any"}]


def test_a_value_is_checked_as_for_a_report_and_goes_converted(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w, parameters=PARAMETERS))
    reader = _reader(app, session, w)
    r = _data(reader, board)
    assert r.status_code == 400 and r.json()["detail"]["parameter"] == "region"
    r = _data(reader, board, params={"region": "West"})
    assert r.status_code == 400 and "not one of its choices" in r.json()["detail"]["message"]
    r = _data(reader, board, params={"region": "North", "since": "soon"})
    assert r.status_code == 400 and r.json()["detail"]["parameter"] == "since"
    r = _data(reader, board, params={"region": "North", "nope": "x"})
    assert r.status_code == 400 and "the dashboard has no parameter" in r.json()["detail"]["message"]
    r = _data(reader, board, params={"region": {"a": 1}})
    assert r.status_code == 400
    assert fake_engine.report_tiles == []
    assert _data(reader, board, params={"region": "North", "since": "2026-01-02"}).status_code == 200
    assert fake_engine.report_tiles[-1]["params"] == {"region": "North", "since": "2026-01-02", "note": "any"}


def test_none_says_no_value_whatever_the_default(app, session, fake_engine):
    w = _world(session)
    content = definition(w, parameters=PARAMETERS[1:])
    content["datasets"][0]["source"]["filters"] = [{"column": "region", "op": "=", "value": "{note}"}]
    board = _saved(session, w, content)
    reader = _reader(app, session, w)
    _data(reader, board)
    assert fake_engine.report_tiles[-1]["datasets"]["orders"]["operations"][0]["params"]["value"] == "any"
    _data(reader, board, params={"note": "{none}"})
    body = fake_engine.report_tiles[-1]
    assert body["params"]["note"] is None and body["datasets"]["orders"]["operations"] == []


def test_a_parameter_filters_its_dataset_as_in_a_report(app, session, fake_engine):
    w = _world(session)
    content = definition(w, parameters=PARAMETERS)
    content["datasets"][0]["source"]["filters"] = [{"column": "region", "op": "=", "value": "{region}"}]
    _data(_reader(app, session, w), _saved(session, w, content), params={"region": "South"})
    assert fake_engine.report_tiles[-1]["datasets"]["orders"]["operations"][0]["params"] == \
        {"column": "region", "operator": "eq", "value": "South"}


def _choosing(w) -> dict:
    """A region and an agent, both from the dataset both filter: the agents follow the region."""
    content = definition(w, parameters=[
        {"name": "region", "type": "text", "choices": {"dataset": "orders", "value": "[region]"}},
        {"name": "agent", "type": "text", "choices": {"dataset": "orders", "value": "[agent]"}}])
    content["datasets"][0]["source"]["filters"] = [{"column": "region", "op": "=", "value": "{region}"},
                                                   {"column": "agent", "op": "=", "value": "{agent}"}]
    return content


def test_the_choices_read_the_dataset_they_filter_and_cascade(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, _choosing(w))
    reader = _reader(app, session, w)
    fake_engine.report_choices_response = (200, {"choices": [{"value": "North", "label": "North"}], "truncated": False})
    out = {p["name"]: p for p in reader.get(f"/dashboards/{board}/parameters?region=North").json()}
    assert out["region"]["choices"] == [{"value": "North", "label": "North"}]
    first, second = fake_engine.report_choices[0], fake_engine.report_choices[1]
    assert first["dataset"] == "orders" and first["datasets"]["orders"]["operations"] == []  # the region's list: no filter of its own
    # the agent's list follows the region, and the engine reads a definition of a report's shape
    assert second["datasets"]["orders"]["operations"][0]["params"]["value"] == "North"
    assert "tiles" not in second["definition"] and "sections" in second["definition"]


def test_a_value_must_be_among_the_choices_of_its_dataset(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, _choosing(w))
    reader = _reader(app, session, w)
    fake_engine.report_choices_response = (200, {"choices": [{"value": "North", "label": "North"}], "truncated": False})
    r = _data(reader, board, params={"region": "West"})
    assert r.status_code == 400 and r.json()["detail"]["parameter"] == "region"
    assert fake_engine.report_tiles == []
    assert _data(reader, board, params={"region": "North"}).status_code == 200


def test_choices_from_a_dataset_the_reader_may_not_read_say_so_and_leak_nothing(app, session, fake_engine):
    w = _world(session)
    content = definition(w, _dataset("pay", w["salaries"]), parameters=[
        {"name": "grade", "type": "text", "choices": {"dataset": "pay", "value": "[pay_grade]"}}])
    board = _saved(session, w, content)
    reader = _reader(app, session, w)
    fake_engine.report_choices_response = (200, {"choices": [{"value": "top", "label": "top"}], "truncated": False})
    out = reader.get(f"/dashboards/{board}/parameters").json()[0]
    assert out["choices"] is None and "pay_grade" not in out["choices_error"] and "Salaries" not in out["choices_error"]
    assert fake_engine.report_choices == []
    # its value checks nothing against a list they may not read, and tells nothing of it; the tiles still work
    r = _data(reader, board, params={"grade": "guess"})
    assert r.status_code == 200 and r.json()["tiles"]["mine"]["value"] == 1 and r.json()["tiles"]["on_pay"]["code"] == "no_access"


def test_the_choices_of_a_definition_being_edited_need_edit(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w))
    draft = _choosing(w)
    viewer = _reader(app, session, w)
    assert viewer.post(f"/dashboards/{board}/parameters", json={"definition": draft}, headers=CSRF).status_code == 403
    editor = _reader(app, session, w, "editor@x.local", "edit")
    fake_engine.report_choices_response = (200, {"choices": [{"value": "N", "label": "North"}], "truncated": False})
    r = editor.post(f"/dashboards/{board}/parameters", json={"definition": draft, "params": {"region": "N"}}, headers=CSRF)
    assert r.status_code == 200 and r.json()[0]["choices"] == [{"value": "N", "label": "North"}]
    assert fake_engine.report_choices[-1]["datasets"]["orders"]["operations"][0]["params"]["value"] == "N"
    # half done elsewhere, whole where the form looks: no tile needed
    draft["tiles"] = [{"id": "half"}]
    assert editor.post(f"/dashboards/{board}/parameters", json={"definition": draft}, headers=CSRF).status_code == 200
    many = {**draft, "parameters": [{"name": f"p{i}", "type": "text"} for i in range(21)]}
    r = editor.post(f"/dashboards/{board}/parameters", json={"definition": many}, headers=CSRF)
    assert r.status_code == 422 and "at most 20" in r.text
    r = editor.post(f"/dashboards/{board}/parameters", json={"definition": draft, "params": {"nope": "x"}}, headers=CSRF)
    assert r.status_code == 400 and "the dashboard has no parameter" in r.text


# ── the preview ───────────────────────────────────────────────────────────────
def _preview(client, **body):
    return client.post("/dashboards/preview", json=body, headers=CSRF)


def test_the_preview_takes_a_definition_not_saved_and_needs_edit(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w))
    draft = definition(w)
    draft["tiles"].append(kpi("new", "orders", y=9))
    viewer = _reader(app, session, w)
    assert _preview(viewer, definition=draft, dashboard_id=board).status_code == 403
    assert _preview(viewer, definition=draft, project_id=w["sales"]).status_code == 403
    assert _preview(viewer, definition=draft).status_code == 404
    assert fake_engine.report_tiles == []
    editor = _reader(app, session, w, "editor@x.local", "edit")
    r = _preview(editor, definition=draft, dashboard_id=board)
    assert r.status_code == 200 and sorted(r.json()["tiles"]) == ["mine", "new"]
    assert fake_engine.report_tiles[-1]["slot"].split(":")[2] == f"preview-{board}"
    r = _preview(editor, definition=draft, project_id=w["sales"], tiles=["new"])
    assert list(r.json()["tiles"]) == ["new"]
    # nothing was saved
    assert session.exec(select(DashboardVersion).where(DashboardVersion.dashboard_id == board)).one().version == 1


def test_the_preview_says_the_problems_of_a_definition_by_tile(app, session, fake_engine):
    w = _world(session)
    draft = definition(w)
    draft["tiles"][0]["grid"]["w"] = 1
    r = _preview(_reader(app, session, w, capability="edit"), definition=draft, project_id=w["sales"])
    assert r.status_code == 422 and r.json()["detail"]["problems"][0]["item"] == "mine"
    assert fake_engine.report_tiles == []


def test_the_preview_cannot_point_at_what_its_author_may_not_read(app, session, fake_engine):
    w = _world(session)
    r = _preview(_reader(app, session, w, capability="edit"), definition=definition(w, _dataset("pay", w["salaries"])),
                 project_id=w["sales"])
    assert r.status_code == 422 and "pay_grade" not in r.text
    assert fake_engine.report_tiles == []


def test_the_preview_of_a_dashboard_refuses_in_the_tile_what_it_already_read(app, session, fake_engine):
    """Another's saved dashboard, a dataset this editor may not read: their live view works on the rest."""
    w = _world(session)
    content = definition(w, _dataset("pay", w["salaries"]))
    board = _saved(session, w, content)
    r = _preview(_reader(app, session, w, capability="edit"), definition=content, dashboard_id=board)
    assert r.status_code == 200
    _no_leak(r.json()["tiles"]["on_pay"], "pay_grade", "Salaries")
    assert r.json()["tiles"]["mine"]["value"] == 1


def test_the_preview_refuses_a_click_that_cannot_filter_and_unknown_tiles(app, session, fake_engine):
    w = _world(session)
    editor = _reader(app, session, w, capability="edit")
    draft = definition(w)
    assert _preview(editor, definition=draft, project_id=w["sales"], clicks=[{"tile": "mine", "values": ["x"]}]).status_code == 422
    assert _preview(editor, definition=draft, project_id=w["sales"], tiles=["nope"]).status_code == 422


# ── an API token ──────────────────────────────────────────────────────────────
def _token(session, email, scope="read", **kw) -> tuple[str, int]:
    user = session.exec(select(User).where(User.email == email)).first() or make_user(session, email=email, **kw)
    row, secret = api_tokens.create(session, user, "nightly", scope, 30, user)
    return secret, row.id


def _bearer(token) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_a_read_only_token_reads_a_dashboard_as_it_reads_a_report(app, session, fake_engine):
    w = _world(session)
    board = _saved(session, w, definition(w, _dataset("pay", w["salaries"])))
    read, read_id = _token(session, "reader@x.local")
    make_permission(session, user_id=_uid(session, "reader@x.local"), project_id=w["sales"], capability="view")
    api = TestClient(app)
    r = api.post(f"/dashboards/{board}/data", json={}, headers=_bearer(read))
    assert r.status_code == 200 and r.json()["tiles"]["mine"]["value"] == 1
    assert r.json()["tiles"]["on_pay"]["code"] == "no_access"  # with its owner's rights
    assert api.get(f"/dashboards/{board}/parameters", headers=_bearer(read)).status_code == 200
    # what it changes or edits stays closed
    assert api.post("/dashboards/preview", json={"definition": definition(w), "dashboard_id": board}, headers=_bearer(read)).status_code == 403
    assert api.post(f"/dashboards/{board}/parameters", json={"definition": definition(w)}, headers=_bearer(read)).status_code == 403
    assert api.delete(f"/dashboards/{board}", headers=_bearer(read)).status_code == 403
    # a stranger's sees nothing, a revoked one is no token, nor is an observer's personal one
    stranger, _ = _token(session, "stranger@x.local")
    assert api.post(f"/dashboards/{board}/data", json={}, headers=_bearer(stranger)).status_code == 404
    api_tokens.revoke(session, read_id)
    assert api.post(f"/dashboards/{board}/data", json={}, headers=_bearer(read)).status_code == 401
    observer, _ = _token(session, "observer@x.local", is_observer=True)
    assert api.post(f"/dashboards/{board}/data", json={}, headers=_bearer(observer)).status_code == 401


def test_the_field_limits_are_a_report_s(app, session, fake_engine):
    """A text of a parameter, a list of values: what a report takes, and no more."""
    w = _world(session)
    board = _saved(session, w, definition(w, parameters=[{"name": "note", "type": "text"}, {"name": "many", "type": "text", "multi": True}]))
    reader = _reader(app, session, w)
    r = _data(reader, board, params={"note": "x" * 1001})
    assert r.status_code == 400 and "at most 1000" in r.text
    r = _data(reader, board, params={"many": ["x"] * 1001})
    assert r.status_code == 400 and "at most 1000" in r.text
    assert fake_engine.report_tiles == []
