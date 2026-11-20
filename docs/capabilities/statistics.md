# Statistics: hypothesis tests in a flow

A question such as «do the two channels sell the same on average?», «is the share of
cancelled orders above 2%?» or «which of the five plants differ?» is asked without leaving
the canvas. Drop the node of the test you want after any step, choose its columns and
options in the panel, and read the answer in the same place as any other step: a **table of
results** that the next nodes can use, and a **Report**, a few pages drawn for that test,
with its charts and every number needed to redo the computation by hand, that you download
as PDF or Excel.

There are 21 tests in six families, one node each. The numbers are the same on every
engine, so a flow designed on Polars gives the same results in production on ClickHouse.

## At a glance

| | |
|---|---|
| Where | The flow editor's palette, group **Statistics** (between the transformations and the control nodes). It starts closed; the search box looks inside it. |
| What a test node does | Reads the rows that reach it, computes one test, and outputs a table of results (ten columns, below) |
| What you see | The **Table** tab with the results and a **Report** tab with the pages of the test, under the canvas |
| Downloads | The report as **PDF** or **Excel** from the Report tab; the table as CSV like any step |
| Engines | Polars, DuckDB, chDB, ClickHouse (external) and BigQuery, with the same numbers (see [Engine differences](../engines/engine-differences.md#statistics)) |
| Permissions | The same as any step: whoever may read the data a step reads may see its table and its report |

## Where the tests are

Open **Statistics** in the palette (its title is a button; the choice stays open or closed
on your browser). Inside, the tests are grouped by family. While the search box has text the
group opens by itself and shows what matches, looking in names, hints and families. Drag a
test onto the canvas as you would a transformation: dropped on a node it becomes its next
step.

A statistics node has one input and one output. Its form is made of the keys that test has
and nothing else: a column picker (numeric columns only where a number is needed), the
groups to compare filled with the column's values, the alternative hypothesis, the
significance level. An option another option forbids is greyed out with the reason (Tukey's
test with unequal variances, say).

| Family | Tests |
|---|---|
| **Means** | `stat_t_one_sample`, `stat_t_two_sample`, `stat_t_paired`, `stat_anova` |
| **Variances** | `stat_var_f`, `stat_levene` |
| **Proportions and categories** | `stat_prop_one_sample`, `stat_prop_two_sample`, `stat_chi2_independence`, `stat_chi2_gof`, `stat_fisher_exact`, `stat_mcnemar`, `stat_cmh`, `stat_g_test` |
| **Correlation** | `stat_pearson`, `stat_spearman` |
| **Ranks** | `stat_mann_whitney`, `stat_wilcoxon_signed_rank`, `stat_kruskal_wallis` |
| **Distributions** | `stat_ks_two_sample`, `stat_normality` |

Common to every test:

- **`alpha`**, the significance level, 0.05 by default. The confidence intervals have level
  1 − alpha, and the decision is «reject H0» when the p-value is below it.
- **`alternative`**, on the tests that have a direction: two-sided (≠), less (<) or greater
  (>). Tests whose statistic only grows with the departure from H0 (ANOVA, Levene,
  Kruskal–Wallis, chi-square, normality) have none.
- **Which rows count.** A row with an empty or `NaN` value in a column the test uses is left
  out of that test; an empty group is no group. Nothing is filled in or guessed. The number
  of rows left out is in the results (`n_missing`).
- A column with infinite values stops the test with a sentence that names the column and
  the group: no test can be computed on them.

## The tests, with an example each

The examples use the sample databases of the Docker Compose stack (a pasta and bakery
company), imported into **Sample / Private tables** ([Flows](flows.md#example-revenue-by-region-and-channel-on-the-sample-data)
tells how to get at them). `crm_activities` has the columns `tipo` (visita, telefonata,
email, sollecito, reclamo), `esito` (positivo, neutro, negativo, ordine) and `durata_min`;
`orders` has `canale`, `stato` and `deposito`; `order_rows` has `quantita`, `prezzo_unitario`
and `sconto_pct`, stored as text: put a **Change type** to *decimal* before a test that needs
a number.

### Means

**`stat_t_two_sample`**: do two groups have the same mean? Options: the two groups to
compare (needed when the group column has more than two), equal variances (off by default:
Welch's test; on: Student's), the difference under H0 (`mu`, 0 by default), alternative,
alpha. Welch's test is the safer default: it does not assume that the two groups spread
alike. With Student's version, a note says so when one variance is more than four times the
other.
*Example:* source `crm_activities`, **Two-sample t test**, value column `durata_min`, group
column `tipo`, groups `visita` and `telefonata`: do visits last as long as phone calls? The
results hold the difference of the means (first minus second), its interval, `t`, the
degrees of freedom, the p-value and the effect sizes `cohen_d` and `hedges_g`.

**`stat_t_one_sample`**: does a column have a given mean? Options: `mu` (the mean H0 states),
alternative, alpha. *Example:* source `order_rows` after the change of type, value column
`sconto_pct`, `mu` 5: is the average discount 5%?

**`stat_t_paired`**: are two measures on the same rows different on average? Options: `mu`,
alternative, alpha; two numeric columns, `x` and `y`, and the test is on x − y. A row where
either has no number is left out. The report draws the differences. *Example:* the price
applied on an order line against the list price in force, after a Join that puts both on the
same row.

**`stat_anova`**: do three or more groups have the same mean? Options: equal variances (on:
the classic F test; off: Welch's, which does not assume equal variances), **pairwise
comparisons** and the correction for them ([below](#which-groups-differ-the-post-hoc-comparisons)).
The results hold the sums of squares, the F statistic, `eta_squared` and `omega_squared`.
The classic test takes a group with a single value, as R does (Welch's needs two in each).
*Example:* `crm_activities`, value `durata_min`, group `tipo`, pairwise comparisons
**Tukey's HSD**: which kinds of contact last longer than the others?

### Variances

**`stat_var_f`**: is the variance of one group a given multiple of another's? Options: the two
groups, `ratio` (1 for «equal variances»), alternative, alpha. It assumes both groups are
roughly normal; it reacts to a heavy tail more than to a real difference. *Example:*
`durata_min` for `visita` against `telefonata`.

**`stat_levene`**: are the variances of several groups equal? Options: centre of each group,
the median (default, the Brown–Forsythe form, robust to skewed values) or the mean. It is
what to run before an ANOVA when you do not know whether the groups spread alike.
*Example:* `durata_min` by `tipo`.

### Proportions and categories

**`stat_prop_one_sample`**: is a proportion equal to a given value? The column and the value
that counts as a **success** (compared as the Filter node's «equals»), `p0` (0.5 by default),
method (`score`, the default, with a continuity correction on by default, or `exact`, the
binomial test with the Clopper–Pearson interval), alternative, alpha. *Example:* source
`orders`, column `stato`, success `annullato`, `p0` 0.02, alternative greater: are cancelled
orders above 2%? If no row holds the success value the test still runs, with 0 successes, and
a note asks whether it is the right value.

**`stat_prop_two_sample`**: do two groups have the same proportion? The column and its
success value, the group column and the two groups, continuity correction, alternative,
alpha. The results hold the difference of the proportions with its interval and `cohen_h`.
*Example:* `orders`, `stato` = `annullato`, groups by `canale`: `Online` against `Negozi`.

**`stat_chi2_independence`**: are two categorical columns independent? Two columns, `x` (the
rows of the table) and `y` (its columns), continuity correction (used for 2 × 2 tables
only), and the post-hoc: **cells** or **pairs**. The results hold the observed and expected
counts and the residuals of every cell, `cramers_v` (and `phi` for 2 × 2). A note appears
when more than 20% of the expected counts are below 5 or one is below 1: the test is then
less reliable, and Fisher's exact test (2 × 2) or merging rare categories is the way out.
*Example:* `orders`, `x` = `canale`, `y` = `stato`: does the cancellation rate depend on the
channel? With `cells`, which combinations make the association.

**`stat_chi2_gof`**: do the categories of a column have the proportions you expect? The
column, and **expected**: left empty it means all categories equally likely, or a proportion
for every category (they must sum to 1). *Example:* `orders`, column `canale`, expected empty:
are orders spread evenly across channels?

**`stat_fisher_exact`**: the exact test of a table of counts, for small counts. On a 2 × 2
table (two columns with two values each) it has an alternative, and the results hold the
odds ratio and its interval, and the odds ratio of the sample. On a larger table it is the
Freeman–Halton test, as R's `fisher.test`: the two-sided p-value only (a one-sided
alternative is refused), computed exactly; past four million steps of the count (some four
seconds: a 3 × 3 table of a few hundred rows, a 6 × 6 one of a hundred) it is refused and the
chi-square test is the way out.
*Example:* `orders` filtered to `canale` in (`Online`, `Negozi`) and `stato` in (`evaso`,
`annullato`), `x` = `canale`, `y` = `stato`.

**`stat_mcnemar`**: two answers of the same rows (before and after): did they change one way
more than the other? Two columns, `x` and `y`, with the same categories; method (automatic:
the exact binomial below 25 discordant pairs, the chi-square above), continuity correction.
On a 2 × 2 table the results hold the discordant pairs `b` and `c` and their odds ratio b / c
with an exact interval; on a larger table it is Bowker's test of symmetry. The table of the
report marks each cell concordant or discordant. *Example:* the `esito` of a customer's first
and second contact, after a pivot that puts both on the same row.

**`stat_cmh`**: are two categorical columns independent within each stratum of a third (the
Cochran–Mantel–Haenszel test, as R's `mantelhaen.test`)? `x`, `y`, the **strata** column,
continuity correction. With 2 × 2 tables it gives the common odds ratio of Mantel and
Haenszel and its interval; with larger tables the generalized statistic. *Example:*
`orders`, `x` = `canale`, `y` = `stato`, strata `deposito`: does the channel matter once the
warehouse is accounted for?

**`stat_g_test`**: the G test of independence (likelihood ratio), an alternative to the
chi-square: `x`, `y`, Williams' correction (off by default), the post-hoc of the **cells**.

### Correlation

**`stat_pearson`**: is there a linear relation between two numeric columns? `x`, `y`,
alternative, alpha. The results hold r, `r_squared`, the t statistic and the interval of r.
*Example:* `order_rows`, `x` = `quantita`, `y` = `sconto_pct`: do larger lines get larger
discounts? It needs at least 3 rows and a column that is not constant.

**`stat_spearman`**: the same on ranks, for relations that are monotonic but not linear, or
data with outliers. The p-value uses the t approximation, with ties handled by average
ranks; there is no interval for ρ.

### Ranks

The tests that do not assume a normal distribution: they compare the order of the values.
Prefer them with small samples, skewed data or outliers.

**`stat_mann_whitney`**: do two groups tend to have different values? Options: the two groups,
method (automatic: exact for samples of up to 49 rows each without ties, a normal
approximation otherwise; or forced), continuity correction, alternative, alpha. The results
hold each group's rank sum and mean rank, U of the first group (`statistic`, R's W) and of the
second (`u_2`), the normal score `z` of the approximation (empty for the exact test), and the
effect sizes `cles` (the probability that a row of the first group is above one of the second)
and `rank_biserial`. *Example:* `durata_min`, `visita` against `telefonata`, the rank-based
answer to the t test above.

**`stat_wilcoxon_signed_rank`**: are the differences between two paired columns (or between
one column and `mu`) symmetric about 0? `x`, optionally `y`, `mu`, how to treat zero
differences (`wilcox`: leave them out; `pratt`: rank them, then leave them out of the sums),
method (exact for up to 49 differences without ties or zeros), correction, alternative,
alpha. The statistic is V, the sum of the ranks of the positive differences; the results hold
W (`w_minus`), the zeros (`n_zero`) and `rank_biserial`. Without `y` it is the one-sample test
of `x` against `mu`. The paired counterpart of the Mann–Whitney test.

**`stat_kruskal_wallis`**: do three or more groups have the same distribution? Value, group,
and the post-hoc **Dunn** with its correction. The results hold the rank sums, the `H`
statistic and `epsilon_squared`. *Example:* `durata_min` by `tipo`, Dunn with Holm.

### Distributions

**`stat_ks_two_sample`**: do two groups come from the same distribution? Kolmogorov–Smirnov
compares the whole shape, not only the centre. Value, group and the two groups, method
(exact when the product of the two sizes is at most 9,999 and no value is repeated,
Kolmogorov's limit otherwise), alternative, alpha. The statistic is the largest distance
between the two cumulative distributions, D (two-sided), D+ (`greater`: the first group's
distribution above the second's) or D− (`less`). The groups must be named by their values in
a query: a date-time or decimal grouping column is cast to text first.

**`stat_normality`**: do the values of a column look normally distributed? Method:
D'Agostino's K² (default, from the skewness and the kurtosis; needs at least 20 rows) or
Jarque–Bera. A small p-value says the values are not normal. Use it to choose between a t
test and a rank test, keeping in mind that with very many rows it flags tiny departures that
do not matter.

## Which groups differ: the post-hoc comparisons

A test over several groups answers «is there a difference somewhere?». The post-hoc answers
«between which groups?», comparing them two by two. It is an option of the test that needs it:

| Test | Option | Methods |
|---|---|---|
| `stat_anova` | **Pairwise comparisons** | none, **Tukey's HSD**, **Games–Howell**, **t test per pair** |
| `stat_kruskal_wallis` | **Pairwise comparisons** | none, **Dunn** |
| `stat_chi2_independence` | **Post-hoc** | none, **cells** (which cells make the association), **pairs** (which categories of `x` differ) |

The comparisons are computed from the numbers the test already has: they cost no extra pass
over the rows. They are computed whatever the test's own p-value, and a note says when that
one is not below alpha, because a post-hoc after a result that is not significant is easy to
over-read. At most **20** groups (190 pairs) can be compared two by two.

**Which method.**

- **Tukey's HSD** when the groups have about the same variance (the classic ANOVA; with
  different sizes it is the Tukey–Kramer version). It controls the error over all pairs
  together and gives simultaneous intervals. It is refused with Welch's ANOVA.
- **Games–Howell** when the variances differ (Welch's ANOVA, equal variances off): the
  counterpart of Tukey that does not pool them. It is refused with the classic ANOVA.
- **t test per pair** with either variance assumption, followed by a correction you choose.
  The most flexible, and the one to use when you compare only some pairs after filtering.
- **Dunn** after Kruskal–Wallis, on the mean ranks of the groups, with a correction.
- After a chi-square, **cells** points at the combinations that depart most from independence
  (the adjusted residuals), **pairs** runs a chi-square on each pair of categories.

**Which correction** (for the methods that take one). Comparing many pairs makes a false
positive likely somewhere; a correction adjusts the p-values for the number of comparisons.

| Correction | Use it when |
|---|---|
| **Holm** (default) | In general. It is never less powerful than Bonferroni and it needs no assumption on the comparisons. |
| **Bonferroni** | You want the simplest, most conservative rule, or confidence intervals that hold together: it gives simultaneous intervals. |
| **Šidák** | Like Bonferroni but slightly less conservative when the comparisons are independent; it too gives simultaneous intervals. |
| **None** | You want each pair's own p-value, accepting that false positives are not controlled. |

Read the **adjusted p-value** (`p_adjusted`) of a pair, not its plain `p_value`: the decision
of a pair is taken on the adjusted one. Tukey's HSD and Games–Howell have their own
adjustment built in, so their pairs have only the adjusted p-value.

## The output

Every test outputs the same **long table of ten columns**, one row per number or text:

| Column | Holds |
|---|---|
| `section` | What the row is about: `test` (the whole test), `group`, `pair` (two groups), `cell` (a cell of a contingency table), and `hist`, `curve` (the points the report's charts draw; present when the report is drawn) |
| `level` | The group, category or column the row is about; empty for the whole test |
| `level_2` | The second group of a pair, or the column category of a cell |
| `stat` | The name of the number or text: `statistic`, `p_value`, `mean`, `observed`… |
| `value` | The number |
| `text` | The text, for the rows that are not numbers: the hypotheses, the formula, the decision, notes |
| `x0`, `x1` | The edges of a histogram interval, or a point of a curve (report rows) |
| `ord` | The row's place, from 0: sort by it to read the table in order |
| `key` | The row's identity, `section\|level\|level_2\|stat` (for example `test\|\|\|p_value`, `group\|web\|\|mean`, `pair\|a\|b\|p_adjusted`), unique |

The `test` section holds, in this order: the texts (`test`, `variant`, `alternative`, `h0`,
`h1`, `formula`, `distribution`, `decision`, `engine`, the post-hoc and its correction,
`note_1`, `note_2`…) and the numbers that apply (`alpha`, `conf_level`, `n`, `n_missing`, the
building blocks of the test, `estimate`, `null_value`, `std_error`, `statistic`, `df`,
`critical_low`, `critical_high`, `p_value`, `ci_low`, `ci_high`, then the effect sizes). The
`group` section has per group its `n`, `mean`, `var`, `sd`, `se`, the interval of the mean,
`min`, `max` (or, for the tests that need them, rank sums and proportions). A number that
does not exist (the open side of a one-sided interval) is empty, never `inf` or `NaN`.

Next nodes read it as any other data. A few ways in:

- **The p-value alone**: **Filter** `section` equals `test`, then `stat` equals `p_value`:
  one row.
- **A wide table of the groups**: **Filter** `section` equals `group`, then **Pivot** with row
  key `level`, columns from `stat`, values `value`.
- **The pairs that differ**: **Filter** `section` equals `pair` and `stat` equals
  `p_adjusted`, then `value` less than 0.05.

No column name is a reserved word of any engine, so an **Execute SQL** node after the test
can name them without quotes.

### Significance codes

Beside every p-value the table has a text row `significance`, in the style of R's
`summary()`: `***` below 0.001, `**` below 0.01, `*` below 0.05, `.` below 0.1, and nothing
above. For a post-hoc the code is on the adjusted p-value of each pair. The code says R's
threshold, not the decision: the **decision** of the test (`reject_h0` or `fail_to_reject_h0`)
is always taken against the alpha of the node, so with alpha 0.01 a result marked `*` does
not reject H0. The report prints the stars beside the p-value, in a **Signif.** column in its
tables and with R's legend under them.

## The Report tab

With a statistics node selected, the data view under the canvas has a third tab, **Report**,
beside **Table** and **Chart**. It draws the pages of a report made for that test:

1. a header with the test, the step and the date;
2. the result: the statistic, the degrees of freedom, the p-value (with its stars) and the
   estimate with its interval, and one sentence, «At α = 0.05 the null hypothesis is
   rejected» or «is not rejected»;
3. the hypotheses, H0 and H1, and the alpha;
4. how it is computed: the formula, and every number of the computation in order, with the
   distribution and the critical values: enough to redo the result by hand;
5. the groups, with their counts, means, variances and intervals;
6. the comparisons of a post-hoc, with the method and the correction, and a chart of the
   differences with their intervals;
7. charts: box plots by group, the means with their intervals, histograms, and the
   distribution of the statistic with the rejection region shaded and the observed value
   marked;
8. the notes (an assumption worth a look, a fallback taken), the rows left out and the
   engine;
9. a footer.

The page is in the language of the interface (English, Italian, German, Spanish or French).

Above the pages: **−**, **+** and **Fit width** change the zoom (kept on your browser), the
page counter says «Page n of m», **Expand** raises the panel to its largest height and back.
**PDF** and **Excel** download the same report: the PDF holds the pages; the Excel file holds
their tables, one sheet each, with the numbers and not the charts. It is not an object in a
folder: there is nothing to save, and it is drawn again whenever you ask.

The report is drawn when you open the tab. If you change the test's options, or anything
before it, while it is open, the pages stay and are marked **Not up to date** until you press
**Update**: it never redraws at each keystroke. A test the engine refuses (three groups for a
two-sample test) shows its sentence instead of pages.

The PDF and the Excel are made from the same rows as the pages: once a report is drawn,
asking for the file or for another language reads no data again. They count among your exports
(there is a cap on how many run at once) and run on the export queue, so the editor stays
free. A report on a very large table can run longer than the workers allow: the step then
says so, and the way out is a development sample or a Filter before the test.

### Which rows

A test, and its report, are computed on **the rows that reach the node**: the rows the **Table**
tab shows are the rows the test reads, and the Report tab says the same. There is no switch
on the node or on the report between a sample and all the rows. When the source upstream
carries a **development sample** (the automatic one included), the rows that arrive are the
sample's, the Table tab's bar shows its chip, and the page and the PDF say so («Rows: a
development sample of the source — the first 100,000 rows»). To test every row, change the
source's sample. In a run the node is computed on the run's rows; the report is drawn in the
editor only.

## Where Tabularia and R differ in the last digits

Every number is checked against R 4.6. Two differences are R's, not Tabularia's:

- **Fisher's odds ratio on a 2 × 2 table.** The conditional estimate and its interval differ
  from R's `fisher.test` by some 1e-4, relatively. R finds them with `uniroot` at its
  default tolerance, about 1e-4; Tabularia finds them to the last digit. Asked for a
  tolerance of 1e-14 (its `uniroot` calls changed to `tol = 1e-14`), R gives exactly
  Tabularia's numbers: on the table of counts 9, 5 (first row) and 7, 12 (second row) the
  estimate is 2.97678052390209 and the 95% interval 0.599265965836366 to 16.647372273873344.
- **The studentized range** (Tukey's HSD, Games–Howell) is computed more precisely than R's
  `qtukey` and `ptukey` with few degrees of freedom: R's `qtukey(0.999, 3, 2)` gives 42.41,
  the right value is 60.418. With the usual degrees of freedom the two agree to many digits.

## What it does not do

- **Only the rows that reach the node.** No sampling on the node, no second table: what
  the step receives is what is tested.
- **At most 20 groups for the post-hoc comparisons** (190 pairs); at most 200 groups in a
  column for the tests over several groups; at most 50 values per variable of a contingency
  table. Beyond a limit the test stops with a sentence that says what to do.
- **No test on infinite values**, and no filling in of empty ones.
- **Exact p-values only for small samples**: Mann–Whitney and Wilcoxon up to 49 rows per
  group, Kolmogorov–Smirnov up to a product of the sample sizes of 9,999, Fisher and the
  exact binomial up to a million possible outcomes. With ties (or zero differences), or
  beyond these sizes, the normal approximation (Kolmogorov's limit for Kolmogorov–Smirnov)
  is used, and a note says so when the exact one was asked. R 4.6 computes some exact
  p-values with ties; Tabularia, as SciPy, does not.
- **Rank tests of two groups of a date-time or decimal column**: such a column must hold the
  two groups only (filter the others out), or be cast to text: the engine cannot name its
  groups by a literal that every engine reads the same.
- **Not included**: one-sample Kolmogorov–Smirnov, Shapiro–Wilk, two-way ANOVA, Dunnett's and
  Scheffé's comparisons, the Benjamini–Hochberg correction, power and sample size, and an
  interval for Spearman's ρ or for the Mann–Whitney shift.
- **No report as a saved object.** The report lives in the editor; it cannot be saved in a
  folder, scheduled, put on a dashboard or sent by email from a flow group. [Saved
  reports](reports.md) and dashboards are unchanged and do not offer the chart kinds the
  statistics reports use.
- **Not exportable to dbt.** A flow with a statistics node is refused by the
  [dbt export](../dbt-export.md#limits) (`op_not_exportable`): the computation ends in code
  of its own that SQL models do not have.

## Related

- [Flows](flows.md#statistics): the nodes and the editor
- [Engine differences](../engines/engine-differences.md#statistics): what is computed where, and the guarantee
- [Preview and analysis](preview-and-analysis.md): the Table and Chart tabs
- [Pixel-perfect reports](reports.md): the report engine that draws these pages
- [Export to dbt](../dbt-export.md)
