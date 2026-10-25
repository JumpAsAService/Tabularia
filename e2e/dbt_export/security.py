"""Security of the exports (dbt and OpenLineage) on the throwaway stack: can a user with VIEW
on the shared folder ONLY get, through the export of a flow they see, the data or the
definitions of objects of a folder they do NOT see?
  · private folder: a flow that publishes «zzsec dati segreti» (then deleted: the datasource
    stays without an origin → the export would make it a SEED, that is data) and a datasource
    from a QUERY with a recognisable text;
  · shared folder: a flow that reads the two private datasources.
Check: the preview of the private source must give 403 (the usual fence).
Jinja typed in a flow (#2): a native export whose filter value, compute expression, column
name and output table name carry Jinja goes through a real `dbt compile` and `dbt run`. Each
piece of Jinja would print a marker that its literal text does not contain ('zz' ~ 'filter'
gives «zzfilter», 7 ** 13 gives 96889010407), so a piece that ran shows on the console, in logs/
or in target/; the model's alias and schema in the manifest are the table typed in the flow; the
table dbt writes in the sample Postgres has that very name, and holds the texts as typed.
A quote in a native output's table name is refused with name_not_quotable (dbt-postgres writes a
model's table unescaped in its own SQL), by the plan before the download and by the download.
Creates everything and removes it at the end. usage: python3 security.py"""
import io, json, os, sys, time, zipfile
from common import api as _api, dbt, sign_in, export, ok, outcomes, connection_passwords, sample_postgres
import common

CAP_VIEW = sys.argv[1] if len(sys.argv) > 1 else "view"
ADMIN = None

# ── Jinja typed in a flow (#2) ────────────────────────────────────────────────
# The schema the output table lands in: the oracles' own schema in the sample Postgres (schema.sql)
JINJA_SCHEMA = "zzdbt"
# The output's table, with a backslash and Jinja: dbt's static parser keeps the escapes of a string
# literal as part of the value, so a `config(alias='t\\x')` made dbt write another table. No quote:
# dbt-postgres cannot have one in a model's table (QUOTED_TABLE), and none is needed to close the
# alias' literal early — a name written unneutralised anywhere else (a header, a comment) would run
# the log() and print 7 ** 13
JINJA_TABLE = "t\\x }}{{ log(7 ** 13, info=True) }}{{ config(a="
# a model's table dbt-postgres cannot write: refused before dbt sees it (name_not_quotable)
QUOTED_TABLE = "o'brien"
JINJA_FILTER = "{% do run_query('select 1') %}{% do log('zz' ~ 'filter', info=True) %}{# x"
JINJA_EXPRESSION = "'{{ log(''zz'' ~ ''expr'', info=True) }}{{ env_var(''ZZSEC_SEGRETO'') }}'"
JINJA_COLUMN = "c{{ 'zz' ~ 'col' }}lc"
JINJA_SECRET = {"ZZSEC_SEGRETO": "zz-segreto-9431"}
# what only a piece of Jinja that RAN would write: log() at info level goes to the console, and
# every log() call goes to logs/dbt.log at debug level, so the files are searched too
JINJA_MARKS = ("96889010407", "zzfilter", "zzexpr", "zzcol", "zz-segreto-9431")


def jinja_operations() -> list[dict]:
    """The flow's steps after the source: a filter, a compute, each with Jinja in its texts."""
    return [
        {"id": "l", "type": "operation", "data": {"opType": "limit", "params": {"n": 5}}},
        {"id": "f", "type": "operation", "data": {"opType": "filter", "params": {"column": "canale", "operator": "ne", "value": JINJA_FILTER}}},
        {"id": "c", "type": "operation", "data": {"opType": "compute", "params": {"columns": [
            {"name": "x", "expr": JINJA_EXPRESSION}, {"name": JINJA_COLUMN, "expr": "'{{ 7 * 7 }}'"}]}}},
    ]


def _ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _drop_jinja_tables(schema: str, sql=sample_postgres):
    """Drop the table of the check, and any table dbt may have created under another name."""
    sql(f"""DO $zz$ DECLARE r record; BEGIN
              FOR r IN SELECT table_name FROM information_schema.tables WHERE table_schema = '{schema}' AND table_name LIKE '%config(a=%' LOOP
                EXECUTE format('DROP TABLE %I.%I', '{schema}', r.table_name);
              END LOOP; END $zz$;""")


def check_jinja(folder: str, variables: dict, schema: str = JINJA_SCHEMA, sql=sample_postgres):
    """The exported native project of the Jinja flow: compile, manifest, run, the table in Postgres."""
    variables = {**variables, **JINJA_SECRET}
    rc, out_compile = dbt(folder, "compile", variables=variables)
    compiled = "\n".join(open(os.path.join(r, f)).read() for r, _, fs in os.walk(f"{folder}/target/compiled") for f in fs if f.endswith(".sql"))
    texts = ("{{ log(''zz'' ~ ''expr'', info=True) }}{{ env_var(''ZZSEC_SEGRETO'') }}", "{{ 7 * 7 }}",
             "{% do run_query(''select 1'') %}{% do log(''zz'' ~ ''filter'', info=True) %}{# x", JINJA_COLUMN)
    ok("dbt compile passes and gives the flow's texts back as text", rc == 0 and all(t in compiled for t in texts),
       out_compile[-800:] if rc else [t for t in texts if t not in compiled])
    try:
        manifest = json.load(open(f"{folder}/target/manifest.json"))
    except (OSError, ValueError) as e:
        manifest = {"nodes": {}}
        print(f"  (no manifest: {e})")
    outputs = [n for n in manifest["nodes"].values() if n["resource_type"] == "model" and (n.get("config") or {}).get("alias")]
    ok("the output model's alias and schema in the manifest are the table typed in the flow, to the character",
       len(outputs) == 1 and outputs[0]["alias"] == JINJA_TABLE and outputs[0]["schema"] == schema,
       {"expected": [schema, JINJA_TABLE], "manifest": [[n["schema"], n["alias"]] for n in outputs]})
    _drop_jinja_tables(schema, sql)
    rc, out_run = dbt(folder, "run", variables=variables)
    ok("dbt run of the project with Jinja in its texts", rc == 0, out_run[-800:])
    console = out_compile + out_run
    in_files = {s: [os.path.relpath(os.path.join(r, f), folder) for r, _, fs in os.walk(folder) for f in fs
                    if s.encode() in open(os.path.join(r, f), "rb").read()] for s in JINJA_MARKS}
    ok("no Jinja of the flow ran: none of its markers, nor the secret, on the console or in a file (logs/, target/)",
       not any(s in console for s in JINJA_MARKS) and not any(in_files.values()),
       {"console": [s for s in JINJA_MARKS if s in console], "files": {s: f for s, f in in_files.items() if f}})
    table = f"{_ident(schema)}.{_ident(JINJA_TABLE)}"
    r = sql(f"SELECT json_build_object('x', x, 'col', {_ident(JINJA_COLUMN)}, 'n', count(*) OVER ()) FROM {table} LIMIT 1")
    try:
        row = json.loads(r.stdout.strip() or "null")
    except ValueError:
        row = None
    others = sql(f"SELECT table_name FROM information_schema.tables WHERE table_schema = '{schema}' AND table_name LIKE '%config(a=%'").stdout.split("\n")
    ok("dbt wrote the very table typed in the flow, and its values are the texts as typed",
       r.returncode == 0 and isinstance(row, dict) and row.get("n", 0) > 0
       and row.get("x") == "{{ log('zz' ~ 'expr', info=True) }}{{ env_var('ZZSEC_SEGRETO') }}" and row.get("col") == "{{ 7 * 7 }}",
       {"row": row, "error": r.stderr[-300:], "tables in the schema": [t for t in others if t]})
    _drop_jinja_tables(schema, sql)


def sentence(code: str, **params) -> str:
    """The gateway's sentence for a refused export, in English (the oracles send no Accept-Language),
    from its own catalogue."""
    import importlib.util
    from environment import REPO

    for name, path in (("app.core.language", "gateway/app/core/language.py"), ("dbt_messages", "gateway/app/services/dbt_messages.py")):
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, f"{REPO}/{path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            sys.modules[name] = module
    return sys.modules["dbt_messages"].translate(code, params, "en")


def detail(answer) -> str:
    """The `detail` of an error answer (the API helper keeps its body as text)."""
    text = answer.decode(errors="replace") if isinstance(answer, bytes) else str(answer)
    try:
        return str(json.loads(text).get("detail"))
    except (ValueError, AttributeError):
        return text


def note(what, present):
    """A KNOWN behaviour the user ACCEPTED (2026-10-10): it is recorded, it does not fail the run."""
    print(f"  ·  note (accepted): {what} → {'yes' if present else 'no'}", flush=True)


created = {"flows": [], "ds": [], "folders": [], "users": []}


def as_user(token, *a, **k):
    old = common.TOK
    common.TOK = token
    try:
        return _api(*a, **k)
    finally:
        common.TOK = old


def api(*a, **k):
    return as_user(ADMIN, *a, **k)


def wait_run(rid, seconds=300):
    end = time.time() + seconds
    while time.time() < end:
        st, r = api("GET", f"/runs/{rid}")
        if st == 200 and r["status"] in ("SUCCESS", "FAILURE"):
            return r
        time.sleep(2)
    return r


def main():
    global ADMIN
    sign_in()
    ADMIN = common.TOK
    try:
        ds = {d["name"]: d for d in api("GET", "/datasources")[1]}
        pg = next(c for c in api("GET", "/connections")[1] if c["db_type"] == "postgresql")
        st, priv = api("POST", "/projects", {"name": "zzsec privata"}); created["folders"].append(priv["id"])
        st, shared = api("POST", "/projects", {"name": "zzsec condivisa"}); created["folders"].append(shared["id"])
        ok("folders created", priv.get("id") and shared.get("id"), (priv, shared))

        # in the private one: data published by a flow then deleted
        defin = {"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": ds["orders"]["id"]}},
                           {"id": "l", "type": "operation", "data": {"opType": "limit", "params": {"n": 20}}},
                           {"id": "o", "type": "output", "data": {"destType": "datasource", "name": "zzsec dati segreti", "projectId": priv["id"], "overwrite": True}}],
                 "edges": [{"source": "s", "target": "l", "targetHandle": "left"}, {"source": "l", "target": "o", "targetHandle": "left"}]}
        st, fp = api("POST", f"/projects/{priv['id']}/flows", {"name": "zzsec segreto", "definition": json.dumps(defin), "engine": "duckdb"})
        r = wait_run(api("POST", f"/flows/{fp['id']}/run-now?mode=production")[1]["run_id"])
        ok("the private flow published the data", r["status"] == "SUCCESS", r)
        api("DELETE", f"/flows/{fp['id']}")
        secrets = next(d for d in api("GET", "/datasources")[1] if d["name"] == "zzsec dati segreti"); created["ds"].append(secrets["id"])
        ok("with the flow deleted, the datasource stays without an origin", secrets.get("flow_id") is None, secrets)
        st, q = api("POST", f"/projects/{priv['id']}/datasources/database", {"name": "zzsec query segreta", "connection_id": pg["id"], "source_type": "sql",
                                                                               "source_ref": "SELECT id, canale /* TESTO_SEGRETO_DELLA_QUERY */ FROM " + ds["orders"]["source_ref"]})
        created["ds"].append(q["id"])
        for _ in range(60):
            if next((x for x in api("GET", "/datasources")[1] if x["id"] == q["id"]), {}).get("rows"):
                break
            time.sleep(2)

        # in the shared one: a flow that reads them
        defin2 = {"nodes": [{"id": "a", "type": "source", "data": {"datasourceId": secrets["id"]}},
                            {"id": "b", "type": "source", "data": {"datasourceId": q["id"]}},
                            {"id": "o1", "type": "output", "data": {"destType": "datasource", "name": "zzsec uno", "projectId": shared["id"]}},
                            {"id": "o2", "type": "output", "data": {"destType": "datasource", "name": "zzsec due", "projectId": shared["id"]}}],
                  "edges": [{"source": "a", "target": "o1", "targetHandle": "left"}, {"source": "b", "target": "o2", "targetHandle": "left"}]}
        st, fc = api("POST", f"/projects/{shared['id']}/flows", {"name": "zzsec condiviso", "definition": json.dumps(defin2), "engine": "duckdb"})
        created["flows"].append(fc["id"])

        # the user who sees only the shared one
        st, u = api("POST", "/users", {"email": "zzsec-lettore@example.com", "password": "Lettore-di-prova-9", "full_name": "zzsec"})
        created["users"].append(u["id"])
        st, perm = api("POST", f"/projects/{shared['id']}/permissions", {"capability": CAP_VIEW, "user_id": u["id"]})
        ok("VIEW permission granted on the shared folder", st in (200, 201), (st, perm, u))
        tok = as_user(None, "POST", "/auth/login", {"email": "zzsec-lettore@example.com", "password": "Lettore-di-prova-9"})[1]["access_token"]
        st, _ = as_user(tok, "GET", f"/flows/{fc['id']}")
        ok("the reader sees the shared flow", st == 200, st)
        st, r = as_user(tok, "POST", "/tasks/export", {"bucket": secrets["bucket"], "input_key": secrets["key"], "operations": [], "format": "csv"}, raw=True)
        ok("usual fence: the reader can NOT download the private datasource (403)", st == 403, (st, r[:120] if isinstance(r, (bytes, str)) else r))

        # dbt export by the reader
        st, z = as_user(tok, "GET", f"/flows/{fc['id']}/export/dbt?target=duckdb", raw=True)
        if st == 200 and z[:2] == b"PK":
            files = zipfile.ZipFile(io.BytesIO(z))
            seed = [n for n in files.namelist() if n.startswith("seeds/") and n.endswith(".csv")]
            data = files.read(seed[0]).decode() if seed else ""
            models = "\n".join(files.read(n).decode() for n in files.namelist() if n.endswith(".sql"))
            ok("dbt export: NO data of the private folder in the seeds", not seed, f"{len(data.splitlines()) - 1} rows of private data in the seed {seed}: {data[:160]!r}")
            ok("dbt export: NO text of a private query in the models", "TESTO_SEGRETO_DELLA_QUERY" not in models, "the private query is in the project")
        else:
            ok("dbt export refused to the reader: it is for administrators only (403)", st == 403 and b"administrators" in (z if isinstance(z, bytes) else str(z).encode()), (st, z[:200] if isinstance(z, (bytes, str)) else z))

        # static OpenLineage by the reader
        st, ev = as_user(tok, "GET", f"/flows/{fc['id']}/openlineage")
        text = json.dumps(ev)
        ok("OpenLineage: no text of a private query", "TESTO_SEGRETO_DELLA_QUERY" not in text, "the private query is in the events")
        note("OpenLineage shows the reader names and folder of private objects (metadata only)",
             [x for x in ("zzsec dati segreti", "zzsec privata", "zzsec query segreta") if x in text])

        # dbt export with Jinja in the flow: whoever runs it would run someone else's code. Since #2 every text of the
        # flow is written so that dbt gives it back as it is: the flow exports, and nothing in it runs
        sample_postgres(f"CREATE SCHEMA IF NOT EXISTS {JINJA_SCHEMA}")
        nodes = [{"id": "s", "type": "source", "data": {"datasourceId": ds["orders"]["id"]}}, *jinja_operations(),
                 {"id": "o", "type": "output", "data": {"destType": "database", "connectionId": pg["id"],
                                                        "table": f"{JINJA_SCHEMA}.{JINJA_TABLE}", "mode": "replace"}}]
        chain = [n["id"] for n in nodes]
        st, fj = api("POST", f"/projects/{shared['id']}/flows", {"name": "zzsec jinja", "engine": "duckdb", "definition": json.dumps({
            "nodes": nodes, "edges": [{"source": a, "target": b, "targetHandle": "left"} for a, b in zip(chain, chain[1:])]})})
        created["flows"].append(fj["id"])
        folder, names = export(fj, "native", suffix="_jinja")
        if ok("dbt export (admin) of a flow with Jinja in its texts: a zip, not a refusal", folder is not None, names):
            check_jinja(folder, connection_passwords())

        # a quote in the native output's table: dbt-postgres writes a model's table between quotes in its own
        # SQL without escaping it (an incremental's second run, `dbt docs generate` failed). Refused, with the
        # gateway's sentence, before the download and at the download: never a project that breaks at dbt run
        nodes = [{"id": "s", "type": "source", "data": {"datasourceId": ds["orders"]["id"]}},
                 {"id": "l", "type": "operation", "data": {"opType": "limit", "params": {"n": 5}}},
                 {"id": "o", "type": "output", "data": {"destType": "database", "connectionId": pg["id"],
                                                        "table": f"{JINJA_SCHEMA}.{QUOTED_TABLE}", "mode": "replace"}}]
        st, fq = api("POST", f"/projects/{shared['id']}/flows", {"name": "zzsec apice", "engine": "duckdb", "definition": json.dumps({
            "nodes": nodes, "edges": [{"source": "s", "target": "l", "targetHandle": "left"}, {"source": "l", "target": "o", "targetHandle": "left"}]})})
        created["flows"].append(fq["id"])
        expected = sentence("name_not_quotable", name=QUOTED_TABLE, character="'", adapter="dbt-postgres")
        st, z = api("GET", f"/flows/{fq['id']}/export/dbt?target=native", raw=True)
        ok("a quote in the native output's table: the download is refused with name_not_quotable (dbt-postgres, ')",
           st == 422 and expected in detail(z), {"status": st, "answer": z[:300] if isinstance(z, (bytes, str)) else z, "expected": expected})
        plan = api("GET", f"/flows/{fq['id']}/export/dbt/plan")[1]
        native = next((t for t in (plan if isinstance(plan, dict) else {}).get("targets", []) if t["id"] == "native"), {"plan": plan})
        ok("… and the plan says it before the download: native not available, with the same reason",
           native.get("available") is False and native.get("reason") == expected, {"plan": native, "expected": expected})
        note("OpenLineage: the reader can send the events to the collector (POST)", as_user(tok, "POST", f"/flows/{fc['id']}/openlineage")[0] not in (403, 409))
    finally:
        for fid in created["flows"]:
            api("DELETE", f"/flows/{fid}")
        for d in api("GET", "/datasources")[1]:
            if d["name"].startswith("zzsec"):
                api("DELETE", f"/datasources/{d['id']}")
        for uid in created["users"]:
            api("DELETE", f"/users/{uid}")
        for pid in reversed(created["folders"]):
            api("DELETE", f"/projects/{pid}")
        _drop_jinja_tables(JINJA_SCHEMA)
    print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
    sys.exit(0 if all(outcomes) else 1)


if __name__ == "__main__":
    main()
