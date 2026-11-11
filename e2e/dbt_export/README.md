<!-- {% raw %} GitHub Pages: no Liquid in this page -->
# dbt export — end-to-end oracles

These scripts check that a flow exported as a dbt project gives **the data Tabularia
publishes**. They run a real dbt (in a container, or in a pod on Kubernetes) against a
running Tabularia and compare, model by model, what dbt builds with what the app wrote.
They are the checks behind [docs/dbt-export.md](../../docs/dbt-export.md); run them after
any change to the exporter (`backend/app/engine/dbt_export.py`,
`backend/app/api/routes/dbt.py`, the Go gateway's `gateway-go/internal/dbtexport/` and
`gateway-go/internal/api/flows_dbt.go`, and — for the exports with the AI options, which
the Go gateway hands to the AI service — `gateway/app/services/dbt_export.py` and
`gateway/app/services/dbt_ai.py`) or to the engines it mirrors.

| Oracle | What it checks |
|---|---|
| `matrix.py` | Every operation of the editor (≈60 cases, tricky data: NULLs, spaces, decimals, dates, reserved and accented names) gives the same columns in the same order, the same family of types (integer, float and decimal apart — an integer that comes back as a float, or a sum left as a 128-bit HUGEINT, is a difference — text, date, datetime, bool) and the same rows in dbt as in Tabularia, on the three targets (DuckDB federated, native Postgres, ClickHouse). An empty text is not NULL and a text is compared as text (`'200'` is not `'200.0'`); dbt runs in `Europe/Rome` (`E2E_TZ`). The known types of native Postgres for `ROUND` and `EXTRACT` in a compute (a `numeric`) are allowed by name, case by case. Also: chains after a compute, a sort and a pivot (joined back on its index with `left_on`/`right_on` too), the mean, deviation and variance of an integer column, a text on several lines (LF, CRLF, a lone CR, a tab) in a compute, a filter value, a fill_null value, a sql node and a column name; a table with a `timestamptz`, an unbounded `numeric` and empty texts; a file read as a seed with the texts dbt's seed loaders change |
| `lifecycle.py` | A violated data contract gives the same verdict, rule by rule, in Tabularia and in `dbt test` (whole-value patterns, also on integer and date columns; accepted values with an apostrophe or `3.0` on an integer column; a unique column with two NULLs; not_null on names with spaces, capitals or reserved words; two rules on one column); `parse` / `compile` / `docs generate` without deprecations; an append output is an incremental model (run, run, `--full-refresh` → 8, 16, 8 rows); the source passwords never land in `logs/` or `target/`, also on the federated target with a wrong password or a source that does not answer; provenance in the manifest; native ClickHouse, with NULL on the missing side of an outer join |
| `folder_package.py` | The "folder for an existing project" package, copied into a team project with its own model (tagged like the flow) and its own `sources.yml`, with the profile blocks `INTEGRATION.md` gives, builds with the command it gives (`dbt build -s +tag:<flow>,tag:tabularia`), selects none of the team's models, and gives the same data; without "already declared" dbt rejects the duplicate source |
| `sample_flows.py` | Every Sample flow, on every target its export plan allows, run in dbt and compared with its published datasources |
| `deterministic.py` | The same flow version with the same options gives the same zip, byte for byte, twice in a row and after restarting gateway and engine (also a drop of several columns); exported by another administrator, only `README.md` differs (it names who exported), and with the emails option off nothing does |
| `round_trip.py` | A round with an upstream flow, shared steps, an append table, a query datasource, an uploaded file and a contract: `dbt seed/run/test` in two targets. The downstream export brings only the branch it reads of the upstream flow; the upstream export has the shared step as an ephemeral model, and on native its append model writes the very table Tabularia wrote (one `dbt run` doubles its rows) |
| `security.py` | The dbt export is for administrators only; Jinja typed in a flow (`{{ env_var(…) }}`, `{% … %}`, `{# …`, `log(…, info=True)` in a filter value, a compute expression, a column name and the output table's name, with a backslash) never runs: no marker that only running Jinja prints on the console, in `logs/` or `target/`; with dbt's static parser on, the model's alias and schema are the table typed in the flow, and `dbt run` writes that very table, with the texts as typed. A quote in a native output's table name (`o'brien`, which dbt-postgres cannot write) is refused with `name_not_quotable`: by the plan, before the download, and by the download |
| `clickhouse_paths.py` | The ClickHouse paths against Tabularia's ClickHouse engine: a Postgres table and a Postgres query datasource on the ClickHouse target, a table of the engine's ClickHouse on the ClickHouse and native targets, both in one flow, a file and an Excel sheet read as seeds by dbt-postgres' and dbt-clickhouse's loaders, a seed of exactly the cap and one more row. Data the engines read differently: -0.0 and 0.0 as keys, NaN, a Float32, dates out of 1970-2149, zoned DateTimes, Decimal, UInt64 at its maximum, the calendar's filter value, Postgres' integer division, `::numeric`, `::int` and `date_trunc`; the queries whose numbers the export cannot tell are refused (`query_not_translatable`). Run on its own (`python3 clickhouse_paths.py`); everything it makes is `zzc…` |
| `native_targets.py` | The angles the others do not reach: an Output into a table that exists already on native Postgres and native ClickHouse (what Tabularia keeps of it, what dbt's rebuild changes — documented, the model's header and the README say it — and an append on ClickHouse, which dbt-clickhouse does by position); the data contract rule by rule on every target (patterns with `\b` and POSIX classes, accepted values on floats, dates and booleans, unique with NULLs, ranges on instants with an offset or without seconds); MySQL/MariaDB sources (a throwaway MariaDB in `E2E_MYSQL_NAMESPACE`, default `zz-hunt-mysql`): DECIMAL, TINYINT(1), BIT, zoned DATETIME/TIMESTAMP, a case-insensitive collation, ENUM, SET, JSON, emoji, on the federated and the ClickHouse targets. The import's own limits on MySQL ('0000-00-00' among real dates, a BIGINT UNSIGNED over 2^63) are printed, not checked. Everything it makes is `zzn…`; parts: `dest_pg dest_ch contracts mysql` |
| `codes.py` | Every error code the gateways and the engine raise (`DbtExportError` in the exporter and the engine route; `refuse(…)` and `Error{Code: …}` in the Go gateway; the AI service's export and AI translation in the Python gateway), and every reason of a refused AI translation, has its sentence, with its parameters, in the five languages of the Go gateway's catalogue (`messages_gen.go`), which says the same as the AI service's (`dbt_messages.py`); no sentence is left that nothing raises |
| `drift.py` | What the Go gateway mirrors of the engine's exporter, so that the plan says before the download what the engine would refuse, has not drifted apart (read in the code with `ast` and [`golit.py`](golit.py), no stack and no Go toolchain needed): the parameters each step requires (`required` = the `_require` calls of `_compile_op` and what the helpers it calls require, `pivot_params`), the seed cap, the databases the ClickHouse, federated and native targets read, the dialects the gateway sends and the engine accepts, the characters refused in a name, role by role (a source's schema and table, a model's table, a model's schema). The same for the AI service's Python copy, which still builds the exports with the AI options |
| `browser_dialog.py` | The export dialog in Firefox: options, validation, the downloaded zip, remembered choices, refusals in the interface language, the AI options when a model is enabled |
| `browser_dialog_state.py` | The dialog's state, with the export answered inside the page (one real download): a download cancelled by Esc, ×, Cancel or leaving the page never reaches the next flow's dialog; Schema and «already declared» only sent for their package; an empty folder remembered empty; each field's rule with `aria-invalid`/`aria-describedby`; duplicate source names flagged, and each source's default the name the gateway gives (the plan's `default_base`); the file name from `filename*` or a fallback that keeps any letter; the focus back on the row's Export button; ClickHouse with a source it cannot read chosen in the folder package with that source «already declared» and locked, disabled in the complete project; the AI texts kept for the flow version offered with the AI off, sent and remembered, and the hint naming the models that wrote them. The gateway's plans are served without the AI options and the AI checks serve their own plans with the export answered inside the page: no paid call |
| `ai_descriptions.py`, `ai_translation.py` | The optional AI with the real provider: descriptions, and a Postgres query that sqlglot translates badly (`SIMILAR TO`) rewritten by the AI, checked, run in dbt on ClickHouse. **Paid calls** |

## What they need

- A running Tabularia with the **sample data** (the Postgres ERP and the ClickHouse CRM
  that `sampledb-init` loads, with its eleven flows in the folder *Flows*), and an
  administrator account.
- The **ClickHouse engine** configured (`CLICKHOUSE_EXTERNAL__*`): the ClickHouse target
  runs dbt-clickhouse on it, and it must reach the sample Postgres.
- Docker on the machine that runs the oracles (dbt runs from the image in
  `Dockerfile.dbt`; `run_all.py` builds it if missing).
- For `browser_dialog.py` and `browser_dialog_state.py`: Firefox (headless, Marionette on
  port 2828, or `E2E_MARIONETTE_PORT`).
- For the AI oracles: the assistant configured and a model enabled by the administrator.

Everything that depends on the installation is read from environment variables, with
defaults for a local stack — see [`environment.py`](environment.py). The usual way is to
point `TABULARIA_ENV` at the stack's `.env`, which carries the admin credentials and
the ClickHouse settings:

```bash
TABULARIA_ENV=/path/to/stack/.env TABULARIA_URL=http://localhost:8088 \
  python3 e2e/dbt_export/run_all.py            # the complete suite, ~40 minutes
python3 e2e/dbt_export/run_all.py quick       # ~5 minutes
python3 e2e/dbt_export/run_all.py hunt        # clickhouse_paths and native_targets
python3 e2e/dbt_export/run_all.py matrix lifecycle
python3 e2e/dbt_export/run_all.py ai           # paid calls to the AI provider
```

`run_all.py` checks the prerequisites first (API and login, containers, dbt image, the
case tables of `schema.sql` in the sample Postgres), then runs each oracle, writes its
log in `E2E_WORKDIR` (by default `e2e/dbt_export/.work/`, ignored by git) and prints
a summary; it exits with an error if one is not green. Each oracle creates what it
needs (folders, flows, datasources named `zz…`) and removes it at the end.

The determinism oracle restarts gateway and engine only if `E2E_RESTART` holds the
command that does it (for example `docker compose … restart gateway orchestrator
backend worker`); without it, it checks two exports in a row.

## On Kubernetes (the kind test stack)

With `E2E_KUBE_CONTEXT` set the oracles run against a Tabularia on Kubernetes, with no
`.env` to point at: the admin's credentials come from the secret `sampledb-admin`, the
engine's ClickHouse from the app's ConfigMap and Secret (read by the scripts, never printed
nor written to a file).

- dbt runs in **one pod**, `zz-dbt-e2e`, in the app's namespace (the NetworkPolicies of the
  sample databases let that namespace in). The pod meets the `restricted` Pod Security level
  the kind guide puts on that namespace: not root, no privilege, the project in a volume.
  `run_all.py` loads the image on one node only
  (`kind load docker-image … --nodes <node>`, `E2E_KUBE_NODE`), starts the pod there and, at
  each dbt call, sends the project folder in and takes it back as dbt left it (target/,
  logs/, the `.duckdb` file) with `tar` through `kubectl exec`; the variables travel in a
  file outside the project, removed before dbt starts.
- The sample databases are deployments (`sampledb-postgres`, `sampledb-clickhouse` in
  `tabularia-deps`), reached with `kubectl exec`; the engine's ClickHouse is the deployment
  behind `CLICKHOUSE_EXTERNAL__HOST` (on kind, the sample ClickHouse itself).
- A cluster built with [the kind guide](../../docs/deploy/kind.md) and MinIO has no ClickHouse
  engine (its step 12 is for a cloud bucket). For the oracles, give it the sample ClickHouse of
  step 13, which reads MinIO with the keys Tabularia sends: a second values file with
  `externalServices.clickhouse` (`enabled: true`, `host:
  sampledb-clickhouse.tabularia-deps.svc.cluster.local`, `port: 8123`, `secure: false`,
  `database: tabularia`, `username:` the sample's user, `transport: s3`, `s3Endpoint:` MinIO's
  address) and `secrets.clickhousePassword`, after `CREATE DATABASE IF NOT EXISTS tabularia`
  on that ClickHouse, then `helm upgrade` with both files.
- The ClickHouse target needs the engine's ClickHouse to reach the sample Postgres, which
  the `sampledb` NetworkPolicy does not allow: `run_all.py` adds one of its own,
  `<dbt pod>-to-<service>` (that pod to that port, nothing else), labelled with the run's
  owner (`E2E_OWNER`, by default the dbt pod). The cleanup removes that run's policies and
  pod only, so two runs with two pods (`E2E_DBT_POD`) can go in parallel; each oracle that
  hunts on its own names what it makes `zz<letter>…` and cleans only that, and the suite's
  ClickHouse cleanup leaves those databases alone (`common.suite_database`).
- On kind a dbt run that hits the small sample ClickHouse's memory limit
  (`MEMORY_LIMIT_EXCEEDED`) can be waited out and run again: `run_dbt_image(…, retry_memory=True)`.
- At the end `run_all.py` removes the pod and that policy (`E2E_KUBE_KEEP=1` keeps them for
  the next round; `python3 run_all.py cleanup` removes them). The image stays on the node.
- The sample ClickHouse of kind has a 2 GiB memory limit, and the sample ERP there has 42
  million order lines: `E2E_CH_SETTINGS` adds to the dbt-clickhouse profiles what a team
  with a small ClickHouse adds (a join that spills to disk). Even so, the ClickHouse target
  of the flows that join the order lines several times (Margin by Category and Month, Cost
  to Serve by Channel, Sales by Weekday and Channel, and the folder package built on the
  first) runs out of memory there: a limit of that stack, not of the export.
- `E2E_WORKDIR` under your home: the browser oracles keep Firefox's profile there, and the
  snap's Firefox has a `/tmp` of its own.

```bash
export E2E_KUBE_CONTEXT=kind-tabularia            # interface on http://localhost:8080
export E2E_CH_SETTINGS=join_algorithm=grace_hash,grace_hash_join_initial_buckets=16,max_bytes_before_external_group_by=300000000,max_bytes_before_external_sort=300000000,max_threads=2
export E2E_RESTART='kubectl --context kind-tabularia -n tabularia rollout restart deploy/tabularia-tabularia-gateway deploy/tabularia-tabularia-engine && kubectl --context kind-tabularia -n tabularia rollout status deploy/tabularia-tabularia-gateway --timeout=300s && kubectl --context kind-tabularia -n tabularia rollout status deploy/tabularia-tabularia-engine --timeout=300s'
python3 e2e/dbt_export/run_all.py complete browser    # everything but the paid AI calls
```

## Reading a failure

Each line of a log is one check: `ok` or `✗` with what differed. In the matrix a
difference prints the rows only Tabularia has and the rows only dbt has. A case that
Tabularia itself refuses must fail in dbt too — that is parity, not a defect. Known
differences between the engines are listed in
[docs/engines/engine-differences.md](../../docs/engines/engine-differences.md).
<!-- {% endraw %} -->
