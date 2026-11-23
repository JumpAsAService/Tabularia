# Spec: the explanation of the method in every report (lots B0–B4)

Status: **written by the agent on 2026-11-22; lots B0–B4 built** (the trial on kind is left for the next one,
with the forecasting group's). The owner decided the piece on 2026-11-21
(«la stessa spiegazione anche nei report dei 21 test statistici e dei modelli esistenti, dopo l'A/B»);
the questions this spec had to settle were answered by the agent while the owner was away, each marked
«Decided by the agent, to confirm» in «Open questions».

The A/B test's report (`docs/specs/ab-testing.md`, «Explaining the method in the report», **the A/B
spec**) and the decomposition's (`docs/specs/ts-decomposition.md`, the same section) explain their
method to a reader who is not a statistician, in two places: **a reading** of one to three sentences in
italics under each block, with the run's numbers; and **«How this report was computed»**, the last
page(s), one paragraph for each analysis that ran and a box of formulas. Everything in five languages
(en, it, de, es, fr), from templates filled with the run's numbers (`@id name=value`, said by
`structured_note`); no text written at run time, no AI. This spec brings the same explanation to the
reports of the **21 statistical tests** (`backend/app/engine/stats/layouts.py`) and of the **ten
existing models** (linear, logistic, Kaplan–Meier, Cox, parametric survival, ARIMA, XGBoost, CatBoost,
CLV, BG/BB).

Words: **verified** = read in this repository's code on 2026-11-22.

## Decisions

| Question | Decision | By |
|---|---|---|
| The piece | The explanation of the A/B and of the decomposition in the reports of the 21 tests and of the ten models | Owner (2026-11-21) |
| Verdicts and measures | A verdict («significant», «rejected», «the data show…») **only** from a test whose p-value is compared with α; every other number is a **measure**, said without a word of judgement; a measure is compared with a threshold only as «above / below the threshold of …» | Owner (rule of 2026-11-20, A/B spec) |
| Who chooses the sentences | **The layout**, from the rows the runner (or the statistics engine) already wrote: no runner, no image and no statistics computation changes | Agent, to confirm (Open question 1) |
| Where the shared pieces live | `backend/app/engine/models/layouts/common.py` (moved there from `experiments.py` in B0) | Agent (B0), as the decomposition spec's Open question 12 planned |
| The statistics tests' formula box | **None new**: their page already has «How it is computed» (the formula with the run's numbers, its symbols, the distribution, the critical region, every number); the last page holds the method's paragraphs only | Agent, to confirm (Open question 2) |

## What exists and is reused (verified)

- **The text side** (`layouts/common.py`, unchanged by this piece): `with_labels` says a row of the
  section `reading` through `reading_said` — the template `reading_texts.<id>` (`formula_texts.<id>` in
  the block `formulas`), each `{name}` replaced by its value, `{name:pct|spct|int|p|num|date}` a number
  in that form (`number_form`), a name (`NOTE_NAMES`) never read as a number, the label from
  `reading_labels.<id>` or `reading_blocks.<block>`; `by_params` lets a template depend on the node's
  settings. Values arrive encoded (`%25`, `%20`, `%2C` for «%», « », «,») and are decoded once.
- **The page side** (moved to `common.py` in B0): `reading_of`, `reading_under(p, w, *blocks)` (the
  block's sentences in italics, «Reading:» first, visible only when the block has a `line_1`),
  `reading_table` (a sentence or two per level, a table), `how_computed(p, w)` (the heading, a lead and
  one table «Reading»: the `method` paragraphs, then the `formulas` on a darker panel; Excel keeps it as
  the sheet «Reading»), `pct`, `signed_pct`, `p_with_sign`, `model_number`, `model_text`, `key_part`,
  `document`.
- **The words**: each model's `report_texts/<model>.json` (five languages, the same keys), merged over
  `common.json`; the statistics tests' `backend/app/engine/stats/report_texts.json` (one file, `tests.<id>`
  per test).
- **The tests of the A/B and of the decomposition** (`backend/tests/test_models_layouts.py`): the five
  texts of every id with the same names, the measures without verdict words, no «= <», the headers that
  fit their columns, the readings said with no `@`, `{` or `%2` left.

### What B0 added (verified by its tests)

- `common.READINGS`: `model → readings(rows, checked) → [(block, level_2, id, {name: value})]`. A model's
  own file registers its function (`c.READINGS["linear"] = readings`); `with_labels` appends the lines as
  rows `reading|<block>|<level_2>|line_<k>` = `@<id> name=value…` after the runner's rows (`ord` after the
  last; `common.reading_rows`, `common.encoded`: a whole number «12», any other as Python writes it back
  exactly, «0.05», «1e-300»; a name encoded; a list joined by «,»; a value `None` leaves its name out, so
  the tests see the `{name}` left). Then they are said like the A/B's.
- `common.page(…, explained=("summary", "assumptions", "coefficients", "fit"))`: a reading under each
  named block of the page every model shares, and «How this report was computed» before the notes when
  any is named. `explained=()` (the default) draws the page as before, item for item. A model's own
  blocks place their readings themselves (`c.reading_under(p, w, "<block>")` in its own file).
- `common.json`: `sections.how`, `reading` (`lead`, `title`, `part`, `text`, `variant` = «Group»,
  `how_lead`), `verdict_words` (the union of the A/B's and the decomposition's lists, per language;
  a model's own list, when it has one, replaces it).
- `backend/tests/test_report_explanations.py`: the shared pieces, and **the criteria below run over every
  model in `READINGS`**: a lot that explains a model is checked by registering it, without editing the
  test. `test_models_layouts.EXPLAINED` includes `READINGS` (twelve pages allowed, no «= <»).

## The rules of the words

1. **A verdict only from a test**: a sentence may say «significant», «rejected», «not rejected», «the data
   show / do not show» only when it is chosen by a p-value (or an adjusted p-value) compared with the
   node's α — the statistics test's `decision` row, a model's check whose row has a `p_value`, a
   coefficient's `p_value`. The layout makes the choice from those rows; the sentence says the p-value
   and α.
2. **Not rejecting is not proving**: «the data do not show a difference; they do not prove there is none».
3. **A measure is a number**: an estimate, an interval, an effect size (Cohen's d, Cramér's V, η²…), an
   R², an AUC, a concordance, a share, a median. Its sentence states the number and what it measures
   («the difference is 2.39 standard deviations»), never «large», «small», «good», «strong». No
   conventional cut-off (Cohen's 0.2 / 0.5 / 0.8, «an AUC above 0.8 is good») is ever said.
4. **Threshold checks are measures** (a VIF, Cook's distance, an EPV, a boosting diagnostic): the reading
   says the number and the threshold the page uses, «above / below the threshold of 10», never «failed».
   The checks' table keeps its verdict column as it is (Open question 3). (Correction of 2026-11-24 from the step-5 specs: **except `bayes_model`**,
   whose report compares no number of the run with a threshold at all — answer 11 of
   `bayesian-editor-map.md` —: the literature's reference values appear only as quotations, in English as their source
   writes them, in the method's paragraphs, never «above / below».)
5. **Many rows**: when a model's `n` is above `MANY_ROWS` (5,000) the assumptions' reading says in words
   what the page already says in amber: with this many rows a test rejects departures too small to
   matter.
6. **No «= <»**: a p-value's form `{p:p}` may say «< 0.0001», so a template writes «with probability
   {p:p}», never «= {p:p}»; the tests refuse «=» before a `:p` name.
7. **Names stay**: a column's, a group's, a level's name is a value the template uses without a number
   form (or one of `NOTE_NAMES`), never put in a form.
8. **A list is capped**: a reading that names terms, pairs or strata names at most five, in the order the
   table sorts them, and says how many more («and 3 more»). Counting rows by their verdict and sorting
   them is selection, not computation; no number is derived.

## The statistics tests (lot B1)

**Where the sentences are chosen** (`backend/app/engine/stats/layouts.py`, `with_labels`): a function
of the layout reads the test's rows — `decision` (reject_h0 / fail_to_reject_h0: already the p-value
against α), `alternative`, `variant`, the numbers of the section `test`, the groups' rows, the pairs'
`decision` — and appends rows of the section `reading` as above. **The statistics engine
(`compute.py`) does not change**: its rows are the node's output table, which the next steps read and
the four engines must write identically. `with_labels(test, rows, locale)` keeps its signature (the
rows hold everything the choice needs). The text helpers come from `models/layouts/common.py`, imported
inside the functions (as `formats` is): `common.py` imports `stats/layouts.py`, so an import at the top
would be circular.

The columns of a contingency table are not in its rows (only their categories), so its sentences say «the two
columns»; the header names them.

**Where they are placed** (`_page`): `result` under the decision sentence; `pairs` under the
comparisons' tables (a test with a post-hoc); «How this report was computed» after the charts and before
the notes, with the `method` paragraphs only (no `formulas` block: the existing «How it is computed» is
the formula box, Open question 2).

**The blocks and their lines.** The values come from the rows; `alpha`, `conf` (conf_level), `p`
(p_value) and `n` are in every line that needs them.

| Block | Lines (in order) | Kind |
|---|---|---|
| `result` | 1. the **estimate** in words: `<test>_estimate` with its interval when the alternative is two-sided, `<test>_estimate_one_sided` without it (the card says the one-sided interval) | measure |
| | 2. the **p-value read**: two-sided, `<test>_reject` / `<test>_keep` with H0 in plain words — «If ⟨H0⟩, a result at least this far from it would appear by chance with probability {p:p}, below α = {alpha:num}: ⟨the verdict⟩.» / «…not below α = …: the data do not show ⟨…⟩; they do not prove there is none.»; one-sided, `one_sided_reject` / `one_sided_keep`, which name H1 as the page writes it (`h1`) | verdict |
| | 3. the **effect size** when the test writes one (`effect_<name>`) | measure |
| `pairs` | `pairs_some` / `pairs_some_more` (k, total, the pairs ≤ 5, more, alpha) / `pairs_none` (total, alpha); `cells_*` the same for the cells of a contingency table — by each pair's or cell's `decision` (its adjusted p against α) | verdict |
| `method` | `method_<test>` (or `method_<test>_<variant>` where the variant changes the method), `method_p_value` (what a p-value and α are; «not rejecting is not proving»), `method_one_sided` for a one-sided alternative, `method_exact` for an exact p-value, `method_posthoc_<method>` when comparisons ran | — |

| Test (variants) | `result`: the estimate line and its values | H0 in plain words (verdict line) | Effect line | Method paragraph |
|---|---|---|---|---|
| `t_one_sample` | the mean of «column» {estimate:num} against {null_value:num}, interval | the mean of «column» were {mu} | `effect_cohen_d` | one-sample t: a normal mean or many rows, independent rows |
| `t_two_sample` (welch, student) | the two groups' means (the first and last `group` rows), the difference {estimate:num}, interval | the two groups had the same mean (or differed by mu) | `effect_cohen_d` (+ hedges_g) | Welch: unequal variances, its df; Student: pooled variance |
| `t_paired` | the mean of the differences, interval | the mean difference were {mu} | `effect_cohen_dz` | paired: the differences of the same rows |
| `anova` (one_way, welch) | k groups, the largest and the smallest mean (names and values from the group rows) — a description, not a test of a pair | all k means were equal | `effect_eta_squared` (+ omega²) | one-way / Welch ANOVA; then `method_posthoc_tukey|games_howell|pairwise_t` |
| `var_f` | the ratio of the variances, interval | the variances were in the ratio {ratio} | — | F test of two variances: sensitive to non-normality |
| `levene` (mean, median) | k groups, the largest and smallest mean absolute deviation | the groups had the same spread | — | Levene / Brown–Forsythe |
| `pearson` | r, interval | there were no linear association | `effect_r_squared` | Pearson: linear, sensitive to outliers |
| `spearman` | ρ | there were no monotonic association | `effect_rho_squared` | ranks, monotonic |
| `prop_one_sample` (score, exact) | {successes:int} of {n:int}, the proportion, interval, against p0 | the proportion were {mu:pct} | `effect_cohen_h` | score test / exact binomial |
| `prop_two_sample` | the two proportions, the difference, interval | the two proportions were equal | `effect_cohen_h` | score test of two proportions, continuity correction |
| `chi2_independence` (+ cells, pairs) | an r × c table of n rows | the two columns were independent | `effect_cramers_v` | χ² of independence, the expected counts; then the post-hoc's |
| `g_test` (plain, williams) | the same | the same | `effect_cramers_v` | likelihood ratio G, Williams' correction |
| `chi2_gof` (equal, given) | k categories of n rows, against equal or given shares | the shares were the expected ones | `effect_cohen_w` | goodness of fit |
| `fisher_exact` (2 × 2, larger) | 2 × 2: the odds ratio, interval; larger: the table's size | the two columns were independent | — | the exact conditional test; `method_exact` |
| `mcnemar` (chi_square, exact) | {discordant:int} discordant pairs (b, c), the difference of the proportions, interval | the two kinds of change were equally likely | — | McNemar: only the discordant pairs count |
| `cmh` | K strata, the common odds ratio, interval (2 × 2) | the two columns were independent within every stratum | — | Cochran–Mantel–Haenszel |
| `mann_whitney` (asymptotic, exact) | the two groups' medians (from the group rows) | the two distributions were the same | `effect_rank_biserial` (+ cles) | ranks; what it compares (not the medians in general) |
| `wilcoxon_signed_rank` (paired, one sample) | the median of the differences (or of the column, against mu) | the differences were symmetric around {mu} | `effect_rank_biserial` | signed ranks, the zeros (Wilcoxon / Pratt) |
| `kruskal_wallis` (+ Dunn) | k groups, the largest and smallest mean rank | the k distributions were the same | `effect_epsilon_squared` | ranks of several groups; `method_posthoc_dunn` |
| `ks_two_sample` (asymptotic, exact) | D: the largest gap between the two cumulative distributions | the two distributions were the same | — | Kolmogorov–Smirnov, two samples |
| `normality` (dagostino, jarque_bera) | the skewness and the kurtosis | «column» came from a normal distribution | — | D'Agostino–Pearson / Jarque–Bera; with many rows, small departures |

**Words**: `report_texts.json`, per language: `reading` (lead, how_lead, title, part, text),
`sections.how`, `reading_texts` (every id above), `reading_labels` (the `method_*` ids), `reading_blocks`
(`result`, `pairs`, `method`), `measure_readings` (every estimate and effect id), `verdict_words` (the
same list as `common.json`'s). **Excel**: the sheet «Reading» (the method); the result's reading is a
text, not a sheet, as the A/B's readings of the whole experiment.

## The models (lots B2 and B3)

**Where the sentences are chosen, for every model: in the layout**, by the function each model's file
registers in `common.READINGS`, from the rows its runner already wrote. **No R or Python runner changes,
so no image is rebuilt** (the column «Runner» says so for each). α is the node's `alpha`
(`checked["alpha"]`, the catalogue's default 0.05 filled in), the α the runner compared the checks with.

**Where they are placed**: `common.page(…, explained=…)` for the shared blocks; a model's own blocks
call `c.reading_under` in its own file; «How this report was computed» before the notes.

**The lines every model shares** (each lot writes them in its own models' words, Open question 4):

| Block | Lines | Kind |
|---|---|---|
| `assumptions` | `assumptions_rejected` (count and names ≤ 5 of the checks **with a p-value** whose verdict is `failed`, alpha) / `assumptions_none_rejected` (the count of tests, alpha); `assumptions_thresholds` (the checks **without a p-value** past their threshold, their values and thresholds); `assumptions_many_rows` (n) | verdict / measure |
| `coefficients` | `coef_some` (the count of terms with p < α, the total, names ≤ 5 by p, more, alpha) / `coef_none` | verdict |

### B2: regressions and survival

| Model | Blocks with a reading and their lines (values from the rows) | «How this report was computed»: paragraphs; formulas | Verdicts chosen from | Runner |
|---|---|---|---|---|
| `linear` (OLS, WLS) | `summary`: `summary_linear` (n, outcome, r_squared:pct, adj_r_squared:pct) measure, `fit_f_reject|keep` (f_statistic, f_df_1, f_df_2, f_p_value, alpha); `assumptions`; `coefficients` (+ `coef_robust` when `se_type` is not the classic one); `fit`: `fit_errors` (rmse, mae, sigma) measure | `method_ols|wls`, `method_se_<type>`, `method_checks`, `method_vif`; `formula_ols`, `formula_r2`, `formula_f`, `formula_t`, `formula_sandwich`, `formula_vif` | `f_p_value`, the checks' and the terms' `p_value` | unchanged |
| `linear` and `logistic` with a penalty (`penalized.py`) | `summary`: `summary_penalized` (penalty, lambda, the rule min / 1se, n_selected, cv_value with cv_se) measure; no verdict anywhere (no p-value exists) | `method_ridge|lasso|elastic_net`, `method_cv`, `method_no_inference`; `formula_penalized`, `formula_cv` | none | unchanged |
| `logistic` | `summary`: `summary_logistic` (n, events, auc with its interval, mcfadden_r2, brier) measure, `fit_lr_reject|keep` (lr_statistic, lr_df, lr_p_value); `assumptions`; `coefficients` with `coef_odds` (the first significant term's odds ratio and its interval) measure; `confusion` (own block): `confusion_at` (threshold, accuracy, sensitivity, specificity, precision) measure | `method_logit|probit|firth`, `method_auc`, `method_threshold`; `formula_logit`, `formula_probit`, `formula_lr`, `formula_wald`, `formula_auc`, `formula_brier` | `lr_p_value`, checks, terms | unchanged |
| `kaplan_meier` | `summary`: `summary_km` (strata, n, events, censored) measure; `curves` (own): `km_medians` (≤ 5 strata: the median or «not reached», its interval) measure, `km_test_reject|keep` (log-rank or Peto, test_statistic, test_df, test_p_value) with two strata or more; `pairs` (own, with `pairwise`): `pairs_some|none` by `p_adjusted`; `rmst` (tau, ≤ 5 strata) measure | `method_km`, `method_censoring`, `method_logrank|peto`, `method_rmst`; `formula_km`, `formula_greenwood`, `formula_logrank`, `formula_rmst` | `test_p_value`, the pairs' `p_adjusted` | unchanged |
| `cox` | `summary`: `summary_cox` (n, events, concordance with its se) measure, `fit_lr_reject|keep`; `assumptions` (Schoenfeld's global test is one of them); `coefficients` with `coef_hazard` (the first significant term's hazard ratio and interval: «each unit of load multiplies the hazard by 1.12») measure | `method_cox`, `method_ties_<ties>`, `method_schoenfeld`; `formula_cox`, `formula_partial_likelihood`, `formula_hr`, `formula_schoenfeld` | `lr_p_value`, checks, terms | unchanged |
| `parametric_survival` (AFT, PH, auto) | `summary`: `summary_parametric` (the distribution in words, the profile's median with its interval) measure; with `auto`: `auto_chosen` (distribution, akaike_weight, the next one's ΔAIC) measure; `fit_lr_reject|keep`; `assumptions` (`distribution_vs_gengamma` and `ph_cox` are tests); `coefficients` with `coef_time_ratio` (AFT) or `coef_hazard` (PH) measure | `method_aft|ph`, `method_<distribution>`, `method_auto` (AIC, Akaike weights), `method_km_agreement`; `formula_aft`, `formula_ph`, `formula_aic`, `formula_akaike_weight` | `lr_p_value`, checks, terms | unchanged |

### B3: time series, trees, customer lifetime

| Model | Blocks with a reading and their lines | «How this report was computed» | Verdicts chosen from | Runner |
|---|---|---|---|---|
| `arima` (manual, auto) | `summary`: `summary_arima` (p d q, P D Q s, n, transform) measure; with `orders = auto`: `auto_orders` (criterion, candidates) — a choice by an information criterion, said as a choice, not a verdict; `stationarity` (own): the four cases of ADF and KPSS by their verdicts (the decomposition's `stationarity_*` words); `assumptions` (Ljung–Box, ARCH, Jarque–Bera are tests, `roots` a threshold); `forecast` (own): `forecast_horizon` (horizon, the 80 % and 95 % bands) measure, `holdout_errors` (holdout, mape:pct, mase) measure | `method_arima`, `method_differencing`, `method_auto`, `method_transform_<t>`, `method_holdout`; `formula_arima`, `formula_aicc`, `formula_ljung_box`, `formula_mape`, `formula_mase` | ADF, KPSS (D7's rule at the table's ends), Ljung–Box, ARCH, Jarque–Bera | unchanged |
| `xgboost`, `catboost` (`boosting.py`; regression, binary, multiclass) | `summary`: `summary_boosting_<task>` (n_train, n_validation, trees, best_iteration; the validation metric beside the baseline's: rmse and baseline_rmse, logloss and baseline_logloss, mlogloss and baseline_mlogloss) measure; `assumptions`: `diagnostics_thresholds` (the diagnostics past their threshold) measure — **no verdict sentence anywhere** (no diagnostic is a test); `importance` (own): `importance_top` (the first three by their share of mean \|SHAP\|) measure | `method_boosting`, `method_early_stopping`, `method_validation`, `method_shap`; `formula_boosting`, `formula_shap`, the task's loss | none | unchanged |
| `clv`, `bg_bb` (`customer_lifetime.py`) | `summary`: `summary_clv` (customers, the repeaters' share, the purchase model; with `auto` the one chosen and by what, a choice) measure; `assumptions`: `frequency_fit_reject|keep` (χ², df, p) and `spend_independence_reject|keep` are tests, the rest measures; `fit`: `holdout_compare` (observed and expected, the relative error) measure; `clv` (own): `clv_totals` (total, mean, median over the horizon, the discount) measure; `p_alive` (own): `p_alive_mean` measure | `method_pareto_nbd|bg_nbd|mbg_nbd|bg_bb`, `method_gamma_gamma`, `method_discount`, `method_holdout`; `formula_<purchase model>`, `formula_p_alive`, `formula_gamma_gamma`, `formula_clv` | `frequency_fit`, `spend_independence` | unchanged |

## The lots

```
B0 (done): the move to common.py, READINGS, page(explained), common.json's words, the criteria's tests, this spec
   ├── B1 statistics tests ──────────┐
   ├── B2 regressions, survival ─────┼── B4 final review, docs, kind
   └── B3 ARIMA, trees, CLV ─────────┘
```

| Lot | Files it owns (no other lot touches them) | Depends on | Heavy | Model |
|---|---|---|---|---|
| **B0** | `layouts/common.py`, `layouts/experiments.py`, `ab_test.py`, `ab_plan.py`, `decomposition.py`, `layouts/__init__.py` (docstring), `report_texts/common.json`, `tests/test_report_explanations.py`, `tests/test_models_layouts.py` (`EXPLAINED`), this spec | — | golden, layouts suites | opus |
| **B1** | `backend/app/engine/stats/layouts.py`, `backend/app/engine/stats/report_texts.json`, `backend/tests/test_stats_layouts.py` (the reference PDF's hash; the reads-only test learns the `reading` section), `backend/tests/test_stats_explanations.py` (new) | B0 | statistics suites | sonnet (layout), **opus** (the texts) |
| **B2** | `layouts/{linear,logistic,penalized,kaplan_meier,cox,parametric_survival}.py`, `report_texts/{linear,logistic,penalized,kaplan_meier,cox,parametric_survival}.json`, `backend/tests/test_explanations_regression_survival.py` (new) | B0 | models suites | sonnet, **opus** (the texts) |
| **B3** | `layouts/{arima,boosting,xgboost,catboost,customer_lifetime,clv,bg_bb}.py`, `report_texts/{arima,boosting,xgboost,catboost,customer_lifetime,clv,bg_bb}.json`, `backend/tests/test_explanations_ts_trees_clv.py` (new) | B0 | models suites | sonnet, **opus** (the texts) |
| **B4** | `docs/capabilities/{statistics,models}.md` (a line each), `CHANGELOG.md`, this spec's «As built», the corrections found | B1–B3 | engine suite (`ci_tests.sh engine`), kind | opus |

**Shared files and the rule.** Frozen during B1–B3: `layouts/common.py`, `layouts/__init__.py`,
`layouts/experiments.py`, `report_texts/common.json`, `report_texts/experiments.json`,
`report_texts/decomposition.json`, `tests/test_models_layouts.py`, `tests/test_report_explanations.py`,
the catalogue (`models/catalog.*`, the gateway's and the frontend's copies, `params_cases.json`), every
runner, the statistics engine (`stats/compute.py`, `stats/catalog.json`). A lot that needs one of them
changed leaves that part, writes the request in its «As built» and goes on with the rest; the change is
made once, in a short serial step (B4, or before it by whoever holds B0's files), with the golden
comparison before and after. The tests of the criteria pick a model up from `READINGS`: no lot edits
them. Heavy jobs (the engine suites, the golden dumps, any render of every fixture) run one at a time
under the machine's lock: three lanes in parallel write code and words, not suites.

## Criteria (every lot)

1. **Every fixture renders in the five languages** (`test_models_layouts.test_every_report_renders…`
   for the models, twelve pages at most for an explained one; `test_stats_layouts` for every variant
   of every test) and passes `reports.definition.check`.
2. **Every id has its five texts with the same names and number forms** (`test_report_explanations`;
   the statistics' file in `test_stats_explanations`), and every `method_*` / `formula_*` id its label.
3. **No measure holds a verdict word**: `measure_readings` declared per model (per test), checked
   against `verdict_words` in five languages; and each verdict id holds one (the list catches it).
4. **No «= <»**: no template writes «=» before a `:p` name; no reading said on a fixture holds «= <».
5. **Headers do not overlap**: every word of a header of the explanation's tables fits its column in
   bold (the engine's measure, `CELL_PAD`).
6. **The other reports change only by what is added**: `test_report_explanations.only_additions` —
   every item of the page without its explanation is on the page with it, in the same order, the same
   but for its place; every labelled row is as before. **And by sha256**: each lot renders every fixture
   of every model and every variant of every test in the five languages before and after (the PDF, the
   definition, the labelled rows; the fake chart service) and writes in its «As built» that only its own
   reports changed.
7. **Every reading said on a fixture has no `@`, `{` or `%2` left** and a label that is not its raw stat.
8. **A verdict follows α**: per model (per family of tests), the same fixture with `alpha` just above
   and just below the p-value reads «rejected» and «not rejected».

## Open questions

1. **Who chooses the sentences: the runner or the layout?** The A/B and the decomposition write their
   readings in R. For the existing models that would change ten runners (R and Python), rebuild their
   images and rerun their self-tests; for the statistics tests it would change the engine's output, which
   is the node's table and must be the same on four engines. Everything the choice needs (the verdicts,
   the p-values, α, the numbers, the options) is already in the rows. **Recommendation: the layout.**
   **Decided by the agent, to confirm.** Consequence: a sentence that would need a number no row holds is
   not written (the lot asks for it in its «As built»; the layout never computes it).
2. **The statistics tests' formula box**: their page already shows the formula with the run's numbers,
   its symbols, the distribution and the critical region («How it is computed»). A second box on the
   last page would say it twice. **Recommendation: no `formulas` block for the tests; the last page holds
   the method's paragraphs, the first of which points to «How it is computed».** **Decided by the agent,
   to confirm.**
3. **The models' checks table says «failed» for threshold checks** (VIF > 10, Cook's distance, EPV,
   every boosting diagnostic), which the owner's rule of 2026-11-20 would call a measure. Changing the
   table changes every model's first page and is not this piece. **Recommendation: the readings follow
   the rule (the number and the threshold), the table stays; a later piece decides whether its verdict
   column for checks without a p-value becomes «above / below the threshold».** **Decided by the agent,
   to confirm.**
4. **The lines every model shares** (`assumptions_*`, `coef_*`, `method_p_value`): in `common.json` they
   would be written once, but `common.json` is shared by B2 and B3, which run in parallel.
   **Recommendation: each lot writes them in its models' words (B2 in `linear.json`…, B3 in
   `arima.json`…) with the same text; B4 moves the identical ones to `common.json` in one serial step,
   with the golden comparison.** **Decided by the agent, to confirm.**
5. **Lists of terms, pairs, strata**: a reading could name every significant term. **Recommendation: at
   most five, in the table's order, «and k more»** (the table has them all). **Decided by the agent, to
   confirm.**
6. **The effect sizes' conventional labels** («a large effect», Cohen 1988) would be verdicts without a
   test. **Recommendation: never said; the number and what it measures only.** **Decided by the agent,
   to confirm.**
7. **Penalized regressions and boosting have no test at all**: their readings are measures only, and
   their method paragraph says why no p-value is given (inference after a selection; trees). **Decided by
   the agent, to confirm.**

## As built

### B0 (2026-11-22)

**The move** (`148d22d0`). The explanation's pieces (`reading_of`, `reading_under`, `reading_table`,
`how_computed`, `READING_LINES`, `FORMULA_PANEL`, `FORMULA_RULE`) and the small expressions they and the
decomposition use (`key_part`, `pct`, `signed_pct`, `p_with_sign`, `model_number`, `model_text`,
`document`) moved from `experiments.py` to `common.py`; `ab_test.py` and `ab_plan.py` call them as `c.…`;
`decomposition.py` no longer imports `experiments.py` (its Open question 12).

**The hooks, the words, the tests** (`2e5a8310`): `READINGS`, `ReadingLine`, `encoded`,
`reading_rows`, `EXPLAINED_BLOCKS`, `page(explained=…)` (an unknown place is refused); `with_labels` adds
the rows a registered model chooses; `common.json`'s `sections.how`, `reading`, `verdict_words`;
`tests/test_report_explanations.py` (the shared pieces, a toy explanation of the linear regression
that goes through `only_additions`, and the criteria over `READINGS`); `EXPLAINED` in
`test_models_layouts.py`.

**Golden.** A scratch test (not committed) renders every model fixture (32) and every variant of every
statistics test (68) in the five languages — 500 renderings — and writes the sha256 of the PDF (the fake
chart service), of the definition and of the labelled rows. HEAD `01f953d2` against `148d22d0`: **500 of 500
identical**; against `2e5a8310`: **500 of 500 identical** (the new words in `common.json` reach no page
until a model registers its readings).

**Tests** (the CI image, on clean copies of each commit, the other lane's uncommitted files left out):
`test_models_layouts`, `test_models_ab_review`, `test_reports_model` **742 passed** on the move; with
`test_report_explanations` (37) and `test_stats_layouts`, **1,071 passed** on the hooks. The test of «only
additions» was itself checked: on the toy explanation it caught the charts' invisible rows renamed by their
place (`row_<n>_keep`), which it now reads as the same item.

### B1 — the statistics tests (2026-11-22)

**Files.** `backend/app/engine/stats/layouts.py`: `readings(test, rows)` chooses the lines from the test's rows (its
`decision`, `alternative`, `variant`, numbers, groups, pairs' and cells' decisions); `with_labels` appends them as rows
of the section `reading`, said through `models/layouts/common.py`'s `reading_rows` and `reading_said` (imported inside
the function: `common.py` imports this module); `_page` places `reading_under(…, "result")` under the decision,
`reading_under(…, "pairs")` after the comparisons and `how_computed` before the notes. `READING_ROWS = 16` is added to
`MAX_REPORT_ROWS` (23,383, was 23,367): the rows a report reads leave room for the explanation. `report_texts.json`:
in each language `sections.how`, `reading`, `reading_blocks`, `reading_labels` (34), `reading_texts` (126 ids),
`measure_readings` (42: every estimate and effect), `verdict_words` (`common.json`'s). Tests:
`backend/tests/test_stats_explanations.py` (new) and `test_stats_layouts.py` (the reference PDF's hash; the
reference report stays on two pages; the sheet «Reading» in Excel, the reads-only test accepts the eight places of a reading).

**Template ids.** Estimates: `<test>_estimate` and `<test>_estimate_one_sided` for the tests with an interval
(t one sample, two samples, paired, var_f, pearson, the two proportions, Fisher 2 × 2), `contingency_estimate`
(χ², G, Fisher larger), `chi2_gof_estimate`, `anova|levene|kruskal_wallis_estimate` (the groups with the largest and
the smallest mean, mean distance, mean rank: chosen among the groups' rows), `mcnemar_estimate(_larger)`,
`cmh_estimate(_larger)`, `spearman_estimate`, `mann_whitney_estimate`, `wilcoxon_signed_rank_estimate`,
`ks_two_sample_estimate`, `normality_estimate`. Verdicts: `<test>_reject|keep` for each of the 21 tests (two-sided,
H0 in plain words: a contingency table's columns are not in its rows, so they are «the two columns»),
`one_sided_reject|keep` (H1 as the page writes it). Effects: `effect_cohen_d_one|two`, `effect_cohen_dz`,
`effect_eta_squared`, `effect_r_squared`, `effect_rho_squared`, `effect_cohen_h`, `effect_cramers_v`, `effect_cohen_w`,
`effect_rank_biserial_two|paired`, `effect_epsilon_squared`. Comparisons: `pairs_some|some_more|none`,
`cells_some|some_more|none`. Method: one paragraph per test (per variant for t two samples, ANOVA, one proportion,
normality), `method_p_value`, `method_one_sided`, `method_exact`, `method_posthoc_tukey|games_howell|pairwise_t|dunn|cells|pairs`.

**Deviations.** «Success» is a verdict word: the proportion tests' measures say «the value counted», not «a
success». Two method labels were shortened to fit the 40 mm column «Part» (`Mantel–Haenszel (CMH)`, de
`Kolmogorov–Smirnov`); a test now checks every label's words against that column. The Spanish texts say «valor p»,
as the page's card.
After the first review of the sentences on the reference data: the normality test's verdict says «excess kurtosis»
(the number its estimate gives, 0 for a normal distribution), and the German one-proportion verdict names its noun.

**Golden** (the scratch dump of B0, on clean copies of `db954510` and of this lot): the 160 renderings of the model
fixtures are **byte for byte as before**; the 340 of the statistics tests all change (the reading under the decision,
«How this report was computed»), none with a warning; 248 stay on two pages, 52 go from two to three, 40 were and
stay on three. The reference report (Welch, English) stays on two pages: its hash is the new
`GOLDEN_PDF_SHA256`, drawn with the fake chart service (to be seen with the real one, as the test's comment asks).

**Tests** (the CI image, the other lane's uncommitted files left out): `test_stats_explanations` (20),
`test_stats_layouts`, `test_report_explanations`, `test_models_layouts`, `test_reports_model`,
`test_models_ab_review`: **1,091 passed**. The verdict follows α (the same data at an α just above and just below
the p-value), a one-sided test names its H1, a name that looks like a template stays as written, the rows a report
reads leave room for the explanation, the reads-only test accepts the reading's places.

### B2 — regressions and survival (2026-11-22)

**Files.** Each model's file registers its `readings(rows, checked)` in `common.READINGS` and names its explained blocks
in `page(explained=…)`: `linear.py` (summary, assumptions, coefficients, fit), `penalized.py` (summary, assumptions;
called by `linear.readings` and `logistic.readings` when a penalty is on), `logistic.py` (summary, assumptions,
coefficients; its own block `confusion` places its reading under the confusion table), `kaplan_meier.py` (summary,
assumptions; its own blocks place `curves` under the number at risk, `rmst` under the restricted means, `pairs` under the
pairs), `cox.py` and `parametric_survival.py` (summary, assumptions, coefficients). The lines the five share are in a new
file of this lot, `layouts/shared_readings.py`: the checks (`assumptions_rejected` / `assumptions_none_rejected` by each
check's p-value against α, `threshold_<check>` for a check without a p-value that the table marks past its threshold,
`assumptions_many_rows`), the terms (`coef_some` / `coef_all` / `coef_none` by each term's p-value, the intercept left
out; five names at most by p-value, then `more_in_table`), the first significant term's ratio with its interval at
1 − α (`coef_odds`, `coef_hazard`, `coef_time_ratio`, and `_level` for a level against the reference; an interaction is
skipped). The words: in each of the six `report_texts` files `reading_blocks`, `reading_labels`, `reading_texts`,
`formula_texts`, `measure_readings`; the shared lines written in each model's file with the same text (Open question 4).
Test: `backend/tests/test_explanations_regression_survival.py` (new).

**Verdicts** come from `f_p_value`, `lr_p_value`, `test_p_value`, the pairs' `p_adjusted`, the checks' and the terms'
`p_value`, each compared with the node's α by the layout (so the same fixture at an α just above and just below a p-value
reads both ways, criterion 8). A check that has a p-value is read by it even when the runner's table marks it
«doubtful» rather than «failed» (Kaplan–Meier's censoring by stratum). The thresholds (VIF, condition number, Cook's
distance, leverage, deviance residuals, dfbeta, events per coefficient, rows at risk) are said as «above / below the
threshold of …, a convention of the literature, not a test»; the table keeps its verdict column (Open question 3).
A penalized fit has no verdict anywhere; its method says why no p-value is given.

**Deviations and requests for the serial step (B4).**
- The checks rejected are **counted, not named** («4 of 9, marked in the table»): the layout does not know the page's
  language, and naming a check in it needs `structured_note` to translate an id the readings choose. Request: in
  `common.structured_note`, add `values.reading_names` to the names it translates (one line); then
  `assumptions_rejected` can list `check:<id>` names, as the spec's table asks.
- The penalized fit names its cross-validation measure in a sentence of its own, chosen by the node's `measure`
  (`penalized_measure`, `by_params`), for the same reason.
- `tests/test_report_explanations.py` (frozen during B1–B3) has two tests that take the linear regression as the model
  *without* an explanation and fail now that it has one: `test_a_page_with_nothing_explained_is_the_page_as_before…`
  (it should remove `READINGS["linear"]` and force `explained=()` with `monkeypatch`, as `only_additions` does) and
  `test_a_model_that_registers_its_readings…` (its toy words should also clear `reading_labels`, which now name
  `method_ols`). Every other test of that file passes over the five models.
- `tests/test_stats_layouts.py::test_a_model_report_keeps_its_headings_and_the_coefficients_note_with_their_tables`
  (B1's, on the parametric survival) refuses `keep_with_next` on any item whose id ends with «_how» (the charts'
  captions): the heading of «How this report was computed», `s_how`, matches and keeps with its lead as every heading
  does. Request: leave `s_how` out of that check (or name the captions' suffix more narrowly).
- With several strata the medians are listed without their intervals (the table beside them has them); with one,
  the median is said with its interval.

**Golden** (the scratch dump of B0, on clean copies of `f4159f67` and of `f4159f67` with this lot's files): **450 of 500
renderings byte for byte as before** (PDF, definition, labelled rows) — every other model's fixture and every variant of
every statistics test; the 50 that change are exactly this lot's ten fixtures in five languages, none with a warning,
all within twelve pages (three to six; 30 gain a page: the readings and «How this report was computed»).

**Tests** (the CI image, `--memory=2500m --cpus=1`, on the clean copy with this lot):
`test_explanations_regression_survival` (new), `test_report_explanations`, `test_models_layouts`, `test_reports_model`,
`test_models_ab_review`, `test_stats_layouts`, `test_stats_explanations`: **1,185 passed, 3 failed** — the three tests
of frozen files above, which assume the linear regression and the parametric survival have no explanation.

### B3 — time series, trees, customer lifetime (2026-11-22)

**Files.** `layouts/arima.py`: `readings` (in `READINGS`) and `table_rejects` (ADF and KPSS at the ends of tseries'
tables, read by the side as the runner reads them, lot D7); a reading under `summary`, `assumptions`, `coefficients`
(`page(explained=…)`) and under its own blocks `stationarity` (after the four cases) and `forecast` (after the holdout's
line). `layouts/boosting.py`: `readings`, registered by `xgboost.py` and `catboost.py`; under `summary`, `assumptions`
and its own `importance`. `layouts/customer_lifetime.py`: `readings`, registered by `clv.py` and `bg_bb.py`; under
`summary`, `assumptions` and its own `fit_data`, `p_alive` and `clv`. Words: `arima.json`; `boosting.json` (each
library's `method_library` in `xgboost.json` and `catboost.json`); `customer_lifetime.json` (the summary and the purchase
models' paragraphs and formulas in `clv.json` and `bg_bb.json`). Test: `backend/tests/test_explanations_ts_trees_clv.py`
(new, 19). No runner, no shared file and not `shared_readings.py` changed (its check lines name a regression's checks;
ARIMA's residual tests and the customer lifetime's two tests are read by their own ids).

**Template ids.** ARIMA — summary: `summary_arima|sarima` (the orders, n), `summary_transform_log|box_cox`,
`summary_exog(_more)`, `auto_orders` (by the node's criterion: a ranking, not a test); stationarity:
`adf_reject|reject_end|keep|keep_end|table_end`, the same for `kpss`, then the case `stationarity_both_ok|adf_only|
kpss_only|neither` (the decomposition's ids and words), `stationarity_no_case`, `stationarity_short`; assumptions:
`<check>_reject` for each residual test whose p-value is below α (Ljung–Box 10 and 2s, ARCH, the variance ratio,
Jarque–Bera), `residuals_keep_all|keep_others`, `roots_threshold`, `assumptions_many_rows`; coefficients:
`coef_one|some|some_more|none`; forecast: `forecast_horizon` (the bands 80 % and 1 − α), `holdout_errors` (MAPE, MASE),
`sample_errors` without a holdout. Method: `method_arima|seasonal|differencing|transform_log|transform_box_cox|exog|auto|
stationarity|checks|holdout|bands`; formulas: `formula_arima|sarima|aicc|box_cox|adf|kpss|ljung_box|mape|mase` (ADF's
regression with trend, as `tseries::adf.test` fits it; both p-values from the tables of Banerjee et al. and
Kwiatkowski et al.). Boosting — `summary_boosting_<task>` (the parts, the trees, the validation loss beside the
baseline's, R², AUC or accuracy); `diag_<check>` for each diagnostic past its threshold, its number and the threshold
(`diag_trees_more|few|no_stop|size|time`, `diag_stop_on_loss` by the advice the runner wrote, `diag_leakage_share|fit|
one`, `diag_zero_importance(_more)`), `diagnostics_more`, `diagnostics_none`; `importance_top|one` (the first three by
their share of mean |SHAP|). Method: `method_boosting|library|validation_<random|time|grouped>|early_stopping|baseline|
class_weight|threshold|shap|no_test`; formulas: `formula_boosting|loss_<task>|baseline|shap`. Customer lifetime —
`summary_clv` (each model's own words), `summary_auto`, `summary_spend`; `frequency_fit_reject|keep`,
`spend_independence_reject|keep` (the only verdicts); `convergence_edge|not_reached`, `spend_tail_fact` (facts),
`horizon_beyond`, `assumptions_many_rows` (measures); `holdout_compare|none`; `p_alive_mean`;
`clv_totals(_one)`, `transactions_total(_one)` (a unit after 1 in the singular). Method: `method_<purchase model>|auto|
p_alive|gamma_gamma|clv|holdout|tests|tests_purchases`; formulas: `formula_<purchase model>`,
`formula_p_alive_<purchase model>`, `formula_chi_square|gamma_gamma|clv`.

**Choices.** The verdict is chosen by the row's p-value against the node's α (ADF and KPSS by the side of their table,
as the runner does), not by the row's verdict: the same as the runner's at the node's α, and it lets the test read
«rejected» and «not rejected» around the p-value (criterion 8). Where the table cannot say (α beyond its end, the
runner's «not computed»), the sentence is the decomposition's `kpss_table_end` («the test gives no verdict») and the
four cases are not read. Every boosting line is a measure (listed in `measure_readings`): a diagnostic says its number
and the threshold the page uses, «below / above the threshold of …», never «doubtful» or «failed»; `method_no_test` says
why no p-value is given. The customer lifetime keeps its runner's distinction: the χ² of the repeat purchases and the
spend's slope are the only verdicts; convergence and the spend's tail are facts, the horizon's ratio a measure, all in
`measure_readings`, which the verdict words' test then covers. The words of ADF and KPSS are the decomposition's
(«rejects a unit root», «a unit root is not excluded», «the two tests do not tell»), checked by a test; the page's own
terms are kept (it «periodo di verifica» for ARIMA's holdout, «arresto anticipato», «riferimento», «P(vivo)», «clienti
con riacquisti»).

**Deviations.** (1) ARIMA's assumptions say one sentence per residual test that rejects and count the others, in
place of `assumptions_rejected` naming them: the same reason as B2's (`structured_note` does not translate a check's
name chosen by the readings). ADF, KPSS and their four cases are read under «ADF and KPSS read together», not in the
assumptions. (2) The holdout's sentence is under «Fit to the data», next to its chart, not under «Fit». (3) The SHAP
formula writes «S and j» and R² a weighted mean `m`: ∖, ∪, ⊆ and ȳ are not in the report's font.

**Found, not fixed (each would change an existing page; for B4).**
- ARIMA's four-case box (`arima._stationarity`) marks the series' case by `p < α`; the runner and the reading read ADF
  and KPSS at the ends of their tables by the side (D7). At α = 0.01 with ADF's p clipped at 0.01 the reading says
  «rejects», the box marks «ADF keeps». Request: mark the box by `table_rejects`, and leave no case marked when a test
  gives no verdict.
- The box's words (`arima.json`, `cases`) still say «the differences are enough» and «Neither rejects: the series is
  too short or too noisy for them to tell», the wording D9 removed from the decomposition as saying more than the tests;
  the reading under it says «the two tests do not tell». Request: the decomposition's words in the box.
- Boosting, multiclass with `class_weight = balanced` (fixture `catboost-multiclass`, at HEAD as well): the caption
  «Overfitting — ✓ ok» and the note on the weighted curves are drawn on the same line under the learning curves.
- Open question 4: `coef_*` and `assumptions_many_rows` are in B3's words as B2's are in theirs, to be moved to
  `common.json`.

**Golden** (B2's scratch dump, on clean copies of `a775fed8` and of `a775fed8` with this lot's files, the fake chart
service): **440 of 500 renderings byte for byte as before** (PDF, definition, labelled rows) — every other model's
fixture and every variant of every statistics test; the 60 that change are exactly this lot's twelve fixtures in five
languages, none with a warning, all within seven pages (18 keep their pages, 39 gain one, 3 gain two: ARIMA with
automatic orders in German, Spanish and French). `only_additions` holds on the twelve (every item and every labelled
row as before).

**Tests** (the CI image, `--memory=2500m --cpus=1`, on the clean copy with this lot):
`test_explanations_ts_trees_clv` (new), `test_report_explanations`, `test_models_layouts`, `test_reports_model`,
`test_models_ab_review`, `test_stats_layouts`, `test_stats_explanations`, `test_explanations_regression_survival`:
**1,204 passed, 3 failed** — the three tests of frozen files that B2 reported, which assume the linear regression
and the parametric survival have no explanation.

### B4 — the closing step (2026-11-22)

The shared files were no longer frozen: B4 made the requests of B0–B3's «As built» in one serial step (`ba9a8d1a`), with
the golden comparison before and after.

**The three red tests of the shared files.** `test_report_explanations`: «a page with nothing explained» takes the linear
regression out of `READINGS` and asks `page` for no reading place (every model now explains itself, so one stands for a
model that does not); the toy explanation also clears `reading_labels` (the linear's own labels would name its rows).
`test_stats_layouts`: the check that a chart's caption never keeps with the next item leaves out `s_how`, the heading of
«How this report was computed», which keeps with its lead as every heading does (now asserted).

**The checks named** (`common.structured_note`, `values.reading_names`). A reading names a check as `check:<id>`, and
`check:<id>|<term>` for a check of each term (Schoenfeld's, Box–Tidwell's); `structured_note` says it as
`values.reading_names` has it, and `words` fills that map from the checks' names in the table, in guillemets (a name may
hold a comma: «Independence: Breusch–Godfrey, order 1»), the term after it in brackets; a model's own
`values.reading_names` comes first. A user's name that does not start with `check:` is never translated.
`assumptions_rejected` now counts **and names** the checks whose test rejects («4 of 9 — «Homoscedasticity:
Breusch–Pagan», …»), five at most in the table's order, then `more_in_table`; ARIMA's `residuals_keep_others` names the
residual tests that do not reject.

**The lines several models say, in `common.json`** (Open question 4). Moved, with their identical texts in five languages:
`assumptions_rejected`, `assumptions_none_rejected`, `assumptions_many_rows`, `coef_some`, `coef_all`, `coef_none`,
`more_in_table`, `method_p_value`, `method_se_cluster`, `threshold_vif`, `threshold_epv`, `threshold_cooks_distance`,
`threshold_leverage`, `coef_hazard`, `coef_hazard_level`, and the labels of `method_p_value` and `method_se_cluster`.
ARIMA now chooses its coefficients' line with `shared_readings.coefficient_lines` (its own `coef_one`, `coef_some_more`
and its `coef_*` words removed): the same ids and words as the regressions. `assumptions_many_rows` takes B3's wording,
which has no verdict word («the tests detect departures too small to matter»: it is chosen by the number of rows, not
by a p-value), with «rows», and joins every B2 model's `measure_readings`; ARIMA («observations») and the customer
lifetime («customers») keep their own noun. A test checks that no model file holds these ids again.

**ARIMA's box of the four cases** (`arima._stationarity`). Each test is read by the side of its table, as the runner and
the reading read it: `_verdict_of_table` says `table_rejects` as two expressions of the p-value (rejects, keeps) with the
node's α; a case is marked when both tests give its verdicts, and with a test beyond the end of its table no case is
marked. At α = 0.01 with ADF's p at its lower end the box now marks «ADF rejects» as the reading says. The box's words
(`arima.json`, `cases`) are the decomposition's: «both point to a constant level», «the level is not constant; try
d + 1», «the level drifts, a trend or a break», «the two tests do not tell»; no more «the differences are enough» or
«too short or too noisy». Each case, marked, fits the page's width on one line (a test measures it).

**A chart's note under its verdict** (`common.grid`). A chart that illustrates a check and has a note on how to read it
drew both on the same line (at HEAD as well): CatBoost multiclass with `class_weight = balanced` («Overfitting — ✓ ok» and
the weighted curves), and a binary model whose stop metric is the error rate. The note is now under the verdict and the
row of charts keeps room for both; a test checks every fixture.

**Golden** (the scratch dump of B2, on clean copies of `00ac719d` and of `00ac719d` with this lot's files, the fake chart
service): **465 of 500 renderings byte for byte as before** — every variant of every statistics test, and every other
model's fixture; the 35 that change are seven fixtures in five languages, each for a reason of this lot, none with a
warning, none with a page more or less: `linear-unpenalized`, `cox-default`, `kaplan_meier-default`,
`parametric_survival-aft` (the rejected checks named; the PDF and the labelled rows), `arima-manual`, `arima-auto` (the
box's words and its marking, the coefficients' shared line; the definition too), `catboost-multiclass` (the note under
the verdict; the PDF and the definition). The fixtures with no rejected check (`logistic-*`,
`parametric_survival-auto|ph`) and the many-rows sentence (no fixture has 5,000 rows) do not change.

**Tests.** The engine suite as the CI runs it (`ci_tests.sh engine`: ClickHouse, PostgreSQL, MinIO, Valkey; each
container's memory bounded on this machine, the tests' at 2.5 GB), on the clean copy with this lot: **6,551 passed, 194
skipped**, every skip one the CI allows (BigQuery, Oracle, SQL Server).

**Left for the trial on kind** (with the forecasting group's): drawing the reports of a test and of each model family
from a real run, with the real chart service, in Chrome and Firefox; the reference PDF of the statistics
(`GOLDEN_PDF_SHA256`) seen with the real charts.

**On kind (2026-11-22).** Reports of real runs on the cluster at `main-916fa9ce`, PDF in Italian and English, looked at page by page: Welch's t test
(a table of two groups), linear, logistic, Cox, Kaplan–Meier, ARIMA, XGBoost, CatBoost, customer lifetime, A/B test and decomposition. Every
model has its readings under the blocks, the page «How this report was computed» (the paragraphs and the formulas with their symbols) and drawn
charts; no text overlaps another, no `{name}` or `@id` is left. ARIMA's box of the four cases (ADF and KPSS) is there, but its heading and
lead line sit at the bottom of page 2 and its four rows begin page 3 (no keep-with-next for the box). CatBoost's page of the learning curves is
clean (importance, SHAP summary, the curve with the best round marked, observed against predicted); the charts' page has no reading under it
(the readings of the importance are with its table, on page 2). Welch's page 2 keeps «How this report was computed» with its two paragraphs.
