"""SQL generato dalle operazioni BigQuery, SENZA BigQuery: un contesto finto
risponde alle introspezioni (colonne/schema) e ai conteggi. Si verifica il
dialetto (SAFE_CAST, QUALIFY, UNPIVOT, FARM_FINGERPRINT…), le protezioni di
compute/sql e la struttura di join/pivot. La correttezza dei RISULTATI e' nella
suite live (test_bigquery_live.py) contro un progetto vero.
"""
import pytest

from app.engine.bigquery_ops import _lit, _qi, get_bigquery_operation, unpivot_value_type
from app.engine.exceptions import EngineError


class FakeCtx:
    def __init__(self, schema, right_schema=None, counts=None, distinct=None):
        self.schema = schema  # [(nome, tipo)] della catena principale
        self.right_schema = right_schema or [("k", "INTEGER"), ("x", "STRING")]
        self.counts = counts or {}
        self.distinct = distinct or []
        self.queries: list[str] = []

    def schema_of(self, sql):
        return self.right_schema if "_right" in sql else self.schema

    def columns_of(self, sql):
        return [n for n, _ in self.schema_of(sql)]

    def scalar(self, sql):
        self.queries.append(sql)
        return self.counts.get("scalar", 0)

    def distinct_rows(self, base, cols):
        return self.distinct

    def build_right(self, ref):
        return "SELECT * FROM `_right`"

    # stessa mappa dei nomi del contesto vero (nomi gia' sicuri = identita')
    def qi(self, name):
        from app.engine.bigquery_engine import _sanitize

        return _qi(_sanitize(name))

    def map_quoted(self, expr):
        return expr


BASE = "SELECT * FROM `_src1`"
SCHEMA = [("id", "INTEGER"), ("nome", "STRING"), ("prezzo", "FLOAT"), ("quando", "TIMESTAMP"), ("ok", "BOOLEAN")]


def op(name, params, ctx=None, sql=BASE):
    return get_bigquery_operation(name)(sql, params, ctx or FakeCtx(SCHEMA))


def test_helpers():
    assert _qi("a`b") == "`a\\`b`"
    assert _lit(True) == "TRUE" and _lit(None) == "NULL" and _lit(3) == "3" and _lit("l'a") == "'l\\'a'"


def test_column_ops_use_except_replace():
    assert op("select", {"columns": ["id", "nome"]}) == "SELECT `id`, `nome` FROM (SELECT * FROM `_src1`)"
    assert op("drop", {"columns": ["ok"]}) == "SELECT * EXCEPT (`ok`) FROM (SELECT * FROM `_src1`)"
    assert op("reorder", {"columns": ["nome"]}).startswith("SELECT `nome`, * EXCEPT (`nome`) FROM")
    sql = op("rename", {"mapping": {"nome": "name"}})
    assert "`nome` AS `name`" in sql and "`prezzo`" in sql


def test_cast_is_safe_and_follows_the_standard():
    sql = op("cast", {"columns": {"nome": "int", "prezzo": "int", "prezzo": "str", "quando": "date", "id": "bool"}})
    assert "SAFE_CAST(TRIM(`nome`) AS INT64)" in sql
    assert "CONCAT(CAST(CAST(`prezzo` AS INT64) AS STRING), '.0')" in sql  # 200.0 → "200.0"
    assert "DATE(`quando`, 'UTC')" in sql
    assert "SAFE_CAST(SAFE_CAST(`id` AS INT64) AS BOOL)" in sql
    assert op("cast", {"columns": {"prezzo": "int"}}).count("SAFE_CAST(TRUNC(`prezzo`) AS INT64)") == 1
    with pytest.raises(EngineError):
        op("cast", {"columns": {"id": "uuid"}})


def test_filters():
    assert op("filter", {"column": "ok", "operator": "eq", "value": True}).endswith("WHERE `ok` = TRUE")
    assert "STRPOS(CAST(`nome` AS STRING), 'ab') > 0" in op("filter", {"column": "nome", "operator": "contains", "value": "ab"})
    assert "`id` IN (1, 2)" in op("filter", {"column": "id", "operator": "in", "value": [1, 2]})
    assert "`id` BETWEEN 1 AND 5" in op("filter", {"column": "id", "operator": "between", "value": [1, 5]})
    assert "`nome` IS NULL" in op("filter", {"column": "nome", "operator": "is_null"})


def test_rows_ops():
    assert op("sort", {"by": ["id"], "descending": True}).endswith("ORDER BY `id` DESC NULLS LAST")
    assert op("sort", {"by": ["manca"], "ignore_missing": True}) == BASE
    assert op("limit", {"n": 7}).endswith("LIMIT 7")
    s = op("sample", {"fraction": 0.1, "seed": 42})
    assert "FARM_FINGERPRINT" in s and "TO_JSON_STRING(_r)" in s and "MOD(" in s
    assert op("unique", {"subset": ["id"]}).endswith("QUALIFY ROW_NUMBER() OVER (PARTITION BY `id`) = 1")
    assert op("unique", {}).startswith("SELECT DISTINCT *")
    assert "COALESCE(`nome`, 'n/a') AS `nome`" in op("fill_null", {"columns": {"nome": "n/a"}})
    assert op("drop_nulls", {}).endswith("WHERE `id` IS NOT NULL AND `nome` IS NOT NULL AND `prezzo` IS NOT NULL AND `quando` IS NOT NULL AND `ok` IS NOT NULL")


def test_group_by_plain_and_with_exact_median():
    sql = op("group_by", {"by": ["nome"], "aggregations": [{"column": "prezzo", "func": "sum"}, {"column": "id", "func": "n_unique", "alias": "n"}]})
    assert sql == "SELECT `nome`, SUM(`prezzo`) AS `prezzo_sum`, COUNT(DISTINCT `id`) AS `n` FROM (SELECT * FROM `_src1`) GROUP BY `nome`"
    sql = op("group_by", {"by": ["nome"], "aggregations": [{"column": "prezzo", "func": "median"}, {"column": "id", "func": "count"}]})
    assert "PERCENTILE_CONT(`prezzo`, 0.5) OVER (PARTITION BY `nome`) AS `prezzo_median`" in sql
    assert "LEFT JOIN" in sql and "IS NOT DISTINCT FROM" in sql
    # ordine delle colonne = chiavi, poi le aggregazioni nell'ordine chiesto
    assert sql.startswith("SELECT a.`nome`, m.`prezzo_median`, a.`id_count` FROM")
    with pytest.raises(EngineError):
        op("group_by", {"by": ["nome"], "aggregations": [{"column": "prezzo", "func": "mode"}]})


def test_compute_and_sql_guards():
    sql = op("compute", {"columns": [{"name": "doppio", "expr": "prezzo * 2"}, {"name": "nome", "expr": "UPPER(nome)"}]})
    assert "(prezzo * 2) AS `doppio`" in sql and "* REPLACE ((UPPER(nome)) AS `nome`)" in sql
    for bad in ("(SELECT 1)", "EXTERNAL_QUERY('c', 'x')", "ML.PREDICT(x)"):
        with pytest.raises(EngineError):
            op("compute", {"columns": [{"name": "x", "expr": bad}]})
    ok = op("sql", {"query": "SELECT nome, COUNT(*) AS n FROM self GROUP BY nome"})
    assert ok.startswith("WITH input AS (SELECT * FROM `_src1`), self AS (SELECT * FROM input) SELECT nome")
    for bad in ("SELECT * FROM `proj.ds.tab`", "SELECT * FROM self; DROP TABLE x", "SELECT * FROM self -- c",
                "DELETE FROM self WHERE 1=1", "SELECT * FROM altra"):
        with pytest.raises(EngineError):
            op("sql", {"query": bad})


def test_joins():
    ctx = FakeCtx([("k", "INTEGER"), ("x", "STRING")])
    sql = op("join", {"right": {"bucket": "b", "key": "r.parquet"}, "on": ["k"], "how": "left"}, ctx)
    assert sql == "SELECT l.`k`, l.`x`, r.`x` AS `x_right` FROM (SELECT * FROM `_src1`) AS l LEFT JOIN (SELECT * FROM `_right`) AS r USING (`k`)"
    full = op("join", {"right": {"bucket": "b", "key": "r.parquet"}, "on": ["k"], "how": "full"}, ctx)
    assert full.startswith("SELECT COALESCE(l.`k`, r.`k`) AS `k`")
    semi = op("join", {"right": {"bucket": "b", "key": "r.parquet"}, "left_on": ["k"], "right_on": ["k"], "how": "anti"}, ctx)
    assert semi == "SELECT l.* FROM (SELECT * FROM `_src1`) AS l WHERE NOT EXISTS (SELECT 1 FROM (SELECT * FROM `_right`) AS r WHERE l.`k` = r.`k`)"
    big = FakeCtx([("k", "INTEGER")], counts={"scalar": 10**6})
    with pytest.raises(EngineError):
        op("join", {"right": {"bucket": "b", "key": "r.parquet"}, "how": "cross"}, big)


def test_union_relaxed_aligns_by_name():
    ctx = FakeCtx([("a", "INTEGER"), ("b", "STRING")], right_schema=[("b", "STRING"), ("c", "FLOAT")])
    sql = op("union", {"right": {"bucket": "b", "key": "r.parquet"}}, ctx)
    assert sql == ("SELECT `a` AS `a`, `b` AS `b`, NULL AS `c` FROM (SELECT * FROM `_src1`) "
                   "UNION ALL SELECT NULL AS `a`, `b` AS `b`, `c` AS `c` FROM (SELECT * FROM `_right`)")


def test_pivot_conditional_aggregation_sorted_labels():
    ctx = FakeCtx(SCHEMA, counts={"scalar": 3}, distinct=[["b"], [None], ["a"]])
    sql = op("pivot", {"index": ["id"], "on": ["nome"], "values": "prezzo", "func": "count"}, ctx)
    assert "COUNT(IF(`nome` = 'a', `prezzo`, NULL)) AS `a`" in sql
    assert "COUNT(IF(`nome` IS NULL, `prezzo`, NULL)) AS `null`" in sql
    assert sql.index("AS `a`") < sql.index("AS `b`") < sql.index("AS `null`")
    assert sql.endswith("GROUP BY `id`")
    with pytest.raises(EngineError):
        op("pivot", {"index": ["id"], "on": ["nome"], "values": "prezzo", "func": "median"}, ctx)


def test_unpivot_native_with_supertype():
    assert unpivot_value_type(["INTEGER", "INT64"]) == "INT64"
    assert unpivot_value_type(["INTEGER", "FLOAT"]) == "FLOAT64"
    assert unpivot_value_type(["STRING", "FLOAT"]) == "STRING"
    sql = op("unpivot", {"index": ["id"], "on": ["prezzo", "nome"]})
    assert "UNPIVOT INCLUDE NULLS (`value` FOR `variable` IN (`prezzo`, `nome`))" in sql
    assert "CONCAT(CAST(CAST(`prezzo` AS INT64) AS STRING), '.0')" in sql  # float → testo con .0
    assert sql.startswith("SELECT `id`, `variable`, `value` FROM (SELECT `id`, ")
    with pytest.raises(EngineError):
        op("unpivot", {"index": ["id"], "on": ["manca"]})


def test_unknown_operation():
    with pytest.raises(EngineError):
        get_bigquery_operation("foreach")


def test_unsafe_names_are_mapped_and_restored():
    from app.engine.bigquery_engine import BigQueryContext, _sanitize

    assert _sanitize("Ragione Sociale") == "Ragione_Sociale"
    assert _sanitize("Importo (€)") == "Importo____"
    assert _sanitize("1anno") == "_1anno" and _sanitize("") == "_col"
    ctx = BigQueryContext.__new__(BigQueryContext)
    ctx._safe, ctx._orig = {}, {}
    assert ctx.qi("Ragione Sociale") == "`Ragione_Sociale`"
    # due originali che collidono → il secondo prende un suffisso
    assert ctx.qi("a b") == "`a_b`" and ctx.qi("a-b") == "`a_b_2`"
    assert ctx.original("a_b_2") == "a-b" and ctx.original("altro") == "altro"
    # espressioni libere: solo gli identificatori quotati NOTI vengono tradotti
    assert ctx.map_quoted("`a b` * 2 + `ignoto`") == "`a_b` * 2 + `ignoto`"
    sql = op("rename", {"mapping": {"nome": "Ragione Sociale"}}, FakeCtx(SCHEMA))
    assert "`nome` AS `Ragione_Sociale`" in sql


def test_compute_expressions_are_transpiled_to_googlesql():
    from app.engine.bigquery_ops import to_bigquery_expr

    assert to_bigquery_expr("qta % 3") == "MOD(qta, 3)"
    assert to_bigquery_expr("strftime(data, '%Y')") == "FORMAT_DATE('%Y', data)"
    assert to_bigquery_expr("CASE WHEN a > 1 THEN 'x' ELSE 'y' END") == "CASE WHEN a > 1 THEN 'x' ELSE 'y' END"
    assert to_bigquery_expr("SAFE_CAST(x AS INT64)") == "SAFE_CAST(x AS INT64)"  # gia' GoogleSQL: invariato
    assert to_bigquery_expr("questo non e' sql ((") == "questo non e' sql (("  # ripiego sul testo
    sql = op("compute", {"columns": [{"name": "resto", "expr": "prezzo % 2"}]})
    assert "(MOD(prezzo, 2)) AS `resto`" in sql
