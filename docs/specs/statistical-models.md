# Spec: statistical models

Status: **draft, 2026-11-19, revised the same evening after the owner's decisions
(per-model nodes with their variants inside, Predict in the first release, automatic ARIMA
orders, everything in R). This file is not committed.** **Built by 2026-11-20: every piece**
(0, A, B, C1, C2, D, E1, E2, G, H, I1, I2, I3, M1–M7; «Pieces» lists their commits), after a
final review whose corrections are in «As built, the final review». Migration 0044 is written
(`3c11f300`); the owner's approval of it is still to be confirmed. Where the built code settled a fact this spec had left
open, or differs from what the spec first said, the text below says what the code does, and
the blocks called «As built» collect those facts per model; what the builders ask the owner to
decide is in «Open questions for the owner». Decisions marked
**(owner)** are the owner's; «Provisional decisions» lists what this spec decided for the
owner to confirm; the rest are this spec's choices. Work started with piece 0, the common
contract; «Pieces» says what can run in parallel. **Added on 2026-11-20, built the same day (lots X0–X9, all done)**: the group `boosting` in Python, with the nodes *XGBoost* and *CatBoost* (owner's
request of that day): its contract is «Group `boosting` — Python», its lots «The boosting
group's lots», its questions 16–22 of «Open questions for the owner». **Added on 2026-11-20, planned,
not built**: the node *Parametric survival* in the group `survival` (owner's request of that day:
Weibull, exponential, log-normal, log-logistic, generalized gamma, Gompertz, or the lowest AIC of the
six, with Predict): its contract is «The node «Parametric survival»», its lots «The parametric
survival lots» (P0–P12), its questions 24–27 of «Open questions for the owner».

Words used below: **verified** = read in this repository's code on 2026-11-19;
**assumed** = known from the packages' and MLflow's public documentation but not run or
checked against a pinned version today; every assumed fact is checked by the piece that
first depends on it, and the spec is corrected where the fact differs.

## Objective

A person who prepares data in a flow fits a statistical model on the rows a step
produces, without leaving the canvas: «how much does the price move the quantity sold,
net of the season and the channel?», «which customers are likely to leave?», «how long
until a machine fails, and what shortens it?», «how many boxes of pasta next quarter?».
They drop the node of the model they want, choose the columns and the options that model
has, press **Run**, and get, in the flow editor:

- a **table** that the next nodes read like any other data: the coefficients with their
  standard errors, tests and intervals, the measures of fit, the results of the tests of
  the model's assumptions; or, when they ask for it, the input rows with the model's
  fitted values, residuals and predictions; or the forecast of a series;
- the **Report** of the model, pixel-perfect, drawn by Tabularia's report engine with a
  layout made for that model, shown in the node's preview beside the table, downloaded as
  **PDF or Excel**. Besides the coefficients and the fit it holds **the tests of the
  model's assumptions and their results**, each with its verdict and what to do when it
  fails, and the diagnostic charts that go with them (residuals against fitted values,
  normal Q–Q, survival curves, the correlogram of the residuals, the forecast with its
  bands);
- when the node names an **MLflow** connection and an experiment, every run of the flow
  is logged there (parameters, metrics, artifacts, the model), and the model can be
  registered as a new version that a **Predict** node applies to new rows, adding the
  predicted column.

The computation runs **on Kubernetes only**, in an image of its own per group of models,
built in the cluster when an administrator enables that group: the computational weight
is configured on those images, never on Tabularia's workers.

Readers: analysts who know which model they want and need its numbers and its
diagnostics in one place; people who do not, and need the node to refuse a wrong setup
with a sentence they understand and the report to say which assumption failed;
administrators, who decide which groups exist on their cluster, their resources and who
may use them; whoever receives the PDF and must be able to check it.

## Decisions

| Question | Decision | By |
|---|---|---|
| Where models run | **Kubernetes only**, as Jobs, in an image separate from Tabularia's, «to leave the configuration of the computational weight to a separate image». On Docker Compose the nodes and the admin section are visible and greyed out, «requires Kubernetes», as Script nodes are | Owner |
| Language | **Everything in R in the first release**: one R image per group (regression, survival, time series). Python comes with the more complex models of later releases, as groups of their own | Owner |
| Images | **Groups of models**, each with its own recipe in the repository (language, base image, packages pinned by version, reference values). **Not built by default.** An administrator **enables** a group: the cluster builds its image then, checks it against its reference values, and only then do its nodes appear in the palette. A new group is added without touching the others | Owner |
| How the images are built | **The scripting environments' machinery, unchanged in its essentials**: a group is a scripting environment *managed by Tabularia* (hidden from the Scripting section and from the Script node), built by the same rootless BuildKit Job, checked by the same check Job, versioned the same way. The check also runs the group's reference cases: a version whose numbers differ is **failed**, not ready | Spec |
| Where the code that fits the models lives | In Tabularia's repository, **not in the image**: the image holds the packages (heavy, built once); the runner code (light) travels with each Job in its ConfigMap, as a Script node's files do. A Tabularia upgrade that changes a group's recipe queues a rebuild of that group by itself | Spec |
| MLflow | **External to Tabularia, a new kind of connection** (`mlflow`): tracking URI, authentication, credentials encrypted like every connection's, tested from the connection dialog. **The experiment's name is a parameter of the node** | Owner |
| Who talks to MLflow | **The Go gateway, not the Job and not the engine.** The Job writes its files to the scripts bucket; the gateway reads them back (through the engine's `POST /scripts/objects/read` and `POST /scripts/results/rows`, which hold the bucket's credentials), decrypts the connection's password itself and logs to MLflow. The engine never sees the MLflow credentials; the MLflow token never reaches a pod that runs model code, and the model Jobs run with internet access off | Spec |
| How many nodes | **One node per model, its variants inside.** Five nodes: *Linear regression* (OLS, weighted, robust standard errors; ridge, lasso, elastic net as options), *Logistic regression* (the same variants and penalties), *Kaplan–Meier*, *Cox*, *ARIMA* (one node, one id, covering AR, MA, ARMA, ARIMA, SARIMA and SARIMAX through its orders p, d, q, P, D, Q, s and its exogenous columns; the palette may show six entries that are presets of that same node). Each node has its own report with its own assumption tests; when a penalty is on, the form and the report change with it (no p-values; the cross-validation curve and the coefficient paths) | Owner |
| Models of the first release | The five above. ARIMA also chooses its orders automatically when asked, in the manner of `auto.arima` (information criterion, stationarity tests), and its report says which orders it chose and why, with the table of the candidates | Owner |
| Output | **The statistics' ten columns**, the same table shape as every `stat_*` node (`section`, `level`, `level_2`, `stat`, `value`, `text`, `x0`, `x1`, `ord`, `key`), with sections of its own for models. Optionally the input rows with the model's columns, or a forecast | Spec |
| Charts | **Drawn by Tabularia's report engine, never by R or Python.** The runners send numbers and the points of the charts (residuals, quantiles, curves, bands, autocorrelations), capped in number | Owner |
| The report | The **Report** tab of the node's preview, as for the tests: a layout preset per model, read-only, **PDF and Excel**; it includes **the tests of the assumptions and their results**, chosen by the model's statistical requirements | Owner |
| When a model is computed in the editor | **With a button** (**Run** in the node panel, as for a Script node), on the development sample of the sources; never at each change. The Report and Table tabs show the latest trial or run, marked «Not up to date» when the node or anything before it changed since | Owner |
| Data while developing Tabularia | **Small data.** «Sviluppa su dati piccoli, è inutile e deleterio sviluppare su dataset giganti» (owner): development, reference values and the checks on kind and minikube use small tables, tens to a few thousand rows. The limits for large data are written as limits with clear messages and tested by lowering the limit, never by building giant datasets | Owner |
| Predict | A **Predict** node («Previsione») applies to new rows a model registered in MLflow, chosen by experiment and version, and adds the predicted column. **In the first release** of the models | Owner |
| Release | The next minor version on `main`; never the 1.4 line | Project rule |
| Gradient boosting (2026-11-20) | A Python group `boosting` with one node per model, *XGBoost* and *CatBoost*; regression, binary and multiclass chosen in the node; gain and mean \|SHAP\| with a beeswarm in the report; hyperparameters with defaults, early stopping on a validation part, learning curves; diagnostics instead of assumption tests; the model saved as XGBoost JSON or CatBoost `cbm`, never pickle. Details in «Group `boosting` — Python» | Owner |
| Parametric survival (2026-11-20, planned) | One node *Parametric survival* in the group `survival` (R, its image extended with `flexsurv`): Weibull, exponential, log-normal, log-logistic, generalized gamma, Gompertz, or `auto` (the lowest AIC, the comparison in the report); time ratios for every AFT distribution, hazard ratios for Weibull, exponential and Gompertz beside Cox's; Predict of the median, the quantiles and S(t) from its first lot. Details in «The node «Parametric survival»» | Owner |
| Parametric survival: `auto` (2026-11-20) | **Parsimony rule**: the lowest AIC, unless a simpler nested distribution is within 2 of it, then the simpler; Gompertz stays a candidate; left and interval censoring not now; predictions beyond the data stay with the warning; no thresholds anywhere (2026-11-20: verdicts only from tests with a p-value, against the node's alpha). Details in «Automatic choice» | Owner |

## What exists and is reused (verified)

| Need | Already there | New |
|---|---|---|
| Images built in the cluster | `gateway-go/internal/api/jobs_script_builds.go`: the build loop (advisory lock 7402131), the Dockerfile from a language, a base image and a package list (`dockerfile`, `sbParsePackages`; Python `name==version`, R `name@version` through `remotes::install_version`, CRAN only), rootless BuildKit in the build namespace, the check Job in the scripts namespace that prints the lock, versions, digest, architecture, failures explained (registry refused, out of memory) | A *managed* environment per group; the check also runs the group's self-test |
| Where the images run | The scripts namespace (`restricted`: user 10001, read-only root, no capability, no ServiceAccount token), the trials' namespace with its ResourceQuota (`scripts.trialQuota`), the NetworkPolicy `scripts.blockTabularia` (`templates/user-code-networkpolicy.yaml`), a version's **Internet access** switch and the egress proxy that lets a version without internet reach only the storage (`gateway-go/internal/egress`, `templates/scripts-no-internet.yaml`, migration `0042_script_internet_access.py`) | Model group versions have internet access **off** |
| A step that is a Kubernetes Job | `gateway-go/internal/api/jobs_scripts.go`: the input written by the engine to the scripts bucket (`env-<id>/<step_id>/input.parquet`), a ConfigMap with the code, a Secret with the bucket's key, the Job by digest, polling, retries as new Jobs, the log kept (`SCRIPTS__LOG_MAX_BYTES`), the output imported into Tabularia's storage by the engine (`importOutput` → `POST /scripts/output/import`, `backend/app/api/routes/scripts.py`), clean-up by exact key | A model step uses the same functions with a fixed runner instead of the user's code |
| A step boundary in the resolver | `flowresolver.StepNodeTypes = {"script", "container"}`, `actionTypes`, `defaultPriority` (`gateway-go/internal/flowresolver/flowresolver.go:40, 743, 745`) | `model`, `model_predict` |
| The editor's trial and the preview after a step | `POST /scripting/flows/{id}/nodes/{node}/run` (`scripting_trial.go`, a run with `trigger_type = 'trial'`), `GET /scripting/flows/{id}/outputs` (`scripting_runtime.go`: latest output, `stale` by fingerprint) | The model node's fingerprint is its parameters |
| The ten-column table, R's stars, the report of a node | `backend/app/engine/stats/output.py` (`OUTPUT_COLUMNS`), `compute.significance`, `stats/layouts.py` (bands, sample line, `_signif_legend`, `_table`, `_document`), `report_texts.json`, `frontend/components/ui/StatReportView.vue`, `composables/statReport.ts`, the export queue | Layouts per model, built with the same helpers (made public, not copied) |
| Chart kinds | `CHART_KINDS` in `backend/app/reports/definition.py`, including the engine-only `boxplot`, `interval`, `density`, and the chart `filter` | One engine-only kind, `xy` («Charts») |
| A catalogue copied to every component | `backend/app/engine/stats/catalog.py` → `catalog.json` → `.github/scripts/stat_catalog.py` copies to the interface, the Go gateway and the AI service, `--check` in CI | The same for models |
| Connections | `connections` table (folder-scoped, `CONNECT` to use), secrets encrypted with Fernet (`encryptSecret` in `connections_create.go`, `backend/app/core/crypto.py`), the kind's options checked in `validExtra`, the draft tested by the engine for the other kinds (`engineInspect` → `/…/inspect`, `connections_inspect.go:123`); `mlflow` is tested by the gateway itself (`gateway-go/internal/mlflowconn`, `api/connections_mlflow.go`), the redaction of secrets and «credentials stay on the API's host» of `backend/app/ingest/http_api.py` | `db_type = "mlflow"` |
| Admin sections | `frontend/components/AdminPanel.vue` with one component per section in `frontend/components/admin/` (`ScriptingSection.vue`, `RegistriesSection.vue`) | `ModelsSection.vue` |
| Refusal from API tokens | `scNoScriptingFromToken` (`scripting.go`) | The same for the models section |
| dbt export | Flows with Script and Container nodes are refused (`dbtexport/export.go:1164`, `script_not_exportable`) | `model` and `model_predict` too |

## The map of the parts

```text
                ┌───────────────────────┐
                │ 0 Common contract     │  (everything reads it)
                └───────────┬───────────┘
          ┌─────────────────┼──────────────────────┐
          ▼                 ▼                      ▼
 ┌──────────────────┐ ┌───────────────┐  ┌──────────────────────┐
 │ 2 Groups, images │ │ 4 Nodes, form │  │ 6 Report (layouts,   │
 │   admin enable   │ │   palette     │  │   xy chart, route)   │
 └────────┬─────────┘ └──────┬────────┘  └──────────┬───────────┘
          ▼                  ▼                      │
 ┌──────────────────────────────────┐               │
 │ 3 Execution: runner protocol,    │◄──────────────┘ (reads the results it writes)
 │   Job, trial, import of results  │
 └────────┬─────────────────────────┘
          ▼
 ┌──────────────────┐     ┌──────────────────────┐     ┌─────────────────┐
 │ 7 MLflow conn.   │────►│ 8 MLflow tracking    │────►│ 9 Predict node  │
 │   (independent)  │     │   (needs 3 and 7)    │     │ (needs 8)       │
 └──────────────────┘     └──────────────────────┘     └─────────────────┘
```

| Part | Depends on | What it is |
|---|---|---|
| 0 Common contract | — | The catalogue (every model, its variants, parameters, kinds, defaults, the numbers each variant writes); the ten columns and their check in the engine; the runner protocol (`task.json`, the files written, `error.json`, exit codes); the R library every runner shares; the model card's schema; the three recipes; the migration |
| 2 Model groups and admin enabling | 0, the scripting environments | Recipes, managed environments, the admin section, build, self-test |
| 3 Execution | 0 (and 2 to run on a cluster) | The runner protocol, the Job, the trial, the import of results and outputs |
| 4 Nodes and forms | 0 | Palette, form from the catalogue, node panel, Run |
| 5 The five models | 0 (each a runner file, a reference case, a layout) | Linear, logistic, Kaplan–Meier, Cox, ARIMA |
| 6 Report with assumptions | 0, the report engine | Layouts per model, the `xy` chart kind, the route, the Report tab |
| 7 MLflow connection | — | Connection kind, checks, test |
| 8 MLflow tracking | 3, 7 | Logging, model card, registration, links |
| 9 Predict | 8 | A registered version applied to new rows |

## The nodes

### What a model node is

A model node is a **step node** of the flow, like Script and Container: a run executes it
as a Kubernetes Job, the chain before it ends at its input, the chain after it starts from
its output. It is **not** an engine operation (the `stat_*` tests are): no engine computes
a Cox model.

```json
{ "type": "model",
  "data": { "model": "linear",
            "params": { "outcome": "quantity", "predictors": ["price", "channel", "month"],
                        "se": "hc3", "alpha": 0.05, "output": "results" },
            "mlflow": { "connectionId": 12, "experiment": "Pasta/price elasticity",
                        "registerAs": "price-elasticity" },
            "timeoutSeconds": 900, "retries": 0 } }
```

- One data input on the left, one data output on the right, the sequence handles of the
  other step nodes. A model node with nothing after it still runs: its results stay with
  its run, its report can be opened, and MLflow gets its log.
- `data.model` is the catalogue's id; `params` holds that model's parameters only; a key
  the model does not take is refused, as one it needs and lacks (the statistics rule).
- `data.mlflow` is optional. With a connection, `experiment` is required; `registerAs` is
  optional. `mlflow.continueWithout` (false by default) is the option «Continue without
  MLflow».
- `timeoutSeconds` and `retries` as on a Script node, within the group's maximum.

`flowresolver.StepNodeTypes` and `actionTypes` gain `model` and `model_predict` (priority 1,
as `script`); `FlowEditor.vue`'s `hasControlNodes` counts them, so a flow with a model
runs as an orchestration.

**Columns never become code.** A column's name travels as data in `params.json`. The
runner renames the columns it reads to positional names (`v1`, `v2`…) before it builds a
formula or a design matrix, and maps them back when it writes its results; no user text
reaches `eval`, `parse`, `reformulate` with raw text, `patsy` or `Q()`. A test of each
runner feeds column names such as `` a`b ``, `x); system("id")`, `ü ñ`, and gets them back
as labels.

### Rules every model follows

The statistics' rules, extended:

- **Rows left out.** A row with NULL or NaN in a column the model uses is left out; the
  number is `n_missing`. Nothing is imputed. An infinite value stops the node and names
  the column.
- **Types.** A number column is numeric. A text or boolean column used as a predictor is
  categorical (treatment contrasts); its **reference level** is a parameter
  (`reference: {"channel": "shop"}`), the first label in text order by default (labels as
  `catalog.label_of`). A date or datetime column is refused as a predictor with a
  sentence («compute a number from the date first: the month, the days since a start»);
  it is the time column of a series.
- **Limits, before the Job.** The engine counts the rows when it writes the input; the
  gateway refuses before creating the Job when a limit is passed («Limits»). A limit is a
  sentence that says what to do, never a crash.
- **Only the columns the model reads** are written to its input (the chain ends with a
  `select` of them, the time column first for a series): less to write, less to read.
- **Notes.** A doubt the numbers raise (few events per variable, a perfect fit, a
  boundary λ, very large n making every test significant) is a `note_…` row, an English
  sentence that names the way out.
- **Assumptions are reported, not enforced.** A failed assumption never stops the node; it
  is a row with its verdict and advice, and the report puts it on the first page. Only
  what makes the numbers meaningless stops it: complete separation with maximum
  likelihood, a singular design (an aliased term is named), no events, a series shorter
  than the model needs.
- **Determinism.** Every random choice (cross-validation folds, the sample of the points
  drawn) uses `seed` (a parameter, 1 by default). Linear algebra threads equal the CPU
  limit (`OMP_NUM_THREADS`, `OPENBLAS_NUM_THREADS`). The same input on the same group
  version gives the same numbers.

### In the palette

A group **Models** after **Statistics**, folded and searchable like it
(`frontend/components/ui/OpSidebar.vue`), with one subtitle per **enabled** group the
person may use: *Regression*, *Survival*, *Time series*. A disabled group does not
appear. On Compose the group is shown greyed out with «requires Kubernetes», as the
Script entry is. A flow that holds a node of a group that is not enabled (brought from
another installation, or a group disabled since) opens, shows that node greyed out with
the reason, and does not run.

*Regression* holds *Linear regression* and *Logistic regression*; *Survival* holds
*Kaplan–Meier* and *Cox* (and, planned, *Parametric survival*); *Time series* holds *ARIMA*, and may also show the six presets of
that same node (AR, MA, ARMA, ARIMA, SARIMA, SARIMAX: each drops `arima` with the orders it
leaves free). *Predict* is in the group *Models* above the subtitles, for whoever may use at
least one enabled group and `CONNECT` at least one `mlflow` connection.

### The form

`frontend/components/ui/ModelForm.vue`, drawn from the catalogue as `StatTestForm.vue` is:
each parameter by its kind's control (column, numeric column, columns, level of a column,
choice, number, boolean, orders), only the keys that `applies` allows for the current
variant (turning a penalty on hides `se` and shows λ, folds, mixing; `orders = auto` hides
the orders and shows their maxima and the criterion), the options another option forbids
disabled with the reason, and below, the **MLflow** block (connection: only `mlflow`
connections the person may `CONNECT`; experiment; register as), and **Run**, timeout,
retries. `NodePanel.vue` shows it for `model` nodes; `PredictForm.vue` for `model_predict`.

## The catalogue

`backend/app/engine/models/catalog.py` declares every group and model once, as plain data,
as `stats/catalog.py` does for the tests: `GROUPS[id]` (language, label key, the models in
order) and `MODELS[id]` (group, family, `needs`, `takes`, each key's kind, `defaults`,
`applies`, the outputs it offers, the assumptions it checks, the charts of its report).
`python -m app.engine.models.catalog` writes `catalog.json`;
`.github/scripts/model_catalog.py` copies it to `frontend/assets/model-catalog.json`,
`gateway-go/internal/modelcatalog/catalog.json` (embedded) and
`gateway/app/services/model_catalog.json` (the assistants), and `--check` fails in CI when
a copy differs by a byte (`.github/workflows/model-catalog.yml`, as `stat-catalog.yml`).
The runner receives the gateway's copy in its ConfigMap.

Keys shared by many models mean the same wherever they appear:

| Key | Kind | Meaning |
|---|---|---|
| `outcome` | column | The response: a number (linear), two values (logistic), ordered or unordered categories |
| `event_value` | a level of `outcome` or of `event` | Which value is the event (logistic: the 1; survival: the death, the failure) |
| `predictors` | columns | The explanatory columns, numbers or categories |
| `reference` | object column → level | Reference level of a categorical predictor |
| `interactions` | pairs of predictors | Products of two predictors (`price × channel`) |
| `intercept` | boolean | true by default |
| `weights` | number column | Case weights (≥ 0) |
| `se` | choice | How the standard errors are computed (per model) |
| `cluster` | column | The cluster of the clustered standard errors |
| `order_by` | column | The order of the rows, for the tests that depend on it (Durbin–Watson, Breusch–Godfrey) |
| `alpha` | (0, 1) | Significance level, 0.05; intervals at 1 − alpha |
| `seed` | integer | Every random choice |
| `output` | choice | `results` (default), `rows`, `table` (Kaplan–Meier), `forecast` (series) |

### How a variant shows in the catalogue

A model's variants are values of its own parameters, not other models. Two keys organize
them:

- `estimation` — how the model is estimated, where it has several ways: linear `ols` \|
  `wls`; logistic `ml` \| `firth`; ARIMA orders `manual` \| `auto`;
- `penalty` — `none` (default) \| `ridge` \| `lasso` \| `elastic_net`, on *Linear* and
  *Logistic*.

The catalogue's `applies` says which keys matter with which values (`se` only with
`penalty = none`; `lambda`, `folds`, `mixing` only with a penalty; the orders only with
`orders = manual`), so the form shows only those and the gateway refuses the others. The
catalogue's `sections` says, **per variant**, which numbers the runner writes (no
`std_error`, `p_value`, `ci_low`, `ci_high` in `coef` when a penalty is on; `cv` rows only
then), and the layout chooses its blocks from the same declaration. A test compares every
runner's output keys with that declaration, variant by variant.

`applies` also has «given / not given» rules: `test` and `pairwise` of Kaplan–Meier apply
only with `strata`; `horizon` only without `exog`. `forbids.when` accepts a list of values.
The gateway refuses a key that does not apply, so the form removes it on save.

### Group `regression` — R

Packages beyond R's base and recommended ones (`stats`, `MASS`, which `rocker/r-ver`
ships, **assumed**): `sandwich`, `lmtest`, `nortest`, `detectseparation`, `brglm2`,
`glmnet`, `jsonlite` (`arrow` is installed in every R image, **verified** in
`sbBasePackages`). Each at a pinned version in the recipe. `car`, `ResourceSelection` and
`pROC` are **not** installed (`car` pulls `lme4` and a long compile): the variance
inflation factors, the Hosmer–Lemeshow test and DeLong's interval of the AUC are a few
lines of the runner each, checked against those packages' values computed outside
Tabularia («Reference values»).

#### `linear` — Linear regression

| | |
|---|---|
| Estimator | `stats::lm`; `estimation = wls`: weighted least squares with `weights`; `penalty` ≠ `none`: `glmnet::cv.glmnet(family = "gaussian")` |
| Parameters, every variant | `outcome` (number), `predictors`, `reference`, `interactions`, `intercept`, `weights` (required with `wls`; case weights with a penalty; ignored with `ols`, with a note: «Open questions for the owner», 6), `alpha`, `seed`, `output`: `results` \| `rows` |
| Parameters, unpenalized | `se`: `classical` (default) \| `hc0` \| `hc1` \| `hc2` \| `hc3` \| `hac` (Newey–West, lag by the package's rule) \| `cluster` (with `cluster`); `order_by` |
| Parameters, penalized | `penalty`: `ridge` (α = 0) \| `lasso` (α = 1) \| `elastic_net` (`mixing` in (0, 1), 0.5, or `mixing = cv`: the grid 0.1, 0.25, 0.5, 0.75, 0.9, 1 on the same folds); `lambda`: `cv_1se` (default) \| `cv_min` \| a fixed value; `folds` (10, 3 to 20); `fold_by` (a column: grouped folds); `measure`: `mse` \| `mae`; `standardize` (true) |
| Variants | OLS; WLS; heteroscedasticity-consistent SE (`sandwich::vcovHC`), HAC (`sandwich::NeweyWest`), clustered (`sandwich::vcovCL`), the tests of the coefficients with the chosen covariance (`lmtest::coeftest`); ridge, lasso, elastic net |
| Coefficients, unpenalized | per term: `estimate`, `std_error`, `statistic` (t), `df`, `p_value`, `significance`, `ci_low`, `ci_high`, `vif` (the VIF of a one-column term; for a categorical term GVIF^(1/df), the figure read against 5 and 10: see «As built, linear») |
| Coefficients, penalized | per term, at the chosen λ, on the original scale: `estimate`, `selected`. **No standard error, p-value or interval**: the report says why in one sentence (inference after selection is out of scope) |
| Fit, unpenalized | `n`, `n_missing`, `df_model`, `df_residual`, `r_squared`, `adj_r_squared`, `sigma`, `f_statistic`, `f_df_1`, `f_df_2`, `f_p_value` (a Wald test with the chosen covariance when `se` is not classical), `log_lik`, `aic`, `bic`, `rmse`, `mae`, `condition_number` (Belsley's) |
| Fit, penalized | `lambda`, `log_lambda`, `lambda_min`, `lambda_1se`, `log_lambda_min`, `log_lambda_1se`, `mixing`, `cv_measure`, `cv_value`, `cv_se`, `n_selected`, `n`, and on the out-of-fold predictions (`keep = TRUE`): `rmse`, `mae`, `r_squared` |
| Rows output | `pred_fitted`, `pred_residual`; unpenalized also `pred_std_residual` (internally studentized), `pred_leverage`, `pred_cooks_distance`, `pred_low`, `pred_high` (prediction interval) |

Assumptions, unpenalized:

| Assumption | Check | Package | Verdict |
|---|---|---|---|
| Linearity, form | Ramsey RESET, powers 2 and 3 of the fitted values | `lmtest::resettest` | p < alpha: «add the missing curvature: a square, a log, an interaction» |
| Homoscedasticity | Breusch–Pagan, studentized (Koenker) | `lmtest::bptest` | p < alpha: «use robust standard errors (se = hc3)»; with robust SE chosen, the row says the inference does not need it |
| Homoscedasticity | White's test (Breusch–Pagan on the fitted values and their squares) | `lmtest::bptest(m, ~ fitted + I(fitted²))` | as above |
| Normality of residuals | Shapiro–Wilk for 3 ≤ n ≤ 5,000; Anderson–Darling above; Jarque–Bera always | `stats::shapiro.test`, `nortest::ad.test`, the runner (JB) | p < alpha: «the intervals rely on n being large; with few rows, transform the outcome» |
| Independence | Durbin–Watson and Breusch–Godfrey (order 1 and 4) — **only with `order_by`**: they depend on the row order; without it the row says so and is not computed (with `se = hac` the row says the inference already allows for the correlation) | `lmtest::dwtest`, `lmtest::bgtest` | p < alpha: «use se = hac, or model the time» |
| Multicollinearity | VIF / GVIF^(1/df) per term; Belsley's condition number | runner (checked against `car::vif`; as built, against a second construction of the same figure) | value > 5 doubtful, > 10 failed (the condition number: > 30 doubtful, > 100 failed): «drop or combine the correlated predictors, or turn on a penalty» |
| Influence | Cook's distance, leverage, Bonferroni outlier test on the studentized residuals; the five largest rows named by row number | `stats::cooks.distance`, `hatvalues`, `rstudent` | **The verdict by the classical thresholds** (`3c3dff03`): Cook's distance above the median of F(p, n − p) doubtful, above 1 failed; leverage above max(3p/n; 0.5) doubtful; the outlier test by its p. The wide thresholds, 4/n for Cook and 2p/n for leverage, which a clean fit of a few hundred rows passes nearly always, only **name the rows to look at**. The check writes the largest value, the threshold of its verdict and how many rows pass it |

Assumptions, penalized: the **choice of λ** (a λ at the edge of the path is a note, «widen
the path: the best λ is its smallest»; every coefficient zero is a note); residuals against
fitted values and the Q–Q of the out-of-fold residuals, read as diagnostics, not as tests;
the categorical predictors' dummies are penalized one by one (said in the report). The
tests of the table above assume an unpenalized fit and are not computed: the report says
so.

Charts, unpenalized: residuals against fitted values with a lowess line and 0; normal Q–Q
of the standardized residuals with its line; scale–location; standardized residuals against
leverage, the largest Cook's distances labelled; histogram of the residuals; observed
against fitted with the diagonal; coefficients with their intervals (`interval`, line at
0). Penalized: the **cross-validation curve** against log λ with its error band and the two
λ marked; the **coefficient paths** against log λ (the 20 terms that enter first, the rest
summed up as «others»); the coefficients at the chosen λ; residuals against fitted values
and Q–Q out of fold.

**As built, linear** (lane M1: `e745afb3`, `7434bf31`; the file is `groups/regression/runner/R/linear.R`):

- **VIF of a categorical term.** The assumption row `vif` holds GVIF in `statistic` and the
  term's df in `df`; its `value`, and the coefficient rows' `vif`, are GVIF^(1/df) (the VIF
  itself for a one-column term), read against 5 and 10. The first version of this spec
  said GVIF^(1/(2·df)); the code does not use it.
- **Condition number.** Belsley's: the ratio of the largest to the smallest singular value of
  the design with each column scaled to unit norm, the intercept included; with `wls` the
  design is √w·X. Doubtful above 30, failed above 100.
- **WLS.** The packages that refit without the weights are not called on a weighted fit. The
  RESET is the F test of the weighted fit with the powers of the fitted values added; the
  tests of independence (Durbin–Watson, Breusch–Godfrey) run on the transformed regression,
  √w·y on √w·X, whose residuals are the weighted ones; the normality checks and the charts
  read the weighted residuals (√w·e, as `plot.lm` does). The assumptions' table says so in a
  note. Rows of weight 0 are left out, with a note.
- **Prediction interval with robust errors.** The interval of `pred_low` and `pred_high` is
  built from the covariance the node chose plus σ² (σ²/weight for a weighted row), at 1 − alpha.
- **Labels.** The three most extreme points of a chart are labelled by their row number,
  as `plot.lm` does.
- **`weights` with `estimation = ols`** are ignored, with a note saying to choose `wls`;
  **`se = hac` without `order_by`** reads the rows in the order they arrived, with a note
  (both are in «Open questions for the owner»).
- **`estimation`** is a parameter of the node but not one of the `model` section's texts of the
  unpenalized variant: the runner does not write it (the penalized variant has `penalty`).
  The `formula`, `family`, `link` and `se_type` texts say enough to redo the fit.
- **Report.** The assumptions' `df` column writes the two degrees of freedom of an F test as
  one text (`2; 111`) in a column 12 wide, and it can wrap in the PDF.
- **Witnesses.** `car` and `statsmodels` were not available when the reference values were
  computed: the VIFs are checked against a second construction (Fox and Monette's formula on
  the regressors' correlation matrix, and 1/(1 − R²) for a one-column term), and no
  `statsmodels` witness file exists yet for the regression group.

#### `logistic` — Logistic regression

| | |
|---|---|
| Estimator | `stats::glm(family = binomial(link))`; `estimation = firth`: `brglm2::brglmFit` (`type = "AS_mean"`, Firth's penalized likelihood for the logit link); `penalty` ≠ `none`: `glmnet::cv.glmnet(family = "binomial")` |
| Parameters, every variant | `outcome` (two values), `event_value`, `predictors`, `reference`, `interactions`, `intercept`, `weights` (frequency), `threshold` (0.5), `alpha`, `seed`, `output`: `results` \| `rows` |
| Parameters, unpenalized | `link`: `logit` (default) \| `probit`; `estimation`: `ml` (default) \| `firth`; `se`: `classical` \| `hc0`…`hc3` \| `cluster`; `ci`: `wald` (default) \| `profile` |
| Parameters, penalized | as for `linear`, with `measure`: `deviance` \| `auc` \| `class`; logit link only |
| Coefficients, unpenalized | `estimate`, `std_error`, `statistic` (z), `p_value`, `significance`, `ci_low`, `ci_high`, `odds_ratio`, `or_ci_low`, `or_ci_high` (logit only), `vif` |
| Coefficients, penalized | `estimate`, `odds_ratio`, `selected`; no inference, as for `linear` |
| Fit | `n`, `events`, `n_missing`, `auc` with DeLong's `auc_ci_low`, `auc_ci_high` (unpenalized; a penalized fit gives `auc` as the mean of its folds' AUCs with `auc_se` and `auc_folds`, no interval), `brier`, at `threshold`: `accuracy`, `sensitivity`, `specificity`, `precision` (out of fold with a penalty); unpenalized also `deviance`, `null_deviance`, `lr_statistic`, `lr_df`, `lr_p_value`, `log_lik`, `aic`, `bic`, `mcfadden_r2`, `nagelkerke_r2`, `tjur_r2`; penalized also the λ rows of `linear` |
| Rows output | `pred_prob`, `pred_class`, `pred_linear`; unpenalized also `pred_deviance_residual`, `pred_pearson_residual`, `pred_leverage`, `pred_cooks_distance` |

Assumptions:

| Assumption | Check | Package | Verdict |
|---|---|---|---|
| No separation | Linear-programming check of complete and quasi-complete separation | `detectseparation::detect_separation` | With `estimation = ml` and separation: **the node stops** («… separates the outcome: use estimation = firth, or a penalty»); with Firth or a penalty a note |
| Calibration (goodness of fit) | Hosmer–Lemeshow, 10 groups of predicted risk | runner (checked against `ResourceSelection::hoslem.test`; as built, against a second construction) | p < alpha: «the predicted probabilities do not match the observed rates»; above 5,000 rows a note that it rejects small departures, read the calibration chart |
| Linearity of the logit | Box–Tidwell: each positive numeric predictor's x·log(x) term added, its Wald p (unpenalized only) | runner, `stats::glm` | p < alpha: «that predictor's effect is not linear in the logit: transform it» |
| Multicollinearity | VIF / GVIF (unpenalized only) | runner | as for linear |
| Enough events | events per variable (EPV) | runner | EPV < 10: note, «few events for the number of terms: Firth or a penalty helps» |
| Influence | Cook's distance, leverage, the largest deviance residuals (unpenalized only) | `stats` | counts and rows |
| The choice of λ | as for `linear` (penalized only) | runner | notes |

Charts: ROC curve with the AUC; calibration (deciles of predicted risk against observed
rate, with the diagonal); distribution of the predicted probabilities by outcome; the
confusion matrix at the threshold (`heatmap`); unpenalized: deviance residuals against the
linear predictor, odds ratios with their intervals (`interval`, logarithmic axis, line at
1); penalized: the cross-validation curve and the coefficient paths, ROC and calibration
out of fold.

**As built, logistic** (lane M2: `b046c407`; `groups/regression/runner/R/logistic.R`):

- **Weights are frequencies.** The column must hold whole numbers, 0 or more (a refusal
  otherwise); `n` and `events` are sums of the weights, and the rates, the AUC, the
  Hosmer–Lemeshow groups and the confusion matrix count the cases the rows stand for. Rows
  of weight 0 are left out, with a note.
- **Robust errors with weights.** `sandwich` counts rows, not weights: the note
  `robust_weights` says that each row is taken as one observation. With Firth the robust
  errors are `sandwich`'s for a `glm`, at Firth's estimates (note `firth_robust`).
- **Firth with `ci = profile`.** `brglm2` has no profile intervals: the intervals are Wald's,
  and the note `firth_profile` says so.
- **Large tables** (`b4cf0059`). The ROC is built in one sort, O(n log n). Above 10,000 rows
  `detectseparation`'s linear program is not run: the separation check is approximate and says
  «doubtful» at most, with the note `note_separation_approximate`. A fit of 100,000 rows takes
  1.9 s. Not tried above that.
- **Separation.** With `estimation = ml` the node stops (`separation`). With Firth, one
  assumption row `separation` per term `detect_separation` names, each doubtful; one «ok» row
  when there is none; «not computed» with its reason when the check fails.
- **Hosmer–Lemeshow.** Groups cut at the deciles of the predicted risk; the degrees of
  freedom are the groups actually made minus 2 (`ResourceSelection::hoslem.test` takes 10 − 2
  even when tied risks make fewer groups); fewer than 3 groups, or too few distinct risks, is
  «not computed» with the reason. The groups also draw the calibration chart.
- **Choices fixed here.** A deviance residual with |r| > 3 is a doubtful row of the
  `deviance_residuals` check; EPV = min(events, non-events) / terms, doubtful below 10;
  Box–Tidwell adds one predictor's x·log(x) at a time (by the node's own estimation, Firth
  included), «not computed» for a predictor with a value at or below 0 or with fewer than
  three distinct values; influence as in `linear` (Cook's distance above the median of F(p, n − p) doubtful and above 1 failed, leverage above max(3p/n; 0.5) doubtful; 4/n and 2p/n only name rows).
- **Notes** are the `note_<id>` rows (ids `firth`, `firth_profile`, `firth_robust`, `probit`,
  `weights`, `robust_weights`, `zero_weights`, `interactions`, `not_converged`), each one fixed
  English sentence; the layout says them in the page's language through the `values` of
  the words file, under the row's `stat`, **keyed by that English sentence** (the penalized
  notes the same): a sentence changed in R loses its translation without a word. The lanes
  ask for a common mechanism in `common.with_labels` that translates a note by its id
  (not decided here).
- **Witnesses.** `pROC`, `ResourceSelection` and `car` were not available: Hosmer–Lemeshow
  and DeLong's interval are checked against a second construction in `make_reference.R`, so
  the 1e-9 comparison with those packages themselves is not done.

**As built, the penalties of both regressions** (lane M3: `2a872962`;
`groups/regression/runner/R/penalized.R`):

- **Folds.** The runner draws them and hands them to `cv.glmnet` as `foldid`: after
  `set.seed(seed)`, `sample(rep(1:K, length.out = n))`; with `fold_by`, the values of that
  column in text order are drawn into the folds the same way, and every row of a value
  falls in its fold (note `grouped_folds`); fewer values than folds is a refusal.
- **A fixed λ** is added to `glmnet`'s own path, so the curve, the coefficients and the
  out-of-fold numbers are read at it exactly. With `mixing = cv` the grid is run on the same
  folds and the mixing with the least error at its best λ (or at the fixed one) is kept
  (notes `mixing_cv`, `mixing_cv_fixed`).
- **New numbers.** `log_lambda_min`, `log_lambda_1se`; for the logistic fit, also
  `deviance` (−2 × the log-likelihood of the out-of-fold probabilities). `threshold`
  and the classification numbers are at the node's threshold.
- **Edges of the path.** Notes `edge_small` (least error at the smallest λ), `edge_large`
  (at the largest: with a ridge the coefficients never all reach zero), `all_zero`,
  `all_zero_1se`, `largest`, `largest_1se`.
- **«(others)»** in the paths is the sum of the |coefficients| of the terms past the 20
  drawn.
- **Minimums, refusals.** 3 rows per fold; at least 2 terms besides the intercept;
  for the logistic, the rarer value on at least as many rows as folds; with `measure = auc`,
  10 rows per fold.
- **`measure = class`.** `glmnet` counts at the threshold 0.5 whatever the node's
  `threshold` is: note `class_threshold` when they differ.
- **Weights** of the linear fit are case weights; the logistic fit's are frequencies, as in
  the unpenalized one.
- **The AUC of a penalized logistic fit** is the mean, weighted by fold, of the AUCs of the folds
  (`auc`), with `auc_se` (the standard error between folds) and `auc_folds` (the folds that
  have one), as `cv.glmnet`'s own `cvm` (`a1b9263d`). The AUC of all the out-of-fold predictions
  together, and DeLong's interval, are gone: the folds' models have different intercepts and their
  predictions are not comparable (0.45 against `cv.glmnet`'s 0.75 with a ridge). Brier, the
  deviance and the classification numbers stay on the pooled out-of-fold predictions; the ROC
  curve is the vertical mean of the folds' curves.
- **Hosmer–Lemeshow** is a row of the penalized variant's assumptions, always «not computed»
  (see «Open questions for the owner»); the calibration chart, out of fold, is the diagnostic.
- **Layout.** `common.page` takes `assumptions_note` (a sentence under the checks: they are
  diagnostics, not tests) and `p_values=False` (no statistic, df or p column).
  In an `xy` chart whose x crosses 0 the y axis is drawn at x = 0 and can be mistaken for the
  λ lines; the title «Fit» can be left alone at the foot of page 1. Both are known layout
  faults of lane E, not of the runner.

### Group `survival` — R

Packages: `survival` (a recommended package, pinned in the recipe all the same), `jsonlite`.
**Planned on 2026-11-20**: `flexsurv` and its closure (34 packages more), for a third node of the
group, `parametric_survival`, whose contract is «The node «Parametric survival»» (after the
boosting group).

#### `kaplan_meier` — Kaplan–Meier

| | |
|---|---|
| Estimator | `survival::survfit`; comparison `survival::survdiff` |
| Parameters | `time` (number ≥ 0), `event` (column) with `event_value` (the rest is censored), `strata` (a column, up to 20 strata), `conf_type`: `log` (R's default) \| `log-log` \| `plain`; `test`: `logrank` (ρ = 0, default) \| `peto` (ρ = 1); `pairwise` (boolean: every pair of strata, Holm-adjusted); `times` (up to 10 times to report survival at); `rmst_tau` (the horizon of the restricted mean); `alpha`; `seed` (default 1; the charts' sampling); `output`: `results` \| `table` (one row per stratum and time: `n_risk`, `n_event`, `n_censor`, `surv`, `lower`, `upper`) |
| Per stratum (`section = stratum`) | `n`, `events`, `censored`, `q25`, `median`, `q75` each with `_ci_low` and `_ci_high` (the quartile rule of `survival`'s `quantile`: where a curve lies exactly on the level over a step, the middle of the step), `surv_at_<t>` with its interval (`surv_at_<t>_low`, `surv_at_<t>_high`; `x0` = the time), `rmst`, `rmst_se`, `observed`, `expected` (the log-rank's, unweighted) |
| Test | The overall test is in `section = model`: `test` (text), `test_statistic` (chi-square), `test_df`, `test_p_value`, `test_significance`; observed and expected events per stratum; pairs in `section = pair` |

Assumptions: **non-informative censoring** cannot be tested from the data, and the report
says so; **proportional hazards**, which the log-rank test needs for its power: the
log-minus-log chart (parallel lines) and a note when the curves cross («the curves cross:
compare with test = peto, or with the restricted mean»); censoring by stratum (the share
censored per stratum, a chi-square of censored against stratum as a descriptive check:
«censoring differs between strata», and the **follow-up by stratum** that tells whether
censoring itself differs, below).

Charts: the survival curves as steps with their bands and the censoring marks; the
**number at risk** under them, as a table at round times; the cumulative hazard; the
log-minus-log chart.

**As built, Kaplan–Meier** (lane M4: `b8521e60`; `groups/survival/runner/R/kaplan_meier.R`):

- **Checks written** (section `assumption`): `censoring_informative` (not computed, the
  share censored as its value), `censoring_by_stratum`, `follow_up_by_stratum`,
  `proportional_hazards` (Grambsch–Therneau by `cox.zph` on the strata, and the crossing of
  curves: each above the other by at least 0.05 while both keep 10 rows at risk) and
  `few_at_risk` (a number reported, a quartile, a median, a survival at a time, the
  restricted mean's horizon, is read where fewer than 10 rows are still at risk: doubtful).
  The chi-square of censored against stratum fires whenever survival differs between strata,
  even with non-informative censoring; it is shown with `follow_up_by_stratum`, the reverse
  Kaplan–Meier (censoring as the event) compared across strata by the log-rank test, which
  is the check that points at censoring that differs.
- **Names.** A stratum is called `column=value`, or `all` without `strata`. `x0` holds
  the time in every `surv_at_<t>` row, so that a page writes it as a number of its language.
- **`peto`.** `test = peto` is `survdiff`'s ρ = 1, which is Fleming–Harrington G(1, 0); the
  observed and expected events written are the unweighted (ρ = 0) ones whatever the test.
- **`rmst_tau`** is, when not given, **the shortest of the strata's longest observed times**
  (`3c3dff03`; it was the longest observed time overall, which asked a restricted mean past
  where one stratum had any data): the RMST values differ from before. The stratum that ends
  at that time is left out of the `few_at_risk` count. Curves that cross: the check fails when
  one curve is above the other by at least 0.05 **and** by 2 standard errors of the
  difference, in both directions, with enough rows at risk. Past a stratum's last time,
  a survival (and its interval) is NULL unless the curve has reached 0.
- **Card.** Per stratum, the curve at its event times and `last_time`; the schema
  (`card.schema.json`) does not declare `time_column`, `conf_type` or `last_time`, which the
  code writes (the schema is open to them). On a large table the curves are **thinned within
  a declared tolerance** (`8d827068`): a step is kept when the survival or an interval end
  has moved by 0.001 or more since the last kept step, so the card differs from the fit by
  less than 0.001 at every time and is exact past the last step. The tolerance is in the card's
  `tolerance` field and the note `note_card_thinned` says so; a card that still passes its
  ceiling is dealt with as for every model («As built, the final review»).
- **Group tests.** The runner's own self-tests (column names that look like code, predicting
  the fitted rows) are registered in `TAB_COMMON_TESTS`, inside `kaplan_meier.R`, the hook
  the self-test runs besides the reference cases.
- **Witness.** `lifelines` 0.30.3 (the witness file pins pandas 2.3.3) compared 94 values,
  largest gap 2.2e-14, in a scratch environment (the lane's run, `make_witness.py`).

#### `cox` — Cox proportional hazards

| | |
|---|---|
| Estimator | `survival::coxph` |
| Parameters | `time`, `event`, `event_value`, optional `start` (counting-process data: start–stop rows, time-varying covariates), `predictors`, `reference`, `interactions`, `strata` (a column: separate baselines), `ties`: `efron` (default) \| `breslow` \| `exact`; `se`: `classical` \| `robust` (with optional `cluster`); `alpha`; `output`: `results` \| `rows` |
| Coefficients | `estimate` (log hazard ratio), `hazard_ratio`, `hr_ci_low`, `hr_ci_high`, `std_error` (and `robust_se`), `statistic` (z), `p_value`, `significance`, `vif` |
| Fit | `n`, `events`, `n_missing`, `lr_statistic`, `wald_statistic`, `score_statistic` with their df and p, `concordance` with `concordance_se`, `log_lik`, `aic` |
| Rows output | `pred_linear`, `pred_risk` (exp of the centred linear predictor), `pred_martingale`, `pred_deviance` |

Assumptions:

| Assumption | Check | Package | Verdict |
|---|---|---|---|
| Proportional hazards | Scaled Schoenfeld residuals, per term and global (`transform = "km"`) | `survival::cox.zph` | p < alpha: «the effect of … changes over time: stratify by it, or let it interact with time» |
| Linear form of numeric covariates | Martingale residuals of the null model against each numeric covariate, with a lowess line, and a likelihood-ratio test of a natural spline of the covariate (3 df, `splines::ns`) added to the model, so that the check has a verdict on the first page | `residuals(type = "martingale")`, `stats::lowess`, `splines::ns` | p < alpha: «the effect is not linear on the log hazard: read its chart; a log, a square or a spline of it» |
| Influence | dfbetas (the change in a coefficient in units of its standard error) per term, the five largest named | `residuals(type = "dfbetas")` | any |dfbetas| above 1 **failed** (the classical threshold; 2/√n only names the rows to look at, `3c3dff03`); not computed with `ties = exact` (`survival` gives none) |
| Multicollinearity | VIF on the design | runner | as for linear |
| Enough events | events per variable | runner | EPV < 10: note |

Charts: hazard ratios with their intervals (logarithmic axis, line at 1); the scaled
Schoenfeld residuals of each term against time with a smooth (natural spline, 4 df,
`splines::ns`) and its band, up to 12 terms; martingale residuals against each numeric
covariate; deviance residuals against the linear predictor; adjusted survival curves per
stratum at the covariates' means (`survfit` of the fit, whose chart title says «at the predictors' means»: `survival` 3.x centres a 0/1 column at 0, and with interactions the curve says little).

**As built, Cox** (lane M5: `570b1ef3`; `groups/survival/runner/R/cox.R`):

- **Wald and robust.** The Wald statistic is `fit$wald.test`, not the rounded figure of
  `summary`; with robust errors the intervals, the z tests and the Wald test use them, the
  likelihood-ratio and score tests do not (they assume independent rows: a note). Robust
  score tests (`rscore`) are not in the catalogue. With start–stop rows and no `cluster`, a
  note says each row is then taken as independent.
- **`n`** is the number of rows of the fit, so with start–stop rows it counts intervals, not
  subjects; `events` counts the events.
- **Monotone likelihood.** When `coxph` warns that a coefficient may be infinite, the node
  writes a note and goes on (it does not stop). Whether it should stop is in «Open
  questions for the owner».
- **Risk centring.** `pred_risk`, and the `prediction` of a Predict node, are exp of the
  linear predictor centred on `fit$means` (`predict(type = "risk", reference = "sample")`);
  R's own default centres per stratum, so the two differ with strata. The self-test
  checks the card against `reference = "sample"`.
- **Card.** `cox`: `centring`, `ties`, `time_column`, `start_column`, `strata`, the baseline
  cumulative hazard per stratum at its event times (at most the catalogue's
  `max_baseline_times`) and, per stratum, `last_time`; the strata's column is in `inputs` with
  its levels. The schema (`card.schema.json`) declares only `centring`, `strata` and
  `baseline`; the others are to be added.
- **Run record.** `run.json`'s `software` lists only R and `stats`, because the common
  library asks for the versions without naming packages; the `model` section's `software` text
  does name them.
- **Witness.** `lifelines` agrees at 1e-6 for Efron's ties only (it knows no other), and not
  for the robust errors with tied times (R's score residuals carry Efron's correction,
  `lifelines`' do not) nor for Schoenfeld (an older approximation of `cox.zph`); on start–stop
  rows the classical errors are compared.

### Group `timeseries` — R

Packages: `forecast` (`Arima`, `auto.arima`, `forecast`, `ndiffs`, `nsdiffs`), `tseries`
(`adf.test`, `kpss.test`, `jarque.bera.test`), `urca` (`ur.kpss`, which `ndiffs` uses),
`FinTS` (`ArchTest`, the ARCH LM test), `jsonlite`; `stats` for `Box.test`, `acf`, `pacf`.
Pinned in the recipe. **Assumed**: `forecast` brings `urca`, `fracdiff`, `lmtest`,
`timeDate`, `ggplot2` among its imports (a longer build, once); `FinTS` is a small pure-R
package. Checked on the build's lock by the piece; if `FinTS` is not installable, the ARCH
test is the runner's (Engle's LM regression, a few lines) checked against it outside.

#### `arima` — ARIMA, one node from AR to SARIMAX

One node, one id, one estimator: `forecast::Arima` (regression with ARIMA errors, maximum
likelihood, R's `stats::arima` underneath), forecasts by `forecast::forecast`. AR, MA, ARMA,
ARIMA, SARIMA and SARIMAX are what the orders and the exogenous columns make of it; the
palette may show six entries that are **presets of this node** (each drops it with the
orders it leaves free: AR with q = d = 0, MA with p = d = 0, ARMA with d = 0, ARIMA without
the seasonal part, SARIMA without exogenous columns, SARIMAX with all), the node's title
says which model the current orders make («SARIMA(1,1,1)(0,1,1)[12]»; with exogenous
columns and no season, the name is «ARIMAX(p,d,q)»; with a season, «SARIMAX(p,d,q)(P,D,Q)[s]»), and the type
selector does not exist: changing an order is enough.

| | |
|---|---|
| Parameters | `time` (date, datetime or integer), `value` (number), `frequency`: `hour` \| `day` \| `week` \| `month` \| `quarter` \| `year` \| `index` (integer steps); `s` (the seasonal period, 1–60; 1 = none; defaults from `frequency`: 24, 7, 52, 12, 4, 1); `orders`: `manual` (default) \| `auto`; with `manual`: `p`, `q` (0–5), `d` (0–2), `P`, `Q` (0–2), `D` (0–1); with `auto`: `criterion`: `aicc` (default) \| `aic` \| `bic`, `stepwise` (true), `max_p`, `max_q` (5), `max_P`, `max_Q` (2), `max_d` (2), `max_D` (1), `unit_root_test`: `kpss` (default) \| `adf` \| `pp`, `seasonal_test`: `seas` (default) \| `ocsb`; `drift`/`mean` (as `Arima`'s `include.drift`, `include.mean`, where differencing allows; with `orders = auto` they are `auto.arima`'s `allowdrift` and `allowmean`: the search tries a constant only where they are true and the differences let it; defaults `drift = false`, `mean = true`); `exog` (columns); `transform`: `none` \| `log` \| `box_cox` (λ estimated by `forecast::BoxCox.lambda`, or given); `horizon` (1–1,000; with `exog`, the horizon is the rows after the last observed value whose `exog` are present and whose `value` is empty); `holdout` (the last k observations kept out to measure the forecast); `alpha` (bands at 80% and at 1 − alpha); `output`: `results` \| `forecast` |
| Series rules | One row per period: duplicates of `time` stop the node («aggregate first with Group by»); a missing period is a missing value, which the likelihood handles; the rows are sorted by `time` before the input is written |
| Automatic orders | The search of `forecast::auto.arima` (Hyndman–Khandakar): `D` by the seasonal test, then `d` by successive unit-root tests (`nsdiffs`, `ndiffs`, at alpha 0.05), then a stepwise search over p, q, P, Q minimizing the criterion; the runner calls `auto.arima` **once**, with its helpers `myarima` (the fit of a candidate), `ndiffs` and `nsdiffs` wrapped to record what they did, so that the candidates are exactly the models `auto.arima` fitted, in its order, with its criterion, and the chosen model is its own (the self-test compares it with `auto.arima` called directly); the unit-root and seasonal tests' statistics are computed again by the same functions with the same arguments. Every candidate's orders, criterion and convergence are rows (`section = candidate`) |
| Coefficients | per parameter (`ar1`, `sma1`, `drift`, the exogenous ones): `estimate`, `std_error`, `statistic` (z), `p_value`, `significance`, `ci_low`, `ci_high` |
| Fit | `n`, `n_missing`, `p`, `d`, `q`, `P`, `D`, `Q`, `s`, `log_lik`, `aic`, `aicc`, `bic`, `sigma2`, in-sample `rmse`, `mae`, `mape`, `mase`, and on `holdout`: `holdout_rmse`, `holdout_mae`, `holdout_mape`, `holdout_mase`; the moduli of the AR and MA roots (from `polyroot`, all outside the unit circle) |
| Choice of the orders (`orders = auto`) | `section = candidate`, `level` = the model («ARIMA(2,1,1)(0,1,1)[12]»): `aicc`, `aic`, `bic`, `converged`, `chosen`; and in `section = model` the texts `orders_reason` (one sentence per step: «D = 1: the seasonal strength 0.82 is above 0.64», «d = 1: KPSS rejects stationarity of the series (p 0.01) and not of its first difference (p 0.10)», «ARIMA(2,1,1)(0,1,1)[12] has the lowest AICc of 14 candidates»), with the tests' statistics and p-values as numbers |
| Forecast output | `time`, `observed`, `fitted`, `forecast`, `lower_80`, `upper_80`, `lower`, `upper` (1 − alpha), `is_forecast` |

Assumptions:

| Assumption | Check | Package | Verdict |
|---|---|---|---|
| Stationarity of the differenced series | ADF (H0: unit root) and KPSS (H0: level-stationary, or trend-stationary with drift), and the four-case table of the two | `tseries::adf.test`, `tseries::kpss.test` | ADF does not reject and KPSS rejects: «difference once more (d + 1), or let orders = auto choose»; both reject: «a trend or a break: look at the series» |
| Stationarity and invertibility of the fit | The roots of the fitted AR and MA polynomials | `polyroot` on the coefficients | a root of modulus up to 1 failed, below 1.05 doubtful: «an order too many, or a difference missing» |
| Residuals are white noise | Ljung–Box at lags 10 and 2·s, `fitdf` = p + q + P + Q | `stats::Box.test(type = "Ljung-Box")` | p < alpha: «structure left in the residuals: raise an order, or try orders = auto» |
| Constant variance of residuals | ARCH LM (s lags when s > 1, else 12; not computed below 3 × lags + 10 residuals); the variance of the first third against the last third (an F ratio) | `FinTS::ArchTest`; the runner | p < alpha: «the variance changes: try transform = log or box_cox» |
| Normality of residuals | Jarque–Bera | `tseries::jarque.bera.test` | p < alpha: «the bands assume normal errors: read them with care» |

Charts: the series with the fitted values and the forecast with its two bands; ACF and
PACF of the differenced series (up to 40 lags, with ±1.96/√n); ACF and PACF of the
residuals; the residuals over time; histogram and normal Q–Q of the residuals; with
`orders = auto`, the table of the candidates sorted by the criterion, the chosen one
marked, and the sentences of `orders_reason` above it.

**As built, ARIMA** (lanes M6 and M7: `fcd6668c`, `646547b9`, `8a614dde`, `e10b530c`; files
`groups/timeseries/runner/R/arima.R` and `arima_auto.R`):

- **Minimum length.** The series needs 2·s + p + q + d + 10 observations, where `s` counts
  only when the model has a seasonal part (P + D + Q > 0): a weekly AR(1) of 60 points is
  fitted. Seasonal orders with `s = 1` are refused.
- **Stationarity checks.** `adf`, `kpss` and the four-case `stationarity` row; a p-value at an
  end of the table the tests interpolate (0.01 and 0.1) is written «≤ 0.01» or «≥ 0.1», not as if
  it were exact (`b7da2b3b`); the KPSS tests
  trend stationarity only with a drift and d + D = 0, level stationarity otherwise.
  Both tests «not computed» on a differenced series that is too short. Ljung–Box,
  ARCH and Jarque–Bera as the table says; the `roots` check is «not computed» without an
  AR or MA order.
- **Advice by case.** The advice of the stationarity check differs by case (ADF keeps a unit
  root and KPSS rejects; both reject; neither rejects). With `orders = auto` the layout swaps
  the advice that would send the reader to `orders = auto` (`layouts/arima.py`,
  `_auto_advice`); the lane proposed `advice_<verdict>` in `common.with_labels` instead.
- **Plots by `time_kind`.** The runner writes `time_kind` (`date`, `datetime` or
  `integer`); the layout draws the two variants of each time chart and shows the one the
  `time_kind` says. Words that depend on the variant are a computed field in `layouts/arima.py`,
  not in `common.with_labels`.
- **Card.** `arima.orders`, `frequency`, `coefficients`, `sigma2`, `transform`, the Box–Cox
  λ, `drift`, `mean`, `exog`, `horizon` and `last` (the last 5,000 observations with their
  times and exogenous values, for a forecast from the card alone); the exact copy of the
  coefficients that `fcd6668c` kept beside them was dropped by `8a614dde`, because
  `tab_json` now writes 17 significant digits.
- **Automatic orders, the rows.** The name of a candidate carries « with drift» or « with
  mean» when it has one, and the seasonal part only when P + D + Q > 0. `orders_reason_<k>`
  does **not** hold a sentence: its text is a **key** (`D_seas_yes`, `d_diff_0`, `search_stepwise`…:
  the list is `ARIMA_AUTO_KEYS`), which the report says in its language with the numbers of
  the step, written as `order_test_*` rows (`order_test_seasonal_strength`,
  `order_test_ocsb_statistic`, `order_test_kpss_d0_statistic`, `…_d0_p_value`…). The tests'
  p-values are read from `urca`'s critical-value tables by interpolation, so they stay
  between 0.01 and 0.10, and such an `order_test_*_p_value` has no `_significance` row
  (the one exception to «every `_p_value` has its `_significance`»).
- **Approximation.** With more than 150 values or a season above 12, `auto.arima` compares
  candidates by an approximation (CSS); the keys `search_*_approx` say so. A candidate with no
  criterion (empty `aicc`, `aic`, `bic`) is one `auto.arima` left out: the fit failed, a root is
  within 1.01 of the unit circle, or a standard error is not a number.
- **Dependence on `forecast`'s internals.** Wrapping `myarima`, `ndiffs` and `nsdiffs` relies on
  names inside `forecast`: the version is pinned in the recipe (`forecast@9.0.2`), the runner
  stops if `myarima`'s arguments change, and the self-test notices the difference.
- **Time.** The seasonal automatic case takes about 12 s (the lane's measurement).
- **Witness.** `statsmodels` agrees with `forecast::Arima` at 1e-4 on every case but a
  SARIMA with D = 1, where the gap is 6e-4, about 0.002 standard errors (see «Open
  questions for the owner»).

### Group `boosting` — Python

**Planned on 2026-11-20, built the same day by lots X0–X7 («As built, the boosting group» below).** The owner's request of 2026-11-20: «mettiamo anche xgboost
e catboost, mettiamo anche la feature importance nel report». This section is the contract the
lots of «The boosting group's lots» (under «Pieces») build against; everything the rest of this
spec says holds for this group too (Kubernetes only, a managed environment enabled by an
administrator, the ten columns, charts drawn by the report engine, Run with a button, MLflow
logged by the Go gateway, the self-test before a version is ready) unless this section says
otherwise.

Words, besides **verified** and **assumed** (top of this file): **checked online** = read on
2026-11-20 on the library's official documentation or on PyPI, for the version named; like an
assumed fact, it is run in the image by the lot that first depends on it, and the spec is
corrected where it differs. The documentation read: XGBoost's «stable» pages (they describe
3.4: the categorical tutorial cites a feature «added in version 3.4.0»), CatBoost's current
pages at catboost.ai (they carry no version; PyPI's latest is 1.2.10, 2026-02-18), the PyPI
JSON API of every package of the recipe. The closure of the recipe was resolved on 2026-11-20
with `uv pip compile --only-binary :all:` for Python 3.13 and 3.14, `x86_64-manylinux_2_28`
and `aarch64-manylinux_2_28`: the same 20 packages in the four cases.

#### The owner's decisions (2026-11-20)

| Question | Decision | By |
|---|---|---|
| Language | **Python**, a group of its own, `boosting` («R for the classical statistical models, Python for the more complex ones», the owner's rule from the start) | Owner |
| Nodes | **One node per model**: *XGBoost* (`xgboost`) and *CatBoost* (`catboost`) | Owner |
| Tasks | **Regression, binary classification, multiclass classification**, chosen in the node (`task`); the report changes with it: RMSE, MAE and R², or AUC, log-loss and the confusion matrix | Owner |
| Importance | **Gain** (the model's native importance) and **mean \|SHAP\|** per predictor, with their bar charts and the **SHAP summary (beeswarm)** of the first predictors. SHAP is computed by the libraries themselves (TreeSHAP, exact on trees): no `shap` package in the image | Owner |
| Hyperparameters | Given values with sensible defaults, editable in the form (depth, learning rate, maximum number of trees…); **early stopping** on a validation part; the report shows the **learning curves**, training and validation | Owner |
| «Assumptions» | For these models they are **diagnostics** (overfitting, early stopping at the maximum, class imbalance, calibration, stability of the importance, predictors never used, evident leakage), each with its verdict and advice; the report says they are **not statistical tests** | Owner |
| The saved model | A format that loads **without running code**: XGBoost's JSON, CatBoost's `cbm`; a separate artifact whose sha256 the card records, read by Predict with the card's integrity check. Never pickle | Owner (the format), spec (the details) |

#### The runner in Python

The protocol is the R runners' one, unchanged: the same `task.json` (`protocol` 1), the same
environment, the same files written (`results.parquet`, `output.parquet`, `artifacts/model.json`,
`run.json`, `error.json`), the same exit codes (3 a refusal, 4 a failed self-test, anything else a
crash), the same log lines (`TABULARIA_ERROR {json}`, `TABULARIA_SELFTEST {status} {name}: …`,
`TABULARIA_SELFTEST_REPORT {json}` last), the same `reference/expected.json` format (cases,
`expect` with `key`, `value` or `text`, `rel` default 1e-9 and `abs` default 1e-12, `refuses`).
So `modelgroups.FitTask`, `PredictTask`, `CodeFiles`, `ErrorFromLog`, `SelftestFromLog`, the
check Job of lot B and the recheck of a changed runner treat a Python group as an R one; only the
command, the check script and the Dockerfile differ.

Files (a module's name never equals an installed package's: `import xgboost` inside
`boosting_xgboost.py` must reach the library, so no file is called `xgboost.py` or
`catboost.py`):

```text
gateway-go/internal/modelgroups/
  common/py/                     the Python library every Python runner imports (flat modules)
    tab_protocol.py              task.json, refusals, JSON, s3:// or path I/O, artifacts, error.json
    tab_columns.py               kinds, labels (catalog.label_of), the input, typed CSVs, renaming, rows left out
    tab_results.py               the ten columns, finite numbers, points, the catalogue's check
    tab_card.py                  the card, its ceiling, MLmodel, software versions
    tab_charts.py                caps on points, thinning of curves
    tab_selftest.py              COMMON_TESTS, the reference cases, the report line
    tab_main.py                  main(group, routes): the dispatcher, run.json, exit codes
  groups/boosting/
    recipe.json
    runner/run.py                the dispatcher: sys.path, then tab_main.main("boosting", ROUTES)
    runner/py/boosting_shared.py preparing, splitting, metrics, diagnostics, importance, points, card
    runner/py/boosting_xgboost.py   fit_xgboost(task), predict_xgboost(task, card, model_path)
    runner/py/boosting_catboost.py  fit_catboost(task), predict_catboost(task, card, model_path)
    reference/data/*.csv, expected.json, make_reference.py, make_witness.py, witness.json
```

- **No file of the runner or of the library may start with `_` or `.`**: `//go:embed common
  groups` silently leaves such files out (no `__init__.py`: the modules are flat and imported by
  path). A Go test lists every `.py` under the group's and the library's folders in the source
  tree and asserts that `CodeFiles` holds each one.
- **The command**: `python -I -B /tabularia/code/runner/run.py` (`--selftest` for the self-test).
  `-I` ignores `PYTHONPATH`, the user's site and the script's folder on `sys.path`; `run.py`
  then puts exactly `/tabularia/code/common/py` and `/tabularia/code/runner/py` first on
  `sys.path`. `-B` writes no bytecode (the code is a read-only ConfigMap).
- **One for one with the R library**: each `tab_*` function of `common/R` that a runner calls
  has its Python twin with the same behaviour and the same English sentences: `tab_refuse` →
  `Refusal(code, message, params)` (an exception class), `tab_json` → `json.dumps(…,
  allow_nan=False)` after non-finite numbers became `None` (Python's `repr` of a float is
  already the shortest text that reads back as the same double: the 17-digit rule holds by
  construction), `tab_label` (true/false, dates as `2026-11-19`, numbers as Python's `repr`, which is the
  rule the R twin imitates), text order by code point, `tab_finite` (non-finite → NULL,
  −0 → 0, below the smallest normal double → 0), `tab_add` and its keys, `tab_check_declared`
  reading `catalog.json`'s `section_shapes` and the variant's declaration, `tab_card_fits` and
  `note_card_too_large`, `tab_mlmodel`, `tab_cap`, the self-test's report. Reading and writing
  `s3://` uses `pyarrow.fs.FileSystem.from_uri` and `pyarrow.parquet`, the same Arrow C++
  (26.0.1) as the R images' `arrow@26.0.1`, which already reads the variables the Job sets
  (`AWS_REGION`, `AWS_ENDPOINT_URL`, the egress proxy's): **assumed** that pyarrow reads them
  as R's arrow does, verified by X8 on kind's MinIO.
- **The results' Parquet types**: `section`, `level`, `level_2`, `stat`, `text`, `key` utf8;
  `value`, `x0`, `x1` float64; `ord` int64, as `tab_results_schema`.
- **Randomness**: `numpy.random.Generator(PCG64(seed))`, one generator per run, created from the
  node's `seed`, consumed in a fixed order (the split, then the SHAP sample, then the chart
  samples). The Job also sets `PYTHONHASHSEED=0` (no code relies on set or dict order of
  strings, but nothing is left to it).
- **Warnings** of the libraries are caught (`warnings.catch_warnings(record=True)`) and written
  to `run.json`, as R's are.
- **COMMON_TESTS**: the library's own tests (the 20 rows of `common/selftest/table.csv`, whose
  column names look like code, come back as labels; the ten columns and their types; a refusal
  writes `error.json` and exits 3) plus a dict a model module registers itself into, as
  `TAB_COMMON_TESTS` in R.

**What changes in the Go gateway** (lot X0):

| Where | Change |
|---|---|
| `internal/modelgroups/groups.go` | `RunCommand` and `SelftestCommand` become `Commands(language) (run, selftest []string)`: R as today, Python `["python", "-I", "-B", CodeDir + "/runner/run.py"]` (and `--selftest`). `load` accepts `python` besides `r`, and checks that the group's entry file exists (`runner/run.R` or `runner/run.py`). `CodeFiles` takes `common/R` for an R group and `common/py` for a Python group, plus `common/selftest/*` for both (today it takes all of `common`): **the R groups' `RunnerHash` changes once**, which makes their versions in use run their self-test again on the same image (no build). `EnvModelFileIn = "TABULARIA_MODEL_FILE_IN"` (Predict: the model file) |
| `internal/scriptimage/scriptimage.go` | `Settings` gains `Managed bool`. A managed Python image installs **only the recipe's packages** (not `BasePackages["python"]`: no `polars`, no `marimo`), with `pip install --no-deps --only-binary=:all: -r /opt/tabularia/requirements.txt && pip check` (the recipe is the whole closure: a package missing from it fails the build at `pip check`, and nothing compiles). R images, and the Python images of scripting environments, are byte for byte as before (a test compares them with today's output) |
| `internal/api/jobs_script_builds.go` | `dockerfile` passes `Managed: env.ManagedBy != nil` |
| `internal/api/models_build.go` | `mgCheckScript` per language: for Python, `python -I -c "$TABULARIA_PY_MANAGED_CHECK"` (prints the lock, one `name==version` per installed distribution, and fails unless `pyarrow.fs.S3FileSystem` imports), the mark, then `python -I -B …/runner/run.py --selftest`; the scripting environments' `sbPyCheck` is not used (it imports `polars`) |
| `internal/api/jobs_models.go`, `jobs_predict.go` | The command from `Commands(group language)`; `PYTHONHASHSEED=0` for a Python group; `modelStepKeys` and Predict's written keys gain `artifacts/xgboost.json`, `artifacts/catboost.cbm` and `<step>/model_file`; `task.limits.time_budget_seconds` = the step's timeout; the check of `max_cells` before the Job («Performance and limits») |
| `cmd/modelgroup` | `dockerfile boosting`, `code boosting <dir>` as for R; the doc comment gives the Python command |

The base image is `python:3.14-slim` (`"base": "3.14"`, one of `SCRIPTS__BASE_IMAGES`' Python
versions, **verified** in `scriptcfg.go`). 3.14 rather than 3.13: every wheel of the closure
exists for both (checked online), and 3.14 is supported a year longer.

#### The recipe

`groups/boosting/recipe.json`:

```json
{ "language": "python", "base": "3.14", "pinned_on": "2026-11-20",
  "packages": ["catboost==1.2.10", "contourpy==1.4.0", "cycler==0.12.1", "fonttools==4.68.0",
               "graphviz==0.21", "kiwisolver==1.5.1", "matplotlib==3.11.2", "narwhals==2.29.0",
               "numpy==2.5.5", "packaging==26.3", "pandas==3.1.1", "pillow==13.0.0", "plotly==7.1.0",
               "pyarrow==26.0.1", "pyparsing==3.3.3", "python-dateutil==4.0.0", "scipy==1.18.1",
               "six==1.17.0", "tzdata==2026.6", "xgboost-cpu==3.4.1"],
  "resources": { "cpu": "2", "memory": "4Gi" }, "max_rows": 1000000, "max_seconds": 3600 }
```

| Package | Version | Licence (PyPI, checked online) | Why |
|---|---|---|---|
| `xgboost-cpu` | 3.4.1 | Apache-2.0 | XGBoost **without GPU**: the `xgboost` wheel for Linux requires `nvidia-nccl-cu13` (hundreds of MB, GPU only); `xgboost-cpu` is the same library built for CPU, 5.8 MB, needing only `numpy` and `scipy`. Requires Python ≥ 3.12 |
| `catboost` | 1.2.10 | Apache-2.0 | CatBoost; wheels for CPython 3.8–3.14 on `manylinux2014` x86_64 and aarch64 (97 MB). It requires `graphviz`, `matplotlib`, `plotly`, `pandas`, `scipy`, `six`, `numpy`: they are installed and **never imported** by the runner (CatBoost imports them only to draw) |
| `numpy` | 2.5.5 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | arrays, every hand-written metric |
| `pandas` | 3.1.1 | BSD-3-Clause | the frame both libraries read (`category` dtype for XGBoost's categorical predictors, `Pool` for CatBoost) |
| `pyarrow` | 26.0.1 | Apache-2.0 | Parquet and `s3://` |
| `scipy` | 1.18.1 | BSD-3-Clause | required by both libraries |
| `matplotlib` 3.11.2, `contourpy` 1.4.0, `cycler` 0.12.1, `fonttools` 4.68.0, `kiwisolver` 1.5.1, `pillow` 13.0.0, `pyparsing` 3.3.3 | | Matplotlib licence (PSF-based), BSD-3-Clause, BSD, MIT, BSD, MIT-CMU, MIT | `catboost`'s requirement and its closure |
| `plotly` 7.1.0, `narwhals` 2.29.0, `graphviz` 0.21 | | MIT, MIT, MIT | `catboost`'s requirement and its closure |
| `packaging` 26.3, `python-dateutil` 4.0.0, `six` 1.17.0, `tzdata` 2026.6 | | Apache-2.0 OR BSD-2-Clause, BSD-3-Clause, MIT, Apache-2.0 | `pandas`' and `matplotlib`'s closure |

Every licence is permissive and compatible with Tabularia's. **Not installed**: `scikit-learn`
(the metrics are written by hand with numpy, a few lines each, and checked against
scikit-learn 1.9.1, BSD-3-Clause, as a witness outside the image), `shap` (the libraries compute
SHAP; `shap` 0.54.1, MIT, is a witness outside the image), `polars`, `marimo`, `mlflow`, any GPU
package. Packages from PyPI only (or the mirror `SCRIPTS__PYPI_INDEX_URL` names), wheels only.
The image weighs about 1 GB (`catboost` alone is 97 MB compressed): nothing compiles, so the
build is a download (minutes, not the R groups' half hour), still a heavy job on this machine.

#### The two nodes

Both nodes take the same keys, but for their own hyperparameters. `task` comes first in the form;
it is the variant (`variants` keyed on `task`: `regression`, `binary`, `multiclass`).

| Key | Kind | Default | Applies | Meaning |
|---|---|---|---|---|
| `task` | choice `regression` \| `binary` \| `multiclass` | — (needed) | always | What is predicted |
| `outcome` | column | — (needed) | always | regression: a number column; binary: exactly two distinct values; multiclass: 3 to `max_classes` (20) distinct values (text, boolean or integer) |
| `event_value` | level of `outcome` | — | `task = binary` (required then) | The class whose probability is predicted (the 1 of the log-loss and of the AUC) |
| `predictors` | predictors | — (needed) | always | Numbers, texts and booleans; a date is refused with the regressions' sentence; at most `max_predictors` (200) |
| `weights` | number column | none | always | Case weights (≥ 0; rows of weight 0 left out with a note), in the fit **and** in every metric |
| `class_weight` | choice `none` \| `balanced` | `none` | `task` ≠ `regression` | `balanced` multiplies each row's weight by n / (k · n_c) (weighted counts, training part): in the fit only, never in the metrics; the probabilities are then no longer calibrated (the diagnostic says so) |
| `threshold` | proportion | 0.5 | `task = binary` | The class predicted, the confusion matrix, accuracy, sensitivity, specificity, precision, F1 |
| `validation` | choice `random` \| `time` | `random` | always | How the validation part is drawn («The validation part») |
| `validation_share` | number 0.05–0.5 | 0.2 | always | Its share of the rows |
| `order_by` | column: number, integer, date or datetime | none | `validation = time` (required then) | The order of time |
| `split_by` | column | none | `validation = random` | Rows of one value of this column fall in the same part (a customer's rows) |
| `stratify` | boolean | true | `validation = random`, `task` ≠ `regression`, no `split_by` | Each class keeps its share in both parts |
| `early_stopping` | boolean | true | always | Stop when the validation metric has not improved for `early_stopping_rounds` trees, keep the trees up to the best |
| `early_stopping_rounds` | integer 5–1,000 | 50 | `early_stopping = true` | |
| `stop_metric` | choice `loss` \| `mae` \| `auc` \| `error` | `loss` | always | The validation metric early stopping watches: `loss` = RMSE, log-loss or multiclass log-loss by task; `mae` regression only, `auc` binary only, `error` (1 − accuracy) classification only (`forbids` rules with lists in `when`) |
| `max_trees` | integer 10–5,000 | 1,000 | always | Boosting rounds at most (a multiclass round grows one tree per class) |
| `learning_rate` | number in (0, 1] | 0.05 | always | Shrinkage of each tree |
| `max_depth` | integer: XGBoost 1–12, CatBoost 1–10 | 6 | always | XGBoost `max_depth`; CatBoost `depth` (symmetric trees: 2^depth leaves) |
| `subsample` | number in (0, 1] | 0.8 | always | Share of rows each tree sees (CatBoost: `bootstrap_type = "Bernoulli"` for every task, so the key means the same everywhere) |
| `seed` | integer | 1 | always | The split, the libraries' randomness, the samples |
| `output` | choice `results` \| `rows` | `results` | always | `rows`: the input's rows with the model's columns |

XGBoost only: `min_child_weight` (number 0–1,000, 1), `colsample_bytree` (number in (0, 1], 0.8),
`reg_lambda` (number 0–1,000, 1), `reg_alpha` (number 0–1,000, 0), `gamma` (number 0–1,000, 0).
CatBoost only: `l2_leaf_reg` (number 0–1,000, 3), `rsm` (number in (0, 1], 1), `random_strength`
(number 0–100, 1). `disjoint`: `outcome`, `predictors`, `weights`, `order_by`, `split_by`.
New kinds in the catalogue (plain data, no new control): `task_boosting`, `class_weight`,
`validation`, `validation_share`, `order_column` (`column` with `types` number, integer, date,
datetime), `stop_metric`, `max_trees`, `rounds`, `learning_rate`, `depth_xgboost`,
`depth_catboost`, `nonnegative`, `fraction`, `strength`.

The defaults differ from the libraries' own on purpose (XGBoost's learning rate is 0.3 and its
`subsample` 1; CatBoost picks a learning rate by itself when none is given): with early stopping
on, a small learning rate and some row and column sampling are the safe start, and an explicit
value is what makes a fit reproducible from its parameters. The report writes every value used.

**Rows and types.**

- **Rows left out**: a row whose outcome, weight, `order_by` or `split_by` is NULL or NaN (the
  count is `n_missing`). **Missing predictors are kept** (unlike the regressions): both libraries
  handle a missing value natively, XGBoost by learning a default direction at each split,
  CatBoost with `nan_mode = "Min"` (its default, checked online) for numbers. The count of
  missing values per predictor is written (`importance|<predictor>||missing_share`). An infinite
  value stops the node and names the column (the common rule).
- **Categorical predictors** (text and boolean, labels by `catalog.label_of`, levels in text
  order; at most `max_levels_trees`, 1,000, per predictor, «is it an identifier?» beyond):
  - **XGBoost: native categories**, not one-hot: `enable_categorical = True`, `tree_method =
    "hist"`, a pandas `category` dtype whose categories are the card's levels in their order,
    `max_cat_to_onehot = 4` and `max_cat_threshold = 64` written explicitly. Why: one value of
    gain and of SHAP per predictor (no sum over dummies), a missing or unseen level follows the
    missing-value path, the model file holds no expansion. The runner builds the codes itself
    from the card's levels at fit and at predict, so XGBoost's own re-coding (since 3.1, checked
    online) sees the same categories both times. **Assumed** (the tutorial shows
    `pred_interactions`, not `pred_contribs`): `pred_contribs` works on a model with categorical
    splits; X2 checks it on the first day, and if it does not, the fallback is one-hot with the
    contributions summed per predictor, written here before going on.
  - **CatBoost: native** (`cat_features` = the positions of the categorical predictors; ordered
    target statistics). A categorical value must be a string: a missing one becomes the sentinel
    `"␟missing"` (U+241F, then `missing`), refused as a level if a column holds it.
  - Columns become positional names `v1`, `v2`… before either library sees them (XGBoost also
    refuses feature names holding `[`, `]` or `<`); the card maps them back.
- **Outcome**: regression float64; classification: class index 0…k − 1 in the text order of
  the labels (binary: 1 = `event_value`).

**The validation part.** Always present (metrics, early stopping and diagnostics read it).

1. `random`, no `split_by`: rows shuffled with the generator; classification with `stratify`:
   per class (in label order) its rows shuffled, round(share × n_c) to validation (at least one
   when the class has two rows or more); otherwise the first round(share × n) of the shuffled
   rows.
2. `random` with `split_by`: the column's values (labels in text order) shuffled; whole values
   go to validation, in that order, until the validation part holds at least share × n rows
   (note `grouped_split`).
3. `time`: rows sorted by `order_by` (stable: ties keep the input's order); the last share × n
   rows are validation, extended back so that rows with the same `order_by` value as the first
   validation row are all in it (no time falls on both sides; note `time_split`). CatBoost then
   gets the training rows in time order with `has_time = true` (checked online: it keeps the
   given order instead of random permutations), so its target statistics never read later rows.
4. **Refusals**: fewer than 20 training rows or 10 validation rows; binary with one class
   absent from either part; multiclass with a class absent from the training part («raise
   validation_share, or check the outcome»). A class absent from validation only (multiclass)
   is a note and its per-class numbers are NULL.

`model|||n_train`, `n_validation` and (classification) the class counts per part are written.
**The validation part also chooses the number of trees** with early stopping, so its numbers
are slightly optimistic: the note `validation_chose_trees` says so in every report with early
stopping (a third part, a test, is «Open questions for the owner», 17).

**The fit.**

- **XGBoost** (native API): `xgb.train(params, dtrain, num_boost_round = max_trees, evals =
  [(dtrain, "train"), (dvalid, "validation")], early_stopping_rounds = …, evals_result = curves,
  verbose_eval = False, callbacks = [time budget])` with `dtrain` a `QuantileDMatrix`
  (`max_bin` 256) and `dvalid` a `QuantileDMatrix(ref = dtrain)`; `objective`
  `reg:squarederror`, `binary:logistic`, `multi:softprob` (`num_class` = k); `eval_metric` =
  the loss's metric (`rmse`, `logloss`, `mlogloss`) and the stop metric **last** (`mae`, `auc`,
  `error`, `merror`), since early stopping reads the last metric of the last `evals` item;
  `seed`, `nthread` = the threads, `validate_parameters = True`. Early stopping returns the
  model of the **last** iteration (checked online): the runner keeps `booster[:
  best_iteration + 1]` and records `best_iteration` itself (slicing drops the attribute,
  checked online), so the saved model needs no `iteration_range` when it is applied.
- **CatBoost**: `CatBoostRegressor` / `CatBoostClassifier(iterations = max_trees,
  learning_rate, depth, l2_leaf_reg, rsm, random_strength, bootstrap_type = "Bernoulli",
  subsample, loss_function = "RMSE" | "Logloss" | "MultiClass", eval_metric = the stop metric
  ("RMSE", "MAE", "Logloss", "AUC", "MultiClass", "ZeroOneLoss"), custom_metric = [the loss]
  when it differs, random_seed = seed, thread_count, use_best_model = early_stopping,
  early_stopping_rounds, has_time, allow_writing_files = False, train_dir = "/tmp/catboost",
  verbose = False)`, `fit(train_pool, eval_set = valid_pool, callbacks = [time budget])`;
  `get_evals_result()` gives the curves. **Assumed**: `allow_writing_files` (the code folder
  is read-only, so CatBoost must write nothing beside it), `ZeroOneLoss` as the error metric,
  and `hints=skip_train~false` to have the AUC on the training part (CatBoost skips it there by
  default); X3 checks them.
- **Threads** = the CPU limit (`OMP_NUM_THREADS`, as the Job sets it, **verified**:
  `threadsOf(CPULimit)` in `jobs_models.go`), passed as `nthread` / `thread_count`, and written
  (`model|||threads`).
- **Determinism**: the same input, parameters, group version **and number of threads** give the
  same numbers bit for bit; the self-test fits twice and compares. CatBoost documents that
  `thread_count` does not change results (checked online for `get_feature_importance`; assumed
  for training); for XGBoost's `hist` the thread count may change the last bits of a
  histogram's sums, so a change of the group's CPU limit may change the numbers in their last
  digits: the report writes the threads, and X2 measures 1 against 2 threads and writes what it
  finds here.
- **Time budget**: a training callback (XGBoost's `TrainingCallback.after_iteration` returning
  True; CatBoost's `after_iteration(info)` returning False, checked online) stops the boosting
  when 60 % of `time_budget_seconds` has passed, so that SHAP, the points and the files still fit
  in the step's timeout; the trees kept are the best so far, the note `time_budget` and the
  diagnostic `trees_limit` say so.

#### The output

The ten columns. Two new sections in `SECTION_SHAPES` (engine, and through `catalog.json` the
R and Python checks): `importance` (`level` required: the predictor's input name; `level_2`
optional: a class, multiclass only) and `class` (`level` required: the class label; `level_2`
null). Both not indexed.

| Section | Rows |
|---|---|
| `model`, every task | texts `model`, `variant`, `formula` («churn ~ price + channel», as `common.subject`), `software` («Python 3.14.x; xgboost-cpu 3.4.1; numpy 2.5.5; pandas 3.1.1»), `group_version`, `rows_from`, `note_*`, `task`, `library`, `objective` (the library's name: `binary:logistic`, `Logloss`), `stop_metric` (the library's metric name), `validation` (`random`, `grouped`, `time`), `importance_type` (`total_gain`, `PredictionValuesChange`); numbers `n`, `n_missing`, `n_train`, `n_validation`, `n_predictors`, `n_categorical`, `trees`, `best_iteration` (from 0), `max_trees`, `stopped_early` (1 or 0), `stopped_by_time` (1 or 0), `threads`, `shap_rows`, `seed` |
| `model`, regression | `train_rmse`, `train_mae`, `train_r_squared`, `validation_rmse`, `validation_mae`, `validation_r_squared`, `baseline_rmse` (the validation part predicted by the training part's weighted mean), `validation_mean` |
| `model`, binary | text `event_value`; `events`, `validation_events`, `train_auc`, `validation_auc`, `train_logloss`, `validation_logloss`, `baseline_logloss` (the training part's weighted prevalence), `train_brier`, `validation_brier`, `threshold`, and on validation at the threshold `validation_accuracy`, `validation_sensitivity`, `validation_specificity`, `validation_precision`, `validation_f1` |
| `model`, multiclass | `n_classes`, `train_mlogloss`, `validation_mlogloss`, `baseline_mlogloss` (the training part's class shares), `train_accuracy`, `validation_accuracy`, `validation_balanced_accuracy`, `validation_macro_auc` (mean of the one-against-rest AUCs of the classes present), `validation_macro_f1` |
| `importance` | per predictor: `gain` (XGBoost `get_score(importance_type = "total_gain")`, 0 for a predictor it omits: «zero-importance features will not be included», checked online; CatBoost `PredictionValuesChange`, which sums to 100, checked online), `gain_share` (gain / Σ gain), `mean_abs_shap` (on the SHAP sample; multiclass: Σ over classes of the class's mean \|φ\|), `mean_abs_shap_share`, `rank` (by `mean_abs_shap`, 1 = first), `mean_abs_shap_train`, `mean_abs_shap_validation` (the two parts of the sample, for the stability check), `missing_share`; multiclass also `mean_abs_shap` per class in `level_2` |
| `class` (multiclass) | per class: `support` (validation, weighted), `share_train`, `share_validation`, `precision`, `recall`, `f1`, `auc_ovr` |
| `cell` (classification) | the validation part's confusion matrix: `level` observed, `level_2` predicted, `observed` = the weighted count (the row count without weights) |
| `assumption` | the diagnostics, below |
| `curve` | `learning_loss` and `learning_stop` (when the stop metric is not the loss): `level_2` `train` \| `validation`, `x0` = the round from 1, `value` = the metric, at most `max_chart_points` per series (above: every ⌈T / max⌉-th round plus the best and the last); `roc` (binary, `level_2` `validation`: `x0` false-positive rate, `value` true-positive rate, at most 1,000 points: every k-th distinct threshold plus both ends; the AUC is computed on all); `roc_ovr` (multiclass, `level_2` = class, the first `max_roc_classes`, 10, by support, 200 points each) |
| `point` | `shap_beeswarm` (below); `calibration` (binary: `x0` the mean predicted probability of a decile of the validation part, `value` the observed rate, `x1` its rows); `observed_predicted`, `resid_predicted` (regression, validation, capped with the extremes kept and labelled, as `tab_cap`) |
| `hist` | `residual` (regression, validation); `probability` (binary: the predicted probabilities of the validation part, `level_2` = the observed class, 20 bins) |

`outputs.rows`: regression `pred_value`, `pred_residual`, `pred_part` (`train` \| `validation`);
binary `pred_prob`, `pred_class`, `pred_part`; multiclass `pred_class`, `pred_prob` (of the class
predicted), `pred_prob_*` (one per class, `pred_prob_<label>`; a clash with an input column is a
refusal at run time, since the labels are not known to the form), `pred_part`.

**The beeswarm's points.** Regression and binary only (a multiclass model has one beeswarm per
class: the report shows the per-class bars instead, and says why). `k` = min(10, predictors with
`mean_abs_shap` > 0), ranked by `mean_abs_shap`. The same rows for every predictor:
m = ⌊`max_chart_points` / k⌋ rows of the SHAP sample, drawn with the generator. One row per
point: `point|shap_beeswarm|<predictor>|<bin>|<i>`, with `x0` = the SHAP value (the model's raw
scale: the outcome's units, or log-odds), `value` = the y position, `x1` = the predictor's value
(NULL for a category or a missing value). `stat` = the colour bin: `low`, `mid_low`, `mid`,
`mid_high`, `high` (the quintile of the predictor's value among the m rows, numbers only),
`category`, `missing`. **The y position**: the predictor of rank r sits at k − r + 1; within it,
the points sorted by SHAP value fall into 50 equal bins of the x range, and the j-th point of a
bin (j from 0) is moved by o_j = (0, +1, −1, +2, −2, …)_j × h, with h = 0.4 / max(1, ⌈c / 2⌉) and
c the largest bin count of that predictor: the swarm's width follows the density, never past ±0.4,
the same picture on every run.

**The diagnostics** (section `assumption`; the catalogue's verdicts; no `p_value`, `statistic`,
`df`; `value` and `threshold` hold the numbers; `h0` holds «what is looked at»). Each row has
`advice` when not ok. The thresholds are rules of thumb chosen by this spec («Open questions for
the owner», 20).

| Check id | Tasks | What is computed | ok · doubtful · failed | Advice |
|---|---|---|---|---|
| `validation_size` | all | regression: validation rows; classification: the smallest class count in validation | ≥ 30 (regression ≥ 100) · ≥ 10 (≥ 30) · below | «the validation numbers rest on few rows: raise validation_share, or add rows» |
| `beats_baseline` | all | u = 1 − L_val / B_val, L the loss (RMSE, log-loss), B the baseline's; with `class_weight = balanced` both are weighted as the fit and B is the best constant of equal class shares | u ≥ 0.05 · 0 < u < 0.05 · u ≤ 0; not computed when the baseline's loss is 0 | «the model predicts the validation part no better than the average (the class shares): check the predictors and the outcome»; balanced: the advice `beats_baseline_balanced` |
| `overfitting` | all | r = (L_kept − L_best) / (B − L_best) on the validation loss curve, L_best its lowest value up to the tree kept, B the baseline's loss: the share of the gain that the trees after the best give back (the training loss is not read) | r ≤ 0.05 · r ≤ 0.25 · above; **not computed** with early stopping on the loss (the tree kept is the best by construction), where `value` is the validation loss over the training loss, for information | «the model learns the training rows more than the pattern: lower max_depth, raise min_child_weight (XGBoost) or l2_leaf_reg (CatBoost), lower subsample, or add rows»; with the loss as stop metric: `overfitting_kept_best` |
| `trees_limit` | all | with early stopping: whether it stopped (`stopped_early`), and `best_iteration` + 1; without: the validation loss at the last tree against its best | stopped, on the loss or not on its noise, and (learning rate above 0.05) ≥ 20 trees · not stopped (it reached `max_trees`), stopped by time or by the model file's size, stopped on another metric (auc, error, mae) while the loss at the tree kept is above its later minimum by more than 5 % of the gain over the baseline (`stop_on_loss`), or < 20 trees with a rate above 0.05 · — | «the validation loss was still falling at the last tree: raise max_trees or learning_rate» · `stop_on_loss`: stop on the loss · `trees_time`: raise the node's timeout · `trees_size`: the model file reached its limit · «stopped after very few trees: lower learning_rate» · without early stopping, the last tree > 1 % worse than the best: «turn early stopping on» |
| `class_imbalance` | binary, multiclass | r = largest class share / smallest (training, weighted) | r < 4 · 4 ≤ r < 20 · r ≥ 20 | «read the AUC and the log-loss, not the accuracy; choose a threshold; class_weight = balanced trades calibrated probabilities for balance» |
| `calibration` | binary | expected calibration error on validation: Σ_b (W_b / W) · \|ȳ_b − p̄_b\| over 10 bins of equal weight of the predicted probability | ≤ max(0.05, noise) · ≤ max(0.10, noise + 0.05) · above; noise is the ECE that calibrated probabilities reach 95 % of the time with those rows; not computed under 10 validation rows in a class | «the predicted probabilities do not match the observed rates: do not read them as probabilities, or recalibrate»; with `class_weight = balanced`: «expected: balanced weights move the probabilities; turn it off if they are used as probabilities» |
| `calibration_slope` | binary | the slope of a logistic regression of the outcome on logit(p) (validation, Newton's method in numpy, weighted), read with its 95 % interval | the interval meets 0.8–1.25 · meets 0.5–2 · neither; not computed under 10 rows in a class or when the classes separate | «below 1: the probabilities are too extreme (overfitting); above 1: too timid» |
| `importance_stability` | all | «Same importance in the two parts»: Spearman's ρ between `mean_abs_shap_train` and `mean_abs_shap_validation` over the union of the two parts' first 10 predictors; it reads a drift between the parts, not noise (a bootstrap does not tell the two apart) | ρ ≥ 0.8 · 0.5 ≤ ρ < 0.8 · ρ < 0.5; not computed below 3 predictors or 50 validation rows in the sample | «the model leans on different predictors in the two parts: the ranking is not reliable; more rows, or fewer correlated predictors» |
| `zero_importance` | all | one row per predictor with `gain` = 0 (no split uses it), `level_2` = the predictor; one ok row when there is none | ok · doubtful · — | «the model never uses it: drop it, or check that it varies» |
| `leakage` | all | `value` = the first predictor's share of \|SHAP\| (`level_2` = its name); and the validation's R² (regression), AUC (binary) or macro AUC (multiclass) | with two predictors or more: share < 0.8 or metric < 0.98 (ok unless a share ≥ 0.6 meets a metric ≥ 0.95) · doubtful: metric ≥ 0.98 alone, or share ≥ 0.6 with metric ≥ 0.95 · failed: share ≥ 0.8 and metric ≥ 0.98; with one predictor: ok · doubtful when the metric ≥ 0.98 · — | «one predictor explains nearly everything, or the fit is nearly perfect: check that no predictor is computed from the outcome or recorded after it (an id, a status, a later date)»; with `validation = random` and a time in the data the advice adds «try validation = time» |

The layout writes, under the table, the sentence of `assumptions_note`: «These are diagnostics
read against rules of thumb, not statistical tests: they have no p-value and no level of
significance. A doubtful or failed verdict says where to look, not that the model is wrong.»

#### The report

`backend/app/engine/models/layouts/boosting.py` (the blocks both models share), `xgboost.py` and
`catboost.py` (each: its name, its cards, its hyperparameters), words in `report_texts/
boosting.json`, `xgboost.json`, `catboost.json` (five languages), built with
`common.page(…, assumptions_note = …, p_values = False)` and the title of the assumptions'
block overridden as «Diagnostics» in the words file.

1. **First page**: the header band; four cards — regression: n (training · validation),
   validation RMSE, validation R², trees (best round); binary: n and events, validation AUC,
   validation log-loss, trees; multiclass: n and classes, validation accuracy, validation
   log-loss, trees; the formula line with the task and the validation («binary: churn = yes ·
   validation: random 20 %, seed 1»); the **Diagnostics** and their advice.
2. **Fit**: a table «Metric · Training · Validation · Baseline» (rows by task); the model's
   numbers (trees, best round, stopped early, threads, seed, the SHAP sample «SHAP on 5,000 of
   812,340 rows, drawn with seed 1» when `shap_rows` < n); the **hyperparameters** as the node
   had them (read from `params`, defaults filled by `check_params`).
3. **Importance**: two `bar` charts, horizontal, the first `max_importance_bars` (20) predictors
   sorted descending, «(others)» summed after them: gain share and mean |SHAP| share
   (multiclass: mean |SHAP| stacked by class, `split_by` the class); the table of every
   predictor (rank, gain share, mean |SHAP|, its share, missing share).
4. **SHAP summary (beeswarm)**, regression and binary, wide: one `xy` chart, seven `point`
   series (the five bins in a diverging ramp `#2c5aa0`, `#7fa7d6`, `#8c8c8c`, `#e3866b`,
   `#b2182b`; `category` `#6b7280`; `missing` `#c4c8cf`), a vertical line at 0, the predictors'
   names on the y axis; under it: «Each point is a row of the SHAP sample. Right of 0 the
   predictor raises the prediction, left of 0 it lowers it (log-odds for a classification).
   Colour: the predictor's value, from low (blue) to high (red); grey for a category or a
   missing value».
5. **Learning curves**: `learning_loss` (and `learning_stop`), training and validation as
   `line`, a vertical line at the best round (`target_x` = `model|||best_iteration` + 1), under
   `overfitting` and `trees_limit`.
6. **By task** — regression: observed against predicted (diagonal), residuals against predicted
   (line at 0), the residuals' histogram, all on validation; binary: ROC (diagonal, the AUC in
   the title), calibration (deciles, diagonal) under `calibration`, the predicted probabilities
   by observed class (`hist`), the confusion matrix at the threshold (`heatmap`, as logistic);
   multiclass: the confusion matrix (`heatmap`), the per-class table, ROC one against the rest
   (up to 10 classes, `split_by` the class).
7. **Notes**: the `note_*` rows (`validation_chose_trees`, `time_split`, `grouped_split`,
   `time_budget`, `shap_sampled`, `class_absent_validation`, `zero_weights`,
   `model_file_too_large`, `card_too_large`), the software, the group version.

Excel: the diagnostics, the metrics, the hyperparameters, the importance, the per-class table,
the confusion matrix.

**The `xy` chart grows two options, engine-only like the kind itself** (`xy` exists only in the
engine: saved reports, dashboards and the designer are untouched): `Series.color` (`#rrggbb`,
`xy` only: a series' fixed colour) and `options.y_categories` (an expression: the y axis shows,
at each whole number j, the expression's value on the drawn rows whose `ROUND(value)` is j — the
first such row's; the axis then runs from 0.5 to its largest j + 0.5 with a tick at each whole
number, names cut at 24 characters). `reports.definition.check` refuses both outside `xy`;
`frontend/chart-renderer/render.mjs` maps them to ECharts (`itemStyle.color`; the y axis's
`min`, `max`, `interval` 1 and a label formatter); `tests/report_samples.py` and
`presets.sample.json` gain a beeswarm; the existing golden PDFs do not change. A new kind was
considered and not taken: a beeswarm is points on a numeric x with a jitter the runner already
computes, which is what `xy` draws. No other chart needs anything new (`bar` with `sort` and
`top`, `heatmap`, `xy` lines and points exist, **verified** in `definition.py`).

#### The saved model, MLflow and Predict

**The card** (`model.json`, ≤ `max_card_bytes`, 1 MB, as for every model) holds what applies the
model besides the trees: the common keys (`format`, `format_version`, `model`, `variant`,
`group`, `group_version`, the hashes, `software`, `params`, `inputs` in the order of the
positional names `v1`…, each category with its `levels`), and a `boosting` object, declared in
`card.schema.json`:

```json
"boosting": { "library": "xgboost", "library_version": "3.4.1", "task": "binary",
              "objective": "binary:logistic", "classes": ["no", "yes"], "event_value": "yes",
              "threshold": 0.5, "trees": 412, "best_iteration": 411, "threads": 2,
              "missing_level": null,
              "model_file": { "name": "xgboost.json", "format": "xgboost-json",
                              "bytes": 812345, "sha256": "…" } }
```

**The model file**, beside the card in `artifacts/`, written only when the node logs to MLflow
(as `native.rds` today):

| Model | File | How it is written and read | Why it is safe to read |
|---|---|---|---|
| XGBoost | `xgboost.json` | `booster.save_model(path)` with a `.json` name; `xgboost.Booster(model_file = path)` | XGBoost's own JSON model format, «universal among the various XGBoost interfaces» and the one that keeps the categories (checked online); data parsed by the library, no code |
| CatBoost | `catboost.cbm` | `model.save_model(path, format = "cbm")`; `CatBoost().load_model(path, format = "cbm")` | CatBoost's binary model format, the default of both calls (checked online). Not `json`: CatBoost saves a usable JSON of a model with categorical features only when handed the training pool, and without it «for review only»; `onnx`, `coreml` refuse categorical features; `python` and `cpp` are code |

Nothing else is written: **no `native.pkl`**, and no pickle, joblib or other format that runs
code when read, ever (the boundary «loading a serialized model» stays: these two files are model
data that the library parses, by the owner's choice of 2026-11-20). The sha256 and the size of
the file are computed from its bytes before it is written and go into the card; the card's own
sha256 is the MLflow tag `tabularia.card_sha256`, as today: so MLflow's tag fixes the card and
the card fixes the file.

**Limits.** The file is at most `max_model_file_bytes` (64 MiB; 1,000 trees of depth 6 make a
JSON of tens of megabytes at most, which X2 measures and writes here). Above it, or with a card
above its ceiling, the rule of «A card too large» holds: the fit goes on, results, rows and
report are complete, the note `model_file_too_large` (or `card_too_large`) says that the model is
not logged to MLflow, and neither `model.json`, `MLmodel` nor the file is written.

**MLflow** (lot G's logging, extended):

- artifacts `model/MLmodel`, `model/model.json`, `model/xgboost.json` or `model/catboost.cbm`
  (streamed from the scripts bucket, as `native.rds` is); the `tabularia` flavor of `MLmodel`
  gains `model_file: <name>` and `model_file_sha256`;
- tags of the version: `tabularia.variant` (for every model from now on: Predict's form reads
  the task from it) and `tabularia.model_file_sha256`, besides today's;
- metrics: every number of the `model` section and `assumption.<check>[.<level_2>].value` (the
  diagnostics have no statistic or p-value); `importance.<predictor>.gain_share` and
  `.mean_abs_shap_share` for the first 20 predictors; the learning curves as **metric
  histories** (`learning.train.<metric>`, `learning.validation.<metric>` with `step` = the
  round), the points of `curve|learning_*`, so MLflow draws them in its own interface.

**Predict** (lot H's node, extended; `PREDICT` in the catalogue):

1. The gateway reads the card as today (tag `tabularia.card_sha256`), then the card's
   `boosting.model_file`: its `name` must be the one of the card's library (`xgboost.json` for
   `xgboost`, `catboost.cbm` for `catboost`; any other name, a path, is refused), its `bytes`
   at most `max_model_file_bytes`. It **streams** `model/<name>` from MLflow (a new
   `mlflowconn.Tracker.Open(ctx, artifactPath, name, limit)`: the body read with a limit, not
   held whole in memory) into the engine's new **`POST /scripts/objects/upload`** (the raw
   bytes, `?key=<step>/model_file&sha256=…&bytes=…`; the engine writes them to the scripts
   bucket while hashing, refuses past `max_model_file_bytes`, and on a different sha256
   deletes the object and answers 409). Today's `/scripts/objects/write` takes UTF-8 text up to
   2 MB, which neither file is. Refusals: «the model file of … does not match the fingerprint
   its card keeps: it is not applied», «… has no model file among its files», «… is larger than
   a model file may be».
2. The Job gets `TABULARIA_MODEL_IN` (the card) and `TABULARIA_MODEL_FILE_IN` (the file); the
   runner checks the sha256 again, copies the file to `/tmp`, loads it with the calls above,
   builds the frame from the card's `inputs` (positional names, the card's levels; a level not
   seen when fitting becomes missing for XGBoost and an unknown category for CatBoost), predicts
   with the card's threads rule, and writes the rows.
3. **Columns added** (`PREDICT.columns`, keyed `model:variant` for these models):
   regression `prediction`; binary `prediction` (the probability of `event_value`, as the
   logistic regression's) and `prediction_class` (at the node's `threshold`, else the card's);
   multiclass `prediction` (the class with the highest probability), `prediction_probability`
   (its probability) and `prediction_prob_<label>` per class. `threshold` applies to
   `xgboost:binary` and `catboost:binary` only (`PREDICT.applies_to` accepts `model:variant`;
   `modelcatalog.CheckPredict` gets the card's variant, which `PredictTask` already has).
4. **Missing and unseen values** get a prediction through the model's missing-value path (unlike
   the regressions, whose rows get NULL): the rows with an unseen level are counted per column
   and said on the run page (`run.json` warnings, as today).
5. **Checked against the fit**: predicting the training rows from the saved file gives the
   fitted model's predictions **bit for bit** (same library, same file), a self-test of each
   model.

**Security.** The model file is parsed by the library's reader (C++), not executed; it is read
only after its sha256 matched the card's, and the card's matched MLflow's tag. That chain ties the
file to the card, **not to Tabularia**: whoever can register versions in that MLflow can register
a card and a file of their own making with the right tags, as today for the cards. A crafted file
could aim at a fault of the reader; the Job's isolation (no internet, no MLflow credential, the
scripts bucket's key only, user 10001, read-only root) is then the boundary. Signing the cards
with a key of the gateway closes this for every group: «Open questions for the owner», 16.

#### Reference values

Small synthetic tables, CSV, fixed seeds, in `groups/boosting/reference/data/`: regression (300
rows, five numbers, one category of four levels, 5 % missing values in two predictors, weights);
binary (400 rows, events about one in five, one category); multiclass (450 rows, three classes);
a leakage case (a predictor equal to the outcome plus a little noise: `leakage` failed); a time
case (`order_by` a date, `validation = time`); refusals (15 rows; a binary outcome whose events
all fall in the training part). Each case is run for both models where it applies (about a dozen
cases). `make_reference.py` computes `expected.json` **outside Tabularia**, inside the group's
image built from the recipe (the same packages): it re-implements the split from this section
(a second construction, not the runner's code), calls `xgb.train` and CatBoost directly with the
same parameters, and computes the metrics by the formulas below. `make_witness.py` runs in a
separate environment (`uv`, the witnesses pinned in `witness.json`: `scikit-learn==1.9.1`,
`shap==0.54.1` with their closure, and the recipe's `numpy`, `pandas`, `xgboost-cpu`,
`catboost`). Both files' headers give the command and the versions.

| Check | Where | Tolerance |
|---|---|---|
| The runner against the library called directly, same parameters, same split | self-test (expected.json) | relative 1e-9 (in practice equal) |
| Predictions of the model file read back against the fitted model's | self-test | bit for bit |
| **XGBoost predictions by a tree walker written in numpy** over `xgboost.json` (the trees' `split_indices`, `split_conditions`, `default_left`, `split_type` and categories, the leaves' values, `base_score`), comparing in float32 as XGBoost does | self-test (`boosting_xgboost.py` registers it in `COMMON_TESTS`) | absolute 1e-6 × max(1, \|margin\|) |
| **CatBoost predictions by an oblivious-tree walker in numpy** over a JSON export of a model **with numbers only** (`save_model(format = "json")`, the witness's only use of it) | witness | relative 1e-9 |
| **SHAP additivity**: Σ_j φ_j + φ_0 = the raw score (margin, log-odds), every row of the sample, every class | self-test | CatBoost (doubles): 1e-6 absolute; XGBoost (float32 outputs, summed in float64): (p + 1) · 2⁻²³ · max(1, Σ\|φ_j\| + \|φ_0\|), the rounding of p + 1 float32 values |
| SHAP against `shap.TreeExplainer` (XGBoost; CatBoost if `shap` 0.54.1 reads it) | witness | 1e-5 |
| **Gain**: Σ of the `loss_changes` of the splits on a predictor in `xgboost.json` against `get_score(importance_type = "total_gain")` | self-test | relative 1e-6 (float32 sums) |
| CatBoost `PredictionValuesChange` recomputed from the JSON export's leaf values and weights by its documented formula, normalized to 100 (numbers only) | witness | 1e-6 |
| The metrics written by hand (below) against scikit-learn's `mean_squared_error`, `mean_absolute_error`, `r2_score`, `roc_auc_score`, `log_loss`, `brier_score_loss`, `confusion_matrix`, `f1_score`, `balanced_accuracy_score`, all with `sample_weight` | witness | relative 1e-10 |
| **Determinism**: two fits in one process, same seed and threads | self-test | bit for bit: `results.parquet`'s rows, predictions, SHAP, and for XGBoost the file's bytes (assumed: XGBoost writes no time into it; if it does, the predictions only) |
| Counts, the split's sizes, verdicts, texts | self-test | exact |

**The metrics, by hand** (numpy, weights w, all Σ over the part): RMSE = √(Σw e² / Σw); MAE =
Σw\|e\| / Σw; R² = 1 − Σw e² / Σw (y − ȳ_w)²; log-loss = −Σw [y log p + (1 − y) log(1 − p)] /
Σw with p clipped to [ε, 1 − ε], ε = the float64 machine epsilon (scikit-learn's rule since 1.5,
**assumed**, checked by the witness); multiclass log-loss with the observed class's probability;
Brier = Σw (p − y)² / Σw; AUC = Σ_{pos i} Σ_{neg j} w_i w_j ([s_i > s_j] + ½[s_i = s_j]) / (W_pos
W_neg), in one sort; accuracy, sensitivity, specificity, precision, F1 from the weighted confusion
matrix; macro AUC and macro F1 as plain means over the classes present.

#### Performance and limits

| Limit | Default | Where | Beyond it |
|---|---|---|---|
| Input rows (`max_rows`) | 1,000,000 | group setting | the common sentence |
| Cells, rows × predictors (`max_cells`) | 50,000,000 | catalogue, checked by the gateway before the Job (it knows the rows from the engine and the predictors from the parameters) | «1,000,000 rows × 120 predictors make 120 million values; this group allows 50 million: fewer predictors, or sample before the node» |
| Memory, CPU | 4 GiB, 2 CPU (request half) | group setting | the cluster's exit 137, the Script node's message |
| Predictors | 200 (`max_predictors`) | catalogue | sentence |
| Levels of a categorical predictor | 1,000 (`max_levels_trees`) | catalogue | «the column … has 4,812 values: is it an identifier?» |
| Classes | 20 (`max_classes`) | catalogue | sentence |
| Trees, depth | 5,000 rounds; depth 12 (XGBoost), 10 (CatBoost) | catalogue | the form's bounds |
| **SHAP sample** (`shap_rows`) | at most `max_shap_rows` (5,000) rows, fewer when the model is large: rows = clamp(`shap_cost_budget` / (T · L · D²), 200, 5,000), T the trees, L the leaves per tree (2^D), D the depth, `shap_cost_budget` 2·10¹⁰ (TreeSHAP's cost is T · L · D² per row) | catalogue | drawn with the generator, in proportion to the parts with at least min(500, validation rows) from validation; the row `shap_rows` of the `model` section and the note `shap_sampled` say so, and the report's line under the importance |
| Chart points | `max_chart_points` (2,000) per chart; beeswarm ⌊2,000 / k⌋ rows per predictor; ROC 1,000; learning curves thinned | runner | as above |
| Model file | 64 MiB (`max_model_file_bytes`); the boosting stops before 95 % of it (XGBoost measures the JSON as it grows, CatBoost has a cap per tree) | catalogue, runner | the note `model_size`, the diagnostic `trees_limit` doubtful (`trees_size`); a file over the limit is not registered |
| Time | the group's 3,600 s; the node's timeout within it; boosting stops at 60 % of it, counted from the runner's start; the SHAP sample is sized to end by 85 % | group, node, runner | the note `time_budget`, the diagnostic `trees_limit` doubtful (`trees_time`) |

The catalogue's `LIMITS` gain `max_predictors` (200), `max_levels_trees` (1,000), `max_classes`
(20), `max_cells` (50,000,000), `max_shap_rows` (5,000), `shap_cost_budget` (20,000,000,000),
`max_model_file_bytes` (64 MiB), `max_importance_bars` (20), `max_beeswarm_predictors` (10),
`max_roc_classes` (10); the R groups read none of them.

Memory: XGBoost's `QuantileDMatrix` keeps one byte per value (256 bins) instead of a float
matrix, and the pandas frame is freed after it is built; 50 million cells are about 400 MB as
float64 at the peak. Durations and sizes measured by X8 on kind and by X9 are in «The kind run»; they are not promised.

#### Assistants and documentation

- **Assistants** (`gateway/app/services/ai_flow_agent.py` reads its copy of the catalogue, so the
  two nodes and their parameters come with the snapshot; `gateway-go/internal/aiprompts/
  prompts.json` and `internal/mcp/tools.json` written again by their generators): a paragraph
  of the flow assistant's prompt — choose the regressions to explain an effect with its
  coefficient and its test, boosting to predict with many predictors, interactions and
  non-linear effects; always set `task` (and `event_value` for binary); leave the
  hyperparameters at their defaults unless asked; prefer `validation = time` with `order_by`
  when the goal is to predict later rows and the table has a time column; never put in the
  predictors a column computed from the outcome. The data assistant's prompt: the diagnostics
  are rules of thumb, not tests; SHAP values are on the raw scale; the importance says what the
  model uses, not what causes the outcome.
- **Documentation**: `docs/capabilities/models.md` gains «Boosting (Python)»: the two nodes,
  the tasks, the validation part and why its numbers are slightly optimistic, every parameter
  with its default, the diagnostics and how to read them, gain against SHAP, how to read a
  beeswarm, the learning curves, the model file and Predict, the limits; an example on the
  pasta maker's sample data (will a customer reorder? next month's boxes?). `docs/scripting.md`:
  managed environments may be Python. The MLflow page: the model files and the new tags and
  metric histories. The capabilities map, `README.md`'s map, `CHANGELOG.md`; the site's
  navigation is unchanged (the page exists). `docs/configuration.md` changes only if a setting
  is added (none is: the limits are the catalogue's). No competitor named.

### As built, the boosting group (lots X0–X9)

Facts the builders settled; where one differs from the text above, this block is right
(**verified** in the committed code, 2026-11-20).

**Contract and runner (X0, `49bd8318`)**
- The fixtures are named `{model}-{variant}.json`; `PredictColumns` takes the variant, as
  `CheckPredict` does.
- XGBoost 3.4 refuses categories it has never seen: the runner turns them into missing values
  before predicting.
- Determinism checked with 1, 2 and 4 threads: identical bit for bit, XGBoost and CatBoost.
- The card's `software` is «Python» for boosting; `reference` is required only outside boosting.
- `PYTHONHASHSEED=0` is also set in the checking Job (`models_build.go`, `jobs_models.go`).
- The `max_cells` message rounds to tenths of a million, so just over the limit it can say
  «50 million … allows 50 million».
- Verified: XGBoost's SHAP with native categories works (difference from the exact Shapley
  values 4.8e-7); CatBoost's multiclass SHAP has shape (n, k, p + 1); in XGBoost's JSON the listed
  categories go right, and `base_score` is a string holding an array.
- pyarrow with the `AWS_*` variables on kind's MinIO: verified by X8 (see «The kind run»).

**The shared module and the two models (X1, X2, X3: `2bbf8c04`, `25c24dd5`, `4091cd38`, `b0d0018b`)**
- `shap` is no witness for XGBoost (it would use the same `pred_contribs`): the witness is the
  exact Shapley value by brute force.
- `classes` in the binary card is [the other class, `event_value`], not in text order.
- The stratified split rounds a half up; the calibration bins, the `Fitted` interface and the new
  refusals (`empty_predictor`, `multiclass_two`, `too_many_classes`, `outcome_type`,
  `class_absent`, `too_many_predictors`, `order_by_type`) are in `boosting_shared.py`.
- `run.json`'s `software` says only «Python» (`tab_main`).
- Time: SHAP dominates, 5,000 rows × 1,000 trees take 16.7 s against 1.1 s of training.
- CatBoost starts at the best constant (`5b78ab2d`): `boost_from_average` for RMSE and Logloss; for
  MultiClass CatBoost refuses the option, so the start is the log of the class shares, copied into
  the bias with `set_scale_and_bias`.
- `l2_leaf_reg` 0 crashed CatBoost on a multiclass («System of linear equations is not positive
  definite»): its type in the catalogue is now `positive` (`f0f46c7b`), with a case in
  `params_cases.json`.

**The model file in MLflow and Predict (X5, `d4cdd535`)**
- The engine's upload route writes the file to a temporary file while it computes the hash and
  puts it in the bucket only if the hash is right: with another hash the object never exists. The
  bucket connection travels in the header `X-Tabularia-Scripts-Connection` (base64, encrypted
  secret). 409: size or hash differ from the card's; 413: over the limit.
- The gateway skips a file over the limit without logging it, with a warning.
- A file that does not match the card leaves the MLflow run with `MLmodel` and `model.json` and
  no registered version.
- New tags of the version: `tabularia.variant`, `tabularia.model_file_sha256`.
- The Job's `TABULARIA_MODEL_FILE_IN` points to `<step>/model_file`, without extension; the runner
  recomputes the sha256 and loads only with the libraries' native APIs.

**The report (X4, `cf604909`, `9426a324`, `6c1b1443`)**
- `render.mjs` needs only the `names` formatter; the ECharts option is built by `charts.py`.
- The «Note» table may land alone on a new page.
- The layout tests read the importance order and the classes from the fixture.

**The diagnostics' thresholds (tuning: `713c8469`, `5b78ab2d`, `ae1b55af`)**
The first thresholds raised false alarms on clean data (overfitting 22 of 30 at n = 400 for the
binary and 27 for the multiclass, still «doubtful» 10 of 10 at n = 2,000; calibration failing 22 of
30 at n = 400; leakage always «doubtful» with one predictor). They were redone against simulations
of clean data:
- **overfitting**: r = (L_held − L_best)/(B − L_best) on the validation curve; ok at r ≤ 0.05,
  doubtful up to 0.25, failed above; the training loss is no longer read.
- **calibration** (ECE): ok at max(0.05, the 95 % noise of the ECE with those rows), failed above
  max(0.10, noise + 0.05); «not_computed» under 10 rows per class.
- **calibration_slope**: the 95 % interval is read: ok if it meets 0.8–1.25, doubtful if it meets
  0.5–2.
- **leakage**: with two predictors or more, failed when one holds ≥ 0.8 of |SHAP| and the metric
  is ≥ 0.98; with one predictor, doubtful when the metric is ≥ 0.98 (and, with a share ≥ 0.6, a
  metric ≥ 0.95).
- **trees_limit**: «few trees» counts only with a learning rate above 0.05.
- **Advice**: the `assumption` section writes an `advice_id` next to the sentence and the report
  picks its advice by that id (`713c8469`), not by a substring of the sentence.
- **Still imperfect**: `beats_baseline` is «doubtful» 10–15 % of the time on weak classifications
  at n = 400; a heavily overfitted regression is caught only in 40–80 % of the cases.

**The kind run (X8: `72cb1ab4`, `4000df9e`, `9e2d5428`)**
- MLflow now holds the learning curves as metric histories, the diagnostics' `assumption.*.value`
  and the importance of the first 20 predictors; the engine's `/scripts/results/rows` also returns
  `x0`.
- The Administration panel has the group's name and description in the five languages; the Predict
  form offers the threshold for the binary versions of both models (the variant is in the tag
  `tabularia.variant`).
- The prediction is identical bit for bit to the fit's. pyarrow with the `AWS_*` variables on kind's
  MinIO works: **verified**.
- **Durations** (kind, one node): the group's build about 2 minutes; a fit on the small reference
  tables 11–13 s; 100,000 rows × 10 predictors: XGBoost 14 s, CatBoost 24 s; a prediction 7–9 s.
  SHAP dominates: 5,000 rows × 1,000 trees take 16.7 s against 1.1 s of training.
- **Sizes**: XGBoost's JSON at depth 6 weighs about 7.5 KB per tree; 20 classes at depth 6 about
  156 KB per round, which is what the 64 MiB limit and the check below are for.
- Cosmetic points found there were closed by X9 (the «UserWarning:» prefix, the beeswarm's caption,
  the panel's intro).

**The final review (X9: runner `a4049aac`, `469eeaf3`, `afaaf159`, `dd056acd`, `9a4b7d50`, `c71b8000`, `20b89493`, `a5e18952`, `810d3b74`, `1e4a558c`, `f57b520a`; Go, engine and UI `b67db93a`, `39da4b1c`, `22016095`, `21f095d9`, `916265bc`, `dd2cdc9d`, `1d67933d`, `3ba3e3db`)**
- **Infinite values**: refused as `infinite`, with the column's name, the same in both nodes and in
  Predict; `constant_predictors` when no predictor varies in the training part.
- **Time budget**: counted from the runner's start (`_started`); the SHAP sample is sized on the
  time left and ends by 85 % of it, and with enough time the sample does not change.
- **Model file**: XGBoost measures the JSON as it grows and stops before 95 % of the limit;
  CatBoost has a cap per tree; the note `model_size`, the advice `trees_size`.
- **Balanced classes**: validation is weighted as the fit; `beats_baseline` uses a baseline of
  equal class shares (advice `beats_baseline_balanced`).
- **`stop_on_loss`**: see the table; `error` and `ZeroOneLoss` cut at 0.5.
- **overfitting** is not computed with early stopping on the loss (advice `overfitting_kept_best`).
- **Report**: `layouts.common.by_params` picks the h0 by the settings; the `xy` option `unit_square`
  draws the calibration on a square; the beeswarm's caption depends on the task; `trees_time`
  after a stop by time; the runner's own warnings reach `run.json` as sentences.
- **Go, engine and UI**: a `native.pkl` is never uploaded to MLflow; Predict refuses a version whose
  `tabularia.model_file_sha256` differs from the card's (the tag is **verified**); the form reads
  the variant of the version chosen; a model Job's `/tmp` has a `sizeLimit` of 2 × the file + the
  group's memory + 256 MiB; the upload runs in a threadpool and takes only keys
  `env-*/…/model_file`; MLflow metric keys stay within 250 characters, colliding names are kept
  with suffixes and said.
- **Witnesses**: `make_witness.py` rerun with the pinned packages on the final rules: every value
  agrees (99 compared; shap's own numbers on XGBoost and CatBoost are printed beside, not
  compared).

**Documents and assistants (X7, `d9e155d5`, `8ac0fcd8`, `6cac7397`)**: the page «Boosting (Python)»
of `docs/capabilities/models.md`, MLflow, Kubernetes, flows, the changelog, the README; the
assistants' prompts and tools are regenerated from the catalogue and the generated files hold
the two nodes. The lot is seven models (five in R, two in Python) and the Predict node.

### The node «Parametric survival» — group `survival`, R (planned 2026-11-20)

**Planned on 2026-11-20, not built.** The owner's request of that day: one node *Parametric
survival* for the Weibull and the other classical distributions of a time to an event, in the
group `survival`, with Predict from its first lot. This section is the contract the lots of «The
parametric survival lots» (under «Pieces») build against; everything this spec says of every
model holds for it (Kubernetes only, the group enabled by an administrator, the ten columns,
charts drawn by the report engine, Run with a button, MLflow logged by the Go gateway, Predict
from the card alone, the self-test before a version is ready) unless this section says otherwise.

Words, besides **verified** and **assumed** (top of this file): **verified in R** = run on
2026-11-20 in the survival group's image (`tabularia-models-survival:dev`: R 4.6.0, `survival`
3.8-12) with `flexsurv` 2.3.2 and its closure installed into a **scratch library** from the binary
packages of the CRAN snapshot of 2026-11-19, on synthetic tables of 25 to 20,000 rows (a Weibull
AFT with a number and a category of three levels, uniform censoring; the main one has 240 rows and
174 events); those scripts are not in the repository, and lot P1's `make_reference.R` redoes the
checks with the recipe's own build. **From CRAN's index** = read in CRAN's `PACKAGES` file of
2026-11-19, the one the three recipes were pinned from.

#### The owner's decisions (2026-11-20)

| Question | Decision | By |
|---|---|---|
| Nodes | **One node**, `parametric_survival` («Parametric survival»), the distribution chosen inside: Weibull, exponential, log-normal, log-logistic, generalized gamma, Gompertz | Owner |
| Group, language, image | The group **`survival`**, R, **its image extended**; the new recipe is a new version, which rebuilds the group | Owner |
| Coefficients | **Time ratios** (accelerated failure time, AFT) with their intervals for every distribution; for Weibull and exponential also **hazard ratios** with their intervals, comparable with the Cox node's | Owner |
| Gompertz | A proportional-hazards model by nature: **hazard ratios only**, no time ratio («Coefficients») | Spec |
| Automatic choice | An option (`distribution = auto`): every candidate fitted, the **lowest AIC** kept, the report shows the comparison (log-likelihood, parameters, AIC, BIC, ΔAIC, Akaike weights) | Owner |
| A candidate that does not converge | A second start; still not converged, it stays in the table marked, out of the choice and of the weights; none left, the node refuses («Automatic choice») | Spec |
| Predict | **In the first lot**: per row the median time, the node's quantiles and S(t) at the node's times, with intervals on request | Owner |
| Conditional survival | Proposed as an option of Predict, `alive_at`, in a lot of its own (P9) | Spec |
| Parsimony in `auto` | **Decided 2026-11-20**: when a simpler distribution nested in the one with the lowest AIC (Weibull in the generalized gamma, exponential in the Weibull) is within 2 of that AIC, the simpler one is chosen; the report says so in one sentence and the comparison table marks it («Automatic choice») | Owner |
| Left and interval censoring, extrapolation, thresholds, Gompertz in `auto` | **Decided 2026-11-20**: as «Open questions for the owner» 24–27 recommend: not now; predictions beyond the last time observed stay, with the warning; there are no thresholds (2026-11-20, later: verdicts only from tests with a p-value against the node's alpha, «The checks»); Gompertz stays among the candidates of `auto`, with a sentence on the first page | Owner |

#### The distributions

| `distribution` | flexsurv `dist` | Its parameters (flexsurv's names; the location first) | The predictors act on | Coefficients reported | Hazard shapes |
|---|---|---|---|---|---|
| `weibull` (default) | `weibull` | `scale` (log), `shape` (log) | log scale: AFT | time ratios; hazard ratios exp(−β · shape) without strata | rising (shape > 1), falling (< 1), constant (= 1) |
| `exponential` | `exp` | `rate` (log) | log rate | time ratios exp(−γ) and hazard ratios exp(γ), γ the rate's coefficient | constant |
| `lognormal` | `lnorm` | `meanlog` (identity), `sdlog` (log) | meanlog: AFT | time ratios | rises, then falls |
| `loglogistic` | `llogis` | `scale` (log), `shape` (log) | log scale: AFT | time ratios | falling (shape ≤ 1), or rises then falls |
| `gengamma` | `gengamma` (Prentice's parametrization) | `mu` (identity), `sigma` (log), `Q` (identity) | mu: AFT | time ratios | rising, falling, arc, bathtub; Weibull at Q = 1, log-normal at Q = 0, gamma at Q = σ |
| `gompertz` | `gompertz` | `rate` (log), `shape` (identity, either sign) | log rate: proportional hazards | hazard ratios exp(β); **no time ratio** | rising exponentially (shape > 0), falling (shape < 0: a share of the rows never has the event) |
| `auto` | the six | — | — | the chosen one's | — |

(identity, log: the scale on which the optimiser moves the parameter, flexsurv's `transforms`.)

**Gompertz has hazard ratios only.** Scaling time in a Gompertz model changes its shape too: an
AFT effect turns a Gompertz baseline into a Gompertz of another shape for each value of the
predictor, so «the time ratio of x» is not a constant of the model. Its predictors multiply the
rate, so exp(β) is a hazard ratio, constant over time: that is the Gompertz model, and it is what
the report gives, beside the Cox node's hazard ratio on the same rows. The report says it in one
sentence where a time ratio would stand; the predicted median and quantiles of the profile say how
long, in time units. Gompertz stays among the candidates of `auto` («Open questions for the
owner», 27).

**Not added: gamma and generalized F.** The gamma is the generalized gamma at Q = σ: whenever it
would fit, the generalized gamma fits at least as well and its own Q says how close it is; its
hazard has no closed form and it is rarely the scientific choice for a time to an event. The
generalized F (four parameters, flexsurv `genf`) nests the generalized gamma; on the 240-row table
its fourth parameter P came out with the interval 1.4e-21 to 4.1e17 and the log-likelihood moved by
0.0007 (**verified in R**): a parameter more than tables of this size hold, with a fragile
optimiser. Both stay one line of the catalogue away (flexsurv fits them); not now.

#### The package: `flexsurv` for every distribution

`survival::survreg` fits four of the six (Weibull, exponential, log-normal, log-logistic);
`flexsurv::flexsurvreg` fits all six with one likelihood convention (the full density of the times,
so the AICs of different distributions compare), one set of `d`, `p`, `q`, `h`, `H` functions, the
ancillary formulas (`anc`) that give each stratum its shape, and `Surv(start, stop, event)` for
delayed entry. **The runner fits every distribution with `flexsurvreg`**; `survreg`, already in the
image, is the reference for the four it knows («Reference values»). The other way (`survreg` for
four, and a likelihood written by the runner, with an optimiser of its own, for the generalized
gamma and Gompertz) needs no new package but keeps two code paths and leaves the hardest optimiser,
the generalized gamma's, with no library behind it: not taken.

**Verified in R** (the 240-row table; `flexsurvreg` against `survreg` with the same formula):

| What | Result |
|---|---|
| Log-likelihoods, the four common distributions | equal within 1.1e-13 |
| Coefficients | within 2.0e-10 (Weibull), 7.5e-11 (exponential, with the sign turned: survreg's are AFT, flexsurv's on the rate), 4.1e-10 (log-normal), 7.1e-12 (log-logistic); with case weights 8.3e-11 |
| Strata: `survreg(… + g + strata(g))` against `flexsurvreg(… + g, anc = list(shape = ~ g))` | the same log-likelihood and the same scale per stratum to every printed digit; the numeric coefficient within 1e-10 |
| Exponential without predictors | rate = events / total time within 2.4e-11; the standard error of log rate = 1/√events exactly; the log-likelihood equals d · log λ − λ · Σt |
| **flexsurv's own standard errors** | relative gap to survreg's analytic ones: 7.6e-9 (Weibull), 1.9e-9 (exponential), **7.7e-6** (log-normal), **5.4e-4** (log-logistic): its Hessian is `optim`'s finite differences where the distribution has no analytic derivatives |
| The inverse of a Richardson Hessian (`numDeriv::hessian`) of the log-likelihood at the estimates | 7.6e-9, 1.0e-9, 1.9e-10 (Weibull, log-normal, log-logistic): **the runner computes the covariance itself, for every distribution** |
| The runner's log-likelihood written from flexsurv's `h<dist>` and `H<dist>`: Σ w (δ log h(t) − H(t)) | equal to flexsurv's to the last bit, all six |
| Weibull hazard ratios exp(−β/σ) with a delta-method interval, against a `weibullPH` fit | log hazard ratios within 1.4e-10, standard errors within 3.6e-9 (at the maximum the delta method and the Wald interval of the other parametrization agree) |
| Quantiles and S(t) from the parameters with flexsurv's `q`, `p` functions, against `summary.flexsurvreg(newdata)` | quantiles equal, survival within 1.1e-16, all six |
| The delta-method standard error of the log median against `predict(survreg, type = "uquantile", se.fit = TRUE)` | 4.3e-9 (Weibull), 2.4e-9 (log-normal) |
| dfbetas as V · Uᵢ (Uᵢ the gradient of row i's log-likelihood, `numDeriv::jacobian`) against `residuals(survreg, type = "dfbetas")` | within 5.2e-9 |
| The optimiser's default tolerance (`reltol` 1e-8) against 1e-15 | the same numbers for five distributions; **Gompertz moves by 7.5e-6** in a coefficient (49 against 67 iterations) |
| Generalized gamma with Q fixed at 0, against the log-normal | log-likelihood within 5e-7 (nested as documented) |
| No events | flexsurv stops with «initial value in 'vmmin' is not finite» |
| An event at time 0 | flexsurv and survreg: «Invalid survival times for this distribution» |
| A level of a category with no events | Weibull: the coefficient 13.5 with a standard error of 2,384, **the optimiser reports convergence and no warning** |
| Gompertz on falling hazards (no predictors) | shape −0.0895: S(∞) = exp(rate / shape) = 0.511, `qgompertz(0.5)` = Inf |
| 25 rows, 18 events | generalized gamma, Gompertz, log-logistic converge |
| Cox–Snell residuals of the correct (Weibull) model, 240 rows | the largest \|Ĥ_KM(r) − r\| for r ≤ 2 is 0.124: no fixed threshold can be put on that chart without false alarms |
| A `flexsurvreg` object with the rows taken out (`data$Y` removed, the model frame and matrices emptied, `logliki` dropped) | `summary(newdata = …)` gives the same numbers; no element with one value per row is left; 70 KB → 39 KB |
| Times, 20,000 rows | Weibull 0.6 s, log-logistic 0.8 s, Gompertz 1.3 s, generalized gamma 8.6 s; the Richardson Hessian of a 5-parameter Weibull 3.0 s |

#### The recipe

`groups/survival/recipe.json` gains `flexsurv` and its closure, **from CRAN's index**: 34
packages, all from CRAN, so the recipe goes from 17 to **51** packages, in installing order (each
after its dependencies). The versions shared with the regression and time series recipes are the
same (`Rcpp` 1.1.2, `RcppArmadillo` 15.6.2-1, `numDeriv` 2016.8-1.1, `statmod` 1.5.2, `ggplot2`
4.0.4, `generics` 0.1.4…):

```json
"packages": ["assertthat@0.2.1", "bit@4.6.0", "bit64@4.8.6", "glue@1.8.1", "cli@3.6.6", "rlang@1.3.0",
  "lifecycle@1.0.5", "magrittr@2.0.5", "vctrs@0.7.3", "purrr@1.2.2", "R6@2.6.1", "withr@3.0.3",
  "tidyselect@1.2.1", "cpp11@0.5.5", "arrow@26.0.1", "jsonlite@2.0.0", "survival@3.8-12",
  "deSolve@1.42", "generics@0.1.4", "data.table@1.18.6.1", "RColorBrewer@1.1-3", "viridisLite@0.4.3",
  "mstate@0.3.3", "muhaz@1.2.6.5", "mvtnorm@1.4-2", "numDeriv@2016.8-1.1", "quadprog@1.5-8",
  "Rcpp@1.1.2", "bdsmatrix@1.3-7", "bbmle@1.0.26", "fastGHQuad@1.0.1", "lsoda@1.2",
  "RcppArmadillo@15.6.2-1", "rstpm2@1.7.1", "statmod@1.5.2", "utf8@1.2.6", "pillar@1.11.1",
  "pkgconfig@2.0.3", "tibble@3.3.1", "dplyr@1.2.1", "stringi@1.8.9", "stringr@1.6.0", "tidyr@1.3.2",
  "gtable@0.3.6", "isoband@0.3.0", "S7@1.0.0", "farver@2.1.2", "labeling@0.4.3", "scales@1.4.0",
  "ggplot2@4.0.4", "flexsurv@2.3.2"]
```

| Packages added | Licence (CRAN's index) | Why |
|---|---|---|
| `flexsurv` 2.3.2 | GPL (≥ 2) | the fits, the distributions' functions |
| `numDeriv` | GPL-2 | the Richardson Hessian, the gradients of the delta method and of dfbetas (already in the regression recipe) |
| `deSolve` 1.42, `mstate` 0.3.3, `muhaz` 1.2.6.5, `quadprog` 1.5-8, `Rcpp`, `RcppArmadillo`, `rstpm2` 1.7.1, `statmod`, `bbmle` 1.0.26 | GPL (≥ 2), GPL, GPL-2 \| GPL-3 | flexsurv's imports and theirs |
| `mvtnorm` 1.4-2; `bdsmatrix` 1.3-7 | GPL-2; LGPL-2 | `rstpm2`'s and flexsurv's imports |
| `data.table` 1.18.6.1 | MPL-2.0 | `mstate`'s import |
| `dplyr`, `tidyr`, `tibble`, `pillar`, `pkgconfig`, `stringr`, `generics`, `ggplot2`, `scales`, `gtable`, `isoband`, `S7`, `farver`, `labeling`, `viridisLite`, `fastGHQuad`, `lsoda` | MIT (`labeling`: MIT or Unlimited) | flexsurv's imports and theirs |
| `RColorBrewer`, `utf8` | Apache-2.0 | the same |
| `stringi` 1.8.9 | its own file: 3-clause BSD, with ICU's licence and a GPL (≥ 2) word-count notice (**read in the image by P0**) | `stringr`'s |

Every licence is open source; the GPL-2-only ones are as `numDeriv`, already in the regression
recipe: the packages are downloaded from CRAN and built in the administrator's own cluster, the
recipe names them, Tabularia ships no binary of them. flexsurv **imports** `ggplot2`, `dplyr`,
`tidyr`, `rstpm2`, `mstate`, `muhaz`, `deSolve`: they are installed because R loads a namespace's
imports, and the runner calls none of them.

**Weight.** The closure, installed from binaries, takes **74 MB** (`ggplot2` 12.5, `RcppArmadillo`
8.1, `stringi` 7.6, `Rcpp` 6.3, `data.table` 5.8, `rstpm2` 3.9, `flexsurv` 3.3 MB…: **verified in
R**): the image goes from 1.41 GB (`docker image inspect`, 1,408,161,895 bytes) to **about
1.48 GB** (+5 %). Built from source (CRAN has sources only), the size is about the same; **the
build is longer**: the package step of today's survival image took 305 s, 282 of them for `arrow`;
`stringi` (with its bundled ICU), `rstpm2` and `flexsurv` (C++ against Armadillo), `data.table`,
`dplyr` add an estimated **8 to 12 minutes** on this machine. P0 measures both and writes them
here. The largest compile unit is `rstpm2`'s, against Armadillo, which the time series group
already compiles for `forecast`: **assumed** within the build Job's memory.

**The rebuild.** The recipe's hash changes, so every installation rebuilds the group by itself
when Tabularia starts («Enabling a group»); until the new version is ready, Kaplan–Meier and Cox
steps fail with «the model group … is being rebuilt». No installation is in use, so nothing is
done to keep the old version running meanwhile. The runners of Kaplan–Meier and Cox do not change:
their reference values must come out of the new image unchanged (P0's acceptance).

#### The node

| Key | Kind | Default | Applies | Meaning |
|---|---|---|---|---|
| `time` | number column | — (needed) | always | The follow-up time, from the start, in any unit |
| `event` | column | — (needed) | always | The column that says whether the event happened |
| `event_value` | level of `event` | — (needed) | always | Its value for an event; every other value is censored at `time` |
| `predictors` | predictors, **none or more** (new kind `predictors_optional`: `columns`, `min` 0, `predictor`) | `[]` | always | Numbers, texts and booleans (categories with treatment contrasts); a date is refused with the regressions' sentence. Without predictors the node fits the distribution of the times (per stratum) |
| `reference` | reference | `{}` | predictors given | A categorical predictor's reference level |
| `interactions` | pairs of predictors | `[]` | predictors given | Products of two predictors, as Cox |
| `strata` | column | none | always | A baseline of its own per value, at most 20: the column enters the location (one coefficient per level) **and** the shape (one shape per level, flexsurv's `anc`; `sigma` of the generalized gamma, whose Q stays common; the exponential has no shape, so its strata enter the rate only) |
| `weights` | number column | none | always | Case weights, 0 or more; each multiplies its row's log-likelihood. Rows of weight 0 are left out (note `zero_weights`); weights that are not whole numbers get the note `weights_counts` (the standard errors read them as counts of rows) |
| `distribution` | choice `weibull` \| `exponential` \| `lognormal` \| `loglogistic` \| `gengamma` \| `gompertz` \| `auto` (new kind `distribution`) | `weibull` | always | The variant |
| `quantiles` | numbers in (0, 1), at most 9 (new kind `quantiles`: `numbers`, `above` 0, `below` 1, `max_count` = `LIMITS.max_quantiles`) | `[0.25, 0.75]` | always | The quantiles of the time reported for the profile and written by `rows` and Predict, **besides the median, always computed** (0.5 given is not repeated) |
| `times` | times (the existing kind, at most 10) | `[]` | always | S(t) reported at these times, for the profile, `rows` and Predict |
| `alpha` | alpha | 0.05 | always | Intervals at 1 − alpha |
| `seed` | seed | 1 | always | Only the sampling of chart points: the fit has no randomness |
| `output` | `results` \| `rows` | `results` | always | `rows`: the input's rows with the model's columns |

`disjoint`: `time`, `event`, `predictors`, `strata`, `weights`. Lot P8 adds `start` («Delayed
entry»). Every name stays a label: the runner renames the columns to positional ones before it
builds the frame flexsurv reads (`time`, `status`, `w`, and the design's numeric columns `d1`, `d2`…,
the strata's dummies `s1`, `s2`…), as Cox does; no column's name reaches a formula.

**Rows and types.**

- Left out, counted in `n_missing`: a row with NULL or NaN in `time`, `event`, a predictor, the
  stratum or the weight. Nothing is imputed. An infinite value stops the node and names the column.
- **`time`**: a number column (a date is refused with Cox's sentence, `time_type`); a negative time
  is refused (`negative_time`, Cox's sentence). **A time of 0** has no density under any of the six
  distributions (flexsurv and survreg refuse it, **verified in R**): such rows are left out,
  counted in `n_zero_time`, with the note `zero_times` («… rows have a time of 0 and are left out:
  a parametric distribution gives an event at 0 no density; if those events are real, count time
  from a moment before them»).
- **Refusals before the fit** (exit 3, `error.json`, the line `TABULARIA_ERROR {json}`, a sentence
  that says what to do): `missing_column`, `time_type`, `negative_time`, `no_rows`, `no_events`
  («the column … is never «…» in the rows the model uses: with no event there is nothing to fit;
  check event_value», Cox's), `too_many_strata`, **`stratum_no_events`** («the stratum «plant=B»
  has no event: its shape cannot be estimated; merge it with another, or leave its rows out»),
  `too_few_rows` (rows fewer than the parameters + 2), `aliased` (Cox's QR check on the design with
  a column of ones), the design's own (`date_predictor`, `too_many_levels`, `one_level`,
  `reference_level`, `infinite`), `output_column_exists`; after the fit, **`not_converged`** («the
  fit of the … distribution did not converge from two starting points: fewer predictors, merge rare
  levels, or another distribution») and, with `auto`, **`no_candidate`** («no distribution could be
  fitted to these rows: …»).

#### The catalogue

`backend/app/engine/models/catalog.py`: `GROUPS.survival.models` gains `parametric_survival` (after
`cox`); `MODELS.parametric_survival` (family `survival`) with `needs` `time`, `event`,
`event_value` and `takes` the keys above; new kinds `predictors_optional`, `distribution`,
`quantiles`; `LIMITS.max_quantiles` (9); `SECTION_SHAPES.parameter` (`level` required, `level_2`
optional, not indexed); `PREDICT` as in «Predict» below. **Four variants**, keyed on
`distribution`, because what the runner writes differs:

| Variant | `when` | `coef` holds | Checks |
|---|---|---|---|
| `ph_aft` | `weibull`, `exponential` | the estimates, time ratios, hazard ratios and Cox's | every check of «The checks» (`constant_hazard` written for the exponential only) |
| `aft` | `lognormal`, `loglogistic`, `gengamma` | the estimates, time ratios | no `ph_cox`, `ph_cox_global`, `constant_hazard` (`distribution_vs_gengamma` for the log-normal only) |
| `ph` | `gompertz` | the estimates, hazard ratios and Cox's | no `distribution_vs_gengamma`, `constant_hazard` |
| `auto` | `auto` | the union of the three (the chosen distribution decides at run time) | the union |

A variant's declaration is what the runner **may** write (`results.py` refuses anything else); a
check that does not apply to the distribution fitted is not written at all, never as «not
computed» (the log-logistic, in `aft`, writes no `distribution_vs_gengamma`). `applies`:
`reference` and `interactions` only with `predictors` given. Snapshot, the three copies,
`model_catalog.py --check`, `params_cases.json` (the Go and the engine checks run the same cases),
as for every model.

#### The fit

- **The call**: `flexsurv::flexsurvreg(Surv(time, status) ~ d1 + … + dk + s1 + …, anc = <the
  shape's formula on s1 + …, with strata>, data = <the positional frame>, weights = w, dist = <the
  table's>, control = list(reltol = 1e-15, maxit = 2000))`. The tolerance is not flexsurv's default:
  with the default (1e-8) a Gompertz coefficient stops 7.5e-6 away from the maximum (**verified in
  R**), which the reference values at 1e-7 would not hold.
- **Starts**: flexsurv's own (for the Weibull, `survreg`'s estimates). The **generalized gamma**
  starts from the best, by log-likelihood, of three points: flexsurv's own (μ the mean of log t, σ
  its deviation, Q = 0), the Weibull fit (μ = log scale, σ = 1/shape, Q = 1, the same AFT
  coefficients) and the log-normal fit (μ = meanlog, σ = sdlog, Q = 0): so its log-likelihood is
  never below the two models it nests, which the check `distribution_vs_gengamma` needs.
- **A second attempt** when flexsurvreg stops with an error or `opt$convergence ≠ 0`: from the next
  start (generalized gamma), from the exponential's rate and shape 0 (Gompertz), from `survreg`'s
  estimates (the four others), with `maxit = 5000`; the note `second_start` says so. Still not
  converged: the refusal `not_converged` (with `auto`, the candidate's status).
- **The covariance** is the inverse of `numDeriv::hessian` of the runner's own log-likelihood
  (Σ wᵢ (δᵢ log h(tᵢ) − H(tᵢ)), from flexsurv's `h<dist>` and `H<dist>`, checked equal to
  flexsurv's at the estimates in every fit, 1e-10 relative, a failed check stops with a bug's exit)
  on the optimiser's scale (flexsurv's `opt$par`: the distribution's parameters transformed, then
  the location's coefficients, then the shape's). flexsurv's own covariance is not used: «The
  package». A Hessian that is not positive definite leaves every standard error and interval NULL,
  and the check `convergence` fails.
- **The null fit**, for the likelihood-ratio test of the predictors: the same distribution with the
  strata only.
- **Determinism**: no random choice in the fit (`optim`'s BFGS, `numDeriv`'s Richardson steps); the
  seed only samples chart points.

#### Coefficients

The `coef` section has one row per term of the design plus `(Intercept)`, the location parameter
at every predictor 0 and every category at its reference level, in the stratum of reference:

| Stat | Distributions | What |
|---|---|---|
| `estimate`, `std_error`, `statistic` (z), `p_value`, `significance` | all | the coefficient as fitted, on its scale: **log time ratio** for the AFT distributions (for the exponential, −γ, so that every AFT `estimate` reads the same way: positive lengthens the time), **log hazard ratio** for Gompertz; the `model` section's text `coefficient_scale` says which (`log_time_ratio`, `log_hazard_ratio`) |
| `time_ratio`, `tr_ci_low`, `tr_ci_high` | Weibull, exponential, log-normal, log-logistic, generalized gamma | exp(estimate), the interval exp(estimate ± z · se); not for `(Intercept)` |
| `hazard_ratio`, `hr_ci_low`, `hr_ci_high` | exponential; Gompertz; **Weibull without strata** | exponential exp(γ); Gompertz exp(β); Weibull exp(−β · shape) = exp(−β/σ), its interval by the delta method on log HR = −β/σ (equal to the PH parametrization's Wald interval, **verified in R**); not for `(Intercept)` |
| `cox_hazard_ratio`, `cox_hr_ci_low`, `cox_hr_ci_high` | where `hazard_ratio` is written | the Cox node's estimate on the same rows (`coxph`, Efron's ties, the same strata as Cox strata), for the comparison the owner asked |
| `vif` | all, terms of one or more columns | as Cox (`cox_vif`, GVIF^(1/df)) |

- **Weibull with strata has no hazard ratio.** With a shape per stratum, the hazard ratio of a
  predictor is exp(−β · shape_s), different in each stratum, while its time ratio is common: the
  report says so in one sentence (note `strata_no_hr`) and gives the time ratios. The exponential
  (no shape) and Gompertz (its predictors on the rate) keep one hazard ratio across strata.
- **One p-value per coefficient**, the `estimate`'s. For the Weibull the hazard ratio's interval is
  Wald's on its own scale, so near the edge it can exclude 1 while p is just above alpha: the note
  `hr_interval_scale` says so.

**The distribution's parameters**, a new section `parameter` (`level` the parameter's flexsurv
name, required; `level_2` the stratum, optional; not indexed): for each parameter that is not the
location (`shape`, `sdlog`, `sigma`, `Q`), and per stratum when there are strata: `estimate`,
`std_error`, `ci_low`, `ci_high` (a positive parameter's interval from the log scale, exp(log θ ±
z · se); Q and Gompertz's shape on their own scale). The location is in `coef` (`(Intercept)`) and,
in time units, in the profile.

#### The output

| Section | Rows |
|---|---|
| `model` | texts `model`, `variant`, `formula` («Surv(days, status = failed) ~ load + line + strata(plant)», as Cox's), `software` («R 4.6.0; stats 4.6.0; survival 3.8-12; flexsurv 2.3.2; numDeriv 2016.8-1.1»), `group_version`, `rows_from`, `note_*`, `event_value`, `distribution` (the one fitted, also with `auto`), `selection` (`manual` \| `auto`), `coefficient_scale`, `profile` (the profile in one line, «load = 52.3 (mean); line = A (reference)»), `lr_significance`, `exponential_lr_significance`; numbers `n`, `events`, `censored`, `n_missing`, `n_zero_time`, `n_strata`, `n_parameters`, `log_lik`, `aic`, `bic` (−2ℓ + k · log n, n the rows used, as flexsurv's `BIC` with `cens = TRUE`: **verified in R**), `lr_statistic`, `lr_df`, `lr_p_value` (against the null fit), `exponential_lr_statistic`, `exponential_lr_df`, `exponential_lr_p_value` (Weibull and Gompertz: the shape against the exponential's constant hazard, df = the shapes), `concordance`, `concordance_se` (Harrell's C of the location's linear predictor, sign turned for Gompertz, within strata: `survival::concordance`), `last_time`, `last_event_time` |
| `coef` | above |
| `parameter` | above |
| `stratum` | **the profile**, one row group per stratum (`all` without strata): `n`, `events`, `last_time`, `median`, `median_ci_low`, `median_ci_high`, `quantile_<p>` with `_ci_low`, `_ci_high` (`quantile_0.25`: p written as `km_time_label` writes a number, so that Predict's column names match the gateway's `timeLabel`), `surv_at_<t>` with `_low`, `_high` (Kaplan–Meier's names, `x0` = t), `km_median` (the stratum's Kaplan–Meier median, for comparison) |
| `candidate` | `distribution = auto` only: `level` the distribution; `n_parameters`, `log_lik`, `aic`, `bic`, `delta_aic`, `akaike_weight`, `converged`, `admissible`, `chosen` (1 or 0); text `status` (`ok`, `not_converged`, `hessian`, `time`) |
| `assumption` | the checks, below |
| `curve`, `point` | the charts' points, below |

**The profile** is a row with every numeric predictor at its weighted mean over the rows used and
every category at its reference level (interactions computed from those), one per stratum: what the
`stratum` section, the profile's charts and the first page's median describe. A median or quantile
the distribution never reaches (Gompertz with a falling hazard: a share of the rows never has the
event, **verified in R**) is NULL, with the note `quantile_infinite` («the fitted distribution
leaves …% of the profile without the event for ever: a quantile beyond that is infinite and left
empty»).

**Intervals of what is predicted** (the profile here, the rows in `rows` and Predict): the delta
method with the gradient of the quantity with respect to the optimiser's parameters by
`numDeriv::jacobian` (vectorized over the rows, by chunks of 100,000) and the covariance above; a
quantile's interval on log time, exp(log q ± z · se); a survival's on log(−log S), the `log-log`
interval of Kaplan–Meier. Deterministic; checked against `predict(survreg, type = "uquantile",
se.fit = TRUE)` («Reference values»).

`outputs.rows`: `pred_linear` (the location's linear predictor, strata included), `pred_median`,
`pred_quantile_<p>` (one per `quantiles`), `pred_surv_at_<t>` (one per `times`), and on the rows of
the fit `pred_cox_snell` (rᵢ = H(tᵢ | xᵢ)), `pred_martingale` (δᵢ − rᵢ), `pred_deviance`
(sign(m) · √(−2 (m + δ log(δ − m)))). Their names carry the numbers of the node's options: the form
cannot check them against the input, so a clash is the runner's refusal at run time
(`output_column_exists`), as the boosting group's `pred_prob_*`.

#### Automatic choice (`distribution = auto`)

1. The candidates are the six, fitted in order of cost: exponential, Weibull, Gompertz,
   log-logistic, log-normal, generalized gamma (started from the best of its three starts, two of
   which are fits just made). Each with the same predictors, strata and weights; each with its own
   second start.
2. A candidate is **admissible** when it converged and its Hessian (flexsurv's, cheap: only to
   tell; the Richardson one is computed for the chosen fit only) is positive definite. A candidate
   that is not stays in the table with its status and, when finite, its log-likelihood and AIC, and
   is **out of the choice and of the Akaike weights**; the note `auto_not_converged` names it.
3. The chosen one is the admissible candidate with the **lowest AIC**; on a tie, the one with fewer
   parameters, then the order above. **Parsimony (decided by the owner on 2026-11-20)**: if a
   simpler distribution nested in that candidate (the exponential in the Weibull, the Weibull and
   the log-normal in the generalized gamma, the exponential and the Weibull through it) is
   admissible and within 2 of its AIC, the simplest such distribution is chosen instead; the
   model's `selection` stays `auto`, the `candidate` section's `chosen` marks the one chosen and
   its text `status` stays as it is, and the new stat `parsimony` (1 on the chosen one when the
   rule moved the choice away from the lowest AIC, 0 otherwise) lets the report mark the row.
   The report says it in one sentence («The generalized gamma has the lowest AIC, but Weibull is
   within 2 of it and has fewer parameters: Weibull is chosen»). ΔAIC stays against the lowest
   AIC. ΔAICᵢ = AICᵢ − AIC_min; the weights wᵢ = exp(−ΔAICᵢ/2) / Σⱼ
   exp(−ΔAICⱼ/2) over the admissible ones.
4. **None admissible**: the refusal `no_candidate`.
5. **Time**: before each candidate, when more than half of the step's `time_budget_seconds` has
   passed, the remaining ones are not fitted (status `time`, note `auto_time`, which names them);
   the chosen fit's Hessian, checks and charts take the rest.
6. Then the chosen fit is the node's fit: every section above is written for it, and the
   `candidate` section beside.

The note `auto_close` names every other admissible candidate within 2 of the lowest AIC («Weibull
is within 2 of the AIC of the generalized gamma (ΔAIC 0.30, weight 0.40): it describes these times
about as well, with one parameter fewer»). On the 240-row table drawn from a Weibull, the
generalized gamma had the lowest AIC (1344.915 against 1345.220; weights 0.47 and 0.40, then
log-logistic 0.12; **verified in R**): with the parsimony rule the Weibull is chosen, and this
table is a **reference case of lot P3** (`ps_auto_parsimony`: the choice, the weights, the mark).

#### The checks

Section `assumption`. **Decided 2026-11-20 (owner): «niente cose hardcoddate».** A **verdict**
(`ok`, `failed`, `not_computed`; `h0`, `advice`, `advice_id`, `threshold`) comes only from a **test
with a p-value**, compared with the node's **`alpha`** (the node's option, default 0.05, which is also
the level of every interval; the report writes it): `p ≥ alpha` is «ok», `p < alpha` is «failed»,
and `threshold` carries alpha. There is no «doubtful» and no constant of ours between the two. The
other verdicts are **facts**: `convergence` (a positive definite Hessian) and `separation` (a level
with no event), which stay warnings. **Every other measure is numbers and a sentence** (stat
`description`), with **no verdict** and no advice: the agreement with Kaplan–Meier, the influential
rows, events per parameter, the VIF, the extrapolation; Cox–Snell is a chart only. The report
recognises the measures by their ids. Built so that a false alarm cannot come from a rule of ours:

- Cox–Snell has **a chart and no verdict**.
- The shape's test against the exponential is **a number of the fit, not a check**, for the
  distributions that have a shape; it is a check (`constant_hazard`) only for the exponential.
- Proportional hazards is checked only where the distribution assumes it.
- `distribution_vs_gengamma` only where the distribution is nested in the generalized gamma.
- The agreement with Kaplan–Meier is a measure: the largest gap in survival, where it is reached, and
  the gap in Greenwood's standard errors there; Kaplan–Meier's points with fewer than 10 rows at risk
  are not read (a rule of what is read, not of a verdict).
- Separation is read from the data (a level with no events), not from a large standard error.
- With a large table the tests reject small departures: the report says so once, whatever the size.

Light by construction (no refit per row, no bootstrap, no simulation): the influence is `V · Uᵢ` with
`Uᵢ` the row's score from central differences of the vectorized row log-likelihood (two evaluations
of all rows at once per parameter, one matrix product); curves on a grid of 100 times, the agreement
read at 60 of Kaplan–Meier's times at most; points capped by `max_chart_points`. The only fits besides
the node's own are those of the two likelihood-ratio checks, not made once half the step's
`time_budget_seconds` has gone.

| Check id | Distributions | What is computed | Verdict | Written |
|---|---|---|---|---|
| `convergence` | all | the Hessian positive definite; `value` the Newton step left at the estimates, in standard errors | ok · failed (intervals NULL) | verdict, advice |
| `separation` | with a categorical predictor | one row per level (`level_2` «column=value») among whose rows no event happens; one ok row when there is none | ok · failed | verdict, advice |
| `distribution_vs_gengamma` | Weibull (df 1), log-normal (df 1), exponential (df 2; with strata the strata's σ too) | LR = 2 (ℓ_gengamma − ℓ), the generalized gamma fitted with the same predictors and strata, both at their maxima; not computed when it does not converge or the time is up | p ≥ alpha ok · p < alpha failed | statistic, df, p, threshold = alpha |
| `constant_hazard` | exponential | LR of the Weibull against it (df 1, or the strata) | as above | as above |
| `ph_cox`, `ph_cox_global` | Weibull, exponential, Gompertz, with predictors | Grambsch–Therneau (`cox.zph`, `transform = "km"`) of the Cox fit on the same predictors, weights and strata, per term (`level_2`) and global | as above | as above |
| `km_agreement` | all | per group (`level_2`: the stratum; without strata the level of the first categorical predictor when it has at most 6; else «all»): `value` the largest \|S̄_g(t) − KM_g(t)\| over the event times with ≥ 10 rows at risk, `at` its time, `statistic` the gap in Greenwood's standard errors there, `count` the group's rows | none | description |
| `dfbeta` | all, with predictors | per term (`level_2`): `value` the largest \|dfbetas\| of any row, from V · Uᵢ (unweighted, as `survreg`'s); `rows` the five most influential | none | description |
| `epv` | all | `value` events per parameter, `count` events, `df` parameters | none | description |
| `vif` | all, with two terms or more | as Cox: GVIF^(1/df), per term | none | description |
| `extrapolation` | all | per stratum: `value` the largest of the node's `times` and of the profile's median and quantiles over the stratum's last observed time, `at` that time | none | description |

The model section gains the number `alpha`.

#### The report

`backend/app/engine/models/layouts/parametric_survival.py`, words in `report_texts/
parametric_survival.json` (five languages), with `common.page`; A4 portrait as every model.

1. **First page**: the header band; four cards: n and events; the distribution («Weibull»; with
   `auto` «Weibull · auto, weight 0.40»); the profile's median with its interval (with strata: the
   concordance); the AIC. The formula line with the distribution and its scale («Surv(days, status =
   failed) ~ load + line · Weibull, accelerated failure time»). The checks and their advice.
2. **Coefficients**: the estimates (estimate, standard error, z, p, «Signif.» and R's legend); by
   distribution, the time ratios with their intervals and/or the hazard ratios with theirs and Cox's
   beside; the interval chart of each (`interval`, `options.log`, a line at 1); the sentence that
   Gompertz has no time ratio, or that a Weibull with strata has no hazard ratio, where it applies;
   the **distribution's parameters** (`parameter`), per stratum.
3. **Fit**: every number of the `model` section: log-likelihood, AIC, BIC, the test of the
   predictors, the shape against the exponential (Weibull, Gompertz), the concordance, the last
   observed times, the rows left out.
4. **Choice of the distribution** (`auto` only): the candidates sorted by AIC (distribution,
   parameters, log-likelihood, AIC, BIC, ΔAIC, Akaike weight, status, the chosen one marked), the
   sentence «Weibull has the lowest AIC of 6 candidates» and the `auto_close` note; a chart of the
   Kaplan–Meier curve with the mean fitted curve of every admissible candidate.
5. **Fit to the data**, under `km_agreement`: Kaplan–Meier (steps, with its band) against the mean
   fitted curve, per group; the Cox–Snell chart (the Nelson–Aalen cumulative hazard of the
   residuals against them, the diagonal); for the Weibull and the exponential, log(−log S) against
   log t: Kaplan–Meier's points and the fitted lines (parallel straight lines under a Weibull with
   proportional hazards), per group.
6. **The profile**: its line; the table per stratum of the median, the quantiles and S(t) with
   their intervals, Kaplan–Meier's median beside; the survival curve with its band, the node's first
   four times as vertical lines; the hazard h(t) with its band (delta method on log h), up to the
   last observed time.
7. **Influence**, under `dfbeta`: deviance residuals against the linear predictor, the largest
   named.
8. **Notes**: the `note_*` rows, the rows left out, the software, the group version.

Excel: the checks, the coefficients, the parameters, the fit, the candidates, the profile.

Chart ids (the runner's `level`, the layout's `under`): `fit_km` (`curve`: `km` with `surv`,
`lower`, `upper`; `fitted` with `surv`; `level_2` the group) under `km_agreement`; `cox_snell`
(`point`) under `km_agreement`; `loglog` (`point` from Kaplan–Meier, `curve` fitted) under
`km_agreement`; `candidates` (`curve`, `level_2` the distribution or `km`); `profile_survival` and
`hazard` (`curve`: `surv` or `hazard` with `lower`, `upper`; `level_2` the stratum); `deviance_linear`
(`point`) under `dfbeta`. At most `max_chart_points` points a chart, the extremes kept (`tab_cap`);
the curves at 100 times from 0 to the last observed time (`tab_thin_steps` for Kaplan–Meier's).

**Nothing new in the chart engine**: `xy` with `step`, `line`, `point`, `band_low`/`band_high`,
`split_by`, `options.diagonal`, `options.target_x` (at most 4: `MAX_XY_TARGETS_X`), `min`/`max`, and
`interval` with `options.log` exist (**verified** in `backend/app/reports/definition.py`).
**Assumed**: an `xy` with `split_by` draws a step series and a line series for each group (the
Kaplan–Meier step against the fitted line); P5 checks it on its first day, and if it does not, the
layout draws one chart per group, at most 6.

#### The card, MLflow and Predict

**The card** (`model.json`, the common keys: `inputs` with the predictors and, as Cox does, the
strata's column as a category with its levels; `terms`; `coefficients` = the optimiser's whole
vector; `vcov` its covariance, up to `max_vcov_terms`) adds a `parametric_survival` object, declared
in `card.schema.json`:

```json
"parametric_survival": {
  "distribution": "weibull", "selection": "auto", "coefficient_scale": "log_time_ratio",
  "parameters": ["shape", "scale"], "location": "scale", "transforms": ["log", "log"],
  "order": [ {"role": "parameter", "name": "shape"}, {"role": "parameter", "name": "scale"},
             {"role": "location", "term": 1}, {"role": "location", "term": 2},
             {"role": "location", "stratum": "B"}, {"role": "anc", "name": "shape", "stratum": "B"} ],
  "strata": {"column": "plant", "levels": ["A", "B"]},
  "time_column": "days", "event_column": "status", "event_value": "failed",
  "last_time": 59.8, "last_time_by_stratum": {"A": 59.8, "B": 41.2},
  "quantiles": [0.25, 0.75], "times": [30, 90], "alpha": 0.05, "flexsurv": "2.3.2" }
```

`order` names each element of `coefficients` (the location's terms by their position in `terms`,
from 1); its size is the number of parameters, a few dozen at most, and the card holds **no row**:
it is all Predict needs. **The computation is the runner's, in R**, as for Cox and the linear model
(there is no prediction in Go): the design of the new rows from `inputs` (`tab_design_rows`), the
location from the linear predictor, the shape from the stratum, flexsurv's `p`, `q`, `h` functions
of the card's distribution, the delta method with `vcov` for the intervals. The gateway reads, of
this card, what it reads of every card (`format`, `model`, `variant`, `group`, `inputs`);
`predictCard.columns()`, the columns a prediction may not overwrite, gains nothing: the rows to
predict need no time column (a new unit has no time yet). The tag `tabularia.card_sha256` is as today. The self-test checks, inside the
fit when `group_version` is `selftest`, that the card gives back flexsurv's own predictions of the
rows of the fit (`summary.flexsurvreg(newdata, type = "quantile" | "survival")`) within 1e-9.

**`native.rds`** (with MLflow, for people): the `flexsurvreg` object with the rows taken out
(`data$Y` removed, the model frame and the matrices emptied, `logliki` dropped: **verified in R**
that `summary(newdata = …)` still works and that no per-row element is left); `tab_native_lean`
gains the class `flexsurvreg`; the self-test's «holds no row of the input» covers it.

**MLflow**: the parameters, the metrics of the `model` section and of the checks, the artifacts,
the version, as every model. Added for every model, in the gateway (`jobs_models_mlflow.go`): the
sections `candidate` and `parameter` are read back with the others, and logged as metrics
`candidate.<level>.<stat>` (`aic`, `bic`, `log_lik`, `akaike_weight`; ARIMA's candidates get
`aicc`, `aic`, `bic` the same way) and `parameter.<level>[.<level_2>].estimate`; the `model`
section's text `distribution` is logged as the parameter `distribution_chosen` (with `auto`, the
node's parameter says only `auto`). `mainMetrics` (`models_mlflow.go`): `parametric_survival` →
`concordance`, then `aic`.

**Predict** (`PREDICT` in the catalogue):

| Key | Applies to | Default | What |
|---|---|---|---|
| `quantiles` (new key, kind `quantiles`) | `parametric_survival` | the card's | the quantiles besides the median |
| `at_times` | `cox`, **`parametric_survival`** | the card's `times` | S(t) at these times |
| `interval` | `linear`, `kaplan_meier`, `arima`, **`parametric_survival`** | false | the intervals at the card's 1 − alpha |
| `alive_at` (P9, kind `number_column`) | `parametric_survival` | none | conditional survival, below |

Columns (`PREDICT.columns.parametric_survival`: `always` `prediction`; `quantiles` →
`quantile_*`; `at_times` → `survival_at_*`; `interval` → `prediction_low`, `prediction_high`):
`prediction` = **the median time**; `quantile_<p>` per quantile; `survival_at_<t>` per time
(Cox's names); with `interval`, `prediction_low`, `prediction_high` and `<column>_low`,
`<column>_high` for each quantile and time. The gateway's `PredictColumns` names the ones it can
from the options given; the card's defaults and the `_low`/`_high` columns it cannot name are the
runner's refusal on a clash, as for the boosting group's classes. Unlike Kaplan–Meier and Cox, a
time past the fit's last observed time **is predicted** (the distribution's shape gives it): the
rows whose median, a quantile or a time is beyond `last_time` are counted in a `run.json` warning
(«… rows are predicted beyond the last time observed when fitting (59.8): an extrapolation»)
(«Open questions for the owner», 25). A median the distribution never reaches is NULL. A level not
seen when fitting, a missing predictor or stratum: NULL, counted, as for the regressions. With
`interval` and a card without `vcov` (above 200 parameters): no interval column, a warning, as the
penalized linear model.

**Conditional survival** (`alive_at`, lot P9): a number column of the rows to predict, the time
each unit has already survived without the event («this machine has run 300 days»). Then the
answers are about the **time left**: `prediction` = the median remaining time, q such that
S(a + q) = ½ S(a); `quantile_<p>` likewise; `survival_at_<t>` = S(a + t) / S(a), the probability of
lasting t more. A NULL or negative `alive_at` gives NULL on the row; an `alive_at` beyond the last
observed time is counted in the extrapolation warning. The intervals by the same delta method. The
Predict form gains a column choice for it (a numeric column of the input). Kaplan–Meier and Cox do
not take it (their curves stop at the last time observed).

#### Delayed entry (`start`, lot P8)

`start` (number column, Cox's key): rows observed from `start` to `time`, the subject not seen
before `start` (entry into a study, a machine bought second-hand): `Surv(start, time, status)`,
which flexsurv reads as left truncation and, with several rows per subject, as start–stop rows
with time-varying predictors (each row adds δ log h(stop) − (H(stop) − H(start))). Refused as Cox
refuses (`start_not_before_time`). Kaplan–Meier's curves of the checks and charts become
`survfit(Surv(start, time, status))`; Cox–Snell's residuals are the pairs (H(start), H(time)) read
by the Nelson–Aalen estimator with entry; `n` counts rows, the note `start_rows` says that rows of
one subject are taken as independent (no robust errors: «Not in this node»). Lot P8 builds it after
the rest; it does not delay the first lots. **Built: «As built, P8».**

**`start` is not `alive_at`.** Both are a time a unit had already lasted, and they do not meet:

| | `start` (the node's key, lot P8) | `alive_at` (the node's and Predict's key, lot P9) |
|---|---|---|
| What it is | where each row of the **fit** begins to be observed: the subject was not seen before it | how long each unit **to predict** has already lasted: the question is asked at that age |
| Where it acts | the likelihood: each row is conditional on lasting until its start (−log S(start)); the parameters change | the prediction: S(a + t)/S(a), the time left; the parameters are the fit's |
| In Predict | **absent**: the card holds the parameters, in which the entry is already; its `start_column` names the column for the record | a column of the rows to predict |
| Rows without it | observed from 0 (a start of 0 is the fit without a start) | predicted from 0 |

A model fitted with a start, asked with `alive_at`, gives the time left of units of that age under
the distribution the truncated data estimated.

#### Not in this node

Left and interval censoring («Open questions for the owner», 24); robust or clustered standard
errors (flexsurv has none; with start–stop rows the note says the rows are taken as independent);
cure models, splines (Royston–Parmar), competing risks; the gamma and generalized F distributions;
a profile chosen by the person (Predict gives any profile).

#### Reference values

Small synthetic tables in `groups/survival/reference/data/` (fixed seeds, tens to a few hundred
rows), cases in `expected.json` written by `make_reference.R` **outside Tabularia**, in the group's
image, with flexsurv and survival called directly:

| Case | Data | What it checks | Tolerance and why |
|---|---|---|---|
| `ps_exponential_exact` | 60 rows, no predictors | rate = events / Σ time, se(log rate) = 1/√events, ℓ = d log λ − λ Σt, the median log 2 / λ: **closed forms** | 1e-10 relative (the optimiser's stop; 2.4e-11 measured) |
| `ps_exponential_groups` | 90 rows, one category of three levels | each level's rate d_g / T_g, the hazard ratios (d_b/T_b)/(d_a/T_a), exact | 1e-9 |
| `ps_weibull` | 240 rows: a number, a category, an interaction, case weights, rows with a NULL | every number against `flexsurvreg` with the same call; coefficients, ℓ, standard errors and the profile's median against `survreg` | 1e-9 against flexsurv (same code); **1e-7** relative coefficients and ℓ against survreg (two optimisers: 2e-10 measured), **1e-6** standard errors (a numerical Hessian against survreg's analytic one: 7.6e-9 measured) |
| `ps_weibull_strata` | 240 rows, three strata | against `survreg(… + strata(g))`: ℓ, the scale per stratum, the coefficients; no hazard ratio written | as above |
| `ps_lognormal`, `ps_loglogistic` | the 240 rows | as `ps_weibull` | as above |
| `ps_gengamma` | the 240 rows | against flexsurv; ℓ ≥ max(ℓ_Weibull, ℓ_lognormal); `distribution_vs_gengamma` of the Weibull and the log-normal | 1e-9; the inequality exact |
| `ps_gompertz` | 200 rows drawn from a Gompertz | against flexsurv; and a **second construction**: the Gompertz log-likelihood written by hand in `make_reference.R`, maximized by `stats::nlm` | 1e-9; 1e-6 against the second construction |
| `ps_gompertz_cure` | 300 rows, falling hazard | S(∞) = exp(rate/shape); the median NULL, the note | 1e-9 |
| `ps_auto` | the 240 rows | each candidate's ℓ, AIC, BIC against its own flexsurvreg fit; ΔAIC, weights; the choice | 1e-9; the choice exact |
| `ps_auto_failing` | a table where a candidate does not converge (P3 looks for one among small tables; if none of the six fails on any, a self-test hook forces a failure, so the path is tested) | the status, the note, the weights without it | exact |
| `ps_separation` | a level with no events | `separation` failed; the fit goes on | exact |
| `ps_all_events` | no censoring | fits | 1e-9 |
| refusals | no events; a negative time; a stratum without events; too few rows; one level | the sentences | exact |

Predictions and intervals, in the self-test: the card's quantiles and S(t) of the fitted rows
against `summary.flexsurvreg(newdata)` (1e-9; equal in the check of «The package»); the delta
method's standard error of the log median against `predict(survreg, type = "uquantile", se.fit =
TRUE)`, Weibull and log-normal (1e-6; 4.3e-9 measured); conditional survival against S(a + t) /
S(a) written by hand (1e-12); dfbetas against `residuals(survreg, type = "dfbetas")` (1e-6; 5.2e-9
measured). **Witness**: `make_witness.py` with the survival group's pinned `lifelines` 0.30.3
(`WeibullAFTFitter`, `LogNormalAFTFitter`, `LogLogisticAFTFitter`,
`GeneralizedGammaRegressionFitter`), at 1e-5 relative (its own optimiser); **assumed**: those four
classes and parametrizations in 0.30.3, P1 checks them and writes what differs. Gompertz has no
second implementation outside R: its witness is the hand-written likelihood above.

#### Performance and limits

The group's limits (1,000,000 rows, 4 GiB, its longest run) and the catalogue's (200 terms, 50
levels, 20 strata, 10 times) hold; the catalogue's `LIMITS` gains `max_quantiles` (9). The
generalized gamma dominates: 8.6 s on 20,000 rows against 0.6 s for the Weibull (**verified in R**),
so `auto` on a million rows takes minutes: the time rule of «Automatic choice» keeps it within the
step's timeout, and the Richardson Hessian is computed once, for the chosen fit. P11 measures the
durations on kind at 100,000 generated rows and writes them here; they are not promised.

#### Assistants and documentation

- **Assistants**: the catalogue's copies carry the node; a paragraph of the flow assistant's prompt:
  choose *Parametric survival* over Cox when times must be predicted (a median, a quantile, the
  survival at a time, beyond the data) or the hazard's shape matters, Cox when only the effects are
  asked; `distribution = auto` when the person does not know which; always `event_value`; never a
  predictor recorded after the event. The data assistant's: time ratios multiply the time, hazard
  ratios the rate; extrapolations come from the distribution's shape. `prompts.json` and
  `tools.json` written again by their generators.
- **Documentation**: `docs/capabilities/models.md` gains «Parametric survival» after Cox: the
  distributions and when each fits, time ratios against hazard ratios, Gompertz, strata, the
  automatic choice and how to read the weights, the checks, the profile, Predict's columns,
  `alive_at`, an example on the pasta maker's sample data (how long until a machine of the line
  fails?); the Predict table, the MLflow page (the candidates' metrics), «Where Tabularia and R
  differ» (the covariance from a Richardson Hessian, not flexsurv's; the exponential's coefficients
  as AFT; time 0 left out); the capabilities map, `README.md`, `CHANGELOG.md`. No competitor
  named.

### Not in the first release

- Python groups for the more complex models beyond `boosting` (owner: later releases); multinomial and
  ordinal logistic regression; mixed models, generalized additive models, Poisson and
  negative binomial regression, quantile regression, parametric survival (Weibull and the
  others: planned on 2026-11-20 as a node of its own, «The node «Parametric survival»»),
  competing risks; exponential smoothing; Fourier terms for long seasons.
- Inference after a penalized selection.
- A fit and its prediction within one flow without MLflow.

## The output table

The ten columns of `docs/specs/statistics.md` («The output table»), the same names, types
and order; `ord` 0 to N−1; `key` unique; a number that is not finite is NULL.

| Section | `level` | `level_2` | Holds |
|---|---|---|---|
| `model` | null | null | Texts: `model`, `variant`, `formula` (in symbols, with the columns' names), `family`, `link`, `se_type`, `software` (`R 4.6.x; stats 4.6.x; sandwich …` from the image's lock), `group_version`, `rows_from` (`trial` or `run`), `note_<id>` (an English sentence a row; the id names the doubt: `note_firth`, `note_edge_small`…; the statistics' `note_1` numbering is not used). Numbers: the «Fit» of the model, in the catalogue's order |
| `coef` | term | the category's level (null for a number) | The «Coefficients» of the model and variant, then `significance` after `p_value` |
| `cv` | null | null | Penalized only: one row group per λ of the path: `x0` = log λ, `stat` = `cv_mean`, `cv_low`, `cv_high`, `n_selected` |
| `candidate` | the model's name, «ARIMA(2,1,1)(0,1,1)[12]» | null | ARIMA with `orders = auto`: `aicc`, `aic`, `bic`, `converged`, `chosen` |
| `assumption` | check id (`breusch_pagan`, `schoenfeld_global`, `ljung_box_10`…) | term or null | Texts `h0`, `verdict` (`ok`, `doubtful`, `failed`, `not_computed`), `advice`, `rows` (the row numbers a check names, comma-separated); numbers `statistic`, `df`, `df_2` (an F test's second df), `p_value`, `significance`, `threshold`, `value` (a VIF, an EPV), `count` (the rows over a threshold). One check can have several rows of `level_2` (a term, a stratum) |
| `stratum` | stratum | null | Kaplan–Meier, per stratum |
| `pair` | first stratum | second | Pairwise log-rank: `statistic`, `p_value`, `p_adjusted`, `significance` |
| `cell` | observed class | predicted class | Confusion matrices: `observed` |
| `point` | chart id | series | Points of a chart: `x0` = x, `value` = y, `text` = a label (a row number) when the point is named |
| `curve` | chart id | series (stratum, term, band edge) | Lines and bands: `x0` = x, `value` = y |
| `lag` | `acf`, `pacf`, `resid_acf`, `resid_pacf` | null | `x0` = lag, `value` = the correlation, `stat = bound` once per chart |
| `hist` | `residual` | null | As in statistics: `x0`, `x1`, `value` = count |

Keys, exactly: `model|||r_squared`, `coef|price||estimate`, `coef|price|b|estimate`,
`assumption|breusch_pagan||p_value`, `assumption|schoenfeld|price|p_value`,
`point|resid_fitted||y|17`, `curve|km|channel=web|surv|42`, `lag|resid_acf||acf|3`.

A point's key has five parts: section, chart id, series (empty when there is none), measure
(`y`), and the index (the point's position in its series, counted from 0): `point|resid_fitted||y|17`.

**Statistics with a variable name.** These names carry a variable part, and the catalogue
declares them with a star: `surv_at_*`, `note_*`, `orders_reason_*`, `order_test_*`,
`ar_root_modulus_*`, `ma_root_modulus_*`. Every `<x>_p_value` has its `<x>_significance`, except `order_test_*_p_value`, which comes from interpolated critical-value tables and stays between 0.01 and 0.10.

**Points are capped.** At most `MAX_CHART_POINTS` (2,000) per chart: above it the runner
draws a sample with `seed`, and always keeps the ten most extreme points of the chart
(largest |standardized residual|, Cook's distance, leverage), labelled. Q–Q charts use 200
quantiles of **all** the residuals; histograms count all of them; a Kaplan–Meier curve
keeps every step when it has at most 2,000, else its value at 2,000 equally spaced times
(the steps' heights are exact there); a correlogram has at most 40 lags. So the table of a
model on ten million rows is a few thousand rows, and its report reads it whole.

`rows` output: the input's columns, then the model's columns (`pred_…`). A model column
whose name is already an input column is refused in the form.

## Execution

### The runner protocol

Each group has a **runner**: `gateway-go/internal/modelgroups/groups/<group>/runner/`
(`run.R`, the dispatcher, and **one file per model**, `R/<model>.R`), plus the library every
R runner shares, `gateway-go/internal/modelgroups/common/R/` (reading `task.json`, renaming
the columns, building the design, writing `results.parquet` in the ten columns, the caps on
points, the model card, `error.json`), all embedded in the Go gateway (its build
context is `gateway-go`, **verified**: `release-images.yml` builds it with
`context: gateway-go`). The Job's ConfigMap holds, under `/tabularia/code`:

| File | What |
|---|---|
| the runner's files and the common library | `runner/run.R`, `runner/R/*.R` from the group's directory; `common/R/*.R` (the library every runner sources), `common/selftest/*`, `card.schema.json`, and with the self-test the reference cases; put together by `modelgroups.CodeFiles`, the same files `cmd/modelgroup code` writes |
| `catalog.json` | the gateway's copy |
| `task.json` | `mode` (`fit`, `predict`, `selftest`), `model`, `params` (already checked by the gateway), the output wanted, the limits, `seed`, the identifiers |

Environment, besides the scripts' own `TABULARIA_INPUT`, `TABULARIA_OUTPUT` and `AWS_*`
(**verified**, `docs/specs/script-node.md`): `TABULARIA_RESULTS`
(`…/<step_id>/results.parquet`), `TABULARIA_ARTIFACTS` (`…/<step_id>/artifacts/`),
`TABULARIA_MODEL_IN` (Predict: the model card to apply).

The runner writes:

| File | Always | What |
|---|---|---|
| `results.parquet` | yes | The ten columns, types exact |
| `output.parquet` | when the node has nodes after it and `output` is not `results` | Rows or forecast |
| `artifacts/model.json` | `fit` | The model card («Model card») |
| `artifacts/native.rds` or `native.pkl` | `fit`, when MLflow is set and the card fits its ceiling | The fitted object as the language saves it, for whoever wants it outside Tabularia, **without the rows of the input** (`030a052c`): with `lm`, `glm` and Cox only `predict` works on it (for Cox with `reference = "sample"`); ARIMA's object is whole. **Tabularia never loads it** |
| `artifacts/run.json` | yes | Software versions, timings of each phase, warnings of the packages |
| `artifacts/error.json` | on a refusal | `{code, message, params}`: the runner exits with code 3; the step fails with that sentence, not with the log's tail |

**Exit codes**: 3 = a refusal (`error.json`); 4 = the self-test failed. **Log lines** the
runner prints: `TABULARIA_ERROR` and `TABULARIA_SELFTEST_REPORT`.

**As built, the protocol and the common library** (pieces 0 and the lanes: `c8fcf304`,
`7434bf31`, `646547b9`):

- **Numbers keep 17 significant digits** in every JSON the runner writes (`tab_json`,
  `jsonlite` with `digits = I(17)`; its `NA` keeps only 15). A card's coefficients give the
  fit's own predictions within 1e-9, which needs the last bits. The cost: cards grow, and a
  200 × 200 `vcov` comes near the 1 MB ceiling.
- **A runner reads every column** of an input when the task names none (`7434bf31`: a
  tidy-evaluation slip had selected none).
- **Hooks for a model's own self-tests.** The self-test runs the common library's tests, then
  `TAB_COMMON_TESTS` (a list of named functions a model file registers itself, as
  `kaplan_meier.R` and `cox.R` do), then the group's reference cases. The checks that the card
  gives back the fitted values still run **inside** each `fit_*` when `group_version` is
  `selftest` (linear, logistic, penalized, Cox); a hook for them is not built.
- **`run.json`.** Its `software` lists only R and `stats`, because the common library asks for
  the versions without naming a package; the `model` section's `software` text names the
  packages.
- **Notes** are `note_<id>` rows of the `model` section (see «The output table»).

### A run

The orchestrator runs a model node as it runs a Script node (`jobs_scripts.go`), with a
fixed code and these differences:

1. **Before anything**: the group is enabled, its current version is ready and built from
   the recipe this Tabularia ships, the person who runs the flow (or the schedule's
   author) may use the group, the parameters pass the catalogue's check, an MLflow
   connection named is one they may `CONNECT`. Otherwise the step fails with the reason
   and creates nothing.
2. **The input**: the chain to the node plus its `select`, written by the engine to the
   scripts bucket (`script_input`), which returns the row count; past the group's
   `max_rows` the step fails here («Limits»).
3. **The Job**, in the scripts namespace (trials: the trials' namespace and its quota),
   with the group's resources, the image by digest, internet access off, labels
   `tabularia.io/model-group`, `tabularia.io/model`.
4. **The import**: the engine imports `results.parquet` (always) and `output.parquet`
   (when present) into Tabularia's storage, as `/scripts/output/import` does; the engine
   checks `results.parquet` against the ten columns before accepting it (`app/engine/
   models/results.py`): a runner that writes another shape fails the step, it is a bug.
   The run keeps both keys (`runs.output_key`, `runs.model_results_key`) and `model.json`.
   The engine checks the parameters again at the import (`check_params` is idempotent, see
   «As built, the final review»).
5. **MLflow** (when set, flow runs only): the gateway logs («MLflow»), after the import.
6. **Clean-up**: the step's files in the scripts bucket are deleted by exact key, as for
   Script nodes: the prefix `env-<id>/<step>`, with `model.json`, `run.json`, `error.json`,
   `MLmodel` and `native.*` among them.

A run records the group's version, the image digest, the attempts, the exit code and the
log, in the columns Script runs already have; `runs.kind = 'model'`.

### The trial: Run in the editor

**Run** in the node panel is the Script node's trial (`POST /scripting/flows/{id}/nodes/
{node}/run`, `trigger_type = 'trial'`), for a `model` node: the saved flow (unsaved changes
saved first), the chain before it on the development sample, nothing after it, the trials'
namespace and its quota. **Trials are not logged to MLflow** («Provisional decisions», 2). The node's
Table tab and Report tab show its latest trial or run, whichever is newer
(`GET /scripting/flows/{id}/outputs`, with `stale` from the node's fingerprint: its
`params` and the fingerprint of the chain before it).

**As built, the node in runs and the trial** (lots C1 and C2: `c2c6d3b9`, `d674372b`, `fff6733c`):

- With the output `results`, `output_key` is the same key as `model_results_key`.
- The trial asks for **RUN** on the folder, as the Script node's does, where this spec said EDIT
  (open question 14); saving a flow with a group that cannot be used is not refused: the check
  happens when the step runs (open question 13).
- The trials appear in the list of runs as «Kind model test».
- `runs.kind = 'model'` is not a value of the frozen Python gateway's enum: that gateway does
  not read those rows.
- Hooks: `modelAfterImport` in `jobs_models.go` is where the MLflow logging starts
  (`task.MLflow = true`); the `model_predict` case in `jobs_queue.go` is Predict's.

### Report

`POST /tasks/model-report` (Go gateway, `api/proxy.go`, declared in `server/go_only_routes.go`):

```json
{ "flow_id": 42, "node_id": "n7", "run_id": 9811, "format": "svg", "locale": "it" }
```

- **Rights**: VIEW on the flow's folder and the flow opens for the person
  (`flowaccess.EnsureCanOpen`), as the outputs route asks; the run is one of that node's.
- Forwarded to the engine's `POST /reports/model` (`backend/app/api/routes/reports.py`),
  which starts `render_model_report_task` (`app/tasks/report_jobs.py`) on the export queue:
  it reads `results.parquet` from Tabularia's storage (the rows are small by construction),
  builds the definition with `app/engine/models/layouts.definition(model, params, locale,
  sample, step)`, renders it. **No query reaches the data, no Job starts**: the report of a
  model is a drawing of its results.
- Followed and downloaded like every export (`/tasks/exports/{id}`, `/file`).

## The report

### In the editor

The **Report** tab of the preview panel (the statistics' tab, `FlowEditor.vue`) is shown
for `model` nodes too, filled by `StatReportView.vue` with the model route: pages, zoom,
**PDF**, **Excel**, the sample line when the run was a trial on a development sample. With
no trial or run yet, the tab says so and offers **Run**. When the node or its chain changed
since, the pages stay, marked «Not up to date», with **Run**.

### The layouts

`backend/app/engine/models/layouts/`: `common.py` (the blocks every model has: bands,
summary cards, the assumptions' table, the coefficients' table, notes; built on the helpers
of `stats/layouts.py`, made public there, not copied) and **one file per model**
(`linear.py`, `logistic.py`, `kaplan_meier.py`, `cox.py`, `arima.py`, and `penalized.py`, whose blocks
both regression layouts call when a penalty is on), each registering
itself in `LAYOUTS`; words in `backend/app/engine/models/report_texts/common.json` and one
file per model, five languages in each. One file per model so that the models can be built
in parallel without touching each other's files. A4 portrait, one dataset `result`.
Blocks:

1. **Header band**: the model's name (the preset's for ARIMA), the step's name, the
   sample line, the date, «Page x of y».
2. **Summary**: four KPI cards chosen per model (linear: n, R², adjusted R², σ; logistic:
   n and events, AUC with its interval, McFadden R², Brier; Cox: n and events,
   concordance, LR p-value, AIC; Kaplan–Meier: strata, events, the medians, the log-rank
   p; ARIMA: n, AIC, σ², holdout MAPE) and the formula in symbols.
3. **Assumptions** — on the first page: one row per check: what it checks, H0, statistic,
   df, p with R's stars, the verdict in words and with a mark (ok, doubtful, failed, not
   computed), the advice. Above 5,000 rows a sentence: «with this many rows the tests
   reject very small departures: read the charts». R's legend of the stars.
4. **Coefficients**: the table, with «Signif.» and its legend; the interval chart.
5. **Fit**: every number of the `model` section.
6. **Diagnostics**: the charts of the model, each under the assumption it illustrates.
7. **Strata, pairs, forecast, confusion matrix**, where the model has them.
8. **Notes**: the `note_*` rows, rows left out, the software and the group version.
9. **Footer band**: «Computed by Tabularia», the sample line again, the time.

Excel: the tables of blocks 3, 4, 5 and 7, one sheet each; the numbers, not the charts.

### Charts: one engine-only kind, `xy`

The charts of models are on numeric (or time) axes with several marks: points with a
smooth line, steps with a band, a curve with bands, stems with bounds. Rather than one kind
each, one kind:

| Kind | Reads | Draws |
|---|---|---|
| `xy` | `category` = x (a number); series, each with `as`: `point`, `line`, `step`, `stem`, `band_low`, `band_high` (a band is the pair, filled between); `split_by`; `options.target` (a horizontal line), `options.target_x` (a vertical one), `options.diagonal` (y = x), `options.x_time` (x is days since 1970-01-01, the axis shows dates), `options.log_x` | ECharts `line` (with `step: 'end'` for steps), `scatter`, `bar` of zero width with caps for stems, an area between the band's edges (`stack` of the low edge and the width, the low invisible) |

`interval` gains `options.log` (an axis on a log scale, for odds and hazard ratios).
`Series.as_` gains the six values above **for `xy` only**; `reports.definition.check`
refuses them elsewhere, and refuses a band edge without its pair. Like `boxplot`,
`interval` and `density`, `xy` exists in the engine only: the gateway's validator of saved
reports (`gateway-go/internal/reportdef`) and the designer do not list it, so saved reports
and dashboards are unchanged. `tests/report_samples.py` and
`frontend/chart-renderer/presets.sample.json` gain one `xy` with each mark; the renderer's
count of presets goes up accordingly (`render.test.mjs`). ECharts 6.1.0's `line.step`,
`scatter`, `markLine` and stacked areas are in the service's bundle (**assumed**: checked
in the bundle by the piece, as the statistics checked `boxplot` and `markArea`).

## Model groups and their images

### A group in the repository

```text
gateway-go/internal/modelgroups/
  groups.go                       the embedded groups, their recipe hash, the self-test's task
  groups/regression/
    recipe.json                   {"language": "r", "base": "4.6", "packages": ["sandwich@…", …],
                                   "resources": {"cpu": "2", "memory": "4Gi"}, "max_rows": …}
    runner/run.R, runner/R/*.R
    reference/data/*.csv          the reference tables (small, fixed)
    reference/expected.json       the reference values, computed outside Tabularia
    reference/make_reference.R    how they were computed (run by hand, with R and the witness packages)
  groups/survival/…
  groups/timeseries/              {"language": "r", "base": "4.6", "packages": ["forecast@…", "tseries@…", …]}
  common/R/                        the R library every runner sources (task, renaming, results, card, errors, caps)
```

`base` is a version the chart's `scripts.baseImages` offers (a tag such as
`rocker/r-ver:4.6.0`, not a digest; **verified** in `SCRIPTS__BASE_IMAGES`: Python
3.11–3.14, R 4.5–4.6). A **recipe hash** is the hash of `recipe.json`; a **runner hash** of
the runner's files.

The recipes pin the whole closure of dependencies, in order: regression 48 packages,
survival 17, timeseries 42. The witnesses (the packages that compute the reference values)
are in `reference/witness.json`, outside the recipe.

### The supply chain

Decided by the owner on 2026-11-23, after the security review: a version alone does not say which bytes a mirror serves
(`SCRIPTS__PYPI_INDEX_URL`, `SCRIPTS__CRAN_URL`), so every package of a recipe is also pinned by the sha256 of the file the
build installs it from, and a longer build is accepted.

- **The recipe** gains `hashes`: `{"<package as listed>": ["<sha256>", …]}`, one entry for every package, nothing else.
  An R package has one hash, its **source tarball's**, computed by Tabularia on the tarball of the pinned version (CRAN's
  `PACKAGES` offers only an MD5, which is not used); a Python package has the hashes of **every wheel of that version a glibc
  Linux build of the recipe's Python may pick** (x86-64 and arm64, `cp314`, `abi3` or `py3-none-any`), sorted. The field
  sits beside `packages` (no migration of the format: a new key, the version strings unchanged, the packages text of a
  version in the database unchanged).
- **The tool**: `go run ./cmd/modelgroup pin <group>` (from `gateway-go/`) downloads every file and writes the hashes into
  the recipe, its other keys as they were. R: the tarball from `src/contrib/`, else `src/contrib/Archive/<name>/`, its
  `DESCRIPTION` read to check that every package it needs (`Depends`, `Imports`, `LinkingTo`) is earlier in the recipe or is
  one of R's base or recommended packages. Python: PyPI's JSON API for the version's files, each wheel downloaded and hashed
  here, and refused unless it is the digest PyPI publishes. The same files give the same recipe, byte for byte (a test). It
  is run whenever a version in a recipe changes; a recipe without a hash still loads, so that the tool runs, but a test
  checks every recipe shipped and the Dockerfile of an image refuses a package without its hash.
- **The Python build**: the requirements carry `--hash=sha256:…` each; `pip download --require-hashes` into a folder, then
  `pip install --require-hashes --no-index --find-links` from it (every file checked twice, no index reached by the
  install), the sha256 of the wheels installed recorded in `/opt/tabularia/package-sha256.txt`, then `pip check`. The
  PyTorch wheel (its URL already carried `#sha256=`) records its line too.
- **The R build** (`scriptimage.RInstallPinned`, managed images only; the scripting environments' images are byte for byte
  as before): `packages.txt` holds `name@version <sha256>` lines; every tarball is downloaded first (current, else archived)
  and checked with `sha256sum`, and a difference stops the build with a `TABULARIA_SHA256_MISMATCH` line before anything
  compiles; then each is installed from its file with `R CMD INSTALL`, in the recipe's order, which fails if a package it
  needs is not installed; the installed versions are compared with the recipe's, and the hashes recorded. `remotes` and
  `install.packages` are gone from the model images (no unpinned `arrow` either). `arrow`'s installer downloads libarrow
  and checks it against the SHA-512 sums its tarball ships, enforced with `ARROW_R_ENFORCE_CHECKSUM=true`.
- **The lock and the card**: the check Job appends ` sha256:<hex>` to the line of every package installed from a recorded
  file (`arrow@26.0.1 sha256:2b0f…`, `numpy==2.5.5 sha256:cc6e…`); the lock's parser accepts the suffix, and the card shows
  the lock.
- **A build that gets other bytes** fails the version with «The image build stopped: a package downloaded is not the file
  the group's recipe pins (its sha256 differs). …» (pip's sentence or the R installer's line, in the build log).
- **Not covered**: the base images (by tag), the Debian/Ubuntu libraries (apt verifies its signed archive), and what a
  package's own build fetches besides libarrow (none of the recipes' packages does, as far as their builds show).

#### As built, the supply chain (2026-11-23)

- **Format**: `hashes` beside `packages`, keyed by the package's line, a list of sha256 (sorted); `Recipe.CheckHashes`
  checks it (one hash an R package, 64 lower-case hex digits, no package without, no hash of another). Every recipe was
  pinned with the tool on 2026-11-23, at the versions it already had (`pinned_on` unchanged): 16 + 48 + 51 + 55 + 66
  tarballs for the five R groups, every one's `DESCRIPTION` in the recipe's order (the check found no missing or misplaced
  dependency); 20 Python packages of boosting (32 wheels) and 15 of `foundation_forecast` (24 wheels), each hashed here and
  equal to PyPI's digest. The tool takes a few seconds a group.
- **The builds**, on this machine, one at a time, without cache (`docker buildx build --no-cache`), before → after:
  experiments 353 → 317 s, regression 624 → 632 s, survival 898 → 810 s, customer lifetime 801 → 731 s, time series
  926 → 876 s, boosting 44 → 50 s, `foundation_forecast` (four sizes of weights) 60 → 69 s. The R builds are no longer:
  they no longer install `remotes` nor read CRAN's archive index once a package; the Python ones download then install
  (6 to 9 s more). Each image's check (the check Job's own script: lock, then self-test, uid 10001, read-only root,
  `--network none`, 2.5 GB) passed: experiments 103 checks, regression 67, survival 114, customer lifetime 72, time series
  119, boosting 56, `foundation_forecast` 36 with the sha256 of its four sizes; no check failed or skipped. Every package
  of every recipe has its ` sha256:` in the lock (`foundation_forecast` also `torch`); only the base image's own packages
  (`pip`, R's base and recommended ones) have none. `arrow` logged «Checksum validated successfully for libarrow»; no
  build fetched anything else.
- **A wrong hash, by hand**: the experiments Dockerfile with `jsonlite`'s hash changed stopped after 4.8 s, before anything
  compiled: `TABULARIA_SHA256_MISMATCH jsonlite@2.0.0: the file jsonlite_2.0.0.tar.gz downloaded from https://cran.r-project.org
  has sha256 75eb…, the recipe pins 0000…`; the boosting one with `numpy`'s two hashes changed stopped at pip's «THESE
  PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE … Expected sha256 0000… Got c489…».
- **On a cluster**: every recipe changed, so every group's recipe hash changed and each enabled group is built again by
  itself (state «Rebuild needed»); the version in use serves meanwhile, as for any new recipe.
- Tests: `scriptimage` (the managed R and Python Dockerfiles, a package without its hash refused, the scripting
  environments' images unchanged, the layers before the packages shared), `modelgroups` (every recipe shipped passes,
  every hash in its Dockerfile, a recipe without a hash refused by `CheckHashes` and by the Dockerfile), `cmd/modelgroup`
  (`pin` on a fake CRAN and PyPI: the same bytes twice, an archived tarball, the order, a digest that differs, no wheel for
  x86-64, the wheels' tags), `api` (the lock's parser, the R and Python checks on this machine with a record, a build that
  gets other bytes fails the version with its sentence, a lock with the hashes kept).

### Enabling a group

**Administration → Statistical models** (`frontend/components/admin/ModelsSection.vue`,
routes in `gateway-go/internal/api/models_admin.go`). One card per group shipped:

- name, language, the models it holds, its state: **Not enabled** · **Building** ·
  **Checking** · **Ready** (version n, built on …, packages from the lock) · **Failed**
  (why, the end of the log) · **Rebuild needed** (the shipped recipe changed);
- **Enable**: creates (or re-enables) the group's managed environment and queues its
  version 1; the existing build loop builds it, the check Job runs the group's
  **self-test** (`mode = selftest`: the runner on `reference/data`, compared with
  `expected.json` at the tolerances of «Reference values»); the version is **ready** only
  if every value matches, **failed** with the differences otherwise;
- **Disable**: the nodes leave the palette; flows that use them stop running with «the
  model group … is disabled»; the images stay in the registry; enabling again uses the
  current version at once;
- **Rebuild**: a new version from the same recipe;
- **Resources**: CPU and memory requested and limited, the longest run, `max_rows`
  (defaults from the recipe); they apply to the next run, without a build;
- **Who may use it**: everyone who can edit flows (default), or the user groups listed
  («Provisional decisions», 3).

When Tabularia starts with a group whose shipped recipe hash differs from its current
version's, the build loop queues a new version by itself; until it is ready, the group's
steps fail with «the model group … is being rebuilt for this version of Tabularia». A
change of the runner alone needs no build: the version in use **runs its self-test again** on
the same image (`8c7b5f85`: `runner_hash` was saved and never compared). It stays usable while it
is checked; the admin API marks it `rechecking`, which the interface does not show yet.

The section needs the scripting machinery configured (`SCRIPTS__ENABLED`, the registry,
the scripts bucket): otherwise it says what is missing, with a link to the guide. On
Compose it says «requires Kubernetes».

**As built, the groups in the panel** (lot B: `bf6f4559`, `911a7b2b`):

- The image's size is not available from BuildKit: the card shows the digest. The column
  `selftest` holds the tail of the self-test's log.
- **Limits.** CPU, memory and the longest run are copied into the environment when the group
  is first enabled; only an empty `max_rows` keeps following the recipe.
- **Who may use it**: administrators are always allowed. `GET /models/groups` answers 403 to
  whoever has EDIT nowhere.
- **Disabling** a group disables its managed environment too.

### Data model (one Alembic migration, `gateway/app/db/migrations/versions/0044_models.py`)

- `script_environments.managed_by` (text, nullable, unique): `model_group:<id>`. Managed
  rows are left out of the Scripting section, of the Scripting tab, of the Script node's
  choices and of the notebooks'; their packages cannot be edited; their versions are built
  by the same loop.
- `script_environment_versions.recipe_hash`, `runner_hash`, `selftest` (text: the
  comparison's report).
- `model_groups`: `id` (text, the group's id), `environment_id`, `enabled`,
  `everyone` (boolean), `max_rows`, `enabled_by`, `enabled_at`, `updated_by`, `updated_at`.
  The listed user groups reuse `script_environment_groups` on the managed environment.
- `runs`: `model_results_key`, `model_card` (text, ≤ 1 MB), `mlflow_connection_id`,
  `mlflow_experiment_id`, `mlflow_run_id`, `mlflow_model_name`, `mlflow_model_version`,
  `mlflow_error`.

The migration is **upgrade only**: its downgrade refuses to run. Besides the migration, the
SQLModel models of the gateway Python and `SchemaRevision` in `gateway-go/internal/db/db.go`
are in place, as for 0043 (`3c11f300`). **The owner's approval is still to be confirmed.**

Audit: `models.group_enable`, `models.group_disable`, `models.group_rebuild`,
`models.group_settings`, `models.group_access`; the connection's actions are the existing
`connection.*` with `db_type = mlflow`.

Configuration, as built: **no chart block `models` and no entry in the config**. The section
depends only on `scripts.*`; `MODELS__ENABLED` (on unless `false`) and
`MODELS__MAX_CHART_POINTS` (2,000) are read from the environment by the gateway, and nothing
else is the models' own.

## MLflow

### The connection

`db_type = "mlflow"`, a folder's connection like the others (`connections`, `CONNECT` to
use it in a node), created in `frontend/components/ui/ConnectionDialog.vue`.

| Field | Column | Notes |
|---|---|---|
| Tracking URI | `host` | `http(s)://host[:port][/path]`; no user, password, query or fragment (the API connection's `urlError`) |
| Authentication | `extra.auth` | `none` \| `basic` (MLflow's basic-auth app) \| `bearer` (a token: a reverse proxy, an identity provider) |
| User | `username` | `basic` |
| Password or token | `password_encrypted` | Fernet, never read back, as every connection's secret |
| TLS | `extra.verify_tls` (true), `extra.ca_cert` | A company authority, or (marked in the audit) no verification |
| UI address | `extra.ui_url` | Optional, when the browser reaches MLflow at another address than the gateway. Saved now; the run link of piece G uses it |

`gateway-go`: `supportedDBTypes` gains `mlflow`; `validExtra` checks its options;
`connpayload.MLflow(...)`; `connpayload.NotDatabases` gains it. The test is in the gateway
(Go), package `gateway-go/internal/mlflowconn`, route in `api/connections_mlflow.go`: the
engine is not called.

**Test** (from the dialog, before saving): with the connection's credentials, read-only.
The deciding check is `POST /api/2.0/mlflow/experiments/search` with `{"max_results": 1}`:
the credentials work or they do not. Two more checks: `GET
/api/2.0/mlflow/registered-models/search?max_results=1` (the registry exists: a server
without a database backend has none, and the test says that registering is not possible)
and `GET /api/2.0/mlflow-artifacts/artifacts` (artifacts can be uploaded through the
server). `GET /version` is not in MLflow's REST reference: it is optional, and the test
does not fail if it is missing. Each line: ok, or why not. **Assumed**: those REST
endpoints and their answers on the MLflow versions supported; the piece pins a server
version for the tests and writes the supported range in the guide.

Credentials stay on the tracking server's origin (`http_api.py`'s rule): a redirect to
another origin is not followed at all; every message passes the redactor.

### What is logged

Only **flow runs** (scheduled, run now, from a group of flows), not trials. Per model
node:

| What | MLflow | Content |
|---|---|---|
| Experiment | `experiments/get-by-name`, else `experiments/create` | The node's `experiment`; created when missing, with the tag `tabularia.created = true`. A refusal to create says so; an experiment in MLflow's trash has a sentence of its own |
| Run | `runs/create`, `runs/update` (`FINISHED` or `FAILED`, end time) | `run_name` = «flow · node · date». A failed model step is logged as `FAILED` with its error when it got as far as the Job |
| Tags | `runs/log-batch` | `tabularia.url` (the run page), `tabularia.flow_id`, `.flow_name`, `.flow_version`, `.node_id`, `.node_name`, `.run_id` (the orchestration), `.step_run_id`, `.model`, `.group`, `.group_version`, `.image_digest`, `.input_rows`, `.dev_sample` (true when the chain read a development sample), `.launched_by` (the person or the schedule's author, by email), `mlflow.source.name` = the flow's address, `mlflow.source.type = JOB`. Values are cut to 250 characters, the limit of `log-batch`. `tabularia.url` and `mlflow.source.name` start from `SHARE__PUBLIC_BASE_URL` (relative addresses when it is not set, see «Open questions for the owner») |
| Parameters | `runs/log-batch` | Every node parameter, flattened (`predictors` as a comma-separated text, `reference.channel`), and the formula |
| Metrics | `runs/log-batch` | Every number of the `model` section (`r_squared`, `aic`, `auc`…), and per assumption `assumption.<check>[.<level_2>].statistic` and `.p_value`; characters MLflow does not accept in a key become `_` |
| Artifacts | the artifact proxy, `PUT mlflow-artifacts/artifacts/<run's artifact path>/…` | `tabularia/results.parquet`, `tabularia/coefficients.csv`, `tabularia/report.pdf` (asked by the gateway of the engine after the step, the layout of the Report tab, in the flow's owner's language; if the rendering fails the step only gets a warning), `model/MLmodel` (with the flavor `tabularia` and the `run_id`), `model/model.json`, `model/native.rds` or `.pkl` |
| Model version | `registered-models/create` (when missing), `model-versions/create` with `source` = the run's `model` artifact path, `run_id`, tags `tabularia.model`, `tabularia.group`, `tabularia.group_version`, `tabularia.card_sha256` | Only with `registerAs`; aliases are left to people in MLflow |

The **gateway** does the logging (`gateway-go/internal/api/jobs_models_mlflow.go` and
`mlflowconn/tracking.go`, after the results' import): it decrypts the connection's password,
and reads the step's files and results back from the engine with `POST /scripts/objects/read`
(one file, streamed) and `POST /scripts/results/rows` (some sections of `results.parquet`),
which hold the bucket's credentials. There is no `/models/mlflow/log` route. Batches
respect the server's limits on entries per call and on the length of values (**assumed**
from MLflow's documentation: 1,000 metrics, 100 parameters and 100 tags per call; a
parameter value cut to the server's maximum and marked `…`; checked by the piece).

**A server without the artifact proxy** (artifacts written straight to a store the client
must reach): parameters, metrics and tags are logged, artifacts and registration are not,
and the run page says so. Writing to a store directly would hand its credentials to
Tabularia; not in the first release.

**When MLflow cannot be reached**, the step **fails** after its model was computed, with
«the model was fitted but MLflow could not record it: …»: a person who asked for tracking
must not get a silently untracked model. The node's option **Continue without MLflow**
(off) turns it into a warning on the run page.

**Where Tabularia shows it**: the run page shows, for a model step, the experiment and the
run with a link to MLflow's interface (`ui_url` or the tracking URI,
`#/experiments/<id>/runs/<run>`), and the model version when one was registered.

### The model card

`model.json`, written by the runner, read by Tabularia: everything needed to apply the
model to new rows, **as data, never as code**.

```json
{ "format": "tabularia-model", "format_version": 1,
  "model": "logistic", "group": "regression", "group_version": 3,
  "recipe_hash": "…", "runner_hash": "…", "software": { "R": "4.6.1", "stats": "4.6.1" },
  "params": { … },
  "inputs": [ { "name": "price", "type": "number" },
              { "name": "channel", "type": "category", "levels": ["shop", "web"], "reference": "shop" } ],
  "terms": [ { "term": "(Intercept)", "level": null, "variables": [] },
             { "term": "price", "level": null, "variables": ["price"] },
             { "term": "channelweb", "level": "web", "variables": ["channel"] } ],
  "coefficients": [ … ], "vcov": [[ … ]],
  "link": "logit", "threshold": 0.5 }
```

Per model it adds what prediction needs: `sigma` and `df_residual` (linear), the penalty,
λ and the standardization (penalized), the centring and the baseline cumulative hazard per
stratum at most 5,000 times (Cox), the survival table per stratum (Kaplan–Meier), the
orders, the coefficients, the transformation, the drift and the last observations the
filter needs (ARIMA). `vcov` is written up to 200 terms. `terms` are objects: `term` (the
coefficient's name), `level` (the category's level, null for a number), `variables` (the
input columns the term is built from).

**As built.** `card.schema.json` declares the common keys and one object per family: `linear`
(`sigma`, `df_residual`), `penalty` (`penalty`, `lambda`, `mixing`, `standardize`), `cox`
(`centring`, `strata`, `baseline`), `kaplan_meier` (`strata_column`, `strata`) and `arima`
(`orders`, `frequency`, `coefficients`, `sigma2`, `transform`, `box_cox_lambda`, `drift`,
`mean`, `exog`, `last`); it allows keys it does not declare. The code writes more than the
schema declares: Cox's `ties`, `time_column`, `start_column` and each stratum's `last_time`
(the strata's column is also in `inputs`); Kaplan–Meier's `time_column`, `conf_type` and
each stratum's `last_time`; ARIMA's `horizon`. These are still to be added to the schema (it declares only Kaplan–Meier's `tolerance`, `8d827068`).
`vcov` is kept for at most `max_vcov_terms` terms (200), Cox's baseline for at most
`max_baseline_times` (5,000) times; a card above `max_card_bytes` (1 MB) **no longer
stops the fit** (`5c6dd73d`): results, rows and report are written, the note `note_card_too_large`
says that the model cannot be registered, and only the model files (`model.json`, `MLmodel`,
`native.*`) are not written, so no MLflow version and no Predict for that step. A Kaplan–Meier
card is thinned first (`tolerance`, 0.001). ARIMA's `last` keeps the last 5,000 observations.

`MLmodel` is a YAML file the runner writes by hand: a `tabularia` flavor naming
`model.json`, and the input `signature` in MLflow's format. No `mlflow` package is
installed in any image. Registered models are therefore Tabularia's to apply («Provisional
decisions», 4).

## Predict

A **Predict** node («Previsione», `type: model_predict`) applies a model registered in
MLflow to the rows that reach it and adds the predicted column. **In the first release**
(owner).

```json
{ "type": "model_predict",
  "data": { "connectionId": 12, "experiment": "Pasta/price elasticity",
            "registeredModel": "price-elasticity", "version": "7",
            "params": { "interval": true, "threshold": 0.4, "horizon": 12, "at_times": [12, 24] } } }
```

**Choosing the model, in the form** (`frontend/components/ui/PredictForm.vue`): the MLflow
connection; the **experiment** (a list read from the server); the **version**: the versions
of registered models whose run belongs to that experiment and that Tabularia made (the
`tabularia.model` tag), newest first, each with its model, the date, the flow and node that
made it and its main metric; or **Latest version** of a registered model, resolved when the
step runs and recorded on the run. The lists come from the GET routes of lot H, answered by the gateway,
`GET /models/mlflow/{connection_id}/experiments` and `GET /models/mlflow/{connection_id}/versions?experiment=`,
with `CONNECT` on the connection.

**What it adds**, per model: one predicted column, `prediction`, always; the others when
asked.

| Model | `prediction` | With `interval` or the model's options |
|---|---|---|
| linear | the fitted value | `prediction_low`, `prediction_high` (prediction interval; unpenalized only) |
| logistic | the probability of the event | `prediction_class` at `threshold` |
| cox | the risk score (exp of the linear predictor centred on the fit's means, `reference = "sample"`, not R's per-stratum default) | `survival_at_<t>` for each of `at_times` |
| kaplan_meier | the survival at the row's `time`, in the row's stratum | `prediction_low`, `prediction_high` |
| arima | the rows of the forecast: `horizon` steps after the input series (or the future rows of the input, with `exog`), with the fitted coefficients, no re-estimation | `lower_80`, `upper_80`, `lower`, `upper` |

A predicted column whose name is already an input column is refused in the form.

**As built.** Each model's runner already has its `predict` from a card (written by its lane,
checked in the self-test against the fitted values, within 1e-9, where the model has fitted
rows); piece H wires them to the node. The lanes assumed the task parameters `horizon` and
`interval` for ARIMA (the example above) and `at_times` for Cox; piece H settles the final
names. A Kaplan–Meier prediction past a stratum's last time is NULL, and a Cox survival past
it is NULL too.

**As built, Predict** (lot H: `deec0fe4`, `8e599471`; `jobs_predict.go`, `models_mlflow.go`,
`mlflowconn/registry.go`, `modelcatalog/predict.go`):

- **The parameters** are `interval`, `threshold`, `horizon` and `at_times`.
- **«Latest»** is the highest version number carrying the tag `tabularia.model`, in any
  experiment; the experiment filters only the lists. The run records the version it was.
  With «latest», whoever has `CONNECT` on the connection can change the predictions by
  registering a newer version: **pin a version in production** (the documents say so,
  `c0069476`). The «Obsolete» state with «latest» does not see versions registered outside this
  Tabularia.
- **Which card is applied**: its `format` is `tabularia-model`, its `format_version` one this
  Tabularia knows, its model, group and variant known. The `group_version` it was fitted with
  may differ from the group's current one; the run page says so.
- **The group's `max_rows`** applies to Predict too.
- **The Job** is a run of its own kind, `model_predict`, handled like `model` by the
  reconciliation and the stop (`dbad274c`): an orphan is closed and its Job deleted.
- **Where the code is.** There is no `common/R/predict.R`: each model's runner has its
  `predict_*`. The gateway holds the MLflow reading; the engine only writes the card.
- **The trial** of a Predict node reads MLflow within 45 s (`44f9c04b`).

**How**: the **gateway** reads the version from MLflow (`model-versions/get`, or the latest
version), refuses it unless its `MLmodel` has the `tabularia` flavor and the sha256 of
`model.json` equals the version's `tabularia.card_sha256` tag, and writes the card into the
step's prefix of the scripts bucket (through the engine's `POST /scripts/objects/write`); the Job of the card's group applies it (`mode =
predict`) and writes the rows with the new columns. The Job never sees MLflow, and **no
serialized object is loaded**: only the card's numbers. The card's group must be enabled.

**The rows**: the card's `inputs` must be columns of the input, of the same kind; a level
not seen when fitting gives NULL predictions on its rows, counted and said on the run page.
**Checked against the fit**: predicting the rows a model was fitted on gives its fitted
values within 1e-9, a test of each model. A Predict step logs nothing to MLflow in the
first release; its run records the model name, the version and the card's hash.

Pros: the reason to register a model; reproducible predictions from a fixed version;
governance stays in MLflow (who promotes which version); no pickle or RDS is ever loaded,
so a version written by someone else cannot run code. Cons: a Job per prediction (seconds
of start-up, a few rows cost as much as a thousand); a card format of Tabularia's own, to
keep compatible across versions (`format_version`); predicting needs MLflow. Predicting a
linear or logistic model inside the engine, as an expression on any of the five engines
with no Job, is a later option the card already allows.

## Security

- **Credentials.** The MLflow password or token is encrypted with Fernet, never read back,
  decrypted only in the gateway to call the server (the engine never holds it), never sent to a browser, a log or a Job.
  The scripts bucket's key reaches the Jobs as for Script nodes; Tabularia's storage keys
  never do.
- **Who enables groups**: administrators only, not with an API token (as the scripting
  section), audited. Observers see the section read-only.
- **Who uses the nodes**: whoever may edit the flow (EDIT on its folder) and may use the
  group (everyone, or the listed user groups); an MLflow connection needs `CONNECT` on its
  folder. Running a flow (or its schedule) requires the same of whoever runs it or authored
  the schedule; a schedule set by someone else is switched off when a model node is added
  or its connection changes, as for any change that makes a flow reach further.
- **Isolation**: the Script node's — the scripts namespace (`restricted`), user 10001,
  read-only root filesystem, no capability, no ServiceAccount token, the NetworkPolicy that
  keeps them out of Tabularia's namespace and the metadata addresses — plus **internet
  access off**: a model Job reaches the scripts bucket through the egress proxy and nothing
  else. Trials in the trials' namespace and its quota.
- **No user code**: the runner is Tabularia's, from the repository; column names travel as
  data and are renamed before any formula («Columns never become code»); the self-test
  proves each image before it is used.
- **MLflow addresses.** The connection's client refuses what the gateway never calls
  (`9fc84530`, `egress.Forbidden`): loopback, link-local and the metadata addresses; private
  addresses are allowed, since an MLflow server is often inside the company's network.
  The refusal of a connection to someone without `CONNECT` quotes its id, not its name
  (`44f9c04b`).
- **Predict**: only Tabularia's cards are applied, verified by hash; serialized objects are
  artifacts for people, never loaded.
- **Supply chain**: packages pinned by version **and by the sha256 of the file each is installed from**
  («The supply chain», 2026-11-23), from CRAN and PyPI (or the mirrors the chart sets), base images
  by tag (`rocker/r-ver:4.6.0`); the lock of every version is kept and shown, with those hashes.

## Limits

| Limit | Default | Where | Beyond it |
|---|---|---|---|
| Input rows (`max_rows`) | regression 2,000,000; survival 1,000,000; time series 20,000 observations | group setting | «The model would read 3,400,000 rows; this group allows 2,000,000: filter or sample before the node, or ask an administrator» |
| Memory | regression and survival 4 GiB, time series 2 GiB (limit; request half) | group setting | the cluster stops the Job (exit 137); the step says so with the limit (the Script node's message) |
| Terms of the design | 200 after encoding | catalogue | «… makes 340 terms: fewer categories, or turn on a penalty» |
| Levels of a categorical predictor | 50 | catalogue | «the column … has 812 values: is it the right column?» |
| Strata | 20 (Kaplan–Meier, Cox `strata`) | catalogue | sentence |
| Seasonal period `s` | 60 | catalogue | «for a yearly season on daily data use exogenous Fourier terms» (later) |
| Horizon | 1,000 | catalogue | sentence |
| Minimum rows | per model: more rows than terms + 1; at least one event; a series of 2·s + p + q + d + 10 observations (`s` counted only with a seasonal part); with a penalty, 3 rows per fold, at least 2 terms, the rarer outcome value on at least as many rows as folds, and with `measure = auc` 10 rows per fold | runner | `error.json` with the sentence |
| Chart points | 2,000 per chart (`MODELS__MAX_CHART_POINTS`) | runner | sampled with `seed`, extremes kept |
| Time | the group's longest run; the node's timeout within it | group, node | the Job's deadline; the step says which |
| Model card | 1 MB | runner | `vcov` (above 200 terms) and the baseline hazard (above 5,000 times) are cut first; a Kaplan–Meier card is thinned within 0.001. If it still does not fit, the fit goes on and only the registration is lost (note `note_card_too_large`: no MLflow version, no Predict). A `vcov` of 200 terms takes most of the megabyte once numbers have 17 digits |

**Start-up.** A Job starts in seconds when the image is on the node; the first run on a
node pulls the image (R images with compiled packages weigh more than a gigabyte; minutes
on a slow registry). The panel shows the image's size per version; the guide suggests
pre-pulling on large clusters. The editor never starts a Job by itself: **Run** does.
Expected durations are measured on kind with the sample data in the slice, not promised.

**Large data in development.** These limits are tested by lowering them (a group with
`max_rows = 50` fed 51 rows; a design limit of 5 fed 6 terms), never with giant tables.

## As built, the test on kind and the final review

### The test on kind (lot I1)

Four bugs found on a running cluster, corrected: `tab_read_text` read an address on S3 with no
size, which broke Predict (it reads in pieces now, `a50f3a6d`); the Predict form's experiment
list stayed a skeleton when the node already had its experiment (`b102e987`); a Kaplan–Meier
with no `times` failed (`f43a6a19`, with the catalogue case `kaplan_meier_default`); **no ARIMA
could run**, because `exog` had a minimum of one column and a default of `[]`: the minimum is
gone (`d8785c17`), which is the «`check_params` is not idempotent» fault of lane C1, fixed for
good by the tests of «the final review», point 11.

**Described, not corrected**: the trials are in the list of runs as «Kind model test»; the MLflow
tags hold relative addresses, since the installation's public address is not set; the sentence
about a quota carries Kubernetes' own text; the 403 sentence writes the e-mail with a capital.

**Seen live on a second round on kind**: exit code 4 of the self-test (a failed version v3 while
v1 stayed in use); Cox, also with strata, and the logistic regression, also with a lasso (trial,
report and flow); ARIMA with automatic orders (SARIMA(0,1,2)(0,1,1)[12] out of 24 candidates, the
report with the candidates' table); Predict for Kaplan–Meier (survival at 30 days equal to the
fit) and for ARIMA (horizon 6, equal to the fit).

**Not seen live**: the egress proxy with an external storage; the logistic regression beyond
100,000 rows.

### The final review and the corrections

Go: `dbad274c`, `9fc84530`, `8c7b5f85`, `40cc14dd`, `6dfd237f`, `e95fd9d2`, `3170cd7a`, `44f9c04b`,
`c0069476`. R and report: `5c6dd73d`, `8d827068`, `b4cf0059`, `a1b9263d`, `3c3dff03`, `030a052c`,
`b7da2b3b`.

1. **A card too large** no longer stops the fit (`5c6dd73d`): only the registration is lost, with
   the note `note_card_too_large` («Model card»). A Kaplan–Meier card is thinned within 0.001
   (`8d827068`, note `note_card_thinned`).
2. **The logistic regression holds large tables** (`b4cf0059`): see «As built, logistic».
3. **The AUC of a penalized fit** is the mean of the folds' (`a1b9263d`): see «As built, the
   penalties». This settles the owner's question on the out-of-sample AUC.
4. **The verdicts' thresholds** (`3c3dff03`): Cook's distance above the median of F(p, n − p)
   doubtful, above 1 failed; leverage above max(3p/n; 0.5) doubtful; Cox's dfbetas above 1
   failed; the Kaplan–Meier curves count as crossing only when the difference passes 0.05 and
   2 standard errors, both ways. The wide thresholds only name the rows to look at. **`rmst_tau`**
   by default is the shortest of the strata's longest times, so the RMST values change.
5. **`native.rds`** no longer carries the rows of the input (`030a052c`): with `lm`, `glm` and
   Cox only `predict` works on it (Cox with `reference = "sample"`); ARIMA's object is whole.
6. **The assumptions' «Value» column** says what it counts, and shows the verdict's threshold;
   the ADF and KPSS p-values at their table's ends say so (`b7da2b3b`).
7. **Orphan predictions.** The `model_predict` runs are handled by the reconciliation and by the
   stop, like the model runs (`dbad274c`).
8. **SSRF.** The MLflow client refuses loopback, link-local and metadata addresses, with
   `egress.Forbidden`; private addresses are allowed (`9fc84530`).
9. **A new runner** makes the version in use self-test again on the same image, without a
   rebuild; the version stays usable and the admin API says `rechecking` (the interface does not
   show it yet) (`8c7b5f85`).
10. **Refusals and time-outs.** The refusal for whoever has no `CONNECT` quotes the connection's
    id, not its name; the Predict trial reads MLflow within 45 s (`44f9c04b`). The documents carry
    the warning on «latest» (`c0069476`).
11. **Idempotence.** The catalogue's check gives the same answer on its own answer: tested in the
    Go catalogue (`40cc14dd`), the engine (`6dfd237f`, which also checks the parameters again at
    the results' import), the form (`e95fd9d2`) and the assistant (`3170cd7a`); an empty list of
    columns stays `[]`, not null.
12. **What remains.** A linear or logistic fit of about 190 terms loses its whole card (only the
    `vcov` could be dropped instead); a Cox fit with many strata and times is not thinned; the
    logistic regression is not tried beyond 100,000 rows.

## Reference values

Each group's `reference/expected.json` is computed **outside Tabularia** by
`reference/make_reference.R` (and `make_witness.py` for the Python witnesses), with the
same pinned packages as the recipe plus the witness packages (`car`, `ResourceSelection`,
`pROC` for the regression group; as built, these three were not available and a second construction of the same figures stands in, see below), run by hand by whoever builds the piece; the command and the versions are written in the file's
header. The tables are **small** and in the repository: tens to a few hundred rows, fixed
seeds, written as CSV (synthetic, so no licence question; one table per case: a linear
model with a categorical predictor and an interaction; a heteroscedastic one; a logistic
one with a separated variant; the linear and logistic tables again for the penalties; a survival table with censoring, ties and three strata; a
monthly series of 144 observations with a season of 12, and one with an exogenous column
and a future horizon). The sample data of the project's example (the pasta maker) gives
the demonstration flows, not the references.

| Check | Tolerance |
|---|---|
| The runner against the same package called directly (`lm`, `glm`, `glmnet`, `coxph`, `survfit`, `forecast::Arima`, `forecast::auto.arima`) | relative 1e-9 (same image, same BLAS) |
| The runner's own pieces (VIF, Hosmer–Lemeshow, Box–Tidwell, DeLong) against `car`, `ResourceSelection`, `pROC` (as built, against a second construction of each: see «As built» below) | 1e-9 |
| A second implementation as witness, computed outside Tabularia: `statsmodels` OLS and GLM for linear and logistic, `lifelines` for Kaplan–Meier and Cox, `statsmodels` SARIMAX for ARIMA; the chosen orders of `orders = auto` against `forecast::auto.arima` itself | 1e-6 for OLS, GLM, Kaplan–Meier and Cox; 1e-4 relative on ARIMA coefficients (the two initialize the likelihood differently: said, not hidden; a SARIMA with D = 1 differs by 6e-4, see «Open questions for the owner»); the chosen orders exactly |
| Counts and texts | exact |

**As built.** The tolerances above are the ones the code uses, with these facts. The
regression group has no witness script yet: the VIFs, Hosmer–Lemeshow and DeLong's interval
are compared with a second construction written in `make_reference.R`, because `car`,
`ResourceSelection` and `pROC` (named in its `witness.json`) were not available, so the 1e-9
comparison with those packages is not done, and `statsmodels` OLS and GLM at 1e-6 is not done
either. The survival witness (`make_witness.py`, `lifelines` 0.30.3) compares Kaplan–Meier at
1e-6 and Cox with Efron's ties only; it applies R's quartile rule (the middle of a step, where
`lifelines` takes its start) and prints, without comparing, what the two define differently
(the proportional-hazards test, robust errors with tied times). The time series witness
(`statsmodels` 0.15.0) is at 1e-4. The quartile rule of `survival`'s `quantile` belongs here
as a reference value: where a curve lies exactly on the level over a step, R takes the middle
of the step.

The self-test of the image runs the first two rows of this table: an image is ready only
when it reproduces them.

## Testing strategy

**On small data, always** (owner, 2026-11-19): every test below uses the reference tables
or the sample data, tens to a few thousand rows.

- **Catalogue** (`backend/tests/test_models_catalog.py`, `model_catalog.py --check`,
  `frontend/tests/model-catalog.test.mjs`, `gateway-go/internal/modelcatalog`): snapshot and
  copies equal; every model has a group, a layout, its words in five languages, its runner
  function, its reference case, its line in the documentation.
- **Runners**, in the group's image (`mode = selftest`, and the group's unit tests run the
  same way): every reference value; the refusals (separation with ML, a singular design, no
  events, a series too short, duplicates in time) with their sentences; column names that
  look like code; the ten columns and their types; **each variant's keys equal the
  catalogue's declaration** (no p-value with a penalty); the point caps; predicting the
  fitted rows from the card gives the fitted values; automatic ARIMA orders equal
  `auto.arima`'s.
- **Gateway** (`go test ./internal/...`): parameters checked from the catalogue; the Job's
  spec (ConfigMap files, environment, labels, internet off, resources, namespace of a
  trial); the limits before the Job; a disabled group, a group being rebuilt, a person not
  allowed; the managed environments hidden from the scripting routes; the MLflow connection's
  checks; `/tasks/model-report` rights; the resolver's boundary; dbt refusal.
- **Engine** (`ci_tests.sh engine`): `results.py` accepts the right shape and refuses others;
  layouts of every model on stored results (fixtures written by the runners' tests), in five
  languages, pass `reports.definition.check` and render; the `xy` kind; the MLflow client
  in Go, against an `httptest` server (experiment found or created, batches split, artifacts
  uploaded, version registered, redaction of the secret in every error, no credentials to
  another origin).
- **Interface** (`npm test`, `vue-tsc`): the palette shows only enabled groups; the form
  per kind; the ARIMA presets; the Report tab's states for a model node; the connection
  dialog's MLflow kind; the admin section's states.
- **On kind** (three nodes with Calico, the project's rule) **and minikube**, Chrome and
  Firefox, sample data only: enable each group and see it build, check and become ready;
  a failed self-test (a reference value altered on a scratch branch) leaves it failed;
  drop each model, **Run**, read the Table and the Report, download PDF and Excel; a flow
  run logged to an **open-source MLflow server** started in the cluster for the test (an
  image pinned by version, with basic authentication; not shipped), the model registered,
  then a Predict node applying that version; a person outside the group's user groups cannot
  run; the Job's pod has no route to the internet and no MLflow variable.
- **On Compose**: the section and the nodes say «requires Kubernetes».
- **Regression**: the three suites; the statistics' browser checks (the Report tab is
  shared); the scripting checks (the build loop is shared).

## Commands

```bash
# engine, gateway, interface: as for the statistics (one heavy job at a time on this machine)
docker build --build-arg WITH_DEV=true --build-arg WITH_CHDB=true -t tabularia-backend:ci backend
.github/scripts/ci_tests.sh engine tabularia-backend:ci
.github/scripts/ci_tests.sh gateway tabularia-gateway:ci
cd gateway-go && go test ./internal/...
cd frontend && npx nuxi prepare && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
cd frontend && npm i --no-save @intlify/message-compiler@9.14.5 && npm test
cd frontend && node --test chart-renderer/render.test.mjs
# the catalogue: the snapshot, then its copies and their check
docker run --rm --user "$(id -u)" -v "$PWD/backend:/app" -w /app tabularia-backend:ci python -m app.engine.models.catalog
python3 .github/scripts/model_catalog.py && python3 .github/scripts/model_catalog.py --check
# the sha256 of every package of a recipe, after a version changed (downloads every file; writes recipe.json)
cd gateway-go && go run ./cmd/modelgroup pin regression
# a group's image built locally and its self-test, without a cluster (new: gateway-go/cmd/modelgroup)
cd gateway-go && go run ./cmd/modelgroup dockerfile regression > /tmp/regression.Dockerfile
docker build -f /tmp/regression.Dockerfile -t tabularia-models-regression:dev /tmp
# the code folder (an absolute <dir>): the group's files plus common/R, which sits outside the group's folder
go run ./cmd/modelgroup code regression <dir>
docker run --rm -v "<dir>:/tabularia/code:ro" tabularia-models-regression:dev \
  Rscript /tabularia/code/runner/run.R --selftest
# the boosting group (Python): the image once, then the self-test with the code mounted
cd gateway-go && go run ./cmd/modelgroup dockerfile boosting > /tmp/boosting.Dockerfile
docker build -f /tmp/boosting.Dockerfile -t tabularia-models-boosting:dev /tmp
go run ./cmd/modelgroup code boosting <dir>
docker run --rm --read-only --tmpfs /tmp -v "<dir>:/tabularia/code:ro" tabularia-models-boosting:dev \
  python -I -B /tabularia/code/runner/run.py --selftest
# its reference values (inside the image) and its witnesses (outside, uv with witness.json's pins)
docker run --rm -v "$PWD/gateway-go/internal/modelgroups/groups/boosting/reference:/ref" tabularia-models-boosting:dev \
  python -I /ref/make_reference.py
uv run --no-project --with-requirements <witness requirements> python gateway-go/internal/modelgroups/groups/boosting/reference/make_witness.py
# the reference values, by hand, outside Tabularia (header of the file says the versions)
Rscript gateway-go/internal/modelgroups/groups/regression/reference/make_reference.R
# parametric survival (planned): the survival image and its self-test as above with `survival`;
# settings reference, site
python3 .github/scripts/config_reference.py --check
cd website && npm run generate && npm run check-links
```

## The path of a new model or group

| Where | What |
|---|---|
| `backend/app/engine/models/catalog.py` | The model (and the group, for a new one); then the snapshot and the copies |
| `gateway-go/internal/modelgroups/groups/<group>/` | `runner/R/<model>.R` (a Python group: `runner/py/<group>_<model>.py`, never a library's own name), the recipe's packages, the reference table and values |
| `backend/app/engine/models/layouts/<model>.py`, `report_texts/<model>.json` | The layout and its words |
| `frontend/i18n/locales/*.gen.ts` | `models.<id>.name`, `.hint`, the parameters' and choices' labels |
| `docs/capabilities/models.md` | The model's section |

A new group touches none of the existing groups' directories, images or settings: it
appears in the admin section as **Not enabled**.

## Boundaries

- **Always**: Kubernetes only; a group built only when an administrator enables it, and
  ready only when its self-test passes; charts drawn by the report engine; the ten columns;
  assumptions reported with their verdicts; Run with a button; MLflow credentials only in
  the engine; small data in development; five languages; English code and documents; no
  competitor named; commits by piece with their checks green.
- **Ask first**: a new dependency of the engine, the gateway or the interface (none is
  needed); a package of a group outside CRAN or PyPI; a change to what an existing chart
  kind, route or operation returns; logging trials to MLflow; loading a serialized model
  (the one exception, decided by the owner on 2026-11-20: the `boosting` group's
  `xgboost.json` and `catboost.cbm`, model data the library parses, after their hash
  matched the card's; pickle, joblib, RDS or any format that runs code when read: never).
- **Never**: model code in Tabularia's workers; a Job with internet access or with an
  MLflow credential; a column name inside code; a render or a Job at each keystroke; a
  giant dataset to test a limit; a function of the Python gateway outside the AI service
  and the migrations; the 1.4 line touched.

## Success criteria

1. On kind, an administrator enables *Regression*; it builds, its self-test passes, and
   *Linear regression* and *Logistic regression* appear in the palette; on Compose the section says «requires
   Kubernetes».
2. A person drops it after a step of the sample data, chooses the columns, presses **Run**:
   the Table tab shows the coefficients, and the Report tab the pages, with the
   assumptions' table on the first page; the numbers equal `lm` + `sandwich` + `lmtest` on
   the same rows within 1e-9.
3. A wrong setup (a text outcome for a linear model, separation with ML, a date as a
   predictor, duplicates in a series) stops with a sentence that says what to change.
4. A flow run with an MLflow connection and an experiment creates a run there with the
   parameters, metrics, artifacts and tags of «What is logged», and a model version when
   asked; the run page links to it; the Job never held the token.
5. A Predict node applies that version to new rows and reproduces the fitted values on the
   training rows.
6. Every model of the catalogue passes its reference values in its image; ARIMA with
   automatic orders chooses what `forecast::auto.arima` chooses and its report lists the
   candidates and why.
7. Adding a group or a model touches only what «The path of a new model or group» lists.
8. No new dependency in `pyproject.toml`, `package.json` or `go.mod`.
9. Flows, scripts, statistics nodes, reports and dashboards that existed behave as before.

## Pieces

The first release of the models is all of this: the five models, MLflow and Predict. It is
cut into small pieces, each one commit with its own check green.

Three rules:

- **Piece 0 comes first, alone**: it is the contract every other piece builds against
  (runner ↔ engine ↔ output format ↔ catalogue ↔ model card). After it, the lanes below
  work **in parallel on disjoint files**.
- **One heavy job at a time on the development machine** (owner's rule, 16 GB): lanes may
  write code in parallel, but image builds, the engine suite and the gateway suite run one
  after the other. An R image build (the regression group compiles `glmnet`, the time series
  group `forecast`) is a heavy job.
- **Small data in every acceptance**: the reference tables and the sample data, tens to a
  few thousand rows. A limit is accepted by lowering it, never with a large table.

The five `frontend/i18n/locales/*.gen.ts` files (edited by hand, **verified**) are shared
by several lanes: each lane writes only its own namespace (`models`, `modelsAdmin`,
`modelReport`, `connections.mlflow`, `predict`) and rebases on the others.

### Where the pieces stand (2026-11-20)

Commits are on `main` (`git log --oneline 97c17448..HEAD` lists the models' with the others').

| Piece | State | Commits |
|---|---|---|
| **0** Common contract | **Done**; the migration `0044_models.py`, its SQLModel models and `SchemaRevision` came with lot B (`3c11f300`), the owner's approval still to be confirmed | `97c17448` catalogue and its copies; `3dc72ff2` the results check and a fixture per model and variant; `c8fcf304` the runner protocol, the R library, the three recipes; `7434bf31` fix: a runner reads every column of its input |
| **A** MLflow connection | **Done** | `82004a9a` |
| **D** The node in the editor | **Done** (the Model and Predict nodes, the form from the catalogue; the Report tab for model nodes is `098e6d2f`, under E2) | `f724f305`, `8e9dd5c4` (test fix), `5ce1fae2` (a click on a palette item adds it, as Enter does) |
| **E1** The `xy` chart | **Done** | `e5b886eb`, `26637a61` (the diagonal inside the plot, round axes under bands) |
| **E2** Common layout, route, Report tab | **Done** | `c95ff34d` the layouts and `POST /reports/model`; `c58a4620` `POST /tasks/model-report`; `098e6d2f` the Report tab |
| **M1** Linear | **Done**, but for the witnesses `car` and `statsmodels` of its acceptance, which were not available (see «As built, linear») | `e745afb3` |
| **M2** Logistic | **Done**, but for the witnesses `pROC`, `ResourceSelection`, `car` and `statsmodels` of its acceptance, which were not available (see «As built, logistic») | `b046c407` |
| **M3** Penalties | **Done** | `2a872962` |
| **M4** Kaplan–Meier | **Done** | `b8521e60` |
| **M5** Cox | **Done** | `570b1ef3` |
| **M6** ARIMA, manual orders | **Done** | `fcd6668c`; with M7: `646547b9` (numbers to 17 digits), `8a614dde` (the card drops its exact copy of the coefficients) |
| **M7** ARIMA, automatic orders | **Done** | `e10b530c` |
| **B** Groups in the admin panel | **Done** | `3c11f300` migration 0044; `bf6f4559` the admin API, the build with its self-test and the ready image; `911a7b2b` Administration → Statistical models |
| **C1** The node in runs | **Done** | `c2c6d3b9` |
| **C2** Trial and outputs | **Done** | `d674372b` Run in the editor and the latest output; `fff6733c` the Run button, its outcome and its runs on the run page |
| **G** MLflow tracking | **Done**, in the gateway Go | `b5bf7620` |
| **H** Predict | **Done**, the reading of MLflow in the gateway Go | `deec0fe4`, `8e599471` (Run for a Predict node, its runs on the run page) |
| **I1** Integration, on kind | **Done**: four bugs found and corrected, three kinds of thing left as described («As built, the test on kind») | `a50f3a6d`, `b102e987`, `f43a6a19`, `d8785c17` |
| **I2** Documentation | **Done** | `e2284332` the page, the admin section, the map, flows, engines, dbt, MLflow, Kubernetes, changelog, site navigation; `c0069476` the warning on «latest» |
| **I3** Assistants and MCP | **Done** | `24d59654` |
| **Final review and corrections** | **Done** | Go: `dbad274c`, `9fc84530`, `8c7b5f85`, `40cc14dd`, `6dfd237f`, `e95fd9d2`, `3170cd7a`, `44f9c04b`, `c0069476`; R and report: `5c6dd73d`, `8d827068`, `b4cf0059`, `a1b9263d`, `3c3dff03`, `030a052c`, `b7da2b3b` («As built, the final review») |

All the pieces are built. The end-to-end demonstration (0 + B + C1 + C2 + D + E1 + E2 + M1) and
the integration ran on kind (lot I1). What each model's lane found while building is in the
«As built» blocks of «The catalogue» and of the sections below; what the final review changed
is in «As built, the final review».

### Piece 0 — the common contract (first, alone)

| | |
|---|---|
| Objective | Everything the lanes share, fixed once: the catalogue of the five models and of `model_predict` (parameters, kinds, defaults, `applies`, and per variant the numbers each section holds); the ten columns and their check in the engine; the runner protocol (`task.json`, the files written, `error.json`, exit codes 3 and 4); the R library every runner shares and the dispatcher (`run.R` routes `(model, penalty, orders)` to `R/<model>.R`, `R/penalized.R`, `R/arima_auto.R`, which exist as stubs that write `error.json` «not built yet»); the model card's schema; the three recipes with their packages pinned (versions taken from CRAN on the day and written in the recipe); the migration; the local build-and-self-test command; a results fixture per model and variant, written from the catalogue's declaration with placeholder numbers, for the report lane to draw before the runners exist |
| Files | `backend/app/engine/models/{__init__,catalog,results}.py`, `catalog.json`, `backend/tests/test_models_catalog.py`, `backend/tests/test_models_results.py`, `backend/tests/fixtures/models/*.json`; `.github/scripts/model_catalog.py`, `.github/workflows/model-catalog.yml`; `gateway-go/internal/modelcatalog/`; `frontend/assets/model-catalog.json`; `gateway-go/internal/modelgroups/` (`groups.go`, `card.schema.json`, `common/R/*.R`, `groups/{regression,survival,timeseries}/recipe.json`, `runner/run.R`, the stubs); `gateway-go/cmd/modelgroup/`; `gateway/app/db/migrations/versions/0044_models.py`; `gateway/app/models/model_group.py` (new), `run.py`, `scripting.py`, `__init__.py` (SQLModel); `gateway-go/internal/db/db.go` (`SchemaRevision`); `gateway-go/internal/modelgroups/groups/{regression,survival,timeseries}/reference/witness.json` |
| Accepted when | `model_catalog.py --check` is green and fails on a changed byte; `results.py` accepts every fixture and refuses a wrong type, a missing column, a duplicated key, a key the catalogue does not declare for that variant; the regression image builds locally from its recipe with `cmd/modelgroup` and `run.R --selftest` runs the common library's own tests on a table of 20 rows (column names that look like code come back as labels; the ten columns written with their types; `error.json` and exit 3); the migration upgrades on the development database; its downgrade refuses to run |
| Check | `ci_tests.sh engine` (or its two new modules while working), `go test ./internal/...`, `model_catalog.py --check`, the local image commands of «Commands» (one build) |

### The lanes after piece 0

```text
piece 0 ──┬── A  MLflow connection          (can start even before piece 0)
          ├── B  groups in the admin panel
          ├── C1 the node in runs ── C2 trial and outputs
          ├── D  the node in the editor
          ├── E1 the xy chart ── E2 common layout, route, Report tab
          ├── M1 linear ─────────┐
          ├── M2 logistic        │   each: runner first, its layout once E2 is in
          ├── M3 penalties (linear and logistic)
          ├── M4 Kaplan–Meier    │
          ├── M5 Cox             │
          └── M6 ARIMA ── M7 ARIMA automatic orders
                                 ▼
   G  MLflow tracking (needs A, C1, D, M1) ── H Predict (needs G) ── I integration, docs
```

The first end-to-end demonstration is **0 + B + C1 + C2 + D + E1 + E2 + M1**: a linear
model enabled, run and read in its report on kind.

| Lane, piece | Objective | Files (owned by the lane) | Depends on | Accepted when (small data) | Check |
|---|---|---|---|---|---|
| **A** MLflow connection | The `mlflow` kind: dialog, options' checks, encrypted secret, test (in the gateway) | `gateway-go/internal/mlflowconn/` and `api/connections_mlflow.go` (the test); `gateway-go/internal/api/connections_create.go`, `connections_inspect.go`, `internal/connpayload/connpayload.go`; `frontend/components/ui/ConnectionDialog.vue` | — | Against an open-source MLflow server in kind (or a local one, pinned version, basic auth): the test says ok; a wrong password, a wrong address, no registry, no artifact proxy each give their sentence; the secret is never in a response or a log; `httptest` tests in Go cover redirects to another origin (none is followed) | `go test`, engine module tests, `vue-tsc`, `npm test` |
| **B** Groups in the admin panel | Managed environments; enable, disable, rebuild, resources, access; the self-test in the check Job (decision: once the models are built, at least one reference case per model); «Rebuild needed» | `api/jobs_script_builds.go` (managed versions, self-test, recipe hash), `api/scripting.go` (managed rows hidden), `api/models_admin.go`; `frontend/components/admin/ModelsSection.vue`, `AdminPanel.vue` | 0 | On kind: enable *Regression*, see Building → Checking → Ready with its lock; a reference value altered on a scratch branch leaves the version Failed with the difference; the Scripting section and the Script node do not list the managed environment; on Compose the section says «requires Kubernetes» | `go test`, `ci_tests.sh gateway`, `vue-tsc`, `npm test`, kind |
| **C1** The node in runs | `model` and `model_predict` as step nodes; input with `select` and row count; the Job with the runner's ConfigMap, internet off; import of results and output; clean-up; limits before the Job; dbt refusal | `internal/flowresolver/flowresolver.go`, `internal/flowaccess/`, `api/jobs_models.go`, `internal/dbtexport/export.go`; `backend/app/api/routes/scripts.py` (results import) | 0 (B to run on kind) | On kind: sample → `linear` (a stub until M1) → Output runs, the run page shows the step, group version and log; `max_rows = 50` with 51 rows fails before any Job with its sentence; the Job's pod spec has no MLflow variable and the internet label off | `go test`, `ci_tests.sh engine`, kind |
| **C2** Trial and outputs | **Run** for a model node; the latest output and `stale` | `api/scripting_trial.go`, `api/scripting_runtime.go` | C1 | Run on the sample data in the trials' namespace; changing a parameter marks the output stale | `go test`, kind |
| **D** The node in the editor | Palette group *Models* (enabled groups only, ARIMA presets), `ModelNode.vue`, `ModelForm.vue` (variants through `applies`), the MLflow block, Run, the Table tab, the Report tab shown for model nodes | `OpSidebar.vue`, `useOpIcons.ts`, `components/nodes/ModelNode.vue`, `components/ui/ModelForm.vue`, `composables/modelCatalog.ts`, `NodePanel.vue`, `FlowEditor.vue`, `useFlowModel.ts`, `frontend/tests/model-*.test.mjs` | 0 | Chrome and Firefox on kind: find, drop, configure with mouse and with keyboard alone; turning a penalty on hides `se` and shows λ; `orders = auto` hides the orders; a disabled group's node is greyed out with the reason | `vue-tsc`, `npm test`, browser |
| **E1** The `xy` chart | The engine-only kind and `interval`'s log axis | `backend/app/reports/definition.py`, `charts.py`, `model.py`, `tests/report_samples.py`, `tests/test_reports_render.py`, `frontend/chart-renderer/presets.sample.json`, `render.test.mjs` | — | One `xy` with each mark renders to PDF and SVG; the existing golden PDFs are unchanged | `ci_tests.sh engine`, `node --test chart-renderer/render.test.mjs` |
| **E2** Common layout, route, Report tab | `layouts/common.py`, `report_texts/common.json`; `/reports/model` and `/tasks/model-report`; the Report tab's view for model nodes | `backend/app/engine/models/layouts/`, `report_texts/`, `api/routes/reports.py`, `tasks/report_jobs.py`, `tests/test_models_layouts.py`; `gateway-go/internal/api/proxy.go`, `server/go_only_routes.go`; `StatReportView.vue`, `statReport.ts` | 0, E1 | Every fixture renders through the common blocks in five languages; PDF and Excel download; a person who cannot open the flow gets 403; asking a PDF after the pages starts no Job | `ci_tests.sh engine`, `go test`, `npm test` |
| **M1** Linear | OLS, WLS, robust SE (HC0–HC3, HAC, cluster); every assumption of its table; `rows` output; the card and its `predict`; its layout | `groups/regression/runner/R/linear.R`, `groups/regression/reference/linear*`, `layouts/linear.py`, `report_texts/linear.json` | 0 (layout: E2) | Every reference value passes in the image (1e-9 against `lm`, `sandwich`, `lmtest`; VIF against `car::vif`; witness `statsmodels` OLS 1e-6); predicting the fitted rows from the card gives the fitted values; the fixture is replaced by the runner's real output and the layout still passes | local self-test, `ci_tests.sh engine` (layout) |
| **M2** Logistic | ML and Firth, logit and probit, robust SE; separation, Hosmer–Lemeshow, Box–Tidwell, VIF, EPV, influence; ROC, calibration, confusion matrix; card and `predict`; layout | `groups/regression/runner/R/logistic.R`, `reference/logistic*`, `layouts/logistic.py`, `report_texts/logistic.json` | 0 (layout: E2) | References pass (witnesses `ResourceSelection`, `pROC`, `statsmodels` GLM); separation with ML stops with its sentence, with Firth it fits | local self-test, engine |
| **M3** Penalties in linear and logistic | `penalty` ridge, lasso, elastic net (mixing given or by CV), λ by CV (min or 1se), grouped folds; no inference; CV curve and paths; card and `predict` | `groups/regression/runner/R/penalized.R`, `reference/penalized*`, `layouts/penalized.py` (the blocks both layouts call when a penalty is on), `report_texts/penalized.json` | 0 (layout: E2) | `cv.glmnet` reproduced exactly with the same `foldid`; a boundary λ gives its note; the report of a penalized fit has no p-value | local self-test, engine |
| **M4** Kaplan–Meier | Strata, log-rank and Peto, pairs with Holm, medians, survival at times, RMST; the `table` output; the number at risk; card and `predict` | `groups/survival/runner/R/kaplan_meier.R`, `reference/kaplan_meier*`, `layouts/kaplan_meier.py`, `report_texts/kaplan_meier.json` | 0 (layout: E2) | References pass (witness `lifelines` 1e-6); the survival image builds and self-tests | local build, self-test, engine |
| **M5** Cox | Efron, Breslow, exact; strata; start–stop; robust SE; Schoenfeld, martingale, dfbetas, VIF, EPV; card with the baseline hazard and `predict` | `groups/survival/runner/R/cox.R`, `reference/cox*`, `layouts/cox.py`, `report_texts/cox.json` | 0 (layout: E2) | References pass; a stratified Cox and a start–stop table reproduce `coxph` | local self-test, engine |
| **M6** ARIMA, manual orders | Orders p, d, q, P, D, Q, s, exogenous columns and future rows, transform, holdout; ADF/KPSS, roots, Ljung–Box, ARCH, Jarque–Bera; forecast and correlograms; card and `predict` (forecast with fixed coefficients) | `groups/timeseries/runner/R/arima.R`, `reference/arima*`, `layouts/arima.py`, `report_texts/arima.json` | 0 (layout: E2) | References pass against `forecast::Arima` (1e-9) and the witness `statsmodels` SARIMAX (1e-4); duplicates in time stop with their sentence; the time series image builds and self-tests | local build, self-test, engine |
| **M7** ARIMA, automatic orders | `orders = auto`: unit-root and seasonal tests, the stepwise search, the candidates' rows, `orders_reason`; the candidates' table in the report | `groups/timeseries/runner/R/arima_auto.R`, `reference/arima_auto*`, the candidates' block in `layouts/arima.py` | M6 | On every reference series the chosen orders equal `forecast::auto.arima`'s with the same settings; the report lists the candidates and says why | local self-test, engine |
| **G** MLflow tracking | The node's MLflow block live; the **gateway** logs (experiment, run, tags, parameters, metrics, artifacts, report PDF, card, `MLmodel`, version); the run page's link; «Continue without MLflow» | As built: `gateway-go/internal/api/jobs_models_mlflow.go` (the call after import), `mlflowconn/tracking.go`, `mlflowconn/mlflowtest/`; `backend/app/api/routes/scripts.py` (`/scripts/objects/read`, `/scripts/results/rows`, the engine only serving the bucket's files); the run page's component. (Planned: `backend/app/models/mlflow_log.py` and `/models/mlflow/log` in the engine; not built) | A, C1, D, M1 | On kind against the test MLflow server: a flow run with a linear model creates experiment, run, tags, parameters, metrics, artifacts and a version; the server stopped fails the step with its sentence (or warns with the option); the Job's environment holds no MLflow variable | `go test`, engine, kind |
| **H** Predict | `model_predict`: the form (connection, experiment, version or latest), the fetch and checks of the card, the Job in `predict` mode, the added columns | `api/jobs_predict.go`, `api/models_mlflow.go` (the browse routes `GET /models/mlflow/{connection_id}/experiments` and `.../versions`), `components/ui/PredictForm.vue`; as built the reading of MLflow is in `mlflowconn/registry.go` and the card is written with the engine's `/scripts/objects/write`; there is no `/models/mlflow/fetch` and no `common/R/predict.R` (each runner has its `predict_*`) | G (and A) | On kind: a version registered by G is chosen by experiment and version and adds `prediction` to new rows; on the training rows it gives the fitted values; a card with a wrong hash or a version without the `tabularia` flavor is refused with its sentence | `go test`, engine, kind, browser |
| **I1** Integration | The browser checks of «Testing strategy» on kind and on minikube, Chrome and Firefox; Compose's «requires Kubernetes»; the three suites whole | — | all | All green | the suites, browser |
| **I2** Documentation | `docs/capabilities/models.md` (the groups; every model with an example on the sample data; the assumptions and how to read them; the output; MLflow; Predict; what it does not do), `docs/scripting.md` (managed environments), `docs/connections/` (MLflow), the capabilities map, `flows.md`, `README.md`, `CHANGELOG.md`; `docs/configuration.md` regenerated; the site | docs | all | `config_reference.py --check`, the site builds and its links hold | site commands |
| **I3** Assistants and MCP | `ai_flow_agent.py` from its copy of the catalogue (the `model` node, its parameters); `prompts.json`, `tools.json` written again | `gateway/app/services/ai_flow_agent.py`, `gateway-go/internal/aiprompts/`, `internal/mcp/` | 0 | An assistant places a configured linear model on a draft | `ci_tests.sh gateway`, `go test` |

### The boosting group's lots (planned 2026-11-20; X0–X9 done the same day)

The contract is «Group `boosting` — Python». The same three rules as above: **X0 first, alone**;
then lanes on disjoint files; one heavy job at a time on this machine; small data in every
acceptance. The image is built **once**, in X0: the runner travels in the ConfigMap, so X1–X3
run their self-tests on that image with `cmd/modelgroup code boosting <dir>` mounted, and only a
change of `recipe.json` asks for another build. The cluster kind `tabularia` is used only by X5's
and X8's acceptances, by whoever runs those lots. The «Agent» column follows the project's rule
for sub-agents (sonnet for the medium work, haiku for the simple, opus for the review).

**Where the lots stand (2026-11-20)**: X0–X9 are **done**.

| Lot | State | Commits |
|---|---|---|
| **X0** Contract and Python runner | **Done** | `49bd8318` |
| **X1** The shared module | **Done** | `2bbf8c04` |
| **X2** XGBoost | **Done** | `25c24dd5` (fixtures again from the runner's output: `ae1b55af`) |
| **X3** CatBoost | **Done** | `4091cd38`, `b0d0018b` (reference cases and witnesses); `5b78ab2d` (start at the class shares); `f0f46c7b` (`l2_leaf_reg` above 0) |
| **X4** Report | **Done** | `cf604909`, `9426a324`, `6c1b1443`; the advice by id `713c8469` |
| **X5** The model file in MLflow and Predict | **Done** | `d4cdd535` |
| **X6** The nodes in the editor | **Done** | `fa4c293c` |
| **X7** Documentation and assistants | **Done** | `d9e155d5`; `57719ba1`, `98714b71` (the catalogue workflow); `6cac7397` (changelog), `8ac0fcd8` (README) |
| **X8** On kind | **Done** | `72cb1ab4`, `4000df9e`, `9e2d5428` |
| **X9** Final review | **Done** | the runner `a4049aac`, `469eeaf3`, `afaaf159`, `dd056acd`, `9a4b7d50`, `c71b8000`, `20b89493`, `a5e18952`, `810d3b74`, `1e4a558c`, `f57b520a`; Go, engine and UI `b67db93a`, `39da4b1c`, `22016095`, `21f095d9`, `916265bc`, `dd2cdc9d`, `1d67933d`, `3ba3e3db`; the witnesses rerun on the final rules, all values agree |

The diagnostics' thresholds were tuned after X4 (`5b78ab2d`): see «As built, the boosting group».

```text
X0 contract, Python runner, recipe, Go plumbing (first, alone)
 ├── X1 boosting_shared.py ──┬── X2 XGBoost ──┐
 │                           └── X3 CatBoost ─┤   (X2 and X3 in parallel, against X1's interface)
 ├── X4 report: xy options, layouts, words ───┤   (fixtures from X0; real outputs after X2, X3)
 ├── X5 model file in MLflow and Predict ─────┤   (end to end after X2)
 └── X6 the nodes in the editor ──────────────┤
                                              ▼
                      X7 docs and assistants ── X8 on kind ── X9 final review
```

**X1's interface**, fixed here so that X2 and X3 start with it: `prepare(task) -> Prepared`
(the input read, rows left out, kinds, categories with their levels, the positional frame, `y`,
`w`, the boolean `validation` mask, the classes, the card's `inputs`, the row numbers);
a library wrapper returns a `Fitted` (`raw_train`, `raw_validation` and `raw_shap_rows` margins,
n or n × k; `curves` {metric: {"train": [...], "validation": [...]}}; `best_iteration`, `trees`,
`stopped_early`, `stopped_by_time`; `gain` {position: value}; `shap(rows) -> (phi, base)` with
phi n × p or n × k × p on the raw scale; `objective`, `stop_metric_name`, `importance_type`;
`model_bytes` and `model_name`); `finish(task, prepared, fitted) -> {rows, output, card,
model_file}` writes everything the section lists from those numbers.

| Lot | Objective | Files (owned) | Depends on | Accepted when (small data) | Check | Agent |
|---|---|---|---|---|---|---|
| **X0** Contract and Python runner | The catalogue entries (group `boosting`, `xgboost`, `catboost`, their kinds, `applies`, `requires`, `forbids`, three variants each with their sections, charts, outputs; `PREDICT` keyed `model:variant`; the new limits); `SECTION_SHAPES` `importance` and `class`; `card.schema.json`'s `boosting`; a results fixture per model and variant; the Python library `common/py/tab_*.py` and its `COMMON_TESTS`; `recipe.json`; `run.py` and three stub modules that refuse «not built yet»; the Go changes of «The runner in Python» (commands by language, `CodeFiles` by language, the managed Python Dockerfile, the check script, `PYTHONHASHSEED`, the step keys, `max_cells`, `time_budget_seconds`, `cmd/modelgroup`) | `backend/app/engine/models/{catalog,results}.py`, `catalog.json` and its three copies, `backend/tests/test_models_catalog.py`, `test_models_results.py`, `backend/tests/fixtures/models/{xgboost,catboost}_*.json`; `gateway-go/internal/modelgroups/` (`groups.go`, its tests, `card.schema.json`, `common/py/`, `groups/boosting/`); `internal/scriptimage/`; `internal/api/{jobs_script_builds,models_build,jobs_models,jobs_predict}.go` (the lines named); `internal/modelcatalog/` (the `model:variant` keys of `PREDICT`, the new limits); `cmd/modelgroup/` | — | `model_catalog.py --check` green; `results.py` accepts the six fixtures and refuses a key a variant does not declare; the R groups' Dockerfiles and the scripting environments' Python Dockerfile byte for byte as before; every `.py` of the tree is in `CodeFiles`; **one image build** from the recipe with `cmd/modelgroup` passes `pip check`; `run.py --selftest` passes the common tests (labels of the code-like names, the ten columns and their types, `error.json` and exit 3) and exits 4 when a test is made to fail; `go test ./internal/...` | the commands of «Commands» for the catalogue and `go test`; one local image build | sonnet |
| **X1** The shared module | `boosting_shared.py`: `prepare`, the three ways of splitting, the refusals, the metrics by hand, the baselines, the ten diagnostics, the importance rows, the SHAP sample and its cost rule, the beeswarm's points and positions, the curves, ROC, calibration, histograms and residual points with their caps, the card's `boosting` object, the rows output | `groups/boosting/runner/py/boosting_shared.py`; `reference/make_witness.py` (the metrics' part) | X0 | Its tests, registered in `COMMON_TESTS`, pass in the image on synthetic arrays: each split's sizes and its no-time-on-both-sides rule; each diagnostic at both sides of each threshold; the beeswarm's positions within ±0.4 and identical on a second run; the metrics against scikit-learn in the witness at 1e-10 | local self-test, witness | sonnet |
| **X2** XGBoost | `fit_xgboost`, `predict_xgboost`; `QuantileDMatrix`, native categories, early stopping and slicing, the time-budget callback; `pred_contribs` (first day: with categorical splits); the numpy tree walker; the gain from `loss_changes`; the model file; reference cases and `make_reference.py` | `runner/py/boosting_xgboost.py`; `reference/data/*`, `expected.json` (its cases), `make_reference.py` (its part) | X0, X1 | Every row of «Reference values» for XGBoost in the image; determinism at 2 threads; 1 against 2 threads measured and written in the spec; the model file's size for 1,000 trees of depth 6 measured and written | local self-test, witness | sonnet |
| **X3** CatBoost | `fit_catboost`, `predict_catboost`; `cat_features`, the missing sentinel, `has_time`, `allow_writing_files = False`, the stop metric and the training AUC, the callback; `ShapValues` (its shape for multiclass, assumed n × k × (p + 1)); the `cbm` file | `runner/py/boosting_catboost.py`; its cases in `reference/` | X0, X1 | Every row of «Reference values» for CatBoost; the witness's oblivious-tree walker and `PredictionValuesChange` on the numeric-only case; determinism; nothing written outside `/tmp` (the read-only root proves it) | local self-test, witness | sonnet |
| **X4** Report | `Series.color` and `options.y_categories` for `xy` (definition, check, renderer, samples); `layouts/{boosting,xgboost,catboost}.py`; `report_texts/{boosting,xgboost,catboost}.json` in five languages | `backend/app/reports/definition.py`, `charts.py`, `tests/report_samples.py`, `tests/test_reports_render.py`; `frontend/chart-renderer/{render.mjs,presets.sample.json,render.test.mjs}`; `backend/app/engine/models/layouts/`, `report_texts/`, `backend/tests/test_models_layouts.py` | X0 (fixtures); X2, X3 to replace the fixtures by real outputs | Every fixture renders in five languages and passes `reports.definition.check`; the beeswarm sample renders to PDF and SVG with the names on the y axis; the golden PDFs unchanged; the first page holds the diagnostics and the sentence that they are not tests | `ci_tests.sh engine`, `node --test chart-renderer/render.test.mjs` | sonnet |
| **X5** The model file in MLflow and Predict | The upload of `xgboost.json` / `catboost.cbm`, the flavor's `model_file`, the tags `tabularia.variant` and `tabularia.model_file_sha256`, the metric histories; `Tracker.Open`; the engine's `POST /scripts/objects/upload`; Predict's reading of `model_file`, the checks, `TABULARIA_MODEL_FILE_IN`, the clean-up; `CheckPredict` by variant; Predict's form for the new columns | `gateway-go/internal/api/{jobs_models_mlflow,jobs_predict,models_mlflow}.go`, `internal/mlflowconn/{tracking,registry}.go` and `mlflowtest/`; `backend/app/api/routes/scripts.py` (the upload route) and its tests; `frontend/components/ui/PredictForm.vue` | X0 (end to end: X2) | `httptest`: the file uploaded and streamed back, a wrong sha256 refused with its sentence, a name that is a path refused, a file over the limit refused, no file held whole in memory; the engine's route refuses a wrong hash and leaves no object; on kind, after X2: a registered XGBoost version applied by Predict gives the fitted predictions bit for bit | `go test`, engine module tests, `vue-tsc`, `npm test`, kind | sonnet |
| **X6** The nodes in the editor | The palette's subtitle *Boosting*, the two icons, `task` first in the form, the new kinds' controls (all existing controls), the five languages of `models.xgboost`, `models.catboost` and their parameters | `OpSidebar.vue`, `useOpIcons.ts`, `ModelForm.vue` (only if a kind needs it), `frontend/i18n/locales/*.gen.ts` (namespace `models`), `frontend/tests/model-*.test.mjs` | X0 | Chrome and Firefox: find, drop and configure each node with mouse and with keyboard alone; `task = binary` shows `event_value` and `threshold`; `validation = time` asks for `order_by`; `stop_metric = auc` is refused with `task = regression`, with its reason | `vue-tsc`, `npm test`, browser | haiku |
| **X7** Documentation and assistants | «Assistants and documentation» of the section | `docs/…`, `README.md`, `CHANGELOG.md`; `gateway/app/services/ai_flow_agent.py` (prompt only), `internal/aiprompts/`, `internal/mcp/` (regenerated) | X2–X6 | An assistant places a configured XGBoost binary node on a draft; `config_reference.py --check`; the site builds, links hold | `ci_tests.sh gateway`, `go test`, site commands | haiku (docs written from the spec), sonnet (prompts) |
| **X8** On kind | Enable *Boosting*: Building → Checking → Ready with its lock; a reference value altered on a scratch branch leaves it Failed; the R groups' recheck after X0's `RunnerHash` change passes; each node, each task: Run, Table, Report, PDF, Excel; a flow run logged to the test MLflow server with the model file and the metric histories; Predict from that version; the Job without internet and without MLflow variables; durations measured on the reference tables and on 100,000 generated rows, written in the spec | — | all | All green, Chrome and Firefox | the suites, browser | sonnet |
| **X9** Final review | The section against the code; the diagnostics' thresholds; the security of the model file's path | — | X8 | Corrections committed, the spec's «As built» blocks written | the suites | opus |

### The parametric survival lots (planned 2026-11-20)

The contract is «The node «Parametric survival»». The same rules as above: **P0 first, alone**;
then lanes on disjoint files; **one heavy job at a time** on this machine (16 GB: the image build,
the engine suite and the gateway suite each run alone); small data in every
acceptance. The image is built **once**, in P0: the runner travels in the ConfigMap, so P1–P3, P8
and P9 run their self-tests on that image with `cmd/modelgroup code survival <dir>` mounted, and
only a change of `recipe.json` asks for another build. The cluster `tabularia` (kind) is used by
P11 only. The «Agent» column: **sonnet** for the medium lots, **opus** for the difficult ones (the
numerics of P1, the final review), haiku for the simple ones.

**Where the lots stand (2026-11-21)**: **P0, P1, P2, P6, P7, P8 and P9 done** (below; P3 and P5 have their blocks), the others **planned**; the final review's findings (P12) are corrected: see «As built, P12 fixes» at the end.

**As built, P0** (2026-11-20; commits are on `main`, `git log --oneline -8` after `ee6e7fdf`):

- **Catalogue**: `parametric_survival` after `cox` (the models are listed in the order of their
  groups, so the snapshot's and the sentences' order changed: `cox, parametric_survival, arima`);
  kinds `predictors_optional` (`columns`, `min` 0), `distribution`, `quantiles` (`numbers`, above 0,
  below 1, at most 9); `LIMITS.max_quantiles`; `SECTION_SHAPES.parameter`; the four variants
  `ph_aft`, `aft`, `ph`, `auto`; the candidate section's stats include `parsimony` and the text
  `status`; Predict's `quantiles`, `at_times`, `interval` and `columns.parametric_survival`. The
  charts declared are `time_ratio_interval`, `hazard_ratio_interval`, `fit_km`, `cox_snell`,
  `profile_survival`, `hazard`, `deviance_linear`, `loglog` (Weibull, exponential, `auto`),
  `candidates` (`auto`); the curve stats are `surv`, `lower`, `upper`, `fitted`, `hazard`, `y`
  (P1–P3 refine them if they need to: a variant declares what the runner may write). 13 shared
  parameter cases; the four fixtures hold **placeholder numbers**, from
  `parametric_survival-*.json`, for P5 until P2 and P3 give real output.
- **Card schema**: `parametric_survival` object, required for the model (with `outcome`, `terms`,
  `coefficients`).
- **Runner**: the route in `run.R` and a stub `parametric_survival.R` that refuses
  «Parametric survival is not built yet in this version of Tabularia» (exit 3, `error.json`).
- **A deviation from the plan**: a Job's ConfigMap would have passed the 900 KiB the groups' test
  allows (the regression group: 959,237 bytes) because the catalogue copy grew by 23 KB to 118 KB.
  The Job now receives the catalogue **without its indentation** (`modelcatalog.Compact()`: the same
  JSON, 52 KB); the snapshot and its three copies are unchanged. Every group's runner hash changes
  (their versions in use run their self-test again on the same image).
- **The editor**: the palette leaves the node out and the words test does not ask for its words,
  until P6 (`NOT_IN_THE_PALETTE_YET` in `frontend/composables/modelCatalog.ts`, `WORDED` in
  `frontend/tests/model-catalog.test.mjs`, `PENDING` in `backend/tests/test_models_layouts.py`:
  P5 and P6 take them away).
- **The assistants** (`prompts.json`, `tools.json`) are not regenerated: P10's.
- **The image**: one build with `cmd/modelgroup dockerfile survival`, `TABULARIA_JOBS=2`, 8 cores,
  16 GB: **797 s (13 min 17 s)** from `docker build` to the end, of which the package step took
  about 790 s (arrow 190 s, `rstpm2` 98 s, `RcppArmadillo` 21 s, `stringi` 63 s, `dplyr` 12 s, `flexsurv`
  30 s); the image is **1,525,823,308 bytes (1.53 GB)**, against 1,408,161,895: **+117 MB, +8 %**
  (the estimate was +74 MB and +5 %). No build step ran out of memory (no limit was set on the
  local build; the Job's `4Gi` is not tested by P0, P11 does). `library(flexsurv)` loads as user
  10001 with a read-only root; the self-test of Kaplan–Meier and Cox passes with **35 checks ok,
  none failed or skipped**, their reference values unchanged; the stub exits 3 with its sentence.
- **Licences, read in the image** (`packageDescription(p)$License` for the 51 packages, `stringi`'s
  `LICENSE` file): every one is open source. GPL: `flexsurv`, `deSolve`, `mstate`, `muhaz`, `mvtnorm`,
  `numDeriv`, `quadprog`, `Rcpp`, `RcppArmadillo`, `bbmle`, `rstpm2`, `statmod`, `assertthat`,
  `bit`, `bit64`; LGPL: `survival`, `bdsmatrix`; MPL-2.0: `data.table`; Apache-2.0: `arrow`,
  `RColorBrewer`, `utf8`; MIT for the rest. **`stringi`** is «file LICENSE», marked FOSS by R: its
  own 3-clause BSD, with the ICU4C library's licence (Unicode, Inc.; IBM's ICU licence for its older
  parts) and the licences of what ICU bundles; the file also holds the notice of a **LaTeX word-count
  algorithm taken from Kile (GPL ≥ 2)** used by `stri_stats_latex`, which the runner never calls.
  No licence forbids use as the recipe is used (downloaded from CRAN and built in the administrator's
  cluster, no binary shipped); the owner may want a lawyer's eye on the combination of `stringi`'s
  bundled texts only if the image is ever distributed.

**As built, P1** (2026-11-20; commits `30aa295c` and `0b895d6e` on `main`, not pushed):

- **Runner** (`groups/survival/runner/R/parametric_survival.R`): the interface fixed above, with
  these signatures: `ps_prepare(task)`; `ps_fit(prepared, distribution, starts = NULL, hessian =
  FALSE, fail_first = FALSE)` → `converged`, `status`, `attempts`, `par` (the optimiser's vector),
  `loglik`, `loglik_rows(par)` (each row's log-likelihood, unweighted), `loglik_fn(par)`, `fit`
  (flexsurv's object), `spec`; `ps_newton(fitted)` (below) → `par`, `loglik`, `vcov`;
  `ps_covariance(fitted, par)` → V or NULL; `ps_card`, `ps_model(card)` (what Predict reads);
  `ps_predict(model, X, strata, quantiles, times, interval, alive_at = NULL, hazard_times)` →
  median, quantiles, S(t) and the hazard at `hazard_times`, each with `_low`/`_high`;
  `ps_write(task, prepared, fitted, rows, model)` (sections `model`, `coef`, `parameter`,
  `stratum`). **Hooks**, found by name so that a lot adds its own file: `ps_checks(task, prepared,
  fitted, model, rows)` (P2), `ps_auto(task, prepared)` returning `list(fitted, write(rows))` (P3;
  until then `distribution = auto` refuses `not_built`), `alive_at` (P9; refuses `not_built`).
- **Three things the plan did not say**, found by the reference values:
  1. **Newton steps after flexsurv's BFGS** (`ps_newton`, at most 4, each with the Richardson
     gradient and Hessian, until a step is below 1e-8 standard errors, accepted when the
     log-likelihood does not fall by more than its rounding). BFGS stops on the function's relative
     change, so its estimates are known to about the square root of `reltol`: **2.5e-9 relative**
     from the exponential's closed form, **1e-4 relative (1e-4 standard errors)** from a Gompertz
     maximum with a predictor of values near 50, `reltol` 1e-15 notwithstanding. After the steps the
     closed forms hold to 1.2e-12. The flexsurv object kept for `native.rds` and the card check gets
     the polished estimates and the runner's covariance (`ps_native`: `res`, `res.t`, `cov`, `loglik`).
  2. **The Hessian is taken with the covariates centred** (the location's and the shape's
     intercepts at the columns' means, `par = A · par_c`) and brought back exactly, `V = A V_c A'`:
     uncentred, a predictor far from 0 makes it ill-conditioned (standard errors **7.2e-7** relative
     from survreg's analytic ones on `ps_all_events`, a temperature near 20; centred, below 1e-8).
  3. **The runner's log-likelihood** is Σ w (δ log f(t) + (1 − δ) log S(t)) with the distribution's
     `d(log = TRUE)` and `p(lower.tail = FALSE, log.p = TRUE)`, not log h − H: the same function,
     exact in the tails (flexsurv itself uses log(1 − F)). Checked against flexsurv's at the BFGS
     estimates in every fit (1e-10 relative; a gap stops the Job as a bug).
- **What else the code decides**: the strata's location effects are in the card's `order` but
  not in `coef` (the profile per stratum gives them in time units); the concordance's score is
  the location's linear predictor turned for the exponential **and** Gompertz (both on the rate);
  `n` counts rows (weights only weigh); `hr_interval_scale` is written only when a Weibull hazard
  ratio's interval and its p-value disagree on 1; the extrapolation warning of Predict reads each
  row's stratum's last time; the notes `zero_times`, `zero_weights` and `quantile_infinite` carry
  their numbers in the sentence (P5 may want them as numbers); `software` writes «survival 3.8.12»
  as every R node (`packageVersion`). `card.schema.json`'s `parameters` said «the location last»:
  it is flexsurv's order (`meanlog, sdlog`; `mu, sigma, Q`), the description corrected.
- **The rows output** writes the intervals too: `pred_median_low`/`_high`,
  `pred_quantile_<p>_low`/`_high`, `pred_surv_at_<t>_low`/`_high` (the catalogue's `outputs.rows`,
  descriptive, does not name `pred_median_low`/`_high`: not changed, a line for P5 or P10). Predict
  from the card read back from its JSON gives the rows output's numbers **identical to the bit**
  (the linear predictors and the delta method's quadratic forms are summed column by column, no
  BLAS). Refusals added to Cox's: `too_few_rows` («the model has 7 parameters and 8 rows it can use:
  it needs at least 9 rows (the parameters + 2)»), `stratum_no_events`, `negative_weight`,
  `weights_type`, `not_converged`, `distribution`.
- **Reference values** (`make_reference_parametric.R`, sourced by `make_reference.R`; six tables:
  `ps_exponential` 90 rows serving both exponential cases, `ps_weibull` 240, `ps_gompertz` 200,
  `ps_gompertz_cure` 300 with `rate` 0.035 so that S(∞) = 0.61 leaves the median empty,
  `ps_all_events` 80, `ps_small` 8 for the refusals): 16 cases, 632 values; Kaplan–Meier's and Cox's
  16 cases **unchanged** (compared case by case). The reference side brings flexsurvreg to its
  maximum with the same Newton steps on flexsurv's own likelihood and the centred Richardson
  Hessian; survreg is called with `rel.tolerance` 1e-13 (its default, 1e-9, leaves estimates
  5e-7 apart). **Tolerances and what was measured**:

  | Against | Tolerance | Largest measured |
  |---|---|---|
  | closed forms (exponential: rate, ℓ, se = 1/√d, median, quantile) | 1e-10 | 1.2e-12 |
  | every estimate, against flexsurv, survreg or the hand-written Gompertz | **1e-6 standard errors** (abs), 1e-9 relative; exp(β) and interval ends 1e-6 · se relative | 4.5e-7 se (a z statistic of the generalized gamma) |
  | ℓ, AIC, BIC, LR statistics, against flexsurv | 1e-9 (LR 1e-8) | 4.1e-15 |
  | profile's median, quantiles, S(t), against flexsurv | **1e-8** | 1.4e-9 |
  | standard errors, against flexsurv's likelihood's own Richardson Hessian | **1e-6** | 1.4e-7 (generalized gamma: its incomplete gamma function); 1e-9 to 1e-8 the others |
  | survreg: standard errors, hazard ratio and median intervals | 1e-6 | 1.2e-7 |
  | survreg: ℓ; the profile's median | 1e-9; 1e-8 | 5.5e-16; 7.2e-10 |
  | coxph's hazard ratios beside | 1e-9 | 4.9e-15 |
  | Gompertz written by hand (nlm, then Newton) | 1e-6 and 1e-6 se | 1.3e-10 |

  The spec's 1e-9 relative on every estimate against flexsurv cannot hold: two maxima of the same
  likelihood found by Richardson gradients agree to about 1e-8 standard errors, which is 5e-7
  relative for a coefficient near 0 (0.0013 with a standard error of 0.011). The worst value is at
  45 % of its tolerance.
- **Self-test**: **56 ok, none failed or skipped** (the 35 of Kaplan–Meier and Cox, 5 runner tests,
  16 reference cases); regression **63 ok**, time series **25 ok**, customer lifetime **15 ok**
  after the change to `common/R/card.R`; `go test ./internal/...` green. The runner tests: column
  names that look like code (identical numbers, the labels back) and Predict equal to the rows
  output to the bit, an unseen level and stratum and an empty stratum giving empty predictions,
  counted; the median's delta-method interval against `predict(survreg, type = "uquantile", se.fit
  = TRUE)` (Weibull and log-normal, alpha 0.1: within 1e-6); `native.rds` without a row (searched
  recursively) and `summary(newdata)` equal to the card's; a first attempt forced to fail
  (`.selftest_fail_first`, self-test only) ending at the same log-likelihood (1e-9) with the note,
  for Weibull, log-normal, generalized gamma and Gompertz; Predict's defaults from the card, its
  own quantiles and times, the extrapolation warning, a card without `vcov`, the clashes. Inside
  every self-test fit, the card read back gives flexsurv's `summary(newdata)` quantiles and
  survival of the fitted rows within 1e-9 (all six distributions, with and without strata).
  **Not exercised**: the refusal `not_converged` (both attempts failing): P3's `ps_auto_failing`.
- **Witness** (`make_witness.py`, lifelines 0.30.3): the four AFT fitters on the cases without
  strata, scipy's `gtol` brought down to 1e-9 (its default stops 1e-2 from the maximum; the
  generalized gamma reaches only 1e-7): **79 values within 1e-5 relative or 1e-3 standard errors**
  (largest 3.8e-5 relative, 9.3e-6 standard errors). Said apart: the log-logistic's estimates, as
  lifelines stops **2.2e-8 relative below R's log-likelihood** (the same likelihood evaluated at
  both points: its optimiser stops short); the generalized gamma's standard errors, which lifelines'
  experimental fitter gives up to 5.6 times off both R constructions (which agree to 1.4e-7). The four
  Kaplan–Meier restricted means that differ were there before P1 (the default `rmst_tau`).
- **Durations** (local image, 5,000 generated rows, the whole Job with Parquet in and out): Weibull
  with two strata, `rows` output with intervals and `native.rds`, **7.2 s**; generalized gamma
  **15.9 s**; Predict with intervals on the 5,000 rows 3.3 s and 5.1 s (about 2 s of each is loading
  flexsurv's imports). Cards of 3.1 and 3.5 KB valid against `card.schema.json`; `native.rds`
  6.3 KB.
- **Ready for the next lots**: P2 has `loglik_rows` for dfbetas (V · Uᵢ), `vcov`, `ps_cox`'s frame,
  `ps_predict`'s `hazard_times` for the hazard chart, the profile; P3 has `ps_fit(…, starts =
  list(weibull, lognormal), hessian = TRUE)` and `ps_refuse_too_few_rows` per candidate, the card's
  `selection`; P5 can take real outputs for the sections above (checks and points after P2,
  candidates after P3); P9 has the card's numbers and `ps_predict`'s `alive_at` argument.

**As built, P2** (2026-11-20; commits on `main`, not pushed; files `groups/survival/runner/R/parametric_survival_checks.R`, `reference/make_reference_checks.R`; the catalogue, `predict.go`, `modelCatalog.ts` for the intervals' columns):

- **What the owner changed on the way** (the section «The checks» is rewritten to say it): **no constant of ours makes a verdict.** A verdict (`ok`, `failed`, `not_computed`, with `h0`, `advice`, `advice_id`, `threshold`) comes only from a test with a p-value against the node's `alpha` (its option, default 0.05, also the level of every interval, written in the model section as the number `alpha`): `distribution_vs_gengamma`, `constant_hazard` (exponential only), `ph_cox` and `ph_cox_global` (`cox.zph`, Efron's ties, the weights, the strata, for Weibull, exponential and Gompertz with predictors); `threshold` carries alpha and there is no «doubtful». The only other verdicts are facts: `separation` (one row per level with no event, or one ok row) and `convergence` (the Hessian positive definite; `value` is the Newton step left at the estimates, in standard errors, read as a number). **Everything else is a measure**, rows with numbers and a `description` and no verdict, no advice: `km_agreement` (per group `value` the largest gap in survival, `at` its time, `statistic` the gap in Greenwood's standard errors, `count` the rows), `dfbeta` (per term `value` the largest |dfbetas|, `rows` the five most influential, whatever their size), `epv` (`value`, `count` events, `df` parameters), `vif` (GVIF^(1/df) per term, two terms or more), `extrapolation` (per stratum `value` the largest number asked over the last time, `at` that time). Lot P4 is dropped; `PS_CHECK_THRESHOLDS` is gone. What stays in one list, `PS_CHECK_LIMITS`, is what the measures read and draw, not a verdict: Kaplan–Meier points with fewer than 10 rows at risk are not read, the agreement is read at 60 of its times at most, a categorical predictor of more than 6 levels is not a grouping, 100 points a curve, 5 rows named, 3 points labelled, and `gengamma_max_rows` (5,000).
- **Declared in the catalogue** (`_ps_assumptions`): the `assumption` numbers gain `at` and the texts `description`; the model numbers gain `alpha`. P5's layout reads the measures by their ids (`MEASURES`).
- **Charts' points**, as «Chart ids» says, one thing more: the grid of a curve is `min(100, max_chart_points)` times; `fit_km` has Kaplan–Meier's steps from time 0 (censoring times are steps too, of height unchanged) with the log-log band at 1 − alpha (clamped to 1 − 1e-12 for `survfit`), `fitted` the mean fitted survival of the group; `cox_snell` x = r, y the Nelson–Aalen estimate of (r, δ) at r; `loglog` (Weibull and exponential only) Kaplan–Meier's points where 0 < S < 1 and the fitted mean curve; `profile_survival` from 0 with its band, `hazard` from the first grid time (no time 0: h(0) may not be finite); `deviance_linear` against the location's linear predictor, the three largest named, only with predictors.
- **Light** (the owner, 2026-11-20: «niente cose che in run time ci metterebbero eoni»): no refit per row, no bootstrap, no simulation. The influence is `V · Uᵢ` with `Uᵢ` from central differences of the vectorized row log-likelihood (two evaluations of all rows per parameter), one matrix product: against `residuals(survreg, type = "dfbetas")` **1.6e-10**; **survreg's is unweighted** (a weighted row is one row), so is ours. **Costs** (synthetic Weibull tables, a number and a category of three levels, 1 core of the dev machine, the whole Job in R without Parquet): 1,000 rows, the checks 1.2 s against the fit's 1.8 s; 10,000 rows 0.7 s against 1.8 s; 100,000 rows 2.5 s against 18 s (km_agreement 0.5 s, ph_cox 0.65, dfbeta 0.6, convergence 0.6, charts 1.4). **The one check that costs more than the fit is the generalized gamma's** (its fit, brought to its maximum by the Newton steps: 0.7 s at 1,000 rows, 10 s at 10,000, 92 s at 100,000, against the Weibull's 2 s and 18 s): above `gengamma_max_rows` = 5,000 it is `not_computed` with its reason («fit gengamma, or auto, to see it»), and always when half of the step's `time_budget_seconds` has gone. The Weibull of `constant_hazard` is as costly as the node's own fit. The first version took the generalized gamma's log-likelihood from BFGS alone: its LR statistic was 0.0109 short of the maximum's on the 240 rows (0.6618 against 0.6726), so both fits of the two LR checks go through `ps_newton`.
- **Reference values** (`make_reference_checks.R`, sourced by `make_reference.R`; cases `ps_checks_weibull`, `ps_checks_exponential`, `ps_checks_gompertz`, `ps_checks_all_events`, 71 values): dfbetas against `survreg` (1e-6; the rows named exactly), `ph_cox` against `cox.zph` on the same Cox fit (1e-9, p-values 1e-8), the LR statistics of the gengamma (df 1 for the Weibull, 2 for the exponential) and of the constant hazard against flexsurv's polished fits (1e-8), `km_agreement` from a second construction (`survfit`, `summary.survfit`'s standard error, flexsurv's `summary(newdata)` averaged by the weights; 1e-6), events per parameter, the extrapolation against flexsurv's quantiles (1e-8). **`cox.zph` refuses integer weights** (a bug of `survival` 3.8-12); the runner's are numeric.
- **Self-test**: the runner's tests of the checks (3, in `parametric_survival_checks.R`): each p-value check on both sides of its p by moving alpha (alpha twice p fails it, half of p passes it, the p-value unchanged, `threshold` = alpha), a measure has no verdict and a description, separation, the influence of one outlier named first, collinear predictors said, extrapolation said, crossing hazards failing `ph_cox`, the time of the step and the row limit making the generalized gamma `not_computed`, convergence without a covariance; what is not written (the log-normal has neither `constant_hazard` nor `ph_cox` nor `loglog`; Gompertz neither `distribution_vs_gengamma` nor `loglog`; no predictors, no `ph_cox`, `dfbeta`, `vif`, `deviance_linear`; no category, no `separation`; strata and a predictor of 8 levels as groups); the charts exactly on a table of six rows (Kaplan–Meier's steps, the fitted curve exp(−λt), Cox–Snell and its Nelson–Aalen by hand, the log-log points and line, the profile, the hazard) and capped on 600 rows at 50 points with the largest deviance residual kept and named. Every written row passes `tab_check_declared`. With the 36 cases of the group (the 35 reference cases of Kaplan–Meier, Cox and P1 and the four of this lot) the self-test is **all ok** on the tree of this lot (P3's cases and P1's `note_quantile_infinite`, which P5 turned into `@quantile_infinite`, are theirs: the reference file of P1 was not regenerated).
- **The intervals' columns** (owner's request): `rows` and Predict wrote `_low`/`_high` for the median, each quantile and each time, which the catalogue did not name. The catalogue now declares `pred_median_low`/`_high`, `pred_quantile_*_low`/`_high`, `pred_surv_at_*_low`/`_high` in `outputs.rows` (the engine's clash check expands the `*` with the node's `quantiles` and `times`: a column the input already has is refused before the run) and, for Predict, the keys **`quantiles+interval`** and **`at_times+interval`** of `PREDICT.columns` (a key `a+b` is given when both options are; its `*` is one per entry of `a`), read by `PredictColumns` (Go) and `predictColumns` (interface): the form lists them and the gateway's clash check with the columns the model reads sees them. Tests in the three languages.
- **The four differences of the witness** were the witness's own definition: `make_witness.py` read Kaplan–Meier's restricted mean up to the largest time of the whole table, the runner (and `survRM2`) up to the least of the strata's largest times; a stratum with a shorter follow-up was then read past its last time, lifelines carrying the last step on. With the runner's horizon the four (`kaplan_meier_peto` A and B, `kaplan_meier_crossing` basic and premium) agree to 3e-14; the witness exits 0 (253 values compared; Kaplan–Meier 103, largest 3e-14; Cox 71, 1.1e-7; parametric 79, 3.8e-5). It also skips the cases without a log-likelihood of their own.
- **The group's code and the ConfigMap**: `expected.json` is written compact, 13 digits and without the default tolerance (1e-9), 139 KB instead of 264 KB; the group's code is still **955 KB, over the 900 KiB the test allows** (the code counts twice, code and code with the reference): the R files' comment-only lines are 60 KB of 304 KB (120 KB counted twice). Not solved here.

**As built, P7** (2026-11-20; commit `6affe6f6` on `main`):

- **Read back**: `modelResultRows` asks the engine for the sections `candidate` and `parameter` too
  (the engine's `/scripts/results/rows` takes any section name: no change in Python).
- **Metrics** (`modelMetricsChecked`): every finite number of the `candidate` section is
  `candidate.<level>.<stat>`, **all of them**, not only the four the plan names: `aic`, `bic`,
  `log_lik`, `akaike_weight`, and also `n_parameters`, `delta_aic`, `converged`, `admissible`,
  `chosen`, `parsimony` for the distributions; `aicc`, `aic`, `bic`, `converged`, `chosen` for
  ARIMA's orders (their level, e.g. `ARIMA(1,1,0)(0,1,1)[12]`, becomes `ARIMA_1_1_0__0_1_1__12_`).
  The text `status` is not a metric; a null value (a candidate that did not converge) gives none.
  Only the `estimate` of the `parameter` section is logged, as `parameter.<level>[.<level_2>].estimate`.
  Keys go through the same cleaning, 250-character cut and collision suffix as the others.
- **Parameter**: `distribution_chosen` = the `model` section's text `distribution` (the one the fit
  used, also with `auto`); the node's own `distribution` stays as the node says it. Any model whose
  results have that text gets it.
- **Tags, artifacts, version**: nothing new: the same tags (`tabularia.model` = `parametric_survival`,
  group, variant, card sha256), files (`results.parquet`, `coefficients.csv`, `report.pdf`,
  `MLmodel`, `model.json`, `native.rds`) and the registered version as every model.
  `mainMetrics` was P0's.
- **Tests** (`jobs_models_mlflow_test.go`): the exact keys on inline rows; keys within 250
  characters and kept apart; the DB test of a whole run (the fake engine now also returns a
  candidate, a parameter and the distribution, and the fake MLflow holds them); and one that reads
  `backend/tests/fixtures/models/{parametric_survival-*,arima-*}.json` and checks **structure, not
  numbers** (every finite candidate number is a metric of the same value, no key over 250, no
  collision, `distribution_chosen` present): it is skipped when the fixtures are not mounted (the
  `test.sh` image mounts only `gateway-go`), and holds when P5 replaces the placeholders.
- **Checked** in a clean worktree (HEAD + the two files), as the tree held B0's half-done
  `customer_lifetime` group, whose missing recipe makes `modelgroups` panic: `test.sh` all green
  (`internal/api` with the database, 27 s).

**As built, P3** (2026-11-20; commits `829f4906` (the option) and `64d3af54` (the choice) on `main`, not pushed; files `groups/survival/runner/R/parametric_survival_auto.R`, `reference/make_reference_auto.R`, `reference/data/ps_auto*.csv`):

- **The option `aic_margin`** (owner, 2026-11-20: «niente cose hardcoddate»): a node option, kind `aic_margin` (a number from 0 to 10), default 2.0, only with `distribution = auto` (`applies`); the `auto` variant's `model` section declares the number `aic_margin`, which the runner writes; its words (`models.params.aic_margin`, `models.hints.aic_margin`) in five languages; 5 new shared cases in `params_cases.json`. 0 takes the lowest AIC always. The margin of the rule and of the note is this option, never a constant.
- **The rule and the choice** (`ps_auto_choose`, pure, tested on made-up AICs): the lowest AIC (tie: fewer parameters, then the order); a nested distribution (exponential in Weibull; exponential, Weibull, log-normal in the generalized gamma) admissible and within the margin (inclusive) of the lowest wins, the simplest of them; **not chained** (the exponential within the margin of the Weibull but not of the generalized gamma does not move a generalized-gamma choice). Gompertz has nothing nested in it by the spec's list (the exponential is in fact Gompertz at shape 0: not used; a question for the owner).
- **Lightness** (owner: «stiamo leggeri»): **no Hessian for a candidate**: they are compared by log-likelihood, AIC and BIC; admissible = converged with a finite log-likelihood (the status `hessian` of the plan is not written; the Richardson Hessian is taken for the chosen fit only, by `ps_newton`, and `convergence` fails if it is not positive definite). **Order** by cost: exponential, Weibull, log-normal, log-logistic, Gompertz, then the generalized gamma (started from the Weibull's and log-normal's fits just made); the plan's order had Gompertz third. The second start only when the first fails (`ps_fit`'s own).
- **Time**: `task.limits.time_budget_seconds` is the step's configured timeout (the gateway writes it: `jobs_models.go`, nothing invented); before each candidate, when more than **half** of it has passed (counted from the runner's process start; the half is the plan's «more than half», a named constant `PS_AUTO_TIME_SHARE`), it and the later candidates are not fitted: status `time`, note `auto_time`. The self-test gives a clock (`task$.selftest_clock`).
- **What it writes**: section `candidate` for every candidate: `n_parameters` always; `log_lik`, `aic`, `bic` when finite (also for a candidate that did not converge); `delta_aic`, `akaike_weight` for the admissible; `converged`, `admissible`, `chosen`, `parsimony` (1 only on the chosen when the rule moved the choice), text `status` (`ok`, `not_converged`, `time`, `too_few_rows`: the last one added, for a candidate with fewer rows than its parameters + 2; the exponential without them refuses `too_few_rows` for the whole node). The chosen candidate's log-likelihood is the node's own, to the bit (the row reads `model|||log_lik`). The **lowest AIC is taken to its maximum** (`ps_newton`) when the rule moves the choice: BFGS stops short of the maximum on the generalized gamma by up to 0.012 in log-likelihood (0.03 in a ΔAIC); the other candidates are left at BFGS (Weibull, exponential, log-normal, log-logistic, Gompertz: 1e-7 relative or better; the generalized gamma 2e-5), so a ΔAIC or a weight of a candidate other than the chosen and the lowest is good to about 0.05 and 0.005. `no_candidate` when none is admissible («no distribution could be fitted to these rows (exponential: …; …): fewer predictors, merge rare levels, or check the times»).
- **Notes, in P5's form** (`@id name=value …`, the report words them): `auto_close` only when the rule moved the choice (`distribution` the chosen, `other` the lowest AIC, `margin`, `delta_aic` and `weight` of the chosen; one note, not one per close candidate: the plan's «every other admissible candidate within 2» is dropped; the report's sentence is the parsimony sentence), `auto_not_converged names=a,b`, `auto_time names=a,b`. The sentence `auto_parsimony` of the first draft is gone. In `parametric_survival.R`: `@second_start`, `@weights_counts`, `@quantile_infinite share=12.5`.
- **The chart `candidates`**: `curve`, level `candidates`, `level_2` `km` (steps) and each admissible distribution, stat `surv`, 100 times from 0 to the last observed time, the mean of S(t | x) over at most 5,000 equally spaced rows (no random choice).
- **Reference cases** (12 more checks in the self-test: **70 ok, none failed**, the 56 of P1 and P9's unchanged): `ps_auto_parsimony` (table `ps_auto.csv`: 240 rows, a Weibull AFT with a number and a category, 161 events, seed 439 found by looking among 500 seeds; the generalized gamma has the lowest AIC 1276.972, the Weibull 1277.290, **ΔAIC 0.318, weights 0.539 and 0.46**, every other candidate above 12: the Weibull is chosen and marked, the note `@auto_close distribution=weibull other=gengamma margin=2 delta_aic=0.32 weight=0.46`), `ps_auto_margin` (the same with `aic_margin` 0.1: the generalized gamma, no mark), `ps_auto` (the 240 rows of `ps_weibull`, weights, no strata: Weibull, no mark; generalized gamma ΔAIC 1.33, weight 0.34), `ps_auto_strata` (with a shape per plant: Weibull; ΔAIC of the generalized gamma 1.71), `ps_auto_failing` (`ps_auto_failing.csv`: 23 rows, 10 events, no predictors, seed 54, times rounded to 2 decimals so that the CSV holds the very numbers: the generalized gamma does not converge from two starts, the log-normal is chosen, the weights 0.221 0.137 0.295 0.190 0.157 over the other five, the note `@auto_not_converged names=gengamma`), `ps_not_converged` (the same table, `distribution = gengamma`: the refusal `not_converged` that P1 had not exercised, with its sentence) and `ps_auto_too_few_rows`. The failure is **real, not a hook** (it needs no self-test switch), but it hangs on the optimiser: the same table with its times unrounded converges on the second start; the table is read from the CSV, so the case is deterministic. The reference side is flexsurv with `ps_polish`; tolerances: the chosen candidate 1e-9, the others 1e-7 (the generalized gamma 1e-4), ΔAIC 0.05 and weights 0.005 absolute (BFGS, above), the choice, the marks, the statuses and the notes exact. Runner tests: the rule on made-up numbers (inclusive margin, not chained, an inadmissible nested one, ties, the option), the candidate rows against a single fit (`model|||log_lik`, `aic`, `coef` identical to the bit, the weights sum to 1, the curves start at (0, 1), the rows pass `tab_check_declared`), the time rule (a clock; `no_candidate`).
- **Times** (local image, `ps_auto`-like synthetic tables of Weibull data with a number and a category, 30 % censoring, generated on the fly, not saved; the Job's Parquet in and out not included; the 16 GB machine shared with other agents): the **whole `auto` fit** with the chosen one's Hessian, the card and the curves: **2.0 s at 1,000 rows, 6.3 s at 10,000, 45.5 s at 100,000**. Candidates alone at 100,000 rows: exponential 5.0 s, Weibull 2.6 s, log-normal 2.7 s, log-logistic 1.5 s, Gompertz 11.0 s, generalized gamma 4.7 s (the first fit includes loading flexsurv). The generalized gamma, started from the Weibull's and log-normal's fits, is not the dearest on these data; it may be on others (a million rows: the time rule). The self-test (the reference file, 70 checks) takes 37 s.
- **Not done / deviations from the plan**: the candidates' Hessian (status `hessian`) is not taken; ΔAIC of non-chosen candidates are BFGS-level; a note per close candidate; `auto_parsimony`; Gompertz's order; `too_few_rows` as a candidate status; the spec's «1e-9 on every candidate» holds for the chosen only.

**As built, P6** (2026-11-20; commit `b7d9b531`, together with B6, which shares its files):

- **Palette and node**: `NOT_IN_THE_PALETTE_YET` is gone; *Parametric survival* is the third item of *Survival*. A new
  composable `frontend/composables/modelIcons.ts` (`modelIconOf`) gives the icons of the palette, the canvas node and the
  panel's title in one place: `TrendingDown` for this node, `CircleDollarSign` and `UsersRound` for `clv` and `bg_bb`, the
  families' (`HeartPulse`, `ChartSpline`, `ChartScatter`) for the others. `modelSummary` says «days, status ~ load + line ·
  Weibull» (the distributions' names are codes, not words of a language: `auto` is «auto», as ARIMA's «(auto)»).
- **The form**: no new control. `distribution` (seven choices) is the app's Select, `quantiles` the `numbers` box,
  `predictors_optional` the `columns` boxes (none required); `reference` and `interactions` are hidden while there is no
  predictor (the `applies` of P0). The `numbers` box now says what is wrong against the kind's bounds
  (`modelNumbersProblem`: the quantiles in (0, 1) «between 0 and 1, both excluded», at most 9, the times from 0), in the node's
  form and in Predict's.
- **Predict**: a parametric version offers `interval`, `at_times` and `quantiles` (a Cox one only `at_times`; verified in
  the form). `predictColumns` now lists the options in alphabetical order, as the gateway's `PredictColumns` does (the
  «Adds:» line was in the catalogue's order before). Not done: with `interval`, the runner also writes `<column>_low` and
  `<column>_high` for each quantile and time; the catalogue declares only `prediction_low` and `prediction_high`, so the
  «Adds:» line and the clash check do not name them (the run still refuses a clash at the gateway).
- **Words**: `models.parametric_survival.{name,hint,params.predictors,hints.predictors,hints.strata}`, the shared
  `models.params.{distribution,quantiles}` and hints, `models.choice.{weibull…gompertz}`,
  `models.choices.distribution.auto`, `predict.params.quantiles` and its hint, in five languages; the Administration card's
  description of *Survival analysis* now names the parametric models. In it, de, es and fr the lists of numbers in the
  hints use a point («0.25, 0.75») because the box separates numbers with commas.
- **Tests**: `WORDED` is the whole catalogue now; 8 new tests in `tests/model-catalog.test.mjs` (the quantiles' bounds,
  the applies, the summary, Predict's fields and columns, the form and the palette rendered). `npm test` 521 pass, `vue-tsc` 0 errors.
- **Browser** (Playwright, Chromium 1254 and Firefox 1573, a scratch page with the real components and the API stubbed,
  no stack: «Not run on a stack» below): palette item reached with Tab and added with Enter; every field set with the
  keyboard (Select with Enter and the arrows, boxes with Space, text boxes by typing); every control of the panel reached with Tab;
  a quantile of 1.5 marks its box invalid; the Predict form with a parametric and then a Cox version. Chromium in the five
  languages, Firefox in English and Italian. Screenshots kept outside the repository.
- **Firefox**: the list of predictor boxes is a scrollable region and takes one Tab stop of its own; Chromium does not.
- **Not run on a stack**: the real editor page (a flow, a source, the preview of the columns, a drop with the mouse) was not
  tried: no stack was up but kind, which this lot does not touch.

**As built, P9** (2026-11-20; commits `74cc9b52` for the catalogue and the editor, `9760f652` for the runner; not pushed):

- **Catalogue**: `alive_at` (kind `number_column`) is a key of **Predict** (`applies_to`: `parametric_survival` alone, so
  `CheckPredict` refuses it for Cox and Kaplan–Meier with «Predict's option 'alive_at' applies to parametric_survival, not to
  cox») **and of the node**, between `times` and `alpha` (default null), so that the `rows` output can give the time left too:
  a deviation from the plan, which named `PREDICT` only. The columns Predict adds are the same ones (`prediction`,
  `quantile_*`, `survival_at_*`, their bounds): they now mean the time left. `params_cases.json` has `alive_at: null` in
  its three parametric cases; the snapshot and its three copies are regenerated.
- **Runner** (`ps_predict(…, alive_at)`, `ps_alive_column`): the median and the quantile p of the time left are
  Q(1 − (1 − p) S(a)) − a (computed as `-expm1(log1p(-p) + log S(a))`), the survival at t is S(a + t)/S(a); the intervals by
  the same delta method, on log q and on log(−log S), **with the gradient through S(a)** (a depends on the parameters), so
  that the interval of the median left is not the unconditional one shifted. The hazard at `hazard_times` stays the
  unconditional one. The `rows` output (`alive_at` of the node) and Predict (`alive_at` of the step) give the same numbers
  **to the bit** (test: lognormal, strata, weights, ages empty, negative and valid; the fit, the residuals and the linear
  predictor do not depend on `alive_at`).
- **Refusals and empty rows** (spec's rule kept: a row's value is the row's, not the table's): a column that is not there is
  `missing_column`; a column that is not a number is `alive_at_type`, in the node (before the fit) and in Predict. A NULL,
  negative or infinite `alive_at` gives NULL on the row and a warning «N rows have an empty, negative or infinite time already
  survived (alive_at)»; an `alive_at` with S(a) = 0 in double precision (nobody lasts that long under the model) likewise,
  «N rows have survived beyond what the model allows», counted apart. **A Gompertz with a cure never has S = 0**: there the
  median left is NULL as soon as ½ S(a) is not above the share that never has the event, while S(a + t)/S(a) goes to 1
  (a difference of log S below rounding is read as no hazard accrued: S = 1, not NaN). The extrapolation warning counts the
  row by a + the furthest of the median, the quantiles and the times.
- **A change in passing**: a quantile the distribution never reaches (the cure's median) was `Inf` in Predict's and the rows'
  numbers; `ps_predict` now gives NULL there, as «Predict» says. The `stratum` section already wrote it empty.
- **Tolerances and what was measured** (self-test, image `tabularia-models-survival:dev`, 6 distributions):

  | Against | Tolerance | Largest measured |
  |---|---|---|
  | exponential, closed forms: median `ln 2/λ`, quantiles, `exp(−λt)`, for a from 0 to 600 (S(a) down to 5e-5) | 1e-10 relative | 6.7e-12 (the rate is the fit's: 1.2e-12 away from d/Σt) |
  | exponential, intervals in closed form (se of log λ = 1/√d) | 1e-6 | 8.5e-12 |
  | flexsurv's `summary(type = "survival")`, S(a + t)/S(a), the six distributions, a from 0 to 1.1 × the last time | 1e-9 | 9.9e-16 |
  | flexsurv's `summary(type = "quantile")` at 1 − (1 − p)S(a), less a: median, 0.1, 0.9 | 1e-9 relative | 9.5e-14 (generalized gamma) |
  | a root of S(a + q) = ½ S(a) (`uniroot`, tol 1e-12, from flexsurv's S alone), where S(a) > 1e-6 | 1e-7 | 1.1e-14 |

  The quantiles of the time left lose digits when S(a) is very small (1 − p' is formed from S(a): relative error about
  1e-16/S(a)); the checks keep S(a) above 0.05 for the 1e-9 and 5e-5 for the closed form, which measured 6.7e-12.
- **Editor**: the Predict form draws `alive_at` as the app's Select over the numeric columns of the input (`columnOptions`:
  none chosen is «—»; a column dropped upstream stays listed with «not in the step's input»; no numeric column says so), after
  the quantiles; the node's form has it as one more optional column (Select, between «Survival at times» and «Significance
  level»). Words in five languages: `models.params.alive_at`, `models.hints.alive_at`, `predict.params.alive_at`,
  `predict.hints.alive_at`. Not tried in a browser (Chrome and Firefox) nor on a stack: the controls are P6's own Select, drawn
  by the same code as the node's column fields.
- **Tests**: runner, 4 new (the exponential's closed form and its intervals; flexsurv and a root, six distributions; the empty
  and impossible rows, the cure, the extrapolation count, the column's refusals; rows output equal to Predict to the bit);
  `go test ./internal/modelcatalog/` (Cox and Kaplan–Meier refused, a number instead of a column refused, the columns
  added); `test_models_catalog.py` (the key, its default, `applies_to`); `npm test` 521 pass (Predict's fields, params and
  columns, the node's page with its words and its place), `vue-tsc` 0 errors. In the same self-test run, the other lots' own
  cases (P2's checks, P3's auto) were failing as they were being written: not P9's.
- **Seen, not P9's**: `go test ./internal/modelgroups/` fails `TestEveryGroupOfTheCatalogueHasARecipeAndARunner` («survival:
  1,093,697 bytes of code») with the uncommitted `expected.json` of P2/P3 (+2,000 lines); P9's own code adds about 14 KB.

**As built, P8** (2026-11-21; commits `cbf4186f` (catalogue, editor), `91b47a60` (report), `f2cc1617` (runner, checks, reference values); the layout's test travelled in B3's `0adefc0e`, which staged the file with it; not pushed; files `groups/survival/runner/R/parametric_survival{,_checks,_auto}.R`, `reference/make_reference_delayed.R`, `reference/data/ps_delayed{,_exp}.csv`):

- **Catalogue and editor**: `start` (kind `number_column`, Cox's own) between `weights` and `distribution`, default null, in `disjoint` with the time, event, predictors, strata and weights; the model section declares the number `n_delayed` (rows that enter after 0) in the four variants, the note is a `note_*` already declared. The words of `start` are Cox's (the key is shared): its hint, in five languages, now says what delayed entry is; `alive_at`'s hint says it is not the start column. `card.schema.json` names `start_column` (string or null: Predict does not read it). Tests: `params_cases.json` (the three defaults, one case with a start, two refusals of the same column twice), the engine's catalogue test, the frontend's form test (order, optional numeric column, hint). **Predict has no `start`**: the card holds the parameters of the truncated fit; the table in «Delayed entry» says how it differs from `alive_at`.
- **The fit**: each row's log-likelihood loses log S(start) (the likelihood conditional on lasting until the start), computed only for the rows with a start above 0 (the others are exactly as before); flexsurv's own `Surv(start, time, status)` is the same likelihood (checked to 1e-10 relative at every fit, as for any fit). Refusals, in the existing format: **`time_type`** (Cox's code and sentence) for a start that is not a number, **`negative_start`** («the column «s» holds a negative start (-1.0): a time is counted from the start, 0 or more»; Cox allows it, a distribution's origin is 0), **`start_not_before_time`** with Cox's own sentence (row, start, time). `survreg` takes no start–stop rows («start-stop type Surv objects are not supported»): its second start is the fit of the rows read from 0, and Gompertz's is d / Σ (time − start). The Harrell concordance, the Kaplan–Meier median per stratum and Cox's hazard ratios beside use the counting-process form. Written: `n_delayed`, the note `@start_rows delayed=<n> rows=<n>`, the formula `Surv(s, t, e = v) ~ …`, the card's `start_column`. **The rows output**: `pred_cox_snell` is H(time) (the end of the pair); the martingale and deviance residuals use the hazard accrued while observed, H(time) − H(start). With `weights`, a weight multiplies the whole row (the truncation term too).
- **The checks and charts** (every one still makes sense with a start; **none is `not_computed` because of it**, which departs from the plan's «where it makes no sense»): the likelihood-ratio checks (`distribution_vs_gengamma`, `constant_hazard`) and the fits of the model section refit with the start; `dfbeta` is V · U_i of the row log-likelihood with the entry term, so it needs nothing else; `ph_cox` is `cox.zph` of `coxph(Surv(start, time, status))` (counting process; the Cox hazard ratios beside likewise); `km_agreement` and the charts `fit_km`, `loglog` use `survfit(Surv(start, time, status))`, Cox–Snell uses the pairs (H(start), H(time)) with Nelson–Aalen's estimate with entry. **One rule is added, which the plan did not have**: with entry the first times of those estimates rest on the few rows that have entered, and one early event with one row at risk takes Kaplan–Meier to 0 for good, so they are read from `t0`, the last time before the first with at least `km_min_at_risk` (10) rows at risk (weights count), as the survival conditional on lasting until t0: the rows with a time above t0 enter at the later of their start and t0; the fitted curve is Σ w S(t) / Σ w S(t0) (the first version had the numerator weighed by S(t0) as well: found by the reference values, `ps_delayed_checks`, line B); the curve starts at (t0, 1); Cox–Snell's estimate is set back on the diagonal (c0 + the hazard accrued since c0). Without a start t0 is 0 and nothing changes (the 43 earlier reference cases are byte for byte the same). The note `start_rows`, in five languages, says it. A group with no time with 10 rows at risk says so, as ever (a measure, no verdict). Verdicts stay from p-values against alpha; no threshold was added. `auto` fits every candidate with the start; its curves chart follows the same rule.
- **Time** (what the reference values showed; the one-heavy-job rule): with a start flexsurv has **no analytic derivatives**, so each BFGS step costs a numerical gradient: a Weibull fit on 88,000 rows takes 10.7 s at `reltol` 1e-15 and 5.8 s at 1e-10 (without a start 1.4 s). So (1) a fit with a start stops BFGS at 1e-10 (`PS_RELTOL_START`), (2) its Newton polishing is `ps_newton_stale`: a Richardson Hessian (2 s) once per round, then up to three steps reusing it with the gradient of the new point (0.7 s), at most four rounds, V at the end taken at the point kept; without a start `ps_newton` is the old code, untouched. A warm start from the fit read from 0 was tried and dropped (no gain at 1e-10). **Measured** (image `tabularia-models-survival:dev`, 1 core, whole Job in R without Parquet, synthetic Weibull AFT tables with a number and a category of three levels, half the rows entering at 0 and the others before 6, made on the fly and not saved; the 16 GB machine shared with another agent): 10,000 rows (8,747 kept after the truncation) **5.4 s with a start against 2.6 s without**, `auto` **9.2 s against 6.4 s**; 100,000 rows (87,980 kept) **23.6 s against 12.2 s** (38.6 s before the two measures above). The first version of the stale steps made the Gompertz test of the second start end 0.0076 in log-likelihood short: it is for the fits with a start only.
- **Reference values** (`make_reference_delayed.R`, sourced last by `make_reference.R`; the tables `ps_delayed_exp` (60 rows) and `ps_delayed` (150 rows: a number, a category of two levels, weights 1 to 3, a NULL, a column `zero`; the rows of line A enter at 0 for 60 % and before 6 otherwise, those of line B between 2 and 10 but for 6 % that enter before 1.5 and leave at once, so that B's first times have fewer than 10 rows at risk); **14 cases, 492 values**, `expected.json` regenerated once from the whole tree: 57 cases, 2,518 values, 178,734 bytes, the 43 earlier cases identical): `ps_delayed_exponential_exact` (λ = events / Σ (time − start), ℓ = d log λ − λ Σ (t − s), median and quartile: **1e-10**; the standard error 1/√d **1e-8**, 6.9e-10 measured: the runner's is a numerical Hessian); `ps_delayed_<distribution>` ×6 against `flexsurvreg(Surv(start, stop, event))` brought to its maximum (the tolerances of P1: ℓ, AIC, BIC 1e-9, estimates 1e-9 and a millionth of a standard error, standard errors 1e-6, the profile 1e-8, the likelihood-ratio statistic 1e-8, concordance 1e-8, Cox's hazard ratios beside 1e-9), with **a second construction** for the Weibull and Gompertz (the truncated log-likelihood Σ w (δ log h − H(t) + H(s)) written by hand, maximized by `nlm` and Newton: ℓ and estimates **1e-6**); `ps_delayed_zero_weibull` and `_lognormal` (a start of 0 for every row: no truncation, so **survreg** is the witness, at P1's 1e-9 to 1e-6); `ps_delayed_checks` (dfbetas from the hand-written likelihood **1e-5**, the rows named exact; `ph_cox` against `cox.zph(coxph(Surv(start, stop, event)))` **1e-9** (p 1e-8); the generalized gamma's statistic 1e-8; `km_agreement` of line A (t0 = 0) and line B (t0 = 1.6) by a second construction **1e-6**; extrapolation 1e-8); `ps_delayed_auto` (P3's rule written again: the choice, marks, ℓ and AIC 1e-9 chosen, 1e-7 the others, 1e-4 the generalized gamma); three refusals. **The lifelines witness** (`make_witness.py`, `entry_col`): 45 more values compared, 124 for the parametric node, none differs beyond its tolerance (1e-5, as for any lifelines fit).
- **Self-test**: **93 checks, all ok** (77 before: 14 reference cases, and 2 runner tests: the closed forms, refusals, rows output, Predict without a start and a start of 0 equal to the fit without; Kaplan–Meier with entry, from t0 and Cox–Snell's pairs exact on seven rows, the checks' verdicts with entry, dfbeta naming an outlier first), 2 min 38 s on this machine. **The ConfigMap** of the survival self-test: **637,333 bytes** (571 KB before), job 406,455; the limit is 921,600 (900 KiB); `go test ./internal/modelgroups/ ./internal/modelcatalog/ ./cmd/modelgroup/` green; engine `test_models_catalog.py` 172, `test_models_layouts.py` and `test_models_results.py` 304, the frontend's `npm test` 523.
- **Not done / departures**: no check is `not_computed` for the start (none makes no sense; the rule above is how the early times are read); robust errors for rows of one subject stay out («Not in this node»); the exponential's standard error is 1e-8, not 1e-10; one case `ps_start` of the plan is fourteen; the editor was not tried in a browser (Chrome and Firefox); a start on **Predict** was not added, by design.


```text
P0 contract, recipe, image (first, alone)
 ├── P1 fit, card, rows, Predict in the runner ──┬── P2 checks, charts' points ──┐
 │                                                ├── P3 automatic choice ───────┤   (P2 and P3 in parallel: files of their own)
 │                                                │                              └── P8 delayed entry
 │                                                └── P9 conditional survival (also after P6)
 ├── P5 report (P0's fixtures; real outputs after P2, P3)
 ├── P6 the node in the editor, the Predict form
 └── P7 MLflow: candidates and parameters
                                           ▼
                      P10 docs and assistants ── P11 on kind ── P12 final review
```

**P1's interface**, fixed here so that P2 and P3 start with it (`runner/R/parametric_survival.R`):
`ps_prepare(task)` → the positional frame, the design, the strata's dummies, the weights, the rows
left out and their counts, the profile's design rows; `ps_fit(prepared, distribution, starts =
NULL)` → the flexsurv object, the optimiser's vector, `loglik`, `status`, and `loglik_rows(par)`
(each row's log-likelihood, for the Hessian, dfbetas and the checks); `ps_covariance(fitted)` → V
(the Richardson Hessian's inverse) or NULL; `ps_predict(model, X, strata, quantiles, times,
interval, alive_at = NULL)` → quantiles, survival and hazard with their delta-method intervals,
where `model` is a fitted model or a card; `ps_write(task, prepared, fitted, rows)` writes the
sections of «The output».

| Lot | Objective | Files (owned) | Depends on | Accepted when (small data) | Check | Agent |
|---|---|---|---|---|---|---|
| **P0** Contract, recipe, image | The catalogue entries (the model, its kinds, its four variants with their sections, charts and outputs; `SECTION_SHAPES.parameter`; `LIMITS.max_quantiles`; `PREDICT`'s `quantiles` and the extended `at_times` and `interval`, `columns.parametric_survival`); `card.schema.json`'s `parametric_survival`; a results fixture per variant; the recipe of 51 packages; the route in `run.R` and a stub `parametric_survival.R` that refuses «not built yet»; `mainMetrics` | `backend/app/engine/models/catalog.py`, `catalog.json` and its three copies, `backend/tests/test_models_catalog.py`, `test_models_results.py`, `backend/tests/fixtures/models/parametric_survival-{ph_aft,aft,ph,auto}.json`, `params_cases.json`; `gateway-go/internal/modelgroups/{card.schema.json,groups/survival/recipe.json,groups/survival/runner/run.R,groups/survival/runner/R/parametric_survival.R}`; `gateway-go/internal/modelcatalog/` (tests); `gateway-go/internal/api/models_mlflow.go` (`mainMetrics`) | — | `model_catalog.py --check` green; `results.py` accepts the four fixtures and refuses a key a variant does not declare; the Go and engine checks pass `params_cases.json`'s new cases; **one image build** of `survival` with `cmd/modelgroup`: `library(flexsurv)` loads as user 10001 with a read-only root; the self-test of Kaplan–Meier and Cox passes with **their reference values unchanged**; the stub exits 3 with its sentence; the build's duration, the image's size and `stringi`'s licence written in this spec | the catalogue commands of «Commands», `go test ./internal/...`, one local image build | sonnet |
| **P1** Fit, card, rows, Predict | `ps_prepare`, `ps_fit` (the six distributions, the starts, the second attempt, `reltol` 1e-15), the runner's log-likelihood and its check against flexsurv's, the Richardson covariance, `coef` (time ratios, hazard ratios, Cox's beside), `parameter`, the `model` section, the profile in `stratum`, the delta method, the card, `predict_parametric_survival` (`prediction`, `quantile_*`, `survival_at_*`, `interval`), the `rows` output; `tab_native_lean` for `flexsurvreg`; the reference cases `ps_exponential_exact`, `ps_exponential_groups`, `ps_weibull`, `ps_weibull_strata`, `ps_lognormal`, `ps_loglogistic`, `ps_gengamma`, `ps_gompertz`, `ps_gompertz_cure`, `ps_all_events` and the refusals; the witness's four fitters | `groups/survival/runner/R/parametric_survival.R`; `groups/survival/reference/data/ps_*.csv`, `expected.json` (its cases), `make_reference.R`, `make_witness.py` (their parts); `common/R/card.R` (`tab_native_lean`: one class more, which changes every R group's runner hash, so their versions in use run their self-test again, as after X0) | P0 | Every reference row of its cases in the image (closed forms 1e-10, flexsurv 1e-9, survreg 1e-7 and 1e-6); the card's predictions of the fitted rows equal flexsurv's within 1e-9; the delta method against `survreg`'s `uquantile` 1e-6; an unseen level and a missing stratum give NULL; the Gompertz cure gives a NULL median and its note; column names that look like code come back as labels; `native.rds` holds no row; the witness agrees at 1e-5, or what differs is written here | local self-test, witness | **opus** |
| **P2** Checks and charts' points | The checks of «The checks» (verdicts from p-values against alpha and facts; measures without a verdict, a `description`); the points and curves of `fit_km`, `cox_snell`, `loglog`, `profile_survival`, `hazard`, `deviance_linear`; dfbetas from `loglik_rows`; `ps_separation` | `groups/survival/runner/R/parametric_survival_checks.R`; its reference cases | P1 | Each check on both sides of each threshold on synthetic tables; dfbetas against `residuals(survreg, type = "dfbetas")` 1e-6; `ph_cox` equal to `cox.zph` on the same Cox fit; a check that does not apply is not written; the charts' points within `max_chart_points` with the extremes kept | local self-test | sonnet |
| **P3** Automatic choice | The candidates in their order, admissibility, the weights, ties, the time rule, the notes `auto_close`, `auto_not_converged`, `auto_time`, the `candidate` rows and the `candidates` curves; `ps_auto`, `ps_auto_parsimony` (the Weibull table of 240 rows: the generalized gamma has the lowest AIC, ΔAIC 0.30, the Weibull is chosen and marked), `ps_auto_failing` (and the self-test hook that forces a failure, if no small table makes one) | `groups/survival/runner/R/parametric_survival_auto.R`; its reference cases | P1 | Each candidate's ℓ, AIC, BIC equal its own `flexsurvreg` fit (1e-9); the choice and the weights; a failing candidate is out of the weights and named; a budget of 1 s leaves the late candidates with status `time` | local self-test | sonnet |
| **P4** Calibration of the verdicts | **Dropped 2026-11-20**: verdicts come only from tests with a p-value, against the node's alpha; measures without a p-value are shown without a verdict. Nothing to calibrate | — | — | — | — | — |
| **P5** Report | `layouts/parametric_survival.py`, its words in five languages, Excel; first day: an `xy` with `split_by`, a step and a line series per group (the fallback of «The report» if not) | `backend/app/engine/models/layouts/parametric_survival.py`, `report_texts/parametric_survival.json`, `backend/tests/test_models_layouts.py`; the fixtures again from the runner's output after P2 and P3 | P0 (fixtures); P2, P3 (real outputs) | Every variant's fixture renders in five languages and passes `reports.definition.check`; PDF and Excel; the first page holds the checks; the sentences on Gompertz and on the strata appear where they apply; the golden PDFs unchanged | `ci_tests.sh engine` | sonnet |
| **P6** The node in the editor, the Predict form | The palette entry under *Survival*, its icon, the form (the kinds `distribution`, `quantiles`, `predictors_optional` drawn by existing controls), `modelSummary` («days, status ~ load + line · Weibull»), the Predict form's `quantiles`, `at_times` and `interval` for a parametric version, the words of `models.parametric_survival` and `predict.quantiles` in five languages | `frontend/components/ui/OpSidebar.vue`, `useOpIcons.ts`, `frontend/composables/modelCatalog.ts`, `frontend/components/ui/PredictForm.vue`, `ModelForm.vue` (only if a kind needs it), `frontend/i18n/locales/*.gen.ts` (namespaces `models`, `predict`), `frontend/tests/model-*.test.mjs` | P0 | Chrome and Firefox: find, drop and configure with the mouse and with the keyboard alone; `interactions` hidden without predictors; the Predict form shows `quantiles` for a parametric version and not for a Cox one | `vue-tsc`, `npm test`, browser | haiku |
| **P7** MLflow | The sections `candidate` and `parameter` read back and logged as metrics, `distribution_chosen` as a parameter, ARIMA's candidates with them | `gateway-go/internal/api/jobs_models_mlflow.go`, `internal/mlflowconn/mlflowtest/`, their tests | P0 | `httptest`: the new metrics and the parameter, keys within 250 characters and their suffixes, ARIMA's candidates logged | `go test ./internal/...` | sonnet |
| **P8** Delayed entry | `start`: the catalogue key (snapshot, copies), the fit, the checks and charts with entry, the note `start_rows`, the layout's subject («start–days»), a reference case `ps_start` against flexsurv and against a truncated likelihood written by hand | `catalog.py` and copies; `parametric_survival.R`, `parametric_survival_checks.R` (their `start` parts); `layouts/parametric_survival.py` (`about`); its reference case | P2 (and P5 for the layout) | The case passes (1e-9 flexsurv, 1e-6 the hand-written one); `start ≥ time` refused with Cox's sentence | local self-test, `ci_tests.sh engine` | sonnet |
| **P9** Conditional survival | `alive_at` in `PREDICT` (snapshot, copies), its check in `modelcatalog.CheckPredict` (a numeric column of the rows), the runner's remaining-time answers and their intervals, the Predict form's column choice, its words | `catalog.py` and copies; `gateway-go/internal/modelcatalog/predict.go`; `parametric_survival.R` (its predict part); `PredictForm.vue`, `*.gen.ts` | P1, P6 | S(a + t)/S(a) and the remaining median against a hand computation (1e-12); `alive_at` NULL or negative gives NULL; `alive_at` refused for a Cox or Kaplan–Meier card with its sentence | local self-test, `go test`, `npm test` | sonnet |
| **P10** Documentation and assistants | «Assistants and documentation» of the section | `docs/capabilities/models.md`, the MLflow page, `README.md`, `CHANGELOG.md`; `gateway/app/services/ai_flow_agent.py` (prompt only), `internal/aiprompts/`, `internal/mcp/` (regenerated) | P1–P9 | An assistant places a configured `parametric_survival` node with `distribution = auto` on a draft; `config_reference.py --check`; the site builds, its links hold | `ci_tests.sh gateway`, `go test`, site commands | haiku (docs from the spec), sonnet (prompts) |
| **P11** On kind | Rebuild *Survival* (Building → Checking → Ready, the lock with 51 packages); Kaplan–Meier and Cox flows as before; each distribution and `auto`: Run, Table, Report, PDF, Excel; a flow run logged to the test MLflow server, the version registered; Predict from it with `quantiles`, `at_times`, `interval`, `alive_at`; the Job without internet and without MLflow variables; durations on the reference tables and on 100,000 generated rows, the image's size from the registry, written here | — | all | All green, Chrome and Firefox | the suites, browser | sonnet |
| **P12** Final review | The section against the code; the verdicts' false alarms; the card's sufficiency for Predict | — | P11 | Corrections committed, the «As built» block written | the suites | **opus** |

## Decided during the final review

Decided by the final review, **owner to confirm**:

1. **The out-of-sample AUC of a penalized logistic fit** is computed fold by fold and averaged
   (`auc`, with `auc_se` and `auc_folds`); the AUC of the pooled out-of-fold predictions and
   DeLong's interval are not given for a penalized fit (`a1b9263d`). The other option, an
   explanation in the report, was not taken.
2. **A changed runner re-checks the version in use** without rebuilding it: the self-test runs
   again on the same image, the version stays usable meanwhile (`8c7b5f85`).
3. **The verdicts use the classical thresholds**; the wide ones only name rows to look at
   (`3c3dff03`); `rmst_tau` defaults to the shortest of the strata's longest times.
4. **A card too large does not stop the fit**; only the registration is lost (`5c6dd73d`).

## Provisional decisions

Taken by this spec, **provisional, owner to confirm**; the work goes ahead on them.

1. **MLflow connections are ordinary folder connections**: created by whoever may create
   connections in the folder, used with `CONNECT`; each team can point at its own server.
2. **Trials are not logged to MLflow**: only flow runs. A later per-node option can log
   them with `tabularia.dev_sample = true`.
3. **A group may be used by everyone who can edit flows**, with the option of listing user
   groups; the code is Tabularia's own, so the risk is cost, capped per group.
4. **No `python_function` flavor**: a registered version is Tabularia's card plus the
   fitted object (`native.rds`, without the rows of the input) as a plain artifact for people; no `mlflow` package in the
   images, no serialized object loaded by Tabularia.
5. **Predict lists the versions Tabularia made** (the `tabularia.model` tag) whose run is in
   the chosen experiment, plus «latest version» of a registered model, resolved at each run.
6. **MLflow unreachable fails the model step**, unless the node's «Continue without MLflow»
   is on.
7. **A server without the artifact proxy** gets parameters, metrics and tags, no artifacts
   and no registration, and the run page says so.
8. **ARIMA's automatic orders** use `auto.arima`'s defaults: AICc, stepwise, KPSS for `d`,
   the seasonal-strength test for `D`. (As built the node's `drift` default is false, so the search
   does not try a drift as `auto.arima` alone would: open question 2.)

## Open questions

None blocks anything now: every piece is built. One for later: **the version** that carries the models (the
next minor on `main`) and when.

## Open questions for the owner

Facts the builders found that the spec cannot settle: each is **the owner's to decide**; the
text above says what the code does today, and the proposal is the builder's, not adopted.

1. **Monotone likelihood in Cox** (lane M5). Today `coxph`'s warning that a coefficient may
   be infinite becomes a note («monotone likelihood …: that coefficient and its interval
   mean nothing») and the node goes on. *Options:* a note (today), or stop the
   node as complete separation stops a logistic fit with maximum likelihood. The lane left
   it to the owner.
2. **Drift in the automatic orders** (lane M7). `drift` and `mean` become `allowdrift` and
   `allowmean`; with the default `drift = false` the search never tries a drift, while
   `auto.arima` called alone does try it, so the automatic choice can differ from a bare
   `auto.arima`. The lane made no proposal beyond «decide»: either `orders = auto` follows the
   node's `drift` (today), or it leaves the drift to `auto.arima`'s own default.
3. **Hosmer–Lemeshow in the penalized variant** (lane M3). The check is a row that is
   always «not computed». *Proposal:* remove it from the catalogue for that variant, or say
   in the report why it is not computed (the lane's code says it today).
4. **`requires: order_by` with HAC** (lane M1). With `se = hac` and no `order_by`, the
   rows are read in the order they arrived, with a note. *Proposal:* require `order_by`
   whenever `se = hac`.
5. **`weights` with `estimation = ols`** (lane M1). The weights are ignored, with a note
   saying to choose `wls`. *Proposal:* apply them only with `wls` or with a penalty, and
   refuse or hide the key with `ols` in the catalogue.
6. **Firth with profile intervals** (lane M2). `brglm2` has none: the intervals are Wald's,
   with a note. *Proposal:* a `forbids` rule in the catalogue (`estimation = firth` with
   `ci = profile`), or say it in the specification and keep the note.
7. **Frequency weights: rows or cases** (lane M2). `n` and `events` are sums of the weights.
   The lane asks which word the form and the report use: «rows» or «cases».
8. **Robust errors with weights** (lane M2). `sandwich` counts rows, not weights, and with
   Firth it uses the `glm`'s score. *Proposal:* forbid a robust `se` with `weights`, or keep
   the note that says each row is one observation.

9. **The address of Tabularia in MLflow** (lot G). `tabularia.url` and `mlflow.source.name` start
   from `SHARE__PUBLIC_BASE_URL`; when it is not set they are relative and a person in MLflow
   cannot follow them (seen on kind). *Proposal:* a setting of its own for Tabularia's address,
   or the run page opening on `?run=<id>`, so that the link can be built from the interface's
   own address.
10. **Penalized linear fit with `interval`** (lot H). The catalogue says that Predict adds
    `prediction_low` and `prediction_high`; the runner does not and writes a warning.
    *Proposal:* take the parameter out of the catalogue for a penalized fit (a penalized fit has
    no honest interval), or write the interval from the cross-validation's error.
11. **The column check in the form for an ARIMA Predict** (lot H). It asks that the input hold the
    columns of the card, which is too strict for ARIMA, whose output is the forecast.
    *Proposal:* the form asks only for the exogenous columns.
12. **Cards past the megabyte** (final review). A linear or logistic fit of about 190 terms loses
    its whole card; a Cox fit with many strata and times is not thinned. *Proposals:* drop only the
    `vcov` and keep the rest; thin Cox's baseline as Kaplan–Meier's.
13. **Saving a flow with a group that cannot be used** (lot C1). It is accepted; the refusal comes
    when the step runs. *Proposal:* refuse at the save, or only warn there.
14. **The permission of the trial** (lot C1). The code asks for RUN on the folder, as the Script
    node's; this spec said EDIT. *Proposal:* keep RUN, which is the Script node's rule, and
    correct «Security».
15. **Smaller faults of the cluster test** (lot I1): the trials appear in the list of runs as
    «Kind model test»; the sentence about a quota carries Kubernetes' own text; the 403 sentence
    writes the e-mail with a capital; the interface does not show `rechecking`; the run
    `kind` `model` is not in the frozen Python gateway's enum. *Proposal:* a label and a filter for
    the trials, a rewritten quota sentence, the e-mail as written, a small badge on the group
    while it is rechecked.

**The boosting group (2026-11-20)**, asked before its lots start; the section is written on the
recommendation of each, so that the lots can start if the owner says nothing:

16. **Sign the cards.** The boosting group loads a model file that the library parses; its hash
    is tied to the card, and the card's to an MLflow tag that anyone who can register versions in
    that MLflow can write. *Recommendation:* yes, for every group: the gateway adds an HMAC of the
    card (a key derived from its Fernet key) as the tag `tabularia.card_hmac` when it registers a
    version, and Predict refuses a version without a valid one (versions registered before keep
    working only with an explicit «trust unsigned versions» on the connection). A small lot of
    its own after X5; the boosting lots do not wait for it.
17. **A test part besides validation.** Early stopping chooses the number of trees on the
    validation part, so its numbers are slightly optimistic (the note says so). *Recommendation:*
    not now; later an optional `test_share` that keeps a third part out of everything.
18. **Unseen levels and missing values in Predict.** The boosting models predict them through
    their missing-value path, counted on the run page; the regressions give NULL. *Recommendation:*
    keep it so: trees have a defined answer, the count says how often it was used.
19. **Hashes in the Python recipe** (`pip install --require-hashes`). *Recommendation:* not now,
    as the R recipes pin versions without hashes; later for every Python recipe at once, generated
    with the closure.
20. **The defaults and the thresholds.** The hyperparameters' defaults (learning rate 0.05, depth 6,
    1,000 trees, 50 rounds of patience, `subsample` and `colsample_bytree` 0.8) and the
    diagnostics' thresholds are this spec's rules of thumb. *Recommendation:* keep them and let X8
    and X9 revise them on what the sample data show. **Update:** the thresholds were revised
    after simulations of clean data (`5b78ab2d`): overfitting ratio 0.05 / 0.25; calibration
    max(0.05, noise) / max(0.10, noise + 0.05); slope interval against 0.8–1.25 and 0.5–2;
    leakage share 0.8 with a metric of 0.98 (one predictor: 0.98 alone); few trees only above
    learning rate 0.05; `beats_baseline` 0.05. Those numbers are new rules of thumb for the owner
    to confirm, and two weaknesses remain (`beats_baseline` «doubtful» 10–15 % of the time on weak
    classifications at n = 400; a heavily overfitted regression caught in 40–80 % of cases).
    X9 then redid `overfitting` (not computed with early stopping on the loss), `trees_limit`
    (`stop_on_loss`), `importance_stability` and `beats_baseline` (balanced weights): see «As built»,
    «The final review (X9)».
21. **Two options for the engine-only `xy` chart** (`Series.color`, `options.y_categories`):
    «a change to an existing chart kind» is under «Ask first», though these only add to a kind
    saved reports never see. *Recommendation:* accept them; a new kind would duplicate `xy`.
22. **MLflow's own flavors** (`xgboost`, `catboost`) in `MLmodel`, so that a person can load the
    version with MLflow's functions. *Recommendation:* not now (provisional decision 4 holds); the
    two files load directly with the two libraries, and the documents say how.
23. **Choices of the builders that the owner may want otherwise** (boosting, 2026-11-20).
    (a) CatBoost's `l2_leaf_reg` has its minimum above 0 (not 0), because 0 crashes a multiclass
    fit; (b) CatBoost's multiclass starts from the log of the class shares, not from the library's
    own start; (c) the advice of a diagnostic is chosen by an id the runner writes, so a new
    diagnostic needs an id in the runner and a sentence in the report's words. *Recommendation:*
    keep all three.

**The node «Parametric survival» (2026-11-20)**, asked before its lots start; the section is written
on the recommendation of each, so that the lots can start if the owner says nothing:

24. **Left and interval censoring.** The node takes right censoring, and delayed entry in lot P8.
    flexsurv also fits times known only to lie between two inspections (`Surv(type =
    "interval2")`), which the comparison with Kaplan–Meier would then read with Turnbull's
    estimator, and the Cox–Snell chart would need another construction. *Recommendation:* not now;
    later, on this node only, as two optional columns `time_low` and `time_high`, when a table of
    inspections asks for it.
25. **Predicting beyond the data.** Kaplan–Meier and Cox give NULL past the last time observed when
    fitting; a parametric model is often wanted for exactly that extrapolation. The section
    predicts it, and counts the rows so predicted in a warning of the run page. *Recommendation:*
    keep it so; if the rows must be marked in the data, a column `prediction_extrapolated`
    (true or false) later.
26. **The thresholds of `km_agreement`.** Closed 2026-11-20: there are none. Verdicts come only from tests with a
    p-value, against the node's alpha; the agreement is a measure without a verdict («The checks»).
27. **Gompertz in the automatic choice.** When `auto` chooses Gompertz the coefficients are hazard
    ratios, not the time ratios of the five other distributions. *Recommendation:* keep it among
    the candidates (it is the distribution of ageing, hazards rising exponentially) and let the
    first page say «hazard ratios: Gompertz is not an accelerated failure time model»; the other
    way is an `auto` over the five AFT distributions only.
    **Decided 2026-11-20: not now; the section stays as written.**
    **Decided 2026-11-20: kept so, with the warning; a column `prediction_extrapolated` only if asked later.**
    **Decided 2026-11-20: Gompertz stays among the candidates of `auto`, and the first page says «hazard ratios: Gompertz is not an accelerated failure time model» when it is chosen.**

Smaller proposals the lanes made, **not on that list and not adopted** (each changes only
how a thing is said or tested):

- **Per-case advice for ARIMA** (lane M6): `advice_<verdict>` in `common.with_labels`, because
  the four cases of ADF and KPSS want different advice; M7 meanwhile swaps the advice in
  `layouts/arima.py`.
- **The witness tolerance for ARIMA** (lane M6): 1e-4 does not hold for a SARIMA with D = 1
  (gap 6e-4, about 0.002 standard errors). *Proposal:* 1e-3, or 0.01 standard errors.
- **A common mechanism to translate notes by id** (lanes M2, M3), instead of the English
  sentence as the key.
- **A hook for a group's own self-tests in `tab_reference_case`** (lane M1), instead of the
  check inside each `fit_*` when `group_version` is `selftest`.
- **Words that depend on the variant in `common.with_labels`** (lane M7), instead of the
  computed field in `layouts/arima.py`.

**As built, P5** (2026-11-20; report of the parametric survival node; files `layouts/parametric_survival.py`, `report_texts/parametric_survival.json`, the four fixtures):

- **Pages** (A4, 5 pages for `auto` and for Weibull with strata, 4 for the others): 1 summary (four cards: rows/events, distribution with `auto` and its Akaike weight, the profile's median with its interval or, with strata, the concordance, AIC with BIC), the formula, the checks, their advice, and under them the table of **measures without a verdict**; 2 coefficients (the ratios' interval charts first, then the estimates, time ratios and VIF, hazard ratios, Cox's beside, the distribution's parameters per stratum) and the numbers of the fit; 3 the choice of the distribution (`auto`: sentence, candidates sorted by AIC with status, ΔAIC, weight, the chosen one and the parsimony mark; Kaplan–Meier against every candidate) and the fit to the data; 4 Kaplan–Meier against the fitted curve, Cox–Snell, log(−log S); 5 the profile (strata, median and quantiles with intervals and Kaplan–Meier's median, survival at the times asked, survival and hazard charts with bands and the node's times as vertical lines), influence, notes.
- **Step plus line, verified**: an `xy` with `split_by` draws, for each group, the step (with its band) and the line in the group's colour; with one group they have the same colour, so the page draws **two charts in the same place**, one visible without groups (`fit_km`: named series, two colours) and one with them (`fit_km_groups`: `split_by`, one colour a group), chosen by whether a `fit_km` row has a `level_2` other than `all`. No change in the chart engine for it.
- **Heatmap**: see «As built, B5» in the customer lifetime spec (`options.min`/`max` honoured by `charts.py`).
- **Common blocks changed** (`layouts/common.py`, all models): `page(..., summary_lines, coef_first, after_coefficients, after_assumptions)`; `grid(p, w, charts, heading, tall)` (the charts' rows, now with or without a heading and with a caption that may be an expression); `measures(ids)`; `alpha_said`; the `quantile_*` wildcards of `stat_label`; `fit_skip` accepts a prefix (a name ending in «_»); the three notes' lines are one text with its table's room, and the Fit heading keeps a room too, so that no page ends with a heading or starts with a table alone.
- **Decision of 2026-11-20 («niente cose hardcoddate»)**: verdicts only from tests with a p-value read against the node's `alpha`, which the page writes under the checks; `km_agreement`, `dfbeta`, `epv`, `extrapolation` are **numbers with a sentence and no colour** (the report lists the `assumption` rows that have a `value` and no `verdict`); `auto`'s AIC margin is the model number `aic_margin` (the catalogue's, default 2), written in the sentence of the choice. The fixtures follow: those four checks have a `value` and no verdict.
- **Fixtures** (placeholders, completed): `fit_km` with `surv`, `lower`, `upper` (Kaplan–Meier) and `fitted`; `candidates` with the `km` curve and a candidate that did not converge (no AIC, status `not_converged`); curves with `lower` and `upper` for the hazard; the `ph_aft` fixture is a Weibull **with strata `plant`** (no hazard ratio, a shape per stratum, `km_agreement` per stratum, note `strata_no_hr`); every fixture has `times` 15 and 30; one `separation` row.
- **Words**: the layout reads `values.distribution`, `values.selection`, `values.coefficient_scale` and the checks' advice by `advice_id` (`km_agreement_auto`); the runner's notes stay in English except `strata_no_hr`, `hr_interval_scale`, `zero_weights`, `zero_times` (`note_texts`).
- **PENDING** for the node is gone (`test_models_layouts.py`). The PDFs (4 fixtures × 5 languages) were drawn and looked at outside the repository.

**As built, P5, languages and notes** (2026-11-20): every phrase of the page is in the report's language with its numbers in the language's decimal. The runner's English is turned in `with_labels`: the `profile` («(mean)», «(reference)», «(stratum of reference)» by `profile_words`, decimals), the `formula` («· Weibull, accelerated failure time» by `formula_kinds` and `values.distribution`), the labels made of a number («Quantile 0,25»). **Notes the runner should send as an id and its parameters**, `@id name=value name=value` in the text of `note_<id>` (a list joined by «,», a distribution by its slug), which the layout composes (`note_texts.<id>`): `@auto_close distribution=weibull other=gengamma margin=2 delta_aic=0.30 weight=0.47` (the chosen one, the candidate with the lowest AIC, the node's `aic_margin`), `@auto_not_converged names=gompertz,lognormal`, `@auto_time names=…`, `@quantile_infinite share=12.5`, `@second_start`, `@weights_counts`, and the already known `strata_no_hr`, `hr_interval_scale`, `zero_weights`, `zero_times`. A note sent as plain English is shown as it is. The Fit and Notes headings keep room for their tables only in these reports (`room`); the other models' pages are as before.

**As built, P10** (2026-11-21; done together with B8; commits on `main`, not pushed):

- **Documentation**: `docs/capabilities/models.md` has «Parametric survival» (after Cox: every option with the form's labels, the six distributions, time and hazard ratios, Gompertz, `auto` with the AIC margin and the nested rule, delayed entry against `alive_at`, the checks as tests, facts and measures, the report, the refusals, an example on `customers` and `orders`), the new section «How a check speaks: verdicts, facts and measures» (the rule «no constants of ours», where alpha is set, the older models keep their thresholds), the output, MLflow, Predict, limits, «does not do» and «Where Tabularia and R differ» updated, the counts («Ten models in five groups», «An eleventh node»). Also `flows.md`, `docs/capabilities/README.md`, `docs/connections/mlflow.md`, `docs/deploy/kubernetes.md` (compiling `flexsurv`, GSL from Debian), `CHANGELOG.md`. `docs/contributing/adding-a-node.md` gets «Path 3: a statistical model» (catalogue, group, `system_libraries`, the ConfigMap's 900 KiB, the verdicts rule, the `@id` notes, the assistants) and the CI row of `model-catalog.yml`. `README.md` untouched (the owner's local hunk): its «Statistical models» bullet (lines 215-218) should name the ten models.
- **Example**: described, with **no number**: no run on the sample data was made (no stack up, no sample database loaded). The expressions of the computed columns (`CAST(... AS INTEGER)` differences, `COALESCE`, `MIN(day) OVER (PARTITION BY ...)`, `EXTRACT`) were run in Polars on small synthetic tables and the per-customer summary equals a hand computation (30 customers, 0 mismatches); DuckDB's `date_diff('day', a, b)` was run too; ClickHouse's `dateDiff` is written from its manual, not run.
- **Assistants**: two bullets in `_MODELS_INTRO` (`ai_flow_agent.py`: when to choose parametric survival over Cox, `distribution: auto`, `start` against `alive_at`; and the customer lifetime nodes: one row per customer, how to build the summary with Group by, `time_unit`, holdout, CLV of revenue) and one on verdicts against measures; `prompts.json` and `tools.json` regenerated with their generators (only the flow assistant's lines changed: 5 + 4). 20 new tests in `test_ai_flow_agent.py` (120 pass). The data assistant's prompt names no statistics, so nothing was added there.


**As built, P11** (2026-11-21; the test on kind, with B9; images `main-f2cc1617` (backend, gateway-go, gateway, frontend), then `main-838d8300` and `main-94c1f1c0` (gateway-go, and the frontend for the first) for the three corrections below; Helm revisions 17 to 19; the commits `5c7c4867`, `838d8300`, `94c1f1c0`; not pushed; the test data are small tables generated for it, 200 rows, nothing of 100,000):

- **The group**: the recipe had changed (flexsurv): version 2 was built by the cluster (queued by the new recipe hash, nobody pressed anything), checked, self-tested and made current: **93 checks ok, none failed**; the lock of the version has 83 lines (the recipe's 51 packages and what they pull); version 1 stays, marked stale. The other groups: the runner changed (common/R, the catalogue) and the gateway saw it on its own: «the runner of the group regression changed: version 1 is self-tested again, and serves meanwhile», the same for timeseries and boosting, and the three passed again within 40 seconds, on the same images and without a build (regression 63 ok, timeseries 25, boosting 50). The versions' stored runner hash equals the shipped one (`cmd/modelgroup groups`).
- **The Job**: the ConfigMap of the code is 403,176 bytes (under the MiB), the container asks 1 CPU and 2 GiB and is limited to 2 CPU and 4 GiB, no init container, the same variables as the other groups; a run takes about 10 seconds on 200 rows, most of it the pod.
- **Fits**, all `SUCCESS` in the editor's trial: `weibull` with a predictor (AIC 1333.12), with `strata = line` (a shape per stratum, the note `strata_no_hr` says why there is no hazard ratio, AIC 1317.90), `lognormal`, `gompertz`, `auto` without strata (the Weibull, `selection = auto`, `aic_margin = 2`) and with strata (the Weibull again: the table was generated from one), `start = entry` (89 rows enter after 0, the note `start_rows delayed=89 rows=200`), `alive_at = age_now`, output `results` and output `rows`.
- **Refusals**, each a sentence and no crash: a negative time («the column «days» holds a negative time (-3.0): a time is counted from the start, 0 or more»), a `start` that is not before the time (named row, both values), a `start` equal to the time column and a predictor equal to the time column (the catalogue's «is both 'time' and 'start'»), an event value that never occurs («with no event there is nothing to fit; check event_value»), `alive_at` on a Cox node («there is no parameter 'alive_at' (it takes: …)»).
- **MLflow** (the test server, connection 3): a run of the flow (not the trial: a trial logs nothing, by design) with `distribution = auto`, `strata`, `alive_at`, `times`, `quantiles`, output `rows`: the version 1 of `p11-surv`, tags `tabularia.model`, `.group`, `.group_version = 2`, `.card_sha256`, `.variant`; parameters including `distribution = auto` and **`distribution_chosen = weibull`**; 92 metrics (the model's and the checks', the six `candidate.<d>.*` of AIC, BIC, log-likelihood, ΔAIC, Akaike weight, `converged`, `admissible`, `chosen`, `parsimony`, and `parameter.shape.line_A/B.estimate`); artifacts `model/MLmodel`, `model.json`, `native.rds`, `tabularia/{coefficients.csv, report.pdf, results.parquet}`.
- **Predict** from that version, with `quantiles = [0.25, 0.5]`, `at_times = [20, 40]`, `interval`, `alive_at = age_now`: **every column equals the fit's `rows` output, to the last bit** (median with its interval, the 0.25 quantile with its interval, S(20) and S(40) with theirs; 200 rows, `IDENTICAL` by value comparison). Without options the columns are `prediction`, `quantile_0.25`, `survival_at_20`, `survival_at_40` (Predict's names, not the fit's `pred_*`). The tag `tabularia.card_sha256` set to zeros: «the model card of the version 1 of «p11-surv» does not match the fingerprint MLflow keeps for it (tabularia.card_sha256): it was changed after it was registered, and is not applied», the Predict refused before the Job; **the tag was put back** (`ad26bf98…b514`) and read back.
- **Seen, not changed**: the tag `tabularia.variant` of this version says `auto` (the node's distribution), where the catalogue's variants are `ph_aft` and the others: Predict worked, so the form's choice of columns reads it; to look at in the final review (P12). The placeholder of the quantiles' field in the form is the times' («e.g. 12, 24, 36»): one `numbers` control for both.
- **Browser**: see «As built, B9», the same test (the three nodes were set up together).
- **Not done**: durations on 100,000 generated rows and the image's size from the registry (small tables only, the rule of the project's development); the Excel export of the report.

**As built, P12 fixes** (2026-11-21; the corrections of the final review's findings; commits `110fad8c`, `4598faa7`, `d0f0fc04`, `32465ae7`, `b8b5460d`, `f45f6afd`, `46445680` on `main`, not pushed; the survival self-test **96 ok, none failed**: the 93 of P11 unchanged, no reference value moved, and three tests more):

- **The comparison with the generalized gamma** (`110fad8c`; `psc_larger_fit`, `psc_lr_check`, `ps_newton_steps`, `ps_structured_derivatives`, `ps_within`, `ps_extra_allowance`). Measured on 5,000 rows, a Weibull with a number and a category of 30 levels (32 parameters), 1 CPU (`docker --cpus=1`): **before** the whole fit 145 s, the comparison 103 s (the review's own measure, on a busier machine: 426 s and 379 s); **after** 45 s and **4 s**, the statistic unchanged (0.1485081); the node's own fit is 35 s of it (BFGS 6 s, its Richardson Newton 31 s). On 1,000 rows and 3 levels: 0.66 s → 0.20 s. What changed:
  - the larger distribution (the generalized gamma; the exponential's Weibull of `constant_hazard` alike) starts **only from the node's own estimates, nested in it**: the Weibull at Q = 1 (μ = log scale, log σ = −log shape, the shape's strata turned on σ), the log-normal at Q = 0, the exponential at σ = 1, Q = 1 (and for the Weibull: shape 1, the coefficients on the log rate turned). No starting fit of its own (the review found a log-normal fitted, and flexsurv's own start evaluated, for every Weibull node); one attempt, BFGS to `PSC_RELTOL` 1e-8;
  - then **Newton steps from the rows' own derivatives**: a row's log-likelihood depends on the parameters only through its nb linear predictors, so g = Σ B_j'(w ∂ℓ/∂η_j) and H = Σ B_j' diag(w ∂²ℓ/∂η_j∂η_l) B_l, the row derivatives by central differences (19 evaluations of the rows for three parameters), O(n k²) to add up by chunks: 2e-8 from numDeriv's Richardson Hessian of all the parameters, which cost about 100 s for 33 parameters on 5,000 rows. **One step was not enough, so the steps go on to the maximum** (at most 10, until a step is below 1e-8 standard errors): from Q = 1 BFGS hardly moves (13 evaluations, Q still 0.9999999 on the 236 rows of `ps_weibull.csv`) and the first step left the LR statistic at 0.6687 against the maximum's 0.6726 (the reference cases `ps_checks_weibull` and `ps_delayed_checks` failed with one step; with the steps they pass at 1e-8). The steps cost 0.36 s on the 5,000 rows;
  - with **auto** the candidate already fitted is reused (its Newton steps only): no fit is made again for the checks, nor for the LR test against the exponential (a test counts the calls of `ps_fit`: the six candidates and the model without predictors). This also settles finding 4: in auto the advice no longer says «or auto» (`distribution_vs_gengamma_auto`: «fit gengamma to use it»), and a candidate not fitted makes the check `not_computed` with `_candidate`;
  - **no row limit** (`gengamma_max_rows` is gone): every fit a check or a test adds (the generalized gamma, the Weibull of `constant_hazard`, the model without predictors and the exponential of the model section's LR tests, which now take the same Newton steps: finding 12) runs under `ps_within`, R's `setTimeLimit` (checked while R code runs, the likelihood included), with **the time the node's own fit took** (with auto, the whole choice), at least `PS_EXTRA_FIT_MIN_SECONDS` = 10 so that a small table never depends on the machine, never past half the step's budget. Out of time: the check is `not_computed` with `advice_id` `<check>_time`, an LR test of the model section is not written and the note `lr_time` or `exponential_lr_time` says so. It decides only whether a number is computed, never a verdict. Self-test hook `task$.selftest_allowance`;
  - the reasons a comparison is `not_computed` each have an `advice_id` (`_no_time`, `_time`, `_candidate`, `_not_converged`, `_below`) and their words in five languages.
- **Strata of one level** (finding 6, `4598faa7`): `has_strata` is «more than one level»; a Weibull with a constant strata column has its hazard ratios and no `strata_no_hr` note, the log-likelihood and the ratios those of the fit without strata (1e-9).
- **A time of 0 with a start** (finding 10): the rows with a time of 0 are left out (and counted) before the start is compared with the time, as without a start; a start column of 0 everywhere gives the fit without a start.
- **Predict without a covariance** (finding 11): too many parameters to keep it («more than N parameters») or a fit whose Hessian was not positive definite: two sentences.
- **An altered card** (finding 16): `ps_model` checks the distribution, the coefficients against their order, a covariance of k × k finite numbers (rows of the wrong length included), alpha in (0, 1), the `parametric_survival` part itself: the refusal `card_invalid`, «the model card is not a parametric survival model's as the node writes it: …; fit the model again».
- **The report** (`32465ae7`): `km_agreement` says the gap **in survival, from 0 to 1** (finding 2), and the row adds «at t = 22.05, 1.93 standard errors of Kaplan–Meier» (`measure_more`, from `at` and `statistic`) by the new `more` of `common.measures`; **`common.measures` lists only the ids declared** (finding 3): `vif` stays out of the parametric survival's measures, because the coefficients' tables already give it per term beside the ratios it inflates; the customer lifetime page declares every measure its runner writes, so its table is unchanged (a test); the candidates' status `too_few_rows` has words, `hessian` is gone with its legend (finding 9); the candidates' columns are wider and an Akaike weight below 0.001 is «< 0.001» in the page's decimal (finding 13); the node's words lose the H0s and advices of its measures (epv, dfbeta, km_agreement, vif, extrapolation), never shown and speaking of thresholds (finding 14; the common words of epv, dfbeta and vif stay, for the models where they are verdicts); the delayed-entry note says the fitted curve beside Kaplan–Meier's is an approximation of what Kaplan–Meier estimates (finding 17, also in `docs/capabilities/models.md`).
- **Fixtures** (finding 5, `d0f0fc04`): the four `parametric_survival-*.json` are the runner's output (`--from-results`, in `PARAMS` and `FROM_RUNNER`): ph_aft a Weibull with three plants and weights, aft a log-normal, ph a Gompertz on `ps_gompertz.csv`, auto (the Weibull) with weights. The PDFs, 4 fixtures and a delayed-entry run × 5 languages, drawn outside the repository (the charts drawn by a stand-in service: the text and tables are the real ones); looked at: `auto-de` page 3 (the candidates: «< 0,001», the German names fit), `ph_aft-en` page 1 (the measures without VIF, the gap with its time and standard errors), `auto-it` pages 1–3, `delayed-it` and `delayed-fr` (the note's sentence).
- **Predict and the forms** (`b8b5460d`): a quantile of 0.5 adds no column (finding 8): the runner folds it into the median, so the gateway's `PredictColumns`, the editor's `predictColumns` and the engine's rows-output names leave it out; the option stays accepted (refusing it would break flows asking 0.25, 0.5, 0.75). The quantiles' box has its own example, «e.g. 0.1, 0.9» (finding 7), with a point in every language since the comma separates the list.
- **MLflow** (finding 15, `f45f6afd`): the registered version has the tag `tabularia.distribution` (the distribution fitted; with auto the one chosen), read from the card as `tabularia.purchase_model`.
- **BIC with weights** (finding 18): not changed; `docs/capabilities/models.md` says that n is the number of rows used, whatever their weights (`46445680`).
- **Follow-up, the two points left open** (commits `20f8e6a9`, `03b2016f`; the docs' sentence on the covariance travelled in `a8c9e305`, another lot's commit that staged the file whole). **The node's own covariance** (`ps_covariance`, taken by every step of `ps_newton` and `ps_newton_stale`) is now the Hessian from the rows' derivatives, with one Richardson extrapolation at the row level (h = 1e-3 and h / 2 relative to max(1, |η|): 37 evaluations of the rows for three parameters), centred as before; numDeriv's Hessian of all the parameters stays only where a row's derivative is not finite or the Hessian is not negative definite; the steps' gradient is still numDeriv's. It holds for the six distributions, with strata (the shape's dummies are columns of its own η) and with a start (each row's −log S(start) is a function of the same η), so Richardson's is kept nowhere by design. Against numDeriv's on the reference tables the standard errors move by 3e-8 relative at most (gengamma with a start), but for the log-logistic with strata, 9.3e-7, where the rows' derivatives are the closer to survreg's analytic standard errors (1e-9, numDeriv's 9e-7). **`expected.json` was not regenerated: every case passes it as it is**; no tolerance was widened. A test compares the covariance with survreg's (Weibull, log-normal, log-logistic with strata, 1e-7) and with numDeriv's (1e-6). **Auto's generalized gamma** (measured first: 10.7 s of the choice's 18 s, more than the node's own fit of about 3 s once the covariance is fast) is taken by Newton steps straight from the point nested in it of the better of the Weibull and the log-normal just fitted, no BFGS; flexsurv's fit from the three starts only when the steps do not reach the maximum (a step below `PS_AT_MAXIMUM` = 1e-4 standard errors with −H positive definite); when it is chosen, flexsurv's object for native.rds is made at its estimates with every parameter fixed (`ps_attach_flexsurv`, its log-likelihood checked against the runner's). The checks' larger distribution goes the same way (BFGS first only as the fallback). `ps_auto_failing`'s generalized gamma still does not converge, and every auto case keeps its choice. **Self-test 97 ok, none failed.** Times, 1 CPU (`docker --cpus=1`), the same tables:

  | case | | before P12 | after the first round | now |
  |---|---|---|---|---|
  | 5,000 rows, 30 levels (32 parameters), Weibull | whole fit | 145 s | 45 s | **13.0 s** |
  | | node's own fit (BFGS + Newton) | ≈ 35 s | ≈ 37 s | **≈ 3 s** (Newton 1.7 s) |
  | | comparison with the generalized gamma | 103 s | 3.9 s | **0.7 s** |
  | 5,000 rows, 30 levels, auto | whole fit | — | 57.7 s | **15.8 s** |
  | | the choice (six candidates) | — | 18.0 s (gengamma 10.7 s) | **8.1 s** (gengamma ≤ 3.4 s, the curves included) |
  | 1,000 rows, 3 levels, Weibull | whole fit / comparison | 5.3 s / 0.66 s | 5.0 s / 0.20 s | **5.0 s / 0.25 s** |
  | 1,000 rows, 3 levels, auto | whole fit / the choice | — | 6.0 s / 2.9 s | **5.4 s / 3.0 s** |

  What is left of a wide fit's time is the profile's chart (3.3 s: the delta method's jacobian per stratum) and fixed costs; on 1,000 rows the time is the process's own. The four report fixtures were written before this follow-up: their standard errors would move by 1e-8 or less, so they were not written again.
