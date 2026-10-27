# SQL scripts

A SQL script is SQL kept in a folder, like a flow or a saved view: written in an editor,
run with one key, saved with its versions and shared through the folder's permissions.
It reads the datasources of the catalog on the engine you choose. Every installation has
it, Docker Compose included. The design is in [the spec](specs/sql-scripts.md).

## Writing one

Explore → **New → SQL script** in a folder. The page has the editor, the result under it,
and on the side the datasources you can read with their columns: a click on a name writes
it where the cursor is, the arrow shows the columns, a click on a column writes it too.
The editor also completes names and columns as you type.

A datasource is a table by its **name**. A name with spaces or capitals goes in double
quotes; unquoted names match regardless of case:

```sql
SELECT c.canale, count(*) AS ordini
FROM "orders" o
JOIN "customers" c ON o.cliente_id = c.id
GROUP BY c.canale
ORDER BY ordini DESC
```

A name is unique only in its folder. When two datasources you can read share it, write the
folder's name in front, `"Marketing"."Clienti"`: the error says so and lists the folders.
Renaming a datasource breaks the scripts that name it, as it would any SQL.

The dialect is the engine's: Polars SQL, DuckDB, ClickHouse or BigQuery, chosen at the top
of the page (the administrator decides which engines may be chosen). `--` and `/* */`
comments are fine.

## Running

**Run**, or **Ctrl+Enter** in the editor, runs the selection when there is one, otherwise
the whole script, and shows the first 200 rows with the time taken and the datasources
read. Only one query that reads data runs: a `SELECT` (a `WITH` before it too). It reads the
datasources you name and nothing else: the engine checks every table against the ones the
gateway resolved, as for the SQL step of a flow.

A run reads with **your** permissions, whoever wrote the script: a datasource you cannot
read gives the same error as one that does not exist. Every run is in the audit log
(`sql_script.run`) with its SQL and the datasources it read.

## Saving and sharing

**Create** the first time, then **Save** (Ctrl+S): a save that changes the SQL or the
engine is a new version. **Versions** on the side lists them; *View* shows an older one
read-only, *Restore* makes it the newest. Old versions beyond
`ORCHESTRATOR__VERSIONS_KEPT` are pruned, the current one never.

| On the script's folder | You can |
|---|---|
| View | open it, read its SQL, run it, try changes without saving them |
| Edit | save, rename, move, duplicate, delete it |

Administrators see every script. SQL scripts show in Explore and in the search. A folder
that holds one cannot be deleted until it is moved or deleted.

## Coming next

Running on a database connection, exporting the whole result as CSV or Excel, saving the
result as a datasource and refreshing it on a schedule: see the pieces in
[the spec](specs/sql-scripts.md#pieces).
