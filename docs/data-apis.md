# Data APIs

A data API publishes one or more datasources as a read-only HTTP API, for the systems that
need Tabularia's tables one record at a time: a CRM asking for the score of customer 123,
a website asking for the open orders of an account. It lives in a folder of Explore, like
a SQL script, and is answered by pods of its own on Kubernetes, so the other systems'
traffic never slows the people using Tabularia. The design is in
[the spec](specs/data-apis.md).

> **What this version does.** It keeps a data API's definition, its versions, its state and
> its keys, with every check below, and it has the program and the image of the pods that
> answer the calls (`tabularia-data-api`, below: [How a data API answers](#how-a-data-api-answers)).
> Tabularia does not start those pods yet, nor give them an address on the ingress: that,
> and the usage on the page, come with a later version. Until then an active data API
> answers nothing, and its page says so.

## Where it is offered

On **Kubernetes only**, with the scripting environments on (`SCRIPTS__ENABLED`, the same
criterion as the saved scripts and the notebooks) and with `DATA_API__ENABLED`, which is
off by default; the Helm chart sets both, the second from `dataApi.enabled`. With Docker
Compose there are none: Explore offers no «New → Data API», the lists are empty and every
other route answers 409.

## Who may do what

- **MANAGE** on the folder, or an administrator, makes a data API, edits it, activates and
  pauses it, deletes it and manages its keys. An observer never does.
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
- **Runtime**: a number of replicas, or a minimum and a maximum with autoscaling on CPU, and
  the CPU and memory of each pod, within the administrator's caps.

Every save that changes the resources or the runtime is a **version**; an older one can be
restored, and it becomes the newest (its columns must still be in the datasources).

## States

A new data API is a **draft**. **Activate** needs at least one resource, the creator's
authority above, and room within the most pods the active data APIs may run together;
**Pause** keeps the definition and the keys. Deleting it deletes its versions and its keys.

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
  API's replicas: approximate when the traffic is uneven between them.
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
million. A preparation that fails is logged and tried again, and the previous version keeps
answering meanwhile. The disk of the data directory holds the new parquet and the new file
next to the one being served.

Each request is logged on the pod's standard output as one JSON line (the key's first
characters, the resource, the status, the rows, the milliseconds, the client's address),
never the key nor the query. The pod reads its environment, which Tabularia sets:

| Variable | Default | What |
|---|---|---|
| `DATA_API__ID`, `DATA_API__GATEWAY_URL`, `DATA_API__POD_TOKEN` | required | the data API, the gateway's address in the cluster, the pod token |
| `DATA_API__DATA_DIR` | `/data` | where the data files go (the only place it writes) |
| `DATA_API__PORT` | `8000` | the port; `/healthz` and `/readyz` for Kubernetes |
| `DATA_API__MEMORY_LIMIT_MB` | 60% of the container's memory | DuckDB's memory: a third serves the resources, two thirds prepare the next version |
| `DATA_API__THREADS` | the container's CPUs | DuckDB's threads preparing a version (serving, one per query, the requests side by side) |
| `DATA_API__FORWARDED_ALLOW_IPS` | none | the proxies (the ingress controller) whose `X-Forwarded-For` gives the client's address for the keys' address limits |
| `DATA_API__QUERY_SECONDS` | `30` | the longest a request may take |
| `DATA_API__MANIFEST_MAX_AGE_SECONDS` | `900` | past this without a manifest the pod stops answering |

## What it keeps from being deleted

A datasource a data API publishes cannot be deleted: the refusal names the data APIs, and
the datasource can go once it is taken out of them. A folder with data APIs cannot be
deleted, as with the other objects: move or delete them first.

## The administrator's caps

Set in the environment (the chart's `dataApi` values), read by the gateway, which checks
them at every save, activation and key ([Configuration](configuration.md)):

| Variable | Default | What it caps |
|---|---|---|
| `DATA_API__ENABLED` | `false` | the feature |
| `DATA_API__MAX_APIS` | `20` | the data APIs of the installation |
| `DATA_API__MAX_REPLICAS` | `3` | the pods of one data API |
| `DATA_API__MAX_TOTAL_REPLICAS` | `10` | the pods of all the active ones together |
| `DATA_API__MAX_CPU` | `1` | the CPU of one pod |
| `DATA_API__MAX_MEMORY` | `1Gi` | the memory of one pod |
| `DATA_API__KEY_MAX_DAYS` | `365` | a key's life |
| `DATA_API__KEY_DEFAULT_RATE` | `600` | a key's requests per minute, when none is given |
| `DATA_API__POLL_SECONDS` | `20` | how often the pods ask for their manifest: a revoked key or a refresh reaches them within it |
