# Spec: data contracts

Status: draft, 2026-10-06. Decisions marked **(owner)** were taken by the project
owner; the rest are assumptions to correct.

## Objective

A datasource in the catalog is read by flows, views and people who did not produce
it. Today nothing says what they may rely on: a refresh that brings an empty table,
a flow that starts publishing nulls in a key, a renamed column — all are published
and discovered downstream, by whoever notices.

A **data contract** is what the producer of a datasource promises to its consumers:
which columns exist and of what type, which may not be null, what is unique, which
values are legal, how many rows to expect, how fresh the data is. Tabularia checks
the promise every time the datasource is about to receive new data, and shows at a
glance whether it holds.

Users: whoever owns a datasource (writes the contract), whoever builds on it (reads
the contract and its state), whoever operates the installation (is told when one
breaks).

Success looks like: a refresh that would break a contract with a blocking rule does
not replace the data consumers are reading; the catalog shows, on every datasource
with a contract, one icon that says "respected", "warning" or "broken".

## Decisions

- **(owner) The contract lives on the datasource**, not on a node of a flow. It
  covers both ways a datasource receives data: a refresh from a database and the
  publication of a flow output.
- **(owner) Two severities, per rule, and only two.** A rule is either a *warning*
  (non-blocking: the data is published and the violation is reported) or an *error*
  (blocking: the new snapshot is **not** published, the datasource keeps the last
  good one, the run fails saying which rule broke).
- **(owner) A datasource with a contract shows an icon**, and the icon tells the
  state: success when every rule holds, warning when only warning rules are
  violated, error when a blocking rule is violated.
- **(owner) ODCS**: the contract can be downloaded in the Open Data Contract
  Standard (YAML). Import comes later.
- One evaluator, written once on Polars, whatever engine produced the data. The
  output of every engine is a parquet in the object storage; evaluating there keeps
  the five engines out of it.
- Who may write a contract: EDIT on the datasource's folder. Who may read it and
  its state: VIEW.
- The contract can be proposed from the current data (types, no-nulls, uniqueness,
  low-cardinality values) and then corrected by hand.

## Capability map

| Module id | Responsibility | Depends on |
|---|---|---|
| `contract-model` | the contract document, its versions, API, permissions, audit, ODCS export | — |
| `contract-validation` | the evaluator: a parquet and a contract in, a report rule by rule out; profiling for the proposal | `contract-model` |
| `contract-enforcement` | evaluation at every refresh and publish, before the snapshot swap; state of the datasource; history; freshness over time | `contract-validation` |
| `contract-ui` | authoring, the state icon everywhere a datasource is listed, the report, the proposal | `contract-model` |
| `contract-notify` | telling the owner and the consumers when a contract breaks | `contract-enforcement` |

Build order: `contract-model` → `contract-validation` → `contract-enforcement`;
`contract-ui` alongside, from the moment the model exists; `contract-notify` last.

## The contract

One contract per datasource. A JSON document, versioned (every save is a new
version, like flows), with an on/off switch.

```json
{
  "description": "Order lines, one per product ordered.",
  "rules": [
    {"id": "r1", "kind": "column",          "column": "order_id", "dtype": "integer", "severity": "error"},
    {"id": "r2", "kind": "not_null",        "column": "order_id",                    "severity": "error"},
    {"id": "r3", "kind": "unique",          "columns": ["order_id", "line"],         "severity": "error"},
    {"id": "r4", "kind": "accepted_values", "column": "status", "values": ["open", "shipped"], "severity": "warning"},
    {"id": "r5", "kind": "range",           "column": "quantity", "min": 0,          "severity": "warning"},
    {"id": "r6", "kind": "pattern",         "column": "sku", "regex": "^[A-Z]{3}-\\d+$", "severity": "warning"},
    {"id": "r7", "kind": "row_count",       "min": 1,                                "severity": "error"},
    {"id": "r8", "kind": "freshness",       "max_age_hours": 26,                     "severity": "warning"},
    {"id": "r9", "kind": "expression",      "name": "dates in order", "sql": "shipped_at >= ordered_at", "severity": "error"}
  ]
}
```

| Kind | Holds when | Notes |
|---|---|---|
| `column` | the column exists and, if `dtype` is given, belongs to that family | families: `integer`, `number` (any numeric), `string`, `boolean`, `date`, `datetime` |
| `not_null` | no null in the column | |
| `unique` | no two rows share the same value of `columns` (one or more) | nulls count as values |
| `accepted_values` | every non-null value is in `values` | |
| `range` | every non-null value is ≥ `min` and ≤ `max` (either may be absent) | numbers, dates, datetimes |
| `pattern` | every non-null value matches `regex` in full | strings |
| `row_count` | the number of rows is ≥ `min` and ≤ `max` | |
| `freshness` | the snapshot is at most `max_age_hours` old; with `column`, the newest value of that column is | the only rule that can break without new data arriving |
| `expression` | the SQL boolean expression is true on every row | null counts as a violation; the SQL is a Polars SQL expression, as in the Computed column node |

Limits, checked when the contract is saved: at most 200 rules; `severity` is exactly
`warning` or `error`; a rule on a column names a column; regexes compile; an
expression parses.

## The report

Evaluating a contract on a snapshot gives a report:

```json
{
  "outcome": "failed",
  "rows": 105035,
  "rules": [
    {"id": "r2", "passed": true},
    {"id": "r4", "passed": false, "violations": 12, "sample": ["cancelled", "lost"], "message": "12 values outside the accepted list"},
    {"id": "r7", "passed": false, "observed": 0, "message": "0 rows, at least 1 expected"}
  ]
}
```

`outcome` is `failed` if a rule with severity `error` does not hold, `warning` if
only warning rules do not hold, `passed` otherwise. A rule that cannot be evaluated
(its column is missing, the expression does not run on this data) does not hold,
and says why: a contract never passes because it could not be checked.

`sample` holds at most five offending values of the rule's own column, cut at 80
characters. They are data, and are shown only to who can read the datasource.

## State of a datasource

| State | Icon | Means |
|---|---|---|
| none | no icon | no contract, or switched off |
| `pending` | shield, neutral | a contract exists and has not been evaluated on the current data yet |
| `passed` | shield with a check, green | every rule holds on the current data |
| `warning` | shield with an exclamation mark, amber | only warning rules are violated |
| `failed` | shield with a cross, red | a blocking rule is violated: by the current data (the contract was written or tightened after it arrived, or it grew stale), or by the last update, which was refused |

The icon is never the only carrier: a title and an accessible name say the state in
words, and the three shapes differ, not only the colours.

Where it shows: the Explore, the Datasources page, the Viewer header, the source
node and the datasource picker in the editor.

When a blocking rule refuses an update the datasource keeps serving the last good
snapshot, and its state is `failed` with the reason "the last update was refused":
the data is correct but no longer current, and consumers must be able to tell.

## Enforcement

A datasource receives data in two places — the end of a database refresh and the
publication of a flow output over an existing datasource — and both already swap
the snapshot atomically with the claim of the run. The contract is evaluated
between the write and the swap:

1. at launch the gateway hands the engine the contract of the target datasource,
   if it has one that is switched on;
2. the worker, after writing the output parquet, evaluates it and returns the report
   with the result of the task;
3. the gateway, in the same transaction that closes the run:
   - `failed` → the run becomes FAILURE with the broken rules as its error, the
     snapshot is **not** swapped, the rejected parquet goes to deferred deletion,
     the state becomes `failed`;
   - `warning` or `passed` → the snapshot is swapped and the state follows;
   - in every case the report is stored in the history.

A first publication creates the datasource: there is no contract yet, nothing is
evaluated. Outputs to a database table, to S3 or by email have no datasource and
no contract.

Other moments a contract is evaluated, none of which can block (there is no new
snapshot to refuse): when it is saved or switched on (on the current snapshot), on
demand ("Check now"), and by the scheduler for `freshness` rules, which records a
change of state once, when it happens, not at every tick.

## API (gateway)

| Route | Who | Does |
|---|---|---|
| `GET /datasources/{id}/contract` | VIEW | contract, state, last report |
| `PUT /datasources/{id}/contract` | EDIT | create or replace (new version), then evaluate |
| `DELETE /datasources/{id}/contract` | EDIT | remove contract and state (history stays) |
| `POST /datasources/{id}/contract/check` | EDIT | evaluate now on the current snapshot |
| `POST /datasources/{id}/contract/proposal` | EDIT | a contract proposed from the current data, not saved |
| `GET /datasources/{id}/contract/history` | VIEW | past reports, newest first |
| `GET /datasources/{id}/contract/odcs` | VIEW | the contract as an ODCS YAML document |

Every list that returns datasources carries `contract: {status, checked_at,
errors, warnings, blocked} | null` for each, read with one query for the whole
list.

The contract read by `GET` says whether its reader may also change it
(`editable`): the dialog shows the commands only to who can use them. The
notification settings (`notify_emails`, `notify_connection_id`) travel with `PUT`
(absent = unchanged) and are returned only to who can edit: addresses are
personal data.

## Notifications

A contract can carry addresses and an SMTP connection (CONNECT on it is needed to
*choose* it; the connection's allowed domains apply). Whoever records a report
marks the result `notify = 'pending'` when the state changed (`passed`, `warning`,
`failed`, or `refused` for a blocked update) and the trigger is one that happens on
its own (`refresh`, `publish`, `freshness`); the scheduler sends the pending ones
(`services/contract_notifier.py`), claiming each with a conditional update, so
several orchestrators send one mail. No mail is sent inside the transaction that
publishes the data. `pending → passed` is not news; a failed send is recorded and
not retried.

## ODCS export

`services/odcs.py` writes an ODCS **v3.0.2** document: the columns as schema
properties (logical + physical type), `required`/`unique` for blocking rules,
range and pattern as `logicalTypeOptions` where the logical type allows them, and
**every rule as one `quality` item** with its severity and, in `customProperties`,
its Tabularia id and kind. `nullValues`, `duplicateCount` and `rowCount` are
library rules; accepted values, ranges, composite uniqueness and expressions are
SQL checks (`mustBe: 0`); `column`, `pattern` and `freshness` are text checks, the
last also an `slaProperties` latency. YAML is emitted by hand (the gateway ships
no YAML library): every string double-quoted with JSON escapes.

## Tech stack

Unchanged: FastAPI + SQLModel (gateway), FastAPI + Celery + Polars (engine), Nuxt 3
(frontend). No new dependency: ODCS YAML is emitted by hand (strings in JSON form,
which YAML accepts).

## Commands

```bash
# gateway and engine tests (development stack)
docker compose -f infrastructure/docker-compose.yml exec -T gateway /opt/venv/bin/python -m pytest -q
docker exec -e CLICKHOUSE_TEST_HOST=chtest -e CLICKHOUSE_TEST_PORT=8123 -e CLICKHOUSE_TEST_USER=tab \
  -e CLICKHOUSE_TEST_PASSWORD=tab -e CLICKHOUSE_TEST_DB=tab dataprep-backend python -m pytest tests -q -p no:cacheprovider
# frontend types
docker exec -w /app dataprep-frontend sh -lc 'npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json'
# live sweep and browser checks against the scratch stack
~/tabularia-carico/giro.sh
```

## Project structure

```
backend/app/contracts/rules.py        the rule vocabulary and its validation (shared shape with the gateway)
backend/app/contracts/evaluator.py    parquet + contract → report (Polars); profiling for the proposal
backend/app/api/routes/contracts.py   POST /contracts/evaluate, POST /contracts/profile
backend/tests/test_contract_evaluator.py
gateway/app/models/contract.py        DataContract, DataContractVersion, DataContractResult
gateway/app/services/contracts.py     save, state, history, enforcement at the swap, freshness, proposal, ODCS
gateway/app/routes/contracts.py       the routes above
gateway/tests/test_contracts.py, test_contract_enforcement.py
frontend/composables/useContracts.ts  API and the state → icon mapping
frontend/components/ui/ContractBadge.vue, ContractDialog.vue, Modal.vue
```

## Code style

As the surrounding code: comments explain why, in Italian; identifiers of the public
vocabulary (rule kinds, states, routes) in English; a function per rule kind,
registered in a table, like the engine operations:

```python
@rule("not_null")
def _not_null(r: Rule, cols: Schema) -> Check:
    """Nessun null nella colonna."""
    return Check(violations=pl.col(r.column).is_null().sum(), offending=pl.col(r.column).is_null())
```

## Testing strategy

- **Evaluator** (engine, pytest): every rule kind, holding and not holding, on a
  small parquet; nulls, empty table, missing column, wrong type, composite key,
  invalid regex and expression; the outcome from the severities; a 2-million-row
  parquet in a time budget.
- **Gateway** (pytest, SQLite): validation of the document; versions; permissions
  (VIEW reads, EDIT writes, a stranger gets 403/404); the state in the lists; the
  three branches of enforcement at the swap, including "a refused update leaves the
  old snapshot and its key untouched"; freshness recorded once per change.
- **Live** (scratch stack, real Postgres and workers): a refresh that breaks a
  blocking rule leaves the datasource on the old snapshot and fails the run; a
  warning publishes; the same through a flow output; added to the live sweep.
- **Browser**: the icon in its four states in the Explore and in the Datasources
  page; writing a contract, the proposal, "Check now".

## Boundaries

- **Always**: evaluate before the swap, never after; store a report for every
  evaluation; keep the two write paths behaving exactly as today for datasources
  without a contract.
- **Ask first**: a new dependency; rules that need more than one pass over the data;
  changing what a run reports when no contract is involved.
- **Never**: publish a snapshot that breaks a blocking rule; let a contract pass
  because a rule could not be evaluated; show offending values to who cannot read
  the datasource; evaluate with the engine that produced the data (five
  implementations).

## Success criteria

1. A contract can be written, read, replaced and removed through the API with the
   permissions above; every save is a version.
2. Every rule kind is evaluated correctly, holding and not holding (tests).
3. A database refresh that violates an `error` rule: run FAILURE naming the rule,
   datasource still on the previous snapshot (same key, same rows), state `failed`.
4. The same refresh violating only `warning` rules: published, state `warning`.
5. The same two cases through a flow that publishes over the datasource.
6. A datasource without a contract behaves exactly as before (existing suites green).
7. Every list of datasources shows the state icon — none, pending, success,
   warning, error — with a text alternative.
8. The contract dialog proposes a contract from the data, saves it, and shows the
   report rule by rule.
9. A `freshness` rule turns the state without new data arriving, once.
10. The ODCS document is produced and is valid YAML.

## Open questions

- **Tolerances.** "At most 1% nulls" is a common request; v1 rules are exact. Add a
  threshold per rule later if exact proves too strict.
- **First publication.** A flow that *creates* a datasource cannot be checked: the
  contract does not exist yet. A contract template on the Output node would close
  the gap.
- **Consumers.** Decided in `contract-notify`: the people told by email are the
  addresses written on the contract, nobody else — mailing every owner of a flow
  that reads the datasource would be mail nobody asked for. Consumers are told
  where they work: the state icon is on the source nodes of their flows, in the
  Viewer and in the lists. A lineage-driven "suggest the consumers" in the dialog
  is a possible later step.
