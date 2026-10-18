# Release checklist

What to do, in order, when installing or upgrading Tabularia. Written for whoever
runs the deploy; the product changes are in `CHANGELOG.md`.

## Before

1. **Schema.** Database migrations are a step of their own, run before the new
   gateways start: `python -m app.db.migra` from the new gateway image (the Helm
   chart runs it as an init container; in a compose deployment it is a one-shot
   service the gateways wait for). It applies the Alembic revisions in one
   transaction under a Postgres advisory lock, then checks the database against
   the models and rolls everything back on any difference. A gateway refuses to
   start on a database behind its code. Revisions go forward only and are written
   so the previous version keeps working during a rolling update; undoing a
   migration means restoring the backup below. A database from before Alembic
   (until 2026-10) is adopted by the first run, nothing to do by hand. To
   rehearse an upgrade, restore the backup on a scratch database and run the step
   against it, then `python -m app.db.migra controlla`. See
   [database-migrations.md](../database-migrations.md).
2. **Backups.** Take a backup of the Postgres metadata database. Object storage
   is append-only by design (every refresh writes a new snapshot), but the
   metadata is not.
3. **Configuration.** Compare `infrastructure/.env.example` with the deployed
   values; the [configuration reference](../configuration.md) has every variable of the
   release, its default and what must change before production. New in 1.0.0: `INGEST__PARQUET_ROW_GROUP_ROWS` (default 1,000,000 —
   raise the worker memory ceilings with it, they are documented next to it),
   `CLICKHOUSE_EXTERNAL__MATERIALIZE_MIN_ROWS` (now `0` = off),
   `PREVIEW_WORKER_CONCURRENCY` (4), `SHAREPOINT__GRAPH_BASE` /
   `SHAREPOINT__LOGIN_BASE` (only on a sovereign cloud).
   **New in 1.1.0:** `EXPORT__MAX_ROWS` (default 5,000,000 rows per CSV
   download; 0 = no cap). And `JWT__SECRET` must be at least 32 characters in
   production: a shorter one now stops the gateway at startup — generate one with
   `openssl rand -hex 32` before upgrading (everyone signs in again).
   `INGEST__DB_READ_TIMEOUT_SECONDS` (default 3000): how long an import from
   ClickHouse waits for the first rows; keep it below
   `CELERY__TASK_SOFT_TIME_LIMIT`.
   **New in 1.2.0:** `AUTH__SESSION_IDLE_MINUTES` (default 0 = off): minutes
   without activity after which a session closes. Sessions are now rows in the
   metadata database (revision 0002 of the migration step): the upgrade **signs
   everyone out once**, and the interface must keep reaching the gateway on the same
   site (the session is an `HttpOnly` cookie the gateway writes).
   API tokens, service accounts and webhooks (revision 0003): `AUTH__PERSONAL_TOKENS`
   (default `true`), `AUTH__API_TOKEN_MAX_DAYS` (365), `WEBHOOKS__RETENTION_DAYS`
   (30), `WEBHOOKS__TIMEOUT_SECONDS` (10). Webhook calls leave from the
   orchestrators: if your network limits their egress, open it to the receivers.
   SFTP connections (revision 0011: three columns on `connections`, empty for the
   existing ones): `SFTP__MAX_FILE_BYTES` (512 MB per file), `SFTP__MAX_FILES` (500),
   `SFTP__MAX_TOTAL_BYTES` (2 GB per refresh), `SFTP__TIMEOUT_SECONDS` (30) on the
   backend and the workers, whose image now carries paramiko. Workers reach the SFTP servers: open their egress to them — see
   [sftp.md](../sftp.md).
   HTTP API datasources (no schema change: the connection uses the existing columns):
   `API__TIMEOUT_SECONDS` (30), `API__MAX_PAGES` (1000), `API__MAX_CALLS` (500),
   `API__MAX_ROWS` (2 000 000), `API__MAX_RESPONSE_BYTES` (100 MB),
   `API__MAX_TOTAL_BYTES` (1 GB), `API__MAX_RETRIES` (4), `API__MAX_RETRY_WAIT_SECONDS`
   (120), `API__MAX_REDIRECTS` (5), `API__MAX_REQUEST_SECONDS` (300), `API__MIN_INTERVAL_SECONDS` (0) on the backend and
   the workers. Workers call the APIs: open their egress to them — see
   [api-datasource.md](../api-datasource.md).
   Placeholders, ranges and the «S3 folder» datasource (revision 0012: a nullable
   `notes` column on `runs`): `PLACEHOLDERS__MAX_VALUES` (1000 values per range and
   combinations per file datasource), `S3_SOURCE__MAX_FILE_BYTES` (512 MB),
   `S3_SOURCE__MAX_FILES` (500), `S3_SOURCE__MAX_TOTAL_BYTES` (2 GB),
   `S3_SOURCE__MAX_LISTED` (100 000) on the backend and the workers. Workers read the
   buckets of the S3 connections: open their egress to the endpoints — see
   [s3-datasource.md](../s3-datasource.md) and [placeholders.md](../placeholders.md).
   Revision 0013 is a DATA migration: it writes the literal braces of the stored
   definitions (email subject and attachment name, S3 key and copy key, SFTP Output
   folder, SFTP datasource folder and pattern, SharePoint path) as doubled braces, so
   they keep their meaning under the placeholder language; its downgrade is a no-op —
   roll the code back only together with a database backup.
   **Update the workers (and the engine API) before the gateway** this time. A worker
   of the previous version, under the new gateway, gets these wrong:
   - an S3 key or copy key with placeholders is written literally (`exports/{today}/x.csv`);
   - an email subject or attachment name with placeholders is sent literally;
   - the Output node's time zone is ignored (the SFTP file name is resolved in UTC);
   - a templated SFTP Output folder fails («not found»);
   - `mode: append` of a file datasource is ignored: the refresh keeps only the window
     just read (a snapshot of the last days), and `{last_refresh}`/the variables of a
     file datasource are not understood (the refresh fails on the braces).
   Engine settings from the panel (revision 0004): `ENGINE__SETTINGS_PANEL` (default
   `true`; `false` keeps the environment the only source, see
   `docs/engine-settings.md`). Audit log to Splunk (revision 0005): no variable, off
   until an administrator turns it on; the calls to Splunk leave from the
   orchestrators like webhooks (`docs/siem-splunk.md`).
   **The administrator's password has no default any more.** `AUTH__ADMIN_PASSWORD`
   must be set, at least 12 characters and not a published one (`admin`, the guides'
   examples): the gateway refuses to start otherwise, in every environment. An
   administrator still holding `admin` gets the configured password at the next start
   and its sessions close. `AUTH__ALLOW_KNOWN_ADMIN_PASSWORD=true` is for a local
   development stack only and is ignored with `APP__ENV_NAME=production`. New
   passwords follow the same rule (12 characters, published ones refused); existing
   ones keep working. Also new: `UPLOAD__MAX_BYTES` (1 GiB, 0 = none, on the gateway
   and the engine), `AUDIT__IP_RETENTION_DAYS` (90: client addresses are then emptied
   from audit entries, tokens and sessions), `SMTP__VERIFY_TLS` (`true`; `false` only
   for a relay with a self-signed certificate). The client address is the one uvicorn
   resolves: behind a reverse proxy set `FORWARDED_ALLOW_IPS` to the proxy's address
   (the chart's `gateway.trustedProxies`), or every request shows the proxy's.
   Revision 0014 adds a nullable `locale` to `users`: email notices go out in the
   recipient's interface language, English for addresses that are not users.
4. **SQL Server.** The published backend images no longer contain the SQL Server
   driver (since 1.1.1): it is Microsoft's, under Microsoft's licence, and an AGPL
   image cannot carry it. If you have SQL Server connections, build or extend the
   backend image with the driver **before** upgrading, and run that image for the
   engine API, both workers and beat — see [sqlserver.md](../sqlserver.md). Without
   it, those connections and their datasources fail with «the SQL Server driver is
   not installed», and nothing else is affected.

## Rollout order

**From 1.1.x to 1.2.0:** the migration step, then **the workers, beat and the engine
API**, then the gateways and orchestrators, the interface last (revision 0013 above:
an old worker under a new gateway writes placeholders literally). With the compose of
`infrastructure/`: `docker compose up -d backend worker preview-worker beat`, then
`docker compose up -d` (its gateway applies the migrations when it starts). The rule
below, gateway first, is for an upgrade from 1.0.x.

1. **The gateway first (and the orchestrators), then the workers, then the
   engine API.** From this release the engine API answers only requests that
   carry the gateway's internal token: a new engine behind an old gateway
   refuses everything (401) until the gateway is upgraded, while an old engine
   simply ignores the token of a new gateway. Workers before the API, because a
   new API talking to an old preview worker cancels previews with a signal the
   old worker does not clean up after. With compose: `docker compose up -d
   gateway orchestrator`, then `docker compose up -d`; with Helm, see the chart
   README → Upgrades. **The interface last** when upgrading past 1.1.0: it signs
   in with a cookie an older gateway does not read.
2. **Recreate, do not restart, the beat and worker containers** (`docker compose
   up -d --force-recreate` or a rolling replacement of the pods): a restart
   keeps the old code and the old memory limits.
3. **Refresh the large datasources once** after the upgrade so their Parquet
   files are rewritten with the new row groups — and set **sort keys** on the
   columns people filter by (typically a date) while doing it. Until then those
   files keep their old layout and the old speed.

## After

1. Open the sign-in screen in the dark and light themes, sign in, check the
   version in the settings menu (it comes from the gateway, so it is the
   version actually running).
   Open **Your sessions** in the same menu: this browser is listed as «this
   device». Sign out, then press Back: the app must not come back.
2. Open a flow with a large source and click through five or six nodes
   quickly: the preview of the last node should appear, the others should be
   cancelled, nothing should hang.
3. Refresh a datasource, change page and come back: the spinner must still be
   there until the refresh ends.
4. Check the admin **Performance** page and the Grafana dashboard: the preview
   panels fill within a few minutes of use.
5. If the deployment uses the external ClickHouse: confirm no `_mv_…` tables are
   left on it (the sweep runs every 30 minutes and now runs even with
   materialization off).

## Known limits to tell users

- Two people saving the same flow: last save wins. The editor says who else has
  it open; the version history keeps the overwritten work.
- CSV export stops at `EXPORT__MAX_ROWS` (5,000,000 by default). Excel export stops
  at 1,048,575 rows.
- The step cache in the editor materializes every intermediate step; on very
  large sources use the source node's development sample.
- The external ClickHouse has a memory ceiling of its own (7 GiB on the current
  instance): two heavy sorts over a 25M-row, 50-column table at once can hit it
  and fail with a clear error.
