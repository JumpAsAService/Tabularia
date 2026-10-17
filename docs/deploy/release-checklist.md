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
   values. New in 1.0.0: `INGEST__PARQUET_ROW_GROUP_ROWS` (default 1,000,000 —
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
   **New in 1.1.1: SQL Server.** The published backend images no longer contain the
   SQL Server driver: it is Microsoft's, under Microsoft's licence, and an AGPL image
   cannot carry it. If you have SQL Server connections, build or extend the backend
   image with the driver **before** upgrading, and run that image for the engine API,
   both workers and beat — see [sqlserver.md](../sqlserver.md). Without it, those
   connections and their datasources fail with «the SQL Server driver is not
   installed», and nothing else is affected. Nothing else changes from 1.1.0.

## Rollout order

1. **The gateway first (and the orchestrators), then the workers, then the
   engine API.** From this release the engine API answers only requests that
   carry the gateway's internal token: a new engine behind an old gateway
   refuses everything (401) until the gateway is upgraded, while an old engine
   simply ignores the token of a new gateway. Workers before the API, because a
   new API talking to an old preview worker cancels previews with a signal the
   old worker does not clean up after. With compose: `docker compose up -d
   gateway orchestrator`, then `docker compose up -d`; with Helm, see the chart
   README → Upgrades.
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
- CSV export has no row cap. Excel export stops at 1,048,575 rows.
- The step cache in the editor materializes every intermediate step; on very
  large sources use the source node's development sample.
- Sort keys cannot be edited after import; re-import to change them.
- The external ClickHouse has a memory ceiling of its own (7 GiB on the current
  instance): two heavy sorts over a 25M-row, 50-column table at once can hit it
  and fail with a clear error.
