# Spec: the Bayesian model node in MLflow (`bayes-mlflow`)

Status: **Approved by the owner on 2026-11-24** (drafted the same day). It specifies the module `bayes-mlflow` of
`docs/specs/bayesian-editor-map.md` (**the map**), step 7 of its order of construction, beside `assistants` and `docs`,
after the approved `bayes-contract.md` (**the contract**: the card, the ten-column rows), `bayes-runner.md` (**the runner
spec**), `bayes-flow-node.md` (**the node spec**: the step, `planBayes`, `runBayes`) and `bayes-report.md` (**the report
spec**: the report the gateway renders for MLflow). Nothing of it is built. Decisions marked **(owner)** are the owner's
(the map's «Interview answers», **the answers**, and the owner's rules); the rest are this spec's, to confirm; those marked
«spec decision, 2026-11-24, owner may revisit» are small choices the owner may change without reopening the module. The four
questions the draft left open were answered by the owner on 2026-11-24 and are in «Decisions».

Words: **verified** = read in this repository's code on 2026-11-24; **assumed** = known from MLflow's public documentation
or an earlier spec, not run here, closed by the lot named beside it.

## Objective

When a Bayesian model node names an MLflow connection and an experiment, **every flow run of the node is recorded in MLflow
by the gateway**, as a model step of the other groups is, with what a Bayesian run is made of and **never the draws**
(answer 14):

- the **parameters**: the saved model and its pinned version, the sampler's settings, the bindings of the data variables to
  columns, the output;
- the **metrics**: the sampler's facts and the posterior summary of the scalar quantities (mean, sd, three quantiles, R-hat,
  the two effective sample sizes), each a number with no verdict;
- the **artifacts**: the report as PDF, the summary as a table (`summary.csv`), the model's card, its `MLmodel`, **the Stan
  program** the version stores; **no `results.parquet`**;
- with `registerAs`, **a version of a registered model** made from the run's `model` folder, with the tags the models
  spec's other versions carry.

It adds the node's **MLflow block** (the connection, the experiment, the model's name, «Continue without MLflow»), the same
as a model node's, and the sentence on the run page. The prior predictive check is a trial and **logs nothing**, as every
trial.

## Decisions

| Question | Decision | By |
|---|---|---|
| Who talks to MLflow | **The gateway, never the Job**: the Job writes its files to the scripts bucket and `task.mlflow` only makes the runner write `MLmodel` (the card is always written); the connection's secret never reaches it | The map, `bayes-mlflow` «Choices»; the models spec, «MLflow» |
| What never goes | **The draws**: no `draws` output, no `replicated` output, no `curve`, `hist`, `point`, `ppc` or `prior_check` row of the results (they hold thinned draws and the charts' bins), no sample of any chain | Owner (answer 14) |
| Which runs log | **Flow runs of a node with an `mlflow` block**, after the results are imported; **never a trial** (the prior predictive check is one) | The models spec («Only flow runs, not trials»); `importModel` already guards it with `r.trial == 0` (verified) |
| The machinery | **The model nodes' own** (`planMLflow`, `logModelMLflow`, `mlflowLog`, `mlflowconn.Tracker`, the batches, the key cleaning and `newKeyNames`, the run page's record); the Bayesian step only supplies what is its own (its parameters, metrics, tags, the summary table, the program) through branches on `modelStep.bayes` | Spec; the other groups' behaviour and tests stay byte for byte |
| When MLflow fails | **The step fails** after the model is fitted, «the model was fitted but MLflow could not record it: …», unless the node's «Continue without MLflow» is on (then a warning) | The models spec |
| A step that fails after its Job ran | **A FAILED run** in MLflow, with the error as a tag and the node's parameters | The models spec (`logFailedModel`) |
| The registered model | Only with `registerAs`; the version comes from the run's `model` folder (`MLmodel`, `model.json`, `program.stan`); tags as the other groups' plus the Bayesian ones («The tags») | The models spec, extended |
| Predict | **None for a Bayesian model** (the contract: predictions on new rows are out of scope): a registered version of `bayes_model` is refused by Predict **at planning (`planPredict`)** with a clear sentence, never in the Job | The contract, «The card»; the refusal: owner, 2026-11-24 («Predict») |
| `registerAs` | **Offered** for the Bayesian node: the version holds the card, the program and the summary, which is what a registry is for; Predict refuses it (above) | Owner, 2026-11-24 |
| `results.parquet` | **Never uploaded to MLflow**: the files are `summary.csv`, the program (from the gateway, sha256 checked) and the report PDF; no draw and no trace, not even thinned | Owner, 2026-11-24 |
| The panel's MLflow block | **A new component** (`BayesMlflow.vue`) that reuses the helpers and keys of `ModelForm` without touching it | Owner, 2026-11-24 |
| The program in MLflow | **`model/program.stan`**, the stored program of the pinned version, written by the gateway from the version it already holds (never read back from the Job), checked against the card's `program.sha256` | The map, `bayes-mlflow` «Choices» («the program and the report as artifacts»); the check: spec |
| The metrics' scope | **The scalar quantities of the report's scope, in its order, at most 100** (`bayesMLflowQuantities`, a constant of the gateway), eight statistics each, plus the sampler's facts and the run's counts; the quantities of a plate are in the summary table, not in metrics | Owner, 2026-11-24 (the draft's choice confirmed) |
| No verdict in MLflow | A metric is a measure or a fact (`summary.mu.rhat`, `sampler.divergences`); no tag, parameter or metric holds a threshold, a reference value, a «converged», a «passed» | Owner (answers 11, 12; the rule of 2026-11-20) |
| The node's fingerprint | **Unchanged**: the MLflow block never makes a node «not up to date» (it does not change the numbers); `bayesFingerprint` already leaves it out (verified) | The node spec, «Not up to date» |
| Names as data | A quantity's name, a label, a unit, a column, the saved model's name are values: in a metric's key only after the existing cleaning (`metricKey`, `shortKey`, collisions kept apart and warned), in a tag or a parameter as text, in the CSV as text with no formula (a leading `=`, `+`, `-`, `@` is neutralised) | The models spec; the report spec's D9 test |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| The node's MLflow plan | `internal/api/jobs_models_mlflow.go`: `planMLflow(ctx, user, node, label)` reads `data.mlflow` `{connectionId, experiment, registerAs, continueWithout}`, refuses an empty experiment, a node with `card: false` and `registerAs`, a connection the run's authority may not `CONNECT` (`mlflowConnection`); `mlflowPlan` | `planBayes` calls it for a flow run (`r.trial == 0`), as `planModel` does (`jobs_models.go:575`), and keeps the plan in `modelStep.mlflow` |
| The logging | `logModelMLflow` → `mlflowLog`: the experiment by name (created with `tabularia.created`), the run «flow · node · date», `LogBatch` of parameters, metrics and tags, the artifacts through the proxy, `EndRun` before the registration, the version with its tags, the run FINISHED, the record on Tabularia's run (`mlflow_*`, the `mlflow` object the run page reads); a failed step: `logFailedModel` | Reused whole; branches for the Bayesian parts |
| The hook | `importModel` in `jobs_models.go` (shared by `runModel` and `runBayes`) calls `logModelMLflow` when `s.mlflow != nil && r.trial == 0` (`jobs_models.go:968`) with `modelFiles{storage, results, artifacts, engineBucket, engineResultsKey}` | Nothing to add: `runBayes` already ends in `importModel`; it only needs `s.mlflow` set |
| What `runBayes` lacks today | `jobs_bayes.go` never calls `planMLflow` (its header says «nothing logged to MLflow»), `bayesStep.task` never sets `task.MLflow`, and `runBayes` does not call `logFailedModel` (only `runModel` does, `jobs_models.go:732`) | The three lines of lot M1 |
| The runner's side | `bayes_card.py` writes `model.json` (the common keys and the object `bayes`: `model`, `program` (sha256, bytes), `binary` (sha256, CmdStan, image), `mapping`, `data`, `units`, `sampler`, `facts`, `summary` of the scalar quantities, **no draw**) and `MLmodel` when `task.raw["mlflow"]` is set; `bayes_run.py:688` copies them to the artifacts; `bayescontract.Card` is the Go twin | Read as it is; no change to the runner |
| The card's ceiling | `max_card_bytes` (1 MiB): the summary is cut first, then the units' labels, then `note_card_too_large` and **no card, no `MLmodel`** | A step without a card logs parameters, metrics, the summary table and the report, registers nothing, and says so (the existing warning «The runner wrote no model card») |
| The artifacts | `mlflowArtifacts`: uploads `tabularia/results.parquet` first (the call that tells a server without the artifact proxy), `coefficients.csv` (the `coef` section: **a Bayesian run has none**, so it returns nil), `MLmodel` (with the run's id by `withRunID`), `model.json` (kept: its sha256 is the tag), the model files of the other groups, then `modelReportPDF` | Branches: the first upload becomes `tabularia/summary.csv`; `program.stan` is added; `results.parquet` is **not uploaded** (owner, 2026-11-24) |
| The report | `modelReportPDF` asks the engine for the layout of the Report tab in the flow owner's language (`POST /reports/model`, formats `pdf`); `bayes_model`'s layout comes with `bayes-report` | Reused as it is; a render that fails is a warning, as for the other models |
| The parameters | `flattenParams(s.checked)`: for a Bayesian node `s.checked` holds only `mode` and `output` | A function of its own, `bayesParams`, from the node and the stored version |
| The metrics | `modelMetricsChecked(rows, at)`: logs every number of the `model` section (**for a Bayesian run that would log `chains`, `seed`, `adapt_delta`, `threads`, the seconds…** as metrics beside their parameters), the `parameter` estimates, assumptions, importance, learning curves | A function of its own, `bayesMetrics`, over the `summary`, `sampler` and `model` rows |
| The tags | `mlflowTags`: `tabularia.url`, `.flow_id`, `.flow_name`, `.flow_version`, `.node_id`, `.node_name`, `.run_id`, `.step_run_id`, `.model` (**`bayes_model`**), `.group` (**`bayes`**), `.group_version`, `.image_digest`, `.input_rows`, `.dev_sample`, `.launched_by`, `mlflow.source.*` | Reused; `bayesTags` adds the saved model's |
| The fake and the real server | `mlflowconn/mlflowtest` (a fake with every call recorded and a `Leak` string no error may show), `api/mlflow_loopback_test.go`, `jobs_models_mlflow_test.go` (the run, the keys, the failures, the trial, the permission); the connection's page says the client was tried against a real MLflow 2.22.0 | The tests of this module are written in the same style; the real server is the optional one of `docs/deploy/kind.md` §14 |
| The node's panel | `components/ui/ModelForm.vue` holds the MLflow block (`models.mlflow.*` keys, `modelMlflow`, `modelMlflowProblem`, `withoutRegistration` in `composables/modelCatalog.ts`); `components/bayes/BayesNodePanel.vue` has none (the node spec: «None in this module») | A section of the Bayesian panel with the same fields and keys («The panel») |
| The run page | `components/models/ModelRunDetail.vue` shows the experiment, the run's link and the registered version from the run's `mlflow` object; the Bayesian child has kind `model` (the node spec) | Nothing: the Bayesian step's record is read as every model's |

## What is logged

### The run, the tags

As the models spec's table; the run's name «flow · node · date» (the node's label is the Bayesian node's). To the common tags
(`bayesTags`):

| Tag | Value |
|---|---|
| `tabularia.bayes_model_id`, `tabularia.bayes_model_version` | the saved model's id and the pinned version (`card.bayes.model`) |
| `tabularia.bayes_model_name` | the saved model's name, cut to MLflow's length (data) |
| `tabularia.bayes_semantic_hash` | the version's semantic hash |
| `tabularia.program_sha256`, `tabularia.binary_sha256` | the program's and the binary's hashes (`card.bayes.program`, `binary`) |
| `tabularia.cmdstan` | CmdStan's version (`card.bayes.binary.cmdstan`) |
| `tabularia.sampler` | `nuts` |
| `tabularia.card_sha256` | on the registered version only (as every group) |

### The parameters (`bayesParams`)

From the node and the stored version, **logged before the artifacts** (as the other groups log theirs): `model.id`,
`model.version`, `sampler.chains`, `sampler.warmup`, `sampler.draws`, `sampler.seed`, `sampler.adapt_delta`,
`sampler.max_treedepth`, `sampler.init` (`default`, `radius <r>` or `values`), `output`, `replicated_draws` (only with the
output `replicated`); for each bound data variable `data.<name>.column`, `data.<name>.standardized` and, when standardized,
`data.<name>.mean` and `data.<name>.sd` (from the `data` rows: they are what a reader needs to put a coefficient back on a
column's scale). `<name>` is the variable's name, else its Stan identifier. At most 100 per call: `mlflowconn.Batches`
splits. A key MLflow does not take is cleaned and a collision kept apart and warned, as `flattenParams` does.

### The metrics (`bayesMetrics`)

| Key | Source | Holds |
|---|---|---|
| `summary.<quantity>.mean`, `.sd`, `.q5`, `.q50`, `.q95`, `.rhat`, `.ess_bulk`, `.ess_tail` | the `summary` rows with no unit (`level_2` null), the report's scope in its order, at most `bayesMLflowQuantities` quantities | the posterior summary of a scalar; R-hat and the effective sample sizes are measures |
| `summary.<quantity>.mcse_mean` | the same | the standard error of the mean |
| `sampler.divergences`, `sampler.treedepth_hits`, `sampler.e_bfmi_min`, `sampler.accept_stat_mean`, `sampler.stepsize` | the `sampler` row `all` (the minimum of the chains' E-BFMI) | facts |
| `run.n`, `run.n_missing`, `run.n_not_finite`, `run.seconds_warmup`, `run.seconds_sampling` | the `model` rows | counts and times |
| `quantity.<node>.rhat_max`, `.ess_bulk_min`, `.ess_tail_min` | the `quantity` rows (a plate's quantities together), at most 100 nodes | the extremes over a node's quantities, so a plate's worst are in MLflow without its units |

**Not metrics**: `chains`, `warmup`, `draws`, `seed`, `adapt_delta`, `max_treedepth`, `threads` (they are parameters;
`modelMetricsChecked` is therefore not used for this group), anything of `curve`, `hist`, `point`, `ppc`, `prior_check`,
`data`. The keys are cleaned and cut by `metricKey`/`shortKey` (a quantity named `alpha[Liceo Galilei]` becomes a valid key
that a hash ends when it is cut), two quantities that clean to the same key are kept apart (`_2`) and said in the run
page's warnings; the timestamp is the run's start, the step 0 (a posterior summary has no history).

### The artifacts

| Path in the run | Content | Source |
|---|---|---|
| `tabularia/summary.csv` | one row a quantity of the report's scope (the scalars, the correlations, each plate's quantities with their unit): `quantity`, `unit`, `mean`, `sd`, `q5`, `q50`, `q95`, `rhat`, `ess_bulk`, `ess_tail`, `mcse_mean`, and for a linear predictor's coefficient the `orig_*` columns; at most the report's 2,000 quantities; **numbers and the names the person gave, as text, never a formula** | the `summary` rows (`bayesSummaryCSV`) |
| `tabularia/report.pdf` | the Report tab's layout, in the flow owner's language | `modelReportPDF` |
| `model/MLmodel` | the `tabularia` flavor naming `model.json`, with the run's id | the runner; `withRunID` |
| `model/model.json` | the card (**no draw**), its sha256 the version's tag | the runner |
| `model/program.stan` | the stored program | the gateway, from the pinned version |
| *(`tabularia/results.parquet`)* | **never uploaded** (owner, 2026-11-24): it holds thinned trace draws and bins | — |

An artifact the server cannot keep (no artifact proxy) is the existing case: parameters, metrics and tags are logged, no
file, the run page says so, **and nothing is registered**.

### The registration

With `registerAs`: `registered-models/create` when missing, then `model-versions/create` with `source` the run's `model`
artifact path, `run_id` and the tags `tabularia.model` (`bayes_model`), `tabularia.group` (`bayes`),
`tabularia.group_version`, `tabularia.variant` (`sample`), `tabularia.card_sha256`, and the `tabularia.bayes_*` tags above.
No `tabularia.model_file_sha256`: the group has no model file. The registration is the last act; a registration that fails
without an answer drops its version (`dropRegistered`) and sets the run FAILED, as the other groups'. A card that is absent
(`note_card_too_large`), a server with no registry or no proxy: the step's warning, no version, as today.

### Predict

A version `tabularia.model = bayes_model` passes today's checks of `planPredict` (it has `tabularia.model`, a card whose hash
matches, an `MLmodel` with the `tabularia` flavor: verified in `jobs_predict.go`) and would start a Job of the group `bayes`
whose runner has no `predict` mode (`bayescontract.Modes` is compile, prior_predictive, sample, selftest): a late failure.
Lot M2 adds the refusal **in `planPredict`** (owner, 2026-11-24), after the model is read from the tag and before any Job: «the version N of «x» holds a Bayesian model, which
Predict does not apply: a Bayesian model's draws are read in its report and its summary» (a sentence for the five languages
is not needed: the gateway's refusals are English, as the others').

## The panel

A section «MLflow» in `components/bayes/BayesNodePanel.vue`, after the output and before the limits, **with the model node's
fields and words** (`models.mlflow.*`, already in five languages): the connection (`mlflow` connections the person may use,
or none), then, when one is chosen, the experiment's name (required, with the experiments of the server offered as
`ModelForm` does through `/models/mlflow/{connection}/experiments`), the registered model's name (optional; **a registered
name is cleared when the connection is**: `withoutRegistration`), and «Continue without MLflow» (off). One sentence says what
goes to MLflow: «the parameters, the summary of the quantities, the sampler's facts, the report and the model's files; never the
draws». The node's `data.mlflow` is the same object as a model node's, so `planMLflow` and `flowaccess/reach.go` (which
reaches a flow's connections) read it as it is (verified: `reach.go` names `mlflow`).

The component is written for the Bayesian panel (`components/bayes/BayesMlflow.vue`) from the helpers `modelMlflow`,
`modelMlflowProblem` and `withoutRegistration` and the keys of `models.mlflow`, **without touching `ModelForm.vue`** (spec
decision, confirmed by the owner on 2026-11-24: extracting a shared component would change a form every other group uses). Every
control is reachable by keyboard on a Mac and on a laptop without F/Home/End keys (the owner's rule); the five languages'
catalogs hold the one new sentence (`bayes.node.mlflowNote`), checked by `tests/i18n.test.mjs`.

## Tech stack

- **Go** (the gateway): `internal/api/jobs_bayes.go` (the plan, the task flag, the failed-step call), a new
  `internal/api/jobs_bayes_mlflow.go` (`bayesParams`, `bayesMetrics`, `bayesTags`, `bayesSummaryCSV`, the program upload),
  branches in `jobs_models_mlflow.go` on `modelStep.bayes`, `jobs_predict.go` (the refusal); no new module, no new package.
- **TypeScript/Vue**: `BayesMlflow.vue`, its section in `BayesNodePanel.vue`, one key in five locales.
- **Documentation**: the MLflow section of `docs/capabilities/models.md` gets the Bayesian paragraph (small, written with the
  lot; the module `docs` consolidates the rest).
- **The runner is not changed.**

## Commands

```bash
cd gateway-go && go test ./internal/api/ -run 'Bayes|MLflow' && go test ./internal/mlflowconn/... ./internal/bayescontract/...
cd gateway-go && go vet ./internal/api/ && gofmt -l internal/api internal/mlflowconn
cd frontend && node --experimental-strip-types --no-warnings=ExperimentalWarning --test tests/i18n.test.mjs tests/bayes-node.test.mjs
cd frontend && npx nuxi prepare && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
```

(no `-n`/parallelism on this machine; the Go tests of `api` use the fake MLflow and the fake cluster, no real one.)

## Project structure

```text
gateway-go/internal/api/jobs_bayes.go             (changed) planBayes calls planMLflow; task.MLflow; runBayes calls logFailedModel
gateway-go/internal/api/jobs_bayes_mlflow.go      bayesParams, bayesMetrics, bayesTags, bayesSummaryCSV, the program's upload and check
gateway-go/internal/api/jobs_models_mlflow.go     (changed) branches on s.bayes in mlflowLog and mlflowArtifacts
gateway-go/internal/api/jobs_models.go            (changed) modelStep gains what the Bayesian log needs (the program, the data rows' names)
gateway-go/internal/api/jobs_predict.go           (changed) the refusal of a Bayesian version
gateway-go/internal/api/jobs_bayes_mlflow_test.go the content of a Bayesian run in MLflow, the keys, no draw, the failures, the trial
frontend/components/bayes/BayesMlflow.vue         the section
frontend/components/bayes/BayesNodePanel.vue      (changed) the section
frontend/i18n/locales/*.gen.ts                    one key
docs/capabilities/models.md                       (changed) the MLflow section's Bayesian paragraph
```

## Code style

The Bayesian metrics in the voice of `modelMetricsChecked` (verified): every raw name goes through `names.add`, gets its key
at the end, collisions are said.

```go
// bayesMetrics is the Bayesian run's metrics: the sampler's facts, the counts, and the posterior summary of the
// scalar quantities (at most bayesMLflowQuantities, in the report's order). Never a draw: it reads the summary,
// sampler, quantity and model rows and no other section.
func bayesMetrics(rows []resultRow, at time.Time) ([]mlflowconn.Metric, []string) {
	names := newKeyNames("metrics of the model")
	var out []mlflowconn.Metric
	var named []int
	add := func(raw string, v float64) {
		if names.add(raw) {
			named = append(named, len(out))
			out = append(out, mlflowconn.Metric{Key: raw, Value: v, Timestamp: at.UnixMilli()})
		}
	}
	// ...
	names.assign()
	for _, i := range named {
		out[i].Key = names.key(out[i].Key)
	}
	return out, names.warnings()
}
```

## Testing strategy

**On small data**: the hierarchical-slopes fixture (`api/testdata/bayes_task_hier_slopes.json` and the report spec's
`bayes_model-sample.json`), the fake MLflow, no cluster.

- **The content of a run** (`jobs_bayes_mlflow_test.go`, in the style of `TestTheContentOfAnMLflowRun`): the experiment, the
  run's name, the tags (the common ones, the Bayesian ones, no `tabularia.model_file_sha256`), the parameters, the metrics
  (every key within 250 characters and apart), the artifacts' list **exactly** `tabularia/summary.csv`,
  `tabularia/report.pdf`, `model/MLmodel`, `model/model.json`, `model/program.stan` (and nothing else), the version and its
  tags.
- **No draw, ever**: the artifacts' bytes are scanned for every value of a `curve|trace` row and of the draws' fixture (none
  appears); the metrics hold no key of `curve`, `hist`, `point`, `ppc`, `prior_check`; a run whose output is `draws` or
  `replicated` logs the same files (its parquet never reaches MLflow); the card is the contract's, checked by the Go twin
  `bayescontract.Card` (a field that would hold an array of draws does not exist).
- **The program**: `model/program.stan` equals the stored program; a program whose sha256 differs from the card's is not
  uploaded and the step fails (a fault of Tabularia, said).
- **The caps**: 2,000 quantities in the report's scope give 100 quantities' metrics and a 2,000-row CSV; 200 data variables
  stay within the parameters' batches; keys of hostile names (`=HYPERLINK("http://x","y") #set ] 1% <b>`, brackets, a name of
  300 characters, two names that clean to one) are valid, apart and warned; the CSV keeps them as text (a leading `=`, `+`,
  `-`, `@` neutralised).
- **The failures**, as the models': MLflow refusing credentials or not answering fails the step with the sentence and never a
  server's text (the `Leak` string); with «Continue without MLflow» a warning; a server with no proxy or no registry logs
  what it can; a run that cannot be closed registers no version; a failed sampling Job is a FAILED run with `tabularia.error`;
  a **trial** (and the prior predictive check) creates no experiment, no run.
- **Permissions**: a connection the run's authority may not use stops the step before the compile is even asked (`planBayes`
  order: the compile after the plan's checks, the node spec).
- **Predict**: a version of `bayes_model` is refused with its sentence, before any Job; the other models' versions are
  applied as before (`jobs_predict_test.go` unchanged).
- **The other groups**: `jobs_models_mlflow_test.go` and `jobs_predict_test.go` pass **without a line changed** (the branches
  are on `s.bayes`).
- **The panel**: the section's states (no connection, a connection and no experiment: the problem sentence, a registered
  name cleared with the connection), five languages with the same placeholders, no vue-i18n syntax.
- **By hand (lot M3)**: a flow with a Bayesian node and an MLflow connection on kind, against the optional real MLflow
  server of the kind guide (`docs/deploy/kind.md` §14): the run's page, its link, the artifacts opened in MLflow's interface,
  the registered version, Chrome and Firefox for the panel.

## Boundaries

- **Always**: the gateway logs; the summary, never the draws; a measure or a fact, never a verdict; names as data; the other
  groups' tests unchanged; the connection checked with the run's authority at every run.
- **Ask first**: a new metric family, tag or artifact; uploading anything of a draw, a trace or `results.parquet`; a Predict for Bayesian versions; a change to `planMLflow`, `mlflowLog` or `mlflowArtifacts` that
  the other groups would see.
- **Never**: `results.parquet` in MLflow; the Job holding the connection's secret; a draw, a thinned chain, a replicated row in MLflow; a threshold, a
  reference value or a verdict word in a tag, parameter, metric or file name; a trial logged; a registered version that cannot
  be told from a Predict-able one; a Python route.

## Success criteria

1. A flow run of a Bayesian node with an MLflow block records its parameters, metrics, the five files (no `results.parquet`) and, with `registerAs`,
   a version, and the run page shows the link and the version.
2. **No file or number in the run is a draw**: the test of «No draw, ever» passes for the three outputs.
3. The metrics of a model with 2,000 quantities stay within the caps; every key is valid and apart; the CSV never holds a
   formula.
4. The failures and the options (`continueWithout`, no proxy, no registry, a card too large, a trial) behave as the models
   spec says for every group.
5. Predict refuses a Bayesian version before any Job.
6. The other groups' MLflow and Predict tests pass untouched.

## Lots

| Lot | Objective | Files | Accepted when | Depends on | Agent |
|---|---|---|---|---|---|
| **M0** | The pure functions: `bayesParams`, `bayesMetrics`, `bayesTags`, `bayesSummaryCSV`, the program's check, over the fixtures' rows; the caps and the key tests | `jobs_bayes_mlflow.go`, its tests | criterion 3; the keys' and the CSV's tests | the report spec's fixtures (Q1) | sonnet |
| **M1** | The wiring: `planBayes` → `planMLflow`; `task.MLflow`; the branches in `mlflowLog` and `mlflowArtifacts`; `runBayes` → `logFailedModel`; the program's upload | `jobs_bayes.go`, `jobs_models_mlflow.go`, `jobs_models.go`, tests | criteria 1, 2, 4, 6 on the fake MLflow | M0; the node spec's F2 | **opus** (a shared path of every model; the permission and failure cases) |
| **M2** | Predict's refusal of a Bayesian version | `jobs_predict.go`, its test | criterion 5 | — | sonnet |
| **M3** | The panel's section and the five languages; the models page's MLflow paragraph | `BayesMlflow.vue`, `BayesNodePanel.vue`, locales, `docs/capabilities/models.md`, tests | the panel's tests; `tests/i18n.test.mjs` | the node spec's F3 | sonnet |
| **M4** | On kind against a real MLflow server | «As built» | criterion 1 by hand | M1, M3; the node spec's F5 | sonnet (by hand, alone) |

M0, M2 and M3 can start at once; M4 is the only heavy lot (one heavy job at a time on this machine).

## Open questions

None. The four questions of the draft were answered by the owner on 2026-11-24 (see «Decisions»).
