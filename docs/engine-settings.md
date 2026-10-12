# Engine settings from the administration panel

For whoever installs and runs Tabularia. **Administration → Settings** changes some
engine parameters while everything keeps running: no environment edit, no restart.
The environment (`.env`, the Helm values) stays the starting value of every
parameter; the panel overrides it, and **Back to the environment** returns to it.

## What can be changed

Only parameters the engine reads at the moment it uses them. The panel accepts
narrower ranges than the environment, so that a typing mistake cannot take the system
down; a value outside them goes in the environment.

| Category | Parameter | Environment variable | Panel range |
|---|---|---|---|
| Import | Rows per row group | `INGEST__PARQUET_ROW_GROUP_ROWS` | 10,000 – 5,000,000 |
| Import | Longest wait for an answer during an import | `INGEST__DB_READ_TIMEOUT_SECONDS` | 30 s – 1 day |
| Export | Most rows in a CSV export (0 = no limit) | `EXPORT__MAX_ROWS` | 0 – 100,000,000 |
| Execution | Longest a task can run (hard limit) | `CELERY__TASK_TIME_LIMIT` | 60 s – stale timeout − 5 min |
| Execution | Shutdown notice (soft limit) | `CELERY__TASK_SOFT_TIME_LIMIT` | 30 s – 7 days |
| Step cache | How long the step cache lasts | `CACHE__TTL_SECONDS` | 5 min – 30 days |
| Step cache | Most rows in a cached step (0 = no limit) | `CACHE__MAX_STEP_ROWS` | 0 – 100,000,000 |
| ClickHouse | Threads for scans (0 = ClickHouse decides) | `CLICKHOUSE_EXTERNAL__PARQUET_SCAN_MAX_THREADS` | 0 – 64 |
| ClickHouse | Longest a query can run (0 = no limit) | `CLICKHOUSE_EXTERNAL__MAX_EXECUTION_TIME` | 0 s – 1 day |
| ClickHouse | Copy for the viewer above (0 = off) | `CLICKHOUSE_EXTERNAL__MATERIALIZE_MIN_ROWS` | 0 – 1,000,000,000 |
| ClickHouse | How long the viewer copies last | `CLICKHOUSE_EXTERNAL__MATERIALIZE_TTL_SECONDS` | 1 min – 7 days |
| BigQuery | Most bytes billed per query (0 = no limit) | `BIGQUERY__MAXIMUM_BYTES_BILLED` | 0 – 100 TiB |

The panel shows each one with a description in the reader's language, the value of
the environment and its variable, the allowed range, and who changed it and when.
The ClickHouse and BigQuery categories are greyed out when that engine is not
configured.

Three rules link parameters to one another, and a change that breaks one is refused:

- the soft time limit stays below the hard one: it exists to let the task close files
  and connections before it is stopped;
- the import read timeout stays below the soft limit, so that a slow import reports
  the timeout and not a killed task;
- the hard limit stays at least five minutes below `ENGINE__RUN_STALE_TIMEOUT_SECONDS`.
  The gateway declares a run lost after that threshold, and a higher hard limit would
  have it close as failed a run that is still alive. From the panel the hard limit can
  be lowered freely; to raise it beyond, raise both values in the environment.

**Not in the panel, on purpose:**

- credentials and keys (storage, ClickHouse, BigQuery, Fernet);
- addresses (Redis, endpoints);
- anything a process reads once at start, such as worker concurrency, memory per
  worker process and API threads.

These stay in the environment.

## How a change travels

1. An administrator saves. Values are whole numbers: `true`, `"100"` or `1.5` are
   refused, not converted. The gateway sends the full set to the engine, which checks
   it and refuses it as a whole if one value is wrong. The panel then shows, in the
   reader's language, which value and why; when two values conflict, the error names
   the one just changed. Nothing is saved until the engine accepts. Saves go one at a
   time, also across gateway replicas (a lock in the database): two administrators
   saving together both end up saved and applied.
2. The gateway stores the accepted values in its database (table `engine_settings`,
   schema revision 0004) and writes an audit entry, `engine_settings.update`, with the
   old and the new value of each parameter.
3. The engine keeps them in Valkey. Every engine process (the API, each worker
   process, beat) re-reads them at most every ten seconds.
4. The gateway sends them again every minute. If Valkey is flushed or restarted
   without persistence, the values come back within a minute instead of silently
   falling back to the environment.

Where each change takes effect:

- **Row group size and read timeout:** from the next import.
- **Export cap:** from the next export.
- **Cache and ClickHouse parameters:** from the next query or preview.
- **Task time limits:** from the next task. They travel in each task's message, and
  Celery uses the message's limits over the worker's configuration.

The soft limit reaches the task as a Python exception, delivered when the task's code
runs again. A task waiting inside a database driver gets it only when the driver
returns. The hard limit stops the process on time regardless: on the reference test
stack, a 90-second import was stopped at 60.0 seconds after the hard limit was set to
60 from the panel.

A saved value can stop being valid later, for instance after the environment's limits
or the stale timeout are lowered. The gateway then sends the other values without it,
as many times as needed, and logs which one was dropped and why. The panel marks it
**Not applied**. It does not block changes to other parameters. Changing it or going
back to the environment clears it.

## Who

- **Administrators** change the values.
- **Observers** see them, with masked names.
- **Everyone else** gets `403`.
- **API tokens** cannot change them, whatever their owner: a leaked token must not be
  able to stretch time limits or lift caps.

## Turning it off

`ENGINE__SETTINGS_PANEL=false` (Helm: `engine.settingsPanel: false`) keeps the
environment the only source, for installations that manage configuration in Git:

- the panel shows the values but cannot change them;
- values saved earlier stop applying within a minute (they stay in the database, and
  come back if the switch is turned on again).

## Upgrading

Schema revision 0004 only adds the empty `engine_settings` table. Until someone saves
a value from the panel, every parameter keeps its environment value, as before. The
engine and the gateway can be updated in either order:

- an older engine never asks for the values;
- an older gateway never sends any.
