# dbt export — end-to-end oracles

These scripts check that a flow exported as a dbt project gives **the data Tabularia
publishes**. They run a real dbt (in a container) against a running Tabularia and
compare, model by model, what dbt builds with what the app wrote. They are the
checks behind [docs/dbt-export.md](../../docs/dbt-export.md); run them after any
change to the exporter (`backend/app/engine/dbt_export.py`,
`backend/app/api/routes/dbt.py`, `gateway/app/services/dbt_export.py`,
`gateway/app/services/dbt_ai.py`) or to the engines it mirrors.

| Oracle | What it checks |
|---|---|
| `matrice.py` | Every operation of the editor (≈60 cases, tricky data: NULLs, spaces, decimals, dates, reserved and accented names) gives the same columns in the same order, the same family of types (integer, float and decimal apart — an integer that comes back as a float, or a sum left as a 128-bit HUGEINT, is a difference — text, date, datetime, bool) and the same rows in dbt as in Tabularia, on the three targets (DuckDB federated, native Postgres, ClickHouse). An empty text is not NULL and a text is compared as text (`'200'` is not `'200.0'`); dbt runs in `Europe/Rome` (`E2E_TZ`). The known types of native Postgres for `ROUND` and `EXTRACT` in a compute (a `numeric`) are allowed by name, case by case. Also: chains after a compute, a sort and a pivot (joined back on its index with `left_on`/`right_on` too), the mean, deviation and variance of an integer column, a text on several lines (LF, CRLF, a lone CR, a tab) in a compute, a filter value, a fill_null value, a sql node and a column name; a table with a `timestamptz`, an unbounded `numeric` and empty texts; a file read as a seed with the texts dbt's seed loaders change |
| `ciclo.py` | A violated data contract gives the same verdict, rule by rule, in Tabularia and in `dbt test` (whole-value patterns, also on integer and date columns; accepted values with an apostrophe or `3.0` on an integer column; a unique column with two NULLs; not_null on names with spaces, capitals or reserved words; two rules on one column); `parse` / `compile` / `docs generate` without deprecations; an append output is an incremental model (run, run, `--full-refresh` → 8, 16, 8 rows); the source passwords never land in `logs/` or `target/`, also on the federated target with a wrong password or a source that does not answer; provenance in the manifest; native ClickHouse, with NULL on the missing side of an outer join |
| `cartella.py` | The "folder for an existing project" package, copied into a team project with its own model (tagged like the flow) and its own `sources.yml`, with the profile blocks `INTEGRATION.md` gives, builds with the command it gives (`dbt build -s +tag:<flow>,tag:tabularia`), selects none of the team's models, and gives the same data; without "already declared" dbt rejects the duplicate source |
| `flussi_sample.py` | Every Sample flow, on every target its export plan allows, run in dbt and compared with its published datasources |
| `deterministico.py` | The same flow version with the same options gives the same zip, byte for byte, twice in a row and after restarting gateway and engine (also a drop of several columns); exported by another administrator, only `README.md` differs (it names who exported), and with the emails option off nothing does |
| `giro.py` | A round with an upstream flow, shared steps, an append table, a query datasource, an uploaded file and a contract: `dbt seed/run/test` in two targets. The downstream export brings only the branch it reads of the upstream flow; the upstream export has the shared step as an ephemeral model, and on native its append model writes the very table Tabularia wrote (one `dbt run` doubles its rows) |
| `sicurezza.py` | The dbt export is for administrators only; Jinja typed in a flow (`{{ env_var(…) }}`, `{% … %}`, `{# …`, `log(…, info=True)` in a filter value, a compute expression, a column name and the output table's name, with a backslash) never runs: no marker that only running Jinja prints on the console, in `logs/` or `target/`; with dbt's static parser on, the model's alias and schema are the table typed in the flow, and `dbt run` writes that very table, with the texts as typed. A quote in a native output's table name (`o'brien`, which dbt-postgres cannot write) is refused with `name_not_quotable`: by the plan, before the download, and by the download |
| `codici.py` | Every error code the gateway and the engine raise (`DbtExportError` in the exporter, the engine route, the gateway's export and AI services), and every reason of a refused AI translation, has its sentence, with its parameters, in the five languages; no sentence is left that nothing raises |
| `deriva.py` | What the gateway mirrors of the engine's exporter, so that the plan says before the download what the engine would refuse, has not drifted apart (read in the code, no stack needed): the parameters each step requires (`_RICHIESTI` = the `_require` calls of `_compile_op`), the seed cap, the databases the ClickHouse, federated and native targets read, the dialects the gateway sends and the engine accepts, the characters refused in a name, role by role (a source's schema and table, a model's table, a model's schema) |
| `browser_dialogo.py` | The export dialog in Firefox: options, validation, the downloaded zip, remembered choices, refusals in the interface language, the AI options when a model is enabled |
| `browser_dialogo_stato.py` | The dialog's state, with the export answered inside the page (one real download): a download cancelled by Esc, ×, Cancel or leaving the page never reaches the next flow's dialog; Schema and «already declared» only sent for their package; an empty folder remembered empty; each field's rule with `aria-invalid`/`aria-describedby`; duplicate source names flagged, and each source's default the name the gateway gives (the plan's `default_base`); the file name from `filename*` or a fallback that keeps any letter; the focus back on the row's Export button; ClickHouse with a source it cannot read chosen in the folder package with that source «already declared» and locked, disabled in the complete project; the AI texts kept for the flow version offered with the AI off, sent and remembered, and the hint naming the models that wrote them. The gateway's plans are served without the AI options and the AI checks serve their own plans with the export answered inside the page: no paid call |
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
- For `browser_dialogo.py` and `browser_dialogo_stato.py`: Firefox (headless, Marionette on port 2828).
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
