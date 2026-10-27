# Spec: SQL scripts

Status: pieces 1 to 3 done (the catalog, connections, export); 4 to do. Decided with the
owner on 2026-10-27.

## Objective

A person writes SQL in an editor, runs it and sees the result, and keeps it as **an
object of a folder**, like a flow, a saved view or a saved Python/R script: saved with its
versions, found in Explore and in the search, shared through the folder's permissions.

The SQL reads either **the catalog** (the datasources the person may read, on the engine
they choose) or **a database connection** (piece 2). The result can be exported (piece 3),
saved as a datasource and refreshed on a schedule (piece 4).

Unlike saved Python/R scripts, SQL scripts need no Kubernetes: they run on the engine, and
show on every installation.

## Decisions

- **Who sees a SQL script: VIEW on its folder.** Whoever has it opens the script, reads
  its SQL and runs it. EDIT on the folder saves, renames, moves, duplicates, deletes it.
  Administrators see every script.
- **A run reads with the authority of whoever runs it**, never of the author: on the
  catalog, every datasource the SQL names must be readable by the person running it, and
  one that is not gives the same error as one that does not exist (as in the AI
  assistant). A shared script is a shared text, not shared access.
- **On the catalog, a datasource is a table by its name.** `SELECT … FROM "Vendite Nord
  2024"`; unquoted names match regardless of case. A name is unique only in its folder:
  when two readable datasources share it, the SQL must say which with the folder's name,
  `"Marketing"."Vendite Nord 2024"`, and the error says so, listing the folders. Renaming
  a datasource breaks a script that names it, as it would any SQL.
- **The SQL is read-only**: one statement, a SELECT (WITH allowed), checked by the engine
  with the same allowlist of the `sql` node, extended to the datasources the gateway
  resolved — and nothing else. The person never names a bucket or a key.
- **Every run is audited** (`sql_script.run`), with the SQL and the datasources read, as
  the AI assistant's queries are: it is a free query over the data.
- **Connections** (piece 2): running SQL on a connection needs CONNECT on the connection's
  folder, as creating a database datasource does; whoever has only VIEW reads the SQL and
  does not run it. Choosing a connection for a script needs CONNECT on it too (nobody points a
  script at a database they could not query themselves); an editor without it still saves
  the SQL of a script that has its connection already, and the script's readers see the
  connection's name, to know why they cannot run it. The barrier on the database
  is a read-only user, as for database datasources: the documentation says so, with a
  time limit on that user.

## The object

`sql_scripts` (migration 0024):

| Column | |
|---|---|
| `id`, `name`, `description`, `project_id`, `owner_id` | as a flow; the name is unique in its folder |
| `current_version` | the version shown and run |
| `created_at`, `updated_at` | |

`sql_script_versions`: `script_id`, `version` (1, 2, 3…), `content` (JSON below), `note`
(`created`, `edited`, `restored from v2`), `created_at`, `created_by`. Every save that
changes the content makes a version; an older one can be restored (it becomes the newest).

```json
{"sql": "SELECT …", "engine": "duckdb", "connection_id": null}
```

`engine` null = the installation's default engine; `connection_id` null = the catalog.

## Running on the catalog

1. The gateway parses the SQL (sqlglot, in the engine's dialect) and finds the tables it
   names, leaving out the names of its own CTEs.
2. Each name is resolved to a datasource the runner may read (name, or folder and name).
3. The SQL text is changed **only where a datasource is named**: the name becomes an
   internal alias (`ds_12`), keeping the original name as the table's alias when it had
   none, so `"Vendite".importo` still works. The rest of the text is the person's, to the
   character: sqlglot's token positions, not a regenerated query.
4. The engine gets a preview: the first datasource as the source, and one `sql` operation
   with `tables: {"ds_12": {"source": {...}}, …}`. Every engine builds each table as it
   builds the right side of a join (`build_right`), exposes it under its alias, and the
   allowlist accepts those aliases and nothing else.
5. The result comes back as the Viewer's: columns, the first rows (capped), truncated or
   not, and the time taken.

## Running on a connection

1. The gateway checks CONNECT on the connection's folder (404 when the connection is gone
   or is not a database, 403 without CONNECT) and sends the SQL as it is to the engine's
   `POST /db/query`, with the connection (password still encrypted) and the script's
   slot.
2. The engine runs it on the preview worker (`db_query_task`), as a preview: a newer run
   on the same slot knocks it down, `PREVIEW_TIMEOUT_SECONDS` revokes it. The SQL is
   wrapped as a SQL datasource's (`build_query`) and capped in the database's dialect at
   one row more than shown (`LIMIT`, `FETCH FIRST`, `TOP`; SQL Server's SQL with a CTE is
   not wrapped, and the reader stops instead), so the database stops there.
3. The answer is a preview's: columns, rows, truncated or not. The database's refusal comes
   back in its words (`db_errors.describe_db_error`).
4. The side panel lists the connection's tables (`/connections/{id}/tables`) and, when one
   is opened, its columns (`/connections/{id}/columns`, the schema of a read of one row).

## Exporting

`POST /sql-scripts/export` (`sql`, `format` csv|xlsx, `engine`, `connection_id`,
`script_id`) checks what a run checks and starts an export the person owns
(`user:{id}`, as the Viewer's): on the catalog the run's chain goes to the engine's
`POST /tasks/exports`, without a row limit; on a connection the SQL goes to
`POST /db/exports`, whose `db_export_task` reads the database into a temporary parquet
snapshot (the import's reader, capped at the format's cap and one) and has Polars write
the file from it. Either way the person follows it and takes the file once through
`/tasks/exports/{id}` and `/tasks/exports/{id}/file`. The file is named after the script;
every export is audited (`sql_script.export`) with its SQL and what it read.

## The page

`/sql-script?id=` (or `?project=` for a new one):

- the name, the folder, where the SQL runs (the catalog or a database connection the
  person may use), the engine on the catalog (the ones the administrator allows), Run
  (Ctrl+Enter), Save, the versions with restore;
- the SQL editor (Monaco, the one of the script editor), with completion of the
  datasources' names and of their columns;
- beside it, the readable datasources with their folder and columns, or the connection's
  tables: a click inserts the name;
- under it, the result in a grid, with the rows, the time, and the error of a failed run
  in plain words;
- a person with VIEW only reads the SQL and runs it; nothing to save.

## Elsewhere

- **Explore**: a new kind of object (icon, filter, New → SQL script, open, rename, move,
  duplicate, delete); **search**: a new kind.
- **Audit**: `sql_script.create`, `sql_script.update`, `sql_script.delete`,
  `sql_script.restore`, `sql_script.run`, `sql_script.export`.
- **API tokens**: read tokens list and read the scripts they may see.

## Pieces

1. **The object and the catalog**: tables and migration 0024, versions, visibility, CRUD,
   Explore, search, the page with the editor and the result, the engine's named tables on
   every engine, the audit.
2. **Connections**: the connection picker, its tables and columns, the run on the database
   with a row cap and a time limit, CONNECT.
3. **Export**: the whole result as CSV or Excel, written by a worker as the Viewer's export.
4. **Save as a datasource and schedules**: from a connection, a database datasource with
   the script's SQL and its refresh schedule; from the catalog, a datasource the engine
   writes, refreshed on a schedule with the authority of whoever set it.
