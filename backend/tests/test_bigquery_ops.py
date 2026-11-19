"""The SQL the BigQuery operations generate, WITHOUT BigQuery: a fake context answers the
introspections (columns/schema) and the counts. Checks the dialect (SAFE_CAST, QUALIFY,
UNPIVOT, FARM_FINGERPRINT…), the protections of compute/sql and the structure of
join/pivot. The RESULTS' correctness is in the live suite (test_bigquery_live.py) against
a real project.
"""
import pytest

from app.engine.bigquery_ops import _lit, _qi, get_bigquery_operation, unpivot_value_type
from app.engine.exceptions import EngineError


class FakeCtx:
    def __init__(self, schema, right_schema=None, counts=None, distinct=None):
        self.schema = schema  # [(name, type)] of the main chain
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

    # the same name map as the real context (names safe already = identity)
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
    # text to yes/no takes the same words as the other engines («1», «yes», «n»…), not true/false only
    sql = op("cast", {"columns": {"nome": "bool"}})
    assert "CASE WHEN LOWER(TRIM(`nome`)) IN ('true', 't', '1', 'yes', 'y') THEN TRUE" in sql
    assert "WHEN LOWER(TRIM(`nome`)) IN ('false', 'f', '0', 'no', 'n') THEN FALSE END" in sql
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
    # the columns' order = the keys, then the aggregations in the order asked
    assert sql.startswith("SELECT a.`nome`, m.`prezzo_median`, a.`id_count` FROM")
    with pytest.raises(EngineError):
        op("group_by", {"by": ["nome"], "aggregations": [{"column": "prezzo", "func": "mode"}]})


def test_compute_and_sql_guards():
    sql = op("compute", {"columns": [{"name": "doppio", "expr": "prezzo * 2"}, {"name": "nome", "expr": "UPPER(nome)"}]})
    assert "(prezzo * 2) AS `doppio`" in sql and "* REPLACE ((UPPER(nome)) AS `nome`)" in sql
    for bad in ("(SELECT 1)", "EXTERNAL_QUERY('c', 'x')", "ML.PREDICT(x)", "AI.GENERATE_BOOL(nome)", "SESSION_USER()",
                "x IN (SELECT a FROM t)", "read_parquet('gs://b/x.parquet')"):
        with pytest.raises(EngineError):
            op("compute", {"columns": [{"name": "x", "expr": bad}]})
    # FROM inside a scalar function is not a subquery (4 of the 11 Sample flows use it)
    for good in ("EXTRACT(YEAR FROM data) * 100 + EXTRACT(MONTH FROM data)", "TRIM(BOTH ' ' FROM nome)"):
        assert "AS `x`" in op("compute", {"columns": [{"name": "x", "expr": good}]})
    ok = op("sql", {"query": "SELECT nome, COUNT(*) AS n FROM self GROUP BY nome"})
    assert ok.startswith("WITH input AS (SELECT * FROM `_src1`), self AS (SELECT * FROM input) SELECT nome")
    for bad in ("SELECT * FROM `proj.ds.tab`", "SELECT * FROM self; DROP TABLE x", "SELECT * FROM self -- c",
                "DELETE FROM self WHERE 1=1", "SELECT * FROM altra", "SELECT `a` FROM self JOIN `ds.t` ON TRUE",
                "SELECT * FROM self, `my-proj`.`ds.tab`"):
        with pytest.raises(EngineError):
            op("sql", {"query": bad})
    # a dot BETWEEN two quoted names is no table path (a number, a decimal): found against a
    # real project, where the column profile's histogram was refused
    for good in ("SELECT `a` * 1.5 AS `b` FROM self",
                 "SELECT LEAST(9, GREATEST(0, CAST(floor((CAST(`amount` AS FLOAT64) - (-700.0)) / 450.0) AS INT64))) AS b, "
                 "count(*) AS n FROM self WHERE CAST(`amount` AS FLOAT64) BETWEEN -700.0 AND 3800.0 GROUP BY b"):
        assert op("sql", {"query": good}).endswith(good)


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


def test_pivot_rows_only_is_a_group_by_with_one_values_column():
    sql = op("pivot", {"index": ["id"], "values": "prezzo", "func": "sum"})
    assert sql == "SELECT `id`, SUM(`prezzo`) AS `values` FROM (SELECT * FROM `_src1`) GROUP BY `id`"
    assert "COUNT(DISTINCT `nome`) AS `values`" in op("pivot", {"index": ["id"], "on": [], "values": "nome", "func": "n_unique"})
    with pytest.raises(EngineError, match="a row key already has that name"):
        op("pivot", {"index": ["values"], "values": "prezzo"})


def test_pivot_columns_only_is_one_row_led_by_the_measure_label():
    ctx = FakeCtx(SCHEMA, counts={"scalar": 2}, distinct=[["b"], ["a"]])
    sql = op("pivot", {"on": ["nome"], "values": "prezzo", "func": "sum"}, ctx)
    assert sql.startswith("SELECT 'sum(prezzo)' AS `values`, SUM(IF(`nome` = 'a', `prezzo`, NULL)) AS `a`, ")
    assert "GROUP BY" not in sql
    assert op("pivot", {"on": ["nome"], "values": "prezzo"}, FakeCtx(SCHEMA)) == "SELECT 'sum(prezzo)' AS `values`"
    with pytest.raises(EngineError, match="a value of the spread columns is 'values'"):
        op("pivot", {"on": ["nome"], "values": "prezzo"}, FakeCtx(SCHEMA, counts={"scalar": 1}, distinct=[["values"]]))
    with pytest.raises(EngineError, match="choose the row keys"):
        op("pivot", {"index": [], "on": [], "values": "prezzo"})


def test_unpivot_native_with_supertype():
    assert unpivot_value_type(["INTEGER", "INT64"]) == "INT64"
    assert unpivot_value_type(["INTEGER", "FLOAT"]) == "FLOAT64"
    assert unpivot_value_type(["STRING", "FLOAT"]) == "STRING"
    sql = op("unpivot", {"index": ["id"], "on": ["prezzo", "nome"]})
    assert "UNPIVOT INCLUDE NULLS (`value` FOR `variable` IN (`prezzo`, `nome`))" in sql
    assert "CONCAT(CAST(CAST(`prezzo` AS INT64) AS STRING), '.0')" in sql  # float → text with .0
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
    # two originals that collide → the second takes a suffix
    assert ctx.qi("a b") == "`a_b`" and ctx.qi("a-b") == "`a_b_2`"
    assert ctx.original("a_b_2") == "a-b" and ctx.original("altro") == "altro"
    # free expressions: only the KNOWN quoted identifiers are translated
    assert ctx.map_quoted("`a b` * 2 + `ignoto`") == "`a_b` * 2 + `ignoto`"
    sql = op("rename", {"mapping": {"nome": "Ragione Sociale"}}, FakeCtx(SCHEMA))
    assert "`nome` AS `Ragione_Sociale`" in sql


def test_compute_expressions_are_transpiled_to_googlesql():
    from app.engine.bigquery_ops import to_bigquery_expr

    assert to_bigquery_expr("qta % 3") == "MOD(qta, 3)"
    assert to_bigquery_expr("strftime(data, '%Y')") == "FORMAT_DATE('%Y', data)"
    assert to_bigquery_expr("CASE WHEN a > 1 THEN 'x' ELSE 'y' END") == "CASE WHEN a > 1 THEN 'x' ELSE 'y' END"
    assert to_bigquery_expr("SAFE_CAST(x AS INT64)") == "SAFE_CAST(x AS INT64)"  # gia' GoogleSQL: invariato
    assert to_bigquery_expr("questo non e' sql ((") == "questo non e' sql (("  # falls back on the text
    sql = op("compute", {"columns": [{"name": "resto", "expr": "prezzo % 2"}]})
    assert "(MOD(prezzo, 2)) AS `resto`" in sql


def test_statistics_run_their_aggregates_in_bigquery_and_return_typed_literals():
    """A hypothesis test asks BigQuery for its aggregates (`ctx.rows`: here answered with what
    R's samples give) and returns the results as an UNNEST of typed STRUCTs. The queries are
    GoogleSQL a parser reads; that BigQuery answers them is the live suite's to say."""
    import sqlglot

    class StatCtx(FakeCtx):
        def rows(self, sql):
            self.queries.append(sql)
            if "LIMIT 202" in sql:  # the groups of `nome`, the one without a name too
                return [["a", 9], ["b", 8], [None, 1]]
            if "LIMIT 100000" in sql:  # the centre: the mean of the first rows
                return [[6.5]]
            return [["a", 9, 8, 5.6875 - 6.5, 0.364107142857143, 4.9, 6.7],
                    [None, 1, 1, 3.4, None, 9.9, 9.9],
                    ["b", 8, 7, 7.128571428571429 - 6.5, 0.365714285714286, 6.4, 8.0]]

    ctx = StatCtx(SCHEMA)
    sql = op("stat_t_two_sample", {"value": "prezzo", "group": "nome"}, ctx)
    levels, centre, moments = ctx.queries
    x = "CASE WHEN `prezzo` IS NOT NULL AND NOT IS_NAN(CAST(`prezzo` AS FLOAT64)) THEN CAST(`prezzo` AS FLOAT64) END"
    finite = ("CASE WHEN `prezzo` IS NOT NULL AND (NOT IS_NAN(CAST(`prezzo` AS FLOAT64)) AND NOT IS_INF(CAST(`prezzo` AS FLOAT64))) "
              "THEN CAST(`prezzo` AS FLOAT64) END")
    assert levels == "SELECT `nome` AS st_lv, count(*) AS st_rows FROM (SELECT * FROM `_src1`) AS st_t GROUP BY `nome` LIMIT 202"
    assert centre == f"SELECT avg({finite}) AS st_centre FROM (SELECT `prezzo` FROM (SELECT * FROM `_src1`) AS st_t LIMIT 100000) AS st_head"
    assert moments == (
        f"SELECT `nome` AS st_lv, count(*) AS st_rows, count({x}) AS st_n, avg(({finite} - (6.5))) AS st_mean, "
        f"VAR_SAMP(({finite} - (6.5))) AS st_var, min({x}) AS st_min, max({x}) AS st_max "
        "FROM (SELECT * FROM `_src1`) AS st_t GROUP BY `nome`"
    )
    struct = "STRUCT<section STRING, level STRING, level_2 STRING, stat STRING, value FLOAT64, text STRING, x0 FLOAT64, x1 FLOAT64, ord INT64, key STRING>"
    assert sql.startswith(f"SELECT * FROM UNNEST([{struct}('test', NULL, NULL, 'test', NULL, 't_two_sample', NULL, NULL, 0, 'test|||test'), {struct}(")
    assert f"{struct}('test', NULL, NULL, 'engine', NULL, 'bigquery', NULL, NULL, 8, 'test|||engine')" in sql
    assert f"{struct}('test', NULL, NULL, 'n_missing', 3.0, NULL, NULL, NULL, 12, 'test|||n_missing')" in sql
    assert f"{struct}('group', 'b', NULL, 'max', 8.0, NULL, NULL, NULL, 43, 'group|b||max')])" in sql
    assert "'test|||statistic'" in sql and "-4.60902122168" in sql
    for query in (levels, centre, moments, sql):
        sqlglot.parse_one(query, read="bigquery")  # raises on what is not GoogleSQL
    # an output never takes the name of a column of the input, whatever its case or its safe form
    ctx = StatCtx([("ST_LV", "STRING"), ("st rows", "INTEGER"), ("prezzo", "NUMERIC"), ("nome", "STRING")])
    op("stat_t_two_sample", {"value": "prezzo", "group": "nome"}, ctx)
    assert ctx.queries[0].startswith("SELECT `nome` AS st_lv_, count(*) AS st_rows_ FROM")
    # the types that are numbers, as a BigQuery job's schema names them
    with pytest.raises(EngineError, match="the column «quando» \\('value'\\) is not a number \\(TIMESTAMP\\)"):
        op("stat_t_two_sample", {"value": "quando", "group": "nome"}, StatCtx(SCHEMA))
    with pytest.raises(EngineError, match="not a number \\(BOOLEAN\\)"):
        op("stat_t_two_sample", {"value": "ok", "group": "nome"}, StatCtx(SCHEMA))
    assert op("stat_t_two_sample", {"value": "id", "group": "nome"}, StatCtx(SCHEMA)).startswith("SELECT * FROM UNNEST(")


def test_statistics_report_rows_ask_bigquery_for_quartiles_and_a_histogram():
    """`detail = report`: the quartiles through a window function (BigQuery has no exact
    quantile aggregate), the histogram as a count per group and interval. GoogleSQL a parser
    reads, written from the documentation; that BigQuery answers it is the live suite's to say."""
    import sqlglot

    class StatCtx(FakeCtx):
        def rows(self, sql):
            self.queries.append(sql)
            if "LIMIT 202" in sql:
                return [["a", 9], ["b", 8], [None, 1]]
            if "LIMIT 100000" in sql:
                return [[6.5]]
            if "PERCENTILE_CONT" in sql:
                return [["b", 6.7, 7.1, 7.5], [None, 9.9, 9.9, 9.9], ["a", 5.25, 5.65, 6.05]]
            if "LEAST(" in sql:  # an interval with no rows is not answered
                return [["a", 0, 4], ["a", 1, 2], ["a", 2, 2], ["b", 2, 2], ["b", 3, 3], ["b", 4, 2], [None, 4, 1]]
            return [["a", 9, 8, 5.6875 - 6.5, 0.364107142857143, 4.9, 6.7],
                    [None, 1, 1, 3.4, None, 9.9, 9.9],
                    ["b", 8, 7, 7.128571428571429 - 6.5, 0.365714285714286, 6.4, 8.0]]

    ctx = StatCtx(SCHEMA)
    sql = op("stat_t_two_sample", {"value": "prezzo", "group": "nome", "detail": "report"}, ctx)
    _, _, _, quartiles, histogram = ctx.queries
    finite = ("CASE WHEN `prezzo` IS NOT NULL AND (NOT IS_NAN(CAST(`prezzo` AS FLOAT64)) AND NOT IS_INF(CAST(`prezzo` AS FLOAT64))) "
              "THEN CAST(`prezzo` AS FLOAT64) END")
    assert quartiles == (
        f"SELECT DISTINCT `nome` AS st_lv, PERCENTILE_CONT({finite}, 0.25) OVER (PARTITION BY `nome`) AS st_q1, "
        f"PERCENTILE_CONT({finite}, 0.5) OVER (PARTITION BY `nome`) AS st_median, "
        f"PERCENTILE_CONT({finite}, 0.75) OVER (PARTITION BY `nome`) AS st_q3 FROM (SELECT * FROM `_src1`) AS st_t "
        # the two groups compared, as A1 gave them: the values of any other group are never partitioned
        "WHERE `nome` = 'a' OR `nome` = 'b'"
    )
    width = repr((8.0 - 4.9) / 5)
    b = f"LEAST(4, GREATEST(0, CAST(floor((CAST(`prezzo` AS FLOAT64) - (4.9)) / {width}) AS INT64)))"
    assert histogram == (
        f"SELECT `nome` AS st_lv, {b} AS st_bin, count(*) AS st_rows FROM (SELECT * FROM `_src1`) AS st_t "
        f"WHERE CAST(`prezzo` AS FLOAT64) BETWEEN 4.9 AND 8.0 GROUP BY `nome`, {b}"
    )
    for query in (quartiles, histogram, sql):
        sqlglot.parse_one(query, read="bigquery")
    assert "'group', 'a', NULL, 'median', 5.65, NULL, NULL, NULL, 36, 'group|a||median')" in sql
    assert "('hist', 'a', NULL, 'count', 4.0, '[4.9, 5.52)', 4.9, " in sql and "'hist|a||count|0')" in sql
    assert "'[7.38, 8]', " in sql and ", 8.0, 59, 'hist|b||count|4')" in sql
    assert sql.count("('curve', NULL, NULL, 'density', ") == 201 and sql.endswith("'curve|||reject_high|200')])")
    # without a group column the quantiles are of every row: one window, one row kept
    from app.engine.stats.aggregates import SqlStatSource

    asked: list[str] = []
    source = SqlStatSource("bigquery", lambda q: asked.append(q) or [[1.0, 2.0, 3.0]], lambda c: f"`{c}`", repr,
                           "SELECT 1 AS prezzo", [("prezzo", "FLOAT64")])
    assert source.quartiles("prezzo") == [(None, 1.0, 2.0, 3.0)]
    assert asked[0].startswith(f"SELECT DISTINCT PERCENTILE_CONT({finite}, 0.25) OVER () AS st_q1, ")
    sqlglot.parse_one(asked[0], read="bigquery")
