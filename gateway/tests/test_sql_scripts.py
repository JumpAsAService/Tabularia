"""SQL scripts (docs/specs/sql-scripts.md, piece 1).

The rules these tests hold: VIEW on the folder sees, reads and runs a script, EDIT
changes it, the rest get 404; a run reads with the authority of whoever runs it, and a
datasource they cannot read is one that does not exist; the SQL is changed only where a
datasource is named, comments go, the rest stays the person's to the character; a name two
folders share needs its folder; only one SELECT runs; every run is audited with its SQL;
versions, restore, duplicate, move, delete, the folder that holds one, the search.
On a connection (piece 2): CONNECT on its folder runs the SQL and chooses the connection;
the SQL goes to the database as it is; VIEW alone reads the script and does not run it.
"""
import json

import pytest
from fastapi import FastAPI, HTTPException
from sqlmodel import select

from app.db.session import get_session
from app.models import AuditLog, Connection, SqlScript, SqlScriptVersion
from app.routes import sql_scripts as routes
from app.services import audit, sql_scripts
from tests.conftest import make_datasource, make_permission, make_project, make_user
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


def _world(session):
    """Two folders: «Vendite» and «Marketing», each with a «Clienti»; «Riservato» with «Stipendi»."""
    sales, marketing, secret = (make_project(session, name=n) for n in ("Vendite", "Marketing", "Riservato"))
    cols = json.dumps([{"name": "id", "dtype": "Int64"}, {"name": "importo", "dtype": "Float64"}])
    ds = {
        "ordini": make_datasource(session, name="Ordini Nord 2024", project_id=sales.id, key="datasets/1/o.parquet", columns=cols),
        "clienti": make_datasource(session, name="Clienti", project_id=sales.id, key="datasets/2/c.parquet"),
        "clienti_mkt": make_datasource(session, name="Clienti", project_id=marketing.id, key="datasets/3/c.parquet"),
        "stipendi": make_datasource(session, name="Stipendi", project_id=secret.id, key="datasets/4/s.parquet"),
        "vuota": make_datasource(session, name="Vuota", project_id=sales.id, key="", kind="database"),
    }
    # ids only: the requests of a test client leave the objects detached
    return sales.id, marketing.id, secret.id, {k: d.id for k, d in ds.items()}


# ── the SQL ───────────────────────────────────────────────────────────────────
def test_comments_go_and_quoted_text_stays():
    sql = "SELECT '--not a comment', \"a--b\" /* note */ FROM t -- why\nWHERE x = 'it''s /* fine */'"
    assert sql_scripts.strip_comments(sql) == "SELECT '--not a comment', \"a--b\"   FROM t \nWHERE x = 'it''s /* fine */'"


def test_names_become_aliases_and_the_rest_stays_to_the_character(session):
    sales, marketing, _, ds = _world(session)
    me = make_user(session, email="ana@x.local")
    for p in (sales, marketing):
        make_permission(session, user_id=me.id, project_id=p)
    sql = ('SELECT o.importo, "Ordini Nord 2024".id   FROM "ordini nord 2024" o\n'
           'JOIN "Vendite"."Clienti" ON o.id = "Clienti".id -- the customers\n'
           'WHERE o.importo >  10;')
    r = sql_scripts.resolve(session, me, sql, "duckdb")
    o, c = ds["ordini"], ds["clienti"]
    assert r.query == (f'SELECT o.importo, "Ordini Nord 2024".id   FROM ds_{o} o\n'
                       f'JOIN ds_{c} AS "Clienti" ON o.id = "Clienti".id \nWHERE o.importo >  10')
    assert list(r.tables) == [f"ds_{o}", f"ds_{c}"]
    # a CTE named like a datasource is the CTE
    r = sql_scripts.resolve(session, me, 'WITH stipendi AS (SELECT 1 AS a) SELECT * FROM stipendi', "duckdb")
    assert r.tables == {}
    # BigQuery quotes with backticks
    r = sql_scripts.resolve(session, me, "SELECT * FROM `Ordini Nord 2024`", "bigquery")
    assert r.query == f"SELECT * FROM ds_{o} AS `Ordini Nord 2024`"


def test_what_cannot_run(session):
    sales, marketing, _, _ = _world(session)
    me = make_user(session, email="ana@x.local")
    for p in (sales, marketing):
        make_permission(session, user_id=me.id, project_id=p)

    def refused(sql):
        with pytest.raises(HTTPException) as e:
            sql_scripts.resolve(session, me, sql, "duckdb")
        assert e.value.status_code == 422
        return e.value.detail

    # unreadable and missing say the same
    assert refused('SELECT * FROM "Stipendi"') == "There is no datasource «Stipendi» you can read"
    assert refused('SELECT * FROM "Nessuna"') == "There is no datasource «Nessuna» you can read"
    assert "more than one folder (Marketing, Vendite)" in refused('SELECT * FROM "Clienti"')
    assert refused('SELECT * FROM "Riservato"."Clienti"') == "There is no datasource «Riservato.Clienti» you can read"
    assert refused('SELECT * FROM "Marketing"."clienti" JOIN "Vendite"."Stipendi" USING (id)') == \
        "There is no datasource «Vendite.Stipendi» you can read"
    assert refused('SELECT * FROM "Vuota"') == "The datasource «Vuota» has no data yet"
    assert "One query at a time" in refused('SELECT * FROM "Clienti" ; SELECT 1')
    assert "start with SELECT or WITH" in refused('DELETE FROM "Ordini Nord 2024"')
    assert refused("  -- only a comment\n ;") == "The query is empty"
    assert "could not be read" in refused("SELECT * FROM (")
    assert "by its folder and its name" in refused('SELECT * FROM a.b.c')


# ── the object ────────────────────────────────────────────────────────────────
def test_create_read_run_and_change_by_permission(app, session, fake_engine):
    sales, _, _, ds = _world(session)
    boss = _sign_in(app, session, "capo@x.local")
    reader = _sign_in(app, session, "lettore@x.local")
    stranger = _sign_in(app, session, "altro@x.local")
    from app.models import User

    by = {u.email: u.id for u in session.exec(select(User)).all()}
    make_permission(session, user_id=by["capo@x.local"], project_id=sales, capability="edit")
    make_permission(session, user_id=by["lettore@x.local"], project_id=sales, capability="view")

    r = boss.post(f"/projects/{sales}/sql-scripts", json={"name": "Top ordini", "content": {"sql": 'SELECT * FROM "Clienti"'}}, headers=CSRF)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["current_version"] == 1 and r.json()["editable"]
    assert boss.post(f"/projects/{sales}/sql-scripts", json={"name": "Top ordini"}, headers=CSRF).status_code == 409

    seen = reader.get(f"/sql-scripts/{sid}").json()
    assert seen["content"]["sql"] == 'SELECT * FROM "Clienti"' and not seen["editable"]
    assert [s["name"] for s in reader.get(f"/projects/{sales}/sql-scripts").json()] == ["Top ordini"]
    assert reader.patch(f"/sql-scripts/{sid}", json={"name": "Mio"}, headers=CSRF).status_code == 403
    assert stranger.get(f"/sql-scripts/{sid}").status_code == 404
    assert stranger.patch(f"/sql-scripts/{sid}", json={"name": "Mio"}, headers=CSRF).status_code == 404

    # the reader runs it, with their own authority
    fake_engine.preview_response = (200, {"columns": [{"name": "id", "dtype": "Int64"}], "rows": [{"id": 1}],
                                          "row_count": 1, "truncated": False})
    out = reader.post("/sql-scripts/run", json={"sql": 'SELECT id FROM "Clienti"', "script_id": sid, "engine": "duckdb"},
                      headers=CSRF)
    assert out.status_code == 200, out.text
    assert out.json()["rows"] == [{"id": 1}] and out.json()["datasources"] == [{"id": ds["clienti"], "name": "Clienti"}]
    body = fake_engine.previews[-1]
    c = ds["clienti"]
    assert (body["bucket"], body["input_key"], body["engine"], body["no_cache"]) == ("data-prep", "datasets/2/c.parquet", "duckdb", True)
    assert body["operations"] == [{"type": "sql", "params": {
        "query": f'SELECT id FROM ds_{c} AS "Clienti"',
        "tables": {f"ds_{c}": {"source": {"bucket": "data-prep", "key": "datasets/2/c.parquet"}}}}}]
    assert body["slot"] == f"u{by['lettore@x.local']}:sql:{sid}"
    # a stranger may not run it as that script, nor read the datasource as a draft
    assert stranger.post("/sql-scripts/run", json={"sql": 'SELECT 1 FROM "Clienti"', "script_id": sid}, headers=CSRF).status_code == 404
    assert stranger.post("/sql-scripts/run", json={"sql": 'SELECT 1 FROM "Clienti"'}, headers=CSRF).status_code == 422

    runs = session.exec(select(AuditLog).where(AuditLog.action == audit.SQL_SCRIPT_RUN)).all()
    assert len(runs) == 1 and runs[0].target_id == sid and json.loads(runs[0].detail)["sql"] == 'SELECT id FROM "Clienti"'


def test_an_engine_refusal_speaks_of_the_datasources(app, session, fake_engine):
    sales, _, _, ds = _world(session)
    me = _sign_in(app, session, "ana@x.local", is_superuser=True)
    c = ds["clienti"]
    fake_engine.preview_response = (422, {"detail": f"sql: the query is not valid: Binder Error: column nope not found in ds_{c}"})
    r = me.post("/sql-scripts/run", json={"sql": 'SELECT nope FROM "Vendite"."Clienti"'}, headers=CSRF)
    assert r.status_code == 422 and r.json()["detail"] == "the query is not valid: Binder Error: column nope not found in Clienti"
    failed = session.exec(select(AuditLog).where(AuditLog.action == audit.SQL_SCRIPT_RUN)).one()
    assert failed.outcome == "failure" and failed.target_id is None


def test_versions_restore_duplicate_move_delete(app, session):
    sales, marketing, _, _ = _world(session)
    me = _sign_in(app, session, "capo@x.local", is_superuser=True)
    sid = me.post(f"/projects/{sales}/sql-scripts", json={"name": "Q", "content": {"sql": "SELECT 1"}}, headers=CSRF).json()["id"]
    assert me.patch(f"/sql-scripts/{sid}", json={"content": {"sql": "SELECT 2"}}, headers=CSRF).json()["current_version"] == 2
    # the same content again: no new version
    assert me.patch(f"/sql-scripts/{sid}", json={"content": {"sql": "SELECT 2"}}, headers=CSRF).json()["current_version"] == 2
    assert [(v["version"], v["note"], v["current"]) for v in me.get(f"/sql-scripts/{sid}/versions").json()] == [
        (2, "edited", True), (1, "created", False)]
    assert me.get(f"/sql-scripts/{sid}/versions/1").json()["sql"] == "SELECT 1"
    restored = me.post(f"/sql-scripts/{sid}/versions/1/restore", headers=CSRF).json()
    assert restored["current_version"] == 3 and restored["content"]["sql"] == "SELECT 1"
    assert me.patch(f"/sql-scripts/{sid}", json={"content": {"sql": "x" * (sql_scripts.MAX_SQL_CHARS + 1)}}, headers=CSRF).status_code == 422
    assert me.patch(f"/sql-scripts/{sid}", json={"content": {"sql": "SELECT 1", "connection_id": 4}}, headers=CSRF).status_code == 404

    empty = make_project(session, name="Analisi").id
    copy = me.post(f"/sql-scripts/{sid}/duplicate", headers=CSRF).json()
    assert copy["name"] == "Q (1)" and copy["content"]["sql"] == "SELECT 1" and copy["current_version"] == 1
    assert me.patch(f"/sql-scripts/{sid}", json={"project_id": empty, "name": "Q2"}, headers=CSRF).json()["project_id"] == empty
    # a folder holding one cannot be deleted
    r = me.delete(f"/projects/{empty}", headers=CSRF)
    assert r.status_code == 409 and "SQL scripts" in r.json()["detail"]
    hits = me.get("/search", params={"q": "q2"}).json()
    assert [(h["kind"], h["name"]) for h in hits["items"]] == [("sql", "Q2")]
    assert me.delete(f"/sql-scripts/{sid}", headers=CSRF).status_code == 204
    assert session.get(SqlScript, sid) is None
    assert session.exec(select(SqlScriptVersion).where(SqlScriptVersion.script_id == sid)).all() == []
    actions = [a.action for a in session.exec(select(AuditLog).order_by(AuditLog.id)).all() if a.action.startswith("sql_script.")]
    assert actions == ["sql_script.create", "sql_script.update", "sql_script.restore", "sql_script.create",
                       "sql_script.update", "sql_script.delete"]


def test_the_catalog_lists_what_the_person_reads(app, session):
    sales, marketing, _, ds = _world(session)
    me = _sign_in(app, session, "ana@x.local")
    from app.models import User

    uid = session.exec(select(User).where(User.email == "ana@x.local")).one().id
    make_permission(session, user_id=uid, project_id=sales)
    items = me.get("/sql-scripts/catalog").json()
    assert sorted((i["name"], i["folder"], i["has_data"]) for i in items) == [
        ("Clienti", "Vendite", True), ("Ordini Nord 2024", "Vendite", True), ("Vuota", "Vendite", False)]
    ordini = next(i for i in items if i["name"] == "Ordini Nord 2024")
    assert ordini["columns"] == [{"name": "id", "type": "Int64"}, {"name": "importo", "type": "Float64"}]


# ── on a connection ───────────────────────────────────────────────────────────
def _connection(session, project_id, db_type="postgresql", name="Gestionale"):
    c = Connection(name=name, project_id=project_id, db_type=db_type, host="db", database="erp",
                   username="lettore", password_encrypted="cifrata")
    session.add(c)
    session.commit()
    session.refresh(c)
    return c.id


def test_a_script_on_a_connection_runs_with_connect(app, session, fake_engine):
    sales, _, secret, _ = _world(session)
    pg = _connection(session, secret)
    smtp = _connection(session, secret, db_type="smtp", name="Posta")
    boss = _sign_in(app, session, "capo@x.local")
    reader = _sign_in(app, session, "lettore@x.local")
    from app.models import User

    by = {u.email: u.id for u in session.exec(select(User)).all()}
    make_permission(session, user_id=by["capo@x.local"], project_id=sales, capability="edit")
    make_permission(session, user_id=by["lettore@x.local"], project_id=sales, capability="view")
    sql = "SELECT cliente, sum(importo) FROM ordini -- the ERP's own table\nGROUP BY 1;"

    # choosing a connection needs CONNECT on its folder, and a database
    new = {"name": "Dal gestionale", "content": {"sql": sql, "connection_id": pg}}
    assert boss.post(f"/projects/{sales}/sql-scripts", json=new, headers=CSRF).status_code == 403
    make_permission(session, user_id=by["capo@x.local"], project_id=secret, capability="connect")
    mail = {"name": "Posta", "content": {"sql": sql, "connection_id": smtp}}
    assert boss.post(f"/projects/{sales}/sql-scripts", json=mail, headers=CSRF).status_code == 404
    r = boss.post(f"/projects/{sales}/sql-scripts", json={**new, "content": {**new["content"], "engine": "duckdb"}},
                  headers=CSRF)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert r.json()["content"] == {"sql": sql, "engine": None, "connection_id": pg, "output": None}  # no engine on a database
    assert r.json()["connection"] == {"id": pg, "name": "Gestionale", "db_type": "postgresql", "usable": True}

    # the SQL goes as it is, with the connection's encrypted password and the script's slot
    fake_engine.db_query_response = (200, {"columns": [{"name": "cliente", "dtype": "String"}], "rows": [{"cliente": "Ada"}],
                                           "row_count": 1, "truncated": True})
    out = boss.post("/sql-scripts/run", json={"sql": sql, "connection_id": pg, "script_id": sid, "limit": 50}, headers=CSRF)
    assert out.status_code == 200, out.text
    assert out.json()["rows"] == [{"cliente": "Ada"}] and out.json()["truncated"] and out.json()["engine"] is None
    assert out.json()["connection"] == {"id": pg, "name": "Gestionale", "db_type": "postgresql"}
    body = fake_engine.db_queries[-1]
    assert (body["sql"], body["limit"], body["slot"]) == (sql, 50, f"u{by['capo@x.local']}:sql:{sid}")
    assert body["connection"]["password_encrypted"] == "cifrata" and body["connection"]["database"] == "erp"
    assert fake_engine.previews == []  # nothing on the catalog

    # VIEW on the script's folder reads it, and does not run it without CONNECT
    seen = reader.get(f"/sql-scripts/{sid}").json()
    assert seen["content"]["sql"] == sql and seen["connection"]["usable"] is False
    r = reader.post("/sql-scripts/run", json={"sql": sql, "connection_id": pg, "script_id": sid}, headers=CSRF)
    assert r.status_code == 403 and "CONNECT permission" in r.json()["detail"]
    assert len(fake_engine.db_queries) == 1

    # the database's refusal comes back as it is; a run that ran out of time says so
    fake_engine.db_query_response = (422, {"detail": 'PostgreSQL: relation "ordini" does not exist (42P01)'})
    r = boss.post("/sql-scripts/run", json={"sql": sql, "connection_id": pg}, headers=CSRF)
    assert r.status_code == 422 and r.json()["detail"] == 'PostgreSQL: relation "ordini" does not exist (42P01)'
    fake_engine.db_query_response = (504, {"detail": "Preview timed out: the worker did not answer in time"})
    r = boss.post("/sql-scripts/run", json={"sql": sql, "connection_id": pg}, headers=CSRF)
    assert r.status_code == 504 and r.json()["detail"] == "The query took too long and was stopped"
    assert boss.post("/sql-scripts/run", json={"sql": " -- nothing\n", "connection_id": pg}, headers=CSRF).status_code == 422

    runs = session.exec(select(AuditLog).where(AuditLog.action == audit.SQL_SCRIPT_RUN).order_by(AuditLog.id)).all()
    assert [(r.outcome, json.loads(r.detail).get("connection")) for r in runs] == [
        ("success", {"id": pg, "name": "Gestionale"}), ("failure", {"id": pg, "name": "Gestionale"}),
        ("failure", {"id": pg, "name": "Gestionale"})]

    # an editor without CONNECT changes the SQL of a script that has the connection already,
    # and cannot point it at another one
    make_permission(session, user_id=by["lettore@x.local"], project_id=sales, capability="edit")
    other = _connection(session, secret, name="Magazzino")
    assert reader.patch(f"/sql-scripts/{sid}", json={"content": {"sql": "SELECT 2", "connection_id": pg}},
                        headers=CSRF).status_code == 200
    assert reader.patch(f"/sql-scripts/{sid}", json={"content": {"sql": "SELECT 2", "connection_id": other}},
                        headers=CSRF).status_code == 403

    # a connection deleted: the script says so
    session.delete(session.get(Connection, pg))
    session.commit()
    assert boss.get(f"/sql-scripts/{sid}").json()["connection"] == {"id": pg, "name": "", "db_type": "", "usable": False}
    r = boss.post("/sql-scripts/run", json={"sql": "SELECT 1", "connection_id": pg}, headers=CSRF)
    assert r.status_code == 404 and "no longer exists" in r.json()["detail"]


def test_a_tables_columns_need_connect(app, session, fake_engine):
    from app.routes import connections as connection_routes

    app.include_router(connection_routes.router)
    _, _, secret, _ = _world(session)
    pg = _connection(session, secret)
    me = _sign_in(app, session, "capo@x.local")
    assert me.get(f"/connections/{pg}/columns", params={"table": "public.ordini"}).status_code == 403
    from app.models import User

    make_permission(session, user_id=session.exec(select(User)).one().id, project_id=secret, capability="connect")
    fake_engine.db_responses["columns"] = (200, {"columns": [{"name": "id", "dtype": "Int64"}]})
    r = me.get(f"/connections/{pg}/columns", params={"table": "public.ordini"})
    assert r.status_code == 200 and r.json() == {"columns": [{"name": "id", "dtype": "Int64"}]}
    assert fake_engine.db_inspects[-1]["action"] == "columns" and fake_engine.db_inspects[-1]["table"] == "public.ordini"


def test_the_whole_result_exports_with_the_runs_checks(app, session, fake_engine):
    sales, _, secret, ds = _world(session)
    pg = _connection(session, secret)
    boss = _sign_in(app, session, "capo@x.local")
    reader = _sign_in(app, session, "lettore@x.local")
    stranger = _sign_in(app, session, "altro@x.local")
    from app.models import User

    by = {u.email: u.id for u in session.exec(select(User)).all()}
    make_permission(session, user_id=by["capo@x.local"], project_id=sales, capability="edit")
    make_permission(session, user_id=by["capo@x.local"], project_id=secret, capability="connect")
    make_permission(session, user_id=by["lettore@x.local"], project_id=sales, capability="view")
    r = boss.post(f"/projects/{sales}/sql-scripts", json={"name": 'Ordini: "nord"/sud', "content": {"sql": "SELECT 1"}},
                  headers=CSRF)
    sid = r.json()["id"]

    # on the catalog: the run's chain, the whole result, owned by whoever asked
    out = reader.post("/sql-scripts/export", json={"sql": 'SELECT id FROM "Clienti"', "format": "xlsx", "script_id": sid,
                                                   "engine": "duckdb"}, headers=CSRF)
    assert out.status_code == 200, out.text
    assert out.json() == {"task_id": "t-export-1", "filename": "Ordini nord sud.xlsx"}
    path, body = fake_engine.exports[-1]
    c = ds["clienti"]
    assert path == "/tasks/exports" and body["owner"] == f"user:{by['lettore@x.local']}"
    assert (body["bucket"], body["input_key"], body["engine"], body["format"]) == ("data-prep", "datasets/2/c.parquet", "duckdb", "xlsx")
    assert body["operations"][0]["params"]["query"] == f'SELECT id FROM ds_{c} AS "Clienti"'
    assert "limit" not in body and "slot" not in body
    # a datasource the person cannot read is not there, as for a run
    assert stranger.post("/sql-scripts/export", json={"sql": 'SELECT 1 FROM "Clienti"'}, headers=CSRF).status_code == 422
    assert stranger.post("/sql-scripts/export", json={"sql": "SELECT 1", "script_id": sid}, headers=CSRF).status_code == 404

    # on a connection: CONNECT, the SQL as it is, a draft's name
    r = reader.post("/sql-scripts/export", json={"sql": "SELECT 1", "connection_id": pg}, headers=CSRF)
    assert r.status_code == 403
    out = boss.post("/sql-scripts/export", json={"sql": "SELECT * FROM ordini", "connection_id": pg}, headers=CSRF)
    assert out.status_code == 200 and out.json()["filename"] == "sql-script.csv"
    path, body = fake_engine.exports[-1]
    assert path == "/db/exports" and body["sql"] == "SELECT * FROM ordini" and body["format"] == "csv"
    assert body["connection"]["password_encrypted"] == "cifrata" and body["owner"] == f"user:{by['capo@x.local']}"
    assert len(fake_engine.exports) == 2 and fake_engine.previews == [] and fake_engine.db_queries == []

    exported = session.exec(select(AuditLog).where(AuditLog.action == audit.SQL_SCRIPT_EXPORT).order_by(AuditLog.id)).all()
    assert [(e.outcome, json.loads(e.detail)["format"]) for e in exported] == [("success", "xlsx"), ("success", "csv")]
    assert json.loads(exported[0].detail)["datasources"] == [{"id": c, "name": "Clienti"}] and exported[0].target_id == sid
    assert json.loads(exported[1].detail)["connection"] == {"id": pg, "name": "Gestionale"}
