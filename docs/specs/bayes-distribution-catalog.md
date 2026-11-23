# Spec: the distribution catalogue of the Bayesian editor (`distribution-catalog`)

Status: **approved by the owner on 2026-11-23.** It specifies the module `distribution-catalog`
of `docs/specs/bayesian-editor-map.md` (**the map**), step 0 of its order of construction, after the
interview of 2026-11-23 (**the answers**, numbered as in the map's «Interview answers»). Nothing of it is
built. Decisions marked **(owner)** are the owner's; those marked «spec decision, 2026-11-23, owner may revisit»
are this spec's. The owner answered this spec's questions on 2026-11-23 («Decisions of 2026-11-23»); no question
is open.

Words: **verified** = read in this repository's code on 2026-11-23; **checked online** = read on the npm
registry, in the Stan reference manual or functions reference (version 2.40) on 2026-11-23; **measured** = run
on 2026-11-23 in a scratch directory outside the repository (an install of the stdlib packages, a bundle with
the esbuild the frontend already has, a few calls under Node); **assumed** = from the literature or a
library's documentation, not run here, checked by the lot or the module named beside it.

## Objective

Every other module of the editor needs the same facts about distributions and functions, and must never
disagree on them: the browser draws a prior's chart (`prior-preview`), the gateway types an expression
(`expression-language`) and validates a graph (`model-graph`), the codegen writes Stan's names
(`stan-codegen`), the runner and the checks draw replicated data (`predictive-checks`), the report names the
parameters (`bayes-report`). The catalogue is that one source:

- the **distributions** of the first release (answer 2 (b), owner): their parameters in Stan's order with
  their constraints, their support, the role they may play (prior, likelihood, internal), their names in Stan
  (the statement, the log density, the random generator, the CDFs a truncation needs), whether a parameter is
  a location and a scale (the non-centred form, answer 4), and what the browser can draw of them;
- the **rule of bounds and truncation**, stated once (the half-normal trap of the map);
- the **functions and operators** the expression language may use (answer 3), each with its Stan name and
  the interval it maps to;
- **Stan's reserved words** and suffixes (answer 5), copied from the reference manual of the pinned version;
- the **limits** of the expression language and of the graph, one number each, the same in the browser and
  in the gateway;
- the **browser bindings**: one TypeScript module that gives each distribution its density, log density, CDF,
  quantile and seeded draws through the stdlib packages (accepted as a new dependency, answer 2, owner).

A reader who does not know Bayesian statistics (answer 1) gets, for each distribution, a one-sentence
description of what it says about a quantity, in five languages.

## Decisions

| Question | Decision | By |
|---|---|---|
| Distributions | Priors: normal, Student-t, Cauchy, exponential, gamma, inverse-gamma, lognormal, beta, uniform; likelihoods: normal, Bernoulli-logit, binomial-logit, Poisson-log, negative binomial 2-log; half forms by bounds; **plus** LKJ (Cholesky form) and the multivariate normal (Cholesky form) for correlated group effects | Owner (answer 2) |
| The browser's library | **stdlib**, modular packages, accepted as a new frontend dependency | Owner (answer 2) |
| What the browser draws | Only what the priors' marginals need (answer 13: no prior predictive in the browser): the nine prior distributions exactly; LKJ through the closed-form marginal of one correlation; the multivariate normal through the marginal of one component; the likelihood-only distributions **not at all** | Owner (answer 13), the mapping: spec |
| Where the catalogue is declared | **In Go**, in `gateway-go/internal/bayescatalog`, as plain declarations; a command writes the snapshot `catalog.json` from them; the frontend keeps a byte-for-byte copy checked in CI, as the models' catalogue does | Spec (the models' pattern with the gateway in Go: the engine plays no part in this editor) |
| Truncation | One rule («Bounds and truncation»), applied by the browser and by the codegen | Spec |
| Uniform | Its two bounds are **constants** in the first release | Spec decision, 2026-11-23, owner may revisit |
| The limits of the language and of the graph | **Documented constants of the catalogue**, changed by a release; the run's limits stay administrator settings | Owner (2026-11-23) |
| The licence notices | **Left as they are**: one text per package, about 3.7 MB more | Owner (2026-11-23) |
| Draws in the browser | One seeded generator per preview (stdlib's MT19937 through each factory's `prng` option); untruncated draws by stdlib's generators, truncated draws by inversion of the CDF | Spec (measured: «Bundle and speed») |

## What exists and is reused (verified)

| Need | Already there | What this module adds |
|---|---|---|
| A catalogue declared once, copied byte for byte | `.github/scripts/model_catalog.py` (standard library only; copies `backend/app/engine/models/catalog.json` to `frontend/assets/model-catalog.json`, `gateway-go/internal/modelcatalog/catalog.json`, `gateway/app/services/model_catalog.json`; `--check` fails on a changed byte, a missing copy, a model not named in `docs/capabilities/models.md` once the page exists); `.github/workflows/model-catalog.yml` (runs `test_model_catalog.py -v` on changes of those paths) | `.github/scripts/bayes_catalog.py`, `test_bayes_catalog.py`, `.github/workflows/bayes-catalog.yml`, the same way |
| An embedded copy in the gateway | `gateway-go/internal/modelcatalog/modelcatalog.go`: `//go:embed catalog.json`, `Raw()`, `Compact()` («what a model Job receives as catalog.json»; a ConfigMap holds at most 1 MiB) | `bayescatalog.Raw()`, `Compact()`; the runner's copy is put in its ConfigMap by `bayes-contract` |
| A typed reader of a catalogue in the editor | `frontend/composables/modelCatalog.ts` over `~/assets/model-catalog.json`; tests import the app's TypeScript through `frontend/tests/alias-hooks.mjs` (Node strips the types: `node --experimental-strip-types`, the `test` script of `frontend/package.json`; Node 22 in CI) | `frontend/composables/bayesCatalog.ts`, `bayesDistributions.ts` |
| Witnesses outside the image | `gateway-go/internal/modelgroups/groups/boosting/reference/witness.json` (pins `scipy==1.18.1`, `numpy==2.5.5` among others, «never installed in the group's image»), `make_witness.py`, run with `uv run --no-project` | `make_points.py` and `points.json` for the distributions |
| The licence notices of the frontend | `frontend/scripts/third-party-notices.mjs`: every non-dev package of `package-lock.json`, its declared licence and every file whose name starts with `LICENSE`, `COPYING`, `NOTICE` or `THIRD-PARTY`, written to `public/third-party-notices.txt` (1,391,323 bytes today) | The stdlib packages appear there by themselves («Licences») |
| Five languages | `frontend/i18n/locales/*.gen.ts`, edited by hand, one namespace per feature (models spec) | The namespace `bayes` |

## The catalogue

### Shape of the snapshot

`catalog.json` is one JSON object, indented by two spaces, keys in the order below, lists in the
declarations' order. Abridged:

```json
{
  "format_version": 1,
  "stan": {
    "version": "2.40.0",
    "source": "https://mc-stan.org/docs/reference-manual/expressions.html",
    "reserved_words": ["for", "in", "while", "repeat", "until", "if", "then", "else", "true", "false",
                       "target", "struct", "typedef", "export", "auto", "extern", "var", "static", "lower",
                       "upper", "offset", "multiplier"],
    "type_names": ["int", "real", "complex", "vector", "simplex", "unit_vector", "sum_to_zero_vector", "…"],
    "reserved_functions": ["print", "reject", "profile", "fatal_error", "target", "jacobian"],
    "block_names": ["functions", "model", "data", "parameters", "quantities", "transformed", "generated"],
    "reserved_suffixes": ["_lpdf", "_lpmf", "_lcdf", "_lccdf", "_cdf", "_ccdf", "_lupdf", "_lupmf"],
    "renamed_backend_words": ["alignas", "alignof", "and", "…", "STAN_MATH_PATCH"],
    "backend_member_words": ["log_prob", "write_array", "…", "errno", "…", "chkder_log10e"],
    "backend_member_patterns": ["get_*_sizedtypes", "*_impl"]
  },
  "intervals": { "real": {}, "positive": {"lower": 0, "lower_open": true}, "unit": {"lower": 0, "lower_open": true, "upper": 1, "upper_open": true} },
  "distributions": [
    { "id": "normal", "uses": ["prior", "likelihood"],
      "params": [ {"name": "mu", "constraint": "real"}, {"name": "sigma", "constraint": "positive"} ],
      "support": "real", "outcome": "real",
      "stan": { "statement": "normal", "density": "normal_lpdf", "rng": "normal_rng",
                "lcdf": "normal_lcdf", "lccdf": "normal_lccdf" },
      "location_scale": {"location": "mu", "scale": "sigma"},
      "browser": "exact" }
  ],
  "language": { "functions": [ "…" ], "operators": [ "…" ], "hints": [ "…" ], "confusables": [ "…" ] },
  "internal_functions": [ "…" ],
  "limits": { "max_name_bytes": 40, "…": 0 }
}
```

`stan.*` are **Stan 2.40.0's lists, copied verbatim** from the reference manual's «Reserved names» (checked
online): the reserved words, the type names, the reserved keywords and built-in functions, the block
identifiers, the distribution suffixes («No user-defined variable can take a name ending in _lupdf or _lupmf
even if a corresponding _lpdf or _lpmf is not defined»), and the backend words that are legal but «renamed
_stan_NAME». `stan.version` is replaced by the version `bayes-image` pins; a test of `bayes-image` checks the
lists against that version's manual page when the version changes. The lists are inputs of `stan-codegen`'s
identifier rule (answer 5); `model-graph` only checks names lexically.

`stan.backend_member_words` and `stan.backend_member_patterns` are **not the manual's**: they are the names the C++
class stanc writes for a model already holds, which stanc accepts for a data variable and g++ refuses (a data variable
is a member of that class): the class's member functions, its base class's name, `size_t`, the object-like macros of
the libraries the compile includes (`errno`, `math_errhandling`, …), `model_model` (refused by stanc: the compile
Job's file is `model.stan`), and two families, `get_*_sizedtypes` and `*_impl`, where `*` stands for one or more of
`[a-z0-9_]` (so a generated identifier, upper-case first, never matches). They were found by compiling in the image
of the group `bayes` every lower-case word of the headers stanc 2.40.0 writes and of the macros the compile defines
(correction of 2026-11-24 from the expression language's review E4, finding 2); the search is to be repeated
when `bayes-image` pins another version, and the golden case `names_backend` of `stan-codegen` must compile.

### The distributions

Parameters are in Stan's argument order. «Constraint» is what the argument must satisfy; «support» is where
the distribution puts its mass. The browser's column names the stdlib packages (each with its `-pdf`,
`-logpdf`, `-cdf`, `-quantile` package and `@stdlib/random-base-<name>`).

| id | Uses | Parameters (Stan's order, constraint) | Support / outcome | Stan statement, density, generator | Browser | Notes |
|---|---|---|---|---|---|---|
| `normal` | prior, likelihood | `mu` real, `sigma` positive | real / real | `normal`, `normal_lpdf`, `normal_rng` | exact: `stats-base-dists-normal-*`, `random-base-normal` (mean, sd) | location `mu`, scale `sigma` |
| `student_t` | prior | `nu` positive, `mu` real, `sigma` positive | real | `student_t`, `student_t_lpdf`, `student_t_rng` | exact: `stats-base-dists-t-*`, `random-base-t` | **stdlib's Student-t is the standard one (one argument, the degrees of freedom; measured: `t-pdf` takes two arguments, `x` and `v`)**: the binding moves and stretches it, x = mu + sigma·t, density f((x − mu)/sigma)/sigma; location `mu`, scale `sigma` |
| `cauchy` | prior | `mu` real, `sigma` positive | real | `cauchy`, `cauchy_lpdf`, `cauchy_rng` | exact: `stats-base-dists-cauchy-*` (x0, gamma), `random-base-cauchy` | measured: `cauchy-pdf(1, 0, 2)` = 1/(2π(1 + 1/4)); location `mu`, scale `sigma` |
| `exponential` | prior | `beta` positive (rate, «inverse scale» in Stan) | positive | `exponential`, `exponential_lpdf`, `exponential_rng` | exact: `stats-base-dists-exponential-*` (λ, a rate; measured: `pdf(1, 2)` = 2e⁻²) | |
| `gamma` | prior | `alpha` positive (shape), `beta` positive (rate) | positive | `gamma`, `gamma_lpdf`, `gamma_rng` | exact: `stats-base-dists-gamma-*` (shape, rate; measured: `pdf(1, 2, 3)` = 9e⁻³ = Stan's Gamma(1 \| 2, 3)) | |
| `inv_gamma` | prior | `alpha` positive (shape), `beta` positive (scale) | positive | `inv_gamma`, `inv_gamma_lpdf`, `inv_gamma_rng` | exact: `stats-base-dists-invgamma-*` (measured: `pdf(1, 2, 3)` = 9e⁻³ = Stan's InvGamma(1 \| 2, 3)) | |
| `lognormal` | prior | `mu` real, `sigma` positive | positive | `lognormal`, `lognormal_lpdf`, `lognormal_rng` | exact: `stats-base-dists-lognormal-*` | |
| `beta` | prior | `alpha` positive, `beta` positive | unit | `beta`, `beta_lpdf`, `beta_rng` | exact: `stats-base-dists-beta-*` | |
| `uniform` | prior | `alpha` real, `beta` real, both **constants**, `alpha < beta` | [alpha, beta] | `uniform`, `uniform_lpdf`, `uniform_rng` | exact: `stats-base-dists-uniform-*` | the parameter is declared with these bounds |
| `lkj_corr_cholesky` | prior (of a correlated-effects node) | `eta` positive, a **constant** | Cholesky factors of K×K correlation matrices | `lkj_corr_cholesky`, `lkj_corr_cholesky_lpdf`, `lkj_corr_cholesky_rng(K, eta)` | marginal: one correlation r has (r + 1)/2 ~ Beta(eta − 1 + K/2, eta − 1 + K/2) (Lewandowski, Kurowicka, Joe 2009; assumed, checked in `bayes-image`'s self-test against `lkj_corr_cholesky_rng` draws) | |
| `multi_normal_cholesky` | internal (the centred form of correlated effects) | `mu` vector, `L` Cholesky factor of a covariance | vector | `multi_normal_cholesky`, `multi_normal_cholesky_lpdf`, `multi_normal_cholesky_rng` | marginal: with L = diag(tau)·L_Ω and L_Ω a correlation's Cholesky factor, component k is normal(mu_k, tau_k), whatever the correlation | location `mu`, scale `L` |
| `std_normal` | internal (the non-centred form) | — | real | `std_normal`, `std_normal_lpdf`, `std_normal_rng` | exact (normal(0, 1)) | |
| `bernoulli_logit` | likelihood | `alpha` real (log-odds) | outcome: 0 or 1 | `bernoulli_logit`, `bernoulli_logit_lpmf`, `bernoulli_logit_rng` | none | link `logit` |
| `binomial_logit` | likelihood | `N` integer ≥ 0 (data), `alpha` real (log-odds) | outcome: 0…N | `binomial_logit`, `binomial_logit_lpmf`; generator **`binomial_rng(N, inv_logit(alpha))`**: Stan 2.40 has no `binomial_logit_rng` (checked online, functions index) | none | link `logit` |
| `poisson_log` | likelihood | `alpha` real (log rate) | outcome: 0, 1, 2… | `poisson_log`, `poisson_log_lpmf`, `poisson_log_rng` | none | link `log`; Stan's Poisson generator refuses very large rates (assumed; `predictive-checks` reads the bound in the pinned version) |
| `neg_binomial_2_log` | likelihood | `eta` real (log mean), `phi` positive (precision) | outcome: 0, 1, 2… | `neg_binomial_2_log`, `neg_binomial_2_log_lpmf`, `neg_binomial_2_log_rng` | none | link `log`; Stan has no CDF for it («as of yet, there are no CDFs implemented for it», checked online): it cannot be truncated |

Every name of the «Stan» column is in the functions index of Stan 2.40 (checked online, one search per name).
The parametrizations of gamma (shape and rate: «the rate (or inverse scale) parameter β»), inverse-gamma and
exponential («inverse scale (rate) β») were read in the functions reference (checked online). `bayes-image`'s
self-test checks every name against the pinned stanc (`--info` of a program that calls each, assumed to be the
simplest way; the lot chooses).

**Labels and explanations** (answer 1). Each distribution has `bayes.dist.<id>.name`, `bayes.dist.<id>.what`
(one sentence for a reader who does not know Bayes, a fact and never a judgement: «Normal: values around μ;
about two in three fall within σ of it») and `bayes.dist.<id>.param.<name>` in the five
`frontend/i18n/locales/*.gen.ts`. The catalogue holds the keys' ids, not the texts.

### Bounds and truncation (the one rule)

A parameter's **range** is the intersection of its prior's support with the bounds the person sets
(constants; `model-graph` refuses an empty intersection).

1. If the range equals the support, nothing is truncated.
2. If it is narrower (a normal prior on a parameter bounded below by 0: the half-normal), the prior is
   **truncated** to the range: its density is the prior's density divided by the prior's mass on the range.
3. **The browser** draws a truncated prior exactly, by inversion: u uniform on (F(L), F(U)), x = Q(u). When
   F(U) − F(L) is 0 in double precision, it draws nothing and says that the bounds leave the prior no mass the
   computer can represent; the graph is not refused for it.
4. **Stan** restricts the parameter by its declared bounds (`real<lower=L, upper=U>`). The `~` statement drops
   constants; the prior's mass on the range is a constant when **every argument of the prior is a constant**,
   and then the plain statement samples the truncated prior. When an argument is a parameter (a hyperprior),
   the mass changes with it: the codegen then writes the truncation (`T[L, U]`, `T[L, ]`, `T[, U]`), so that
   Stan's joint density is the one the browser samples ancestrally. (The rule writes `T[…]` also in cases
   where the mass happens to be constant, such as a half-normal whose scale is a parameter: correct, at a small
   cost; `stan-codegen` may refine it with a golden test.)
5. A truncation needs the distribution's `lcdf` and `lccdf`; a distribution without them (`neg_binomial_2_log`)
   cannot be truncated, and `model-graph` refuses such a bound.

`stan-codegen` and `prior-preview` cite this section; neither restates it.

### The expression language's vocabulary

The catalogue carries, as data, what `expression-language` (its spec: `bayes-expression-language.md`) may
accept and emit; that spec owns their meaning, this one their place:

- `language.functions`: `exp`, `log`, `log1p`, `expm1`, `sqrt`, `square`, `inv_logit`, `logit`, `log1p_exp`,
  `log_inv_logit` (each one argument, its Stan name equal to its own, the interval it accepts and the interval
  it returns: `exp` real → (0, ∞), `log` (0, ∞) → real, `log1p` (−1, ∞) → real, `expm1` real → (−1, ∞), `sqrt`
  [0, ∞) → [0, ∞), `square` real → [0, ∞), `inv_logit` real → (0, 1), `logit` (0, 1) → real, `log1p_exp` real →
  (0, ∞), `log_inv_logit` real → (−∞, 0)); and `lag`, which the language treats apart. Stan has no `softplus`:
  its name is `log1p_exp` (checked online). Non-differentiable functions (`abs`, `fmin`, `fmax`, `step`) are left
  out of the first release.
- `language.operators`: `+`, `-`, `*`, `/`, `^`, unary `-`, each with its scalar and elementwise Stan form
  (`*` / `.*`, `/` / `./`, `^` / `.^`) and Stan's precedence and associativity (reference manual: `+ -` 5 left;
  `* .* / ./` 4 left; unary `-` 2; `^ .^` 1 right; checked online).
- `language.hints`: the operators of other languages and their message ids (`**`, `%`, `==`, `;`, `~`, `.*`…).
- `language.confusables`: the non-ASCII characters that look like ASCII ones and the ASCII character meant
  (U+2212 → `-`, U+00D7 → `*`, U+00A0 → a space, U+0430 → `a`…).
- `internal_functions`: what `stan-codegen` may emit beyond the language (`to_vector`, `rep_vector`,
  `append_row`, `diag_pre_multiply`, `multiply_lower_tri_self_transpose`, `inv_logit`, `std_normal`…); the
  list belongs to `stan-codegen`, which extends it here, so that the cross-check of the compiled program's
  `--info` has one complete list of what may appear.

### Limits

One number each, read by the Go gateway and by the browser from the same snapshot (the browser must know
them before any request: they shape the instant feedback).

| Key | Default | Read by | What it bounds |
|---|---|---|---|
| `max_name_bytes` | 40 | `model-graph`, `expression-language` | A node's name |
| `max_label_chars` | 200 | `model-graph` | A label, in code points |
| `max_note_chars` | 2,000 | `model-graph` | A node's note |
| `max_expression_bytes` | 1,000 | `expression-language` | One expression, in UTF-8 bytes, checked before anything else |
| `max_expression_nesting` | 32 | `expression-language` | Parentheses, calls, indexing and unary signs inside one another |
| `max_expression_nodes` | 256 | `expression-language` | The syntax tree of one expression |
| `max_number_chars` | 32 | `expression-language` | One numeric literal |
| `max_abs_literal` | 1e15 | `expression-language`, `model-graph` (bounds) | A literal's or a bound's magnitude (a kind bound, as the risk node's ±1e15) |
| `max_lag` | 60 | `expression-language` | `k` of `lag(x, k)` |
| `max_nodes` | 200 | `model-graph` | Nodes of a graph |
| `max_plates` | 8 | `model-graph` | Plates, the rows' and the categories' included |
| `max_plate_depth` | 4 | `model-graph` | Nested group plates |
| `max_effect_components` | 10 | `model-graph` | Components of a correlated-effects node |
| `max_total_expression_nodes` | 4,096 | `model-graph` | All the syntax trees of a graph |
| `max_program_vector_nodes` | 1,500 | `model-graph` | The compile cost: the nodes of the expressions whose value is over a plate, and 1 + 3 a term of each linear predictor over a plate, summed over the graph (correction of 2026-11-24 from E4, owner: split into locals, 1,494 such nodes compile in 133 s with 1 CPU and 3 GiB, 3,984 do not finish in 330 s) |
| `max_graph_bytes` | 262,144 | `model-graph` (and the routes that receive a graph) | The graph's JSON |

These limits bound what a person may write, not a result; no verdict comes from them. They are **documented
constants of the catalogue**, not administrator settings (owner, 2026-11-23): the browser must know them before
any request, and they protect the parser. The run's limits (time, draws, parameters, rows, compile) stay
administrator settings with visible defaults (answer 10).

## The browser bindings

`frontend/composables/bayesDistributions.ts` gives each distribution with `browser` ≠ `none`:

```ts
export interface Binding {
  pdf(x: number, args: number[]): number
  logpdf(x: number, args: number[]): number
  cdf(x: number, args: number[]): number
  quantile(p: number, args: number[]): number
  /** n draws into `out`, from the stream `rng` (seeded by the caller), truncated to [lower, upper] when given */
  draw(rng: Uniform, args: number[], out: Float64Array, lower?: number, upper?: number): void
}
```

- **The packages** (answer 2): for the nine prior distributions, `@stdlib/stats-base-dists-<d>-{pdf,logpdf,cdf,quantile}`
  and `@stdlib/random-base-<d>`, with `<d>` ∈ normal, t, cauchy, exponential, gamma, invgamma, lognormal, beta,
  uniform: **45 packages**, pinned at their exact versions in `frontend/package.json` (as `monaco-editor` is)
  and by the lockfile. Their versions on 2026-11-23 (checked online): 0.3.1 (25 packages), 0.2.3 (12), 0.2.2
  (8), published on 2026-02-08 (42) and 2026-02-05 (3: `uniform-cdf`, `uniform-pdf`, `uniform-quantile`). They
  are CommonJS (`"main": "./lib"`) with TypeScript declarations (`docs/types/index.d.ts`) (measured); that Nuxt
  3.21's build bundles them into the worker's chunk is assumed, checked by lot C1.
- **One stream per preview**: a seeded MT19937 (`@stdlib/random-base-mt19937`, which the generators already
  pull) handed to every factory through its `prng` option; the stream's `state` is saved and restored
  (measured: restoring a factory's `state` gives the same draws again), so the same seed gives the same chart.
- **Untruncated draws** by stdlib's generators; **truncated draws** by inversion («Bounds and truncation»,
  3). Inversion is slower (measured under Node on the development machine: 4,000 quantiles of a gamma in 60
  ms, of a beta in 90 ms, of a Student-t in 33 ms); `prior-preview` measures both on a slow laptop in Chrome
  and Firefox and may move every draw to inversion if one code path is worth the time.
- **LKJ**: the binding draws one correlation as 2·Beta(b, b) − 1, b = eta − 1 + K/2; **multivariate normal**:
  component k as normal(mu_k, tau_k). Nothing else of these two is drawn in the browser.
- **Memory**: the caller passes a preallocated `Float64Array` of the size it decided (the number of draws of
  `prior-preview`, a setting of that module); the binding never grows an array (the rule of 2026-11-22, in
  the small).
- **Imported only by the worker** of `prior-preview` and by the tests: never by a module the entry chunk
  loads (the entry is already 1.59 MB, an open item of the project).

### Bundle and speed (measured, 2026-11-23)

The 45 packages installed alone in a scratch directory (`npm install --ignore-scripts`): **369 packages**
in all (360 of them `@stdlib/*`), 43 MB on disk. Bundled with the esbuild 0.28.2 of `frontend/node_modules`
(`--bundle --minify --format=esm --platform=browser`), all 45 imported:

| What is bundled | Minified | gzip -9 |
|---|---|---|
| pdf, logpdf, cdf, quantile of the nine (36 packages) | 238,682 bytes | 64,791 bytes |
| the same + `random-base-mt19937` | 341.9 KB | 84,821 bytes |
| all 45 | 392,653 bytes | 95,932 bytes |

The three largest inputs are `@stdlib/array-complex128`, `array-complex64` and `array-bool` (about 20, 20 and
17 KB minified), pulled by the generators' argument checks. `debug@2.6.9` (4,348 bytes in the bundle) comes
through `@stdlib/math-base-special-gammaincinv`. Vite's numbers will differ a little; lot C1 records them.

## Licences

Cited as the packages declare them, not interpreted (checked in the scratch install, 2026-11-23):

- the 45 packages and 303 of their dependencies declare `"license": "Apache-2.0"`;
- 12 dependencies declare `"license": "Apache-2.0 AND BSL-1.0"`: `@stdlib/math-base-special-beta`, `-betainc`,
  `-betaincinv`, `-erfcinv`, `-erfinv`, `-gamma-delta-ratio`, `-gamma-lanczos-sum`,
  `-gamma-lanczos-sum-expg-scaled`, `-gammainc`, `-kernel-betainc`, `-kernel-betaincinv`, `-powm1`. Their
  `LICENSE` ends with «DEPENDENCIES & ATTRIBUTION — The library links against the following external libraries
  or contains implementations from the following external libraries, which have their own licenses: * Boost
  <http://www.boost.org/LICENSE_1_0.txt>» followed by the text of the «Boost Software License - Version 1.0 -
  August 17th, 2003»; their sources say «The original C++ code and copyright notice are from the [Boost
  library]» with the Boost copyright lines;
- 9 dependencies outside stdlib declare `"license": "MIT"`: `debug`, `ms`, `resolve`, `is-core-module`,
  `hasown`, `es-errors`, `function-bind`, `path-parse`, `supports-preserve-symlinks-flag` (`resolve` and its
  own come through `@stdlib/utils-library-manifest`, which serves the packages' native add-ons under Node);
- every stdlib package ships a `NOTICE` («Copyright (c) 2016-2026 The Stdlib Authors.»).

`frontend/scripts/third-party-notices.mjs` picks all of them up by itself. It would also add their licence
files: about **3.7 MB** of text (measured), 328 of the 360 `LICENSE` files being byte-identical, to a notices
file of 1.39 MB today. **The owner decided to leave the script as it is** (2026-11-23): one text per package,
the file growing by about 3.7 MB.

## Copies

| Copy | Written by | Read by |
|---|---|---|
| `gateway-go/internal/bayescatalog/catalog.json` (the snapshot, embedded) | `go run ./cmd/bayescatalog` from the declarations | the gateway; `Compact()` for the Jobs' ConfigMaps (`bayes-contract` decides the file's name and what the runner reads) |
| `frontend/assets/bayes-catalog.json` | `.github/scripts/bayes_catalog.py` | `bayesCatalog.ts`, `bayesDistributions.ts`, `bayesExpr.ts`, `bayesGraph.ts` |
| `gateway/app/services/bayes_catalog.json` | the same script, **added by `assistants`** | the AI service |

`bayes_catalog.py --check` fails when a copy is missing or differs by a byte, when the snapshot is not a
catalogue (no distribution, an id twice, a `uses` value or a constraint it does not know), and, once
`docs/capabilities/bayesian-models.md` exists, when a distribution's id is not named in it. The Go test fails
when the snapshot is not what the declarations write.

## Tech stack

- **Go** 1.27 (`gateway-go/go.mod`: `go 1.27`; CI image `golang:1.27.3-bookworm`), standard library only
  (`encoding/json`, `embed`): no new module in `go.mod`.
- **Frontend**: Nuxt 3.21.11, Vue 3.5.43 (installed; `^3.14.0` and `^3.5.0` in `package.json`), TypeScript
  checked by `vue-tsc` 2.2.10 with TypeScript 5.6.3; tests with `node --test` and `--experimental-strip-types`
  (Node 22 in CI). **New**: the 45 `@stdlib/*` packages above (answer 2).
- **CI script**: Python 3.11+, standard library only (as `model_catalog.py`).
- **Witness** (outside Tabularia, by hand): SciPy 1.18.1 and NumPy 2.5.5 through `uv run --no-project`, the
  pins the boosting group's witness already uses. SciPy's forms (assumed from its documentation, checked by
  lot C2 on a few points by hand): `norm(loc=mu, scale=sigma)`, `t(nu, loc=mu, scale=sigma)`,
  `cauchy(loc=mu, scale=sigma)`, `expon(scale=1/beta)`, `gamma(alpha, scale=1/beta)`,
  `invgamma(alpha, scale=beta)`, `lognorm(sigma, scale=exp(mu))`, `beta(alpha, beta)`,
  `uniform(loc=alpha, scale=beta-alpha)`.

## Commands

```bash
# the snapshot from the declarations, then the copies and their check
cd gateway-go && go run ./cmd/bayescatalog
python3 .github/scripts/bayes_catalog.py && python3 .github/scripts/bayes_catalog.py --check
python3 .github/scripts/test_bayes_catalog.py -v
# gateway
cd gateway-go && go test ./internal/bayescatalog/... ./cmd/bayescatalog/...
# frontend (as for the models: one heavy job at a time on this machine)
cd frontend && npm i --no-save @intlify/message-compiler@9.14.5 && npm test
cd frontend && npx nuxi prepare && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
# the witness's points, by hand, outside Tabularia (the header of points.json says the versions)
uv run --no-project --with scipy==1.18.1 --with numpy==2.5.5 \
  python gateway-go/internal/bayescatalog/testdata/make_points.py
```

## Project structure

```text
gateway-go/internal/bayescatalog/
  catalog.go            types, Raw(), Compact(), lookups (Distribution, Function, Limits, Stan)
  declare.go            the declarations: distributions, functions, operators, hints, confusables, limits, Stan's lists
  catalog.json          the snapshot (embedded)
  catalog_test.go       the snapshot is the declarations; the invariants («Testing strategy»)
  testdata/make_points.py, testdata/points.json      the witness and its points
gateway-go/cmd/bayescatalog/main.go                  writes catalog.json
.github/scripts/bayes_catalog.py, test_bayes_catalog.py
.github/workflows/bayes-catalog.yml
frontend/assets/bayes-catalog.json                   the copy
frontend/composables/bayesCatalog.ts                 types and lookups over the copy
frontend/composables/bayesDistributions.ts           the stdlib bindings
frontend/tests/bayes-catalog.test.mjs, bayes-distributions.test.mjs
frontend/i18n/locales/*.gen.ts                       the namespace `bayes` (dist, param, fn, support)
```

## Code style

The gateway's embedded catalogues, as they are (verified, `gateway-go/internal/modelcatalog/modelcatalog.go`):

```go
//go:embed catalog.json
var raw []byte

// Raw is the snapshot, byte for byte.
func Raw() []byte { return append([]byte(nil), raw...) }

// Compact is the snapshot without its indentation (the same JSON, half the bytes): what a model
// Job receives as catalog.json. A Job's ConfigMap holds at most 1 MiB with the runner, the
// library and the reference cases, and the catalogue grows with every model.
func Compact() []byte {
	var out bytes.Buffer
	if err := json.Compact(&out, raw); err != nil {
		panic("modelcatalog: catalog.json: " + err.Error())
	}
	return out.Bytes()
}
```

A declaration in the same voice (a sketch; the lot writes it):

```go
// Student's t as Stan writes it: degrees of freedom, location, scale. The browser's library has
// the standard t only; bayesDistributions.ts moves and stretches it.
{ID: "student_t", Uses: uses(Prior),
	Params:  []Param{{"nu", Positive}, {"mu", Real}, {"sigma", Positive}},
	Support: RealLine,
	Stan:    StanNames{Statement: "student_t", Density: "student_t_lpdf", RNG: "student_t_rng",
		LCDF: "student_t_lcdf", LCCDF: "student_t_lccdf"},
	LocationScale: &LocScale{Location: "mu", Scale: "sigma"},
	Browser:       Exact},
```

Comments are sentences that say why; names in English; no abbreviation a reader must guess.

## Testing strategy

**On small data, always** (owner's rule): points and draws, never large tables.

- **Go** (`catalog_test.go`): the snapshot equals what the declarations write, byte for byte; ids unique;
  every `uses` value, constraint, support and interval known; every prior has `browser` ≠ `none`; every
  likelihood has an `outcome` and a generator (`rng` or the binomial-logit form); every truncatable
  distribution has `lcdf` and `lccdf`; every Stan name matches `^[a-z][a-z0-9_]*$`; the `location_scale` names
  are parameters of their distribution; the language's functions have one argument and known intervals;
  limits positive; Stan's lists equal the frozen copy of the manual's page (`testdata/stan_reserved_2.40.json`,
  with the URL and the date), sorted and without duplicates.
- **CI script** (`test_bayes_catalog.py`, as `test_model_catalog.py`): copies written, `--check` green, and red
  on a changed byte, a missing copy, a broken snapshot.
- **Frontend** (`bayes-catalog.test.mjs`): the copy loads through `bayesCatalog.ts`; every distribution,
  parameter, function and support has its words in the five languages; every distribution with `browser` ≠
  `none` has a binding, and none without.
- **Frontend** (`bayes-distributions.test.mjs`), against the witness's `points.json` (for each prior
  distribution, a handful of argument sets including extreme ones — `sigma` 1e-3 and 1e3, `nu` 1, 2 and 30,
  gamma's shape 0.5 and 50, beta (0.5, 0.5) and (50, 2) — and points at the 1e-6, 0.01, 0.5, 0.99 and
  1 − 1e-6 quantiles): pdf, logpdf and cdf within a relative 1e-10, quantile within 1e-8, unless `points.json`
  writes a wider tolerance for a case with its reason; the Student-t's location and scale; the same seed gives
  the same draws, also after a saved `state` is restored; truncated draws stay inside the bounds and their
  empirical CDF matches the truncated CDF by a Kolmogorov–Smirnov test with its p-value against the suite's
  alpha (0.001, fixed seeds: the test is deterministic); LKJ's correlation marginal has mean 0 and variance
  1/(2·eta + K − 1) (moments of the Beta, within the Monte Carlo error of the fixed draws).
- **Handed to `bayes-image`** (not this module's lot): the same points against Stan's own `_lpdf`/`_lpmf`,
  `_lcdf` in the group's self-test, and the LKJ marginal against `lkj_corr_cholesky_rng` draws. That closes the
  half-normal trap on Stan's side.
- **Regression**: the models' catalogue tests and the frontend suite as before; the notices script's test.

## Boundaries

- **Always**: one declaration in Go, copies by the script, `--check` in CI; Stan's lists verbatim from the
  pinned version's manual; every name the codegen may emit listed here; licences cited as declared; the
  bindings imported only by the worker and the tests; five languages.
- **Ask first**: a distribution, a function or an operator beyond the owner's list (answer 2, answer 3); a
  limit's default; a change to `third-party-notices.mjs` (the owner kept it as it is); any JavaScript dependency beyond
  the 45 packages (they bring their own 324); stdlib packages at a new version.
- **Never**: a distribution's name or parametrization written outside the catalogue; a JavaScript draw
  without a seed; the bindings in the entry chunk; a licence interpreted.

## Success criteria

1. `go run ./cmd/bayescatalog` writes `catalog.json`; `bayes_catalog.py --check` is green and red on a changed
   byte; the Go and frontend tests above pass.
2. Every distribution of answer 2 is in the catalogue with its Stan names; every name exists in Stan 2.40
   (checked online now, by `bayes-image`'s self-test later).
3. The bindings agree with the witness at every point of `points.json` within the tolerances; the
   Student-t's location and scale and the truncated draws are right.
4. The same seed gives the same draws in Chrome and Firefox (one manual check, recorded).
5. `nuxt build` puts no stdlib code in the entry chunk (lot C1 records the chunk sizes).
6. No new module in `go.mod`; in `package.json`, exactly the 45 packages.

## Lots

| Lot | Objective | Files | Accepted when | Agent |
|---|---|---|---|---|
| **C0** | Declarations, snapshot, command, copy script and workflow, Go tests | `gateway-go/internal/bayescatalog/*`, `cmd/bayescatalog/*`, `.github/scripts/bayes_catalog.py`, `test_bayes_catalog.py`, `.github/workflows/bayes-catalog.yml`, `frontend/assets/bayes-catalog.json` | the Go tests and `--check` green | sonnet |
| **C1** | The 45 packages, `bayesCatalog.ts`, `bayesDistributions.ts`, the words in five languages, the frontend tests | `frontend/package.json`, `package-lock.json`, the two composables, the two tests, the locales (`bayes`) | the frontend tests and `vue-tsc` green; build chunk sizes recorded | sonnet |
| **C2** | The witness's points | `testdata/make_points.py`, `points.json` | the points read back by the frontend test | haiku |

C0 first (its lists unblock `expression-language`); C1 and C2 in parallel after it. None is
heavy (no image, no cluster).

## Decisions of 2026-11-23 (the answers to this spec's questions)

| Question | Decision | By |
|---|---|---|
| The licence notices: print each distinct licence text once, or leave the script as it is | **Leave it as it is**: one text per package, about 3.7 MB more in `public/third-party-notices.txt` (against this spec's recommendation; the lot that would have changed the script is dropped) | Owner, 2026-11-23 |
| The limits of the language and of the graph: catalogue constants or administrator settings | **Documented constants of the catalogue**; the run's limits stay administrator settings (answer 10) | Owner, 2026-11-23 |
| Uniform with parameter bounds | **Constants only** in the first release | Spec decision, 2026-11-23, owner may revisit |

## Correction of 2026-11-24 from the step-4 specs

From `bayes-stan-compile.md`, `bayes-diagnostics.md` and `bayes-predictive-checks.md`, approved by the owner on
2026-11-24. Where this section and the text above differ, this section holds.

1. **`stan.quantile`** per prior distribution: the form of Stan's quantile a truncated prior's ancestral draw uses in the
   program (normal: `inv_Phi`; lognormal: `exp` of it; Cauchy: `tan`; Student-t and beta: `inv_inc_beta`; exponential:
   `log1m`; uniform: linear), and **none for `gamma` and `inv_gamma`** (Stan 2.40 has no quantile for them, assumed): such a
   prior truncated inside its support has no ancestral draw in Stan (`bayes-predictive-checks.md`, «Truncated priors»).
2. **`finite_variance`** per distribution, for its constant arguments: normal, exponential, gamma, lognormal, beta,
   uniform always; Cauchy never; Student-t only with ν > 2; inverse-gamma only with α > 2; a truncation to a bounded range
   always. `bayes-diagnostics` leaves the contraction NULL when a node's marginal, or a node its arguments read, has none
   (the owner, 2026-11-24).

## Open questions

None.
