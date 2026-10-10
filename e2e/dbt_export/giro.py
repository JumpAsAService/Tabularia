"""Il giro dbt completo sullo stack di scarto, su due flussi costruiti apposta:
  · «DBT base» (a monte): orders → filter → compute → [group_by → datasource «DBT base»]
    e [→ tabella Postgres «dbt_righe» in append]: i passi condivisi diventano un modello
    intermedio, la tabella in append un modello incremental;
  · «DBT finale» (a valle): la datasource «DBT base» ⋈ un FILE caricato nell'editor
    (→ seed) → datasource «DBT finale» con un CONTRATTO (→ test dbt); più una
    datasource da QUERY → group_by → datasource «DBT stati» (→ modello ephemeral).
Esporta il flusso a valle (federato e nativo: porta dentro anche il flusso a monte),
lancia dbt seed/run/test VERO, confronta ogni modello con la datasource pubblicata,
guarda la struttura del progetto. Toglie tutto alla fine. uso: python3 giro.py"""
import json, subprocess, sys, time
from ambiente import PASSWORD_DA
from comune import api, confronta, dbt, entra, esiti, esporta, ok, password_delle_connessioni, righe_della_datasource

entra()
ds = {d["name"]: d for d in api("GET", "/datasources")[1]}
conn = {c["name"]: c for c in api("GET", "/connections")[1]}
pg = conn["Sample ERP (Postgres)"]
cartella_id = ds["orders"]["project_id"]
creati = {"flussi": [], "ds": []}


def crea_flusso(nome, definizione, descrizione):
    """Con il primo motore che l'amministratore ammette."""
    for motore in ("polars", "duckdb", "clickhouse"):
        st, f = api("POST", f"/projects/{cartella_id}/flows", {"name": nome, "definition": json.dumps(definizione), "engine": motore, "description": descrizione})
        if st != 422 or "disabilitato" not in str(f):
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


def pulisci():
    for fid in creati["flussi"]:
        api("DELETE", f"/flows/{fid}")
    for d in api("GET", "/datasources")[1]:
        if d["name"].startswith("DBT ") or d["id"] in creati["ds"]:
            api("DELETE", f"/datasources/{d['id']}")
    cont = PASSWORD_DA["postgresql"][0]
    subprocess.run(["docker", "exec", cont, "sh", "-c", 'psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DROP SCHEMA IF EXISTS dbt_tabularia CASCADE; DROP TABLE IF EXISTS dbt_righe"'], capture_output=True)


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
    canali_veri = sorted({r["canale"] for r in righe_della_datasource(base)})
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
    canali = sorted({r["canale"] for r in righe_della_datasource(finale)})
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
    generici = sum(1 for k in salvate if k in ("not_null", "unique", "accepted_values"))
    singolari = sum(1 for k in salvate if k in ("range", "pattern", "row_count", "expression"))
    st, d = api("PATCH", f"/datasources/{finale['id']}", {"column_descriptions": {"ordini": "Orders shipped on the channel"}})
    ok("descrizione di colonna messa", st == 200, (st, d))

    variabili = password_delle_connessioni()
    for target in ("duckdb", "native"):
        print(f"\n== export {target} ==")
        cartella, nomi = esporta(fb, target)
        if not ok("l'export risponde con uno zip", cartella is not None, nomi):
            continue
        print("  file:", ", ".join(nomi))
        attesi = ["models/dbt_base.sql", "models/dbt_righe.sql", "models/int_anno.sql", "models/src_dbt_ordini_evasi.sql", "models/dbt_finale.sql",
                  "models/dbt_stati.sql", "models/schema.yml", "models/sources.yml", "seeds/seed_canali.csv", "seeds/schema.yml",
                  "tests/dbt_finale__range_r4.sql", "tests/dbt_finale__row_count_r5.sql", "tests/dbt_finale__pattern_r6.sql", "tests/dbt_finale__expression_r7.sql", "README.md"]
        ok("il progetto ha i modelli a monte, l'intermedio, la query, il seed, i test singolari", all(a in nomi for a in attesi), [a for a in attesi if a not in nomi])
        leggi = lambda f: open(f"{cartella}/{f}").read()
        ok("i passi condivisi sono un modello ephemeral e le uscite lo referenziano",
           "materialized='ephemeral'" in leggi("models/int_anno.sql") and "{{ ref('int_anno') }}" in leggi("models/dbt_base.sql") and "{{ ref('int_anno') }}" in leggi("models/dbt_righe.sql"))
        ok("la tabella in append è un modello incremental" + (" nella tabella di destinazione" if target == "native" else ""),
           "materialized='incremental', incremental_strategy='append'" in leggi("models/dbt_righe.sql") and (target != "native" or "alias='dbt_righe'" in leggi("models/dbt_righe.sql")))
        ok("il flusso a valle legge il flusso a monte con ref() e il file con il seed",
           "{{ ref('dbt_base') }}" in leggi("models/dbt_finale.sql") and "{{ ref('seed_canali') }}" in leggi("models/dbt_finale.sql"))
        atteso_query = "postgres_query('db_%d'" % pg["id"] if target == "duckdb" else "SELECT id, canale, stato FROM"
        ok("la datasource da query è un modello ephemeral con la query", "ephemeral" in leggi("models/src_dbt_ordini_evasi.sql") and atteso_query in leggi("models/src_dbt_ordini_evasi.sql") and "{{ ref('src_dbt_ordini_evasi') }}" in leggi("models/dbt_stati.sql"))
        schema = leggi("models/schema.yml")
        ok("schema.yml: descrizioni e test generici del contratto con le severità",
           "Orders shipped on the channel" in schema and "- not_null:\n              config: {severity: error}" in schema and "- unique:" in schema
           and "- accepted_values:\n              arguments:\n                values: [" in schema and "config: {severity: warn}" in schema and "channels we promise" in schema
           and (("- unique:" in schema) == ("unique" in salvate)), schema[:600])
        ok("sources.yml ha la tabella orders con la descrizione delle colonne", "name: ordini" in leggi("models/sources.yml") and "columns:" in leggi("models/sources.yml"))
        ok("il README avvisa della freshness e dei seed", "freshness" in leggi("README.md") and "seed" in leggi("README.md"))
        rc, out = dbt(cartella, "seed", variabili=variabili)
        ok("dbt seed passa", rc == 0 and "Completed successfully" in out, out[-1500:])
        rc, out = dbt(cartella, "run", variabili=variabili)
        if not ok("dbt run passa (6 modelli, 2 ephemeral)", rc == 0 and "Completed successfully" in out, out[-2500:]):
            continue
        rc, out = dbt(cartella, "test", variabili=variabili)
        ok(f"dbt test passa: {generici} test generici + {singolari} singolari", rc == 0 and f"PASS={generici + singolari}" in out, out[-2500:])
        for nome in ("DBT base", "DBT finale", "DBT stati"):
            confronta(cartella, nome.lower().replace(" ", "_"), aspetta_datasource(nome), variabili)
finally:
    pulisci()
print(f"\n{sum(esiti)}/{len(esiti)} verifiche passate")
sys.exit(0 if all(esiti) else 1)
