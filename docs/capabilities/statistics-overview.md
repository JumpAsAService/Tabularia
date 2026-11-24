# Statistics in Tabularia: the whole picture

Statistics in Tabularia is a part of the flow editor, not a separate tool. A statistical step is
a node like a filter or a join: you drop it after any step, choose its columns and options in the
panel, and read the answer in the same place as any other step, as a **table of results** the next nodes
can use and a **Report** of a few pages. This page is the map: what exists, which page explains it,
and how to choose.

There are two kinds of node, and they differ in one thing that matters when you install:

| | Hypothesis tests | Statistical models |
|---|---|---|
| The question | «Is this difference, or this link, more than chance?» | «How much? How long? What next?» |
| Palette group | **Statistics** | **Models** |
| Nodes | 21 | 16, and **Predict** |
| Where it runs | On the engine of the flow (Polars, DuckDB, chDB, ClickHouse, BigQuery), with the same numbers on all | As a Job on **Kubernetes**, in an image of a group an administrator turned on |
| Needs | Nothing more | Kubernetes with the feature on, and an enabled group |
| Details | [Statistics: hypothesis tests](statistics.md) | [Statistical models](models.md) |

On Docker Compose the tests work and the models are in the palette greyed out, with the reason.

## The hypothesis tests

Twenty-one tests, one node each, in six families. Every test has an **alpha** (the significance level, 0.05
unless you change it) and a decision taken only by comparing the test's p-value with that alpha.

| Family | Nodes | Anchor |
|---|---|---|
| Means | One-sample t test, Two-sample t test, Paired t test, One-way ANOVA (with Tukey's HSD, Games–Howell or a t test per pair) | [The tests](statistics.md#means) |
| Variances | F test of two variances, Levene's test | [The tests](statistics.md#variances) |
| Proportions and categories | Test of one proportion, Test of two proportions, Chi-square test of independence, Chi-square goodness-of-fit test, Fisher's exact test, McNemar's test, Cochran–Mantel–Haenszel test, G test of independence | [The tests](statistics.md#proportions-and-categories) |
| Correlation | Pearson's correlation, Spearman's rank correlation | [The tests](statistics.md#correlation) |
| Ranks | Mann–Whitney U test, Wilcoxon signed-rank test, Kruskal–Wallis test (with Dunn's comparisons) | [The tests](statistics.md#ranks) |
| Distributions | Two-sample Kolmogorov–Smirnov test, Test of normality | [The tests](statistics.md#distributions) |

Each test's section tells what it asks, its options, what it refuses and an example on the sample
data. Which groups differ after an ANOVA, a Kruskal–Wallis or a chi-square is in
[the post-hoc comparisons](statistics.md#which-groups-differ-the-post-hoc-comparisons). The table every test
writes (ten columns, one row per number) is in [The output](statistics.md#the-output), the
pages of the report in [The Report tab](statistics.md#the-report-tab), and what is not covered in
[What it does not do](statistics.md#what-it-does-not-do).

## The statistical models

Models are in groups, each turned on by an administrator in **Administration → Statistical models**
([how](models.md#turning-a-group-on-administrators)). The palette shows the group's subtitle and its nodes.

| Group | Nodes | What they answer | Section |
|---|---|---|---|
| **Regression** | Linear regression, Logistic regression | How a number, or the odds of a yes-or-no, change with other columns; with ridge, lasso or elastic-net penalties | [Linear](models.md#linear-regression), [Logistic](models.md#logistic-regression) |
| **Survival** | Kaplan–Meier, Cox proportional hazards, Parametric survival | How long until an event, with groups compared, hazard ratios, and a named distribution (Weibull, log-normal, Gompertz…) that predicts how long | [Kaplan–Meier](models.md#kaplanmeier), [Cox](models.md#cox-proportional-hazards), [Parametric](models.md#parametric-survival) |
| **Time series** | ARIMA, Decomposition, Volatility (GARCH), Risk simulation | A series forecast from its own past; its trend, seasons and remainder; how large the day's movement is expected to be; the value at risk, the expected shortfall and the chance of passing a level | [ARIMA](models.md#arima), [Decomposition](models.md#decomposition-r), [GARCH](models.md#volatility-garch-r), [Risk simulation](models.md#risk-simulation-r) |
| **Boosting** (Python) | XGBoost, CatBoost | A number, a yes-or-no or a class predicted by gradient-boosted trees, with the importance of each predictor | [Boosting](models.md#boosting-python-xgboost-and-catboost) |
| **Customer lifetime** | Customer lifetime value, BG/BB | For customers who leave without saying so: the purchases to expect, the probability of still being active and the discounted value | [CLV](models.md#customer-lifetime-value-r), [BG/BB](models.md#bgbb-r) |
| **Experiments** | A/B test, A/B test planning | The result of a randomized experiment (the difference, its interval, the check of the split, CUPED, the Bayesian and sequential readings) and the size of the next one | [A/B tests](models.md#ab-tests-r) |
| **Pre-trained forecasting** (Python) | Forecast (Timestral) | A forecast from a model trained in advance, with nothing fitted on your data. Its weights come under a licence of their own, quoted word for word in the section | [Timestral](models.md#pre-trained-forecasting-timestral) |
| **Bayesian models** (Python) | Bayesian model | A model you draw as a graph of data, parameters and distributions, sampled with Stan: the draws of its posterior, its diagnostics and its predictive checks, before and after the data are seen | [Bayesian models](bayesian-models.md) |

**Predict** applies a model you registered in MLflow to new rows. It works for the models that make a model
(linear, logistic, Kaplan–Meier, Cox, parametric survival, ARIMA, GARCH, customer lifetime value, BG/BB, XGBoost and CatBoost);
Decomposition, Risk simulation, the A/B nodes and Forecast (Timestral) make no model and have no Predict; a Bayesian model can be recorded in MLflow, and Predict does not apply it
([Predict](models.md#predict), [MLflow](models.md#mlflow)).

## Choosing

| You ask | Use |
|---|---|
| Do two channels sell the same on average? | Two-sample t test, or Mann–Whitney if the values are far from normal |
| Do five plants differ, and which? | One-way ANOVA with a post-hoc, or Kruskal–Wallis with Dunn |
| Are two categorical columns linked? | Chi-square test of independence, Fisher's exact test on small counts, G test |
| Did the same rows change between before and after? | Paired t test, Wilcoxon signed-rank test, McNemar's test |
| Does an experiment's variant beat its control, and how many units do I need? | A/B test and A/B test planning |
| How does the quantity sold change with the price? | Linear regression |
| Which customers are likely to order? | Logistic regression, or XGBoost and CatBoost to predict rather than explain |
| How long does a customer, or a machine, last? | Kaplan–Meier, Cox, Parametric survival |
| What will next month look like? | ARIMA, or Forecast (Timestral) with nothing to fit |
| What is in a series: trend, season, noise? | Decomposition |
| How much could I lose over the next days? | Volatility (GARCH), then Risk simulation |
| What is a customer worth, and is the customer still active? | Customer lifetime value, BG/BB |

## How to read a report

Every statistical node has a **Report** tab beside **Table** and **Chart**: A4 pages in the language of the
interface, downloaded as **PDF** or **Excel**. It is drawn from the results the run kept; no query reaches your
data to draw it.

- **Verdicts come only from tests.** A check says *ok* or *failed* by comparing a p-value with the **alpha of the
  node**: p at or above alpha is ok, p below it is failed. Tabularia sets no threshold of its own. Where a quantity
  has no test, the report gives it as a **measure**, a number with a sentence that says what it reads, and no colour
  and no advice; a **fact** (the optimizer converged, a category has no event) is a warning, not a test. See [How a
  check speaks](models.md#how-a-check-speaks-verdicts-facts-and-measures).
- **A model's first page is its assumptions**, one row per check, with the statistic, the p-value and the
  advice. A failed check never stops a model: the numbers are still there.
- **The method is explained** in plain words, from templates in five languages filled with the run's numbers, and
  the last page, **How this report was computed**, holds the formulas. No sentence is written by an AI.
- **Big tables reject small departures.** With many rows a test rejects very small gaps; read the measure and the chart
  beside it. With few rows a test rejects nothing, and *ok* means only that no departure was found.
- **The stars are R's** (`***` below 0.001 to `.` below 0.1); the decision of a test is taken against the node's alpha,
  not against the stars ([Significance codes](statistics.md#significance-codes)).

## Good to know

- The same options on the same rows give the same numbers: every random choice uses a `seed`, 1 by default.
- A node takes the rows that reach it and nothing else; rows with an empty value in a column it uses are left out and counted
  (`n_missing`), never filled in.
- A flow with a statistics or a model node cannot be exported to [dbt](../dbt-export.md#limits).
- A report lives in the editor: it is not an object in a folder and cannot be scheduled or put on a dashboard.
- Where Tabularia's numbers differ from R's in the last digits is listed for [the tests](statistics.md#where-tabularia-and-r-differ-in-the-last-digits) and
  [the models](models.md#where-tabularia-and-r-differ).
- Models record their runs in an [MLflow connection](../connections/mlflow.md) when you choose one.

## Related

- [Statistics: hypothesis tests](statistics.md)
- [Statistical models](models.md)
- [Flows](flows.md#statistics): the nodes and the editor
- [Engine differences](../engines/engine-differences.md#statistics): what is computed where
- [Kubernetes](../deploy/kubernetes.md#statistical-models): what the chart needs for the models
- [MLflow connections](../connections/mlflow.md)
