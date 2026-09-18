# Engine differences

Tabularia runs the same flow on five interchangeable engines. Most of the semantics are
guaranteed to be identical — that guarantee is defined below and enforced by a test suite.
The rest is genuine engine behaviour, and **this page is the contract for it**: read it
before you pick an engine, and especially before you run a flow on an engine other than
the one you designed it with.

## Which engine runs your flow

Every flow carries two engine settings:

- the **development engine** — used by the editor: previews, the Viewer, manual runs;
- the **production engine** (optional, per flow) — used by scheduled runs and *Run in
  production*. When unset, production uses the development engine.

This is deliberate: you can design on Polars locally and run the scheduled DAG on an
external ClickHouse or on BigQuery. It also means **the engine can change between the result you saw and
the result you ship**. Every run records the engine it actually ran on — check it in the
run history when a number surprises you.

## Operation coverage

| Engine | id | Operations | Notes |
|---|---|---|---|
| Polars | `polars` | all 20 | Default. In-process, lazy, streaming. |
| DuckDB | `duckdb` | all 20 | Out-of-core SQL, spills to disk. Good for very large joins and aggregations. |
| chDB | `chdb` | 19 — no `foreach` | Out-of-core SQL, ClickHouse dialect, in-process. |
| ClickHouse (external) | `clickhouse` | 19 — no `foreach` | Same dialect and operation set as chDB, executed on a remote server. |
| BigQuery | `bigquery` | 19 — no `foreach`; no `median` inside `pivot` | GoogleSQL over temporary external tables on Google Cloud Storage; results through the Storage Read API. |

**There is no silent fallback.** If an engine does not implement an operation, the run
fails with an explicit message naming the engine and suggesting another one — it does not
quietly hand the work to a different engine. (The external ClickHouse engine reuses chDB's
implementations, so its message mentions chDB. BigQuery names itself: a median inside a
pivot, for instance, is refused with a message suggesting another engine.)

## What is guaranteed identical

These are the semantics of SQL, and they hold on every engine. They are verified by an
oracle suite where each case declares a hand-computed expected result, run against all
five engines (BigQuery live, against a real project):

- comparisons with `NULL` are false — `ne` and `not_in` do **not** keep nulls;
- aggregates ignore nulls; `count` and `distinct count` never count nulls; `sum`, `mean`,
  `min`, `max` over an all-null group return `NULL`;
- `std` and `var` are the **sample** versions (n−1), and `NULL` with fewer than 2 values;
- `median` uses linear interpolation (R7);
- sorting always puts **nulls last**, ascending and descending — a "top N" never returns
  nulls;
- a failed cast yields `NULL`, never an error; numbers cast to integer are **truncated**,
  not rounded; numeric strings are trimmed of surrounding whitespace;
- in a join, a `NULL` key never matches; a missing side is `NULL`, never a default value;
  a non-key column with the same name on the right gets the `_right` suffix;
- a datetime without timezone is an instant in **UTC**.

## What differs

Two kinds of difference. The first kind is the dangerous one, because the result changes
without any error.

### Differences that change results silently

| Operation | What differs | Engines |
|---|---|---|
| `contains` | **Polars treats the text as a regular expression**; the others match a literal substring. | Polars vs all others |
| `not_in` with a `NULL` in the list | DuckDB and BigQuery return **zero rows** (strict SQL: `<> NULL` is never true). | DuckDB, BigQuery |
| `round()` in `compute` | Polars and the ClickHouse engines round half to **even** (`0.5 → 0`); DuckDB and BigQuery round half **away from zero** (`0.5 → 1`). Relevant for money. | DuckDB, BigQuery |
| `%` (modulo) with negative numbers | Polars takes the sign of the divisor (`-5 % 3 = 1`); the others take the sign of the dividend (`-5 % 3 = -2`). | Polars |
| `concat()` in `compute` | Polars, DuckDB and BigQuery skip a `NULL` argument; the ClickHouse engines return `NULL`. The `\|\|` operator is consistent everywhere — prefer it. | chDB, ClickHouse |
| `contains` / `ends_with` on a **numeric** column | The number is turned into text differently: `100.0` on Polars and DuckDB, `100` on the ClickHouse engines and BigQuery. | chDB, ClickHouse, BigQuery |
| `sort` with a per-column list of directions | DuckDB, chDB and BigQuery apply the first direction to **every** column; Polars applies them per column. | DuckDB, chDB, BigQuery |
| `join` with key columns of different types | DuckDB coerces and finds matches; Polars, the ClickHouse engines and BigQuery raise an error. | DuckDB |
| `cast` to boolean | Polars **fails** on text; DuckDB and BigQuery parse `"true"`/`"false"` (BigQuery: any non-zero number is `true`); the ClickHouse engines return an integer and turn `-1` into `NULL`. | all |

**The size of the `contains` difference, measured on real data.** On a table of 150,000
Italian company names, 37,396 of which contain a full stop, the filter `contains "."`
returns **150,000 rows on Polars** and **37,396 on DuckDB and chDB**: to Polars the full
stop is a metacharacter matching any character. A filter written during design and run in
production on another engine can silently go from "every customer" to "a quarter of them".

Until `contains` is made uniform, treat it as a **regex on Polars** and a **literal
substring elsewhere**. For literal matching that behaves the same everywhere, prefer
`starts with` and `ends with`, which have no such difference.

### Engine-level behaviour

Legitimate dialect differences. Know them, choose accordingly.

| Topic | Behaviour |
|---|---|
| Integer overflow in `sum` | Polars and chDB wrap around silently; DuckDB and BigQuery raise an error. |
| `std` / `var` of a single value | Polars, DuckDB and BigQuery give `NULL`; the ClickHouse engines give `NaN`, which is **not** replaced by "fill null" downstream. |
| `first` / `last`, and which row `unique` keeps | Deterministic in practice in-process, but not guaranteed on a parallel ClickHouse server nor on BigQuery (`ANY_VALUE`, `ROW_NUMBER` over an unordered partition). Sort explicitly if the choice matters. |
| Casting text to `datetime` | The ClickHouse engines parse almost anything, including a numeric string read as a Unix timestamp; Polars, DuckDB and BigQuery only accept standard formats and return `NULL` otherwise. |
| Casting text to `date` | Permissiveness varies: `2024-1-5` works on Polars, DuckDB and BigQuery, `2024/01/05` only on DuckDB. Use ISO dates. |
| Integer division `//` | Polars floors (`-7 // 2 = -4`), DuckDB truncates (`-3`), BigQuery translates it to `DIV`, which truncates. `DIV` / `intDiv` exist only on the ClickHouse engines and BigQuery. |
| SQL functions inside `compute` | `compute` parses real SQL expressions, so the dialect is the engine's: `year(d)` works on DuckDB and the ClickHouse engines but not on Polars (use `EXTRACT`); `^` is a power operator only on DuckDB. These fail loudly. **BigQuery transpiles the expression from the DuckDB dialect with sqlglot** (`%` → `MOD`, `strftime` → `FORMAT_DATE`, `year()` → `EXTRACT`, `//` → `DIV`); GoogleSQL-only functions pass through untouched, and an expression sqlglot cannot parse is sent as written. |
| Whitespace trimmed when casting text to a number | Polars and BigQuery remove tabs and newlines too; DuckDB and the ClickHouse engines remove spaces only. |
| `sample` (random p%) | Deterministic on every engine, but each engine hashes rows its own way, so the *same* fraction picks *different* rows on Polars, DuckDB, the ClickHouse engines and BigQuery (`FARM_FINGERPRINT`). |
| Column names on BigQuery | BigQuery only accepts letters, digits and underscores. Names such as `Ragione Sociale` or `Importo (€)` are mapped to safe names inside the query and restored in the results; nothing changes for the flow, but a `compute` or `sql` expression must quote such a column with backticks exactly as the flow names it. |

## Rules of thumb

1. **Keep the production engine equal to the development engine** unless you have a
   reason not to. When they differ, re-check any flow that uses an operation from the
   first table above.
2. **Avoid `contains` with punctuation** (`.`, `(`, `+`, `*`) until it is uniform, or
   accept that it means a regular expression on Polars.
3. **Prefer `\|\|` over `concat()`**, and ISO dates in text you cast.
4. **Sort explicitly** when you rely on `first`, `last`, or which duplicate `unique` keeps.
5. **Check the engine recorded on the run** when a production number differs from what you
   saw in the editor. It is the first thing to look at.
6. **On BigQuery, write `compute` expressions in the DuckDB dialect**: they are translated;
   GoogleSQL-only functions work too but will not run elsewhere.

## Where these findings come from

The catalogue above is the result of an audit that ran the same inputs through the four
in-process and ClickHouse engines and compared them; the BigQuery entries come from the
same oracle suite run live against a GCP project on 2026-09-18. The full list, with severities, file references and the exact
inputs and outputs of each case, is in [`../audit/AUDIT-2026-09-13.md`](../audit/AUDIT-2026-09-13.md).
