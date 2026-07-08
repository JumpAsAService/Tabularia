# Tabularia

**Self-hosted, open-source visual data-preparation platform** — a Tableau Prep–style
flow editor on a streaming Polars engine, with full-data previews, loops, and charts.
No sampling.

> *Tabularia takes its name from the Tabularium, the records office of ancient Rome —
> the place where the state's tables were kept in order.*

## Why

Visual data-prep tools either cost thousands per seat, or quietly work on **samples**
of your data and hope for the best. Tabularia takes a different route:

- **No sampling, ever.** Previews, charts, and profile queries run on the *entire*
  dataset through a streaming engine. A `group_by` over 220M rows peaks at ~450 MiB
  of worker memory.
- **Incremental step cache.** Every node's output is content-addressed and cached:
  editing the last step of a 10-step flow costs one step, not ten.
- **Loops, like the big ETL suites.** A `foreach` container iterates its body over a
  driver table with `{{placeholder}}` substitution — appended results, bounded memory.
- **Charts on real aggregates.** The chart panel (bars, lines, areas, pie, treemap,
  scatter) queries the engine, not the preview sample.
- **Multi-user from day one.** JWT auth, users/groups, nested projects with inherited
  view/edit/manage/connect permissions, flows saved per project.

## Architecture

```
 Browser (Nuxt 3 + Vue Flow editor)
    │
    ▼
 Gateway  ── FastAPI · JWT auth · RBAC · metadata in Postgres
    │           (users, groups, projects, permissions, flows)
    ▼
 Engine   ── FastAPI · declarative IR → pluggable executor (Polars today)
    │           preview (sync) · runs & ingest (Celery workers)
    ├── Valkey  — broker, cache index, counters
    └── S3 storage — MinIO by default; any S3 endpoint via env vars
                     (raw/ → datasets/ → cache/ → out/, all parquet)
```

Flows are stored as a **declarative IR** (a JSON list of typed operations), fully
decoupled from the execution engine — swapping or adding engines (e.g. DuckDB) does
not touch routes, workers, or saved flows. Monitoring ships in the box:
VictoriaMetrics + Grafana dashboards (task durations, cache hit rate, storage growth,
per-container memory).

## Quickstart

Requires Docker and Docker Compose.

```bash
git clone git@github.com:JumpAsAService/Tabularia.git
cd Tabularia/infrastructure
cp .env.example .env        # then edit: secrets, admin credentials
docker compose up -d
```

| Service   | URL                    | Default credentials        |
|-----------|------------------------|----------------------------|
| App       | http://localhost:3000  | `admin@tabularia.local` / `admin` |
| Gateway   | http://localhost:8000  | (JWT via app login)        |
| Grafana   | http://localhost:3001  | `admin` / `admin`          |
| MinIO     | http://localhost:9001  | `minioadmin` / `minioadmin`|

Upload a CSV/XLSX/JSON/parquet file, drag transformations from the sidebar, connect
nodes, preview at any point, then run — or download any node's data as CSV/Excel.

**Production note:** the gateway refuses to start with `APP__ENV_NAME=production`
unless the dev-default secrets (`JWT__SECRET`, admin and DB passwords) have been
overridden. Storage, broker, and database endpoints are all env-driven — pointing
at managed S3/Postgres/Redis-compatible services is a config change, not a code change.

## Status

Working: file ingest (auto-converted to parquet, sync/async by size), ~13 operations
(select/filter/join/group_by/foreach/…), visual editor with drag & drop, saved flows
in permissioned project folders, incremental cache with TTL eviction, CSV/XLSX export
from any node, full-data charts, monitoring stack.

On the roadmap: database connections as sources (ingest → parquet), run history with
downloadable results, scheduling, union & computed-column operations, DuckDB engine,
an agentic AI copilot that generates flows against the declarative IR.

## License

Copyright © 2025–2026 Leonardo Trivelli.

Licensed under the **GNU Affero General Public License v3.0** — see [LICENSE](LICENSE).
For commercial licensing options, contact the author.

Tabularia is **not affiliated with, endorsed by, or sponsored by Salesforce, Inc.**
"Tableau" and "Tableau Prep" are trademarks of Salesforce, Inc., referenced solely
for comparison purposes.

### Third-party services

The default docker-compose deployment includes **MinIO** (AGPL-3.0) and **Grafana**
(AGPL-3.0) as separate, unmodified services accessed over standard APIs (S3, HTTP).
They are deployment choices, swappable via environment variables. All libraries used
by Tabularia itself are under permissive licenses (MIT/BSD/Apache-2.0/ISC).
