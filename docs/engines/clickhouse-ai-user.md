# A dedicated ClickHouse user for the AI assistant

The engine talks to the external ClickHouse with one account, and that account
can read everything on the server. The AI assistant hands a free-form `SELECT`
to every user with VIEW on a datasource, so its queries get **two** barriers:

1. the SQL node allow-list (`backend/app/engine/sql_guard.py`): a query can name
   only its own input, `self` / `input`;
2. a **dedicated, read-only ClickHouse user**: if a query ever got past the
   first barrier, on the server it would find an account that cannot read
   `system`, cannot read any table, and cannot use `url()` / `file()` / `remote()`.

The second barrier is optional and off until you configure it.

## Configuration

| Variable | Meaning |
| --- | --- |
| `CLICKHOUSE_EXTERNAL__AI_USERNAME` | the dedicated user, e.g. `ai` |
| `CLICKHOUSE_EXTERNAL__AI_PASSWORD` | its password (a secret) |

Helm: `externalServices.clickhouse.aiUsername` and `secrets.clickhouseAiPassword`.

The user is used only when **both** variables are set **and**
`CLICKHOUSE_EXTERNAL__TRANSPORT=s3`. In every other case the assistant runs as
`CLICKHOUSE_EXTERNAL__USERNAME`, exactly as before — a half-configured pair
never produces failed logins. With the `push` transport a preview creates
staging tables on the server, which a read-only user cannot do.

Set them on the **backend and the workers** (the gateway never talks to
ClickHouse). Flows, runs, the Viewer and the editor previews are not affected:
only the assistant's queries carry the `ai` identity.

## Creating the user

Run as an administrator of the ClickHouse service:

```sql
CREATE USER ai IDENTIFIED WITH sha256_password BY '<a long random password>'
  SETTINGS readonly = 2;

-- reading the parquet files from object storage is a table function:
-- it needs both grants, and nothing else
GRANT CREATE TEMPORARY TABLE, S3 ON *.* TO ai;
```

`readonly = 2` rather than `1`: the engine raises `max_threads` on scans and
tags every query with `log_comment`, and `readonly = 1` forbids changing
settings.

If the engine uses a named collection for the S3 credentials
(`CLICKHOUSE_EXTERNAL__S3_NAMED_COLLECTION`), also:

```sql
GRANT NAMED COLLECTION ON <collection_name> TO ai;
```

Do **not** grant `SELECT` on any database. The assistant never reads the
materialized copies of the Viewer (it reads the parquet), so it needs none.

## What the user can and cannot do

Verified on ClickHouse 24.8 with exactly the grants above:

| Query | Result |
| --- | --- |
| `SELECT … FROM s3(<parquet>)`, in the engine's `WITH input AS (…), self AS (…)` form, with `SETTINGS max_threads, log_comment` | works |
| `SELECT … FROM system.query_log`, `system.processes`, `system.users` | `ACCESS_DENIED` |
| `merge('system', '^query_log$')` | finds no table |
| `system.tables`, `SHOW TABLES` | empty |
| `information_schema.tables` | `ACCESS_DENIED` |
| any table of any database | `ACCESS_DENIED` |
| `url(…)`, `file(…)` | `ACCESS_DENIED` (needs `URL` / `FILE`) |
| `system.settings` | readable — harmless, and the client library needs it |

### One thing it does not stop

`INSERT INTO FUNCTION s3(…)` **succeeds** even with `readonly = 2`: on these
ClickHouse versions the `S3` grant covers reads and writes alike. It is not
reachable from the assistant — the SQL node refuses `INSERT`, refuses `s3(`, and
the query never sees the storage credentials, which only the engine writes into
the SQL it builds — but it means this user is *read-only on the server*, not
*read-only on the bucket*. From ClickHouse 25.7 the grant can be split
(`GRANT READ ON S3`); use that form when the service offers it.

## Checking it

```sql
-- as the ai user: must work
SELECT 1;
-- must fail with ACCESS_DENIED
SELECT count() FROM system.query_log;
```

Then ask the assistant a question on a datasource with the `clickhouse` engine
selected, and look at the server's `system.query_log` as an administrator: the
assistant's queries must appear with `user = 'ai'`.
