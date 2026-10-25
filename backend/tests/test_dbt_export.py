"""The engine's dbt exporter: compiling the IR into models (one CTE per operation, the
schema tracked), the translation into the native dialect with its known gaps, the
singular tests from the contracts and the assembled project (schema.yml, sources with
descriptions, seeds, the schema macro, README).

The native SQL runs on real servers, which the regression run must provide: a Postgres
(`POSTGRES_TEST_URI`, only SELECTs over VALUES; its default collation must not be "C", or
the byte-order tests prove nothing) and a ClickHouse (`CLICKHOUSE_TEST_*`, see
test_clickhouse_engine.py). E.g. on the dev stack's network:
    docker run -d --rm --name pgtest --network dataprep-network -e POSTGRES_USER=tab \
      -e POSTGRES_PASSWORD=tab -e POSTGRES_DB=tab postgres:18
    POSTGRES_TEST_URI=postgresql://tab:tab@pgtest:5432/tab
Without them those tests are skipped; with `TABULARIA_TEST_REQUIRE=postgres,clickhouse`
(what the regression run sets) a missing server FAILS them instead."""
import os
import re

import pytest

from app.engine import dbt_export as d


def _required(server: str) -> bool:
    """Whether the run requires this live server (TABULARIA_TEST_REQUIRE): then its tests fail
    without it instead of being skipped, which would let a regression on it pass unnoticed."""
    return server in {s.strip() for s in os.getenv("TABULARIA_TEST_REQUIRE", "").split(",")}


def _missing(server: str, variable: str):
    if _required(server):
        pytest.fail(f"{variable} is not set, and this run requires {server} (TABULARIA_TEST_REQUIRE)")
    pytest.skip(f"needs {variable}")


# the ClickHouse tests that need no helper: skipped without the server, unless the run requires it
_WITH_CLICKHOUSE = pytest.mark.skipif(not os.getenv("CLICKHOUSE_TEST_HOST") and not _required("clickhouse"), reason="needs CLICKHOUSE_TEST_HOST")


def _resolve(src: dict):
    if "model" in src:
        return "{{ ref('%s') }}" % src["model"], ["id", "x"], {}
    if "seed" in src:
        return "{{ ref('%s') }}" % src["seed"], ["id", "y"], {}
    return "{{ source('db', '%s') }}" % src["key"].split("/")[-1], ["id", "canale", "importo"], {}


# ── compilation ──────────────────────────────────────────────────────────────
def test_one_cte_per_operation_and_the_schema_follows_the_renames():
    sql, cols, _ = d.compile_model_sql(
        {"key": "datasets/ordini"},
        [{"type": "rename", "params": {"mapping": {"importo": "ricavo"}}},
         {"type": "filter", "params": {"column": "canale", "operator": "eq", "value": "web"}},
         {"type": "group_by", "params": {"by": ["canale"], "aggregations": [{"column": "ricavo", "func": "sum", "alias": "tot"}]}}],
        _resolve,
    )
    assert cols == ["canale", "tot"]
    # every step has a name that says what it does, a note in words, one clause per line
    assert sql.startswith("WITH\nsource_ordini AS (\n    -- source: ordini\n    SELECT *\n    FROM {{ source('db', 'ordini') }}")
    assert "rename_columns AS (\n    -- rename importo to ricavo\n    SELECT * RENAME (\"importo\" AS \"ricavo\")\n    FROM source_ordini" in sql
    assert "filter_canale AS (\n    -- keep rows where canale = 'web'" in sql and "WHERE \"canale\" = 'web'" in sql
    assert "aggregate_by_canale AS (\n    -- aggregate by canale: sum(ricavo) as tot" in sql and "GROUP BY \"canale\"" in sql
    assert sql.endswith("SELECT *\nFROM aggregate_by_canale")


def test_a_model_may_start_from_another_model_or_a_seed():
    sql, cols, _ = d.compile_model_sql({"model": "int_pulito"}, [{"type": "limit", "params": {"n": 5}}], _resolve)
    assert "FROM {{ ref('int_pulito') }}" in sql and cols == ["id", "x"]
    sql, cols, _ = d.compile_model_sql({"seed": "seed_listino"}, [], _resolve)
    assert "FROM {{ ref('seed_listino') }}" in sql and cols == ["id", "y"]


def test_the_publishs_sort_ignores_the_columns_not_there():
    sql, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "sort", "params": {"by": ["sparita", "id"], "ignore_missing": True}}], _resolve)
    assert 'ORDER BY "id" ASC NULLS LAST' in sql and "sparita" not in sql
    sql, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "sort", "params": {"by": ["sparita"], "ignore_missing": True}}], _resolve)
    assert "ORDER BY" not in sql


def test_sort_on_several_columns_keeps_a_direction_each():
    ops = [{"type": "sort", "params": {"by": ["sparita", "importo", "id"], "descending": [True, True, False], "ignore_missing": True}}]
    sql, _, _ = d.compile_model_sql({"key": "k"}, ops, _resolve)
    assert 'ORDER BY "importo" DESC NULLS LAST, "id" ASC NULLS LAST' in sql and "sparita" not in sql
    assert "-- sort by importo descending, id ascending (nulls last)" in sql
    sql, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "sort", "params": {"by": ["importo", "id"], "descending": True}}], _resolve)
    assert 'ORDER BY "importo" DESC NULLS LAST, "id" DESC NULLS LAST' in sql


def test_join_with_right_branch_compiled_as_ctes_and_right_columns():
    sql, cols, _ = d.compile_model_sql(
        {"key": "a"},
        [{"type": "join", "params": {"how": "left", "on": "id", "right": {"source": {"key": "b"}, "operations": [{"type": "select", "params": {"columns": ["id", "canale"]}}]}}}],
        _resolve,
    )
    assert cols == ["id", "canale", "importo", "canale_right"]
    assert "LEFT JOIN" in sql and "source('db', 'b')" in sql


def test_foreach_and_native_pivot_are_refused_with_a_clear_message():
    with pytest.raises(d.DbtExportError, match="foreach"):
        d.compile_model_sql({"key": "k"}, [{"type": "foreach", "params": {}}], _resolve)
    piv = [{"type": "pivot", "params": {"index": ["id"], "on": ["canale"], "values": "importo"}}]
    sql, cols, _ = d.compile_model_sql({"key": "k"}, piv, _resolve)            # DuckDB can
    assert "PIVOT" in sql and cols == []
    with pytest.raises(d.DbtExportError, match="federated"):
        d.compile_model_sql({"key": "k"}, piv, _resolve, native=True)


# ── native dialect ───────────────────────────────────────────────────────────
def test_the_postgres_translation_expands_stars_and_casts_round():
    raw, _, _ = d.compile_model_sql(
        {"key": "k"},
        [{"type": "compute", "params": {"columns": [{"name": "pct", "expr": 'ROUND("importo" / 3, 2)'}]}}],
        lambda src: ("__s_0", ["id", "canale", "importo"], {}), native=True,
    )
    sql = _code(d.transpile_model(raw, "postgres", {"__s_0": ["id", "canale", "importo"]}))
    assert "*" not in sql                                        # stars expanded: explicit columns
    assert re.search(r'ROUND\(CAST\(.*AS DECIMAL\), 2\)', " ".join(sql.split()).replace("( ", "(").replace(" )", ")"))   # Postgres has no round(double, int)
    sql = d.substitute_source_idents(sql, {"__s_0": "{{ source('src', 'ordini') }}"})
    assert "{{ source('src', 'ordini') }} AS \"__s_0\"" in sql


def test_the_clickhouse_translation_leaves_round():
    raw, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "compute", "params": {"columns": [{"name": "pct", "expr": 'ROUND("importo", 2)'}]}}],
                                    lambda src: ("__s_0", ["id", "importo"], {}))
    sql = d.transpile_model(raw, "clickhouse", {"__s_0": ["id", "importo"]})
    assert "DECIMAL" not in sql and "ROUND(" in sql


# ── singular tests from the contract ──────────────────────────────────────────────
def test_the_contracts_rules_become_queries_selecting_the_violations():
    t = d.singular_test_sql
    assert t({"kind": "range", "column": "eta", "min": 0, "max": 120}) == 'SELECT * FROM "__t" AS "__t" WHERE "eta" IS NOT NULL AND ("eta" < 0 OR "eta" > 120)'
    assert t({"kind": "row_count", "min": 10}) == 'SELECT COUNT(*) AS n FROM "__t" AS "__t" HAVING COUNT(*) < 10'
    # the whole value, read as text (#31): anchored, and cast when the column's type is not known as text
    assert "regexp_matches(CAST(\"cap\" AS VARCHAR), '^(?:^[0-9]{5}$)$')" in t({"kind": "pattern", "column": "cap", "regex": "^[0-9]{5}$"})
    assert "regexp_matches(\"cap\", '^(?:[0-9]{5})$')" in t({"kind": "pattern", "column": "cap", "regex": "[0-9]{5}"}, {"cap": "VARCHAR"})
    assert t({"kind": "unique_combination", "columns": ["a", "b"]}) == 'SELECT "a", "b", COUNT(*) AS n FROM "__t" AS "__t" GROUP BY "a", "b" HAVING COUNT(*) > 1'
    assert t({"kind": "expression", "sql": "importo >= 0"}) == 'SELECT * FROM "__t" AS "__t" WHERE NOT COALESCE((importo >= 0), FALSE)'
    with pytest.raises(d.DbtExportError):
        t({"kind": "freshness", "max_age_hours": 24})


def test_a_singular_test_translates_to_postgres_with_the_models_ref():
    sql = d.singular_test_sql({"kind": "pattern", "column": "cap", "regex": "^[0-9]{5}$"})
    translated_one = d.transpile_model(sql, "postgres", {"__t": ["id", "cap"]})
    translated_one = d.substitute_source_idents(translated_one, {"__t": "{{ ref('clienti') }}"})
    # a dbt test selects the violating rows: a `*` is fine (left to the warehouse)
    assert " ".join(translated_one.split()).startswith("SELECT * FROM {{ ref('clienti') }} AS \"__t\" WHERE")
    assert "~" in translated_one or "REGEXP" in translated_one.upper()        # Postgres's regexp


# ── project ──────────────────────────────────────────────────────────────────
def _project(**kw):
    models = [
        {"name": "int_pulito", "sql": "select 1", "materialized": "ephemeral", "description": "Shared steps", "columns": []},
        {"name": "margine", "sql": "select 2", "materialized": "table", "description": "Output «margine»: the margin. Contract: what we promise",
         # the route writes unique and accepted_values as singular tests (#34, #35): only not_null is generic
         "columns": [{"name": "categoria", "description": "The product's category", "tests": [{"kind": "not_null", "severity": "error"}]},
                     {"name": "margine", "description": "", "tests": [{"kind": "not_null", "severity": "warn"}]}]},
        {"name": "margini", "sql": "select 3", "materialized": "incremental", "description": "Table report.margini (append)", "columns": [], "schema": "report", "alias": "margini"},
    ]
    sources = [{"name": "db_1_vendite", "database": "db_1", "schema": "vendite",
                 "tables": [{"name": "ordini", "description": "Datasource «orders»", "columns": [{"name": "id", "description": "The order id"}]}]}]
    base = dict(flow_name="Margin by Category", models=models, attachments=[{"alias": "db_1", "db_type": "postgresql", "pw_env": "TABULARIA_DB_1_PASSWORD",
                "conn": {"host": "db", "port": 5432, "database": "shop", "username": "shop"}}], sources=sources)
    base.update(kw)
    return d.build_dbt_project(**base)


def test_the_project_has_schema_yml_with_descriptions_and_tests_from_the_contract():
    files = _project()
    schema = files["models/schema.yml"]
    assert "- name: margine\n    description: \"Output «margine»: the margin. Contract: what we promise\"" in schema
    # quote: true — dbt's generic tests write the column name in the SQL as it is (#33)
    assert "- name: \"categoria\"\n        description: \"The product's category\"\n        quote: true\n        data_tests:\n          - not_null:\n              config: {severity: error}\n" in schema
    assert "- name: \"margine\"\n        quote: true\n        data_tests:\n          - not_null:\n              config: {severity: warn}" in schema
    # unique and accepted_values are singular tests now (the route writes them): never a generic one
    with pytest.raises(d.DbtExportError):
        d._schema_yaml([{"name": "m", "columns": [{"name": "c", "tests": [{"kind": "unique", "severity": "error"}]}]}])
    assert "ephemeral" in files["models/int_pulito.sql"]
    assert files["models/margini.sql"].startswith("{{ config(materialized='incremental', incremental_strategy='append', schema='report', alias='margini') }}")
    assert "generate_schema_name" in files["macros/generate_schema_name.sql"]
    assert "description: \"Datasource «orders»\"" in files["models/sources.yml"] and "description: \"The order id\"" in files["models/sources.yml"]
    assert "seed-paths" in files["dbt_project.yml"] and "test-paths" in files["dbt_project.yml"]
    assert "dbt test" in files["README.md"] and "`int_pulito` (ephemeral)" in files["README.md"]


def test_every_model_says_where_it_comes_from():
    """Traceability: the data team, in front of a model, finds the flow in the app — at the
    top of the file, in schema.yml (config.meta + tag) and in the README."""
    meta = {"flow": "Margin by Category", "flow_id": 7, "flow_version": 3, "version_saved_at": "2026-10-09 18:00 UTC", "folder": "Sales",
            "owner": "anna@example.com", "output": "output «margine» (datasource)", "node": "out-1"}
    models = [
        {"name": "src_q", "sql": "select 0", "materialized": "ephemeral", "description": "", "columns": [],
         "meta": {"datasource": "Orders {{ x }}", "datasource_id": 4}, "tags": ["tabularia"]},
        {"name": "margine", "sql": "select 2", "materialized": "table", "description": "Out", "columns": [],
         "meta": meta, "tags": ["tabularia", "margin_by_category"]},
    ]
    files = _project(models=models, exported_by="admin@example.com")
    sql = files["models/margine.sql"]
    # facts of the flow only: exporting the same version again gives the same file
    assert sql.startswith("{{ config(materialized='table') }}\n\n-- Exported from Tabularia: flow «Margin by Category» (id 7, version 3 saved 2026-10-09 18:00 UTC), "
                          "output «margine» (datasource).\n-- Built by anna@example.com. Re-exporting the same version gives this same file.\n\nselect 2")
    assert "admin@example.com" not in sql
    # a name with Jinja does not open a block in the comment
    assert "«Orders { { x } }» (id 4)" in files["models/src_q.sql"]
    schema = files["models/schema.yml"]
    assert ("  - name: margine\n    description: \"Out\"\n    config:\n      tags: [\"tabularia\", \"margin_by_category\"]\n      meta:\n        tabularia:\n"
            "          flow: \"Margin by Category\"\n          flow_id: 7\n          flow_version: 3\n"
            "          version_saved_at: \"2026-10-09 18:00 UTC\"\n          folder: \"Sales\"\n") in schema
    assert "exported" not in schema
    readme = files["README.md"]
    assert "## Where it comes from" in readme and "- Tabularia flow «Margin by Category» (id 7, version 3, saved 2026-10-09 18:00 UTC), built by anna@example.com" in readme
    assert "Exported by admin@example.com." in readme and "tag:tabularia" in readme


def test_without_traceability_the_file_stays_as_it_was():
    files = _project()
    assert files["models/margine.sql"] == "{{ config(materialized='table') }}\n\nselect 2\n"
    assert "config:" not in files["models/schema.yml"].split("- name: margine")[1].split("columns:")[0]
    assert "Where it comes from" not in files["README.md"]


def test_seeds_singular_tests_and_notes_end_in_the_project():
    files = _project(seeds=[{"name": "seed_listino", "csv": "id,prezzo\n1,2.5\n", "description": "A snapshot", "columns": [{"name": "id", "description": ""}]}],
                      tests=[{"name": "margine__range_r1", "sql": "select * from {{ ref('margine') }} where x < 0", "severity": "warn", "description": "Contract rule range"}],
                      notes=["The seed is a copy."])
    assert files["seeds/seed_listino.csv"] == "id,prezzo\n1,2.5\n"
    assert "- name: seed_listino\n    description: \"A snapshot\"" in files["seeds/schema.yml"]
    assert files["tests/margine__range_r1.sql"] == "{{ config(severity='warn') }}\n-- Contract rule range\nselect * from {{ ref('margine') }} where x < 0\n"
    assert "dbt seed" in files["README.md"] and "- The seed is a copy." in files["README.md"]


def test_the_native_profile_has_no_attach_and_the_federated_one_has():
    native = _project(attachments=None, native={"db_type": "postgresql", "pw_env": "P", "target_schema": "dbt_tabularia",
                                                 "conn": {"host": "db", "port": 5432, "database": "shop", "username": "shop"}})
    assert "type: postgres" in native["profiles.yml"] and "attach" not in native["profiles.yml"]
    federato = _project()
    assert "type: duckdb" in federato["profiles.yml"] and "alias: db_1" in federato["profiles.yml"] and "env_var('TABULARIA_DB_1_PASSWORD')" in federato["profiles.yml"]



# ── parity with the engine (2026-10-10, from the dbt matrix oracle) ───────────────
TYPES = {"id": "INTEGER", "testo": "VARCHAR", "numero": "DOUBLE", "categoria": "VARCHAR", "Nome Strano": "VARCHAR"}


def _code(sql: str) -> str:
    """The SQL without comments (the steps' notes may hold '*' or 'contains')."""
    import re
    return re.sub(r"/\*.*?\*/|--[^\n]*", "", sql, flags=re.S)


def _tipato(src):
    return "__s_0", list(TYPES), dict(TYPES)


def test_the_source_takes_tabularias_types():
    sql, cols, types = d.compile_model_sql({"key": "k"}, [], _tipato)
    assert 'CAST("testo" AS VARCHAR) AS "testo"' in sql and 'CAST("Nome Strano" AS VARCHAR) AS "Nome Strano"' in sql
    assert cols == list(TYPES) and types == TYPES


def test_the_cast_is_the_engines_in_federated_and_guarded_in_native():
    from app.engine.duckdb_ops import _cast_expr
    op = [{"type": "cast", "params": {"columns": {"testo": "int", "numero": "int"}}}]
    fed, _, types = d.compile_model_sql({"key": "k"}, op, _tipato)
    assert _cast_expr('"testo"', "VARCHAR", "BIGINT") in fed and _cast_expr('"numero"', "DOUBLE", "BIGINT") in fed
    assert types["testo"] == "BIGINT"
    nat, _, _ = d.compile_model_sql({"key": "k"}, op, _tipato, native=True)
    assert "TRY_CAST" not in nat and "regexp_matches(trim(\"testo\")" in nat and 'CAST(trunc("numero") AS BIGINT)' in nat
    pg = d.transpile_model(nat, "postgres", {"__s_0": list(TYPES)})
    # the guard checks the value too (#17): an integer literal that fits a BIGINT, a number in its range
    assert f"~ '{d._RE_INTEGER}'" in pg and "TRY" not in pg and '>= -9223372036854775808 AND "source_source"."numero" < 9223372036854775808' in " ".join(pg.split())


def test_compute_replaces_the_column_in_its_place():
    sql, cols, _ = d.compile_model_sql({"key": "k"}, [{"type": "compute", "params": {"columns": [{"name": "numero", "expr": "numero * 10"}]}}], _tipato)
    assert 'REPLACE ((numero * 10) AS "numero")' in sql and cols == list(TYPES)
    # EXTRACT(… FROM …) is a scalar expression: the export carries it as it is
    sql, cols, _ = d.compile_model_sql({"key": "k"}, [{"type": "compute", "params": {"columns": [{"name": "anno", "expr": "EXTRACT(YEAR FROM data)"}]}}], _tipato)
    assert "(EXTRACT(YEAR FROM data)) AS \"anno\"" in sql and cols[-1] == "anno"


def test_full_join_with_using_fills_the_key_on_the_right_too():
    sql, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "join", "params": {"how": "full", "on": "categoria", "right": {"source": {"key": "r"}, "operations": []}}}], _tipato)
    assert 'COALESCE(s1."categoria", r1."categoria") AS "categoria"' in sql.replace("\n", " ").replace("  ", " ") or "COALESCE(" in sql


def test_text_filters_in_native_use_portable_forms():
    for op, expected in (("contains", "strpos(CAST(\"categoria\" AS VARCHAR), 'il') > 0"), ("starts_with", "left(CAST(\"categoria\" AS VARCHAR), 2) = 'il'"),
                       ("ends_with", "right(CAST(\"categoria\" AS VARCHAR), 2) = 'il'")):
        nat, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "filter", "params": {"column": "categoria", "operator": op, "value": "il"}}], _tipato, native=True)
        assert expected in nat, op
        pg = _code(d.transpile_model(nat, "postgres", {"__s_0": list(TYPES)}))
        assert "CONTAINS" not in pg.upper().replace("CONTAINS_", "") or op != "contains"
        assert "ENDS_WITH" not in pg.upper()


def test_union_by_name_in_native_lists_the_columns():
    nat, cols, _ = d.compile_model_sql({"key": "k"}, [{"type": "select", "params": {"columns": ["id", "numero"]}},
                                                      {"type": "union", "params": {"right": {"source": {"key": "r"}, "operations": [{"type": "select", "params": {"columns": ["id", "categoria"]}}]}}}],
                                       _tipato, native=True)
    assert "BY NAME" not in nat and 'NULL AS "categoria"' in nat and 'NULL AS "numero"' in nat and cols == ["id", "numero", "categoria"]


def test_unpivot_keeps_nulls_and_uses_the_supertype():
    sql, cols, types = d.compile_model_sql({"key": "k"}, [{"type": "unpivot", "params": {"index": ["id"], "on": ["numero", "id"], "variable_name": "v", "value_name": "x"}}], _tipato)
    assert "UNPIVOT" not in sql and sql.count("UNION ALL") == 1 and "CAST(\"numero\" AS DOUBLE)" in sql
    assert cols == ["id", "v", "x"] and types["x"] == "DOUBLE"


def test_the_sql_node_points_self_and_input_at_the_step_before_even_with_its_own_with():
    q = "WITH x AS (SELECT * FROM input WHERE numero > 0) SELECT id FROM x JOIN self AS s USING (id)"
    sql, cols, _ = d.compile_model_sql({"key": "k"}, [{"type": "sql", "params": {"query": q}}], _tipato)
    assert "FROM source_source AS input" in sql and "JOIN source_source AS s" in sql and "WITH x AS" in sql and cols == ["id"]
    import duckdb  # the model is valid SQL: it is run on a real table
    with_contract = duckdb.connect()
    with_contract.execute('CREATE TABLE "__s_0" (id INTEGER, testo VARCHAR, numero DOUBLE, categoria VARCHAR, "Nome Strano" VARCHAR)')
    with_contract.execute("INSERT INTO \"__s_0\" VALUES (1, 'a', 2.0, 'A', 'x'), (2, 'b', -1.0, 'B', 'y')")
    assert with_contract.execute(sql).fetchall() == [(1,)]


def test_sum_of_integers_returns_bigint_as_in_the_engine():
    sql, _, types = d.compile_model_sql({"key": "k"}, [{"type": "group_by", "params": {"by": ["categoria"], "aggregations": [{"column": "id", "func": "sum", "alias": "s"}, {"column": "numero", "func": "mean", "alias": "m"}]}}], _tipato)
    assert 'CAST(sum("id") AS BIGINT) AS "s"' in sql and types["s"] == "BIGINT" and types["m"] == "DOUBLE"


def test_the_native_translation_keeps_the_names_case():
    nat, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "select", "params": {"columns": ["Nome Strano", "id"]}}], _tipato, native=True)
    pg = d.transpile_model(nat, "postgres", {"__s_0": list(TYPES)})
    assert '"Nome Strano"' in pg and '"nome strano"' not in pg


def test_polars_types():
    assert d.duck_type("Int32") == "INTEGER" and d.duck_type("String") == "VARCHAR" and d.duck_type("Float64") == "DOUBLE"
    assert d.duck_type("Datetime(time_unit='us', time_zone=None)") == "TIMESTAMP" and d.duck_type("Decimal(precision=10, scale=2)") == "DECIMAL(10,2)"
    assert d.duck_type("List(Int64)") is None and d.duck_type(None) is None



def test_after_a_sql_node_the_columns_are_worked_out_and_native_can_go_on():
    ops = [{"type": "sql", "params": {"query": 'SELECT *, numero * 2 AS "Doppio" FROM self WHERE numero > 0'}},
           {"type": "cast", "params": {"columns": {"Doppio": "int"}}}, {"type": "drop", "params": {"columns": ["testo"]}}]
    nat, cols, _ = d.compile_model_sql({"key": "k"}, ops, _tipato, native=True)
    assert cols == ["id", "numero", "categoria", "Nome Strano", "Doppio"]
    pg = d.transpile_model(nat, "postgres", {"__s_0": list(TYPES)})
    assert '"Doppio"' in pg and '"Nome Strano"' in pg and "EXCLUDE" not in pg


@pytest.mark.parametrize("ops", [
    [{"type": "select", "params": {"columns": ["id", "categoria"]}}],
    [{"type": "reorder", "params": {"columns": ["valore"]}}],
    [{"type": "drop", "params": {"columns": ["valore"]}}],
    [{"type": "rename", "params": {"mapping": {"valore": "select"}}}],
    [{"type": "cast", "params": {"columns": {"valore": "str"}}}],
    [{"type": "filter", "params": {"column": "valore", "operator": "gt", "value": 1}}],
    [{"type": "sort", "params": {"by": ["valore"], "descending": True}}],
    [{"type": "group_by", "params": {"by": ["categoria"], "aggregations": [{"column": "valore", "func": "sum", "alias": "tot"}]}}],
    [{"type": "unique", "params": {"subset": ["categoria"]}}],
    [{"type": "fill_null", "params": {"columns": {"valore": 0}}}],
    [{"type": "drop_nulls", "params": {}}],
    [{"type": "limit", "params": {"n": 2}}],
    [{"type": "compute", "params": {"columns": [{"name": "doppio", "expr": "valore * 2"}]}}],
    [{"type": "pivot", "params": {"index": ["id"], "on": "categoria", "values": "valore", "func": "sum"}}],
    [{"type": "unpivot", "params": {"index": ["id"], "on": ["valore", "altro"], "variable_name": "misura", "value_name": "v"}}],
    [{"type": "sql", "params": {"query": "SELECT categoria, count(*) AS n FROM self GROUP BY categoria"}}],
], ids=lambda ops: ops[0]["type"])
def test_every_steps_federated_sql_really_runs_in_duckdb(ops):
    """A model's SQL RUNS, not only compiles: the steps' names are identifiers without
    quotes, and a CTE named `pivot` or `unpivot` (DuckDB's reserved words) failed the model
    in dbt."""
    import duckdb

    with_contract = duckdb.connect()
    with_contract.execute("CREATE TABLE casi AS SELECT * FROM (VALUES (1, 'a', 1.5, 10), (2, 'b', NULL, 20), (3, 'a', 3.0, 30)) t(id, categoria, valore, altro)")

    def resolve(_):
        return "casi", ["id", "categoria", "valore", "altro"], {"id": "INTEGER", "categoria": "VARCHAR", "valore": "DOUBLE", "altro": "INTEGER"}, "casi"

    sql, cols, _ = d.compile_model_sql({"key": "x"}, ops, resolve)
    rows = with_contract.execute(sql).fetchall()
    assert rows is not None
    if cols:
        assert [c[0] for c in with_contract.description] == cols


def test_a_step_is_never_named_as_a_reserved_word():
    assert d._step_slug("pivot") == "pivot_step" and d._step_slug("Select") == "select_step"
    assert d._step_slug("filter_stato") == "filter_stato" and d._step_slug("2024") == "s_2024"


# ── ClickHouse target: the customer's ClickHouse reads Postgres/MySQL live ──────
_PG = {"id": 1, "name": "gestionale", "db_type": "postgresql", "host": "db.x.it", "port": 5432, "database": "erp", "username": "shop", "pw_env": "TABULARIA_DB_1_PASSWORD"}
_MY = {"id": 2, "name": "negozio", "db_type": "mysql", "host": "my.x.it", "port": 3306, "database": "shop", "username": "web", "pw_env": "TABULARIA_DB_2_PASSWORD"}


def test_every_postgres_or_mysql_schema_is_a_federated_database():
    ch = d.TargetClickHouse([_PG, _MY, {"id": 3, "name": "crm", "db_type": "clickhouse", "host": "CH.local", "database": "analytics"}], "ch.local")
    # a schema that is not a plain lowercase name has a fingerprint of its exact name too (#68)
    piu = d.federated_db_name(1, "Vendite Più")
    assert ch.database_per(1, "vendite") == "tab_src_1_vendite" and ch.database_per(1, "Vendite Più") == piu and piu.startswith("tab_src_1_vendite_pi_")
    assert ch.database_per(2, "shop") == "tab_src_2_shop"
    assert ch.database_per(3, "analytics") == "analytics"          # the same server: its tables, as they are
    assert {f["name"]: f["engine"] for f in ch.federated.values()} == {"tab_src_1_vendite": "PostgreSQL", piu: "PostgreSQL", "tab_src_2_shop": "MySQL"}
    sources = ch.sources([{"name": "db_1_vendite", "database": None, "schema": "vendite", "connection_id": 1, "tables": []}])
    assert sources[0]["schema"] == "tab_src_1_vendite" and sources[0]["database"] is None


def test_another_clickhouse_or_a_type_clickhouse_cannot_read_are_refused():
    ch = d.TargetClickHouse([{"id": 3, "name": "crm", "db_type": "clickhouse", "host": "altro.local", "database": "a"},
                             {"id": 4, "name": "lago", "db_type": "trino", "host": "t.local"}], "ch.local")
    with pytest.raises(d.DbtExportError, match="ANOTHER ClickHouse"):
        ch.database_per(3, "a")
    with pytest.raises(d.DbtExportError, match="trino"):
        ch.database_per(4, "x")


def test_a_datasources_query_translates_onto_the_federated_tables():
    ch = d.TargetClickHouse([_PG], "ch.local")
    sql = ch.translate_query("WITH a AS (SELECT * FROM crm.clienti WHERE attivo) SELECT a.id, o.totale::numeric FROM a JOIN ordini o ON o.cliente = a.id", 1, "clienti")
    assert "tab_src_1_crm.clienti" in sql and "tab_src_1_public.ordini" in sql      # without schema: public, as in Postgres
    assert "FROM a" in sql and "tab_src_1_public.a" not in sql                       # the CTE stays a CTE
    assert set(ch.federated) == {"tab_src_1_crm", "tab_src_1_public"}
    with pytest.raises(d.DbtExportError, match="cannot be translated"):
        ch.translate_query("SELECT FROM WHERE (", 1, "rotta")


def test_the_clickhouse_project_has_profile_macro_and_readme():
    ch = d.TargetClickHouse([_PG], "ch.local")
    sources = ch.sources([{"name": "db_1_vendite", "database": None, "schema": "vendite", "connection_id": 1,
                            "tables": [{"name": "ordini", "description": "", "columns": []}]}])
    files = _project(sources=sources, attachments=None, clickhouse={"host": "ch.local", "port": 8443, "secure": True, "target_schema": "dbt_tabularia",
                                                                      "federati": list(ch.federated.values())})
    prof = files["profiles.yml"]
    assert "type: clickhouse" in prof and "env_var('CLICKHOUSE_HOST', 'ch.local')" in prof and "env_var('CLICKHOUSE_PORT', '8443')" in prof
    assert "env_var('CLICKHOUSE_PASSWORD')" in prof and "join_use_nulls: 1" in prof and "attach" not in prof
    macro = files["macros/create_tabularia_sources.sql"]
    # one table per source table, no database engine any more (#25: it typed a date as Date, 1955 → 2134)
    assert "CREATE DATABASE IF NOT EXISTS `tab_src_1_vendite`\n" in macro
    assert "ENGINE = PostgreSQL('db.x.it:5432', 'erp', 'ordini', 'shop', '{{ pw_tab_src_1_vendite }}', 'vendite')" in macro
    assert "env_var('TABULARIA_DB_1_PASSWORD')" in macro
    assert "database:" not in files["models/sources.yml"] and "schema: tab_src_1_vendite" in files["models/sources.yml"]
    readme = files["README.md"]
    assert "pip install dbt-clickhouse" in readme and "dbt run-operation create_tabularia_sources --log-level-file none" in readme
    assert readme.index("run-operation") < readme.index("dbt run\n") and "`db.x.it:5432`" in readme


def test_a_value_with_jinja_delimiters_does_not_enter_the_macro():
    # decision of 2026-10-16 (#2): a value with `{`, `%` or `#` is neutralised, no longer refused
    assert d._ch_literal("o'neil\\x") == "'o\\'neil\\\\x'"
    for v in ("{{ evil }}", "sales#2", "a{%b", "100%"):
        assert _render_like_dbt(d._ch_literal(v)) == "'" + v + "'"


def test_the_clickhouse_export_translates_the_query_and_takes_all_to_federated_databases():
    import io, zipfile
    from app.api.routes import dbt as route

    def body(stream):
        import asyncio

        async def read():
            return b"".join([c async for c in stream.body_iterator])
        return asyncio.run(read())

    req = route.ExportRequest(
        flow_name="Margini", mode="clickhouse", sources=[{"name": "db_1_vendite", "database": None, "schema": "vendite", "connection_id": 1,
                                                          "tables": [{"name": "ordini", "description": "", "columns": []}]}],
        source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_vendite', 'ordini') }}", "columns": ["id", "cliente"],
                          "types": {"id": "Int64", "cliente": "Int64"}, "label": "ordini"}},
        clickhouse={"connections": [_PG], "target_schema": "dbt_tabularia"},
        models=[
            {"name": "src_clienti", "kind": "raw", "materialized": "ephemeral", "raw_sql": "SELECT id, nome FROM crm.clienti WHERE attivo",
             "columns": [{"name": "id", "dtype": "Int64"}, {"name": "nome", "dtype": "String"}], "translate": {"connection_id": 1, "label": "clienti"}},
            {"name": "margini", "source": {"key": "k"}, "operations": [
                {"type": "join", "params": {"how": "left", "left_on": ["cliente"], "right_on": ["id"], "right": {"source": {"model": "src_clienti"}}}}]},
        ],
    )
    with zipfile.ZipFile(io.BytesIO(body(route.export_dbt(req)))) as z:
        files = {n: z.read(n).decode() for n in z.namelist()}
    assert "tab_src_1_crm.clienti" in files["models/src_clienti.sql"] and "Nullable(" in files["models/src_clienti.sql"]
    assert "{{ ref('src_clienti') }}" in files["models/margini.sql"] and "{{ source('db_1_vendite', 'ordini') }}" in files["models/margini.sql"]
    macro = files["macros/create_tabularia_sources.sql"]
    assert "`tab_src_1_vendite`" in macro and "`tab_src_1_crm`" in macro          # the schema only the query needs too
    assert "translated from postgresql to ClickHouse" in files["README.md"]


def test_semi_and_anti_join_on_clickhouse_are_the_engines():
    """ClickHouse has no correlated EXISTS: semi/anti become LEFT SEMI/ANTI JOIN, as in the
    engine; the other warehouses keep EXISTS."""
    def resolve(src):
        if src.get("key") == "destra":
            return "__s_1", ["k", "v"], {"k": "BIGINT", "v": "VARCHAR"}, "destra"
        return "__s_0", ["k", "x"], {"k": "BIGINT", "x": "VARCHAR"}, "casi"
    for how in ("semi", "anti"):
        ops = [{"type": "join", "params": {"how": how, "on": ["k"], "right": {"source": {"key": "destra"}}}}]
        sql_ch, cols, _ = d.compile_model_sql({"key": "casi"}, ops, resolve, native=True, dialect="clickhouse")
        assert f"LEFT {how.upper()} JOIN" in sql_ch and "EXISTS" not in sql_ch and cols == ["k", "x"]
        translated_one = d.transpile_model(sql_ch, "clickhouse", {"__s_0": ["k", "x"], "__s_1": ["k", "v"]})
        assert f"LEFT {how.upper()} JOIN" in translated_one
        sql_pg, _, _ = d.compile_model_sql({"key": "casi"}, ops, resolve, native=True, dialect="postgres")
        assert "EXISTS" in sql_pg


# ── download options: a folder for an existing project, layers, sources declared already ──
def _with_types(**kw):
    models = [
        {"name": "tab_src_q", "kind": "raw", "sql": "select 0", "materialized": "ephemeral", "description": "", "columns": []},
        {"name": "tab_int_pulito", "kind": "intermediate", "sql": "select 1", "materialized": "ephemeral", "description": "", "columns": []},
        {"name": "tab_margine", "kind": "output", "sql": "select 2", "materialized": "table", "description": "", "columns": [],
         "meta": {"flow": "Margin by Category", "flow_id": 7}, "tags": ["tabularia", "margin_by_category"]},
        {"name": "tab_righe", "kind": "output", "sql": "select 3", "materialized": "incremental", "description": "", "columns": [],
         "schema": "report", "alias": "righe"},
    ]
    sources = [{"name": "erp", "schema": "vendite", "database": "db_1", "declared": True, "tables": [{"name": "ordini"}, {"name": "clienti"}]},
                {"name": "db_1_crm", "schema": "crm", "database": "db_1", "tables": [{"name": "contatti", "description": "", "columns": []}]}]
    return _project(models=models, sources=sources, **kw)


def test_the_complete_project_with_layers_puts_the_models_by_layer():
    files = _with_types(layout={"package": "project", "folder": "margini", "layers": True})
    assert {"models/margini/staging/tab_src_q.sql", "models/margini/intermediate/tab_int_pulito.sql",
            "models/margini/marts/tab_margine.sql", "models/margini/schema.yml", "models/margini/sources.yml",
            "profiles.yml", "dbt_project.yml", "README.md", "macros/margini/generate_schema_name.sql"} <= set(files)
    assert "INTEGRATION.md" not in files


def test_the_folder_for_an_existing_project_has_only_what_to_copy_and_the_instructions():
    files = _with_types(layout={"package": "folder", "folder": "margini", "layers": False},
                      seeds=[{"name": "tab_seed_listino", "csv": "id\n1\n", "description": "", "columns": []}],
                      tests=[{"name": "tab_margine__range_r1", "sql": "select 1", "severity": "warn", "description": "x"}])
    assert not {"profiles.yml", "dbt_project.yml", "README.md"} & set(files)
    assert {"models/margini/tab_margine.sql", "seeds/margini/tab_seed_listino.csv", "seeds/margini/schema.yml",
            "tests/margini/tab_margine__range_r1.sql", "INTEGRATION.md"} <= set(files)
    # no generate_schema_name: it would rename the schemas of their whole project (INTEGRATION.md says so)
    assert not any("generate_schema_name" in f for f in files)
    # the «already declared» source does not enter our sources.yml: dbt would refuse the duplicate
    assert "name: erp" not in files["models/margini/sources.yml"] and "name: db_1_crm" in files["models/margini/sources.yml"]
    guide = files["INTEGRATION.md"]
    assert "`erp` is **declared in your project**: it must list the tables `clienti`, `ordini`." in guide
    assert "dbt build -s +tag:margin_by_category" in guide and "alias: db_1" in guide and "attach:" in guide
    assert "`schema='report'`" in guide and "generate_schema_name" in guide
    assert "`models/margini/`" in guide and "`seeds/margini/`" in guide and "`tests/margini/`" in guide


def test_the_clickhouse_folder_carries_the_macro_and_says_join_use_nulls():
    ch = d.TargetClickHouse([_PG], "ch.local")
    sources = ch.sources([{"name": "db_1_vendite", "schema": "vendite", "connection_id": 1, "tables": [{"name": "ordini"}]},
                           {"name": "loro", "schema": "crm", "connection_id": 1, "declared": True, "tables": [{"name": "contatti"}]}])
    assert set(ch.federated) == {"tab_src_1_vendite"}                     # the declared one makes no federated database
    files = _project(sources=sources, attachments=None, layout={"package": "folder", "folder": "f"},
                      clickhouse={"host": "ch.local", "port": 8443, "secure": True, "federati": list(ch.federated.values())})
    assert "macros/f/create_tabularia_sources.sql" in files and "profiles.yml" not in files
    guide = files["INTEGRATION.md"]
    assert "join_use_nulls: 1" in guide and "dbt run-operation create_tabularia_sources --log-level-file none" in guide
    assert "DBT_PROFILES_DIR" not in guide


def test_the_chosen_schema_goes_in_the_duckdb_profile_and_an_odd_folder_is_refused():
    assert "      schema: analisi\n" in _project(schema="analisi")["profiles.yml"]
    assert "schema:" not in _project()["profiles.yml"]
    with pytest.raises(d.DbtExportError):
        _project(layout={"package": "folder", "folder": "../fuori"})


# ── determinism and refusals with a code ───────────────────────────────────────
def _export_request(**kw):
    from app.api.routes import dbt as route

    base = dict(flow_name="Margini", mode="duckdb", sources=[], source_map={"k": {"ref": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "x"],
                                                                                  "types": {"id": "Int64", "x": "String"}, "label": "ordini"}},
                models=[{"name": "uno", "source": {"key": "k"}, "operations": [{"type": "filter", "params": {"column": "x", "operator": "eq", "value": "a"}}]},
                        {"name": "due", "source": {"model": "uno"}, "operations": []}])
    base.update(kw)
    return route.ExportRequest(**base)


def _zip(req):
    import asyncio

    from app.api.routes import dbt as route

    async def read(s):
        return b"".join([c async for c in s.body_iterator])
    return asyncio.run(read(route.export_dbt(req)))


def test_the_zip_is_the_same_byte_for_byte_at_every_export():
    """Exporting the same version with the same options again gives the same zip: a diff in
    the team's repository shows only what changed in the flow."""
    import io, zipfile

    first, second = _zip(_export_request()), _zip(_export_request())
    assert first == second
    with zipfile.ZipFile(io.BytesIO(first)) as z:
        items = z.infolist()
    assert [v.filename for v in items] == sorted(v.filename for v in items)
    assert {v.date_time for v in items} == {(1980, 1, 1, 0, 0, 0)}


def test_a_refusal_reaches_the_gateway_with_code_and_parameters():
    from fastapi import HTTPException

    req = _export_request(models=[{"name": "uno", "source": {"key": "k"}, "operations": [{"type": "pivot", "params": {"index": ["id"], "on": "x", "values": "id"}}]}],
                            mode="native", native={"db_type": "postgresql", "conn": {"host": "h", "port": 5432, "database": "d", "username": "u"}, "pw_env": "P"},
                            source_map={"k": {"ident": "__s_0", "macro": "{{ source('s', 'ordini') }}", "columns": ["id", "x"], "types": {"id": "Int64", "x": "String"}, "label": "ordini"}})
    with pytest.raises(HTTPException) as e:
        _zip(req)
    assert e.value.status_code == 422 and e.value.detail["code"] == "pivot_not_portable"
    assert "pivot" in e.value.detail["message"] and e.value.detail["params"] == {}
    with pytest.raises(d.DbtExportError) as e2:
        d.TargetClickHouse([{"id": 3, "name": "crm", "db_type": "clickhouse", "host": "altro"}], "ch").database_per(3, "a")
    assert e2.value.code == "other_clickhouse" and e2.value.params == {"connection": "crm", "host": "altro"}


def test_std_and_var_on_clickhouse_give_null_with_one_value():
    """As the engine: ClickHouse would give NaN; the model guards it with count > 1."""
    def resolve(_):
        return "__s_0", ["k", "v"], {"k": "VARCHAR", "v": "DOUBLE"}, "t"
    ops = [{"type": "group_by", "params": {"by": ["k"], "aggregations": [{"column": "v", "func": "std", "alias": "sd"}]}}]
    sql_ch, _, _ = d.compile_model_sql({"key": "t"}, ops, resolve, native=True, dialect="clickhouse")
    assert 'CASE WHEN count("v") > 1 THEN stddev("v") END' in sql_ch
    translated_one = d.transpile_model(sql_ch, "clickhouse", {"__s_0": ["k", "v"]})
    assert "CASE WHEN COUNT(" in translated_one and "stddevSamp" in translated_one
    sql_pg, _, _ = d.compile_model_sql({"key": "t"}, ops, resolve, native=True, dialect="postgres")
    assert "CASE WHEN" not in sql_pg


# ── the translation the AI proposes: the engine makes the checks ─────────────────
_MACRO = {"__s_0": "{{ source('db_1_v', 'ordini') }}", "__m_0": "{{ ref('clienti') }}"}


def test_a_good_proposal_is_used_with_the_macros_in_place_of_the_placeholders():
    sql = d.use_proposal("SELECT o.id, c.nome FROM __s_0 o LEFT JOIN __m_0 AS c ON o.cliente = c.id", "clickhouse", _MACRO, ["id", "nome"], "m")
    assert "{{ source('db_1_v', 'ordini') }}" in sql and "{{ ref('clienti') }}" in sql and "__TABULARIA" not in sql
    assert '"o"' in sql or "o." in sql      # the alias the AI chose stays


@pytest.mark.parametrize("proposal,reason", [
    ("SELECT o.id FROM __s_0 o", "columns"),
    ("SELECT * FROM __s_0", "star"),
    ("SELECT id, nome FROM altra_tabella", "unknown_table"),
    ("SELECT id, nome FROM s3('http://x/y.csv', 'CSV')", "table_function"),
    ("INSERT INTO t SELECT id, nome FROM __s_0", "not_select"),
    ("SELECT id, nome FROM __s_0; DROP TABLE x", "not_select"),
    ("SELEC id nome FRM", "unparsable"),
])
def test_a_proposal_failing_the_checks_is_refused_with_its_reason(proposal, reason):
    with pytest.raises(d.DbtExportError) as e:
        d.use_proposal(proposal, "clickhouse", _MACRO, ["id", "nome"], "m")
    assert e.value.code == "ai_translation_invalid" and e.value.params["reason"] == reason and e.value.params["model"] == "m"


def test_a_datasources_query_translated_by_ai_is_pointed_by_the_code():
    ch = d.TargetClickHouse([_PG], "ch.local")
    sql = ch.translate_query("SELECT impossibile", 1, "clienti", proposal="SELECT id, nome FROM crm.clienti WHERE attivo = 1", columns=["id", "nome"])
    assert "tab_src_1_crm.clienti" in sql and set(ch.federated) == {"tab_src_1_crm"}
    with pytest.raises(d.DbtExportError) as e:
        ch.translate_query("x", 1, "clienti", proposal="SELECT id FROM crm.clienti", columns=["id", "nome"])
    assert e.value.params["reason"] == "columns"


def test_the_model_translated_by_ai_says_so_and_the_readme_too(monkeypatch):
    from app.api.routes import dbt as route

    def broken(*a, **k):
        raise d.DbtExportError("non va", "translation_failed", dialect="clickhouse", detail="x")

    req = _export_request(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "dbt_tabularia"},
                            source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "x"],
                                              "types": {"id": "Int64", "x": "String"}, "label": "ordini"}},
                            models=[{"name": "uno", "source": {"key": "k"}, "operations": [{"type": "filter", "params": {"column": "x", "operator": "eq", "value": "a"}}]}])
    monkeypatch.setattr(route, "transpile_model", broken)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _zip(req)
    prm = e.value.detail["params"]
    assert e.value.detail["code"] == "translation_failed" and prm["model"] == "uno" and prm["columns"] == ["id", "x"]
    assert prm["source_dialect"] == "duckdb" and prm["target_dialect"] == "clickhouse" and "__s_0" in prm["sql"]
    import io, zipfile
    req.overrides = {"uno": "SELECT id, x FROM __s_0 WHERE x = 'a'"}
    req.ai = {"model": "finto"}
    with zipfile.ZipFile(io.BytesIO(_zip(req))) as z:
        files = {n: z.read(n).decode() for n in z.namelist()}
    assert "-- Translated by AI from duckdb: review it before use." in files["models/uno.sql"] and "{{ source('db_1_v', 'ordini') }}" in files["models/uno.sql"]
    assert "          ai_translated: true\n" in files["models/schema.yml"]
    assert "translated by an AI model" in files["README.md"] and "`uno`" in files["README.md"]


# ── the ClickHouse SQL read by a real ClickHouse parser (EXPLAIN AST) ─────────
def test_the_syntax_check_sees_what_sqlglot_lets_through(monkeypatch):
    pytest.importorskip("chdb")
    c = d.ClickHouseSyntaxCheck()
    error = c.error("SELECT id FROM tab_src_1_v.ordini WHERE canale SIMILAR TO 'w%'")
    assert error and error.startswith("Syntax error") and "SIMILAR" in error and "Expected one of" not in error
    assert c.error('SELECT "__s_0"."id" AS "id" FROM "__s_0" AS "__s_0" WHERE match("__s_0"."x", \'a\')') is None
    assert c.error("SELECT 1 FROM t SAMPLE SYSTEM 50 PERCENT") is not None
    off = d.ClickHouseSyntaxCheck()
    off._via = False                       # no parser reachable: the check is skipped
    assert off.error("SELECT FROM WHERE") is None


def test_a_sql_node_clickhouse_cannot_read_is_untranslated_with_what_the_ai_needs():
    pytest.importorskip("chdb")
    from fastapi import HTTPException

    req = _export_request(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "dbt_tabularia"},
                            source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "x"],
                                              "types": {"id": "Int64", "x": "String"}, "label": "ordini"}},
                            models=[{"name": "uno", "source": {"key": "k"}, "operations": [
                                {"type": "sql", "params": {"query": "SELECT id, x FROM self WHERE x SIMILAR TO 'a%'"}}]}])
    with pytest.raises(HTTPException) as e:
        _zip(req)
    det = e.value.detail
    assert det["code"] == "translation_failed" and "SIMILAR" in det["params"]["detail"] and det["params"]["model"] == "uno"
    assert det["params"]["target_dialect"] == "clickhouse" and det["params"]["columns"] == ["id", "x"]


def test_a_postgres_query_clickhouse_cannot_read_and_a_proposal_it_cannot_read():
    pytest.importorskip("chdb")
    from fastapi import HTTPException

    base = dict(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "dbt_tabularia"}, source_map={},
                models=[{"name": "src_q", "kind": "raw", "materialized": "ephemeral", "raw_sql": "SELECT id, canale FROM vendite.ordini WHERE canale SIMILAR TO 'w%'",
                         "columns": [{"name": "id", "dtype": "Int64"}, {"name": "canale", "dtype": "String"}], "translate": {"connection_id": 1, "label": "ordini web"}}])
    with pytest.raises(HTTPException) as e:
        _zip(_export_request(**base))
    det = e.value.detail
    assert det["code"] == "query_not_translatable" and "SIMILAR" in det["params"]["detail"]
    assert det["params"]["sql"].startswith("SELECT id, canale FROM vendite.ordini") and det["params"]["source_dialect"] == "postgres"
    # an AI proposal ClickHouse cannot read is discarded with its reason
    with pytest.raises(HTTPException) as e:
        _zip(_export_request(**base, overrides={"src_q": "SELECT id, canale FROM vendite.ordini WHERE canale SIMILAR TO 'w%'"}))
    assert e.value.detail["code"] == "ai_translation_invalid" and e.value.detail["params"]["reason"] == "syntax"
    # a good proposal passes, with the tables pointed by the code
    import io, zipfile
    with zipfile.ZipFile(io.BytesIO(_zip(_export_request(**base, overrides={"src_q": "SELECT id, canale FROM vendite.ordini WHERE match(canale, '^w')"})))) as z:
        sql = z.read("models/src_q.sql").decode()
    assert "tab_src_1_vendite.ordini" in sql and "Translated by AI from postgres" in sql


def test_the_clickhouse_target_refuses_oracle_with_a_reason():
    # ClickHouse cannot read Oracle live: no federated database, a clear refusal
    ch = d.TargetClickHouse([{"id": 9, "name": "contabilita", "db_type": "oracle", "host": "ora.x.it", "database": "ORCLPDB1"}], "ch.local")
    with pytest.raises(d.DbtExportError) as e:
        ch.database_per(9, "FIN")
    assert e.value.code == "clickhouse_cannot_read" and e.value.params["db_type"] == "oracle"


def test_the_clickhouse_target_refuses_sql_server_with_a_reason():
    ch = d.TargetClickHouse([{"id": 9, "name": "vendite", "db_type": "sqlserver", "host": "sql.x.it", "database": "vendite"}], "ch.local")
    with pytest.raises(d.DbtExportError) as e:
        ch.database_per(9, "dbo")
    assert e.value.code == "clickhouse_cannot_read" and e.value.params["db_type"] == "sqlserver"


# ── user text is data, never a Jinja template (#2) ────────────────────────────
# dbt renders every model, test, macro and YAML value as Jinja. A value typed in the
# flow ('{{ env_var(...) }}', 'price {% 10', a column named 'c{{a}}lc') must reach the
# warehouse as that text, exactly as Tabularia publishes it.
_OPENER = re.compile(r"\{[{%#]")
_JLIT = r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\""     # one Jinja string literal
_JSTR = r"(?:" + _JLIT + r")(?: ~ (?:" + _JLIT + r"))*"   # a string expression: literals joined with ~
_ESCAPE = re.compile(r"\{\{ '(\{\{|\{%|\{#|\\r|\\n)' \}\}")
_REL = re.compile(r"\{\{ (source|ref)\((" + _JSTR + r")(?:, (" + _JSTR + r"))?\) \}\}")
_CONFIG = re.compile(r"\{\{ config\((\w+=" + _JSTR + r"(?:, \w+=" + _JSTR + r")*)\) \}\}")


def _jinja_value(expr: str) -> str:
    """A Jinja string expression (literals joined with `~`) → its value, each literal read as
    Jinja reads it: its line breaks as `\\n`, then its escapes."""
    return "".join(_jinja_data(lit[1:-1]).encode("ascii", "backslashreplace").decode("unicode-escape") for lit in re.findall(_JLIT, expr))


def _config_args(call: str) -> dict:
    """The arguments of a config() call the exporter wrote: {name: the expression as written}."""
    out, i = {}, 0
    while (g := re.compile(r"(\w+)=(" + _JSTR + r")(?:, |$)").match(call, i)) is not None:
        out[g.group(1)] = g.group(2)
        i = g.end()
    assert i == len(call), call
    return out


def _render_like_dbt(text: str, relations: dict | None = None, config: dict | None = None) -> str:
    """What dbt's Jinja makes of a file the exporter wrote, for the only constructs the
    exporter may write: an escape that prints a Jinja opener, source()/ref() (→ the
    relation in `relations`) and config() (→ nothing; its arguments go into `config`).
    Any other `{{`, `{%` or `{#` opens a template that Jinja would RUN: the test fails.
    As Jinja does (keep_trailing_newline is off), the template's final line break is dropped."""
    out, i = [], 0
    while (m := _OPENER.search(text, i)) is not None:
        out.append(_jinja_data(text[i:m.start()]))
        for rx in (_ESCAPE, _REL, _CONFIG):
            g = rx.match(text, m.start())
            if g:
                break
        else:
            raise AssertionError(f"Jinja would run user text: {text[m.start():m.start() + 80]!r}")
        if rx is _ESCAPE:
            out.append({"\\r": "\r", "\\n": "\n"}.get(g.group(1), g.group(1)))
        elif rx is _REL:
            out.append((relations or {})[(g.group(1),) + tuple(_jinja_value(a) for a in g.groups()[1:] if a is not None)])
        elif config is not None:
            config.update({k: _jinja_value(v) for k, v in _config_args(g.group(1)).items()})
        i = g.end()
    tail = _jinja_data(text[i:])
    return "".join(out) + (tail[:-1] if tail.endswith("\n") else tail)


def _jinja_data(text: str) -> str:
    """Jinja writes a template's text with every line break as `\\n` (dbt 1.12.5: a `\\r\\n` in a
    model's file reached the warehouse as `\\n`, a lone `\\r` too)."""
    return re.sub(r"\r\n?", "\n", text)


_JINJA_TEXT = {
    "secret": '{{ env_var("TABULARIA_DB_1_PASSWORD") }}',
    "expr": "{{ 7 * 7 }}",
    "tag": "price {% 10",
    "comment": "x {# y",
    "braces": "{{{ 6*7 }}}",
    "endraw": "{% endraw %}{{ run_query('select 1') }}",
}


def _ops_with_jinja(sql_node: bool = True) -> list:
    j = _JINJA_TEXT
    ops = [
        {"type": "fill_null", "params": {"columns": {"nota": j["secret"]}}},
        {"type": "filter", "params": {"column": "nota", "operator": "ne", "value": j["tag"]}},
        {"type": "compute", "params": {"columns": [{"name": "calc", "expr": "'" + j["expr"] + "' || '" + j["comment"] + "'"}]}},
        {"type": "rename", "params": {"mapping": {"calc": "c{{a}}lc"}}},
    ]
    if sql_node:
        ops.append({"type": "sql", "params": {"query": "SELECT *, '" + (j["braces"] + j["endraw"]).replace("'", "''") + "' AS extra FROM self"}})
    return ops


def _files(req) -> dict:
    import io, zipfile

    with zipfile.ZipFile(io.BytesIO(_zip(req))) as z:
        return {n: z.read(n).decode() for n in z.namelist()}


def test_jinja_in_a_value_is_written_as_text_not_rendered():
    """Federated target: the model, rendered as dbt renders it, gives in DuckDB the
    literal texts — not the source password, not '49', not a parse error."""
    import duckdb

    j = _JINJA_TEXT
    req = _export_request(models=[{"name": "uno", "source": {"key": "k"}, "operations": _ops_with_jinja()}],
                            source_map={"k": {"ref": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "nota"],
                                              "types": {"id": "Int64", "nota": "String"}, "label": "ordini"}})
    sql = _render_like_dbt(_files(req)["models/uno.sql"], {("source", "db_1_v", "ordini"): "ordini"}, config={})
    with_contract = duckdb.connect()
    with_contract.execute("CREATE TABLE ordini AS SELECT * FROM (VALUES (1, 'a'), (2, NULL), (3, 'price {% 10')) t(id, nota)")
    rows = with_contract.execute(f"SELECT * FROM ({sql}) ORDER BY id").fetchall()
    assert [c[0] for c in with_contract.description] == ["id", "nota", "c{{a}}lc", "extra"]
    calc, extra = j["expr"] + j["comment"], j["braces"] + j["endraw"]
    assert rows == [(1, "a", calc, extra), (2, j["secret"], calc, extra)]


def test_jinja_in_a_value_is_neutralised_on_the_native_and_clickhouse_targets():
    j = _JINJA_TEXT
    native = {"db_type": "postgresql", "conn": {"host": "h", "port": 5432, "database": "d", "username": "u"}, "pw_env": "P"}
    smap = {"k": {"ident": "__s_0", "macro": "{{ source('s', 'ordini') }}", "columns": ["id", "nota"],
                  "types": {"id": "Int64", "nota": "String"}, "label": "ordini"}}
    for mode, extra in (("native", {"native": native}), ("clickhouse", {"clickhouse": {"connections": [_PG], "target_schema": "dbt_tabularia"}})):
        req = _export_request(mode=mode, source_map=smap, **extra,
                                models=[{"name": "uno", "source": {"key": "k"}, "operations": _ops_with_jinja(sql_node=mode == "native")}])
        sql = _render_like_dbt(_files(req)["models/uno.sql"], {("source", "s", "ordini"): "s.ordini"})
        for text in (j["secret"], j["tag"], j["expr"], j["comment"]):
            assert text in sql, (mode, text)
        assert "c{{a}}lc" in sql


def test_jinja_in_a_datasource_query_and_in_an_ai_proposal_is_neutralised():
    raw = [{"name": "src_q", "kind": "raw", "materialized": "ephemeral", "columns": [{"name": "id", "dtype": "Int64"}],
            "raw_sql": "SELECT * FROM postgres_query('db_1', 'SELECT id FROM t WHERE c = ''{%'' OR d = ''{{ 1 }}''')"}]
    sql = _render_like_dbt(_files(_export_request(models=raw, source_map={}))["models/src_q.sql"])
    assert "c = ''{%'' OR d = ''{{ 1 }}''" in sql
    proposal = "SELECT id, x FROM __s_0 WHERE x = '{{ env_var(\"P\") }}' /* {% do run_query('DROP TABLE t') %} */"
    translated_one = d.use_proposal(proposal, "clickhouse", _MACRO, ["id", "x"], "m")
    rendered_one = _render_like_dbt(translated_one, {("source", "db_1_v", "ordini"): "v.ordini", ("ref", "clienti"): "clienti"})
    assert "'{{ env_var(\"P\") }}'" in rendered_one and "v.ordini" in rendered_one


def test_jinja_in_contract_rules_and_in_the_model_config_is_neutralised():
    req = _export_request(models=[{"name": "uno", "source": {"key": "k"}, "operations": [], "alias": "o'neil {{ x }}", "schema": "s{%b",
                                     "tests": [{"id": "r1", "kind": "expression", "sql": "x <> '{{ 7*7 }}'"},
                                               {"id": "r2", "kind": "pattern", "column": "x", "regex": "^\\{#"}],
                                     # accepted values are a singular test's SQL since #34 (they were YAML values)
                                     "columns": [{"name": "x", "description": "", "tests": [
                                         {"kind": "accepted_values", "severity": "error", "values": ["{{ 7*7 }}", "{% if x %}", "{{x}}"]}]}]}])
    files = _files(req)
    for path in ("tests/uno__expression_r1.sql", "tests/uno__pattern_r2.sql", "tests/uno__accepted_values_x.sql"):
        _render_like_dbt(files[path], {("ref", "uno"): "uno"})
    assert "x <> '{{ 7*7 }}'" in _render_like_dbt(files["tests/uno__expression_r1.sql"], {("ref", "uno"): "uno"})
    assert "IN ('{{ 7*7 }}', '{% if x %}', '{{x}}')" in _render_like_dbt(files["tests/uno__accepted_values_x.sql"], {("ref", "uno"): "uno"})
    config: dict = {}
    _render_like_dbt(files["models/uno.sql"], {("source", "db_1_v", "ordini"): "o"}, config)
    assert config["alias"] == "o'neil {{ x }}" and config["schema"] == "s{%b"


def _static_parser_value(arg: str):
    """What dbt's static parser (dbt_extractor, on by default for models) makes of a config()
    argument: a lone string literal AS WRITTEN, escapes included (dbt 1.12.5: `alias='o\\'brien'`
    gave the alias `o\\'brien`, the table dbt then looked for); any other expression
    (`'a' ~ 'b'`) it leaves to Jinja: None."""
    return arg[1:-1] if re.fullmatch(_JLIT, arg) else None


# A name with a quote or a backslash (a native output's real table, its schema) was written
# with backslash escapes: Jinja reads them, dbt's static parser keeps them. dbt-postgres then
# looked for `zz\'q` (`dbt run` failed with a syntax error) and dbt-clickhouse built the table
# but did not find it again (TABLE_ALREADY_EXISTS on the second `dbt run`)
@pytest.mark.parametrize("name", ["o'brien", "a\\b", "x'y\"z", "città'è", "'", "\\", "\\\\'", "a }} b {{ c {% d %}",
                                  "t\\x') }}{{ log('zz' ~ 'alias', info=True) }}{{ config(a='", "a\rb", "o'\r\n\\", "\r"])
def test_a_name_in_config_reads_the_same_with_dbt_s_static_parser_and_with_jinja(name):
    # the config line alone: which names an adapter can write is another check (name_not_quotable)
    line = d._model_header({"materialized": "table", "alias": name, "schema": name}).split(") }}\n")[0] + ") }}"
    config: dict = {}
    _render_like_dbt(line, config=config)
    assert config["alias"] == name and config["schema"] == name, line                     # what Jinja reads
    for key in ("alias", "schema"):
        arg = _config_args(line[len("{{ config("):-len(") }}")])[key]
        assert _static_parser_value(arg) in (None, name), arg                            # what the static parser reads


def test_an_ordinary_name_in_config_is_written_as_before():
    files = _files(_export_request(models=[{"name": "uno", "source": {"key": "k"}, "operations": [], "alias": "orders", "schema": "report"}]))
    assert files["models/uno.sql"].startswith("{{ config(materialized='table', schema='report', alias='orders') }}\n")


@pytest.mark.parametrize("host", ["ch.local", "o'h", "a\\b", 'q"h', "x'y\"z\\"])
def test_the_clickhouse_profile_gives_back_the_engine_s_host(host):
    """profiles.yml is YAML first (the value in double quotes), then Jinja: the default of
    CLICKHOUSE_HOST must come out of both as the host. A backslash escape is not a YAML one
    (`\\'`: the profile did not load)."""
    import json

    line = next(r for r in d._clickhouse_profile("p", {"host": host, "port": 8123}).splitlines() if r.startswith("      host: "))
    value = json.loads(line[len("      host: "):])                     # a double-quoted YAML scalar is JSON
    m = re.fullmatch(r"\{\{ env_var\('CLICKHOUSE_HOST', (" + _JSTR + r")\) \}\}", value)
    assert m and _jinja_value(m.group(1)) == host, line
    if host == "ch.local":
        assert line == "      host: \"{{ env_var('CLICKHOUSE_HOST', 'ch.local') }}\""


def test_jinja_in_the_yaml_values_is_neutralised():
    """Descriptions, column names, meta and names: dbt renders them (accepted values: see
    test_jinja_in_contract_rules_and_in_the_model_config_is_neutralised)."""
    import json

    t = "Note {{ env_var('TABULARIA_DB_1_PASSWORD') }} {% if x %} {#"
    models = [{"name": "uno", "sql": "select 1", "materialized": "table", "description": t, "meta": {"flow": t, "flow_id": 7},
                "tags": ["tabularia"], "columns": [{"name": "c{{a}}lc", "description": t,
                                                   "tests": [{"kind": "not_null", "severity": "error"}]}]}]
    sources = [{"name": "db_1_v", "database": "db_1", "schema": "v",
                 "tables": [{"name": "ordini", "description": t, "columns": [{"name": "n{%x", "description": t}]}]}]
    files = _project(models=models, sources=sources,
                      seeds=[{"name": "seed_x", "csv": "a\n1\n", "description": t, "columns": [{"name": "a{#", "description": t}]}])
    for path in ("models/schema.yml", "models/sources.yml", "seeds/schema.yml"):
        values = [json.loads(v) for v in re.findall(r'"(?:[^"\\]|\\.)*"', files[path])]
        rendered = [_render_like_dbt(v) for v in values]
        assert t in rendered, path
    assert "c{{a}}lc" in set(_render_like_dbt(json.loads(v)) for v in re.findall(r'"(?:[^"\\]|\\.)*"', files["models/schema.yml"]))


def test_jinja_in_a_step_comment_never_opens_a_block():
    for text in ("{{{ 7*7 }}}", "{%%}", "{#{#", "a{{%b"):
        assert not _OPENER.search(d._comment(text)), text


# ── the federated source password stays out of the ATTACH path (#32) ──────────
def test_the_federated_profile_attaches_through_a_secret_not_a_dsn_with_the_password():
    """DuckDB prints the ATTACH path on every failed connection (wrong password, source
    down) and dbt writes it to the console and logs/dbt.log; an unquoted libpq/MySQL DSN
    also breaks on a space or a backslash. The password goes into a DuckDB secret
    instead: the path is empty and the error says `at ""` (proved with real dbt)."""
    my = {"alias": "db_2", "db_type": "mysql", "pw_env": "TABULARIA_DB_2_PASSWORD",
          "conn": {"host": "my.x", "port": 3306, "database": "shop", "username": "web"}}
    files = _project(attachments=[{"alias": "db_1", "db_type": "postgresql", "pw_env": "TABULARIA_DB_1_PASSWORD",
                                    "conn": {"host": "db", "port": 5432, "database": "shop", "username": "shop"}}, my])
    prof = files["profiles.yml"]
    assert "password=" not in prof and "dbname=" not in prof
    assert ("      secrets:\n"
            "        - type: postgres\n          name: tabularia_db_1\n          host: \"db\"\n          port: 5432\n"
            "          database: \"shop\"\n          user: \"shop\"\n          password: \"{{ env_var('TABULARIA_DB_1_PASSWORD') }}\"\n"
            "        - type: mysql\n          name: tabularia_db_2\n          host: \"my.x\"\n          port: 3306\n"
            "          database: \"shop\"\n          user: \"web\"\n          password: \"{{ env_var('TABULARIA_DB_2_PASSWORD') }}\"\n") in prof
    assert ("      attach:\n"
            "        - path: \"\"\n          type: postgres\n          alias: db_1\n          secret: tabularia_db_1\n"
            "        - path: \"\"\n          type: mysql\n          alias: db_2\n          secret: tabularia_db_2\n") in prof
    assert "extensions: [mysql, postgres]" in prof
    # the folder package tells the team the same blocks for THEIR profile
    guide = _project(attachments=[my], layout={"package": "folder", "folder": "f"})["INTEGRATION.md"]
    assert "secrets:" in guide and "secret: tabularia_db_2" in guide and "password=" not in guide


def test_connection_values_in_the_profile_are_quoted_and_neutralised():
    files = _project(attachments=[{"alias": "db_1", "db_type": "postgresql", "pw_env": "TABULARIA_DB_1_PASSWORD",
                                    "conn": {"host": "h#1", "port": 5432, "database": "d: x", "username": "{{ env_var('X') }}"}}])
    prof = files["profiles.yml"]
    assert "host: \"h#1\"" in prof and "database: \"d: x\"" in prof
    assert "user: \"{{ '{{' }} env_var('X') }}\"" in prof


# ── the AI proposal reads only the tables its model reads (#53) ────────────────
@pytest.mark.parametrize("proposal,reason", [
    # a CTE never hides a QUALIFIED table: system.users is read for real
    ("WITH users AS (SELECT 1 AS x) SELECT name AS id, storage AS nome FROM system.users", "unknown_table"),
    # a CTE of an inner query is not visible outside it
    ("SELECT u.id AS id, u.nome AS nome FROM (WITH users AS (SELECT 1 AS a) SELECT a FROM users) AS q JOIN users AS u ON 1 = 1", "unknown_table"),
    # a non-recursive CTE does not see itself: the `users` inside is the real table
    ("WITH users AS (SELECT name AS id, storage AS nome FROM users) SELECT id, nome FROM users", "unknown_table"),
    ("SELECT id, nome FROM __s_0 WHERE id IN system.numbers_mt", "unknown_table"),
    ("SELECT id, nome FROM analytics.__s_0", "unknown_table"),
    ("SELECT dictGet('secrets', 'value', toUInt64(id)) AS id, nome FROM __s_0", "function_not_allowed"),
    ("SELECT file('passwd') AS id, nome FROM __s_0", "function_not_allowed"),
    ("SELECT joinGet('j', 'v', id) AS id, nome FROM __s_0", "function_not_allowed"),
    ("SELECT getSetting('max_threads') AS id, nome FROM __s_0", "function_not_allowed"),
    ("SELECT id, nome FROM __s_0 SETTINGS max_execution_time = 1", "settings"),
])
def test_an_ai_proposal_cannot_read_outside_its_model(proposal, reason):
    with pytest.raises(d.DbtExportError) as e:
        d.use_proposal(proposal, "clickhouse", _MACRO, ["id", "nome"], "m")
    assert e.value.code == "ai_translation_invalid" and e.value.params["reason"] == reason, e.value.params


@pytest.mark.parametrize("proposal,reason", [
    ("WITH pg_authid AS (SELECT 1 AS x) SELECT rolname AS id, rolpassword AS nome FROM pg_catalog.pg_authid", "unknown_table"),
    ("SELECT current_setting('server_version') AS id, nome FROM __s_0", "function_not_allowed"),
    ("SELECT pg_read_file('/etc/passwd') AS id, nome FROM __s_0", "function_not_allowed"),
    ("SELECT id, nome FROM __s_0 WHERE id IN (SELECT x FROM dblink('c', 'q') AS t(x int))", "table_function"),
    ("SELECT dblink_exec('c', 'DROP TABLE t') AS id, nome FROM __s_0", "function_not_allowed"),
    ("SELECT nextval('s') AS id, nome FROM __s_0", "function_not_allowed"),
    ("SELECT set_config('a', 'b', false) AS id, nome FROM __s_0", "function_not_allowed"),
])
def test_an_ai_proposal_for_postgres_cannot_read_outside_its_model(proposal, reason):
    with pytest.raises(d.DbtExportError) as e:
        d.use_proposal(proposal, "postgres", _MACRO, ["id", "nome"], "m")
    assert e.value.params["reason"] == reason, e.value.params


def test_a_good_proposal_with_its_own_ctes_still_passes():
    sql = d.use_proposal("WITH a AS (SELECT id, nome FROM __s_0), b AS (SELECT id, nome FROM a) SELECT id, nome FROM b", "clickhouse",
                         _MACRO, ["id", "nome"], "m")
    assert "{{ source('db_1_v', 'ordini') }}" in sql


def test_an_ai_proposal_for_a_datasource_query_stays_on_its_connection():
    ch = d.TargetClickHouse([_PG], "ch.local")
    # a table behind an inner CTE of the same name is still a table of the connection: re-pointed
    sql = ch.translate_query("x", 1, "clienti", columns=["id", "nome"],
                           proposal="SELECT u.id AS id, u.nome AS nome FROM (WITH users AS (SELECT 1 AS a) SELECT a FROM users) AS q JOIN users AS u ON 1 = 1")
    assert "tab_src_1_public.users" in sql.replace("`", "").replace('"', "")
    for proposal, reason in (("SELECT id, nome FROM crm.clienti WHERE id IN system.one", "unknown_table"),
                             ("SELECT dictGet('d', 'v', id) AS id, nome FROM crm.clienti", "function_not_allowed")):
        with pytest.raises(d.DbtExportError) as e:
            ch.translate_query("x", 1, "clienti", proposal=proposal, columns=["id", "nome"])
        assert e.value.params["reason"] == reason


def test_on_native_a_proposal_reads_only_the_sources_of_its_own_model():
    from fastapi import HTTPException

    smap = {"a": {"ident": "__s_0", "macro": "{{ source('s', 'ordini') }}", "columns": ["id", "x"], "types": {"id": "Int64", "x": "String"}, "label": "ordini"},
            "b": {"ident": "__s_1", "macro": "{{ source('s', 'clienti') }}", "columns": ["id", "x"], "types": {"id": "Int64", "x": "String"}, "label": "clienti"}}
    req = _export_request(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "dbt_tabularia"}, source_map=smap,
                            models=[{"name": "uno", "source": {"key": "a"}, "operations": []}, {"name": "due", "source": {"key": "b"}, "operations": []}],
                            overrides={"due": "SELECT id, x FROM __s_0"})
    with pytest.raises(HTTPException) as e:
        _zip(req)
    assert e.value.detail["code"] == "ai_translation_invalid" and e.value.detail["params"]["reason"] == "unknown_table"
    req.overrides = {"due": "SELECT id, x FROM __s_1"}
    assert _zip(req)[:2] == b"PK"


# ── names YAML would read as something else, and flows named like a dbt package (#39) ──
def test_names_that_yaml_reads_as_booleans_or_null_stay_strings():
    """PyYAML (YAML 1.1) reads on/yes/no/true/false/null as booleans or None: a flow «On»
    gave «Could not find profile named 'on'», an output «Yes» a schema.yml dbt refused."""
    models = [{"name": "yes", "sql": "select 1", "materialized": "table", "description": "", "columns": []},
               {"name": "margine", "sql": "select 2", "materialized": "table", "description": "", "columns": []}]
    sources = [{"name": "on", "database": "null", "schema": "no", "tables": [{"name": "yes", "description": "", "columns": []}, "true"]},
                {"name": "db_1_vendite", "database": "db_1", "schema": "vendite", "tables": [{"name": "a: b#c", "description": "", "columns": []}]}]
    files = _project(flow_name="On", models=models, sources=sources, schema="off",
                      seeds=[{"name": "Null", "csv": "a\n1\n", "description": "", "columns": []}])
    assert files["profiles.yml"].startswith('"on":\n') and '      schema: "off"\n' in files["profiles.yml"]
    assert "name: 'on'" in files["dbt_project.yml"] and "profile: 'on'" in files["dbt_project.yml"] and 'models:\n  "on":\n' in files["dbt_project.yml"]
    schema = files["models/schema.yml"]
    assert '  - name: "yes"\n' in schema and "  - name: margine\n" in schema      # an ordinary name stays as it was
    src = files["models/sources.yml"]
    assert '  - name: "on"\n    database: "null"\n    schema: "no"\n' in src
    assert '      - name: "yes"\n' in src and '      - name: "true"\n' in src and '      - name: "a: b#c"\n' in src
    assert "  - name: db_1_vendite\n    database: db_1\n    schema: vendite\n" in src
    assert '  - name: "Null"\n' in files["seeds/schema.yml"]
    for prof in (_project(flow_name="On", attachments=None, native={"db_type": "postgresql", "pw_env": "P", "target_schema": "yes",
                                                                    "conn": {"host": "db", "port": 5432, "database": "shop", "username": "shop"}})["profiles.yml"],
                 _project(flow_name="On", attachments=None, clickhouse={"host": "ch", "port": 8123, "secure": False, "target_schema": "yes"})["profiles.yml"]):
        assert prof.startswith('"on":\n') and '      schema: "yes"\n' in prof


@pytest.mark.parametrize("flow,project", [("DBT", "tabularia_dbt"), ("dbt duckdb", "tabularia_dbt_duckdb"),
                                             ("dbt-postgres", "tabularia_dbt_postgres"), ("Margin by Category", "margin_by_category")])
def test_a_flow_named_like_a_dbt_package_gets_a_project_name_of_its_own(flow, project):
    """dbt installs its own packages `dbt` and `dbt_<adapter>`: a project with the same
    name fails with «dbt found more than one package with the name …»."""
    files = _project(flow_name=flow)
    assert f"name: '{project}'\n" in files["dbt_project.yml"] and f"profile: '{project}'\n" in files["dbt_project.yml"]
    assert files["profiles.yml"].startswith(f"{project}:\n") and f"path: {project}.duckdb" in files["profiles.yml"]


# ── characters YAML does not allow are escaped, and read back exactly (#61) ────
# YAML's printable set (what PyYAML/libyaml accept in a file), minus the line breaks
# YAML folds into a space inside a quoted scalar (NEL, LS, PS)
_YAML_RAW_FORBIDDEN = re.compile("[^\t\n\r\x20-\x7e\xa0-‧‪-퟿-�\U00010000-\U0010ffff]")


def test_yaml_forbidden_characters_are_escaped_and_read_back_exactly():
    """A C1 control (U+0096: cp1252 text read as latin-1) in a description, a column name
    or an accepted value made `dbt parse` fail: «unacceptable character #x0096»."""
    import json

    odd = ["Smith\x96Jones", "a\x7fb", "x\x85y", "l s", "p s", "z￾", "c\x01d", "eè–"]
    models = [{"name": "uno", "sql": "select 1", "materialized": "table", "description": odd[0], "meta": {"flow": odd[1]},
                "columns": [{"name": s, "description": s, "tests": [{"kind": "not_null", "severity": "error"}]} for s in odd]}]
    sources = [{"name": "db_1_v", "database": "db_1", "schema": "v",
                 "tables": [{"name": "t", "description": odd[2], "columns": [{"name": s, "description": s} for s in odd]}]}]
    files = _project(models=models, sources=sources, attachments=[{"alias": "db_1", "db_type": "postgresql", "pw_env": "P",
                                                                       "conn": {"host": "h\x96", "port": 5432, "database": "d", "username": "u"}}],
                      seeds=[{"name": "seed_x", "csv": "a\n1\n", "description": odd[3], "columns": [{"name": s, "description": ""} for s in odd],
                              "column_types": {s: "VARCHAR" for s in odd}}])
    for path in ("models/schema.yml", "models/sources.yml", "seeds/schema.yml", "profiles.yml"):
        assert not _YAML_RAW_FORBIDDEN.search(files[path]), (path, _YAML_RAW_FORBIDDEN.search(files[path]))
        values = {json.loads(v) for v in re.findall(r'"(?:[^"\\]|\\.)*"', files[path])}
        if path != "profiles.yml":
            assert set(odd) <= values, (path, set(odd) - values)
    assert '"h\\u0096"' in files["profiles.yml"]


# ── E2: the federated SQL gives what the engine gives, and free text keeps its dialect ──
# A parity harness: the same sources and operations through Tabularia's Polars and DuckDB
# engines and through the exported model, run in DuckDB as dbt-duckdb would run it.
import datetime as _dt

import polars as _pl


def _norm(v):
    import decimal

    if v is None or isinstance(v, bool):
        return v
    if isinstance(v, (int, float, decimal.Decimal)):
        return round(float(v), 9)
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    return str(v)


def _pl_family(dtype: str) -> str:
    t = str(dtype).lower()
    if t.startswith(("int", "uint")):
        return "int"
    if t.startswith(("float", "decimal")):
        return "float"
    if t.startswith("datetime"):
        return "datetime"
    return {"string": "str", "utf8": "str", "boolean": "bool", "date": "date"}.get(t, t)


def _duck_family(t: str) -> str:
    t = str(t).upper()
    if t in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UBIGINT", "UINTEGER"):
        return "int"
    if t in ("DOUBLE", "FLOAT") or t.startswith("DECIMAL"):
        return "float"
    if t.startswith("TIMESTAMP"):
        return "datetime"
    return {"VARCHAR": "str", "BOOLEAN": "bool", "DATE": "date"}.get(t, t.lower())


def _key(name: str) -> dict:
    from tests.conftest import BUCKET

    return {"bucket": BUCKET, "key": f"datasets/{name}.parquet"}


def _engine_snapshot(engine: str, sources: dict, main: str, ops: list):
    from app.engine.cache import StepCache
    from tests.conftest import upload_df
    from tests.fakes import FakeRedis, FakeStorage

    storage = FakeStorage()
    srcs = {k: upload_df(storage, df, _key(k)["key"]) for k, df in sources.items()}
    cache = StepCache(storage, redis_client=FakeRedis())
    if engine == "polars":
        from app.engine.polars_engine import PolarsEngine
        eng = PolarsEngine(storage=storage, cache=cache)
    else:
        from app.engine.duckdb_engine import DuckDBEngine
        eng = DuckDBEngine(storage=storage, cache=cache)
    res = eng.preview(srcs[main], ops, limit=100_000, use_cache=False)
    cols = [c.name for c in res.columns]
    rows = sorted((tuple(_norm(r.get(c)) for c in cols) for r in res.rows), key=lambda t: tuple(map(str, t)))
    return cols, rows, [_pl_family(c.dtype) for c in res.columns]


def _export_sql(sources: dict, main: str, ops: list, native: bool = False, dialect=None, **kw):
    def resolve(spec):
        name = spec["key"].split("/")[-1].removesuffix(".parquet")
        df = sources[name]
        return f"t_{name}", list(df.columns), {c: d.duck_type(str(t)) for c, t in df.schema.items()}, name
    return d.compile_model_sql(_key(main), ops, resolve, native=native, dialect=dialect, **kw)


def _duck_with(sources: dict):
    import duckdb

    with_contract = duckdb.connect()
    for name, df in sources.items():
        with_contract.register(f"_df_{name}", df.to_arrow())
        with_contract.execute(f'CREATE TABLE "t_{name}" AS SELECT * FROM "_df_{name}"')
    return with_contract


def _export_snapshot(sources: dict, main: str, ops: list, **kw):
    sql, _, _ = _export_sql(sources, main, ops, **kw)
    rel = _duck_with(sources).sql(sql)
    cols, types = list(rel.columns), [str(t) for t in rel.types]
    rows = sorted((tuple(_norm(v) for v in r) for r in rel.fetchall()), key=lambda t: tuple(map(str, t)))
    return cols, rows, [_duck_family(t) for t in types]


def _parita(sources: dict, main: str, ops: list, engines=("polars", "duckdb")):
    out = {e: _engine_snapshot(e, sources, main, ops) for e in engines}
    out["export"] = _export_snapshot(sources, main, ops)
    return out


_CUSTOMERS = _pl.DataFrame({"cliente": [1, 2, 3], "nome": ["a", "b", "c"]})
_SALES = _pl.DataFrame({"cliente": [1, 1, 2, 3], "anno": [2023, 2024, 2023, 2024], "importo": [10, 20, 30, 40]})
_PIVOT = {"type": "pivot", "params": {"index": ["cliente"], "on": "anno", "values": "importo", "func": "sum"}}


def _equal(par: dict, types: bool = True):
    rif = par["polars"]
    for name, snap in par.items():
        assert snap[0] == rif[0], f"{name}: columns {snap[0]} != {rif[0]}"
        assert snap[1] == rif[1], f"{name}: rows {snap[1]} != {rif[1]}"
        if types:
            assert snap[2] == rif[2], f"{name}: types {snap[2]} != {rif[2]}"


# #0 — a join or a compute after a pivot (columns the data decides) loses nothing
def test_a_join_after_a_pivot_keeps_the_pivot_columns():
    src = {"clienti": _CUSTOMERS, "vendite": _SALES}
    right = [{"type": "join", "params": {"how": "left", "on": "cliente", "right": {"source": _key("vendite"), "operations": [_PIVOT]}}}]
    par = _parita(src, "clienti", right)
    assert par["polars"][0] == ["cliente", "nome", "2023", "2024"]
    _equal(par, types=False)
    left = [_PIVOT, {"type": "join", "params": {"how": "left", "on": "cliente", "right": {"source": _key("clienti")}}}]
    par = _parita(src, "vendite", left)
    assert par["polars"][0] == ["cliente", "2023", "2024", "nome"]
    _equal(par, types=False)
    # keys with other names: the pivot's index comes back as cliente_right, like the engine
    key_names = [{"type": "join", "params": {"how": "full", "left_on": ["cliente"], "right_on": ["cliente"],
                                          "right": {"source": _key("vendite"), "operations": [_PIVOT]}}}]
    par = _parita(src, "clienti", key_names)
    assert par["polars"][0] == ["cliente", "nome", "cliente_right", "2023", "2024"]
    _equal(par, types=False)
    # the pivot on the LEFT, joined back on its index with left_on/right_on (what the editor writes as
    # soon as one key pair differs): the index is known, so the right one becomes cliente_right — the
    # model failed on a duplicate name
    for how in ("left", "inner", "full"):
        back = [_PIVOT, {"type": "join", "params": {"how": how, "left_on": ["cliente"], "right_on": ["cliente"],
                                                        "right": {"source": _key("clienti")}}}]
        par = _parita(src, "vendite", back)
        assert par["polars"][0] == ["cliente", "2023", "2024", "cliente_right", "nome"], how
        _equal(par, types=False)
    # and a column the flow makes after the pivot, or renames, is known too; a dropped one is gone
    after = [_PIVOT, {"type": "compute", "params": {"columns": [{"name": "nome", "expr": "'x'"}]}},
            {"type": "rename", "params": {"mapping": {"cliente": "id"}}},
            {"type": "join", "params": {"how": "left", "left_on": ["id"], "right_on": ["cliente"], "right": {"source": _key("clienti")}}}]
    par = _parita(src, "vendite", after)
    assert par["polars"][0] == ["id", "2023", "2024", "nome", "cliente", "nome_right"]
    _equal(par, types=False)
    removed = [_PIVOT, {"type": "compute", "params": {"columns": [{"name": "nome", "expr": "'x'"}]}},
             {"type": "drop", "params": {"columns": ["nome"]}},
             {"type": "join", "params": {"how": "left", "left_on": ["cliente"], "right_on": ["cliente"], "right": {"source": _key("clienti")}}}]
    par = _parita(src, "vendite", removed)
    assert par["polars"][0] == ["cliente", "2023", "2024", "cliente_right", "nome"]
    _equal(par, types=False)


def test_a_pivot_column_that_the_other_side_also_has_gets_the_right_suffix():
    customers = _CUSTOMERS.with_columns(_pl.Series("2023", ["x", "y", "z"]))
    src = {"clienti": customers, "vendite": _SALES}
    ops = [{"type": "join", "params": {"how": "inner", "on": "cliente", "right": {"source": _key("vendite"), "operations": [_PIVOT]}}}]
    par = _parita(src, "clienti", ops)
    assert par["polars"][0] == ["cliente", "nome", "2023", "2023_right", "2024"]
    _equal(par, types=False)


def test_a_pivot_on_the_left_whose_column_the_right_side_also_has_fails_the_run_loudly():
    """With the pivot on the LEFT, which right columns collide is decided by the data: the
    model cannot add `_right` by itself, so it stops with an error instead of renaming in silence."""
    import duckdb

    customers = _CUSTOMERS.with_columns(_pl.Series("2024", ["x", "y", "z"]))
    src = {"clienti": customers, "vendite": _SALES}
    ops = [_PIVOT, {"type": "join", "params": {"how": "left", "on": "cliente", "right": {"source": _key("clienti")}}}]
    sql, _, _ = _export_sql(src, "vendite", ops)
    with pytest.raises(duckdb.Error, match="2024"):
        _duck_with(src).sql(sql).fetchall()
    # without a collision the same model runs and gives what the engine gives
    _equal(_parita({"clienti": _CUSTOMERS, "vendite": _SALES}, "vendite", ops), types=False)


def test_a_compute_after_a_pivot_replaces_the_column_in_place_or_adds_it():
    ops = [_PIVOT, {"type": "compute", "params": {"columns": [{"name": "2023", "expr": '"2023" * 2'},
                                                              {"name": "tot", "expr": 'coalesce("2023", 0) + coalesce("2024", 0)'}]}}]
    par = _parita({"vendite": _SALES}, "vendite", ops)
    assert par["polars"][0] == ["cliente", "2023", "2024", "tot"]
    _equal(par, types=False)


def test_steps_after_a_pivot_never_claim_to_know_only_some_of_its_columns():
    """reorder and union by name after a pivot used to record a PARTIAL column list as the
    whole: the join after them then listed only those columns."""
    src = {"clienti": _CUSTOMERS, "vendite": _SALES}
    join = {"type": "join", "params": {"how": "left", "on": "cliente", "right": {"source": _key("clienti")}}}
    par = _parita(src, "vendite", [_PIVOT, {"type": "reorder", "params": {"columns": ["2024"]}}, join])
    assert par["polars"][0] == ["2024", "cliente", "2023", "nome"]
    _equal(par, types=False)
    others_ = _pl.DataFrame({"cliente": [9], "extra": ["e"]})
    src["altri"] = others_
    union = {"type": "union", "params": {"right": {"source": _key("altri")}}}
    par = _parita(src, "vendite", [_PIVOT, union, join], engines=("duckdb",))
    assert par["duckdb"][0] == ["cliente", "2023", "2024", "extra", "nome"]
    assert par["export"][:2] == par["duckdb"][:2]


# #3 — a cast after compute / sql / union uses the column's REAL type (truncates, guards text)
_ORDERS = _pl.DataFrame({"id": [1, 2, 3, 4], "importo": [10.7, 20.2, 5.9, -7.6], "testo": ["12.7", "1e3", " 42 ", "x"]})


def test_cast_to_int_of_a_computed_number_truncates_like_the_engine():
    ops = [{"type": "compute", "params": {"columns": [{"name": "x", "expr": "importo * 1"}, {"name": "t2", "expr": "testo || ''"}]}},
           {"type": "cast", "params": {"columns": {"x": "int", "t2": "int"}}}]
    par = _parita({"ordini": _ORDERS}, "ordini", ops)
    assert [r[3:] for r in par["polars"][1]] == [(10.0, None), (20.0, None), (5.0, 42.0), (-7.0, None)]
    _equal(par)
    # native: the same guarded forms, written for the warehouse (no bare CAST of a double or a text)
    nat, _, _ = _export_sql({"ordini": _ORDERS}, "ordini", ops, native=True, dialect="postgres")
    assert 'CAST(trunc("x") AS BIGINT)' in nat and "regexp_matches(trim(\"t2\")" in nat


def test_cast_after_a_sql_node_or_a_union_of_two_types_uses_the_real_type():
    sql_op = [{"type": "sql", "params": {"query": "SELECT id, importo AS f, testo FROM self"}},
              {"type": "cast", "params": {"columns": {"f": "int"}}}]
    _equal(_parita({"ordini": _ORDERS}, "ordini", sql_op))
    others_ = _pl.DataFrame({"id": [9], "importo": [1.5], "testo": ["7"]}).with_columns(_pl.col("testo").cast(_pl.String))
    union = [{"type": "union", "params": {"right": {"source": _key("altri")}}}, {"type": "cast", "params": {"columns": {"importo": "int"}}}]
    _equal(_parita({"ordini": _ORDERS, "altri": others_}, "ordini", union))


def test_a_cast_of_a_column_whose_type_nobody_knows_is_refused_not_guessed():
    """A source column of a type the export cannot read, computed on and then cast: without
    its type the export cannot write the engine's cast (trunc? trim and the integer guard?)."""
    def resolve(_):
        return "__s_0", ["id", "lista"], {"id": "BIGINT", "lista": None}, "t"
    ops = [{"type": "compute", "params": {"columns": [{"name": "n", "expr": "len(lista)"}]}}, {"type": "cast", "params": {"columns": {"n": "int"}}}]
    with pytest.raises(d.DbtExportError) as e:
        d.compile_model_sql({"key": "t"}, ops, resolve)
    assert e.value.code == "cast_type_unknown" and e.value.params == {"column": "n"}
    # a column whose type IS known still casts, and so does a pivot column (its type is the aggregate's)
    d.compile_model_sql({"key": "t"}, [{"type": "cast", "params": {"columns": {"id": "str"}}}], resolve)
    par = _parita({"vendite": _SALES}, "vendite", [_PIVOT, {"type": "cast", "params": {"columns": {"2023": "float"}}}])
    _equal(par, types=False)
    assert par["export"][2] == ["int", "float", "int"]


# #3 #10 — a column of a type the export cannot name (a Postgres uuid, bytea, an array) no longer
# switches the probe off for the whole model: a compute that does not read it is cast, a sql node
# gets DuckDB's names; only what is made from that column stays unknown
def _with_a_uuid(_):
    return "t_src", ["uid", "x", "s"], {"uid": None, "x": "DOUBLE", "s": "VARCHAR"}, "src"


@pytest.mark.parametrize("dialect", [None, "postgres", "clickhouse"])
def test_a_column_of_an_unknown_type_leaves_the_probe_on_for_the_others(dialect):
    import duckdb

    nat = dialect is not None
    ops = [{"type": "compute", "params": {"columns": [{"name": "y", "expr": "x * 2"}, {"name": "u", "expr": "CAST(uid AS VARCHAR)"}]}},
           {"type": "cast", "params": {"columns": {"y": "int", "u": "str"}}}]
    sql, cols, types = d.compile_model_sql({"key": "k"}, ops, _with_a_uuid, native=nat, dialect=dialect)
    assert cols == ["uid", "x", "s", "y", "u"] and types["y"] == "BIGINT" and types["uid"] is None
    if not nat:
        with_contract = duckdb.connect()
        with_contract.execute("CREATE TABLE t_src AS SELECT '5f0c6c2e-8a8f-4f39-9c7e-2f1b9d0a4e11'::UUID AS uid, 10.7 AS x, 'a' AS s")
        assert with_contract.sql(sql).fetchall() == [(__import__("uuid").UUID("5f0c6c2e-8a8f-4f39-9c7e-2f1b9d0a4e11"), 10.7, "a", 21, "5f0c6c2e-8a8f-4f39-9c7e-2f1b9d0a4e11")]
    # a sql node's expression without an alias gets DuckDB's name, as in the engine
    q = [{"type": "sql", "params": {"query": "SELECT x + 1, s FROM self"}}, {"type": "sort", "params": {"by": ["s"]}}]
    _, cols, _ = d.compile_model_sql({"key": "k"}, q, _with_a_uuid, native=nat, dialect=dialect)
    assert cols == ["(x + 1)", "s"]
    # what is made from the unknown column itself is not known: that cast, and only it, is refused
    with pytest.raises(d.DbtExportError) as e:
        d.compile_model_sql({"key": "k"}, [{"type": "compute", "params": {"columns": [{"name": "z", "expr": "coalesce(uid, uid)"}]}},
                                           {"type": "cast", "params": {"columns": {"z": "str"}}}], _with_a_uuid, native=nat, dialect=dialect)
    assert e.value.code == "cast_type_unknown" and e.value.params == {"column": "z"}


@pytest.mark.parametrize("text", ["clickhouse", "bigquery"])
def test_translated_text_that_reads_a_column_of_an_unknown_type_is_not_refused(text):
    """The probe types such a column as a placeholder: text that reads it does not bind there,
    which says nothing about the warehouse (an array has a length) — not refused for that."""
    def resolve(_):
        return "t_src", ["id", "tags"], {"id": "BIGINT", "tags": None}, "src"
    read = "has(tags, 'a')" if text == "clickhouse" else "ARRAY_LENGTH(tags)"
    ops = [{"type": "compute", "params": {"columns": [{"name": "n", "expr": read}]}},
           {"type": "sql", "params": {"query": f"SELECT id, {read} AS m FROM self"}}]
    _, cols, _ = d.compile_model_sql({"key": "k"}, ops, resolve, text=text)
    assert cols == ["id", "m"]
    # text that does not run in DuckDB for another reason is still refused
    with pytest.raises(d.DbtExportError) as e:
        d.compile_model_sql({"key": "k"}, [{"type": "compute", "params": {"columns": [{"name": "n", "expr": "nonexistent_fn(id)"}]}}], resolve, text=text)
    assert e.value.code == "text_not_translatable"


def test_the_probe_of_a_long_wide_flow_takes_well_under_a_second_per_hundred_steps():
    """The probe built a chain of views: each step re-bound the whole chain, so 150 columns over 120
    steps took 10 s or more (synchronously, in the engine's request). Each step is now an empty table
    read only by the next one."""
    import time

    cols = [f"c{i}" for i in range(150)]
    types = {c: ("DOUBLE" if i % 3 == 0 else "VARCHAR" if i % 3 == 1 else "BIGINT") for i, c in enumerate(cols)}
    ops = []
    for k in range(40):
        ops += [{"type": "compute", "params": {"columns": [{"name": f"c{k * 3 % 150}", "expr": f"c{k * 3 % 150} * 2"}]}},
                {"type": "filter", "params": {"column": f"c{(k * 3 + 1) % 150}", "operator": "ne", "value": "x"}},
                {"type": "cast", "params": {"columns": {f"c{k * 3 % 150}": "int"}}}]
    start = time.monotonic()
    _, _, finals = d.compile_model_sql({"key": "k"}, ops, lambda _: ("t_src", cols, types, "src"))
    assert time.monotonic() - start < 4, "the probe re-binds the whole chain at every step"
    assert finals["c0"] == "BIGINT" and finals["c1"] == "VARCHAR"


def test_a_column_of_an_unknown_type_on_native_postgres_casts_the_others():
    import uuid

    ops = [{"type": "compute", "params": {"columns": [{"name": "y", "expr": "x * 2"}]}}, {"type": "cast", "params": {"columns": {"y": "int"}}}]
    info: dict = {}
    raw, _, _ = d.compile_model_sql({"key": "k"}, ops, _with_a_uuid, native=True, dialect="postgres", info=info)
    pg = d.put_back_texts(d.transpile_model(raw, "postgres", {"t_src": ["uid", "x", "s"]}), info)
    values = "(VALUES ('5f0c6c2e-8a8f-4f39-9c7e-2f1b9d0a4e11'::uuid, 10.7::float8, 'a')) AS \"t_src\"(\"uid\", \"x\", \"s\")"
    u = uuid.UUID("5f0c6c2e-8a8f-4f39-9c7e-2f1b9d0a4e11")
    [line] = _su_postgres(pg.replace('"t_src" AS "t_src"', values))
    assert line[0] in (u, u.bytes) and line[1:] == (10.7, "a", 21)   # the driver gives a uuid as its bytes


# #4 — aggregate types are the engine's, also read through a shared step
_PAID = _pl.DataFrame({"g": ["a", "a", "a", "b"], "pagato": [True, True, True, False],
                         "prezzo": [99.99, 99.99, 36.76, 1.0], "giorno": [_dt.date(2024, 1, 1), _dt.date(2024, 1, 3), _dt.date(2024, 1, 2), _dt.date(2024, 2, 1)]}
                        ).with_columns(_pl.col("prezzo").cast(_pl.Decimal(4, 2)))


def _through_a_shared_step(sources, main, first, second):
    """Like the route: model A (a shared int_ step), then model B reading it with {model: A}
    and the column types the export recorded for A."""
    with_contract = _duck_with(sources)
    sql_a, cols_a, types_a = _export_sql(sources, main, first)
    with_contract.execute(f"CREATE VIEW int_a AS {sql_a}")
    sql_b, _, _ = d.compile_model_sql({"model": "int_a"}, second, lambda spec: ("int_a", cols_a, types_a, "int_a"))
    rel = with_contract.sql(sql_b)
    rows = sorted((tuple(_norm(v) for v in r) for r in rel.fetchall()), key=lambda t: tuple(map(str, t)))
    return list(rel.columns), rows, [str(t) for t in rel.types]


def test_a_sum_of_booleans_read_through_a_shared_step_stays_a_count():
    gb = [{"type": "group_by", "params": {"by": ["g"], "aggregations": [{"column": "pagato", "func": "sum", "alias": "n_pagati"},
                                                                         {"column": "prezzo", "func": "sum", "alias": "tot"}]}}]
    fil = [{"type": "filter", "params": {"column": "g", "operator": "eq", "value": "a"}}]
    cols, rows, types = _through_a_shared_step({"pagati": _PAID}, "pagati", gb, fil)
    rif = _engine_snapshot("polars", {"pagati": _PAID}, "pagati", gb + fil)
    assert (cols, rows) == (rif[0], rif[1]) == (["g", "n_pagati", "tot"], [("a", 3.0, 236.74)])
    assert types == ["VARCHAR", "BIGINT", "DECIMAL(38,2)"]


def test_the_median_and_mean_of_dates_read_through_a_shared_step_run():
    gb = [{"type": "group_by", "params": {"by": ["g"], "aggregations": [{"column": "giorno", "func": "median", "alias": "med"},
                                                                         {"column": "giorno", "func": "mean", "alias": "media"}]}}]
    cols, rows, types = _through_a_shared_step({"pagati": _PAID}, "pagati", gb, [{"type": "limit", "params": {"n": 10}}])
    rif = _engine_snapshot("duckdb", {"pagati": _PAID}, "pagati", gb)
    assert (cols, rows) == (rif[0], rif[1]) and types == ["VARCHAR", "TIMESTAMP", "TIMESTAMP"]


def test_integer_sums_after_a_compute_and_in_a_pivot_are_bigint_like_the_engine():
    ops = [{"type": "compute", "params": {"columns": [{"name": "k", "expr": "importo * 2"}]}},
           {"type": "group_by", "params": {"by": ["cliente"], "aggregations": [{"column": "k", "func": "sum", "alias": "tot"}]}}]
    _, _, types = _export_sql({"vendite": _SALES}, "vendite", ops)
    sql, _, _ = _export_sql({"vendite": _SALES}, "vendite", ops)
    assert [str(t) for t in _duck_with({"vendite": _SALES}).sql(sql).types] == ["BIGINT", "BIGINT"] and types["tot"] == "BIGINT"
    sql, _, _ = _export_sql({"vendite": _SALES}, "vendite", [_PIVOT])
    assert [str(t) for t in _duck_with({"vendite": _SALES}).sql(sql).types] == ["BIGINT", "BIGINT", "BIGINT"]


def test_a_model_read_by_another_is_not_cast_again_to_guessed_types():
    sql, _, _ = d.compile_model_sql({"model": "int_a"}, [], lambda spec: ("int_a", ["g", "n"], {"g": "VARCHAR", "n": "BIGINT"}, "int_a"))
    assert "CAST(" not in _code(sql)
    sql, _, _ = d.compile_model_sql({"key": "k"}, [], _tipato)      # a source still gets Tabularia's types
    assert 'CAST("testo" AS VARCHAR)' in sql


# #5 — unpivot of computed columns keeps the numeric supertype
def test_unpivot_of_a_computed_number_stays_a_number():
    src = {"n": _pl.DataFrame({"id": [1, 2, 3, 4], "numero": [12.7, 9.0, 10.0, None]})}
    ops = [{"type": "compute", "params": {"columns": [{"name": "doppio", "expr": "numero * 2"}]}},
           {"type": "unpivot", "params": {"index": ["id"], "on": ["numero", "doppio"], "variable_name": "misura", "value_name": "valore"}},
           {"type": "sort", "params": {"by": ["valore"]}}]
    par = _parita(src, "n", ops)
    assert par["polars"][2] == ["int", "str", "float"]
    _equal(par)
    gb = ops[:2] + [{"type": "group_by", "params": {"by": ["misura"], "aggregations": [{"column": "valore", "func": "sum", "alias": "tot"}]}}]
    _equal(_parita(src, "n", gb))


# #10 — a sql node's unaliased expressions keep the names DuckDB gives them
_CASES = _pl.DataFrame({"id": [1, 2, 3], "g": ["g1", "g1", "g2"], "i": [10, 20, 30], "s": ["x", None, "z"]})
_RIGHT = _pl.DataFrame({"id": [1, 2], "z": ["b1", "b2"]})


@pytest.mark.parametrize("query", ["SELECT id, i + 1 FROM self", "SELECT *, upper(s) FROM self", "SELECT COLUMNS('^(id|g)$') FROM self",
                                   "SELECT id, g, count(*) OVER (PARTITION BY g) FROM self"])
def test_an_unaliased_expression_in_a_sql_node_then_a_join_runs(query):
    ops = [{"type": "sql", "params": {"query": query}},
           {"type": "join", "params": {"how": "inner", "on": "id", "right": {"source": _key("destra")}}}]
    par = _parita({"casi": _CASES, "destra": _RIGHT}, "casi", ops, engines=("duckdb",))
    assert par["export"] == par["duckdb"]
    nul = [{"type": "sql", "params": {"query": query}}, {"type": "drop_nulls", "params": {}}]
    par = _parita({"casi": _CASES}, "casi", nul, engines=("duckdb",))
    assert par["export"] == par["duckdb"]


def test_the_publish_sort_on_a_sql_node_column_named_by_duckdb_is_kept():
    ops = [{"type": "sql", "params": {"query": "SELECT g, count(*) FROM self GROUP BY g"}},
           {"type": "sort", "params": {"by": ["count_star()"], "ignore_missing": True}}]
    sql, cols, _ = _export_sql({"casi": _CASES}, "casi", ops)
    assert cols == ["g", "count_star()"] and 'ORDER BY "count_star()" ASC NULLS LAST' in sql
    assert _duck_with({"casi": _CASES}).sql(sql).fetchall() == [("g2", 1), ("g1", 2)]


@pytest.mark.parametrize("query,names", [("SELECT id, i + 1 FROM self", ["id", "(i + 1)"]),
                                        ("SELECT *, upper(s) FROM self", ["id", "g", "i", "s", "upper(s)"]),
                                        ("SELECT g, count(*) FROM self GROUP BY g", ["g", "count_star()"])])
def test_on_native_a_sql_node_publishes_the_names_duckdb_gives(query, names):
    """Not sqlglot's made-up `_col_1` (and with a `*` next to the expression, sqlglot's
    count did not even match the translated query's)."""
    ops = [{"type": "sql", "params": {"query": query}}, {"type": "rename", "params": {"mapping": {"id": "id"}}}]
    nat, cols, _ = _export_sql({"casi": _CASES}, "casi", ops[:1] + ([] if "GROUP" in query else ops[1:]), native=True, dialect="postgres")
    assert cols == names
    pg = " ".join(d.transpile_model(nat, "postgres", {"t_casi": list(_CASES.columns)}).split())
    assert "_col_" not in pg and f'AS "{names[-1]}"' in pg


# #8 — first/last follow the sort before them, and exist on every target
def _history(n: int = 300_007) -> "_pl.DataFrame":
    # unique dates in a shuffled order (7919 is prime, so i*7919 mod n is a permutation)
    return _pl.DataFrame({"i": range(n)}).select(
        (_pl.col("i") % 7).alias("cliente"),
        (_pl.date(1000, 1, 1) + _pl.duration(days=(_pl.col("i") * 7919) % n)).alias("data"),
        ("s" + _pl.col("i").cast(_pl.String)).alias("stato"))


_FIRST_LAST = [{"type": "sort", "params": {"by": ["data"], "descending": True}},
                 {"type": "filter", "params": {"column": "cliente", "operator": "ge", "value": 0}},
                 {"type": "group_by", "params": {"by": ["cliente"], "aggregations": [
                     {"column": "data", "func": "first", "alias": "ultima"}, {"column": "stato", "func": "first", "alias": "stato_ultimo"},
                     {"column": "data", "func": "last", "alias": "prima"}]}}]


def test_first_and_last_after_a_sort_follow_the_sort_in_the_federated_model():
    """DuckDB's parallel aggregate does not keep the order of the CTE before it: sort by
    date then first per customer gave an arbitrary row; Polars gives the latest."""
    src = {"storico": _history()}
    rif = _engine_snapshot("polars", src, "storico", _FIRST_LAST)
    sql, _, _ = _export_sql(src, "storico", _FIRST_LAST)
    with_contract = _duck_with(src)
    with_contract.execute("SET threads = 8")
    rows = sorted((tuple(_norm(v) for v in r) for r in with_contract.sql(sql).fetchall()), key=lambda t: tuple(map(str, t)))
    assert rows == rif[1]


def test_first_and_last_exist_on_postgres_and_clickhouse_and_keep_the_order():
    src = {"storico": _history(1009)}
    nat, _, _ = _export_sql(src, "storico", _FIRST_LAST, native=True, dialect="postgres")
    pg = _code(d.transpile_model(nat, "postgres", {"t_storico": ["cliente", "data", "stato"]}))
    assert "FIRST(" not in pg.upper() and "LAST(" not in pg.upper() and pg.count("ARRAY_AGG(") == 3 and "ORDER BY" in pg
    info: dict = {}
    ch_raw, _, _ = _export_sql(src, "storico", _FIRST_LAST, native=True, dialect="clickhouse", info=info)
    ch = d.put_back_texts(d.transpile_model(ch_raw, "clickhouse", {"t_storico": ["cliente", "data", "stato"]}), info)
    assert "FIRST(" not in _code(ch).upper() and "arrayReverseSort(" in ch and "__tabularia_" not in ch
    # unordered (no sort before): still a function the warehouse has
    gb = _FIRST_LAST[2:]
    nat, _, _ = _export_sql(src, "storico", gb, native=True, dialect="postgres")
    assert "FIRST(" not in _code(d.transpile_model(nat, "postgres", {"t_storico": ["cliente", "data", "stato"]})).upper()
    info = {}
    ch_raw, _, _ = _export_sql(src, "storico", gb, native=True, dialect="clickhouse", info=info)
    ch = d.put_back_texts(d.transpile_model(ch_raw, "clickhouse", {"t_storico": ["cliente", "data", "stato"]}), info)
    assert "any(" in ch and "anyLast(" in ch and "FIRST(" not in _code(ch).upper()


@_WITH_CLICKHOUSE
def test_first_and_last_after_a_sort_run_on_clickhouse_like_polars():
    import os

    import clickhouse_connect

    df = _pl.DataFrame({"cliente": [1, 1, 1, 1, 2, 2], "data": [3, None, 1, 2, None, None], "stato": ["c", "n", "a", None, "x", "y"]})
    rif = _engine_snapshot("polars", {"t": df}, "t", [{"type": "sort", "params": {"by": ["data"]}}] + _FIRST_LAST[2:])
    ops = [{"type": "sort", "params": {"by": ["data"]}}] + _FIRST_LAST[2:]
    info: dict = {}
    raw, cols, _ = _export_sql({"t": df}, "t", ops, native=True, dialect="clickhouse", info=info)
    ch = d.put_back_texts(d.transpile_model(raw, "clickhouse", {"t_t": list(df.columns)}), info)
    values = ", ".join(f"({c}, {'NULL' if x is None else x}, {'NULL' if s is None else repr(s)})" for c, x, s in df.iter_rows())
    ch = ch.replace('"t_t" AS "t_t"', f"(SELECT * FROM values('cliente Int64, data Nullable(Int64), stato Nullable(String)', {values})) AS \"t_t\"")
    cli = clickhouse_connect.get_client(host=os.environ["CLICKHOUSE_TEST_HOST"], port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8123")),
                                        username=os.getenv("CLICKHOUSE_TEST_USER", "default"), password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""))
    rows = sorted((tuple(_norm(v) for v in r) for r in cli.query(ch).result_rows), key=lambda t: tuple(map(str, t)))
    assert rows == rif[1] == [(1.0, 1.0, "a", None), (2.0, None, "x", None)]


# #9 — a filter Tabularia refuses (no value) is refused, not exported as `= NULL`
@pytest.mark.parametrize("params", [{"column": "g", "operator": "eq"}, {"column": "g", "operator": "ne", "value": None},
                                    {"column": "id", "operator": "between", "value": [1]}, {"column": "id", "operator": "between", "value": 1},
                                    {"column": "g", "operator": "in", "value": []}, {"column": "id", "operator": "gt"}])
def test_a_filter_without_a_value_is_refused_like_the_engine(params):
    from app.engine.exceptions import EngineError
    from app.engine.operations import check_filter_value

    with pytest.raises(EngineError):                                   # what every engine says
        check_filter_value(params["operator"], params.get("value"))
    for native in (False, True):
        with pytest.raises(d.DbtExportError) as e:
            d.compile_model_sql({"key": "k"}, [{"type": "filter", "params": params}], _resolve, native=native)
        assert e.value.code == "filter_value_missing" and e.value.params == {"column": params["column"], "operator": params["operator"]}


def test_a_filter_on_null_still_needs_no_value():
    sql, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "filter", "params": {"column": "canale", "operator": "is_null"}}], _resolve)
    assert 'WHERE "canale" IS NULL' in sql


# #65 — the same flow gives the same file in every engine process (no set order in the SQL)
def test_a_drop_of_several_columns_is_the_same_under_every_hash_seed():
    import os
    import subprocess
    import sys

    code = ("from app.engine import dbt_export as d\n"
              "r = lambda s: ('__s_0', ['id', 'canale', 'importo', 'stato', 'deposito', 'nota'], {})\n"
              "print(d.compile_model_sql({'key': 'k'}, [{'type': 'drop', 'params': {'columns': ['canale', 'importo', 'stato', 'deposito', 'canale']}}], r)[0])")
    outputs = set()
    for seed in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        outputs.add(subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True).stdout)
    assert len(outputs) == 1
    assert 'EXCLUDE ("canale", "importo", "stato", "deposito")' in outputs.pop()


# #11 — an incomplete node is a 422 with a code naming the step, never a 500
@pytest.mark.parametrize("op", [
    {"type": "join", "params": {"how": "inner", "right": {"source": {"key": "k"}}}},                       # no keys
    {"type": "join", "params": {"how": "left", "on": ["id"]}},                                            # nothing on the right handle
    {"type": "union", "params": {}},
    {"type": "filter", "params": {"operator": "eq", "value": "a"}},                                        # no column
    {"type": "filter", "params": {"column": "x", "operator": "zzz", "value": "a"}},
    {"type": "group_by", "params": {"by": ["x"], "aggregations": [{"func": "sum"}]}},                      # an aggregation without a column
    {"type": "join", "params": {"how": "inner", "left_on": ["id", "x"], "right_on": ["id"], "right": {"source": {"key": "k"}}}},
    {"type": "sort", "params": {"descending": False}},
    {"type": "select", "params": {}},
    {"type": "compute", "params": {"columns": "x"}},
    {"type": "pivot", "params": {"index": ["id"]}},
    {"type": "limit", "params": {"n": "dieci"}},
], ids=lambda op: op["type"])
def test_an_incomplete_node_is_a_422_with_a_code_not_a_500(op):
    from fastapi import HTTPException

    for mode in ("duckdb", "native"):
        extra = {} if mode == "duckdb" else dict(
            mode="native", native={"db_type": "postgresql", "conn": {"host": "h", "port": 5432, "database": "d", "username": "u"}, "pw_env": "P"},
            source_map={"k": {"ident": "__s_0", "macro": "{{ source('s', 'ordini') }}", "columns": ["id", "x"], "types": {"id": "Int64", "x": "String"}, "label": "ordini"}})
        req = _export_request(models=[{"name": "uno", "source": {"key": "k"}, "operations": [op]}], **extra)
        if mode == "native" and op["type"] == "pivot":
            continue                                                      # refused anyway on native, with its own code
        with pytest.raises(HTTPException) as e:
            _zip(req)
        assert e.value.status_code == 422 and e.value.detail["code"] == "step_invalid", (mode, e.value.detail)
        assert e.value.detail["params"]["step"] == op["type"] and e.value.detail["params"]["detail"]


@pytest.mark.parametrize("op", [
    {"type": "filter", "params": {"column": "x", "operator": "eq", "value": "a"}},
    {"type": "group_by", "params": {"by": ["x"], "aggregations": [{"column": "id", "func": "sum"}]}},
    {"type": "compute", "params": {"columns": [{"name": "y", "expr": "id + 1"}]}},
], ids=lambda op: op["type"])
def test_an_error_of_the_exporter_on_a_complete_node_is_not_blamed_on_the_flow(op, monkeypatch):
    """A complete node whose compilation fails for a reason of the exporter's own (a bug): «internal»
    with the error, not «the step is incomplete or not valid: complete it in the editor»."""
    from fastapi import HTTPException

    def failing(*a, **k):
        raise TypeError("a bug of the exporter")
    monkeypatch.setattr(d, "_column_list", failing)
    monkeypatch.setattr(d, "_predicate", failing)
    monkeypatch.setattr(d, "_aggregato", failing)
    monkeypatch.setattr(d, "_expression", failing)
    with pytest.raises(HTTPException) as e:
        _zip(_export_request(models=[{"name": "uno", "source": {"key": "k"}, "operations": [op]}]))
    assert e.value.status_code == 422 and e.value.detail["code"] == "internal", e.value.detail
    assert "a bug of the exporter" in e.value.detail["params"]["detail"]


def test_a_text_that_looks_like_the_exporters_placeholder_stays_as_it_is():
    """The texts written in the warehouse's dialect wait in placeholders until sqlglot is done; one
    the flow's own text spelled out was replaced too (now each export has its own random marker)."""
    ops = [{"type": "compute", "params": {"columns": [{"name": "a", "expr": "concat('__tabularia_t1__()', s)"},
                                                      {"name": "b", "expr": "upper(s)"}]}}]
    for _ in range(2):
        files = _files(_export_request(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"},
                                         models=[{"name": "uno", "source": {"key": "k"}, "operations": ops, "dialect": "clickhouse"}],
                                         source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "s"],
                                                           "types": {"id": "Int64", "s": "String"}, "label": "ordini"}}))
        model = files["models/uno.sql"]
        assert "'__tabularia_t1__()'" in model and "upperUTF8(s)" in model and model.count("upperUTF8") == 1
    # and the zip stays the same, export after export
    assert _zip(_export_request(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"},
                                  models=[{"name": "uno", "source": {"key": "k"}, "operations": ops, "dialect": "clickhouse"}],
                                  source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "s"],
                                                    "types": {"id": "Int64", "s": "String"}, "label": "ordini"}})) == \
        _zip(_export_request(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"},
                               models=[{"name": "uno", "source": {"key": "k"}, "operations": ops, "dialect": "clickhouse"}],
                               source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "s"],
                                                 "types": {"id": "Int64", "s": "String"}, "label": "ordini"}}))


def test_a_lone_surrogate_in_the_flows_text_does_not_break_the_zip():
    """A lone surrogate (half an emoji pasted from a browser) in a description reached README.md and
    INTEGRATION.md as it was: UTF-8 cannot write it, and the export ended in a 500. It becomes U+FFFD."""
    import io
    import zipfile

    for layout in (None, {"package": "folder", "folder": "f"}):
        req = _export_request(flow_description="prima \ud800 dopo", layout=layout,
                                models=[{"name": "uno", "source": {"key": "k"}, "description": "modello \udfff", "operations": []}])
        with zipfile.ZipFile(io.BytesIO(_zip(req))) as z:
            texts = {n: z.read(n).decode("utf-8") for n in z.namelist()}
        leggimi = texts.get("README.md") or texts["INTEGRATION.md"]
        assert "prima \ufffd dopo" in leggimi
        for name, text in texts.items():
            assert not re.search("[\ud800-\udfff]", text), name


# #7 — compute and sql text is read in the dialect of the flow's engine
def _models_with_dialect(dialect, ops):
    return [{"name": "uno", "source": {"key": "k"}, "operations": ops, "dialect": dialect}]


def _types_k():
    return {"k": {"ref": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "n", "d", "s"],
                  "types": {"id": "Int64", "n": "Float64", "d": "Date", "s": "String"}, "label": "ordini"}}


def test_the_model_spec_carries_the_dialect_of_its_free_text():
    from app.api.routes import dbt as route

    assert route.ModelSpec(name="m", dialect="polars").dialect == "polars"
    assert route.ModelSpec(name="m").dialect is None


def test_polars_text_is_kept_and_listed_for_review_in_the_readme_and_the_model():
    ops = [{"type": "compute", "params": {"columns": [{"name": "x", "expr": "CAST(n AS INTEGER)"}]}},
           {"type": "sql", "params": {"query": "SELECT id, x FROM self"}}]
    files = _files(_export_request(source_map=_types_k(), models=_models_with_dialect("polars", ops)))
    model = files["models/uno.sql"]
    assert "(CAST(n AS INTEGER)) AS \"x\"" in model
    assert "-- Written for the Polars engine, review: compute_x, custom_sql." in model
    assert "`uno`" in files["README.md"] and "Polars engine" in files["README.md"]
    # a DuckDB flow's text needs no review
    files = _files(_export_request(source_map=_types_k(), models=_models_with_dialect("duckdb", ops)))
    assert "review" not in files["models/uno.sql"].lower() and "Polars" not in files["README.md"]


def test_clickhouse_text_is_translated_for_the_federated_target_and_runs():
    src = {"k": _pl.DataFrame({"id": [1, 2], "n": [2.5, -7.0], "d": [_dt.date(2024, 3, 9), _dt.date(2023, 12, 31)], "s": ["città", "x"]})}
    ops = [{"type": "compute", "params": {"columns": [{"name": "mese", "expr": "toStartOfMonth(d)"}, {"name": "u", "expr": "if(n > 0, 'pos', 'neg')"}]}},
           {"type": "sql", "params": {"query": "SELECT id, mese, u, countIf(n > 0) OVER () AS c FROM self"}}]
    sql, cols, _ = _export_sql(src, "k", ops, text="clickhouse")
    assert "toStartOfMonth" not in _code(sql) and "countIf" not in _code(sql)
    rows = _duck_with(src).sql(sql).fetchall()
    assert cols == ["id", "mese", "u", "c"] and sorted(r[:3] for r in rows) == [(1, _dt.datetime(2024, 3, 1), "pos"), (2, _dt.datetime(2023, 12, 1), "neg")]
    files = _files(_export_request(source_map=_types_k(), models=_models_with_dialect("clickhouse", ops)))
    assert "Translated from ClickHouse SQL by sqlglot, review: compute_mese, compute_u, custom_sql." in files["models/uno.sql"]


def test_clickhouse_text_duckdb_does_not_run_is_refused_for_the_federated_target():
    from fastapi import HTTPException

    ops = [{"type": "compute", "params": {"columns": [{"name": "anno", "expr": "toYear(d)"}]}}]
    with pytest.raises(HTTPException) as e:
        _zip(_export_request(source_map=_types_k(), models=_models_with_dialect("clickhouse", ops)))
    det = e.value.detail
    assert det["code"] == "text_not_translatable" and det["params"]["step"] == "compute_anno" and det["params"]["dialect"] == "clickhouse"
    assert "toyear" in det["params"]["detail"].lower()


def test_clickhouse_text_is_used_as_it_is_on_the_clickhouse_target():
    ops = [{"type": "compute", "params": {"columns": [{"name": "meta", "expr": "intDiv(id, 2)"}, {"name": "up", "expr": "upper(s)"}]}},
           {"type": "sql", "params": {"query": "SELECT id, meta, up, toYear(d) AS anno FROM self"}}]
    req = _export_request(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "dbt_tabularia"},
                            source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "n", "d", "s"],
                                              "types": {"id": "Int64", "n": "Float64", "d": "Date", "s": "String"}, "label": "ordini"}},
                            models=_models_with_dialect("clickhouse", ops))
    model = _files(req)["models/uno.sql"]
    # as the ClickHouse engine runs it: upper on characters (upperUTF8), not on bytes
    assert "intDiv(id, 2)" in model and "upperUTF8(s)" in model and "toYear(d)" in model and "__tabularia_" not in model
    assert "review" not in model.lower()


def test_duckdb_text_keeps_its_unicode_semantics_on_clickhouse():
    """DuckDB's upper/lower/substring/length/reverse/left work on characters, ClickHouse's on
    bytes: 'città' became 'CITTà'."""
    ops = [{"type": "compute", "params": {"columns": [{"name": "u", "expr": "upper(s)"}, {"name": "p", "expr": "left(s, 4)"},
                                                      {"name": "r", "expr": "reverse(lower(s))"}, {"name": "m", "expr": "substring(s, 2, 3)"}]}},
           {"type": "filter", "params": {"column": "s", "operator": "starts_with", "value": "cit"}}]
    req = _export_request(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "dbt_tabularia"},
                            source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "s"],
                                              "types": {"id": "Int64", "s": "String"}, "label": "ordini"}},
                            models=_models_with_dialect("duckdb", ops))
    model = _code(_files(req)["models/uno.sql"])
    for f in ("upperUTF8(", "leftUTF8(", "reverseUTF8(", "lowerUTF8(", "substringUTF8("):
        assert f in model, f
    assert not re.search(r"\b(UPPER|LOWER|REVERSE|SUBSTRING|LEFT)\(", model)


def test_a_compute_with_a_trailing_comment_does_not_swallow_the_rest_of_the_line():
    src = {"k": _pl.DataFrame({"id": [1, 2], "n": [1.0, 2.0]})}
    ops = [{"type": "compute", "params": {"columns": [{"name": "doppio", "expr": "n * 2 -- double it"}]}}]
    sql, _, _ = _export_sql(src, "k", ops)
    assert sorted(_duck_with(src).sql(sql).fetchall()) == [(1, 1.0, 2.0), (2, 2.0, 4.0)]
    nat, _, _ = _export_sql(src, "k", ops, native=True, dialect="postgres")
    assert "n * 2" in d.transpile_model(nat, "postgres", {"t_k": ["id", "n"]})


def test_bigquery_text_is_translated_for_duckdb():
    src = {"k": _pl.DataFrame({"id": [1, 2], "t": ["7", "x"]})}
    ops = [{"type": "compute", "params": {"columns": [{"name": "v", "expr": "SAFE_CAST(t AS INT64)"}]}}]
    sql, _, _ = _export_sql(src, "k", ops, text="bigquery")
    assert sorted(_duck_with(src).sql(sql).fetchall()) == [(1, "7", 7), (2, "x", None)]


def test_a_sql_node_with_a_pivot_has_columns_the_data_decides():
    """The probe reads empty views: a PIVOT in a sql node would give it no pivot column, and
    the steps after would list only the others. Its columns are not known, as after a pivot node."""
    src = {"vendite": _SALES, "clienti": _CUSTOMERS}
    ops = [{"type": "sql", "params": {"query": "PIVOT self ON anno USING sum(importo) GROUP BY cliente"}},
           {"type": "join", "params": {"how": "left", "on": "cliente", "right": {"source": _key("clienti")}}}]
    _, cols, _ = _export_sql(src, "vendite", ops)
    assert cols == []
    par = _parita(src, "vendite", ops, engines=("duckdb",))
    assert par["export"][:2] == par["duckdb"][:2] and par["duckdb"][0] == ["cliente", "2023", "2024", "nome"]


# ── the first step's types, the native targets' casts and comparisons (E3) ────
_UTC = "Datetime(time_unit='us', time_zone='UTC')"
_PG_NATIVE = {"db_type": "postgresql", "conn": {"host": "h", "port": 5432, "database": "d", "username": "u"}, "pw_env": "P"}


def _export(types: dict, ops: list, mode: str = "duckdb", cols: list | None = None) -> dict:
    """The project the route writes for one model over a source table `orders` whose
    columns Tabularia read with these dtypes (None: a type the export cannot read)."""
    cols = cols or list(types)
    if mode == "duckdb":
        item, extra = {"ref": "{{ source('db_1_v', 'ordini') }}"}, {}
    else:
        item = {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}"}
        extra = {"native": _PG_NATIVE} if mode == "native" else {"clickhouse": {"connections": [_PG], "target_schema": "dbt_tabularia"}}
    return _files(_export_request(mode=mode, **extra, source_map={"k": {**item, "columns": cols, "types": types, "label": "ordini"}},
                                    models=[{"name": "uno", "source": {"key": "k"}, "operations": ops}]))


def _model(files: dict, source: str = "ordini") -> str:
    """The model as dbt renders it, with the source table replaced by `source`."""
    return _render_like_dbt(files["models/uno.sql"], {("source", "db_1_v", "ordini"): source}, config={})


def _pg():
    """A connection to a real Postgres (POSTGRES_TEST_URI), for the native SQL: only SELECTs over VALUES."""
    import os

    uri = os.getenv("POSTGRES_TEST_URI")
    if not uri:
        _missing("postgres", "POSTGRES_TEST_URI")
    import adbc_driver_postgresql.dbapi as pg

    return pg.connect(uri)


def test_a_required_live_server_that_is_missing_fails_the_test_instead_of_skipping_it(monkeypatch):
    monkeypatch.delenv("POSTGRES_TEST_URI", raising=False)
    monkeypatch.delenv("CLICKHOUSE_TEST_HOST", raising=False)
    monkeypatch.setenv("TABULARIA_TEST_REQUIRE", "postgres, clickhouse")
    with pytest.raises(pytest.fail.Exception, match="POSTGRES_TEST_URI"):
        _pg()
    with pytest.raises(pytest.fail.Exception, match="CLICKHOUSE_TEST_HOST"):
        _clickhouse()
    monkeypatch.setenv("TABULARIA_TEST_REQUIRE", "")
    with pytest.raises(pytest.skip.Exception):
        _pg()


def _su_postgres(sql: str, *before: str) -> list:
    with _pg() as with_contract, with_contract.cursor() as cur:
        for s in before:
            cur.execute(s)
        cur.execute(sql)
        return cur.fetchall()


# #1 — a timestamptz source is a UTC instant whatever the time zone of the machine running dbt
def test_a_timestamptz_source_does_not_depend_on_the_local_time_zone():
    """Tabularia reads a Postgres timestamptz as a UTC instant and its engine works in UTC.
    A bare CAST AS TIMESTAMP follows the session's time zone, which dbt-duckdb takes from
    the machine: on a laptop in Rome 23:30 UTC became 01:30 of the next day."""
    import duckdb

    ops = [{"type": "compute", "params": {"columns": [{"name": "giorno", "expr": "CAST(ts AS DATE)"}]}}]
    files = _export({"id": "Int32", "ts": _UTC}, ops)
    with_contract = duckdb.connect()
    with_contract.execute("SET TimeZone = 'Europe/Rome'")
    with_contract.execute("CREATE TABLE ordini AS SELECT * FROM (VALUES (1, TIMESTAMPTZ '2024-01-15 10:30:00+00'), "
                "(2, TIMESTAMPTZ '2024-07-15 23:30:00+00')) t(id, ts)")
    rows = with_contract.execute(f"SELECT * FROM ({_model(files)}) ORDER BY id").fetchall()
    assert rows == [(1, _dt.datetime(2024, 1, 15, 10, 30), _dt.date(2024, 1, 15)), (2, _dt.datetime(2024, 7, 15, 23, 30), _dt.date(2024, 7, 15))]
    # and the project's DuckDB works in UTC, as the engine does, for whatever the flow does with time zones
    assert "      settings:\n        TimeZone: UTC\n" in files["profiles.yml"]
    folder = _project(layout={"package": "folder", "folder": "f"})["INTEGRATION.md"]
    assert "TimeZone: UTC" in folder


def test_a_timestamptz_source_on_native_postgres_is_read_in_utc():
    files = _export({"id": "Int32", "ts": _UTC}, [], mode="native")
    sql = " ".join(_code(files["models/uno.sql"]).split())
    assert "AT TIME ZONE 'UTC'" in sql
    # the ClickHouse target: a DateTime is already an instant, nothing to convert
    assert "AT TIME ZONE" not in _code(_export({"id": "Int32", "ts": _UTC}, [], mode="clickhouse")["models/uno.sql"])
    values = "(SELECT * FROM (VALUES (1, timestamptz '2024-01-15 10:30:00+00'), (2, timestamptz '2024-07-15 23:30:00+00')) AS t(id, ts))"
    rows = _su_postgres(f"SELECT * FROM ({_model(files, values)}) AS m ORDER BY id", "SET TIME ZONE 'Europe/Rome'")
    assert rows == [(1, _dt.datetime(2024, 1, 15, 10, 30)), (2, _dt.datetime(2024, 7, 15, 23, 30))]


def test_a_timestamptz_column_of_a_datasource_query_is_read_in_utc_too():
    raw = [{"name": "src_q", "kind": "raw", "materialized": "ephemeral", "raw_sql": "SELECT id, ts FROM ordini",
            "columns": [{"name": "id", "dtype": "Int32"}, {"name": "ts", "dtype": _UTC}]}]
    fed = _files(_export_request(models=raw, source_map={}))["models/src_q.sql"]
    assert "CAST(\"ts\" AS TIMESTAMPTZ) AT TIME ZONE 'UTC' AS \"ts\"" in fed
    nat = _files(_export_request(models=raw, source_map={}, mode="native", native=_PG_NATIVE))["models/src_q.sql"]
    assert "AT TIME ZONE 'UTC' AS \"ts\"" in nat
    rows = _su_postgres(_render_like_dbt(nat, config={}).replace("FROM ordini", "FROM (VALUES (1, timestamptz '2024-07-15 23:30:00+00')) AS ordini(id, ts)"),
                         "SET TIME ZONE 'Europe/Rome'")
    assert rows == [(1, _dt.datetime(2024, 7, 15, 23, 30))]


# #17 — a native cast checks the VALUE: what the engine turns into NULL is NULL, never a failed
# model (Postgres) nor a made-up value (ClickHouse: 2024-02-30 → 1 March, 1e20 → INT64_MIN)
_DATE_TXT = ["2024-02-29", "2023-02-29", "2024-02-30", "2024-13-01", "2024-00-10", "0000-00-00", "1900-02-29", "2000-02-29",
             " 2024-03-10 ", "2024-04-31", "2024-12-31", "1999-11-30", "x", "", None]
_INSTANTS_TXT = ["2024-01-05 10:30:00", "2024-01-05T10:30:00", "2024-01-05 10:30", "2024-01-05T10:30", "2024-01-05 23:59:59",
                "2024-01-05 25:00:00", "2024-01-05 23:60:00", "2024-01-05 10:61:00", "2024-01-05 10:30:61", "2024-02-30 10:00:00",
                "2023-02-29T00:00:00", "2024-02-29T12:00:00", " 2024-03-10 08:00:00 ", "2024-01-05 10:30:00.1234567",
                "2024-01-05 10:30:00.5", "2024-01-05", "x", None]
_INTEGERS_TXT = ["99999999999999999999", "-9223372036854775808", "9223372036854775807", "9223372036854775808", "-9223372036854775809",
               "+5", "007", " 42 ", "1e3", "12.0", "-0", "", "0000000000000000000009223372036854775807", "x", None]
_NUMBERS = [1e20, -1e20, float("nan"), float("inf"), float("-inf"), 9.223372036854775807e18, -9.223372036854775808e18,
           9.2233720368547748e18, 100.9, -2.7, 0.0, None]
# the shapes both engines (Polars and DuckDB) convert, with the same value, beyond the canonical
# ones: a month or a day of one digit, a year of fewer than four (year 24, not 2024), an instant
# with a 'Z' or an offset (dropped: the wall-clock time stays), an hour, a minute or a second of one
# digit, 'inf', 'nan', a number beyond the double range (±inf, ±0). Native gave NULL, '1e400' failed
# the whole Postgres model. Where the engines disagree ('2024/01/01', '24:00:00', '1_000.5') the
# native targets give NULL, as one of them does.
_DATE_TXT_SHORT = ["2024-1-1", "2024-01-5", "2024-1-05", "2024-12-1", " 2024-1-1 ", "2024-2-29", "2023-2-29", "2024-4-31", "2024-1-32",
                   "2024-13-1", "2024-0-1", "1999-9-9"]
_DATE_TXT_YEARS = ["24-01-05", "4-1-5", "024-2-29", "100-2-29", "400-2-29", "0024-01-05", "1899-12-31", "2300-01-01"]
_INSTANTS_TXT_SHORT = ["2024-01-05 10:30:00Z", "2024-01-05T10:30:00Z", "2024-01-05 10:30:00+02", "2024-01-05 10:30:00+02:00",
                      "2024-01-05 10:30:00-0530", "2024-01-05 10:30:00-05:30", "2024-01-05 10:30:00.5Z", "2024-01-05 10:30:00.1234567+01:00",
                      "2024-01-05 10:30:00+25", "2024-1-5T10:30:00", "2024-01-05 1:30", "2024-01-05 10:3", "2024-01-05 1:3:5",
                      "2024-01-05 10:30:5", "2024-01-05 10:30:00.", "2024-01-05T10:30:05.Z", "2023-2-29 10:00", "2024-01-05Z",
                      "2024-01-05 10:60", "2024-01-05T", "2024-01-05 10"]
# where the two engines disagree, the native targets give what the DuckDB engine (the federated
# target) gives: a value for a one-digit month or day before a space, NULL for a 'Z' after the
# minutes, a 'z', an offset after a space or of one digit, a 60th second
_INSTANTS_TXT_DUCKDB = ["2024-1-5 10:30", "2024-01-5 10:30:00", "2024-2-29 23:59:59", " 2024-1-5 10:30 ", "2024-01-05 10:30Z",
                       "2024-01-05 10:30:00 +02:00", "2024-01-05 10:30:00z", "2024-01-05 23:59:60", "2024-01-05 10:30:00+2"]
_INSTANTS_TXT_YEARS = ["24-01-05T10:30", "0024-01-05 10:30:00", "1899-12-31 23:59:59", "2300-01-01 00:00:00"]
_DOUBLES_TXT = ["inf", "-inf", "+inf", "Inf", "INF", "infinity", "-Infinity", "+Infinity", "nan", "NaN", "-nan", "+nan", " nan ",
              "1e400", "-1e400", "1e-400", "-1e-400", "1.8e308", "-1.8e308", "1.7976931348623157e308", "2e-324",
              "1e99999", "-1e99999", "1e-99999", "0e99999", "-0.00e99999", "0e-99999", ".0e5", "1e00005", "-2.5e-00007",
              "1e+0000000000400", "1E+400", " .5", "1.", "-.5e3", "+.5", "-0", "+1e+3", "1e", "1d", "0x10",
              "inf5", "nan1", "x", "", None]
# a subnormal double: ClickHouse's float parsing (the ClickHouse engine's too) reads it as 0
_SUBNORMAL_DOUBLES_TXT = ["4.9e-324", "1e-310", "-2.5e-320"]
_BOOLEANS_TXT = ["true", "TRUE", "True", "t", "T", "1", "yes", "YES", "y", "Y", " yes ", "false", "FALSE", "f", "F", "0", "no", "No",
                 "n", "N", "on", "off", "2", "tru", "ye", "-1", "01", "", "x", None]
_CAST_CASES = [("VARCHAR", "date", _DATE_TXT), ("VARCHAR", "datetime", _INSTANTS_TXT), ("VARCHAR", "int", _INTEGERS_TXT),
              ("DOUBLE", "int", _NUMBERS), ("VARCHAR", "date", _DATE_TXT_SHORT), ("VARCHAR", "datetime", _INSTANTS_TXT_SHORT),
              ("VARCHAR", "datetime", _INSTANTS_TXT_DUCKDB), ("VARCHAR", "float", _DOUBLES_TXT), ("VARCHAR", "bool", _BOOLEANS_TXT)]
# what ClickHouse cannot hold (Date32 and DateTime64 span 1900-2299; a subnormal double): Postgres keeps it
_CAST_CASES_BEYOND_CLICKHOUSE = [("VARCHAR", "date", _DATE_TXT_YEARS), ("VARCHAR", "datetime", _INSTANTS_TXT_YEARS),
                   ("VARCHAR", "float", _SUBNORMAL_DOUBLES_TXT)]


def _reference_cast(src: str, dt: str, values: list) -> list:
    """What the DuckDB engine (the federated target's own cast) gives."""
    import duckdb

    from app.engine.duckdb_ops import _DUCK_DTYPES, _cast_expr

    with_contract = duckdb.connect()
    with_contract.execute("SET TimeZone = 'UTC'")
    with_contract.execute(f"CREATE TABLE t_casi (i BIGINT, v {src})")
    with_contract.executemany("INSERT INTO t_casi VALUES (?, ?)", list(enumerate(values)))
    return with_contract.execute(f"SELECT i, {_cast_expr('v', src, _DUCK_DTYPES[dt])} FROM t_casi ORDER BY i").fetchall()


def _native_cast(src: str, dt: str, dialect: str, info: dict | None = None) -> str:
    def resolve(_):
        return "t_casi", ["i", "v"], {"i": "BIGINT", "v": src}, "casi"
    sql, _, _ = d.compile_model_sql({"key": "casi"}, [{"type": "cast", "params": {"columns": {"v": dt}}}], resolve,
                                    native=True, dialect=dialect, info=info)
    return sql


def _native_cast_in_duckdb(src: str, dt: str, dialect: str) -> str:
    """The native cast with the text written for the warehouse (see `_Model.native_text`) in its
    DuckDB form, the one the probe reads."""
    info: dict = {}
    sql = _native_cast(src, dt, dialect, info)
    return d.put_back_texts(sql, {"segnaposti": info.get("per_sonda")})


def _equal_to_values(got: list, exp: list, values: list, ctx: str):
    for (i, a), (_, b) in zip(sorted(got), exp):
        equal = (a == b) or (isinstance(a, float) and isinstance(b, float) and a != a and b != b)
        assert equal, f"{ctx}: {values[i]!r} gives {a!r}, the engine {b!r}"
    assert len(got) == len(exp), ctx


def _cast_in_duckdb(src: str, dt: str, values: list, dialects=("postgres", "clickhouse")):
    """The native guards run in DuckDB, which fails a CAST of '2024-02-30' or of 1e20 to BIGINT
    as Postgres does: a value a guard lets through that the warehouse cannot convert fails here."""
    import duckdb

    rif = _reference_cast(src, dt, values)
    with_contract = duckdb.connect()
    with_contract.execute(f"CREATE TABLE t_casi (i BIGINT, v {src})")
    with_contract.executemany("INSERT INTO t_casi VALUES (?, ?)", list(enumerate(values)))
    for dialect in dialects:
        got = with_contract.execute(f"SELECT * FROM ({_native_cast_in_duckdb(src, dt, dialect)}) ORDER BY i").fetchall()
        _equal_to_values(got, rif, values, dialect)


_PG_TYPE = {"VARCHAR": "TEXT", "DOUBLE": "DOUBLE PRECISION", "BIGINT": "BIGINT", "BOOLEAN": "BOOLEAN"}


def _pg_literal(v, src: str) -> str:
    if v is None:
        return f"CAST(NULL AS {_PG_TYPE[src]})"
    if isinstance(v, float):
        return f"CAST('{'NaN' if v != v else ('Infinity' if v == float('inf') else ('-Infinity' if v == float('-inf') else repr(v)))}' AS DOUBLE PRECISION)"
    if isinstance(v, (bool, int)):
        return f"CAST({str(v).upper() if isinstance(v, bool) else v} AS {_PG_TYPE[src]})"
    return "'" + str(v).replace("'", "''") + "'"


def _cast_su_postgres(src: str, dt: str, values: list):
    rif = _reference_cast(src, dt, values)
    info: dict = {}
    pg = d.put_back_texts(d.transpile_model(_native_cast(src, dt, "postgres", info), "postgres", {"t_casi": ["i", "v"]}), info)
    rows = ", ".join(f"({i}, {_pg_literal(v, src)})" for i, v in enumerate(values))
    pg = pg.replace('"t_casi" AS "t_casi"', f'(VALUES {rows}) AS "t_casi"("i", "v")')
    _equal_to_values(_su_postgres(pg), rif, values, "postgres")


def _clickhouse():
    import os

    import clickhouse_connect

    if not os.getenv("CLICKHOUSE_TEST_HOST"):
        _missing("clickhouse", "CLICKHOUSE_TEST_HOST")
    return clickhouse_connect.get_client(host=os.environ["CLICKHOUSE_TEST_HOST"], port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8123")),
                                         username=os.getenv("CLICKHOUSE_TEST_USER", "default"), password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""))


_CH_TYPE = {"VARCHAR": "Nullable(String)", "DOUBLE": "Nullable(Float64)", "BIGINT": "Nullable(Int64)", "BOOLEAN": "Nullable(Bool)"}


def _cast_su_clickhouse(src: str, dt: str, values: list):
    cli = _clickhouse()
    rif = _reference_cast(src, dt, values)
    info: dict = {}
    ch = d.put_back_texts(d.transpile_model(_native_cast(src, dt, "clickhouse", info), "clickhouse", {"t_casi": ["i", "v"]}), info)

    def lit(v):
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return str(v).lower()
        if isinstance(v, float):
            return "nan" if v != v else ("inf" if v == float("inf") else ("-inf" if v == float("-inf") else repr(v)))
        if isinstance(v, int):
            return str(v)
        return "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"
    rows = ", ".join(f"({i}, {lit(v)})" for i, v in enumerate(values))
    ch = ch.replace('"t_casi" AS "t_casi"', f"(SELECT * FROM values('i Int64, v {_CH_TYPE[src]}', {rows})) AS \"t_casi\"")
    _equal_to_values([tuple(r) for r in cli.query(ch).result_rows], rif, values, "clickhouse")


@pytest.mark.parametrize("src,dt,values", _CAST_CASES, ids=lambda x: x if isinstance(x, str) else None)
def test_a_native_cast_never_fails_the_model_and_gives_the_engines_nulls(src, dt, values):
    _cast_in_duckdb(src, dt, values)


@pytest.mark.parametrize("src,dt,values", _CAST_CASES, ids=lambda x: x if isinstance(x, str) else None)
def test_a_native_cast_on_postgres_gives_what_the_engine_gives(src, dt, values):
    _cast_su_postgres(src, dt, values)


@pytest.mark.parametrize("src,dt,values", _CAST_CASES, ids=lambda x: x if isinstance(x, str) else None)
def test_a_native_cast_on_clickhouse_gives_what_the_engine_gives(src, dt, values):
    _cast_su_clickhouse(src, dt, values)


@pytest.mark.parametrize("src,dt,values", _CAST_CASES_BEYOND_CLICKHOUSE, ids=lambda x: x if isinstance(x, str) else None)
def test_a_native_cast_on_postgres_keeps_what_clickhouse_cannot_hold(src, dt, values):
    _cast_in_duckdb(src, dt, values, dialects=("postgres",))
    _cast_su_postgres(src, dt, values)


@pytest.mark.parametrize("src,dt,values", [c for c in _CAST_CASES_BEYOND_CLICKHOUSE if c[1] != "float"],
                         ids=lambda x: x if isinstance(x, str) else None)
def test_a_native_cast_on_clickhouse_gives_null_for_a_year_it_cannot_hold(src, dt, values):
    """ClickHouse moved 1899-12-31 to 1900-01-01 and year 24 to 1900: a made-up value. NULL instead,
    as the ClickHouse engine gives; a year it can hold keeps its value. (A subnormal double is a
    limit of ClickHouse's float parsing, not of the export: not here.)"""
    cli = _clickhouse()
    info: dict = {}
    ch = d.put_back_texts(d.transpile_model(_native_cast(src, dt, "clickhouse", info), "clickhouse", {"t_casi": ["i", "v"]}), info)
    rows = ", ".join(f"({i}, '{v}')" for i, v in enumerate(values))
    got = dict(cli.query(ch.replace('"t_casi" AS "t_casi"', f"(SELECT * FROM values('i Int64, v Nullable(String)', {rows})) AS \"t_casi\"")).result_rows)
    assert all(v is None for v in got.values()), got
    within = ["1900-01-01", "2299-12-31"] if dt == "date" else ["1900-01-01 00:00:00", "2299-12-31 23:59:59"]
    _cast_su_clickhouse(src, dt, within)


def _writings_of_dates_and_instants() -> dict:
    """Many writings of days and instants: the shapes the native guards were measured on."""
    days = [f"{a}-{m}-{g}" for a in ("2024", "24", "4", "024", "0024", "02024", "1900", "2000", "2100") for m in ("1", "01", "2", "02", "001", "13")
              for g in ("5", "05", "005", "29", "31")]
    times = ("10:30", "1:3", "9:05", "10:30:5", "10:30:00", "10:30:00.", "10:30:00.5", "10:30:00.1234567", "24:00", "23:59:60", "10:60", "10")
    zones = ("", "Z", "z", "+02", "+2", "+0200", "+02:00", "-05:30", " +02:00", "+02:00:00", "+020", "UTC", " Z")
    instants = [f"{g}{sep}{o}{f}" for g in ("2024-01-05", "2024-1-5", "24-01-05", "2024-02-30") for sep in (" ", "T", "  ", "t", "\t", " \n", "\r", "\u00a0")
               for o in times for f in zones] + [f"2024-01-05{f}" for f in zones]
    return {"date": days, "datetime": instants}


def _native_cast_of(where: str, dialect: str, src: str, dt: str, values: list) -> list:
    """The values the native cast gives, run in DuckDB (the warehouse's SQL in its DuckDB form)
    or in the warehouse itself (`dove`)."""
    if where == "duckdb":
        import duckdb

        with_contract = duckdb.connect()
        with_contract.execute(f"CREATE TABLE t_casi (i BIGINT, v {src})")
        with_contract.executemany("INSERT INTO t_casi VALUES (?, ?)", list(enumerate(values)))
        return [v for _, v in with_contract.execute(f"SELECT * FROM ({_native_cast_in_duckdb(src, dt, dialect)}) ORDER BY i").fetchall()]
    info: dict = {}
    sql = d.put_back_texts(d.transpile_model(_native_cast(src, dt, dialect, info), dialect, {"t_casi": ["i", "v"]}), info)
    if dialect == "postgres":
        rows = ", ".join(f"({i}, {_pg_literal(v, src)})" for i, v in enumerate(values))
        return [v for _, v in sorted(_su_postgres(sql.replace('"t_casi" AS "t_casi"', f'(VALUES {rows}) AS "t_casi"("i", "v")')))]
    cli = _clickhouse()
    lit = lambda v: "NULL" if v is None else "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"   # noqa: E731
    rows = ", ".join(f"({i}, {lit(v)})" for i, v in enumerate(values))
    sql = sql.replace('"t_casi" AS "t_casi"', f"(SELECT * FROM values('i Int64, v {_CH_TYPE[src]}', {rows})) AS \"t_casi\"")
    return [v for _, v in sorted(tuple(r) for r in cli.query(sql).result_rows)]


@pytest.mark.parametrize("where,dialect", [("duckdb", "postgres"), ("duckdb", "clickhouse"), ("postgres", "postgres"), ("clickhouse", "clickhouse")])
@pytest.mark.parametrize("dt", ["date", "datetime"])
def test_a_native_cast_of_any_writing_gives_the_engines_value_or_null(dt, where, dialect):
    """Over hundreds of writings: wherever the native cast gives a value it is the DuckDB engine's,
    and every value the two engines agree on is given (on ClickHouse, in the years it holds)."""
    from app.engine.operations import op_cast

    values = _writings_of_dates_and_instants()[dt]
    rif = [v for _, v in _reference_cast("VARCHAR", dt, values)]
    polars = op_cast(_pl.DataFrame({"v": values}, schema={"v": _pl.String}).lazy(), {"columns": {"v": dt}}, None).collect()["v"].cast(_pl.String).to_list()
    got = _native_cast_of(where, dialect, "VARCHAR", dt, values)
    assert len(got) == len(values)
    for v, n, r, p in zip(values, got, rif, polars):
        assert n is None or n == r, (dialect, v, n, r)
        within = dialect != "clickhouse" or (r is not None and 1900 <= r.year <= 2299)
        if r is not None and p is not None and _norm(r) == _norm(_dt.datetime.fromisoformat(p) if dt == "datetime" else _dt.date.fromisoformat(p)) and within:
            assert n == r, (dialect, v, "both engines give", r, "native", n)


def test_the_native_day_pattern_is_the_calendar():
    """`_RE_DATA` with a year of one to four digits and a month or day of one or two is exactly the
    calendar (leap years included) the engines read; every other writing is refused."""
    import calendar

    rule = re.compile(d._RE_DATA)
    for year in range(0, 10001):
        for written in {str(year), str(year).zfill(4), str(year).zfill(2), str(year).zfill(3)}:
            for month, day in ((2, 29), (2, 28), (12, 31), (4, 31), (1, 1)):
                expected = 1 <= year <= 9999 and len(written) <= 4 and day <= (calendar.monthrange(year, month)[1] if 1 <= year <= 9999 else 0)
                for m, g in {(str(month), str(day)), (str(month).zfill(2), str(day).zfill(2))}:
                    assert bool(rule.match(f"{written}-{m}-{g}")) == expected, f"{written}-{m}-{g}"
    for month in range(0, 14):
        for day in range(0, 33):
            expected = 1 <= month <= 12 and 1 <= day <= calendar.monthrange(2023, month)[1] if 1 <= month <= 12 else False
            for m in {str(month), str(month).zfill(2), str(month).zfill(3)}:
                for g in {str(day), str(day).zfill(2), str(day).zfill(3)}:
                    assert bool(rule.match(f"2023-{m}-{g}")) == (expected and len(m) <= 2 and len(g) <= 2), f"2023-{m}-{g}"


def test_the_new_cast_values_are_ones_both_engines_agree_on():
    """The values above that the native targets now convert are converted alike by the Polars and
    the DuckDB engines (the reference of these tests is DuckDB's): never one engine's whim."""
    from app.engine.operations import op_cast

    for dt, values in (("date", _DATE_TXT_SHORT + _DATE_TXT_YEARS), ("datetime", _INSTANTS_TXT_SHORT + _INSTANTS_TXT_YEARS), ("float", _DOUBLES_TXT + _SUBNORMAL_DOUBLES_TXT)):
        rif = [v for _, v in _reference_cast("VARCHAR", dt, values)]
        polars = op_cast(_pl.DataFrame({"v": values}, schema={"v": _pl.String}).lazy(), {"columns": {"v": dt}}, None).collect()["v"].to_list()
        for v, a, b in zip(values, polars, rif):
            assert a == b or (isinstance(a, float) and isinstance(b, float) and a != a and b != b), (dt, v, a, b)


# #12 #13 #16 #23 — no native target on MySQL/MariaDB (decision of 2026-10-16): the only dbt-mysql
# release needs dbt-core 1.7, which drops the project's tests; MySQL stays a source of the others
@pytest.mark.parametrize("db_type", ["mysql", "mariadb"])
def test_the_native_target_refuses_mysql_and_mariadb(db_type):
    from fastapi import HTTPException

    conn = {"host": "my.x", "port": 3306, "database": "shop", "username": "web"}
    req = _export_request(mode="native", native={"db_type": db_type, "conn": conn, "pw_env": "P"},
                            source_map={"k": {"ident": "__s_0", "macro": "{{ source('s', 'ordini') }}", "columns": ["id", "x"],
                                              "types": {"id": "Int64", "x": "String"}, "label": "ordini"}})
    with pytest.raises(HTTPException) as e:
        _zip(req)
    assert e.value.status_code == 422 and e.value.detail["code"] == "no_native_adapter" and e.value.detail["params"] == {"db_type": db_type}
    with pytest.raises(d.DbtExportError) as e2:
        _project(attachments=None, native={"db_type": db_type, "conn": conn, "pw_env": "P", "target_schema": "s"})
    assert e2.value.code == "no_native_adapter"
    # still a source: attached by the federated target, read by a ClickHouse database engine
    assert "type: mysql" in _project(attachments=[{"alias": "db_2", "db_type": db_type, "pw_env": "P", "conn": conn}])["profiles.yml"]
    assert d.TargetClickHouse([{**_MY, "db_type": db_type}], "ch").database_per(2, "shop") == "tab_src_2_shop"


# #6 — a column of a type the export cannot write (an array, a uuid, bytea) is passed through as
# it is: it no longer switches off the casts of all the other columns
_WITH_A_LIST = {"id": "Int32", "importo": "Float64", "quando": _UTC, "tags": "List(String)", "u": "Binary"}


def test_a_column_of_an_unknown_type_does_not_drop_the_casts_of_the_others():
    import duckdb

    ops = [{"type": "cast", "params": {"columns": {"importo": "str"}}}]
    files = _export(_WITH_A_LIST, ops)
    with_contract = duckdb.connect()
    with_contract.execute("SET TimeZone = 'Europe/Rome'")
    with_contract.execute("CREATE TABLE ordini AS SELECT 1 AS id, 1.50::DECIMAL(10,2) AS importo, TIMESTAMPTZ '2024-07-15 23:30:00+00' AS quando, "
                "['a', 'b'] AS tags, '5f0c6c2e-8a8f-4f39-9c7e-2f1b9d0a4e11'::UUID AS u")
    rel = with_contract.sql(_model(files))
    assert rel.columns == ["id", "importo", "quando", "tags", "u"]
    # a numeric of the warehouse is Tabularia's double ('1.5', not '1.50'), the timestamptz its UTC instant
    assert rel.fetchall() == [(1, "1.5", _dt.datetime(2024, 7, 15, 23, 30), ["a", "b"], __import__("uuid").UUID("5f0c6c2e-8a8f-4f39-9c7e-2f1b9d0a4e11"))]
    assert [str(t) for t in rel.types] == ["INTEGER", "VARCHAR", "TIMESTAMP", "VARCHAR[]", "UUID"]
    # native: the same, the unknown columns listed as they are
    nat = " ".join(_code(_export(_WITH_A_LIST, [], mode="native")["models/uno.sql"]).split())
    assert 'CAST("__s_0"."importo" AS DOUBLE PRECISION) AS "importo"' in nat and '"__s_0"."tags" AS "tags"' in nat
    # and a datasource query
    raw = [{"name": "src_q", "kind": "raw", "materialized": "ephemeral", "raw_sql": "SELECT * FROM ordini",
            "columns": [{"name": c, "dtype": t} for c, t in _WITH_A_LIST.items()]}]
    q = _files(_export_request(models=raw, source_map={}))["models/src_q.sql"]
    assert 'CAST("importo" AS DOUBLE) AS "importo"' in q and '"tags" AS "tags"' in q


# #82 — two columns whose names differ only by case: DuckDB (the federated target) resolves both
# to the first one and renames the second `codice_1`, with the first one's values
def test_columns_that_differ_only_by_case_are_refused_on_the_federated_target():
    from fastapi import HTTPException

    types = {"id": "Int32", "Codice": "String", "codice": "String"}
    with pytest.raises(HTTPException) as e:
        _export(types, [])
    assert e.value.detail["code"] == "columns_differ_only_by_case" and e.value.detail["params"]["columns"] == "Codice, codice"
    # a step can make the clash too: a compute, a rename, the right side of a join
    steps = [[{"type": "compute", "params": {"columns": [{"name": "Importo", "expr": "importo * 2"}]}}],
             [{"type": "rename", "params": {"mapping": {"id": "IMPORTO"}}}],
             [{"type": "join", "params": {"how": "inner", "on": "id", "right": {"source": {"key": "k"}, "operations": [
                 {"type": "rename", "params": {"mapping": {"importo": "Importo"}}}]}}}]]
    for ops in steps:
        with pytest.raises(HTTPException) as e:
            _export({"id": "Int32", "importo": "Float64"}, ops)
        assert e.value.detail["code"] == "columns_differ_only_by_case", ops
        assert e.value.detail["params"]["columns"] in ("IMPORTO, importo", "Importo, importo", "importo, Importo"), e.value.detail
    # Postgres and ClickHouse compare names with their case: there they stay two columns, with their own names
    for mode in ("native", "clickhouse"):
        sql = _code(_export(types, [{"type": "select", "params": {"columns": ["codice", "Codice"]}},
                                      {"type": "filter", "params": {"column": "codice", "operator": "eq", "value": "a1"}}], mode=mode)["models/uno.sql"])
        assert '"codice"' in sql and '"Codice"' in sql and "codice_1" not in sql.lower(), mode


def test_native_postgres_keeps_columns_that_differ_only_by_case():
    files = _export({"id": "Int32", "Codice": "String", "codice": "String"}, [{"type": "filter", "params": {"column": "codice", "operator": "eq", "value": "d4"}}],
                     mode="native")
    values = '(SELECT * FROM (VALUES (1, \'A1\', \'a1\'), (4, NULL, \'d4\')) AS t(id, "Codice", codice))'
    assert _su_postgres(f"SELECT * FROM ({_model(files, values)}) AS m") == [(4, None, "d4")]


# #28 — an unsigned 64-bit column (ClickHouse hash keys, MySQL BIGINT UNSIGNED) keeps its values:
# cast to Int64 in the first step, ClickHouse wrapped 2^64-1 to -1 without an error
_U64 = [18446744073709551615, 9223372036854775808, 9223372036854775807, 1]


def test_an_unsigned_64_bit_column_keeps_its_values_in_the_federated_model():
    import duckdb

    assert d.duck_type("UInt64") == "UBIGINT" and d.duck_type("UInt32") == "BIGINT"
    files = _export({"id": "UInt64", "n": "String"}, [{"type": "filter", "params": {"column": "id", "operator": "ge", "value": 2}}])
    with_contract = duckdb.connect()
    with_contract.execute("CREATE TABLE ordini (id UBIGINT, n VARCHAR)")
    with_contract.executemany("INSERT INTO ordini VALUES (?, ?)", [(v, str(v)) for v in _U64])
    rel = with_contract.sql(_model(files))
    assert sorted(r[0] for r in rel.fetchall()) == sorted(_U64[:3]) and str(rel.types[0]) == "UBIGINT"


@_WITH_CLICKHOUSE
def test_an_unsigned_64_bit_column_keeps_its_values_on_clickhouse():
    import os

    import clickhouse_connect

    ops = [{"type": "filter", "params": {"column": "id", "operator": "ge", "value": 2}},
           {"type": "cast", "params": {"columns": {"big": "int"}}}]
    sql = _model(_export({"id": "UInt64", "big": "UInt64"}, ops, mode="clickhouse"),
                   "(SELECT * FROM values('id UInt64, big UInt64', " + ", ".join(f"({v}, {v})" for v in _U64) + "))")
    cli = clickhouse_connect.get_client(host=os.environ["CLICKHOUSE_TEST_HOST"], port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8123")),
                                        username=os.getenv("CLICKHOUSE_TEST_USER", "default"), password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""))
    rows = sorted(cli.query(sql).result_rows)
    # an id over 2^63 stays itself; cast to a signed integer it does not fit: NULL, as the engine (never -1)
    assert rows == [(9223372036854775807, 9223372036854775807), (9223372036854775808, None), (18446744073709551615, None)]


def test_an_unsigned_64_bit_column_on_native_postgres_is_a_numeric():
    from app.api.routes.dbt import _type_in_dialect

    # a seed's column type too
    assert _type_in_dialect("UBIGINT", "postgres") == "DECIMAL(20, 0)" and _type_in_dialect("UBIGINT", "clickhouse") == "Nullable(UInt64)"
    files = _export({"id": "UInt64", "big": "UInt64"}, [{"type": "cast", "params": {"columns": {"big": "int"}}}], mode="native")
    values = "(SELECT * FROM (VALUES " + ", ".join(f"({v}::numeric, {v}::numeric)" for v in _U64) + ") AS t(id, big))"
    rows = sorted((int(a), b) for a, b in _su_postgres(f"SELECT * FROM ({_model(files, values)}) AS m"))   # numeric: text from the driver
    assert rows == [(1, 1), (9223372036854775807, 9223372036854775807), (9223372036854775808, None),
                                              (18446744073709551615, None)]


# #18 — a float cast to text is written as Tabularia writes it: 200.0 → '200.0', not '200'
_DOUBLES = [200.0, -30.0, 0.1, 1e20, None, 2.5, 100.5, 1e15, 123456789.0, 9999999999999998.0, 1e16, float("nan"),
          float("inf"), float("-inf"), 1e-05, 0.0001, -0.0, 0.0, 1.5e-07, 123456.789, -1234567.5]


def test_a_float_cast_to_text_on_native_postgres_is_written_as_the_engine_writes_it():
    import duckdb

    rif = _reference_cast("DOUBLE", "str", _DOUBLES)
    assert rif[0][1] == "200.0" and rif[7][1] == "1000000000000000.0"
    with_contract = duckdb.connect()
    with_contract.execute("CREATE TABLE t_casi (i BIGINT, v DOUBLE)")
    with_contract.executemany("INSERT INTO t_casi VALUES (?, ?)", list(enumerate(_DOUBLES)))
    _equal_to_values(with_contract.execute(f"SELECT * FROM ({_native_cast('DOUBLE', 'str', 'postgres')}) ORDER BY i").fetchall(), rif, _DOUBLES, "duckdb")
    pg = d.transpile_model(_native_cast("DOUBLE", "str", "postgres"), "postgres", {"t_casi": ["i", "v"]})
    rows = ", ".join(f"({i}, {_pg_literal(v, 'DOUBLE')})" for i, v in enumerate(_DOUBLES))
    pg = pg.replace('"t_casi" AS "t_casi"', f'(VALUES {rows}) AS "t_casi"("i", "v")')
    _equal_to_values(_su_postgres(pg), rif, _DOUBLES, "postgres")


@_WITH_CLICKHOUSE
def test_a_float_cast_to_text_on_clickhouse_is_written_as_the_clickhouse_engine_writes_it():
    import os

    import clickhouse_connect

    from app.engine.chdb_ops import _cast_expr as cast_ch

    info: dict = {}
    ch = d.put_back_texts(d.transpile_model(_native_cast("DOUBLE", "str", "clickhouse", info), "clickhouse", {"t_casi": ["i", "v"]}), info)
    lit = lambda v: "NULL" if v is None else ("nan" if v != v else ("inf" if v == float("inf") else ("-inf" if v == float("-inf") else repr(v))))
    values = "(SELECT * FROM values('i Int64, v Nullable(Float64)', " + ", ".join(f"({i}, {lit(v)})" for i, v in enumerate(_DOUBLES)) + "))"
    cli = clickhouse_connect.get_client(host=os.environ["CLICKHOUSE_TEST_HOST"], port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8123")),
                                        username=os.getenv("CLICKHOUSE_TEST_USER", "default"), password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""))
    got = sorted(tuple(r) for r in cli.query(ch.replace('"t_casi" AS "t_casi"', f'{values} AS "t_casi"')).result_rows)
    rif = sorted(tuple(r) for r in cli.query(f"SELECT i, {cast_ch('v', 'Float64', 'str')} FROM {values}").result_rows)
    assert dict(rif)[0] == "200.0"
    _equal_to_values(got, rif, _DOUBLES, "clickhouse")


# #14 — Postgres has no cast between boolean and bigint/double precision, nor min/max of a boolean:
# the model failed at `dbt run` where Tabularia gives 1/0, true/false
_BOOL_CASES = [("BOOLEAN", "int", [True, False, None]), ("BOOLEAN", "float", [True, False, None]),
              ("BIGINT", "bool", [5, 0, -3, None]), ("DOUBLE", "bool", [2.5, 0.0, -0.0, float("nan"), float("inf"), None])]


@pytest.mark.parametrize("src,dt,values", _BOOL_CASES, ids=lambda x: x if isinstance(x, str) else None)
def test_a_cast_between_boolean_and_number_runs_on_native_postgres(src, dt, values):
    _cast_in_duckdb(src, dt, values)
    _cast_su_postgres(src, dt, values)


@pytest.mark.parametrize("src,dt,values", _BOOL_CASES, ids=lambda x: x if isinstance(x, str) else None)
def test_a_cast_between_boolean_and_number_runs_on_clickhouse(src, dt, values):
    _cast_su_clickhouse(src, dt, values)


def _native_on(where: str, df, ops: list) -> list:
    """The native model of `ops` over `df` (as the source `t`), run on a real Postgres or ClickHouse:
    its rows, normalised and sorted as `_engine_snapshot` gives them."""
    info: dict = {}
    raw, _, _ = _export_sql({"t": df}, "t", ops, native=True, dialect=where, info=info)
    sql = d.put_back_texts(d.transpile_model(raw, where, {"t_t": list(df.columns)}), info)
    pg = {"BIGINT": "BIGINT", "DOUBLE": "DOUBLE PRECISION", "VARCHAR": "TEXT", "BOOLEAN": "BOOLEAN", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP"}
    ch = {"BIGINT": "Int64", "DOUBLE": "Float64", "VARCHAR": "String", "BOOLEAN": "Bool", "DATE": "Date32", "TIMESTAMP": "DateTime64(6)"}
    types = {c: d.duck_type(str(t)) for c, t in df.schema.items()}
    rows = df.rows()
    if where == "postgres":
        lit = lambda v, t: f"CAST(NULL AS {pg.get(t, t)})" if v is None else f"CAST('{v}' AS {pg.get(t, t)})"   # noqa: E731
        values = ", ".join("(" + ", ".join(lit(v, types[c]) for v, c in zip(r, df.columns)) + ")" for r in rows)
        names = ", ".join(f'"{c}"' for c in df.columns)
        out = _su_postgres(sql.replace('"t_t" AS "t_t"', f'(VALUES {values}) AS "t_t"({names})'))
    else:
        lit = lambda v: "NULL" if v is None else (str(v).lower() if isinstance(v, bool) else f"'{v}'")   # noqa: E731
        schema = ", ".join(f"{c} Nullable({ch.get(types[c], types[c])})" for c in df.columns)
        values = ", ".join("(" + ", ".join(lit(v) for v in r) + ")" for r in rows)
        out = [tuple(r) for r in _clickhouse().query(sql.replace('"t_t" AS "t_t"', f"(SELECT * FROM values('{schema}', {values})) AS \"t_t\"")).result_rows]
    return sorted((tuple(_norm(v) for v in r) for r in out), key=lambda t: tuple(map(str, t)))


# DuckDB's date-part functions (year(d), month(d)…) do not exist in Postgres, and sqlglot kept them:
# the native model failed at `dbt run`. They are EXTRACT there, an integer as in DuckDB
_PARTS = ["year", "month", "day", "dayofmonth", "dayofweek", "isodow", "dayofyear", "quarter", "week", "weekofyear", "hour", "minute", "second"]


def test_date_parts_of_duckdb_text_run_on_native_postgres():
    df = _pl.DataFrame({"id": [1, 2, 3], "t": [_dt.datetime(2024, 1, 7, 10, 30, 59, 900000), _dt.datetime(1960, 12, 31, 23, 5, 1),
                                                 _dt.datetime(2021, 1, 1, 0, 0, 0)]})
    ops = [{"type": "compute", "params": {"columns": [{"name": p, "expr": f"{p}(t)"} for p in _PARTS]}}]
    rif = _engine_snapshot("duckdb", {"t": df}, "t", ops)
    assert rif[1][1][2:] == (1960.0, 12.0, 31.0, 31.0, 6.0, 6.0, 366.0, 4.0, 52.0, 52.0, 23.0, 5.0, 1.0)
    assert _native_on("postgres", df, ops) == rif[1]


def test_the_readme_of_a_native_postgres_project_says_its_sql_was_not_run_on_postgres():
    """The ClickHouse SQL is parsed by a ClickHouse at export time; the Postgres SQL is not checked
    by any Postgres: the team runs it first, and the README says so."""
    native = {"db_type": "postgresql", "conn": {"host": "h", "port": 5432, "database": "d", "username": "u"}, "pw_env": "P",
              "target_schema": "s"}
    leggimi = " ".join(_project(attachments=None, native=native)["README.md"].split())
    assert "translated by sqlglot and not run on Postgres" in leggimi
    assert "not run on Postgres" not in " ".join(_project(attachments=None, native={**_NATIVE_CH, "target_schema": "s"})["README.md"].split())


# #4 (native) — Postgres' avg/stddev/variance of an integer or a decimal is a numeric: cast to text it
# was '1.5000000000000000' where Tabularia writes '1.5'; of a boolean they did not exist
_INTEGER_GROUPS = _pl.DataFrame({"g": [1, 1, 2, 2, 2, 2, 2, 3], "x": [1, 2, 1, 2, 3, 4, 5, None],
                                "b": [True, False, True, True, True, True, None, None]}).with_columns(_pl.col("x").cast(_pl.Decimal(10, 2)).alias("dx"))


@pytest.mark.parametrize("where", ["postgres", "clickhouse"])
@pytest.mark.parametrize("col", ["x", "dx", "b"])
def test_mean_std_and_var_on_native_targets_are_doubles(where, col):
    gb = [{"type": "group_by", "params": {"by": ["g"], "aggregations": [{"column": col, "func": f, "alias": f} for f in ("mean", "std", "var")]}},
          {"type": "cast", "params": {"columns": {"mean": "str", "std": "str", "var": "str"}}}]
    rif = _engine_snapshot("polars", {"t": _INTEGER_GROUPS}, "t", gb)
    if col != "b":   # the DuckDB engine has no mean of a boolean (Polars has: the share of true)
        assert _engine_snapshot("duckdb", {"t": _INTEGER_GROUPS}, "t", gb)[1] == rif[1]
    assert rif[1][:2] == ([(1.0, "0.5", "0.7071067811865476", "0.5"), (2.0, "1.0", "0.0", "0.0")] if col == "b" else
                          [(1.0, "1.5", "0.7071067811865476", "0.5"), (2.0, "3.0", "1.5811388300841898", "2.5")]), rif
    _, _, types = d.compile_model_sql(*_export_types(gb[:1], col))
    assert all(types[f] == "DOUBLE" for f in ("mean", "std", "var"))
    assert _native_on(where, _INTEGER_GROUPS, gb) == rif[1]
    # the federated target too
    assert _export_snapshot({"t": _INTEGER_GROUPS}, "t", gb)[1] == rif[1]


def _export_types(ops: list, col: str):
    def resolve(_):
        return "t_t", list(_INTEGER_GROUPS.columns), {c: d.duck_type(str(t)) for c, t in _INTEGER_GROUPS.schema.items()}, "t"
    return ({"key": "t"}, ops, resolve)


# #4 (native) — a mean or a median of dates or instants: avg(date) and median(date) do not exist on
# Postgres and ClickHouse, the model failed; Tabularia gives an instant
_DATE_GROUPS = _pl.DataFrame({
    "g": [1, 1, 1, 2, 2, 3, 3, 3, 3, 4],
    "d": [_dt.date(2024, 1, 1), _dt.date(2024, 1, 2), _dt.date(2024, 1, 4), _dt.date(1960, 5, 5), _dt.date(1960, 5, 6),
          _dt.date(2024, 1, 1), _dt.date(2024, 1, 5), _dt.date(2024, 1, 6), _dt.date(2299, 12, 31), None],
    "t": [_dt.datetime(2024, 1, 1, 10), _dt.datetime(2024, 1, 1, 11), _dt.datetime(2024, 1, 1, 15), _dt.datetime(1960, 5, 5, 0, 0, 1),
          _dt.datetime(1960, 5, 5, 0, 0, 3), _dt.datetime(2024, 1, 1), _dt.datetime(2024, 1, 2), _dt.datetime(2024, 1, 2, 0, 0, 0, 500000),
          _dt.datetime(2024, 1, 3), None]})


@pytest.mark.parametrize("where", ["postgres", "clickhouse"])
@pytest.mark.parametrize("func", ["mean", "median"])
def test_mean_and_median_of_dates_and_instants_run_on_native_targets(where, func):
    gb = [{"type": "group_by", "params": {"by": ["g"], "aggregations": [{"column": "d", "func": func, "alias": "md"},
                                                                         {"column": "t", "func": func, "alias": "mt"}]}}]
    rif = _engine_snapshot("polars", {"t": _DATE_GROUPS}, "t", gb)
    assert _engine_snapshot("duckdb", {"t": _DATE_GROUPS}, "t", gb)[1] == rif[1]
    assert rif[2][1:] == ["datetime", "datetime"] and rif[1][-1] == (4.0, None, None)
    assert _native_on(where, _DATE_GROUPS, gb) == rif[1]


def test_min_and_max_of_a_boolean_run_on_native_postgres():
    gb = [{"type": "group_by", "params": {"by": ["g"], "aggregations": [{"column": "flag", "func": "min", "alias": "lo"},
                                                                         {"column": "flag", "func": "max", "alias": "hi"}]}}]
    rif = _engine_snapshot("duckdb", {"t": _pl.DataFrame({"g": ["a", "a", "b", "c"], "flag": [True, False, True, None]})}, "t", gb)
    files = _export({"g": "String", "flag": "Boolean"}, gb, mode="native")
    values = "(SELECT * FROM (VALUES ('a', TRUE), ('a', FALSE), ('b', TRUE), ('c', CAST(NULL AS BOOLEAN))) AS t(g, flag))"
    rows = sorted(_su_postgres(f"SELECT * FROM ({_model(files, values)}) AS m"), key=lambda r: r[0])
    assert [tuple(_norm(v) for v in r) for r in rows] == rif[1] == [("a", False, True), ("b", True, True), ("c", None, None)]


# #15 — native Postgres compares and orders text by BYTES, like Tabularia (decision of 2026-10-16),
# whatever the collation of the database or of the column: on en_US 'a' < 'B', for Tabularia 'B' < 'a'
_TEXTS = _pl.DataFrame({"id": [1, 2, 3, 4, 5, 6, 7, 8], "g": ["x", "x", "y", "y", "y", "z", "z", "z"],
                        "nome": ["a", "B", "Città", "citta", "Zeta", "alpha", "Bravo", None]})
_TEXT_ORDER = {
    "sort_limit": [{"type": "sort", "params": {"by": ["nome"]}}, {"type": "limit", "params": {"n": 3}}],
    "sort_desc_limit": [{"type": "sort", "params": {"by": ["nome"], "descending": True}}, {"type": "limit", "params": {"n": 2}}],
    "range": [{"type": "filter", "params": {"column": "nome", "operator": "gt", "value": "B"}}],
    "between": [{"type": "filter", "params": {"column": "nome", "operator": "between", "value": ["B", "a"]}}],
    "min_max": [{"type": "group_by", "params": {"by": ["g"], "aggregations": [{"column": "nome", "func": "min", "alias": "lo"},
                                                                              {"column": "nome", "func": "max", "alias": "hi"}]}}],
    "computed": [{"type": "compute", "params": {"columns": [{"name": "u", "expr": "nome || '!'"}]}},
                 {"type": "sort", "params": {"by": ["u"]}}, {"type": "limit", "params": {"n": 3}}],
    # text made of literals only: it has the database's collation, not the source column's
    "literal": [{"type": "compute", "params": {"columns": [{"name": "u", "expr": "CASE WHEN id < 4 THEN 'a' ELSE 'B' END || CAST(id AS VARCHAR)"}]}},
                {"type": "sort", "params": {"by": ["u"]}}, {"type": "limit", "params": {"n": 3}}],
    "first_last": [{"type": "sort", "params": {"by": ["nome"]}},
                   {"type": "group_by", "params": {"by": ["g"], "aggregations": [{"column": "id", "func": "first", "alias": "primo"},
                                                                                 {"column": "id", "func": "last", "alias": "ultimo"}]}}],
}


@pytest.mark.parametrize("case", list(_TEXT_ORDER))
@pytest.mark.parametrize("collazione", [None, "en-x-icu"])
def test_native_postgres_orders_and_compares_text_by_bytes_like_tabularia(case, collazione):
    ops = _TEXT_ORDER[case]
    rif = _engine_snapshot("polars", {"t": _TEXTS}, "t", ops)
    files = _export({c: str(t) for c, t in _TEXTS.schema.items()}, ops, mode="native")
    name = "nome" + (f' COLLATE "{collazione}"' if collazione else "")
    rows = ", ".join(f"({i}, '{g}', {'NULL' if n is None else repr(n)})" for i, g, n in _TEXTS.iter_rows())
    values = f"(SELECT id, g, {name} AS nome FROM (VALUES {rows}) AS t(id, g, nome))"
    got = sorted((tuple(_norm(v) for v in r) for r in _su_postgres(f"SELECT * FROM ({_model(files, values)}) AS m")),
                 key=lambda t: tuple(map(str, t)))
    assert got == rif[1], (case, collazione)


# #22 — Postgres keeps only the first 63 bytes of a name: two long column names that share them are
# one column there (the model fails), and one long name is shorter in the warehouse than in Tabularia
_LONG = "net_revenue_after_discount_per_order_line_for_the_board_report_"      # 63 bytes


def test_on_native_postgres_two_column_names_equal_in_their_first_63_bytes_are_refused():
    from fastapi import HTTPException

    assert len(_LONG.encode()) == 63
    ops = [{"type": "compute", "params": {"columns": [{"name": _LONG + "2024", "expr": "id * 1"}, {"name": _LONG + "2025", "expr": "id * 2"}]}}]
    with pytest.raises(HTTPException) as e:
        _export({"id": "Int32"}, ops, mode="native")
    assert e.value.detail["code"] == "column_names_too_long"
    assert e.value.detail["params"] == {"columns": f"{_LONG}2024, {_LONG}2025", "limit": 63}
    # a name cut inside a two-byte character: Postgres keeps the whole characters only
    accentato = "à" * 31 + "x"                                                         # 63 bytes
    ops2 = [{"type": "compute", "params": {"columns": [{"name": accentato + "àa", "expr": "id * 1"}, {"name": accentato + "àb", "expr": "id"}]}}]
    with pytest.raises(HTTPException):
        _export({"id": "Int32"}, ops2, mode="native")
    # the federated and the ClickHouse targets have no such limit
    for mode in ("duckdb", "clickhouse"):
        _export({"id": "Int32"}, ops, mode=mode)


def test_on_native_postgres_one_long_column_name_runs_and_the_readme_says_what_it_becomes():
    ops = [{"type": "compute", "params": {"columns": [{"name": _LONG + "2024", "expr": "id * 2"}]}}]
    files = _export({"id": "Int32"}, ops, mode="native")
    assert f"`{_LONG}2024`" in files["README.md"] and f"`{_LONG}`" in files["README.md"]
    assert "63" in files["README.md"]
    rows = _su_postgres(f"SELECT * FROM ({_model(files, '(SELECT * FROM (VALUES (3)) AS t(id))')}) AS m")
    assert rows == [(3, 6)]
    assert "63" not in _export({"id": "Int32"}, ops)["README.md"]


# ── E4: the ClickHouse target and the native profiles ─────────────────────────
_DT = "Datetime(time_unit='us', time_zone=None)"
_TEMPI = {"id": "Int32", "nato": "Date", "visto": _DT, "ora": "Time"}


def _su_clickhouse(sql: str) -> list:
    return [tuple(r) for r in _clickhouse().query(sql).result_rows]


# #24 — dates and times on ClickHouse: Date (1970-2149) and DateTime (whole seconds, 1970-2106) wrapped
# a birth date of 1955, cut every timestamp's microseconds; TIME is an Int64 there, so a time was NULL
def test_dates_and_times_on_clickhouse_have_the_types_of_the_engine():
    from app.api.routes.dbt import _type_in_dialect

    sql = " ".join(_code(_export(_TEMPI, [], mode="clickhouse")["models/uno.sql"]).split())
    assert '"nato" AS Nullable(Date32))' in sql and '"visto" AS Nullable(DateTime64(6)))' in sql and '"ora" AS Nullable(String))' in sql
    assert "Nullable(DATE)" not in sql and "Nullable(DateTime)" not in sql and "TIME)" not in sql
    # a seed's column types, and a datasource query's projection
    assert [_type_in_dialect(t, "clickhouse") for t in ("DATE", "TIMESTAMP", "TIME")] == ["Nullable(Date32)", "Nullable(DateTime64(6))", "Nullable(String)"]
    raw = [{"name": "src_q", "kind": "raw", "materialized": "ephemeral", "raw_sql": "SELECT * FROM ordini",
            "columns": [{"name": c, "dtype": t} for c, t in _TEMPI.items()]}]
    q = _files(_export_request(models=raw, source_map={}, mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"}))["models/src_q.sql"]
    assert "Nullable(Date32)" in q and "Nullable(DateTime64(6))" in q and "TIME" not in q
    # the native ClickHouse target is translated the same way
    native = {"db_type": "clickhouse", "conn": {"host": "h", "port": 8123, "database": "d", "username": "u"}, "pw_env": "P"}
    nat = _files(_export_request(mode="native", native=native, models=[{"name": "uno", "source": {"key": "k"}, "operations": []}],
                                   source_map={"k": {"ident": "__s_0", "macro": "{{ source('s', 'ordini') }}", "columns": list(_TEMPI),
                                                     "types": _TEMPI, "label": "ordini"}}))["models/uno.sql"]
    assert "Nullable(Date32)" in nat and "Nullable(DateTime64(6))" in nat


def test_dates_and_times_on_clickhouse_keep_the_values_tabularia_publishes():
    values = ("(SELECT * FROM values('id Int32, nato Nullable(Date32), visto Nullable(DateTime64(6)), ora Nullable(String)', "
              "(1, '1955-03-12', '2024-01-15 10:30:00.123456', '13:45:00'), (2, '2200-01-01', '1965-06-01 08:00:00', '08:15:30.5'), "
              "(3, NULL, '2200-01-01 00:00:00', NULL)))")
    ops = [{"type": "compute", "params": {"columns": [{"name": "anno", "expr": "year(nato)"}]}}]
    rows = sorted(_su_clickhouse(_model(_export(_TEMPI, ops, mode="clickhouse"), values)))
    assert rows == [(1, _dt.date(1955, 3, 12), _dt.datetime(2024, 1, 15, 10, 30, 0, 123456), "13:45:00", 1955),
                     (2, _dt.date(2200, 1, 1), _dt.datetime(1965, 6, 1, 8, 0), "08:15:30.5", 2200),
                     (3, None, _dt.datetime(2200, 1, 1), None, None)]
    # a filter on an instant with a fraction of a second: it used to fail the model (TYPE_MISMATCH)
    ops = [{"type": "filter", "params": {"column": "visto", "operator": "eq", "value": "2024-01-15 10:30:00.123456"}}]
    assert [r[0] for r in _su_clickhouse(_model(_export(_TEMPI, ops, mode="clickhouse"), values))] == [1]
    # the order of instants before 1970 and after 2106: the first is 1965's
    ops = [{"type": "sort", "params": {"by": ["visto"]}}, {"type": "limit", "params": {"n": 1}}]
    assert [r[0] for r in _su_clickhouse(_model(_export(_TEMPI, ops, mode="clickhouse"), values))] == [2]


# #25 — the sources on the ClickHouse target: a PostgreSQL/MySQL DATABASE engine types the columns by
# itself, a date as Date (1955 → 2134 before any cast), an unconstrained numeric as Decimal(38, 19)
# (a bigger value failed every model), a MySQL datetime as DateTime (1965 → 1970, no microseconds)
def test_the_clickhouse_sources_are_tables_with_types_that_keep_every_value():
    ch = d.TargetClickHouse([_PG, _MY], "ch.local")
    ch.sources([{"name": "db_1_vendite", "schema": "vendite", "connection_id": 1, "tables": [{"name": "ordini"}, {"name": "clienti"}]},
                {"name": "db_2_shop", "schema": "shop", "connection_id": 2, "tables": [{"name": "righe"}]}])
    ch.translate_query("SELECT c.id FROM crm.contatti AS c JOIN vendite.ordini AS o ON o.cliente = c.id", 1, "q")
    macro = d._clickhouse_sources_macro(list(ch.federated.values()))
    assert not re.search(r"CREATE DATABASE[^\n]*ENGINE", macro)            # no database engine any more
    for db, tables, engine_name, arguments in (
            ("tab_src_1_vendite", ["clienti", "ordini"], "PostgreSQL", "'db.x.it:5432', 'erp', '{t}', 'shop', '{{{{ pw_tab_src_1_vendite }}}}', 'vendite'"),
            ("tab_src_1_crm", ["contatti"], "PostgreSQL", "'db.x.it:5432', 'erp', '{t}', 'shop', '{{{{ pw_tab_src_1_crm }}}}', 'crm'"),
            ("tab_src_2_shop", ["righe"], "MySQL", "'my.x.it:3306', 'shop', '{t}', 'web', '{{{{ pw_tab_src_2_shop }}}}'")):
        assert f"CREATE DATABASE IF NOT EXISTS `{db}`" in macro
        for t in tables:
            # one table per source table (and per table a query reads), typed from what the source has
            assert f"CREATE OR REPLACE TABLE `{db}`.`{t}` AS {engine_name.lower()}({arguments.format(t=t)})" in macro
            assert f"CREATE OR REPLACE TABLE `{db}`.`{t}` ({{{{ columns | join(', ') }}}}) ENGINE = {engine_name}({arguments.format(t=t)})" in macro
            assert d._structure_sql(db, t, engine_name) in macro
        # a database an earlier export made with the PostgreSQL/MySQL engine is replaced (it holds no data)
        assert f"name = '{db}' AND engine IN ('PostgreSQL', 'MySQL')" in macro
    assert macro.count("{% call statement(") == macro.count("{%- endcall %}")


@_WITH_CLICKHOUSE
def test_the_types_of_a_clickhouse_source_table_keep_dates_before_1970_and_big_numbers():
    import uuid

    cli = _clickhouse()
    table = f"zz_e4_{uuid.uuid4().hex[:8]}"
    names = ["id", "nato", "nati", "date_list", "visto", "visto6", "n", "n2", "we`ird\\name", "pair"]
    types = ["Int32", "Nullable(Date)", "Date", "Array(Nullable(Date))", "Nullable(DateTime)", "Nullable(DateTime64(6))",
            "Nullable(Decimal(38, 19))", "Nullable(Decimal(12, 2))", "Nullable(String)", "Tuple(a Date, b DateTime)"]
    columns = ", ".join(f"`{n.replace(chr(92), chr(92) * 2).replace('`', chr(92) + '`')}` {t}" for n, t in zip(names, types))
    cli.command(f"CREATE TABLE tab.{table} ({columns}) ENGINE = Memory")
    try:
        pg = [r[0] for r in cli.query(d._structure_sql("tab", table, "PostgreSQL")).result_rows]
        my = [r[0] for r in cli.query(d._structure_sql("tab", table, "MySQL")).result_rows]
    finally:
        cli.command(f"DROP TABLE tab.{table}")
    expected_items = ["Int32", "Nullable(Date32)", "Date32", "Array(Nullable(Date32))", "Nullable(DateTime64(6))", "Nullable(DateTime64(6))",
              "Nullable(Float64)", "Nullable(Decimal(12, 2))", "Nullable(String)", "Tuple(a Date32, b DateTime64(6))"]
    quoted = ["`id`", "`nato`", "`nati`", "`date_list`", "`visto`", "`visto6`", "`n`", "`n2`", "`we\\`ird\\\\name`", "`pair`"]
    assert pg == [f"{q} {t}" for q, t in zip(quoted, expected_items)]
    # MySQL: a decimal is kept exact (Tabularia reads it as a decimal); Postgres numeric is Tabularia's double
    assert my == [f"{q} {t}" for q, t in zip(quoted, expected_items[:6] + ["Nullable(Decimal(38, 19))"] + expected_items[7:])]


# #26 — on ClickHouse upper/lower/substring/reverse and left/right work on bytes: 'Città' became
# 'CITTà', a substring cut a character in half, and starts_with/ends_with an accented value kept no row
_ACCENTS = _pl.DataFrame({"id": [1, 2, 3, 4], "s": ["Città", "Università", "ÀÈ", "Roma"]})
_UTF8_TEXT = [{"type": "compute", "params": {"columns": [
    {"name": "su", "expr": "upper(s)"}, {"name": "giu", "expr": "lower(s)"}, {"name": "pezzo", "expr": "substring(s, 4, 2)"},
    {"name": "rovescio", "expr": "reverse(s)"}, {"name": "lungo", "expr": "length(s)"}, {"name": "testa", "expr": "left(s, 4)"}]}}]


@pytest.mark.parametrize("ops", [
    _UTF8_TEXT,
    [{"type": "filter", "params": {"column": "s", "operator": "starts_with", "value": "Città"}}],
    [{"type": "filter", "params": {"column": "s", "operator": "ends_with", "value": "tà"}}],
    [{"type": "filter", "params": {"column": "s", "operator": "contains", "value": "à"}}],
], ids=["compute", "starts_with", "ends_with", "contains"])
def test_accented_text_on_clickhouse_gives_what_the_engines_give(ops):
    rif = _engine_snapshot("polars", {"k": _ACCENTS}, "k", ops)
    assert rif == _engine_snapshot("duckdb", {"k": _ACCENTS}, "k", ops)
    values = "(SELECT * FROM values('id Int64, s Nullable(String)', " + ", ".join(f"({i}, '{s}')" for i, s in _ACCENTS.iter_rows()) + "))"
    rows = _su_clickhouse(_model(_export({"id": "Int64", "s": "String"}, ops, mode="clickhouse"), values))
    assert sorted((tuple(_norm(v) for v in r) for r in rows), key=lambda t: tuple(map(str, t))) == rif[1]


def test_the_position_of_a_text_on_clickhouse_counts_characters_as_duckdb_does():
    """#26: DuckDB's strpos/instr/position count characters, ClickHouse's POSITION bytes: in 'ÀÈ' the
    È was 3. (The Polars engine counts bytes too: its text is flagged for review.)"""
    ops = [{"type": "compute", "params": {"columns": [{"name": "dove", "expr": "strpos(s, 'È')"}, {"name": "dove2", "expr": "position('È' IN s)"},
                                                      {"name": "dove3", "expr": "instr(s, 'tà')"}]}}]
    rif = _engine_snapshot("duckdb", {"k": _ACCENTS}, "k", ops)
    assert [r[2:] for r in rif[1]] == [(0.0, 0.0, 4.0), (0.0, 0.0, 9.0), (2.0, 2.0, 0.0), (0.0, 0.0, 0.0)]
    values = "(SELECT * FROM values('id Int64, s Nullable(String)', " + ", ".join(f"({i}, '{s}')" for i, s in _ACCENTS.iter_rows()) + "))"
    rows = _su_clickhouse(_model(_export({"id": "Int64", "s": "String"}, ops, mode="clickhouse"), values))
    assert sorted((tuple(_norm(v) for v in r) for r in rows), key=lambda t: tuple(map(str, t))) == rif[1]


# #62 — dbt inlines an ephemeral model (a shared step, a datasource query) as a CTE of the model that
# reads it: a step name both use (drop_columns, fill_nulls, filter_x…) is UNKNOWN_TABLE on ClickHouse 24
_SHARED = [
    {"name": "int_d1", "kind": "intermediate", "materialized": "ephemeral", "source": {"key": "k"},
     "operations": [{"type": "drop", "params": {"columns": ["numero"]}}, {"type": "filter", "params": {"column": "id", "operator": "le", "value": 3}}]},
    {"name": "uno", "source": {"model": "int_d1"},
     "operations": [{"type": "drop", "params": {"columns": ["deposito"]}}, {"type": "filter", "params": {"column": "id", "operator": "ge", "value": 2}}]},
]


def _cte(sql: str) -> list:
    return re.findall(r"^\s*(?:WITH\s+)?[\"`]?(\w+)[\"`]? AS \(", sql, flags=re.M)


def _with_the_ephemeral(files: dict, model: str, effimero: str, source: str) -> str:
    """The model as dbt-clickhouse runs it: the ephemeral model's SQL inlined as the CTE
    `__dbt__cte__<name>` at the head of the model's own WITH."""
    rel = {("source", "db_1_v", "ordini"): source, ("ref", effimero): f"__dbt__cte__{effimero}"}
    inner = _render_like_dbt(files[f"models/{effimero}.sql"], rel, config={})
    outer_one = _render_like_dbt(files[f"models/{model}.sql"], rel, config={}).lstrip()
    assert outer_one.startswith("WITH")
    return f"WITH __dbt__cte__{effimero} AS (\n{inner}\n),\n" + outer_one[len("WITH"):].lstrip()


def test_a_model_and_the_ephemeral_model_it_reads_never_share_a_cte_name():
    for mode in ("duckdb", "native", "clickhouse"):
        extra = {"native": _PG_NATIVE} if mode == "native" else ({"clickhouse": {"connections": [_PG], "target_schema": "s"}} if mode == "clickhouse" else {})
        item = {"ref": "{{ source('db_1_v', 'ordini') }}"} if mode == "duckdb" else {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}"}
        files = _files(_export_request(mode=mode, **extra, models=_SHARED, source_map={"k": {
            **item, "columns": ["id", "numero", "deposito"], "types": {"id": "Int64", "numero": "Int64", "deposito": "String"}, "label": "ordini"}}))
        inner_ones, outer = _cte(files["models/int_d1.sql"]), _cte(files["models/uno.sql"])
        assert inner_ones and outer and not set(inner_ones) & set(outer), (mode, inner_ones, outer)


@_WITH_CLICKHOUSE
def test_a_model_that_reads_an_ephemeral_model_runs_on_clickhouse_24():
    files = _files(_export_request(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"}, models=_SHARED,
                                     source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "numero", "deposito"],
                                                       "types": {"id": "Int64", "numero": "Int64", "deposito": "String"}, "label": "ordini"}}))
    values = "(SELECT * FROM values('id Int64, numero Int64, deposito String', (1, 10, 'a'), (2, 20, 'b'), (3, 30, 'c'), (4, 40, 'd')))"
    assert sorted(_su_clickhouse(_with_the_ephemeral(files, "uno", "int_d1", values))) == [(2,), (3,)]


# #27 — Postgres folds the names a query writes without quotes to lowercase; ClickHouse does not: the
# query `SELECT Id, Nome FROM Catalogo.Prodotti` passed the export and failed at dbt run (UNKNOWN_TABLE)
def test_a_postgres_query_reads_on_clickhouse_the_names_postgres_reads():
    ch = d.TargetClickHouse([_PG, _MY], "ch.local")
    sql = " ".join(ch.translate_query('WITH Attivi AS (SELECT Id, Nome, "Città" FROM Catalogo.Prodotti AS P WHERE P.Attivo) '
                                    "SELECT A.Id, A.Nome, A.\"Città\", O.Totale AS Totale FROM Attivi AS A JOIN Ordini AS O ON O.Prodotto = A.Id",
                                    1, "q").split())
    assert "tab_src_1_catalogo.prodotti" in sql and "tab_src_1_public.ordini" in sql
    assert "SELECT id, nome, \"Città\" FROM" in sql and "o.totale AS totale" in sql and "FROM attivi AS a" in sql
    assert not re.search(r"\b(Id|Nome|Prodotti|Catalogo|Attivi|Totale)\b", sql)
    # the database is made for the schema Postgres has, `catalogo`: 'Catalogo' would be an empty one
    assert ch.federated["tab_src_1_catalogo"]["schema"] == "catalogo" and ch.federated["tab_src_1_catalogo"]["tables"] == {"prodotti"}
    # MySQL keeps table names as written (they are case sensitive on its usual servers)
    assert "tab_src_2_shop.Clienti" in ch.translate_query("SELECT id FROM shop.Clienti", 2, "m")


# #63 — without join_use_nulls ClickHouse fills the missing side of an outer join with 0 / '': the
# native ClickHouse profile did not set it (the ClickHouse target's did), a customer without orders had 0
_NATIVE_CH = {"db_type": "clickhouse", "conn": {"host": "ch.x.it", "port": 8123, "database": "crm", "username": "u"}, "pw_env": "P"}


def _profile_settings(profile: str) -> dict:
    """The custom_settings of a dbt-clickhouse profile, as dbt-clickhouse sends them with every query."""
    block = profile.split("custom_settings:\n", 1)[1] if "custom_settings:\n" in profile else ""
    return dict(re.findall(r"^ {8}(\w+): (\S+)$", block, flags=re.M))


def test_the_native_clickhouse_profile_gives_null_on_the_missing_side_of_an_outer_join():
    customers = _pl.DataFrame({"id": [1, 2], "nome": ["con ordini", "senza ordini"]})
    orders = _pl.DataFrame({"oid": [10, 11], "cliente_id": [1, 1]})
    ops = [{"type": "join", "params": {"how": "left", "left_on": ["id"], "right_on": ["cliente_id"], "right": {"source": _key("ordini"), "operations": [
        {"type": "group_by", "params": {"by": ["cliente_id"], "aggregations": [{"column": "oid", "func": "count", "alias": "n_ordini"}]}}]}}}]
    rif = _engine_snapshot("polars", {"clienti": customers, "ordini": orders}, "clienti", ops)
    assert rif[1] == [(1.0, "con ordini", 1.0, 2.0), (2.0, "senza ordini", None, None)]
    files = _files(_export_request(mode="native", native=_NATIVE_CH, models=[{"name": "uno", "source": _key("clienti"), "operations": ops}],
                                     source_map={_key(k)["key"]: {"ident": f"__s_{i}", "macro": "{{ source('crm', '%s') }}" % k, "columns": list(df.columns),
                                                     "types": {c: str(t) for c, t in df.schema.items()}, "label": k}
                                                 for i, (k, df) in enumerate((("clienti", customers), ("ordini", orders)))}))
    settings_ = _profile_settings(files["profiles.yml"])
    assert settings_.get("join_use_nulls") == "1"
    sql = _render_like_dbt(files["models/uno.sql"], {("source", "crm", "clienti"): "(SELECT * FROM values('id Int64, nome String', (1, 'con ordini'), (2, 'senza ordini')))",
                                                     ("source", "crm", "ordini"): "(SELECT * FROM values('oid Int64, cliente_id Int64', (10, 1), (11, 1)))"}, config={})
    rows = [tuple(r) for r in _clickhouse().query(sql, settings=settings_).result_rows]
    assert sorted((tuple(_norm(v) for v in r) for r in rows), key=lambda t: tuple(map(str, t))) == rif[1]
    # the folder package says it to the team, as it does for the ClickHouse target
    folder = _project(attachments=None, layout={"package": "folder", "folder": "f"}, native={**_NATIVE_CH, "target_schema": "s"})
    assert "join_use_nulls: 1" in folder["INTEGRATION.md"]


# #21 — Tabularia reaches a ClickHouse connection on 8443 or 443 over TLS (clickhouse-connect does
# so from the port); the native profile wrote `secure: false`, and dbt spoke plain HTTP to a TLS port
@pytest.mark.parametrize("port,tls", [(8443, True), (443, True), (8123, False), (9000, False)])
def test_the_native_clickhouse_profile_uses_tls_where_tabularia_does(port, tls):
    from clickhouse_connect.driver import _parse_connection_params   # how Tabularia's client picks the scheme

    schema = _parse_connection_params(host="h", username="", password="", port=port, database="", interface=None, secure=False,
                                      dsn=None, kwargs={})[-1]
    assert (schema == "https") is tls
    native = {**_NATIVE_CH, "conn": {**_NATIVE_CH["conn"], "port": port}, "target_schema": "s"}
    profile = d._native_profile("p", native)
    line = next(r for r in profile.splitlines() if r.strip().startswith("secure:"))
    # the default is Tabularia's choice; CLICKHOUSE_SECURE changes it, as on the ClickHouse target
    assert line == "      secure: \"{{ (env_var('CLICKHOUSE_SECURE', '" + str(tls).lower() + "') | lower in ['true', '1', 'yes']) | as_bool }}\""
    assert ("over TLS (`secure: true`)" in _project(attachments=None, native=native)["README.md"]) is tls


# #64 — every step lists and qualifies every column on ClickHouse: a wide, long flow passes 256 KiB,
# ClickHouse's default max_query_size. The profiles never raised it (dbt run failed), the syntax
# check refused it as «not translatable» (and asked the AI), and it measured each model alone while
# dbt inlines the ephemeral ones into the model that reads them
_WIDE = [f"colonna_numero_{i:03d}" for i in range(150)]


def _wide_models(intermediate_steps: int, output_steps: int) -> list:
    filters = lambda n, da: [{"type": "filter", "params": {"column": _WIDE[(da + i) % 150], "operator": "ge", "value": -i}} for i in range(n)]  # noqa: E731
    return [{"name": "int_largo", "kind": "intermediate", "materialized": "ephemeral", "source": {"key": "k"}, "operations": filters(intermediate_steps, 0)},
            {"name": "uno", "source": {"model": "int_largo"}, "operations": filters(output_steps, intermediate_steps)}]


def _export_wide(models: list):
    return _export_request(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"}, models=models,
                             source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'larga') }}", "columns": _WIDE,
                                               "types": {c: "Int64" for c in _WIDE}, "label": "larga"}})


def test_the_clickhouse_profiles_raise_max_query_size_and_the_check_uses_the_same_limit():
    for profile in (_project(attachments=None, clickhouse={"host": "h", "federati": []})["profiles.yml"],
                    d._native_profile("p", {**_NATIVE_CH, "target_schema": "s"})):
        assert _profile_settings(profile)["max_query_size"] == str(d.MAX_QUERY_SIZE)
    assert d.MAX_QUERY_SIZE > 262144
    # one model over ClickHouse's default 256 KiB: exported, it runs with the profile's setting
    files = _files(_export_wide([{**_wide_models(0, 28)[1], "source": {"key": "k"}}]))
    assert len(files["models/uno.sql"]) > 262144


def test_a_model_over_the_limit_with_the_ephemeral_models_it_inlines_is_refused_with_its_own_code(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(d, "MAX_QUERY_SIZE", 150_000)
    # each model alone is under the limit, together (as dbt runs the output) they are over it
    with pytest.raises(HTTPException) as e:
        _zip(_export_wide(_wide_models(9, 9)))
    assert e.value.detail["code"] == "model_too_large"
    assert e.value.detail["params"]["model"] == "uno" and e.value.detail["params"]["max"] == "150,000"
    assert int(e.value.detail["params"]["size"].replace(",", "")) > 150_000
    # one model alone over the limit: the same code, not «not translatable» (which asks the AI for a translation)
    with pytest.raises(HTTPException) as e:
        _zip(_export_wide([{**_wide_models(0, 18)[1], "source": {"key": "k"}}]))
    assert e.value.detail["code"] == "model_too_large" and e.value.detail["params"]["model"] == "uno"


def test_the_size_of_a_clickhouse_model_is_counted_in_bytes(monkeypatch):
    """#64: max_query_size is in bytes; a model of accented text near the limit passed a check
    that counted characters and failed at `dbt run`."""
    from fastapi import HTTPException

    monkeypatch.setattr(d, "MAX_QUERY_SIZE", 100_000)
    text = "'" + "à" * 60_000 + "'"   # 60,000 characters, 120,000 bytes
    with pytest.raises(HTTPException) as e:
        _export({"id": "Int64"}, [{"type": "compute", "params": {"columns": [{"name": "n", "expr": text}]}}], mode="clickhouse")
    assert e.value.detail["code"] == "model_too_large" and int(e.value.detail["params"]["size"].replace(",", "")) > 120_000
    # the same text in ASCII is under the limit
    _export({"id": "Int64"}, [{"type": "compute", "params": {"columns": [{"name": "n", "expr": "'" + "a" * 60_000 + "'"}]}}], mode="clickhouse")


def test_a_column_with_a_double_quote_is_refused_where_dbt_clickhouse_cannot_create_it():
    """dbt-clickhouse writes a table's column list as "…" without escaping a double quote: a table or
    incremental model with the column `quote"col` failed at `dbt run` (Code 62, unmatched parentheses;
    measured with dbt-clickhouse 1.10). A view, an ephemeral model and the other adapters build it."""
    from fastapi import HTTPException

    types = {"id": "Int64", 'quote"col': "String"}
    for mode, extra in (("clickhouse", {"clickhouse": {"connections": [_PG], "target_schema": "s"}}),
                        ("native", {"native": {**_NATIVE_CH, "target_schema": "s"}})):
        item = {"ident": "__s_0", "macro": "{{ source('db_1_v', 'q') }}", "columns": list(types), "types": types, "label": "q"}
        for materializzato in ("table", "incremental"):
            with pytest.raises(HTTPException) as e:
                _zip(_export_request(mode=mode, **extra, source_map={"k": item},
                                       models=[{"name": "uno", "source": {"key": "k"}, "operations": [], "materialized": materializzato}]))
            assert e.value.detail["code"] == "column_name_unsupported", e.value.detail
            assert e.value.detail["params"] == {"model": "uno", "column": 'quote"col', "character": '"', "adapter": "dbt-clickhouse"}
        for materializzato in ("view", "ephemeral"):
            _zip(_export_request(mode=mode, **extra, source_map={"k": item},
                                   models=[{"name": "uno", "source": {"key": "k"}, "operations": [], "materialized": materializzato}]))
    # the other adapters quote it
    _zip(_export_request(mode="native", native=_PG_NATIVE, source_map={"k": {**item, "macro": "{{ source('db_1_v', 'q') }}"}},
                           models=[{"name": "uno", "source": {"key": "k"}, "operations": []}]))


# #66 — without chDB (the production image) the syntax check asks the engine's ClickHouse; one that
# accepts the connection and never answers held the export 300 s (the client's default), then the
# check was skipped with only a log line
def test_a_clickhouse_that_never_answers_does_not_hold_the_export_and_the_readme_says_the_check_was_skipped(monkeypatch):
    import socket
    import sys
    import threading
    import time

    from app.core.config import get_settings

    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(8)                    # accepts, never answers
    cfg = get_settings().clickhouse_external
    monkeypatch.setattr(cfg, "host", "127.0.0.1")
    monkeypatch.setattr(cfg, "port", silent.getsockname()[1])
    monkeypatch.setitem(sys.modules, "chdb", None)        # as in the production image
    monkeypatch.setattr(d, "CHECK_WAIT", 1)
    check, outcome = d.ClickHouseSyntaxCheck(), {}

    def run_check():
        start = time.monotonic()
        outcome["primo"] = check.error("SELECT 1")
        outcome["secondi"] = time.monotonic() - start
        outcome["poi"] = check.error("SELECT FROM WHERE (")
        outcome["dopo"] = time.monotonic() - start
    worker_thread = threading.Thread(target=run_check, daemon=True)
    worker_thread.start()
    worker_thread.join(20)
    try:
        assert not worker_thread.is_alive(), "the check is still waiting for ClickHouse"
        assert outcome["primo"] is None and outcome["poi"] is None and outcome["secondi"] < 10
        assert outcome["dopo"] - outcome["secondi"] < 1        # off for the rest of the export: no second wait
        assert check.skipped
        check.close()
        # the README says that the SQL was not read by a ClickHouse parser
        start = time.monotonic()
        files = _files(_export_request(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"},
                                         source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "x"],
                                                           "types": {"id": "Int64", "x": "String"}, "label": "ordini"}}))
        assert time.monotonic() - start < 10
        assert "not read by a ClickHouse parser" in files["README.md"]
    finally:
        silent.close()
    # with a parser that answers (chDB here), no such note
    monkeypatch.undo()
    files = _files(_export_request(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"},
                                     source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "x"],
                                                       "types": {"id": "Int64", "x": "String"}, "label": "ordini"}}))
    assert "not read by a ClickHouse parser" not in files["README.md"]


# #68 — two schemas that differ only by case, punctuation or non-ASCII letters were ONE federated
# database: it was made for the first, and the second's tables were read from the first schema
def test_every_source_schema_has_a_database_of_its_own_on_clickhouse():
    ch = d.TargetClickHouse([_PG, _MY], "ch.local")
    schemas = ("Sales", "sales", "sales-eu", "sales_eu", "vendite_città", "vendite_citt", "продажи", "склад", "default", "Vendite Più")
    names = {s: ch.database_per(1, s, "orders") for s in schemas}
    assert len(set(names.values())) == len(schemas)
    assert {f["name"]: f["schema"] for f in ch.federated.values()} == {n: s for s, n in names.items()}
    # a schema that is already a plain lowercase name keeps the database it always had
    assert names["sales"] == "tab_src_1_sales" and names["sales_eu"] == "tab_src_1_sales_eu" and names["default"] == "tab_src_1_default"
    assert ch.database_per(2, "shop") == "tab_src_2_shop"
    # the others: the readable part and a short fingerprint of the exact name, the same at every export
    assert re.fullmatch(r"tab_src_1_sales_[0-9a-f]{10}", names["Sales"]) and re.fullmatch(r"tab_src_1_default_[0-9a-f]{10}", names["продажи"])
    assert names == {s: d.federated_db_name(1, s) for s in schemas}


# #25 #26 — a datasource query translated for the ClickHouse target: ClickHouse 24 hands a typed date
# constant to the PostgreSQL/MySQL table as a number of days (`"nato" < 0`: Postgres refuses the query,
# found by the real dbt proof), and its upper/substring work on bytes as everywhere on ClickHouse
def test_a_translated_query_compares_dates_by_their_text_and_keeps_characters_and_wide_types():
    ch = d.TargetClickHouse([_PG], "ch.local")
    sql = " ".join(ch.translate_query("SELECT id, upper(nome) AS u, substring(nome, 2, 3) AS s, CAST(visto AS date) AS giorno FROM zz.persone "
                                    "WHERE nato < DATE '1970-01-01' AND visto >= '2024-01-01 10:00'::timestamp "
                                    "AND nato IN (DATE '1955-03-12', DATE '1969-12-31') AND nato BETWEEN DATE '1900-01-01' AND CAST('1970-01-01' AS date)",
                                    1, "q").split())
    assert "nato < '1970-01-01'" in sql and "visto >= '2024-01-01 10:00'" in sql
    assert "nato IN ('1955-03-12', '1969-12-31')" in sql and "nato BETWEEN '1900-01-01' AND '1970-01-01'" in sql
    assert "upperUTF8(nome)" in sql and "substringUTF8(nome, 2, 3)" in sql and "CAST(visto AS Nullable(Date32))" in sql
    assert "Nullable(DATE)" not in sql and "Nullable(DateTime)" not in sql
    # a MySQL query too: its DATETIME is an instant to the microsecond, with no range of 1970-2106
    my = d.TargetClickHouse([_MY], "ch.local").translate_query("SELECT CAST(quando AS DATETIME) AS q, lower(nome) AS n FROM shop.t "
                                                             "WHERE quando >= TIMESTAMP '1960-01-01 00:00:00'", 2, "m")
    assert "Nullable(DateTime64(6))" in my and "lowerUTF8(nome)" in my and "quando >= '1960-01-01 00:00:00'" in " ".join(my.split())


# #25 (found by the proof) — ClickHouse's PostgreSQL engine reads a timestamptz as the wall-clock time of
# its Postgres session and drops the offset: on a server in Europe/Rome 10:30 UTC became 12:30
def test_the_readme_says_how_clickhouse_reads_a_postgres_timestamptz_right():
    files = _export({"id": "Int32", "ts": _UTC}, [], mode="clickhouse")
    assert "ALTER ROLE shop SET timezone = 'UTC'" in files["README.md"]
    assert "ALTER ROLE" not in _export({"id": "Int32", "ts": _DT}, [], mode="clickhouse")["README.md"]
    # a datasource query with such a column too
    raw = [{"name": "src_q", "kind": "raw", "materialized": "ephemeral", "raw_sql": "SELECT id, ts FROM v.ordini", "translate": {"connection_id": 1},
            "columns": [{"name": "id", "dtype": "Int32"}, {"name": "ts", "dtype": _UTC}]}]
    q = _files(_export_request(models=raw, source_map={}, mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"}))
    assert "ALTER ROLE shop SET timezone = 'UTC'" in q["README.md"]


# G2 handoff 1 — a source() macro the gateway writes for a table name with a backslash (`'\\'` in Jinja)
# was used as a re.sub template: the backslashes were processed and dbt could not parse the model
def test_a_source_macro_with_backslashes_reaches_the_model_exactly():
    macro = "{{ source('src_vendite', 'o' ~ \"'\" ~ 'brien' ~ '\\\\' ~ 'x') }}"
    sql = d.substitute_source_idents('SELECT * FROM "__s_0" AS "__s_0"', {"__s_0": macro})
    assert sql == f'SELECT * FROM {macro} AS "__s_0"'


# ── E5: the data contract, the seeds and the guides the export writes ───────────
import decimal as _decimal

from app.contracts.evaluator import evaluate as _evaluate

_PG_TYPE_OF = {"String": "TEXT", "Int64": "BIGINT", "Date": "DATE", "Boolean": "BOOLEAN", "Float64": "DOUBLE PRECISION"}
_CH_TYPE_OF = {"String": "Nullable(String)", "Int64": "Nullable(Int64)", "Date": "Nullable(Date32)", "Boolean": "Nullable(Bool)",
               "Float64": "Nullable(Float64)"}


def _type_in(dtype: str, where: str) -> str:
    if dtype.startswith("Datetime"):
        return "TIMESTAMP" if where == "postgres" else "Nullable(DateTime64(6))"
    m = re.match(r"Decimal\(precision=(\d+), scale=(\d+)\)", dtype)
    if m:
        return f"NUMERIC({m[1]},{m[2]})" if where == "postgres" else f"Nullable(Decimal({m[1]}, {m[2]}))"
    return (_PG_TYPE_OF if where == "postgres" else _CH_TYPE_OF)[dtype]


def _relation(df: "_pl.DataFrame", where: str, with_contract=None) -> str:
    """The rows of a model as a relation of `dove` (duckdb: a table in `con`; postgres and
    clickhouse: a typed VALUES, nothing written in the database)."""
    if where == "duckdb":
        with_contract.register("_df_uno", df.to_arrow())
        with_contract.execute('CREATE OR REPLACE TABLE "uno_dati" AS SELECT * FROM "_df_uno"')
        return '"uno_dati"'
    types = {c: _type_in(str(t), where) for c, t in df.schema.items()}

    def lit(v, t):
        if v is None:
            return f"CAST(NULL AS {t})" if where == "postgres" else "NULL"
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        text = str(v)
        if where == "postgres":
            return "CAST('" + text.replace("'", "''") + f"' AS {t})"
        if isinstance(v, (int, float, _decimal.Decimal)):
            return text
        return "'" + text.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r") + "'"
    rows = [", ".join(lit(v, types[c]) for c, v in r.items()) for r in df.to_dicts()]
    if where == "postgres":
        names = ", ".join(d._qi(c) for c in df.columns)
        return "(SELECT * FROM (VALUES " + ", ".join(f"({r})" for r in rows) + f") AS v({names}))"
    structure = ", ".join(f"{c} {t}" for c, t in types.items())
    return f"(SELECT * FROM values('{structure}', " + ", ".join(f"({r})" for r in rows) + "))"


def _test_rows(files: dict, df: "_pl.DataFrame", where: str) -> dict:
    """Every singular test of the project → the rows it selects (dbt's failures) on `dove`,
    over a model `one` whose rows are `df`."""
    import duckdb

    with_contract = duckdb.connect() if where == "duckdb" else None
    rel = _relation(df, where, with_contract)
    out = {}
    for path, text in sorted(files.items()):
        if not path.startswith("tests/"):
            continue
        sql = _render_like_dbt(text, {("ref", "uno"): rel}, config={})
        if where == "duckdb":
            out[path] = len(with_contract.execute(sql).fetchall())
        elif where == "postgres":
            out[path] = len(_su_postgres(sql))
        else:
            out[path] = len(_su_clickhouse(sql))
    return out


def _export_contract(df: "_pl.DataFrame", rules: list | None = None, columns: list | None = None, mode: str = "duckdb") -> dict:
    """The project the route writes for one model `one` that publishes `df` as it is, with the
    contract's tests as the gateway sends them (singular rules; column tests)."""
    types = {c: str(t) for c, t in df.schema.items()}
    if mode == "duckdb":
        item, extra = {"ref": "{{ source('db_1_v', 'dati') }}"}, {}
    else:
        item = {"ident": "__s_0", "macro": "{{ source('db_1_v', 'dati') }}"}
        extra = {"native": _PG_NATIVE} if mode == "native" else {"clickhouse": {"connections": [_PG], "target_schema": "dbt_tabularia"}}
    return _files(_export_request(mode=mode, **extra, source_map={"k": {**item, "columns": list(df.columns), "types": types, "label": "dati"}},
                                    models=[{"name": "uno", "source": {"key": "k"}, "operations": [], "tests": list(rules or []),
                                             "columns": list(columns or [])}]))


_WHERE = {"duckdb": "duckdb", "native": "postgres", "clickhouse": "clickhouse"}


# #31 — a pattern rule matches the WHOLE value, read as text, as Tabularia's contract check does:
# the export wrote a search (an unanchored '[0-9]{5}' passed '123456') on the column as it is
# (a pattern on an integer column made the dbt test ERROR on every warehouse)
_PATTERN = _pl.DataFrame({
    "cap": ["00100", "123456", "20100", None, "7010", "a\nb"],
    "codice": ["AB-12", "xAB-12y", "C-1", "AB\n-12", None, ""],
    "anno": [2021, 1999, 2024, None, 2023, 20],
    "giorno": [_dt.date(2024, 1, 5), _dt.date(2023, 12, 31), None, _dt.date(2024, 1, 31), _dt.date(2025, 1, 1), _dt.date(1999, 1, 2)],
    "visto": [_dt.datetime(2024, 1, 5, 10, 30), _dt.datetime(2024, 1, 5, 10, 30, 0, 123456), None, _dt.datetime(2024, 1, 5, 23, 59, 59),
              _dt.datetime(1999, 12, 31, 0, 0), _dt.datetime(2024, 2, 1, 8, 15, 0, 120000)],
    "attivo": [True, False, None, True, False, True],
    "prezzo": _pl.Series([_decimal.Decimal("1.50"), _decimal.Decimal("2.00"), None, _decimal.Decimal("10.25"), _decimal.Decimal("-3.10"),
                          _decimal.Decimal("7.00")], dtype=_pl.Decimal(10, 2)),
    "misura": [1.0, 2.5, None, 10.0, -0.5, 100.0],
})
_PATTERN_RULES = [
    ("cap", "[0-9]{5}"), ("codice", "[A-Z]+-[0-9]+"), ("codice", ".+"), ("codice", "(?i)ab-12"), ("codice", "[^x]+"),
    ("anno", "20[0-9]{2}"), ("giorno", r"\d{4}-01-\d{2}"), ("visto", r".*:00\.000000"), ("attivo", "true"),
    ("prezzo", r"-?[0-9]+\.[0-9]{2}"), ("misura", r"-?[0-9]+\.[0-9]"),
]


@pytest.mark.parametrize("mode", ["duckdb", "native", "clickhouse"])
def test_a_pattern_rule_fails_in_dbt_exactly_where_tabularia_s_contract_does(mode):
    rules = [{"id": f"r{i}", "kind": "pattern", "column": c, "regex": r, "severity": "error"} for i, (c, r) in enumerate(_PATTERN_RULES, 1)]
    expected_list = {f"tests/uno__pattern_{x['id']}.sql": x["violations"] for x in _evaluate(_PATTERN.lazy(), {"rules": rules})["rules"]}
    assert [expected_list[f"tests/uno__pattern_r{i}.sql"] for i in range(1, 12)] == [3, 3, 2, 4, 2, 2, 1, 3, 2, 0, 0]
    files = _export_contract(_PATTERN, rules, mode=mode)
    assert _test_rows(files, _PATTERN, _WHERE[mode]) == expected_list


_REGEX = ["a.b", ".+", "[.]b", r"a\.b", "[]a.]+", "[^]x]+", "[[:alpha:].]+", "(?s)a.b", "(?i)A.B", r"a\\.b", "[a-c.]{3}"]
_REGEX_TEXTS = ["a.b", "a\nb", "axb", "ab", ".b", "]a.", "a]b", "AxB", "a\\xb", "a\\\nb", "b.c", "x"]


@pytest.mark.parametrize("where", ["duckdb", "postgres", "clickhouse"])
def test_the_regex_of_a_pattern_rule_reads_newlines_and_flags_as_tabularia_does(where):
    """Polars' `.` does not match a newline; Postgres' and ClickHouse's do. Postgres reads
    inline flags only at the start of the pattern, so `^(?:(?i)…)$` made the test ERROR."""
    import duckdb

    expected_list = {r: _pl.Series(_REGEX_TEXTS).str.contains(f"^(?:{r})$").to_list() for r in _REGEX}
    dialect = {"duckdb": None, "postgres": "postgres", "clickhouse": "clickhouse"}[where]
    sql = "SELECT " + ", ".join(f"regexp_matches({d._lit(v)}, {d._lit(d._whole_regex(r, dialect))})" for r in _REGEX for v in _REGEX_TEXTS)
    if where == "duckdb":
        line = duckdb.connect().execute(sql).fetchone()
    else:
        translated_one = d.transpile_model(sql, dialect, {})
        line = (_su_postgres(translated_one) if where == "postgres" else _su_clickhouse(translated_one))[0]
    got = {r: [bool(x) for x in line[i * len(_REGEX_TEXTS):(i + 1) * len(_REGEX_TEXTS)]] for i, r in enumerate(_REGEX)}
    assert got == expected_list


def _generic_tests(schema_yml: str) -> dict:
    """models/schema.yml → {(model, column): {"quote": bool, "tests": [(kind, severity)]}}: the
    generic tests dbt builds from it (only the forms the exporter writes)."""
    import json

    out, model, column = {}, None, None
    for line in schema_yml.splitlines():
        if line.startswith("  - name: "):
            model = line[len("  - name: "):].strip('"')
        elif line.startswith("      - name: "):
            column = json.loads(line[len("      - name: "):])
            out[(model, column)] = {"quote": False, "tests": []}
        elif line == "        quote: true":
            out[(model, column)]["quote"] = True
        elif m := re.fullmatch(r"          - (\w+):", line):
            out[(model, column)]["tests"].append([m.group(1), None])
        elif m := re.fullmatch(r"              config: \{severity: (\w+)\}", line):
            out[(model, column)]["tests"][-1][1] = m.group(1)
    return {k: {"quote": v["quote"], "tests": [tuple(t) for t in v["tests"]]} for k, v in out.items() if v["tests"]}


def _not_null_like_dbt(files: dict, df: "_pl.DataFrame", where: str) -> dict:
    """dbt's own not_null test (default__test_not_null) on `dove`, with the column name as dbt
    puts it in the SQL: quoted by the adapter (`"…"`) only with `quote: true`."""
    import duckdb

    with_contract = duckdb.connect() if where == "duckdb" else None
    rel = _relation(df, where, with_contract)
    out = {}
    for (_, col), v in _generic_tests(files["models/schema.yml"]).items():
        if col not in df.columns:
            continue
        for kind, sev in v["tests"]:
            assert kind == "not_null", kind
            name = f'"{col}"' if v["quote"] else col
            sql = f"select {name} from {rel} AS m where {name} is null"
            out[(col, sev)] = len(with_contract.execute(sql).fetchall() if where == "duckdb" else _su_postgres(sql))
    return out


# #33 — dbt's generic tests put the column name in the SQL as it is: a space or a reserved word
# broke the test on every warehouse, and Postgres read «Canale» as canale
_NAMES = _pl.DataFrame({"Nome Cliente": ["Anna", None, "Bruno"], "Canale": [None, "web", None], "select": ["x", "y", "z"],
                       'il "codice"': ["a", None, "c"]})


@pytest.mark.parametrize("mode", ["duckdb", "native"])
def test_a_not_null_rule_on_a_column_with_spaces_upper_case_or_a_reserved_word_runs(mode):
    columns = [{"name": c, "description": "", "tests": [{"kind": "not_null", "severity": "error"}]} for c in _NAMES.columns]
    rules = [{"id": f"r{i}", "kind": "not_null", "column": c, "severity": "error"} for i, c in enumerate(_NAMES.columns)]
    expected_list = {x["column"]: x["violations"] for x in _evaluate(_NAMES.lazy(), {"rules": rules})["rules"]}
    files = _export_contract(_NAMES, columns=columns, mode=mode)
    got = {c: n for (c, _), n in _not_null_like_dbt(files, _NAMES.drop('il "codice"'), _WHERE[mode]).items() if c != 'il "codice"'}
    generic = _generic_tests(files["models/schema.yml"])
    # a name with a double quote cannot be quoted by the adapter (it writes "…" as it is): a singular test
    assert set(c for _, c in generic) == {"Nome Cliente", "Canale", "select"} and all(v["quote"] for v in generic.values())
    got |= {'il "codice"': n for p, n in _test_rows(files, _NAMES, _WHERE[mode]).items() if "not_null" in p}
    assert got == expected_list == {"Nome Cliente": 1, "Canale": 2, "select": 0, 'il "codice"': 1}


# #34 — dbt's accepted_values writes every value as '{{ value }}': an apostrophe (Sant'Antonio,
# L'Aquila) broke the SQL; a 3.0 on an integer column failed on Postgres and ClickHouse, where
# Tabularia compares numbers as numbers, booleans as booleans and anything else by its text
_ACCEPTED = _pl.DataFrame({
    "citta": ["Roma", "Sant'Antonio", "Bari", None, "C:\\temp", "L'Aquila"],
    "n": [1, 2, 3, None, 2, 1],
    "f": [1.0, 2.5, 3.0, None, 2.5, 1.0],
    "ok": [True, False, True, None, True, True],
    "giorno": [_dt.date(2024, 1, 5), _dt.date(2024, 1, 6), None, _dt.date(2024, 1, 5), _dt.date(2024, 1, 5), _dt.date(2024, 1, 7)],
})
_ACCEPTED_RULES = [
    ("citta", ["Roma", "Milano", "Bari", "Sant'Antonio", "C:\\temp", "L'Aquila"]), ("citta", ["Roma", "Sant'Antonio"]),
    ("n", [1, 2, 3.0]), ("n", ["1", "2"]), ("n", [1, "due"]), ("f", [1, 2.5]), ("ok", ["true"]), ("ok", [True, False]),
    ("giorno", ["2024-01-05", "2024-01-06"]),
]


@pytest.mark.parametrize("mode", ["duckdb", "native", "clickhouse"])
def test_an_accepted_values_rule_fails_in_dbt_exactly_where_tabularia_s_contract_does(mode):
    rules = [{"id": f"r{i}", "kind": "accepted_values", "column": c, "values": v, "severity": "warning"}
              for i, (c, v) in enumerate(_ACCEPTED_RULES, 1)]
    expected_list = [x["violations"] for x in _evaluate(_ACCEPTED.lazy(), {"rules": rules})["rules"]]
    assert expected_list == [0, 3, 0, 1, 3, 1, 1, 0, 1]
    columns = {}
    for c, v in _ACCEPTED_RULES:
        columns.setdefault(c, {"name": c, "description": "", "tests": []})["tests"].append({"kind": "accepted_values", "severity": "warn", "values": v})
    files = _export_contract(_ACCEPTED, columns=list(columns.values()), mode=mode)
    rows = _test_rows(files, _ACCEPTED, _WHERE[mode])
    names = ["citta", "citta_2", "n", "n_2", "n_3", "f", "ok", "ok_2", "giorno"]
    assert rows == {f"tests/uno__accepted_values_{n}.sql": a for n, a in zip(names, expected_list)}
    assert "accepted_values" not in files["models/schema.yml"]
    assert files["tests/uno__accepted_values_citta.sql"].startswith(
        "{{ config(severity='warn') }}\n-- Contract rule accepted_values on uno, column «citta»\n")


# #35 (decision 7) — a one-column unique rule: Tabularia counts two NULLs as a duplicate (NULLs
# count as values), dbt's generic `unique` filters them out (`where col is not null`) and passed
@pytest.mark.parametrize("mode", ["duckdb", "native", "clickhouse"])
@pytest.mark.parametrize("values,viola", [(["A1", None, None, "B2"], True), (["A1", None, "B2", "B2"], True), (["A1", None, "B2", "C3"], False)])
def test_a_one_column_unique_rule_counts_two_nulls_as_a_duplicate_like_tabularia(mode, values, viola):
    df = _pl.DataFrame({"codice": values, "n": [1, 2, 3, 4]})
    outcome = _evaluate(df.lazy(), {"rules": [{"id": "r1", "kind": "unique", "columns": ["codice"], "severity": "error"}]})["rules"][0]
    assert (outcome["violations"] > 0) is viola
    files = _export_contract(df, columns=[{"name": "codice", "description": "", "tests": [{"kind": "unique", "severity": "error"}]}], mode=mode)
    assert "unique" not in files["models/schema.yml"]
    assert {p: n > 0 for p, n in _test_rows(files, df, _WHERE[mode]).items()} == {"tests/uno__unique_codice.sql": viola}


# #40 — two rules of one generic kind on one column (a warning the proposal made plus an error
# added by hand) were two tests with the same name: dbt refused the whole project at parse
def test_two_rules_of_one_kind_on_one_column_never_give_two_tests_with_the_same_name():
    df = _pl.DataFrame({"codice": ["a", None, "a", "c"], "Codice": ["x", "y", "x", None]})
    dup = lambda kind, **kw: [{"kind": kind, "severity": "warn", **kw}, {"kind": kind, "severity": "error", **kw}]   # noqa: E731
    columns = [{"name": "codice", "description": "", "tests": dup("not_null") + dup("unique") + dup("accepted_values", values=["a", "b"])},
               {"name": "Codice", "description": "", "tests": dup("not_null") + dup("unique")}]
    files = _export_contract(df, columns=columns, mode="native")
    generic = _generic_tests(files["models/schema.yml"])
    # the same check twice is one test, with the stricter severity: it fails where the error rule fails
    assert generic == {("uno", "codice"): {"quote": True, "tests": [("not_null", "error")]},
                        ("uno", "Codice"): {"quote": True, "tests": [("not_null", "error")]}}
    singular = {p: re.match(r"\{\{ config\(severity='(\w+)'\) \}\}", t).group(1) for p, t in files.items() if p.startswith("tests/")}
    assert singular == {"tests/uno__unique_codice.sql": "warn", "tests/uno__unique_codice_2.sql": "error",
                         "tests/uno__accepted_values_codice.sql": "warn", "tests/uno__accepted_values_codice_2.sql": "error",
                         "tests/uno__unique_codice_3.sql": "warn", "tests/uno__unique_codice_4.sql": "error"}
    assert "«Codice»" in files["tests/uno__unique_codice_3.sql"] and "«codice»" in files["tests/uno__unique_codice.sql"]
    assert _test_rows(files, df, "duckdb") == {"tests/uno__unique_codice.sql": 1, "tests/uno__unique_codice_2.sql": 1,
                                                     "tests/uno__accepted_values_codice.sql": 1, "tests/uno__accepted_values_codice_2.sql": 1,
                                                     "tests/uno__unique_codice_3.sql": 1, "tests/uno__unique_codice_4.sql": 1}


# #36 — a file datasource is a seed, and dbt's seed loaders change some texts: dbt-duckdb's COPY
# reads "" as NULL; agate (dbt-postgres, dbt-clickhouse) reads '', 'null' in any case and blank
# text as NULL and turns \r into \n; ClickHouse's CSV input cuts spaces and tabs at the ends and
# reads \N as NULL. Tabularia keeps every one of them: is_null, fill_null and joins differed.
_SEED_TEXTS = ["Anna", "", None, "null", "NULL", " Null ", "  ", "\t", "\xa0", " a ", "a ", "\tx", "x\t", "\\N", "a\\Nb", "\r",
                   "x\r\ny", "a\rb", "~", "~x", "~0", "~1~", "~~", '"q"', "a,b", "a\nb", "\u2028", "N/A", "Città", "x~1y", "'q'", "'"]
_SEED = _pl.DataFrame({"id": list(range(1, len(_SEED_TEXTS) + 1)), "v": _SEED_TEXTS,
                       "w": ["plain"] * len(_SEED_TEXTS), "n": [None if i % 3 else i for i in range(len(_SEED_TEXTS))]})


def _export_seed(df: "_pl.DataFrame", mode: str, monkeypatch) -> dict:
    import app.utils
    from tests.conftest import upload_df
    from tests.fakes import FakeStorage

    storage = FakeStorage()
    upload_df(storage, df, "datasets/seed_dati.parquet")
    monkeypatch.setattr(app.utils, "get_storage_service", lambda: storage)
    extra = {"native": _PG_NATIVE} if mode == "native" else ({"clickhouse": {"connections": [_PG], "target_schema": "s"}} if mode == "clickhouse" else {})
    return _files(_export_request(mode=mode, **extra, source_map={},
                                    seeds=[{"name": "seed_dati", **_key("seed_dati"), "description": "", "columns": []}],
                                    models=[{"name": "uno", "source": {"seed": "seed_dati"}, "operations": []}]))


def _like_agate(csv_text: str) -> list:
    """What dbt's seed loader (dbt_common agate_helper.from_csv, column_types given) makes of
    the CSV: a file read with Python's universal newlines, every typed column as agate Text
    whose null values are '' and 'null' (compared stripped and lower-cased)."""
    import csv, os, tempfile

    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(csv_text)
    try:
        with open(path, encoding="utf-8") as f:
            rows = list(csv.reader(f))
    finally:
        os.remove(path)
    return [[None if v.strip().lower() in ("", "null") else v for v in r] for r in rows[1:]]


def _loaded_seed(files: dict, where: str, with_contract=None) -> str:
    """The seed table as the warehouse's dbt adapter loads it, as a relation of `dove`."""
    import csv, io, os, tempfile

    text = files["seeds/seed_dati.csv"]
    if where == "duckdb":
        # dbt-duckdb: COPY … FROM '<seed file>' (FORMAT CSV, HEADER TRUE, DELIMITER ',')
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        with_contract.execute('CREATE TABLE "seed_dati" ("id" BIGINT, "v" VARCHAR, "w" VARCHAR, "n" BIGINT)')
        with_contract.execute(f"""COPY "seed_dati" FROM '{path}' (FORMAT CSV, HEADER TRUE, DELIMITER ',')""")
        os.remove(path)
        return '"seed_dati"'
    rows = _like_agate(text)
    if where == "postgres":
        lit = lambda v, t: f"CAST(NULL AS {t})" if v is None else "CAST('" + v.replace("'", "''") + f"' AS {t})"   # noqa: E731
        return ('(SELECT * FROM (VALUES ' + ", ".join(f"({lit(r[0], 'BIGINT')}, {lit(r[1], 'TEXT')}, {lit(r[2], 'TEXT')}, {lit(r[3], 'BIGINT')})"
                                                      for r in rows) + ') AS v("id", "v", "w", "n"))')
    # dbt-clickhouse: the agate rows written back with csv.writer, then INSERT … FORMAT CSV
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(["" if v is None else v for v in r] for r in rows)
    cli = _clickhouse()
    cli.command('CREATE TEMPORARY TABLE "seed_dati" ("id" Nullable(Int64), "v" Nullable(String), "w" Nullable(String), "n" Nullable(Int64))')
    cli.command('INSERT INTO "seed_dati" FORMAT CSV\n' + buf.getvalue())
    with_contract.append(cli)
    return '"seed_dati"'


@pytest.mark.parametrize("mode", ["duckdb", "native", "clickhouse"])
def test_a_seed_gives_the_models_every_text_tabularia_reads_even_the_ones_dbt_s_loaders_change(mode, monkeypatch):
    import duckdb

    files = _export_seed(_SEED, mode, monkeypatch)
    where = _WHERE[mode]
    with_contract = duckdb.connect() if where == "duckdb" else []
    rel = _loaded_seed(files, where, with_contract)
    sql = _render_like_dbt(files["models/uno.sql"], {("ref", "seed_dati"): rel}, config={})
    if where == "duckdb":
        rows = with_contract.execute(sql).fetchall()
    elif where == "postgres":
        rows = _su_postgres(sql)
    else:
        rows = [tuple(r) for r in with_contract[0].query(sql).result_rows]
    assert sorted(rows, key=lambda r: r[0]) == list(_SEED.iter_rows())
    # only the column that needs it is written another way; the README says so
    import csv, io

    assert [r[2] for r in csv.reader(io.StringIO(files["seeds/seed_dati.csv"], newline=""))][1:] == ["plain"] * len(_SEED_TEXTS)
    assert "`seed_dati`" in files["README.md"] and "`v`" in files["README.md"] and "`w`" not in files["README.md"]


def test_a_seed_without_texts_the_loaders_change_is_written_as_it_was(monkeypatch):
    df = _pl.DataFrame({"id": [1, 2, 3], "v": ["a", None, "b c"]})
    files = _export_seed(df, "duckdb", monkeypatch)
    assert files["seeds/seed_dati.csv"] == df.write_csv()
    assert "seed_dati" not in files["README.md"].split("## Notes")[-1]


# #70 (decision 10) — «already declared» means the team's own sources.yml declares the source: that
# exists only for the folder package. A complete project left the source out of its sources.yml,
# and `dbt parse` failed: «depends on a source named 'db_1_vendite.ordini' which was not found»
@pytest.mark.parametrize("mode", ["duckdb", "clickhouse"])
@pytest.mark.parametrize("layout", [None, {"package": "project", "folder": "", "layers": False}])
def test_already_declared_sources_are_ignored_outside_the_folder_package(mode, layout):
    sources = [{"name": "db_1_vendite", "schema": "vendite", "connection_id": 1, "declared": True, "database": "db_1",
                 "tables": [{"name": "ordini", "description": "", "columns": []}]}]
    extra = {"clickhouse": {"connections": [_PG], "target_schema": "s"}} if mode == "clickhouse" else {}
    item = {"ident": "__s_0", "macro": "{{ source('db_1_vendite', 'ordini') }}"} if mode == "clickhouse" else {"ref": "{{ source('db_1_vendite', 'ordini') }}"}
    files = _files(_export_request(mode=mode, **extra, layout=layout, sources=sources,
                                     source_map={"k": {**item, "columns": ["id"], "types": {"id": "Int64"}, "label": "ordini"}},
                                     models=[{"name": "uno", "source": {"key": "k"}, "operations": []}]))
    assert "  - name: db_1_vendite\n" in files["models/sources.yml"] and "      - name: ordini\n" in files["models/sources.yml"]
    if mode == "clickhouse":
        # and the macro creates the ClickHouse database the source reads
        assert "tab_src_1_vendite" in files["macros/create_tabularia_sources.sql"]
    folder = _files(_export_request(mode=mode, **extra, layout={"package": "folder", "folder": "f"}, sources=sources,
                                        source_map={"k": {**item, "columns": ["id"], "types": {"id": "Int64"}, "label": "ordini"}},
                                        models=[{"name": "uno", "source": {"key": "k"}, "operations": []}]))
    assert "models/f/sources.yml" not in folder and "`db_1_vendite` is **declared in your project**" in folder["INTEGRATION.md"]


# #81 — on the federated target the models land in DuckDB's `main` unless a schema is chosen
# (dbt-duckdb's default; the profile names no schema). The docs said `dbt_tabularia`, and the
# README said nothing: whoever queried dbt_tabularia.<model> found no such schema
@pytest.mark.parametrize("schema,where", [(None, "main"), ("analisi", "analisi")])
def test_the_readme_says_where_the_federated_models_land(schema, where):
    files = _files(_export_request(schema=schema))
    profile = files["profiles.yml"]
    assert ("schema:" in profile) is (schema is not None)
    assert f"The models are built in `margini.duckdb`, in the schema `{where}`" in files["README.md"]


# #41 — the folder package goes into the team's project, where a tag like `sales` is common: the
# documented `dbt build -s +tag:sales` also built their models tagged `sales` and all they read
def test_the_folder_package_selects_only_what_came_from_tabularia():
    models = [{"name": "sales_out", "kind": "output", "sql": "select * from {{ ref('seed_dati') }}", "materialized": "table",
                "description": "", "columns": [], "meta": {"flow": "Sales", "flow_id": 3}, "tags": ["tabularia", "sales"]}]
    files = _project(flow_name="Sales", models=models, layout={"package": "folder", "folder": "tabularia_sales"},
                      seeds=[{"name": "seed_dati", "csv": "id\n1\n", "description": "", "columns": [], "column_types": {"id": "BIGINT"}}])
    guide = files["INTEGRATION.md"]
    # the flow's tag AND tabularia's: a model of theirs tagged `sales` (and what it reads) is not ours
    assert "dbt build -s +tag:sales,tag:tabularia\n" in guide and "dbt build -s +tag:sales\n" not in guide
    # the seeds carry the tag too, so the selection still loads them
    assert "  - name: seed_dati\n    config:\n      tags: [\"tabularia\"]\n      column_types:\n" in files["seeds/tabularia_sales/schema.yml"]


def test_an_accepted_values_rule_tabularia_cannot_evaluate_is_refused_with_its_code():
    """On a boolean column Tabularia reads true/false/1/0 only: 'yes' makes its check fail the
    rule every time (it cannot evaluate it). The export says so instead of guessing."""
    from fastapi import HTTPException

    df = _pl.DataFrame({"ok": [True, False]})
    outcome = _evaluate(df.lazy(), {"rules": [{"id": "r1", "kind": "accepted_values", "column": "ok", "values": ["yes"], "severity": "error"}]})
    assert outcome["rules"][0]["passed"] is False and "not a boolean" in outcome["rules"][0]["error"]
    with pytest.raises(HTTPException) as e:
        _export_contract(df, columns=[{"name": "ok", "description": "", "tests": [{"kind": "accepted_values", "severity": "error", "values": ["yes"]}]}])
    assert e.value.status_code == 422 and e.value.detail["code"] == "contract_rule_invalid" and e.value.detail["params"] == {"rule": "accepted_values"}


# ── a text on several lines keeps its bytes (follow-up of the review) ────────────────────────
# The model's steps are indented line by line: the indentation went INSIDE a string literal or a
# quoted name that spans lines ('a\n  b' became 'a\n      b', and a filter on such a value kept
# no row). And dbt writes every line break of a model's file as `\n`: a `\r` in a value was lost.
_ROWS = "a\n  b\r\n\tc\rd \u2028e"


def _sql_of(text: str, dialect: str) -> str:
    """`_ROWS` as a string literal of the flow's text in `dialect`, its bytes raw."""
    return "'" + text.replace("'", "''") + "'" if dialect != "bigquery" else "'''" + text + "'''"


def _multiriga(target: str, dialect: str, ops: list) -> str:
    """The model `one` over the source `orders` (id, t) as the route writes it for `target`."""
    smap = {"columns": ["id", "t"], "types": {"id": "Int64", "t": "String"}, "label": "ordini"}
    if target == "duckdb":
        return _files(_export_request(source_map={"k": {"ref": "{{ source('db_1_v', 'ordini') }}", **smap}},
                                        models=[{"name": "uno", "source": {"key": "k"}, "operations": ops, "dialect": dialect}]))["models/uno.sql"]
    item = {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", **smap}
    extra = {"clickhouse": dict(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "dbt_tabularia"}),
             "native-pg": dict(mode="native", native=_PG_NATIVE), "native-ch": dict(mode="native", native=_NATIVE_CH)}[target]
    return _files(_export_request(**extra, source_map={"k": item},
                                    models=[{"name": "uno", "source": {"key": "k"}, "operations": ops, "dialect": dialect}]))["models/uno.sql"]


def _multiline_rows(target: str, text: str) -> list:
    """The rows of the model, rendered as dbt renders it, on the warehouse of `target`, over
    ordini = (1, _ROWS), (2, 'x')."""
    if target == "duckdb":
        import duckdb

        with_contract = duckdb.connect()
        with_contract.execute("CREATE TABLE ordini (id BIGINT, t VARCHAR)")
        with_contract.execute("INSERT INTO ordini VALUES (1, ?), (2, 'x')", [_ROWS])
        return with_contract.execute(f"SELECT * FROM ({_render_like_dbt(text, {('source', 'db_1_v', 'ordini'): 'ordini'}, config={})}) ORDER BY id").fetchall()
    if target == "native-pg":
        e = _ROWS.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        rel = f"(SELECT * FROM (VALUES (CAST(1 AS BIGINT), E'{e}'), (2, 'x')) AS v(id, t))"
        return sorted(_su_postgres(_render_like_dbt(text, {("source", "db_1_v", "ordini"): rel}, config={})))
    e = _ROWS.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    rel = f"(SELECT * FROM values('id Nullable(Int64), t Nullable(String)', (1, '{e}'), (2, 'x')))"
    return sorted(_su_clickhouse(_render_like_dbt(text, {("source", "db_1_v", "ordini"): rel}, config={})))


@pytest.mark.parametrize("target,dialect", [
    ("duckdb", "duckdb"), ("duckdb", "polars"), ("duckdb", "clickhouse"), ("duckdb", "bigquery"),
    ("native-pg", "duckdb"), ("native-pg", "clickhouse"),
    ("clickhouse", "duckdb"), ("clickhouse", "clickhouse"),
    ("native-ch", "duckdb"), ("native-ch", "clickhouse"),
])
def test_a_text_on_several_lines_keeps_its_bytes_on_every_target(target, dialect):
    ops = [{"type": "compute", "params": {"columns": [{"name": "m", "expr": _sql_of(_ROWS, dialect)}]}},
           {"type": "filter", "params": {"column": "t", "operator": "eq", "value": _ROWS}},
           {"type": "fill_null", "params": {"columns": {"m": _ROWS}}},
           {"type": "sql", "params": {"query": f"SELECT *, {_sql_of(_ROWS, dialect)} AS n FROM self"}}]
    assert _multiline_rows(target, _multiriga(target, dialect, ops)) == [(1, _ROWS, _ROWS, _ROWS)]


def test_a_name_on_several_lines_keeps_its_bytes():
    """A quoted name that spans lines (a column renamed in the flow) was indented inside too."""
    import duckdb

    name = "r\n  s\r\nt"
    text = _multiriga("duckdb", "duckdb", [{"type": "rename", "params": {"mapping": {"t": name}}},
                                            {"type": "filter", "params": {"column": name, "operator": "eq", "value": "x"}}])
    with_contract = duckdb.connect()
    with_contract.execute("CREATE TABLE ordini AS SELECT * FROM (VALUES (1, 'y'), (2, 'x')) v(id, t)")
    rel = with_contract.sql(_render_like_dbt(text, {("source", "db_1_v", "ordini"): "ordini"}, config={}))
    assert rel.columns == ["id", name] and rel.fetchall() == [(2, "x")]


def test_a_yaml_value_ending_with_a_line_break_keeps_it():
    """dbt renders every YAML value as a Jinja template, and Jinja drops a template's final line
    break: a column named «Total⏎» (a spreadsheet header ending with Alt+Enter) was «Total» in
    schema.yml, and its not_null rule failed at `dbt build` (dbt 1.12.5: «Referenced column
    "Total" not found»). The final one is printed by an expression, as a `\\r` is."""
    import json

    name = "Total\n"
    smap = {"k": {"ref": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "t"], "types": {"id": "Int64", "t": "String"}, "label": "ordini"}}
    files = _files(_export_request(source_map=smap, models=[{
        "name": "uno", "source": {"key": "k"}, "operations": [{"type": "rename", "params": {"mapping": {"t": name}}}],
        "description": "two lines\n\n", "meta": {"flow": "a\r\n"},
        "columns": [{"name": name, "description": "", "tests": [{"kind": "not_null", "severity": "error"}]}]}]))
    rows = files["models/schema.yml"].splitlines()
    value = lambda prefix: [_render_like_dbt(json.loads(r[len(prefix):])) for r in rows if r.startswith(prefix)]  # noqa: E731
    assert value("      - name: ") == [name]
    assert value("    description: ") == ["two lines\n\n"] and value("          flow: ") == ["a\r\n"]
    assert d._y("a\nb") == '"a\\nb"'   # a line break that does not end the value is written as before


# A DuckDB escape string (E'…') in a compute or a sql node. sqlglot reads its escapes its own way
# (`\\` is a backslash, but `\q` stays `\q` where DuckDB reads `q`, `\101` stays where DuckDB reads
# `A`) and writes an escape string back without doubling the backslash, so the warehouse read `\ `
# as an escape: 'a\ b' became 'a b' on native Postgres, and in a sql node on the federated target too
# (sqlglot rewrites the query). The value must be the one DuckDB, Tabularia's engine, gives.
_ESCAPE_DUCKDB = "E'a\\\\ b\\nc\\t\\'d\\q\\101\\x42 ''e'"


@pytest.mark.parametrize("target", ["duckdb", "native-pg", "clickhouse", "native-ch"])
@pytest.mark.parametrize("where", ["compute", "sql"])
def test_a_duckdb_escape_string_keeps_the_value_duckdb_gives_on_every_target(target, where):
    import duckdb

    [(expected,)] = duckdb.sql("SELECT " + _ESCAPE_DUCKDB).fetchall()
    assert expected == "a\\ b\nc\t'dqAB 'e"
    step = ({"type": "compute", "params": {"columns": [{"name": "m", "expr": _ESCAPE_DUCKDB}]}} if where == "compute"
             else {"type": "sql", "params": {"query": f"SELECT *, {_ESCAPE_DUCKDB} AS m FROM self"}})
    ops = [{"type": "filter", "params": {"column": "t", "operator": "eq", "value": _ROWS}}, step]
    assert _multiline_rows(target, _multiriga(target, "duckdb", ops)) == [(1, _ROWS, expected)]


@pytest.mark.parametrize("body", [
    "a\\\\b", "\\'", "''", "\\n\\t\\r\\b\\f", "\\v\\a\\q\\ \\\"", "\\101\\1018\\8", "\\x41\\x414\\x4\\x\\xZ",
    "\\\r\n\\\n", "città\\n", "-- no comment", "{{ x }}", "",
])
def test_an_escape_string_is_written_as_the_standard_string_duckdb_reads_it_as(body):
    import duckdb

    sql = f"SELECT E'{body}' AS a, 'x\\y' AS b, $$E'z'$$ AS c -- E'w'"
    written = d._standard_strings(sql)
    assert "E'" not in written.replace("$$E'z'$$", "").replace("-- E'w'", "")
    assert duckdb.sql(written).fetchall() == duckdb.sql(sql).fetchall()


def test_an_escape_string_duckdb_refuses_is_left_as_it_is():
    """A byte outside ASCII or a unicode escape: DuckDB refuses it (the flow does not run in
    Tabularia either), the export does not invent a value."""
    for body in ("\\xe8", "\\0", "\\u0041"):
        assert d._standard_strings(f"SELECT E'{body}'") == f"SELECT E'{body}'"


def test_the_steps_are_indented_as_before_when_no_text_spans_lines():
    sql, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "filter", "params": {"column": "canale", "operator": "eq", "value": "web"}}], _resolve)
    assert sql == ("WITH\nsource_k AS (\n    -- source: k\n    SELECT *\n    FROM {{ source('db', 'k') }}\n),\n"
                   "filter_canale AS (\n    -- keep rows where canale = 'web'\n    SELECT *\n    FROM source_k\n    WHERE \"canale\" = 'web'\n)\n"
                   "SELECT *\nFROM filter_canale")



# ── names an adapter cannot write: refused by the engine too (defence in depth) ─────────────
# The gateway refuses them before the download (`name_not_quotable`); a payload that reaches the
# engine anyway must not give a project that fails at `dbt run`. Seen with dbt 1.12.5: every
# adapter writes the names unescaped into its own SQL — `"…"` (dbt-postgres, dbt-duckdb) or `…`
# (dbt-clickhouse) around a relation, '…' in the queries that list relations and columns — and
# dbt-postgres compares the schema with ILIKE, where a backslash escapes.
def _with_names(mode: str, schema=None, alias=None, table="ordini", target_schema="dbt_tabularia", materialized="table",
              source_schema="v"):
    item = {"columns": ["id"], "types": {"id": "Int64"}, "label": table}
    sources = [{"name": "db_1_v", "database": "d", "schema": source_schema, "connection_id": 1,
                 "tables": [{"name": table, "description": "", "columns": []}]}]
    model = {"name": "uno", "source": {"key": "k"}, "operations": [], "materialized": materialized, "schema": schema, "alias": alias}
    if mode == "duckdb":
        return _export_request(sources=sources, source_map={"k": {"ref": "{{ source('db_1_v', 'x') }}", **item}}, models=[model])
    smap = {"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'x') }}", **item}}
    if mode == "clickhouse":
        return _export_request(mode="clickhouse", sources=sources, source_map=smap, models=[model],
                                 clickhouse={"connections": [_PG], "target_schema": target_schema})
    native = {**(_PG_NATIVE if mode == "native-pg" else _NATIVE_CH), "target_schema": target_schema}
    return _export_request(mode="native", native=native, sources=sources, source_map=smap, models=[model])


@pytest.mark.parametrize("mode,kw,name,character,adapter", [
    ("native-pg", {"alias": 'o"x'}, 'o"x', '"', "dbt-postgres"),
    ("native-pg", {"alias": "o'brien", "materialized": "incremental"}, "o'brien", "'", "dbt-postgres"),
    ("native-pg", {"alias": "o'brien"}, "o'brien", "'", "dbt-postgres"),
    ("native-pg", {"schema": "zz'q", "alias": "t"}, "zz'q", "'", "dbt-postgres"),
    ("native-pg", {"schema": "zz\\b", "alias": "t"}, "zz\\b", "\\", "dbt-postgres"),
    ("native-pg", {"target_schema": "a'b"}, "a'b", "'", "dbt-postgres"),
    ("native-pg", {"table": 'or"dini'}, 'or"dini', '"', "dbt-postgres"),
    ("native-ch", {"alias": "o'brien"}, "o'brien", "'", "dbt-clickhouse"),
    ("native-ch", {"schema": "zz'q", "alias": "t"}, "zz'q", "'", "dbt-clickhouse"),
    ("native-ch", {"alias": "a\\b"}, "a\\b", "\\", "dbt-clickhouse"),
    ("native-ch", {"alias": "a`b"}, "a`b", "`", "dbt-clickhouse"),
    ("native-ch", {"table": "or`dini"}, "or`dini", "`", "dbt-clickhouse"),
    ("clickhouse", {"table": "or\\dini"}, "or\\dini", "\\", "dbt-clickhouse"),
    ("clickhouse", {"target_schema": "a`b"}, "a`b", "`", "dbt-clickhouse"),
    ("duckdb", {"table": 'or"dini'}, 'or"dini', '"', "dbt-duckdb"),
    # a quote in a SOURCE's schema or table: `dbt build` passes, but `dbt docs generate` writes them
    # between '…' in its catalog query (dbt 1.12.5: «upper('o'tab')» a syntax error on all three)
    ("native-pg", {"table": "o'brien"}, "o'brien", "'", "dbt-postgres"),
    ("native-pg", {"source_schema": "zz'src"}, "zz'src", "'", "dbt-postgres"),
    ("duckdb", {"table": "o'brien"}, "o'brien", "'", "dbt-duckdb"),
    ("duckdb", {"source_schema": "zz'src"}, "zz'src", "'", "dbt-duckdb"),
    ("native-ch", {"table": "o'brien"}, "o'brien", "'", "dbt-clickhouse"),
    ("native-ch", {"source_schema": "zz'src"}, "zz'src", "'", "dbt-clickhouse"),
    ("clickhouse", {"table": "o'brien"}, "o'brien", "'", "dbt-clickhouse"),
])
def test_a_name_the_adapter_cannot_write_is_refused_by_the_engine_too(mode, kw, name, character, adapter):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        _files(_with_names(mode, **kw))
    assert e.value.status_code == 422 and e.value.detail["code"] == "name_not_quotable"
    assert e.value.detail["params"] == {"name": name, "character": character, "adapter": adapter}


@pytest.mark.parametrize("mode,kw", [
    ("native-pg", {"alias": "a\\b"}), ("native-pg", {"alias": "a\\b", "materialized": "incremental"}),
    ("native-pg", {"alias": "a }} b {{ c", "schema": "città"}), ("native-pg", {"table": "o\\brien", "source_schema": "zz\\b"}),
    ("native-ch", {"alias": 'x"y', "schema": 'zz"q'}), ("native-ch", {"table": 'o"brien'}),
])
def test_the_names_the_adapter_builds_are_kept(mode, kw):
    """Built twice by real dbt 1.12.5, then `dbt docs generate`. A SOURCE's name is quoted by
    source(), but a quote in it broke docs generate (see the refusals above): a backslash in a
    Postgres source's schema and table, a double quote in a ClickHouse source's table are kept
    (their catalog query compares with `=`, and Postgres reads '…' with standard_conforming_strings)."""
    files = _files(_with_names(mode, **kw))
    config: dict = {}
    _render_like_dbt(files["models/uno.sql"].splitlines()[0], config=config)
    assert config.get("alias") == kw.get("alias") and config.get("schema") == kw.get("schema")


# ── the probe's cost (review follow-up) ──────────────────────────────────────────────────────
# Each step is an EMPTY TABLE the next one binds against alone. As a view, every step re-bound the
# whole chain before it: the export time grew with the square of the steps (measured through the
# route, 100 steps over 300 columns: see the follow-up report). A guard that the probe keeps them so.
def test_the_probe_keeps_each_step_as_an_empty_table(monkeypatch):
    seen_ones: list = []
    close = d._Probe.close

    def look(self):
        if self._con is not None:
            seen_ones.extend(self._con.execute("SELECT table_name, 'BASE TABLE', estimated_size FROM duckdb_tables() "
                                           "UNION ALL SELECT view_name, 'VIEW', 0 FROM duckdb_views() WHERE NOT internal").fetchall())
        close(self)
    monkeypatch.setattr(d._Probe, "close", look)
    cols = [f"c{i}" for i in range(30)]
    ops = [op for k in range(10) for op in (
        {"type": "compute", "params": {"columns": [{"name": f"k{k}", "expr": f'"c{k}" * 2'}]}},
        {"type": "filter", "params": {"column": f"c{k + 1}", "operator": "gt", "value": 0}})]
    _, _, types = d.compile_model_sql({"key": "k"}, ops, lambda _: ("t_src", cols, {c: "BIGINT" for c in cols}, "src"))
    assert len(seen_ones) == 1 + len(ops) and {v[1] for v in seen_ones} == {"BASE TABLE"} and {v[2] for v in seen_ones} == {0}, seen_ones
    assert all(types[f"k{k}"] == "BIGINT" for k in range(10))   # what the probe reads, from the tables
