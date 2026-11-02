![Tabularia — Messy in. Trusted out.](https://tabularia.org/og.png)

# Tabularia backend (engine)

**Tabularia** is an open-source, self-hosted data preparation platform: people build data
flows visually — joins, filters, pivots, SQL, Python and R — on data from databases, files,
SFTP, S3, APIs and SharePoint, run them on the engine that fits, and publish trusted
datasets, pixel-perfect reports and live dashboards. Permissions, data contracts, lineage
and an audit trail come with it from the first day.

**This image is the engine**: where the data is read, transformed and written. One image,
four roles, chosen by the command:

| Role | Command | Replicas |
|---|---|---|
| Engine API — previews, the viewer, imports, exports | image default (`uvicorn` on port 8000) | any |
| Run worker — flows, imports, exports, reports and dashboards rendered | `celery -A app.tasks.celery_app worker -E -Q celery --loglevel=info` | any |
| Preview worker — the editor's previews and the dashboards' tiles | `celery -A app.tasks.celery_app worker -E -Q preview --loglevel=info` | any |
| Scheduler of the upkeep (cache sweep, metrics) | `celery -A app.tasks.celery_app beat --schedule /tmp/celerybeat-schedule --loglevel=info` | **exactly 1** |

The engine answers only the gateway: every request carries a token derived from the key the
two share, and on Kubernetes a network policy closes it to everything else. A working
installation runs the three images together, with PostgreSQL, Valkey (or Redis) and an
S3-compatible object storage.

| Image | What it is |
|---|---|
| [`jumpasaservice/tabularia-gateway`](https://hub.docker.com/r/jumpasaservice/tabularia-gateway) | the control plane: API, sign-in, permissions, orchestration |
| [`jumpasaservice/tabularia-backend`](https://hub.docker.com/r/jumpasaservice/tabularia-backend) | the engine API and its workers (this image) |
| [`jumpasaservice/tabularia-frontend`](https://hub.docker.com/r/jumpasaservice/tabularia-frontend) | the web app |

## What is inside

- **Engines**: Polars and DuckDB in the worker; an external ClickHouse server and BigQuery
  when configured. chDB, the embedded ClickHouse, is not in the published image: build it
  with `--build-arg WITH_CHDB=true`.
- **Sources and destinations**: PostgreSQL, MySQL and MariaDB, Oracle (thin mode, no
  Instant Client), ClickHouse, Trino, BigQuery, files (CSV, TSV, Excel, Parquet, JSON lines), SFTP,
  S3 folders, HTTP APIs, SharePoint and Google Sheets. Microsoft's SQL Server driver is not
  in the published image, for its licence: build with `--build-arg WITH_MSSQL=true`
  ([SQL Server](https://tabularia.org/docs/sqlserver/)).
- **Reports**: the Typst compiler lays out the PDF pages; the charts come from the chart
  service in the frontend image (a sidecar of the run workers on Kubernetes).

Python and R code in flows does not run here: it runs in images of its own, built by
Tabularia on Kubernetes from the environments an administrator defines.

## Tags

| Tag | Meaning |
|---|---|
| `1.3.0` | one release, for `linux/amd64` and `linux/arm64` |
| `1.3` | the latest `1.3.x` |
| `latest` | the latest stable release |
| `1.3.0-amd64`, `1.3.0-arm64` | one architecture only |

Pin an exact version in production and upgrade the three images together, following the
[upgrade guide](https://tabularia.org/docs/deploy/upgrading/). Every release is described
in the [changelog](https://github.com/JumpAsAService/Tabularia/blob/main/CHANGELOG.md).

## Getting started

- **Production on Kubernetes**: the Helm chart in the repository runs the four roles as
  separate workloads, with the worker autoscaling, network policies and a read-only root
  filesystem — [Kubernetes and Helm](https://tabularia.org/docs/deploy/kubernetes/).
- **Try it on your computer**: three commands with Docker Compose —
  [getting started](https://tabularia.org/docs/getting-started-docker-compose/) — or a
  local Kubernetes with [minikube](https://tabularia.org/docs/deploy/minikube/).

## Configuration

Settings are environment variables; the ones the engine needs first:

| Variable | What it is |
|---|---|
| `SECURITY__FERNET_KEY` | **the same key as the gateway's**: it decrypts the connections' credentials and signs the gateway's requests |
| `STORAGE__ENDPOINT`, `STORAGE__ACCESS_KEY`, `STORAGE__SECRET_KEY`, `STORAGE__BUCKET` | the S3-compatible object storage, as the workers reach it |
| `REDIS__HOST` (and `REDIS__PASSWORD`) | Valkey or Redis: the task queues and the cache index |
| `CELERY__TASK_TIME_LIMIT` | the longest a task may run |
| `CLICKHOUSE_EXTERNAL__HOST` and the rest | an external ClickHouse engine (also settable from Administration → Settings) |
| `BIGQUERY__PROJECT` and its credentials | the BigQuery engine |

All of them, with defaults and the Helm values that set them:
[configuration reference](https://tabularia.org/docs/configuration/). Many limits and caps
can also be changed while it runs, from Administration → Settings.

## The container

- The engine API listens on port **8000**; `GET /healthcheck/` answers while it is serving.
- Runs as an unprivileged user (uid **10001**), writes only to `/tmp`, and works with a
  read-only root filesystem; size `/tmp` for the spills of the large joins and sorts.
- Python 3.12 on Debian bookworm slim, with DuckDB's `httpfs` extension installed in the
  image (nothing is downloaded at run time).

Before a release is pushed, its images are built for both architectures, started for a smoke
test, checked for the licences of the packages they hold, and scanned for secrets (the
maintainers' fingerprints and TruffleHog's verified credentials). The process is described
in the repository: [release process](https://github.com/JumpAsAService/Tabularia/blob/main/docs/release-process.md).

## Links

- Website and documentation: [tabularia.org](https://tabularia.org/)
- Source code and issues: [github.com/JumpAsAService/Tabularia](https://github.com/JumpAsAService/Tabularia)
- Licence: [AGPL-3.0](https://github.com/JumpAsAService/Tabularia/blob/main/LICENSE). The
  notices of the third-party software are linked from the app's menu and the website's footer.
