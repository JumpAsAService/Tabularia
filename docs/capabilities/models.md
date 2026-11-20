# Statistical models in a flow

A hypothesis test answers a yes-or-no question. A model answers «how much»: how the quantity
sold changes with the price, which customers are likely to order, how long a customer lasts,
what next month will look like. Tabularia fits these models inside a flow: drop the node of
the model after any step, choose its columns and options in the panel, press **Run**, and read
the answer in the same place as any other step, as a **table of results** the next nodes can
use and a **Report** of a few pages whose first page says whether the model's assumptions hold.

Six models are in the first release, in three groups: **regression** (linear, logistic, both
with ridge, lasso and elastic-net penalties), **survival** (Kaplan–Meier, Cox) and **time
series** (ARIMA, from AR to SARIMAX, with the orders given or searched for). A seventh node,
**Predict**, applies a model you registered in MLflow to new rows.

**They need Kubernetes.** A model is not computed by an engine: it runs as a Job in your
cluster, in an image built from a recipe Tabularia ships, with R. On Docker Compose the
models are in the palette greyed out («Kubernetes only»), the section in Administration says
«Requires Kubernetes», and a flow with a model step cannot run it. The [hypothesis tests](statistics.md), which run on every engine, are the part of
statistics that works everywhere.

## At a glance

| | |
|---|---|
| Where | The flow editor's palette, group **Models** (after **Statistics**), with a subtitle for each group an administrator enabled and you may use. **Predict** is in the same group. |
| What a model node does | Reads the rows that reach it, fits one model in a Job of its group, and outputs a table (the ten columns of the statistics, or the rows with the fitted values added, or the survival table, or the forecast) |
| What you see | **Table** and **Report** tabs under the canvas, filled by **Run** in the node's panel or by a run of the flow |
| Downloads | The report as **PDF** or **Excel**, from the Report tab |
| Who turns it on | An administrator, group by group, in **Administration → Statistical models** (below) |
| Who may use it | Whoever may edit the flow and may use the group; running needs RUN on the flow's folder |
| Record | Optionally an [MLflow](../connections/mlflow.md) server: parameters, metrics, the report and a registered version for Predict |
| Not exportable | A flow whose output reads a model cannot be exported to [dbt](../dbt-export.md#limits) (`model_not_exportable`) |

## Turning a group on (administrators)

**Administration → Statistical models** lists the three groups Tabularia ships, each with its
models, the number of packages its image pins and its state. Only an administrator acts, and
not with an API token; an observer reads the section. Every action is in the
[audit log](../governance/audit-log.md) (`models.group_enable`, `models.group_disable`,
`models.group_rebuild`, `models.group_settings`, `models.group_access`).

What it takes first is the same as for the Python and R of [scripting](../scripting.md): the
chart's `scripts.enabled` with its registry (see [Kubernetes](../deploy/kubernetes.md#statistical-models)),
and the **scripts bucket** set in **Administration → Scripting environments**, because a model
reads its input from it. Without the bucket the section says so and the models do not run.

| State | Meaning |
|---|---|
| **Not enabled** | Nothing is built; the nodes are not in the palette |
| **Building** | A Job in the build namespace builds the image from the group's recipe: R 4.6 and its packages at pinned versions, without internet access at run time |
| **Checking** | The image is built and its **self-test** runs: the models are fitted on small reference tables and compared with values computed outside Tabularia |
| **Ready** | Every value matched; the version is in use |
| **Failed** | The build or the self-test failed; the version in use, if any, is kept. The section shows the build log and the self-test's output |
| **Rebuild needed** | This Tabularia ships a newer recipe than the image in use; a new version is queued by itself, and until it is ready the group's steps do not run |

**Enable** creates the group's own scripting environment (managed by Tabularia, you do not
edit its packages) and queues its first version. **Build again** makes a new version from the
shipped recipe, after a failure or to start over. **Disable** takes the nodes out of the
palette and stops the steps that use them with a sentence; the images stay, and enabling the
group again uses the current version at once. A flow that holds a node of a group that is not
enabled opens, shows the node as unusable with the reason, and does not run.

A version is used only when its self-test passed on the recipe this Tabularia ships. The
section keeps every version with its status, its image reference, the packages installed
(the lock), the build log and the self-test.

### Limits and access

Open **Resources, access and versions** on a group once it is enabled. Changes apply from the
next run, without building again.

| Setting | Default | Meaning |
|---|---|---|
| CPU (limit) | regression 2, survival 2, time series 1 | The Job asks for half and may use up to this |
| Memory (limit) | 4 GiB, 4 GiB, 2 GiB | At least 256 MiB; the Job asks for half. A Job over it is stopped by the cluster and the step says so |
| Longest run | 30 minutes | 1 to 1,440 minutes. A node's own timeout (900 seconds unless you change it) must be within it |
| Most input rows | 2,000,000 (regression), 1,000,000 (survival), 20,000 (time series) | 1 to 1,000,000,000. Empty follows the recipe's number |

**Who may use it:** *everyone who can edit flows* (the default), or only the user groups you
tick. Administrators always can. With a list and none ticked, only administrators.

The Job runs in the scripts' namespaces (the editor's **Run** in the trials' namespace and
its quota), with **internet access off**: it reaches the scripts bucket through the egress
proxy and nothing else, under the same isolation as a Script node. The code is Tabularia's
own and never yours: the columns' names travel as data and are renamed before any formula is
built, so a column called `x); system("id")` is a label and nothing more.

## Using a model in a flow

1. Open **Models** in the palette, drag the model onto the canvas after the step that holds
   the data (dropped on a node it becomes its next step).
2. In the panel, choose the columns and the options. The form shows only the options that
   apply to what you chose (turning a penalty on hides the standard errors and shows the
   penalty's strength and folds; automatic orders hide p, d and q), greys out a combination
   the model forbids and says why, and lists a column's values where it asks for one.
3. Press **Run**, or run the flow.

A model node has one input on the left and one output on the right. A model with nothing
after it still runs: its results stay with its run, its report can be opened and MLflow gets
its log. **Longest run** and **Retries** (0 to 5) are on the node, as on a Script node.

**Which rows count.** A row with an empty or `NaN` value in a column the model uses is left
out (`n_missing`); nothing is filled in. An infinite value stops the node and names the
column. A text or boolean predictor is a category, compared with its **reference level** (the
first in text order unless you choose); a date is refused as a predictor with a sentence that
says to compute a number from it first, the month or the days since a start.

**What stops a model, and what only gets a remark.** A failed assumption never stops a model:
it is a row with its verdict and its advice, and the report puts it on the first page. What
stops it is what makes the numbers meaningless: complete separation under maximum likelihood,
a singular design (the term that repeats another is named), no events, too few rows (more
rows than terms plus one), a series shorter than the model needs. The step then fails with a
sentence that says what to do.

**Determinism.** Every random choice (the cross-validation folds, the sample of points drawn)
uses `seed`, 1 by default. The same rows on the same version of the group give the same
numbers.

## The models

The examples use the sample databases of the Docker Compose stack, which a Kubernetes install
can load too ([Flows](flows.md#example-revenue-by-region-and-channel-on-the-sample-data) says
how to reach them): `order_rows` (`quantita`, `prezzo_unitario` and `sconto_pct`, stored as
text: put a **Change type** to *decimal* first), `orders` (`data_ordine`, `canale`, `stato`)
and `crm_activities` (`tipo`, `esito`, `durata_min`). They show how a node is filled in; run
them on your own data to read numbers that mean something.

### Linear regression

Model id in the catalogue: `linear`.

Explains a number with other columns. **Outcome** is a numeric column, **Predictors** are
numbers and categories, with **Interactions** (pairs of predictors), **Reference levels** and
an **Intercept** that is on unless you turn it off.

| Choice | Options |
|---|---|
| **Estimation** (no penalty) | Ordinary least squares, or weighted (a **Weights** column is then required) |
| **Standard errors** (no penalty) | Classical, robust HC0 to HC3, robust to autocorrelation (HAC; **Order of the rows** names the column that orders them) or clustered (**Cluster column**) |
| **Penalty** | None, ridge, lasso, elastic net (**Lasso share** from 0 to 1, or `cv` to choose it). The penalty's strength λ is chosen by cross-validation (**Largest within one error of the best**, or **The smallest error**) or given; **Cross-validation folds** (3 to 20), **Keep together by** (a column whose values never split between folds), the **error measure** (squared or absolute) and whether to standardize the predictors |

The penalized variant gives no standard errors, p-values or intervals: inference after a
selection is not offered. It gives the coefficients, which are kept, the path of λ, the
cross-validation curve and the fit out of sample.

**Assumptions checked** (no penalty), each with its null hypothesis, statistic, p-value, a
verdict (ok, doubtful, failed or not computed) and what to do:

| Check | What it looks for | What it advises when it fails |
|---|---|---|
| Ramsey RESET | A missing curve | Add a square, a log or an interaction |
| Breusch–Pagan, White | Changing spread of the residuals | Robust standard errors (HC3); with robust ones already chosen the inference does not rely on this |
| Shapiro–Wilk, Anderson–Darling, Jarque–Bera | Normality of the residuals | The intervals rely on many rows; with few, transform the outcome |
| Durbin–Watson, Breusch–Godfrey (order 1 and 4) | Residuals correlated in the order of the rows | HAC standard errors, or model the time. They depend on the order of the rows: choose **Order of the rows**, otherwise they are not computed |
| VIF, condition number | Predictors that repeat each other (VIF over 5 doubtful, over 10 failed; condition number over 30) | Drop or combine them, centre them, or turn a penalty on |
| Cook's distance, leverage, Bonferroni outlier test | Rows that pull the fit | Look at the rows named |

With weighted estimation the RESET is the F test of the weighted model and the independence
tests run on the transformed regression. A penalized fit has one check, the choice of λ: it
warns when the best λ is at an edge of the path or when the chosen one keeps no term.

*Example:* `order_rows`, after a **Change type** of the three numbers: outcome `quantita`,
predictors `prezzo_unitario` and `sconto_pct`, standard errors robust HC3. Does the quantity
move with the price, and does the spread grow with it?

### Logistic regression

Model id in the catalogue: `logistic`.

Explains a yes-or-no outcome. **Outcome** is any column with the **Value that is the event**
named; the other values are «no event». Options: **Estimation** (maximum likelihood, or
**Firth's correction**, which keeps estimates finite with few events or separation; logit
link only), **Link** (logit or probit), **Standard errors** (classical, HC0 to HC3,
clustered), **Confidence intervals** (Wald, or profile likelihood with classical errors), the
**Classification threshold** (0.5) and the same **Penalty** block as the linear regression (error
measure: deviance, area under the ROC curve, or misclassification rate).

The results carry the coefficients with their odds ratios and intervals, the AUC with its
interval, the Brier score, the confusion matrix at the threshold (accuracy, sensitivity,
specificity, precision), the deviance, McFadden's, Nagelkerke's and Tjur's R², and the
likelihood-ratio test.

**Assumptions checked:** no separation (a predictor that splits the outcome completely: one
row per term the check names), Hosmer–Lemeshow calibration, linearity of the logit
(Box–Tidwell, one numeric predictor at a time), VIF, **events per variable** (at least 10),
Cook's distance, leverage and deviance residuals (a row over 3 in absolute value is named).
A penalized fit checks separation, EPV and the choice of λ; Hosmer–Lemeshow is not computed
for it, and the calibration chart is read out of fold.

*Example:* `crm_activities`: outcome `esito`, event value `ordine`, predictors `tipo` and
`durata_min`. Which kind of contact ends in an order, and how much do minutes matter?

### Kaplan–Meier

Model id in the catalogue: `kaplan_meier`.

The survival curve: the probability of still waiting for an event over time. **Follow-up
time** is a numeric column, **Event column** and its **Value that means the event
happened**; the rest are censored (their time is the last moment known). Options: a
**Strata column** (one curve per value, at most 20), the **confidence band** (log, log–log or
plain), the **test between strata** (log-rank, or Peto–Peto, in which early differences weigh
more) and whether to **compare each pair** of strata, **Survival at times** (up to 10 times
reported with their intervals) and **Restricted mean up to** a time (the longest observed
time by default).

The table gives, per stratum, the rows, events, censored, the median and the quartiles with
their intervals, the survival at the times asked, and the restricted mean. The **survival
table** output is the step table of every curve (time, at risk, events, censored, survival and
its band), to send on to other nodes.

**Assumptions checked:** that censoring tells nothing of the time to the event (it cannot be
tested from the data: the report says to ask how the rows came to be censored), proportional
hazards between strata (the curves must not cross), the share censored in each stratum, the
follow-up of each stratum (by the reverse Kaplan–Meier) and whether the numbers are read
where at least 10 rows are still at risk. Stratum names are `column=value`, or `all`.

### Cox proportional hazards

Model id in the catalogue: `cox`.

How columns change the rate of an event over time. The time, event and event value are as in
Kaplan–Meier, with **Predictors**, **Interactions**, **Reference levels**, a **Strata
column** (a baseline hazard of its own for each value), **Tied times** (Efron, the usual
choice, Breslow or exact), **Standard errors** (classical or robust, with a **Cluster
column**) and a **Start time column** for rows that give an interval (start, stop) instead of
a follow-up time; then the number of rows is the number of intervals.

The results carry the hazard ratios with their intervals, the likelihood-ratio, Wald and
score tests, the concordance, and, as rows output, the linear predictor, the risk, the
martingale and the deviance residuals. The survival curve the report draws is for a row that
sits at the means of the predictors.

**Assumptions checked:** proportional hazards for each term and globally (Schoenfeld: it
advises to stratify by the term, or to let it interact with time), the linear form of a
numeric predictor (a test against a natural spline with 3 degrees of freedom, and the
martingale residuals' chart), influence (dfbeta: over 2/√n doubtful, over 1 failed; not
computed with exact ties), VIF and **events per coefficient** (at least 10).

### ARIMA

Model id in the catalogue: `arima`.

A time series modelled on its own past, and forecast. **Time column** (a date, a datetime or
a whole number), **Series** (a number), **Frequency of the series** (hourly, daily, weekly,
monthly, quarterly, yearly, or whole numbers), and **Seasonal period** `s` (empty: from the
frequency; at most 60). One row per period, no period twice.

The palette offers the same node under six names, each dropped with its orders set: **AR**,
**MA**, **ARMA**, **ARIMA (p, d, q)**, **SARIMA** and **SARIMAX**. They are one node; the
name in the report follows what the orders make of it, for example `SARIMA(1,1,1)(0,1,1)[12]`.

| Choice | Options |
|---|---|
| **Orders: manual** | p, d, q (up to 5, 2, 5) and the seasonal P, D, Q (up to 2, 1, 2); **Drift** (a straight-line trend, when there is differencing) or **Mean** (a constant, when there is none) |
| **Orders: automatic** | Tabularia searches (stepwise, or all candidates) for the best orders by AICc, AIC or BIC, bounded by the largest p, d, q, P, D, Q you allow, with the unit-root test (KPSS, ADF or Phillips–Perron) and the seasonal test (seasonal strength or OCSB) that choose the differencing. The report lists the candidates and says why each difference was taken. With the default Drift off the search does not try a drift |
| **Exogenous columns** | Other columns that explain the series (SARIMAX); their future values must be in the rows after the last observed one, and then the forecast covers exactly those rows |
| **Transform** | None, log, or Box–Cox with λ given or chosen |
| **Forecast steps**, **Rows kept out** | `horizon` (up to 1,000) and a hold-out, whose last rows are left out of the fit to measure the forecast (RMSE, MAE, MAPE, MASE) |

The output **The series and its forecast** gives the observed values, the fitted ones and the
forecast with its 80% and `1 − alpha` bands.

**Assumptions checked:** stationarity of the differenced series (ADF, KPSS and the four cases
of the two together), that every root of the AR and MA polynomials is outside the unit circle
(a modulus at most 1 failed, under 1.05 doubtful), white-noise residuals (Ljung–Box at 10 lags
and at 2·s lags), constant variance (ARCH LM, and the first third of the residuals against the
last) and normal residuals (Jarque–Bera). Statistics that depend on how many rows there are
are «not computed» when there are too few.

*Example:* `orders` with a **Group by** `data_ordine` and the *distinct count* of `id`: one row
per day. ARIMA with the time `data_ordine`, the series the count, daily frequency, seasonal
period 7 and automatic orders: what a week looks like, and the next two.

### Boosting (Python): being built

Model ids in the catalogue: `xgboost` and `catboost`. A fourth group, **Boosting**, written in
Python, is being built: gradient-boosted trees for a regression, a binary or a multiclass
outcome, with a validation part, early stopping, the importance of each predictor (gain and
SHAP) and diagnostics in the report. In this version its nodes do not fit a model yet: they stop
with «… is not built yet in this version of Tabularia». This section will describe them when
they do.

## The output

Every model writes the same **ten columns** as the [statistical tests](statistics.md#the-output):
`section`, `level`, `level_2`, `stat`, `key`, `x0`, `x1`, `value`, `text` and `ord`, one fact
a row, the key unique and a number that is not finite empty. A flow can read it like any
other table, and a **Filter** on `section` is how you take the coefficients out. The sections:

| Section | Holds |
|---|---|
| `model` | The fit: rows used, rows left out, the model's numbers (R², AIC, AUC, concordance…), and texts: the model, the variant, the formula in symbols, the software, the group's version, whether the rows came from a trial or a run, and a `note_…` row for every doubt the numbers raised (few events for the terms, a boundary λ, so many rows that every test rejects) |
| `coef` | One row per term and statistic: estimate, standard error, statistic, p-value, interval, odds or hazard ratio, VIF, and `significance` with R's codes. For a category, the level is in `level_2` |
| `assumption` | One row per check and statistic: `h0`, `verdict`, `advice`, the statistic, its degrees of freedom, the p-value, its stars and the numbers of the rows the check names |
| `cv`, `candidate` | The path of a penalty (λ against the error, with its band and the terms kept), and the candidates of an automatic ARIMA |
| `stratum`, `pair` | Kaplan–Meier: each stratum, and each pair of strata |
| `cell` | The confusion matrix |
| `point`, `curve`, `lag`, `hist` | What the charts draw: points, lines with their bands, the correlogram's lags, the histogram's bins. At most 2,000 points a chart; above that a sample with the seed, keeping the ten most extreme points, labelled |

What the node hands to the step after it is the **Output** option:

| Model | Output |
|---|---|
| Linear, logistic, Cox | **The results table** (the default), or **the rows**, the input's columns with the fitted values added (`pred_fitted`, `pred_residual`, `pred_prob`, `pred_class`, `pred_risk`…). The input must not already have a column with one of those names |
| Kaplan–Meier | **The results table**, or **the survival table** |
| ARIMA | **The results table**, or **the series and its forecast** |

The model's own results are kept with the run whatever the output is, so the report can always
be drawn.

## The Report tab

The **Report** tab, beside **Table** and **Chart**, draws the pages of the model: A4, in the
language of the interface (English, Italian, German, Spanish or French).

1. A header with the model's name, the step's name, the date and the sample line when there is
   one.
2. A summary: four cards chosen for the model (for the linear: rows, R², adjusted R², σ), and
   the formula.
3. **The assumptions, on the first page**: one row per check, with what it checks, the null
   hypothesis, the statistic, the degrees of freedom, the p-value with R's stars, the verdict
   in words and with a mark, and the advice. Above 5,000 rows a sentence says that the tests
   reject very small departures and to read the charts.
4. The coefficients with their stars, and an interval chart (the odds ratios and hazard ratios
   on a log scale).
5. The fit: every number of the `model` section.
6. The diagnostic charts, each under the check it illustrates: residuals against fitted
   values, the normal Q–Q chart, scale–location, residuals against leverage, the ROC curve,
   the calibration, Schoenfeld, martingale residuals, the correlograms, the forecast.
7. Strata, pairs, the confusion matrix, the forecast or the candidates, where the model has them.
8. The notes, the rows left out, the software and the group's version, and a footer.

**−**, **+** and **Fit width** change the zoom, **Expand** raises the panel. **PDF** downloads
the pages; **Excel** the tables of the assumptions, the coefficients, the fit and the strata or
the forecast, one sheet each, with the numbers and not the charts. As for the statistics, the
report is drawn from the results the run kept: **no query reaches your data to draw it**, and
no Job starts. It is not an object in a folder, so it cannot be saved, scheduled or put on a
dashboard; a run of the flow can send its PDF to MLflow (below).

## Trying a model: Run, and «not up to date»

The editor never starts a Job by itself. **Run** in the node's panel (**Save and run** when
the flow has unsaved changes) fits the model on **the development sample of the sources**,
without the nodes after it, in the trials' namespace and its quota. It needs RUN on the flow's
folder. The Table and Report tabs then show the latest result of the node, the trial or the
flow's run, whichever is newer, with a line that says which: «From the model's run of …, on
the sample» or «From the flow's run of …».

If the node or any step before it changed since, the numbers stay and are marked **not up to
date**: «run it again to see its numbers now». They are never redrawn from a guess.

A trial is a trial: it logs nothing to MLflow. A run of the flow fits the model on every row
that reaches it, up to the group's limit, and logs.

The **Runs** page shows a model step like any other, with its detail: the group and its
version, the rows read, the attempts, the exit code, the refusal when the model said no, the
log and, with MLflow, the experiment and the run, with a link to MLflow.

## MLflow

A model node has an optional **MLflow** block: the **connection** (a type [MLflow
connection](../connections/mlflow.md) you may use), the **Experiment** (required with a
connection) and **Register the model as** (optional). Tabularia does not run MLflow.

**What a flow run records.** After the step has its results, and before its files leave the
scripts bucket, the gateway:

1. finds the experiment by name and creates it when missing (tag `tabularia.created = true`;
   one in MLflow's trash gets a sentence of its own);
2. opens a run named after the flow, the node and the date, with
   - **parameters**: the node's, flattened, and the model's formula;
   - **metrics**: every number of the `model` section, and for each assumption its statistic
     and p-value, as `assumption.<check>[.<level_2>].<statistic>`;
   - **tags**: `tabularia.url` (the run's page), the flow's id and name, the node, the model
     and its group, the group's version, the image digest, whether the rows were a development
     sample (`tabularia.dev_sample`), who launched it, the number of rows and
     `mlflow.source.name`. Values are cut at 250 characters, the limit of MLflow's batches;
   - **files**, through the server's artifact proxy: `tabularia/results.parquet`,
     `tabularia/coefficients.csv`, `tabularia/report.pdf` (the Report tab's layout, in the
     flow owner's language), and `model/MLmodel`, `model/model.json` and `model/native.rds`.
     A server that keeps its runs' files elsewhere gets none of them, and the run page says
     so; a PDF that cannot be drawn is a warning, not a failure;
3. with **Register the model as**, adds a version of that registered model from the run's
   `model` folder, with the tags `tabularia.model`, `tabularia.group`,
   `tabularia.group_version` and `tabularia.card_sha256`: what Predict checks;
4. ends the run as finished.

**When MLflow cannot be reached,** the step fails with «the model was fitted but MLflow could
not record it» and the reason. With **Continue without MLflow** on, the step succeeds with a
warning instead and the run page says «Not logged, the step went on». A step that fails after
its Job ran is logged to MLflow as a failed run, as far as the server answers.

**No secret reaches the Job.** The connection's password or token is decrypted in the
gateway, which makes the calls; the Job's `task.json` only says that files for MLflow are
wanted, and it holds no MLflow variable, address or token. The Job has no internet access in
any case.

`model/model.json` is the **model card**: the fitted numbers (coefficients, covariance,
baseline hazard, the orders and the coefficients of an ARIMA…), at most 1 MB, in a format of
Tabularia's own (`tabularia-model`). Its sha256 is the tag `tabularia.card_sha256`.
`native.rds` is R's own object, kept for people who want to open the fit in R; Tabularia
never loads it. It holds **no row of the input**: before it is written the runner removes the
model frame, the design and the outcome, every value that has one number per row (residuals,
fitted values, linear predictors, weights, a Cox model's stratum of each row, the
cross-validation's out-of-fold predictions and folds), the QR decomposition of the design
(only its R, the first rows, stays) and the environment of the formula. What stays gives the
same predictions as the fit: `predict(fit, newdata)` for a linear or logistic regression
(with `se.fit` when you pass the residual scale, which the report gives), the linear
predictor and the risk of a Cox model (`reference = "sample"`), and `predict(fit, newx, s =)`
for a penalized fit. What needs the rows does not work on it (`summary()`, `residuals()`, a
Cox model's `survfit()`): the report and `coefficients.csv` have those numbers. Two objects
are kept whole, and they are data: a Kaplan–Meier's `survfit` is the curve itself (one step
per event time), and an ARIMA's `forecast::Arima` keeps the series, which `forecast()` needs
to go on from its last values. The columns inside are named by position (`d1`, `d2`…), as
the runner names them.

A model card larger than 1 MB does not stop the step: its results and its report are there,
with a note, but the model is not logged to MLflow (no `model/` files) and no Predict node can
apply it.

## Predict

The **Predict** node applies a model registered in MLflow to the rows that reach it, and adds
the predicted columns. It is in the same palette group, for whoever may use at least one
enabled group and has CONNECT on an MLflow connection.

In its panel choose the **connection**, the **experiment** (the list is read from the server),
and the **version**: one of the versions Tabularia registered in that experiment, newest first,
or the **latest version** of a registered model. A version is fixed: the same numbers, the
same predictions, whoever promotes another later. **Latest** is read again at each run, resolved
to a number then, and the run records the number: it is the highest version of that
registered model that carries the tag `tabularia.model`, in any experiment (the experiment
filters the lists only). A version registered by something other than Tabularia is not offered
and is refused.

> **Pin a version in production.** With **latest**, whoever has CONNECT on the MLflow
> connection can register a new version of the same model name, with the tags Tabularia reads
> (`tabularia.model` and the others), and the flows that use **latest** predict with it at
> their next run: the predictions change without anyone editing or approving the flow. The
> card's fingerprint only says the card was not changed after registration, not who registered
> it. For the flows that run in production choose a **fixed version**, and give CONNECT on the
> connection only to those who may change what those flows predict.

**What it checks before reading a row,** and a step that fails any of it stops before any Job
exists:

- the version carries Tabularia's tags and its `MLmodel` has the `tabularia` flavor;
- **integrity**: the sha256 of its `model.json` equals the version's `tabularia.card_sha256`
  tag, so a card changed after it was registered is refused;
- the card is one this Tabularia applies: its format is `tabularia-model`, its format
  version one this release reads, its model, group and variant known. The version of the
  group it was fitted with may differ from the one that applies it: the numbers are the same
  to every version, and the run page says so;
- the card's group is enabled, ready and usable by you, and its limit on rows holds for the
  rows to predict;
- the card's input columns are in the rows, of the same kind, and none of the columns to add
  is already there.

**The Job never sees MLflow.** The gateway reads the version and gives the Job its card;
nothing is run from the server, and no serialized object is ever loaded: only the card's
numbers. Predicting the rows a model was fitted on gives its fitted values to within 1e-9,
which each group's self-test checks.

**Columns added**, always `prediction`, and with options for the model:

| Model | `prediction` | Options |
|---|---|---|
| Linear | The fitted value | **Prediction interval**: `prediction_low`, `prediction_high` (not for a penalized model, which warns and adds none) |
| Logistic | The probability of the event | **Class threshold**: `prediction_class` (0.5 when empty, or the card's) |
| Kaplan–Meier | The survival at the row's time, in the row's stratum (empty past the stratum's last time) | **Prediction interval**: `prediction_low`, `prediction_high` |
| Cox | The risk score, relative to the means of the fit | **Survival at times**: `survival_at_<t>` for each time |
| ARIMA | The forecast, with the fitted coefficients, no re-estimation | **Forecast steps**; **Prediction interval**: `lower_80`, `upper_80`, `lower`, `upper` |

An option that does not belong to the model of the card is refused. A level of a category the
model did not see gives an empty prediction on its rows, and the run page counts them. A
Predict step logs nothing to MLflow: its run records the registered model, the version it
used, the card's hash and the group's versions. **Run** in its panel works like a model's,
and needs its connection and version chosen first.

## Limits, and what it refuses

| Limit | Value |
|---|---|
| Input rows | The group's: 2,000,000, 1,000,000 or 20,000 unless an administrator changed it. Beyond it the step fails before the Job, with «the model would read 3,400,000 rows; the group allows 2,000,000: filter or sample before the node» |
| Terms of the design, after encoding categories | 200 |
| Levels of a categorical predictor | 50 |
| Strata (Kaplan–Meier, Cox) | 20 |
| Interactions | 20 pairs |
| Times of «survival at» | 10 |
| Seasonal period | 60 |
| Forecast steps | 1,000 |
| Rows | More than terms plus one; with a penalty, 3 per fold, at least 2 terms, and with the AUC measure 10 per fold; at least one event; for ARIMA enough observations for its orders and season |
| Run time | The group's longest run (30 minutes by default); the node's timeout, 900 seconds unless you change it, must be within it |
| Model card | 1 MB: the covariance (above 200 terms) and the baseline hazard (above 5,000 times) are dropped first, then the card is refused |

Every refusal is a sentence that says what to do. A model started on an installation without
Kubernetes, or without the scripting machinery or the scripts bucket, says so before anything.
The start of a Job takes seconds when the image is on the node; the first run on a node pulls
it, and an R image with its packages is more than a gigabyte.

**What it does not do.**

- **No model beyond the six**: no multinomial or ordinal logistic, mixed models, additive
  models, Poisson or negative binomial, quantile regression, parametric survival, competing
  risks, exponential smoothing; no inference after a penalized selection.
- **No code of yours**: the runners are Tabularia's. For another model, write it in a
  [Script node](../scripting.md) in R or Python.
- **No fit in an engine.** A model is not an operation of the five engines; the rows that
  reach it are written to the scripts bucket, read by the Job, and deleted at the end.
- **No report as a saved object.** As for the statistics, it lives in the editor, in the PDF
  you download and in the MLflow run.
- **Not exportable to dbt**: an Output that reads what a model or Predict node writes is
  refused with `model_not_exportable`; a model that feeds no exported Output is left out of
  the project with a note, and Tabularia keeps running it with the flow.
- **Not on Docker Compose.**
- **Predict is a Job**, not an expression: a few rows cost as much as a thousand, a few seconds
  of start-up. Only a model Tabularia registered with its card is applied.

## Where Tabularia and R differ

Every number is checked against values computed outside Tabularia: R 4.6 with its own
packages, and for some models a second tool (statsmodels for the linear regression and ARIMA,
lifelines for Kaplan–Meier and Cox). The self-test of each group repeats the comparison, on
the cluster, before the group can be used. A few conventions are Tabularia's own and the
report says so:

- the VIF of a categorical term is the generalized VIF to the power 1/df, comparable with the
  usual limits of 5 and 10;
- the Cook's distance verdict: over 1 failed, over 4/n doubtful; leverage over 2p/n is doubtful;
- Hosmer–Lemeshow's degrees of freedom are the groups made minus 2;
- Cox's risk score is relative to the means of the fit, and its survival curve is for a row at
  the means, not for each stratum's.

## Related

- [Statistics](statistics.md): the hypothesis tests, and the output table the models share
- [Flows](flows.md#models): the nodes and the editor
- [MLflow connections](../connections/mlflow.md): the server and the test
- [Python and R](../scripting.md): the scripting machinery the groups are built with
- [Kubernetes](../deploy/kubernetes.md#statistical-models): what the chart needs
- [Engine differences](../engines/engine-differences.md#statistical-models): why a model is not an operation
- [Export to dbt](../dbt-export.md#limits)
