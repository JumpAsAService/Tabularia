# Spec: dbt export, piece 3 — the ClickHouse target and the download options

Status: approved, 2026-10-10 (Q1–Q3 answered by the owner, see the end). Decisions marked **(owner)** were taken by the project
owner; the rest are assumptions to correct.

> **Changed after the bug hunt of 2026-10-15/16** — see the section at the end. Where this spec and
> [docs/dbt-export.md](../dbt-export.md) disagree, the docs describe the code.

## Objective

**(owner)** "If a flow built by a business user becomes business-critical, the data
team must be able to integrate it in the least time possible." Pieces 1 and 2 made the
exported project give the same data as Tabularia and read like the flow. Piece 3 makes
it land where the data team works:

- **in their warehouse.** **(owner)** 99% of customers run the ClickHouse external
  engine; for dbt users ClickHouse comes first, then BigQuery, then DuckDB (BigQuery
  postponed). Today a customer with the ClickHouse engine and data in Postgres gets a
  DuckDB or a Postgres project, never a ClickHouse one.
- **reading the source databases live, not Tabularia's parquet.** **(owner)** Drift
  between Tabularia's last refresh and dbt's live read is accepted.
- **in the shape of their repository.** A complete project, or a folder to drop into
  the project they already have, with their names for sources, folders, schema and
  materializations.

Users: an administrator acting for the data team. **(owner)** The dbt export is
admin-only.

Success: the data team takes the zip and either runs `dbt build` on it as a project,
or copies one folder into their repository and runs `dbt build -s tag:<flow>` without
editing a generated file. On ClickHouse, the result is the data Tabularia publishes.

## Capability map

| Module id | Responsibility | Depends on |
|---|---|---|
| `dbt-clickhouse-target` | A third target: dbt-clickhouse on the customer's ClickHouse, Postgres/MySQL sources read live through ClickHouse database engines | — |
| `dbt-download-options` | An options dialog and an options object from the browser through the gateway to the engine: target, package shape, names, schema, materializations, tests, provenance | `dbt-clickhouse-target` (only for the list of targets) |

Build order: `dbt-clickhouse-target` → `dbt-download-options`. Each one is closed with
its own verification and reported, like pieces 1 and 2.

---

## Module `dbt-clickhouse-target`

### What exists today

| Target | Where dbt runs | Sources |
|---|---|---|
| federated | dbt-duckdb, a local file | Postgres/MySQL ATTACHed and read live; ClickHouse refused |
| native | the source database itself | all on ONE connection |
| **clickhouse** (new) | the customer's ClickHouse | Postgres/MySQL live through ClickHouse; tables of that ClickHouse directly |

### Checked on 2026-10-10 (local ClickHouse 24.8 + the sample Postgres)

- `CREATE DATABASE x ENGINE = PostgreSQL('host:port', 'db', 'user', 'password', 'schema')`
  shows every table of the schema as a ClickHouse table, read live (8 rows of
  `zzdbt.casi`). Types are `Nullable(...)` except NOT NULL columns. `SHOW CREATE
  DATABASE` prints the password as `[HIDDEN]`.
- Named collections, which keep the password out of the DDL, need a grant the engine's
  user does not have. On a managed ClickHouse it may not be available at all.
- **With the default `join_use_nulls = 0` the missing side of an outer join is 0 or ''
  on a non-Nullable column, not NULL.** The engine never shows it, because everything
  it reads from parquet is Nullable. A model on ClickHouse must run with
  `join_use_nulls = 1`. The same holds for today's *native* ClickHouse export, which
  reads MergeTree tables: probably a latent difference there. To be proved with the
  oracle and fixed in this module.

### Design

- **Target** `clickhouse` (`?target=clickhouse`, and in the dialog).
- **Profile**: dbt-clickhouse on the engine's ClickHouse external (host, port, secure
  from `CLICKHOUSE_EXTERNAL__*`, which the engine knows). User and password are taken
  from `env_var('CLICKHOUSE_USER')` / `env_var('CLICKHOUSE_PASSWORD')`, never the
  engine's credentials. Settings: `custom_settings: {join_use_nulls: 1}`. Models land
  in the ClickHouse database `dbt_tabularia` unless the dialog says otherwise.
- **Sources**
  - A Postgres / MySQL / MariaDB connection becomes one ClickHouse database per
    (connection, schema), `tab_src_<connection>_<schema>`, with the PostgreSQL / MySQL
    engine. `sources.yml` points at it, so models use a plain `source()` and the dbt
    docs show the lineage.
  - A ClickHouse connection to the same server as the target: its database, directly.
    To another ClickHouse server: refused with a clear message (v1).
  - Files and SharePoint: seeds, as today.
  - A datasource defined by a SQL query: **(owner, Q3)** the data is defined in
    ClickHouse through the federated databases and from there on everything is
    ClickHouse. The query, written for Postgres/MySQL, is translated with sqlglot into
    ClickHouse SQL over the `tab_src_*` databases; the README marks it "translated,
    review". A query that does not translate is refused with the reason.
- **Creating the source databases** — **(owner, Q1)** a dbt command:
  `dbt run-operation create_tabularia_sources --log-level-file none`. A macro in the
  project runs `CREATE DATABASE IF NOT EXISTS … ENGINE = PostgreSQL(…)` / `MySQL(…)`
  for every source, the password from `env_var('TABULARIA_DB_<id>_PASSWORD')`, as the
  other targets already do. dbt writes the SQL it runs into `logs/dbt.log` only at
  debug level: the README's command turns the file log off for this one step.
- **SQL**: the native path with dialect `clickhouse`, the one today's native ClickHouse
  export already uses. The first step of every model casts the sources to Tabularia's
  types as `Nullable(...)`, like the engine reading parquet. Pivot is refused (its
  columns are decided by the data), as in native.
- **Network**: the customer's ClickHouse must reach their Postgres/MySQL. A managed
  ClickHouse reaching a database on a private network may need work on their side;
  the README says so, and the federated target remains for that case.

### Acceptance

1. The matrix runs its 60 cases plus pivot on the **clickhouse** target too (the local
   ClickHouse reading `zzdbt.casi` from the sample Postgres). Every case gives the
   same as Tabularia's ClickHouse engine, or both refuse.
2. `ciclo.py` on clickhouse: every contract rule gets the same verdict in Tabularia
   and in dbt. Lifecycle parse / compile / docs / run ×2 / `--full-refresh`, with the
   append output as an incremental model on ClickHouse.
3. The Sample flows 1, 2, 5, 6, 8, 10, 11 on clickhouse: same data as Tabularia; 5
   and 8 refused because of the pivot.
4. The native ClickHouse export gives NULL on outer joins: a case in the oracle,
   proved first and then fixed.
5. MySQL: unit tests on the generated DDL and sources. A live check only with a
   throwaway MySQL container (the samples have no MySQL).

---

## Module `dbt-download-options`

The Export dialog in Flows today has two dbt buttons (federated, native). They become
one entry, **dbt project…**, which opens an options form. OpenLineage stays as it is.

### Options (proposed — the owner cuts)

| # | Option | Default |
|---|---|---|
| 1 | **Target**: federated / native / ClickHouse. A target that cannot work is disabled, with the reason (e.g. "sources on more than one connection") | the one that works, ClickHouse first |
| 2 | **Package**: complete project · folder for an existing project (`models/<folder>/`, `seeds/<folder>/`, `tests/<folder>/`, no `dbt_project.yml` or `profiles.yml`; an `INTEGRATION.md` says what to add to theirs) | complete project |
| 3 | **Folder** and **model prefix** (e.g. `tab_`) | flow name, no prefix |
| 4 | **Layers**: `src_` models in `staging/`, `int_` in `intermediate/`, outputs in `marts/` | off |
| 5 | **Sources**: for every (connection, schema), the source name to use, and "already declared in our project" → no entry in `sources.yml` (otherwise dbt fails on a duplicate source) | today's names |
| 6 | **Schema** of the output models | `dbt_tabularia` |
| 7 | **Materialization per output**: table / view / incremental (append outputs only) | from the flow, as today |
| 8 | **Contract tests**: include / leave out | include |
| 9 | **Provenance emails** (who built the flow, who exported it): include / leave out | include |

Not in piece 3: AI descriptions (piece 4), export as a pull request.

### Interfaces

- `GET /flows/{id}/export/dbt/plan` (admin): what the dialog needs, computed by the
  same code as the export without calling the engine:
  `{targets: [{id, available, reason, sources: [{key, connection, schema, default_name,
  default_base, tables}]}], outputs: [{model, label, materialized, materializations}]}`.
  `default_name` is a source's name with nothing typed; `default_base` the unsuffixed
  default it comes from, which the dialog suffixes again (`_2`, `_3`) as the export does
  when typed names change the collisions.
- `POST /flows/{id}/export/dbt` with the options as JSON. `GET` stays and means
  "defaults": no output change for whoever uses it today.
- The last options used for a flow are remembered in the browser (localStorage), a
  convenience only.

### Acceptance

1. The folder package copied into a fresh `dbt init` project, and into the project of
   the module above, builds with `dbt build -s tag:<flow>` with no edits. With a
   source marked "already declared" and the team's `sources.yml` present, there is no
   duplicate.
2. Each option changes exactly what it says: unit tests per option and on the
   structure of the zip.
3. The `GET` export without options gives the same files as today, apart from the
   export time.
4. Browser: an admin opens the dialog, changes options, downloads; a non-admin does
   not see it. Keyboard and labels as in the other dialogs; `.head-actions` untouched.
5. `giro`, `dbt_giro`, `sicurezza_export`, the matrix and `ciclo` stay green.

---

## Commands

```
engine:    docker exec -e CLICKHOUSE_TEST_HOST=chtest -e CLICKHOUSE_TEST_USER=tab -e CLICKHOUSE_TEST_DB=tab \
             -e CLICKHOUSE_TEST_PASSWORD=… dataprep-backend python -m pytest tests -q
gateway:   docker exec dataprep-gateway python -m pytest tests -q
typecheck: vue-tsc pinned, against .nuxt/tsconfig.json (memory: frontend-typecheck-command)
scratch:   ~/tabularia-carico/dcp build backend gateway frontend && dcp up -d --no-deps …
oracles:   ~/tabularia-carico/dbt_matrice/matrice.py --motore clickhouse [--solo clickhouse]
           ~/tabularia-carico/dbt_matrice/ciclo.py · dbt_giro.py · dbt_prova.py <flow> clickhouse
           giro.sh · sicurezza_export.py · a new prova_dbt_dialogo_browser.py
```

## Boundaries

- **Always**: tests with every slice and the full regression before reporting; the
  local ClickHouse (`chmotore`), never Scaleway (off by the owner's choice); interface
  text in English through i18n keys; admin-only.
- **Ask first**: a new dependency (none expected: dbt-clickhouse lives only in the
  throwaway test image); any change to what the plain `GET` export produces; a new
  table or column in the gateway database (none expected).
- **Never**: a password in a generated file; a commit or a push without being asked;
  experiments on the production ClickHouse; wrapping `.head-actions`.

## Questions answered by the owner (2026-10-10)

- **Q1 — who creates the source databases on ClickHouse.** (a) A
  `setup/clickhouse_sources.sql` in the zip, run once by whoever administers
  ClickHouse, with a placeholder for each password: dbt never sees the source
  passwords. (b) A dbt macro, `dbt run-operation create_tabularia_sources`, reading
  the passwords from env vars: one command, but the password goes through dbt and
  ends up in `logs/dbt.log`.
- **Q2 — which options go in the dialog** (the table above). **Answer: all of them.**
- **Q3 — datasources defined by a SQL query, on ClickHouse.** The query is written in
  Postgres or MySQL SQL, and ClickHouse has no pass-through for it. (a) Translate it
  with sqlglot, with its tables pointing at the `tab_src_*` databases, flagged
  "translated, review" in the README; refuse if it does not translate. (b) Refuse,
  and point to the federated target. **Answer: the owner's model is "define the data in
  ClickHouse with federated reads, then only ClickHouse" — which is (a).**

## Changed after the bug hunt (2026-10-16)

A hunt with real dbt found defects in what this spec designed; the fixes changed these points.

- **Source databases on ClickHouse.** Not `CREATE DATABASE … ENGINE = PostgreSQL/MySQL` any more: the
  macro creates an ordinary database per (connection, schema) and in it one table per table the project
  reads, `ENGINE = PostgreSQL/MySQL`, with types that keep every value (`Date32`, `DateTime64(6)`, a
  Postgres `numeric` as `Float64`). The ClickHouse user needs `CREATE DATABASE`, `CREATE TABLE` and
  `DROP`; the macro runs again when a source table changes its columns; a schema that is not a plain
  lowercase name gets a fingerprint in the database name. Every cast on ClickHouse writes `Date32` and
  `DateTime64(6)`, a time of day as text.
- **Profiles.** Both ClickHouse profiles set `join_use_nulls: 1` and `max_query_size: 16777216` (a model
  bigger than that is refused as `model_too_large`); the native ClickHouse profile uses TLS on 8443/443 as
  Tabularia does. The federated profile pins `TimeZone: UTC` and reads the source passwords through DuckDB
  secrets, never in an `ATTACH` DSN.
- **Native target on MySQL/MariaDB: removed (owner).** MySQL/MariaDB stay sources of the federated and the
  ClickHouse targets.
- **Folder package.** The command is `dbt build -s +tag:<flow>,tag:tabularia` (only what came from
  Tabularia; seeds are tagged `tabularia` too). The Schema option exists only for the complete project
  (owner); "already declared" only for the folder package (owner). The default folder is an ASCII slug of
  at most 40 characters, `tabularia` when nothing is left (owner).
- **Names.** A prefixed model name is the prefix plus the name the plan shows (owner); names fit Postgres'
  63 bytes with any prefix. Option 6's default is `dbt_tabularia` on the native and ClickHouse targets and
  `main` on dbt-duckdb.
- **Upstream flows (owner).** Only the branch read downstream comes into the project; an upstream model
  never writes into the upstream flow's tables.
- **Native database outputs (owner).** An output on another connection than the one dbt runs on is an
  ordinary model in the project's schema; on the same connection it keeps the real table, a one-part
  name in the connection's schema — only when it is materialized as Tabularia writes it (`table` for
  replace, `incremental` for append) and, on Postgres, when dbt-postgres can rebuild its name (63 bytes);
  otherwise it is built in the project's schema with a README note.
- **Sources no dbt target reads (Oracle, SQL Server, Trino).** Refused with the reason, except that the
  ClickHouse target takes their tables into the folder package when the team's project already declares
  them: the plan marks that target with `requires` and the dialog offers it there, the source ticked
  «already declared» and locked.
- **Names dbt cannot write.** A name the dbt adapter writes unescaped into its own SQL and cannot hold
  (a double quote; on ClickHouse a backtick or a backslash; in a table or schema dbt writes, a single
  quote, and a backslash in a Postgres schema) is refused (`name_not_quotable`); a `"` in a column of a
  dbt-clickhouse table is refused (`column_name_unsupported`). Names in `config()` are written so that
  dbt's static parser reads them exactly.
- **Acceptance 1 (`dbt build -s tag:<flow>`)** became the selector above; the oracles now also compare
  type families, tell an empty text from NULL and run dbt outside UTC.
