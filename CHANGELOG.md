# Changelog

All notable changes to Tabularia. The version shown here is the one the gateway
exposes at `/system/info` and in the app's settings menu.

## Unreleased

- **DuckDB and chDB read their sources in place.** Both engines downloaded every
  source and every cached step whole before reading it, even for a preview of 100
  rows of a few columns. They now read the object storage by ranges, only the columns
  and row groups a query needs: on a 541 MB parquet a filtered DuckDB preview went
  from 1.3 s to 0.13 s with the storage on the same machine, and the gap grows with a
  remote one. DuckDB uses its httpfs extension, shipped in the image (installed at
  build time; workers never download code), with the credentials as a per-connection
  temporary secret; chDB's `s3()` takes them from the process environment, so no
  query carries them. Execute-SQL and computed columns still cannot reach the
  storage. `STORAGE__DIRECT_READ=false` goes back to downloading.
- **Fixed: ClickHouse destinations wrote by position.** An Output appending to a
  ClickHouse table whose columns were in another order put values in the wrong
  columns without any error, and "replace" swapped in a plain MergeTree built from
  the flow, losing the customer's engine, ORDER BY, partitions, TTL and DEFAULT
  columns. Columns are now matched by name, as on the other databases; a column the
  table lacks stops the write with a message naming it, columns the flow lacks take
  their DEFAULT, and "replace" loads a copy of the table's own definition before the
  atomic swap. Tested against a real ClickHouse.
- **DuckDB works within its share of the worker.** Each connection gets the memory of
  one worker task (the container limit × 0.75 / concurrency, minus room for Python)
  and the CPUs divided the same way, and spills to disk past it instead of growing
  until the kernel kills the task. Each connection has its own spill directory:
  sharing one, two tasks spilling at once read each other's files and returned wrong
  sums. The Execute-SQL node's sandbox now lives on disk under the same budget.
- **Request bodies are capped at 8 MB before they are read** (uploads excepted): one
  user saving a flow of a hundred megabytes could take a gateway replica down.
- **A flow waits for each Output to really end.** The ten-minute cap counted the time
  in the worker queue too; on expiry the flow moved on, so later steps read data not
  written yet and the flow could be started again while the output was writing. The
  wait is now bounded only by the run's own lifetime (`ENGINE__RUN_STALE_TIMEOUT_SECONDS`);
  `ORCHESTRATOR__OUTPUT_WAIT_SECONDS` is gone (an existing value is ignored).
- **Lineage starts from an object.** The page loads nothing until a flow, a datasource
  or a connection is searched and applied (the whole graph stays one click away), and a
  node's downstream impact is asked for when its panel opens. Each flow's latest run is
  read with two aggregate queries on a new index (`runs(flow_id, started_at)`, schema
  revision 0007) instead of loading every run: with a year of history the page took
  seconds and hundreds of megabytes per call.
- **Fixed: deletes that answered 500 on Postgres.** A flow whose runs had started
  imports or sub-flow runs can be deleted (those runs stay, detached); deleting a
  datasource used by saved views, or a connection that sends the failure notices of
  flows, now says which ones block it.
- **Saving a flow checks the datasources it names by id.** A flow can read a
  datasource by its id alone, and a schedule runs with its author's rights: with
  EDIT on a folder, a user could make an admin's nightly run copy a datasource they
  cannot read. Whoever saves a flow must now be able to read every datasource it
  names (a datasource deleted since does not block the save).
- **Exports are written by a worker.** The file used to be built inside the API
  process, a whole Excel file in memory (up to a million rows), next to every other
  request. A worker now writes it and leaves it on the storage; the browser follows
  the export and then saves the file as it arrives, instead of holding it in memory,
  and the file is deleted as soon as it has been sent (one never taken is swept
  after an hour). Only the user who started an export sees its state and its file.
  Exports now share the workers' queue with runs. `POST /tasks/export` keeps its
  contract for API callers (the file in the response); the browser uses the new
  `POST /tasks/exports`, `GET /tasks/exports/{id}` and `GET /tasks/exports/{id}/file`.
- **Fixed: filter and fill values lost their column's type.** The form read every
  typed value as JSON: on a text column of codes "20121" became the number 20121
  (Polars refused the comparison, DuckDB cast the whole column and matched "0144"
  with "144"), and an empty box became null and kept zero rows without a word. Values
  are now read as their column stores them (`""` means the empty string), and every
  engine refuses a filter left without a value, saying which.
- **Fixed: `EXTRACT(YEAR FROM d)` on DuckDB and BigQuery.** Their computed columns
  still refused any expression with the word FROM, so 4 of the 11 Sample flows could
  not run on duckdb; they now use the same parsing check as ClickHouse.
- **Fixed: wrong totals in DuckDB previews after an SQL node.** To spare work, the
  preview cut the SQL node's result to the rows it shows, even when other operations
  followed: a group_by after it counted a sample (the Sample RFM flow showed 37
  groups instead of 58). It now cuts only when the SQL node is the last operation.
- **Gateway: permissions computed once per request.** Each check reloaded the whole
  folder tree and every grant of every user, several times per request (a preview by
  a non-admin cost 0.25 s at 2,000 folders). The tree is read as (id, parent) pairs,
  grants only for the user and their groups, and the answers are reused within the
  request until something is written.
- **Run history from indexes** (schema revision 0008): a datasource's refreshes, the
  "importing…" flag of the datasource lists and the history page read indexes on
  `runs` instead of the whole table; the Flows page's calendar is counted by the
  database instead of loading every run of 180 days, and each flow's latest run is a
  per-flow lookup.
- **Polls no longer hold a database connection while the engine answers.** The run,
  flow-runs and datasource-refresh polls waited up to 30 s for the engine with their
  transaction open; with enough of them the next request froze the replica. They now
  give the connection back before waiting and check permissions in a worker thread.
- **Fixed: editing a connection could blank its database and schema.** The Connections
  page and the Explore folder reuse one dialog; opening a connection after a SharePoint
  one (or the reverse) fired the "type changed" reset, the form showed database and
  schema (or tenant and library) empty, and Save stored them so. The reset now
  happens only when the user changes the type, and an edit sends only the fields it
  changed.
- **Fixed: the parameter form after Undo, Redo or a change of operation type.** It was
  rebuilt only for another node, so it kept showing values the node no longer had,
  and the next edit wrote them back. It now follows the node's parameters.
- **A ClickHouse post-SQL that fails says the data is already written.** ClickHouse
  cannot undo the load, and a run that said only "failed" was run again, appending
  every row a second time. The message now gives the rows written and whether running
  the flow again is safe (replace) or would duplicate them (append).
- **Timestamps written in UTC.** ClickHouse columns created by an Output are
  `DateTime64(6, 'UTC')`, so a server in another time zone shows and filters the
  values Tabularia shows; MySQL writes with the session in UTC, so a `TIMESTAMP`
  column stores the right instant (it was one or two hours off).

- **Flow groups**: several flows and database refreshes run as one, in order and in
  parallel. A group is a grid: each row is a step, the cells of a row start together,
  and the next step starts when every cell of the previous one has succeeded; the first
  failed step stops the group and the steps after it are skipped. Cells are filled by
  dragging flows and database datasources from a panel (searchable by name or folder)
  or by choosing them in an empty cell, which also works from the keyboard; the same
  object twice in one step is refused. A group lives in a folder like a flow (Explore,
  global search, move, delete, audit), is run by hand or on a cron schedule, and its
  page shows the state of every cell in the latest run and in the earlier ones. Its
  flows run on their production engines, as a scheduled run does. A flow already
  running when its cell starts is never started a second time: the cell waits for
  it, and its outcome counts when it runs in production and started after the
  previous step ended; otherwise (a development run, or one that may have read data
  older than the earlier steps) the cell lets it end and then runs the flow. While
  the group runs a flow, the flow's own schedule cannot start it again. Cells count
  in the orchestrator's `ORCHESTRATOR__MAX_CONCURRENT` like flow runs. Rights: VIEW
  reads a group, EDIT changes it, RUN runs and schedules it; every cell checks again,
  with the rights of whoever runs the group, when it runs, and a schedule turns
  itself off when its author is deactivated or loses RUN. A cell whose flow or
  datasource the reader cannot read shows its state but not its error. Endpoints
  under `/flow-groups`; the flows inside send their own `flow.run.*` webhooks.
  Schema revision 0006.
- **Fixed: deleting an account answered 500 on Postgres** when the account had saved
  a flow version, a saved view or a banner, changed an AI model, the engine policy or
  the privacy notice, or set a flow's or a datasource's schedule: those references
  are now cleared first, as the others already were. A schedule left without its
  author turns itself off the next time it is due.
- **Audit log to Splunk** (Administration → SIEM, described in `docs/siem-splunk.md`):
  every audit entry goes to Splunk through the HTTP Event Collector within about
  fifteen seconds, one event per entry with CIM-friendly `user` and `src`. The audit
  table is the queue: forwarding moves on only when Splunk confirms, waits and
  retries (5 s up to 5 min) when it does not, and resumes from the first unconfirmed
  entry, so nothing is skipped. Delivery is at least once, and each event carries
  the entry's `id` to deduplicate. With several replicas one process sends, under a
  lock in the database. A test button shows Splunk's own answer; the panel shows sent
  and waiting entries and the last error. The HEC token is stored encrypted and never
  shown again; observers see the state without the address or errors; API tokens
  cannot change the destination. Certificate verification is on by default and can
  be turned off for Splunk's factory self-signed certificate. Schema revision 0005.
- **Engine settings from the administration panel** (Administration → Settings,
  described in `docs/engine-settings.md`): twelve parameters the engine reads when it
  needs them, grouped as Import, Export, Execution, Step cache, ClickHouse and BigQuery,
  each with a description in the five languages. A change applies to every engine
  process within ten seconds, without restarts; task time limits apply from the next
  task, because they now travel in each task's message. The environment stays the
  starting value and one click goes back to it. The engine checks every value (ranges
  narrower than the environment's, the soft time limit below the hard one, the import
  read timeout below the soft limit) and the hard limit cannot exceed
  `ENGINE__RUN_STALE_TIMEOUT_SECONDS` minus five minutes. Values live in the gateway's
  database (schema revision 0004) and are sent again every minute, so a flushed Valkey
  heals itself. Administrators change them, observers see them, API tokens cannot
  change them, and each change is in the audit log with the old and new values.
  `ENGINE__SETTINGS_PANEL=false` keeps the environment the only source.
- **Fixed: a database import wrote one row group per driver batch larger than the row
  group size**, instead of splitting it (25,000 rows delivered at once came out as one
  block with a 10,000-row setting), and left a tiny row group after every block. Blocks
  are now exactly the configured size, except the last.
- **API tokens, service accounts and webhooks**: an API for integrations, described
  in `docs/api.md` (and on the `/docs` page the gateway serves, now in English).
  - **Personal API tokens** (Settings → API tokens) for one's own scripts, and
    **service accounts** (Administration → Service accounts) for schedulers, CI and
    department scripts: they sign in only with a token, never with a password or
    SSO, and get permissions from groups and folders like anyone else. Tokens are
    read-only or read-and-write, never more powerful than their account, always
    expire (at most `AUTH__API_TOKEN_MAX_DAYS`, 365 days), and are shown once:
    only a fingerprint is kept. Administrators see and revoke every token. Each
    action records the token's name in the audit log. A token cannot create other
    tokens, users or passwords, and is accepted only in the `Authorization` header.
    A read-only token can still export and preview. A new password, or deactivating
    the account, revokes its tokens with its sessions (reactivating does not bring
    them back). A token cannot reactivate an account or make one an administrator or
    an observer, so revoking a leaked token closes the door it opened.
    `AUTH__PERSONAL_TOKENS=false` leaves tokens to service accounts only, and
    switches off the personal ones already given out. Observer accounts have no
    personal tokens, and lose the ones they had when they become observers.
  - **Webhooks** (Administration → Webhooks): Tabularia calls an address when a flow
    run or a datasource refresh succeeds or fails, optionally only for one folder.
    Calls are signed (`X-Tabularia-Signature`, HMAC-SHA256 with the webhook's
    secret), queued in the same transaction that closes the run, retried for about
    seven hours, and delivered at least once (deduplicate on
    `X-Tabularia-Delivery`). Runs closed because their process died, the gateway
    shut down or they waited too long in the queue send `flow.run.failed` too.
    Loopback, link-local, multicast and the clouds' metadata addresses are refused,
    and the connection goes to the address that was checked, with the certificate
    checked against the name. Up to 16 calls go out at once per process, at most 2
    to the same webhook, so a receiver that is down does not hold up the others;
    `SSL_CERT_FILE` adds a company certificate authority. The call history, with
    a resend button, is kept `WEBHOOKS__RETENTION_DAYS` (30). A folder a webhook
    watches cannot be deleted until the webhook is changed.
  - Schema revision 0003 (three tables and `users.is_service`).
- **Fixed: an observer could find hidden IP addresses and email addresses in the
  audit log by searching for them** and counting the results. An observer's search
  now looks only at what the observer sees (the action, and targets that are not an
  email address).
- **Fixed: `NaN` or `Infinity` in a JSON body gave a 500 instead of a 422**, on any
  route, including sign-in.
- **Fixed: inputs of type url had the browser's default look**, and focused inputs,
  open selects, the selected node and a few active buttons glowed in the default
  theme's blue in every theme. They follow the theme's accent now.
- **Fixed: changing the theme after moving to another page did nothing** until the
  page was reloaded, and the choice was not saved. The theme was applied by a watcher
  created by the first page's top bar, which went away with that page. The preferred
  engine had the same fault (chosen, but forgotten at the next load). Both are now
  applied and saved at the moment they are chosen.
- **Explore: a row opens from anywhere on it**, not just from its name. Folders,
  flows, saved views and connections respond across the whole row, look like a button
  under the pointer and while pressed; the action buttons on the right stay separate,
  Ctrl/Cmd-click and middle-click open flows and views in a new tab, and selecting
  text in a row does not open it. Datasources, which open nothing, no longer look
  clickable.
- **A new theme, Imperium**, in Settings → Theme: the colours of Rome in a modern
  key. Light: travertine and marble surfaces, ink text, Tyrian purple for the main
  action, the focus and the current page, gold as a detail, Pompeian red for errors,
  laurel for success. Every colour measured for AA. Two charts that kept fixed colours
  in every theme (the run calendar and the schedule load) now follow the theme, and
  so do text selection and checkboxes.
- **Sessions that can be closed.** A session was a signed token and nothing more:
  signing out only dropped the browser's cookie, and a copied token kept working for
  up to twelve hours; nobody could sign anyone out, a new password closed nothing, and
  the token sat in a cookie the page's JavaScript could read. Now every sign-in,
  password or SSO, is a row in a new `user_sessions` table, and the gateway checks it
  on each request in the same query that loads the user (no extra round trip).
  - The browser keeps the session in an `HttpOnly`, `SameSite=Lax` cookie the gateway
    writes — `Secure` over HTTPS — so no script on the page can read it. Requests
    that change something and arrive with the cookie alone must carry an
    `X-Tabularia-Csrf` header, which a page on another site cannot add. Scripts and
    the API keep using `Authorization: Bearer`, unchanged.
  - Signing out ends the session on the server: the token stops working wherever it
    was copied.
  - **Your sessions**, in the settings menu: where the account is signed in (browser
    and system, address, last activity, password or SSO), with a button to close one
    or all the others.
  - The Audit page lists the open sessions one per browser or script, and an
    administrator closes any of them; observers see them masked and cannot close them.
    Closing is recorded in the audit log, and so is signing out.
  - A password set by an administrator, or a disabled account, closes that user's
    sessions (an administrator changing their own password stays signed in where
    they did it).
  - `AUTH__SESSION_IDLE_MINUTES` (`auth.sessionIdleMinutes` in the chart; 0, the
    default, = off): after that many minutes without a key, click or scroll, the
    page warns for a minute, then signs out and says why on the sign-in page. The
    activity is shared between the tabs, so working in one keeps the others open.
    The gateway refuses a session no request has used for that long.
    `JWT__ACCESS_TTL_MINUTES` stays the longest a session lasts.
  - SSO no longer passes the token through the address bar (`#token=…`): the gateway
    writes the cookie on its redirect, and the landing page only asks who signed in.
  - Expired and idle sessions are deleted by the orchestrator's clean-up, and with
    them the addresses they kept. An observer's session keeps neither address nor
    browser, as in the audit log. The last address of each user, which was kept
    forever on the user row (`users.last_seen_ip`) and no longer shown anywhere, is
    not written any more, and the upgrade clears it.
  - A sign-in that was still checking the old password while an administrator set a
    new one does not open a session: the two now wait for each other.
  - Observers no longer read in the audit log the email of the person a session,
    promotion or group change was about, nor the browser it came from.
  - **Upgrading signs everyone out once**: tokens from before carry no session and
    are refused. Upgrade the gateway before the interface. The interface and the
    gateway stay on one host (gateway under `/api`, as the chart and the compose file
    already do); on two hosts of one domain, set `AUTH__COOKIE_DOMAIN`
    (`auth.cookieDomain`). `APP__CORS_ORIGINS` now refuses `*`.
  - The shared observer account of a public demo sees and closes only its own
    session: closing the others would sign every visitor out.
  - For scripts using the API: `/audit/sessions` returns a page (`items`, `total`;
    the 100 most recent by default, `?limit=` up to 500), one row per session with
    `id`, `ip` and `user_agent` (it was one row per user with `last_seen_ip`); new
    `POST /auth/logout`, `GET /auth/sessions`, `DELETE /auth/sessions/{id}` and
    `POST /auth/sessions/close-others`; `/auth/login` leaves `access_token` empty
    when the request carries `X-Tabularia-Csrf` (the browser), and returns it as
    before otherwise.
  - The session cookie no longer travels past the gateway: the engine does not
    receive it.
- **Fixed: adopting a database from before Alembic** would have stopped at the first
  revision after the baseline: the adoption created every table of the current
  models, the new revision found its table already there. It now creates only the
  baseline's tables.
- **Fixed: the development compose did not start on a new machine.** It asked for
  `minio/minio:latest`, which has been withdrawn from Docker Hub; machines that had
  it cached never noticed. It now uses the community fork pinned to a release,
  `pgsty/minio:RELEASE.2026-08-04T00-00-00Z`, as the production compose already
  did; `MINIO_IMAGE` replaces it.
- **The quickstart generates the Fernet key without extra packages**: with `openssl`
  or a Python one-liner that needs only the standard library, or with Docker alone.
- **Everything in Explore moves to another folder**, not only flows: datasources,
  saved views, connections and folders have the same "move to another folder"
  button. The folder picker shows each folder's path and, for a folder, leaves out
  the folder itself and everything inside it; the row leaves the list in place with
  a message. The gateway checks the rights on both folders, as before.
- **Fixed: the end of an import blanked the Datasources list**, which looked like a
  crash: the whole page reloaded behind a skeleton. The rows now update in place,
  the open row stays open, and a message says "import completed, N rows" or, when
  it failed, why. Explore says the same at the end of a refresh started there. Only
  imports seen running get a message, not ones that ended before the page opened.
- **Pickers show where things live.** Datasources, flows, saved views and connections
  have names that are unique only inside their folder, so two "orders" in two folders
  looked the same. Their pickers (source, refresh and run-flow nodes, the Output
  node's connections, the viewer, the database datasource dialog) now show the folder
  path under each name, the name first and never cut for the path; searching a
  folder name finds what lives in it.
- **Sort keys can be changed after the import.** A database datasource's sort keys
  are edited in Datasources → open the row → Order, picked from its columns in the
  order ticked, and apply from the next refresh. They used to be fixed at creation:
  a mistyped key failed every refresh for good. A key that is no longer a column
  (renamed at the source) is shown and can be removed; a flow's datasource keeps
  taking them from its Output node.
- **Fixed: an SMTP connection with malformed options broke every flow launch** with a
  500. The options are checked when the connection is saved, and a malformed
  allowed-domains list already saved stops the email instead of lifting the
  restriction.
- **Fixed: saving a view could overwrite another datasource's view** with the same
  name in the same folder. It is refused, with a message.
- **Fixed: a run that failed showed its email as pending forever** (in the API); it
  now reads "not sent".
- **Fixed, viewer copies on ClickHouse:** a preview no longer fails when its
  materialized copy is dropped by the TTL sweep at the same moment (it reads the
  parquet instead), and the caches of sources below the threshold or recently failed
  no longer grow without end.
- **Release images published by CI, for amd64 and arm64** (`docs/release-process.md`).
  A `v*` tag builds the three images from scratch, smoke-tests them, and pushes each
  one only after two secret scans pass: the maintainer's own secrets, matched by
  fingerprint layer by layer (deleted files and nested archives included) without CI
  ever holding them, and TruffleHog for anybody's live cloud credentials. The tag
  must agree with the version in the code and in the Helm chart.

## 1.1.0 «Appio» — 2026-10-11

Second release, still «Appio». Since 1.0.0 Tabularia is ready for production on one
machine with Docker Compose and on Kubernetes with Helm chart 2.0; the metadata
database moves forward with Alembic migrations; the gateway runs in more than one
copy; flows export as dbt projects and as OpenLineage, datasources carry data
contracts, and an optional AI assistant answers over the datasources a user may read.
Oracle and Microsoft SQL Server join the sources, BigQuery the engines and Google
Cloud Storage the object stores. Before upgrading read
`docs/deploy/release-checklist.md`: the migration step, a JWT secret of at least 32
characters in production, and the order in which the services roll out.

- **Interface: the nine major findings of the 2026-10-11 UI/UX review, fixed.** The
  review (audit 13/20, critique 22/40, report in
  `docs/audit/FRONTEND-AUDIT-2026-10-11.md`) found nothing blocking and nine major
  issues; all nine are closed, each verified in the browser.

  *Navigation.* The top bar gave the navigation only what the search box and the
  memory gauge left over: at 1440px Connections, Runs and Administration were out of
  view, and on a phone the navigation was zero pixels wide with the settings gear off
  screen. The navigation now has precedence: the memory gauge keeps its bars (numbers
  in its tooltip, now translated) below 1800px and hides on a phone, the search
  narrows and widens while in use, the brand keeps its mark below 1100px. All nine
  entries are visible at 1440px. Global search now lands on the datasource or
  connection it found, even past the first page. Explore no longer overflows between
  721 and ~1170px and Flows keeps its row actions on a phone.

  *Numbers and dates* follow the interface language everywhere (they were Italian in
  16 places, whatever the language): «42,007,616», «Oct 11, 10:27 AM», never
  «11/10/26». The Viewer aligns numbers right with tabular figures, gives a column the
  same decimals and does not group integers (a period like 202607 no longer reads
  «202.607»); its number formatter is built once, not per cell.

  *Contrast.* Primary buttons in the default dark theme were white on #4f8cff, 3.22:1;
  their fill is now the light theme's gradient (4.58:1) and hover darkens instead of
  lightening. The privacy notice, the run status pills, the engine badge, node labels
  and lineage edge labels pass AA in all four themes (measured in the browser: 16/16
  above 4.5), through a per-theme `--enc-text-mix` for encoding colours used as text
  and `--danger-fill` / `--on-danger` for red fills.

  *Accessibility.* Form labels are now real labels (a `v-label-fields` directive links
  each label to the field after it; unnamed fields went to zero on the pages
  measured), the Select is a combobox with a listbox, `<html lang>` is sent by the
  server, the flow export choice is a real dialog, the Administration menu works with
  arrows and Esc, the settings panel, the «New» panels and «Save view» take focus and
  close with Esc, the folder drawer is modal, groups in Admin are selectable from the
  keyboard, and Esc inside a Select no longer closes the whole dialog with what had
  been typed.

  *Editor.* Unsaved changes are marked next to the flow name, leaving (Projects, sign
  out, a link) asks to stay, leave without saving or save and leave, closing the tab
  triggers the browser's warning, and Ctrl/Cmd+S saves. Nodes carry the operation's
  name instead of its id and say in one line what they do («stato = evaso», «agente_id
  → ricavo, ordini, clienti») instead of «1 parameters». A flow opens fitted to its
  nodes (a 32-node flow is wholly in view) instead of zoomed 2× on empty canvas.

  *Import progress* no longer speaks every second to screen readers: the ticking text
  is hidden from them, the page announces start and end once, and the clock ticks in
  its own small component instead of redrawing the page.
- **An import says where it is.** While a datasource is imported or refreshed, the
  Datasources page (under the name) and the Explore show the phase and the time since
  it started: queued, the database preparing the data (the query has gone out and no
  row has come back yet — the long silence of a large sort), the rows received so far,
  file 3 of 12 for SharePoint, saving to storage, checking the data contract. Every
  database gets it, because the phases come from the writer they all share. The worker
  reports them at most every two seconds in the task's own state, the engine returns
  them with the status and the gateway adds the seconds, counted on the server and run
  on in the browser between two polls; nothing is stored. There is no percentage: the
  total of a query is not known in advance. With an engine from before, only the time
  shows.
- **Large ClickHouse tables import instead of failing after five minutes.** ClickHouse
  sends no row until it has the first one: with sort keys on columns outside the
  table's own key it reads and sorts the whole table first, and the client gave up
  after 300 seconds without data — the library's default — whenever nothing kept the
  connection alive: always with a `readonly=1` user, which cannot ask for the progress
  headers that do it, and behind a proxy that holds them back. Imports from ClickHouse
  and writes into it now wait `INGEST__DB_READ_TIMEOUT_SECONDS` (default 3000, below
  the task's own limit); the connection test and the table list keep 300. The message
  told the user to check host, port and firewall; it now says the server accepted the
  query and was still computing it. Measured on 50 million rows (3.4 GB) sorted
  outside the key by a read-only user on one thread: 14 seconds before the first byte,
  growing with the table.
- **Microsoft SQL Server as a source.** A connection to SQL Server (host, port,
  database, user; tried on SQL Server 2022) imports a table, a view or a SQL query,
  refreshes it and schedules it like the other databases. It streams: mssql-python,
  Microsoft's own driver, carries its ODBC layer inside the wheel — no Microsoft ODBC
  driver in the image, three small system libraries only — and hands over Arrow
  batches; the shared writer gathers them into whole row groups (2.5 million rows in 5
  seconds, three row groups of up to a million, the worker's memory following the row
  group). The connection chooses how to encrypt: verify the server's certificate (the
  default), trust a self-signed one, or no encryption. A SQL source is sent as
  written, CTEs and `ORDER BY` included, but it must be one query that reads: a batch
  carrying `; DELETE …`, or a `SELECT … INTO` that would create a table, is refused
  before it reaches the server. Integer `decimal` up to 18 digits arrives as Int64,
  other decimals and `money` as Float64, `datetimeoffset` as UTC, `xml` as text;
  `geography`, `geometry` and `hierarchyid` arrive in their binary form (`.STAsText()`
  or `.ToString()` in a SQL source for text), and `sql_variant` is refused naming the
  column. The server's own messages reach the user, with what to check: a failed login
  (which is also how SQL Server reports a database that does not exist), a certificate
  that cannot be verified, a host that does not resolve or refuses. A dbt export of a
  flow reading from SQL Server is refused with the reason, as for Oracle.
- **Oracle as a source.** A connection to Oracle (12c and later; host, port, service
  name, user) imports a table, a view or a SQL query, refreshes it and schedules it
  like the other databases. It streams: python-oracledb in Thin mode — pure Python,
  no Oracle client in the image — hands over Arrow batches straight from the server,
  and the shared writer gathers them into whole row groups (2.5 million rows: three
  row groups of up to a million, 139,000 rows/s, the worker's memory following the
  row group, not the table). Oracle's rules apply: names without quotes are read in
  upper case, quoted ones as written, and the table list returns them in that form.
  `NUMBER` without precision (and over 18 digits) arrives as Float64, like
  Postgres `numeric`; `TIMESTAMP WITH TIME ZONE` is converted to UTC on the server;
  intervals, XML and object types are refused naming the columns, with a hint to
  convert them in a SQL source. The server's own messages reach the user
  (`ORA-01017`, a service name not registered with the listener). A dbt export of a
  flow reading from Oracle is refused with the reason: no dbt target can read it.
- **Ready for production on Kubernetes: Helm chart 2.0, tried on a real cluster.**
  The chart had never run on one; it now has, on minikube with Calico and
  ingress-nginx: install, three upgrades, the migration step queued across three
  pods, the live tour 153/153, single sign-on through Keycloak 11/11 and every page
  in Firefox 14/14. What changed: the gateway is published under `/api` on the UI's
  host (the old per-path routing sent pages such as `/flows` and the SSO return to
  the API, so reloading a page showed JSON and SSO did not work); the engine API
  refuses any caller without the gateway's internal token — derived from the
  Fernet key, so nothing to configure, in compose too — and the NetworkPolicy that
  admits only the gateway and the workers is on by default; the gateway reads the
  client's real address past `gateway.trustedProxies`, so a stranger's wrong
  passwords no longer lock the administrator out; each role loses the secrets it
  has no use for; CORS is closed by default; no pod mounts the service account
  token; PodDisruptionBudgets; two gateway replicas; the RAM badge is off, not
  failing, without a node-exporter. **Upgrade the gateway before the engine this
  time** (release checklist, chart README → Upgrades): a new engine refuses an old
  gateway.
- **Ready for production on compose: the open findings of the 2026-10-08 audit.** "Run
  now" on a flow that is already queued or running answers 409, in the language of the
  interface, instead of queueing a row that could only be refused: repeated, it pushed
  every other flow down the queue. The check and the enqueue are serialized per flow
  with a Postgres advisory lock, so it holds across gateway replicas and against the
  scheduler (twenty launches at once on three replicas: one accepted, nineteen 409). The
  gateway's control calls to the engine (launch, status, revoke, clean-up) give up after
  30 seconds: a hung engine or a half-open connection used to keep an execution "alive"
  forever, its flow blocked and its schedules skipped until a restart. In production
  `JWT__SECRET` must be at least 32 characters. Every page now carries a
  Content-Security-Policy, set by the frontend server: scripts only from the app (Nuxt's
  inline ones allowed by hash), requests only to the site and the gateway, so an
  injected script can neither run nor send the session token away; vue-i18n compiles
  messages without `eval` to live with it. The session cookie is `Secure` over HTTPS. A
  CSV download stops at `EXPORT__MAX_ROWS` (5,000,000 by default) with a clear message
  instead of filling the browser's memory — and the engine's reason for refusing a
  download (that one, or Excel's row limit) now reaches the user instead of a bare "400
  Bad Request". anyio, cryptography, urllib3 and pydantic-settings updated.
- **Helm chart: the engine starts with ClickHouse or BigQuery switched on.** Helm reads
  every number in a values file as a float, and the chart wrote a million or more in
  exponent form: `CLICKHOUSE_EXTERNAL__MATERIALIZE_MIN_ROWS: "5e+06"` by default, and
  `BIGQUERY__MAXIMUM_BYTES_BILLED: "2.147483648e+10"`, which the engine refuses as
  integers — with either engine enabled its pods would not start. Whole numbers are now
  written out in full, for every setting. The chart also gains what the application
  learned since chart 1.1.0: `app.auditRetentionMinutes`, the AI spending caps,
  `openlineage.endpoint` and `timeoutSeconds`, and `extraEnv` for any other variable of
  `infrastructure/.env.example` (a name the chart already sets is refused).
  `.env.example` documents the variables it lacked (`DB__AUTO_MIGRATE`,
  `APP__ROOT_PATH`, the OpenLineage endpoint and timeout, the chDB and cross-join
  limits).
- **Database migrations with Alembic.** The gateway's schema was `create_all` plus a
  list of idempotent `ALTER … IF NOT EXISTS` run at startup: additions only, no version,
  a manual "every new column needs its ALTER" rule, and drift — a database created today
  and one upgraded over months differed (a missing foreign key, TEXT vs VARCHAR, server
  defaults on one side only). Now the schema has Alembic revisions generated from the
  models, a single command (`python -m app.db.migra`) runs them in one transaction under
  an advisory lock and then compares the database with the models, and in production it
  is a step of its own: a `migrate` init container in the Helm chart (1.2.0); the
  development compose migrates at startup (`DB__AUTO_MIGRATE=true`). Otherwise the
  application only checks the revision and refuses to start on a database behind its
  code. A database from before Alembic is adopted on the first run; revisions go forward
  only and follow expand/contract rules so rolling updates keep working. Three indexes
  that lived only in the old ALTER list — one of them the unique SSO identity — are now
  declared in the models; a test fails when models and revisions disagree. See
  docs/database-migrations.md.
- **The dbt export's oracles are in the repository.** `e2e/dbt_export/` holds the
  end-to-end checks that prove an exported project gives the data Tabularia publishes
  — every operation on three targets, contracts, lifecycle, the folder in a team
  project, every Sample flow, determinism, the dialog in a browser, the AI — with the
  image of the real dbt they run (`Dockerfile.dbt`) and a runner (`esegui.py veloce |
  completo | browser | ai`) that checks the prerequisites, runs them against a stack
  and says which are green. Everything installation-specific comes from environment
  variables.
- **A dbt model for ClickHouse is read by ClickHouse before it is written.** sqlglot
  translated Postgres constructs ClickHouse does not have (`SIMILAR TO`, `LATERAL`,
  `TABLESAMPLE`) without an error, and the data team found out at `dbt run`. Every
  ClickHouse model now goes through `EXPLAIN AST` (chDB, or the engine's ClickHouse; no
  execution); a syntax error refuses the export with ClickHouse's words or, with the AI
  option, gets an AI translation that must pass the same check — with a second attempt,
  the reason in front, when the first does not (the model sometimes sent the query back
  as it was). Checked live, three times out of three: a
  Postgres query with `SIMILAR TO` became `match(...)`, ran in dbt on ClickHouse and
  gave the rows Tabularia publishes.
- **The dbt export can ask the AI.** Two options in the export dialog, shown when the
  assistant has an enabled model and off by default: descriptions written by AI where
  they are missing (a paragraph on what each flow does, a sentence for every column of
  the outputs, sources and seeds without one — in English, marked `(AI)`, from the
  structure of the flow and at most 3 sample values per column), and an AI translation
  where a query does not translate, used only if it passes Tabularia's own checks (one
  SELECT, nothing that writes, no table function, only the tables the model reads, the
  same columns). What the AI writes is kept per version of the flow, so re-exporting
  gives the same zip without a new call; spending goes through the assistant's
  accounting and caps.
- **`std` and `var` of a single value are NULL on ClickHouse too.** The ClickHouse and
  chDB engines gave `NaN` where Polars, DuckDB and BigQuery give `NULL`, in an aggregate
  and in a pivot, and "fill null" downstream did not replace it. Now every engine gives
  `NULL`, and so does a dbt project on ClickHouse. A cross-engine test reads the raw
  value (the old one turned `NaN` into `NULL` before comparing, so it never saw it).
- **A dbt export is the same every time, and says no in your language.** The same
  version of a flow with the same options now gives the same zip, byte for byte: the
  files no longer carry the export time (the version and when it was saved do; who
  exported it is in the README only), the zip entries are in order with a fixed date,
  and the steps shared by several outputs are named in the same order in every gateway
  process (they followed the order of a Python set, which changes between processes).
  Re-exporting into a repository shows only what changed in the flow. Why a target is
  not available and why a download was refused now come in the language of the
  interface: gateway and engine send a code, the gateway words it in English, Italian,
  German, Spanish or French (the frontend sends the interface language as
  `Accept-Language`).
- **The dbt export asks how the data team wants it.** Export → dbt opens a dialog:
  where it runs (a target that cannot work for the flow is disabled, with the reason),
  a complete project or a **folder to copy into an existing project** (with an
  `INTEGRATION.md`: what their profile needs, which sources they must already declare,
  `dbt build -s +tag:<flow>`), a folder name and a model prefix, staging /
  intermediate / marts layers, the schema, the team's own source names and the sources
  their project already declares, the materialization of each output, the contract
  tests and the emails in or out. The choices are remembered per flow; a refusal from
  the engine is shown in the dialog, in words. Checked by copying the folder into a
  project with its own model and `sources.yml`, on ClickHouse and DuckDB.
- **PostgreSQL `numeric` columns are numbers.** Amounts and prices from Postgres were
  imported as text, so they could not be summed or compared with a number; they are now
  doubles, like the decimals of a CSV or an Excel file. The new type arrives with the
  next refresh of each datasource: a flow that treated such a column as text, or a
  contract `column` rule that declared it `string`, needs a look then (a blocking rule
  keeps the old data and says so). Doubles and not exact decimals: the Postgres driver
  does not say the precision, and the engines compute doubles the same way. Also found
  by the dbt export oracle: on ClickHouse the same value read as text was `100` instead
  of `100.00`.
- **The dbt export runs on your ClickHouse.** A third target, next to the federated
  DuckDB and the native one: dbt-clickhouse on the ClickHouse Tabularia computes on
  (or another, through `CLICKHOUSE_HOST`), the usual setup of a customer whose data
  sits in Postgres or MySQL. ClickHouse reads those sources live through database
  engines — one `tab_src_<connection>_<schema>` database per source schema, created
  once by `dbt run-operation create_tabularia_sources` with the passwords from env
  vars (and the file log off, so they do not land in `logs/dbt.log`) — and
  `sources.yml` points at them; from there on everything runs in ClickHouse. Sources
  can sit on several connections, a datasource defined by a SQL query is translated to
  ClickHouse over the same databases, semi and anti joins are ClickHouse's own `LEFT
  SEMI/ANTI JOIN` (as in the engine), and the profile sets `join_use_nulls`. In the
  Flows page it is the first dbt option of the Export dialog.
- **A dbt model says what it does and where it comes from.** The SQL of an exported
  model is one `WITH` list with a step per node, each named after what it does
  (`filter_canale`, `aggregate_by_canale`, `left_join_customers`) and opened by a
  comment in plain words, laid out one clause per line. The head of the file says
  which flow, which version (and when it was saved) and which output it comes from,
  and who built it; `schema.yml` carries the same under `config.meta.tabularia`,
  with the tags `tabularia` and the flow's name, so `dbt run -s tag:tabularia` picks
  everything that came from the app. Contract `accepted_values` tests use the
  `arguments:` form, so dbt 1.12 no longer warns about them.
- **ClickHouse: `EXTRACT(YEAR FROM data)` and a `WITH` in the sql node.** ClickHouse
  is the engine almost every customer runs, and two everyday things failed on it.
  The compute check looked for words, so `EXTRACT(… FROM …)`, `TRIM(… FROM …)` or a
  column named `url` were refused as if they were subqueries; it now parses the
  expression and refuses only what reads outside the row — subqueries, tables and
  table functions (`s3()`, `file()`, `remote()`…), dictionaries, `joinGet`, models.
  A sql node whose query had its own `WITH` failed with a syntax error, because the
  engine put a second `WITH` in front of it; the engine's `input`/`self` now join the
  query's own list. Both found by running the dbt export oracle with ClickHouse as
  the reference engine, against a local ClickHouse reading the parquet in MinIO
  through `s3()`, as in production.
- **The dbt export gives the data Tabularia publishes, checked case by case.** An
  oracle runs every operation of the editor (60 cases, both targets) on tricky data
  through Tabularia and through a real dbt and compares the results column by
  column. It found, and this fixes: the cast (no trimming, rounding instead of
  truncation), the sort (no `NULLS LAST`), a compute that replaced a column moved it
  to the end, right/full joins lost the key of right-only rows, pivot labels, an
  unpivot that dropped NULL rows, a `sql` node with its own `WITH` that did not
  compile; natively, upper-case column names lost their case (sqlglot lowercased
  them, and expanding `*` again lowercased every alias born mid-flow), `TRY_CAST`,
  `contains`, `ends_with`, `UNION BY NAME`, semi/anti joins had no equivalent; a
  SharePoint datasource was taken for a database table. The first step of every
  model now casts the sources to the types Tabularia read them with, seeds declare
  their column types instead of letting dbt guess, and contract rules map to tests
  that pass, warn or fail exactly when Tabularia's check does.
- **A Polars panic no longer kills a worker.** Found by the same oracle: Postgres
  `numeric` columns were written to parquet as an opaque Arrow extension type, on
  which Polars' streaming reader panics as soon as a filter or a null check is
  pushed into the read; the panic is a `BaseException`, which no `except Exception`
  stopped, so the worker process exited mid-task and the run failed with
  "Worker exited prematurely". The import now writes the plain text underneath (the
  type users already saw), the Polars engine catches panics as errors, and every
  task turns a panic that still slips through into an ordinary failed run.
- **No more surprise logouts.** The frontend dropped the session whenever its
  "who am I?" request failed for any reason: a page change while the request was
  still in flight, a network hiccup, or a 502/503 while a gateway restarted — so a
  rolling update logged out everyone loading a page. Now only a 401 (expired or
  forged token, deactivated user) ends the session, as the rest of the API client
  already did. Verified in the browser: 117 rapid page loads, and all gateways
  stopped and restarted, without losing the session; a forged token and a
  deactivated user still land on the login page.
- **Dependencies.** PyJWT 2.15.1 (2.13 accepted a token with stray characters
  appended to its signature); Nuxt 3.21.11 and Vue 3.5.43 with their server
  renderer fixes; the production frontend image no longer ships npm, yarn or
  corepack.
- **The dbt export is for administrators.** A dbt project is self-contained: it
  carries the upstream flows, the text of SQL queries and, as seeds, the data of
  sources that are not in a database — including those of folders the person
  viewing the flow cannot see. So only administrators (the personal flag or an
  admin group: the way to give it to the data team) can export it; observers and
  ordinary users see only the OpenLineage option in the Export dialog, and the
  route answers 403.
- **The dbt export grows up.** It already produced a model per output; now the
  project is one you can put in a repository as it is: the steps shared by
  several outputs are `ephemeral` models referenced with `ref()`, database
  outputs in append are `incremental` models (and, natively, land in the
  destination table), `sources.yml` carries the tables' and columns'
  descriptions, `schema.yml` carries every model's columns and descriptions and
  the datasource's **data contract as dbt tests** with its severities, a
  datasource defined by a SQL query is an ephemeral model (run in the source
  database through DuckDB's `postgres_query`/`mysql_query` when federated), the
  output of another flow brings that flow's models along, and a file the flow
  reads becomes a seed (up to 50 000 rows). The native translation now casts
  `ROUND` for PostgreSQL and refuses `pivot` with a clear message instead of a
  broken model. All of it is verified with a real `dbt seed/run/test` in a
  throwaway container, and every Sample flow's result is compared with what
  Tabularia publishes. Guide: `docs/dbt-export.md`.
- **OpenLineage, optionally.** The lineage Tabularia already derives for its own
  Lineage page can now be told to an external catalog in the OpenLineage
  standard, with the official client: set `OPENLINEAGE__URL` (Marquez, DataHub,
  Astro, or any collector) or `OPENLINEAGE__FILE` (JSON Lines) and every run
  that closes becomes a job — a flow run named after its folder path, its
  output nodes as child jobs with a parent facet, a datasource refresh on its
  own — with what it read and wrote: datasources (named as in Tabularia under
  their folder path, with schema, snapshot version, the parquet as a symlink
  and the Tabularia id in a facet), database tables under the standard naming, the tables of a SQL query (the
  query in the `sql` facet), SharePoint files, published datasources, tables
  written, S3/GCS objects. Events leave after the commit that closes the run,
  through a bounded queue and a background thread, so a collector that is down
  or slow never fails or delays a run. Only metadata leaves the installation.
  `GET /flows/{id}/openlineage` exports a flow's dependencies as a static job
  event without running it, and `POST` sends it to the collector. Verified
  against a local Marquez.
- **Data contracts: what a datasource promises to whoever uses it.** A flow
  that publishes garbage on time is worse than one that fails: the failure is in
  the run history, the garbage is in everybody's dashboards. A datasource can now
  carry a contract — which columns it has, of which type, and the rules its
  values follow — and the rules are checked **every time new data arrives, before
  it is published**: on a refresh from a database or SharePoint and on a flow
  that overwrites the datasource.

  **Two severities, per rule, and nothing in between.** A *warning* does not
  block: the new data is published and the violation is reported. An *error*
  blocks: the new data is not published, the run fails saying which rule broke,
  and the datasource **keeps serving the last good data** — same snapshot, same
  rows, as if the update had never been attempted. The check sits between the
  write and the swap, so there is no moment in which the bad data is readable.

  Nine kinds of rule: column present and of a type, not null, unique (one column
  or several), accepted values, range, pattern, row count, freshness, and a free
  SQL condition that must hold on every row. One evaluator for every engine, so a
  contract means the same thing whether the flow ran on Polars or on BigQuery. A
  rule that cannot be evaluated (its column is gone, the expression does not run)
  does not hold — silence is not consent. Freshness is the only rule that breaks
  without anything arriving, so the scheduler re-reads it as time passes.

  **The state is an icon wherever the datasource appears** — the Explore, the
  Datasources page, the Viewer, and the source nodes of the editor, so whoever
  builds on a datasource sees whether its producer is keeping the promise: a
  plain shield (not checked yet), a shield with a check (respected), with an
  exclamation mark (warnings), with a cross (violated, or the last update was
  refused). Shape and colour both change, and the state is also written out in
  the tooltip. The dialog groups the rules by column and puts next to each one
  its **last result** — how many rows broke it, a few offending values — rather
  than a separate report to cross-reference. *Propose from the data* writes a
  first contract from what the datasource contains today; a history keeps every
  check and every refusal; people who can only read the folder read the contract
  and its report, without the commands.

  **Be told when the state changes.** A contract can carry addresses and an SMTP
  connection, with the same barriers as the failure notice of a flow. One mail
  per *change* of state, not per check — a refresh refused every night says so
  once — and only for what happens on its own: whoever saves a contract or
  presses *Check now* is already looking. The mail is sent by the scheduler from
  a queue, never inside the transaction that publishes the data, and with several
  orchestrators exactly one of them sends it.

  **Export to ODCS.** The contract downloads as an Open Data Contract Standard
  v3.0.2 document (YAML), validated against the official schema: columns with
  logical and physical types, `required` and `unique` for the blocking rules,
  every rule as a quality check with its severity, freshness as a service level.
  Export only: the contract in Tabularia stays the source.
- **Undo, redo, copy and paste in the flow editor.** Ctrl/Cmd+Z and Shift+Z (or
  Y) walk back and forth through the changes to the canvas — a node added, moved,
  deleted, a parameter edited, an edge drawn — and Ctrl/Cmd+C, X, V and D copy,
  cut, paste and duplicate the selected nodes with the edges between them, also
  from one flow to another. A drag is one step, not one per pixel; loading a flow
  or re-attaching a source to its fresh snapshot is not a step at all, because
  undoing it would lead to a state nobody chose. The shortcuts stay out of the
  way while typing in a field or selecting text.
- **The gateway can run in more than one copy, and orchestration scales on its
  own.** One gateway process served about 95 requests a second, and everything
  that was not a request — the scheduler, the running orchestrations, the login
  throttle, who has a flow open — lived in its memory, so a second copy would
  have run every schedule twice. `APP__ROLE` now splits the two jobs: `api`
  answers requests, `orchestrator` runs schedules and flows, `all` (the default)
  does both, exactly as before, for an installation that wants one process.

  An orchestration is a row in `runs` that waits to be claimed: any orchestrator
  takes it with a conditional update, beats while it works, and if it dies the
  others close its runs after a minute instead of leaving them running forever.
  A unique index allows one live orchestration per flow, whichever copy is asked.
  The login throttle and editor presence moved to tables for the same reason.
  Schema changes run once, behind a lock, and only when there is something to
  apply. The Helm chart (1.1.0) deploys the two roles separately; coming from
  chart 1.0.x, scale the old gateway to zero first.

  Found on the way: **the failure notice of a scheduled flow never fired.** It
  looked for runs of kind `flow`, and what the scheduler runs is the
  orchestration. It does now.
- **An observer role: reads the administration panels, writes nothing.** Granted
  by a flag on the person or by a group, like administration itself; an
  administrator is already an observer, because whoever commands sees.

  The split is by **method**, not by panel: every GET on an admin surface accepts
  an observer, every POST, PUT, PATCH and DELETE stays with the administrator. A
  panel declared "read-only" that happened to contain a write route would be a
  silent hole; this way the containment is structural.

  **The panels open, the personal data does not.** They hold emails, IP addresses
  and the trace of who did what — what an administrator needs to decide, and what
  someone who only watches does not need at all. For an observer, addresses are
  masked to `a***@example.com` (the domain stays: it says inside or outside the
  organisation), names are dropped and IPs are omitted. The audit still says
  *what* happened, which is the point of the register, and the mask is stable, so
  "these three actions are the same person" is still visible.

  Also, for everyone and not only for observers: the heavy-query list in the
  Performance tab now empties the arguments of `s3(...)` and `url(...)`.
  ClickHouse masks the secret key in its query log by itself, but not the access
  key, and leaves the endpoint, the bucket and the object path in clear — checked
  against a ClickHouse 24.8. In a list of slow queries none of that says anything
  useful.
- **What an installation open to strangers needed.** Three gaps that were there
  all along, found while planning a public demo.

  Uploading a file asked only for a login — no capability at all — so a
  read-only account could write into the installation's bucket. An upload has no
  folder to ask permission on, which is why it was never gated; the question that
  does make sense is whether this person creates things or only looks at them, so
  it now needs EDIT somewhere.

  The assistant had a ceiling per turn and none per day: a thousand turns under
  the ceiling still cost a thousand times. `AI__MAX_COST_PER_DAY_USD` caps what
  one account can spend between midnights, refused before the stream opens so it
  arrives as an error and not as a broken answer. A turn whose model could not be
  priced counts as zero — not because it was free, but because guessing would be
  worse than admitting we do not know.

  Repeated sign-in attempts had no brake beyond bcrypt: 25 in under seven seconds
  during the audit. Now five attempts pass and the sixth waits, doubling with
  each further failure, counted per (address, email) so an office behind one NAT
  cannot lock itself out and switching account does not reset the count. A
  successful sign-in clears it.

  Also fixed, and it only shows up with several people on one account: the
  assistant's preview slot was scoped per user, so a second question cancelled
  the first person's answer mid-stream. It is scoped per conversation now, which
  is also right for one person with two tabs open.
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
