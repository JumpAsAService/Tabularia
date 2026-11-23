# Spec: the prior and posterior predictive checks (`predictive-checks`)

Status: **written and approved by the owner on 2026-11-24.** It specifies the module `predictive-checks` of
`docs/specs/bayesian-editor-map.md` (**the map**), step 3 of its order of construction, after the approved
`bayes-stan-codegen.md` (**the codegen spec**, whose extension point this module fills: its «As built» of lots S2 and
S5), `bayes-contract.md` (**the contract**) and `bayes-prior-preview.md` (**the preview spec**, whose lot P3 waits for
this module's fixtures). Nothing of it is built. Decisions marked **(owner)** are the owner's (the map's «Interview
answers», numbered as there: **the answers**); the rest are this spec's, to confirm; those marked «spec decision,
2026-11-24, owner may revisit» are small choices the owner may change without reopening the module. The owner answered
this spec's question on 2026-11-24 («Decisions of 2026-11-24»), conditioned on lot P0's measure; no question is open.

Words: **verified** = read in this repository's code on 2026-11-24; **measured** = recorded by an earlier lot in the
spec named beside it; **assumed** = from the Stan or CmdStan documentation, not run here: each is closed by the stanc
check of the codegen's CI (`BAYES_STANC`) or by lot P0 on the group's image, as said beside it.

## Objective

Three capabilities with one source of draws — the `generated quantities` of **the same stored program** that samples
the posterior (the map: one program, one binary):

- **The prior predictive check, as a Job only** (answer 13): the program run with Stan's `fixed_param` and `Mode = 1`
  on the development sample's bound rows, from the flow node's panel; ancestral draws of every parameter
  (hyperparameters first: the true marginal) and replicated data drawn from them; shown as **charts** against the
  observed data (the binned densities of the replicated data beside the observed one; histograms of T(y_rep) with T(y)
  marked).
- **The posterior predictive check, as charts only** (answer 12): the same charts from replicated data drawn from the
  posterior draws; **no posterior predictive p-value, no probability, no share** is computed or shown.
- **The golden test of the priors' marginals**: the browser's ancestral draws (`prior-preview`) against Stan's
  `fixed_param` draws of the same graph, by a two-sample Kolmogorov–Smirnov test per quantity (the preview spec, «The
  golden test against Stan»): this module writes the Stan half (the fixtures and the command that makes them).

Through the codegen's typed extension point (names `Ppc_…` and `Prior_…`), never through text; with replicated data
**never all in memory and never written per row** except on request and thinned (answer 14, the owner's memory rule):
the program writes per draw only statistics, bin counts and a few sums; the runner accumulates them and draws the
charts from a second pass over a small file.

Who reads it: `stan-codegen` (calls the extension), `model-store` (the programs it stores hold it), `bayes-runner`
(the runs, the accumulators), `bayes-diagnostics` (the `prior_marginal` columns), `bayes-report` (draws the rows),
`flow-node` (the panel's «Prior predictive check», the output `replicated`).

## Decisions

| Question | Decision | By |
|---|---|---|
| The prior predictive | **Only as a Job**, `fixed_param`, ancestral `_rng` draws in `generated quantities`, `Mode = 1`, the same binary as the posterior; from the flow node's panel on the development sample's bound rows | Owner (answer 13); the map |
| The posterior predictive | **Only charts**: overlaid binned densities, histograms of T(y_rep) with T(y) marked; **no p-value, no probability, no share** | Owner (answer 12) |
| Replicated rows | **Never written per row** by a sampling run; only on request (`replicated`), for a thinned set of draws, in blocks | Owner (answer 14; rule of 2026-11-22/23) |
| The extension in the stored program | **Every program a save writes holds this module's extension** (`Generate(m, bayeschecks.Extension{})`), so a saved version can always run both checks with its stored program; `CodegenVersion` becomes **2** | Spec (follows from the owner's «a saved version always runs with its saved program»), to confirm |
| A version stored by generator 1 (before the extension) | Runs as stored (the owner's rule), **without** predictive checks: the report says «this version was saved before the predictive checks: saving it again adds them»; the panel's «Prior predictive check» is not offered for it | Spec decision, 2026-11-24, owner may revisit |
| Which likelihoods are checked | The first **3** likelihoods in `Model.Order` (`max_check_outcomes`); a fourth and later are said in a note («the checks cover 3 of the model's 4 likelihoods») | Spec decision, 2026-11-24, owner may revisit (the columns' and rows' caps below) |
| The statistics T | By the outcome's kind: **continuous** (`normal`): mean, sd, min, max, q10, q50, q90; **counts** (`poisson_log`, `neg_binomial_2_log`, `binomial_logit`): mean, sd, max, q50, q90, zeros; **binary** (`bernoulli_logit`): mean; and the **mean of each checked unit** of the outcome's first group plate | Spec decision, 2026-11-24, owner may revisit (the map's list: «mean, sd, min, max, chosen quantiles, share of zeros for counts; per group for hierarchical models») |
| The checked units | The **8** units with the most rows (ties: text order), `max_check_units`; the others together in one slot, never drawn | Spec decision, 2026-11-24, owner may revisit |
| The bins of the densities | **Fixed from the observed data** in the runner's pass A (the map): continuous, 40 equal bins between the observed minimum and maximum; counts, bins of a whole width from −0.5 covering the observed maximum in at most 40; binary, the two values; **plus one bin below and one above**, drawn as such | Spec decision, 2026-11-24, owner may revisit |
| The charts | Overlaid densities: the observed one and **20** replicated draws evenly spaced; T(y_rep) histograms of **30** bins with T(y) marked | Spec decision, 2026-11-24, owner may revisit (the caps' arithmetic) |
| Where the posterior checks are computed | **(b) a `generate_quantities` run after sampling on 1,000 thinned draws**, if lot P0 confirms that CmdStan 2.40 reads the follower's thinned CSV; **otherwise (a) inline**, in the sampling run on every draw | Owner, 2026-11-24 (conditioned on P0) |
| The prior's marginal for the report and the diagnostics | One ancestral draw a draw of **each quantity node** (a plate's representative unit), written in **every** run: role `prior_marginal` (new) | Spec decision, 2026-11-24, owner may revisit (the report's prior density, `bayes-diagnostics`' contraction and the golden fixtures need the same draws) |
| A truncated prior's ancestral draw in Stan | **By inversion** (u uniform between the CDF at the bounds, then the quantile), with the quantile Stan has for the distribution; a truncated `gamma` or `inv_gamma` (Stan 2.40 has no quantile for them, assumed) has no ancestral draw: its node has no `prior_marginal`, and the prior predictive is not offered for the model, both said | Spec decision, 2026-11-24, owner may revisit (Stan has no rejection loop the codegen may write: `while` and `if` are refused words, the codegen spec's safety check 1) |
| The golden test | The Stan half: fixtures written **by hand, in the group's image**, committed; the suite never runs CmdStan | The preview spec |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| The extension point | `gateway-go/internal/bayescodegen/generated.go`: `Extension{Data, Generated}`, `Scope{Identifier, Local, ValueWith, Slot, Call, PlateSize, Rows}`, typed `Decl` (`Elem`, `Shape`, `Size`, `Kind`, `Role`, `Check`, `Of`), `Stmt` (`Assign`, `For`, `Declare`; «loops do not nest»), `Expr` (`Num`, `Integer`, `LoopIndex`, `Index`, a `Decl`'s `Ref`), `Size` (`Count`, `AsSize` of an extension's integer, `WhenPrior`: `Mode == 1 ? s : 0`), `extensionPrefixes = {"Ppc_", "Prior_"}`; the extension's data after `Mode`, its written declarations after the correlations, one nested block with the per-row locals; a refusal is `ErrExtension` | Implemented by `internal/bayeschecks`; three additions («What the codegen gains») |
| A test extension and its golden program | `bayescodegen/testdata/golden/extension_fake.stan`: data `Ppc_bins`, `Ppc_edges`; written `Ppc_first_rep`, `array[Mode == 1 ? J_grp : 0] real Prior_a`; the nested block with `Full_bc`, `eta`, `Prior_mu`, `Prior_tau`, a loop drawing `Prior_a[I]`, `array[N] real Ppc_yrep = normal_rng(eta, s)` | The shape this module's programs take; note that the fake draws the half-normal `tau` untruncated, which a real extension must not do («Truncated priors») |
| Splitting of large values | Lot S5: a value the extension asks for (`Slot`, `ValueWith`) has its locals `E_<id>_<k>` written before the statement, in the mapping | Inherited: a long mean is split in the nested block too |
| The manifests' roles | `internal/bayescontract/manifest.go`: `RoleCheck` («to the checks file, never to the draws»), `RolePriorDraw` (replaces `Of`'s columns in `prior_predictive`; in `sample` read and dropped); `Dim{Size}` over an extension's integer; `check.go`: `extensionName` `^(Ppc\|Prior)_[a-z0-9]+(_[a-z0-9]+)*$`, `extensionKind`, an extension's data entry with no `node`; `Header(mode, quantities, sizes)` (an extension's integers among the sizes; `fixed_param` writes two sampler columns, measured by K0) | Used; three corrections («Corrections proposed») |
| The task and its checks | `bayescontract.Task.Checks json.RawMessage` («what predictive-checks asks the runner to accumulate: null, or an object it defines»); `testdata/tasks/prior_predictive.json` | `bayes.checks` defined here |
| The rows | `declaration.go`: sections `ppc` and `prior_check` (`level` required, `level_2` optional, indexed, `observed` unindexed; numbers `observed`, `count`); the curves `ppc_density`, `prior_density` (the contract); limits `max_check_columns` 200, `bayes_max_check_rows` 5,000 | Filled within those caps |
| The catalogue's generators | `internal/bayescatalog/catalog.json`: `normal_rng`, `bernoulli_logit_rng`, `binomial_rng` (the binomial-logit form with `inv_logit`), `poisson_log_rng`, `neg_binomial_2_log_rng`, the priors' `_rng` and `lcdf`/`lccdf`; `internal_functions` = `to_vector`, `rep_vector`, `append_row`, `diag_pre_multiply`, `multiply_lower_tri_self_transpose`, `inv_logit`, `std_normal` («the list belongs to `stan-codegen`, which extends it») | The generators; `internal_functions` extended («What the codegen gains») |
| The store's save | `internal/bayesmodels/save.go`: `bayescodegen.Generate(m, nil)` | `Generate(m, bayeschecks.Extension{})` |
| The browser half | `frontend/composables/bayesPriorCore.ts`: `bayesDrawMarginals(plan, seed, n)` (the preview spec's `drawMarginals`), one representative unit per plate | Compared with the fixtures by the preview's lot P3 |
| The exact KS distribution in numpy | `groups/bayes/runner/py/bayes_stats.py` (one-sample, B0) | The golden test is the preview's (TypeScript, asymptotic two-sample); nothing in the runner |

## What the program holds (the extension)

For each checked outcome k (1…3) of a likelihood whose observed data node is `y` (a sketch for a normal likelihood on
the rows; the golden files `checks_*` pin the exact bytes):

```stan
data {
  …                                            // the model's data, then Mode
  int<lower=0, upper=1> Ppc_on;                // 1 in a run that computes the checks
  int<lower=0> Ppc_o1_n;                       // rows replicated from the posterior (0 or the statement's rows)
  array[Ppc_o1_n] int<lower=1> Ppc_o1_rows;
  int<lower=0> Prior_o1_n;                     // rows replicated from the prior (0 or the statement's rows)
  array[Prior_o1_n] int<lower=1> Prior_o1_rows;
  int<lower=0> Ppc_o1_m;                       // the rows replicated in this run (one of the two above)
  int<lower=0> Ppc_o1_slots;                   // the bins and the two outer ones, 0 when the checks are off
  real Ppc_o1_scale;
  real Ppc_o1_shift;
  int<lower=0> Ppc_o1_units;                   // the checked units and the slot of the others, 0 without a group
  array[Ppc_o1_m] int<lower=1> Ppc_o1_slot;    // each replicated row's unit slot
  int<lower=0> Ppc_o1_rep;                     // the replicated rows written (0, or the rows in a replicated run)
}
generated quantities {
  real Prior_m_1 = normal_rng(0.0, 10.0);      // prior_marginal of mu
  …                                            // one per quantity node; a plate's representative unit
  array[Mode == 1 ? J_p1 : 0] real Prior_alpha_school;   // prior_draw (the prior predictive's parameters)
  array[Ppc_on] real Ppc_o1_mean;              // the statistics, sized by the flag
  …
  array[Ppc_o1_slots] int Ppc_o1_count;        // the bins' counts of this draw
  vector[Ppc_o1_units] Ppc_o1_usum;            // each checked unit's sum (the runner divides by its rows)
  array[Ppc_o1_rep] real Ppc_o1_yrep;          // replicated rows, only in a replicated run
  {
    …                                          // the per-row locals asked for, the prior's ancestral draws
    array[Ppc_o1_n] real Ppc_o1_post = normal_rng(<mean on Ppc_o1_rows>, <sd on Ppc_o1_rows>);
    array[Prior_o1_n] real Prior_o1_draw = normal_rng(<the same with the prior's draws>, …);
    vector[Ppc_o1_m] Ppc_o1_y = to_vector(append_array(Ppc_o1_post, Prior_o1_draw));
    Ppc_o1_count = rep_array(0, Ppc_o1_slots);
    Ppc_o1_usum = rep_vector(0.0, Ppc_o1_units);
    for (I in 1:Ppc_on) {
      Ppc_o1_mean[I] = mean(Ppc_o1_y);
      …                                        // sd, min, max, quantile(Ppc_o1_y, 0.1) …
    }
    for (I in 1:Ppc_o1_m) {
      int Ppc_o1_b = to_int(fmin(fmax(floor((Ppc_o1_y[I] * Ppc_o1_scale) + Ppc_o1_shift), 1.0), Ppc_o1_slots));
      Ppc_o1_count[Ppc_o1_b] = (Ppc_o1_count[Ppc_o1_b] + 1);
      Ppc_o1_usum[Ppc_o1_slot[I]] = (Ppc_o1_usum[Ppc_o1_slot[I]] + Ppc_o1_y[I]);
    }
    for (I in 1:Ppc_o1_rep) {
      Ppc_o1_yrep[I] = Ppc_o1_y[I];
    }
  }
}
```

- **One program, every run.** Which path draws is chosen by **data the runner writes**, never by `if` (a refused word):
  the posterior path's rows (`Ppc_o1_rows`) or the prior's (`Prior_o1_rows`) are the statement's rows or empty, so the
  other path draws nothing; the statistics are sized by `Ppc_on`; the counts by `Ppc_o1_slots`; a loop of 0 or 1
  iteration fills the statistics. A run with the checks off writes **no check column** and draws no replicated value.
- **The prior's path** emits the likelihood's arguments with the prior's draws in place of the parameters
  (`ValueWith`), **on the rows given** (`Prior_o1_rows`): in the posterior run that index is empty, so a plate's prior
  draws, sized 0 there, are never indexed.
- **Bins without a branch**: slot = clamp(⌊y·scale + shift⌋, 1, slots), where the runner sets scale = 1/width and
  shift = 2 − lo/width, so slot 1 is «below the observed range», 2…B+1 the bins, B+2 «above». `zeros` for counts is
  Σ (1 − fmin(y, 1)) in the same loop (counts are whole numbers ≥ 0).
- **Per-unit sums**, not means: the runner knows each slot's rows and divides; a slot's last is «all other units» and is
  never drawn.
- **Prior marginals**: `Prior_m_<k>` (k the node's place in `Model.Order`), one scalar a quantity node: a scalar
  parameter, a plate parameter's representative unit (drawn from its parents' representative draws: the preview spec's
  «One unit stands for its plate»), a correlated effect's component for one unit and one correlation (Ω[2,1]), a state's
  first value. Written in every run (a few scalars a draw).

### Truncated priors (the half-normal trap of the map)

A truncated prior's ancestral draw is u ~ uniform(F(L), F(U)), x = Q(u), with Stan's own functions, as the catalogue's
«Bounds and truncation» rule 3 draws it in the browser:

| Distribution | F | Q |
|---|---|---|
| `normal` | `normal_cdf` | μ + σ·`inv_Phi`(u) |
| `lognormal` | `lognormal_cdf` | exp(μ + σ·`inv_Phi`(u)) |
| `cauchy` | `cauchy_cdf` | μ + σ·tan(π(u − 1/2)) |
| `student_t` | `student_t_cdf` | through `inv_inc_beta` (the Student-t quantile from the regularized incomplete beta's inverse) |
| `exponential` | `exponential_cdf` | −log1m(u)/β |
| `beta` | `beta_cdf` | `inv_inc_beta`(a, b, u) |
| `uniform` | — | L + (U − L)·u (its bounds are its support) |
| `gamma`, `inv_gamma` | — | **none in Stan 2.40** (assumed): no ancestral draw when truncated inside its support |

The functions' existence in Stan 2.40 and their precision in the tails are **assumed**: the stanc check closes the
names, lot P0's fixtures close the values (the golden test compares them with the browser's exact inversion). The
catalogue gains a field `stan.quantile` with these forms (a correction to an approved spec).

## What the codegen gains (lot P1)

1. **`Scope.SlotOn(key, ids, rows)`** and **`Scope.ValueOn(node, ids, rows)`**: a slot's or a node's value emitted **on
   the rows an extension's index array names** (positions among the statement's rows), as `Emit`'s `RowsSubset` already
   writes a lagged statement's rows (the codegen spec, «What exists»). A lagged likelihood's rows are `Rows_lag`.
2. **`Add(a, b)` and `Mul(a, b)`**: two typed constructors of `Expr`, fully parenthesized, the only arithmetic an
   extension writes; the alphabet and the refused words do not change.
3. **`internal_functions`** gains `mean`, `sd`, `min`, `max`, `quantile`, `floor`, `fmin`, `fmax`, `to_int`,
   `append_array`, `rep_array`, `inv_Phi`, `inv_inc_beta`, `tan`, `log1m`, `uniform_rng` and the priors' `_cdf` names
   — each checked by stanc's `--info` allow-list (`CrossCheck`) like every call. That `quantile` and `to_int` accept
   values that depend on parameters **inside `generated quantities`** (stanc types every expression there as data:
   assumed from stanc3's typing of that block) is closed by the stanc check of the golden files.
4. **`CodegenVersion` = 2**, and new golden cases `checks_<template>` for the ten templates and `checks_truncated`,
   `checks_lagged`, `checks_counts`, `checks_three_outcomes`; the golden cases without an extension are unchanged.

## The runs

| Run | Sampler | `Mode` | `Ppc_on` | Rows of the posterior path | Rows of the prior path | `Ppc_o<k>_rep` |
|---|---|---|---|---|---|---|
| Prior predictive | `fixed_param`, **4 chains × 250 draws** (`bayes_prior_draws` 1,000) | 1 | 1 | none | all the statement's rows | 0 |
| Sampling | NUTS | 0 | 0 (1 if P0 falls back to inline) | none (all if inline) | none | 0 |
| Checks after sampling (the owner's (b), 2026-11-24) | `generate_quantities` on **`bayes_ppc_draws` (1,000) draws** thinned evenly over the chains | 0 | 1 | all | none | 0 |
| Replicated (output `replicated`) | `generate_quantities` on `bayes.replicated.draws_per_chain` draws a chain | 0 | 0 | all | none | the rows |

- **`generate_quantities`** (CmdStan's standalone generated quantities, `method=generate_quantities
  fitted_params=<file>`): the follower writes the thinned draw lines of each chain, as CmdStan wrote them, into
  `/tmp/run/fitted_<c>.csv` while it converts the chain (a line kept, never a whole file), and the runner calls the
  binary once a chain after sampling with a data file that differs only by the flags above. That CmdStan 2.40 accepts such
  a file, and the header it writes, are **assumed**: lot P0 reads both on the pinned CmdStan.
- **The prior predictive** is offered on the flow node's panel only for a version stored with generator 2 and a model
  without a truncated `gamma` or `inv_gamma`; its rows are the development sample's bound rows (the predictors matter:
  the map); the results' variant is `prior_predictive` (the contract).

## The runner's side

**Pass A** (the contract's data pass) gains, for each checked outcome: its minimum and maximum (the bins), Welford's mean
and sd, its zeros, the rows of each unit of its group plate (dictionaries bounded by `max_units`, as the contract's).
After pass B the **observed outcome is held once** as one float64 array (8 bytes a row: 8 MB at the group's `max_rows`
of 1,000,000 — the data's exception of the contract, since Stan holds the same values) for its quantiles, its binned
density and the checked units' means; it is freed before sampling starts.

**While a run converts its CSV** (the contract's follower): the check columns go to `/tmp/run/checks.parquet` (long,
the draws' schema, at most `max_check_columns` columns), the `prior_marginal` columns to the same file under their
names; nothing is accumulated in memory but the counts of rows written.

**The second pass over the checks file** (after the run; at most 200 columns × 80,000 draws = 128 MB at the most
chains and draws the node allows, read by blocks of columns like the draws' second pass):

| Rows | How |
|---|---|
| `ppc\|<y>:<stat>\|\|observed` | T(y) of the observed array (mean, sd, min, max, type 7 quantiles, zeros) |
| `ppc\|<y>:<stat>\|\|count\|k` with `x0`, `x1` | the statistic's draws in 30 equal bins between the smallest and largest of its draws and T(y) |
| `ppc\|<y>:unit_mean\|<unit>\|observed`, `…\|count\|k` | the same for each checked unit's mean (its sum over its rows) |
| `curve\|ppc_density:<y>\|observed\|value\|k`, `…\|draw_<j>\|value\|k` | the observed and 20 replicated draws' densities (count / (rows × width)) at the bins' middles, the outer bins at the range's edges |
| the same with `prior_check` and `prior_density` | in the prior predictive |

`<y>` is the outcome's Stan identifier (the key's parts joined by `|`, as the engine's check wants them: the contract,
correction of 2026-11-24). **The arithmetic**, per outcome: 7 statistics ×
31 + 8 units × 31 + 21 series × 42 points = **1,347** rows, three outcomes **4,041**, under `bayes_max_check_rows`
(5,000); columns per outcome at most 7 + 42 + 9 + 1 = 59, three outcomes 177, under `max_check_columns` (200).

**`bayes.checks`** in `task.json` (the contract's place): `{"format": 1, "on": true, "outcomes": 3, "value_bins": 40,
"stat_bins": 30, "density_draws": 20, "max_units": 8, "ppc_draws": 1000}` — the constants travel in the task, so the
runner never guesses them; which columns are which is read from the manifests («Corrections proposed»: a check names
its outcome).

## The golden test of the priors' marginals (the Stan half)

- **The cases**: the ten templates and the codegen's free cases with hyperpriors, truncations and correlations
  (`free_truncated_hyper`, `free_explicit_noncentred`, `free_category`, `hier_slopes_centred`): the preview spec's list.
- **The command**, by hand, one heavy job at a time: `go run ./cmd/bayesfixtures prepare <dir>` writes, for each case,
  the program generated with this extension and a **minimal data file** (each group plate 2 units, a category 2 levels,
  the rows `max_lag + 3`, values 0 or 1 by kind, `Mode = 1`, the checks off); `gateway-go/scripts/bayes-prior-fixtures.sh <dir>`
  compiles and runs each program in the group's image with the self-test's isolation (read-only root, no network, uid
  10001), `fixed_param`, 4 chains × 1,000 draws, a fixed seed; `go run ./cmd/bayesfixtures collect <dir>` reads the CSVs
  line by line and writes `frontend/tests/fixtures/bayes-prior/<case>.json`: the header (CmdStan's version, the seed, the
  program's sha256) and, for each quantity node, its 4,000 `Prior_m_<k>` draws as CmdStan wrote them (8 significant
  digits, CmdStan 2.40.0's default).
- **The comparison** is the preview's (`bayes-prior-golden.test.mjs`, its lot P3): two-sample KS per quantity,
  Holm-adjusted over the suite, alpha 0.001.
- **Size**: about 14 cases × 6 quantities × 4,000 draws × 9 bytes ≈ **3 MB** of fixtures (estimated, not measured).

## Measure first (lot P0, on the image, by hand, alone)

The map's «Measure first»: the added time of the generated quantities **at 1,000, 10,000 and 100,000 rows**, on the
hierarchical logistic template and the linear one, 4 chains × 1,000 draws:

1. the sampling run with the checks **inline** (`Ppc_on` 1) against the same run with them off — the added seconds;
2. the checks in a **`generate_quantities` run** on 1,000 thinned draws — its seconds, and whether CmdStan 2.40 accepts the
   follower's thinned CSV and what header it writes;
3. the prior predictive at the same sizes;
4. the stanc and run behaviour the spec assumes: `quantile` and `to_int` on parameter-dependent values in `generated
   quantities`, `append_array` and multi-indexing with an empty array, `inv_Phi`, `inv_inc_beta`, the `_cdf` names.

The numbers go to «As built»; point 2 decides between the owner's (b) and the fallback (a) (2026-11-24).

## Words (for `bayes-report`)

Every chart says what it shows and never whether the model «passes» (the map): «The grey lines are 20 data sets the
model draws from the posterior; the black line is the data. Where the black line runs outside the grey ones, the model
does not reproduce that feature of the data.» No p-value, no probability, no share is written beside a histogram of
T(y_rep); the outer bins say «below / above the observed range» and how many replicated values fell there **in the
drawn sets**, a count of the chart, never a share of the posterior. The literature: Gabry, Simpson, Vehtari, Betancourt,
Gelman (2019) for the workflow and the prior predictive; the Stan User's Guide, «Posterior predictive checks»; and, for
why no p-value is shown (answer 12), Meng (1994), Gelman, Meng and Stern (1996), Bayarri and Berger (2000) — the map's
«Literature».

## Tech stack

- **Go** 1.27: `internal/bayeschecks` (new: the `Extension`), `internal/bayescodegen` (lot P1's additions),
  `cmd/bayesfixtures` (new: the fixtures' command); no new module.
- **Python** 3.14 of the image, `numpy` and `pyarrow` only: `runner/py/bayes_checks.py` (pass A's additions, the
  second pass over the checks file, the rows).
- **Shell**, by hand: `gateway-go/scripts/bayes-prior-fixtures.sh` (the image, as the self-test runs it).

## Commands

```bash
cd gateway-go && go test ./internal/bayeschecks/... ./internal/bayescodegen/...
cd gateway-go && BAYES_STANC=<stanc 2.40.0> BAYES_STANC_REQUIRED=1 go test ./internal/bayeschecks/... -run Stanc
uv run --no-project --with pyarrow==26.0.1 --with numpy==2.5.5 \
  python -m unittest discover -s gateway-go/internal/modelgroups/groups/bayes/runner/tests -p 'test_checks*.py' -v
# the fixtures, by hand, one heavy job at a time (about 14 compiles of 10–30 s)
cd gateway-go && go run ./cmd/bayesfixtures prepare <dir>
gateway-go/scripts/bayes-prior-fixtures.sh <dir>
cd gateway-go && go run ./cmd/bayesfixtures collect <dir>
cd frontend && node --experimental-strip-types --no-warnings=ExperimentalWarning --test tests/bayes-prior-golden.test.mjs
```

## Project structure

```text
gateway-go/internal/bayeschecks/
  extension.go       Extension: the outcomes, the statistics, the data, the written declarations, the nested block
  priors.go          the ancestral draws (prior_draw, prior_marginal), truncation by inversion
  outcomes.go        which likelihoods, their kind, their rows, their group plate
  *_test.go, testdata/golden/checks_*.stan (with their manifests)
gateway-go/internal/bayescodegen/   generated.go (SlotOn, ValueOn, Add, Mul), CodegenVersion 2, golden checks_*
gateway-go/internal/bayescatalog/catalog.json   internal_functions, stan.quantile (via the catalogue's snapshot tool)
gateway-go/internal/bayesmodels/save.go         Generate(m, bayeschecks.Extension{})
gateway-go/cmd/bayesfixtures/                   prepare, collect
gateway-go/scripts/bayes-prior-fixtures.sh
gateway-go/internal/modelgroups/groups/bayes/runner/py/bayes_checks.py
gateway-go/internal/modelgroups/groups/bayes/runner/tests/test_checks.py
frontend/tests/fixtures/bayes-prior/<case>.json  (the preview's P3 reads them)
```

## Code style

The extension's names and the mode's sizes as lot S2 wrote them (verified, `bayescodegen/generated.go`):

```go
// extensionPrefixes are the prefixes of an extension's names: no identifier of the program has
// them (a person's name starts with a lower-case letter; the generator's own are N, J_, K_, Up_,
// N_lag, Rows_*, Lag<k>, N_first, N_next, Prev, Mode, V_, Z_, L_, W_, Full_, Arg<k>_, Omega_, I).
var extensionPrefixes = []string{"Ppc_", "Prior_"}
```

```go
// WhenPrior is the size in the prior predictive (Mode 1), 0 in the posterior.
func (s Size) WhenPrior() Size {
	s.prior = true
	return s
}
```

This module's in the same voice (a sketch; lot P2 writes it): the bin of a replicated value, written only with the
package's constructors, never with text.

```go
// binSlot is clamp(floor(y * scale + shift), 1, slots): slot 1 below the observed range, the last
// above it, the bins between. No branch: Stan's if is a word the extension never writes.
func binSlot(s bayescodegen.Scope, y, scale, shift bayescodegen.Expr, slots bayescodegen.Size) (bayescodegen.Expr, error) {
	scaled, err := bayescodegen.Add(bayescodegen.Mul(y, scale), shift)
	if err != nil {
		return bayescodegen.Expr{}, err
	}
	low, err := s.Call("fmax", must(s.Call("floor", scaled)), one)
```

## Testing strategy

On small data only: the templates' graphs, a few hundred rows, fake CSVs.

- **Go, the extension** (`bayeschecks`): every template and free case gives a program with the extension that passes
  `CheckProgram`, `CheckIdentifiers` and the scan; the same bytes twice and after the graph's JSON is shuffled; golden
  `checks_*` bytes; the manifests: every check names its outcome, every `prior_marginal` its node, the sizes are
  extension integers; a truncated `gamma` gives no `prior_marginal` for its node and the model's «prior predictive
  available» is false; at most 3 outcomes and 200 check columns for the largest graph of the corpus.
- **Go, stanc** (`BAYES_STANC`, as the codegen's lot S3): every `checks_*` program and 300 generated ones with the
  extension pass `stanc --info` and `CrossCheck`.
- **Python** (`test_checks.py`, the contract's `fake_cmdstan.py` writing check columns): T(y) against numpy on the
  observed array; the bins' scale and shift put each observed value in its bin and the outer values in the outer slots;
  the units' means from sums and rows; the histograms' edges hold T(y); the rows and keys exactly; the caps'
  arithmetic; **no row holds a p-value, a probability or a share** (a test scans the stats' names and texts);
  a run with the checks off writes no check row.
- **The memory**: the contract's test with the replicated output (1,000 rows, 50 then 500 thinned draws a chain) and
  the checks file (flat within 5 MB at ten times the draws).
- **On the image** (lot P0, by hand): «Measure first»; a prior predictive of the hierarchical template on 600 rows;
  the posterior checks of the linear template where the replicated mean's histogram holds the observed mean.
- **The golden test**: the preview's P3 with these fixtures, every quantity of every case passing at alpha 0.001 after
  Holm.
- **Regression**: the codegen's golden cases without an extension unchanged; the store's tests with programs that now
  hold the extension (their golden sha256 written again with `CodegenVersion` 2, reviewed); the templates' tests.

## Boundaries

- **Always**: the extension through typed values only; one program for every run, chosen by data; no replicated row
  written except on request and thinned; the observed outcome's array freed before sampling; no p-value, probability or
  share anywhere; every cap in the arithmetic's test.
- **Ask first**: another statistic, another number of bins, units, draws or outcomes; a new kind of row; a second binary
  for the prior; anything the alphabet or the refused words of the codegen would have to admit.
- **Never**: `if`, `while`, `print`, `reject` or a person's text in the program; a prior predictive in the browser
  (answer 13); a posterior predictive p-value or probability (answer 12); y_rep of every draw on disk or in memory; a
  truncated prior drawn untruncated.

## Success criteria

1. Every saved template's program holds the extension, compiles in the image within the compile limits, and runs as the
   posterior (checks off), the prior predictive and the checks, with the same binary.
2. A prior predictive of the hierarchical template on the development sample writes `prior_check` rows and densities, no
   draw, no replicated row, within the caps.
3. A sampling run writes the `ppc` rows and the overlaid densities as the owner's answer of 2026-11-24 says (after P0), and no p-value,
   probability or share.
4. The output `replicated` holds the requested thinned draws × rows, written in blocks, the memory flat.
5. The fixtures exist for every case and the preview's golden test passes every quantity (Holm, alpha 0.001).
6. The measures of «Measure first» are recorded.

## Lots

| Lot | Objective | Files | Accepted when | Depends on | Agent |
|---|---|---|---|---|---|
| **P0** | The measures on the image; the assumptions of `generate_quantities`, `quantile`, `to_int`, `inv_inc_beta` | «As built» | recorded | `bayes-image` B0; `bayes-contract` K3 | sonnet (by hand, alone) |
| **P1** | The codegen's additions: `SlotOn`, `ValueOn`, `Add`, `Mul`, the functions, `CodegenVersion` 2 | `bayescodegen/*`, `catalog.json` | golden and stanc green | — | **opus** (the safety of the output) |
| **P2** | The extension: outcomes, statistics, bins, units, prior draws and marginals, truncation | `bayeschecks/*`; `save.go` | Go tests and stanc green | P1 | **opus** (the safety of the output) |
| **P3** | The runner's side: pass A, the checks file, the second pass, the rows, the generate-quantities runs | `bayes_checks.py`, tests | Python tests green | P2; the contract's K1, K2; P0 (which of the owner's (b) or (a)) | sonnet |
| **P4** | The fixtures and their command; the preview's P3 unblocked | `cmd/bayesfixtures`, `gateway-go/scripts/bayes-prior-fixtures.sh`, fixtures | criterion 5 | P2; the image | sonnet (by hand, alone) |

P0 and P1 can start at once; P0 and P4 are heavy.

## Corrections made to the map and to the approved specs (2026-11-24)

Applied on the owner's approval of 2026-11-24, each marked «Correction of 2026-11-24 from the step-4 specs» where it
lands:

- **`bayes-stan-codegen.md`**, «The extension point»: `Scope` gains `SlotOn` and `ValueOn`, `Expr` gains `Add` and `Mul`;
  `internal_functions` is extended (the list above); **`CodegenVersion` 2**, the stored programs of generator 1 keep running
  without checks. And the test extension's draw of the half-normal `tau` (`normal_rng(0.0, 1.0)`) is untruncated, which a
  real extension must not copy.
- **`bayes-model-store.md`**, «Saving a graph», step 3: `Generate` with this module's extension; the golden sha256 of the
  templates' saved programs change once.
- **`bayes-contract.md`**: a role **`prior_marginal`** (`of` its node; to the checks file in every mode, read by
  `bayes-diagnostics`); a **check names its outcome** in `of` (the observed data node's id; `checkQuantity` refuses `of`
  on a check today, verified); an **extension's data entry may name its outcome** in `node` (`checkDataEntry` refuses it
  today, verified); the `ppc`/`prior_check` level is `<outcome>:<statistic>` and the density chart `ppc_density:<outcome>`;
  `bayes.checks` is the object above; the `replicated` output comes from a `generate_quantities` run (the contract's
  «assumed: its spec»).
- **`bayes-distribution-catalog.md`**: a field `stan.quantile` per prior (the inversion forms above, none for `gamma` and
  `inv_gamma`); and a field `finite_variance` (the owner's answer to `bayes-diagnostics`' question).
- **`bayes-prior-preview.md`**, «The golden test against Stan»: the browser half is `bayesDrawMarginals` (verified name);
  the Stan half's draws are the `Prior_m_<k>` columns; a case with a truncated `gamma` or `inv_gamma` has no Stan half.
- **The map**, `predictive-checks`: the statistics, bins and units are fixed here; «Measure first» is lot P0.

## Decisions of 2026-11-24 (the answers to this spec's questions)

| Question | Decision | By |
|---|---|---|
| Where the posterior predictive checks are computed | **(b) a `generate_quantities` run after sampling on 1,000 thinned draws** (`bayes_ppc_draws`), the same mechanism as the `replicated` output, **if lot P0 confirms that CmdStan 2.40 reads the follower's thinned CSV**; **otherwise (a) inline**, in the sampling run on every draw. P0 records which in «As built» | Owner, 2026-11-24 |

## As built — lot P1 (the codegen's additions)

`gateway-go/internal/bayescodegen` (`generated.go`, `model.go`, `codegen.go`), `internal/bayesexpr/emit.go`,
`internal/bayescatalog` (`catalog.go`, `declare.go`, the snapshot and the interface's copy), with their tests.

- **`Scope.SlotOn(key, ids, rows)` and `Scope.ValueOn(node, ids, rows)`.** `rows` is an `Expr`: the `Ref` of an array of
  integers of the extension's **data** (anything else is `ErrExtension`: a written or local array, a program's identifier,
  an indexing, reals). Every value over the statement's plate is taken at those positions among the rows the statement
  runs on: `x[Ppc_o1_rows]` on every row, `x[Rows_lag[Ppc_o1_rows]]` on a lagged statement, `lag(x, k)` as
  `x[Lag<k>[Ppc_o1_rows]]`, an index and its lift at the same positions, a split value's locals (`E_<id>_<k>`) assigned
  and read there; a single value stays one (the extension repeats it with `rep_vector` when it needs one a row). A slot of
  a single value (no plate) and a node without a plate are refused. `bayesexpr.Emit` accepts as `RowsSubset` and
  `LagArray` an identifier indexed by one, and nothing more (`checkRows`).
- **Values of the prior's draws never read the posterior's** (a choice of this lot, in the spirit of «Truncated priors»):
  with `ids`, a value per row that reads a node `ids` answers (directly or through what it reads) is rebuilt with `ids` on
  the same rows in a local of its own (`vector[N] E_<id>_<k>; E_<id>_<k>[rows] = …;`, in the mapping table as
  `internal`, once per value asked for); any other node that reads one (a parameter whose prior reads it, a transformed
  parameter, a component, a state) and that `ids` does not answer is `ErrExtension` («ids must answer it too»). So the
  prior's path of a lagged likelihood reading a value without lag (`eta` read by `m = eta + phi * lag(y, 1)`) is written
  `E_eta_1[Rows_lag[Prior_o1_rows]] = …; E_m_1[Rows_lag[Prior_o1_rows]] = (E_eta_1[…] + (Prior_phi * (y[Lag1[Prior_o1_rows]])));`.
  A draw standing for a parameter over a plate must be a `vector` (`vector[Mode == 1 ? J_grp : 0]`), as the parameter is:
  an array does not add to a vector (stanc refuses it).
- **`Add(a, b)` and `Mul(a, b)`** return `(Expr, error)` as `Index` does (the sketch of «Code style» passes `Mul`'s result
  directly: P2 wraps it): `(a + b)`, `(a * b)`, refused when an operand is no value; their result is no variable (not
  indexed, not assigned). A difference is `Add(a, Mul(Num(-1), b))` (`((-1.0) * b)`), a quotient a product with `inv`.
- **The extension's statements**: a local declared in a loop is read only in that loop (`ErrExtension` otherwise, before
  stanc); an extension's data may not take a kind of the program's own (`size`, `real`, `mode`, `lag`…: the contract's
  check refuses them, now the codegen first).
- **The functions.** stanc 2.40.0 refuses `normal_cdf(x, mu, sigma)`: «Probability functions with suffixes _lpdf, …, _cdf,
  _lcdf and _lccdf, require a vertical bar (|) between the first two arguments» (measured), and `|` is outside the
  program's alphabet («Ask first»: not admitted). **No `_cdf` name is in `internal_functions`**; the cumulative
  distributions of «Truncated priors» are written with functions without a bar: `Phi` (normal, lognormal), `atan` (Cauchy),
  `inc_beta` (beta; Student-t with `step` for the sign), `expm1` (exponential, a function of the language), and `inv` for
  a division. `internal_functions` gains, after `std_normal`: `mean`, `sd`, `min`, `max`, `quantile`, `floor`, `fmin`,
  `fmax`, `to_int`, `append_array`, `rep_array`, `inv_Phi`, `inv_inc_beta`, `tan`, `log1m`, `uniform_rng`, `Phi`, `atan`,
  `inc_beta`, `step`, `fabs`, `inv`. They are names a person's node no longer takes (renamed as every callable name is).
  stanc 2.40.0 types `quantile`, `to_int(fmin(fmax(floor(…), 1.0), n))`, `append_array` with an empty array and
  multi-indexing on parameter-dependent values in the generated quantities (measured on the golden programs below).
- **`stan.quantile`** (the catalogue's correction 1): `inv_Phi` (normal), `exp_inv_Phi` (lognormal), `tan` (Cauchy),
  `inv_inc_beta_t` (Student-t), `inv_inc_beta` (beta), `log1m` (exponential), `linear` (uniform); none for `gamma` and
  `inv_gamma`. The F and Q of each form, in the functions above, are written beside the type `bayescatalog.Quantile`;
  `QuantileFunctions` lists them and a test checks that a program may call each. `finite_variance` is not added here
  (`bayes-diagnostics`' lot X4 reads it).
- **`CodegenVersion` = 2.** The golden programs without an extension, and `extension_fake`, are unchanged byte for byte;
  every golden manifest records `codegen_version` 2. The store's programs (`save.go`, still `Generate(m, nil)`) are the
  same bytes until P2. The example tasks of `bayescontract/testdata/tasks` keep `codegen_version` 1 (the runner's tests read
  them as they are; P3 moves them).
- **Golden cases** (with `checksExtension`, the test extension of `checks_ext_test.go`, the shape of «What the program
  holds» written with the primitives only; P2's `checks_*` replace it): `extension_checks` (the graph of
  `hier_intercepts`: a group's levels drawn in a loop into a vector sized by the mode, a half-normal by inversion with
  `Phi`, `inv` and `inv_Phi`, `eta` rebuilt on the prior's rows), `extension_checks_counts` (a binary outcome with a single
  argument repeated, a negative binomial, a binomial through `inv_logit`; a fourth likelihood left out),
  `extension_checks_lagged` (the lagged likelihood above). The test extension loops the bins over a data size of their
  own (`Ppc_o<k>_binned`), not over `Ppc_o<k>_m`: in the `replicated` run of «The runs» (`Ppc_on` 0, the slots 0, the rows
  replicated) a loop over the replicated rows would index `Ppc_o<k>_count[0]`; P2 decides.
- **Checked**: stanc 2.40.0 (`--info`, `CrossCheck`, `--o`) on the 26 golden programs and on 900 generated ones (300 seeds,
  without an extension, with `ppcExtension`, with `checksExtension`); the properties' 500 graphs with `checksExtension`
  reach a prior's path (135), a value rebuilt on the rows named (63), a lagged statement's positions (134), a lag at the
  positions (75), a split value on the rows named (115); `FuzzGenerate` 2 minutes (`-parallel 2 -fuzzminimizetime=2s`,
  the three extensions): 18,043 executions, no failure.

## Open questions

None.
