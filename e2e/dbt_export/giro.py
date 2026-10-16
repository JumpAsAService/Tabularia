"""Il giro dbt completo sullo stack di scarto, su due flussi costruiti apposta:
  · «DBT base» (a monte): orders → filter → compute → [group_by → datasource «DBT base»]
    e [→ tabella Postgres «dbt_righe» in append]: i passi condivisi diventano un modello
    intermedio, la tabella in append un modello incremental;
  · «DBT finale» (a valle): la datasource «DBT base» ⋈ un FILE caricato nell'editor
    (→ seed) → datasource «DBT finale» con un CONTRATTO (→ test dbt); più una
    datasource da QUERY → group_by → datasource «DBT stati» (→ modello ephemeral).
Esporta il flusso a valle (federato e nativo: porta dentro il SOLO ramo del flusso a monte
che legge, decisione 4), lancia dbt seed/run/test VERO, confronta ogni modello con la
datasource pubblicata, guarda la struttura del progetto. Then it exports the upstream flow
itself: the shared steps are an ephemeral model, and on native the append model writes the
very table Tabularia wrote (<connection schema>.dbt_righe): one dbt run doubles its rows.
Toglie tutto alla fine. uso: python3 giro.py"""
import json, sys, time
from comune import api, confronta, dbt, entra, esiti, esporta, ok, password_delle_connessioni, postgres_esempio, righe_della_datasource

entra()
ds = {d["name"]: d for d in api("GET", "/datasources")[1]}
conn = {c["name"]: c for c in api("GET", "/connections")[1]}
pg = conn["Sample ERP (Postgres)"]
cartella_id = ds["orders"]["project_id"]
creati = {"flussi": [], "ds": []}


def motori_ammessi():
    """The engines the administrator allows (Admin → Engines), in our order of preference."""
    st, politica = api("GET", "/engine-policy")
    vietati = {m["engine_id"] for m in politica if not m.get("allowed")} if st == 200 and isinstance(politica, list) else set()
    return [m for m in ("polars", "duckdb", "clickhouse") if m not in vietati] or ["polars", "duckdb", "clickhouse"]


def crea_flusso(nome, definizione, descrizione):
    """Con il primo motore che l'amministratore ammette (the gateway's refusal is in English
    since c054933: the policy is read first, and the text is only the fallback)."""
    for motore in motori_ammessi():
        st, f = api("POST", f"/projects/{cartella_id}/flows", {"name": nome, "definition": json.dumps(definizione), "engine": motore, "description": descrizione})
        if st != 422 or "has been disabled" not in str(f):
            return st, f
    return st, f


def aspetta_run(rid, secondi=300):
    fine = time.time() + secondi
    while time.time() < fine:
        st, r = api("GET", f"/runs/{rid}")
        if st == 200 and r["status"] in ("SUCCESS", "FAILURE"):
            return r
        time.sleep(2)
    return r


def aspetta_datasource(nome, secondi=120):
    fine = time.time() + secondi
    while time.time() < fine:
        d = next((x for x in api("GET", "/datasources")[1] if x["name"] == nome), None)
        if d and d.get("rows") and not d.get("refreshing"):
            return d
        time.sleep(2)
    return None


# Tabularia writes the one-part table «dbt_righe» in the connection's schema
# (db_destination._qualified_table): «vendite» on the sample ERP, else public
SCHEMA_RIGHE = pg.get("db_schema") or "public"


def pulisci():
    for fid in creati["flussi"]:
        api("DELETE", f"/flows/{fid}")
    for d in api("GET", "/datasources")[1]:
        if d["name"].startswith("DBT ") or d["id"] in creati["ds"]:
            api("DELETE", f"/datasources/{d['id']}")
    # the qualified table: a bare «dbt_righe» resolved to public.dbt_righe (search_path) and left
    # «vendite».dbt_righe growing at every round (#78)
    postgres_esempio(f'DROP SCHEMA IF EXISTS dbt_tabularia CASCADE; DROP TABLE IF EXISTS "{SCHEMA_RIGHE}".dbt_righe;')


def righe_di_dbt_righe():
    r = postgres_esempio(f'SELECT count(*) FROM "{SCHEMA_RIGHE}".dbt_righe')
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


pulisci()
try:
    # ── una datasource da QUERY ─────────────────────────────────────────────
    st, sqlds = api("POST", f"/projects/{cartella_id}/datasources/database", {
        "name": "DBT ordini evasi", "connection_id": pg["id"], "source_type": "sql",
        "source_ref": f"SELECT id, canale, stato FROM {ds['orders']['source_ref']} WHERE stato = 'evaso'"})
    if not ok("creata una datasource da query", st in (200, 201), (st, sqlds)):
        raise SystemExit(1)
    creati["ds"].append(sqlds["id"])
    ok("la datasource da query ha i dati", aspetta_datasource("DBT ordini evasi") is not None)

    # ── flusso a monte: passi condivisi + tabella in append ─────────────────
    monte = {"nodes": [
        {"id": "src", "type": "source", "data": {"datasourceId": ds["orders"]["id"]}},
        {"id": "evasi", "type": "operation", "data": {"opType": "filter", "params": {"column": "stato", "operator": "eq", "value": "evaso"}}},
        {"id": "anno", "type": "operation", "data": {"opType": "compute", "params": {"columns": [{"name": "anno", "expr": "date_part('year', data_ordine)"}]}}},
        {"id": "per_canale", "type": "operation", "data": {"opType": "group_by", "params": {"by": ["canale"], "aggregations": [{"column": "id", "func": "count", "alias": "ordini"}]}}},
        {"id": "o1", "type": "output", "data": {"destType": "datasource", "name": "DBT base", "projectId": cartella_id, "overwrite": True}},
        {"id": "o2", "type": "output", "data": {"destType": "database", "connectionId": pg["id"], "table": "dbt_righe", "mode": "append"}},
    ], "edges": [{"source": "src", "target": "evasi", "targetHandle": "left"}, {"source": "evasi", "target": "anno", "targetHandle": "left"},
                 {"source": "anno", "target": "per_canale", "targetHandle": "left"}, {"source": "per_canale", "target": "o1", "targetHandle": "left"},
                 {"source": "anno", "target": "o2", "targetHandle": "left"}]}
    st, fa = crea_flusso("DBT base", monte, "Orders shipped, per channel")
    if not ok("creato il flusso a monte", st in (200, 201), (st, fa)):
        raise SystemExit(1)
    creati["flussi"].append(fa["id"])
    run = aspetta_run(api("POST", f"/flows/{fa['id']}/run-now?mode=production")[1]["run_id"])
    ok("il flusso a monte è andato a buon fine (datasource + tabella)", run["status"] == "SUCCESS", run)
    base = aspetta_datasource("DBT base")
    ok("la datasource «DBT base» è pubblicata", base is not None)

    # ── un file caricato nell'editor (→ seed): i canali VERI, con una descrizione ──
    canali_veri = sorted({r["canale"] for r in righe_della_datasource(base) if r["canale"] is not None})
    csv = "canale,descrizione\n" + "\n".join(f"{c},Canale {c}" for c in canali_veri)
    st, up = api("POST", "/files", multipart={"file": ("canali.csv", csv.encode(), "text/csv")})
    if not ok(f"file caricato (come dall'editor), {len(canali_veri)} canali", st in (200, 201) and up.get("parquet_key"), (st, up)):
        raise SystemExit(1)
    nodo_file = {"id": "file", "type": "source", "data": {"parquetKey": up["parquet_key"], "bucket": ds["orders"]["bucket"], "filename": "canali.csv",
                                                           "columns": [{"name": "canale", "dtype": "String"}, {"name": "descrizione", "dtype": "String"}]}}

    # ── flusso a valle: flusso a monte ⋈ file, query → group_by ─────────────
    valle = {"nodes": [
        {"id": "s_base", "type": "source", "data": {"datasourceId": base["id"]}},
        nodo_file,
        {"id": "j", "type": "operation", "data": {"opType": "join", "params": {"how": "inner", "on": "canale"}}},
        {"id": "o1", "type": "output", "data": {"destType": "datasource", "name": "DBT finale", "projectId": cartella_id, "overwrite": True}},
        {"id": "s_sql", "type": "source", "data": {"datasourceId": sqlds["id"]}},
        {"id": "per_stato", "type": "operation", "data": {"opType": "group_by", "params": {"by": ["stato"], "aggregations": [{"column": "id", "func": "count", "alias": "n"}]}}},
        {"id": "o2", "type": "output", "data": {"destType": "datasource", "name": "DBT stati", "projectId": cartella_id, "overwrite": True}},
    ], "edges": [{"source": "s_base", "target": "j", "targetHandle": "left"}, {"source": "file", "target": "j", "targetHandle": "right"},
                 {"source": "j", "target": "o1", "targetHandle": "left"}, {"source": "s_sql", "target": "per_stato", "targetHandle": "left"},
                 {"source": "per_stato", "target": "o2", "targetHandle": "left"}]}
    st, fb = crea_flusso("DBT finale", valle, "Channels with their description")
    if not ok("creato il flusso a valle", st in (200, 201), (st, fb)):
        raise SystemExit(1)
    creati["flussi"].append(fb["id"])
    run = aspetta_run(api("POST", f"/flows/{fb['id']}/run-now?mode=production")[1]["run_id"])
    ok("il flusso a valle è andato a buon fine", run["status"] == "SUCCESS", run)
    finale = aspetta_datasource("DBT finale"); stati = aspetta_datasource("DBT stati")
    ok("le datasource «DBT finale» e «DBT stati» sono pubblicate", finale is not None and stati is not None)

    # ── un contratto sulla datasource finale (→ test dbt) ───────────────────
    canali = sorted({r["canale"] for r in righe_della_datasource(finale) if r["canale"] is not None})
    regole = [{"kind": "not_null", "column": "canale", "severity": "error"},
              {"kind": "unique", "column": "canale", "severity": "error"},
              {"kind": "accepted_values", "column": "canale", "values": canali, "severity": "warning"},
              {"kind": "range", "column": "ordini", "min": 1, "severity": "error"},
              {"kind": "row_count", "min": 1, "max": 1000, "severity": "warning"},
              {"kind": "pattern", "column": "descrizione", "regex": "^Canale ", "severity": "error"},
              {"kind": "expression", "sql": "ordini >= 1", "severity": "warning"},
              {"kind": "freshness", "max_age_hours": 48, "severity": "warning"}]
    st, c = api("PUT", f"/datasources/{finale['id']}/contract", {"document": {"description": "channels we promise", "rules": regole}, "enabled": True})
    ok("contratto messo sulla datasource finale", st == 200, (st, c))
    salvate = [r["kind"] for r in (api("GET", f"/datasources/{finale['id']}/contract")[1] or {}).get("document", {}).get("rules", [])]
    print("  regole salvate:", salvate)
    # only not_null stays a generic test: one-column unique and accepted_values are singular (#34 #35)
    generici = sum(1 for k in salvate if k == "not_null")
    singolari = sum(1 for k in salvate if k in ("unique", "accepted_values", "range", "pattern", "row_count", "expression"))
    st, d = api("PATCH", f"/datasources/{finale['id']}", {"column_descriptions": {"ordini": "Orders shipped on the channel"}})
    ok("descrizione di colonna messa", st == 200, (st, d))

    variabili = password_delle_connessioni()
    for target in ("duckdb", "native"):
        print(f"\n== export {target} of «DBT finale» ==")
        cartella, nomi = esporta(fb, target)
        if not ok("l'export risponde con uno zip", cartella is not None, nomi):
            continue
        print("  file:", ", ".join(nomi))
        attesi = ["models/dbt_base.sql", "models/src_dbt_ordini_evasi.sql", "models/dbt_finale.sql",
                  "models/dbt_stati.sql", "models/schema.yml", "models/sources.yml", "seeds/seed_canali.csv", "seeds/schema.yml",
                  "tests/dbt_finale__unique_canale.sql", "tests/dbt_finale__accepted_values_canale.sql",
                  "tests/dbt_finale__range_r4.sql", "tests/dbt_finale__row_count_r5.sql", "tests/dbt_finale__pattern_r6.sql", "tests/dbt_finale__expression_r7.sql", "README.md"]
        ok("il progetto ha il modello a monte letto, la query, il seed, i test singolari", all(a in nomi for a in attesi), [a for a in attesi if a not in nomi])
        # decision 4: only the branch read downstream comes from the upstream flow — not its append
        # output, and so no step shared with it either
        ok("the upstream flow brings only the branch read: no dbt_righe, no int_anno",
           "models/dbt_righe.sql" not in nomi and "models/int_anno.sql" not in nomi, [n for n in nomi if "righe" in n or "int_" in n])
        leggi = lambda f: open(f"{cartella}/{f}").read()
        ok("il flusso a valle legge il flusso a monte con ref() e il file con il seed",
           "{{ ref('dbt_base') }}" in leggi("models/dbt_finale.sql") and "{{ ref('seed_canali') }}" in leggi("models/dbt_finale.sql"))
        ok("the upstream model lives in the project, never in an upstream destination table", "alias=" not in leggi("models/dbt_base.sql"))
        atteso_query = "postgres_query('db_%d'" % pg["id"] if target == "duckdb" else "SELECT id, canale, stato FROM"
        ok("la datasource da query è un modello ephemeral con la query", "ephemeral" in leggi("models/src_dbt_ordini_evasi.sql") and atteso_query in leggi("models/src_dbt_ordini_evasi.sql") and "{{ ref('src_dbt_ordini_evasi') }}" in leggi("models/dbt_stati.sql"))
        schema = leggi("models/schema.yml")
        # not_null is dbt's generic test, with the column quoted; one-column unique and accepted_values are
        # singular tests that count NULLs and compare values as Tabularia's check does (#33 #34 #35)
        ok("schema.yml: descrizioni e not_null del contratto (colonna quotata), unique e accepted_values come test singolari",
           "Orders shipped on the channel" in schema and "- not_null:\n              config: {severity: error}" in schema and "quote: true" in schema
           and "- unique:" not in schema and "- accepted_values:" not in schema and "channels we promise" in schema, schema[:600])
        ok("the singular accepted_values test keeps the rule's severity (warning)",
           "config(severity='warn')" in leggi("tests/dbt_finale__accepted_values_canale.sql"), leggi("tests/dbt_finale__accepted_values_canale.sql")[:300])
        ok("sources.yml ha la tabella orders con la descrizione delle colonne", "name: ordini" in leggi("models/sources.yml") and "columns:" in leggi("models/sources.yml"))
        ok("il README avvisa della freshness e dei seed", "freshness" in leggi("README.md") and "seed" in leggi("README.md"))
        rc, out = dbt(cartella, "seed", variabili=variabili)
        ok("dbt seed passa", rc == 0 and "Completed successfully" in out, out[-1500:])
        rc, out = dbt(cartella, "run", variabili=variabili)
        if not ok("dbt run passa (4 modelli, 1 ephemeral)", rc == 0 and "Completed successfully" in out, out[-2500:]):
            continue
        rc, out = dbt(cartella, "test", variabili=variabili)
        ok(f"dbt test passa: {generici} test generici + {singolari} singolari", rc == 0 and f"PASS={generici + singolari}" in out, out[-2500:])
        for nome in ("DBT base", "DBT finale", "DBT stati"):
            confronta(cartella, nome.lower().replace(" ", "_"), aspetta_datasource(nome), variabili)

    # ── the upstream flow exported by itself: shared steps, the append table ──
    for target in ("duckdb", "native"):
        print(f"\n== export {target} of «DBT base» ==")
        cartella, nomi = esporta(fa, target, suffisso="_monte")
        if not ok("l'export del flusso a monte risponde con uno zip", cartella is not None, nomi):
            continue
        leggi = lambda f: open(f"{cartella}/{f}").read()
        attesi = ["models/dbt_base.sql", "models/dbt_righe.sql", "models/int_anno.sql"]
        ok("il flusso a monte ha le sue due uscite e l'intermedio", all(a in nomi for a in attesi), [a for a in attesi if a not in nomi])
        ok("i passi condivisi sono un modello ephemeral e le uscite lo referenziano",
           "materialized='ephemeral'" in leggi("models/int_anno.sql") and "{{ ref('int_anno') }}" in leggi("models/dbt_base.sql") and "{{ ref('int_anno') }}" in leggi("models/dbt_righe.sql"))
        testa = leggi("models/dbt_righe.sql")
        # #19: a one-part table name lands where Tabularia writes it, the connection's schema
        ok("la tabella in append è un modello incremental" + (f" nella tabella di destinazione «{SCHEMA_RIGHE}».dbt_righe" if target == "native" else ""),
           "materialized='incremental', incremental_strategy='append'" in testa
           and (target != "native" or (f"schema='{SCHEMA_RIGHE}'" in testa and "alias='dbt_righe'" in testa)), testa[:400])
        prima = righe_di_dbt_righe()
        rc, out = dbt(cartella, "run", variabili=variabili)
        if not ok("dbt run passa sul flusso a monte (2 modelli, 1 ephemeral)", rc == 0 and "Completed successfully" in out, out[-2500:]):
            continue
        if target == "native":
            # #78: compare the rows, not the alias string — dbt appends into the very table Tabularia wrote
            dopo = righe_di_dbt_righe()
            altrove = postgres_esempio("SELECT to_regclass('dbt_tabularia.dbt_righe') IS NULL").stdout.strip()
            ok(f"native: dbt appends into «{SCHEMA_RIGHE}».dbt_righe, the table Tabularia wrote ({prima} rows → {dopo})",
               prima and dopo == 2 * prima and altrove == "t", (prima, dopo, altrove))
        confronta(cartella, "dbt_base", aspetta_datasource("DBT base"), variabili)
finally:
    pulisci()
print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
sys.exit(0 if all(esiti) else 1)
