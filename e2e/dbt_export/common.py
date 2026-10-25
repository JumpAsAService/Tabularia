"""The helpers the dbt export oracles share: Tabularia's API, a REAL dbt in a throwaway
container (image E2E_DBT_IMAGE) on the stack's network, the export of a flow, the
comparison of a dbt model with the datasource Tabularia publishes (rows and, column by
column, the sum for numbers and the distinct values for the rest).
An empty text and NULL are two different values everywhere here (#85).
The configuration is in environment.py."""
import io, json, os, re, subprocess, sys, urllib.error, urllib.request, zipfile

from environment import BASE, WORKDIR, IMAGE, PASSWORD_FROM, NETWORK, env

# dbt runs in this time zone (TZ for dbt-duckdb, PGTZ for dbt-postgres): a project that is right only
# in UTC is wrong for most teams. The models must not depend on it (E2E_TZ=UTC turns it off)
TZ_NAME = env("E2E_TZ", "Europe/Rome")

outcomes = []
TOK = None


def ok(name, cond, detail=""):
    outcomes.append(bool(cond))
    print(("  ok " if cond else "  ✗  ") + name + ("" if cond else f"  → {str(detail)[:900]}"), flush=True)
    return bool(cond)




def api(m, p, c=None, raw=False, multipart=None):
    if multipart:
        boundary = "----tabularia-test"
        body = b""
        for name, (fname, data, ctype) in multipart.items():
            body += f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{fname}\"\r\nContent-Type: {ctype}\r\n\r\n".encode() + data + b"\r\n"
        body += f"--{boundary}--\r\n".encode()
        headers = {"content-type": f"multipart/form-data; boundary={boundary}"}
    else:
        body = json.dumps(c).encode() if c is not None else None
        headers = {"content-type": "application/json"}
    if TOK:
        headers["authorization"] = f"Bearer {TOK}"
    req = urllib.request.Request(BASE + p, method=m, data=body, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            x = r.read(); return r.status, (x if raw else (json.loads(x) if x else None))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")[:600]


def sign_in():
    global TOK
    code, body = api("POST", "/auth/login", {"email": env("AUTH__ADMIN_EMAIL"), "password": env("AUTH__ADMIN_PASSWORD")})
    if code != 200 or not isinstance(body, dict) or not body.get("access_token"):
        # the reason, instead of a TypeError: sign-in throttle, gateway restarting, wrong
        # password. A plain exception, not SystemExit: whoever waits for a gateway that
        # restarts (deterministic.restart) tries again
        raise RuntimeError(f"the administrator's sign-in failed: {code} {str(body)[:300]}")
    TOK = body["access_token"]


def container_env(name, key):
    out = subprocess.run(["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", name], capture_output=True, text=True).stdout
    return next((r.split("=", 1)[1] for r in out.splitlines() if r.startswith(key + "=")), None)


def connection_passwords():
    # clickhouse target: dbt signs in to the engine's ClickHouse with a user of its own (here the throwaway engine's)
    variables = {"CLICKHOUSE_USER": env("CLICKHOUSE_EXTERNAL__USERNAME") or "", "CLICKHOUSE_PASSWORD": env("CLICKHOUSE_EXTERNAL__PASSWORD") or ""}
    for c in api("GET", "/connections")[1]:
        cont, key = PASSWORD_FROM.get(c["db_type"], (None, None))
        if cont:
            variables[f"TABULARIA_DB_{c['id']}_PASSWORD"] = container_env(cont, key) or ""
    return variables


def signature(rows):
    """{column: rounded sum if all numbers, otherwise number of distinct values} + rows.
    NULLs and empty texts are counted apart: '' is a value, and folding it into NULL hid a seed
    that turned '' into NULL (#36, #85)."""
    if not rows:
        return {"rows": 0}
    out = {"rows": len(rows)}
    for col in rows[0]:
        vals = [{"true": 1, "false": 0}.get(str(r[col]).lower(), r[col]) for r in rows]   # booleans: dbt gives true/false, the CSV "true"/"false"
        nulls = sum(1 for v in vals if v is None)
        try:
            nums = [float(v) for v in vals if v is not None]
            out[col] = ("sum", round(sum(nums), 2), nulls)
        except (TypeError, ValueError):
            out[col] = ("distinct", len({str(v) for v in vals if v is not None}), nulls, sum(1 for v in vals if v == ""))
    return out


def dbt(folder, *arguments, variables=None):
    # with MY user, so the files dbt writes (target/, logs/) stay mine
    cmd = ["docker", "run", "--rm", "--network", NETWORK, "--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp",
           "-v", f"{folder}:/project", "-w", "/project", "-e", "DBT_PROFILES_DIR=/project", "-e", f"TZ={TZ_NAME}", "-e", f"PGTZ={TZ_NAME}"]
    for k, v in (variables or {}).items():
        cmd += ["-e", f"{k}={v}"]
    r = subprocess.run(cmd + [IMAGE, "dbt", *arguments, "--no-use-colors"], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


def export(flow, target, suffix=""):
    """Downloads the export's zip and opens it in a clean folder: (folder, file names) or (None, error)."""
    st, zipped = api("GET", f"/flows/{flow['id']}/export/dbt?target={target}", raw=True)
    if st != 200 or zipped[:2] != b"PK":
        return None, (st, zipped[:400] if isinstance(zipped, (bytes, str)) else zipped)
    slug = re.sub(r"[^a-z0-9]+", "_", flow["name"].lower()).strip("_")
    folder = f"{WORKDIR}/{slug}_{target}{suffix}"
    if os.path.exists(folder):
        subprocess.run(["docker", "run", "--rm", "-v", f"{WORKDIR}:/x", IMAGE, "sh", "-c", f"rm -rf /x/{os.path.basename(folder)}"], capture_output=True)
    os.makedirs(folder)
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        z.extractall(folder)
        return folder, sorted(z.namelist())


def model_rows(folder, model, variables):
    rc, out = dbt(folder, "show", "--inline", "select * from {{ ref('%s') }}" % model, "--limit", "200000", "--output", "json", "--quiet", variables=variables)
    start = out.index("{"); data = json.loads(out[start:])
    return data.get("show") or data.get("data") or []


_FIELD = re.compile(r'(?:"((?:[^"]|"")*)"|([^,"\r\n]*))(,|\r\n|\n|\r|$)')


def read_csv(text: str) -> list[list]:
    """The rows of a CSV as Tabularia writes it (Polars): a quoted empty field `""` is the empty
    text, an empty field is NULL. csv.reader gives '' for both, and Python 3.12's QUOTE_NOTNULL
    does not tell them apart when reading either."""
    rows, row, pos = [], [], 0
    while pos < len(text):
        m = _FIELD.match(text, pos)
        quoted, bare, end = m.group(1), m.group(2), m.group(3)
        row.append(quoted.replace('""', '"') if quoted is not None else (bare or None))
        pos = m.end()
        if end != ",":
            rows.append(row)
            row = []
            if not end:
                break
    if row:
        rows.append(row)
    return rows


def datasource_rows(d):
    st, csv_bytes = api("POST", "/tasks/export", {"bucket": d["bucket"], "input_key": d["key"], "operations": [], "format": "csv", "filename": "x.csv"}, raw=True)
    if st != 200:
        raise RuntimeError(f"CSV export of the datasource: {st} {csv_bytes}")
    head, *rows = read_csv(csv_bytes.decode())
    return [dict(zip(head, r)) for r in rows]


def compare(folder, model, datasource, variables):
    """The dbt model ↔ Tabularia's datasource: same signature?"""
    try:
        f1 = signature(model_rows(folder, model, variables))
    except Exception as e:  # noqa: BLE001
        return ok(f"rows of the model «{model}» readable", False, e)
    f2 = signature(datasource_rows(datasource))
    keys = sorted(set(f1) | set(f2))
    return ok(f"«{model}»: dbt and Tabularia give the same result ({f2['rows']} rows, {len(keys) - 1} columns)",
              all(f1.get(k) == f2.get(k) for k in keys),
              "\n".join(f"    {k}: dbt={f1.get(k)} tabularia={f2.get(k)}" for k in keys if f1.get(k) != f2.get(k)))


def model_slug(name):
    return re.sub(r"[^a-z0-9_]", "_", name.lower()).strip("_")


# ── the engine's ClickHouse (clickhouse target) ──────────────────────────────
def engine_clickhouse(sql: str) -> subprocess.CompletedProcess:
    """SQL to the engine's ClickHouse, on the client's INPUT: inside `sh -c "…"` the
    backticks of the names would be the shell's command substitutions."""
    from environment import CH_CONTAINER

    cmd = ["docker", "exec", "-i", CH_CONTAINER, "sh", "-c", 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery']
    return subprocess.run(cmd, input=sql, capture_output=True, text=True)


def clickhouse_databases() -> set:
    return set(engine_clickhouse("SELECT name FROM system.databases FORMAT TSV").stdout.split())


def clean_clickhouse():
    """Drops the databases of dbt and of the federated sources, and checks they are gone."""
    names = [n for n in clickhouse_databases() if n == "dbt_tabularia" or n.startswith("tab_src_")]
    if names:
        r = engine_clickhouse("".join(f"DROP DATABASE IF EXISTS `{n}`;\n" for n in names))
        if r.returncode != 0:
            raise RuntimeError(f"ClickHouse cleanup failed: {r.stderr[-300:]}")
    left = [n for n in clickhouse_databases() if n == "dbt_tabularia" or n.startswith("tab_src_")]
    if left:
        raise RuntimeError(f"ClickHouse cleanup: {left} are still there")


def sample_postgres(sql: str) -> subprocess.CompletedProcess:
    """SQL to the sample Postgres, on psql's input."""
    from environment import PG_CONTAINER

    return subprocess.run(["docker", "exec", "-i", PG_CONTAINER, "sh", "-c", 'psql -q -t -A -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'],
                          input=sql, capture_output=True, text=True)


def wait_run(rid, seconds=900):
    import time

    end = time.time() + seconds
    while time.time() < end:
        st, r = api("GET", f"/runs/{rid}")
        if st == 200 and r.get("status") in ("SUCCESS", "FAILURE", "REVOKED"):
            return r
        time.sleep(2)
    return {"status": "TIMEOUT"}


def summary():
    print(f"\n{sum(outcomes)}/{len(outcomes)} checks passed")
    sys.exit(0 if all(outcomes) else 1)
