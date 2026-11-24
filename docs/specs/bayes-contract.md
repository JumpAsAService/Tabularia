# Spec: the runner protocol of the group `bayes` (`bayes-contract`)

Status: **written and approved by the owner on 2026-11-23.** It specifies the module `bayes-contract`
of `docs/specs/bayesian-editor-map.md` (**the map**), step 2 of its order of construction, beside `stan-codegen`
(`bayes-stan-codegen.md`) and `prior-preview` (`bayes-prior-preview.md`), after the approved
`bayes-distribution-catalog.md`, `bayes-expression-language.md`, `bayes-model-graph.md` and `bayes-image.md`.
Nothing of it is built. Decisions marked **(owner)** are the owner's (the map's «Interview answers», numbered as
there: **the answers**); the rest are this spec's, to confirm; those marked «spec decision, 2026-11-23, owner may
revisit» are small choices the owner may change without reopening the module. The owner answered this spec's
questions on 2026-11-23 («Decisions of 2026-11-23»); no question is open.

Words: **verified** = read in this repository's code on 2026-11-23; **measured** = run on 2026-11-23 outside the
repository on the development machine (a few Python scripts with the image's pins, `pyarrow==26.0.1` and
`numpy==2.5.5`, through `uv run --no-project`; the files deleted afterwards); **checked online** = read on the
project's own page on 2026-11-23; **assumed** = from documentation, not run here, checked by the lot named beside
it.

It is to this editor what «piece 0, the common contract» was to the models spec (`docs/specs/statistical-models.md`,
**the models spec**): every other module reads it — `stan-codegen` writes the manifests it defines, `stan-compile`
starts its compile mode, `bayes-runner` implements its sample mode, `bayes-diagnostics` and `predictive-checks`
fill its rows, `bayes-report` draws them, `flow-node` writes its `task.json`, `bayes-mlflow` reads its card.

## Objective

One protocol between the Go gateway and the Python runner of the group `bayes` (answer 15, owner), in four modes —
`compile`, `prior_predictive`, `sample`, `selftest` — with:

- **`task.json`**: what the gateway asks, as data, never as code;
- **the files**: the program, the binary and its sha256, the data file CmdStan reads, the CSV files CmdStan writes;
- **the draws parquet**: one fixed schema, **always long**, **appended as the chains sample**;
- **the results**: the rows of the ten-column table, their sections, their keys, their caps;
- **the outputs**: the summary by default, the draws on request, the replicated data on request (answer 14);
- **the refusals**: codes, sentences, exit codes, and the rule that a refusal leaves no partial result;
- **the card**: what a run of a model is, as data (`model.json`), for MLflow (answer 14: without the draws).

### The owner's rule, written here first

The owner, 2026-11-22 and again 2026-11-23: «Attenzione a non caricare tutto in memoria, l'output deve essere un
parquet in append in base ai campionamenti.» In this protocol it means, without exception:

1. **The draws are appended to the parquet while the chains sample.** CmdStan writes one CSV file per chain into
   `/tmp`; the runner **follows each file while it grows**, reads the new complete lines, converts a **block** of
   draws of one chain, appends it to the draws parquet as **one row group**, and frees it.
2. **No step waits for the end of sampling to read a chain**, and **no function loads a whole CSV file or a whole
   chain**: no `read_csv` of a file, no `cmdstanpy`, no `read_cmdstan_csv` or the like (the map), no `pq.write_table`
   of a whole table (`tab_main.write_output` and `tab_results.write` do that, verified, and are not used for draws).
   The part of a CSV already converted is never read again (the runner keeps an offset per file).
3. **The schema of the parquet allows it**: row groups in a fixed order with the **chains interlaced** block by
   block, `chain` and `draw` in every row, the file's own metadata written **at its closing**.
4. **Summaries, R-hat and ESS** come from **accumulators** fed by each block (the sampler's facts) or from a **second
   pass by blocks of columns** read back from the parquet (the per-quantity measures, which need every draw of one
   quantity: 32 KB at 4 chains × 1,000 draws). Never from a whole table.
5. **The same holds for the replicated data** (y_rep): written in blocks, summarized with accumulators, never all in
   memory (map, `predictive-checks`).
6. **A test measures the memory peak** of the runner and shows it **flat when the draws are multiplied by ten**
   («Testing strategy», «The memory test»).

The data the model is fitted to are the one exception the map allows: Stan holds them in its own process, bounded by
`max_rows`; the runner itself still reads the input in batches and writes the data file in a stream («The data file»).

## Decisions

| Question | Decision | By |
|---|---|---|
| Runner language | **Python** (`numpy`, `pyarrow` only, as `bayes-image` pins them) | Owner (answer 15) |
| Modes | `compile`, `prior_predictive`, `sample`, `selftest` | The map |
| Sampler | **NUTS only** (`method=sample algorithm=hmc engine=nuts`), and `fixed_param` for the prior predictive | Owner (answer 9, answer 13) |
| Outputs | **`summary` by default**; `draws` on request (all the person's quantities); `replicated` (y_rep rows) on request and thinned; `results` (the ten columns) as for every model node; **MLflow never receives draws** | Owner (answer 14) |
| Draws parquet: how it is written | **Appended while sampling**: one row group per block of draws of one chain, chains interlaced in a **fixed order** (block 1 of chains 1…C, block 2 of chains 1…C, …), whatever the chains' speed | Owner (rule of 2026-11-22/23), the order: spec |
| Draws parquet: its layout | **Always long** (one row a quantity of a draw): no wide layout, no limit, no choice («The layout») | Owner (2026-11-23, replacing answer 14's «wide when the columns are few») |
| Limits | Every limit is a setting with a visible default, checked **before any partial result**; a refusal leaves nothing written | Owner (answer 10) |
| Verdicts | **None.** R-hat, ESS, MCSE, divergences, tree depth, E-BFMI are measures or facts; no row holds a threshold, a reference value or a verdict word; no row stops the step on divergences; **no posterior predictive p-value or probability** | Owner (answers 11, 12; rule of 2026-11-20) |
| The common Python library | **Read, not changed**: the runner reuses `tab_protocol` (refusals, JSON, files) and `tab_results` (the schema of the ten columns), and has its own dispatcher and its own task reader, because `tab_protocol.MODES` is `("fit", "predict", "selftest")` (verified) and changing it would change every Python group's `RunnerHash` and start their rechecks | Spec |
| CSV precision | **CmdStan's default: 8 significant digits** a draw (`sig_figs = 8`, measured on CmdStan 2.40.0); the report says it in «How this report was computed» («The CSV files») | Owner (2026-11-23, against this spec's recommendation of 17; 8 instead of 6 on 2026-11-24, «Decisions of 2026-11-24 (K0)») |
| Internal parameters | The program's own variables that are not a node of the graph (a Cholesky factor, the standard normals of a non-centred correlated effect) are **read from the CSV and dropped**: never in the draws, never in the summary | Spec decision, 2026-11-23, owner may revisit |
| Sampler fields in the draws | `lp__`, `accept_stat__`, `stepsize__`, `treedepth__`, `n_leapfrog__`, `divergent__`, `energy__` are kept in the draws (a person looking for where divergences happen needs them) | Spec decision, 2026-11-23, owner may revisit |
| The card | **Written** (`model.json`, `card: true`), registered in MLflow by the gateway; **no Predict** in the first release (predictions on new rows are out of scope, map) | Spec (the map's «Out of scope» and answer 14) |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| A runner protocol | `gateway-go/internal/modelgroups/common/py/tab_protocol.py`: `task.json` `{"protocol": 1, "mode", "group", "group_version", "model", "variant", "params", "output", "write_output", "rows_from", "mlflow", "limits", "input_rows", "ids", "recipe_hash", "runner_hash"}`; `Refusal(code, message, params)`; `write_error` (`artifacts/error.json` and the log line `TABULARIA_ERROR {json}`); exit codes 0, 3 (refused), 4 (self-test failed); `to_json` (no NaN, Python's shortest round-trip floats); `write_bytes`/`read_bytes` on paths or `s3://` addresses | Reused as it is; protocol 1; a key `bayes` added to `task.json` («`task.json`») |
| The task the gateway writes | `gateway-go/internal/modelgroups/groups.go`, `type Task struct` (the fields above; `Limits map[string]int`); `FitTask` checks a model's params through `modelcatalog.Check` | `Task` gains `Bayes *BayesTask` with `json:"bayes,omitempty"`, so the other groups' `task.json` keeps its bytes |
| Run limits | `internal/api/jobs_models.go`, `setRunLimits`: `time_budget_seconds` (the step's timeout), `memory_bytes` (the group's memory limit), `tmp_bytes`, `started_unix`; `modelTmpMiB` sizes `/tmp` from twice the largest model file plus the memory limit plus 256 MiB | Reused; `/tmp` of a `bayes` Job is sized by its own setting instead («Limits») |
| Python Jobs | `modelJobManifest`: uid 10001, read-only root, no capability, `PYTHONHASHSEED=0` for Python groups, `OMP_NUM_THREADS` = the CPU limit (`threadsOf`); the run command `python -I -B /tabularia/code/runner/run.py` (`modelgroups.Commands`) | Reused as is |
| The input of a model step | `modelPrepareInput` appends a `select` of the columns read to the chain and has the engine write it as parquet; the engine registers a `sort` operation (`backend/app/engine/operations.py`, `@register("sort")`) | The input of a series is sorted by the engine («The data file») |
| The ten columns | `docs/specs/statistics.md` «The output table»; `tab_results.schema()` (types exact); `backend/app/engine/models/results.py` checks the table at the import against the catalogue's declaration for the variant, **reads it whole** (`to_pylist`) and refuses more than `max_result_rows` (100,000 in the models' catalogue) | The bayes rows are declared in the models' catalogue as the model `bayes_model`; the caps keep them under 100,000 («The results») |
| Section shapes | The models' catalogue `section_shapes`: `model`, `point`, `curve`, `hist`, `lag` (with `unindexed: ["bound"]`), `reading`, … (verified in `backend/app/engine/models/catalog.json`) | New shapes `summary`, `quantity`, `diagnostic`, `sampler`, `data`, `ppc`, `prior_check` |
| Columns known before a run | `backend/app/engine/models/catalog.py`, `output_columns`: what a model node hands on before it has run; a name the data fill is returned apart | Every output's columns are fixed (the draws are always long), so they are known before any run |
| Readings and «How this report was computed» | `docs/specs/report-explanations.md`: the layout adds the `reading` rows (`common.READINGS`, `with_labels`), the runner writes none | The runner writes no `reading` row; the section is declared so that the layout's rows pass the check |
| Streaming to parquet | `docs/specs/garch-risk.md`, «The Parquet file of the paths» (one writer opened once, one row group per batch, the chunked copy to the output), «The measures, without holding the paths» (two passes, accumulators), «Time» (projection, refusal before any partial result) | The same discipline for the draws |
| MLflow and the card | `internal/api/jobs_models_mlflow.go`: a node with `card: false` registers nothing; `model.json` read whole (small), its sha256 a tag; `tab_card.card(…)` gives the common keys | `card: true`; the `bayes` object of the card |

## The modes

| Mode | Job | Reads | Writes | Exit |
|---|---|---|---|---|
| `compile` | the compile Job (`stan-compile` starts it, in the background at save, or before a run as the fallback: answer 8) | `program.stan` from the ConfigMap, its sha256 in `task.json` | the binary at `TABULARIA_BINARY_OUT`; `artifacts/compile.json`; the **termination message** | 0, 3 |
| `prior_predictive` | a sampling Job with `fixed_param` (answer 13) | the input parquet, the binary, `task.json` | `results.parquet` (variant `prior_predictive`); `run.json` | 0, 3 |
| `sample` | the sampling Job, NUTS | the input parquet, the binary, `task.json` | `results.parquet` (variant `sample`), the output, `model.json`, `MLmodel` when MLflow is set, `run.json` | 0, 3 |
| `selftest` | the check Job of the group (the models spec), and `--selftest` | `reference/` | the self-test report (`TABULARIA_SELFTEST_REPORT`) | 0, 4 |

Exit code 1 (anything else) is a bug of the runner, as for every group: the log's tail says what.

## `task.json`

The common keys of `tab_protocol` (protocol 1), with `group` `bayes`, `model` `bayes_model`, `variant` `sample` or
`prior_predictive` (`compile` for the compile mode), and one key `bayes`:

```json
{ "protocol": 1, "mode": "sample", "group": "bayes", "group_version": 3,
  "model": "bayes_model", "variant": "sample", "params": {"mode": "sample", "output": "summary"},
  "output": "summary", "write_output": true, "rows_from": "trial", "mlflow": false,
  "limits": { "time_budget_seconds": 1800, "memory_bytes": 2147483648, "tmp_bytes": 2147483648, "started_unix": 1795000000,
              "max_result_rows": 100000, "max_chart_points": 2000, "block_values": 1048576,
              "max_quantities": 20000, "max_units": 20000 },
  "input_rows": 600, "ids": {"flow_id": 42, "node_id": "n7", "run_id": 9811, "step_run_id": 9812},
  "recipe_hash": "…", "runner_hash": "…",
  "bayes": {
    "model": {"id": 17, "version": 4, "semantic_hash": "…", "program_sha256": "…", "binary_sha256": "…",
              "codegen_version": 1, "cmdstan": "2.40.0"},
    "sampler": {"chains": 4, "warmup": 1000, "draws": 1000, "seed": 20261123, "adapt_delta": 0.8,
                "max_treedepth": 10, "init": null, "refresh": 100},
    "data": [ … the data manifest … ],
    "quantities": [ … the quantity manifest … ],
    "checks": { … what predictive-checks asks … },
    "replicated": null,
    "series": {"by": "p1", "time": "week"}
  } }
```

- **`params`** holds only what the engine's check of the results reads (`mode`, `output`): the node's other settings
  — the saved model, the binding, the sampler — are checked by the Go gateway (`flow-node`) and travel in `bayes`.
  The engine's import calls the catalogue's `check_params` again (the models spec); for `bayes_model` it knows only
  these two keys.
- **`bayes.model`**: the pinned version (answer 7) and the hashes. `program_sha256` is the sha256 of the program's
  bytes; `binary_sha256` the one `stan-compile` recorded in Postgres from the compile Job's termination message (the
  map, «Integrity»). The runner **refuses to execute a binary whose sha256 differs** (`binary_hash`).
- **`bayes.sampler`**: answer 9 — chains, warmup, draws and seed in view, `adapt_delta`, `max_treedepth` and the
  initial values (`init`: null, a number for CmdStan's `init=<radius>`, or an object of values per node identifier)
  under «Advanced». `refresh` is the runner's, not the person's (how often CmdStan prints its progress).
- **`bayes.data`** and **`bayes.quantities`**: the two manifests `stan-codegen` writes from the validated model
  (below). The runner never reads the graph; it reads what the program declares.
- **`bayes.checks`**: the statistics and bins `predictive-checks` asks the runner to accumulate: `{"format": 1, "on",
  "outcomes", "value_bins", "stat_bins", "density_draws", "max_units", "ppc_draws"}` (`bayes-predictive-checks.md`);
  its cap is `max_check_columns` («Limits») (Correction of 2026-11-24 from the step-4 specs).
- **`bayes.replicated`**: null, or the thinning of the replicated output (`{"draws_per_chain": 50}`) when the node's
  output is `replicated`.
- **`bayes.series`** (left out when the model has no series): `{"by": a group plate's id or null, "time": the input column
  of the series' time or null}`, written by `flow-node` from the binding; the runner checks the rows' order with it (the
  data entries of a series carry neither) (Correction of 2026-11-24 from the step-4 specs).

Every number in `task.json` is checked by the gateway before the Job starts; the runner checks again the ones it
depends on (types, ranges) and refuses a task it cannot run (`task_value`, a bug of the gateway).

### The data manifest

One entry per variable of the program's `data` block, in the program's order:

```json
[ {"stan": "N", "kind": "size", "of": "rows"},
  {"stan": "J_p1", "kind": "size", "of": "p1"},
  {"stan": "score", "kind": "real", "plate": "rows", "column": "score", "node": "n1"},
  {"stan": "hours", "kind": "real", "plate": "rows", "column": "hours", "node": "n2", "standardize": true},
  {"stan": "school", "kind": "group_index", "plate": "rows", "column": "school", "node": "n3", "target": "p1"},
  {"stan": "Mode", "kind": "mode"} ]
```

Kinds: `size` (`N`, `J_<plate>`, `K_<category node>`, and the sizes of `lag` subsets), `real`, `int` (with
`support`: `any`, `nonnegative`, `binary`), `group_index` (1…J, the units in text order), `category` (1…K, the
levels in text order, the first the reference: answer 6), `lift` (an array over a group plate: the unit of an
enclosing plate for each unit, `Model.Lifts` of `model-graph`), `lag` (the row k steps before, within the series'
unit), `rows_subset` (the rows a lagged expression runs on), `first` and `previous` (a latent state's recursion),
`mode` (`Mode`: 0 the posterior, 1 the prior predictive), and the kinds `predictive-checks` adds (bin edges, the
thinning). The `column` is the input column bound by the flow node (answer 6); it is data, renamed before any
library sees it, as every runner does (`tab_protocol`'s rule: «a column's name is data»).

### The quantity manifest

One entry per variable the program **writes** (its `parameters`, `transformed parameters` and `generated
quantities`), in the order of the CSV's columns:

```json
[ {"stan": "mu", "role": "quantity", "node": "n4", "dims": []},
  {"stan": "tau", "role": "quantity", "node": "n5", "dims": []},
  {"stan": "alpha_school", "role": "quantity", "node": "n6", "dims": [{"plate": "p1"}]},
  {"stan": "L_n9", "role": "internal", "dims": [{"size": "K_n9"}, {"size": "K_n9"}]},
  {"stan": "Omega_n9", "role": "quantity", "node": "n9.correlation", "dims": [{"component": "n9"}, {"component": "n9"}],
   "triangle": "strict_lower"},
  {"stan": "Ppc_mean", "role": "check", "check": "mean", "dims": []} ]
```

- **`role`**: `quantity` (a node of the graph: in the draws, the summary, the report), `internal` (read and dropped),
  `check` (a generated quantity of `predictive-checks`: written to the checks file, never to the draws), `prior_draw`
  (in the prior predictive, the ancestral draw of a quantity, with `of` naming it: it replaces the parameter's own
  column, which `fixed_param` leaves at its initial value), `prior_marginal` (one ancestral draw a draw of a quantity node,
  a plate's representative unit, `of` naming it: written in every mode to the checks file, read by `bayes-diagnostics`
  for the prior's density and the contraction) (Correction of 2026-11-24 from the step-4 specs). A `check` names its outcome in `of` (the observed data
  node's id), and an extension's data entry may name its outcome in `node` (Correction of 2026-11-24 from the step-4 specs).
- **`dims`** say how the CSV names a variable's elements: CmdStan writes `name.i.j` (assumed from the CmdStan guide,
  «MCMC sampling», output; checked by lot K1 on the pinned CmdStan). The runner expands the dims with the data's sizes
  and **checks the CSV header against the expanded list, name for name and in order**; a header that differs is a
  bug of Tabularia (exit 1, «a program and its manifest disagree»), never a person's error.
- **`triangle`**: for a correlation matrix, only the elements below the diagonal are quantities; the others are
  dropped.

### Names of the quantities

Every scalar quantity has:

| Field | Example | From |
|---|---|---|
| `quantity` | `alpha_school[Liceo Galilei]`, `tau`, `Omega_n9[slope,intercept]` | the Stan identifier (the person's name, or the generated one: answer 5), then the unit in brackets |
| `node` | `alpha_school` | the identifier alone |
| `label` | `Mean of a school` | the node's label (answer 5), as data |
| `unit` | `Liceo Galilei` | the unit of a group plate (the group column's value as text), a category's level, the component names of a correlation, or null |
| `index`, `index_2` | 3, null | the unit's position (1…J, in text order), the second position of a correlation, or null |

Identifiers are unique (`stan-codegen`), so `quantity` is unique within a run; labels are not, which is why no key
of this protocol is built from a label. The units' texts come from the data: they are data in every file, encoded
in every key («The results») and never part of a program.

## The files

### The program and the binary (`compile`)

- The ConfigMap holds `program.stan` (the program's exact bytes, at most the graph's limits allow: well under the
  1 MiB of a ConfigMap) beside the runner. The runner checks its sha256 against `bayes.model.program_sha256`
  (`program_hash` otherwise), copies it to `/tmp/c/model.stan` and runs the compile **exactly as `bayes-image`'s
  contract says**: `env -i PATH=/usr/local/bin:/usr/bin:/bin make -C /opt/cmdstan /tmp/c/model
  PRECOMPILED_HEADERS=false`, its output to `/tmp/c/make.log` (followed, never held whole), then
  `/opt/cmdstan/bin/stanc --info /tmp/c/model.stan` for the cross-check of `stan-codegen`.
- The binary is hashed while it is read and uploaded **in chunks** (8 MiB, as garch's `ts_sim_copy`) to
  `TABULARIA_BINARY_OUT` (an address in the step's prefix of the scripts bucket).
- `artifacts/compile.json`: `{"program_sha256", "binary_sha256", "binary_bytes", "info" (stanc's
  --info JSON), "stanc": "stanc3 v2.40.0 (Unix)", "seconds", "warnings": [the lines stanc printed as warnings, at
  most 50]}`. Correction of 2026-11-24 from C2: it holds no `info_sha256` of its own (a file cannot hold its own hash);
  the message's `info_sha256` is the sha256 of this file's bytes as uploaded.
- **The termination message** (`/dev/termination-log`, Kubernetes' default path, at most 4,096 bytes; assumed
  writable with a read-only root: lot K3 checks it on kind): `{"program_sha256": "…", "binary_sha256": "…",
  "info_sha256": "…"}`. Another pod cannot write it; the scripts bucket can be written by any user script
  (`docs/specs/script-node.md`, «one key for the installation», the map). **The gateway trusts only the termination
  message**: it copies the binary out of the bucket into Tabularia's storage, checks its sha256 against the message,
  and checks `compile.json`'s `info` against `info_sha256` before using it (`stan-compile`).
- Refusals: `program_hash`; `compile_failed` (stanc or the C++ compiler failed: «the generated program did not
  compile; this is a fault of Tabularia, not of the model: the log says where», the last 40 lines of `make.log` in
  the log). A compile that passes its time or memory limit is stopped by Kubernetes (the Job's deadline, exit 137),
  and the step says which limit, as the Script node does.

### The binary in a sampling Job

`TABULARIA_BINARY_IN` is the binary's address in the step's prefix (the gateway put it there from its own storage).
The runner downloads it in chunks to `/tmp/run/model`, hashing as it goes; it **compares the sha256 with
`bayes.model.binary_sha256` before `chmod 0700` and before any execution** (`binary_hash` otherwise: «the compiled
model is not the one Tabularia recorded: it is not run»).

### The data file

CmdStan's JSON data file, `/tmp/run/data.json`, written **in a stream** from the input parquet
(`pyarrow.parquet.ParquetFile.iter_batches` over the bound columns; never `read_table`, never `tab_columns.read_input`,
which reads the whole input into pandas, verified — and pandas is not in the image):

1. **Pass A** (one read of the bound columns, batch by batch): which rows are kept — a row is kept when every bound
   column is present and finite; a NULL, a NaN or an infinity leaves the row out and is counted per column (no
   imputation, as every model node); the units of each group index and the levels of each category (dictionaries
   bounded by `max_units`: `too_many_units` the moment one passes it); the mean and standard deviation of each
   standardized predictor (Welford's accumulators: answer 6); the checks of the data's shape — an integer column
   with a non-integer value, a count below 0, a binary not 0 or 1 (`data_shape`), a column on a group plate that
   varies within a unit (`data_unit_varies`), a unit of a nested plate in two parents (`data_nesting`), a series out
   of order or with two rows at one time in a unit (`data_order`, `data_time_twice`), a standardized column with no
   spread (`data_constant`), no row left (`data_empty`); and the accumulators `predictive-checks` declares for the
   observed outcome.
2. **Pass B** (a second read, batch by batch): each variable's values appended to its own part file
   (`/tmp/run/data/<variable>.part`), standardized where the manifest says so, indices as 1…J, lags and subsets from
   the sorted rows (a ring of the last `max_lag` units' ids, not the rows); then `data.json` is written by
   concatenating the parts in the manifest's order, 8 MiB at a time, and the parts deleted.

Numbers are written as Python writes back a double exactly (`repr`), integers as integers. A series' rows must come
sorted by (the series' unit, time): **the gateway appends the engine's `sort` operation** after the `select` it
already appends; the runner only checks the order. Memory: one batch, the dictionaries of units and levels (bounded
by `max_units`), the accumulators.

### The CSV files

CmdStan runs as:

```text
/tmp/run/model method=sample num_samples=<draws> num_warmup=<warmup> save_warmup=0
  adapt delta=<adapt_delta> algorithm=hmc engine=nuts max_depth=<max_treedepth>
  num_chains=<chains> data file=/tmp/run/data.json [init=<radius or /tmp/run/init.json>]
  random seed=<seed> output file=/tmp/run/out.csv refresh=<refresh> num_threads=<CPU limit>
```

and with `algorithm=fixed_param` instead of `hmc` in the prior predictive. The argument list is fixed; every value
is a number the runner formats itself; no text of a person reaches it. CmdStan writes `/tmp/run/out_1.csv` …
`out_<C>.csv` (one per chain, assumed from the CmdStan guide, «MCMC sampling», multiple chains: lot K1 checks the
names) and its progress to standard output, which the runner sends to `/tmp/run/stdout.txt` and follows like the
CSVs.

- **8 significant digits** (owner, 2026-11-23 and 2026-11-24): no `sig_figs` argument is passed, so CmdStan writes its
  default, `sig_figs = 8` (measured on CmdStan 2.40.0 of the image by lot K0: `-0.29838641`). **Every draw in the
  parquet, and every number computed from the draws, rests on values rounded to 8 significant digits**: a draw x is
  off by at most 5·10⁻⁸·|x|. The text takes about **10.3 bytes a value** (measured by `bayes-image` on its programs),
  which the disk projection counts. The report says the precision in «How this report
  was computed» (`bayes-report`); the cost is a risk («Risks»).
- Lines: `#` comments (the configuration; after warm-up the adaptation — «Step size = …» and the inverse metric's
  diagonal, a line as long as the parameters; at the end the elapsed times), one header line, then one line a draw:
  the sampler's seven fields, then the program's written variables (`bayes-image`, check 4; the map, «CSV output»).

### Following the CSV files while the chains run

The runner starts CmdStan with `subprocess.Popen` (standard output to `/tmp/run/stdout.txt`, standard error to
`/tmp/run/stderr.txt`) and **does not wait for it**. In one loop:

1. it reads the new bytes of `stdout.txt` and parses the progress lines («Chain [k] Iteration: i / n …», assumed;
   lot K1 checks the pinned format) for the time projection («Time and disk»);
2. it converts blocks in the **fixed order**: block 1 of chain 1, block 1 of chain 2, …, block 1 of chain C, block 2
   of chain 1, … For the chain whose turn it is, it reads that file from its offset (`os.read`, at most 1 MiB at a
   time), splits complete lines, keeps an incomplete last line for the next read (CmdStan's stream may be flushed
   in pieces: the follower never relies on a flush, assumed), skips the comments (keeping only the step size and the
   elapsed times), checks the header once against the manifest, and gathers draw lines **until it has the block's
   B lines** — or the chain's last lines when the process has ended;
3. when nothing new has arrived it sleeps 50 ms and checks that the process is alive;
4. a complete block is parsed into a float matrix (B × columns), checked (a non-finite value in a quantity is
   `not_finite`, with the quantity and the draw; CmdStan writes `nan` and `inf` as text: assumed), fed to the
   accumulators («The measures»), split by role (quantities and sampler fields to the draws writer, checks to the
   checks writer, internal columns dropped), **appended as one row group** to each writer, and freed.

A faster chain is not waited for in memory: its lines wait **on disk** in its own file until its turn. The order of
the row groups therefore depends only on the block size and the number of draws, never on the machines' speed: the
same task gives the **same file**, byte for byte (a self-test check).

**The block.** `B = max(1, floor(block_values / columns))` draws, `block_values` a limit of the task (default
1,048,576 values: 8 MiB of doubles), `columns` the CSV's columns. The text of a block is about 10.3 × `block_values`
bytes (10.3 MiB). The parse of a block: `bytes.split` then `numpy.array(…, dtype=float)`, measured at **36–45 MB of CSV
text a second** for 10 to 20,000 columns, one thread, on 17-digit text (`pyarrow.csv.read_csv` reached 112–165 MB/s
at 10 and 1,000 columns but fell to 13 MB/s at 20,000 columns with 20 lines; lot R0 of `bayes-runner` may use it below
some width if its doubles are the same bits). At 20,000 quantities × 4 × 1,000 draws the CSVs hold about 0.82 GB of
8-digit text; the parse of 8-digit text was not measured (assumed no slower a value than the 17-digit one, about
2 million values a second: about 40 s at that size), and the time projection measures it on the run itself.

## The draws parquet

### Where and when

- `/tmp/run/draws.parquet` is **always written**, also when the output is `summary` or `results`: the second pass
  reads it. With the output `draws` it is copied to `TABULARIA_OUTPUT` **in chunks** at the end (8 MiB at a time, as
  garch's paths file); otherwise it is deleted.
- `/tmp/run/checks.parquet`: the `check` columns of `predictive-checks` (at most `max_check_columns`), in the same
  long schema and the same row-group order; read by the checks' second pass, then deleted.
- **One writer each, opened once**, zstd, one `write_table(block, row_group_size=rows of the block)` per block; the
  block of B draws × P quantities is one row group as long as B × P ≤ Arrow's maximum row-group length
  (1,048,576 rows, measured by the garch spec on Arrow 26.0.1) — which `block_values` = 1,048,576 guarantees.
- **The metadata at the closing.** The schema's key-value metadata `tabularia.bayes` (format version, chains,
  draws per chain, block, program sha256, semantic hash, seed, sampler settings, the count of quantities, the
  `draw` range of each row group) is added when the writer closes (`ParquetWriter.add_key_value_metadata`, assumed
  in pyarrow 26.0.1; lot K1 checks it; otherwise what is known at the start goes in the schema's metadata at the
  opening and the counts are read from the footer's row-group statistics). A reader finds a chain's rows from the
  footer alone, without reading a row group.

### The layout: always long

One row a quantity of a draw (owner, 2026-11-23: always long):

| Column | Type | Holds |
|---|---|---|
| `chain` | Int32 | 1…C |
| `draw` | Int32 | 1…D, within its chain |
| `quantity` | String (dictionary-encoded) | the quantity's name («Names of the quantities»); the sampler's fields as quantities `lp__`, `accept_stat__`, `stepsize__`, `treedepth__`, `n_leapfrog__`, `divergent__`, `energy__` |
| `node` | String (dictionary-encoded) | the identifier |
| `unit` | String (dictionary-encoded), nullable | the unit |
| `index`, `index_2` | Int32, nullable | the unit's positions |
| `value` | Float64 | the draw (8 significant digits) |

Inside a row group the rows are **quantity-major** (all the block's draws of one quantity, then the next): the
second pass slices a block of quantities out of a row group without sorting a column. No order of the rows is
promised to a reader: `chain`, `draw` and `quantity` identify a row. The schema does not depend on the model or on
the data, so the nodes after the Bayesian model know it before any run.

**Why always long** (measured 2026-11-23, for the record): a parquet writer keeps about 1.5–2.4 KB of metadata a
column chunk until it closes, so a wide file of 20,000 quantities × 4 × 1,000 draws needed **2.8 GB** to write (a
159 MB footer), where the long file of the same draws needed **50 MB** (622 MB of file, 5.8 s; its second pass 26.5 s
in 20 passes of 32 MiB). The long writer's peak stays flat with the draws: 43 MB at 2,000 quantities × 4 × 1,000
draws, 45 MB at × 10,000.

## The measures: accumulators and the second pass

**While sampling** (accumulators, per chain, fed by each block in block order): the count of divergent draws, of
draws at `max_treedepth`, the deepest tree, the means of `accept_stat__`, `stepsize__` and `n_leapfrog__`, and for
E-BFMI the running sum of (E_n − E_{n−1})² with the last energy of the previous block kept, and Welford's mean and
variance of the energy (Betancourt 2016, the map's literature); the elapsed times from the comments; the accumulators
`predictive-checks` declares. Memory: a few numbers a chain.

**After sampling** (the second pass, by blocks of quantities read back from the parquet): for each quantity all its
C × D draws (8·C·D bytes: 32 KB at 4 × 1,000), the quantities taken in manifest order, as many at a time as
`pass_bytes` allows — `memory_bytes` / 4 by default, the sampler having exited — so the passes are
⌈8·C·D·P / `pass_bytes`⌉ (one at the defaults up to 16,000 quantities × 4,000 draws). For each block of quantities,
`bayes-diagnostics` computes the summary (mean, sd, quantiles 5, 25, 50, 75, 95 % by linear interpolation between
order statistics — Hyndman and Fan's type 7, NumPy's default, the definition `prior-preview` uses too), MCSE, the rank-normalized split
R-hat, the bulk and tail ESS (Vehtari et al. 2021), and the thinned draws of the charts; the original-scale pairs of
`model-graph` (`Model.Pairs`) are computed draw by draw from the block that holds both members. The summary's rows are
**appended** to `/tmp/run/summary.parquet` (one row group a block of quantities) and to the results writer as they
come. Measured: the long file of 20,000 quantities × 4,000 draws read back in 20 passes of 32 MiB in 26.5 s with a
peak of 113 MB above the start; with `pass_bytes` = 512 MiB it is two passes. The time of the second pass is projected
from the first row group's read rate before it starts («Time and disk»). (Correction of 2026-11-24 from the step-4 specs): the pass reads the `value` column
alone, at offsets computed from the order (checked once against the first row group's `quantity` column), and the
computation has a budget of its own, `compute_bytes` = `memory_bytes` / 8, beside `pass_bytes` (`bayes-diagnostics.md`,
«The second pass»).

## The outputs

| `output` | `TABULARIA_OUTPUT` holds | Schema known before a run |
|---|---|---|
| `summary` (default) | one row a quantity: `quantity`, `node`, `label`, `unit`, `index`, `index_2`, `kind` (`parameter`, `transformed`, `state`, `effect`, `correlation`), `scale` (`model`, or `original` for the second row of a linear-predictor coefficient, answer 6 and the owner's 2026-11-23 answer), `mean`, `sd`, `q5`, `q25`, `q50`, `q75`, `q95`, `mcse_mean`, `mcse_sd`, `rhat`, `ess_bulk`, `ess_tail` | yes (fixed) |
| `draws` | the draws parquet (long) | yes (fixed) |
| `replicated` | y_rep rows for a thinned set of draws: `chain`, `draw`, `row` (1…N, the kept rows in the input's order), `outcome` (the observed data's Stan identifier, dictionary-encoded: Correction of 2026-11-24 from the step-5 specs), `value`; written in blocks like the draws (from a second CmdStan run of `predictive-checks`, assumed: its spec) | yes (fixed) |
| `results` | nothing beyond `results.parquet` | — |

The summary's numbers are Float64 (`rhat`, `ess_*` NULL when not computed: a quantity constant over every draw); its
texts are data. `results.parquet` is written in every mode, as for every model node.

## The results (the ten columns)

The statistics' ten columns, the same names, types and order; `ord` 0 to N−1; `key` unique, its parts joined by `|` exactly as
the engine checks it (`backend/app/engine/models/results.py`: `key == "|".join(parts)`, then `|` and the index for a
chart's point), **without `key_part`'s escape** (Correction of 2026-11-24 from the step-4 specs); a non-finite number NULL. Written by a **streaming writer** with `tab_results.schema()` (one row
group per block of rows, `ord` continuing), not by `tab_results.Rows`, which keeps every row in Python lists
(verified): the rows are bounded anyway (below), but the second pass produces them block by block and nothing is
gained by holding them.

### Sections

| Section | `level` | `level_2` | Holds |
|---|---|---|---|
| `model` | null | null | Texts: `model` (`bayes_model`), `variant`, `rows_from`, `group_version`, `software` («CmdStan 2.40.0; stanc3 v2.40.0; Python 3.14.x; numpy 2.5.5; pyarrow 26.0.1», from the image's lock), `bayes_model` (the saved model's id), `bayes_version`, `semantic_hash`, `program_sha256`, `binary_sha256`, `sampler` (`nuts` or `fixed_param`), `metric` (`diag_e`), `precision` (`8 significant digits`), `note_<id>`. Numbers: `n` (rows used), `n_missing`, `n_not_finite`, `chains`, `warmup`, `draws`, `seed`, `adapt_delta`, `max_treedepth`, `threads`, `n_quantities`, `n_in_summary`, `seconds_data`, `seconds_warmup`, `seconds_sampling`, `seconds_conversion`, `seconds_pass_2` |
| `quantity` | the identifier | null | The mapping (answer 5): texts `label`, `name` (null when generated), `node_id`, `kind`, `plate`, `plate_label`; numbers `n_quantities` (its scalars), `rhat_max`, `ess_bulk_min`, `ess_tail_min` (over all its scalars, measures) |
| `summary` | the identifier | the unit (null for a scalar) | The summary's numbers for the quantities in the report's scope (below); with a linear-predictor pair also `orig_mean`, `orig_sd`, `orig_q5`, `orig_q50`, `orig_q95` |
| `data` | the data identifier | null | Texts `column`, `type`, `reference` (a category's first level), `standardized` (`yes`, `no`); numbers `mean`, `sd` (the standardization), `n_units` or `n_levels`, `n_missing` |
| `sampler` | the chain (`1`…`C`) or `all` | null | Facts: `divergences`, `treedepth_hits`, `treedepth_max`, `e_bfmi`, `stepsize`, `accept_stat_mean`, `n_leapfrog_mean`, `seconds_warmup`, `seconds_sampling`, `draws` |
| `ppc` | the statistic (`mean`, `sd`, `min`, `max`, `q10`, `q90`, `zeros`…, as `predictive-checks` declares), or the outcome alone for its row `n` | the group's unit or null | `observed` (T(y), unindexed); `n` (the outcome's rows, unindexed, one a checked outcome: *Correction of 2026-11-24 from lot Q4*, the report's count of values drawn outside the data's range needs it, an outcome with a lag having fewer rows than the model's `n`); `count` per bin of T(y_rep) over the draws with `x0`, `x1` (indexed). **No p-value, no probability, no share beyond T(y)** (answer 12) |
| `prior_check` | as `ppc` | as `ppc` | The same rows from the prior predictive (variant `prior_predictive`) |
| `point` | chart id | series | `units:<identifier>` (each unit's posterior median and interval: series `median`, `low`, `high`; `x0` its rank, `text` its unit) |
| `curve` | chart id | series | `trace:<identifier>` (series = the chain; thinned draws), `density:<identifier>` (series `posterior`, `prior`), `ppc_density` (series `observed`, `draw_<k>`), `prior_density` |
| `hist` | chart id | series | `rank:<identifier>` (series = the chain; rank histograms) |
| `reading` | block | level_2 | **Written by the layout, never by the runner** (`report-explanations.md`, B0); declared here so that the check accepts them |

Keys, exactly: `model|||n`, `quantity|tau||rhat_max`, `summary|alpha_school|Liceo Galilei|rhat`,
`summary|tau||mean`, `sampler|2||divergences`, `ppc|mean||observed`, `ppc|mean||count|7`,
`curve|trace:tau|3|value|41`, `point|units:alpha_school|median|y|12`. A unit holding a bar or a backslash is written as
it is: the engine's check joins the parts without escaping them (verified by lot K1) (Correction of 2026-11-24 from the step-4 specs). The `ppc` and
`prior_check` level is `<outcome>:<statistic>` (`ppc|y:mean||observed`) and the density chart `ppc_density:<outcome>`
(`bayes-predictive-checks.md`) (Correction of 2026-11-24 from the step-4 specs).

Every `ppc`/`prior_check` row is a chart's data: the report draws them (`bayes-report`), and its words say what the
chart shows and never whether the model «passes» (the map).

### The report's scope and the caps

`backend/app/engine/models/results.py` reads the results whole and refuses more than `max_result_rows` (100,000):
the table must be bounded **by construction**, whatever the model's size. Constants of the models' catalogue (new
keys of its `limits`):

| Limit | Default | What |
|---|---|---|
| `bayes_max_summary_quantities` | 2,000 | Quantities with `summary` rows (12 rows each, 17 for a linear-predictor pair: at most 34,000 rows) |
| `bayes_max_chart_quantities` | 12 | Quantities with trace, rank and density charts |
| `bayes_trace_points` | 200 | Thinned draws a chain in a trace (evenly spaced, the last included) |
| `bayes_rank_bins` | 20 | Bins of a rank histogram |
| `bayes_density_points` | 200 | Points of a density curve |
| `bayes_max_unit_charts` | 8 | Plates' quantities with a `units:` chart |
| `bayes_unit_points` | 1,000 | Units in a `units:` chart: above it a seeded sample, the ten most extreme medians always kept (the models spec's rule for `max_chart_points`, with a smaller cap) |

**Which quantities**: the scalar quantities first (the hyperparameters and the population-level parameters, answer
14), then the correlations, then each plate's quantities node by node in the program's order and units in index
order, until `bayes_max_summary_quantities`; the charts follow the same order. Everything else is in the `summary`
output and in its node's `quantity` row (its maximum R-hat, its minimum ESS: measures); the note
`note_summary_capped` says how many are left out of the report («the report lists 2,000 of 6,120 quantities; the
node's summary output has them all»).

**The arithmetic** (asserted by a test of the catalogue), at the most chains the node allows (8): 34,000 (summary)
+ 12 × (8 × 200 + 8 × 20 + 2 × 200) = 25,920 (charts) + 8 × 3 × 1,000 = 24,000 (units) + `quantity` and `data` (at
most `max_nodes` = 200 nodes × 8 rows each: 3,200) + `sampler` (9 × 10) + `model` (about 60) + `ppc` and
`prior_check` (`predictive-checks`' caps, at most 5,000) ≈ **92,300 rows, under 100,000**. (Correction of 2026-11-24 from the step-4 specs): the code's formula
(`bayescontract.MaxResultRows`, with `bayes_max_quantity_entries` 600 × 10 for the `quantity` section) gives **96,690**;
the pair charts of `bayes-diagnostics` add 6 × 300 = 1,800, **98,490**. A change of any cap, or of
the chains' maximum, re-runs the test. (Correction of 2026-11-24 from the step-5 specs: with the contraction and the original scale counted for the
`bayes_max_quantity_entries` alone, as lot X4 of `bayes-diagnostics` built it, the arithmetic is **93,490**: 26,000 + 3,000
+ 25,920 + 1,800 + 24,000 + 6,000 + 1,600 + 90 + 80 + 5,000, 6,510 under the limit; the figures 96,690 and 98,490 above are
superseded.)

### The catalogue's declaration

A model `bayes_model` in `backend/app/engine/models/catalog.json` (group `bayes`, language `python`, `card: true`),
`takes: {"mode", "output"}`, two variants (`sample` when `mode = sample`, `prior_predictive`), each with its
`sections` and `numbers`/`texts` as above, its `charts`, and its `outputs` (`summary`, `draws`, `replicated`, all with
fixed columns). Copied to the gateway by the existing
`model_catalog.py`, as every model. `predict.applies_to` does not name it.

## The card

`artifacts/model.json`, the common keys of `tab_card.card` (`format` `tabularia-model`, `format_version` 1, `model`,
`variant`, `group`, `group_version`, `recipe_hash`, `runner_hash`, `software`), `inputs` (the bound columns with
their types, the categories' levels and reference), and an object `bayes`:

```json
{ "bayes": {
    "model": {"id": 17, "version": 4, "semantic_hash": "…", "codegen_version": 1},
    "program": {"sha256": "…", "bytes": 2314}, "binary": {"sha256": "…", "cmdstan": "2.40.0", "image": "sha256:…"},
    "mapping": [{"stan": "mu", "node_id": "n4", "name": "mu", "label": "Mean of the schools", "kind": "parameter"}],
    "data": [{"stan": "hours", "column": "hours", "standardized": true, "mean": 6.21, "sd": 2.4}],
    "units": {"p1": {"count": 20, "labels": ["…"]}},
    "sampler": {"algorithm": "nuts", "chains": 4, "warmup": 1000, "draws": 1000, "seed": 20261123,
                "adapt_delta": 0.8, "max_treedepth": 10},
    "facts": {"divergences": 0, "treedepth_hits": 0, "e_bfmi_min": 0.93},
    "summary": [{"quantity": "mu", "mean": 3.1, "sd": 0.4, "q5": 2.4, "q50": 3.1, "q95": 3.8, "rhat": 1.001,
                 "ess_bulk": 3510, "ess_tail": 2980}] } }
```

- **No draw**, ever (answer 14). `summary` holds the scalar quantities only (the report's first ones), `units` the
  labels of each plate; both are cut first when the card would pass `max_card_bytes` (1 MiB), then the existing rule
  applies: `note_card_too_large`, no registration (the models spec, verified in `tab_card`).
- `MLmodel` (the `tabularia` flavor, written by hand as `tab_card.mlmodel` does) when `mlflow` is set. What MLflow
  logs is `bayes-mlflow`'s; it reads this card and `results.parquet`, never the draws.
- `card.schema.json` gains the `bayes` object.

## Refusals and errors

`artifacts/error.json` `{code, message, params}`, the log line `TABULARIA_ERROR`, exit 3; the sentence names the way
out. **A refusal leaves no partial result**: every file is written under `/tmp` first and copied to the bucket only
after the last check, so a refused step has no results, no output and no card (the garch rule: the same seed must
give the same numbers, and a cut that depends on the machine's speed would not).

| Code | When | Sentence (abridged) |
|---|---|---|
| `protocol`, `no_task`, `mode`, `task_value` | `task.json` missing, of another protocol or mode, or a value out of range | as `tab_protocol` (a fault of Tabularia) |
| `program_hash` | compile: `program.stan` is not the program recorded | «the program is not the one this version saved» |
| `compile_failed` | stanc or the C++ compiler failed | «the generated program did not compile: a fault of Tabularia, not of the model» |
| `binary_hash` | the binary's sha256 differs | «the compiled model is not the one Tabularia recorded: it is not run» |
| `data_empty` | no row left after leaving out the missing values | «no row has every bound column: 1,204 rows have a missing value» |
| `data_shape` | an integer, count or binary column with a value that does not fit | «`visits` must be a count: row 812 holds 2.5» |
| `data_constant` | a standardized predictor with no spread | «`hours` has the same value on every row: it cannot be standardized» |
| `data_unit_varies` | a column on a group plate varies within a unit | «`district_income` changes within the school «Liceo Galilei»» |
| `data_nesting` | a unit of a nested plate in two parents | «the school «Liceo Galilei» is in two districts» |
| `data_order`, `data_time_twice` | a series out of order, two rows at one time in a unit | the unit and the time |
| `too_many_units` | a group or a category passes `max_units` | «`school` has more than 20,000 values: is it the right column?» |
| `too_many_quantities` | the program would write more than `max_quantities` scalars | «the model has 31,400 quantities; this installation allows 20,000» |
| `too_slow` | the time projection passes `time_budget_seconds` | «about 41 minutes for 4 chains of 2,000 iterations; the node's timeout is 30 minutes: fewer draws, or a longer timeout up to the group's 60» |
| `too_much_disk` | the disk projection passes `tmp_bytes` | «the draws would write about 6.1 GB; this Job's disk holds 4 GB: fewer draws or chains» |
| `init_failed` | CmdStan could not start a chain (its message «Initialization … failed», assumed; lot K1 reads the pinned text) | «the sampler found no starting point where the model's density is finite: check the bounds and the priors, or give initial values under Advanced» |
| `sampler_failed` | CmdStan exited with another error | the last lines of `stderr.txt` (CmdStan's own text, about our program and our data) |
| `not_finite` | a quantity's draw is not a finite number | the quantity and the draw |

**Divergences are not a refusal** (answer 11): the run goes on to the end and their count is a fact
(`sampler|all||divergences`, the report's first page).

## Limits

Settings with visible defaults (answer 10), shown on the group's card or the node, all in `task.json`'s `limits`:

| Limit | Default | Where | Checked |
|---|---|---|---|
| `time_budget_seconds` | the node's timeout, up to the group's longest run (3,600 s, `bayes-image`) | node, group | after the first progress lines, after every progress line, before the second pass |
| `memory_bytes` | the group's memory (2 GiB, `bayes-image`) | group | sizes `pass_bytes` |
| `tmp_bytes` | **2 GiB**, a new sampling setting of the group (`modelTmpMiB`'s formula fits model files, not CSVs; `bayes-image`'s card shows it) | group | after the first block of each chain (the bytes a draw line takes × the draws left, plus the parquet's bytes a draw × the draws, plus what `/tmp` holds) |
| `max_quantities` | 20,000 (the map's measured size) | group | when the manifest is expanded with the data's sizes, before sampling |
| `max_units` | 20,000 | group | in pass A |
| `block_values` | 1,048,576 | constant (a test lowers it) | — |
| `max_check_columns` | 200 | constant | by the gateway (`predictive-checks`' declaration) |
| chains, warmup, draws | 1–8, 0–10,000, 1–10,000 | node (answer 9), within the group's maxima | by the gateway |

## Time and disk

The runner's clock starts at `started_unix`. After CmdStan's first progress lines it projects the rest — the
iterations left × the time an iteration took so far (warm-up and sampling counted apart once both are seen), the
conversion at the measured parse rate, the second pass at the first row group's read rate, the copy at the write
rate — and refuses `too_slow` when the projection passes the budget; it checks again at every progress line. After
the first block of each chain it projects the disk. A refusal stops CmdStan (`terminate`, then `kill` after 5 s) and
deletes `/tmp/run`.

**The disk, estimated** (recomputed 2026-11-24 for 8 digits: 10.3 bytes a value of 8-digit CSV text, measured by
`bayes-image`; the long parquet measured 2026-11-23 at 7.5 bytes a value with 6-digit values and 7.75 with full
doubles, so at most 7.75 with 8-digit ones): about **18 bytes a value** while both are on `/tmp`, so the map's
reference size of 20,000 quantities × 4 chains × 1,000 draws (80 million values) takes about 1.45 GB, plus the data
file (tens of MB at `max_rows`) and the binary (3 MB). The default `tmp_bytes` of **2 GiB** holds it with about 0.7 GB
to spare (some 119 million values);
beyond, the projection refuses `too_much_disk` before any result.

## Risks

| Risk | Where it bites | Answer |
|---|---|---|
| **8 significant digits** (owner, 2026-11-23 and 2026-11-24) | A draw is rounded by up to 5·10⁻⁸ of its size, so a quantity whose mean is large beside its posterior standard deviation loses its posterior: an intercept of 1,000,000.123 with a standard deviation of 0.001 is written as 1,000,000.1 on every draw, and its sd, quantiles, R-hat and ESS describe the rounding, not the posterior. The predictors are standardized by default (answer 6), which keeps the coefficients near the scale of their sd; **the outcome is not**, so the intercept sits on the outcome's scale (an outcome in the millions, measured precisely on many rows, is the case) | A fact said in «How this report was computed» (the precision of the draws); the summary's numbers are what they are, never adjusted; no threshold, no verdict. The person can rescale the outcome before the node |
| **The disk** | Large models with many draws | The projection after the first block, `too_much_disk` before any result |
| **CmdStan's text** (file names, progress lines, the initialization message, `nan`) | The follower misreads a line | Assumed here; lot K3 reads them on the pinned CmdStan and the fake sampler copies them |

## Tech stack

- **Python** 3.14 of the image (`python:3.14-slim`), **`numpy` 2.5.5, `pyarrow` 26.0.1** (`bayes-image`'s pins); the
  standard library (`subprocess`, `os`, `hashlib`, `json`); no pandas (not in the image), no `cmdstanpy`.
- **Go** 1.27, standard library: `modelgroups.Task` gains `Bayes`; the manifests' types live in
  `internal/bayescontract` (shared by `stan-codegen`, `stan-compile`, `flow-node`).
- **The engine** (Python, `backend/`): the catalogue's entry `bayes_model` and its section shapes; no new code path in
  `results.py` beyond the declaration.

## Commands

```bash
# the runner's unit tests, outside any image (uv with the image's pins; small data only)
uv run --no-project --with pyarrow==26.0.1 --with numpy==2.5.5 \
  python -m unittest discover -s gateway-go/internal/modelgroups/groups/bayes/runner/tests -v
# the memory test, alone (a heavy job for this machine: one at a time)
BAYES_MEMORY_TEST=1 uv run --no-project --with pyarrow==26.0.1 --with numpy==2.5.5 \
  python -m unittest gateway-go/internal/modelgroups/groups/bayes/runner/tests/test_memory.py -v
# the Go side and the catalogue
cd gateway-go && go test ./internal/bayescontract/... ./internal/modelgroups/...
cd backend && pytest tests/test_models_catalog.py -k bayes -q        # no -n on this machine
python3 .github/scripts/model_catalog.py && python3 .github/scripts/model_catalog.py --check
```

## Project structure

```text
gateway-go/internal/bayescontract/
  task.go            BayesTask, the data and quantity manifests, Validate()
  task_test.go       the manifests' invariants on the corpus of model-graph
  testdata/tasks/    task.json examples per mode (read by the Python tests too)
gateway-go/internal/modelgroups/groups.go             Task gains Bayes (omitempty)
gateway-go/internal/modelgroups/groups/bayes/runner/
  run.py             the dispatcher: sys.path as foundation_forecast's run.py, then bayes_main.main()
  py/bayes_main.py   the task reader (the four modes) and the dispatch; reuses tab_protocol
  py/bayes_task.py   the manifests read and checked; the CSV header expected
  py/bayes_data.py   passes A and B, data.json
  py/bayes_follow.py the follower of the CSV files and of stdout
  py/bayes_draws.py  the draws and checks writers (long), the copy in chunks
  py/bayes_rows.py   the streaming writer of the ten columns
  py/bayes_card.py   model.json
  py/bayes_compile.py the compile mode
  tests/             unit tests, fake_cmdstan.py (writes CSV and progress as CmdStan would, slowly), test_memory.py
backend/app/engine/models/catalog.json                the entry bayes_model, the section shapes, the limits
gateway-go/internal/modelgroups/card.schema.json      the object bayes
```

`bayes-runner`, `bayes-diagnostics` and `predictive-checks` add their own files under `runner/py/`; this module owns
the protocol files above.

## Code style

The common library's refusal, as it is (verified, `common/py/tab_protocol.py`):

```python
class Refusal(Exception):
    """A refusal (tab_refuse): code (a short id), message (an English sentence that names the
    way out), params (what the sentence names, as data)."""

    def __init__(self, code: str, message: str, params: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.params = params or {}
```

The follower in the same voice (a sketch; lot K2 writes it): a block is gathered from the file's offset, never from
its start, and freed once written.

```python
def next_block(chain: Chain, rows: int, alive: Callable[[], bool]) -> list[bytes] | None:
    """The next `rows` draw lines of `chain`, read from where the last block ended; fewer only
    when CmdStan has ended. Comments are parsed for the few numbers kept and dropped."""
    lines: list[bytes] = []
    while len(lines) < rows:
        got = chain.read_complete_lines(limit=rows - len(lines))   # os.read from chain.offset
        if got:
            lines.extend(chain.keep_draws(got))
            continue
        if not alive() and chain.at_end():
            return lines or None
        time.sleep(0.05)
    return lines
```

Comments are sentences that say why; names in English; no abbreviation a reader must guess.

## Testing strategy

**On small data, always** (owner's rule): a few hundred rows, a few quantities, fake CSVs; the sizes of the
measurement above are never in the suite.

- **Go** (`bayescontract`): the manifests of every graph of `model-graph`'s corpus validate; `Task`'s JSON of
  the other groups unchanged byte for byte; `testdata/tasks/` written by Go and read by the Python tests.
- **Python, no CmdStan** (`fake_cmdstan.py` writes the CSVs as CmdStan does — comments, header, the adaptation lines
  after warm-up, draw lines, the timing — in random pieces cut mid-line, chains at different speeds, with `nan` and
  `inf` when asked):
  - the follower: the row groups' order is block-major and chain-minor whatever the speeds; the file is the same
    byte for byte across five runs with different speeds; a line cut in two is read once; the header checked;
    `not_finite` named; the converted part of a file is never read again (a test counts the bytes read: equal to the
    files' sizes);
  - **no whole read**: a test patches `open`, `read_csv`, `pq.read_table` and `pq.write_table` to fail on the CSVs and
    the draws file, and runs a whole fake sampling;
  - the long writers: the schema exactly; the rows quantity-major; the metadata at the closing; the
    chunked copy equal to the file;
  - the data passes: every refusal of «Refusals» on a tiny input; standardization by Welford against numpy on the
    same column; the units in text order; lags and subsets; NULLs and non-finite values counted;
  - the results: the ten columns' types; keys unique and joined as the engine checks them (a unit named `a|b`, a backslash); the caps; the
    arithmetic of «The report's scope and the caps»;
  - the refusals leave `/tmp/run` empty and nothing at the output addresses (a fake bucket directory);
  - `binary_hash`: a binary with one byte changed is never executed (a test checks that `os.chmod` and `Popen` are
    not called on it).
- **The memory test** (`test_memory.py`, run alone, by hand and in lot K2's acceptance): a child process runs the
  runner on `fake_cmdstan.py` output, its peak resident memory read from `/proc/<pid>/status` (`VmHWM`):
  - **long**: 2,000 quantities, 4 chains, 1,000 then **10,000** draws a chain (62 MB then 619 MB of parquet);
  - **the replicated output**: 1,000 rows, 50 then 500 thinned draws a chain;
  - accepted when each peak at ten times the draws is within **5 MB** of the peak at one time (a declared tolerance
    of the test, not a verdict of any report). Measured with the same writer outside the runner: 43 → 45 MB above the
    process's start.
- **On the image** (lot K3, by hand, one heavy job at a time): the compile mode on `bayes-image`'s reference AR(1),
  the termination message's content, the sample mode on the normal model with 4 chains; the CSV names, the
  default of 8 significant digits (measured by K0), the progress lines' format and the initialization message read from the pinned CmdStan.
- **Regression**: the models' catalogue tests, `test_models_results` (the check of the ten columns), the other Python
  groups' runner hashes unchanged (no file of `common/py` changed).

## Boundaries

- **Always**: draws appended while sampling, one row group a block, chains interlaced in the fixed order; offsets
  kept, nothing read twice; summaries from accumulators or the second pass by blocks; every file under `/tmp` until
  the last check; the binary's sha256 checked before it runs; only the termination message trusted for hashes; units
  and labels as data, keys joined as the engine checks them; every limit a setting with its default visible.
- **Ask first**: a new mode, section, output or column; changing a cap of the results; another layout of the draws; a
  package beyond `numpy` and `pyarrow`; any change to `common/py` (it changes every Python group's runner hash).
- **Never**: a whole CSV, a whole chain or a whole draws table in memory (`read_csv`, `read_table`, `write_table` of
  draws, `cmdstanpy`); a step that waits for the end of sampling to read a chain; a verdict, a threshold, a reference
  value or a posterior predictive p-value in a row; draws in MLflow or in the card; a person's text in CmdStan's
  arguments; a binary run without its hash checked; a partial result after a refusal.

## Success criteria

1. `task.json` of the four modes is written by Go and read by the runner, `testdata/tasks/` shared; the other groups'
   `task.json` bytes are unchanged.
2. On fake CSVs at different speeds, the draws parquet is the same file byte for byte, its row groups in block-major,
   chain-minor order, every draw present once, the CSV bytes read exactly once.
3. The memory test passes: the peak at ten times the draws is within 5 MB of the peak at one time, for the draws
   and the replicated output.
4. Every refusal of the table has a test that trips it and leaves no file; a binary with a changed byte is never
   executed.
5. `results.parquet` passes the engine's check for both variants; the caps' arithmetic stays under 100,000 rows.
6. The card holds no draw and passes `card.schema.json`; above 1 MiB the existing note applies.
7. On the image (K3): a compile writes the termination message with the binary's sha256; a sampling run of the
   normal model gives the closed-form posterior within four MCSE (as `bayes-image`'s check 4).

## Lots

| Lot | Objective | Files | Accepted when | Agent |
|---|---|---|---|---|
| **K0** | The Go types and manifests, `Task.Bayes`, `testdata/tasks/`; the shapes, sections and limits declared in Go (`testdata/contract.json`) | `internal/bayescontract/*`, `groups.go`, its tests | Go tests green; other groups' bytes unchanged (done, «As built — lot K0») | sonnet |
| **K0b** | The filtered view: the `catalog.json` and the `card.schema.json` of every other group's ConfigMap are the catalogue and the schema without the group `bayes` (its group, its model, its limits, shapes and kinds; the schema's `bayes` object), filtered in the order of the file; the group `bayes` receives them whole. Then the entry `bayes_model` in `catalog.py` (equal to `bayescontract`'s declaration, a test compares them), its copies, and the card schema's `bayes` object, in one commit with the interface's lot (the copy, the words in five languages, the node left out of the Models palette) and `bayes-report`'s first lot (the layout, a fixture per variant, the texts) | `internal/modelgroups/groups.go` (`codeFiles`), `internal/modelcatalog`, `catalog.py`, `card.schema.json`, its tests | **the golden bytes of the seven groups unchanged, without rewriting them** (`TestTheOtherGroupsBytesAreAsTheyWere`, `TestTheOtherGroupsBuildAndCheckAreAsTheyWere`); the catalogue's tests and `model_catalog.py --check` green | sonnet |
| **K1** | The task reader, the data passes, the writers (draws, checks, results), the card | `bayes_main.py`, `bayes_task.py`, `bayes_data.py`, `bayes_draws.py`, `bayes_rows.py`, `bayes_card.py`, tests | unit green on fake inputs | sonnet |
| **K2** | The follower and `fake_cmdstan.py`; the memory test | `bayes_follow.py`, `tests/fake_cmdstan.py`, `test_memory.py` | criteria 2 and 3 | **opus** (the owner's rule; concurrency of files) |
| **K3** | The compile mode, the termination message, the check on the image (by hand) | `bayes_compile.py`, tests | criterion 7 on the image, alone | sonnet |

K0 first (it needs `model-graph`'s M2 for the validated model); K1 and K2 in parallel after it; K0b before or with
`bayes-report`'s first lot, and before K1 writes the card's schema; K3 after
`bayes-image`'s B0. Only K2's memory test and K3 are heavy: one at a time.

## Correction of 2026-11-24 from K0

Lot K0 read stan-codegen's manifests as built (lots S0–S5), measured CmdStan 2.40.0 in the image of `bayes-image`'s B0
(a two-variable program compiled and run with NUTS and `fixed_param`, `--memory 3g --cpus 1`, read-only root), and fixes
what the sections above left open. Where this section and the text above differ, this section holds.

1. **A levels plate has K − 1 elements.** A dimension `{"plate": "levels:<c>"}` runs over the levels **after the
   reference** (the second to the K-th in text order): `K_<c>` is the number of levels, the vector has `K_<c> - 1`
   elements. No field is added (stan-codegen's manifests keep their bytes). Such a quantity's `unit` is the level's text
   and its `index` the level's position among all the levels (2…K), the same number the data's category code gives it.
2. **A `prior_draw` in the sample mode.** Its `dims` are the prior predictive's. A prior draw with a dimension is sized
   `Mode == 1 ? s : 0`, so in `sample` it has **no column** (measured: a zero-size array writes none); stan-codegen now
   refuses (`ErrExtension`) a prior draw with a dimension whose size is not by the mode. A single-valued prior draw has
   its one column in both modes: in `sample` it is read and dropped, never checked for finiteness. In
   `prior_predictive` a prior draw's columns replace those of the quantity it is `of`.
3. **The data manifest, complete.** Kinds: `size` (`of`: `rows`, a group plate's id, `levels:<c>`, or the series' array
   it sizes: `N_lag` of `Rows_lag`, `N_first` of `Rows_first`, `N_next` of `Rows_next`), `real` and `int` (`plate` the
   rows or a group plate, `node`, `int` with `support` `any` | `nonnegative` | `binary`, `standardize`), `group_index`
   (`plate` rows, `node`, `target` the group plate), `category` (`plate` rows, `node`; its levels plate
   `levels:<node>`), `lift` (`plate` the inner group plate, `node` the group index lifted, `target` the outer plate),
   `rows_subset` (`Rows_lag`), `lag` (`Lag<k>`, with the field **`lag`: k**), `first` (`Rows_first`), **`next`**
   (`Rows_next`, new), `previous` (`Prev`), all four on the rows; `mode` (`Mode`, once); and an extension's kinds (a
   lower-case word, names `Ppc_…` or `Prior_…`, defined by `predictive-checks`). `column` is written by flow-node, and
   only for `real`, `int`, `group_index` and `category` (`DataReadsColumn`); the others the runner derives. The order
   is stan-codegen's: `N` first, then the group plates' `J`, the categories' `K`, the data nodes, the lifts, the series'
   arrays, `Mode`, the extension's data.
4. **The quantity manifest, complete.** A dimension is exactly one of `plate`, `size` (a `size` entry or an integer of
   the extension's data) or `component` (an effect node's id: its components, in the order of the mapping's
   `component` entries). An effect's Cholesky factor is `L_<id>` with dims `[{"component": id}, {"component": id}]`
   (not `{"size": "K_…"}`: `K_` is a category's size), its standard normals `Z_<id>` `[{"component"}, {"plate"}]`, a
   centred effect `V_<id>` `[{"plate"}, {"component"}]`; a component's quantity has `node` `<id>.<component>`, the
   correlation `<id>.correlation` with `triangle` `strict_lower`. **Units**: a group plate's unit is the group column's
   value as text; a levels plate's as in 1; a component's unit is the component's Stan identifier; a quantity on the rows
   plate (a state, a value per row) has `unit` null and `index` the kept row's number (1…N, as the replicated output's
   `row`); a `size` dimension has `unit` null and `index` the position.
5. **The CSV, measured on CmdStan 2.40.0.** Elements are named `name.i.j` with **the first index fastest**
   (column-major), for a `matrix` and for an `array[] vector` alike (`M.1.1, M.2.1, M.1.2, …`). With `num_chains=C ≥ 2`
   the files are `out_1.csv … out_C.csv`; **with one chain the file is `out.csv`, without a suffix**. Under
   `fixed_param` the sampler's fields are only `lp__` and `accept_stat__` (both 0). Progress lines with one chain read
   `Iteration:  21 / 23 [ 91%]  (Sampling)`. `bayescontract.Header` writes the expected header (the runner checks its
   own against `testdata/tasks/<mode>.header.json`).
6. **The default precision is 8 significant digits, not 6.** CmdStan 2.40.0 writes `sig_figs = 8 (Default)` in the CSV's
   configuration and 8 digits a value (`-0.29838641`); with `sig_figs=6` it writes `-0.298386`. «6, CmdStan's default»
   (Decisions of 2026-11-23) held two things that differ; the owner chose CmdStan's default, 8 digits («Decisions of
   2026-11-24 (K0)»), and the text above says 8. It is also why `bayes-image` measured 10.3 bytes a value where this
   spec assumed 8.5.
7. **`task.json`'s `bayes` object, as built** (`bayescontract.Task`, the Go name; «BayesTask» above): `model`, `sampler`,
   **`plates`** (`[{id, kind, label}]`: the `quantity` section's `plate_label`), **`mapping`** (stan-codegen's mapping
   table as saved with the version: the labels, names, kinds and components the summary and the `quantity` section
   write; the runner still never reads the graph), `data`, `quantities`, `checks`, `replicated`. Every key is always
   written (lists as `[]`, absent objects as `null`); `binary_sha256` is left out in the compile mode. The compile mode
   reads **`model` alone** (no sampler, no manifest; `output` none); `prior_predictive` runs **without warm-up**
   (`warmup` 0) and its output is `results`; `replicated` is set exactly when the output is `replicated`, with
   `draws_per_chain` in 1…draws (default 50); `selftest` has no `bayes` object. Ranges: chains 1–8 (4), warmup
   0–10,000 (1,000), draws 1–10,000 (1,000), `adapt_delta` strictly between 0 and 1 (0.8), `max_treedepth` 1–15 (10),
   `seed` 0…2³²−1 (CmdStan's unsigned seed), `refresh` 1–10,000 (100); `init` null, a radius ≥ 0, or an object of
   finite values (a number for a single value, a list for a vector) keyed by a quantity's Stan identifier. The gateway
   checks all of it before a Job (`Task.Check`, with `CheckManifests`).
8. **The caps, recounted.** The `quantity` section has a row group per quantity *entry* (a node, each component of an
   effect, a correlation: up to 11 a node), not per node: 200 nodes × 11 × 10 rows would pass 100,000 alone. A cap is
   added, **`bayes_max_quantity_entries` = 600** (in the report's order; the rest counted in `note_quantities_capped`,
   and in the summary output); the `ppc`/`prior_check` cap is named **`bayes_max_check_rows` = 5,000**. The arithmetic
   (`MaxResultRows`, asserted against the catalogue's `max_result_rows`): 34,000 (summary) + 25,920 (charts) + 24,000
   (units) + 6,000 (`quantity`) + 1,600 (`data`, 200 entries × 8) + 90 (`sampler`) + 80 (`model`) + 5,000 (checks) =
   **96,690**.
9. **Shapes.** `diagnostic` is not declared: no section uses it. `summary` (level required, level_2 optional),
   `quantity`, `data`, `sampler` (level required, level_2 null), `ppc` and `prior_check` (indexed, `observed`
   unindexed). The variant `prior_predictive` writes `model`, `quantity`, `summary` (mean, sd and the five quantiles of
   the prior draws), `data`, `prior_check`, `curve` and the layout's `reading`.
10. **What every group's ConfigMap holds.** `catalog.json` **and `card.schema.json`** are in the ConfigMap of every
    model group (`modelgroups.codeFiles`) and in its `RunnerHash`: adding `bayes_model` to the catalogue or the `bayes`
    object to `card.schema.json` changes the bytes of all seven other groups and starts their recheck. K0 did not add
    the catalogue's entry; the owner chose the filtered view (lot K0b, «Decisions of 2026-11-24 (K0)»). K1 does not edit
    `card.schema.json` before K0b (the card's `bayes` object is in `bayescontract.Card` meanwhile).

## Correction of 2026-11-24 from the step-4 specs

From `bayes-stan-compile.md`, `bayes-diagnostics.md` and `bayes-predictive-checks.md`, approved by the owner on
2026-11-24, and the coordinator's checks of lot K1. Where this section and the text above differ, this section holds.

1. **The compile's task.** A compile is not a step of a flow: its `ids` are **empty**, and its `limits` are the compile's
   (`time_budget_seconds` the deadline, `memory_bytes` and `tmp_bytes` the compile's), not the sampling ones of
   `testdata/tasks/compile.json`'s example (to be rewritten by the lot that next writes the testdata).
2. **Keys.** The engine's check wants `key == "|".join(parts)` (then `|` and the index for a chart's point) **without**
   `key_part`'s escape (verified by lot K1 in `backend/app/engine/models/results.py`); the text of «The results» is
   corrected.
3. **`bayes.series`** = `{by, time}`, left out (`omitempty`) when the model has no series: the runner of K1 reads it
   already; `flow-node` writes it.
4. **Roles and manifests.** A role `prior_marginal` (`of` its node; in every mode, to the checks file); a `check` names its
   outcome in `of`; an extension's data entry may name its outcome in `node` (`checkQuantity` and `checkDataEntry` refuse
   both today: their lot changes them). Done by lot P2 of predictive-checks (its «As built — lot P2»):
   `RolePriorMarginal`, and `CheckManifests` wants a check's `of` and an extension datum's `node` to be data nodes the data
   reads (the units' slots name their group index).
5. **The checks.** `bayes.checks` is `predictive-checks`' object; the `ppc`/`prior_check` level is
   `<outcome>:<statistic>`, the density chart `ppc_density:<outcome>`; the `replicated` output comes from a
   `generate_quantities` run on the thinned draws (the «assumed: its spec» of «The outputs»), and so do the posterior
   checks if lot P0 confirms that CmdStan reads the follower's thinned CSV, otherwise they are inline (the owner,
   2026-11-24).
6. **The summary and the charts.** `summary` gains the number `contraction` (and the `summary` output its column),
   NULL when the prior's marginal has no finite variance (the owner, 2026-11-24); `curve` gains the charts
   `pairs:<a>:<b>` (series `draw`, `divergent`); the catalogue's `limits` gain `bayes_max_pair_quantities` (4) and
   `bayes_pair_points` (300). The arithmetic: 96,690 rows by the code's formula, 98,490 with the pairs, under 100,000.
7. **The second pass** reads the `value` column alone, with a computation budget `compute_bytes` (`memory_bytes` / 8).

## As built — lot K0

`gateway-go/internal/bayescontract/`: `task.go` (the constants, `Task`, `ModelRef`, `Sampler`, `Init`, `Replicated`,
`Plate`, `Task.Check`), `manifest.go` (`DataEntry`, `Quantity`, `Dim`, `MappingEntry` and their kinds and roles, moved
from `bayescodegen`, which now uses them: the golden manifests and mappings keep their bytes), `check.go`
(`CheckManifests`, `Header`, `Sizes`, the sampler's columns), `declaration.go` (the columns of the draws, summary and
replicated outputs, the limits with their defaults, the shapes, the sections of both variants, `MaxResultRows`, the
refusals and the exit codes), `card.go` (`Card`). `testdata/tasks/{compile,prior_predictive,sample,selftest}.json`
(written by Go from stan-codegen's golden `hier_slopes` and `extension_fake`) with `<mode>.header.json`, and
`testdata/contract.json` (the declaration): `BAYES_CONTRACT_WRITE=1 go test ./internal/bayescontract/` rewrites them.
`modelgroups.Task` gains `Bayes *bayescontract.Task` (`omitempty`): a test keeps another group's task.json byte for byte.
Tests: every golden case and every valid graph of model-graph's corpus and of the opened templates gives manifests that
hold together; broken manifests and tasks refused; the header against the measured CmdStan; the caps under 100,000.
The models' catalogue is unchanged (no `bayes_model` yet), and the seven groups' golden bytes are as they were.

## Decisions of 2026-11-23 (the answers to this spec's questions)

| Question | Decision | By |
|---|---|---|
| The draws: wide, long, or decided from the graph | **Always long**: no wide layout, no limit, no choice; the schema is the same for every model and known before any run | Owner, 2026-11-23 |
| The CSV's precision: 17, 9 or 6 significant digits | **6, CmdStan's default** (replaced on 2026-11-24 by 8, CmdStan's true default: below) (against this spec's recommendation of 17): the precision of every draw is 6 significant digits, said in «How this report was computed»; the loss for an intercept large beside its sd is in «Risks»; the disk is estimated at 8.5 bytes a value of CSV | Owner, 2026-11-23 |

## Decisions of 2026-11-24 (K0)

| Question | Decision | By |
|---|---|---|
| The entry `bayes_model` in the models' catalogue, which every group's ConfigMap holds | **Option A**: the entry goes in the models' catalogue; every other group's ConfigMap receives a **view** of the catalogue without the group `bayes`, filtered in the order of the file, and the same for `card.schema.json`, so that their bytes do not change; the entry comes with the interface's lot (the copy, the words, the node left out of the Models palette) and `bayes-report`'s lot (the layout, the fixtures, the texts). The view is lot K0b | Owner, 2026-11-24 |
| The CSV's precision, CmdStan 2.40.0's default being 8 significant digits (measured by K0), not 6 | **8 significant digits, CmdStan's true default** (`sig_figs = 8`), instead of 6: no `sig_figs` argument; the disk recomputed at about 18 bytes a value while the CSV and the parquet are both on `/tmp` (the reference size about 1.45 GB, under the 2 GiB default) | Owner, 2026-11-24 |

## Open questions

None (the two opened by K0 are answered in «Decisions of 2026-11-24 (K0)»).

## Correction of 2026-11-24 from the step-5 specs

From `bayes-runner.md`, `bayes-report.md` and `bayes-flow-node.md`, approved by the owner on 2026-11-24. Where this section
and the text above differ, this section holds.

1. **`task.json`'s `bayes` gains four keys, all `omitempty`** (the other groups' bytes unchanged), written by `flow-node`:
   `finite_variance` (the `of` of every `prior_marginal` whose prior has a finite variance, decided in Go by
   `bayeschecks.FiniteVariance` from the stored graph; absent: every contraction NULL with its note), `scale_pairs` (the
   original-scale pairs of `Model.Pairs` as Stan identifiers: `{coef, data}` for a term, `{intercept, lp, data}` for a
   linear predictor's intercept), `model.program_bytes` and `model.image` (the card's `program.bytes` and `binary.image`).
   `series` (correction 3 of the step-4 section) is written by `flow-node` too.
2. **The `generate_quantities` runs.** Besides the sampling run, a sampling Job runs CmdStan as `/tmp/run/model
   method=generate_quantities fitted_params=/tmp/run/fitted.csv num_chains=<C> data file=/tmp/run/data_gq.json random
   seed=<seed> output file=/tmp/run/gq.csv refresh=<refresh> num_threads=1` (the first three measured by lot P0 of
   `bayes-predictive-checks.md`, the rest checked by `bayes-runner`'s lot R2) for the posterior checks and, with the output
   `replicated`, once more with `fitted_rep.csv`. Their header is the quantity manifest's entries whose mapping `block` is
   `generated quantities`, expanded with that run's sizes; their data file is `data.json`'s bytes up to the first entry of
   the extension's data, then that run's entries.
3. **The files under `/tmp/run`.** `prior.parquet` holds the sampling run's `prior_marginal` columns (long, the draws'
   schema and order), read by the second pass for the prior's density and the contraction; `checks.parquet` holds the
   checks' run's check columns only (its redrawn `prior_marginal` columns are dropped).
4. **The `replicated` output** gains the column `outcome` (the observed data's Stan identifier, dictionary-encoded):
   `chain`, `draw`, `row`, `outcome`, `value`; `draw` is the thinned draw's number in its chain (the owner's decision of
   2026-11-24, through the coordinator).
5. **The `model` section** gains the numbers `seconds_checks` and `seconds_replicated` and the notes `note_checks_dropped`
   (draws whose generated quantities CmdStan wrote as NaN, dropped from the checks and counted), `note_no_checks` (a version
   stored before the checks), `note_prior_undrawn` (quantities without an ancestral draw in the prior predictive).
6. **Time and disk** count the `generate_quantities` runs (their data file, fitted lines, outputs); the disk is also
   projected before sampling with the measured constants; a checks' run that would pass the timeout is `too_slow` for the
   whole step, before any partial result (the owner's rule on the limits).
7. **The charts' ids** hold the **quantity's name** (`trace:alpha_school[Liceo Galilei]`), as lot X3 writes them, the
   identifier only for a scalar.
8. **K0b** is lot Q0 of `bayes-report.md` (the filtered view, alone) and lot Q1 (the entry `bayes_model`, in one commit
   with the interface's catalogue lot and the report's first layout); a node of type `model` naming `bayes_model` is
   refused by the gateway («the Bayesian model is a node of its own»).
9. **The arithmetic of the caps is 93,490** (the paragraph «The arithmetic» above, corrected in place).

## Correction of 2026-11-24 from P3a

Lot P3a of `bayes-predictive-checks.md` found that the data manifest did not say which likelihood runs on the rows that
have every lag: with a lagged likelihood and one on every row, the runner could not tell whose positions are among
`Rows_lag`. Where this section and the text above differ, this section holds.

1. **An extension's datum may name in `of` the rows its positions are among**: the rows plate (`rows`) or `Rows_lag`
   (the series' `rows_subset`, which the data must then hold); any other `of` on an extension's datum is refused
   (`CheckManifests`). `DataEntry.Of` keeps its meaning for a size.
2. **predictive-checks' `post_rows` and `prior_rows`** (`Ppc_o<k>_rows`, `Prior_o<k>_rows`) carry it: `"of": "Rows_lag"`
   when the outcome's likelihood is lagged (its statement is `y[Rows_lag] ~ …`), `"of": "rows"` otherwise. The generator
   decides it from its own `lagged` map (`bayescodegen.Decl.RowsOf`: the extension names the likelihood, the codegen writes
   the rows), so the manifest and the program cannot disagree.
3. **`CodegenVersion` 3**: the programs' bytes are unchanged, the manifests a save stores change (every golden manifest
   says `codegen_version: 3`; the `checks_*` ones gain the `of`; a new golden case `checks_mixed_lag` holds a lagged
   likelihood and one on every row). A version stored by version 2 has no `of`: the runner reads it as before (every
   outcome lagged when the data holds a `rows_subset`), and a save of it writes the new manifest.
4. **The runner** (`bayes_checks.py`, `Plan._lagged`) reads each outcome's `post_rows` `of`; `of` other than `rows` or
   `Rows_lag`, or `Rows_lag` without a `rows_subset`, is `task_value`.

## Correction of 2026-11-24 from R2

Lot R2 of `bayes-runner.md` (the `generate_quantities` runs) left one question to the owner: a `replicated` draw with a value that is not
finite (the program threw in that draw: CmdStan writes every generated quantity of it as NaN) was the refusal `not_finite` of the whole
step, while the checks' draws are dropped and counted. The owner's decision (2026-11-24), settled in lot R3; where this section and the text
above differ, this section holds.

1. **A replicated draw with a value that is not finite is left out whole and counted**: none of its rows is written (the draw's number in
   the file has a gap), the footer says `dropped_draws`, and the `model` section gets the note `note_replicated_dropped` («N of the M
   replicated draws were left out of the replicated data because the program failed to draw a value in them»). The step is refused
   (`not_finite`, the quantity `replicated`) **only when no draw is left**. The note is declared by the `note_*` of the `model` section's
   texts: no change to the declaration or the catalogue.
2. **The same rule in the prior predictive for the draws file** (lot R3): a prior draw with a quantity that is not finite (an ancestral draw
   that threw, or one with no mass) is left out of that quantity's summary and counted in `note_prior_dropped`; the checks' draws are
   dropped as in `sample` (`note_checks_dropped`). The prior predictive's draws file is therefore written without the `not_finite` check.
3. **The prior predictive writes no `hist` and no `point` row** (the variant declares none: «Shapes»): neither the rank histograms, the
   units' charts nor the pairs; and no `curve` of the quantities (traces, densities), whose pages the variant does not have: its charts are
   the `prior_density` curves of the checks alone. The summary is the mean, the sd and the five quantiles; `model|||metric` is not written
   (`fixed_param` has no metric) and no `sampler` row.
4. **A quantity without an ancestral draw** (a correlation matrix, a node the prior path does not draw) is left out of the draws, the
   `quantity` section and the `summary`, and counted in `note_prior_undrawn` (`n_quantities` counts the scalars written).

