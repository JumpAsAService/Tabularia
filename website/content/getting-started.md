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

Step by step, from an empty machine to a first scheduled flow, with every command and
click checked on a clean install: [Getting started with Docker Compose](getting-started-docker-compose.md).
The short version follows.

You need Docker with Docker Compose. This starts the **development** stack: the
quickest way to see the product, not a production setup:

```bash
git clone https://github.com/JumpAsAService/Tabularia.git
cd Tabularia/infrastructure
docker compose up -d
```

Nothing to configure. Open http://localhost:3000 and create the administrator: the page
asks for it only while the installation has no account. The first start builds the
images, 2–3 minutes on a fast machine and 5–15 on a laptop.

| Service | URL | Credentials |
|---|---|---|
| App | http://localhost:3000 | the administrator you create at the first visit |
| Gateway | http://localhost:8000 | through the app's sign-in |
| Grafana | http://localhost:3001 | `admin` / `admin` |
| MinIO | http://localhost:9001 | `minioadmin` / `minioadmin` |

Upload a CSV, Excel, JSON or parquet file, or connect a database. Drag transformations
from the sidebar, connect the nodes, click any node to preview its output, then run the
flow, or download any node's data as CSV or Excel.

### Sample data

Already there: an ERP on PostgreSQL and a CRM on ClickHouse for a cured-meats company,
and a `Sample` folder in Tabularia with the two connections, eleven flows (three
scheduled) and four saved views. It appears a couple of minutes after the administrator
is created. Details, and how to leave it out, in the
[sample database README](../infrastructure/sampledb/README.md).

## Go to production

Docker Compose is for trying Tabularia; production runs on Kubernetes with the Helm chart.

| For | Read |
|---|---|
| Kubernetes | [Kubernetes and Helm](deploy/kubernetes.md) and the chart in [`infrastructure/helm/tabularia`](../infrastructure/helm/tabularia) |
| Every install and upgrade | [Install and upgrade checklist](deploy/release-checklist.md) |
| Every setting | [Configuration reference](configuration.md): the environment variables, with their defaults |

`SECURITY__FERNET_KEY` and `AUTH__ADMIN_PASSWORD` are mandatory there: the chart has no
first-start setup. With `APP__ENV_NAME=production` the gateway also refuses the development default secrets (`JWT__SECRET`, admin and database
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
