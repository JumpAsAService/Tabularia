"""L'exporter dbt del motore: la compilazione dell'IR in modelli (una CTE per
operazione, lo schema tracciato), la traduzione nel dialetto nativo con i suoi
buchi noti, i test singolari dai contratti e il progetto assemblato (schema.yml,
sorgenti con descrizioni, seed, macro dello schema, README)."""
import re

import pytest

from app.engine import dbt_export as d


def _resolve(src: dict):
    if "model" in src:
        return "{{ ref('%s') }}" % src["model"], ["id", "x"]
    if "seed" in src:
        return "{{ ref('%s') }}" % src["seed"], ["id", "y"]
    return "{{ source('db', '%s') }}" % src["key"].split("/")[-1], ["id", "canale", "importo"]


# ── compilazione ──────────────────────────────────────────────────────────────
def test_una_cte_per_operazione_e_lo_schema_segue_le_rinomine():
    sql, cols = d.compile_model_sql(
        {"key": "datasets/ordini"},
        [{"type": "rename", "params": {"mapping": {"importo": "ricavo"}}},
         {"type": "filter", "params": {"column": "canale", "operator": "eq", "value": "web"}},
         {"type": "group_by", "params": {"by": ["canale"], "aggregations": [{"column": "ricavo", "func": "sum", "alias": "tot"}]}}],
        _resolve,
    )
    assert cols == ["canale", "tot"]
    assert sql.startswith("WITH s1 AS (\n  SELECT * FROM {{ source('db', 'ordini') }}")
    assert "s2 AS (\n  SELECT * RENAME (\"importo\" AS \"ricavo\") FROM s1" in sql
    assert "WHERE \"canale\" = 'web'" in sql and "GROUP BY \"canale\"" in sql
    assert sql.endswith("SELECT * FROM s4")


def test_un_modello_puo_partire_da_un_altro_modello_o_da_un_seed():
    sql, cols = d.compile_model_sql({"model": "int_pulito"}, [{"type": "limit", "params": {"n": 5}}], _resolve)
    assert "FROM {{ ref('int_pulito') }}" in sql and cols == ["id", "x"]
    sql, cols = d.compile_model_sql({"seed": "seed_listino"}, [], _resolve)
    assert "FROM {{ ref('seed_listino') }}" in sql and cols == ["id", "y"]


def test_il_sort_del_publish_ignora_le_colonne_che_non_ci_sono():
    sql, _ = d.compile_model_sql({"key": "k"}, [{"type": "sort", "params": {"by": ["sparita", "id"], "ignore_missing": True}}], _resolve)
    assert 'ORDER BY "id" ASC' in sql and "sparita" not in sql
    sql, _ = d.compile_model_sql({"key": "k"}, [{"type": "sort", "params": {"by": ["sparita"], "ignore_missing": True}}], _resolve)
    assert "ORDER BY" not in sql


def test_join_con_ramo_destro_compilato_come_cte_e_colonne_right():
    sql, cols = d.compile_model_sql(
        {"key": "a"},
        [{"type": "join", "params": {"how": "left", "on": "id", "right": {"source": {"key": "b"}, "operations": [{"type": "select", "params": {"columns": ["id", "canale"]}}]}}}],
        _resolve,
    )
    assert cols == ["id", "canale", "importo", "canale_right"]
    assert "LEFT JOIN" in sql and "source('db', 'b')" in sql


def test_foreach_e_pivot_nativo_sono_rifiutati_con_un_messaggio_chiaro():
    with pytest.raises(d.DbtExportError, match="foreach"):
        d.compile_model_sql({"key": "k"}, [{"type": "foreach", "params": {}}], _resolve)
    piv = [{"type": "pivot", "params": {"index": ["id"], "on": ["canale"], "values": "importo"}}]
    sql, cols = d.compile_model_sql({"key": "k"}, piv, _resolve)            # in DuckDB si può
    assert "PIVOT" in sql and cols == []
    with pytest.raises(d.DbtExportError, match="federato"):
        d.compile_model_sql({"key": "k"}, piv, _resolve, native=True)


# ── dialetto nativo ───────────────────────────────────────────────────────────
def test_la_traduzione_postgres_espande_gli_star_e_mette_il_cast_a_round():
    raw, _ = d.compile_model_sql(
        {"key": "k"},
        [{"type": "compute", "params": {"columns": [{"name": "pct", "expr": 'ROUND("importo" / 3, 2)'}]}}],
        lambda src: ("__s_0", ["id", "canale", "importo"]),
    )
    sql = d.transpile_model(raw, "postgres", {"__s_0": ["id", "canale", "importo"]})
    assert "*" not in sql                                        # star espansi: colonne esplicite
    assert re.search(r'ROUND\(CAST\(.*AS DECIMAL\), 2\)', sql)   # Postgres non ha round(double, int)
    sql = d.substitute_source_idents(sql, {"__s_0": "{{ source('src', 'ordini') }}"})
    assert "{{ source('src', 'ordini') }} AS \"__s_0\"" in sql


def test_la_traduzione_clickhouse_non_tocca_round():
    raw, _ = d.compile_model_sql({"key": "k"}, [{"type": "compute", "params": {"columns": [{"name": "pct", "expr": 'ROUND("importo", 2)'}]}}],
                                 lambda src: ("__s_0", ["id", "importo"]))
    sql = d.transpile_model(raw, "clickhouse", {"__s_0": ["id", "importo"]})
    assert "DECIMAL" not in sql and "ROUND(" in sql


# ── test singolari dal contratto ──────────────────────────────────────────────
def test_le_regole_del_contratto_diventano_query_che_selezionano_le_violazioni():
    t = d.singular_test_sql
    assert t({"kind": "range", "column": "eta", "min": 0, "max": 120}) == 'SELECT * FROM "__t" AS "__t" WHERE "eta" IS NOT NULL AND ("eta" < 0 OR "eta" > 120)'
    assert t({"kind": "row_count", "min": 10}) == 'SELECT COUNT(*) AS n FROM "__t" AS "__t" HAVING COUNT(*) < 10'
    assert "regexp_matches(\"cap\", '^[0-9]{5}$')" in t({"kind": "pattern", "column": "cap", "regex": "^[0-9]{5}$"})
    assert t({"kind": "unique_combination", "columns": ["a", "b"]}) == 'SELECT "a", "b", COUNT(*) AS n FROM "__t" AS "__t" GROUP BY "a", "b" HAVING COUNT(*) > 1'
    assert t({"kind": "expression", "sql": "importo >= 0"}) == 'SELECT * FROM "__t" AS "__t" WHERE NOT COALESCE((importo >= 0), FALSE)'
    with pytest.raises(d.DbtExportError):
        t({"kind": "freshness", "max_age_hours": 24})


def test_un_test_singolare_si_traduce_in_postgres_col_ref_al_modello():
    sql = d.singular_test_sql({"kind": "pattern", "column": "cap", "regex": "^[0-9]{5}$"})
    tradotto = d.transpile_model(sql, "postgres", {"__t": ["id", "cap"]})
    tradotto = d.substitute_source_idents(tradotto, {"__t": "{{ ref('clienti') }}"})
    assert tradotto.startswith('SELECT "__t"."id" AS "id", "__t"."cap" AS "cap" FROM {{ ref(\'clienti\') }} AS "__t"')
    assert "~" in tradotto or "REGEXP" in tradotto.upper()        # il regexp di Postgres


# ── progetto ──────────────────────────────────────────────────────────────────
def _progetto(**kw):
    modelli = [
        {"name": "int_pulito", "sql": "select 1", "materialized": "ephemeral", "description": "Shared steps", "columns": []},
        {"name": "margine", "sql": "select 2", "materialized": "table", "description": "Output «margine»: the margin. Contract: what we promise",
         "columns": [{"name": "categoria", "description": "The product's category", "tests": [{"kind": "not_null", "severity": "error"}, {"kind": "accepted_values", "severity": "warn", "values": ["a", "b: c"]}]},
                     {"name": "margine", "description": "", "tests": [{"kind": "unique", "severity": "error"}]}]},
        {"name": "margini", "sql": "select 3", "materialized": "incremental", "description": "Table report.margini (append)", "columns": [], "schema": "report", "alias": "margini"},
    ]
    sorgenti = [{"name": "db_1_vendite", "database": "db_1", "schema": "vendite",
                 "tables": [{"name": "ordini", "description": "Datasource «orders»", "columns": [{"name": "id", "description": "The order id"}]}]}]
    base = dict(flow_name="Margin by Category", models=modelli, attachments=[{"alias": "db_1", "db_type": "postgresql", "pw_env": "TABULARIA_DB_1_PASSWORD",
                "conn": {"host": "db", "port": 5432, "database": "shop", "username": "shop"}}], sources=sorgenti)
    base.update(kw)
    return d.build_dbt_project(**base)


def test_il_progetto_ha_schema_yml_con_descrizioni_e_test_dal_contratto():
    files = _progetto()
    schema = files["models/schema.yml"]
    assert "- name: margine\n    description: \"Output «margine»: the margin. Contract: what we promise\"" in schema
    assert "- name: \"categoria\"\n        description: \"The product's category\"\n        data_tests:\n          - not_null:\n              config: {severity: error}\n          - accepted_values:\n              values: [\"a\", \"b: c\"]\n              config: {severity: warn}" in schema
    assert "          - unique:\n              config: {severity: error}" in schema
    assert "ephemeral" in files["models/int_pulito.sql"]
    assert files["models/margini.sql"].startswith("{{ config(materialized='incremental', incremental_strategy='append', schema='report', alias='margini') }}")
    assert "generate_schema_name" in files["macros/generate_schema_name.sql"]
    assert "description: \"Datasource «orders»\"" in files["models/sources.yml"] and "description: \"The order id\"" in files["models/sources.yml"]
    assert "seed-paths" in files["dbt_project.yml"] and "test-paths" in files["dbt_project.yml"]
    assert "dbt test" in files["README.md"] and "`int_pulito` (ephemeral)" in files["README.md"]


def test_seed_test_singolari_e_note_finiscono_nel_progetto():
    files = _progetto(seeds=[{"name": "seed_listino", "csv": "id,prezzo\n1,2.5\n", "description": "A snapshot", "columns": [{"name": "id", "description": ""}]}],
                      tests=[{"name": "margine__range_r1", "sql": "select * from {{ ref('margine') }} where x < 0", "severity": "warn", "description": "Contract rule range"}],
                      notes=["The seed is a copy."])
    assert files["seeds/seed_listino.csv"] == "id,prezzo\n1,2.5\n"
    assert "- name: seed_listino\n    description: \"A snapshot\"" in files["seeds/schema.yml"]
    assert files["tests/margine__range_r1.sql"] == "{{ config(severity='warn') }}\n-- Contract rule range\nselect * from {{ ref('margine') }} where x < 0\n"
    assert "dbt seed" in files["README.md"] and "- The seed is a copy." in files["README.md"]


def test_il_profilo_nativo_non_ha_attach_e_quello_federato_si():
    nativo = _progetto(attachments=None, native={"db_type": "postgresql", "pw_env": "P", "target_schema": "dbt_tabularia",
                                                 "conn": {"host": "db", "port": 5432, "database": "shop", "username": "shop"}})
    assert "type: postgres" in nativo["profiles.yml"] and "attach" not in nativo["profiles.yml"]
    federato = _progetto()
    assert "type: duckdb" in federato["profiles.yml"] and "alias: db_1" in federato["profiles.yml"] and "env_var('TABULARIA_DB_1_PASSWORD')" in federato["profiles.yml"]
