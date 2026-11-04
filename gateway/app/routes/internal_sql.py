"""The AI service's internal API: the SQL parsing the Go gateway asks for.

docs/specs/gateway-go.md, «The AI service». Mounted only when `APP__ROLE=ai` (main.py), out
of the OpenAPI schema, behind the token the gateway already sends the engine
(`core/crypto.internal_token`): whoever does not carry it is told the routes do not exist.

Each call is one use of sqlglot the gateway makes, with the very expressions of the Python
code it stands for; everything around the parse (who may read what, the refusals' words,
the text the engine gets) is the Go gateway's:

- `/tables`: what `sql_scripts.resolve` and `sql_scripts._named_tables` read of a SQL;
- `/without-params`: `report_params.without_params`, as it is;
- `/qualified-tables`: `openlineage._qualified_tables`, as it is.

And, on its own prefix, what only Python's own regular expressions can say:

- `/internal/python/re-compile`: whether `re.compile` takes each pattern
  (`contracts.validate_document`, a «pattern» rule).

Offsets in the answers count characters (code points), as Python indexes a string.
"""
from __future__ import annotations

import hmac
import re
from typing import Literal, Optional

import sqlglot
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlglot import exp
from sqlglot.errors import SqlglotError

from app.core.crypto import INTERNAL_HEADER, internal_token
from app.services import openlineage, report_params


def _internal_only(request: Request) -> None:
    """The internal token, compared in constant time; without it, the 404 of a route that
    does not exist."""
    given = request.headers.get(INTERNAL_HEADER, "")
    if not hmac.compare_digest(given.encode(), internal_token().encode()):
        raise HTTPException(status_code=404, detail="Not Found")


router = APIRouter(prefix="/internal/sql", include_in_schema=False, dependencies=[Depends(_internal_only)])

# the dialects the gateway reads a SQL in: sql_scripts.DIALECTS' values and report_params'
SqlglotDialect = Literal["postgres", "duckdb", "clickhouse", "bigquery", "mysql", "tsql", "oracle", "trino"]


class TablesIn(BaseModel):
    sql: str
    dialect: SqlglotDialect


class TableOut(BaseModel):
    name: str  # table.name
    folder: str  # table.db
    catalog: bool  # a catalog qualifies it
    written: str  # table.sql(dialect), only for one with a catalog (the only one resolve says)
    aliased: bool  # bool(table.alias)
    start: Optional[int]  # where its folder, or its name when it has none, starts
    end: Optional[int]  # where its name ends (the last character)


class TablesOut(BaseModel):
    error: Optional[str] = None  # str() of sqlglot's refusal
    statements: int = 0
    query: bool = False  # the only statement is an exp.Query
    ctes: list[str] = []  # alias_or_name.lower() of every CTE
    tables: list[TableOut] = []


@router.post("/tables", response_model=TablesOut)
def tables(body: TablesIn) -> TablesOut:
    """The tables a SQL names, as `sql_scripts.resolve` and `_named_tables` walk them:
    `find_all(exp.Table)` of the only statement, table functions left out."""
    try:
        trees = [t for t in sqlglot.parse(body.sql, read=body.dialect) if t is not None]
    except SqlglotError as e:
        return TablesOut(error=str(e))
    out = TablesOut(statements=len(trees))
    if len(trees) != 1:
        return out
    tree = trees[0]
    out.query = isinstance(tree, exp.Query)
    for c in tree.find_all(exp.CTE):
        if (name := c.alias_or_name.lower()) not in out.ctes:
            out.ctes.append(name)
    for table in tree.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier):
            continue
        folder = table.db
        start_node = table.args["db"] if folder else table.this
        out.tables.append(TableOut(
            name=table.name, folder=folder, catalog=bool(table.catalog),
            written=table.sql(dialect=body.dialect) if table.catalog else "", aliased=bool(table.alias),
            start=start_node.meta.get("start"), end=table.this.meta.get("end"),
        ))
    return out


class WithoutParamsIn(BaseModel):
    sql: str
    dialect: Optional[str] = None  # a key of report_params.DIALECTS: an engine or a connection type
    names: list[str]


class SqlOut(BaseModel):
    sql: str


@router.post("/without-params", response_model=SqlOut)
def without_params(body: WithoutParamsIn) -> SqlOut:
    """`report_params.without_params`: the conditions on `names` taken for true."""
    return SqlOut(sql=report_params.without_params(body.sql, report_params.dialect_of(body.dialect), body.names))


class QualifiedIn(BaseModel):
    sql: str
    db_type: Optional[str] = None  # the connection's type


class QualifiedOut(BaseModel):
    tables: list[str]


@router.post("/qualified-tables", response_model=QualifiedOut)
def qualified_tables(body: QualifiedIn) -> QualifiedOut:
    """`openlineage._qualified_tables`: the qualified names of the tables a query reads."""
    return QualifiedOut(tables=list(openlineage._qualified_tables(body.db_type, body.sql)))


# ── Python's own regular expressions ────────────────────────────────────────

regex_router = APIRouter(prefix="/internal/python", include_in_schema=False, dependencies=[Depends(_internal_only)])


class PatternsIn(BaseModel):
    patterns: list[str]


class CompilesOut(BaseModel):
    compiles: list[bool]  # one per pattern, in their order


@regex_router.post("/re-compile", response_model=CompilesOut)
def re_compile(body: PatternsIn) -> CompilesOut:
    """Whether `re.compile` takes each pattern, as `contracts._check_rule` asks it: `re.error`
    is a no; any other exception is not caught, as there (a 500)."""
    out = []
    for pattern in body.patterns:
        try:
            re.compile(pattern)
        except re.error:
            out.append(False)
        else:
            out.append(True)
    return CompilesOut(compiles=out)
