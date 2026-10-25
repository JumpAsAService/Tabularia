"""The rest of the dbt export, besides the matrix of the operations (throwaway stack):
  A. violated CONTRACT: for every rule, the outcome of Tabularia's check and that of the
     dbt test must match (pass / warning / error), on the two targets;
  B. LIFECYCLE: parse, compile, docs generate; a table in APPEND is an incremental model
     — two runs double it (like two Tabularia runs), --full-refresh starts over;
  C. native CLICKHOUSE: a flow on the sample CRM, compared with Tabularia.
The clickhouse target in A and B (the engine's ClickHouse reading the sample Postgres live).
In A, the rules the export used to get wrong: a pattern matches the WHOLE value read as text
(also on an integer and a date column), accepted values with an apostrophe or 3.0 on an integer
column, a one-column unique with two NULLs, not_null on names with a space, capitals or a
reserved word, two not_null rules on one column. In B, the federated target with a wrong
password and with an unreachable source: the password is in no file and not on the console.
usage: python3 lifecycle.py"""
import json, os, re, shutil, subprocess, sys, time

from environment import CH_SAMPLE_CONTAINER
from common import api, clickhouse_databases, sign_in, outcomes, export, ok, connection_passwords, sample_postgres, datasource_rows
from matrix import wait_run, compare, dbt, read_dbt

sign_in()
conn = {c["db_type"]: c for c in api("GET", "/connections")[1]}
pg, ch = conn["postgresql"], conn["clickhouse"]
import matrix
matrix.PG_ID = pg["id"]
matrix.PG_CONN.update(host=pg["host"], database=pg["database"], username=pg["username"])
variables = connection_passwords()
st, folder_obj = api("POST", "/projects", {"name": "zzdbt ciclo"}); C = folder_obj["id"]


def psql(sql):
    return sample_postgres(sql)


def files_with(folder, text):
    """The files under the project that hold the text (logs/, target/…)."""
    return [os.path.join(r, f) for r, _, fs in os.walk(folder) for f in fs if text.encode() in open(os.path.join(r, f), "rb").read()]


def slug(s):
    """The engine's slug of a test name (backend/app/engine/dbt_export.py `_slug`)."""
    s = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(s).lower()).strip("_") or "model"
    return s if not s[0].isdigit() else f"m_{s}"


def test_outcomes(folder, model, rules):
    """{rule id: dbt status}, from what dbt ran: the generic tests (only not_null) by their column,
    the singular ones by name — `<model>__<kind>_<rule id>`, or `<model>__<kind>_<column>` for a
    column rule (one-column unique, accepted_values), `_2`, `_3` when two share a file name."""
    results = json.load(open(f"{folder}/target/run_results.json"))["results"]
    nodes = json.load(open(f"{folder}/target/manifest.json"))["nodes"]
    dbt_outcomes = {}
    for r in results:
        n = nodes.get(r["unique_id"]) or {}
        if n.get("resource_type") != "test":
            continue
        meta = n.get("test_metadata")
        if meta:
            col = (n.get("column_name") or (meta.get("kwargs") or {}).get("column_name") or "").strip('"`')
            for rule in rules:
                if rule["kind"] == meta.get("name") and rule.get("column") == col:
                    dbt_outcomes[rule["id"]] = r["status"]
            continue
        m = re.fullmatch(rf"{model}__(\w+?)_(r\d+)", n["name"])
        if m:
            dbt_outcomes[m.group(2)] = r["status"]
            continue
        m = re.fullmatch(rf"{model}__(not_null|unique|accepted_values)_(\w+?)(?:_(\d+))?", n["name"])
        if m:
            alike = [x for x in rules if x["kind"] == m.group(1) and slug(x.get("column") or (x.get("columns") or [""])[0]) == m.group(2)]
            k = int(m.group(3) or 1) - 1
            for x in (alike[k:k + 1] if len(alike) > 1 else alike):
                dbt_outcomes[x["id"]] = r["status"]
    return dbt_outcomes


def flow(name, nodes, edges):
    st, f = api("POST", f"/projects/{C}/flows", {"name": name, "definition": json.dumps({"nodes": nodes, "edges": edges}), "engine": matrix.ENGINE})
    assert st in (200, 201), (st, f)
    return f


def launch(f):
    return wait_run(api("POST", f"/flows/{f['id']}/run-now?mode=production")[1]["run_id"])


def ds(name):
    for _ in range(60):
        d = next((x for x in api("GET", "/datasources")[1] if x["name"] == name and x["project_id"] == C), None)
        if d and d.get("rows") is not None and not d.get("refreshing"):
            return d
        time.sleep(2)
    return None


E = lambda a, b, h="left": {"source": a, "target": b, "targetHandle": h}
try:
    st, cases = api("POST", f"/projects/{C}/datasources/database", {"name": "zzciclo casi", "connection_id": pg["id"], "source_type": "table", "source_ref": "zzdbt.casi"})
    cases = ds("zzciclo casi")
    ok("source imported", cases is not None)

    # ── A. violated contract ─────────────────────────────────────────────────
    print("A. violated contract: Tabularia and dbt must say the same things", flush=True)
    fa = flow("zzciclo contratto", [
        {"id": "s", "type": "source", "data": {"datasourceId": cases["id"]}},
        {"id": "p", "type": "operation", "data": {"opType": "select", "params": {"columns": [
            "id", "categoria", "numero", "testo", "intero", "data", "città", "Nome Strano", "select"]}}},
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "zzciclo dati", "projectId": C, "overwrite": True}}],
        [E("s", "p"), E("p", "o")])
    ok("the contract's flow ran", launch(fa)["status"] == "SUCCESS")
    data = ds("zzciclo dati")
    rules = [
        {"id": "r1", "kind": "not_null", "column": "id", "severity": "error"},                 # passes
        {"id": "r2", "kind": "not_null", "column": "categoria", "severity": "error"},          # NULLs present
        {"id": "r3", "kind": "unique", "columns": ["categoria"], "severity": "warning"},        # duplicates
        {"id": "r4", "kind": "accepted_values", "column": "categoria", "values": ["A", "B"], "severity": "error"},  # there is C
        {"id": "r5", "kind": "range", "column": "numero", "min": 0, "severity": "warning"},    # negatives
        {"id": "r6", "kind": "range", "column": "id", "min": 1, "max": 100, "severity": "error"},  # passes
        {"id": "r7", "kind": "pattern", "column": "testo", "regex": "^[0-9]+$", "severity": "error"},  # ' 5 ' etc.
        {"id": "r8", "kind": "row_count", "max": 3, "severity": "warning"},                     # 8 rows
        {"id": "r9", "kind": "expression", "sql": "numero < 1000", "severity": "error"},        # 1e10
        {"id": "r10", "kind": "unique", "columns": ["id"], "severity": "error"},               # passes
        {"id": "r11", "kind": "not_null", "column": "Nome Strano", "severity": "error"},       # a space and a capital: NULL at id 2
        {"id": "r12", "kind": "not_null", "column": "select", "severity": "warning"},          # a reserved word: passes
        {"id": "r13", "kind": "pattern", "column": "intero", "regex": "[0-9]+", "severity": "error"},   # '-4' does not match whole
        {"id": "r14", "kind": "pattern", "column": "data", "regex": "[0-9]{4}-[0-9]{2}-[0-9]{2}", "severity": "warning"},  # a date as text
        {"id": "r15", "kind": "accepted_values", "column": "città", "values": ["Milano", "Parma", "Città d'altri", "Torino", "Roma"], "severity": "error"},
        {"id": "r16", "kind": "accepted_values", "column": "intero", "values": [10, 7, -4, 2, 3, 1000000.0], "severity": "error"},
        {"id": "r17", "kind": "unique", "columns": ["intero"], "severity": "error"},           # two NULLs, nothing else twice
        {"id": "r18", "kind": "not_null", "column": "testo", "severity": "warning"},           # two rules on one column: one
        {"id": "r19", "kind": "not_null", "column": "testo", "severity": "error"},             # test, the stricter severity
    ]
    st, c = api("PUT", f"/datasources/{data['id']}/contract", {"document": {"rules": rules}, "enabled": True})
    ok("contract saved", st == 200, (st, c))
    st, rep = api("POST", f"/datasources/{data['id']}/contract/check")
    tab_outcomes = {}
    for r in (rep.get("report") or {}).get("rules", []):
        tab_outcomes[r.get("id")] = "pass" if r.get("passed") else ("warn" if r.get("severity") == "warning" else "fail")
    ok("Tabularia evaluated every rule", len(tab_outcomes) == len(rules), rep if not tab_outcomes else tab_outcomes)
    for target in ("duckdb", "native", "clickhouse"):
        folder, names = export(fa, target, suffix="_ciclo")
        if not ok(f"A · {target}: export", folder is not None, names):
            continue
        psql("DROP SCHEMA IF EXISTS dbt_tabularia CASCADE")
        if target == "clickhouse":
            matrix.clean_clickhouse()
            rc, out = matrix.prepare_clickhouse(folder, variables)
            ok(f"A · {target}: dbt run-operation create_tabularia_sources", rc == 0, out[-800:])
        rc, out = dbt(folder, "dbt", "run", "--no-use-colors", variables=variables)
        ok(f"A · {target}: dbt run", rc == 0, out[-800:])
        # only not_null is dbt's generic test, on a quoted column; unique and accepted_values are singular
        # tests that count NULLs and compare values as Tabularia's check does
        schema_yml = open(f"{folder}/models/schema.yml").read()
        ok(f"A · {target}: schema.yml has only not_null generic tests, each column quoted",
           "- not_null:" in schema_yml and "quote: true" in schema_yml and "- unique:" not in schema_yml and "- accepted_values:" not in schema_yml,
           schema_yml[:600])
        warnings = [l for l in out.splitlines() if re.search(r"(?i)deprecat|\[WARNING\]", l)]
        ok(f"A · {target}: dbt run without deprecation warnings", not warnings, "\n".join(warnings)[:600])
        rc, out = dbt(folder, "dbt", "test", "--no-use-colors", variables=variables)
        dbt_outcomes = test_outcomes(folder, "zzciclo_dati", rules)
        for rule in rules:
            expected = tab_outcomes.get(rule["id"])
            if rule["kind"] == "not_null":
                # two not_null rules on one column are ONE test with the stricter severity: it fails
                # where the error rule fails (and the warning rule warns)
                same = [x for x in rules if x["kind"] == "not_null" and x.get("column") == rule.get("column")]
                expected = next((tab_outcomes.get(x["id"]) for x in same if x["severity"] == "error"), expected)
            ok(f"A · {target} · {rule['id']} {rule['kind']} ({rule['severity']}): Tabularia {tab_outcomes.get(rule['id'])}, dbt {dbt_outcomes.get(rule['id'])}",
               expected == dbt_outcomes.get(rule["id"]))

    # ── B. lifecycle, append table ───────────────────────────────────────────
    print("B. lifecycle: parse, compile, docs, incremental append, full-refresh", flush=True)
    psql("DROP TABLE IF EXISTS zzdbt.ciclo_append")
    fb = flow("zzciclo append", [
        {"id": "s", "type": "source", "data": {"datasourceId": cases["id"]}},
        {"id": "p", "type": "operation", "data": {"opType": "select", "params": {"columns": ["id", "categoria"]}}},
        {"id": "o", "type": "output", "data": {"destType": "database", "connectionId": pg["id"], "table": "zzdbt.ciclo_append", "mode": "append"}}],
        [E("s", "p"), E("p", "o")])
    for _ in range(2):
        launch(fb)
    tab_rows = int(psql("SELECT count(*) FROM zzdbt.ciclo_append").stdout.strip() or 0)
    ok(f"Tabularia: two runs in append = {tab_rows} rows", tab_rows == 16, tab_rows)
    for target in ("duckdb", "native", "clickhouse"):
        folder, names = export(fb, target, suffix="_ciclo")
        if not ok(f"B · {target}: export", folder is not None, names):
            continue
        if target == "clickhouse":
            matrix.clean_clickhouse()
            ok(f"B · {target}: before the macro the federated databases are not there", not any(n.startswith("tab_src_") for n in clickhouse_databases()))
            rc, out = matrix.prepare_clickhouse(folder, variables)
            ok(f"B · {target}: dbt run-operation create_tabularia_sources", rc == 0, out[-800:])
            ok(f"B · {target}: the macro really creates the federated database", f"tab_src_{pg['id']}_zzdbt" in clickhouse_databases(), sorted(clickhouse_databases()))
            # the source's password stays in no file dbt writes. A FAKE, recognisable password (the
            # sample's real one is also the database's name): the macro
            # runs CREATE OR REPLACE TABLE … PostgreSQL(…, password) every time, so that SQL goes through dbt
            fake = "zz-SEGRETO-4242"
            v2 = {**variables, f"TABULARIA_DB_{pg['id']}_PASSWORD": fake}
            search = lambda: files_with(folder, fake)
            # the typed proxy tables read the structure from Postgres when the macro creates them, so the
            # wrong password fails the macro (loudly); what matters is that it is printed and kept nowhere
            rc, out = matrix.prepare_clickhouse(folder, v2)
            ok(f"B · {target}: with the README's command a wrong password fails and is in no file (logs/, target/) nor on the console", rc != 0 and not search() and fake not in out, (rc, search(), out[-300:]))
            rc, out = dbt(folder, "dbt", "run-operation", "create_tabularia_sources", "--no-use-colors", variables=v2)
            ok(f"B · {target}: counter-check — without --log-level-file none the password ends up in logs/dbt.log", any("dbt.log" in f for f in search()), search())
        if target == "native":
            psql("DROP TABLE IF EXISTS zzdbt.ciclo_append")
        for command in (["dbt", "parse"], ["dbt", "compile"], ["dbt", "docs", "generate"]):
            rc, out = dbt(folder, *command, "--no-use-colors", variables=variables)
            ok(f"B · {target}: {' '.join(command)}", rc == 0, out[-600:])
            if command == ["dbt", "parse"]:
                warnings = [l for l in out.splitlines() if re.search(r"(?i)deprecat|\[WARNING\]", l)]
                ok(f"B · {target}: dbt parse without deprecation warnings", not warnings, "\n".join(warnings)[:600])
        # piece 2: provenance — where the model comes from, in the manifest and at the top of the file
        manifest = json.load(open(f"{folder}/target/manifest.json"))
        node = next((n for n in manifest["nodes"].values() if n["resource_type"] == "model" and n["name"] == "ciclo_append"), None)
        meta = ((node or {}).get("config") or {}).get("meta", {}).get("tabularia", {})
        ok(f"B · {target}: the manifest carries meta.tabularia (flow, id, version and when it was saved, output) and not the export's time",
           meta.get("flow") == "zzciclo append" and meta.get("flow_id") == fb["id"] and meta.get("flow_version") and "zzdbt.ciclo_append" in (meta.get("output") or "")
           and (meta.get("version_saved_at") or "").endswith("UTC") and not any(k.startswith("exported") for k in meta), meta)
        ok(f"B · {target}: the tags tabularia + flow", node is not None and {"tabularia", "zzciclo_append"} <= set(node.get("tags") or []), (node or {}).get("tags"))
        rc, out = dbt(folder, "dbt", "ls", "-s", "tag:tabularia", "--resource-type", "model", "--quiet", "--no-use-colors", variables=variables)
        ok(f"B · {target}: dbt ls -s tag:tabularia finds the model", rc == 0 and "ciclo_append" in out, out[-400:])
        head = open(f"{folder}/models/ciclo_append.sql").read().split("\n")[2:4]
        ok(f"B · {target}: where it comes from, at the top of the file", head and head[0].startswith("-- Exported from Tabularia: flow «zzciclo append»"), head)
        print("    " + "\n    ".join(head))
        counts = []
        for arguments in (["dbt", "run"], ["dbt", "run"], ["dbt", "run", "--full-refresh"]):
            rc, out = dbt(folder, *arguments, "--no-use-colors", variables=variables)
            if target == "native":
                n = int(psql("SELECT count(*) FROM zzdbt.ciclo_append").stdout.strip() or -1)
            else:
                produced = read_dbt(folder, target, variables)
                n = len(produced.get("ciclo_append", {}).get("rows", []))
            counts.append((rc, n))
        ok(f"B · {target}: run, run, --full-refresh → 8, 16, 8 rows" + (" in the flow's destination table" if target == "native" else ""),
           [n for _, n in counts] == [8, 16, 8] and all(rc == 0 for rc, _ in counts), counts)
        if target == "duckdb":
            # the federated profile reads the password through a DuckDB secret, never in an ATTACH DSN:
            # a wrong password, or a source that does not answer, fails without printing it (#32)
            fake = "zz-SEGRETO 4242'x\\y"
            # searched by a part with no quote and no backslash: a JSON file (run_results.json) or a
            # Python repr in a traceback writes those escaped, and the whole password would not match
            mark = "SEGRETO 4242"
            v2 = {**variables, f"TABULARIA_DB_{pg['id']}_PASSWORD": fake}
            rc, out = dbt(folder, "dbt", "run", "--no-use-colors", variables=v2)
            ok(f"B · {target}: a wrong password fails the run and is in no file (logs/, target/) nor on the console",
               rc != 0 and mark not in out and not files_with(folder, mark), (rc, files_with(folder, mark), out[-300:]))
            far = folder + "_irraggiungibile"
            shutil.rmtree(far, ignore_errors=True)
            shutil.copytree(folder, far, ignore=shutil.ignore_patterns("logs", "target", "*.duckdb", "*.wal"))
            profile = open(f"{far}/profiles.yml").read()
            open(f"{far}/profiles.yml", "w").write(profile.replace(pg["host"], "zz-nessuno.invalid"))
            rc, out = dbt(far, "dbt", "run", "--no-use-colors", variables=v2)
            ok(f"B · {target}: a source that does not answer fails the run, and the password is nowhere",
               rc != 0 and pg["host"] in profile and mark not in out and not files_with(far, mark), (rc, files_with(far, mark), out[-300:]))
    psql("DROP TABLE IF EXISTS zzdbt.ciclo_append")

    # ── C. native ClickHouse ─────────────────────────────────────────────────
    print("C. native ClickHouse on the sample CRM", flush=True)
    crm = next(x for x in api("GET", "/datasources")[1] if x.get("source_ref") == "attivita_crm")
    fc = flow("zzciclo crm", [
        {"id": "s", "type": "source", "data": {"datasourceId": crm["id"]}},
        {"id": "f", "type": "operation", "data": {"opType": "filter", "params": {"column": "durata_min", "operator": "gt", "value": 10}}},
        {"id": "g", "type": "operation", "data": {"opType": "group_by", "params": {"by": ["tipo", "esito"], "aggregations": [
            {"column": "id", "func": "count", "alias": "attivita"}, {"column": "durata_min", "func": "mean", "alias": "Durata_media"}]}}},
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "zzciclo crm", "projectId": C, "overwrite": True}}],
        [E("s", "f"), E("f", "g"), E("g", "o")])
    ok("CRM flow ran in Tabularia", launch(fc)["status"] == "SUCCESS")
    folder, names = export(fc, "native", suffix="_ciclo")
    if ok("C · native ClickHouse export", folder is not None, names):
        print("    " + open(f"{folder}/profiles.yml").read().replace("\n", "\n    ")[:400])
        rc, out = dbt(folder, "dbt", "run", "--no-use-colors", variables=variables)
        if ok("C · dbt run on ClickHouse", rc == 0, "\n".join(l for l in out.splitlines() if "rror" in l)[:1200] or out[-1200:]):
            rc, out = dbt(folder, "dbt", "show", "--inline", "select * from {{ ref('zzciclo_crm') }}", "--limit", "10000", "--output", "json", "--quiet", "--no-use-colors", variables=variables)
            rows = json.loads(out[out.index("{"):]).get("show", [])
            cols = list(rows[0].keys()) if rows else []
            tab = datasource_rows(ds("zzciclo crm"))
            tab_cols = list(tab[0].keys()) if tab else []
            equal, why = compare("crm", {}, (tab_cols, [list(r.values()) for r in tab]), {"cols": cols, "rows": [list(r.values()) for r in rows]})
            ok(f"C · ClickHouse gives the same result as Tabularia ({len(tab)} rows)", equal, why)

    # C2. outer join on NOT Nullable columns (MergeTree): without join_use_nulls = 1 ClickHouse
    # fills the missing side with 0 / '' instead of NULL. Three agents have no manager.
    agents = next(x for x in api("GET", "/datasources")[1] if x.get("source_ref") == "agenti")
    fj = flow("zzciclo crm join", [
        {"id": "s", "type": "source", "data": {"datasourceId": agents["id"]}},
        {"id": "r", "type": "source", "data": {"datasourceId": agents["id"]}},
        {"id": "j", "type": "operation", "data": {"opType": "join", "params": {"how": "left", "left_on": ["responsabile_id"], "right_on": ["id"]}}},
        {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "zzciclo crm join", "projectId": C, "overwrite": True}}],
        [E("s", "j"), {"source": "r", "target": "j", "targetHandle": "right"}, E("j", "o")])
    ok("CRM flow with a left join ran in Tabularia", launch(fj)["status"] == "SUCCESS")
    folder, names = export(fj, "native", suffix="_ciclo")
    if ok("C2 · native ClickHouse export of the left join", folder is not None, names):
        rc, out = dbt(folder, "dbt", "run", "--no-use-colors", variables=variables)
        if ok("C2 · dbt run on ClickHouse", rc == 0, "\n".join(l for l in out.splitlines() if "rror" in l)[:1200] or out[-1200:]):
            rc, out = dbt(folder, "dbt", "show", "--inline", "select * from {{ ref('zzciclo_crm_join') }}", "--limit", "10000", "--output", "json", "--quiet", "--no-use-colors", variables=variables)
            rows = json.loads(out[out.index("{"):]).get("show", [])
            tab = datasource_rows(ds("zzciclo crm join"))
            equal, why = compare("crm join", {}, (list(tab[0].keys()) if tab else [], [list(r.values()) for r in tab]),
                                 {"cols": list(rows[0].keys()) if rows else [], "rows": [list(r.values()) for r in rows]})
            ok(f"C2 · left join: the missing side is NULL in dbt as in Tabularia ({len(tab)} rows)", equal, why)
finally:
    for f in api("GET", "/flows")[1]:
        if f["name"].startswith("zzciclo"):
            api("DELETE", f"/flows/{f['id']}")
    for d in api("GET", "/datasources")[1]:
        if d["project_id"] == C:
            api("DELETE", f"/datasources/{d['id']}")
    api("DELETE", f"/projects/{C}")
    psql("DROP SCHEMA IF EXISTS dbt_tabularia CASCADE")
    matrix.clean_clickhouse()
    subprocess.run(["docker", "exec", "-i", CH_SAMPLE_CONTAINER, "sh", "-c", 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD"'],
                   input="DROP DATABASE IF EXISTS dbt_tabularia", capture_output=True, text=True)
print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
sys.exit(0 if all(outcomes) else 1)
