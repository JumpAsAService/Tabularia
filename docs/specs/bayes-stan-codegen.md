# Spec: the Stan program generated from the graph (`stan-codegen`)

Status: **written and approved by the owner on 2026-11-23.** It specifies the module `stan-codegen` of
`docs/specs/bayesian-editor-map.md` (**the map**), step 2 of its order of construction, beside `bayes-contract`
(`bayes-contract.md`) and `prior-preview` (`bayes-prior-preview.md`), after the approved
`bayes-distribution-catalog.md` (**the catalogue spec**), `bayes-expression-language.md` (**the language spec**),
`bayes-model-graph.md` (**the graph spec**) and `bayes-image.md`. Nothing of it is built. Decisions marked
**(owner)** are the owner's (the map's «Interview answers», numbered as there: **the answers**); the rest are this
spec's, to confirm; those marked «spec decision, 2026-11-23, owner may revisit» are small choices the owner may
change without reopening the module. The owner answered this spec's question on 2026-11-23 («Decisions of
2026-11-23»); no question is open.

Words: **verified** = read in this repository's code or in the approved specs on 2026-11-23; **checked online** =
read in the Stan reference manual, functions reference or CmdStan guide (version 2.40) on 2026-11-23 by the approved
specs that cite it; **assumed** = from the Stan documentation, not run here: every such point is closed by the
golden tests compiled with the pinned stanc («Testing strategy», the stanc check), which is where «assumed» becomes
«run».

The map calls this module **the critical path**: templates, the store, the compile and the checks wait for it, and
its golden tests come first.

## Objective

From a **validated model** (`bayesgraph.Model`, the graph spec's «Validated model»), the Go gateway writes **one
Stan program**:

- **deterministic**: the same model gives the same bytes, on every machine, in every run of the gateway; a moved box
  or a renamed label gives the same bytes (labels never enter the program, answer 5);
- **safe by construction**: no text of a person reaches it — names only when they pass the identifier rule, every
  expression through `bayesexpr.Emit` (the language spec), every function and distribution from the catalogue, every
  number re-printed from its double; no comment, no `print`, `reject` or `fatal_error`, no `#include`, no
  `functions` block, no `target`;
- **the model the person drew**: the data block from the bound variables, the priors with their bounds and the
  truncation rule of the catalogue, the non-centred or centred forms (answer 4), vectorized likelihoods, correlated
  effects with LKJ (answer 2), latent states, `lag` for observed series;
- **bounded output**: what the program writes grows with the parameters, never with the rows (per-row quantities
  are local variables; the map's provisional decision);
- with an **extension point** where `predictive-checks` adds its data and its generated quantities;
- with the **mapping table** (identifier ↔ node ↔ label) and the **two manifests** `bayes-contract` defines (the
  data the runner writes, the quantities the program writes).

The browser never sends a program: it asks the gateway for the generated text to show it, read-only (the map).

## Decisions

| Question | Decision | By |
|---|---|---|
| Where | **Go, in the gateway**: `gateway-go/internal/bayescodegen`, the only producer of a program | The map (provisional, follows from answer 3's authority in Go) |
| Code | **Generated only**; shown, never edited | Owner |
| Identifiers | The person's name when it passes the rule («Identifiers»), a generated identifier otherwise; labels never in the program, not even in a comment | Owner (answer 5) |
| Hierarchies | **Non-centred by default, a switch per plate** | Owner (answer 4) |
| How a plate parameter is non-centred | Stan's **affine transform** (`<offset=…, multiplier=…>`) when its location and scale are constants or parameters; otherwise an explicit standard-normal parameter and a transformed parameter («Non-centred and centred») | Spec decision, 2026-11-23, owner may revisit |
| Truncation | The catalogue's «Bounds and truncation», applied as the validated model flags it; a truncated statement is **scalar**, inside a loop over the units for a plate | Spec (the catalogue spec, rule 4; the loop: spec decision, 2026-11-23, owner may revisit) |
| Correlated effects | Non-centred: `cholesky_factor_corr` with `lkj_corr_cholesky(eta)`, standard normals multiplied by `diag_pre_multiply(scales, L)`; centred: `multi_normal_cholesky` | Owner (answer 2), the form: spec |
| Per-row quantities | **Local variables** of the model block (and of a nested block of `generated quantities` when an extension needs them); a value that depends on no parameter goes to `transformed data` | The map (provisional), spec |
| Comments | **None** in the program: the mapping table is shown beside it | Spec |
| `_glm` likelihoods (`normal_id_glm`, `bernoulli_logit_glm`…) | **Not in the first release**: plain vectorized statements; a later lot may measure them | Spec decision, 2026-11-23, owner may revisit |
| The stored program | `model-store` keeps the program with each version; **a saved version always runs with its stored program**; a new `CodegenVersion` never rewrites a stored program and never changes what a version runs | Owner (2026-11-23) |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| The validated model | The graph spec: `Model{Plates, Order, Slots, Params, Edges, Lifts, Lags, Bindings, Pairs, Findings}`; `Order` a topological order with ties by id («the program's order of declarations»); `Params` with range, `truncated`, «truncation in the program», role, non-centred | The only input |
| Expressions to Stan | The language spec: `Emit(ast, env) → string`, fully parenthesized, its output alphabet `^[A-Za-z0-9_ .()\[\],+\-*/^]*$`; `EmitEnv{Identifier, IndexArray, LagArray, RowsSubset}`; numbers by `strconv.FormatFloat(v, 'g', -1, 64)` with `.0` appended | This module implements `EmitEnv` and calls `Emit` for every slot |
| Names Stan reserves | The catalogue's `stan.reserved_words`, `type_names`, `reserved_functions`, `block_names`, `reserved_suffixes`, `renamed_backend_words` (Stan 2.40.0, copied verbatim) | The identifier rule |
| What may be called | The catalogue's distributions (statement, density, generator names), `language.functions`, `internal_functions` («the list belongs to `stan-codegen`, which extends it here») | The allow-list of every call and statement |
| The image's compiler | `bayes-image`: stanc3 v2.40.0, `bin/linux-stanc` sha256 `29306a35092c7f6c1aa20df903ddb9dcd83af5995ba37630324a2e019d05e6f4`, equal to the stanc3 release asset's digest (measured there); stanc runs with no flag | The stanc check of the golden programs in CI |
| Text generated by Go that refuses what is not of its shape | `internal/scriptimage/scriptimage.go` (`wheelSteps`, `fileSteps`: each value checked, then written by `fmt.Fprintf` into a `strings.Builder`) | The same voice («Code style») |
| Golden files rewritten by an environment variable | `BAYES_CORPUS_WRITE=1` of the language and graph specs | `BAYES_GOLDEN_WRITE=1` |
| The manifests | `bayes-contract`: the data manifest, the quantity manifest, `internal/bayescontract` | Written by `Generate` |

## The program

### Blocks and their order

Always in this order; a block with nothing to hold is left out, except `data`, `parameters` and `model`:

1. `data` — sizes, the bound variables, the derived index arrays, `Mode`, the extension's data;
2. `transformed data` — values that depend on data only;
3. `parameters` — the parameters in `Model.Order`, each with its bounds and its affine transform;
4. `transformed parameters` — values computed from parameters that are **not per row**: non-centred plates of the
   explicit form, correlated effects' components, latent states;
5. `model` — the per-row locals, then the priors in `Order`, then the likelihoods in `Order`;
6. `generated quantities` — the correlation matrices, then the extension's written declarations, then one nested
   block with the per-row locals the extension asks for and its statements.

Never a `functions` block (no user-defined functions, answer 3; never an external C++ declaration).

### Layout of the text

- Two spaces of indentation per level; one statement or declaration a line; `{` at the end of the block's line, `}`
  alone on its line; one line feed (`\n`) after every line, the last included; no blank line, no tab, no carriage
  return, no trailing space; ASCII only.
- Declarations and statements in the order of «Blocks and their order», nodes in `Model.Order`, plates in
  `Model.Plates`' order, a node's own internal declarations right beside its own.
- Numbers: the language spec's formatter for every real (bounds, constants: `0.0`, `10.0`, `1e+15`); integers only
  where Stan needs an integer (sizes of components, `1` in a loop's range), printed by `strconv.Itoa` from a count
  the graph holds — never from a person's text.

### An example

The graph spec's example (pupils' scores in schools, a varying intercept per school, non-centred, one standardized
predictor, a scale without a name) gives exactly:

```stan
data {
  int<lower=0> N;
  int<lower=1> J_p1;
  vector[N] score;
  vector[N] hours;
  array[N] int<lower=1, upper=J_p1> school;
  int<lower=0, upper=1> Mode;
}
parameters {
  real mu;
  real<lower=0.0> tau;
  vector<offset=mu, multiplier=tau>[J_p1] alpha_school;
  real V_n7;
  real<lower=0.0> V_n8;
}
model {
  mu ~ normal(0.0, 10.0);
  tau ~ normal(0.0, 5.0);
  alpha_school ~ normal(mu, tau);
  V_n7 ~ normal(0.0, 1.0);
  V_n8 ~ exponential(1.0);
  score ~ normal(((alpha_school[school]) + (V_n7 * hours)), V_n8);
}
```

- `tau` is a half-normal: bounded below by 0 under a normal prior whose arguments are constants, so no `T[…]` (the
  catalogue's rule 4: the mass is a constant and `~` drops it).
- `V_n8` has no name (its label is «Spread within a school»): a generated identifier; its lower bound comes from the
  exponential's support (the range is the support ∩ the bounds, the graph spec).
- `V_n7` is the node named `beta` (its label «Effect of one more hour»): `beta` is the Stan name of a distribution the
  catalogue lists, so «The rule» below gives it a generated identifier and the mapping table says `renamed: reserved`
  with the word; the results keep the person's `beta` (Correction of 2026-11-24 from the mockups: the example wrote
  `real beta;`, against its own rule; the templates name such a coefficient `b_x` to avoid the renaming).
- `hours` arrives standardized: the runner standardizes (answer 6, `bayes-contract`); the program does not know.
- `alpha_school` is non-centred by the affine transform: the sampler moves on the standardized scale, the program
  writes the person's own `alpha_school` (no internal column).
- `Mode` is always declared (0 the posterior, 1 the prior predictive: one program, one binary for both, the map).

## Identifiers (answer 5)

| What | Identifier | Why it cannot collide |
|---|---|---|
| A node or component whose **name passes the rule** | the name | the rule below |
| A node without a name, or whose name fails the rule | `V_<node id>`, a component `V_<node id>_<component id>` | starts with an upper-case letter |
| Sizes | `N`, `J_<group plate id>`, `K_<category node id>` | upper case |
| Derived data | `Up_<index node id>_<plate id>` (a lifted index), `N_lag`, `Rows_lag`, `Lag<k>`, `N_first`, `Rows_first`, `N_next`, `Rows_next`, `Prev` | upper case |
| The mode flag | `Mode` | upper case |
| Internal parameters and locals | `Z_<id>` (standard normals), `L_<id>` (a Cholesky factor), `W_<id>`, `Full_<id>` (a category's coefficients with the reference's 0), `Arg<k>_<id>`, `I` (a loop's index) | upper case |

**The rule** for a person's name (lexically valid already, `[a-z][a-z0-9_]*`, at most 40 bytes, not ending in `__`:
the graph spec): it is used as is unless it is one of the catalogue's `stan.reserved_words`, `type_names`,
`reserved_functions`, `block_names` or `renamed_backend_words`, ends with one of `reserved_suffixes`, is one of the
words the output check refuses as a token (`print`, `reject`, `fatal_error`, `target`, `include`, `functions`,
`profile`: «Safety checks on the output»), or is the Stan name of any distribution, density, generator or function
the catalogue lists (a variable never shadows a library function). Otherwise it gets `V_<id>`, and the mapping table
says so.

**Why no two identifiers collide**: a person's name starts with a lower-case letter and every generated identifier
with an upper-case one; person's names are unique (the graph spec, `graph.name_taken`); generated identifiers embed
ids, which are unique and hold no `_` (`^[a-z][a-z0-9]{0,15}$`), so `V_<a>_<b>` reads back to one node and one
component. **Why a person cannot write a generated identifier in an expression**: the language's lexer refuses an
upper-case letter (`expr.name_case`); every name in an expression is a key into the graph, never text copied into
the program.

### The mapping table

One entry per identifier the program declares, in the program's order:

```json
{"stan": "V_n8", "block": "parameters", "node_id": "n8", "component": null, "name": null,
 "label": "Spread within a school", "kind": "parameter", "plate": null, "role": "quantity", "renamed": "no_name"}
```

`renamed` is `null`, `no_name` or `reserved` (with the word that made it). The table is shown beside the program by
`graph-editor`, stored with the version by `model-store`, carried by the card (`bayes-contract`); labels live only
here, never in the program.

## Declarations

### Data (the bound variables, answer 6)

| Node | Declaration |
|---|---|
| `real` on rows / on a group plate | `vector[N] x;` / `vector[J_<plate>] x;` |
| `int` (`any`, `nonnegative`, `binary`) | `array[N] int x;`, `array[N] int<lower=0> x;`, `array[N] int<lower=0, upper=1> x;` (on a group plate with `J_<plate>`) |
| `group_index` targeting plate p | `array[N] int<lower=1, upper=J_<p>> x;` and the size `int<lower=1> J_<p>;` |
| `category` | `array[N] int<lower=1, upper=K_<id>> x;` and `int<lower=1> K_<id>;` |
| `time` | nothing: the runner sorts by it (`bayes-contract`) |
| a lift of `Model.Lifts` | `array[J_<child>] int<lower=1, upper=J_<parent>> Up_<index>_<child>;` |
| `Model.Lags` (a series) | `int<lower=0> N_lag; array[N_lag] int<lower=1, upper=N> Rows_lag;` and for each k `array[N_lag] int<lower=1, upper=N> Lag<k>;` |
| a latent state | `int<lower=0> N_first; array[N_first] int<lower=1, upper=N> Rows_first; int<lower=0> N_next; array[N_next] int<lower=1, upper=N> Rows_next; array[N_next] int<lower=1, upper=N> Prev;` |

Sizes first (`N`, then the groups' `J` in `Model.Plates`' order, then the categories' `K`), then the data nodes in
`Order`, then lifts, lags and state arrays, then `Mode`, then the extension's data. The manifest of `bayes-contract`
lists them in the same order with the column each binds and whether it is standardized.

### Parameters, bounds and truncation

- A parameter's declaration carries **its range** (the graph spec: the prior's support ∩ the person's bounds) as
  `<lower=…, upper=…>`, an unbounded side left out. Uniform's range is its two constants (the catalogue).
- Its statement is `x ~ dist(args);` with every argument emitted by `Emit` in the parameter's context plate (a `ref`
  slot is the referenced identifier).
- **Truncation** (the catalogue's «Bounds and truncation», applied, not restated): when `Model.Params[x]` says the
  program needs it — an argument is not a constant and the range is narrower than the support — the statement is
  `x ~ dist(args) T[L, U];` with `T[L, ]` or `T[, U]` for one side. A truncated statement is written for **one
  value**: on a plate it sits in a loop, and an argument that varies over the plate is first a local vector:

  ```stan
    {
      vector[J_p1] Arg1_n6 = (mu_district[Up_n3_p1]);
      for (I in 1:J_p1) {
        sigma_school[I] ~ normal(Arg1_n6[I], s_hyper) T[0.0, ];
      }
    }
  ```

  (Vectorized truncation is not relied on: whether Stan 2.40 accepts `T[…]` on a vector statement is **assumed
  unknown**; the loop is correct in every version. A later lot may measure the vector form against the loop with the
  stanc check and a golden test.)
- A distribution without `lcdf` and `lccdf` is never truncated: `model-graph` refuses such a bound (the catalogue's
  rule 5).

### Non-centred and centred (answer 4)

For a parameter on a group plate whose plate's `form` is `non_centred` and whose prior is location-scale (the
catalogue's `location_scale`):

- **Affine form** (the default): when the location and the scale reference only constants and parameters (nodes the
  `parameters` block declares before it in `Order`):

  ```stan
    vector<offset=mu, multiplier=tau>[J_p1] alpha_school;
  …
    alpha_school ~ normal(mu, tau);
  ```

  The statement is the centred one; Stan samples the standardized variable (the reference manual's affinely
  transformed reals, assumed for vector offsets and multipliers: the stanc check closes it). The output is the
  person's parameter, nothing more.
- **Explicit form**: when the location or the scale references a `transformed` node (declared after `parameters`):
  `vector[J_p1] Z_n6;` in `parameters`, `vector[J_p1] alpha_school = loc + scale .* Z_n6;` (emitted) in
  `transformed parameters`, `Z_n6 ~ std_normal();` in `model`. `Z_n6` is internal (`bayes-contract`: read and
  dropped).
- **Centred** (the switch, or a prior that is not location-scale, or a bounded parameter): the plain declaration and
  statement. The affine transform cannot be combined with bounds, so a bounded parameter on a non-centred plate
  **stays centred**; `model-graph` should say it (a correction proposed to the graph spec: its warning
  `graph.form_centred` covers only the non-location-scale priors).

### Categories (answer 6)

A parameter on a category's levels plate is `vector[K_<id> - 1] b;` (the reference level has no coefficient); where a
linear predictor or an expression uses it per row, the model block first makes
`vector[K_<id>] Full_<b id> = append_row(0.0, b);` and indexes `Full_<b id>[category]`.

### Linear predictors and transformed nodes

| Node | Where | Text |
|---|---|---|
| `linear_predictor` on rows | a local of `model` (and of the extension's nested block) | `vector[N] eta = (((alpha + (beta1 * x1)) + (beta2 * to_vector(k))) + (Full_n7[channel]));` — the terms in the node's order, left-associated, each a scalar coefficient times a real data vector, an integer data through `to_vector`, a category through `Full_` |
| `linear_predictor` on a group plate | `transformed parameters` | the same over `J_<plate>` |
| `transformed`, per row, depending on a parameter | a local of `model` | `vector[N] x = <Emit>;` |
| `transformed`, depending on no parameter | `transformed data` | computed once, never written |
| `transformed`, scalar or on a group plate | `transformed parameters` | written: a quantity the person sees |

A local is named by the node's identifier. A per-row value is therefore never written by the program: what the CSV
holds grows with the parameters, the group plates and the latent states, never with the rows of a regression.

### Likelihoods

One vectorized statement per likelihood node, over its observed node's plate, in `Order`:

| Distribution (the catalogue) | Statement |
|---|---|
| `normal` | `y ~ normal(mu_expr, sigma_expr);` |
| `bernoulli_logit` | `y ~ bernoulli_logit(eta_expr);` |
| `binomial_logit` | `y ~ binomial_logit(trials, eta_expr);` (the trials a `nonnegative` integer data node) |
| `poisson_log` | `y ~ poisson_log(eta_expr);` |
| `neg_binomial_2_log` | `y ~ neg_binomial_2_log(eta_expr, phi_expr);` |

With `lag` in an argument (a series), the statement runs on the rows that have every lag the model uses:
`y[Rows_lag] ~ normal(mu_expr, sigma);` where the emitter writes a per-row name as `x[Rows_lag]` (`RowsSubset`) and
`lag(x, k)` as `(x[Lag<k>])` (`LagArray`); the runner builds `Rows_lag` and `Lag<k>` from the sorted rows, within
each unit of the series (`bayes-contract`). An AR(2) of a single series:

```stan
model {
  phi1 ~ normal(0.0, 0.5);
  phi2 ~ normal(0.0, 0.5);
  sigma ~ exponential(1.0);
  c ~ normal(0.0, 1.0);
  y[Rows_lag] ~ normal(((c + (phi1 * (y[Lag1]))) + (phi2 * (y[Lag2]))), sigma);
}
```

This example shows the emission of `lag`; the golden `ts_ar2` follows the AR template, which writes the mean apart
from the coefficients (`mu + phi1 * (lag(y, 1) - mu) + …`, `bayes-templates.md`). (Correction of 2026-11-23 from the
step-3/4 specs.)

### Latent states (`state`, random walk)

`x₁ ~ init`, `xₜ ~ normal(xₜ₋₁, scale)` within each unit of the series:

- **centred**: `vector[N] level;` in `parameters`; `level[Rows_first] ~ <init>(args);` and
  `level[Rows_next] ~ normal(level[Prev], scale);` in `model` — vectorized, no loop;
- **non-centred** (the default when `init` is location-scale): `vector[N] Z_<id>;` in `parameters`; in
  `transformed parameters`
  ```stan
    vector[N] level;
    level[Rows_first] = m0 + (s0 * Z_n5[Rows_first]);
    for (I in 1:N_next) {
      level[Rows_next[I]] = level[Prev[I]] + (scale * Z_n5[Rows_next[I]]);
    }
  ```
  and `Z_n5 ~ std_normal();` in `model`. The rows come sorted by (unit, time) (`bayes-contract`), so `Prev[I] <
  Rows_next[I]` and every value is set before it is read. An `init` that is not location-scale keeps the state
  centred (the same correction proposed to `graph.form_centred`).

A latent state is N parameters: **its output grows with the series' length** (the map's «bounded by the parameters»
holds, the parameters being the states), bounded by `max_rows` and by `bayes-contract`'s `max_quantities`.

### Correlated effects (answer 2: LKJ)

K components (an intercept and slopes) per unit of a group plate, scalar means and scales, `eta` a constant:

- **non-centred** (default):

  ```stan
  parameters {
    matrix[2, J_p1] Z_n9;
    cholesky_factor_corr[2] L_n9;
  }
  transformed parameters {
    vector[J_p1] intercept_school;
    vector[J_p1] slope_school;
    {
      matrix[2, J_p1] W_n9 = diag_pre_multiply(to_vector([s_int, s_slope]), L_n9) * Z_n9;
      intercept_school = m_int + to_vector(W_n9[1]);
      slope_school = m_slope + to_vector(W_n9[2]);
    }
  }
  model {
    to_vector(Z_n9) ~ std_normal();
    L_n9 ~ lkj_corr_cholesky(2.0);
  }
  generated quantities {
    matrix[2, 2] rho_school = multiply_lower_tri_self_transpose(L_n9);
  }
  ```

  The components are the person's quantities; `Z_n9`, `L_n9` are internal (dropped by the runner); the correlation
  matrix carries the correlation's name and `bayes-contract` keeps its strict lower triangle. No transpose operator is
  emitted (the program's alphabet has no `'`).
- **centred**: `array[J_p1] vector[2] V_n9;` and `cholesky_factor_corr[2] L_n9;` in `parameters`;
  `V_n9 ~ multi_normal_cholesky(to_vector([m_int, m_slope]), diag_pre_multiply(to_vector([s_int, s_slope]), L_n9));`;
  the components as `to_vector(V_n9[:, 1])`… in `transformed parameters` (`V_n9` internal).

The functions used — `diag_pre_multiply`, `multiply_lower_tri_self_transpose`, `to_vector`, `append_row` — are in the
catalogue's `internal_functions` already; this module adds none in the first release.

## The extension point (`predictive-checks`)

```go
// Extension adds to a program what predictive-checks needs: data (bins fixed from the observed data),
// written generated quantities (statistics of y_rep, prior draws), and the statements that fill them.
// Everything it writes passes the same checks as the rest of the program.
type Extension interface {
	Data(m *bayesgraph.Model) []Decl
	Generated(m *bayesgraph.Model, s Scope) (written []Decl, statements []Stmt, err error)
}

// Scope lets an extension write a node's value without writing text of its own: the identifiers of the
// program, the per-row locals rebuilt in the extension's block, and a node's value emitted against
// another source of identifiers (the prior's ancestral draws instead of the parameters).
type Scope interface {
	Identifier(node string) string
	Local(node string) string                              // asks for the node's per-row local in the nested block
	ValueWith(node string, ids func(node string) string) (string, error)
	Call(name string, args ...string) (string, error)      // only a catalogue name; anything else is an error
}
```

- `Decl` and `Stmt` are typed values (a declaration of a catalogue type with sizes, an assignment, a `for` over a
  size, a call of a catalogue `_rng` or function), **never strings**: the extension cannot write text the codegen has
  not checked.
- The written declarations of the prior predictive are sized by the mode (`array[Mode == 1 ? J_p1 : 0] real …`, the
  conditional operator in a size: assumed, the stanc check closes it), so that one program serves both modes and the
  posterior run writes nothing for the prior.
- Placement is fixed: the extension's data after `Mode`; its written declarations after the correlations; one nested
  block `{ … }` holding the per-row locals it asked for (in `Order`) and its statements. Locals in a nested block are
  not written: the y_rep vectors of `predictive-checks` live there (the map: «y_rep is drawn inside generated
  quantities as a local vector»).
- A program generated **without** an extension and **with** a test extension are both golden files: the point is
  pinned before `predictive-checks` exists.

## Safety checks on the output

Three checks, each a test and the first two also run by `Generate` before it returns (a failure is a bug: `Generate`
returns an error, never a program):

1. **The alphabet**: the program matches `^[A-Za-z0-9_ .,;:()\[\]{}<>=+\-*/^~\n?]*$` and contains none of `#`, `"`,
   `'`, `\`, `//`, `/*`, `*/`, `|`, `&`, `!`, `%`, `@`, `$`, a backtick, a tab, a carriage return; the words `print`,
   `reject`, `fatal_error`, `target`, `include`, `functions`, `profile` never appear as tokens. (`?` appears only in
   a size's conditional; `:` only there, in a loop's range and in the centred effects' `[:, k]`.)
2. **The allow-list**: every identifier followed by `(` (except the keyword `for`) is a catalogue name — a
   distribution's density or generator, a `language.functions` entry, an `internal_functions` entry — and every name
   after `~` is a distribution's statement; checked by a lexical scan of the text, independent of how it was written.
   The writer itself has one method that writes a call, and it refuses a name outside the list.
3. **stanc's `--info`** (`stan-compile`'s compile Job, and the stanc check in CI): the variables of each block, with
   their dimensions, equal the manifests; the functions and distributions stanc reports are in the allow-list
   (`CrossCheck(info, program) []Mismatch`; the format of `--info` is assumed from the stanc documentation, the stanc
   check reads the pinned version's).

## Determinism

- `Generate` reads only the validated model (whose `Order`, plates and slots are canonical: the graph spec) and the
  catalogue snapshot; it iterates no map without sorting its keys; it uses no time, no randomness, no environment.
- **Same model, same bytes**: tested by generating every golden case twice, after shuffling the graph's JSON (plates,
  nodes, keys) and after moving every box and renaming every label.
- `CodegenVersion` (an integer constant) is stored with each program (`model-store`). Every golden file records the
  version it was written with: **a golden program that changes while `CodegenVersion` does not fails the suite**
  («bump CodegenVersion»). The compile cache's key holds the program's sha256 (the map), so a new version compiles
  anew by itself.

## The golden corpus

`testdata/golden/<case>.graph.json` → `<case>.stan` (exact bytes), `<case>.mapping.json`, `<case>.manifest.json`,
written by Go with `BAYES_GOLDEN_WRITE=1` and reviewed by hand, compared byte for byte by `go test`.

| Case | What it pins |
|---|---|
| `glm_linear`, `glm_logistic`, `glm_poisson`, `glm_negbin` | the four GLM templates of the map (linear-predictor node, standardized predictors, default priors as the templates will write them) |
| `hier_intercepts` | varying intercepts, non-centred by the affine transform |
| `hier_slopes` | varying intercepts and slopes, correlated, LKJ, non-centred; and `hier_slopes_centred` |
| `hier_nested` | schools in districts: a lifted index, two non-centred plates |
| `ts_ar2`, `ts_random_walk`, `ts_local_level` | `lag` on a series, the mean apart from the coefficients as the template writes it (`mu + phi1 * (lag(y, 1) - mu) + phi2 * (lag(y, 2) - mu)`); `lag` with a drift (`lag(y, 1) + delta`, no state); a non-centred latent state with a normal likelihood around it; and `ts_local_level_centred` (correction of 2026-11-23 from the step-3/4 specs: `ts_random_walk` was described as «a latent state non-centred», which is `ts_local_level`'s) |
| `free_truncated_hyper` | a half-normal whose scale is a parameter: `T[0.0, ]` scalar; and on a plate: the loop with a local argument |
| `free_category` | treatment contrasts: `K − 1` coefficients, `Full_` with `append_row` |
| `free_explicit_noncentred` | a location through a `transformed` node: the explicit `Z_` form |
| `free_transformed_data` | `log(x)` of a data node in `transformed data` |
| `free_binomial` | `binomial_logit` with trials; `free_int_predictor` with `to_vector` |
| `names_reserved` | nodes named `lower`, `model`, `normal`, `x_lpdf`: generated identifiers, the mapping's `renamed` |
| `names_unnamed` | nodes without names referenced by `ref` |
| `extension_fake` | the test extension: data after `Mode`, written declarations sized by `Mode`, the nested block |

The ten templates' graphs are written here by hand first; `templates` (step 3) writes its own and a test of that
module checks that they give these programs. **The golden graphs of the ten template cases are then the templates'
own** (`bayes-templates.md`, lot T1 replaces the hand-written ones, reviewed), so that the two cannot drift; the
programs change only by the priors' numbers, and `CodegenVersion` does not change for it (the generator is the same).
(Correction of 2026-11-23 from the step-3/4 specs.)

## Tech stack

- **Go** 1.27, standard library only (`strings.Builder`, `strconv`, `crypto/sha256`, `encoding/json`, `sort`); no new
  module in `go.mod`. Depends on `internal/bayescatalog`, `internal/bayesexpr`, `internal/bayesgraph`,
  `internal/bayescontract`.
- **CI**: the stanc check downloads `linux-stanc` of stanc3 v2.40.0 by URL and refuses it unless its sha256 is
  `29306a35…e6f4` (the value `bayes-image` measured); no C++ compile in CI (that is the image's, `bayes-image` and
  `stan-compile`).
- **The image, by hand**: simulation-based calibration (Talts et al. 2018, the map) with the group's CmdStan.

## Commands

```bash
cd gateway-go && go test ./internal/bayescodegen/...                                  # golden, properties, scans
cd gateway-go && BAYES_GOLDEN_WRITE=1 go test ./internal/bayescodegen/ -run TestGolden  # rewrites (reviewed by hand)
# the stanc check, as CI runs it (stanc downloaded and its sha256 checked first)
cd gateway-go && BAYES_STANC=/path/to/linux-stanc go test ./internal/bayescodegen/ -run TestGoldenStanc -v
cd gateway-go && go test ./internal/bayescodegen/ -run '^$' -fuzz '^FuzzGenerate$' -fuzztime 10m   # by hand, alone
cd gateway-go && go test ./internal/bayescodegen/ -run '^$' -bench . -benchmem          # recorded, not asserted
```

## Project structure

```text
gateway-go/internal/bayescodegen/
  codegen.go        Generate, Program, CodegenVersion
  ident.go          the identifier rule, the mapping table
  writer.go         the block writer: indentation, the call allow-list, numbers
  data.go           the data block and the data manifest
  params.go         parameters, bounds, truncation, affine and explicit non-centred, categories
  effects.go        correlated effects, latent states
  model.go          locals, priors, likelihoods; the EmitEnv
  generated.go      correlations, the extension point (Extension, Scope, Decl, Stmt)
  check.go          the alphabet, the scan, CrossCheck
  *_test.go         per file; golden_test.go; stanc_test.go (skipped without BAYES_STANC); property_test.go; fuzz_test.go
  testdata/golden/  the cases above
  testdata/fuzz/FuzzGenerate/
.github/workflows/bayes-codegen.yml   go test, then the stanc check
```

## Code style

Text written by Go from values it checked, as the image's Dockerfile is (verified,
`internal/scriptimage/scriptimage.go`, `wheelSteps`):

```go
	for _, w := range wheels {
		arch, ok := machines[w.Machine]
		if !ok {
			return "", ValueError(fmt.Sprintf("«%s» is not a machine a wheel is built for (x86_64 or aarch64)", w.Machine))
		}
		if !url.MatchString(w.URL) || strings.ContainsAny(w.URL, "#`$") {
			return "", ValueError(fmt.Sprintf("«%s» is not the address of a wheel", w.URL))
		}
```

This module's writer in the same voice (a sketch; lot S0 writes it): one way to write a call, and it refuses what
the catalogue does not list.

```go
// call writes a call of a Stan function or density. The name comes from the catalogue, never from
// the graph: a name the allow-list does not hold is a bug of the codegen, and no program is returned.
func (w *writer) call(name string, args ...string) (string, error) {
	if !w.allowed[name] {
		return "", fmt.Errorf("bayescodegen: %q is not a function the catalogue lists", name)
	}
	return name + "(" + strings.Join(args, ", ") + ")", nil
}
```

## Testing strategy

All inputs are small graphs (at most `max_nodes`); no data, no sampling in the Go suite.

- **Golden** (`golden_test.go`): every case of «The golden corpus» byte for byte; the mapping and the manifests; the
  version rule.
- **The stanc check** (`stanc_test.go`, in CI): every golden program parses and type-checks with the pinned stanc,
  and its `--info` matches the manifests (`CrossCheck` empty). This is where every «assumed» of this spec about Stan's
  syntax — affine transforms with vector arguments, the conditional operator in a size, `append_row(real, vector)`,
  the row-vector literal, `to_vector` of a matrix row — becomes «run».
- **Properties** (`property_test.go`): `model-graph`'s seeded generator of valid graphs (the four families, random
  sizes, names and orders) gives programs that pass the alphabet, the scan and the identifier rule; generating twice
  gives the same bytes; shuffling the graph's JSON, moving boxes and changing labels gives the same bytes; renaming
  one node changes only that identifier's occurrences (a diff of the two texts).
- **Fuzzing**: `FuzzGenerate(seed []byte)` builds a valid graph from the fuzzer's bytes through the same generator
  and asserts the three checks and that `Generate` never panics; its seeds (the golden graphs) run in every
  `go test`; a 10-minute run is lot S3's acceptance, by hand, alone.
- **Simulation-based calibration** (Talts et al. 2018; by hand on the image, lot S4, one heavy job at a time): three
  small models — `glm_linear`, `hier_intercepts` (non-centred) and `ts_ar2` — each with 200 parameter sets drawn from
  the prior, data simulated from them, a short NUTS run (4 chains × 250 draws, thinned to 100), the rank of each true
  value among the draws; the ranks' uniformity is tested by a chi-square test over 20 bins with its p-value against the
  suite's alpha 0.001, Holm-adjusted over the parameters of the three models (a test of the software, not a verdict
  of any report). Times, p-values and ranks' histograms recorded in «As built».
- **Regression**: the language's and the graph's suites (this module is the language's first `EmitEnv`); the
  catalogue's.

## Boundaries

- **Always**: every expression through `Emit`; every call through the allow-list; the identifier rule; no comment;
  the program from the validated model only; golden files updated (and reviewed) with every change of the output;
  `CodegenVersion` bumped with it.
- **Ask first**: a new block, a new form (another non-centring, `_glm` likelihoods, vectorized truncation), a
  function beyond the catalogue's lists, any change to the identifier rule.
- **Never**: a label, a note or any person's text in the program; a program received from the browser; `#include`,
  `functions`, `print`, `reject`, `fatal_error`, `target`; a stanc flag (`--include-paths`, `--allow-undefined`); a
  per-row quantity written by the program; a stored program rewritten in place.

## Success criteria

1. Every golden case is generated byte for byte, twice, under shuffles, moved boxes and changed labels.
2. Every golden program passes the stanc check in CI (parse, types, `--info` equal to the manifests).
3. No program of the golden corpus, the properties or 10 minutes of fuzzing fails the alphabet, the scan or the
   identifier rule; `Generate` never panics.
4. The written quantities of every GLM case do not depend on N (the manifest has no per-row variable); a latent
   state's do, and only by its N values.
5. The extension point's placement is pinned by `extension_fake`.
6. The calibration of the three models is recorded with its p-values (lot S4).
7. No new module in `go.mod`.

## Lots

| Lot | Objective | Files | Accepted when | Agent |
|---|---|---|---|---|
| **S0** | Writer, identifiers, mapping, data block and manifest, scalar parameters, priors, truncation, GLM likelihoods, the alphabet and the scan | `codegen.go`, `ident.go`, `writer.go`, `data.go`, `params.go` (part), `model.go`, `check.go`, golden GLM and `names_*` | golden green | **opus** (the safety of the output) |
| **S1** | Plates: affine and explicit non-centred, centred, lifts, categories, the truncation loop | `params.go`, golden `hier_*` (but slopes), `free_*` | golden green | sonnet |
| **S2** | Correlated effects, latent states, `lag`, `transformed data`, the extension point | `effects.go`, `generated.go`, golden `hier_slopes*`, `ts_*`, `extension_fake` | golden green | sonnet |
| **S3** | The stanc check in CI, `CrossCheck`, properties, `FuzzGenerate` | `stanc_test.go`, `property_test.go`, `fuzz_test.go`, the workflow | CI green with stanc; 10 minutes of fuzzing | sonnet |
| **S4** | Simulation-based calibration on the image | a script under `testdata/sbc/`, «As built» | recorded | sonnet (by hand, alone) |

S0 needs `expression-language` E2 (`Emit`) and its security review (E4: «before `stan-codegen` calls `Emit`»), and
`model-graph` M2; S1 and S2 after S0, in parallel; S3 after S2; S4 after `bayes-image` B0 and `bayes-contract` K3.

## Decisions of 2026-11-23 (the answers to this spec's questions)

| Question | Decision | By |
|---|---|---|
| When Tabularia's codegen changes, which program does a saved version run? | **Always the stored program**: a change of the generator reaches a model only when someone saves a new version; regeneration at each run and an administrator's «regenerate all» are not built | Owner, 2026-11-23 |

## Decisions of 2026-11-24 (mockups)

| Question | Decision | By |
|---|---|---|
| The example's coefficient named `beta` | Aligned with «The rule»: a name equal to a distribution's gets a generated identifier (`V_n7`), as the templates avoid with `b_x` | Technical, 2026-11-24 |

## Open questions

None.
