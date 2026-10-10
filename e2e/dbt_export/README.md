# dbt export — end-to-end oracles

These scripts prove that a flow exported as a dbt project gives **the data Tabularia
publishes**. They run a real dbt (in a container) against a running Tabularia and
compare, model by model, what dbt builds with what the app wrote. They are the
evidence behind [docs/dbt-export.md](../../docs/dbt-export.md); run them after any
change to the exporter (`backend/app/engine/dbt_export.py`,
`backend/app/api/routes/dbt.py`, `gateway/app/services/dbt_export.py`,
`gateway/app/services/dbt_ai.py`) or to the engines it mirrors.

| Oracle | What it proves |
|---|---|
| `matrice.py` | Every operation of the editor (≈60 cases, tricky data: NULLs, spaces, decimals, dates, reserved and accented names) gives the same columns in the same order and the same rows in dbt as in Tabularia, on the three targets (DuckDB federated, native Postgres, ClickHouse) |
| `ciclo.py` | A violated data contract gives the same verdict, rule by rule, in Tabularia and in `dbt test`; `parse` / `compile` / `docs generate` without deprecations; an append output is an incremental model (run, run, `--full-refresh` → 8, 16, 8 rows); the source passwords never land in `logs/` or `target/`; provenance in the manifest; native ClickHouse |
| `cartella.py` | The "folder for an existing project" package, copied into a team project with its own model and its own `sources.yml`, builds with `dbt build -s +tag:<flow>` and gives the same data; without "already declared" dbt rejects the duplicate source |
| `flussi_sample.py` | Every Sample flow, on every target its export plan allows, run in dbt and compared with its published datasources |
| `deterministico.py` | The same flow version with the same options gives the same zip, byte for byte, twice in a row and after restarting gateway and engine |
| `giro.py` | A round with an upstream flow, shared steps, an append table, a query datasource, an uploaded file and a contract: `dbt seed/run/test` in two targets |
| `sicurezza.py` | The dbt export is for administrators only |
| `codici.py` | Every error code the gateway and the engine raise has its sentence, with its parameters, in the five languages |
| `browser_dialogo.py` | The export dialog in Firefox: options, validation, the downloaded zip, remembered choices, refusals in the interface language, the AI options when a model is enabled |
| `ai_descrizioni.py`, `ai_traduzione.py` | The optional AI with the real provider: descriptions, and a Postgres query that sqlglot translates badly (`SIMILAR TO`) rewritten by the AI, checked, run in dbt on ClickHouse. **Paid calls** |

## What they need

- A running Tabularia with the **sample data** (the Postgres ERP and the ClickHouse CRM
  that `sampledb-init` loads, with its eleven flows in the folder *Flows*), and an
  administrator account.
- The **ClickHouse engine** configured (`CLICKHOUSE_EXTERNAL__*`) as a container on the
  stack's Docker network: the ClickHouse target runs dbt-clickhouse on it, and it must
  reach the sample Postgres.
- Docker on the machine that runs the oracles (dbt runs in a container on the stack's
  network, from the image in `Dockerfile.dbt`; `esegui.py` builds it if missing).
- For `browser_dialogo.py`: Firefox (headless, Marionette on port 2828).
- For the AI oracles: the assistant configured and a model enabled by the administrator.

Everything that depends on the installation is read from environment variables, with
defaults for a local stack — see [`ambiente.py`](ambiente.py). The usual way is to
point `TABULARIA_ENV` at the stack's `.env`, which carries the admin credentials and
the ClickHouse settings:

```bash
TABULARIA_ENV=/path/to/stack/.env TABULARIA_URL=http://localhost:8088 \
  python3 e2e/dbt_export/esegui.py            # the complete suite, ~40 minutes
python3 e2e/dbt_export/esegui.py veloce       # ~5 minutes
python3 e2e/dbt_export/esegui.py matrice ciclo
python3 e2e/dbt_export/esegui.py ai           # paid calls to the AI provider
```

`esegui.py` checks the prerequisites first (API and login, containers, dbt image, the
case tables of `schema.sql` in the sample Postgres), then runs each oracle, writes its
log in `E2E_LAVORO` (by default `e2e/dbt_export/.lavoro/`, ignored by git) and prints
a summary; it exits with an error if one is not green. Each oracle creates what it
needs (folders, flows, datasources named `zz…`) and removes it at the end.

The determinism oracle restarts gateway and engine only if `E2E_RIAVVIO` holds the
command that does it (for example `docker compose … restart gateway orchestrator
backend worker`); without it, it checks two exports in a row.

## Reading a failure

Each line of a log is one check: `ok` or `✗` with what differed. In the matrix a
difference prints the rows only Tabularia has and the rows only dbt has. A case that
Tabularia itself refuses must fail in dbt too — that is parity, not a defect. Known
differences between the engines are listed in
[docs/engines/engine-differences.md](../../docs/engines/engine-differences.md).
