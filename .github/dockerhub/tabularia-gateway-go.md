![Tabularia — Messy in. Trusted out.](https://tabularia.org/og.png)

# Tabularia gateway (Go)

**Tabularia** is an open-source, self-hosted data preparation platform: people build data
flows visually — joins, filters, pivots, SQL, Python and R — on data from databases, files,
SFTP, S3, APIs and SharePoint, run them on the engine that fits (Polars, DuckDB, chDB, an
external ClickHouse, BigQuery), and publish trusted datasets, pixel-perfect reports and live
dashboards. Permissions, data contracts, lineage and an audit trail come with it from the
first day.

**This image is the gateway**, Tabularia's control plane, written in Go since 1.4.0: the API
every client talks to, and the background work that runs the flows.

- **Sign-in and access**: passwords, single sign-on over OpenID Connect, API tokens and
  service accounts; folders with view, run, edit and manage rights; an audit log that can
  be sent to a SIEM.
- **What people build**: flows and their versions, datasources, saved views, SQL scripts,
  reports, dashboards, notebooks, and the links that share a report or a dashboard
  outside Tabularia behind a password.
- **Orchestration**: schedules, flow groups, Pushmail, webhooks and the runs' history.
- **Administration**: settings changed live from the panel, database backups, and on
  Kubernetes the worker autoscaling, Python and R environments, container registries.

It answers the same routes as the Python gateway it replaces, with the same database,
cookies and errors, checked request by request against it. On 2 CPUs one replica answers
9 to 45 times the requests a second (7,600 a second on `/auth/me`) and uses 10 MB of memory
idle instead of 141.

## One image, two roles

`APP__ROLE` chooses what a process does:

| `APP__ROLE` | What it does |
|---|---|
| `api` | answers requests. Stateless: replicate it when people wait |
| `orchestrator` | the background work: fires the schedules, executes the flows, reconciles the runs, cleans up. Several can run; they share the work through the database |
| `all` (the default) | both, in one process: the small installations |

## The images of an installation

| Image | What it is |
|---|---|
| [`jumpasaservice/tabularia-gateway-go`](https://hub.docker.com/r/jumpasaservice/tabularia-gateway-go) | the control plane: API, sign-in, permissions, orchestration (this image) |
| [`jumpasaservice/tabularia-gateway`](https://hub.docker.com/r/jumpasaservice/tabularia-gateway) | the AI service (`APP__ROLE=ai`): the AI assistant, the SQL parsing, the schema migrations. **Required**: this gateway does not start without it |
| [`jumpasaservice/tabularia-backend`](https://hub.docker.com/r/jumpasaservice/tabularia-backend) | the engine API and its workers: where the data is read, transformed and written |
| [`jumpasaservice/tabularia-frontend`](https://hub.docker.com/r/jumpasaservice/tabularia-frontend) | the web app |

With PostgreSQL, Valkey (or Redis) and an S3-compatible object storage. The gateway
processes no data itself: the engine does.

## Tags

| Tag | Meaning |
|---|---|
| `1.4.0` | one release, for `linux/amd64` and `linux/arm64` |
| `1.4` | the latest `1.4.x` |
| `latest` | the latest stable release |
| `1.4.0-amd64`, `1.4.0-arm64` | one architecture only |

The first release of this image is `1.4.0`. Pin an exact version in production and upgrade
all the images together, following the
[upgrade guide](https://tabularia.org/docs/deploy/upgrading/). Every release is described
in the [changelog](https://github.com/JumpAsAService/Tabularia/blob/main/CHANGELOG.md).

## Getting started

- **Production on Kubernetes**: the Helm chart in the repository installs every image, with
  the gateway, the orchestrator and the AI service as separate workloads, network policies
  and a read-only root filesystem —
  [Kubernetes and Helm](https://tabularia.org/docs/deploy/kubernetes/).
- **Try it on your computer**: three commands with Docker Compose —
  [getting started](https://tabularia.org/docs/getting-started-docker-compose/) — or a
  local Kubernetes with [minikube](https://tabularia.org/docs/deploy/minikube/).
- **Upgrading from 1.3**: the chart and the compose file add this image and the AI service
  for you; the environment variables stay the same, except `FORWARDED_ALLOW_IPS`, now
  `GATEWAY_GO__FORWARDED_ALLOW_IPS`.

## Configuration

Settings are environment variables, the same as the Python gateway's; the ones the gateway
needs first:

| Variable | What it is |
|---|---|
| `GATEWAY_GO__AI_SERVICE` | the AI service's address, for example `http://ai:8000`: **required** |
| `APP__ROLE` | `api`, `orchestrator` or `all` (the default) |
| `DB__HOST`, `DB__PORT`, `DB__USER`, `DB__PASSWORD`, `DB__NAME` (or `DB__URL`) | the PostgreSQL metadata database |
| `JWT__SECRET` | signs the sessions: at least 32 characters, the same in every gateway |
| `SECURITY__FERNET_KEY` | encrypts the saved credentials and signs the internal calls; **the same key in the gateway, the AI service and the engine**, kept with the backups |
| `ENGINE__BASE_URL` | the engine API's internal address, for example `http://backend:8000` |
| `GATEWAY_GO__FORWARDED_ALLOW_IPS` | the proxies whose `X-Forwarded-For` is believed: `127.0.0.1` by default, `*` for any |
| `GATEWAY_GO__LISTEN` | where it listens: `:8000` by default |

All of them, with defaults and the Helm values that set them:
[configuration reference](https://tabularia.org/docs/configuration/). Many limits, waits and
caps can also be changed while it runs, from Administration → Settings.

**The schema.** This gateway never migrates the database: at start it checks that the
schema is at the revision it reads, and stops otherwise. The migrations run from the AI
service's image (`python -m app.db.migra`; the Helm chart does it in an init container).

## The container

- Listens on port **8000**; `GET /health` answers while the app is serving (a liveness check).
- Runs as an unprivileged user (uid **10001**), writes only to `/tmp`, and works with a
  read-only root filesystem.
- Holds `pg_dump` and `pg_restore` for PostgreSQL 14 to 18, for the database backups.
- One Go binary on Debian bookworm slim: about 44 MB to download.
- Stops cleanly on `SIGTERM`: it finishes the requests in flight; an orchestrator closes
  the runs it was carrying as interrupted.

Before a release is pushed, its images are built for both architectures, started for a smoke
test, checked for the licences of the packages they hold, and scanned for secrets (the
maintainers' fingerprints and TruffleHog's verified credentials). The process is described
in the repository: [release process](https://github.com/JumpAsAService/Tabularia/blob/main/docs/release-process.md).

## Links

- Website and documentation: [tabularia.org](https://tabularia.org/)
- Source code and issues: [github.com/JumpAsAService/Tabularia](https://github.com/JumpAsAService/Tabularia)
- Licence: [AGPL-3.0](https://github.com/JumpAsAService/Tabularia/blob/main/LICENSE). The
  notices of the third-party software are linked from the app's menu and the website's footer.
