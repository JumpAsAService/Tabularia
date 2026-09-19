# Tabularia

**Self-hosted, open-source visual data-preparation platform.** A Tableau Prep–style
flow editor on pluggable engines: build a pipeline once, preview it on the whole
dataset, and decide *later* where the compute runs.

**The same flow scales from a laptop to a warehouse without being rewritten.** Start
with nothing installed but Docker: transformations stream through memory in-process,
no cluster, no external service, no per-query bill. When the data outgrows one
machine, point the flow at **DuckDB** (same process, spilling to disk), a **managed
ClickHouse**, or **Google BigQuery** (serverless, nothing to operate) — the flow
definition does not change by a single node. The parquet stays in your bucket and the
warehouse reads it **in place**: nothing is copied, nothing streams back through the
workers. Storage follows the same curve, from MinIO on the same host to S3, Scaleway or
Google Cloud Storage; deployment from one `docker compose up` to Helm on Kubernetes.

Database sources and destinations, loops, charts, scheduling, cross-flow lineage, an
AI assistant over your own catalog and an audit trail come in the box.

> *Tabularia takes its name from the Tabularium, the records office of ancient Rome —
> the place where the state's tables were kept in order.*

Current release: **1.0.0 «Appio»** — see [CHANGELOG.md](CHANGELOG.md).

---

# Part I · Business

## The problem

Visual data-prep tools either cost thousands per seat, or quietly work on **samples**
of your data and hope for the best. And once a pipeline exists, moving it to bigger
iron usually means rebuilding it: the tool that was comfortable on a laptop does not
speak to the warehouse the company already pays for.

Tabularia is an open-source alternative you host yourself: a visual, drag-and-drop
pipeline builder that runs on the **entire** dataset, connects straight to your
databases, keeps a full record of who did what — and lets the same flow run on five
engines, from an in-process library to a serverless warehouse.

## What you get

- **Build pipelines visually — no code.** Drag transformations onto a canvas, connect
  them, and preview the result at any step. Familiar to anyone who has used Tableau
  Prep, Alteryx, or Knime.
- **No sampling, ever — unless you ask for it.** Previews, profiles and charts run on
  the *whole* dataset through a streaming engine: what you see in the editor is what
  you ship. Where every read is billed, an optional development sample keeps the
  editor cheap, per node and switchable, and never touches production runs.
- **Compute where it makes sense.** The same flow runs on Polars, DuckDB, chDB, an
  external ClickHouse or BigQuery. Design in-process, then hand production to the
  warehouse: the parquet stays in your bucket, the warehouse reads it **in place**,
  nothing is copied and nothing streams through the workers. See
  [Run it on your engine — or on your warehouse](#run-it-on-your-engine--or-on-your-warehouse).
- **Explore without building a flow.** The **Viewer** is a read-only BI surface over any
  datasource: filters, calculated fields, pivots and charts, computed on the whole
  dataset by the engine you choose. Any configuration can be **saved as a view** into a
  folder, next to flows and datasources. A view stores the configuration and never the
  rows, so reopening it reads today's data — it is a lens, not a copy.
- **Connect to your databases.** Read directly from Postgres, MySQL, MariaDB,
  ClickHouse, Trino and SharePoint Excel; write results back to a database table or
  publish them as a reusable datasource. Files (CSV/Excel/JSON/parquet) work too.
- **Ask your data.** An optional AI assistant answers questions in plain language over
  the datasources you are allowed to read: it finds the table, reads the curated field
  descriptions, writes and runs the query on your engine, and shows the result table the
  numbers come from. Conversations are saved and can be reopened, so a question already
  answered is never paid for twice, and **every turn shows what it cost**. Any
  OpenAI-compatible endpoint works; an administrator decides which models may be used,
  and every query lands in the audit log.
- **Nothing happens to your data that you cannot preview.** Every node shows its row
  and column count as the preview arrives — no extra query, it is what the preview
  already returned — and edges can be removed with a click or the keyboard. Repeated
  previews reuse a per-step cache that is written *after* the answer is on screen, so a
  second click is fast without the first one waiting for it.
- **Schedule and forget.** Put flows on a schedule (timezone-aware, DST-safe) and let
  them refresh on their own; a load heatmap shows busy time-bands and collisions
  before they bite.
- **Full history & governance.** Every run is recorded with downloadable results; an
  **audit log** captures logins, flow runs, data exports and permission changes —
  "who entered, who ran what, which data was downloaded" — with a 24-hour access chart.
- **See how everything connects.** A dedicated **Lineage** view maps datasources and
  flows across the whole workspace: provenance, impact/blast-radius, staleness and
  broken references at a glance.
- **Multi-user from day one.** JWT login or SSO (Keycloak, Entra ID, Auth0, Okta),
  users & groups, nested projects with inherited view / edit / manage / connect
  permissions. Everyone works in the same place, safely.
- **Yours to run.** Self-hosted, AGPL-3.0 open source, no per-seat fees, no data
  leaving your infrastructure. Point it at managed S3 / Postgres / Redis if you prefer.
- **Speaks your language, looks the part.** UI in **English, Italian, French, German,
  Spanish**, with **Dark, Light, Dracula and Monokai** themes, selectable per user.

## Run it on your engine — or on your warehouse

Every flow is stored as a declarative list of operations, decoupled from execution.
That is what makes the engine a deployment choice instead of a design constraint:

| Engine | Where the compute runs | What you need | How it is billed | Choose it when |
|---|---|---|---|---|
| **Polars** *(default)* | In the worker process | Nothing | Your servers | Day-to-day work, anything that fits the worker's memory |
| **DuckDB** | In the worker process, spilling to disk | Nothing | Your servers | Very large joins and aggregations on one machine |
| **chDB** | In the worker process (embedded ClickHouse) | The chDB build of the image | Your servers | Prototyping the ClickHouse dialect locally |
| **ClickHouse (external)** | On a managed or self-hosted ClickHouse server | A ClickHouse reachable from the workers | The server you rent | Big tables, sub-second aggregations, a warehouse you already run |
| **Google BigQuery** | On BigQuery, serverless | A GCP project and a bucket on Google Cloud Storage | Per byte read, no idle cost | Data already on GCP, elastic scale, zero servers to operate |

A flow carries **two engines**: the development one, used by the editor and manual
runs, and an optional production one, used by the scheduler and "Run in production".
Design on Polars with a development sample, schedule on ClickHouse or BigQuery: the
flow does not change, and every run records the engine it actually ran on. An
administrator can restrict which engines the installation allows.

The engines agree on one data standard — nulls, casts, joins, aggregates, sort order,
pivots — locked by an oracle test suite that runs the same hand-computed expectations
on all five. The remaining differences are documented, not discovered in production:
[docs/engines/engine-differences.md](docs/engines/engine-differences.md).

### How far it scales, and what it costs to get there

The flow is a declarative list of operations; the engine is a runtime decision. That
one property is what lets an installation grow without a migration:

| Stage | Compute | External services | Typical scale |
|---|---|---|---|
| **Laptop / one server** | Polars, streaming in the worker's memory | none | up to what the worker's RAM allows |
| **One bigger machine** | DuckDB, spilling to local disk | none | joins and aggregates past RAM |
| **Your warehouse** | Managed or self-hosted ClickHouse | a ClickHouse the workers can reach | tens of millions of rows, sub-second aggregates |
| **Serverless** | BigQuery | a GCP project and a GCS bucket | elastic, no idle cost |

Nothing above is a rewrite: it is a dropdown on the flow, and a flow can use one engine
in the editor and another in production. Two mechanisms keep the growth honest:

- **A development sample, off in production.** Where every read is billed, an optional
  sample keeps the editor cheap. It is per source node, switchable, marked in the
  definition, and **scheduled runs never sample** — you cannot ship a sampled result by
  accident.
- **A step cache with a ceiling.** Repeated previews of the same chain reuse the
  previous step instead of recomputing it. The copy is made *after* the preview has
  answered, in a separate task, and steps above `CACHE__MAX_STEP_ROWS` are not cached at
  all: a big intermediate result never makes you wait for a copy you did not ask for.

### Managed ClickHouse

Set `CLICKHOUSE_EXTERNAL__HOST` and the engine appears in the picker. With the `s3`
transport the server reads and writes the parquet **directly on the object storage**
through its `s3()` table function: nothing passes through the workers, and an
aggregation over 25 million rows answers in 0.6 s instead of 18 s, because the parquet
is written with large row groups the server can skip. With the `push` transport
the worker stages the source in a table and streams the result back, which works with
any server that cannot see the bucket. Validated on Scaleway's managed ClickHouse;
ClickHouse Cloud speaks the same protocol and reads S3-compatible buckets the same way
(multi-replica services are not handled yet: query cancellation and the performance
page read one replica).

Optionally, datasets above a size threshold are copied once into a MergeTree on the
server so the Viewer's full scans read local columns instead of the bucket.

### Google BigQuery

Point the object storage at Google Cloud Storage, give the engine a project and a
service account, and every flow can run on BigQuery. Each source parquet becomes a
**temporary external table** of the query (`gs://bucket/key`), so no dataset is
loaded and no copy is made; the chain of operations is one GoogleSQL query; results
come back through the Storage Read API and are written as parquet like everywhere
else. Columns are checked with free dry runs; every job is capped by
`BIGQUERY__MAXIMUM_BYTES_BILLED` so a runaway query is refused before it runs.

The editor gets a **native step-cache**: intermediate steps are materialised as
expiring tables in a `tabularia_cache` dataset by a background task, so the first click
on a node never waits and the next ones read a small native table. Measured on a
25 M × 50 table with a 100 k development sample: 0.6–1.1 s per click with the cache,
1.1–2.9 s without.

Know the cost model before pointing the Viewer at a wide table: BigQuery bills the
**logical** bytes of the columns a query touches, whatever the file compression. On
that same 25 M × 50 table, aggregations read 100 MB–1 GB per query (a fraction of a
cent); sorting whole rows reads all 50 columns, about 19 GB, eleven cents per click.
The per-query cap and the development sample are the two guards; the free tier
covers 1 TB a month.

## Who it's for

Data & analytics teams that want a self-hosted, governed, no-sampling data-prep tool
without enterprise licensing — and anyone who needs an auditable, multi-user pipeline
builder that can start on one machine and grow onto the warehouse they already have.

---

# Part II · Technical

## Architecture

[![Tabularia runtime architecture](docs/architecture/tabularia-runtime.png)](https://jumpasaservice.github.io/Tabularia/architecture/runtime-architecture.html)

**[Open the interactive diagram](https://jumpasaservice.github.io/Tabularia/architecture/runtime-architecture.html)**
(pan/zoom, light/dark theme, search, guided views, PNG/SVG export). It is generated
with [Archify](https://github.com/tt-a1i/archify) from
[`docs/architecture/runtime.architecture.json`](docs/architecture/runtime.architecture.json);
every node cites the source files that implement it, verified against the commit
pinned in the spec.

| Runtime component | Role |
|---|---|
| Web UI | Nuxt 3 app, calls the gateway only (JWT) |
| Gateway | FastAPI control plane: auth, RBAC, audit, in-process scheduler; proxies to the engine after the permission check |
| PostgreSQL | control-plane metadata (users, groups, projects, versioned flows, connections, runs, schedules, audit) |
| Engine API | FastAPI on the private network: turns every preview and run into a Celery task |
| Valkey | Celery broker, step-cache index, preview slots |
| Run worker / preview worker | Celery workers on two queues (`celery`: runs, DB ingest, export · `preview`: interactive previews); in-process engines (Polars, DuckDB, chDB) and the clients of the external ones |
| Celery beat | cache eviction and storage statistics |
| Object storage | one bucket, all parquet: `datasets/` snapshots, `cache/` steps, `out/` results. MinIO locally; any S3 (Scaleway, AWS…) or Google Cloud Storage through its S3 API in the cloud |
| External engines *(optional)* | a managed ClickHouse reading and writing the parquet through `s3()`; Google BigQuery reading it as temporary external tables over GCS |
| External databases | sources ingested via ADBC into parquet snapshots; Output nodes can write tables back |
| VictoriaMetrics + Grafana | scrape the engine `/metrics`, celery-exporter, cAdvisor and node-exporter |

The **gateway** (control plane) is the only public ingress: it owns the metadata
Postgres and enforces auth + RBAC on every call before proxying to the internal
**engine** (data plane), which stays stateless (Valkey + object storage only).

That metadata Postgres has a diagram of its own:
**[Open the metadata database schema](https://jumpasaservice.github.io/Tabularia/architecture/metadata-schema.html)**
— 14 tables and all 26 foreign keys, read back from a live database rather than
transcribed from the models, and generated with Archify from
[`docs/architecture/metadata-schema.architecture.json`](docs/architecture/metadata-schema.architecture.json).

## Declarative IR & pluggable engines

Flows are stored as a **declarative IR** — a JSON list of typed operations — fully
decoupled from execution. Adding or swapping an engine touches neither the routes, the
workers, nor saved flows. Five engines are registered:

| Engine | id | Notes |
|---|---|---|
| **Polars** | `polars` | In-process, lazy, streaming. **Default**; full operation coverage. |
| **DuckDB** | `duckdb` | Out-of-core SQL (spills to disk) for very large joins/aggregations. Full operation coverage. |
| **chDB (ClickHouse)** | `chdb` | Out-of-core SQL with the ClickHouse dialect, embedded. Full coverage except `foreach`. |
| **ClickHouse (external)** | `clickhouse` | *Optional.* Same dialect and ops as chDB, executed on a **remote ClickHouse server**. Enabled by `CLICKHOUSE_EXTERNAL__HOST`. Transport `s3` (the server reads/writes parquet directly on the object storage) or `push` (staging table + streamed result). |
| **BigQuery** | `bigquery` | *Optional.* GoogleSQL over **temporary external tables** on GCS; results through the Storage Read API. Enabled by `BIGQUERY__PROJECT` plus a service-account key. Full coverage except `foreach` and `median` inside `pivot`. |

Each engine is a registry of per-operation implementations. DuckDB and chDB are
guarded imports — absent packages simply mark the engine unavailable without breaking
Polars; the external engines are listed but unavailable until configured. chDB is
**fork-unsafe**, so it is imported *lazily inside the Celery child* (never in the
prefork parent) to avoid inherited native-thread deadlocks. Users pick a **preferred
engine** in settings (default for the Viewer and new flows); each flow persists the
engine it was built with, so opening a non-preferred flow is regression-safe. Each
flow also carries an optional **production engine**: the editor (previews, manual
runs) uses the development engine, while scheduled runs and "Run in production" use
the production engine. Every run records the engine it actually ran on.

An administrator can narrow that choice for the whole installation. **Admin → Engines**
decides which engines a flow may be built on. Disabling an engine stops it from being
*chosen* — at creation, when changing a flow's engine, and as a production engine — but
deliberately does **not** stop the flows already using it: a switch in an admin panel
should never halt a scheduled DAG the moment it is pressed. The panel reports how many
flows still run on each engine — that list is the migration backlog. At least one
engine always stays allowed, and the picker keeps *not allowed here* apart from *not
configured*.

### External engines in detail

**ClickHouse (external)** — `CLICKHOUSE_EXTERNAL__HOST`, `__PORT` (8443 with TLS on
managed services), `__USERNAME`, `__PASSWORD`, `__DATABASE`, `__SECURE`, `__TRANSPORT`
(`s3` | `push`), `__S3_ENDPOINT` (the storage endpoint *as the server sees it*) and,
recommended, `__S3_NAMED_COLLECTION`: a named collection holding the bucket credentials
on the server, so keys never travel inside a query nor land in `query_log`. The engine
raises `max_threads` on scan-heavy queries to hide object-storage latency, never on
`LIMIT` queries; `__MATERIALIZE_MIN_ROWS` turns on the MergeTree copy for the Viewer.
Interactive previews carry a `log_comment` tag: when a newer preview supersedes one
still running, the server is told to `KILL` it. Preview timings and the server's own
`query_log` are shown on the admin **Performance** page.

**BigQuery** — `BIGQUERY__PROJECT`, `BIGQUERY__CREDENTIALS_FILE` (path of the
service-account JSON key inside the container) or `BIGQUERY__CREDENTIALS_B64` (the
same key, base64 on one line), `BIGQUERY__LOCATION` (empty = the bucket's region),
`BIGQUERY__MAXIMUM_BYTES_BILLED` (default 20 GiB) and `BIGQUERY__CACHE_DATASET`
(default `tabularia_cache`, empty disables the step-cache). The service account needs
*BigQuery Job User* on the project, *BigQuery Data Editor* for the cache dataset, and
read access on the bucket; the object storage must be Google Cloud Storage
(`STORAGE__ENDPOINT=https://storage.googleapis.com` with HMAC keys). Column names
BigQuery cannot represent (`Ragione Sociale`, `Importo (€)`) are mapped to safe names
inside the query and restored in the results; `compute` expressions are transpiled from
the common dialect with sqlglot (`%` → `MOD`, `strftime` → `FORMAT_DATE`). Jobs are
labelled with the preview tag so a superseded preview is cancelled on the service.
Verified against a real project: the cross-engine oracle suite passes on BigQuery
(`BIGQUERY_LIVE=1 pytest -k bigquery`).

## Operations

~18 transforms plus source / output / control nodes, all engine-agnostic in the IR:

`select · drop · rename · cast · compute · sql · filter · sort · limit · unique ·
fill_null · drop_nulls · group_by · pivot · unpivot · join · union · foreach`

- **`foreach`** is a loop container: it iterates its body over a driver table with
  `{{placeholder}}` substitution, appending results with bounded memory.
- **Upstream filters** per source node: AND-ed `{column, operator, value}` conditions set in
  the editor on the input datasource and saved with the flow. They become plain `filter`
  operations injected right after the source is read, before anything else (development
  sample included), in *every* mode — editor previews, scheduled runs, "Run in production"
  and the dbt export — so a flow can read only the slice it needs from a large table.
- **Cross-engine data standard.** The same flow must give the same data on every engine,
  so the engines follow one explicit semantics, SQL-like, locked by an oracle test suite
  (`backend/tests/test_data_correctness.py`, hand-computed expectations run on Polars,
  DuckDB, chDB, external ClickHouse and BigQuery): comparisons with NULL are false
  (`ne`/`not_in` drop NULLs); aggregates ignore NULLs, `count`/`n_unique` never count NULL,
  `sum`/`mean`/`min`/`max` of an all-NULL group are NULL, `std`/`var` are sample (n-1),
  `median` interpolates; sort puts NULLs **last** in both directions; failed casts give
  NULL (never an error), text is trimmed before parsing, text→int accepts integer literals
  only, number→int truncates; joins never match NULL keys, `on` keys are one coalesced
  column, `left_on`/`right_on` keep both key columns, a non-key homonym from the right
  gets `_right`; `compute` overwrites an existing column in place; integer sums stay
  exact int64; datetimes are **naive UTC instants** everywhere.
- **Development sampling** per source node: "first N rows" or "random p%" set in the
  editor and saved with the flow. It only affects previews and editor runs: the gateway
  resolver injects it solely in development, never for scheduled runs or "Run in
  production", and strips any such marked operation from production launches as defence
  in depth. With `PREVIEW__DEFAULT_SAMPLE_ROWS` set, every source node without a sample
  of its own gets an **automatic** "first N rows" sample, shown as a badge on the node
  and switchable per node ("off (all rows)") — the right default where every read is
  billed. Sample only the big table: joining two sampled sources loses most matches.
- **Field descriptions** on datasources: a hand-curated `{column: text}` map, kept
  separately from the inferred schema so it survives refreshes.
- **`pivot` / `unpivot`** follow one cross-engine standard: pivot columns are named by the
  value as text (`null` for NULL, `2024_web` for multi-column keys, existing combinations
  only, text-ordered), missing or all-NULL groups are NULL (`count`/`n_unique` → 0), and
  unpivot keeps only the index columns with the value cast to the common supertype.
- **`sql`** runs engine-native SQL against the node input (`FROM input`), with a
  guardrail floor that blocks filesystem / URL / executable access.
- **Nodes**: `source` (file or DB datasource), `output` (write to a DB table or
  publish a datasource; append/replace + post-SQL, and optionally a copy on an
  external S3 bucket — see [Publishing to an external bucket](#publishing-to-an-external-bucket)),
  `refresh` (re-ingest a DB source), `runflow` (invoke another flow).

Every node's output is **content-addressed and cached**: editing the last step of a
10-step flow recomputes one step, not ten. The copy is written *after* the preview
answers, by a separate task, and only for steps up to `CACHE__MAX_STEP_ROWS` (1M by
default): a step as large as the source is recomputed from the original parquet
instead of being copied. In-process engines and ClickHouse keep the steps as parquet
under `cache/`; BigQuery keeps them as expiring native tables. Cache entries evict by
TTL.

## Storage layout

All parquet, on any S3-compatible object storage (MinIO by default) or on Google Cloud
Storage through its S3 API — the two things GCS does differently (no batch delete,
no chunked request checksums) are handled by the storage layer:

```
raw/       ingested files, as uploaded
datasets/  normalized parquet datasources
cache/     content-addressed step outputs
out/       run results, downloadable as CSV/Excel
```

Parquet files are written with large row groups (`INGEST__PARQUET_ROW_GROUP_ROWS`,
default 1 M) and, when a datasource declares sort keys, ordered on them: that is what
lets an external engine skip most of a file on a filtered query instead of reading it.

## Publishing to an external bucket

An Output node that publishes a datasource can also drop a copy of the same parquet
on a bucket Tabularia doesn't own — the one another team already reads from. It is
the integration path for the common case where nobody wants a data-prep tool writing
into their database, and it needs no orchestrator: the consumer polls a path.

The copy always lands at **the same key, always parquet, overwritten on every run**,
so whoever reads it agrees on one path once and never has to discover a new filename.
The overwrite is a single object and becomes visible only when the upload completes:
a reader gets either the previous file or the new one in full, never a mixture.

It is **best effort by design**. The datasource is the result; the copy is a delivery
downstream of it. A copy that fails leaves the run successful and the datasource
published, and the error is recorded on the run and returned by the API.

> **No screen shows that error yet.** Until that is wired up, treat a successful run
> as saying nothing either way about the copy.

> **Use a different bucket from the one Tabularia itself runs on.** A copy written
> into that bucket is not checked against the objects already there: aim it at a key
> another datasource is using and you overwrite that datasource's snapshot, outside the
> permission model.

## Auth, RBAC & audit

Administrators are granted in two ways: a personal flag, or membership of a group
marked as an **administrators group** — every member is an administrator for as long as
they belong to it, which also means the right can be granted from an identity provider
through SSO group mapping. Both paths are audited, and no administrator can remove
their own last route to administration: demoting or deactivating yourself, demoting or
deleting your only admin group, or leaving it, is refused rather than silently
performed.

- **JWT** login through the gateway; stateless verification with a throttled
  `last_seen` touch for active-session tracking.
- **RBAC**: users & groups, nested projects, inherited **view / edit / manage /
  connect** permissions; access resolves to a set of `readable_project_ids` applied on
  every query. Object-level permissions extend to connections and datasources on the
  data plane.
- **Audit log**: append-only, with text snapshots that survive rename/delete. Captures
  logins, CRUD, flow runs, data exports, and permission changes; admin tab with a
  24-hour access-activity chart. Secrets (DB passwords) are encrypted at rest (Fernet).
- **SSO / OIDC** *(optional)*: sign in against Keycloak, Microsoft Entra ID (MSAL),
  Auth0 or Okta with the authorization-code flow (PKCE, `state`, `nonce`, id_token
  validated against the IdP JWKS). Users are provisioned on first login and the IdP's
  `groups` claim is reconciled onto Tabularia groups **by name**. Off until
  `OIDC__ISSUER` is set; local login always stays available as break-glass. See
  [`docs/design/sso-group-mapping.md`](docs/design/sso-group-mapping.md) and the
  runnable Keycloak example in [`docs/examples/keycloak/`](docs/examples/keycloak/).

### Free-form SQL, contained

A flow can carry a node holding hand-written SQL, and the assistant writes one for
every question. That node is bound to a single rule: **it may read its own input and
nothing else.** The query is parsed, and anything in a `FROM` or `JOIN` position that
is not `self` / `input` (or a `WITH` defined in the same query) is refused — table
functions, another database's tables, `information_schema`, dictionary and settings
lookups included. A query that cannot be parsed is refused rather than run. On DuckDB
the node executes in a locked in-memory sandbox with external access disabled; Polars
only ever sees the registered frame.

This matters because the engine holds credentials that can read the whole bucket: if a
query could name another table, the permission on a datasource would be decorative.

## AI assistant

Optional, in the gateway, built with [pydantic-ai](https://ai.pydantic.dev) against any
OpenAI-compatible endpoint (`AI__BASE_URL`, `AI__SECRET_KEY`; verified on Scaleway
Generative APIs). The agent has three tools and nothing else: `list_datasources`,
`describe_datasource` (columns, types and the hand-written **field descriptions** — the
semantic context that makes the query right — plus a few sample rows) and
`query_datasource` (one read-only `SELECT … FROM self`, executed by the engine through
the same `sql` node and guardrails as the editor, rows capped by
`AI__MAX_RESULT_ROWS`). Every tool re-checks the caller's permissions: a datasource
outside the user's VIEW scope looks exactly like one that does not exist. Each query is
an audit event (`ai.query`, with the SQL, the model and the row count).

**Conversations are saved** in the gateway's database, private to their author and
deleted with the account. They can be searched by title, reopened with their steps and
result tables, and continued — a question already answered costs nothing to read again.
The assistant names each conversation itself from the first question. Because the
history lives on the server, the browser sends only a conversation id: it cannot forge
a history, and a turn is bounded by `AI__MAX_COST_PER_TURN_USD` and
`AI__MAX_INPUT_TOKENS_PER_TURN`, not merely by a number of model round-trips.

**What a call cost is on screen**, under each answer and for the conversation as a
whole. The figure comes from the price data shipped with pydantic-ai — there is no
price list of ours to go stale — and it is an estimate from the model's list price,
not your provider's invoice; a dash means that model could not be priced, which is not
the same as free.

While the assistant works, the page names the phase it is actually in — scanning the
catalog, reading the fields of a datasource, querying it, writing the answer — read
from the same stream events the steps are read from.

Models are an administrator's decision (Admin → AI models): the provider's catalog is
listed, nothing is enabled by default, and embedding or audio models cannot be enabled
for chat.

On the external ClickHouse engine the assistant's queries can run as a **dedicated
read-only account** (`CLICKHOUSE_EXTERNAL__AI_USERNAME`): a second barrier behind the
SQL node's allow-list, with no access to `system`, to any table, or to `url()` and
`file()`. See [docs/engines/clickhouse-ai-user.md](docs/engines/clickhouse-ai-user.md).

## Scheduling & timezone

Cron-style schedules are evaluated in a **deployment-wide timezone** (`APP__TIMEZONE`,
DST-aware) and stored/returned in UTC; the frontend displays browser-local time. A
schedule-load heatmap surfaces busy bands and collisions against a configurable
worker capacity.

## Monitoring

Ships in the box: **VictoriaMetrics + Grafana** dashboards (task durations, cache hit
rate, storage growth, per-container memory, preview latency by engine), plus cAdvisor,
node-exporter and a celery-exporter. Grafana is embedded in an admin-only Monitoring
tab, and a **Performance** page shows run durations, preview histograms per engine and
the external ClickHouse's slowest queries without any extra table.

## Services (docker-compose)

| Service | Role |
|---|---|
| `frontend` | Nuxt 3 app (SSR) — http://localhost:3000 |
| `gateway` | FastAPI control plane (auth, RBAC, audit, scheduling) — :8000 |
| `backend` | FastAPI engine (preview) |
| `worker` / `preview-worker` | Celery workers (runs, DB ingest / preview) |
| `postgres` | control-plane metadata |
| `redis` | Valkey — broker, cache index, counters |
| `minio` | S3 object storage — :9001 |
| `victoriametrics` / `grafana` | metrics + dashboards — :3001 |
| `cadvisor` / `node-exporter` / `celery-exporter` | metric exporters |

## Quickstart

> Production on Kubernetes: see [`docs/deploy/kubernetes.md`](docs/deploy/kubernetes.md)
> for the components, the hardened backend image, ingress and storage sizing, and the
> Helm chart in [`infrastructure/helm/tabularia`](infrastructure/helm/tabularia).
> Upgrading: [`docs/deploy/release-checklist.md`](docs/deploy/release-checklist.md).

Requires Docker and Docker Compose.

```bash
git clone git@github.com:JumpAsAService/Tabularia.git
cd Tabularia/infrastructure
cp .env.example .env        # then edit: secrets, admin credentials, timezone
# required in every environment: the key that encrypts DB-connection passwords
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
#   → paste it as SECURITY__FERNET_KEY in .env (compose refuses to start without it)
docker compose up -d
```

| Service   | URL                    | Default credentials                |
|-----------|------------------------|------------------------------------|
| App       | http://localhost:3000  | `admin@tabularia.local` / `admin`  |
| Gateway   | http://localhost:8000  | (JWT via app login)                |
| Grafana   | http://localhost:3001  | `admin` / `admin`                  |
| MinIO     | http://localhost:9001  | `minioadmin` / `minioadmin`        |

Upload a CSV/XLSX/JSON/parquet file **or** connect a database, drag transformations
from the sidebar, connect nodes, preview at any point, then run — or download any
node's data as CSV/Excel.

**Adding an external engine** is configuration only. For a managed ClickHouse:

```bash
CLICKHOUSE_EXTERNAL__HOST=xxxx.datawarehouse.it-mil.scw.eu
CLICKHOUSE_EXTERNAL__PORT=8443
CLICKHOUSE_EXTERNAL__SECURE=true
CLICKHOUSE_EXTERNAL__USERNAME=...
CLICKHOUSE_EXTERNAL__PASSWORD=...
CLICKHOUSE_EXTERNAL__S3_ENDPOINT=https://s3.it-mil.scw.cloud   # the bucket as the server sees it
```

For BigQuery, with the bucket on Google Cloud Storage:

```bash
STORAGE__ENDPOINT=https://storage.googleapis.com               # HMAC keys in STORAGE__ACCESS_KEY / SECRET_KEY
STORAGE__REGION=auto
BIGQUERY__PROJECT=my-gcp-project
BIGQUERY__CREDENTIALS_B64=...                                  # base64 -w0 service-account.json, one line
PREVIEW__DEFAULT_SAMPLE_ROWS=100000                             # keep the editor cheap
```

Recreate the workers (`docker compose up -d`) and the engine shows up in the picker;
`.env.example` documents every variable.

**Secrets:** `SECURITY__FERNET_KEY` is mandatory everywhere — gateway, engine and
workers refuse to start without a valid key, in development too. With
`APP__ENV_NAME=production` the gateway additionally refuses the dev-default secrets
(`JWT__SECRET`, admin and DB passwords) until they are overridden. Storage, broker,
database, timezone and engines are all env-driven — pointing at managed services is a
config change, not a code change.

## Sample database (optional)

A realistic, opt-in **cured-meats company** dataset (Postgres ERP + ClickHouse CRM,
generated with Faker/numpy) is available to exercise the connectors and build the
line-level margin case. It ships built-in seasonality — monthly volume, product &
channel seasonal mix, and per-warehouse fulfilment anomalies — so pivots tell a story.
See [`infrastructure/sampledb/README.md`](infrastructure/sampledb/README.md).

```bash
SAMPLEDB_SCALE=small docker compose \
  -f infrastructure/docker-compose.sampledb.yml \
  --profile generate run --rm sampledb-generator
```

## Internationalization & themes

UI copy lives in per-language i18n catalogs (`en`, `it`, `fr`, `de`, `es`), selected
per user and persisted via cookie for SSR safety. Themes (`dark`, `light`, `dracula`,
`monokai`) are CSS-variable palettes on `:root[data-theme]`, persisted in localStorage
with an anti-flash head script. Code and comments are English-only; end-user strings
are translated.

## License

Copyright © 2025–2026 Leonardo Trivelli.

Licensed under the **GNU Affero General Public License v3.0** — see [LICENSE](LICENSE).
For commercial licensing options, contact the author.

Tabularia is **not affiliated with, endorsed by, or sponsored by Salesforce, Inc.**
"Tableau" and "Tableau Prep" are trademarks of Salesforce, Inc., referenced solely
for comparison purposes. "BigQuery" and "Google Cloud" are trademarks of Google LLC;
"ClickHouse" is a trademark of ClickHouse, Inc.

### Third-party services

The default docker-compose deployment includes **MinIO** (AGPL-3.0) and **Grafana**
(AGPL-3.0) as separate, unmodified services accessed over standard APIs (S3, HTTP).
They are deployment choices, swappable via environment variables. All libraries used
by Tabularia itself are under permissive licenses (MIT/BSD/Apache-2.0/ISC).
