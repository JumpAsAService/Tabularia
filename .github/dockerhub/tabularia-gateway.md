![Tabularia — Messy in. Trusted out.](https://tabularia.org/og.png)

# Tabularia AI service (formerly the gateway)

**Tabularia** is an open-source, self-hosted data preparation platform: people build data
flows visually — joins, filters, pivots, SQL, Python and R — on data from databases, files,
SFTP, S3, APIs and SharePoint, run them on the engine that fits (Polars, DuckDB, chDB, an
external ClickHouse, BigQuery), and publish trusted datasets, pixel-perfect reports and live
dashboards. Permissions, data contracts, lineage and an audit trail come with it from the
first day.

> **Since 1.4.0 the gateway is a new image:**
> [`jumpasaservice/tabularia-gateway-go`](https://hub.docker.com/r/jumpasaservice/tabularia-gateway-go).
> This image stays, with a narrower job: it is the **AI service**, started with
> `APP__ROLE=ai`, and every installation needs it. Up to 1.3 it was the whole gateway;
> its `1.3.x` tags still are.

## What it does from 1.4.0

- **The AI assistant**: the chat on the data, the assistants that build flows and write
  scripts, the comments the AI writes on reports and dashboards, with spending caps and a
  metadata-only mode. The gateway forwards these requests to it.
- **The SQL parsing and the script checks** the gateway and the orchestrator ask
  for while they work, on an internal API that answers only calls signed with the
  installation's key.
- **The database's schema**: the migrations (`python -m app.db.migra`), the first
  administrator from the environment, and at start the production guard, which refuses
  development secrets.

It is never exposed and does no background work: only the gateway and the orchestrator
call it. Without it the gateway does not start, and with no AI provider
configured the assistant simply stays off.

## The images of an installation

| Image | What it is |
|---|---|
| [`jumpasaservice/tabularia-gateway-go`](https://hub.docker.com/r/jumpasaservice/tabularia-gateway-go) | the control plane: API, sign-in, permissions, orchestration |
| [`jumpasaservice/tabularia-gateway`](https://hub.docker.com/r/jumpasaservice/tabularia-gateway) | the AI service, the SQL parsing and the migrations (this image, `APP__ROLE=ai`) |
| [`jumpasaservice/tabularia-backend`](https://hub.docker.com/r/jumpasaservice/tabularia-backend) | the engine API and its workers: where the data is read, transformed and written |
| [`jumpasaservice/tabularia-frontend`](https://hub.docker.com/r/jumpasaservice/tabularia-frontend) | the web app |

With PostgreSQL, Valkey (or Redis) and an S3-compatible object storage.

## Tags

| Tag | Meaning |
|---|---|
| `1.4.0` | one release, for `linux/amd64` and `linux/arm64`: the AI service |
| `1.4` | the latest `1.4.x` |
| `latest` | the latest stable release |
| `1.4.0-amd64`, `1.4.0-arm64` | one architecture only |
| `1.3.0` and older | the whole gateway, before the Go one |

Pin an exact version in production and upgrade all the images together, following the
[upgrade guide](https://tabularia.org/docs/deploy/upgrading/). Every release is described
in the [changelog](https://github.com/JumpAsAService/Tabularia/blob/main/CHANGELOG.md).

## Getting started

- **Production on Kubernetes**: the Helm chart in the repository installs every image, with
  the AI service as its own workload (and this image again as the init container that
  migrates the schema), network policies and a read-only root filesystem —
  [Kubernetes and Helm](https://tabularia.org/docs/deploy/kubernetes/).
- **Try it on your computer**: three commands with Docker Compose —
  [getting started](https://tabularia.org/docs/getting-started-docker-compose/) — or a
  local Kubernetes with [minikube](https://tabularia.org/docs/deploy/minikube/).
- **Upgrading from 1.3**: the chart and the compose file add the Go gateway and turn this
  image into the AI service for you; the environment variables stay the same.

## Configuration

Settings are environment variables, the same the gateway reads; the ones the AI service
needs first:

| Variable | What it is |
|---|---|
| `APP__ROLE=ai` | makes it the AI service |
| `APP__ENV_NAME=production` | turns on the production guard: it refuses to start with development secrets |
| `DB__HOST`, `DB__PORT`, `DB__USER`, `DB__PASSWORD`, `DB__NAME` (or `DB__URL`) | the PostgreSQL metadata database |
| `JWT__SECRET` | at least 32 characters, the same as the gateway's |
| `SECURITY__FERNET_KEY` | encrypts the saved credentials and signs the internal calls; **the same key in the gateway, the AI service and the engine**, kept with the backups |
| `ENGINE__BASE_URL` | the engine API's internal address, for example `http://backend:8000` |
| `AUTH__ADMIN_EMAIL`, `AUTH__ADMIN_PASSWORD` | the administrator created at the first start |
| `DB__AUTO_MIGRATE` | `true` migrates the schema at start; otherwise run `python -m app.db.migra` first (the Helm chart does, in an init container) |
| `AI__BASE_URL`, `AI__SECRET_KEY` | the AI provider (any OpenAI-compatible endpoint); more providers, Anthropic among them, are added from the panel |

All of them, with defaults and the Helm values that set them:
[configuration reference](https://tabularia.org/docs/configuration/). The models that may be
used and the spending caps are set from the administration panel.

## The container

- Listens on port **8000**; `GET /health` answers while the app is serving (a liveness check).
- Runs as an unprivileged user (uid **10001**), writes only to `/tmp`, and works with a
  read-only root filesystem.
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
