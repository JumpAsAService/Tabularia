# Adding a node to the flow editor

A node of the flow editor is an **operation** the engine knows: `filter`, `group_by`,
`pivot`, `stat_t_two_sample`. This guide takes you from nothing to a pull request whose
checks pass at the first push. It is written for someone who has not read the code before:
every step names the file, shows a few lines of the real code to copy the shape of, the
test to write and the command that runs it.

There are three ways to add a node, and the code treats them differently. Choose first.

| You want to add | Follow |
|---|---|
| A transformation of the rows or columns: a new `filter`, `reorder`, `pivot`… | [Path 1: a transformation](#path-1-a-transformation) |
| A hypothesis test (`stat_<id>`): a new member of the **Statistics** group | [Path 2: a statistical test](#path-2-a-statistical-test) |
| A statistical model (`model_<id>`): a new member of the **Models** group, fitted in a Job on Kubernetes | [Path 3: a statistical model](#path-3-a-statistical-model) |

A statistical test is not a transformation: it has its own catalogue, one function for
its final computation, a report and a generic form. If your node reads the data and writes
a table of results of a test, it is Path 2. A model is neither: no engine fits it. It is
code in R or Python, in an image Tabularia builds per group, that runs as a Job; its path is
the longest, and it is described below from what the models already added. Everything else is Path 1.

Before you start, read [Engine differences](../engines/engine-differences.md): a node must
give the same result on the five engines (Polars, DuckDB, chDB, the external ClickHouse and
BigQuery), and that page says what "the same" means.

## What you need

- Docker, to build the two development images and run the engine's and the gateway's suites
  as CI does. The first build takes a while and the suites start real ClickHouse, PostgreSQL,
  MinIO and Valkey containers: keep a few GB of memory free.
- Node 22 and `npm ci` in `frontend/` for the interface checks (CI uses Node 22).
- Go, only if you touch `gateway-go/` (CI runs it in the `golang:1.27.3-bookworm` image).
- Python 3.11 or later, for the catalogue script of Path 2 (standard library only).

The repository's map and the rules for pull requests are in `CONTRIBUTING.md`.

## Path 1: a transformation

The model used below is `reorder`, the simplest operation with a complete trail in the
history: commit `d6794def` added it to the engine, the dbt export and the editor. Three
later commits added what did not exist yet: `4c540b14` (the BigQuery engine), `d57651e0`
(the flow assistant) and `9ea7dcd5` (the editor's right-click menu). You can follow any of
them with `git show --stat <commit>`. The newest operation that was added in one commit is
`sample` (`9934f585`, with the oracle suite): it is a good second model when your node has a
seed or a fraction. No new transformation has been added since September 2026, so the
list below was checked against the code as it is today, not against a recent commit.

### The whole list, at a glance

| # | Where | File |
|---|---|---|
| 1 | The operation, on Polars | `backend/app/engine/operations.py` |
| 2 | The operation, on DuckDB | `backend/app/engine/duckdb_ops.py` |
| 3 | The operation, on chDB and on the external ClickHouse | `backend/app/engine/chdb_ops.py` |
| 4 | The operation, on BigQuery | `backend/app/engine/bigquery_ops.py` |
| 5 | Tests of the engine | `backend/tests/test_operations.py`, `test_data_correctness.py`, `test_bigquery_ops.py` |
| 6 | The dbt export | `backend/app/engine/dbt_export.py`, `backend/tests/test_dbt_export.py` |
| 7 | The Go gateway | `gateway-go/internal/api/flow_shortcuts.go` (and `dbtexport/payload.go`) |
| 8 | The form, the icon, the summary | `frontend/composables/useFlowModel.ts`, `useOpIcons.ts`, `opSummary.ts` |
| 9 | The words in five languages | `frontend/i18n/locales/{en,it,de,es,fr}.ts` |
| 10 | Interface tests | `frontend/tests/flow-shortcuts.test.mjs` |
| 11 | The AI assistant and MCP | `gateway/app/services/ai_flow_agent.py`, then `prompts.json` and `tools.json` |
| 12 | Documentation and changelog | `docs/capabilities/flows.md`, `docs/engines/engine-differences.md`, `CHANGELOG.md` |

There is no file where an operation is "declared" with a schema. An operation exists
because a function is registered under its name in the four Python registries; its
parameters are a plain dictionary that each function reads and checks by itself, and the
form that edits it is declared in the interface (step 8). The palette needs nothing: the
editor asks the engine for the registered names (`GET /tasks/operations`) and lists them,
so a registered operation shows up in the palette, with a generic icon until you give it
one.

### Step 1. Polars: `backend/app/engine/operations.py`

An operation is a pure function `(lf, params, ctx) -> lf` on a Polars `LazyFrame`,
registered with `@register("name")`. Use `_require(params, "key")` for a required
parameter and raise `EngineError` with a sentence that says what to do. This is the
whole of `reorder`:

```python
@register("reorder")
def op_reorder(lf: pl.LazyFrame, params: dict[str, Any], ctx: OperationContext) -> pl.LazyFrame:
    # reorder: the listed columns first (in this order), then ALL the others (so the
    # columns added upstream after the configuration are not lost).
    order = _require(params, "columns")
    return lf.select([*order, pl.exclude(order)])
```

Keep it lazy and streaming: do not collect inside an operation. If it needs a second input
(as `join` and `union` do), read how those two use `_build_right` and how the flow is
resolved first (`gateway-go/internal/flowresolver/flowresolver.go` treats `join` and `union`
specially); that path is not covered by this guide.

Test: `backend/tests/test_operations.py` tests one operation on a small frame:

```python
def apply(op_type: str, params: dict, df: pl.DataFrame | None = None, ctx=None) -> pl.DataFrame:
    """Applies one operation and materializes the result."""
    lf = (df if df is not None else df_base()).lazy()
    return get_operation(op_type)(lf, params, ctx).collect(engine="streaming")
```

Check: see "Running the engine's tests" below.

### Steps 2 to 4. The other four engines

The three SQL engines each have a registry of their own, with the same names. The
functions differ in what they receive, and none of them runs the query: they return the
next step of the chain.

| Engine | File | Signature | Returns |
|---|---|---|---|
| DuckDB | `duckdb_ops.py` | `(rel, params, a, ctx)` | `rel.query(a, "SELECT ...")` |
| chDB and the external ClickHouse | `chdb_ops.py` (the external engine reuses it entirely: `clickhouse_engine.py` imports `get_chdb_operation`) | `(sql, params, ctx)` | an SQL string |
| BigQuery | `bigquery_ops.py` | `(sql, params, ctx)` | an SQL string |

The same operation on each of them:

```python
# duckdb_ops.py
@_register("reorder")
def op_reorder(rel, params, a, ctx):
    # the listed columns first (in order), then all the others (EXCLUDE the listed ones)
    order = _require(params, "columns")
    q = ", ".join(_qi(c) for c in order)
    return rel.query(a, f"SELECT {q}, * EXCLUDE ({q}) FROM {a}")

# chdb_ops.py
@_register("reorder")
def op_reorder(sql, params, ctx):
    # the listed columns first (in order), then the others (EXCEPT the listed ones)
    order = _require(params, "columns")
    cols = ", ".join(_qi(c) for c in order)
    return f"SELECT {cols}, * EXCEPT ({cols}) FROM {_sub(sql)}"

# bigquery_ops.py
@_register("reorder")
def op_reorder(sql, params, ctx):
    order = _require(params, "columns")
    cols = ", ".join(ctx.qi(c) for c in order)
    return f"SELECT {cols}, * EXCEPT ({cols}) FROM {_sub(sql)}"
```

Always quote names with the engine's `_qi` (`ctx.qi` on BigQuery) and write literals with
`_lit`: column names with spaces, quotes and accents are part of the test data.

`backend/tests/test_engine_registries.py` compares the names in the four registries and
fails when one engine lacks an operation the others have. Without it, a missing operation
would only show when someone runs a flow with it: DuckDB refuses it with "operation 'x' is
not supported by the DuckDB engine yet". `foreach` is the one operation that exists on
Polars and DuckDB only; the test keeps it in an explicit list of exceptions (`EXCEPTIONS`),
which must agree with [Engine differences](../engines/engine-differences.md#operation-coverage).
A new exception goes in both places. If your node cannot be
written on an engine, say so there and refuse it with a sentence that names another engine,
as `pivot` does for the median on BigQuery.

If your node writes SQL that changes what a step gives under the same parameters (a fix to
an existing operation, rather than a new one), the step cache must not reuse old copies.
The cache's keys carry the release of the app, which handles releases; inside a release
only `_FINGERPRINT_NAME` in `backend/app/engine/cache.py` moves the ClickHouse copies on
(`"clickhouse": "clickhouse.2"`). A brand-new operation does not need it.

Two optional lists let the editor skip work, and leaving your node out of them is always
safe, only slower: `PRESERVING` in `backend/app/engine/rowcount.py` (operations that keep
every row, so the row count of the step before is reused) and `KEEPS_VALUES` in
`backend/app/engine/profile.py` (operations after which the parquet footer's statistics
still describe the column). Add your node only if it really keeps every row, or every
value. `_SCAN_HEAVY` in `backend/app/engine/clickhouse_engine.py` is for operations that
read the whole table on ClickHouse (it raises the thread count for them).

#### The standard of correctness, equal across engines

A node is not done when it runs on each engine: it is done when it gives the **same
rows, columns and types** on each. The semantics are those of SQL, listed in
[Engine differences](../engines/engine-differences.md#what-is-guaranteed-identical): a
comparison with `NULL` is false, aggregates ignore nulls, sorting puts nulls last, a failed
cast is `NULL`, a `NULL` join key never matches, a datetime without zone is UTC. The
module docstring of `backend/tests/test_data_correctness.py` has the full list. When your
node has a place where the engines would naturally differ, write the helper once, in
`operations.py`, and import it from the other registries, as `sample_threshold`,
`sort_keys` and `pivot_params` are.

### Step 5. The tests of the engine

Three files, each answering a different question.

**Do all the engines agree, with data that is hard?** Add a test to
`backend/tests/test_data_correctness.py`. Each case states the expected rows computed by
hand (not by running the code) on the dataset `ORDERS`, which has nulls, empty strings,
apostrophes, accents, spaces in column names and integers past 2^53. The test is
parametrised on `ENGINES`, so one function proves Polars, DuckDB, chDB, the external
ClickHouse (when `CLICKHOUSE_TEST_HOST` is set, which CI does) and BigQuery (only with
`BIGQUERY_LIVE=1`, never in CI):

```python
@pytest.mark.parametrize("name", ENGINES)
def test_select_reorder_drop_rename_with_hard_names(storage, orders, name):
    res = run(name, storage, orders, [{"type": "select", "params": {"columns": ["Ragione Sociale", "id"]}}])
    assert [c.name for c in res.columns] == ["Ragione Sociale", "id"] and res.row_count == 8
    res = run(name, storage, orders, [{"type": "reorder", "params": {"columns": ["note", "id", "cliente"]}}])
    assert [c.name for c in res.columns][:3] == ["note", "id", "cliente"] and len(res.columns) == 15
```

`check(name, storage, orders, ops, expected_rows)` compares the result with a list of
expected rows. For a node that reshapes the table (changes the columns' names or types),
also add cases to `CASES` in `backend/tests/test_reshape_parity.py`, which compares every
other engine with Polars.

**Is the BigQuery SQL what we meant?** BigQuery cannot run in CI. `backend/tests/test_bigquery_ops.py`
checks the generated SQL string against a fake context:

```python
def test_column_ops_use_except_replace():
    assert op("select", {"columns": ["id", "nome"]}) == "SELECT `id`, `nome` FROM (SELECT * FROM `_src1`)"
    assert op("reorder", {"columns": ["nome"]}).startswith("SELECT `nome`, * EXCEPT (`nome`) FROM")
```

**Does one operation behave on its own?** `backend/tests/test_operations.py` (Polars only).

#### Running the engine's tests

CI builds the development image and runs every test of `backend/tests` inside it, beside
real servers. The same script runs on a laptop:

```bash
docker build --build-arg WITH_DEV=true --build-arg WITH_CHDB=true -t tabularia-backend:ci backend
.github/scripts/ci_tests.sh engine tabularia-backend:ci
```

The script fails for any skipped test whose reason is not one of the four it allows
(`BIGQUERY_LIVE`, `ORACLE_TEST_HOST`, `SQLSERVER_TEST_HOST`, `mssql_python`): a test of
yours that skips because a server is missing breaks the run (`.github/scripts/ci_skips.py`).

The script always runs the whole suite. To iterate on one file, run the same image
without the servers (the tests that need them skip, which is fine locally; this is not a
command of the repository, only what the script does with fewer parts):

```bash
docker run --rm -v "$PWD/backend:/app" -w /app tabularia-backend:ci \
  python -m pytest tests/test_operations.py -q -p no:cacheprovider -k reorder
```

Always run the full script once before you push: the external ClickHouse in CI is the
version 24.8 server, and the dialect of newer ones is not the same.

### Step 6. The dbt export

Every operation is either **exportable** to dbt (its SQL is written by the exporter) or
**refused** with the code `op_not_exportable`. Decide, and say which in the pull request.

*Refused*: write nothing. `_compile_op` in `backend/app/engine/dbt_export.py` ends with

```python
    raise DbtExportError(f"operation '{op_type}' cannot be exported to dbt", "op_not_exportable", op=op_type)
```

and the message exists in the five languages already. Add a test beside
`test_a_statistics_step_is_not_exported` in `backend/tests/test_dbt_export.py`, and one
line to the limits in `docs/dbt-export.md`.

*Exportable*: add a branch in `_compile_op`, which returns the new reference and the
schema after the step. `reorder`'s branch is:

```python
    if op_type == "reorder":
        order = _require(params, "columns")
        new_ones = list(order) + [c for c in cols if c not in set(order)]
        if _needs_list(model, s, "reorder"):
            return model.add(f"SELECT {_column_list(new_ones)} FROM {ref}"), _S(new_ones, types)
        qs = ", ".join(q(x) for x in order)
        # with the columns not known, the listed ones are only the first of them
        after = _S(new_ones, types) if cols else _S([], types, s.others)
        return model.add(f"SELECT {qs}, * EXCLUDE ({qs}) FROM {ref}"), after
```

Then, in the same file: the step's name and its sentence for the generated model
(the `simple` dictionary: `"reorder": ("reorder_columns", lambda: f"put first ...")`),
the order a following `first`/`last` must respect (`_order_after`: does your step keep
the order of the rows?) and, in `_wrong_shape`, the shape check of the parameters
(`"reorder": lambda: list_of(p.get("columns"), str)`).

If your node has parameters it cannot work without, add them to `required` in
`gateway-go/internal/dbtexport/payload.go` (the `required` map) so an incomplete step is
refused before anything is sent. The Python gateway keeps the same table
(`_REQUIRED` in `gateway/app/services/dbt_export.py`); it is frozen, and
`TestRequiredAsPython` in `gateway-go/internal/dbtexport/required_test.go` reads it as text
and fails if the two tables differ. A new step with required parameters therefore makes it
fail while the Python gateway stays frozen: say so in the pull request, and the test is
adapted then (an explicit list of Go-only steps).

Tests: add your step to the parametrised list that precedes
`test_every_steps_federated_sql_really_runs_in_duckdb` in `backend/tests/test_dbt_export.py`
(the SQL really runs in DuckDB), and a case that goes through `_parity` and `_equal` (the
exported model gives the rows the Polars and DuckDB engines give; see
`test_a_compute_after_a_pivot_replaces_the_column_in_place_or_adds_it`). The end-to-end matrix `e2e/dbt_export/matrix.py` (a real dbt and a real
ClickHouse) is not part of CI: add a case to it if you can run a whole stack, and say
whether you did.

```bash
.github/scripts/ci_tests.sh engine tabularia-backend:ci   # test_dbt_export.py is in it
```

A new error code of the exporter (a new `DbtExportError` code) needs its message in the
five languages in `gateway/app/services/dbt_messages.py`; the Go gateway holds
the same messages in `gateway-go/internal/dbtexport/messages_gen.go`. There is no generator
in the repository: the Go file is edited by hand and its header says so. Change both files
in the same commit.

### Step 7. The Go gateway

The Go gateway does not validate a flow's operations when the flow is saved: the flow is
stored as JSON and the engine refuses what it does not know when it runs. Two places in Go
name the operations, and one of them you must touch.

1. **The right-click menu's allow-list**, `transformOps` in
   `gateway-go/internal/api/flow_shortcuts.go`, written by hand and in this order:

   ```go
   var transformOps = []any{
   	"select", "reorder", "drop", "rename", "cast", "compute", "sql", "filter", "sort", "limit",
   	"unique", "sample", "fill_null", "drop_nulls", "group_by", "pivot", "unpivot", "join", "union",
   }
   ```

   Add your name. Without it, `PUT /users/me/flow-shortcuts` refuses your node (a
   `literal_error`) when a person tries to put it in their menu.
2. `required` in `gateway-go/internal/dbtexport/payload.go`: step 6.

The interface test of step 10 reads this list and fails when it differs from the
interface's list, so the two cannot drift. Test and check:

```bash
cd gateway-go && go test ./internal/...
```

CI runs more than that: `gateway-go/scripts/test.sh` starts a PostgreSQL, creates the
schema with the gateway image's migrations, then runs `gofmt -l .` (the run fails if a
file is listed), `go vet ./...`, `go test -count=1 ./...`, and the same for `dataapi/`.
Run `gofmt -l .` yourself; `go test` alone does not catch formatting.

### Step 8. The editor: the form, the icon, the summary

Three files in `frontend/composables/`.

**The form: `useFlowModel.ts`, `OP_SPECS`.** One entry per operation, a list of fields; each
field has a `key` (the parameter's name in the engine), a `label` (a word of step 9) and a
`control`:

```ts
  reorder: [{ key: 'columns', label: 'params.reorder_columns', control: 'reorder' }],
  limit: [{ key: 'n', label: 'params.limit_n', control: 'number' }],
  unique: [{ key: 'subset', label: 'params.unique_subset', control: 'columns', optional: true }],
```

The controls that exist are the union of `FieldSpec['control']`: `column`, `columns`,
`operator`, `func`, `dtype`, `how`, `text`, `number`, `boolean`, `value`, `castlist`,
`renamelist`, `filllist`, `agglist`, `exprlist`, `strategy`, `joinkeys`, `reorder`,
`sortlist`, `json`, `sqltext`. Reuse one if it fits: then `frontend/components/ui/ParamForm.vue`
needs no change (a `text`, `number` or `boolean` field is written under its `key` by the
`default` branch). A new control means a new member of that union, a branch that fills its
state when the form opens and one that writes the parameters back, and a block in the
template: `reorder` is the one to read (`ParamForm.vue`, search `reorder`). If the node
starts with defaults, add them to `defaultParams` in the same file.

**The icon and colour: `useOpIcons.ts`, `OP_META`.** Without an entry the palette shows a
gear and the operation's id. The colour says the group (blue: columns, orange: rows,
violet: nulls, green: combining and reshaping):

```ts
  reorder:    { icon: ArrowLeftRight, label: 'ops.reorder', color: '#4f8cff' },
```

Import the icon from `lucide-vue-next` at the top of the file.

**The line under the node: `opSummary.ts`**, a `case` in `opSummary` that returns a short
text from the parameters (`case 'limit': return p.n != null ? ...`). `previewGrid.ts`
(`consumedColumns`) only matters when your node turns columns into other columns, as
`pivot` and `unpivot` do: the columns it lists are not shown as "removed".

### Step 9. The words, in five languages

For a transformation the words are in the **hand-written** catalogues
`frontend/i18n/locales/en.ts`, `it.ts`, `de.ts`, `es.ts` and `fr.ts`, under `ops` (the
node's name) and `params` (the form's labels). English is the base; the four others must
have exactly the same keys:

```ts
  ops: {
    reorder: 'Reorder columns',            // en.ts
    reorder: 'Riordina colonne',           // it.ts
    reorder: 'Spalten neu ordnen',         // de.ts
  },
  params: {
    reorder_columns: 'Drag to reorder',    // en.ts
    reorder_columns: 'Trascina per riordinare',   // it.ts
  },
```

(Each line goes in its own file, not together.) The files `*.gen.ts` beside them hold the
words of whole pages (the report designer, the statistics group); their name
is a leftover of an i18n sweep that no longer exists: there is no generator in the
repository and the files are edited by hand, as their header says. A transformation does not need them.

Test: `frontend/tests/i18n.test.mjs` fails when a language lacks a key English has or has
one more, when a message does not compile, and when the code names a key that does not
exist.

### Step 10. The interface tests and the type check

`frontend/tests/flow-shortcuts.test.mjs` has the list of the engine's operations, written
by hand:

```js
const TRANSFORMS = ['cast', 'compute', 'drop', 'drop_nulls', 'fill_null', 'filter', 'foreach', 'group_by', 'join', 'limit',
  'pivot', 'rename', 'reorder', 'sample', 'select', 'sort', 'sql', 'union', 'unique', 'unpivot']
```

Add your name, in alphabetical order. The same test reads `transformOps` of
`flow_shortcuts.go` and fails when the two differ, which is how you find out you forgot
step 7. If your node has a summary or consumes columns, add a case to
`frontend/tests/preview-grid.test.mjs` (see its `consumedColumns('unpivot', ...)` line).

```bash
cd frontend
npm ci
npx nuxi prepare && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
npm test
```

`npm ci` runs `nuxt prepare` itself (the `postinstall`), so `npx nuxi prepare` is only
needed when you did not install. `npm test` runs every `tests/*.test.mjs` with Node's test
runner. vue-tsc does not check keys of translations (a key is a string to it): that is
what `npm test` is for.

### Step 11. The AI assistants and MCP

The flow assistant (the Python AI service: with the database migrations, the one thing the
otherwise frozen `gateway/` still takes changes for) lists the operations it may place and checks their parameters, in
`gateway/app/services/ai_flow_agent.py`:

1. add the name to `BASE_OPERATIONS`;
2. add a branch to `clean_params` that returns the parameters in the shape the engine and
   the form read, or raises `ModelRetry` with what to fix:

   ```python
       if operation == "limit":
           n = p.get("n")
           if not isinstance(n, int) or isinstance(n, bool) or n < 1:
               raise ModelRetry("limit needs n, a whole number of rows of at least 1.")
           return {"n": n}
   ```
3. add a line to `_REFERENCE`, the text the model reads
   (`- limit {n}. unique {subset?: [..]}. ...`).

The MCP server and the assistant's prompts are not generated from the Python at run time:
the Go gateway embeds two snapshots, `gateway-go/internal/aiprompts/prompts.json` and
`gateway-go/internal/mcp/tools.json` (the description of the tool `flow_add_step` lists
`ai_flow_agent.OPERATIONS`). After a change to `ai_flow_agent.py`, write them again, from
the repository's root, in the gateway's image (the commands are the ones in the scripts'
own docstrings):

```bash
docker build --build-arg WITH_DEV=true -t tabularia-gateway:latest gateway
docker run --rm --user "$(id -u)" -v "$PWD/gateway/app:/app/app:ro" -v "$PWD/gateway-go/internal/aiprompts:/out" \
    -w /app -e HOME=/tmp -e SECURITY__FERNET_KEY=$(openssl rand -base64 32 | tr '+/' '-_') tabularia-gateway:latest \
    python /out/gen_prompts.py
docker run --rm --user "$(id -u)" -v "$PWD/gateway/app:/app/app:ro" -v "$PWD/gateway-go/internal/mcp:/out" -w /app \
    -e SECURITY__FERNET_KEY=$(openssl rand -base64 32 | tr '+/' '-_') tabularia-gateway:latest \
    python /out/gen_tools.py
git diff --stat gateway-go/internal/aiprompts gateway-go/internal/mcp
```

Commit the two files with the change. Tests: `gateway/tests/test_ai_flow_agent.py`
(`test_wrong_params_tell_the_model_what_to_fix` is a parametrised list of bad parameters
and the message each earns). Run the gateway's suite as CI does:

```bash
docker build --build-arg WITH_DEV=true -t tabularia-gateway:ci gateway
.github/scripts/ci_tests.sh gateway tabularia-gateway:ci
```

Lineage needs nothing: it works by dataset, and a step neither reads nor writes one.

### Step 12. Documentation and changelog

- `docs/capabilities/flows.md`: a row in the table of the right group (Columns, Rows,
  Combining and reshaping), in the voice of its neighbours:
  `| **Reorder columns** | Puts the listed columns first, in the order you drag them; the others follow. |`
- `docs/engines/engine-differences.md`: the counts in the table "Operation coverage"
  (`all 41`, `40 — no foreach`) and the sentence under it ("the 19 transformations and the
  loop, plus the 21 statistical tests"); a row in "What differs" if the engines differ in
  some way a person can see.
- The same counts are written in `README.md` and `docs/capabilities/README.md`
  ("19 transformations and a loop"); change them together:
  `git grep -n "19 transformations"`.
- `CHANGELOG.md`: a bullet under `## Unreleased`, in the shape of its neighbours: bold
  lead sentence, then what changes for the person.
- `docs/dbt-export.md` if the node is refused (step 6).

Write the documentation in English, with no names of other products.

## Path 2: a statistical test

Everything starts from one table, the catalogue, in
`backend/app/engine/stats/catalog.py`. The four engine registries, the palette, the form,
the allow-list of the menu, the dbt refusal and the assistants are all made from it, so a
new test has far fewer places to touch than a transformation. The design is in
`docs/specs/statistics.md` (the section "The path of a new node" lists every file and its
model; the spec is kept in the repository and not published on the site) and the user's page
is [Statistics](../capabilities/statistics.md).

The model is `pearson` (`stat_pearson`): two numeric columns, no groups, a result table with
a report. It came in commit `321b3cb2`, on the foundations laid by `a0c50f06` (the first
test on the five engines), `a51c6490` (the catalogue, its copies, the palette and the form),
`8798900b` (the assistants) and `0cede891` (the documentation). The newest group of tests
is `91c91444` (proportions and categories): read it to see a test with new kinds of
parameter. List the history with `git log --oneline 3ac34816..HEAD`.

### The whole list, at a glance

| # | Where | File |
|---|---|---|
| 1 | The catalogue | `backend/app/engine/stats/catalog.py` |
| 2 | The counting passes | `stats/aggregates.py` and `stats/polars_source.py` (only for a new pass) |
| 3 | The final computation | `stats/compute.py` (`dist.py`, `exact.py` for distributions) |
| 4 | The report | `stats/layouts.py` and `stats/report_texts.json` |
| 5 | The snapshot and its three copies | `backend/app/engine/stats/catalog.json`, `.github/scripts/stat_catalog.py` |
| 6 | The words | `frontend/i18n/locales/{en,it,de,es,fr}.gen.ts` |
| 7 | Tests | `backend/tests/test_stats_*.py`, `test_bigquery_ops.py`, `frontend/tests/stat-*.test.mjs` |
| 8 | The assistants and MCP | the copy `gateway/app/services/stat_catalog.json`, then `prompts.json` and `tools.json` |
| 9 | Documentation and changelog | `docs/capabilities/statistics.md`, `CHANGELOG.md` and the counts |

What you do **not** touch: the four registries (`operations.py`, `duckdb_ops.py`,
`chdb_ops.py`, `bigquery_ops.py` register one operation per catalogue entry in a loop over
`stat_operations()`), the Go gateway (it embeds the copy of the catalogue: its menu, its
dbt refusal `op_not_exportable` and its route `POST /tasks/stat-report` know the test by
name), the dbt export (the last `raise` refuses it), the palette, the icon (a test takes
its family's) and the form.

### Step 1. The catalogue: `backend/app/engine/stats/catalog.py`

An entry of `TESTS` is plain data, which the interface reads: its family, the keys it
needs and takes (each with a *kind*), and the defaults of what it takes. The keys are shown
in this order by the form. `pearson`, whole:

```python
    "pearson": {
        "family": "correlation",
        "needs": {"x": "number_column", "y": "number_column"},
        "takes": {"alternative": "alternative", "alpha": "alpha", "detail": "detail"},
        "defaults": {"alternative": "two_sided", "alpha": 0.05, "detail": "results"},
    },
```

`detail` is the key every test takes: `results` is the table, `report` also writes the rows
the report's charts draw. The kinds are in `KINDS` (what the form draws: `number_column` is
a column picker of numeric columns) and `_READERS` (what the node accepts and the sentence
it refuses with). Reuse a kind when you can. A new kind needs both, plus a control the form
can draw: the controls are `CONTROLS` (`column`, `levels`, `boolean`, `number`, `choice`,
`level`, `proportions`); a new control means work in `frontend/components/ui/StatTestForm.vue`
and `frontend/composables/statTests.ts`, which the test below makes you do. When two
options exclude each other, add a `forbids` rule; when a key matters only with some values
of another, `applies` (see `anova` and `chi2_independence`).

Test: `backend/tests/test_stats_catalog.py` checks that the snapshot is what the
catalogue says, that every kind has a reader and a control the form draws, and
`test_the_tests_of_the_first_lots_in_their_families` lists the tests of each family **by
name**: add yours there.

### Step 2. The counting passes (only if the test needs a new one)

A test sees the engine through two classes with the same methods: `SqlStatSource` in
`aggregates.py` (for DuckDB, chDB, ClickHouse and BigQuery, which differ only by dialect)
and `PolarsStatSource` in `polars_source.py`. The existing methods are `moments`,
`pair_moments`, `abs_dev_moments`, `quartiles`, `successes`, `crosstab`, `histogram`. A
test that fits them (as `pearson` fits `pair_moments`) needs no change here. A new pass is a
method on both classes that returns the same named tuple, **centred where it sums**: the
numbers must agree across engines within a relative 1e-9, also on a column near 1e9.

The engine never sends the rows to Python: the passes aggregate in the engine and the test
computes on the aggregates (the CHANGELOG says "the last step in one module on the
aggregates").

### Step 3. The final computation: `stats/compute.py`

One function `(source, p, out, engine_name)` per test, entered in `_TESTS`, that reads the
aggregates, computes the statistic and the p-value with the functions of `dist.py` and
`exact.py`, and writes the output table with `out.number(...)` and `out.text(...)` (a long
table of ten columns: `section`, `level`, `level_2`, `stat`, `value`, `text`, `x0`, `x1`,
`ord`, `key`; see `stats/output.py`). The head of `_pearson`:

```python
def _pearson(source, p: dict[str, Any], out: Rows, engine_name: str) -> None:
    """Pearson's correlation, by its t. H0: correlation = 0."""
    test = "pearson"
    x, y, alpha, alternative = p["x"], p["y"], p["alpha"], p["alternative"]
    pm = _two_columns(source, test, p, 3)
    for column, var in ((x, pm.var_x), (y, pm.var_y)):
        if var == 0.0:
            raise refusal(test, f"all the values of «{column}» are equal on the rows used (the variance is 0): there is no correlation")
```

and its registration:

```python
_TESTS: dict[str, Callable[..., None]] = {
    ...
    "pearson": _pearson,
    ...
}
_WITH_REPORT_ROWS = frozenset({"t_one_sample", ..., "pearson", ...})
```

A test not in `_WITH_REPORT_ROWS` refuses `detail = report` with a sentence. Refuse bad
data with `refusal(test, "...")`, whose sentence says what to do. Never print a p-value as
zero, and write a number that is not finite as `NULL` (`output.py` does it).

### Step 4. The report: `stats/layouts.py` and `stats/report_texts.json`

The Report tab draws pages from a layout: a function that builds a definition of the
report engine (`backend/app/reports`) from the test's name, its parameters and the
language. Most tests share one by shape: `LAYOUTS` maps `"pearson": _correlation`,
`"t_two_sample": _two_groups`. If a shape fits, add your test to the map; otherwise write
a function in the style of `_correlation` with `_page(...)`. The words of the report are
in `report_texts.json`, in five languages under `tests.<id>` (`name`, `stats`, `kpi`,
`symbols`, `tables`), and must have identical keys in every language and use only characters
the PDF font has (tests for both).

Test: `backend/tests/test_stats_layouts.py`:

```python
def test_every_test_of_the_catalogue_has_a_layout_and_its_reference():
    assert set(layouts.LAYOUTS) == set(TESTS) == set(REFERENCE)
```

Add your test to `REFERENCE` (the parameters it is asked on the reference data) and to
`VARIANTS` (the variants of its options, which together write every row it may write) at the
top of that file.

### Step 5. The snapshot and the three copies

The interface, the Go gateway and the AI service each read a copy of the catalogue,
because each is built from its own folder. After any change to `catalog.py`:

```bash
# 1. the snapshot, written by the engine's own code (the backend image, from the repository's root)
docker run --rm --user "$(id -u)" -v "$PWD/backend:/app" -w /app tabularia-backend:ci \
    python -m app.engine.stats.catalog
# 2. its three copies (standard library only)
python3 .github/scripts/stat_catalog.py
# 3. the check CI runs
python3 .github/scripts/stat_catalog.py --check
```

The copies are `frontend/assets/stat-catalog.json`,
`gateway-go/internal/statcatalog/catalog.json` and `gateway/app/services/stat_catalog.json`.
`--check` fails when a copy differs from the snapshot by a byte, and when a test's id is
not named in backticks (`` `pearson` `` or `` `stat_pearson` ``) in
`docs/capabilities/statistics.md`. The engine's suite fails when the snapshot is not what
`catalog.py` says, and Go and the interface have a test each on their copy.

### Step 6. The words: `frontend/i18n/locales/*.gen.ts`

The test's words go in the five `*.gen.ts` files, namespace `stat`. The test
`every test, family, field, choice and reason has its words in the five languages` in
`frontend/tests/stat-tests.test.mjs` asks for them **by name** (`statWordKeys()` in
`frontend/composables/statTests.ts` computes the list):

| Key | What |
|---|---|
| `stat.tests.<id>.name`, `stat.tests.<id>.hint` | the name in the palette and the one-line question it answers |
| `stat.tests.<id>.params.<key>`, `.hints.<key>` | optional: a label or a hint of its own for a key |
| `stat.params.<key>` | the label of a key (shared by the tests that have it) |
| `stat.choices.<kind>.<choice>` | each choice of a `choice` kind |
| `stat.reasons.<reason>` | the sentence of a `forbids` rule |
| `stat.families.<family>` | a family (only when you add one) |

The entry of `pearson` in `en.gen.ts`:

```ts
      "pearson": {
        "name": "Pearson's correlation",
        "hint": "Do two columns of the same rows move together? Pearson's r, its test and its confidence interval."
      },
```

Do the same in `it`, `de`, `es` and `fr`. The `*.gen.ts` files are edited by hand; there is no generator to run.

### Step 7. Tests that tell you the numbers are right

All under `backend/tests/`, all parametrised on the engines the way the transformations'
tests are:

- `test_stats_reference.py`: **reference values** computed outside Tabularia (the file's
  docstring says R 4.6.1, on small samples written in the file). Every engine must give them
  within a relative `1e-9`, counts and texts exactly. Compute the expected numbers with R
  or SciPy and write them into the test; never copy them from your own output.
- `test_stats_parity.py`: `CASES` is a dictionary of flows (`stat("pearson", x="x", y="x2")`);
  every other engine must give the table Polars gives, on 5,000 rows with ties, empty
  values, `NaN`, `-0.0` and a column near 1e9. Add a case for each variant of each option,
  and one with `detail="report"`.
- `test_stats_params.py`: the refusals (every sentence a bad parameter earns).
- `test_bigquery_ops.py`: BigQuery's SQL is parsed, not run (it cannot run in CI).
- `test_stats_layouts.py`: step 4.
- `test_dbt_export.py`: nothing to write; `test_a_statistics_step_is_not_exported` goes
  through every test of the catalogue.

The interface: `frontend/tests/stat-tests.test.mjs` (the fields of each test, e.g.
`assert.deepEqual(fields('stat_pearson'), ['x:column', 'y:column', 'alternative:choice', 'alpha:number'])`)
and `stat-form.test.mjs` (the form rendered). `frontend/tests/flow-shortcuts.test.mjs`
reads the catalogue, so it needs nothing for a new test.

### Step 8. The assistants and MCP

`gateway/app/services/ai_flow_agent.py` builds its operations, its reference text and its
checks from the copy of the catalogue (`STAT_TESTS`, `stat_reference()`,
`clean_stat_params`): nothing to write. What changes is the text the Go gateway embeds, so
write `prompts.json` and `tools.json` again with the two commands of Path 1 step 11 and
commit them. `gateway/tests/test_ai_flow_agent.py` has a test that goes through the whole
catalogue (`test_every_test_of_the_catalogue_is_an_operation_with_a_line_of_reference_and_its_defaults_pass`).

### Step 9. Documentation and changelog

- `docs/capabilities/statistics.md`: a paragraph in its family's section,
  `**`stat_pearson`**: ...` with an example on the sample data. The name in backticks is
  what `stat_catalog.py --check` looks for.
- The number of tests: `git grep -n "21 \(hypothesis \)\?tests\|21 statistical\|Twenty-one"`
  finds it in `README.md`, `docs/capabilities/README.md`, `docs/capabilities/flows.md`,
  `docs/capabilities/statistics.md`, `docs/engines/engine-differences.md` and `CHANGELOG.md`;
  the operation counts in the table "Operation coverage" move with it (`all 41`, `40`).
- `CHANGELOG.md`, under `## Unreleased`.

## Path 3: a statistical model

A model is declared once, in `backend/app/engine/models/catalog.py`, and everything else reads
that: the palette, the form, the Go gateway's check of the parameters, the Jobs' ConfigMap, the
assistants, MLflow and Predict. What the catalogue cannot say is code: the runner, in the
group's image, and the layout of the report. The design, with the rules every model follows
(Kubernetes only, the ten columns, the report drawn from the results, MLflow logged by the
gateway, Predict from the card alone), is in `docs/specs/statistical-models.md` (kept in the
repository, not published); the user's page is [Statistical models](../capabilities/models.md).
Read, as models to copy, the **parametric survival** node (an R model in an existing group, with
variants, an automatic choice and Predict) and the **customer lifetime** group (a new group, with
a system library); the history is `git log --oneline -- backend/app/engine/models/catalog.py`.

### The whole list, at a glance

| # | Where | File |
|---|---|---|
| 1 | The catalogue: group, model, kinds, variants, `PREDICT`, `LIMITS` | `backend/app/engine/models/catalog.py` |
| 2 | The snapshot and its three copies | `backend/app/engine/models/catalog.json`, `.github/scripts/model_catalog.py` |
| 3 | The group: recipe, runner, reference values | `gateway-go/internal/modelgroups/groups/<group>/` (`recipe.json`, `runner/`, `reference/`) |
| 4 | The card's schema | `gateway-go/internal/modelgroups/card.schema.json` |
| 5 | The report | `backend/app/engine/models/layouts/<model>.py`, `report_texts/<model>.json` |
| 6 | The editor and its words | `frontend/composables/modelCatalog.ts`, `modelIcons.ts`, `OpSidebar.vue`, `frontend/i18n/locales/*.gen.ts` (namespace `models`) |
| 7 | Tests | `backend/tests/test_models_{catalog,results,layouts}.py`, `frontend/tests/model-*.test.mjs`, `gateway-go/internal/modelgroups/`, the group's own self-test |
| 8 | The assistants and MCP | `gateway/app/services/ai_flow_agent.py` (a paragraph), then `prompts.json` and `tools.json` |
| 9 | Documentation and changelog | `docs/capabilities/models.md`, `CHANGELOG.md` and the counts |

### Step 1. The catalogue

An entry of `MODELS` says which keys the node `needs` and `takes` (their kinds, from `kinds`:
plain data drawn by the form's existing controls), their `defaults`, when a key `applies` or is
`required` or `forbids` another, which columns must be `disjoint`, and its **variants**: what the
runner **may** write for each combination of options. `results.py` refuses a section or a key
that the variant does not declare, in the engine and in the Go gateway, so the catalogue is
also the list of what a run can contain. A check that does not apply to the options chosen is
simply not written; it is never written as «not computed». `PREDICT` says which options Predict
takes for the model and which columns it adds. A new group is an entry of `GROUPS`, with its
language. Then write the snapshot, in the engine's image, and its copies, and check them:

```bash
docker run --rm --user "$(id -u)" -v "$PWD/backend:/app" -w /app tabularia-backend:ci python -m app.engine.models.catalog
python3 .github/scripts/model_catalog.py
python3 .github/scripts/model_catalog.py --check
```

`--check` also fails when `docs/capabilities/models.md` does not name the model's id in
backticks (step 6). `backend/tests/fixtures/models/params_cases.json` holds the cases of parameters that the
engine, the Go gateway and the assistants all run: add the defaults of your model, one with its
options and its refusals.

### Step 2. The group, the runner and the reference values

A group is a folder, `gateway-go/internal/modelgroups/groups/<group>/`:

- **`recipe.json`**: `language`, `base` (R 4.6 or Python 3.14), the **packages pinned by version, in
  the order they are installed** (each after its dependencies), the default `resources`,
  `max_rows` and `max_seconds`. A new package or version changes the recipe's hash, and
  every installation builds the group again by itself; say so in the changelog. A package that
  needs a **system library** is listed in `system_libraries` (the Debian packages, for example
  `libgsl-dev`): the group's Dockerfile installs them with `apt-get` and records the versions that
  were installed, which the group's card in Administration shows. Only a group with the field has the step,
  and the Dockerfiles of the others do not change by a byte (a test holds it).
- **`runner/`**: `run.R` (or `run.py`) routes each model to its fit and its predict function, in a file
  of its own (`runner/R/<model>.R`), over the common library in `common/R`. The rules are the common
  library's: a refusal is `tab_refuse(code, sentence, params)` with a sentence that says what to do;
  every row the runner writes is checked against the catalogue (`tab_check_declared`); the column
  names are renamed to positional ones before any formula is built, so that a name that looks like
  code is a label; a run that is slow stops by the node's `time_budget_seconds`, never by the
  Job's deadline. **The card** is what Predict reads, and holds no row of the input.
- **`reference/`**: small data (`data/*.csv`), `expected.json` and the scripts that write it
  (`make_reference*.R`). The expected values are computed **outside Tabularia's code**, with the library called
  directly or a second construction, never with the runner itself; the tolerance of each case says what was measured.
  The group's **self-test** fits them in the image before a version can be used. A second tool
  (a *witness*, for example `make_witness.py` with lifelines) is run by hand, in a scratch library, not by CI.

`cmd/modelgroup` builds and checks a group on a laptop as the cluster does
(`dockerfile <group>`, `code <group> <dir>`, `task`, `groups`): its usage is at the top of
`gateway-go/cmd/modelgroup/main.go`.

**The ConfigMap limit.** A Job receives the group's code in a ConfigMap, which Kubernetes limits to 1 MiB.
`TestEveryGroupOfTheCatalogueHasARecipeAndARunner` measures, for the Job and for the self-test (the code
with the reference cases), the ConfigMap that `ConfigMap()` makes, keys and data together, against 900 KiB. When a
group's runner or its `expected.json` grow, write `expected.json` compact and look at comment-only lines: the test
fails before the Job does. The catalogue copy the Job gets is its compact form, without indentation.

### Step 3. How a check speaks, and the notes

**No constant of ours makes a verdict.** A check's verdict (`ok`, `failed`, `not_computed`, with its `h0`,
`advice`, `advice_id` and `threshold`) comes only from a test with a **p-value read against the node's `alpha`**, with `threshold`
carrying alpha; or from a **fact** (the Hessian is positive definite, a level has no event), the only case in which
`doubtful` is still written. Any other quantity is a **measure**: a number with a `description` and no verdict
and no advice, which the report lists by its id in `MEASURES`. A rule of thumb with a threshold belongs to the models
that already have them (the boosting group's diagnostics); a new one needs a reason, and the simulation that shows it does not
cry wolf. See [How a check speaks](../capabilities/models.md#how-a-check-speaks-verdicts-facts-and-measures).

The runner's **notes** (`note_<id>` rows of the `model` section) that carry numbers are written as an id and its
parameters, `@auto_close distribution=weibull other=gengamma margin=2 delta_aic=0.30 weight=0.47`, which the layout turns into a sentence in the
report's language (`note_texts.<id>` in the words file). A value that is a name of the data may hold a space, a comma or a
«%»: write them %20, %2C and %25 (`surv_note` in `kaplan_meier.R`), and the layout decodes them; a list is joined with «,». A
unit of time is said in the page's words, `{unit}` its plural, `{unit:one}` its singular, `{unit:dative}` the plural after a
preposition (German's dative). A note sent as plain English is shown as it is, in English, in every language: send an id.
A heading of the report stays with the table right under it (the report engine keeps a band of words right above a table on
the table's page): a layout needs no room kept under a heading for that.

### Step 4. The report, the editor and the words

`layouts/<model>.py` builds the pages from the results rows with `common.page`: the blocks that models share (the
checks and their advice, the measures, the coefficient tables, the charts' grid) are in `common.py`. Its words are in
`report_texts/<model>.json` in the five languages (the catalogue's test fails when one is missing), and the tests of
`test_models_layouts.py` render every fixture (`backend/tests/fixtures/models/<model>-<variant>.json`, written
from the runner's own output with `tests.models_fixtures --from-results`) in five languages. The editor draws the node from
the catalogue: a palette entry, an icon in `modelIcons.ts`, the form with the existing controls (a new kind of key needs one of
them, not a component), and the words `models.<id>` and `models.params.<key>` in `*.gen.ts`; `model-catalog.test.mjs` asks for the words of
every model of the catalogue.

### Step 5. The assistants and MCP

`ai_flow_agent.py` reads its copy of the catalogue (`MODELS`, `models_reference()`, `clean_model_operation`): the node, its keys, its
variants and the refusals of the form come with the snapshot. What you write is a paragraph of `_MODELS_INTRO` on **when to choose it** and what
to set (and, for a model that takes a prepared table, how to prepare it with the steps that exist), and tests in `test_ai_flow_agent.py`: the line
of reference, the defaults pass, the wrong parameters are sent back with what to fix, a turn that places the node on a draft. Then write
`prompts.json` and `tools.json` again with the commands of Path 1 step 11 and commit them; `go test ./internal/aiprompts ./internal/mcp` in `gateway-go/`.

### Step 6. Documentation and changelog

- `docs/capabilities/models.md`: a section with the line `Model id in the catalogue: `<id>`.`, the tables of
  its options (as the form says them), what it gives, the checks (verdicts, facts and measures), Predict, MLflow, what it refuses and an
  *Example* on the sample data; an example that cites a number must come from a real run, otherwise it describes the nodes and gives no number.
- The counts («Ten models are in the first release, in five groups») in `models.md`, `flows.md`, `docs/capabilities/README.md` and the README, `CHANGELOG.md` under `## Unreleased`
  and, when you add a package or a system library, `docs/deploy/kubernetes.md`.

## What CI runs

Six workflows in `.github/workflows/`. A pull request is green when these pass:

| Workflow | Job | What it runs | When |
|---|---|---|---|
| `ci.yml` | `engine tests` | builds the backend image with `WITH_DEV=true` and `WITH_CHDB=true`, then `.github/scripts/ci_tests.sh engine tabularia-backend:ci`: every test of `backend/tests` beside real ClickHouse 24.8, PostgreSQL, MinIO and Valkey; a skip not on the allowed list fails | every push and pull request |
| `ci.yml` | `gateway tests` | builds the gateway image, runs `ci_tests.sh gateway tabularia-gateway:ci`, then `gateway-go/scripts/test.sh`: `gofmt -l .`, `go vet ./...` and `go test ./...` on the gateway and on `dataapi/`, with a PostgreSQL whose schema is made by the migrations | every push and pull request |
| `ci.yml` | `interface type check` | in `frontend/`: `npm ci`, `vue-tsc --noEmit` (pinned: typescript 5.6.3, vue-tsc 2.2.10) and `npm test`; then the unit tests of the sample data's seed | every push and pull request |
| `stat-catalog.yml` | `check` | `python3 .github/scripts/test_stat_catalog.py -v`: the script's tests and the check that the three copies are the snapshot and every test is named in the statistics page | when the catalogue, its copies, the page or the script change |
| `model-catalog.yml` | `check` | `python3 .github/scripts/model_catalog.py --check` and its tests: the three copies of the models' catalogue are the snapshot, and every model's id is named in `docs/capabilities/models.md` | when the catalogue, its copies or the page change |
| `config-reference.yml` | | `docs/configuration.md` against the settings of the code (`python3 .github/scripts/config_reference.py` writes it) | when code that reads a setting changes |
| `website.yml` | `Generate and check the site` | the site's unit tests, `npm run generate` and `npm run check-links` (every internal link and anchor, and every link to a file of the repository) | when `docs/`, `website/` or `README.md` change |
| `release-images.yml` | | calls `ci.yml` on the tag and publishes nothing unless it is green | a version tag |

Not in CI, and said so in `ci.yml`: live BigQuery, Oracle and SQL Server, and the
end-to-end oracles of `e2e/` (they need a whole stack). If you can run them for your
change, say so in the pull request.

## The checklist

Path 1, a transformation:

- [ ] The function is registered on all four registries: `operations.py`, `duckdb_ops.py`,
      `chdb_ops.py`, `bigquery_ops.py`; names quoted with `_qi`, literals with `_lit`
- [ ] A hand-computed case in `test_data_correctness.py` (all engines), the BigQuery SQL in
      `test_bigquery_ops.py`, a unit test in `test_operations.py`
- [ ] The dbt export: a branch (and its tests), or nothing and a test of `op_not_exportable`
- [ ] The name in `transformOps` of `gateway-go/internal/api/flow_shortcuts.go`, and in
      `TRANSFORMS` of `frontend/tests/flow-shortcuts.test.mjs`
- [ ] `OP_SPECS` (and `defaultParams`), `OP_META` and `opSummary` in `frontend/composables/`
- [ ] `ops.<name>` and `params.<key>` in the five `frontend/i18n/locales/*.ts`
- [ ] `ai_flow_agent.py` (`BASE_OPERATIONS`, `clean_params`, `_REFERENCE`) and `prompts.json`
      and `tools.json` written again and committed
- [ ] `flows.md`, `engine-differences.md` (the counts), the READMEs' counts, `CHANGELOG.md`

Path 2, a statistical test:

- [ ] The entry of `TESTS` (and its kinds), the function in `compute.py` and `_TESTS`,
      `_WITH_REPORT_ROWS`, `LAYOUTS`, `report_texts.json` in five languages
- [ ] `python -m app.engine.stats.catalog`, `python3 .github/scripts/stat_catalog.py`, and
      `--check` green
- [ ] The words `stat.*` in the five `*.gen.ts`
- [ ] `REFERENCE` and `VARIANTS` of `test_stats_layouts.py`, the family list of
      `test_stats_catalog.py`, references from outside in `test_stats_reference.py`, a case
      in `test_stats_parity.py`, the refusals in `test_stats_params.py`, BigQuery's SQL
- [ ] The test named in backticks in `docs/capabilities/statistics.md`, the counts,
      `CHANGELOG.md`, `prompts.json` and `tools.json` written again

Path 3, a statistical model:

- [ ] The entry of `MODELS` (and its kinds, variants, `PREDICT` and `LIMITS`), the snapshot and its three copies, `--check` green
- [ ] `params_cases.json`: defaults, options and refusals
- [ ] The group's `recipe.json` (packages pinned, `system_libraries` if needed), the runner, the card's schema, reference values computed outside the runner
- [ ] Verdicts only from a p-value against `alpha` or from a fact; the rest a measure; notes as `@id name=value`
- [ ] The group's ConfigMap under 900 KiB (`go test ./internal/modelgroups/`)
- [ ] The layout, its words in five languages, its fixtures from the runner's output
- [ ] The palette entry, the icon, the words `models.*` in the five `*.gen.ts`
- [ ] `_MODELS_INTRO` and its tests, `prompts.json` and `tools.json` written again
- [ ] The model's id in backticks in `models.md` with its section and an example, the counts, `CHANGELOG.md`

For all: no name of another product in any text; code, comments, messages and
documentation in English; one change per pull request, with the reason in its description.

## The commands

From the repository's root unless said otherwise. Together they are what CI runs.

```bash
# the interface: types and translations
cd frontend && npx nuxi prepare && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
cd frontend && npm test

# the Go gateway (CI also runs gofmt, go vet and the database tests: gateway-go/scripts/test.sh)
cd gateway-go && go test ./internal/...
cd gateway-go && gofmt -l .

# the engine and the gateway, in their development images
docker build --build-arg WITH_DEV=true --build-arg WITH_CHDB=true -t tabularia-backend:ci backend
.github/scripts/ci_tests.sh engine tabularia-backend:ci
docker build --build-arg WITH_DEV=true -t tabularia-gateway:ci gateway
.github/scripts/ci_tests.sh gateway tabularia-gateway:ci

# the catalogue of the statistical tests (Path 2)
python3 .github/scripts/stat_catalog.py --check
python3 .github/scripts/test_stat_catalog.py -v

# the catalogue of the statistical models (Path 3)
python3 .github/scripts/model_catalog.py --check
python3 .github/scripts/test_model_catalog.py -v
cd gateway-go && go test ./internal/modelgroups/ ./internal/modelcatalog/

# the configuration reference, when you touched a setting
python3 .github/scripts/config_reference.py
```

The site's own checks (`npm run generate` and `npm run check-links` in `website/`) run in
CI when a page of `docs/` changes: a link in this guide to a page that does not exist
fails them. Link only to pages of `docs/` that are published; the folders `docs/specs/` and
`docs/audit/` are not, so name their files in backticks instead.
