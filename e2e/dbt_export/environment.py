"""Where the dbt export oracles run: everything that depends on the installation,
read from environment variables. Two kinds of stack:

  · Docker compose (the default): the production compose with the `samples` profile, API
    behind the proxy on 8088, ClickHouse engine in a container. dbt runs in a throwaway
    container on the stack's network, the sample databases are reached with `docker exec`.
  · Kubernetes (E2E_KUBE_CONTEXT set, e.g. the kind test stack): dbt runs in ONE pod the
    oracles create in the app's namespace (it reaches what the engine reaches, through the
    NetworkPolicies of the sample databases), the project folder goes in and comes back
    with every call (tar through `kubectl exec`); the sample databases are deployments,
    reached with `kubectl exec`. What is not in the environment is read from the cluster:
    the admin from a secret, the engine's ClickHouse from the app's ConfigMap and Secret.
    Nothing read there is printed or written to a file.

  TABULARIA_URL            the interface (default http://localhost:8088, on Kubernetes http://localhost:8080);
                           the API is under /api
  TABULARIA_API            the API, if it is not under /api of the interface
  TABULARIA_ENV            a .env file to read what is not in the environment from
                           (AUTH__ADMIN_EMAIL/PASSWORD, CLICKHOUSE_EXTERNAL__*)
  E2E_PG                   the sample Postgres: a container (default tabularia-sampledb-postgres-1), on
                           Kubernetes a deployment, `namespace/name` (default sampledb-postgres in E2E_KUBE_DEPS)
  E2E_CH_SAMPLE            the sample ClickHouse, the CRM (default tabularia-sampledb-clickhouse-1; on
                           Kubernetes sampledb-clickhouse)
  E2E_CH_ENGINE            the engine's ClickHouse, to run SQL on (default: CLICKHOUSE_EXTERNAL__HOST; on
                           Kubernetes the deployment named like the service of CLICKHOUSE_EXTERNAL__HOST,
                           `<service>.<namespace>.svc…` → `<namespace>/<service>`)
  E2E_DBT_IMAGE            the image with dbt (default tabularia-dbt-e2e, see Dockerfile.dbt)
  E2E_WORKDIR              where to write the exported projects (default .work/ next to this file, ignored by git);
                           the browser oracles keep Firefox's profile there: with the snap's Firefox it must be
                           under your home (the snap has a /tmp of its own)
  E2E_RESTART              a command that restarts gateway and engine (for the determinism oracle);
                           without it, that part is skipped
  E2E_TZ                   the time zone dbt runs in (TZ, PGTZ; default Europe/Rome): the models must not
                           depend on it. UTC turns the check off
  E2E_MARIONETTE_PORT      the port of Firefox's Marionette for the browser oracles (default 2828)
  E2E_CH_SETTINGS          ClickHouse settings (`key=value,key=value`) added to the `custom_settings` of every
                           dbt-clickhouse profile the oracles run, after those the export writes: what a team
                           with a small ClickHouse adds (on kind the sample ClickHouse has a 2 GiB limit and
                           the sample's order lines are 42 million: `join_algorithm=grace_hash,…`)

  Docker compose only:
  E2E_NETWORK              the stack's Docker network (default tabularia_interna)

  Kubernetes only:
  E2E_KUBE_CONTEXT         the kubectl context (e.g. kind-tabularia): turns the Kubernetes mode on
  E2E_KUBE_NAMESPACE       the app's namespace, where the dbt pod runs (default tabularia)
  E2E_KUBE_DEPS            the namespace of the sample databases (default tabularia-deps)
  E2E_KUBE_RELEASE         the Helm release's prefix of the app's ConfigMap and Secret
                           (default tabularia-tabularia: tabularia-tabularia-config, -secrets)
  E2E_KUBE_ADMIN_SECRET    `namespace/name` of the secret with the admin's `email` and `password`
                           (default <E2E_KUBE_DEPS>/sampledb-admin)
  E2E_KUBE_NODE            the node the dbt pod runs on, and the only one the image is loaded to
                           (default: the first worker node)
  E2E_KIND_CLUSTER         the kind cluster to load the dbt image into (default: the context without
                           «kind-»); empty when the image comes from a registry the nodes pull from
  E2E_DBT_POD              the dbt pod's name (default zz-dbt-e2e)
  E2E_DBT_MEMORY           the dbt pod's memory limit (default 6Gi; DuckDB uses 80% of it)
"""
import json, os, subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))


# a relative path holds from where the command is launched: run_all.py starts the oracles
# from their folder, and from there «infrastructure/…/.env» would no longer be found
if os.environ.get("TABULARIA_ENV"):
    os.environ["TABULARIA_ENV"] = os.path.abspath(os.environ["TABULARIA_ENV"])


def _file_env() -> dict:
    path = os.environ.get("TABULARIA_ENV")
    if not path:
        return {}
    if not os.path.exists(path):
        # it used to return empty in silence: the sign-in went out without credentials (422)
        raise SystemExit(f"TABULARIA_ENV points to a file that does not exist: {path}")
    out = {}
    for line in open(path):
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


_FILE = _file_env()
KUBE = os.environ.get("E2E_KUBE_CONTEXT") or _FILE.get("E2E_KUBE_CONTEXT")


def kubectl(*arguments, **kw) -> subprocess.CompletedProcess:
    """kubectl on the oracles' context (capture_output and text unless told otherwise)."""
    kw.setdefault("capture_output", True)
    if "input" not in kw or isinstance(kw.get("input"), str):
        kw.setdefault("text", True)
    return subprocess.run(["kubectl", "--context", KUBE, *arguments], **kw)


_CLUSTER: dict | None = None
# what the oracles read from the cluster when the environment does not say it: (ConfigMap or
# Secret, key). The values stay in this process: never printed, never written to a file
_FROM_CONFIG = ("CLICKHOUSE_EXTERNAL__HOST", "CLICKHOUSE_EXTERNAL__PORT", "CLICKHOUSE_EXTERNAL__USERNAME")
_FROM_SECRETS = ("CLICKHOUSE_EXTERNAL__PASSWORD",)


def _cluster_env() -> dict:
    global _CLUSTER
    if _CLUSTER is not None:
        return _CLUSTER
    _CLUSTER = {}
    if not KUBE:
        return _CLUSTER
    import base64

    get = lambda k: os.environ.get(k) or _FILE.get(k)
    ns = get("E2E_KUBE_NAMESPACE") or "tabularia"
    release = get("E2E_KUBE_RELEASE") or "tabularia-tabularia"
    r = kubectl("-n", ns, "get", "configmap", f"{release}-config", "-o", "json")
    if r.returncode == 0:
        data = json.loads(r.stdout).get("data") or {}
        _CLUSTER.update({k: data[k] for k in _FROM_CONFIG if data.get(k)})
    r = kubectl("-n", ns, "get", "secret", f"{release}-secrets", "-o", "json")
    if r.returncode == 0:
        data = json.loads(r.stdout).get("data") or {}
        _CLUSTER.update({k: base64.b64decode(data[k]).decode() for k in _FROM_SECRETS if data.get(k)})
    admin = get("E2E_KUBE_ADMIN_SECRET") or f"{get('E2E_KUBE_DEPS') or 'tabularia-deps'}/sampledb-admin"
    ans, _, name = admin.rpartition("/")
    r = kubectl("-n", ans or ns, "get", "secret", name, "-o", "json")
    if r.returncode == 0:
        data = json.loads(r.stdout).get("data") or {}
        for key, var in (("email", "AUTH__ADMIN_EMAIL"), ("password", "AUTH__ADMIN_PASSWORD")):
            if data.get(key):
                _CLUSTER[var] = base64.b64decode(data[key]).decode()
    return _CLUSTER


def env(key: str, default: str | None = None) -> str | None:
    """The environment first, then the TABULARIA_ENV file, then (Kubernetes) the cluster, then the default."""
    return os.environ.get(key) or _FILE.get(key) or _cluster_env().get(key) or default


UI = (env("TABULARIA_URL") or ("http://localhost:8080" if KUBE else "http://localhost:8088")).rstrip("/")
BASE = (env("TABULARIA_API") or UI + "/api").rstrip("/")
NETWORK = env("E2E_NETWORK", "tabularia_interna")
KUBE_NAMESPACE = env("E2E_KUBE_NAMESPACE", "tabularia")
KUBE_DEPS = env("E2E_KUBE_DEPS", "tabularia-deps")
DBT_POD = env("E2E_DBT_POD", "zz-dbt-e2e")
# the dbt pod's memory limit: DuckDB takes 80% of it (the federated target's big joins)
DBT_MEMORY = env("E2E_DBT_MEMORY", "6Gi")
# settings added to the custom_settings of the dbt-clickhouse profiles: `key=value,key=value`
CH_SETTINGS = dict(kv.split("=", 1) for kv in (env("E2E_CH_SETTINGS") or "").split(",") if "=" in kv)
# empty (set to nothing) when the nodes pull the image from a registry
KIND_CLUSTER = os.environ.get("E2E_KIND_CLUSTER", (KUBE or "")[len("kind-"):] if (KUBE or "").startswith("kind-") else "")
CH_PORT = env("CLICKHOUSE_EXTERNAL__PORT", "8123")


def _workload(name: str) -> str:
    """On Kubernetes a sample database is `namespace/deployment`: a bare name is in E2E_KUBE_DEPS."""
    return name if "/" in name else f"{KUBE_DEPS}/{name}"


if KUBE:
    PG_CONTAINER = _workload(env("E2E_PG", "sampledb-postgres"))
    CH_SAMPLE_CONTAINER = _workload(env("E2E_CH_SAMPLE", "sampledb-clickhouse"))
    # the host dbt (in its pod) and the oracles' profiles reach the engine's ClickHouse at
    CH_HOST = env("CLICKHOUSE_EXTERNAL__HOST") or "sampledb-clickhouse.tabularia-deps.svc.cluster.local"
    _svc, _, _rest = CH_HOST.partition(".")
    CH_CONTAINER = _workload(env("E2E_CH_ENGINE") or (f"{_rest.split('.')[0]}/{_svc}" if _rest else _svc))
else:
    PG_CONTAINER = env("E2E_PG", "tabularia-sampledb-postgres-1")
    CH_SAMPLE_CONTAINER = env("E2E_CH_SAMPLE", "tabularia-sampledb-clickhouse-1")
    CH_CONTAINER = env("E2E_CH_ENGINE") or env("CLICKHOUSE_EXTERNAL__HOST") or "chmotore"
    # on the compose network the container's name is its host name
    CH_HOST = CH_CONTAINER
IMAGE = env("E2E_DBT_IMAGE", "tabularia-dbt-e2e")
WORKDIR = env("E2E_WORKDIR") or os.path.join(HERE, ".work")
RESTART = env("E2E_RESTART")
# which container to read a sample connection's password from, by type
PASSWORD_FROM = {"postgresql": (PG_CONTAINER, "POSTGRES_PASSWORD"), "clickhouse": (CH_SAMPLE_CONTAINER, "CLICKHOUSE_PASSWORD")}
os.makedirs(WORKDIR, exist_ok=True)
