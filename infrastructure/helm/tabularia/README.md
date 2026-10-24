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

Each installation needs its own Redis database (`externalServices.redis.db`) or
instance: the queues and keys have fixed names, and two installations sharing them run
each other's tasks. And one release per namespace: the namespaces of the users' code
are named after the release's namespace.

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
`jumpasaservice/tabularia-frontend`, tagged with the release (`1.2.2`). The backend
one is built without chDB. Releases are built by the release workflow for amd64 and
arm64, and each image is scanned for secrets before it is pushed
([docs/release-process.md](../../../docs/release-process.md)).

To run your own builds instead, build from a checkout of the release tag, push them
to your registry, and set `image.registry` and the tags:

```bash
# Engine, workers and beat all run this one image.
docker build -t <registry>/tabularia-backend:1.2.2 backend/
#   Add --build-arg WITH_CHDB=true only if your flows use the embedded chDB
#   engine: it takes the image from about 800 MB to about 1.4 GB.
#   Add --build-arg WITH_MSSQL=true only if you read from SQL Server: the driver
#   is Microsoft's, under Microsoft's licence, and not in the published images
#   (docs/sqlserver.md).

docker build -t <registry>/tabularia-gateway:1.2.2 gateway/

# NOTE the -f: frontend/Dockerfile is the hot-reload DEVELOPMENT server.
docker build -f frontend/Dockerfile.prod -t <registry>/tabularia-frontend:1.2.2 frontend/
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

The pods restart by themselves when the chart's Secret changes (`checksum/secret`,
as `checksum/config` does for the ConfigMap). A Secret of your own is outside the
chart: after changing it, restart them with
`kubectl rollout restart deployment --namespace <namespace>`.

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
the Redis lists `celery` and `preview`. Use KEDA if you can: with KEDA installed in
the cluster, `worker.autoscaling.keda.enabled` (and the same for `previewWorker`)
replaces the CPU HPA with a ScaledObject that adds a pod for every `listLength`
tasks waiting. The gateway is the exception: it is busy exactly when it burns CPU,
so `gateway.autoscaling` on CPU is the right signal for it.

Scaling in can also interrupt a run, which is why workers get a long termination
grace period. Prefer letting the queue drain over aggressive scale-in.

### Scaling from the admin panel

Admin → Workers shows every worker, its busy processes, its memory and the tasks
waiting, and lets an administrator change two things:

- **Processes per worker**, for the run and the preview workers. Every worker
  resizes its own pool within seconds, on any installation: it grows at once and
  shrinks only by idle processes, so no task is interrupted. The pod's memory
  limit stays the chart's: give the pods room for the most processes you allow.
- **Minimum and maximum pods**, on Kubernetes only, with `adminScaling.enabled`
  and an autoscaler on the workers (`autoscaling.enabled`, CPU HPA or KEDA).

With `adminScaling.enabled`, the gateway's and the orchestrator's service accounts
(`<release>-tabularia-scaler`, `<release>-tabularia-orchestrator`) may `get` the two worker Deployments
and `get`/`patch` their two autoscalers, named one by one — nothing else, in this
namespace only. The panel cannot set a maximum above `autoscaling.panelMaxReplicas`:
that is your budget per Deployment. An HPA keeps at least one pod; KEDA may go to
zero.

The bounds set from the panel are kept: the orchestrator puts them back within a
minute when the cluster has others, so a `helm upgrade` does not silently undo an
administrator's choice. Removing them from the panel hands them back to the chart
at the next upgrade. With GitOps, tell the controller to ignore those two fields,
or it and the gateway will keep undoing each other. For Argo CD:

```yaml
ignoreDifferences:
  - group: autoscaling
    kind: HorizontalPodAutoscaler
    jsonPointers: [/spec/minReplicas, /spec/maxReplicas]
  - group: keda.sh
    kind: ScaledObject
    jsonPointers: [/spec/minReplicaCount, /spec/maxReplicaCount]
```

On docker compose the panel shows the containers and changes the processes per
worker; the number of containers stays yours (`docker compose up -d --scale
worker=N`): scaling a service from the app would need the Docker socket, which is
root on the host.

## Scripting environments

`scripts.enabled` turns on **Administration → Scripting environments**: the Python and
R environments that user code will run in, each built once into an image by a Job and
checked by a second one ([docs/scripting.md](../../../docs/scripting.md)). It needs an
image registry the cluster can pull from (`scripts.registry.repository`, with a push
Secret and a pull Secret).

The chart then creates three namespaces next to the release's (`createNamespaces`):
- `<namespace>-scripts`, at the `restricted` Pod Security level, for the checks and
  the scripts;
- `<namespace>-scripts-dev`, `restricted`, for the editor's trials, with a
  ResourceQuota (`trialQuota`) so they never take resources from the flows' runs;
- `<namespace>-script-builds`, at the `privileged` level, for rootless BuildKit. It
  needs a user namespace of its own, which only `privileged` admits; its pods still
  run as a user, without privileges.

A registry whose certificate is signed by your company's authority: give it in
`scripts.registry.caCert` (PEM) for the builds, and install it on the nodes for the
pulls. Each image is recorded with the architecture it was built for, and its check and
scripts run on nodes of that architecture. Details, and what fails how, in
[docs/scripting.md](../../../docs/scripting.md#a-registry-with-your-companys-certificate).

The gateway runs as `<release>-tabularia-scaler`, the orchestrator as `<release>-tabularia-orchestrator`:
the accounts of Admin → Workers. In those namespaces only, they may create, read and delete Jobs, read their Pods, logs and
events, and (scripts and trials) create the ConfigMap and Secret a Job mounts. In the
builds' namespace, `privileged`, only the orchestrator's account creates Jobs: it alone
runs the builds, so the gateway, which answers the internet, cannot start a privileged
Pod. Nothing runs in the release's namespace.

These two accounts are the chart's own, created whenever `adminScaling`, `scripts`,
`containers`, `clusterView` or `settingsRollout` is on (the last two are on by default):
`serviceAccount.create`, `serviceAccount.name` and `serviceAccount.annotations` apply to
the other pods only. A cluster where the release may not create ServiceAccounts, Roles
and RoleBindings must turn all five off (`clusterView.enabled=false`,
`settingsRollout.enabled=false`).

## Container registries

`containers.enabled` turns on **Administration → Container registries**: the registries
the images of the Container node come from, defined by administrators with their
credentials, how to trust their certificate (a company authority, or HTTP for an
internal registry) and the repositories allowed
([docs/container-registries.md](../../../docs/container-registries.md)). The gateway
speaks to the registries; the nodes pull the images, so with a company authority or HTTP
they must trust the registry too (**Try in the cluster**, in the registry's page, checks
it).

The chart creates `containers.namespace` (default `<namespace>-containers`, after the
release's namespace) and `containers.trialNamespace` (`<namespace>-containers-dev`, with
`containers.trialQuota`),
both at the `baseline` Pod Security level, and a Role in each for the gateway's and the
orchestrator's accounts
(Jobs, Pods, logs, events, Secrets: Tabularia writes a pull Secret per registry there).
`containers.createNamespaces: false` when they exist already. `containers.limits`
caps what a Container node may ask for (CPU, memory, timeout). The namespaces where
users' code runs (scripts, their trials, the image builds, the containers and their
trials) get a NetworkPolicy (`scripts.blockTabularia`, `containers.blockTabularia`, on by
default): the code reaches the internet and the other namespaces, never the pods of the
release's namespace nor the cloud metadata addresses. The private ranges outside the
cluster stay closed unless listed in `networkPolicy.userCodePrivateCIDRs`: the registry
the image builds push to when its address is private, a scripts bucket on a private
endpoint, a company database. A database, broker or storage of Tabularia in another
namespace of the cluster is reachable from that code: give it a policy of its own.
The policy needs a network plugin that enforces egress rules (Calico, Cilium);
without one it has no effect. What to list, in
[docs/scripting.md](../../../docs/scripting.md#network-policies).

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

Every process refuses to start without it — that is deliberate, so a missing key
fails at deploy time rather than at the first run. (The Compose stack of
`infrastructure/` is the one exception: it creates a key in its `keys` volume through
`SECURITY__KEYS_DIR`, which pods cannot share.)

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

## Administration → Settings: restarts

`settingsRollout.enabled` (on by default) lets Administration → Settings apply the
settings read only when a process starts (marked with the restart icon: the gateway's
threads and database pool, the workers' memory and recycling, the engine API's threads,
the scheduler's intervals): the orchestrator evicts the pods of the components concerned
one at a time, through the eviction API, waiting each time for the new pod to be ready,
so the PodDisruptionBudgets hold. The gateway's and the orchestrator's accounts get a Role
in the release's namespace to `get`/`list` pods, the orchestrator's alone one to `create`
`pods/eviction` (it runs the restart and answers nobody from outside), and the variable
`KUBERNETES__ROLLOUT`. Not the right to patch the
Deployments, as `kubectl rollout restart` would need: that right is the right to change
their image or account. Off, the panel saves those settings and names the components to
restart by hand (docs/engine-settings.md).

## Administration → Cluster

`clusterView.enabled` (on by default) gives Tabularia's administrators and observers a
page with the pods of the release and of the users' code (the scripts' and containers'
namespaces): their state, restarts, age, node, CPU and memory against their limits, and
the warning events, refreshed every three seconds; a Job of a script or a container
links to the flow, saved script or flow group that started it. The gateway's and the
orchestrator's accounts get a Role in the release's namespace to `get`/`list` pods and
pod metrics and `list` events; the scripts' and containers' Roles read their pods'
metrics too. Nothing cluster-wide, no logs, no exec: for those, a Kubernetes client.
CPU and memory need metrics-server in the cluster (most managed clusters have it;
`minikube addons enable metrics-server`; on kind, docs/deploy/kind.md).

## Database backups

`backup.enabled` (on by default; Kubernetes only — on Docker Compose the section is
greyed out): every night at 03:00 (`backup.schedule`, a cron in
`app.timezone`) an orchestrator runs `pg_dump` of Tabularia's database and streams it to
the engine, which stores it in the object storage and keeps the latest `backup.keep`
(14). Administration → Backups changes those three values without an upgrade, runs one
at once and downloads them; every download is in the audit log.

By default the files go to Tabularia's own bucket, under `backup.prefix`
(`backups/postgres/`). **In production give them a bucket of their own** — `backup.bucket`,
and `backup.endpoint`, `backup.region`, `secrets.backupAccessKey` and
`secrets.backupSecretKey` when it is elsewhere or needs other keys — so that losing one
does not lose both; with `networkPolicy.egressCIDRs` restricted, the engine must reach
that endpoint. The engine alone reads those keys: the chart keeps them out of the
gateway's and the orchestrator's environment. A backup holds every account's e-mail
address and the connections' passwords (encrypted with `secrets.fernetKey`): keep the
bucket private, and the Fernet key apart from it. Restoring one:
[docs/deploy/upgrading.md](../../../docs/deploy/upgrading.md#when-something-goes-wrong).

## Upgrades

The whole procedure — backup, `helm upgrade`, the checks, rolling back and restoring —
is in [docs/deploy/upgrading.md](../../../docs/deploy/upgrading.md). Below, what a given
release asks for besides it.

**Coming from chart 2.2.0 (Tabularia 1.2.1)** — the namespaces of the users' code get a
NetworkPolicy (`scripts.blockTabularia`, `containers.blockTabularia`, default `true`).
Before upgrading, list in `networkPolicy.userCodePrivateCIDRs` the private addresses
the scripts, the image builds and the Container nodes must reach (the registry the
builds push to, a scripts bucket on a private endpoint, company databases), or set the
two values to `false` to keep the previous behaviour. The orchestrator moves to a
service account of its own, `<release>-tabularia-orchestrator`, the only one that may create the
image builds' Jobs: a build that starts while the old orchestrator pod is still
running fails with *Forbidden*; **Build again** after the upgrade.

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
  `egressCIDRs` list (CIDRs or `{cidr, except}`); the metadata address
  169.254.169.254 (fd00:ec2::254) is excluded from any range that holds it. The
  broker, the storage and ClickHouse, when their host is a Service of this cluster
  in another namespace, are allowed from that host; list any other namespace the
  workers must reach in `egressNamespaces`.
- **Outputs that append are not idempotent.** A run interrupted mid-write and
  retried appends its rows twice. Prefer replace mode where you can.
- **Engines are not perfectly interchangeable.** A flow designed on one engine
  can produce different results on another; the differences are catalogued in
  [`docs/engines/engine-differences.md`](../../../docs/engines/engine-differences.md).
  Keep the production engine equal to the development one unless you have checked
  that page.
