"""The FOLDER for an existing project (piece 3) inside a data team's project: the team
already has a model of its own and a sources.yml of its own that declares the sales
source (on ClickHouse: with a federated database of its own). The folder is downloaded
from the dialog (POST with the options: folder, prefix, layers, source «already
declared» with THEIR name), copied into their project, and:
  · their profile gets the blocks INTEGRATION.md gives (DuckDB secrets + attach and the UTC
    session; ClickHouse custom_settings), and the command it gives,
    `dbt build -s +tag:<flow>,tag:tabularia`, passes and the model gives the same data as Tabularia;
  · their model stays where it is, and tagged like the flow it is NOT selected;
  · counter-check: without «already declared» dbt refuses the duplicate source.
ClickHouse target (the engine's ClickHouse) and federated DuckDB.
usage: python3 folder_package.py [clickhouse|duckdb]"""
import io, os, re, subprocess, sys, zipfile
from environment import CH_CONTAINER, CH_PORT, WORKDIR, IMAGE
from common import api, compare, dbt, sign_in, outcomes, ok, connection_passwords

FLOW = "Margin by Category and Month"
TARGETS = [a for a in sys.argv[1:] if not a.startswith("--")] or ["clickhouse", "duckdb"]
CH = CH_CONTAINER


def ch_sql(sql):
    return subprocess.run(["docker", "exec", "-i", CH, "sh", "-c", 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery'],
                          input=sql, capture_output=True, text=True)


def download(flow, options):
    st, zipped = api("POST", f"/flows/{flow['id']}/export/dbt", options, raw=True)
    if st != 200 or zipped[:2] != b"PK":
        return None
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        return {n: z.read(n) for n in z.namelist()}


def profile_blocks(integration):
    """The YAML blocks of INTEGRATION.md's «Your profile» section: what the team adds to its output."""
    section = integration.split("## 2. Your profile", 1)[1].split("\n## ", 1)[0]
    return re.findall(r"```yaml\n(.*?)```", section, re.S)


def selector(integration):
    """The selector of INTEGRATION.md's «Run»: `dbt build -s <it>`."""
    return re.search(r"^dbt build -s (\S+)$", integration, re.M).group(1)


def team_project(folder, target, pg, their_source, blocks, tag):
    """A dbt project «of a team»: its own name, its own profile (with the blocks INTEGRATION.md asks
    for), a model of its own — tagged like the flow, as a team's own model may well be — a sources.yml of its own."""
    if os.path.exists(folder):
        subprocess.run(["docker", "run", "--rm", "-v", f"{WORKDIR}:/x", IMAGE, "sh", "-c", f"rm -rf /x/{os.path.basename(folder)}"], capture_output=True)
    os.makedirs(f"{folder}/models/staging")
    open(f"{folder}/dbt_project.yml", "w").write(
        "name: 'team'\nversion: '1.0.0'\nconfig-version: 2\nprofile: 'team'\nmodel-paths: [\"models\"]\nseed-paths: [\"seeds\"]\n"
        "test-paths: [\"tests\"]\nmacro-paths: [\"macros\"]\nmodels:\n  team:\n    +materialized: view\n")
    if target == "clickhouse":
        open(f"{folder}/profiles.yml", "w").write(
            "team:\n  target: dev\n  outputs:\n    dev:\n      type: clickhouse\n"
            f"      host: {CH}\n      port: {CH_PORT}\n"
            "      user: \"{{ env_var('CLICKHOUSE_USER') }}\"\n      password: \"{{ env_var('CLICKHOUSE_PASSWORD') }}\"\n"
            "      schema: team_analytics\n" + "".join(blocks))
        source = f"  - name: {their_source}\n    schema: team_erp\n"
    else:
        # the secrets + attach blocks (the password through a DuckDB secret, never in an ATTACH DSN, #32)
        # and the UTC session (#1), as INTEGRATION.md prints them
        open(f"{folder}/profiles.yml", "w").write(
            "team:\n  target: dev\n  outputs:\n    dev:\n      type: duckdb\n      path: team.duckdb\n      extensions: [postgres]\n"
            + "".join(blocks))
        source = f"  - name: {their_source}\n    database: db_{pg['id']}\n    schema: vendite\n"
    open(f"{folder}/models/staging/sources.yml", "w").write(
        "version: 2\n\nsources:\n" + source + "    tables:\n      - name: ordini\n      - name: righe_ordine\n")
    open(f"{folder}/models/staging/stg_esistente.sql", "w").write("{{ config(tags=['%s']) }}\nselect 1 as gia_qui\n" % tag)


def copy(files, folder):
    for name, data in files.items():
        if name == "INTEGRATION.md":
            continue
        os.makedirs(os.path.dirname(f"{folder}/{name}"), exist_ok=True)
        open(f"{folder}/{name}", "wb").write(data)


sign_in()
flow = next(f for f in api("GET", "/flows")[1] if f["name"] == FLOW)
plan = api("GET", f"/flows/{flow['id']}/export/dbt/plan")[1]
pg = next(c for c in api("GET", "/connections")[1] if c["db_type"] == "postgresql")
variables = connection_passwords()
ds = {d["name"]: d for d in api("GET", "/datasources")[1]}
output = plan["outputs"][0]
try:
    if "clickhouse" in TARGETS:
        # the team already has a federated database OF ITS OWN towards the sales
        pw = variables[f"TABULARIA_DB_{pg['id']}_PASSWORD"].replace("\\", "\\\\").replace("'", "\\'")
        r = ch_sql("DROP DATABASE IF EXISTS team_erp; DROP DATABASE IF EXISTS team_analytics; "
                   f"CREATE DATABASE team_erp ENGINE = PostgreSQL('{pg['host']}:{pg.get('port') or 5432}', '{pg['database']}', '{pg['username']}', '{pw}', 'vendite');")
        ok("clickhouse: the team's federated database", r.returncode == 0, r.stderr[-300:])
    for target in TARGETS:
        print(f"\n== target {target}", flush=True)
        t = next(x for x in plan["targets"] if x["id"] == target)
        sales = next(s for s in t["sources"] if s["schema"] == "vendite")
        options = {"target": target, "package": "folder", "folder": "tabularia_margini", "prefix": "tab_", "layers": True,
                   "sources": {sales["key"]: {"name": "erp_vendite", "declared": True}}}
        files = download(flow, options)
        if not ok(f"{target}: the folder downloads", files is not None):
            continue
        integration = files["INTEGRATION.md"].decode()
        choice = selector(integration)
        tag = re.match(r"\+tag:([^,]+),tag:tabularia$", choice)
        if not ok(f"{target}: INTEGRATION.md selects only what came from Tabularia ({choice})", tag, choice):
            continue
        tag = tag.group(1)
        blocks = profile_blocks(integration)
        expected = ("secrets:", "secret: tabularia_db_", "TimeZone: UTC") if target == "duckdb" else ("join_use_nulls: 1", "max_query_size:")
        ok(f"{target}: INTEGRATION.md gives the profile blocks", all(a in "".join(blocks) for a in expected), blocks)
        folder = f"{WORKDIR}/team_{target}"
        team_project(folder, target, pg, "erp_vendite", blocks, tag)
        copy(files, folder)
        if target == "clickhouse":
            ch_sql("DROP DATABASE IF EXISTS tab_src_%d_catalogo;" % pg["id"])
            rc, out = dbt(folder, "run-operation", "create_tabularia_sources", "--log-level-file", "none", variables=variables)
            ok(f"{target}: the folder's macro creates only the databases of the sources NOT declared",
               rc == 0 and "tab_src_%d_catalogo" % pg["id"] in out and "vendite" not in out, out[-500:])
        rc, out = dbt(folder, "build", "-s", choice, variables=variables)
        ok(f"{target}: dbt build -s {choice} in the team's project passes", rc == 0 and "Completed successfully" in out,
           "\n".join(l for l in out.splitlines() if "rror" in l or "ERROR" in l)[:1200] or out[-1200:])
        rc, out = dbt(folder, "ls", "--resource-type", "model", "--quiet", variables=variables)
        ok(f"{target}: the team's model stays, next to ours", rc == 0 and "team.staging.stg_esistente" in out and f"tab_{output['model']}" in out, out[-500:])
        rc, out = dbt(folder, "ls", "-s", choice, "--resource-type", "model", "--quiet", variables=variables)
        ok(f"{target}: the team's model tagged «{tag}» is not selected, the flow's model is (#41)",
           rc == 0 and "stg_esistente" not in out and f"tab_{output['model']}" in out, out[-500:])
        compare(folder, f"tab_{output['model']}", ds[FLOW], variables)
        # counter-check: the same name WITHOUT «already declared» → the source is there twice and dbt refuses it
        duplicate = download(flow, {**options, "sources": {sales["key"]: {"name": "erp_vendite", "declared": False}}})
        counter = f"{WORKDIR}/team_{target}_doppia"
        team_project(counter, target, pg, "erp_vendite", blocks, tag)
        copy(duplicate, counter)
        rc, out = dbt(counter, "parse", variables=variables)
        ok(f"{target}: counter-check — without «already declared» dbt refuses the duplicate source", rc != 0 and re.search(r"(?i)duplicate|two sources|same name", out), out[-600:])
finally:
    if "clickhouse" in TARGETS:
        ch_sql(f"DROP DATABASE IF EXISTS team_erp; DROP DATABASE IF EXISTS team_analytics; DROP DATABASE IF EXISTS tab_src_{pg['id']}_catalogo;")
print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
sys.exit(0 if all(outcomes) else 1)
