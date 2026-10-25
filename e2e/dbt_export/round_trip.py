"""The complete dbt round on the throwaway stack, on two flows built on purpose:
  · «DBT base» (upstream): orders → filter → compute → [group_by → datasource «DBT base»]
    and [→ Postgres table «dbt_righe» in append]: the shared steps become an intermediate
    model, the append table an incremental model;
  · «DBT finale» (downstream): the datasource «DBT base» ⋈ a FILE uploaded in the editor
    (→ seed) → datasource «DBT finale» with a CONTRACT (→ dbt tests); plus a datasource
    from a QUERY → group_by → datasource «DBT stati» (→ ephemeral model).
Exports the downstream flow (federated and native: it brings in ONLY the branch of the
upstream flow it reads, decision 4), runs a REAL dbt seed/run/test, compares every model
with the published datasource, looks at the project's structure. Then it exports the upstream flow
itself: the shared steps are an ephemeral model, and on native the append model writes the
very table Tabularia wrote (<connection schema>.dbt_righe): one dbt run doubles its rows.
Removes everything at the end. usage: python3 round_trip.py"""
import json, sys, time
from common import api, compare, dbt, sign_in, outcomes, export, ok, connection_passwords, sample_postgres, datasource_rows

sign_in()
ds = {d["name"]: d for d in api("GET", "/datasources")[1]}
conn = {c["name"]: c for c in api("GET", "/connections")[1]}
pg = conn["Sample ERP (Postgres)"]
folder_id = ds["orders"]["project_id"]
created = {"flows": [], "ds": []}


def allowed_engines():
    """The engines the administrator allows (Admin → Engines), in our order of preference."""
    st, policy = api("GET", "/engine-policy")
    forbidden = {m["engine_id"] for m in policy if not m.get("allowed")} if st == 200 and isinstance(policy, list) else set()
    return [m for m in ("polars", "duckdb", "clickhouse") if m not in forbidden] or ["polars", "duckdb", "clickhouse"]


def create_flow(name, definition, description):
    """With the first engine the administrator allows (the gateway's refusal is in English
    since c054933: the policy is read first, and the text is only the fallback)."""
    for engine in allowed_engines():
        st, f = api("POST", f"/projects/{folder_id}/flows", {"name": name, "definition": json.dumps(definition), "engine": engine, "description": description})
        if st != 422 or "has been disabled" not in str(f):
            return st, f
    return st, f


def wait_run(rid, seconds=300):
    end = time.time() + seconds
    while time.time() < end:
        st, r = api("GET", f"/runs/{rid}")
        if st == 200 and r["status"] in ("SUCCESS", "FAILURE"):
            return r
        time.sleep(2)
    return r


def wait_datasource(name, seconds=120):
    end = time.time() + seconds
    while time.time() < end:
        d = next((x for x in api("GET", "/datasources")[1] if x["name"] == name), None)
        if d and d.get("rows") and not d.get("refreshing"):
            return d
        time.sleep(2)
    return None


# Tabularia writes the one-part table «dbt_righe» in the connection's schema
# (db_destination._qualified_table): «vendite» on the sample ERP, else public
ROWS_SCHEMA = pg.get("db_schema") or "public"


def clean():
    for fid in created["flows"]:
        api("DELETE", f"/flows/{fid}")
    for d in api("GET", "/datasources")[1]:
        if d["name"].startswith("DBT ") or d["id"] in created["ds"]:
            api("DELETE", f"/datasources/{d['id']}")
    # the qualified table: a bare «dbt_righe» resolved to public.dbt_righe (search_path) and left
    # «vendite».dbt_righe growing at every round (#78)
    sample_postgres(f'DROP SCHEMA IF EXISTS dbt_tabularia CASCADE; DROP TABLE IF EXISTS "{ROWS_SCHEMA}".dbt_righe;')


def dbt_righe_count():
    r = sample_postgres(f'SELECT count(*) FROM "{ROWS_SCHEMA}".dbt_righe')
    return int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip().isdigit() else None


clean()
try:
    # ── a datasource from a QUERY ───────────────────────────────────────────
    st, sqlds = api("POST", f"/projects/{folder_id}/datasources/database", {
        "name": "DBT ordini evasi", "connection_id": pg["id"], "source_type": "sql",
        "source_ref": f"SELECT id, canale, stato FROM {ds['orders']['source_ref']} WHERE stato = 'evaso'"})
    if not ok("a datasource from a query created", st in (200, 201), (st, sqlds)):
        raise SystemExit(1)
    created["ds"].append(sqlds["id"])
    ok("the datasource from a query has the data", wait_datasource("DBT ordini evasi") is not None)

    # ── upstream flow: shared steps + append table ──────────────────────────
    upstream = {"nodes": [
        {"id": "src", "type": "source", "data": {"datasourceId": ds["orders"]["id"]}},
        {"id": "evasi", "type": "operation", "data": {"opType": "filter", "params": {"column": "stato", "operator": "eq", "value": "evaso"}}},
        {"id": "anno", "type": "operation", "data": {"opType": "compute", "params": {"columns": [{"name": "anno", "expr": "date_part('year', data_ordine)"}]}}},
        {"id": "per_canale", "type": "operation", "data": {"opType": "group_by", "params": {"by": ["canale"], "aggregations": [{"column": "id", "func": "count", "alias": "ordini"}]}}},
        {"id": "o1", "type": "output", "data": {"destType": "datasource", "name": "DBT base", "projectId": folder_id, "overwrite": True}},
        {"id": "o2", "type": "output", "data": {"destType": "database", "connectionId": pg["id"], "table": "dbt_righe", "mode": "append"}},
    ], "edges": [{"source": "src", "target": "evasi", "targetHandle": "left"}, {"source": "evasi", "target": "anno", "targetHandle": "left"},
                 {"source": "anno", "target": "per_canale", "targetHandle": "left"}, {"source": "per_canale", "target": "o1", "targetHandle": "left"},
                 {"source": "anno", "target": "o2", "targetHandle": "left"}]}
    st, fa = create_flow("DBT base", upstream, "Orders shipped, per channel")
    if not ok("the upstream flow created", st in (200, 201), (st, fa)):
        raise SystemExit(1)
    created["flows"].append(fa["id"])
    run = wait_run(api("POST", f"/flows/{fa['id']}/run-now?mode=production")[1]["run_id"])
    ok("the upstream flow succeeded (datasource + table)", run["status"] == "SUCCESS", run)
    base = wait_datasource("DBT base")
    ok("the datasource «DBT base» is published", base is not None)

    # ── a file uploaded in the editor (→ seed): the REAL channels, with a description ──
    real_channels = sorted({r["canale"] for r in datasource_rows(base) if r["canale"] is not None})
    csv = "canale,descrizione\n" + "\n".join(f"{c},Canale {c}" for c in real_channels)
    st, up = api("POST", "/files", multipart={"file": ("canali.csv", csv.encode(), "text/csv")})
    if not ok(f"file uploaded (as from the editor), {len(real_channels)} channels", st in (200, 201) and up.get("parquet_key"), (st, up)):
        raise SystemExit(1)
    file_node = {"id": "file", "type": "source", "data": {"parquetKey": up["parquet_key"], "bucket": ds["orders"]["bucket"], "filename": "canali.csv",
                                                           "columns": [{"name": "canale", "dtype": "String"}, {"name": "descrizione", "dtype": "String"}]}}

    # ── downstream flow: upstream flow ⋈ file, query → group_by ─────────────
    downstream = {"nodes": [
        {"id": "s_base", "type": "source", "data": {"datasourceId": base["id"]}},
        file_node,
        {"id": "j", "type": "operation", "data": {"opType": "join", "params": {"how": "inner", "on": "canale"}}},
        {"id": "o1", "type": "output", "data": {"destType": "datasource", "name": "DBT finale", "projectId": folder_id, "overwrite": True}},
        {"id": "s_sql", "type": "source", "data": {"datasourceId": sqlds["id"]}},
        {"id": "per_stato", "type": "operation", "data": {"opType": "group_by", "params": {"by": ["stato"], "aggregations": [{"column": "id", "func": "count", "alias": "n"}]}}},
        {"id": "o2", "type": "output", "data": {"destType": "datasource", "name": "DBT stati", "projectId": folder_id, "overwrite": True}},
    ], "edges": [{"source": "s_base", "target": "j", "targetHandle": "left"}, {"source": "file", "target": "j", "targetHandle": "right"},
                 {"source": "j", "target": "o1", "targetHandle": "left"}, {"source": "s_sql", "target": "per_stato", "targetHandle": "left"},
                 {"source": "per_stato", "target": "o2", "targetHandle": "left"}]}
    st, fb = create_flow("DBT finale", downstream, "Channels with their description")
    if not ok("the downstream flow created", st in (200, 201), (st, fb)):
        raise SystemExit(1)
    created["flows"].append(fb["id"])
    run = wait_run(api("POST", f"/flows/{fb['id']}/run-now?mode=production")[1]["run_id"])
    ok("the downstream flow succeeded", run["status"] == "SUCCESS", run)
    final = wait_datasource("DBT finale"); states = wait_datasource("DBT stati")
    ok("the datasources «DBT finale» and «DBT stati» are published", final is not None and states is not None)

    # ── a contract on the final datasource (→ dbt tests) ────────────────────
    channels = sorted({r["canale"] for r in datasource_rows(final) if r["canale"] is not None})
    rules = [{"kind": "not_null", "column": "canale", "severity": "error"},
             {"kind": "unique", "column": "canale", "severity": "error"},
             {"kind": "accepted_values", "column": "canale", "values": channels, "severity": "warning"},
             {"kind": "range", "column": "ordini", "min": 1, "severity": "error"},
             {"kind": "row_count", "min": 1, "max": 1000, "severity": "warning"},
             {"kind": "pattern", "column": "descrizione", "regex": "Canale .*", "severity": "error"},  # the WHOLE value, as Tabularia matches it
             {"kind": "expression", "sql": "ordini >= 1", "severity": "warning"},
             {"kind": "freshness", "max_age_hours": 48, "severity": "warning"}]
    st, c = api("PUT", f"/datasources/{final['id']}/contract", {"document": {"description": "channels we promise", "rules": rules}, "enabled": True})
    ok("contract put on the final datasource", st == 200, (st, c))
    saved = [r["kind"] for r in (api("GET", f"/datasources/{final['id']}/contract")[1] or {}).get("document", {}).get("rules", [])]
    print("  saved rules:", saved)
    # only not_null stays a generic test: one-column unique and accepted_values are singular (#34 #35)
    generic = sum(1 for k in saved if k == "not_null")
    singular = sum(1 for k in saved if k in ("unique", "accepted_values", "range", "pattern", "row_count", "expression"))
    st, d = api("PATCH", f"/datasources/{final['id']}", {"column_descriptions": {"ordini": "Orders shipped on the channel"}})
    ok("column description set", st == 200, (st, d))

    variables = connection_passwords()
    for target in ("duckdb", "native"):
        print(f"\n== export {target} of «DBT finale» ==")
        folder, names = export(fb, target)
        if not ok("the export answers with a zip", folder is not None, names):
            continue
        print("  files:", ", ".join(names))
        expected = ["models/dbt_base.sql", "models/src_dbt_ordini_evasi.sql", "models/dbt_finale.sql",
                    "models/dbt_stati.sql", "models/schema.yml", "models/sources.yml", "seeds/seed_canali.csv", "seeds/schema.yml",
                    "tests/dbt_finale__unique_canale.sql", "tests/dbt_finale__accepted_values_canale.sql",
                    "tests/dbt_finale__range_r4.sql", "tests/dbt_finale__row_count_r5.sql", "tests/dbt_finale__pattern_r6.sql", "tests/dbt_finale__expression_r7.sql", "README.md"]
        ok("the project has the upstream model it reads, the query, the seed, the singular tests", all(a in names for a in expected), [a for a in expected if a not in names])
        # decision 4: only the branch read downstream comes from the upstream flow — not its append
        # output, and so no step shared with it either
        ok("the upstream flow brings only the branch read: no dbt_righe, no int_anno",
           "models/dbt_righe.sql" not in names and "models/int_anno.sql" not in names, [n for n in names if "righe" in n or "int_" in n])
        read = lambda f: open(f"{folder}/{f}").read()
        ok("the downstream flow reads the upstream flow with ref() and the file with the seed",
           "{{ ref('dbt_base') }}" in read("models/dbt_finale.sql") and "{{ ref('seed_canali') }}" in read("models/dbt_finale.sql"))
        ok("the upstream model lives in the project, never in an upstream destination table", "alias=" not in read("models/dbt_base.sql"))
        expected_query = "postgres_query('db_%d'" % pg["id"] if target == "duckdb" else "SELECT id, canale, stato FROM"
        ok("the datasource from a query is an ephemeral model with the query", "ephemeral" in read("models/src_dbt_ordini_evasi.sql") and expected_query in read("models/src_dbt_ordini_evasi.sql") and "{{ ref('src_dbt_ordini_evasi') }}" in read("models/dbt_stati.sql"))
        schema = read("models/schema.yml")
        # not_null is dbt's generic test, with the column quoted; one-column unique and accepted_values are
        # singular tests that count NULLs and compare values as Tabularia's check does (#33 #34 #35)
        ok("schema.yml: descriptions and the contract's not_null (column quoted), unique and accepted_values as singular tests",
           "Orders shipped on the channel" in schema and "- not_null:\n              config: {severity: error}" in schema and "quote: true" in schema
           and "- unique:" not in schema and "- accepted_values:" not in schema and "channels we promise" in schema, schema[:600])
        ok("the singular accepted_values test keeps the rule's severity (warning)",
           "config(severity='warn')" in read("tests/dbt_finale__accepted_values_canale.sql"), read("tests/dbt_finale__accepted_values_canale.sql")[:300])
        ok("sources.yml has the orders table with the columns' description", "name: ordini" in read("models/sources.yml") and "columns:" in read("models/sources.yml"))
        ok("the README warns about freshness and seeds", "freshness" in read("README.md") and "seed" in read("README.md"))
        rc, out = dbt(folder, "seed", variables=variables)
        ok("dbt seed passes", rc == 0 and "Completed successfully" in out, out[-1500:])
        rc, out = dbt(folder, "run", variables=variables)
        if not ok("dbt run passes (4 models, 1 ephemeral)", rc == 0 and "Completed successfully" in out, out[-2500:]):
            continue
        rc, out = dbt(folder, "test", variables=variables)
        ok(f"dbt test passes: {generic} generic tests + {singular} singular", rc == 0 and f"PASS={generic + singular}" in out, out[-2500:])
        for name in ("DBT base", "DBT finale", "DBT stati"):
            compare(folder, name.lower().replace(" ", "_"), wait_datasource(name), variables)

    # ── the upstream flow exported by itself: shared steps, the append table ──
    for target in ("duckdb", "native"):
        print(f"\n== export {target} of «DBT base» ==")
        folder, names = export(fa, target, suffix="_monte")
        if not ok("the upstream flow's export answers with a zip", folder is not None, names):
            continue
        read = lambda f: open(f"{folder}/{f}").read()
        expected = ["models/dbt_base.sql", "models/dbt_righe.sql", "models/int_anno.sql"]
        ok("the upstream flow has its two outputs and the intermediate", all(a in names for a in expected), [a for a in expected if a not in names])
        ok("the shared steps are an ephemeral model and the outputs reference it",
           "materialized='ephemeral'" in read("models/int_anno.sql") and "{{ ref('int_anno') }}" in read("models/dbt_base.sql") and "{{ ref('int_anno') }}" in read("models/dbt_righe.sql"))
        head = read("models/dbt_righe.sql")
        # #19: a one-part table name lands where Tabularia writes it, the connection's schema
        ok("the append table is an incremental model" + (f" in the destination table «{ROWS_SCHEMA}».dbt_righe" if target == "native" else ""),
           "materialized='incremental', incremental_strategy='append'" in head
           and (target != "native" or (f"schema='{ROWS_SCHEMA}'" in head and "alias='dbt_righe'" in head)), head[:400])
        before = dbt_righe_count()
        rc, out = dbt(folder, "run", variables=variables)
        if not ok("dbt run passes on the upstream flow (2 models, 1 ephemeral)", rc == 0 and "Completed successfully" in out, out[-2500:]):
            continue
        if target == "native":
            # #78: compare the rows, not the alias string — dbt appends into the very table Tabularia wrote
            after = dbt_righe_count()
            elsewhere = sample_postgres("SELECT to_regclass('dbt_tabularia.dbt_righe') IS NULL").stdout.strip()
            ok(f"native: dbt appends into «{ROWS_SCHEMA}».dbt_righe, the table Tabularia wrote ({before} rows → {after})",
               before and after == 2 * before and elsewhere == "t", (before, after, elsewhere))
        compare(folder, "dbt_base", wait_datasource("DBT base"), variables)
finally:
    clean()
print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
sys.exit(0 if all(outcomes) else 1)
