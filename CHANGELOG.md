# Changelog

All notable changes to Tabularia. The version shown here is the one the gateway
exposes at `/system/info` and in the app's settings menu.

## 1.2.2 «Appio» — 2026-10-22

- **Administration → Settings holds every setting an administrator may own**, about a
  hundred instead of fourteen, in a box per component (Gateway, Kubernetes, Engine) with
  a search: sign-in and sessions, single sign-on, orchestration and schedules, history
  kept, webhooks, the AI assistant's limits and budget, dbt export, uploads, backups'
  timeout, monitoring, OpenLineage, the Kubernetes calls, scripts, image builds and
  Container nodes, the workers' recycling and memory, the embedded engines, the caps of
  SFTP, S3 folders, HTTP APIs and placeholders. Most apply within ten seconds on every
  process, gateway and orchestrator replicas included. The ones read only when a process
  starts carry a restart icon; a banner lists the components still running a previous
  value and, on Kubernetes, restarts their pods one at a time through the eviction API
  (Helm `settingsRollout.enabled`, on by default; the chart grants reading and evicting
  the release's pods, not changing its Deployments); on Docker Compose it names the
  services to restart. Addresses, credentials, the switches tied to the cluster and the
  audit log's retention stay in the environment, on purpose. Constraints between
  settings are checked (heartbeat and dead-after, executions and database pool, the
  run's lost threshold and the engine's hard limit, both ways). Schema revision 0022
  adds two empty tables and a column. Guide: docs/engine-settings.md.
- **Fixed: `helm rollback` to the previous release stopped half way.** The `migrate` step
  of the older release found a database revision it did not know and every new gateway
  and orchestrator pod stayed in `Init:CrashLoopBackOff`, while the other components had
  already gone back. From this release on, a database newer than the code is left as it
  is, with a warning, and the rollback completes. **Upgrade note:** 1.2.1 and earlier
  still stop that way: to go back from 1.2.2 to 1.2.1, restore the backup taken before
  the upgrade (docs/deploy/upgrading.md, tried on kind).
- **Fixed (Helm chart): with `clusterView` and `settingsRollout` off, the backups never
  ran.** Without a service account token the gateway took the cluster for Docker Compose:
  the nightly backup and «Back up now» stayed off. It now knows a Kubernetes pod by
  `KUBERNETES_SERVICE_HOST`, which Kubernetes always sets.
- **Fixed (Helm chart): the scripts' or the Container nodes' namespace set to the
  release's own, with `blockTabularia: false`, gave the gateway rights on the release's
  Secret.** That namespace is now refused whatever `blockTabularia` says, and so are two
  user-code namespaces with the same name. `extraEnv` refuses too the names the chart sets
  container by container (`APP__GATEWAY_THREADS`, `CELERY__WORKER_CONCURRENCY`, …),
  which silently covered the value given there.
- **Fixed: after restoring a backup, no backup could be made for over an hour.** A
  backup is taken while its own run is marked in the database, so the restored database
  said a backup was running, on a process long gone: «Back up now» was refused and the
  nightly one skipped until that claim aged past the longest a backup may take. A running
  backup now writes a heartbeat; a claim without one for two minutes is ignored.
- **Fixed: with PostgreSQL not answering, the sign-in and every page answered a bare
  «Internal Server Error».** They now answer 503, «The database is not answering».
- **An assistant for scripts**, beside the code of a Script node or a saved script
  (where an AI assistant is configured): it writes and fixes Python and R for the
  script's environment, proposes whole files that are applied only when the user says
  so (one change, Ctrl+Z takes it back), and «Explain and fix the last error» starts
  from a failed run. No data reaches the model: the code, the environment and its
  packages, the names of the variables and of the inputs' columns, and only the error
  part of a failed run's log. The editor of a saved script now shows its latest run
  too, with the line it stopped at.
- **The Refresh datasource and Run flow nodes are no longer offered in the editor**: a
  flow group does the same, in steps (refreshes, flows, scripts, images, Pushmail).
  Flows saved with them keep running and scheduling as before; selecting one of these
  nodes says where to go instead.
- **Pushmail, a new cell of the flow groups**: one email through an SMTP connection
  with one `.xlsx` per datasource (up to 20), To and Cc, a subject and file names with
  the date placeholders, and the rows at most of each datasource; a datasource with
  more sends its first rows and the email says so. Several datasources, or a big one,
  bring a warning to ask the administrators how large a message the SMTP relay takes;
  more than 15 MB of files is never sent. CONNECT on the connection, the allowed
  domains and VIEW on the datasources are checked when the group is saved and again
  when it runs; every email is audited. Guide: docs/pushmail.md.
- **SharePoint reads CSV files too**, not only Excel: the datasource has a format —
  Excel and CSV by each file's extension (the default), Excel only, CSV only — and the
  CSV options of the SFTP datasource (separator, encoding, header); the sheet is now
  optional (empty = the first). One table can stack workbooks and CSV files, united by
  column name. Datasources saved before keep reading Excel files only, so a `Budget/*`
  of theirs does not start taking the CSV files beside the workbooks. The connection
  type is called «SharePoint» (it was «SharePoint (Excel)»).
- **The sheet of an Excel file is chosen, no longer always the first.** Uploading a
  workbook with several sheets in the editor opens a window that lists them (the hidden
  ones marked): every sheet ticked becomes a source on the canvas, and the source shows
  its sheet. Without a choice the first *visible* sheet is read (before, a hidden first
  sheet was). An empty or missing sheet is refused saying so. In the SharePoint, SFTP and
  «S3 folder» datasources, «Check the files» now lists the sheets of the first workbook
  found, and the sheet is picked from that list instead of typed; for SFTP and S3 «the
  first sheet» names which one it is.
- **The Sort node sorts on several columns, a direction each**: the first column decides,
  the next ones break the ties (region ascending, then amount descending). In the editor a
  row per column, with ascending/descending, arrows to change which decides first, and «+
  add a column»; the node's label shows the direction of each. Same order on every engine
  and in the dbt export (before, DuckDB, the ClickHouse engines and BigQuery applied the
  first direction to every column of a list). Flows saved before, one column with one
  yes/no, open and run as they did. The AI assistant writes the new form too.
- **Fixed: on ClickHouse (external, or chDB) every filter on a date-time failed.** The
  editor's calendar sends `2024-01-15T10:30`, without seconds, and ClickHouse reads a
  DateTime only with them («Cannot parse time component of DateTime»). The value is now
  written in full for a date-time column; a text column keeps it as it is. A value that is
  not a date (`15/01/2024`) is refused in the same words on every engine, not with the
  database's error and the query in it.
- **Fixed: the filter «contains» on Polars read the value as a regular expression** (also
  in a source node's filters): «Speck+Brie» found nothing and a run published an empty
  datasource, «1.5» found «105» too, «Vino (» was an error. The text as written now, as on
  the other engines.
- **Fixed (DuckDB): a pivot with «distinct count» counted every row**; **«first», «last»
  and «Unique» took any row of the group** on large data (DuckDB aggregates in parallel),
  where Polars took the first: both now take the first row in the data's order (Polars'
  «Unique» kept any too).
- **Fixed: a union «identical schemas» of two inputs with the same columns in another order
  swapped them without a word on DuckDB and ClickHouse** (the products' names in the ids).
  Columns are now put together by name on every engine; inputs with other columns or types
  are refused, saying which.
- **Fixed: a CSV saved by Excel in Italian (or French, German, Spanish) uploaded from the
  editor**: `;`-separated, it answered 500; with decimal commas, the prices stayed text and a
  conversion emptied them; in Windows-1252, «Città» came as «Citt�». The separator, the
  decimal comma (thousands grouped by dots too) and the encoding are now read from the file.
  A column whose type changes after the first 10,000 rows no longer answers 500: the types
  are guessed again on the whole file. Any CSV the reader cannot read is refused saying
  why; spaces around the headers go, as for the SFTP and S3 datasources.
- **Fixed: a division by zero showed as an empty cell in the preview** (a missing value that
  «is null» did not find); it shows inf or NaN.
- **Fixed: converting text to yes/no failed on Polars, and read «true» as empty on
  ClickHouse**: true/t/1/yes/y and false/f/0/no/n on every engine, anything else empty.
- **Fixed: Ctrl+S in the flow editor, with the cursor still in a field of the node's panel,
  saved the value from before the one just typed**, said «saved», and the change was lost at
  the next reload. The field now gives its value first.
- **Fixed in the interface:** the chart of the Viewer and of the editor's preview stayed blank
  with its axes chosen, until a control changed; a saved view now keeps its chart (the tab and
  the axes); changes in Administration → Settings not saved are asked about before going to
  another section or page; the Viewer says when it shows the first rows only; thirty-one
  actions of the audit log showed as raw keys; the runs' status was in English in every
  language, and so was a wrong sign-in; «+ Operation» put the new node off the canvas;
  a node just added showed a red error instead of a hint; the editor's way back went to
  All folders instead of the flow's; the code editor of a saved script spoke of a node and
  a flow; the pages Runs, Connections and Viewer, and the Explore's path, were wider than a
  phone; the page kept asking every few seconds for a memory reading the installation does
  not have (on Kubernetes). A few texts in Italian, French, German and Spanish too.
- **Fixed: about twenty-five texts of the Explore page were in English in French, German
  and Spanish** (the search, New, the empty folder, the path).
- **Fixed: an orchestrator that stopped left the flow's steps writing.** A `helm
  upgrade`, a restart from Administration → Settings, an eviction or an OOM closed the
  flow as interrupted, but its Output or refresh already with a worker went on, and the
  flow could start again at once on top of it (the same rows twice in an append Output).
  The steps still open of an interrupted flow are now stopped with it: their engine tasks
  revoked, the Jobs of Script and Container nodes deleted. After a database outage the
  orphans' sweep waits until every live orchestrator has beaten again, and never closes
  what its own process runs.
- **Fixed: a worker killed mid-run left its runs «running» for an hour,** then failed them
  with «Timeout… raise the limit». On cgroup v2 an OOM kills the whole container, and an
  evicted pod or a lost node write nothing either. The engine now tells such a task from
  a running one by the worker's own record and fails it within seconds (the same pod's
  container started again) or about a minute (the pod gone), saying the worker stopped.
- **Fixed: one failed answer of the Kubernetes API failed a Script or Container node,**
  ignored its retries and deleted a Job running fine (a 503 during a control plane
  upgrade, a 429). It is asked again at the next look; a log the cluster cannot give (the
  Pod's node lost) no longer fails the node either. A Pod the cluster takes away (a drained
  node), a container that cannot be created and a Pod never scheduled are now said to be,
  as is a script or container out of memory, with its limit, instead of «exit code 137».
  Two Container nodes of one registry at once no longer fail on its pull Secret (409); an
  image build or «Try in the cluster» the cluster refuses (Pod Security, a quota) fails at
  once saying why, instead of after an hour; a build killed at its memory says so.
- **Fixed: an editor's trial still running could be closed as an orphan** when its input
  waited long in the workers' queue, and one whose gateway pod went stayed «running» for
  half an hour. Trials now beat on their own run: a live one is never closed, a dead one
  within two minutes. A run closed meanwhile keeps its outcome.
- **Fixed: with two gateway replicas, a setting just saved could show «not applied»** for
  up to ten seconds, on the page served by the other replica. The page now puts in force
  the values it reads.
- **Fixed (Helm chart): with Helm 4, every `helm upgrade` failed once the workers'
  autoscaling bounds had been changed from Administration → Workers** (a server-side
  apply conflict on `maxReplicas`, the upgrade stopped half way). The chart now renders
  the bounds the panel set.
- **Helm chart: the gateway and the engine stop gracefully.** They wait 5 s for the Service
  to drop them, then finish the requests in flight within a 300 s grace period
  (`gateway.terminationGracePeriodSeconds`, `engineApi.terminationGracePeriodSeconds`): an
  upgrade or a scale-in no longer answers 500 or 502 to the requests caught in between
  (tried on kind: none of 2,791 requests failed while both rolled). Their probes answer on
  the event loop and wait 5 s, so a busy gateway is not restarted as a dead one; the
  gateway's database connections time out and keep alive. An engine that does not answer
  is a 503 that says so, not a 500.
- **Fixed: DuckDB took the processes per worker from the environment, not from
  Administration → Workers:** a pool grown from the panel gave each process a share of
  memory meant for fewer, and the kernel killed the container. It also spills within the
  pod's `/tmp` now (`TMP_SIZE_LIMIT`, set by the chart), past which the kubelet evicts
  the pod; its CPUs are read on cgroup v1 too.
- **Fixed:** a backup stopped by its orchestrator's shutdown is recorded as failed (it
  vanished, and the night's backup was skipped in silence); the engine's API empties its
  temporary directory when its container starts again in the same pod; the SIEM export
  and the workers' scaling no longer share a database lock, nor a flow group's queue and
  a datasource's refresh; a flow group's parallel Container cells each link their own
  run; a failed attempt's output is deleted before the next one; «Try in the cluster»
  answers within 270 s, under the ingress' 300 s, saying when a large image is still
  downloading.
- **Documented:** one installation per Redis database, one release per namespace, the
  workers' grace period above the tasks' hard limit (the panel cannot check the chart),
  KEDA's `cooldownPeriod`.
- **Fixed: a failed task could show the error as Python's own words**
  (`<class 'botocore.exceptions.EndpointConnectionError'>(…)`); it now reads as a sentence,
  «The object storage is not answering: …» for the storage.
- **Fixed: about fifty texts of the administration panel were in English in French,
  German and Spanish** (users, groups, banners, engines).
- **Fixed: the space bar typed nothing in the code editor of the Script node on Chrome
  and Edge.** The editor takes the text through the browser's EditContext there, an
  element the flow canvas did not recognise as a text field, and the canvas kept the
  space for itself (hold space to pan). The keys typed in the code editor now stay
  there, on every browser.
- **Fixed (Helm chart): the scripts and the containers could reach Tabularia's own
  namespace.** The policy the design promised was never written: the code of a Script or
  Container node, the trials and the image builds could reach the database, the broker,
  the storage of the release and the cloud metadata addresses. Their namespaces now get a
  NetworkPolicy (`scripts.blockTabularia`, `containers.blockTabularia`, on by default):
  the internet and the other namespaces stay open, Tabularia's and the metadata do not,
  and nothing reaches into them. Private networks outside the cluster are opened with
  `networkPolicy.userCodePrivateCIDRs`: **before upgrading, list there the registry the
  image builds push to when its address is private** (see the chart's README,
  Upgrades). The policy needs a network plugin that enforces egress rules (Calico,
  Cilium). The guides' dependencies (`infrastructure/minikube/dependencies.yaml`) now
  let only Tabularia's namespace reach their database and queue.
- **Fixed (Helm chart): a link-local range such as a node-local DNS cache
  (`169.254.20.10/32`) in `networkPolicy.egressCIDRs` made a policy the cluster
  refused.** The metadata address is cut only out of the ranges that hold it; listing
  it alone is refused. The scripts' or containers' namespaces set to the release's own
  are refused too: their policy would cut Tabularia off.
- **Fixed: Ctrl+X in the code editor cut the Script node off the flow (Chrome, Edge).**
  The flow editor's shortcuts (cut, copy, paste, undo, delete, duplicate) now leave alone
  the code editor and every dialog, on every browser; Ctrl+S saves what was typed in the
  last moment too.
- **Fixed: an editor's «Run» of a Script or Container node could stay open for ever**
  when the gateway running it stopped. It is closed saying so, and the next «Run» starts
  a new one.
- **Fixed (Helm chart): the gateway, which answers the internet, could create Jobs in the
  image builds' `privileged` namespace.** The orchestrator, which alone runs the builds,
  now has a service account of its own (`<release>-orchestrator`), the only one that may
  create them; the gateway's may still read and delete them.
- **Fixed: a successful image build could end as failed** when the Kubernetes API did not
  answer right between the build and its check. The check's Job is created before the
  build's is deleted, and the next round goes on.
- **Fixed: the dbt export refused a flow whose email Output sends what a Script or
  Container node writes.** An email has no model: the export goes on, with a note.
- **Faster: the editor's previews of Script and Container nodes** read two runs per node,
  instead of the flow's last 500 with their logs.
- **Docker Compose: scripting and containers show as unavailable**, greyed out with the
  reason (they need Kubernetes, with the feature on in the chart), in Administration, in the palette, in Explore and on the
  nodes of a flow that came from a cluster; such a flow is saved as it is.
- **Fixed in the flow editor:** the preview of a join whose right branch reads a Script
  node; an observer could change a Script or Container node's panel; a failed «Run»
  hid its log; the tags of a Container node were cleared at every change; a dialog
  opened at once ignored Esc.
- **Saved scripts (Kubernetes).** A Python or R script becomes an object of a folder,
  next to flows and views (**Explore → New → Script**), with the code editor of the
  Script node and its **versions** (every save that changes it is one; an older one can
  be looked at and restored). It **reads datasources** declared as its inputs, each by
  an alias (`TABULARIA_INPUT_<ALIAS>`, and `TABULARIA_INPUTS` with the real names and
  folders, so two datasources with the same name stay apart; a refresh changes nothing
  for the script), may leave an **output** shown at the end of the run and, when asked,
  **published as a datasource**. It runs on its own (**Run** on its page, followed to its
  end with its log and the first rows of its output), **on a schedule**, as a **cell of a
  flow group**, and from any flow's **Script node by reference**: fixed once, every flow
  runs the new version. Whoever lacks VIEW on its folder or the use of its environment
  does not see it at all; every run checks again, with the runner's authority, each
  input and the publish folder; a change by someone else that makes it reach more
  switches off the schedules that run it. Migrations 0019 and 0020
  ([Python and R in flows](docs/scripting.md#saved-scripts)).
- **The Scripting tab**, for whoever may use a scripting environment or a container
  registry: their saved scripts with their last run and schedule, their environments
  with packages and variables, their registries with images and variables. The
  **Lineage** shows saved scripts: what they read, what they publish, the flows that run
  them.
- **Container images, guided.** The Container node reads the chosen image's own
  configuration from the registry: what it runs by default, its platforms, its build
  date, size and labels; one click copies its command, or adds a variable it declares.
  A **flow group** can run an image as a **Container cell**, a step of its own
  ([The Container node](docs/container-node.md#in-a-flow-group)).
- **Faster: a short step is seen ending sooner.** Tabularia looked at a running step
  (a Script or Container Job, an engine run, a refresh) at a fixed interval, and the
  interface at another: a one-second script was seen ending after nine to eleven
  seconds. It now looks soon at first, then every second for half a minute, then at the
  interval set (`ORCHESTRATOR__POLL_INTERVAL_SECONDS`, `SCRIPTS__POLL_SECONDS`,
  `CONTAINERS__POLL_SECONDS`, now a ceiling); the same script is seen ending in four to
  six seconds.
- **Administration → Cluster (Kubernetes).** What Tabularia has running, refreshed every
  three seconds: the pods of the release and of the users' code (scripts, their trials,
  image builds, containers), their state with the reason a container waits or died
  (CrashLoopBackOff, ImagePullBackOff, OOMKilled…), restarts, age, node, CPU and memory
  against their limits (with metrics-server), and the warning events; a script's or a
  container's Job links to the flow, saved script or group that started it. Read at a
  glance: one bar of every pod by state; a map with a row per node of the cluster and a
  tile per pod on it (a square for Tabularia's, a circle for a script or a container; the
  pods not on a node yet apart), each leading to its row; the pods grouped by component
  ("2 of 2 ready"), with the last minute of CPU drawn from the page's own readings and
  memory against the limit; a row lights up for a moment when its pod changes state. A
  pod per block at phone width. Read-only, for administrators and observers
  (`clusterView.enabled` in the chart, on by default: read rights on pods, events and pod
  metrics in those namespaces only).
- **The database stops growing with history.** Every hour the orchestrators keep the
  latest 100 data contract reports per datasource (`ORCHESTRATOR__CONTRACT_RESULTS_KEPT`;
  a report whose notice is still to be sent always stays), the latest 50 versions of a
  flow and of a saved script (`ORCHESTRATOR__VERSIONS_KEPT`; the one in use always), and
  delete the assistant's conversations untouched for 90 days
  (`ORCHESTRATOR__AI_CHAT_RETENTION_DAYS`; the spending ledger stays). 0 keeps everything
  of that kind. A pass that removed something ends with `VACUUM (ANALYZE)` of the tables
  it touched: Postgres does not reuse the space of deleted or rewritten rows until a
  vacuum passes, and this one does not block the application.
- **Runs keep their logs for 30 days** (`ORCHESTRATOR__LOG_RETENTION_DAYS`,
  `orchestrator.logRetentionDays` in the chart; 0 keeps them for ever). A run kept what
  a Script or Container node printed (up to 256 KB) and the engine's full traceback of a
  failure for ever: a chatty container scheduled every five minutes added tens of
  gigabytes a year to the database. Past the window the orchestrator empties them, older
  runs included on its first pass; the runs stay, with their outcome, short error,
  timings, attempts and exit code, and say that their log was removed.
- **Fixed: single sign-on with Keycloak failing in company setups.** Reproduced on
  minikube with Keycloak behind the chart's ingress:
  - **a second sign-in started meanwhile broke the first.** A double click, another tab
    or the Back button replaced the only transaction cookie, and the first one came back
    as `state_mismatch`. Each sign-in now has its own cookie;
  - **clocks a fraction of a second apart refused the tokens.** An id_token from a
    Keycloak whose clock is ahead, or a transaction or session token signed by another
    gateway replica, was "not yet valid": with someone already signed in to Keycloak the
    round trip takes less than that. A minute of tolerance, as the identity providers
    allow;
  - **a sign-in started on http with an https redirect URI lost its cookie**: `Secure` now
    follows the browser's scheme, as the session's cookie already did;
  - **`tx_missing` said nothing useful.** The gateway logs the address the sign-in
    started at and the redirect URI's when they differ, and the login page tells the
    person what to do for the common codes, in the five languages.
- **Fixed: single sign-on with an identity provider whose certificate a company's own
  authority signed.** The gateway trusted the public authorities only: every sign-in
  failed with `idp_unreachable`. `oidc.caCert` (the authority's certificate) or
  `oidc.caConfigMap` (an existing ConfigMap, a trust-manager bundle) now gives it,
  besides the public ones, for every call to the provider (`OIDC__CA_FILE`); a
  certificate not trusted has a code of its own, `idp_untrusted`, which names the host
  in the log and tells the person to call their administrator. Whoever cannot have the
  authority may accept the certificate unchecked with `OIDC__VERIFY_TLS=false`
  (`oidc.verifyTls`): the installer's choice, explained with its risk in the Keycloak
  guide and logged at every start. Reproduced and tried on
  minikube with Keycloak behind such a certificate.
- **A guide to single sign-on with Keycloak** ([docs/sso-keycloak.md](docs/sso-keycloak.md)), on
  the website too: the client in Keycloak, groups or roles, the chart's values, the
  checks, and what each error code means. Every step run against Keycloak 26 with the
  chart. The website's menu also lists the upgrade guide.
- **SSO groups from any part of the token.** `OIDC__GROUPS_CLAIM` (`oidc.groupsClaim`)
  takes a path as it appears in the token's JSON — `realm_access.roles`,
  `resource_access.<client id>.roles` for Keycloak's realm and client roles, a client id
  with dots included — and several sources comma-separated, joined. A path the id_token
  does not carry is read from the access token, where Keycloak puts the roles by default
  (signature, issuer, expiry and person checked), then from userinfo. Tried on minikube
  with Keycloak 26.
- **Automatic backups of the database.** Every night at 03:00 an orchestrator runs
  `pg_dump` of Tabularia's database and streams it to the engine, which stores it in the
  object storage — by default Tabularia's bucket under `backups/postgres/`; a bucket of
  their own (`BACKUP__BUCKET`, `backup.bucket` in the chart) is advised in production —
  and keeps the latest 14. **Administration → Backups** changes the schedule, the number
  kept and whether it runs, without a restart; «Back up now» makes one at once; the
  backups are listed and an administrator downloads them (each download is in the audit
  log, and so is each backup, failed or not). Nothing is written on the orchestrator's
  disk. The gateway image now carries `pg_dump` and `pg_restore` of PostgreSQL 14 to 18
  (5 MB): a backup is made with the server's own version, so the server's own
  `pg_restore` reads it. Migration 0021 adds the table of the settings. Kubernetes only: on
  Docker Compose the section is greyed out, with the reason, like scripting and containers.
- **A guide to upgrading on Kubernetes and minikube** ([docs/deploy/upgrading.md](docs/deploy/upgrading.md)):
  what an upgrade keeps (PostgreSQL and its volume, the bucket, the secrets), how the
  `migrate` init container moves the schema while the previous pods keep serving, the
  order in which the pods roll, how to back up and check, and the way back — `helm
  rollback` one release, a restore further. Every command was run on minikube. The
  migration command (`python -m app.db.migra`) now speaks English.
- **Fixed: the Lineage page failed** as soon as the graph held a datasource or a flow
  the reader cannot read, or one deleted since.
- **Fixed: «Run» of a Script or Container node right after a change ran the flow as
  saved before it** (within a third of a second). The change is saved first.

## 1.2.1 «Appio» — 2026-10-21

Still «Appio». On Kubernetes, flows run code outside Tabularia: Python and R written in
the editor, in environments an administrator builds (the Script node), and images of
your own from your container registries (the Container node), each with access given
by group or user in the administration panel. A flow that uses what one may not use
does not open. The chart was tried on a three-node cluster with enforced network
policies and Pod Security, as a company runs it, and two guides come with it: minikube
to try Tabularia on your own computer, kind for a company-like cluster on one. The
Compose stack starts with three commands and nothing to configure.

- **Try it with three commands and nothing to configure.** `git clone`, `cd
  Tabularia/infrastructure`, `docker compose up -d`: no `.env`, no key to generate, no
  password to choose in a file. The Compose stack creates the key that encrypts the
  connection passwords at the first start, in a `keys` volume every service shares
  (`SECURITY__KEYS_DIR`; created once even when the services start together, readable by
  them only), and the sign-in page creates the first administrator while the
  installation has no account (`AUTH__FIRST_RUN_SETUP`; the stack answers on 127.0.0.1
  only, never in production, closed for good once any account exists, audited). The
  sample data is part of the stack: its container waits for that administrator and
  signs in with a start-up token the gateway leaves in the same volume (`POST
  /auth/bootstrap`), so the `Sample` folder appears a couple of minutes after the first
  sign-in. A key or a password set in `.env` still wins; on Kubernetes nothing changes
  (the chart sets both). **Upgrade note:** back the `keys` volume up with the database
  (the guide's backup step now includes it); an existing `.env` with its key keeps
  working as it is. To leave the sample data out: `docker compose up -d --scale
  sampledb-init=0 --scale sampledb-postgres=0 --scale sampledb-clickhouse=0`.
- **Fixed: the gateway never became healthy on a machine with images built before the
  `keys` volume** (*SECURITY__KEYS_DIR must be writable by the services*). `docker compose
  up -d` keeps images it already has; one without the keys directory left the new volume
  to root. A one-shot `keys-init` container now gives the volume to the services before
  any of them starts, whatever image or container mounts it first.

- **The Container node (Kubernetes)** — a step of a flow that runs an image of yours, as
  a Kubernetes Job ([docs/container-node.md](docs/container-node.md)): a registry, an
  image and a tag among those granted to you (the tags read from the registry), the
  command and arguments (or the image's own), variables, CPU and memory within the
  installation's limits (`containers.limits`), timeout and retries. The tag is resolved
  to its digest when the run starts and every attempt runs that digest; the registry's
  pull Secret is written right before the Pod needs it; the registry's variables (secret
  ones through a Secret) and the node's are in its environment. Input and output are
  optional, through the scripts bucket, as on the Script node; a node with neither gets
  no bucket credentials. The Pod runs at the `baseline` Pod Security level, without a
  service account token. A failure stops the flow; **Run** in the panel tries the saved
  node on the sample in the trials' namespace, and its output previews the nodes after
  it; **Runs** shows the image by digest, the command, the exit code and the log. A
  schedule set by someone else is switched off when the image, command or variables
  change; the dbt export refuses an output that reads what a Container node writes.
- **Container registries (Kubernetes)** — Administration → Container registries, where
  the images of the Container node come from
  ([docs/container-registries.md](docs/container-registries.md)). An administrator
  defines each registry: its address, HTTPS checked against the public authorities or
  your company's authority (PEM), HTTP for an internal registry, or a certificate not
  checked for a test one (recorded in the audit); a user and a password or token,
  encrypted and never read back; the repositories whose images may run (`team/forecast`,
  `team/*`, `*`); who may use it, groups or single users, each with all the repositories
  or some of them; variables and secrets every container of it finds. In the flow editor
  only what one may use is shown, and a flow that uses a registry or an image one has no
  access to does not open (nor its versions, copies, runs, schedules or groups of flows):
  Explore, Flows and the search list it greyed out, with a lock and the reason. The same
  now holds for a flow with a Script node of a scripting environment one may not use. **Test** signs in the way the registry asks (a token from its authentication
  service, as Docker Hub, GHCR, GitLab, Harbor and Scaleway do, or Basic), reads an allowed
  repository and keeps the result; a failure says which part to fix. **Try in the
  cluster** checks the nodes instead: a Pod downloads the image by digest and runs
  nothing of it, and a failure is explained (a certificate the nodes do not trust, HTTP,
  credentials refused). The chart creates the containers' namespaces at the `baseline`
  Pod Security level (`containers.namespace`, `containers.trialNamespace` with a quota)
  and Tabularia keeps a pull Secret per registry there. Migration 0018;
  `containers.enabled` in the chart (`CONTAINERS__ENABLED`).
- **A guide to a company-like cluster on one computer**
  ([docs/deploy/kind.md](docs/deploy/kind.md)): kind with three nodes, enforced network
  policies and Pod Security, a private registry with TLS signed by a certificate
  authority of your own and a password, nodes that trust it, PostgreSQL, Valkey and
  MinIO in their own namespace, Tabularia's images copied into the registry (or built
  from a branch), Python and R scripts built and run through it. Every command was run
  as written. `infrastructure/kind/storage.yaml` holds the object storage it applies.
- **Fixed (Helm chart): with `networkPolicy.egressCIDRs` set, the workers could not reach
  a broker or a storage installed in another namespace of the cluster.** The policy
  allowed in-cluster traffic to the release's own namespace only, so the workers timed
  out on Valkey while their pods looked ready. The namespaces of the broker, the storage
  and ClickHouse are now allowed from their Service host names, and
  `networkPolicy.egressNamespaces` lists any other.
- **Scripting on real clusters**, found by running the chart as a company would (three
  nodes, enforced network policies, a registry with its own certificate authority and a
  password):
  - a registry signed by your company's authority: `scripts.registry.caCert` makes the
    builds trust it; before, they failed to push (*x509: certificate signed by unknown
    authority*);
  - an image a node cannot download (refused credentials, an untrusted certificate, an
    image no longer there) now fails the run in about half a minute saying which, instead
    of waiting for the whole timeout and reporting that the script ran too long. A build
    or a check that cannot start, or a push the registry refuses, says why too;
  - each environment image records the architecture it was built for (migration
    0017), and its check and scripts run on nodes of that architecture: on a cluster
    with amd64 and arm64 nodes, a script no longer lands on the other one.
- **A guide to try Tabularia on your own computer with minikube**
  ([docs/deploy/minikube.md](docs/deploy/minikube.md)), for people who do not know
  Kubernetes: Windows, macOS and Linux, every command in full (PowerShell and bash where
  they differ), the data in a bucket of your cloud, optional ClickHouse and Python and R,
  and what to do when a step fails. `infrastructure/minikube/` holds what it applies:
  PostgreSQL and Valkey for one computer, and a settings file to fill in.
- **Fixed (Helm chart): a new key or password in the values was not used until the pods
  restarted by themselves.** The pods carried the ConfigMap's checksum but not the
  Secret's, so `helm upgrade` with a corrected `secrets.*` value left the old one running.
  The pods that read the Secret now roll when it changes; a Secret of your own
  (`existingSecret`) is still yours to roll.

- **Fixed: a run started from the Flows tab kept spinning until the page was reloaded,**
  and its end was never announced. The tab now follows the run it started: the row stops
  when it ends, and a message says whether it succeeded or why it failed. Flows already
  running when the page opened are refreshed quietly until they end.
- **Python and R in flows (Kubernetes): the Script node.** A flow step written in
  Python or R runs as a Kubernetes Job, outside Tabularia, in a scripting environment
  an administrator defined ([docs/scripting.md](docs/scripting.md)).
  - **Scripting environments** (Administration): a language version (Python 3.11–3.14,
    R 4.5–4.6) and a list of packages, built once into an image by a Job (rootless
    BuildKit) and checked by a second one; versions, an earlier one can be put back;
    variables of their own, secret or not, separate from Tabularia's; the groups that
    may use each one. Packages come from PyPI and CRAN (or mirrors), searched from the
    panel, with a warning for a Python version that has no wheel.
  - **The Script node**: its input (an edge in) and output (an edge out) are optional.
    The step before it is written to a bucket kept for the scripts, the script reads
    it with `TABULARIA_INPUT` and writes `TABULARIA_OUTPUT`, which the next nodes
    read; with no edges it simply runs as a step. Several files (modules next to the
    one that runs), retries, a timeout; a failure stops the flow; its run shows the
    attempts, the exit code, the log and the variables it had (secret ones by name).
    **Run** in its panel tries the saved node on the sources' sample in a few seconds,
    and the nodes after a script preview what it wrote last (a trial or the flow's
    run, saying which and whether the code changed since). Trials run in a namespace
    of their own with a ResourceQuota (`scripts.trialQuota`), never among the flows'
    runs; one that does not fit says so at once.
  - **Problems before running**: the code editor underlines what Ruff finds in Python
    (undefined names, syntax errors, unused imports) and the imports the environment
    does not have, when typing pauses, and marks the
    line where the last run stopped, with its log under the code (`SCRIPTS__LINT`).
  - **Image builds** run with their own CPU and memory (`SCRIPTS__BUILD_CPU`,
    `SCRIPTS__BUILD_MEMORY`) and as many compilers as those allow, never as many as
    the node has cores: an R environment with `arrow` and `forecast` no longer runs
    the node out of memory.
  - **dbt export**: a script is not dbt. An Output that reads what a Script node writes,
    however far down, refuses the export and says which; a Script node no exported
    Output reads from is left out, with a note in the project.
  - **The code editor** is Monaco (VS Code's), served by the frontend: the variables
    and the installed packages are listed and inserted with a click, and offered while
    typing, with the public names of each module after its alias.
  - **Safety**: no Tabularia key reaches a Job (the scripts bucket has its own, which
    should not list the bucket), the code may not name the scripts bucket, writing a
    script needs the environment's groups, and someone else's schedule stops when the
    code changes.

  On the chart: `scripts.enabled`, three namespaces of its own (scripts, trials,
  builds) and a service account limited to Jobs, their logs and events, and the code
  and variables they mount. Revisions 0015
  and 0016 add six tables and two nullable columns.

## 1.2.0 «Appio» — 2026-10-18

Third release, still «Appio». New sources: SFTP folders (in and out), HTTP APIs and S3
folders, all with one placeholder language for dates, ranges and file names, in
inputs and outputs alike. Flows run together in flow groups; API tokens, service
accounts and signed webhooks open Tabularia to other systems; the audit log can
stream to Splunk; engine settings and worker processes are set from the
administration panel. The editor gains an assistant, a minimap and a mark on the
steps already in the cache, and DuckDB and chDB read their sources in place. A
closing security round makes the administrator's password required (at least 12
characters, published ones refused), caps uploads, keeps client addresses for a
limited time and sends notices in the recipient's language. Before upgrading read
`docs/deploy/release-checklist.md`: this time the workers go before the gateway, and
`AUTH__ADMIN_PASSWORD` has no default any more.

- **Security closing round for 1.2.0.** **Upgrade note:** `AUTH__ADMIN_PASSWORD` has no
  default any more; the gateway refuses to create the administrator with an empty,
  published (`admin`, the guides' examples) or shorter-than-12-characters password, in
  every environment, and the development Compose refuses to start without one. An
  administrator still holding `admin` from an older release gets the configured password
  at the next start (its sessions close), or the gateway stops and says why;
  `AUTH__ALLOW_KNOWN_ADMIN_PASSWORD=true` keeps a fixed password on a local development
  stack only. **Uploads** have a ceiling, `UPLOAD__MAX_BYTES` (1 GiB, 0 = none): a 413
  before the engine is called, a chunked body cut at the ceiling, and the engine API as
  a second barrier. **Client addresses** are kept `AUDIT__IP_RETENTION_DAYS` (90) and
  then emptied from audit entries, tokens and sessions (the entries stay); deleting an
  account empties its addresses at once. **The client's address** is the one the server
  resolved (uvicorn's `FORWARDED_ALLOW_IPS` / the chart's `gateway.trustedProxies`): a
  forged `X-Forwarded-For` no longer lands in the audit log, sessions or tokens. **Sign-in**
  spends the bcrypt time on unknown emails too (the timing told which accounts exist),
  and one address failing on many accounts is slowed after 30 failures. **SMTP**
  verifies the server's certificate (`SMTP__VERIFY_TLS=false` to opt out for a
  self-signed relay). **API docs** load pinned Swagger UI / ReDoc versions checked by
  hash, under a CSP. Observers no longer read hosts, endpoints, IdP groups or emails in
  audit details, nor colleagues' emails in the editor's presence. SSO never grants or
  removes administrator or observer groups by name (administration only through
  `OIDC__SUPERUSER_GROUP`). Folders, user deletion, datasource moves, group creation
  and job stops are audited. The Helm egress policy accepts `{cidr, except}` and always
  excludes the cloud metadata address; the committed `.gitignore` covers every `.env`.
  **Schedules** keep to their author's rights: a scheduled flow or flow group checks,
  before it starts, everything it will need from its author (RUN on every flow it runs,
  read access to every source, RUN and CONNECT for every refresh, CONNECT on every
  connection it writes to, EDIT where it publishes); a gap fails the run with the
  reason and sends the failure notice. An edit, a version promotion or a move by
  someone else that widens what a flow or group reaches switches its schedule off,
  audited with the reason; adding a cell to a flow group needs RUN on the cell's
  folder. **One refresh per datasource at a time**: the manual refresh, the scheduler
  and a flow's Refresh node queue on a per-datasource lock (a second manual refresh
  gets 409, a scheduled slot is skipped, a flow waits for the running import), and the
  snapshot swap reads the datasource `FOR UPDATE`, so the newer snapshot always wins.

- **The SQL Server driver is no longer in the images** (as in 1.1.1). `mssql-python`
  depends on Microsoft's ODBC driver (`mssql-python-odbc`), proprietary and under a
  licence an AGPL-3.0 image cannot meet, so the published backend images are built
  without it and whoever reads from SQL Server adds it, accepting Microsoft's terms:
  `--build-arg WITH_MSSQL=true` (the new `sqlserver` extra), or a four-line Dockerfile
  on top of the published image, then the Compose or Helm image pointed at that build.
  Without the driver the type stays available, marked «not installed here» in the
  connection form with a link to the guide; a connection can be saved, and testing it,
  listing its tables or importing from it says, in the user's language, that the driver
  is not installed — no other database is affected. The engine reports its optional
  drivers (`GET /db/drivers`, relayed by the gateway at `GET /connections/drivers`).
  The release workflow now checks the licences of every package in the images and
  stops on a proprietary or AGPL-incompatible one; the development stack builds without
  the driver too (`WITH_MSSQL=true docker compose build` to add it). See
  `docs/sqlserver.md`.
- **One placeholder language for inputs and outputs, ranges, and S3 folders as
  datasources.** The placeholders of the API call — `{today}`, `{yesterday}`, `{now}`,
  offsets (`{today-7d}`, `{today-1m}` clamped, `{now-2h}`), `{start_of_month}`,
  `{last_refresh}`, readable (`yyyyMMdd`, `HH:mm`, `'T'`) or strftime formats, `epoch`,
  a time zone, escaped braces — now hold in the folder and glob of the SFTP, SharePoint and new
  S3 datasources, and in the SFTP Output (folder and file name, a dated folder created
  when missing), the S3 Output's key (and the copy's) and the email's subject and
  attachment name, with `{run_id}`, `{flow}` and a time zone on the Output node. The
  SFTP Output's `{date}`, `{datetime}`, `{run_id}`, `{flow}` write the same bytes as
  before; a resolved value never takes a name out of its folder. **Range variables**
  (`day = today-7 .. today step 1d`, the end excluded, `{day_end}` the end of each
  window) make one call or one expansion per value with `_param_<name>` on every row;
  a file missing for a value is skipped and listed on the run, none at all fails the
  refresh; at most `PLACEHOLDERS__MAX_VALUES` (1000) values. The file datasources can
  refresh **appended by key** like the API one. Templates are checked by the engine
  when a datasource is saved and before a run, and every form previews the resolved
  value while typing. The datasource **«S3 folder»** reads the objects of a bucket's
  folder through the S3 connection of the Outputs — CSV, parquet, Excel, NDJSON,
  `_file` and `_file_modified_at`, caps checked on the listing before downloading
  (`S3_SOURCE__MAX_FILE_BYTES`, `__MAX_FILES`, `__MAX_TOTAL_BYTES`, `__MAX_LISTED`),
  nothing outside the bucket and folder — with «Check the files», scheduled refresh,
  OpenLineage (`s3://bucket/folder/glob`) and the dbt export (a seed). Runs keep their
  notes (revision 0012). Braces now always mean a placeholder in those fields; the
  definitions stored before keep their meaning — revision 0013 writes their literal
  braces doubled, as escapes (email subject and attachment name, S3 key and copy key, SFTP
  Output folder, SFTP datasource folder and pattern, SharePoint path; the SFTP file
  name keeps its four old placeholders). An email Output refused at launch stops the
  flow's sequence like a failed send; an S3 copy whose key cannot be resolved is
  skipped and said on the run, never refusing the publish. See `docs/placeholders.md`,
  `docs/s3-datasource.md`.
- **HTTP APIs as datasources.** A connection of type HTTP API (base URL, default
  headers, and an API key in a header or in the query, a bearer token, basic, or
  OAuth2 client credentials with the token kept until it expires and asked again once
  on a 401; the secret encrypted like every other) reaches any REST/JSON API. The
  datasource «API call» makes a GET or POST at each refresh, scheduled too, with
  placeholders resolved in its own time zone — `{today-7d}`, `{today-1m}`, `{now-2h}`,
  `{start_of_month}`, `{last_refresh}`, formats such as `{today:%Y%m%d}` and
  `{now:epoch}` — and list variables that make one call per value, kept in
  `_param_<name>`. The answer (JSON, NDJSON, CSV) becomes a table: a records path, an
  optional JMESPath expression, nested objects as `a.b.c` columns, a list exploded into
  rows, an object keyed by date as one row per key (`_key`); a column with mixed types
  becomes text. Pagination by page, offset, cursor or next link; retries on 429 and
  5xx honouring `Retry-After`; a minimum interval between requests; every limit fails
  the refresh instead of cutting the data (`API__MAX_PAGES`, `API__MAX_CALLS`,
  `API__MAX_ROWS`, `API__MAX_RESPONSE_BYTES`, `API__MAX_TOTAL_BYTES`…). Each refresh
  rebuilds the table, or appends to it by key columns, replacing rows whose key comes
  again. «Try the call» shows the resolved request, the answer and the first rows
  before saving. Credentials and headers go only to the base URL's host — never to
  another host a redirect or a next link points to — and the secret is masked in every
  message, run and trial. In OpenLineage and in the dbt export (a seed). See
  `docs/api-datasource.md`.
- **SFTP connections: files in, files out.** A connection of type SFTP (host, port,
  user, password or private key, encrypted like every other secret) reaches any SSH
  server. The datasource «SFTP folder» reads every file of a folder that matches a
  glob (`vendite_*.csv`) — CSV, parquet, Excel, NDJSON — at each refresh, scheduled
  too, and stacks them into one table where every row says its file in `_file`; files
  that are not machine readable are refused, naming the file, and the files stay on the
  server. An Output node can write the flow's result to the server as CSV, parquet or
  Excel, under a name with `{date}`, `{datetime}`, `{run_id}` and `{flow}`, through a
  temporary name renamed in one step. The server's host key is accepted at the first
  «Test connection», which shows it without sending any credential, and «Accept this
  key», which sends them only to that key; every connection after checks it before
  any credential: a different key is refused until it is accepted again, and each
  acceptance is in the audit log. Caps per file, per refresh and in total
  (`SFTP__MAX_FILE_BYTES`, `SFTP__MAX_FILES`, `SFTP__MAX_TOTAL_BYTES`).
  See `docs/sftp.md`.
- **Error messages in English.** The engine's and the gateway's error messages — under a
  preview, in the run and import history, in toasts, in failure emails, at startup —
  were still in Italian in an otherwise English product: about 550 of them now read in
  English, with the same information and the same placeholders, and the five engines
  word the same error the same way. The dbt export's errors keep their codes, translated
  into the reader's language as before. A run stored earlier with the Italian «flusso già
  in esecuzione» is still recognised as a launch refused, not as a failure.
- **The assistants say, on every message, that they are an AI.** In the data assistant's
  page and in the flow editor's panel each answer is labelled «AI assistant» with the
  model that wrote it and each question as the user's (screen readers hear it too), and
  a notice that stays under the input says it is an AI that can make mistakes and that
  the results must be checked. A downloaded conversation opens with the same notice and
  labels who wrote what. The disclosure dialog before the first question was already
  there; a page read later, or by someone who never saw it, now says it as well
  (EU AI Act, article 50).
- **An assistant in the flow editor.** The right panel gets a second tab, Assistant,
  that builds the flow on the canvas with you: it reads the catalogue as the data
  assistant does, puts datasources on the canvas, adds and changes steps with the
  parameters the node panel shows, and checks each step on a preview of the data (with
  your rights, checked as the editor's previews are), fixing a step that fails before
  going on. What it built comes back as a whole and is applied as one Undo step; it
  never saves, runs or schedules the flow, and if you change the canvas while it works
  its version waits for your go-ahead. The conversation is kept with the flow and
  reopens with it; spending caps and the audit log (`ai.query`, via flow editor) count
  it like the data assistant's. Its disclosure shows only when the tab is first opened.
  Migration 0009 marks the conversations of each assistant.
- **Rename from the Explore.** Flows, flow groups, saved views, datasources and folders
  get a Rename button: the name becomes a field in its row, Enter or leaving the field
  keeps it, Escape gives up, and a name already taken in the folder is refused with
  the field still open. Renaming a datasource does not change the Output nodes that
  write it by name: update them, or their next run creates one with the old name.
- **OpenLineage follows renames and moves.** The folder path is part of the names
  flows and datasources have in a lineage catalog, so renaming or moving a flow, a
  datasource or a folder renamed them there only at their next run, and the old names
  stayed. Now the catalog gets them under their new names at once (datasources with
  the standard `lifecycleStateChange` RENAME facet naming the old one), together with
  the flows that read or write a renamed datasource, and on Marquez the old jobs and
  datasets are deleted through its API. Saving a flow or a folder without changing a
  name sends nothing. Folder and flow names can no longer be blank.
- **Duplicate from the Explore.** Flows, flow groups, saved views, connections and
  datasources get a Duplicate button: the copy lands in the same folder as «name (1)»,
  then «(2)», and a copy of «name (1)» takes the next free number, as a file manager
  does. It takes the rights creating one takes (CONNECT for connections, which keep
  their encrypted password, and for database datasources) and its author owns it.
  A flow's Output nodes are copied as they are, and the message says so: rename them
  before running the copy. A datasource's copy has its own data file, copied on the
  storage without downloading it, so refreshing or deleting either never touches the
  other; its data contract comes along, to be checked again. Schedules, failure and
  contract alerts stay with the original.
- **A minimap in the flow editor.** A small overview in the corner of the canvas,
  one shape per node in the colour of its kind and the visible part outlined, themed
  like the zoom controls: drag it to move the view, scroll on it to zoom. It comes from
  Vue Flow's own minimap package (MIT).
- **The flow editor shows which nodes are in the step cache.** An operation whose
  result is cached gets a soft ring set off its border, in each theme's positive
  green (Imperium: its gold), and an icon in its footer that says so in words: the
  nodes after it start from there. The editor asks the
  server (`POST /tasks/cache-status`, checked like a preview), which looks up the
  same fingerprints previews use, for the flow's engine, so the ring is right also
  for copies made by other users, gone once the cache expires, and gone as soon as a
  node upstream changes.
- **Cache copies give way to previews.** After a preview, the copy of its upstream
  step into the cache ran on the preview worker's slots in arrival order: during the
  review it took 39% of that worker's time, and clicks queued behind it. Copies now go
  to the preview queue at the lowest priority, only one runs at a time, and a copy
  that finds previews waiting gives up (the next preview of that node asks for it
  again). The queue page counts every priority list, so waiting copies show there.
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

## 1.1.1 «Appio» — 2026-10-18

**The SQL Server driver is no longer in the images; how to add it.** `mssql-python`
(MIT) depends on `mssql-python-odbc`, Microsoft's ODBC Driver 18 for SQL Server:
proprietary, under a licence that allows redistributing it only on terms an AGPL-3.0
project cannot meet. The 1.1.0 images carried it; the 1.1.1 images do not. Nothing
else changes from 1.1.0.

- **Adding the driver.** Whoever reads from SQL Server adds it to their own backend
  image and accepts Microsoft's licence directly: build from the release tag with
  `--build-arg WITH_MSSQL=true` (the new `sqlserver` extra of the backend, at the
  versions in `uv.lock`), or extend the published image with
  `pip --python /opt/venv/bin/python install "mssql-python==1.15.0"`, then point the
  engine API, the workers and beat at that image (Compose `image:`, Helm
  `image.backend`). The system libraries the driver needs stay in every image. See
  `docs/sqlserver.md`.
- **Without the driver** a SQL Server connection can still be saved; testing it,
  listing its tables or importing from it says that the driver is not installed, with
  the guide, instead of failing on an import error. No other database is affected.
- The development stack builds without the driver too, like the published images
  (`WITH_MSSQL=true docker compose build` to add it). Helm chart 2.0.1 installs 1.1.1.

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
