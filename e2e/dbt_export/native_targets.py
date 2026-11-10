"""The second round on the dbt export, on the angles the other oracles do not reach (Kubernetes or
compose; on kind give it a pod and a work folder of its own, see the bottom of this text):

  A. a DESTINATION TABLE THAT EXISTS ALREADY, on the native Postgres target. Tabularia's Output
     node writes into the customer's table and keeps it (replace: TRUNCATE + INSERT by name;
     append: INSERT by name, the columns the flow lacks take their DEFAULT). The model dbt runs
     for that same table must leave it as Tabularia leaves it: the column types, NOT NULL,
     DEFAULT, the primary key and CHECK, the indexes, the grants, the comments, a view that
     reads it, the values as the table's types store them. An Output on ANOTHER connection
     (decision #44) is an ordinary model in the project's schema, never its real table — also
     when that other connection is the very database dbt runs on.
  B. the same on the native CLICKHOUSE target: Tabularia's replace keeps the table's engine,
     ORDER BY, column types and comment (a staging copy of its definition, EXCHANGE TABLES).
  C. the DATA CONTRACT on every target — federated DuckDB, native Postgres, the ClickHouse
     target and native ClickHouse — rule by rule the verdict Tabularia's check gives: whole-value
     patterns with escapes (`\\d`, `\\.`, POSIX classes, `\\b`, `(?i)`), accepted values on
     float, date, datetime and boolean columns, unique with NULLs (one and two columns), ranges
     on dates and instants, an expression with a division, names with a dash and accents.
  D. MYSQL / MARIADB SOURCES (a throwaway MariaDB in the namespace E2E_MYSQL_NAMESPACE,
     default zz-hunt-mysql, created and removed here): DECIMAL, TINYINT(1), BIT, DATETIME and
     TIMESTAMP with a server time zone that is not UTC, '0000-00-00', a case-insensitive
     collation ('A' and 'a' in filters, distinct and group by), ENUM, SET, JSON, a BIGINT
     UNSIGNED over 2^63, utf8mb4 (emoji), NULL — on the federated and the ClickHouse targets,
     compared with what Tabularia imports and publishes.

Every object is named zzn… (flows, datasources, folder, connections), the Postgres schemas zzn_…,
the ClickHouse tables zzn_…; all of it is removed at the end, with the dbt pod and the MariaDB.
dbt writes in the schema / database `zzn_dbt` (never the shared `dbt_tabularia`).

usage (kind):
  E2E_KUBE_CONTEXT=kind-tabularia E2E_DBT_POD=zz-dbt-hunt-native \\
  E2E_WORKDIR=$PWD/e2e/dbt_export/.work/hunt-native \\
  E2E_CH_SETTINGS=join_algorithm=grace_hash,max_threads=2 \\
  python3 e2e/dbt_export/native_targets.py [dest_pg] [dest_ch] [contracts] [mysql]
(no argument: all four parts)"""
import base64, io, json, os, re, secrets, shlex, subprocess, sys, time, zipfile

from common import (api, sign_in, ok, outcomes, dbt, connection_passwords, sample_postgres, sample_clickhouse,
                    engine_clickhouse, datasource_rows, clickhouse_settings, remove_folder, wait_run, kubectl, exec_in,
                    test_outcomes, test_slug as _slug, reach_sources, kube_cleanup)
from environment import WORKDIR, KUBE, KUBE_NAMESPACE, DBT_POD, CH_HOST

PARTS = [a for a in sys.argv[1:] if not a.startswith("-")] or ["dest_pg", "dest_ch", "contracts", "mysql"]
ENGINE = os.environ.get("E2E_ENGINE", "clickhouse")       # the customers' engine is the reference
SCHEMA = "zzn_dbt"                                         # where dbt builds its models (never dbt_tabularia)
MYSQL_NS = os.environ.get("E2E_MYSQL_NAMESPACE", "zz-hunt-mysql")
OWNER = "zzn-hunt-native"                                 # the label of this oracle's NetworkPolicy
E = lambda a, b, h="left": {"source": a, "target": b, "targetHandle": h}

if KUBE:
    from common import load_image
    trouble = load_image()
    if trouble:
        raise SystemExit(trouble)
sign_in()
CONN = {c["name"]: c for c in api("GET", "/connections")[1]}
PG = next(c for c in CONN.values() if c["db_type"] == "postgresql" and c["name"].startswith("Sample"))
CH = next(c for c in CONN.values() if c["db_type"] == "clickhouse" and c["name"].startswith("Sample"))
st, folder_obj = api("POST", "/projects", {"name": "zzn native"})
assert st in (200, 201), (st, folder_obj)
C = folder_obj["id"]
created = {"flows": [], "connections": []}
VARIABLES = connection_passwords()


# ── helpers ──────────────────────────────────────────────────────────────────
def psql(sql):
    r = sample_postgres(sql)
    return r


def psql_rows(sql):
    """Rows of a query on the sample Postgres, each a tuple of texts (psql -A, `|` between fields)."""
    r = sample_postgres(sql)
    if r.returncode != 0:
        raise RuntimeError(f"psql: {r.stderr[-400:]}")
    return [tuple(line.split("|")) for line in r.stdout.splitlines() if line.strip()]


def ch_sample(sql):
    r = sample_clickhouse(sql)
    if r.returncode != 0:
        raise RuntimeError(f"clickhouse: {r.stderr[-400:]}")
    return [tuple(line.split("\t")) for line in r.stdout.splitlines() if line.strip()]


def flow(name, nodes, edges, engine=ENGINE):
    st, f = api("POST", f"/projects/{C}/flows", {"name": name, "definition": json.dumps({"nodes": nodes, "edges": edges}), "engine": engine})
    assert st in (200, 201), (st, f)
    created["flows"].append(f["id"])
    return f


def launch(f):
    st, r = api("POST", f"/flows/{f['id']}/run-now?mode=production")
    if st not in (200, 201, 202):
        return {"status": "REFUSED", "detail": r}
    return wait_run(r["run_id"])


def ds(name, seconds=180):
    """The datasource once it has its rows; None when it does not get them (a failed import: no rows and
    not refreshing for 20 s)."""
    start = time.time()
    idle = None
    while time.time() < start + seconds:
        d = next((x for x in api("GET", "/datasources")[1] if x["name"] == name and x["project_id"] == C), None)
        if d and d.get("rows") is not None and not d.get("refreshing"):
            return d
        if d and not d.get("refreshing"):
            idle = idle or time.time()
            if time.time() - idle > 20:
                return None
        else:
            idle = None
        time.sleep(2)
    return None


def table_datasource(name, conn, ref, source_type="table"):
    st, d = api("POST", f"/projects/{C}/datasources/database", {"name": name, "connection_id": conn["id"], "source_type": source_type, "source_ref": ref})
    if st not in (200, 201):
        return None, (st, d)
    return ds(name), None


def export(f, target, tag, **options):
    """The project with the dialog's options (POST), the models in SCHEMA: (folder, file names) or (None, why)."""
    body = {"target": target, "schema": SCHEMA, **options}
    st, zipped = api("POST", f"/flows/{f['id']}/export/dbt", body, raw=True)
    if st != 200 or not isinstance(zipped, bytes) or zipped[:2] != b"PK":
        return None, (st, zipped[:400] if isinstance(zipped, (bytes, str)) else zipped)
    folder = os.path.join(WORKDIR, f"{tag}_{target}")
    remove_folder(folder)
    os.makedirs(folder)
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        z.extractall(folder)
        names = sorted(z.namelist())
    clickhouse_settings(f"{folder}/profiles.yml")
    return folder, names


def run(folder, *arguments, variables=None):
    return dbt(folder, *arguments, variables=variables or VARIABLES)


def errors_of(out):
    return "\n".join(l for l in out.splitlines() if re.search(r"(?i)error|exception", l))[:900] or out[-900:]


def model_file(folder, model):
    for root, _, files in os.walk(os.path.join(folder, "models")):
        if f"{model}.sql" in files:
            return open(os.path.join(root, f"{model}.sql")).read()
    return ""


def show(folder, sql, variables=None):
    """`dbt show --inline` → list of dicts."""
    rc, out = run(folder, "show", "--inline", sql, "--limit", "100000", "--output", "json", "--quiet", variables=variables)
    if rc != 0 or "{" not in out:
        raise RuntimeError(errors_of(out))
    data = json.loads(out[out.index("{"):])
    return data.get("show") or data.get("data") or []


# ═════════════════════════════════════════════════════════════════════════════
# A. existing destination tables, native Postgres
# ═════════════════════════════════════════════════════════════════════════════
PG_SETUP = r"""
DROP SCHEMA IF EXISTS zzn_src CASCADE; DROP SCHEMA IF EXISTS zzn_dest CASCADE; DROP SCHEMA IF EXISTS zzn_dbt CASCADE;
DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zzn_lettore') THEN
  EXECUTE 'DROP OWNED BY zzn_lettore'; EXECUTE 'DROP ROLE zzn_lettore'; END IF; END $$;
CREATE ROLE zzn_lettore NOLOGIN;
CREATE SCHEMA zzn_src; CREATE SCHEMA zzn_dest;
CREATE TABLE zzn_src.base (id integer, nome text, importo double precision, quando timestamp);
INSERT INTO zzn_src.base VALUES
 (1, 'Anna',  10.0 / 3,  '2024-01-15 10:30:00'),
 (2, 'Bruno', 2.675,     '2024-03-31 01:30:00'),
 (3, 'Carla', 0.004,     NULL),
 (4, 'Dario', 1234.5678, '2024-07-01 23:59:59.5');
-- the customer's table a replace output writes: types narrower than the flow's, NOT NULL, a key,
-- a CHECK, a column with a DEFAULT the flow does not have, an index, a grant, comments, a view
CREATE TABLE zzn_dest.clienti (
  id integer PRIMARY KEY,
  nome varchar(20) NOT NULL,
  importo numeric(10,2) CHECK (importo >= 0),
  quando timestamptz,
  nota text NOT NULL DEFAULT 'caricato da Tabularia'
);
CREATE INDEX zzn_clienti_nome ON zzn_dest.clienti (nome);
COMMENT ON TABLE zzn_dest.clienti IS 'anagrafica del cliente';
COMMENT ON COLUMN zzn_dest.clienti.nome IS 'ragione sociale';
GRANT USAGE ON SCHEMA zzn_dest TO zzn_lettore;
GRANT SELECT ON zzn_dest.clienti TO zzn_lettore;
CREATE VIEW zzn_dest.v_clienti AS SELECT id, nome FROM zzn_dest.clienti;
-- a table another table references: Tabularia's TRUNCATE refuses it
CREATE TABLE zzn_dest.padre (id integer PRIMARY KEY, nome text, importo double precision, quando timestamp);
INSERT INTO zzn_dest.padre VALUES (1, 'x', 0, NULL);
CREATE TABLE zzn_dest.figlio (id integer PRIMARY KEY, padre_id integer REFERENCES zzn_dest.padre (id));
INSERT INTO zzn_dest.figlio VALUES (10, 1);
-- append outputs: one with an audit column the flow lacks (DEFAULT now()), one with a varchar(20)
-- read by a view and an index
CREATE TABLE zzn_dest.storico (id integer NOT NULL, nome varchar(20), importo numeric(10,2), quando timestamptz,
                               caricato timestamptz NOT NULL DEFAULT now());
CREATE TABLE zzn_dest.storico2 (id integer NOT NULL, nome varchar(20) NOT NULL, importo numeric(10,2), quando timestamptz);
CREATE INDEX zzn_storico2_nome ON zzn_dest.storico2 (nome);
CREATE VIEW zzn_dest.v_storico2 AS SELECT nome, count(*) AS n FROM zzn_dest.storico2 GROUP BY nome;
-- the table an Output on ANOTHER connection (the same database) writes
CREATE TABLE zzn_dest.altra (id integer PRIMARY KEY, nome varchar(20), importo numeric(10,2), quando timestamptz);
CREATE VIEW zzn_dest.v_altra AS SELECT id FROM zzn_dest.altra;
"""


def pg_definition(schema, table):
    """What Tabularia keeps of the customer's table: columns (order, type, NOT NULL, DEFAULT),
    constraints, indexes, grants to zzn_lettore, comments, the views that read it."""
    rel = f"'{schema}.{table}'::regclass"
    exists = psql_rows(f"SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                       f"WHERE n.nspname = '{schema}' AND c.relname = '{table}'")[0][0] == "1"
    if not exists:
        return {"exists": False}
    return {
        "columns": psql_rows(f"SELECT column_name, data_type, coalesce(character_maximum_length::text, ''), "
                             f"coalesce(numeric_precision::text, '') || ',' || coalesce(numeric_scale::text, ''), is_nullable, "
                             f"coalesce(column_default, '') FROM information_schema.columns "
                             f"WHERE table_schema = '{schema}' AND table_name = '{table}' ORDER BY ordinal_position"),
        "constraints": sorted(psql_rows(f"SELECT contype, pg_get_constraintdef(oid) FROM pg_constraint WHERE conrelid = {rel}")),
        "referenced by": sorted(psql_rows(f"SELECT conrelid::regclass::text, pg_get_constraintdef(oid) FROM pg_constraint WHERE confrelid = {rel}")),
        "indexes": sorted(psql_rows(f"SELECT indexname FROM pg_indexes WHERE schemaname = '{schema}' AND tablename = '{table}'")),
        "grants": sorted(psql_rows(f"SELECT grantee, privilege_type FROM information_schema.role_table_grants "
                                   f"WHERE table_schema = '{schema}' AND table_name = '{table}' AND grantee = 'zzn_lettore'")),
        "comments": psql_rows(f"SELECT coalesce(obj_description({rel}, 'pg_class'), ''), "
                              f"coalesce((SELECT string_agg(a.attname || '=' || col_description({rel}, a.attnum), ',') "
                              f"FROM pg_attribute a WHERE a.attrelid = {rel} AND a.attnum > 0 AND col_description({rel}, a.attnum) IS NOT NULL), '')"),
        "views": sorted(psql_rows(f"SELECT DISTINCT v.relname FROM pg_depend d JOIN pg_rewrite r ON r.oid = d.objid "
                                  f"JOIN pg_class v ON v.oid = r.ev_class WHERE d.refobjid = {rel} AND v.oid <> {rel}")),
    }


def pg_data(schema, table, columns):
    return psql_rows(f"SELECT {', '.join(columns)} FROM {schema}.{table} ORDER BY 1, 2")


def differences(before, after):
    return "\n".join(f"    {k}: Tabularia {before.get(k)}\n    {' ' * len(k)}  dbt       {after.get(k)}"
                     for k in sorted(set(before) | set(after)) if before.get(k) != after.get(k))


def part_dest_pg():
    print("A. existing destination tables on native Postgres", flush=True)
    r = psql(PG_SETUP)
    if not ok("A · the customer's tables created", r.returncode == 0, r.stderr[-400:]):
        return
    base, why = table_datasource("zzn base", PG, "zzn_src.base")
    if not ok("A · source zzn_src.base imported", base is not None, why):
        return
    src = {"id": "s", "type": "source", "data": {"datasourceId": base["id"]}}
    out = lambda table, mode, conn=PG: {"id": "o", "type": "output", "data": {"destType": "database", "connectionId": conn["id"], "table": table, "mode": mode}}
    data_cols = ["id", "nome", "importo::text", "quando AT TIME ZONE 'UTC'"]

    # A1. replace into the customer's table
    f1 = flow("zzn replace", [src, out("zzn_dest.clienti", "replace")], [E("s", "o")])
    r = launch(f1)
    ok("A1 · Tabularia writes the customer's table (replace)", r["status"] == "SUCCESS", r)
    tab_def, tab_data = pg_definition("zzn_dest", "clienti"), pg_data("zzn_dest", "clienti", data_cols + ["nota"])
    print(f"    Tabularia leaves: {json.dumps(tab_def)[:600]}")
    folder, names = export(f1, "native", "zzn_replace")
    if ok("A1 · native export", folder is not None, names):
        header = model_file(folder, "clienti").split("\n", 1)[0]
        ok("A1 · the model writes the very table (schema zzn_dest, alias clienti)", "schema='zzn_dest'" in header and "alias='clienti'" in header, header)
        readme = open(f"{folder}/README.md").read()
        rc, out_ = run(folder, "run")
        ok("A1 · dbt run", rc == 0, errors_of(out_))
        dbt_def, dbt_data = pg_definition("zzn_dest", "clienti"), None
        try:
            dbt_data = pg_data("zzn_dest", "clienti", data_cols)
        except RuntimeError as e:
            dbt_data = str(e)
        # the user's choice (2026-11-10): dbt rebuilds the customer's table its own way, and the project says
        # so. What the rebuild changes is printed, the checks are that it is said and that the rows are there
        lost = differences({k: tab_def.get(k) for k in ("columns", "constraints", "indexes", "grants", "comments", "views")},
                           {k: dbt_def.get(k) for k in ("columns", "constraints", "indexes", "grants", "comments", "views")})
        print(f"    what dbt's rebuild changes (documented):\n{lost}")
        ok("A1 · after dbt run the table holds the flow's rows (ids and names as Tabularia writes them)",
           [r[:2] for r in tab_data] == ([r[:2] for r in dbt_data] if isinstance(dbt_data, list) else dbt_data),
           f"\n    Tabularia {[r[:2] for r in tab_data]}\n    dbt       {dbt_data if not isinstance(dbt_data, list) else [r[:2] for r in dbt_data]}")
        ok("A1 · the model's header says dbt rebuilds the table, and what is lost", "rebuilds `zzn_dest.clienti`" in model_file(folder, "clienti")
           and "views and foreign keys" in model_file(folder, "clienti"), model_file(folder, "clienti")[:600])
        ok("A1 · the README says that dbt rebuilds the customer's table instead of emptying it, and what is lost",
           re.search(r"(?i)(rebuil|recreat|drop).{0,200}(constraint|index|grant|view|type)", readme) is not None,
           "no note: " + " | ".join(l for l in readme.splitlines() if "zzn_dest" in l)[:400])

    # A2. replace into a table another table references with a foreign key
    f2 = flow("zzn replace fk", [src, out("zzn_dest.padre", "replace")], [E("s", "o")])
    r = launch(f2)
    tab_refused = r["status"] != "SUCCESS"
    ok("A2 · Tabularia refuses to empty a table another table references (TRUNCATE)", tab_refused, r.get("status"))
    before = pg_definition("zzn_dest", "padre")
    folder, names = export(f2, "native", "zzn_replace_fk")
    if ok("A2 · native export", folder is not None, names):
        readme = open(f"{folder}/README.md").read()
        rc, out_ = run(folder, "run")
        after = pg_definition("zzn_dest", "padre")
        # where Tabularia's TRUNCATE stops, dbt's rebuild drops the foreign key (CASCADE): documented
        print(f"    dbt run rc={rc}; {differences({'referenced by': before.get('referenced by')}, {'referenced by': after.get('referenced by')})}")
        ok("A2 · the README says dbt drops the foreign keys that read the table it rebuilds",
           "foreign keys" in readme and "zzn_dest.padre" in readme, readme[-800:])

    # A3. append into a table with an audit column the flow lacks (DEFAULT now())
    f3 = flow("zzn append audit", [src, out("zzn_dest.storico", "append")], [E("s", "o")])
    r = launch(f3)
    ok("A3 · Tabularia appends to a table with a column the flow lacks (it takes its DEFAULT)", r["status"] == "SUCCESS", r)
    n_tab = int(psql_rows("SELECT count(*) FROM zzn_dest.storico")[0][0])
    folder, names = export(f3, "native", "zzn_append_audit")
    if ok("A3 · native export", folder is not None, names):
        readme = open(f"{folder}/README.md").read()
        rc, out_ = run(folder, "run")
        n_dbt = int(psql_rows("SELECT count(*) FROM zzn_dest.storico")[0][0])
        # documented: an append into a table with a column the model lacks fails (dbt does not take its DEFAULT)
        ok(f"A3 · the README says an append fails on a table with a column the model lacks, and dbt run does "
           f"(rc={rc}, rows {n_tab} → {n_dbt})", "a column the model lacks" in readme and (rc != 0 or n_dbt == 2 * n_tab),
           errors_of(out_)[:500])

    # A4. append into a varchar(20) column read by a view and an index
    f4 = flow("zzn append view", [src, out("zzn_dest.storico2", "append")], [E("s", "o")])
    r = launch(f4)
    ok("A4 · Tabularia appends to zzn_dest.storico2", r["status"] == "SUCCESS", r)
    before = pg_definition("zzn_dest", "storico2")
    folder, names = export(f4, "native", "zzn_append_view")
    if ok("A4 · native export", folder is not None, names):
        header = model_file(folder, "storico2").split("\n", 1)[0]
        ok("A4 · the model is the incremental of the very table", "incremental" in header and "alias='storico2'" in header, header)
        rc, out_ = run(folder, "run")
        ok("A4 · dbt run", rc == 0, errors_of(out_))
        after = pg_definition("zzn_dest", "storico2")
        ok("A4 · after an incremental dbt run the table keeps its constraints", before.get("constraints") == after.get("constraints"),
           differences({"constraints": before.get("constraints")}, {"constraints": after.get("constraints")}))
        # documented: dbt-postgres widens a column by a new one and the old dropped with CASCADE (its index, the view)
        print("    after an incremental run (documented: dbt-postgres widens a column, CASCADE): "
              + (differences({k: before.get(k) for k in ("columns", "indexes", "views")}, {k: after.get(k) for k in ("columns", "indexes", "views")})
                 or "unchanged"))
        readme = open(f"{folder}/README.md").read()
        ok("A4 · the README says dbt-postgres widens the column types, and that --full-refresh rebuilds the table",
           "widens the column types" in readme and "--full-refresh" in readme, readme[-800:])

    # A5. decision #44: an Output on ANOTHER connection — here one to the very same database
    st, other = api("POST", f"/projects/{C}/connections", {"name": "zzn altra connessione", "db_type": "postgresql", "host": PG["host"],
                                                            "port": PG["port"], "database": PG["database"], "username": PG["username"],
                                                            "password": VARIABLES[f"TABULARIA_DB_{PG['id']}_PASSWORD"], "db_schema": "zzn_dest"})
    if not ok("A5 · a second connection to the same database", st in (200, 201), (st, other)):
        return
    created["connections"].append(other["id"])
    f5 = flow("zzn altra", [src, out("altra", "replace", conn=other)], [E("s", "o")])
    r = launch(f5)
    ok("A5 · Tabularia writes zzn_dest.altra through the other connection", r["status"] == "SUCCESS", r)
    before = pg_definition("zzn_dest", "altra")
    variables = {**VARIABLES, f"TABULARIA_DB_{other['id']}_PASSWORD": VARIABLES[f"TABULARIA_DB_{PG['id']}_PASSWORD"]}
    folder, names = export(f5, "native", "zzn_altra")
    if ok("A5 · native export", folder is not None, names):
        models = [n for n in names if n.startswith("models/") and n.endswith(".sql")]
        text = "".join(open(f"{folder}/{n}").read() for n in models)
        ok("A5 · the output on the other connection is an ordinary model: no schema, no alias in its config",
           "alias=" not in text and "schema=" not in text, [l for l in text.splitlines() if "config(" in l])
        readme = open(f"{folder}/README.md").read()
        ok("A5 · the README says loading it into the other connection is the data team's job", "data team's job" in readme,
           readme[-600:])
        rc, out_ = run(folder, "run", variables=variables)
        ok("A5 · dbt run", rc == 0, errors_of(out_))
        after = pg_definition("zzn_dest", "altra")
        ok("A5 · dbt leaves the real table of the other connection alone (same database)", before == after, differences(before, after))
        built = psql_rows(f"SELECT table_name FROM information_schema.tables WHERE table_schema = '{SCHEMA}'")
        ok(f"A5 · the model is in the project's schema {SCHEMA}", ("altra",) in built or any("altra" in t[0] for t in built), built)


def clean_pg():
    psql("DROP SCHEMA IF EXISTS zzn_src CASCADE; DROP SCHEMA IF EXISTS zzn_dest CASCADE; DROP SCHEMA IF EXISTS zzn_dbt CASCADE; "
         "DROP SCHEMA IF EXISTS zzn_c CASCADE; "
         "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'zzn_lettore') THEN "
         "EXECUTE 'DROP OWNED BY zzn_lettore'; EXECUTE 'DROP ROLE zzn_lettore'; END IF; END $$;")


# ═════════════════════════════════════════════════════════════════════════════
# B. existing destination tables, native ClickHouse (the sample CRM's server)
# ═════════════════════════════════════════════════════════════════════════════
CH_SETUP = """
DROP TABLE IF EXISTS analytics.zzn_v_agenti; DROP TABLE IF EXISTS analytics.zzn_dest_agenti; DROP TABLE IF EXISTS analytics.zzn_dest_storico;
CREATE TABLE analytics.zzn_dest_agenti (id UInt32, nome String, ruolo LowCardinality(String), provvigione Decimal(5, 2),
  quota Decimal(10, 2), aggiornato DateTime DEFAULT toDateTime('2024-01-01 00:00:00'))
  ENGINE = ReplacingMergeTree ORDER BY id COMMENT 'tabella del cliente';
CREATE VIEW analytics.zzn_v_agenti AS SELECT id, nome FROM analytics.zzn_dest_agenti;
CREATE TABLE analytics.zzn_dest_storico (id UInt32, ruolo String, nome String, provvigione Decimal(5, 2), quota Float64)
  ENGINE = MergeTree ORDER BY id;
"""


def ch_definition(table):
    rows = ch_sample(f"SELECT engine_full, sorting_key, comment FROM system.tables WHERE database = 'analytics' AND name = '{table}' FORMAT TSV")
    if not rows:
        return {"exists": False}
    return {"engine": rows[0][0].split(" SETTINGS")[0], "order by": rows[0][1], "comment": rows[0][2] if len(rows[0]) > 2 else "",
            "columns": ch_sample(f"SELECT name, type, default_expression FROM system.columns WHERE database = 'analytics' AND table = '{table}' "
                                 f"ORDER BY position FORMAT TSV")}


def part_dest_ch():
    print("B. existing destination tables on native ClickHouse", flush=True)
    try:
        ch_sample(CH_SETUP)
    except RuntimeError as e:
        ok("B · the customer's tables created", False, e)
        return
    agents = next((x for x in api("GET", "/datasources")[1] if x.get("source_ref") == "agenti" and x.get("connection_id") == CH["id"]), None)
    if not ok("B · the sample CRM's agenti datasource", agents is not None):
        return
    steps = [{"id": "s", "type": "source", "data": {"datasourceId": agents["id"]}},
             {"id": "f", "type": "operation", "data": {"opType": "filter", "params": {"column": "id", "operator": "le", "value": 20}}},
             {"id": "p", "type": "operation", "data": {"opType": "select", "params": {"columns": ["id", "nome", "ruolo", "provvigione_base_pct"]}}},
             {"id": "r", "type": "operation", "data": {"opType": "rename", "params": {"mapping": {"provvigione_base_pct": "provvigione"}}}},
             {"id": "c", "type": "operation", "data": {"opType": "compute", "params": {"columns": [{"name": "quota", "expr": "provvigione / 3"}]}}}]
    chain = [E("s", "f"), E("f", "p"), E("p", "r"), E("r", "c"), E("c", "o")]
    out = lambda table, mode: {"id": "o", "type": "output", "data": {"destType": "database", "connectionId": CH["id"], "table": table, "mode": mode}}
    data_sql = "SELECT id, nome, ruolo, toString(provvigione), toString(quota) FROM analytics.{} ORDER BY id, nome FORMAT TSV"

    # B1. replace into the customer's table
    f1 = flow("zzn ch replace", steps + [out("zzn_dest_agenti", "replace")], chain)
    r = launch(f1)
    ok("B1 · Tabularia writes the customer's ClickHouse table (replace)", r["status"] == "SUCCESS", r)
    tab_def, tab_data = ch_definition("zzn_dest_agenti"), ch_sample(data_sql.format("zzn_dest_agenti"))
    print(f"    Tabularia leaves: {json.dumps(tab_def)[:500]}")
    folder, names = export(f1, "native", "zzn_ch_replace")
    if ok("B1 · native ClickHouse export", folder is not None, names):
        rc, out_ = run(folder, "run")
        ok("B1 · dbt run", rc == 0, errors_of(out_))
        dbt_def = ch_definition("zzn_dest_agenti")
        print(f"    what dbt's rebuild changes (documented):\n{differences(tab_def, dbt_def)}")
        try:
            dbt_ids = ch_sample("SELECT id, nome FROM analytics.zzn_dest_agenti ORDER BY id, nome FORMAT TSV")
        except RuntimeError as e:
            dbt_ids = str(e)
        ok("B1 · after dbt run the table holds the flow's rows", [r[:2] for r in tab_data] == dbt_ids, f"dbt {dbt_ids[:3]}")
        ok("B1 · the model's header says dbt rebuilds the table, engine and ORDER BY included",
           "rebuilds `analytics.zzn_dest_agenti`" in model_file(folder, "zzn_dest_agenti") and "ORDER BY" in model_file(folder, "zzn_dest_agenti"),
           model_file(folder, "zzn_dest_agenti")[:500])
        ok("B1 · the view that reads the table still answers", bool(ch_sample("SELECT count() FROM analytics.zzn_v_agenti FORMAT TSV")))

    # B2. append into a table whose columns are in another order (same names, same family)
    f2 = flow("zzn ch append", steps + [out("zzn_dest_storico", "append")], chain)
    r = launch(f2)
    ok("B2 · Tabularia appends to a table with the columns in another order (by name)", r["status"] == "SUCCESS", r)
    check = "SELECT count(), countIf(nome IN ('Agente', 'Responsabile', 'Direttore') OR ruolo NOT IN (SELECT DISTINCT ruolo FROM analytics.agenti)) FROM analytics.zzn_dest_storico FORMAT TSV"
    tab_rows = ch_sample(check)
    folder, names = export(f2, "native", "zzn_ch_append")
    if ok("B2 · native ClickHouse export", folder is not None, names):
        rc, out_ = run(folder, "run")
        ok("B2 · dbt run (incremental, the table exists)", rc == 0, errors_of(out_))
        dbt_rows = ch_sample(check)
        # dbt-clickhouse inserts by position: the values land in the wrong columns. No standard way to make it
        # insert by name without an enforced contract (every column's exact type): a warning in the project
        print(f"    rows and misplaced values: Tabularia {tab_rows}, after dbt {dbt_rows} (documented: by position)")
        readme, model = open(f"{folder}/README.md").read(), model_file(folder, "zzn_dest_storico")
        ok("B2 · the README and the model's header warn that dbt-clickhouse appends by position",
           "WARNING" in readme and "by position" in readme and "by position" in model, readme[-600:])


def clean_ch():
    try:
        ch_sample("DROP TABLE IF EXISTS analytics.zzn_v_agenti; DROP TABLE IF EXISTS analytics.zzn_dest_agenti; "
                  "DROP TABLE IF EXISTS analytics.zzn_dest_storico; DROP TABLE IF EXISTS analytics.zzn_contratti; "
                  f"DROP DATABASE IF EXISTS {SCHEMA}")
    except RuntimeError:
        pass


# ═════════════════════════════════════════════════════════════════════════════
# C. the data contract on every target
# ═════════════════════════════════════════════════════════════════════════════
CONTRACT_ROWS = [
    # id, codice, nota, cifre, persona, testo, importo, giorno, quando, flag, codice2, cat, sub, intero, "Col-strana è"
    (1, "A1", "x.y", "123", "MARIO ROSSI", "riga", 1.5, "2024-01-15", "2024-06-01 10:30:00", True, "u1", None, "a", 7, "sì"),
    (2, "B22", "xzy", "4567", "ROSSI ANNA", "riga1\nriga2", 2.0, "2024-02-29", "2024-06-01 11:30:00", False, "u2", None, "a", 8, "no"),
    (3, "C333", None, "0", "PAOLO ROSSI JR", None, None, None, None, None, None, "x", "a", None, "sì"),
    (4, None, "a.b", None, None, "ok", 3.25, "2024-01-15", "2024-06-01 10:30:00", True, None, "x", "b", 7, "no"),
    (5, "D4", None, "89", "ROSSI", "x", 1.5, None, None, False, "u3", None, "c", 8, "sì"),
]
CONTRACT_RULES = [
    {"id": "r1", "kind": "pattern", "column": "codice", "regex": r"[A-Z]\d+", "severity": "error"},            # passes: \d escaped
    {"id": "r2", "kind": "pattern", "column": "codice", "regex": r"(?i)[a-z]\d+", "severity": "error"},       # passes: inline flag
    {"id": "r3", "kind": "pattern", "column": "nota", "regex": r"[a-z]\.[a-z]", "severity": "error"},         # 'xzy': fails
    {"id": "r4", "kind": "pattern", "column": "cifre", "regex": r"[[:digit:]]+", "severity": "error"},        # passes: POSIX class
    {"id": "r5", "kind": "pattern", "column": "persona", "regex": r".*\bROSSI\b.*", "severity": "warning"},   # passes: word boundary
    {"id": "r6", "kind": "pattern", "column": "testo", "regex": r".*", "severity": "warning"},                # a newline: fails
    {"id": "r7", "kind": "accepted_values", "column": "importo", "values": [1.5, "2", 3.25], "severity": "error"},       # passes
    {"id": "r8", "kind": "accepted_values", "column": "giorno", "values": ["2024-01-15", "2024-02-29"], "severity": "error"},  # passes
    {"id": "r9", "kind": "range", "column": "giorno", "min": "2024-01-01", "max": "2024-12-31", "severity": "error"},   # passes
    {"id": "r10", "kind": "range", "column": "quando", "min": "2024-06-01T11:00:00+02:00", "severity": "warning"},       # 09:00 UTC: passes
    {"id": "r11", "kind": "accepted_values", "column": "quando", "values": ["2024-06-01 10:30:00.000000", "2024-06-01 11:30:00.000000"], "severity": "error"},
    {"id": "r12", "kind": "accepted_values", "column": "flag", "values": ["true", 0], "severity": "error"},          # passes
    {"id": "r13", "kind": "unique", "columns": ["codice2"], "severity": "warning"},                                # two NULLs: fails
    {"id": "r14", "kind": "unique", "columns": ["cat", "sub"], "severity": "error"},                               # (NULL, a) twice: fails
    {"id": "r15", "kind": "expression", "sql": "intero IS NULL OR intero / 4 > 1", "severity": "error"},           # 7 / 4 = 1.75: passes
    {"id": "r16", "kind": "not_null", "column": "Col-strana è", "severity": "error"},                              # passes
    {"id": "r17", "kind": "accepted_values", "column": "Col-strana è", "values": ["sì", "no"], "severity": "error"},  # passes
    {"id": "r18", "kind": "range", "column": "intero", "min": 0.5, "max": 7.5, "severity": "warning"},             # 8: fails
    {"id": "r19", "kind": "pattern", "column": "importo", "regex": r"\d+\.\d+", "severity": "error"},             # 2.0 as text: passes
    {"id": "r20", "kind": "pattern", "column": "giorno", "regex": r"\d{4}-\d{2}-\d{2}", "severity": "error"},     # passes
    {"id": "r21", "kind": "pattern", "column": "flag", "regex": "true|false", "severity": "error"},               # passes
    {"id": "r22", "kind": "range", "column": "quando", "min": "2024-06-01 09:00", "severity": "error"},           # no seconds: passes
    {"id": "r23", "kind": "range", "column": "quando", "max": "2024-06-01T12:00:00Z", "severity": "error"},       # a Z: passes
]


def _pg_lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return "E'" + v.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def _ch_lit(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + v.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n") + "'"


def contract_tables():
    values = ",\n ".join("(" + ", ".join(_pg_lit(v) for v in r) + ")" for r in CONTRACT_ROWS)
    r = psql("DROP SCHEMA IF EXISTS zzn_c CASCADE; CREATE SCHEMA zzn_c;\n"
             'CREATE TABLE zzn_c.contratti (id integer, codice text, nota text, cifre text, persona text, testo text, importo double precision, '
             'giorno date, quando timestamp, flag boolean, codice2 text, cat text, sub text, intero integer, "Col-strana è" text);\n'
             f"INSERT INTO zzn_c.contratti VALUES\n {values};")
    if r.returncode != 0:
        raise RuntimeError(r.stderr[-400:])
    values = ", ".join("(" + ", ".join(_ch_lit(v) for v in r) + ")" for r in CONTRACT_ROWS)
    ch_sample("DROP TABLE IF EXISTS analytics.zzn_contratti;\n"
              "CREATE TABLE analytics.zzn_contratti (id Int32, codice Nullable(String), nota Nullable(String), cifre Nullable(String), "
              "persona Nullable(String), testo Nullable(String), importo Nullable(Float64), giorno Nullable(Date32), "
              "quando Nullable(DateTime64(6)), flag Nullable(Bool), codice2 Nullable(String), cat Nullable(String), sub Nullable(String), "
              "intero Nullable(Int32), `Col-strana è` Nullable(String)) ENGINE = MergeTree ORDER BY id;\n"
              f"INSERT INTO analytics.zzn_contratti VALUES {values};")


def part_contracts():
    print("C. the data contract, rule by rule, on every target", flush=True)
    try:
        contract_tables()
    except RuntimeError as e:
        ok("C · the contract tables created", False, e)
        return
    why = reach_sources([(PG["host"], PG.get("port") or 5432)], owner=OWNER) if KUBE else None
    ok("C · the engine's ClickHouse reaches the sample Postgres", why is None, why)
    cases = []
    for label, conn, ref, targets in (("pg", PG, "zzn_c.contratti", ("duckdb", "native", "clickhouse")),
                                      ("ch", CH, "zzn_contratti", ("native", "clickhouse"))):
        src, why = table_datasource(f"zzn contratti {label}", conn, ref)
        if not ok(f"C · {label}: source imported", src is not None, why):
            continue
        f = flow(f"zzn contratto {label}", [{"id": "s", "type": "source", "data": {"datasourceId": src["id"]}},
                                             {"id": "o", "type": "output", "data": {"destType": "datasource", "name": f"zzn contratto {label}",
                                                                                     "projectId": C, "overwrite": True}}], [E("s", "o")])
        r = launch(f)
        if not ok(f"C · {label}: the flow ran", r["status"] == "SUCCESS", r):
            continue
        out = ds(f"zzn contratto {label}")
        st, c = api("PUT", f"/datasources/{out['id']}/contract", {"document": {"rules": CONTRACT_RULES}, "enabled": True})
        ok(f"C · {label}: contract saved", st == 200, (st, c))
        st, rep = api("POST", f"/datasources/{out['id']}/contract/check")
        tab = {}
        for x in (rep.get("report") or {}).get("rules", []) if isinstance(rep, dict) else []:
            tab[x.get("id")] = "pass" if x.get("passed") else ("warn" if x.get("severity") == "warning" else "fail")
            if x.get("error"):
                print(f"    Tabularia cannot evaluate {x.get('id')}: {x['error']}")
        ok(f"C · {label}: Tabularia evaluated every rule", len(tab) == len(CONTRACT_RULES), rep)
        print(f"    Tabularia ({label}): {tab}")
        for target in targets:
            folder, names = export(f, target, f"zzn_contratto_{label}")
            if not ok(f"C · {label} · {target}: export", folder is not None, names):
                continue
            if target == "clickhouse" and os.path.exists(f"{folder}/macros/create_tabularia_sources.sql"):
                rc, o = run(folder, "run-operation", "create_tabularia_sources", "--log-level-file", "none")
                ok(f"C · {label} · {target}: create_tabularia_sources", rc == 0, errors_of(o))
            rc, o = run(folder, "run")
            if not ok(f"C · {label} · {target}: dbt run", rc == 0, errors_of(o)):
                continue
            rc, o = run(folder, "test")
            got = test_outcomes(folder, f"zzn_contratto_{label}", CONTRACT_RULES)
            for rule in CONTRACT_RULES:
                ok(f"C · {label} · {target} · {rule['id']} {rule['kind']} {rule.get('regex') or rule.get('sql') or rule.get('values') or rule.get('min') or ''}: "
                   f"Tabularia {tab.get(rule['id'])}, dbt {got.get(rule['id'])}",
                   tab.get(rule["id"]) == got.get(rule["id"]),
                   next((l for l in o.splitlines() if rule["id"] in l and ("ERROR" in l or "FAIL" in l or "WARN" in l)), ""))


# ═════════════════════════════════════════════════════════════════════════════
# D. MySQL / MariaDB sources (a throwaway MariaDB)
# ═════════════════════════════════════════════════════════════════════════════
MARIADB = f"{MYSQL_NS}/zz-mariadb"
MARIADB_HOST = f"zz-mariadb.{MYSQL_NS}.svc.cluster.local"
MYSQL_SETUP = r"""
DROP TABLE IF EXISTS tipi; DROP TABLE IF EXISTS zeri; DROP TABLE IF EXISTS grandi;
CREATE TABLE tipi (
  id INT PRIMARY KEY, prezzo DECIMAL(12,4), attivo TINYINT(1), vero BOOL, bitto BIT(1), bit8 BIT(8),
  quando_dt DATETIME, quando_ts TIMESTAMP NULL DEFAULT NULL, giorno DATE,
  nome VARCHAR(50) COLLATE utf8mb4_general_ci, colore ENUM('rosso','verde','blu'), tags SET('a','b','c'), doc JSON,
  grande BIGINT UNSIGNED, testo VARCHAR(50) CHARACTER SET utf8mb4, vuoto VARCHAR(10)
) DEFAULT CHARSET = utf8mb4 COLLATE = utf8mb4_general_ci;
SET time_zone = '+01:00';
INSERT INTO tipi VALUES
 (1, 1234.5678, 1, TRUE, b'1', b'10000001', '2024-01-15 10:30:00', '2024-01-15 10:30:00', '2024-01-15',
  'Alfa', 'rosso', 'a,b', '{"k": 1, "s": "x"}', 9223372036854775807, '😀 ciao', ''),
 (2, -0.0001, 0, FALSE, b'0', b'00000000', '2024-07-01 23:59:59', '2024-07-01 23:59:59', '2024-02-29',
  'alfa', 'verde', '', '[1, 2]', 1, 'città ✓', NULL),
 (3, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL),
 (4, 99999999.9999, 2, TRUE, b'1', b'11111111', '1969-12-31 23:00:00', '2038-01-19 03:14:07', '1969-12-31',
  'ALFA ', 'blu', 'c', 'null', 42, '👍🏽 ok', 'x'),
 (5, 0.5, 1, TRUE, b'0', b'00000010', '2024-01-15 10:30:00', '2024-01-15 10:30:00', '2024-01-15',
  'Beta', 'rosso', 'a', '{}', 0, 'β', 'y');
-- MySQL's «zero» dates, among real ones
CREATE TABLE zeri (id INT PRIMARY KEY, zero_dt DATETIME, zero_d DATE);
INSERT INTO zeri VALUES (1, '2024-01-01 00:00:00', '2024-01-01'), (2, '0000-00-00 00:00:00', '0000-00-00'), (3, NULL, NULL);
-- a BIGINT UNSIGNED over 2^63 - 1
CREATE TABLE grandi (id INT PRIMARY KEY, grande BIGINT UNSIGNED);
INSERT INTO grandi VALUES (1, 5), (2, 9223372036854775808), (3, 18446744073709551615), (4, NULL);
"""


def mariadb(sql):
    """SQL to the throwaway MariaDB (its password stays in the pod's environment)."""
    r = exec_in(MARIADB, 'mariadb -uroot -p"$MARIADB_ROOT_PASSWORD" --default-character-set=utf8mb4 zzn_shop', sql)
    if r.returncode != 0:
        raise RuntimeError(f"mariadb: {r.stderr[-400:]}")
    return r.stdout


def mariadb_password():
    r = kubectl("-n", MYSQL_NS, "get", "secret", "zz-mariadb", "-o", "jsonpath={.data.password}")
    return base64.b64decode(r.stdout).decode() if r.returncode == 0 and r.stdout else None


def start_mariadb():
    """A MariaDB 11.4 with a server time zone that is not UTC, in a namespace of its own; the password
    random, in a Secret, never printed. None when it answers; otherwise why not."""
    if kubectl("get", "namespace", MYSQL_NS).returncode != 0:
        kubectl("create", "namespace", MYSQL_NS)
    if not mariadb_password():
        secret = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "zz-mariadb", "namespace": MYSQL_NS},
                  "type": "Opaque", "stringData": {"password": secrets.token_urlsafe(18)}}
        kubectl("apply", "-f", "-", input=json.dumps(secret))
    env = [{"name": "MARIADB_ROOT_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "zz-mariadb", "key": "password"}}},
           {"name": "MARIADB_USER", "value": "zzn"},
           {"name": "MARIADB_PASSWORD", "valueFrom": {"secretKeyRef": {"name": "zz-mariadb", "key": "password"}}},
           {"name": "MARIADB_DATABASE", "value": "zzn_shop"}]
    deploy = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "zz-mariadb", "namespace": MYSQL_NS},
              "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "zz-mariadb"}}, "template": {"metadata": {"labels": {"app": "zz-mariadb"}},
                       "spec": {"automountServiceAccountToken": False, "containers": [{
                           "name": "db", "image": "docker.io/library/mariadb:11.4", "imagePullPolicy": "IfNotPresent",
                           "args": ["--default-time-zone=+01:00", "--character-set-server=utf8mb4", "--collation-server=utf8mb4_general_ci"],
                           "env": env, "ports": [{"containerPort": 3306}],
                           "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": "768Mi"}},
                           "readinessProbe": {"exec": {"command": ["sh", "-c", 'mariadb-admin ping -uroot -p"$MARIADB_ROOT_PASSWORD" --silent']},
                                              "periodSeconds": 5}}]}}}}
    service = {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "zz-mariadb", "namespace": MYSQL_NS},
               "spec": {"selector": {"app": "zz-mariadb"}, "ports": [{"port": 3306, "targetPort": 3306}]}}
    for obj in (deploy, service):
        r = kubectl("apply", "-f", "-", input=json.dumps(obj))
        if r.returncode != 0:
            return r.stderr[-300:]
    r = kubectl("-n", MYSQL_NS, "rollout", "status", "deploy/zz-mariadb", "--timeout=240s")
    return None if r.returncode == 0 else r.stderr[-300:]


_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:?\d{2})?$")


def norm(v):
    """A value as text, the same whatever wrote it (Tabularia's CSV, dbt show's JSON)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, sort_keys=True)
    s = str(v)
    if s.lower() in ("true", "false"):
        return s.lower()
    m = _STAMP.match(s)
    if m:
        frac = (m.group(3) or ".0")[1:].ljust(6, "0")[:6]
        return f"{m.group(1)} {m.group(2)}.{frac}" + ("" if m.group(4) in (None, "Z", "+00:00", "+0000") else m.group(4))
    if re.fullmatch(r"-?\d+", s):
        return str(int(s))
    try:
        f = float(s)
        return repr(f) if f == f else "nan"
    except ValueError:
        return s


def by_column(tab_rows, dbt_rows, key="id"):
    """{column: [(key, Tabularia, dbt)]} where they differ (the rows matched on `key`)."""
    t = {norm(r[key]): r for r in tab_rows}
    d = {norm(r[key]): r for r in dbt_rows}
    diff = {}
    columns = list(tab_rows[0].keys()) if tab_rows else []
    for c in columns:
        for k in sorted(set(t) & set(d), key=str):     # the rows on one side only are said once, below
            a, b = norm((t.get(k) or {}).get(c)), norm((d.get(k) or {}).get(c, "<missing column>"))
            if a != b:
                diff.setdefault(c, []).append((k, a, b))
    if set(t) != set(d):
        diff["<rows>"] = [(f"only Tabularia {[t[k] for k in sorted(set(t) - set(d), key=str)][:3]}",
                           f"only dbt {[d[k] for k in sorted(set(d) - set(t), key=str)][:3]}")]
    return diff


def part_mysql():
    print("D. MySQL / MariaDB sources (federated and ClickHouse targets)", flush=True)
    why = start_mariadb()
    if not ok("D · the throwaway MariaDB answers", why is None, why):
        return
    # the image's first start runs a temporary server while it sets the root password, then restarts, and
    # the readiness probe (mariadb-admin ping answers even «access denied») passes before it is done:
    # tried again (access denied, a refused connection, a TLS pipe broken by the restart)
    for attempt in range(40):
        try:
            mariadb(MYSQL_SETUP)
            break
        except RuntimeError as e:
            if attempt == 39 or not re.search(r"Access denied|Can't connect|Lost connection|TLS/SSL error|ERROR 2002|ERROR 2013", str(e)):
                ok("D · the MariaDB table created", False, e)
                return
            time.sleep(3)
    password = mariadb_password()
    st, conn = api("POST", f"/projects/{C}/connections", {"name": "zzn mariadb", "db_type": "mariadb", "host": MARIADB_HOST, "port": 3306,
                                                           "database": "zzn_shop", "username": "zzn", "password": password})
    if not ok("D · a MariaDB connection in Tabularia", st in (200, 201), (st, conn)):
        return
    created["connections"].append(conn["id"])
    variables = {**VARIABLES, f"TABULARIA_DB_{conn['id']}_PASSWORD": password}
    zeri, why = table_datasource("zzn my zeri", conn, "zeri", )
    # not a dbt matter: Tabularia's own import of a table with '0000-00-00' among real dates (PyMySQL gives
    # the zero date as text, the real ones as datetime). Only said, the dbt cases need an imported table
    print(f"    Tabularia imports a table with '0000-00-00' among real dates: {'yes' if zeri else 'NO (the import fails)'}")
    grandi, why = table_datasource("zzn my grandi", conn, "grandi")
    print(f"    Tabularia imports a BIGINT UNSIGNED over 2^63 - 1: {'yes' if grandi else 'NO (the import fails)'}")
    tipi, why = table_datasource("zzn my tipi", conn, "tipi")
    if not ok("D · the MariaDB table imported", tipi is not None, why):
        return
    print("    Tabularia's types: " + ", ".join(f"{c['name']} {c.get('dtype')}" for c in tipi.get("columns") or []))
    query, why = table_datasource("zzn my query", conn, "SELECT nome, COUNT(*) AS n FROM tipi WHERE nome = 'alfa' GROUP BY nome", source_type="sql")
    ok("D · a datasource from a MariaDB query", query is not None, why)
    src = lambda d, i="s": {"id": i, "type": "source", "data": {"datasourceId": d["id"]}}
    outp = lambda name, i="o": {"id": i, "type": "output", "data": {"destType": "datasource", "name": name, "projectId": C, "overwrite": True}}
    nobit = {"id": "b", "type": "operation", "data": {"opType": "drop", "params": {"columns": ["bitto", "bit8"]}}}
    flows = {
        # every column as it is
        # a BIT is Binary in Tabularia and passes through as the warehouse has it (documented: a type the export
        # cannot name), and Tabularia's ClickHouse engine cannot write a BIT(8) over 0x7F (not UTF-8): left out
        "zzn my copia": ([src(tipi), nobit, outp("zzn my copia")], [E("s", "b"), E("b", "o")]),
        # a case-insensitive collation: Tabularia compares bytes once the data is imported
        "zzn my filtro": ([src(tipi), nobit, {"id": "f", "type": "operation", "data": {"opType": "filter", "params": {"column": "nome", "operator": "eq", "value": "alfa"}}},
                           outp("zzn my filtro")], [E("s", "b"), E("b", "f"), E("f", "o")]),
        "zzn my gruppi": ([src(tipi), {"id": "g", "type": "operation", "data": {"opType": "group_by", "params": {"by": ["nome"], "aggregations": [
                               {"column": "id", "func": "count", "alias": "n"}]}}}, outp("zzn my gruppi")], [E("s", "g"), E("g", "o")]),
        "zzn my distinti": ([src(tipi), {"id": "p", "type": "operation", "data": {"opType": "select", "params": {"columns": ["nome", "colore"]}}},
                             {"id": "u", "type": "operation", "data": {"opType": "unique", "params": {}}}, outp("zzn my distinti")],
                            [E("s", "p"), E("p", "u"), E("u", "o")]),
    }
    if query is not None:
        flows["zzn my query"] = ([src(query), outp("zzn my query out")], [E("s", "o")])
    plan_checked = False
    for name, (nodes, edges) in flows.items():
        f = flow(name, nodes, edges)
        r = launch(f)
        if not ok(f"D · {name}: Tabularia runs it", r["status"] == "SUCCESS", r):
            continue
        out_name = name + (" out" if name == "zzn my query" else "")
        published = ds(out_name)
        tab_rows = datasource_rows(published)
        key = "id" if tab_rows and "id" in tab_rows[0] else next(iter(tab_rows[0])) if tab_rows else "id"
        if not plan_checked:
            st, plan = api("GET", f"/flows/{f['id']}/export/dbt/plan")
            targets = {t.get("id") or t.get("target"): t for t in (plan.get("targets") or [])} if isinstance(plan, dict) and isinstance(plan.get("targets"), list) else (plan or {}).get("targets") if isinstance(plan, dict) else None
            print(f"    plan targets: {json.dumps(targets)[:400]}")
            plan_checked = True
        for target in ("duckdb", "clickhouse"):
            folder, names = export(f, target, _slug(name))
            if not ok(f"D · {name} · {target}: export", folder is not None, names):
                continue
            if target == "clickhouse" and os.path.exists(f"{folder}/macros/create_tabularia_sources.sql"):
                rc, o = run(folder, "run-operation", "create_tabularia_sources", "--log-level-file", "none", variables=variables)
                if not ok(f"D · {name} · {target}: create_tabularia_sources", rc == 0, errors_of(o)):
                    continue
            rc, o = run(folder, "run", variables=variables)
            if not ok(f"D · {name} · {target}: dbt run", rc == 0, errors_of(o)):
                continue
            try:
                got = show(folder, "select * from {{ ref('%s') }}" % _slug(out_name), variables=variables)
            except RuntimeError as e:
                ok(f"D · {name} · {target}: the model is readable", False, e)
                continue
            if name == "zzn my query" and target == "clickhouse":
                # documented: a MySQL query on ClickHouse compares text by bytes, MySQL ignored the case
                ok(f"D · {name} · {target}: the README says ClickHouse compares the query's text by bytes (a known limit)",
                   "compares text by bytes" in open(f"{folder}/README.md").read())
                continue
            if key not in (got[0] if got else {}):
                key = next(iter(tab_rows[0]))
            diff = by_column(tab_rows, got, key)
            cols_t, cols_d = list(tab_rows[0].keys()) if tab_rows else [], list(got[0].keys()) if got else []
            ok(f"D · {name} · {target}: the same columns in the same order", cols_t == cols_d, f"Tabularia {cols_t}\n    dbt       {cols_d}")
            if not diff:
                ok(f"D · {name} · {target}: the same values as Tabularia ({len(tab_rows)} rows)", True)
            for column, items in diff.items():
                ok(f"D · {name} · {target}: " + ("the same rows as Tabularia" if column == "<rows>" else f"column «{column}» as Tabularia publishes it"), False,
                   "; ".join(f"{k}: Tabularia {a!r} dbt {b!r}" for k, a, b in items[:4]) if column != "<rows>" else "\n    ".join(items[0]))


# ═════════════════════════════════════════════════════════════════════════════
def cleanup():
    """Everything this oracle made — also what an earlier run left (E2E_KEEP=1, an interrupted run):
    the folders «zzn native», their flows and datasources, the connections «zzn …»."""
    folders = [p["id"] for p in (api("GET", "/projects")[1] or []) if p.get("name") == "zzn native"]
    for f in api("GET", "/flows")[1]:
        if f.get("project_id") in folders:
            api("DELETE", f"/flows/{f['id']}")
    for d in api("GET", "/datasources")[1]:
        if d.get("project_id") in folders:
            api("DELETE", f"/datasources/{d['id']}")
    mine = [c for c in api("GET", "/connections")[1] if c["name"].startswith("zzn ")]
    if KUBE:
        engine_clickhouse("".join(f"DROP DATABASE IF EXISTS `{n}`;\n" for n in engine_clickhouse("SELECT name FROM system.databases FORMAT TSV").stdout.split()
                                  if any(n.startswith(f"tab_src_{c['id']}_") for c in mine)))
    for c in mine:
        api("DELETE", f"/connections/{c['id']}")
    for fid in folders:
        api("DELETE", f"/projects/{fid}")
    clean_pg()
    clean_ch()
    if KUBE:
        engine_clickhouse(f"DROP DATABASE IF EXISTS tab_src_{PG['id']}_zzn_c; DROP DATABASE IF EXISTS tab_src_{PG['id']}_zzn_src; DROP DATABASE IF EXISTS {SCHEMA}")
        kubectl("delete", "namespace", MYSQL_NS, "--ignore-not-found", "--wait=true", "--timeout=180s")
        # this oracle's dbt pod and NetworkPolicy only: the other oracles' stay
        kube_cleanup(owner=OWNER)


if __name__ == "__main__":
    try:
        if "dest_pg" in PARTS:
            part_dest_pg()
        if "dest_ch" in PARTS:
            part_dest_ch()
        if "contracts" in PARTS:
            part_contracts()
        if "mysql" in PARTS:
            part_mysql()
    finally:
        if os.environ.get("E2E_KEEP") != "1":
            cleanup()
    print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
    sys.exit(0 if all(outcomes) else 1)
