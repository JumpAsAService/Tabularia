# Changelog

All notable changes to Tabularia. The version shown here is the one the gateway
exposes at `/system/info` and in the app's settings menu.

## Unreleased

- **The assistant can answer across two datasources.** It could only ever query
  one, because the SQL node may read nothing but its own input — so a question
  spanning two tables got two separate queries and a total the model added up in
  its head, which is how a confident wrong number happens. It now joins them:
  the model names the second datasource by **id**, never a bucket or a key, and
  the gateway resolves it under the same permissions as everything else, builds
  the join as a flow operation, and runs the SQL on the joined result. The
  allow-list is untouched — the query still sees only `self` — so the capability
  costs nothing in containment: a datasource the caller cannot read cannot be
  joined, and the engine is not even called.
- **A datasource says when it is ready for the assistant.** A table whose fields
  carry no descriptions forces the model to guess from column names, which is
  exactly how it once invented the contents of a table it had never read. The
  catalog now marks the ones that are complete — the table has a description and
  so does every one of its fields — with the same symbol the assistant carries
  in the navigation, so the connection reads without explanation. Short of that,
  the existing counter shows how far along it is, which is the part that invites
  finishing. The rule lives on the server: three surfaces show it, and written
  three times it would drift on the first change.
- **Be told when a scheduled flow fails.** "Schedule it and forget" only works
  if something wakes you: a flow that broke at three in the morning stayed
  broken in silence until someone opened the run history or noticed the data was
  stale. A flow can now carry a list of addresses and an SMTP connection, set
  where the schedule is set, because it is the other half of the same decision.

  Three rules, each of them a choice rather than an omission. Only scheduled
  runs notify — whoever presses Run is already watching the screen, and noise
  teaches people to ignore the real ones. Only the *transition* into failure
  notifies: a broken flow firing every five minutes would send three hundred
  mails a day and by the third nobody reads them, so it speaks again only after
  a run has succeeded in between. And a notice that cannot be sent never changes
  anything — the run has already failed, and an error there would only make it
  harder to understand.

  The addresses go through the same barrier as the email output node: a
  connection that restricts its domains restricts these too, and configuring one
  needs CONNECT on the connection, so a person with only RUN on a flow cannot
  send from someone else's mail server.
- **Conversations with the assistant are saved, and each turn shows what it
  cost.** The history now lives in the gateway database instead of the browser:
  conversations survive a closed tab, and reopening one costs nothing — the
  answers are already there. The chat page lists your own conversations, reopens
  them with their steps and results, and lets you delete them. Under every
  answer, and on the conversation as a whole, is the cost of the call. The
  figure comes from the price data shipped with `pydantic-ai` — we keep no price
  list of our own to go stale. That lookup is scoped to the *provider*, which
  for an OpenAI-compatible endpoint only resolves models that exist at OpenAI
  too, so when it comes back empty we look the same data up by model name. It
  is an estimate from the model's list price, not your provider's invoice, and
  the tooltip says so; `—` means the model could not be priced at all, which is
  not the same as free.
  Two security consequences: the client no longer sends the history, so it can
  no longer forge one (an injected `system` part used to reach the model
  verbatim), and a turn is now capped by cost and input tokens, not only by the
  number of model round-trips. New tables `ai_chats` and `ai_chat_turns`; new
  settings `AI__MAX_COST_PER_TURN_USD` and `AI__MAX_INPUT_TOKENS_PER_TURN`.
  Conversations are private to their author and are deleted with the account.
  The assistant names each conversation itself from the first question, so the
  list reads like a set of topics rather than truncated sentences; a search box
  filters it. While the assistant works, the three-dot placeholder is replaced
  by a status line — a pulsing dot and a sentence naming the phase it is in
  (scanning the catalog, reading the fields of X, querying X, writing the
  answer). The phases are read from the same stream events the steps are read
  from: nothing there is invented. A conversation is created once its first
  turn completes, so an interrupted request no longer leaves an empty one.
- **Security fixes from the 2026-09-19 audit.** Moving a folder now requires
  MANAGE on the folder being moved, not EDIT: with EDIT alone it granted MANAGE
  (and therefore CONNECT) over the whole moved subtree. A folder can no longer
  be moved inside its own subtree. Scheduled datasource refreshes re-check RUN
  and CONNECT every time they fire and switch themselves off when the
  permissions are gone, instead of running forever with the schedule author's
  authority; moving a datasource drops someone else's schedule. The storage
  credentials are stripped from engine error messages, which could carry them
  in clear text out of a ClickHouse syntax error — into a toast, into
  `Run.error_detail` and on to the model provider. The scatter tooltip escapes
  cell values and column names, which reached `innerHTML` unescaped. The SQL
  node's allow-list no longer trusts how the parser labels a node: a dotted
  reference in `FROM`/`JOIN` position is refused whatever its type, closing a
  bypass through `ARRAY JOIN` that read other tables of the server.
  The Ingress now routes every prefix the gateway serves — `/ai`, `/search`,
  `/saved-views`, `/engine-policy` and `/admin/performance` were missing, so on
  Kubernetes those calls fell through to the frontend and got its 404 page.
  Development stack only: published ports are bound to localhost and Nuxt
  DevTools are off — its RPC is unauthenticated and the container has the
  sources mounted writable.
- **A dedicated ClickHouse user for the AI assistant (optional).**
  `CLICKHOUSE_EXTERNAL__AI_USERNAME` / `CLICKHOUSE_EXTERNAL__AI_PASSWORD` (Helm:
  `externalServices.clickhouse.aiUsername`, `secrets.clickhouseAiPassword`): when
  both are set and the transport is `s3`, the assistant's queries run on the
  external ClickHouse as that user instead of the engine's account — a second
  barrier behind the SQL node allow-list. With `readonly = 2` and only
  `CREATE TEMPORARY TABLE, S3` it reads the parquet files and nothing else: no
  `system`, no tables, no `url()`/`file()`. Flows, runs, the Viewer and the
  editor previews keep the main account. Unset = exactly as before. Grants,
  what was verified and the one limit (the `S3` grant also covers writes on
  these versions): `docs/engines/clickhouse-ai-user.md`.
- **Security: the SQL node can only read its own input (allow-list).** On the
  engines that run the query on a real server (external ClickHouse, chDB,
  BigQuery) the free-form SQL node was protected by a deny-list of dangerous
  table functions. A live test of the AI assistant showed it was not enough:
  `merge('db', 'regex')` was not on the list and read another table of the
  server through the node, and `information_schema` listed the tables; on
  BigQuery an unquoted `dataset.table` passed too. The node now parses the query
  (sqlglot, scope-aware) and accepts only `self`/`input`, CTEs in their own
  scope and row generators (`numbers`, `UNNEST`, …); `x IN table`, `dictGet*`,
  `joinGet`, `getSetting` are refused, and a query that cannot be parsed is
  refused rather than waved through. This matters for RBAC: the engine reads
  everything, so a query able to name another table would make the permission
  on the datasource decorative — for flows and, since the assistant, for anyone
  with VIEW. DuckDB (locked in-memory sandbox) and Polars were not affected.
- **Administrator groups, and promotion from the admin page.** A group can be
  marked as an *administrators group*: every member is an administrator for as
  long as they belong to it, and stops being one on leaving. With SSO the
  membership follows the identity provider, so admin rights can be granted from
  there without `OIDC__SUPERUSER_GROUP`. The Users table gains a promote/demote
  switch for the personal flag and shows when someone is admin *through* a
  group. Every path is audited (`user.promote/demote`, `group.promote/demote`,
  `group.admin_join/admin_leave`) and none lets an administrator lock themselves
  out: demoting or deactivating yourself, demoting or deleting your only admin
  group, or leaving it answers 409. New column `groups.is_admin`, added at
  gateway start-up. `/auth/me` reports the effective role.
- **AI assistant (optional).** A chat over the catalog, open to every user,
  built with pydantic-ai against any OpenAI-compatible endpoint (`AI__BASE_URL`,
  `AI__ACCESS_KEY`, `AI__SECRET_KEY`; verified on Scaleway Generative APIs).
  The assistant lists, describes and queries only the datasources the user can
  read; field descriptions reach the model with the columns; queries are one
  read-only `SELECT … FROM self` run by the engine through the `sql` node, with
  capped rows, and each one is written to the audit log (`ai.query`). The page
  shows the steps, the SQL and the result table of every query next to the
  answer. Administrators enable the models in Admin → AI models: none is
  usable by default. The conversation is not stored on the server.
- **Datasource description.** Next to the field descriptions, the Datasources
  page now edits a description of the datasource itself (what it contains,
  what one row is, period, caveats; up to 4,000 characters), saved with the
  same button. The assistant gets a short version when listing the catalog and
  the full text when describing a datasource, and its catalog search matches
  words rather than exact substrings (`anagrafica di test` finds
  `anagrafica_test`), falling back to the full list instead of an empty one.

- **Step-cache written after the answer, and capped.** Every engine used to
  materialise the previous step *inside* the preview: on a 25M-row table the
  external ClickHouse copied 2.4 GB to the bucket at every click (40 s), and
  the next click cancelled and restarted it. The preview now answers after its
  own query and hands the steps to a separate task, which writes each one once
  (a lock on Valkey). Steps above `CACHE__MAX_STEP_ROWS` (default 1M) are not
  cached at all and are remembered as such: a copy as large as the source
  gains nothing over the original parquet. The final output of a run is cached
  under the same cap. The preview reports the cache state of the upstream step
  (`cache_state`, `cache_cap_rows`); the editor shows a mark on the node and a
  hint in its panel when that step is recomputed at every preview, plus one
  warning toast per node and session.
- **Flow editor.** Edges can be removed: a «×» on hover or selection, and
  Delete/Backspace on a selected edge. Operation and Output nodes show the row
  and column counts known from their last preview ("100+" when the preview was
  full), at no extra query.

## Unreleased

- **BigQuery engine (optional).** A fifth engine, `bigquery`, runs the
  transformations on Google BigQuery: the parquet files stay in the bucket and
  are read as temporary external tables, results come back through the Storage
  Read API and are written as parquet like the other engines. Needs Google
  Cloud Storage as the object storage and a service account
  (`BIGQUERY__PROJECT`, `BIGQUERY__CREDENTIALS_FILE` or `_B64`). Every query is
  capped by `BIGQUERY__MAXIMUM_BYTES_BILLED` and labelled with the preview tag,
  so a superseded preview is cancelled on the service. Native step-cache: the
  intermediate steps of the editor are written as tables with an expiration in
  the `BIGQUERY__CACHE_DATASET` dataset (default `tabularia_cache`, created by
  the engine; needs "BigQuery Data Editor") by a deferred task, so the preview
  never waits for them and the next clicks read a small native table. Median
  in pivot and `foreach` are not supported on this engine.
- **Google Cloud Storage as object storage.** Works through the S3-compatible
  XML API with HMAC keys. Batch deletes, which GCS does not implement, fall back
  to single deletes; deleting a missing object is not an error on any storage.

- **Automatic development sample.** With `PREVIEW__DEFAULT_SAMPLE_ROWS` > 0
  (Helm: `app.previewDefaultSampleRows`), every source node without a sample
  of its own is read only for its first N rows in editor previews and trial
  runs. The node shows an "automatic sample" badge; users switch it off per
  node with "off (all rows)" or pick their own sample. Scheduled runs and
  "Run in production" are never sampled. Off by default. Meant for
  deployments where every read is billed (remote object storage, pay-per-use
  warehouses).

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
