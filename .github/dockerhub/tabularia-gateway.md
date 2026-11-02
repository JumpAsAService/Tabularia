![Tabularia — Messy in. Trusted out.](https://tabularia.org/og.png)

# Tabularia gateway

**Tabularia** is an open-source, self-hosted data preparation platform: people build data
flows visually — joins, filters, pivots, SQL, Python and R — on data from databases, files,
SFTP, S3, APIs and SharePoint, run them on the engine that fits (Polars, DuckDB, chDB, an
external ClickHouse, BigQuery), and publish trusted datasets, pixel-perfect reports and live
dashboards. Permissions, data contracts, lineage and an audit trail come with it from the
first day.

**This image is the gateway**, Tabularia's control plane: the API every client talks to.

- **Sign-in and access**: passwords, single sign-on over OpenID Connect, API tokens and
  service accounts; folders with view, run, edit and manage rights; an audit log that can
  be sent to a SIEM.
- **What people build**: flows and their versions, datasources, saved views, SQL scripts,
  reports, dashboards, notebooks — and the checks of who may read what.
- **Orchestration**: schedules, flow groups, Pushmail, webhooks and the runs' history
  (`APP__ROLE=orchestrator` runs only this background work, `api` only the requests,
  `all` both).
- **The AI assistant and the MCP server**, with spending caps and a metadata-only mode.
- **Administration**: settings changed live from the panel, database backups, and on
  Kubernetes the worker autoscaling, Python and R environments, container registries.

The gateway processes no data itself: the engine does, and the browser app is the frontend.
A working installation runs the three images together, with PostgreSQL, Valkey (or Redis)
and an S3-compatible object storage.

| Image | What it is |
|---|---|
| [`jumpasaservice/tabularia-gateway`](https://hub.docker.com/r/jumpasaservice/tabularia-gateway) | the control plane: API, sign-in, permissions, orchestration (this image) |
| [`jumpasaservice/tabularia-backend`](https://hub.docker.com/r/jumpasaservice/tabularia-backend) | the engine API and its workers: where the data is read, transformed and written |
| [`jumpasaservice/tabularia-frontend`](https://hub.docker.com/r/jumpasaservice/tabularia-frontend) | the web app |

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

- **Production on Kubernetes**: the Helm chart in the repository installs the three images,
  with the gateway and the orchestrator as separate workloads, network policies and a
  read-only root filesystem —
  [Kubernetes and Helm](https://tabularia.org/docs/deploy/kubernetes/).
- **Try it on your computer**: three commands with Docker Compose —
  [getting started](https://tabularia.org/docs/getting-started-docker-compose/) — or a
  local Kubernetes with [minikube](https://tabularia.org/docs/deploy/minikube/).

## Configuration

Settings are environment variables; the ones the gateway needs first:

| Variable | What it is |
|---|---|
| `APP__ENV_NAME=production` | turns on the production guard: the gateway refuses to start with development secrets |
| `APP__ROLE` | `api`, `orchestrator` or `all` (the default) |
| `DB__HOST`, `DB__PORT`, `DB__USER`, `DB__PASSWORD`, `DB__NAME` (or `DB__URL`) | the PostgreSQL metadata database |
| `JWT__SECRET` | signs the sessions: at least 32 characters |
| `SECURITY__FERNET_KEY` | encrypts the saved credentials; **the same key in the gateway and the engine**, kept with the backups |
| `ENGINE__BASE_URL` | the engine API's internal address, for example `http://backend:8000` |
| `AUTH__ADMIN_EMAIL`, `AUTH__ADMIN_PASSWORD` | the administrator created at the first start |
| `DB__AUTO_MIGRATE` | `true` migrates the schema at start; otherwise run `python -m app.db.migra` first (the Helm chart does, in an init container) |

All of them, with defaults and the Helm values that set them:
[configuration reference](https://tabularia.org/docs/configuration/). Many limits, waits and
caps can also be changed while it runs, from Administration → Settings.

## The container

- Listens on port **8000**; `GET /health` answers while the app is serving (a liveness check).
- Runs as an unprivileged user (uid **10001**), writes only to `/tmp`, and works with a
  read-only root filesystem.
- Holds `pg_dump` and `pg_restore` for PostgreSQL 14 to 18, for the database backups.
- Python 3.12 on Debian bookworm slim.

Before a release is pushed, its images are built for both architectures, started for a smoke
test, checked for the licences of the packages they hold, and scanned for secrets (the
maintainers' fingerprints and TruffleHog's verified credentials). The process is described
in the repository: [release process](https://github.com/JumpAsAService/Tabularia/blob/main/docs/release-process.md).

## Links

- Website and documentation: [tabularia.org](https://tabularia.org/)
- Source code and issues: [github.com/JumpAsAService/Tabularia](https://github.com/JumpAsAService/Tabularia)
- Licence: [AGPL-3.0](https://github.com/JumpAsAService/Tabularia/blob/main/LICENSE). The
  notices of the third-party software are linked from the app's menu and the website's footer.
