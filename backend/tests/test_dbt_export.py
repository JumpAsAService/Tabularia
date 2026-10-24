"""L'exporter dbt del motore: la compilazione dell'IR in modelli (una CTE per
operazione, lo schema tracciato), la traduzione nel dialetto nativo con i suoi
buchi noti, i test singolari dai contratti e il progetto assemblato (schema.yml,
sorgenti con descrizioni, seed, macro dello schema, README).

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


def _richiesto(server: str) -> bool:
    """Whether the run requires this live server (TABULARIA_TEST_REQUIRE): then its tests fail
    without it instead of being skipped, which would let a regression on it pass unnoticed."""
    return server in {s.strip() for s in os.getenv("TABULARIA_TEST_REQUIRE", "").split(",")}


def _manca(server: str, variabile: str):
    if _richiesto(server):
        pytest.fail(f"{variabile} is not set, and this run requires {server} (TABULARIA_TEST_REQUIRE)")
    pytest.skip(f"needs {variabile}")


# the ClickHouse tests that need no helper: skipped without the server, unless the run requires it
_CON_CLICKHOUSE = pytest.mark.skipif(not os.getenv("CLICKHOUSE_TEST_HOST") and not _richiesto("clickhouse"), reason="needs CLICKHOUSE_TEST_HOST")


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


def test_sort_on_several_columns_keeps_a_direction_each():
    ops = [{"type": "sort", "params": {"by": ["sparita", "importo", "id"], "descending": [True, True, False], "ignore_missing": True}}]
    sql, _, _ = d.compile_model_sql({"key": "k"}, ops, _resolve)
    assert 'ORDER BY "importo" DESC NULLS LAST, "id" ASC NULLS LAST' in sql and "sparita" not in sql
    assert "-- sort by importo descending, id ascending (nulls last)" in sql
    sql, _, _ = d.compile_model_sql({"key": "k"}, [{"type": "sort", "params": {"by": ["importo", "id"], "descending": True}}], _resolve)
    assert 'ORDER BY "importo" DESC NULLS LAST, "id" DESC NULLS LAST' in sql


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
    with pytest.raises(d.DbtExportError, match="federated"):
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
    # the whole value, read as text (#31): anchored, and cast when the column's type is not known as text
    assert "regexp_matches(CAST(\"cap\" AS VARCHAR), '^(?:^[0-9]{5}$)$')" in t({"kind": "pattern", "column": "cap", "regex": "^[0-9]{5}$"})
    assert "regexp_matches(\"cap\", '^(?:[0-9]{5})$')" in t({"kind": "pattern", "column": "cap", "regex": "[0-9]{5}"}, {"cap": "VARCHAR"})
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
         # the route writes unique and accepted_values as singular tests (#34, #35): only not_null is generic
         "columns": [{"name": "categoria", "description": "The product's category", "tests": [{"kind": "not_null", "severity": "error"}]},
                     {"name": "margine", "description": "", "tests": [{"kind": "not_null", "severity": "warn"}]}]},
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
    # the guard checks the value too (#17): an integer literal that fits a BIGINT, a number in its range
    assert f"~ '{d._RE_INTERO}'" in pg and "TRY" not in pg and '>= -9223372036854775808 AND "source_source"."numero" < 9223372036854775808' in " ".join(pg.split())


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
    # a schema that is not a plain lowercase name has a fingerprint of its exact name too (#68)
    piu = d.nome_db_federato(1, "Vendite Più")
    assert ch.database_per(1, "vendite") == "tab_src_1_vendite" and ch.database_per(1, "Vendite Più") == piu and piu.startswith("tab_src_1_vendite_pi_")
    assert ch.database_per(2, "shop") == "tab_src_2_shop"
    assert ch.database_per(3, "analytics") == "analytics"          # lo stesso server: le sue tabelle, così come sono
    assert {f["name"]: f["engine"] for f in ch.federati.values()} == {"tab_src_1_vendite": "PostgreSQL", piu: "PostgreSQL", "tab_src_2_shop": "MySQL"}
    sorgenti = ch.sources([{"name": "db_1_vendite", "database": None, "schema": "vendite", "connection_id": 1, "tables": []}])
    assert sorgenti[0]["schema"] == "tab_src_1_vendite" and sorgenti[0]["database"] is None


def test_un_altro_clickhouse_o_un_tipo_che_clickhouse_non_legge_sono_rifiutati():
    ch = d.TargetClickHouse([{"id": 3, "name": "crm", "db_type": "clickhouse", "host": "altro.local", "database": "a"},
                             {"id": 4, "name": "lago", "db_type": "trino", "host": "t.local"}], "ch.local")
    with pytest.raises(d.DbtExportError, match="ANOTHER ClickHouse"):
        ch.database_per(3, "a")
    with pytest.raises(d.DbtExportError, match="trino"):
        ch.database_per(4, "x")


def test_la_query_di_una_datasource_si_traduce_sulle_tabelle_federate():
    ch = d.TargetClickHouse([_PG], "ch.local")
    sql = ch.traduci_query("WITH a AS (SELECT * FROM crm.clienti WHERE attivo) SELECT a.id, o.totale::numeric FROM a JOIN ordini o ON o.cliente = a.id", 1, "clienti")
    assert "tab_src_1_crm.clienti" in sql and "tab_src_1_public.ordini" in sql      # senza schema: public, come in Postgres
    assert "FROM a" in sql and "tab_src_1_public.a" not in sql                       # la CTE resta una CTE
    assert set(ch.federati) == {"tab_src_1_crm", "tab_src_1_public"}
    with pytest.raises(d.DbtExportError, match="cannot be translated"):
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
    # one table per source table, no database engine any more (#25: it typed a date as Date, 1955 → 2134)
    assert "CREATE DATABASE IF NOT EXISTS `tab_src_1_vendite`\n" in macro
    assert "ENGINE = PostgreSQL('db.x.it:5432', 'erp', 'ordini', 'shop', '{{ pw_tab_src_1_vendite }}', 'vendite')" in macro
    assert "env_var('TABULARIA_DB_1_PASSWORD')" in macro
    assert "database:" not in files["models/sources.yml"] and "schema: tab_src_1_vendite" in files["models/sources.yml"]
    readme = files["README.md"]
    assert "pip install dbt-clickhouse" in readme and "dbt run-operation create_tabularia_sources --log-level-file none" in readme
    assert readme.index("run-operation") < readme.index("dbt run\n") and "`db.x.it:5432`" in readme


def test_un_valore_con_delimitatori_jinja_non_entra_nella_macro():
    # decision of 2026-10-16 (#2): a value with `{`, `%` or `#` is neutralised, no longer refused
    assert d._letterale_ch("o'neil\\x") == "'o\\'neil\\\\x'"
    for v in ("{{ evil }}", "sales#2", "a{%b", "100%"):
        assert _render_like_dbt(d._letterale_ch(v)) == "'" + v + "'"


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


def test_il_target_clickhouse_rifiuta_oracle_con_un_motivo():
    # ClickHouse non legge Oracle dal vivo: niente database federato, un rifiuto chiaro
    ch = d.TargetClickHouse([{"id": 9, "name": "contabilita", "db_type": "oracle", "host": "ora.x.it", "database": "ORCLPDB1"}], "ch.local")
    with pytest.raises(d.DbtExportError) as e:
        ch.database_per(9, "FIN")
    assert e.value.code == "clickhouse_cannot_read" and e.value.params["db_type"] == "oracle"


def test_il_target_clickhouse_rifiuta_sql_server_con_un_motivo():
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
    fine = _jinja_data(text[i:])
    return "".join(out) + (fine[:-1] if fine.endswith("\n") else fine)


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
    req = _richiesta_export(models=[{"name": "uno", "source": {"key": "k"}, "operations": _ops_with_jinja()}],
                            source_map={"k": {"ref": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "nota"],
                                              "types": {"id": "Int64", "nota": "String"}, "label": "ordini"}})
    sql = _render_like_dbt(_files(req)["models/uno.sql"], {("source", "db_1_v", "ordini"): "ordini"}, config={})
    con = duckdb.connect()
    con.execute("CREATE TABLE ordini AS SELECT * FROM (VALUES (1, 'a'), (2, NULL), (3, 'price {% 10')) t(id, nota)")
    rows = con.execute(f"SELECT * FROM ({sql}) ORDER BY id").fetchall()
    assert [c[0] for c in con.description] == ["id", "nota", "c{{a}}lc", "extra"]
    calc, extra = j["expr"] + j["comment"], j["braces"] + j["endraw"]
    assert rows == [(1, "a", calc, extra), (2, j["secret"], calc, extra)]


def test_jinja_in_a_value_is_neutralised_on_the_native_and_clickhouse_targets():
    j = _JINJA_TEXT
    native = {"db_type": "postgresql", "conn": {"host": "h", "port": 5432, "database": "d", "username": "u"}, "pw_env": "P"}
    smap = {"k": {"ident": "__s_0", "macro": "{{ source('s', 'ordini') }}", "columns": ["id", "nota"],
                  "types": {"id": "Int64", "nota": "String"}, "label": "ordini"}}
    for mode, extra in (("native", {"native": native}), ("clickhouse", {"clickhouse": {"connections": [_PG], "target_schema": "dbt_tabularia"}})):
        req = _richiesta_export(mode=mode, source_map=smap, **extra,
                                models=[{"name": "uno", "source": {"key": "k"}, "operations": _ops_with_jinja(sql_node=mode == "native")}])
        sql = _render_like_dbt(_files(req)["models/uno.sql"], {("source", "s", "ordini"): "s.ordini"})
        for testo in (j["secret"], j["tag"], j["expr"], j["comment"]):
            assert testo in sql, (mode, testo)
        assert "c{{a}}lc" in sql


def test_jinja_in_a_datasource_query_and_in_an_ai_proposal_is_neutralised():
    raw = [{"name": "src_q", "kind": "raw", "materialized": "ephemeral", "columns": [{"name": "id", "dtype": "Int64"}],
            "raw_sql": "SELECT * FROM postgres_query('db_1', 'SELECT id FROM t WHERE c = ''{%'' OR d = ''{{ 1 }}''')"}]
    sql = _render_like_dbt(_files(_richiesta_export(models=raw, source_map={}))["models/src_q.sql"])
    assert "c = ''{%'' OR d = ''{{ 1 }}''" in sql
    proposta = "SELECT id, x FROM __s_0 WHERE x = '{{ env_var(\"P\") }}' /* {% do run_query('DROP TABLE t') %} */"
    tradotto = d.usa_proposta(proposta, "clickhouse", _MACRO, ["id", "x"], "m")
    reso = _render_like_dbt(tradotto, {("source", "db_1_v", "ordini"): "v.ordini", ("ref", "clienti"): "clienti"})
    assert "'{{ env_var(\"P\") }}'" in reso and "v.ordini" in reso


def test_jinja_in_contract_rules_and_in_the_model_config_is_neutralised():
    req = _richiesta_export(models=[{"name": "uno", "source": {"key": "k"}, "operations": [], "alias": "o'neil {{ x }}", "schema": "s{%b",
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
@pytest.mark.parametrize("nome", ["o'brien", "a\\b", "x'y\"z", "città'è", "'", "\\", "\\\\'", "a }} b {{ c {% d %}",
                                  "t\\x') }}{{ log('zz' ~ 'alias', info=True) }}{{ config(a='", "a\rb", "o'\r\n\\", "\r"])
def test_a_name_in_config_reads_the_same_with_dbt_s_static_parser_and_with_jinja(nome):
    # the config line alone: which names an adapter can write is another check (name_not_quotable)
    riga = d._model_header({"materialized": "table", "alias": nome, "schema": nome}).split(") }}\n")[0] + ") }}"
    config: dict = {}
    _render_like_dbt(riga, config=config)
    assert config["alias"] == nome and config["schema"] == nome, riga                     # what Jinja reads
    for chiave in ("alias", "schema"):
        arg = _config_args(riga[len("{{ config("):-len(") }}")])[chiave]
        assert _static_parser_value(arg) in (None, nome), arg                            # what the static parser reads


def test_an_ordinary_name_in_config_is_written_as_before():
    files = _files(_richiesta_export(models=[{"name": "uno", "source": {"key": "k"}, "operations": [], "alias": "orders", "schema": "report"}]))
    assert files["models/uno.sql"].startswith("{{ config(materialized='table', schema='report', alias='orders') }}\n")


@pytest.mark.parametrize("host", ["ch.local", "o'h", "a\\b", 'q"h', "x'y\"z\\"])
def test_the_clickhouse_profile_gives_back_the_engine_s_host(host):
    """profiles.yml is YAML first (the value in double quotes), then Jinja: the default of
    CLICKHOUSE_HOST must come out of both as the host. A backslash escape is not a YAML one
    (`\\'`: the profile did not load)."""
    import json

    riga = next(r for r in d._profilo_clickhouse("p", {"host": host, "port": 8123}).splitlines() if r.startswith("      host: "))
    valore = json.loads(riga[len("      host: "):])                     # a double-quoted YAML scalar is JSON
    m = re.fullmatch(r"\{\{ env_var\('CLICKHOUSE_HOST', (" + _JSTR + r")\) \}\}", valore)
    assert m and _jinja_value(m.group(1)) == host, riga
    if host == "ch.local":
        assert riga == "      host: \"{{ env_var('CLICKHOUSE_HOST', 'ch.local') }}\""


def test_jinja_in_the_yaml_values_is_neutralised():
    """Descriptions, column names, meta and names: dbt renders them (accepted values: see
    test_jinja_in_contract_rules_and_in_the_model_config_is_neutralised)."""
    import json

    t = "Note {{ env_var('TABULARIA_DB_1_PASSWORD') }} {% if x %} {#"
    modelli = [{"name": "uno", "sql": "select 1", "materialized": "table", "description": t, "meta": {"flow": t, "flow_id": 7},
                "tags": ["tabularia"], "columns": [{"name": "c{{a}}lc", "description": t,
                                                   "tests": [{"kind": "not_null", "severity": "error"}]}]}]
    sorgenti = [{"name": "db_1_v", "database": "db_1", "schema": "v",
                 "tables": [{"name": "ordini", "description": t, "columns": [{"name": "n{%x", "description": t}]}]}]
    files = _progetto(models=modelli, sources=sorgenti,
                      seeds=[{"name": "seed_x", "csv": "a\n1\n", "description": t, "columns": [{"name": "a{#", "description": t}]}])
    for path in ("models/schema.yml", "models/sources.yml", "seeds/schema.yml"):
        valori = [json.loads(v) for v in re.findall(r'"(?:[^"\\]|\\.)*"', files[path])]
        resi = [_render_like_dbt(v) for v in valori]
        assert t in resi, path
    assert "c{{a}}lc" in set(_render_like_dbt(json.loads(v)) for v in re.findall(r'"(?:[^"\\]|\\.)*"', files["models/schema.yml"]))


def test_jinja_in_a_step_comment_never_opens_a_block():
    for testo in ("{{{ 7*7 }}}", "{%%}", "{#{#", "a{{%b"):
        assert not _OPENER.search(d._commento(testo)), testo


# ── the federated source password stays out of the ATTACH path (#32) ──────────
def test_the_federated_profile_attaches_through_a_secret_not_a_dsn_with_the_password():
    """DuckDB prints the ATTACH path on every failed connection (wrong password, source
    down) and dbt writes it to the console and logs/dbt.log; an unquoted libpq/MySQL DSN
    also breaks on a space or a backslash. The password goes into a DuckDB secret
    instead: the path is empty and the error says `at ""` (proved with real dbt)."""
    my = {"alias": "db_2", "db_type": "mysql", "pw_env": "TABULARIA_DB_2_PASSWORD",
          "conn": {"host": "my.x", "port": 3306, "database": "shop", "username": "web"}}
    files = _progetto(attachments=[{"alias": "db_1", "db_type": "postgresql", "pw_env": "TABULARIA_DB_1_PASSWORD",
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
    guida = _progetto(attachments=[my], layout={"package": "folder", "folder": "f"})["INTEGRATION.md"]
    assert "secrets:" in guida and "secret: tabularia_db_2" in guida and "password=" not in guida


def test_connection_values_in_the_profile_are_quoted_and_neutralised():
    files = _progetto(attachments=[{"alias": "db_1", "db_type": "postgresql", "pw_env": "TABULARIA_DB_1_PASSWORD",
                                    "conn": {"host": "h#1", "port": 5432, "database": "d: x", "username": "{{ env_var('X') }}"}}])
    prof = files["profiles.yml"]
    assert "host: \"h#1\"" in prof and "database: \"d: x\"" in prof
    assert "user: \"{{ '{{' }} env_var('X') }}\"" in prof


# ── the AI proposal reads only the tables its model reads (#53) ────────────────
@pytest.mark.parametrize("proposta,motivo", [
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
def test_an_ai_proposal_cannot_read_outside_its_model(proposta, motivo):
    with pytest.raises(d.DbtExportError) as e:
        d.usa_proposta(proposta, "clickhouse", _MACRO, ["id", "nome"], "m")
    assert e.value.code == "ai_translation_invalid" and e.value.params["reason"] == motivo, e.value.params


@pytest.mark.parametrize("proposta,motivo", [
    ("WITH pg_authid AS (SELECT 1 AS x) SELECT rolname AS id, rolpassword AS nome FROM pg_catalog.pg_authid", "unknown_table"),
    ("SELECT current_setting('server_version') AS id, nome FROM __s_0", "function_not_allowed"),
    ("SELECT pg_read_file('/etc/passwd') AS id, nome FROM __s_0", "function_not_allowed"),
    ("SELECT id, nome FROM __s_0 WHERE id IN (SELECT x FROM dblink('c', 'q') AS t(x int))", "table_function"),
    ("SELECT dblink_exec('c', 'DROP TABLE t') AS id, nome FROM __s_0", "function_not_allowed"),
    ("SELECT nextval('s') AS id, nome FROM __s_0", "function_not_allowed"),
    ("SELECT set_config('a', 'b', false) AS id, nome FROM __s_0", "function_not_allowed"),
])
def test_an_ai_proposal_for_postgres_cannot_read_outside_its_model(proposta, motivo):
    with pytest.raises(d.DbtExportError) as e:
        d.usa_proposta(proposta, "postgres", _MACRO, ["id", "nome"], "m")
    assert e.value.params["reason"] == motivo, e.value.params


def test_a_good_proposal_with_its_own_ctes_still_passes():
    sql = d.usa_proposta("WITH a AS (SELECT id, nome FROM __s_0), b AS (SELECT id, nome FROM a) SELECT id, nome FROM b", "clickhouse",
                         _MACRO, ["id", "nome"], "m")
    assert "{{ source('db_1_v', 'ordini') }}" in sql


def test_an_ai_proposal_for_a_datasource_query_stays_on_its_connection():
    ch = d.TargetClickHouse([_PG], "ch.local")
    # a table behind an inner CTE of the same name is still a table of the connection: re-pointed
    sql = ch.traduci_query("x", 1, "clienti", colonne=["id", "nome"],
                           proposta="SELECT u.id AS id, u.nome AS nome FROM (WITH users AS (SELECT 1 AS a) SELECT a FROM users) AS q JOIN users AS u ON 1 = 1")
    assert "tab_src_1_public.users" in sql.replace("`", "").replace('"', "")
    for proposta, motivo in (("SELECT id, nome FROM crm.clienti WHERE id IN system.one", "unknown_table"),
                             ("SELECT dictGet('d', 'v', id) AS id, nome FROM crm.clienti", "function_not_allowed")):
        with pytest.raises(d.DbtExportError) as e:
            ch.traduci_query("x", 1, "clienti", proposta=proposta, colonne=["id", "nome"])
        assert e.value.params["reason"] == motivo


def test_on_native_a_proposal_reads_only_the_sources_of_its_own_model():
    from fastapi import HTTPException

    smap = {"a": {"ident": "__s_0", "macro": "{{ source('s', 'ordini') }}", "columns": ["id", "x"], "types": {"id": "Int64", "x": "String"}, "label": "ordini"},
            "b": {"ident": "__s_1", "macro": "{{ source('s', 'clienti') }}", "columns": ["id", "x"], "types": {"id": "Int64", "x": "String"}, "label": "clienti"}}
    req = _richiesta_export(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "dbt_tabularia"}, source_map=smap,
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
    modelli = [{"name": "yes", "sql": "select 1", "materialized": "table", "description": "", "columns": []},
               {"name": "margine", "sql": "select 2", "materialized": "table", "description": "", "columns": []}]
    sorgenti = [{"name": "on", "database": "null", "schema": "no", "tables": [{"name": "yes", "description": "", "columns": []}, "true"]},
                {"name": "db_1_vendite", "database": "db_1", "schema": "vendite", "tables": [{"name": "a: b#c", "description": "", "columns": []}]}]
    files = _progetto(flow_name="On", models=modelli, sources=sorgenti, schema="off",
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
    for prof in (_progetto(flow_name="On", attachments=None, native={"db_type": "postgresql", "pw_env": "P", "target_schema": "yes",
                                                                    "conn": {"host": "db", "port": 5432, "database": "shop", "username": "shop"}})["profiles.yml"],
                 _progetto(flow_name="On", attachments=None, clickhouse={"host": "ch", "port": 8123, "secure": False, "target_schema": "yes"})["profiles.yml"]):
        assert prof.startswith('"on":\n') and '      schema: "yes"\n' in prof


@pytest.mark.parametrize("flusso,progetto", [("DBT", "tabularia_dbt"), ("dbt duckdb", "tabularia_dbt_duckdb"),
                                             ("dbt-postgres", "tabularia_dbt_postgres"), ("Margin by Category", "margin_by_category")])
def test_a_flow_named_like_a_dbt_package_gets_a_project_name_of_its_own(flusso, progetto):
    """dbt installs its own packages `dbt` and `dbt_<adapter>`: a project with the same
    name fails with «dbt found more than one package with the name …»."""
    files = _progetto(flow_name=flusso)
    assert f"name: '{progetto}'\n" in files["dbt_project.yml"] and f"profile: '{progetto}'\n" in files["dbt_project.yml"]
    assert files["profiles.yml"].startswith(f"{progetto}:\n") and f"path: {progetto}.duckdb" in files["profiles.yml"]


# ── characters YAML does not allow are escaped, and read back exactly (#61) ────
# YAML's printable set (what PyYAML/libyaml accept in a file), minus the line breaks
# YAML folds into a space inside a quoted scalar (NEL, LS, PS)
_YAML_RAW_FORBIDDEN = re.compile("[^\t\n\r\x20-\x7e\xa0-‧‪-퟿-�\U00010000-\U0010ffff]")


def test_yaml_forbidden_characters_are_escaped_and_read_back_exactly():
    """A C1 control (U+0096: cp1252 text read as latin-1) in a description, a column name
    or an accepted value made `dbt parse` fail: «unacceptable character #x0096»."""
    import json

    strani = ["Smith\x96Jones", "a\x7fb", "x\x85y", "l s", "p s", "z￾", "c\x01d", "eè–"]
    modelli = [{"name": "uno", "sql": "select 1", "materialized": "table", "description": strani[0], "meta": {"flow": strani[1]},
                "columns": [{"name": s, "description": s, "tests": [{"kind": "not_null", "severity": "error"}]} for s in strani]}]
    sorgenti = [{"name": "db_1_v", "database": "db_1", "schema": "v",
                 "tables": [{"name": "t", "description": strani[2], "columns": [{"name": s, "description": s} for s in strani]}]}]
    files = _progetto(models=modelli, sources=sorgenti, attachments=[{"alias": "db_1", "db_type": "postgresql", "pw_env": "P",
                                                                       "conn": {"host": "h\x96", "port": 5432, "database": "d", "username": "u"}}],
                      seeds=[{"name": "seed_x", "csv": "a\n1\n", "description": strani[3], "columns": [{"name": s, "description": ""} for s in strani],
                              "column_types": {s: "VARCHAR" for s in strani}}])
    for path in ("models/schema.yml", "models/sources.yml", "seeds/schema.yml", "profiles.yml"):
        assert not _YAML_RAW_FORBIDDEN.search(files[path]), (path, _YAML_RAW_FORBIDDEN.search(files[path]))
        valori = {json.loads(v) for v in re.findall(r'"(?:[^"\\]|\\.)*"', files[path])}
        if path != "profiles.yml":
            assert set(strani) <= valori, (path, set(strani) - valori)
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


def _famiglia_pl(dtype: str) -> str:
    t = str(dtype).lower()
    if t.startswith(("int", "uint")):
        return "int"
    if t.startswith(("float", "decimal")):
        return "float"
    if t.startswith("datetime"):
        return "datetime"
    return {"string": "str", "utf8": "str", "boolean": "bool", "date": "date"}.get(t, t)


def _famiglia_duck(t: str) -> str:
    t = str(t).upper()
    if t in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UBIGINT", "UINTEGER"):
        return "int"
    if t in ("DOUBLE", "FLOAT") or t.startswith("DECIMAL"):
        return "float"
    if t.startswith("TIMESTAMP"):
        return "datetime"
    return {"VARCHAR": "str", "BOOLEAN": "bool", "DATE": "date"}.get(t, t.lower())


def _key(nome: str) -> dict:
    from tests.conftest import BUCKET

    return {"bucket": BUCKET, "key": f"datasets/{nome}.parquet"}


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
    righe = sorted((tuple(_norm(r.get(c)) for c in cols) for r in res.rows), key=lambda t: tuple(map(str, t)))
    return cols, righe, [_famiglia_pl(c.dtype) for c in res.columns]


def _export_sql(sources: dict, main: str, ops: list, native: bool = False, dialetto=None, **kw):
    def resolve(spec):
        nome = spec["key"].split("/")[-1].removesuffix(".parquet")
        df = sources[nome]
        return f"t_{nome}", list(df.columns), {c: d.duck_type(str(t)) for c, t in df.schema.items()}, nome
    return d.compile_model_sql(_key(main), ops, resolve, native=native, dialetto=dialetto, **kw)


def _duck_with(sources: dict):
    import duckdb

    con = duckdb.connect()
    for nome, df in sources.items():
        con.register(f"_df_{nome}", df.to_arrow())
        con.execute(f'CREATE TABLE "t_{nome}" AS SELECT * FROM "_df_{nome}"')
    return con


def _export_snapshot(sources: dict, main: str, ops: list, **kw):
    sql, _, _ = _export_sql(sources, main, ops, **kw)
    rel = _duck_with(sources).sql(sql)
    cols, tipi = list(rel.columns), [str(t) for t in rel.types]
    righe = sorted((tuple(_norm(v) for v in r) for r in rel.fetchall()), key=lambda t: tuple(map(str, t)))
    return cols, righe, [_famiglia_duck(t) for t in tipi]


def _parita(sources: dict, main: str, ops: list, engines=("polars", "duckdb")):
    out = {e: _engine_snapshot(e, sources, main, ops) for e in engines}
    out["export"] = _export_snapshot(sources, main, ops)
    return out


_CLIENTI = _pl.DataFrame({"cliente": [1, 2, 3], "nome": ["a", "b", "c"]})
_VENDITE = _pl.DataFrame({"cliente": [1, 1, 2, 3], "anno": [2023, 2024, 2023, 2024], "importo": [10, 20, 30, 40]})
_PIVOT = {"type": "pivot", "params": {"index": ["cliente"], "on": "anno", "values": "importo", "func": "sum"}}


def _uguali(par: dict, tipi: bool = True):
    rif = par["polars"]
    for nome, snap in par.items():
        assert snap[0] == rif[0], f"{nome}: columns {snap[0]} != {rif[0]}"
        assert snap[1] == rif[1], f"{nome}: rows {snap[1]} != {rif[1]}"
        if tipi:
            assert snap[2] == rif[2], f"{nome}: types {snap[2]} != {rif[2]}"


# #0 — a join or a compute after a pivot (columns the data decides) loses nothing
def test_a_join_after_a_pivot_keeps_the_pivot_columns():
    src = {"clienti": _CLIENTI, "vendite": _VENDITE}
    destra = [{"type": "join", "params": {"how": "left", "on": "cliente", "right": {"source": _key("vendite"), "operations": [_PIVOT]}}}]
    par = _parita(src, "clienti", destra)
    assert par["polars"][0] == ["cliente", "nome", "2023", "2024"]
    _uguali(par, tipi=False)
    sinistra = [_PIVOT, {"type": "join", "params": {"how": "left", "on": "cliente", "right": {"source": _key("clienti")}}}]
    par = _parita(src, "vendite", sinistra)
    assert par["polars"][0] == ["cliente", "2023", "2024", "nome"]
    _uguali(par, tipi=False)
    # keys with other names: the pivot's index comes back as cliente_right, like the engine
    chiavi = [{"type": "join", "params": {"how": "full", "left_on": ["cliente"], "right_on": ["cliente"],
                                          "right": {"source": _key("vendite"), "operations": [_PIVOT]}}}]
    par = _parita(src, "clienti", chiavi)
    assert par["polars"][0] == ["cliente", "nome", "cliente_right", "2023", "2024"]
    _uguali(par, tipi=False)
    # the pivot on the LEFT, joined back on its index with left_on/right_on (what the editor writes as
    # soon as one key pair differs): the index is known, so the right one becomes cliente_right — the
    # model failed on a duplicate name
    for how in ("left", "inner", "full"):
        indietro = [_PIVOT, {"type": "join", "params": {"how": how, "left_on": ["cliente"], "right_on": ["cliente"],
                                                        "right": {"source": _key("clienti")}}}]
        par = _parita(src, "vendite", indietro)
        assert par["polars"][0] == ["cliente", "2023", "2024", "cliente_right", "nome"], how
        _uguali(par, tipi=False)
    # and a column the flow makes after the pivot, or renames, is known too; a dropped one is gone
    dopo = [_PIVOT, {"type": "compute", "params": {"columns": [{"name": "nome", "expr": "'x'"}]}},
            {"type": "rename", "params": {"mapping": {"cliente": "id"}}},
            {"type": "join", "params": {"how": "left", "left_on": ["id"], "right_on": ["cliente"], "right": {"source": _key("clienti")}}}]
    par = _parita(src, "vendite", dopo)
    assert par["polars"][0] == ["id", "2023", "2024", "nome", "cliente", "nome_right"]
    _uguali(par, tipi=False)
    tolto = [_PIVOT, {"type": "compute", "params": {"columns": [{"name": "nome", "expr": "'x'"}]}},
             {"type": "drop", "params": {"columns": ["nome"]}},
             {"type": "join", "params": {"how": "left", "left_on": ["cliente"], "right_on": ["cliente"], "right": {"source": _key("clienti")}}}]
    par = _parita(src, "vendite", tolto)
    assert par["polars"][0] == ["cliente", "2023", "2024", "cliente_right", "nome"]
    _uguali(par, tipi=False)


def test_a_pivot_column_that_the_other_side_also_has_gets_the_right_suffix():
    clienti = _CLIENTI.with_columns(_pl.Series("2023", ["x", "y", "z"]))
    src = {"clienti": clienti, "vendite": _VENDITE}
    ops = [{"type": "join", "params": {"how": "inner", "on": "cliente", "right": {"source": _key("vendite"), "operations": [_PIVOT]}}}]
    par = _parita(src, "clienti", ops)
    assert par["polars"][0] == ["cliente", "nome", "2023", "2023_right", "2024"]
    _uguali(par, tipi=False)


def test_a_pivot_on_the_left_whose_column_the_right_side_also_has_fails_the_run_loudly():
    """With the pivot on the LEFT, which right columns collide is decided by the data: the
    model cannot add `_right` by itself, so it stops with an error instead of renaming in silence."""
    import duckdb

    clienti = _CLIENTI.with_columns(_pl.Series("2024", ["x", "y", "z"]))
    src = {"clienti": clienti, "vendite": _VENDITE}
    ops = [_PIVOT, {"type": "join", "params": {"how": "left", "on": "cliente", "right": {"source": _key("clienti")}}}]
    sql, _, _ = _export_sql(src, "vendite", ops)
    with pytest.raises(duckdb.Error, match="2024"):
        _duck_with(src).sql(sql).fetchall()
    # without a collision the same model runs and gives what the engine gives
    _uguali(_parita({"clienti": _CLIENTI, "vendite": _VENDITE}, "vendite", ops), tipi=False)


def test_a_compute_after_a_pivot_replaces_the_column_in_place_or_adds_it():
    ops = [_PIVOT, {"type": "compute", "params": {"columns": [{"name": "2023", "expr": '"2023" * 2'},
                                                              {"name": "tot", "expr": 'coalesce("2023", 0) + coalesce("2024", 0)'}]}}]
    par = _parita({"vendite": _VENDITE}, "vendite", ops)
    assert par["polars"][0] == ["cliente", "2023", "2024", "tot"]
    _uguali(par, tipi=False)


def test_steps_after_a_pivot_never_claim_to_know_only_some_of_its_columns():
    """reorder and union by name after a pivot used to record a PARTIAL column list as the
    whole: the join after them then listed only those columns."""
    src = {"clienti": _CLIENTI, "vendite": _VENDITE}
    join = {"type": "join", "params": {"how": "left", "on": "cliente", "right": {"source": _key("clienti")}}}
    par = _parita(src, "vendite", [_PIVOT, {"type": "reorder", "params": {"columns": ["2024"]}}, join])
    assert par["polars"][0] == ["2024", "cliente", "2023", "nome"]
    _uguali(par, tipi=False)
    altri = _pl.DataFrame({"cliente": [9], "extra": ["e"]})
    src["altri"] = altri
    union = {"type": "union", "params": {"right": {"source": _key("altri")}}}
    par = _parita(src, "vendite", [_PIVOT, union, join], engines=("duckdb",))
    assert par["duckdb"][0] == ["cliente", "2023", "2024", "extra", "nome"]
    assert par["export"][:2] == par["duckdb"][:2]


# #3 — a cast after compute / sql / union uses the column's REAL type (truncates, guards text)
_ORDINI = _pl.DataFrame({"id": [1, 2, 3, 4], "importo": [10.7, 20.2, 5.9, -7.6], "testo": ["12.7", "1e3", " 42 ", "x"]})


def test_cast_to_int_of_a_computed_number_truncates_like_the_engine():
    ops = [{"type": "compute", "params": {"columns": [{"name": "x", "expr": "importo * 1"}, {"name": "t2", "expr": "testo || ''"}]}},
           {"type": "cast", "params": {"columns": {"x": "int", "t2": "int"}}}]
    par = _parita({"ordini": _ORDINI}, "ordini", ops)
    assert [r[3:] for r in par["polars"][1]] == [(10.0, None), (20.0, None), (5.0, 42.0), (-7.0, None)]
    _uguali(par)
    # native: the same guarded forms, written for the warehouse (no bare CAST of a double or a text)
    nat, _, _ = _export_sql({"ordini": _ORDINI}, "ordini", ops, native=True, dialetto="postgres")
    assert 'CAST(trunc("x") AS BIGINT)' in nat and "regexp_matches(trim(\"t2\")" in nat


def test_cast_after_a_sql_node_or_a_union_of_two_types_uses_the_real_type():
    sql_op = [{"type": "sql", "params": {"query": "SELECT id, importo AS f, testo FROM self"}},
              {"type": "cast", "params": {"columns": {"f": "int"}}}]
    _uguali(_parita({"ordini": _ORDINI}, "ordini", sql_op))
    altri = _pl.DataFrame({"id": [9], "importo": [1.5], "testo": ["7"]}).with_columns(_pl.col("testo").cast(_pl.String))
    union = [{"type": "union", "params": {"right": {"source": _key("altri")}}}, {"type": "cast", "params": {"columns": {"importo": "int"}}}]
    _uguali(_parita({"ordini": _ORDINI, "altri": altri}, "ordini", union))


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
    par = _parita({"vendite": _VENDITE}, "vendite", [_PIVOT, {"type": "cast", "params": {"columns": {"2023": "float"}}}])
    _uguali(par, tipi=False)
    assert par["export"][2] == ["int", "float", "int"]


# #3 #10 — a column of a type the export cannot name (a Postgres uuid, bytea, an array) no longer
# switches the probe off for the whole model: a compute that does not read it is cast, a sql node
# gets DuckDB's names; only what is made from that column stays unknown
def _con_un_uuid(_):
    return "t_src", ["uid", "x", "s"], {"uid": None, "x": "DOUBLE", "s": "VARCHAR"}, "src"


@pytest.mark.parametrize("dialetto", [None, "postgres", "clickhouse"])
def test_a_column_of_an_unknown_type_leaves_the_probe_on_for_the_others(dialetto):
    import duckdb

    nat = dialetto is not None
    ops = [{"type": "compute", "params": {"columns": [{"name": "y", "expr": "x * 2"}, {"name": "u", "expr": "CAST(uid AS VARCHAR)"}]}},
           {"type": "cast", "params": {"columns": {"y": "int", "u": "str"}}}]
    sql, cols, tipi = d.compile_model_sql({"key": "k"}, ops, _con_un_uuid, native=nat, dialetto=dialetto)
    assert cols == ["uid", "x", "s", "y", "u"] and tipi["y"] == "BIGINT" and tipi["uid"] is None
    if not nat:
        con = duckdb.connect()
        con.execute("CREATE TABLE t_src AS SELECT '5f0c6c2e-8a8f-4f39-9c7e-2f1b9d0a4e11'::UUID AS uid, 10.7 AS x, 'a' AS s")
        assert con.sql(sql).fetchall() == [(__import__("uuid").UUID("5f0c6c2e-8a8f-4f39-9c7e-2f1b9d0a4e11"), 10.7, "a", 21, "5f0c6c2e-8a8f-4f39-9c7e-2f1b9d0a4e11")]
    # a sql node's expression without an alias gets DuckDB's name, as in the engine
    q = [{"type": "sql", "params": {"query": "SELECT x + 1, s FROM self"}}, {"type": "sort", "params": {"by": ["s"]}}]
    _, cols, _ = d.compile_model_sql({"key": "k"}, q, _con_un_uuid, native=nat, dialetto=dialetto)
    assert cols == ["(x + 1)", "s"]
    # what is made from the unknown column itself is not known: that cast, and only it, is refused
    with pytest.raises(d.DbtExportError) as e:
        d.compile_model_sql({"key": "k"}, [{"type": "compute", "params": {"columns": [{"name": "z", "expr": "coalesce(uid, uid)"}]}},
                                           {"type": "cast", "params": {"columns": {"z": "str"}}}], _con_un_uuid, native=nat, dialetto=dialetto)
    assert e.value.code == "cast_type_unknown" and e.value.params == {"column": "z"}


@pytest.mark.parametrize("testo", ["clickhouse", "bigquery"])
def test_translated_text_that_reads_a_column_of_an_unknown_type_is_not_refused(testo):
    """The probe types such a column as a placeholder: text that reads it does not bind there,
    which says nothing about the warehouse (an array has a length) — not refused for that."""
    def resolve(_):
        return "t_src", ["id", "tags"], {"id": "BIGINT", "tags": None}, "src"
    leggi = "has(tags, 'a')" if testo == "clickhouse" else "ARRAY_LENGTH(tags)"
    ops = [{"type": "compute", "params": {"columns": [{"name": "n", "expr": leggi}]}},
           {"type": "sql", "params": {"query": f"SELECT id, {leggi} AS m FROM self"}}]
    _, cols, _ = d.compile_model_sql({"key": "k"}, ops, resolve, testo=testo)
    assert cols == ["id", "m"]
    # text that does not run in DuckDB for another reason is still refused
    with pytest.raises(d.DbtExportError) as e:
        d.compile_model_sql({"key": "k"}, [{"type": "compute", "params": {"columns": [{"name": "n", "expr": "nonexistent_fn(id)"}]}}], resolve, testo=testo)
    assert e.value.code == "text_not_translatable"


def test_the_probe_of_a_long_wide_flow_takes_well_under_a_second_per_hundred_steps():
    """The probe built a chain of views: each step re-bound the whole chain, so 150 columns over 120
    steps took 10 s or more (synchronously, in the engine's request). Each step is now an empty table
    read only by the next one."""
    import time

    cols = [f"c{i}" for i in range(150)]
    tipi = {c: ("DOUBLE" if i % 3 == 0 else "VARCHAR" if i % 3 == 1 else "BIGINT") for i, c in enumerate(cols)}
    ops = []
    for k in range(40):
        ops += [{"type": "compute", "params": {"columns": [{"name": f"c{k * 3 % 150}", "expr": f"c{k * 3 % 150} * 2"}]}},
                {"type": "filter", "params": {"column": f"c{(k * 3 + 1) % 150}", "operator": "ne", "value": "x"}},
                {"type": "cast", "params": {"columns": {f"c{k * 3 % 150}": "int"}}}]
    inizio = time.monotonic()
    _, _, finali = d.compile_model_sql({"key": "k"}, ops, lambda _: ("t_src", cols, tipi, "src"))
    assert time.monotonic() - inizio < 4, "the probe re-binds the whole chain at every step"
    assert finali["c0"] == "BIGINT" and finali["c1"] == "VARCHAR"


def test_a_column_of_an_unknown_type_on_native_postgres_casts_the_others():
    import uuid

    ops = [{"type": "compute", "params": {"columns": [{"name": "y", "expr": "x * 2"}]}}, {"type": "cast", "params": {"columns": {"y": "int"}}}]
    info: dict = {}
    raw, _, _ = d.compile_model_sql({"key": "k"}, ops, _con_un_uuid, native=True, dialetto="postgres", info=info)
    pg = d.rimetti_testi(d.transpile_model(raw, "postgres", {"t_src": ["uid", "x", "s"]}), info)
    valori = "(VALUES ('5f0c6c2e-8a8f-4f39-9c7e-2f1b9d0a4e11'::uuid, 10.7::float8, 'a')) AS \"t_src\"(\"uid\", \"x\", \"s\")"
    u = uuid.UUID("5f0c6c2e-8a8f-4f39-9c7e-2f1b9d0a4e11")
    [riga] = _su_postgres(pg.replace('"t_src" AS "t_src"', valori))
    assert riga[0] in (u, u.bytes) and riga[1:] == (10.7, "a", 21)   # the driver gives a uuid as its bytes


# #4 — aggregate types are the engine's, also read through a shared step
_PAGATI = _pl.DataFrame({"g": ["a", "a", "a", "b"], "pagato": [True, True, True, False],
                         "prezzo": [99.99, 99.99, 36.76, 1.0], "giorno": [_dt.date(2024, 1, 1), _dt.date(2024, 1, 3), _dt.date(2024, 1, 2), _dt.date(2024, 2, 1)]}
                        ).with_columns(_pl.col("prezzo").cast(_pl.Decimal(4, 2)))


def _attraverso_un_passo_condiviso(sources, main, primo, secondo):
    """Like the route: model A (a shared int_ step), then model B reading it with {model: A}
    and the column types the export recorded for A."""
    con = _duck_with(sources)
    sql_a, cols_a, tipi_a = _export_sql(sources, main, primo)
    con.execute(f"CREATE VIEW int_a AS {sql_a}")
    sql_b, _, _ = d.compile_model_sql({"model": "int_a"}, secondo, lambda spec: ("int_a", cols_a, tipi_a, "int_a"))
    rel = con.sql(sql_b)
    righe = sorted((tuple(_norm(v) for v in r) for r in rel.fetchall()), key=lambda t: tuple(map(str, t)))
    return list(rel.columns), righe, [str(t) for t in rel.types]


def test_a_sum_of_booleans_read_through_a_shared_step_stays_a_count():
    gb = [{"type": "group_by", "params": {"by": ["g"], "aggregations": [{"column": "pagato", "func": "sum", "alias": "n_pagati"},
                                                                         {"column": "prezzo", "func": "sum", "alias": "tot"}]}}]
    fil = [{"type": "filter", "params": {"column": "g", "operator": "eq", "value": "a"}}]
    cols, righe, tipi = _attraverso_un_passo_condiviso({"pagati": _PAGATI}, "pagati", gb, fil)
    rif = _engine_snapshot("polars", {"pagati": _PAGATI}, "pagati", gb + fil)
    assert (cols, righe) == (rif[0], rif[1]) == (["g", "n_pagati", "tot"], [("a", 3.0, 236.74)])
    assert tipi == ["VARCHAR", "BIGINT", "DECIMAL(38,2)"]


def test_the_median_and_mean_of_dates_read_through_a_shared_step_run():
    gb = [{"type": "group_by", "params": {"by": ["g"], "aggregations": [{"column": "giorno", "func": "median", "alias": "med"},
                                                                         {"column": "giorno", "func": "mean", "alias": "media"}]}}]
    cols, righe, tipi = _attraverso_un_passo_condiviso({"pagati": _PAGATI}, "pagati", gb, [{"type": "limit", "params": {"n": 10}}])
    rif = _engine_snapshot("duckdb", {"pagati": _PAGATI}, "pagati", gb)
    assert (cols, righe) == (rif[0], rif[1]) and tipi == ["VARCHAR", "TIMESTAMP", "TIMESTAMP"]


def test_integer_sums_after_a_compute_and_in_a_pivot_are_bigint_like_the_engine():
    ops = [{"type": "compute", "params": {"columns": [{"name": "k", "expr": "importo * 2"}]}},
           {"type": "group_by", "params": {"by": ["cliente"], "aggregations": [{"column": "k", "func": "sum", "alias": "tot"}]}}]
    _, _, tipi = _export_sql({"vendite": _VENDITE}, "vendite", ops)
    sql, _, _ = _export_sql({"vendite": _VENDITE}, "vendite", ops)
    assert [str(t) for t in _duck_with({"vendite": _VENDITE}).sql(sql).types] == ["BIGINT", "BIGINT"] and tipi["tot"] == "BIGINT"
    sql, _, _ = _export_sql({"vendite": _VENDITE}, "vendite", [_PIVOT])
    assert [str(t) for t in _duck_with({"vendite": _VENDITE}).sql(sql).types] == ["BIGINT", "BIGINT", "BIGINT"]


def test_a_model_read_by_another_is_not_cast_again_to_guessed_types():
    sql, _, _ = d.compile_model_sql({"model": "int_a"}, [], lambda spec: ("int_a", ["g", "n"], {"g": "VARCHAR", "n": "BIGINT"}, "int_a"))
    assert "CAST(" not in _codice(sql)
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
    _uguali(par)
    gb = ops[:2] + [{"type": "group_by", "params": {"by": ["misura"], "aggregations": [{"column": "valore", "func": "sum", "alias": "tot"}]}}]
    _uguali(_parita(src, "n", gb))


# #10 — a sql node's unaliased expressions keep the names DuckDB gives them
_CASI = _pl.DataFrame({"id": [1, 2, 3], "g": ["g1", "g1", "g2"], "i": [10, 20, 30], "s": ["x", None, "z"]})
_DESTRA = _pl.DataFrame({"id": [1, 2], "z": ["b1", "b2"]})


@pytest.mark.parametrize("query", ["SELECT id, i + 1 FROM self", "SELECT *, upper(s) FROM self", "SELECT COLUMNS('^(id|g)$') FROM self",
                                   "SELECT id, g, count(*) OVER (PARTITION BY g) FROM self"])
def test_an_unaliased_expression_in_a_sql_node_then_a_join_runs(query):
    ops = [{"type": "sql", "params": {"query": query}},
           {"type": "join", "params": {"how": "inner", "on": "id", "right": {"source": _key("destra")}}}]
    par = _parita({"casi": _CASI, "destra": _DESTRA}, "casi", ops, engines=("duckdb",))
    assert par["export"] == par["duckdb"]
    nul = [{"type": "sql", "params": {"query": query}}, {"type": "drop_nulls", "params": {}}]
    par = _parita({"casi": _CASI}, "casi", nul, engines=("duckdb",))
    assert par["export"] == par["duckdb"]


def test_the_publish_sort_on_a_sql_node_column_named_by_duckdb_is_kept():
    ops = [{"type": "sql", "params": {"query": "SELECT g, count(*) FROM self GROUP BY g"}},
           {"type": "sort", "params": {"by": ["count_star()"], "ignore_missing": True}}]
    sql, cols, _ = _export_sql({"casi": _CASI}, "casi", ops)
    assert cols == ["g", "count_star()"] and 'ORDER BY "count_star()" ASC NULLS LAST' in sql
    assert _duck_with({"casi": _CASI}).sql(sql).fetchall() == [("g2", 1), ("g1", 2)]


@pytest.mark.parametrize("query,nomi", [("SELECT id, i + 1 FROM self", ["id", "(i + 1)"]),
                                        ("SELECT *, upper(s) FROM self", ["id", "g", "i", "s", "upper(s)"]),
                                        ("SELECT g, count(*) FROM self GROUP BY g", ["g", "count_star()"])])
def test_on_native_a_sql_node_publishes_the_names_duckdb_gives(query, nomi):
    """Not sqlglot's made-up `_col_1` (and with a `*` next to the expression, sqlglot's
    count did not even match the translated query's)."""
    ops = [{"type": "sql", "params": {"query": query}}, {"type": "rename", "params": {"mapping": {"id": "id"}}}]
    nat, cols, _ = _export_sql({"casi": _CASI}, "casi", ops[:1] + ([] if "GROUP" in query else ops[1:]), native=True, dialetto="postgres")
    assert cols == nomi
    pg = " ".join(d.transpile_model(nat, "postgres", {"t_casi": list(_CASI.columns)}).split())
    assert "_col_" not in pg and f'AS "{nomi[-1]}"' in pg


# #8 — first/last follow the sort before them, and exist on every target
def _storico(n: int = 300_007) -> "_pl.DataFrame":
    # unique dates in a shuffled order (7919 is prime, so i*7919 mod n is a permutation)
    return _pl.DataFrame({"i": range(n)}).select(
        (_pl.col("i") % 7).alias("cliente"),
        (_pl.date(1000, 1, 1) + _pl.duration(days=(_pl.col("i") * 7919) % n)).alias("data"),
        ("s" + _pl.col("i").cast(_pl.String)).alias("stato"))


_PRIMO_ULTIMO = [{"type": "sort", "params": {"by": ["data"], "descending": True}},
                 {"type": "filter", "params": {"column": "cliente", "operator": "ge", "value": 0}},
                 {"type": "group_by", "params": {"by": ["cliente"], "aggregations": [
                     {"column": "data", "func": "first", "alias": "ultima"}, {"column": "stato", "func": "first", "alias": "stato_ultimo"},
                     {"column": "data", "func": "last", "alias": "prima"}]}}]


def test_first_and_last_after_a_sort_follow_the_sort_in_the_federated_model():
    """DuckDB's parallel aggregate does not keep the order of the CTE before it: sort by
    date then first per customer gave an arbitrary row; Polars gives the latest."""
    src = {"storico": _storico()}
    rif = _engine_snapshot("polars", src, "storico", _PRIMO_ULTIMO)
    sql, _, _ = _export_sql(src, "storico", _PRIMO_ULTIMO)
    con = _duck_with(src)
    con.execute("SET threads = 8")
    righe = sorted((tuple(_norm(v) for v in r) for r in con.sql(sql).fetchall()), key=lambda t: tuple(map(str, t)))
    assert righe == rif[1]


def test_first_and_last_exist_on_postgres_and_clickhouse_and_keep_the_order():
    src = {"storico": _storico(1009)}
    nat, _, _ = _export_sql(src, "storico", _PRIMO_ULTIMO, native=True, dialetto="postgres")
    pg = _codice(d.transpile_model(nat, "postgres", {"t_storico": ["cliente", "data", "stato"]}))
    assert "FIRST(" not in pg.upper() and "LAST(" not in pg.upper() and pg.count("ARRAY_AGG(") == 3 and "ORDER BY" in pg
    info: dict = {}
    ch_raw, _, _ = _export_sql(src, "storico", _PRIMO_ULTIMO, native=True, dialetto="clickhouse", info=info)
    ch = d.rimetti_testi(d.transpile_model(ch_raw, "clickhouse", {"t_storico": ["cliente", "data", "stato"]}), info)
    assert "FIRST(" not in _codice(ch).upper() and "arrayReverseSort(" in ch and "__tabularia_" not in ch
    # unordered (no sort before): still a function the warehouse has
    gb = _PRIMO_ULTIMO[2:]
    nat, _, _ = _export_sql(src, "storico", gb, native=True, dialetto="postgres")
    assert "FIRST(" not in _codice(d.transpile_model(nat, "postgres", {"t_storico": ["cliente", "data", "stato"]})).upper()
    info = {}
    ch_raw, _, _ = _export_sql(src, "storico", gb, native=True, dialetto="clickhouse", info=info)
    ch = d.rimetti_testi(d.transpile_model(ch_raw, "clickhouse", {"t_storico": ["cliente", "data", "stato"]}), info)
    assert "any(" in ch and "anyLast(" in ch and "FIRST(" not in _codice(ch).upper()


@_CON_CLICKHOUSE
def test_first_and_last_after_a_sort_run_on_clickhouse_like_polars():
    import os

    import clickhouse_connect

    df = _pl.DataFrame({"cliente": [1, 1, 1, 1, 2, 2], "data": [3, None, 1, 2, None, None], "stato": ["c", "n", "a", None, "x", "y"]})
    rif = _engine_snapshot("polars", {"t": df}, "t", [{"type": "sort", "params": {"by": ["data"]}}] + _PRIMO_ULTIMO[2:])
    ops = [{"type": "sort", "params": {"by": ["data"]}}] + _PRIMO_ULTIMO[2:]
    info: dict = {}
    raw, cols, _ = _export_sql({"t": df}, "t", ops, native=True, dialetto="clickhouse", info=info)
    ch = d.rimetti_testi(d.transpile_model(raw, "clickhouse", {"t_t": list(df.columns)}), info)
    valori = ", ".join(f"({c}, {'NULL' if x is None else x}, {'NULL' if s is None else repr(s)})" for c, x, s in df.iter_rows())
    ch = ch.replace('"t_t" AS "t_t"', f"(SELECT * FROM values('cliente Int64, data Nullable(Int64), stato Nullable(String)', {valori})) AS \"t_t\"")
    cli = clickhouse_connect.get_client(host=os.environ["CLICKHOUSE_TEST_HOST"], port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8123")),
                                        username=os.getenv("CLICKHOUSE_TEST_USER", "default"), password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""))
    righe = sorted((tuple(_norm(v) for v in r) for r in cli.query(ch).result_rows), key=lambda t: tuple(map(str, t)))
    assert righe == rif[1] == [(1.0, 1.0, "a", None), (2.0, None, "x", None)]


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

    codice = ("from app.engine import dbt_export as d\n"
              "r = lambda s: ('__s_0', ['id', 'canale', 'importo', 'stato', 'deposito', 'nota'], {})\n"
              "print(d.compile_model_sql({'key': 'k'}, [{'type': 'drop', 'params': {'columns': ['canale', 'importo', 'stato', 'deposito', 'canale']}}], r)[0])")
    uscite = set()
    for seme in ("1", "2", "3"):
        env = {**os.environ, "PYTHONHASHSEED": seme}
        uscite.add(subprocess.run([sys.executable, "-c", codice], env=env, capture_output=True, text=True, check=True).stdout)
    assert len(uscite) == 1
    assert 'EXCLUDE ("canale", "importo", "stato", "deposito")' in uscite.pop()


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

    for modo in ("duckdb", "native"):
        extra = {} if modo == "duckdb" else dict(
            mode="native", native={"db_type": "postgresql", "conn": {"host": "h", "port": 5432, "database": "d", "username": "u"}, "pw_env": "P"},
            source_map={"k": {"ident": "__s_0", "macro": "{{ source('s', 'ordini') }}", "columns": ["id", "x"], "types": {"id": "Int64", "x": "String"}, "label": "ordini"}})
        req = _richiesta_export(models=[{"name": "uno", "source": {"key": "k"}, "operations": [op]}], **extra)
        if modo == "native" and op["type"] == "pivot":
            continue                                                      # refused anyway on native, with its own code
        with pytest.raises(HTTPException) as e:
            _zip(req)
        assert e.value.status_code == 422 and e.value.detail["code"] == "step_invalid", (modo, e.value.detail)
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

    def guasto(*a, **k):
        raise TypeError("a bug of the exporter")
    monkeypatch.setattr(d, "_lista", guasto)
    monkeypatch.setattr(d, "_predicate", guasto)
    monkeypatch.setattr(d, "_aggregato", guasto)
    monkeypatch.setattr(d, "_espressione", guasto)
    with pytest.raises(HTTPException) as e:
        _zip(_richiesta_export(models=[{"name": "uno", "source": {"key": "k"}, "operations": [op]}]))
    assert e.value.status_code == 422 and e.value.detail["code"] == "internal", e.value.detail
    assert "a bug of the exporter" in e.value.detail["params"]["detail"]


def test_a_text_that_looks_like_the_exporters_placeholder_stays_as_it_is():
    """The texts written in the warehouse's dialect wait in placeholders until sqlglot is done; one
    the flow's own text spelled out was replaced too (now each export has its own random marker)."""
    ops = [{"type": "compute", "params": {"columns": [{"name": "a", "expr": "concat('__tabularia_t1__()', s)"},
                                                      {"name": "b", "expr": "upper(s)"}]}}]
    for _ in range(2):
        files = _files(_richiesta_export(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"},
                                         models=[{"name": "uno", "source": {"key": "k"}, "operations": ops, "dialect": "clickhouse"}],
                                         source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "s"],
                                                           "types": {"id": "Int64", "s": "String"}, "label": "ordini"}}))
        modello = files["models/uno.sql"]
        assert "'__tabularia_t1__()'" in modello and "upperUTF8(s)" in modello and modello.count("upperUTF8") == 1
    # and the zip stays the same, export after export
    assert _zip(_richiesta_export(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"},
                                  models=[{"name": "uno", "source": {"key": "k"}, "operations": ops, "dialect": "clickhouse"}],
                                  source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "s"],
                                                    "types": {"id": "Int64", "s": "String"}, "label": "ordini"}})) == \
        _zip(_richiesta_export(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"},
                               models=[{"name": "uno", "source": {"key": "k"}, "operations": ops, "dialect": "clickhouse"}],
                               source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "s"],
                                                 "types": {"id": "Int64", "s": "String"}, "label": "ordini"}}))


def test_a_lone_surrogate_in_the_flows_text_does_not_break_the_zip():
    """A lone surrogate (half an emoji pasted from a browser) in a description reached README.md and
    INTEGRATION.md as it was: UTF-8 cannot write it, and the export ended in a 500. It becomes U+FFFD."""
    import io
    import zipfile

    for layout in (None, {"package": "folder", "folder": "f"}):
        req = _richiesta_export(flow_description="prima \ud800 dopo", layout=layout,
                                models=[{"name": "uno", "source": {"key": "k"}, "description": "modello \udfff", "operations": []}])
        with zipfile.ZipFile(io.BytesIO(_zip(req))) as z:
            testi = {n: z.read(n).decode("utf-8") for n in z.namelist()}
        leggimi = testi.get("README.md") or testi["INTEGRATION.md"]
        assert "prima \ufffd dopo" in leggimi
        for nome, testo in testi.items():
            assert not re.search("[\ud800-\udfff]", testo), nome


# #7 — compute and sql text is read in the dialect of the flow's engine
def _modelli_con_dialetto(dialetto, ops):
    return [{"name": "uno", "source": {"key": "k"}, "operations": ops, "dialect": dialetto}]


def _tipi_k():
    return {"k": {"ref": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "n", "d", "s"],
                  "types": {"id": "Int64", "n": "Float64", "d": "Date", "s": "String"}, "label": "ordini"}}


def test_the_model_spec_carries_the_dialect_of_its_free_text():
    from app.api.routes import dbt as rotta

    assert rotta.ModelSpec(name="m", dialect="polars").dialect == "polars"
    assert rotta.ModelSpec(name="m").dialect is None


def test_polars_text_is_kept_and_listed_for_review_in_the_readme_and_the_model():
    ops = [{"type": "compute", "params": {"columns": [{"name": "x", "expr": "CAST(n AS INTEGER)"}]}},
           {"type": "sql", "params": {"query": "SELECT id, x FROM self"}}]
    files = _files(_richiesta_export(source_map=_tipi_k(), models=_modelli_con_dialetto("polars", ops)))
    modello = files["models/uno.sql"]
    assert "(CAST(n AS INTEGER)) AS \"x\"" in modello
    assert "-- Written for the Polars engine, review: compute_x, custom_sql." in modello
    assert "`uno`" in files["README.md"] and "Polars engine" in files["README.md"]
    # a DuckDB flow's text needs no review
    files = _files(_richiesta_export(source_map=_tipi_k(), models=_modelli_con_dialetto("duckdb", ops)))
    assert "review" not in files["models/uno.sql"].lower() and "Polars" not in files["README.md"]


def test_clickhouse_text_is_translated_for_the_federated_target_and_runs():
    src = {"k": _pl.DataFrame({"id": [1, 2], "n": [2.5, -7.0], "d": [_dt.date(2024, 3, 9), _dt.date(2023, 12, 31)], "s": ["città", "x"]})}
    ops = [{"type": "compute", "params": {"columns": [{"name": "mese", "expr": "toStartOfMonth(d)"}, {"name": "u", "expr": "if(n > 0, 'pos', 'neg')"}]}},
           {"type": "sql", "params": {"query": "SELECT id, mese, u, countIf(n > 0) OVER () AS c FROM self"}}]
    sql, cols, _ = _export_sql(src, "k", ops, testo="clickhouse")
    assert "toStartOfMonth" not in _codice(sql) and "countIf" not in _codice(sql)
    righe = _duck_with(src).sql(sql).fetchall()
    assert cols == ["id", "mese", "u", "c"] and sorted(r[:3] for r in righe) == [(1, _dt.datetime(2024, 3, 1), "pos"), (2, _dt.datetime(2023, 12, 1), "neg")]
    files = _files(_richiesta_export(source_map=_tipi_k(), models=_modelli_con_dialetto("clickhouse", ops)))
    assert "Translated from ClickHouse SQL by sqlglot, review: compute_mese, compute_u, custom_sql." in files["models/uno.sql"]


def test_clickhouse_text_duckdb_does_not_run_is_refused_for_the_federated_target():
    from fastapi import HTTPException

    ops = [{"type": "compute", "params": {"columns": [{"name": "anno", "expr": "toYear(d)"}]}}]
    with pytest.raises(HTTPException) as e:
        _zip(_richiesta_export(source_map=_tipi_k(), models=_modelli_con_dialetto("clickhouse", ops)))
    det = e.value.detail
    assert det["code"] == "text_not_translatable" and det["params"]["step"] == "compute_anno" and det["params"]["dialect"] == "clickhouse"
    assert "toyear" in det["params"]["detail"].lower()


def test_clickhouse_text_is_used_as_it_is_on_the_clickhouse_target():
    ops = [{"type": "compute", "params": {"columns": [{"name": "meta", "expr": "intDiv(id, 2)"}, {"name": "up", "expr": "upper(s)"}]}},
           {"type": "sql", "params": {"query": "SELECT id, meta, up, toYear(d) AS anno FROM self"}}]
    req = _richiesta_export(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "dbt_tabularia"},
                            source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "n", "d", "s"],
                                              "types": {"id": "Int64", "n": "Float64", "d": "Date", "s": "String"}, "label": "ordini"}},
                            models=_modelli_con_dialetto("clickhouse", ops))
    modello = _files(req)["models/uno.sql"]
    # as the ClickHouse engine runs it: upper on characters (upperUTF8), not on bytes
    assert "intDiv(id, 2)" in modello and "upperUTF8(s)" in modello and "toYear(d)" in modello and "__tabularia_" not in modello
    assert "review" not in modello.lower()


def test_duckdb_text_keeps_its_unicode_semantics_on_clickhouse():
    """DuckDB's upper/lower/substring/length/reverse/left work on characters, ClickHouse's on
    bytes: 'città' became 'CITTà'."""
    ops = [{"type": "compute", "params": {"columns": [{"name": "u", "expr": "upper(s)"}, {"name": "p", "expr": "left(s, 4)"},
                                                      {"name": "r", "expr": "reverse(lower(s))"}, {"name": "m", "expr": "substring(s, 2, 3)"}]}},
           {"type": "filter", "params": {"column": "s", "operator": "starts_with", "value": "cit"}}]
    req = _richiesta_export(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "dbt_tabularia"},
                            source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "s"],
                                              "types": {"id": "Int64", "s": "String"}, "label": "ordini"}},
                            models=_modelli_con_dialetto("duckdb", ops))
    modello = _codice(_files(req)["models/uno.sql"])
    for f in ("upperUTF8(", "leftUTF8(", "reverseUTF8(", "lowerUTF8(", "substringUTF8("):
        assert f in modello, f
    assert not re.search(r"\b(UPPER|LOWER|REVERSE|SUBSTRING|LEFT)\(", modello)


def test_a_compute_with_a_trailing_comment_does_not_swallow_the_rest_of_the_line():
    src = {"k": _pl.DataFrame({"id": [1, 2], "n": [1.0, 2.0]})}
    ops = [{"type": "compute", "params": {"columns": [{"name": "doppio", "expr": "n * 2 -- double it"}]}}]
    sql, _, _ = _export_sql(src, "k", ops)
    assert sorted(_duck_with(src).sql(sql).fetchall()) == [(1, 1.0, 2.0), (2, 2.0, 4.0)]
    nat, _, _ = _export_sql(src, "k", ops, native=True, dialetto="postgres")
    assert "n * 2" in d.transpile_model(nat, "postgres", {"t_k": ["id", "n"]})


def test_bigquery_text_is_translated_for_duckdb():
    src = {"k": _pl.DataFrame({"id": [1, 2], "t": ["7", "x"]})}
    ops = [{"type": "compute", "params": {"columns": [{"name": "v", "expr": "SAFE_CAST(t AS INT64)"}]}}]
    sql, _, _ = _export_sql(src, "k", ops, testo="bigquery")
    assert sorted(_duck_with(src).sql(sql).fetchall()) == [(1, "7", 7), (2, "x", None)]


def test_a_sql_node_with_a_pivot_has_columns_the_data_decides():
    """The probe reads empty views: a PIVOT in a sql node would give it no pivot column, and
    the steps after would list only the others. Its columns are not known, as after a pivot node."""
    src = {"vendite": _VENDITE, "clienti": _CLIENTI}
    ops = [{"type": "sql", "params": {"query": "PIVOT self ON anno USING sum(importo) GROUP BY cliente"}},
           {"type": "join", "params": {"how": "left", "on": "cliente", "right": {"source": _key("clienti")}}}]
    _, cols, _ = _export_sql(src, "vendite", ops)
    assert cols == []
    par = _parita(src, "vendite", ops, engines=("duckdb",))
    assert par["export"][:2] == par["duckdb"][:2] and par["duckdb"][0] == ["cliente", "2023", "2024", "nome"]


# ── the first step's types, the native targets' casts and comparisons (E3) ────
_UTC = "Datetime(time_unit='us', time_zone='UTC')"
_PG_NATIVE = {"db_type": "postgresql", "conn": {"host": "h", "port": 5432, "database": "d", "username": "u"}, "pw_env": "P"}


def _esporta(types: dict, ops: list, mode: str = "duckdb", cols: list | None = None) -> dict:
    """The project the route writes for one model over a source table `ordini` whose
    columns Tabularia read with these dtypes (None: a type the export cannot read)."""
    cols = cols or list(types)
    if mode == "duckdb":
        voce, extra = {"ref": "{{ source('db_1_v', 'ordini') }}"}, {}
    else:
        voce = {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}"}
        extra = {"native": _PG_NATIVE} if mode == "native" else {"clickhouse": {"connections": [_PG], "target_schema": "dbt_tabularia"}}
    return _files(_richiesta_export(mode=mode, **extra, source_map={"k": {**voce, "columns": cols, "types": types, "label": "ordini"}},
                                    models=[{"name": "uno", "source": {"key": "k"}, "operations": ops}]))


def _modello(files: dict, sorgente: str = "ordini") -> str:
    """The model as dbt renders it, with the source table replaced by `sorgente`."""
    return _render_like_dbt(files["models/uno.sql"], {("source", "db_1_v", "ordini"): sorgente}, config={})


def _pg():
    """A connection to a real Postgres (POSTGRES_TEST_URI), for the native SQL: only SELECTs over VALUES."""
    import os

    uri = os.getenv("POSTGRES_TEST_URI")
    if not uri:
        _manca("postgres", "POSTGRES_TEST_URI")
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


def _su_postgres(sql: str, *prima: str) -> list:
    with _pg() as con, con.cursor() as cur:
        for s in prima:
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
    files = _esporta({"id": "Int32", "ts": _UTC}, ops)
    con = duckdb.connect()
    con.execute("SET TimeZone = 'Europe/Rome'")
    con.execute("CREATE TABLE ordini AS SELECT * FROM (VALUES (1, TIMESTAMPTZ '2024-01-15 10:30:00+00'), "
                "(2, TIMESTAMPTZ '2024-07-15 23:30:00+00')) t(id, ts)")
    righe = con.execute(f"SELECT * FROM ({_modello(files)}) ORDER BY id").fetchall()
    assert righe == [(1, _dt.datetime(2024, 1, 15, 10, 30), _dt.date(2024, 1, 15)), (2, _dt.datetime(2024, 7, 15, 23, 30), _dt.date(2024, 7, 15))]
    # and the project's DuckDB works in UTC, as the engine does, for whatever the flow does with time zones
    assert "      settings:\n        TimeZone: UTC\n" in files["profiles.yml"]
    folder = _progetto(layout={"package": "folder", "folder": "f"})["INTEGRATION.md"]
    assert "TimeZone: UTC" in folder


def test_a_timestamptz_source_on_native_postgres_is_read_in_utc():
    files = _esporta({"id": "Int32", "ts": _UTC}, [], mode="native")
    sql = " ".join(_codice(files["models/uno.sql"]).split())
    assert "AT TIME ZONE 'UTC'" in sql
    # the ClickHouse target: a DateTime is already an instant, nothing to convert
    assert "AT TIME ZONE" not in _codice(_esporta({"id": "Int32", "ts": _UTC}, [], mode="clickhouse")["models/uno.sql"])
    valori = "(SELECT * FROM (VALUES (1, timestamptz '2024-01-15 10:30:00+00'), (2, timestamptz '2024-07-15 23:30:00+00')) AS t(id, ts))"
    righe = _su_postgres(f"SELECT * FROM ({_modello(files, valori)}) AS m ORDER BY id", "SET TIME ZONE 'Europe/Rome'")
    assert righe == [(1, _dt.datetime(2024, 1, 15, 10, 30)), (2, _dt.datetime(2024, 7, 15, 23, 30))]


def test_a_timestamptz_column_of_a_datasource_query_is_read_in_utc_too():
    raw = [{"name": "src_q", "kind": "raw", "materialized": "ephemeral", "raw_sql": "SELECT id, ts FROM ordini",
            "columns": [{"name": "id", "dtype": "Int32"}, {"name": "ts", "dtype": _UTC}]}]
    fed = _files(_richiesta_export(models=raw, source_map={}))["models/src_q.sql"]
    assert "CAST(\"ts\" AS TIMESTAMPTZ) AT TIME ZONE 'UTC' AS \"ts\"" in fed
    nat = _files(_richiesta_export(models=raw, source_map={}, mode="native", native=_PG_NATIVE))["models/src_q.sql"]
    assert "AT TIME ZONE 'UTC' AS \"ts\"" in nat
    righe = _su_postgres(_render_like_dbt(nat, config={}).replace("FROM ordini", "FROM (VALUES (1, timestamptz '2024-07-15 23:30:00+00')) AS ordini(id, ts)"),
                         "SET TIME ZONE 'Europe/Rome'")
    assert righe == [(1, _dt.datetime(2024, 7, 15, 23, 30))]


# #17 — a native cast checks the VALUE: what the engine turns into NULL is NULL, never a failed
# model (Postgres) nor a made-up value (ClickHouse: 2024-02-30 → 1 March, 1e20 → INT64_MIN)
_DATE_TXT = ["2024-02-29", "2023-02-29", "2024-02-30", "2024-13-01", "2024-00-10", "0000-00-00", "1900-02-29", "2000-02-29",
             " 2024-03-10 ", "2024-04-31", "2024-12-31", "1999-11-30", "x", "", None]
_ISTANTI_TXT = ["2024-01-05 10:30:00", "2024-01-05T10:30:00", "2024-01-05 10:30", "2024-01-05T10:30", "2024-01-05 23:59:59",
                "2024-01-05 25:00:00", "2024-01-05 23:60:00", "2024-01-05 10:61:00", "2024-01-05 10:30:61", "2024-02-30 10:00:00",
                "2023-02-29T00:00:00", "2024-02-29T12:00:00", " 2024-03-10 08:00:00 ", "2024-01-05 10:30:00.1234567",
                "2024-01-05 10:30:00.5", "2024-01-05", "x", None]
_INTERI_TXT = ["99999999999999999999", "-9223372036854775808", "9223372036854775807", "9223372036854775808", "-9223372036854775809",
               "+5", "007", " 42 ", "1e3", "12.0", "-0", "", "0000000000000000000009223372036854775807", "x", None]
_NUMERI = [1e20, -1e20, float("nan"), float("inf"), float("-inf"), 9.223372036854775807e18, -9.223372036854775808e18,
           9.2233720368547748e18, 100.9, -2.7, 0.0, None]
# the shapes both engines (Polars and DuckDB) convert, with the same value, beyond the canonical
# ones: a month or a day of one digit, a year of fewer than four (year 24, not 2024), an instant
# with a 'Z' or an offset (dropped: the wall-clock time stays), an hour, a minute or a second of one
# digit, 'inf', 'nan', a number beyond the double range (±inf, ±0). Native gave NULL, '1e400' failed
# the whole Postgres model. Where the engines disagree ('2024/01/01', '24:00:00', '1_000.5') the
# native targets give NULL, as one of them does.
_DATE_TXT_BREVI = ["2024-1-1", "2024-01-5", "2024-1-05", "2024-12-1", " 2024-1-1 ", "2024-2-29", "2023-2-29", "2024-4-31", "2024-1-32",
                   "2024-13-1", "2024-0-1", "1999-9-9"]
_DATE_TXT_ANNI = ["24-01-05", "4-1-5", "024-2-29", "100-2-29", "400-2-29", "0024-01-05", "1899-12-31", "2300-01-01"]
_ISTANTI_TXT_BREVI = ["2024-01-05 10:30:00Z", "2024-01-05T10:30:00Z", "2024-01-05 10:30:00+02", "2024-01-05 10:30:00+02:00",
                      "2024-01-05 10:30:00-0530", "2024-01-05 10:30:00-05:30", "2024-01-05 10:30:00.5Z", "2024-01-05 10:30:00.1234567+01:00",
                      "2024-01-05 10:30:00+25", "2024-1-5T10:30:00", "2024-01-05 1:30", "2024-01-05 10:3", "2024-01-05 1:3:5",
                      "2024-01-05 10:30:5", "2024-01-05 10:30:00.", "2024-01-05T10:30:05.Z", "2023-2-29 10:00", "2024-01-05Z",
                      "2024-01-05 10:60", "2024-01-05T", "2024-01-05 10"]
# where the two engines disagree, the native targets give what the DuckDB engine (the federated
# target) gives: a value for a one-digit month or day before a space, NULL for a 'Z' after the
# minutes, a 'z', an offset after a space or of one digit, a 60th second
_ISTANTI_TXT_DUCKDB = ["2024-1-5 10:30", "2024-01-5 10:30:00", "2024-2-29 23:59:59", " 2024-1-5 10:30 ", "2024-01-05 10:30Z",
                       "2024-01-05 10:30:00 +02:00", "2024-01-05 10:30:00z", "2024-01-05 23:59:60", "2024-01-05 10:30:00+2"]
_ISTANTI_TXT_ANNI = ["24-01-05T10:30", "0024-01-05 10:30:00", "1899-12-31 23:59:59", "2300-01-01 00:00:00"]
_DOPPI_TXT = ["inf", "-inf", "+inf", "Inf", "INF", "infinity", "-Infinity", "+Infinity", "nan", "NaN", "-nan", "+nan", " nan ",
              "1e400", "-1e400", "1e-400", "-1e-400", "1.8e308", "-1.8e308", "1.7976931348623157e308", "2e-324",
              "1e99999", "-1e99999", "1e-99999", "0e99999", "-0.00e99999", "0e-99999", ".0e5", "1e00005", "-2.5e-00007",
              "1e+0000000000400", "1E+400", " .5", "1.", "-.5e3", "+.5", "-0", "+1e+3", "1e", "1d", "0x10",
              "inf5", "nan1", "x", "", None]
# a subnormal double: ClickHouse's float parsing (the ClickHouse engine's too) reads it as 0
_DOPPI_SUBNORMALI_TXT = ["4.9e-324", "1e-310", "-2.5e-320"]
_BOOLEANI_TXT = ["true", "TRUE", "True", "t", "T", "1", "yes", "YES", "y", "Y", " yes ", "false", "FALSE", "f", "F", "0", "no", "No",
                 "n", "N", "on", "off", "2", "tru", "ye", "-1", "01", "", "x", None]
_CASI_CAST = [("VARCHAR", "date", _DATE_TXT), ("VARCHAR", "datetime", _ISTANTI_TXT), ("VARCHAR", "int", _INTERI_TXT),
              ("DOUBLE", "int", _NUMERI), ("VARCHAR", "date", _DATE_TXT_BREVI), ("VARCHAR", "datetime", _ISTANTI_TXT_BREVI),
              ("VARCHAR", "datetime", _ISTANTI_TXT_DUCKDB), ("VARCHAR", "float", _DOPPI_TXT), ("VARCHAR", "bool", _BOOLEANI_TXT)]
# what ClickHouse cannot hold (Date32 and DateTime64 span 1900-2299; a subnormal double): Postgres keeps it
_CASI_CAST_OLTRE_CLICKHOUSE = [("VARCHAR", "date", _DATE_TXT_ANNI), ("VARCHAR", "datetime", _ISTANTI_TXT_ANNI),
                   ("VARCHAR", "float", _DOPPI_SUBNORMALI_TXT)]


def _riferimento_cast(src: str, dt: str, valori: list) -> list:
    """What the DuckDB engine (the federated target's own cast) gives."""
    import duckdb

    from app.engine.duckdb_ops import _DUCK_DTYPES, _cast_expr

    con = duckdb.connect()
    con.execute("SET TimeZone = 'UTC'")
    con.execute(f"CREATE TABLE t_casi (i BIGINT, v {src})")
    con.executemany("INSERT INTO t_casi VALUES (?, ?)", list(enumerate(valori)))
    return con.execute(f"SELECT i, {_cast_expr('v', src, _DUCK_DTYPES[dt])} FROM t_casi ORDER BY i").fetchall()


def _cast_nativo(src: str, dt: str, dialetto: str, info: dict | None = None) -> str:
    def resolve(_):
        return "t_casi", ["i", "v"], {"i": "BIGINT", "v": src}, "casi"
    sql, _, _ = d.compile_model_sql({"key": "casi"}, [{"type": "cast", "params": {"columns": {"v": dt}}}], resolve,
                                    native=True, dialetto=dialetto, info=info)
    return sql


def _cast_nativo_in_duckdb(src: str, dt: str, dialetto: str) -> str:
    """The native cast with the text written for the warehouse (see `_Model.testo_nativo`) in its
    DuckDB form, the one the probe reads."""
    info: dict = {}
    sql = _cast_nativo(src, dt, dialetto, info)
    return d.rimetti_testi(sql, {"segnaposti": info.get("per_sonda")})


def _uguali_ai_valori(got: list, exp: list, valori: list, ctx: str):
    for (i, a), (_, b) in zip(sorted(got), exp):
        uguali = (a == b) or (isinstance(a, float) and isinstance(b, float) and a != a and b != b)
        assert uguali, f"{ctx}: {valori[i]!r} gives {a!r}, the engine {b!r}"
    assert len(got) == len(exp), ctx


def _cast_in_duckdb(src: str, dt: str, valori: list, dialetti=("postgres", "clickhouse")):
    """The native guards run in DuckDB, which fails a CAST of '2024-02-30' or of 1e20 to BIGINT
    as Postgres does: a value a guard lets through that the warehouse cannot convert fails here."""
    import duckdb

    rif = _riferimento_cast(src, dt, valori)
    con = duckdb.connect()
    con.execute(f"CREATE TABLE t_casi (i BIGINT, v {src})")
    con.executemany("INSERT INTO t_casi VALUES (?, ?)", list(enumerate(valori)))
    for dialetto in dialetti:
        got = con.execute(f"SELECT * FROM ({_cast_nativo_in_duckdb(src, dt, dialetto)}) ORDER BY i").fetchall()
        _uguali_ai_valori(got, rif, valori, dialetto)


_TIPO_PG = {"VARCHAR": "TEXT", "DOUBLE": "DOUBLE PRECISION", "BIGINT": "BIGINT", "BOOLEAN": "BOOLEAN"}


def _letterale_pg(v, src: str) -> str:
    if v is None:
        return f"CAST(NULL AS {_TIPO_PG[src]})"
    if isinstance(v, float):
        return f"CAST('{'NaN' if v != v else ('Infinity' if v == float('inf') else ('-Infinity' if v == float('-inf') else repr(v)))}' AS DOUBLE PRECISION)"
    if isinstance(v, (bool, int)):
        return f"CAST({str(v).upper() if isinstance(v, bool) else v} AS {_TIPO_PG[src]})"
    return "'" + str(v).replace("'", "''") + "'"


def _cast_su_postgres(src: str, dt: str, valori: list):
    rif = _riferimento_cast(src, dt, valori)
    info: dict = {}
    pg = d.rimetti_testi(d.transpile_model(_cast_nativo(src, dt, "postgres", info), "postgres", {"t_casi": ["i", "v"]}), info)
    righe = ", ".join(f"({i}, {_letterale_pg(v, src)})" for i, v in enumerate(valori))
    pg = pg.replace('"t_casi" AS "t_casi"', f'(VALUES {righe}) AS "t_casi"("i", "v")')
    _uguali_ai_valori(_su_postgres(pg), rif, valori, "postgres")


def _clickhouse():
    import os

    import clickhouse_connect

    if not os.getenv("CLICKHOUSE_TEST_HOST"):
        _manca("clickhouse", "CLICKHOUSE_TEST_HOST")
    return clickhouse_connect.get_client(host=os.environ["CLICKHOUSE_TEST_HOST"], port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8123")),
                                         username=os.getenv("CLICKHOUSE_TEST_USER", "default"), password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""))


_TIPO_CH = {"VARCHAR": "Nullable(String)", "DOUBLE": "Nullable(Float64)", "BIGINT": "Nullable(Int64)", "BOOLEAN": "Nullable(Bool)"}


def _cast_su_clickhouse(src: str, dt: str, valori: list):
    cli = _clickhouse()
    rif = _riferimento_cast(src, dt, valori)
    info: dict = {}
    ch = d.rimetti_testi(d.transpile_model(_cast_nativo(src, dt, "clickhouse", info), "clickhouse", {"t_casi": ["i", "v"]}), info)

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
    righe = ", ".join(f"({i}, {lit(v)})" for i, v in enumerate(valori))
    ch = ch.replace('"t_casi" AS "t_casi"', f"(SELECT * FROM values('i Int64, v {_TIPO_CH[src]}', {righe})) AS \"t_casi\"")
    _uguali_ai_valori([tuple(r) for r in cli.query(ch).result_rows], rif, valori, "clickhouse")


@pytest.mark.parametrize("src,dt,valori", _CASI_CAST, ids=lambda x: x if isinstance(x, str) else None)
def test_a_native_cast_never_fails_the_model_and_gives_the_engines_nulls(src, dt, valori):
    _cast_in_duckdb(src, dt, valori)


@pytest.mark.parametrize("src,dt,valori", _CASI_CAST, ids=lambda x: x if isinstance(x, str) else None)
def test_a_native_cast_on_postgres_gives_what_the_engine_gives(src, dt, valori):
    _cast_su_postgres(src, dt, valori)


@pytest.mark.parametrize("src,dt,valori", _CASI_CAST, ids=lambda x: x if isinstance(x, str) else None)
def test_a_native_cast_on_clickhouse_gives_what_the_engine_gives(src, dt, valori):
    _cast_su_clickhouse(src, dt, valori)


@pytest.mark.parametrize("src,dt,valori", _CASI_CAST_OLTRE_CLICKHOUSE, ids=lambda x: x if isinstance(x, str) else None)
def test_a_native_cast_on_postgres_keeps_what_clickhouse_cannot_hold(src, dt, valori):
    _cast_in_duckdb(src, dt, valori, dialetti=("postgres",))
    _cast_su_postgres(src, dt, valori)


@pytest.mark.parametrize("src,dt,valori", [c for c in _CASI_CAST_OLTRE_CLICKHOUSE if c[1] != "float"],
                         ids=lambda x: x if isinstance(x, str) else None)
def test_a_native_cast_on_clickhouse_gives_null_for_a_year_it_cannot_hold(src, dt, valori):
    """ClickHouse moved 1899-12-31 to 1900-01-01 and year 24 to 1900: a made-up value. NULL instead,
    as the ClickHouse engine gives; a year it can hold keeps its value. (A subnormal double is a
    limit of ClickHouse's float parsing, not of the export: not here.)"""
    cli = _clickhouse()
    info: dict = {}
    ch = d.rimetti_testi(d.transpile_model(_cast_nativo(src, dt, "clickhouse", info), "clickhouse", {"t_casi": ["i", "v"]}), info)
    righe = ", ".join(f"({i}, '{v}')" for i, v in enumerate(valori))
    got = dict(cli.query(ch.replace('"t_casi" AS "t_casi"', f"(SELECT * FROM values('i Int64, v Nullable(String)', {righe})) AS \"t_casi\"")).result_rows)
    assert all(v is None for v in got.values()), got
    entro = ["1900-01-01", "2299-12-31"] if dt == "date" else ["1900-01-01 00:00:00", "2299-12-31 23:59:59"]
    _cast_su_clickhouse(src, dt, entro)


def _scritture_di_date_e_istanti() -> dict:
    """Many writings of days and instants: the shapes the native guards were measured on."""
    giorni = [f"{a}-{m}-{g}" for a in ("2024", "24", "4", "024", "0024", "02024", "1900", "2000", "2100") for m in ("1", "01", "2", "02", "001", "13")
              for g in ("5", "05", "005", "29", "31")]
    orari = ("10:30", "1:3", "9:05", "10:30:5", "10:30:00", "10:30:00.", "10:30:00.5", "10:30:00.1234567", "24:00", "23:59:60", "10:60", "10")
    fusi = ("", "Z", "z", "+02", "+2", "+0200", "+02:00", "-05:30", " +02:00", "+02:00:00", "+020", "UTC", " Z")
    istanti = [f"{g}{sep}{o}{f}" for g in ("2024-01-05", "2024-1-5", "24-01-05", "2024-02-30") for sep in (" ", "T", "  ", "t", "\t", " \n", "\r", "\u00a0")
               for o in orari for f in fusi] + [f"2024-01-05{f}" for f in fusi]
    return {"date": giorni, "datetime": istanti}


def _cast_nativo_di(dove: str, dialetto: str, src: str, dt: str, valori: list) -> list:
    """The values the native cast gives, run in DuckDB (the warehouse's SQL in its DuckDB form)
    or in the warehouse itself (`dove`)."""
    if dove == "duckdb":
        import duckdb

        con = duckdb.connect()
        con.execute(f"CREATE TABLE t_casi (i BIGINT, v {src})")
        con.executemany("INSERT INTO t_casi VALUES (?, ?)", list(enumerate(valori)))
        return [v for _, v in con.execute(f"SELECT * FROM ({_cast_nativo_in_duckdb(src, dt, dialetto)}) ORDER BY i").fetchall()]
    info: dict = {}
    sql = d.rimetti_testi(d.transpile_model(_cast_nativo(src, dt, dialetto, info), dialetto, {"t_casi": ["i", "v"]}), info)
    if dialetto == "postgres":
        righe = ", ".join(f"({i}, {_letterale_pg(v, src)})" for i, v in enumerate(valori))
        return [v for _, v in sorted(_su_postgres(sql.replace('"t_casi" AS "t_casi"', f'(VALUES {righe}) AS "t_casi"("i", "v")')))]
    cli = _clickhouse()
    lit = lambda v: "NULL" if v is None else "'" + str(v).replace("\\", "\\\\").replace("'", "\\'") + "'"   # noqa: E731
    righe = ", ".join(f"({i}, {lit(v)})" for i, v in enumerate(valori))
    sql = sql.replace('"t_casi" AS "t_casi"', f"(SELECT * FROM values('i Int64, v {_TIPO_CH[src]}', {righe})) AS \"t_casi\"")
    return [v for _, v in sorted(tuple(r) for r in cli.query(sql).result_rows)]


@pytest.mark.parametrize("dove,dialetto", [("duckdb", "postgres"), ("duckdb", "clickhouse"), ("postgres", "postgres"), ("clickhouse", "clickhouse")])
@pytest.mark.parametrize("dt", ["date", "datetime"])
def test_a_native_cast_of_any_writing_gives_the_engines_value_or_null(dt, dove, dialetto):
    """Over hundreds of writings: wherever the native cast gives a value it is the DuckDB engine's,
    and every value the two engines agree on is given (on ClickHouse, in the years it holds)."""
    from app.engine.operations import op_cast

    valori = _scritture_di_date_e_istanti()[dt]
    rif = [v for _, v in _riferimento_cast("VARCHAR", dt, valori)]
    polars = op_cast(_pl.DataFrame({"v": valori}, schema={"v": _pl.String}).lazy(), {"columns": {"v": dt}}, None).collect()["v"].cast(_pl.String).to_list()
    got = _cast_nativo_di(dove, dialetto, "VARCHAR", dt, valori)
    assert len(got) == len(valori)
    for v, n, r, p in zip(valori, got, rif, polars):
        assert n is None or n == r, (dialetto, v, n, r)
        entro = dialetto != "clickhouse" or (r is not None and 1900 <= r.year <= 2299)
        if r is not None and p is not None and _norm(r) == _norm(_dt.datetime.fromisoformat(p) if dt == "datetime" else _dt.date.fromisoformat(p)) and entro:
            assert n == r, (dialetto, v, "both engines give", r, "native", n)


def test_the_native_day_pattern_is_the_calendar():
    """`_RE_DATA` with a year of one to four digits and a month or day of one or two is exactly the
    calendar (leap years included) the engines read; every other writing is refused."""
    import calendar

    regola = re.compile(d._RE_DATA)
    for anno in range(0, 10001):
        for scritto in {str(anno), str(anno).zfill(4), str(anno).zfill(2), str(anno).zfill(3)}:
            for mese, giorno in ((2, 29), (2, 28), (12, 31), (4, 31), (1, 1)):
                atteso = 1 <= anno <= 9999 and len(scritto) <= 4 and giorno <= (calendar.monthrange(anno, mese)[1] if 1 <= anno <= 9999 else 0)
                for m, g in {(str(mese), str(giorno)), (str(mese).zfill(2), str(giorno).zfill(2))}:
                    assert bool(regola.match(f"{scritto}-{m}-{g}")) == atteso, f"{scritto}-{m}-{g}"
    for mese in range(0, 14):
        for giorno in range(0, 33):
            atteso = 1 <= mese <= 12 and 1 <= giorno <= calendar.monthrange(2023, mese)[1] if 1 <= mese <= 12 else False
            for m in {str(mese), str(mese).zfill(2), str(mese).zfill(3)}:
                for g in {str(giorno), str(giorno).zfill(2), str(giorno).zfill(3)}:
                    assert bool(regola.match(f"2023-{m}-{g}")) == (atteso and len(m) <= 2 and len(g) <= 2), f"2023-{m}-{g}"


def test_the_new_cast_values_are_ones_both_engines_agree_on():
    """The values above that the native targets now convert are converted alike by the Polars and
    the DuckDB engines (the reference of these tests is DuckDB's): never one engine's whim."""
    from app.engine.operations import op_cast

    for dt, valori in (("date", _DATE_TXT_BREVI + _DATE_TXT_ANNI), ("datetime", _ISTANTI_TXT_BREVI + _ISTANTI_TXT_ANNI), ("float", _DOPPI_TXT + _DOPPI_SUBNORMALI_TXT)):
        rif = [v for _, v in _riferimento_cast("VARCHAR", dt, valori)]
        polars = op_cast(_pl.DataFrame({"v": valori}, schema={"v": _pl.String}).lazy(), {"columns": {"v": dt}}, None).collect()["v"].to_list()
        for v, a, b in zip(valori, polars, rif):
            assert a == b or (isinstance(a, float) and isinstance(b, float) and a != a and b != b), (dt, v, a, b)


# #12 #13 #16 #23 — no native target on MySQL/MariaDB (decision of 2026-10-16): the only dbt-mysql
# release needs dbt-core 1.7, which drops the project's tests; MySQL stays a source of the others
@pytest.mark.parametrize("db_type", ["mysql", "mariadb"])
def test_the_native_target_refuses_mysql_and_mariadb(db_type):
    from fastapi import HTTPException

    conn = {"host": "my.x", "port": 3306, "database": "shop", "username": "web"}
    req = _richiesta_export(mode="native", native={"db_type": db_type, "conn": conn, "pw_env": "P"},
                            source_map={"k": {"ident": "__s_0", "macro": "{{ source('s', 'ordini') }}", "columns": ["id", "x"],
                                              "types": {"id": "Int64", "x": "String"}, "label": "ordini"}})
    with pytest.raises(HTTPException) as e:
        _zip(req)
    assert e.value.status_code == 422 and e.value.detail["code"] == "no_native_adapter" and e.value.detail["params"] == {"db_type": db_type}
    with pytest.raises(d.DbtExportError) as e2:
        _progetto(attachments=None, native={"db_type": db_type, "conn": conn, "pw_env": "P", "target_schema": "s"})
    assert e2.value.code == "no_native_adapter"
    # still a source: attached by the federated target, read by a ClickHouse database engine
    assert "type: mysql" in _progetto(attachments=[{"alias": "db_2", "db_type": db_type, "pw_env": "P", "conn": conn}])["profiles.yml"]
    assert d.TargetClickHouse([{**_MY, "db_type": db_type}], "ch").database_per(2, "shop") == "tab_src_2_shop"


# #6 — a column of a type the export cannot write (an array, a uuid, bytea) is passed through as
# it is: it no longer switches off the casts of all the other columns
_CON_UNA_LISTA = {"id": "Int32", "importo": "Float64", "quando": _UTC, "tags": "List(String)", "u": "Binary"}


def test_a_column_of_an_unknown_type_does_not_drop_the_casts_of_the_others():
    import duckdb

    ops = [{"type": "cast", "params": {"columns": {"importo": "str"}}}]
    files = _esporta(_CON_UNA_LISTA, ops)
    con = duckdb.connect()
    con.execute("SET TimeZone = 'Europe/Rome'")
    con.execute("CREATE TABLE ordini AS SELECT 1 AS id, 1.50::DECIMAL(10,2) AS importo, TIMESTAMPTZ '2024-07-15 23:30:00+00' AS quando, "
                "['a', 'b'] AS tags, '5f0c6c2e-8a8f-4f39-9c7e-2f1b9d0a4e11'::UUID AS u")
    rel = con.sql(_modello(files))
    assert rel.columns == ["id", "importo", "quando", "tags", "u"]
    # a numeric of the warehouse is Tabularia's double ('1.5', not '1.50'), the timestamptz its UTC instant
    assert rel.fetchall() == [(1, "1.5", _dt.datetime(2024, 7, 15, 23, 30), ["a", "b"], __import__("uuid").UUID("5f0c6c2e-8a8f-4f39-9c7e-2f1b9d0a4e11"))]
    assert [str(t) for t in rel.types] == ["INTEGER", "VARCHAR", "TIMESTAMP", "VARCHAR[]", "UUID"]
    # native: the same, the unknown columns listed as they are
    nat = " ".join(_codice(_esporta(_CON_UNA_LISTA, [], mode="native")["models/uno.sql"]).split())
    assert 'CAST("__s_0"."importo" AS DOUBLE PRECISION) AS "importo"' in nat and '"__s_0"."tags" AS "tags"' in nat
    # and a datasource query
    raw = [{"name": "src_q", "kind": "raw", "materialized": "ephemeral", "raw_sql": "SELECT * FROM ordini",
            "columns": [{"name": c, "dtype": t} for c, t in _CON_UNA_LISTA.items()]}]
    q = _files(_richiesta_export(models=raw, source_map={}))["models/src_q.sql"]
    assert 'CAST("importo" AS DOUBLE) AS "importo"' in q and '"tags" AS "tags"' in q


# #82 — two columns whose names differ only by case: DuckDB (the federated target) resolves both
# to the first one and renames the second `codice_1`, with the first one's values
def test_columns_that_differ_only_by_case_are_refused_on_the_federated_target():
    from fastapi import HTTPException

    tipi = {"id": "Int32", "Codice": "String", "codice": "String"}
    with pytest.raises(HTTPException) as e:
        _esporta(tipi, [])
    assert e.value.detail["code"] == "columns_differ_only_by_case" and e.value.detail["params"]["columns"] == "Codice, codice"
    # a step can make the clash too: a compute, a rename, the right side of a join
    passi = [[{"type": "compute", "params": {"columns": [{"name": "Importo", "expr": "importo * 2"}]}}],
             [{"type": "rename", "params": {"mapping": {"id": "IMPORTO"}}}],
             [{"type": "join", "params": {"how": "inner", "on": "id", "right": {"source": {"key": "k"}, "operations": [
                 {"type": "rename", "params": {"mapping": {"importo": "Importo"}}}]}}}]]
    for ops in passi:
        with pytest.raises(HTTPException) as e:
            _esporta({"id": "Int32", "importo": "Float64"}, ops)
        assert e.value.detail["code"] == "columns_differ_only_by_case", ops
        assert e.value.detail["params"]["columns"] in ("IMPORTO, importo", "Importo, importo", "importo, Importo"), e.value.detail
    # Postgres and ClickHouse compare names with their case: there they stay two columns, with their own names
    for mode in ("native", "clickhouse"):
        sql = _codice(_esporta(tipi, [{"type": "select", "params": {"columns": ["codice", "Codice"]}},
                                      {"type": "filter", "params": {"column": "codice", "operator": "eq", "value": "a1"}}], mode=mode)["models/uno.sql"])
        assert '"codice"' in sql and '"Codice"' in sql and "codice_1" not in sql.lower(), mode


def test_native_postgres_keeps_columns_that_differ_only_by_case():
    files = _esporta({"id": "Int32", "Codice": "String", "codice": "String"}, [{"type": "filter", "params": {"column": "codice", "operator": "eq", "value": "d4"}}],
                     mode="native")
    valori = '(SELECT * FROM (VALUES (1, \'A1\', \'a1\'), (4, NULL, \'d4\')) AS t(id, "Codice", codice))'
    assert _su_postgres(f"SELECT * FROM ({_modello(files, valori)}) AS m") == [(4, None, "d4")]


# #28 — an unsigned 64-bit column (ClickHouse hash keys, MySQL BIGINT UNSIGNED) keeps its values:
# cast to Int64 in the first step, ClickHouse wrapped 2^64-1 to -1 without an error
_U64 = [18446744073709551615, 9223372036854775808, 9223372036854775807, 1]


def test_an_unsigned_64_bit_column_keeps_its_values_in_the_federated_model():
    import duckdb

    assert d.duck_type("UInt64") == "UBIGINT" and d.duck_type("UInt32") == "BIGINT"
    files = _esporta({"id": "UInt64", "n": "String"}, [{"type": "filter", "params": {"column": "id", "operator": "ge", "value": 2}}])
    con = duckdb.connect()
    con.execute("CREATE TABLE ordini (id UBIGINT, n VARCHAR)")
    con.executemany("INSERT INTO ordini VALUES (?, ?)", [(v, str(v)) for v in _U64])
    rel = con.sql(_modello(files))
    assert sorted(r[0] for r in rel.fetchall()) == sorted(_U64[:3]) and str(rel.types[0]) == "UBIGINT"


@_CON_CLICKHOUSE
def test_an_unsigned_64_bit_column_keeps_its_values_on_clickhouse():
    import os

    import clickhouse_connect

    ops = [{"type": "filter", "params": {"column": "id", "operator": "ge", "value": 2}},
           {"type": "cast", "params": {"columns": {"big": "int"}}}]
    sql = _modello(_esporta({"id": "UInt64", "big": "UInt64"}, ops, mode="clickhouse"),
                   "(SELECT * FROM values('id UInt64, big UInt64', " + ", ".join(f"({v}, {v})" for v in _U64) + "))")
    cli = clickhouse_connect.get_client(host=os.environ["CLICKHOUSE_TEST_HOST"], port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8123")),
                                        username=os.getenv("CLICKHOUSE_TEST_USER", "default"), password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""))
    righe = sorted(cli.query(sql).result_rows)
    # an id over 2^63 stays itself; cast to a signed integer it does not fit: NULL, as the engine (never -1)
    assert righe == [(9223372036854775807, 9223372036854775807), (9223372036854775808, None), (18446744073709551615, None)]


def test_an_unsigned_64_bit_column_on_native_postgres_is_a_numeric():
    from app.api.routes.dbt import _tipo_nel_dialetto

    # a seed's column type too
    assert _tipo_nel_dialetto("UBIGINT", "postgres") == "DECIMAL(20, 0)" and _tipo_nel_dialetto("UBIGINT", "clickhouse") == "Nullable(UInt64)"
    files = _esporta({"id": "UInt64", "big": "UInt64"}, [{"type": "cast", "params": {"columns": {"big": "int"}}}], mode="native")
    valori = "(SELECT * FROM (VALUES " + ", ".join(f"({v}::numeric, {v}::numeric)" for v in _U64) + ") AS t(id, big))"
    righe = sorted((int(a), b) for a, b in _su_postgres(f"SELECT * FROM ({_modello(files, valori)}) AS m"))   # numeric: text from the driver
    assert righe == [(1, 1), (9223372036854775807, 9223372036854775807), (9223372036854775808, None),
                                              (18446744073709551615, None)]


# #18 — a float cast to text is written as Tabularia writes it: 200.0 → '200.0', not '200'
_DOPPI = [200.0, -30.0, 0.1, 1e20, None, 2.5, 100.5, 1e15, 123456789.0, 9999999999999998.0, 1e16, float("nan"),
          float("inf"), float("-inf"), 1e-05, 0.0001, -0.0, 0.0, 1.5e-07, 123456.789, -1234567.5]


def test_a_float_cast_to_text_on_native_postgres_is_written_as_the_engine_writes_it():
    import duckdb

    rif = _riferimento_cast("DOUBLE", "str", _DOPPI)
    assert rif[0][1] == "200.0" and rif[7][1] == "1000000000000000.0"
    con = duckdb.connect()
    con.execute("CREATE TABLE t_casi (i BIGINT, v DOUBLE)")
    con.executemany("INSERT INTO t_casi VALUES (?, ?)", list(enumerate(_DOPPI)))
    _uguali_ai_valori(con.execute(f"SELECT * FROM ({_cast_nativo('DOUBLE', 'str', 'postgres')}) ORDER BY i").fetchall(), rif, _DOPPI, "duckdb")
    pg = d.transpile_model(_cast_nativo("DOUBLE", "str", "postgres"), "postgres", {"t_casi": ["i", "v"]})
    righe = ", ".join(f"({i}, {_letterale_pg(v, 'DOUBLE')})" for i, v in enumerate(_DOPPI))
    pg = pg.replace('"t_casi" AS "t_casi"', f'(VALUES {righe}) AS "t_casi"("i", "v")')
    _uguali_ai_valori(_su_postgres(pg), rif, _DOPPI, "postgres")


@_CON_CLICKHOUSE
def test_a_float_cast_to_text_on_clickhouse_is_written_as_the_clickhouse_engine_writes_it():
    import os

    import clickhouse_connect

    from app.engine.chdb_ops import _cast_expr as cast_ch

    info: dict = {}
    ch = d.rimetti_testi(d.transpile_model(_cast_nativo("DOUBLE", "str", "clickhouse", info), "clickhouse", {"t_casi": ["i", "v"]}), info)
    lit = lambda v: "NULL" if v is None else ("nan" if v != v else ("inf" if v == float("inf") else ("-inf" if v == float("-inf") else repr(v))))
    valori = "(SELECT * FROM values('i Int64, v Nullable(Float64)', " + ", ".join(f"({i}, {lit(v)})" for i, v in enumerate(_DOPPI)) + "))"
    cli = clickhouse_connect.get_client(host=os.environ["CLICKHOUSE_TEST_HOST"], port=int(os.getenv("CLICKHOUSE_TEST_PORT", "8123")),
                                        username=os.getenv("CLICKHOUSE_TEST_USER", "default"), password=os.getenv("CLICKHOUSE_TEST_PASSWORD", ""))
    got = sorted(tuple(r) for r in cli.query(ch.replace('"t_casi" AS "t_casi"', f'{valori} AS "t_casi"')).result_rows)
    rif = sorted(tuple(r) for r in cli.query(f"SELECT i, {cast_ch('v', 'Float64', 'str')} FROM {valori}").result_rows)
    assert dict(rif)[0] == "200.0"
    _uguali_ai_valori(got, rif, _DOPPI, "clickhouse")


# #14 — Postgres has no cast between boolean and bigint/double precision, nor min/max of a boolean:
# the model failed at `dbt run` where Tabularia gives 1/0, true/false
_CASI_BOOL = [("BOOLEAN", "int", [True, False, None]), ("BOOLEAN", "float", [True, False, None]),
              ("BIGINT", "bool", [5, 0, -3, None]), ("DOUBLE", "bool", [2.5, 0.0, -0.0, float("nan"), float("inf"), None])]


@pytest.mark.parametrize("src,dt,valori", _CASI_BOOL, ids=lambda x: x if isinstance(x, str) else None)
def test_a_cast_between_boolean_and_number_runs_on_native_postgres(src, dt, valori):
    _cast_in_duckdb(src, dt, valori)
    _cast_su_postgres(src, dt, valori)


@pytest.mark.parametrize("src,dt,valori", _CASI_BOOL, ids=lambda x: x if isinstance(x, str) else None)
def test_a_cast_between_boolean_and_number_runs_on_clickhouse(src, dt, valori):
    _cast_su_clickhouse(src, dt, valori)


def _nativo_su(dove: str, df, ops: list) -> list:
    """The native model of `ops` over `df` (as the source `t`), run on a real Postgres or ClickHouse:
    its rows, normalised and sorted as `_engine_snapshot` gives them."""
    info: dict = {}
    raw, _, _ = _export_sql({"t": df}, "t", ops, native=True, dialetto=dove, info=info)
    sql = d.rimetti_testi(d.transpile_model(raw, dove, {"t_t": list(df.columns)}), info)
    pg = {"BIGINT": "BIGINT", "DOUBLE": "DOUBLE PRECISION", "VARCHAR": "TEXT", "BOOLEAN": "BOOLEAN", "DATE": "DATE", "TIMESTAMP": "TIMESTAMP"}
    ch = {"BIGINT": "Int64", "DOUBLE": "Float64", "VARCHAR": "String", "BOOLEAN": "Bool", "DATE": "Date32", "TIMESTAMP": "DateTime64(6)"}
    tipi = {c: d.duck_type(str(t)) for c, t in df.schema.items()}
    righe = df.rows()
    if dove == "postgres":
        lit = lambda v, t: f"CAST(NULL AS {pg.get(t, t)})" if v is None else f"CAST('{v}' AS {pg.get(t, t)})"   # noqa: E731
        valori = ", ".join("(" + ", ".join(lit(v, tipi[c]) for v, c in zip(r, df.columns)) + ")" for r in righe)
        nomi = ", ".join(f'"{c}"' for c in df.columns)
        out = _su_postgres(sql.replace('"t_t" AS "t_t"', f'(VALUES {valori}) AS "t_t"({nomi})'))
    else:
        lit = lambda v: "NULL" if v is None else (str(v).lower() if isinstance(v, bool) else f"'{v}'")   # noqa: E731
        schema = ", ".join(f"{c} Nullable({ch.get(tipi[c], tipi[c])})" for c in df.columns)
        valori = ", ".join("(" + ", ".join(lit(v) for v in r) + ")" for r in righe)
        out = [tuple(r) for r in _clickhouse().query(sql.replace('"t_t" AS "t_t"', f"(SELECT * FROM values('{schema}', {valori})) AS \"t_t\"")).result_rows]
    return sorted((tuple(_norm(v) for v in r) for r in out), key=lambda t: tuple(map(str, t)))


# DuckDB's date-part functions (year(d), month(d)…) do not exist in Postgres, and sqlglot kept them:
# the native model failed at `dbt run`. They are EXTRACT there, an integer as in DuckDB
_PARTI = ["year", "month", "day", "dayofmonth", "dayofweek", "isodow", "dayofyear", "quarter", "week", "weekofyear", "hour", "minute", "second"]


def test_date_parts_of_duckdb_text_run_on_native_postgres():
    df = _pl.DataFrame({"id": [1, 2, 3], "t": [_dt.datetime(2024, 1, 7, 10, 30, 59, 900000), _dt.datetime(1960, 12, 31, 23, 5, 1),
                                                 _dt.datetime(2021, 1, 1, 0, 0, 0)]})
    ops = [{"type": "compute", "params": {"columns": [{"name": p, "expr": f"{p}(t)"} for p in _PARTI]}}]
    rif = _engine_snapshot("duckdb", {"t": df}, "t", ops)
    assert rif[1][1][2:] == (1960.0, 12.0, 31.0, 31.0, 6.0, 6.0, 366.0, 4.0, 52.0, 52.0, 23.0, 5.0, 1.0)
    assert _nativo_su("postgres", df, ops) == rif[1]


def test_the_readme_of_a_native_postgres_project_says_its_sql_was_not_run_on_postgres():
    """The ClickHouse SQL is parsed by a ClickHouse at export time; the Postgres SQL is not checked
    by any Postgres: the team runs it first, and the README says so."""
    nativo = {"db_type": "postgresql", "conn": {"host": "h", "port": 5432, "database": "d", "username": "u"}, "pw_env": "P",
              "target_schema": "s"}
    leggimi = " ".join(_progetto(attachments=None, native=nativo)["README.md"].split())
    assert "translated by sqlglot and not run on Postgres" in leggimi
    assert "not run on Postgres" not in " ".join(_progetto(attachments=None, native={**_NATIVO_CH, "target_schema": "s"})["README.md"].split())


# #4 (native) — Postgres' avg/stddev/variance of an integer or a decimal is a numeric: cast to text it
# was '1.5000000000000000' where Tabularia writes '1.5'; of a boolean they did not exist
_INTERI_GRUPPI = _pl.DataFrame({"g": [1, 1, 2, 2, 2, 2, 2, 3], "x": [1, 2, 1, 2, 3, 4, 5, None],
                                "b": [True, False, True, True, True, True, None, None]}).with_columns(_pl.col("x").cast(_pl.Decimal(10, 2)).alias("dx"))


@pytest.mark.parametrize("dove", ["postgres", "clickhouse"])
@pytest.mark.parametrize("col", ["x", "dx", "b"])
def test_mean_std_and_var_on_native_targets_are_doubles(dove, col):
    gb = [{"type": "group_by", "params": {"by": ["g"], "aggregations": [{"column": col, "func": f, "alias": f} for f in ("mean", "std", "var")]}},
          {"type": "cast", "params": {"columns": {"mean": "str", "std": "str", "var": "str"}}}]
    rif = _engine_snapshot("polars", {"t": _INTERI_GRUPPI}, "t", gb)
    if col != "b":   # the DuckDB engine has no mean of a boolean (Polars has: the share of true)
        assert _engine_snapshot("duckdb", {"t": _INTERI_GRUPPI}, "t", gb)[1] == rif[1]
    assert rif[1][:2] == ([(1.0, "0.5", "0.7071067811865476", "0.5"), (2.0, "1.0", "0.0", "0.0")] if col == "b" else
                          [(1.0, "1.5", "0.7071067811865476", "0.5"), (2.0, "3.0", "1.5811388300841898", "2.5")]), rif
    _, _, tipi = d.compile_model_sql(*_esporta_tipi(gb[:1], col))
    assert all(tipi[f] == "DOUBLE" for f in ("mean", "std", "var"))
    assert _nativo_su(dove, _INTERI_GRUPPI, gb) == rif[1]
    # the federated target too
    assert _export_snapshot({"t": _INTERI_GRUPPI}, "t", gb)[1] == rif[1]


def _esporta_tipi(ops: list, col: str):
    def resolve(_):
        return "t_t", list(_INTERI_GRUPPI.columns), {c: d.duck_type(str(t)) for c, t in _INTERI_GRUPPI.schema.items()}, "t"
    return ({"key": "t"}, ops, resolve)


# #4 (native) — a mean or a median of dates or instants: avg(date) and median(date) do not exist on
# Postgres and ClickHouse, the model failed; Tabularia gives an instant
_DATE_GRUPPI = _pl.DataFrame({
    "g": [1, 1, 1, 2, 2, 3, 3, 3, 3, 4],
    "d": [_dt.date(2024, 1, 1), _dt.date(2024, 1, 2), _dt.date(2024, 1, 4), _dt.date(1960, 5, 5), _dt.date(1960, 5, 6),
          _dt.date(2024, 1, 1), _dt.date(2024, 1, 5), _dt.date(2024, 1, 6), _dt.date(2299, 12, 31), None],
    "t": [_dt.datetime(2024, 1, 1, 10), _dt.datetime(2024, 1, 1, 11), _dt.datetime(2024, 1, 1, 15), _dt.datetime(1960, 5, 5, 0, 0, 1),
          _dt.datetime(1960, 5, 5, 0, 0, 3), _dt.datetime(2024, 1, 1), _dt.datetime(2024, 1, 2), _dt.datetime(2024, 1, 2, 0, 0, 0, 500000),
          _dt.datetime(2024, 1, 3), None]})


@pytest.mark.parametrize("dove", ["postgres", "clickhouse"])
@pytest.mark.parametrize("func", ["mean", "median"])
def test_mean_and_median_of_dates_and_instants_run_on_native_targets(dove, func):
    gb = [{"type": "group_by", "params": {"by": ["g"], "aggregations": [{"column": "d", "func": func, "alias": "md"},
                                                                         {"column": "t", "func": func, "alias": "mt"}]}}]
    rif = _engine_snapshot("polars", {"t": _DATE_GRUPPI}, "t", gb)
    assert _engine_snapshot("duckdb", {"t": _DATE_GRUPPI}, "t", gb)[1] == rif[1]
    assert rif[2][1:] == ["datetime", "datetime"] and rif[1][-1] == (4.0, None, None)
    assert _nativo_su(dove, _DATE_GRUPPI, gb) == rif[1]


def test_min_and_max_of_a_boolean_run_on_native_postgres():
    gb = [{"type": "group_by", "params": {"by": ["g"], "aggregations": [{"column": "flag", "func": "min", "alias": "lo"},
                                                                         {"column": "flag", "func": "max", "alias": "hi"}]}}]
    rif = _engine_snapshot("duckdb", {"t": _pl.DataFrame({"g": ["a", "a", "b", "c"], "flag": [True, False, True, None]})}, "t", gb)
    files = _esporta({"g": "String", "flag": "Boolean"}, gb, mode="native")
    valori = "(SELECT * FROM (VALUES ('a', TRUE), ('a', FALSE), ('b', TRUE), ('c', CAST(NULL AS BOOLEAN))) AS t(g, flag))"
    righe = sorted(_su_postgres(f"SELECT * FROM ({_modello(files, valori)}) AS m"), key=lambda r: r[0])
    assert [tuple(_norm(v) for v in r) for r in righe] == rif[1] == [("a", False, True), ("b", True, True), ("c", None, None)]


# #15 — native Postgres compares and orders text by BYTES, like Tabularia (decision of 2026-10-16),
# whatever the collation of the database or of the column: on en_US 'a' < 'B', for Tabularia 'B' < 'a'
_TESTI = _pl.DataFrame({"id": [1, 2, 3, 4, 5, 6, 7, 8], "g": ["x", "x", "y", "y", "y", "z", "z", "z"],
                        "nome": ["a", "B", "Città", "citta", "Zeta", "alpha", "Bravo", None]})
_ORDINE_TESTO = {
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


@pytest.mark.parametrize("caso", list(_ORDINE_TESTO))
@pytest.mark.parametrize("collazione", [None, "en-x-icu"])
def test_native_postgres_orders_and_compares_text_by_bytes_like_tabularia(caso, collazione):
    ops = _ORDINE_TESTO[caso]
    rif = _engine_snapshot("polars", {"t": _TESTI}, "t", ops)
    files = _esporta({c: str(t) for c, t in _TESTI.schema.items()}, ops, mode="native")
    nome = "nome" + (f' COLLATE "{collazione}"' if collazione else "")
    righe = ", ".join(f"({i}, '{g}', {'NULL' if n is None else repr(n)})" for i, g, n in _TESTI.iter_rows())
    valori = f"(SELECT id, g, {nome} AS nome FROM (VALUES {righe}) AS t(id, g, nome))"
    got = sorted((tuple(_norm(v) for v in r) for r in _su_postgres(f"SELECT * FROM ({_modello(files, valori)}) AS m")),
                 key=lambda t: tuple(map(str, t)))
    assert got == rif[1], (caso, collazione)


# #22 — Postgres keeps only the first 63 bytes of a name: two long column names that share them are
# one column there (the model fails), and one long name is shorter in the warehouse than in Tabularia
_LUNGO = "net_revenue_after_discount_per_order_line_for_the_board_report_"      # 63 bytes


def test_on_native_postgres_two_column_names_equal_in_their_first_63_bytes_are_refused():
    from fastapi import HTTPException

    assert len(_LUNGO.encode()) == 63
    ops = [{"type": "compute", "params": {"columns": [{"name": _LUNGO + "2024", "expr": "id * 1"}, {"name": _LUNGO + "2025", "expr": "id * 2"}]}}]
    with pytest.raises(HTTPException) as e:
        _esporta({"id": "Int32"}, ops, mode="native")
    assert e.value.detail["code"] == "column_names_too_long"
    assert e.value.detail["params"] == {"columns": f"{_LUNGO}2024, {_LUNGO}2025", "limit": 63}
    # a name cut inside a two-byte character: Postgres keeps the whole characters only
    accentato = "à" * 31 + "x"                                                         # 63 bytes
    ops2 = [{"type": "compute", "params": {"columns": [{"name": accentato + "àa", "expr": "id * 1"}, {"name": accentato + "àb", "expr": "id"}]}}]
    with pytest.raises(HTTPException):
        _esporta({"id": "Int32"}, ops2, mode="native")
    # the federated and the ClickHouse targets have no such limit
    for mode in ("duckdb", "clickhouse"):
        _esporta({"id": "Int32"}, ops, mode=mode)


def test_on_native_postgres_one_long_column_name_runs_and_the_readme_says_what_it_becomes():
    ops = [{"type": "compute", "params": {"columns": [{"name": _LUNGO + "2024", "expr": "id * 2"}]}}]
    files = _esporta({"id": "Int32"}, ops, mode="native")
    assert f"`{_LUNGO}2024`" in files["README.md"] and f"`{_LUNGO}`" in files["README.md"]
    assert "63" in files["README.md"]
    righe = _su_postgres(f"SELECT * FROM ({_modello(files, '(SELECT * FROM (VALUES (3)) AS t(id))')}) AS m")
    assert righe == [(3, 6)]
    assert "63" not in _esporta({"id": "Int32"}, ops)["README.md"]


# ── E4: the ClickHouse target and the native profiles ─────────────────────────
_DT = "Datetime(time_unit='us', time_zone=None)"
_TEMPI = {"id": "Int32", "nato": "Date", "visto": _DT, "ora": "Time"}


def _su_clickhouse(sql: str) -> list:
    return [tuple(r) for r in _clickhouse().query(sql).result_rows]


# #24 — dates and times on ClickHouse: Date (1970-2149) and DateTime (whole seconds, 1970-2106) wrapped
# a birth date of 1955, cut every timestamp's microseconds; TIME is an Int64 there, so a time was NULL
def test_dates_and_times_on_clickhouse_have_the_types_of_the_engine():
    from app.api.routes.dbt import _tipo_nel_dialetto

    sql = " ".join(_codice(_esporta(_TEMPI, [], mode="clickhouse")["models/uno.sql"]).split())
    assert '"nato" AS Nullable(Date32))' in sql and '"visto" AS Nullable(DateTime64(6)))' in sql and '"ora" AS Nullable(String))' in sql
    assert "Nullable(DATE)" not in sql and "Nullable(DateTime)" not in sql and "TIME)" not in sql
    # a seed's column types, and a datasource query's projection
    assert [_tipo_nel_dialetto(t, "clickhouse") for t in ("DATE", "TIMESTAMP", "TIME")] == ["Nullable(Date32)", "Nullable(DateTime64(6))", "Nullable(String)"]
    raw = [{"name": "src_q", "kind": "raw", "materialized": "ephemeral", "raw_sql": "SELECT * FROM ordini",
            "columns": [{"name": c, "dtype": t} for c, t in _TEMPI.items()]}]
    q = _files(_richiesta_export(models=raw, source_map={}, mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"}))["models/src_q.sql"]
    assert "Nullable(Date32)" in q and "Nullable(DateTime64(6))" in q and "TIME" not in q
    # the native ClickHouse target is translated the same way
    native = {"db_type": "clickhouse", "conn": {"host": "h", "port": 8123, "database": "d", "username": "u"}, "pw_env": "P"}
    nat = _files(_richiesta_export(mode="native", native=native, models=[{"name": "uno", "source": {"key": "k"}, "operations": []}],
                                   source_map={"k": {"ident": "__s_0", "macro": "{{ source('s', 'ordini') }}", "columns": list(_TEMPI),
                                                     "types": _TEMPI, "label": "ordini"}}))["models/uno.sql"]
    assert "Nullable(Date32)" in nat and "Nullable(DateTime64(6))" in nat


def test_dates_and_times_on_clickhouse_keep_the_values_tabularia_publishes():
    valori = ("(SELECT * FROM values('id Int32, nato Nullable(Date32), visto Nullable(DateTime64(6)), ora Nullable(String)', "
              "(1, '1955-03-12', '2024-01-15 10:30:00.123456', '13:45:00'), (2, '2200-01-01', '1965-06-01 08:00:00', '08:15:30.5'), "
              "(3, NULL, '2200-01-01 00:00:00', NULL)))")
    ops = [{"type": "compute", "params": {"columns": [{"name": "anno", "expr": "year(nato)"}]}}]
    righe = sorted(_su_clickhouse(_modello(_esporta(_TEMPI, ops, mode="clickhouse"), valori)))
    assert righe == [(1, _dt.date(1955, 3, 12), _dt.datetime(2024, 1, 15, 10, 30, 0, 123456), "13:45:00", 1955),
                     (2, _dt.date(2200, 1, 1), _dt.datetime(1965, 6, 1, 8, 0), "08:15:30.5", 2200),
                     (3, None, _dt.datetime(2200, 1, 1), None, None)]
    # a filter on an instant with a fraction of a second: it used to fail the model (TYPE_MISMATCH)
    ops = [{"type": "filter", "params": {"column": "visto", "operator": "eq", "value": "2024-01-15 10:30:00.123456"}}]
    assert [r[0] for r in _su_clickhouse(_modello(_esporta(_TEMPI, ops, mode="clickhouse"), valori))] == [1]
    # the order of instants before 1970 and after 2106: the first is 1965's
    ops = [{"type": "sort", "params": {"by": ["visto"]}}, {"type": "limit", "params": {"n": 1}}]
    assert [r[0] for r in _su_clickhouse(_modello(_esporta(_TEMPI, ops, mode="clickhouse"), valori))] == [2]


# #25 — the sources on the ClickHouse target: a PostgreSQL/MySQL DATABASE engine types the columns by
# itself, a date as Date (1955 → 2134 before any cast), an unconstrained numeric as Decimal(38, 19)
# (a bigger value failed every model), a MySQL datetime as DateTime (1965 → 1970, no microseconds)
def test_the_clickhouse_sources_are_tables_with_types_that_keep_every_value():
    ch = d.TargetClickHouse([_PG, _MY], "ch.local")
    ch.sources([{"name": "db_1_vendite", "schema": "vendite", "connection_id": 1, "tables": [{"name": "ordini"}, {"name": "clienti"}]},
                {"name": "db_2_shop", "schema": "shop", "connection_id": 2, "tables": [{"name": "righe"}]}])
    ch.traduci_query("SELECT c.id FROM crm.contatti AS c JOIN vendite.ordini AS o ON o.cliente = c.id", 1, "q")
    macro = d._macro_sorgenti_clickhouse(list(ch.federati.values()))
    assert not re.search(r"CREATE DATABASE[^\n]*ENGINE", macro)            # no database engine any more
    for db, tabelle, motore, argomenti in (
            ("tab_src_1_vendite", ["clienti", "ordini"], "PostgreSQL", "'db.x.it:5432', 'erp', '{t}', 'shop', '{{{{ pw_tab_src_1_vendite }}}}', 'vendite'"),
            ("tab_src_1_crm", ["contatti"], "PostgreSQL", "'db.x.it:5432', 'erp', '{t}', 'shop', '{{{{ pw_tab_src_1_crm }}}}', 'crm'"),
            ("tab_src_2_shop", ["righe"], "MySQL", "'my.x.it:3306', 'shop', '{t}', 'web', '{{{{ pw_tab_src_2_shop }}}}'")):
        assert f"CREATE DATABASE IF NOT EXISTS `{db}`" in macro
        for t in tabelle:
            # one table per source table (and per table a query reads), typed from what the source has
            assert f"CREATE OR REPLACE TABLE `{db}`.`{t}` AS {motore.lower()}({argomenti.format(t=t)})" in macro
            assert f"CREATE OR REPLACE TABLE `{db}`.`{t}` ({{{{ colonne | join(', ') }}}}) ENGINE = {motore}({argomenti.format(t=t)})" in macro
            assert d._struttura_sql(db, t, motore) in macro
        # a database an earlier export made with the PostgreSQL/MySQL engine is replaced (it holds no data)
        assert f"name = '{db}' AND engine IN ('PostgreSQL', 'MySQL')" in macro
    assert macro.count("{% call statement(") == macro.count("{%- endcall %}")


@_CON_CLICKHOUSE
def test_the_types_of_a_clickhouse_source_table_keep_dates_before_1970_and_big_numbers():
    import uuid

    cli = _clickhouse()
    tabella = f"zz_e4_{uuid.uuid4().hex[:8]}"
    nomi = ["id", "nato", "nati", "date_list", "visto", "visto6", "n", "n2", "we`ird\\name", "pair"]
    tipi = ["Int32", "Nullable(Date)", "Date", "Array(Nullable(Date))", "Nullable(DateTime)", "Nullable(DateTime64(6))",
            "Nullable(Decimal(38, 19))", "Nullable(Decimal(12, 2))", "Nullable(String)", "Tuple(a Date, b DateTime)"]
    colonne = ", ".join(f"`{n.replace(chr(92), chr(92) * 2).replace('`', chr(92) + '`')}` {t}" for n, t in zip(nomi, tipi))
    cli.command(f"CREATE TABLE tab.{tabella} ({colonne}) ENGINE = Memory")
    try:
        pg = [r[0] for r in cli.query(d._struttura_sql("tab", tabella, "PostgreSQL")).result_rows]
        my = [r[0] for r in cli.query(d._struttura_sql("tab", tabella, "MySQL")).result_rows]
    finally:
        cli.command(f"DROP TABLE tab.{tabella}")
    attesi = ["Int32", "Nullable(Date32)", "Date32", "Array(Nullable(Date32))", "Nullable(DateTime64(6))", "Nullable(DateTime64(6))",
              "Nullable(Float64)", "Nullable(Decimal(12, 2))", "Nullable(String)", "Tuple(a Date32, b DateTime64(6))"]
    quotati = ["`id`", "`nato`", "`nati`", "`date_list`", "`visto`", "`visto6`", "`n`", "`n2`", "`we\\`ird\\\\name`", "`pair`"]
    assert pg == [f"{q} {t}" for q, t in zip(quotati, attesi)]
    # MySQL: a decimal is kept exact (Tabularia reads it as a decimal); Postgres numeric is Tabularia's double
    assert my == [f"{q} {t}" for q, t in zip(quotati, attesi[:6] + ["Nullable(Decimal(38, 19))"] + attesi[7:])]


# #26 — on ClickHouse upper/lower/substring/reverse and left/right work on bytes: 'Città' became
# 'CITTà', a substring cut a character in half, and starts_with/ends_with an accented value kept no row
_ACCENTI = _pl.DataFrame({"id": [1, 2, 3, 4], "s": ["Città", "Università", "ÀÈ", "Roma"]})
_TESTO_UTF8 = [{"type": "compute", "params": {"columns": [
    {"name": "su", "expr": "upper(s)"}, {"name": "giu", "expr": "lower(s)"}, {"name": "pezzo", "expr": "substring(s, 4, 2)"},
    {"name": "rovescio", "expr": "reverse(s)"}, {"name": "lungo", "expr": "length(s)"}, {"name": "testa", "expr": "left(s, 4)"}]}}]


@pytest.mark.parametrize("ops", [
    _TESTO_UTF8,
    [{"type": "filter", "params": {"column": "s", "operator": "starts_with", "value": "Città"}}],
    [{"type": "filter", "params": {"column": "s", "operator": "ends_with", "value": "tà"}}],
    [{"type": "filter", "params": {"column": "s", "operator": "contains", "value": "à"}}],
], ids=["compute", "starts_with", "ends_with", "contains"])
def test_accented_text_on_clickhouse_gives_what_the_engines_give(ops):
    rif = _engine_snapshot("polars", {"k": _ACCENTI}, "k", ops)
    assert rif == _engine_snapshot("duckdb", {"k": _ACCENTI}, "k", ops)
    valori = "(SELECT * FROM values('id Int64, s Nullable(String)', " + ", ".join(f"({i}, '{s}')" for i, s in _ACCENTI.iter_rows()) + "))"
    righe = _su_clickhouse(_modello(_esporta({"id": "Int64", "s": "String"}, ops, mode="clickhouse"), valori))
    assert sorted((tuple(_norm(v) for v in r) for r in righe), key=lambda t: tuple(map(str, t))) == rif[1]


def test_the_position_of_a_text_on_clickhouse_counts_characters_as_duckdb_does():
    """#26: DuckDB's strpos/instr/position count characters, ClickHouse's POSITION bytes: in 'ÀÈ' the
    È was 3. (The Polars engine counts bytes too: its text is flagged for review.)"""
    ops = [{"type": "compute", "params": {"columns": [{"name": "dove", "expr": "strpos(s, 'È')"}, {"name": "dove2", "expr": "position('È' IN s)"},
                                                      {"name": "dove3", "expr": "instr(s, 'tà')"}]}}]
    rif = _engine_snapshot("duckdb", {"k": _ACCENTI}, "k", ops)
    assert [r[2:] for r in rif[1]] == [(0.0, 0.0, 4.0), (0.0, 0.0, 9.0), (2.0, 2.0, 0.0), (0.0, 0.0, 0.0)]
    valori = "(SELECT * FROM values('id Int64, s Nullable(String)', " + ", ".join(f"({i}, '{s}')" for i, s in _ACCENTI.iter_rows()) + "))"
    righe = _su_clickhouse(_modello(_esporta({"id": "Int64", "s": "String"}, ops, mode="clickhouse"), valori))
    assert sorted((tuple(_norm(v) for v in r) for r in righe), key=lambda t: tuple(map(str, t))) == rif[1]


# #62 — dbt inlines an ephemeral model (a shared step, a datasource query) as a CTE of the model that
# reads it: a step name both use (drop_columns, fill_nulls, filter_x…) is UNKNOWN_TABLE on ClickHouse 24
_CONDIVISO = [
    {"name": "int_d1", "kind": "intermediate", "materialized": "ephemeral", "source": {"key": "k"},
     "operations": [{"type": "drop", "params": {"columns": ["numero"]}}, {"type": "filter", "params": {"column": "id", "operator": "le", "value": 3}}]},
    {"name": "uno", "source": {"model": "int_d1"},
     "operations": [{"type": "drop", "params": {"columns": ["deposito"]}}, {"type": "filter", "params": {"column": "id", "operator": "ge", "value": 2}}]},
]


def _cte(sql: str) -> list:
    return re.findall(r"^\s*(?:WITH\s+)?[\"`]?(\w+)[\"`]? AS \(", sql, flags=re.M)


def _con_l_effimero(files: dict, modello: str, effimero: str, sorgente: str) -> str:
    """The model as dbt-clickhouse runs it: the ephemeral model's SQL inlined as the CTE
    `__dbt__cte__<name>` at the head of the model's own WITH."""
    rel = {("source", "db_1_v", "ordini"): sorgente, ("ref", effimero): f"__dbt__cte__{effimero}"}
    interno = _render_like_dbt(files[f"models/{effimero}.sql"], rel, config={})
    esterno = _render_like_dbt(files[f"models/{modello}.sql"], rel, config={}).lstrip()
    assert esterno.startswith("WITH")
    return f"WITH __dbt__cte__{effimero} AS (\n{interno}\n),\n" + esterno[len("WITH"):].lstrip()


def test_a_model_and_the_ephemeral_model_it_reads_never_share_a_cte_name():
    for mode in ("duckdb", "native", "clickhouse"):
        extra = {"native": _PG_NATIVE} if mode == "native" else ({"clickhouse": {"connections": [_PG], "target_schema": "s"}} if mode == "clickhouse" else {})
        voce = {"ref": "{{ source('db_1_v', 'ordini') }}"} if mode == "duckdb" else {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}"}
        files = _files(_richiesta_export(mode=mode, **extra, models=_CONDIVISO, source_map={"k": {
            **voce, "columns": ["id", "numero", "deposito"], "types": {"id": "Int64", "numero": "Int64", "deposito": "String"}, "label": "ordini"}}))
        interni, esterni = _cte(files["models/int_d1.sql"]), _cte(files["models/uno.sql"])
        assert interni and esterni and not set(interni) & set(esterni), (mode, interni, esterni)


@_CON_CLICKHOUSE
def test_a_model_that_reads_an_ephemeral_model_runs_on_clickhouse_24():
    files = _files(_richiesta_export(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"}, models=_CONDIVISO,
                                     source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "numero", "deposito"],
                                                       "types": {"id": "Int64", "numero": "Int64", "deposito": "String"}, "label": "ordini"}}))
    valori = "(SELECT * FROM values('id Int64, numero Int64, deposito String', (1, 10, 'a'), (2, 20, 'b'), (3, 30, 'c'), (4, 40, 'd')))"
    assert sorted(_su_clickhouse(_con_l_effimero(files, "uno", "int_d1", valori))) == [(2,), (3,)]


# #27 — Postgres folds the names a query writes without quotes to lowercase; ClickHouse does not: the
# query `SELECT Id, Nome FROM Catalogo.Prodotti` passed the export and failed at dbt run (UNKNOWN_TABLE)
def test_a_postgres_query_reads_on_clickhouse_the_names_postgres_reads():
    ch = d.TargetClickHouse([_PG, _MY], "ch.local")
    sql = " ".join(ch.traduci_query('WITH Attivi AS (SELECT Id, Nome, "Città" FROM Catalogo.Prodotti AS P WHERE P.Attivo) '
                                    "SELECT A.Id, A.Nome, A.\"Città\", O.Totale AS Totale FROM Attivi AS A JOIN Ordini AS O ON O.Prodotto = A.Id",
                                    1, "q").split())
    assert "tab_src_1_catalogo.prodotti" in sql and "tab_src_1_public.ordini" in sql
    assert "SELECT id, nome, \"Città\" FROM" in sql and "o.totale AS totale" in sql and "FROM attivi AS a" in sql
    assert not re.search(r"\b(Id|Nome|Prodotti|Catalogo|Attivi|Totale)\b", sql)
    # the database is made for the schema Postgres has, `catalogo`: 'Catalogo' would be an empty one
    assert ch.federati["tab_src_1_catalogo"]["schema"] == "catalogo" and ch.federati["tab_src_1_catalogo"]["tables"] == {"prodotti"}
    # MySQL keeps table names as written (they are case sensitive on its usual servers)
    assert "tab_src_2_shop.Clienti" in ch.traduci_query("SELECT id FROM shop.Clienti", 2, "m")


# #63 — without join_use_nulls ClickHouse fills the missing side of an outer join with 0 / '': the
# native ClickHouse profile did not set it (the ClickHouse target's did), a customer without orders had 0
_NATIVO_CH = {"db_type": "clickhouse", "conn": {"host": "ch.x.it", "port": 8123, "database": "crm", "username": "u"}, "pw_env": "P"}


def _impostazioni_del_profilo(profilo: str) -> dict:
    """The custom_settings of a dbt-clickhouse profile, as dbt-clickhouse sends them with every query."""
    blocco = profilo.split("custom_settings:\n", 1)[1] if "custom_settings:\n" in profilo else ""
    return dict(re.findall(r"^ {8}(\w+): (\S+)$", blocco, flags=re.M))


def test_the_native_clickhouse_profile_gives_null_on_the_missing_side_of_an_outer_join():
    clienti = _pl.DataFrame({"id": [1, 2], "nome": ["con ordini", "senza ordini"]})
    ordini = _pl.DataFrame({"oid": [10, 11], "cliente_id": [1, 1]})
    ops = [{"type": "join", "params": {"how": "left", "left_on": ["id"], "right_on": ["cliente_id"], "right": {"source": _key("ordini"), "operations": [
        {"type": "group_by", "params": {"by": ["cliente_id"], "aggregations": [{"column": "oid", "func": "count", "alias": "n_ordini"}]}}]}}}]
    rif = _engine_snapshot("polars", {"clienti": clienti, "ordini": ordini}, "clienti", ops)
    assert rif[1] == [(1.0, "con ordini", 1.0, 2.0), (2.0, "senza ordini", None, None)]
    files = _files(_richiesta_export(mode="native", native=_NATIVO_CH, models=[{"name": "uno", "source": _key("clienti"), "operations": ops}],
                                     source_map={_key(k)["key"]: {"ident": f"__s_{i}", "macro": "{{ source('crm', '%s') }}" % k, "columns": list(df.columns),
                                                     "types": {c: str(t) for c, t in df.schema.items()}, "label": k}
                                                 for i, (k, df) in enumerate((("clienti", clienti), ("ordini", ordini)))}))
    impostazioni = _impostazioni_del_profilo(files["profiles.yml"])
    assert impostazioni.get("join_use_nulls") == "1"
    sql = _render_like_dbt(files["models/uno.sql"], {("source", "crm", "clienti"): "(SELECT * FROM values('id Int64, nome String', (1, 'con ordini'), (2, 'senza ordini')))",
                                                     ("source", "crm", "ordini"): "(SELECT * FROM values('oid Int64, cliente_id Int64', (10, 1), (11, 1)))"}, config={})
    righe = [tuple(r) for r in _clickhouse().query(sql, settings=impostazioni).result_rows]
    assert sorted((tuple(_norm(v) for v in r) for r in righe), key=lambda t: tuple(map(str, t))) == rif[1]
    # the folder package says it to the team, as it does for the ClickHouse target
    cartella = _progetto(attachments=None, layout={"package": "folder", "folder": "f"}, native={**_NATIVO_CH, "target_schema": "s"})
    assert "join_use_nulls: 1" in cartella["INTEGRATION.md"]


# #21 — Tabularia reaches a ClickHouse connection on 8443 or 443 over TLS (clickhouse-connect does
# so from the port); the native profile wrote `secure: false`, and dbt spoke plain HTTP to a TLS port
@pytest.mark.parametrize("porta,tls", [(8443, True), (443, True), (8123, False), (9000, False)])
def test_the_native_clickhouse_profile_uses_tls_where_tabularia_does(porta, tls):
    from clickhouse_connect.driver import _parse_connection_params   # how Tabularia's client picks the scheme

    schema = _parse_connection_params(host="h", username="", password="", port=porta, database="", interface=None, secure=False,
                                      dsn=None, kwargs={})[-1]
    assert (schema == "https") is tls
    nativo = {**_NATIVO_CH, "conn": {**_NATIVO_CH["conn"], "port": porta}, "target_schema": "s"}
    profilo = d._native_profile("p", nativo)
    riga = next(r for r in profilo.splitlines() if r.strip().startswith("secure:"))
    # the default is Tabularia's choice; CLICKHOUSE_SECURE changes it, as on the ClickHouse target
    assert riga == "      secure: \"{{ (env_var('CLICKHOUSE_SECURE', '" + str(tls).lower() + "') | lower in ['true', '1', 'yes']) | as_bool }}\""
    assert ("over TLS (`secure: true`)" in _progetto(attachments=None, native=nativo)["README.md"]) is tls


# #64 — every step lists and qualifies every column on ClickHouse: a wide, long flow passes 256 KiB,
# ClickHouse's default max_query_size. The profiles never raised it (dbt run failed), the syntax
# check refused it as «not translatable» (and asked the AI), and it measured each model alone while
# dbt inlines the ephemeral ones into the model that reads them
_LARGHE = [f"colonna_numero_{i:03d}" for i in range(150)]


def _modelli_larghi(passi_intermedio: int, passi_uscita: int) -> list:
    filtri = lambda n, da: [{"type": "filter", "params": {"column": _LARGHE[(da + i) % 150], "operator": "ge", "value": -i}} for i in range(n)]  # noqa: E731
    return [{"name": "int_largo", "kind": "intermediate", "materialized": "ephemeral", "source": {"key": "k"}, "operations": filtri(passi_intermedio, 0)},
            {"name": "uno", "source": {"model": "int_largo"}, "operations": filtri(passi_uscita, passi_intermedio)}]


def _esporta_largo(modelli: list):
    return _richiesta_export(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"}, models=modelli,
                             source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'larga') }}", "columns": _LARGHE,
                                               "types": {c: "Int64" for c in _LARGHE}, "label": "larga"}})


def test_the_clickhouse_profiles_raise_max_query_size_and_the_check_uses_the_same_limit():
    for profilo in (_progetto(attachments=None, clickhouse={"host": "h", "federati": []})["profiles.yml"],
                    d._native_profile("p", {**_NATIVO_CH, "target_schema": "s"})):
        assert _impostazioni_del_profilo(profilo)["max_query_size"] == str(d.MAX_QUERY_SIZE)
    assert d.MAX_QUERY_SIZE > 262144
    # one model over ClickHouse's default 256 KiB: exported, it runs with the profile's setting
    files = _files(_esporta_largo([{**_modelli_larghi(0, 28)[1], "source": {"key": "k"}}]))
    assert len(files["models/uno.sql"]) > 262144


def test_a_model_over_the_limit_with_the_ephemeral_models_it_inlines_is_refused_with_its_own_code(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(d, "MAX_QUERY_SIZE", 150_000)
    # each model alone is under the limit, together (as dbt runs the output) they are over it
    with pytest.raises(HTTPException) as e:
        _zip(_esporta_largo(_modelli_larghi(9, 9)))
    assert e.value.detail["code"] == "model_too_large"
    assert e.value.detail["params"]["model"] == "uno" and e.value.detail["params"]["max"] == "150,000"
    assert int(e.value.detail["params"]["size"].replace(",", "")) > 150_000
    # one model alone over the limit: the same code, not «not translatable» (which asks the AI for a translation)
    with pytest.raises(HTTPException) as e:
        _zip(_esporta_largo([{**_modelli_larghi(0, 18)[1], "source": {"key": "k"}}]))
    assert e.value.detail["code"] == "model_too_large" and e.value.detail["params"]["model"] == "uno"


def test_the_size_of_a_clickhouse_model_is_counted_in_bytes(monkeypatch):
    """#64: max_query_size is in bytes; a model of accented text near the limit passed a check
    that counted characters and failed at `dbt run`."""
    from fastapi import HTTPException

    monkeypatch.setattr(d, "MAX_QUERY_SIZE", 100_000)
    testo = "'" + "à" * 60_000 + "'"   # 60,000 characters, 120,000 bytes
    with pytest.raises(HTTPException) as e:
        _esporta({"id": "Int64"}, [{"type": "compute", "params": {"columns": [{"name": "n", "expr": testo}]}}], mode="clickhouse")
    assert e.value.detail["code"] == "model_too_large" and int(e.value.detail["params"]["size"].replace(",", "")) > 120_000
    # the same text in ASCII is under the limit
    _esporta({"id": "Int64"}, [{"type": "compute", "params": {"columns": [{"name": "n", "expr": "'" + "a" * 60_000 + "'"}]}}], mode="clickhouse")


def test_a_column_with_a_double_quote_is_refused_where_dbt_clickhouse_cannot_create_it():
    """dbt-clickhouse writes a table's column list as "…" without escaping a double quote: a table or
    incremental model with the column `quote"col` failed at `dbt run` (Code 62, unmatched parentheses;
    measured with dbt-clickhouse 1.10). A view, an ephemeral model and the other adapters build it."""
    from fastapi import HTTPException

    tipi = {"id": "Int64", 'quote"col': "String"}
    for modo, extra in (("clickhouse", {"clickhouse": {"connections": [_PG], "target_schema": "s"}}),
                        ("native", {"native": {**_NATIVO_CH, "target_schema": "s"}})):
        voce = {"ident": "__s_0", "macro": "{{ source('db_1_v', 'q') }}", "columns": list(tipi), "types": tipi, "label": "q"}
        for materializzato in ("table", "incremental"):
            with pytest.raises(HTTPException) as e:
                _zip(_richiesta_export(mode=modo, **extra, source_map={"k": voce},
                                       models=[{"name": "uno", "source": {"key": "k"}, "operations": [], "materialized": materializzato}]))
            assert e.value.detail["code"] == "column_name_unsupported", e.value.detail
            assert e.value.detail["params"] == {"model": "uno", "column": 'quote"col', "character": '"', "adapter": "dbt-clickhouse"}
        for materializzato in ("view", "ephemeral"):
            _zip(_richiesta_export(mode=modo, **extra, source_map={"k": voce},
                                   models=[{"name": "uno", "source": {"key": "k"}, "operations": [], "materialized": materializzato}]))
    # the other adapters quote it
    _zip(_richiesta_export(mode="native", native=_PG_NATIVE, source_map={"k": {**voce, "macro": "{{ source('db_1_v', 'q') }}"}},
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

    muto = socket.socket()
    muto.bind(("127.0.0.1", 0))
    muto.listen(8)                    # accepts, never answers
    cfg = get_settings().clickhouse_external
    monkeypatch.setattr(cfg, "host", "127.0.0.1")
    monkeypatch.setattr(cfg, "port", muto.getsockname()[1])
    monkeypatch.setitem(sys.modules, "chdb", None)        # as in the production image
    monkeypatch.setattr(d, "ATTESA_CONTROLLO", 1)
    controllo, esito = d.ControlloSintassiClickHouse(), {}

    def controlla():
        inizio = time.monotonic()
        esito["primo"] = controllo.errore("SELECT 1")
        esito["secondi"] = time.monotonic() - inizio
        esito["poi"] = controllo.errore("SELECT FROM WHERE (")
        esito["dopo"] = time.monotonic() - inizio
    filo = threading.Thread(target=controlla, daemon=True)
    filo.start()
    filo.join(20)
    try:
        assert not filo.is_alive(), "the check is still waiting for ClickHouse"
        assert esito["primo"] is None and esito["poi"] is None and esito["secondi"] < 10
        assert esito["dopo"] - esito["secondi"] < 1        # off for the rest of the export: no second wait
        assert controllo.saltato
        controllo.chiudi()
        # the README says that the SQL was not read by a ClickHouse parser
        inizio = time.monotonic()
        files = _files(_richiesta_export(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"},
                                         source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "x"],
                                                           "types": {"id": "Int64", "x": "String"}, "label": "ordini"}}))
        assert time.monotonic() - inizio < 10
        assert "not read by a ClickHouse parser" in files["README.md"]
    finally:
        muto.close()
    # with a parser that answers (chDB here), no such note
    monkeypatch.undo()
    files = _files(_richiesta_export(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"},
                                     source_map={"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "x"],
                                                       "types": {"id": "Int64", "x": "String"}, "label": "ordini"}}))
    assert "not read by a ClickHouse parser" not in files["README.md"]


# #68 — two schemas that differ only by case, punctuation or non-ASCII letters were ONE federated
# database: it was made for the first, and the second's tables were read from the first schema
def test_every_source_schema_has_a_database_of_its_own_on_clickhouse():
    ch = d.TargetClickHouse([_PG, _MY], "ch.local")
    schemi = ("Sales", "sales", "sales-eu", "sales_eu", "vendite_città", "vendite_citt", "продажи", "склад", "default", "Vendite Più")
    nomi = {s: ch.database_per(1, s, "orders") for s in schemi}
    assert len(set(nomi.values())) == len(schemi)
    assert {f["name"]: f["schema"] for f in ch.federati.values()} == {n: s for s, n in nomi.items()}
    # a schema that is already a plain lowercase name keeps the database it always had
    assert nomi["sales"] == "tab_src_1_sales" and nomi["sales_eu"] == "tab_src_1_sales_eu" and nomi["default"] == "tab_src_1_default"
    assert ch.database_per(2, "shop") == "tab_src_2_shop"
    # the others: the readable part and a short fingerprint of the exact name, the same at every export
    assert re.fullmatch(r"tab_src_1_sales_[0-9a-f]{10}", nomi["Sales"]) and re.fullmatch(r"tab_src_1_default_[0-9a-f]{10}", nomi["продажи"])
    assert nomi == {s: d.nome_db_federato(1, s) for s in schemi}


# #25 #26 — a datasource query translated for the ClickHouse target: ClickHouse 24 hands a typed date
# constant to the PostgreSQL/MySQL table as a number of days (`"nato" < 0`: Postgres refuses the query,
# found by the real dbt proof), and its upper/substring work on bytes as everywhere on ClickHouse
def test_a_translated_query_compares_dates_by_their_text_and_keeps_characters_and_wide_types():
    ch = d.TargetClickHouse([_PG], "ch.local")
    sql = " ".join(ch.traduci_query("SELECT id, upper(nome) AS u, substring(nome, 2, 3) AS s, CAST(visto AS date) AS giorno FROM zz.persone "
                                    "WHERE nato < DATE '1970-01-01' AND visto >= '2024-01-01 10:00'::timestamp "
                                    "AND nato IN (DATE '1955-03-12', DATE '1969-12-31') AND nato BETWEEN DATE '1900-01-01' AND CAST('1970-01-01' AS date)",
                                    1, "q").split())
    assert "nato < '1970-01-01'" in sql and "visto >= '2024-01-01 10:00'" in sql
    assert "nato IN ('1955-03-12', '1969-12-31')" in sql and "nato BETWEEN '1900-01-01' AND '1970-01-01'" in sql
    assert "upperUTF8(nome)" in sql and "substringUTF8(nome, 2, 3)" in sql and "CAST(visto AS Nullable(Date32))" in sql
    assert "Nullable(DATE)" not in sql and "Nullable(DateTime)" not in sql
    # a MySQL query too: its DATETIME is an instant to the microsecond, with no range of 1970-2106
    my = d.TargetClickHouse([_MY], "ch.local").traduci_query("SELECT CAST(quando AS DATETIME) AS q, lower(nome) AS n FROM shop.t "
                                                             "WHERE quando >= TIMESTAMP '1960-01-01 00:00:00'", 2, "m")
    assert "Nullable(DateTime64(6))" in my and "lowerUTF8(nome)" in my and "quando >= '1960-01-01 00:00:00'" in " ".join(my.split())


# #25 (found by the proof) — ClickHouse's PostgreSQL engine reads a timestamptz as the wall-clock time of
# its Postgres session and drops the offset: on a server in Europe/Rome 10:30 UTC became 12:30
def test_the_readme_says_how_clickhouse_reads_a_postgres_timestamptz_right():
    files = _esporta({"id": "Int32", "ts": _UTC}, [], mode="clickhouse")
    assert "ALTER ROLE shop SET timezone = 'UTC'" in files["README.md"]
    assert "ALTER ROLE" not in _esporta({"id": "Int32", "ts": _DT}, [], mode="clickhouse")["README.md"]
    # a datasource query with such a column too
    raw = [{"name": "src_q", "kind": "raw", "materialized": "ephemeral", "raw_sql": "SELECT id, ts FROM v.ordini", "translate": {"connection_id": 1},
            "columns": [{"name": "id", "dtype": "Int32"}, {"name": "ts", "dtype": _UTC}]}]
    q = _files(_richiesta_export(models=raw, source_map={}, mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "s"}))
    assert "ALTER ROLE shop SET timezone = 'UTC'" in q["README.md"]


# G2 handoff 1 — a source() macro the gateway writes for a table name with a backslash (`'\\'` in Jinja)
# was used as a re.sub template: the backslashes were processed and dbt could not parse the model
def test_a_source_macro_with_backslashes_reaches_the_model_exactly():
    macro = "{{ source('src_vendite', 'o' ~ \"'\" ~ 'brien' ~ '\\\\' ~ 'x') }}"
    sql = d.substitute_source_idents('SELECT * FROM "__s_0" AS "__s_0"', {"__s_0": macro})
    assert sql == f'SELECT * FROM {macro} AS "__s_0"'


# ── E5: the data contract, the seeds and the guides the export writes ───────────
import decimal as _decimal

from app.contracts.evaluator import evaluate as _valuta

_TIPO_PG_DI = {"String": "TEXT", "Int64": "BIGINT", "Date": "DATE", "Boolean": "BOOLEAN", "Float64": "DOUBLE PRECISION"}
_TIPO_CH_DI = {"String": "Nullable(String)", "Int64": "Nullable(Int64)", "Date": "Nullable(Date32)", "Boolean": "Nullable(Bool)",
               "Float64": "Nullable(Float64)"}


def _tipo_in(dtype: str, dove: str) -> str:
    if dtype.startswith("Datetime"):
        return "TIMESTAMP" if dove == "postgres" else "Nullable(DateTime64(6))"
    m = re.match(r"Decimal\(precision=(\d+), scale=(\d+)\)", dtype)
    if m:
        return f"NUMERIC({m[1]},{m[2]})" if dove == "postgres" else f"Nullable(Decimal({m[1]}, {m[2]}))"
    return (_TIPO_PG_DI if dove == "postgres" else _TIPO_CH_DI)[dtype]


def _relazione(df: "_pl.DataFrame", dove: str, con=None) -> str:
    """The rows of a model as a relation of `dove` (duckdb: a table in `con`; postgres and
    clickhouse: a typed VALUES, nothing written in the database)."""
    if dove == "duckdb":
        con.register("_df_uno", df.to_arrow())
        con.execute('CREATE OR REPLACE TABLE "uno_dati" AS SELECT * FROM "_df_uno"')
        return '"uno_dati"'
    tipi = {c: _tipo_in(str(t), dove) for c, t in df.schema.items()}

    def lit(v, t):
        if v is None:
            return f"CAST(NULL AS {t})" if dove == "postgres" else "NULL"
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        testo = str(v)
        if dove == "postgres":
            return "CAST('" + testo.replace("'", "''") + f"' AS {t})"
        if isinstance(v, (int, float, _decimal.Decimal)):
            return testo
        return "'" + testo.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r") + "'"
    righe = [", ".join(lit(v, tipi[c]) for c, v in r.items()) for r in df.to_dicts()]
    if dove == "postgres":
        nomi = ", ".join(d._qi(c) for c in df.columns)
        return "(SELECT * FROM (VALUES " + ", ".join(f"({r})" for r in righe) + f") AS v({nomi}))"
    struttura = ", ".join(f"{c} {t}" for c, t in tipi.items())
    return f"(SELECT * FROM values('{struttura}', " + ", ".join(f"({r})" for r in righe) + "))"


def _righe_dei_test(files: dict, df: "_pl.DataFrame", dove: str) -> dict:
    """Every singular test of the project → the rows it selects (dbt's failures) on `dove`,
    over a model `uno` whose rows are `df`."""
    import duckdb

    con = duckdb.connect() if dove == "duckdb" else None
    rel = _relazione(df, dove, con)
    out = {}
    for path, testo in sorted(files.items()):
        if not path.startswith("tests/"):
            continue
        sql = _render_like_dbt(testo, {("ref", "uno"): rel}, config={})
        if dove == "duckdb":
            out[path] = len(con.execute(sql).fetchall())
        elif dove == "postgres":
            out[path] = len(_su_postgres(sql))
        else:
            out[path] = len(_su_clickhouse(sql))
    return out


def _esporta_contratto(df: "_pl.DataFrame", regole: list | None = None, colonne: list | None = None, mode: str = "duckdb") -> dict:
    """The project the route writes for one model `uno` that publishes `df` as it is, with the
    contract's tests as the gateway sends them (singular rules; column tests)."""
    types = {c: str(t) for c, t in df.schema.items()}
    if mode == "duckdb":
        voce, extra = {"ref": "{{ source('db_1_v', 'dati') }}"}, {}
    else:
        voce = {"ident": "__s_0", "macro": "{{ source('db_1_v', 'dati') }}"}
        extra = {"native": _PG_NATIVE} if mode == "native" else {"clickhouse": {"connections": [_PG], "target_schema": "dbt_tabularia"}}
    return _files(_richiesta_export(mode=mode, **extra, source_map={"k": {**voce, "columns": list(df.columns), "types": types, "label": "dati"}},
                                    models=[{"name": "uno", "source": {"key": "k"}, "operations": [], "tests": list(regole or []),
                                             "columns": list(colonne or [])}]))


_DOVE = {"duckdb": "duckdb", "native": "postgres", "clickhouse": "clickhouse"}


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
_REGOLE_PATTERN = [
    ("cap", "[0-9]{5}"), ("codice", "[A-Z]+-[0-9]+"), ("codice", ".+"), ("codice", "(?i)ab-12"), ("codice", "[^x]+"),
    ("anno", "20[0-9]{2}"), ("giorno", r"\d{4}-01-\d{2}"), ("visto", r".*:00\.000000"), ("attivo", "true"),
    ("prezzo", r"-?[0-9]+\.[0-9]{2}"), ("misura", r"-?[0-9]+\.[0-9]"),
]


@pytest.mark.parametrize("mode", ["duckdb", "native", "clickhouse"])
def test_a_pattern_rule_fails_in_dbt_exactly_where_tabularia_s_contract_does(mode):
    regole = [{"id": f"r{i}", "kind": "pattern", "column": c, "regex": r, "severity": "error"} for i, (c, r) in enumerate(_REGOLE_PATTERN, 1)]
    attese = {f"tests/uno__pattern_{x['id']}.sql": x["violations"] for x in _valuta(_PATTERN.lazy(), {"rules": regole})["rules"]}
    assert [attese[f"tests/uno__pattern_r{i}.sql"] for i in range(1, 12)] == [3, 3, 2, 4, 2, 2, 1, 3, 2, 0, 0]
    files = _esporta_contratto(_PATTERN, regole, mode=mode)
    assert _righe_dei_test(files, _PATTERN, _DOVE[mode]) == attese


_REGEX = ["a.b", ".+", "[.]b", r"a\.b", "[]a.]+", "[^]x]+", "[[:alpha:].]+", "(?s)a.b", "(?i)A.B", r"a\\.b", "[a-c.]{3}"]
_TESTI_REGEX = ["a.b", "a\nb", "axb", "ab", ".b", "]a.", "a]b", "AxB", "a\\xb", "a\\\nb", "b.c", "x"]


@pytest.mark.parametrize("dove", ["duckdb", "postgres", "clickhouse"])
def test_the_regex_of_a_pattern_rule_reads_newlines_and_flags_as_tabularia_does(dove):
    """Polars' `.` does not match a newline; Postgres' and ClickHouse's do. Postgres reads
    inline flags only at the start of the pattern, so `^(?:(?i)…)$` made the test ERROR."""
    import duckdb

    attese = {r: _pl.Series(_TESTI_REGEX).str.contains(f"^(?:{r})$").to_list() for r in _REGEX}
    dialetto = {"duckdb": None, "postgres": "postgres", "clickhouse": "clickhouse"}[dove]
    sql = "SELECT " + ", ".join(f"regexp_matches({d._lit(v)}, {d._lit(d._regex_intera(r, dialetto))})" for r in _REGEX for v in _TESTI_REGEX)
    if dove == "duckdb":
        riga = duckdb.connect().execute(sql).fetchone()
    else:
        tradotto = d.transpile_model(sql, dialetto, {})
        riga = (_su_postgres(tradotto) if dove == "postgres" else _su_clickhouse(tradotto))[0]
    avute = {r: [bool(x) for x in riga[i * len(_TESTI_REGEX):(i + 1) * len(_TESTI_REGEX)]] for i, r in enumerate(_REGEX)}
    assert avute == attese


def _test_generici(schema_yml: str) -> dict:
    """models/schema.yml → {(model, column): {"quote": bool, "tests": [(kind, severity)]}}: the
    generic tests dbt builds from it (only the forms the exporter writes)."""
    import json

    out, modello, colonna = {}, None, None
    for riga in schema_yml.splitlines():
        if riga.startswith("  - name: "):
            modello = riga[len("  - name: "):].strip('"')
        elif riga.startswith("      - name: "):
            colonna = json.loads(riga[len("      - name: "):])
            out[(modello, colonna)] = {"quote": False, "tests": []}
        elif riga == "        quote: true":
            out[(modello, colonna)]["quote"] = True
        elif m := re.fullmatch(r"          - (\w+):", riga):
            out[(modello, colonna)]["tests"].append([m.group(1), None])
        elif m := re.fullmatch(r"              config: \{severity: (\w+)\}", riga):
            out[(modello, colonna)]["tests"][-1][1] = m.group(1)
    return {k: {"quote": v["quote"], "tests": [tuple(t) for t in v["tests"]]} for k, v in out.items() if v["tests"]}


def _not_null_come_dbt(files: dict, df: "_pl.DataFrame", dove: str) -> dict:
    """dbt's own not_null test (default__test_not_null) on `dove`, with the column name as dbt
    puts it in the SQL: quoted by the adapter (`"…"`) only with `quote: true`."""
    import duckdb

    con = duckdb.connect() if dove == "duckdb" else None
    rel = _relazione(df, dove, con)
    out = {}
    for (_, col), v in _test_generici(files["models/schema.yml"]).items():
        if col not in df.columns:
            continue
        for kind, sev in v["tests"]:
            assert kind == "not_null", kind
            nome = f'"{col}"' if v["quote"] else col
            sql = f"select {nome} from {rel} AS m where {nome} is null"
            out[(col, sev)] = len(con.execute(sql).fetchall() if dove == "duckdb" else _su_postgres(sql))
    return out


# #33 — dbt's generic tests put the column name in the SQL as it is: a space or a reserved word
# broke the test on every warehouse, and Postgres read «Canale» as canale
_NOMI = _pl.DataFrame({"Nome Cliente": ["Anna", None, "Bruno"], "Canale": [None, "web", None], "select": ["x", "y", "z"],
                       'il "codice"': ["a", None, "c"]})


@pytest.mark.parametrize("mode", ["duckdb", "native"])
def test_a_not_null_rule_on_a_column_with_spaces_upper_case_or_a_reserved_word_runs(mode):
    colonne = [{"name": c, "description": "", "tests": [{"kind": "not_null", "severity": "error"}]} for c in _NOMI.columns]
    regole = [{"id": f"r{i}", "kind": "not_null", "column": c, "severity": "error"} for i, c in enumerate(_NOMI.columns)]
    attese = {x["column"]: x["violations"] for x in _valuta(_NOMI.lazy(), {"rules": regole})["rules"]}
    files = _esporta_contratto(_NOMI, colonne=colonne, mode=mode)
    avute = {c: n for (c, _), n in _not_null_come_dbt(files, _NOMI.drop('il "codice"'), _DOVE[mode]).items() if c != 'il "codice"'}
    generici = _test_generici(files["models/schema.yml"])
    # a name with a double quote cannot be quoted by the adapter (it writes "…" as it is): a singular test
    assert set(c for _, c in generici) == {"Nome Cliente", "Canale", "select"} and all(v["quote"] for v in generici.values())
    avute |= {'il "codice"': n for p, n in _righe_dei_test(files, _NOMI, _DOVE[mode]).items() if "not_null" in p}
    assert avute == attese == {"Nome Cliente": 1, "Canale": 2, "select": 0, 'il "codice"': 1}


# #34 — dbt's accepted_values writes every value as '{{ value }}': an apostrophe (Sant'Antonio,
# L'Aquila) broke the SQL; a 3.0 on an integer column failed on Postgres and ClickHouse, where
# Tabularia compares numbers as numbers, booleans as booleans and anything else by its text
_AMMESSI = _pl.DataFrame({
    "citta": ["Roma", "Sant'Antonio", "Bari", None, "C:\\temp", "L'Aquila"],
    "n": [1, 2, 3, None, 2, 1],
    "f": [1.0, 2.5, 3.0, None, 2.5, 1.0],
    "ok": [True, False, True, None, True, True],
    "giorno": [_dt.date(2024, 1, 5), _dt.date(2024, 1, 6), None, _dt.date(2024, 1, 5), _dt.date(2024, 1, 5), _dt.date(2024, 1, 7)],
})
_REGOLE_AMMESSI = [
    ("citta", ["Roma", "Milano", "Bari", "Sant'Antonio", "C:\\temp", "L'Aquila"]), ("citta", ["Roma", "Sant'Antonio"]),
    ("n", [1, 2, 3.0]), ("n", ["1", "2"]), ("n", [1, "due"]), ("f", [1, 2.5]), ("ok", ["true"]), ("ok", [True, False]),
    ("giorno", ["2024-01-05", "2024-01-06"]),
]


@pytest.mark.parametrize("mode", ["duckdb", "native", "clickhouse"])
def test_an_accepted_values_rule_fails_in_dbt_exactly_where_tabularia_s_contract_does(mode):
    regole = [{"id": f"r{i}", "kind": "accepted_values", "column": c, "values": v, "severity": "warning"}
              for i, (c, v) in enumerate(_REGOLE_AMMESSI, 1)]
    attese = [x["violations"] for x in _valuta(_AMMESSI.lazy(), {"rules": regole})["rules"]]
    assert attese == [0, 3, 0, 1, 3, 1, 1, 0, 1]
    colonne = {}
    for c, v in _REGOLE_AMMESSI:
        colonne.setdefault(c, {"name": c, "description": "", "tests": []})["tests"].append({"kind": "accepted_values", "severity": "warn", "values": v})
    files = _esporta_contratto(_AMMESSI, colonne=list(colonne.values()), mode=mode)
    righe = _righe_dei_test(files, _AMMESSI, _DOVE[mode])
    nomi = ["citta", "citta_2", "n", "n_2", "n_3", "f", "ok", "ok_2", "giorno"]
    assert righe == {f"tests/uno__accepted_values_{n}.sql": a for n, a in zip(nomi, attese)}
    assert "accepted_values" not in files["models/schema.yml"]
    assert files["tests/uno__accepted_values_citta.sql"].startswith(
        "{{ config(severity='warn') }}\n-- Contract rule accepted_values on uno, column «citta»\n")


# #35 (decision 7) — a one-column unique rule: Tabularia counts two NULLs as a duplicate (NULLs
# count as values), dbt's generic `unique` filters them out (`where col is not null`) and passed
@pytest.mark.parametrize("mode", ["duckdb", "native", "clickhouse"])
@pytest.mark.parametrize("valori,viola", [(["A1", None, None, "B2"], True), (["A1", None, "B2", "B2"], True), (["A1", None, "B2", "C3"], False)])
def test_a_one_column_unique_rule_counts_two_nulls_as_a_duplicate_like_tabularia(mode, valori, viola):
    df = _pl.DataFrame({"codice": valori, "n": [1, 2, 3, 4]})
    esito = _valuta(df.lazy(), {"rules": [{"id": "r1", "kind": "unique", "columns": ["codice"], "severity": "error"}]})["rules"][0]
    assert (esito["violations"] > 0) is viola
    files = _esporta_contratto(df, colonne=[{"name": "codice", "description": "", "tests": [{"kind": "unique", "severity": "error"}]}], mode=mode)
    assert "unique" not in files["models/schema.yml"]
    assert {p: n > 0 for p, n in _righe_dei_test(files, df, _DOVE[mode]).items()} == {"tests/uno__unique_codice.sql": viola}


# #40 — two rules of one generic kind on one column (a warning the proposal made plus an error
# added by hand) were two tests with the same name: dbt refused the whole project at parse
def test_two_rules_of_one_kind_on_one_column_never_give_two_tests_with_the_same_name():
    df = _pl.DataFrame({"codice": ["a", None, "a", "c"], "Codice": ["x", "y", "x", None]})
    dup = lambda kind, **kw: [{"kind": kind, "severity": "warn", **kw}, {"kind": kind, "severity": "error", **kw}]   # noqa: E731
    colonne = [{"name": "codice", "description": "", "tests": dup("not_null") + dup("unique") + dup("accepted_values", values=["a", "b"])},
               {"name": "Codice", "description": "", "tests": dup("not_null") + dup("unique")}]
    files = _esporta_contratto(df, colonne=colonne, mode="native")
    generici = _test_generici(files["models/schema.yml"])
    # the same check twice is one test, with the stricter severity: it fails where the error rule fails
    assert generici == {("uno", "codice"): {"quote": True, "tests": [("not_null", "error")]},
                        ("uno", "Codice"): {"quote": True, "tests": [("not_null", "error")]}}
    singolari = {p: re.match(r"\{\{ config\(severity='(\w+)'\) \}\}", t).group(1) for p, t in files.items() if p.startswith("tests/")}
    assert singolari == {"tests/uno__unique_codice.sql": "warn", "tests/uno__unique_codice_2.sql": "error",
                         "tests/uno__accepted_values_codice.sql": "warn", "tests/uno__accepted_values_codice_2.sql": "error",
                         "tests/uno__unique_codice_3.sql": "warn", "tests/uno__unique_codice_4.sql": "error"}
    assert "«Codice»" in files["tests/uno__unique_codice_3.sql"] and "«codice»" in files["tests/uno__unique_codice.sql"]
    assert _righe_dei_test(files, df, "duckdb") == {"tests/uno__unique_codice.sql": 1, "tests/uno__unique_codice_2.sql": 1,
                                                     "tests/uno__accepted_values_codice.sql": 1, "tests/uno__accepted_values_codice_2.sql": 1,
                                                     "tests/uno__unique_codice_3.sql": 1, "tests/uno__unique_codice_4.sql": 1}


# #36 — a file datasource is a seed, and dbt's seed loaders change some texts: dbt-duckdb's COPY
# reads "" as NULL; agate (dbt-postgres, dbt-clickhouse) reads '', 'null' in any case and blank
# text as NULL and turns \r into \n; ClickHouse's CSV input cuts spaces and tabs at the ends and
# reads \N as NULL. Tabularia keeps every one of them: is_null, fill_null and joins differed.
_TESTI_DEL_SEED = ["Anna", "", None, "null", "NULL", " Null ", "  ", "\t", "\xa0", " a ", "a ", "\tx", "x\t", "\\N", "a\\Nb", "\r",
                   "x\r\ny", "a\rb", "~", "~x", "~0", "~1~", "~~", '"q"', "a,b", "a\nb", "\u2028", "N/A", "Città", "x~1y", "'q'", "'"]
_SEED = _pl.DataFrame({"id": list(range(1, len(_TESTI_DEL_SEED) + 1)), "v": _TESTI_DEL_SEED,
                       "w": ["plain"] * len(_TESTI_DEL_SEED), "n": [None if i % 3 else i for i in range(len(_TESTI_DEL_SEED))]})


def _esporta_seed(df: "_pl.DataFrame", mode: str, monkeypatch) -> dict:
    import app.utils
    from tests.conftest import upload_df
    from tests.fakes import FakeStorage

    storage = FakeStorage()
    upload_df(storage, df, "datasets/seed_dati.parquet")
    monkeypatch.setattr(app.utils, "get_storage_service", lambda: storage)
    extra = {"native": _PG_NATIVE} if mode == "native" else ({"clickhouse": {"connections": [_PG], "target_schema": "s"}} if mode == "clickhouse" else {})
    return _files(_richiesta_export(mode=mode, **extra, source_map={},
                                    seeds=[{"name": "seed_dati", **_key("seed_dati"), "description": "", "columns": []}],
                                    models=[{"name": "uno", "source": {"seed": "seed_dati"}, "operations": []}]))


def _come_agate(csv_testo: str) -> list:
    """What dbt's seed loader (dbt_common agate_helper.from_csv, column_types given) makes of
    the CSV: a file read with Python's universal newlines, every typed column as agate Text
    whose null values are '' and 'null' (compared stripped and lower-cased)."""
    import csv, os, tempfile

    fd, path = tempfile.mkstemp(suffix=".csv")
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
        f.write(csv_testo)
    try:
        with open(path, encoding="utf-8") as f:
            righe = list(csv.reader(f))
    finally:
        os.remove(path)
    return [[None if v.strip().lower() in ("", "null") else v for v in r] for r in righe[1:]]


def _seed_caricato(files: dict, dove: str, con=None) -> str:
    """The seed table as the warehouse's dbt adapter loads it, as a relation of `dove`."""
    import csv, io, os, tempfile

    testo = files["seeds/seed_dati.csv"]
    if dove == "duckdb":
        # dbt-duckdb: COPY … FROM '<seed file>' (FORMAT CSV, HEADER TRUE, DELIMITER ',')
        fd, path = tempfile.mkstemp(suffix=".csv")
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(testo)
        con.execute('CREATE TABLE "seed_dati" ("id" BIGINT, "v" VARCHAR, "w" VARCHAR, "n" BIGINT)')
        con.execute(f"""COPY "seed_dati" FROM '{path}' (FORMAT CSV, HEADER TRUE, DELIMITER ',')""")
        os.remove(path)
        return '"seed_dati"'
    righe = _come_agate(testo)
    if dove == "postgres":
        lit = lambda v, t: f"CAST(NULL AS {t})" if v is None else "CAST('" + v.replace("'", "''") + f"' AS {t})"   # noqa: E731
        return ('(SELECT * FROM (VALUES ' + ", ".join(f"({lit(r[0], 'BIGINT')}, {lit(r[1], 'TEXT')}, {lit(r[2], 'TEXT')}, {lit(r[3], 'BIGINT')})"
                                                      for r in righe) + ') AS v("id", "v", "w", "n"))')
    # dbt-clickhouse: the agate rows written back with csv.writer, then INSERT … FORMAT CSV
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(["" if v is None else v for v in r] for r in righe)
    cli = _clickhouse()
    cli.command('CREATE TEMPORARY TABLE "seed_dati" ("id" Nullable(Int64), "v" Nullable(String), "w" Nullable(String), "n" Nullable(Int64))')
    cli.command('INSERT INTO "seed_dati" FORMAT CSV\n' + buf.getvalue())
    con.append(cli)
    return '"seed_dati"'


@pytest.mark.parametrize("mode", ["duckdb", "native", "clickhouse"])
def test_a_seed_gives_the_models_every_text_tabularia_reads_even_the_ones_dbt_s_loaders_change(mode, monkeypatch):
    import duckdb

    files = _esporta_seed(_SEED, mode, monkeypatch)
    dove = _DOVE[mode]
    con = duckdb.connect() if dove == "duckdb" else []
    rel = _seed_caricato(files, dove, con)
    sql = _render_like_dbt(files["models/uno.sql"], {("ref", "seed_dati"): rel}, config={})
    if dove == "duckdb":
        righe = con.execute(sql).fetchall()
    elif dove == "postgres":
        righe = _su_postgres(sql)
    else:
        righe = [tuple(r) for r in con[0].query(sql).result_rows]
    assert sorted(righe, key=lambda r: r[0]) == list(_SEED.iter_rows())
    # only the column that needs it is written another way; the README says so
    import csv, io

    assert [r[2] for r in csv.reader(io.StringIO(files["seeds/seed_dati.csv"], newline=""))][1:] == ["plain"] * len(_TESTI_DEL_SEED)
    assert "`seed_dati`" in files["README.md"] and "`v`" in files["README.md"] and "`w`" not in files["README.md"]


def test_a_seed_without_texts_the_loaders_change_is_written_as_it_was(monkeypatch):
    df = _pl.DataFrame({"id": [1, 2, 3], "v": ["a", None, "b c"]})
    files = _esporta_seed(df, "duckdb", monkeypatch)
    assert files["seeds/seed_dati.csv"] == df.write_csv()
    assert "seed_dati" not in files["README.md"].split("## Notes")[-1]


# #70 (decision 10) — «already declared» means the team's own sources.yml declares the source: that
# exists only for the folder package. A complete project left the source out of its sources.yml,
# and `dbt parse` failed: «depends on a source named 'db_1_vendite.ordini' which was not found»
@pytest.mark.parametrize("mode", ["duckdb", "clickhouse"])
@pytest.mark.parametrize("layout", [None, {"package": "project", "folder": "", "layers": False}])
def test_already_declared_sources_are_ignored_outside_the_folder_package(mode, layout):
    sorgenti = [{"name": "db_1_vendite", "schema": "vendite", "connection_id": 1, "declared": True, "database": "db_1",
                 "tables": [{"name": "ordini", "description": "", "columns": []}]}]
    extra = {"clickhouse": {"connections": [_PG], "target_schema": "s"}} if mode == "clickhouse" else {}
    voce = {"ident": "__s_0", "macro": "{{ source('db_1_vendite', 'ordini') }}"} if mode == "clickhouse" else {"ref": "{{ source('db_1_vendite', 'ordini') }}"}
    files = _files(_richiesta_export(mode=mode, **extra, layout=layout, sources=sorgenti,
                                     source_map={"k": {**voce, "columns": ["id"], "types": {"id": "Int64"}, "label": "ordini"}},
                                     models=[{"name": "uno", "source": {"key": "k"}, "operations": []}]))
    assert "  - name: db_1_vendite\n" in files["models/sources.yml"] and "      - name: ordini\n" in files["models/sources.yml"]
    if mode == "clickhouse":
        # and the macro creates the ClickHouse database the source reads
        assert "tab_src_1_vendite" in files["macros/create_tabularia_sources.sql"]
    cartella = _files(_richiesta_export(mode=mode, **extra, layout={"package": "folder", "folder": "f"}, sources=sorgenti,
                                        source_map={"k": {**voce, "columns": ["id"], "types": {"id": "Int64"}, "label": "ordini"}},
                                        models=[{"name": "uno", "source": {"key": "k"}, "operations": []}]))
    assert "models/f/sources.yml" not in cartella and "`db_1_vendite` is **declared in your project**" in cartella["INTEGRATION.md"]


# #81 — on the federated target the models land in DuckDB's `main` unless a schema is chosen
# (dbt-duckdb's default; the profile names no schema). The docs said `dbt_tabularia`, and the
# README said nothing: whoever queried dbt_tabularia.<model> found no such schema
@pytest.mark.parametrize("schema,dove", [(None, "main"), ("analisi", "analisi")])
def test_the_readme_says_where_the_federated_models_land(schema, dove):
    files = _files(_richiesta_export(schema=schema))
    profilo = files["profiles.yml"]
    assert ("schema:" in profilo) is (schema is not None)
    assert f"The models are built in `margini.duckdb`, in the schema `{dove}`" in files["README.md"]


# #41 — the folder package goes into the team's project, where a tag like `sales` is common: the
# documented `dbt build -s +tag:sales` also built their models tagged `sales` and all they read
def test_the_folder_package_selects_only_what_came_from_tabularia():
    modelli = [{"name": "sales_out", "kind": "output", "sql": "select * from {{ ref('seed_dati') }}", "materialized": "table",
                "description": "", "columns": [], "meta": {"flow": "Sales", "flow_id": 3}, "tags": ["tabularia", "sales"]}]
    files = _progetto(flow_name="Sales", models=modelli, layout={"package": "folder", "folder": "tabularia_sales"},
                      seeds=[{"name": "seed_dati", "csv": "id\n1\n", "description": "", "columns": [], "column_types": {"id": "BIGINT"}}])
    guida = files["INTEGRATION.md"]
    # the flow's tag AND tabularia's: a model of theirs tagged `sales` (and what it reads) is not ours
    assert "dbt build -s +tag:sales,tag:tabularia\n" in guida and "dbt build -s +tag:sales\n" not in guida
    # the seeds carry the tag too, so the selection still loads them
    assert "  - name: seed_dati\n    config:\n      tags: [\"tabularia\"]\n      column_types:\n" in files["seeds/tabularia_sales/schema.yml"]


def test_an_accepted_values_rule_tabularia_cannot_evaluate_is_refused_with_its_code():
    """On a boolean column Tabularia reads true/false/1/0 only: 'yes' makes its check fail the
    rule every time (it cannot evaluate it). The export says so instead of guessing."""
    from fastapi import HTTPException

    df = _pl.DataFrame({"ok": [True, False]})
    esito = _valuta(df.lazy(), {"rules": [{"id": "r1", "kind": "accepted_values", "column": "ok", "values": ["yes"], "severity": "error"}]})
    assert esito["rules"][0]["passed"] is False and "not a boolean" in esito["rules"][0]["error"]
    with pytest.raises(HTTPException) as e:
        _esporta_contratto(df, colonne=[{"name": "ok", "description": "", "tests": [{"kind": "accepted_values", "severity": "error", "values": ["yes"]}]}])
    assert e.value.status_code == 422 and e.value.detail["code"] == "contract_rule_invalid" and e.value.detail["params"] == {"rule": "accepted_values"}


# ── a text on several lines keeps its bytes (follow-up of the review) ────────────────────────
# The model's steps are indented line by line: the indentation went INSIDE a string literal or a
# quoted name that spans lines ('a\n  b' became 'a\n      b', and a filter on such a value kept
# no row). And dbt writes every line break of a model's file as `\n`: a `\r` in a value was lost.
_RIGHE = "a\n  b\r\n\tc\rd \u2028e"


def _sql_di(testo: str, dialetto: str) -> str:
    """`_RIGHE` as a string literal of the flow's text in `dialetto`, its bytes raw."""
    return "'" + testo.replace("'", "''") + "'" if dialetto != "bigquery" else "'''" + testo + "'''"


def _multiriga(target: str, dialetto: str, ops: list) -> str:
    """The model `uno` over the source `ordini` (id, t) as the route writes it for `target`."""
    smap = {"columns": ["id", "t"], "types": {"id": "Int64", "t": "String"}, "label": "ordini"}
    if target == "duckdb":
        return _files(_richiesta_export(source_map={"k": {"ref": "{{ source('db_1_v', 'ordini') }}", **smap}},
                                        models=[{"name": "uno", "source": {"key": "k"}, "operations": ops, "dialect": dialetto}]))["models/uno.sql"]
    voce = {"ident": "__s_0", "macro": "{{ source('db_1_v', 'ordini') }}", **smap}
    extra = {"clickhouse": dict(mode="clickhouse", clickhouse={"connections": [_PG], "target_schema": "dbt_tabularia"}),
             "native-pg": dict(mode="native", native=_PG_NATIVE), "native-ch": dict(mode="native", native=_NATIVO_CH)}[target]
    return _files(_richiesta_export(**extra, source_map={"k": voce},
                                    models=[{"name": "uno", "source": {"key": "k"}, "operations": ops, "dialect": dialetto}]))["models/uno.sql"]


def _righe_multiriga(target: str, testo: str) -> list:
    """The rows of the model, rendered as dbt renders it, on the warehouse of `target`, over
    ordini = (1, _RIGHE), (2, 'x')."""
    if target == "duckdb":
        import duckdb

        con = duckdb.connect()
        con.execute("CREATE TABLE ordini (id BIGINT, t VARCHAR)")
        con.execute("INSERT INTO ordini VALUES (1, ?), (2, 'x')", [_RIGHE])
        return con.execute(f"SELECT * FROM ({_render_like_dbt(testo, {('source', 'db_1_v', 'ordini'): 'ordini'}, config={})}) ORDER BY id").fetchall()
    if target == "native-pg":
        e = _RIGHE.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
        rel = f"(SELECT * FROM (VALUES (CAST(1 AS BIGINT), E'{e}'), (2, 'x')) AS v(id, t))"
        return sorted(_su_postgres(_render_like_dbt(testo, {("source", "db_1_v", "ordini"): rel}, config={})))
    e = _RIGHE.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    rel = f"(SELECT * FROM values('id Nullable(Int64), t Nullable(String)', (1, '{e}'), (2, 'x')))"
    return sorted(_su_clickhouse(_render_like_dbt(testo, {("source", "db_1_v", "ordini"): rel}, config={})))


@pytest.mark.parametrize("target,dialetto", [
    ("duckdb", "duckdb"), ("duckdb", "polars"), ("duckdb", "clickhouse"), ("duckdb", "bigquery"),
    ("native-pg", "duckdb"), ("native-pg", "clickhouse"),
    ("clickhouse", "duckdb"), ("clickhouse", "clickhouse"),
    ("native-ch", "duckdb"), ("native-ch", "clickhouse"),
])
def test_a_text_on_several_lines_keeps_its_bytes_on_every_target(target, dialetto):
    ops = [{"type": "compute", "params": {"columns": [{"name": "m", "expr": _sql_di(_RIGHE, dialetto)}]}},
           {"type": "filter", "params": {"column": "t", "operator": "eq", "value": _RIGHE}},
           {"type": "fill_null", "params": {"columns": {"m": _RIGHE}}},
           {"type": "sql", "params": {"query": f"SELECT *, {_sql_di(_RIGHE, dialetto)} AS n FROM self"}}]
    assert _righe_multiriga(target, _multiriga(target, dialetto, ops)) == [(1, _RIGHE, _RIGHE, _RIGHE)]


def test_a_name_on_several_lines_keeps_its_bytes():
    """A quoted name that spans lines (a column renamed in the flow) was indented inside too."""
    import duckdb

    nome = "r\n  s\r\nt"
    testo = _multiriga("duckdb", "duckdb", [{"type": "rename", "params": {"mapping": {"t": nome}}},
                                            {"type": "filter", "params": {"column": nome, "operator": "eq", "value": "x"}}])
    con = duckdb.connect()
    con.execute("CREATE TABLE ordini AS SELECT * FROM (VALUES (1, 'y'), (2, 'x')) v(id, t)")
    rel = con.sql(_render_like_dbt(testo, {("source", "db_1_v", "ordini"): "ordini"}, config={}))
    assert rel.columns == ["id", nome] and rel.fetchall() == [(2, "x")]


def test_a_yaml_value_ending_with_a_line_break_keeps_it():
    """dbt renders every YAML value as a Jinja template, and Jinja drops a template's final line
    break: a column named «Total⏎» (a spreadsheet header ending with Alt+Enter) was «Total» in
    schema.yml, and its not_null rule failed at `dbt build` (dbt 1.12.5: «Referenced column
    "Total" not found»). The final one is printed by an expression, as a `\\r` is."""
    import json

    nome = "Total\n"
    smap = {"k": {"ref": "{{ source('db_1_v', 'ordini') }}", "columns": ["id", "t"], "types": {"id": "Int64", "t": "String"}, "label": "ordini"}}
    files = _files(_richiesta_export(source_map=smap, models=[{
        "name": "uno", "source": {"key": "k"}, "operations": [{"type": "rename", "params": {"mapping": {"t": nome}}}],
        "description": "two lines\n\n", "meta": {"flow": "a\r\n"},
        "columns": [{"name": nome, "description": "", "tests": [{"kind": "not_null", "severity": "error"}]}]}]))
    righe = files["models/schema.yml"].splitlines()
    valore = lambda prefisso: [_render_like_dbt(json.loads(r[len(prefisso):])) for r in righe if r.startswith(prefisso)]  # noqa: E731
    assert valore("      - name: ") == [nome]
    assert valore("    description: ") == ["two lines\n\n"] and valore("          flow: ") == ["a\r\n"]
    assert d._y("a\nb") == '"a\\nb"'   # a line break that does not end the value is written as before


# A DuckDB escape string (E'…') in a compute or a sql node. sqlglot reads its escapes its own way
# (`\\` is a backslash, but `\q` stays `\q` where DuckDB reads `q`, `\101` stays where DuckDB reads
# `A`) and writes an escape string back without doubling the backslash, so the warehouse read `\ `
# as an escape: 'a\ b' became 'a b' on native Postgres, and in a sql node on the federated target too
# (sqlglot rewrites the query). The value must be the one DuckDB, Tabularia's engine, gives.
_ESCAPE_DUCKDB = "E'a\\\\ b\\nc\\t\\'d\\q\\101\\x42 ''e'"


@pytest.mark.parametrize("target", ["duckdb", "native-pg", "clickhouse", "native-ch"])
@pytest.mark.parametrize("dove", ["compute", "sql"])
def test_a_duckdb_escape_string_keeps_the_value_duckdb_gives_on_every_target(target, dove):
    import duckdb

    [(atteso,)] = duckdb.sql("SELECT " + _ESCAPE_DUCKDB).fetchall()
    assert atteso == "a\\ b\nc\t'dqAB 'e"
    passo = ({"type": "compute", "params": {"columns": [{"name": "m", "expr": _ESCAPE_DUCKDB}]}} if dove == "compute"
             else {"type": "sql", "params": {"query": f"SELECT *, {_ESCAPE_DUCKDB} AS m FROM self"}})
    ops = [{"type": "filter", "params": {"column": "t", "operator": "eq", "value": _RIGHE}}, passo]
    assert _righe_multiriga(target, _multiriga(target, "duckdb", ops)) == [(1, _RIGHE, atteso)]


@pytest.mark.parametrize("corpo", [
    "a\\\\b", "\\'", "''", "\\n\\t\\r\\b\\f", "\\v\\a\\q\\ \\\"", "\\101\\1018\\8", "\\x41\\x414\\x4\\x\\xZ",
    "\\\r\n\\\n", "città\\n", "-- no comment", "{{ x }}", "",
])
def test_an_escape_string_is_written_as_the_standard_string_duckdb_reads_it_as(corpo):
    import duckdb

    sql = f"SELECT E'{corpo}' AS a, 'x\\y' AS b, $$E'z'$$ AS c -- E'w'"
    scritto = d._stringhe_standard(sql)
    assert "E'" not in scritto.replace("$$E'z'$$", "").replace("-- E'w'", "")
    assert duckdb.sql(scritto).fetchall() == duckdb.sql(sql).fetchall()


def test_an_escape_string_duckdb_refuses_is_left_as_it_is():
    """A byte outside ASCII or a unicode escape: DuckDB refuses it (the flow does not run in
    Tabularia either), the export does not invent a value."""
    for corpo in ("\\xe8", "\\0", "\\u0041"):
        assert d._stringhe_standard(f"SELECT E'{corpo}'") == f"SELECT E'{corpo}'"


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
def _con_nomi(mode: str, schema=None, alias=None, tabella="ordini", target_schema="dbt_tabularia", materialized="table",
              schema_sorgente="v"):
    voce = {"columns": ["id"], "types": {"id": "Int64"}, "label": tabella}
    sorgenti = [{"name": "db_1_v", "database": "d", "schema": schema_sorgente, "connection_id": 1,
                 "tables": [{"name": tabella, "description": "", "columns": []}]}]
    modello = {"name": "uno", "source": {"key": "k"}, "operations": [], "materialized": materialized, "schema": schema, "alias": alias}
    if mode == "duckdb":
        return _richiesta_export(sources=sorgenti, source_map={"k": {"ref": "{{ source('db_1_v', 'x') }}", **voce}}, models=[modello])
    smap = {"k": {"ident": "__s_0", "macro": "{{ source('db_1_v', 'x') }}", **voce}}
    if mode == "clickhouse":
        return _richiesta_export(mode="clickhouse", sources=sorgenti, source_map=smap, models=[modello],
                                 clickhouse={"connections": [_PG], "target_schema": target_schema})
    native = {**(_PG_NATIVE if mode == "native-pg" else _NATIVO_CH), "target_schema": target_schema}
    return _richiesta_export(mode="native", native=native, sources=sorgenti, source_map=smap, models=[modello])


@pytest.mark.parametrize("mode,kw,nome,carattere,adapter", [
    ("native-pg", {"alias": 'o"x'}, 'o"x', '"', "dbt-postgres"),
    ("native-pg", {"alias": "o'brien", "materialized": "incremental"}, "o'brien", "'", "dbt-postgres"),
    ("native-pg", {"alias": "o'brien"}, "o'brien", "'", "dbt-postgres"),
    ("native-pg", {"schema": "zz'q", "alias": "t"}, "zz'q", "'", "dbt-postgres"),
    ("native-pg", {"schema": "zz\\b", "alias": "t"}, "zz\\b", "\\", "dbt-postgres"),
    ("native-pg", {"target_schema": "a'b"}, "a'b", "'", "dbt-postgres"),
    ("native-pg", {"tabella": 'or"dini'}, 'or"dini', '"', "dbt-postgres"),
    ("native-ch", {"alias": "o'brien"}, "o'brien", "'", "dbt-clickhouse"),
    ("native-ch", {"schema": "zz'q", "alias": "t"}, "zz'q", "'", "dbt-clickhouse"),
    ("native-ch", {"alias": "a\\b"}, "a\\b", "\\", "dbt-clickhouse"),
    ("native-ch", {"alias": "a`b"}, "a`b", "`", "dbt-clickhouse"),
    ("native-ch", {"tabella": "or`dini"}, "or`dini", "`", "dbt-clickhouse"),
    ("clickhouse", {"tabella": "or\\dini"}, "or\\dini", "\\", "dbt-clickhouse"),
    ("clickhouse", {"target_schema": "a`b"}, "a`b", "`", "dbt-clickhouse"),
    ("duckdb", {"tabella": 'or"dini'}, 'or"dini', '"', "dbt-duckdb"),
    # a quote in a SOURCE's schema or table: `dbt build` passes, but `dbt docs generate` writes them
    # between '…' in its catalog query (dbt 1.12.5: «upper('o'tab')» a syntax error on all three)
    ("native-pg", {"tabella": "o'brien"}, "o'brien", "'", "dbt-postgres"),
    ("native-pg", {"schema_sorgente": "zz'src"}, "zz'src", "'", "dbt-postgres"),
    ("duckdb", {"tabella": "o'brien"}, "o'brien", "'", "dbt-duckdb"),
    ("duckdb", {"schema_sorgente": "zz'src"}, "zz'src", "'", "dbt-duckdb"),
    ("native-ch", {"tabella": "o'brien"}, "o'brien", "'", "dbt-clickhouse"),
    ("native-ch", {"schema_sorgente": "zz'src"}, "zz'src", "'", "dbt-clickhouse"),
    ("clickhouse", {"tabella": "o'brien"}, "o'brien", "'", "dbt-clickhouse"),
])
def test_a_name_the_adapter_cannot_write_is_refused_by_the_engine_too(mode, kw, nome, carattere, adapter):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as e:
        _files(_con_nomi(mode, **kw))
    assert e.value.status_code == 422 and e.value.detail["code"] == "name_not_quotable"
    assert e.value.detail["params"] == {"name": nome, "character": carattere, "adapter": adapter}


@pytest.mark.parametrize("mode,kw", [
    ("native-pg", {"alias": "a\\b"}), ("native-pg", {"alias": "a\\b", "materialized": "incremental"}),
    ("native-pg", {"alias": "a }} b {{ c", "schema": "città"}), ("native-pg", {"tabella": "o\\brien", "schema_sorgente": "zz\\b"}),
    ("native-ch", {"alias": 'x"y', "schema": 'zz"q'}), ("native-ch", {"tabella": 'o"brien'}),
])
def test_the_names_the_adapter_builds_are_kept(mode, kw):
    """Built twice by real dbt 1.12.5, then `dbt docs generate`. A SOURCE's name is quoted by
    source(), but a quote in it broke docs generate (see the refusals above): a backslash in a
    Postgres source's schema and table, a double quote in a ClickHouse source's table are kept
    (their catalog query compares with `=`, and Postgres reads '…' with standard_conforming_strings)."""
    files = _files(_con_nomi(mode, **kw))
    config: dict = {}
    _render_like_dbt(files["models/uno.sql"].splitlines()[0], config=config)
    assert config.get("alias") == kw.get("alias") and config.get("schema") == kw.get("schema")


# ── the probe's cost (review follow-up) ──────────────────────────────────────────────────────
# Each step is an EMPTY TABLE the next one binds against alone. As a view, every step re-bound the
# whole chain before it: the export time grew with the square of the steps (measured through the
# route, 100 steps over 300 columns: see the follow-up report). A guard that the probe keeps them so.
def test_the_probe_keeps_each_step_as_an_empty_table(monkeypatch):
    viste: list = []
    chiudi = d._Sonda.chiudi

    def guarda(self):
        if self._con is not None:
            viste.extend(self._con.execute("SELECT table_name, 'BASE TABLE', estimated_size FROM duckdb_tables() "
                                           "UNION ALL SELECT view_name, 'VIEW', 0 FROM duckdb_views() WHERE NOT internal").fetchall())
        chiudi(self)
    monkeypatch.setattr(d._Sonda, "chiudi", guarda)
    cols = [f"c{i}" for i in range(30)]
    ops = [op for k in range(10) for op in (
        {"type": "compute", "params": {"columns": [{"name": f"k{k}", "expr": f'"c{k}" * 2'}]}},
        {"type": "filter", "params": {"column": f"c{k + 1}", "operator": "gt", "value": 0}})]
    _, _, tipi = d.compile_model_sql({"key": "k"}, ops, lambda _: ("t_src", cols, {c: "BIGINT" for c in cols}, "src"))
    assert len(viste) == 1 + len(ops) and {v[1] for v in viste} == {"BASE TABLE"} and {v[2] for v in viste} == {0}, viste
    assert all(tipi[f"k{k}"] == "BIGINT" for k in range(10))   # what the probe reads, from the tables
