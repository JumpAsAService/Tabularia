# Export a flow as a dbt project

A flow designed in Tabularia can leave as a **dbt project**: a zip with one model per
output, the shared steps as their own models, `sources.yml`, `schema.yml` with the
columns, their descriptions and the data contract as dbt tests, seeds for the files
the flow reads, and a README that says how to run it. Put it in a repository, run it
in your CI, read it in your catalog: the flow is no longer locked in the tool.

It is an **exporter, not a second engine**: Tabularia keeps running the flow as it
does; the dbt project is a faithful, versionable copy of the same transformations
that you own from then on.

## Two targets

| Target | Where the SQL runs | Needs | When |
|---|---|---|---|
| **dbt-duckdb** (federated) | In DuckDB, which attaches the source databases and reads their tables live | `pip install dbt-duckdb`; network access to the sources | Sources on PostgreSQL, MySQL or MariaDB, on any number of connections; any operation of the editor |
| **Native warehouse** | Inside the source database itself (dbt-postgres, dbt-mysql, dbt-clickhouse) | The adapter of that database | All sources on the **same** connection; the result lands in that database, next to the data |

The federated target runs the very SQL Tabularia's DuckDB engine runs: nothing is
translated. The native target translates it to the warehouse's dialect with sqlglot,
column by column (the `SELECT *` of every step is expanded with the schema the flow
knows), and patches the differences sqlglot does not: `ROUND(x, n)` gets a cast to
`DECIMAL` on PostgreSQL, for one. A `pivot` has no portable SQL, so it is refused in
native mode; `foreach` is refused in both (dbt has no runtime loop).

Both are in the Flows page, under **Export**, and at
`GET /flows/{id}/export/dbt?target=duckdb|native` (VIEW on the flow's folder; the
download is in the audit log).

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
pip install dbt-duckdb          # or dbt-postgres / dbt-mysql / dbt-clickhouse for the native target
export TABULARIA_DB_1_PASSWORD=...   # one per connection, named in profiles.yml
DBT_PROFILES_DIR=. dbt seed     # only if the project has seeds
DBT_PROFILES_DIR=. dbt run
DBT_PROFILES_DIR=. dbt test
```

Federated: the result is in `<project>.duckdb`, next to the project (the DuckDB
`postgres`/`mysql` extension is downloaded at first run). Native: the models are in
the `dbt_tabularia` schema of the source database, or in the destination tables of
the flow's database outputs.

## Verified

Every Sample flow is exported in both targets and run with a real dbt against the
sample databases; the result of each model is compared with the datasource Tabularia
publishes for the same flow, row count and column by column. The federated target
matches on every flow it can serve; the native target matches wherever the flow has
no `pivot`. The comparison tool is a durable script and runs again on every change.

## Limits

- Column-level: a `sql` node is passed through, so the schema after it is unknown
  until dbt runs; the following steps use `SELECT *`.
- `foreach` cannot be exported; `pivot` only in the federated target.
- ClickHouse and Trino cannot be federated (DuckDB has no scanner for them): use the
  native target, which needs all sources on that connection.
- Seeds are snapshots: a flow that reads a file keeps reading the CSV exported that
  day until you run `dbt seed` with a new one.
- The native target writes with the user of the connection, in `dbt_tabularia` or in
  the destination schemas: that user needs `CREATE` there.
