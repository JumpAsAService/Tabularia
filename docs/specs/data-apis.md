# Spec: data APIs

Status: decided with the owner on 2026-11-07; open questions answered the same day. Pieces 1-4
done, and the runtime made the administrator's (owner, 2026-11-07: «The runtime is the
administrator's»); open: the live «Data APIs» category of Administration → Settings (see
«Administration»).

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
| Pods | **One Deployment per data API**, with its replicas, CPU and memory **set by an administrator only** (owner, later the same day: see «The runtime is the administrator's») |
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
  per pod: shown on the page, set by an administrator only (below).
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
- **Settled while writing the reconcile loop (piece 3)**:
  - *The objects* (`gateway-go/internal/dataapis/cluster.go`, the loop in
    `internal/api/jobs_data_apis.go`): every one named `<release>-data-api-<id>` and labelled
    with the release, `app.kubernetes.io/component: data-api` and `tabularia.org/data-api:
    <id>`; applied server-side (field manager `tabularia-data-apis`), so a round that changes
    nothing writes nothing. Every labelled object of a data API the database does not have
    as active or paused (deleted, a draft, gone with its creator's account in cascade, the
    feature switched off) is removed. One round at a time across orchestrators (advisory
    lock 7402150). The kinds: Secret, Service, NetworkPolicy, Deployment, Ingress, PDB
    (`maxUnavailable: 1`, while the pods are at least two), HPA (`autoscaling/v2`, CPU at 70%
    of the request; the Deployment's count is then left to it).
  - *The Deployment*: requests equal to the limits (the runtime's CPU and memory; the
    pods are Guaranteed), no service account token, no service links, `/readyz` as readiness
    (every 5 s) and `/healthz` as liveness (every 10 s, six failures), 40 s to stop; a
    rollout adds a pod before it removes one (`maxSurge: 1, maxUnavailable: 0`), and the
    replicas spread over the nodes when they can (`ScheduleAnyway`).
  - *The pod token*: an activation drops the stored hash (the previous token stops at
    once); the loop's next round makes a new token (`RotatePodToken`), writes it to the
    Secret, and puts its fingerprint's first 16 characters on the pod template, which
    restarts the pods with it. A Secret whose token is not the stored one (deleted, edited)
    gets a new token the same way.
  - *The disk*: the pod's `emptyDir` (`sizeLimit`), its ephemeral storage requested at the
    same size and limited 256Mi above. Measured on kind (42 million rows, six columns, a
    parquet of 465 MB, a pod of 1 CPU and 1Gi): at a refresh the data directory went from
    312 MB (the file served) to 2,321 MB at its highest (the new parquet, the new file of
    312 MB and 1.2 GB that DuckDB spilled while sorting), five times the parquet. Hence six
    times the parquets plus 256Mi, rounded up to a power of two (a refresh that grows the
    data seldom changes it, and a change restarts the pods), at least 1Gi and at most
    `DATA_API__MAX_DISK` (20Gi). The parquet's size comes from the engine's signed-link route,
    which now returns it (`/files/presigned` → `size`, from the storage's HEAD), asked once an
    hour per data file.
  - *The client's address*: `DATA_API__FORWARDED_ALLOW_IPS` is the chart's
    `dataApi.trustedProxies`, by default `gateway.trustedProxies` (the private ranges).
    ingress-nginx replaces the `X-Forwarded-For` a client sends with the address it sees, so a
    forged header changes nothing: on kind a key limited to `203.0.113.0/24` was refused
    with and without `X-Forwarded-For: 203.0.113.9`, one limited to the host's address let in.
  - *The NetworkPolicy*, one per data API, made from what the chart computes: in, the
    ingress controller's namespace and pods (`dataApi.networkPolicy.ingressController`) on
    port 8000, nothing else; out, DNS (port 53, anywhere: the cluster's DNS may be a
    node-local cache), the gateway's pods on 8000, the storage on its port (its namespace when
    the endpoint is a Service of the cluster, else `storageCIDRs` or every public address).
    The scripts' own policy (`podSelector: {}` in the same namespace) would open the pods to
    the internet, policies adding up: it leaves the data APIs' pods out. Measured on kind
    with Calico: a pod in `default`, in the scripts' namespace and in Tabularia's own reached
    neither the pods nor their Service (the ingress controller did); from a data API's pod the
    gateway and MinIO answered, PostgreSQL, Valkey, the frontend, the Kubernetes API, the
    registry and the internet did not, all of them reachable from a pod of Tabularia's
    namespace without the policy.
  - *Exposure*: the Ingress is in the data APIs' namespace (an Ingress reaches the Services of
    its own namespace only), on `DATA_API__INGRESS_HOST` (the first of `ingress.hosts` or
    `dataApi.ingress.host`), with the app's class and `dataApi.ingress.annotations`. Without a
    `tls` of its own: on the app's host ingress-nginx serves every Ingress of a host with that
    host's certificate. `DATA_API__INGRESS_TLS_SECRET` (a Secret of the namespace) and the
    `IngressHost` hook are where piece 4's `dataApi.publicHost` comes in. On kind: `/data/{slug}/`
    reaches the pods, `/healthz`, `/readyz`, `/data/{slug}/../../readyz` and another slug do not.
  - *RBAC*: a Role in the namespace, bound to the orchestrator's account only (the gateway,
    which answers the internet, gets nothing there): `get, list, create, patch, delete` on
    Deployments, Services, Secrets, PDBs, HPAs, Ingresses and NetworkPolicies; `get, list` on
    the pods.
  - *Queries take turns*: the load test on kind found a pod of 1 CPU answering fewer
    requests with three clients than with one (440 a second with one, 180 with three, 150 with
    eight). DuckDB alone does the same (20 million rows, 1 CPU: 1,150 lookups a second one at a
    time, 430 three at once, 280 eight at once). The pod now runs at most as many queries at
    once as it has CPUs (`DATA_API__THREADS`), the others waiting their turn within the
    request's time: 1,300 a second with eight at once, and on kind 455 with three clients, 385
    with eight.
  - *Measured on kind* (42 million rows, three pods of 1 CPU and 1Gi on two workers, through
    ingress-nginx, keep-alive, random keys): a lookup by key 2 ms in the pod, 2.2 ms for the
    client one at a time; 895 requests a second with three clients (p99 5.7 ms), 1,093 with
    eight (p99 18 ms), 1,074 with 24 (p99 64 ms, the pods' CPU spent). Activated, the first pod
    answered 44 s later (the next round, then 26.5 s to download and prepare); a refresh of the
    datasource was served 33 s after its run ended (a poll, then 22 s to prepare); a revoked key
    refused 11 s after the revocation (poll 20 s). Memory serving: about 230 MB anonymous per pod
    for 42 million rows, 47 MB for 150,000: DuckDB's cache stops at its share of the pod's
    memory, the file itself stays in the page cache, which the kernel reclaims. Preparing, the
    pod's memory reached its limit of 1Gi with page cache, 650 MB of it anonymous, without
    being killed. Switched to autoscaling (one to three pods), a load on one pod made the autoscaler ask
    for a second 33 s later, ready 17 s after; the loop leaves the count to it (switching to
    autoscaling takes the Deployment to the minimum first: its count is no longer the loop's).
  - *Status*: migration 0041 adds `data_api_status` (what the loop saw of the Deployment: the
    pods wanted, ready, updated, the disk, what the cluster refused) and `data_api_pods` (each
    pod as the cluster sees it — phase, ready, restarts, node, the reason it does not run —
    and as it reports itself). The pods send their status at every poll with the usage (even
    with nothing counted): ready, the manifest's state, the version each resource serves or
    prepares, the size of its file, the last error. `GET /data-apis/{id}/status` (VIEW) gives
    the page all that, the version each datasource has now and how many pods serve it, and the
    traffic of the last hour, day or week; the page asks it every 5 s while the API is active.
    The loop deletes the usage older than `DATA_API__USAGE_RETENTION_DAYS`.

## Administration

Administration → Settings, category «Data APIs» (live, as the other panel settings):
enabled; the most data APIs; the most replicas in total; the poll; the key's longest life and
default rate; usage retention. Administration lists every data API with its creator, state,
runtime, pods and traffic, sets the runtime of any of them, and can pause or delete any of
them.

Settled while writing piece 4:

- *The list* is Administration → **Data APIs** (`GET /admin/data-apis`, an administrator or an
  observer, as the other panels): every data API with its folder, its creator (and whether
  the account is active), its state, the pods the loop last saw ready out of those wanted and
  what the cluster refused, the requests, errors and `429`s of the last 24 hours with the last
  call, and the keys that answer now. `POST /admin/data-apis/{id}/pause` and
  `DELETE /admin/data-apis/{id}` (an administrator only, never an observer) act on any data
  API; the delete works with the feature switched off too, to clean what is left. The audit
  records them as the page's actions (`data_api.pause`, `data_api.delete`) with
  `"by": "administration"`.
- *The caps stay in the chart* for now (`dataApi.*`, read at start): the section shows them,
  and the live «Data APIs» category of Administration → Settings is **not done**. Every cap is
  read in many places of the gateway and of the reconcile loop (the pods, the disk, the
  Ingress) and the panel's machinery checks each setting against the Python gateway's
  catalogue: a piece of its own. `enabled` would stay out of it in any case: the chart renders
  the RBAC and the namespace only with `dataApi.enabled`.

## The runtime is the administrator's (owner, 2026-11-07)

Decided by the owner after piece 4: **only an administrator decides the resources of a data
API**, for each one. Whoever creates it (MANAGE) no longer chooses them.

- *The creator's side*: the page shows the runtime read-only (the pods, or the minimum and the
  maximum with autoscaling, the CPU and the memory of each pod), with a line saying an
  administrator decides it. A new data API, and a copy, start from the default: **one pod of
  250m and 256Mi** (`dataapis.DefaultRuntime`). The gateway refuses a create or an edit that
  sends a different runtime: `403` with `{"code": "runtime_admin_only"}`; the same one sent back
  (as a page that sends the whole document does, whatever the spelling: `1` and `1000m`,
  `1Gi` and `1024Mi`) passes. The page itself no longer sends it, so an administrator's change
  made while it was open is never undone by a save. A version restored brings back its
  resources, not its runtime; an edit never writes the runtime (only the creation does). The
  data APIs made before keep the runtime they have.
- *The administrator's side*: `PUT /admin/data-apis/{id}/runtime` (an administrator, never an
  observer, on any data API), from Administration → Data APIs (a dialog per row) and from the
  API's page when an administrator opens it (`can_set_runtime` in the detail). It is **not a
  version**: what the data API publishes stays as it is. The audit records `data_api.runtime`
  with the runtime `before` and `after`, the state and `"by": "administration"`; the same
  runtime again answers `200` and records nothing. An active data API's change is taken with
  the activations' lock (7140), so the two count each other's pods. Refused: a runtime out of
  the bounds below (`422`), an active one past the most pods of all the active ones (`409`),
  with the feature switched off (`409`).
- *The chart's per-API caps are gone*: `dataApi.maxReplicas`, `maxCpu` and `maxMemory`
  (`DATA_API__MAX_REPLICAS`, `_MAX_CPU`, `_MAX_MEMORY`) limited what a creator could ask; the
  administrator decides now, and was told «even above the chart's caps». The data APIs were in
  no release yet, so nothing is kept for compatibility. **Kept**: `dataApi.maxTotalReplicas`
  (the most pods of all the active ones together, checked at every activation and every
  change of an active one's runtime) and `dataApi.maxDisk` (the disk of a pod).
- *The bounds of sense* (`dataapis.CheckRuntime`, not settings): CPU and memory are Kubernetes
  quantities; **100m to 16 CPU** a pod; **256Mi to 64Gi** a pod (DuckDB takes 60% of the pod's
  memory and prepares with two thirds of it: below 256Mi it fails on a table of a few million
  rows, and 128Mi was the old minimum); **at most 40 pods** for one data API (the status keeps
  50 pods' rows, with room for a rollout's), and never more than `maxTotalReplicas`, which no
  activation could then pass. A pod larger than any node waits unscheduled: its old pods keep
  answering (the rollout never takes one down before a new one is ready) and the Pods tab says
  why.
- *Applied by the reconcile loop*: the Deployment is applied from the database at every round
  (30 s), so a change of an active data API reaches it at the next one: the replicas, the HPA
  and the PodDisruptionBudget, and the requests and limits of the pods' template, which rolls
  (`maxSurge: 1`, `maxUnavailable: 0`: a new pod is ready, its data prepared, before an old one
  stops). DuckDB's memory follows by itself: the pod reads its own limit
  (`/sys/fs/cgroup/memory.max`, 60% of it) and its CPUs for the threads; the disk does not
  depend on the runtime (six times the parquets, `dataApi.maxDisk` at most).
- *Measured on kind* (2026-11-07, the data API of 42 million rows, active, calls in a loop
  through the ingress all along): an administrator took it from one pod of 1 CPU and 1Gi to two
  of 1 CPU and 1536Mi. The loop's next round applied it, the rollout ended 71 s after the
  change (the new pods prepare their data before they are ready), and the 1,400 calls made
  meanwhile all answered `200` (p50 2.8 ms); two took 5 s, each right after an old pod
  stopped: the ingress controller still had its address and waited its connect timeout before
  trying another pod. Each new pod has requests and limits of 1 CPU and 1536Mi, and DuckDB
  921 MB (60%), where it had 614 MB with 1Gi. Back to one pod of 1Gi: 59 s, every call
  answered. In Chrome and Firefox (50 checks each): someone with MANAGE sees the runtime
  read-only and the gateway refuses theirs; an observer sees it in Administration and changes
  nothing; the administrator sets 3 pods of 2 CPU and 3Gi from Administration's dialog (100Mi
  said below the bounds first) and autoscaling 1 to 4 from the page; the creator's next edit
  keeps it; no horizontal scroll at 390 px, the dialog included.

## Piece 4: a host of their own, the documentation, the lineage

Settled while writing it:

- *`dataApi.publicHost`*, as `share.publicHost`: the reconcile loop puts every data API's
  Ingress on that host (`DATA_API__INGRESS_HOST`) and on no other, so on it only
  `/data/{slug}/` is routed and everything else is the controller's 404; the interface's host
  no longer routes `/data/…` to the pods. The chart refuses a host that is one of
  `ingress.hosts` or `share.publicHost`. The certificate is a Secret of the data APIs'
  namespace (an Ingress reaches Services and Secrets of its own namespace only),
  `dataApi.tlsSecretName` or `<release>-data-api-tls` when the interface has TLS; one Ingress
  of the chart with no path (`<release>-data-api-host`, not labelled as a data API's, so the
  loop leaves it alone) asks for it with the main Ingress's annotations: with several Ingresses
  naming one Secret, only one may be cert-manager's. `DATA_API__PUBLIC_BASE_URL`
  (`dataApi.publicBaseUrl`, else `https://<publicHost>`, `http://` without a certificate) is
  the address the page, the documentation, the administration and the downloaded OpenAPI
  write; empty, the page uses its own origin. Measured on kind: see «Testing strategy».
- *The documentation* is `GET /data-apis/{id}/documentation` (VIEW, no key, a draft too): the
  resources with each column's kind, the filters, the error catalogue and the OpenAPI document.
  The generator moved from the pods to `internal/dataapiwire` (standard library only), with the
  kinds and the catalogue of the errors (`ErrorCodes`, which the description's `code` lists; a
  test of the pods checks every code they write is in it). The pods type the columns from the
  DuckDB file, the gateway from the datasource's Polars types (`KindOfPolars`); a test of the
  pods compares the two documents for the same definition, and one of the gateway compares its
  document with the one made from the manifest the pods get. The description's version is the
  definition's (the manifest carries it, `definition_version`), no longer the manifest's time.
  The column types a reader sees are those of the columns that go out (the API's own
  contract), never the datasource's other columns. The page's `curl` lines read the key from
  `$TABULARIA_KEY`: the page never writes one.
- *Polars and DuckDB agree*: checked on parquet files Polars wrote (Polars 1, DuckDB 1). Two
  differences came out and are fixed: a Polars `Time` is `TIME_NS` in the parquet, which the
  pods did not know (now made `TIME` when prepared, to the microsecond), and an `Int128` is
  bytes (`BLOB`), not filterable (the gateway and the page now refuse it, as the pods would).
  A `Duration` is its count of time units, a whole number.
- *The page* has three tabs, Definition, Pods and traffic, Documentation (`?tab=`), the keys
  and the versions beside them.
- *The lineage*: a node `dataapi:{id}` (type `data_api`) read from each datasource it
  publishes (edge `read`), with its address, state and version; a centre of the graph
  (`type=dataapi`). Explore's deletion dialog of a datasource counts it among its dependents.
  The search and Explore had the data APIs since piece 1.

## Model (migration 0040)

- `data_apis`: id, project_id, name, slug (unique), description, state, creator_id
  (`ON DELETE CASCADE`), runtime JSON (replicas or min/max, cpu, memory: an
  administrator's, outside the versions; a version's content keeps the runtime it had), pod
  token hash,
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
4. Public host (`dataApi.publicHost`), administration section, **the API's documentation
   in the page** (a «Documentation» tab: every resource with its columns, types, filters and
   operators, the query parameters, the answers and the errors, a curl example filled in
   with the API's address, and the OpenAPI JSON to download; generated by the gateway from
   the definition, with the same code the pods use, so it needs no key, only VIEW on the
   API), docs, Selenium on Chrome and Firefox.

## Testing strategy

- Go with a database: create with and without MANAGE, as admin, with the feature off or on
  Compose; slug and resource names; keys created, expired, revoked, renewed with grace;
  the creator losing VIEW on a datasource or MANAGE, deactivated; the manifest with a
  wrong or another API's token; datasource deletion refused; caps; audit of each event; the
  runtime refused to the creator (`403`) and passed when sent back the same, set by an
  administrator on someone else's data API past the old caps, refused out of the bounds or
  past the total, refused to an observer, audited, a copy back to the default.
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
- The usual regression: Go, Python engine and migrations, npm, typecheck; the new image in
  the release workflow's checks. No comparison with the Python gateway (owner, 2026-11-07).
- *Piece 4 on kind* (2026-11-07): with `dataApi.publicHost: data.localhost`, on that host only
  `/data/{slug}/` answered (the pod's `health` 200, the rows 401 without a key and 200 with
  one); `/`, `/login`, `/api/health`, `/api/auth/me`, `/share/…`, `/_nuxt/…`, `/healthz`,
  `/readyz`, `/data/{slug}/../../readyz` and another slug were the controller's 404, and the
  interface's host no longer reached the pods (`/data/…` went to the interface). Without it,
  `/data/{slug}/` answered on the interface's host as before. The documentation the gateway
  wrote for the data API of 42 million rows was byte for byte the `openapi.json` its pod
  served (a Polars parquet: integers and floats). In Chrome and Firefox (54 checks each): a
  data API made, keyed and activated from its page (its pod ready in 45 s), the documentation's
  `curl` lines run as copied against the ingress (200, and on the interface's host not), the
  downloaded OpenAPI equal to the pod's, paused from Administration (its calls `503` within 2
  s) and deleted there, audited; no horizontal scroll at 390 px; the lineage centred on a data
  API in both.

## Boundaries

- Always: read-only; bound parameters; keys only as hashes; the creator's authority
  checked at every poll; caps enforced by the gateway, not by the page.
- Ask first: anything that writes back to a datasource; aggregations; live queries;
  exposing the app's own API on the public host.
- Never: S3 credentials in the pods (presigned URLs only); the key in the URL; a query
  from the pods to the source databases; new routes in the Python gateway.
