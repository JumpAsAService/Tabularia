"""Where the dbt export oracles run: everything that depends on the installation,
read from environment variables. The defaults are those of the test stack the README
describes (production compose with the `samples` profile, API behind the proxy on 8088,
ClickHouse engine in a container).

  TABULARIA_URL            the interface (default http://localhost:8088); the API is under /api
  TABULARIA_API            the API, if it is not under /api of the interface
  TABULARIA_ENV            a .env file to read what is not in the environment from
                           (AUTH__ADMIN_EMAIL/PASSWORD, CLICKHOUSE_EXTERNAL__*)
  E2E_NETWORK              the stack's Docker network (default tabularia_interna)
  E2E_PG                   the sample Postgres container (default tabularia-sampledb-postgres-1)
  E2E_CH_SAMPLE            the sample ClickHouse container, the CRM (default tabularia-sampledb-clickhouse-1)
  E2E_CH_ENGINE            the engine's ClickHouse container (default: CLICKHOUSE_EXTERNAL__HOST)
  E2E_DBT_IMAGE            the image with dbt (default tabularia-dbt-e2e, see Dockerfile.dbt)
  E2E_WORKDIR              where to write the exported projects (default .work/ next to this file, ignored by git)
  E2E_RESTART              a command that restarts gateway and engine (for the determinism oracle);
                           without it, that part is skipped
  E2E_TZ                   the time zone dbt runs in (TZ, PGTZ; default Europe/Rome): the models must not
                           depend on it. UTC turns the check off
"""
import os

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


def env(key: str, default: str | None = None) -> str | None:
    """The environment first, then the TABULARIA_ENV file, then the default."""
    return os.environ.get(key) or _FILE.get(key) or default


UI = (env("TABULARIA_URL") or "http://localhost:8088").rstrip("/")
BASE = (env("TABULARIA_API") or UI + "/api").rstrip("/")
NETWORK = env("E2E_NETWORK", "tabularia_interna")
PG_CONTAINER = env("E2E_PG", "tabularia-sampledb-postgres-1")
CH_SAMPLE_CONTAINER = env("E2E_CH_SAMPLE", "tabularia-sampledb-clickhouse-1")
CH_CONTAINER = env("E2E_CH_ENGINE") or env("CLICKHOUSE_EXTERNAL__HOST") or "chmotore"
CH_PORT = env("CLICKHOUSE_EXTERNAL__PORT", "8123")
IMAGE = env("E2E_DBT_IMAGE", "tabularia-dbt-e2e")
WORKDIR = env("E2E_WORKDIR") or os.path.join(HERE, ".work")
RESTART = env("E2E_RESTART")
# which container to read a sample connection's password from, by type
PASSWORD_FROM = {"postgresql": (PG_CONTAINER, "POSTGRES_PASSWORD"), "clickhouse": (CH_SAMPLE_CONTAINER, "CLICKHOUSE_PASSWORD")}
os.makedirs(WORKDIR, exist_ok=True)
