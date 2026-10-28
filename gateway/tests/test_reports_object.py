"""Reports as objects of a folder (docs/specs/reports.md, piece 2): who sees and changes
them, their versions, the Explore's operations, the audit.

The rules these tests hold: VIEW on the folder sees and reads a report, EDIT changes,
moves, renames, duplicates, deletes it and restores a version, the rest get 404; a save
that changes nothing makes no version; a definition points only to what its author may
read — CONNECT for a connection — while an editor without that right still changes the
rest of a report; the engine checks the expressions when it can, and a save does not
wait for an engine that cannot; the folder that holds a report is not deleted; search
finds it; every change leaves its own action in the audit.
"""
import json

import pytest
from fastapi import FastAPI
from sqlmodel import select

from app.db.session import get_session
from app.models import AuditLog, Connection, Report, ReportVersion, SavedView, SqlScript, SqlScriptVersion, User
from app.routes import reports as routes
from tests.conftest import make_datasource, make_permission, make_project
from tests.test_engine_settings import CSRF, _sign_in


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


def definition(*datasets, parameters=(), items=()) -> dict:
    return {"v": 1, "timezone": "UTC", "parameters": list(parameters), "datasets": list(datasets),
            "sections": {"body": {"items": list(items)}}}


def sql_dataset(name="orders", sql="SELECT * FROM \"Orders\"", **source) -> dict:
    return {"name": name, "source": {"kind": "sql", "sql": sql, **source}}


def _actions(session) -> list[str]:
    return [a.action for a in session.exec(select(AuditLog).order_by(AuditLog.id)).all() if a.action.startswith("report.")]


# ── the object ────────────────────────────────────────────────────────────────
def test_create_read_and_change_by_permission(app, session):
    sales = make_project(session, name="Sales").id
    boss, reader, stranger = (_sign_in(app, session, e) for e in ("boss@x.local", "reader@x.local", "other@x.local"))
    by = _ids(session)
    make_permission(session, user_id=by["boss@x.local"], project_id=sales, capability="edit")
    make_permission(session, user_id=by["reader@x.local"], project_id=sales, capability="view")

    r = boss.post(f"/projects/{sales}/reports", json={"name": "Monthly sales"}, headers=CSRF)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["current_version"] == 1 and r.json()["editable"] and r.json()["content"]["v"] == 1
    assert boss.post(f"/projects/{sales}/reports", json={"name": "Monthly sales"}, headers=CSRF).status_code == 409
    assert boss.post(f"/projects/{sales}/reports", json={"name": "  "}, headers=CSRF).status_code == 422

    seen = reader.get(f"/reports/{rid}").json()
    assert seen["name"] == "Monthly sales" and not seen["editable"]
    assert [x["name"] for x in reader.get(f"/projects/{sales}/reports").json()] == ["Monthly sales"]
    assert [x["name"] for x in reader.get("/reports").json()] == ["Monthly sales"]
    assert reader.patch(f"/reports/{rid}", json={"name": "Mine"}, headers=CSRF).status_code == 403
    assert reader.post(f"/reports/{rid}/duplicate", headers=CSRF).status_code == 403
    assert reader.delete(f"/reports/{rid}", headers=CSRF).status_code == 403
    for client in (stranger,):
        assert client.get(f"/reports/{rid}").status_code == 404
        assert client.get(f"/reports/{rid}/versions").status_code == 404
        assert client.patch(f"/reports/{rid}", json={"name": "Mine"}, headers=CSRF).status_code == 404
        assert client.get("/reports").json() == []
        assert client.get(f"/projects/{sales}/reports").status_code == 403


def test_a_new_report_starts_in_its_author_s_language_and_the_installation_s_zone(app, session, monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings().app, "timezone", "Europe/Rome")
    folder = make_project(session, name="F").id
    me = _sign_in(app, session, "ana@x.local", is_superuser=True, locale="it")
    content = me.post(f"/projects/{folder}/reports", json={"name": "R"}, headers=CSRF).json()["content"]
    assert (content["locale"], content["timezone"], content["page"]["size"]) == ("it", "Europe/Rome", "A4")


def test_versions_restore_duplicate_rename_move_delete_and_the_audit(app, session):
    sales, other = make_project(session, name="Sales").id, make_project(session, name="Other").id
    me = _sign_in(app, session, "boss@x.local", is_superuser=True)
    first = definition(parameters=[{"name": "region", "type": "text"}])
    rid = me.post(f"/projects/{sales}/reports", json={"name": "R", "content": first}, headers=CSRF).json()["id"]
    second = {**first, "locale": "de"}
    assert me.patch(f"/reports/{rid}", json={"content": second}, headers=CSRF).json()["current_version"] == 2
    # the same definition again, and a description: no new version
    r = me.patch(f"/reports/{rid}", json={"content": second, "description": "For the board"}, headers=CSRF).json()
    assert r["current_version"] == 2 and r["description"] == "For the board"
    assert [(v["version"], v["note"], v["current"]) for v in me.get(f"/reports/{rid}/versions").json()] == [
        (2, "edited", True), (1, "created", False)]
    assert me.get(f"/reports/{rid}/versions/1").json() == first
    assert me.get(f"/reports/{rid}/versions/9").status_code == 404
    restored = me.post(f"/reports/{rid}/versions/1/restore", headers=CSRF).json()
    assert restored["current_version"] == 3 and restored["content"] == first and restored["parameters"] == 1

    copy = me.post(f"/reports/{rid}/duplicate", headers=CSRF).json()
    assert copy["name"] == "R (1)" and copy["content"] == first and copy["current_version"] == 1
    moved = me.patch(f"/reports/{rid}", json={"project_id": other, "name": "Board pack"}, headers=CSRF).json()
    assert (moved["project_id"], moved["name"]) == (other, "Board pack")
    assert me.patch(f"/reports/{copy['id']}", json={"project_id": other, "name": "Board pack"},
                    headers=CSRF).status_code == 409
    r = me.delete(f"/projects/{other}", headers=CSRF)
    assert r.status_code == 409 and "reports" in r.json()["detail"]
    hits = me.get("/search", params={"q": "board"}).json()
    assert [(h["kind"], h["name"], h["project_name"]) for h in hits["items"]] == [("report", "Board pack", "Other")]
    assert hits["counts"] == {"report": 1}

    assert me.delete(f"/reports/{rid}", headers=CSRF).status_code == 204
    assert session.get(Report, rid) is None
    assert session.exec(select(ReportVersion).where(ReportVersion.report_id == rid)).all() == []
    assert _actions(session) == ["report.create", "report.update", "report.update", "report.restore",
                                 "report.duplicate", "report.rename", "report.move", "report.delete"]
    rows = {a.action: a for a in session.exec(select(AuditLog)).all()}
    assert json.loads(rows["report.rename"].detail) == {"from": "R", "to": "Board pack"}
    assert json.loads(rows["report.move"].detail) == {"from": sales, "to": other}
    assert json.loads(rows["report.duplicate"].detail) == {"project_id": sales, "duplicate_of": rid}
    assert rows["report.delete"].target_id == rid and rows["report.delete"].target_label == "Board pack"


def test_a_move_needs_edit_on_both_folders(app, session):
    a, b = make_project(session, name="A").id, make_project(session, name="B").id
    me = _sign_in(app, session, "ed@x.local")
    make_permission(session, user_id=_ids(session)["ed@x.local"], project_id=a, capability="edit")
    make_permission(session, user_id=_ids(session)["ed@x.local"], project_id=b, capability="view")
    rid = me.post(f"/projects/{a}/reports", json={"name": "R"}, headers=CSRF).json()["id"]
    assert me.patch(f"/reports/{rid}", json={"project_id": b}, headers=CSRF).status_code == 403
    assert me.patch(f"/reports/{rid}", json={"project_id": 999}, headers=CSRF).status_code == 404


# ── what a definition points to ──────────────────────────────────────────────
def test_a_definition_points_only_to_what_its_author_may_read(app, session):
    sales, secret, vault = (make_project(session, name=n).id for n in ("Sales", "Secret", "Vault"))
    erp = _connection(session, secret)
    mail = _connection(session, secret, db_type="smtp", name="Mail")
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
    view_id, script_id = view.id, script.id  # the requests of a test client leave the objects detached
    boss, helper = _sign_in(app, session, "boss@x.local"), _sign_in(app, session, "helper@x.local")
    by = _ids(session)
    for who in ("boss@x.local", "helper@x.local"):
        make_permission(session, user_id=by[who], project_id=sales, capability="edit")

    def save(client, content, rid=None):
        if rid is None:
            return client.post(f"/projects/{sales}/reports", json={"name": f"R{len(_actions(session))}", "content": content},
                               headers=CSRF)
        return client.patch(f"/reports/{rid}", json={"content": content}, headers=CSRF)

    on_erp = definition(sql_dataset(sql="SELECT 1", connection_id=erp))
    r = save(boss, on_erp)
    assert r.status_code == 422
    assert r.json()["detail"]["message"] == ("The dataset «orders»: it reads a database connection "
                                             f"({erp}) that does not exist or that you cannot use")
    assert r.json()["detail"]["where"] == "datasets[0].source"
    assert save(boss, definition(sql_dataset(sql="SELECT 1", connection_id=mail))).status_code == 422
    for source, words in ((({"kind": "datasource", "datasource_id": hidden}), "a datasource"),
                          (({"kind": "view", "view_id": view_id}), "a datasource"),
                          (({"kind": "view", "view_id": 999}), "a saved view (999)"),
                          (({"kind": "sql_script", "script_id": script_id}), "a SQL script")):
        r = save(boss, definition({"name": "d", "source": source}))
        assert r.status_code == 422 and words in r.json()["detail"]["message"], r.text
    r = save(boss, definition({"name": "d", "source": {"kind": "datasource", "datasource_id": shown,
                                                       "filters": [{"column": "nope", "op": "=", "value": 1}]}}))
    assert "names the column «nope», which «Orders» does not have" in r.json()["detail"]["message"]

    # with CONNECT the boss may; the helper, without it, still changes the rest of the report
    make_permission(session, user_id=by["boss@x.local"], project_id=secret, capability="connect")
    r = save(boss, on_erp)
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert save(helper, {**on_erp, "locale": "fr"}, rid).status_code == 200
    r = save(helper, definition(sql_dataset(sql="SELECT 2", connection_id=erp)), rid)
    assert r.status_code == 422 and "cannot use" in r.json()["detail"]["message"]
    # and restoring a version needs the same right as saving it
    save(boss, definition(), rid)
    assert helper.post(f"/reports/{rid}/versions/1/restore", headers=CSRF).status_code == 422
    assert boss.post(f"/reports/{rid}/versions/1/restore", headers=CSRF).status_code == 200


def test_the_sql_s_parameters_are_checked_at_the_save(app, session):
    folder = make_project(session, name="F").id
    make_datasource(session, name="Orders", project_id=folder)
    me = _sign_in(app, session, "ana@x.local", is_superuser=True)
    region = {"name": "region", "type": "text"}

    def save(sql, parameters=(region,), **extra):
        content = definition(sql_dataset(sql=sql), parameters=list(parameters), **extra)
        return me.post(f"/projects/{folder}/reports", json={"name": "R", "content": content}, headers=CSRF)

    r = save("SELECT * FROM \"Orders\" WHERE region = '{region}'")
    assert r.status_code == 422
    assert r.json()["detail"]["message"] == ("The dataset «orders»: write {region} without quotes: "
                                             "Tabularia quotes it")
    assert "neither a parameter" in save("SELECT * FROM \"Orders\" WHERE a = {regoin}").json()["detail"]["message"]
    # a dataset that fills a parameter's choices cannot use it
    chooser = {"name": "region", "type": "text", "choices": {"dataset": "orders", "value": "[region]"}}
    r = save("SELECT region FROM \"Orders\" WHERE region <> {region}", parameters=[chooser])
    assert "fills the choices of «region»" in r.json()["detail"]["message"]
    assert save("SELECT * FROM \"Orders\" WHERE region = {region} -- '{region}'").status_code == 201


def test_the_engine_checks_the_expressions_when_it_can(app, session, fake_engine):
    folder = make_project(session, name="F").id
    me = _sign_in(app, session, "ana@x.local", is_superuser=True)
    text = {"id": "t", "type": "text", "x": 0, "y": 0, "w": 50, "h": 10, "value": "=SUMM(1)"}
    content = definition(items=[text])
    # an engine without the route (as before reports) or out of order does not stop the save
    assert me.post(f"/projects/{folder}/reports", json={"name": "A", "content": content}, headers=CSRF).status_code == 201
    fake_engine.report_check_response = (500, {"detail": "boom"})
    assert me.post(f"/projects/{folder}/reports", json={"name": "B", "content": content}, headers=CSRF).status_code == 201
    # the engine's own words start in lower case: said as the gateway says its own
    error = {"item": "t", "field": "value", "pos": 1, "length": 4, "message": "unknown function SUMM"}
    fake_engine.report_check_response = (200, {"problems": [error]})
    r = me.post(f"/projects/{folder}/reports", json={"name": "C", "content": content}, headers=CSRF)
    assert r.status_code == 422
    said = {**error, "message": "Unknown function SUMM"}
    assert r.json()["detail"] == {"message": "Unknown function SUMM", "where": "value", "item": "t",
                                  "errors": [said], "problems": [said]}
    assert fake_engine.report_checks[-1]["definition"]["sections"]["body"]["items"][0]["value"] == "=SUMM(1)"
    fake_engine.report_check_response = (200, {"problems": []})
    assert me.post(f"/projects/{folder}/reports", json={"name": "C", "content": content}, headers=CSRF).status_code == 201


def test_a_structural_mistake_says_where(app, session):
    folder = make_project(session, name="F").id
    me = _sign_in(app, session, "ana@x.local", is_superuser=True)
    content = definition(items=[{"id": "t", "type": "text", "x": 150, "y": 0, "w": 50, "h": 10}])
    r = me.post(f"/projects/{folder}/reports", json={"name": "A", "content": content}, headers=CSRF)
    assert r.status_code == 422
    assert r.json()["detail"]["item"] == "t" and "right margin" in r.json()["detail"]["message"]
