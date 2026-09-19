"""Il nodo `sql` legge solo il proprio input (lista bianca, vedi engine/sql_guard).

Le query «ostili» qui sotto non sono ipotesi: `merge()` e `information_schema`
hanno davvero letto altre tabelle del ClickHouse esterno il 2026-09-19, passando
la vecchia lista nera. Le query «lecite» sono la garanzia opposta: l'SQL analitico
che un utente scrive davvero deve continuare a passare.
"""
import pytest

from app.engine.exceptions import EngineError
from app.engine.sql_guard import ensure_reads_only_input

LECITE_CLICKHOUSE = [
    "SELECT paese, count() AS n FROM self GROUP BY paese ORDER BY n DESC",
    "SELECT * FROM input WHERE importo > 10",
    # FROM dentro una funzione non è una tabella
    "SELECT EXTRACT(YEAR FROM d) AS y, trim(BOTH ' ' FROM x) AS t FROM self",
    "SELECT * FROM (WITH t AS (SELECT a FROM self) SELECT * FROM t) AS q JOIN input USING (a)",
    "SELECT a, b FROM self ARRAY JOIN arr AS b",
    "SELECT arrayMap(x -> x * 2, arr) AS z, toStartOfMonth(d) AS m FROM self LIMIT 2 BY a",
    "SELECT a, row_number() OVER (PARTITION BY a ORDER BY b) AS r FROM self QUALIFY r = 1",
    "SELECT * FROM self WHERE a IN (SELECT a FROM self GROUP BY a HAVING count() > 1)",
    "SELECT s.a, n.number FROM self AS s CROSS JOIN numbers(3) AS n",
    "SELECT a FROM self UNION ALL SELECT a FROM input",
    # una colonna può chiamarsi come le parole che fanno paura
    'SELECT "system", "merge", file FROM self',
    # l'ARRAY JOIN vero e proprio — quello sugli array dell'input — resta lecito
    "SELECT a, b FROM self ARRAY JOIN arr AS b",
    "SELECT a, b FROM self LEFT ARRAY JOIN arr AS b",
    "SELECT a, x FROM self ARRAY JOIN (SELECT groupArray(a) FROM self) AS x",
]

OSTILI_CLICKHOUSE = [
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
    # una CTE definita DENTRO una sottoquery non autorizza la tabella omonima fuori
    "SELECT * FROM (WITH mv_abc AS (SELECT 1 AS a) SELECT * FROM mv_abc) AS s, self, mv_abc",
    # ARRAY JOIN: sqlglot analizza il riferimento puntato come COLONNA e non come
    # tabella, quindi la lista bianca lo mancava. Audit 2026-09-19, A9: eseguito
    # davvero su un ClickHouse, leggeva information_schema e system.settings.
    "SELECT * FROM self ARRAY JOIN (SELECT groupArray(table_name) FROM information_schema.tables) AS leak",
    "SELECT * FROM self ARRAY JOIN (SELECT groupArray(c) FROM altro_db.tabella_altrui) AS leak",
    "SELECT * FROM self LEFT ARRAY JOIN (SELECT groupArray(query) FROM system.query_log) AS leak",
    "SELECT * FROM self ARRAY JOIN (SELECT groupArray(v) FROM `tabularia`.`mv_abc`) AS leak",
]


@pytest.mark.parametrize("sql", LECITE_CLICKHOUSE)
def test_analytical_sql_on_the_input_passes(sql):
    ensure_reads_only_input(sql, "clickhouse")


@pytest.mark.parametrize("sql", OSTILI_CLICKHOUSE)
def test_anything_that_names_another_table_is_refused(sql):
    with pytest.raises(EngineError) as e:
        ensure_reads_only_input(sql, "clickhouse")
    assert "solo l'input" in str(e.value)


def test_unparseable_sql_is_refused_not_waved_through():
    with pytest.raises(EngineError) as e:
        ensure_reads_only_input("SELECT * FROM self WHERE ((( ", "clickhouse")
    assert "non analizzabile" in str(e.value)


@pytest.mark.parametrize("sql", [
    "SELECT `Ragione Sociale`, COUNT(*) AS n FROM self GROUP BY 1",
    "SELECT x FROM self, UNNEST(GENERATE_ARRAY(1, 3)) AS x",
    "SELECT * FROM self QUALIFY ROW_NUMBER() OVER (PARTITION BY a ORDER BY b) = 1",
])
def test_bigquery_analytics_pass(sql):
    ensure_reads_only_input(sql, "bigquery")


@pytest.mark.parametrize("sql", [
    # senza backtick la vecchia regex non lo vedeva: è la step-cache degli altri
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
