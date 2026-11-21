# Statistical models in a flow

A hypothesis test answers a yes-or-no question. A model answers «how much»: how the quantity
sold changes with the price, which customers are likely to order, how long a customer lasts,
what next month will look like. Tabularia fits these models inside a flow: drop the node of
the model after any step, choose its columns and options in the panel, press **Run**, and read
the answer in the same place as any other step, as a **table of results** the next nodes can
use and a **Report** of a few pages whose first page says whether the model's assumptions hold.

Ten models are in the first release, in five groups: **regression** (linear, logistic, both
with ridge, lasso and elastic-net penalties), **survival** (Kaplan–Meier, Cox, and parametric
survival: the time to an event described by a Weibull, log-normal or another distribution, chosen
or searched for), **time series** (ARIMA, from AR to SARIMAX, with the orders given or searched
for), **boosting** (XGBoost and CatBoost: gradient-boosted trees to predict a number or a class,
with the importance of each predictor) and **customer lifetime** (customer lifetime value and
BG/BB: for customers who leave without saying so, the purchases to expect, the probability of
still being active and the discounted value of each customer). An eleventh node, **Predict**,
applies a model you registered in MLflow to new rows.

**They need Kubernetes.** A model is not computed by an engine: it runs as a Job in your
cluster, in an image built from a recipe Tabularia ships, with R (or Python, for boosting). On Docker Compose the
models are in the palette greyed out (the tooltip says «Needs Kubernetes, with this feature turned on in the Helm chart: not available on this installation»), the section in Administration says
«Requires Kubernetes», and a flow with a model step cannot run it. The [hypothesis tests](statistics.md), which run on every engine, are the part of
statistics that works everywhere.

## At a glance

| | |
|---|---|
| Where | The flow editor's palette, group **Models** (after **Statistics**), with a subtitle for each group an administrator enabled and you may use. **Predict** is in the same group. |
| What a model node does | Reads the rows that reach it, fits one model in a Job of its group, and outputs a table (the ten columns of the statistics, or the rows with the fitted values added, or the survival table, or the forecast, or the scores of each customer) |
| What you see | **Table** and **Report** tabs under the canvas, filled by **Run** in the node's panel or by a run of the flow |
| Downloads | The report as **PDF** or **Excel**, from the Report tab |
| Who turns it on | An administrator, group by group, in **Administration → Statistical models** (below) |
| Who may use it | Whoever may edit the flow and may use the group; running needs RUN on the flow's folder |
| Record | Optionally an [MLflow](../connections/mlflow.md) server: parameters, metrics, the report and a registered version for Predict |
| Not exportable | A flow whose output reads a model cannot be exported to [dbt](../dbt-export.md#limits) (`model_not_exportable`) |

## Turning a group on (administrators)

**Administration → Statistical models** lists the five groups Tabularia ships, each with its
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
| **Building** | A Job in the build namespace builds the image from the group's recipe: R 4.6 (Python 3.14 for boosting) and its packages at pinned versions, without internet access at run time. The customer lifetime recipe also installs a system library, the GNU Scientific Library (`libgsl-dev`), with the image's package manager, and the build records the version installed |
| **Checking** | The image is built and its **self-test** runs: the models are fitted on small reference tables and compared with values computed outside Tabularia |
| **Ready** | Every value matched; the version is in use |
| **Failed** | The build or the self-test failed; the version in use, if any, is kept. The section shows the build log and the self-test's output |
| **Rebuild needed** | This Tabularia ships a newer recipe than the image in use; a new version is queued by itself, and until it is ready the group's steps do not run |
| **Rechecking** | This Tabularia ships a new runner for the group (the code that fits the models), not a new recipe: the self-test runs again on the image in use, without a build. The version serves meanwhile; it becomes **Failed** only if the self-test reports values that differ |
| **Recheck retrying** | The cluster stopped that self-test before it reported (a Pod never scheduled or refused, an image not pulled, the deadline, a node gone, a log not given): that says nothing of the image, so the version serves on and the self-test runs again a minute later, up to five times (a node's most retries, at its default delay): six attempts in all. After the sixth the group is **Ready** again: the version serves on, self-tested with its previous runner, and the section says why; **Build again** checks it |

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
| CPU (limit) | regression 2, survival 2, time series 1, boosting 2, customer lifetime 2 | The Job asks for half and may use up to this |
| Memory (limit) | 4 GiB, 4 GiB, 2 GiB, 4 GiB, 2 GiB | At least 256 MiB; the Job asks for half. A Job over it is stopped by the cluster and the step says so |
| Longest run | 30 minutes (boosting 60) | 1 to 1,440 minutes. A node's own timeout (900 seconds unless you change it) must be within it |
| Most input rows | 2,000,000 (regression), 1,000,000 (survival), 20,000 (time series), 1,000,000 (boosting), 1,000,000 (customer lifetime) | 1 to 1,000,000,000. Empty follows the recipe's number |

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
its log. Steps can follow it from the start: before any Run they already know the columns it
will hand on ([below](#trying-a-model-run-and-not-up-to-date)). **Longest run** and
**Retries** (0 to 5) are on the node, as on a Script node.

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
text: put a **Change type** to *decimal* first), `orders` (`data_ordine`, `cliente_id`, `canale`, `stato`),
`crm_activities` (`tipo`, `esito`, `durata_min`) and `customers` (`canale`, `regione`, `agente_id`, and the
customers' lives: `data_acquisizione`, the day the customer was acquired, and `stato`, the CRM's
status `attivo`, `inattivo` or `nuovo`). They show how a node is filled in; run
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
reported with their intervals) and **Restricted mean up to** a time (by default the shortest of the strata's
follow-ups, so that every stratum is read over the same span; without strata the longest observed
time).

The table gives, per stratum, the rows, events, censored, the median and the quartiles with
their intervals, the survival at the times asked, and the restricted mean. The **survival
table** output is the step table of every curve (time, at risk, events, censored, survival and
its band), to send on to other nodes.

**Assumptions checked:** that censoring tells nothing of the time to the event (it cannot be
tested from the data: the report says to ask how the rows came to be censored), proportional
hazards between strata (curves that cross fail it; Grambsch–Therneau's test below the significance level
makes it doubtful; when that test cannot be made and no curves cross, it is not computed, with the reason),
the share censored in each stratum, the
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
computed with exact ties), VIF and **events per coefficient** (at least 10). With exact ties and many
events at one time the exact likelihood cannot be computed: the node refuses and says to use Efron's. A start
not before its time, or too close to it for the survival package to tell them apart, is refused, naming the row.

### Parametric survival

Model id in the catalogue: `parametric_survival`.

The time to an event, described by a named distribution. Kaplan–Meier draws the curve and Cox
compares rates, and neither says anything beyond the last time observed. This node fits a
distribution to the times (Weibull, exponential, log-normal, log-logistic, generalized gamma or
Gompertz, or the one that suits best) and then answers **how long**: the median time, any quantile
and the probability of lasting past a time, for each row of a table, with intervals, also beyond
the follow-up. It gives each predictor as a **time ratio** (the factor by which it lengthens or
shortens the time) or as a **hazard ratio** (the factor on the rate, comparable with Cox's).
Use it when you need a time (the months a customer stays, the days until a part fails) or when
the shape of the hazard matters; use Cox when only the effects are asked and no distribution
should be assumed.

**Follow-up time**, **Event column** and **Value that means the event happened** are as in
Kaplan–Meier: every other value is censored at its time. A time of 0 has no density under any of
the distributions, so such rows are left out and counted, with a note that says to count the time
from a moment before them; a negative time is refused.

| Option | Default | Meaning |
|---|---|---|
| **Predictors (optional)** | none | Numbers, texts and booleans (a text is a category compared with its **reference level**); a date is refused. With none, the node fits the distribution of the times alone, per stratum |
| **Reference levels**, **Interactions** | | Only with predictors: the level each category is compared with, and products of two predictors (at most 20) |
| **Strata column** | none | A baseline of its own for each value, at most 20: the column enters the typical time **and** the shape (one shape per value; the exponential has no shape, so its strata change the rate only) |
| **Weights column** | none | Case weights, 0 or more, each multiplying its row's likelihood. Rows of weight 0 are left out; weights that are not whole numbers get a note, because the standard errors read them as counts of rows |
| **Start time column** | none | **Delayed entry**: the subject was not observed before this time (it entered the study, or the machine was bought second-hand, already that old). The likelihood of the row is conditional on having lasted until its start. A start of 0 is no start. Refused when it is not before the time, or negative |
| **Distribution** | Weibull | Weibull, exponential, log-normal, log-logistic, generalized gamma, Gompertz, or **auto** |
| **AIC margin for the simplest model** | 2 | Only with **auto**: from 0 to 10 (below) |
| **Quantiles of the time** | 0.25, 0.75 | At most 9, between 0 and 1: reported for the profile and written in the rows and by Predict, besides the median, which is always there |
| **Survival at times** | none | Up to 10 times at which S(t) is reported |
| **Time already survived** | none | A number column: how long each row's unit has already lasted. The answers become the **time left** (below) |
| **Significance level** | 0.05 | The level of every interval (1 − alpha) and of the verdicts of the checks ([How a check speaks](#how-a-check-speaks-verdicts-facts-and-measures)) |
| **Seed** | 1 | Only the sample of points drawn in the charts: the fit has no random choice |
| **Output** | The results table | **The results table**, or **the rows** |

**The distributions.**

| Distribution | The hazard | The predictors act on | The coefficients, as reported |
|---|---|---|---|
| Weibull | constant, rising or falling | the time (accelerated failure time) | time ratios; also hazard ratios, except with strata |
| Exponential | constant | the rate | time ratios and hazard ratios |
| Log-normal | rises, then falls | the time | time ratios |
| Log-logistic | falling, or rises and then falls | the time | time ratios |
| Generalized gamma | rising, falling, an arc or a bathtub; the Weibull and the log-normal are special cases | the time | time ratios |
| Gompertz | rising or falling exponentially (falling: a share of the rows never has the event) | the rate | **hazard ratios only** |

A **time ratio** above 1 says the predictor lengthens the time to the event (1.5: half as long
again); a **hazard ratio** above 1 says it makes the event come sooner. For the same Weibull
they are one the inverse of the other, raised to its shape. **Gompertz has hazard ratios only**:
scaling its time changes its shape too, so «the time ratio of x» is not a constant of that
model; the report says so where a time ratio would stand. A **Weibull with strata has no hazard
ratio** either, since each stratum has its own shape: its time ratios are common to all. Beside
every hazard ratio the report gives **Cox's** on the same rows, for comparison.

**Auto.** With *auto* the node fits all six distributions in order of cost and keeps the one
with the **lowest AIC**, with the same predictors, strata and weights. It then applies a rule of
parsimony: when a simpler distribution **nested** in the winner (the exponential in the Weibull,
the Gompertz and the generalized gamma; the Weibull and the log-normal in the generalized gamma)
has an AIC within the **AIC margin** of the lowest, the simplest such distribution is chosen
instead, and the report says it in a sentence. A margin of 0 always takes the lowest AIC; the rule
does not chain. The report lists every candidate with its log-likelihood, AIC, BIC, ΔAIC and
**Akaike weight** (the share of support among the candidates: two weights close to each other mean the two
distributions describe the times about equally well), marks the chosen one, and draws Kaplan–Meier's curve
with that of each candidate. A candidate that does not converge from two starting points stays in
the table marked, out of the choice and of the weights, and a note names it; if none can be
fitted the step refuses. When more than half of the step's timeout has passed, the candidates not
yet fitted are left out (status *time*, with a note).

**What the node gives.**

- **The results table**: the `model` section (rows, events, the distribution used, log-likelihood,
  AIC, BIC, the test of the predictors, the concordance, the last observed times, `alpha`), the
  `coef` section (estimates on the log scale, time ratios or hazard ratios with their intervals,
  Cox's beside, VIF), `parameter` (the distribution's own parameters, per stratum, with their
  intervals), `stratum` (the **profile**: a row with every number at its mean and every category at
  its reference level, per stratum, with the median, the quantiles and the survival at the times
  asked, each with its interval, and Kaplan–Meier's median beside), `candidate` (with *auto*), the
  checks and the points of the charts.
- **The rows** output: the input's columns with `pred_linear`, `pred_median`, `pred_quantile_<p>`
  and `pred_surv_at_<t>`, each with `_low` and `_high` columns, and on the rows of the fit
  `pred_cox_snell`, `pred_martingale` and `pred_deviance` (residuals). The input must not already
  have one of those names.
- The **intervals** of what is predicted come from the delta method, computed from a covariance
  Tabularia builds itself (the Hessian of the log-likelihood, from each row's derivatives in the
  distribution's few parameters: fast whatever the number of predictors; a Richardson Hessian of all of
  them only where a row's derivatives fail), the same for every distribution; a median or quantile the distribution never reaches (a Gompertz whose hazard
  falls, so that part of the rows never has the event) is left empty, with a note.

**The time left, and delayed entry.** They are two different things. **Start time column** acts
on the **fit**: where each row begins to be observed. **Time already survived** acts on the
**prediction**: how long each unit to be predicted has already lasted. With it, the median and
the quantiles are the time **left** (the `q` such that S(a + q) = ½ S(a)), and the survival at
`t` is the probability of lasting `t` more, S(a + t) / S(a), with intervals that follow the
conditioning. A row with no value, a negative one or an infinite one gets an empty prediction and a
warning that counts them. Predict takes the same option and no start: the card holds the
parameters of the fit in which the entry is already.

**Assumptions checked.** Verdicts come only from tests with a p-value, read against the node's
significance level:

- for the exponential, the **constant hazard** (a likelihood-ratio test against the Weibull; for the
  distributions that have a shape, the same test is a number of the fit, not a check), and the
  distribution against the **generalized gamma** (the Weibull, the log-normal, the exponential).
  The larger distribution of these two tests starts from the node's own fit (the Weibull is the
  generalized gamma at Q = 1, the log-normal at Q = 0, the exponential the Weibull of shape 1) and
  is taken to its maximum by Newton steps that cost little; with *auto* it is the candidate
  already fitted. It is given as much time as the node's own fit took (at least 10 seconds),
  checked while it runs: a test that needs longer, or that starts after half the step's timeout,
  is **not computed** and says why, as when the larger distribution does not converge. The
  likelihood-ratio tests of the fit (the predictors, the shape against the exponential) follow
  the same rule: out of time, a note says which is missing;
- **proportional hazards**, for each term and globally (Grambsch–Therneau, as in Cox), for the
  Weibull, the exponential and the Gompertz with predictors, which are the distributions that assume it;
- two facts: **convergence** (a positive definite Hessian: without it the intervals are empty) and
  **separation** (a level of a category among whose rows no event happens, which leaves its
  coefficient with no limit).

**Measures, with no verdict:** the **agreement with Kaplan–Meier** (the largest gap in survival,
from 0 to 1, between the fitted mean curve and Kaplan–Meier's, where it happens and how many of
Kaplan–Meier's standard errors it is; points with fewer than 10 rows still at risk are not read), the **influential rows**
(the largest change one row makes to each coefficient, with the five rows that make it), **events
per parameter**, the **VIF** and the **extrapolation**, per stratum (how far beyond that stratum's last time observed the
medians, quantiles and times asked go). The VIF is shown in the tables of the coefficients, beside
each term. Cox–Snell's residuals are a chart. With a start time column every check is still
written, read on the rows from their entry. Kaplan–Meier's curve is then drawn with the rows at
risk at each time, and the fitted curve beside it is the mean, over the rows, of their survival
conditional on lasting until the first time read, each row weighed by its chance of it: when rows
enter at different times this is **an approximation** of what Kaplan–Meier estimates, not the
same quantity, and the report's note says so.

**AIC and BIC with weights.** The BIC is −2 ℓ + k log n with **n the number of rows** used, whatever
their weights: with case weights that stand for several units each, the BIC is that of the rows,
not of the units they count (the AIC does not depend on n). The comparison of the candidates of
*auto* reads the AIC.

**The report** (four or five pages): the first has the checks, the measures and the significance
level; then the coefficients with their interval charts and the distribution's parameters, the fit,
the choice of the distribution with *auto*, the fit to the data (Kaplan–Meier against the fitted
curve, Cox–Snell, log(−log S) for the Weibull and the exponential), the profile with its survival
and hazard curves and bands, the influence, and the notes.

**What it refuses**, with a sentence that says what to do: no event, a negative time, start or weight,
a time, start or weight that is not a number, a start not before the time (or so close to it that the
survival package cannot tell them apart), a column of the rows output already in the input, a stratum with no event (its shape cannot be estimated), fewer rows
than the parameters plus 2, a design with a term that repeats another, more than 20 strata, a date as
predictor, a fit that does not converge from two starting points, and with *auto* none of the six
fitting.

**Limits.** Strata 20; quantiles 9; times 10; the rows of the group (1,000,000 unless an
administrator changed it).

*Example:* how long does a customer last? From **Sample / Private tables**, drop `customers`,
**Select columns** `id`, `canale`, `data_acquisizione` and `stato`, **Rename** `id` to
`cliente_id`, and keep the customers acquired inside the data's window with a **Filter**
`data_acquisizione` *greater than or equal to* 2023-07-01 (the first orders of the older ones are
not in the data). On a second branch drop `orders`, a **Filter** `stato` *not equal to*
`annullato`, and a **Group by** `cliente_id` with the *maximum* of `data_ordine` as `last_order`.
**Join** the branch into the customers, type *left* so that those who never ordered stay, on
`cliente_id`. The CRM calls a customer `inattivo` after six months with no order, so the time of
those is the days from the acquisition to the last order (the customer lasted until then; the six
months of silence are not counted), and the time of the others is the days from the acquisition to
the end of the data, 2026-07-15, where they are censored. Two **Computed columns**, in the engine's
dialect (here Polars; DuckDB spells the difference `date_diff('day', a, b)` and ClickHouse
`dateDiff('day', a, b)`):

| Name | Expression |
|---|---|
| `days_to_end` | `CAST(DATE '2026-07-15' AS INTEGER) - CAST(data_acquisizione AS INTEGER)` |
| `days` | `CASE WHEN stato = 'inattivo' THEN CAST(COALESCE(last_order, data_acquisizione) AS INTEGER) - CAST(data_acquisizione AS INTEGER) ELSE days_to_end END` |

Then a **Filter** `days` *greater than* 0 (a customer whose only order is on the day of the
acquisition, or who has none, has no time to be read), and **Parametric survival** with the time `days`, the
event column `stato`, the event value `inattivo`, the predictor `canale` and the distribution
*auto*. Read the first page (the checks and the measures), then the candidates, then the time ratio
of each channel against the reference channel, and the profile's median. On the sample data at
the `small` size this gives 2,034 customers, 697 of them lost, and *auto* keeps the Weibull (the
generalised gamma is within 2 points of AIC and has one parameter more, so the simpler one wins); the
convergence check is ok. The channels differ: the likelihood-ratio test of the predictor has
p < 0.001, the time ratio of `Supermercati` against the reference channel `Ingrosso` is about 4.3 and
that of `Ristorazione` about 2.1 (both significant), while `Negozi` and `Online` are not
distinguishable from `Ingrosso`. The profile's median, for a customer of `Ingrosso`, is about 550 days.
For the customers still active, a second **Parametric survival** is not
needed: register the model in MLflow, and a **Predict** with **Time already survived** set to a
column that holds each customer's days since acquisition gives the median time left.

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

### Boosting (Python): XGBoost and CatBoost

Model ids in the catalogue: `xgboost` and `catboost`.

The **Boosting** group holds two nodes, **XGBoost** and **CatBoost**. Both fit gradient-boosted
trees: many small decision trees, each one correcting the mistakes of those before it. They are
the models to reach for when you want to **predict** and not to explain: many predictors, effects
that bend, interactions nobody wrote down. They give no coefficient and no p-value. They give how
well the model predicts rows it did not learn from, which predictors it leans on, and a few
diagnostics. When the question is «how much does the price move the quantity», use a
[regression](#linear-regression); when it is «which orders will be late», use these.

**They are written in Python**, unlike the three groups above, and run as a Job like the others: an
image built from a recipe with pinned packages (`xgboost-cpu`, `catboost` and what they need),
without internet access at run time.

**Turning it on.** The group is in **Administration → Statistical models** beside the others
(**Enable**, **Build again**, **Disable**, the limits and who may use it: everything above holds).
The image is a download of wheels, not a compilation, so it builds in minutes. The build needs
PyPI, or your mirror of it (`SCRIPTS__PYPI_INDEX_URL`), as a Python environment of
[scripting](../scripting.md) does. The defaults are 2 CPUs, 4 GiB, a longest run of 60 minutes and
1,000,000 input rows. The group must be **Ready**, its self-test passed, before a node of it runs.

**Tasks.** The first choice in the form is the **task**:

| Task | The outcome column | What the model predicts |
|---|---|---|
| `regression` | A number | The value, in the outcome's units |
| `binary` | Two distinct values (text, boolean or integer); `event_value` names the one whose probability is predicted | The probability of the event, and the class at the **threshold** (0.5 unless you change it) |
| `multiclass` | 3 to 20 distinct values | The probability of every class, and the likeliest |

The report changes with the task: RMSE, MAE and R² for a regression; AUC, log-loss, the ROC curve,
calibration and the confusion matrix for a binary outcome; accuracy, log-loss, a table per class and
the confusion matrix for a multiclass one.

**Main parameters** (the rest of the form is the same for both nodes, and the report writes every
value used):

| Parameter | Default | Meaning |
|---|---|---|
| `outcome`, `predictors` | needed | Numbers, texts and booleans as predictors, at most 200; a date is refused (compute a number from it first). A predictor may have **missing values**: both libraries handle them, unlike the regressions, which leave the row out |
| `weights` | none | Case weights, used in the fit and in every metric |
| `class_weight` | none | `balanced` weights the classes against each other in the fit, and in the losses early stopping and the diagnostics read (the validation part weighted the same way, the baseline one share per class); the metrics table stays unweighted. The probabilities are then no longer calibrated, and a diagnostic says so |
| `validation`, `validation_share` | random, 20 % | How the validation part is drawn (below) |
| `early_stopping`, `early_stopping_rounds` | on, 50 | Stop when the validation measure has not improved for that many trees |
| `stop_metric` | loss | What early stopping watches: the loss (RMSE or log-loss), `mae` (regression), `auc` (binary), `error` (classification: for a binary task the share of rows on the wrong side of a probability of 0.5, XGBoost's `error` and CatBoost's `ZeroOneLoss`, not of the node's `threshold`). The AUC and the error rate move in steps and can stop the boosting on their noise after a few trees: `trees_limit` then reads the loss curve and is doubtful (the advice `stop_on_loss`) |
| `max_trees` | 1,000 | The most trees. A multiclass round grows one tree per class |
| `learning_rate` | 0.05 | How much each tree counts |
| `max_depth` | 6 | The depth of a tree: up to 12 for XGBoost, 10 for CatBoost |
| `subsample` | 0.8 | The share of rows each tree sees |
| `seed` | 1 | The split, the libraries' randomness and the samples |
| `output` | results | `rows` adds the model's columns to the input rows (`pred_value` and `pred_residual`, or `pred_prob` and `pred_class`, with `pred_part`: `train` or `validation`) |

XGBoost has also `min_child_weight`, `colsample_bytree`, `reg_lambda`, `reg_alpha` and `gamma`;
CatBoost `l2_leaf_reg`, `rsm` and `random_strength`. The defaults are not the libraries' own: with
early stopping on, a small learning rate with some sampling of rows is the safe start, and a
written value makes the fit reproducible from its parameters. Turn them only when a diagnostic
asks you to.

**Categories** (text and boolean predictors) are used as they are, not expanded into one column per
level: XGBoost with its native categorical splits, CatBoost with its own encoding. So an
importance is one number per predictor, and a level not seen when the model was fitted follows the
missing-value path. A column with more than 1,000 distinct values is refused, with the question
«is it an identifier?».

**The validation part, and early stopping.** Every run sets some rows aside, never used to fit the
trees. They are used to choose how many trees to keep (early stopping), to compute every metric
that says «on rows the model did not learn», and to run the diagnostics. How they are chosen:

- **random** (the default): a random share of the rows; a classification keeps each class's share
  in both parts (`stratify`). With `split_by`, rows with one value of that column (a customer's
  rows) all fall on the same side;
- **time**: with `order_by`, the last rows in time are the validation part, and no instant falls on
  both sides. Choose it when the goal is to predict later rows: a random split lets the model peek
  at the future.

> **The validation numbers are slightly optimistic.** The same part chooses the number of trees, so
> the model has been tuned a little to it. Read them as «about this good», not as a promise; the
> report says so in a note. A third part kept for testing only is not in this version.

A run fails with a sentence when the training part has fewer than 20 rows or the validation part
fewer than 10, when a binary outcome has a class missing from one part, or when a multiclass class
is missing from the training part.

**Importance, gain and SHAP.** Two answers to «which predictors does the model use»:

- **Gain**: how much the splits on a predictor improved the fit, summed over the trees. It is
  cheap and comes from the trees themselves.
- **SHAP**: for each row, how much each predictor pushed the prediction up or down from the
  average. The report shows the mean of its absolute value, in the model's own scale (the
  outcome's units, or log-odds for a classification), and the ranking follows it. It is computed
  on a **sample** of at most 5,000 rows, fewer for a very large model, drawn with the seed: the
  report says «SHAP on 5,000 of 812,340 rows».

The **Report** draws two bar charts (the first 20 predictors, the rest summed), and for a
regression or a binary outcome the **beeswarm**: one line per predictor, one point per sampled row.
A point to the right of 0 raised the prediction, to the left lowered it; its colour is the
predictor's value, from low (blue) to high (red), grey for a category or a missing value. A
predictor whose red points sit on the right pushes the prediction up as it grows. A multiclass
model shows the bars by class instead. Then the learning curves (training against validation, with
the best round marked) and, by task, observed against predicted, residuals, ROC, calibration and
the confusion matrix.

> **Importance is not cause.** It says what the model uses, not what changes the outcome. Two
> correlated predictors share it unpredictably.

**Diagnostics, not tests.** Where the regressions have tests of assumptions, these models have
**diagnostics**: checks read against rules of thumb. They have **no p-value and no level of
significance**, and the report says so under the table. A **doubtful** or **failed** verdict says
where to look, not that the model is wrong; each comes with advice. They look at:

| Diagnostic | What it looks at |
|---|---|
| `validation_size` | Whether the validation part has enough rows (the rarest class, for a classification) for its numbers to mean something |
| `beats_baseline` | Whether the model predicts the validation part better than the plain average (or the class shares) |
| `overfitting` | Which share of the validation gain over the baseline the trees after the best one give back. With early stopping on the loss the tree kept is the best by construction: the check is then *not computed*, and its value is the validation loss over the training loss, for information |
| `trees_limit` | Whether early stopping stopped by itself, or the trees ran out, the time ran out or very few were kept |
| `class_imbalance` | How uneven the classes are (classification) |
| `calibration`, `calibration_slope` | Whether a binary model's probabilities match the rates observed, and whether they are too extreme or too timid |
| `importance_stability` | Whether the predictors weigh the same (mean \|SHAP\|) on the validation rows as on the training rows: a shift between the parts, such as later rows in a split in time, shows here. It does not say that another sample would rank them the same: on pure noise it is ok too |
| `zero_importance` | The predictors no split uses |
| `leakage` | Whether one predictor explains nearly everything, or the fit is nearly perfect: a sign that a column was computed from the outcome, or recorded after it |

Each row of the `assumption` section has its verdict, its advice, the number looked at in `value`
and the limit it was read against in `threshold`. The rules of thumb are Tabularia's choice, written
in the report beside the number. Read an **ok** as «nothing here», and a **failed** `leakage` first:
a model that is too good is usually a column that should not be there.

**Output.** The ten columns, with two sections of their own: `importance` (gain, its share, the
mean absolute SHAP, its share and rank, the same on the training and validation rows, the share of
missing values; per class for multiclass) and `class` (multiclass: support, precision, recall, F1
and AUC per class). The `model` section holds the sizes of the two parts, the trees kept, the
best round, whether it stopped early or by time, the threads, and the metrics of the task for the
training part, the validation part and the baseline.

**The saved model, and Predict.** With an MLflow block, the run writes, besides the card
(`model/model.json`), the model itself as a file of its own: **`model/xgboost.json`** (XGBoost's
JSON format) or **`model/catboost.cbm`** (CatBoost's binary format). Both are model data the library
parses; **no pickle, joblib or any format that runs code is ever written or read.** Its size and
sha256 are in the card, the card's sha256 is the version's tag `tabularia.card_sha256`, and the
version carries `tabularia.variant` (the task) and `tabularia.model_file_sha256`. The model file
may be 64 MiB at most, and a deep tree is large (XGBoost's JSON takes about 8 KB a tree at depth 6
and 260 KB at depth 12; a multiclass round grows a tree per class). So the boosting **stops before
the file would pass the limit**: XGBoost measures its file as it grows, CatBoost caps its trees
ahead (each adds 2^depth × (classes + 1) numbers). The trees kept are the best so far, the note
`model_size` says so and `trees_limit` is doubtful. A file that still came out larger gets a
note, the fit goes on with its results and report, and the model is not logged.

**Predict** applies such a version like the others (see [Predict](#predict)), with one more check:
the gateway streams the file from MLflow to the scripts bucket and compares its **fingerprint** with
the card's; if it differs, the file is not applied. The Job checks it again before reading. Rows
with a missing value, or a category the model never saw, get a prediction through the model's
missing-value path (the run page counts them), where a regression leaves them empty. It adds
`prediction` (a regression's value, a binary model's probability, a multiclass model's likeliest
class), `prediction_class` (binary, at the node's threshold or the card's), and for multiclass
`prediction_probability` and `prediction_prob_<class>`.

**Limits.**

| Limit | Value |
|---|---|
| Input rows | 1,000,000, unless an administrator changed it |
| Cells (rows × predictors) | 50 million: «1,000,000 rows × 120 predictors make 120 million values; this group allows 50 million: fewer predictors, or sample before the node» |
| Predictors, classes | 200; 20 |
| Levels of a categorical predictor | 1,000 |
| Trees | 5,000 at most |
| SHAP sample | 5,000 rows at most |
| Points in a chart | 2,000 (the beeswarm splits them among its predictors) |
| Model file | 64 MiB: the boosting stops before it (the note `model_size`) |
| Time | The group's longest run (60 minutes) and the node's timeout. Boosting stops at 60 % of the timeout so that SHAP and the files still fit: the note `time_budget` says so, and `trees_limit` is doubtful |

SHAP is the long part: it can take several times as long as the training itself.

*Example:* from `order_rows` (after a **Change type** to *decimal* of `quantita`, `prezzo_unitario`
and `sconto_pct`), an **XGBoost** with the task `regression`, the outcome `quantita` and the
predictors `prezzo_unitario` and `sconto_pct`. Read the first page's diagnostics, then the
beeswarm: whether a higher price pushes the quantity down.

### Customer lifetime value (R)

Model id in the catalogue: `clv`.

For a shop, a web store or a donor, a customer who leaves does not say so: there is no churn column.
The «buy till you die» models estimate, from three numbers per customer, **how many purchases to expect**, **the
probability that the customer is still active** (P(alive)), the **expected spend** and the **discounted value** over a
horizon, the **customer lifetime value (CLV)**. They fit a population of customers who buy at
different rates and may leave at any moment, without any other column about the customer.
They are for **non-contractual** customers; where a contract ends (a subscription) there is a
date, and [Kaplan–Meier or parametric survival](#parametric-survival) are the tools.

The group **Customer lifetime** has two nodes. **Customer lifetime value** (this section) is for
customers who may buy at any moment, and has the purchase model inside; **[BG/BB](#bgbb-r)** is for
purchases at fixed occasions (a yearly campaign, a monthly renewal). In both, the spend model
(Gamma–Gamma) and the CLV are part of the node. The image of this group installs the GNU
Scientific Library, and the build records its version: the group's card in Administration says «GSL 2.7.1»,
with the package's whole version beside it. An image built before that record shows no version.

#### The input: one row per customer

The node takes **no transactions**: its input is one row per customer, which you build first with
the nodes of Tabularia ([below](#building-the-summary-per-customer)).

| Option | Meaning, and what is refused |
|---|---|
| **Customer column** | The customer's id. A repeated id is refused («the customer 4812 appears on 3 rows: one row per customer; group the transactions by customer first»); an empty one, or one of spaces only, leaves the row out |
| **Repeat purchases** (x) | The purchases after the first one, in the calibration period: a whole number, 0 or more |
| **Time of the last purchase** (t_x) | The time from the first purchase to the last one, in the unit below. 0 for a customer with no repeat purchase, and above 0 for every other (purchases of the same day must be one purchase); not more than the age (one above it by a rounding error is set to the age, with a note) |
| **Age of the customer** (T) | The time from the first purchase to the end of the calibration period: more than 0 |
| **Unit of time** | Days, weeks, months, quarters or years: the unit of the last two columns, the horizon, the holdout and the discounting. **Required**; a note asks whether the unit is right when the largest age passes 30 years in it |
| **Average spend** | Optional. The **average value of the repeat purchases** (the first one left out, as the spend model assumes), above 0 for a customer who bought again; empty leaves that customer out of the spend fit only. With it the node gives the expected spend and the CLV; without it, only the purchases and P(alive). Returns and refunds: leave those customers out, or net them before averaging |
| **Purchases in the holdout**, **Length of the holdout** | Optional, both or neither: the purchases each customer made in the period that follows the calibration end, and its length in the unit (the same for every customer). They give the holdout check; an empty cell leaves the customer out of it |

A row with an empty `frequency`, `recency` or `age` is left out; an infinite value stops the node
and names the column. Every refusal names one customer and says how many break the same rule. At least **20
customers, 5 repeat customers** and, with a spend, 5 of those with a spend are needed.

| Option | Default | Meaning |
|---|---|---|
| **Purchase model** | auto | **auto** fits Pareto/NBD, BG/NBD and MBG/NBD and keeps the one with the **highest log-likelihood** among those whose convergence check passes (the three have four parameters, so AIC and BIC rank them the same way). It checks them from the highest log-likelihood down and stops at the first that passes: the others are «not checked» in the comparison and have no `converged` row. When none passes, it keeps the highest whose log-likelihood is not numerical noise; a model whose fit fails is left out, with a note. Or one of the three. The holdout is never used to choose, so it stays an honest check |
| **Compare with the other models** | off | With a purchase model chosen: fits the two others for the comparison table only |
| **Periods ahead** | one year | 1 to 1,000, in the unit (365 days, 52 weeks, 12 months, 4 quarters, 1 year when empty) |
| **Yearly discount rate** | 0.10 | Only with a spend; 0 to 1, annual: the node derives the rate of one period |
| **Margin** | 1 | Only with a spend; above 0 and up to 1. **With 1 the CLV is of revenue**, and the report says so; set the margin to have a CLV of profit |
| **Significance level** | 0.05 | The intervals and the verdicts ([How a check speaks](#how-a-check-speaks-verdicts-facts-and-measures)) |
| **Seed** | 1 | The samples drawn for the charts |
| **Output** | **The rows** | Unlike the other models, the default is the per-customer scores; **the results table** keeps only the tables |

**The models, in plain words.** Customers buy at their own rate (a gamma distribution across
customers). **Pareto/NBD** lets a customer leave at any moment, at a rate that also varies;
**BG/NBD** lets a customer leave right after a purchase with a probability that varies (a beta
distribution), and **MBG/NBD** also after the first. They differ little when the data are good, and *auto*
lets the likelihood choose. **Gamma–Gamma** models the average value of a repeat customer's
purchases, assuming it does not depend on how often the customer buys (a check tests that). The
parameters are estimated by maximum likelihood on one row per distinct summary, so large
tables are cheap; Pareto/NBD, BG/NBD and Gamma–Gamma use the compiled likelihoods of the CLVTools package,
MBG/NBD and BG/BB are written in the runner and checked against independent implementations
([Where Tabularia and R differ](#where-tabularia-and-r-differ)).

**The CLV.** For each customer, the expected purchases of each period of the horizon, discounted at
the end of the period, times the expected spend, times the margin: the sum over the horizon. It is
a finite horizon you choose, not an infinite one: the far periods rest on the form of the model,
and a measure says when the horizon is more than twice the longest age the model learned from.

**What the node gives.**

- **The rows** (default): the input's columns with `pred_p_alive`, `pred_transactions` (the
  expected purchases over the horizon), with a holdout `pred_holdout_transactions` (without one the column is not
  added, and an input column of that name is no clash), and with a
  spend `pred_spend`, `pred_discounted_transactions` and `pred_clv`. P(alive) is a **probability
  from the model**, not an observed churn: a customer with a low value has not been seen to leave. A
  column of the input with one of those names is refused.
- **The results table**: the parameters with their intervals (`coef`, with the purchases' and the
  spend's apart), the `model` section (customers, repeat customers, log-likelihood, AIC, BIC, the mean
  purchase rate and dropout, the expected purchases, the total CLV, the holdout's numbers), the
  `candidate` section with *auto* or *compare*, the checks, the histograms, the P(alive) map and
  curves, and the **top 20 customers** by CLV (by expected purchases without a spend).

**Assumptions checked.** A verdict comes from two tests with a p-value, read against the significance level:
**frequency fit** (a χ² of the observed against the expected number of customers by number of
repeat purchases, adjacent classes pooled from the top until every one expects at least 5, the last open
(«12+»), the condition of the χ² test known as Cochran's rule; the chart shows the pooled classes, «5–7»; its
degrees of freedom are the classes less 1 less the parameters, and with none left the check is **not computed**;
the share gap is beside it as a number) and, with a spend, **spend independence** (the slope of the average spend
on the frequency, with HC3 standard errors; Pearson's r is its value. It is **not computed** when the frequencies
or the spends do not vary, or when one repeat customer alone holds a frequency and the others share one: that
customer's leverage is 1, and the slope is its alone). Two facts: **convergence** (the optimizer
converged, its log-likelihood is a real one and not the noise of a computation that lost its digits, and
the Hessian is positive definite; **doubtful** when an estimate sits at an edge, where the likelihood keeps
growing towards the limit, so that the estimate is that limit; **failed** otherwise; a fit that ends in numerical noise is never chosen by *auto*, one that only fails the check
is chosen only when no other passes it) and **spend tail** (the spend model's q must be above 2 for its variance to be
finite: **doubtful** at 2 or below; at 1 or below the average spend does not exist and the node refuses: *look for extreme amounts, or leave out the spend column*).
**Measures with no verdict**: **customers and repeat customers**, the **holdout** (the expected against the
observed purchases: the relative error of the total, and by frequency class in a chart; **not computed** when no
purchase was observed in it, since the error has no denominator; without a holdout the page says how to add one), the **horizon** against the longest age, and the **estimates at an edge** (one row for each
parameter where one more step towards 0 or towards infinity does not lower the likelihood, with what it means:
*almost nobody leaves*, or *the customers look alike*; no row when there is none).

**The report** (four pages): the first has the cards (customers and repeat customers, the purchase model and
its log-likelihood, expected purchases, the total CLV), the formula with the unit, horizon, discount and
margin («CLV of revenue»), the checks and measures and the parameters; then the comparison of the
purchase models, the fit (the repeat purchases observed and expected, the holdout by frequency class), the
P(alive) map and curves, the spend against its model and against the frequency, the distribution
of the CLV, the concentration curve («the top 10 % of the customers hold…») and the top customers, and the
notes.

**Predict** applies a registered version to the summaries of other customers and gives
`prediction` (the expected purchases over the horizon), `prediction_p_alive` and, with a spend,
`prediction_spend` and `prediction_clv`; its option is the horizon, in periods of the fit's unit.
Customers must come with a history (a frequency, recency and age of their own); a row that breaks
a rule of the summary gets empty predictions and a warning that counts them. Predicting the rows of
the fit gives the fit's own numbers.

**MLflow** records the parameters, `log_lik`, the CLV total, the holdout's error, the number of
every candidate, and the statistic, p-value and value of each check; the model the fit used
(the one *auto* chose, or the one given) is the parameter `purchase_model_chosen`, the horizon used the parameter
`horizon` when the node left it empty, and the registered version has the tags `tabularia.variant`
(`purchases` or `value`) and `tabularia.purchase_model`. No `native.rds` is written for this
group: the card holds every parameter.

**Limits.** At least 20 customers and 5 repeat customers; periods ahead 1,000 (above 520 steps the
sum is made in coarser steps, with a note); the rows of the group (1,000,000), 2 GiB of memory.

#### Building the summary per customer

You need a table of purchases, one row per purchase, with the customer, the date and, for the spend,
the amount. The nodes below exist in every flow; the expressions are written in the engine's
dialect (here Polars; DuckDB spells a difference of dates `date_diff('day', a, b)` and ClickHouse
`dateDiff('day', a, b)`). Purchases of the same day count as one purchase.

1. **Computed column** `day` = `CAST(ts AS DATE)` (skip it when the column is already a date), and a
   **Filter** `day` *less than or equal to* the calibration end.
2. **Group by** `customer`, `day`: the *sum* of `amount` as `day_amount`.
3. **Computed columns**, in this order: `first_day` = `MIN(day) OVER (PARTITION BY customer)`, then
   `repeat_amount` = `CASE WHEN day = first_day THEN 0 ELSE day_amount END`.
4. **Group by** `customer`: the *count* of `day` as `days`, the *minimum* of `day` as `first_day`,
   the *maximum* of `day` as `last_day`, the *sum* of `repeat_amount` as `repeat_total`.
5. **Computed columns**, with a week as the unit and `2025-07-15` as the calibration end:

   | Name | Expression |
   |---|---|
   | `frequency` | `days - 1` |
   | `recency` | `(CAST(last_day AS INTEGER) - CAST(first_day AS INTEGER)) / 7.0` |
   | `age` | `(CAST(DATE '2025-07-15' AS INTEGER) - CAST(first_day AS INTEGER)) / 7.0` |
   | `avg_spend` | `CASE WHEN days > 1 THEN repeat_total / (days - 1) END` |

   A **Filter** `age` *greater than* 0 leaves out customers whose first purchase is the calibration end.

   On DuckDB the two date expressions are `date_diff('day', first_day, last_day) / 7.0` for `recency`
   and `date_diff('day', first_day, DATE '2025-07-15') / 7.0` for `age`: `date_diff` counts the days
   from its second argument to its third.
6. For a **holdout**, a second chain from the same purchases: a **Filter** on the days after the
   calibration end, a **Group by** `customer` with the *distinct count* of `day` as
   `holdout_purchases`, then a **Join**, type *left*, on `customer` into the summary and a **Fill nulls**
   of `holdout_purchases` with 0.

A customer whose first purchase is after the calibration end is not in the summary, as the models
assume. The summary is what the node reads: Customer column `customer`, Repeat purchases `frequency`,
Time of the last purchase `recency`, Age `age`, Average spend `avg_spend`, Unit of time *Weeks*.

*Example:* the customers of the pasta and bakery company in **Sample / Private tables**, who buy
when they like and leave without a word (their `stato` is the CRM's guess, not a fact), with the
spend, a holdout of the following year and the calibration ending on 2025-07-15.

1. The amount of each order: drop `order_rows`, **Change type** `quantita`, `prezzo_unitario` and
   `sconto_pct` to *decimal*, a **Computed column** `ricavo_netto` =
   `quantita * prezzo_unitario * (1 - sconto_pct / 100)`, and a **Group by** `ordine_id` with the
   *sum* of `ricavo_netto` as `amount`.
2. Drop `orders`, **Select columns** `id`, `data_ordine`, `cliente_id` and `stato`, **Filter** `stato`
   *not equal to* `annullato`, **Rename** `id` to `ordine_id`, and **Join** step 1 into it, type *inner*,
   on `ordine_id`.
3. Drop `customers`, **Select columns** `id` and `data_acquisizione`, **Rename** `id` to `cliente_id`,
   **Filter** `data_acquisizione` *greater than or equal to* 2023-07-01 (the customers acquired inside the data's
   window, whose first order is in the data), and **Join** step 2 into it, type *inner*, on `cliente_id`.
4. The summary, from this table, as in the steps above with `cliente_id` as the customer,
   `data_ordine` as the day and `amount` as the amount: a **Filter** `data_ordine` *less than or equal to*
   2025-07-15, the **Group by** by customer and day, the two computed columns, the second **Group by** and
   the four columns of step 5.
5. The holdout, from the table of step 3: a **Filter** `data_ordine` *greater than* 2025-07-15, a **Group by**
   `cliente_id` with the *distinct count* of `data_ordine` as `holdout_purchases`, a **Join** (*left*, on
   `cliente_id`) into the summary and a **Fill nulls** of `holdout_purchases` with 0.

The node: Customer column `cliente_id`, Repeat purchases `frequency`, Time of the last purchase
`recency`, Age `age`, Average spend `avg_spend`, Unit of time *Weeks*, Purchases in the holdout
`holdout_purchases`, Length of the holdout 52.14 (the 365 days that follow the calibration end, in
weeks), Periods ahead 52, Purchase model *auto*, Output *the rows*. On the first page read the
frequency fit and the holdout: whether the model's expected purchases of the next year are near what
the customers did. Without `avg_spend` the same flow gives the purchases and P(alive) alone. These
data were built to suit the models, so they are a good place to learn to read the report; the
numbers are those of your run. On the `small` size this flow gives 1,282 customers and the
Pareto/NBD; the frequency fit is ok (p 0.56) and the holdout's expected purchases are 1.8% below
the observed ones. The check to watch is **Spend independent of frequency**: inside a channel a
customer's average order does not move with how often it orders, which is Gamma–Gamma's
assumption, but the channels differ both in the size of their orders and in their frequency
(`Supermercati` order often and in bulk, `Ingrosso` in bulk and rarely, `Online` rarely and
little), so over all the customers the two move together. With the calibration ending on
2025-07-15 the correlation is +0.06 and the check stays just inside its limit (p 0.054, verdict
*ok*); on a summary that reads the whole window, as the sample database's README describes, it
fails (p 0.010 on 2,054 customers). When the CLV matters, fit it per channel: keep `canale` in
step 2's **Select columns** and add a **Filter** `canale` *equal to* one channel before the
summary, one node per channel.

### BG/BB (R)

Model id in the catalogue: `bg_bb`.

The same questions for customers who can buy only at **fixed occasions**: a yearly campaign, a
monthly renewal, a weekly newsletter. Time is not continuous but counted in **opportunities**: a
customer who bought or not at each one, and who may leave after any. The node reads the same kind
of row per customer, and gives the same outputs and the same Predict, the same checks and a report of
the same pages, with these differences:

| Option | Meaning |
|---|---|
| **Repeat transactions** | The transactions after the first one, among the opportunities: a whole number, 0 or more, at most one per opportunity |
| **Opportunity of the last transaction** | 1, 2, 3…: which opportunity had the last transaction, 0 for a customer with none; it is not less than the repeat transactions nor more than the opportunities |
| **Opportunities** | How many opportunities the customer has had after the first transaction, from 1 to 1,000 |
| **Length of an opportunity** | Days, weeks, months, quarters or years: it discounts the CLV. Required |
| **Opportunities ahead** | The horizon, in opportunities: one year's worth when empty |
| **Opportunities in the holdout** | An integer from 1 to 1,000, with **Purchases in the holdout** (0 up to that number) |

There is no purchase model to choose and no comparison; the average spend, the discount rate, the
margin, the significance level and the output are as above. With the spend, the node fits
Gamma–Gamma and gives the CLV in the same way.

To build the summary from purchases by month: a **Computed column** `month` =
`(EXTRACT(YEAR FROM day) - 2023) * 12 + EXTRACT(MONTH FROM day)` (the months counted from a fixed one),
a **Filter** `month` up to the calibration end's, **Unique rows** on `customer` and `month`, and a **Group by**
`customer` with the *count* of `month` as `months`, the *minimum* as `first_month` and the *maximum* as `last_month`.
Then `frequency` = `months - 1`, `recency` = `last_month - first_month` and `opportunities` = the
calibration end's month minus `first_month`, the first month itself not being an opportunity.
Set the length of an opportunity to *Months*. A customer who bought twice in one month counts once, as the
model has one purchase or none at each opportunity.

*Example:* the same customers of the sample data, with a month as the opportunity: did the customer
order in each month? Build the summary as above, from the table of step 3 of the previous example, with
the month expression of the page (July 2023 is 7, June 2025 is 30) and the calibration end in June 2025,
then a **Filter** `opportunities` *greater than* 0: a customer whose first month is the calibration end
has no opportunity after it, and the node refuses a summary that holds one. On the `small` size 1,196
customers are left, the convergence is ok and the frequency fit too (p 0.68).
In the node, **Opportunities** `opportunities`, **Opportunity of the last transaction** `recency`,
Length of an opportunity *Months*, Opportunities ahead 12. These customers buy at a rhythm of their own
in continuous time, so for them *Customer lifetime value* is the natural choice; BG/BB is for data that
really come in occasions.

### A/B tests (R): being built

Model ids in the catalogue: `ab_test` and `ab_plan`. A group of two nodes, **A/B test** and **A/B test
planning**, is being built for experiments: users (or sessions, shops, devices) split at random between a
control and one or more treatments, with a metric measured on each. The first will say whether a treatment
moved the metric, whether the split of the units can be trusted and how sure the result is; the second will
size an experiment (units, days, the lift it can detect) from historical data. The nodes make no model: no
model card, no registration as a model, no Predict; a flow run logs to MLflow like any other. In this
version the nodes do not run yet: they stop with «… is not built yet in this version of Tabularia». This
section will describe them when they do.

## The output

Every model writes the same **ten columns** as the [statistical tests](statistics.md#the-output):
`section`, `level`, `level_2`, `stat`, `key`, `x0`, `x1`, `value`, `text` and `ord`, one fact
a row, the key unique and a number that is not finite empty. A flow can read it like any
other table, and a **Filter** on `section` is how you take the coefficients out. The sections:

| Section | Holds |
|---|---|
| `model` | The fit: rows used, rows left out, the model's numbers (R², AIC, AUC, concordance…), and texts: the model, the variant, the formula in symbols, the software, the group's version, whether the rows came from a trial or a run, and a `note_…` row for every doubt the numbers raised (few events for the terms, a boundary λ, so many rows that every test rejects). For the linear model, survival, ARIMA and customer lifetime a note's text is an id with its values, `@curves_cross pairs=A%20×%20B` (a space, a comma or a % in a value written %20, %2C, %25), which the report says in the page's language |
| `coef` | One row per term and statistic: estimate, standard error, statistic, p-value, interval, odds or hazard ratio, VIF, and `significance` with R's codes. For a category, the level is in `level_2` |
| `assumption` | One row per check and statistic: `h0`, `verdict`, `advice`, the statistic, its degrees of freedom, the p-value, its stars and the numbers of the rows the check names |
| `cv`, `candidate` | The path of a penalty (λ against the error, with its band and the terms kept), and the candidates of an automatic choice: the orders of ARIMA, the distributions of parametric survival (log-likelihood, AIC, BIC, ΔAIC, Akaike weight, status), the purchase models of customer lifetime |
| `parameter` | Parametric survival: the distribution's own parameters (shape, σ, Q…), per stratum, with their intervals |
| `customer` | Customer lifetime: the twenty customers of highest value, with their frequency, recency, age, P(alive), expected purchases and spend |
| `stratum`, `pair` | Kaplan–Meier: each stratum, and each pair of strata; for parametric survival, the **profile** of each stratum: its median, quantiles and survival at the times asked |
| `cell` | The confusion matrix; for customer lifetime, the map of P(alive) by frequency and recency |
| `point`, `curve`, `lag`, `hist` | What the charts draw: points, lines with their bands, the correlogram's lags, the histogram's bins. At most 2,000 points a series, fewer when the results would pass 100,000 rows: the series then share what is left. Above that a sample with the seed, keeping the ten most extreme points, labelled |

What the node hands to the step after it is the **Output** option:

| Model | Output |
|---|---|
| Linear, logistic, Cox | **The results table** (the default), or **the rows**, the input's columns with the fitted values added (`pred_fitted`, `pred_residual`, `pred_prob`, `pred_class`, `pred_risk`…). The input must not already have a column with one of those names |
| Kaplan–Meier | **The results table**, or **the survival table** |
| Parametric survival | **The results table** (the default), or **the rows**: the input's columns with the median time, the quantiles and the survival at the times asked, each with its interval, and the residuals (`pred_median`, `pred_quantile_<p>`, `pred_surv_at_<t>`, `_low`, `_high`, `pred_cox_snell`…) |
| ARIMA | **The results table**, or **the series and its forecast** |
| Customer lifetime value, BG/BB | **The rows** (the default, unlike the others): the input's columns with `pred_p_alive`, `pred_transactions`, `pred_clv` and the rest, or **the results table** |

The model's own results are kept with the run whatever the output is, so the report can always
be drawn.

The columns' types are known before the model runs, and are what a run writes: the model's
columns are numbers (`Float64`) but for `pred_class` and `pred_part`, texts; in the survival
table `stratum` is a text and `n_risk`, `n_event` and `n_censor` whole numbers (`Int32`); in the
forecast `time` has the type of the series' time column (a whole number as `Int32`, a date as a
date) and `is_forecast` is true or false; the results table has the statistics' types. In the
rows output of an R model (all but XGBoost and CatBoost) a whole-number column of the input comes
back as `Int32`. The catalogue declares these types, and a test checks them against what the
runners write.

## The Report tab

The **Report** tab, beside **Table** and **Chart**, draws the pages of the model: A4, in the
language of the interface (English, Italian, German, Spanish or French).

1. A header with the model's name, the step's name, the date and the sample line when there is
   one.
2. A summary: four cards chosen for the model (for the linear: rows, R², adjusted R², σ), and
   the formula.
3. **The assumptions, on the first page**: one row per check, with what it checks, the null
   hypothesis, the statistic, the degrees of freedom, the p-value with R's stars, the verdict
   in words and with a mark, and the advice. Parametric survival and customer lifetime also
   list, under the checks, the **measures** that have no verdict, and write the significance level
   ([How a check speaks](#how-a-check-speaks-verdicts-facts-and-measures)). Above 5,000 rows a sentence says that the tests
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

## How a check speaks: verdicts, facts and measures

The checks that open a report do not all say the same kind of thing. For **parametric survival and the
customer lifetime nodes** one rule holds: **no number of Tabularia's decides a verdict.** A check
speaks in one of three ways, and the report marks which.

| It is | What you see | Where it comes from |
|---|---|---|
| A **verdict from a test** | **ok** or **failed**, with the statistic, the degrees of freedom, the p-value, its stars and the advice | A test with a p-value against the node's **significance level** (`alpha`): **p ≥ alpha is ok, p < alpha is failed**. The level is written under the checks. When the test cannot be made, **not computed**, with the reason |
| A **fact** | **ok**, **failed**, or **doubtful** | Something true or false of the fit, whatever the size of the table: the optimizer converged and the Hessian is positive definite; a category has a level with no event; the spend model's variance is finite. They are warnings, not tests |
| A **measure** | A number and a sentence that says what it reads, **with no colour and no advice** | A quantity a test cannot judge, such as the largest gap between the fitted curve and Kaplan–Meier's, the influence of the most influential row, the events per parameter, the number of customers, the holdout's error, the horizon against the longest age. You read it; the report never calls it good or bad |

**Where to set alpha.** In the node's panel, the option **Significance level**, 0.05 unless you change it. It is
also the level of every interval of that node (an interval at 1 − alpha: 95 % by default), so a stricter
level, 0.01, widens the intervals and makes fewer checks fail. The p-values themselves do not change with
alpha, only the line between ok and failed, so you can see how close a check came.

**What a verdict means.** *Failed* says that the data are unlikely under the assumption the check
tests; it does not say how much it matters. With many rows a test rejects very small departures, so the
report says once that on a large table you should read the **measure and the chart** beside it (the share gap
next to the frequency fit, the gap in survival next to the agreement with Kaplan–Meier) before concluding the model is
wrong. With few rows a test rejects nothing, and *ok* means only that no departure was found. A failed
check never stops a model: it is a row with its advice, and the numbers are still there to use.

**Older models.** Linear and logistic regression, Kaplan–Meier, Cox and ARIMA have tests with p-values and,
where a quantity has no test, the classical thresholds of the literature (a VIF above 5 or 10, Cook's
distance), which give a **doubtful** verdict between ok and failed. The boosting models have diagnostics that are rules of thumb with advice
and no p-value, as their sections say. They are read as they always were.

## Trying a model: Run, and «not up to date»

The editor never starts a Job by itself. **Run** in the node's panel (**Save and run** when
the flow has unsaved changes) fits the model on **the development sample of the sources**,
without the nodes after it, in the trials' namespace and its quota. It needs RUN on the flow's
folder. The Table and Report tabs then show the latest result of the node, the trial or the
flow's run, whichever is newer, with a line that says which: «From the model's run of …, on
the sample» or «From the flow's run of …».

If the node or any step before it changed since, the numbers stay and are marked **not up to
date**: «run it again to see its numbers now». They are never redrawn from a guess.

**Before the first Run** a model has no rows, but its columns are known: they follow from its
options and the **Output** (above). The **Table** tab shows them as a header without rows, with
the line «The columns this model will give, from its options. Its rows come with a Run». The
steps after the model can be set up at once: a step that only keeps, drops, orders, renames,
casts or filters columns (Select, Drop, Reorder, Rename, Cast, Filter, Sort, Limit, Unique rows,
Drop nulls, Fill nulls) gets those columns in its panel, its lists and its formulas, and its Table
shows them too, through any number of such steps. After another kind of step (a Group by, a Join,
a computed column) the columns come with the model's Run, as the rows do. A name that depends on
the data is not known before the Run, and the line says so: a multiclass XGBoost or CatBoost writes
one `pred_prob_<class>` per class of the outcome. Once the model has run, its output is what the
steps after it read, as before. No Job starts for any of this. The flow editor's
[assistant](../integrations/ai.md) works the same way: it adds steps after a model and is told
their columns, worked out, with no rows until the model runs.

A trial is a trial: it logs nothing to MLflow. A run of the flow fits the model on every row
that reaches it, up to the group's limit, and logs.

The **Runs** page shows a model step like any other, with its detail: the group and its
version, the rows read, the attempts, the exit code, the refusal when the model said no, the
log and, with MLflow, the experiment and the run, with a link to MLflow.

## MLflow

A model node has an optional **MLflow** block: the **connection** (a type [MLflow
connection](../connections/mlflow.md) you may use), the **Experiment** (required with a
connection) and **Register the model as** (optional). Tabularia does not run MLflow. To try a
tracking server on a Kubernetes cluster, see [the kind guide, step 14](../deploy/kind.md#14-optional-the-statistical-models-and-a-tracking-server-for-them-mlflow).

**What a flow run records.** After the step has its results, and before its files leave the
scripts bucket, the gateway:

1. finds the experiment by name and creates it when missing (tag `tabularia.created = true`;
   one in MLflow's trash gets a sentence of its own);
2. opens a run named after the flow, the node and the date, with
   - **parameters**: the node's, flattened, and the model's formula; the distribution or the
     purchase model the fit used (`distribution_chosen`, `purchase_model_chosen`: the one *auto* chose, or the
     one given), beside the node's own; for customer lifetime also `horizon` when the node left it empty;
   - **metrics**: every number of the `model` section, and for each assumption its statistic
     and p-value, as `assumption.<check>[.<level_2>].<statistic>`; for the customer lifetime models also
     each check's `value` (the holdout's error, the share gap of the frequency fit); a parametric survival
     measure's value is not a metric, it is in `tabularia/results.parquet`. The candidates of
     an automatic choice are metrics of their own, `candidate.<name>.<number>` (`aic`, `bic`,
     `log_lik`, `akaike_weight`, `chosen`…, also ARIMA's orders), and the distribution's parameters
     `parameter.<name>[.<stratum>].estimate`. A name MLflow does not take in a key (a column's,
     a predictor's) is cleaned and kept within 250 characters; when two names become the same
     key, the one that already is a key keeps it, the others get `_2`, `_3`…, and the run page
     says under which key each was logged;
   - **tags**: `tabularia.url` (the run's page), the flow's id and name, the node, the model
     and its group, the group's version, the image digest, whether the rows were a development
     sample (`tabularia.dev_sample`), who launched it, the number of rows and
     `mlflow.source.name`. Values are cut at 250 characters, the limit of MLflow's batches;
   - **files**, through the server's artifact proxy: `tabularia/results.parquet`,
     `tabularia/coefficients.csv`, `tabularia/report.pdf` (the Report tab's layout, in the
     flow owner's language), and `model/MLmodel`, `model/model.json` and `model/native.rds` (for boosting, the model file
     instead of `native.rds`; the customer lifetime models write none, their card holds every
     parameter).
     A server that keeps its runs' files elsewhere gets none of them, and the run page says
     so; a PDF that cannot be drawn is a warning, not a failure;
3. ends the run as finished;
4. with **Register the model as**, adds a version of that registered model from the run's
   `model` folder, with the tags `tabularia.model`, `tabularia.group`,
   `tabularia.variant` (the variant fitted: `unpenalized`, `ph_aft`, `auto`, `purchases`…),
   `tabularia.group_version` and `tabularia.card_sha256`: what Predict checks. A parametric survival
   version adds `tabularia.distribution`, the distribution fitted (with *auto*, the one chosen); the customer
   lifetime models add `tabularia.purchase_model`. A registration that fails sets the run failed.

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
never loads it. The boosting models write no R object: they write the library's own model file
(`model/xgboost.json` or `model/catboost.cbm`, see [Boosting](#boosting-python-xgboost-and-catboost)),
with its size and sha256 in the card, and never a pickle. It holds **no row of the input**: before it is written the runner removes the
model frame, the design and the outcome, every value that has one number per row (residuals,
fitted values, linear predictors, weights, a Cox model's stratum of each row, the
cross-validation's out-of-fold predictions and folds), the QR decomposition of the design
(only its R, the first rows, stays) and the environment of the formula. What stays gives the
same predictions as the fit: `predict(fit, newdata)` for a linear or logistic regression
(with `se.fit` when you pass the residual scale, which the report gives), the linear
predictor and the risk of a Cox model (`reference = "sample"`), and `predict(fit, newx, s =)`
for a penalized fit. What needs the rows does not work on it (`summary()`, `residuals()`, a
Cox model's `survfit()`): the report and `coefficients.csv` have those numbers. A parametric survival model's `flexsurv`
object is kept without the rows too. Two objects
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
registered model that carries the tag `tabularia.model` and whose MLflow run is `FINISHED`, in
any experiment (the experiment filters the lists only). A version registered by something other
than Tabularia is not offered and is refused.

A model step closes its MLflow run `FINISHED` before it registers the version, and sets it
`FAILED` when the registration fails. When the server made the version but its answer was lost
(a timeout, a proxy's 502 or 504), the step fails, deletes the version it finds made from its run,
and, should that deletion fail too, the version's run stays `FAILED`: **latest** skips it, and a
fixed version whose run is not `FINISHED` is refused with a sentence that says so.

> **Pin a version in production.** With **latest**, whoever has CONNECT on the MLflow
> connection can register a new version of the same model name, with the tags Tabularia reads
> (`tabularia.model` and the others), and the flows that use **latest** predict with it at
> their next run: the predictions change without anyone editing or approving the flow. The
> card's fingerprint only says the card was not changed after registration, not who registered
> it. For the flows that run in production choose a **fixed version**, and give CONNECT on the
> connection only to those who may change what those flows predict.

**What it checks before reading a row,** and a step that fails any of it stops before any Job
exists:

- the version carries Tabularia's tags, its MLflow run is `FINISHED`, and its `MLmodel` has the
  `tabularia` flavor;
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
nothing is run from the server, and no serialized object that runs code is ever loaded: the
card's numbers, and for boosting the library's model file (JSON or CatBoost's binary format) after
its sha256 matched the card's. Predicting the rows a model was fitted on gives its fitted values to within 1e-9,
which each group's self-test checks.

**Columns added**, always `prediction`, and with options for the model:

| Model | `prediction` | Options |
|---|---|---|
| Linear | The fitted value | **Prediction interval**: `prediction_low`, `prediction_high` (not for a penalized model, which warns and adds none) |
| Logistic | The probability of the event | **Class threshold**: `prediction_class` (0.5 when empty, or the card's) |
| Kaplan–Meier | The survival at the row's time, in the row's stratum (empty past the stratum's last time) | **Prediction interval**: `prediction_low`, `prediction_high` |
| Cox | The risk score, relative to the means of the fit | **Survival at times**: `survival_at_<t>` for each time |
| Parametric survival | The **median time** | **Survival at times**: `survival_at_<t>`; **Quantiles**: `quantile_<p>`; **Prediction interval**: `prediction_low`, `prediction_high`, and `_low`, `_high` for each quantile and time; **Time already survived**: a column of the rows, and then every answer is the time left |
| ARIMA | The forecast, with the fitted coefficients, no re-estimation | **Forecast steps**; **Prediction interval**: `lower_80`, `upper_80`, `lower`, `upper` |
| Customer lifetime value, BG/BB | The expected purchases over the horizon | **Periods ahead** (the card's when empty, in the fit's unit); also `prediction_p_alive`, and with a spend `prediction_spend` and `prediction_clv` |
| XGBoost, CatBoost | A regression's value, a binary model's probability of the event, a multiclass model's likeliest class | Binary: **Class threshold** gives `prediction_class`; multiclass adds `prediction_probability` and `prediction_prob_<class>` |

A parametric survival model predicts **beyond the last time observed** (the shape of the distribution
gives it), unlike Kaplan–Meier and Cox, and the run page counts the rows that go beyond the last time observed in their stratum as an
extrapolation; a median the distribution never reaches is empty. Without a covariance in the card (more than 200
parameters, or a fit whose Hessian was not positive definite) no interval is added, and a warning says why. A row
whose time already survived is beyond what the model allows gets an empty prediction, counted. A quantile of 0.5 is
the median, `prediction`, and adds no column. **Time already survived** is refused
for the other models, and a row with no value, a negative one or an infinite one gets an empty
prediction. For the customer lifetime models a row that breaks a rule of the summary (a recency above
the age, a repeated purchase at the first moment) gets empty predictions and a warning that counts them.
An option that does not belong to the model of the card is refused. A level of a category the
model did not see gives an empty prediction on its rows, and the run page counts them. A
Predict step logs nothing to MLflow: its run records the registered model, the version it
used, the card's hash and the group's versions. **Run** in its panel works like a model's,
and needs its connection and version chosen first.

## Limits, and what it refuses

| Limit | Value |
|---|---|
| Input rows | The group's: 2,000,000, 1,000,000, 20,000, 1,000,000 or 1,000,000 unless an administrator changed it. Beyond it the step fails before the Job, with «the model would read 3,400,000 rows; the group allows 2,000,000: filter or sample before the node» |
| Terms of the design, after encoding categories | 200 |
| Levels of a categorical predictor | 50 |
| Strata (Kaplan–Meier, Cox, parametric survival) | 20 |
| Interactions | 20 pairs |
| Times of «survival at» | 10 |
| Quantiles (parametric survival) | 9 |
| AIC margin of the automatic choice | 0 to 10 |
| Customers and repeat customers (customer lifetime) | At least 20 and 5 (5 with a spend); 1,000,000 customers unless an administrator changed it |
| Periods ahead; opportunities of BG/BB | 1,000 each; above 520 steps the CLV is summed in coarser steps, with a note |
| Seasonal period | 60 |
| Forecast steps | 1,000 |
| Rows | More than terms plus one; with a penalty, 3 per fold, at least 2 terms, and with the AUC measure 10 per fold; at least one event; for ARIMA enough observations for its orders and season |
| Run time | The group's longest run (30 minutes by default, 60 for boosting); the node's timeout, 900 seconds unless you change it, must be within it |
| Rows of the results table | 100,000: the charts share what the other sections leave; a fit still above it is refused, «fewer strata, predictors or levels» |
| Model card | 1 MB: the covariance (above 200 terms) and the baseline hazard (above 5,000 times) are dropped first, then the card is refused |
| Model file (boosting) | 64 MiB: the boosting stops before it (a note); a file still larger is not logged to MLflow, with a note |

Every refusal is a sentence that says what to do. A model started on an installation without
Kubernetes, or without the scripting machinery or the scripts bucket, says so before anything.
The start of a Job takes seconds when the image is on the node; the first run on a node pulls
it, and an R image with its packages is more than a gigabyte.

**What it does not do.**

- **No model beyond the ten**: no multinomial or ordinal logistic, mixed models, additive
  models, Poisson or negative binomial, quantile regression, competing risks, exponential
  smoothing; no inference after a penalized selection. In parametric survival no left or
  interval censoring, no cure models or splines, no robust errors for rows of one subject (rows
  with a start are taken as independent), no gamma or generalized F distribution. The customer
  lifetime models take one row per customer and no column about the customer (no covariates), not
  a log of transactions, and a customer with no purchase yet has no row.
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
lifelines for Kaplan–Meier, Cox and four of the parametric distributions; BTYD, BTYDplus and
hypergeo for the customer lifetime models, whose published estimates for two textbook data sets
are also reproduced). The self-test of each group repeats the comparison, on
the cluster, before the group can be used. A few conventions are Tabularia's own and the
report says so:

- the VIF of a categorical term is the generalized VIF to the power 1/df, comparable with the
  usual limits of 5 and 10;
- the Cook's distance verdict: over 1 failed, over 4/n doubtful; leverage over 2p/n is doubtful;
- Hosmer–Lemeshow's degrees of freedom are the groups made minus 2;
- Cox's risk score is relative to the means of the fit, and its survival curve is for a row at
  the means, not for each stratum's;
- parametric survival fits with `flexsurv` but takes the covariance from its own Hessian of the
  log-likelihood, built from each row's derivatives in the distribution's few parameters (a Richardson
  Hessian only where those fail), for every distribution, because flexsurv's is a finite difference
  for the distributions with no analytic derivatives; the exponential's coefficients are reported
  as time ratios, the opposite sign of its rate's, so that every distribution reads the same way;
  a time of 0 is left out, since no distribution gives it a density;
- customer lifetime: Pareto/NBD, BG/NBD and Gamma–Gamma are computed with CLVTools' compiled likelihoods;
  MBG/NBD and BG/BB, which CLVTools does not have, are written in the runner and agree with
  independent implementations to better than 1e-13 on likelihoods, P(alive) and expected purchases. Where
  the Gauss hypergeometric function fails in the GNU Scientific Library the runner uses a
  quadrature, and says how many customers needed it; the CLV is a discrete sum over the horizon
  you choose, not CLVTools' infinite-horizon expectation.

## Related

- [Statistics](statistics.md): the hypothesis tests, and the output table the models share
- [Flows](flows.md#models): the nodes and the editor
- [MLflow connections](../connections/mlflow.md): the server and the test
- [Python and R](../scripting.md): the scripting machinery the groups are built with
- [Kubernetes](../deploy/kubernetes.md#statistical-models): what the chart needs
- [Engine differences](../engines/engine-differences.md#statistical-models): why a model is not an operation
- [Export to dbt](../dbt-export.md#limits)
