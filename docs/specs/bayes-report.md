# Spec: the report of a Bayesian model's run (`bayes-report`)

Status: **written and approved by the owner on 2026-11-24.** It specifies the module `bayes-report` of
`docs/specs/bayesian-editor-map.md` (**the map**), step 6 of its order of construction, beside `flow-node`, after the
approved `bayes-contract.md` (**the contract**: the ten-column rows, their sections and keys, the lot K0b of the
catalogue's entry), `bayes-diagnostics.md` (**the diagnostics spec**: what each measure is, the charts' rows, the
literature to cite) and `bayes-predictive-checks.md` (**the checks spec**: the `ppc` and `prior_check` rows, the words of
the charts), and of `docs/specs/report-explanations.md` (**the explanations spec**: the readings and «How this report was
computed» of every model's report). Nothing of it is built. It also holds lot **K0b** of the contract (the filtered view of
the models' catalogue for the other groups, and the entry `bayes_model`), which the owner tied to this module's first lot
(«Decisions of 2026-11-24 (K0)» of the contract). Decisions marked **(owner)** are the owner's (the map's «Interview
answers», **the answers**, and the owner's rules); the rest are this spec's, to confirm; those marked «spec decision,
2026-11-24, owner may revisit» are small choices the owner may change without reopening the module.

Words: **verified** = read in this repository's code on 2026-11-24; **assumed** = from the literature or an earlier spec, not
checked here, closed by the lot named beside it.

## Objective

The report of a node «Bayesian model», drawn **from the ten-column rows its run wrote and nothing else** (no Job, no query
of the data: the models spec, «The report»), on screen, in PDF and in Excel, in five languages (en, it, de, es, fr):

- **the summary and the fit**: the saved model and its pinned version, the rows used and left out, the draws, the facts of
  the sampler on the first page (the divergences' count among them: answer 11), the posterior summary with its intervals,
  the predictors' standardization said and the **coefficients of a linear predictor on both scales** (answer 6);
- **the diagnostics as measures and facts**: R-hat, ESS, MCSE per quantity, divergences, tree depth, E-BFMI per chain, each
  with a sentence on what it measures and the **reference values of the literature cited as such**, never compared with a
  quantity's number, no mark, no colour of judgement (answer 11);
- **the charts on thinned draws**: trace, rank histograms, posterior density with the prior's overlaid (prior → posterior),
  pairs with the divergent draws marked, each unit's median and interval;
- **the predictive checks as charts only**: the overlaid densities, the histograms of T(y_rep) with T(y) marked, the units'
  means; **no p-value, no probability, no share** (answer 12); the same pages for the prior predictive check (answer 13);
- **«How this report was computed»**: NUTS and its warm-up, the 8 significant digits of the draws, the standardization, why
  R-hat and ESS and how they are computed, what a divergence is, the contraction, what the predictive charts show and do not
  show, the program's and the binary's hashes, CmdStan's version, the seed;

and, in the same first commit, the catalogue's entry `bayes_model` with the **filtered view** that keeps the seven other
groups' ConfigMaps byte for byte (K0b), and the interface's catalogue lot (the copy, the words, the node left out of the
Models palette).

## Decisions

| Question | Decision | By |
|---|---|---|
| What a diagnostic is on the page | **A measure or a fact**, its sentence says what it measures; the literature's reference value **cited as such** (quoted in English, as the source writes it, in the five languages), in the method's explanation, never beside a quantity's number, never as «above / below»; no mark, no colour that judges, no verdict word | Owner (answer 11; the rule of 2026-11-20). Stricter than the explanations spec's rule 4 («above / below the threshold of …» for the other models' checks): answer 11 forbids any reference in the node, so no number of this report is compared with one |
| The predictive checks | **Charts only**; the outer bins say «below / above the observed range» with their counts **in the drawn sets**; no p-value, no probability, no share beside a histogram | Owner (answer 12); the checks spec, «Words» |
| The precision | Said once, in «How this report was computed» and on the summary's facts line: «the draws carry 8 significant digits (CmdStan's default)»; a quantity is never marked for it | Owner (2026-11-23/24, the contract) |
| Both scales (answer 6) | Only for the terms of a `linear_predictor` node: the coefficient on the standardized scale, then on the column's scale (`orig_*`), with the predictor's mean and sd from the `data` section; in free expressions the standardized scale only, with the mean and sd said | Owner (2026-11-23) |
| The report's scope | The rows' scope (the contract's «Which quantities»): scalars first, then the correlations, then each plate's quantities; the group-level quantities **also** as `units:` charts; on screen and in PDF the scalars and the correlations in full, each plate's quantities as its `units:` chart; the full table in Excel | The contract; the PDF's part: owner, 2026-11-24 |
| The literature | The references of the diagnostics spec («What the literature says») and of the checks spec («Words»), each **quoted from its source with its page** by lot Q4 before the words are final (the diagnostics spec: «assumed until `bayes-report` quotes it verbatim»); **the quotation in English in all five languages**, as the original writes it | The diagnostics spec; the language: owner, 2026-11-24 |
| Where the sentences are chosen | **The layout**, from the rows (`common.READINGS["bayes_model"]`, the explanations spec's B0 machinery); the runner writes no `reading` row | The contract; the explanations spec |
| The catalogue's entry | **The filtered view** (option A of the contract's K0): every other group's ConfigMap receives the catalogue and `card.schema.json` without the group `bayes`, in the order of the file; the group `bayes` receives them whole | Owner, 2026-11-24 |
| The Models palette | The Models palette leaves out the models of the group `bayes` (one constant in the interface, no field in the catalogue); the node «Bayesian model» is a palette item of its own (`flow-node`) | Spec decision, 2026-11-24, owner may revisit (a catalogue field would change the view's bytes) |
| A `model` node naming `bayes_model` | **Refused** by the gateway before any Job («the Bayesian model is a node of its own: add «Bayesian model» from the palette»), since `modelcatalog.GroupOf("bayes_model")` becomes `bayes` with the entry | Spec (found while reading `planModel`, verified) |
| The variants' pages | `sample`: «The pages of `sample`»; `prior_predictive`: «The pages of `prior_predictive`»; the Report tab of the node shows the latest of each (`flow-node`) | Spec |
| Charts whose ids are data | **Addressed by slot**: a hook `common.SLOTS["bayes_model"]` in `with_labels` gives each chart's rows its place in the scope; the runner's ids stay as written («The charts' rows») | Spec decision, 2026-11-24, owner may revisit (no change to the built runner) |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| A model's report | `backend/app/engine/models/layouts/__init__.py`: `register(model, layout, name, about)`, `definition`, `title`; the engine's `POST /reports/model` (`backend/app/api/routes/reports.py`: refuses a model without a layout, checks the params, queues `render_model_report_task`; formats `svg`, `pdf`, `xlsx`); the gateway's `/tasks/model-report` (`internal/api/proxy_model_report.go`: the run, its results key, the model and params the run kept) | `layouts/bayes_model.py` registered; the gateway's node check widened by `flow-node` |
| The page's pieces | `layouts/common.py`: `page(…)` (summary cards, coefficients, fit, blocks, charts, `explained`, notes), `Card`, `Chart`, `xy(…)`, `mark(…)`, `histogram(…)`, `coef_interval(…)`, `grid`, `text_rows`, `reading_under`, `reading_table`, `how_computed` (the `method` paragraphs, then the `formulas` on a darker panel; Excel's sheet «Reading»), `MAX_REPORT_ROWS` = the catalogue's `max_result_rows`, `encoded`, `structured_note` | The layout is a `page(…)` with blocks of its own, as `garch.py` is |
| A vertical mark on a chart | `reports/definition.py`: an `xy` chart's option `target_x` takes numbers **or an expression** (`targets_x()`), drawn as a dashed `markLine` (`reports/charts.py`) | T(y) on each histogram of T(y_rep), read from its `observed` row |
| The words | `report_texts/<model>.json` merged over `common.json` (`sections.how`, `reading`, `verdict_words` per language); the explanations spec's rules 1–8 | `report_texts/bayes_model.json`, five languages |
| The tests of the words | `backend/tests/test_report_explanations.py` (every model in `READINGS`: five texts of every id, the same names, no verdict word, no «= <», no `@`, `{` or `%2` left); `test_models_layouts.py` (the fixtures drawn, a hostile name `=HYPERLINK("http://x","y") #set ] 1% <b>` kept as text in Excel and PDF) | Run over `bayes_model` by registering it |
| The fixtures | `backend/tests/fixtures/models/<model>-<variant>.json` (the ten columns as rows), `results.read_fixture`, `results.check_table` | `bayes_model-sample.json`, `bayes_model-prior_predictive.json` |
| The rows | The contract's sections (`model`, `quantity`, `summary`, `data`, `sampler`, `ppc`, `prior_check`, `point`, `curve`, `hist`, `reading`) and the declaration in Go (`bayescontract/testdata/contract.json`: the numbers and texts of each section in both variants, `contraction` and `orig_*` in `summary`) | Read as declared |
| The charts' ids, as the runner writes them | `bayes_pass2.py` and `bayes_charts.py` (lot X3): `trace:<quantity>` (series the chain, stat `value`, `x0` the draw from 1), `rank:<quantity>` (`hist`, series the chain, stat `count`, `x0`/`x1` the bin's edges among 1…S), `density:<quantity>` (series `posterior`, `prior`, stat `value`, `x0` the abscissa), `pairs:<a>:<b>` (series `draw`, `divergent`, `x0` a's draw, `value` b's), `units:<identifier>` (`point`, series `median`, `low`, `high`, stat `y`, `x0` the rank, `text` the unit); the notes `note_summary_capped`, `note_density_cut`, `contraction_null` | Drawn as written; `<quantity>` is the element's name (`alpha[s2]`), the identifier for a scalar |
| The checks' rows | The checks spec, «The runner's side»: `ppc\|<y>:<stat>\|\|observed`, `…\|count\|k` with `x0`, `x1`; `ppc\|<y>:unit_mean\|<unit>\|…`; `curve\|ppc_density:<y>\|observed\|value\|k`, `…\|draw_<j>\|…`; the same under `prior_check` and `prior_density` | Drawn as written |
| The catalogue and its copies | `backend/app/engine/models/catalog.py` → `catalog.json`; `.github/scripts/model_catalog.py` copies it to `frontend/assets/model-catalog.json`, `gateway-go/internal/modelcatalog/catalog.json` (embedded; `Compact()` goes into every group's ConfigMap) and `gateway/app/services/model_catalog.json` (the assistants'); `modelgroups.codeFiles` appends `card.schema.json` and `catalog.json` to every group's code; `aheadOfCatalogue` holds the group `bayes` until the catalogue names it | The entry; the view (K0b) |
| The bytes of the other groups | `TestTheOtherGroupsBytesAreAsTheyWere`, `TestTheOtherGroupsBuildAndCheckAreAsTheyWere` (golden, recorded before B0) | Must stay green **without rewriting them** |

## The pages of `sample`

Every page reads the rows by the keys of the contract; every name, label, unit and column is **data** (encoded, never a
template, never a formula in Excel).

1. **Summary** (the first page). The header: «Bayesian model — <the model's name> v<version>», the step's name, the sample
   line when the run read a development sample (`stats.sample_line`). Four cards: **rows used** (`n`, under it the rows left
   out, `n_missing` + `n_not_finite`); **draws** (`chains` × `draws` after `warmup` warm-up iterations); **divergences**
   (`sampler|all||divergences`, under it «of <chains × draws> draws»: a fact, answer 11); **R-hat and ESS** (the largest
   `rhat_max` of the `quantity` rows and the smallest `ess_bulk_min` and `ess_tail_min`, as measures, plain). The facts line:
   the sampler (`nuts`, `diag_e`), the seed, the precision («8 significant digits»), the seconds of each stage. The reading
   under it (`summary`): what was estimated, from how many rows, how many draws; numbers only.
2. **Posterior summary.** One table, the report's scope in its order: the label (the name under it when the person gave
   one), the unit, mean, sd, q5, q50, q95, R-hat, bulk ESS, tail ESS, MCSE of the mean, contraction (NULL shown as «—» with
   the note's reason). A coefficient of a linear predictor has a second line «on the column's scale» (`orig_mean`, `orig_sd`,
   `orig_q5`, `orig_q50`, `orig_q95`), and the table's note says which predictors were standardized with their mean and sd
   (the `data` section). The notes `note_summary_capped` and `note_quantities_capped` under it. On screen and in PDF the table holds the scalars and the
   correlations only (each plate's quantities are page 3's caterpillars); Excel's sheet holds every `summary` row of the
   scope (owner, 2026-11-24).
3. **The groups.** For each `units:<identifier>` chart (at most `bayes_max_unit_charts`, 8): the units' medians with their
   5–95 % intervals in the order of their medians (a «caterpillar», `x0` the rank, the unit's text on hover and in Excel), the
   sample of units said when the plate has more than `bayes_unit_points` (1,000).
4. **Data.** The `data` rows: each bound variable, its column, its type, standardized or not with its mean and sd, the units
   or levels and the reference level of a category, the missing values; the series' order said when the model has one.
5. **Diagnostics.** The `sampler` table (a row a chain and «all»: divergences, the draws at the largest tree depth and the
   deepest tree, E-BFMI, the step size, the mean acceptance statistic, the mean number of leapfrog steps, the seconds of
   warm-up and sampling). Under it, one paragraph a measure: **what it measures**, then **the literature's reference value
   cited as such** («Vehtari et al. (2021, p. …) recommend using the draws only when R-hat is below 1.01, with at least four
   chains»), never followed by a comparison. The `quantity` table (each node: its quantities' count, the largest R-hat, the
   smallest ESS), plain.
6. **Charts** (`grid`, two a row): for each quantity of the charts' scope (at most 12): its trace (a line a chain), its rank
   histograms (bars a chain, 20 bins), its density with the prior's (the posterior solid, the prior dashed; the cut said
   under it from `note_density_cut`, «0.4 % of the posterior draws and 2 % of the prior's lie outside the drawn range»); then
   the pairs (`pairs:<a>:<b>`, at most six: the thinned draws as points, the divergent ones in their own colour and legend).
   The caption of each kind says how to read it, never what it proves.
7. **Predictive checks** (when the run has `ppc` rows; otherwise the note «this version was saved before the predictive
   checks: saving it again adds them» or «the checks cover 3 of the model's 4 likelihoods»). For each checked outcome: the
   overlaid densities (`ppc_density:<y>`: the observed line black and thick, the 20 replicated thin and grey, the outer bins
   at the range's edges labelled «below / above the observed range»); the histograms of T(y_rep) of each statistic (`xy`
   bars of `count` between `x0` and `x1`, `target_x` = the `observed` row's value, its label «the data»); the checked units'
   means, likewise. The caption: «The grey lines are 20 data sets the model draws from the posterior; the black line is the
   data. Where the black line runs outside the grey ones, the drawn data sets do not show that feature of the data.» (*Correction of 2026-11-24 from lot Q3*: the checks
   spec's «the model does not reproduce that feature» judged the model; the sentence says what the picture shows) (the checks
   spec). The outcome is named by its column (the `data` section's `column` of its Stan identifier).
8. **How this report was computed** (`common.how_computed`): «The method» below.
9. **Notes**: every `note_*` of the `model` section through `structured_note`.

## The pages of `prior_predictive`

1. **Summary**: the cards rows used, draws (4 × 250, `fixed_param`), the quantities drawn, the checked outcomes; the facts
   line («ancestral draws from the priors: no warm-up, no sampler's diagnostics»).
2. **The priors' summary**: mean, sd and the five quantiles of each quantity's prior draws (the variant's seven numbers),
   with `note_prior_undrawn` when some quantities have no ancestral draw.
3. **Prior predictive checks**: the pages of «Predictive checks» with `prior_check` and `prior_density`; the caption says the
   grey lines are data sets drawn **from the priors alone, before the data are seen** (Gabry et al. 2019).
4. **How this report was computed**, **Notes**.

## The method («How this report was computed»)

Paragraphs (`method`), each chosen by the rows and filled with the run's numbers (`@id name=value`, said by
`structured_note`):

| Id | Says | Its source |
|---|---|---|
| `nuts` | The draws come from Stan's dynamic Hamiltonian Monte Carlo (the No-U-Turn sampler), `chains` chains of `warmup` warm-up and `draws` kept iterations, the step size and a diagonal metric adapted in the warm-up, the seed | Hoffman and Gelman (2014), arXiv:1111.4246v1, p. 1 (*Correction of 2026-11-24 from Q4*: quoted from the arXiv version, the only one with pages Q4 could read) |
| `precision` | Every draw carries 8 significant digits (CmdStan 2.40.0's default): a draw x is off by at most 5·10⁻⁸·\|x\|; a quantity whose mean is large beside its sd may repeat values | The contract, «Risks» |
| `standardized` | Which predictors were centred and divided by their sd (the numbers), and that a linear predictor's coefficients are also given on the column's scale | Answer 6 |
| `rhat_ess` | R-hat compares the chains with each other and within their halves, on ranks, in the bulk and in the tails; the bulk and tail ESS say how many independent draws the chains are worth; computed as the R package `posterior` 1.8.0 computes them (the diagnostics spec) | Vehtari, Gelman, Simpson, Carpenter, Bürkner (2021), *Bayesian Analysis* 16(2) |
| `divergences` | What a divergent transition is, the count of this run, and the literature's statement cited as such | The Stan Reference Manual 2.40, «MCMC Sampling», «Divergent transitions», quoted by Q4 (*correction of 2026-11-24 from Q4*: Betancourt (2016) is not quoted) |
| `treedepth`, `ebfmi` | What reaching the largest tree means (efficiency), what E-BFMI measures, each with its cited value | The Stan Reference Manual 2.40, «NUTS and its configuration»; Betancourt (2017), arXiv:1701.02434v2, p. 44 (*correction of 2026-11-24 from Q4*: the 0.3 is in the 2017 review, not in the 2016 paper) |
| `contraction` | 1 − Var_post/Var_prior, what it measures, why it is absent when the prior has no finite variance | Betancourt (2018), arXiv:1803.08393v1, p. 17 (*correction of 2026-11-24 from Q4*: that paper is «Calibrating Model-Based Inferences and Decisions», and the contraction is its «posterior shrinkage»; «Towards a principled Bayesian workflow» is not an arXiv paper) |
| `checks` | What the overlaid densities and the histograms show, and why no p-value is given | Gabry et al. (2019), p. 8 (posterior) and p. 5 (prior), quoted by lot Q4. *Correction of 2026-11-24 from Q4*: the Stan User's Guide and Meng (1994), Gelman, Meng and Stern (1996), Bayarri and Berger (2000) were not read at their source, so they are not cited |
| `program` | The program's sha256, the binary's, CmdStan's and stanc's versions (the `software` text), the saved model's id and version | The rows |

The formulas' box (`formulas`): split R-hat (√(var⁺/W)), the ESS (S/τ̂), the MCSE of the mean, E-BFMI (with the N − 1
divisor), the contraction, the original scale of a coefficient (coef/sd) and of an intercept. Every formula as the
diagnostics spec writes it.

## The words

- `report_texts/bayes_model.json`, the same keys in en, it, de, es, fr; merged over `common.json`; the variant's words
  (`sample`, `prior_predictive`) apart where they differ.
- **No verdict word** in any text the layout writes (outside the literature's quotations, which are their authors' words: lot Q4's scan removes what lies between “ and ” before it looks, and the quotations' own test checks them byte for byte) (`common.json`'s `verdict_words`, and for this model also «converged»,
  «not converged», «good», «bad», «reliable», «unreliable», «passes», «fails», «acceptable», «problem» in the five
  languages, added to the model's own list): a test scans every text.
- **The literature's values** appear only inside the `method` paragraphs and the diagnostics' paragraphs, as quotations of
  their source **in English, in every language** (owner, 2026-11-24), with author, year and page; never in a table, a card or a caption next to a number of the run.
- **Names stay** (rule 7 of the explanations spec): a label, a unit, a column is a value, never put in a number form,
  encoded in the reading rows and kept as text in Excel.

## The charts' rows, and what the layout expects of them

| Chart (as the runner writes it) | Its slot after `SLOTS` | Filter | Drawn as |
|---|---|---|---|
| `trace:<q>` | `trace:<k>`, k 1…12 | `[section] = "curve" AND [level] = "trace:<k>"` | `xy`, `split_by` the series (the chain), lines |
| `rank:<q>` | `rank:<k>` | `[section] = "hist" AND [level] = "rank:<k>"` | `xy` bars between `x0` and `x1`, a colour a chain |
| `density:<q>` | `density:<k>` | `curve`, series `posterior`, `prior` | `xy` lines, the prior dashed |
| `pairs:<a>:<b>` | `pairs:<k>`, k 1…6 | `curve`, series `draw`, `divergent` | `xy` points, `x0` against `value` |
| `units:<id>` | `units:<k>`, k 1…8 | `point`, series `median`, `low`, `high` | `interval` by rank |
| `ppc_density:<y>`, `prior_density:<y>` | `ppc_density:o<k>`, k 1…3 | `curve`, series `observed`, `draw_<j>` | `xy` lines |
| T(y_rep): `ppc` or `prior_check`, level `<y>:<stat>` | `o<k>:<stat>` | stat `count` | `xy` bars, `target_x` the `observed` value |

**The charts' ids are data** (a `<q>` holds a person's name and a unit's text, a `<y>` the outcome's identifier), and a
layout is a definition written **before** it sees the rows (`definition(model, params, locale, sample, step)`, verified: its
filters and chart titles are fixed text; `ChartItem.title` is a string, verified). So the layout addresses the charts **by
their slot**: `common.with_labels` (which sees the rows) calls a hook of the model, `common.SLOTS["bayes_model"](rows)`, of
the same shape as `READINGS` (the explanations spec's B0 machinery), which rewrites each chart row's `level` from
`<kind>:<q>` to `<kind>:<k>` (k its quantity's place in the scope's order: 1…12 for trace, rank and density, 1…6 for pairs,
1…8 for units; 1…3 for an outcome, `o<k>:<stat>` and `ppc_density:o<k>`) and keeps the name in `label` — how a row is said,
not a computation, as every other rewrite of `with_labels`. The layout then defines the fixed number of slots, each drawn
only when its rows exist (`exists(filter)`), and a caption above each slot says its quantity's label and unit, read from the
rows with `LOOKUP` (in the expression language, verified; that a text item's value may hold it is **assumed**, Q2 checks it,
else the caption is a one-row table of the slot's label).

## The catalogue's entry and the filtered view (lot K0b of the contract)

- **The view.** `modelcatalog.CompactFor(group)`: for any group but `bayes`, the compact catalogue **without** the group
  `bayes` — its entry in `groups`, the model `bayes_model`, its keys of `limits` (`bayes_*`, `max_check_columns`), its
  section shapes (`summary`, `quantity`, `data`, `sampler`, `ppc`, `prior_check`) — removed by a filter over the JSON's
  tokens that keeps every other byte as it is (no decode and re-encode of numbers); for `bayes`, `Compact()` whole. The same
  for `card.schema.json` (the `bayes` property of the card's object removed). `codeFiles` takes the view of its group.
  Accepted when the two golden tests of the other groups pass **without rewriting a golden file**.
- **The entry.** In `catalog.py`: the group `bayes` (`python`, «Bayesian models»), the model `bayes_model` (`card: true`,
  `takes: {mode, output}`, the variants `sample` and `prior_predictive` with their sections, numbers, texts and charts as
  `bayescontract`'s declaration, the outputs `summary`, `draws`, `replicated` with their fixed columns, the limits and
  shapes); a test compares it with `bayescontract/testdata/contract.json`; `predict.applies_to` does not name it.
  `aheadOfCatalogue` loses the group. `model_catalog.py` writes the copies; `--check` green.
- **The card's schema** gains the `bayes` object (`bayescontract.Card`).
- **The interface's lot**: the frontend's copy; the model's words (`models.names.bayes_model` and what the catalogue's
  frontend test asks of every model) in five languages; the Models palette leaves out the group `bayes`; the run page's
  label for a `model` run of `bayes_model`; the gateway's `planModel` refuses a node of type `model` whose model is
  `bayes_model` (Decisions).
- **The assistants' copy** (`gateway/app/services/model_catalog.json`, written by the script) gains the entry too: what the
  assistants do with it is the module `assistants`' (step 7); until then a proposed `model` node naming it is refused by
  `planModel` with its sentence.

## Tech stack

- **Python** (the engine, `backend/`): `layouts/bayes_model.py`, `report_texts/bayes_model.json`, the catalogue's entry in
  `catalog.py`; the report engine as it is (Typst for PDF, the xlsx writer); no new package.
- **Go**: `internal/modelcatalog` (`CompactFor`), `internal/modelgroups` (`codeFiles`, `card.schema.json`, `aheadOfCatalogue`),
  `internal/api/jobs_models.go` (the refusal in `planModel`); no new module.
- **TypeScript**: the catalogue's copy, the words, the palette's filter; no new package.

## Commands

```bash
cd backend && pytest tests/test_models_layouts.py tests/test_report_explanations.py tests/test_models_catalog.py \
  tests/test_models_results.py -k bayes -q                       # no -n on this machine
python3 .github/scripts/model_catalog.py && python3 .github/scripts/model_catalog.py --check
cd gateway-go && go test ./internal/modelcatalog/... ./internal/modelgroups/... ./internal/bayescontract/...
cd frontend && node --experimental-strip-types --no-warnings=ExperimentalWarning --test tests/model-catalog.test.mjs
cd frontend && npx nuxi prepare && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
```

## Project structure

```text
backend/app/engine/models/layouts/bayes_model.py        the layout: the pages of both variants, READINGS["bayes_model"]
backend/app/engine/models/layouts/__init__.py           (changed) the import
backend/app/engine/models/layouts/common.py             (changed) the hook SLOTS, called by with_labels as READINGS is
backend/app/engine/models/report_texts/bayes_model.json  the words, five languages
backend/app/engine/models/catalog.py, catalog.json      (changed) the entry bayes_model, its limits and shapes
backend/tests/fixtures/models/bayes_model-sample.json, bayes_model-prior_predictive.json
backend/tests/test_models_bayes_report.py                the pages, the words, the hostile names, the caps
gateway-go/internal/modelgroups/groups/bayes/reference/make_report_fixtures.py   the fixtures from the runner's writers (by hand)
gateway-go/internal/modelcatalog/modelcatalog.go        (changed) CompactFor
gateway-go/internal/modelgroups/groups.go, card.schema.json   (changed) the view, the bayes object, aheadOfCatalogue
gateway-go/internal/api/jobs_models.go                  (changed) a model node naming bayes_model refused
frontend/assets/model-catalog.json, frontend/composables/modelCatalog.ts, frontend/i18n/locales/*.gen.ts
```

## Code style

A model's chart, as the existing layouts write it (verified, `layouts/common.py`):

```python
def xy(id_: str, title: str, filter_: str, series: list[dict], options: dict | None = None, under: str | None = None,
       split_by: str | None = None, wide: bool = False, visible: str | None = None) -> Chart:
    """An `xy` chart of the rows `filter_` keeps, x their `x0`."""
    more: dict[str, Any] = {"filter": filter_, "category": "[x0]", "series": series, "options": {"format": "0.###", **(options or {})}}
```

This module's in the same voice (a sketch; lot Q3 writes it): the data's value marked on the histogram of the replicated
statistic by an expression of the rows, never by a number the layout computes.

```python
def statistic_chart(w: dict, section: str, k: int, stat: str) -> c.Chart:
    """The draws of T(y_rep) of the k-th checked outcome in their bins, T(y) a dashed line: a drawing, never a share or a
    p-value. The level is the slot `SLOTS` gave the rows (`o<k>:<stat>`), so no name of the data enters the definition."""
    rows = f'[section] = "{section}" AND [level] = "o{k}:{stat}"'
    observed = f'SUMIF({rows} AND [stat] = "observed", [value])'
    return c.xy(f"t_{section}_{k}_{stat}", w["checks"]["statistic"][stat], rows + ' AND [stat] = "count"',
                [c.mark("count", "bar", w["checks"]["draws"])], {"target_x": observed, "legend": False})
```

## Testing strategy

**On small data**: the two fixtures (a template's run on the fake CmdStan), plus rows made bigger by the tests themselves.

- **The fixtures** (`make_report_fixtures.py`, by hand, committed): written with the runner's own writers (`bayes_rows`,
  `bayes_pass2`) on fake draws of `hier_slopes` (a plate, a correlation, a linear predictor with standardized terms), the
  `ppc` rows from P3b once it exists; `results.check_table` accepts them for both variants.
- **The pages** (`test_models_bayes_report.py`): both variants drawn in five languages and three formats; every chart kind
  of «The charts' rows» present with a non-empty series; the `orig_*` line under a linear predictor's coefficient only; a
  NULL contraction shown with its reason; the notes; a version without checks; a run with 4 of 3 likelihoods checked.
- **The words** (`test_report_explanations.py`, by registering `bayes_model` in `READINGS`): five texts of every id, the
  same names, no verdict word (the common list and the model's), no «= <», nothing left of `@`, `{`, `%2`; **no number of a
  run beside a literature value**: a test scans the readings and captions for the cited values (1.01, 400, 100, 0.3) and
  accepts them only in the `method` and diagnostics paragraphs.
- **Hostile data**: a label, a unit and a column named `=HYPERLINK("http://x","y") #set ] 1% <b>` (the existing test's
  string) kept as text in the PDF and in Excel (no formula, no markup).
- **The caps**: rows made to the contract's maxima (2,000 summary quantities, 12 charts at 8 chains, 8 unit charts of 1,000
  points, six pairs, the checks' 4,044 rows: 93,490 in all) drawn without error; its time and memory recorded by lot Q5.
- **K0b**: the golden bytes of the seven other groups unchanged without rewriting them; `CompactFor("bayes")` equals
  `Compact()`; the view of every other group equals the catalogue before the entry, byte for byte (the golden file of
  today's catalogue kept by the test); `card.schema.json`'s view likewise; `model_catalog.py --check` green; the frontend's
  catalogue test with the words; a `model` node naming `bayes_model` refused with its sentence.
- **By hand** (lot Q5): the report of a real run on kind, on screen, in PDF and in Excel, on Chrome and Firefox (the
  owner's rule), at the development machine's size of the largest rows.

## Boundaries

- **Always**: a drawing of the rows only; measures and facts with what they measure; the literature quoted with its page;
  names and labels as data; five languages with the same keys; the other groups' bytes unchanged.
- **Ask first**: a new section, number, chart or row of the runner (the contract's «Ask first»); a reference value beside a
  number; a word of judgement in any language; a new field of the catalogue.
- **Never**: a verdict, a mark, a colour that judges, «above / below» a reference; a p-value, a probability or a share of a
  predictive check; a draw read from anywhere but the rows; a label in a formula or a template; the other groups' golden
  files rewritten.

## Success criteria

1. Both variants' fixtures pass the engine's check and draw in five languages, as SVG, PDF and Excel.
2. Every measure of the diagnostics has its sentence and its cited reference value with the source's page; no row, card,
   caption or table compares a number of the run with it (the test of «The words»).
3. The predictive pages show the overlaid densities and the histograms with T(y) marked, and no p-value, probability or
   share in any language.
4. A linear predictor's coefficients appear on both scales with the predictors' means and sds; nothing else does.
5. K0b: the seven other groups' golden bytes are unchanged without rewriting them; the catalogue's copies are checked.
6. The largest rows the contract allows render; their time and memory are recorded (Q5).

## Lots

| Lot | Objective | Files | Accepted when | Depends on | Agent |
|---|---|---|---|---|---|
| **Q0** | The filtered view (`CompactFor`, the schema's view, `codeFiles`), without the entry | `modelcatalog.go`, `groups.go`, tests | the other groups' golden bytes unchanged without rewriting | — | sonnet |
| **Q1** | In one commit (the owner's K0b decision): the entry `bayes_model` and its copies, the card's `bayes` object, the interface's catalogue lot, `planModel`'s refusal, the layout's first pages (summary, posterior summary, groups, data, notes, the method's shell), the two fixtures without checks | `catalog.py`, copies, `card.schema.json`, `bayes_model.py`, `bayes_model.json`, frontend, `jobs_models.go`, tests | criteria 1, 4, 5 | Q0; the runner's writers (built) | sonnet |
| **Q2** | The diagnostics page and the charts (trace, rank, density with the prior, pairs, units) | `bayes_model.py`, words, tests | the pages' and the words' tests | Q1 | sonnet |
| **Q3** | The predictive checks' pages of both variants; the fixtures with `ppc` and `prior_check` rows | the same, fixtures | criterion 3 | Q1; the checks spec's P3b | sonnet |
| **Q4** | «How this report was computed» and the readings; the literature read at its source and quoted in English with its page, the same bytes in the five languages | words, `READINGS`, tests | criterion 2 | Q2, Q3 | sonnet, then **opus** reviews the words against the measures-without-verdict rule (a fresh context, given the texts and the rule, not this spec's conclusions) |
| **Q5** | The largest rows rendered (time, memory); the report on kind, Chrome and Firefox | «As built» | criterion 6 | Q4; `flow-node`'s trial on kind | sonnet (by hand, alone) |

## Corrections made to the map and to the approved specs (2026-11-24)

Applied on the owner's approval of 2026-11-24, each marked «Correction of 2026-11-24 from the step-5 specs» where it lands:

- **`bayes-contract.md`**, «Sections»: a chart's id holds the **quantity's name** (`trace:alpha_school[Liceo Galilei]`), as
  lot X3 writes it, the identifier only for a scalar; the notes the layout reads; K0b is this spec's Q0 (alone) and Q1 (the
  entry, in one commit with the interface's lot and the first layout).
- **`report-explanations.md`**, rule 4: an exception for `bayes_model` — no «above / below the threshold» (answer 11), the
  literature's values only as English quotations in the method.
- **`bayes-diagnostics.md`**, «What the literature says»: the values are quoted **in English, as their source writes them,
  in all five languages**, with their page, by lot Q4.

## Decisions of 2026-11-24 (the answers to this spec's questions)

| Question | Decision | By |
|---|---|---|
| How much of the posterior summary the PDF prints | **The scalars and the correlations in full; each plate's quantities as its `units:` chart (caterpillar); the full table of the report's scope in Excel** (and every quantity in the node's `summary` output). Page 2 of «The pages of `sample`» prints the scalars and correlations only on screen and in PDF; the Excel sheet «Posterior summary» holds every `summary` row | Owner, 2026-11-24 |
| How the literature's reference values are said in the five languages | **In English everywhere, as the original writes them**, in quotation marks with the author, year and page, in all five languages (against the recommendation): the sentence around the quotation is translated, the quotation never; the words' test checks that the quotation is byte for byte the same in the five files | Owner, 2026-11-24 |
| What 1.5.0 needs (asked in `bayes-flow-node.md`) | **Everything of this spec**: Q0–Q5, the predictive checks' pages of both variants, the original scale | Owner, 2026-11-24 |

## Open questions

None.

## As built — lot Q4 (2026-11-24)

- **The method** (`READINGS["bayes_model"]` in `layouts/bayes_model.py`): the lines chosen from the rows (selecting and counting, no computation) —
  `method_nuts` or `method_prior`, `method_precision`, `method_standardized` (when a predictor was standardized), `method_original` (when rows have
  `orig_*`), then for `sample` `method_rhat_ess`, `method_divergences`, `method_treedepth`, `method_ebfmi`, `method_contraction` (when the summary has
  the contraction), then `method_checks` / `method_prior_checks` (when the run has the checks' rows; the number of drawn sets is counted from the
  density's series), `method_program`; the formulas `formula_rhat`, `formula_ess`, `formula_mcse`, `formula_ebfmi`, `formula_contraction`,
  `formula_original`. Words: `reading_texts`, `formula_texts`, `reading_labels`, `measure_readings`, `verdict_words` (the common list and the spec's
  own, in the five languages).
- **The literature, read at its source on 2026-11-24 and quoted in English in the five files** (a test compares the quotations byte for byte; «[R-hat]»
  stands for the R with a hat the paper writes, a character the report's font has but that prints badly): Vehtari et al. (2021), arXiv:1903.08008v5,
  p. 4 (R-hat below 1.01; at least four chains) and p. 5 (rank-normalized ESS greater than 400); Hoffman and Gelman, arXiv:1111.4246v1, p. 1 (NUTS);
  Betancourt (2017), arXiv:1701.02434v2, p. 44 (E-BFMI below 0.3); Betancourt (2018), arXiv:1803.08393v1, p. 17 (posterior shrinkage); Gabry et al.
  (2019), arXiv:1709.01449v5, p. 8 (posterior predictive checking) and p. 5 (prior predictive); the Stan Reference Manual 2.40, «MCMC Sampling»
  («Divergent transitions», «NUTS and its configuration»), which has sections, not pages. Pages are those of the arXiv versions. *Corrections*: the
  diagnostics spec's «100 per chain (400 for four chains)» is not in the paper — it says 400 for the rank-normalized ESS (p. 5), and 50 per split
  chain for R-hat (p. 8), which this report does not quote; the divergence sentence quoted is the manual's, whose «should prompt suspicion» (Betancourt
  2017, p. 45) is a recommendation, not quoted.
- **The notes of the runner are structured** — `@bayes_<id> name=value …` (`bayes_task.note_line`), said by `note_texts.bayes_<id>` (a name is encoded,
  a list joined by commas, as the other groups do): `summary_capped`, `quantities_capped`, `contraction_null` (the three counts, zero included),
  `original_capped`, `density_cut` (three lists in the same order), `no_checks`, `checks_dropped`, `prior_undrawn_one` / `prior_undrawn` /
  `prior_undrawn_more`, `prior_dropped`, `replicated_dropped`. A note of the old form (an English sentence) is still shown as written.
- **The scale of the column** is a second table under the posterior's (the first lot's choice, kept: the spec's «second line» is that table's
  row of the same coefficient): the pages print the scalars and the correlations, the Excel sheet «On the column's scale» every row of the scope with
  its unit (the plate's intercepts, moved by the predictors' means). The fixtures hold the runner's own numbers (`bayes_scale`).
- **The count outside the data's range** uses the outcome's own number of rows: the runner writes `ppc|<y>||n` / `prior_check|<y>||n` (a number,
  unindexed; the declaration, the catalogue and the contract say so), the layout falls back to the model's `n` for a version saved before it.
- **The verdict scan** of the five languages: `verdict_words` of the words holds the common list and the spec's (converged, reliable, acceptable,
  problem…, translated), `test_report_explanations.py` and `test_models_bayes_report.py` scan the sentences with the quotations removed.
- Formulas are written without the sub- and superscripts the report's font lacks (`Σ_t`, `E_n`, `var+`).
- **For the review (opus)**: the quotations contain words of the common list («proven problematic», «recommend»); they are the sources', the
  sentences around them are ours. The values 1.01, 400 and 0.3 appear nowhere but in the method's quotations.
