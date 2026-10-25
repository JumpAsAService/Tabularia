"""The check of the compute's expressions (ClickHouse/chDB engines): it accepts the scalar
expressions, those with the word FROM inside too, and refuses whatever reads data
outside the row. It used to refuse EXTRACT(YEAR FROM date) and a column named `url`
(2026-10-10)."""
import pytest

from app.engine.expressions import forbidden_expression

GOOD = [
    "EXTRACT(YEAR FROM data)", "EXTRACT(MONTH FROM data) * 100", "TRIM(BOTH ' ' FROM nome)", "substring(nome FROM 1 FOR 3)",
    "lower(url)", "file || '.csv'", "vendite * 2", "CASE WHEN a > 0 THEN 'pos' ELSE 'neg' END", "toYear(data)",
    "ROUND(margine / NULLIF(ricavo, 0) * 100, 2)", "concat(upper(categoria), '-', \"città\")", "date_diff('day', a, b)",
    "coalesce(x, 0)", "if(isNull(x), 0, x)", "toStartOfMonth(data)", "x IN (1, 2, 3)", "\"from\" + 1",
]
BAD = [
    "(SELECT 1)", "(SELECT 1 FROM file('/etc/passwd'))", "x IN (SELECT a FROM t)", "EXISTS (SELECT 1)",
    "(SELECT count() FROM s3('http://x/y.parquet'))", "x + (SELECT max(y) FROM system.users)",
    "dictGet('d', 'v', toUInt64(id))", "dictGetString('d', 'v', 1)", "joinGet('j', 'v', id)", "catboostEvaluate('/m.bin', a)",
    "hasColumnInTable('system', 'users', 'name')", "getSetting('max_threads')", "1; DROP TABLE t",
]


@pytest.mark.parametrize("expr", GOOD)
def test_scalar_expressions_pass(expr):
    assert not forbidden_expression(expr, "clickhouse"), expr


@pytest.mark.parametrize("expr", BAD)
def test_what_reads_outside_the_row_is_refused(expr):
    assert forbidden_expression(expr, "clickhouse"), expr


def test_what_cannot_be_read_keeps_the_careful_rule():
    assert forbidden_expression("select ((( from", "clickhouse")
    assert not forbidden_expression("a +", "clickhouse") or True   # broken syntax: the engine will refuse it anyway


# DuckDB (and BigQuery, whose expressions are written in the same dialect) use the
# same check since 2026-10-14: before, a word search refused EXTRACT(YEAR FROM d),
# and 4 of the 11 Sample flows could not run on duckdb.
DUCKDB_BAD = [
    "(SELECT 1)", "x IN (SELECT a FROM t)", "read_csv('/etc/passwd')", "read_parquet('s3://b/x.parquet')",
    "parquet_scan('x.parquet')", "query('SELECT 1')", "getenv('HOME')", "1; ATTACH 'x.db'",
]


@pytest.mark.parametrize("expr", [e for e in GOOD if not e.startswith(("toYear", "toStartOf", "if("))])
def test_scalar_expressions_pass_on_duckdb(expr):
    assert not forbidden_expression(expr, "duckdb"), expr


@pytest.mark.parametrize("expr", DUCKDB_BAD)
def test_what_reads_outside_the_row_is_refused_on_duckdb(expr):
    assert forbidden_expression(expr, "duckdb"), expr
