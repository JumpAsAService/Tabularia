"""The `sql` node reads only its own input (an allowlist, see engine/sql_guard).

The «hostile» queries below are not hypotheses: `merge()` and `information_schema` really
read other tables of the external ClickHouse on 2026-09-19, getting past the old
denylist. The «lawful» queries are the opposite guarantee: the analytical SQL a user
really writes must keep passing.
"""
import pytest

from app.engine.exceptions import EngineError
from app.engine.sql_guard import ensure_reads_only_input

LAWFUL_CLICKHOUSE = [
    "SELECT paese, count() AS n FROM self GROUP BY paese ORDER BY n DESC",
    "SELECT * FROM input WHERE importo > 10",
    # FROM inside a function is not a table
    "SELECT EXTRACT(YEAR FROM d) AS y, trim(BOTH ' ' FROM x) AS t FROM self",
    "SELECT * FROM (WITH t AS (SELECT a FROM self) SELECT * FROM t) AS q JOIN input USING (a)",
    "SELECT a, b FROM self ARRAY JOIN arr AS b",
    "SELECT arrayMap(x -> x * 2, arr) AS z, toStartOfMonth(d) AS m FROM self LIMIT 2 BY a",
    "SELECT a, row_number() OVER (PARTITION BY a ORDER BY b) AS r FROM self QUALIFY r = 1",
    "SELECT * FROM self WHERE a IN (SELECT a FROM self GROUP BY a HAVING count() > 1)",
    "SELECT s.a, n.number FROM self AS s CROSS JOIN numbers(3) AS n",
    "SELECT a FROM self UNION ALL SELECT a FROM input",
    # a column may be named as the scary words
    'SELECT "system", "merge", file FROM self',
    # the ARRAY JOIN proper — over the input's arrays — stays lawful
    "SELECT a, b FROM self ARRAY JOIN arr AS b",
    "SELECT a, b FROM self LEFT ARRAY JOIN arr AS b",
    "SELECT a, x FROM self ARRAY JOIN (SELECT groupArray(a) FROM self) AS x",
]

HOSTILE_CLICKHOUSE = [
    "SELECT count() FROM self WHERE 1=0 UNION ALL SELECT count() FROM merge('system', '^query_log$')",
    "SELECT t.table_name FROM self CROSS JOIN information_schema.tables AS t",
    "SELECT t.table_name FROM self CROSS JOIN INFORMATION_SCHEMA.TABLES AS t",
    "SELECT * FROM self, `tabularia`.`mv_abc`",
    "SELECT * FROM self, altra_tabella",
    "SELECT (SELECT max(query) FROM altra) AS q FROM self",
    "SELECT * FROM self WHERE a IN (SELECT name FROM altra)",
    "SELECT * FROM self WHERE a IN altra",
    "SELECT * FROM self UNION ALL SELECT * FROM view(SELECT * FROM altra)",
    "SELECT * FROM self UNION ALL SELECT * FROM icebergS3('https://x/y')",
    "SELECT * FROM self UNION ALL SELECT * FROM azureBlobStorageCluster('c', 'x')",
    "SELECT joinGet('db.t', 'v', a) FROM self",
    "SELECT dictGetString('d', 'attr', toUInt64(a)) FROM self",
    "SELECT getSetting('s3_max_connections') FROM self",
    # a CTE defined INSIDE a subquery does not authorize the table of the same name outside
    "SELECT * FROM (WITH mv_abc AS (SELECT 1 AS a) SELECT * FROM mv_abc) AS s, self, mv_abc",
    # ARRAY JOIN: sqlglot parses the dotted reference as a COLUMN and not as a table, so
    # the allowlist missed it. Audit 2026-09-19, A9: really run on a ClickHouse, it read
    # information_schema and system.settings.
    "SELECT * FROM self ARRAY JOIN (SELECT groupArray(table_name) FROM information_schema.tables) AS leak",
    "SELECT * FROM self ARRAY JOIN (SELECT groupArray(c) FROM altro_db.tabella_altrui) AS leak",
    "SELECT * FROM self LEFT ARRAY JOIN (SELECT groupArray(query) FROM system.query_log) AS leak",
    "SELECT * FROM self ARRAY JOIN (SELECT groupArray(v) FROM `tabularia`.`mv_abc`) AS leak",
]


@pytest.mark.parametrize("sql", LAWFUL_CLICKHOUSE)
def test_analytical_sql_on_the_input_passes(sql):
    ensure_reads_only_input(sql, "clickhouse")


@pytest.mark.parametrize("sql", HOSTILE_CLICKHOUSE)
def test_anything_that_names_another_table_is_refused(sql):
    with pytest.raises(EngineError) as e:
        ensure_reads_only_input(sql, "clickhouse")
    assert "only the input" in str(e.value)


def test_unparseable_sql_is_refused_not_waved_through():
    with pytest.raises(EngineError) as e:
        ensure_reads_only_input("SELECT * FROM self WHERE ((( ", "clickhouse")
    assert "could not be parsed" in str(e.value)


@pytest.mark.parametrize("sql", [
    "SELECT `Ragione Sociale`, COUNT(*) AS n FROM self GROUP BY 1",
    "SELECT x FROM self, UNNEST(GENERATE_ARRAY(1, 3)) AS x",
    "SELECT * FROM self QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1",
])
def test_bigquery_analytics_pass(sql):
    ensure_reads_only_input(sql, "bigquery")


@pytest.mark.parametrize("sql", [
    # without backticks the old regex did not see it: it is the others' step cache
    "SELECT * FROM self UNION ALL SELECT * FROM tabularia_cache.sc_0123",
    "SELECT * FROM self UNION ALL SELECT * FROM progetto.dataset.tabella",
    "SELECT * FROM self, altra",
])
def test_bigquery_cannot_name_project_tables(sql):
    with pytest.raises(EngineError):
        ensure_reads_only_input(sql, "bigquery")


def test_the_clickhouse_sql_node_applies_the_guard():
    from app.engine.chdb_ops import get_chdb_operation

    op = get_chdb_operation("sql")
    assert "FROM self" in op("SELECT 1 AS a", {"query": "SELECT a FROM self"}, None)
    with pytest.raises(EngineError):
        op("SELECT 1 AS a", {"query": "SELECT a FROM self UNION ALL SELECT 1 FROM merge('default', '.*')"}, None)


# ── the tables a SQL script on the catalog brings ─────────────────────────────
def test_the_tables_of_the_operation_are_readable_and_nothing_else():
    ensure_reads_only_input("SELECT o.a, c.b FROM ds_1 AS o JOIN ds_2 AS c ON o.a = c.a", "clickhouse", {"ds_1", "ds_2"})
    ensure_reads_only_input("SELECT a FROM ds_1 WHERE a IN (SELECT a FROM ds_2)", "bigquery", {"ds_1", "ds_2"})
    for query in ("SELECT * FROM ds_3", "SELECT * FROM ds_1 JOIN system.tables USING (a)",
                  "SELECT * FROM db.ds_1", "SELECT * FROM merge('system', '^query_log$')"):
        with pytest.raises(EngineError):
            ensure_reads_only_input(query, "clickhouse", {"ds_1", "ds_2"})


def test_table_names_are_checked():
    from app.engine.sql_guard import MAX_TABLES, table_names

    assert table_names({"tables": {"ds_12": {"source": {}}}}) == {"ds_12": {"source": {}}}
    assert table_names({}) == {}
    for bad in ({"self": {}}, {"DS_1": {}}, {"ds-1": {}}, {"ds_1 ; drop": {}}, {"ds_1": "x"}, "nope",
                {f"t{i}": {} for i in range(MAX_TABLES + 1)}):
        with pytest.raises(EngineError):
            table_names({"tables": bad})
