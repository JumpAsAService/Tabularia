"""Rendering a report (docs/specs/reports.md, «Rendering» and «Gateway API»).

The rules these tests hold: a render reads every dataset with the rights of whoever asks
— SQL on the catalog among the datasources they read, CONNECT for a connection, VIEW for
a datasource, a view (and its datasource) and a SQL script — and one they may not read
stops it with the dataset's name, before the engine is asked; the engine receives final
SQL, the parameters as literals, a view's operations after the report's filters; a value
that does not fit is a 400 naming its parameter; the preview needs EDIT; the API answers
to a token of either level with its owner's rights, waits for the file and sends it, and
a revoked token or an observer's gets nothing; every render is audited.
"""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.config import get_settings
from app.db.session import get_session
from app.models import AuditLog, Report, ReportVersion, SavedView, SqlScript, SqlScriptVersion, User
from app.routes import reports as routes
from app.services import api_tokens
from tests.conftest import make_datasource, make_permission, make_project, make_user
from tests.test_engine_settings import CSRF, _sign_in
from tests.test_reports_object import _connection


@pytest.fixture
def app(session, fake_engine):
    from app.routes import auth as auth_routes

    fake_engine.placeholder_values = {"{today-30}": "2026-09-28"}
    app = FastAPI()
    for r in (auth_routes.router, routes.router):
        app.include_router(r)
    app.dependency_overrides[get_session] = lambda: session
    return app


PARAMETERS = [
    {"name": "region", "type": "text", "required": True, "choices": {"values": ["North", "South"]}},
    {"name": "since", "type": "date", "default": "{today-30}"},
    {"name": "regions", "type": "text", "multi": True, "default": ["N"]},
]


def _world(session) -> dict:
    """«Sales» (orders, a view on them, a SQL script), «Secret» (a database connection),
    «Vault» (salaries); a report in «Sales» reading all of it."""
    sales, secret, vault = (make_project(session, name=n).id for n in ("Sales", "Secret", "Vault"))
    cols = json.dumps([{"name": "region", "dtype": "String"}, {"name": "amount", "dtype": "Float64"},
                       {"name": "day", "dtype": "Date"}])
    orders = make_datasource(session, name="Orders", project_id=sales, key="datasets/o.parquet", columns=cols).id
    salaries = make_datasource(session, name="Salaries", project_id=vault, key="datasets/s.parquet").id
    erp = _connection(session, secret)
    spec = {"engine": "duckdb", "filters": [{"column": "amount", "operator": "gt", "value": "10", "value2": ""}],
            "computedFields": [{"name": "double", "expr": "amount * 2"}], "pivotOn": False,
            "pivot": {"index": [""], "on": [""], "values": "", "func": "sum"}}
    view = SavedView(name="Big orders", project_id=sales, datasource_id=orders, spec=json.dumps(spec))
    script = SqlScript(name="Top", project_id=sales, current_version=1)
    session.add_all([view, script])
    session.commit()
    session.add(SqlScriptVersion(script_id=script.id, version=1, content=json.dumps(
        {"sql": "SELECT * FROM \"Orders\" WHERE region = {region} ORDER BY amount DESC", "engine": "polars"})))
    content = {
        "v": 1, "timezone": "Europe/Rome", "locale": "it", "parameters": PARAMETERS,
        "datasets": [
            {"name": "orders", "source": {"kind": "sql", "engine": "duckdb", "sql":
                "SELECT region, amount FROM \"Orders\" -- the {region} of the reader\n"
                "WHERE region = {region} AND day >= {since}"}},
            {"name": "erp", "source": {"kind": "sql", "connection_id": erp,
                                       "sql": "SELECT * FROM invoices WHERE area IN {regions} AND note <> 'it''s'"}},
            {"name": "plain", "source": {"kind": "datasource", "datasource_id": orders, "filters": [
                {"column": "region", "op": "=", "value": "{region}"}, {"column": "day", "op": ">=", "value": "{since}"},
                {"column": "amount", "op": ">", "value": 0}]}},
            {"name": "kpis", "source": {"kind": "view", "view_id": view.id,
                                        "filters": [{"column": "region", "op": "in", "value": "{regions}"}]}},
            {"name": "top", "source": {"kind": "sql_script", "script_id": script.id}},
        ],
        "sections": {"body": {"items": []}},
    }
    report = Report(name="Monthly: sales", project_id=sales, current_version=1)
    session.add(report)
    session.commit()
    session.add(ReportVersion(report_id=report.id, version=1, content=json.dumps(content)))
    session.commit()
    return {"sales": sales, "secret": secret, "vault": vault, "orders": orders, "salaries": salaries, "erp": erp,
            "view": view.id, "script": script.id, "report": report.id}


def _uid(session, email) -> int:
    return session.exec(select(User).where(User.email == email)).one().id


def _reader(app, session, w, email="reader@x.local", connect=True) -> TestClient:
    client = _sign_in(app, session, email)
    make_permission(session, user_id=_uid(session, email), project_id=w["sales"], capability="view")
    if connect:
        make_permission(session, user_id=_uid(session, email), project_id=w["secret"], capability="connect")
    return client


def _renders(session) -> list[AuditLog]:
    return session.exec(select(AuditLog).where(AuditLog.action == "report.render").order_by(AuditLog.id)).all()


# ── what the engine receives ──────────────────────────────────────────────────
def test_a_render_reads_every_dataset_with_the_reader_s_rights(app, session, fake_engine):
    w = _world(session)
    reader = _reader(app, session, w)
    r = reader.post(f"/reports/{w['report']}/render", json={"format": "pdf", "params": {
        "region": "North", "regions": ["N", "S'; DROP TABLE x; --"]}, "state": {"sort": {"t1": "amount"}}},
        headers=CSRF)
    assert r.status_code == 200, r.text
    assert r.json() == {"task_id": "t-report-1", "filename": "Monthly sales.pdf"}
    [body] = fake_engine.report_renders
    assert (body["format"], body["filename"], body["locale"], body["owner"]) == (
        "pdf", "Monthly sales.pdf", "it", f"user:{_uid(session, 'reader@x.local')}")
    assert body["params"] == {"region": "North", "since": "2026-09-28", "regions": ["N", "S'; DROP TABLE x; --"]}
    assert body["state"] == {"sort": {"t1": "amount"}} and body["definition"]["datasets"][0]["name"] == "orders"
    ds, o = body["datasets"], w["orders"]
    # SQL on the catalog: the comment gone, the parameters as literals, the datasource as its alias
    assert ds["orders"]["engine"] == "duckdb" and ds["orders"]["input_key"] == "datasets/o.parquet"
    assert ds["orders"]["operations"] == [{"type": "sql", "params": {
        "query": f"SELECT region, amount FROM ds_{o} AS \"Orders\" \nWHERE region = 'North' AND day >= DATE '2026-09-28'",
        "tables": {f"ds_{o}": {"source": {"bucket": "data-prep", "key": "datasets/o.parquet"}}}}}]
    # on a connection: its payload, the list quoted for PostgreSQL
    assert ds["erp"]["sql"] == "SELECT * FROM invoices WHERE area IN ('N', 'S''; DROP TABLE x; --') AND note <> 'it''s'"
    assert ds["erp"]["connection"]["db_type"] == "postgresql" and ds["erp"]["connection"]["password_encrypted"] == "encrypted"
    # a datasource with the report's filters
    assert ds["plain"]["input_key"] == "datasets/o.parquet" and ds["plain"]["operations"] == [
        {"type": "filter", "params": {"column": "region", "operator": "eq", "value": "North"}},
        {"type": "filter", "params": {"column": "day", "operator": "ge", "value": "2026-09-28"}},
        {"type": "filter", "params": {"column": "amount", "operator": "gt", "value": 0}}]
    # a view: the report's filters first, then the view's own, as the Viewer builds them, on its engine
    assert ds["kpis"]["engine"] == "duckdb" and ds["kpis"]["operations"] == [
        {"type": "filter", "params": {"column": "region", "operator": "in", "value": ["N", "S'; DROP TABLE x; --"]}},
        {"type": "filter", "params": {"column": "amount", "operator": "gt", "value": 10}},
        {"type": "compute", "params": {"columns": [{"name": "double", "expr": "amount * 2"}]}}]
    # a SQL script: its current version, with the report's parameters, on its engine
    assert ds["top"]["engine"] == "polars"
    assert ds["top"]["operations"][0]["params"]["query"] == \
        f"SELECT * FROM ds_{o} AS \"Orders\" WHERE region = 'North' ORDER BY amount DESC"
    # the placeholders resolved once, in the report's zone
    assert {p["timezone"] for p in fake_engine.placeholder_previews} == {"Europe/Rome"}
    [entry] = _renders(session)
    detail = json.loads(entry.detail)
    assert entry.outcome == "success" and entry.target_id == w["report"]
    assert detail == {"format": "pdf", "from": "interface",
                      "parameters": {"region": "North", "since": "2026-09-28", "regions": ["N", "S'; DROP TABLE x; --"]}}


@pytest.mark.parametrize("change, words", [
    ("no connect", "The dataset «erp»: it reads a database connection"),
    ("no sales", "Report not found"),
    ("vault in sql", "The dataset «orders»: there is no datasource «Salaries» you can read"),
    ("view on vault", "The dataset «kpis»: it reads a datasource"),
    ("script in vault", "The dataset «top»: it reads a SQL script"),
])
def test_a_dataset_the_reader_cannot_read_stops_the_render_with_its_name(app, session, fake_engine, change, words):
    w = _world(session)
    if change == "no sales":
        reader = _sign_in(app, session, "reader@x.local")
    else:
        reader = _reader(app, session, w, connect=change != "no connect")
    report = session.get(Report, w["report"])
    content = json.loads(session.exec(select(ReportVersion)).one().content)
    if change == "vault in sql":
        content["datasets"][0]["source"]["sql"] = "SELECT * FROM \"Salaries\" WHERE x = {region}"
    elif change == "view on vault":
        session.get(SavedView, w["view"]).datasource_id = w["salaries"]
    elif change == "script in vault":
        session.get(SqlScript, w["script"]).project_id = w["vault"]
    row = session.exec(select(ReportVersion)).one()
    row.content = json.dumps(content)
    session.add_all([row, report])
    session.commit()
    r = reader.post(f"/reports/{w['report']}/render", json={"params": {"region": "North"}}, headers=CSRF)
    assert r.status_code in (404, 422) and r.json()["detail"].startswith(words), r.text
    assert fake_engine.report_renders == []
    if change != "no sales":
        [entry] = _renders(session)
        assert entry.outcome == "failure" and json.loads(entry.detail)["error"].startswith(words)


def test_values_are_checked_before_anything_runs(app, session, fake_engine):
    w = _world(session)
    reader = _reader(app, session, w)
    for params, name in (({"region": "West"}, "region"), ({"region": "North", "since": "2026-02-30"}, "since"),
                         ({}, "region"), ({"region": "North", "nope": 1}, "nope"),
                         ({"region": {"a": 1}}, "region")):
        r = reader.post(f"/reports/{w['report']}/render", json={"params": params}, headers=CSRF)
        assert r.status_code == 400 and r.json()["detail"]["parameter"] == name, (params, r.text)
    assert fake_engine.report_renders == [] and fake_engine.db_queries == []
    assert reader.post(f"/reports/{w['report']}/render", json={"format": "docx"}, headers=CSRF).status_code == 422


def test_an_empty_list_in_the_sql_stops_with_a_clear_message(app, session, fake_engine):
    w = _world(session)
    reader = _reader(app, session, w)
    r = reader.post(f"/reports/{w['report']}/render", json={"params": {"region": "North", "regions": []}}, headers=CSRF)
    # no value given: the default; an empty list given on purpose is no value either
    assert r.status_code == 200
    content = json.loads(session.exec(select(ReportVersion)).one().content)
    content["parameters"][2].pop("default")
    row = session.exec(select(ReportVersion)).one()
    row.content = json.dumps(content)
    session.add(row)
    session.commit()
    r = reader.post(f"/reports/{w['report']}/render", json={"params": {"region": "North"}}, headers=CSRF)
    assert r.status_code == 422
    assert r.json()["detail"] == "The dataset «erp»: «regions» has no value, and the SQL needs at least one: choose one"


def test_the_engine_s_refusal_is_said(app, session, fake_engine):
    w = _world(session)
    reader = _reader(app, session, w)
    fake_engine.report_render_response = (422, {"detail": "The dataset «orders» has 25,000 rows: at most 20,000"})
    r = reader.post(f"/reports/{w['report']}/render", json={"params": {"region": "North"}}, headers=CSRF)
    assert r.status_code == 422 and "at most 20,000" in r.json()["detail"]
    assert _renders(session)[-1].outcome == "failure"


def test_choices_from_a_dataset_are_checked_when_the_engine_reads_them(app, session, fake_engine):
    w = _world(session)
    reader = _reader(app, session, w)
    content = json.loads(session.exec(select(ReportVersion)).one().content)
    content["parameters"][0]["choices"] = {"dataset": "plain", "value": "[region]", "label": "[region]"}
    content["datasets"][2]["source"]["filters"] = []  # the dataset that fills the choices does not use them
    row = session.exec(select(ReportVersion)).one()
    row.content = json.dumps(content)
    session.add(row)
    session.commit()
    # an engine that cannot read choices yet: any value of the type
    assert reader.post(f"/reports/{w['report']}/render", json={"params": {"region": "West"}}, headers=CSRF).status_code == 200
    fake_engine.report_choices_response = (200, {"choices": [{"value": "North", "label": "Nord"}], "truncated": False})
    r = reader.post(f"/reports/{w['report']}/render", json={"params": {"region": "West"}}, headers=CSRF)
    assert r.status_code == 400 and r.json()["detail"]["parameter"] == "region"
    asked = fake_engine.report_choices[-1]
    assert (asked["dataset"], asked["value"], list(asked["datasets"])) == ("plain", "[region]", ["plain"])
    assert reader.post(f"/reports/{w['report']}/render", json={"params": {"region": "North"}}, headers=CSRF).status_code == 200


# ── the editor's preview ──────────────────────────────────────────────────────
def test_the_preview_needs_edit(app, session, fake_engine):
    w = _world(session)
    reader = _reader(app, session, w)
    editor = _reader(app, session, w, email="editor@x.local")
    make_permission(session, user_id=_uid(session, "editor@x.local"), project_id=w["sales"], capability="edit")
    draft = {"v": 1, "parameters": [], "datasets": [{"name": "d", "source": {"kind": "sql", "sql": "SELECT 1 FROM \"Orders\""}}],
             "sections": {}}
    assert reader.post("/reports/preview", json={"report_id": w["report"], "definition": draft},
                       headers=CSRF).status_code == 403
    r = editor.post("/reports/preview", json={"report_id": w["report"], "definition": draft, "format": "svg"},
                    headers=CSRF)
    assert r.status_code == 200 and r.json()["filename"] == "Monthly sales.svg"
    r = editor.post("/reports/preview", json={"project_id": w["sales"], "definition": draft}, headers=CSRF)
    assert r.status_code == 200 and r.json()["filename"] == "report.pdf"
    assert editor.post("/reports/preview", json={"definition": draft}, headers=CSRF).status_code == 404
    bad = {**draft, "v": 2}
    assert editor.post("/reports/preview", json={"project_id": w["sales"], "definition": bad},
                       headers=CSRF).status_code == 422
    assert [json.loads(e.detail)["from"] for e in _renders(session)] == ["editor", "editor"]


# ── the API ───────────────────────────────────────────────────────────────────
def _token(session, email, scope="read", **kw) -> tuple[str, int]:
    user = session.exec(select(User).where(User.email == email)).first() or make_user(session, email=email, **kw)
    row, secret = api_tokens.create(session, user, "nightly", scope, 30, user)
    return secret, row.id


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_the_api_waits_for_the_file_and_sends_it(app, session, fake_engine):
    w = _world(session)
    _reader(app, session, w)
    token, _ = _token(session, "reader@x.local")
    fake_engine.export_states["t-report-1"] = [{"status": "running"}, {"status": "running"},
                                               {"status": "ready", "filename": "Monthly sales.pdf", "size": 15}]
    api = TestClient(app)
    r = api.get(f"/reports/{w['report']}/render?format=pdf&region=North&regions=N&regions=S", headers=_bearer(token))
    assert r.status_code == 200, r.text
    assert r.content == b"%PDF-1.7 report" and r.headers["content-disposition"] == 'attachment; filename="report.pdf"'
    owner = f"user:{_uid(session, 'reader@x.local')}"
    assert fake_engine.export_polls == [("/tasks/exports/t-report-1", owner)] * 3 + \
        [("/tasks/exports/t-report-1/file", owner)]
    assert fake_engine.report_renders[0]["params"]["regions"] == ["N", "S"]
    [entry] = _renders(session)
    assert entry.outcome == "success" and json.loads(entry.detail)["from"] == "api"
    assert json.loads(entry.detail)["token"] == "nightly"


def test_the_api_names_the_parameter_that_does_not_fit(app, session, fake_engine):
    w = _world(session)
    _reader(app, session, w)
    token, _ = _token(session, "reader@x.local")
    api = TestClient(app)
    for query, name in (("region=North&since=2026-13-01", "since"), ("region=North&zone=1", "zone"),
                        ("since=2026-01-01", "region"), ("region=North&region=South", "region")):
        r = api.get(f"/reports/{w['report']}/render?{query}", headers=_bearer(token))
        assert r.status_code == 400 and r.json()["detail"]["parameter"] == name, (query, r.text)
    assert api.get(f"/reports/{w['report']}/render?format=docx&region=North", headers=_bearer(token)).status_code == 422
    assert fake_engine.report_renders == []


def test_the_api_says_a_failed_render_and_stops_a_slow_one(app, session, fake_engine, monkeypatch):
    w = _world(session)
    _reader(app, session, w)
    token, _ = _token(session, "reader@x.local")
    api = TestClient(app)
    fake_engine.export_states["t-report-1"] = [{"status": "failed", "code": 422, "detail": "Too many pages: 512"}]
    r = api.get(f"/reports/{w['report']}/render?region=North", headers=_bearer(token))
    assert r.status_code == 422 and r.json()["detail"] == "Too many pages: 512"
    monkeypatch.setattr(get_settings().reports, "api_wait_seconds", 0.3)
    fake_engine.export_states["t-report-2"] = [{"status": "running"}]
    r = api.get(f"/reports/{w['report']}/render?region=North", headers=_bearer(token))
    assert r.status_code == 504 and "REPORTS__API_WAIT_SECONDS" in r.json()["detail"]
    assert fake_engine.revoked == ["t-report-2"]
    assert [e.outcome for e in _renders(session)] == ["failure", "failure"]


def test_which_tokens_the_api_answers(app, session, fake_engine):
    w = _world(session)
    _reader(app, session, w)
    read, read_id = _token(session, "reader@x.local")
    write, _ = _token(session, "reader@x.local", scope="write")
    api = TestClient(app)
    url = f"/reports/{w['report']}/render?region=North"
    assert api.get(url, headers=_bearer(read)).status_code == 200
    assert api.get(url, headers=_bearer(write)).status_code == 200
    # a read-only token starts a render as it starts an export, and changes nothing
    assert api.post(f"/reports/{w['report']}/render", json={"params": {"region": "North"}},
                    headers=_bearer(read)).status_code == 200
    assert api.patch(f"/reports/{w['report']}", json={"name": "x"}, headers=_bearer(read)).status_code == 403
    # a revoked token is no token; nor is an observer's personal one; a stranger's sees nothing
    api_tokens.revoke(session, read_id)
    assert api.get(url, headers=_bearer(read)).status_code == 401
    observer, _ = _token(session, "observer@x.local", is_observer=True)
    make_permission(session, user_id=_uid(session, "observer@x.local"), project_id=w["sales"], capability="view")
    assert api.get(url, headers=_bearer(observer)).status_code == 401
    stranger, _ = _token(session, "stranger@x.local")
    assert api.get(url, headers=_bearer(stranger)).status_code == 404
    assert api.get(url).status_code == 401


# ── the form and the editor ───────────────────────────────────────────────────
def test_the_parameters_as_the_form_shows_them(app, session, fake_engine):
    w = _world(session)
    reader = _reader(app, session, w)
    content = json.loads(session.exec(select(ReportVersion)).one().content)
    content["parameters"].append({"name": "area", "type": "text", "label": "Area",
                                  "choices": {"dataset": "plain", "value": "[region]", "label": "UPPER([region])"}})
    row = session.exec(select(ReportVersion)).one()
    row.content = json.dumps(content)
    session.add(row)
    session.commit()
    out = reader.get(f"/reports/{w['report']}/parameters").json()
    assert [(p["name"], p["label"], p["default"]) for p in out] == [
        ("region", "region", None), ("since", "since", "2026-09-28"), ("regions", "regions", ["N"]), ("area", "Area", None)]
    assert out[0]["choices"] == [{"value": "North", "label": "North"}, {"value": "South", "label": "South"}]
    assert out[3]["choices"] is None and "cannot read choices" in out[3]["choices_error"]
    # the dataset's filter needs a region: with it, the choices follow
    fake_engine.report_choices_response = (200, {"choices": [{"value": "N", "label": "NORTH"}], "truncated": True})
    out = reader.get(f"/reports/{w['report']}/parameters?region=South").json()
    assert out[3]["choices"] == [{"value": "N", "label": "NORTH"}] and out[3]["choices_truncated"]
    assert fake_engine.report_choices[-1]["datasets"]["plain"]["operations"][0]["params"]["value"] == "South"
    # the values given steer the choices only: the default stays the report's own — it came
    # back as the value given, and a form that read it lost the report's default
    assert [p["default"] for p in out[:3]] == [None, "2026-09-28", ["N"]]
    assert reader.get(f"/reports/{w['report']}/parameters?nope=1").status_code == 400


def test_a_dataset_s_fields(app, session, fake_engine):
    w = _world(session)
    reader = _reader(app, session, w)
    r = reader.post(f"/reports/{w['report']}/fields", json={"dataset": "plain"}, headers=CSRF)
    assert r.json() == {"dataset": "plain", "fields": [{"name": "region", "type": "string"},
                                                       {"name": "amount", "type": "float"}, {"name": "day", "type": "date"}]}
    columns = [{"name": "n", "dtype": "Int64"}, {"name": "t", "dtype": "Datetime(us)"}]
    fake_engine.preview_response = (200, {"columns": columns, "rows": [], "row_count": 0, "truncated": False})
    r = reader.post(f"/reports/{w['report']}/fields", json={"dataset": "orders"}, headers=CSRF)
    assert r.json()["fields"] == [{"name": "n", "type": "integer"}, {"name": "t", "type": "datetime"}]
    asked = fake_engine.previews[-1]
    assert asked["limit"] == 1 and "region = ''" in asked["operations"][0]["params"]["query"]  # a required one: neutral
    fake_engine.db_query_response = (200, {"columns": [{"name": "area", "dtype": "String"}], "rows": []})
    assert reader.post(f"/reports/{w['report']}/fields", json={"dataset": "erp"}, headers=CSRF).json()["fields"] == [
        {"name": "area", "type": "string"}]
    assert fake_engine.db_queries[-1]["limit"] == 1
    assert reader.post(f"/reports/{w['report']}/fields", json={"dataset": "nope"}, headers=CSRF).status_code == 404
    # the editor's definition, not saved yet, needs EDIT
    draft = {"v": 1, "datasets": [{"name": "x", "source": {"kind": "datasource", "datasource_id": w["orders"]}}]}
    assert reader.post(f"/reports/{w['report']}/fields", json={"dataset": "x", "definition": draft},
                       headers=CSRF).status_code == 403
    make_permission(session, user_id=_uid(session, "reader@x.local"), project_id=w["sales"], capability="edit")
    r = reader.post(f"/reports/{w['report']}/fields", json={"dataset": "x", "definition": draft}, headers=CSRF)
    assert r.status_code == 200 and len(r.json()["fields"]) == 3


def test_the_rows_of_a_dataset_come_with_its_fields(app, session, fake_engine):
    """«Refresh fields» counts the rows too: with the reader's rights and the parameters'
    defaults (what the viewer reads when the report opens), up to the engine's cap. A count
    that cannot be made — a required parameter without a default, an engine that fails or
    takes too long — leaves the fields as they were: never an error of its own."""
    import httpx

    from app.services import reports as service

    w = _world(session)
    reader = _reader(app, session, w)
    uid = _uid(session, "reader@x.local")
    fake_engine.db_query_response = (200, {"columns": [{"name": "area", "dtype": "String"}], "rows": []})
    fake_engine.report_count_response = (200, {"rows": 840_000, "over": False, "max_rows": 1_000_000})
    r = reader.post(f"/reports/{w['report']}/fields", json={"dataset": "erp"}, headers=CSRF)
    assert r.status_code == 200 and r.json() == {"dataset": "erp", "fields": [{"name": "area", "type": "string"}],
                                                 "rows": 840_000, "over": False, "max_rows": 1_000_000}
    [asked] = fake_engine.report_counts
    assert asked["slot"] == f"u{uid}:report:{w['report']}:count" and asked["seconds"] == service.COUNT_SECONDS == 20
    # the default of `regions`, as a literal of the connection's dialect, and its payload
    assert asked["dataset"]["sql"] == "SELECT * FROM invoices WHERE area IN ('N') AND note <> 'it''s'"
    assert asked["dataset"]["connection"]["password_encrypted"] == "encrypted"

    # over the cap: said as the engine says it
    fake_engine.report_count_response = (200, {"rows": 1_000_000, "over": True, "max_rows": 1_000_000})
    r = reader.post(f"/reports/{w['report']}/fields", json={"dataset": "kpis"}, headers=CSRF)
    assert (r.json()["rows"], r.json()["over"], r.json()["max_rows"]) == (1_000_000, True, 1_000_000)
    assert fake_engine.report_counts[-1]["dataset"]["operations"][0]["params"]["value"] == ["N"]

    # `region` is required and has no default: a count would depend on a value nobody gave
    before = len(fake_engine.report_counts)
    r = reader.post(f"/reports/{w['report']}/fields", json={"dataset": "plain"}, headers=CSRF)
    assert r.status_code == 200 and "rows" not in r.json() and len(r.json()["fields"]) == 3
    r = reader.post(f"/reports/{w['report']}/fields", json={"dataset": "orders"}, headers=CSRF)
    assert r.status_code == 200 and "rows" not in r.json()
    assert len(fake_engine.report_counts) == before

    # the engine fails, takes too long, or has no such route: the fields all the same
    for answer in ((500, {"detail": "boom"}), (504, {"detail": "Preview timed out"}), httpx.ReadTimeout("slow"), None):
        fake_engine.report_count_response = answer
        r = reader.post(f"/reports/{w['report']}/fields", json={"dataset": "erp"}, headers=CSRF)
        assert r.status_code == 200 and r.json() == {"dataset": "erp", "fields": [{"name": "area", "type": "string"}]}
    fake_engine.report_count_response = (200, {"rows": "many"})  # an answer that is not one
    r = reader.post(f"/reports/{w['report']}/fields", json={"dataset": "erp"}, headers=CSRF)
    assert r.status_code == 200 and "rows" not in r.json()

    # a dataset the reader may not read: the fields' refusal, as before, and nothing counted
    fake_engine.report_count_response = (200, {"rows": 1, "over": False, "max_rows": 1_000_000})
    before = len(fake_engine.report_counts)
    stranger = _reader(app, session, w, email="stranger@x.local", connect=False)
    r = stranger.post(f"/reports/{w['report']}/fields", json={"dataset": "erp"}, headers=CSRF)
    assert r.status_code == 422 and "erp" in r.json()["detail"] and len(fake_engine.report_counts) == before


def test_an_optional_parameter_without_a_default_is_counted_as_the_render_reads_it(app, session, fake_engine):
    """No value is NULL in the SQL, as in a render with nothing given; a required one that
    the dataset does not use does not stop its count."""
    w = _world(session)
    reader = _reader(app, session, w)
    make_permission(session, user_id=_uid(session, "reader@x.local"), project_id=w["sales"], capability="edit")
    fake_engine.report_count_response = (200, {"rows": 3, "over": False, "max_rows": 1_000_000})
    draft = {"v": 1, "parameters": [{"name": "area", "type": "text"}, {"name": "must", "type": "number", "required": True}],
             "datasets": [{"name": "x", "source": {"kind": "sql", "engine": "duckdb",
                                                   "sql": 'SELECT * FROM "Orders" WHERE region = {area}'}}]}
    r = reader.post(f"/reports/{w['report']}/fields", json={"dataset": "x", "definition": draft}, headers=CSRF)
    assert r.status_code == 200 and r.json()["rows"] == 3
    assert "WHERE region = NULL" in fake_engine.report_counts[-1]["dataset"]["operations"][0]["params"]["query"]
