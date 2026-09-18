# Changelog

All notable changes to Tabularia. The version shown here is the one the gateway
exposes at `/system/info` and in the app's settings menu.

## 1.0.0 «Appio» — 2026-09-18

First release, named after Appius Claudius Caecus, the censor of 312 BC who
rewrote the citizen rolls and built Rome's first aqueduct: counting,
classifying and cleaning the registers, and the first pipeline. Everything below is what the product does today; see
`docs/deploy/release-checklist.md` for what to do when installing it and for
the known limits.

### What it is
- A visual data-preparation tool: connect a source, build a flow of
  transformation nodes on a canvas, preview every step, publish the result as a
  datasource or write it to a database, S3 or email, schedule it.
- The same flow runs on four interchangeable engines — Polars, DuckDB, chDB and
  an external ClickHouse — with one verified SQL semantics across them (a
  cross-engine correctness suite runs all four on the same "dirty" data).
- A read-only Viewer with pivot, charts and saved views; lineage; a dbt export
  of a flow; an admin area with users, groups, per-project permissions, audit
  log, queue, engine policy, monitoring and performance.

### Sources and destinations
- Files (CSV, TSV, JSON, NDJSON, Excel, Parquet) and databases (PostgreSQL,
  MySQL/MariaDB, ClickHouse, Trino) as sources, with manual and scheduled
  refresh, upstream filters, development sampling and sort keys on import.
- **New in this release:** Excel files on SharePoint as a datasource — a site
  (connection) plus a path in its document library, globs allowed, and a
  sheet; several matching files are stacked into one table with a `_file`
  column. Files must be machine readable (header in the first row, one name
  per column); anything else is rejected naming the file. Not yet exercised
  against a real Microsoft tenant.
- Destinations: datasource, database table (append/replace with post-SQL),
  S3 object or dataset, email attachment; an S3 mirror of the output.

### Access and operations
- Gateway with JWT login and optional OIDC single sign-on (Keycloak, Entra);
  RBAC on users, groups and projects; audit log of logins, changes, runs,
  exports and permissions.
- Helm chart for Kubernetes; Docker Compose for development; Prometheus-style
  metrics scraped by VictoriaMetrics and dashboards in Grafana.

### Notable improvements landed just before the release
- Previews on large tables: Parquet files are now written in row groups of one
  million rows (aggregations on a 25M-row table went from 18.7 s to 0.6 s), the
  ClickHouse client is reused per worker thread, and the synchronous
  materialized-copy feature is off by default.
- A newer preview takes down the one it replaces: clicking through nodes no
  longer queues work nobody will read (eight rapid clicks: the wanted preview
  in 25 s instead of 79 s, and other users are no longer blocked behind it).
- "Also open by …" notice in the flow editor; the datasource refresh spinner is
  driven by the server and survives reloads and scheduled refreshes.
- Timestamps reach the browser with their UTC offset (run times were shown
  shifted by the client's timezone).
- New sign-in screen and a vector logo that follows the theme.
- Admin Performance page: slowest flows, refreshes and previews, queue wait,
  heaviest warehouse queries — with no new table behind it.

### Known limits
- Saving a flow that two people have open is last-write-wins; the editor warns
  who else has it open, and the version history keeps the overwritten work.
- CSV export has no row cap (Excel export stops at the format's 1,048,575 rows).
- In the flow editor, every intermediate step of a preview is materialized to
  the step cache; on very large sources use the source node's development
  sample, or each step costs a full write.
- Sort keys on a datasource cannot be edited after import (re-import to change
  them).
- The SharePoint connector, the new sign-in screen and the admin Performance
  page were verified through the API and headless renders, not clicked
  through in a browser.
