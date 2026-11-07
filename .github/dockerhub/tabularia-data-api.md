![Tabularia — Messy in. Trusted out.](https://tabularia.org/og.png)

# Tabularia data API

**Tabularia** is an open-source, self-hosted data preparation platform: people build data
flows visually — joins, filters, pivots, SQL, Python and R — on data from databases, files,
SFTP, S3, APIs and SharePoint, run them on the engine that fits (Polars, DuckDB, chDB, an
external ClickHouse, BigQuery), and publish trusted datasets, pixel-perfect reports and live
dashboards. Permissions, data contracts, lineage and an audit trail come with it from the
first day.

**This image answers Tabularia's data APIs**: a datasource published as a read-only HTTP API
for the systems that need it one record at a time — a CRM asking for the score of customer
123, a website asking for the open orders of an account. On Kubernetes, Tabularia runs a
Deployment of this image for each active data API, so the other systems' traffic never slows
the people using Tabularia.

- **One record in a millisecond or two**: each resource's data is downloaded once per
  version and served from a read-only DuckDB file sorted by its key; on 20 million rows in a
  pod of 1 GB, a lookup by key answers in 1.3 ms (p50, in the pod) with about 220 MB of
  memory, however big the table.
- **Typed filters and pages**: `col=`, `col__in=`, `__gt`, `__gte`, `__lt`, `__lte`, `__null`
  on the columns the definition allows, `_columns`, `_sort`, `_limit`, `_count=true`, CSV, and
  cursors bound to the data's version, so a page never mixes two refreshes.
- **Keys checked in the pod**: `tdk_…` keys known only by their SHA-256, with an expiry, a
  rate per minute, the resources and the addresses they are allowed; a revoked key is refused
  within the poll.
- **Its own description**: `GET /data/{api}/openapi.json` is OpenAPI 3.1, to import into the
  CRM or a client; the same document Tabularia's page shows in its Documentation tab.
- **Read only, and nothing else**: no storage credentials (a short-lived link per version),
  no query to the source databases, no SQL from the request (every value is a bound
  parameter).

## How it runs

The image is not meant to be run by hand: the Tabularia gateway's orchestrator creates, in
the data APIs' namespace, each active data API's Deployment, Service, Secret, Ingress and
NetworkPolicy, and keeps them in line every 30 seconds. Every `DATA_API__POLL_SECONDS` a pod
asks the gateway for its **manifest** (the definition, the keys as their fingerprints, a link
to each resource's data valid for five minutes) with a token of its own data API, and sends
back the requests, rows, errors and over-the-rate answers of each key per minute, and its
status. The other systems reach it through the ingress controller only, at
`/data/{address}/` on the app's host or on a host of the data APIs' own.

## The images of an installation

| Image | What it is |
|---|---|
| [`jumpasaservice/tabularia-gateway-go`](https://hub.docker.com/r/jumpasaservice/tabularia-gateway-go) | the control plane: API, sign-in, permissions, orchestration; it runs this image's pods |
| [`jumpasaservice/tabularia-gateway`](https://hub.docker.com/r/jumpasaservice/tabularia-gateway) | the AI service: the AI assistant, the SQL parsing, the schema migrations |
| [`jumpasaservice/tabularia-backend`](https://hub.docker.com/r/jumpasaservice/tabularia-backend) | the engine API and its workers: where the data is read, transformed and written |
| [`jumpasaservice/tabularia-frontend`](https://hub.docker.com/r/jumpasaservice/tabularia-frontend) | the web app |
| [`jumpasaservice/tabularia-data-api`](https://hub.docker.com/r/jumpasaservice/tabularia-data-api) | the data APIs' pods (this image), on Kubernetes only |

## Tags

| Tag | Meaning |
|---|---|
| `X.Y.Z` | one release, for `linux/amd64` and `linux/arm64` |
| `X.Y` | the latest `X.Y.z` |
| `latest` | the latest stable release |
| `X.Y.Z-amd64`, `X.Y.Z-arm64` | one architecture only |

The image is published from the release after 1.4.0, the first with data APIs. Use the same
version as the other images: the Helm chart's `image.dataApi` does it. Every release is
described in the [changelog](https://github.com/JumpAsAService/Tabularia/blob/main/CHANGELOG.md).

## Getting started

Data APIs are part of the Helm chart: switch them on with `scripts.enabled` and
`dataApi.enabled`, give them a host of their own with `dataApi.publicHost` if the systems that
call them should see nothing else of Tabularia, and set the administrator's caps in the
`dataApi` values — [Data APIs](https://tabularia.org/docs/data-apis/),
[Kubernetes and Helm](https://tabularia.org/docs/deploy/kubernetes/). With Docker Compose there
are no data APIs.

## Configuration

The orchestrator sets every variable; the ones the program reads:

| Variable | Default | What it is |
|---|---|---|
| `DATA_API__ID`, `DATA_API__GATEWAY_URL`, `DATA_API__POD_TOKEN` | required | the data API, the gateway's address in the cluster, the pod token |
| `DATA_API__DATA_DIR` | `/data` | where the data files go: the only place it writes |
| `DATA_API__PORT` | `8000` | the port |
| `DATA_API__MEMORY_LIMIT_MB` | 60% of the container's memory | DuckDB's memory: a third serves, two thirds prepare the next version |
| `DATA_API__THREADS` | the container's CPUs | DuckDB's threads preparing a version; serving, as many queries at once, the others wait their turn |
| `DATA_API__FORWARDED_ALLOW_IPS` | none | the proxies (the ingress controller) whose `X-Forwarded-For` gives the client's address, for the keys' address limits |
| `DATA_API__QUERY_SECONDS` | `30` | the longest a request may take |
| `DATA_API__MANIFEST_MAX_AGE_SECONDS` | `900` | past this without a manifest the pod refuses every key rather than miss a revocation |

## The container

- Listens on port **8000**: `/data/{api}/…` for the calls, `GET /data/{api}/health` without a
  key, `/healthz` (liveness) and `/readyz` (ready once every resource is prepared, with a fresh
  manifest) for Kubernetes, which the Ingress does not publish.
- Runs as an unprivileged user (uid **10001**), writes only to `/data` (an `emptyDir` sized by
  the orchestrator from the data), and works with a read-only root filesystem and no
  capability.
- One Go program with DuckDB linked in (cgo), on Debian bookworm slim: about 168 MB.
- Logs one JSON line per request on its standard output (the key's first characters, the
  resource, the status, the rows, the milliseconds), never a key nor a query.
- Stops cleanly on `SIGTERM`, finishing the requests in flight.

Before a release is pushed, its images are built for both architectures, started for a smoke
test, checked for the licences of the packages they hold (the program's own, in
`/usr/share/licenses/tabularia-data-api`), and scanned for secrets. The process is described in
the repository: [release process](https://github.com/JumpAsAService/Tabularia/blob/main/docs/release-process.md).

## Links

- Website and documentation: [tabularia.org](https://tabularia.org/), and the data APIs'
  guide: [tabularia.org/docs/data-apis](https://tabularia.org/docs/data-apis/)
- Source code and issues: [github.com/JumpAsAService/Tabularia](https://github.com/JumpAsAService/Tabularia)
- Licence: [AGPL-3.0](https://github.com/JumpAsAService/Tabularia/blob/main/LICENSE). The
  notices of the third-party software are linked from the app's menu and the website's footer.
