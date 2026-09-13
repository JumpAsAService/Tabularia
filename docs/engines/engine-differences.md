# Engine differences

Tabularia runs the same flow on four interchangeable engines. Most of the semantics are
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
external ClickHouse. It also means **the engine can change between the result you saw and
the result you ship**. Every run records the engine it actually ran on — check it in the
run history when a number surprises you.

## Operation coverage

| Engine | id | Operations | Notes |
|---|---|---|---|
| Polars | `polars` | all 20 | Default. In-process, lazy, streaming. |
| DuckDB | `duckdb` | all 20 | Out-of-core SQL, spills to disk. Good for very large joins and aggregations. |
| chDB | `chdb` | 19 — no `foreach` | Out-of-core SQL, ClickHouse dialect, in-process. |
| ClickHouse (external) | `clickhouse` | 19 — no `foreach` | Same dialect and operation set as chDB, executed on a remote server. |

**There is no silent fallback.** If an engine does not implement an operation, the run
fails with an explicit message naming the engine and suggesting another one — it does not
quietly hand the work to a different engine. (The external ClickHouse engine reuses chDB's
implementations, so its message mentions chDB.)

## What is guaranteed identical

These are the semantics of SQL, and they hold on every engine. They are verified by an
oracle suite where each case declares a hand-computed expected result, run against all
four engines:

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
| `not_in` with a `NULL` in the list | DuckDB returns **zero rows** (strict SQL: `<> NULL` is never true). | DuckDB |
| `round()` in `compute` | Polars and the ClickHouse engines round half to **even** (`0.5 → 0`); DuckDB rounds half **away from zero** (`0.5 → 1`). Relevant for money. | DuckDB |
| `%` (modulo) with negative numbers | Polars takes the sign of the divisor (`-5 % 3 = 1`); the others take the sign of the dividend (`-5 % 3 = -2`). | Polars |
| `concat()` in `compute` | Polars and DuckDB skip a `NULL` argument; the ClickHouse engines return `NULL`. The `\|\|` operator is consistent everywhere — prefer it. | chDB, ClickHouse |
| `contains` / `ends_with` on a **numeric** column | The number is turned into text differently: `100.0` on Polars and DuckDB, `100` on the ClickHouse engines. | chDB, ClickHouse |
| `sort` with a per-column list of directions | DuckDB and chDB apply the first direction to **every** column; Polars applies them per column. | DuckDB, chDB |
| `join` with key columns of different types | DuckDB coerces and finds matches; Polars and the ClickHouse engines raise an error. | DuckDB |
| `cast` to boolean | Polars **fails** on text; DuckDB parses `"true"`/`"false"`; the ClickHouse engines return an integer and turn `-1` into `NULL`. | all |

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
| Integer overflow in `sum` | Polars and chDB wrap around silently; DuckDB raises an error. |
| `std` / `var` of a single value | Polars and DuckDB give `NULL`; the ClickHouse engines give `NaN`, which is **not** replaced by "fill null" downstream. |
| `first` / `last`, and which row `unique` keeps | Deterministic in practice in-process, but not guaranteed on a parallel ClickHouse server. Sort explicitly if the choice matters. |
| Casting text to `datetime` | The ClickHouse engines parse almost anything, including a numeric string read as a Unix timestamp; Polars and DuckDB only accept standard formats and return `NULL` otherwise. |
| Casting text to `date` | Permissiveness varies: `2024-1-5` works on Polars and DuckDB, `2024/01/05` only on DuckDB. Use ISO dates. |
| Integer division `//` | Polars floors (`-7 // 2 = -4`), DuckDB truncates (`-3`). `DIV` / `intDiv` exist only on the ClickHouse engines. |
| SQL functions inside `compute` | `compute` parses real SQL expressions, so the dialect is the engine's: `year(d)` works on DuckDB and the ClickHouse engines but not on Polars (use `EXTRACT`); `^` is a power operator only on DuckDB. These fail loudly. |
| Whitespace trimmed when casting text to a number | Polars removes tabs and newlines too; DuckDB and the ClickHouse engines remove spaces only. |

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

## Where these findings come from

The catalogue above is the result of an audit that ran the same inputs through all four
engines and compared them. The full list, with severities, file references and the exact
inputs and outputs of each case, is in [`../audit/AUDIT-2026-09-13.md`](../audit/AUDIT-2026-09-13.md).
