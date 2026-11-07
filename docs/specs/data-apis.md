# Spec: data APIs

Status: decided with the owner on 2026-11-07; open questions answered the same day.

## Objective

Other systems (a CRM, an ERP, a website, an internal service) need Tabularia's tables
**one record at a time**: «the score of customer 123», «the open orders of this
account». Today they can only export a whole file (`POST /tasks/export`) through the
app's API. A **data API** is a new Explore object that publishes one or more datasources
as a read-only HTTP API, answered by **pods of its own** on Kubernetes, so the traffic of
the other systems never slows the people using Tabularia.

Who uses it: an analyst or a folder's owner publishes it (MANAGE); the people who
integrate the CRM call it with a key; the administrator sets how much of the cluster
the data APIs may take.

Success: a CRM reads one customer's row by its id in milliseconds; a refresh of the
datasource reaches the API by itself; a key can be revoked and stops working within a
minute; the API's description imports into the CRM or Postman as OpenAPI.

## Decisions (owner, 2026-11-07)

| Topic | Decision |
|---|---|
| Where | **Kubernetes only**. With Docker Compose the feature is off and the object is not offered (as saved scripts and notebooks) |
| Pods | **One Deployment per data API**, with its replicas, CPU and memory chosen on its page, within the caps the administrator sets |
| Data | A Tabularia datasource is served from its **latest parquet**: the pods keep a local copy, swapped at every refresh. No query reaches the source databases |
| Who creates | Whoever has **MANAGE** on the folder, or an administrator: the data API and its keys |
| First version | **Read only** (filters, pagination, chosen columns) **plus an OpenAPI description** of every data API |
| Not now | Writing, aggregations, named parameters as in the reports |
| Later | **Sources other than datasources** (owner: yes): a table of a connection read live, the result of a saved SQL script. Not in the first version; a resource keeps its source as a kind plus an id, so they fit in later |
| Count | **Yes**: `_count=true` adds how many rows match |
| Who calls | **Systems outside the cluster only** (owner: «they are for integrating»): every call comes through the ingress, never straight to the pods |

## The object

A data API lives in a folder of Explore, like a saved script: moved, renamed, duplicated,
searched, deleted; MANAGE on the folder (or admin) to create, edit and manage keys, VIEW
to see its page. Its page has:

- **Name**, description, and the **slug** in its address (`crm-customers`), unique in the
  installation, lowercase letters, digits and dashes.
- **Resources**: one per datasource it publishes, each with
  - a **name** in the path (`customers`), unique in the API;
  - the **datasource** (by id: a rename or a move does not break it);
  - the **columns that go out** (chosen one by one; a column added later to the
    datasource does not go out until someone adds it);
  - the **columns one may filter on**, and for each the operators allowed (below);
  - the **default order** and the **largest page** (default 1,000, at most 10,000).
- **Runtime**: replicas (or minimum and maximum with autoscaling on CPU), CPU and memory
  per pod, within the administrator's caps.
- **Keys** (below), **state** (draft, active, paused), and what the cluster says: pods
  ready out of wanted, the data version each resource serves, the last error.
- Versions of the definition, as the other objects, and **Activate**, **Pause**,
  **Delete**.

The authority is the **creator's, now**, as for the share links: every time the pods ask
for their manifest the gateway checks that the creator's account is active, that they
still have MANAGE on the folder (or are an admin), and VIEW on each datasource. A resource
the creator can no longer read stops answering (`403`, saying the resource is not
available); a creator deactivated or without MANAGE pauses the whole API. Masking for
observers does not apply: an observer cannot create one.

## Keys

- A key belongs to one data API and names who uses it (`Salesforce production`). Created
  by MANAGE or admin; shown **once** (`tdk_` + 40 random characters); the database keeps
  its SHA-256 and the first 8 characters to recognise it.
- An **expiry** is required (at most `DATA_API__KEY_MAX_DAYS`, default 365), a **rate
  limit** per minute (default 600), optionally the **resources** it may read (default:
  all of the API's) and the **source addresses** allowed (CIDR list, optional).
- Revoke, renew (a new key, the old one kept for a grace period chosen at renewal),
  last used, requests of the last 24 hours.
- Sent as `Authorization: Bearer tdk_…` (or `X-API-Key`). Never in the query string.

## The HTTP interface

```
GET /data/{api}/{resource}?customer_id=123
GET /data/{api}/{resource}?region__in=North,South&signup__gte=2026-01-01&_columns=id,name&_sort=-score&_limit=100
GET /data/{api}/{resource}?_cursor=…
GET /data/{api}/openapi.json
GET /data/{api}/health
```

- **Filters** only on the columns marked filterable: `col=` (equal), `col__in=` (comma
  list, at most 500 values), `col__gte/__gt/__lte/__lt=` (numbers and dates),
  `col__null=true|false`. Values are typed by the column's type; a wrong type is a `400`
  that names the column. Several filters are ANDed. A filter on another column is a `400`.
- `_columns`: a subset of the columns that go out; `_sort`: columns with `-` for
  descending, among the filterable ones; `_limit`: up to the resource's largest page;
  `_count=true`: the answer adds `count`, the rows matching the filters (not only the
  page); `_format=json` (default) or `csv`.
- **Pagination** by cursor: the answer's `next` carries the data version and the position;
  a cursor of a version that is no longer served answers `409` («the data changed, start
  again»), so a page never mixes two refreshes.
- JSON answer: `{"data": [...], "next": "…" | null, "version": "2026-11-07T08:12:03Z"}`
  (plus `"count": 1234` with `_count=true`); dates in ISO 8601, decimals as strings when they do not fit a float exactly.
- Errors: `{"detail": "a sentence", "code": "…"}` with `400`, `401` (no or wrong key),
  `403` (key not allowed on that resource, address not allowed, resource no longer
  readable by the creator), `404`, `409`, `429` (with `Retry-After`), `503` (data still
  loading).
- `openapi.json`: OpenAPI 3.1 generated from the definition: one path per resource, the
  filters as parameters with their types, the row schema from the columns that go out,
  the bearer scheme. The page in Tabularia shows it, with a `curl` example per resource.
  It needs a valid key, as the data.
- Settled while writing the pods (piece 2): `next` is the value to give `_cursor`, and a
  cursor carries its query, so `_cursor` comes alone (with `_format` at most); the default
  page is the resource's largest; the `version` is when the datasource's data was written (the
  run that wrote it ended, or its refresh), to the second, and the pods prepare again whenever
  the data file changes (another object, another run) or the columns and the order do; a
  cursor is bound to both. CSV gives the cursor, the version and the count in `X-Next-Cursor`,
  `X-Data-Version` and `X-Total-Count`. A float that is not a number goes out as `"NaN"` or
  `"Infinity"`. A key sent in the address is refused (`400 key_in_query`). The `openapi.json`
  lists only the resources the key may read. A pod whose manifest is older than
  `DATA_API__MANIFEST_MAX_AGE_SECONDS` (15 minutes) answers `503`: a revoked key must not
  live on while the gateway is out of reach. `GET /data/{api}/health` needs no key.

## Runtime

- **Program and image (measured 2026-11-07)**: a Go program with DuckDB linked in
  (`github.com/duckdb/duckdb-go/v2`, MIT, prebuilt static libraries for linux amd64 and
  arm64; cgo, so a build stage of its own), in **a Go module of its own**,
  `gateway-go/dataapi` (`internal/pod`: the server, DuckDB, keys, cursor, OpenAPI). In the
  gateway's module, `go mod download` in its image would fetch DuckDB's libraries of every
  platform: its build stage went from 26 s to 38 s and its module cache from 52 MB to
  1.2 GB. The module shares the gateway's own code that is the standard library only
  (`internal/dataapiwire`: the manifest, the usage, the keys' rules; `internal/clientip`)
  through a `replace` of the checkout. Its own image, `tabularia-data-api`
  (`gateway-go/dataapi/Dockerfile`, built from `gateway-go/`; 84 MB of binary, 168 MB in all):
  the gateway's image stays small and without cgo. Keys, limits, cursors and the HTTP
  answers are written once, in Go, with the gateway's conventions.
- **Data, read lazily**: the table is **never loaded in memory**. At start and at every
  change, a pod downloads each resource's parquet through a **short-lived presigned URL**
  into its `emptyDir` and turns it once into a **DuckDB database file**: only the columns
  that go out, **sorted by the first filterable column** (the key a CRM asks by). DuckDB
  keeps the min/max of every column for each block of rows (its zone maps) and reads a
  block from disk only when a query needs it, through a buffer capped by
  `memory_limit`; the file is opened read-only. Each request is a prepared DuckDB query
  with bound parameters only (no text from the request becomes SQL): a lookup by key
  touches one block, only the asked columns are decoded, `_limit` stops the scan as soon
  as the page is full. The new version is swapped in atomically (a new file, then the
  connection moves to it); the old one serves until then. A pod is **ready** once every
  resource is prepared.

  Measured on 20 million rows (six columns), in a container of 1 GB and 2 CPUs:

  | Serving from | Preparation | Lookup by key (avg / max) | Filter on two other columns, page of 1,000 | Memory serving |
  |---|---|---|---|---|
  | the parquet, read lazily (163 row groups of 122,880, metadata cached) | 13 s | 6.5 ms / 18 ms | 20–33 ms | 90 MB |
  | the parquet with row groups of 10,000 | 14 s | 35 ms / 47 ms | 72 ms | 75 MB |
  | **a DuckDB file, sorted by the key** (chosen) | 13 s, 213 MB on disk | **0.8 ms / 1.7 ms** | 13 ms | 70 MB |
  | the same with an index (ART) on the key | 19 s, 654 MB | 0.5 ms / 0.9 ms | 16 ms | 80 MB |

  The pods' program as built (piece 2, `gateway-go/dataapi/scripts/standalone.sh`: the
  gateway, MinIO and the pod in a container of 1 GB and 2 CPUs, a datasource of 20 million
  rows and five columns, 346 MB of parquet): ready 15 s after starting (download and
  preparation, 14 s; 144 MB on disk); a lookup by key over HTTP, 1,000 random customers,
  **1.3 ms in the pod (p50; 1.5 ms average, 3.7 ms p99)**, 1.8 ms for the client; a filter on
  two other columns with a page of 1,000, 15 ms; a count on a column, 47 ms. The program's
  memory serving 221 MB (166 MB anonymous in the cgroup), 500 MB at its highest while
  preparing. A revoked key refused 4 s after the revocation, a refresh served 14 s after it
  (poll 5 s). Preparing sorts the table: 244 MB of DuckDB memory fail with two threads and
  pass with one (23 s), 400 MB pass with two (13 s); hence the pod gives DuckDB 60% of its
  memory, a third to serve and two thirds to prepare, with as many threads as 200 MB each
  allow. Each request prepared its query again at first: keeping the prepared statements by
  their text took a lookup from 2.5 ms to 1.5 ms (average, in the pod).

  Smaller row groups make the parquet slower, not faster: the cost is checking every
  group's statistics, not reading the data. The index saves 0.3 ms for three times the
  disk and is left out. Reading straight from the object storage at each request was
  left out too: every lookup would pay network round trips.
- **Manifest**: every pod asks the gateway `GET /internal/data-apis/{id}/manifest` every
  `DATA_API__POLL_SECONDS` (default 20) with the API's pod token. The manifest carries
  the definition, each resource's data version and presigned URL, and the keys
  (SHA-256, expiry, revoked, limits, resources, addresses). So a revoked key, a refresh
  or an edit reaches every pod within the poll; keys are checked **in the pod**, with no
  call to the gateway per request.
- **Pod token**: random, per data API, in the Deployment's Secret, its SHA-256 in the
  database; valid only on the manifest and usage endpoints of its own API; rotated at
  every activation.
- **Rate limit**: per key, in each pod, at the key's limit divided by the replicas
  (documented as approximate).
- **Usage**: each pod sends with its poll the counts per key per minute (requests, rows,
  errors, `429`s) → table `data_api_usage` (minute buckets, kept
  `DATA_API__USAGE_RETENTION_DAYS`, default 30); the page draws them. Every request is
  also logged by the pod on stdout as JSON (key prefix, resource, status, rows, ms): the
  cluster's logs and the SIEM take it from there. The audit records the definition's
  changes and every key event (`data_api.create/update/activate/pause/delete`,
  `data_api.key_create/revoke/renew`), not each request.
- **Kubernetes objects**, created and kept in line by a reconcile loop in the
  orchestrator role (every 30 s, as the notebooks'): in `DATA_API__NAMESPACE` (default:
  the scripts' namespace), per data API a Deployment, a Service, a Secret, a
  PodDisruptionBudget when replicas > 1, an HPA when autoscaling, all labelled with the
  API's id; a paused API keeps its objects at 0 replicas, a deleted one loses them.
  Pods: non-root 10001, read-only root, no capabilities, the resources chosen.
- **Network**: a NetworkPolicy lets the pods reach only the gateway's manifest endpoint
  and the object storage; ingress **only from the ingress controller**: the data APIs are
  for integrating systems outside the cluster, and no pod or namespace inside it may call
  them directly.
- **Exposure**: always through an Ingress that Tabularia keeps for each active API, with
  only `/data/{slug}/`. On the app's own host by default; on a host of its own when the
  chart's `dataApi.publicHost` is set (the address to give the CRM, with nothing else of
  Tabularia on it, as `share.publicHost` for the share links); the TLS secret from the
  chart. The Service is internal and only the ingress controller reaches it.

## Administration

Administration → Settings, category «Data APIs» (live, as the other panel settings):
enabled; the most data APIs; the most replicas per API and in total; the largest CPU and
memory per pod; the poll; the key's longest life and default rate; usage retention.
Administration lists every data API with its creator, state, pods and traffic, and can
pause or delete any of them.

## Model (migration 0040)

- `data_apis`: id, project_id, name, slug (unique), description, state, creator_id
  (`ON DELETE CASCADE`), runtime JSON (replicas or min/max, cpu, memory), pod token hash,
  versioned definition (as the other objects), created/updated.
- `data_api_resources`: id, data_api_id, name, datasource_id (`ON DELETE RESTRICT`: a
  datasource published by an API cannot be deleted until it is taken out, the dialog
  names the API), columns JSON, filters JSON, default sort, max page.
- `data_api_keys`: id, data_api_id, name, prefix, hash, expires_at, rate_per_minute,
  resources JSON, cidrs JSON, revoked_at, grace_until, created_by, created_at,
  last_used_at.
- `data_api_usage`: data_api_id, key_id, minute, requests, rows, errors, throttled.

## Where the code goes

- **Gateway: Go only** (`gateway-go/internal/dataapis`): the object's routes, keys,
  manifest, usage, reconcile loop (`internal/kube`), search, Explore, lineage (a data API
  is a reader of its datasources), audit, settings. Nothing in the Python gateway.
- **The pods' program**: `gateway-go/dataapi`, a module of its own (`main.go`, and
  `internal/pod`: the server, DuckDB, keys, cursor, OpenAPI), what it shares with the gateway
  in `gateway-go/internal/dataapiwire`, its Dockerfile and its place in the release workflow
  (built for amd64 and arm64, smoke-tested, licences and secrets scanned).
- **Frontend**: Explore type, the page (resources, columns, filters, runtime, keys, usage,
  OpenAPI and examples), the admin section, i18n in the 5 languages.
- **Helm**: RBAC on Deployments, Services, Secrets, PDBs, HPAs and Ingresses in the
  namespace; `dataApi.publicHost`; NetworkPolicies. Docs: a guide page, the configuration
  reference, the API docs.

## Pieces

1. Model, migration 0040, routes and page (definition, resources, keys), with the
   feature visible only on Kubernetes.
2. The pods' Go program and image, with manifest, keys, filters, cursor, OpenAPI;
   tested alone against MinIO.
3. Reconcile loop, RBAC, NetworkPolicy, activation and pause; usage and the page's
   status; trying it on kind (3 nodes, Calico).
4. Public host (`dataApi.publicHost`), administration section, docs, Selenium on Chrome
   and Firefox.

## Testing strategy

- Go with a database: create with and without MANAGE, as admin, with the feature off or on
  Compose; slug and resource names; keys created, expired, revoked, renewed with grace;
  the creator losing VIEW on a datasource or MANAGE, deactivated; the manifest with a
  wrong or another API's token; datasource deletion refused; caps; audit of each event.
- The pods' program (Go): filters of each type and operator, wrong types, unknown columns, `_columns`,
  `_sort`, pages and cursors across a version swap (`409`), CSV, rate limit, address
  limits, `503` before the load, no SQL from the request (injection attempts in every
  parameter), OpenAPI validated by a schema validator.
- kind: activate, scale, pause, delete; a refresh reaching the pods within the poll; a key
  revoked refused within the poll; the NetworkPolicy refusing everything else; the public
  host answering only `/data/…`; a load test (one key, several replicas) for the latency.
- Laziness, measured again in the cluster: on a datasource of the robust samples (tens of
  millions of rows) a lookup by key answers in about a millisecond and the pod's memory
  stays flat whatever the table's size.
- The usual regression: Go, Python engine, npm, typecheck, diffcheck; the new image in
  the release workflow's checks.

## Boundaries

- Always: read-only; bound parameters; keys only as hashes; the creator's authority
  checked at every poll; caps enforced by the gateway, not by the page.
- Ask first: anything that writes back to a datasource; aggregations; live queries;
  exposing the app's own API on the public host.
- Never: S3 credentials in the pods (presigned URLs only); the key in the URL; a query
  from the pods to the source databases; new routes in the Python gateway.
