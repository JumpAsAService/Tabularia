# Spec: statistics — hypothesis tests

Status: specified on 2026-11-19 and revised the same day after the owner's answers (the
last step in Python approved; one node per test, with the post-hoc comparisons; the report
inside the flow editor, on the rows that reach the node). Lots 1, 2, 5 and 6 are built
(commits in «Pieces»): where the code found a fact different from what this spec said, the
spec now says what the code does. Lots 3 and 4 are built too, and their sections say what their code does.
The points still open are at the end.

Words used below: **verified** = read in this repository's code, run against the versions
its lock file pins, or read in the vendor's documentation, on 2026-11-19; **assumed** =
not checked, and said so where it matters.

## Objective

A person who prepares data in a flow asks it a statistical question without leaving the
canvas: «do the two channels sell the same on average?», «is the defect rate above 2%?»,
«which of the five plants differ?». They drop the node of the test they want after any
step, choose the columns and the options that test has, and get, **in the flow editor**:

- a **table of results** that the next nodes read as any other data (statistic, degrees of
  freedom, p-value, confidence interval, effect size, the numbers of each group, the
  comparisons between pairs of groups);
- the **Report** of the test: the pages of a pixel-perfect report, drawn by Tabularia's
  report engine with a layout made for that test, shown beside the table in the editor's
  preview, **downloaded as PDF or Excel**. It holds the charts that show the test (box
  plots, histograms, means with their intervals, the distribution of the statistic with
  the critical region) and every number needed to redo the computation by hand (n, means,
  variances, rank sums, degrees of freedom, the formula, alpha, the hypotheses).

Every pass over the rows runs **inside the engine the flow uses** (DuckDB, Polars, chDB, an
external ClickHouse, BigQuery), on tables of any size the engine handles; the same flow
gives **the same numbers on every engine**.

Readers: analysts who know which test they want; people who do not, and need the node to
refuse a wrong setup with a sentence they understand; whoever receives the PDF and must
be able to check it.

## Decisions

| Question | Decision | By |
|---|---|---|
| Where numbers are computed | Counts, means, variances, rank sums, contingency tables, quantiles, histograms, the Kolmogorov–Smirnov distance: **in the engine**. From those aggregates to the statistic, the p-value, the critical values and the intervals: **one scalar module in the worker, the same for every engine** | Owner, 2026-11-19: «ultimi calcoli in python» |
| How many nodes | **One operation per test**, named `stat_` + the test's id (`stat_t_two_sample`, `stat_anova`…), each with its own parameters. No operation that chooses a test | Owner, 2026-11-19: «ogni test ha le sue peculiarità» |
| One list of the tests | `backend/app/engine/stats/catalog.py` is the only place a test is declared; every other list is made from it or checked against it («One catalogue») | Owner, 2026-11-19 |
| Tests of the first release | 18 operations in six families, with the **post-hoc comparisons** of the tests that have them: Tukey HSD, Games–Howell, pairwise t, Dunn, pairs and cells after a chi-square; corrections Holm, Bonferroni, Šidák | Owner asked the post-hoc; the list is this spec's |
| Output | **One long table**, ten columns, the same on every engine: one row per number or text, in sections (`test`, `group`, `pair`, `cell`, and for the report `hist`, `curve`) | Spec |
| Same numbers | Counts and texts identical; every number equal within a relative 1e-9 between any two engines | Spec |
| The report | A **Report** tab in the flow editor's preview, next to Table and Chart: pages drawn by the report engine from a layout **preset per test**, read-only; **PDF and Excel** from there. Not an object in a folder, no trip through the report designer | Owner, 2026-11-19 |
| What a test and its report are computed on | **The rows that reach the node**, nothing else: in the editor the rows of the node's preview (the Table tab), in a run the run's. No switch between a sample and all the rows on the node or on its report: a development sample is a choice of the source | Owner, 2026-11-19: «Solo sulle righe che arrivano» |
| What the page says about those rows | When the chain that reaches the node carries a development sample, the page and the PDF say so, with the mark the preview already has | Spec |
| Where the layouts live | In the engine, beside the catalogue; their words in five languages in one file there | Spec |
| New in the report engine | Three kinds of chart (`boxplot`, `interval`, `density`) and a `filter` on charts. In the first release only the preset layouts use them | Owner keeps them; scope is this spec's |
| A report's dataset that reads a flow's step | **Not in the first release** (it was only needed by a saved report). It comes back with the optional piece «Save as report» | Spec |
| New dependencies | **None.** The scalar module uses Python's standard library only (`math`), the studentized range included | Spec |
| dbt export | Not exportable: refused with the existing message `op_not_exportable` | Spec |
| Release | The next minor version on `main`; never the 1.4 line | Project rule |

## What was verified in the engines (2026-11-19)

Versions: `backend/uv.lock` pins polars 1.41.2, duckdb 1.5.4, chdb 4.2.1 with chdb-core
26.5.0 (which embeds ClickHouse 26.5.1.1: `SELECT version()` in the CI image),
google-cloud-bigquery 3.45.2; the ClickHouse server of Compose, of the kind samples and of
the CI is `clickhouse/clickhouse-server:24.8` (24.8.14.39 when piece 1.1 ran on it).

| Engine | Aggregates and ranks | Hypothesis tests | Distribution functions | How verified |
|---|---|---|---|---|
| DuckDB 1.5.4 | `avg`, `var_samp`, `covar_samp`, `corr`, `quantile_cont` (exact, linear), `rank`, window sums, `skewness`, `kurtosis` | none | none: no `erf`, no CDF; only `gamma`, `lgamma` | `duckdb_functions()` listed on 1.5.4 |
| chDB 4.2.1 / ClickHouse | `avg`, `varSampStable`, `covarSampStable`, `quantileExactInclusive`, window functions | `studentTTest`, `studentTTestOneSample`, `welchTTest`, `mannWhitneyUTest`, `kolmogorovSmirnovTest`, `meanZTest`, `analysisOfVariance`, `rankCorr`, `contingency`, `cramersV`, `proportionsZTest` | none: no CDF or quantile of t, F, chi-square; only `erf`, `erfc`, `lgamma`, `tgamma` | `system.functions` listed on a chDB with a newer core (26.11: the first study did not pin chdb-core); `varSampStable`, `covarSampStable`, `quantileExactInclusive` run on the locked chDB and **on ClickHouse 24.8.14.39** in piece 1.1; `studentTTestOneSample` does not exist in 24.8 (run); `mannWhitneyUTest` uses the normal approximation only (its documentation, v21.1); nothing for signed ranks, Kruskal–Wallis, Levene, Fisher, chi-square p-values, any post-hoc |
| BigQuery | `AVG`, `VAR_SAMP`, `COVAR_SAMP`, `CORR`, `STDDEV_SAMP`, window functions; `PERCENTILE_CONT` **as a window function only**, linear | none | none: no `ERF`, no gamma function, no CDF | The mathematical, statistical-aggregate and navigation function pages of the documentation; piece 1.1's queries are written from them and parsed, not run (no credentials) |
| Polars 1.41.2 | `mean`, `var`, `quantile(…, "linear")`, `rank`, `cum_sum`, `pl.cov`, `pl.corr`, `skew`, `kurtosis` | none | none | `dir(pl.Expr)` on 1.41.2 |

Five facts found while checking and while building piece 1.1, which the design depends on:

1. **ClickHouse's `varSamp` is not numerically stable** (its documentation says so;
   `varSampStable` exists since v1.1). Run on chDB: the sample variance of
   `1e9 + {5.1, 4.9, 6.2, 5.8, 6.0, 5.5, 5.3, 6.7}` is `0` with `varSamp` and
   `0.36410717423676026` with `varSampStable`, which is DuckDB's `var_samp` to the last
   bit. Statistics use the `…Stable` functions on the ClickHouse engines. (The existing
   `group_by` uses `varSamp`: outside this spec, see «Open points».)
2. **A stable variance is still not enough far from zero.** On 1,600 values near 1e9 with
   a spread of 1 every engine's stable variance is 1e-8 from the true one, Polars 2e-8 from
   DuckDB and ClickHouse, and a mean near 1e9 is known to 1e-7, the size of the difference
   a test is about: the 1e-9 between engines fails. Hence «Centred aggregates» below.
3. **ClickHouse takes a constant out of an aggregate**: `avg(x - c)` is rewritten
   `avg(x) - c`, the very sum the centre is there to avoid (measured on 24.8 and on chDB:
   a difference of two means near 1e9 wrong at the sixth digit). The centre is written
   `materialize(c)` there.
4. **ClickHouse resolves an output alias inside the same `SELECT`**: `max(n1) AS n1` next
   to `max(k1 / n1)` is refused («aggregate function inside another aggregate function»).
   No output of a statistics query takes the name of one of its inputs (`SqlStatSource`
   names its outputs `st_…`, never a name the input has, and reads them by position).
5. **No engine can compute a p-value of every test**; only ClickHouse computes some, by its
   own approximations. A uniform result needs one implementation of the last step.

## How it reuses what exists

| Piece | Reused from | New |
|---|---|---|
| An operation with intermediate queries | `pivot` on every engine: it runs aggregate queries while the chain is built (`ctx.scalar`, `ctx.distinct_rows`, a streaming `collect` in Polars) and returns a small table | The same pattern, for aggregates |
| One SQL text for three dialects | `app/engine/profile.py` (`sql_aggregate`, `sql_histogram_bin`, the dialects `duckdb`, `clickhouse`, `bigquery`) | `app/engine/stats/aggregates.py` |
| Group labels as text | `operations.pivot_label` | Nothing |
| Palette, node, preview, counts, cache, export of a step | The registry and the editor as they are | A group in the palette, a form made from the catalogue |
| The place of the report in the editor | The preview panel of `FlowEditor.vue`: its tab list (`viewtabs`: Table, Chart), its height kept in `localStorage`, its resizer by mouse and keyboard | A third tab |
| Pages on screen | `components/report/ReportPages.vue` (the viewer's and the designer's: each page an SVG laid out as the PDF), `useReports().followRender`, `screenOf`, `download` | A thin view around them |
| A report drawn from rows | The report engine as it is (`reports/render.render`), whose datasets may already be «rows given as they are» (`reports/data.py`); its `svg`, `pdf`, `xlsx` formats; the export queue and `/tasks/exports/{id}` | A route that builds the definition from the test |
| A small result remembered | `profile.remember`, `remembered`, `rowcount.count_key` (Valkey, the counts' lifetime) | A key for a test's rows |
| A recent answer kept in the editor | `previewGrid.Recent`, `previewKey` | Nothing |
| Rights on a step's data | `authorizeExport` (`gateway-go/internal/api/proxy.go`), as a step's CSV download | Nothing |
| Knowing that the rows come from a development sample | The mark the editor puts on the sample's operation (`_dev_sample`, `useFlowModel.devSampleOperation`), the one the Table tab's bar shows as its sample chip | Nothing |

## The nodes

### One operation per test

Each test is an operation of its own: `stat_` + the test's id.

```json
{ "type": "stat_t_two_sample",
  "params": { "value": "amount", "group": "channel", "groups": ["web", "shop"],
              "var_equal": false, "mu": 0, "alternative": "two_sided", "alpha": 0.05 } }
```

One input (the left one), one output, as every transformation. A test's parameters are its
own: what one test calls `posthoc` another does not have, and nothing is shared by
convention. Keys that many tests happen to use mean the same wherever they appear:

| Key | Kind | Meaning |
|---|---|---|
| `value` | a column | The measured column (a number), or the outcome column of a proportion or of a goodness of fit (any type) |
| `group` | a column | The column that names the groups (any type) |
| `groups` | two labels | Which two groups to compare; the first minus the second. Needed when `group` has more than two. A label is a text; a number or a boolean is taken as its label |
| `x`, `y` | columns | Two columns of the same rows: a pair, the two variables of a correlation, the row and column variable of a contingency table |
| `alternative` | `two_sided` \| `less` \| `greater` | The alternative hypothesis; only on the tests that have a direction |
| `alpha` | a number in (0, 1) | Significance level, 0.05 by default; the intervals have level 1 − alpha |
| `detail` | `results` \| `report` | `report` adds the rows the report's charts draw; set by the report, not shown in the form |

A key a test does not take is refused, as a key it needs and lacks; a key set to `null` is
a key not given, and takes its default. The message starts with the operation's name
(`stat_t_paired: there is no parameter 'group'`). Messages are English, as every engine
message.

### One catalogue

`backend/app/engine/stats/catalog.py` declares every test once, as plain data: `TESTS[id]`
with what it `needs`, what it `takes`, each key's kind (today `column`, `number_column`,
`two_groups`, `boolean`, `number`, `positive`, `alternative`, `alpha`, `detail`, and the
choices of one test's own option: `anova_posthoc`, `p_adjust`, `levene_center`; a test with a
parameter of its own adds its kind and its reader), the `defaults`, and `applies`: a key that
matters only with some values of another (`p_adjust` only with `posthoc = pairwise_t`), which
the form shows only then and the node ignores otherwise. `operation_name(test)`,
`stat_operations()` and `is_stat_operation(name)` are what the other modules ask. From
piece 1.2 a test also has its `family`, and `KINDS` says for each kind of value how a form
asks for it
(`{"control": "choice", "choices": [...]}`, `{"control": "column", "numeric": true}`,
`{"control": "levels", "of": "group", "count": 2}`…). `FAMILIES` is the ordered list of
the six families. A new test is an entry there, its function in `compute.py`, its layout
and its words: nothing else names it.

How each list that is written by hand today gets the tests:

| List | Today | With statistics |
|---|---|---|
| The engines' four registries (`operations.py`, `duckdb_ops.py`, `chdb_ops.py`, `bigquery_ops.py`) | One decorated function per operation | One loop each over `catalog.stat_operations()` |
| `GET /tasks/operations`, the palette, the node's type selector | The Polars registry | The same: the names arrive by themselves. (Since piece 1.1 `stat_t_two_sample` is in that answer: until piece 1.2 the palette shows it among the transformations, with no icon and its raw name) |
| `clickhouse_engine._SCAN_HEAVY` | A set of names | The set, or `is_stat_operation(name)` |
| The snapshot other languages read | — | `backend/app/engine/stats/catalog.json`, written by `python -m app.engine.stats.catalog`; `backend/tests/test_stats_catalog.py` fails when it is not what `catalog.py` says |
| The copies | — | `frontend/assets/stat-catalog.json`, `gateway-go/internal/statcatalog/catalog.json` (embedded), `gateway/app/services/stat_catalog.json`: written by `.github/scripts/stat_catalog.py`, and `--check` (a workflow of its own, as `config-reference.yml`) fails when one differs from the snapshot by a byte |
| Icons and names (`useOpIcons.ts`) | One entry per operation | No entry per test: `opMeta` reads the test's family from the catalogue; six entries, one per family (icon, colour); the label key is `stat.tests.<id>.name` |
| The form (`OP_SPECS`, `ParamForm.vue`) | One list of fields per operation | `StatTestForm.vue` draws the test's keys from the catalogue, each by its kind's control |
| Defaults, summary, consumed columns (`useFlowModel.ts`, `opSummary.ts`, `previewGrid.ts`) | A `case` per operation | One branch for every `stat_*`: the catalogue's defaults; the summary is the columns it reads («amount ~ channel»); the consumed columns are the keys whose kind is a column |
| The quick menu's allowed operations (`flow_shortcuts.go`, `FlowShortcutOps`) | A literal list | The list plus `statcatalog.Operations()` |
| dbt (`dbtexport/payload.go`) | `foreach` by name | `statcatalog.Is(name)` refused with `op_not_exportable` |
| The assistants (`gateway/app/services/ai_flow_agent.py`) | `OPERATIONS`, a line of `_REFERENCE` and a branch of the check per operation | `OPERATIONS` gains the copy's names; the reference gains one generated line per test (`stat_anova {value, group, var_equal?, posthoc?: none\|tukey\|…}`); one generic check for every `stat_*` (needed keys, known keys, each value by its kind, the combinations the test forbids); the engine stays the judge at the step's preview |
| The five languages | Keys written by hand | Keys still written by hand, but `frontend/tests/stat-tests.test.mjs` asks for `stat.tests.<id>.name` and `.hint`, `stat.families.<id>`, `stat.params.<key>`, `stat.choices.<kind>.<value>` for everything in the catalogue; the report's words are checked the same way in the engine |
| The tests that compare lists (`flow-shortcuts.test.mjs`, `flow_shortcuts_test.go`) | `ENGINE` written by hand | `ENGINE` = the hand list plus the catalogue's names, read from the snapshot |
| The documentation | — | `stat_catalog.py --check` fails when a test's id is not in `docs/capabilities/statistics.md` |

So a nineteenth test touches `catalog.py`, `compute.py`, its layout, its words in the two
text files, the documentation page, and one command that rewrites the snapshot and its
copies.

### In the palette

`OpSidebar.vue` has group titles and a search today, nothing that folds (verified). The
tests get a group **Statistics** between the transformations and the control nodes:

- its title is a button (`aria-expanded`) that opens and closes it; closed until the
  person opens it, remembered in `localStorage` (`tabularia.palette.statistics`);
- inside, the six families as subtitles, each with its tests in the catalogue's order;
- the search looks in the tests' names, hints and family; while it has text the group is
  open and shows what matches.

The type selector of a statistics node offers the other tests, by family; that of any
other node does not list them.

### Rules every test follows

Settled by piece 1.1 where the first draft was silent; they hold for every test.

- **Rows left out.** A row with NULL or NaN in a column the test uses is left out of that
  test. A NULL in `group` is no group. Nothing is filled or guessed.
- **`n_missing`** counts the rows with no group, plus the rows of the groups the test
  compares whose value is NULL or NaN. The rows of a group the test does not compare (a
  third channel, with `groups` given) are neither used nor missing.
- **An infinite value is a value**, and no test can be computed on it: the node stops and
  names the column and the group («the column «amount» holds infinite values in the group
  «web»»). It is counted, and kept out of means and variances, so that the query itself
  never fails (DuckDB stops on a variance of infinities).
- **Labels.** A group's label is `catalog.label_of(value)`: `pivot_label`, a datetime with a
  zone as the same instant in UTC, and −0.0 and 0.0 one label, `0.0` (the ClickHouse
  engines group by a column without its −0.0, `_zero_signless`).
- **What is always written.** `critical_low`, `critical_high`, `ci_low`, `ci_high` are
  rows of every test that has them, NULL on the side a one-sided alternative leaves open;
  a building block is written in every variant that can compute it (`pooled_var` for Welch
  too: Cohen's d needs it).
- **`formula`** holds every formula a reader needs to redo the result, the degrees of
  freedom's included.
- **Notes.** An assumption the numbers themselves put in doubt is a `note_…` row, an
  English sentence that names the way out (Student's t with one variance more than four
  times the other: «… Welch's test (var_equal = false) does not assume them equal.»).
- **`detail = report`** is refused by a test until its report rows exist (`_WITH_REPORT_ROWS`
  in `compute.py`: the tests of lots 1 and 2 have them), with a sentence; never answered with
  the results alone.
- **A two-group test with `groups` given reads those two groups only** (commit 966c6eb2):
  the engine is asked for them (`levels_of`, then A2 and A12 with an equality on each, the
  values typed by `typed_labels`: a text, a whole or a float number, a date, a boolean), and
  for the rows with no group, which `n_missing` counts. The column's groups are listed only
  to say which are there when a named one is missing. Where no literal reads the same on
  every engine (a datetime with a zone, a decimal, a NaN) the column is listed as before, and
  past `MAX_STAT_GROUPS` the refusal says that the two chosen cannot be read alone from it.
  Every two-group test goes through `_two_groups`.

### Limits (constants of `catalog.py`)

| Constant | Value | Applies to |
|---|---|---|
| `MAX_STAT_GROUPS` | 200 | Groups of an ANOVA, Kruskal–Wallis, Levene; categories of a goodness of fit. **Not** a two-group test with `groups` given: it reads those two, however many the column holds |
| `MAX_POSTHOC_GROUPS` | 20 | Groups compared two by two (190 pairs) |
| `MAX_STAT_LEVELS` | 50 | Levels of each variable of a contingency table |
| `EXACT_MAX_N` | 49 | Largest sample for the exact Mann–Whitney and signed-rank distributions |
| `EXACT_KS_PRODUCT` | 9,999 | Largest n₁·n₂ for the exact Kolmogorov–Smirnov distribution |
| `EXACT_SUPPORT` | 1,000,000 | Largest support of an exact binomial or Fisher computation |
| `CURVE_POINTS` | 201 | Points of the reference distribution's curve |
| `MIN_BINS`, `MAX_BINS` | 5, 30 | Intervals of a histogram |

Beyond a limit the node stops with a sentence that says what to do («the column «city» has
more than 200 groups: is it the right column?»).

## The output table

Ten columns, in this order, on every engine (types as a preview names them):

| # | Column | Type | Holds |
|---|---|---|---|
| 1 | `section` | String | `test`, `group`, `pair`, `cell`, `hist`, `curve` |
| 2 | `level` | String, nullable | The group, category or column the row is about; null for the whole test |
| 3 | `level_2` | String, nullable | The second group of a pair; a contingency cell's column category |
| 4 | `stat` | String | The name of the number or text (below) |
| 5 | `value` | Float64, nullable | The number |
| 6 | `text` | String, nullable | The text, for the rows that are not numbers |
| 7 | `x0` | Float64, nullable | A histogram interval's lower edge; a curve point's abscissa |
| 8 | `x1` | Float64, nullable | A histogram interval's upper edge |
| 9 | `ord` | Int64 | The row's place, 0 to N−1: sort by it |
| 10 | `key` | String | Section, level, level_2 and stat joined by a vertical bar, with nothing for a null part; `hist` and `curve` rows add a bar and their index. Unique (examples below) |

Keys, exactly: `test|||p_value`, `group|web||mean`, `pair|a|b|p_adjusted`,
`cell|m|x|observed`, `hist|web||count|3`, `curve|||density|117`.

No name is a reserved word of any engine's SQL (`group`, `order` avoided on purpose): a
`sql` or `compute` node after the test names them without quotes.

A number that is not finite (an unbounded side of a one-sided interval, a statistic that
does not exist) is NULL, never `inf` or `NaN`. A count is a Float64 like the rest (exact
up to 2^53).

Downstream: `filter section = test`, `filter stat = p_value` gives one row; a `pivot` with
`index = level`, `on = stat`, `values = value` gives a wide table of the groups; a filter
on `section = pair` and `stat = p_adjusted` lists the pairs that differ.

### Section `test` (`level` null)

Texts (`text`), always present, in this order: `test` (the id), `variant` (below, per
test), `alternative`, `h0`, `h1`, `formula`, `distribution` (as `t(12.7279)`,
`F(2, 18)`, `chi-square(2)`, `normal`, `exact`), `decision` (`reject_h0` when
`p_value < alpha`, else `fail_to_reject_h0`), `engine` (the engine's id), `posthoc`, `p_adjust` and `posthoc_formula` (the formulas of a
pair, in symbols) when the test compares pairs (`p_adjust` reads `studentized_range` for
Tukey and Games–Howell, which control the family-wise error by themselves), then `note_1`,
`note_2`… (English sentences: an assumption worth a look, a fallback taken). One text comes
among the numbers, right after `p_value`: `significance`.

`h0`, `h1` and `formula` are written in symbols with the names of the columns and groups,
no words of a language: `mean(amount | web) − mean(amount | shop) = 0`,
`t = (m1 − m2 − mu) / √(s1²/n1 + s2²/n2)`. Characters allowed in them (checked against the
`cmap` of the report's font, IBM Plex Sans, with `app/reports/glyphs.py`): ASCII, Greek
letters, `₀ ₁ ₂ ₃`, `² ³`, `√ ≠ ≤ ≥ ± × − ∞ ∑ ≈ ·` and the combining macron. **Not** in
the font, so not used: subscript letters (`ᵢ ⱼ ₙ`), `∈`, `∩`, `∼`.

Numbers (`value`), those that apply to the test, in this order: `alpha`, `conf_level`,
`n` (rows used), `n_missing`, then the test's building blocks (below, per test), `estimate`,
`null_value`, `std_error`, `statistic`, `df` (or `df_1`, `df_2`), `critical_low`,
`critical_high`, `p_value`, `ci_low`, `ci_high`, then the effect sizes by their names, then
for the post-hoc `n_pairs` and `posthoc_critical` or `alpha_adjusted`.

**Significance codes** (commit eacb760d). The row `significance` of the section `test`, right
after `p_value`, holds R's code: `***` below 0.001, `**` below 0.01, `*` below 0.05, `.` below
0.1, an empty text above (`compute.significance`, one function for every test; no row when
there is no p-value). It says R's threshold, not the decision: `decision` keeps to the
node's alpha. It is computed once, after the engine, so it is the same on every engine. In
the section `pair`, the same code for `p_adjusted`, right after it.

Critical values and one-sided results follow one rule, for a statistic T with quantile
function q:

| `alternative` | p-value | `critical_low` | `critical_high` | Interval |
|---|---|---|---|---|
| `two_sided` | 2·P(T ≥ \|t\|) | −q(1 − α/2) | q(1 − α/2) | estimate ± q(1 − α/2)·se |
| `less` | P(T ≤ t) | q(α) | null | `ci_low` null; `ci_high` = estimate + q(1 − α)·se |
| `greater` | P(T ≥ t) | null | q(1 − α) | `ci_low` = estimate − q(1 − α)·se; `ci_high` null |

Tests whose statistic only grows with the departure from H0 (ANOVA, Levene,
Kruskal–Wallis, chi-square, normality) have no `alternative`: p = P(T ≥ t),
`critical_high` = q(1 − α).

### Section `group` (`level` = the group's label, or the column's name for paired data)

Per test, among: `n`, `mean`, `var`, `sd`, `se` (sd/√n), `ci_low`, `ci_high` (the
two-sided interval of the group's mean at 1 − alpha: mean ± q_t(1 − α/2; n − 1)·se),
`min`, `max`, `median`, `rank_sum`, `mean_rank`, `successes`, `proportion`; a Welch ANOVA
adds `weight` (n/s²); Levene's rows are those of the deviations (`center`, `dev_mean`,
`dev_var`, `dev_sd`, `dev_se`, `dev_ci_low`, `dev_ci_high`). With
`detail = report`, for numeric groups also `q1`, `median`, `q3` (quantiles of R's type 7,
the engines' standard for the median).

Labels are `catalog.label_of(value)` (`pivot_label`); groups are written in the order of
`groups` when given, else in the text order of their labels.

### Section `pair` (`level` = the first group, `level_2` = the second)

One block of rows per pair of groups, pairs in the groups' order (a–b, a–c, b–c). Per
method, among: `estimate` (first minus second: of the means, of the mean ranks),
`std_error`, `statistic`, `df`, `critical` (the multiplier of the pair's interval; null where
the method has none), `p_value` (the pair's own, unadjusted; null where the method has
none), `p_adjusted` (the one to read: adjusted for all the pairs), `significance` (R's code
of `p_adjusted`), `ci_low`, `ci_high` (simultaneous at 1 − alpha, where the method has
them), and the text `decision` (`reject_h0` when `p_adjusted < alpha`). The ANOVA's
post-hoc writes these eleven rows for every method, in this order, NULL where a method has
none.

### Section `cell` (`level` = row category, `level_2` = column category, null for a goodness of fit)

`observed`, `expected`, `residual` ((O − E)/√E), `contribution` ((O − E)²/E); for a
contingency table also `adj_residual`, and with the post-hoc on the cells `p_value`,
`p_adjusted`.

### Sections of the report (`detail = report`)

- `hist`: for each numeric group, `stat = count`, `value` = rows, `x0`, `x1` = the interval,
  `text` = `"[x0, x1)"` with the edges to 6 significant digits. Intervals: equal widths
  between the smallest and the largest value of all groups together; their number
  `min(MAX_BINS, max(MIN_BINS, ceil(log2(n_max)) + 1))`, `n_max` the largest group; the last
  interval includes its upper edge; an interval with no rows is written with 0.
- `curve`: the density of the reference distribution at `CURVE_POINTS` equally spaced
  abscissas `x0`: `stat = density`; the same density again, only on the points inside the
  critical region, as `stat = reject_low` and `stat = reject_high`. Range: for t and normal,
  ±max(4, 1.15·\|statistic\|, 1.15·\|critical\|); for F and chi-square, 0 to
  max(q(0.999), 1.15·statistic). An exact test has no curve.

## Computing in the engine

### The aggregates

`app/engine/stats/aggregates.py` defines what a test may ask of the data, and nothing
else reaches the rows. Below, `{T}` is the step's input (the SQL engines: the chain's
SELECT as a derived table, written again where it is used twice; Polars: the LazyFrame),
`F(c)` the column as a double, `OK(c)` «not NULL and not NaN», `X` =
`CASE WHEN OK(v) THEN F(v) END`, `Xf` the same and NULL where the value is infinite, `C`
the centred value `Xf − c` («Centred aggregates»), `VAR` and `COVAR` the sample variance
and covariance. Every query returns at most a row per group, cell or interval.

| # | Aggregate | Returns | Query |
|---|---|---|---|
| A1 | `levels(g)` | per level: rows | `SELECT g AS lv, count(*) AS n_rows FROM {T} GROUP BY g`, at most the limit + 2 rows. A two-group test with `groups` given does not list the column: `levels_of` finds the two |
| A2 | `moments(v, g?)` (built) | per group: rows, n, mean, variance, min, max, and the centre with the mean less it | The centre, then `SELECT g, count(*), count(X), avg(C), VAR(C), min(X), max(X) FROM {T} GROUP BY g` |
| A3 | `pair_moments(x, y)` | rows, n, means, variances, covariance, mean and variance of x − y | The two centres, then one `SELECT` of the same aggregates over `Cx`, `Cy` and `Cx − Cy`, each NULL unless `OK(x) AND OK(y)` |
| A4 | `rank_sums(v, g, levels?)` | per group: n, sum of the average ranks over all groups together, min, max; Σ(t³ − t) over the tied values | Below |
| A5 | `signed_ranks(x, y?, mu, pratt)` | the differences ranked (without the zeros, or with them by Pratt's rule), sum of the ranks of the positive ones, of the negative ones, Σ(t³ − t); rows, pairs, pairs with an infinite value, zeros, the extremes of x, y, x − y | Below |
| A6 | `rank_corr(x, y)` | n, Σc·rx·ry, Σc·rx², Σc·ry² over the centred average ranks, the two Σ(t³ − t), the extremes | Below |
| A7 | `crosstab(x, y)` | per pair of values: rows | `SELECT x AS lx, y AS ly, count(*) AS n_obs FROM {T} GROUP BY x, y`, at most `MAX_STAT_LEVELS`² + 2·`MAX_STAT_LEVELS` + 2 rows |
| A8 | `successes(v, lit, g?)` | per group: rows, n, successes | `SELECT g AS lv, count(*) AS n_rows, sum(CASE WHEN v IS NOT NULL THEN 1 ELSE 0 END) AS n_obs, sum(CASE WHEN v = lit THEN 1 ELSE 0 END) AS k_obs FROM {T} GROUP BY g` |
| A9 | `ks(v, g, [a, b])` | n₁, n₂, D+·n₁n₂ and D−·n₁n₂ as whole numbers, the values held by more than one row, the extremes | Below |
| A10 | `central_moments(v)` | rows, n, finite values, mean, m₂, m₃, m₄, the extremes | The centre, then `SELECT count(*), count(X), count(C), max(m.d), avg(e·e), avg(e·e·e), avg((e·e)·(e·e)), min(X), max(X) FROM (SELECT X, C FROM {T}) AS s CROSS JOIN (SELECT avg(C) AS d FROM {T}) AS m`, e = s.C − m.d: the powers written as the same products on every engine, never `pow` |
| A11 | `abs_dev_moments(v, g, center)` | per group: n, mean and variance of \|v − centre of its group\| | The centre, then `{T}` joined on `g` to its own `SELECT g, CENTER(C) GROUP BY g`, then A2's aggregates over `abs(C − ctr)` (already of the spread's size: not centred again) |
| A12 | `quartiles(v, g?)` | per group: q1, median, q3 | The engine's exact, linearly interpolated quantile |
| A13 | `histogram(v, g?, lo, hi, bins)` | per group and interval: rows | `profile.sql_histogram_bin`, `GROUP BY g, b` |

**The post-hoc comparisons ask nothing more of the engine**: every pair is computed from
the per-group rows of A2 or A4, or from the cells of A7, already read for the test.

### Centred aggregates

Found by piece 1.1, and a rule for every aggregate that sums values, built or to come (A2,
A3, A10, A11): **means, variances, covariances and power sums are taken of the values less
a centre**, a number near them asked of the engine first.

- The centre is `avg(Xf)` over the chain's first `CENTRE_ROWS` (100,000) rows
  (`SELECT avg(…) FROM (SELECT v FROM {T} LIMIT 100000)`): an aggregate of a prefix, which
  an engine stops reading at. No value there, or a mean that overflowed: the centre is 0,
  nothing is centred.
- One centre per column, **the same for every group** of an answer. A group's mean is
  `centre + mean_centred`, and both are handed to the tests (`Moments.centre`,
  `Moments.mean_centred`): **a difference of two means is taken between the centred
  ones**, where no digit is lost to the centre's size. A2's minimum and maximum are of the
  values as they are.
- On ClickHouse the centre is written `materialize(c)`, a column and not a constant
  (fact 3 above); elsewhere a literal, `repr(c)`.
- Why: without it the 1e-9 between engines fails on a column near 1e9 (fact 2). Near zero
  nothing changes, and the engines agree to their last digits.
- Not centred, because nothing is summed: the counts (A1, A7, A8, A13), the ranks (A4, A5,
  A6, A9: a value is a key, and a difference `F(x) − F(y)` of two near values is exact),
  the quantiles (A12).
- A2 is therefore **two queries**, and so is every aggregate above: the centre, then the
  aggregates.

**What the code does (lot 4)**, where the queries below were a first draft: every alias is a
fresh name (ClickHouse resolves an alias inside its own SELECT); the value is `F(x) + 0.0`, so
that −0.0 and 0.0 are one value on every engine; and **no window is partitioned by a
double**, which BigQuery refuses: each distinct value gets a whole number first,
`dense_rank() OVER (ORDER BY xv)`, and the windows are partitioned and ordered by it (A4,
A6). The cumulative counts use the `RANGE` frame where several groups share a value (its
peers count). A4 is one query: the ties' Σ(t³ − t) is summed on each value's first row
(`row_number() = 1`), the groups' sums beside it. A5 is two (the ranks; the counts and
extremes). A9 returns its distances as whole numbers (`max(k1·m2 − k2·m1)`, the counts cast
to signed 64 bits: ClickHouse's are unsigned) and needs no partition at all. Polars does the
same with `group_by`, `sort`, `cum_sum` and joins, every product in the same order.

**Ranks without sorting the rows twice.** A rank is a property of a *value*: the rows are
first counted by value (a parallel hash aggregation), and one ordered window runs over the
distinct values only. With many ties (prices, quantities, scores) the window is small; with
none it is as long as the table, which is what a rank costs. Verified on DuckDB 1.5.4,
chDB (with the newer core, 26.11) and Polars 1.41.2 against R: rank sums 38 and 82 on the data without ties, 47
and 106 with Σ(t³ − t) = 108 on the data with ties («Reference values»).

```sql
-- A4, rank sums. f: rows per group and value; v: rows per value; r: the value's average rank
WITH f AS (SELECT g AS gv, F(x) AS xv, count(*) AS c FROM {T}
           WHERE g IS NOT NULL AND OK(x) [AND g IN (a, b)] GROUP BY g, F(x)),
     v AS (SELECT xv, sum(c) AS t FROM f GROUP BY xv),
     r AS (SELECT xv, sum(t) OVER (ORDER BY xv ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                      - (t - 1) / 2.0 AS rk FROM v)
SELECT f.gv AS lv, sum(f.c) AS n_obs, sum(f.c * r.rk) AS rank_sum FROM f JOIN r ON f.xv = r.xv GROUP BY f.gv;
SELECT sum(t * t * t - t) AS tie_sum FROM v;

-- A5, signed ranks of d = F(x) - F(y) - mu (or F(x) - mu), zeros left out before ranking
WITH d AS (SELECT F(x) - F(y) - mu AS dv FROM {T} WHERE OK(x) AND OK(y)),
     f AS (SELECT abs(dv) AS av, sum(CASE WHEN dv > 0 THEN 1 ELSE 0 END) AS cp, count(*) AS t
           FROM d WHERE dv <> 0 GROUP BY abs(dv)),
     r AS (SELECT av, cp, t, sum(t) OVER (ORDER BY av ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
                             - (t - 1) / 2.0 AS rk FROM f)
SELECT sum(t) AS n_used, sum(cp * rk) AS w_plus, sum((t - cp) * rk) AS w_minus, sum(t * t * t - t) AS tie_sum FROM r;
SELECT count(*) AS n_pairs, sum(CASE WHEN dv = 0 THEN 1 ELSE 0 END) AS n_zero FROM d;

-- A9, the distance between two empirical distribution functions
WITH f AS (SELECT F(x) AS xv, sum(CASE WHEN g = a THEN 1 ELSE 0 END) AS c1,
                              sum(CASE WHEN g = b THEN 1 ELSE 0 END) AS c2
           FROM {T} WHERE g IN (a, b) AND OK(x) GROUP BY F(x)),
     w AS (SELECT sum(c1) OVER (ORDER BY xv ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS k1,
                  sum(c2) OVER (ORDER BY xv ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS k2,
                  sum(c1) OVER () AS m1, sum(c2) OVER () AS m2,
                  CASE WHEN c1 + c2 > 1 THEN 1 ELSE 0 END AS tied FROM f)
SELECT max(m1) AS n_1, max(m2) AS n_2, max(k1 / m1 - k2 / m2) AS d_plus, max(k2 / m2 - k1 / m1) AS d_minus,
       max(abs(k1 / m1 - k2 / m2)) AS d_abs, sum(tied) AS n_tied FROM w;
```

A5 with zeros ranked (Pratt's rule, `zeros = pratt`): `f` keeps the rows with `dv = 0`
(they take the lowest ranks), a third count holds the negative differences, the two sums
run over the positive and the negative only, and `tie_sum` over the values above zero.

A6 is A4 twice: the pairs are counted (`GROUP BY F(x), F(y)`), each of `x` and `y` gets
its table of average ranks as `r` above, centred by `(n + 1) / 2` with `n = sum(t) OVER ()`,
and the pair counts joined to both give the three sums. The CTEs are written as derived
tables on the ClickHouse dialect (`base.py`, `count_inside_sql`: it does not resolve a
`WITH` nested in another).

### Dialects

One builder, `SqlStatSource(dialect, rows, quote, lit, source_sql, schema)`, three
dialects, as `profile.py`; it says which columns are numbers from the engine's own type
names (`columns()`), and `PolarsStatSource(lf)` answers the same methods:

| | `duckdb` | `clickhouse` (chDB and the server) | `bigquery` |
|---|---|---|---|
| Identifier | `duckdb_ops._qi` | `chdb_ops._qi` | `ctx.qi` (the safe name) |
| Literal | `duckdb_ops._lit` | `chdb_ops._lit` | `bigquery_ops._lit` |
| `F(c)` | `CAST(c AS DOUBLE)` | `toFloat64(c)`; `toFloat64(c) + 0.` where a query groups or joins by it (−0.0 and 0.0 are two keys there: `chdb_ops._zero_signless`) | `CAST(c AS FLOAT64)` |
| NaN | `isnan(e)` | `isNaN(e)` | `IS_NAN(e)` |
| `VAR(e)` | `var_samp(e)` | `if(count(e) > 1, varSampStable(e), NULL)` | `VAR_SAMP(e)` |
| `COVAR(a, b)` | `covar_samp(a, b)` | `if(count() > 1, covarSampStable(a, b), NULL)` over rows with both | `COVAR_SAMP(a, b)` |
| Exact quantile | `quantile_cont(e, q)` | `quantileExactInclusive(q)(e)` | `SELECT DISTINCT g, PERCENTILE_CONT(e, q) OVER (PARTITION BY gid)`, gid = `dense_rank() OVER (ORDER BY g)` (GoogleSQL partitions by no FLOAT64; A11's medians too) |
| Finite | `isfinite(e)` | `isFinite(e)` | `NOT IS_NAN(e) AND NOT IS_INF(e)` |
| A value less the centre | `(e - (c))` | `(e - materialize(c))` | `(e - (c))` |
| Rows of a query, values typed | `ctx.con.sql(q).fetchall()` | `ctx.rows(q)`: on chDB read **through Arrow**, as a preview's rows (its JSON gives 2.0 as 2, a NaN as null, a datetime as a text: the labels would differ); on the server the driver's own types | `ctx.rows(q)`, the client's own types |
| The input's columns and types | `rel.columns`, `rel.types` | `ctx.schema_of(sql)` | `ctx.schema_of(sql)` |
| The result table (`output.py`) | `ctx.con.from_arrow(to_arrow(rows))` | `SELECT * FROM values('section String, level Nullable(String), …', (…), …)` | `SELECT * FROM UNNEST([STRUCT<section STRING, …>(…), STRUCT<…>(…)])`: **every row a typed `STRUCT`**, so that nothing is guessed from a NULL |

Polars (`PolarsStatSource`): the same aggregates as lazy expressions —
`group_by(...).agg(pl.len(), pl.col(v).mean(), pl.col(v).var(), …)`,
`pl.col(v).quantile(q, "linear")`, `pl.cov`, the rank tables with `group_by` → `sort` →
`cum_sum` → `join` — each collected with `collect(engine="streaming")` as `pivot` does;
the result is `pl.LazyFrame(rows, schema=…)`.

Rules for every dialect: a count comes back as an integer whatever the engine's type; a
variance of fewer than two values is NULL; no output alias repeats an input's name;
group values come back typed and become labels in Python with `label_of`; a `g IN (…)`
is written with the typed values A1 returned, never with text typed by a person.

### Cost

A moment test is A1 plus **two queries** (the centre, on the first 100,000 rows, and the
aggregates: one pass); Levene one more; a rank test one aggregation by value and one
window over the distinct values; `detail = report` adds two passes (quartiles, histogram);
a post-hoc adds none. Exact quantiles keep the column's values in the engine's
memory on DuckDB and ClickHouse: on a billion rows the report's quartiles are the
expensive part, which is why they are computed only for the report.

## From the aggregates to the results

`app/engine/stats/compute.py` turns the aggregates into the rows of the output:
arithmetic on a handful of numbers, the same code for every engine. A test is a function
`(source, p, out, engine_name)` named in its table `_TESTS`, and
`run_hypothesis_test(source, test, params, engine_name)` checks the parameters, runs it and
returns the rows. `app/engine/stats/dist.py` and `exact.py` hold the distributions.
Standard library only.

### `dist.py` — continuous distributions

The normal, the t and the incomplete beta under it are **built** (piece 1.1) and are what
this table says. The rest was prototyped for this spec, outside the repository, and run
against R 4.6.1 and SciPy 1.18.1: of 46 values, 41 agree to a relative 1e-13 or better,
Fisher's odds ratio and its bounds (4) to 1e-4, and one differs because the reference
stops a series early («Reference values»). The closed formulas of «The tests» (55, and 28
more for the post-hoc) were checked the same way, to 1e-9.

One thing the prototype got wrong and the code got right: the incomplete beta by the
classic continued fraction (Lentz), which the first draft prescribed, does **not** keep
the contract — 6e-12 at 10⁵ degrees of freedom, 5e-10 at 3·10⁶ — because x, rounded at its
sixteenth digit, sits in every term. The draft's remedy for a huge first parameter (an
incomplete gamma in its place) was not built: it would have been worse than what is there.

| Function | Method |
|---|---|
| `norm_sf(z)`, `norm_cdf(z)` (built) | `0.5 · erfc(±z / √2)` |
| `betainc(a, b, x, y)` (built) | The regularized incomplete beta, with `y = 1 − x` **given by the caller**, who knows it exactly (t²/(df + t²)), and never recomputed by subtraction. The front factor in logs, `ln x` read as `log1p(−y)` when x > 0.5. The fraction is **the even part of the classic one (DiDonato and Morris, 1992)**, two of its steps at a time, where the sum that cancels is made on paper and what is left of it, λ = a − (a + b)·x, is read from whichever of x and y is small (`(a + b)·y − b` when x > 0.5); it stops when a step changes it by less than 4.5e-16. The mirrored form `1 − I_y(b, a)` on the other side of the mean, the side chosen on the smaller of x and y |
| `lbeta(a, b)` (built) | `lgamma` sums when both are below 10; otherwise Stirling's remainder Δ (eight terms) and `log1p`, so that no two large numbers are subtracted: for a ≤ b, b ≥ 10, a < 10: `lgamma(a) − [(b − ½)·log1p(a/b) + a·ln(a + b) − a + Δ(a + b) − Δ(b)]`; both ≥ 10: `½ln(2π) − ½ln b + Δ(a) + Δ(b) − Δ(a + b) − (a − ½)·log1p(b/a) − b·log1p(a/b)` |
| `t_sf(t, df)`, `t_cdf` (built) | `½·betainc(df/2, ½, df/(df + t²), t²/(df + t²))`, mirrored for t < 0 |
| `t_isf(q, df)`, `norm_isf(q)`, `t_ppf`, `norm_ppf` (built) | Bisection on the tail until the interval is one double wide. A quantile is **asked by its tail** (`t_isf(α/2)`, not `t_ppf(1 − α/2)`), so that a small alpha loses no digit in `1 − alpha` |
| `gammainc_q(a, x)` (built, lot 2) | Series for `x < a + 1`, Legendre's continued fraction otherwise; the front factor with its large terms cancelled on paper (Stirling's remainder, ln(1 + u) − u written without cancellation), or a billion degrees of freedom would leave five digits. Its iterations grow with √a |
| `f_sf(f, d1, d2)`, `f_cdf` (built, lot 2) | `betainc(d2/2, d1/2, d2/(d2 + d1·f), d1·f/(d2 + d1·f))`, its complement written by the caller, never as 1 − x |
| `chi2_sf(x, df)`, `chi2_cdf` (built, lot 2) | `gammainc_q(df/2, x/2)` |
| `f_isf`, `chi2_isf` (built, lot 2); `beta_ppf` (lot 3) | The same bisection, by the tail; the lower quantile of F is 1 / the upper one of F(d2, d1), so that a small alpha keeps its digits |
| `t_pdf`, `f_pdf`, `chi2_pdf`, `norm_pdf` | Closed forms through `lgamma`, for the curve |
| `kolmogorov_sf`, `kolmogorov_pdf`, `kolmogorov_isf` (lot 4) | Kolmogorov's limit of √(n₁n₂/(n₁ + n₂))·D: for λ ≥ 1 the alternating series 2Σ(−1)^(k−1)e^(−2k²λ²), below 1 the theta form √(2π)/λ·Σe^(−(2k − 1)²π²/(8λ²)), each summed to the end (R's `ks.test` stops at 1e-6). One-sided: e^(−2λ²), 4λe^(−2λ²), √(−ln q/2). Within 1e-14 of mpmath (SciPy's `kstwobign.pdf` is 5e-10 off near λ = 1; its `sf` agrees) |
| `srange_sf(q, k, df)`, `srange_isf(alpha, k, df)` (built, lot 2); `srange_cdf`, `srange_ppf` | The studentized range, below. The upper tail is what is computed and what the post-hoc reads; `srange_cdf` is 1 − it, `srange_ppf(p)` is `srange_isf(1 − p)` |

**Accuracy contract** (`tests/test_stats_dist.py`, proven for the t in piece 1.1): every
p-value above 1e-280 within a relative **1e-12** of its 60-digit value for degrees of
freedom up to 10¹⁰ (measured: 2e-13 at worst, up to 10¹⁶). Why it needs saying: with a
billion rows the degrees of freedom are a billion, and a textbook incomplete beta is wrong
at the eighth digit there. The F and the chi-square of lot 2 are held to the same
contract (built in a10a1aec, measured against 60-digit arithmetic and SciPy 1.18.1: F within
4.6e-13 and the chi-square within 6.1e-13, both tails, up to 10¹⁰ degrees of freedom).

### The studentized range (Tukey, Games–Howell)

No engine has it; it is two nested integrals, computed with the standard library:

P(Q ≤ q; k, df) = ∫ g(s; df) · W_k(q·s) ds, where W_k(w) = k ∫ φ(u)·[Φ(u + w) − Φ(u)]^(k−1) du
is the distribution of the range of k standard normals and g is the density of
S = √(χ²_df / df). **Only the upper tail is computed** (the code of a10a1aec):
P(Q > q) = ∫ g(s)·T_k(q·s) ds with T_k = 1 − W_k, a sum of small positive numbers, so that a
p-value of 1e-30 is a number, smooth in q, the same on two engines whose means differ in the
last digit (1 − the cdf would leave the rounding of the 1 as its only digits).

1. **A table of ln T_k, once per k**: at w = j/32 for 0 ≤ w ≤ 14, each value by
   Gauss–Legendre (8 nodes on each panel of width 0.5 over [−8.5 − ½·⌈w⌉, 8.5], where φ
   lives), the bracket written b^(k−1)·(1 − (1 − r)^(k−1)) with b = Q(u), r = Q(u + w)/b:
   no subtraction of two near numbers. Beyond 14, T_k falls as the tail of the difference of
   two normals, Q(w/√2), joined to the table's last point (it is not 1 − W_k = 0). Between
   the table's points, six-point Lagrange interpolation. Gauss–Legendre nodes are computed
   once (Newton on the Legendre polynomial).
2. **The outer integral**, in t = √s (so that a small df has no singular derivative at 0),
   over [lo, hi] = [max(0, 1 − 9/√df), 1 + 9/√df] for df ≥ 20 and [0, 1 + 12/√df] below,
   cut where q·s passes 40 (beyond it the range's tail is below e^−400: no number).
   Panels: `min(96, max(4, ceil(q·(hi − lo)), ceil((√hi − √lo)·2√(2·df)/3)))`, 16 nodes
   each: the last term keeps a panel within three standard deviations of S's density.
3. **The density in logs without cancellation** for df ≥ 20:
   `ln g = ln 2 − ln s + ½ln(df/4π) − Δ(df/2) + df·(log1p(u) − u − u²/2)`, u = s − 1 (Δ is
   `dist._stirling_remainder`); the plain form with `lgamma` below 20.
4. `srange_isf(alpha, k, df)`: the q with P(Q > q) = alpha, bracketed, then regula falsi
   (the Illinois variant) on ln P(Q > q), to a relative 1e-12 of q. `srange_ppf(p)` asks it
   by its tail, `srange_isf(1 − p)`.

Built in lot 2 (a10a1aec, after `gammainc_q`); the numbers below are those measured on the
code, not the prototype's.

**Accuracy, measured**: on 880 points (k from 2 to 20; df from 1 to 20,000; q from 0.2 to 40)
the largest absolute difference from SciPy 1.18.1's `studentized_range.cdf` is **3e-11**
(`test_stats_dist.py`). Quantiles agree with SciPy to a relative 1e-9 (the tests' bound). Contract: an absolute 2e-10 on the probability for
k ≤ `MAX_POSTHOC_GROUPS`, so an adjusted p-value below 1e-8 is right in size and not in
its digits. **SciPy is not a reliable reference everywhere**: at df = 20,000 it is 8e-4 off
(q = 8, k = 2) and past some 10⁵ it returns the value for infinite degrees of freedom (R
too). For k = 2 the range is √2·|T|, so P(Q > q) = 2·P(T ≥ q/√2) is known exactly from the
t of «dist.py»: the code is held to it within a relative 5e-12 for df from 1 to 10⁹ (1.8e-12
at worst), and keeps the finite df (it differs from the infinite-df value by 4e-8 at
df = 10⁶). R's `ptukey` and `qtukey` are less exact than that as well (its 95% quantile for
k = 3, df = 18 is off by 9e-8), so the references of the post-hoc are SciPy's, on values
where it is right.

**Speed, measured** (Python, one core): the table 0.12 s per k; a probability 1.8 ms; 190
pairs with a quantile each (Games–Howell on 20 groups, the worst case) **1.7 s**. Tukey HSD
needs one quantile for all pairs.

### `exact.py` — small samples

| Function | Method |
|---|---|
| `mann_whitney_exact(u, n1, n2, alternative)` (built, lot 4) | The number of arrangements with each U, by the product Π(1 − q^(n2+i))/(1 − q^i) over i = 1…n1 on an array of Python's whole numbers, each division exact; the p-value one fraction, divided once; two-sided = min(1, 2·min(lower, upper)). Within 1e-13 of R's `pwilcox` up to 49 × 49; 0.03 s there |
| `signed_rank_exact(v, n, alternative)` (built) | The number of subsets of {1…n} with each sum, by a 0/1 knapsack on whole numbers; the same two-sided rule. Within 1e-13 of `psignrank` up to n = 49; 0.006 s there |
| `ks_exact(distance, n1, n2, alternative)` (built) | P(D ≥ d) with `distance` = d·n1·n2 **a whole number from the engine**: the paths from (0, 0) to (n1, n2) that never reach it (\|i·n2 − j·n1\| < distance two-sided, i·n2 − j·n1 < distance for `greater`, the other sign for `less`) counted row by row in whole numbers, P = 1 − their share as one fraction. No `floor(… − 1e-7)` needed: nothing is rounded. Within 1e-13 of R's `psmirnov`, one-sided too; 0.003 s at 99 × 101, 0.007 s at 1 × 9,999 |
| `ks_asymptotic(d, n1, n2, alternative)` (built) | `dist.kolmogorov_sf(d·√(n1·n2/(n1 + n2)))`; one-sided `exp(−2·(n1n2/(n1 + n2))·d²)`, R's order of operations |
| `binomial_test(k, n, p0, alternative)` | Tails by `betainc`; two-sided = the sum of the probabilities not above the observed one times (1 + 1e-7) |
| `clopper_pearson(k, n, level)` | `beta_ppf` of (k, n − k + 1) and (k + 1, n − k) |
| `fisher_exact(a, b, c, d, alternative, level)` | The hypergeometric probabilities of the support in logs (`lgamma`); two-sided = the sum of those not above the observed one times (1 + 1e-7); the odds ratio = the conditional maximum-likelihood estimate (the root of «mean of the non-central hypergeometric = a»), its interval from the non-central tails, both by bisection |

### Adjusting for many comparisons

For m p-values (`compute.py`, `p_adjusted`):

| `p_adjust` | Adjusted p | Simultaneous interval |
|---|---|---|
| `holm` (default) | Sorted ascending p₍₁₎ ≤ … ≤ p₍ₘ₎: the running maximum of min(1, (m − i + 1)·p₍ᵢ₎) | none |
| `bonferroni` | min(1, m·p) | level 1 − α/m per pair |
| `sidak` | 1 − (1 − p)^m, written `−expm1(m·log1p(−p))` | level (1 − α)^(1/m) per pair |
| `none` | p | level 1 − α per pair |

## Deviations from "everything in the engine"

**Approved by the owner on 2026-11-19** («ultimi calcoli in python»). Kept here because it
is the contract.

**1. The last step is computed in the worker, in Python, on aggregates.** From the
numbers the engine returned (at most one row per group, cell or interval: never a row of
the data, never a column of values) one module computes, for every engine:

- the arithmetic that combines them: a difference of means over its standard error, sums
  of squares, expected counts, the tie correction, the comparisons between pairs of groups;
- the p-value, the critical values and the quantiles of the intervals (normal, t, F,
  chi-square, beta, studentized range), and their adjustment for many comparisons;
- the exact distributions of small samples (Mann–Whitney, signed rank,
  Kolmogorov–Smirnov, binomial, Fisher);
- the density curve of the report (201 points of a formula).

Why not in the engine. DuckDB, BigQuery and Polars have no distribution function at all
(verified above); ClickHouse has none either, only whole tests for seven of the eighteen
(the three t tests, the one-way ANOVA, Mann–Whitney, Kolmogorov–Smirnov, two proportions;
on the 24.8 server not even the one-sample t: `studentTTestOneSample` is not there),
with its own approximations (a normal approximation where a small sample has an exact
distribution). Writing an incomplete beta function in four SQL dialects and in Polars
expressions would be five implementations to keep equal to the last digit, for a
computation on three numbers. One implementation is what makes the result **the same on
every engine**, which the cross-engine standard asks first.

Size of what leaves the engine: at most `MAX_STAT_GROUPS` rows of moments,
`MAX_STAT_LEVELS`² cells, `MAX_BINS` intervals per group. The rows of the data never do.

**2. ClickHouse's own test functions are not used to produce results.** On the ClickHouse
engines the means and variances are ClickHouse's (`avg`, `varSampStable`), the p-value is
the shared module's. `studentTTest`, `welchTTest` and `mannWhitneyUTest` are used **in the
tests of this feature only**, as a second witness of the p-values on chDB — and not on a
column far from zero: on values near 1e9 `welchTTest` and `studentTTest` answer
`(nan, 0)` (run on chDB 4.2.1), which is one more reason not to produce results with them.

**3. Quantiles of a group on BigQuery go through a window function**
(`PERCENTILE_CONT … OVER`), because BigQuery has no exact quantile aggregate. In the
engine, but not as an aggregate: the cost is BigQuery's.

Nothing else deviates. In particular no row is read into pandas, numpy or SciPy; none of
them is a dependency of this feature.

## The tests

Notation: n rows used, m mean, s² sample variance, N total, k groups, q_t(p; df) a
quantile. Headings are the operations' names. «Variant» is the text of the `variant` row.
Every test fails with a sentence when it has no usable row. Every test takes `alpha` and
`detail`; `alternative` where said.

### Means

**`stat_t_one_sample`** — `value`, `mu` (default 0), `alternative`. H0: mean = mu. A2.
se = s/√n; t = (m − mu)/se; df = n − 1; estimate = m; interval of the mean; effect
`cohen_d` = (m − mu)/s. Blocks: `mean`, `var`, `sd`. Refused: n < 2; s² = 0 («all the
values are equal»). Variant `one_sample`.

**`stat_t_two_sample`** (built) — `value`, `group`, `groups?`, `var_equal` (default
false), `mu`, `alternative`. H0: mean₁ − mean₂ = mu. A1, A2. estimate = m₁ − m₂, taken
between the centred means.
- `pooled_var` = ((n₁ − 1)s₁² + (n₂ − 1)s₂²)/(n₁ + n₂ − 2), written in both variants.
- Welch (`var_equal` false, variant `welch`): se = √(s₁²/n₁ + s₂²/n₂);
  df = (s₁²/n₁ + s₂²/n₂)² / ((s₁²/n₁)²/(n₁ − 1) + (s₂²/n₂)²/(n₂ − 1)).
- Student (variant `student`): se = √(pooled_var·(1/n₁ + 1/n₂)); df = n₁ + n₂ − 2. A note
  when one variance is more than four times the other, or one is 0.
- t = (estimate − mu)/se. Effects, both variants: `cohen_d` = (m₁ − m₂)/√pooled_var,
  `hedges_g` = cohen_d·(1 − 3/(4(n₁ + n₂ − 2) − 1)).
- Rows: the texts; `alpha`, `conf_level`, `n`, `n_missing`, `pooled_var`, `estimate`,
  `null_value`, `std_error`, `statistic`, `df`, `critical_low`, `critical_high`, `p_value`,
  `ci_low`, `ci_high`, `cohen_d`, `hedges_g`; per group `n`, `mean`, `var`, `sd`, `se`,
  `ci_low`, `ci_high`, `min`, `max`.
- Refused, each with its sentence: a column of `group` with no group, with one, or with
  more than two and no `groups` (the groups it has are listed); a group of `groups` that
  is not there; a group with fewer than 2 usable values; infinite values; both variances 0;
  more than `MAX_STAT_GROUPS` groups, unless `groups` is given (see «Rules every test
  follows»).

**`stat_t_paired`** — `x`, `y`, `mu`, `alternative`. H0: mean(x − y) = mu. A3. The
one-sample t on the differences: blocks `mean_diff`, `var_diff`, `sd_diff`; effect
`cohen_dz` = (mean_diff − mu)/sd_diff. Groups: `x`, `y` and the differences, named «x − y» with the columns' names (`before − after`). Variant `paired`.

**`stat_anova`** — `value`, `group`, `var_equal` (default true), `posthoc`, `p_adjust`.
H0: all means equal. A1, A2.
- Classic (variant `one_way`): `grand_mean` = Σnᵢmᵢ/N; `ss_between` = Σnᵢ(mᵢ − grand_mean)²;
  `ss_within` = Σ(nᵢ − 1)sᵢ²; `ms_between` = ss_between/(k − 1); `ms_within` =
  ss_within/(N − k); statistic F = ms_between/ms_within; `df_1` = k − 1, `df_2` = N − k.
  Effects: `eta_squared` = ss_between/(ss_between + ss_within); `omega_squared` =
  (ss_between − (k − 1)·ms_within)/(ss_between + ss_within + ms_within).
- Welch (variant `welch`): wᵢ = nᵢ/sᵢ², W = Σwᵢ, m_w = Σwᵢmᵢ/W, Λ = Σ(1 − wᵢ/W)²/(nᵢ − 1);
  F = [Σwᵢ(mᵢ − m_w)²/(k − 1)] / [1 + 2(k − 2)Λ/(k² − 1)]; `df_1` = k − 1,
  `df_2` = (k² − 1)/(3Λ).
- Refused: fewer than two groups; a group with no usable value (named), and for Welch one
  with n < 2; every group of one value (N − k = 0); `ss_within` = 0; Welch with a group of
  variance 0. The classic ANOVA takes a group of one value, as R's `aov` and `oneway.test`
  (var.equal = TRUE) do: it counts in `ss_between`, adds nothing to `ss_within`, and a note
  names it; its `var`, `sd`, `se` and interval are NULL rows. Blocks of Welch: `sum_weights`,
  `weighted_mean`, `welch_lambda`.
- Note, classic only: the largest group variance more than four times the smallest (or one
  of them 0): «Welch's ANOVA (var_equal = false) does not assume them equal.»

Its post-hoc, `posthoc` = `none` (default), `tukey`, `games_howell`, `pairwise_t`; for
every pair (i, j), d = mᵢ − mⱼ = `estimate`:

| `posthoc` | Allowed with | `std_error` | `statistic`, `df` | `p_value` | `p_adjusted` | Interval |
|---|---|---|---|---|---|---|
| `tukey` (Tukey–Kramer for unequal n) | `var_equal` true | √(ms_within/2·(1/nᵢ + 1/nⱼ)) | q = \|d\|/se; df = N − k | null | `srange_sf(q, k, N − k)` | d ± `srange_isf(α, k, N − k)`·se; that quantile is `posthoc_critical`, and the pair's `critical` |
| `games_howell` | `var_equal` false | √(sᵢ²/nᵢ + sⱼ²/nⱼ) | q = √2·\|d\|/se; df = the pair's Welch df | null | `srange_sf(q, k, df)` | d ± `srange_isf(α, k, df)`/√2·se (the pair's `critical`) |
| `pairwise_t` | either | classic: √(ms_within·(1/nᵢ + 1/nⱼ)); Welch: √(sᵢ²/nᵢ + sⱼ²/nⱼ) | t = d/se; df = N − k, or the pair's Welch df | 2·P(T ≥ \|t\|) | by `p_adjust` | d ± q_t·se at the level of «Adjusting» |

`tukey` with `var_equal` false, or `games_howell` with true, is refused with the sentence
that names the other («Tukey's HSD assumes equal variances: with Welch's ANOVA use
Games–Howell»). More than `MAX_POSTHOC_GROUPS` groups: refused, naming the limit. The
post-hoc is computed whatever the ANOVA's own p-value, and a note says when that one is
not below alpha. More than `MAX_POSTHOC_GROUPS` groups are refused only when a post-hoc is
asked.

### Variances

**`stat_var_f`** — `value`, `group`, `groups?`, `ratio` (default 1, above 0),
`alternative`. H0: variance₁/variance₂ = ratio. A1, A2. estimate = s₁²/s₂²; F =
estimate/ratio; `df_1` = n₁ − 1, `df_2` = n₂ − 1; two-sided p = 2·min(P(F′ ≤ F),
P(F′ ≥ F)); interval estimate/q_F(1 − α/2), estimate/q_F(α/2) (one-sided: the one bound
with α); `critical_low` = q_F(α/2), `critical_high` = q_F(1 − α/2). Refused: a variance
of 0.

**`stat_levene`** — `value`, `group`, `center` (`median` default, the Brown–Forsythe form;
`mean`). H0: all variances equal. A1, A11. The classic ANOVA F on z = \|v − centre of its
group\|, from the groups' n, mean and variance of z. Variant `median` or `mean`.

### Proportions and categories

Built in lot 3 (commits c2e3935c, 91c91444, 14485d24 and the four tests after them). Where
the build differs from what this section first said, it says what the code does.

**`stat_prop_one_sample`** — `value` (any column), `success`, `p0` (default 0.5), `method`
(`score` default, `exact`), `correction` (default true; `applies` with `score` only),
`alternative`. H0: proportion = p0. A8 (`successes`). p̂ = k/n = estimate. `success` is a
text, a number or true/false, typed as the outcome column holds it (`aggregates.typed_value`:
a text, a whole or a float number, a date, a boolean) and compared with an equality in the
engine; a column of another type (a datetime, a decimal) is refused, as a success no value of
the column can be («yes» in a column of numbers).
- `score`: Y = min(0.5, \|k − n·p0\|) with the correction, else 0; `chi_square` =
  (\|k − n·p0\| − Y)²/(n·p0·(1 − p0)); statistic z = ±√chi_square with the sign of p̂ − p0;
  normal p-value. Interval (Wilson, with the correction when asked) as R's `prop.test`.
- `exact`: `binomial_test`, `clopper_pearson`; no `statistic` row, the `successes` row is
  there for every method; distribution `binomial(n, p0)`; refused when n + 1 passes
  `EXACT_SUPPORT`.
- The open side of a one-sided interval is the bound of the parameter (0 or 1), as R writes
  it, not NULL: a proportion's interval is never unbounded.
- Group row: the column, with `n`, `rows`, `successes`, `proportion`, `se`, and its two-sided
  interval (the test's method).
- Refused: n = 0; `success` never found is not an error (k = 0), and a note says so.

**`stat_prop_two_sample`** — `value`, `success`, `group`, `groups?`, `correction`,
`alternative`. H0: proportion₁ = proportion₂. A1, A8. As R's `prop.test` of two groups:
Δ = p̂₁ − p̂₂ = estimate; p̄; I = 1/n₁ + 1/n₂; Y = min(0.5, \|Δ\|/I) with the correction;
`chi_square` = (\|Δ\| − Y·I)²/(p̄(1 − p̄)I); z = ±√chi_square; interval
Δ ± (Y·I + z_q·√(p̂₁(1 − p̂₁)/n₁ + p̂₂(1 − p̂₂)/n₂)) inside [−1, 1] (one-sided: −1 or 1 on
the open side); `cohen_h`. Each group's row has its Wilson interval (Y = 0.5 when corrected).
Refused: p̄ = 0 or 1.

**`stat_chi2_independence`** — `x`, `y` (any columns), `correction` (default true; 2×2
only), `posthoc`, `p_adjust`. H0: x and y independent. A7 (`crosstab`, at most
`MAX_STAT_LEVELS`² + 2·`MAX_STAT_LEVELS` + 2 pairs read; more means a column past 50
categories). Categories are labels: −0.0 and 0.0 one, on every engine. As R's `chisq.test`:
the 2 × 2 correction is Y = min(0.5, \|O − E\|), not 0.5 (R since 3.x). Each cell writes
`observed`, `expected`, `residual`, `adj_residual`, `contribution` — the cell's term of the
statistic actually used (corrected when the correction applies), so that they add up to it;
`chi_square_plain` (without the correction) and `yates` are numbers of the test; `phi` is
signed, (ad − bc)/√(R₁R₂C₁C₂). The margins are groups: `row_total`, `row_share` for each
category of x, `col_total`, `col_share` for each of y. A note when more than 20% of the
expected counts are below 5 or one is below 1. Refused: x = y; a column with one category;
past `MAX_STAT_LEVELS`.

Its post-hoc, `posthoc` = `none` (default), `cells`, `pairs`:
- `cells`: each cell's `p_value` = 2·P(Z ≥ \|adj_residual\|), `p_adjusted` over the r·c
  cells, `significance`, `decision`.
- `pairs`: for every pair of categories of `x`, the chi-square of their 2 × c table, the
  columns with no row in either left out (df = c′ − 1; with fewer than two, statistic 0 and
  p = 1); the pair's eleven rows (`_write_pair`), no estimate, no interval. At most
  `MAX_POSTHOC_GROUPS` categories; a note when the test itself does not reject.

**`stat_chi2_gof`** — `value`, `expected` (null = all equal, or `{label: proportion}`; the
form: «all equal» or a box per category, with their sum). H0: the categories have the
expected proportions. A1, NaN and NULL left out, labels merged. E = N·pᵢ; Σ(O − E)²/E;
df = k − 1; `cohen_w`. Cells (level = the category, level_2 null): `observed`, `proportion`,
`expected_share`, `expected`, `residual`, `contribution`. Refused: `expected` that does not
list exactly the categories found, a proportion ≤ 0, a sum not 1 within 1e-9.

**`stat_fisher_exact`** — `x`, `y`, `alternative`. As R's `fisher.test`:
- 2 × 2: `exact.fisher_exact`; estimate = the conditional odds ratio, its interval, found to
  the last digit of their logarithm (R's `uniroot` leaves them up to 1.4e-2 off on some
  tables); `sample_odds_ratio`; distribution `hypergeometric(C1, C2, R1)`; refused when the
  support passes `EXACT_SUPPORT`.
- r × c (Freeman–Halton): the two-sided p-value only, `exact.fisher_rxc` (the network
  algorithm, standard library, deterministic); a one-sided `alternative` is refused with a
  sentence; refused past `EXACT_NETWORK` (4,000,000) steps, «use the chi-square test». A step
  is a cell of a way of filling a column gone through, a path extended, a node remembered, a
  line bounded, each counted as it is taken; a table whose own path through the network
  already costs more is refused before the count (`_count_vectors` counts the ways without
  going through them). Measured in the backend's image: refused after 4.2 s and 235 MB at
  most (3 × 3 to 7 × 7 tables), an 8 × 8 table of 144 rows at once.
  `estimate`, `ci_low`, `ci_high`, `sample_odds_ratio` are NULL rows there.
- Both: `rows`, `cols`, the margins, each cell's `observed` and `expected`. No curve.

**`stat_mcnemar`** — `x`, `y` (two answers of the same row), `method` (`auto` default,
`exact`, `asymptotic`), `correction` (default true, as R's `mcnemar.test`; `applies` with
`auto` and `asymptotic`). The categories are those of the two columns together (a square
table). H0: P(x = i, y = j) = P(x = j, y = i).
- 2 × 2: b = n₁₂, c = n₂₁; chi-square (\|b − c\| − Y)²/(b + c), Y = 1 with the correction;
  or exact, the binomial of b in b + c at ½ (`binomial_test`). `auto` takes the exact test
  below 25 discordant pairs. Estimate: the odds ratio of the discordant pairs b/c, its
  interval Clopper–Pearson's of b in b + c as odds (the convention of R's
  `exact2x2::mcnemar.exact`); `prop_difference` = (b − c)/n is a number of the test.
- k × k: Bowker's test of symmetry, Σ_(i<j) (n_ij − n_ji)²/(n_ij + n_ji) over the pairs with
  n_ij + n_ji > 0, df = their number (R answers NaN when a pair is empty: a note says they
  are left out); `exact` refused.
- Cells: `observed`, `expected` under symmetry ((n_ij + n_ji)/2), `agreement` (a text,
  `concordant` or `discordant`). Refused: x = y; no discordant pair.

**`stat_cmh`** — `x`, `y`, `strata`, `correction` (default true). As R's
`mantelhaen.test`. A7 within strata (`crosstab3`, at most 100,000 triples). Strata of fewer
than 2 rows refused, as R does.
- 2 × 2 × K: Δ = Σ(a − R₁C₁/N), V = Σ R₁R₂C₁C₂/(N²(N − 1)), Y = 0.5 when \|Δ\| ≥ 0.5 and
  the correction is on, χ² = (\|Δ\| − Y)²/V, df = 1; the common odds ratio of Mantel and
  Haenszel and its interval (Robins–Breslow–Greenland). Two-sided only. Each stratum is a
  group with `n`, `a`, `b`, `c`, `d`, `expected_a`, `variance_a`, `odds_ratio`.
- I × J × K: the generalized statistic (n − m)·V⁻¹·(n − m), df = (I − 1)(J − 1), solved by
  Gaussian elimination; refused past 100 equations, or when V is singular.
- Breslow–Day's homogeneity of the odds ratios is **not** built (no base-R reference).

**`stat_g_test`** — `x`, `y`, `williams` (default false), `posthoc` (`none`, `cells`),
`p_adjust`. G = 2·ΣO·ln(O/E) (a cell with O = 0 adds 0), df = (r − 1)(c − 1); Williams'
q = 1 + (N·Σ1/R − 1)(N·Σ1/C − 1)/(6N·df), G/q. Cells as the chi-square's, `contribution` the
cell's term of G; `cramers_v` from Pearson's χ². The post-hoc of the cells as the
chi-square's (adjusted residuals).

All of them: R's significance codes (`significance`) after every p-value, of the test and of
each cell or pair of a post-hoc.

### Correlation

**`stat_pearson`** — `x`, `y`, `alternative`. H0: correlation = 0. A3.
r = cov/√(var_x·var_y) = estimate; t = r·√((n − 2)/(1 − r²)); df = n − 2; interval
tanh(atanh(r) ± z_q/√(n − 3)) (n ≥ 4); effect `r_squared`. Refused: n < 3; a variance of 0.

**`stat_spearman`** (built in lot 4) — `x`, `y`, `alternative`. H0: rank correlation = 0,
written `cor(rank(x), rank(y)) = 0`. A6, and the input's rows (`count_rows`, for
`n_missing`). ρ = Σc·rx·ry/√(Σc·rx²·Σc·ry²) = estimate (Pearson's r of the average ranks, ties
included); t and df as Pearson's (R's `cor.test(method = "spearman", exact = FALSE)`); **no
interval** in the first release. Variant `t_approximation`. Numbers: `sum_rxy`, `sum_rx2`,
`sum_ry2`, `tie_sum_x`, `tie_sum_y`, estimate, `std_error`, t, df, the critical values, the
p-value, `rho_squared`; each column a group with `n`, `min`, `max`. Refused: n < 3; a column
constant on the rows used; infinite values.

### Ranks

Built in lot 4. Where the build differs from what this section first said, it says what the
code does. Every rank test reads its ranks from the engine (A4, A5, A6), sums them there, and
computes only the last step in Python; the exact distributions (`exact.py`) count whole
numbers. Infinite values are refused by name, as everywhere (a rank of +∞ exists, but such a
value is almost always an error upstream).

**`stat_mann_whitney`** — `value`, `group`, `groups?`, `method` (`auto` default, `exact`,
`asymptotic`), `correction` (default true; `applies` with `auto` and `asymptotic`),
`alternative`. H0: P(X > Y) = 0.5, written `P(v | a > v | b) = 0.5`. A1 (the groups and their
rows), A4 restricted to the two groups (ranked together, without the others). `statistic` =
U of group 1 = R₁ − n₁(n₁ + 1)/2 (R's W); `u_2` = n₁n₂ − U.
- Exact (variant `exact`, distribution `exact`): n₁ ≤ `EXACT_MAX_N`, n₂ ≤ `EXACT_MAX_N` and no
  ties. `auto` takes it then; `exact` asked with ties or larger samples falls back to the
  approximation, and a note says why. No `z`, no critical value, no curve (the report says
  why in its place).
- Asymptotic (variant `asymptotic`, distribution `normal`): `mean_u` = n₁n₂/2; `sd_u` =
  √(n₁n₂/12·((N + 1) − tie_sum/(N(N − 1)))); z = (U − mean_u − c)/sd_u with `continuity` c =
  0.5·sign(U − mean_u) two-sided, −0.5 for `less`, +0.5 for `greater`, 0 without the
  correction. `critical_low` and `critical_high` are **of z** (the report says «z ≤ …»), and
  the curve is the normal's with z marked.
- Effects: `cles` = U/(n₁n₂), `rank_biserial` = 2·cles − 1. No interval (a shift estimate
  needs every pair of rows).
- Numbers: alpha, conf_level, n, n_missing, `u_2`, `tie_sum`, `mean_u`, `sd_u`, `continuity`,
  `statistic`, `z`, `critical_low`, `critical_high`, p_value, `cles`, `rank_biserial`. Groups:
  `n`, `rank_sum`, `mean_rank`, `min`, `max`, and the quartiles with `detail = report` (the
  medians are the report's, as every test's quartiles: A12 is not read for the results).
- Refused: a group with no usable value; all the values equal (σ = 0); infinite values; a
  grouping column whose groups no literal names (a datetime, a decimal) holding more than the
  two groups (the ranks are of every value read: «cast it to text first, or filter the two
  groups before the test»). Such a column with exactly two groups is ranked whole, which is
  right.

**`stat_wilcoxon_signed_rank`** — `x`, `y?` (none: one sample, d = x − mu), `mu`, `zeros`
(`wilcox` default, `pratt`), `method`, `correction`, `alternative`. H0: the differences
x − y − mu are symmetric about 0, written `median(x − y) = mu`. A5 (two queries: the ranks of
|d|, and the counts with the extremes). `statistic` = V = the sum of the ranks of the positive
differences; `w_minus`, `n_zero` written.
- `zeros = wilcox` (R's): zeros left out before ranking; `n` = the non-zero differences.
  Exact when n ≤ `EXACT_MAX_N`, no ties, no zeros. Asymptotic: `mean_w` = n(n + 1)/4; `sd_w` =
  √(n(n + 1)(2n + 1)/24 − tie_sum/48).
- `zeros = pratt` (SciPy's `zero_method = "pratt"`; base R has none): zeros ranked (the lowest
  ranks), then left out of the sums; `n` counts them; with n₀ zeros: mean n(n + 1)/4 −
  n₀(n₀ + 1)/4; sd √((n(n + 1)(2n + 1) − n₀(n₀ + 1)(2n₀ + 1) − tie_sum/2)/24), tie_sum over the
  values above zero; asymptotic whenever there is a zero.
- The same continuity correction and the same z-scale critical values as Mann–Whitney's.
  Effect: `rank_biserial` = (w_plus − w_minus)/(w_plus + w_minus). `null_value` = mu.
- Groups: x, y and «x − y» (the extremes of x − y, without mu) with `n` (the pairs), `min`,
  `max`, and the quartiles for the report; one sample: x alone.
- Refused: x = y; no pair; every difference 0; an infinite value in x or y (the difference of
  two infinities would be NaN, which no two engines order alike).

**`stat_kruskal_wallis`** — `value`, `group`, `posthoc` (`none`, `dunn`), `p_adjust`
(`applies` with `dunn`). H0: all the groups have the same distribution, written
`F(v | a) = F(v | b) = …`. A1, A4 over every group. H is written without the cancellation of
R's form: H = 12/(N(N + 1))·Σnᵢ(R̄ᵢ − (N + 1)/2)², divided by C = 1 − tie_sum/(N³ − N)
(`tie_correction`; the uncorrected H is `statistic_plain`); df = k − 1; chi-square p-value;
`epsilon_squared` = H/(N − 1). Groups: `n`, `rank_sum`, `mean_rank`, `min`, `max`. Refused:
fewer than two groups; a group with no usable value (named: «filter it out first»); all the
values equal (C = 0); infinite values; the post-hoc past `MAX_POSTHOC_GROUPS`.

Its post-hoc, `posthoc` = `none` (default), `dunn`: for every pair, `estimate` = the
difference of the mean ranks R̄ᵢ − R̄ⱼ (ranks over all the groups together, as the test's);
`std_error` = √((N(N + 1)/12 − tie_sum/(12(N − 1)))·(1/nᵢ + 1/nⱼ)); `statistic` z =
estimate/se; `p_value` = 2·P(Z ≥ |z|); `p_adjusted` by `p_adjust`; `df`, `critical` and the
interval NULL (the eleven rows of `_write_pair`). `n_pairs` in the test section; a note when
the test itself does not reject. At most `MAX_POSTHOC_GROUPS` groups.

### Distributions

Built in lot 4.

**`stat_ks_two_sample`** — `value`, `group`, `groups?`, `method` (`auto`, `exact`,
`asymptotic`), `alternative`. H0: the two distributions are the same, written
`F(v | a) = F(v | b)`. A1, A2 (the groups' rows, values and extremes), A9. The distances come
back from the engine **as whole numbers**, max(k₁n₂ − k₂n₁) and max(k₂n₁ − k₁n₂) over the
running counts, so that the exact distribution reads them exactly and every engine returns
the same: `d` (two-sided D), `d_plus` = max(F₁ − F₂), `d_minus` = max(F₂ − F₁); `statistic` is
the one the alternative uses (D, D+ for `greater`, D− for `less`, as R's `ks.test`).
- Exact (variant `exact`) when n₁·n₂ ≤ `EXACT_KS_PRODUCT` and no value is held by more than
  one row (`n_tied`); no critical value, no curve.
- Asymptotic (variant `asymptotic`, distribution `kolmogorov`, or `smirnov` one-sided):
  `lambda` = D·√(n₁n₂/(n₁ + n₂)); p = `dist.kolmogorov_sf` (two-sided) or exp(−2λ²);
  `critical_high` = `kolmogorov_isf(α)`/√(n₁n₂/(n₁ + n₂)); the curve is the density of D under
  Kolmogorov's (Smirnov's) limit.
- Groups: `n`, `min`, `max` and, for the report, the quartiles.
- Refused: a group with no usable value; infinite values; a grouping column whose groups no
  literal names (a datetime, a decimal), whatever it holds: the distance needs each row's
  group in the query.

**`stat_normality`** — `value`, `method` (`dagostino` default, `jarque_bera`). H0: the values
come from a normal distribution, written `(skewness(v), kurtosis(v)) = (0, 3)`. A10 centred.
`skewness` g₁ = m₃/m₂^1.5; `kurtosis` b₂ = m₄/m₂²; `excess_kurtosis` = b₂ − 3; `mean`, `m2`,
`m3`, `m4` written.
- `jarque_bera`: n/6·(g₁² + (b₂ − 3)²/4), chi-square with 2 df. Refused below 3 values.
- `dagostino` (the K² omnibus, SciPy's `normaltest`): the z of D'Agostino's skewness test
  (written with `asinh`, SciPy's logarithm without its cancellation; 0 for g₁ = 0, where SciPy
  writes another number) and of Anscombe–Glynn's kurtosis test, `z_skewness`, `z_kurtosis`
  (NULL rows with Jarque–Bera); K² = their squares summed, chi-square with 2 df. Refused below
  n = 20 (`DAGOSTINO_MIN_N`), with the way out («or use method = 'jarque_bera'»).
- Group: the column with `n`, `mean`, `var`, `sd` (of the sample), `min`, `max`, quartiles.
- Refused: variance 0; infinite values.

### Options looked at again, now that each test has its node

Added because a node of its own can carry them: the post-hoc of `stat_anova`,
`stat_kruskal_wallis`, `stat_chi2_independence`; `ratio` of `stat_var_f`; `zeros` of
`stat_wilcoxon_signed_rank`. Looked at and left out, with the reason: a trimmed-mean
centre for Levene (a trimmed mean is not an aggregate every engine has); an interval for
Spearman's ρ (no agreed formula, no reference to test against); other intervals for a
proportion (Wilson and Clopper–Pearson cover the two methods); pairwise Mann–Whitney after
Kruskal–Wallis (it re-ranks every pair: k² passes over the rows; Dunn needs none).

## Not in the first release

- One-sample Kolmogorov–Smirnov against a normal: it needs the normal CDF of every value
  inside the engine, and only ClickHouse has `erf`.
- Shapiro–Wilk: its statistic weighs the ordered values one by one; it is not an aggregate.
- An interval for Spearman's ρ and for the Mann–Whitney shift; exact p-values with ties
  (the conditional permutation distribution); Tukey's whiskers and outliers on the box plot
  (it draws minimum and maximum); a scatter plot for the correlations.
- Dunnett's comparisons with a control, Scheffé, the Benjamini–Hochberg correction;
  two-way ANOVA, tests by stratum, power and sample size.
- A report kept as an object of a folder («Save as report», the optional last piece).

## The path of a new node

| Where | File | What | Model to follow |
|---|---|---|---|
| Catalogue, checks | `backend/app/engine/stats/catalog.py`, `catalog.json` | `TESTS`, the readers of each kind, the limits, `check_params(test, params, columns)`, `operation_name`, `stat_operations()`, `is_stat_operation()`, `label_of`, `refusal`; from piece 1.2 `KINDS`, `FAMILIES`, the snapshot | `t_two_sample` there |
| Aggregates | `backend/app/engine/stats/aggregates.py`, `polars_source.py` | A method on `SqlStatSource` and the same on `PolarsStatSource`, centred where it sums | `moments` |
| Results | `backend/app/engine/stats/compute.py`, `dist.py`, `exact.py`, `output.py` | One function `(source, p, out, engine_name)` in `_TESTS`; `output.Rows` (`text`, `number`) writes the rows and their keys; `to_polars`, `to_arrow`, `values_sql` hand them to an engine | `_t_two_sample` |
| The four registries | `operations.py`, `duckdb_ops.py`, `chdb_ops.py`, `bigquery_ops.py` (`_stat_operation`, a loop over `stat_operations()`); `rows()` on the chDB, ClickHouse and BigQuery contexts; `clickhouse_engine` (`is_stat_operation` among the scan-heavy) | Nothing to add for a new test | — |
| Copies and their check | `.github/scripts/stat_catalog.py`, `.github/workflows/stat-catalog.yml` | «One catalogue» | `config_reference.py`, `config-reference.yml` |
| Gateway: names | `gateway-go/internal/statcatalog/` (the embedded copy, `Operations()`, `Is()`); `api/flow_shortcuts.go`; `dbtexport/payload.go` (`knownRefusal`) | «One catalogue» | `notebookpod_test.go` reads a file of another component the same way |
| Engine: dbt | `backend/app/engine/dbt_export.py` | Nothing: the last `raise` already says `op_not_exportable`; a test | — |
| Palette | `frontend/components/ui/OpSidebar.vue`, `composables/useOpIcons.ts` | «In the palette» | The control group |
| Form | `frontend/components/ui/StatTestForm.vue`, `composables/statTests.ts`; `NodePanel.vue` shows it in place of `ParamForm` | The test's keys from the catalogue, each by its kind's control; numeric columns only where a number is needed; `groups` and `success` filled with `fetchDistinct`; an option another option forbids (Tukey with unequal variances) is disabled with the reason | `ParamForm.vue` |
| Model | `frontend/composables/useFlowModel.ts`, `opSummary.ts`, `previewGrid.ts` | One branch for `stat_*` | `pivot` |
| Texts | `frontend/i18n/locales/{en,it,de,es,fr}.gen.ts` (namespaces `stat`, `statReport`; `opSidebar.statisticsGroup`) | Five languages | Any namespace |
| The report of a step | «The report» | — | — |
| Assistants, MCP | `gateway/app/services/ai_flow_agent.py`, its copy of the catalogue; then `gateway-go/internal/aiprompts/prompts.json` and `internal/mcp/tools.json` written again by `gen_prompts.py` and `gen_tools.py` | «One catalogue» | `pivot` |
| Lineage | — | Nothing: lineage is by dataset, and the step neither reads nor writes one of its own | — |
| Documentation | `docs/capabilities/statistics.md` (new), `capabilities/README.md`, `capabilities/flows.md`, `capabilities/reports.md`, `engines/engine-differences.md`, `dbt-export.md`, `README.md`, `CHANGELOG.md`; the site | Below | `capabilities/reports.md` |

Names exported from a composable are auto-imported everywhere: every export of
`statTests.ts` and `statReport.ts` starts with `stat` or `STAT_`.

No keyboard shortcut is added: the form's controls are native fields, the palette's group
is a button, the Report tab is a tab of the existing tab list (arrows, as Table and Chart),
on every keyboard.

## The report

### What exists (verified) and what is missing

| Need | State |
|---|---|
| A place in the flow editor for a node's result | **Exists**: the preview panel under the canvas (`FlowEditor.vue`, `.grid`): a tab list with **Table** (`DataGrid`) and **Chart** (`ChartPanel`), a bar that says whose rows are shown and on which sample, a height the person drags or moves with the keyboard (a third of the window by default, kept in `localStorage`) |
| Pages of a report on screen, read-only | **Exists**: `ReportPages.vue`, used by the viewer (`pages/report.vue`) and by the designer's Preview: each page the SVG of the same layout as the PDF, zoom, lazy pages |
| A report drawn from rows handed to it | **Exists**: a dataset may be `{"rows": […], "columns": […]}` (`reports/data.py`) |
| PDF and Excel of a report | **Exist**: the render's `pdf` and `xlsx` formats (Excel: the report's tables, one sheet each) |
| A render followed and downloaded | **Exists**: the export queue, `GET /tasks/exports/{id}` and `/file`, owned by who asked |
| Tables, texts with expressions, KPI cards, `LOOKUP`, `SUMIF`, `visible` | Exist |
| Histogram; observed against expected; residuals | Exist: `column` with one series per group; `column`; `heatmap` |
| Box plot | **Missing** |
| A mean with its interval | **Missing** |
| A curve on a numeric axis with shaded regions and a marked value | **Missing** (`scatter` draws points; `line` and `area` have categories) |
| A chart over some rows of its dataset | **Missing**: tables have `filter`, charts do not |
| ECharts able to draw them on the server | Exists: the service's bundle (echarts 6.1.0) has `series.boxplot`, `markArea`, `markLine` (checked in the bundle), and `render.mjs` takes any JSON option without images |
| A route that draws a report for a step of a flow | **Missing** |
| A layout per test | **Missing** |

### In the editor

A third tab, **Report**, in the preview panel's tab list, present when the node whose
preview is on screen is a statistics node (`viewTab: 'table' | 'chart' | 'report'`; with
another node selected the panel falls back to Table).

`components/ui/StatReportView.vue` fills it:

- **the pages**: `ReportPages` as it is (no hotspots, no definition), at 75% by default,
  the zoom kept in `localStorage`; zoom out, zoom in, actual size, **Fit width**; «Page n of
  m»;
- **Expand**: the panel to its largest height and back (the resizer's own limits);
- **PDF** and **Excel**: the same request with another format, downloaded when ready
  (`useReports().followRender(…).download()`);
- the bar above it is the preview's own, with its sample chip when the source has a
  development sample: the same rows as the Table tab, said the same way. **No control
  chooses other rows**: whoever wants the test on every row changes the source's sample,
  as for any other step;
- while it is computed: the page-shaped skeleton of `ReportPages`, the seconds after a
  while; an engine refusal (three groups for a two-sample test) is shown as the table
  shows a preview's error, with its sentence;
- it is drawn when the tab is opened for a node, or in another language. When the node's
  parameters or anything before it change while it is open, the pages stay, marked «Not up
  to date», with **Update**: never a render at each keystroke. A test that still lacks a
  column is not asked: the tab says what to set;
- a render's file is handed once: the pages are kept in the editor's memory by the
  preview's identity, the language and the step's name, so going back to a node shows its
  report at once; a download is read as a Blob and saved from the page (a link to a gateway
  on another origin is a navigation, and the editor would ask to confirm leaving an unsaved
  flow); the header band names the step only when the person named it.

Read-only by construction: there is nothing to edit, and the layout is not the person's.

### The route

`POST /tasks/stat-report`, Go gateway only (`api/proxy.go`, beside `/tasks/exports`;
declared in `server/go_only_routes.go` after `/tasks/profile`).

```json
{ "engine": "duckdb", "bucket": "…", "input_key": "…",
  "operations": [ … the chain the preview would send, ending with the stat_* step … ],
  "format": "svg", "locale": "it", "filename": "Welch t test — amount by channel",
  "step": "Revenue by channel" }
```

- **Rights**: `exportBody` as a step's CSV export: the keys the chain reads pinned to the
  engine's bucket and readable by the person (`authorizeExport`, which also writes the
  audit line), the owner of the result set to the person. Whoever may download the step's
  rows may draw its report; nobody else.
- **Checks**, before anything is read, 422 otherwise: the body is a JSON object, it has a
  chain, the last operation is a statistics one (`statcatalog.Is`), `format` is `svg`, `pdf`
  or `xlsx`. A `locale` Tabularia does not speak is `en`, not an error. Nothing else of the
  request travels to the engine but the engine, the source, the format, the language, the
  file's name and the step's name (`step`): no definition is taken from a browser. A
  read-only token may use the route (it only reads); the render waits for its place among
  the person's exports and the interface is told at once.
- **The chain is forwarded as it came**: the gateway adds nothing to it and takes nothing
  out. The rows are those the preview of the same node reads.
- Forwarded to the engine's `POST /reports/stat`; the answer is `{task_id, filename}`, and
  the render is followed and read as every export (`/tasks/exports/{task_id}`, `/file`).

The engine's `POST /reports/stat` (`backend/app/api/routes/reports.py`, beside `/render`)
starts `render_stat_report_task` (`app/tasks/report_jobs.py`) on the export queue, with
the per-person cap every export has:

1. **The rows**: the chain with the last step's `detail` set to `report`, read once
   (`engine.preview`, the step cache on for what is before the test), sorted by `ord`.
2. **The definition**: `stats/layouts.definition(test, params, locale, sample, step)`, where
   `sample` is what the chain says of itself: its operations marked `_dev_sample`, in the
   main chain or in a branch (a `limit`: «the first 100,000 rows»; a `sample`: «a random
   10%»), or nothing.
3. **The render**: `reports.render.render(definition, {"result": {"rows": …, "columns": …}},
   {}, format, …)`, the rows carrying one more column, `label`, the translated name of
   each `stat`. The file goes where every export goes.

### On which rows, and how fast

- **The rows that reach the node** (owner, 2026-11-19). The report is computed on the
  chain the editor sends for the node's preview: the Table tab and the Report tab show the
  same test on the same rows, always. A run computes the node on the run's rows; the
  report is drawn in the editor only.
- **A development sample is said, not chosen here.** When the source upstream has one
  (the automatic one included), the rows that arrive are its rows: the Table tab's bar
  shows the sample chip, and the page says the same in its header band, in the PDF too
  («Rows: a development sample of the source — the first 100,000 rows»). It costs nothing
  more: the editor already marks the sample's operation in the chain it sends
  (`_dev_sample`), and the engine reads the mark. With no mark the page says nothing about
  the rows but their number, `n`.
- **One computation.** Nothing is computed twice for a node: no second chain, no other
  basis.
- **Remembered rows.** The rows of step 1 are kept in Valkey under
  `dataprep:statreport:` + the chain's hash (`rowcount.count_key`, as the column profile
  does), for the counts' lifetime, when they weigh less than 2 MB. The PDF and the Excel
  asked after the pages, another language, a second person on the same flow: no query
  reaches the data again. A refresh of the datasource changes its key, so nothing stale
  is served.
- **In the browser**, the pages of the last renders are kept by the preview's identity, the
  language and the step's name: going back to a node shows its report at once.
- **Very large tables.** The report costs what the node's preview costs on the same rows,
  plus the two passes of the charts (quartiles, histogram): one or two passes for most
  tests, a sort of the distinct values for the rank tests («Cost»). It runs on the export
  queue, not on the preview's: it does not hold up the editor, it counts against the
  person's exports, and the workers' time limit ends it with a sentence («the test took
  longer than the workers allow: give the source a development sample, or filter before
  the test»). On a source with a development sample, the automatic one included, it is as
  fast as the preview.

### The layouts

`backend/app/engine/stats/layouts.py`: one function per layout family returning a report
definition (`docs/specs/reports.md`), and `LAYOUTS = {test id: function}`; every test of
the catalogue has one (a test of the suite). The words are in
`backend/app/engine/stats/report_texts.json`: one object per language (`en`, `it`, `de`,
`es`, `fr`) with the titles, the sentences, the names of the tests and of every `stat`;
`tests/test_stats_layouts.py` fails when a language lacks a key, when a definition does
not pass `reports.definition.check`, or when an expression reads a key the test does not
write. In the engine and not in the interface's catalogues because the page is drawn
there, the PDF must not depend on a browser, and the catalogue the layouts follow is
there.

One dataset, `result`; no parameters. A4 portrait; the report's locale = the request's.
Blocks, top to bottom; each layout leaves out what its test has not, and an item whose
rows may be missing carries a `visible` expression
(`=COUNTIF([section] = "curve") > 0`):

1. **Header band**: the test's name, the step's name, the line about the development
   sample when the rows come from one, the date, «Page x of y».
2. **Result**: four KPI cards — statistic, degrees of freedom, p-value, estimate with its
   interval — and one sentence: «At α = 0.05 the null hypothesis is rejected» (or «is not
   rejected»), from `decision`. A p-value below 0.0001 prints as «< 0.0001», a number below
   1e-6 by its exponent (on a very large table a p-value is that small more often than
   not), a large one with fewer decimals. R's stars stand beside the p-value in its card
   («0.0005 ***»); the sentence keeps to the node's alpha.
3. **Hypotheses**: H0, H1, the alternative, alpha, from the `text` rows.
4. **How it is computed**: the `formula` row, then a table of every number of the `test`
   section in the order of the output (`[label]`, `[value]`), so that the page holds every
   piece of the formula; the `distribution` and the critical values. A column «Signif.»
   beside the value holds the stars of the p-value, with R's legend under the table («Signif.
   codes: 0 ‘***’ 0.001 ‘**’ 0.01 ‘*’ 0.05 ‘.’ 0.1 ‘ ’ 1»), in the five languages with their
   decimal mark; in Excel it is a column of its own, so that the p-value stays a number.
5. **Groups**: tables over `[section] = "group"`, grouped by `[level]`, one column per
   statistic of the family (`=SUMIF([stat] = "mean", [value])`). The two-groups layout draws
   two tables, their columns 40 mm wide (a variance of eleven digits must not run into the
   mean beside it): n, mean, variance, standard deviation; then the standard error and the
   interval of the mean («Intervals of the means», a sheet of its own in Excel); and the
   five-number summary.
6. **Comparisons** (with a post-hoc): a table over `[section] = "pair"` — the pair, the
   difference, its standard error, the statistic, p, adjusted p with its «Signif.» column and
   legend, the interval, the decision — with the method and the correction above it. They
   are drawn from what a post-hoc says of itself (`_Comparisons`: the pairs or the cells of
   a table, which numbers, whether the pairs have intervals); an `interval` chart of the
   differences with their simultaneous intervals and a line at 0 (where the method has
   intervals).
7. **Charts**:

| Layout | Tests | Charts |
|---|---|---|
| Two groups | `t_two_sample`, `var_f`, `mann_whitney`, `ks_two_sample` | `boxplot` by group; `interval` of the means (not for ranks); `column` histogram, one series per group; `density` with the critical region and the statistic |
| Several groups | `anova`, `levene`, `kruskal_wallis` | The same, and «Comparisons» |
| One sample | `t_one_sample`, `normality` | `boxplot`; `column` histogram; `interval`; `density` |
| Paired | `t_paired`, `wilcoxon_signed_rank` | `boxplot` and `column` histogram of the differences; `interval`; `density` |
| Proportions | `prop_one_sample`, `prop_two_sample` | `interval` of the proportions; `density` (score method) |
| Contingency | `chi2_independence`, `chi2_gof`, `fisher_exact` | `column` observed against expected; `heatmap` of the adjusted residuals (r × c); the table of the cells; «Comparisons» for `pairs`; `density` |
| Correlation | `pearson`, `spearman` | `density` |

8. **Notes**: the `note_*` rows, the rows left out, the engine.
9. **Footer band**: «Computed by Tabularia», the sample line again when there is one, the
   time.

Excel holds the tables of blocks 4, 5, 6 and of the cells, one sheet each: the numbers, not
the charts.

### Three chart kinds and a filter

`CHART_KINDS` gains `boxplot`, `interval`, `density` in `backend/app/reports/definition.py`;
`ChartItem` gains `filter` (an expression, as a table's: the rows the chart draws, applied
in `model.chart_numbers` with `self.d.where`, as `model.py` does for tables).

| Kind | Reads | Draws (`charts.py`, one preset each, plain JSON) |
|---|---|---|
| `boxplot` | `category`; exactly five series: minimum, first quartile, median, third quartile, maximum | A `boxplot` series on a category axis, the value axis not from zero |
| `interval` | `category`; exactly three series: value, low, high; `options.target` | A `boxplot` series with data `[low, value, value, value, high]` and a narrow box (a line with caps and a tick), a `scatter` series for the value; the target as a dashed `markLine` |
| `density` | `category` = x (a number, not an aggregate), one series = y, `split_by`; `options.target` (a number or an expression) | Points as `scatter`'s (`chart_numbers`: the same branch), sorted by x; the first split a line, the others filled areas without a line in the «bad» colour; the target as a vertical `markLine` with its value |

`reports.definition.check` refuses a wrong number of series for these kinds, saying which
they are (five, three, one), and a `split_by` on the two that draw one mark a category. A
`LOOKUP` still reads the whole dataset, which is how a density names its target among rows
it does not draw.
`tests/report_samples.py` and `frontend/chart-renderer/presets.sample.json` gain one chart
of each (17), `render.test.mjs` counts 17.

In the first release they exist **in the engine only**: the gateway's validator of saved
reports (`gateway-go/internal/reportdef`) and the report designer do not list them, so a
saved report or a dashboard cannot name them, and nothing of theirs changes. They reach
the designer with «Save as report».

## Reference values

Computed for this spec with **R 4.6.1** (`t.test`, `oneway.test`, `prop.test`,
`binom.test`, `cor.test`, `var.test`, `anova(lm())`, `wilcox.test`, `kruskal.test`,
`chisq.test`, `fisher.test`, `ks.test`, `pairwise.t.test`, `p.adjust`, `TukeyHSD`),
**SciPy 1.18.1** (`normaltest`, `jarque_bera`, `tukey_hsd`, `studentized_range`,
`wilcoxon`; `mannwhitneyu`, `ttest_ind`, `fisher_exact`, `ks_2samp` as a second witness),
**scikit-posthocs 0.18.0** (`posthoc_dunn`) and **pingouin 0.7.0**
(`pairwise_gameshowell`), none of which is part of Tabularia. alpha = 0.05 throughout.

Data:

```text
A = 5.1 4.9 6.2 5.8 6.0 5.5 5.3 6.7                      (group a, n = 8)
B = 6.9 7.2 6.5 7.8 7.1 6.4 8.0                          (group b, n = 7)
C = 5.9 6.1 7.0 6.6 5.7 6.3                              (group c, n = 6)
M1 = 3 5 5 6 7 7 7 9          M2 = 5 7 8 8 9 10 10 12 12        M3 = 4 5 6 6 8
BEFORE = 72 80 65 90 85 77 68 95 88 70     AFTER = 75 78 70 94 85 83 71 97 86 76
W1 = 1.83 0.50 1.62 2.48 1.68 1.88 1.55 3.06 1.30
W2 = 0.878 0.647 0.598 2.05 1.06 1.29 1.06 3.14 1.29
PX = 1.2 2.3 2.9 4.1 5.0 6.2 6.8 8.1 9.3 10.0    PY = 2.1 2.9 3.2 4.8 4.9 6.9 6.5 8.8 8.9 10.9
SX = 86 97 99 100 101 103 106 110 112 113        SY = 2 20 28 27 50 29 7 17 6 12
ST1 = 1 2 2 3 4 4 4 5 6 7                        ST2 = 2 1 3 3 5 4 6 6 7 9
NV = 4.2 5.1 3.9 6.3 5.5 4.8 5.0 7.9 4.4 5.2 6.1 3.5 5.7 4.9 5.3 9.4 4.6 5.8 5.0 4.1 6.6 5.4 4.7 5.9
T23 (rows m, f; columns x, y, z) = 30 20 10 / 15 25 20
T32 (rows x, y, z; columns m, f) = 30 15 / 20 25 / 10 20
T22 = 12 5 / 7 16
GOF = 18 22 30 30
```

The tables of the tests add to A/B/C one row with a NULL value and one with a NULL group,
which every engine must leave out.

Groups: A mean 5.6875, variance 0.364107142857143, quartiles 5.25, 5.65, 6.05; B mean
7.128571428571429, variance 0.365714285714286, quartiles 6.7, 7.1, 7.5; C mean
6.266666666666667, variance 0.226666666666667, quartiles 5.95, 6.2, 6.525.

### The tests

| Test (data) | Expected |
|---|---|
| `t_one_sample` (A, mu 5) | t 3.2225754674565299, df 7, p 0.0146011836135197, se 0.213338681108567, CI 5.18303418084786 – 6.19196581915214, cohen_d 1.13935248296196; `greater`: p 0.00730059180675986, ci_low 5.28331309913358904; `less`: p 0.99269940819324, ci_high 6.09168690086641 |
| `t_two_sample` Welch (A, B) | t −4.609021221688904824, df 12.727892707548933870, p 0.000516870272598936, se 0.312663222679493, CI −2.118010136687659 – −0.764132720455199, the pooled_var, cohen_d and hedges_g of Student's row; `less`: p 0.000258435136299468, ci_high −0.886459039581639519, ci_low and critical_high NULL |
| `t_two_sample` Student (A, B) | t −4.60974997282307442, df 13, p 0.00048895585417271, se 0.312613794038139, CI −2.116432470789339 – −0.765710386353519, √pooled_var 0.604027235394979, cohen_d −2.385772270068418, hedges_g −2.245432724770276 |
| `t_paired` (BEFORE, AFTER) | t −2.6434026631884051, df 9, p 0.0267640719920123, mean_diff −2.5, sd_diff 2.990726407487727, se 0.945750730607407, CI −4.639436789465182 – −0.360563210534818, cohen_dz −0.835917318863029 |
| `anova` classic (A, B, C) | F 11.93129260658611024, df 2 and 18, p 0.00050236357775741, ss_between 7.790297619047622, ss_within 5.876369047619047, ms_between 3.895148809523811, ms_within 0.326464947089947, grand_mean 6.333333333333333, eta_squared 0.570021777003484, omega_squared 0.510062216369851 |
| `anova` Welch (A, B, C) | F 10.12076202914818346, df 2 and 11.86715469755575469, p 0.00272306443987334 |
| `var_f` (A, B) | F 0.995605468750001, df 7 and 6, p 0.979311049486517, CI 0.174806536764672 – 5.096102780610449; `ratio` 2: F 0.497802734375, p 0.382685554750232, the same estimate and CI |
| `levene` (A, B, C) | median: F 0.268588151223331, df 2 and 18, p 0.767468080882170; mean: F 0.284554333406152, p 0.755671470681428 |
| `prop_one_sample` score (37 of 120, p0 0.25) | without correction: chi_square 2.177777777777778, p 0.140016503197171, CI 0.232727369889163 – 0.395829977238007; with: chi_square 1.877777777777778, p 0.170586932871441, CI 0.228988034190828 – 0.400158020496785 |
| `prop_one_sample` exact (same) | p 0.140826101479329, CI 0.227255119633832 – 0.399136764531952; `greater`: p 0.0873786876294322, ci_low 0.2390137473904261 |
| `prop_two_sample` (37 of 120, 52 of 110) | without: chi_square 6.5382911109662265, p 0.0105576718290949, CI −0.2890199799483122 – −0.0397678988395665; with: chi_square 5.8636553800013065, p 0.0154566613661394, CI −0.2977321011604335 – −0.0310557776274453; cohen_h −0.338830093855416 |
| `chi2_independence` (T23) | statistic 8.8888888888888893, df 2, p 0.0117436284570214; expected 22.5, 22.5, 15 in both rows; residuals ±1.58113883008419, ∓0.52704627669473, ∓1.29099444873581; cramers_v 0.272165526975909 |
| `chi2_independence` (T22) | with correction: 4.8122864706523361, df 1, p 0.0282575285263613; without: 6.3198918011140410, p 0.0119390913779123; phi 0.397488735724487 |
| `chi2_gof` (GOF) | all equal: 4.32, df 3, p 0.228918864336105; expected 0.1, 0.2, 0.3, 0.4: 9.1, p 0.0279904579321571, cohen_w 0.301662062579967 |
| `fisher_exact` (T22) | p 0.023836895850244; odds_ratio 5.22965523049642, CI 1.16935473342543 – 27.30369774777703 (these three within 1e-4: R finds them by a root search of that tolerance); sample_odds_ratio 5.48571428571429; `greater`: p 0.0135692310775737, ci_low 1.42562760456438; `less`: p 0.997986250989588 |
| `pearson` (PX, PY) | r 0.986487659099871, t 17.0305399622590, df 8, p 1.43496376008138e-07, CI 0.941881378531566 – 0.996912862657782 |
| `spearman` (SX, SY) | ρ −0.175757575757576, t −0.504978249175835, df 8, p 0.627188344776484 |
| `spearman` with ties (ST1, ST2) | ρ 0.950505395110922, p 2.47274108399959e-05 |
| `mann_whitney` (A, B) | rank sums 38 and 82, u_1 2; exact: p 0.00124320124320124, `less` 0.000621600621600622; asymptotic: with correction 0.00316694038165669, without 0.0026218217690906; cles 0.0357142857142857, rank_biserial −0.9285714285714286 |
| `mann_whitney` with ties (M1, M2) | rank sums 47 and 106, tie_sum 108, u_1 11; asymptotic (the exact one does not apply): with correction 0.0171277510389763, without 0.0149906050765829, `less` with correction 0.00856387551948813 |
| `wilcoxon_signed_rank` (W1, W2) | w_plus 40, w_minus 5, n 9; exact: p 0.0390625, `greater` 0.01953125; asymptotic with correction 0.0440109840129514 |
| `wilcoxon_signed_rank` (BEFORE, AFTER), `zeros` wilcox | n 9, n_zero 1, w_plus 4, w_minus 41, tie_sum 36; asymptotic: with correction 0.0320514161821423, without 0.0275723476449571 |
| `wilcoxon_signed_rank` (BEFORE, AFTER), `zeros` pratt | n 10, n_zero 1, w_plus 6, w_minus 48, tie_sum 36; with correction 0.035685440357509104, without 0.03141900555774588; `less` with correction 0.017842720178754552 |
| `kruskal_wallis` (A, B, C) | rank sums 49, 119, 63; H 11.5227272727272663, df 2, p 0.0031468175565042, epsilon_squared 0.576136363636363 |
| `kruskal_wallis` with ties (M1, M2, M3) | rank sums 70, 145.5, 37.5; H 8.1224137931034530, p 0.0172282138910574 |
| `ks_two_sample` (A, B) | D 0.875; exact: p 0.00248640248640249, `greater` (d_plus 0.875) 0.00124320124320124, `less` (d_minus 0) 1; asymptotic: 0.00658132279128158, `greater` 0.00329066151289614 |
| `ks_two_sample` (A, C) | D 0.5; exact: 0.301698301698302, `greater` 0.151182151182151; asymptotic **0.358081192617015** |
| `normality` (NV) | n 24, mean 5.3875, m₂ 1.57026041666667, m₃ 2.81401171875, m₄ 13.59988260904948, skewness 1.43010648851010, kurtosis 5.51558768833777; `jarque_bera`: 14.50899969163128844, p 0.00070698589284095; `dagostino`: z_skewness 2.91579286163371, z_kurtosis 2.43923502194278, K² 14.4517155042263, p 0.000727528232498793 |

### The post-hoc

Pairs in the order a–b, a–c, b–c (first minus second; R prints the opposite sign).
Differences of the means: −1.44107142857143, −0.579166666666667, 0.861904761904762.

| Post-hoc (data) | Expected, pair by pair |
|---|---|
| `anova` classic, `tukey` (A, B, C) | se 0.209100416033208, 0.218195947374412, 0.224776193244824; q 6.891767390565908, 2.654341996888002, 3.834501997130909; p_adjusted 0.00034310104135326647, 0.17408361204612266, 0.03631708245403065; posthoc_critical 3.609303828706538; CI −2.1957783607442165 – −0.6863644963986396, −1.366702134933383 – 0.20836880160004978, 0.050619187024135925 – 1.673190336785387 (SciPy; p within an absolute 1e-10, the rest within a relative 1e-9) |
| `anova` classic, `pairwise_t` (A, B, C) | se 0.29571264425202, 0.308575668031740, 0.317881540985426; t −4.87321545622947, −1.876903225587748, 2.711402364644625; df 18; p_value 0.000122248091448915, 0.076838117463392711, 0.014300940338429397; `holm` 0.000366744274346744, 0.076838117463392711, 0.028601880676858794; `bonferroni` 0.000366744274346744, 0.230514352390178145, 0.042902821015288195; `sidak` 0.000366699442385987, 0.213255723148878640, 0.042292195115505637 |
| the same, intervals | `bonferroni`: −2.221499921684951 – −0.660642935457907, −1.393542542350462 – 0.235209209017129, 0.0229693398251015 – 1.7008401839844232; `sidak`: −2.219117495130157 – −0.663025362012701, −1.39105648407743 – 0.23272315074410, 0.0255303714185868 – 1.6982791523909380; `holm`: none |
| `anova` Welch, `pairwise_t` (A, B, C) | p_value 0.000516870272598936, 0.067972380652348618, 0.015238130785358911; `holm` 0.00155061081779681, 0.06797238065234862, 0.03047626157071782; `bonferroni` 0.00155061081779681, 0.20391714195704586, 0.04571439235607673 |
| `anova` Welch, `games_howell` (A, B, C) | se 0.312663222679493, 0.288602097419476, 0.300037790514731; df 12.727892707548936, 11.933163746175635, 10.946230961174214; q 6.518140320977861, 2.838043667035194, 4.062546260151267; p_adjusted 0.001392913400370, 0.153135474005365, 0.037430036650937 (pingouin; R's `ptukey` agrees to 5e-12); CI −2.268825694185579 – −0.613317162957279, −1.3496990802928008 – 0.19136574695946762, 0.05095619508955207 – 1.6728533287199727 (with SciPy's quantile) |
| `kruskal_wallis`, `dunn` (A, B, C) | estimate −10.875, −4.375, 6.5; se 3.21130814466628, 3.35099487714718, 3.45205252953466; z −3.38647040710263, −1.30558241966773, 1.88293774338254; p_value 0.000707979117289302, 0.191694602051888041, 0.059708805474060947; `holm` 0.00212393735186791, 0.19169460205188804, 0.11941761094812189; `bonferroni` 0.00212393735186791, 0.57508380615566412, 0.17912641642218285; `sidak` 0.0021224340034397, 0.4718875119121219, 0.1686438624061588 |
| `kruskal_wallis` with ties, `dunn` (M1, M2, M3) | estimate −7.41666666666667, 1.25, 8.66666666666667; se 3.12757830145722, 3.66937129686895, 3.59010987142300; p_value 0.0177219531548760, 0.7333612014980047, 0.0157767563889281; `holm` 0.0473302691667842, 0.7333612014980047, 0.0473302691667842; `bonferroni` 0.0531658594646279, 1, 0.0473302691667842 |
| `chi2_independence`, `cells` (T32) | statistic 8.8888888888888893, df 2; adj_residual ±2.828427124746190 (row x), ∓0.942809041582063 (y), ∓2.108185106778920 (z); p_value 0.00467773498104727, 0.34577858615116025, 0.03501498101966251; `holm` 0.0280664098862836, 0.6915571723023205, 0.1400599240786500; `bonferroni` 0.0280664098862836, 1, 0.2100898861179750 |
| `chi2_independence`, `pairs` (T32), pairs x–y, x–z, y–z | with correction: statistic 3.645, 6.75223214285714235, 0.520833333333333; p_value 0.0562378038772762, 0.00936304763753393, 0.470486422058790; `holm` 0.1124756077545524, 0.0280891429126018, 0.4704864220587896; `bonferroni` 0.1687134116318286, 0.0280891429126018, 1; `sidak` 0.1594032026498519, 0.0278269637565374, 0.8515325317798260. Without: statistic 4.5, 8.03571428571428648, 0.925925925925926; p_value 0.0338948535246893, 0.00458639208025349, 0.335923813151736; `holm` 0.0677897070493785, 0.0137591762407605, 0.3359238131517357 |

### The distributions

| What | Expected |
|---|---|
| Quantiles | q_t(0.975; 7) 2.36462425159278; q_t(0.975; 13) 2.16036865646279; q_F(0.95; 2, 18) 3.55455714566179; q_chi²(0.95; 2) 5.99146454710798; q_norm(0.975) 1.95996398454005 |
| Tails | P(T₁₂.₃ ≤ −4.5) 0.000341926886228589; P(T₅ ≥ 40) 9.20598108588648e-08; P(F₂,₁₈ ≥ 9.1) 0.00185812707328192; P(χ²₃ ≥ 200) 4.21854110719204e-43 |
| Large degrees of freedom | P(T ≤ −2.5), df 10⁹: 0.0062096654052012585; P(T ≤ −12), df 10⁹: 1.7764914488705686e-33; P(F ≥ 3), df 2 and 10⁹: 0.049787068815947556; P(F ≥ 1.2), df 199 and 10⁹: 0.028160977591396633; q_t(0.975; 10⁹) 1.9599639869123249; P(T ≤ −2.5), df 10⁶: 0.0062097447510816167 |
| Studentized range, P(Q ≤ q; k, df), SciPy, within an absolute 2e-10 | (3.5; 3, 18) 0.94177531240136; (1; 2, 5) 0.48891591956972; (4.2; 5, 10) 0.91785442462170; (2; 10, 3) 0.13287846948945; (6; 20, 60) 0.98966494238124; (3.3; 3, 11.867) 0.91040711139581; (0.3; 3, 2) 0.02420479824609; (12; 8, 4) 0.99135198682789; (4; 12, 30) 0.78158394507050; (3.9; 20, 1000) 0.61295939541927; (50; 3, 1) 0.97300040851523; (7; 30, 15) 0.97610409142616 |
| Studentized range, quantiles, SciPy, within a relative 1e-9 | 0.95; 3, 18: 3.609303828706538; 0.95; 5, 10: 4.6542929978545375; 0.99; 3, 11.867: 5.058114579318116; 0.95; 20, 4.4: 8.752534417194683 |

Places where the reference needed care, so that nobody «fixes» the code to match a tool:

- **Kolmogorov–Smirnov, asymptotic, (A, C)**: R prints 0.358087601395282 because it stops
  the series early; the two forms of the series, summed to the end, both give
  0.358081192617015. The spec's value is the second. SciPy's `ks_2samp(method = "asymp")` is
  another approximation again (0.333 there): not a reference.
- **Kolmogorov–Smirnov's D− on (A, B)**: R prints −5.6e-17 (a difference of two doubles); the
  engine's whole numbers give 0.
- **Dunn**: base R has no function for it; the references are the formula written in R on
  R's `rank`, equal to the spec's scikit-posthocs values to the last digit.
- **Normality, Pratt's zeros**: base R has neither; SciPy's `normaltest`, `jarque_bera` and
  `wilcoxon(zero_method = "pratt")` are the references.
- **Ties and small samples**: R 4.6 computes an exact p-value in the presence of ties and
  zeros; this spec, as SciPy, uses the normal approximation there. The references with
  ties were taken with R's `exact = FALSE`.
- **The studentized range in R**: `ptukey` and `qtukey` are exact to some 1e-7 only
  (`TukeyHSD`'s bounds differ from SciPy's in the eighth digit). The references are
  SciPy's.
- **The Wilcoxon statistic**: SciPy prints min(w_plus, w_minus); this spec writes
  `w_plus` and `w_minus`.

## Testing strategy

- **Distributions** (`backend/tests/test_stats_dist.py`, no engine): the quantiles, tails,
  large degrees of freedom and the studentized range above; the exact distributions; the
  accuracy contracts.
- **Reference values** (`backend/tests/test_stats_reference.py`): every row of the tables,
  on every engine, with the `ENGINES` list and fixtures of `test_data_correctness.py`
  (Polars, DuckDB, chDB always; ClickHouse with `CLICKHOUSE_TEST_HOST`, which the CI sets;
  BigQuery with `BIGQUERY_LIVE=1`). Tolerance: relative 1e-9 (looser where the table
  says); counts and texts exact.
- **Equality between engines** (`backend/tests/test_stats_parity.py`): one table of 5,000
  rows made from a fixed seed — five groups, ties, NULLs, NaN, −0.0, a column offset by
  1e9 (the variance case), a date and a boolean as groups — every test with every post-hoc
  on every engine, the whole output compared row by row with Polars': `key`, `text` (the
  `engine` row aside), counts exact, numbers within 1e-9.
- **A second witness on chDB**: `studentTTest`, `welchTTest`, `mannWhitneyUTest` on the
  same table against the nodes' p-values (asymptotic variants), within 1e-6; never on the
  column offset by 1e9, where ClickHouse's own tests answer `(nan, 0)`.
- **Parameters** (`backend/tests/test_stats_params.py`): every refusal of «The tests» and
  of «Limits», with its sentence; an unknown key; a column that is not a number; a post-hoc
  its test's variant does not allow.
- **The catalogue** (`backend/tests/test_stats_catalog.py`, `.github/scripts/stat_catalog.py
  --check`, `frontend/tests/stat-tests.test.mjs`, `gateway-go/internal/statcatalog`): the
  snapshot is what `catalog.py` says; the copies are the snapshot; every test is registered
  on the four engines, has a layout, its words in five languages in both text files, its
  line in the documentation.
- **Schema**: the ten columns, names and types, on every engine, with and without
  `detail = report`; `ord` 0…N−1; `key` unique.
- **Report engine** (`backend/tests/test_reports_render.py`, `tests/report_samples.py`):
  the three kinds and the chart filter render; `render.test.mjs`.
- **Layouts** (`backend/tests/test_stats_layouts.py`): every test's layout, on its
  reference data, in the five languages, passes the check and renders; a golden PDF of the
  two-sample layout; the sample line is on the page when the chain carries a marked
  sample (a `limit`, a `sample`, one in a branch) and absent when it carries none; the
  second render of the same chain reads no data (the rows are remembered).
- **Gateway** (`go test ./internal/...`): `POST /tasks/stat-report` — a chain the person
  may not read (403), a last step that is not a test (422), the chain reaches the engine
  as it was sent, the owner is the person; `statcatalog`, `dbtexport`,
  `flow_shortcuts`.
- **Interface** (`npm test`): `stat-tests.test.mjs` (the fields per kind, completeness,
  the options that exclude each other), the palette's grouping and search, the Report tab's
  states (which node shows it, stale, the cache's key), the five catalogues complete
  (`i18n.test.mjs`), `flow-shortcuts.test.mjs`.
- **In the browser**, on kind and on Compose, Chrome and Firefox: find a test in the
  palette by search and drop it; configure it with mouse and with keyboard alone; open
  Report; the numbers on the page are those of the Table tab, with and without a
  development sample on the source; download the PDF and the Excel; the same flow on
  DuckDB, Polars and chDB shows the same numbers on the page; a person who may not read the source gets no
  page.
- **Regression**: the engine, gateway and interface suites unchanged; the report and
  dashboard browser checks (their validator and designer are not touched).

## Commands

```bash
# engine and gateway suites, in the CI images (one at a time on this machine)
docker build --build-arg WITH_DEV=true --build-arg WITH_CHDB=true -t tabularia-backend:ci backend
.github/scripts/ci_tests.sh engine tabularia-backend:ci
.github/scripts/ci_tests.sh gateway tabularia-gateway:ci
# one module of the engine suite while working
docker run --rm -v "$PWD/backend:/app" -w /app tabularia-backend:ci python -m pytest tests/test_stats_dist.py -q
# the catalogue: the snapshot, then its copies and their check
docker run --rm --user "$(id -u)" -v "$PWD/backend:/app" -w /app tabularia-backend:ci python -m app.engine.stats.catalog
python3 .github/scripts/stat_catalog.py && python3 .github/scripts/stat_catalog.py --check
# Go gateway
cd gateway-go && go test ./internal/...
# interface
cd frontend && npx nuxi prepare && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
cd frontend && npm i --no-save @intlify/message-compiler@9.14.5 && npm test
cd frontend && node --test chart-renderer/render.test.mjs
# the charts' samples, after a new kind
cd backend && python -m tests.report_samples ../frontend/chart-renderer/presets.sample.json
# the assistants' texts, after ai_flow_agent.py or the catalogue change: the docker
# commands in the headers of gateway-go/internal/aiprompts/gen_prompts.py and internal/mcp/gen_tools.py
# settings reference, site
python3 .github/scripts/config_reference.py --check
cd website && npm run generate && npm run check-links
```

## Boundaries

- **Always**: every pass over rows by the engine's own expressions; one scalar module for
  every engine; the output's ten columns unchanged between engines; a test declared in the
  catalogue only; rights checked on the server for every chain a report is drawn from; a
  test and its report on the rows that reach the node, the same as its preview's; a
  development sample upstream said on the page; five languages; English code and
  documents, no competitor named; commits by piece with the suites green.
- **Ask first**: a new dependency (none is needed); any change to what an existing
  operation, chart kind or endpoint returns; the new chart kinds in saved reports or
  dashboards; using ClickHouse's own test functions for results; publishing, tagging,
  releasing.
- **Never**: a switch between a sample and all the rows on a statistics node or on its
  report; rows of the data in pandas, numpy or SciPy; a report definition taken from
  the browser by the new route; a function of the Python gateway outside the AI service; a
  render at each keystroke; the 1.4 line touched.

## Success criteria

1. A person finds a test in the palette, drops it after any step, and the preview shows
   its table; the eighteen tests and their post-hoc give the reference values on every
   engine available.
2. The same flow on two engines gives outputs equal as «Equality between engines» defines.
3. A wrong setup (three groups for a two-sample test, a text column as a measure, a group
   of one row, Tukey with unequal variances) stops with a sentence that says what to change.
4. With a statistics node selected, **Report** shows in the editor the pages of its test:
   the charts of its layout and every number of the formula; redoing the formula by hand
   with those numbers gives the statistic printed. They are the numbers of the Table tab;
   when the source has a development sample, the page says so.
5. **PDF** downloads the same pages; **Excel** their tables. Asking them after the pages
   runs no query on the data.
6. A person who may not read what the chain reads gets no report.
7. Adding a test changes the catalogue, its computation, its layout, its words and its
   documentation, and no other list by hand: the checks say what is missing.
8. No new dependency is in `pyproject.toml`, `package.json` or `go.mod`.
9. Flows, reports and dashboards that existed behave as before.

## Pieces

One heavy job at a time: a piece builds an image or runs a suite only after the one before
it has finished. Each piece ends with a commit and its own check green.

### Lot 1 — the vertical slice: the two-sample t test, from the engine to the report in the editor

| Piece | Objective | Files | Accepted when | Check |
|---|---|---|---|---|
| 1.1 Engine (**done**, a0c50f06) | `stat_t_two_sample` (Welch and Student) on the five engines, `detail = results`, registered from the catalogue | `backend/app/engine/stats/` (`catalog.py` with one test; `dist.py`: normal, t, `betainc`, `lbeta`, the quantiles by their tails; `aggregates.py`, `polars_source.py`: A1, A2 centred; `compute.py`, `output.py`); the four registries; `rows()` on the three SQL contexts; `tests/test_stats_dist.py`, `test_stats_reference.py`, `test_stats_parity.py`, `test_stats_params.py` | Done: the references pass on Polars, DuckDB, chDB and ClickHouse 24.8; parity on 5,000 rows; chDB's `welchTTest` and `studentTTest` agree; BigQuery's queries written and parsed, not run | `ci_tests.sh engine` |
| 1.2 Catalogue and editor (**done**, a51c6490) | The test in the palette's Statistics group with a form made from the catalogue, in five languages; every list fed or checked. Until then the palette shows `stat_t_two_sample` among the transformations, bare: this piece takes the `stat_*` names out of that list first | `catalog.py` (`family`, `KINDS`, `FAMILIES`, the snapshot's writer), `catalog.json`, `tests/test_stats_catalog.py`; `.github/scripts/stat_catalog.py`, `workflows/stat-catalog.yml`, the three copies; `gateway-go/internal/statcatalog/`, `flow_shortcuts.go`, `payload.go`, their tests; `OpSidebar.vue`, `useOpIcons.ts`, `StatTestForm.vue`, `statTests.ts`, `NodePanel.vue`, `useFlowModel.ts`, `opSummary.ts`, `previewGrid.ts`, the five `.gen.ts`, `tests/stat-tests.test.mjs`, `flow-shortcuts.test.mjs`; `tests/test_dbt_export.py` | The node is found by search, dragged, configured and previewed by hand on the running stack; a flow with it refuses the dbt export with «The operation «stat_t_two_sample» cannot be exported to dbt»; a second test added to the catalogue in a scratch branch appears in the palette with a working form after the one command, and the checks name the words it lacks | `ci_tests.sh engine`, `stat_catalog.py --check`, `vue-tsc`, `npm test`, `go test ./internal/...` |
| 1.3 Report rows (**done**, 08f4bd8a) | `detail = report` for the slice (refused with a sentence today): quartiles, histogram, curve, critical region | `aggregates.py`, `polars_source.py` (A12, A13), `compute.py`, `dist.py` (`t_pdf`), the tests | The `hist` rows of A and B are 4, 2, 2, 0, 0 and 0, 0, 2, 3, 2 over five intervals from 4.9 to 8.0; the quartiles above; the curve has 201 points and integrates to 1 within 1e-3 over its range | `ci_tests.sh engine` |
| 1.4 Charts (**done**, 82f55e97) | `boxplot`, `interval`, `density` and the chart `filter`, in the report engine | `reports/definition.py`, `model.py`, `charts.py`, `tests/report_samples.py`, `tests/test_reports_render.py`, `presets.sample.json`, `render.test.mjs` | A definition with one chart of each kind and a filter renders to PDF and SVG; the renderer draws 17 presets; the existing golden PDFs are unchanged | `ci_tests.sh engine`, `node --test chart-renderer/render.test.mjs` |
| 1.5 The report of a step (**done**, a9e80eda engine, 41e13e41 gateway) | The two-groups layout and the route that draws it | `stats/layouts.py`, `report_texts.json`, `tests/test_stats_layouts.py`; `api/routes/reports.py` (`/reports/stat`), `tasks/report_jobs.py` (`render_stat_report_task`, the remembered rows); `gateway-go/internal/api/proxy.go` (`/tasks/stat-report`), `server/go_only_routes.go`, their tests | A request for the reference chain answers a task whose SVG pages hold the Welch numbers of «Reference values»; the PDF asked next runs no query; the page carries the sample line when the chain has a marked sample and not otherwise; the gateway's refusals of «The route» | `ci_tests.sh engine`, `go test ./internal/...` |
| 1.6 The Report tab (**done**, fc61bf0c; fixes 55894d22, a0981552, 8d7af5ec) | The report in the editor, on the rows of the node's preview, with its downloads; no control over the rows | `FlowEditor.vue` (the tab, the stale mark, the cache), `components/ui/StatReportView.vue`, `composables/statReport.ts`, the five `.gen.ts`, `tests/stat-report.test.mjs` | Success criteria 4, 5 and 6 on the sample data, in Chrome and Firefox, on kind and on Compose | `vue-tsc`, `npm test`, the browser check |

### The other lots

Each is «engine, then catalogue entry, words and layout»: two pieces, with the checks of
1.1 and of 1.2 + 1.5; accepted when its rows of «Reference values» pass on every engine,
parity passes, the catalogue's checks are green and its report opens in the editor.

| Lot | Operations | Adds |
|---|---|---|
| 2 Means and variances (**done**, a10a1aec `dist.py`, 321b3cb2 the tests, eacb760d the significance codes) | `stat_t_one_sample`, `stat_t_paired`, `stat_anova` with `tukey`, `games_howell`, `pairwise_t`, `stat_var_f`, `stat_levene`, `stat_pearson` | `dist.py`: F, chi-square, incomplete gamma, their quantiles and densities, held to the t's contract; **the studentized range**; the adjustments; A3, A11, centred as A2; the `pair` section; the layouts of several groups (with «Comparisons»), one sample, paired, correlation; `compute.significance` (R's stars, every test) |
| 3 Proportions and categories | `stat_prop_one_sample`, `stat_prop_two_sample`, `stat_chi2_independence` with `cells` and `pairs`, `stat_chi2_gof`, `stat_fisher_exact` | `exact.py`: binomial, Clopper–Pearson, Fisher; A7, A8; the layouts of proportions and contingency |
| 4 Ranks and distributions (**done**, 9624cb20 the exact distributions, b2b4f46d the six tests) | `stat_mann_whitney`, `stat_wilcoxon_signed_rank` (with `pratt`), `stat_kruskal_wallis` with `dunn`, `stat_spearman`, `stat_ks_two_sample`, `stat_normality` | `exact.py`: Mann–Whitney, signed rank, Kolmogorov–Smirnov (whole numbers); `dist.py`: Kolmogorov's limit; A4, A5, A6, A9 (no window partitioned by a double), and A10 centred; the layouts of two groups by ranks, signed ranks, Kruskal–Wallis with Dunn, rank correlation, normality; references of R 4.6.1 (and SciPy 1.18.1 for Pratt and the normality tests) on Polars, DuckDB, chDB and ClickHouse 24.8, BigQuery's SQL parsed |
| 5 Assistants and MCP (**done**, 8798900b) | — | `ai_flow_agent.py` from its copy of the catalogue (the operations, a generated reference line each, the generic check), `prompts.json` and `tools.json` written again; accepted when an assistant places a configured test on a draft and the gateway suite is green (`ci_tests.sh gateway`, `go test`) |
| 6 Documentation (**done**, 0cede891; `README.md`, a725b012) | — | `docs/capabilities/statistics.md` (where the tests are, each family with an example on the sample data, the post-hoc and when to use which, the output's columns, the Report tab and its downloads, what it does not do), the map, `flows.md` (the nodes in a section of their own; the counts become 19 transformations and 18 statistical tests, here, in the map and in `README.md`), `reports.md` (nothing changes for saved reports: said), `engine-differences.md` (the operations in the coverage table; «What is guaranteed identical» gains the statistics rule and its tolerance; what is computed where), `dbt-export.md`, `CHANGELOG.md`; the site (`npm run generate && npm run check-links`) |
| 7 Regression | — | The three suites whole; the browser checks of «Testing strategy» on kind and Compose, Chrome and Firefox; BigQuery live when the owner provides the run |
| 8 Save as report (optional, only if the owner asks) | — | A button of the Report tab that keeps the report as an object of the flow's folder. It needs what the first release leaves out: a dataset kind `flow_node` (`{flow_id, node_id, detail}`: the flow's chain to the step resolved by `flowresolver` in production mode, VIEW on the flow's folder and on every datasource it reads), the three chart kinds and the chart filter in `gateway-go/internal/reportdef` and in the designer (`reportDesignModel.ts`, `RdChartGlyph.vue`, `RdChartProps.vue`, `RdDatasetDialog.vue`), their refusal on dashboards, and the layout handed by the engine as a definition to save |

## Open points

For the owner:

1. **BigQuery** was checked on its documentation only: the reference and parity tests
   must run once with `BIGQUERY_LIVE=1` (a few megabytes billed per test).
2. **The version** that carries it, and when.

Found on the way, outside this work:

- The Filter node's `gt` keeps the rows whose value is NaN on Polars and DuckDB, and drops
  them on chDB and ClickHouse: a difference between engines that
  `docs/engines/engine-differences.md` does not list.
- `group_by` and `pivot` on the ClickHouse engines compute `std` and `var` with
  `stddevSamp` and `varSamp`, which are not stable: on values around 1e9 with a spread of
  1 the variance comes out 0 on chDB 4.2.1 and 0.3641 on DuckDB. `stddevSampStable` and
  `varSampStable` would make the engines agree; it changes existing numbers, so it is a
  decision of its own.
