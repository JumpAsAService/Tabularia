# Data APIs

A data API publishes one or more datasources as a read-only HTTP API, for the systems that
need Tabularia's tables one record at a time: a CRM asking for the score of customer 123,
a website asking for the open orders of an account. It lives in a folder of Explore, like
a SQL script, and is answered by pods of its own on Kubernetes, so the other systems'
traffic never slows the people using Tabularia. The design is in
[the spec](specs/data-apis.md).

> **In short.** A data API's page keeps its definition, its versions, its state and its keys;
> the orchestrator runs the pods that answer the calls, at `/data/{address}/` on the app's own
> host or on a host of the data APIs' own ([A host of their own](#a-host-of-their-own)). The page
> has three tabs: **Definition**, **Pods and traffic** ([On the cluster](#on-the-cluster)) and
> **Documentation**, everything the systems that integrate it need
> ([The Documentation tab](#the-documentation-tab)). Administration lists every data API
> ([In Administration](#in-administration)) and decides how many pods each one gets and how big
> ([The runtime](#the-runtime)); the lineage shows which datasources each one publishes.

## Where it is offered

On **Kubernetes only**, with the scripting environments on (`SCRIPTS__ENABLED`, the same
criterion as the saved scripts and the notebooks) and with `DATA_API__ENABLED`, which is
off by default; the Helm chart sets both, the second from `dataApi.enabled`. With Docker
Compose there are none: Explore offers no «New → Data API», the lists are empty and every
other route answers 409.

## Who may do what

- **MANAGE** on the folder, or an administrator, makes a data API, edits it, activates and
  pauses it, deletes it and manages its keys. An observer never does.
- Only an **administrator** decides its **runtime**: how many pods answer it and how big each
  one is ([The runtime](#the-runtime)).
- **VIEW** on the folder reads its page (without its keys).
- Whoever saves a resource must be able to read its datasource. A resource someone else
  published on a datasource you may not read stays as it is when you save; you may take it
  out, not change it.
- A data API answers on its **creator's** authority: if the creator's account is
  deactivated, or loses MANAGE on the folder, or may no longer read one of the datasources,
  the page says so, it cannot be activated, and the pods stop answering (the resource with
  `403`, or the whole data API with `503`). Duplicate
  it to publish it on your own authority.

## Making one

Explore → **New → Data API** in a folder where you have MANAGE. The page has:

- **Name** and **description**, and the **address**: lowercase letters, digits and dashes,
  unique in the installation (`crm-customers`). The other systems call
  `/data/crm-customers/{resource}`. The address of an active data API does not change:
  pause it first.
- **Resources**, one per datasource it publishes, each with
  - a **name** in the path (`customers`), unique in the API;
  - the **datasource**, kept by its id: renaming or moving the datasource does not break it;
  - the **columns that go out**, chosen one by one: a column added later to the datasource
    does not go out until someone adds it;
  - the **columns one may filter on**, among those that go out, and for each the operators
    allowed: `eq` (`col=`), `in` (`col__in=`), `gt`, `gte`, `lt`, `lte` (`col__gte=`…, numbers
    and dates only) and `null` (`col__null=true|false`). Lists, structures and binary columns
    cannot be filtered on;
  - the **default order**, on a filterable column, ascending or descending;
  - the **largest page**, 1,000 rows by default, at most 10,000.
- **Runtime**: shown, not chosen: a new data API starts with one pod of 250m CPU and 256Mi of
  memory, and an administrator changes it ([The runtime](#the-runtime)).

Every save that changes the resources is a **version**; an older one can be restored, and it
becomes the newest (its columns must still be in the datasources; the runtime stays as the
administrator set it).

## The runtime

How a data API's pods run: a number of **replicas**, or a minimum and a maximum with
**autoscaling on CPU** (at 70% of the request), and the **CPU** and **memory** of each pod. It
is decided by an **administrator only**, for each data API (decided on 2026-11-07): whoever
makes the data API sees it on the page, read-only, with a line saying so. A new data API and a
copy start with **one pod of 250m and 256Mi**, enough for a few million rows; a bigger table,
more traffic or a CRM that cannot wait need an administrator to give it more.

An administrator sets it from **Administration → Data APIs** (the **Runtime** button of each
row opens a dialog) or from the data API's own page (**Change** on its Runtime card). The
bounds:

| | Least | Most |
|---|---|---|
| CPU of a pod | `100m` | `16` |
| Memory of a pod | `256Mi` | `64Gi` |
| Pods of one data API (replicas, or the autoscaling's maximum) | 1 | 40, and never more than `DATA_API__MAX_TOTAL_REPLICAS` |

CPU and memory are Kubernetes quantities (`500m`, `2`, `512Mi`, `4Gi`). For an active data
API, the pods of all the active ones together stay within `DATA_API__MAX_TOTAL_REPLICAS`: a
change past it is refused, saying how many the others take. A change is not a version, and the
audit records it as `data_api.runtime` with the runtime before and after and
`"by": "administration"`.

An active data API gets its new runtime within half a minute (the orchestrator's next round):
its pods are replaced one at a time, a new one ready, its data prepared, before an old one
stops, so the calls go on answering. DuckDB takes 60% of the new memory by itself. A pod
bigger than any node of the cluster waits to be scheduled while the old ones keep answering,
and the **Pods and traffic** tab says why. Through the API, `PUT /admin/data-apis/{id}/runtime`
with `{"replicas": 2, "cpu": "1", "memory": "2Gi"}` (or `"autoscale": true` with
`"min_replicas"` and `"max_replicas"`); a create or an edit of the data API that sends another
runtime is refused with `403` and `"code": "runtime_admin_only"`.

## States

A new data API is a **draft**: it has nothing in the cluster. **Activate** needs at least one
resource, the creator's authority above, and room within the most pods the active data APIs
may run together; within half a minute its pods start, download the data and answer.
**Pause** keeps the definition, the keys and the objects in the cluster, with no pod.
Deleting it deletes its versions and its keys, and its objects leave the cluster within half a
minute.

The page's **Pods and traffic** tab shows what the cluster says, asked again every few seconds:
how many pods are ready out of those wanted, each pod (ready, preparing the data, starting, in
error, stopping; the node it runs on, its restarts and, in the cluster's words, why it does not
run: an image it cannot pull, a pod that keeps failing, a disk full), the data version each
resource serves in each pod and whether the latest data of its datasource has reached them,
the disk each pod has, and the requests, rows, errors and over-the-rate answers of the last
hour, day or week. What the cluster refused (a quota, a Pod Security rule) is said there too.

## The Documentation tab

The page's **Documentation** tab is what to give whoever integrates the data API: it needs
VIEW on the folder only, no key and no pod, and documents a draft or a paused data API as
well (it says that its addresses do not answer yet). The gateway writes it from the current
definition with the same code the pods use for their `openapi.json`: the description in the
page and the one an active pod serves are the same document, the columns typed by their
datasources as the pods type them once they read the data.

- **The address** the other systems call (the public one when there is
  [a host of their own](#a-host-of-their-own), otherwise the page's own), how to send the key,
  and a first `curl` to try, filled in with that address.
- **Each resource**: its path, every column that goes out with its type (whole number, number,
  decimal, true/false, text, date, date and time, time, UUID, JSON for lists and structures; a
  column no longer in its datasource says so), the filters each column allows as the
  parameters to write (`id=`, `id__in=`, `signup__gte=`…), the default order and the largest
  page; `curl` lines for the first page, a filter, a count and a CSV file, each with a copy
  button, and an example of the answer.
- **The parameters** (`_columns`, `_sort`, `_limit`, `_cursor`, `_count`, `_format`, the
  filters), **the answers** (JSON, the cursor and the version, CSV and its headers, how values
  are written) and **every error** with its status and code, as below.
- **OpenAPI (JSON)** downloads the description, with the address filled in, to import into the
  CRM, Postman or a client generator.

The `curl` lines read the key from a variable, `TABULARIA_KEY`: the page never writes a key.

## Keys

The other systems call with a key, sent as `Authorization: Bearer tdk_…` (or `X-API-Key`),
never in the address. A key:

- belongs to one data API and names who uses it (`CRM production`);
- is shown **once**, when it is made: `tdk_` and 40 random letters and digits. Tabularia
  keeps only its SHA-256 fingerprint and its first 8 characters after `tdk_`, which the list
  shows to tell the keys apart;
- has an **expiry**, required, at most `DATA_API__KEY_MAX_DAYS` away (365 days by default);
- has a **rate limit** per minute (`DATA_API__KEY_DEFAULT_RATE`, 600 by default, when none
  is given);
- may be limited to some **resources** (none chosen: all) and to some **source addresses**
  (addresses or ranges such as `203.0.113.0/24`; none: any);
- is **revoked** at once, or **renewed**: a new key with the same name, limits, resources and
  addresses, while the old one keeps working for the grace period chosen (none: it stops now,
  at most 30 days).

Keys are made, renewed and revoked from the interface, not with an API token. A key can be
revoked even when the data APIs are switched off. The audit records every change of a data
API and every key event (`data_api.create`, `update`, `activate`, `pause`, `delete`,
`key_create`, `key_revoke`, `key_renew`), never a key.

## How a data API answers

Every call carries a key, in a header, and asks one resource:

```bash
KEY=tdk_…   # shown once, when the key was made
BASE=https://tabularia.example.com/data/crm-customers

# one customer by its id: the lookup a CRM does, in a millisecond or two
curl -H "Authorization: Bearer $KEY" "$BASE/customers?customer_id=4242"

# filters (ANDed), some columns, an order and a page
curl -H "Authorization: Bearer $KEY" \
  "$BASE/customers?region__in=North,South&signup__gte=2026-01-01&_columns=customer_id,name&_sort=-score&_limit=100"

# the next page: give `next` as the cursor, alone
curl -H "Authorization: Bearer $KEY" "$BASE/customers?_cursor=eyJnIjoi…"

# how many rows match, besides the page; the same as CSV
curl -H "X-API-Key: $KEY" "$BASE/customers?region=North&_count=true&_limit=1"
curl -H "X-API-Key: $KEY" "$BASE/customers?region=North&_format=csv" -o north.csv

# the description, OpenAPI 3.1, to import into the CRM or Postman
curl -H "Authorization: Bearer $KEY" "$BASE/openapi.json"
```

The answer:

```json
{"data": [{"customer_id": 4242, "name": "Rossi", "region": "North", "score": 87.5, "signup": "2026-02-03"}],
 "next": null, "version": "2026-11-07T08:12:03Z"}
```

- **Filters** only on the columns marked filterable, with the operators allowed on them:
  `col=`, `col__in=` (a comma-separated list, at most 500 values), `col__gt=`, `__gte`,
  `__lt`, `__lte` (numbers, dates, times), `col__null=true|false`. A value is read by the
  column's type: whole numbers, numbers (`12.50`), `true`/`false`, dates (`2026-01-31`),
  dates and times (`2026-01-31T08:30:00`, with a zone or without one, which is UTC; a date
  alone is its midnight), times (`08:30:00`). A value of the wrong type is a `400` that names
  the column; so is a filter on another column, or the same parameter twice. In a query
  string `+` is a space: write `%2B`.
- **`_columns`**: some of the columns that go out, in that order; **`_sort`**: filterable
  columns, `-` before one for descending, nulls last either way (by default the resource's
  default order, or the order of its first filterable column); **`_limit`**: the rows of a
  page, by default and at most the resource's largest page; **`_count=true`**: adds `count`,
  the rows the filters match; **`_format=csv`**: the page as CSV, with the cursor, the
  version and the count in the `X-Next-Cursor`, `X-Data-Version` and `X-Total-Count` headers.
- **Pages**: `next` is the cursor of the next page, `null` on the last. A cursor carries its
  query, so `_cursor` comes alone (or with `_format`). It holds for the data it was given on:
  after a refresh of the datasource (or a change of the resource's columns) it answers `409`
  (`data_changed`), so a page never mixes two versions; start again from the first page.
- **Values**: dates and times in ISO 8601 (a date and time without a zone is written
  without one; one with a zone in UTC, `Z`); a decimal as a number when a float holds it
  exactly, as a string when it does not; a float that is not a number as `"NaN"` or
  `"Infinity"`; lists and structures as JSON.
- **`version`**: when the datasource's data was written (the run that wrote it, or its
  refresh). A refresh reaches the pods within the poll: they download the new data, prepare
  it, and switch to it at once; until then the previous version answers.
- **Errors**: `{"detail": "a sentence", "code": "…"}`. `400` a wrong parameter (the detail
  says which; `key_in_query` for a key sent in the address, which should then be revoked);
  `401` no key, or not a valid one (`key_missing`, `key_invalid`, `key_expired`,
  `key_revoked`, `key_replaced`: renewed and past its grace); `403` a key not allowed on the
  resource (`key_not_allowed`) or from this address (`address_not_allowed`), or a resource
  whose datasource the creator may no longer read (`resource_unavailable`); `404` no such
  data API or resource; `409` a cursor of older data; `429` past the key's rate, with
  `Retry-After`; `503` the data still loading (`loading`, with `Retry-After`), the data API
  paused or suspended, or the pod out of touch with Tabularia for more than 15 minutes
  (`manifest_stale`: it then refuses every key rather than miss a revocation).
- **Rate limit**: each pod counts its own requests, at the key's rate divided by the data
  API's replicas (while autoscaling, the pods running now, within a poll): approximate when
  the traffic is uneven between them.
- **`GET /data/{api}/health`** answers without a key: `ready`, `loading`, `paused`…

### The pods

The image `tabularia-data-api` runs one Go program with DuckDB inside. A pod never holds
storage credentials nor reaches a source database: every `DATA_API__POLL_SECONDS` it asks the
gateway for its **manifest** with a token of its own data API (the definition, the keys as
their fingerprints, and for each resource the data's version and a link to its parquet valid
for five minutes), and sends back the requests, rows, errors and `429`s of each key per
minute. It downloads each resource's parquet once per version into its data directory and
turns it into a DuckDB file with only the columns that go out, sorted by the first filterable
column, opened read-only and with no access to any other file: a lookup by that column reads
one block of the file, and the memory stays flat however big the table. Measured on 20
million rows in a pod of 1 GB and 2 CPUs: see the spec.

**Sizing.** Serving takes little memory; preparing a version sorts the table, and DuckDB
wants about 200 MB per thread for it (the pod uses as many threads as its memory allows, at
least one). A pod of 1Gi prepares tens of millions of rows; a small one (256Mi) a few
million. A pod answers as many queries at once as it has CPUs and lines up the
others: more at once made DuckDB slower in all (with 1 CPU, 1,150 lookups a second one at a
time, 280 with eight at once; lined up, 1,300). For more requests, more replicas or more CPU,
which an administrator gives ([The runtime](#the-runtime)).
A preparation that fails is logged and tried again, and the previous version keeps
answering meanwhile (the page shows why). The disk of the data directory holds the new parquet
and the new file next to the one being served: see [On the cluster](#on-the-cluster).

Each request is logged on the pod's standard output as one JSON line (the key's first
characters, the resource, the status, the rows, the milliseconds, the client's address),
never the key nor the query. The pod reads its environment, which Tabularia sets:

| Variable | Default | What |
|---|---|---|
| `DATA_API__ID`, `DATA_API__GATEWAY_URL`, `DATA_API__POD_TOKEN` | required | the data API, the gateway's address in the cluster, the pod token |
| `DATA_API__DATA_DIR` | `/data` | where the data files go (the only place it writes) |
| `DATA_API__PORT` | `8000` | the port; `/healthz` and `/readyz` for Kubernetes |
| `DATA_API__MEMORY_LIMIT_MB` | 60% of the container's memory | DuckDB's memory: a third serves the resources, two thirds prepare the next version |
| `DATA_API__THREADS` | the container's CPUs | DuckDB's threads preparing a version; serving, one per query, and as many queries at once (the others wait their turn, within `DATA_API__QUERY_SECONDS`) |
| `DATA_API__FORWARDED_ALLOW_IPS` | none | the proxies (the ingress controller) whose `X-Forwarded-For` gives the client's address for the keys' address limits |
| `DATA_API__QUERY_SECONDS` | `30` | the longest a request may take |
| `DATA_API__MANIFEST_MAX_AGE_SECONDS` | `900` | past this without a manifest the pod stops answering |

## On the cluster

The orchestrator keeps the data APIs in line with the database every 30 seconds, in the
namespace of the scripts (the chart's `dataApi.namespace` for another one). For each active or
paused data API, every object named `<release>-data-api-<id>` and labelled with its id:

- a **Deployment** of its pods (`tabularia-data-api`): the replicas of its runtime (none while
  paused; the autoscaler's count while autoscaling), its CPU and memory as requests and limits,
  not root (10001), the root filesystem read-only, no capability, no service account token,
  `/readyz` (ready once every resource is prepared, with a fresh manifest) and `/healthz` as
  probes; a rollout starts a new pod and waits for it to be ready before an old one stops, and
  the replicas spread over the nodes when they can;
- a **Secret** with the pod token, a new one at every activation (the previous one stops at
  once), or when the Secret is gone: the pods restart with it;
- a **Service** inside the cluster, which only the ingress controller reaches;
- an **Ingress** with `/data/{address}/` only, on the app's host (the first of the chart's
  `ingress.hosts`, or `dataApi.ingress.host`), or on the data APIs' own
  ([A host of their own](#a-host-of-their-own)), with the app's class: the pods' `/healthz` and
  `/readyz` are not published. On the app's host with TLS, ingress-nginx serves it with that
  host's certificate. `dataApi.ingress.annotations` adds the controller's settings, such as a
  rate limit per client;
- a **NetworkPolicy**: in, only the ingress controller (`dataApi.networkPolicy.ingressController`,
  ingress-nginx's namespace and pods by default), so no pod nor namespace of the cluster calls
  the pods directly; out, DNS, the gateway's pods (the manifest and the usage) and the object
  storage on its port (its namespace when it is a Service of the cluster, read from
  `externalServices.storage.endpoint`; otherwise `dataApi.networkPolicy.storageCIDRs`, or any
  public address), nothing else. The scripts' own policy leaves the data APIs' pods out;
- a **PodDisruptionBudget** while it has at least two pods (one at a time stops in a drain),
  and a **HorizontalPodAutoscaler** while it autoscales (on CPU, at 70% of the request).

Objects labelled for a data API the database no longer has (deleted, or gone with its
creator's account) are removed; switched off (`dataApi.enabled: false`), every one is. Only the
orchestrator has rights in that namespace (`templates/data-api.yaml`): Deployments, Services,
Secrets, Ingresses, NetworkPolicies, PodDisruptionBudgets and autoscalers, and reading the pods.
The namespace needs the image pull secrets of `image.pullSecrets`.

**The client's address.** The keys' address limits read the client's address from the
`X-Forwarded-For` the ingress controller sets, believed only from the proxies of
`dataApi.trustedProxies` (by default `gateway.trustedProxies`, the private ranges): ingress-nginx
replaces whatever the client sent with the address it sees, so a client cannot pass another one
off. Behind a cloud load balancer the controller must see the client's address at all
(`externalTrafficPolicy: Local`, or the PROXY protocol).

**The disk.** Each pod downloads each resource's parquet into its data directory, an
`emptyDir`, and prepares a DuckDB file from it; while a new version is prepared the pod holds
the file it serves, the new parquet and the new file, and what DuckDB spills while sorting. It
gets six times the size of its parquets plus 256Mi, rounded up to a power of two (so a refresh
that grows the data seldom changes it: a change restarts the pods, one at a time), at least
1Gi and at most `dataApi.maxDisk` (20Gi), as the `emptyDir`'s limit and the pod's ephemeral
storage. A pod past it is evicted, and the page says so.

### A host of their own

By default the data APIs answer on the interface's host, next to everything else of Tabularia.
To give the other systems an address with nothing else on it (a host the firewall opens to the
internet while the interface stays on the internal network, say), set the chart's
`dataApi.publicHost`:

```yaml
dataApi:
  enabled: true
  publicHost: data.example.com   # a host of its own: not one of ingress.hosts, nor share.publicHost
  # publicBaseUrl: ""            # the address the pages write; empty: https://<publicHost>
  # tlsSecretName: ""            # the certificate; empty: <release>-data-api-tls
```

Then each active data API's Ingress has `/data/{address}/` on that host only: every other path
of it is the ingress controller's 404 (the interface, the app's API, `/healthz` of the pods),
and the interface's host no longer sends `/data/…` to the pods. The data APIs' pages, their
documentation and the downloaded OpenAPI write the public address (`DATA_API__PUBLIC_BASE_URL`):
`dataApi.publicBaseUrl` when set (a port, another scheme), otherwise `https://<publicHost>`
when there is a certificate and `http://` without one. The chart refuses a public host that
is the interface's or the share links' own.

**The certificate.** With TLS on the interface (`ingress.tls`), or with `dataApi.tlsSecretName`,
the public host gets a certificate: a Secret of the data APIs' namespace (where their Ingresses
are), `<release>-data-api-tls` unless named. The chart adds there an Ingress with no path,
`<release>-data-api-host`, that asks for it with the main Ingress's annotations, so
cert-manager makes it once for all the data APIs; without cert-manager, create the Secret
(`kubectl -n <namespace> create secret tls …`) under that name. Each data API's Ingress names
it.

**Checking it.** With the host pointed at the ingress controller:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://data.example.com/data/crm-customers/health  # 200: the pod answers
curl -s -o /dev/null -w '%{http_code}\n' https://data.example.com/                          # 404: nothing else here
curl -s -o /dev/null -w '%{http_code}\n' https://data.example.com/api/auth/me               # 404
```

## In Administration

Administration → **Data APIs** lists every data API of the installation, whoever made it: its
address and folder, its creator (said when the account is deactivated: the data API then
stops answering), its state, its pods ready out of those wanted (and what the cluster refused),
its requests and its errors and over-the-rate answers of the last 24 hours, with the last call,
and the keys that answer, and its **runtime** (`2 × 500m · 1Gi`: pods, CPU and memory of each).
Above the list, how many data APIs and pods there are against the caps below, the bounds of a
runtime, and the address they are called at.

An administrator sets the **runtime** of any data API there ([The runtime](#the-runtime)). An
administrator may **pause** any active data API or **delete** any data API from there, with
its versions and its keys, whoever made it; the delete works even with the feature switched
off, to clean what is left. The audit records both as the page's own actions
(`data_api.pause`, `data_api.delete`) with `"by": "administration"`. An observer reads the list
and acts on nothing, the runtime included.

## In the lineage

A data API reads the datasources it publishes: the lineage graph draws it downstream of them
(an edge *reads* from each datasource to the data API), it can be the centre of the graph, and
Explore's deletion dialog of a datasource counts the data APIs among what uses it. The search
and Explore find data APIs like the other objects, by name.

## What it keeps from being deleted

A datasource a data API publishes cannot be deleted: the refusal names the data APIs, and
the datasource can go once it is taken out of them. A folder with data APIs cannot be
deleted, as with the other objects: move or delete them first.

## The administrator's caps

Set in the environment (the chart's `dataApi` values), read by the gateway, which checks
them at every activation, runtime and key ([Configuration](configuration.md)); Administration →
Data APIs shows them. The runtime of each data API is not among them: an administrator sets it
([The runtime](#the-runtime)).

| Variable | Default | What it caps |
|---|---|---|
| `DATA_API__ENABLED` | `false` | the feature |
| `DATA_API__MAX_APIS` | `20` | the data APIs of the installation |
| `DATA_API__MAX_TOTAL_REPLICAS` | `10` | the pods of all the active ones together (and of any one) |
| `DATA_API__KEY_MAX_DAYS` | `365` | a key's life |
| `DATA_API__KEY_DEFAULT_RATE` | `600` | a key's requests per minute, when none is given |
| `DATA_API__POLL_SECONDS` | `20` | how often the pods ask for their manifest: a revoked key or a refresh reaches them within it |
| `DATA_API__USAGE_RETENTION_DAYS` | `30` | how long the requests per key and minute are kept for the pages |
| `DATA_API__MAX_DISK` | `20Gi` | the disk of one pod for its data files |

How the orchestrator runs the pods (their image, namespace, Ingress and NetworkPolicy) is set
by the chart from the `dataApi` values and `image.dataApi`: see
[Configuration](configuration.md).
