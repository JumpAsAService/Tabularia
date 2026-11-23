# Capability map: the Bayesian model editor

Status: **phase 0 of the spec-driven method, written on 2026-11-23; interview answered on 2026-11-23;
approved by the owner on 2026-11-23.** It is the map of the parts of the visual editor of Bayesian models on Stan that the
owner asked for on 2026-11-22 and shaped on 2026-11-23. It holds the modules with their stable ids, their
dependencies and the order of construction, the large choices and the risks of each module, what must be
measured before its spec, the results of the research with their licences and sources, and the owner's answers
to the interview («Interview answers»), which came **before** any module spec. The answers moved one boundary: a
module `expression-language` was added between `distribution-catalog` and `model-graph` (answer 3). Each module
spec is written by module id (`docs/specs/bayes-<module-id>.md`). Approved on 2026-11-23:
`bayes-distribution-catalog.md`, `bayes-expression-language.md`, `bayes-model-graph.md`, `bayes-image.md`.

Words, as in the other specs: **checked online** = read on the project's own page, registry or repository on
2026-11-23; **verified** = read in this repository's code on 2026-11-23; **assumed** = known from the
literature or a library's documentation but not run here, to be checked by the module that first depends
on it.

It reuses the architecture of `docs/specs/statistical-models.md` (**the models spec**): a model group built
only when an administrator enables it, its recipe pinned by version and by sha256 («The supply chain»), its
self-test, the Job on Kubernetes with internet access off, uid 10001, read-only root, `/tmp` an `emptyDir`,
the ten-column results table, the layouts and their five languages, the explanation of the method of
`docs/specs/report-explanations.md`, Run in the editor on the development sample, MLflow logged by the Go
gateway only. Where this map says nothing, the models spec holds.

## Objective

A person draws a Bayesian model as a graph — data, parameters, priors, hyperpriors, the likelihood, the
groups of a hierarchical model — and sees, while typing a prior's numbers, the distribution that prior and
its hyperpriors imply. The empty canvas proposes the templates; the free model is one click away; every
choice is explained for a reader who does not know Bayesian statistics (answer 1). In the free model the
person may **type expressions** (`a + b * x`), read by a grammar of Tabularia's own (answer 3). The model is
saved as an object of a folder, with versions and the folder's permissions, like a saved script. Tabularia
generates the Stan program from the graph (shown, never edited by hand). In a flow, a node **Bayesian model**
chooses a saved model, binds its data variables to columns, and runs NUTS as a Kubernetes Job; it yields a
report with the diagnostics and the posterior summary as measures and facts, the predictive checks as charts,
and, on request, the posterior draws in a parquet table for the nodes after it.

## The owner's decisions (2026-11-23, with the addition and the interview of the same day)

| Question | Decision |
|---|---|
| Where it lives | **A space of its own**, as Scripting: models are drawn and saved there, with versions and folder permissions. In a flow, a node **Bayesian model** chooses a saved model and binds its variables to columns |
| Code | **Graph only.** The Stan program is generated from the graph; it can be shown, it is never edited by hand |
| Families of the first release | **GLM** (linear, logistic, Poisson, negative binomial); **hierarchical** (intercepts and slopes per group, nested levels, hyperpriors); **time series** (AR, random walk, simple state space); **free model** (distribution and transformation nodes wired at will, with typed expressions: answer 3) |
| Outputs | A **report with diagnostics** (posterior summary, R-hat, ESS, divergences, trace and density charts, the explanation of the method as in the other reports) and the **posterior draws in the parquet** on request (answer 14). **No LOO/WAIC comparison** for now |
| Predictive checks (added 2026-11-23, shaped by answers 12 and 13) | **Prior predictive checks** only as a **Job** (`fixed_param`); **posterior predictive checks** only as **charts** (overlaid densities, histograms of T(y_rep) with T(y) marked): **no number** such as a posterior predictive p-value or probability. **Predictions on new rows of the flow stay out** («Out of scope») |
| Interactive priors | The prior's chart is **computed in the browser** while the values change. With hyperpriors the hyperparameter is drawn first, then the parameter, so the chart is the true marginal. Light; **no Job**. The browser shows **only the marginals of the priors**: no prior predictive in the browser (answer 13) |
| Memory (rule of 2026-11-22; owner, 2026-11-23: «attenzione a non caricare tutto in memoria, l'output deve essere un parquet in append in base ai campionamenti») | Never everything in RAM, not even with Stan. **The output is a parquet appended as the sampling goes**: CmdStan's chains write their CSV to `/tmp`, and the runner follows each file while the chains run, reading the new draws as they arrive and appending them to the draws parquet in blocks (one row group per block of draws of one chain, with `chain` and `draw`), each block freed once written; no step waits for the end of sampling to read a chain, no function loads a whole CSV or a whole chain (`read_cmdstan_csv` and the like are never used), and the part of a CSV already converted is not read again. A test measures the memory peak and shows it does not grow with the number of draws. Summaries, R-hat and ESS in blocks or with accumulators; charts and report on thinned samples. Replicated data (y_rep) written in blocks and summarized with accumulators, never all in RAM |
| Verdicts (rule of 2026-11-20) | A verdict comes only from a test with a p-value against the node's alpha. R-hat, ESS and divergences are **measures or facts**, each with a sentence on what it measures and the reference values of the literature **cited as such**; **no reference option in the node**, **no option that stops the step on divergences** (answer 11). No posterior predictive p-value at all (answer 12) |
| Execution | Kubernetes, a model group with an image of its own that an administrator enables; recipes with hashes; Jobs without network, non-root, read-only root; the runner in **Python** (answer 15) |
| Licences, languages, documents | Licences cited, not interpreted; no competitor named; user texts in five languages; no local path in any file |
| Security | «Fundamental» (owner). Generated code is compiled and executed: injection through names, labels, **typed expressions** and transformations, compilation in the Job (time, memory, the cache of compiled binaries), the sandbox. The expression language widens the surface: it gets **a security review of its own** (answer 3) |

The other answers (users, distributions, plates, names, data binding, versions, compile, sampler, limits,
outputs) are in «Interview answers».

## What exists and is reused (verified)

| Need | Already there | What the editor adds |
|---|---|---|
| A folder object with versions | `gateway-go/internal/versioned` (reports and dashboards: the object, its versions, VIEW on the folder, 404 to whoever cannot see it); `gateway-go/internal/savedscripts` (saved scripts: versions, restore, «used by N flows», the schedule switched off when someone else changes a script a flow uses by reference) | The kind `bayes_model` with its versions |
| A space of its own in the shell | `frontend/pages/scripting.vue` (a tab shown only to whoever may use it, hidden on Compose), `frontend/pages/script.vue` (the object's page) | A tab or a section «Bayesian models» and a page `bayes-model.vue` |
| A graph canvas | `@vue-flow/core` (`^1.42.0` in `frontend/package.json`, 1.48.2 installed) with background, controls, minimap, node resizer, used by `FlowEditor.vue` | A second canvas, with plates (parent nodes) |
| Charts in the browser | `echarts` 6.1 and `vue-echarts` 8.0 | The prior's density and histogram |
| An expression language checked twice | The reports' language: `backend/app/reports/expressions.py` alone judges an expression (`MAX_EXPRESSION_CHARS` 4000, `MAX_DEPTH` 64, `_HINTS` for operators of other languages); `frontend/composables/reportExpressions.ts` checks what can be told while typing | The same split for `expression-language`, with the authority in Go |
| Test cases shared by two implementations | `backend/tests/fixtures/models/params_cases.json`, read by the engine's tests and by `gateway-go/internal/modelcatalog/modelcatalog_test.go` («word for word») | The corpora of `expression-language` and `model-graph`, read by the Go and the TypeScript tests |
| A catalogue copied to the components | `.github/scripts/model_catalog.py` (copies a snapshot byte for byte, `--check` in CI through `.github/workflows/model-catalog.yml`), `gateway-go/internal/modelcatalog` (embedded copy, `Compact()` for the Jobs' ConfigMap) | The same for `distribution-catalog` |
| A step that is a Job | `jobs_scripts.go`, `jobs_models.go`, the resolver's `StepNodeTypes` with `model` and `model_predict`, the trial (`POST /scripting/flows/{id}/nodes/{node}/run`) | `bayes_model` as a step node |
| A model group | `gateway-go/internal/modelgroups` (recipes, runner files in the ConfigMap, self-test, `RunnerHash`), `scriptimage` (managed images; a recipe may already pin `wheels` and `weights` by URL and sha256, as `foundation_forecast` does) | A group `bayes` whose image holds CmdStan and a C++ toolchain |
| Streaming to parquet | `docs/specs/garch-risk.md`, «The Parquet file of the paths» and «The measures, without holding the paths»: one writer, one row group per batch, two passes, accumulators, disk and time projected after the first batch, a refusal leaves no partial result | The same discipline for the draws and y_rep |
| Report and explanation | `backend/app/engine/models/layouts/common.py` (`reading_under`, `how_computed`), `report_texts/*.json` in five languages, the engine-only chart kind `xy`, `density` | Layouts `bayes_*` |
| Assistants | `gateway-go/internal/aiprompts`, `internal/mcp/tools.json`, the flow assistant | A paragraph for the node; an assistant in the editor |

**A fact that matters for security (verified, `docs/specs/script-node.md`, «one key for the installation»):**
every Script Job holds the same key of the scripts bucket, so **any user script can read and write any key
there**. A compiled Stan binary must therefore never be trusted because of where it sits in that bucket
(«`stan-compile`»).

## The modules

| Module id | Responsibility | Depends on |
|---|---|---|
| `distribution-catalog` | The distributions as data: parameters, their constraints, the support, the Stan names (`_lpdf`, `_lpmf`, `_rng`, the `_logit`/`_log` forms), the link functions, the functions and operators the expression language may use, Stan's reserved words, the limits of the language and of the graph; the browser's bindings to stdlib; copied to the interface and the gateway (and from the gateway to the runner) from one snapshot, as `catalog.json` is | — |
| `expression-language` | The typed expressions of the free model (answer 3): the grammar (EBNF), the lexer and the parser into an AST, the types (real, integer, per-plate vector, group index) and the shape rules, the whitelist of operators and functions taken from `distribution-catalog`, the limits of length, depth and size, no identifier that is not a node of the graph; the emission of an expression's Stan text **from the AST only**; one authoritative parser in Go, a mirror in TypeScript for instant feedback only | `distribution-catalog` |
| `model-graph` | The canonical JSON of a model: node kinds (data, parameter with its prior, transformed, linear predictor, likelihood, latent state, correlated group effects), plates (rows, groups and their nesting, rows as a series), names and labels, the format version; the validation rules (types, shapes, supports, a DAG, every parameter with a proper prior, plates consistent); one authoritative validator in Go, a mirror in TypeScript for instant feedback | `distribution-catalog`, `expression-language` |
| `bayes-contract` | The runner protocol of the group: `task.json` (modes `compile`, `prior_predictive`, `sample`, `selftest`), the data file's shape, the draws parquet's schema, the rows of the ten-column table (`summary`, `diagnostic`, `sampler`, `ppc`, `prior_check`, `point`, `curve`, `reading`), the error codes and refusals, the card | `model-graph` |
| `stan-codegen` | Graph → Stan program, in the Go gateway: deterministic (same graph, same bytes), safe by construction (no user text reaches the program; expressions through `expression-language`'s emitter), the data block from the bound variables, non-centred or centred hierarchical forms, the vectorized likelihoods, the mapping table identifier ↔ label; an extension point for the generated quantities of `predictive-checks` | `model-graph`, `expression-language`, `distribution-catalog` |
| `prior-preview` | In the browser, in a Web Worker: densities, quantiles and seeded draws for every catalogue distribution usable as a prior; ancestral sampling of the graph's parameters (hyperparameter first, then the parameter: the true marginal); **only the marginals of the priors**, no prior predictive (answer 13); no Job, no request | `model-graph`, `distribution-catalog` |
| `templates` | GLM (four families), hierarchical (varying intercepts, varying slopes, nested levels), time series (AR(p), random walk, local level) as predefined graphs, with their default priors written in the open; each checked through `stan-codegen` and the reference values | `model-graph`, `stan-codegen` |
| `model-store` | The saved object `bayes_model`: folder, versions (graph, generated program, its sha256, the mapping table), restore, permissions (VIEW, EDIT, RUN), Explore and search, «used by N flows», audit, the migration | `model-graph`, `stan-codegen` |
| `graph-editor` | The space and the canvas: palette, nodes, edges, plates, the inspector of a node with `prior-preview` beside its numbers, the expression fields with the mirror's instant feedback, the validation shown on the node, the read-only Stan code with its mapping, templates as the empty canvas's proposal and the free model one click away, the explanations for a reader who does not know Bayes, keyboard access (Mac and laptops without F keys), five languages | `model-graph`, `expression-language`, `distribution-catalog`, `prior-preview`, `templates`, `model-store` |
| `bayes-image` | The group `bayes`: the recipe (CmdStan's source tarball by URL and sha256, the toolchain from the distribution's signed archive, the runner's packages by hash), CmdStan built once in the image with its precompiled header, the self-test, the admin card (enable, resources, compile limits) through the existing section | — (the existing `modelgroups` and `scriptimage`) |
| `predictive-checks` | The **prior predictive check** as a Job only (Stan `fixed_param`, ancestral draws in `generated quantities`); the **posterior predictive check** as charts only (y_rep in `generated quantities`, test statistics T(y_rep) against T(y), the binned densities of y_rep); the generated quantities it adds to the program and the accumulators it gives the runner; the golden test of the priors' marginals (browser draws against `fixed_param` draws) | `stan-codegen`, `bayes-contract`, `prior-preview` |
| `stan-compile` | Program → executable: a compile Job (stanc with fixed flags, then the C++ compiler), started in the background when a version is saved, the run as the fallback (answer 8); the cache of binaries keyed by the hash of program, CmdStan version, flags and architecture, least recently used evicted under a limit the administrator sees; the integrity of a cached binary (its sha256 recorded by the gateway, checked by the runner before exec), the time and memory limits of a compile | `bayes-image`, `stan-codegen`, `predictive-checks`, `model-store` |
| `bayes-diagnostics` | From the draws parquet, by blocks of columns: rank-normalized split R-hat, bulk and tail ESS, MCSE, the posterior summary (mean, sd, quantiles), divergences, tree depth reached, E-BFMI, prior-to-posterior contraction; thinned draws for trace, rank and pair charts; all as measures and facts (answer 11) | `bayes-contract` |
| `bayes-runner` | The sampling Job: input parquet → the data file in a stream; the executable checked and run (`sample`, `num_chains`, seeds, threads); progress read from stdout, time and disk projected, refusal with a sentence before any partial result when a projection passes a limit (answer 10); CSV → parquet in blocks of draws, never a whole chain in memory; calls `bayes-diagnostics` and the accumulators of `predictive-checks`; writes the ten-column results and the draws output | `bayes-contract`, `bayes-image`, `stan-compile`, `bayes-diagnostics`, `predictive-checks` |
| `bayes-report` | The layouts: summary and fit, diagnostics (measures and facts with what each measures and the literature's reference values cited as such, no verdict word, no reference option), trace, rank and density charts on thinned draws, the predictive checks as charts (overlaid densities, T(y_rep) against T(y)), the prior-to-posterior overlay, «How this report was computed», five languages, PDF and Excel | `bayes-contract`, `bayes-diagnostics`, `predictive-checks` |
| `flow-node` | The node **Bayesian model**: the saved model and its pinned version with the notice «a newer version exists» (answer 7), the binding of its data variables to columns (numbers, integers, group indices and treatment-coded categories from a text column, the series' time column; predictors standardized by default: answer 6), the sampler options (answer 9), the limits (answer 10), Run as a trial on the development sample, the resolver boundary, the fingerprint and «not up to date», the output (summary by default, draws on request: answer 14), the permissions and the schedule authority, the Report tab | `model-store`, `bayes-runner`, `bayes-report` |
| `bayes-mlflow` | When the node names an MLflow connection: parameters, summary metrics, the program and the report as artifacts, **never the draws** (answer 14), logged by the gateway as for the other groups | `flow-node`, `bayes-contract` |
| `assistants` | The editor's assistant (explains a node, proposes priors on the scale of the standardized predictors, reads divergences and proposes the non-centred form), the flow assistant's paragraph, the MCP tool list; names, labels and expressions are data | `graph-editor`, `flow-node`, `templates`, `bayes-diagnostics` |
| `docs` | The capability page (how to draw, expressions, priors, hierarchical, checks, reading the diagnostics), the administrator's guide (enabling, resources, compile cache), the licences page, five-language texts checked | every module |

**Why the two predictive checks are where they are.** Both checks are made of the same three pieces: the
draws come from `generated quantities` of the same program (code), they are accumulated while the CSV is
streamed (runner), and they are drawn in the report (report). Spreading them inside `stan-codegen`,
`bayes-runner` and `bayes-report` would hide one capability in three modules; one module `predictive-checks`
owns the generated quantities, the accumulators and the rows they produce, and the three others call it
through an interface. Since answer 13 there is **no browser half**: the prior predictive check is a Job only,
and `prior-preview` draws only the parameters. The two still share one definition of the priors: a golden test
of `predictive-checks` draws the **marginals of the parameters** of the same graph in the browser's code (run
under Node in the test suite) and with `fixed_param`, and compares each parameter's two samples by a
two-sample test with a p-value against the suite's alpha.

**Why `model-graph` and `bayes-contract` are modules and not files.** Every other module reads them; the
models spec built its «piece 0, the common contract» first and alone for the same reason.

**Why `expression-language` is a module of its own (answer 3).** Typed expressions are the one place where text
written by a person turns into code that is compiled and executed. Putting the grammar inside `model-graph`
would hide the attack surface in a module about shapes and plates; a module of its own has its own threats
table, its own fuzzing, its own security review, and one interface that `model-graph` (types),
`stan-codegen` (emission) and `graph-editor` (feedback) call.

## Order of construction and what runs in parallel

```text
step 0 (measure first, in parallel)   bayes-image (spike: build, compile cost)   distribution-catalog
step 1 (in sequence)                   expression-language → model-graph
step 2 (parallel)                      bayes-contract   stan-codegen   prior-preview
step 3 (parallel)                      templates   model-store   bayes-diagnostics (on synthetic draws)   predictive-checks
step 4 (parallel)                      graph-editor   stan-compile
step 5                                 bayes-runner
step 6 (parallel)                      flow-node   bayes-report
step 7 (parallel)                      bayes-mlflow   assistants   docs
step 8                                 the trial on kind, the security review, the final review
```

- **Step 0 is a measurement, not a feature.** The image spike answers the questions on which half of the
  map rests (compile time and memory, whether CmdStan compiles with a read-only root and `/tmp` only, the
  image's size); the catalogue is pure data and blocks the language and the graph.
- **Step 1 is sequential.** `expression-language` can start as soon as the catalogue's lists of functions,
  operators, reserved words and limits are fixed (the catalogue's first lot); `model-graph` types its
  expressions through it. Its **security review** (opus, a fresh context, given the code and the threats
  table, not this map's conclusions) runs when it is built, before `stan-codegen` uses its emitter, and again
  in step 8.
- `prior-preview` and the canvas of `graph-editor` are frontend work that never waits for Kubernetes; the
  group's image is cluster work that never waits for the canvas. Two lanes can go side by side, under the
  rule «one heavy job at a time» on the development machine (the image build and a compile benchmark are
  heavy; the frontend tests are not).
- `bayes-diagnostics` is built and tested on synthetic draws parquets (known R-hat and ESS from the
  literature's examples), before any real sampling.
- `stan-codegen` is the critical path: templates, store, compile and checks all wait for it. Its golden
  tests (graph → bytes) come first.

## The modules, one by one

Each entry: **choices** (the large ones; the rest belongs to the module's spec), **risks**, **measure first**.

### `distribution-catalog`

- **Choices.** One source of truth for the browser, the codegen and the runner. Every distribution declares
  its parameters with constraints (`sigma > 0`, `0 < p < 1`), its support, its Stan name, its `_rng`, and
  whether the browser draws it (density, quantile, draw through stdlib, confirmed by answer 2). A parameter's
  declared bounds (`real<lower=0>`) and a prior on a wider support make a **truncated** prior in Stan (a
  half-normal); the catalogue states the rule once, and the browser and the codegen both follow it. The
  distributions are those of answer 2 (b): the minimum of the four families, plus LKJ and the multivariate
  normal. The functions and operators the expression language may use, Stan's reserved words and the limits
  of the language and of the graph are catalogue data too. Spec: `bayes-distribution-catalog.md` (draft).
- **Risks.** The half-normal trap: if the browser draws from a normal and Stan samples a half-normal, the
  preview lies; and when a truncated prior's arguments are parameters, the truncation must be written in the
  program, or Stan's joint is not the browser's ancestral one. Parametrizations differ between libraries
  (Stan's `gamma(alpha, beta)` is shape and rate; `neg_binomial_2(mu, phi)`; stdlib's Student-t is the
  standard one only): one test per distribution compares the browser's numbers with a witness at fixed
  points, and `bayes-image`'s self-test compares the same points with Stan's own functions.
- **Measure first.** Done for this map (checked online, 2026-11-23): the stdlib packages, their versions,
  licences, bundle size («Research»).

### `expression-language`

- **Choices.** A closed grammar of Tabularia's own (EBNF in the module's spec): numbers, names of graph nodes,
  `+ - * / ^`, unary minus, parentheses, calls of the catalogue's whitelisted functions, indexing by a group
  index, `lag(x, k)` on a series; nothing else — no strings, no comments, no statements, no user-defined
  functions (answer 3). Typed: real, integer, per-plate vector, group index; the shape rules say what may meet
  what in which plate. The input is ASCII only; limits of bytes, depth and nodes are checked before the work
  they bound. The Stan text of an expression is **emitted from the typed AST only**, fully parenthesized,
  every identifier given by the caller (`stan-codegen`), every number re-printed from its double; the
  person's text never reaches the program. The parser in Go (`gateway-go`) is the authority; a TypeScript
  mirror gives instant feedback, and a shared corpus keeps the two equal. Spec: `bayes-expression-language.md`
  (draft).
- **Risks.** It is new attack surface (owner): injection into the Stan program, resource exhaustion in the
  parser, confusable characters. Answered by the grammar, the emitter, the limits and fuzzing («Security»).
  The mirror drifting from the authority (answered by the corpus). Expressions that type but mean nothing
  (`log` of a quantity that can be negative): warnings from the supports, never a silent change.
- **Measure first.** Nothing; the corpus and the fuzz targets come first.

### `model-graph`

- **Choices.** The graph is a DAG with **plates** (rows, groups with their nesting, rows read as a series) as
  the visual form of indexing (answer 4: boxes, nested for nested levels; non-centred by default, a switch per
  level). Every node has a stable generated id; a **name** (identifier, `[a-z][a-z0-9_]*`) and a **label**
  (free text) are separate fields (answer 5). Edges are not stored: they are derived from the references of
  the expressions, so the drawing and the model cannot disagree. Priors and hyperpriors are roles of
  parameter nodes, not node kinds. Shapes come from plates. Data variables are typed (real, integer, group
  index, category with treatment contrasts, time; answer 6). The Go validator is the authority; the
  TypeScript mirror only gives instant feedback, and a test feeds both the same corpus of graphs and compares
  their findings. Spec: `bayes-model-graph.md` (draft).
- **Risks.** Expressiveness against safety, now carried by `expression-language` (answer 3). Time-series
  state-space models need a recursion (`mu[t]` from `mu[t-1]`), which a DAG by itself does not express: a
  latent-state node with its own recursion, and `lag` for observed series.
- **Measure first.** Nothing; the format is versioned from day one (`format_version`).

### `bayes-contract`

- **Choices.** As piece 0 of the models spec: the modes, the files, the rows. The draws output has a schema
  fixed per model version: summary by default, draws on request, wide when the columns are few and long above
  a limit (a setting, answer 14). Refusals are sentences with codes (`too_slow`, `too_much_disk`,
  `compile_failed`, `data_shape`), exit 3.
- **Risks.** A wide draws table (one column per scalar parameter) can have thousands of columns for a
  hierarchical model with many groups; parquet handles it badly beyond some thousands.
- **Measure first.** Write and read back a draws parquet of 4 chains × 1,000 draws for 10, 1,000 and 20,000
  scalar parameters, wide and long, with the memory peak; the measurement sets the default of the wide/long
  limit.

### `stan-codegen`

- **Choices.** Written in **Go in the gateway**, the only place the program is produced; the browser asks for
  the generated code to show it (a light request, no Job) and never sends code. Deterministic output (fixed
  order of blocks, of declarations; numbers printed by one formatter, the shortest text that reads back to the
  same double, always as a real literal: `expression-language`). The hierarchical default is **non-centred**,
  with a switch per level (answer 4). Identifiers: the person's names when they pass the pattern, are not
  reserved in Stan and do not end in `__`; generated identifiers otherwise, with the mapping table; labels
  never in the code (answer 5). Per-row quantities (a linear predictor of N rows) are **local variables**,
  never written to the output: what the program writes is bounded by the number of parameters, not by the
  number of rows. Never `#include`, never external C++ (`--allow-undefined`), never a comment holding user
  text, never `print` or `reject` with user text.
- **Risks.** Injection: none can come from the graph if no user string ever reaches the program — names are
  checked against a strict pattern and a list of reserved words, or replaced by generated identifiers;
  numbers are formatted from finite doubles; expressions are emitted from their typed AST; distributions and
  functions come only from the catalogue. A second safety net: stanc's `--info` JSON of the compiled program
  is compared with what the graph declares (the same variables, only catalogue distributions and functions).
  A bug in the codegen is a wrong model, not a breach: golden tests (graph → exact bytes), reference values
  per template, and simulation-based calibration ([Talts et al. 2018](https://arxiv.org/abs/1804.06788)) on
  two or three small models in the test suite.
- **Measure first.** Nothing; the golden corpus comes first.

### `prior-preview`

- **Choices.** A Web Worker, seeded draws, a fixed number of draws per redraw, density curves when the
  marginal is known in closed form and a histogram of ancestral draws when a hyperprior makes it a mixture
  (the owner's «true marginal»). Debounced on input. **Only the marginals of the parameters** (answer 13): no
  prior predictive in the browser, so no data is needed in the model's space (answer 6: data are bound only
  in the flow node). The stdlib packages are confirmed (answer 2) and loaded lazily with the worker.
- **Risks.** Heavy tails (Cauchy, Student-t with ν ≤ 2) make histograms unreadable: the axis is cut at
  catalogue quantiles and the cut is written on the chart. Bundle size (measured for this map: about 94 KB
  gzip for the 45 packages, «Research»; the entry is already heavy: lazy-load the worker with the page).
- **Golden test.** The marginals of the parameters drawn by this module's code, under Node in the test suite,
  against the `fixed_param` draws of the same graph, compared by a two-sample test per parameter; written by
  `predictive-checks`, which holds the Job half.
- **Measure first.** The time of 4,000 ancestral draws of a three-level hierarchy on a slow laptop, in Chrome
  and Firefox, by inversion (quantile of a uniform) and by stdlib's own generators.

### `templates`

- **Choices.** A template is a graph, opened as a copy; once opened it is a graph like any other (the
  owner's «graph only»). The templates are the empty canvas's proposal (answer 1). Default priors are weakly
  informative and written on the nodes, on the scale of **standardized** predictors (answer 6: standardization
  on by default, said in the report); they never change with the data.
- **Risks.** Defaults become the de facto model of most users; each default needs its source in the docs.
- **Measure first.** Reference values for each template (posterior means and sds from an independent run
  outside Tabularia, within Monte Carlo error) on the sample data.

### `model-store`

- **Choices.** A table `bayes_models` and `bayes_model_versions` (graph, program, program sha256, mapping
  table, note), one Alembic migration (the Python gateway frozen except migrations), served by the Go
  gateway through the `versioned` pattern. Visibility as saved scripts: VIEW on the folder and the use of the
  group; EDIT to change; RUN to run. A flow runs a **pinned** version, with the notice «a newer version exists»
  (answer 7). Saving a version starts its compile in the background (answer 8).
- **Risks.** A graph saved by an older format version must still open.
- **Measure first.** Nothing.

### `graph-editor`

- **Choices.** A second Vue Flow canvas with **plates as parent nodes**; the inspector shows the node's
  numbers with `prior-preview` beside them; expression fields checked by the TypeScript mirror as the person
  types, the gateway's answer authoritative on save; validation errors on the node; the Stan code read-only
  in a panel (Monaco is already a dependency) with the mapping of identifiers to labels; templates as the
  empty canvas's proposal, the free model one click away, every choice explained for a reader who does not
  know Bayes (answer 1). Every shortcut also reachable on a Mac and on laptops without F/Home/End keys. Tried
  on Chrome and Firefox.
- **Risks.** Plates with nesting and drag-and-drop between plates are the hardest interaction of the map;
  accessibility of a canvas (keyboard-only editing of edges).
- **Measure first.** A throw-away prototype of nested plates in Vue Flow (parent nodes, extent, resizing).

### `bayes-image`

- **Choices.** A Python runner (answer 15) with `pyarrow` and `numpy` pinned by hash; **CmdStan from its
  source tarball pinned by URL and sha256** (the `wheels`/`weights` precedent); `g++` and `make` from the
  distribution's signed archive; `make build` at image build so that the precompiled header, TBB and the
  CmdStan main object are in the image and a compile writes only to `/tmp`. No `cmdstanpy` at run time
  (its readers load whole chains); the runner calls the executable itself. The compile limits are settings
  with visible defaults (answer 10). Spec: written by the lane that measures the compile cost.
- **Risks.** The image carries a **C++ compiler** that runs in the Job: acceptable only because the only
  code it compiles is ours (`stan-codegen`) and the Job has no network. Building CmdStan takes «10+ minutes
  and 2+ GB» (CmdStan guide): heavy on the development machine, one at a time. Two architectures (amd64,
  arm64).
- **Measure first.** Image size; build time; the compile of each template: seconds and memory peak, with
  and without `STAN_CPP_OPTIMS`, with a read-only root; whether anything writes outside `/tmp`.

### `predictive-checks`

- **Choices.**
  - **Prior predictive, only as a Job** (answer 13): Stan's `fixed_param` sampler with ancestral `_rng` draws
    in `generated quantities` (exact, no MCMC, no divergences), from the **same program** as the posterior,
    selected by a data flag, so one compiled binary serves both (one compile, one cache entry). Run from the
    flow node's panel on the development sample's bound rows (the prior predictive of a regression depends on
    the predictors). Shown as **charts** against the real data: the replicated data's binned densities beside
    the observed one, the replicated T(y_rep) as histograms with T(y) marked; no share, no verdict.
  - **Posterior predictive, only as charts** (answer 12): y_rep is drawn **inside** `generated quantities` as a
    local vector and only its summaries are written per draw — the test statistics T(y_rep) (mean, sd, min,
    max, chosen quantiles, share of zeros for counts; per group for hierarchical models) and the counts of y_rep
    in bins fixed from the observed data (for the overlaid densities). The output grows with the number of
    statistics and bins, never with the rows. The report draws the overlaid densities and the histograms of
    T(y_rep) with T(y) marked; **no posterior predictive p-value or probability is computed or shown**. Full
    y_rep rows only on request, for a thinned set of draws, written in blocks (answer 14).
  - The **golden test** of the priors' marginals (see «Why the two predictive checks are where they are»).
- **Risks.** A program with `generated quantities` that draws N values per draw costs time proportional to
  N × draws; bounded by thinning the draws used for the checks. A chart read as a verdict: the report's words
  say what the chart shows and never whether the model «passes».
- **Measure first.** The added time of the generated quantities at 1,000, 10,000 and 100,000 rows.

### `stan-compile`

- **Choices.** A **compile Job** of its own (mode `compile`), separate from sampling: its limits are the
  compiler's (memory, time), it runs once per program, and it starts **in the background when a version is
  saved**, the first run that needs it being the fallback (answer 8). stanc with fixed flags only (never
  `--include-paths`, `--allow-undefined`, user headers); then `make` of the executable in `/tmp`. The **cache
  key** is the sha256 of (program, CmdStan version, stanc flags, C++ flags, architecture, image digest); no
  `-march=native` (a binary must run on every node of its architecture). The cache is evicted least recently
  used under a size limit the administrator sees, with a visible default (answer 8).
- **Integrity.** The compile Job writes the binary to the scripts bucket and states its sha256 in its
  termination message (the Kubernetes API, which another pod cannot write); the gateway copies the binary into
  Tabularia's own storage (not the scripts bucket), checks that hash and records it in Postgres; a sampling Job
  receives the binary in its step prefix and the expected sha256 in `task.json` (its ConfigMap, written by the
  gateway), and refuses to execute a file whose hash differs. A poisoned binary in the scripts bucket is
  therefore never executed.
- **Risks.** Compile time and memory (assumed: tens of seconds to a minute, one to a few GB; to measure).
- **Measure first.** As `bayes-image`; and the binary's size (tens of MB, assumed).

### `bayes-diagnostics`

- **Choices.** The draws of **one parameter** across chains are small (4 chains × 1,000 draws = 32 KB): the
  rank-normalized split R-hat and the bulk and tail ESS of [Vehtari et al. 2021](https://doi.org/10.1214/20-BA1221)
  are computed **one block of columns at a time**, read from the parquet; the memory is bounded by the block,
  not by the model. The sampler's facts (divergences, tree depth reached, E-BFMI per chain) by accumulators
  while the CSV is streamed. A pure-Python implementation checked against the `posterior` R package and
  ArviZ as **witnesses outside the recipe** (as `witness.json`). Thinned draws (a fixed number per chain)
  for the charts. Everything is a **measure or a fact** (answer 11): no reference option, no mark, no stop.
- **Risks.** A posterior correlation matrix for pair charts is quadratic in the parameters: limited to the
  parameters the report shows.
- **Measure first.** Time and memory of the diagnostics for 20,000 parameters × 4,000 draws.

### `bayes-runner`

- **Choices.** Python, one process: the input parquet → CmdStan's JSON data file **in a stream** (the data
  themselves must be in the sampler's memory: Stan holds them, and `max_rows` bounds them); the predictors'
  means and standard deviations computed in a first pass for the standardization (answer 6); the executable
  checked and run with `num_chains`, seeds and threads equal to the CPU limit; stdout's progress lines read to
  project the time after the first iterations, and the job refused (`too_slow`) before it passes the node's
  timeout, as the risk node does; the CSV files **followed while the chains run** (owner, 2026-11-23: the parquet is appended as the sampling
  goes), read line by line in batches of draws (comment lines skipped, the adaptation and timing comments
  parsed, a partial last line kept until it is complete), each batch appended to the draws parquet as one
  row group of one chain and freed, the accumulators of `bayes-diagnostics` and `predictive-checks` fed from
  the same batch; when sampling ends only the tail not yet read is converted; the disk projected after the first batch. Every limit is a setting with a visible default
  and is checked before any partial result (answer 10). A run with divergences goes on to the end; their
  count is a fact on the report's first page (answer 11).
- **Risks.** CSV is text: parsing speed at 20,000 columns; the disk at `num_chains` × draws × columns × ~20
  bytes of text.
- **Measure first.** Memory peak and time of the conversion for the sizes of `bayes-contract`; a test that
  measures the memory peak, as the rule asks, and shows it flat when the draws are multiplied by ten.

### `bayes-report`

- **Choices.** The ten-column rows only: the report is a drawing of results, no Job. Pages: the summary
  table with intervals, the predictors' standardization said and the coefficients on both scales (answer 6;
  only for the terms of a linear-predictor node, owner 2026-11-23: in free expressions the standardized scale
  with the predictor's mean and standard deviation);
  **Diagnostics** (R-hat, ESS, MCSE per parameter, divergences, tree depth, E-BFMI), each with a sentence on
  what it measures and the reference values of the literature **cited as such** («Vehtari et al. 2021 propose
  using the draws when R-hat is below 1.01»), no mark, no «above / below the reference» of a node option
  (answer 11); the divergences' count on the first page as a fact; trace and rank charts on thinned draws;
  prior against posterior densities; the predictive checks **as charts only** (overlaid binned densities,
  T(y_rep) histograms with T(y) marked; answer 12); «How this report was computed» (NUTS, the warmup, why R-hat
  and ESS, what a divergence is, what the predictive charts show and do not show). Five languages.
- **Risks.** Hundreds of parameters: the report shows the hyperparameters and the population-level
  parameters in full and the group-level ones in a compact table or chart (answer 14: the parquet holds all
  parameters; the report holds less).
- **Measure first.** Nothing beyond the layouts' render time with the largest table the contract allows.

### `flow-node`

- **Choices.** A step node `bayes_model` (resolver boundary, priority as `model`), the saved model chosen by
  folder path and **pinned to a version** with the notice «a newer version exists» (answer 7), its data
  variables listed with a column picker each (answer 6: the binding happens only here); a text column bound to
  a group becomes indices 1…J in the runner, a text column bound to a category becomes treatment contrasts
  with the first level as reference (labels travel as data, mapped back in the results); NULLs left out with
  their count (no imputation), as every model node. Sampler options (answer 9): chains, warmup, draws and seed
  in view; `adapt_delta`, `max_treedepth` and initial values under «Advanced»; NUTS only. Limits (answer 10).
  Output (answer 14): the summary by default; the draws on request (wide when the columns are few, long above
  a limit), all parameters; y_rep rows on request and thinned. Run is a trial on the development sample; the
  compile, when the background one has not finished, happens first and says so.
- **Risks.** The binding broken when a newer version adds a data variable (the node keeps its pinned version
  and names the unbound variable when the person moves to the newer one).
- **Measure first.** Nothing beyond the runner's numbers.

### `bayes-mlflow`

- **Choices.** As the other groups: the gateway logs, never the Job. Parameters, the summary as metrics,
  the program and the report as artifacts; **never the draws** (answer 14).
- **Risks.** None beyond the other groups'.
- **Measure first.** Nothing.

### `assistants`

- **Choices.** The editor's assistant reads the graph (names, labels and expressions as data, never
  instructions): explains a node, proposes priors on the scale of standardized predictors, and after a run
  reads the diagnostics and proposes known remedies (the non-centred form, a tighter prior, more warmup),
  always as a proposal the person applies. An expression it proposes goes through the same parser as one typed.
- **Risks.** An assistant that sounds like a verdict: its prompt follows the measures-without-verdict rule.
- **Measure first.** Nothing.

### `docs`

- **Choices.** One capability page («Bayesian models»), the administrator's guide (the group, resources,
  the compile cache), the third-party licence notices (CmdStan, Stan Math and what it bundles, the stdlib
  packages), no competitor named, no local path.

## Security: the threats and where each is answered

| Threat | Module | Answer |
|---|---|---|
| Stan code injection through a name, a label or a number | `model-graph`, `stan-codegen` | No user string reaches the program: names match a strict pattern and are not reserved words, or are replaced by generated identifiers; labels never enter the program, not even in comments; numbers formatted from finite doubles; functions and distributions only from the catalogue; the program is produced by the gateway, never received from the browser; the program's `--info` compared with the graph |
| Injection through the free model's typed expressions (answer 3) | `expression-language` | A closed grammar of our own, ASCII only; no strings, comments, statements, `#`, `;`, braces or user-defined functions in the language; every identifier must be a node of the graph and every function a catalogue one; the Stan text emitted **from the typed AST only**, fully parenthesized, never from the person's text; limits of bytes, depth and nodes checked before the work; fuzzing of the Go parser and emitter, a corpus shared with the TypeScript mirror; **a security review of its own** when built, before `stan-codegen` uses it, and again in step 8 |
| Confusable characters (a Cyrillic letter for a Latin one, `−` for `-`, a non-breaking space) | `expression-language`, `model-graph` | Expressions and names are ASCII only, checked byte by byte, never with Unicode-aware classes; a non-ASCII character is refused with its code point and, for the known look-alikes, the ASCII character meant; labels refuse control and bidirectional-override characters |
| `#include`, external C++, `print`/`reject` with data | `stan-codegen`, `stan-compile` | Never emitted; stanc called with a fixed argument list, without include paths or `--allow-undefined` |
| A compile that eats the node | `stan-compile` | A Job of its own with memory and time limits; the program's size bounded by the graph's limits |
| A poisoned binary in the shared scripts bucket | `stan-compile`, `bayes-runner` | sha256 from the compile Job's termination message, recorded in Postgres; the binary kept in Tabularia's storage; checked before exec |
| A compiler inside a pod that runs user data | `bayes-image` | It compiles only generated code; no network; uid 10001, read-only root, no capability, no ServiceAccount token, `/tmp` only |
| Hostile data (huge arrays, NaN, extreme values) | `flow-node`, `bayes-runner` | Row limits before the Job; NaN and infinities refused or left out with a sentence; shapes checked against the program's data block |
| Disk and time exhaustion | `bayes-runner` | Projections after the first iterations and the first batch; refusal before the budget, with a sentence (answer 10) |
| Names and labels in the report and the parquet | `bayes-report` | Static text and encoded values, as the D9 test of the decomposition (`=HYPERLINK(…)`, markup) |
| Assistants | `assistants` | Names, labels, expressions and rows are data in the prompt; a proposed expression is parsed like a typed one |
| Supply chain | `bayes-image`, `distribution-catalog` | CmdStan by sha256, Python packages by hash, toolchain from the signed archive; the stdlib packages at exact versions in the lockfile |

## Out of scope for the first release (with why)

- **Predictions on new rows of the flow** (the posterior predictive on rows not used in the fit): the owner
  left it out; it is not needed for the checks, which use the observed rows. The machinery of
  `predictive-checks` (y_rep in `generated quantities`) makes it a small later step.
- **LOO, WAIC, model comparison** (owner).
- **Hand-edited Stan code** (owner).
- **User-defined functions** in the expression language (answer 3).
- **A prior predictive check in the browser** (answer 13); **posterior predictive p-values or probabilities**
  (answer 12); **reference options and a stop on divergences** in the node (answer 11).
- **Variational inference, Pathfinder, Laplace**: CmdStan offers them (assumed); NUTS only (answer 9).
- **ODEs, Gaussian processes, mixture models**: not in the four families.

## Research

### Stan and its ecosystem

| Component | What it is | Licence | Version, weight | Source |
|---|---|---|---|---|
| CmdStan | The command-line interface: builds a model executable, runs NUTS, writes CSV | BSD-3-Clause | 2.40.0, 2026-09-16; source tarball 46,989,297 bytes (checked online) | [releases](https://github.com/stan-dev/cmdstan/releases), [LICENSE](https://github.com/stan-dev/cmdstan/blob/develop/LICENSE) |
| stanc3 | The Stan compiler (OCaml): Stan → C++; also `--info`, `--auto-format`, `--warn-pedantic` | BSD-3-Clause (repository licence `LICENSE.txt`) | 2.40.0; native `linux-stanc` 5.7 MB; **`stanc.js` 3.0 MB** (`stanc-pretty.js` 16.6 MB) | [releases](https://github.com/stan-dev/stanc3/releases), [repository](https://github.com/stan-dev/stanc3) |
| Stan Math | The C++ library the executables link | «new BSD»; it bundles Intel TBB (Apache-2.0, checked online); Boost, Eigen and SUNDIALS also ship with it (assumed; their licences to be read from the tarball by `bayes-image`) | in the CmdStan tarball | [stan-dev/math](https://github.com/stan-dev/math) |
| CmdStanPy | Python interface to CmdStan | BSD-3-Clause | 1.3.2 (2026-11-03); 2.0.0rc1 pre-release 2026-11-17; needs pandas, numpy, tqdm, stanio | [PyPI](https://pypi.org/project/cmdstanpy/) |
| CmdStanR | R interface to CmdStan | BSD-3-Clause (assumed) | **not on CRAN** (its CRAN page answers 404, checked online): it would break the group's «CRAN or PyPI» supply chain | [site](https://mc-stan.org/cmdstanr/) |
| BridgeStan | Log density and gradients of a Stan model from Python, R, Julia | BSD-3-Clause | not needed for NUTS through CmdStan | [repository](https://github.com/roualdes/bridgestan) |
| posterior (R) | Draws formats, R-hat, ESS, summaries | BSD-3-Clause | 1.8.0 (2026-11-18) | [CRAN](https://cran.r-project.org/package=posterior) |
| ArviZ (Python) | Diagnostics and plots of Bayesian inference | Apache-2.0 | witness only | [repository](https://github.com/arviz-devs/arviz) |
| loo (R) | PSIS-LOO, WAIC | GPL (≥ 3) | 2.10.1; out of scope (no LOO/WAIC) | [CRAN](https://cran.r-project.org/package=loo) |
| bayesplot (R) | Plots of draws and predictive checks | GPL (≥ 3) | 1.17.0; not needed: charts are drawn by Tabularia's report engine | [CRAN](https://cran.r-project.org/package=bayesplot) |

**The dialect.** Stan 2.33 removed the long-deprecated syntax (the `<-` assignment, `increment_log_prob`, the
`_log` suffix in `~` statements, `#` comments, postfix array declarations); 2.34 removed real values in
conditionals ([removed features](https://mc-stan.org/docs/reference-manual/removals.html)). The page states no
formal deprecation policy or stability promise. Since the codegen emits only the current syntax and its
golden tests compile every template with the pinned stanc, a change of dialect shows as a failed test at the
next CmdStan upgrade, never in production.

**Names and literals in Stan 2.40** (checked online, [expressions](https://mc-stan.org/docs/reference-manual/expressions.html)):
identifiers are ASCII letters, digits and `_`, start with a letter and may not end in `__`; the reference manual
lists reserved words, type names, block names, `print`, `reject`, `profile`, `fatal_error`, `target`,
`jacobian`, and the suffixes `_lpdf`, `_lpmf`, `_lcdf`, `_lccdf`, `_cdf`, `_ccdf`, `_lupdf`, `_lupmf`; C++
keywords are legal but renamed internally. A number followed by `i` is an imaginary literal. In the operator
table, `^` binds tighter than unary minus and is right-associative; `.*`, `./` and `.^` are the elementwise
forms. These facts are the inputs of `expression-language` and are copied into the catalogue.

**Functions that do not exist** (checked online in the functions index of 2.40): there is no
`binomial_logit_rng` (the catalogue draws `binomial_rng(N, inv_logit(eta))`) and no `softplus` (Stan's name is
`log1p_exp`).

**CSV output.** Draws are written only as CSV, one file per chain, with `#` comment lines (configuration,
the adaptation after warmup, the timing at the end) and the columns `lp__`, `accept_stat__`, `stepsize__`,
`treedepth__`, `n_leapfrog__`, `divergent__`, `energy__`, then the model's quantities; several chains run in
one process with `num_chains` (in parallel with `STAN_THREADS`) ([CmdStan guide, MCMC](https://mc-stan.org/docs/cmdstan-guide/mcmc_config.html)).
No parquet or binary format for draws: the streaming conversion is ours.

**stanc options that matter here** ([using stanc](https://mc-stan.org/docs/stan-users-guide/using-stanc.html)):
`--include-paths` (the directories `#include` reads: never passed), `--allow-undefined` (external C++
functions: never passed), `--info` (a JSON of the program's variables and distributions: the codegen's
cross-check), `--warn-pedantic` (warnings on the program's meaning, with possible false positives: the
assistant's input, never a verdict).

### stanc3 in JavaScript: yes

stanc3's releases ship **`stanc.js`** (3.0 MB for 2.40.0, checked online), built with js_of_ocaml; its
`stanc` function returns an object with `result` (C++, formatted Stan or the `--info` JSON), `warnings` and
`errors` (assumed from secondary documentation and from the R packages that call it, e.g.
[rstan's `stanc.R`](https://rdrr.io/cran/rstan/src/R/stanc.R); the module checks it on the pinned file).
So the generated program **can** be validated in the browser, in a Web Worker. It is **not needed for
safety** (the program is produced and compiled on the server, and stanc runs in the compile Job anyway): its
value would be instant pedantic warnings and the `--info` cross-check in the editor, for 3 MB loaded lazily.
Decision of this map: not in the first release (see «Provisional decisions»).

### Prior art: a visual editor for Stan

No open-source visual (drag-and-drop) editor that generates Stan was found (one search, checked online).
Closest: **bayesvl** (R, GPL-3.0) builds a network with R functions and generates Stan code, without a
canvas ([repository](https://github.com/sshpa/bayesvl)); **Stan Playground** (Apache-2.0) is a text editor
in the browser that compiles on a server and samples in the browser
([features](https://flatironinstitute.github.io/stan-playground/feature_overview.html),
[repository](https://github.com/flatironinstitute/stan-playground)). Neither is reused: the editor is ours,
the compiler and sampler are Stan's.

### JavaScript libraries for the distributions

| Library | Licence | Seeded draws | Notes | Source |
|---|---|---|---|---|
| stdlib (`@stdlib/stats-base-dists-*`, `@stdlib/random-base-*`) — **confirmed by the owner (answer 2)** | the packages' `license` field: `Apache-2.0`; 12 special-function packages they pull: `Apache-2.0 AND BSL-1.0` (checked online and in a scratch install, 2026-11-23) | **yes**: `factory(…, {seed})`, a writable `state`, a choice of uniform generator (`prng`) | modular packages, one per distribution and function; the 45 packages of the catalogue's nine prior distributions (pdf, logpdf, cdf, quantile, random) are versions 0.2.2, 0.2.3 and 0.3.1 published on 2026-02-05 and 2026-02-08, and install 369 packages; bundled with esbuild 0.28.2, minified: 384 KB, 94 KB gzip (measured 2026-11-23); details in `bayes-distribution-catalog.md` | [random-base-normal](https://github.com/stdlib-js/random-base-normal) |
| jStat | MIT | not documented in its README | pdf, cdf, inverse, sample for most distributions; its README says the old module «will no longer be updated» | [repository](https://github.com/jstat/jstat) |
| Our own code | — | yes (a small seeded generator) | densities are easy; quantiles and draws of gamma, beta, Student-t need special functions and careful algorithms: a correctness risk the libraries have already paid | — |

### The cost of compiling a Stan model, and how to avoid it at each run

- **Building CmdStan** (once, at image build): «10+ minutes and 2+ GB of memory»; needs g++ ≥ 4.9.3 and make
  ≥ 3.81 ([CmdStan installation](https://mc-stan.org/docs/cmdstan-guide/installation.html)). The makefile
  precompiles part of the Stan library (a precompiled header), which must match the compiler: built in the
  image with the same compiler that compiles the models.
- **Compiling one model**: no official benchmark found. One measurement on a laptop: the eight-schools model
  in about 9 s, 19 s with `STAN_CPP_OPTIMS`, which gave «a few percent» faster sampling
  ([Stan blog, 2022](https://blog.mc-stan.org/2022/08/03/options-for-improving-stan-sampling-speed/)); users
  report about a minute in other environments. Memory is not documented (assumed one to a few GB: to measure
  in step 0).
- **Avoiding it**: the program is deterministic per model version, so the binary is **cached by the sha256 of
  the program, the CmdStan version, the flags, the architecture and the image digest**; a run of an unchanged
  model never compiles. The prior predictive, the posterior and the checks share one program and one binary
  (a data flag selects the mode). The compile starts in the background when a version is saved (answer 8).
- **Alternative, not recommended**: precompiled generic programs driven by data (one executable per family,
  the structure passed as data, as some R packages do): no compile for templates, but the shown code would not
  be the model's own, and the free model would still compile. Kept as a fallback if step 0 measures a compile
  too heavy for the clusters.

### The literature the report and the checks rest on

- R-hat and ESS: Vehtari, Gelman, Simpson, Carpenter, Bürkner (2021), «Rank-normalization, folding, and
  localization: an improved R-hat for assessing convergence of MCMC», *Bayesian Analysis* 16(2)
  ([doi](https://doi.org/10.1214/20-BA1221)).
- Divergences and E-BFMI: Betancourt (2016), «Diagnosing suboptimal cotangent disintegrations in Hamiltonian
  Monte Carlo» ([arXiv:1604.00695](https://arxiv.org/abs/1604.00695)).
- The workflow and prior predictive checks: Gabry, Simpson, Vehtari, Betancourt, Gelman (2019),
  «Visualization in Bayesian workflow», *JRSS A* 182(2) ([doi](https://doi.org/10.1111/rssa.12378)).
- Posterior predictive checks in Stan: [Stan User's Guide](https://mc-stan.org/docs/stan-users-guide/posterior-predictive-checks.html).
- Why no posterior predictive p-value is shown (answer 12): Meng (1994), *Annals of Statistics* 22(3); Gelman,
  Meng, Stern (1996), *Statistica Sinica* 6; Bayarri, Berger (2000), *JASA* 95 (they are not uniform under the
  model, and conservative).
- The LKJ distribution and the marginal of one correlation: Lewandowski, Kurowicka, Joe (2009), «Generating
  random correlation matrices based on vines and extended onion method», *Journal of Multivariate Analysis*
  100(9) (assumed; `distribution-catalog` checks the marginal against `lkj_corr_cholesky_rng` draws).
- Testing the codegen: Talts, Betancourt, Simpson, Vehtari, Gelman (2018), «Validating Bayesian inference
  algorithms with simulation-based calibration» ([arXiv:1804.06788](https://arxiv.org/abs/1804.06788)).

## Provisional decisions (this map's), and what the interview confirmed

| Decision | Why | Status |
|---|---|---|
| The codegen lives in the Go gateway; the browser never sends a program | One authoritative producer; nothing the browser sends can become code | The map's, to confirm |
| The expression parser's authority in Go, a TypeScript mirror for feedback only | The same producer for every byte of the program; the mirror is never trusted | The map's, to confirm (follows from answer 3) |
| A compile Job separate from the sampling Job | Its limits differ; a compile runs once per program; a cached binary needs no compile pod | The map's, to confirm; the background compile at save is the owner's (answer 8) |
| The compiled binary kept in Tabularia's storage, its sha256 in Postgres, checked before exec | The scripts bucket's key is shared by every user script | The map's, to confirm |
| stdlib packages for the browser's distributions (a new frontend dependency) | Apache-2.0, seeded and stateful draws, modular | **Confirmed by the owner (answer 2)** |
| `stanc.js` not in the first release | Not needed for safety; 3 MB for pedantic warnings the compile Job already returns | The map's, to confirm |
| Per-row quantities never written by the program | Output bounded by parameters, not rows | The map's, to confirm |
| y_rep summarized inside `generated quantities` | The checks cost memory proportional to statistics and bins, not rows | The map's, to confirm; the summaries feed charts only (answer 12) |
| The runner in Python | `pyarrow` streams CSV into parquet; PyPI pins by hash; `cmdstanr` is not on CRAN | **Confirmed by the owner (answer 15)** |

## Interview answers (2026-11-23)

The fifteen questions of the interview, answered by the owner on 2026-11-23. The options are kept in short
form; «Why» is one line on the reason of the choice.

| # | Question | Answer (owner) | Why |
|---|---|---|---|
| 1 | Who is the typical user? | **(c) both**: the templates as the empty canvas's proposal, the free model one click away, the explanations written for whoever does not know Bayes | An analyst and a statistician share the space; explanations cost the statistician nothing |
| 2 | Which distributions in the first release? | **(b)** the minimum of the four families (priors: normal, Student-t, Cauchy, exponential, gamma, inverse-gamma, lognormal, beta, uniform; likelihoods: normal, Bernoulli-logit, binomial-logit, Poisson-log, negative binomial 2-log; half forms by bounds) **plus LKJ and the multivariate normal**; **stdlib (Apache-2.0) accepted** as a new frontend dependency | «Intercepts and slopes per group» usually means correlated effects, which need LKJ |
| 3 | What can the free model do? | **(b), against the recommendation**: typed expressions (`a + b * x`) read by a grammar of ours into an AST; the Stan code generated only from the AST, never from the text; **no user-defined functions**; a dedicated security review | The graph alone is too verbose for the free model; the closed grammar and the AST keep the surface bounded, at the price of a module and a review of its own (`expression-language`) |
| 4 | How does a hierarchical model look and behave? | **Plates** (boxes, nested for nested levels); the **non-centred** form by default, a switch per level | Plates are the textbook drawing; the non-centred form is the usual remedy for the divergences of hierarchical models with few data per group (Stan User's Guide, assumed), and the switch keeps the centred one for groups with many data |
| 5 | The names in the generated code | **(a)** the person's names when they match `[a-z][a-z0-9_]*`, are not reserved in Stan and do not end in `__`; otherwise generated identifiers with a mapping table; labels never in the code | The pattern makes injection impossible and keeps the code readable |
| 6 | How are data bound to variables? | **(a)** only in the flow node; text columns as treatment contrasts with the first level as reference; **predictors standardized by default**, said in the report, coefficients on both scales | One place for data; priors on a known scale |
| 7 | Which version of a saved model does a flow run? | **(c)** a pinned version, with the notice «a newer version exists» | A model's numbers should not change under a scheduled flow without someone choosing it |
| 8 | When does the compile happen, and how long is a binary kept? | **(b)** in the background when a version is saved, the run as the fallback; an LRU cache with a limit the administrator sees | Runs find the binary ready; the cache cannot grow without bound |
| 9 | Which sampler settings and methods? | **(b)** chains, warmup, draws and seed in view; `adapt_delta`, `max_treedepth` and initial values under «Advanced»; **NUTS only** | The usual remedies within reach, without CmdStan's every option |
| 10 | Which limits, and their defaults? | **All limits as settings with visible defaults**; the runner refuses with a sentence when its projection passes a limit, before any partial result | As every model node; the same seed must give the same numbers |
| 11 | How are R-hat, ESS and divergences presented? | **(a) only measures and facts**, each with the sentence on what it measures and the literature's reference values cited as such; **no reference option in the node**; **no option «stop the step on divergences»**: the run goes on, the count is a fact on the first page | The rule of 2026-11-20 taken strictly: nothing in the node that works as a verdict |
| 12 | How is the posterior predictive p-value presented? | **(c) only the charts**: no p-value, no «posterior predictive probability» | It is not uniform under the model; a number beside a p-value would read as a test |
| 13 | Where does the prior predictive check get its data? | **(c) only as a Job** (`fixed_param`); the browser keeps only the priors' marginals (with hyperpriors by ancestral sampling, the true marginal); no prior predictive in the browser | One implementation of the prior predictive; the model's space needs no data |
| 14 | What do the parquet output and MLflow hold? | The **summary by default**; the draws on request (wide when the columns are few, long beyond a limit); **all parameters**; y_rep rows on request and thinned; **MLflow without draws** | The flow's storage and MLflow do not fill with draws nobody asked for |
| 15 | In which language is the runner written? | **(a) Python** | `pyarrow` streams CSV to parquet; PyPI pins by hash; `cmdstanr` is not on CRAN |

### Answers to the module specs' questions (2026-11-23)

The drafts of `bayes-distribution-catalog.md`, `bayes-expression-language.md` and `bayes-model-graph.md` asked
five questions; the owner answered them on 2026-11-23. Each spec records them in its «Decisions of 2026-11-23»
and has no open question left; its smaller choices are marked there «spec decision, 2026-11-23, owner may
revisit».

| Question | Answer (owner) | Module |
|---|---|---|
| The licence notices of the stdlib packages (about 3.7 MB more) | **Left as they are**: one text per package (against the recommendation to print each distinct text once) | `distribution-catalog` |
| The limits of the expression language and of the graph | **Documented constants of the catalogue**; the run's limits stay administrator settings (answer 10) | `distribution-catalog`, `expression-language`, `model-graph` |
| Series in the free model | **`lag(x, k)` in the expression language** | `expression-language` |
| Coefficients on both scales (answer 6) | **Only for the terms of a `linear_predictor` node**; in free expressions the standardized scale only, with the predictor's mean and standard deviation | `model-graph`, `bayes-report` |
| A data variable the model does not use | **An error** | `model-graph` |
