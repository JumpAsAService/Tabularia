# Spec: customer lifetime models («buy till you die»)

Status: **approved by the owner 2026-11-20 with all seven recommendations of «Open questions for the owner» (each marked «Decided 2026-11-20»). This file is not committed.** Lot B0 is being built. It adds one
group of statistical models to the architecture of `docs/specs/statistical-models.md` (called
**the models spec** below), which it reuses and does not repeat: a model group built only when an
administrator enables it, its recipe and self-test (exit 3 a refusal, 4 a failed self-test),
`RunnerHash`, the Job on Kubernetes with its parquet input, the ten columns, the layouts and
`report_texts`, the card with its sha256, MLflow logged by the Go gateway only, the Predict node.
Where this file says nothing, the models spec holds. Decisions marked **(owner)** are the
owner's of 2026-11-20; the rest are this spec's, and the ones the owner should look at are in
«Open questions for the owner». The lots that build it are «The lots B0–B10».

Words, as in the models spec: **verified** = read in this repository's code on 2026-11-20;
**measured** = run on 2026-11-20 in a scratch container (`tabularia-models-survival:dev`, R 4.6.0,
Ubuntu 24.04, with GSL 2.7.1 added and the packages below installed from Posit's binaries into a
scratch library; nothing installed on the machine, no image built), with the numbers given;
**checked online** = read on CRAN's pages on 2026-11-20; **assumed** = from the literature or the
packages' documentation, not run. The scripts and their output were
kept outside the repository.

## Objective

A person with a table of customers wants to know, for each one: how many purchases to expect in
the next months, whether the customer is still «alive», what each purchase is worth on average,
and the customer's discounted value over a horizon, the **customer lifetime value** (CLV). The
«buy till you die» models answer this from three numbers per customer (how many repeat
purchases, when the last one was, how long the customer has been known), with no covariate and
no churn label: they are the standard for **non-contractual** settings, where a customer leaves
without saying so (a shop, a web store, a donor).

They drop a node after a step that yields one row per customer, choose the columns and the time
unit, press **Run**, and get the per-customer scores as rows, the Report (the parameters, the fit
against the data, P(alive), the spend model and its key assumption, the CLV's distribution and
its top customers, a holdout check when the data has one), MLflow logging, and a registered
version that a Predict node applies to the summaries of other customers.

## Decisions

| Question | Decision | By |
|---|---|---|
| Models of the first lot | **The full set**: Pareto/NBD, BG/NBD and MBG/NBD (expected transactions and P(alive), continuous time); BG/BB (discrete opportunities); Gamma–Gamma (spend); with them, the discounted CLV | Owner |
| Language, library | **R, CLVTools**, a new modular R group | Owner |
| Input | **The ready-made summary**, one row per customer: id, repeat frequency, recency (t_x), age (T), average value of the repeat purchases; for BG/BB x, t_x and n opportunities. The node does **not** compute the RFM summary from transactions | Owner |
| Output | Per customer: expected transactions over the node's horizon, P(alive), expected average spend (Gamma–Gamma), discounted CLV with the node's rate and unit and an optional margin; a holdout check in the report when the summary holds the holdout purchases | Owner |
| The group | `customer_lifetime` (label «Customer lifetime»), R, one image, CLVTools 0.12.1 with GSL | Spec |
| How many nodes | **Two**: *Customer lifetime value* (`clv`: Pareto/NBD, BG/NBD, MBG/NBD, `auto`, Gamma–Gamma and CLV inside) and *BG/BB* (`bg_bb`: discrete opportunities, Gamma–Gamma and CLV inside). Gamma–Gamma is a part of both, not a node (below) | Spec |
| Estimation from the summary | **CLVTools' own compiled likelihoods**, called on the summary exactly as CLVTools' fitting functions call them, with CLVTools' own optimizer settings: the estimates are **identical** (relative difference 0, measured) to `pnbd()`, `bgnbd()`, `gg()` run on a transaction log. No transaction log is ever built in the runner | Spec («Estimating from the summary») |
| MBG/NBD and BG/BB | **Not in CLVTools** (verified: `bgbb()` is a stub that stops with «This model has not yet been implemented!»; no MBG/NBD). Their closed-form likelihoods and expectations are **written in the runner** (a few dozen lines of R each, no special function beyond `lbeta`, `lgamma` and the GSL ₂F₁ CLVTools already wraps), checked against `BTYDplus` and `BTYD` outside the image and against published estimates | Spec |
| CLV formula | **A discrete sum**: the expected transactions of each period of the horizon, discounted at the end of the period with the per-period rate derived from the node's annual rate, times the expected spend, times the margin. The same formula for the four purchase models; the horizon is finite and explicit | Spec |
| GSL in the image | A recipe field `system_libraries`, used by this group only; the other R images unchanged (Open questions, 1) | Owner |
| Where Predict computes | **In the group's R runner**, from the card's parameters, as for Cox and the linear model today: the same functions as the fit, so a prediction of the training rows equals the fit's rows bit for bit | Spec |

### Why two nodes, and Gamma–Gamma inside

The product's rule is «one node per model, its variants inside» (the models spec, «Decisions»).
The «model» here is a CLV model, and a CLV needs **two** fitted processes on the same customers
with the same time unit: purchases and spend. Considered:

| Option | For | Against |
|---|---|---|
| Three nodes: «Purchase model», «BG/BB», «Spend (Gamma–Gamma)», the CLV in the spend node fed by the purchase node's rows | Each node one model | The spend node needs the purchase node's per-period expectations to discount (not one column), plus its unit and horizon: the two nodes' parameters must agree by hand; **two cards, two MLflow versions, two chained Predict nodes** to score a customer, each able to be at a different version |
| One node for everything (continuous and discrete) | One entry | BG/BB's input (opportunities n, integer recency) and its time (an opportunity, not a duration) differ from the three others: half of the form would hide the other half |
| **Two nodes, each with its spend part** (chosen) | One card, one MLflow version, one Predict per CLV; the unit, horizon and discounting are said once; the spend model is fitted on exactly the customers the purchase model saw | Gamma–Gamma alone, without a purchase model, is not a node (its only use alone is the average order value, which a Group by gives) |

`clv`'s variants are the purchase model (`purchase_model`: `auto`, `pareto_nbd`, `bg_nbd`,
`mbg_nbd`) and whether a spend column is given; `bg_bb`'s, whether a spend column is given. In the
catalogue the two **variants** of each node are `purchases` (no spend column: no Gamma–Gamma, no
CLV) and `value` (a spend column: Gamma–Gamma and CLV); the purchase model is a text of the
`model` section, since the sections it writes are the same for the three.

## What exists and is reused (verified)

Everything of the models spec's table «What exists and is reused», unchanged: the managed
environment and its build loop, the Job (`jobs_models.go`, `jobs_predict.go`), the runner
protocol and `common/R` (`tab_main`, `tab_input`, `tab_refuse`, `tab_add`, `tab_points`,
`tab_cap`, `tab_card`, `tab_write_card`, `tab_mlmodel`, `tab_selftest`, `TAB_COMMON_TESTS`),
the catalogue and its copies (`model_catalog.py --check`), `results.py`, `layouts/common.py`
(`page`, `xy`, `mark`, `histogram`, `by_params`, `with_labels`), the chart kinds `xy`, `column`,
`bar`, `heatmap` (verified in `reports/definition.py`: `CHART_KINDS`, `XY_MARKS`, the `xy`
options `target`, `target_x`, `diagonal`, `unit_square`, `log_x`, `Series.color`), the Report
tab, the MLflow logging (`jobs_models_mlflow.go`) and Predict (`modelcatalog.CheckPredict`,
`PredictColumns`, data-driven from `PREDICT`).

What is new: the group's folder, its catalogue entries, its layouts and words, **one optional
field of a recipe** (`system_libraries`, for GSL: «The recipe»), one section shape (`customer`).

## CLVTools: what it has, what it lacks (checked online, measured)

- **Version**: CLVTools **0.12.1**, published on CRAN 2025-11-06 (checked online: the current
  version on 2026-11-20), GPL-3, `NeedsCompilation: yes`, `SystemRequirements: GNU GSL`;
  Imports `data.table`, `digest`, `Formula`, `ggplot2`, `lubridate`, `numDeriv`, `Matrix`, `MASS`,
  `optimx`, `Rcpp`; LinkingTo `Rcpp`, `RcppArmadillo`, `RcppGSL`, `testthat`.
- **Models** (measured, `getNamespaceExports`): `pnbd`, `bgnbd`, `ggomnbd`, `gg`,
  `latentAttrition`, `spending`. **`bgbb` is exported but its only method is
  `stop("This model has not yet been implemented!")`** (measured). **No MBG/NBD** (no function,
  no class, nothing in the NEWS since 0.10.0). So BG/BB and MBG/NBD are written in the runner
  («The models»).
- **Data**: the public fitting functions take a `clv.data` object made by `clvdata()` from a
  **transaction log** (id, date, price), with an estimation split. They do not take a summary.
- **The compiled functions** (measured): per model `pnbd_nocov_LL_ind`, `pnbd_nocov_LL_sum`,
  `pnbd_nocov_PAlive`, `pnbd_nocov_CET`, `pnbd_nocov_DERT`, `pnbd_nocov_PMF`,
  `pnbd_nocov_expectation`; the same for `bgnbd_nocov_*` (no DERT: CLVTools' manual says that
  expression is not derived); `gg_LL`; `vec_gsl_hyp2f1_e` (GSL's ₂F₁ with its status). They are
  **documented in the reference manual** (usage and arguments) **but not exported**: reachable as
  `getFromNamespace("pnbd_nocov_LL_sum", "CLVTools")`. They take vectors: frequency, recency,
  age, and a count of identical rows.
- **What CLVTools' own fit does** (measured, reading `clv.template.controlflow.estimate` and
  `clv.model.prepare.optimx.args`): it reduces the transaction log to the summary
  (x, t.x, T.cal), **compresses it** to the distinct triples with their counts, and minimizes
  `*_LL_sum` (which returns the negative log-likelihood) over the log of the parameters with
  `optimx`. Pareto/NBD and Gamma–Gamma: `method = "L-BFGS-B"`, `itnmax = 3000`,
  `control = list(kkt = TRUE, save.failures = TRUE, starttests = FALSE)`, start r = 0.5,
  α = 15, s = 0.5, β = 10 (Pareto/NBD), p = q = γ = 1 (Gamma–Gamma); BG/NBD: `optimx`'s default
  methods, Nelder–Mead then BFGS, the **last** row kept, start r = 1, α = 3, a = 1, b = 3. The
  summary is therefore all the data the likelihood ever sees (the sufficient statistics), and
  the transaction log is only a way to compute it.

## Estimating from the summary (measured)

Three routes were tried on CLVTools' `cdnow` (2,357 customers, a 39-week calibration):

| Route | What | Result |
|---|---|---|
| **A. A transaction log rebuilt from the summary** | For each customer: a first purchase at (split − T), x − 1 purchases on distinct days drawn in (0, t_x), the last at t_x; one holdout purchase added after the split (CLVTools refuses a split after the last transaction); then `clvdata()` and `pnbd()` | The summary CLVTools recomputes equals the given one exactly; the estimates equal `pnbd()` on the real log: **relative difference 0**. But it is exact **only when the summary is in whole days** (a date has no finer grain; `POSIXct` rounds to the second), it needs Σ(x + 1) rows, and «month» is not a CLVTools time unit |
| **B. CLVTools' compiled likelihood on the summary** (chosen) | `optimx` on `pnbd_nocov_LL_sum` / `bgnbd_nocov_LL_sum` / `gg_LL` over the compressed summary, with the settings and start values above | **Relative difference 0** against `pnbd()`, `bgnbd()`, `gg()` on the log (BG/NBD equal only with its own Nelder–Mead-then-BFGS, measured: with L-BFGS-B the estimates differ by 1e-4). Any real numbers, any unit, no rows to build, 1.6 s with the Hessian for CDNOW |
| C. Likelihoods written in the runner for all five | — | Possible (the closed forms are short, and the measurements below check them), but it drops the owner's library for the three models it has |

**Route B it is**, with route A kept as **the self-test's proof**: a reference case in whole days
is fitted by the runner (route B) and, in the self-test, by CLVTools' public `pnbd()`, `bgnbd()`
and `gg()` on the log rebuilt from it (route A); the parameters must agree to 1e-9 relative
(measured: 0). If a later CLVTools renamed, removed or changed the meaning of a compiled function
the runner calls, the self-test fails and the version stays **failed**: the same protection as
the time series group's reliance on `forecast`'s `myarima` (the models spec, «As built, ARIMA»).
The version is pinned in the recipe.

**The summary is sufficient** for these models without covariates: the Pareto/NBD, BG/NBD and
MBG/NBD likelihoods depend on a customer's data only through (x, t_x, T); BG/BB's through
(x, t_x, n); Gamma–Gamma's through (x, m̄) (the sum of the repeat purchases' values, given x).
What the summary loses: the timing of the purchases between the first and the last (no tracking
plot of weekly purchases, no test of the Poisson timing between purchases), and every covariate
(CLVTools' static and dynamic covariates are out of this release).

**Standard errors**: from the Hessian `optimx` returns at the optimum (`nhatend`) on the log
scale, inverted, then by the delta method on the original scale (diag(exp θ) V diag(exp θ)), as
CLVTools' `vcov()` does; **intervals** at 1 − alpha on the log scale, exp(θ ± z·se_θ), so that
they stay positive (the report says so). A Hessian that is not positive definite gives NULL
standard errors and the `convergence` check says so.

**When the optimizer fails.** `L-BFGS-B` stops with «L-BFGS-B needs finite values of 'fn'» when a
trial point makes the likelihood overflow (measured on an artificial Pareto/NBD case whose
optimum lies far out on s and β). The runner then restarts from the same start with Nelder–Mead
(which tolerates an infinite value) and polishes with `L-BFGS-B` from its end point; the note
`optimizer_fallback` and the `convergence` check say so. On data where CLVTools' own settings
converge, nothing differs from them.

## The group `customer_lifetime` — R

### The recipe

`gateway-go/internal/modelgroups/groups/customer_lifetime/recipe.json`:

```json
{ "language": "r", "base": "4.6", "pinned_on": "2026-11-20",
  "system_libraries": ["libgsl-dev"],
  "packages": ["assertthat@0.2.1", "bit@4.6.0", "bit64@4.8.6", "glue@1.8.1", "cli@3.6.6", "rlang@1.3.0",
    "lifecycle@1.0.5", "magrittr@2.0.5", "vctrs@0.7.3", "purrr@1.2.2", "R6@2.6.1", "withr@3.0.3",
    "tidyselect@1.2.1", "cpp11@0.5.5", "arrow@26.0.1", "jsonlite@2.0.0",
    "brio@1.1.5", "crayon@1.5.3", "data.table@1.18.6.1", "desc@1.4.3", "digest@0.6.39", "evaluate@1.0.5",
    "farver@2.1.2", "Formula@1.2-6", "fs@2.1.0", "generics@0.1.4", "gtable@0.3.6", "isoband@0.3.0",
    "labeling@0.4.3", "nloptr@2.2.1", "numDeriv@2016.8-1.1", "otel@0.3.0", "pracma@2.4.6", "praise@1.0.0",
    "ps@1.9.3", "RColorBrewer@1.1-3", "Rcpp@1.1.2", "rprojroot@2.1.1", "S7@1.0.0", "timechange@0.4.0",
    "viridisLite@0.4.3", "diffobj@0.3.10", "lubridate@1.9.5", "optimx@2025-4.9", "processx@3.10.0",
    "RcppArmadillo@15.6.2-1", "RcppGSL@0.3.14", "scales@1.4.0", "callr@3.8.0", "ggplot2@4.0.4",
    "waldo@0.6.2", "pkgbuild@1.4.8", "pkgload@1.5.3", "testthat@3.3.2", "CLVTools@0.12.1"],
  "resources": { "cpu": "2", "memory": "2Gi" }, "max_rows": 1000000, "max_seconds": 1800 }
```

The 55 packages are the whole closure of `CLVTools` (Depends, Imports, LinkingTo, recursively)
minus R's base and recommended packages, in an order where each comes after what it needs,
versions from CRAN on 2026-11-20 (computed with `tools::package_dependencies` on CRAN's index;
the first 16 are every R image's `arrow` closure, as in the other recipes). `MASS`, `Matrix` and
`lattice` are R's recommended packages, already in `rocker/r-ver:4.6.0` (verified: Matrix 1.7-5,
MASS 7.3-65 in the survival image), which satisfy CLVTools' minimums (Matrix ≥ 1.2-17); they are
not pinned, as in the other groups. `testthat` is there because CLVTools lists it in LinkingTo.
`ggplot2`, `lubridate`, `testthat` and their closures are installed and never called by the
runner.

**GSL is a Debian library, not an R package**: `RcppGSL` compiles against `libgsl-dev`, and
CLVTools links `libgsl` at run time. `scriptimage.RSystemLibraries` (verified: the fixed list every
R image installs) has `cmake` (which `nloptr` needs to build NLopt) but not GSL. The recipe gains
an optional field **`system_libraries`**: Debian package names (each matching
`^[a-z0-9][a-z0-9+.-]+$`), appended to that image's `apt-get install` line only. A recipe is
Tabularia's own file, not a user's input, so the field takes no list from outside; the other
groups' and the scripting environments' Dockerfiles stay **byte for byte as before** (a test
compares them). The build of a managed environment finds the field in the shipped recipe through
`managed_by` (`model_group:customer_lifetime`): no column and no migration. The alternative,
adding `libgsl-dev` to `RSystemLibraries` for every R image, is «Open questions for the owner», 1.

**Licences** (CRAN's `License` fields, checked online with `available.packages()` on 2026-11-20):

| Package or library | Licence | Note |
|---|---|---|
| CLVTools 0.12.1 | GPL-3 | the models |
| GNU GSL 2.7.1 (`libgsl-dev`, `libgsl27`, `libgslcblas0`, Ubuntu 24.04) | GPL-3 | the special functions (₂F₁) |
| Rcpp 1.1.2, RcppArmadillo 15.6.2-1, RcppGSL 0.3.14, digest 0.6.39 | GPL (≥ 2) | Armadillo itself is Apache-2.0 |
| optimx 2025-4.9, numDeriv 2016.8-1.1 | GPL-2 | `numDeriv` is already in the regression recipe |
| Formula 1.2-6, diffobj 0.3.10 | GPL-2 \| GPL-3 | |
| pracma 2.4.6, timechange 0.4.0 | GPL (≥ 3) | |
| nloptr 2.2.1 | LGPL (≥ 3) | builds NLopt (MIT / LGPL) |
| data.table 1.18.6.1 | MPL-2.0 | |
| RColorBrewer 1.1-3 | Apache-2.0 | |
| ggplot2, lubridate, scales, S7, testthat, waldo, brio, callr, cli, crayon, desc, evaluate, farver, fs, generics, gtable, isoband, labeling, otel, pkgbuild, pkgload, praise, processx, ps, rprojroot, viridisLite and the `arrow` closure | MIT (labeling: MIT \| Unlimited) | |

Nothing proprietary, nothing new in kind: GPL-2, GPL-3 and LGPL packages are already in the R
groups (`numDeriv`, `forecast`, `survival`). As for every group, **Tabularia ships none of them**:
the cluster builds the image from CRAN and Debian when an administrator enables the group, and
the runner (Tabularia's, AGPL-3.0) runs them through R. No competitor of Tabularia is named
anywhere.

**Weight** (measured and estimated). The 55 packages installed take **66.8 MB** (measured on the
binaries; a source build installs the same files); GSL **10.4 MB** installed (7.3 MB `-dev`,
2.8 MB `libgsl27`, 0.3 MB `libgslcblas0`). The survival image weighs **1.41 GB** (measured: the
`rocker/r-ver` base, 288 MB of build libraries, 167 MB of packages, mostly `arrow`). So the image
is about **1.5 GB**, like the regression group's 1.53 GB. Its **build** compiles CLVTools (C++
with Armadillo and GSL), `data.table`, `nloptr` (a CMake build of NLopt) and `RcppArmadillo`: a
**heavy job**, at most `TABULARIA_JOBS` compilers at once. **Measured by B0 (2026-11-20): 759 s (12 min 39 s) with 2 compilers, 1.51 GB** (`tabularia-models-customer_lifetime:dev`; the survival image is 1.53 GB). `library(CLVTools)` 0.12.1 loads as uid 10001 on a read-only root; libgsl 2.7.1+dfsg-6ubuntu2; the 15 unexported functions of «CLVTools: what it has» exist with the arguments the spec uses; the self-test passes its 15 common checks, exits 4 when a case is made to fail, the stub exits 3.

**B0 deviations**: the Gamma–Gamma parameters are `spend_param_p`, `spend_param_q`, `spend_param_gamma` (not `param_*`: BG/BB's own γ is `param_gamma`); `holdout_length` applies only with `holdout_transactions` (the reverse of `requires` is the `applies` rule); `margin` defaults to 1.0; the heatmap's fixed scale is left to B5.

### The runner

```text
gateway-go/internal/modelgroups/groups/customer_lifetime/
  recipe.json
  runner/run.R                    the dispatcher: tab_main("customer_lifetime", routes)
  runner/R/clv_shared.R           the summary's checks, the optimizer, standard errors, Gamma–Gamma,
                                  discounting and CLV, holdout, chi-square, P(alive) grid, charts' points,
                                  the card, the rows; the diagnostics
  runner/R/clv.R                  fit_clv, predict_clv: Pareto/NBD and BG/NBD through CLVTools,
                                  MBG/NBD written here, auto and compare
  runner/R/bg_bb.R                fit_bg_bb, predict_bg_bb: BG/BB written here
  reference/data/*.csv, expected.json, make_reference.R, make_witness.R, witness.json
```

No file starts with `_` or `.` (`go:embed`, verified rule of the models spec). The CLVTools
functions are bound once, at the top of `clv.R`, with `getFromNamespace(name, "CLVTools")`, and
the runner stops with a sentence if one is missing («the image's CLVTools lacks pnbd_nocov_LL_sum:
rebuild the group»). Random numbers (only the charts' samples) follow `tab_seed`.

## The input: the per-customer summary

### `clv` (continuous time)

| Key | Kind | Rule (the row is refused with a sentence that names the customer, unless said) |
|---|---|---|
| `customer` | column (any kind) | The customer's id. **Duplicates are refused** («the customer 4812 appears on 3 rows: one row per customer; group the transactions by customer first»). A NULL id: the row is left out (`n_missing`) |
| `frequency` (x) | number column | **Repeat** purchases in the calibration period (the first purchase not counted), a **whole number ≥ 0** (a value within 1e-9 of a whole number is read as it) |
| `recency` (t_x) | number column | Time from the first purchase to the last one, in the node's `time_unit`; **0 ≤ t_x ≤ T**; **t_x = 0 when x = 0**; **t_x > 0 when x > 0** (a repeat purchase at the first moment: «purchases of the same day must be one purchase: group by customer and day first»). A t_x above T by at most 1e-9·T is set to T (note `t_x_clamped`) |
| `age` (T) | number column | Time from the first purchase to the end of the calibration period, in `time_unit`, **> 0** |
| `spend` (m̄) | number column, optional | The **average value of the repeat purchases** (the first purchase excluded, as Gamma–Gamma assumes and CLVTools' `remove.first.transaction = TRUE` does). For x > 0: **> 0** («returns and refunds: leave those customers out, or net them before averaging»); NULL: the customer is left out of the spend fit only and gets the population's expected spend (note `spend_missing`). For x = 0: ignored whatever it holds |
| `holdout_transactions` | number column, optional | Purchases (counted as `frequency` is) in the holdout period that follows the calibration end; whole number ≥ 0; NULL: the customer is left out of the holdout check only |
| `holdout_length` | number > 0, required with `holdout_transactions` (and only with it) | The holdout period's length in `time_unit`, the same for every customer (a calendar window) |
| `time_unit` | choice `day` \| `week` \| `month` \| `quarter` \| `year`, **required** | The unit of t_x, T, the horizon, the holdout and the discounting. A note `unit_check` when the largest T exceeds 30 years in that unit («is the unit right?») |

A row with NULL in `frequency`, `recency` or `age` is left out (`n_missing`), as the common rule
says; a non-finite number stops the node naming the column (the common rule).

### `bg_bb` (discrete opportunities)

| Key | Kind | Rule |
|---|---|---|
| `customer` | column | as above |
| `frequency` (x) | number column | Repeat transactions in the n opportunities, whole number ≥ 0 |
| `recency` (t_x) | number column | The opportunity (1…n) of the last transaction, 0 when x = 0; whole number; **x ≤ t_x ≤ n** (at most one transaction per opportunity), t_x = 0 exactly when x = 0 |
| `opportunities` (n) | number column | Opportunities after the first transaction, whole number, 1 ≤ n ≤ `max_opportunities` (1,000) |
| `spend`, `holdout_transactions` | as above | the holdout counts transactions in the next `holdout_length` opportunities, 0 ≤ count ≤ `holdout_length` |
| `holdout_length` | integer 1–1,000 | Opportunities in the holdout |
| `period_unit` | choice `day` \| `week` \| `month` \| `quarter` \| `year`, **required** | How long one opportunity lasts: the discounting's period |

### Building the summary with Tabularia's nodes (a note for the user's documentation)

The nodes below exist (verified: `operations.py` has `op_filter`, `op_group_by` with `count`,
`min`, `max`, `sum`, `first`, `n_unique`, `op_join`, `op_compute`, `op_fill_null`; the
user's guide `docs/capabilities/flows.md` lists Filter, Group by, Join, Computed column, Fill
nulls). From a table of transactions (`customer`, `ts`, `amount`) and a calibration end, say
1997-09-30, with weeks as the unit:

1. **Filter** `ts` ≤ the calibration end.
2. **Computed column** `day` = the date of `ts` (the engine's SQL: `CAST(ts AS DATE)`).
3. **Group by** `customer`, `day`: *sum* of `amount` as `day_amount`. Purchases of the same day
   become one, as the literature and CLVTools count them.
4. **Group by** `customer`: *count* of `day` as `days`, *minimum* of `day` as `first_day`,
   *maximum* as `last_day`, *sum* of `day_amount` as `total`.
5. **Join** (left) the rows of step 3, renamed `day` → `first_day`, `day_amount` →
   `first_amount`, on `customer` and `first_day`.
6. **Computed column**, in the engine's SQL dialect (DuckDB `date_diff('day', a, b)`, ClickHouse
   `dateDiff('day', a, b)`): `frequency = days - 1`; `recency = date_diff('day', first_day,
   last_day) / 7.0`; `age = date_diff('day', first_day, DATE '1997-09-30') / 7.0`;
   `avg_spend = CASE WHEN days > 1 THEN (total - first_amount) / (days - 1) END`.
7. For a holdout: a second chain from the transactions, **Filter** the holdout window, Group by
   `customer` with the *distinct count* of the day as `holdout_purchases`; **Join** (left) it on
   `customer`; **Fill nulls** with 0.

A customer whose first purchase is after the calibration end is not in the summary (step 1), as
the models assume. The documentation shows this flow on the sample data, and the assistants know
it («Assistants»).

## The two nodes

### `clv` — Customer lifetime value

| Key | Kind | Default | Applies | Meaning |
|---|---|---|---|---|
| `customer`, `frequency`, `recency`, `age`, `time_unit` | (above) | — (needed) | always | |
| `spend` | number column | none | always | Gamma–Gamma and the CLV (variant `value`) |
| `purchase_model` | choice `auto` \| `pareto_nbd` \| `bg_nbd` \| `mbg_nbd` | `auto` | always | `auto` fits the three and keeps the one with the highest log-likelihood («Choosing the purchase model») |
| `compare` | boolean | false | `purchase_model` ≠ `auto` | Fit the two others too, for the comparison table only; the node's model stays the one chosen |
| `horizon` | integer 1–1,000 | none: one year in `time_unit` (365, 52, 12, 4, 1) | always | Periods ahead for the expected transactions and the CLV |
| `discount_rate` | number 0–1 | 0.10 | `spend` given | **Annual** rate; per period d = (1 + D)^(1/k) − 1 with k = 365.25, 365.25/7, 12, 4, 1 periods per year |
| `margin` | number in (0, 1] | 1 | `spend` given | The share of the spend that is value: with 1 the CLV is of revenue, and the report says so |
| `holdout_transactions`, `holdout_length` | (above) | none | always | The holdout check |
| `alpha` | (0, 1) | 0.05 | always | The intervals, the tests |
| `seed` | integer | 1 | always | The charts' samples |
| `output` | choice `results` \| `rows` | **`rows`** | always | The per-customer scores are what this node is for; `results` keeps only the table of results |

`requires`: `holdout_length` with `holdout_transactions` and the reverse (reason
`holdout_needs_length`). `disjoint`: `customer`, `frequency`, `recency`, `age`, `spend`,
`holdout_transactions`. New kinds (plain data, existing controls): `time_unit`,
`purchase_model`, `horizon_periods` (`integer` 1–`max_horizon`, default none), `discount_rate`
(`number` min 0 max 1), `fraction` (exists), `positive` (exists). The default `output = rows`
differs from the other models' `results`: «Open questions for the owner», 4.

### `bg_bb` — BG/BB (discrete opportunities)

The same keys as `clv`, but `age` → `opportunities`, `time_unit` → `period_unit`, no
`purchase_model` and no `compare`; `horizon` counts opportunities (default: one year in
`period_unit`), `holdout_length` is an integer.

### In the palette and the panel

A subtitle *Customer lifetime* under *Models*, with *Customer lifetime value* and *BG/BB*; the
panel shows the group's name and description in five languages (`modelsAdmin`), its
system library on the card beside the lock («GSL 2.7.1 (Debian)»).

## The models

Notation: r, α (purchase rate λ ~ Gamma(r, α)); Pareto/NBD s, β (dropout rate μ ~ Gamma(s, β));
BG/NBD and MBG/NBD a, b (dropout probability p ~ Beta(a, b)); BG/BB α, β (transaction
probability ~ Beta(α, β)), γ, δ (dropout probability ~ Beta(γ, δ)); Gamma–Gamma p, q, γ. Every
parameter is estimated on the log scale and is > 0.

| Model | Likelihood | P(alive) | Expected transactions in (T, T + t] (CET) | P(X(t) = x) (for the fit check) | Where |
|---|---|---|---|---|---|
| Pareto/NBD (Schmittlein, Morrison, Colombo 1987; Fader & Hardie 2005) | `pnbd_nocov_LL_sum` (₂F₁ by GSL inside) | `pnbd_nocov_PAlive` | `pnbd_nocov_CET` | `pnbd_nocov_PMF` | CLVTools |
| BG/NBD (Fader, Hardie, Lee 2005, *Marketing Science* 24(2)) | `bgnbd_nocov_LL_sum` | `bgnbd_nocov_PAlive` | `bgnbd_nocov_CET` (₂F₁ by GSL) | `bgnbd_nocov_PMF` | CLVTools |
| MBG/NBD (Batislam, Denizel, Filiztekin 2007, *IJRM* 24(3)) | below | below | below | below | runner |
| BG/BB (Fader, Hardie, Shang 2010, *Marketing Science* 29(6)) | below | below | below | below | runner |
| Gamma–Gamma (Colombo & Jiang 1999; Fader, Hardie, Lee 2005, *JMR* 42(4); Fader & Hardie 2013 note) | `gg_LL` | — | — | — | CLVTools; the expectation below in the runner |

**MBG/NBD** (a customer may also leave right after the first purchase), with
L₀ = Γ(r+x)α^r / Γ(r):

- L = L₀ · [ B(a, b+x+1)/B(a,b) · (α+T)^−(r+x) + B(a+1, b+x)/B(a,b) · (α+t_x)^−(r+x) ], summed in
  logs (log-sum-exp), for every x including 0;
- P(alive) = 1 / [1 + a/(b+x) · ((α+T)/(α+t_x))^(r+x)];
- CET(t) = (a+b+x)/(a−1) · [1 − ((α+T)/(α+T+t))^(r+x) · ₂F₁(r+x, b+x+1; a+b+x; t/(α+T+t))] · P(alive);
- P(X(t) = x) = B(a, b+x+1)/B(a,b) · NB(x) + B(a+1, b+x)/B(a,b) · [1 − Σ_{j<x} NB(j)], with
  NB(j) = Γ(r+j)/(Γ(r) j!) · (α/(α+t))^r · (t/(α+t))^j (the sum is empty for x = 0).

Measured against `BTYDplus` 1.2.0's `mbgcnbd.*` with k = 1: the log-likelihood on 20 random
parameter sets over CDNOW, largest relative difference 8.0e-15; P(alive) 0; CET(39) 2.6e-14; the
PMF for x = 0…12, 2.2e-16 (the first version of the PMF summed j ≤ x and differed by 0.26: the
sum stops at x − 1, since leaving after the x-th repeat purchase needs that purchase to have
happened). The fit on CDNOW gives r = 0.524844, α = 6.183093, a = 0.891388, b = 1.614048,
LL = −9,582.136, the same six digits as `BTYDplus::mbgnbd.EstimateParameters`.

**BG/BB** (x transactions in n opportunities, the last at t_x):

- L = B(α+x, β+n−x)/B(α,β) · B(γ, δ+n)/B(γ,δ) + Σ_{i=0}^{n−t_x−1} B(α+x, β+t_x−x+i)/B(α,β) ·
  B(γ+1, δ+t_x+i)/B(γ,δ), in logs;
- P(alive at n + 1) = B(α+x, β+n−x)/B(α,β) · B(γ, δ+n+1)/B(γ,δ) / L;
- CET(n*) = (1/L) · B(α+x+1, β+n−x)/B(α,β) · δ/(γ−1) · Γ(γ+δ)/Γ(1+δ) ·
  [Γ(1+δ+n)/Γ(γ+δ+n) − Γ(1+δ+n+n*)/Γ(γ+δ+n+n*)] (at γ = 1 exactly, its limit, the digamma
  difference: B3 writes it and tests it at γ = 1 ± 1e-6);
- P(X(n) = x) = C(n,x) B(α+x, β+n−x)/B(α,β) · B(γ, δ+n)/B(γ,δ) + Σ_{i=x}^{n−1} C(i,x)
  B(α+x, β+i−x)/B(α,β) · B(γ+1, δ+i)/B(γ,δ).

Measured against `BTYD` 2.4.3's `bgbb.*`: log-likelihood 1.8e-15 absolute on 20 random sets,
P(alive) 1.0e-15, CET(5) 8.9e-16 relative, PMF 5.6e-17 (and Σ_x P = 1). The data are compressed to
the distinct (x, t_x, n) with their counts (BG/BB's data compress to a few dozen rows).

**The fit** of MBG/NBD and BG/BB: `optimx` with `L-BFGS-B` as for Pareto/NBD, start (1, 3, 1, 3)
for MBG/NBD (`BTYDplus`' start) and (1, 1, 1, 1) for BG/BB, the same fallback and standard errors.

**Gamma–Gamma** (the customers with x > 0 and a spend): m̄ | x has the density
Γ(px+q)/(Γ(px)Γ(q)) · γ^q · m̄^(px−1) x^(px) / (γ + x m̄)^(px+q), so x m̄/(γ + x m̄) ~ Beta(px, q)
(used for the chart of the spend's distribution). The expected average spend of a customer is
E[M | x, m̄] = (γ + x m̄) p / (p x + q − 1) for x > 0 with a spend, and the population's p γ/(q − 1)
otherwise. **q ≤ 1** makes the mean spend infinite: the node stops (refusal `spend_no_mean`,
«Open questions for the owner», 6).

### The CLV

For customer i, with CET_i(0) = 0, the horizon H periods, d the per-period rate, m the margin:

CLV_i = m · E[M_i] · Σ_{k=1}^{H} [CET_i(k) − CET_i(k−1)] / (1 + d)^k

Each period's expected transactions are discounted at the end of the period (end-of-period
convention). The same formula serves the four purchase models (for BG/BB, k counts
opportunities). Why not CLVTools' DERT: it exists only for Pareto/NBD (CLVTools has none for
BG/NBD), its horizon is infinite (the models' weakest extrapolation), and its timing differs from
the holdout's periods. Measured on CDNOW (Pareto/NBD, weekly, 10 % a year, 100 years): the sum
over periods gives 9,003.93 discounted transactions, CLVTools' DERT with δ = ln(1 + d) 9,012.30,
a ratio 0.99907 against the e^(−δ/2) = 0.99908 that half a period of discounting predicts: the
formula is consistent with DERT, and this comparison is a witness check. When H exceeds
`max_clv_steps` (520), the sum uses steps of ⌈H / 520⌉ periods, each discounted at its end (note
`clv_steps`). The expected transactions over the horizon are CET_i(H); the report also gives the
discounted ones (Σ of the discounted increments).

**Precision of ₂F₁.** Pareto/NBD's likelihood and P(alive), and BG/NBD's and MBG/NBD's CET, need
the Gauss hypergeometric function. CLVTools computes it with GSL's `gsl_sf_hyperg_2F1`; the
runner uses the same routine for MBG/NBD through `vec_gsl_hyp2f1_e`, which also returns GSL's
status. Measured against `hypergeo` 1.2-15 (a separate implementation, witness): relative
differences 0 to 2.5e-14, and 1.1e-11 at a = 120.5, z = 0.99. Where GSL fails — a non-zero
status, or a non-finite CET (measured: `bgnbd_nocov_CET` returns NaN for x = 5,000 and x =
20,000), or a = 1 exactly, where the closed form divides by zero — the runner computes the CET
from its integral form instead: CET(t) = P(alive) · E[(1 − ((α+T)/(α+T+p t))^(r+x)) / p] over the
posterior p ~ Beta(a, b+x) (BG/NBD) or Beta(a, b+x+1) (MBG/NBD), by **Gauss–Jacobi quadrature of
64 nodes** (Golub–Welsch on the Jacobi matrix, a few lines; the integrand is smooth and bounded on
[0, 1], the Beta weight is exact). Measured on CDNOW: within 1.0e-12 of GSL at t = 1, 6.4e-14 at
t = 39 and 7.4e-14 at t = 520; for x = 5,000 and 20,000 it gives 2,070 and 8,437 transactions in 52
weeks where GSL gave NaN. The note `cet_quadrature` counts the customers that needed it. Tolerances
in «Reference values».

### Choosing the purchase model: `auto` and `compare`

`auto` fits Pareto/NBD, BG/NBD and MBG/NBD and keeps the **highest log-likelihood** among those that
converged; the three have four parameters, so AIC and BIC rank them the same way (they are
written all the same). A tie within 1e-6 keeps the first of Pareto/NBD, BG/NBD, MBG/NBD. The
holdout is **not** used to choose (it stays an honest check: «Open questions for the owner», 5).
`compare` fits the others for the table only. The `candidate` section holds, per model: `log_lik`,
`aic`, `bic`, `chi_square`, `chi_square_p_value`, `holdout_rel_error` and `holdout_mae` (with a
holdout), `converged`, `chosen`, and its parameters (`param_*`). Measured on CDNOW: BG/NBD −9,582.43,
MBG/NBD −9,582.14, Pareto/NBD −9,594.98: `auto` chooses MBG/NBD.

## The output

The ten columns. One new section shape in `SECTION_SHAPES` (engine, and through `catalog.json`
the R and Python checks): **`customer`** (`level` required: the customer's id as a label;
`level_2` null; not indexed), the top customers' table.

| Section | Rows |
|---|---|
| `model` | texts `model`, `variant`, `formula` («x: frequency, t_x: recency, T: age, in weeks»), `software` («R 4.6.x; CLVTools 0.12.1; optimx 2025-4.9; GSL 2.7.1»), `group_version`, `rows_from`, `note_*`, `purchase_model` (the one used), `purchase_model_asked` (`auto`…), `time_unit`, `optimizer` (`L-BFGS-B`, `Nelder-Mead, BFGS`…); numbers `n` (customers), `n_missing`, `n_repeaters`, `share_repeaters`, `mean_frequency`, `max_age`, `median_age`, `horizon`, `log_lik`, `aic`, `bic`, `n_params`, `converged` (1/0), `param_*` (the purchase model's parameters, so that MLflow logs them), derived `mean_rate` (r/α), `mean_dropout` (BG/NBD, MBG/NBD a/(a+b); Pareto/NBD s/β, the mean dropout rate), `expected_transactions` (Σ CET(H)), `mean_p_alive`; with a holdout `holdout_length`, `holdout_observed`, `holdout_expected`, `holdout_rel_error` ((expected − observed)/observed), `holdout_mae`, `holdout_rmse`, `holdout_correlation` (Pearson, per customer); variant `value` also `spend_n`, `spend_log_lik`, `param_p`, `param_q`, `param_gamma`, `mean_spend_population` (pγ/(q−1)), `discount_rate`, `discount_per_period`, `margin`, `clv_total`, `clv_mean`, `clv_median`, `clv_top10_share` (the share of the total CLV held by the top 10 % of customers), `clv_top20_share`, `discounted_transactions` |
| `coef` | `level` = the parameter (`r`, `alpha`, `s`, `beta`, `a`, `b`; BG/BB `alpha`, `beta`, `gamma`, `delta`; Gamma–Gamma `p`, `q`, `gamma`), `level_2` = `purchases` \| `spend`: `estimate`, `std_error`, `ci_low`, `ci_high` (on the log scale, back-transformed) |
| `candidate` | `level` = `pareto_nbd` \| `bg_nbd` \| `mbg_nbd` («Choosing the purchase model»), only with `auto` or `compare` |
| `assumption` | the checks («Diagnostics») |
| `hist` | `frequency` (`level_2` `observed` \| `expected`; `x0` = the class 0, 1, …, the last class open «k+», `x1` = the class's upper end or NULL; `value` = customers); `holdout` (`level_2` `observed` \| `expected`, per frequency class 0…7+: `value` = the mean holdout purchases per customer, `x1` = customers in the class); `p_alive` (the distribution of P(alive), 20 bins of 0.05); `spend` (`observed` \| `expected`, 20 bins at the quantiles 0, 5 %, …, 95 % of the observed m̄, the last open; expected = Σ_i over the repeaters of P(m̄ in the bin \| x_i) by the Beta form above); `clv` (bins equal on log₁₀ of the CLV above 0, 20 at most, plus one bin for 0) |
| `cell` | `p_alive`: `level` = frequency x (0…10, or the 99th percentile of x if smaller), `level_2` = the recency class (10 equal classes of [0, T*], T* = the median age, rounded to a tenth; for x = 0 only t_x = 0), `value` = P(alive) of the chosen model at (x, t_x = the class's middle, T*) |
| `curve` | `p_alive_recency`: `level_2` = `x=1`, `x=2`, `x=5`, `x=10` (those ≤ the largest x), `x0` = t_x on 50 points of [0, T*], `value` = P(alive) at T*; `spend_frequency`: the lowess line of m̄ on x (`tab_lowess`, 200 points at most); `concentration`: `x0` = the share of customers ranked by CLV from the highest, `value` = their share of the CLV, 101 points |
| `point` | `spend_frequency`: `x0` = x, `value` = m̄, the repeaters, capped at `max_chart_points` with the ten largest m̄ kept and labelled by the customer's id; its lowess line in `curve|spend_frequency` |
| `customer` | the top `max_top_customers` (20) by CLV (by expected transactions in the `purchases` variant): `rank`, `frequency`, `recency`, `age`, `p_alive`, `expected_transactions`, `expected_spend`, `clv` |

**`rows` output**: the input's columns, then `pred_p_alive`, `pred_transactions` (CET(H)), with a
holdout `pred_holdout_transactions` (CET(holdout_length)), and in the variant `value`
`pred_spend` (E[M]), `pred_discounted_transactions`, `pred_clv`. A clash with an input column is a
refusal at run time (as for boosting). A row left out (NULL key column) keeps NULL predictions.

## Diagnostics

**Decided 2026-11-20 (owner): nothing hardcoded.** A verdict comes only from a test with a real p-value (the χ² on the pooled frequency classes, the HC3 slope test of the spend against the frequency, any likelihood-ratio test), compared with the node's `alpha` (default 0.05, written in the report). Measures without a p-value (the holdout's error, the share gap, the horizon against the window) are numbers and charts with a descriptive sentence, never a threshold; only certain facts stay warnings: no convergence, parameters at an edge, q ≤ 1. The thresholds in the table below (0.15, 0.03, 0.1, 0.3, 300 customers…) are therefore superseded; lot B4 is dropped.

Section `assumption`, the catalogue's verdicts (`ok`, `doubtful`, `failed`, `not_computed`), an
`advice_id` beside the advice (the report picks its sentence by the id, as boosting's do since
`713c8469`), `h0` saying what is looked at, `value` and `threshold` holding the numbers. The
lesson of the R groups' and the boosting group's reviews is applied from the start: **a check must
not fail by construction on data the model describes well.** So every threshold below is accepted
only after lot B4's simulations of clean data (data generated by the fitted model itself,
200 replicates at n = 300 and n = 3,000): «failed» in at most 1 % of them, «doubtful» in at most
10 %; and the textbook data the literature calls a good fit (CDNOW with BG/NBD, the donations
with BG/BB) must not come out failed. The numbers given are B4's starting point.

| Check id | Nodes | What is computed | ok · doubtful · failed | Advice id: sentence |
|---|---|---|---|---|
| `convergence` | both | the optimizer's code, KKT's first- and second-order conditions (`optimx`), a positive-definite Hessian | converged, KKT both true · converged with a KKT condition false, or the Nelder–Mead fallback used, or no standard errors · not converged | `convergence`: «the estimates may not be the best ones: the data may hold too few repeat customers for this model; try another purchase model» |
| `boundary` | both | one row per parameter at an edge: an estimate below 1e-4 or above 1e4 (`level_2` the parameter); the reading per model (Pareto/NBD s and β large with s/β finite: everyone has the same dropout rate; BG/NBD, MBG/NBD a → 0 or b large: nearly nobody drops out; r and α large: everyone buys at the same rate) | none · any — never failed: an edge is a legitimate limit of the model | `boundary_no_dropout`: «the data show almost no dropout: P(alive) is close to 1 for everyone and the model reduces to a plain purchase-rate model»; `boundary_homogeneous`: «the customers look alike on this process: the spread of the rate is not identified» |
| `sample_size` | both | customers and repeaters (`value`, `count`) | ≥ 300 customers and ≥ 100 repeaters · fewer — never failed (the refusals take the hopeless cases) | `sample_size`: «few customers or few repeat buyers: the parameters are imprecise; read their intervals» |
| `frequency_fit` | both | χ² of the observed against the expected number of customers per frequency class (expected = Σ_i P(X(T_i) = x), for BG/BB P(X(n_i) = x)); classes 0, 1, … pooled from the top until every expected count is ≥ 5; df = classes − 1 − 4; the largest gap between observed and expected **shares** in `value` | p ≥ alpha · p < alpha and the largest share gap ≤ 0.03 · p < alpha and a gap > 0.03; not computed with fewer than 6 classes | `frequency_fit`: «the model misses the distribution of repeat purchases: compare the purchase models (compare = true)»; `frequency_fit_small`: «the departure is detectable but small (at most 3 points of share): with many customers the test rejects small departures» |
| `holdout` | both, with a holdout | the total's relative error (expected − observed)/observed; per class in the chart | \|e\| ≤ 0.15 · ≤ 0.30 · above; not computed when the observed total is below 30, and a row `holdout` not computed with «the summary has no holdout column» without one | `holdout_under` / `holdout_over`: «the model predicts fewer (more) purchases than happened in the holdout: the calibration period may differ from the holdout (a season, a campaign); try another purchase model» |
| `horizon_length` | both | the horizon over the longest age (opportunities for BG/BB) | ≤ 2 · above — never failed: an extrapolation, not an error | `horizon_length`: «the horizon is more than twice the period the model learned from: the far periods rest on the model's form» |
| `spend_independence` | variant `value` | Gamma–Gamma assumes the average spend independent of the frequency. Pearson's r between x and m̄ over the repeaters (`value`), and the test of the slope of m̄ on x with **HC3 standard errors** (`statistic` z, `p_value`) | \|r\| < 0.1, or p ≥ alpha · p < alpha and \|r\| < 0.3 · p < alpha and \|r\| ≥ 0.3 | `spend_independence`: «the average spend moves with the frequency: Gamma–Gamma's CLV is then biased; segment the customers by frequency, or read the CLV with care» |
| `spend_tail` | variant `value` | q | q > 2 · 1 < q ≤ 2 — (q ≤ 1 is a refusal) | `spend_tail`: «the spend varies so much across customers that its average has no finite variance: a few large spenders drive it» |

**Why these forms** (measured on CDNOW, the textbook case):

- **`frequency_fit`.** The χ² of Pareto/NBD over 8 classes is 11.99: p = 0.0074 with df = 3,
  p = 0.10 with df = 7, and the largest share gap is 1.8 points; BG/NBD 4.82 (p = 0.19), MBG/NBD
  2.95 (p = 0.40). Read on p alone, Pareto/NBD on CDNOW would «fail». With the share gap it is
  «doubtful», which is what the literature says of it (it over-predicts the ones). The df: the
  parameters are fitted on the individual data, not on the grouped counts, so the statistic lies
  between χ²(k − 1 − m) and χ²(k − 1) (Chernoff and Lehmann, 1954); k − 1 − m is the literature's
  choice and the stricter one, and the share gap guards against its false alarms. B4 checks the
  rates; if df = k − 1 − m still gives more than 1 % «failed» on clean data, it takes k − 1.
- **`spend_independence`.** On CDNOW r = 0.114 (Fader and Hardie report 0.11 and call it fine):
  Pearson's classical test gives p = 0.00045, the HC3 slope test p = 0.082. Under Gamma–Gamma the
  variance of m̄ falls as x rises (it is p/(ν² x)), so the classical test assumes what the model
  denies, and would flag the textbook case **by construction**. Spearman's ρ (0.206 on CDNOW) is
  not used: under the model the median of m̄ rises with x as its skew falls, so ρ is above 0 even
  under independence.
- **`holdout`.** On CDNOW over 39 weeks the three models predict 11.5 % (Pareto/NBD), 12.1 %
  (BG/NBD) and 16.2 % (MBG/NBD) fewer purchases than the 1,882 observed; BG/BB on the donations,
  12,884 against 13,260 (−2.8 %). A 10 % threshold would mark the textbook fits as doubtful;
  15 % does not. Per class the report shows both.

The layout writes under the table: «These checks read the model against the data. A doubtful
verdict says where to look, not that the numbers are wrong.»

## The report

`backend/app/engine/models/layouts/customer_lifetime.py` (the blocks both nodes share), `clv.py`
and `bg_bb.py`; words in `report_texts/customer_lifetime.json`, `clv.json`, `bg_bb.json`, five
languages each; built with `common.page(…, assumptions_note = …)`, A4 portrait as every model.

1. **First page**: the header band; four cards — customers (and repeaters), the purchase model
   used (and its log-likelihood), expected transactions over the horizon (total), and in the
   `value` variant the total CLV (else the mean P(alive)); the formula line («MBG/NBD, chosen by
   the log-likelihood · weeks · horizon 52 weeks · discount 10 % a year · margin 100 %: CLV of
   revenue»); the **checks** with their advice.
2. **The model**: the parameters' table per part (purchases, spend: estimate, standard error,
   interval «on the log scale»), log-likelihood, AIC, BIC, the derived numbers (mean purchase rate
   per period, mean dropout), the optimizer and its result; with `auto` or `compare`, the
   **comparison table** (model, log-likelihood, AIC, BIC, χ² p, holdout error, converged), the
   chosen one marked, and the sentence «the three have four parameters: AIC and BIC rank them as
   the log-likelihood does».
3. **The fit**: «Repeat purchases, observed and expected» — a `column` chart, category the
   frequency class (`0` … `7+`), two series observed and expected (the palette's two first
   colours), under `frequency_fit`; then «Holdout: purchases per customer by calibration
   frequency» — a `column` chart, the same classes, two series, under `holdout`, with the totals
   in a line beneath («expected 1,653, observed 1,882, −12.1 %»); without a holdout column, the
   sentence «The summary has no holdout column: add the purchases of a later period
   (holdout_transactions) and its length to check the model on data it did not see» in place of
   the chart.
4. **P(alive)**: the `heatmap` of `cell|p_alive` (rows the frequency, columns the recency classes,
   cells labelled with their value: `labels: true`), the caption «for a customer known for T* =
   36.4 weeks, the median age»; beside it an `xy` chart, `curve|p_alive_recency`, four `line`
   series, y from 0 to 1 (`min`/`max`); under them the histogram of P(alive) over the customers
   (`column` via `common.histogram`).
5. **Spend** (variant `value`): «Average spend, observed and the model's» — a `column` chart of
   `hist|spend`, two series, bins labelled by their ends in the locale's numbers; «Average spend
   against frequency» — an `xy` chart, `point` and the lowess `line`, under `spend_independence`,
   with r and the HC3 p in the caption; the Gamma–Gamma parameters and the population's mean
   spend.
6. **CLV** (variant `value`; in the `purchases` variant the same page on expected transactions):
   the histogram of `hist|clv` (`column`, bins on the log scale, labelled in currency-free
   numbers); the **concentration curve** (`xy`, `line`, `diagonal: true`, `unit_square: true`:
   «the top 10 % of customers hold 41 % of the CLV»); the table of the **top 20 customers**
   (`customer` section: rank, id, x, t_x, T, P(alive), expected transactions, expected spend,
   CLV).
7. **Notes**: the `note_*` rows (`optimizer_fallback`, `cet_quadrature`, `spend_missing`,
   `clv_steps`, `t_x_clamped`, `unit_check`, `card_too_large`), the software, the group version.

Excel: the checks, the parameters, the comparison, the frequency and holdout tables, the top
customers. The per-customer scores are the node's rows, not the report's.

**The chart engine needs nothing new** (verified in `definition.py` and `charts.py`): `column`
with two series, `heatmap` with labels, `xy` with `line`, `point`, `diagonal`, `unit_square`, and
`min`/`max` exist. One limit found: `heatmap` sets its colour scale from the data's own minimum
and maximum (`charts.py`, `visualMap`) and ignores `options.min`/`max`, so a P(alive) map whose
values run from 0.3 to 0.9 colours 0.3 as 0. The cells' labels carry the values; a fixed 0–1
scale would be a one-line change to an existing kind, which is «Ask first»: «Open questions for
the owner», 3.

## The saved model, MLflow and Predict

**The card** (`model.json`, the common keys of the models spec), with `inputs` = the columns
`frequency`, `recency`, `age` (or `opportunities`), and `spend` in the `value` variant, each
`number`; `customer` and the holdout columns are not inputs. Its own object, declared in
`card.schema.json`:

```json
"customer_lifetime": {
  "node": "clv", "time_unit": "week", "purchase_model": "mbg_nbd", "chosen_by": "log_lik",
  "purchases": { "model": "mbg_nbd", "params": { "r": 0.52484…, "alpha": 6.18309…, "a": 0.89138…, "b": 1.61404… },
                 "vcov_log": [[…]] },
  "spend": { "model": "gamma_gamma", "params": { "p": 6.24935…, "q": 3.74426…, "gamma": 15.44431… },
             "vcov_log": [[…]] },
  "horizon": 52, "discount_rate": 0.10, "periods_per_year": 52.17857142857143, "margin": 1,
  "library": { "CLVTools": "0.12.1", "gsl": "2.7.1" } }
```

Numbers with 17 significant digits (`tab_json`). The parameters, the unit, the horizon, the rate
and the margin are **all that a prediction needs**: no row of the input, no RDS, no pickle. **No
`native.rds`** is written for this group: CLVTools' fitted objects are built from a transaction
log the runner never makes, and the card already holds everything (the MLflow page of the
documentation says how to recompute a prediction from the card with CLVTools' functions). A card
is a few kilobytes.

**MLflow** (lot G's logging, unchanged): parameters (the node's, flattened), metrics (every
number of the `model` section, so `param_r`, `param_alpha`… `log_lik`, `clv_total`,
`holdout_rel_error`; `assumption.<check>[.<level_2>].value`, `.statistic`, `.p_value`), artifacts
(`tabularia/results.parquet`, `tabularia/report.pdf`, `model/MLmodel`, `model/model.json`), the
registered version with the tags of today plus `tabularia.variant` (`purchases` \| `value`, which
the Predict form reads, as for boosting) and `tabularia.purchase_model`.

**Predict** (lot H's node, data-driven; `PREDICT` in the catalogue):

- `applies_to`: `horizon` gains `clv` and `bg_bb` (the card's by default; given, it replaces it,
  in the card's unit). No other key in this release (a rate or a margin other than the card's:
  refit).
- `columns`: `clv:purchases` and `bg_bb:purchases` → `prediction` (expected transactions over
  the horizon), `prediction_p_alive`; `clv:value` and `bg_bb:value` → also `prediction_spend`,
  `prediction_clv`.
- The rows must hold the card's `inputs` (number columns), and are checked as the fit's are: a
  row that breaks a rule of «The input» gets NULL predictions, counted and said on the run page
  (a refusal of the whole step would block a scoring run for one bad row; the fit refuses because
  its numbers depend on every row).
- **Where it is computed**: in the group's Job, `predict_clv` / `predict_bg_bb` in the runner, from
  the card, with the same CLVTools functions, GSL routine and quadrature as the fit. The engine
  cannot compute ₂F₁ in SQL; the runner is where those functions are. **Checked against the fit**:
  predicting the training rows from the card gives the fit's `pred_*` columns **bit for bit** (the
  same functions on the same numbers), a test of each node in the self-test.
- **What «new customers» means**: customers with a history (x, t_x, T measured to their own
  calibration end); a customer with no purchase yet has no row (CLVTools' `newcustomer` is not
  in this release).

## Reference values

**Data in the repository: synthetic only** (`reference/data/*.csv`, written by
`make_reference.R` from the models' own generative processes with fixed seeds, as the other
groups' are). The CDNOW data (Fader & Hardie 2001) ship inside CLVTools (`cdnow`, 6,696 daily
rows of 2,357 customers) and BTYD (`cdnowSummary`, and `donationsSummary` for BG/BB) under the
packages' GPL-3, with no licence of their own stated (checked online and in the packages' help):
they are **not copied** into Tabularia's repository; `make_witness.R` loads them from the
installed packages at run time, outside the image, to check the published values.

| File | Rows | What it tests |
|---|---|---|
| `clv_bgnbd.csv` | 400 | BG/NBD (r 0.25, α 4, a 0.8, b 2.5), T uniform 20–52 weeks **in whole days** (/7), a 26-week holdout, Gamma–Gamma spend (p 6, q 4, γ 15) independent of x: the main case, the route-A proof |
| `clv_pnbd.csv` | 400 | Pareto/NBD (r 0.55, α 10, s 0.6, β 12), whole days |
| `clv_mbgnbd.csv` | 400 | MBG/NBD |
| `clv_equal_age.csv` | 300 | one cohort: every T = 39 |
| `clv_dependent_spend.csv` | 400 | m̄ rising with x: `spend_independence` failed |
| `clv_no_dropout.csv` | 300 | almost no dropout: `boundary` doubtful (`boundary_no_dropout`) |
| `clv_huge.csv` | 50 | customers with x = 5,000 and 20,000, m̄ up to 1e7: the quadrature path (`cet_quadrature`), finite likelihood |
| `clv_names.csv` | 30 | column names that look like code (`` a`b ``, `x); system("id")`, `ü ñ`) |
| `bgbb_donations_like.csv` | 1,000 | BG/BB (α 1.2, β 0.75, γ 0.66, δ 2.78), n = 6, a 5-opportunity holdout, spend |
| refusals | 1–25 each | all x = 0; one customer; fewer than `min_customers`; t_x > T; x > 0 with t_x = 0; a duplicated id; spend 0 for a repeater; q ≤ 1 (spend drawn with q = 0.8); BG/BB x > t_x, t_x > n, n = 0 |

**Checks and tolerances**:

| Check | Where | Tolerance | Measured on 2026-11-20 |
|---|---|---|---|
| The runner (route B) against CLVTools' public `pnbd()`, `bgnbd()`, `gg()` on the transaction log rebuilt from a whole-day reference case (route A) | self-test | relative 1e-9 | 0 on CDNOW |
| P(alive), CET, PMF of Pareto/NBD and BG/NBD (CLVTools' functions) against `BTYD` 2.4.3's | witness | relative 1e-12 | ≤ 2.4e-14 |
| MBG/NBD (runner) against `BTYDplus` 1.2.0 (k = 1): likelihood, P(alive), CET, PMF; the fit | witness | 1e-12; the fit's parameters 1e-6 | ≤ 2.6e-14; the fit equal to 6 digits |
| BG/BB (runner) against `BTYD`: likelihood, P(alive), CET, PMF; the fit on the donations | witness | 1e-12; parameters 1e-6 | ≤ 1.8e-15; equal to 6 digits |
| Gamma–Gamma against `BTYD::spend.EstimateParameters` | witness | 1e-6 | equal to 6 digits |
| GSL's ₂F₁ against `hypergeo` 1.2-15 on a grid (a up to 150, z from −0.5 to 0.995) | witness | relative 1e-9 | ≤ 1.1e-11 |
| The Gauss–Jacobi CET against GSL's where GSL works | self-test | relative 1e-10 | ≤ 1.0e-12 |
| The discrete CLV over 100 years against CLVTools' DERT × e^(−δ/2) (Pareto/NBD, weekly) | witness | relative 1e-4 | 1e-5 |
| Predict on the training rows against the fit's rows | self-test | bit for bit | — |
| Counts, classes, verdicts, texts, refusals | self-test | exact | — |

**Published values** (`make_witness.R`, on the data loaded from the packages; tolerance relative
1e-3 on the parameters, 0.05 on the log-likelihood: the articles print three or four digits, and
Pareto/NBD's s and β lie on a flat ridge — CLVTools' β is 11.664, the MATLAB note's 11.656):

| Model, data | Published | Measured (route B, or the runner's own code) |
|---|---|---|
| BG/NBD, CDNOW 1/10th sample, 39 weeks (Fader, Hardie, Lee 2005, *Marketing Science*) | r 0.243, α 4.414, a 0.793, b 2.426, LL −9,582.4 | 0.242598, 4.413684, 0.792990, 2.426167, −9,582.429 |
| Pareto/NBD, the same (Fader, Hardie 2005 note, «Implementing the Pareto/NBD model»; `BTYD`'s `cdnowSummary$est.params`) | r 0.5534, α 10.5802, s 0.6061, β 11.6562, LL −9,595.0 | 0.55327, 10.57780, 0.60602, 11.66391, −9,594.976 |
| Gamma–Gamma, the same repeaters (Fader, Hardie 2013 note, «The Gamma–Gamma model of monetary value») | p 6.25, q 3.74, γ 15.44; corr(x, m̄) 0.11 | 6.24935, 3.74426, 15.44431; 0.114 |
| BG/BB, the donations, 11,104 donors, n = 6 (Fader, Hardie, Shang 2010) | α 1.204, β 0.750, γ 0.657, δ 2.783, LL −33,225.6 | 1.203507, 0.749767, 0.656757, 2.783887, −33,225.581 |
| MBG/NBD | no published value on public data (Batislam et al. used a private grocery panel) | the `BTYDplus` witness only |

The published numbers above are as this spec's author recalls them from the articles and notes;
lot B2 checks each against its source before writing it into `make_witness.R` (BG/NBD and
Gamma–Gamma agree with the measurement to their last printed digit; BG/BB's δ by 3e-4 relative,
Pareto/NBD's α by 2e-4 and β by 7e-4: all within the 1e-3).

`witness.json`: `BTYD@2.4.3` (GPL-3, on CRAN, last updated 2021), `BTYDplus@1.2.0` (GPL-3,
**archived** by CRAN on 2025-06-13: installed from the CRAN Archive's tarball), `hypergeo@1.2-15`
(GPL-2), and the recipe's CLVTools. `make_witness.R` runs in a scratch R library with GSL, as
measured here, never in the group's image.

**Edge cases** (each a reference case or a self-test):

| Case | Behaviour |
|---|---|
| Nobody repeats (every x = 0) | refusal `no_repeaters`: the likelihood has no maximum (measured: `L-BFGS-B` fails) |
| One customer, or fewer than `min_customers` (20) | refusal `few_customers` |
| Fewer than `min_repeaters` (5) repeaters | refusal `few_repeaters`; for the spend, fewer than 5 repeaters with a spend: refusal `few_spenders` |
| Every T equal (a cohort) | fitted normally (a reference case) |
| x of 5,000–20,000, m̄ of 1e7 | likelihood finite (measured: −0.52 … +83,950 per customer, a log-density); CET by quadrature; the CLV a finite number |
| t_x = T | allowed |
| a = 1 exactly (BG/NBD, MBG/NBD), γ = 1 (BG/BB) | the quadrature, the limit form |
| The optimizer's overflow | the Nelder–Mead fallback |

## Refusals

Through `tab_refuse(code, message, params)`: `error.json`, the log line `TABULARIA_ERROR {json}`,
exit 3; the step fails with the sentence (the models spec, «The runner protocol»). The catalogue's
own refusals (a missing key, `holdout_needs_length`) come from `check_params`, in the gateway and
the engine, as for every model.

| Code | Sentence (English; the step shows it as written) |
|---|---|
| `duplicate_customer` | «the customer 4812 appears on 3 rows: one row per customer; group the transactions by customer first» |
| `frequency_not_whole` | «frequency must count purchases: the customer 17 has 2.5» |
| `negative_value` | «recency, age and frequency are 0 or more: the customer 17 has a recency of −3» |
| `recency_after_age` | «the customer 17 bought last (recency 41) after the end of the period (age 39): recency is at most age» |
| `recency_without_purchase` | «the customer 17 has no repeat purchase but a recency of 12: recency is 0 without repeat purchases» |
| `same_moment` | «the customer 17 has 2 repeat purchases at recency 0: purchases of the same day must be one purchase; group by customer and day first» |
| `age_zero` | «the customer 17 has an age of 0: age is the time since the first purchase, more than 0» |
| `spend_not_positive` | «the customer 17 has an average spend of 0 with repeat purchases: the spend model needs amounts above 0; leave out returns and refunds, or those customers» |
| `bgbb_frequency` | «the customer 17 has 4 transactions in 3 opportunities up to its last: at most one transaction per opportunity» |
| `bgbb_opportunities` | «opportunities run from 1 to 1,000: the customer 17 has 0» |
| `holdout_negative` | «holdout purchases are whole numbers, 0 or more: the customer 17 has −1» |
| `few_customers` | «12 customers: these models need at least 20» |
| `no_repeaters` | «no customer bought again: the models cannot tell the purchase rate from the dropout without repeat purchases» |
| `few_repeaters` | «3 customers bought again: these models need at least 5» |
| `few_spenders` | «3 repeat customers have a spend: the spend model needs at least 5» |
| `spend_no_mean` | «the spend model's q is 0.83, at most 1: the average spend has no finite mean, and the CLV cannot be computed; look for extreme amounts, or leave out the spend column» |
| `output_column_exists` | «the rows already have a column «pred_clv»: rename it before the node» |
| `clvtools_missing` | «the image's CLVTools lacks pnbd_nocov_LL_sum: rebuild the group» (a fault of the image, said as one) |

Every refusal names one customer and the count of customers that break the same rule.

## Performance and limits

| Limit | Default | Where | Beyond it |
|---|---|---|---|
| Customers (`max_rows`) | 1,000,000 | group setting | the common sentence |
| Memory, CPU | 2 GiB, 2 CPU (request half) | group setting | the Job's exit 137, as today |
| `min_customers`, `min_repeaters` | 20, 5 | catalogue | refusals |
| `max_opportunities` (BG/BB n) | 1,000 | catalogue | refusal |
| `max_horizon` | 1,000 periods (exists) | catalogue | the form's bound |
| `max_clv_steps` | 520 | catalogue | coarser steps, note `clv_steps` |
| `max_top_customers` | 20 | catalogue | — |
| `palive_grid_frequency` | 10 | catalogue | — |
| Chart points | `max_chart_points` (2,000) | runner | as today |
| Time | 1,800 s; the node's timeout within it | group, node | the Job's deadline |

**Measured** (scratch container, one core): Pareto/NBD on 100,000 synthetic customers, all
summaries distinct: the fit 22.7 s, the Hessian 11.0 s; 52 CET evaluations on 100,000 customers
3.2 s. CDNOW (1,016 distinct summaries of 2,357 customers): the two fits 0.55 s. So `auto` on
1,000,000 customers is a few minutes, within the 1,800 s; real summaries in whole days compress far
more than the synthetic ones. B9 measures on kind and writes it here.

## Languages, catalogue, documentation, assistants

- **Five languages** (it, en, de, fr, es) for the report's words (`report_texts/*.json`, checked by
  the existing catalogue test that every model has its words in five languages) and the form
  (`frontend/i18n/locales/*.gen.ts`, namespace `models`: `models.clv`, `models.bg_bb`, their keys
  and choices; `models.groups.customer_lifetime`; `modelsAdmin` for the panel's description).
- **Catalogue**: `GROUPS["customer_lifetime"]`, `MODELS["clv"]`, `MODELS["bg_bb"]`, the new kinds,
  `LIMITS` (`min_customers`, `min_repeaters`, `max_opportunities`, `max_clv_steps`,
  `max_top_customers`, `palive_grid_frequency`), `SECTION_SHAPES["customer"]`, `PREDICT`'s entries;
  then `python -m app.engine.models.catalog` and `model_catalog.py`, and `--check` green. The
  catalogue files are shared with the survival group's work that runs now: B0 rebases on it and
  adds its blocks without touching theirs.
- **`docs/capabilities/models.md`**: a section «Customer lifetime value (R)»: when to use these
  models (non-contractual customers, no churn label), the two nodes, the summary and how to build
  it («Building the summary»), every parameter with its default, the five models in plain words,
  the CLV formula, the holdout, the checks and how to read them, Predict, the limits; an example
  on the pasta maker's sample data (`orders` → one row per customer).
  `docs/specs/statistical-models.md` gets a one-line pointer to this file **after** the survival
  lane has finished with it.
- **Assistants** (`gateway/app/services/ai_flow_agent.py`, prompt only; `gateway-go/internal/
  aiprompts/prompts.json`, `internal/mcp/tools.json` regenerated): the nodes come with the
  catalogue; a paragraph: these nodes take one row per customer, never transactions — build the
  summary first with Group by as the documentation shows; `clv` for purchases at any time,
  `bg_bb` for fixed occasions (a yearly campaign); set `time_unit`; prefer a holdout when the data
  allows; the CLV is of revenue unless a margin is set. The data assistant's: P(alive) is a
  probability from the model, not an observed churn.
- **`CHANGELOG.md`** (the next minor), `README.md`'s map, the capabilities map.
- **Administration**: nothing to build beyond the group's card (the panel lists the shipped groups
  from the catalogue) and its system library line.

## Commands

```bash
# the catalogue: the snapshot, then its copies and their check (as in the models spec)
docker run --rm --user "$(id -u)" -v "$PWD/backend:/app" -w /app tabularia-backend:ci python -m app.engine.models.catalog
python3 .github/scripts/model_catalog.py && python3 .github/scripts/model_catalog.py --check
# the group's image, once (B0), not while another group's image builds
cd gateway-go && go run ./cmd/modelgroup dockerfile customer_lifetime > /tmp/customer_lifetime.Dockerfile
docker build -f /tmp/customer_lifetime.Dockerfile -t tabularia-models-customer_lifetime:dev /tmp
go run ./cmd/modelgroup code customer_lifetime <dir>
docker run --rm --read-only --tmpfs /tmp -v "<dir>:/tabularia/code:ro" tabularia-models-customer_lifetime:dev \
  Rscript /tabularia/code/runner/run.R --selftest
# the reference values (inside the image) and the witnesses (a scratch library with GSL, BTYD, BTYDplus, hypergeo)
docker run --rm -v "$PWD/gateway-go/internal/modelgroups/groups/customer_lifetime/reference:/ref" \
  tabularia-models-customer_lifetime:dev Rscript /ref/make_reference.R
R_LIBS_USER=<scratch library> Rscript gateway-go/internal/modelgroups/groups/customer_lifetime/reference/make_witness.R
```

## Boundaries

As in the models spec, plus:

- **Always**: the summary as input; the CLVTools functions bound by name and proved by the
  self-test against CLVTools' public fit; synthetic reference data in the repository.
- **Ask first**: the recipe field `system_libraries` (a change of how R images are built, even if
  only this group uses it); the `heatmap` scale; any package outside the recipe's closure.
- **Never**: a transaction log built inside the runner; CDNOW or the donations copied into the
  repository; a serialized R object loaded by Predict.

## Success criteria

1. On kind, an administrator enables *Customer lifetime*: it builds (GSL included), its self-test
   passes, *Customer lifetime value* and *BG/BB* appear in the palette.
2. After a flow that builds the summary from the sample orders, *Customer lifetime value* with
   `auto` and a spend column gives one row per customer with P(alive), expected transactions,
   expected spend and CLV; the Report shows the checks on page 1, the comparison of the three
   purchase models, the fit, P(alive), the spend and its independence check, the CLV and its top
   customers.
3. The runner's estimates equal CLVTools' `pnbd()`, `bgnbd()`, `gg()` on the same customers within
   1e-9; MBG/NBD and BG/BB equal `BTYDplus` and `BTYD`; the published values hold within 1e-3.
4. A wrong summary (a duplicated id, t_x > T, same-day repeats, no repeater) stops with a sentence
   that says what to change.
5. A flow run logs to MLflow (the parameters as metrics) and registers a version; a Predict node
   scores other customers' summaries, and on the training rows gives the fit's numbers bit for bit.
6. On clean simulated data no check is «failed» in more than 1 % of the replicates.
7. The other groups' Dockerfiles are byte for byte as before; nothing else changes.

## The lots B0–B10

The same rules as the boosting lots (the models spec, «The boosting group's lots»): **B0 first,
alone**; then lanes on disjoint files; **one heavy job at a time** on this machine (16 GB) — an
image build, the engine suite, the gateway suite, B4's simulations, a kind run — and none while
the survival lane builds or runs its suites; small data in every acceptance. **The image is built
once, in B0**: the runner travels in the ConfigMap, so B1–B4 run their self-tests on that image
with `cmd/modelgroup code customer_lifetime <dir>` mounted; only a change of `recipe.json` asks
for another build. The «Agent» column follows the project's rule (sonnet for the medium work,
haiku for the simple, opus for the difficult and the review).

```text
B0 contract, recipe with GSL, Go plumbing, stubs, fixtures, the one image build (first, alone)
 ├── B1 clv_shared.R (input checks, optimizer, SE, Gamma–Gamma, CLV, holdout, chi-square, card, rows)
 │    ├── B2 clv: Pareto/NBD and BG/NBD through CLVTools, MBG/NBD, auto, ₂F₁ fallback ─┐
 │    └── B3 bg_bb                                                                     ├── B4 checks tuned on clean simulations
 ├── B5 report: layouts and words (fixtures from B0; real outputs after B2, B3) ───────┤
 ├── B6 the nodes in the editor ───────────────────────────────────────────────────────┤
 └── B7 MLflow and Predict (after B1's card; end to end after B2) ──────────────────────┤
                                                                                       ▼
                                   B8 docs and assistants ── B9 on kind ── B10 final review
```

| Lot | Objective | Files (owned) | Depends on | Accepted when (small data) | Check | Heavy? | Agent |
|---|---|---|---|---|---|---|---|
| **B0** Contract and plumbing | Catalogue entries (group, two models, kinds, `applies`, `requires`, `disjoint`, variants `purchases` and `value` with their sections, charts, outputs; `PREDICT`; `LIMITS`; `SECTION_SHAPES["customer"]`); `card.schema.json`'s `customer_lifetime`; a results fixture per model and variant; `recipe.json` with `system_libraries`; `run.R` and three stub files that refuse «not built yet»; the recipe field in `groups.go` and `scriptimage.Settings`, its use in the managed build (`jobs_script_builds.go`, `models_build.go`) and `cmd/modelgroup`; **the one image build**, its time written here | `backend/app/engine/models/{catalog,results}.py`, `catalog.json` and its three copies, `backend/tests/test_models_catalog.py`, `test_models_results.py`, `backend/tests/fixtures/models/{clv,bg_bb}_*.json`, `params_cases.json`; `gateway-go/internal/modelgroups/` (`groups.go` and tests, `card.schema.json`, `groups/customer_lifetime/`); `internal/scriptimage/` and tests; `internal/api/{jobs_script_builds,models_build}.go` (the lines named); `internal/modelcatalog/`; `cmd/modelgroup/` | the survival lane's catalogue changes merged first | `model_catalog.py --check` green; `results.py` accepts the four fixtures and refuses an undeclared key; the other R groups' and the scripting environments' Dockerfiles byte for byte as before; the image builds, `library(CLVTools)` loads, `run.R --selftest` passes the common tests and exits 4 when one is made to fail; `go test ./internal/...` | catalogue commands, `go test`, the image | **yes**: the build (wait for the survival lane's) | sonnet |
| **B1** The shared module | `clv_shared.R`: reading and every rule of «The input» with its refusal; compression; the optimizer wrapper (settings per model, the Nelder–Mead fallback); Hessian, standard errors, intervals; Gamma–Gamma (fit through `gg_LL`, expectation, `spend_no_mean`); per-period discounting and the CLV sum with `max_clv_steps`; holdout numbers; the χ² with pooling; the P(alive) grid and curves; histograms, points, concentration, top customers; the card; the rows; `predict` core | `groups/customer_lifetime/runner/R/clv_shared.R`; its tests in `TAB_COMMON_TESTS` | B0 | Its tests pass in the image: each refusal with its sentence on a tiny table; Gamma–Gamma equals `gg()` on a whole-day case (1e-9); the CLV sum equals a hand computation on 3 customers; pooling of classes; caps | local self-test | moderate | sonnet |
| **B2** `clv` | `fit_clv`, `predict_clv`: Pareto/NBD and BG/NBD through the bound CLVTools functions with their exact settings, MBG/NBD written here (likelihood, P(alive), CET, PMF), `auto` and `compare`, GSL's status and the Gauss–Jacobi fallback; the reference cases, `make_reference.R` (route A in the image), `make_witness.R` (BTYD, BTYDplus, hypergeo, the published values checked against their sources) | `runner/R/clv.R`; `reference/data/clv_*.csv`, `expected.json` (its cases), `make_reference.R`, `make_witness.R`, `witness.json` | B1 | Every row of «Reference values» for `clv`; route B equals route A at 1e-9; Predict on the training rows bit for bit; the huge case uses the quadrature and stays finite; witnesses green | local self-test, witness | moderate (witness: a scratch library, once) | **opus** |
| **B3** `bg_bb` | `fit_bg_bb`, `predict_bg_bb`: likelihood, P(alive), CET (and its γ = 1 limit), PMF; its cases and witness lines | `runner/R/bg_bb.R`; `reference/data/bgbb_*.csv`, its cases in `expected.json`, its part of `make_witness.R` | B1 | BG/BB rows of «Reference values»; the donations' published values in the witness; Predict bit for bit | local self-test, witness | moderate | sonnet |
| **B4** *dropped 2026-11-20: verdicts come only from tests with a p-value, against the node's alpha* — The checks, tuned | The eight checks with their advice ids; **simulations of clean data** (200 replicates × n = 300 and 3,000 per model, generated by the fitted models, `seed` fixed) measuring each check's «failed» and «doubtful» rates; thresholds set so that «failed» ≤ 1 %, «doubtful» ≤ 10 %, and CDNOW and the donations are not failed; the measured rates written in this spec's «As built» | `clv_shared.R` (the checks' block), `reference/simulate_checks.R` (outside the self-test: run by hand, its output kept) | B2, B3 | The rates table written; each check at both sides of each threshold in the self-test | the simulation, local self-test | **yes**: the simulations | **opus** |
| **B5** Report | `layouts/{customer_lifetime,clv,bg_bb}.py`, `report_texts/{customer_lifetime,clv,bg_bb}.json` in five languages | `backend/app/engine/models/layouts/`, `report_texts/`, `backend/tests/test_models_layouts.py` | B0 (fixtures); B2, B3 for the real outputs | Every fixture renders in five languages and passes `reports.definition.check`; the first page holds the checks; without a holdout the sentence replaces the chart; the golden PDFs of the other models unchanged | `ci_tests.sh engine` | **yes**: the engine suite | sonnet |
| **B6** The nodes in the editor | The palette's subtitle, two icons, the form's new kinds (existing controls), `compare` hidden with `auto`, the holdout pair, the five languages of `models.clv`, `models.bg_bb`, `models.groups.customer_lifetime` | `OpSidebar.vue`, `useOpIcons.ts`, `ModelForm.vue` (only if a kind needs it), `frontend/i18n/locales/*.gen.ts` (namespace `models`), `frontend/tests/model-*.test.mjs` | B0 | Chrome and Firefox: find, drop and configure both nodes with the mouse and with the keyboard alone; `holdout_transactions` without `holdout_length` refused with its reason | `vue-tsc`, `npm test`, browser | no | haiku |
| **B7** MLflow and Predict | `tabularia.variant` and `tabularia.purchase_model` tags; the `PREDICT` entries live (`horizon` for the two models, the columns); the Predict form's horizon for these models; rows breaking a rule get NULL and a count | `gateway-go/internal/api/{jobs_models_mlflow,jobs_predict}.go` (tags, only if not data-driven), `internal/modelcatalog/` tests, `frontend/components/ui/PredictForm.vue`, `predict_*` in B2/B3's files | B1 (end to end: B2) | `go test`: the columns per `model:variant`; on kind after B2: a registered `clv` version applied by Predict reproduces the fit's rows | `go test`, `vue-tsc`, `npm test`, kind (with B9) | no | sonnet |
| **B8** Documentation and assistants | «Languages, catalogue, documentation, assistants»; the pointer in the models spec once the survival lane is done | `docs/capabilities/models.md`, `CHANGELOG.md`, `README.md`, `docs/specs/statistical-models.md` (one line); `gateway/app/services/ai_flow_agent.py` (prompt), `internal/aiprompts/`, `internal/mcp/` (regenerated) | B2–B7 | An assistant places a configured `clv` node after a summary built with Group by; the site builds, links hold | `ci_tests.sh gateway`, `go test`, site commands | the gateway suite | haiku (docs), sonnet (prompts) |
| **B9** On kind | Enable *Customer lifetime*: Building → Checking → Ready with its lock and GSL; a reference value altered on a scratch branch leaves it Failed; both nodes, both variants: Run, Table, Report, PDF, Excel; a flow run logged to the test MLflow server; Predict; durations on the reference tables and on 100,000 generated customers written here | — | all | All green, Chrome and Firefox | the suites, browser | **yes**: the cluster build | sonnet |
| **B10** Final review | This spec against the code; the checks' false-alarm rates; the bound CLVTools functions; the refusals' sentences | — | B9 | Corrections committed, «As built» written | the suites | — | opus |

**In parallel**: after B0, B1 ∥ B5 (on fixtures) ∥ B6; after B1, B2 ∥ B3 (B7's Go part may start
on B1's card). **In sequence**: B0 alone; B4 after B2 and B3; B8 after B2–B7; B9, then B10.
**Shared files**: the catalogue and its copies, `card.schema.json`, `SECTION_SHAPES` and
`frontend/i18n/locales/*.gen.ts` are also edited by the survival lane: each lot rebases on it and
writes only its own blocks and namespace, then regenerates the copies.

## Open questions for the owner

Only what this spec cannot settle; each with the recommendation the lots follow if the owner says
nothing.

1. **GSL in the image: a recipe field or every R image.** CLVTools needs `libgsl-dev`, which the
   fixed list of R system libraries lacks. *Options:* (a) a recipe field `system_libraries`, used
   by this group only, the other images unchanged; (b) add `libgsl-dev` to `RSystemLibraries`,
   which changes every R image's Dockerfile (all R groups and the R scripting environments: 10 MB
   each, and a rebuild when their recipe next changes). *Recommendation:* (a).
   **Decided 2026-11-20: (a), a recipe field `system_libraries` used by this group only; the other images stay byte for byte as before.**
2. **Relying on CLVTools' unexported functions.** Route B calls `pnbd_nocov_LL_sum` and the others,
   documented in CLVTools' manual but not exported. They are bound by name at the pinned 0.12.1,
   and the self-test compares the result with CLVTools' public `pnbd()`, `bgnbd()`, `gg()` on a
   rebuilt transaction log, so a later version that changes them fails the build's check instead
   of the numbers. *Alternatives:* route A (public API only; exact only for summaries in whole
   days, Σ(x + 1) rows, no «month» unit), or likelihoods written in the runner for all five (no
   CLVTools, no GSL; against the owner's choice of library). *Recommendation:* route B as written.
   **Decided 2026-11-20: route B as written; the unexported functions are bound by name at the pinned version and protected by the self-test.**
3. **The P(alive) heatmap's colour scale.** `heatmap` colours from the data's own minimum to its
   maximum; a fixed 0–1 scale needs `heatmap` to honour the existing `options.min` and `max` (a
   one-line change to an existing chart kind, «Ask first»). *Recommendation:* make it, for every
   heatmap that sets them (none does today, so nothing else changes); until then the cells carry
   their values.
   **Decided 2026-11-20: make it; `heatmap` honours `options.min` and `max` when they are set, and the P(alive) map has a fixed scale from 0 to 1.**
4. **`output = rows` by default.** These nodes exist for their per-customer scores; every other
   model defaults to `results`. *Recommendation:* `rows` for these two.
   **Decided 2026-11-20: `rows` by default for these two nodes.**
5. **`auto` chooses by the log-likelihood, never by the holdout.** With a holdout one could choose
   the model that predicts it best. *Recommendation:* the log-likelihood (equal to AIC's and BIC's
   choice here), so that the holdout stays a check the choice did not see; the comparison table
   shows the holdout error of each for whoever wants to choose by hand.
   **Decided 2026-11-20: `auto` chooses by the log-likelihood, never by the holdout.**
6. **q ≤ 1 stops the node.** Gamma–Gamma's mean spend is then infinite, so no CLV exists; the
   purchase part would still be valid. *Options:* stop (as written: a CLV node without a CLV is a
   surprise), or continue with the `purchases` outputs and a failed check. *Recommendation:* stop,
   with the sentence that says to remove the spend column to get the purchase model alone.
   **Decided 2026-11-20: q ≤ 1 stops the node with a sentence that says to remove the spend column to get the purchase model alone.**
7. **The defaults that are business choices.** `discount_rate` 10 % a year, `margin` 1 (a CLV of
   revenue, said on page 1), `horizon` one year. *Recommendation:* keep them; the report writes all
   three on the first page.
   **Decided 2026-11-20: discount 10 % a year, margin 1 and horizon one year, written on the report's first page.**

**As built, B7** (2026-11-20; commits `34759b07` (MLflow and Predict) and `a7f4ff58` (the Administration card), on `main`, not pushed):

- **MLflow** (`jobs_models_mlflow.go`; the logging is data-driven, nothing per node): the results of `clv` and `bg_bb` are read back as for every model, so the **metrics** are every number of the `model` section (`param_*`, `log_lik`, `clv_total`, `holdout_rel_error`, `horizon`, `discount_rate`, `margin`…), every number of the `candidate` section as `candidate.<model>.<stat>` (the three purchase models of `auto` and `compare`, `chosen` with them) and the checks' `assumption.<check>[.<level_2>].statistic|p_value`, **and `.value`**: a customer lifetime result (it has the text `purchase_model`) logs the `value` of its checks, which the boosting models alone logged until now (the holdout, `horizon_length`, `frequency_fit`'s share gap are measures with no verdict). The per-customer rows, the charts' points and the `coef` table are not metrics (the table goes to `tabularia/coefficients.csv`, the rows to `results.parquet`).
- **Parameters**: the node's checked parameters with their defaults (so unit, `purchase_model` as asked, `discount_rate`, `margin`, `alpha`, `horizon` when given); and, new, **`purchase_model_chosen`** (the model section's `purchase_model`: what `auto` picked) and **`horizon`** (the horizon used, from the results, only when the node left it empty).
- **Tags and files**: the run's tags and artifacts are unchanged (`native.rds` does not exist for this group: nothing to upload). The registered version has `tabularia.model`, `group`, `group_version`, `card_sha256`, `tabularia.variant` (`purchases` | `value`, already read from the card) and, new, **`tabularia.purchase_model`** (the card's `customer_lifetime.purchase_model`, the one used).
- **List's main number** (`models_mlflow.go`, B0's note): `clv` and `bg_bb` show `log_lik`, then `aic`. The choice is mine (the spec names none): it is the number `auto` chooses by; it is comparable only on the same data.
- **Predict** (`jobs_predict.go`): the options, the columns and the Job were data-driven since B0 (`CheckPredict`: `horizon` and nothing else for the two models, refused with «applies to arima, clv, bg_bb» for the others; `PredictColumns`: `prediction`, `prediction_p_alive` and, with a spend part, `prediction_spend`, `prediction_clv`; the clash with a column the card reads, `inputs`, is refused before any run). New: for the customer lifetime group the card is checked (`customerLifetimeProblem`): it must hold `customer_lifetime.purchases`, name the node the version says, hold `spend` exactly when its variant is `value`; the refusals say «holds no customer lifetime parameters (customer_lifetime.purchases): fit the model again», «holds the parameters of the node «bg_bb», but the version says «clv»» and the two about the spend part. A card of another group, or a version whose group tag disagrees with its model, was refused already («says the group …», «says it belongs to the group …»). The rows that break a rule of the summary get NULL and the runner's warning («N rows break a rule of the summary: their predictions are NULL») reaches the run page through `run.json`'s warnings, as the other models' counts do. A column of the rows that the model does not read and Predict would overwrite is the runner's refusal (`output_column_exists`), as for the other models.
- **Predict's form**: the horizon field is the catalogue's (`applies_to`), so it appears for the two models by itself; its hint now says, in five languages, that for the customer lifetime models it counts periods ahead in the unit the model was fitted in. `PredictForm.vue` needed no change.
- **Administration card, the «GSL 2.7.1» line** (corrected the same day: the version is **not** written in Go): the groups' answer (`GET /admin/models`, `/admin/models/{id}`) has `system_libraries`, a list (empty, not null, for the groups with none) of `{package, name, version}`; the card says «System library: GSL 2.7.1+dfsg-6ubuntu2 (Debian)» after the number of packages, in five languages (`modelsAdmin.systemLibrary`). The **name** is a label in a map (`internal/modelgroups/systemlibs.go`: `libgsl-dev` → «GSL»; a package without a label is shown by its Debian name). The **version** is the build's: the Dockerfile of an image whose recipe has `system_libraries` adds, right after the `apt-get install`, `RUN mkdir -p /opt/tabularia && dpkg-query -W -f '${Package}@${Version}\n' libgsl-dev > /opt/tabularia/system-libraries.txt` (`scriptimage.SystemLibrariesFile`; no other image has the step, the existing test of the other groups' Dockerfiles holds, and `TestOnlyTheCustomerLifetimeImageInstallsGSL` now states the record as the only other difference); the group's check script (`mgCheckScriptWith(language, true)`, only for such groups) does `cat` of that file right after the R lock, so its `libgsl-dev@<debian version>` line is in the version's lock (it matches the lock's line pattern); `mgOut` reads it from the lock of the version in use (also when the recipe has changed since and a rebuild is wanted). Before a build, or for a version built before this step existed (the one B0 built), the card shows «GSL (Debian)» without a version. The version shown is Debian's whole (`2.7.1+dfsg-6ubuntu2`): no parsing. The recipe is untouched, so no version becomes stale.
- **To check in B9 (the build on kind)**: the build log of the `customer_lifetime` image has the `dpkg-query` step and `/opt/tabularia/system-libraries.txt` holds `libgsl-dev@2.7.1+dfsg-…`; the check Job does not fail on the `cat` (`set -e`); the version's lock (Administration, «installed packages») ends its R lines with the `libgsl-dev@…` line and the count includes it; the card of the ready group says «System library: GSL <that version> (Debian)»; a second group (regression) has no such step, line or lock entry; a version built before (no record) shows the name alone. Note that the lock count of this group's «installed» disclosure is one more than the R packages.
- **Tests**: `jobs_models_mlflow_test.go` (the metrics and parameters of a customer lifetime result, with inline rows; the card's purchase model; a whole run on the database that registers a version with `variant` and `purchase_model`; the catalogue's four fixtures `clv-*` and `bg_bb-*` through the same structure check as P7's: every finite candidate number is a metric of the same value, no key over 250, no collision), `jobs_predict_test.go` (the card's rules; a Predict of a registered `clv` version in the group's Job with `horizon` 26 and without; the refusals: option of another model, negative horizon, card without parameters, clash with a column the model reads, group tag of another group, card under another model, group disabled), `models_admin_libraries_test.go`, `modelgroups/systemlibs_test.go`, `scriptimage_test.go`, `groups_test.go`, `jobs_models_test.go` (the check script), `frontend/tests/models-admin.test.mjs`. **None depends on the fixtures' numbers** (B2 regenerates them). `test.sh` all green (`internal/api` with the database 28 s), `npm test` 523 tests, `vue-tsc` no errors.
- **Not done, on purpose**: reproducing the fit's rows with a registered `clv` version on kind (B7's «end to end after B2») waits for `predict_clv` on the real runner and for `predict_bg_bb` (B3: today `tab_not_built`); it is part of B9.

**As built, B6** (2026-11-20; commit `b7d9b531`, together with P6):

- **Palette**: the group *Customer lifetime* (its title is the subtitle) lists *Customer lifetime value* and *BG/BB*;
  `NOT_IN_THE_PALETTE_YET` and the exclusions of the words test are gone. Icons as in P6.
- **The form**: only existing controls (`choice`, `integer`, `number`, `boolean`, `column`). `compare` shows only with a
  chosen purchase model (the `applies` list; `auto` hides it and its `false`), `discount_rate` and `margin` only with a
  `spend` column, `holdout_length` only with `holdout_transactions`; and then **the reason of `holdout_needs_length` is
  said at the length's own box** (`requiredNote` had been drawn for columns only; it now serves the number boxes too),
  beside «Still to set». `time_unit` and `period_unit` (five choices) are the Select; `purchase_model` (four) the radios,
  the arrows moving among them. The node's line says «frequency, recency, age · week · + spend».
- **Words**: `models.clv` and `models.bg_bb` have their own `frequency`, `recency`, `horizon` and `holdout_length` words
  and hints (the shared ones are the time series' «Frequency of the series», «Forecast steps»); the shared
  `models.params.*` for the new keys and their hints; `models.choice.{pareto_nbd,bg_nbd,mbg_nbd}`;
  `models.choices.purchase_model.auto`, `models.choices.time_unit.*` («Days», «Weeks»… not the series' «Daily»…);
  `models.reasons.holdout_needs_length`; in five languages. The name of `clv` in the palette is «Customer lifetime value»
  (it «Valore del ciclo di vita del cliente», which the palette truncates with an ellipsis; the title carries it whole).
- **Not done**: the **«GSL 2.7.1» line on the Administration card**. The card (`ModelsSection.vue`) shows the state, the
  version, the number of packages and the lock; the route does not give the group's `system_libraries` (only a count of
  packages), so the line needs the gateway's route to carry them: a lot of Go, not of the editor. It is a small, separate
  piece for B8 or B9.
- **Tests and browser**: as P6 (the same commit). The keyboard run covered both nodes: the arrows chose the purchase model, Space
  turned the comparison on, the spend brought the discount and the margin, the holdout purchases without a length showed
  the reason, the length cleared it; `bg_bb` has no purchase model and no age.

**As built, B5** (2026-11-20; report of `clv` and `bg_bb`; files `layouts/{customer_lifetime,clv,bg_bb}.py`, `report_texts/{customer_lifetime,clv,bg_bb}.json`, the four fixtures):

- **Pages** (A4, 4 pages): 1 summary (customers and repeaters, purchase model with its log-likelihood, expected purchases over the horizon, total CLV with its mean or, in `purchases`, the mean P(alive)), the formula and under it the **business settings** line (model, unit, horizon, discount, margin, «CLV of revenue»), the checks with their advice, the measures without a verdict, the model's parameters per part; 2 the comparison of the three purchase models (`clv` only, chosen one marked), the numbers of the fit, repeat purchases observed and expected, the holdout by calibration frequency and its totals (without a holdout column the sentence that says how to add one stands in the chart's place); 3 P(alive) (map, curves, distribution), the spend (`value`); 4 the CLV (or the expected purchases): distribution, concentration, top customers, notes.
- **Heatmap**: `charts.py` honours `options.min` and `options.max` when set (the P(alive) map has 0 and 1); a bound that is not set stays the data's. Nothing else changes: no existing report sets them. `tests/test_reports_model.py` has a test of both bounds, of one, and of the default. The chart service needed nothing (the option goes in `visualMap`). The dashboards' own heatmap (`tiles.py`) is not changed.
- **Reading of the fixtures that the runner (B1–B3) must keep**: `hist|clv` edges are read as the numbers the page prints (CLV, or expected transactions), equal on a log scale, one bin for zero with `x0 = x1 = 0`; `hist|frequency` and `hist|holdout` classes by `x0`, the last open (`x1` null) printed «7+»; `cell|p_alive` level = frequency, level_2 = recency class (x = 0 has one cell at the first column).
- **Decision of 2026-11-20 («niente cose hardcoddate»)**: the page writes the node's `alpha` under the checks; `holdout` and `horizon_length` are numbers with a sentence and no verdict (they appear in the measures' table when their rows have a `value` and no `verdict`); `frequency_fit` keeps its verdict from its p-value and shows the share gap as its value. `sample_size`, `spend_tail`, `convergence` and `boundary` are unchanged (facts); if B1/B4 also turn `spend_tail` or `sample_size` into measures, add its id to `MEASURES` in `customer_lifetime.py` and its sentence under `measure_texts`.
- **Words**: the parameters' descriptions per node (`clv.json`, `bg_bb.json`, `parameters.purchases`) and the Gamma–Gamma's (`customer_lifetime.json`, `parameters.spend`); `common._words` merges the family file as for boosting.
- **PENDING** for both nodes is gone. The PDFs (4 fixtures × 5 languages) were drawn and looked at outside the repository.

**As built, B5, languages and notes** (2026-11-20): the formula's «in weeks» is said by `formula_in` and `values.time_unit` (the user's column names stay). **Notes the runner should send as an id and its parameters**, `@id name=value…` in the text of `note_<id>`: `@cet_quadrature n=12`, `@spend_missing n=45`, `@clv_steps max=520 step=2`, `@t_x_clamped n=3`, `@unit_check age=1900 unit=week`, `@optimizer_fallback`; the layout composes them from `note_texts` in the report's language. Numbers are in the language's decimal; the tables' numbers have five decimals at most.

## As built, B1 (2026-11-20)

`groups/customer_lifetime/runner/R/clv_shared.R` (the module), `clv_shared_tests.R` (its self-tests, added to `TAB_COMMON_TESTS`), `reference/make_reference.R`, `reference/data/clv_refusals*.csv` and 17 refusal cases in `expected.json`. The module holds everything B2 and B3 need, so that they are the nodes' wiring, the assumption rows, the fitted reference cases and the witnesses:

- the summary's rules (`clv_read_summary`, `clv_rules`) with every sentence of «Refusals»; Predict uses the same rules and gives NULL to the rows that break one;
- the five models: Pareto/NBD and BG/NBD through CLVTools' compiled functions (bound lazily by `clv_fn`, not when the file is sourced: `clv.R` is sourced before `clv_shared.R`), MBG/NBD and BG/BB written here (likelihood, P(alive), expectation, PMF), GSL's ₂F₁ with its status, the 64-node Gauss–Jacobi quadrature where GSL fails (status, non-finite or negative result, a = 1), BG/BB's limit form at γ = 1;
- the optimizer with CLVTools' own settings (BG/NBD: optimx's defaults, last row), the Nelder–Mead + L-BFGS-B fallback, standard errors by the delta method, intervals on the log scale;
- Gamma–Gamma through `gg_LL`, `spend_no_mean`; the discounted CLV with `max_clv_steps`; holdout numbers; the χ² on pooled classes; the HC3 test; P(alive) grid, curves and histogram; the spend, CLV and concentration charts; the card; the rows; the output rows; `clv_predict_node` (Predict).
- `clv_fit(task, node)` runs summary → candidates → choice → spend → per-customer numbers; `clv_results(task, fit)` gives the rows (all sections but `assumption`), the output and the card. No threshold anywhere: the module computes measures and p-values only.
- Notes are `@id name=value` strings: `cet_quadrature n`, `spend_missing n`, `clv_steps max step`, `t_x_clamped n`, `unit_check age unit`, `optimizer_fallback`, and the new `candidate_failed models`.

**Light by design.** The likelihoods run on the distinct (x, t_x, T) with their counts; the Hessian, standard errors and KKT tests are computed only for the chosen model (`clv_add_inference`; candidates are fitted with `kkt = FALSE`, which does not change the estimates); the quadrature runs only for the customers GSL fails on; the χ² groups continuous ages in 200 classes of equal counts, each at its mean age. Measured on the loaded development machine (load 8, container limited to 2 CPUs; `auto` with a spend column, 52 weeks, rows output): 10,000 customers all distinct 140 s (Pareto/NBD fit 26 s, BG/NBD fit 49 s with optimx's Nelder–Mead then BFGS, MBG/NBD 2 s, 52 expectations of Pareto/NBD 10 s); 10,000 customers in whole days (5,008 distinct) 65 s; 100,000 customers in whole days (42,957 distinct) 199 s; building the rows, card and output 1 s. A plain `optimx` on CLVTools' likelihood for 10,000 distinct customers takes 25 s on the same machine, so these times are the machine's and the settings', not the module's.

**Deviations.** Route B equals route A at 1e-10 (relative, estimates), not 0: CLVTools sums its compressed rows in another order; standard errors agree to 8e-8 (the Hessians are finite differences of different functions). CLVTools' `pnbd_nocov_PMF` returns NaN for probabilities below its terms (k of dozens in a short age): taken as 0. `tab_variant_of` (common) does not understand the catalogue's `when: {spend: true}` for a column name: the reference cases name their variant. `auto` skips a candidate whose parameters leave 1e-10…1e10 (BG/NBD's likelihood is numerical noise there, measured 7e108). Lot B4 is dropped (above).

## As built, B2 (2026-11-20)

`runner/R/clv.R` is the node: `fit_clv` = `clv_fit` → `clv_results` → `clv_checks`; `predict_clv` = `clv_predict_node` (the card's parameters, the fit's functions: the training rows get the fit's numbers bit for bit, B1's self-test). The checks are a new file shared by both nodes, `runner/R/clv_checks.R` (`clv_checks(rows, fit)`: B3 calls it from `bg_bb.R`), with its self-test `clv_checks_tests.R`. `clv_add_inference` keeps the gradient (the convergence check's Newton step). `common/R`: `tab_variant_of` reads a rule of `when` as the engine's `_holds` and the gateway's `holds` (`true` given, `false` not given, a list one of its values), with a test in the common self-test; the regression (63 checks), time series (25) and survival self-tests stay green.

**The checks, as decided on 2026-11-20** (no fixed threshold):

| Check | Kind | Verdict | Numbers |
|---|---|---|---|
| `frequency_fit` | test | p ≥ alpha ok, p < alpha failed; not computed when the pooled classes leave no degree of freedom | `statistic` χ², `df` classes − 1 − 4, `p_value`, `threshold` = alpha, `value` the largest share gap, `count` the classes |
| `spend_independence` | test (`value`) | the HC3 slope's p against alpha; not computed when x or m̄ does not vary | `value` Pearson's r, `statistic` z, `p_value`, `threshold` = alpha, `count` the repeaters with a spend |
| `convergence` | fact | ok: converged and a positive definite Hessian (both parts); failed: not converged, or no positive definite Hessian with every estimate inside its range; **doubtful** (`advice_id` `convergence_edge`): no positive definite Hessian because an estimate is at an edge, where the likelihood is flat | `value` the Newton step left, in standard errors |
| `boundary` | fact | doubtful, one row per estimate below 1e-4 or above 1e4 (`level_2` the parameter, the spend's `spend_p` …), `advice_id` `boundary_no_dropout` (the dropout's first parameter → 0 or its second → ∞), `boundary_homogeneous` (both parameters of a mixing distribution → ∞), `boundary` otherwise; one row ok with `count` 0 when none | `value` the estimate, `count` the estimates at an edge |
| `spend_tail` | fact (`value`) | q > 2 ok, q ≤ 2 doubtful (no finite variance); q ≤ 1 stopped the node before | `value` q, `threshold` 2 |
| `sample_size` | measure | none | `value` the customers, `count` the repeat customers |
| `holdout` | measure (with a holdout column) | none; not computed when nothing was bought in the holdout | `value` (expected − observed)/observed |
| `horizon_length` | measure | none | `value` the horizon over the longest age |

The report: `sample_size` joins `MEASURES` (its sentence in `measure_texts`, and the repeat customers after it: `common.measures` takes an opt-in `counted`); the check is called «Customers and repeat customers»; the H0 and advice that named a threshold are rewritten (sample_size, horizon_length; holdout keeps only its not-computed sentence; `frequency_fit_small` is gone); `convergence` has the advice `convergence_edge`; `frequency_fit` and `spend_independence` say what their count counts; the note `@candidate_failed models=…` has its sentence in `note_texts`. Five languages each.

**Reference cases** (`make_reference.R`, in the image; the CSVs drawn from the models' processes with fixed seeds, in whole days, written with 17 digits; the expected values computed without the runner): A `clv_bgnbd.csv` 400 BG/NBD customers, `auto`, spend, 26-week holdout (the three candidates, the choice, the spend, the HC3 test, the clean verdicts); B `clv_pnbd.csv` 400 Pareto/NBD, the `purchases` variant (P(alive) and the expected purchases against CLVTools' `predict()`); C `clv_mbgnbd.csv` 400 MBG/NBD with a spend, margin 0.4, rate 8 %; D `clv_equal_age.csv` 300 of age 39, BG/NBD, 13-week holdout (`predict()`); E `clv_dependent_spend.csv` (m̄ = (10 + 4x) × log-normal: `spend_independence` failed, p 3e-44); F `clv_no_dropout.csv` (Poisson purchases: MBG/NBD's a at the edge, `boundary_no_dropout`, `convergence_edge`); G `clv_huge.csv` (x = 5,000 and 20,000, spends of millions: `@cet_quadrature n=2`, every number finite); H `clv_names.csv` (names that look like code, unit month); I `clv_spend_no_mean.csv` (q = 0.8: the refusal, q as CLVTools' `gg()` finds it). expected.json is shared: each case has `by`, and a script replaces only its own (B3: `make_reference_bgbb.R` with `by` = its name). The self-test: **52 checks, all green** (41 before, + 2 of the checks, + 9 cases), 49 s on the idle machine, 131 s with the survival lane's self-test beside it.

**Tolerances, as measured** (deviations from «Reference values»): the runner against CLVTools' public `pnbd()`, `bgnbd()`, `gg()` on the log the summary comes from, **relative 1e-7** on the estimates, not 1e-9 (measured at most 8.7e-9 for Pareto/NBD's s and β, 3.4e-8 for Gamma–Gamma, 3.5e-10 for BG/NBD: CLVTools derives t_x and T from the dates, a last-bit difference that moves where L-BFGS-B stops along a flat direction; on the case E, where Gamma–Gamma's p is 178, it is 4e-6, so that case does not compare the spend); log-likelihoods 1e-6 absolute (measured 3e-12); `predict()`'s P(alive) and expected purchases 1e-7 relative (measured 6e-10). **MBG/NBD** against a maximum found here to the end (Nelder–Mead, BFGS, nlminb): log-likelihood 1e-6 absolute (measured 1.5e-7), parameters **1e-3** relative (measured 5.3e-4: the runner's L-BFGS-B, like BTYDplus', stops at its default tolerance on a flat ridge; the witness finds BTYDplus at 4e-10 of the runner on CDNOW and at the same 5.3e-4 from that maximum). CLVTools' own `vcov()` is not a reference: on `clv_pnbd.csv` it differs by 6 % on r's standard error, where the runner's numDeriv Hessian and optimx's `nhatend` agree to 5e-5.

**Witnesses** (`make_witness.R`, the group's image with the scratch library on `R_LIBS_USER`, 68 s, all green): CLVTools' P(alive), CET(39), PMF of Pareto/NBD and BG/NBD against BTYD ≤ 1.8e-14; MBG/NBD against BTYDplus (k = 1): likelihood on 20 random sets 7.1e-15, P(alive) 3.3e-15, CET 2.5e-14, PMF 1.4e-16, the CDNOW fit 4.0e-10; Gamma–Gamma against `spend.EstimateParameters` 3.9e-10; GSL's ₂F₁ on 231 points (35 more where GSL's status is not 0) against the Gauss series 7.4e-14, against hypergeo for z ≤ 0.5 1.2e-14; the discrete CLV over 100 years against DERT × e^(−δ/2) 1.3e-5. **Deviation**: the ₂F₁ grid runs over z from 0.1 to 0.995, not from −0.5: the runner never calls ₂F₁ below 0, and at z = −0.5 with a large a both GSL and hypergeo lose every digit (values near 1e-16 of opposite signs); hypergeo also returns a negative value at a = 60.5, b = 25.2, c = 80.3, z = 0.9, where every term is positive, so the first witness is the series itself (positive terms, Kahan sum), hypergeo the second where z ≤ 0.5.

**Published values, checked against their sources** (read on 2026-11-20 in the PDFs; the check is «agrees to its printed digits or within 1e-3 relative», the log-likelihood to its printed digit):

| Model | Source | Printed | Measured (route B, BTYD's `cdnowSummary`) |
|---|---|---|---|
| BG/NBD | Fader, Hardie, Lee 2005, *Marketing Science* 24(2), Table 2, as quoted in «Implementing the BG/NBD model for customer base analysis in Excel» (brucehardie.com/notes/004) | r 0.243, α 4.414, a 0.793, b 2.426, LL −9582.4 | 0.242595, 4.413602, 0.792920, 2.425888, −9582.429 |
| Pareto/NBD | «A note on implementing the Pareto/NBD model in MATLAB» (brucehardie.com/notes/008), the table of starting values, first row | r 0.5533, α 10.5777, s 0.6062, β 11.6681, LL −9595.0 | 0.553270, 10.577801, 0.606020, 11.663915, −9594.976 |
| Gamma–Gamma | «The Gamma-Gamma model of monetary value» (brucehardie.com/notes/025) | p 6.25, q 3.74, γ 15.44 | 6.249349, 3.744256, 15.444307 |
| BG/BB (for B3) | Fader, Hardie, Shang 2010, as quoted in «Implementing the BG/BB model for customer-base analysis in Excel» (brucehardie.com/notes/010) | α 1.204, β 0.750, γ 0.657, δ 2.783, LL −33,225.6 | — |

**Corrections to «Reference values»**: the Pareto/NBD values the spec recalled (r 0.5534, α 10.5802, s 0.6061, β 11.6562) are not the MATLAB note's; they are close to the BTYD vignette's (0.553, 10.580, 0.606, 11.656, LL −9594.976), and the note's own table moves β from 11.6681 to 11.6806 with the starting values (the ridge). The correlation 0.11 of x and m̄ is **not printed** in the 2013 Gamma–Gamma note: the witness prints the measured 0.1139 as a note, with no published value. BG/NBD, Gamma–Gamma and BG/BB's printed values are as the spec recalled them.

**Fixtures and PDFs**: `clv-value.json` and `clv-purchases.json` are the runner's output on `clv_bgnbd.csv` (auto, 26-week holdout, horizon 52; `value` with the spend), written by `tests.models_fixtures --from-results` (their parameters in `PARAMS`, in `FROM_RUNNER`). The PDFs, 4 fixtures × 5 languages, drawn outside the repository (BG/BB's still on B0's placeholder fixtures, with the new words).

**Left open**: the holdout chart labels its last class «10», not «10+» (its `x1` holds the customers of the class, so the layout's open-class rule does not see it): a line of the layout or of the rows, for B10. The spend chart's expected counts move from bin to bin (observed counts are flat by construction of quantile bins, the expected ones follow each bin's width): correct, but a reader may find it noisy.

## As built, B3 (2026-11-20)

`runner/R/bg_bb.R` is the node: `fit_bg_bb` = `clv_fit(task, "bg_bb")` → `clv_results` → `clv_checks`, `predict_bg_bb` = `clv_predict_node` (as `clv`; B1 had already written the likelihood, P(alive), CET with its γ = 1 limit, PMF, the summary's BG/BB rules and the compressed fit). No stub is left. The checks are the shared ones (`clv_checks.R`): for BG/BB the horizon is counted in opportunities (`horizon_length` = horizon / the most opportunities) and the χ² has classes − 1 − 4 degrees of freedom; no purchase model is chosen, so there is no `candidate` section and no comparison. Verdicts as decided on 2026-11-20: `frequency_fit` and `spend_independence` from their p-value against the node's `alpha`, `convergence` and `spend_tail` facts; `sample_size`, `holdout`, `horizon_length` and now `boundary` are measures.

**Reference cases** (`reference/make_reference_bgbb.R`, in the image; replaces only the cases with `by` = `make_reference_bgbb.R` in `expected.json`, keeps the 26 of B1 and B2 and the file's header; the CSVs `bgbb_*.csv` drawn from BG/BB's process with fixed seeds, written with 17 digits). A `bgbb_donations_like.csv` 1,000 donors (α 1.2, β 0.75, γ 0.66, δ 2.78), 6 opportunities, a Gamma–Gamma gift (6, 4, 15), a 5-opportunity holdout, horizon 5, a year: the value variant; B `bgbb_cohorts.csv` 800 donors with 3 to 12 opportunities (α 0.9, β 1.4, γ 0.4, δ 3.5), the `purchases` variant, a month, the default horizon (12); C `bgbb_no_dropout.csv` 300 donors with θ = 1e-9 and 20 opportunities: γ goes to 4.5e-6, the Hessian is not positive definite, `convergence` doubtful (`convergence_edge`) and the `boundary` measure of γ reads `boundary_no_dropout`. The expected values are computed **without the runner's code**: the likelihood as the sum over the possible moments of dropout, term by term in logs; P(alive) and the expected transactions as sums over the opportunities, not telescoped (so γ = 1 needs no limit); Gamma–Gamma's likelihood; Pearson's r and the HC3 test through `lm()`; the CLV from its definition; all maximized to the end (Nelder–Mead, BFGS, nlminb). Counts, verdicts and texts exact.

**Tolerances, as measured** (deviation from «Reference values», where the BG/BB fit was to be equal to 6 digits to BTYD's): the runner's L-BFGS-B stops at its default tolerance on the ridge of BG/BB's four parameters, so against the maximum found here to the end the parameters differ by at most **2.3e-4 relative on A and 2.1e-3 on B** (the dropout's δ and γ; their standard errors are 50 % and more), tolerance **5e-3**; the log-likelihood by 2e-7 and 7e-6 absolute, tolerance **5e-5**; the sums over the customers (expected transactions, P(alive), the CLV, the discounted transactions, the holdout's expected) by at most 2.4e-5 (A) and 1.7e-4 (B) relative, tolerance **1e-3** (the holdout's relative error, a small difference of two numbers, 1e-2); Gamma–Gamma's parameters by 1.7e-5 relative (5e-3), its log-likelihood 3e-8 absolute; the HC3 test and Pearson's r to 1e-15. On the donations of BTYD the runner's fit equals BTYD's own to 2.7e-10 (below), so the ridge costs nothing there: it is these small synthetic cases, with 6 opportunities and a thousand donors, that do not pin the optimum.

**Witness** (`make_witness.R`, the BG/BB block at the end; the scratch library with GSL, BTYD, BTYDplus and hypergeo on the group's image, 5 minutes on the loaded machine, **0 failed**): the donations (`donationsSummary`, 11,104 donors, 22 rows of the recency–frequency matrix, expanded and never written to the repository) fitted by the runner: α 1.203507, β 0.749767, γ 0.656757, δ 2.783887, LL −33,225.581, against the published α 1.204, β 0.750, γ 0.657, δ 2.783, LL −33,225.6 (within the printed digits or 1e-3 relative, the LL within 0.05); the runner's log-likelihood on 20 random parameter sets 1.8e-15 absolute against `bgbb.LL`, P(alive) 1.9e-15, CET(5) 2.0e-15, PMF x = 0…6 at n = 6 8.3e-17; CET(5) at γ = 1 exactly (the limit form) against the mean of BTYD's on both sides, γ = 1 ± 1e-6, 6.4e-9 (tolerance 1e-6); the fit against `bgbb.EstimateParameters` 2.7e-10 relative, the log-likelihood at the same parameters equal; and the reference cases A and B: this script's own likelihood at the case's parameters against BTYD's `bgbb.rf.matrix.LL` 1.8e-12 and 2.3e-12 absolute, the case's parameters against BTYD's fit on the same donors 2.3e-4 and 2.1e-3 (tolerance 5e-3). BTYD's `bgbb.PAlive` and the others work on the matrix's rows, so the runner's compressed rows are compared as they are.

**Self-test: 57 checks, all green** (52 before; + 2 of the node in `bg_bb_tests.R`: the route fit → rows declared → checks of a clean fit → `frequency_fit` against `alpha` at both sides of its p → Predict on the training rows bit for bit and NULL on a row that breaks a rule → the `purchases` variant, and an estimate at an edge as a measure with its reading and no verdict; + the 3 reference cases; the checks' test was renamed and extended, and the test of the output's keys got the holdout's classes). 86 s for the run with 55 checks, about 4 minutes for the whole with the survival lane's self-test beside it (the machine was loaded both times).

**Correction 1 (shared), `boundary` is a measure.** The rule «an estimate below 1e-4 or above 1e4 is doubtful» was a fixed threshold. Now `boundary` writes, for each estimate far out on the log scale (the same 1e-4 / 1e4, which decides what is shown, never a verdict), a row `assumption|boundary|<parameter>|value` (the estimate; the spend's as `spend_p`, `spend_q`, `spend_gamma`) and a text `advice_id` with the model's reading (`boundary_no_dropout`: the dropout's first parameter towards 0 or its second towards ∞, so almost nobody leaves; `boundary_homogeneous`: both parameters of a mixing distribution large, so the customers look alike and the spread is not identified; `boundary`: no reading); no verdict, no `h0`, no advice, no count, and **no row at all when no estimate is far out**. The fact stays in `convergence`: with a singular Hessian because of an edge it is doubtful (`convergence_edge`), whose sentence now points to the measure. `MEASURES` has `boundary` (and the layout's `SCIENTIFIC`: its value is written by its exponent, «2E+4», since 5e-5 or 3e5 would be rounded or lost by a fixed number of decimals); `measure_texts.boundary` is a dictionary of the three readings in five languages (`common.measures` says the sentence of the row's `advice_id`, the general one for any other); `checks.boundary` keeps its name only; the words test accepts a check that is a measure with no H0. Tests: the R test of the edges (BG/NBD, Pareto/NBD, BG/BB; the spend; no row when clean; no verdict), the layout test in five languages (the three sentences and the exponent are in the table of measures), and the references: `make_reference.R` expects no verdict for case F (its `boundary_no_dropout` reading stays) and no `boundary|ok` row elsewhere. The PDFs and the fixtures of `clv` were regenerated.

**Correction 2 (report), the holdout chart's last class.** The last class read «10», not «10+», because the holdout's points carried the customers of the class in `x1`. Now they carry the frequency chart's bounds (`x1` = the upper end, none for the open last class), and the customers of each class are `hist|holdout|<class>|count` rows with `level_2` = `customers` (the chart reads the stat `mean` only). A deviation from «The output» (`x1` = customers in the class). Tests: R (the holdout's classes equal the frequency's, the last open, the customers' rows sum to the customers with a holdout) and Python (the same on the four fixtures with a holdout, and the chart's categories: `0 … 9`, `10+`).

**Fixtures and PDFs.** `bg_bb-value.json` and `bg_bb-purchases.json` are the runner's output on `bgbb_donations_like.csv` (value: the gift, the holdout of 5, horizon 5; purchases: neither, so the page shows the sentence that says how to add a holdout), written by `tests.models_fixtures --from-results` (their parameters in `PARAMS`, in `FROM_RUNNER`); `clv-value.json` and `clv-purchases.json` regenerated. The 8 reports × 5 languages were drawn outside the repository (4 pages each). Looked at: `bg_bb-value-en` (4 pages), `bg_bb-purchases-it` (page 2: the sentence in the holdout's place), `clv-value-en` (page 2: «10+»), and a scratch PDF of `clv-value` with three estimates at an edge in Italian and English (the measure's rows with `2E+4`, `3E+5`, `5E-5` and their sentences).

**Deviations from the spec.** (1) The tolerances above, looser than «Reference values» for BG/BB (1e-6 on the parameters). (2) No BG/BB case at γ = 1 exactly in `expected.json`: the estimate never lands there; the limit form is tested by B1's self-test and by the witness above. (3) The reference cases are three, not one (`bgbb_donations_like.csv` is the 1,000 donors of the spec; the cohorts and the no-dropout case are new). (4) `x1` of the holdout's histogram is a bound, not the customers (correction 2). (5) `boundary` has no row when clean (was one «ok» row with a count 0). (6) The self-test count is 57. Not done, by the lot's rules: nothing in the catalogue (P8's), so `bg_bb`'s checks list still names `boundary` and its sections still declare the stat `count` of `hist` (already declared).

## As built, B8 (2026-11-21)

Done together with P10 (they share `docs/capabilities/models.md`, `CHANGELOG.md` and the assistants' text); see «As built, P10» in the models spec.

- **Page**: «Customer lifetime value (R)» (the input table with the form's labels and every refusal in short, the options, the models in plain words, the CLV, outputs, checks as tests, facts and measures, the report, Predict, MLflow, limits), «Building the summary per customer» (six steps with the nodes that exist: Computed column, Filter, Group by, Computed columns with a window `MIN(day) OVER (PARTITION BY customer)`, Join, Fill nulls; the Polars chain was run on synthetic data and equals a hand computation) and «BG/BB (R)» with its month-based summary (Unique rows, Group by) and the differences. An example on the sample data (orders, order amounts, customers acquired in the window, a one-year holdout, the calibration end 2025-07-15), **without numbers**: it was not run.
- **Deviations from the spec**: the summary uses a window function instead of the spec's join of the first day's amount (fewer nodes, same numbers); the holdout length of the example is 52.14 weeks (365 days). The pointer line in the models spec was not added (the owner's rule: no edits beyond the As built blocks).
- **Assistants**: as in P10. `README.md` not touched.


**As built, B9** (2026-11-21; the test on kind, with P11; see «As built, P11» for the images and the revisions; the commits `5c7c4867`, `838d8300`, `94c1f1c0`; not pushed; the test data are small generated tables: 240 customers for `clv` (a BG/NBD simulation with spend and a 26-week holdout), 240 donors for `bg_bb`):

- **Enabling**: *Customer lifetime* enabled from the API as the Administration does; the cluster built it (the BuildKit Job, about 15 minutes), checked it and made it ready: version 1, 55 packages in the recipe, **88 lines in the lock (87 R packages and `libgsl-dev@2.7.1+dfsg-6ubuntu2` at the end), CLVTools 0.12.1**; the self-test: **57 checks ok, none failed**.
- **The seven checks of the correction B7**: (1) the build log has `dpkg-query -W -f '${Package}@${Version}\n' libgsl-dev > /opt/tabularia/system-libraries.txt`; (2) the check Job did not fail on the `cat` (the version is ready); (3) the lock ends with the `libgsl-dev@…` line and its count is the R packages plus one; (4) the card says «System library: GSL 2.7.1+dfsg-6ubuntu2 (Debian)», **after a correction**: the list read no lock, so the card had said «GSL (Debian)» alone (`94c1f1c0`: a column of the lock's system-library lines, read for the list too, which still carries no lock, log or self-test; a database test); (5) regression has no such step, no line in its lock and an empty `system_libraries`; (6) there is no version built before B0 in this cluster, so «the name alone» was seen only as the bug above; (7) the Predict of CLV and BG/BB gives the fit's rows (below). **The wording «(Debian)»**: the image is `rocker/r-ver` (Ubuntu noble) and the version says `ubuntu2`; the packages are Debian's format. To decide: «(Ubuntu)», or no distribution.
- **`clv`**, all `SUCCESS`: `purchase_model = auto` (BG/NBD chosen, log-likelihood −2589.20, the table was generated from it; Pareto/NBD −2613.87, MBG/NBD −2600.65), the value variant with `spend`, the 26-week holdout (relative error −0.171, correlation 0.681, CLV total 11,251, mean 46.88), `compare` with `bg_nbd` (36 `candidate` rows), `pareto_nbd` with a horizon of 52, `mbg_nbd` (variant `purchases`), `output = rows` and `output = results`. **`bg_bb`**: the value and purchases variants, `results` and `rows`, with a holdout of 4 opportunities and a period of one month.
- **Refusals**, with the sentences of the spec: a duplicate customer («the customer c005 appears on 2 rows: one row per customer; group the transactions by customer first»), a recency after the age (named customer, both numbers), 12 customers («these models need at least 20»), a BG/BB customer with more transactions than opportunities, a holdout without its length, `compare` with `auto`, a time unit outside the list, a column used twice. **One was broken**: with no customer who bought again the step said «the runner refused the data and did not say why (its log may)»: the runner wrote its empty `params` as `[]` (R's empty list) and the gateway read `params` as an object. Corrected in the gateway (`5c7c4867`: an empty list, null or anything but an object is no params, the sentence stays; a test with the three forms); on kind it now says «no customer bought again: the models cannot tell the purchase rate from the dropout without repeat purchases». The R side still writes `[]`, which the corrected reader accepts; any other refusal with no params was affected the same way.
- **The Job**: the ConfigMap of the code is 295,972 bytes, the container asks 1 CPU and 1 GiB and is limited to 2 CPU and 2 GiB: **it was enough for 240 customers** (each run about 9.5 seconds, with the pod; no OOM kill); the 100,000 customers of the spec's B9 were **not** tried (small data only), so the 2 GiB of `max_rows = 1,000,000` is still a number of the spec, not a measure.
- **MLflow and Predict** (`b9-clv-value`, `b9-clv-purch`, `b9-bgbb-value`, `b9-bgbb-purch`, all version 1): tags `tabularia.model`, `.group = customer_lifetime`, `.group_version = 1`, `.card_sha256`, `.variant` (`value` or `purchases`), `.purchase_model` (the chosen one, `bg_bb` for BG/BB); parameters with `purchase_model_chosen`, `horizon`, the formula in words; 96 metrics for CLV, 54 for BG/BB; artifacts `model/MLmodel`, `model.json`, `tabularia/{coefficients.csv, report.pdf, results.parquet}`. **Predict from each version equals the fit**: `prediction_p_alive`, `prediction` (the expected transactions), `prediction_spend`, `prediction_clv` against `pred_p_alive`, `pred_transactions`, `pred_spend`, `pred_clv`: **identical, 240 rows each** (the holdout and discounted columns are the fit's alone). `horizon = 10` accepted, 5000 refused («'horizon' must be a whole number from 1 to 1000»). The tag `tabularia.card_sha256` set to zeros on `b9-clv-value` and `b9-bgbb-value`: the Predict refused with the same sentence as P11's; **both tags put back** and read back.
- **The editor, Chromium and Firefox** (screenshots kept outside the repository): the three nodes are in the palette (*Parametric survival*, *Customer lifetime value*, *BG/BB* under *Models*, 17 entries). **With the keyboard alone** (focus the Models header, Enter, Tab to the entry, Enter after selecting the source: the node is added and connected to it; then the form's fields by Tab, Enter and the arrows, Space on a predictor, Ctrl+S): every field of the three nodes was reached and set in both browsers, and the saved flow holds exactly the parameters typed (`survival`: time, event, event value, predictor, strata, start, quantiles, times, `alive_at`; `clv`: the five columns, the unit, spend, horizon, holdout and its length; `bg_bb`: the same for opportunities). The reports of `auto` with strata, CLV value and BG/BB open in the editor's Report tab and download as PDF in both browsers. **Found and corrected**: picking an option of a `Select` with Enter closed the list and the focus fell to the page (Escape and Tab returned it), so the next Tab started from the top of the page; `838d8300` returns it to the field, a test on the three keys; checked in both browsers on the new image. **The hydration warning** of the P6/B6 test page does **not** appear in the real editor: the console of both browsers, for the three nodes added, filled and saved, held no warning and no error but `GET /api/system/memory` 404 (the footer's memory indicator on Kubernetes: not from these nodes). **Firefox's extra Tab stop** is the side panel's scrolling area, before «Delete node»: it comes with every node's panel, not with the list of predictors, whose check boxes are one stop each in both browsers.
- **Open**: the 100,000 customers; the wording «(Debian)»; the `tabularia.variant` of P11 above; Excel exports not opened; the card of the Administration was read in Chromium only.


## As built, B10 (2026-11-21)

The final review's findings, corrected in four commits (`40143b43` runner and references, `e43ff9ff` report, `69dc54eb` the Administration card and the check script, `20e45ed8` sample data, CI and docs); not pushed.

**1. The guard against a likelihood of noise.** `clv_sound(model, comp, lp, value)`, at every end point of the optimizer (one evaluation more): a customer without repeat purchases has a probability, so its log-likelihood is at most 0 (every BG/BB customer's is), and the optimizer's value must equal the customers' sum computed in the stable form, within `CLV_ROUNDING` = √ε relative (a numerical tolerance, not a verdict). The stable form: log Γ(r+x) − log Γ(r) = log Γ(x) − lbeta(r, x) (R's lbeta subtracts Stirling corrections and keeps its digits at r = 1e35), r·log(α/(α+t)) as −r·log1p(t/α); BG/NBD written here in that form (`clv_bgnbd_ll_ind`, the guard's recomputation and the fallback's likelihood; 1e-10 from CLVTools' where CLVTools has its digits), MBG/NBD rewritten in it, MBG/NBD's P(alive) with log1p. A fit that is not sound falls back: L-BFGS-B (stable form, tight) from the best sound row of the first run (optimx's Nelder–Mead row for BG/NBD, from which BFGS had jumped into the noise), else Nelder–Mead from the start; a fit that stays noise is `fault`, not converged (MLflow's `converged` is 0), keeps no Hessian and is never chosen by `auto`. `clv_choose`'s 1e-10…1e10 is gone. Measured: on the sample data (2,059 customers) CLVTools' BG/NBD gave r 1.4e35, α 8.1e67, LL +4.86e24 «converged», with the stable sum −992,183 at that point; now LL −39,144.3, r 0.898, α 7.91, a 0.369, b 6.42. **The same failure was in the 10,000-customer timing case of «As built, B1»** (a BG/NBD simulation): r 2e20, LL +4,602, and `auto` chose MBG/NBD; now BG/NBD at its true parameters (0.495, 5.00, 0.815, 3.17), LL −89,166.6, chosen. Reference case J, `clv_bgnbd_noise.csv`: 300 BG/NBD customers in continuous time (the sample's parameters, ages with a falling density), seed 21 being the first draw that reproduces the noise with CLVTools 0.12.1; its expected values from a BG/NBD written in `make_reference.R` and maximized to the end.

**2. The χ² classes.** Adjacent counts pooled from the top down until EVERY class expects 5 (Cochran's rule, the condition of Pearson's χ², documented as such), a remainder at the bottom joining the class above; classes carry `x0` (lowest count) and `x1` (highest; NULL for the open last); the report labels them «5–7». Sample data, Pareto/NBD: 60 classes (28 below 5), p 0.0055 failed → 39 classes, p 0.115 ok.

**3, 4. The expected transactions.** BG/NBD's CET is the closed form written here with GSL's status (`clv_bgnbd_cet_closed`, 1e-12 from CLVTools' where GSL succeeds), so a GSL failure goes to the quadrature (the review's r 2.57, α 0.96, a 1.07, b 24, x 18: CLVTools 426, the integral 125). The quadrature is cut at the knee p* = (α+T)/((r+x)t): Gauss–Jacobi (32) on [0, c₀] with the weight u^(a−1) exact, Gauss–Legendre (16 per panel, panels ≤ 1 wide) on log p up to ½, Gauss–Jacobi (32) on [½, 1] with (1−v)^(B−1) exact (left out when B ≥ 60); against integrate() at rel.tol 1e-13 on x = 0…20,000, T = 2…100, t = 1…520 within 2e-14 (64 nodes on p were 0.5–2 % off, up to 8 %). About 0.3 ms per customer and step: used only where GSL fails.

**5. The edges, a fact.** `CLV_CHECK_EDGE` is gone. `clv_edges` probes, at the chosen model's estimate and the spend's, one step of a factor e towards 0 and towards ∞ for each parameter, and both parameters of each mixing distribution up together (`CLV_PAIRS`); an edge is a direction where the log-likelihood is not lower than at the estimate beyond rounding (2k + pairs evaluations, once). Choice: the edge as a fact of the likelihood, not «a non-positive-definite Hessian is always failed», because B2's reason stands (customers who never drop out push the dropout's parameter to a legitimate limit). Convergence: not converged or noise → failed; an edge → **doubtful** even with a positive definite Hessian (measured on `bgbb_no_dropout.csv`: with the tight optimizer γ goes so far that the Hessian is positive definite to its last digits, while the likelihood still grows); a flat Hessian without an edge → failed. The boundary measure shows the parameters at an edge with the same readings. `convergence_edge`'s sentence rewritten in five languages.

**6.** BG/BB's frequency check takes the PMF at each distinct number of opportunities (a cumulative sum up to the largest n), never at a mean.

**7.** CI: the interface job builds the sampledb image and runs `test_generate_lifecycle` in it with `SAMPLEDB_REQUIRE_LIBS=1`; run locally with the same two commands (10 tests, ok).

**8.** The generator is unchanged (its realism is meant). A test on the orders as the databases get them: inside each channel |r| of x and m̄ below 0.12 (measured −0.098…+0.087, n 174–478), and the channels differ in both order size and frequency; the docs and the sampledb README say that the spend check fails on the whole base (r 0.058, p 0.0115) and holds inside a channel, and how to fit per channel (a Filter on `canale`).

**9, 10.** The card says «GSL 2.7.1» (`UpstreamVersion`: no epoch, revision or repack suffix) and the package's whole version as secondary text, «(package libgsl-dev 2.7.1+dfsg-6ubuntu2)»; «(Debian)» removed from five languages. The check script reads the record with `[ -f … ] && cat … || true`, tested under sh with and without the file.

**11.** `spend_independence`'s not_computed sentence in five languages, with a test per language. **12.** Predict: an infinite spend is a NULL row; BG/BB rounds t_x and n as the fit. **13.** `clv_card_params`: a card's parameters by name; a model outside the list, a node that does not fit it, a purchase model that differs from its parameters' model, a parameter that is not a positive finite number: refused (`bad_card`, «the saved model cannot predict: …; fit it again»). **14, 16.** sampledb README: the recipe keeps `age > 0`; the CRM activity around the churn is a signal left on purpose.

**15. Tolerances.** MBG/NBD and BG/BB run L-BFGS-B with `CLV_TIGHT` (factr 10, pgtol 1e-10; CLVTools' models keep CLVTools' settings). Measured against the maxima found by the reference scripts: MBG/NBD parameters 1.8e-6 (5.3e-4 before), tolerance 1e-3 → **1e-5**; BG/BB parameters 7.1e-6 (2.1e-3), 5e-3 → **5e-5**; BG/BB log-likelihood 1e-10, 5e-5 → **1e-8**; BG/BB sums 1.3e-6, 1e-3 → **1e-5** (the holdout's relative error 1.6e-4, 1e-2 → 1e-3); BG/BB's Gamma–Gamma (CLVTools' settings) 2.4e-5, 5e-3 → 2e-4; case J 3.8e-6, tolerance 1e-5. New independent `clv_total` references: case A from CLVTools' predict() week by week (4.3e-12, tolerance 1e-7), case C from MBG/NBD's CET by integrate() at the maximum found here (5.6e-8, tolerance 1e-6).

**Self-test: 63 checks, all green** (57 before; + the edges' test, the guard and stable forms, GSL's failures and the quadrature against integrate(), Predict's rows and cards, BG/BB's PMF at distinct n, case J), 64 s on the idle machine. **Times**, the 10,000-customer case of B1 on this machine, `auto` with a spend, 52 weeks: 69.5 s before (with BG/NBD's noise, which was cheap and wrong), 82.9 s now (BG/NBD's fallback 2.4 s, its true fit then evaluated and chosen); the sample data's `auto` 37 s → 26–37 s.

**The sample data, before → after** (`auto`, `bg_nbd`, `pareto_nbd`, `mbg_nbd`, all with the spend): BG/NBD LL +4.86e24, CLV 0, convergence doubtful, frequency_fit not computed (1 class) → LL −39,144.3, convergence ok, CLV 5,776,905, frequency_fit failed (p 0.0017, 39 classes); Pareto/NBD frequency_fit failed (p 0.0055, 60 classes) → ok (p 0.115, 39 classes); MBG/NBD frequency_fit failed (p 0.0011) → failed (p 0.049); `auto` chooses Pareto/NBD before and after (frequency_fit now ok); spend_independence failed everywhere (r 0.058, p 0.0115), as meant; spend_tail ok (q 3.31).

**Fixtures and PDFs.** The four fixtures regenerated from the runner's output (`--from-results`), the 20 PDFs drawn outside the repository; looked at: `clv-value-en` pages 1–2 (the pooled classes «5–6», «7–9», «10+» in both charts), `bg_bb-value-it` page 1.

**Left open.** The note `@optimizer_fallback` is written when any candidate used the fallback, also when the chosen model did not (B1's rule). The edges' probe uses a factor e: a step, not a threshold, but a likelihood flat within √ε over a factor e on an interior estimate would read as an edge.
