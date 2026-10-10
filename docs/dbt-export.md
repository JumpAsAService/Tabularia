# Export a flow as a dbt project

A flow designed in Tabularia can leave as a **dbt project**: a zip with one model per
output, the shared steps as their own models, `sources.yml`, `schema.yml` with the
columns, their descriptions and the data contract as dbt tests, seeds for the files
the flow reads, and a README that says how to run it. Put it in a repository, run it
in your CI, read it in your catalog: the flow is no longer locked in the tool.

It is an **exporter, not a second engine**: Tabularia keeps running the flow as it
does; the dbt project is a faithful, versionable copy of the same transformations
that you own from then on.

## Three targets

| Target | Where the SQL runs | Needs | When |
|---|---|---|---|
| **ClickHouse** | In your ClickHouse — by default the one Tabularia computes on. Postgres and MySQL sources are read live through ClickHouse database engines | `pip install dbt-clickhouse`; a ClickHouse user that can `CREATE DATABASE`; ClickHouse must reach the source databases | The usual case: the ClickHouse engine, data in Postgres/MySQL (any number of connections), files as seeds |
| **dbt-duckdb** (federated) | In DuckDB, which attaches the source databases and reads their tables live | `pip install dbt-duckdb`; network access to the sources | Sources on PostgreSQL, MySQL or MariaDB, on any number of connections; any operation of the editor |
| **Native warehouse** | Inside the source database itself (dbt-postgres, dbt-mysql, dbt-clickhouse) | The adapter of that database | All sources on the **same** connection; the result lands in that database, next to the data |

**The project gives the data Tabularia publishes.** The first step of every model
casts the source columns to the types Tabularia read them with, so the project sees
the data as the flow does even where the warehouse types them differently. The
federated target then runs every operation with the very SQL functions of
Tabularia's DuckDB engine (the cast that trims text and truncates numbers, the
predicates, the `NULLS LAST` of sorting, the join that fills the key of right/full
joins, the pivot labels) — imported, not copied. The native target translates to the
warehouse's dialect with sqlglot and replaces what the dialect lacks with equivalent
portable forms: casts guarded by a pattern instead of `TRY_CAST` (a bad value gives
NULL, as in Tabularia, instead of failing the model), `strpos`/`left`/`right`
instead of `contains`/`starts_with`/`ends_with`, `EXISTS` for semi and anti joins,
explicit column lists instead of DuckDB's `* EXCLUDE/REPLACE/RENAME` and of
`UNION BY NAME`, an unpivot written as `UNION ALL`, a cast on `ROUND` for PostgreSQL.
Column names keep their case (`"Città"` stays `"Città"`). A `pivot` has no portable
SQL, so it is refused in native mode; `foreach` is refused in both (dbt has no
runtime loop). A `sql` node keeps its query, its own `WITH` included: `self` and
`input` point at the previous step.

### The ClickHouse target

ClickHouse reads a Postgres or MySQL schema through a database with the PostgreSQL
or MySQL engine: every table of the schema is a ClickHouse table, read live. The
project names one such database per (connection, schema), `tab_src_<connection>_<schema>`,
and `sources.yml` points at them, so the models use plain `source()` and the dbt docs
show the lineage. From there on everything runs in ClickHouse, with the native SQL
translated to its dialect (the casts of the first step are `Nullable(...)`, as the
engine reads its data).

The databases are created **once**, by a macro in the project:

```bash
export CLICKHOUSE_USER=... CLICKHOUSE_PASSWORD=...   # dbt's own ClickHouse user
export TABULARIA_DB_1_PASSWORD=...                   # one per source connection
DBT_PROFILES_DIR=. dbt run-operation create_tabularia_sources --log-level-file none
```

The source passwords are part of the `CREATE DATABASE` it runs, and dbt writes the
SQL it runs into `logs/dbt.log` at debug level: `--log-level-file none` keeps them
out of it (checked: without it, the password is in the file). ClickHouse keeps the
password in the database definition — `SHOW CREATE DATABASE` prints `[HIDDEN]` — so
give it a read-only user on the sources. `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT` and
`CLICKHOUSE_SECURE` point the profile at another ClickHouse.

- A **datasource defined by a SQL query** is translated with sqlglot from the
  source's dialect to ClickHouse, its tables read from the `tab_src_*` databases; the
  README marks it "review". A query that does not translate is refused.
- A **ClickHouse connection** is read directly if it is the ClickHouse the project
  runs on; another ClickHouse server is refused (use the native target there).
- The profile sets `join_use_nulls = 1`: with ClickHouse's default the missing side
  of an outer join is 0 or `''` on a non-Nullable column instead of NULL.
- **The SQL is read by a real ClickHouse parser before it is written.** sqlglot rarely
  fails: it translates constructs ClickHouse does not have (`SIMILAR TO`, `LATERAL`,
  `TABLESAMPLE`) without a word, and the error would surface only at `dbt run`. Every
  ClickHouse model (this target and the native one on ClickHouse) goes through
  `EXPLAIN AST` — no execution, no table needed — on chDB if the engine has it,
  otherwise on the engine's ClickHouse; if neither answers the check is skipped. A
  syntax error there is a model that does not translate: the export says so, or, with
  the AI option, asks the AI for a translation. It does not catch functions ClickHouse
  does not have — that would need the tables.
- `pivot` is refused, as in native.
- The result is in the ClickHouse database `dbt_tabularia`; a database output of the
  flow becomes a model there, not a write into the source database.

### Download options

In the Flows page, **Export → dbt** opens a dialog that shapes the project for the
data team's repository. The choices are remembered in the browser, per flow.

| Option | What it does |
|---|---|
| **Where it runs** | ClickHouse, DuckDB (federated) or native. A target that cannot work for the flow is disabled, with the reason (e.g. a `pivot` outside DuckDB, sources on several connections for native) |
| **Complete project** | Ready to run: `profiles.yml`, `dbt_project.yml`, README — as described above |
| **Folder for an existing project** | `models/<folder>/`, `seeds/<folder>/`, `tests/<folder>/`, `macros/<folder>/` to copy into the team's repository, and an `INTEGRATION.md` that says what their profile needs (the DuckDB attachments, ClickHouse's `join_use_nulls`), which sources they must already declare, and how to run it: `dbt build -s +tag:<flow>` (the flow's models and everything they read, with their tests). No `generate_schema_name` macro in a folder: it would rename the schemas of their whole project |
| **Folder, model prefix** | Models and seeds named `<prefix><name>` (e.g. `tab_`), away from the team's own names |
| **Layers** | Query models in `staging/`, shared steps in `intermediate/`, outputs in `marts/` |
| **Schema** | Where the models land (the profile's schema; `dbt_tabularia` by default) |
| **Sources** | For each (connection, schema), the source name of the team's project, and **already declared**: no entry in our `sources.yml` (dbt rejects a source declared twice) and, on ClickHouse, no federated database for it |
| **Materialization of each output** | `table` or `view`; `incremental` too for an append output |
| **Include** | The data contract as dbt tests (off: the declared types stay in the descriptions); who built and exported the flow (off: the emails leave the metadata, the flow id and version stay) |

Checked with a "team project" that has its own model and its own `sources.yml`
already declaring the sales source: the folder copied in builds with
`dbt build -s +tag:<flow>`, gives the data Tabularia publishes, and leaves their model
alone; without "already declared" dbt rejects the duplicate source.

### AI (optional)

When the assistant is configured and the administrator has enabled a model, the dialog
offers two more choices, both off by default:

- **Descriptions written by AI where they are missing**: a paragraph "what the flow
  does" for every flow (in the README, and after the description of its outputs), and
  a sentence for every column of the outputs, the source tables and the seeds that has
  no description. In English, marked `(AI)`, without Jinja delimiters (dbt renders
  descriptions as Jinja). The AI sees the structure of the flow — names, types, steps,
  the descriptions already written — and at most 3 short sample values per column.
- **An AI translation where a query does not translate**: when the engine cannot
  translate a model or a datasource query to the target's SQL, the AI proposes one,
  and the engine uses it only if it passes Tabularia's checks — one `SELECT`, nothing
  that writes, no table function, only the tables the model reads, the same output
  columns in the same order, and, on ClickHouse, read by ClickHouse's own parser. If a
  proposal does not pass, the AI gets one more attempt with the reason in front of it
  (a model sometimes sends the query back unchanged); if that fails too, the export
  says why. A model so translated says it in its header and in its `meta`, and the
  README lists it.

What the AI writes is **kept per version of the flow**: re-exporting the same version
reuses it, without a new call, and gives the same zip. A new version asks again. The
calls go through the assistant's model, accounting and daily caps; a cap reached or a
provider that does not answer stops the export with the reason.

Why a target is not available, and why a download was refused, is written in the
language of the interface (English, Italian, German, Spanish, French).

The API: `GET /flows/{id}/export/dbt/plan` (what the dialog needs: targets and why
not, sources with their default names, outputs with the allowed materializations),
`POST /flows/{id}/export/dbt` with the options as JSON, and
`GET /flows/{id}/export/dbt?target=clickhouse|duckdb|native` for the complete project
with the defaults. All three are for administrators only. **Only administrators can export**
(the personal flag or an admin group — give the data team a group): the project is
self-contained, so it carries the upstream flows, the text of SQL queries and, as
seeds, the data of sources that are not in a database, including those of folders
the person who views the flow cannot see. Observers and ordinary users see only the
OpenLineage export in the dialog. The download is in the audit log.

## What the project contains

```
dbt_project.yml          profile, paths; models materialized as table by default
profiles.yml             the connection(s): password via env_var, never in the file
models/
  sources.yml            one source per (connection, schema): tables, descriptions, columns
  schema.yml             every model: description, columns, descriptions, contract tests
  <output>.sql           one model per output node of the flow
  int_<step>.sql         the steps shared by several outputs (ephemeral)
  src_<datasource>.sql   a datasource defined by a SQL query (ephemeral)
seeds/<name>.csv         the files the flow reads, as CSV (with seeds/schema.yml)
tests/<model>__<rule>.sql   contract rules with no generic dbt test (range, pattern, row count, expression)
macros/generate_schema_name.sql   native only: a model with a `schema` lands in that schema
README.md                what is in it, how to run it, what to know
```

### Models and materializations

| In the flow | In dbt |
|---|---|
| Output → datasource | `table` |
| Output → database table, *replace* | `table` (native: with `schema` and `alias` of the destination table, so dbt writes the same table) |
| Output → database table, *append* | `incremental`, `incremental_strategy='append'` |
| Output → S3/GCS file | `table` (export it from the warehouse) |
| Output → email | no model: an email is not a dataset |
| Steps shared by several outputs | `int_<step>` model, `ephemeral`, referenced with `ref()` — one definition, as in the editor |
| Publish sort keys | an `ORDER BY` at the end of the model, as the app does |

A *post-SQL* on a database output is not carried over: the README tells you to add it
as a `post-hook` if you need it.

### Sources

| Datasource | In dbt |
|---|---|
| A database table | `{{ source('<connection>_<schema>', '<table>') }}` in `sources.yml`, with the table's and columns' descriptions |
| A SQL query on a database | `src_<name>`, an ephemeral model with the query. Federated: it runs in the source database through DuckDB's `postgres_query`/`mysql_query`; native: the query as written |
| The output of another flow | that flow's models are included too (recursively, with a cycle check) and referenced with `ref()` |
| A file (uploaded in the editor, imported, SharePoint) | a **seed**: the data as CSV at export time, up to 50 000 rows. Beyond that the export says to import the file into a database |

### Reading a model

A model is one `WITH` list, one step per node, in the order of the flow. Each step
is named after what it does (`source_orders`, `filter_canale`, `compute_netto`,
`aggregate_by_canale`, `left_join_customers`) and opens with a comment in plain
words, so a reviewer reads the flow in the SQL without opening the app:

```sql
{{ config(materialized='table') }}

-- Exported from Tabularia: flow «Margin flow» (id 12, version 7 saved 2026-10-09 18:00 UTC), output «Margin» (datasource).
-- Built by anna@example.com. Re-exporting the same version gives this same file.

WITH
source_orders AS (
    -- source: orders, with the column types Tabularia reads
    SELECT
        CAST("id" AS BIGINT) AS "id",
        CAST("canale" AS VARCHAR) AS "canale",
        CAST("importo" AS DOUBLE) AS "importo"
    FROM {{ source('db_1_vendite', 'ordini') }}
),
filter_canale AS (
    -- keep rows where canale = 'web'
    SELECT *
    FROM source_orders
    WHERE "canale" = 'web'
),
compute_netto AS (
    -- netto = importo * 0.8
    SELECT *, (importo * 0.8) AS "netto"
    FROM filter_canale
),
aggregate_by_canale AS (
    -- aggregate by canale: sum(netto) as tot
    SELECT "canale", sum("netto") AS "tot"
    FROM compute_netto
    GROUP BY "canale"
)
SELECT *
FROM aggregate_by_canale
```

### Where a model comes from

The header of every model says which flow it comes from (id, the version that was
exported and when that version was saved), which output, and who built the flow. The
same facts sit in `schema.yml` under `config.meta.tabularia` — `flow`, `flow_id`,
`flow_version`, `version_saved_at`, `folder`, `owner`, `output`, `node` (the output
node's id) — and so in the dbt docs and the manifest, where a catalog can read them.
Who exported the project is written in the README only.

**The export is deterministic.** The same version of a flow with the same options
gives the same zip, byte for byte — whoever exports it, whenever, from whichever
gateway: no export time in the files, entries in order with a fixed date. Re-exporting
into the team's repository shows, in the diff, only what changed in the flow. Checked by
exporting every Sample flow on the three targets, with and without options, twice and
again after restarting the gateway and the engine. Every model is tagged `tabularia` and with the flow's name:
`dbt run -s tag:tabularia` runs everything that came from the app,
`dbt run -s tag:margin_flow` one flow. A model made from a datasource's SQL query
names that datasource instead of a flow. The README lists the flows the project was
made from.

### Descriptions and the data contract

The datasource an output publishes lends the model its columns and column
descriptions. If it has a **data contract**, every rule becomes a dbt test with the
rule's severity (`warning` → `warn`, `error` → `error`):

| Contract rule | dbt |
|---|---|
| `not_null`, `unique` (one column), `accepted_values` | generic tests on the column in `schema.yml` |
| `unique` (several columns), `range`, `pattern`, `row_count`, `expression` | a singular test in `tests/`, a query that selects the violations |
| `column` (exists, type) | the column is listed, the declared type in its description |
| `freshness` | a note in the README: dbt checks freshness on sources, not on models |

`dbt test` then fails or warns exactly where Tabularia's contract would.

## Run it

```bash
pip install dbt-duckdb          # or dbt-clickhouse, or dbt-postgres / dbt-mysql for the native target
export TABULARIA_DB_1_PASSWORD=...   # one per connection, named in profiles.yml
# ClickHouse target only: CLICKHOUSE_USER / CLICKHOUSE_PASSWORD, then once
# DBT_PROFILES_DIR=. dbt run-operation create_tabularia_sources --log-level-file none
DBT_PROFILES_DIR=. dbt seed     # only if the project has seeds
DBT_PROFILES_DIR=. dbt run
DBT_PROFILES_DIR=. dbt test
```

Federated: the result is in `<project>.duckdb`, next to the project (the DuckDB
`postgres`/`mysql` extension is downloaded at first run). Native: the models are in
the `dbt_tabularia` schema of the source database, or in the destination tables of
the flow's database outputs.

## Verified

With a real dbt (1.12: dbt-duckdb, dbt-postgres, dbt-clickhouse), against the sample
databases, every model compared with what Tabularia publishes for the same flow —
columns in order, rows, and order where the order is the point:

- **every operation of the editor** with its variants (60 cases: every cast, every
  filter operator, every join kind, `on` and `left_on/right_on`, both unions,
  every aggregation, compute that adds or replaces a column, unpivot, sql nodes with
  and without their own `WITH`, names with spaces, accents, upper case and reserved
  words, NULLs everywhere) in both targets, plus pivot in the federated one: all equal.
  A case Tabularia refuses (a numeric comparison on a text column) fails in dbt too;
- **the data contract**: for every rule, the dbt test passes, warns or fails exactly
  when Tabularia's contract check does, with the same severity;
- **the dbt lifecycle**: `parse`, `compile`, `docs generate`; a table written in append
  doubles on the second `dbt run` (as two Tabularia runs do) and `--full-refresh` starts
  over — natively, in the flow's own destination table;
- **ClickHouse** as native warehouse, and every Sample flow in both targets.

The tools are durable scripts and run again on every change.

## Limits

- After a `sql` node the export works out its columns from the query and the
  previous step. When it cannot (a query it does not parse, a star over a table it
  does not know), the following steps use `SELECT *`, and in the native target the
  steps that need the column list (drop, rename, reorder, cast, fill null, union by
  name) are refused with a message.
- A PostgreSQL `numeric` column is a number (a double) in Tabularia, and the first step
  of every model casts it the same way: exact decimals become doubles, as for a CSV.
- `foreach` cannot be exported; `pivot` only in the federated target.
- ClickHouse and Trino cannot be federated (DuckDB has no scanner for them): use the
  native target, which needs all sources on that connection.
- Seeds are snapshots: a flow that reads a file keeps reading the CSV exported that
  day until you run `dbt seed` with a new one.
- The native target writes with the user of the connection, in `dbt_tabularia` or in
  the destination schemas: that user needs `CREATE` there.
