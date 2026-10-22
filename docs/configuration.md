<!-- {% raw %} GitHub Pages: no Liquid in this page -->
# Configuration reference

Every setting of Tabularia is an environment variable. This page lists all of them: the
ones the gateway and the engine read, the frontend's, and the few that only the Docker
Compose files and the Helm chart use. It is generated from the code by
[`.github/scripts/config_reference.py`](../.github/scripts/config_reference.py), and a
check fails when a variable is added, removed or changed without the page following:
edit the code or [`config_reference.toml`](../.github/scripts/config_reference.toml),
never this page.

**Nested names.** The settings are grouped in sections, and a variable names the section
and the field with a double underscore: `STORAGE__BUCKET` is the `bucket` field of the
`storage` section, `CLICKHOUSE_EXTERNAL__S3_ENDPOINT` the `s3_endpoint` field of
`clickhouse_external`. Write them in capitals. Booleans are `true` or `false`, lists are
JSON (`APP__CORS_ORIGINS=["https://app.example.com"]`), and a variable left out keeps the
default shown below. Keep comments on their own line: not every tool strips a trailing
`# comment` from a value.

**Where to set them.**

- *Docker Compose*: in `infrastructure/.env`, a copy of
  [`infrastructure/.env.example`](../infrastructure/.env.example). The gateway and
  engine containers read the whole file, so one value reaches every service that needs
  it. Compose is the way to try Tabularia; production runs on Kubernetes with the Helm
  chart ([Kubernetes guide](deploy/kubernetes.md)).
- *Helm*: the **Helm value** column gives the key of `values.yaml` the chart renders the
  variable from. A variable without one goes in `extraEnv`, by its name (it lands in the
  shared ConfigMap; the chart refuses a name it already sets), or, for a secret, in the
  Secret you name in `existingSecret`. See [Deploying on Kubernetes](deploy/kubernetes.md).

**Who reads what.** The *gateway* is the control plane (API, sign-in, permissions,
schedules, flow orchestration), in one process or split into `api` and `orchestrator`
copies (`APP__ROLE`). The *engine* is one image with several roles: the internal engine
API, the run and preview workers, and the beat scheduler; they all read the same
settings. The *frontend* reads three public values at startup. A variable only matters to
the service it is listed under; giving it to the others is harmless.

**Secrets** are never printed here. *required* means the process refuses to start
without the variable; *development value (hidden)* means a placeholder that works on a
laptop and must be replaced before anyone else can reach the installation (the gateway
refuses some of them when `APP__ENV_NAME=production`).

**Not in the environment.** Some things are set in the administration panel instead: the
engine parameters marked *Also adjustable in Administration → Settings* (the variable is
the starting value, see [engine settings](engine-settings.md)), the processes per worker
(Administration → Workers), the AI models people may use, the engines allowed, the
forwarding of the audit log to a SIEM ([Splunk](siem-splunk.md)), webhook receivers and
service accounts ([API](api.md)), and the banners. Connection credentials (databases,
SFTP, S3, APIs, SharePoint) belong to each connection, encrypted with
`SECURITY__FERNET_KEY`.

| Read by | Variables |
|---|---|
| [Shared by the gateway and the engine](#shared-by-the-gateway-and-the-engine) | 7 |
| [Gateway](#gateway) | 144 |
| [Engine and workers](#engine-and-workers) | 92 |
| [Frontend](#frontend) | 3 |
| [Compose and Helm only](#compose-and-helm-only) | 13 |
| **Total** | **259** |

## Before production

The defaults are made for a laptop. Before an installation is reachable by anyone else,
set at least these (the [release checklist](deploy/release-checklist.md) lists what
changed between versions):

- `STORAGE__ACCESS_KEY` — use credentials of your own (`minioadmin` is the development MinIO's).
- `STORAGE__SECRET_KEY` — use credentials of your own.
- `SECURITY__FERNET_KEY` — generate a key of your own and keep it with your backups: without it the saved connections cannot be decrypted.
- `DB__PASSWORD` — replace the development password (the gateway refuses it in production unless `DB__URL` is set).
- `JWT__SECRET` — generate a secret of at least 32 characters (the gateway refuses the default and shorter ones in production).
- `AUTH__ADMIN_PASSWORD` — set a password of your own before the first start (at least 12 characters; the gateway refuses `admin` and other published passwords).
- `APP__ENV_NAME` — set it to `production`, so the gateway refuses the development secrets.
- `SECURITY__KEYS_DIR` — on Kubernetes leave it empty and set `SECURITY__FERNET_KEY`; with Compose, back the `keys` volume up with the database.
- `AI__MAX_COST_PER_DAY_USD` — set it, and `AI__MAX_COST_PER_DAY_TOTAL_USD`, wherever people you do not know can sign in (every question is paid with your key).
- `AUTH__FIRST_RUN_SETUP` — leave it off: the administrator comes from `AUTH__ADMIN_PASSWORD`.
- `GRAFANA_ADMIN_PASSWORD` — change it wherever Grafana is reachable by others.

## Shared by the gateway and the engine

Read by the gateway and by the engine. Give both the same value: the Compose file and the
chart do, since every container reads the same `.env` or ConfigMap.

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `APP__ENV_NAME` | string | `development` | `app.envName` | **Production:** set it to `production`, so the gateway refuses the development secrets. `production` turns on the gateway's production guard: it refuses to start with the development secrets (`JWT__SECRET`, `DB__PASSWORD`), an empty or published `AUTH__ADMIN_PASSWORD`, or a `JWT__SECRET` shorter than 32 characters. The engine accepts it and nothing there depends on it. |
| `APP__CORS_ORIGINS` | JSON list of strings; `*` and `null` refused | `["http://localhost:3000", "http://127.0.0.1:3000"]` | `app.corsOrigins` | Origins allowed to call the API from a browser, with the session cookie. Needed only when the interface is served from another host than the gateway (then set `AUTH__COOKIE_DOMAIN` too). The engine API applies the same list to its own CORS. |
| `STORAGE__ENDPOINT` | string | `http://localhost:9002` | `externalServices.storage.endpoint` | URL of the S3-compatible object storage, as the workers reach it (`http://minio:9000` in Compose). The gateway reads it only to name the storage in OpenLineage events (`gs://` for Google Cloud Storage). |
| `CELERY__TASK_TIME_LIMIT` | integer | `3600` | `engine.taskTimeLimitSeconds` | Seconds after which the worker kills a task: the real ceiling on a run, an import or a preview. The gateway reads it too, to derive `ENGINE__RUN_STALE_TIMEOUT_SECONDS`. Also adjustable in Administration → Settings. |
| `SECURITY__FERNET_KEY` | string | empty | `secrets.fernetKey` | **Production:** generate a key of your own and keep it with your backups: without it the saved connections cannot be decrypted. Encrypts the credentials of database and file connections, at rest in the gateway and on their way to the engine, which decrypts them only inside the worker. The same value everywhere; never change it once a connection is saved. Empty with `SECURITY__KEYS_DIR` set (the Compose stack): the key created there at the first start. Empty without it: the services refuse to start. Generate one with `openssl rand -base64 32 \| tr '+/' '-_'`. |
| `SECURITY__KEYS_DIR` | string | empty | `extraEnv` | **Production:** on Kubernetes leave it empty and set `SECURITY__FERNET_KEY`; with Compose, back the `keys` volume up with the database. A directory every service shares on one machine (the Compose stack mounts the `keys` volume there): the secrets left empty are created in it at the first start and read afterwards, `fernet.key` for `SECURITY__FERNET_KEY` and, with `AUTH__FIRST_RUN_SETUP`, `bootstrap.token`, which the sample-data container exchanges for a session once the administrator exists. Back it up with the data. Not for Kubernetes, where the pods share no volume: the chart sets the key. |
| `UPLOAD__MAX_BYTES` | integer, ≥ 0 | `1073741824` (1 GiB) | `extraEnv` | The largest file a person can upload, in bytes; 0 = no ceiling. The gateway answers 413 before calling the engine when the size is declared, and cuts an undeclared (chunked) body at the ceiling; the engine API refuses it too. The file is spooled on the engine's disk and converted there, so this is also a disk bound: keep the engine's temporary space above it. Imports from SFTP, S3 and APIs have their own caps (`SFTP__*`, `S3_SOURCE__*`, `API__*`). |

## Gateway

The control plane: in Compose the `gateway` service; on Kubernetes the `gateway` (`api`)
and `orchestrator` workloads.

### Process and HTTP

Read by both services, so listed under [Shared by the gateway and the engine](#shared-by-the-gateway-and-the-engine): `APP__CORS_ORIGINS`, `APP__ENV_NAME`.

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `APP__NAME` | string | `Tabularia` | `extraEnv` | Product name shown by the API. Product identity, not a deployment setting: leave it. |
| `APP__VERSION` | string | `1.2.2` | `extraEnv` | The release, shown by `/system/info` and in the interface's menu. Set by the code; leave it. |
| `APP__CODENAME` | string | `Appio` | `extraEnv` | The release's name. Set by the code; leave it. |
| `APP__ROLE` | one of `all`, `api`, `orchestrator` | `all` | set per workload: `api` for the gateway, `orchestrator` for the orchestrator | What this gateway process does: `all` answers requests and does the background work (schedules, flow executions, clean-up); `api` only answers; `orchestrator` only does the background work. Split them to run several copies: they share the work through the database. |
| `APP__ROOT_PATH` | string; a path such as `/api` | empty | `ingress.apiPrefix` | Path prefix a reverse proxy publishes the gateway under, stripping it before forwarding (`/api` when one host serves interface and API). It fixes the paths the gateway writes for the browser: the SSO cookie, `/docs`, redirects. Empty = published at the root. |
| `APP__GATEWAY_THREADS` | integer, ≥ 4 | `40` | `gateway.threads` | Threads serving the synchronous routes. More do not serve more (Python runs one at a time); the database pool follows this number. A different name from the engine's `APP__ENGINE_API_THREADS` because both read the same file and want opposite values. |
| `APP__TIMEZONE` | string; an IANA name, e.g. `Europe/Rome` | `UTC` | `app.timezone` | Time zone in which cron schedules are read (wall-clock time, daylight saving included); they are stored in UTC. Timestamps are still shown in each viewer's own time zone. |

### Metadata database (Postgres)

Users, groups, permissions, flows, connections, runs, schedules and the audit log. The schema is migrated with Alembic: see [database migrations](database-migrations.md).

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `DB__HOST` | string | `localhost` | `externalServices.postgres.host` | Postgres host. |
| `DB__PORT` | integer | `5432` | `externalServices.postgres.port` | Postgres port. |
| `DB__USER` | string | `tabularia` | `externalServices.postgres.user` | Postgres user. The development Compose file also creates the database with it. |
| `DB__PASSWORD` | string | development value (hidden) | `secrets.dbPassword` | **Production:** replace the development password (the gateway refuses it in production unless `DB__URL` is set). Postgres password. |
| `DB__NAME` | string | `tabularia` | `externalServices.postgres.database` | Postgres database. |
| `DB__URL` | string | empty | `existingSecret` | A full DSN (`postgresql://user:password@host:5432/dbname`) for a managed Postgres that does not fit the pieces above; when set it wins over them. It holds the password: treat it as a secret. |
| `DB__POOL_SIZE` | integer, ≥ 1 | empty | `extraEnv` | Connections kept open to Postgres. Empty = one per thread (`APP__GATEWAY_THREADS`), which is what the gateway needs. Pool plus overflow, summed over every gateway process, must stay under Postgres' `max_connections`. |
| `DB__MAX_OVERFLOW` | integer, ≥ 0 | `10` | `extraEnv` | Extra connections opened at a peak and closed when returned. |
| `DB__AUTO_MIGRATE` | boolean | `false` | `extraEnv` | `true`: the gateway migrates the schema itself at startup (the development Compose file sets it). `false`: migrations are a step of their own (`python -m app.db.migra`, the chart's init container) and the gateway only refuses a database behind its code. |

### Sign-in, sessions and API tokens

Local sign-in and the session cookie, the first administrator, and the API tokens of scripts and service accounts ([API](api.md)).

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `JWT__SECRET` | string | development value (hidden) | `secrets.jwtSecret` | **Production:** generate a secret of at least 32 characters (the gateway refuses the default and shorter ones in production). Signs every session and API token (HS256). Generate one with `openssl rand -hex 32`; changing it signs everybody out. |
| `JWT__ALGORITHM` | string | `HS256` | `extraEnv` | Signature algorithm of the session tokens. Leave it. |
| `JWT__ACCESS_TTL_MINUTES` | integer | `720` | `auth.jwtAccessTtlMinutes` | The longest a session lasts, active or not. Sessions also end at sign-out, from «Your sessions», by an administrator, at a password change or when the account is disabled. |
| `AUTH__ADMIN_EMAIL` | string | `admin@tabularia.local` | `auth.adminEmail` | E-mail of the administrator created at the first start. Existing users are never touched. |
| `AUTH__ADMIN_PASSWORD` | string | empty | `secrets.adminPassword` | **Production:** set a password of your own before the first start (at least 12 characters; the gateway refuses `admin` and other published passwords). Password of that administrator, used when it is created. No default: the gateway refuses to create the administrator with a published password (`admin`, the guides' examples) or one under 12 characters, in every environment. Empty with `AUTH__FIRST_RUN_SETUP` (the Compose stack): no administrator at the start, the sign-in page creates it. Empty otherwise: the gateway refuses to start. An administrator still holding `admin` from an older release gets this password at the next start, and its sessions close. |
| `AUTH__ADMIN_NAME` | string | `Administrator` | `auth.adminName` | Display name of that administrator. |
| `AUTH__ALLOW_KNOWN_ADMIN_PASSWORD` | boolean | `false` | `extraEnv` | Development only: `true` lets a local stack whose scripts sign in with a fixed password (`admin`) create or keep it. Ignored with `APP__ENV_NAME=production`. |
| `AUTH__FIRST_RUN_SETUP` | boolean | `false` | `extraEnv` | **Production:** leave it off: the administrator comes from `AUTH__ADMIN_PASSWORD`. While no account exists, the sign-in page asks for the first administrator (email and password, the same password rule), signs it in, and from then on is the ordinary sign-in. On with `docker-compose.yml`, which answers on 127.0.0.1 only; `AUTH__ADMIN_PASSWORD`, when set, creates the administrator instead. Ignored with `APP__ENV_NAME=production`. |
| `AUTH__SESSION_IDLE_MINUTES` | integer, ≥ 0 | `0` | `auth.sessionIdleMinutes` | Minutes without activity after which a session closes: the gateway refuses it and the open page signs out by itself after a one-minute warning. 0 = never. |
| `AUTH__COOKIE_DOMAIN` | string | empty | `auth.cookieDomain` | Domain of the session cookie. Empty = the gateway's host, right when interface and API share a host. With the interface on another host of the same domain (`app.example.com` and `api.example.com`) set the common domain, `example.com`; two unrelated domains do not work. |
| `AUTH__PERSONAL_TOKENS` | boolean | `true` | `auth.personalTokens` | Whether people may create API tokens for their own scripts, with their own permissions. `false`: only service accounts, created by an administrator, have tokens. |
| `AUTH__API_TOKEN_MAX_DAYS` | integer, 1–3650 | `365` | `auth.apiTokenMaxDays` | The furthest expiry of an API token: every token has one. |

### Single sign-on (OIDC)

Optional: Keycloak, Microsoft Entra ID, Auth0, Okta… Off while `OIDC__ISSUER` is empty; when it is set, the gateway configures itself from the issuer's discovery document and refuses to start if the client secret, redirect URI or post-login URL is missing. Runnable example: [`docs/examples/keycloak/`](examples/keycloak/); how groups are mapped: [SSO group mapping](design/sso-group-mapping.md).

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `OIDC__ISSUER` | string | empty | `oidc.issuer` | Issuer URL, e.g. `https://keycloak.example.com/realms/tabularia` or, for Entra ID, `https://login.microsoftonline.com/<tenant-id>/v2.0`. Empty = SSO off. |
| `OIDC__CLIENT_ID` | string | empty | `oidc.clientId` | Client id registered on the identity provider. |
| `OIDC__CLIENT_SECRET` | string | empty | `secrets.oidcClientSecret` | Secret of that (confidential) client. Required when SSO is on. |
| `OIDC__REDIRECT_URI` | string | empty | `oidc.redirectUri` | The gateway's callback, e.g. `https://tabularia.example.com/api/auth/sso/callback`; it must match the one registered on the identity provider exactly. Required when SSO is on. |
| `OIDC__SCOPES` | string | `openid profile email` | `oidc.scopes` | Scopes asked for, separated by spaces; must include `openid`. |
| `OIDC__GROUPS_CLAIM` | string | `groups` | `oidc.groupsClaim` | Where the groups are: a claim (`groups` for Keycloak's group mapper, `roles` for Entra ID app roles) or a path into the token, as it appears in the JSON: `realm_access.roles` and `resource_access.<client id>.roles` for Keycloak's realm and client roles (a client id with dots works). Several, comma-separated, are joined: `groups,realm_access.roles`. A path the id_token does not carry is looked up in the access token (Keycloak puts the roles there by default), then in the provider's userinfo. |
| `OIDC__GROUP_ALLOWLIST` | string | empty | `oidc.groupAllowlist` | Identity-provider groups mapped onto Tabularia groups, separated by commas. Empty = all of them. |
| `OIDC__AUTO_CREATE_GROUPS` | boolean | `false` | `oidc.autoCreateGroups` | `true`: create the Tabularia groups missing at sign-in. `false`: map only onto groups an administrator already made. |
| `OIDC__AUTHORITATIVE` | boolean | `true` | `oidc.authoritative` | `true`: the identity provider owns group membership (groups absent from the claim are removed). `false`: SSO only adds. |
| `OIDC__SUPERUSER_GROUP` | string | empty | `oidc.superuserGroup` | Members of this group are administrators. Empty = nobody becomes one through SSO. |
| `OIDC__ALLOWED_EMAIL_DOMAINS` | string | empty | `oidc.allowedEmailDomains` | Only people with an e-mail in these domains may sign in, separated by commas. Empty = anyone the identity provider authenticates, which may be a whole company directory. |
| `OIDC__REQUIRE_ALLOWLISTED_GROUP` | boolean | `false` | `oidc.requireAllowlistedGroup` | `true`: signing in requires one of the groups of `OIDC__GROUP_ALLOWLIST` (or the administrators' group). |
| `OIDC__POST_LOGIN_URL` | string | `http://localhost:3000/auth/callback` | `oidc.postLoginUrl` | The interface page that receives the session after SSO, e.g. `https://tabularia.example.com/auth/callback`. |
| `OIDC__BUTTON_LABEL` | string | `Single sign-on` | `oidc.buttonLabel` | Label of the SSO button on the sign-in page. |
| `OIDC__DISCOVERY_TTL_SECONDS` | integer | `3600` | `extraEnv` | How long the identity provider's discovery document is trusted before it is fetched again. |
| `OIDC__TIMEOUT_SECONDS` | number | `10` | `extraEnv` | Timeout of the calls to the identity provider. |
| `OIDC__CA_FILE` | string | empty | oidc.caCert / oidc.caConfigMap | A PEM file of the certificate authorities to trust for the identity provider, besides the public ones: a company Keycloak whose certificate the company's own authority signed (without it every sign-in fails with `idp_untrusted`). The chart mounts it from `oidc.caCert` or `oidc.caConfigMap`. The gateway refuses to start when the file is not one. |
| `OIDC__VERIFY_TLS` | boolean | `true` | `oidc.verifyTls` | Whether the identity provider's certificate is checked. `false` accepts it even when no trusted authority signed it (a company Keycloak whose authority you cannot give in `OIDC__CA_FILE`): whoever can stand between the gateway and the provider can then sign in as anyone, administrators included. The gateway logs a warning at every start. See [Single sign-on with Keycloak](sso-keycloak.md#a-certificate-the-gateway-does-not-trust). |
| `OIDC__LOGIN_TX_TTL_SECONDS` | integer | `600` | `extraEnv` | How long a sign-in may stay on the identity provider's page (the state, nonce and PKCE cookie). |

### Link to the engine

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `ENGINE__BASE_URL` | string | `http://localhost:8000` | set by the chart (the engine Service) | Internal URL of the engine API (`http://backend:8000` in Compose). The gateway is its only client. |
| `ENGINE__BUCKET` | string | `data-prep` | `externalServices.storage.bucket` | The gateway's copy of the bucket name, compared with the bucket of every request (a mismatch is refused with 403). It must equal `STORAGE__BUCKET`: the Compose file and the chart derive it from that value, so set it by hand only if you know why. |
| `ENGINE__RUN_STALE_TIMEOUT_SECONDS` | integer | `CELERY__TASK_TIME_LIMIT` + 300 | `engine.runStaleTimeoutSeconds` | A run that has not reached a final state after this long is declared lost and marked failed. It must stay above `CELERY__TASK_TIME_LIMIT`, so the gateway gives up only after the worker has killed the task. |
| `ENGINE__SETTINGS_PANEL` | boolean | `true` | `engine.settingsPanel` | Whether Administration → Settings may change some engine parameters at runtime ([engine settings](engine-settings.md)). `false`: the environment is the only source, the panel shows the values without changing them, and values saved before stop applying. |
| `ENGINE__DBT_EXPORT_TIMEOUT_SECONDS` | number, > 0 and ≤ 3600 | `100` | `extraEnv` | Seconds the gateway waits for the engine to build one dbt export; past it the download stops with a clear 504 instead of hanging until the ingress cuts it. Keep it, plus `AI__DBT_EXPORT_TIMEOUT_SECONDS`, under the ingress' read timeout. |

### AI assistant

Optional: a chat over the catalog and an assistant in the flow editor, against any OpenAI-compatible endpoint. Off until `AI__BASE_URL` and `AI__SECRET_KEY` are set, and no model is usable until an administrator enables it. On the external ClickHouse the assistant can run as a read-only user: see [the AI user](engines/clickhouse-ai-user.md). The dbt export uses it too ([dbt export](dbt-export.md)).

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `AI__BASE_URL` | string | empty | `ai.baseUrl` | OpenAI-compatible endpoint, e.g. `https://api.scaleway.ai/v1`. Empty = assistant off. |
| `AI__ACCESS_KEY` | string | empty | `ai.accessKey` | Identifier of the key (Scaleway calls it access key); the protocol authenticates with the secret alone, this only tells which key is in use. |
| `AI__SECRET_KEY` | string | empty | `secrets.aiSecretKey` | The API key. Empty = assistant off. |
| `AI__DEFAULT_MODEL` | string | empty | `ai.defaultModel` | Model proposed first in the picker, if an administrator enabled it. |
| `AI__MAX_RESULT_ROWS` | integer, 1–2000 | `200` | `ai.maxResultRows` | Rows a query of the assistant may return, to the model and to the chat. |
| `AI__MAX_REQUESTS` | integer, 1–50 | `12` | `ai.maxRequests` | Model round-trips per message of the data chat: the cap on its tool-calling loop. |
| `AI__MAX_FLOW_REQUESTS` | integer, 1–80 | `30` | `ai.maxFlowRequests` | The same for the flow editor's assistant, which takes a round-trip for every step it builds and checks. |
| `AI__MAX_HISTORY_MESSAGES` | integer, 0–400 | `60` | `extraEnv` | Past messages of a conversation replayed to the model: higher remembers more and costs more per turn. |
| `AI__MAX_COST_PER_TURN_USD` | number, ≥ 0 | `0.5` | `ai.maxCostPerTurnUsd` | Spending ceiling of one turn, in dollars, enforced by pydantic-ai. The round-trip cap does not limit tokens. |
| `AI__MAX_COST_PER_DAY_USD` | number, ≥ 0 | `0` | `ai.maxCostPerDayUsd` | **Production:** set it, and `AI__MAX_COST_PER_DAY_TOTAL_USD`, wherever people you do not know can sign in (every question is paid with your key). Daily ceiling per user, in dollars. 0 = none. |
| `AI__MAX_COST_PER_DAY_TOTAL_USD` | number, ≥ 0 | `0` | `ai.maxCostPerDayTotalUsd` | Daily ceiling of the whole installation, in dollars: where many people share one account (a public demo) the per-user ceiling protects nothing. Once reached, the assistant is unavailable until the next day. 0 = none. |
| `AI__MAX_INPUT_TOKENS_PER_TURN` | integer, ≥ 1000 | `400000` | `extraEnv` | Input tokens one turn may send to the model. |
| `AI__DBT_EXPORT_TIMEOUT_SECONDS` | number, > 0 and ≤ 3600 | `180` | `extraEnv` | Seconds for all the AI calls of one dbt export (descriptions and translations). Keep it, plus `ENGINE__DBT_EXPORT_TIMEOUT_SECONDS`, under the ingress' read timeout (300 s in the chart). |
| `AI__MAX_CHATS_LISTED` | integer, 1–500 | `100` | `extraEnv` | Conversations listed per person; older ones are not deleted, only left out of the list. |

### Development sample

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `PREVIEW__DEFAULT_SAMPLE_ROWS` | integer, ≥ 0 | `0` | `app.previewDefaultSampleRows` | Above 0, every source node without a sample of its own is read only for its first N rows in editor previews and trial runs. People switch it off per node; scheduled runs and «Run in production» always read everything. Useful when every read costs (remote storage, a pay-per-use warehouse). 0 = no automatic sample. |

### Orchestration and scheduling

How flows are executed: queued in the database, picked up by an orchestrator process (`APP__ROLE` `all` or `orchestrator`), which waits for each step to complete before the next.

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `ORCHESTRATOR__REFRESH_WAIT_SECONDS` | integer | `600` | `orchestrator.refreshWaitSeconds` | Longest a flow waits for a Refresh node to complete before aborting rather than reading stale data. Raise it for very long refreshes. An Output node has no cap of its own: it is bounded by `ENGINE__RUN_STALE_TIMEOUT_SECONDS`. |
| `ORCHESTRATOR__LOG_RETENTION_DAYS` | integer, ≥ 0 | `30` | `orchestrator.logRetentionDays` | After how many days a run's logs are emptied: what a Script or Container node printed, and the engine's full traceback of a failure. The runs stay, with their outcome, short error, timings and exit code. 0 keeps them for ever. With the three settings below, the orchestrators trim this history every hour and then vacuum the tables they touched. |
| `ORCHESTRATOR__CONTRACT_RESULTS_KEPT` | integer, ≥ 0 | `100` | `orchestrator.contractResultsKept` | The latest data contract reports kept per datasource; older ones are deleted every hour, never one whose notice is still to be sent. 0 keeps them all. |
| `ORCHESTRATOR__VERSIONS_KEPT` | integer, ≥ 0 | `50` | `orchestrator.versionsKept` | The latest versions kept per flow and per saved script; the one in use is always kept. 0 keeps them all. |
| `ORCHESTRATOR__AI_CHAT_RETENTION_DAYS` | integer, ≥ 0 | `90` | `orchestrator.aiChatRetentionDays` | The assistant's conversations untouched for this many days are deleted, with their turns; the spending ledger stays. 0 keeps them. |
| `ORCHESTRATOR__POLL_INTERVAL_SECONDS` | number | `3` | `orchestrator.pollIntervalSeconds` | The longest wait, in seconds, between two checks of a step the flow is waiting for: the first checks come sooner (a quarter of a second, then every second for half a minute). |
| `ORCHESTRATOR__QUEUE_POLL_SECONDS` | number, > 0 | `1` | `extraEnv` | Seconds between two looks at the queue of flow executions. |
| `ORCHESTRATOR__MAX_CONCURRENT` | integer, ≥ 1 | `20` | `orchestrator.maxConcurrent` | Flow executions (and cells of flow groups) one orchestrator process carries at once; the others wait in the queue. Each uses a database connection while it asks the engine about a step: keep it below the pool (`DB__POOL_SIZE`). |
| `ORCHESTRATOR__QUEUE_TIMEOUT_SECONDS` | number, > 0 | `900` | `orchestrator.queueTimeoutSeconds` | An execution still queued after this long (no orchestrator running, or all of them full) is not started any more: it is closed as failed. |
| `ORCHESTRATOR__HEARTBEAT_SECONDS` | number, > 0 | `10` | `extraEnv` | How often the process executing a flow says it is still there. |
| `ORCHESTRATOR__DEAD_AFTER_SECONDS` | number, > 0 | `60` | `orchestrator.deadAfterSeconds` | An execution silent for longer belongs to a process that died: another orchestrator closes it as interrupted. The machines' clocks must agree within this margin. |
| `SCHEDULING__WORKER_CAPACITY` | integer | `2` | `extraEnv` | Flow runs one worker is assumed to carry at once: used only to highlight the crowded minutes in the schedule load heatmap. Match it to the run workers' `CELERY__WORKER_CONCURRENCY`. |

### Database backups

Kubernetes only (on Docker Compose Administration → Backups is greyed out). The automatic backup of the metadata database: an orchestrator runs `pg_dump` on the schedule and streams it to the engine, which stores it in the object storage and removes the oldest beyond the number to keep. `BACKUP__ENABLED`, `BACKUP__SCHEDULE` and `BACKUP__KEEP` are the installation's defaults, which Administration → Backups can change without a restart; where the files go is read by the engine. By default Tabularia's bucket under `backups/postgres/`: in production give the backups a bucket of their own. Restoring one: [Upgrading](deploy/upgrading.md#when-something-goes-wrong).

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `BACKUP__ENABLED` | boolean | `true` | `backup.enabled` | Whether the database is backed up on the schedule. Administration → Backups can turn it off or on; «Back up now» works either way. |
| `BACKUP__SCHEDULE` | string | `0 3 * * *` | `backup.schedule` | When: a five-field cron (`minute hour day month weekday`) in `APP__TIMEZONE`. The process refuses to start with an invalid one. |
| `BACKUP__KEEP` | integer, 1–1000 | `14` | `backup.keep` | How many backups stay; after each backup the oldest beyond this number are removed. |
| `BACKUP__PG_DUMP` | string | `pg_dump` | `extraEnv` | The `pg_dump` used when the image has none of the server's own version. The gateway image has those of PostgreSQL 14 to 18 and uses the server's, so that the server's own `pg_restore` reads the backup; this one (18's) also reads older servers. |
| `BACKUP__TIMEOUT_SECONDS` | integer, ≥ 60 | `3600` | `backup.timeoutSeconds` | The longest a backup (the dump and its upload) may take before it is stopped and reported as failed. |

### OpenLineage

Optional: what every run read and wrote, sent to a catalog in the OpenLineage standard (Marquez, DataHub, Astro…). Off when neither `OPENLINEAGE__URL` nor `OPENLINEAGE__FILE` is set. See [OpenLineage](openlineage.md).

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `OPENLINEAGE__URL` | string | empty | `openlineage.url` | HTTP endpoint of the collector, e.g. `http://marquez:5000`. |
| `OPENLINEAGE__ENDPOINT` | string | `api/v1/lineage` | `openlineage.endpoint` | Path of the lineage endpoint on the collector: `api/v1/lineage` for Marquez and Astro, `openapi/openlineage/api/v1/lineage` for DataHub. |
| `OPENLINEAGE__API_KEY` | string | empty | `secrets.openlineageApiKey` | Bearer token, if the collector asks for one. |
| `OPENLINEAGE__FILE` | string | empty | `openlineage.file` | Also, or instead, write the events as JSON Lines to this file. |
| `OPENLINEAGE__NAMESPACE` | string | `tabularia` | `openlineage.namespace` | Namespace of this installation's jobs. |
| `OPENLINEAGE__TIMEOUT_SECONDS` | number, > 0 | `10` | `openlineage.timeoutSeconds` | How long the collector may take to answer. Events are sent after the run is closed, from a background thread: a collector that is down never fails a run. |
| `OPENLINEAGE_URL` | string | empty | `extraEnv` | The OpenLineage library's own variable: switches lineage on like `OPENLINEAGE__URL`, configured the library's way. |
| `OPENLINEAGE_CONFIG` | string | empty | `extraEnv` | The OpenLineage library's own configuration file (`openlineage.yml`), as an alternative to the variables above. |

### Webhooks and audit log

Webhook receivers are added in Administration → Webhooks ([API](api.md)); forwarding the audit log to a SIEM is set up in Administration → SIEM ([Splunk](siem-splunk.md)). Neither has an environment variable beyond these.

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `WEBHOOKS__RETENTION_DAYS` | integer, ≥ 1 | `30` | `webhooks.retentionDays` | Days the history of webhook calls is kept. |
| `WEBHOOKS__TIMEOUT_SECONDS` | number, > 0 and ≤ 60 | `10` | `webhooks.timeoutSeconds` | How long a webhook receiver may take to answer. |
| `AUDIT__RETENTION_MINUTES` | integer, ≥ 0 | `0` | `app.auditRetentionMinutes` | Audit entries older than this many minutes are deleted, once a minute. 0 = kept forever. Minutes because on a public demo the useful window is an hour (43200 = thirty days). The privacy notice can quote it. |
| `AUDIT__IP_RETENTION_DAYS` | integer, ≥ 0 | `90` | `extraEnv` | Client addresses and browser strings are personal data, kept for this many days: past them they are emptied from audit entries, API tokens and sessions, once an hour, and the entry itself stays (who did what, and when). 0 = kept forever. Deleting an account empties its addresses at once, failed sign-ins with its email included. Independent of `AUDIT__RETENTION_MINUTES`: when that one is shorter, entries go before their addresses would. |

### Monitoring

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `MONITORING__NODE_EXPORTER_URL` | string | `http://node-exporter:9100/metrics` | `monitoring.nodeExporterUrl` | node-exporter the RAM badge and the Monitoring panel read the host's memory from, live. Empty = badge off (the chart deploys no node-exporter). |
| `MONITORING__TIMEOUT_SECONDS` | number | `3` | `extraEnv` | Timeout of that read. |
| `MONITORING__CACHE_SECONDS` | number | `3` | `extraEnv` | Seconds one read is shared by every open page. |

### Worker autoscaling from the panel (Kubernetes)

Lets an administrator change the minimum and maximum number of worker pods from Administration → Workers. Off unless `KUBERNETES__SCALING` is true; the chart sets the whole block, and a service account allowed to read the two worker Deployments and change their autoscalers, with `adminScaling.enabled` ([Deploying on Kubernetes](deploy/kubernetes.md)).

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `KUBERNETES__SCALING` | boolean | `false` | `adminScaling.enabled` | Turns the panel's autoscaling bounds on. |
| `KUBERNETES__CLUSTER_VIEW` | boolean | `false` | `clusterView.enabled` (on by default) | Turns Administration → Cluster on: the pods of Tabularia and of its users' code, their state, CPU and memory (with metrics-server) and the warning events, read-only, for administrators and observers. |
| `KUBERNETES__API_URL` | string | `https://kubernetes.default.svc` | `extraEnv` | Kubernetes API server, from inside the cluster. |
| `KUBERNETES__NAMESPACE` | string | empty | `extraEnv` | Namespace of the worker Deployments. Empty = the pod's own, read from its service account. |
| `KUBERNETES__TOKEN_FILE` | string | `/var/run/secrets/kubernetes.io/serviceaccount/token` | `extraEnv` | Service-account token the gateway authenticates with. |
| `KUBERNETES__CA_FILE` | string | `/var/run/secrets/kubernetes.io/serviceaccount/ca.crt` | `extraEnv` | Certificate authority of the API server. |
| `KUBERNETES__NAMESPACE_FILE` | string | `/var/run/secrets/kubernetes.io/serviceaccount/namespace` | `extraEnv` | Where the pod's namespace is read when `KUBERNETES__NAMESPACE` is empty. |
| `KUBERNETES__RUN_WORKER_DEPLOYMENT` | string | empty | set by the chart with `adminScaling.enabled` | Deployment of the run workers. |
| `KUBERNETES__RUN_WORKER_AUTOSCALER` | string | empty | set by the chart with `adminScaling.enabled` | Their autoscaler, `hpa/<name>` or `scaledobject/<name>` (KEDA). Empty = none. |
| `KUBERNETES__RUN_WORKER_MAX_REPLICAS` | integer, ≥ 1 | `10` | `worker.autoscaling.panelMaxReplicas` (or `maxReplicas`) | The highest maximum of run-worker pods the panel may set. |
| `KUBERNETES__PREVIEW_WORKER_DEPLOYMENT` | string | empty | set by the chart with `adminScaling.enabled` | Deployment of the preview workers. |
| `KUBERNETES__PREVIEW_WORKER_AUTOSCALER` | string | empty | set by the chart with `adminScaling.enabled` | Their autoscaler, as for the run workers. |
| `KUBERNETES__PREVIEW_WORKER_MAX_REPLICAS` | integer, ≥ 1 | `8` | `previewWorker.autoscaling.panelMaxReplicas` (or `maxReplicas`) | The highest maximum of preview-worker pods the panel may set. |
| `KUBERNETES__TIMEOUT_SECONDS` | number, > 0 | `10` | `extraEnv` | Timeout of the calls to the Kubernetes API. |

### Scripting environments (Kubernetes)

Administration → Scripting environments: the Python and R environments the Script node runs in, each built once into an image by a Kubernetes Job (rootless BuildKit) and checked by a second Job. Off unless `SCRIPTS__ENABLED` is true; the chart sets the whole block, the three namespaces and the service accounts of the gateway and the orchestrator allowed to run Jobs there (only the orchestrator's creates the builds), with `scripts.enabled`. The Kubernetes API is reached as in `KUBERNETES__API_URL`, `KUBERNETES__TOKEN_FILE` and `KUBERNETES__CA_FILE` ([Scripting environments](scripting.md)).

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `SCRIPTS__ENABLED` | boolean | `false` | `scripts.enabled` | Turns the scripting environments on: the admin section and the image builds. |
| `SCRIPTS__NAMESPACE` | string | empty | `scripts.namespace` | Namespace where the scripts and the image checks run (Pod Security level `restricted` is enough). |
| `SCRIPTS__TRIAL_NAMESPACE` | string | empty | `scripts.trialNamespace` (default `<namespace>-scripts-dev`), quota `scripts.trialQuota` | Namespace where the editor's «Run» of a Script node runs, apart from the flows' runs and with a ResourceQuota of its own, so trials never take resources from scheduled flows. A trial that does not fit in the quota fails at once, saying so. Empty = `SCRIPTS__NAMESPACE`. |
| `SCRIPTS__BUILD_NAMESPACE` | string | empty | `scripts.buildNamespace` | Namespace where the image builds run. Rootless BuildKit needs its own user namespace (seccomp and AppArmor `Unconfined`), which only the `privileged` Pod Security level admits; its pods still run as a user, without privileges. Empty = `SCRIPTS__NAMESPACE`, which must then be `privileged` too: keep them apart. |
| `SCRIPTS__REGISTRY` | string | empty | `scripts.registry.repository` | Where the environment images are pushed, as `host[:port]/path`: an environment's image is `<registry>/tabularia-env-<id>:v<n>`. The cluster's nodes pull from it. |
| `SCRIPTS__REGISTRY_INSECURE` | boolean | `false` | `scripts.registry.insecure` | The registry speaks plain HTTP or has a self-signed certificate. A test cluster only. |
| `SCRIPTS__REGISTRY_PUSH_SECRET` | string | empty | `scripts.registry.pushSecret` | Name of a Secret of type `kubernetes.io/dockerconfigjson` in the build namespace, used by the builds to push. Empty = a registry that takes pushes without credentials. |
| `SCRIPTS__REGISTRY_PULL_SECRET` | string | empty | `scripts.registry.pullSecret` | Name of a Secret of type `kubernetes.io/dockerconfigjson` in the scripts namespace, used by the Jobs to pull the images. Empty = the nodes pull without credentials. |
| `SCRIPTS__REGISTRY_CA_CONFIGMAP` | string | empty | set by the chart | A ConfigMap of the build namespace holding `ca.crt`: the certificate of the authority that signed the registry's, when it is not a public one (a company CA). The builds verify the registry with it. The chart creates it from `scripts.registry.caCert`. |
| `SCRIPTS__BUILDER_IMAGE` | string | `moby/buildkit:v0.33.1-rootless` | `scripts.builderImage` | The image that builds the environments: BuildKit, rootless. Replace it with a mirror on a cluster without internet access. |
| `SCRIPTS__BASE_IMAGES` | string | empty | `scripts.baseImages` | JSON: the language versions offered, and the image each starts from, e.g. `{"python": {"3.12": "python:3.12-slim"}, "r": {"4.5": "rocker/r-ver:4.5.3"}}`. Empty = Python 3.11, 3.12, 3.13, 3.14 (`python:3.x-slim`) and R 4.5, 4.6 (`rocker/r-ver`). Pin them by digest, or point them at a mirror, here. |
| `SCRIPTS__PYPI_INDEX_URL` | string | empty | `scripts.pypiIndexUrl` | The Python package index the builds install from. Empty = PyPI. |
| `SCRIPTS__CRAN_URL` | string | `https://cran.r-project.org` | `scripts.cranUrl` | The CRAN repository the R builds install from, and the one the scripts' R sees: CRAN itself or one of its mirrors. CRAN publishes sources, so R packages compile during the build; the images carry the system libraries the usual packages need. |
| `SCRIPTS__BUILD_TIMEOUT_SECONDS` | integer, ≥ 60 | `3600` | `extraEnv` | The longest an image build may take before it fails. |
| `SCRIPTS__BUILD_CPU` | string | `2` | `extraEnv` | CPU an environment's image build may use. With SCRIPTS__BUILD_MEMORY it sets how many compilers run at once (one per core at most), which is what makes R packages built from CRAN's sources take minutes or tens of minutes. |
| `SCRIPTS__BUILD_MEMORY` | string | `4Gi` | `extraEnv` | Memory an environment's image build requests and may use: it lands only on a node that has it. One compiler runs per 1.5 GiB (arrow's C++ takes over 1 GB each); a build that needs more fails alone, saying its compiler was killed. Raise it, with SCRIPTS__BUILD_CPU, to build R environments faster on large nodes. |
| `SCRIPTS__CHECK_TIMEOUT_SECONDS` | integer, ≥ 30 | `300` | `extraEnv` | The longest the check of a new image may take: pulling it on a node, starting it, listing its packages. |
| `SCRIPTS__POLL_SECONDS` | number, > 0 | `5` | `extraEnv` | How often the orchestrators look at the builds in progress, and the longest wait between two looks at a Script node's Job (the first come sooner). |
| `SCRIPTS__LOG_MAX_BYTES` | integer, ≥ 4096 | `262144` | `extraEnv` | How much of the end of a build's log is kept and shown in the panel. |
| `SCRIPTS__JOB_TTL_SECONDS` | integer, ≥ 60 | `3600` | `extraEnv` | Kubernetes removes a finished Job after this long, if Tabularia has not removed it first. |
| `SCRIPTS__LINT` | boolean | `true` | `extraEnv` | The code editor underlines what Ruff finds in a Script node's Python code (undefined names, syntax errors, unused imports), and the imports the environment does not have, while it is typed. Each check runs Ruff in the gateway for a few milliseconds, at most twice a second per person. |

### Container registries (Kubernetes)

Administration → Container registries: the registries the images of the Container node come from, their credentials, how to trust their certificate and the repositories allowed ([Container registries](container-registries.md)). Off unless `CONTAINERS__ENABLED` is true; the chart sets it with `containers.enabled`.

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `CONTAINERS__ENABLED` | boolean | `false` | `containers.enabled` | Turns on Administration → Container registries: the registries the images of the Container node come from, on Kubernetes. The Helm chart sets it with `containers.enabled`. |
| `CONTAINERS__NAMESPACE` | string | empty | set by the chart | Where the flows' runs run the Container nodes, and where each registry's pull Secret is kept. The chart sets it (`containers.namespace`, `<release namespace>-containers` by default), at the `baseline` Pod Security level. |
| `CONTAINERS__TRIAL_NAMESPACE` | string | empty | set by the chart | Where the editor's «Run» of a Container node and «Try in the cluster» run, with a ResourceQuota of its own. Empty = `CONTAINERS__NAMESPACE`. The chart sets it (`containers.trialNamespace`). |
| `CONTAINERS__PULL_CHECK_SECONDS` | integer, ≥ 30 | `300` | `extraEnv` | «Try in the cluster»: how long a node may take to download an image before the check says it did not. |
| `CONTAINERS__POLL_SECONDS` | number, > 0 | `3` | `extraEnv` | The longest wait between two looks at a Job of the containers' namespaces while the gateway waits for it (the first come sooner). |
| `CONTAINERS__JOB_TTL_SECONDS` | integer, ≥ 60 | `3600` | `extraEnv` | Kubernetes removes a finished Job of the containers' namespaces after this long, if Tabularia has not removed it first. |
| `CONTAINERS__MAX_CPU` | string | `4` | `containers.limits.cpu` | The most CPU a Container node may ask for (its request and its limit). The chart sets it (`containers.limits.cpu`). |
| `CONTAINERS__MAX_MEMORY` | string | `16Gi` | `containers.limits.memory` | The most memory a Container node may ask for (its request and its limit). The chart sets it (`containers.limits.memory`). |
| `CONTAINERS__MAX_TIMEOUT_SECONDS` | integer, ≥ 60 | `86400` | `containers.limits.timeoutSeconds` | The longest timeout a Container node may have. The chart sets it (`containers.limits.timeoutSeconds`). |
| `CONTAINERS__LOG_MAX_BYTES` | integer, ≥ 4096 | `262144` | `extraEnv` | How much of the end of a container's log the run keeps and the run page shows. |

## Engine and workers

The data plane: in Compose the `backend`, `worker`, `preview-worker` and `beat`
services; on Kubernetes the engine, the two worker Deployments and beat. Same image, same
settings: a variable that concerns only one role says so.

### Engine API and previews

The internal API the gateway calls (never exposed publicly). It hands previews to the `preview` queue and waits for the answer.

Read by both services, so listed under [Shared by the gateway and the engine](#shared-by-the-gateway-and-the-engine): `APP__CORS_ORIGINS`, `APP__ENV_NAME`.

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `APP__ENGINE_API_THREADS` | integer, ≥ 40 | `200` | `extraEnv` | Synchronous requests the engine API serves at once. Every open preview holds a thread while it waits for a worker, so this is how many previews can be in flight; they sleep and cost little. |
| `PREVIEW_TIMEOUT_SECONDS` | number | `120` | `engine.previewTimeoutSeconds` | Longest the engine API waits for a preview worker; then it revokes the job and answers 504. Keep any reverse proxy's read timeout above it. |

### Broker and cache index (Valkey or Redis)

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `REDIS__HOST` | string | `localhost` | `externalServices.redis.host` | Host of the Celery broker and of the step-cache index. |
| `REDIS__PORT` | integer | `6379` | `externalServices.redis.port` | Its port. |
| `REDIS__DB` | integer | `0` | `externalServices.redis.db` | Database number. |
| `REDIS__PASSWORD` | string | empty | `secrets.redisPassword` | Password, when the broker requires one (managed services usually do). |

### Object storage

Any S3-compatible service: MinIO, AWS S3, Cloudflare R2, Backblaze B2, Scaleway, Google Cloud Storage through its S3 API (needed by the BigQuery engine). Every datasource snapshot, cached step and run output lives here.

Read by both services, so listed under [Shared by the gateway and the engine](#shared-by-the-gateway-and-the-engine): `STORAGE__ENDPOINT`.

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `STORAGE__ACCESS_KEY` | string | development value (hidden) | `secrets.storageAccessKey` | **Production:** use credentials of your own (`minioadmin` is the development MinIO's). Access key of the storage. |
| `STORAGE__SECRET_KEY` | string | development value (hidden) | `secrets.storageSecretKey` | **Production:** use credentials of your own. Secret key of the storage. |
| `STORAGE__BUCKET` | string | `data-prep` | `externalServices.storage.bucket` | Bucket of every snapshot, cached step and output. The gateway (`ENGINE__BUCKET`) and the frontend (`NUXT_PUBLIC_BUCKET`) need the same name; the Compose file and the chart derive them from this one. |
| `STORAGE__REGION` | string | `us-east-1` | `externalServices.storage.region` | Region of the storage (`auto` for Google Cloud Storage). |
| `STORAGE__DIRECT_READ` | boolean | `true` | `extraEnv` | The DuckDB and chDB engines read sources and cached steps in place, only the columns and row groups a query needs. `false`: download each file first, for a service that does not serve range reads. |
| `DUCKDB_EXTENSION_DIRECTORY` | string | empty (the image sets `/opt/duckdb-extensions`) | `extraEnv` | Where DuckDB finds its extensions (`httpfs`, for reading the storage in place). The image sets it to the extensions it ships, so nothing is downloaded at runtime. |

### Where the database backups go

The gateway makes the backups of its database (see *Database backups* above); the engine stores them, since it holds the storage credentials. Every field left empty falls back to the object storage's.

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `BACKUP__BUCKET` | string | empty | `backup.bucket` | The bucket of the backups; empty = `STORAGE__BUCKET`. A bucket of their own is advised in production, so that losing one does not lose both. |
| `BACKUP__PREFIX` | string | `backups/postgres/` | `backup.prefix` | The folder of the backups in the bucket. Only files named like a backup (`tabularia-<time>.dump`) are listed, downloaded or removed there. |
| `BACKUP__ENDPOINT` | string | empty | `backup.endpoint` | Another object storage for the backups; empty = `STORAGE__ENDPOINT`. |
| `BACKUP__REGION` | string | empty | `backup.region` | The region of `BACKUP__ENDPOINT`; empty = `STORAGE__REGION`. |
| `BACKUP__ACCESS_KEY` | string | empty | `secrets.backupAccessKey` | Keys of the backups' bucket when they are not the storage's; empty = `STORAGE__ACCESS_KEY` and `STORAGE__SECRET_KEY`. Read by the engine only: the chart keeps them out of the gateway. |
| `BACKUP__SECRET_KEY` | string | empty | `secrets.backupSecretKey` | The secret of `BACKUP__ACCESS_KEY`. |

### Workers and task limits (Celery)

How long a run may take is decided by three coupled values: `CELERY__TASK_TIME_LIMIT` (the worker kills the task), `CELERY__TASK_SOFT_TIME_LIMIT` (the task is told to stop shortly before) and the gateway's `ENGINE__RUN_STALE_TIMEOUT_SECONDS` (the gateway declares a silent run lost), which must stay above the hard limit. To allow longer runs, raise the hard limit and the stale timeout together.

Read by both services, so listed under [Shared by the gateway and the engine](#shared-by-the-gateway-and-the-engine): `CELERY__TASK_TIME_LIMIT`.

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `CELERY__BROKER_URL` | string | empty | `extraEnv` | Broker URL, to put the queue somewhere else entirely. Empty = built from `REDIS__*`. |
| `CELERY__RESULT_BACKEND` | string | empty | `extraEnv` | Result store, likewise. Empty = built from `REDIS__*`. |
| `CELERY__WORKER_CONCURRENCY` | integer | `2` | `worker.concurrency`, `previewWorker.concurrency` | Processes per worker container (each runs one task at a time). The preview workers have their own value (`PREVIEW_WORKER_CONCURRENCY` in Compose). |
| `CELERY__TASK_SOFT_TIME_LIMIT` | integer | `3300` | `engine.taskSoftTimeLimitSeconds` | Seconds after which a task is told to stop, so it can close files and connections and report the failure before the hard limit. Keep it below `CELERY__TASK_TIME_LIMIT`. Also adjustable in Administration → Settings. |
| `CELERY__TASK_SERIALIZER` | string | `json` | `extraEnv` | Format of the task messages. JSON is a security property (no pickled objects): leave it. |
| `CELERY__RESULT_SERIALIZER` | string | `json` | `extraEnv` | Format of the results. Leave it. |
| `CELERY__TIMEZONE` | string | `UTC` | `extraEnv` | Clock of beat's periodic tasks. Leave it on UTC: the gateway converts schedules to UTC before queuing them. |
| `CELERY__ENABLE_UTC` | boolean | `true` | `extraEnv` | Celery's own UTC switch. Leave it. |
| `CELERY__MAX_TASKS_PER_CHILD` | integer | `50` | `extraEnv` | A worker process is replaced after this many tasks, so memory the allocator keeps does not pile up. |
| `CELERY__MAX_MEMORY_PER_CHILD_KB` | integer | empty | `extraEnv` | Resident memory (KB) past which a worker process is replaced at the end of its task. Empty = worked out from the container's memory limit × `CELERY__MAX_MEMORY_HEADROOM_FRAC` ÷ concurrency, never below the floor: set it only to force a fixed value. |
| `CELERY__MAX_MEMORY_HEADROOM_FRAC` | number | `0.75` | `extraEnv` | Share of the container's memory limit given to the worker processes; the rest is margin for the main process and the page cache. |
| `CELERY__MAX_MEMORY_PER_CHILD_FALLBACK_KB` | integer | `1500000` | `extraEnv` | The cap used when the container has no readable memory limit. |
| `CELERY__MAX_MEMORY_PER_CHILD_FLOOR_KB` | integer | `512000` | `extraEnv` | The cap is never below this, so processes are not replaced too often. |
| `WORKERS__RUN_CONCURRENCY` | integer | `0` | `extraEnv` | Processes per run-worker container, resized within seconds without stopping running tasks. 0 = keep `CELERY__WORKER_CONCURRENCY`. Also adjustable in Administration → Workers. |
| `WORKERS__PREVIEW_CONCURRENCY` | integer | `0` | `extraEnv` | The same for the preview workers. Also adjustable in Administration → Workers. |

### Step cache and metrics

The editor keeps the output of intermediate steps, so the next preview starts from there instead of from the source.

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `CACHE__TTL_SECONDS` | integer | `86400` | `cache.ttlSeconds` | A cached step unused for this long is deleted (its parquet and its index entry). Also adjustable in Administration → Settings. |
| `CACHE__SWEEP_INTERVAL_SECONDS` | integer | `3600` | `cache.sweepIntervalSeconds` | How often the cache is swept. |
| `CACHE__MAX_STEP_ROWS` | integer | `1000000` | `extraEnv` | Steps with more rows are not cached: a copy as large as the source saves nothing over re-reading it and costs gigabytes of writes. 0 = no cap. Also adjustable in Administration → Settings. |
| `METRICS__STORAGE_STATS_INTERVAL_SECONDS` | integer | `60` | `extraEnv` | How often the size of the storage prefixes is measured and published as a metric. |

### Embedded engines (chDB, DuckDB, Polars)

The engines that run inside the worker process. See [engine differences](engines/engine-differences.md).

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `CHDB_MAX_MEMORY_USAGE` | integer | `1500000000` | `extraEnv` | Memory budget of the embedded chDB engine, in bytes; keep it under the worker's per-process cap. |
| `CHDB_MAX_BYTES_BEFORE_EXTERNAL_GROUP_BY` | integer | `536870912` (512 MiB) | `extraEnv` | Past this, a chDB GROUP BY spills to disk. |
| `CHDB_MAX_BYTES_BEFORE_EXTERNAL_SORT` | integer | `536870912` (512 MiB) | `extraEnv` | Past this, a chDB ORDER BY spills to disk. |
| `CHDB_MAX_BYTES_IN_JOIN` | integer | `536870912` (512 MiB) | `extraEnv` | Past this, a chDB join fails instead of growing. |
| `ENGINE_MAX_CROSS_JOIN_ROWS` | integer | `50000000` | `extraEnv` | Rows a cross join may produce in a run before it is refused with a clear error instead of exhausting memory. 0 = no cap. |

### External ClickHouse engine

Optional: flows run on a remote ClickHouse server (managed or self-hosted) instead of in the worker. Off while `CLICKHOUSE_EXTERNAL__HOST` is empty. Use the HTTP(S) port (8123 / 8443), not the native one.

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `CLICKHOUSE_EXTERNAL__HOST` | string | empty | `externalServices.clickhouse.host` | Server host. Empty = engine off. |
| `CLICKHOUSE_EXTERNAL__PORT` | integer | `8123` | `externalServices.clickhouse.port` | HTTP(S) port: 8123 plain, 8443 TLS. |
| `CLICKHOUSE_EXTERNAL__USERNAME` | string | `default` | `externalServices.clickhouse.username` | User the engine works as. |
| `CLICKHOUSE_EXTERNAL__PASSWORD` | string | empty | `secrets.clickhousePassword` | Its password. |
| `CLICKHOUSE_EXTERNAL__AI_USERNAME` | string | empty | `externalServices.clickhouse.aiUsername` | A dedicated read-only user for the AI assistant's queries, a second barrier behind the SQL allow-list. Used only when both it and its password are set; otherwise the assistant runs as the user above. Grants: [the AI user](engines/clickhouse-ai-user.md). |
| `CLICKHOUSE_EXTERNAL__AI_PASSWORD` | string | empty | `secrets.clickhouseAiPassword` | Its password. |
| `CLICKHOUSE_EXTERNAL__DATABASE` | string | `default` | `externalServices.clickhouse.database` | Working database; it must exist. In `push` mode it holds the staging tables (the user needs CREATE, INSERT and DROP on it). |
| `CLICKHOUSE_EXTERNAL__SECURE` | boolean | `false` | `externalServices.clickhouse.secure` | TLS; managed services require it (port 8443). |
| `CLICKHOUSE_EXTERNAL__CONNECT_TIMEOUT` | integer | `10` | `externalServices.clickhouse.connectTimeout` | Seconds to wait for the handshake. |
| `CLICKHOUSE_EXTERNAL__TRANSPORT` | string; `s3` or `push` | `s3` | `externalServices.clickhouse.transport` | How the server reaches the data. `s3`: ClickHouse reads and writes the parquet files directly on the storage, nothing passes through the worker (the storage must be reachable from the server). `push`: the worker uploads the source into a staging table and streams the result back; works with any server. |
| `CLICKHOUSE_EXTERNAL__S3_ENDPOINT` | string | empty | `externalServices.clickhouse.s3Endpoint` | The storage endpoint as the server sees it (`s3` mode), e.g. `https://s3.fr-par.scw.cloud`. Empty = `STORAGE__ENDPOINT`, right only when the server is on the same network. |
| `CLICKHOUSE_EXTERNAL__S3_NAMED_COLLECTION` | string | empty | `externalServices.clickhouse.s3NamedCollection` | A named collection created on the server with the storage credentials, so the keys never travel in queries nor reach the `query_log`. |
| `CLICKHOUSE_EXTERNAL__MAX_EXECUTION_TIME` | integer | `0` | `externalServices.clickhouse.maxExecutionTime` | Per-query execution cap in seconds. 0 = none (previews are still bounded by `PREVIEW_TIMEOUT_SECONDS`). Also adjustable in Administration → Settings. |
| `CLICKHOUSE_EXTERNAL__MATERIALIZE_MIN_ROWS` | integer | `0` | `externalServices.clickhouse.materializeMinRows` | `s3` mode: a dataset the viewer reads with more rows than this is copied once into a MergeTree table on the server, 2–6× faster on full scans. 0 = off: the copy is built inside the request and, with large row groups, direct reads are already fast. Also adjustable in Administration → Settings. |
| `CLICKHOUSE_EXTERNAL__MATERIALIZE_DATABASE` | string | empty | `externalServices.clickhouse.materializeDatabase` | Database of those copies. Empty = the working database. |
| `CLICKHOUSE_EXTERNAL__MATERIALIZE_TTL_SECONDS` | integer | `1800` | `externalServices.clickhouse.materializeTtlSeconds` | A copy unused for this long is dropped. Also adjustable in Administration → Settings. |
| `CLICKHOUSE_EXTERNAL__PARQUET_SCAN_MAX_THREADS` | integer | `8` | `externalServices.clickhouse.parquetScanMaxThreads` | On scans (aggregations, pivots, sorts, joins) the engine asks for at least this many threads, to hide the latency of reading from S3; never on LIMIT queries, never below the server's cores. 0 = leave `max_threads` alone. Also adjustable in Administration → Settings. |

### BigQuery engine

Optional: flows run on Google BigQuery, which reads the parquet files from the bucket as temporary external tables, so `STORAGE__ENDPOINT` must be Google Cloud Storage (`https://storage.googleapis.com`, HMAC keys). The service account needs *BigQuery Job User* on the project and read access to the bucket. Off while the project or the credentials are empty.

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `BIGQUERY__PROJECT` | string | empty | `externalServices.bigquery.project` | Google Cloud project the jobs run in. Empty = engine off. |
| `BIGQUERY__CREDENTIALS_FILE` | string | empty | `extraEnv` | Path, inside the container, of the service account's JSON key. Either this or `BIGQUERY__CREDENTIALS_B64`. |
| `BIGQUERY__CREDENTIALS_B64` | string | empty | `secrets.bigqueryCredentialsB64` | The same key in base64 on one line (`base64 -w0 key.json`). |
| `BIGQUERY__LOCATION` | string | empty | `externalServices.bigquery.location` | Region of the jobs: the bucket's, or the multi-region containing it (e.g. `EU`). Empty = read from the bucket. |
| `BIGQUERY__MAXIMUM_BYTES_BILLED` | integer | `21474836480` (20 GiB) | `externalServices.bigquery.maximumBytesBilled` | Bytes billed one query may reach: BigQuery refuses a query that would go over before running it (20 GiB ≈ 0.12 $). 0 = no cap. Also adjustable in Administration → Settings. |
| `BIGQUERY__CACHE_DATASET` | string | `tabularia_cache` | `extraEnv` | Dataset the step cache writes expiring native tables into (created if missing, which needs *BigQuery Data Editor*). Empty = no cache. |

### Imports and downloads

Database imports stream Arrow batches into parquet snapshots; a CSV or Excel download goes through the browser.

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `INGEST__PARQUET_ROW_GROUP_ROWS` | integer, 1–20,000,000 | `1000000` | `extraEnv` | Rows per parquet row group written by imports. Large groups let the engines read only the columns a query needs; the group is held in memory before it is written (about 570 MB per million rows of a 50-column table), so raise the worker memory with it. Also adjustable in Administration → Settings. |
| `INGEST__DB_READ_TIMEOUT_SECONDS` | integer, ≥ 10 | `3000` | `extraEnv` | How long an import from ClickHouse, or a write into it, waits while the server sends nothing (it may sort a whole table before the first row). Keep it below `CELERY__TASK_SOFT_TIME_LIMIT`, so people read this error and not a killed task. Also adjustable in Administration → Settings. |
| `EXPORT__MAX_ROWS` | integer, ≥ 0 | `5000000` | `extraEnv` | Rows of a CSV download from the editor, the viewer or the chat: the browser holds the whole file in memory. Larger results go through an Output node. 0 = no cap. Also adjustable in Administration → Settings. |

### SharePoint datasources

Excel files on SharePoint are read through Microsoft Graph with an app registered on Entra ID; tenant, client and site belong to the connection. These two endpoints are deployment settings on purpose: whoever creates a connection cannot point the backend at another host.

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `SHAREPOINT__GRAPH_BASE` | string | `https://graph.microsoft.com/v1.0` | `extraEnv` | Microsoft Graph endpoint; change it only on a sovereign cloud (US Gov: `https://graph.microsoft.us/v1.0`). |
| `SHAREPOINT__LOGIN_BASE` | string | `https://login.microsoftonline.com` | `extraEnv` | Microsoft sign-in endpoint; likewise (US Gov: `https://login.microsoftonline.us`). |

### SFTP datasources

Caps of the «SFTP folder» datasource, checked on the listed sizes before anything is downloaded: going over one fails the import, naming the file. See [SFTP](sftp.md).

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `SFTP__MAX_FILE_BYTES` | integer, ≥ 1 | `536870912` (512 MiB) | `extraEnv` | Bytes of one file: each one is downloaded to the worker's disk and read in memory to be stacked. |
| `SFTP__MAX_FILES` | integer, ≥ 1 | `500` | `extraEnv` | Files one refresh may read. |
| `SFTP__MAX_TOTAL_BYTES` | integer, ≥ 1 | `2147483648` (2 GiB) | `extraEnv` | Bytes of all the matching files together. |
| `SFTP__TIMEOUT_SECONDS` | number, > 0 | `30` | `extraEnv` | Connection and SSH handshake: a silent server fails within this time. |

### S3 folder datasources

Caps of the «S3 folder» datasource, checked on the object listing before anything is downloaded. See [S3 datasources](s3-datasource.md).

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `S3_SOURCE__MAX_FILE_BYTES` | integer, ≥ 1 | `536870912` (512 MiB) | `extraEnv` | Bytes of one object: downloaded to the worker's disk and read in memory. |
| `S3_SOURCE__MAX_FILES` | integer, ≥ 1 | `500` | `extraEnv` | Files one refresh may read. |
| `S3_SOURCE__MAX_TOTAL_BYTES` | integer, ≥ 1 | `2147483648` (2 GiB) | `extraEnv` | Bytes of all the matching objects together. |
| `S3_SOURCE__MAX_LISTED` | integer, ≥ 1 | `100000` | `extraEnv` | Keys the globs of one refresh may scan, all their expansions together: `**` on a huge bucket stops here. |
| `S3_SOURCE__MAX_REQUEST_SECONDS` | number, > 0 | `600` | `extraEnv` | Wall-clock time of one object's download: a server that drips the bytes stops here. |

### API datasources

Caps and manners of the «API call» datasource. A datasource may lower the per-call, per-refresh and row limits for itself, never raise them above these; going over one fails the refresh with a message that names it, the data is never cut silently. See [API datasources](api-datasource.md).

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `API__TIMEOUT_SECONDS` | number, > 0 | `30` | `extraEnv` | Connection and read timeout of one request, the token request included. |
| `API__MAX_PAGES` | integer, ≥ 1 | `1000` | `extraEnv` | Pages of one call: a pagination that never ends stops here. |
| `API__MAX_CALLS` | integer, ≥ 1 | `500` | `extraEnv` | Calls of one refresh (the combinations of the list variables). |
| `API__MAX_ROWS` | integer, ≥ 1 | `2000000` | `extraEnv` | Rows one refresh may receive. |
| `API__MAX_RESPONSE_BYTES` | integer, ≥ 1 | `104857600` (100 MiB) | `extraEnv` | Bytes of one response, decompressed: it is held in memory to be parsed. |
| `API__MAX_TOTAL_BYTES` | integer, ≥ 1 | `1073741824` (1 GiB) | `extraEnv` | Bytes of all the responses of one refresh. |
| `API__MAX_RETRIES` | integer, ≥ 0 | `4` | `extraEnv` | Retries of a request that failed with 429, 500, 502, 503, 504 or a network error, with exponential backoff or the server's `Retry-After`. |
| `API__MAX_RETRY_WAIT_SECONDS` | number, ≥ 0 | `120` | `extraEnv` | A `Retry-After` longer than this fails the refresh instead of holding a worker. |
| `API__MAX_REDIRECTS` | integer, ≥ 0 | `5` | `extraEnv` | Redirects followed per request. |
| `API__MAX_REQUEST_SECONDS` | number, > 0 | `300` | `extraEnv` | Wall-clock time of one request, from sending it to the last byte of the answer. |
| `API__MIN_INTERVAL_SECONDS` | number, ≥ 0 | `0` | `extraEnv` | Minimum pause between two requests of one refresh; a datasource can ask for more. |

### Placeholders

Dates and lists that move by themselves in the names of inputs and outputs. See [placeholders](placeholders.md).

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `PLACEHOLDERS__MAX_VALUES` | integer, ≥ 1 | `1000` | `extraEnv` | Values of one range variable, and combinations of all the variables of a file datasource; checked when the datasource is saved and at every refresh. An API call is also bounded by `API__MAX_CALLS`. |

### Email

The SMTP connections of email outputs and notices are set up in the app; the deployment decides only how their servers are trusted.

| Variable | Type | Default | Helm value | Description |
|---|---|---|---|---|
| `SMTP__VERIFY_TLS` | boolean | `true` | `extraEnv` | Verify the SMTP server's certificate and name, with STARTTLS and with implicit TLS (465). `false` is the explicit opt-out for a relay with a self-signed certificate: the connection is still encrypted, but to a server nobody vouched for, and it receives the SMTP password. |

## Frontend

The Nuxt server reads these at startup (`NUXT_PUBLIC_…` is Nuxt's own runtime
configuration), so one image serves every environment without a rebuild. The browser
calls the gateway directly; the frontend server never does. The development Compose file
sets them in the `frontend` service itself, not from `.env`.

| Variable | Default | Helm value | Description |
|---|---|---|---|
| `NUXT_PUBLIC_API_BASE` | `http://localhost:8000` | `frontend.publicApiBase` | The gateway's URL as the browser calls it: `/api` when one host serves interface and API, or a full URL on another host. |
| `NUXT_PUBLIC_BUCKET` | `data-prep` | `externalServices.storage.bucket` | Default bucket; must match `STORAGE__BUCKET`. |
| `NUXT_PUBLIC_GRAFANA_URL` | `http://localhost:3001` | `frontend.publicGrafanaUrl` | Grafana as the browser reaches it, embedded in the Monitoring tab (administrators only). Optional. |

## Compose and Helm only

Not read by Tabularia's code: the Compose files and the Helm chart use them to configure
containers.

| Variable | Default | Where | Description |
|---|---|---|---|
| `MINIO_IMAGE` | `pgsty/minio:RELEASE.2026-08-04T00-00-00Z` | Compose (`infrastructure/.env`) | Image of the development MinIO (the official one was withdrawn from the registries). |
| `WITH_MSSQL` | `false` | Compose (build argument) | Build argument: `true` adds Microsoft's SQL Server driver to the backend image you build. Not in the published images, for licensing reasons: see [SQL Server](sqlserver.md). |
| `BACKEND_MEM_LIMIT` | `4g` | Compose (`infrastructure/.env`) | Memory ceiling of the engine API container. |
| `WORKER_MEM_LIMIT` | `6g` | Compose (`infrastructure/.env`) | Memory ceiling of the run worker. Keep the sum of the ceilings below the machine's RAM; the per-process cap is derived from it. |
| `PREVIEW_WORKER_CONCURRENCY` | `4` | Compose (`infrastructure/.env`) | Processes of the preview worker (its `CELERY__WORKER_CONCURRENCY`). The per-process memory cap is the ceiling divided by this. |
| `PREVIEW_WORKER_MEM_LIMIT` | `4g` | Compose (`infrastructure/.env`) | Memory ceiling of the preview worker. |
| `GRAFANA_ADMIN_USER` | `admin` | Compose (`infrastructure/.env`) | Grafana administrator of the development stack. |
| `GRAFANA_ADMIN_PASSWORD` | development value (hidden) | Compose (`infrastructure/.env`) | **Production:** change it wherever Grafana is reachable by others. Its password. |
| `SAMPLES_SCALE` | `small` | Compose (`infrastructure/.env`) | Size of the sample data: `small` (seconds, fits a laptop), `medium`, `robusta` (42M order lines, minutes and a few GB). Generated once. |
| `SAMPLES_ENGINE` | empty | Compose (`infrastructure/.env`) | Engine of the sample flows. Empty = the first one the policy allows. |
| `SAMPLES_RUN_FLOWS` | `true` | Compose (`infrastructure/.env`) | Run the sample flows once at initialisation, so their outputs and views exist. |
| `FORWARDED_ALLOW_IPS` | `127.0.0.1` | Compose (`infrastructure/.env`), read by the gateway's server | uvicorn's own variable: the addresses (comma-separated, networks allowed) from which the gateway believes `X-Forwarded-For`. From anyone else the header is ignored and the client is the connection's address, which is what the sign-in throttle, the sessions and the audit log record. Behind your own reverse proxy put its address here; never `*` while the gateway's port is reachable by others, or anyone can choose the address they sign in from. On Kubernetes the chart sets it from `gateway.trustedProxies`. |
| `MALLOC_ARENA_MAX` | `2` | Compose and Helm (set on the engine containers) | glibc's variable, set to 2 on the engine containers: one memory arena per thread inflates the resident memory of Polars and Arrow thread pools. |

## Changing this page

The page is generated. Names, types, defaults, bounds and Helm keys come from the code:
the settings classes of [`gateway/app/core/config.py`](../gateway/app/core/config.py) and
[`backend/app/core/config.py`](../backend/app/core/config.py), the direct reads of the
environment in both apps, `frontend/nuxt.config.ts`, the development Compose file,
`infrastructure/.env.example` and the chart's templates. The explanations come from
[`.github/scripts/config_reference.toml`](../.github/scripts/config_reference.toml).
After changing either, run `python3 .github/scripts/config_reference.py`;
`--check` (run by the *Configuration reference* workflow, and by
`python3 .github/scripts/test_config_reference.py`) fails while the page and the code
disagree.

<!-- {% endraw %} -->
