"""The check of the compute node's expressions: SCALAR expressions only.

A compute expression runs inside the engine (ClickHouse, chDB…), with its permissions: it
must not be able to read other tables, files, URLs or the server's data. The check used
to look for WORDS (`from`, `file`, `url`, …) and so refused harmless expressions too:
`EXTRACT(YEAR FROM date)`, `TRIM(BOTH ' ' FROM x)`, or a column named `url` (found on
2026-10-10: on ClickHouse, the engine of nearly every customer, one could not extract
the year from a date).

Now the expression is PARSED (sqlglot, in the engine's dialect) and what is really
dangerous is refused:
  · a subquery, a FROM, a JOIN, a reference to a table — so every table function
    (`s3()`, `file()`, `url()`, `remote()`, `mysql()`, …);
  · the SCALAR functions that read data outside the row: dictionaries (`dictGet…`),
    `joinGet`, the models (`catboostEvaluate`, `evalMLMethod`), the server's metadata;
  · several expressions in one (`;`).
When the expression cannot be read, the earlier careful rule (by words) holds: better a
refusal too many than a read unseen.
"""
from __future__ import annotations

import re

# the earlier check by words: it stays for what sqlglot cannot read
_FORBIDDEN_WORDS = re.compile(
    r"\b(?:select|from|with|insert|attach|create|"
    r"file|url|s3|hdfs|remote|remoteSecure|mysql|postgresql|jdbc|odbc|"
    r"clusterAllReplicas|cluster|dictionary|merge|numbers|zeros)\b",
    re.IGNORECASE,
)

# scalar functions that read data not in the row (or the server itself)
_FORBIDDEN_FUNCTIONS = re.compile(
    r"^(?:dictget\w*|dicthas|dictisin|dictgethierarchy|dictgetchildren|dictgetdescendants|"
    r"joinget\w*|catboostevaluate|evalmlmethod|modelevaluate|hascolumnintable|"
    r"getsetting|getmacro|getserverport|getenv|filesystem\w*|"
    r"file|url|s3|s3cluster|hdfs|remote|remotesecure|mysql|postgresql|jdbc|odbc|sqlite|mongodb|redis|"
    r"azureblobstorage|gcs|iceberg\w*|deltalake|hudi|input|executable|"
    r"clusterallreplicas|cluster|dictionary|merge|numbers|zeros|generaterandom|view|"
    r"read_\w+|parquet_scan|scan_\w+|query|query_table)$",
    re.IGNORECASE,
)


def _function_name(f) -> str:
    from sqlglot import exp

    if isinstance(f, exp.Anonymous):
        return str(f.this)
    return f.sql_name() if hasattr(f, "sql_name") else type(f).__name__


def forbidden_expression(expr: str, dialect: str = "clickhouse") -> bool:
    """True when the compute expression is NOT a plain scalar expression."""
    import sqlglot
    from sqlglot import exp

    if ";" in expr:
        return True
    try:
        trees = sqlglot.parse(f"SELECT ({expr}) AS _x", read=dialect)
    except Exception:  # noqa: BLE001 — unreadable: the careful rule, by words
        return bool(_FORBIDDEN_WORDS.search(expr))
    if len(trees) != 1 or trees[0] is None:
        return True
    tree = trees[0]
    if not isinstance(tree, exp.Select) or tree.args.get("from") or tree.args.get("joins"):
        return True
    for node in tree.walk():
        if node is tree:
            continue
        if isinstance(node, (exp.Select, exp.Subquery, exp.From, exp.Join, exp.Table, exp.Union, exp.Insert, exp.Create, exp.Command)):
            return True
        if isinstance(node, exp.Func) and _FORBIDDEN_FUNCTIONS.match(_function_name(node) or ""):
            return True
    return False
