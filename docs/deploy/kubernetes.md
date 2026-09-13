# Deploying Tabularia on Kubernetes

Operational notes for whoever moves Tabularia from `infrastructure/docker-compose.yml`
(the **development** stack) to Kubernetes. The compose file is the reference for which
services exist and how they talk to each other; it is **not** a production manifest:
it runs dev servers with hot reload and bind-mounts the sources.

Everything below marked *verified* was exercised on 2026-09-12 against the real
stack (Scaleway object storage, managed ClickHouse).

## Helm chart

A chart that applies everything on this page lives in
[`infrastructure/helm/tabularia`](../../infrastructure/helm/tabularia). It encodes
the constraints as templates — the gateway and beat cannot be scaled, the root
filesystem is read-only, `/tmp` is sized per role — and refuses to render when a
required secret is missing. Its
[README](../../infrastructure/helm/tabularia/README.md) explains each decision,
and `values-production.example.yaml` is a filled-in starting point.

This page remains the reference for *why*; the chart is *how*.

## Components

| Component | Image | Command | Exposure | Replicas |
|---|---|---|---|---|
| Frontend (Nuxt 3) | `frontend/Dockerfile.prod`, see [Frontend](#frontend) | image default | public | N |
| Gateway (FastAPI control plane) | `gateway/Dockerfile`, see [Gateway image](#gateway-image-gatewaydockerfile) | image default | public | **exactly 1**, strategy `Recreate` |
| Engine API | `backend/Dockerfile`, see [Backend image](#backend-image-backenddockerfile) | image default | **internal only** | N |
| Run worker | `backend/Dockerfile` | `celery -A app.tasks.celery_app worker -E -Q celery --loglevel=info` | none | N |
| Preview worker | `backend/Dockerfile` | `celery -A app.tasks.celery_app worker -E -Q preview --loglevel=info` | none | N |
| Celery beat | `backend/Dockerfile` | `celery -A app.tasks.celery_app beat --schedule /tmp/celerybeat-schedule --loglevel=info` | none | **exactly 1** |
| PostgreSQL | managed service recommended | — | internal | — |
| Valkey / Redis | any Redis-compatible | — | internal | 1 |
| Object storage | any S3-compatible endpoint | — | — | — |
| ClickHouse *(optional engine)* | managed service | — | — | — |

Use the **image default command** for the gateway and the engine API. The compose file
overrides them with `--reload`, which must not reach production.

- **Gateway: one replica, `Recreate`.** The flow and refresh scheduler runs inside the
  gateway process and has no distributed lock. Two gateway pods — including the old and
  new pod overlapping during a rolling update — fire every schedule twice. The gateway
  also runs the lightweight schema migrations and seeds the admin user at startup.
- **Beat: one replica.** Two beats enqueue every periodic task twice.
- **Postgres** holds all control-plane metadata (users, groups, permissions, versioned
  flows, encrypted connection credentials, runs, audit log): back it up.
- **Valkey** is the Celery broker and the step-cache index. Its content is disposable:
  losing it drops queued tasks and makes the cache start cold, nothing else.

## Networking

- **Public**: the frontend and the gateway only. The browser calls the gateway directly
  (`NUXT_PUBLIC_API_BASE` = public gateway URL).
- **Internal**: the engine API is reached only by the gateway, through
  `ENGINE__BASE_URL` (cluster DNS, e.g. `http://engine:8000`). It has no authentication
  of its own: keep it off the ingress.

Ingress settings the application depends on:

| Setting | Why | Value |
|---|---|---|
| Max request body size | file uploads stream through the gateway to the engine and can be tens of GB | at least the largest file users upload |
| Request buffering | the proxy must not spool uploads to its own disk | off |
| Read timeout | previews wait for a worker synchronously (`PREVIEW_TIMEOUT_SECONDS` on the engine API, default 120) | above that ceiling |
| Rate limit on `/auth/login` | the application has no login throttling of its own | per client IP |

**Egress.** The engine connects to databases that users configure as connections: that
is the product, and also a server-side request surface. Restrict the engine pods' egress
with a `NetworkPolicy` to the object storage, ClickHouse, Valkey and the approved
database hosts, and block cloud metadata endpoints such as `169.254.169.254`.

## Backend image (`backend/Dockerfile`)

One Dockerfile for the four backend roles and for both development and production.
The local `docker-compose.yml` builds the very same file with `WITH_CHDB=true` and
`WITH_DEV=true` (test dependencies); production builds it with the defaults.
Production variants:

| Variant | Build | Size | Engines |
|---|---|---|---|
| Lightweight, the default | `docker build backend/` | 727 MB | Polars, DuckDB, ClickHouse external |
| With chDB | add `--build-arg WITH_CHDB=true` | 1.36 GB | the above plus embedded chDB |

chDB alone accounts for almost 600 MB, pandas and numpy included, which nothing else in
the application needs. In production the ClickHouse dialect is covered by the external
ClickHouse engine, so the lightweight variant is the default. Without chDB the engine
picker shows it as not configured, and a flow that still asks for it fails with a clear
message. For reference, the development image is 1.41 GB.

*Verified* with `--read-only` and a tmpfs `/tmp`: engine API healthy, run worker ready,
beat started; every available engine producing identical results on real data; CSV and
Excel exports written to `/tmp`; database ingest from PostgreSQL and ClickHouse working in
the lightweight variant.

- runs as uid/gid **10001**, no shell login, no build tools, no `uv`, no test dependencies;
- compatible with a **read-only root filesystem**: the only writable path is `/tmp`
  (`TMPDIR=/tmp`, `HOME=/tmp`); uploads, intermediate parquet files, DuckDB and Polars
  spills and chDB state all go there;
- `MALLOC_ARENA_MAX=2` is preset to limit memory fragmentation from Polars/Arrow threads.

```yaml
securityContext:
  runAsNonRoot: true
  runAsUser: 10001
  runAsGroup: 10001
  readOnlyRootFilesystem: true
  allowPrivilegeEscalation: false
  capabilities: { drop: ["ALL"] }
volumeMounts:
  - { name: tmp, mountPath: /tmp }
volumes:
  - name: tmp
    emptyDir: { sizeLimit: 50Gi }   # size per role, see below
```

### Sizing `/tmp`

Without a sized `/tmp` volume, a large file lands in the container's ephemeral storage
and the kubelet evicts the pod.

| Role | What lands in `/tmp` | Size for |
|---|---|---|
| Engine API | uploaded files and their parquet conversion, CSV/Excel exports | largest upload or export |
| Run worker | source parquet for in-process engines, database refresh results, data written to database or S3 outputs | largest dataset |
| Preview worker | source parquet for in-process engines | largest source |
| Beat | its schedule file | a few KB |

With the **ClickHouse engine in `s3` transport** for a flow, in both its development and
production engine, flow execution never touches local disk: the ClickHouse server reads
sources from and writes results to the bucket. Uploads, exports, database refreshes and
database/S3 outputs still use `/tmp` whatever the engine.

## Gateway image (`gateway/Dockerfile`)

Same rules as the backend image: one Dockerfile for development and production, uid/gid
**10001**, compatible with a read-only root filesystem, `/tmp` the only writable path.
The gateway itself writes no files, so a small `emptyDir` on `/tmp` is enough. Test
dependencies are included only with `--build-arg WITH_DEV=true`, which the local compose
sets. Production size: 199 MB.

*Verified* read-only against an empty PostgreSQL: the startup migrations create the
schema, the admin user is seeded, login works. The `securityContext` shown for the backend
applies unchanged.

## Worker lifecycle

- **Graceful shutdown.** A run can last up to the Celery task limit (3600 s). On
  `SIGTERM` a worker stops taking tasks and waits for the running ones; with the default
  `terminationGracePeriodSeconds: 30`, every deploy kills in-flight runs. Tasks are not
  redelivered (`acks_late` is off, on purpose: outputs in append mode are not
  idempotent), so a killed run is marked failed after `ENGINE__RUN_STALE_TIMEOUT_SECONDS`
  (default 3900). Set the grace period close to 3600 s for run workers, or accept that
  deploys interrupt runs.
- **Memory.** Set a memory limit per pod. The per-child memory cap is derived from the
  container limit and `CELERY__WORKER_CONCURRENCY`, so the limit is also the tuning knob.

## Probes

| Service | Endpoint |
|---|---|
| Gateway | `GET /health` |
| Engine API | `GET /healthcheck/` |
| Frontend | `GET /login` |

Workers and beat expose no HTTP port; rely on the restart policy.

## Configuration

Settings are environment variables with `__` as the nesting delimiter; the full list
with comments is in [`infrastructure/.env.example`](../../infrastructure/.env.example).
Values marked *secret* belong in Kubernetes Secrets.

**Comments in env files.** Not every tool strips a trailing `# comment` from a value:
`docker run --env-file` keeps it as part of the value, and the gateway then refuses to
start on `APP__TIMEZONE` — worse, a secret with a trailing comment would be silently
wrong. `infrastructure/.env.example` keeps every comment on its own line; if you derive
ConfigMaps or Secrets from an existing `.env`, check that no value carries a comment.

| Variable | Gateway | Backend roles | Notes |
|---|---|---|---|
| `APP__ENV_NAME=production` | ✓ | | enables the production guard: the gateway refuses to start with development secrets. It has **no effect** on the engine or the workers — nothing reads it there |
| `SECURITY__FERNET_KEY` *secret* | ✓ | ✓ | **same value everywhere**; encrypts database credentials; every process refuses to start without a valid key |
| `APP__TIMEZONE` | ✓ | | timezone in which the **gateway** interprets cron schedules. Celery beat keeps its own clock (`CELERY__TIMEZONE`, UTC by default), but the gateway converts schedules to UTC before enqueuing, so leaving beat on UTC is correct |
| `DB__HOST`, `DB__PORT`, `DB__USER`, `DB__PASSWORD` *secret*, `DB__NAME` | ✓ | | metadata Postgres |
| `JWT__SECRET` *secret*, `JWT__ACCESS_TTL_MINUTES` | ✓ | | session tokens, default lifetime 720 minutes |
| `AUTH__ADMIN_EMAIL`, `AUTH__ADMIN_PASSWORD` *secret*, `AUTH__ADMIN_NAME` | ✓ | | break-glass admin, seeded at first start |
| `APP__CORS_ORIGINS` | ✓ | | JSON list with the public frontend URL, e.g. `["https://app.example.com"]`. Needed **only** when the frontend is served from a different host than the gateway; the reference ingress puts both on one host, where it can stay unset (`app.corsOrigins` in the chart) |
| `ENGINE__BASE_URL` | ✓ | | internal URL of the engine API |
| `ENGINE__BUCKET` | ✓ | | the gateway's own copy of the bucket name; it compares it against the bucket in every request and rejects a mismatch with 403. It **must equal** `STORAGE__BUCKET`, so the chart and the compose file both derive it from the same single value — set it by hand only if you know why |
| `REDIS__HOST`, `REDIS__PORT`, `REDIS__DB` | | ✓ | broker and cache index |
| `STORAGE__ENDPOINT`, `STORAGE__ACCESS_KEY`, `STORAGE__SECRET_KEY` *secret*, `STORAGE__BUCKET`, `STORAGE__REGION` | | ✓ | object storage |
| `CELERY__WORKER_CONCURRENCY` | | ✓ | per worker pod |
| `CACHE__TTL_SECONDS`, `CACHE__SWEEP_INTERVAL_SECONDS` | | ✓ | step cache lifetime and sweep cadence |
| `CLICKHOUSE_EXTERNAL__*` | | ✓ | optional ClickHouse engine, off when the host is empty |
| `OIDC__*` | ✓ | | optional SSO, off when the issuer is empty |
| `NUXT_PUBLIC_API_BASE`, `NUXT_PUBLIC_BUCKET` | | | frontend, read at startup: public gateway URL and default bucket |

**SSO behind real hostnames.** `OIDC__REDIRECT_URI` is
`https://<gateway host>/auth/sso/callback` and must be registered, identical, on the
identity provider; `OIDC__POST_LOGIN_URL` is `https://<frontend host>/auth/callback`.
See [`docs/examples/keycloak/`](../examples/keycloak/).

## Frontend

Build the **production** image from `frontend/Dockerfile.prod`; `frontend/Dockerfile` is
the development server with hot reload and must not reach production. The production
image compiles the app with `nuxt build` and ships only the compiled output, served by
Nitro on port 3000. Size: 176 MB.

- **One image for every environment.** The public settings are read at startup, so no
  rebuild per environment: `NUXT_PUBLIC_API_BASE` (public gateway URL, called by the
  browser), `NUXT_PUBLIC_BUCKET` (must equal `STORAGE__BUCKET`) and, optionally,
  `NUXT_PUBLIC_GRAFANA_URL`.
- **No server-side API calls.** Server rendering never calls the gateway; every API call
  comes from the browser. The frontend pods need no route to the gateway.
- **Same hardening as the other images**: uid/gid 10001, read-only root filesystem, `/tmp`
  the only writable path; the `securityContext` shown for the backend applies unchanged.

*Verified* read-only: the login page renders, a gateway URL set at startup ends up in the
served page, compiled assets are served, and `/` redirects to `/login` without a session.

## Known application limits that affect operations

- **Single-instance scheduler** — gateway at one replica with `Recreate` (above).
- **Orchestrations interrupted by a gateway restart** can stay in the running state.
  Rollouts make restarts frequent, so expect to spot them in the run history.
- **Sessions are stateless JWTs** with no server-side revocation: disabling a user or
  removing them from an SSO group takes effect when their token expires.
