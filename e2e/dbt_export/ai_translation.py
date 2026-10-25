"""LIVE test of the translation the AI proposes (piece 4) on the throwaway stack: a
datasource from a VALID Postgres query that sqlglot translates into SQL ClickHouse does
not read (SIMILAR TO). Without AI the ClickHouse export refuses it saying why
(ClickHouse's syntax check); with the AI the proposal passes the checks, dbt runs it
on ClickHouse and gives the same data as Tabularia; exporting again does not call the
AI again (same zip). Needs an enabled AI model: paid calls to the provider. usage: python3 ai_translation.py"""
import hashlib, io, json, os, subprocess, sys, time, zipfile
from environment import WORKDIR, IMAGE
from common import wait_run, api, compare, dbt, sign_in, outcomes, ok, connection_passwords, clean_clickhouse

QUERY = "SELECT id, canale, stato FROM vendite.ordini WHERE canale SIMILAR TO 'G(DO|astronomia)' AND id < 400"
sign_in()
pg = next(c for c in api("GET", "/connections")[1] if c["db_type"] == "postgresql")
st, folder_obj = api("POST", "/projects", {"name": "zzai translation"})
C = folder_obj["id"]
try:
    st, ds = api("POST", f"/projects/{C}/datasources/database", {"name": "zzai orders g", "connection_id": pg["id"], "source_type": "sql", "source_ref": QUERY})
    ok("datasource from a Postgres query (SIMILAR TO) imported", st in (200, 201), (st, ds))
    for _ in range(60):
        d = next((x for x in api("GET", "/datasources")[1] if x["id"] == ds["id"]), {})
        if d.get("rows") and not d.get("refreshing"):
            break
        time.sleep(2)
    nodes = [{"id": "s", "type": "source", "data": {"datasourceId": ds["id"]}},
             {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "zzai output", "projectId": C, "overwrite": True}}]
    st, f = api("POST", f"/projects/{C}/flows", {"name": "zzai flow", "engine": "clickhouse",
                                                  "definition": json.dumps({"nodes": nodes, "edges": [{"source": "s", "target": "o", "targetHandle": "left"}]})})
    run = wait_run(api("POST", f"/flows/{f['id']}/run-now?mode=production")[1]["run_id"])
    ok("the flow runs in Tabularia", run["status"] == "SUCCESS", run.get("error"))

    st, answer = api("POST", f"/flows/{f['id']}/export/dbt", {"target": "clickhouse"}, raw=True)
    ok("without AI: the ClickHouse export refuses, and says ClickHouse does not read the translation",
       st == 422 and "SIMILAR" in str(answer) and "does not translate to ClickHouse" in str(answer), (st, str(answer)[:400]))

    st, first = api("POST", f"/flows/{f['id']}/export/dbt", {"target": "clickhouse", "ai_translations": True}, raw=True)
    if not ok("with the AI: the export answers with a zip", st == 200 and first[:2] == b"PK", (st, str(first)[:500])):
        raise SystemExit
    files = {n: zipfile.ZipFile(io.BytesIO(first)).read(n).decode() for n in zipfile.ZipFile(io.BytesIO(first)).namelist()}
    model = next(n for n in files if n.startswith("models/src_"))
    print("    " + files[model].replace("\n", "\n    ")[:900])
    ok("the translated model says so at the top and the README lists it",
       "Translated by AI from postgres" in files[model] and "translated by an AI model" in files["README.md"] and "tab_src_" in files[model])

    st, second = api("POST", f"/flows/{f['id']}/export/dbt", {"target": "clickhouse", "ai_translations": True}, raw=True)
    ok("exporting the same version again: the same zip (the proposal is remembered)", st == 200 and hashlib.sha256(first).digest() == hashlib.sha256(second).digest())

    folder = f"{WORKDIR}/zzai_translation"
    subprocess.run(["docker", "run", "--rm", "-v", f"{WORKDIR}:/x", IMAGE, "sh", "-c", "rm -rf /x/zzai_translation"], capture_output=True)
    os.makedirs(folder)
    zipfile.ZipFile(io.BytesIO(first)).extractall(folder)
    variables = connection_passwords()
    clean_clickhouse()
    rc, out = dbt(folder, "run-operation", "create_tabularia_sources", "--log-level-file", "none", variables=variables)
    ok("dbt run-operation create_tabularia_sources", rc == 0, out[-600:])
    rc, out = dbt(folder, "run", variables=variables)
    ok("dbt run on ClickHouse with the model the AI translated", rc == 0 and "Completed successfully" in out, out[-1200:])
    if rc == 0:
        d = next(x for x in api("GET", "/datasources")[1] if x["name"] == "zzai output")
        compare(folder, "zzai_output", d, variables)
finally:
    for fl in api("GET", "/flows")[1]:
        if fl["name"].startswith("zzai"):
            api("DELETE", f"/flows/{fl['id']}")
    for d in api("GET", "/datasources")[1]:
        if d["project_id"] == C:
            api("DELETE", f"/datasources/{d['id']}")
    api("DELETE", f"/projects/{C}")
    clean_clickhouse()
print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
sys.exit(0 if all(outcomes) else 1)
