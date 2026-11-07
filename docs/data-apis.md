# Data APIs

A data API publishes one or more datasources as a read-only HTTP API, for the systems that
need Tabularia's tables one record at a time: a CRM asking for the score of customer 123,
a website asking for the open orders of an account. It lives in a folder of Explore, like
a SQL script, and is answered by pods of its own on Kubernetes, so the other systems'
traffic never slows the people using Tabularia. The design is in
[the spec](specs/data-apis.md).

> **What this version does.** It keeps a data API's definition, its versions, its state and
> its keys, with every check below. The pods that answer the calls, their address on the
> ingress and the usage counts come with a later version: until then an active data API
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
  the page says so, it cannot be activated, and the pods (later) stop answering. Duplicate
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
