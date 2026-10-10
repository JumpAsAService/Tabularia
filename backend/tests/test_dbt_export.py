"""L'exporter dbt del motore: la compilazione dell'IR in modelli (una CTE per
operazione, lo schema tracciato), la traduzione nel dialetto nativo con i suoi
buchi noti, i test singolari dai contratti e il progetto assemblato (schema.yml,
sorgenti con descrizioni, seed, macro dello schema, README)."""
import re

import pytest

from app.engine import dbt_export as d


def _resolve(src: dict):
    if "model" in src:
        return "{{ ref('%s') }}" % src["model"], ["id", "x"], {}
    if "seed" in src:
        return "{{ ref('%s') }}" % src["seed"], ["id", "y"], {}
    return "{{ source('db', '%s') }}" % src["key"].split("/")[-1], ["id", "canale", "importo"], {}


# ── compilazione ──────────────────────────────────────────────────────────────
def test_una_cte_per_operazione_e_lo_schema_segue_le_rinomine():
    sql, cols, _ = d.compile_model_sql(
        {"key": "datasets/ordini"},
        [{"type": "rename", "params": {"mapping": {"importo": "ricavo"}}},
         {"type": "filter", "params": {"column": "canale", "operator": "eq", "value": "web"}},
         {"type": "group_by", "params": {"by": ["canale"], "aggregations": [{"column": "ricavo", "func": "sum", "alias": "tot"}]}}],
        _resolve,
    )
    assert cols == ["canale", "tot"]
    # ogni passo ha un nome che dice cosa fa, una nota in parole, una clausola per riga
    assert sql.startswith("WITH\nsource_ordini AS (\n    -- source: ordini\n    SELECT *\n    FROM {{ source('db', 'ordini') }}")
    assert "rename_columns AS (\n    -- rename importo to ricavo\n    SELECT * RENAME (\"importo\" AS \"ricavo\")\n    FROM source_ordini" in sql
    assert "filter_canale AS (\n    -- keep rows where canale = 'web'" in sql and "WHERE \"canale\" = 'web'" in sql
    assert "aggregate_by_canale AS (\n    -- aggregate by canale: sum(ricavo) as tot" in sql and "GROUP BY \"canale\"" in sql
    assert sql.endswith("SELECT *\nFROM aggregate_by_canale")


def test_un_modello_puo_partire_da_un_altro_modello_o_da_un_seed():
    sql, cols, _ = d.compile_model_sql({"model": "int_pulito"}, [{"type": "limit", "params": {"n": 5}}], _resolve)
    assert "FROM {{ ref('int_pulito') }}" in sql and cols == ["id", "x"]
    sql, cols, _ = d.compile_model_sql({"seed": "seed_listino"}, [], _resolve)
    assert "FROM {{ ref('seed_listino') }}" in sql and cols == ["id", "y"]


def test_il_sort_del_publish_ignora_le_colonne_che_non_ci_sono():
    sql, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "sort", "params": {"by": ["sparita", "id"], "ignore_missing": True}}], _resolve)
    assert 'ORDER BY "id" ASC NULLS LAST' in sql and "sparita" not in sql
    sql, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "sort", "params": {"by": ["sparita"], "ignore_missing": True}}], _resolve)
    assert "ORDER BY" not in sql


def test_join_con_ramo_destro_compilato_come_cte_e_colonne_right():
    sql, cols, _ = d.compile_model_sql(
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
    sql, cols, _ = d.compile_model_sql({"key": "k"}, piv, _resolve)            # in DuckDB si può
    assert "PIVOT" in sql and cols == []
    with pytest.raises(d.DbtExportError, match="federato"):
        d.compile_model_sql({"key": "k"}, piv, _resolve, native=True)


# ── dialetto nativo ───────────────────────────────────────────────────────────
def test_la_traduzione_postgres_espande_gli_star_e_mette_il_cast_a_round():
    raw, _, _ = d.compile_model_sql(
        {"key": "k"},
        [{"type": "compute", "params": {"columns": [{"name": "pct", "expr": 'ROUND("importo" / 3, 2)'}]}}],
        lambda src: ("__s_0", ["id", "canale", "importo"], {}), native=True,
    )
    sql = _codice(d.transpile_model(raw, "postgres", {"__s_0": ["id", "canale", "importo"]}))
    assert "*" not in sql                                        # star espansi: colonne esplicite
    assert re.search(r'ROUND\(CAST\(.*AS DECIMAL\), 2\)', " ".join(sql.split()).replace("( ", "(").replace(" )", ")"))   # Postgres non ha round(double, int)
    sql = d.substitute_source_idents(sql, {"__s_0": "{{ source('src', 'ordini') }}"})
    assert "{{ source('src', 'ordini') }} AS \"__s_0\"" in sql


def test_la_traduzione_clickhouse_non_tocca_round():
    raw, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "compute", "params": {"columns": [{"name": "pct", "expr": 'ROUND("importo", 2)'}]}}],
                                    lambda src: ("__s_0", ["id", "importo"], {}))
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
    # un test dbt seleziona le righe che violano: uno `*` va bene (lo si lascia al warehouse)
    assert " ".join(tradotto.split()).startswith("SELECT * FROM {{ ref('clienti') }} AS \"__t\" WHERE")
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
    assert "- name: \"categoria\"\n        description: \"The product's category\"\n        data_tests:\n          - not_null:\n              config: {severity: error}\n          - accepted_values:\n              arguments:\n                values: [\"a\", \"b: c\"]\n              config: {severity: warn}" in schema
    assert "          - unique:\n              config: {severity: error}" in schema
    assert "ephemeral" in files["models/int_pulito.sql"]
    assert files["models/margini.sql"].startswith("{{ config(materialized='incremental', incremental_strategy='append', schema='report', alias='margini') }}")
    assert "generate_schema_name" in files["macros/generate_schema_name.sql"]
    assert "description: \"Datasource «orders»\"" in files["models/sources.yml"] and "description: \"The order id\"" in files["models/sources.yml"]
    assert "seed-paths" in files["dbt_project.yml"] and "test-paths" in files["dbt_project.yml"]
    assert "dbt test" in files["README.md"] and "`int_pulito` (ephemeral)" in files["README.md"]


def test_ogni_modello_dice_da_dove_viene():
    """Tracciabilità: il team data, davanti a un modello, risale al flusso
    nell'app — in testa al file, in schema.yml (config.meta + tag) e nel README."""
    meta = {"flow": "Margin by Category", "flow_id": 7, "flow_version": 3, "version_saved_at": "2026-10-09 18:00 UTC", "folder": "Sales",
            "owner": "anna@example.com", "output": "output «margine» (datasource)", "node": "out-1"}
    modelli = [
        {"name": "src_q", "sql": "select 0", "materialized": "ephemeral", "description": "", "columns": [],
         "meta": {"datasource": "Orders {{ x }}", "datasource_id": 4}, "tags": ["tabularia"]},
        {"name": "margine", "sql": "select 2", "materialized": "table", "description": "Out", "columns": [],
         "meta": meta, "tags": ["tabularia", "margin_by_category"]},
    ]
    files = _progetto(models=modelli, esportato_da="admin@example.com")
    sql = files["models/margine.sql"]
    # solo fatti del flusso: riesportare la stessa versione dà lo stesso file
    assert sql.startswith("{{ config(materialized='table') }}\n\n-- Exported from Tabularia: flow «Margin by Category» (id 7, version 3 saved 2026-10-09 18:00 UTC), "
                          "output «margine» (datasource).\n-- Built by anna@example.com. Re-exporting the same version gives this same file.\n\nselect 2")
    assert "admin@example.com" not in sql
    # un nome con Jinja non apre un blocco nel commento
    assert "«Orders { { x } }» (id 4)" in files["models/src_q.sql"]
    schema = files["models/schema.yml"]
    assert ("  - name: margine\n    description: \"Out\"\n    config:\n      tags: [\"tabularia\", \"margin_by_category\"]\n      meta:\n        tabularia:\n"
            "          flow: \"Margin by Category\"\n          flow_id: 7\n          flow_version: 3\n"
            "          version_saved_at: \"2026-10-09 18:00 UTC\"\n          folder: \"Sales\"\n") in schema
    assert "exported" not in schema
    readme = files["README.md"]
    assert "## Where it comes from" in readme and "- Tabularia flow «Margin by Category» (id 7, version 3, saved 2026-10-09 18:00 UTC), built by anna@example.com" in readme
    assert "Exported by admin@example.com." in readme and "tag:tabularia" in readme


def test_senza_tracciabilita_il_file_resta_com_era():
    files = _progetto()
    assert files["models/margine.sql"] == "{{ config(materialized='table') }}\n\nselect 2\n"
    assert "config:" not in files["models/schema.yml"].split("- name: margine")[1].split("columns:")[0]
    assert "Where it comes from" not in files["README.md"]


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



# ── parità con il motore (2026-10-10, dall'oracolo dbt_matrice) ───────────────
TIPI = {"id": "INTEGER", "testo": "VARCHAR", "numero": "DOUBLE", "categoria": "VARCHAR", "Nome Strano": "VARCHAR"}


def _codice(sql: str) -> str:
    """Il SQL senza commenti (le note dei passi possono contenere '*' o 'contains')."""
    import re
    return re.sub(r"/\*.*?\*/|--[^\n]*", "", sql, flags=re.S)


def _tipato(src):
    return "__s_0", list(TIPI), dict(TIPI)


def test_la_sorgente_prende_i_tipi_di_tabularia():
    sql, cols, tipi = d.compile_model_sql({"key": "k"}, [], _tipato)
    assert 'CAST("testo" AS VARCHAR) AS "testo"' in sql and 'CAST("Nome Strano" AS VARCHAR) AS "Nome Strano"' in sql
    assert cols == list(TIPI) and tipi == TIPI


def test_il_cast_e_quello_del_motore_nel_federato_e_protetto_nel_nativo():
    from app.engine.duckdb_ops import _cast_expr
    op = [{"type": "cast", "params": {"columns": {"testo": "int", "numero": "int"}}}]
    fed, _, tipi = d.compile_model_sql({"key": "k"}, op, _tipato)
    assert _cast_expr('"testo"', "VARCHAR", "BIGINT") in fed and _cast_expr('"numero"', "DOUBLE", "BIGINT") in fed
    assert tipi["testo"] == "BIGINT"
    nat, _, _ = d.compile_model_sql({"key": "k"}, op, _tipato, native=True)
    assert "TRY_CAST" not in nat and "regexp_matches(trim(\"testo\")" in nat and 'CAST(trunc("numero") AS BIGINT)' in nat
    pg = d.transpile_model(nat, "postgres", {"__s_0": list(TIPI)})
    assert "~ '^[+-]?[0-9]+$'" in pg and "TRY" not in pg


def test_il_compute_sostituisce_la_colonna_al_suo_posto():
    sql, cols, _ = d.compile_model_sql({"key": "k"}, [{"type": "compute", "params": {"columns": [{"name": "numero", "expr": "numero * 10"}]}}], _tipato)
    assert 'REPLACE ((numero * 10) AS "numero")' in sql and cols == list(TIPI)
    # EXTRACT(… FROM …) è un'espressione scalare: l'export la porta così com'è
    sql, cols, _ = d.compile_model_sql({"key": "k"}, [{"type": "compute", "params": {"columns": [{"name": "anno", "expr": "EXTRACT(YEAR FROM data)"}]}}], _tipato)
    assert "(EXTRACT(YEAR FROM data)) AS \"anno\"" in sql and cols[-1] == "anno"


def test_join_full_con_using_valorizza_la_chiave_anche_a_destra():
    sql, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "join", "params": {"how": "full", "on": "categoria", "right": {"source": {"key": "r"}, "operations": []}}}], _tipato)
    assert 'COALESCE(s1."categoria", r1."categoria") AS "categoria"' in sql.replace("\n", " ").replace("  ", " ") or "COALESCE(" in sql


def test_i_filtri_testuali_nel_nativo_usano_forme_portabili():
    for op, atteso in (("contains", "strpos(CAST(\"categoria\" AS VARCHAR), 'il') > 0"), ("starts_with", "left(CAST(\"categoria\" AS VARCHAR), 2) = 'il'"),
                       ("ends_with", "right(CAST(\"categoria\" AS VARCHAR), 2) = 'il'")):
        nat, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "filter", "params": {"column": "categoria", "operator": op, "value": "il"}}], _tipato, native=True)
        assert atteso in nat, op
        pg = _codice(d.transpile_model(nat, "postgres", {"__s_0": list(TIPI)}))
        assert "CONTAINS" not in pg.upper().replace("CONTAINS_", "") or op != "contains"
        assert "ENDS_WITH" not in pg.upper()


def test_union_per_nome_nel_nativo_elenca_le_colonne():
    nat, cols, _ = d.compile_model_sql({"key": "k"}, [{"type": "select", "params": {"columns": ["id", "numero"]}},
                                                      {"type": "union", "params": {"right": {"source": {"key": "r"}, "operations": [{"type": "select", "params": {"columns": ["id", "categoria"]}}]}}}],
                                       _tipato, native=True)
    assert "BY NAME" not in nat and 'NULL AS "categoria"' in nat and 'NULL AS "numero"' in nat and cols == ["id", "numero", "categoria"]


def test_unpivot_tiene_i_null_e_usa_il_supertipo():
    sql, cols, tipi = d.compile_model_sql({"key": "k"}, [{"type": "unpivot", "params": {"index": ["id"], "on": ["numero", "id"], "variable_name": "v", "value_name": "x"}}], _tipato)
    assert "UNPIVOT" not in sql and sql.count("UNION ALL") == 1 and "CAST(\"numero\" AS DOUBLE)" in sql
    assert cols == ["id", "v", "x"] and tipi["x"] == "DOUBLE"


def test_il_nodo_sql_punta_self_e_input_al_passo_prima_anche_con_un_with_proprio():
    q = "WITH x AS (SELECT * FROM input WHERE numero > 0) SELECT id FROM x JOIN self AS s USING (id)"
    sql, cols, _ = d.compile_model_sql({"key": "k"}, [{"type": "sql", "params": {"query": q}}], _tipato)
    assert "FROM source_source AS input" in sql and "JOIN source_source AS s" in sql and "WITH x AS" in sql and cols == ["id"]
    import duckdb  # il modello è SQL valido: lo si esegue su una tabella vera
    con = duckdb.connect()
    con.execute('CREATE TABLE "__s_0" (id INTEGER, testo VARCHAR, numero DOUBLE, categoria VARCHAR, "Nome Strano" VARCHAR)')
    con.execute("INSERT INTO \"__s_0\" VALUES (1, 'a', 2.0, 'A', 'x'), (2, 'b', -1.0, 'B', 'y')")
    assert con.execute(sql).fetchall() == [(1,)]


def test_somma_di_interi_torna_bigint_come_nel_motore():
    sql, _, tipi = d.compile_model_sql({"key": "k"}, [{"type": "group_by", "params": {"by": ["categoria"], "aggregations": [{"column": "id", "func": "sum", "alias": "s"}, {"column": "numero", "func": "mean", "alias": "m"}]}}], _tipato)
    assert 'CAST(sum("id") AS BIGINT) AS "s"' in sql and tipi["s"] == "BIGINT" and tipi["m"] == "DOUBLE"


def test_la_traduzione_nativa_conserva_le_maiuscole_dei_nomi():
    nat, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "select", "params": {"columns": ["Nome Strano", "id"]}}], _tipato, native=True)
    pg = d.transpile_model(nat, "postgres", {"__s_0": list(TIPI)})
    assert '"Nome Strano"' in pg and '"nome strano"' not in pg


def test_tipi_di_polars():
    assert d.duck_type("Int32") == "INTEGER" and d.duck_type("String") == "VARCHAR" and d.duck_type("Float64") == "DOUBLE"
    assert d.duck_type("Datetime(time_unit='us', time_zone=None)") == "TIMESTAMP" and d.duck_type("Decimal(precision=10, scale=2)") == "DECIMAL(10,2)"
    assert d.duck_type("List(Int64)") is None and d.duck_type(None) is None



def test_dopo_un_nodo_sql_le_colonne_si_ricavano_e_il_nativo_puo_continuare():
    ops = [{"type": "sql", "params": {"query": 'SELECT *, numero * 2 AS "Doppio" FROM self WHERE numero > 0'}},
           {"type": "cast", "params": {"columns": {"Doppio": "int"}}}, {"type": "drop", "params": {"columns": ["testo"]}}]
    nat, cols, _ = d.compile_model_sql({"key": "k"}, ops, _tipato, native=True)
    assert cols == ["id", "numero", "categoria", "Nome Strano", "Doppio"]
    pg = d.transpile_model(nat, "postgres", {"__s_0": list(TIPI)})
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
def test_il_sql_federato_di_ogni_passo_gira_davvero_in_duckdb(ops):
    """Il SQL di un modello GIRA, non solo si compila: i nomi dei passi sono
    identificatori senza virgolette, e una CTE chiamata `pivot` o `unpivot`
    (parole riservate di DuckDB) faceva fallire il modello in dbt."""
    import duckdb

    con = duckdb.connect()
    con.execute("CREATE TABLE casi AS SELECT * FROM (VALUES (1, 'a', 1.5, 10), (2, 'b', NULL, 20), (3, 'a', 3.0, 30)) t(id, categoria, valore, altro)")

    def resolve(_):
        return "casi", ["id", "categoria", "valore", "altro"], {"id": "INTEGER", "categoria": "VARCHAR", "valore": "DOUBLE", "altro": "INTEGER"}, "casi"

    sql, cols, _ = d.compile_model_sql({"key": "x"}, ops, resolve)
    righe = con.execute(sql).fetchall()
    assert righe is not None
    if cols:
        assert [c[0] for c in con.description] == cols


def test_un_passo_non_si_chiama_mai_come_una_parola_riservata():
    assert d._slug_passo("pivot") == "pivot_step" and d._slug_passo("Select") == "select_step"
    assert d._slug_passo("filter_stato") == "filter_stato" and d._slug_passo("2024") == "s_2024"


# ── target ClickHouse: il ClickHouse del cliente legge Postgres/MySQL dal vivo ──────
_PG = {"id": 1, "name": "gestionale", "db_type": "postgresql", "host": "db.x.it", "port": 5432, "database": "erp", "username": "shop", "pw_env": "TABULARIA_DB_1_PASSWORD"}
_MY = {"id": 2, "name": "negozio", "db_type": "mysql", "host": "my.x.it", "port": 3306, "database": "shop", "username": "web", "pw_env": "TABULARIA_DB_2_PASSWORD"}


def test_ogni_schema_di_postgres_o_mysql_e_un_database_federato():
    ch = d.TargetClickHouse([_PG, _MY, {"id": 3, "name": "crm", "db_type": "clickhouse", "host": "CH.local", "database": "analytics"}], "ch.local")
    assert ch.database_per(1, "vendite") == "tab_src_1_vendite" and ch.database_per(1, "Vendite Più") == "tab_src_1_vendite_pi"
    assert ch.database_per(2, "shop") == "tab_src_2_shop"
    assert ch.database_per(3, "analytics") == "analytics"          # lo stesso server: le sue tabelle, così come sono
    assert {f["name"]: f["engine"] for f in ch.federati.values()} == {"tab_src_1_vendite": "PostgreSQL", "tab_src_1_vendite_pi": "PostgreSQL", "tab_src_2_shop": "MySQL"}
    sorgenti = ch.sources([{"name": "db_1_vendite", "database": None, "schema": "vendite", "connection_id": 1, "tables": []}])
    assert sorgenti[0]["schema"] == "tab_src_1_vendite" and sorgenti[0]["database"] is None


def test_un_altro_clickhouse_o_un_tipo_che_clickhouse_non_legge_sono_rifiutati():
    ch = d.TargetClickHouse([{"id": 3, "name": "crm", "db_type": "clickhouse", "host": "altro.local", "database": "a"},
                             {"id": 4, "name": "lago", "db_type": "trino", "host": "t.local"}], "ch.local")
    with pytest.raises(d.DbtExportError, match="ALTRO ClickHouse"):
        ch.database_per(3, "a")
    with pytest.raises(d.DbtExportError, match="trino"):
        ch.database_per(4, "x")


def test_la_query_di_una_datasource_si_traduce_sulle_tabelle_federate():
    ch = d.TargetClickHouse([_PG], "ch.local")
    sql = ch.traduci_query("WITH a AS (SELECT * FROM crm.clienti WHERE attivo) SELECT a.id, o.totale::numeric FROM a JOIN ordini o ON o.cliente = a.id", 1, "clienti")
    assert "tab_src_1_crm.clienti" in sql and "tab_src_1_public.ordini" in sql      # senza schema: public, come in Postgres
    assert "FROM a" in sql and "tab_src_1_public.a" not in sql                       # la CTE resta una CTE
    assert set(ch.federati) == {"tab_src_1_crm", "tab_src_1_public"}
    with pytest.raises(d.DbtExportError, match="non si traduce"):
        ch.traduci_query("SELECT FROM WHERE (", 1, "rotta")


def test_il_progetto_clickhouse_ha_profilo_macro_e_readme():
    ch = d.TargetClickHouse([_PG], "ch.local")
    sorgenti = ch.sources([{"name": "db_1_vendite", "database": None, "schema": "vendite", "connection_id": 1,
                            "tables": [{"name": "ordini", "description": "", "columns": []}]}])
    files = _progetto(sources=sorgenti, attachments=None, clickhouse={"host": "ch.local", "port": 8443, "secure": True, "target_schema": "dbt_tabularia",
                                                                      "federati": list(ch.federati.values())})
    prof = files["profiles.yml"]
    assert "type: clickhouse" in prof and "env_var('CLICKHOUSE_HOST', 'ch.local')" in prof and "env_var('CLICKHOUSE_PORT', '8443')" in prof
    assert "env_var('CLICKHOUSE_PASSWORD')" in prof and "join_use_nulls: 1" in prof and "attach" not in prof
    macro = files["macros/create_tabularia_sources.sql"]
    assert "CREATE DATABASE IF NOT EXISTS `tab_src_1_vendite` ENGINE = PostgreSQL('db.x.it:5432', 'erp', 'shop', '{{ pw_tab_src_1_vendite }}', 'vendite')" in macro
    assert "env_var('TABULARIA_DB_1_PASSWORD')" in macro
    assert "database:" not in files["models/sources.yml"] and "schema: tab_src_1_vendite" in files["models/sources.yml"]
    readme = files["README.md"]
    assert "pip install dbt-clickhouse" in readme and "dbt run-operation create_tabularia_sources --log-level-file none" in readme
    assert readme.index("run-operation") < readme.index("dbt run\n") and "`db.x.it:5432`" in readme


def test_un_valore_con_delimitatori_jinja_non_entra_nella_macro():
    assert d._letterale_ch("o'neil\\x") == "'o\\'neil\\\\x'"
    with pytest.raises(d.DbtExportError):
        d._letterale_ch("{{ evil }}")


def test_l_export_clickhouse_traduce_la_query_e_porta_tutto_nei_database_federati():
    import io, zipfile
    from app.api.routes import dbt as rotta

    def corpo(stream):
        import asyncio

        async def leggi():
            return b"".join([c async for c in stream.body_iterator])
        return asyncio.run(leggi())

    req = rotta.ExportRequest(
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
    with zipfile.ZipFile(io.BytesIO(corpo(rotta.export_dbt(req)))) as z:
        files = {n: z.read(n).decode() for n in z.namelist()}
    assert "tab_src_1_crm.clienti" in files["models/src_clienti.sql"] and "Nullable(" in files["models/src_clienti.sql"]
    assert "{{ ref('src_clienti') }}" in files["models/margini.sql"] and "{{ source('db_1_vendite', 'ordini') }}" in files["models/margini.sql"]
    macro = files["macros/create_tabularia_sources.sql"]
    assert "`tab_src_1_vendite`" in macro and "`tab_src_1_crm`" in macro          # anche lo schema che serve solo alla query
    assert "translated from postgresql to ClickHouse" in files["README.md"]


def test_semi_e_anti_join_su_clickhouse_sono_quelli_del_motore():
    """ClickHouse non ha EXISTS correlato: semi/anti diventano LEFT SEMI/ANTI JOIN,
    come nel motore; gli altri warehouse tengono EXISTS."""
    def resolve(src):
        if src.get("key") == "destra":
            return "__s_1", ["k", "v"], {"k": "BIGINT", "v": "VARCHAR"}, "destra"
        return "__s_0", ["k", "x"], {"k": "BIGINT", "x": "VARCHAR"}, "casi"
    for how in ("semi", "anti"):
        ops = [{"type": "join", "params": {"how": how, "on": ["k"], "right": {"source": {"key": "destra"}}}}]
        sql_ch, cols, _ = d.compile_model_sql({"key": "casi"}, ops, resolve, native=True, dialetto="clickhouse")
        assert f"LEFT {how.upper()} JOIN" in sql_ch and "EXISTS" not in sql_ch and cols == ["k", "x"]
        tradotto = d.transpile_model(sql_ch, "clickhouse", {"__s_0": ["k", "x"], "__s_1": ["k", "v"]})
        assert f"LEFT {how.upper()} JOIN" in tradotto
        sql_pg, _, _ = d.compile_model_sql({"key": "casi"}, ops, resolve, native=True, dialetto="postgres")
        assert "EXISTS" in sql_pg


# ── opzioni del download: cartella per un progetto esistente, livelli, sorgenti già dichiarate ──
def _con_tipi(**kw):
    modelli = [
        {"name": "tab_src_q", "kind": "raw", "sql": "select 0", "materialized": "ephemeral", "description": "", "columns": []},
        {"name": "tab_int_pulito", "kind": "intermediate", "sql": "select 1", "materialized": "ephemeral", "description": "", "columns": []},
        {"name": "tab_margine", "kind": "output", "sql": "select 2", "materialized": "table", "description": "", "columns": [],
         "meta": {"flow": "Margin by Category", "flow_id": 7}, "tags": ["tabularia", "margin_by_category"]},
        {"name": "tab_righe", "kind": "output", "sql": "select 3", "materialized": "incremental", "description": "", "columns": [],
         "schema": "report", "alias": "righe"},
    ]
    sorgenti = [{"name": "erp", "schema": "vendite", "database": "db_1", "declared": True, "tables": [{"name": "ordini"}, {"name": "clienti"}]},
                {"name": "db_1_crm", "schema": "crm", "database": "db_1", "tables": [{"name": "contatti", "description": "", "columns": []}]}]
    return _progetto(models=modelli, sources=sorgenti, **kw)


def test_il_progetto_completo_coi_livelli_mette_i_modelli_per_strato():
    files = _con_tipi(layout={"package": "project", "folder": "margini", "layers": True})
    assert {"models/margini/staging/tab_src_q.sql", "models/margini/intermediate/tab_int_pulito.sql",
            "models/margini/marts/tab_margine.sql", "models/margini/schema.yml", "models/margini/sources.yml",
            "profiles.yml", "dbt_project.yml", "README.md", "macros/margini/generate_schema_name.sql"} <= set(files)
    assert "INTEGRATION.md" not in files


def test_la_cartella_per_un_progetto_esistente_ha_solo_quello_da_copiare_e_le_istruzioni():
    files = _con_tipi(layout={"package": "folder", "folder": "margini", "layers": False},
                      seeds=[{"name": "tab_seed_listino", "csv": "id\n1\n", "description": "", "columns": []}],
                      tests=[{"name": "tab_margine__range_r1", "sql": "select 1", "severity": "warn", "description": "x"}])
    assert not {"profiles.yml", "dbt_project.yml", "README.md"} & set(files)
    assert {"models/margini/tab_margine.sql", "seeds/margini/tab_seed_listino.csv", "seeds/margini/schema.yml",
            "tests/margini/tab_margine__range_r1.sql", "INTEGRATION.md"} <= set(files)
    # niente generate_schema_name: rinominerebbe gli schemi di tutto il loro progetto (lo dice INTEGRATION.md)
    assert not any("generate_schema_name" in f for f in files)
    # la sorgente «già dichiarata» non entra nel nostro sources.yml: dbt rifiuterebbe il doppione
    assert "name: erp" not in files["models/margini/sources.yml"] and "name: db_1_crm" in files["models/margini/sources.yml"]
    guida = files["INTEGRATION.md"]
    assert "`erp` is **declared in your project**: it must list the tables `clienti`, `ordini`." in guida
    assert "dbt build -s +tag:margin_by_category" in guida and "alias: db_1" in guida and "attach:" in guida
    assert "`schema='report'`" in guida and "generate_schema_name" in guida
    assert "`models/margini/`" in guida and "`seeds/margini/`" in guida and "`tests/margini/`" in guida


def test_la_cartella_clickhouse_porta_la_macro_e_dice_join_use_nulls():
    ch = d.TargetClickHouse([_PG], "ch.local")
    sorgenti = ch.sources([{"name": "db_1_vendite", "schema": "vendite", "connection_id": 1, "tables": [{"name": "ordini"}]},
                           {"name": "loro", "schema": "crm", "connection_id": 1, "declared": True, "tables": [{"name": "contatti"}]}])
    assert set(ch.federati) == {"tab_src_1_vendite"}                     # la dichiarata non crea un database federato
    files = _progetto(sources=sorgenti, attachments=None, layout={"package": "folder", "folder": "f"},
                      clickhouse={"host": "ch.local", "port": 8443, "secure": True, "federati": list(ch.federati.values())})
    assert "macros/f/create_tabularia_sources.sql" in files and "profiles.yml" not in files
    guida = files["INTEGRATION.md"]
    assert "join_use_nulls: 1" in guida and "dbt run-operation create_tabularia_sources --log-level-file none" in guida
    assert "DBT_PROFILES_DIR" not in guida


def test_lo_schema_scelto_va_nel_profilo_duckdb_e_una_cartella_strana_e_rifiutata():
    assert "      schema: analisi\n" in _progetto(schema="analisi")["profiles.yml"]
    assert "schema:" not in _progetto()["profiles.yml"]
    with pytest.raises(d.DbtExportError):
        _progetto(layout={"package": "folder", "folder": "../fuori"})


# ── determinismo e rifiuti con un codice ───────────────────────────────────────
def _richiesta_export(**kw):
    from app.api.routes import dbt as rotta

    base = dict(flow_name="Margini", mode="duckdb", sources=[], source_map={"k": {"ref": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "x"],
                                                                                  "types": {"id": "Int64", "x": "String"}, "label": "ordini"}},
                models=[{"name": "uno", "source": {"key": "k"}, "operations": [{"type": "filter", "params": {"column": "x", "operator": "eq", "value": "a"}}]},
                        {"name": "due", "source": {"model": "uno"}, "operations": []}])
    base.update(kw)
    return rotta.ExportRequest(**base)


def _zip(req):
    import asyncio

    from app.api.routes import dbt as rotta

    async def leggi(s):
        return b"".join([c async for c in s.body_iterator])
    return asyncio.run(leggi(rotta.export_dbt(req)))


def test_lo_zip_e_lo_stesso_byte_per_byte_a_ogni_export():
    """Riesportare la stessa versione con le stesse opzioni dà lo stesso zip: un diff
    nel repository del team mostra solo quello che è cambiato nel flusso."""
    import io, zipfile

    primo, secondo = _zip(_richiesta_export()), _zip(_richiesta_export())
    assert primo == secondo
    with zipfile.ZipFile(io.BytesIO(primo)) as z:
        voci = z.infolist()
    assert [v.filename for v in voci] == sorted(v.filename for v in voci)
    assert {v.date_time for v in voci} == {(1980, 1, 1, 0, 0, 0)}


def test_un_rifiuto_arriva_al_gateway_con_codice_e_parametri():
    from fastapi import HTTPException

    req = _richiesta_export(models=[{"name": "uno", "source": {"key": "k"}, "operations": [{"type": "pivot", "params": {"index": ["id"], "on": "x", "values": "id"}}]}],
                            mode="native", native={"db_type": "postgresql", "conn": {"host": "h", "port": 5432, "database": "d", "username": "u"}, "pw_env": "P"},
                            source_map={"k": {"ident": "__s_0", "macro": "{{ source('s', 'ordini') }}", "columns": ["id", "x"], "types": {"id": "Int64", "x": "String"}, "label": "ordini"}})
    with pytest.raises(HTTPException) as e:
        _zip(req)
    assert e.value.status_code == 422 and e.value.detail["code"] == "pivot_not_portable"
    assert "pivot" in e.value.detail["message"] and e.value.detail["params"] == {}
    with pytest.raises(d.DbtExportError) as e2:
        d.TargetClickHouse([{"id": 3, "name": "crm", "db_type": "clickhouse", "host": "altro"}], "ch").database_per(3, "a")
    assert e2.value.code == "other_clickhouse" and e2.value.params == {"connection": "crm", "host": "altro"}


def test_std_e_var_su_clickhouse_danno_null_con_un_solo_valore():
    """Come il motore: ClickHouse darebbe NaN; il modello lo protegge con count > 1."""
    def resolve(_):
        return "__s_0", ["k", "v"], {"k": "VARCHAR", "v": "DOUBLE"}, "t"
    ops = [{"type": "group_by", "params": {"by": ["k"], "aggregations": [{"column": "v", "func": "std", "alias": "sd"}]}}]
    sql_ch, _, _ = d.compile_model_sql({"key": "t"}, ops, resolve, native=True, dialetto="clickhouse")
    assert 'CASE WHEN count("v") > 1 THEN stddev("v") END' in sql_ch
    tradotto = d.transpile_model(sql_ch, "clickhouse", {"__s_0": ["k", "v"]})
    assert "CASE WHEN COUNT(" in tradotto and "stddevSamp" in tradotto
    sql_pg, _, _ = d.compile_model_sql({"key": "t"}, ops, resolve, native=True, dialetto="postgres")
    assert "CASE WHEN" not in sql_pg


# ── la traduzione proposta dall'AI: i controlli li fa l'engine ─────────────────
_MACRO = {"__s_0": "{{ source('db_1_v', 'ordini') }}", "__m_0": "{{ ref('clienti') }}"}


def test_una_proposta_buona_si_usa_con_le_macro_al_posto_dei_segnaposto():
    sql = d.usa_proposta("SELECT o.id, c.nome FROM __s_0 o LEFT JOIN __m_0 AS c ON o.cliente = c.id", "clickhouse", _MACRO, ["id", "nome"], "m")
    assert "{{ source('db_1_v', 'ordini') }}" in sql and "{{ ref('clienti') }}" in sql and "__TABULARIA" not in sql
    assert '"o"' in sql or "o." in sql      # l'alias scelto dall'AI resta


@pytest.mark.parametrize("proposta,motivo", [
    ("SELECT o.id FROM __s_0 o", "columns"),
    ("SELECT * FROM __s_0", "star"),
    ("SELECT id, nome FROM altra_tabella", "unknown_table"),
    ("SELECT id, nome FROM s3('http://x/y.csv', 'CSV')", "table_function"),
    ("INSERT INTO t SELECT id, nome FROM __s_0", "not_select"),
    ("SELECT id, nome FROM __s_0; DROP TABLE x", "not_select"),
    ("SELEC id nome FRM", "unparsable"),
])
def test_una_proposta_che_non_passa_i_controlli_e_rifiutata_col_suo_motivo(proposta, motivo):
    with pytest.raises(d.DbtExportError) as e:
        d.usa_proposta(proposta, "clickhouse", _MACRO, ["id", "nome"], "m")
    assert e.value.code == "ai_translation_invalid" and e.value.params["reason"] == motivo and e.value.params["model"] == "m"


def test_la_query_di_una_datasource_tradotta_dall_ai_e_ripuntata_dal_codice():
    ch = d.TargetClickHouse([_PG], "ch.local")
    sql = ch.traduci_query("SELECT impossibile", 1, "clienti", proposta="SELECT id, nome FROM crm.clienti WHERE attivo = 1", colonne=["id", "nome"])
    assert "tab_src_1_crm.clienti" in sql and set(ch.federati) == {"tab_src_1_crm"}
    with pytest.raises(d.DbtExportError) as e:
        ch.traduci_query("x", 1, "clienti", proposta="SELECT id FROM crm.clienti", colonne=["id", "nome"])
    assert e.value.params["reason"] == "columns"


def test_il_modello_tradotto_dall_ai_lo_dice_e_il_readme_pure(monkeypatch):
    from app.api.routes import dbt as rotta

    def rotto(*a, **k):
        raise d.DbtExportError("non va", "translation_failed", dialect="clickhouse", detail="x")

    req = _richiesta_export(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "dbt_tabularia"},
                            source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "x"],
                                              "types": {"id": "Int64", "x": "String"}, "label": "ordini"}},
                            models=[{"name": "uno", "source": {"key": "k"}, "operations": [{"type": "filter", "params": {"column": "x", "operator": "eq", "value": "a"}}]}])
    monkeypatch.setattr(rotta, "transpile_model", rotto)
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


# ── il SQL ClickHouse letto da un parser ClickHouse vero (EXPLAIN AST) ─────────
def test_il_controllo_di_sintassi_vede_quello_che_sqlglot_lascia_passare(monkeypatch):
    pytest.importorskip("chdb")
    c = d.ControlloSintassiClickHouse()
    errore = c.errore("SELECT id FROM tab_src_1_v.ordini WHERE canale SIMILAR TO 'w%'")
    assert errore and errore.startswith("Syntax error") and "SIMILAR" in errore and "Expected one of" not in errore
    assert c.errore('SELECT "__s_0"."id" AS "id" FROM "__s_0" AS "__s_0" WHERE match("__s_0"."x", \'a\')') is None
    assert c.errore("SELECT 1 FROM t SAMPLE SYSTEM 50 PERCENT") is not None
    spento = d.ControlloSintassiClickHouse()
    spento._via = False                       # nessun parser raggiungibile: il controllo si salta
    assert spento.errore("SELECT FROM WHERE") is None


def test_un_nodo_sql_che_clickhouse_non_legge_e_un_non_tradotto_con_quello_che_serve_all_ai():
    pytest.importorskip("chdb")
    from fastapi import HTTPException

    req = _richiesta_export(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "dbt_tabularia"},
                            source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "x"],
                                              "types": {"id": "Int64", "x": "String"}, "label": "ordini"}},
                            models=[{"name": "uno", "source": {"key": "k"}, "operations": [
                                {"type": "sql", "params": {"query": "SELECT id, x FROM self WHERE x SIMILAR TO 'a%'"}}]}])
    with pytest.raises(HTTPException) as e:
        _zip(req)
    det = e.value.detail
    assert det["code"] == "translation_failed" and "SIMILAR" in det["params"]["detail"] and det["params"]["model"] == "uno"
    assert det["params"]["target_dialect"] == "clickhouse" and det["params"]["columns"] == ["id", "x"]


def test_una_query_postgres_che_clickhouse_non_legge_e_una_proposta_che_non_legge():
    pytest.importorskip("chdb")
    from fastapi import HTTPException

    base = dict(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "dbt_tabularia"}, source_map={},
                models=[{"name": "src_q", "kind": "raw", "materialized": "ephemeral", "raw_sql": "SELECT id, canale FROM vendite.ordini WHERE canale SIMILAR TO 'w%'",
                         "columns": [{"name": "id", "dtype": "Int64"}, {"name": "canale", "dtype": "String"}], "translate": {"connection_id": 1, "label": "ordini web"}}])
    with pytest.raises(HTTPException) as e:
        _zip(_richiesta_export(**base))
    det = e.value.detail
    assert det["code"] == "query_not_translatable" and "SIMILAR" in det["params"]["detail"]
    assert det["params"]["sql"].startswith("SELECT id, canale FROM vendite.ordini") and det["params"]["source_dialect"] == "postgres"
    # una proposta dell'AI che ClickHouse non legge è scartata col suo motivo
    with pytest.raises(HTTPException) as e:
        _zip(_richiesta_export(**base, overrides={"src_q": "SELECT id, canale FROM vendite.ordini WHERE canale SIMILAR TO 'w%'"}))
    assert e.value.detail["code"] == "ai_translation_invalid" and e.value.detail["params"]["reason"] == "syntax"
    # una proposta buona passa, con le tabelle ripuntate dal codice
    import io, zipfile
    with zipfile.ZipFile(io.BytesIO(_zip(_richiesta_export(**base, overrides={"src_q": "SELECT id, canale FROM vendite.ordini WHERE match(canale, '^w')"})))) as z:
        sql = z.read("models/src_q.sql").decode()
    assert "tab_src_1_vendite.ordini" in sql and "Translated by AI from postgres" in sql
