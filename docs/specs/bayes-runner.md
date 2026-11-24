# Spec: the sampling Job of a Bayesian model (`bayes-runner`)

Status: **written and approved by the owner on 2026-11-24.** It specifies the module `bayes-runner` of
`docs/specs/bayesian-editor-map.md` (**the map**), step 5 of its order of construction, after the approved
`bayes-contract.md` (**the contract**), `bayes-diagnostics.md` (**the diagnostics spec**), `bayes-predictive-checks.md`
(**the checks spec**), `bayes-stan-compile.md` (**the compile spec**) and `bayes-image.md` (**the image spec**). Most of
its pieces are built and tested alone — the task reader, the data passes, the writers and the card (contract, lot K1), the
follower of the CSV files (K2), the compile mode (K3), the measures, the sampler's accumulators and the second pass
(diagnostics, X1–X5) — and **nothing assembles them**: `bayes_follow.run` refuses `not_built` today (verified). This
module is the assembly: the modes `sample` and `prior_predictive` from the binary's download to the last copy, the
`generate_quantities` runs of the checks, the projections and refusals, the results, the card, and the Go side of the
sampling Job (its resources, its `/tmp`, its environment, the task fields it needs from the stored graph). Decisions
marked **(owner)** are the owner's (the map's «Interview answers», **the answers**, and the owner's rules); the rest are
this spec's, to confirm; those marked «spec decision, 2026-11-24, owner may revisit» are small choices the owner may change
without reopening the module.

Words: **verified** = read in this repository's code on 2026-11-24; **measured** = recorded by an earlier lot in the spec
named beside it, or measured for this spec (said where); **assumed** = from the CmdStan documentation or an earlier spec,
not run here, closed by the lot named beside it.

## Objective

One Kubernetes Job a node run, one Python process, that:

1. fetches the compiled model from `TABULARIA_BINARY_IN` in chunks and **refuses to execute a file whose sha256 is not
   `bayes.model.binary_sha256`** (the map, «Integrity»; the contract, `binary_hash`);
2. reads the input parquet in batches and writes CmdStan's data file **in a stream** (the contract's passes A and B,
   built), with the predictive checks' data for each run (the checks spec);
3. runs CmdStan with the node's chains, warm-up, draws, seed and `adapt_delta`, `max_treedepth`, initial values, and as
   many threads as the CPU limit, and **follows the CSV files while the chains write them**, appending the draws to the
   parquet block by block (the owner's rule of 2026-11-22/23, built by K2);
4. **projects its time and its disk** from what it has seen and refuses, with a sentence, **before any partial result**
   when a projection passes a limit (answer 10);
5. computes the summary and the diagnostics **by blocks** (second pass, built by X3), the prior-to-posterior contraction
   from the run's own prior draws, the coefficients on the original scale (answer 6), and the posterior predictive checks
   in a **`generate_quantities` run on 1,000 thinned draws** (lot P0's conclusion (b));
6. writes the ten-column results, the output the node asked for (summary by default, draws or replicated data on request:
   answer 14), the card (`model.json`, never a draw) and `run.json`, and copies them only after the last check.

And the same machinery for the **prior predictive check** (answer 13): `fixed_param`, `Mode = 1`, the checks on, no warm-up.

Who calls it: `flow-node` builds its task and starts its Job; `bayes-report` draws its rows; `bayes-mlflow` reads its card.

## Decisions

| Question | Decision | By |
|---|---|---|
| Memory | **Never everything in RAM**: draws appended while sampling, one row group a block; the measures by blocks of quantities; the checks' and the replicated draws written in blocks; a test shows the Job's peak flat when the draws are multiplied by ten | Owner (rule of 2026-11-22/23) |
| Precision | CmdStan's default, **8 significant digits**: no `sig_figs` argument | Owner (2026-11-24, the contract) |
| Diagnostics | Measures and facts only: no threshold, no stop on divergences, no verdict | Owner (answer 11) |
| Posterior predictive checks | **A `generate_quantities` run after sampling** on `bayes.checks.ppc_draws` (1,000) draws thinned evenly over the chains; the sampling run keeps `Ppc_on` 0 | Owner, 2026-11-24, conditioned on P0; P0 measured (b) (the checks spec, «As built — lot P0») |
| Prior predictive | **A Job**: `fixed_param`, `Mode = 1`, `Ppc_on` 1, **4 chains × 250 draws**, no warm-up, the output `results` | Owner (answer 13); the counts: the checks spec, «The runs» |
| Limits | Every limit a setting with a visible default, checked **before any partial result**: everything is written under `/tmp/run` and copied out only after the last check | Owner (answer 10); the contract |
| Order of the work | **The binary first** (fetched and hashed before the data are read), then the data, then the runs: a poisoned binary is refused before any row is read | Spec decision, 2026-11-24, owner may revisit |
| The disk, before sampling | Projected **before** CmdStan starts, with the measured 10.3 bytes a value of CSV text and 7.75 of parquet (the contract), and again **after the first block of each chain** with the chain's own bytes a draw (K2's `BlockInfo`): the first refusal costs no sampling | Spec decision, 2026-11-24, owner may revisit (the contract says «after the first block»; the earlier check is the same projection with the measured constants) |
| The data file of a `generate_quantities` run | **A copy of the sampling run's `data.json` up to the first entry of the checks' data**, then that run's own entries: pass B records the byte offset where the extension's data begin (they come last in the manifest's order: the contract, correction 3); the copy is 8 MiB at a time, never a parse | Spec decision, 2026-11-24, owner may revisit |
| Where the prior's draws come from | The **sampling run's own `prior_marginal` columns** (written in every run: the checks spec), to `/tmp/run/prior.parquet`; the `generate_quantities` run's redrawn ones are dropped | Spec decision, 2026-11-24, owner may revisit (4,000 draws a quantity instead of 1,000; one source for the density and the contraction) |
| A finite prior variance | Decided **in Go from the stored graph** (`bayeschecks.FiniteVariance`, the catalogue's `finite_variance` and the graph's truncations and arguments) and sent in the task as the list of the `prior_marginal`s that have one; absent: every contraction NULL with its note | The diagnostics spec («Prior-to-posterior contraction»: «decided from the graph by one function»); the task's field: this spec |
| The original scale (answer 6) | Computed **draw by draw** in the second pass for the pairs the stored graph declares (`Model.Pairs`, sent in the task), only for the coefficients of a linear predictor | Owner (answer 6 and 2026-11-23); the contract («The measures») |
| The allocator | `bayes_follow.use_system_allocator()` once at the start of every sampling mode, and `MALLOC_MMAP_THRESHOLD_` and `MALLOC_TRIM_THRESHOLD_` at 262,144 in the Job's environment (never in CmdStan's, which runs with `PATH` alone) | K2 and X3's measures (the peaks of the same run varied by 20 and 8 MB without them) |
| The sampling Job's `/tmp` | `bayes_settings.sampling_tmp`, else the recipe's `resources.tmp` (2 GiB), as the `emptyDir`'s `sizeLimit` **and** as `tmp_bytes`; not `modelTmpMiB`'s formula, which fits model files | The contract («Limits»); the compile spec («The administration card») |
| Retries | The node's retries apply to failures of the cluster only; a refusal (exit 3) ends the step at once, as every model step (`modelAttempts`, verified) | The models spec |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| The dispatcher | `runner/py/bayes_main.py`: `run_task` loads `bayes_follow` for the sampling modes, runs `prepare_run` (pass A, `build_layout`, `too_many_quantities`, pass B, `data.json`), then `bayes_follow.run(task, prepared)`; `/tmp/run` deleted on any exception | `run` is replaced by this module's `bayes_run.run(task, prepared)`; `prepare_run` takes the binary's check first («The order») |
| The follower and CmdStan's start | `bayes_follow.py`: `Follower`, `sample(binary, sampler, algorithm, header, block_values, workdir, data, threads, *sinks, check_header, **hooks)`, `cmdstan_args` (the fixed argument list, every value a number it formats), `write_init`, `start` (CmdStan with `PATH` alone, stdout and stderr to files), hooks `on_progress`, `on_idle`, `on_block`, `on_line`; refusals `sampler_failed` (`truncated`, `chain_ended`, `malformed`, `exit`) and `init_failed`; CmdStan stopped and every sink aborted on any exception | Used as it is for every CmdStan run of the Job; `cmdstan_args` gains the `generate_quantities` form |
| The writers | `bayes_draws.py`: `DrawsWriter` (long, one row group a block, `not_finite` for a program's quantity, the metadata at the closing, `abort`), `ReplicatedWriter`, `copy_in_chunks`, `read_metadata`; `bayes_rows.py`: `ResultsWriter` (streaming, `ord` continuous, keys joined as the engine checks them), `model_section`, `data_section`, `quantity_section`, `software` | The draws, prior, checks and replicated files; the results |
| The sampler's facts | `bayes_facts.py`: `FactsAccumulator(header, chains, max_treedepth)`, a sink (`write_block`, `abort`); `facts(step_sizes)` per chain and `all` | A sink of the sampling run |
| The second pass | `bayes_pass2.py`: `second_pass(draws_path, layout, limits, writer, *, summary_path, budget, diagnostics, prior, seed, seconds_left, quantity_entries, prior_variance)`, `Budget.of(limits)` (`memory_bytes`/4 and /8), `prior_variance_of(draws, finite_variance)`, `sampler_section(w, facts, seconds)`; `too_slow` before any row | Called once per run with the prior's draws and variances |
| The card | `bayes_card.py`: `build(task, facts, layout, sampler_facts, summary, program_bytes, image)`, `fit` (cuts the summary, then the labels, then `note_card_too_large`), `write` | Called in the sample mode |
| The compile mode's upload | `bayes_compile.upload_hashing(source, target, chunk)` (8 MiB, sha256 while it reads; `s3://` through `tab_protocol._fs`) | Its mirror for the download: `bayes_binary.fetch_hashing` |
| The common library | `tab_protocol`: `Refusal`, `write_error`, `ENV_INPUT`, `ENV_OUTPUT`, `ENV_RESULTS`, `ENV_ARTIFACTS`, `threads(task)` (= `OMP_NUM_THREADS`), `is_uri`, `_fs` | Read, never changed (its bytes are in every Python group's `RunnerHash`) |
| The checks' data and rows | The checks spec, lot P3 (not built: `bayes_checks.py` does not exist); `bayeschecks.TaskChecks(on)` (P2, working tree) writes `bayes.checks`; the stored manifests name the extension's data by kind (`checks_on`, `post_count`, `post_rows`, `prior_count`, `prior_rows`, `replicated_count`, `bin_slots`, `bin_scale`, `bin_shift`, `binned_count`, `replicated_rows`, verified in the golden `checks_*.manifest.json`) | Called through the interface of «The checks' half» |
| The mapping's blocks | Every mapping entry has `block` (`data`, `parameters`, `transformed parameters`, `generated quantities`, `model`; verified in the golden `checks_hier_slopes.mapping.json`: the checks, the prior marginals and draws, and the correlation `rho_g` are in `generated quantities`) | The expected header of a `generate_quantities` run |
| A model Job | `internal/api/jobs_models.go`: `modelJobManifest` (image by digest, uid 10001, read-only root, no capability, no token, the offline label, `PYTHONHASHSEED=0`), `setRunLimits` (`time_budget_seconds`, `memory_bytes`, `tmp_bytes` = `modelTmpMiB`, `started_unix`), `threadsOf(img.CPULimit)` → `OMP_NUM_THREADS`, `modelStepKeys`, `modelAttempts`, `importModel` (`/scripts/results/import` with the card, `/scripts/output/import`) | The sampling Job is a model Job with the bayes group's `/tmp`, environment and step files («The Job, from the gateway») |
| The bayes settings | `internal/api/models_admin_bayes.go` (lot C4, in the working tree): `bayesSettingsRead`, `bayesCardDefaults` (`SamplingTmp` = the recipe's `resources.tmp`), `bayesCardInForce` | The `/tmp` in force |
| The catalogue's variance rule | `bayescatalog.FiniteVariance` (`always`, `never`, `above` a constant argument; lot X4), `FiniteVariance.Holds(value)` | `bayeschecks.FiniteVariance(m)` walks the graph with it |
| The original-scale pairs | `bayesgraph.Model.Pairs []ScalePair{Node, Term, Data, Intercept}` («coef / sd», the intercept shifted by Σ coef × mean / sd) | Sent in the task as Stan identifiers |

## The sampling Job, in order (`sample`)

1. **The allocator**: `use_system_allocator()`; the environment holds the two `MALLOC_*` variables («The Job, from the
   gateway»).
2. **The binary** («The binary»): fetched, hashed, compared, `chmod 0700`. Nothing else runs before it.
3. **The data** (`prepare_run`, built): pass A with the checks' observations (P3a); the layout; `too_many_quantities`;
   pass B with the sampling run's extension values (checks off); `data.json`, and the byte offset where the extension's
   data begin.
4. **The disk, projected** before sampling («Time and disk»).
5. **The sampling run** (NUTS): the follower hands every block to four sinks — the draws writer (`/tmp/run/draws.parquet`),
   the prior writer (`/tmp/run/prior.parquet`, the `prior_marginal` columns), the `FactsAccumulator`, and the
   thinned-lines writer (`on_line`: the draw lines kept for the `generate_quantities` runs, written as CmdStan wrote them
   into `/tmp/run/fitted_<c>.csv` and, with the output `replicated`, `/tmp/run/fitted_rep_<c>.csv`, each after the
   sampling run's header line). `on_progress` and `on_idle` check the time, `on_block` the disk.
6. **The second pass** over the draws (built): the summary file, the `summary`, `quantity`, chart rows; the prior's draws
   and variances from `prior.parquet` («The contraction»); the original scale («The original scale»).
7. **The checks' run** (`generate_quantities`, `Ppc_on` 1) when the program holds the checks (`checks_on` in the data
   manifest) and the task's `checks.on`: its data file, its run, its output followed into `/tmp/run/checks.parquet`; then
   P3b's second pass over that file writes the `ppc` rows and the overlaid densities.
8. **The replicated run** (`generate_quantities`, `Ppc_on` 0, `Ppc_o<k>_rep` = the rows) when the output is
   `replicated`: its output followed into `/tmp/run/replicated.parquet` by `ReplicatedWriter`.
9. **The rows** that remain: `model_section` (with every `seconds_*`), `data_section`, `quantity_section`,
   `sampler_section`, the notes; the results writer closed (its three required keys checked).
10. **The card** (`build`, `fit`, `write`) and, when `mlflow` is set, `MLmodel`; `run.json` (software, the seconds of each
    stage, the warnings).
11. **The copies**, only now: `results.parquet` to `TABULARIA_RESULTS`; the output to `TABULARIA_OUTPUT` when
    `write_output` (the summary file, the draws file or the replicated file, 8 MiB at a time); the artifacts.
12. `/tmp/run` deleted; exit 0.

Any refusal or fault before step 11 stops what runs, aborts every sink, deletes `/tmp/run` (built in `bayes_main` and the
follower) and leaves nothing at the output addresses. A failure **during** the copies (the bucket) is not a refusal: exit 1,
and the gateway deletes the step's files by exact key as for every model step (`modelStepKeys`, verified).

### `prior_predictive`

The same steps 1–4; the run is `fixed_param` (`cmdstan_args(…, "fixed_param", …)`, built: `num_warmup=0`, no adaptation)
with `Mode` 1 and the checks on, its rows the development sample's bound rows (the trial is the only way in: `flow-node`);
the follower's sinks are the draws writer (the `prior_draw` columns in place of the quantities': `build_layout`, built) and
the checks writer; no `FactsAccumulator`, no thinned lines, no `generate_quantities`. The second pass runs with
`diagnostics=False` (mean, sd, the quantiles; built); P3b writes `prior_check` and `prior_density`. **A quantity that has no
ancestral draw** (a correlation `rho_*`, a transformed parameter the prior path does not draw: the checks spec's P2 note) is
left out of the summary and of the charts, with the note `note_prior_undrawn` (count): `build_layout` keeps its columns
today (verified), at their initial values under `fixed_param` — this module leaves them out. No card, the output `results`.

## The binary

`TABULARIA_BINARY_IN` is an `s3://` address in the step's prefix (`env-<…>/<step>/model`, the only form
`bayescompile.Compiler.Place` accepts: verified). `bayes_binary.fetch_hashing(source, target)` reads it **8 MiB at a time**
into `/tmp/run/model` (created `0600`), hashing as it writes, and compares the digest with `bayes.model.binary_sha256`:

- equal: `os.chmod(target, 0o700)`, then the runs;
- different: the file is deleted and the step refused, `binary_hash` («the compiled model is not the one Tabularia
  recorded: it is not run»), **before** any `chmod` and any `Popen` (a test patches both and fails on a call);
- missing address or missing `binary_sha256`: `task_value` (a fault of the gateway).

The binary is the third check of the same hash (the compile spec, «Three checks of the same hash»); the size is not
checked here (the engine's `place` copied at most `binary_max_bytes`).

## The runs of CmdStan

| Run | Mode | `Mode` | `Ppc_on` | CmdStan | Chains × draws | Its output | Sinks |
|---|---|---|---|---|---|---|---|
| Sampling | `sample` | 0 | 0 | `method=sample … algorithm=hmc engine=nuts` (built) | the node's | `out.csv` or `out_<c>.csv` | draws, prior, facts, thinned lines |
| Checks | `sample` | 0 | 1 | `method=generate_quantities fitted_params=/tmp/run/fitted.csv num_chains=<C>` | `ppc_draws` / C a chain | `gq_<c>.csv` | checks |
| Replicated | `sample`, output `replicated` | 0 | 0, `Ppc_o<k>_rep` > 0 | the same with `fitted_rep.csv` | `draws_per_chain` a chain | `rep_<c>.csv` | replicated |
| Prior predictive | `prior_predictive` | 1 | 1 | `method=sample … algorithm=fixed_param` (built) | 4 × 250 | `out_<c>.csv` | draws, checks |

- **`generate_quantities`** (lot P0, measured on CmdStan 2.40.0): `fitted_params=/tmp/run/fitted.csv num_chains=4` opens
  `fitted_1.csv` … `fitted_4.csv`, runs the chains **one after the other** in one process, and writes `<output stem>_<c>.csv`
  holding **the generated quantities only** (no `lp__`, no parameter, no sampler column), one row a fitted draw in order; a
  size-0 quantity writes no column; the fitted file needs a header with the parameters by name and accepts extra columns
  and comments. The rest of the argument list — `data file=…`, `random seed=<the node's seed>`, `output file=…`,
  `refresh=…`, `num_threads=1` — is **assumed** from the CmdStan guide and checked by lot R2 on the image. The process is
  started with `bayes_follow.start` (PATH alone, stdout and stderr to files).
- **The thinned draws**: for a chain of D draws and k kept, the draws ⌈(i·D)/k⌉ for i = 1…k (evenly spaced, the last
  included), chosen by `on_line(chain, draw, line)` as the sampling run's lines arrive; a kept line is written as CmdStan
  wrote it (8 digits), never parsed again. With `ppc_draws` = 1,000 and 4 chains: 250 a chain. The (chain, draw) of each
  kept line is the position the `generate_quantities` output's rows are joined by (P0: by position).
- **Following a `generate_quantities` output** is the same `Follower` with that run's header: **the quantity manifest's
  entries whose mapping `block` is `generated quantities`, in the manifest's order, expanded with that run's sizes**
  (`Ppc_on` 1 gives the statistics' columns; the prior draws of a plate, `Mode == 1 ? J : 0`, give none). A header that
  differs is `ManifestMismatch` (exit 1). The chains being sequential, the fixed block order of the follower waits on the
  disk for chain 2 while chain 1 runs: its files are small (1,000 rows of at most 200 columns).
- **The data file** of a `generate_quantities` run: the bytes of `data.json` before the recorded offset, then that run's
  extension entries (P3a's values), then `}`; written in 8 MiB pieces. Its size is the sampling one's (counted twice by the
  disk projection).
- **NaN in the generated quantities** (P2's note: CmdStan writes NaN for every generated quantity of a draw whose
  generator throws, measured): the checks writer counts the draws whose check columns are NaN, drops them from the checks
  (P3b), and the count is the note `note_checks_dropped`; never `not_finite` (that refusal is for the program's sampled
  quantities, K1's rule).

## The checks' half (the interface P3 gives)

The checks spec's lot P3 holds the predictive checks' Python. This module calls it through four functions, and **does not
start before they exist** (lots R1 and R2 depend on P3a and P3b, «Lots»):

```python
plan = bayes_checks.plan(task)                       # the outcomes, their kinds, units and bins, from the manifests and bayes.checks
plan.observe(batch_columns)                          # pass A: min, max, Welford, zeros, rows per unit of each checked outcome (P3a)
sizes, values = plan.extension(run, facts)           # run in {"sample", "checks", "replicated", "prior"}: the extension's data (P3a)
plan.rows(checks_path, writer, section, seconds_left)  # the second pass over the checks file: ppc or prior_check rows, densities (P3b)
```

`extension("sample", …)` gives the checks-off values (`Ppc_on` 0, every count 0, every array empty, the scales 0.0), so a
sampling run of a version with the checks writes no check column and draws no replicated value (the checks spec, «One
program, every run»). A version stored by generator 1 has no extension: `plan` is empty, no run beyond the sampling one,
the note `note_no_checks` («this version was saved before the predictive checks: saving it again adds them»).

## The contraction and the prior's density

- `prior.parquet` holds the sampling run's `prior_marginal` columns in the draws' long schema (one row group a block, the
  same order), at most one column a quantity node, a component and a correlation: a few hundred columns × S draws.
- The second pass's `prior(position)` reads **one** quantity's column back (`read_row_group(i, columns=["value"])` sliced as
  the draws are), at most S doubles (80,000 × 8 bytes at the node's maxima): the density chart's prior series, and
  `prior_variance(position) = prior_variance_of(draws, finite)`, with `finite` = the quantity's `prior_marginal` `of` is in
  `bayes.finite_variance` (built: `NO_FINITE_VARIANCE`, `NO_PRIOR_DRAWS` and the counted note `contraction_null`).
- A plate's quantities share their node's representative draws (the units are exchangeable a priori: the diagnostics spec).
  The `of` of a component is `<effect>.<component>`, of a correlation `<effect>.correlation`, of a state the state
  (P2, verified in the handoff and the golden manifests).
- **The Go side**: `bayeschecks.FiniteVariance(m *bayesgraph.Model) []string` returns the `of` of every `prior_marginal`
  whose prior has a finite variance: its distribution's `finite_variance` holds for its constant arguments, or it is
  truncated to a bounded range, **and** every node its arguments read has one (the diagnostics spec's rule). `flow-node`
  calls it on the pinned version's stored graph (`bayesgraph.Decode`, then `BuildModel`) and writes `bayes.finite_variance`.

## The original scale

For each pair of `bayes.scale_pairs` — `{"coef": <the coefficient's Stan column>, "data": <the data's Stan name>}` for a
term, `{"intercept": <the intercept's column or null>, "lp": [<the terms' coefficient columns>], "data": [<their data>]}`
for a linear predictor's intercept — the second pass reads the pair's columns together (one more read of the `value`
column when the block of quantities split them: at most the linear predictor's terms + 1 columns × S doubles), computes the
draws on the column's scale (coef / sd; intercept − Σ coef × mean / sd, the means and sds of pass A's facts), and writes
`orig_mean`, `orig_sd`, `orig_q5`, `orig_q50`, `orig_q95` in the coefficient's `summary` rows and a second row of the
summary output with `scale` `original` (the contract, «The outputs»). Only for the coefficients of a `linear_predictor` node
(owner, 2026-11-23): at most `bayes_max_quantity_entries` (600) entries, the count `MaxResultRows` already holds (X4's
«As built»). `flow-node` writes `bayes.scale_pairs` from the stored graph's `Model.Pairs` and the stored mapping.

## Time and disk

**The clock** starts at `started_unix` (built: `setRunLimits`); the budget is `time_budget_seconds`, which `flow-node` sets
to the step's timeout **minus the time it waited for the compile** (the compile spec: «the step's waiting counts in its own
timeout»).

**The projection** of the time left is the sum of:

| Stage | Projected from |
|---|---|
| Sampling | the iterations left × the time an iteration took so far, warm-up and sampling apart once both are seen (each chain's latest progress line; the slowest chain) |
| Conversion's tail | the CSV bytes not yet read ÷ the follower's measured rate |
| Second pass | the values (S × quantities) × the measured 0.93 µs a value (X5: 74 s for 80 million values on the development machine) until the pass measures its own first row group and sub-block (built: `seconds_left`) |
| Checks' run | `ppc_draws` ÷ the draws a second the sampling run made, × the measured share of a `generate_quantities` draw against a sampling draw (P0: 0.08 of the run at 100,000 rows); replaced by its own rate after its first chain |
| Replicated run | the same with `draws_per_chain` × C |
| Checks' and replicated second passes, the copies | their files' bytes ÷ the measured write rate (the copy) and read rate (the passes) |

It is checked after the first progress lines, at every progress line, at every idle tick (50 ms, built), before each
later stage and at its first measure; `too_slow` when it passes the budget, with the sentence of the contract («about 41
minutes for 4 chains of 2,000 iterations; the node's timeout is 30 minutes: fewer draws, or a longer timeout up to the
group's 60»). The run's facts never wait for a verdict: a projection is arithmetic on what was seen, its refusal a limit.

**The disk** (`tmp_bytes`), projected before sampling with the measured constants and again after the first block of each
chain with the chain's own bytes a draw (`BlockInfo.chain_draw_bytes`, built):

```text
binary + 2 × data.json
+ C × D × (CSV bytes a draw)                 (the sampling run's CSVs, kept to the end: the follower reads them in place)
+ C × D × P × 7.75                           (draws.parquet; prior.parquet likewise with its columns)
+ thinned lines (ppc_draws + C × draws_per_chain) × CSV bytes a draw
+ (ppc_draws + C × draws_per_chain) × generated columns × 10.3 × 2    (their output CSVs and parquets)
+ summary and results (≤ 100,000 rows) + what /tmp already holds
```

`too_much_disk` when it passes `tmp_bytes`, with the sentence of the contract. The sampling run's CSVs are deleted once
CmdStan has exited and the follower has read them to their end, before the second pass: on Linux a file another process
still holds open keeps its space until it is closed, and whether CmdStan closes a chain's file before the others end is
**not known** (R1 looks); so the projection counts every CSV until the exit, and the later stages start with that space
free again.

## Memory

| Stage | Held | Measured |
|---|---|---|
| Data passes | one batch (65,536 rows), the units and levels (≤ `max_units`), the accumulators | K1 |
| Sampling | CmdStan's own (the data, the model); the follower's block (≤ `block_values` doubles = 8 MiB) and the writers' footers | 109.5 MB above the start at 2,000 quantities × 4 chains, flat at × 10 draws (K2) |
| Second pass | `pass_bytes` (`memory_bytes`/4) + `compute_bytes` (/8) + a row group, CmdStan exited | 727 MB above the start at 20,000 quantities × 4,000 draws with 2 GiB (X5) |
| `generate_quantities` | CmdStan's (the data, one draw) and the follower's block | P0: 15.5 MiB at 100,000 rows |
| Copies | one 8 MiB piece | the contract |
| **The whole Job** (sample and prior predictive, with the checks, the replicated output and the original scale) | the stages above, never two at once | R6: 109.5 MB above the start at 2,013 columns × 4 chains × 1,000 draws and 108.3 MB at 10,000 (fake CmdStan, `memory_bytes` 192 MiB); 751.8 MB at 10,000 draws with the Job's 2 GiB (the budgets are the difference); «As built — lot R6» |

The stages never overlap: CmdStan has exited before the second pass, the second pass ended before a `generate_quantities`
run starts. The observed outcome's array (8 bytes a row) is P3a's and is freed before sampling (the checks spec).

## The Job, from the gateway

The sampling Job is a model Job (`modelJobManifest`, verified) with four differences, in one function `bayesJobManifest`
beside it (`internal/api/jobs_bayes.go`), so that no other group's manifest changes (their golden manifests are a test):

- **`/tmp`**: the `emptyDir`'s `sizeLimit` and the task's `tmp_bytes` are the sampling `/tmp` in force
  (`bayesCardInForce(…).SamplingTmp`, default 2 GiB). `modelJobManifest` takes the `/tmp` size as an argument; every
  other caller passes `modelTmpLimit(img)` (the same bytes).
- **Resources**: the group's card (CPU default 4, memory 2 GiB, the longest run 3,600 s, `max_rows` 1,000,000; the image
  spec), as every model Job; `OMP_NUM_THREADS` = `threadsOf(CPULimit)` is CmdStan's `num_threads` (read by `tab_protocol.threads`).
- **Environment**: `TABULARIA_BINARY_IN` = `s3://<scripts bucket>/<step prefix>/model`; `MALLOC_MMAP_THRESHOLD_=262144`,
  `MALLOC_TRIM_THRESHOLD_=262144`; the rest as every model Job (`TABULARIA_INPUT`, `OUTPUT`, `RESULTS`, `ARTIFACTS`, the
  step's ids, `HOME` and `TMPDIR` = `/tmp`, `PYTHONHASHSEED=0`).
- **The step's files** deleted by exact key at the end: `modelStepKeys`' list plus `<prefix>/model`.

**The ConfigMap's room** (measured for this spec on 2026-11-24, the sums of the files `codeFiles` takes): the runner's tree
409,150 bytes (its `py/` 214,871, its `tests/` 193,523), `common/py` 116,901, `common/selftest` 924, `card.schema.json`
18,625, the compact catalogue 121,027: **about 666 KB of the 900 KiB** the size test allows (`groups_test.go`, «a ConfigMap
holds at most 1 MiB»), before this module's files, the catalogue's entry `bayes_model` (lot K0b) and a task of up to
`max_nodes` (200) nodes (the templates' tasks are 9–11 KB). Lot R5 adds the largest task of the corpus to the size test; when
the margin falls under 100 KB the runner's `tests/` leave the ConfigMap (to `groups/bayes/reference/runner_tests/`, outside
`runner/`, as `reference/test_bayes_runner.py` already is: K1's note).

## The task fields this module needs (corrections to the contract)

| Key | Written by | What |
|---|---|---|
| `bayes.series` | `flow-node` | `{by, time}` (the contract's correction 3; Go's `Task` lacks it today, verified: `omitempty`) |
| `bayes.finite_variance` | `flow-node`, through `bayeschecks.FiniteVariance` | the `prior_marginal` `of`s whose prior has a finite variance; `omitempty` |
| `bayes.scale_pairs` | `flow-node`, from `Model.Pairs` and the mapping | the original-scale pairs as Stan identifiers; `omitempty` |
| `bayes.model.program_bytes`, `bayes.model.image` | `flow-node` | the card's `program.bytes` and `binary.image` (`bayes_card.build` takes them as parameters today, default 0 and "": K1's note 8) |

All four are `omitempty`: the other groups' `task.json` keep their bytes (the existing test), and the example tasks of
`bayescontract/testdata/tasks` gain them when the lot that next writes the testdata runs.

## Tech stack

- **Python** 3.14 of the image, `numpy` 2.5.5 and `pyarrow` 26.0.1 only, the standard library (`hashlib`, `subprocess`,
  `os`); no pandas, no SciPy, no `cmdstanpy` (the image spec).
- **Go** 1.27, standard library and `pgx/v5`: `internal/api` (`jobs_bayes.go`), `internal/bayeschecks`
  (`FiniteVariance`), `internal/bayescontract` (the four task fields); no new module.

## Commands

```bash
# the runner's unit tests, outside any image (small data only; -I -B: no __pycache__ in the runner's tree)
uv run --no-project --with pyarrow==26.0.1 --with numpy==2.5.5 \
  python -I -B -m unittest discover -s gateway-go/internal/modelgroups/groups/bayes/reference/runner_tests -v
# the whole Job's memory test, alone (a heavy job for this machine: one at a time, under the heavy-job lock; `spec` for the spec's sizes)
BAYES_RUN_MEMORY_TEST=1 uv run --no-project --with pyarrow==26.0.1 --with numpy==2.5.5 \
  python -I -B gateway-go/internal/modelgroups/groups/bayes/reference/runner_tests/test_run_memory.py -v
# the Go side
cd gateway-go && go test ./internal/bayeschecks/... ./internal/bayescontract/... ./internal/modelgroups/...
cd gateway-go && scripts/test.sh ./internal/api/ -run 'BayesJob'          # on TEST_DATABASE_URL
```

## Project structure

```text
gateway-go/internal/modelgroups/groups/bayes/runner/py/
  bayes_binary.py   fetch_hashing: the binary from TABULARIA_BINARY_IN, its sha256 compared before chmod and exec
  bayes_run.py      run(task, prepared): the modes sample and prior_predictive in order, the sinks, the projections, the copies
  bayes_gq.py       the generate_quantities runs: the thinned lines, the data file by prefix copy, the expected header
  bayes_scale.py    the original-scale pairs in the second pass
  bayes_main.py     (changed) the binary before the data; run → bayes_run.run
  bayes_follow.py   (changed) cmdstan_args gains generate_quantities; `run` removed
  bayes_data.py     (changed) pass B records the offset of the extension's data
  bayes_task.py     (changed) the four task fields read; the prior predictive leaves out a quantity without a prior draw
gateway-go/internal/modelgroups/groups/bayes/reference/runner_tests/
  test_binary.py, test_run.py, test_gq.py, test_scale.py, test_run_memory.py (gated), fake_cmdstan.py (gains generate_quantities)
gateway-go/internal/api/jobs_bayes.go           bayesJobManifest, the step's files, the /tmp in force
gateway-go/internal/api/jobs_models.go          (changed) modelJobManifest takes the /tmp size
gateway-go/internal/bayeschecks/variance.go     FiniteVariance(m)
gateway-go/internal/bayescontract/task.go       (changed) Series, FiniteVariance, ScalePairs, ModelRef.ProgramBytes, Image
```

## Code style

The follower's start, as K2 wrote it (verified, `runner/py/bayes_follow.py`): a person's text never reaches CmdStan's
arguments, every value is a number this function formats.

```python
def cmdstan_args(binary: str | Path, sampler: dict[str, Any], algorithm: str, data: str | Path, out: str | Path, threads: int,
                 init: str | Path | None = None) -> list[str]:
    args = [str(binary), "method=sample", f"num_samples={_number_text(int(sampler['draws']))}"]
    if algorithm == "fixed_param":
        args += ["num_warmup=0", "save_warmup=0", "algorithm=fixed_param"]
```

This module's in the same voice (a sketch; lot R0 writes it): the bytes are hashed as they land, and the file becomes
executable only after the comparison.

```python
def fetch_hashing(source: str, target: Path, expected: str, chunk: int = CHUNK) -> int:
    """Downloads the compiled model `chunk` bytes at a time, hashing as it writes; refuses (binary_hash) before any chmod
    when the digest is not the one Tabularia recorded. The bytes written."""
    digest, total = hashlib.sha256(), 0
    with _open_input(source) as src, open(target, "wb", opener=_private) as out:
        while piece := src.read(chunk):
            digest.update(piece)
            out.write(piece)
            total += len(piece)
    if digest.hexdigest() != expected:
        target.unlink(missing_ok=True)
        raise Refusal("binary_hash", "the compiled model is not the one Tabularia recorded: it is not run", {})
    os.chmod(target, 0o700)
    return total
```

## Testing strategy

**On small data, always** (owner's rule): a few hundred rows, a few quantities, the fake CmdStan; the sizes of the measures
only in the gated memory test and by hand.

- **The binary** (`test_binary.py`): a fake bucket directory; one changed byte → `binary_hash`, the file deleted, `os.chmod`
  and `subprocess.Popen` patched to fail and never called; the right file → mode 0700; a missing address → `task_value`.
- **The whole Job on the fake CmdStan** (`test_run.py`; `fake_cmdstan.py` gains `method=generate_quantities`: it reads the
  fitted files and writes the generated columns, as P0 measured): every step of «The sampling Job, in order» on a template's
  task; the results pass the engine's check for both variants (`backend/app/engine/models/results.py`, imported by the test
  as K1's tests do); the summary output's columns exactly; the draws output byte for byte the same across five runs with
  chains at different speeds; the card holds no draw; `run.json`'s seconds; nothing at the output addresses after a refusal
  injected at each step (binary, data, sampling, each projection, the second pass, each `generate_quantities` run, the
  checks' rows), and `/tmp/run` empty.
- **The projections**: a fake clock and a fake CmdStan slowed on purpose trip `too_slow` at the first progress line, at an
  idle tick, before the second pass and at the checks' run; a small `tmp_bytes` trips `too_much_disk` before sampling and
  after the first block; each leaves nothing.
- **`generate_quantities`** (`test_gq.py`): the thinned draws evenly spaced with the last included; the fitted files hold the
  sampling header and the kept lines byte for byte; the data file equals a pass B written with the run's values (a test
  writes both); the expected header from the mapping's blocks against the fake's output; a NaN draw dropped and counted.
- **The contraction and the original scale** (`test_scale.py`): on fake draws with known coefficients, the original-scale
  summary equals numpy on the whole array; an intercept with three standardized terms; the contraction NULL with its note
  when `finite_variance` omits the quantity; **Go** (`bayeschecks`): `FiniteVariance` on the golden graphs (a Cauchy prior,
  a Student-t with ν 3 and 2, an inverse-gamma with α 2.5, a half-Cauchy truncated to [0, 10], a normal whose scale has an
  inverse-gamma prior with α 1.5).
- **The memory** (`test_run_memory.py`, gated, by hand, alone): a child runs the **whole sample mode** on the fake CmdStan at
  2,000 quantities × 4 chains × 1,000 then 10,000 draws, with the checks' and the replicated runs; `VmHWM` read from
  `/proc/<pid>/status`; accepted when the peak at ten times the draws is within 5 MB of the peak at one time (the contract's
  tolerance), with the allocator and the `MALLOC_*` variables as the Job sets them.
- **Go**: `bayesJobManifest`'s `/tmp`, environment and files; the other groups' golden manifests unchanged; the ConfigMap's
  size with the largest task of the corpus under 900 KiB.
- **On the image, by hand, one heavy job at a time** (lot R6): the ten templates' tasks with their reference data through
  the real runner (the compile mode then the sample mode, in the Job's conditions: read-only root, no network, uid 10001,
  2 CPUs, 2 GiB); the times and peaks of every stage; the prior predictive's cost at 1,000, 10,000 and 100,000 rows (P0 left
  it); the `generate_quantities` arguments assumed above.
- **Regression**: the 167 runner tests of K1–X3 (verified count in X3's handoff), the contract's and the diagnostics' Go
  tests, the other groups' runner hashes and manifests.

## Boundaries

- **Always**: the binary hashed before `chmod` and exec; draws appended while sampling; every file under `/tmp/run` until the
  last check; the projections before each stage; the measures by blocks; the checks and the replicated rows only from
  `generate_quantities` runs on thinned draws; the allocator and the two variables in every sampling Job; units, labels and
  column names as data.
- **Ask first**: a new run of CmdStan; a field of `task.json` beyond the four above; a change to `common/py`; a package beyond
  `numpy` and `pyarrow`; a stage that holds more than its row of «Memory».
- **Never**: a binary executed before its hash; a whole CSV, chain or draws table in memory; a partial result after a refusal;
  a verdict, a threshold or a stop on divergences; draws in the card or in MLflow; a person's text in CmdStan's arguments or
  environment; the replicated rows of every draw.

## Success criteria

1. A binary with one changed byte is never `chmod`-ed nor executed; the step says `binary_hash`.
2. On the fake CmdStan, the sample mode writes results that pass the engine's check for `sample`, the output asked for, a
   card without draws, and the same draws file byte for byte across five runs at different chain speeds.
3. The prior predictive mode writes results that pass the engine's check for `prior_predictive`, no draw output, no card.
4. Every refusal of the contract that a sampling Job can raise has a test that trips it at the stage where it is checked and
   leaves `/tmp/run` empty and nothing at the output addresses.
5. The memory test: the whole Job's peak at ten times the draws within 5 MB of the peak at one time.
6. On the image (R6): the ten templates sample and report within the success criteria of `bayes-templates.md` (their
   reference values within Monte Carlo error), and the times and peaks of every stage are recorded in «As built».

## Lots

| Lot | Objective | Files | Accepted when | Depends on | Agent |
|---|---|---|---|---|---|
| **R0** | The binary: fetch, hash, compare, `chmod`; the dispatcher's order | `bayes_binary.py`, `bayes_main.py`, `test_binary.py` | criterion 1 | — | sonnet |
| **R1** | The sample mode without checks: the sinks, the projections, the second pass with the prior's draws, the rows, the card, `run.json`, the copies | `bayes_run.py`, `bayes_follow.py`, `bayes_data.py`, `test_run.py` | criteria 2 and 4 on a version without checks and with the checks off | R0; the checks spec's P3a (the checks-off values) | **opus** (the owner's memory rule, the order of refusals) |
| **R2** | The `generate_quantities` runs: thinned lines, data file, header, the checks' and the replicated outputs; the arguments checked on the image | `bayes_gq.py`, `fake_cmdstan.py`, `test_gq.py` | criteria 2 and 4 with the checks and the replicated output | R1; P3b | sonnet |
| **R3** | The prior predictive mode; the quantities without a prior draw left out | `bayes_run.py`, `bayes_task.py`, tests | criterion 3 | R1; P3a, P3b | sonnet |
| **R4** | The contraction and the original scale: `FiniteVariance` in Go, the task's fields, the pairs in the second pass | `variance.go`, `task.go`, `bayes_scale.py`, tests | their tests; the contract's caps test unchanged | R1; X4 (built) | sonnet |
| **R5** | The Job from the gateway: `/tmp`, environment, files, the ConfigMap's room | `jobs_bayes.go`, `jobs_models.go`, tests | Go tests; other groups' manifests unchanged | — | sonnet |
| **R6** | The memory test of the whole Job; the trial on the image | `test_run_memory.py`, «As built» | criteria 5 and 6 | R1–R5 | sonnet (by hand, alone) |

R0 and R5 can start at once; R1 waits for P3a; R6 is the only heavy lot.

## Corrections made to the map and to the approved specs (2026-11-24)

Applied on the owner's approval of 2026-11-24, each marked «Correction of 2026-11-24 from the step-5 specs» where it lands:

- **`bayes-contract.md`**: `task.json`'s `bayes` gains `finite_variance`, `scale_pairs`, `model.program_bytes`,
  `model.image` («The task fields»; `series` was there); «The CSV files» gains the `generate_quantities` form of the command;
  `/tmp/run/prior.parquet` (the sampling run's `prior_marginal` columns) beside `checks.parquet`, which holds the checks'
  run's columns only; the `model` section gains `seconds_checks`, `seconds_replicated` and the notes `note_checks_dropped`,
  `note_no_checks`, `note_prior_undrawn`; the `replicated` output gains the column `outcome`; «The arithmetic» says 93,490;
  «Time and disk» counts the `generate_quantities` runs.
- **`bayes-predictive-checks.md`**: lot P3 split into **P3a** (pass A's observations and the extension's values of every
  run) and **P3b** (the second pass over the checks file and the rows); the generate-quantities runs are this module's (R2);
  the `prior_marginal` columns go to `prior.parquet`.
- **The map**, `bayes-runner`'s «Choices»: the sampler's accumulators fed from the batch; the posterior checks from a
  `generate_quantities` run on thinned draws after sampling.

## Decisions of 2026-11-24 (the answers to this spec's questions)

| Question | Decision | By |
|---|---|---|
| The replicated output when the model has more than one checked likelihood | **A column `outcome`** (the observed data's Stan identifier, dictionary-encoded) added to the fixed schema of the `replicated` output, after `row`: `chain`, `draw`, `row`, `outcome`, `value`; `ReplicatedWriter` writes each checked outcome's rows of a draw in one row group | Coordinator, 2026-11-24, on the owner's approval of the spec |
| A checks' run that would pass the timeout | **`too_slow` for the whole step**, before any partial result, as every other stage (answer 10): the same task gives the same report on every machine | Coordinator, 2026-11-24 (the owner's rule on the limits) |
| What 1.5.0 needs (asked in `bayes-flow-node.md`) | **Everything of this spec**: R0–R6, the checks' and the replicated runs, the prior predictive mode and the original scale | Owner, 2026-11-24 |

## As built — lot R4, the Go side

- **`bayeschecks.FiniteVariance(m) []string`** (`variance.go`): the `of` of every `prior_marginal` whose prior has a finite
  variance, in the order of `Analysis.Marginals`; `[]` when none has, `nil` for a model that is not valid. The decision is
  **prudent** (a «yes» is a proof; a «no» may only mean «not proven», and the contraction is then NULL with its note), stricter
  than «every node its arguments read has one», which does not hold for every argument (an exponential whose rate has a
  gamma(1, 1) prior has an infinite variance though the rate's is finite; so has a Student-t whose ν has mass at or below 2):
  - a parameter whose **range is bounded** (a truncation to a bounded range, or a bounded support: the beta, the uniform) is
    finite whatever its parents;
  - otherwise the catalogue's rule: `never` is not finite; `above` needs its argument (ν, α) to be **one number** (a constant, or
    an expression of numbers such as `5 / 2`, read from its static interval) above the bound: **an argument that varies is not
    finite**;
  - every other argument must be one number, or the **location or the scale of a location-scale distribution** (normal,
    Student-t, Cauchy, a component's normal) whose value is finite; any other argument that varies (a rate, a shape, the
    lognormal's μ or σ) is not finite;
  - a value (an argument's expression, a transformed node's value, an intercept-only linear predictor's intercept) is finite
    when it is bounded (its static interval), one number, a node that is finite, or an **affine combination** of these (`+`,
    `-`, negation, a product by a constant, a quotient by a constant); a function, a power, a product or quotient of two values
    that vary is not finite unless its interval is bounded (`inv_logit(b)` is, `exp(b)` and `b * b` are not);
  - a state's marginal is its init's draw (the init's distribution and arguments); a correlation's Ω[2,1] is always finite.
  Tested on the spec's cases (Cauchy; Student-t ν 3 and 2; inverse-gamma α 2.5 and 2; half-Cauchy on [0, 10] and on [0, ∞);
  a normal whose scale has an inverse-gamma α 1.5 prior; the prudent cases) and on every valid graph at hand (an ordered
  subset of the marginals, each a `prior_marginal`'s `of`).
- **`bayeschecks.ScalePairs(m, data, quantities) []bayescontract.ScalePair`** (`scale.go`), in `Model.Pairs`' order: a term
  `{"coef": <the coefficient's quantity, a single value>, "data": <the standardized real's Stan name>}`; an intercept
  `{"intercept": <its quantity's Stan identifier, or null without an intercept slot>, "lp": [<the terms' coefficients>],
  "data": [<their data>]}`. The intercept may be **a quantity over a group plate taken at the row's unit** (`a_g[g]`:
  `"intercept": "a_g"`, each of its elements shifted by the same Σ coef × mean / sd); an intercept that is another
  expression (`mu + a_g[g]`, a constant) has no pair, and neither has a predictor's intercept when one of its standardized
  terms has none (a term's coefficient is always a parameter: model-graph's `coef_not_parameter`). A category's terms are
  never pairs (model-graph's `pairs()`).
- **`bayescontract.Task`**: `FiniteVariance []string` (`finite_variance`, **`omitzero`**: absent when nil, the runner's «not
  decided»; `[]` written when decided and empty), `ScalePairs []ScalePair` (`scale_pairs`, `omitempty`; `ScalePair` is a
  `*TermPair` or an `*InterceptPair`, read by the presence of `lp`); `ModelRef.ProgramBytes` (`program_bytes`) and
  `ModelRef.Image` (`image`), `omitempty`. `Check`: the compile mode refuses `finite_variance` and `scale_pairs`; each
  `finite_variance` is a quantity's node, once; a pair names the manifests' quantities (a term's coefficient and an
  intercept's terms single values) and standardized reals, `lp` and `data` of one non-zero length; `program_bytes` ≥ 0. The
  other groups' `task.json` bytes are unchanged (the field is the group's `bayes` object).
- **The examples** (`bayescontract/testdata/tasks`): `sample.json` gains `finite_variance` (hier_slopes: its eight
  marginals), `program_bytes` and `image`; a new `sample_scale.json` (with its header) is glm_linear with output `draws`,
  `finite_variance` and `scale_pairs` (`b_x`/`x`, and `alpha` shifted by `b_x`). `bayes.series` is not added by this lot
  (`flow-node`'s, lot F2).

## As built — lot R4, the runner

`bayes_scale.py` (`plan`, `read_columns`, `original_scale`), `bayes_task.py` (`Task.scale_pairs`, read and checked: `task_value` for a
pair that is not a term or an intercept), `bayes_pass2.py` (`second_pass(scale=...)`), `bayes_run.py` (the plan from the task's pairs and
pass A's `facts.standardized`); tests `reference/runner_tests/test_scale.py` and `test_task.py`.

- **`plan`** resolves the pairs against the layout (a coefficient must be a single value, a pair's data a standardized real: otherwise
  `ManifestMismatch`, a fault of Tabularia) into term jobs (column, sd) and intercept jobs (the intercept's columns, one for a scalar and
  one a unit for a quantity over a group plate; the terms' columns with mean / sd). A `null` intercept has no row. The cap is
  `bayes_max_quantity_entries` **quantities** (a plate's elements count one each, cut where the cap falls; the rest is the note
  `original_capped`), so the 93,490 rows of the caps' arithmetic hold.
- **`original_scale`** runs after the blocks of the second pass (no diagnostics, so not in the prior predictive mode): the term's draws
  are coef / sd, the intercept's intercept − Σ coef × mean / sd draw by draw (the shift is accumulated over blocks of at most
  `pass_bytes` of coefficient columns, then each block of intercept columns is read, shifted and measured), each block one read of the
  `value` column of every row group. Nothing is kept beyond a block.
- **Rows**: a second row of the summary output per quantity with `scale` `original` (mean, sd, q5…q95; the diagnostics and the
  contraction NULL), and `orig_mean`, `orig_sd`, `orig_q5`, `orig_q50`, `orig_q95` in the `summary` rows of the quantities of the report's
  scope. The declaration (`orig_*` in the summary numbers, 5 rows an entry in `MaxResultRows`) was already in place.
- **Tested** on a regression with a flat prior drawn exactly (a multivariate normal around the least squares on the standardized
  columns): the original-scale mean is the regression made on the raw columns within the Monte Carlo error and its sd within 5%; the
  numbers equal a hand calculation draw by draw to nine places; a vector intercept; no intercept slot; the cap and its note; the budgets
  (one quantity a block, all) do not change a number.

## Open questions

None.

## As built — lot R3

`bayes_run.py` (`begin`, `SampleJob.run_prior`, `run_replicated`), `bayes_task.py` (`Layout.undrawn`), `bayes_pass2.py` (`charts`,
`not_finite_draws`), `bayes_diag.py` (`_measures_of_the_finite`), `bayes_draws.py` (`ReplicatedWriter.dropped`/`kept`); tests
`reference/runner_tests/test_prior.py` and the corrected `test_pass2`, `test_gq`, `test_draws`, `test_run`, `test_task`.

- **`begin`** returns the run `prior` for the mode `prior_predictive` and refuses before the binary is fetched a program without the
  extension or without `Prior_o<k>_n` (`task_value`, «no prior predictive»).
- **`run_prior`**: one CmdStan run (`algorithm=fixed_param`, `num_warmup=0`, `Mode` 1, `Ppc_on` 1) followed into two sinks, `draws.parquet`
  (the prior draws in place of the quantities': `build_layout`, which now leaves out of `draws` and `entries` a quantity without a
  `prior_draw`, recorded in `Layout.undrawn` and said by `note_prior_undrawn`) and `checks.parquet` (as the checks' run's); no
  `FactsAccumulator`, no thinned lines, no prior marginals, no contraction, no card, no output. Then the summary (mean, sd, quantiles:
  `second_pass(diagnostics=False)` with no chart), the `prior_check` rows and `prior_density` curves (`Plan.rows(..., "prior_check")`), the
  notes, `run.json`, and the copy of `results.parquet` only at the end (a refusal before it leaves nothing).
- **The projections** count the checks' file instead of the prior marginals' (disk) and the second pass over the checks (time), before sampling
  and at each progress line, as in `sample`.
- **A non-finite value**: the draws file accepts it in this mode (`measures` over each quantity's finite draws, `note_prior_dropped` with the
  count of draws); the checks' draws are dropped as in `sample`. See the contract's «Correction of 2026-11-24 from R2».
- **`replicated`** (lot R2's open point): a draw with a non-finite value is left out whole, counted in `note_replicated_dropped`; refused
  (`not_finite`) only when no draw is left.

## As built — lot R6

`reference/runner_tests/test_run_memory.py` (new, gated), a correction in `bayes_pass2.py` with its tests in `test_run.py` and `test_prior.py`,
and the trial on the image `tabularia-bayes-b0:local` (CmdStan 2.40.0, `df1cf58a3244`), by hand, one container at a time under the heavy-job
lock: read-only root, `/tmp` a 2 GiB tmpfs, uid 10001, no network, no capability, 2 CPUs, 2 GiB, `PYTHONHASHSEED=0`, `MALLOC_MMAP_THRESHOLD_` and
`MALLOC_TRIM_THRESHOLD_` at 262,144, the code tree as the ConfigMap mounts it (`CodeFiles`), the compile Job then the sampling Job on the
binary it left (`TABULARIA_BINARY_IN`). The tasks are the ten templates' own (`bayescodegen.Generate` with the checks' extension on the
template's graph, `FiniteVariance` and `ScalePairs` of `bayeschecks`, the sampler 4 chains × 2,000 draws after 1,000 of warm-up), the input
the template's reference table of lot T3.

### The memory of the whole Job (criterion 5)

`BAYES_RUN_MEMORY_TEST=spec`, on the fake CmdStan, `hier_intercepts` on 2,000 groups (a vector quantity of 2,000 elements: 2,013 columns, 4,071
in the prior predictive, which draws the prior of each), 4 chains × 1,000 then × 10,000 draws, blocks of 1,048,576 values, `memory_bytes`
192 MiB (budgets 48 and 24 MiB, reached at both sizes). Peak resident memory above the start, in MB; the lower of two runs, then the higher
(the allocator only adds to a peak: two identical runs of the whole Job differed by up to 24 MB, 84.6 and 108.5 at ten times the draws); the
tracemalloc peak of a third run (Python and numpy, which do not vary between identical runs):

| Case | RSS × 1 | RSS × 10 | highest of two, × 1 and × 10 | tracemalloc × 1 | tracemalloc × 10 | seconds × 10 |
|---|---|---|---|---|---|---|
| sample, checks, original scale, output `draws` (217 MB) | 109.5 | 108.3 | 112.8 and 109.7 | 77.2 | 77.7 | 146 |
| sample, checks, original scale, output `replicated` | 109.9 | 111.3 | 110.9 and 131.1 | 77.7 | 78.3 | 146 |
| prior predictive (4,071 columns) | 102.2 | 102.0 | 103.5 and 102.2 | 55.2 | 57.0 | 73 |

All three within the 5 MB (the largest difference of the deterministic measure: 1.8 MB, the prior's: the results writer's keys). With the
Job's own 2 GiB (`memory_bytes`) the whole Job at 10,000 draws peaks at **751.8 MB** above the start (X5: 727 MB for the pass alone): the
second pass's `pass_bytes` 512 MiB and `compute_bytes` 256 MiB are most of it; CmdStan's own memory is the container's, outside this process
(the images' trial below reads the container's peak). The peak above the budgets is about 34 MB at the small budgets and does
not depend on the draws.

**What the test measures is only the growth.** Three sizes that look innocent made the smaller run smaller than the larger by a constant, each
seen: the second pass's block (`pass_bytes` is a cap: at 2 GiB the smaller run holds all of itself, the larger 512 MiB), the follower's block
(under `block_values` a chain's draws are one smaller block) and the copy's piece (a file under 8 MiB is one smaller piece). The test lowers
`memory_bytes` and sets the sizes so that all three are full at both; a profile `small` (800 groups, 600 then 6,000 draws, 8 MiB) runs in six
minutes and measured the same flat tracemalloc. At ten times the draws the draws file has about ten times the row groups (8 then 80 in the spec's profile) and their footers grow the
resident memory (3.5 MB in the small profile, whose blocks are smaller: the cost K2 documented), inside the 5 MB.

### A correction found by the trial: a state on the rows plate

`ts_local_level`'s `level` (a value a row, no unit) crashed the Job in the sample and the prior predictive modes: the second pass wrote
its elements as `summary|level||mean` 200 times and the results writer refused the second row of the key (`RuntimeError`, exit 1: a fault of
the runner, never seen by the fake because no test had a quantity on the rows plate). `bayes_pass2.summary_unit` gives the `summary` rows of
an element with no unit its position, `index` (the row's number the contract gives; «i,j» for a matrix), as `level_2`: `summary|level|17|mean`.
The summary output keeps `unit` null and the position in `index`, as the contract says. Two tests (`test_run`, `test_prior`) run `ts_local_level`
on the fake, red before the correction.

### A defect found and not corrected: `generate_quantities` on a correlated effect

**`hier_slopes` with the checks on (and with the replicated output) is refused by CmdStan: `sampler_failed`, «chain 1: `cholesky_corr_free:
x is not a valid unit vector. The sum of the squares of the elements should be 1, but is 1`»**, at each of the seven seeds tried. A `generate_quantities`
run reads the fitted draws and unconstrains them (`cholesky_corr_free` for `L_eff`, a `cholesky_factor_corr`), and Stan checks the unit vector
within 1e-8; the sampling's CSV holds 8 significant digits, so each element of a row of `L` is rounded by up to 5e-9 of itself and the
squares sum to 1 ± 1e-7. The same program with the checks off samples (`sample_nochecks`: the posterior below); the prior predictive (no
fitted draws) runs. Any model with a `cholesky_factor_corr` (the correlated effects), a simplex or another constrained type with a tight check
would be refused the same way. **Proposed correction** (not made: it changes the owner's decision of 2026-11-24, «8 significant digits, no
`sig_figs`», the contract's «CSV precision» and the engine's report words that say it): the sampling run is given `output … sig_figs=10`
when a `generate_quantities` run is planned (`bayes_follow.cmdstan_args(…, sig_figs)`, passed by `SampleJob` when `gq_runs` is not empty),
the rows `model|||precision` says «10 significant digits» for it, and the engine's `report_texts/bayes_model.json` gains that text in the
five languages. Tried on the image with `sig_figs=10` on `hier_slopes`, the same task: the checks and the replicated runs pass (4.9 and 2.8 s)
and the summary equals the checks-off run's to 8 digits (the largest relative difference 2.7e-8); 10 digits rounds each element by 5e-11 of itself, the sums of squares are within 1e-9
of 1 (ten times inside Stan's tolerance), and the CSV grows by about two bytes a value (10.3 to 12.3: the disk projection measures the first
block's lines, so it follows; this figure is the arithmetic of the digits, not measured). Until it is made, a version with correlated effects must run with the checks off.

### The ten templates (criterion 6)

The comparison is lot T3's: the runner's summary output (mean, sd, `mcse_mean`, `mcse_sd`) against the hand-written program's posterior
(`<id>.expected.json`'s source statistics), z = (run − reference) / √(mcse_run² + mcse_ref²), every comparison of the ten templates Holm-adjusted
together, α = 0.001. **598 comparisons, the smallest adjusted p 1.0 (the smallest raw p 0.0064, `glm_logistic`), all ten pass.** Times are the
container's wall clock (the runner's own seconds in brackets); peaks are the container's cgroup `memory.peak` (which counts CmdStan and the
page cache), sampling 4 × (1,000 + 2,000):

| Template | Compile (s) | Sample (s) | Sampling (s) | Divergences | Max R-hat | Min ESS bulk | Peak (MB) | Replicated run (s) | Prior predictive (s) |
|---|---|---|---|---|---|---|---|---|---|
| glm_linear | 15.8 | 2.3 | 0.11 | 0 | 1.0015 | 7,607 | 77 | 1.8 | 1.8 |
| glm_logistic | 14.8 | 2.3 | 0.23 | 0 | 1.0009 | 4,888 | 68 | 1.8 | 1.8 |
| glm_poisson | 15.3 | 1.8 | 0.13 | 0 | 1.0010 | 4,465 | 60 | 1.8 | 1.8 |
| glm_negbin | 15.3 | 5.4 | 1.17 | 0 | 1.0007 | 6,641 | 77 | 2.8 | 1.8 |
| hier_intercepts | 20.0 | 4.0 | 0.39 | 0 | 1.0028 | 1,851 | 90 | 2.4 | 1.8 |
| hier_slopes (checks off) | 28.2 | 4.4 | 0.78 | 0 | 1.0020 | 1,484 | 106 | refused (above) | 1.8 |
| hier_nested | 21.6 | 6.4 | 1.37 | 169 (2.1%) | 1.0128 | 465 | 105 | 3.3 | 1.8 |
| ts_ar | 16.8 | 2.8 | 0.38 | 0 | 1.0016 | 5,698 | 78 | 2.3 | 1.8 |
| ts_random_walk | 14.7 | 1.8 | 0.08 | 0 | 1.0010 | 6,288 | 67 | 1.8 | 1.9 |
| ts_local_level (not centred) | 17.4 | 18.4 | 6.41 | 11 (0.14%) | 1.0017 | 1,931 | 266 | 7.5 | 2.3 |

The «Replicated run» is the sample mode with output `replicated` (4 chains × 500 draws after 500, 50 draws a chain replicated) and the
prior predictive 4 × 500 draws. The compile Job's peak is 0.8 to 1.3 GB (`hier_slopes` the highest), the binary 2.6 to 2.9 MB. The stages of a
sample run, the runner's own seconds (`run.json`): data 0.01–0.04, warm-up and sampling as above, conversion 0.1–0.7, second pass 0.06–1.6,
checks 0.07–0.2, the replicated run 0.07–0.11. T3's runs had 88 and 63 divergences on `hier_nested` (the hand-written and the generated program) and 14 on `ts_local_level` (the generated
one: the owner left the template uncentred); the divergences of a six-district funnel vary with the seed (this run's replicated run, 4 × 500
draws, had 22), and the posterior agrees all the same.

**The original scale against a regression on the raw columns** (the GLMs: the task's `scale_pairs` on `b_x` and `alpha`; the raw regression is
least squares for `glm_linear`, iteratively reweighted least squares for the others, the negative binomial at the posterior's mean `phi`):

| Template | slope on the original scale (mean ± sd) | raw regression | difference in posterior sd | sd / se, slope and intercept |
|---|---|---|---|---|
| glm_linear | 0.080822 ± 0.006591 | 0.080814 ± 0.006646 | 0.001 | 0.992, 0.990 |
| glm_logistic | 0.10821 ± 0.01411 | 0.10699 ± 0.01397 | 0.087 | 1.010, 1.010 |
| glm_poisson | 0.041714 ± 0.003242 | 0.041769 ± 0.003232 | −0.017 | 1.003, 1.003 |
| glm_negbin | 0.05708 ± 0.003274 | 0.05705 ± 0.003246 | 0.008 | 1.008, 1.007 |

(the intercepts' differences: −0.007, −0.091, 0.014, −0.008 posterior sd). The linear regression is the least squares (its se times
√((n − 2)/(n − 4)), the sd of the flat-prior posterior), within the Monte Carlo error; the others differ by the prior's pull and the posterior's
skewness, under a tenth of a sd. For every template with a pair (the GLMs and the two hierarchical ones with `b_x`) the original slope's mean and sd
are the standardized ones divided by the column's sd to nine digits (a linear map of the draws), and the vector intercepts (`a_g`, `u_s`) have
their original rows, element by element.

### The projections' constants, measured

The constants of `bayes_run.py` «assumed until lot R6»: the CSV's comments, a CSV file's configuration, the adaptation and the timing are 1.4 KB
(11 columns) to 2.0 KB (67 columns) of head and 0.25 to 0.5 KB after the header: the assumed 8 KB and 48 B a column are 3 to 4 times what
CmdStan writes (kept: the projection errs on the side of refusing late); a value is 8.9 to 10.1 bytes of CSV (the 10.3 holds); a summary row
150 to 300 bytes with its footer (256 holds); a results row 14 to 16 bytes (128 is 8 times over, kept); the copy's rate to the bucket
(50 MB/s) is the network's, which a bind mount on the machine does not measure and so stays an assumption. **The original scale's time** is not
in `seconds_left`'s projection, the spec not asking for it: 600 elements of a vector intercept and two coefficients over 8,000 draws add 0.4 to
0.6 s to a second pass of 4.5 s (9 to 13%, 0.08 µs a value against the pass's 0.93), inside the projection's margin.

### The prior predictive's cost by rows (P0 left it)

`glm_linear`'s binary, 4 chains × 250 draws, the input of 1,000, 10,000 and 100,000 rows: 1.8, 1.8 and 6.4 s in the container (sampling 0.02, 0.19
and 2.37 s, the data passes 0.01, 0.04 and 0.29 s), peak 58, 59 and 85 MB: about 24 ns a row-draw of sampling, linear in the rows, nothing held in
proportion (the checks' statistics are accumulated in the program).

