# Spec: the priors' preview in the browser (`prior-preview`)

Status: **written and approved by the owner on 2026-11-23.** It specifies the module `prior-preview` of
`docs/specs/bayesian-editor-map.md` (**the map**), step 2 of its order of construction, beside `bayes-contract`
(`bayes-contract.md`) and `stan-codegen` (`bayes-stan-codegen.md`), after the approved
`bayes-distribution-catalog.md` (**the catalogue spec**), `bayes-expression-language.md` (**the language spec**) and
`bayes-model-graph.md` (**the graph spec**). Nothing of it is built. Decisions marked **(owner)** are the owner's (the
map's «The owner's decisions» and «Interview answers», numbered as there: **the answers**); the rest are this spec's,
to confirm; those marked «spec decision, 2026-11-23, owner may revisit» are small choices the owner may change
without reopening the module. The owner answered this spec's question on 2026-11-23 («Decisions of
2026-11-23»); no question is open.

Words: **verified** = read in this repository's code or in the approved specs on 2026-11-23; **measured** = run on
2026-11-23 under Node 22.23.1 on the development machine (a 4-core laptop CPU from 2018), in a scratch directory
outside the repository with twelve of the catalogue's stdlib packages installed alone, deleted afterwards;
**assumed** = not run here, checked by the lot named beside it.

## Objective

While a person types a prior's numbers in the editor's inspector, the chart beside them shows **the distribution
those numbers imply for that quantity** — and, when the prior's arguments are themselves parameters with priors (a
hierarchy), **the true marginal**: the hyperparameter drawn first, then the parameter (the owner's decision,
«Interactive priors»). It is:

- **computed in the browser**, in a **Web Worker**, with the catalogue's bindings to stdlib (answer 2); **no Job, no
  request** to the gateway;
- **only the marginals of the priors** (answer 13): no data, no likelihood, no prior predictive — that is a Job of
  `predictive-checks`;
- **seeded**: the same graph and the same seed give the same chart; «Draw again» changes the seed;
- **light**: a fixed number of draws into arrays allocated once (the rule of 2026-11-22, in the small);
- **explained** for a reader who does not know Bayesian statistics (answer 1): what the chart shows, in five
  languages, as facts and never as judgements.

Who reads it: the person drawing a model (`graph-editor` shows it); `predictive-checks`, whose golden test compares
this module's draws with Stan's `fixed_param` draws of the same graph (the map, «Why the two predictive checks are
where they are»).

## Decisions

| Question | Decision | By |
|---|---|---|
| Where | **The browser, a Web Worker**, no Job | Owner («Interactive priors») |
| What | **Only the marginals of the priors**; no prior predictive in the browser | Owner (answer 13) |
| Hierarchies | **Ancestral sampling** in the model's order: the hyperparameters first, so the chart is the true marginal | Owner |
| Library | The catalogue's bindings (`bayesDistributions.ts`, stdlib), one seeded MT19937 stream per preview | Owner (answer 2), the catalogue spec |
| A closed-form marginal | When every argument of the prior is a constant: the **density curve** and the exact quantiles, no draws | The map |
| A mixture | When an argument depends on a parameter: a **histogram** of the ancestral draws and their empirical quantiles | The map |
| One unit of a plate | A plate's quantity is shown as the marginal of **one unit** (the units are exchangeable: no prior depends on data) | Spec («One unit stands for its plate») |
| Draws a preview | **4,000**, a constant of the module | Spec decision, 2026-11-23, owner may revisit |
| Heavy tails | The axis is cut at the 0.5 % and 99.5 % quantiles and the cut is **written on the chart** | The map (the risk of Cauchy and Student-t with ν ≤ 2), the numbers: spec decision, 2026-11-23, owner may revisit |
| Transformed values | **Shown** when they depend on no data on any path (`inv_logit(a)`, `exp(log_sigma)`): ancestral draws, the expression evaluated on each draw | Owner (2026-11-23) |
| Elementary functions in expressions | JavaScript's `Math` (`exp`, `log`, `log1p`, `expm1`, `sqrt`) and stable formulas of our own for `inv_logit`, `logit`, `log1p_exp`, `log_inv_logit`; bit-identity across browsers is **not** promised for them (only for stdlib's draws), the chart being the same to the eye | Spec decision, 2026-11-23, owner may revisit (stdlib's own `math-base-special-*` would be bit-identical but are new direct dependencies: «Ask first» of the catalogue spec) |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| Densities, quantiles, seeded draws | The catalogue spec's `frontend/composables/bayesDistributions.ts`: `Binding{pdf, logpdf, cdf, quantile, draw(rng, args, out, lower?, upper?)}` for the nine prior distributions; one MT19937 handed to every factory through `prng`, its `state` saved and restored; truncated draws by inversion; LKJ's correlation as 2·Beta(b, b) − 1 with b = η − 1 + K/2; the multivariate normal's component k as normal(μ_k, τ_k); the caller's preallocated `Float64Array`; **imported only by this module's worker and by the tests** | Every number of a distribution comes from there; nothing restated |
| Bounds and truncation | The catalogue's «Bounds and truncation», rule 3: the browser draws a truncated prior by inversion, u uniform on (F(L), F(U)), x = Q(u); when F(U) − F(L) is 0 in double precision it draws nothing and says so | Applied as it is |
| The validated graph in the browser | The graph spec's mirror `frontend/composables/bayesGraph.ts` (the same rules and codes as Go, after each change, never trusted for a program); its validated model has `Order` (a topological order, ties by id: «`prior-preview` draws the parameters in `Order`»), the ranges and truncations of `Params` | This module reads a **prior plan** the mirror exports («The plan»); a small addition to the mirror, proposed as a correction to the graph spec |
| Typed expressions in the browser | The language spec's mirror `bayesExpr.ts`: the same parser and type rules, the AST's JSON (`{"op"}`, `{"num"}`, `{"neg"}`, `{"call"}`, `{"index"}`, `{"lag"}`, `{"name"}`), no emitter | This module evaluates the ASTs of prior slots over the draws |
| A worker loaded lazily | `frontend/composables/useMonaco.ts`: `await import('monaco-editor/esm/vs/editor/editor.worker?worker')` then `new EditorWorker()`, loaded the first time an editor opens, never with the page | The same Vite `?worker` import, the first time a prior's inspector opens |
| Charts | `echarts` 6.1 and `vue-echarts` 8.0 (`frontend/package.json`) | The curve and the histogram (`graph-editor` draws them) |
| Tests of the app's TypeScript under Node | `frontend/tests/alias-hooks.mjs`, `npm test` = `node --experimental-strip-types --test tests/*.test.mjs`, Node 22 in CI | The worker's core is a plain module the tests import |

## What is shown, and for which node

| Node (the graph spec) | Shown |
|---|---|
| `parameter` (a scalar, a group plate, a levels plate) | the marginal of its value (of one unit for a plate) |
| `correlated_effects`, a component | the marginal of that component for one unit: normal(mean, scale), mean and scale drawn first when they are parameters |
| `correlated_effects`, the correlation | the marginal of **one correlation** of the K × K matrix: 2·Beta(b, b) − 1, b = η − 1 + K/2, η a constant (closed form) |
| `state` | the marginal of its **first** value (its `init`) and of its `scale`; the later values spread further with the series' length, which the model's space does not know (no data, answer 6): the panel says so in a sentence |
| `transformed` depending on no data on any path | the marginal of its value: its ancestors drawn, its expression evaluated on each draw (owner, 2026-11-23); for a plate, of one unit |
| `data`, `linear_predictor`, `likelihood`, `transformed` depending on data | nothing: they need data, which only the flow node binds (answer 6); the panel says that the prior predictive check, a Job run from the flow node, is where they are seen (answer 13) |

### One unit stands for its plate

Priors never depend on data (the graph spec, `graph.prior_data`), so the units of a plate are **exchangeable**:
before any data, every school has the same prior given the hyperparameters. The marginal of `alpha_school` is the
marginal of `alpha_school` for **one** school, and the preview draws one representative unit per plate. An index in a
prior's expression (`mu_district[district]`, lifted from schools to districts) points to the representative unit of
its target plate: the school's district is drawn once per draw, from its own prior, and the school from it — the
true marginal of one school, nested levels included. A levels plate (a category's coefficients) is shown for one
non-reference level.

## The plan

The worker receives a **plan** (exported by the graph mirror as `priorPlan(model)`, plain JSON, recomputed after each
change the mirror validates):

```ts
interface PriorPlan {
  order: string[]                       // the nodes the target needs, ancestors first, in Model.Order
  nodes: Record<string, PlanNode>
}
interface PlanNode {
  id: string
  kind: 'parameter' | 'component' | 'correlation' | 'state_init' | 'state_scale' | 'transformed'
  dist?: string                         // a catalogue id with browser ≠ none
  args?: Record<string, PlanSlot>       // each argument: a typed AST, a reference, or a constant
  range?: { lower?: number, upper?: number }   // the support ∩ the bounds (the graph spec)
  truncated?: boolean                   // the range narrower than the support
  value?: PlanSlot                      // a transformed node's expression
  k?: number, eta?: number              // a correlation's size and LKJ's eta
  errors: number                        // the mirror's error findings on this node or an ancestor
}
type PlanSlot = { ast: unknown } | { ref: string } | { constant: number }
```

A node with `errors > 0` is not drawn: the panel says «fix the messages above to see this prior». The plan holds no
data, no label and nothing a program depends on; it never leaves the browser.

## How a marginal is computed

For the target node T:

1. **Closed form** — when every argument of T's prior is a constant (a number, or an expression of numbers only):
   - the curve: 200 points evenly spaced between the axis' ends, the binding's `pdf` divided by the prior's mass on
     the range when truncated (the catalogue's rule 2);
   - the quantiles 5 %, 50 %, 95 %: the binding's `quantile`, of the truncated distribution when truncated
     (Q(F(L) + p·(F(U) − F(L))));
   - the axis' ends: the 0.5 % and 99.5 % quantiles (of the truncated distribution), widened to the range's bounds
     when those are closer.
   No draw is made; nothing depends on the seed.
2. **Ancestral draws** — otherwise: for i = 1 … 4,000, every node of `order` is drawn in turn from its prior with its
   arguments evaluated **on draw i** (the hyperparameters' values of the same draw), then T. Concretely, node by node
   over whole arrays (each node's 4,000 draws, then the next): an argument's AST is evaluated element-wise over the
   ancestors' arrays, and the binding draws the node's array with per-draw arguments. Truncated nodes draw by
   inversion with **each draw's own** F(L) and F(U) (their arguments change with the draw).
   - The histogram: 60 equal bins between the empirical 0.5 % and 99.5 % quantiles of T's draws; the draws beyond
     are counted and the chart says «0.5 % of the draws on each side are beyond the axis» (with the counts).
   - The quantiles 5 %, 50 %, 95 %: empirical, the linear interpolation between order statistics (Hyndman and Fan's
     type 7, as NumPy's default; `bayes-contract` fixes the same definition for the summary's `q5`…`q95`, which
     `bayes-diagnostics` computes, so the preview and the report say quantiles the same way).
3. **Correlations**: always closed form (η is a constant, the graph spec).

**Draws Stan would reject.** An argument whose value breaks its constraint on draw i (a scale `a - b` that is not
positive: the graph spec's `graph.slot_maybe`), or an expression that is not finite there (`log` of a negative), makes
that draw — and the draws of T that depend on it — **left out**; the chart says «312 of 4,000 draws were left out:
Stan rejects a draw where the scale is not positive». A truncation with no mass on draw i (F(U) − F(L) = 0) is left
out the same way (the catalogue's rule 3). Nothing is clipped or repaired.

**What the panel writes** (five languages, `bayes.preview.*`, facts only, for a reader who does not know Bayes —
answer 1): «Before seeing any data, the model says that the mean of a school is between −19.6 and 19.8 nine times in
ten; half the time it is below 0.1.»; for a mixture, «drawn 4,000 times: first the mean and the spread of the
schools, then a school from them»; for a truncation, «the prior is cut at 0: only positive values». No word such as
«wide», «vague», «informative» or «reasonable» is written (the rule of the words, `docs/specs/report-explanations.md`:
a measure is said as a number).

## Expressions in priors

Prior slots hold typed expressions of the language (answer 3). The worker evaluates their ASTs over arrays:

| AST | Value on draw i |
|---|---|
| `{"num"}` | the number |
| `{"name"}` | the node's draw i (its representative unit's, for a plate) |
| `{"index": x, "by": g}` | x's representative unit's draw i (see «One unit stands for its plate») |
| `{"neg"}`, `{"op": "+" "-" "*" "/" "^"}` | IEEE double arithmetic in JavaScript; `^` is `Math.pow` |
| `{"call": f}` | `exp`, `log`, `log1p`, `expm1`, `sqrt` by `Math`; `square` as x·x; `inv_logit`, `logit`, `log1p_exp`, `log_inv_logit` by stable formulas (`log1p_exp(x)` = x + log1p(exp(−x)) for x > 0, log1p(exp(x)) otherwise; `inv_logit` through `exp(−|x|)`) |
| `{"lag"}` | never in a prior (`lag` lives on series rows, and no parameter is per row: the graph spec) |

An expression is evaluated into temporary arrays taken from a pool allocated once (at most `max_expression_nodes`,
256, arrays of 4,000 doubles: 8 MB at worst, the catalogue's limit). Evaluation order is the AST's, left to right, the
same in every browser.

## The worker

`frontend/workers/bayesPrior.worker.ts`, loaded with Vite's `?worker` import the first time a prior's inspector opens
(the pattern of `useMonaco.ts`, verified), never by the entry chunk; it imports the core
(`frontend/composables/bayesPriorCore.ts`, plain TypeScript with no browser API, so the tests import it under Node)
and the bindings.

**Messages** (structured clone, no shared memory):

```ts
// page → worker
{ id: number, plan: PriorPlan, target: string, seed: number }
// worker → page
{ id: number, target: string, kind: 'curve' | 'histogram' | 'none',
  x?: Float64Array, y?: Float64Array,                     // the curve, or the bins' edges and counts
  quantiles?: { q5: number, q50: number, q95: number },
  cut?: { below: number, above: number },                 // draws beyond the axis
  leftOut?: { count: number, reason: string },            // draws Stan would reject
  none?: 'needs_data' | 'errors' | 'no_mass',
  ms: number }                                            // the time the worker took, for the measurements
```

- **Debounced** by the page: a request 150 ms after the last change of a number or an expression (spec decision,
  2026-11-23, owner may revisit).
- **Only the latest answers**: the worker keeps one pending request; a newer one replaces it before it starts; the
  page drops an answer whose `id` is not its latest. A slow redraw never shows over a newer one.
- **Memory**: one `Float64Array(4000)` per node of `order`, from a pool allocated once and grown only when a plan has
  more nodes than any before (at most `max_nodes`, 200: 6.4 MB); the expression pool above; no array is allocated per
  draw. The curve's and the histogram's arrays are transferred to the page, not copied.
- **Seed**: 1 for a new preview; «Draw again» adds 1; the seed is the stream's MT19937 seed (the catalogue spec). The
  nodes of `order` consume the stream in `order`, each its 4,000 draws (and two uniforms more per truncated draw by
  inversion), so a chart depends only on the plan and the seed.
- **Errors**: a binding or an evaluation that throws answers `kind: 'none'` with the error logged for the console,
  never an unhandled exception in the worker.

### Response times (measured, 2026-11-23)

Under Node 22.23.1, the stdlib packages of the catalogue's pins, 4,000 ancestral draws of a three-level chain — `mu ~
normal(0, 10)`, `tau_d ~ normal(0, 5)` truncated at 0 (by inversion), `mu_d ~ normal(mu, tau_d)`, `tau_s ~
exponential(1)`, `alpha ~ normal(mu_d, tau_s)`, five nodes:

| Path | First call (cold) | Median of 30 (warm) |
|---|---|---|
| stdlib's generators (the truncated node by inversion) | 18.2 ms | **1.9 ms** |
| every draw by inversion (`quantile` of a uniform) | 13.1 ms | **3.2 ms** |
| the histogram (sort + 60 bins) | — | 0.36 ms |
| a closed-form curve (200 pdf points) | — | 0.10 ms |

The same seed gave the same draws twice (checked in the script). On a slow laptop's browser the times will be a
multiple of these (assumed); lot P2 measures the same chain in **Chrome and Firefox** on a slow laptop (the map's
«measure first») and records them. The catalogue spec lets this module move every draw to inversion «if one code path
is worth the time»: at 3.2 ms it is — one path for truncated and untruncated draws — **if** the browsers confirm it
(lot P2 decides, and says so in «As built»).

**Target** (an engineering target, not a verdict on anything): the chart redrawn within 300 ms of the last keystroke
on that slow laptop, debounce included; recorded by P2, not asserted in CI (timings in CI are noise).

## The golden test against Stan (`predictive-checks`'s half and this module's half)

The map gives the golden test of the priors' marginals to `predictive-checks`, «which holds the Job half»; this module
gives it the browser half and the test's shape:

- **The browser half**: `drawMarginals(plan, seed, n)` in `bayesPriorCore.ts` — the same code the worker runs, for
  every quantity of a graph — called under Node in the suite.
- **The Stan half**: for each case (the ten templates of the map and the free cases of `stan-codegen`'s golden
  corpus that have priors with hyperpriors, truncations, correlations), Stan's `fixed_param` run of the same program
  in prior mode (`Mode = 1`, `stan-codegen`'s extension point, `bayes-contract`'s mode `prior_predictive`), 4 chains ×
  1,000 draws, fixed seed, run **in the group's image by a command of `predictive-checks`** and committed as a fixture
  (`frontend/tests/fixtures/bayes-prior/<case>.json`: the draws of each quantity's representative unit, with the
  CmdStan version and the seed in its header) — the suite never runs CmdStan. (Correction of 2026-11-24 from the step-4 specs): the draws are the program's
  `Prior_m_<k>` columns (`prior_marginal`, `bayes-predictive-checks.md`); a case with a truncated `gamma` or `inv_gamma`
  has no Stan half.
- **The comparison**: for each quantity of each case, a **two-sample Kolmogorov–Smirnov test** of the 4,000 browser
  draws against the 4,000 Stan draws (the asymptotic distribution of the statistic, written in the test; n and m are
  large enough for it), the p-values **Holm-adjusted over all the comparisons of the suite**, each compared with the
  suite's **alpha 0.001** (the alpha of the catalogue's own suite). With fixed seeds the test is deterministic. This is
  a test of the software (the half-normal trap of the map, a hyperprior drawn in the wrong order, a parametrization),
  not a verdict of any report.
- **Without fixtures** (this module's own lot, before `predictive-checks` exists): marginals known in closed form —
  normal(μ, τ) with μ ~ normal(0, 10) and τ a constant is normal(0, √(100 + τ²)); normal(0, √v) with v ~
  inv_gamma(ν/2, ν/2) is Student's t with ν degrees of freedom; a half-normal by bounds is the folded normal; LKJ's
  correlation is the Beta above — each compared with a **one-sample** Kolmogorov–Smirnov test against the closed-form
  CDF, Holm-adjusted, alpha 0.001.

## Tech stack

- **TypeScript** in the Nuxt 3.21 frontend; Vite's worker import; **no new dependency** beyond the catalogue's 45
  stdlib packages (the catalogue spec's lot C1 adds them).
- Tests with `node --test` and `--experimental-strip-types` under Node 22 (CI) — the core imports no browser API.

## Commands

```bash
cd frontend && npm i --no-save @intlify/message-compiler@9.14.5 && npm test        # one heavy job at a time
cd frontend && node --experimental-strip-types --no-warnings=ExperimentalWarning --test tests/bayes-prior-preview.test.mjs
cd frontend && npx nuxi prepare && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
cd frontend && node tests/bench/bayes-prior-bench.mjs        # the timings of «Response times», recorded, not asserted
```

## Project structure

```text
frontend/composables/bayesPriorCore.ts        the plan's evaluation, ancestral draws, curves, histograms, quantiles
frontend/composables/bayesPriorPreview.ts     the page's side: the worker started lazily, debounce, latest-only
frontend/workers/bayesPrior.worker.ts         the worker: messages in and out, the pools
frontend/composables/bayesGraph.ts            + priorPlan(model) (the mirror's addition)
frontend/tests/bayes-prior-preview.test.mjs   closed forms, ancestral, truncation, left-out draws, determinism, messages
frontend/tests/bayes-prior-golden.test.mjs    the comparison with Stan's fixtures (predictive-checks adds the fixtures)
frontend/tests/fixtures/bayes-prior/          the fixtures (written by predictive-checks' command in the image)
frontend/tests/bench/bayes-prior-bench.mjs    the timings
frontend/i18n/locales/*.gen.ts                bayes.preview.*
```

## Code style

A heavy part loaded the first time it is needed, never with the page (verified, `frontend/composables/useMonaco.ts`):

```ts
let loading: Promise<MonacoApi> | null = null

export function loadMonaco(): Promise<MonacoApi> {
  if (!loading) {
    loading = (async () => {
      // Vite's worker import: a file of its own under /_nuxt
      const { default: EditorWorker } = await import('monaco-editor/esm/vs/editor/editor.worker?worker')
      ;(self as any).MonacoEnvironment = { getWorker: () => new EditorWorker() }
```

This module's page side in the same voice (a sketch; lot P1 writes it):

```ts
// The worker is started the first time a prior's chart is asked for: the bindings and stdlib stay out
// of the entry chunk. Only the latest request is answered; an older answer is dropped.
let worker: Worker | null = null
let latest = 0

export async function preview(plan: PriorPlan, target: string, seed: number): Promise<PreviewAnswer | null> {
  if (!worker) {
    const { default: PriorWorker } = await import('~/workers/bayesPrior.worker?worker')
    worker = new PriorWorker()
  }
  const id = ++latest
  return new Promise((resolve) => {
    worker!.onmessage = (e: MessageEvent<PreviewAnswer>) => resolve(e.data.id === latest ? e.data : null)
    worker!.postMessage({ id, plan, target, seed })
  })
}
```

Comments are sentences that say why; names in English; no abbreviation a reader must guess.

## Testing strategy

**Small by construction**: 4,000 draws, graphs of a few nodes; no data.

- **Core** (`bayes-prior-preview.test.mjs`, under Node): the closed-form curve and quantiles against the bindings
  (truncated and not); the ancestral draws of the closed-form mixtures above against their CDFs (one-sample KS, Holm,
  alpha 0.001); the representative unit through a lifted index (schools in districts: the school's marginal equals
  the composition drawn by hand); a truncation whose bound has no mass on some draws (left out and counted); an
  argument that breaks its constraint on some draws (left out, the reason named); `log` of a negative; the cut of the
  axis on a Cauchy and a Student-t with ν = 1 (the counts beyond); the same plan and seed give the same arrays, also
  after another plan ran in between (the stream restored); a different seed gives other draws; the pools never grow
  past `max_nodes` and `max_expression_nodes` arrays; every message's shape.
- **Golden against Stan** (`bayes-prior-golden.test.mjs`): skipped, saying so, until `predictive-checks` commits the
  fixtures; then every case as «The golden test against Stan» says.
- **Words** (`i18n`): every `bayes.preview.*` key in the five languages; none of the judgement words of
  `report-explanations.md`'s list.
- **Bundle** (lot P1's acceptance): `nuxt build` puts no stdlib code and no code of this module in the entry chunk;
  the worker's chunk size recorded (the catalogue spec measured 94 KB gzip for the 45 packages).
- **By hand** (lot P2, recorded): Chrome and Firefox, on the development machine and on a slow laptop: the timings,
  the same seed giving the same chart in both, the redraw within the target, keyboard access to «Draw again» (a
  button: no new shortcut, so nothing to map for Mac or for laptops without F keys).

## Boundaries

- **Always**: the catalogue's bindings for every number of a distribution; the plan's order; seeded draws; arrays
  from the pools; the cut and the left-out draws written on the chart; texts in five languages, facts only.
- **Ask first**: another number of draws, bins or quantiles; a prior predictive in the browser (answer 13 says no); any
  dependency beyond the catalogue's packages (stdlib's own math functions included); showing a node that depends on
  data.
- **Never**: a Job or a request for a preview; data in the model's space; an unseeded draw; the worker or stdlib in the
  entry chunk; a judgement word about a prior; a clipped or repaired draw.

## Success criteria

1. For every closed-form case the curve and the quantiles equal the bindings' (to the last bit: the same calls).
2. The ancestral draws of the closed-form mixtures pass their one-sample tests (Holm, alpha 0.001) with the fixed
   seeds.
3. Once `predictive-checks`' fixtures exist, every quantity of every case passes the two-sample test against Stan's
   `fixed_param` draws (Holm, alpha 0.001).
4. The same plan and seed give the same chart in Chrome and in Firefox (lot P2's manual check, recorded); the timings
   of P2 are recorded beside the Node ones above.
5. No stdlib or preview code in the entry chunk; no new package in `package.json` beyond the catalogue's.
6. Nodes that need data are never drawn, and say why.

## Lots

| Lot | Objective | Files | Accepted when | Agent |
|---|---|---|---|---|
| **P0** | The core: the plan's evaluation, closed forms, ancestral draws, truncation, left-out draws, cut, quantiles; `priorPlan` in the graph mirror | `bayesPriorCore.ts`, `bayesGraph.ts` (addition), `bayes-prior-preview.test.mjs` | core tests green | sonnet |
| **P1** | The worker and the page's side, the words in five languages, the bundle check | `bayesPrior.worker.ts`, `bayesPriorPreview.ts`, locales | tests and `vue-tsc` green; entry chunk without stdlib | sonnet |
| **P2** | The timings in Chrome and Firefox (development machine and a slow laptop), the choice of one draw path | the bench, «As built» | recorded | haiku (by hand, with the owner's laptop) |
| **P3** | The golden test against Stan, with `predictive-checks`' fixtures | `bayes-prior-golden.test.mjs` | criterion 3 | sonnet (after `predictive-checks`) |

P0 needs the catalogue's C1 (the bindings) and the graph spec's M4 (the mirror); P1 after P0; P2 after P1 (and the
canvas of `graph-editor` to see the chart in place, or a bare page for the measure); P3 after `predictive-checks`.
None is heavy except a `nuxt build`.

## Decisions of 2026-11-23 (the answers to this spec's questions)

| Question | Decision | By |
|---|---|---|
| Transformed values in the preview | **Yes**: a `transformed` node with no data on any path is shown like a parameter (a log-odds prior seen as probabilities); nodes that need data stay out, as answer 13 says | Owner, 2026-11-23 |

## Correction of 2026-11-24 from the step-4 specs

From `bayes-stan-compile.md`, `bayes-diagnostics.md` and `bayes-predictive-checks.md`, approved by the owner on
2026-11-24. Where this section and the text above differ, this section holds.

1. **The browser half's name** is `bayesDrawMarginals(plan, seed, n)` (verified in `bayesPriorCore.ts`), the
   `drawMarginals` of «The golden test against Stan».
2. **The Stan half** reads the `Prior_m_<k>` columns (`prior_marginal`: one ancestral draw a draw of each quantity node,
   a plate's representative unit) that `predictive-checks` writes in every program; its lot P4 writes the fixtures.
3. **A truncated `gamma` or `inv_gamma`** prior has no Stan half (Stan 2.40 has no quantile for them, assumed): its case
   is compared only with the closed forms of «Without fixtures».

## Open questions

None.

## As built — lot P3 (the golden test against Stan)

`frontend/tests/bayes-prior-golden.test.mjs`, `frontend/tests/fixtures/bayes-prior/<case>.json` (17 fixtures, about 4 MB),
`gateway-go/cmd/bayesfixtures` (`prepare`), `gateway-go/scripts/bayes-prior-fixtures.sh` and `gateway-go/scripts/bayes_prior_fixtures.py`.

- **The command differs from `bayes-predictive-checks.md`'s sketch**: `go run ./cmd/bayesfixtures prepare <dir>` writes, for each case,
  the program a save stores for the graph **as it is now** (`bayescodegen.Generate` with `bayeschecks.Extension{}`) and the manifest's
  quantities; the shell script starts one container per case (the image `tabularia-bayes-b0:local`, CmdStan 2.40.0, read-only root,
  no network, uid 10001, 2 GiB, 2 CPUs, `/tmp` as an executable tmpfs) that runs `bayes_prior_fixtures.py`: it writes a minimal data
  file read from the program's own data block (`Mode` 1, the checks off, no row named, 6 rows, 2 units a group plate, 3 levels a
  category, a state's six rows chained), compiles as the Job does (`PRECOMPILED_HEADERS=false`), runs `fixed_param` (`init=0`,
  4 chains × 1,000 draws, seed 4100 + the case's place) and keeps only the `prior_marginal` columns, as CmdStan wrote them
  (`collect` is that script's last step: there is no second command). About 20 s a case, 6 minutes for the 17.
- **The cases** (17): the ten templates, `hier_slopes_centred`, `ts_local_level_centred`, `free_truncated_hyper`,
  `free_explicit_noncentred`, `free_category`, and the checks module's own `truncated` (every truncation form, single and on a plate)
  and `effects3` (three correlated effects). 84 comparisons.
- **The comparison** is as «The golden test against Stan» says: two-sample Kolmogorov–Smirnov, asymptotic, Holm over the 84
  comparisons, alpha 0.001. A draw Stan wrote as `nan` or `inf` and a draw the browser rejects are left out on both sides
  (`truncated`: 3,822 of 4,000 for the two quantities that sit under an infinite-mass truncation). Stan's marginal of a state is
  its first value, compared with the plan's `<state>.init`. Smallest unadjusted p-value of the file: 2.5e-2 (`truncated`, `vg`);
  every adjusted p-value is 1.
- **A defect found, not in the browser**: the golden programs `bayeschecks/testdata/golden/checks_<template>.stan` of the hier_* and
  ts_* templates are **stale**: lot T1 (`56ec7dd6`) changed the graphs of those cases from the templates opened with example numbers
  (1200 / 300) to the templates at their default numbers, and did not regenerate the `checks_*` files, which still hold `normal(1200.0,
  (2.5 * 300.0))`. The first run of this lot took them and the test failed with D between 0.5 and 0.99 on every quantity of those
  cases, which is how the test is known to catch a wrong scale. The command now generates the programs itself. To refresh the
  goldens: `BAYES_GOLDEN_WRITE=1 go test ./internal/bayeschecks -run TestGolden`, reviewed by hand (not done in this lot).
