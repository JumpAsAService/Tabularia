---
site: true
description: Try Tabularia on one machine with Docker Compose, then run it in production on Kubernetes with the Helm chart.
---

# Getting started

Tabularia runs on your own infrastructure. Everything it needs ships as containers:
the web app, the gateway, the engine and its workers, PostgreSQL for the metadata,
Valkey for the queues and MinIO as object storage. The images are published on
[Docker Hub](https://hub.docker.com/u/jumpasaservice) for amd64 and arm64.

## Try it on one machine

> The images on Docker Hub are release 1.1.1. Some features described on this site (SFTP,
> HTTP API and S3-folder sources, dynamic placeholders, flow groups and more) are on the
> `main` branch and ship with release 1.2.0: see the [changelog](../CHANGELOG.md).

Step by step, from an empty machine to a first scheduled flow, with every command and
click checked on a clean install: [Getting started with Docker Compose](getting-started-docker-compose.md).
The short version follows.

You need Docker with Docker Compose. This starts the **development** stack: the
quickest way to see the product, not a production setup:

```bash
git clone https://github.com/JumpAsAService/Tabularia.git
cd Tabularia/infrastructure
cp .env.example .env        # then edit: secrets, admin credentials, timezone
# required: the key that encrypts database-connection passwords (the last value in .env wins)
echo "SECURITY__FERNET_KEY=$(openssl rand -base64 32 | tr '+/' '-_')" >> .env
docker compose up -d
```

| Service | URL | Default credentials |
|---|---|---|
| App | http://localhost:3000 | `admin@tabularia.local` / `admin` |
| Gateway | http://localhost:8000 | through the app's sign-in |
| Grafana | http://localhost:3001 | `admin` / `admin` |
| MinIO | http://localhost:9001 | `minioadmin` / `minioadmin` |

Upload a CSV, Excel, JSON or parquet file, or connect a database. Drag transformations
from the sidebar, connect the nodes, click any node to preview its output, then run the
flow, or download any node's data as CSV or Excel.

### Sample data

For a realistic dataset to explore, turn on the `samples` profile in `infrastructure/.env`:

```bash
COMPOSE_PROFILES=samples
SAMPLES_SCALE=small        # small | medium | robusta
```

It adds an ERP on PostgreSQL and a CRM on ClickHouse for a cured-meats company, and builds
a `Sample` folder in Tabularia with the two connections, eleven flows (three scheduled)
and four saved views. Details in the
[sample database README](../infrastructure/sampledb/README.md).

## Go to production

Docker Compose is for trying Tabularia; production runs on Kubernetes with the Helm chart.

| For | Read |
|---|---|
| Kubernetes | [Kubernetes and Helm](deploy/kubernetes.md) and the chart in [`infrastructure/helm/tabularia`](../infrastructure/helm/tabularia) |
| Every install and upgrade | [Install and upgrade checklist](deploy/release-checklist.md) |
| Every setting | [Configuration reference](configuration.md): the environment variables, with their defaults |

`SECURITY__FERNET_KEY` is mandatory everywhere. With `APP__ENV_NAME=production` the
gateway also refuses the development default secrets (`JWT__SECRET`, admin and database
passwords) until they are replaced. Storage, broker, database, timezone and engines are
set through the environment: pointing Tabularia at managed services is a configuration
change. Most settings are environment variables, all listed in the
[configuration reference](configuration.md); [`.env.example`](../infrastructure/.env.example)
is the starting file.

## Then

- Add an engine: a managed **ClickHouse** or **Google BigQuery** is configuration only:
  see the [overview](../README.md#run-it-on-your-engine--or-on-your-warehouse) and the
  [engine differences](engines/engine-differences.md).
- Connect your sign-in: [SSO and group mapping](design/sso-group-mapping.md).
- Let another system drive Tabularia: [API, tokens and webhooks](api.md).
- Take a flow with you: [export to dbt](dbt-export.md).
