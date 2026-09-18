# Release checklist

What to do, in order, when installing or upgrading Tabularia. Written for whoever
runs the deploy; the product changes are in `CHANGELOG.md`.

## Before

1. **Schema.** The gateway applies its lightweight migrations itself at startup
   (`create_all` plus idempotent `ALTER … ADD COLUMN IF NOT EXISTS`). It is a
   singleton with a `Recreate` strategy, so two versions never migrate at once.
   To rehearse an upgrade, run the previous version's `init_db` on a scratch
   database and then the new one; both must succeed and the second must be a
   no-op the second time.
2. **Backups.** Take a backup of the Postgres metadata database. Object storage
   is append-only by design (every refresh writes a new snapshot), but the
   metadata is not.
3. **Configuration.** Compare `infrastructure/.env.example` with the deployed
   values. New in 1.0.0: `INGEST__PARQUET_ROW_GROUP_ROWS` (default 1,000,000 —
   raise the worker memory ceilings with it, they are documented next to it),
   `CLICKHOUSE_EXTERNAL__MATERIALIZE_MIN_ROWS` (now `0` = off),
   `PREVIEW_WORKER_CONCURRENCY` (4), `SHAREPOINT__GRAPH_BASE` /
   `SHAREPOINT__LOGIN_BASE` (only on a sovereign cloud).

## Rollout order

1. **Workers first, then the API, then the gateway.** A new API talking to an
   old preview worker cancels previews with a signal the old worker does not
   clean up after; it recovers only when the running query ends.
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
