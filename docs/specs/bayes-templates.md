# Spec: the templates of the Bayesian editor (`templates`)

Status: **written and approved by the owner on 2026-11-23.** It specifies the module `templates` of
`docs/specs/bayesian-editor-map.md` (**the map**), step 3 of its order of construction, after the approved
`bayes-distribution-catalog.md` (**the catalogue spec**), `bayes-expression-language.md` (**the language spec**),
`bayes-model-graph.md` (**the graph spec**), `bayes-stan-codegen.md` (**the codegen spec**) and `bayes-prior-preview.md`
(**the preview spec**). Nothing of it is built. Decisions marked **(owner)** are the owner's (the map's «The owner's
decisions» and «Interview answers», numbered as there: **the answers**); the rest are this spec's, to confirm; those
marked «spec decision, 2026-11-23, owner may revisit» are small choices the owner may change without reopening the
module. The owner answered this spec's questions on 2026-11-23 («Decisions of 2026-11-23»); no question is open.

Words: **verified** = read in this repository's code or in the approved specs on 2026-11-23; **checked online** = read on
the source's own page on 2026-11-23; **assumed** = not run here, checked by the lot named beside it.

## Objective

The ten models the map lists as the first release's families, drawn once as **graphs of `model-graph`'s format**, with
their **default priors written in the open** on the nodes, so that a person who opens the editor finds a working model
instead of an empty canvas (answer 1):

| Id | Family | What it models |
|---|---|---|
| `glm_linear` | GLM | a real outcome from predictors: linear regression |
| `glm_logistic` | GLM | a yes/no outcome: logistic regression |
| `glm_poisson` | GLM | a count: Poisson regression |
| `glm_negbin` | GLM | a count that varies more than a Poisson allows: negative binomial regression |
| `hier_intercepts` | hierarchical | a real outcome with a level per group (varying intercepts) |
| `hier_slopes` | hierarchical | a level and a slope per group, correlated (LKJ, answer 2) |
| `hier_nested` | hierarchical | groups inside groups: schools in districts (nested plates, answer 4) |
| `ts_ar` | time series | a series from its own past values: AR(p) with `lag` |
| `ts_random_walk` | time series | a series that moves by steps from its last value, with a drift |
| `ts_local_level` | time series | a hidden level that wanders, seen with noise (the simplest state space) |

Each template is:

- **a graph like any other once opened** (the map: «a template is a graph, opened as a copy»; the owner's «graph only»):
  nothing in the saved model says it must stay a template;
- **validated** by `bayesgraph` with no error, and **turned into a program** by `bayescodegen` whose bytes are the codegen
  spec's golden files for these cases;
- **explained** for a reader who does not know Bayesian statistics (answer 1): what the model is for, what each node is,
  what each default prior says and where it comes from — in five languages, as facts;
- **checked against reference values**: on reference data with known parameters, the posterior of the program Tabularia
  generates and the posterior of a Stan program written by hand outside Tabularia agree within Monte Carlo error, by a
  test with a p-value («Reference values»).

It does not draw anything (`graph-editor` shows the templates on the empty canvas), save anything (`model-store`), or
bind data (`flow-node`).

## Decisions

| Question | Decision | By |
|---|---|---|
| What a template is | A graph, opened as a copy; once opened it is a graph like any other | Owner («graph only»), the map |
| Where it is proposed | The empty canvas proposes the templates; the free model is one click away | Owner (answer 1) |
| The scale of the defaults | Priors on the scale of **standardized predictors** (answer 6: standardization on by default); never changed by the data | Owner (answer 6), the map |
| The outcome's scale (linear, hierarchical, series) | **Asked when the template is opened**: the outcome's typical value and its typical spread (both optional), the priors writing them in the open (`2.5 * 300`, `1 / 300`); constants the person typed, never data. The outcome is never standardized (the graph spec, `graph.standardize`; `bayes-contract`) | Owner (2026-11-23) |
| The order of the AR template | **Asked when the template is opened**: p from 1 to **31** (`bayesTemplateMaxOrder`: the largest p whose AR expression stays within the language's 1,000-byte limit, below the catalogue's `max_lag`; Correction of 2026-11-24 from the mockups), default 2 | Owner (2026-11-23); the bound: 2026-11-24 |
| One source | The templates are one JSON file in the gateway (`internal/bayestemplates/templates.json`), embedded; the browser reads a byte-for-byte copy (`frontend/assets/bayes-templates.json`), copied and checked in CI as the catalogue is | Spec (the catalogue's precedent, verified) |
| Labels | Written in English in the file; the editor puts the person's language's labels on the nodes when the template is opened (`bayes.templates.<id>.labels.<node>`) | Spec decision, 2026-11-23, owner may revisit |
| Explanations | In the locales (`bayes.templates.*`), never in the saved graph's `note`: the note is the person's; an explanation shown beside a node whose default prior was changed would be false (`graph-editor` shows a template's explanation only while the node's slot still equals the template's) | Spec decision, 2026-11-23, owner may revisit |
| Names | Every node has a name that passes the codegen spec's identifier rule, so the program shows the person's names and the mapping table has no `renamed` entry (`beta`, `gamma`, `normal` are distribution names and would be renamed: the coefficient is `b_x`) | Spec |
| Hierarchical form | Every group plate **non-centred** (answer 4: the default); the centred programs stay golden cases of `stan-codegen` (`hier_slopes_centred`, `ts_local_level_centred`), not templates | Owner (answer 4), spec |
| The LKJ's `eta` | **2** | Spec decision, 2026-11-23, owner may revisit («The default priors») |
| The likelihood of the hierarchical templates | Normal (a real outcome); the panel says how to change it | Spec decision, 2026-11-23, owner may revisit |
| Reference data | **Synthetic**, drawn from known parameters with fixed seeds, at most 2,000 rows per template (the owner's rule of small data), committed as CSV | Spec decision, 2026-11-23, owner may revisit (the map's «on the sample data»: the pasta maker's tables have no column with a known posterior; a flow on them is `docs`' lot) |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| The graph's format | The graph spec («The canonical JSON»), `gateway-go/internal/bayesgraph/graph.go` (`Graph`, `Plate`, `Node`, `Slot`, `Term`, `Component`, `Correlation`, `Series`, `Box`), `Decode`, `CheckStructure` (lot M0, committed `f0f218de`) | Every template is a document of format 1, decoded by `Decode` |
| The validator | `bayesgraph.Validate` (lot M1 in progress; M2 adds likelihoods, linear predictors, states, correlated effects) | Every template validates with no error |
| The generator | The codegen spec (`bayescodegen.Generate`, not built); its golden corpus lists `glm_linear`, `glm_logistic`, `glm_poisson`, `glm_negbin`, `hier_intercepts`, `hier_slopes`, `hier_nested`, `ts_ar2`, `ts_random_walk`, `ts_local_level` and says: «The ten templates' graphs are written here by hand first; `templates` (step 3) writes its own and a test of that module checks that they give these programs» | The test `TestTemplatesGolden` |
| The catalogue's distributions and their argument names | `gateway-go/internal/bayescatalog/catalog.json`: `normal(mu, sigma)`, `exponential(beta)`, `inv_gamma(alpha, beta)`, `lkj_corr_cholesky(eta)`, `bernoulli_logit(alpha)`, `poisson_log(alpha)`, `neg_binomial_2_log(eta, phi)`; `language.functions` holds `lag` | The arguments of every prior and likelihood |
| A snapshot copied to the frontend and checked | `.github/scripts/bayes_catalog.py` (`--check` fails on a byte of difference), `.github/workflows/bayes-catalog.yml`, `frontend/assets/bayes-catalog.json` read by `frontend/composables/bayesCatalog.ts` | `.github/scripts/bayes_templates.py`, the same workflow extended |
| The browser's mirrors | `frontend/composables/bayesExpr.ts` (`bayesParseExpr`, `bayesFormatNumber`), `bayesExprCheck.ts`; the graph mirror `bayesGraph.ts` (lot M4, not built) | The scaling of a template's numbers (`bayesOpenTemplate`), its findings checked by the mirror in the tests |
| The words' rule | `docs/specs/report-explanations.md`: a measure is said as a number, without a word of judgement; the preview spec's list («wide», «vague», «informative», «reasonable») | Every explanation of this module |
| Tests of two implementations on one corpus | `backend/tests/fixtures/models/params_cases.json` read by the engine and by `gateway-go/internal/modelcatalog/modelcatalog_test.go`; the graph corpus of `bayesgraph` | The opened templates' fixtures, read by Go and by Node |
| Witnesses outside Tabularia | The catalogue's `points.json` (SciPy, outside Tabularia, its versions in its header); the models spec's `witness.json` per group | The reference values' file and its header |

## The file

`gateway-go/internal/bayestemplates/templates.json`, one JSON object, the templates in the order of the table above:

```json
{
  "format_version": 1,
  "templates": [
    { "id": "glm_linear", "version": 1, "family": "glm", "scale": "outcome",
      "scaled": [ {"node": "alpha", "slot": "prior.mu", "as": "center"},
                  {"node": "alpha", "slot": "prior.sigma", "as": "spread", "times": 2.5},
                  {"node": "bx", "slot": "prior.sigma", "as": "spread", "times": 2.5},
                  {"node": "sigma", "slot": "prior.beta", "as": "rate"} ],
      "sources": {"alpha": "rstanarm_priors", "bx": "rstanarm_priors", "sigma": "rstanarm_priors"},
      "graph": { "format_version": 1, "plates": [ … ], "nodes": [ … ], "layout": { … } } }
  ],
  "sources": { "rstanarm_priors": {"title": "Prior Distributions for rstanarm Models",
               "url": "https://mc-stan.org/rstanarm/articles/priors.html"} }
}
```

| Key | Meaning |
|---|---|
| `id` | the template's stable id (the table above); the codegen golden case of the same name (`ts_ar` gives `ts_ar2` at its default order) |
| `version` | an integer, raised whenever the template's graph changes; recorded by `model-store` with a model opened from it (`template_id`, `template_version`) |
| `family` | `glm`, `hierarchical`, `series`: the empty canvas's groups |
| `scale` | `outcome` when the template's priors on the outcome's scale are written from the numbers the person gives when opening it, or null |
| `scaled` | the slots the outcome's numbers rewrite: `center` (the typical value), `spread` × `times` (a multiple of the typical spread), `rate` (1 / the spread) |
| `sources` | per node, the source of its default prior (a key of the file's `sources`, or `spec` for this spec's own choice) |
| `graph` | a graph of format 1 **at the default numbers** (centre 0, spread 1, the AR order 2): a valid model as it stands |

The `layout` of each graph is drawn by hand for the canvas, **left to right** as the flow editor (boxes not overlapping
at 1280 px; the hyperparameters in a column at the left, the boxes in the middle, the linear predictor and the likelihood
at the right; every node's sources to its left, so each edge leaves a right handle and enters a left one going
rightwards). It is not part of the model (the graph spec). (Correction of 2026-11-24 from the mockups: the textbook's
top-down drawing, with the editor's side handles, made edges run backwards; the layouts of
`gateway-go/internal/bayestemplates/templates.json` and of its browser copy are rewritten by lot **T4**, the graphs
otherwise unchanged, `version` raised.)

### Opening a template (the browser)

`bayesOpenTemplate(id, options, texts)` in `frontend/composables/bayesTemplates.ts` returns a new graph:

1. a deep copy of the template's `graph`;
2. the labels of the person's language (`texts`: `bayes.templates.<id>.labels.<node id>`; every key exists in the five
   languages, a test says so);
3. when `scale` is `outcome` and the person gave the two numbers (owner, 2026-11-23): each `scaled` slot written again as
   an expression of numbers, in the open — `center` → `1200`, `spread` × 2.5 → `2.5 * 300`, `rate` → `1 / 300` — the
   numbers printed by `bayesFormatNumber` (the language's formatter), so the panel can say «2.5 times the spread you
   gave»;
4. for `ts_ar`, the order p asked when it is opened (1 to `bayesTemplateMaxOrder`, 31: the largest p whose expression the
   language still parses within its 1,000-byte limit, the catalogue's `max_lag` being higher; default 2; owner, 2026-11-23;
   Correction of 2026-11-24 from the mockups): the terms `phi<k>` and `lag(y, k)` for k = 1…p.

The result is plain graph JSON; nothing in it says it came from a template (`model-store` keeps that apart). The
function is pure (no Nuxt, no DOM): the tests import it.

## The ten graphs

Names are the nodes' `name`s (the ids are the same words without `_`). «s» is the outcome's typical spread and «m» its
typical value (centre 0 and spread 1 when the person gives nothing). Every real predictor `x` is a `real` data node with
`standardize: true` (answer 6). The data plate is `rows`.

| Template | Data | Parameters and their default priors | Values and likelihood |
|---|---|---|---|
| `glm_linear` | `y` real; `x` real | `alpha` ~ normal(m, 2.5·s); `b_x` ~ normal(0, 2.5·s); `sigma` ~ exponential(1/s) | `eta` = linear predictor (intercept `alpha`, term `x` × `b_x`); `y` ~ normal(`eta`, `sigma`) |
| `glm_logistic` | `y` int binary; `x` real | `alpha` ~ normal(0, 2.5); `b_x` ~ normal(0, 2.5) | `eta` as above; `y` ~ bernoulli_logit(`eta`) |
| `glm_poisson` | `y` int nonnegative; `x` real | `alpha` ~ normal(0, 2.5); `b_x` ~ normal(0, 2.5) | `eta`; `y` ~ poisson_log(`eta`) |
| `glm_negbin` | `y` int nonnegative; `x` real | as `glm_poisson`, and `phi` ~ inv_gamma(0.4, 0.3) | `eta`; `y` ~ neg_binomial_2_log(`eta`, `phi`) |
| `hier_intercepts` | `y`, `x` real; `g` group index → plate «Group» | `mu_a` ~ normal(m, 2.5·s); `tau_a` ~ normal(0, s), lower 0 (a half-normal); `a_g` on «Group» ~ normal(`mu_a`, `tau_a`); `b_x` ~ normal(0, 2.5·s); `sigma` ~ exponential(1/s) | `eta` = linear predictor (intercept `a_g[g]`, term `x` × `b_x`); `y` ~ normal(`eta`, `sigma`) |
| `hier_slopes` | `y`, `x` real; `g` → «Group» | correlated effects on «Group»: components `a_g` (mean `mu_a`, scale `tau_a`) and `b_g` (mean `mu_b`, scale `tau_b`), correlation `rho_g` with LKJ(2); `mu_a` ~ normal(m, 2.5·s); `mu_b` ~ normal(0, 2.5·s); `tau_a`, `tau_b` ~ normal(0, s), lower 0; `sigma` ~ exponential(1/s) | `y` ~ normal(`a_g[g] + b_g[g] * x`, `sigma`) — an expression, because a linear predictor's coefficient is a scalar parameter (`graph.terms`) |
| `hier_nested` | `y`, `x` real; `d` → «District»; `s` → «School», whose parent is «District» | `mu` ~ normal(m, 2.5·s); `tau_d`, `tau_s` ~ normal(0, s), lower 0; `u_d` on «District» ~ normal(`mu`, `tau_d`); `u_s` on «School» ~ normal(`u_d[d]`, `tau_s`); `b_x` ~ normal(0, 2.5·s); `sigma` ~ exponential(1/s) | `eta` = linear predictor (intercept `u_s[s]`, term `x` × `b_x`); `y` ~ normal(`eta`, `sigma`) |
| `ts_ar` (p = 2) | rows a series: `y` real, `t` time | `mu` ~ normal(m, 2.5·s); `phi1`, `phi2` ~ normal(0, 0.5); `sigma` ~ exponential(1/s) | `y` ~ normal(`mu + phi1 * (lag(y, 1) - mu) + phi2 * (lag(y, 2) - mu)`, `sigma`) on the rows that have both lags |
| `ts_random_walk` | rows a series: `y`, `t` | `delta` ~ normal(0, s); `sigma` ~ exponential(1/s) | `y` ~ normal(`lag(y, 1) + delta`, `sigma`) |
| `ts_local_level` | rows a series: `y`, `t` | state `level` (random walk, non-centred): init normal(m, 2.5·s), scale `sigma_level`; `sigma_level` ~ normal(0, s), lower 0; `sigma_obs` ~ exponential(1/s) | `y` ~ normal(`level`, `sigma_obs`) |

The AR template writes the mean `mu` apart from the coefficients (`mu + phi1 * (lag(y, 1) - mu)`), so that the prior on
`mu` is on the outcome's scale and says «the series' level»; the codegen spec's example `c + phi1 * lag(y, 1)` has an
intercept whose meaning depends on the φ's. The golden `ts_ar2` follows this template (a correction to the codegen spec,
«Corrections»).

`glm_linear` in full, at the default numbers:

```json
{ "format_version": 1,
  "plates": [ {"id": "rows", "kind": "rows", "label": "Rows"} ],
  "nodes": [
    {"id": "y", "kind": "data", "name": "y", "label": "Outcome", "plate": "rows", "type": "real"},
    {"id": "x", "kind": "data", "name": "x", "label": "Predictor", "plate": "rows", "type": "real", "standardize": true},
    {"id": "alpha", "kind": "parameter", "name": "alpha", "label": "Intercept", "plate": null,
     "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "2.5"}}}},
    {"id": "bx", "kind": "parameter", "name": "b_x", "label": "Effect of the predictor", "plate": null,
     "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "2.5"}}}},
    {"id": "sigma", "kind": "parameter", "name": "sigma", "label": "Spread around the line", "plate": null,
     "prior": {"dist": "exponential", "args": {"beta": {"expr": "1"}}}},
    {"id": "eta", "kind": "linear_predictor", "name": "eta", "label": "Expected outcome", "plate": "rows",
     "intercept": {"expr": "alpha"}, "terms": [{"data": "x", "coef": {"expr": "b_x"}}]},
    {"id": "lik", "kind": "likelihood", "label": "Outcome given the predictor", "observed": "y", "dist": "normal",
     "args": {"mu": {"expr": "eta"}, "sigma": {"expr": "sigma"}}}
  ],
  "layout": { "alpha": {"x": 0, "y": 0}, "bx": {"x": 0, "y": 110}, "x": {"x": 0, "y": 220}, "sigma": {"x": 0, "y": 330},
              "eta": {"x": 240, "y": 110}, "y": {"x": 240, "y": 330}, "lik": {"x": 480, "y": 220} } }
```

One predictor in every template: the panel of the linear predictor says how to add another (a data node and a term),
and the assistant (`assistants`) can do it. A second predictor would add nothing the first does not show.

## The default priors, and where each comes from

The rule (the map): **weakly informative, on the scale of standardized predictors, never changed by the data**. Each
default has its source in the file (`sources`) and in the docs (`docs`' page); the explanations quote the numbers, never
a judgement.

| Prior | Default | Source (checked online, 2026-11-23) |
|---|---|---|
| Intercept, real outcome | normal(m, 2.5·s) | the rstanarm documentation's default for the centred intercept, «Normal(m_y, 2.5 · s_y)» with m_y the outcome's mean and s_y its sd ([priors](https://mc-stan.org/rstanarm/articles/priors.html)); here m and s are **typed by the person**, never read from the data (owner, 2026-11-23) |
| Coefficient of a standardized predictor | normal(0, 2.5·s) (s = 1 for the logit and log links) | the same page: «β_k ∼ Normal(0, 2.5 · s_y/s_x)», s_y = 1 outside the Gaussian family; with s_x = 1 (standardized) it is 2.5·s |
| Intercept, logit and log links | normal(0, 2.5) | the same page (m_y = 0 and s_y = 1 outside the Gaussian family) |
| Residual spread | exponential(1/s) | the same page: «an exponential distribution with rate 1/s_y» |
| A group-level spread (`tau_*`, `sigma_level`) | normal(0, s), lower 0 (a half-normal) | the Stan wiki's prior recommendations: «something like half-normal(0,1) or half-t(4,0,1) as default choices» ([Prior Choice Recommendations](https://github.com/stan-dev/stan/wiki/Prior-Choice-Recommendations)), on the outcome's scale |
| Negative binomial's `phi` | inv_gamma(0.4, 0.3) | the same wiki page, for `neg_binomial_2_log`'s shape: «phi ~ inv_gamma(0.4, 0.3)» |
| Correlation of correlated effects | LKJ(2) | the same wiki page: LKJ(1) «is uniform on the correlations», LKJ(2) «goes to 0 at the boundary of positive definiteness»; 2 keeps correlations of ±1 out when the groups are few. Spec decision, 2026-11-23, owner may revisit (1 is the page's «nowadays we usually use») |
| AR coefficients | normal(0, 0.5) each | **spec**: the wiki page gives none for AR coefficients («Penalised complexity priors are available for auto-regressive processes»); 0.5 puts 95 % of each coefficient's prior mass between −0.98 and 0.98. Stationarity is **not** imposed; the panel says so |
| Random walk's drift | normal(0, s) | **spec**: a step of the size of the outcome's spread at most a few times |
| Local level's first value | normal(m, 2.5·s) | as the intercept |

The rstanarm package is cited for its documented numbers only; no code of it is used. (Its licence is not a question
here: nothing of it is distributed.)

**What a default says, in the panel** (`bayes.templates.<id>.why.<node>`, five languages, facts, a number from the
preview where one is needed): «Before seeing the data, the model says the effect of one standard deviation of the
predictor is between −4.1 and 4.1 nine times in ten. This is the default the rstanarm documentation gives for a
standardized predictor: normal(0, 2.5).» Never «weak», «vague», «reasonable», «informative». The quantiles come from
`prior-preview` at the time the panel is shown (they change with the numbers), never written in the locales.

## The explanations (answer 1)

Per template, in `frontend/i18n/locales/*.gen.ts` under `bayes.templates.<id>`:

| Key | What |
|---|---|
| `title` | «Linear regression» |
| `what` | two sentences: what it is for, with an example on a business table («monthly sales from the advertising spend») |
| `data` | what each data variable must hold, for the flow node's binding («`y`: a number per row»; «`g`: the group of each row, text or a number») |
| `labels.<node>` | the node's label |
| `why.<node>` | what the node is and what its default prior says, as above |
| `change.<topic>` | how to change it: add a predictor, change the likelihood, the centred form (answer 4), add a lag, several series (`series.by`) |

A test checks that every key exists in the five languages, that every node of every template has `labels` and `why`, and
that no text holds a word of the judgement list.

## Reference values

The map's «measure first»: posterior means and standard deviations from an independent run outside Tabularia, within
Monte Carlo error. Concretely, per template:

1. **Reference data**: `testdata/reference/<id>.csv`, at most 2,000 rows, drawn by `make_reference.py` (NumPy's PCG64,
   seed 20261123 + the template's position) from **known parameters** written in the script (a real slope of 0.8, 12
   groups, a series of 400 points…); the predictors are drawn on their own scale (a mean of 50, an sd of 10), so that the
   standardization is exercised.
2. **The reference posterior**: a Stan program **written by hand** for the same model (`testdata/reference/<id>.stan`,
   the textbook form, by a person, not by `bayescodegen`), compiled and run **in the group's image** with its CmdStan
   (2.40.0, `bayes-image`) — no Tabularia code: the script standardizes the predictors itself and writes the data file —,
   4 chains × 2,000 draws after 1,000 of warm-up, fixed seed. For each scalar quantity: the mean, the sd and their MCSEs
   (Vehtari et al. 2021, as `bayes-diagnostics`), written to `<id>.expected.json` with a header (CmdStan version, image
   digest, seeds, the script's sha256).
3. **Tabularia's posterior**: the program `bayescodegen` generates from the template, run the same way (same data file
   shape, same CmdStan, other seeds).
4. **The comparison**: for each quantity and each of mean and sd, z = (Tabularia − reference) / √(MCSE_T² + MCSE_R²),
   the two-sided p-value from the normal distribution, **Holm-adjusted over every comparison of the ten templates**,
   each compared with the suite's **alpha 0.001** (the alpha of the catalogue's and the preview's suites). A test of the
   software, not a verdict of any report.

The draws are read from the CSV **line by line** and accumulated (Welford's mean and variance, the batch means of the
MCSE), never loaded whole (the owner's rule of 2026-11-22, in the small: the files are a few MB). The runs are heavy
for the development machine (ten compiles of about 20–60 s each, `bayes-image`'s measures): by hand, one at a time, never
in CI; CI checks only that `<id>.expected.json` and the comparison's last record exist and name the current template
version. A template whose `version` changes without new reference values fails that check.

Once `bayes-runner` exists, `flow-node`'s trial on kind runs the ten templates on the same CSVs through the real node
and compares its summary with `<id>.expected.json` by the same test (the map's step 8).

## Memory

Templates are metadata (ten graphs, a few KB each), held whole by design in the gateway and the browser. The reference
runs follow the rule above; the CSVs of reference data are small by construction.

## Tech stack

- **Go** 1.27, standard library only (`embed`, `encoding/json`); depends on `internal/bayesgraph`, `internal/bayescodegen`
  (tests). No new module.
- **TypeScript** in the Nuxt 3.21 frontend; no new dependency; tests with `node --test` under Node 22.
- **Python** 3.11+ standard library for the copy script; the reference script with the image's `numpy==2.5.5` (run in the
  image, by hand).

## Commands

```bash
cd gateway-go && go test ./internal/bayestemplates/...                       # decode, validate, golden, fixtures
python3 .github/scripts/bayes_templates.py            # copies templates.json to frontend/assets/bayes-templates.json
python3 .github/scripts/bayes_templates.py --check    # fails if the copy differs by a byte
cd frontend && node --experimental-strip-types --no-warnings=ExperimentalWarning --test tests/bayes-templates.test.mjs
# reference values, by hand, alone, inside the group's image (one heavy job at a time)
python3 gateway-go/internal/bayestemplates/testdata/reference/make_reference.py --template glm_linear
```

## Project structure

```text
gateway-go/internal/bayestemplates/
  templates.go          the embedded file, List, Get, the checks of the file's own keys
  templates.json        the ten templates (the one source)
  templates_test.go     decode, validate, names, layout, golden, opened fixtures, versions
  testdata/opened/<id>.<case>.json      opened graphs (scaled numbers, AR orders), written by the TS test, reviewed
  testdata/reference/   make_reference.py, <id>.stan (by hand), <id>.csv, <id>.expected.json, compare.json
frontend/assets/bayes-templates.json    the copy
frontend/composables/bayesTemplates.ts  bayesTemplates(), bayesOpenTemplate()
frontend/tests/bayes-templates.test.mjs
frontend/i18n/locales/*.gen.ts          bayes.templates.*
.github/scripts/bayes_templates.py, .github/scripts/test_bayes_templates.py
.github/workflows/bayes-catalog.yml     + the templates' copy check
```

## Code style

An embedded snapshot, served as it is (verified, `gateway-go/internal/bayescatalog/catalog.go`):

```go
//go:embed catalog.json
var raw []byte

// Raw is the snapshot, byte for byte.
func Raw() []byte { return append([]byte(nil), raw...) }
```

This module's test in the same voice (a sketch; lot T1 writes it): a template is a model only when the validator and the
generator both say so, and the program is the golden one byte for byte.

```go
func TestTemplatesGolden(t *testing.T) {
	for _, tpl := range List() {
		g, findings := bayesgraph.Decode(tpl.Graph)
		if errs := bayesgraph.Errors(findings); len(errs) > 0 {
			t.Fatalf("%s: the template does not decode: %v", tpl.ID, errs)
		}
		model := bayesgraph.Model(g) // the validated model; no finding of severity error
		prog, err := bayescodegen.Generate(model, nil)
		if err != nil {
			t.Fatalf("%s: %v", tpl.ID, err)
		}
		want := mustRead(t, "../bayescodegen/testdata/golden/"+tpl.Golden()+".stan")
		if !bytes.Equal(prog.Text, want) {
			t.Errorf("%s: the program is not the golden one (BAYES_GOLDEN_WRITE=1 in bayescodegen, reviewed)", tpl.ID)
		}
	}
}
```

## Testing strategy

All inputs are ten small graphs; no data in the Go and Node suites.

- **The file**: decoded strictly (unknown keys refused); ids unique and those of the table; every `scaled` entry names an
  existing node and slot holding a number; every `sources` key exists; `version` ≥ 1.
- **Each template** (`templates_test.go`): `Decode` gives no finding; `Validate` gives no error and exactly the expected
  warnings (none is expected); every node is named and its name passes the identifier rule (the mapping table has no
  `renamed`); every group plate is `non_centred`; every real predictor has `standardize: true`; the layout's boxes do not
  overlap.
- **Golden** (`TestTemplatesGolden`): `Generate` of each template equals the codegen golden case byte for byte; the golden
  graph file of `bayescodegen` for these cases **is** the template's graph (a test compares them), so the two cannot drift.
- **Opening** (`bayes-templates.test.mjs`): `bayesOpenTemplate` with no option returns the template's graph with the
  locale's labels; with the outcome's numbers (0/1, 1200/300, −5/0.02) and the AR orders (1, 2, 5, `bayesTemplateMaxOrder` = 31;
  32 refused with a RangeError) it writes the expected slots (fixtures `testdata/opened/`, reviewed); the mirror (`bayesGraph.ts`) finds no
  error on any opened graph. **Go reads the same fixtures** and validates and generates each without error.
- **Words**: every key in five languages; no judgement word; every `{placeholder}` the same in the five.
- **Copy**: `bayes_templates.py --check` in CI.
- **Reference values** (lot T3, by hand, recorded): the comparison above, its p-values and the run times in «As built».
- **Regression**: `bayesgraph`'s and `bayescodegen`'s suites (a template change that breaks a golden file is seen there).

## Boundaries

- **Always**: a template is a valid graph of the current format; its program is a golden file; every default prior has
  its source; every text in five languages, facts only; `version` raised with every change of a graph, with new reference
  values.
- **Ask first**: a new template, a new family, a change of a default prior or of its source, a template that is not
  non-centred, a key added to the graph format for templates.
- **Never**: a prior computed from the data (answer 6, the graph spec's `graph.prior_data`); a template the validator
  refuses; an explanation that judges a prior; a template saved models depend on (they hold a copy); the reference
  posterior computed with Tabularia's own code.

## Success criteria

1. The ten templates decode, validate with no error and generate their golden programs byte for byte.
2. Every node of every template has a label and an explanation in five languages, and every default prior a source.
3. Opened with the outcome's numbers and every AR order of the fixtures, every graph validates in Go and in the mirror.
4. `bayes_templates.py --check` passes in CI.
5. For every template, Tabularia's posterior and the hand-written program's agree by the test of «Reference values»
   (Holm, alpha 0.001), recorded with the times.
6. No new module in `go.mod`, no new package in `package.json`.

## Lots

| Lot | Objective | Files | Accepted when | Depends on | Agent |
|---|---|---|---|---|---|
| **T0** | The file with the ten graphs and their layouts, decoded and structure-checked; the copy script; `bayesTemplates.ts` with `bayesOpenTemplate` (labels, numbers, AR order) and its fixtures | `templates.json`, `templates.go`, part of the tests, `bayes_templates.py`, `bayesTemplates.ts`, `bayes-templates.test.mjs` | `Decode` and `CheckStructure` clean; copy check green; Node tests green | `model-graph` M0 (done) | sonnet |
| **T1** | Full validation and the golden programs; the codegen golden graphs replaced by the templates' | `templates_test.go`, `bayescodegen/testdata/golden/` (the ten cases, reviewed) | criteria 1, 3 | `model-graph` M2 and M4; `stan-codegen` S0–S2 | sonnet |
| **T2** | The explanations and labels in five languages, the sources in the docs' draft | locales, `bayes-templates.test.mjs` (words) | criterion 2 | T0 | haiku |
| **T3** | Reference data, hand-written programs, the comparison, «As built» | `testdata/reference/` | criterion 5 | T1, `bayes-image` B0 | sonnet (by hand, alone: heavy) |
| **T4** | The ten layouts rewritten **left to right** (Correction of 2026-11-24 from the mockups): sources to the left of their targets, hyperparameters in a column at the left, likelihood at the right, boxes not overlapping at 1280 px; each template's `version` raised; the browser copy regenerated | `templates.json`, its copy, `bayes-templates.test.mjs` (a test: every edge of every template goes rightwards) | the layout test green; the ten templates seen on the canvas at 1440 and 1280 px | T0; `graph-editor` G0 | haiku |

T0 is light and can start now (the graph's structure is built): it is what `graph-editor`'s first screen (lot G0) loads.
T1 waits for the validator and the generator; T3 for the image.

## Corrections made to the approved specs (2026-11-23)

Applied by the owner's decision of 2026-11-23, each marked there «Correction of 2026-11-23 from the step-3/4 specs»:

- `bayes-stan-codegen.md`, «The golden corpus»: `ts_random_walk` pins `lag` with a drift (`lag(y, 1) + delta`, no state),
  `ts_local_level` the non-centred latent state with a normal likelihood around it; the AR golden follows this template
  (`mu + phi1 * (lag(y, 1) - mu) + …`); the golden graphs of the ten template cases are **the templates'** (lot T1), the
  generator's bytes changing only by the priors' numbers, `CodegenVersion` unchanged.
- `bayesian-editor-map.md`, `templates`' «measure first»: on **synthetic reference data with known parameters**, the
  sample tables' flows being `docs`' lot.

## Decisions of 2026-11-23 (the answers to this spec's questions)

| Question | Decision | By |
|---|---|---|
| The priors on the outcome's scale (linear, the three hierarchical, the three series) | **Asked when the template is opened**: «about what value is the outcome usually?» and «about how far from it does it go?» (centre m and spread s, both optional, 0 and 1 when not given); the template writes its priors from them as visible expressions (`2.5 * 300`, `1 / 300`): constants the person typed, never data (answer 6, `graph.prior_data`) | Owner, 2026-11-23 |
| The order of the AR template | **Asked when the template is opened**: p from 1 to the catalogue's `max_lag`, default 2, the template written for that p by `bayesOpenTemplate`; the golden stays p = 2, the other orders covered by the opened fixtures | Owner, 2026-11-23 |

## Decisions of 2026-11-24 (mockups)

| Question | Decision | By |
|---|---|---|
| The templates' layout | **Left to right**, as the flow editor (the mockups: a top-down drawing with side handles makes edges run backwards); rewritten by lot T4 | Technical, 2026-11-24 |
| The AR order's bound | **1 to 31** (`bayesTemplateMaxOrder`), the dialog saying «from 1 to 31» | Technical, 2026-11-24 |

## Open questions

None.
