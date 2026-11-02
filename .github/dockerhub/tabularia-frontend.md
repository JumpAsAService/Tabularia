![Tabularia — Messy in. Trusted out.](https://tabularia.org/og.png)

# Tabularia frontend

**Tabularia** is an open-source, self-hosted data preparation platform: people build data
flows visually — joins, filters, pivots, SQL, Python and R — on data from databases, files,
SFTP, S3, APIs and SharePoint, run them on the engine that fits, and publish trusted
datasets, pixel-perfect reports and live dashboards. Permissions, data contracts, lineage
and an audit trail come with it from the first day.

**This image is the web app**: the flow editor, the viewer, the designers of reports and
dashboards, the AI assistant's chat, the lineage, and the administration — in English,
Italian, German, French and Spanish, with five themes, dark and light.

The browser calls the gateway directly; this server only serves the app, so it holds no
credentials and reaches no other service. A working installation runs the three images
together, with PostgreSQL, Valkey (or Redis) and an S3-compatible object storage.

| Image | What it is |
|---|---|
| [`jumpasaservice/tabularia-gateway`](https://hub.docker.com/r/jumpasaservice/tabularia-gateway) | the control plane: API, sign-in, permissions, orchestration |
| [`jumpasaservice/tabularia-backend`](https://hub.docker.com/r/jumpasaservice/tabularia-backend) | the engine API and its workers |
| [`jumpasaservice/tabularia-frontend`](https://hub.docker.com/r/jumpasaservice/tabularia-frontend) | the web app (this image) |

The same image also draws the reports' charts: `node /app/chart-renderer/render.mjs --serve
8090 --host 127.0.0.1` runs the chart service, which the Helm chart starts as a sidecar of
the run workers.

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
  with one ingress for the app and the API, network policies and a read-only root
  filesystem — [Kubernetes and Helm](https://tabularia.org/docs/deploy/kubernetes/).
- **Try it on your computer**: three commands with Docker Compose —
  [getting started](https://tabularia.org/docs/getting-started-docker-compose/) — or a
  local Kubernetes with [minikube](https://tabularia.org/docs/deploy/minikube/).

## Configuration

The server reads these when it starts, so one image serves every environment without a
rebuild:

| Variable | What it is |
|---|---|
| `NUXT_PUBLIC_API_BASE` | the gateway's URL as the browser calls it: `/api` when one host serves the app and the API, or a full URL |
| `NUXT_PUBLIC_BUCKET` | the default bucket; the same as the engine's `STORAGE__BUCKET` |
| `NUXT_PUBLIC_GRAFANA_URL` | Grafana as the browser reaches it, for the Monitoring tab (optional) |
| `NUXT_PUBLIC_NOTEBOOKS_ORIGIN` | the notebooks' own address, allowed by the pages' Content-Security-Policy (Kubernetes) |
| `NUXT_PUBLIC_SOURCE_URL` | where **Source code** in the menu leads: if you offer a changed version over a network, the AGPL asks you to point it at your source |

All of Tabularia's variables: [configuration reference](https://tabularia.org/docs/configuration/).

## The container

- Listens on port **3000** (Node.js 22 on Alpine); `GET /login` answers when the app is up.
- Runs as an unprivileged user (uid **10001**) and works with a read-only root filesystem
  and a `/tmp` of its own.
- Sends a Content-Security-Policy with every page.
- Serves the notices of the third-party software it holds at `/third-party-notices.txt`.

Before a release is pushed, its images are built for both architectures, started for a smoke
test, checked for the licences of the packages they hold, and scanned for secrets (the
maintainers' fingerprints and TruffleHog's verified credentials). The process is described
in the repository: [release process](https://github.com/JumpAsAService/Tabularia/blob/main/docs/release-process.md).

## Links

- Website and documentation: [tabularia.org](https://tabularia.org/)
- Source code and issues: [github.com/JumpAsAService/Tabularia](https://github.com/JumpAsAService/Tabularia)
- Licence: [AGPL-3.0](https://github.com/JumpAsAService/Tabularia/blob/main/LICENSE)
