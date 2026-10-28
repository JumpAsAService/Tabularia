"""Checking a report's definition while it is edited, and what the editor and the API are
told (docs/specs/reports.md, «Gateway API»; the designer's contract of 2026-10-28).

The rules these tests hold: POST /reports/check answers the problems a save would give,
without saving — the gateway's and the engine's, each place once, each as the engine
says its own ({item, field, pos, length, message}) — and needs EDIT; the preview checks
the same before it renders, and refuses with that list; a refused save lists every
problem in the same form; the engine's words start in capitals when the gateway says
them; REPORT() is the report's name as written; a save written on an old version is
refused when another came since; two creations with one name at once give a 409, not a
500; a caller of the API that stops waiting stops the render.
"""
import asyncio
import json

import pytest
from fastapi import FastAPI, HTTPException
from sqlmodel import select

from app.db.session import get_session
from app.models import AuditLog, Report, ReportVersion
from app.routes import reports as routes
from tests.conftest import make_datasource, make_permission, make_project
from tests.test_engine_settings import CSRF, _sign_in
from tests.test_reports_object import _ids, definition, sql_dataset


@pytest.fixture
def app(session, fake_engine):
    from app.routes import auth as auth_routes

    app = FastAPI()
    for r in (auth_routes.router, routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


def _text(id_="t", value="=SUMM([amount])", **kw) -> dict:
    return {"id": id_, "type": "text", "x": 0, "y": 0, "w": 50, "h": 10, "value": value, **kw}


def _folder_and_editor(app, session, email="ed@x.local", capability="edit"):
    folder = make_project(session, name="Sales").id
    client = _sign_in(app, session, email)
    make_permission(session, user_id=_ids(session)[email], project_id=folder, capability=capability)
    return folder, client


# ── the live check ────────────────────────────────────────────────────────────
def test_the_check_says_the_gateway_s_and_the_engine_s_problems_without_saving(app, session, fake_engine):
    folder, editor = _folder_and_editor(app, session)
    make_datasource(session, name="Orders", project_id=folder)
    # a text past the right margin (the gateway) and an unknown function (the engine)
    content = definition(sql_dataset(), items=[_text(x=150), _text("u", "=[nope]", y=20)])
    fake_engine.report_check_response = (200, {"problems": [
        {"item": "t", "field": "value", "pos": 1, "length": 4, "message": "there is no function SUMM"},
        {"item": "u", "field": "value", "pos": 1, "length": 6, "message": "there is no field [nope]"}]})
    r = editor.post("/reports/check", json={"project_id": folder, "definition": content}, headers=CSRF)
    assert r.status_code == 200, r.text
    assert r.json() == {"problems": [
        {"item": "t", "field": "", "pos": None, "length": 0, "message": "The item «t» goes past the right margin by 14.0 mm"},
        {"item": "t", "field": "value", "pos": 1, "length": 4, "message": "There is no function SUMM"},
        {"item": "u", "field": "value", "pos": 1, "length": 6, "message": "There is no field [nope]"}]}
    # the engine read the definition as it is, its images left out
    assert fake_engine.report_checks[-1]["definition"]["sections"]["body"]["items"][1]["value"] == "=[nope]"
    assert session.exec(select(Report)).all() == [] and fake_engine.report_renders == []
    # nothing wrong: no problems; an engine that cannot answer: the gateway's only
    fake_engine.report_check_response = (200, {"problems": []})
    good = definition(sql_dataset(), items=[_text(value="Title")])
    assert editor.post("/reports/check", json={"project_id": folder, "definition": good},
                       headers=CSRF).json() == {"problems": []}
    fake_engine.report_check_response = (503, {"detail": "busy"})
    assert [p["field"] for p in editor.post("/reports/check", json={"project_id": folder, "definition": content},
                                            headers=CSRF).json()["problems"]] == [""]


def test_the_check_names_a_dataset_and_a_parameter_by_name_and_says_a_place_once(app, session, fake_engine):
    folder, editor = _folder_and_editor(app, session)
    region = {"name": "region", "type": "text"}
    content = definition(sql_dataset(sql="SELECT * FROM \"Orders\" WHERE r = '{region}'"), parameters=[region])
    fake_engine.report_check_response = (200, {"problems": [
        {"item": None, "field": "datasets[orders].source", "pos": None, "length": 0, "message": "the same place"},
        {"item": None, "field": "parameters[region].default", "pos": None, "length": 0, "message": "a default"}]})
    problems = editor.post("/reports/check", json={"project_id": folder, "definition": content},
                           headers=CSRF).json()["problems"]
    assert problems == [
        {"item": None, "field": "datasets[orders].source", "pos": None, "length": 0,
         "message": "The dataset «orders»: write {region} without quotes: Tabularia quotes it"},
        {"item": None, "field": "parameters[region].default", "pos": None, "length": 0, "message": "A default"}]


def test_the_check_needs_edit_on_the_folder(app, session, fake_engine):
    folder, reader = _folder_and_editor(app, session, "reader@x.local", "view")
    stranger = _sign_in(app, session, "other@x.local")
    content = definition()
    me = _sign_in(app, session, "boss@x.local", is_superuser=True)
    rid = me.post(f"/projects/{folder}/reports", json={"name": "R", "content": content}, headers=CSRF).json()["id"]
    for body in ({"project_id": folder}, {"report_id": rid}):
        assert reader.post("/reports/check", json={**body, "definition": content}, headers=CSRF).status_code == 403
    assert stranger.post("/reports/check", json={"report_id": rid, "definition": content}, headers=CSRF).status_code == 404
    assert me.post("/reports/check", json={"definition": content}, headers=CSRF).status_code == 404
    assert me.post("/reports/check", json={"project_id": 999, "definition": content}, headers=CSRF).status_code == 404
    assert me.post("/reports/check", json={"report_id": rid, "definition": content}, headers=CSRF).json() == \
        {"problems": []}


def test_the_engine_checks_a_datasource_s_fields_against_its_columns(app, session, fake_engine):
    folder, editor = _folder_and_editor(app, session)
    vault = make_project(session, name="Vault").id
    cols = json.dumps([{"name": "region", "dtype": "String"}, {"name": "amount", "dtype": "Float64"}])
    orders = make_datasource(session, name="Orders", project_id=folder, columns=cols).id
    salaries = make_datasource(session, name="Salaries", project_id=vault, columns=cols).id
    fake_engine.report_check_response = (200, {"problems": []})
    content = definition({"name": "o", "source": {"kind": "datasource", "datasource_id": orders}},
                         {"name": "s", "source": {"kind": "sql", "sql": "SELECT 1"}})
    editor.post("/reports/check", json={"project_id": folder, "definition": content}, headers=CSRF)
    # the columns the gateway knows, of what this person may read; a SQL keeps the editor's fields
    assert fake_engine.report_checks[-1]["columns"] == {"o": {"region": "string", "amount": "float"}}
    hidden = definition({"name": "x", "source": {"kind": "datasource", "datasource_id": salaries}})
    r = editor.post("/reports/check", json={"project_id": folder, "definition": hidden}, headers=CSRF)
    assert "columns" not in fake_engine.report_checks[-1]
    assert "cannot read" in r.json()["problems"][0]["message"]


# ── the preview and the save ──────────────────────────────────────────────────
def test_the_preview_refuses_a_definition_with_problems_before_rendering(app, session, fake_engine):
    folder, editor = _folder_and_editor(app, session)
    make_datasource(session, name="Orders", project_id=folder)
    fake_engine.report_check_response = (200, {"problems": [
        {"item": "t", "field": "value", "pos": 1, "length": 4, "message": "there is no function SUMM"}]})
    r = editor.post("/reports/preview", json={"project_id": folder, "definition": definition(
        sql_dataset(), items=[_text()])}, headers=CSRF)
    assert r.status_code == 422, r.text
    said = {"item": "t", "field": "value", "pos": 1, "length": 4, "message": "There is no function SUMM"}
    assert r.json()["detail"] == {"message": "There is no function SUMM", "where": "value", "item": "t",
                                  "errors": [said], "problems": [said]}
    # the gateway's own: the same form, the item's property relative to it
    fake_engine.report_check_response = (200, {"problems": []})
    r = editor.post("/reports/preview", json={"project_id": folder, "definition": definition(
        sql_dataset(), items=[_text(value="x", w=500)])}, headers=CSRF)
    assert r.status_code == 422
    assert r.json()["detail"]["problems"] == [{"item": "t", "field": "w", "pos": None, "length": 0,
                                               "message": "The item «t»'s width is between 0.1 and 186"}]
    assert fake_engine.report_renders == []
    assert session.exec(select(AuditLog).where(AuditLog.action == "report.render")).all() == []
    # a good one renders; what only the data says (the engine's refusal) is said as a render says it
    r = editor.post("/reports/preview", json={"project_id": folder, "definition": definition(
        sql_dataset(), items=[_text(value="x")])}, headers=CSRF)
    assert r.status_code == 200 and len(fake_engine.report_renders) == 1
    fake_engine.report_render_response = (422, {"detail": "the dataset «orders» has 10,001 rows: at most 10,000"})
    r = editor.post("/reports/preview", json={"project_id": folder, "definition": definition(
        sql_dataset(), items=[_text(value="x")])}, headers=CSRF)
    assert r.status_code == 422 and r.json()["detail"] == "The dataset «orders» has 10,001 rows: at most 10,000"


def test_a_refused_save_lists_its_problems_as_the_check_does(app, session, fake_engine):
    folder, editor = _folder_and_editor(app, session)
    fake_engine.report_check_response = (200, {"problems": [
        {"item": "u", "field": "value", "pos": 1, "length": 6, "message": "there is no field [nope]"}]})
    content = definition(sql_dataset(), items=[_text(x=150), _text("u", "=[nope]", y=20)])
    r = editor.post(f"/projects/{folder}/reports", json={"name": "R", "content": content}, headers=CSRF)
    assert r.status_code == 422
    detail = r.json()["detail"]
    assert (detail["message"], detail["where"], detail["item"]) == (
        "The item «t» goes past the right margin by 14.0 mm", "sections.body.items[0]", "t")
    assert [(p["item"], p["field"]) for p in detail["problems"]] == [("t", ""), ("u", "value")]
    assert detail["errors"] == detail["problems"]
    check = editor.post("/reports/check", json={"project_id": folder, "definition": content}, headers=CSRF).json()
    assert check["problems"] == detail["problems"]


# ── what the engine is told, and says ─────────────────────────────────────────
def test_report_is_the_name_as_written_and_the_engine_s_words_start_in_capitals(app, session, fake_engine):
    folder, editor = _folder_and_editor(app, session)
    rid = editor.post(f"/projects/{folder}/reports", json={"name": "Margin: Q1/2026", "content": definition()},
                      headers=CSRF).json()["id"]
    r = editor.post(f"/reports/{rid}/render", json={"format": "pdf"}, headers=CSRF)
    assert r.json()["filename"] == "Margin Q1 2026.pdf"
    assert fake_engine.report_renders[-1]["title"] == "Margin: Q1/2026"
    fake_engine.report_render_response = (422, {"detail": "the report's definition: item «k1», value: there is no "
                                                          "field [x] (at character 5)"})
    r = editor.post(f"/reports/{rid}/render", json={"format": "pdf"}, headers=CSRF)
    assert r.json()["detail"] == "The report's definition: item «k1», value: there is no field [x] (at character 5)"


@pytest.mark.anyio
async def test_a_failed_render_waited_for_is_said_in_capitals(fake_engine):
    fake_engine.export_states["t9"] = [{"status": "failed", "code": 422, "detail": "too many pages: 512"}]
    with pytest.raises(HTTPException) as e:
        await routes._wait_for_file("t9", "user:1")
    assert (e.value.status_code, e.value.detail) == (422, "Too many pages: 512")


class _Gone:
    """A request whose caller has gone."""

    async def is_disconnected(self) -> bool:
        return True


@pytest.mark.anyio
async def test_a_caller_that_stops_waiting_stops_the_render(fake_engine):
    fake_engine.export_states["t7"] = [{"status": "running"}]
    with pytest.raises(HTTPException) as e:
        await asyncio.wait_for(routes._wait_for_file("t7", "user:1", _Gone()), 5)
    assert e.value.status_code == 499 and fake_engine.revoked == ["t7"]


# ── two at once ───────────────────────────────────────────────────────────────
def test_a_save_written_on_an_older_version_is_refused(app, session, fake_engine):
    folder, editor = _folder_and_editor(app, session)
    rid = editor.post(f"/projects/{folder}/reports", json={"name": "R", "content": definition()},
                      headers=CSRF).json()["id"]
    mine = definition(items=[_text(value="mine")])
    theirs = definition(items=[_text(value="theirs")])
    assert editor.patch(f"/reports/{rid}", json={"content": theirs, "base_version": 1},
                        headers=CSRF).json()["current_version"] == 2
    r = editor.patch(f"/reports/{rid}", json={"content": mine, "base_version": 1}, headers=CSRF)
    assert r.status_code == 409 and "changed since you opened it (now version 2)" in r.json()["detail"]
    assert json.loads(session.exec(select(ReportVersion).where(ReportVersion.version == 2)).one().content) == theirs
    # a rename on an old version changes no content: fine; without base_version, as before
    assert editor.patch(f"/reports/{rid}", json={"name": "R2", "base_version": 1}, headers=CSRF).status_code == 200
    assert editor.patch(f"/reports/{rid}", json={"content": mine}, headers=CSRF).json()["current_version"] == 3


def test_two_creations_with_one_name_at_once_give_a_409(app, session, fake_engine, monkeypatch):
    folder, editor = _folder_and_editor(app, session)
    assert editor.post(f"/projects/{folder}/reports", json={"name": "New report"}, headers=CSRF).status_code == 201
    # the other request checked the name before this one wrote it
    monkeypatch.setattr(routes, "_name", lambda session, project_id, name, exclude_id=None: name.strip())
    r = editor.post(f"/projects/{folder}/reports", json={"name": "New report"}, headers=CSRF)
    assert r.status_code == 409 and "already a report" in r.json()["detail"]
    other = editor.post(f"/projects/{folder}/reports", json={"name": "Other"}, headers=CSRF).json()["id"]
    r = editor.patch(f"/reports/{other}", json={"name": "New report"}, headers=CSRF)
    assert r.status_code == 409
    assert [x.name for x in session.exec(select(Report).order_by(Report.id)).all()] == ["New report", "Other"]


# ── the file's name ───────────────────────────────────────────────────────────
def test_an_accented_file_name_has_a_plain_one_beside_it(app, session, fake_engine):
    """The engine names a file with accents only as `filename*`, which `curl -OJ` and
    `wget --content-disposition` do not read (they saved «render»): a plain name goes
    beside it, the accented one stays for the browsers."""
    from fastapi.testclient import TestClient

    from app.models import User
    from app.routes.proxy import ascii_fallback
    from app.services import api_tokens

    folder, editor = _folder_and_editor(app, session)
    rid = editor.post(f"/projects/{folder}/reports", json={"name": "Margine €/mese", "content": definition()},
                      headers=CSRF).json()["id"]
    owner = session.exec(select(User).where(User.email == "ed@x.local")).one()
    _, secret = api_tokens.create(session, owner, "nightly", "read", 30, owner)
    fake_engine.export_disposition = "attachment; filename*=utf-8''Margine%20%E2%82%AC%20mese%20%E2%80%94%20%C3%91and%C3%BA.pdf"
    r = TestClient(app).get(f"/reports/{rid}/render", headers={"Authorization": f"Bearer {secret}"})
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"] == (
        "attachment; filename=\"Margine mese Nandu.pdf\"; "
        "filename*=utf-8''Margine%20%E2%82%AC%20mese%20%E2%80%94%20%C3%91and%C3%BA.pdf")
    # a plain name, or one with both already, is left as it is
    assert ascii_fallback('attachment; filename="report.pdf"') == 'attachment; filename="report.pdf"'
    both = "attachment; filename=\"a.pdf\"; filename*=utf-8''%C3%A0.pdf"
    assert ascii_fallback(both) == both
    assert ascii_fallback("attachment; filename*=utf-8''%E4%B8%AD.xlsx") == \
        "attachment; filename=\"report.xlsx\"; filename*=utf-8''%E4%B8%AD.xlsx"


def test_a_render_the_engine_did_not_take_says_why_in_the_audit(app, session, fake_engine, monkeypatch):
    import httpx
    from fastapi.testclient import TestClient

    folder, editor = _folder_and_editor(app, session)
    rid = editor.post(f"/projects/{folder}/reports", json={"name": "R", "content": definition()},
                      headers=CSRF).json()["id"]
    answer = fake_engine._handler

    def down(request):
        if request.url.path == "/reports/render":
            raise httpx.ConnectError("connection refused", request=request)
        return answer(request)

    monkeypatch.setattr("app.core.engine_client._client",
                        httpx.AsyncClient(transport=httpx.MockTransport(down), base_url="http://engine"))
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies = editor.cookies
    client.post(f"/reports/{rid}/render", json={"format": "pdf"}, headers=CSRF)
    [entry] = session.exec(select(AuditLog).where(AuditLog.action == "report.render")).all()
    assert entry.outcome == "failure" and json.loads(entry.detail)["error"] == "The engine is not answering"


def test_a_mistake_both_find_is_said_once(app, session, fake_engine):
    # the engine names a dataset by its place in a mistake of shape: by its name here, so
    # the gateway's own words for the same field are not said twice
    folder, editor = _folder_and_editor(app, session)
    content = definition(sql_dataset())
    content["datasets"][0]["fields"] = [{"name": "f" * 300, "type": "string"}]
    fake_engine.report_check_response = (200, {"problems": [
        {"item": None, "field": "datasets[0].fields[0].name", "pos": None, "length": 0,
         "message": "String should have at most 256 characters"}]})
    problems = editor.post("/reports/check", json={"project_id": folder, "definition": content},
                           headers=CSRF).json()["problems"]
    assert [(p["field"], p["message"]) for p in problems] == [
        ("datasets[orders].fields[0].name", "The dataset «orders»'s field name is at most 256 characters")]


def test_there_is_no_version_zero(app, session, fake_engine):
    folder, editor = _folder_and_editor(app, session)
    rid = editor.post(f"/projects/{folder}/reports", json={"name": "R", "content": definition()},
                      headers=CSRF).json()["id"]
    assert editor.get(f"/reports/{rid}/versions/0").status_code == 404
    assert editor.post(f"/reports/{rid}/versions/0/restore", headers=CSRF).status_code == 404
    assert len(editor.get(f"/reports/{rid}/versions").json()) == 1


def test_a_sql_on_the_catalog_names_datasources_its_author_may_read(app, session, fake_engine):
    """As a connection, a datasource or a view: a SQL on the catalog that names a
    datasource its author cannot read — or one that does not exist, a typo — is said at
    the save and while one types, not by the first render."""
    folder, editor = _folder_and_editor(app, session)
    vault = make_project(session, name="Vault").id
    make_datasource(session, name="Orders", project_id=folder)
    make_datasource(session, name="Salaries", project_id=vault)
    region = {"name": "region", "type": "text", "required": True}
    for sql, words in (('SELECT * FROM "Salaries"', "there is no datasource «Salaries» you can read"),
                       ('SELECT * FROM "Ordres" WHERE r = {region}', "there is no datasource «Ordres» you can read"),
                       ("SELECT * FROM", "could not be read")):
        content = definition(sql_dataset(sql=sql), parameters=[region])
        r = editor.post(f"/projects/{folder}/reports", json={"name": "R", "content": content}, headers=CSRF)
        assert r.status_code == 422 and words in r.json()["detail"]["message"], r.text
        assert r.json()["detail"]["problems"][0]["field"] == "datasets[orders].source"
    good = definition(sql_dataset(sql='SELECT * FROM "Orders" WHERE r = {region} AND d >= {today-30}'),
                      parameters=[region])
    assert editor.post(f"/projects/{folder}/reports", json={"name": "R", "content": good},
                       headers=CSRF).status_code == 201


def test_a_placeholder_in_a_dataset_s_sql_is_written_in_the_report_s_language(app, session, fake_engine):
    folder, editor = _folder_and_editor(app, session)
    make_datasource(session, name="Orders", project_id=folder)
    content = {**definition(sql_dataset(sql="SELECT * FROM \"Orders\" WHERE month = '{today:MMMM}'")), "locale": "it"}
    rid = editor.post(f"/projects/{folder}/reports", json={"name": "R", "content": content}, headers=CSRF).json()["id"]
    assert editor.post(f"/reports/{rid}/render", json={"format": "pdf"}, headers=CSRF).status_code == 200
    assert [p["lang"] for p in fake_engine.placeholder_previews] == ["it"]


def test_the_pushmail_panel_previews_names_in_the_language_of_who_runs_the_group(app, session, fake_engine):
    """A Pushmail cell's email is written in the language of whoever runs the group
    (flow_group_runner): its panel previews `{today:MMMM}` in that language too. Any other
    form (a flow's Output) keeps English, as its run does."""
    from app.routes import connections as connection_routes

    app.include_router(connection_routes.router)
    me = _sign_in(app, session, "ana@x.local", locale="it")
    body = {"kind": "output", "templates": {"subject": "Sales {today:MMMM yyyy}"}, "timezone": "Europe/Rome"}
    assert me.post("/placeholders/preview", json={**body, "pushmail": True}, headers=CSRF).status_code == 200
    assert fake_engine.placeholder_previews[-1]["lang"] == "it"
    me.post("/placeholders/preview", json=body, headers=CSRF)
    assert fake_engine.placeholder_previews[-1].get("lang", "en") == "en"
