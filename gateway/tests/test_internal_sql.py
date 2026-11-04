"""The AI service's internal API (routes/internal_sql.py, docs/specs/gateway-go.md «The AI
service»): the SQL parsing the Go gateway asks for, behind the internal token, only in the
role `ai`.

What each call answers is what the Python code it stands for reads: the tables are walked
as `sql_scripts.resolve` walks them (the test rebuilds resolve's text from the answer and
compares), `without_params` and `_qualified_tables` are called as they are."""
import os
import subprocess
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import select

from app.core.crypto import INTERNAL_HEADER, internal_token
from app.models import Datasource, Project
from app.routes.internal_sql import router
from app.services import openlineage, report_params, sql_scripts


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def _post(client, path, body, token=True):
    headers = {INTERNAL_HEADER: internal_token()} if token else {}
    return client.post(f"/internal/sql/{path}", json=body, headers=headers)


# ── the token ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("path", ["tables", "without-params", "qualified-tables"])
def test_without_the_token_the_routes_do_not_exist(client, path):
    r = client.post(f"/internal/sql/{path}", json={"sql": "SELECT 1", "dialect": "duckdb", "names": []})
    assert r.status_code == 404 and r.json() == {"detail": "Not Found"}
    r = client.post(f"/internal/sql/{path}", json={"sql": "SELECT 1", "dialect": "duckdb", "names": []},
                    headers={INTERNAL_HEADER: "0" * 64})
    assert r.status_code == 404
    # before the body is even read: no 422 tells an outsider what the route takes
    r = client.post(f"/internal/sql/{path}", json={"nothing": True})
    assert r.status_code == 404


def test_with_the_token_a_bad_body_is_a_422(client):
    assert _post(client, "tables", {"sql": "SELECT 1", "dialect": "sqlite"}).status_code == 422
    assert _post(client, "tables", {"dialect": "duckdb"}).status_code == 422


# ── /tables ──────────────────────────────────────────────────────────────────
def test_the_tables_of_a_query(client):
    sql = 'WITH recent AS (SELECT * FROM Vendite) SELECT * FROM recent JOIN "Città Nord" c ON 1 = 1 ' \
          "JOIN sales.orders AS o ON 1 = 1, read_parquet('x.parquet')"
    got = _post(client, "tables", {"sql": sql, "dialect": "duckdb"}).json()
    assert got["error"] is None and got["statements"] == 1 and got["query"] is True
    assert got["ctes"] == ["recent"]
    names = [(t["name"], t["folder"], t["aliased"]) for t in got["tables"]]
    assert ("Vendite", "", False) in names and ("recent", "", False) in names
    assert ("Città Nord", "", True) in names and ("orders", "sales", True) in names
    assert not any(t["name"] == "read_parquet" for t in got["tables"])  # a table function: the engine's
    for t in got["tables"]:
        # offsets in characters: the folder's start to the name's end, quotes outside
        written = sql[t["start"]:t["end"] + 1].replace('"', "")
        assert written in (t["name"], f"{t['folder']}.{t['name']}"), written


def test_a_catalog_is_said_as_the_dialect_writes_it(client):
    got = _post(client, "tables", {"sql": "SELECT * FROM db.sales.orders", "dialect": "postgres"}).json()
    (t,) = got["tables"]
    assert t["catalog"] is True and t["written"] == "db.sales.orders" and t["folder"] == "sales"


def test_what_is_not_one_query(client):
    two = _post(client, "tables", {"sql": "SELECT 1; SELECT 2", "dialect": "duckdb"}).json()
    assert two["statements"] == 2 and two["tables"] == [] and two["query"] is False
    delete = _post(client, "tables", {"sql": "DELETE FROM t", "dialect": "postgres"}).json()
    assert delete["statements"] == 1 and delete["query"] is False and [t["name"] for t in delete["tables"]] == ["t"]
    bad = _post(client, "tables", {"sql": "SELECT FROM (", "dialect": "duckdb"}).json()
    assert bad["error"] and bad["statements"] == 0 and bad["tables"] == []


def _resolve_from_answer(answer: dict, text: str, by_name: dict[str, Datasource]) -> str:
    """resolve's edits, made from the API's answer as the Go gateway makes them."""
    edits = []
    for t in answer["tables"]:
        if not t["folder"] and t["name"].lower() in answer["ctes"]:
            continue
        ds = by_name[t["name"].lower()]
        start, end = t["start"], t["end"]
        if start > 0 and text[start - 1] in "\"`[":
            start -= 1
        if end + 1 < len(text) and text[end + 1] in "\"`]":
            end += 1
        alias = f"ds_{ds.id}"
        edits.append((start, end, alias if t["aliased"] else f"{alias} AS {sql_scripts._quote(t['name'], 'duckdb')}"))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end + 1:]
    return text


def test_the_answer_rebuilds_what_resolve_writes(client, session):
    from app.models import User

    admin = User(email="a@x.y", hashed_password="x", is_superuser=True)
    folder = Project(name="Vendite")
    session.add_all([admin, folder])
    session.commit()
    for name in ("Città Nord", "ordini"):
        session.add(Datasource(name=name, project_id=folder.id, bucket="b", key=f"k/{name}", kind="file"))
    session.commit()
    by_name = {d.name.lower(): d for d in session.exec(select(Datasource)).all()}
    for sql in ('SELECT * FROM "Città Nord" JOIN Ordini o ON 1 = 1',
                'WITH x AS (SELECT 1) SELECT * FROM x, "Vendite"."ordini"',
                "SELECT a FROM ordini WHERE a IN (SELECT a FROM \"Città Nord\")"):
        text = sql_scripts.strip_comments(sql).strip()
        answer = _post(client, "tables", {"sql": text, "dialect": "duckdb"}).json()
        assert _resolve_from_answer(answer, text, by_name) == sql_scripts.resolve(session, admin, sql, "duckdb").query


# ── /without-params and /qualified-tables ────────────────────────────────────
@pytest.mark.parametrize("dialect", ["duckdb", "postgresql", "sqlserver", "clickhouse", None])
def test_without_params_is_report_params_own(client, dialect):
    sql = "SELECT region FROM t WHERE region = {region} AND (year = {year} OR {year} IS NULL)"
    got = _post(client, "without-params", {"sql": sql, "dialect": dialect, "names": ["region"]}).json()
    assert got == {"sql": report_params.without_params(sql, report_params.dialect_of(dialect), ["region"])}
    assert "{region}" not in got["sql"] and "{year}" in got["sql"]


def test_qualified_tables_is_openlineages_own(client):
    sql = "SELECT * FROM public.orders o JOIN customers c ON o.c = c.id"
    got = _post(client, "qualified-tables", {"sql": sql, "db_type": "postgresql"}).json()
    assert got == {"tables": list(openlineage._qualified_tables("postgresql", sql))} == {"tables": ["customers", "public.orders"]}
    assert _post(client, "qualified-tables", {"sql": "not sql at all (", "db_type": "mysql"}).json() == {"tables": []}


# ── the role ─────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("role, mounted", [("ai", True), ("api", False), ("all", False)])
def test_only_the_ai_role_has_the_routes(role, mounted):
    """Imported in a process of its own: main.py mounts the routers at import."""
    env = {**os.environ, "APP__ROLE": role}
    probe = ("from app.main import app\n"
             "def walk(rs):\n"
             "    for r in rs:\n"
             "        yield from walk(r.effective_candidates()) if hasattr(r, 'effective_candidates') else [getattr(r, 'path', '')]\n"
             "print('/internal/sql/tables' in set(walk(app.routes)))")
    out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, env=env,
                         cwd=os.path.dirname(os.path.dirname(__file__)), check=True)
    assert out.stdout.strip().splitlines()[-1] == str(mounted)


@pytest.mark.anyio
async def test_the_ai_role_starts_no_background_work(monkeypatch):
    import asyncio

    from app import main
    from app.core.config import get_settings

    monkeypatch.setenv("APP__ROLE", "ai")
    get_settings.cache_clear()
    assert main.start_background_work(asyncio.Event()) == []
