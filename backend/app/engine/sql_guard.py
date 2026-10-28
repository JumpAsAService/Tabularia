"""The `sql` node reads ONLY its own input: an ALLOWLIST of the tables.

On the engines that run the query on a real server (external ClickHouse, chDB, BigQuery)
a denylist of dangerous functions is not enough, as was shown live (2026-09-19):
`merge('system', '^query_log$')` was not in the list and read another table of the
server through the node; `information_schema` listed the tables. Every server version
adds table functions, so the list of bans always lags. Here the question is turned
around: not «what is forbidden» but «what is allowed», that is `self` and `input` — and
nothing else.

Why it matters for the RBAC: the engine runs with credentials that read everything. The
gateway decides WHICH datasource a user may open, but the free query then runs on the
server with those credentials: if it could name another table, the permission on the
datasource would be decorative. It holds for the flow editor and, since 2026-09-19, for
the AI assistant, which puts a free query in the hands of anyone with VIEW.

DuckDB and Polars do not come through here: DuckDB runs the node in an in-memory
connection with external access off and locked, Polars knows only the registered frame.
There is no server there to read anything else from.
"""
from __future__ import annotations

import re

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import traverse_scope

from app.engine.exceptions import EngineError

INPUT_NAMES = frozenset({"self", "input"})

# the names of the other tables a query may read (a SQL script on the catalog: the gateway
# names each datasource it resolved `ds_<id>`), checked before they reach the allowlist
TABLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
MAX_TABLES = 20

# table functions that make rows out of nothing: they read nothing, they stay allowed
HARMLESS_TABLE_FUNCTIONS = frozenset({
    "numbers", "numbers_mt", "zeros", "zeros_mt", "generateseries", "generate_series",
    "values", "unnest", "generate_array", "generate_date_array", "generate_timestamp_array",
})

# SCALAR functions that read other things from the server (dictionaries, Join tables,
# settings and macros, files): they do not appear as tables, they must be named. `file`
# reads any file of the process on chDB, and the denylist of chdb_ops does not see it
# quoted (`"file"(…)`); here the parse tree sees it whatever its quotes.
_FORBIDDEN_SCALARS = (
    "dictget", "dicthas", "dictisin", "joinget", "getsetting", "getserversetting",
    "getmacro", "hascolumnintable", "getclienthttpheader", "file",
)

_REFUSAL = (
    "sql: the query can read only the input of the node (`self` or `input`). "
    "{what} is not allowed: to combine other data use a Join or Union node."
)


def _function_name(node: exp.Expression) -> str:
    name = getattr(node, "name", "") or ""
    return (name or node.key or "").lower()


def table_names(params: dict) -> dict[str, dict]:
    """The `tables` of a `sql` operation, {name: right side as a join's}, checked: a
    handful, plain lowercase names, never one of the input's."""
    tables = params.get("tables") or {}
    if not isinstance(tables, dict):
        raise EngineError("sql: `tables` must map names to sources")
    if len(tables) > MAX_TABLES:
        raise EngineError(f"sql: at most {MAX_TABLES} tables in one query")
    for name, ref in tables.items():
        if not isinstance(name, str) or not TABLE_NAME.match(name) or name in INPUT_NAMES:
            raise EngineError(f"sql: {name!r} is not a valid table name")
        if not isinstance(ref, dict):
            raise EngineError(f"sql: the table {name} has no source")
    return tables


def rename_tables(query: str, dialect: str, names: dict[str, str]) -> str:
    """The query with its references to the tables of `names` (case-insensitive, without a
    schema, not one of its own CTEs) renamed, by their position in the text: nothing else
    is rewritten, so the query stays as its author wrote it. Call it after
    `ensure_reads_only_input`, which refuses a query sqlglot cannot read."""
    wanted = {k.lower(): v for k, v in names.items()}
    tree = sqlglot.parse_one(query, read=dialect)
    own = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    edits: list[tuple[int, int, str]] = []
    for table in tree.find_all(exp.Table):
        name = table.name.lower()
        if table.db or name not in wanted or name in own or not isinstance(table.this, exp.Identifier):
            continue
        start, end = table.this.meta.get("start"), table.this.meta.get("end")
        if start is None or end is None:
            raise EngineError("sql: the query's tables could not be placed in its text: write them without quotes")
        edits.append((start, end, wanted[name]))
    for start, end, name in sorted(edits, reverse=True):
        query = query[:start] + name + query[end + 1:]
    return query


def ensure_reads_only_input(query: str, dialect: str, tables: frozenset[str] | set[str] = frozenset()) -> None:
    """Raises EngineError when the query names a table that is not its input, nor one of
    the `tables` the operation brings.

    A query sqlglot cannot parse is REFUSED: letting through what was not understood
    would reopen the hole this module closes."""
    allowed = INPUT_NAMES | {t.lower() for t in tables}
    try:
        tree = sqlglot.parse_one(query, read=dialect)
    except (SqlglotError, AttributeError, TypeError) as e:  # sqlglot's own bugs on odd SQL too
        raise EngineError(
            "sql: the query could not be parsed, so it was not run (the SQL node checks that the query "
            f"reads only the input). Simplify it or use the dedicated nodes. Detail: {str(e)[:200]}"
        ) from e
    if tree is None:
        raise EngineError("sql: the query is empty")

    # 1) the TRUE sources of every scope: a CTE or a subquery is a Scope, a server
    #    table is an exp.Table. Looking at the scope (and not only the CTE names) stops
    #    defining a CTE `x` in a subquery and then reading the real table `x` outside it.
    real: set[int] = set()
    try:
        for scope in traverse_scope(tree):
            for source in scope.sources.values():
                if isinstance(source, exp.Table):
                    real.add(id(source))
                    _check_table(source, allowed)
    except SqlglotError as e:
        raise EngineError(f"sql: the query could not be parsed, so it was not run. Detail: {str(e)[:200]}") from e

    # the CTE names: legitimate sources anywhere in the query
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}

    # 2) a safety net: every exp.Table the scope analysis did not visit (constructs of
    #    the dialect it does not know) goes through the same check, the CTE names the
    #    only tolerance
    for table in tree.find_all(exp.Table):
        if id(table) in real:
            continue
        if isinstance(table.this, exp.Identifier) and not table.db and not table.catalog \
                and table.name.lower() in cte_names:
            continue
        _check_table(table, allowed)

    # 3) Everything in FROM/JOIN position must be a source the analysis recognized. The
    #    node's TYPE cannot be trusted: in the ClickHouse dialect sqlglot parses
    #    `db.table` inside an ARRAY JOIN's subquery as exp.Column, not exp.Table — so
    #    neither `traverse_scope` nor `find_all(exp.Table)` sees it, and the allowlist
    #    looked in the wrong place. Found by the 2026-09-19 audit (A9), after the same
    #    class of problem had already cost the merge() incident. Here the POSITION is
    #    looked at, which the dialect cannot fake.
    for branch in tree.find_all(exp.From, exp.Join):
        _check_source(branch.this, cte_names, allowed)

    # 4) ClickHouse: `x IN table_name` reads a table without a FROM
    for node in tree.find_all(exp.In):
        if node.args.get("field") is not None:
            raise EngineError(_REFUSAL.format(what=f"`IN {node.args['field'].sql()}`"))

    # 5) scalar functions that read from the server
    for func in tree.find_all(exp.Func):
        name = _function_name(func)
        if name.startswith(_FORBIDDEN_SCALARS):
            raise EngineError(_REFUSAL.format(what=f"The function `{name}`"))


def _check_source(node: exp.Expression | None, cte_names: set[str], allowed: frozenset[str] = INPUT_NAMES) -> None:
    """Validates what appears as the source of a FROM or a JOIN.

    The POSITION is looked at and not the node's type, because the type is not reliable:
    inside an ARRAY JOIN's subquery sqlglot classifies both `self` and
    `information_schema.tables` as `exp.Column`. The true discriminant is the QUALIFIER — a
    dotted name in a source position is always someone else's table."""
    if node is None:
        return
    if isinstance(node, exp.Table):
        if isinstance(node.this, exp.Identifier) and not node.db and not node.catalog \
                and node.name.lower() in cte_names:
            return  # a CTE is a legitimate source
        _check_table(node, allowed)
        return
    if isinstance(node, (exp.Subquery, exp.Unnest, exp.Values, exp.Lateral)):
        return  # their content went through the scopes already
    if isinstance(node, exp.Column):
        # `self`/`input` and the CTEs are BARE names; anything qualified (`db.table`) in
        # a source position is a foreign table
        if not node.table and node.name.lower() in (allowed | cte_names):
            return
        raise EngineError(_REFUSAL.format(what=f"The table `{node.sql()[:80]}`"))
    if isinstance(node, exp.Dot):
        raise EngineError(_REFUSAL.format(what=f"The reference `{node.sql()[:80]}`"))


def _check_table(table: exp.Table, allowed: frozenset[str] = INPUT_NAMES) -> None:
    if not isinstance(table.this, exp.Identifier):
        name = _function_name(table.this)
        if name in HARMLESS_TABLE_FUNCTIONS:
            return
        raise EngineError(_REFUSAL.format(what=f"The table function `{name or table.this.sql()[:40]}`"))
    if table.db or table.catalog or table.name.lower() not in allowed:
        raise EngineError(_REFUSAL.format(what=f"The table `{table.sql()[:80]}`"))
