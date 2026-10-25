"""The sample flows exported to dbt and run with a REAL dbt, compared with what
Tabularia publishes. For every flow of the sample's «Flows» folder and every target:
the flow is run again in production, exported, dbt seed/run/test, and every output
of the flow must give the same data as its datasource.

A target the export plan says is not available (a pivot outside DuckDB, sources on
several connections…) is skipped and said so; the only refusals that only the engine
finds and that are expected are the pivot and the sample CRM's ClickHouse, which is
not the engine's.
usage: python3 sample_flows.py [clickhouse] [duckdb] [native] [--flow NAME]"""
import json, sys

from common import (wait_run, api, compare, dbt, sign_in, export, ok, connection_passwords,
                    clean_clickhouse, summary, model_slug)

TARGETS = [a for a in sys.argv[1:] if a in ("clickhouse", "duckdb", "native")] or ["clickhouse", "duckdb", "native"]
ONLY = sys.argv[sys.argv.index("--flow") + 1] if "--flow" in sys.argv else None
# refusals only the engine knows, expected on the sample (the gateway answers in English without Accept-Language)
EXPECTED = ("A «pivot» node", "is ANOTHER ClickHouse")


def check(flow, target, variables, ds):
    folder, names = export(flow, target)
    if folder is None and any(a in str(names) for a in EXPECTED):
        print(f"  · {target}: expected refusal — {str(names[1])[:140]}")
        return
    if not ok(f"{flow['name']} · {target}: the export answers with a zip", folder is not None, names):
        return
    if "macros/create_tabularia_sources.sql" in names:
        # clickhouse target: the step the data team does once, the federated databases towards the sources
        clean_clickhouse()
        rc, out = dbt(folder, "run-operation", "create_tabularia_sources", "--log-level-file", "none", variables=variables)
        ok(f"{flow['name']} · {target}: dbt run-operation create_tabularia_sources", rc == 0, out[-1500:])
    if any(n.startswith("seeds/") and n.endswith(".csv") for n in names):
        rc, out = dbt(folder, "seed", variables=variables)
        ok(f"{flow['name']} · {target}: dbt seed", rc == 0 and "Completed successfully" in out, out[-1500:])
    rc, out = dbt(folder, "run", variables=variables)
    if not ok(f"{flow['name']} · {target}: dbt run", rc == 0 and "Completed successfully" in out, out[-1500:]):
        return
    if any(n.startswith("tests/") for n in names) or "data_tests:" in open(f"{folder}/models/schema.yml").read():
        rc, out = dbt(folder, "test", variables=variables)
        ok(f"{flow['name']} · {target}: dbt test", rc == 0, out[-1500:])
    defn = api("GET", f"/flows/{flow['id']}")[1]["definition"]
    defn = defn if isinstance(defn, dict) else json.loads(defn)
    for n in defn["nodes"]:
        if n["type"] == "output" and n["data"].get("destType", "datasource") == "datasource":
            name = n["data"]["name"]
            if name in ds:
                compare(folder, model_slug(name), ds[name], variables)
            else:
                ok(f"the datasource «{name}» exists in Tabularia", False, list(ds)[:5])


sign_in()
folders = {x["id"] for x in api("GET", "/projects")[1] if x["name"] == "Flows"}
flows = [f for f in api("GET", "/flows")[1] if f["project_id"] in folders and (ONLY is None or f["name"] == ONLY)]
ok(f"the sample flows are there ({len(flows)})", flows, "needs the stack with the samples profile")
variables = connection_passwords()
for f in flows:
    print(f"\n== «{f['name']}»", flush=True)
    run = wait_run(api("POST", f"/flows/{f['id']}/run-now?mode=production")[1]["run_id"])
    if not ok(f"{f['name']}: the flow runs in Tabularia", run.get("status") == "SUCCESS", (run.get("error") or "")[:300]):
        continue
    ds = {d["name"]: d for d in api("GET", "/datasources")[1]}
    plan = api("GET", f"/flows/{f['id']}/export/dbt/plan")[1]
    for target in TARGETS:
        t = next(x for x in plan["targets"] if x["id"] == target)
        if not t["available"]:
            print(f"  · {target}: not available according to the plan — {(t['reason'] or '')[:140]}")
            continue
        check(f, target, variables, ds)
summary()
