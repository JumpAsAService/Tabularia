"""The helpers the dbt export oracles share: Tabularia's API, a REAL dbt (image E2E_DBT_IMAGE)
in a throwaway container on the stack's network or, on Kubernetes, in the oracles' pod,
SQL on the sample databases and on the engine's ClickHouse, the export of a flow, the
comparison of a dbt model with the datasource Tabularia publishes (rows and, column by
column, the sum for numbers and the distinct values for the rest).
An empty text and NULL are two different values everywhere here (#85).
The configuration is in environment.py."""
import io, json, os, re, shlex, shutil, subprocess, sys, tarfile, time, urllib.error, urllib.request, zipfile

from environment import BASE, WORKDIR, IMAGE, PASSWORD_FROM, NETWORK, HERE, KUBE, KUBE_NAMESPACE, KIND_CLUSTER, DBT_POD, DBT_MEMORY, CH_SETTINGS, env, kubectl

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


# ── where things run: Docker compose or Kubernetes (environment.py) ──────────
def exec_in(target, shell, input=None) -> subprocess.CompletedProcess:
    """`sh -c shell` inside a sample database (or the engine's ClickHouse), with `input` on its
    stdin: `docker exec` on a container, `kubectl exec` on a deployment `namespace/name`."""
    if KUBE:
        ns, name = target.split("/", 1)
        return kubectl("-n", ns, "exec", "-i", f"deploy/{name}", "--", "sh", "-c", shell, input=input or "")
    return subprocess.run(["docker", "exec", "-i", target, "sh", "-c", shell], input=input or "", capture_output=True, text=True)


def present(target) -> bool:
    """The container, or the deployment, is there."""
    if KUBE:
        ns, name = target.split("/", 1)
        return kubectl("-n", ns, "get", "deploy", name).returncode == 0
    return subprocess.run(["docker", "inspect", target], capture_output=True).returncode == 0


def container_env(name, key):
    """A variable of a sample database's environment (its password): read here, never printed."""
    if KUBE:
        r = exec_in(name, f"printenv {shlex.quote(key)}")
        return r.stdout.rstrip("\n") if r.returncode == 0 else None
    out = subprocess.run(["docker", "inspect", "--format", "{{range .Config.Env}}{{println .}}{{end}}", name], capture_output=True, text=True).stdout
    return next((r.split("=", 1)[1] for r in out.splitlines() if r.startswith(key + "=")), None)


def remove_folder(folder):
    """An exported project's folder, gone. On compose dbt may have written in it as another user
    (an old container): a container removes it."""
    if not os.path.exists(folder):
        return
    if KUBE:
        shutil.rmtree(folder)
        return
    subprocess.run(["docker", "run", "--rm", "-v", f"{os.path.dirname(folder)}:/x", IMAGE, "sh", "-c", f"rm -rf /x/{shlex.quote(os.path.basename(folder))}"],
                   capture_output=True)


def run_dbt_image(folder, command, variables=None, retry_memory=False):
    """`command` (e.g. ["dbt", "run"] or ["python", "/dump.py", …]) in the dbt image, in the project
    `folder` seen as /project, with dump.py as /dump.py: (exit code, console).
    `retry_memory`, on Kubernetes only: the sample ClickHouse of kind has 2 GiB and other work runs
    on it, so a MEMORY_LIMIT_EXCEEDED is that stack's limit, not the export's — waited out and run
    again (three times at most)."""
    base = {"HOME": "/tmp", "DBT_PROFILES_DIR": "/project", "TZ": TZ_NAME, "PGTZ": TZ_NAME}
    if KUBE:
        for attempt in range(4 if retry_memory else 1):
            rc, out = _run_in_pod(folder, command, {**base, **(variables or {})})
            if "MEMORY_LIMIT_EXCEEDED" not in out or attempt == 3 or not retry_memory:
                return rc, out
            print(f"  (the ClickHouse of the stack is out of memory: again in {20 * (attempt + 1)} s)", flush=True)
            time.sleep(20 * (attempt + 1))
    # with MY user, so the files dbt writes (target/, logs/) stay mine
    cmd = ["docker", "run", "--rm", "--network", NETWORK, "--user", f"{os.getuid()}:{os.getgid()}",
           "-v", f"{folder}:/project", "-v", f"{HERE}/dump.py:/dump.py:ro", "-w", "/project"]
    for k, v in {**base, **(variables or {})}.items():
        cmd += ["-e", f"{k}={v}"]
    r = subprocess.run(cmd + [IMAGE, *command], capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


# On Kubernetes dbt runs in ONE pod (DBT_POD, in the app's namespace: the NetworkPolicies of the
# sample databases let that namespace in). Each call sends the project folder (and dump.py, and
# the variables in a file of their own OUTSIDE the project, removed before dbt starts, so that
# the oracles that look for a password in the project's files never find the one they sent)
# and gets the folder back as dbt left it: target/, logs/, the .duckdb file.
_RUN = r"""
set -e
rm -rf /project /e2e-result && mkdir -p /e2e-result
tar -x -C / -f -
set -a; . /e2e-env; set +a; rm -f /e2e-env
cd /project
set +e
"$@" > /e2e-result/out 2>&1
echo $? > /e2e-result/rc
tar -c -C / project e2e-result >&3
"""


def _tar_add_bytes(tar, name, data, mode=0o644):
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = len(data), mode, int(time.time())
    tar.addfile(info, io.BytesIO(data))


def _run_in_pod(folder, command, variables):
    ensure_runner()
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w") as tar:
        tar.add(folder, arcname="project")
        tar.add(os.path.join(HERE, "dump.py"), arcname="dump.py")
        _tar_add_bytes(tar, "e2e-env", "".join(f"{k}={shlex.quote(str(v))}\n" for k, v in variables.items()).encode(), 0o600)
    # the folder comes back on fd 3, the console of kubectl (errors) on stderr
    r = kubectl("-n", KUBE_NAMESPACE, "exec", "-i", DBT_POD, "--", "sh", "-c", "exec 3>&1 1>&2; " + _RUN, "sh", *command,
                input=payload.getvalue(), text=False)
    try:
        back = tarfile.open(fileobj=io.BytesIO(r.stdout), mode="r")
        members = back.getmembers()
    except tarfile.TarError:
        raise RuntimeError(f"the dbt pod gave nothing back (kubectl exit {r.returncode}): {r.stderr.decode(errors='replace')[-600:]}")
    out = back.extractfile("e2e-result/out").read().decode(errors="replace")
    rc = int(back.extractfile("e2e-result/rc").read().decode().strip() or 1)
    # the folder as dbt left it: emptied, then filled again with what came back
    for name in os.listdir(folder):
        path = os.path.join(folder, name)
        shutil.rmtree(path) if os.path.isdir(path) and not os.path.islink(path) else os.remove(path)
    inside = [m for m in members if m.name.startswith("project/") and (m.isfile() or m.isdir())]
    for m in inside:
        m.name = m.name[len("project/"):]
    back.extractall(folder, members=inside, filter="data")
    return rc, out


def _runner_phase():
    r = kubectl("-n", KUBE_NAMESPACE, "get", "pod", DBT_POD, "-o", "jsonpath={.status.phase}")
    return r.stdout.strip() if r.returncode == 0 else None


def kube_node():
    """The node the dbt pod runs on (E2E_KUBE_NODE, or the first worker)."""
    node = env("E2E_KUBE_NODE")
    if node:
        return node
    names = kubectl("get", "nodes", "-o", "jsonpath={.items[*].metadata.name}").stdout.split()
    workers = [n for n in names if "control-plane" not in n]
    return (workers or names)[0]


def _image_ref():
    return IMAGE if "/" in IMAGE.split(":")[0] else f"docker.io/library/{IMAGE}"


def load_image() -> str | None:
    """The dbt image on the pod's node (kind: `kind load docker-image` on that node only, if it is
    not there or is an older build). None if it is there; otherwise what went wrong."""
    if not KIND_CLUSTER:
        return None          # the nodes pull it from a registry
    node = kube_node()
    local = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", IMAGE], capture_output=True, text=True).stdout.strip()
    ref = _image_ref() + ("" if ":" in IMAGE.rsplit("/", 1)[-1] else ":latest")
    there = subprocess.run(["docker", "exec", node, "crictl", "inspecti", "-o", "json", ref], capture_output=True, text=True)
    if there.returncode == 0 and local and json.loads(there.stdout).get("status", {}).get("id") == local:
        return None
    print(f"loading the image {IMAGE} on the node {node} (kind load)…", flush=True)
    r = subprocess.run(["kind", "load", "docker-image", IMAGE, "--name", KIND_CLUSTER, "--nodes", node], capture_output=True, text=True)
    if r.returncode != 0:
        return f"kind load docker-image {IMAGE}: {r.stderr[-400:]}"
    if _runner_phase():                 # a pod on the old image: a new one
        kubectl("-n", KUBE_NAMESPACE, "delete", "pod", DBT_POD, "--wait=true", "--grace-period=1")
    return None


def ensure_runner():
    """The dbt pod, running (created if missing): on one node, with the image loaded there, sleeping."""
    phase = _runner_phase()
    if phase == "Running":
        limit = kubectl("-n", KUBE_NAMESPACE, "get", "pod", DBT_POD, "-o", "jsonpath={.spec.containers[0].resources.limits.memory}").stdout.strip()
        if limit == DBT_MEMORY:
            return
    if phase:
        kubectl("-n", KUBE_NAMESPACE, "delete", "pod", DBT_POD, "--wait=true", "--grace-period=1")
    pod = {"apiVersion": "v1", "kind": "Pod",
           "metadata": {"name": DBT_POD, "namespace": KUBE_NAMESPACE, "labels": {"app": DBT_POD, "e2e.tabularia.org/owner": "dbt-export"}},
           "spec": {"nodeSelector": {"kubernetes.io/hostname": kube_node()}, "restartPolicy": "Never", "terminationGracePeriodSeconds": 1,
                    "automountServiceAccountToken": False,
                    "containers": [{"name": "dbt", "image": _image_ref(), "imagePullPolicy": "Never" if KIND_CLUSTER else "IfNotPresent",
                                    "command": ["sleep", "infinity"],
                                    "resources": {"requests": {"cpu": "100m", "memory": "256Mi"}, "limits": {"memory": DBT_MEMORY}}}]}}
    r = kubectl("apply", "-f", "-", input=json.dumps(pod))
    if r.returncode != 0:
        raise RuntimeError(f"the dbt pod cannot be created: {r.stderr[-400:]}")
    r = kubectl("-n", KUBE_NAMESPACE, "wait", "--for=condition=Ready", f"pod/{DBT_POD}", "--timeout=180s")
    if r.returncode != 0:
        raise RuntimeError(f"the dbt pod does not start: {r.stderr[-400:]} "
                           + kubectl("-n", KUBE_NAMESPACE, "get", "pod", DBT_POD, "-o", "jsonpath={.status.containerStatuses}").stdout[-400:])


# the NetworkPolicy the oracles add when the engine's ClickHouse cannot reach a source database
# (the ClickHouse target's federated databases): the sample databases' policy lets in the app's
# namespace only. It belongs to one run of the oracles, named and labelled by its OWNER (by default
# its dbt pod: two runs in parallel have two pods), and kube_cleanup removes that run's only — it
# removed every oracle's, and a run in parallel lost its way to the sources half way
REACH_POLICY = "zz-dbt-e2e-clickhouse-to-sources"
OWNER = env("E2E_OWNER") or DBT_POD


def reach_sources(hosts, owner=OWNER) -> str | None:
    """The engine's ClickHouse reaches every (host, port) of `hosts`, a NetworkPolicy of `owner`
    if it does not (Kubernetes only). None when it does; otherwise what is still closed."""
    from environment import CH_CONTAINER

    def closed():
        return [(h, p) for h, p in hosts
                if exec_in(CH_CONTAINER, f"timeout 4 bash -c '</dev/tcp/{h}/{int(p)}'").returncode != 0]

    shut = closed()
    if not shut or not KUBE:
        return None if not shut else f"the engine's ClickHouse does not reach {shut}"
    ch_ns, ch_name = CH_CONTAINER.split("/", 1)
    ch_labels = json.loads(kubectl("-n", ch_ns, "get", "deploy", ch_name, "-o", "jsonpath={.spec.selector.matchLabels}").stdout or "{}")
    for host, port in shut:
        svc, _, rest = host.partition(".")
        ns = rest.split(".")[0] if rest else KUBE_NAMESPACE
        selector = json.loads(kubectl("-n", ns, "get", "svc", svc, "-o", "jsonpath={.spec.selector}").stdout or "{}")
        if not selector or not ch_labels:
            return f"the engine's ClickHouse does not reach {host}:{port}, and its pods cannot be told apart"
        policy = {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
                  "metadata": {"name": f"{owner}-to-{svc}"[:63], "namespace": ns, "labels": {"e2e.tabularia.org/owner": owner}},
                  "spec": {"podSelector": {"matchLabels": selector}, "policyTypes": ["Ingress"],
                           "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ch_ns}},
                                                  "podSelector": {"matchLabels": ch_labels}}],
                                        "ports": [{"protocol": "TCP", "port": int(port)}]}]}}
        print(f"the engine's ClickHouse does not reach {host}:{port}: NetworkPolicy {policy['metadata']['name']} in {ns} (removed at the end)", flush=True)
        r = kubectl("apply", "-f", "-", input=json.dumps(policy))
        if r.returncode != 0:
            return f"NetworkPolicy towards {host}: {r.stderr[-300:]}"
    for _ in range(20):
        shut = closed()
        if not shut:
            return None
        time.sleep(1)
    return f"the engine's ClickHouse still does not reach {shut}"


def kube_cleanup(owner=OWNER):
    """The dbt pod and the NetworkPolicies of this run of the oracles (`owner`), gone (the image
    stays on the node); another run's, in parallel, stay."""
    kubectl("-n", KUBE_NAMESPACE, "delete", "pod", DBT_POD, "--ignore-not-found", "--wait=true", "--grace-period=1")
    kubectl("delete", "networkpolicy", "-A", "-l", f"e2e.tabularia.org/owner={owner}", "--ignore-not-found")


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
    return run_dbt_image(folder, ["dbt", *arguments, "--no-use-colors"], variables)


def export(flow, target, suffix=""):
    """Downloads the export's zip and opens it in a clean folder: (folder, file names) or (None, error)."""
    st, zipped = api("GET", f"/flows/{flow['id']}/export/dbt?target={target}", raw=True)
    if st != 200 or zipped[:2] != b"PK":
        return None, (st, zipped[:400] if isinstance(zipped, (bytes, str)) else zipped)
    slug = re.sub(r"[^a-z0-9]+", "_", flow["name"].lower()).strip("_")
    folder = f"{WORKDIR}/{slug}_{target}{suffix}"
    remove_folder(folder)
    os.makedirs(folder)
    with zipfile.ZipFile(io.BytesIO(zipped)) as z:
        z.extractall(folder)
        names = sorted(z.namelist())
    clickhouse_settings(f"{folder}/profiles.yml")
    return folder, names


def clickhouse_settings(profile):
    """E2E_CH_SETTINGS added to the `custom_settings` of every dbt-clickhouse output of the profile
    (the file as the export, or INTEGRATION.md, writes it): what a team with a small ClickHouse adds
    so that a big join or group by spills to disk (on kind the sample ClickHouse has 2 GiB). The
    settings the export writes stay as they are."""
    if not CH_SETTINGS or not os.path.exists(profile):
        return
    text = open(profile).read()
    if "type: clickhouse" not in text:
        return
    patched = re.sub(r"(?m)^( *)custom_settings:\n", lambda m: m.group(0) + "".join(f"{m.group(1)}  {k}: {v}\n" for k, v in CH_SETTINGS.items()), text)
    if patched == text:
        raise RuntimeError(f"E2E_CH_SETTINGS: no custom_settings in {profile}")
    open(profile, "w").write(patched)


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

    return exec_in(CH_CONTAINER, 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery', sql)


def sample_clickhouse(sql: str) -> subprocess.CompletedProcess:
    """SQL to the sample ClickHouse (the CRM), on the client's input."""
    from environment import CH_SAMPLE_CONTAINER

    return exec_in(CH_SAMPLE_CONTAINER, 'clickhouse-client --user "$CLICKHOUSE_USER" --password "$CLICKHOUSE_PASSWORD" --multiquery', sql)


def clickhouse_databases() -> set:
    return set(engine_clickhouse("SELECT name FROM system.databases FORMAT TSV").stdout.split())


def suite_database(name: str) -> bool:
    """A ClickHouse database of the suite: dbt_tabularia and the federated databases of the sample
    schemas and of zzdbt (schema.sql). The oracles that hunt on their own name everything they make
    zz<letter>… (clickhouse_paths.py: zzc, native_targets.py: zzn) and clean only that: the suite's
    cleanup, which dropped every tab_src_*, never drops theirs while they run."""
    return name == "dbt_tabularia" or (name.startswith("tab_src_") and not re.match(r"^tab_src_\d+_zz(?!dbt(_|$))", name))


def clean_clickhouse(own=suite_database):
    """Drops the databases of dbt and of the federated sources that `own` says are this oracle's,
    and checks they are gone."""
    names = [n for n in clickhouse_databases() if own(n)]
    if names:
        r = engine_clickhouse("".join(f"DROP DATABASE IF EXISTS `{n}`;\n" for n in names))
        if r.returncode != 0:
            raise RuntimeError(f"ClickHouse cleanup failed: {r.stderr[-300:]}")
    left = [n for n in clickhouse_databases() if own(n)]
    if left:
        raise RuntimeError(f"ClickHouse cleanup: {left} are still there")


def sample_postgres(sql: str) -> subprocess.CompletedProcess:
    """SQL to the sample Postgres, on psql's input."""
    from environment import PG_CONTAINER

    return exec_in(PG_CONTAINER, 'psql -q -t -A -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"', sql)


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


def test_slug(s):
    """The engine's slug of a test name (backend/app/engine/dbt_export.py `_slug`)."""
    s = "".join(ch if (ch.isalnum() or ch == "_") else "_" for ch in str(s).lower()).strip("_") or "model"
    return s if not s[0].isdigit() else f"m_{s}"


def test_outcomes(folder, model, rules):
    """{rule id: dbt status}, from what dbt ran: the generic tests (only not_null) by their column,
    the singular ones by name — `<model>__<kind>_<rule id>`, or `<model>__<kind>_<column>` for a
    column rule (one-column unique, accepted_values), `_2`, `_3` when two share a file name."""
    results = json.load(open(f"{folder}/target/run_results.json"))["results"]
    nodes = json.load(open(f"{folder}/target/manifest.json"))["nodes"]
    found = {}
    for r in results:
        n = nodes.get(r["unique_id"]) or {}
        if n.get("resource_type") != "test":
            continue
        meta = n.get("test_metadata")
        if meta:
            col = (n.get("column_name") or (meta.get("kwargs") or {}).get("column_name") or "").strip('"`')
            for rule in rules:
                if rule["kind"] == meta.get("name") and rule.get("column") == col:
                    found[rule["id"]] = r["status"]
            continue
        m = re.fullmatch(rf"{model}__(\w+?)_(r\d+)", n["name"])
        if m:
            found[m.group(2)] = r["status"]
            continue
        m = re.fullmatch(rf"{model}__(not_null|unique|accepted_values)_(\w+?)(?:_(\d+))?", n["name"])
        if m:
            alike = [x for x in rules if x["kind"] == m.group(1) and test_slug(x.get("column") or (x.get("columns") or [""])[0]) == m.group(2)]
            k = int(m.group(3) or 1) - 1
            for x in (alike[k:k + 1] if len(alike) > 1 else alike):
                found[x["id"]] = r["status"]
    return found
