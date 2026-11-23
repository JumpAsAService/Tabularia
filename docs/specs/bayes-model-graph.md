# Spec: the model graph of the Bayesian editor (`model-graph`)

Status: **approved by the owner on 2026-11-23.** It specifies the module `model-graph` of
`docs/specs/bayesian-editor-map.md` (**the map**), step 1 of its order of construction, after
`distribution-catalog` (`bayes-distribution-catalog.md`) and `expression-language`
(`bayes-expression-language.md`), shaped by the interview of 2026-11-23 (**the answers**, numbered as in the
map's «Interview answers»). Nothing of it is built. Decisions marked **(owner)** are the owner's; the rest are
this spec's, to confirm; those marked «spec decision, 2026-11-23, owner may revisit» are this spec's. The
owner answered this spec's questions on 2026-11-23 («Decisions of 2026-11-23»); no question is open.

Words: **verified** = read in this repository's code on 2026-11-23; **assumed** = not run here, checked by the
lot or module named beside it.

## Objective

A Bayesian model drawn in the editor is a **graph**: data, parameters with their priors, values computed from
them, the likelihood of the observed data, the groups of a hierarchical model drawn as boxes. This module is
that graph as data, and the rules that make it a model Stan can sample:

- **the canonical JSON** of a model: its node kinds, its plates (boxes), names and labels, the slots where a
  value is typed, the format version; what `model-store` saves, what `graph-editor` draws, what `stan-codegen`
  reads;
- **the validation**: types, shapes, supports, a DAG, every parameter with a proper prior, plates consistent,
  every data variable used, the limits; each finding a code, a severity, the node and the slot it is about, and
  a message in five languages written for a person who does not know Bayesian statistics (answer 1);
- **one authority**: the validator in Go, in the gateway; a **mirror in TypeScript** that gives the same
  findings while the person draws, never trusted;
- **the validated model**: the graph with its expressions typed, its parameters' ranges and truncations, its
  roles and its order, handed to `stan-codegen`, `prior-preview` and `flow-node`.

It does not produce Stan code (`stan-codegen`), save anything (`model-store`), draw anything (`graph-editor`)
or bind data (`flow-node`).

## Decisions

| Question | Decision | By |
|---|---|---|
| Hierarchies | **Plates**: boxes, nested for nested levels; the **non-centred** form by default, a switch per level | Owner (answer 4) |
| Names | A node's **name** is used in code when it matches `[a-z][a-z0-9_]*`, is not reserved in Stan and does not end in `__`; otherwise a generated identifier with a mapping table; the **label** (free text) never enters code | Owner (answer 5); here: the lexical check and the field; the identifier choice is `stan-codegen`'s |
| Data | Bound only in the flow node; the model declares typed data variables; a text column is a group index or a **category** coded by treatment contrasts with the first level as reference; real predictors **standardized by default**, said in the report, coefficients on both scales | Owner (answer 6) |
| Priors and data | A prior never depends on data: the model's space has no data (answer 6), and the browser shows the priors' marginals without any (answer 13) | Owner (answers 6, 13), the rule: spec |
| Expressions | Every slot may hold a typed expression of `expression-language` | Owner (answer 3) |
| Distributions | Those of the catalogue (answer 2), with LKJ and the multivariate normal reachable only through a **correlated-effects** node | Owner (answer 2), the node: spec |
| Edges | **Not stored.** They are derived from what the slots reference, so the drawing and the model cannot disagree | Spec |
| Priors and hyperpriors | **Roles** of parameter nodes, derived (a parameter whose value feeds another prior is a hyperprior), not node kinds | Spec (refines the map's list of kinds) |
| Authority | The Go validator; the TypeScript mirror for feedback only; a shared corpus | Spec (the map) |
| An unused data variable | **An error** | Owner (2026-11-23) |
| Coefficients on both scales | **Only for the terms of a `linear_predictor` node**; in free expressions the standardized scale only, with the predictor's mean and standard deviation said | Owner (2026-11-23) |
| Data on a group plate | **Allowed** for real and integer data, the runner refusing a column that varies within a unit | Spec decision, 2026-11-23, owner may revisit |
| `graph.form_centred` widened; the mirror exports `priorPlan(model)` | A bounded parameter on a non-centred plate and a state with a non-location-scale `init` stay centred, with the warning; the TypeScript mirror exports the plan `prior-preview` draws from («The TypeScript mirror») | Correction of 2026-11-23 from the step-2 specs (`bayes-stan-codegen.md`, `bayes-prior-preview.md`) |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| Strict decoding of a JSON document | `gateway-go/internal/modelgroups/groups.go` decodes each `recipe.json` with `dec.DisallowUnknownFields()` and names the file and the field in its errors; `api/home.go` does the same | The graph is decoded the same way |
| A bounded request body | `http.MaxBytesReader` in `api/share_links.go`, `api/data_apis.go` (1 MiB), `api/models_licence.go` (64 KiB) | The routes of `model-store` read a graph through `MaxBytesReader(max_graph_bytes)` |
| Cases read by two implementations | `backend/tests/fixtures/models/params_cases.json`, read by the engine and by `gateway-go/internal/modelcatalog/modelcatalog_test.go` | The graph corpus, read by the Go and the TypeScript tests |
| A graph canvas's model in the editor | `frontend/composables/useFlowModel.ts`, `FlowEditor.vue` with `@vue-flow/core` 1.48.2 (installed) | The editor's graph is this module's JSON; the canvas (`graph-editor`) draws it |
| The vocabulary and limits | `distribution-catalog` (distributions, intervals, functions, limits) | Read from the catalogue |
| Typed expressions | `expression-language` (`Parse`, `Check` against an `Env`, `Interval`) | This module is the `Env` |

## The canonical JSON

### An example

Pupils' scores in schools, a varying intercept per school, one predictor:

```json
{
  "format_version": 1,
  "plates": [
    {"id": "rows", "kind": "rows", "label": "Pupils"},
    {"id": "p1", "kind": "group", "label": "School", "parent": null, "key": "n3", "form": "non_centred"}
  ],
  "nodes": [
    {"id": "n1", "kind": "data", "name": "score", "label": "Test score", "plate": "rows", "type": "real"},
    {"id": "n2", "kind": "data", "name": "hours", "label": "Hours of study", "plate": "rows", "type": "real",
     "standardize": true},
    {"id": "n3", "kind": "data", "name": "school", "label": "School", "plate": "rows", "type": "group_index",
     "target": "p1"},
    {"id": "n4", "kind": "parameter", "name": "mu", "label": "Mean of the schools", "plate": null,
     "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "10"}}}},
    {"id": "n5", "kind": "parameter", "name": "tau", "label": "Spread between schools", "plate": null,
     "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "5"}}},
     "bounds": {"lower": 0}},
    {"id": "n6", "kind": "parameter", "name": "alpha_school", "label": "Mean of a school", "plate": "p1",
     "prior": {"dist": "normal", "args": {"mu": {"expr": "mu"}, "sigma": {"expr": "tau"}}}},
    {"id": "n7", "kind": "parameter", "name": "beta", "label": "Effect of one more hour", "plate": null,
     "prior": {"dist": "normal", "args": {"mu": {"expr": "0"}, "sigma": {"expr": "1"}}}},
    {"id": "n8", "kind": "parameter", "label": "Spread within a school", "plate": null,
     "prior": {"dist": "exponential", "args": {"beta": {"expr": "1"}}}},
    {"id": "n9", "kind": "likelihood", "label": "Scores", "observed": "n1", "dist": "normal",
     "args": {"mu": {"expr": "alpha_school[school] + beta * hours"}, "sigma": {"ref": "n8"}}}
  ],
  "layout": {"p1": {"x": 0, "y": 0, "w": 420, "h": 260}, "n6": {"x": 40, "y": 60}}
}
```

`n8` has no name (its label is «Spread within a school»): it cannot be written in an expression, so the
likelihood's `sigma` slot refers to it by id (answer 5). `tau` is bounded below by 0 under a normal prior: a
half-normal («Bounds and truncation» of the catalogue); its prior's arguments are constants, so the program
needs no `T[…]`. `alpha_school` is non-centred because its plate's `form` says so.

### The document

| Key | Type | Meaning |
|---|---|---|
| `format_version` | integer | 1. A newer version is refused by an older gateway; an older one is upgraded on read («Versions») |
| `plates` | array | The boxes, at least the rows' |
| `nodes` | array | The nodes |
| `layout` | object | Positions and sizes for the canvas, by node or plate id; **not part of the model** (excluded from the semantic hash) |

Ids: `^[a-z][a-z0-9]{0,15}$`, unique across nodes and plates; `rows` is the rows plate's. A
correlated-effects component is addressed as `<node id>.<component id>`. Ids are made by the editor and never
shown; they never reach a program except inside a generated identifier (`stan-codegen`).

### Plates

| `kind` | Fields | Meaning |
|---|---|---|
| `rows` | `label`, `series` (optional) | The observations: one unit per row the flow node binds. Exactly one, id `rows`. With `series` = `{"time": <time data node>, "by": <group plate or null>}` the rows are a time series (one per unit of `by`), ordered by `time`: `lag` and latent states are allowed on them |
| `group` | `label`, `parent` (a group plate or null), `key` (a `group_index` data node on rows whose `target` is this plate), `form` (`non_centred` default, or `centred`) | A level of a hierarchy: J units, J known only when data are bound. `parent` nests it (schools inside districts); the runner checks that every unit of the child belongs to one unit of the parent. `form` is the switch of answer 4 |
| levels (implicit) | — | Made by each `category` data node: id `levels:<node id>`, K units (the column's levels). A parameter on it is a vector of K − 1 free values, the first level's being 0 (treatment contrasts, answer 6). Not stored; drawn as a badge, not a box |

The canvas draws group plates as boxes nested by `parent` (answer 4) and the rows plate as its own box; the
`key` data node links them.

### Nodes

Every node has `id`, `kind`, `label` (required), `name` (optional), `plate` (a plate id, or `null` for a
single value), `note` (optional). The kinds:

| `kind` | Fields | Plates allowed | What it is |
|---|---|---|---|
| `data` | `type`: `real`, `int` (with `support`: `any`, `nonnegative`, `binary`), `group_index` (with `target`), `category`, `time`; `standardize` (real only) | rows; a group plate for `real` and `int` (a column constant within each unit, checked by the runner) | A variable the flow node binds to a column (answer 6) |
| `parameter` | `prior`: `{dist, args}`; `bounds`: `{lower?, upper?}` (numbers) | scalar, a group plate, a levels plate | An unknown with its prior |
| `transformed` | `value`: a slot | scalar, rows, a group plate | A value computed from others (a local variable of the program when per row) |
| `linear_predictor` | `intercept`: a slot or null; `terms`: `[{data, coef}]` | rows, a group plate | `intercept + Σ coef × data`, the GLM's form; the one place where coefficients are reported on both scales («Standardization») |
| `likelihood` | `observed`: a data node; `dist`, `args` | (the observed node's plate) | The distribution of the observed data |
| `state` | `process`: `random_walk`; `init`: `{dist, args}`; `scale`: a slot; `form` | rows that are a series | A latent series: x₁ from `init`, xₜ ~ normal(xₜ₋₁, scale); with a likelihood around it, the local-level model |
| `correlated_effects` | `components`: `[{id, name, label, mean, scale}]`; `correlation`: `{name, label, eta}` | a group plate | K effects per unit (an intercept and slopes), multivariate normal with scales and an LKJ(eta) correlation (answer 2); each component a value over the plate, written by its name |

A **slot** is `{"expr": "<text>"}` (a typed expression; a number is an expression) or `{"ref": "<id>"}` (a node
or component without a name: answer 5). Nothing else.

### Names and labels (answer 5)

- **name**: optional; when present, it matches the catalogue's pattern `[a-z][a-z0-9_]*`, has at most
  `max_name_bytes` (40), does not end in `__`, is unique among the nodes and components, and is not a function
  of the expression language (`exp`, `log`…, `lag`). A name that is reserved in Stan (`lower`, `model`, `int`,
  `target`, a `_lpdf` suffix…) **is accepted**: the program gets a generated identifier for it, with the
  mapping table (`stan-codegen`, answer 5). The editor proposes a name from the label (letters folded to ASCII,
  lower case, other characters to `_`) that the person may change or remove.
- **label**: required; 1 to `max_label_chars` (200) code points of any language; no control character (C0,
  C1), no direction control (U+202A–U+202E, U+2066–U+2069), no U+FEFF. It is shown on the canvas, in the
  mapping table, in the report; it never enters a program, not even a comment (map).
- A node **without a name** can be referenced only through a `ref` slot (by dragging an edge), not in a typed
  expression; the message says so and proposes a name.

### Data and standardization (answer 6)

- `real`: a number column. As a predictor it is **standardized** by default (`standardize: true`, written by the
  editor when the node is made, switchable per node): the runner computes its mean and standard deviation on
  the rows of the run and the program receives (x − mean)/sd; the report says it. A real data node that is
  observed by a likelihood is never standardized.
- `int`: integers (`support` `any`, `nonnegative`, `binary`); never standardized.
- `group_index`: a text or integer column whose distinct values are the units of its `target` plate (1…J in
  the runner, the labels travelling as data).
- `category`: a text column used as a predictor; its levels in text order, the **first the reference**; it makes
  a levels plate (answer 6).
- `time`: the order of a series; not a number in any expression.

**Coefficients on both scales.** A `linear_predictor` term whose data is a standardized real gives the
coefficient on the standardized scale (what the program samples) and on the column's own scale (coef / sd,
the intercept shifted by Σ coef × mean / sd): the validated model marks the pair, the report writes both. In a
free expression (`a + b * hours`) the coefficient stays on the standardized scale only, and the report says
that the predictor was standardized and with which mean and sd (owner, 2026-11-23).

### Priors, bounds and truncation

- Every `parameter` has a `prior` whose `dist` the catalogue lists with the use `prior` (LKJ only inside
  `correlated_effects`; the internal distributions never), with exactly its arguments.
- **No prior depends on data**: a parameter's prior, a state's `init` and `scale`, a component's `mean` and
  `scale` may reference parameters and values computed from parameters, never a data node, directly or through a
  `transformed` or `linear_predictor` node.
- `bounds` are numbers (finite, at most `max_abs_literal` in magnitude, `lower < upper`). The **range** is the
  prior's support ∩ the bounds, per the catalogue's «Bounds and truncation»; an empty range is an error; a
  bound on a distribution that cannot be truncated is an error. The validated model carries, per parameter,
  the range, `truncated`, and whether the program needs `T[…]` (an argument is not a constant: rule 4 of the
  catalogue).
- Uniform's arguments and LKJ's `eta` are constants (a number, possibly negated).
- **Proper priors only**: every parameter has one, and every catalogue prior is proper with valid arguments,
  so no improper prior can be drawn.

### Validated model (what the other modules read)

```go
type Model struct {
	Plates    []Plate            // rows, groups by depth then id, levels by their category's id
	Order     []NodeID           // a topological order, ties by id: the program's order of declarations
	Slots     map[SlotKey]Typed  // every slot: typed AST or ref, its context plate, its interval
	Params    map[NodeID]Param   // range, truncated, truncation in the program, role (prior | hyperprior), non-centred
	Edges     []Edge             // derived: who references whom (the canvas draws them)
	Lifts     []Lift             // the group indices lifted to a plate (schools → districts): data arrays to build
	Lags      []int              // the lags a series needs
	Bindings  []Binding          // the data nodes the flow node binds: name or label, type, plate, standardize
	Pairs     []ScalePair        // linear-predictor coefficients reported on both scales
	Findings  []Finding
}
```

`stan-codegen` writes the program from it; `prior-preview` draws the parameters in `Order` (ancestral
sampling: hyperparameters first); `flow-node` lists `Bindings`; `graph-editor` draws `Edges` and `Findings`.

## Validation

A **finding** is `{code, severity, node, slot, args}`; `severity` is `error` (no code can be generated, the
model cannot be run) or `warning` (a fact the person should know; nothing is blocked). Messages in five
languages (`bayes.graph.<code>`), written for a person who does not know Bayes, saying what to change. All
findings are reported, sorted by (node id, slot, code), not only the first.

| Code | Severity | Rule |
|---|---|---|
| `graph.format` | error | Known `format_version`; no unknown key; the right JSON types (strict decoding) |
| `graph.too_big` | error | Within `max_graph_bytes`, `max_nodes`, `max_plates`, `max_total_expression_nodes` |
| `graph.id` | error | Ids well formed and unique; `rows` only for the rows plate |
| `graph.name`, `graph.name_taken`, `graph.name_function` | error | Names lexically valid, unique, not a function of the language |
| `graph.label`, `graph.note` | error | Labels present, of allowed characters and length; notes within `max_note_chars` |
| `graph.rows` | error | Exactly one rows plate |
| `graph.plate_parent` | error | A parent is a group plate; no cycle; depth within `max_plate_depth` |
| `graph.plate_key` | error | Each group plate has exactly one key: a `group_index` on rows targeting it; every `group_index` targets a group plate |
| `graph.series` | error | `series.time` is a `time` data node on rows; `series.by` a group plate or null |
| `graph.plate_empty` | warning | A group plate with nothing on it |
| `graph.node_plate` | error | The node's plate exists and is allowed for its kind (table «Nodes») |
| `graph.data_type` | error | A known type and support; `group_index`, `category`, `time` only on rows |
| `graph.standardize` | error | `standardize` only on a real data node that no likelihood observes |
| `graph.prior` | error | A parameter's prior from the catalogue, with use `prior`, with exactly its arguments |
| `graph.slot` | error | A slot is one `expr` or one `ref`; a `ref` names an existing node or component that is a number |
| `expr.*` | error | The expression's own error (`expression-language`), with the node and the slot |
| `graph.slot_shape` | error | The slot's value fits where it goes: scalar where a scalar is needed (bounds, `eta`, a state's `scale`, a component's `mean` and `scale`), scalar or over the context plate elsewhere |
| `graph.slot_interval` | error | The slot's interval (`Interval`) is disjoint from what the argument needs (a negative literal for `sigma`) |
| `graph.slot_maybe` | warning | The interval overlaps but is not inside it («Stan will reject a draw where `a - b` is not positive») |
| `graph.prior_data` | error | A prior (or `init`, `scale`, `mean`) reaches a data node |
| `graph.constant` | error | Uniform's arguments, `eta` and bounds are constants |
| `graph.bounds` | error | Bounds finite, ordered, within `max_abs_literal`; the range not empty; truncation possible |
| `graph.cycle` | error | The derived dependencies are acyclic (a `lag` counts as a dependency: a value cannot lag itself; a latent recursion is a `state` node); the message lists the cycle's names in order |
| `graph.likelihood` | error | At least one likelihood; its observed node is a data node observed once; the outcome fits the distribution (real ↔ `normal`; `binary` ↔ `bernoulli_logit`; `nonnegative` or `binary` ↔ `poisson_log`, `neg_binomial_2_log`; `binomial_logit`'s `N` a `nonnegative` integer data node on the same plate) |
| `graph.self_predictor` | error | An observed node used to predict itself without `lag` |
| `graph.unused_data` | error | A data node neither referenced, observed, a plate's key nor a series' time (every data node must be bound; owner, 2026-11-23) |
| `graph.unused_parameter` | warning | A parameter on no path to a likelihood: «its posterior is its prior» |
| `graph.form_centred` | warning | On a `non_centred` plate, a parameter whose prior is not a location-scale one (catalogue `location_scale`), or a parameter with `bounds` (Stan's affine non-centring cannot be combined with bounds); a `non_centred` `state` whose `init` is not location-scale: «this one stays centred» (the last two cases: correction of 2026-11-23 from the step-2 specs) |
| `graph.effects` | error | A correlated-effects node: 2 to `max_effect_components` components, names valid and unique, scales positive scalars, `eta` a positive constant, on a group plate |
| `graph.state` | error | A state only on rows that are a series; `init` a proper prior; `scale` a positive scalar |
| `graph.terms` | error | A linear predictor's terms: a real or integer data node with a scalar parameter, or a category with a parameter on its levels plate; no data node twice |

Every rule of this table, and every `expr.*` code, has at least two cases in the corpus (one that trips it, one
just beside it that does not).

## Versions

`format_version` is 1. A later format comes with an upgrade function `vN → vN+1` (pure, tested on the corpus of
the old version) and a version the gateway refuses to read if it is newer than it knows. A saved version is
never rewritten in place (`model-store` keeps what was saved; the upgrade runs on read).

**Canonical form.** `Canonical(g)` writes plates (rows first, then by id), nodes by id, keys in the order of
the tables above, slots as typed, numbers as Go's encoder writes them (the shortest form that reads back to the
same double). The **semantic hash** is the sha256 of the canonical form without `layout`: moving a box changes
nothing, renaming a label changes the hash (it changes the mapping table and the report). `model-store` and
`stan-codegen` use it; the mirror does not compute it.

## The TypeScript mirror

`frontend/composables/bayesGraph.ts` validates the editor's graph with the same rules and codes, calling
`bayesExpr.ts` for the expressions, after each change (debounced by `graph-editor`). It never computes the
canonical form, the hash, or anything a program depends on. On save the gateway validates again; its findings
replace the mirror's.

It also exports **`priorPlan(model)`** for `prior-preview` (`bayes-prior-preview.md`, «The plan»; correction of
2026-11-23 from the step-2 specs): the nodes a prior's marginal needs, ancestors first in `Order`, with their
distributions, their slots' typed ASTs, references and constants, their ranges and truncations, and the count of
error findings on each node or its ancestors. Plain JSON, no data, no label, nothing a program depends on; it never
leaves the browser.

## Memory

A graph is metadata, bounded by `max_graph_bytes` (256 KiB) and `max_nodes`: it is held whole, by design, in
the gateway and in the browser. The rule of 2026-11-22 («never everything in RAM») concerns data and draws;
nothing here grows with the rows of a flow. The routes that receive a graph read at most `max_graph_bytes`
(`http.MaxBytesReader`), refusing a larger body before decoding it.

## Tech stack

- **Go** 1.27, standard library only (`encoding/json` with `DisallowUnknownFields`, `crypto/sha256`); no new
  module. Depends on `internal/bayescatalog` and `internal/bayesexpr`.
- **TypeScript** in the Nuxt 3.21 frontend; no new dependency; tests with `node --test` under Node 22.

## Commands

```bash
cd gateway-go && go test ./internal/bayesgraph/...
cd gateway-go && BAYES_CORPUS_WRITE=1 go test ./internal/bayesgraph/ -run TestCorpus     # rewrites the expected findings (reviewed by hand)
cd gateway-go && go test ./internal/bayesgraph/ -run '^$' -fuzz '^FuzzDecode$' -fuzztime 10m   # by hand, alone
cd frontend && npm i --no-save @intlify/message-compiler@9.14.5 && npm test
cd frontend && npx nuxi prepare && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
```

## Project structure

```text
gateway-go/internal/bayesgraph/
  graph.go          the document's types, strict decoding, Canonical, SemanticHash
  env.go            the expression environment of a slot (Env of expression-language)
  validate.go       the rules, in the order of the table
  model.go          the validated model
  version.go        format versions and upgrades
  *_test.go         per file; corpus_test.go; property_test.go; fuzz_test.go (FuzzDecode)
  testdata/graphs/<case>.json      a graph and its expected findings (by hand, reviewed)
  testdata/fuzz/FuzzDecode/        inputs the fuzzer found, kept as seeds
frontend/composables/bayesGraph.ts
frontend/tests/bayes-graph.test.mjs    reads ../gateway-go/internal/bayesgraph/testdata/graphs/*.json
frontend/i18n/locales/*.gen.ts         bayes.graph.<code>
```

## Code style

A document decoded strictly and every error naming where it is (verified,
`gateway-go/internal/modelgroups/groups.go`):

```go
		var r Recipe
		dec := json.NewDecoder(strings.NewReader(string(raw)))
		dec.DisallowUnknownFields()
		if err := dec.Decode(&r); err != nil {
			return nil, fmt.Errorf("%s/recipe.json: %v", cg.ID, err)
		}
		if r.Language != cg.Language {
			return nil, fmt.Errorf("%s/recipe.json: language %q, the catalogue says %q", cg.ID, r.Language, cg.Language)
		}
```

A rule of this module in the same voice (a sketch): it reports, it never repairs.

```go
// priorData: a prior never depends on data. The model's space has no data (answer 6), and the
// browser draws the priors' marginals without any (answer 13).
func (v *validator) priorData(n *Node) {
	for _, slot := range n.priorSlots() {
		if d, ok := v.reachesData(slot); ok {
			v.error(n, slot.Key, "graph.prior_data", Args{"data": v.nameOrLabel(d)})
		}
	}
}
```

## Testing strategy

All graphs are small (the largest has `max_nodes` nodes); no data is involved.

- **Unit**: each rule, with the cases of the table; the truncation flags against the catalogue's rule (constant
  arguments → no `T[…]`; a parameter argument → `T[…]`; a non-truncatable distribution with a bound → error);
  the lift of indices through nested plates; the roles (prior, hyperprior); the topological order and its
  ties.
- **The corpus** (`testdata/graphs/`): every rule twice; the templates' graphs added by `templates`; the map's
  four families (GLM, hierarchical with nested levels and correlated effects, AR(2) with `lag`, local level with
  a `state`); hostile documents (unknown keys, ids and names with confusable characters, a label with a
  direction control, 10,000 nodes, a 1 MB label, a cycle through a `transformed` node and a `lag`, a prior that
  reaches data through two `transformed` nodes). Go's findings are the expected ones; `go test` fails when the
  committed file differs from what Go finds.
- **Mirror equality** (`bayes-graph.test.mjs`): the TypeScript findings equal Go's on every case (code,
  severity, node, slot, args), as sets.
- **Properties**: a seeded generator of valid graphs (the four families with random sizes, names and orders)
  gives no error; shuffling the order of plates, nodes and keys gives the same canonical bytes, hash and
  findings; `Decode(Canonical(g))` = `g`; moving `layout` does not change the hash; one targeted mutation of a
  valid graph gives exactly its finding's code.
- **Fuzzing**: `FuzzDecode(data []byte)` never panics and stays within the limits; its seeds (the corpus) run in
  every `go test`; a 10-minute run is lot M3's acceptance, by hand.
- **Regression**: the expression language's suite (this module is its `Env`); the catalogue's.

## Boundaries

- **Always**: the gateway decodes and validates again whatever the browser sends; findings reported, never
  repaired; every finding in five languages; the corpus updated with every rule and every bug; the limits from
  the catalogue.
- **Ask first**: a new node kind or plate kind; a new `format_version`; a change of a finding's severity or
  meaning; storing edges.
- **Never**: a label in an identifier; data in a prior; a graph accepted from the browser without validation;
  a silent rename, reorder or fix of what the person drew; an improper prior.

## Success criteria

1. The map's four families can be drawn as graphs of this format and validate without error (the corpus holds
   them).
2. Every rule of «Validation» trips on its corpus case and not on its neighbour; Go and the mirror give the same
   findings on the whole corpus.
3. The truncation flags of every parameter of the corpus follow the catalogue's rule.
4. A graph's canonical bytes and hash do not depend on the order of its JSON; moving a box does not change the
   hash.
5. `FuzzDecode` runs 10 minutes without a finding (or what it found is corrected and kept as a seed).
6. No new module in `go.mod`, no new package in `package.json`.

## Lots

| Lot | Objective | Files | Accepted when | Agent |
|---|---|---|---|---|
| **M0** | Types, strict decoding, ids, names, labels, plates, `Canonical`, hash, versions | `graph.go`, `version.go`, tests | unit green | sonnet |
| **M1** | The `Env` of expressions, slots, priors, bounds, truncation flags, no data in priors | `env.go`, `validate.go` (part), tests | unit green | **opus** |
| **M2** | Likelihoods, linear predictor, states, correlated effects, the DAG, unused nodes, the validated `Model` | `validate.go`, `model.go`, tests | unit green | sonnet |
| **M3** | The corpus, properties, `FuzzDecode` | `corpus_test.go`, `property_test.go`, `fuzz_test.go`, `testdata/` | green; 10 minutes of fuzzing | sonnet |
| **M4** | The TypeScript mirror and the messages in five languages | `bayesGraph.ts`, `bayes-graph.test.mjs`, the locales (`bayes.graph`) | mirror equality; `vue-tsc` green | sonnet |

M0 can start with the catalogue's C0; M1 needs `expression-language` E1; M2 after M1; M3 and M4 after M2. None
is heavy except a fuzz run (alone).

## Decisions of 2026-11-23 (the answers to this spec's questions)

| Question | Decision | By |
|---|---|---|
| A data variable the model does not use | **An error** (`graph.unused_data`): every data node must be bound in the flow node, and binding a column that changes nothing is a trap | Owner, 2026-11-23 |
| Coefficients on both scales (answer 6) | **Only for the terms of a `linear_predictor` node**, where the intercept's shift is known; in free expressions the standardized scale only, the predictor's mean and standard deviation said in the report | Owner, 2026-11-23 |
| Data on a group plate (a column constant within each unit) | **Allowed** for real and integer data; the runner refuses a column that varies within a unit | Spec decision, 2026-11-23, owner may revisit |

## Open questions

None.
