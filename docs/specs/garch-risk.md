# Spec: volatility models (GARCH) and risk simulation in the `timeseries` group

Status: **approved by the owner on 2026-11-22** (every question of «Open questions for the owner»
answered: two nodes, Monte Carlo of paths, `rugarch`, the other recommendations).

The owner asked for the piece on 2026-11-22 («simulazioni MCMC solamente in ambito
timeseries, ad esempio per calcolare l'expected shortfall o altre misure di rischio»; then «ARCH/GARCH
nella stessa specifica») and accepted the shape of part 1 before the draft: a node «Volatility (GARCH)»
with ARCH, GARCH, GJR-GARCH and EGARCH, an optional ARMA mean, normal, Student t or skewed t errors, the
R package `rugarch`, checks that speak only through tests with a p-value (Ljung–Box on the standardized
residuals and on their squares, a residual ARCH-LM, Nyblom's stability test), persistence and half-life
as measures. Everything else was the agent's proposal, approved by the owner on 2026-11-22 («Open questions for
the owner», each marked «Decided 2026-11-22»).

It adds two nodes to the group `timeseries` of `docs/specs/statistical-models.md` (**the models spec**),
which it reuses and does not repeat: the recipe, the image and its self-test (exit 3 a refusal, 4 a
failed self-test), `RunnerHash` and the recheck of a changed runner, the Job with its parquet input, the
ten columns, layouts and `report_texts`, Run in the editor and «not up to date», MLflow logged by the Go
gateway, Predict. It follows `docs/specs/ts-decomposition.md` (**the decomposition spec**: the group's
newest node, `ts_series.R`, `ts_checks.R`, the input's types kept, a recipe with system libraries, the
rebuild on kind) and `docs/specs/ab-testing.md` (**the A/B spec**: decisions and lots, `card: false`,
no fixed thresholds), and the explanation of the method of `docs/specs/report-explanations.md` (**the
explanations spec**, lots B0–B4). The lots that build it are «The lots G0–G9».

Words: **verified** = read in this repository's code on 2026-11-22; **checked online** = read on CRAN,
PyPI or in the cited paper on 2026-11-22; **assumed** = from the literature or a package's documentation,
not run. **Nothing was measured for this draft**: other lanes were working on this machine (one heavy job
at a time), so every time and size below is an estimate that lot G0 (and G2, G3 for the simulation)
replaces with a measurement.

## Objective

A series of returns — a commodity's price, a currency, a portfolio — is calm for months and then
turbulent for weeks. Its **volatility clusters**: large moves follow large moves. ARIMA models the mean
and assumes one variance (its report has an ARCH-LM test that says when that is wrong, and nothing
else). A person wants:

- **the volatility of each day**, estimated, and its forecast for the next days: how turbulent is the
  market now, and how fast does it calm down (persistence, half-life)?
- **whether falls move the volatility more than rises** (GJR, EGARCH), with a test;
- **whether the model is adequate**: what is left in the standardized residuals, whether the parameters
  are stable over the sample — each answered by a test with a p-value against the node's alpha;
- **the risk ahead**: how much can a position lose over the next ten days with probability 1 % (value
  at risk), how much on average when it does (expected shortfall), how likely is the price to cross a
  level the business cares about, how deep can a fall from a peak be — computed from thousands of
  simulated futures of the fitted model, with their Monte Carlo error said;
- **whether yesterday's risk numbers held**: on the last months kept out of the fit, how many days broke
  the VaR, against the expected number, and whether the breaks come in clusters (Kupiec, Christoffersen).

The person drops **Volatility (GARCH)** after a step with one row per period (a date and a price or a
return), presses **Run**, and gets the conditional volatility as the next step's input and a report that
says, in words with the run's numbers, how persistent the volatility is and which checks the model
passes. With **Risk simulation** they get, from the same series, a table of risk measures per step (or
every simulated path) and a report with the fan of the paths, the distribution of the outcome with its
VaR and ES, and the backtest.

## Decisions

| Question | Decision | By |
|---|---|---|
| Volatility models | **One node `garch` — «Volatility (GARCH)»**: ARCH(q), GARCH(p,q), GJR-GARCH(p,q), EGARCH(p,q); optional ARMA(m,n) mean with or without a constant; errors normal, Student t, skewed Student t | Owner (2026-11-22) |
| Package | **`rugarch` 1.5-6** (GPL-3) for the fit, the filter, the forecasts and the tests it has; its 23 new dependencies and their licences below («The package and the image») | Owner (2026-11-22), the list: spec |
| GARCH checks | Weighted Ljung–Box on z and on z² (Fisher & Gallagher 2012), residual ARCH-LM, Nyblom's joint stability test (individual tests Holm-adjusted), Engle–Ng's sign bias test — **each a verdict from a p-value against `alpha`**; persistence, half-life, unconditional volatility, the news impact as **measures** | Owner (the four, 2026-11-22); sign bias: owner (2026-11-22, Open question 9) |
| No simulation in the GARCH node | The GARCH node draws nothing: forecasts, 1-step VaR, tests are analytic. **No bootstrap** | Owner (rule of 2026-11-20, kept for this node) |
| Risk simulation | **A second node `risk_simulation` — «Risk simulation»** that fits its own model of the series (ARIMA or GARCH, with the same options and the same runner code as the two nodes) and simulates N paths of h steps; here the simulation **is** the product | Owner (2026-11-22, Open question 1) |
| «MCMC» | Read as **Monte Carlo simulation of future paths** with the parameters at their estimates; Bayesian MCMC belongs to the visual Bayesian editor planned after this piece | Owner (2026-11-22, Open question 2) |
| Memory | **Never everything in memory**: paths simulated in batches, each batch one row group appended to one Parquet file by one open Arrow writer, then freed; every measure from accumulators or from a second pass that reads the file a row group at a time; **the batch size derives from the group's memory limit**, not from a number of ours | Owner (rule of 2026-11-22) |
| Exactness | VaR, ES, threshold probabilities and drawdown quantiles **exact** (two passes over the file); the fan chart's band edges within a **declared** error, written per step | Owner (2026-11-22, Open question 5) |
| Reproducibility | A seed is always there (default 1); paths are generated in **seed blocks** of 1,000 paths, each block from its own L'Ecuyer-CMRG stream: **the same seed gives the same numbers, bit for bit, at every batch size and every memory limit** | Owner (rule), the mechanism: spec |
| Time and memory ceilings | Time from the node's timeout (`time_budget_seconds`, already in the task), memory from the group's limit (`memory_bytes`, new in the task), disk from the Job's `/tmp` (`tmp_bytes`, new): the runner projects from its first batch and **refuses with numbers** before it would exceed one | Owner (rule; the mechanism 2026-11-22, Open question 6) |
| Verdicts | Only from tests with a p-value against the node's `alpha` (default 0.05). VaR, ES, persistence, half-life, drawdown are **measures**, said without a word of judgement | Owner (rule of 2026-11-20) |
| Levels | `var_level` **0.99** and `es_level` **0.975**, options with visible defaults (the 99 % VaR of the Basel market-risk rules and the 97.5 % ES of their revision) | Owner (2026-11-22, Open question 15) |
| Backtest | 1-step-ahead VaR on the holdout, Kupiec's unconditional coverage, Christoffersen's independence and conditional coverage (likelihood ratios, χ²) — in both nodes, one shared function | Owner (the tests), spec (where) |
| Explanation | Readings with the run's numbers under each block, «How this report was computed» with the formulas, `@id name=value` rows said by `structured_note` in five languages, chosen by the layout (`common.READINGS`, lot B0), no AI | Owner (rule) |
| Columns before the Run | Every output column known from the catalogue and the options; `time` keeps the input's exact type (`=time`, the local Int64 option of `ts_series.R`) | Owner (rule) |
| Model card, Predict | GARCH: **a card and Predict** (filter new returns with the registered parameters: σ and VaR per row, the volatility forecast). Risk simulation: **none** (`card: false`), MLflow logs the run | Owner (2026-11-22, Open question 11) |

## What exists and is reused (verified)

- **The group `timeseries`** (`gateway-go/internal/modelgroups/groups/timeseries/`): `recipe.json` (R
  4.6 on `rocker/r-ver:4.6.0`, 43 packages pinned on 2026-11-21 among them `forecast@9.0.2`,
  `tseries@0.10-63`, `urca@1.3-4`, `FinTS@0.4-9`, `xts@0.14.3`, `zoo@1.9-1`, `fracdiff@1.5-4`,
  `Rcpp@1.1.2`, `RcppArmadillo@15.6.2-1`, `Rlibeemd@1.4.4`; `system_libraries: ["libgsl-dev"]`;
  1 CPU, 2 GiB, `max_rows` 200,000, `max_seconds` 1,800); `runner/run.R` (routes `arima` fit and
  predict, `decomposition` fit); `runner/R/arima.R`, `arima_auto.R`, `ts_series.R`, `ts_checks.R`,
  `decomposition*.R` and their `*_tests.R`; `reference/` (CSV cases, `make_reference*.R`,
  `make_witness*.py`, `witness.json`, `expected.json`).
- **`ts_series.R`** (decomposition lot D1): the grid of a frequency, `duplicate_time`, `irregular_time`,
  `ts_time_kind`, the local `arrow.int64_downcast = FALSE` read, `ts_time_array(times, type)` (the
  output's time of the input field's exact type), `ts_time_after`, `ts_envelope` (min/max thinning of a
  long series for its chart). `frequency = index` numbers the rows 1…n by rank of their time: **trading
  days** (dates with weekends and holidays missing) are an `index` series whose dates are labels.
- **`ts_checks.R`**: `ts_acf`, `ts_ljung_box`, `ts_lb_lag`, `ts_largest_acf`, `ts_check_rows` (a check's
  rows as the fixtures have them), `ts_measure`.
- **ARIMA** (`arima.R`): the fit (`forecast::Arima`, `auto.arima` wrapped), the holdout, the residual
  ARCH-LM (`FinTS::ArchTest`), the card with the last 5,000 observations, Predict with `horizon`.
- **The common R library** (`gateway-go/internal/modelgroups/common/R/`): `tab_main` (routes, phases,
  `run.json`), `tab_refuse`, `tab_seed` (Mersenne-Twister, Inversion, Rejection), `tab_limit`,
  `tab_write_output` (`arrow::write_parquet` of a whole table: **not** usable for the paths),
  `tab_copy_file` (reads a whole file into memory before writing it to the bucket: **not** usable for
  the paths either), `tab_hist`, `tab_qq`, `tab_cap`.
- **The task** (`jobs_models.go`): `task.limits.time_budget_seconds` = the step's timeout (the boosting
  group stops its trees by it). The Job's `/tmp` is an `emptyDir` on the node's disk with
  `sizeLimit` = 2 × `max_model_file_bytes` + the group's memory limit + 256 MiB (`modelTmpLimit`): about
  2.4 GiB for this group; the kubelet evicts the pod beyond it.
- **The import** (`backend/app/api/routes/scripts.py`, `/scripts/output/import`): downloads the output
  Parquet to a temporary file and uploads it to Tabularia's storage — streamed, its memory does not grow
  with the file; no row limit.
- **The catalogue** (`backend/app/engine/models/catalog.py`): `KINDS` (`alpha`, `seed`, `horizon`,
  `holdout`, `quantiles` (numbers in (0, 1), at most `max_quantiles` 9), `lb_lag`, `transform`,
  `box_cox_lambda`, the ARIMA kinds), `LIMITS` (`max_horizon` 1,000, `max_result_rows` 100,000,
  `max_chart_points` 2,000, `max_lb_lags` 1,000), `OUTPUT_TYPES` with `=<key>`, `_ROWS_WILDCARDS`
  (prefix → a rule of the parameters), a model's own `max_rows` (ARIMA's 20,000, checked by the gateway
  with the group's), `card: false`, `PREDICT` (`applies_to`, `columns`). Mirrored in
  `frontend/composables/modelCatalog.ts` and `gateway/app/services/ai_flow_agent.py`.
- **The report machinery** (`backend/app/engine/models/layouts/common.py`, lot B0): `READINGS` (the
  layout chooses a model's reading ids from its rows), `reading_rows`, `structured_note`, `reading_said`,
  `NUMBER_FORMS` (`pct`, `spct`, `int`, `p`, `num`, `date`), `NOTE_NAMES`, `reading_under`,
  `reading_table`, `how_computed`, `page(…, explained=…)`, `verdict_words`; charts `xy` with marks
  `point`, `line`, `step`, `stem`, `band_low`/`band_high`, `split_by`, `target_x` (at most 4 vertical
  lines), `x_time`, `mirror` (`backend/app/reports/definition.py`).
- **The orchestrator during upgrades** (commits `765fe10f`, `c935a747`; `docs/deploy/upgrading.md`): a
  group's version is built and self-tested only by a gateway of the release that queued it; the
  rollback's wait is `MODELS__OTHER_RELEASE_WAIT_SECONDS`. The decomposition's D8 found the failure this
  fixes (an old pod built the new version with its old recipe); **lot G8 is the first rebuild of a
  changed recipe after the fix**, and checks it on kind.
- **Recipes already holding packages this piece adds** (verified in the recipes): `numDeriv@2016.8-1.1`
  (regression, survival), `mvtnorm@1.4-2` (survival), `nloptr@2.2.1` and `pracma@2.4.6` (customer
  lifetime): the owner's licence check of 2026-10-30 passed them.

## Part 1 — the node `garch`: Volatility (GARCH)

### The form

| Key | Kind | Default | Applies | Meaning |
|---|---|---|---|---|
| `time` | `time_column` | — (needed) | always | One row per period |
| `value` | `number_column` | — (needed) | always | A price or a return |
| `frequency` | `frequency` | — (needed) | always | `index` for trading days (each row a step, the dates are labels) |
| `input_kind` | choice `returns` \| `prices` | `returns` | always | `prices`: the node computes the returns |
| `returns_type` | choice `log` \| `simple` | `log` | `input_kind = prices` | r_t = ln(P_t / P_{t−1}), or P_t / P_{t−1} − 1 |
| `variance_model` | choice `arch` \| `garch` \| `gjr` \| `egarch` | `garch` | always | The variance equation (below) |
| `arch_order` | integer 1–3 | 1 | always | Lags of the shocks ε² (or z for EGARCH): q |
| `garch_order` | integer 0–3 | 1 | `variance_model ≠ arch` | Lags of the variance σ²: p (`arch` is p = 0) |
| `ar_order` | integer 0–5 | 0 | always | AR lags of the mean |
| `ma_order` | integer 0–5 | 0 | always | MA lags of the mean |
| `mean` | boolean | true | always | A constant μ in the mean |
| `distribution` | choice `norm` \| `std` \| `sstd` | **`std`** | always | Normal, Student t, skewed Student t (standardized: mean 0, variance 1) |
| `se_type` | choice `robust` \| `classical` | `robust` | always | Standard errors: the QML sandwich (Bollerslev & Wooldridge 1992), or the inverse Hessian |
| `horizon` | `horizon` (1–1,000) | **10** | always | Steps of the volatility forecast |
| `holdout` | `holdout` (0–1,000) | 0 | always | The last k returns kept out of the fit: the backtest of the 1-step VaR |
| `var_level` | `risk_level` (number in (0.5, 1)) | **0.99** | always | The VaR of the output's `var` column and of the backtest |
| `lb_lag` | `lb_lag` (`auto` or 1–1,000) | `auto` | always | The portmanteau tests' lag m; `auto` = min(10, ⌊n/5⌋) + K, K the larger of the two tests' fitted parameters (FPP3 §5.4's 10 for a series without season, counted beyond what the test subtracts) |
| `arch_lags` | `lb_lag` | `auto` | always | The ARCH-LM's lags; `auto` = ARIMA's rule (the frequency's season when it has one, else 12) |
| `alpha` | `alpha` | 0.05 | always | Every test, every interval |
| `seed` | `seed` | 1 | always | Only for `rugarch`'s solver: its fallback `gosolnp` restarts from random points; the fit is the same for the same seed |
| `output` | choice `volatility` \| `results` | **`volatility`** | always | What the next step receives |

**Why these defaults.** `garch` (1,1): the benchmark of the literature (Hansen & Lunde 2005: hard to
beat for daily returns). `std`: returns have fat tails; the normal is the t's limit (ν → ∞), and the
report shows the estimated ν with its interval, so a reader sees when the data are close to normal (Open
question 4). `robust` standard errors: valid when the distribution is wrong, the usual case. `horizon`
10: the ten trading days of the market-risk rules. `var_level` 0.99: the same rules' VaR.

The node's title says the model as `catalog.garch_name(params)` does: «GARCH(1,1), t», «GJR-GARCH(1,1),
skewed t», «AR(1)-EGARCH(1,1), normal», «ARCH(2), t».

### Combinations

| Rule | Kind | Reason id: sentence |
|---|---|---|
| `returns_type` | `applies` | `input_kind = prices` |
| `garch_order` | `applies` | `variance_model ≠ arch` |
| `disjoint` | | `time`, `value` |

Refused by the runner (exit 3, a sentence each): `too_short` (fewer returns than `rugarch` fits — it
refuses below 100, assumed, G1 reads the bound in 1.5-6 — or than 2·`holdout`: «the series has 180
returns after the holdout of 250: shorten the holdout»), `not_positive` (a price ≤ 0 with `prices`),
`gaps` (an empty value inside the span: «filter out the rows without a price, or fill them in the
flow»: a missing return is not interpolated), `constant`, `scale` (values a double cannot square),
`no_convergence` (every solver of `rugarch`'s `hybrid` failed: «try fewer lags, or the normal
distribution»), the shared `duplicate_time`, `irregular_time`, `time_type`.

### The series

ARIMA's rules through `ts_series.R` (`missing = refuse`, no transform): one row per period, the time's
kind, the grid; trading days need `frequency = index`. With `prices`, r_t from consecutive prices (the
first row has no return: `n` counts returns). **Scale**: the fit runs on r_t / s, s = the sample
standard deviation of the training returns, because the solver's tolerances are absolute and daily
returns in fractions put ω near 1e-6 (the same reason the Python `arch` package asks for rescaled
data). The parameters are brought back **exactly** to the series' units: μ·s, ω·s² (ARCH, GARCH, GJR),
ω + (1 − Σβ_j)·ln s² (EGARCH), the AR, MA, α, β, γ, shape and skew unchanged; their covariance by the
same linear map (EGARCH's ω by the delta method, which involves β). The report says it in «How this
report was computed»; the witness compares on the original scale.

### The models (Engle 1982; Bollerslev 1986; Glosten, Jagannathan & Runkle 1993; Nelson 1991)

Mean (ARMA in deviations, `rugarch`'s `armaOrder`, `include.mean`; assumed form, G1 checks it in
1.5-6): r_t = μ + Σ_{i=1}^{m} φ_i (r_{t−i} − μ) + Σ_{j=1}^{n} θ_j ε_{t−j} + ε_t, ε_t = σ_t z_t,
z_t i.i.d. with mean 0 and variance 1.

| `variance_model` | Equation | `rugarch` |
|---|---|---|
| `arch` | σ²_t = ω + Σ_{i=1}^{q} α_i ε²_{t−i} | `sGARCH`, `garchOrder = c(q, 0)` |
| `garch` | σ²_t = ω + Σ_{i=1}^{q} α_i ε²_{t−i} + Σ_{j=1}^{p} β_j σ²_{t−j} | `sGARCH`, `c(q, p)` |
| `gjr` | σ²_t = ω + Σ_{i=1}^{q} (α_i + γ_i I_{t−i}) ε²_{t−i} + Σ_{j=1}^{p} β_j σ²_{t−j}, I_{t−i} = 1 when ε_{t−i} < 0 | `gjrGARCH` |
| `egarch` | ln σ²_t = ω + Σ_{i=1}^{q} (α_i z_{t−i} + γ_i (\|z_{t−i}\| − E\|z\|)) + Σ_{j=1}^{p} β_j ln σ²_{t−j} | `eGARCH` (α the sign effect, γ the size effect) |

`garchOrder = c(q, p)` is `rugarch`'s order (ARCH first, GARCH second); the form's names `arch_order`
and `garch_order` avoid the two conventions in use (Bollerslev's GARCH(p,q) puts p on σ²; the Python
`arch` package's `p` is on ε²).

Distributions (`rugarch`'s `distribution.model`): `norm`; `std`, the Student t scaled to variance 1,
z = t·√((ν − 2)/ν), ν > 2 (`shape`); `sstd`, Fernández & Steel's (1998) skewing of the standardized t,
f(z) = 2/(ξ + 1/ξ) [f*(z/ξ) for z ≥ 0, f*(zξ) for z < 0] moved and scaled to mean 0 and variance 1
(`skew` ξ > 0, ξ = 1 symmetric; assumed parametrization, G1 transcribes `rugarch`'s `dsstd` for the
witness). The Python `arch` package's skewed t is Hansen's (1994): a different family, so the witness
for `sstd` is a transcription, not `arch`.

**Estimation**: Gaussian or Student quasi-maximum likelihood, `ugarchfit(solver = "hybrid")` (`solnp`,
then `nlminb`, `gosolnp`, `nloptr`), the variance recursion started at the sample mean of ε² (`rugarch`'s
default `rec.init = "all"`, written in the report), stationarity imposed by the solver's constraints.
`out.sample = holdout`: the last k returns are not seen by the likelihood. Standard errors from
`fit@fit$robust.matcoef` or `matcoef`, z = estimate / se, two-sided p, intervals at 1 − alpha. A
parameter at a bound of the solver (α + β at 1, ν at its lower bound): its standard error is not
reliable — a **fact** row `at_bound` names it (no threshold: the bound is the solver's).

### What the node reports (measures)

| Measure | Formula | `rugarch` |
|---|---|---|
| `persistence` P | GARCH: Σα + Σβ; GJR: Σα + Σβ + κΣγ with κ = E[z² I(z < 0)] under the fitted distribution (½ for a symmetric one); EGARCH: Σβ | `persistence()` |
| `half_life` | ln ½ / ln P, in steps and in the frequency's unit («about 23 trading days»); **not computed** when P ≥ 1 | `halflife()` |
| `uncond_sigma` | √(ω / (1 − P)) (GARCH, GJR); EGARCH: the square root of `uncvariance()`'s value (the long-run level of its log-variance recursion; G1 writes the formula `rugarch` 1.5-6 uses); not computed when P ≥ 1 | `uncvariance()` |
| `sigma_last`, `sigma_next` | σ_T and the 1-step forecast σ_{T+1} | the filter, `ugarchforecast` |
| `log_lik`, `aic`, `bic` | the totals −2ℓ + 2k and −2ℓ + k ln n (`rugarch`'s `infocriteria` are per observation: the runner multiplies by n, and says so) | |
| `variance_stationary` (**fact**) | P < 1: the variance returns to a long-run level; P ≥ 1 (an integrated variance) is said, and half-life and the unconditional σ are «not computed». 1 is the mathematical boundary, not a cut of ours | |
| the news impact curve | σ²_t as a function of ε_{t−1} at the unconditional variance, 101 points on ±4 unconditional σ | `newsimpact()` |
| `shape`, `skew` | ν and ξ with their intervals | the fit |

### The checks (verdicts from p-values)

z_t = ε_t / σ_t the standardized residuals of the fit (training part).

| Check | H0 | Statistic and law | Reference, function |
|---|---|---|---|
| `ljung_box_z` | z is not autocorrelated up to lag m | Fisher & Gallagher's weighted portmanteau Q_W = n(n + 2) Σ_{k=1}^{m} ((m − k + 1)/m) ρ̂_k² / (n − k), K = `ar_order` + `ma_order`, its p-value from their gamma approximation (eq. of the paper, transcribed by G1) | Fisher & Gallagher (2012); `rugarch`'s internal weighted Box test (G1 reads it) |
| `ljung_box_z2` | z² is not autocorrelated up to lag m: no ARCH effect left | the same on z², K = `arch_order` + `garch_order`. The weighted form is chosen because the plain Ljung–Box on squared standardized residuals does not have its nominal χ² law (Li & Mak 1994) | idem |
| `arch_lm` | no ARCH effect left in z | Engle's LM: n·R² of z²_t on 1, z²_{t−1}…z²_{t−L}, χ²_L, L = `arch_lags` (ARIMA's test, applied to z) | Engle (1982); `FinTS::ArchTest` |
| `nyblom` | every parameter is constant over the sample | joint L_c = (1/n) Σ_t S_t' V⁻¹ S_t, S_t the cumulated scores, V their variance; asymptotic law Σ_{j≥1} χ²_k,j / (j²π²) (k Brownian bridges' ∫B²); individual L_i, k tests, Holm-adjusted | Nyblom (1989), Hansen (1992); `rugarch::nyblom()` gives the statistics, **the p-values are the runner's** (below) |
| `sign_bias` | the volatility responds alike to falls and rises | z²_t on 1, S⁻_{t−1}, S⁻_{t−1} z_{t−1}, S⁺_{t−1} z_{t−1} (S⁻ = 1 when z < 0, S⁺ = 1 − S⁻): the three t-tests and the joint Wald, χ²₃ (verdict: the joint) | Engle & Ng (1993); `rugarch::signbias()` |
| `at_bound`, `variance_stationary` | **facts** | | |

**Nyblom's p-value.** `rugarch::nyblom()` returns the statistics and three critical values (10 %, 5 %,
1 %, from Hansen's table): a verdict from them would answer only three alphas, which the owner's rule
forbids. The runner computes P(W_k > ℓ) for W_k = Σ_{j=1}^{∞} χ²_{k,j} / (j²π²) by Imhof's (1961)
inversion of its characteristic function φ_k(t) = (√(2it) / sin √(2it))^{k/2}, truncated at J
eigenvalues with the tail's mean added; J is chosen so that the truncation error is below 1e-7 (written in
`How this report was computed`). Witness: the exact asymptotic quantiles of ∫B² for k = 1 (0.34730,
0.46136, 0.74346 at 10 %, 5 %, 1 %, Anderson & Darling 1952) to 1e-5, and Hansen's (1992) simulated
critical values for k = 2…10 within the simulation error his table states. (The same function would
give KPSS a continuous p-value — the decomposition spec's Open question 11, «possible later»; this piece
does not change KPSS.)

Advice when a check fails (five languages, by id): `ljung_box_z_failed` «structure left in the mean:
raise the AR or MA order»; `ljung_box_z2_failed`, `arch_lm_failed` «volatility the model misses: raise an
order, or try GJR or EGARCH»; `nyblom_failed` «the parameters move over the sample: fit a shorter, more
recent period, or read the parameters named below»; `sign_bias_failed` «falls and rises move the
volatility differently: choose GJR or EGARCH» (said only when the model is `arch` or `garch`; with GJR or
EGARCH the advice is «the asymmetry left is not captured by the model's terms»).

**What these tests are, said honestly** («How this report was computed»): they read residuals of a
fitted model, so their nominal laws are approximations (the weighted forms are better ones); with many
observations they reject tiny effects, and the reading then names the size (the largest
autocorrelation of z², `ts_largest_acf`).

### The holdout and the backtest (shared with the risk node)

With `holdout = k > 0`: the fit sees the first n − k returns; the filter runs the fitted parameters,
fixed, through the k last returns (`ugarchforecast(fit, n.ahead = 1, n.roll = k − 1)`, assumed:
`rugarch`'s rolling forecast with the parameters fixed; G1 checks it against `ugarchfilter` with
`fixed.pars`), giving μ_t and σ_t for each held-out return from the returns before it. **1-step VaR**
(lower tail, a return): VaR_t = μ_t + σ_t q_{1−c}, c = `var_level`, q the quantile of the fitted
standardized distribution (`rugarch::qdist`). A **violation** I_t = 1 when r_t < VaR_t.

`ts_risk.R` (new, shared): x = Σ I_t violations in T = k days, p = 1 − c.

| Test | H0 | Statistic | Law |
|---|---|---|---|
| `kupiec` (unconditional coverage) | the violation rate is p | LR_uc = −2 ln[(1 − p)^{T−x} p^x] + 2 ln[(1 − x/T)^{T−x} (x/T)^x] (0·ln 0 = 0) | χ²₁ |
| `christoffersen_ind` (independence) | a violation today does not change tomorrow's chance | n_ij transitions i → j over T − 1 days, π₀₁ = n₀₁/(n₀₀ + n₀₁), π₁₁ = n₁₁/(n₁₀ + n₁₁), π = (n₀₁ + n₁₁)/(T − 1); LR_ind = −2 ln[(1 − π)^{n₀₀+n₁₀} π^{n₀₁+n₁₁}] + 2 ln[(1 − π₀₁)^{n₀₀} π₀₁^{n₀₁} (1 − π₁₁)^{n₁₀} π₁₁^{n₁₁}] | χ²₁ |
| `christoffersen_cc` (conditional coverage) | both | LR_cc = −2 ln[(1 − p)^{n₀₀+n₁₀} p^{n₀₁+n₁₁}] + 2 ln[the Markov likelihood above] on the T − 1 transitions (= LR_uc on T − 1 days + LR_ind) | χ²₂ |

References: Kupiec (1995), Christoffersen (1998). Witness: `rugarch::VaRTest` (inside the image) and a
hand computation (outside); where `VaRTest` counts the first day differently, G1 writes the gap and keeps
the definition above. «Not computed»: the independence and conditional tests with no violation (no
transition to test: «no day broke the VaR, so there is nothing to cluster»), all three with T < 2.
**No minimum size**: with few expected violations (T·p; 250 days at 99 % expect 2.5) the χ² law is
rough and the test can see only large errors — the reading says the expected count with every verdict
(«with 2.5 violations expected, the test can detect only a large error in the VaR level»). Measures
beside them: `violations`, `expected_violations`, `violation_rate`, the longest run of consecutive
violations. Open question 8 weighs an exact binomial p-value for coverage.

### The output

`volatility` (default) — one row per instant of the span, then `horizon` forecast rows:

| Column | Type | When |
|---|---|---|
| `time` | **`=time`** | always; forecast rows by `ts_time_after` (with `index` on dates: calendar days, as ARIMA's forecast does) |
| `observed` | Float64 | the input's value (empty on forecast rows) |
| `return` | Float64 | `input_kind = prices` (`rows_when`) |
| `mean` | Float64 | the conditional mean μ_t (forecast μ on forecast rows) |
| `sigma` | Float64 | σ_t (the forecast on forecast rows) |
| `std_residual` | Float64 | z_t: from the fit on the training rows, from the filter on the holdout rows, empty on forecast rows |
| `var` | Float64 | the 1-step VaR at `var_level` for that instant (in-sample, holdout and the first forecast step) |
| `violation` | Boolean | r_t < VaR_t; empty on forecast rows |
| `is_holdout`, `is_forecast` | Boolean | |

The first row of a `prices` series (no return) is kept with `observed` and empty model columns, so that
the output has a row per input row.

`OUTPUT_TYPES["volatility"]`: `time` `=time`; `observed`, `return`, `mean`, `sigma`, `std_residual`,
`var` Float64; `violation`, `is_holdout`, `is_forecast` Boolean. No wildcard.

### The results table

The ten columns. Sections:

| Section | `level` | `level_2` | Holds |
|---|---|---|---|
| `model` | null | null | texts `model`, `variant`, `formula` («r = μ + ε, σ² = ω + α₁ε²₋₁ + β₁σ²₋₁, z ~ t»), `software`, `group_version`, `rows_from`, `note_*`, `model_name`, `variance_model`, `distribution`, `input_kind`, `returns_type`, `se_type`, `frequency`, `time_kind`, `solver`; numbers `n`, `n_missing`, `first`, `last`, `alpha`, `scale`, `holdout`, `horizon`, `var_level`, `log_lik`, `aic`, `bic`, `persistence`, `half_life`, `half_life_time`, `uncond_sigma`, `sigma_last`, `sigma_next`, `lb_lag_used`, `arch_lags_used`, `kappa` |
| `coef` | `mu`, `ar1`…, `ma1`…, `omega`, `alpha1`…, `beta1`…, `gamma1`…, `shape`, `skew` | null | `estimate`, `std_error`, `statistic`, `p_value`, `significance`, `ci_low`, `ci_high` (the series' units) |
| `assumption` | check id | a parameter (Nyblom's individual rows) or null | the common `_ASSUMPTION` columns + `advice_id`; `p_adjusted` for Nyblom's individual rows |
| `backtest` (new: level required) | `kupiec`, `christoffersen_ind`, `christoffersen_cc`, `counts` | null | `statistic`, `df`, `p_value`, `significance`, `verdict`, `h0`; counts `violations`, `expected_violations`, `violation_rate`, `n00`, `n01`, `n10`, `n11`, `longest_run` |
| `curve` | `returns` (series `return`, `band_low`, `band_high` = μ ± z_{1−α/2}σ), `sigma` (`sigma`, `forecast`, `uncond`), `news_impact` (`sigma2`), `backtest` (`return`, `var`) | series | stat `y`, x0 the time axis (or ε for the news impact) |
| `point` | `std_residual`, `violation`, `qq` | — | `y` |
| `lag` | `z_acf`, `z2_acf` | — | `acf`, `bound` = z_{1−α/2}/√n |
| `hist` | `std_residual` | — | counts, with the fitted density as a `curve` `density` |
| `reading` | the block | null | `line_*` (written by the layout, `common.READINGS`) |

Long series are thinned for their charts by `ts_envelope` (min and max per bucket: the clusters stay
visible); every number uses every instant. Rows: at most ≈ 5 × 2,000 curve points + 2 × 1,000 lags +
2,000 points: under 20,000.

### Explaining the method in the report

The explanations spec's mechanism: `layouts/garch.py` registers `common.READINGS["garch"] = readings`,
which picks template ids from the rows (verdicts by the rows' `verdict`, measures by their values) and
writes `reading|<block>|<level_2>|line_<k>` = `@<id> name=value…`; `report_texts/garch.json` holds the
five languages (`reading_texts`, `formula_texts`, `measure_readings`, `verdict_words`). New
`NOTE_NAMES`: `model`, `distribution`, `parameter`.

| Block | Reading, with the run's numbers | Method (last page) |
|---|---|---|
| `summary` | «A GJR-GARCH(1,1) with skewed t errors was fitted to 763 daily returns of «grano_duro» (3 Jul 2023 – 15 Jul 2026); the last 250 were kept out to test the VaR.» | What a conditional variance is; the model in words; QML; the scale |
| `persistence` (measure) | «The volatility keeps 97.8 % of a shock from one day to the next: half of a shock is gone after about 31 trading days. Today's volatility, 2.1 % a day, is above its long-run level of 1.6 %.» | P, κ, half-life, the unconditional variance |
| `asymmetry` (measure + `sign_bias` verdict) | «A fall of 2 % raises tomorrow's variance 2.4 times as much as a rise of 2 % (γ = 0.09).» + the sign bias verdict sentence | GJR's γ, EGARCH's α and γ, the news impact curve |
| `distribution` (measure) | «The errors have heavy tails: ν = 5.8 (95 % interval 4.2 – 8.9); a normal distribution would have ν = ∞.» | the t, the skewed t |
| `residuals` (verdicts) | «Up to lag 11 the squared standardized residuals show no autocorrelation (p = 0.43, α = 0.05): the model captures the volatility's clusters as far as this test can see.» | the weighted portmanteau, Engle's LM, why on z and z² |
| `stability` (verdict) | «The parameters are stable over the sample (Nyblom's joint test, p = 0.21).» or «… move over the sample (p = 0.003); ω moves most (adjusted p = 0.004).» | Nyblom's scores, the bridge law, Holm |
| `backtest` (verdicts) | «In the 250 days kept out the return fell below the 99 % VaR on 6 days, against 2.5 expected: the coverage test rejects a 1 % rate (p = 0.04). The breaks do not cluster (p = 0.58).» | Kupiec, Christoffersen, the expected count's caveat |
| `method`, `formulas` | — | «How this report was computed»: a paragraph per part that ran; the formula box (the mean, the variance equation of the run, the distribution's density, the likelihood, the sandwich, P and half-life, Q_W, the LM regression, Nyblom's L, the sign bias regression, the VaR, LR_uc, LR_ind, LR_cc) |

Template ids (G4 writes them; the fixtures list them): `summary`, `summary_holdout`, `persistence`,
`persistence_integrated`, `sigma_now_above|below|equal`, `asymmetry_gjr|egarch|none`,
`sign_bias_supported|not_supported`, `distribution_norm|std|sstd`, `lb_z_*`, `lb_z2_*`, `arch_lm_*`,
`nyblom_stable|moves`, `backtest_coverage_*`, `backtest_independence_*|not_computed`,
`backtest_expected`, `at_bound`, `method_*`, `formula_*`.

### The report (pixel perfect)

`backend/app/engine/models/layouts/garch.py`; words in `report_texts/garch.json`; A4 portrait,
`common.page`, the header and footer of every model; each time chart twice (dates, integers), one
visible by `time_kind`, as `layouts/arima.py` does.

1. **Summary.** Header band; four cards — *Observations* (n returns, the span, «250 held out»),
   *Model* (GJR-GARCH(1,1) · skewed t), *Persistence* (0.978 · half-life 31 days), *Volatility now*
   (σ_{T+1} and the long-run σ under it, no colour); the formula line; **the checks table** («Checks at
   α = 0.05»: the two portmanteau tests, ARCH-LM, Nyblom joint, sign bias, the backtest's three rows when
   there is a holdout); **the measures table** («Measures, not tests»: persistence, half-life, long-run
   σ, ν, ξ, the facts); readings `summary`, `persistence`.
2. **Volatility.** Returns as points (muted) with the band μ ± z_{1−α/2}σ_t (band_low/high) — 70 mm;
   σ_t as a line, the forecast dashed after the last instant, the long-run σ as a horizontal target —
   60 mm; standardized residuals as points with 0 — 40 mm. The holdout's span shaded by `target_x` at its
   start.
3. **Coefficients** (the table of `coef`, the series' units, robust or classical said in the header)
   and the **news impact curve** (xy line, x = ε_{t−1} in σ units, 60 mm; symmetric for GARCH, kinked for
   GJR, skewed for EGARCH), readings `asymmetry`, `distribution`.
4. **Checks.** ACF of z and of z² side by side (bars as stems, ±bounds, 50 mm each); the histogram of z
   with the fitted density; the Q–Q of z against the fitted distribution (`tab_qq` with the fitted
   quantile function); Nyblom's table (the joint row, one row per parameter: L_i, p, adjusted p);
   readings `residuals`, `stability`.
5. **Backtest** (with a holdout): the held-out returns as points, the 1-step VaR as a line, the violations
   as red points (90 mm); the counts table and the three tests; reading `backtest`.
6. **How this report was computed.** 7. **Notes**: the scale, the solver that converged, the parameters
   at a bound, the software (`R 4.6.0; rugarch 1.5-6; Rsolnp 2.0.1; FinTS 0.4-9`), the group's version.

**Excel**: sheets *Coefficients*, *Checks*, *Measures*, *Backtest*, *Reading*; the volatility itself is the
node's output.

### Card, MLflow and Predict

**Card** (`garch.*`): the specification (variance model, orders, mean, distribution), the parameters on the
series' units with their covariance (≤ 12 terms), `input_kind`, `returns_type`, `var_level`, the
frequency, the last time, **the state** the filter needs to continue: the last max(q, n) ε, the last p
σ² (ln σ² for EGARCH), the last m returns, the last z for EGARCH, the last price with `prices`. A few
hundred bytes; no series.

**MLflow** logs as for every model: parameters, metrics (every number of `model`, `coef.<term>.<stat>`,
`assumption.<check>.<stat>`, `backtest.<test>.<stat>`), `results.parquet`, `report.pdf`, `model.json`,
`MLmodel`, `native.rds` (the `uGARCHfit` without its data: `tab_native_lean`).

**Predict: yes** (lot G6). A registered GARCH applied to the returns (or prices) **after** the card's
last time: the filter runs the card's parameters, fixed, from the card's state through the new rows
(`ugarchfilter` with `fixed.pars`, or the recursion of «The simulation» with the observed shocks, checked
equal): per row `prediction` = σ_t (the conditional volatility of that row given the rows before it) and
`prediction_var` = the 1-step VaR at the card's `var_level`; with `horizon`, forecast rows after the last
one (`prediction` = σ_{T+k}), as ARIMA's Predict appends its horizon. A first new time not after the
card's last is refused («the rows start before the model's last day: Predict continues a series»). This
is the daily use of a volatility model: fitted monthly, applied each day without refitting.
`PREDICT.applies_to.horizon` gains `garch`; `PREDICT.columns.garch` = `{"always": ["prediction",
"prediction_var"]}`.

## Part 2 — the node `risk_simulation`: Risk simulation

### What it is, and why a node of its own (Open question 1)

The node takes the series (the same input as the ARIMA or GARCH node), fits **its own** model — ARIMA or
GARCH, with the same keys and **the same runner functions** as those nodes, so the same options give the
same estimates — and simulates N future paths of h steps from the end of the series. From the paths it
measures the risk; from the holdout it backtests the model's 1-step VaR.

Why not an output of the ARIMA and GARCH nodes: those nodes stay light (the owner's rule holds for them
unchanged), their reports stay what they are, and the simulation's options (paths, levels, threshold,
position, innovations) do not crowd two forms. Why not «from the model node before it»: a flow's edge
carries a table, not a fitted model; reading the previous node's card would be a new mechanism of the
orchestrator and of the trial (Run in the editor would need that node's latest run). Why not from a
registered model only (as Predict): it would need MLflow, and bootstrapped innovations need the
standardized residuals, which a card of 1 MiB cannot hold for 200,000 rows. **The editor makes «starting
from a fitted model» one click**: an ARIMA or GARCH node's menu gets «Simulate risk from this model»,
which drops a Risk simulation node on the same input with the model's options copied (lot G5).

### The form

The model's keys are the two nodes' own, shown by `applies` on `model`:

| Key | Kind | Default | Applies | Meaning |
|---|---|---|---|---|
| `time`, `value`, `frequency` | as ARIMA's | — (needed) | always | |
| `model` | choice `garch` \| `arima` | `garch` | always | The model of the series |
| GARCH's keys | as «The form» of part 1 (`input_kind`, `returns_type`, `variance_model`, `arch_order`, `garch_order`, `ar_order`, `ma_order`, `distribution`, `se_type`) | as there | `model = garch` | |
| ARIMA's keys | as the ARIMA node's (`s`, `orders`, `p`…`Q`, the `auto` keys, `drift`, `transform`, `box_cox_lambda`); **no `exog`** (future exogenous values cannot be simulated) | as there | `model = arima` | |
| `mean` | boolean | true | always | ARIMA's `include.mean` and GARCH's μ: the same meaning, one key |
| `aggregate` | choice `last` \| `sum` \| `relative_change` | `last` | `model = arima` | The risk variable (below); GARCH's is always `relative_change` |
| `paths` | integer 100–10,000,000 | **10,000** | always | N |
| `horizon` | `horizon` | **10** | always | h steps |
| `innovations` | choice `parametric` \| `bootstrap` | `parametric` | always | Draws from the model's distribution, or resampled standardized residuals (filtered historical simulation, Barone-Adesi, Giannopoulos & Vosper 1999; ARIMA: its residuals) |
| `var_level` | `risk_level` | **0.99** | always | VaR level c |
| `es_level` | `risk_level` | **0.975** | always | ES level c' |
| `tail` | choice `lower` \| `upper` | `lower` | always | Which side is the risk: falls (a return, a cash balance) or rises (a demand, a cost, claims) |
| `quantiles` | `quantiles` | **0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99** | always | The fan's bands and the summary's `quantile_*` columns |
| `threshold` | number (finite) | none | always | A level in the fan's unit (price, cumulative return, level or running total) whose crossing is counted |
| `position` | `positive` | none | risk variable `relative_change` | An amount invested: every measure of the risk variable in money (P&L = position × relative change) |
| `drawdown` | boolean | true | risk variable `relative_change` | The maximum drawdown of each path |
| `holdout` | `holdout` | 0 | always | The backtest of the 1-step VaR (part 1, «The holdout and the backtest») |
| `alpha`, `seed`, `lb_lag`, `arch_lags` | as part 1 | 0.05, **1**, `auto`, `auto` | always | `seed` drives every draw |
| `output` | choice `summary` \| `paths` \| `results` | **`summary`** | always | What the next step receives (Open question 7) |

`paths` 10,000: a 99 % VaR then rests on 100 tail values, and the report gives its Monte Carlo interval;
quadrupling the paths halves it, which the reading says. `horizon` 10: the market-risk rules' ten days.

### The risk variable and the fan

One quantity H_k per path and step k = 1…h carries every measure:

| Model and input | H_k (the risk variable) | The fan shows | `threshold` is in |
|---|---|---|---|
| GARCH, `returns` | the cumulative simple return C_k = exp(Σ_{j≤k} r_j) − 1 (log) or Π(1 + r_j) − 1 (simple) | C_k | a return (−0.10 for −10 %) |
| GARCH, `prices` | C_k as above | the price P_k = P_T (1 + C_k) — an exact linear map of H_k | a price |
| ARIMA, `aggregate = last` | the value y_{T+k} | y_{T+k} | the series' unit |
| ARIMA, `aggregate = sum` | the running total Σ_{j≤k} y_{T+j} (a month's demand) | the running total | the series' unit |
| ARIMA, `aggregate = relative_change` | y_{T+k} / y_T − 1 (y_T ≠ 0, else refused) | y_{T+k} = y_T (1 + H_k) | the series' unit |

With `position` V the measures read V·H_k (money). The price of the fan, P_T(1 + H_k), and the money,
V·H_k, are increasing linear maps of H_k: quantiles, means and expected shortfalls map exactly, so one
tracked quantity per step serves them all.

### The simulation

**GARCH paths** (conditional on the end of the series). The filter, with the fitted parameters fixed,
runs to the last observation T (through the holdout too, «The holdout»): it gives σ²_{T+1}, the last
ε, z, r. For k = 1…h and each path: draw z_{T+k} (below), ε = σ z, the mean recursion gives r_{T+k}, the
variance recursion σ²_{T+k+1}; the cumulative quantities update. **The recursion is the runner's own,
vectorized across the batch's paths step by step, elementwise only** (no matrix product: a BLAS kernel
may round differently with the matrix's size, which would break batch invariance). It is checked in the
self-test against `rugarch::ugarchpath` given the same innovations (`custom.dist = list(name = "sample",
distfit = Z)`, `presigma`, `prereturns`, `preresiduals` from the fit's end) to 1e-12 relative, for each
variance model and a mean with AR and MA terms.

**ARIMA paths**: the expanded polynomials φ(B)Φ(B^s) and θ(B)Θ(B^s), the differencing (1 − B)^d (1 −
B^s)^D undone from the last observations, the drift or mean, the transform undone per value (Box–Cox's
inverse; a value outside its domain is counted in a note `box_cox_domain`, not dropped). Checked against
`forecast:::simulate.Arima(fit, nsim = h, future = TRUE, innov = …)` path by path to 1e-10 on the
reference cases.

**Innovations.** `parametric`: z from the fitted distribution — `rnorm` (Inversion), `rugarch::rdist("std",
…)`, `rugarch::rdist("sstd", …)` (they draw from R's generator); ARIMA: N(0, σ̂²). `bootstrap`: z = ẑ_U,
U uniform on the training's standardized residuals (`sample.int(n, …, replace = TRUE)`); ARIMA: its
residuals. Only here: the GARCH node itself never resamples.

**Seed blocks and streams (reproducibility).** The paths are numbered 1…N and cut into **seed blocks** of
B_s = 1,000 consecutive paths (the last may be shorter). Block b draws from its own stream: `set.seed(seed,
kind = "L'Ecuyer-CMRG", normal.kind = "Inversion", sample.kind = "Rejection")`, s₁ = `.Random.seed`,
s_{b+1} = `parallel::nextRNGStream(s_b)`; before block b the runner sets `.Random.seed <- s_b` and
draws the block's h × B_s innovations in one call, path by path. Consequences, each tested:

1. **The same seed gives the same numbers at every batch size**: a batch is a whole number of blocks, a
   block's draws never depend on the batch, the recursion is elementwise, and every sum is made per
   block and then added **in block order** (moments, bin sums, counts) — the order of the arithmetic is
   fixed by the blocks, not by the batches. Test: budgets of 64 MiB, 256 MiB and 2 GiB give `identical()`
   results and the same paths.
2. **A prefix property**: the first 2,000 paths of a run with N = 10,000 are the 2,000 paths of a run with
   N = 2,000. (The measures differ, since they use all N.)
3. B_s is part of the method, like the seed: changing it changes the draws. It is not tuned to memory.

**Batches from the memory limit.** The gateway adds `task.limits.memory_bytes` (the group's limit, in
bytes) and `task.limits.tmp_bytes` (the `/tmp` size limit of `modelTmpLimit`) — lot G0, two lines in
`jobs_models.go` beside `time_budget_seconds`. In a self-test or a local run they come from the task the
test writes. The runner sizes the batch after the fit:

- R₀ = the process's resident memory after the fit and after allocating the accumulators (read from
  `/proc/self/statm`);
- A = the accumulators (below): about 24·B·h bytes, B the bins;
- per path, the batch holds m doubles per step: the innovations, ε, σ² (GARCH), y, H, and the Arrow
  columns written (value, cumulative, sigma, price) plus the 4-byte ints (path, step) — **m is counted in
  the code** for each model and output, one constant per case;
- ρ = the measured ratio of the batch's peak resident growth to its 8·m·h bytes per path (R's copies,
  Arrow's buffers): G2 measures it at three batch sizes on each case and writes the largest in the code,
  with its measurement in this spec's «As built»;
- **batch paths** = ⌊(memory_bytes − R₀) / (ρ · 8 · m · h)⌋, rounded down to whole seed blocks. If not
  even one block fits: refused `horizon_memory` («a block of 1,000 paths of 1,000 steps needs about 96 MB
  and the group's memory leaves 40 MB after the fit: fewer steps, or more memory for the group in
  Administration → Statistical models»).

After each batch: write, free (`rm`, `gc(full = FALSE)`). R reuses the freed memory, so the resident size
stays at the first batch's peak. `run.json` records the peak read from the cgroup (`memory.peak`, cgroup
v2) or `VmHWM`, the batch size and the number of batches.

### The Parquet file of the paths

- **Where**: `/tmp/paths.parquet` (the Job's disk) — always, because the second pass reads it. With
  `output = paths` it is then copied to `TABULARIA_OUTPUT` **in chunks** (an Arrow input stream to an
  output stream, 8 MiB at a time: `tab_copy_file` reads a whole file into memory and is not used); with
  `summary` or `results` it is deleted at the end.
- **Writer**: one `arrow::ParquetFileWriter` opened once on a `FileOutputStream`, **one row group per
  batch** (`WriteTable(batch, chunk_size = rows of the batch)`), compression zstd, closed at the end
  (assumed API of arrow 26.0.1; G2 checks it and that a row group is never split). If a batch has more
  rows than the writer's maximum row group length (Arrow C++'s default 1,048,576 rows, assumed; G2 reads
  the value in 26.0.1), the batch is made smaller to fit one row group — a bound of the library, not ours.
- **Schema**, rows sorted by path, then step (a path's rows contiguous):

| Column | Type | When |
|---|---|---|
| `path` | Int32 | 1…N |
| `step` | Int32 | 1…h |
| `time` | **`=time`** | `ts_time_after(T, step)` of the input's type (`ts_time_array`) |
| `value` | Float64 | the simulated series: a return (GARCH) or y (ARIMA) |
| `cumulative` | Float64 | H_k |
| `price` | Float64 | GARCH with `prices` (`rows_when`) |
| `sigma` | Float64 | GARCH: σ_{T+k} of that path (`rows_when`) |

- **Disk**: after the first row group the runner reads the file's size, projects the whole (bytes per row
  × N·h) and refuses `too_many_values` when it would exceed `tmp_bytes` − what `/tmp` already holds
  («10,000,000 paths of 250 steps would write about 64 GB; this Job's disk holds 2.4 GB: fewer paths or
  steps»). Estimate (assumed): 26–30 bytes per row with zstd (three doubles that do not compress, the
  ints and the time run-length encoded): 10 million values ≈ 300 MB.

### The measures, without holding the paths (two passes)

**Pass 1** (while simulating, per seed block, added in block order): for each step k and for each
tracked quantity — H_k, and per path the maximum drawdown D (with `drawdown`):

- n, Σx, the central moments to the 4th by Pébay's (2008) pairwise merge (mean, sd, skewness, excess
  kurtosis: exact up to rounding, in a fixed order);
- a **histogram** of B = 1,024 equal bins between the minimum and maximum of the **first seed block** for
  that quantity, plus an underflow and an overflow bin; per bin the count (integer), Σx and Σx² (for
  the ES's standard error). Values beyond the first block's range fall in the two outer bins and are
  still counted and summed exactly;
- with `threshold`: per step the count of paths beyond it (`tail` side) at step k, and the count of
  paths that have crossed it at any step ≤ k (a running flag per path within the batch).

Memory A ≈ (h + 1) × (B + 2) × 24 bytes: 25 MB at h = 1,000, 0.25 MB at h = 10.

**Pass 2** (after the last batch, the file read one row group at a time, only the needed columns): the
exact order statistics the measures need are located by the pass-1 counts — the rank r of a target falls
in a known bin with known counts below it — and pass 2 **keeps only the values falling in those target
bins**, sorts them, and reads the order statistic. Their number is known exactly before pass 2 (the
bins' counts): if it exceeded the memory left, the targets would be split over more reads (never
needed at the sizes measured: a tail bin of 1,024 equal bins holds about 1.5·10⁻⁴·N values at the 1 %
quantile of a normal shape). The fan's band edges (`quantiles` at every step, central bins are full)
are not selected: pass 2 counts their target bin's values in a sub-histogram of B bins, which locates
each edge within **ε_k = (bin width at step k) / B**, written per step as `band_error` and said in the
report («the bands are drawn within 0.0004 % of the price»).

**The definitions** (lower tail; the upper tail mirrors them on −H), with X₍₁₎ ≤ … ≤ X₍N₎ the N values of a
quantity, p = 1 − c:

| Measure | Definition | Exact? | Monte Carlo interval at 1 − alpha |
|---|---|---|---|
| `var` | X₍⌈Np⌉₎, the lower empirical quantile (type 1, the inverse of the empirical distribution function) | yes | distribution-free: [X₍l₎, X₍u₎], l = qbinom(α/2, N, p), u = qbinom(1 − α/2, N, p) + 1 (order statistics' binomial law) |
| `es` | Acerbi & Tasche (2002), on the risk variable's own scale (no sign flip): ES = (1/(Np')) [Σ_{i≤⌊Np'⌋} X₍i₎ + (Np' − ⌊Np'⌋) X₍⌊Np'⌋+1₎], p' = 1 − c' | yes (bin sums below + the selected values) | normal, with se² = [Var(X \| X ≤ VaR_{c'}) + (1 − p')(ES − VaR_{c'})²] / (Np') (Manistre & Hancock 2005) |
| `prob_beyond` (k) | share of paths with H_k beyond `threshold` | yes (counts) | Clopper–Pearson |
| `prob_crossed` (k) | share of paths that crossed `threshold` at some step ≤ k | yes | Clopper–Pearson |
| `drawdown_mean`, `drawdown_median`, `drawdown_var` | of D = max_{k≤h} (1 − W_k / max_{j≤k} W_j), W_k = 1 + C_k, W₀ = 1 | yes | as `var` |
| `mean`, `sd`, `skewness`, `kurtosis` | of H_k | yes (moments) | mean: normal, se = sd/√N |
| `quantile_<q>` (k) | the fan's edges | within ε_k | — |

VaR and ES are written **as values of the risk variable** (a return of −0.042, a price of 298.1, a
demand of 1,180) and, with `position`, in money; the reading says them as losses («the 10-day return falls
below −4.2 % with probability 1 %; when it does, it averages −6.1 %»). No sign convention to learn.

**Why these measures** (and not others): VaR because it is the measure regulators and risk offices
read, and what the backtest can test; ES because VaR says nothing beyond its quantile and ES is
coherent (Artzner et al. 1999) and the revised market-risk rules' measure; the threshold probabilities
because operations ask «will the balance go below zero within the month», a first-passage question no
quantile at the horizon answers; the quantiles per step because the fan is how a person sees the
uncertainty grow; the drawdown because a path can fall deep and recover by the horizon, a loss the
horizon's VaR never sees; the moments to show how far from normal the outcome is (a GARCH with t
errors aggregated over ten days is skewed and fat-tailed). **Not now**: expectiles, spectral measures,
conditional drawdown at risk, an ES backtest (McNeil & Frey 2000; Acerbi & Székely 2014 — they need a
simulation per held-out day; Open question 17).

**No verdict on any of them.** The Monte Carlo intervals are measures of the simulation's precision,
said as such («with 10,000 paths the 99 % VaR is known within −4.0 % … −4.5 %; four times the paths
would halve that»).

### Time

The runner starts its clock at the task's `started_unix` (new in G0: the gateway writes the time it
creates the Job; the runner's own start otherwise). After the first batch it projects the rest from what
it measured — the batch's time × the batches left, pass 2 at the read rate of the first row group, the
copy at the write rate — and refuses `too_slow` when the projection passes `time_budget_seconds`
(«about 14 minutes for 2,000,000 paths of 250 steps; the node's timeout is 10 minutes: fewer paths or
steps, or a longer timeout, up to the group's 30»). It checks again after every batch. A refusal leaves
no partial result: the same seed must always give the same numbers, and a cut that depends on the
machine's speed would not (Open question 6).

### The output

`summary` (default) — one row per step, the risk variable's distribution:

| Column | Type | When |
|---|---|---|
| `step` | Int32 | always |
| `time` | **`=time`** | always |
| `mean`, `sd` | Float64 | always |
| `quantile_<q>` | Float64, one per `quantiles`, increasing | always (wildcard) |
| `price_quantile_<q>`, `price_mean` | Float64 | GARCH with `prices` (wildcard, `rows_when`) |
| `var`, `var_low`, `var_high` | Float64 | always (at `var_level`) |
| `es`, `es_low`, `es_high` | Float64 | always (at `es_level`) |
| `prob_beyond`, `prob_crossed` | Float64 | with `threshold` (`rows_when`: `threshold` given) |
| `band_error` | Float64 | always |

`paths` — the Parquet file of «The Parquet file of the paths», as the next step's input.

`OUTPUT_TYPES["risk_summary"]` and `OUTPUT_TYPES["paths"]` as the tables say. Wildcards in
`_ROWS_WILDCARDS`: `quantile_` ← the numbers of `quantiles` (the rule the Timestral spec's lot F0 adds
for its own `quantile_<level>`: G0 checks it is the same rule — the levels as the catalogue writes a
number, increasing, each once — and otherwise names these `sim_quantile_<q>`), `price_quantile_` ← the
same. The three copies (engine, editor, assistant) as for the decomposition.

### The results table

| Section | `level` | `level_2` | Holds |
|---|---|---|---|
| `model` | null | null | texts as part 1 or ARIMA's (by `model`), `risk_variable`, `tail`, `innovations`, `aggregate`; numbers `paths`, `horizon`, `seed`, `seed_block`, `batches`, `batch_paths`, `bins`, `var_level`, `es_level`, `threshold`, `position`, `last_value`, `simulated_values`, `paths_bytes`, `seconds_simulation`, `seconds_pass_2` |
| `coef` | as the fitted model's | | the fit (compact) |
| `assumption` | the fitted model's checks (part 1, or ARIMA's) | | as there |
| `risk` (new: level required, level_2 optional) | `var`, `es`, `prob_beyond`, `prob_crossed`, `drawdown`, `moments` | the step («10»), or null for the horizon | `value`, `low`, `high`, `se`, `count`, `mean`, `sd`, `skewness`, `kurtosis`, `median` |
| `backtest` | as part 1 | | |
| `curve` | `fan` (series `quantile_<q>` as band_low/band_high pairs, nested, `median`, `history`, `path_<i>`), `term` (`var`, `es`, `prob_crossed` per step, their intervals as bands), `backtest` | series | `y`, x0 the time axis |
| `hist` | `horizon` (H_h), `drawdown` | — | the pass-1 histogram merged to at most 60 bars (counts) |
| `reading` | the block | | `line_*` |

**The sample paths of the fan** are paths 1…20 (`max_sample_paths`): each path is an independent draw, so
the first twenty are a random sample, and they are the same at every batch size. Rows: fan ≤ 1,000 steps
× (9 quantiles + median) + 20 paths × 1,000 + 2,000 history + term 6 × 1,000 + 120 bars + backtest ≤
3,000: under 41,000, within `max_result_rows`.

### Explaining the method in the report

As part 1 (`common.READINGS["risk_simulation"]`, `report_texts/risk_simulation.json`). New `NOTE_NAMES`:
`paths`, `steps`, `level`, `threshold`.

| Block | Reading | Method (last page) |
|---|---|---|
| `summary` | «10,000 futures of 10 trading days were simulated from a GJR-GARCH(1,1) with skewed t errors fitted to «grano_duro» (seed 1).» | Monte Carlo of paths: what a path is, why many, the seed blocks |
| `var` (measure) | «With probability 1 % the 10-day return falls below −4.2 % (a loss of 4,200 € on 100,000 €); with 10,000 paths this value is known within −4.0 % to −4.5 %.» | the empirical quantile, its distribution-free interval |
| `es` (measure) | «When the return falls below its 2.5 % quantile (−3.1 %), it averages −4.4 %.» | Acerbi–Tasche, the standard error |
| `threshold` (measure) | «The price falls below 280 €/t at some day of the next 10 with probability 7.3 % (interval 6.8 – 7.8 %); on the 10th day it is below with probability 4.9 %.» | first passage against the horizon |
| `drawdown` (measure) | «Half the paths lose at least 2.1 % from a peak within the 10 days; 1 % lose more than 8.7 %.» | the drawdown |
| `shape` (measure) | «The 10-day return is skewed to the left (skewness −0.4) with heavier tails than a normal (excess kurtosis 2.1).» | moments |
| `fan` | «The bands hold 50 %, 90 % and 98 % of the paths at each day; they are drawn within 0.0004 % of the price.» | two passes, the declared error |
| `backtest` (verdicts) | as part 1 | as part 1 |
| `model` (verdicts) | the fitted model's main check sentences (part 1 or ARIMA's ids) | |
| `method`, `formulas` | — | the recursion, the innovations, the risk variable, VaR, ES, Clopper–Pearson, the order-statistics interval, the drawdown, LR tests |

### The report (pixel perfect)

`backend/app/engine/models/layouts/risk_simulation.py`, A4 portrait, the common page:

1. **Summary.** Cards — *Simulation* (10,000 paths × 10 days = 100,000 values · seed 1), *Model*
   (GJR-GARCH(1,1) · skewed t, or SARIMA(…)), *VaR 99 %* (−4.2 %, and «4,200 €» with a position),
   *ES 97.5 %*; the measures table («Measures, not tests», each with its Monte Carlo interval); the
   checks table (the backtest's three, the model's own); readings `summary`, `var`, `es`.
2. **The fan.** One xy chart, 110 mm: the last min(3h, 2,000) observations as a line (history), from T
   the bands as nested band_low/high pairs (98 %, 90 %, 50 % by default, lighter outward), the median
   line, the 20 sample paths as thin lines (`split_by` the path, muted), the threshold as a horizontal
   target, a vertical `target_x` at T. Readings `fan`, `threshold`.
3. **The outcome at the horizon.** The histogram of H_h (60 stems or steps on an xy chart) with VaR and
   ES as `target_x` lines (and the threshold when on this scale), 80 mm; beside or below it, with
   `drawdown`, the drawdown's histogram with its VaR line, 60 mm. Readings `shape`, `drawdown`.
4. **Through the horizon.** VaR and ES per step as lines with their Monte Carlo bands, 60 mm; with a
   threshold, `prob_crossed` and `prob_beyond` per step, 50 mm.
5. **Backtest** (with a holdout), as part 1's page 5.
6. **The model.** The coefficients and the checks of the fitted model, compact (the GARCH or ARIMA
   node's own tables), with a line «the full report of this model is the GARCH (ARIMA) node's».
7. **How this report was computed.** 8. **Notes**: seed, seed blocks, batches and their size, the memory
   peak, the file's size, the bins, the software, the group's version.

**Excel**: *Measures* (with intervals), *Per step* (the summary output), *Backtest*, *Model*, *Reading*.

### MLflow, and no model

Logged on a flow run: parameters, metrics (`risk.<measure>.<stat>` at the horizon, the backtest, the
fitted model's coefficients and checks), `results.parquet`, `report.pdf`. **Not the paths** (10 million
values are the flow's data, in its storage, when `output = paths`). `card: false`: the form hides
«Register the model as», and the gateway refuses `registerAs` with «a risk simulation registers no
model: register the GARCH or ARIMA node instead».

### Combinations and refusals

| Rule | Kind | Reason |
|---|---|---|
| ARIMA's keys | `applies` | `model = arima` (and their own rules, e.g. `p` with `orders = manual`) |
| GARCH's keys | `applies` | `model = garch` |
| `aggregate` | `applies` | `model = arima` |
| `position`, `drawdown` | `applies` | `model = garch`, or `aggregate = relative_change` |
| `disjoint` | | `time`, `value` |
| a variant's own `max_rows` | | `arima`: 20,000 (ARIMA's own limit, as its cost); `garch`: the group's, or what G0's measurement asks (Open question 13). The gateway takes the smallest of the group's, the model's and the variant's (a new field read in `jobs_models.go`) |

Runner refusals (exit 3): part 1's; `too_few_paths` («with 100 paths and a 99.9 % level no simulated value
lies beyond the VaR: at least 1,000 paths», when N·(1 − max(c, c')) < 1); `horizon_memory`,
`too_many_values`, `too_slow` (above); `zero_last` (`relative_change` from y_T = 0); the ARIMA's own.

## The package and the image

### `rugarch` and what it brings (checked online, CRAN, 2026-11-22)

`rugarch` 1.5-6, published 2026-07-18, GPL-3, maintainer Alexios Galanos, compiled (C/C++, Rcpp and
RcppArmadillo); CRAN checks **OK on all 13 flavours**, no note. Released since 2011, at least once a year
(1.5-1 2023-09, 1.5-2 2024-08, 1.5-3 2024-09, 1.5-4 2025-06, 1.5-5 2026-03, 1.5-6 2026-07). Imports:
Rsolnp, ks, numDeriv, spd, xts, zoo, chron, SkewHyperbolic, Rcpp, fracdiff, nloptr (+ base packages);
LinkingTo Rcpp (≥ 1.1.1), RcppArmadillo.

New packages, in an install order that satisfies the imports (the recipe installs pinned packages one by
one with `upgrade = "never"`):

| Package | Version | Licence | Compiled | Brought by | Note |
|---|---|---|---|---|---|
| `numDeriv` | 2016.8-1.1 | GPL-2 | no | rugarch, Rsolnp | in the regression and survival images already |
| `truncnorm` | 1.0-9 | GPL (≥ 2) | yes | Rsolnp | |
| `globals` | 0.20.0 | LGPL (≥ 2.1) | no | future | imports `codetools` (recommended) |
| `listenv` | 1.1.0 | Apache-2.0 | no | future | |
| `parallelly` | 1.50.0 | LGPL (≥ 2.1) | yes | future | |
| `future` | 1.82.0 | Apache-2.0 | no | future.apply | |
| `future.apply` | 1.21.0 | GPL (≥ 2) | no | Rsolnp | never called (Rsolnp's parallel starts only) |
| `Rsolnp` | 2.0.1 | **GPL-2** | yes | rugarch | |
| `nloptr` | 2.2.1 | LGPL (≥ 3) | yes | rugarch | builds the bundled NLopt with `cmake` (already in every R image's build libraries); in the customer lifetime image already |
| `FNN` | 1.1.4.1 | GPL (≥ 2) | yes | ks | |
| `kernlab` | 0.9-33 | **GPL-2** | yes | ks | |
| `mclust` | 6.1.4 | GPL (≥ 2) | yes | ks | |
| `gmp` | 0.7-5.1 | GPL (≥ 2) | yes | multicool | needs the system library GMP |
| `multicool` | 1.0.2 | **GPL-2** | yes | ks | |
| `mvtnorm` | 1.4-2 | **GPL-2** | yes | ks | in the survival image already |
| `pracma` | 2.4.6 | GPL (≥ 3) | no | ks | in the customer lifetime image already |
| `ks` | 1.15.3 | GPL-2 \| GPL-3 | yes | rugarch | also imports `KernSmooth`, `Matrix`, `mgcv` (recommended packages, assumed present in `rocker/r-ver`; G0 checks with `requireNamespace`) |
| `spd` | 2.0-1 | GPL | no | rugarch | imports `KernSmooth` |
| `chron` | 2.3-63 | **GPL-2** | yes | rugarch | |
| `DistributionUtils` | 0.6-2 | GPL (≥ 2) | yes | GeneralizedHyperbolic | |
| `GeneralizedHyperbolic` | 0.8-7 | GPL (≥ 2) | no | SkewHyperbolic | imports `MASS` (recommended) |
| `SkewHyperbolic` | 0.4-2 | GPL (≥ 2) | no | rugarch | |
| `rugarch` | 1.5-6 | GPL-3 | yes | — | |

Already in the recipe: `xts`, `zoo`, `fracdiff`, `Rcpp`, `RcppArmadillo`. **System library**: `libgmp-dev`
(GMP, LGPL-3 or GPL-2+), appended to `system_libraries` after `libgsl-dev`. (`ks`, `kernlab`, `mclust`,
`spd` serve `rugarch`'s kernel and semi-parametric distributions, which this piece never calls: they come
with the import, not with use.)

**Licences** (the method of the owner's check of 2026-10-30: the declared licence on CRAN, against the
product's AGPL-3.0): every package is GPL, LGPL or Apache — copyleft or permissive, **none proprietary,
none with a use restriction**. **Six are «GPL-2» only** (Rsolnp, kernlab, multicool, mvtnorm, chron,
numDeriv): the FSF reads GPL-2-only and GPL-3 code as not combinable into one program, the question the
decomposition spec raised about `maps` and avoided there. Here it cannot be avoided with `rugarch`
(Rsolnp and chron are its direct imports), and it is already present: `numDeriv` and `mvtnorm` sit in two
images today. What keeps the risk where it was: **Tabularia distributes none of these packages** — the
image is built in the customer's cluster from CRAN's sources when an administrator enables the group,
as for every model group; the R packages are loaded side by side at run time by the customer's own
build. Open question 3 puts the choice (and the alternative, GARCH written in the runner) to the owner;
a legal reading before any distribution of a built image is advised, as for Timestral.

### The recipe (G0)

`packages`: the 23 above appended after `Rlibeemd@1.4.4` in the table's order; `system_libraries`:
`["libgsl-dev", "libgmp-dev"]`; `pinned_on` the G0 day; resources unchanged (1 CPU, 2 GiB, 1,800 s);
`max_rows` unchanged (200,000).

### Build and size (estimates; G0 measures)

Any change to `packages.txt` reruns the whole package step (arrow and RcppArmadillo included): the
decomposition's D0 took 9 min 52 s on this machine and 11 min 44 s on kind. Added compiles: `rugarch`
(C++ with Armadillo: the heaviest, 2–4 minutes and about 1–1.5 GB per compiler), `kernlab`, `mclust`
(Fortran), `nloptr` (NLopt by cmake), `gmp`, `ks`, `Rsolnp`, `truncnorm`, `FNN`, `chron`,
`parallelly`, `DistributionUtils`, `multicool`, `mvtnorm`: **about 16–20 minutes** in all. The apt layer
changes (GMP), so the GSL layer's cache no longer matches. Size: **+60–90 MB** (installed libraries: rugarch
≈ 10 MB, kernlab, mclust, ks, nloptr a few MB each; GMP ≈ 2 MB): about 1.55–1.58 GB from 1.49 GB.
**Load time**: `library(rugarch)` loads its imports (ks loads kernlab, mclust, mgcv, Matrix): assumed
1–3 s on top of arrow and forecast's 1.6 s; G0 measures it, and the runner loads `rugarch` only on the
routes that need it (`garch`, `risk_simulation` with `model = garch`) so ARIMA and the decomposition do not
pay it.

On a running installation the recipe's new hash queues the group's rebuild; ARIMA and the decomposition
keep serving on the old version until the new one is self-tested (the models spec, «Enabling a group»);
since `765fe10f` the old release's pod leaves the new version alone during the rolling upgrade.

## Performance and limits

### Estimated cost (assumed; G0, G1 measure at 1,000, 10,000 and 100,000 returns, G2, G3 at 10 thousand, 1 million and 10 million simulated values; one CPU, `--network none`)

| Part | Cost | 1k | 10k | 100k |
|---|---|---|---|---|
| R start, arrow, forecast, rugarch loaded | fixed | 3–5 s | | |
| `ugarchfit` GARCH(1,1) t, hybrid | O(n) per likelihood, ≈ 100–300 evaluations | 0.2–0.5 s | 1–3 s | 10–30 s |
| robust standard errors, Nyblom (scores by numerical derivatives) | O(k·n) | < 0.2 s | 0.5 s | 3–8 s |
| weighted Ljung–Box, ARCH-LM, sign bias | O(n·m) | < 0.1 s | | < 1 s |
| Nyblom's p-value (Imhof) | fixed | < 0.1 s | | |
| filter, forecasts, output | O(n) | | | < 1 s |

Acceptance bound of G1: the GARCH node at 100,000 returns with every check under **60 s** of R, or the
model gets its own `max_rows` (Open question 13). ARIMA's own 20,000 holds in the risk node.

| Simulated values N·h | Draws (`rdist`) | Recursion | Write | Pass 2 | Expected |
|---|---|---|---|---|---|
| 10 thousand | ms | ms | ms | ms | dominated by the fit and R's start: 4–6 s |
| 1 million | 0.1–0.3 s | 0.1–0.2 s | 0.2–0.4 s | 0.2–0.4 s | under 2 s beyond the fit |
| 10 million | 1–3 s (t, skewed t the slowest) | 1–2 s | 2–4 s | 2–4 s | 6–13 s beyond the fit, about 300 MB on `/tmp` |

Acceptance bound of G3: 10 million values (100,000 paths × 100 steps, GJR skewed t) under **30 s** of R in
all, peak memory under the given `memory_bytes` at 256 MiB, 512 MiB and 2 GiB.

### Limits

| Limit | Default | Where | Beyond it |
|---|---|---|---|
| Rows | the group's 200,000; ARIMA's 20,000 (its variant in the risk node) | recipe / model / variant | the common sentence, before the Job |
| Orders | ARCH, GARCH 1–3 / 0–3; ARMA 0–5 | catalogue | the form's bound |
| `paths` | 100–10,000,000 | catalogue | the form's bound; then memory, disk and time by projection |
| `horizon` | 1–1,000 (`max_horizon`) | catalogue | the form's bound |
| `quantiles` | at most 9 (`max_quantiles`) | catalogue | the form's bound |
| Seed block | 1,000 paths (`seed_block`) | LIMITS, part of the method | — |
| Bins | 1,024 (`risk_bins`): changes only memory and the bands' declared error, never an exact measure | LIMITS | — |
| Sample paths drawn | 20 (`max_sample_paths`) | LIMITS | — |
| Memory, disk, time | the group's 2 GiB, `/tmp`'s size, the node's timeout | task | `horizon_memory`, `too_many_values`, `too_slow` |

**Not in this piece** (said in the docs): several series at once (a portfolio: DCC or copulas), exogenous
regressors in the variance, GARCH-in-mean, long memory (FIGARCH), regime switching, the uncertainty of the
parameters in the simulation (Open question 2), an ES backtest, a multi-step VaR backtest (overlapping
windows make the independence test meaningless), trading calendars for future dates (with `index` on
dates the future times are calendar days: the `step` column is the reliable one), simple returns
below −100 % (possible with fat-tailed draws: counted in a note `returns_below_minus_one`; log returns
avoid it).

## Sample data

**A series with clustered volatility is needed**: the orders per day have no volatility clusters, and a
risk node on them would teach nothing about GARCH. Proposal (Open question 12): a new reference table,
**`ref.quotazioni_materie_prime`** (Postgres, the `ref` schema of the public reference data: calendar,
regions — market prices are public data too), the daily prices the company pays for three of its raw
materials:

| Column | Type | |
|---|---|---|
| `data` | date | trading days: Monday–Friday without 1 Jan, Good Friday, Easter Monday (`_easter` exists), 1 May, 25–26 Dec — from 2023-07-03 to 2026-07-15 (≈ 765 days) |
| `materia` | text | `grano_duro` (€/t), `olio_evo` (€/kg), `caffe_verde` (€/kg) |
| `prezzo` | numeric(12, 4) | |

Drawn by `generate.py` from **its own generator** (a PCG64 on SEED + 2000, as the experiments' SEED +
1000), so every other table stays byte for byte what it was (the test that pins their hash at `small`):
log returns from a GJR-GARCH(1,1) with skewed t innovations, a parameter set per material (persistence
0.96–0.99, γ > 0, ν 5–8; the olive oil with a high starting volatility that calms down), the same size
at every scale (≈ 2,300 rows: the owner's rule of small data). `ensure_quotazioni()` adds it to a
database filled before it existed, as `ensure_experiments` does. Nothing existing changes.

**Flows** (added by the seed only when the timeseries group is enabled, as the decomposition's):
1. **«Durum wheat: volatility»** — the table, Filter `materia = grano_duro` → Volatility (GARCH): `prices`,
   log, GJR(1,1), skewed t, `frequency = index`, `holdout` 250. The first page: persistence and
   half-life, the asymmetry supported, ν, the backtest.
2. **«Durum wheat: risk of the next month»** — the same series → Risk simulation: GARCH as above, 10,000
   paths, 21 steps, `position` 100,000, `threshold` a price 10 % below the last, `holdout` 250.
3. **«Orders: will next month's orders exceed 1,000?»** — the decomposition's daily orders (calendar join,
   fill 0) → Risk simulation: ARIMA(1,0,1)(0,1,1)[7], `aggregate = sum`, `tail = upper`, `threshold`
   1,000, horizon 30: a risk node on a business series, upper tail.

## Self-test, reference values, witnesses

**Reference data** (`reference/data/*.csv`, written by `make_reference_garch.R` in the image with fixed
seeds; ≤ 5,000 rows each):

| File | Rows | What it tests |
|---|---|---|
| `garch_t.csv` | 2,000 | a GARCH(1,1) t: estimates near the truth, checks pass, Nyblom stable |
| `gjr_sstd.csv` | 2,000 | a GJR skewed t: γ, ξ, sign bias supported with sGARCH, not with GJR |
| `egarch_norm.csv` | 2,000 | EGARCH normal |
| `arma_garch.csv` | 1,500 | AR(1)-MA(1) mean with GARCH: the mean's coefficients and the z checks |
| `garch_break.csv` | 2,000 | ω doubles halfway: Nyblom rejects, names ω |
| `prices_trading.csv` | 800 | prices on trading days, `index` with dates, `prices` log and simple, the first row kept |
| `garch_types.csv` (+ parquet twins) | 300 | Int64 above 2^31, Int32, date32, timestamp[us, UTC]: `=time` kept in `volatility`, `summary`, `paths` |
| `garch_edge_*.csv` | 5–150 | constant, too short, a price ≤ 0, a gap, a duplicate time, names like code |
| `risk_*` cases | — | the risk node on the files above: small N (10,000 paths × 10 steps) |

**Inside the image** (the self-test, `run.R --selftest`):

- the runner's GARCH fit equals `ugarchfit` called directly on the same numbers (after the scale's
  back-transform: 1e-6 relative on the parameters, 1e-8 on the log-likelihood);
- the simulation's recursion equals `rugarch::ugarchpath` with the same innovations (1e-12), for the four
  variance models and an ARMA mean; ARIMA's equals `forecast:::simulate.Arima(future = TRUE, innov = …)`
  (1e-10);
- **the streaming measures equal the same measures on the whole matrix** for the small cases (the test
  alone holds 10,000 × 10 values): VaR, ES, probabilities, drawdown quantiles, moments **exactly** (to
  1e-12), the fan's edges within their `band_error`;
- **batch invariance**: `memory_bytes` 64 MiB, 256 MiB, 2 GiB → `identical()` results and paths;
  the prefix property;
- **memory**: each runner case is run in a child `Rscript` that writes its `VmHWM`; the simulation cases
  with `memory_bytes` = 256 MiB must stay below it (and must have used more than one batch);
- Kupiec and Christoffersen equal `rugarch::VaRTest` on the reference cases (or the documented gap);
- Nyblom's statistics equal `rugarch::nyblom`; its p-value at the exact ∫B² quantiles;
- the reading ids of each case are part of `expected.json`, compared exactly.

**Witnesses** (outside the image: `make_witness_garch.py` with `uv` on `witness.json`'s pins — numpy
2.5.5, scipy 1.18.1, pandas 3.1.1, statsmodels 0.15.0 already pinned, plus **`arch` 9.0.0** (PyPI,
2026-11-11, NCSA licence, checked online); and hand computations):

| Check | Witness | Tolerance |
|---|---|---|
| GARCH, GJR (`arch`'s GARCH with `o = 1`), constant and AR means, normal and t | `arch.arch_model(…).fit()` with `backcast` set to `rugarch`'s initial variance (the same recursion start) — the log-likelihood at `rugarch`'s estimates, then the estimates | log-likelihood 1e-8; estimates 1e-4 relative (two optimizers) |
| EGARCH normal | `arch`'s EGARCH (its size term uses E\|z\| = √(2/π), `rugarch`'s the distribution's own: equal under the normal) | as above |
| EGARCH t, any MA mean, skewed t | a Python transcription of the likelihood (the recursion, `rugarch`'s `dsstd`) evaluated at `rugarch`'s estimates and maximised by SciPy | 1e-8; 1e-4 |
| Robust standard errors | the sandwich from numerical scores and Hessian of the transcription | 1e-3 relative |
| Persistence, κ, half-life, unconditional σ | by the formulas (κ by quadrature of the fitted density) | 1e-8 |
| Weighted Ljung–Box, ARCH-LM, sign bias | by the formulas (statsmodels OLS for the LM and sign bias regressions; `acorr_ljungbox` for the plain Q as a check of the ACF) | 1e-10 |
| Nyblom | the statistic by the formula from numerical scores; the p-value against Anderson–Darling's quantiles (k = 1) and Hansen's table (k ≤ 10) | 1e-5; the table's error |
| Kupiec, Christoffersen | by hand (`scipy.stats.chi2`) | 1e-12 |
| VaR, ES, intervals, probabilities on the paths | numpy on the paths Parquet read whole (`np.quantile(method = "inverted_cdf")`, Acerbi–Tasche by hand, `scipy.stats.beta` for Clopper–Pearson) | exact (1e-12) |
| Counts, texts, verdicts, reading ids, refusals | — | exact |

## Security

- **Names are data**: a column's name reaches the report as static text and the readings as encoded values
  (the decomposition's D9 test with `=HYPERLINK(…) #set ] 1% <b>` is repeated for both nodes); no `eval`,
  `parse` or formula from a name; `rugarch` receives numeric vectors only.
- **Bounds before allocation**: the gateway's row limits before the Job; `paths`, `horizon`, orders and
  `quantiles` bounded by the catalogue; `threshold` and `position` finite and within ±1e15 (a kind bound);
  the batch sized **before** anything is allocated, refused if one block does not fit; the disk and time
  projected before the second batch; `N·h` computed in doubles (never an integer overflow).
- **Files**: the runner writes only `/tmp/paths.parquet` and the protocol's addresses; the paths file is
  deleted at the end when it is not the output.
- **The image**: `rugarch` and GMP exercised in the self-test as uid 10001 on a read-only root, no network;
  extreme inputs (1e300, 1e-300, a step, a constant run, 100 returns) return or refuse with a sentence,
  never abort.
- **Assistant**: no new path from a name into a prompt; the flow prompt's rule that names and rows are data
  holds.

## Boundaries

- **Always**: verdicts only from tests against the node's alpha; the time column's type kept; the paths never
  all in memory, the batch from the memory limit, the same seed the same numbers at every batch size; every
  sentence from a template in five languages; ARIMA's and the decomposition's results unchanged (their
  `expected.json` pass untouched).
- **Ask first**: any package beyond the 23 and GMP; a change to ARIMA's outputs; KPSS's p-value from the
  bridge law in ARIMA or the decomposition; a fourth distribution.
- **Never**: a bootstrap or a simulation in the GARCH node; a fixed cut on persistence, VaR or ES; a partial
  simulation cut by the clock presented as a result; `tab_copy_file` or `write_parquet` on the paths; a model
  card for the risk node.

## Success criteria

1. On kind, the timeseries group is rebuilt from its new recipe **by the new release's pod only** (the
   orchestrator fix), self-tests, and *Volatility (GARCH)* and *Risk simulation* appear under *Time series*;
   ARIMA and the decomposition still pass their reference cases.
2. «Durum wheat: volatility» gives a report whose first page shows persistence and half-life as measures,
   the checks with verdicts and p-values, the backtest, readings in five languages with the run's numbers.
3. «Durum wheat: risk of the next month» gives VaR, ES, the threshold's probabilities and the drawdown with
   their Monte Carlo intervals, the fan with 20 sample paths, the backtest.
4. The output columns equal what the editor showed before the Run; an Int64 time stays Int64 in
   `volatility`, `summary` and `paths`.
5. 10 million simulated values within the group's 2 GiB: the Job's cgroup peak (in `run.json`) below the
   limit, never OOM-killed; the same seed with the group's memory set to 1 GiB gives the same numbers.
6. The numbers equal the witnesses within the table's tolerances.
7. GARCH Predict on the next month's prices of a registered model gives σ and VaR per row.

## The lots G0–G9

Rules of the earlier lots: **G0 first, alone**; lanes on disjoint files after it; **one heavy job at a
time** on this machine (16 GB, it has already restarted for lack of memory): an image build, the engine
suite, the gateway suite, a kind run, a timing run at 10 million values. The image is built once, in G0;
later lots run their self-tests with `cmd/modelgroup code timeseries <dir>` mounted on it (light). No lot
changes `recipe.json` after G0. Agents: sonnet for the medium work, haiku for the simple, opus for the
difficult and the review. **Every lot that runs R or Python on data has a test that measures its peak
memory** (the column «Memory test»).

**Where it fits.** The owner's order (2026-11-22): the explanations B3 and B4, then **Timestral F0–F9**,
then this piece, then the full regression and the security review. G0 starts when F9 is committed (F0–F7
edit the catalogue and its copies, `jobs_models.go`, `layouts/common.py`, the editor and the assistant,
which G0, G4, G5 and G7 edit too), and its image build runs in a window with no suite and no kind run.
Nothing of this piece is pushed without the owner.

```text
G0 contract, recipe, task limits, image, measurements (first, alone; after Timestral F9)
 ├── G1 garch.R: fit, scale, checks, Nyblom p, backtest (ts_risk.R), output, card ──┬── G6 GARCH Predict
 ├── G2 the simulation engine: streams, batches, writer, two passes, measures ──────┤
 │        └── G3 risk_simulation.R: ARIMA and GARCH paths, risk variable, output ───┤
 ├── G4 reports and explanations (two layouts, five languages) ─────────────────────┤
 └── G5 editor: two nodes, «Simulate risk from this model» ─────────────────────────┤
                                                                                     ▼
                       G7 sample data, flows, docs, assistants ── G8 kind ── G9 final review
```

| Lot | Objective | Files (owned) | Depends on | Accepted when (small data) | Memory test | Heavy? | Agent |
|---|---|---|---|---|---|---|---|
| **G0** Contract, recipe, image | Catalogue: `garch` and `risk_simulation` in the group (kinds `risk_level`, `garch_distribution`, `variance_model`, `se_garch`, `input_kind`, `returns_type`, `aggregate`, `innovations`, `tail`, `paths`), variants (`garch`: one; `risk_simulation`: `garch`, `arima`), `applies`, sections `backtest`, `risk`, charts, `OUTPUT_TYPES` `volatility`, `risk_summary`, `paths`, wildcards `quantile_`, `price_quantile_` (three copies), `rows_when`, a variant's `max_rows` (engine and gateway), `LIMITS` (`seed_block`, `risk_bins`, `max_sample_paths`), `card: false` for the risk node, `garch_name`; fixtures per variant with reading rows; `params_cases.json`; **gateway**: `task.limits.memory_bytes`, `tmp_bytes`, `started_unix`; `recipe.json`; `run.R` routes and stubs «not built yet»; **the image**: time, size, lock, the recommended packages present, `rugarch` loads as uid 10001 on a read-only root; **measurements**: load time, `ugarchfit` for the four models × three distributions at 1k/10k/100k, `nyblom`, `ugarchpath` per block, `rdist` per million | `catalog.py`, `catalog.json` and copies, the catalogue tests, `fixtures/models/{garch,risk_simulation}_*.json`, `params_cases.json`; `jobs_models.go` and its test (limits, variant rows); `groups/timeseries/{recipe.json,runner/run.R,runner/R/garch.R,runner/R/risk_simulation.R}` (stubs) | Timestral F9 committed | `--check` green; `output_columns` gives `quantile_0.01…` and `price_quantile_*` before a run; the image builds; the self-test passes ARIMA's and the decomposition's cases and exits 4 when one is made to fail; other groups' Dockerfiles byte for byte | the image's idle RSS after `library(rugarch)`; `ugarchfit` at 100k: `VmHWM` | **yes**: the build (alone) | sonnet; **opus** for the measurements |
| **G1** The GARCH node | `garch.R`: series and returns, the scale and its exact back-transform, `ugarchfit`, coefficients, measures, the checks (weighted portmanteau, ARCH-LM, Nyblom with the bridge-law p-value, sign bias), facts, forecasts, 1-step VaR, `ts_risk.R` (holdout, Kupiec, Christoffersen), the `volatility` output, the card; references and witnesses | `runner/R/garch.R`, `ts_risk.R`, `ts_nyblom.R`, their `*_tests.R`, `reference/` (their cases), `make_reference_garch.R`, `make_witness_garch.py`, `witness.json` (`arch` added) | G0 | the witnesses' table; ARIMA's and the decomposition's `expected.json` untouched; timings at 1k/10k/100k | the fit and checks at 100k in a child process: `VmHWM` below 1 GiB | no | **opus** |
| **G2** The simulation engine | `ts_sim.R`: seed blocks and L'Ecuyer streams, the batch from `memory_bytes`, `ρ` measured, the Parquet writer (one row group a batch), pass-1 accumulators (moments, histograms, counts), pass 2 (selection and sub-histograms), VaR, ES, intervals, drawdown, threshold probabilities, the disk and time projections, the chunked copy to the bucket; on a toy model (i.i.d. normal, i.i.d. t) | `runner/R/ts_sim.R`, `ts_sim_tests.R` | G0 | exact against the whole matrix; batch invariance at 64 MiB / 256 MiB / 2 GiB; the prefix property; refusals with their numbers; timings at 10k, 1M, 10M values | **peak below `memory_bytes` = 256 MiB at 10 M values**, more than one batch used | no (the 10 M timing run alone) | **opus** |
| **G3** The risk node | `risk_simulation.R`: the model fit by G1's functions or ARIMA's (`arima_fit_core` moved out of `fit_arima` without changing ARIMA's results), the end state, the GARCH and ARIMA recursions (parametric and bootstrap), the risk variable and its maps, the summary and paths outputs, the rows, the backtest | `risk_simulation.R`, `risk_simulation_tests.R`, `arima.R` (the move only), their cases | G1, G2 | `ugarchpath` 1e-12; `simulate.Arima` 1e-10; ARIMA's `expected.json` byte for byte; 10 M values under 30 s | the end-to-end node at 10 M values: cgroup-equivalent `VmHWM` below 256 MiB and 2 GiB budgets | no | **opus** |
| **G4** Reports and explanations | `layouts/garch.py`, `layouts/risk_simulation.py`, `READINGS`, `report_texts/{garch,risk_simulation}.json` (five languages: page words, `reading_texts`, `formula_texts`, `measure_readings`, `verdict_words`, advice), `NOTE_NAMES`; golden PDFs | the two layouts, `layouts/__init__.py`, `layouts/common.py` (`NOTE_NAMES` only), the two words files, `test_models_layouts.py`, `test_report_explanations.py` (registration only) | G0 (fixtures); G1, G3 for real outputs | every fixture in five languages passes `reports.definition.check`; no measure sentence holds a verdict word; other PDFs byte for byte | the report task on the largest fixture: `tracemalloc` peak recorded, under 200 MB | **yes**: engine suite | sonnet (layout), **opus** (texts) |
| **G5** Editor | the two palette entries under *Time series*, the forms (`model`-dependent keys, ARIMA's and GARCH's sections reused, the quantiles list), «Register» hidden for the risk node, the action «Simulate risk from this model» on ARIMA and GARCH nodes, five languages | `OpSidebar.vue`, `useOpIcons.ts`, `ModelForm.vue`, the node menu, `modelCatalog.ts` (wildcards if not in G0), locales (`models`), `frontend/tests/model-*.test.mjs` | G0 | Chrome and Firefox, mouse and keyboard alone (Mac, a laptop without F/Home/End keys); the copied options equal the source node's; columns after the node before Run | none: no data path (the lot says so) | no | sonnet |
| **G6** GARCH Predict | `predict_garch`, `PREDICT` entries, the continuity refusal, the card's checks in the gateway | `garch.R` (predict), `run.R` (route), `catalog.py` (`PREDICT`), `jobs_predict.go` and its test | G1 | Predict equals the fit's own σ and VaR on the fit's last rows (1e-10); Int64 time kept | Predict on 100k new rows: `VmHWM` | no | sonnet |
| **G7** Sample data, docs, assistants | `ref.quotazioni_materie_prime` and `ensure_quotazioni`, its generator and hash test; the three flows; `docs/capabilities/models.md` «Volatility (GARCH)», «Risk simulation» (the memory rule, the seed, the limits, the licences); `CHANGELOG.md`; flow assistant, `aiprompts`, MCP tools | `infrastructure/sampledb/{generate.py,schema_ref.sql,seed_tabularia.py}` and tests; docs; `ai_flow_agent.py`; `internal/aiprompts/`, `internal/mcp/` | G3–G6 | `small` only; the other tables' hash unchanged; the flows run | the generator's peak (tracemalloc), recorded | light | haiku (docs), sonnet (seed, prompts) |
| **G8** kind | rebuild on kind (the recipe hash queues it; **the old pod does not build it during the rolling upgrade**: the gateways' logs and the version's Dockerfile checked), self-test, the three flows, PDF and Excel in two languages, MLflow (GARCH registered and predicted; the risk node logged without a model), timings at 100k returns and 10 M values in the Job, the cgroup's `memory.peak` in `run.json`, the same seed with the group's memory at 1 GiB | — (this spec's «As built») | G7 | success criteria 1–7 | the Jobs' cgroup peaks | **yes**: gateway suite, kind | sonnet |
| **G9** Final review | this spec against the code, the formulas against the references, the readings against the verdict/measure rule in five languages, the memory rule against every path of the runner, security | — | G8 | corrections committed | re-run of G2's and G3's memory tests | the suites | **opus** |

**In parallel** after G0: G1 ∥ G2 ∥ G5 (disjoint files; G4 on the fixtures when the engine suite is free).
After G1 and G2: G3 ∥ G6. **In sequence**: G0 alone; G7 after G3–G6; G8 after everything; G9 last.

## Open questions for the owner

Every question was answered by the owner on 2026-11-22; the options stay for the record.

1. **The nodes.** **Decided 2026-11-22 (owner): (a), two nodes, with «Simulate risk from this model».** *Options were:* (a) **Two nodes**: GARCH, and a Risk simulation that fits its own ARIMA or GARCH with
   the same options and code, plus the editor's «Simulate risk from this model» that copies a model
   node's options; (b) the simulation as an output of the ARIMA and GARCH nodes (`output = paths`, the
   risk pages in their reports): fewer nodes, but two heavier forms and reports, and the GARCH node's
   «no simulation» rule broken; (c) the risk node reading a **registered** model from MLflow, as Predict
   does: true «from an estimated model», but it needs MLflow, and bootstrap draws need residuals a card
   cannot hold. **Recommendation: (a)**; (c) can be added later for parametric draws (the GARCH card
   already holds the state).
2. **«MCMC».** **Decided 2026-11-22 (owner): (a), Monte Carlo of paths with the parameters at their estimates; the parameters' uncertainty (c) is a future option.** *Options were:* (a) **Monte Carlo of paths** with the parameters at their estimates: what «simulations to
   compute the expected shortfall» means in risk practice, and what every measure here needs; (b) Bayesian
   MCMC (posterior draws of the parameters, e.g. Stan's NUTS), then paths per draw: the parameters'
   uncertainty included, but a sampler per run, minutes, and convergence diagnostics — the planned
   visual Bayesian editor is its place; (c) between them, a later option `parameter_uncertainty =
   asymptotic`: each seed block draws its parameters from N(θ̂, V̂) (rejecting non-stationary draws).
   **Recommendation: (a) now**, (c) as a later option if wanted, (b) in the Bayesian editor.
3. **`rugarch` and its 23 packages** (six GPL-2-only, two of them already in our images). **Decided 2026-11-22 (owner): (a), `rugarch`.** *Options were:* (a) **`rugarch`
   as accepted**: maintained since 2011, checks OK, fit, filter, Nyblom, sign bias, `VaRTest` and the
   path simulator to check ours; image +60–90 MB, build +5–8 minutes; (b) GARCH written in the runner (the
   likelihoods in R with Rcpp, optim, numerical scores): no new package and a cleaner licence list, but a
   large piece of numerical code to own, without `rugarch` inside the image to compare with. **Recommendation:
   (a)**, with the GPL-2-only list written in the docs; a legal reading before any distribution of a built
   image, as for Timestral.
4. **Default distribution.** **Decided 2026-11-22 (owner): the recommendation, (a).** *Options were:* (a) **Student t**: returns have fat tails, and ν with its interval tells the
   reader when the normal would do; (b) normal, `rugarch`'s default and the textbook start. **Recommendation:
   (a)**.
5. **Exact or approximate quantiles.** **Decided 2026-11-22 (owner): the recommendation, (a).** *Options were:* (a) **VaR, ES, probabilities, drawdown exact by two passes; the
   fan's bands within a declared error** (≤ bin width / 1,024 per step, written); (b) everything from a
   one-pass sketch (t-digest, KLL): one pass, but every number approximate with an error only bounded in
   probability; (c) everything exact, the bands by extra passes over the file: more reading time for
   numbers whose Monte Carlo error is far larger. **Recommendation: (a)**.
6. **When the time would run out.** **Decided 2026-11-22 (owner): the recommendation, (a).** *Options were:* (a) **Refuse with the numbers**, projected after the first batch and
   checked after each; (b) stop at the clock and report the paths done (a prefix: reproducible as paths,
   but the measures would depend on the machine's speed). **Recommendation: (a)**: the same seed must
   give the same numbers.
7. **What the risk node hands on.** **Decided 2026-11-22 (owner): the recommendation, (a).** *Options were:* (a) **`summary`** by default (one row per step), `paths` on request
   (10 million rows into the flow's storage), `results`; (b) `paths` by default. And MLflow: (a) **never
   the paths**; (b) the paths as an artifact up to a size. **Recommendation: summary, and no paths in
   MLflow.**
8. **The coverage test.** **Decided 2026-11-22 (owner): the recommendation, (a).** *Options were:* (a) **Kupiec's likelihood ratio** (χ²₁), the name risk offices know and
   `rugarch` reports, with the expected count said beside each verdict; (b) the exact binomial test of the
   same hypothesis (no χ² approximation with 2.5 expected violations), its p-value instead of Kupiec's.
   **Recommendation: (a)**, the reading saying how few violations were expected; (b) if the owner prefers
   exactness over the familiar name.
9. **The sign bias test.** **Decided 2026-11-22 (owner): the recommendation, (a).** *Options were:* (a) **Include** Engle–Ng's test (a p-value, cheap): it says when GJR or EGARCH is
   needed; (b) only the four checks asked. **Recommendation: (a)**.
10. **A test of the innovations' distribution.** **Decided 2026-11-22 (owner): the recommendation, (a).** *Options were:* (a) **None in the first release**: the backtest tests the
    tail that matters; (b) Berkowitz's (2001) likelihood ratio on the holdout's probability transforms; (c)
    `rugarch`'s adjusted Pearson test (its verdict depends on the number of groups chosen). **Recommendation:
    (a)**, (b) later.
11. **GARCH Predict.** **Decided 2026-11-22 (owner): the recommendation, (a).** *Options were:* (a) **Yes, lot G6**: filter new returns with the registered parameters, σ and VaR per
    row — the daily use of a volatility model; (b) no Predict, as the decomposition. **Recommendation: (a)**.
12. **Sample data.** **Decided 2026-11-22 (owner): the recommendation, (a).** *Options were:* (a) **A new reference table of three raw materials' daily prices**, GJR-GARCH t
    returns, its own generator, nothing else changed; (b) no new data, the risk node only on the orders
    (ARIMA): no volatility clusters to show. **Recommendation: (a)**.
13. **Rows for GARCH.** **Decided 2026-11-22 (owner): the recommendation, (a).** *Options were:* The group's 200,000 stays; if G0 measures more than 60 s for a GARCH(1,1) t with
    every check at 100,000 rows: (a) **a model limit for GARCH** like ARIMA's, set from the measurement;
    (b) the group's limit for both. **Recommendation: (a)** if needed, decided on G0's numbers.
14. **Nyblom's p-value.** **Decided 2026-11-22 (owner): the recommendation, (a).** *Options were:* (a) **Computed from the asymptotic law** (Imhof's inversion, checked against the
    exact quantiles and Hansen's table), so any alpha is answered; (b) `rugarch`'s three critical values:
    a verdict only for alpha 0.10, 0.05 or 0.01 and «not computed» otherwise. **Recommendation: (a)**.
15. **Levels.** **Decided 2026-11-22 (owner): the recommendation, (a).** *Options were:* (a) **VaR 0.99 and ES 0.975**, two options (the market-risk rules' pair); (b) one level for
    both. **Recommendation: (a)**.
16. **Holdout and the simulation's start.** **Decided 2026-11-22 (owner): the recommendation, (a).** *Options were:* (a) **Parameters from the training part, state filtered through
    the holdout to the last day**: the model simulated is the model backtested, one fit; (b) a second fit
    on all the data for the simulation: more data, two fits, and the backtested model is not the simulated
    one. **Recommendation: (a)**.
17. **An ES backtest.** **Decided 2026-11-22 (owner): the recommendation, (a).** *Options were:* (a) **Later** (McNeil–Frey's or Acerbi–Székely's tests need simulations per held-out
    day); (b) now, in the risk node only. **Recommendation: (a)**.

## As built

*(Filled by the lots: deviations, timings at 1k/10k/100k returns and at 10k/1M/10M simulated values, peak
memory of every memory test, the image's size and build time, the self-test's count.)*

### G0 — contract, recipe, image (2026-11-22)

**Catalogue.** `garch` and `risk_simulation` are in the group `timeseries` after the decomposition; GARCH has a card, the risk node
`card: false`; one variant for `garch` (`default`), two for the risk node keyed on `model` (`garch`, `arima`), the latter with
`max_rows` 20,000 (a variant's field, read by the engine's snapshot and by the gateway: `modelcatalog.MaxRowsOf`, `modelMaxRowsOf` in
`jobs_models.go`; the smallest of the group's, the model's and the variant's). New kinds, sections `backtest` and `risk`, outputs
`volatility`, `risk_summary`, `paths`, limits `seed_block` 1000, `risk_bins` 1024, `max_sample_paths` 20, `garch_name`, the three
copies of the snapshot, 69 shared parameter cases (engine and Go answer word for word), three fixtures with their reading rows
(`garch-default`, `risk_simulation-garch`, `risk_simulation-arima`). The gateway's task carries `limits.memory_bytes` (the group's
limit, absent when unreadable), `tmp_bytes` (the Job's `/tmp`, `modelTmpLimit` in bytes: 2432 MiB for 2 GiB) and `started_unix`
(the second the task is made, just before the Job is created), by `setRunLimits`. The runner's routes `garch` (fit, predict) and
`risk_simulation` (fit) answer exit 3 `not_built`, checked in the image.

**Deviations and corrections to this spec** (the rest unchanged):

1. **`output` of the risk node is `risk_summary`, not `summary`**: a node's `output` is the key of `OUTPUT_TYPES` (as `components`, `quantile_forecast`), which the spec names `risk_summary`; the choices are `risk_summary`, `paths`, `results` (default `risk_summary`). The report and the words may still say «summary».
2. **`quantile_<q>` is the same prefix as Timestral's but not the same rule**: Timestral leaves the 0.5 out (it is `forecast`); the risk summary has no other median column, so it keeps it. The names stay `quantile_<q>` and `price_quantile_<q>`; the rule is told apart by the node having an `es_level` (engine `_ROWS_WILDCARDS`, editor `wildcardLabels`, assistant `_ROWS_WILDCARDS`). The fallback name `sim_quantile_<q>` was not needed.
3. **`position` and `drawdown` apply always**: `applies` is a conjunction and cannot say «`model = garch`, or `aggregate = relative_change`». The runner (G3) refuses a `position` with an ARIMA whose risk variable is not a relative change, and does not compute the drawdown there.
4. **GARCH(p,q) naming**: `garch_name` writes Bollerslev's order, p lags of the variance (`garch_order`) then q of the shocks (`arch_order`): `GARCH(2,3)` is `garch_order` 2, `arch_order` 3.
5. **Predict's contract is G0's, not G6's**: the catalogue test asks every model with a card for its Predict columns, so `PREDICT.columns.garch` = `prediction`, `prediction_var` and `applies_to.horizon` gains `garch` are here; the runner's `predict_garch` refuses `not_built` until G6. The flow assistant's line of options now reads «horizon for model_arima, model_garch, …».
6. The gateway refuses `registerAs` on the risk node with «a risk simulation registers no model: register the GARCH or ARIMA node instead» (`jobs_models_mlflow.go`).
7. `threshold` and `position` are bounded by 999,999,999,999,999 (1e15 less one), the last whole number the sentences write without an exponent in Go and Python.
8. The editor's palette leaves both nodes out (`MODEL_NOT_IN_THE_PALETTE_YET`), the report tests skip them (`PENDING`), their columns are not in `output_columns.json` (`NOT_RUN_YET`): G5, G4 and G1/G3 take these away.

**Image** `tabularia-models-timeseries:dev` from the new recipe (`pinned_on` 2026-11-22, the 23 packages of the table in its order,
`system_libraries: ["libgsl-dev", "libgmp-dev"]`), two compilers, this machine, the apt layer rebuilt:

| | Before (D0) | After |
|---|---|---|
| Build time | 592 s (9 min 52 s), packages only | **943 s (15 min 43 s)**, the whole package step (arrow included) and the apt layer |
| Size | 1,485,040,325 bytes | **1,531,342,086 bytes (1.53 GB)**: +46.3 MB (estimated +60–90 MB; the R library is 286 MB, `rugarch` 8.1 MB, `Rsolnp` 7.3 MB, `mclust` 5.8 MB) |
| Lock | 1 library | `libgsl-dev@2.7.1+dfsg-6ubuntu2`, `libgmp-dev@2:6.3.0+dfsg-2ubuntu6.1`; the 23 packages at the pinned versions (all equal to CRAN's of the day); R 4.6.0 |

The other six groups' Dockerfiles are identical byte for byte before and after. The recommended packages `KernSmooth` 2.23-26, `Matrix`
1.7-5, `mgcv` 1.9-4, `MASS` 7.3-65, `codetools` 0.2-20 are present. `rugarch` 1.5.6 and `gmp` load, and a GARCH fits, as uid 10001 on a
read-only root with `/tmp` as tmpfs, `--network none` (`gmp` factorial of 20 exact). Licences as the DESCRIPTION files say: `rugarch` GPL-3;
`Rsolnp`, `chron`, `kernlab`, `multicool`, `mvtnorm`, `numDeriv` GPL-2 (the six of the spec).
**Self-test** (code of `cmd/modelgroup code timeseries`, read-only root, `--network none`, 1 CPU): **75 ok, 0 failed, 0 skipped, exit 0**
(27 s); with one expected number of `arima_ar_index` changed by 50 %: 74 ok, 1 failed, **exit 4**.

**Measurements** (one CPU, `--network none`, `--memory 2500m`, uid 10001; times are `system.time` elapsed; data from `ugarchpath` of a
GJR-GARCH(1,1) skewed t, divided by its standard deviation as the runner will do; every fit `solver = "hybrid"`, convergence 0 in all 36):

Load time (a process each, wall clock): bare `Rscript` 0.40–0.48 s; `arrow` + `forecast` 2.0–2.1 s; plus `rugarch` **3.3–3.6 s**
(`rugarch` alone 1.7–1.8 s; in-process 1.33 s after `arrow` and `forecast`); resident size 55 MB bare, 177 MB after `arrow`+`forecast`,
**286 MB after `rugarch`**. The spec assumed 1–3 s on top of 1.6 s: it is 1.3–1.5 s in the same process.

`ugarchfit` (s) by variance model and distribution (`nyblom()` on each fit took under 0.01 s at 1k and 10k, 0.03–0.05 s at 100k: it reads
the scores `ugarchfit` already holds, which are in the fit's time):

| Model | Dist. | 1k | 10k | 100k |
|---|---|---|---|---|
| ARCH(1) | norm / t / skew t | 0.17 / 0.24 / 0.39 | 0.50 / 1.81 / 2.81 | 6.05 / 13.2 / 29.3 |
| GARCH(1,1) | norm / t / skew t | 0.11 / 0.18 / 0.34 | 0.45 / 1.26 / 2.64 | 4.12 / 11.8 / 24.9 |
| GJR(1,1) | norm / t / skew t | 0.33 / 0.61 / 0.88 | 0.92 / 2.15 / 3.76 | 6.78 / 15.0 / 31.6 |
| EGARCH(1,1) | norm / t / skew t | 0.20 / 0.39 / 0.73 | 0.78 / 1.84 / 3.56 | 6.63 / 15.5 / 30.8 |

The process peak (`VmHWM`) after all twelve fits: 274 MB at 1k, 280 MB at 10k, **413 MB at 100k** (G1's bound: below 1 GiB). The longest
fit, GJR with skewed t at 100,000 returns, is 31.6 s: with the cheap checks the GARCH node stays under G1's 60 s, so **Open question 13 needs
no model limit of rows** (the group's 200,000 stays; at 200,000 the longest fit would be about 65 s by linearity: G1 measures it).

`ugarchpath` per seed block of 1,000 paths, Student t: h = 10: 0.01–0.02 s (the first call 0.6 s); h = 100: 0.06–0.08 s (0.6–0.8 µs a value);
**h = 1000: 14–17 s (14–17 µs a value)**, two orders of magnitude slower per value, so `ugarchpath` is a reference for small cases of the
self-test only, never for the simulation (which is the runner's own elementwise recursion, as the spec has it). `rdist` per million draws: `rnorm`
0.077 s, `rdist("norm")` 0.069 s, `rdist("std")` **0.20 s**, `rdist("sstd")` **0.47 s** (the spec guessed 0.1–0.3 s for 1 M; 10 M skewed t would be
about 4.7 s of draws). The process peak of the simulation script: 550 MB.

**Tests**: engine 1,295 passed (catalogue, results, columns, layouts, explanations), `go test ./internal/...` green, the editor's 112 model tests, the
assistant's 139. `model_catalog.py --check` green.

### G1 — the GARCH node (2026-11-22)

**What is in.** `runner/R/garch.R` (the series and the returns, the scale and its back-transform, the fit, the coefficients, the measures, the checks,
the facts, the forecasts, the 1-step VaR, the holdout and its backtest, the charts, the `volatility` output, the card, the native object without its
data), `ts_nyblom.R` (the statistics from the scores, the bridge law's p-value, Holm), `ts_risk.R` (`ts_backtest`, `ts_backtest_rows`: Kupiec,
Christoffersen, the counts, for the risk node too), `garch_tests.R` (seven self-tests), `card.schema.json` (`garch` and its block), the reference
cases (`make_reference_garch.R`, `data/garch_returns.csv`, `data/garch_prices.csv`, eleven cases `garch_*`, four of them refusals), the witness
(`make_witness_garch.py`, `arch` 9.0.0 added to `witness.json`), two cases of `output_columns.json`. The stub «not built yet» of the fit is gone;
Predict still refuses until G6. ARIMA's and the decomposition's cases of `expected.json` are byte for byte what they were: the GARCH cases are merged
before the first of them, and no line after them changes.

**Deviations and corrections to this spec** (the rest as written):

1. **The runner writes no reading.** The explanation is `layouts/garch.py`'s (`common.READINGS`, lot G4), as ARIMA's: the GARCH cases of
   `expected.json` hold no reading ids. Notes are `@garch_scale scale=…` (always), `@garch_solver solver=…` (when it is not solnp),
   `@garch_at_bound parameter=…` (one per parameter at a bound): their words are G4's.
2. **κ = E[z² 1(z < 0)], as this spec defines it; rugarch uses P(z < 0).** rugarch 1.5-6's `persistence()`, `uncvariance()`, `newsimpact()` and its
   GJR forecasts weigh γ by `pneg`, the probability of a negative z: the same ½ for a symmetric law, not for the skewed t (ξ = 0.85, ν = 7: P(z < 0) =
   0.470, E[z² 1(z < 0)] = 0.555). The runner computes κ by quadrature of the fitted density, and from it the persistence, the half-life, the long-run σ,
   the news impact (every other lag at its long-run level) and the forecasts (`garch_forecast`, its own recursion from the filter's last state). The
   self-test checks the forecasts equal `ugarchforecast` to 1e-10 for the normal, the t and EGARCH, the first step for GJR with the skewed t, and that
   rugarch's persistence is the one with P(z < 0). EGARCH's long-run σ is √exp(ω / (1 − Σβ)) (rugarch's `.uncegarch1`).
3. **Robust standard errors: the sandwich H⁻¹(GᵀG)H⁻¹ from rugarch's Hessian and its per-observation scores** (`fit@fit$hessian`,
   `fit@fit$scores`, the scores Nyblom's test reads). rugarch's own `robust.cvar` takes its meat from other scores (`robustvcv`), which differ from
   these by 6–30 % on the reference cases while the witness's scores agree with these to 2e-8.
4. **The weighted portmanteau** is rugarch's transcription of Fisher & Gallagher (`Weighted.Box.test`: shape (3/4)(m + 1)² m / (2m² + 3m + 1 − 6mK),
   scale (2/3)(2m² + 3m + 1 − 6mK) / (m(m + 1))); it needs m ≥ 3K − 1, so `auto` is max(min(10, ⌊n/5⌋) + K, 3K − 1) (the same for K ≤ 5) and a lag
   given below it is «not computed». K is `ar_order + ma_order` on z and `arch_order + garch_order` on z², as this spec says (rugarch's own test of z²
   passes K = 0). No `df` row: the law is a gamma.
5. **Nyblom's p-value by Talbot's inversion of the exact Laplace transform** E e^{−sW_k} = (√(2s) / sinh √(2s))^{k/2} (24 nodes, no eigenvalue
   truncated), not Imhof's: the witness takes Imhof's road (2,000 eigenvalues, the tail's mean added) and agrees to 6e-7; Anderson & Darling's
   quantiles are given back to their five digits. Hansen's table (rugarch's `.nyblomCritical`, simulated on finite samples, two or three digits) is met
   within 3.5 % of the level at 10 %, 5 % at 5 %, 10 % at 1 % (the self-test allows 12 %). EGARCH's individual tests read the scores of the parameters
   reported (g J⁻¹: ω's back-transform involves β); the joint statistic is rugarch's to 1e-10 for every model.
6. **The break of the reference data is in the mean, not in ω.** A tripled ω halfway is absorbed by the persistence (the fit goes to the 0.999
   constraint and Nyblom does not reject, p 0.18–0.44 over eight designs): `brk`'s mean falls from +0.002 to −0.002, and Nyblom rejects (p 1e−9) and
   names μ.
7. **Sign bias** regresses on z_{t−1} (Engle & Ng's standardized regressors); `rugarch::signbias` uses ε_{t−1}, and with ε the runner's statistic is its
   «Joint Effect» to 1e-8 (self-test). Only the joint Wald row is written (the verdict's): the three t-tests have no row.
8. **The backtest.** Kupiec's and Christoffersen's independence statistics equal `rugarch::VaRTest`'s to 1e-10; its conditional coverage is LR_uc on
   the T days + LR_ind, this spec's is LR_uc on the T − 1 transitions + LR_ind (the self-test checks both relations).
9. **`too_short`** is below 100 returns for the fit: rugarch 1.5-6 only warns there; or a training part shorter than the holdout.
10. **`at_bound`**: an estimate within 1e-6 of the bounds' range from rugarch's LB or UB, or rugarch's persistence within 1e-6 of its 0.999
    constraint; the row `at_bound` holds the count, one row per parameter (`level_2`) its estimate.
11. **The reference data are smaller than planned** (the self-test's ConfigMap): `garch_returns.csv` 800 instants of three series, `garch_prices.csv`
    301 prices; the time types reuse `dec_types.csv`, the edge cases are built in the self-test, the refusals reuse `dec_no_season.csv`, `dec_gaps.csv`,
    `duplicate_monthly.csv`.
12. **A witness's finding**: on the 300 simple returns of `garch_prices_simple` rugarch's solvers stop at the persistence constraint (α₁ = 0, P =
    0.999) with a log-likelihood 0.0138 below an interior maximum (α₁ = 0, β₁ = 0.967) that SciPy finds; the node says the bound with `at_bound`, the
    case keeps rugarch's numbers and the witness prints it as a known gap. EGARCH's standard errors are not compared by the witness: rugarch's
    Hessian starts Richardson's steps at 10 % of each parameter, which moves β₁ = 0.97 past 1, where its recursion and a transcription part.

**Witnesses** (`make_witness_garch.py`, outside R; worst difference over the seven fitted cases, against the spec's tolerance):

| Check | Worst | Tolerance |
|---|---|---|
| log-likelihood at rugarch's estimates, transcription / Python `arch` (3 cases, backcast set to rugarch's first variance) | 2.2e-15 / 1.6e-15 | 1e-8 |
| estimates, SciPy from 10 % away (relative to the estimate, or to ten standard errors on a flat ridge) | 4.1e-5 | 1e-4 |
| SciPy's maximum above rugarch's | 3.5e-9 | 1e-8 |
| standard errors, the sandwich of numDeriv's Richardson scores and Hessian transcribed (cases not at a bound, not EGARCH) | 5.9e-8 | 1e-3 |
| persistence, κ by quadrature, half-life, long-run σ, σ_T, σ_{T+1} | 3.3e-13 | 1e-6 |
| weighted Ljung–Box on z and z², ARCH-LM, sign bias (statistics; p-values absolute) | 4.7e-14 | 1e-6 |
| Nyblom's statistic from the transcription's scores / its p-value by Imhof | 1.8e-8 / 6.2e-7 | 1e-5 |
| Kupiec, Christoffersen, the violations | 4.9e-17 | 1e-10 |

**Timings and memory** (one CPU, `--memory 2500m`, the image of G0 with the files mounted; the node in a child `Rscript`: the series, the fit, the
filter, the forecasts, every check, the charts, the output; `VmHWM` of the child): AR(1)-GJR-GARCH(1,1) with skewed t errors and a holdout of 250, the
heaviest combination — **1,000 returns 2.6 s, 364 MiB; 10,000 returns 7.8 s, 357 MiB; 100,000 returns 59.0 s, 466 MiB**. GARCH(1,1) t with the same
holdout at 100,000 returns: the fit 13.6 s, the node 19 s. The fit is most of it (48.4 s of 52 s for the GJR at 100,000); the checks take 0.7–1.2 s,
the filter 0.1 s, the charts 0.2 s. **The acceptance bound holds** (GARCH(1,1) t under 60 s at 100,000) and the peak stays under 1 GiB: no model limit
of rows (Open question 13); the group's 200,000 would take the GJR with the skewed t about two minutes. The self-test runs the memory test at 10,000
returns (`TABULARIA_GARCH_MEMORY_N` sets another size: the 100,000 above).

**Self-test** (the group's code with the files of this lot mounted on the image of G0, not rebuilt; read-only root, `/tmp` a tmpfs, `--network none`,
uid 10001, 1 CPU): **93 ok, 0 failed, 0 skipped, exit 0** — the 75 of G0, the seven tests of `garch_tests.R`, the eleven `garch_*` cases. The
self-test's ConfigMap grows by about 110 KB (the R files 56 KB, the cases 22 KB, the data 31 KB): 864 KB of the 900 KiB (921,600 bytes) the gateway's test allows,
before the files of G2 and G3 (G2's `ts_sim.R` and its tests, about 61 KB as they stand, would take it past the limit).

### G5 — editor: two nodes, «Simulate risk from this model» (2026-11-22)

**What is in.** Both nodes have their form, words (five languages, namespace `models`), icons (`Activity` for the volatility, `Dices` for the risk
simulation: lucide, no new dependency), one-line summaries and the node menu's action. The forms are the catalogue's, drawn by the same controls as the
other models: the keys change with `model` (GARCH's sections or ARIMA's, by `applies`), with the variance model (`garch_order` leaves an ARCH) and with the
input (`returns_type` with prices); the risk node reuses the two nodes' sections because it takes the same keys with the same kinds and defaults (a test
compares them). The quantiles are the existing list of numbers (up to nine, in (0, 1), kept sorted and once each, said back under the box). The risk node
has `card: false`: «Register the model as» is not drawn and the form says the node writes no model. The columns after each node (the summary's
`quantile_<q>`, the prices' and the threshold's, the paths' `sigma` with a GARCH only) are known before any Run, from the form as it is, and the steps after
the node keep them (select, rename, drop).

**The action.** The node's menu is the right-click ring (`FlowQuickMenu.vue`; its keyboard way is Shift+F10 on a PC and `m` on a Mac, then the arrows and
Enter). It takes an `actions` prop: what the node it was opened on can do, drawn after the steps on the ring and emitted as `action`, not `pick`.
`modelActionsOf` gives `simulate_risk` to an ARIMA or GARCH model node and nothing else. `onQuickAction` in the editor reads `modelRiskFrom(node.data)`
and drops a Risk simulation node **on the same input** as the source node (its upstream, one more branch there: the source's own output is its forecast
table, not the series), selected and focused. `modelRiskFrom` copies every key the two nodes have in common with the value the source has **in effect**
(a default the source relies on is written as that value: an ARIMA's horizon of 12 is carried, not the simulation's 10), only the keys that apply to the
source, plus the time limit and the retries; `output` is the risk node's own, MLflow and the label are not carried. The test compares `modelFull` of both
for every shared key, for a GARCH, for ARIMAs by hand, searched for and bare.

**Hidden.** `MODEL_NOT_IN_THE_PALETTE_YET` still holds `garch` and `risk_simulation` (their runners come with G1 and G3; G7 takes them away). With the
set emptied the palette shows one entry for each under *Time series*, after the decomposition (tested).

**Deviations and corrections.**

1. **An ARIMA with exogenous columns is refused** by the action with a sentence (`models.actions.simulateRiskRefused.exog`): the risk form has no `exog`, so
   the copy would fit another model. A node with no input is refused too (`simulateRiskNoInput`).
2. **The titles of the nodes are one function**, `modelCodeTitle`: an ARIMA's orders, a GARCH's `garch_name` (`GJR-GARCH(1,1), skewed t`: codes and the law's
   English name, as the report's title); the canvas, the panel and the table's header read it. The risk node is named by its words, its summary says the
   model fitted and `N=<paths>`.
3. **Own hints for `mean`, `distribution`, `lb_lag`, `holdout`, `horizon`, `seed`, `output`** (and the risk node's `quantiles` and `threshold`, with their own
   labels), because the words the other models share say another thing («the median is always given», «exogenous columns»).
4. **The wildcards and the copies were G0's**: nothing of the catalogue, the engine, the gateway or the assistant changed.

**Tests.** The editor's model tests 109 → 126 (`frontend/tests/model-volatility.test.mjs`, 17 new: keys, defaults, quantiles, forms in words, keyboard,
the action's copies, titles, palette, columns before the Run, words in five languages); the exclusions of G0 (`WORDED`, the experiments' words) are gone.
The whole `npm test` (614 tests) and the typecheck (`vue-tsc`, no error) are green.

**On screen** (a scratch Vite server outside the repo mounting the real `ModelForm.vue` and `FlowQuickMenu.vue` with the real catalogue and made-up data,
the node menu's wiring mimicked: the editor itself needs the whole app; Playwright, headless): Chromium and Firefox, 13 checks each, no console error.
Mouse: right click on an ARIMA and on a GARCH node, the ring holds the four steps, the action and the hub, the new node has the copied options; an ARIMA
with exogenous columns is refused with its sentence and a linear node has no action; the form follows the model, the variance model and the prices; the
quantiles box reads «0.95, 0.05, 0.5» as 0.05, 0.5, 0.95 and refuses 1.2; the columns of both nodes show before any Run; Italian, German, Spanish and French draw both
forms with no key shown. Keys alone, twice (Shift+F10 as on a PC; `m` as on a Mac and on a laptop without F, Home, End or Menu keys, with a macOS user
agent): Tab to the node, the ring opens on the first step, ArrowLeft reaches the action, Enter makes the node and focuses it, Escape closes the ring and
returns the focus, Tab moves between the ring and its hub, the form's choices by the arrows and the quantiles typed. A real keyboard of a Mac was not
available: the key events are the same ones the editor reads (`m`, `Shift+F10`, arrows, Enter, Escape).

### G4 — reports and explanations (2026-11-22)

**What was built.** `layouts/garch.py` and `layouts/risk_simulation.py`, registered in `layouts/__init__.py`; their words in
`report_texts/garch.json` and `report_texts/risk_simulation.json` in five languages (page words, `reading_texts`, `formula_texts`,
`reading_labels`, `measure_readings`, `note_texts` for the runner's notes, the checks' names, H0 and advice, the backtest's words; the risk
node's file also carries the ARIMA checks' words, since a model's words are one file for all its variants); `common.NOTE_NAMES` gains
`model` and `parameter`. `PENDING` is gone from `test_models_layouts.py`; `tests/test_models_garch_report.py` is new. The pages are those
of «The report (pixel perfect)», with the deviations below. `garch-default.json` is now the GARCH runner's own output (`garch.R` on
`garch_prices.csv`, GJR-GARCH(1,1) with skewed t errors, the last 50 returns kept out, written with `--from-results`); the risk node's
two fixtures stay lot G0's placeholders, without reading rows.

**The explanation is the layouts'** (the rule of lot B0 for the models, as ARIMA's): `common.READINGS["garch"]` and
`common.READINGS["risk_simulation"]` choose the sentences from the numeric rows the runners write. **The runners write no reading and no
sentence.** A verdict is chosen by the row's p-value against the node's `alpha` (a test without a p-value has no line); every other number
is a measure, said as the row holds it (`measure_readings`). The page computes nothing: it selects (a comparison with α, which of two
numbers is the larger, the largest of a set) and says the rows' numbers. The runner's notes `@garch_scale scale=…`,
`@garch_solver solver=…`, `@garch_at_bound parameter=…` are sentences of `note_texts`.

**The rows the GARCH layout reads** (what `garch.R` writes since lot G1; checked on three runs of the runner in the image, below):

| Reading (block: ids) | Rows |
|---|---|
| `summary`: `summary` / `summary_steps` / `summary_holdout` / `summary_holdout_steps`, `summary_prices`, `summary_scale` | `model|||n`, `first`, `last`, `holdout`, `scale`, the text `time_kind` (`integer` → `_steps`); the node's `variance_model`, orders, `distribution`, `input_kind`, `value` |
| `persistence`: `persistence` / `persistence_integrated`, `sigma_now_above` / `_below` / `_equal` | `model|||persistence` (≥ 1: integrated), `half_life`, `uncond_sigma`, `sigma_next`; the node's `frequency` as the unit |
| `asymmetry`: `asymmetry_gjr` (α₁, γ₁), `asymmetry_gjr_falls_only` (γ₁: α₁ at its bound), `asymmetry_egarch` (α₁, γ₁), `asymmetry_none`; `sign_bias_supported` / `_not_supported` | `coef|alpha1||estimate`, `coef|gamma1||estimate`, the row `assumption|at_bound|alpha1|value`; `assumption|sign_bias||p_value` |
| `distribution`: `distribution_norm` / `_std` / `_sstd` | `coef|shape||estimate`, `ci_low`, `ci_high`; `coef|skew||…`; the level 1 − α |
| `residuals`: `lb_z_ok` / `_failed`, `lb_z2_ok` / `_failed`, `arch_lm_ok` / `_failed`, `z2_largest_acf` | `assumption|ljung_box_z||p_value`, `lag`; the same for `ljung_box_z2`, `arch_lm`; after a test on z² that rejects, the `lag|z2_acf` row of the largest \|acf\| and its `x0` |
| `stability`: `nyblom_stable`, `nyblom_moves` (the parameter with the smallest Holm-adjusted p, when below α), `nyblom_moves_none` | `assumption|nyblom||p_value`, `assumption|nyblom|<parameter>|p_adjusted` |
| `backtest`: `backtest_coverage_*`, `backtest_independence_*` (`_not_computed` with no break), `backtest_cc_*`, `backtest_expected`, `backtest_short` | `backtest|kupiec||p_value`, `christoffersen_ind`, `christoffersen_cc`; `backtest|counts||violations`, `expected_violations`; `model|||holdout`, the node's `var_level` |
| `method`, `formulas` | the node's options and the holdout |

The pages read besides: the `curve` rows `returns`, `sigma` (`sigma`, `forecast`), `news_impact`, `backtest` (`return`, `var`); the
`point` rows `std_residual`, `qq`, and `violation` (1 on a break, its index the held-out return's: the page draws that return in red);
`lag` `z_acf`, `z2_acf` with their `bound`; `hist|std_residual`; `assumption|at_bound` (the count; the rows per parameter are left to
the notes) and `variance_stationary`; Nyblom's joint `count` (said «7 parameters»); the coefficients.

**The rows the risk layout reads: what G3 writes** (the names of the catalogue's sections, no sentence, no reading):

| Section | Rows |
|---|---|
| `model` numbers | `n`, `n_missing`, `holdout`, `horizon`, `paths`, `seed`, `threshold` (when given), `position` (when given), `simulated_values`, `seed_block`, `batches`, `batch_paths`, `bins`, `var_level`, `es_level`, `last_value`, `paths_bytes`, `seconds_simulation`, `seconds_pass_2`, and the fitted model's own numbers (the GARCH node's or ARIMA's) |
| `model` texts | `model_name` (an ARIMA's by its orders as chosen, «SARIMA(1,0,1)(0,1,1)[7]»; the page names a GARCH from the options), `formula` (shown for an ARIMA), `time_kind`, `risk_variable` as an id (`cumulative_return`, `price`, `value`, `running_total`, `relative_change`), `tail`, `innovations`, `aggregate`, the notes (the GARCH's `@garch_*`) |
| `risk` at the horizon (`level_2` null) | `var`: `value`, `low`, `high`; `es`: `value`, `low`, `high` (`se`, `count` shown in the fit's numbers); `prob_crossed`: `value`, `low`, `high`; `prob_beyond`: `value`, `low`, `high`; `drawdown`: `value` (the drawdown exceeded with probability 1 − `var_level`, a positive share), `median`, `low`, `high`; `moments`: `mean`, `sd`, `skewness`, `kurtosis` (excess) — in the risk variable's units (a relative change as a fraction) |
| `assumption` | the fitted model's checks as its node writes them: a GARCH's `ljung_box_z`, `ljung_box_z2` (with `lag`), `arch_lm` (with `lag`), `nyblom` (joint and per parameter with `p_adjusted`), `sign_bias`, `at_bound`, `variance_stationary`; an ARIMA's `ljung_box_10`, `ljung_box_2s`, `arch_lm`, `variance_ratio`, `jarque_bera` (each with its `p_value`), `adf`, `kpss`, `roots` |
| `coef` | the fitted model's coefficients with `ci_low`, `ci_high` |
| `backtest` | as the GARCH node (`ts_backtest_rows`), with a holdout |
| `curve|fan` | `level_2` `quantile_<q>` (the catalogue's labels of `quantiles`), `history` (only when on the fan's scale), `path_<i>` (the sample paths), `band_error`; in the fan's unit |
| `curve|term` | `var`, `var_low`, `var_high`, `es`, `es_low`, `es_high`, `prob_crossed`, `prob_beyond`, one row per step |
| `curve|backtest` | `return` (the held-out observation), `var`, `violation` (the observation, only on the days it broke the VaR: the risk node has no `point` section) |
| `hist` | `horizon` and `drawdown`: `x0`, `x1`, `count` |

From them the risk layout says: `summary_garch` / `summary_arima`, `summary_bootstrap`, `summary_variable`, `summary_holdout`;
`var_lower` / `var_upper`, with `_level` when the risk variable is not a relative change; `es_lower` / `es_upper` (`_level`);
`threshold_lower` / `threshold_upper` (`_level` unless a GARCH of returns); `drawdown`; `shape`; `fan` (the shares of the bands from
the node's `quantiles`, the largest `band_error`, the number of sample paths), `fan_bands` without sample paths; the model's checks
(`model` block: the GARCH's ids above, or `arima_checks_ok` / `arima_check_failed` naming the check); the backtest's ids; the
paragraphs and formulas of what ran.

**Numbers the runners do not write** (each a sentence or a mark the page leaves out, never computes):

1. **The fitted density in the histogram's units** (n × bin width × f(z)): `garch.R` writes it as a density, which an `xy` chart cannot
   put on its own axis beside counts; the histogram of z is drawn alone. A row `curve|density|count` would bring the curve back.
2. **The money of the VaR and the ES** (position × the value): the catalogue's `risk` section has no stat for it, so the sentences
   `var_money` and `es_money` were removed and the cards say the Monte Carlo interval instead. A stat (e.g. `risk|var||money`, a catalogue
   change) would bring them back.
3. **The VaR at `es_level`** (the quantile the ES averages beyond): not a row; the ES sentence names its probability only.
4. **The EGARCH's news impact ratio**: the sentence says α₁ and γ₁ and how they act, no ratio.

**Three runs of the real runner** (the image `tabularia-models-timeseries:dev` with the files mounted, one CPU, `--memory 2500m`, the task
made by `modelgroup task`): GJR-GARCH(1,1) skewed t on `garch_prices.csv` with a holdout of 50 (the fixture: α₁ at its bound, every test
keeps its hypothesis, 2 breaks against 0.5 expected), GARCH(1,1) normal on `garch_returns.csv`'s `brk` (Nyblom rejects, p < 0.0001, μ
named, Holm-adjusted p < 0.0001; the Ljung–Box on z rejects), AR(1)-EGARCH(1,1) t with a holdout of 100: every reading a sentence in the
five languages, no `@`, `{` or `%2` left.

**Deviations and corrections to this spec** (the rest unchanged):

1. **Readings chosen by the layouts**, as the spec said; the first version of this lot had the runners write them, and is replaced.
2. **`NOTE_NAMES`**: `model` and `parameter` only. `distribution` was there; `paths`, `steps`, `level`, `threshold` are numbers (and `level`
   is ARIMA's number already). `position` and `size` were already names (a season's position, Timestral's size): no line sends them.
3. **Template ids**: the spec's list where it gave one (`summary_holdout`, `persistence`, `persistence_integrated`, `sigma_now_*`,
   `asymmetry_*`, `sign_bias_*`, `distribution_*`, `nyblom_stable|moves`, `backtest_expected`), `_ok` / `_failed` for the portmanteau,
   ARCH LM and backtest tests, and new ones (`summary_steps`, `summary_holdout_steps`, `summary_prices`, `summary_scale`,
   `asymmetry_gjr_falls_only`, `z2_largest_acf`, `nyblom_moves_none`, `backtest_cc_*`, `backtest_short`, `fan_bands`, the risk node's).
4. **Every backtest verdict says the breaks expected** beside it, as the owner asked; the coverage sentence also says how many there were.
5. **The asymmetry is said by the model's own coefficients** (α₁, γ₁ and what each does), not by a ratio the page would compute.
6. **The formula line is the page's**, built from the node's options with the distribution in the page's words; the runner's `formula`
   stays the English of MLflow. The PDF's font has no subscript minus, so lags are written `_(t−1)`.
7. **The volatility forecast is a line of its own colour**, not dashed (an `xy` chart has no dash); the long-run σ is the dashed
   horizontal line. The fan's sample paths are grey lines drawn by `split_by`, the fan's legend off.
8. **Cards**: the simulation card says «10,000 × 10» under «Paths × steps» (the long form did not fit in French and Spanish); the model
   card says the law only, the holdout is in the summary's reading. The coefficients are two tables, estimates and intervals, as ARIMA's:
   the runner's statistics («192.014141») did not fit one table.

**Checks.**

- Every fixture in five languages passes `reports.definition.check`, and so do ten variants of the options (EGARCH normal without
  holdout, ARCH(2) with ARMA(1,1) and no mean, GARCH(2,3) t; the risk node with returns, no threshold, an upper tail, 10 million paths of
  1,000 steps, an ARIMA relative change with a position, automatic orders).
- No sentence of a measure holds a verdict word (common.json's `verdict_words` and a list of judgement words of these reports, five
  languages; VaR, ES, persistence, half-life, the probabilities and the drawdown required among the measures); each verdict holds one; no
  «= {x:p}». The explanations' general tests (`test_report_explanations.py`) now run on both nodes, «only additions» included.
- **A verdict follows α** (criterion 8): on the runner's output, each of the eight tests read with α just above and just below its p-value
  gives the rejecting and the keeping sentence; so does the ARIMA risk fixture's Ljung–Box.
- The other reports: 505 renderings identical byte for byte before lot G4 and after this version (sha256 of the PDF, of the definition
  and of the labelled rows, and the page count: the 33 fixtures of the other models and the 68 variants of the statistics tests, in five
  languages, the tests' fake chart service).
- Memory: the report on `garch-default` (the runner's output, its readings chosen), in German: `tracemalloc` peak **7.0 MiB** (under
  200 MB).
- Tests: 1,888 passed (the models' layouts, catalogue, results, output columns, the explanations of the models and of the statistics
  tests, Timestral's report, `test_models_garch_report.py`), then 861 after the last corrections of the GARCH layout; in the backend
  image, one CPU.

### G2 — the simulation engine (2026-11-22)

**What is in.** `runner/R/ts_sim.R`: `ts_sim_run` (seed blocks on L'Ecuyer-CMRG streams, the batch from `memory_bytes`, the Parquet writer with one
row group a batch, pass 1's counts and moments, pass 2's selection and sub-histograms, VaR, ES, their Monte Carlo intervals, the drawdown, the
threshold's probabilities, the disk and time projections and their refusals), `ts_sim_copy` (the chunked copy to a path or an `s3://` address),
`ts_sim_bars` (a histogram merged to at most 60 bars), `ts_sim_batch_paths`, `ts_sim_batch_bytes`, `ts_sim_peak`. `runner/R/ts_sim_tests.R`: the
toy model `ts_sim_toy` (i.i.d. returns, normal or Student t scaled to unit variance, H_k = exp(r_1 + … + r_k) − 1) and five self-test checks. No
shared file was touched: the tests register themselves in `TAB_COMMON_TESTS`, `run.R` sources every file of `runner/R`.

**The API G3 calls.**

```r
sim <- ts_sim_run(model, paths, horizon, seed, limits = task$limits, measures, time = NULL,
                  file = "/tmp/paths.parquet", keep = (output == "paths"), after_seconds = 0)
# model    = list(columns = c("value", "cumulative", "price", "sigma"),   # the file's Float64 columns, `cumulative` = H
#                 draw = function(n, h) list(value = <h × n>, cumulative = <h × n>, …),  # one seed block, path by path;
#                                                                                    # .Random.seed is already the block's stream
#                 fixed_bytes, block_factor, batch_factor)                # G3's measured constants (the toy's by default)
# measures = list(var_level, es_level, tail = "lower" | "upper", quantiles, threshold = <on H's scale> or NULL,
#                 alpha, drawdown = TRUE when W_k = 1 + H_k, max_sample_paths)
# time     = an Arrow Array of length h (ts_time_array of the future times): the file's `time`, of the input's type
if (keep) ts_sim_copy(sim$file, Sys.getenv("TABULARIA_OUTPUT"))
```

It returns `steps` (per step: `mean`, `mean_low`, `mean_high`, `sd`, `skewness`, `kurtosis`, `var`, `var_low`, `var_high`, `es`, `es_low`,
`es_high`, `es_se`, `band_error`; with a threshold `count_beyond`, `prob_beyond`, `prob_beyond_low`, `prob_beyond_high` and the same for
`crossed`), `quantiles` (h × the quantiles: the fan), `drawdown` (`mean`, `sd`, `median` and its interval, `var` and its interval, or NULL),
`hist` (`horizon`, `drawdown`: `low`, `high`, `count`), `sample` (paths 1 … 20 of every column, h × 20), `run` (for `run.json` and the notes:
paths, horizon, seed, `seed_block`, bins, batches, `batch_paths`, `row_bytes`, the memory constants, `baseline_bytes`, `memory_bytes`,
`tmp_bytes`, `simulated_values`, `paths_bytes`, `pass_2_reads`, `selected_values`, the seconds of each part, `peak_bytes` with its source,
`peak_pass_1_bytes`) and `file`. Every number is on H's scale; the price and the money are G3's increasing linear maps. A threshold G3 gives
in a price or in money is mapped to H first. A refusal (`too_few_paths`, `horizon_memory`, `too_many_values`, `too_slow`, `not_finite`,
`values_memory`) deletes the file. `limits$group_max_seconds`, when G3 passes the group's `max_seconds`, ends the `too_slow` sentence with
«, up to the group's 30 minutes».

**Deviations and corrections to this spec** (the rest as written):

1. **The memory model has three measured terms, not one ρ.** One ratio cannot hold both a block's fixed work (a block of 1,000 paths of 1,000
   steps is a million values, whatever the batch) and R's garbage between collections. The growth over R0 is modelled as
   F + 8·(φ·B_s·h + ρ·m·P·h), m the file's row bytes divided by 8 (3 for the toy: two Int32 and two Float64; Arrow wraps R's vectors without a
   copy, so the batch's data is R's memory). Measured in fresh processes on a grid (10, 100, 250 and 1,000 steps; 1 to 104 blocks a batch;
   both passes): **F = 12 MB, φ = 31, ρ = 4** bound every point (the largest ρ the points ask is 3.24, at 100 steps and 3 blocks). R0 is read
   after a warm-up of the writer and the reader on one row (Arrow's Parquet code costs about 17 MB the first time).
2. **Collections are the engine's.** Measured without them: the resident size crept up batch after batch (55 MB over R0 at 100 steps,
   1-block batches, against 23 MB with them): the record batches survive the batch's minor collections, are promoted, and only a full one
   frees them. So: a **full** collection after each batch and after each row group read in pass 2 (45 ms each in a process with Arrow; 160 ms
   in one with `rugarch` loaded, G0's image: G3 counts it), a minor one after every 65,536 values made, the engine's own work on a block a
   slice of at most 65,536 values (bit-identical: every sum is per step), the reader not memory-mapped (mapped pages count in the resident
   size), `arrow.use_threads` off during the run. The pass-2 helpers are plain functions: closures made per quantity were compiled anew by R's
   JIT, 19.5 MB of garbage at 100 steps.
3. **The batch is mostly bounded by the row group.** Arrow 26.0.1's maximum row group length is **1,048,576 rows** (a `WriteTable` of 3,000,000
   rows wrote 1,048,576 + 1,048,576 + 902,848): at 10 steps 104 blocks, at 100 steps 10, from 250 steps one. Memory binds below that: at
   `memory_bytes` 256 MiB it gives 69 blocks at 10 steps and 4 at 100.
4. **A bin at the minimum.** B + 3 bins per quantity: below the first block's minimum, **at it** (a point mass: one step's drawdown is 0 on half
   the paths, and its median needs no selection), the B equal bins, above the maximum. A = (h + 1)·(B + 3)·8 bytes.
5. **Pass 1 counts; pass 2 sums.** Pass 1 keeps per bin the count only (no Σx, Σx²: `rowsum` and its row names were half of pass 1's time);
   pass 2 sums, per step and block by block in block order, the values below the bins of the ES's two ranks (⌊Np'⌋ and ⌈Np'⌉), and the
   selected values do the rest. Exact (1e-12) and batch-invariant as before; pass 1 went from 6.9 s to 3.5 s at 10 million values.
6. **The fan's edges**: the middle of their sub-bin, `band_error` the widest sub-bin of the step (the true error is at most half of it); 0 when the
   edge falls in a selected bin or at the minimum. The bins below and above the first block's range are cut between the run's minimum (maximum)
   and the edge.
7. **New refusals**: `not_finite` (a simulated value that is not a finite number: the path and the step), `values_memory` (one quantity's selected
   values would not fit the memory left: never met at the sizes measured, 16,476 values at 10 million).
8. **Formulas fixed by the code**: the upper tail is the lower tail of X = −H (negation is exact); p = 1 − c rounded to 12 decimals and ranks
   ⌈round(Np, 6)⌉ (10,000 × (1 − 0.99) is 100, not 101); ES's standard error with the k = ⌈Np'⌉ smallest values and their sample variance (k − 1);
   skewness g1 and excess kurtosis g2 (population moments); a VaR bound whose rank falls outside 1 … N (l = 0) is null.
9. **Time**: pass 2 is projected as the batches' write plus measure time, the copy at the write rate; the clock starts at `started_unix` or at the
   process's own start. A missing `memory_bytes` falls back to the cgroup's `memory.max`, then the machine's available memory.
10. **The self-test's budgets.** 64 MiB is below what R with Arrow holds before any path (R0 ≈ 188 MB), so the invariance check gives the
    engine a baseline of 0 and the three budgets as the batches' memory: 3, 10 and 10 blocks at 100 steps, and forced batches of 1 and 3 blocks;
    the five runs are `identical()` (measures, histograms, sample paths, the whole file). The real budgets are in the measurements below.
11. **A tight cgroup slows the disk**: the page cache counts in the cgroup, so writing 154 MB under a 300 MB limit took 8–9 s against 1.3 s under
    2.5 GB (the kernel writes back synchronously). The Job's `emptyDir` is the node's disk: the same applies on kind with a small group.
12. **For G3**: with `rugarch` loaded R0 is 286 MB (G0), above 256 MiB: the end-to-end node cannot meet a 256 MiB `memory_bytes`; the engine
    alone does (below). G3's acceptance at 256 MiB needs reading as «the engine's growth within the budget left after the fit», or 512 MiB.

**Criteria** (toy model; image `tabularia-models-timeseries:dev` with the code mounted, uid 10001, read-only root, `--network none`):

| Criterion | Result |
|---|---|
| Exact against the whole matrix | 10,000 paths × 10 steps (normal, lower tail, threshold, drawdown, 4 batches; Student t 4, upper tail, N·p not whole) and × 1 step (N·p' = 2.5, the drawdown's point mass): VaR and its interval, the drawdown's median and VaR **identical**; ES, mean, sd 1e-12; skewness, kurtosis 1e-10; ES's se 1e-9; Clopper–Pearson 1e-12; every fan edge within its `band_error` |
| Batch invariance | self-test: 64 MiB / 256 MiB / 2 GiB and 1- and 3-block batches at 10,000 × 100: `identical()`; 10 million values: 256 MiB (69 and 4 blocks), 512 MiB and 2 GiB (104 and 10) give the same digest at 10 and at 100 steps |
| Prefix | the 2,000 paths of a run of 2,000 are the first 2,000 of a run of 10,000, file and sample paths; block 3 is the seed's third stream |
| Refusals | `too_few_paths` (exact sentence), `horizon_memory` (exact, «needs about 356 MB … leaves 40 MB» at 1,000 steps), `too_many_values`, `too_slow` («about 20 minutes … the node's timeout is 10 minutes … up to the group's 30 minutes»), `not_finite`; no file left |
| Memory (self-test) | a child `Rscript` at `memory_bytes` 256 MiB, 2 million values (200,000 × 10): VmHWM below 256 MiB, more than one batch |
| **Memory at 10 million values** | `memory_bytes` 256 MiB = 268.4 MB, container limit 300 MB, paths on disk: **241.4 MB** peak (VmHWM; 1,000,000 × 10, 15 batches of 69,000 paths) and **250.8 MB** (100,000 × 100, 25 batches of 4,000); R0 187.4 and 188.1 MB |
| Self-test | 98 ok, 0 failed (the five checks of G2 take about 20 s) |

**Timings** (one process, the paths file on disk, Student t / normal, 10 steps, `memory_bytes` 2 GiB; the R start and the libraries, about
2 s, not counted):

| Values | Paths × steps | Simulation (pass 1) | of which measures / write | Pass 2 | Total | File | Peak (VmHWM) |
|---|---|---|---|---|---|---|---|
| 10 thousand | 1,000 × 10 | 0.19 / 0.17 s | 0.11 / 0.06 s | 0.52 s | 1.36 / 1.35 s | 0.2 MB | 196 MB |
| 1 million | 100,000 × 10 | 0.99 / 0.86 s | 0.42 / 0.13 s | 0.79 / 0.81 s | 2.47 / 2.29 s | 15.4 MB | 248 MB |
| 10 million | 1,000,000 × 10 | 9.12 / 7.65 s | 3.52 / 1.27 s | 4.54 / 4.47 s | 14.3 / 12.8 s | 154.3 MB | 267 / 264 MB |
| 10 million | 100,000 × 100 (t) | 6.47 s | 1.90 / 1.22 s | 3.83 s | 10.95 s | 153.5 MB | 295 MB |
| 10 million, 256 MiB | 1,000,000 × 10 (t) | 17.7 s | 3.69 / 9.44 s | 5.30 s | 23.7 s | 154.3 MB | 241 MB |

The spec's estimate for 10 million values was 6–13 s beyond the fit; the engine alone takes 11–14 s at 2 GiB, of which the toy's draws 2.9–4.3 s:
G3's models draw more slowly (`rdist("sstd")` 0.47 s a million, G0) and recurse, so its 30 s bound is tight. 15.4 bytes a row on disk for the toy's
two Float64 columns (the spec assumed 26–30 with a time and a third Float64).

### G6 — GARCH Predict (2026-11-22)

**What is in.** `predict_garch` in `runner/R/garch.R` (the route in `run.R` was G0's), `garch_predict_tests.R` (four self-tests), the gateway's card
check (`predictGarch`, `garchProblem` in `jobs_predict.go`) with its tests, `s.output = "forecast"` for `garch` as for `arima`. The catalogue's
contract (`PREDICT.columns.garch`, `horizon` for `garch`) is G0's and unchanged (only its comment); the copies are up to date.

**Behaviour.** The card alone: the rows need the card's `params.time` and `params.value` columns, no other. The new rows are sorted by time and
checked: no empty time (dropped as the fit does), no empty value (`gaps`), a time held twice (`duplicate_time`), the first not after the card's last
instant (`predict_before_last`: «the rows start before the model's last day … Predict continues a series»), and each the period after the one before
it, the first the period after the card's last (`predict_gap`); a time of another kind than the card's is `predict_column`; a card without its
`garch` object `bad_card`. A frequency `index` on dates has no calendar: only the order is checked (as the fit, which lays trading days one after the
other). With prices the first return is from the card's `last.price`; the output has one row per new price. The recursion is the model's own, row by row
from the card's state, in the series' units (mean, then σ² from the shocks and the variances before it, or ln σ² from z and ln σ² for EGARCH, then ε and
z); a non-finite σ is refused (`filter_diverged`). Output: the time (the input's own Arrow type: Int64 stays Int64 to the last digit), `prediction` = σ_t,
`prediction_var` = μ_t + σ_t·`var_quantile`. `horizon`: forecast rows after the last one (`garch_forecast`, the fit's own function, from the filter's
last state); their `prediction_var` is the 1-step VaR of the first, empty after it (as the fit's `volatility` output). **Without `horizon`**: no forecast
rows when there are new rows; when there are none, the card's own `horizon` (the node's default, 10), so the node never returns an empty table.

**Deviations.** (1) The recursion is the runner's own, not `ugarchfilter`: rugarch's filter cannot start from a state; the self-test checks it against
`ugarchfilter` with the fitted parameters fixed, for eight cases (GARCH with AR(1) and AR(2), GJR with the skewed t, EGARCH with MA(1) and with ARCH order
2, ARCH(2)-MA(1) without a mean, two with prices, simple and log). (2) The new rows are read whole (two columns): at 100,000 rows it is 2.4 MB of
doubles, the loop keeps vectors of the rows only. (3) The gateway refuses a card with a missing or null parameter, too few values of the state, an
unknown variance model or distribution, with a sentence that says what to fit again.

**Criteria.** σ of the new rows equal to `ugarchfilter`'s of the whole series (parameters fixed, recursion started as the fit's) and the VaR to `μ + σ·q`,
relative difference below 1e-10 in the eight cases; the forecast rows equal `garch_forecast` of the whole series' state to 1e-10; the forecast from the
card alone (no new row) has σ_{T+1} equal to the card's `sigma_next` to 1e-12; the time type is kept (Int64 above 2^31, Int32, date32, timestamp UTC, the
horizon's times too). **Memory and time** (the image `tabularia-models-timeseries:dev`, code mounted, `--memory 2500m --cpus 1`, a process of its own that
loads the card and the Parquet file, filters and writes the output): 20,000 new rows VmHWM 309 MiB, 2.6 s; **100,000 new rows `VmHWM` 324 MiB, 3.0 s**
(R, arrow and rugarch alone are about 290 MiB). `TABULARIA_GARCH_PREDICT_N` sets the rows of the self-test.

**For G7.** A flow with a GARCH registered and a Predict node needs the new rows to be the consecutive trading days after the card's last day (for the
sample data on `index`, in order, none missing); the Predict output has only the time, `prediction` and `prediction_var` (no input column), as ARIMA's.
The assistants' line «horizon for model_arima, model_garch, …» is already G0's.

## References

- Acerbi, C., Tasche, D. (2002). On the coherence of expected shortfall. *J. Banking & Finance* 26(7).
- Acerbi, C., Székely, B. (2014). Backtesting expected shortfall. *Risk* 27(11).
- Anderson, T. W., Darling, D. A. (1952). Asymptotic theory of certain «goodness of fit» criteria based on
  stochastic processes. *Ann. Math. Statist.* 23(2).
- Artzner, P., Delbaen, F., Eber, J.-M., Heath, D. (1999). Coherent measures of risk. *Math. Finance* 9(3).
- Barone-Adesi, G., Giannopoulos, K., Vosper, L. (1999). VaR without correlations for portfolios of
  derivative securities. *J. Futures Markets* 19(5).
- Berkowitz, J. (2001). Testing density forecasts, with applications to risk management. *JBES* 19(4).
- Bollerslev, T. (1986). Generalized autoregressive conditional heteroskedasticity. *J. Econometrics* 31.
- Bollerslev, T., Wooldridge, J. M. (1992). Quasi-maximum likelihood estimation and inference in dynamic
  models with time-varying covariances. *Econometric Reviews* 11(2).
- Christoffersen, P. F. (1998). Evaluating interval forecasts. *Int. Economic Review* 39(4).
- Clopper, C. J., Pearson, E. S. (1934). The use of confidence or fiducial limits illustrated in the case of
  the binomial. *Biometrika* 26.
- Engle, R. F. (1982). Autoregressive conditional heteroscedasticity with estimates of the variance of
  United Kingdom inflation. *Econometrica* 50(4).
- Engle, R. F., Ng, V. K. (1993). Measuring and testing the impact of news on volatility. *J. Finance* 48(5).
- Fernández, C., Steel, M. F. J. (1998). On Bayesian modeling of fat tails and skewness. *JASA* 93(441).
- Fisher, T. J., Gallagher, C. M. (2012). New weighted portmanteau statistics for time series goodness of
  fit testing. *JASA* 107(498).
- Galanos, A. (2026). *rugarch: Univariate GARCH models*, R package 1.5-6, CRAN.
- Glosten, L. R., Jagannathan, R., Runkle, D. E. (1993). On the relation between the expected value and the
  volatility of the nominal excess return on stocks. *J. Finance* 48(5).
- Hansen, B. E. (1992). Testing for parameter instability in linear models. *J. Policy Modeling* 14(4).
- Hansen, B. E. (1994). Autoregressive conditional density estimation. *Int. Economic Review* 35(3).
- Hansen, P. R., Lunde, A. (2005). A forecast comparison of volatility models: does anything beat a
  GARCH(1,1)? *J. Applied Econometrics* 20(7).
- Hyndman, R. J., Athanasopoulos, G. (2021). *Forecasting: Principles and Practice*, 3rd ed. (§5.4).
- Imhof, J. P. (1961). Computing the distribution of quadratic forms in normal variables. *Biometrika* 48.
- Kupiec, P. H. (1995). Techniques for verifying the accuracy of risk measurement models. *J. Derivatives* 3(2).
- L'Ecuyer, P., Simard, R., Chen, E. J., Kelton, W. D. (2002). An object-oriented random-number package with
  many long streams and substreams. *Operations Research* 50(6).
- Li, W. K., Mak, T. K. (1994). On the squared residual autocorrelations in non-linear time series with
  conditional heteroskedasticity. *J. Time Series Analysis* 15(6).
- Manistre, B. J., Hancock, G. H. (2005). Variance of the CTE estimator. *North American Actuarial J.* 9(2).
- McNeil, A. J., Frey, R. (2000). Estimation of tail-related risk measures for heteroscedastic financial
  time series: an extreme value approach. *J. Empirical Finance* 7.
- Nelson, D. B. (1991). Conditional heteroskedasticity in asset returns: a new approach. *Econometrica* 59(2).
- Nyblom, J. (1989). Testing for the constancy of parameters over time. *JASA* 84(405).
- Pébay, P. (2008). Formulas for robust, one-pass parallel computation of covariances and arbitrary-order
  statistical moments. Sandia Report SAND2008-6212.
