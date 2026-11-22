# Spec: the explanation of the method in every report (lots B0–B4)

Status: **written by the agent on 2026-11-22; lot B0 built.** The owner decided the piece on 2026-11-21
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
   The checks' table keeps its verdict column as it is (Open question 3).
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
