# Spec — every configurable setting in Administration → Settings

Status: implemented (2026-10-23); the guide is docs/engine-settings.md. Owner's decisions in **bold**.

## Goal

Administration → Settings shows every setting an administrator may change without
touching the deployment, grouped by component, and changes them:

- **Family 1, live:** read by the code each time it is used. A change applies within
  seconds on every process (gateway, orchestrator, engine API, workers), with no restart.
- **Family 2, with a rollout:** read once when a process starts. A change is saved at
  once and applies when the components that read it are rolled out; **such settings
  carry an icon**, and the panel offers the rollout (Kubernetes) or names the restart
  (Docker Compose).
- **Family 3, never in the panel:** addresses, credentials, keys, identities, paths,
  namespaces, buckets, switches tied to resources the chart creates, anything that, wrong,
  stops Tabularia from starting or anyone from signing in. **Not shown at all: that is the
  configuration of the values file.**

The environment (`.env`, the Helm values) stays the starting value of every setting; the
panel overrides it and «Back to the installation's value» returns to it.

## Panel groups

One expandable box per component. `↻` = family 2, with the components rolled out.

### Gateway

| Group | Settings |
|---|---|
| Sign-in and sessions | `JWT__ACCESS_TTL_MINUTES`, `AUTH__SESSION_IDLE_MINUTES`, `AUTH__PERSONAL_TOKENS`, `AUTH__API_TOKEN_MAX_DAYS` |
| Single sign-on (shown when SSO is on) | `OIDC__GROUPS_CLAIM`, `OIDC__GROUP_ALLOWLIST`, `OIDC__AUTO_CREATE_GROUPS`, `OIDC__AUTHORITATIVE`, `OIDC__ALLOWED_EMAIL_DOMAINS`, `OIDC__REQUIRE_ALLOWLISTED_GROUP`, `OIDC__BUTTON_LABEL`, `OIDC__DISCOVERY_TTL_SECONDS`, `OIDC__TIMEOUT_SECONDS`, `OIDC__LOGIN_TX_TTL_SECONDS` |
| Orchestration and schedules | `APP__TIMEZONE`, `ENGINE__RUN_STALE_TIMEOUT_SECONDS`, `ORCHESTRATOR__REFRESH_WAIT_SECONDS`, `ORCHESTRATOR__POLL_INTERVAL_SECONDS`, `ORCHESTRATOR__QUEUE_POLL_SECONDS`, `ORCHESTRATOR__MAX_CONCURRENT`, `ORCHESTRATOR__QUEUE_TIMEOUT_SECONDS`, `ORCHESTRATOR__HEARTBEAT_SECONDS`, `ORCHESTRATOR__DEAD_AFTER_SECONDS`, `SCHEDULING__WORKER_CAPACITY` |
| History kept | `ORCHESTRATOR__LOG_RETENTION_DAYS`, `ORCHESTRATOR__CONTRACT_RESULTS_KEPT`, `ORCHESTRATOR__VERSIONS_KEPT`, `ORCHESTRATOR__AI_CHAT_RETENTION_DAYS`, `WEBHOOKS__RETENTION_DAYS` |
| Webhooks and SIEM | `WEBHOOKS__TIMEOUT_SECONDS` |
| AI assistant | `AI__DEFAULT_MODEL`, `AI__MAX_RESULT_ROWS`, `AI__MAX_REQUESTS`, `AI__MAX_FLOW_REQUESTS`, `AI__MAX_HISTORY_MESSAGES`, `AI__MAX_COST_PER_TURN_USD`, `AI__MAX_COST_PER_DAY_USD`, `AI__MAX_COST_PER_DAY_TOTAL_USD`, `AI__MAX_INPUT_TOKENS_PER_TURN`, `AI__MAX_CHATS_LISTED`, `AI__DBT_EXPORT_TIMEOUT_SECONDS` |
| Agents over MCP | `MCP__ENABLED`, `MCP__CONFIRM_IN_TABULARIA_ONLY` |
| dbt export | `ENGINE__DBT_EXPORT_TIMEOUT_SECONDS` |
| Previews and uploads | `PREVIEW__DEFAULT_SAMPLE_ROWS`, `UPLOAD__MAX_BYTES` (gateway and engine) |
| Database backups | `BACKUP__TIMEOUT_SECONDS` (enabled, schedule and number kept: Administration → Backups) |
| Monitoring | `MONITORING__TIMEOUT_SECONDS`, `MONITORING__CACHE_SECONDS` |
| OpenLineage | `OPENLINEAGE__TIMEOUT_SECONDS` |
| Gateway processes ↻ gateway, orchestrator | `APP__GATEWAY_THREADS`, `DB__POOL_SIZE`, `DB__MAX_OVERFLOW` |

### Kubernetes (shown on Kubernetes)

| Group | Settings |
|---|---|
| Kubernetes API | `KUBERNETES__TIMEOUT_SECONDS` |
| Python and R scripts (when scripting is on) | `SCRIPTS__POLL_SECONDS`, `SCRIPTS__JOB_TTL_SECONDS`, `SCRIPTS__LOG_MAX_BYTES`, `SCRIPTS__LINT` |
| Image builds (when scripting is on) | `SCRIPTS__BUILD_TIMEOUT_SECONDS`, `SCRIPTS__BUILD_CPU`, `SCRIPTS__BUILD_MEMORY`, `SCRIPTS__CHECK_TIMEOUT_SECONDS` |
| Notebooks (when scripting is on) | `NOTEBOOKS__MAX_VISITORS_PER_APP`, `NOTEBOOKS__MAX_VISITORS_CEILING` (the first may not exceed the second) |
| Container node (when containers are on) | `CONTAINERS__PULL_CHECK_SECONDS`, `CONTAINERS__POLL_SECONDS`, `CONTAINERS__JOB_TTL_SECONDS`, `CONTAINERS__MAX_CPU`, `CONTAINERS__MAX_MEMORY`, `CONTAINERS__MAX_TIMEOUT_SECONDS`, `CONTAINERS__LOG_MAX_BYTES` |

### Engine

| Group | Settings |
|---|---|
| Workers and runs | `CELERY__TASK_TIME_LIMIT`, `CELERY__TASK_SOFT_TIME_LIMIT`; ↻ worker, preview-worker: `CELERY__MAX_TASKS_PER_CHILD`, `CELERY__MAX_MEMORY_PER_CHILD_KB`, `CELERY__MAX_MEMORY_HEADROOM_FRAC`, `CELERY__MAX_MEMORY_PER_CHILD_FALLBACK_KB`, `CELERY__MAX_MEMORY_PER_CHILD_FLOOR_KB` (processes per worker: Administration → Workers) |
| Step cache | `CACHE__TTL_SECONDS`, `CACHE__MAX_STEP_ROWS`; ↻ beat: `CACHE__SWEEP_INTERVAL_SECONDS`, `METRICS__STORAGE_STATS_INTERVAL_SECONDS` |
| Embedded engines ↻ worker, preview-worker | `CHDB_MAX_MEMORY_USAGE`, `CHDB_MAX_BYTES_BEFORE_EXTERNAL_GROUP_BY`, `CHDB_MAX_BYTES_BEFORE_EXTERNAL_SORT`, `CHDB_MAX_BYTES_IN_JOIN`, `ENGINE_MAX_CROSS_JOIN_ROWS` |
| Engine API ↻ engine | `APP__ENGINE_API_THREADS`, `PREVIEW_TIMEOUT_SECONDS` |
| External ClickHouse (when configured) | `CLICKHOUSE_EXTERNAL__CONNECT_TIMEOUT`, `…__MAX_EXECUTION_TIME`, `…__PARQUET_SCAN_MAX_THREADS`, `…__MATERIALIZE_MIN_ROWS`, `…__MATERIALIZE_TTL_SECONDS` |
| BigQuery (when configured) | `BIGQUERY__MAXIMUM_BYTES_BILLED` |
| Imports | `INGEST__PARQUET_ROW_GROUP_ROWS`, `INGEST__DB_READ_TIMEOUT_SECONDS` |
| Exports | `EXPORT__MAX_ROWS` |
| SFTP | `SFTP__MAX_FILE_BYTES`, `SFTP__MAX_FILES`, `SFTP__MAX_TOTAL_BYTES`, `SFTP__TIMEOUT_SECONDS` |
| S3 folders | `S3_SOURCE__MAX_FILE_BYTES`, `S3_SOURCE__MAX_FILES`, `S3_SOURCE__MAX_TOTAL_BYTES`, `S3_SOURCE__MAX_LISTED`, `S3_SOURCE__MAX_REQUEST_SECONDS` |
| HTTP APIs | `API__TIMEOUT_SECONDS`, `API__MAX_PAGES`, `API__MAX_CALLS`, `API__MAX_ROWS`, `API__MAX_RESPONSE_BYTES`, `API__MAX_TOTAL_BYTES`, `API__MAX_RETRIES`, `API__MAX_RETRY_WAIT_SECONDS`, `API__MAX_REDIRECTS`, `API__MAX_REQUEST_SECONDS`, `API__MIN_INTERVAL_SECONDS` |
| Placeholders | `PLACEHOLDERS__MAX_VALUES` |

## Not in the panel (family 3) — and why, for the borderline ones

Everything else. Borderline ones left out on purpose: `OIDC__SUPERUSER_GROUP` (it makes
administrators), `OIDC__SCOPES`, `OIDC__VERIFY_TLS`, `SMTP__VERIFY_TLS` (security of a
connection), the scripting base images, builder image, PyPI and CRAN addresses (code that
runs in the builds), `CLICKHOUSE_EXTERNAL__TRANSPORT` and `STORAGE__DIRECT_READ` (depend on
the network between services), `KUBERNETES__*_MAX_REPLICAS` (the ceiling the installer puts
on the panel itself), `SCRIPTS__ENABLED`, `CONTAINERS__ENABLED`,
`KUBERNETES__CLUSTER_VIEW`, `KUBERNETES__SCALING` (they need RBAC and namespaces from the
chart), `CELERY__TIMEZONE`, the serializers, `ENGINE__SETTINGS_PANEL` (it governs the panel),
`AUDIT__RETENTION_MINUTES` and `AUDIT__IP_RETENTION_DAYS` (an administrator must not be able
to shorten the trail of what administrators do: the installer decides).

## Mechanism

- **Gateway settings:** table `gateway_settings` (key, JSON value, who, when). Every
  gateway and orchestrator process overlays the valid ones on `get_settings()`, read again
  every 10 s (`app/core/overrides.py`); values frozen into module constants at import
  became reads at the moment of use (orchestrator, runs, blob deletion, OpenLineage and
  JWKS clients keyed by their timeout, the builds' loop).
- **Engine settings:** pushed to the engine as before (Valkey); `engine_settings` gains a
  nullable `value_json` for decimals and yes/no (`value` keeps an integer for the previous
  gateway during a rolling update). The engine's catalogue has types and the family-2
  entries; those are frozen at a process's first reading (`regolazioni.all_avvio`,
  `valore_all_avvio` for the variables read at import such as `CHDB_*`).
- **Validation:** the panel's ranges, the settings classes' own validators (gateway), and
  the constraints: heartbeat × 3 ≤ dead-after, executions per orchestrator ≤ pool +
  overflow, notebooks' visitors per app ≤ their ceiling, soft < hard time limit, read timeout < soft, hard + 5 min ≤ run stale (checked
  from both sides).
- **Rollout:** table `settings_rollouts` (component, changed_at, requested_at). On
  Kubernetes the orchestrator evicts the pods started before the request, one at a time,
  waiting for readiness (`services/settings_rollout.py`); the chart grants pods get/list
  and pods/eviction create only (`settingsRollout.enabled`). Not a patch of the
  Deployments: that right would let a compromised gateway change their image or account.
  On Compose, or without the chart's right, the panel names what to restart and the
  administrator says when it is done.
- **Audit:** `settings.update`, `engine_settings.update`, `settings.rollout`,
  `settings.rollout_cancel`.
- **Off switch:** `ENGINE__SETTINGS_PANEL=false` freezes both panels (shown, not applied).

## Tests

Unit tests per mechanism and per converted constant (gateway: test_panel_overrides,
test_gateway_settings_api, test_settings_rollout; engine: test_panel_settings); on minikube
and kind: a family-1 change seen by every replica within seconds, a family-2 change applied
after the rollout and not before, a pod started later; UI in Chrome and Firefox.

## Edge cases

Listed, each with its test, in docs/engine-settings.md → «Edge cases tried».
