# Tabularia Helm chart

This chart deploys the Tabularia application: the frontend, the gateway (control
plane), the engine API and the Celery workers. It explains its own choices —
where a setting is unusual, the reason is next to it, here or in `values.yaml`.

For the reasoning behind the images and the runtime behaviour, see
[`docs/deploy/kubernetes.md`](../../../docs/deploy/kubernetes.md).

## What this chart does not deploy

PostgreSQL, Redis/Valkey and the S3-compatible object storage are **not** part of
the chart, on purpose. They carry the state that must survive the application:

| Service | Holds | If you lose it |
|---|---|---|
| PostgreSQL | users, permissions, flows, run history | the installation is gone; back it up |
| Object storage | every dataset, cache blob and output | the data is gone; back it up |
| Redis / Valkey | queues and cache index | in-flight jobs and a warm cache; survivable |

Bundling them as subcharts would produce a deployment that looks complete and
quietly loses data on the first node failure. Point the chart at managed services
through `externalServices`.

**Neither is the monitoring stack.** The compose file ships VictoriaMetrics,
Grafana, cAdvisor, node-exporter and a Celery exporter for local work; in a
cluster you almost certainly already run your own. Two things in the product
depend on them, and both degrade quietly rather than breaking:

| Feature | Without monitoring | To restore it |
|---|---|---|
| RAM badge in the UI | disappears (`/system/memory` answers 503) | point `monitoring.nodeExporterUrl` at your node-exporter |
| Monitoring tab (superusers) | iframe stays blank | set `frontend.publicGrafanaUrl`, and provision the dashboards yourself |

## Building the images

The release images are public on Docker Hub and the chart pulls them by default:
`jumpasaservice/tabularia-backend`, `jumpasaservice/tabularia-gateway` and
`jumpasaservice/tabularia-frontend`, tagged with the release (`1.1.0`). The backend
one is built without chDB.

To run your own builds instead, build from a checkout of the release tag, push them
to your registry, and set `image.registry` and the tags:

```bash
# Engine, workers and beat all run this one image.
docker build -t <registry>/tabularia-backend:1.1.0 backend/
#   Add --build-arg WITH_CHDB=true only if your flows use the embedded chDB
#   engine: it takes the image from about 800 MB to about 1.4 GB.

docker build -t <registry>/tabularia-gateway:1.1.0 gateway/

# NOTE the -f: frontend/Dockerfile is the hot-reload DEVELOPMENT server.
docker build -f frontend/Dockerfile.prod -t <registry>/tabularia-frontend:1.1.0 frontend/
```

All three run as uid 10001 with a read-only root filesystem.

## Quick start

```bash
helm install tabularia infrastructure/helm/tabularia \
  --namespace tabularia --create-namespace \
  --values my-values.yaml
```

A minimal `my-values.yaml`:

```yaml
image:
  registry: registry.example.com

externalServices:
  postgres: { host: db.internal, database: tabularia, user: tabularia }
  redis:    { host: valkey.internal }
  storage:  { endpoint: https://s3.example.com, bucket: tabularia, region: eu-west-1 }

secrets:
  # fernetKey: openssl rand -base64 32 | tr '+/' '-_'
  #        or: python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
  fernetKey: "<one of the two commands above>"
  jwtSecret: "<openssl rand -hex 32>"
  dbPassword: "…"
  storageAccessKey: "…"
  storageSecretKey: "…"
  adminPassword: "…"

ingress:
  hosts:
    - host: tabularia.example.com
      paths: [{ path: /, pathType: Prefix, service: frontend }]
```

In production set `existingSecret` instead of `secrets` and manage the Secret
with your usual tooling.

Optional keys follow the feature they belong to. With
`externalServices.clickhouse.aiUsername` set, the Secret must also hold
`CLICKHOUSE_EXTERNAL__AI_PASSWORD`: the read-only ClickHouse user the AI
assistant's queries run as (grants in `docs/engines/clickhouse-ai-user.md`).

Every other setting in `infrastructure/.env.example` goes through `extraEnv`, by
its variable name (`DB__POOL_SIZE`, `INGEST__PARQUET_ROW_GROUP_ROWS`…): it lands in
the shared ConfigMap. A name the chart already sets from a value of its own is
refused when the chart renders, so there is one place for each setting. Numbers
are written out in full: Helm reads every number in a values file as a float, and
`quote` would hand a million to the pods as `1e+06`, which the application refuses
as an integer.

## Scaling the gateway

The gateway has two jobs, and the chart runs them as two Deployments built from
the same image. What tells them apart is one variable, `APP__ROLE`.

| Deployment | Role | What it does | Scale with |
|---|---|---|---|
| `…-gateway` | `api` | answers requests; the Ingress points here | `gateway.replicas`, or `gateway.autoscaling` |
| `…-orchestrator` | `orchestrator` | fires the schedules, executes the flows, reconciles runs, cleans up; no Service, nothing calls it | `gateway.orchestrator.replicas` |

**Requests.** One Python process uses one core and tops out around 95 requests a
second — a few hundred people clicking at once. Past that, add replicas. Nothing
is tied to a pod: sessions, the sign-in throttle and the editor's "also open by"
notice live in PostgreSQL. Measured with 500 simulated
users on one eight-core machine: one replica saturated (95th percentile of page
requests 5.8 s), three answered all of them in 0.12 s at 44% CPU each.

**Background work.** Orchestrators share their work through the database. A
schedule slot is taken by whoever moves the next run time forward first, a queued
flow execution by whoever signs it first, and a unique index keeps a flow from
ever having two executions running — so more than one orchestrator never means
anything firing twice. Each says "still here" on the executions it carries every
ten seconds; if a pod dies, another orchestrator closes them as interrupted after
`orchestrator.deadAfterSeconds`. They are **not resumed**: the steps already done
have written their output, and the flow has to be launched again. One
orchestrator is enough for most installations; the second is there for failover.

**Database connections.** Every gateway and orchestrator pod keeps its own pool,
up to `threads + threads/4 + 10` connections (25 with the default twelve
threads). Keep `(gateway replicas + orchestrator replicas) × 25` below your
PostgreSQL's `max_connections`.

A single process doing both jobs still exists (`APP__ROLE=all`, the application's
default, and what the development compose file runs); the chart does not use it.

## Why beat is a singleton

Beat is the clock: it enqueues periodic jobs (cache eviction, storage statistics)
and the workers execute them. Two beats enqueue everything twice. It does no work
itself, so one small pod is enough.

## Scaling

| Component | Scales | Notes |
|---|---|---|
| Frontend | freely | stateless |
| Engine API | freely | receives uploads, builds exports |
| Run worker | freely | the heavy one; size `/tmp` and memory together |
| Preview worker | freely | keeps interactive previews off the run queue |
| Gateway | freely | one core per replica; see above |
| Orchestrator | freely | one is enough, a second is for failover; see above |
| Beat | **no** | see above |

### Autoscaling

The HPAs in this chart scale on CPU because that is what works everywhere, but
CPU is a poor signal here: a worker waiting on a slow database uses almost no CPU
while being entirely busy. The signal that matches the work is **queue length** —
the Redis lists `celery` and `preview`. Use KEDA if you can; `templates/hpa.yaml`
carries a ready trigger. The gateway is the exception: it is busy exactly when it
burns CPU, so `gateway.autoscaling` on CPU is the right signal for it.

Scaling in can also interrupt a run, which is why workers get a long termination
grace period. Prefer letting the queue drain over aggressive scale-in.

## Storage and `/tmp`

The images have a read-only root filesystem, so every scratch write goes to the
`/tmp` emptyDir mounted per component. Undersize it and runs fail with "no space
left on device" rather than anything clearer.

| Role | What lands there | Size for |
|---|---|---|
| Engine API | uploads and their parquet conversion, CSV/Excel exports | the largest file a user uploads |
| Run worker | source parquet, refresh results, data written to outputs | the largest dataset a run touches |
| Preview worker | source parquet | the largest source |
| Beat | its schedule file | a few MB |
| Gateway, frontend | little | the defaults are fine |

Using an external ClickHouse as the production engine moves most of this work off
the workers: with the `s3` transport the server reads and writes parquet directly
on the object storage.

## Secrets

`SECURITY__FERNET_KEY` encrypts the database credentials users save in Tabularia.
Two rules:

1. **The same value must reach every component.** The gateway encrypts, the
   workers decrypt.
2. **It must not change after the first connection is saved.** Rotating it makes
   existing credentials undecryptable. There is no re-encryption path.

Every process refuses to start without it, in every environment — that is
deliberate, so a missing key fails at deploy time rather than at the first run.

`JWT__SECRET` signs sessions. Changing it logs everyone out. One leaked session
does not need it: an administrator closes it from the Audit page, and a password
change closes all of that user's sessions.

One Secret is mounted by every pod, so the engine pods also receive gateway-only
values. That keeps the chart readable; split it per component if your policy
requires least privilege.

## Ingress

One hostname serves the UI and the API, so the browser calls the gateway at the
same origin. Three settings matter, and most controllers default them wrong for
this application:

| Setting | Why | Default in the chart |
|---|---|---|
| Body size | uploads stream through the gateway | unlimited (`"0"`) |
| Request buffering | the proxy must not spool uploads to its own disk | off |
| Read timeout | previews wait for the engine synchronously | 300 s |

**One host, the API under `/api`.** The frontend gets `/`, the gateway
`ingress.apiPrefix` (`/api`), not stripped: the gateway knows its prefix
(`APP__ROOT_PATH`) and answers under it, so no controller-specific rewrite is
needed and the browser calls the API relatively. Routing the gateway's own paths
instead cannot work: `/flows`, `/runs`, `/auth/callback` are also pages of the
frontend. The identity provider's redirect URI is
`https://<host>/api/auth/sso/callback`.

**The client's real address.** The gateway reads it from `X-Forwarded-For`,
from the right, past the proxies in `gateway.trustedProxies` (private ranges by
default; narrow it to your controller's pod CIDR). The sign-in throttle is keyed
by (address, email) and shared by all replicas, so a stranger's wrong passwords
do not lock out the administrator. It defeats a password list, not a
distributed attack: to cap `/api/auth/login` at the edge as well, give that path
an Ingress of its own with the controller's rate-limit annotations — not the
whole site, which counts pages and assets and hurts an office behind one NAT.

**The engine API** has no Ingress and must never get one. The NetworkPolicy (on
by default; it needs a CNI that enforces it) admits only the gateway, the
orchestrator and the workers, and every request must carry the gateway's
internal token, derived from the Fernet key. Each role also loses the secrets
it has no use for: the engine roles never see the session key or the metadata
password, the gateway never sees the storage or warehouse keys.

## Upgrades

**Coming from chart 1.x to 2.0** — values that changed meaning:

- `ingress.apiPaths` is gone: the gateway is published under `ingress.apiPrefix`
  (`/api`). Register the new SSO redirect URI on the identity provider,
  `https://<host>/api/auth/sso/callback`, before upgrading.
- `frontend.publicApiBase` is optional (the default is `/api` on the same host);
  remove it unless the API lives on another host of the same domain, and then set
  `auth.cookieDomain` to that domain: the session is a cookie the UI's server must
  see.
- `networkPolicy.enabled` is now `true`; `gateway.replicas` is 2;
  `gateway.trustedProxies` is new; no pod mounts the service account token
  (`serviceAccount.automountToken`).
- **Upgrade in two steps this time.** The engine now refuses requests without the
  gateway's internal token, which the 1.x gateway does not send. Upgrade the
  gateway and the orchestrator first — the old engine ignores the token — then
  the rest:

  ```bash
  helm upgrade … --set image.backend.tag=<the version you run now>   # gateway and orchestrator
  helm upgrade …                                                     # engine and workers
  ```

The database schema is migrated by an init container (`migrate`) in every gateway
and orchestrator pod, before the application starts: `python -m app.db.migra`
(Alembic) creates a fresh database, brings an existing one to the version of the
image, and adopts a database from before Alembic. The first pod of a new version
does the work; the others queue on a Postgres advisory lock and find it done. The
application only checks that the database is not behind (`DB__AUTO_MIGRATE` stays
false) and refuses to start if it is. Migrations are written to work with the
previous version still running (expand/contract), so pods of both Deployments
roll: a new one is ready before an old one stops, and requests are not dropped.
`migrations.enabled: false` drops the init container, for a pipeline that runs
the same command itself before upgrading. See
[docs/database-migrations.md](../../../docs/database-migrations.md).

**Coming from chart 2.0 with app 1.1** (sessions): the new interface signs in with
a cookie the old gateway does not read. Upgrade the gateway first, keeping the
interface you run now, then the rest; everyone signs in again once:

```bash
helm upgrade … --set image.frontend.tag=<the version you run now>   # gateway, orchestrator, migration
helm upgrade …                                                       # the interface
```

Flow executions in progress **are** interrupted when the orchestrator pod
carrying them stops: they are closed as failed with a message that says so, and
are not resumed. Upgrade when no long flow is running, or relaunch afterwards.

**Coming from chart 1.0.x** (one gateway pod that also ran the scheduler): that
version does not know about the queue, and must not run next to the new one.
Scale it to zero first, then upgrade:

```bash
kubectl -n <namespace> scale deployment/<release>-tabularia-gateway --replicas=0
helm upgrade …
```

Rolling back to it works the same way in reverse (scale both Deployments to zero
first); it closes whatever executions it finds queued or running.

Workers finish the task in flight before stopping, up to
`worker.terminationGracePeriodSeconds` (one hour by default). Set it to at least
the longest run you expect, or an upgrade will interrupt work mid-write.

## Known limits

Read these before going to production. They are application limits, not chart
limits, and they are documented in [`docs/audit`](../../../docs/audit).

- **Anyone the provider authenticates gets an account**, unless you say
  otherwise. Identity itself is safe — Tabularia keys users on the stable
  `iss`+`sub` pair from the token, and an existing account can be claimed only
  once and only when the provider asserts the email as verified — but admission
  is open by default. If your issuer is the whole company directory, set
  `oidc.allowedEmailDomains` or `oidc.requireAllowlistedGroup`, or restrict
  assignment on the provider side.
- **Outbound connections are not filtered.** A user who can create database
  connections can make the engine connect to anything the cluster reaches,
  including cloud metadata endpoints. Enable `networkPolicy` with an explicit
  `egressCIDRs` list.
- **Outputs that append are not idempotent.** A run interrupted mid-write and
  retried appends its rows twice. Prefer replace mode where you can.
- **Engines are not perfectly interchangeable.** A flow designed on one engine
  can produce different results on another; the differences are catalogued in
  [`docs/engines/engine-differences.md`](../../../docs/engines/engine-differences.md).
  Keep the production engine equal to the development one unless you have checked
  that page.
