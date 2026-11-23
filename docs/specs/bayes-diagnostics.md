# Spec: the diagnostics and the posterior summary of a sampling run (`bayes-diagnostics`)

Status: **written and approved by the owner on 2026-11-24.** It specifies the module `bayes-diagnostics` of
`docs/specs/bayesian-editor-map.md` (**the map**), step 3 of its order of construction («built and tested on synthetic
draws parquets, before any real sampling»), after the approved `bayes-contract.md` (**the contract**), which fixed the
draws parquet, the second pass and the rows this module fills. Nothing of it is built. Decisions marked **(owner)** are
the owner's (the map's «Interview answers», numbered as there: **the answers**, and the owner's rules); the rest are this
spec's, to confirm; those marked «spec decision, 2026-11-24, owner may revisit» are small choices the owner may change
without reopening the module. The owner answered this spec's question on 2026-11-24 («Decisions of 2026-11-24»); no
question is open.

Words: **verified** = read in this repository's code on 2026-11-24; **measured** = run on 2026-11-24 on the development
machine (a 4-core laptop CPU from 2018, 16 GB) with a throw-away numpy prototype outside the repository, `numpy==2.5.5`
through `uv run --no-project`, under the heavy-job lock, the files deleted afterwards; **assumed** = from the literature
or a package's documentation, not run here, checked by the lot named beside it (most of them by the witnesses).

## Objective

From the draws parquet a sampling run appends while the chains sample (the contract: always long, one row group a block
of draws of one chain, quantity-major inside a row group, 8 significant digits: the contract's «Decisions of 2026-11-24 (K0)»), the runner computes, **as measures and
facts only** (answer 11):

- **per quantity**: the posterior summary (mean, sd, quantiles 5, 25, 50, 75, 95 %), the Monte Carlo standard errors of
  the mean and of the sd, the **rank-normalized split R-hat** and the **bulk and tail effective sample sizes** of
  Vehtari, Gelman, Simpson, Carpenter and Bürkner (2021), and the **prior-to-posterior contraction**;
- **per chain and in all**: divergences, the draws at the largest tree depth and the deepest tree, E-BFMI, the step size,
  the mean acceptance statistic and the mean number of leapfrog steps;
- **for the charts**: thinned draws for the trace charts, rank histograms, density curves (posterior and prior), each
  unit's median and interval, and pair charts with the divergent draws marked;

**never holding the draws whole**: the sampler's facts from accumulators fed by each block while the CSV is converted, the
per-quantity measures from a **second pass by blocks of quantities** read back from the parquet, the memory bounded by
the block, not by the model (the owner's rule of 2026-11-22/23; the contract, «The measures»). It is checked against two
**witnesses outside the image** — the R package `posterior` and ArviZ — and measured at the map's size, **20,000
quantities × 4,000 draws**.

Who reads it: `bayes-runner` (calls the accumulators and the second pass), `bayes-report` (draws the rows, words the
measures), `bayes-mlflow` (the summary metrics of the card), `assistants` (reads the measures to propose remedies). No
verdict, no threshold, no reference value is ever in a row (the contract, «Verdicts»).

## Decisions

| Question | Decision | By |
|---|---|---|
| What a diagnostic is | **A measure or a fact**, each with a sentence on what it measures and the literature's reference values **cited as such** by the report; no reference option, no mark, no stop on divergences | Owner (answer 11; the rule of 2026-11-20) |
| Memory | **Never the draws whole**: accumulators while sampling, then blocks of quantities read back; a test shows the peak flat when the draws are multiplied by ten | Owner (rule of 2026-11-22/23) |
| The precision of the draws | 8 significant digits (CmdStan 2.40.0's default, measured by K0): every measure rests on rounded values; ties are possible and handled («Ties») | Owner (2026-11-24, the contract) |
| The definitions | Those of Vehtari et al. (2021) **as the R package `posterior` 1.8.0 implements them**: where the paper leaves a detail open (the odd draw of a split, ties, the truncation of the autocorrelation sum, the floor of tau), `posterior`'s choice is ours, written down by lot X1 and checked by the witnesses | Spec decision, 2026-11-24, owner may revisit (one reference implementation, so «the same numbers as a known tool» is testable) |
| The inverse normal CDF of the rank normalization | Wichura's algorithm AS241 in numpy (the image holds no SciPy: the image spec), the one Python's `statistics.NormalDist.inv_cdf` and R's `qnorm` use | Spec (Python's: verified in its source; R's: assumed, the witness closes it) |
| Quantiles | Hyndman and Fan's type 7 (numpy's default), as the contract fixed and `prior-preview` uses | The contract |
| E-BFMI | Per chain, the mean of the squared differences of successive energies over the energies' variance (Betancourt 2016), **as ArviZ's `bfmi` computes it** | Spec decision, 2026-11-24, owner may revisit |
| The witnesses | **`posterior` 1.8.0 (R, BSD-3-Clause) and ArviZ 1.3.0 (Python, Apache-2.0: arviz-stats 1.3.3, arviz-base 1.3.1; pinned by lot X0), outside the image**, run by hand by `make_witness.R` and `make_witness.py`, their outputs committed in `reference/diagnostics/witness.json` (the groups' precedent: `witness.json` of `timeseries`, `boosting`…) | The map; the files: spec |
| Density curves | Gaussian kernel, Silverman's rule-of-thumb bandwidth (Silverman 1986), 200 points (`bayes_density_points`) over a range said on the chart | Spec decision, 2026-11-24, owner may revisit (a chart, not a measure) |
| Pair charts | The first four scalar quantities of the charts' scope, the six pairs, **300 thinned draws** each, the divergent draws as a series of their own; two new caps of the catalogue | Spec decision, 2026-11-24, owner may revisit (the map's «thinned draws for … pair charts»; the rows join the caps' arithmetic: «Corrections proposed») |
| The prior's draws, for the contraction and the prior's density | The **`prior_marginal` columns** `predictive-checks` writes in every run (one ancestral draw a draw of each quantity node, a plate's representative unit) | `bayes-predictive-checks.md` (draft of the same day) |
| Prior-to-posterior contraction | **1 − Var_post/Var_prior** (Betancourt 2018), **NULL when the prior's marginal has no finite variance**, decided by a field of the catalogue («Prior-to-posterior contraction») | Owner, 2026-11-24 |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| The draws parquet | The contract, «The draws parquet»: columns `chain`, `draw`, `quantity`, `node`, `unit`, `index`, `index_2`, `value`; one row group a block of B draws of one chain, chains interlaced block-major; **quantity-major inside a row group**; the sampler's seven fields kept as quantities; the metadata `tabularia.bayes` at the closing (chains, draws per chain, block, the `draw` range of each row group) | Read back by blocks of quantities, `value` alone, at offsets computed from the order («The second pass») |
| The second pass's budget | The contract, «The measures»: `pass_bytes` = `memory_bytes` / 4, ⌈8·C·D·P / `pass_bytes`⌉ passes; measured there: 20,000 quantities × 4,000 draws read back in 20 passes of 32 MiB in **26.5 s**, peak **113 MB** above the start | The read; this module adds the computation's own budget |
| The rows | `internal/bayescontract/declaration.go`: `SummaryColumns`, the sections `summary` (12 numbers, 17 with the original scale), `quantity` (`rhat_max`, `ess_bulk_min`, `ess_tail_min`), `sampler` (`divergences`, `treedepth_hits`, `treedepth_max`, `e_bfmi`, `stepsize`, `accept_stat_mean`, `n_leapfrog_mean`, `seconds_warmup`, `seconds_sampling`, `draws`), `curve` (`trace:`, `density:`), `hist` (`rank:`), `point` (`units:`); `Limits` (`bayes_max_summary_quantities` 2,000, `bayes_max_chart_quantities` 12, `bayes_trace_points` 200, `bayes_rank_bins` 20, `bayes_density_points` 200, `bayes_max_unit_charts` 8, `bayes_unit_points` 1,000); `MaxResultRows` | Filled as declared; two caps added for the pairs |
| A first ESS in numpy | `gateway-go/internal/modelgroups/groups/bayes/runner/py/bayes_stats.py` (lot B0's self-test): `ess(chains)` — Stan's multi-chain autocorrelation **without** the rank normalization, Geyer's initial monotone sequence, by FFT; `mean_and_sd` with their MCSE; the exact Kolmogorov–Smirnov distribution | `ess` is the kernel of the bulk and tail ESS, vectorized over a block of quantities; the self-test keeps its own use (its file's tests unchanged) |
| A test of the statistics outside the image | `groups/bayes/reference/test_bayes_runner.py` (B0): the runner's KS p-value against SciPy's `kstwo` within a relative 1e-7, run with SciPy 1.18.1 outside the image | The same place and voice for the witnesses' comparison |
| Witness files | `groups/*/reference/witness.json` with `make_witness.py` (e.g. `timeseries`: «never installed in the group's image; not part of the recipe, so changing them builds nothing») | `reference/diagnostics/witness.json` |
| AS241 | Python's `statistics._normal_dist_inv_cdf` (the pure-Python fallback of `NormalDist.inv_cdf`): «Wichura, M.J. (1988). Algorithm AS241» | The numpy port; the test compares it with `NormalDist().inv_cdf` |
| The order of the quantities | `bayescontract.Header(mode, quantities, sizes)`: the CSV's columns (the sampler's fields, then each variable's elements, column-major); the roles (`quantity`, `internal`, `check`, `prior_draw`) | The order of a row group's rows (the CSV's order without the internal and check columns) |

## The measures

For a quantity θ with M chains of N draws (S = M·N), after the rows of non-finite draws were refused by the follower
(`not_finite`, the contract):

| Measure | Definition | Source |
|---|---|---|
| `mean`, `sd` | the draws' mean and standard deviation (divisor S − 1) | — |
| `q5` … `q95` | type 7 quantiles of the S draws | Hyndman and Fan (1996); the contract |
| Split chains | each chain cut in two halves of ⌊N/2⌋ draws; with an odd N the middle draw is left out | Vehtari et al. (2021); the odd draw: `posterior`'s (assumed, the witness closes it) |
| Rank normalization | z = Φ⁻¹((r − 3/8) / (S + 1/4)), r the rank of a draw among all S, **ties given their average rank** | Vehtari et al. (2021), the normal scores with Blom's offset; ties: `posterior`'s (assumed, the witness) |
| `rhat` | max(R̂ of the rank-normalized split chains, R̂ of the rank-normalized split chains of the folded draws \|θ − median(θ)\|), R̂ = √(var⁺/W), var⁺ = (N′−1)/N′·W + B/N′ | Vehtari et al. (2021) |
| `ess_bulk` | the ESS of the rank-normalized split chains: S / τ̂, τ̂ from the multi-chain autocorrelations by FFT, summed by Geyer's initial monotone sequence, floored at 1/log₁₀(S) | Vehtari et al. (2021); the floor: `posterior`'s (assumed, the witness) |
| `ess_tail` | min(ESS of I(θ ≤ q5), ESS of I(θ ≤ q95)) on the split chains | Vehtari et al. (2021) |
| `mcse_mean` | sd / √ESS of the split chains of θ (not rank-normalized) | Vehtari et al. (2021) |
| `mcse_sd` | as `posterior::mcse_sd` 1.8.0 computes it (from the ESS of the centred squared draws; the formula transcribed from its source by lot X1 and cited there) | assumed, the witness |
| NULLs | `rhat`, `ess_*`, `mcse_*` NULL when the quantity is constant over every draw (the contract) or a split chain has fewer than 3 draws; never `inf` or `NaN` (the ten columns' rule) | the contract |

**Ties.** With 8 significant digits, a quantity whose posterior is narrow beside its size repeats values (an intercept of
1,000.12345 with an sd of 0.00001 is written 1,000.1235 or 1,000.1234: the contract's «Risks», at two more digits than it
first said). Average ranks keep the rank
normalization defined; when every draw is equal the quantity is constant (NULLs). A quantity with ties is never marked:
the precision is a fact the report says once for every quantity (`bayes-report`).

**The sampler's facts** (accumulators, per chain, fed by each block in block order: the contract, «While sampling»):

| Fact | Definition | Source |
|---|---|---|
| `divergences` | the count of draws with `divergent__` = 1 | Betancourt (2016); Stan Reference Manual, «Divergent transitions» (assumed) |
| `treedepth_hits`, `treedepth_max` | the draws whose `treedepth__` equals the node's `max_treedepth`; the deepest | Stan Reference Manual, «Tree depth» (assumed) |
| `e_bfmi` | Σₙ₌₂ᴺ (Eₙ − Eₙ₋₁)² / (N − 1) divided by Σₙ (Eₙ − Ē)² / N, E the chain's `energy__`; the last energy of a block kept for the next | Betancourt (2016); ArviZ's `bfmi` (assumed, the witness) |
| `stepsize`, `accept_stat_mean`, `n_leapfrog_mean` | the step size after adaptation (the CSV's comment, the contract), the means of `accept_stat__` and `n_leapfrog__` | — |
| `all` | the sums of the counts, the deepest tree, the smallest E-BFMI over the chains | — |

### What the literature says, for the report (cited as such, never in a row)

`bayes-report` writes the sentences; this module only fixes the references, so that every module quotes the same ones.
Each value below is **assumed** until `bayes-report` quotes it verbatim from its source and records the page.

| Measure | What it measures | The literature's reference value, as the source writes it |
|---|---|---|
| R-hat | Whether the chains agree with each other and within their halves, in the bulk and in the tails | Vehtari et al. (2021) recommend using the draws only when R-hat is below 1.01, with at least four chains |
| ESS bulk, tail | How many independent draws the chains are worth for the centre and for the 5 % and 95 % quantiles | Vehtari et al. (2021) recommend a bulk and a tail ESS of at least 100 per chain (400 for four chains) |
| MCSE | The Monte Carlo error of the mean and of the sd | No reference value: to compare with the precision the reader needs |
| Divergences | Transitions where the integrator diverged: a region the sampler could not explore | Betancourt (2016) and the Stan Reference Manual: any divergence means the draws may be biased |
| Tree depth | Transitions that stopped at the largest tree | The Stan Reference Manual: an efficiency concern, not a validity one |
| E-BFMI | How well the momentum resampling explores the energy | Betancourt (2016): values below 0.3 suggest the sampler explores the energy poorly |
| Contraction | How much the data narrowed the prior | Betancourt (2018), «Towards a principled Bayesian workflow» (arXiv:1803.08393) |

## The second pass

After CmdStan has exited (the contract), the runner calls `bayes_diag.second_pass(draws_path, plan, budget)`:

1. **The order.** The rows of every row group are the CSV's columns that are not internal or check, in the CSV's order,
   each with the row group's B draws: quantity k's draws are rows [k·B, (k+1)·B). The order is computed from
   `bayescontract.Header` and the roles, and **checked once** against the `quantity` column of the first row group (its
   dictionary indices, a few hundred KB); a difference is a fault of Tabularia (exit 1).
2. **The blocks.** The quantities are taken in the manifest's order, as many at a time as `pass_bytes` allows (8·S bytes
   each; the contract). For each block, every row group's **`value` column alone** is read
   (`ParquetFile.read_row_group(i, columns=["value"])`, at most `block_values` doubles: 8 MiB) and the block's slices are
   copied into an array shaped (P_block, M, N) at their chain and draw; the row group is freed.
3. **The computation**, in sub-blocks of quantities sized by the computation's own budget, `compute_bytes` =
   `memory_bytes` / 8 by default: the measured working memory is about **13 times a sub-block's draws** (104 bytes a
   value: the split, the ranks, the normal scores, the folded draws, the FFT's padded spectra), so a sub-block holds
   ⌊`compute_bytes` / (104·S)⌋ quantities (at least one). Each sub-block's rows are appended to the summary writer and the
   results writer (the contract), then freed.
4. **The charts' draws**, during the same pass, for the quantities of the charts' scope (the contract's «Which
   quantities»): the thinned trace draws, the ranks' histograms, the density curves, each unit's median and interval
   (from the summary), the pairs' thinned draws («The charts»).
5. **The time**, projected before the pass from the first row group's read rate (the contract) **and** from the first
   sub-block's computation rate; `too_slow` before any result when the projection passes the budget.

**Memory**, at most: `pass_bytes` (the block) + `compute_bytes` (the sub-block's working space) + one row group (8 MiB)
+ the charts' thinned draws (12 quantities × 8 chains × 200, and 6 pairs × 300) — at the defaults (`memory_bytes` 2 GiB):
512 + 256 + 8 + small ≈ **0.8 GiB**, whatever the number of quantities and draws, the sampler having exited.

## Measured on 2026-11-24 (the map's «measure first»)

A numpy prototype of the per-quantity measures (the split, the average ranks, AS241, the two R-hats, five ESS — bulk, two
tails, the mean's, the squares' — and the five quantiles), vectorized over a sub-block of quantities; synthetic draws:
AR(1) chains with ρ = 0.5 around a random location, rounded. The prototype's numbers are sane (ESS bulk 1,326 against
S(1 − ρ)/(1 + ρ) = 1,333 at 4 × 1,000 draws; R-hat 1.0005–1.0052 on agreeing chains) but it was **not** checked against
the witnesses: these are costs, not results.

| Draws (chains × draws) | Quantities | Sub-block | Computation | Per quantity | Peak RSS (after imports) |
|---|---|---|---|---|---|
| 4 × 1,000 | **20,000** | 500 | **63.6 s** | 3.18 ms | **248 MiB** (31 MiB) |
| 4 × 1,000 | 2,000 | 50 | 6.4 s | 3.18 ms | 54 MiB |
| 4 × 10,000 | 500 | 50 | 18.5 s | 37.0 ms | 238 MiB |

- **The map's size** (20,000 quantities × 4,000 draws): **about 64 s of computation** plus the contract's measured read
  (26.5 s in 20 passes; fewer passes at the default `pass_bytes`), **about 1.5 minutes** on the measured CPU, with a peak
  that depends on the sub-block (217 MiB above the imports at 2 million values a sub-block), not on the quantities.
- **The working memory** is about 104 bytes a value of the sub-block (217 MiB for 500 × 4,000 values, 207 MiB for
  50 × 40,000): the sizing rule of «The second pass».
- **The time grows a little faster than the draws** (×11.6 for ×10 draws: the sort of the ranks and the FFT are
  n log n). The largest run the disk allows bounds it: the contract's default `tmp_bytes` (2 GiB) holds about 119 million
  values (about 18 bytes a value on `/tmp`, the contract's K0 decision), about **100 s** of computation at the measured rate.
- Not measured: the read of `value` alone (the contract read every column), the charts' draws, a real CmdStan run.
  Lot X3 measures them on the fake draws of the contract's `fake_cmdstan.py`.

## The charts

All from the second pass, for the quantities of the charts' scope; every series is a drawing, never a measure:

| Chart | Rows | Rule |
|---|---|---|
| `trace:<id>` | `curve`, series the chain | `bayes_trace_points` (200) draws a chain, evenly spaced, the last included (the contract) |
| `rank:<id>` | `hist`, series the chain | the ranks of the pooled draws (the rank normalization's), each chain's histogram in `bayes_rank_bins` (20) equal bins of 1…S (Vehtari et al. 2021, rank plots) |
| `density:<id>` | `curve`, series `posterior`, `prior` | Gaussian kernel, bandwidth h = 0.9·min(sd, IQR/1.34)·n^(−1/5) (Silverman 1986), 200 points; the range spans the posterior's 0.5–99.5 % quantiles widened to the prior's 5–95 %, and the part of either outside is cut, the cut said on the chart (as `prior-preview` cuts heavy tails); the prior's draws are the quantity's `prior_marginal` column; no prior series when there is none (a version saved before the checks) |
| `units:<id>` | `point`, series `median`, `low`, `high` | from the summary (q50, q5, q95); `bayes_unit_points` (the contract) |
| `pairs:<a>:<b>` (new) | `curve`, series `draw` and `divergent`, `x0` the draw of a, `value` the draw of b | the first `bayes_max_pair_quantities` (4) scalar quantities of the scope, their six pairs, `bayes_pair_points` (300) draws pooled over the chains and evenly spaced, the divergent draws among them in their series (where divergences happen: the reason the contract keeps `divergent__`) |

## Prior-to-posterior contraction

1 − Var_post(θ) / Var_prior(θ) (Betancourt 2018; the owner, 2026-11-24), per quantity, Var_prior from the quantity's
`prior_marginal` draws (the same draws for every unit of a plate: the units are exchangeable a priori, `prior-preview`'s
«One unit stands for its plate»). **It is NULL when the prior's marginal has no finite variance**, decided from the graph
by one function: a node's marginal has a finite variance when its distribution has one for its constant arguments (the
catalogue's field `finite_variance`: normal, exponential, gamma, lognormal, beta, uniform always; Cauchy never; Student-t
only with ν > 2; inverse-gamma only with α > 2; a truncation to a bounded range always) **and** every node its arguments
read has one; the reason travels with the NULL («the prior has no finite variance»), so the number is never an estimate
of an infinite quantity. It is a number of the `summary` section (`contraction`) and a column of the `summary` output;
never a mark.

## The prior predictive mode

Under `fixed_param` (the contract's variant `prior_predictive`) the draws are independent by construction: the summary
holds mean, sd and the quantiles only (the contract's variant), no R-hat, ESS, MCSE or sampler facts; the density charts
show the prior draws alone.

## Witnesses

`reference/diagnostics/`: `cases.json` (the draws of each case, written once by `make_cases.py` with a fixed seed and
**stored as values**, so that no generator has to agree across languages), `make_witness.R` (`posterior` 1.8.0:
`rhat`, `ess_bulk`, `ess_tail`, `mcse_mean`, `mcse_sd`, `quantile2` type 7), `make_witness.py` (ArviZ: `rhat` with the
rank method, `ess` bulk and tail, `mcse`, `bfmi`; the version pinned in the file's header by lot X0), `witness.json`
(their outputs and versions, «never installed in the group's image»). Run by hand, never in CI.

| Case | What it pins |
|---|---|
| i.i.d. normal draws, 4 × 1,000 | R-hat near 1, ESS near S |
| AR(1), ρ = 0.9 | the autocorrelation sum and its truncation |
| one chain shifted | R-hat of the location (bulk) |
| one chain with four times the scale | the folded R-hat (tail) that the bulk misses (Vehtari et al. 2021's example) |
| Cauchy draws | rank normalization where moments do not exist |
| draws rounded to 8 significant digits around 1,000 with sd 0.00001 | ties (average ranks) |
| a constant quantity | NULLs |
| an odd number of draws (4 × 999) | the split's middle draw |
| one chain | the split of a single chain |
| `divergent__`, `treedepth__`, `energy__` of a fake run | the facts and E-BFMI (ArviZ) |

**Tolerances, declared**: R-hat, ESS, MCSE within a relative **1e-8** of `posterior` (the same definitions, double
precision; the FFTs differ in their last bits: assumed, the lot records the largest gap); quantiles within 1e-12;
E-BFMI within 1e-10 of ArviZ. A case where `posterior` and ArviZ differ from each other is recorded with both values and
the reason; ours must equal `posterior`'s (the decision above).

## Tech stack

- **Python** 3.14 of the image, **`numpy` 2.5.5 and `pyarrow` 26.0.1** only (the image spec's pins); the standard
  library. No SciPy, no pandas (not in the image).
- **Outside the image, by hand**: R with `posterior` 1.8.0 (BSD-3-Clause, the map's «Research»); Python with ArviZ
  (Apache-2.0, the map's «Research») and SciPy 1.18.1 for the tests' comparisons, through `uv run --no-project`.

## Commands

```bash
# the module's unit tests, outside any image (uv with the image's pins; small data only)
uv run --no-project --with pyarrow==26.0.1 --with numpy==2.5.5 \
  python -m unittest discover -s gateway-go/internal/modelgroups/groups/bayes/runner/tests -p 'test_diag*.py' -v
# the witnesses' comparison (SciPy only to compare the AS241 port and the KS kernel; never in the image)
uv run --no-project --with numpy==2.5.5 --with pyarrow==26.0.1 --with scipy==1.18.1 \
  python -m unittest gateway-go/internal/modelgroups/groups/bayes/reference/test_bayes_runner.py -v
# the witnesses themselves, by hand, rarely (their outputs are committed)
Rscript gateway-go/internal/modelgroups/groups/bayes/reference/diagnostics/make_witness.R
uv run --no-project --with arviz==1.3.0 --with numpy==2.5.5 python gateway-go/internal/modelgroups/groups/bayes/reference/diagnostics/make_witness.py
# the measure at the map's size, alone (a heavy job for this machine: one at a time)
BAYES_DIAG_MEASURE=1 uv run --no-project --with pyarrow==26.0.1 --with numpy==2.5.5 \
  python -m unittest gateway-go/internal/modelgroups/groups/bayes/runner/tests/test_diag_measure.py -v
```

## Project structure

```text
gateway-go/internal/modelgroups/groups/bayes/runner/py/
  bayes_diag.py         the per-quantity measures, vectorized over a sub-block (split, ranks, AS241, R-hat, ESS, MCSE, quantiles)
  bayes_facts.py        the sampler's accumulators (fed by the follower block by block)
  bayes_pass2.py        the second pass: the order, the blocks, the sub-blocks, the charts' draws, the rows
  bayes_charts.py       thinning, rank histograms, the kernel density, pairs
gateway-go/internal/modelgroups/groups/bayes/runner/tests/
  test_diag.py, test_facts.py, test_pass2.py, test_diag_measure.py
gateway-go/internal/modelgroups/groups/bayes/reference/diagnostics/
  make_cases.py, cases.json, make_witness.R, make_witness.py, witness.json
gateway-go/internal/modelgroups/groups/bayes/reference/test_bayes_runner.py   + the witnesses' comparison
gateway-go/internal/bayescontract/declaration.go     + contraction, the pairs' caps (lot X4)
backend/app/engine/models/catalog.json               + the same (copied by model_catalog.py)
```

`bayes_stats.py` stays the self-test's; its `ess` moves into `bayes_diag.py` vectorized, and the self-test calls the new
one only if its own tests stay byte for byte (check 4's numbers recorded in the image spec's «As built»).

## Code style

The ESS as lot B0 wrote it (verified, `runner/py/bayes_stats.py`):

```python
def ess(chains: np.ndarray) -> float:
    """The effective sample size of draws shaped (chains, draws)."""
    m, n = chains.shape
    acov = np.array([_autocovariance(c) for c in chains])
    means = chains.mean(axis=1)
    w = float((acov[:, 0] * n / (n - 1)).mean())
    var_plus = w * (n - 1) / n + (float(means.var(ddof=1)) if m > 1 else 0.0)
    if var_plus <= 0:
        return float(m * n)
    rho = 1.0 - (w - acov.mean(axis=0)) / var_plus
```

This module's in the same voice, over a sub-block (a sketch; lot X1 writes it): the axis 0 is the quantities, and no
Python loop runs over the draws.

```python
def ess_block(x: np.ndarray) -> np.ndarray:
    """The effective sample sizes of a sub-block shaped (quantities, split chains, draws): S / tau,
    tau from Geyer's initial monotone sequence of the paired autocorrelations, floored as posterior does."""
    q, m, n = x.shape
    spectrum = np.fft.rfft(x - x.mean(axis=2, keepdims=True), 2 * n, axis=2)
    acov = np.fft.irfft(spectrum * np.conj(spectrum), axis=2)[:, :, :n] / n
    del spectrum                               # the largest array of the computation: freed at once
```

## Testing strategy

**On small data, always** (owner's rule): the witnesses' cases are a few thousand draws; the map's size runs only in the
measure, by hand.

- **The measures** (`test_diag.py`): every case of `witness.json` within its tolerance; AS241 against
  `statistics.NormalDist().inv_cdf` at 10,000 points from 1e-300 to 1 − 1e-16 (equal to the last bit or within 1e-15);
  a sub-block of one quantity gives the numbers of a sub-block of 500 holding it (no leak across the axis); the NULLs;
  non-finite inputs never reach it (the follower's `not_finite`), and a test proves an `inf` would be refused, not summed.
- **The facts** (`test_facts.py`): the accumulators fed block by block (blocks of 1, 7 and 1,000 draws) equal the same
  facts computed on the whole fake chain, to the last bit for counts and within 1e-12 for E-BFMI; the energy kept across a
  block's edge.
- **The second pass** (`test_pass2.py`, on parquets written by the contract's writer from `fake_cmdstan.py`): the order
  checked, and a file whose first row group is shuffled refused; the same rows with `pass_bytes` set to hold 1, 3 and all
  quantities; **`value` alone is read** (a test wraps `read_row_group` and fails on any other column after the order's
  check); the charts' rows and the caps; a unit named `a|b` kept as it is in its key (the engine's rule: parts joined by `|`, no escape).
- **The memory** (`test_diag_measure.py` and the contract's `test_memory.py`): the peak of the second pass flat (within
  the contract's 5 MB) when the draws are multiplied by ten at fixed `pass_bytes` and `compute_bytes`; the measure at
  20,000 quantities × 4,000 draws, by hand, recorded in «As built».
- **Regression**: the self-test's file (`bayes_stats.py`) and its checks unchanged; the contract's tests; the caps'
  arithmetic test of `bayescontract` (`MaxResultRows`) with the pairs added.

## Boundaries

- **Always**: accumulators while sampling, blocks of quantities after; `value` read at computed offsets, the order checked
  once; the working memory sized from `compute_bytes`; the definitions of `posterior` 1.8.0, written down with their
  source; every number a measure or a fact; NULL for what is not defined.
- **Ask first**: another definition of any measure; a new section, number or chart (the contract's «Ask first»); a cap of
  the results; a package beyond `numpy` and `pyarrow`.
- **Never**: the draws of all quantities in memory, or a whole chain of every quantity; `read_table` of the draws; a
  threshold, a reference value, a mark or a verdict word in a row; an option that stops the run; SciPy, pandas, ArviZ or
  R in the image.

## Success criteria

1. Every case of `witness.json` passes within the declared tolerances; a disagreement between the two witnesses is
   recorded, ours equal to `posterior`'s.
2. On the contract's fake draws, the summary, the facts and the charts' rows are the same with any `pass_bytes`, and the
   second pass reads only the `value` column after the order's check.
3. The second pass's peak stays within 5 MB when the draws are multiplied by ten, at fixed budgets.
4. At 20,000 quantities × 4 × 1,000 draws, by hand on the development machine: the computation within 25 % of the
   measured 64 s and its peak within the budget of «The second pass»; recorded.
5. No row holds a threshold, a reference value or a verdict word (a test scans the texts of every row the module writes).

## Lots

| Lot | Objective | Files | Accepted when | Depends on | Agent |
|---|---|---|---|---|---|
| **X0** | The witnesses: cases, `make_witness.R`, `make_witness.py`, `witness.json`; `posterior`'s definitions transcribed with their source lines | `reference/diagnostics/*` | the file written by hand, its versions pinned | — | sonnet (by hand) |
| **X1** | The measures, vectorized; AS241 | `bayes_diag.py`, `test_diag.py` | criterion 1 | X0 | **opus** (numerics against two witnesses) |
| **X2** | The sampler's accumulators | `bayes_facts.py`, `test_facts.py` | the facts' tests | the contract's K2 (the follower calls them) | sonnet |
| **X3** | The second pass and the charts | `bayes_pass2.py`, `bayes_charts.py`, tests | criteria 2, 3 | X1, X2; the contract's K1 (the writers) | sonnet |
| **X4** | The rows added: `contraction`, the pairs and their caps (the owner's answer of 2026-11-24) | `declaration.go`, `catalog.json`, the arithmetic's test | the caps' test green | the catalogue's field `finite_variance` (`bayes-distribution-catalog.md`, correction of 2026-11-24) | sonnet |
| **X5** | The measure at the map's size, by hand, alone | `test_diag_measure.py`, «As built» | criterion 4 | X3 | sonnet (by hand) |

X0 and X2 can start at once; X5 is the only heavy lot.

## Corrections made to the map and to the approved specs (2026-11-24)

Applied on the owner's approval of 2026-11-24, each marked «Correction of 2026-11-24 from the step-4 specs» where it
lands:

- **`bayes-contract.md`**, «Sections»: `summary` gains the number `contraction` (and the `summary` output its column);
  `curve` gains the charts `pairs:<a>:<b>` (series `draw`, `divergent`); the catalogue's `limits` gain
  `bayes_max_pair_quantities` (4) and `bayes_pair_points` (300). **The caps' arithmetic**: the formula of
  `bayescontract.MaxResultRows` (verified) gives today, at 8 chains and `max_nodes` 200, 34,000 + 25,920 + 24,000 + 6,000
  + 1,600 + 90 + 80 + 5,000 = **96,690** rows (the contract's text says «≈ 92,300»: it counts the `quantity` section at
  3,200 where the code has `bayes_max_quantity_entries` 600 × 10 = 6,000); the pairs add 6 × 300 = 1,800, **98,490**,
  under 100,000 with 1,510 to spare.
- **`bayes-contract.md`**, «The measures»: the second pass reads the `value` column alone at offsets computed from the
  order, the order checked once; a computation budget `compute_bytes` (`memory_bytes` / 8) beside `pass_bytes`.
- **`bayes-contract.md`**, «The quantity manifest»: a role `prior_marginal` (`bayes-predictive-checks.md`), read by this
  module in both modes.
- **The map**, `bayes-diagnostics`' «Measure first»: done (this spec, «Measured on 2026-11-24»).

## Decisions of 2026-11-24 (the answers to this spec's questions)

| Question | Decision | By |
|---|---|---|
| Prior-to-posterior contraction | **1 − Var_post/Var_prior** (Betancourt 2018), **NULL when the prior's marginal has no finite variance**, decided by a field of the catalogue (`finite_variance`) and the graph («Prior-to-posterior contraction») | Owner, 2026-11-24 |

## Open questions

None.
