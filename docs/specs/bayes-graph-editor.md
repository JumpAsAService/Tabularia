# Spec: the editor of Bayesian models (`graph-editor`)

Status: **written and approved by the owner on 2026-11-23.** It specifies the module `graph-editor` of
`docs/specs/bayesian-editor-map.md` (**the map**), step 4 of its order of construction, after the approved
`bayes-distribution-catalog.md` (**the catalogue spec**), `bayes-expression-language.md` (**the language spec**),
`bayes-model-graph.md` (**the graph spec**), `bayes-prior-preview.md` (**the preview spec**) and `bayes-stan-codegen.md`
(**the codegen spec**), and beside the drafts `bayes-templates.md` (**the templates spec**) and `bayes-model-store.md`
(**the store spec**). Nothing of it is built. Decisions marked **(owner)** are the owner's (the map's «The owner's
decisions» and «Interview answers», numbered as there: **the answers**); the rest are this spec's, to confirm; those
marked «spec decision, 2026-11-23, owner may revisit» are small choices the owner may change without reopening the
module. The owner answered this spec's question on 2026-11-23 («Decisions of 2026-11-23»); no question is open.

Words: **verified** = read in this repository's code on 2026-11-23; **assumed** = not run here, checked by the lot named
beside it.

## Objective

The space where a person **draws a Bayesian model**:

- **a space of its own**, as Scripting (owner): a list of the models one may see, and the editor of one model;
- **the canvas** (`@vue-flow/core`): the nodes of the graph, the edges the graph derives, the **plates as boxes**, nested
  for nested levels (answer 4), each with its switch non-centred / centred;
- **the palette**: the node kinds of the graph spec, dragged or added from the keyboard;
- **the inspector** of a node: its label, name, plate, its prior with **the chart of that prior beside its numbers**
  (`prior-preview`), redrawn while the person types; typed **expressions** checked by the TypeScript mirror as they are
  typed, the gateway's check authoritative at save;
- **the findings on the nodes** (errors and warnings of the graph's validator), and a list of them;
- **the Stan code**, read only, with the **mapping table** of its identifiers to the nodes' labels;
- **the templates as the empty canvas's proposal**, the free model one click away (answer 1);
- **explanations for a reader who does not know Bayesian statistics** (answer 1), as facts;
- **the keyboard** for everything, every shortcut reachable on a Mac and on a laptop without F, Home, End or Menu keys;
  five languages, the app's five themes and its tokens, the lucide icons the app already uses; **no new dependency**.

Its **first screen** comes early (lot G0): a template loaded on the canvas and the inspector of a prior with its chart
alive, on local data, before `model-store` exists, so the owner can see it.

It produces no program (`stan-codegen`), validates nothing on its own authority (`model-graph`), saves through
`model-store`'s routes, and binds no data (answer 6: only the flow node does).

## Decisions

| Question | Decision | By |
|---|---|---|
| Where it lives | A space of its own, as Scripting; in a flow, the node **Bayesian model** chooses a saved model (`flow-node`) | Owner |
| Where it sits in the top bar | **Scripting becomes a menu** holding «Scripting» (`g s`) and «Bayesian models» (`g y`), each shown by its own `needs` (`scripting`, `bayes`); a plain link when only one of them is visible (the bar has no room for one more item: measured for the Analysis menu, verified in `AppShell.vue`) | Owner (2026-11-23) |
| A graph with errors | **Not saved** (`model-store`: 422 with the findings, no version); warnings never block; the editor warns before leaving with unsaved changes | Owner (2026-11-23) |
| Who sees it | Whoever may use the group `bayes` (`useModelGroups()`: the group's `usable`); hidden on Docker Compose, as Scripting | The store spec's visibility; the Scripting tab's rule (verified) |
| The canvas | `@vue-flow/core` (1.48.2 installed), `@vue-flow/background`, `controls`, `minimap`, `node-resizer`, as `FlowEditor.vue`; a second canvas, never `FlowEditor.vue` itself | The map |
| Plates | **Vue Flow parent nodes**: a node's `plate` is the box it sits in, and dropping it in another box changes its plate; group plates nest by `parent`; the rows plate is a box of its own, linked to the group boxes through the key data nodes (the graph spec) | Owner (answer 4: boxes, nested), the form: spec |
| A node's position | `layout` in **canvas coordinates** (absolute), converted to Vue Flow's coordinates relative to the parent box when drawn | Spec decision, 2026-11-23, owner may revisit (the graph spec does not say; a correction proposes it) |
| Edges | **Drawn from the graph**, never stored (the graph spec): the mirror's `Edges`. Drawing an edge writes the target slot (the source's name, or a `ref` when it has none); removing an edge clears a slot that held only that reference | The graph spec, the gesture: spec |
| Colour | **Neutral nodes**: the kind is said by its icon and shape (plate notation: data filled, parameters round, the likelihood double-bordered); colour only for what it encodes — selection (`--accent`), errors (`--danger`), warnings (`--warning`) | DESIGN.md «The Colour Is Encoding Rule» (verified) |
| The expression fields | Plain inputs with the mirror (`bayesCheckExprText`) on every keystroke, the error's span underlined; no Monaco for one-line expressions | Spec (Monaco is for the program) |
| The Stan code | A **tab of the side panel**: Monaco read only (already a dependency, `useMonaco.ts` lazy), a small tokenizer of the program's alphabet; the mapping table **under** the code (Correction of 2026-11-24 from the mockups) | The map; the place: owner (2026-11-24) |
| The Stan code while the graph has errors | The **stored program of the last saved version**, with a notice that it is not the program of the changes and the errors listed (Correction of 2026-11-24 from the mockups) | Owner (2026-11-24) |
| A prior whose argument has an error | The chart keeps **the last valid form, dimmed**, with «fix μ to update it» (Correction of 2026-11-24 from the mockups) | Owner (2026-11-24) |
| The canvas' direction | **Left to right**, as the flow editor: a node's sources stand to its left (handles: target left, source right), the hyperparameters in a column at the left, the likelihood at the right (Correction of 2026-11-24 from the mockups) | Spec (2026-11-24, the mockups: a top-down drawing with side handles makes edges run backwards) |
| A «check now» button | **None**: the gateway's check runs by itself 600 ms after the last change the mirror finds without error, when the Stan code tab opens, and at save | Spec (2026-11-24, the mockups) |
| The program shown | Asked from the gateway (`POST /bayes-models/check`, the store spec), never written in the browser | The map, the codegen spec |
| History | Snapshots of the graph's JSON through `createFlowHistory` (`useFlowHistory.ts`, verified) | Spec |
| Copy and paste of nodes | **Not in the first release** (a name would have to be made unique, and a prior's references remapped) | Spec decision, 2026-11-23, owner may revisit |
| Screenshots | Chrome and Firefox, at **1440 px and 1280 px**, no secret readable (the owner's rules) | Owner |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| A tab shown only to whoever may use it | `composables/appKeys.ts`: `NAV_SECTIONS` (`{id, to, label, key, menu?, needs?}`), `visibleSections(can)`; `needs: 'scripting'` for Scripting («only to whoever may use a scripting environment or a container registry»); `useKeyHelp.ts` `useGoToSections()` computes `can`; `AppShell.vue` draws the bar from the same table, with the menus `analysis` and `admin` | `needs: 'bayes'`, from `useModelGroups()` |
| Whether the group is usable | `composables/useModelGroups.ts`: `GET /models/groups`, per group `enabled`, `usable`, `ready`; `disabled_reason === 'kubernetes'` | `usable` of `bayes` |
| A space's list page | `pages/scripting.vue`: the objects this person sees (name, folder, version, last run), a filter past five rows, «New» with a folder chosen through `components/ui/Select.vue` | `pages/bayes-models.vue` |
| An object's page with versions | `pages/script.vue`: `editable`, the versions aside (`<aside class="side card">`, restore with a confirm), save | `pages/bayes-model.vue` |
| A canvas | `components/FlowEditor.vue`: `<VueFlow :nodes :min-zoom="0.2" :delete-key-code="null">`, `#node-<type>` slots, `Background`, `Controls` with named buttons (`ControlButton` with `title` and `aria-label`), `MiniMap`; parent nodes with `extent: 'parent'` for the foreach container | The same pieces; the plates as parent nodes |
| Keys declared by a surface | `useKeyHelp.ts` (`useKeyGroups`, `keyNames(platformIsMac())`, `names.ways({pc, mac, any})`), `FlowEditor.vue`'s canvas keys, `GridKeys`; DESIGN.md «Keyboard» (never Alt with a letter, never ⌘+←/→, ⌘+K, ⌘+, ⌘+., nor the browser's own; never three modifiers; Alt+arrows walk the canvas only) | The editor's keys |
| History | `createFlowHistory({serialize, restore, onChange, limit, settleMs})` | Undo and redo |
| Charts and their theme | `echarts` 6.1, `vue-echarts` 8.0; `useChartTheme.ts` (`ChartUi`: text, muted, border, panel, accent… read from the theme's tokens) | The prior's curve and histogram |
| Themes and tokens | `assets/main.css`: `:root` (dark), `[data-theme="light"]`, `"dracula"`, `"monokai"`, `"imperium"`; `--panel`, `--panel-2`, `--border`, `--control-border`, `--text`, `--muted`, `--accent`, `--danger`, `--warning`, `--on-warning`, `--radius`, `--dur-*`, `--ease-out` | Every colour by role |
| Icons | `lucide-vue-next`, already imported in the app: `Database`, `Hash`, `Users`, `List`, `Clock`, `CircleDot`, `SquareFunction`, `Sigma`, `ChartScatter`, `AudioWaveform`, `Variable`, `Boxes`, `CalendarRange`, `ChartSpline`, `Dices`, `FileCode2`, `History`, `TriangleAlert`, `CircleAlert`, `Info`, `Wand2`, `Plus`, `Minus`, `Undo2`, `Redo2`, `Save`, `RefreshCw`, `Keyboard` | No icon the app does not have |
| Monaco, lazily | `composables/useMonaco.ts` (`loadMonaco`), `components/sql/SqlEditor.vue` (`readOnly`) | The code panel |
| The mirrors | `bayesCatalog.ts` (the catalogue), `bayesExpr.ts` (`bayesParseExpr`, `bayesFormatExpr`), `bayesExprCheck.ts` (`bayesCheckExprText`, `BayesExprEnv`), the graph mirror `bayesGraph.ts` with `priorPlan` (lot M4, not built) | Findings while drawing |
| The preview | `bayesPriorPreview.ts` (`preview(plan, target, seed)`, debounce 150 ms, latest only), the worker (lots P0, P1, not built) | The chart beside the numbers |
| The templates | `frontend/assets/bayes-templates.json`, `bayesOpenTemplate` (the templates spec, lot T0) | The empty canvas's proposal; the first screen's graph |
| The store's client | `useBayesModels.ts` (the store spec, lot D3) | Save, versions, restore, check |
| Browser checks | `e2e/dbt_export/marionette.py` (Firefox through Marionette), used by `e2e/editor_mirror/browser_check.py` and `e2e/getting_started/browser_checks.py` (screenshots under `SHOTS`) | `e2e/bayes_editor/browser_check.py` |

## The space

### The list: `pages/bayes-models.vue`

As `pages/scripting.vue`: a card with the models this person sees — name (link) with its description, folder, family
(**derived from the saved graph**, `model-store`'s `family`; the template it was opened from on a line under it), version,
«used by N flows», the compile state of the current version (`model-store`'s `CompileState` in words: compiled, compiling,
to compile, failed with its detail), last save and who — and no «has errors» state: a graph with errors is never saved
(Correction of 2026-11-24 from the mockups); a filter past five
rows, and **«New model»**, which asks for a folder (EDIT checked when the model is created) and opens the editor on the
empty canvas with its templates. An intro paragraph says what the space is for, in one sentence for a reader who does not
know Bayes.

### The editor: `pages/bayes-model.vue`

Addresses: `?id=<model>` (a saved model, its current version), `?id=<model>&version=<v>` (a version, read only, with
«Restore»), `?project=<folder>` (a new model in that folder: the templates' proposal), `?template=<id>` (**lot G0 only**,
before the store: a template opened locally, nothing saved, a banner says «preview: not saved»).

```text
┌ top: ← Bayesian models · name (editable) · folder · version 4 · state · [Undo][Redo] [Save] [⌨] ─────────────┐
│ palette │                          canvas                                   │ side panel (tabs)            │
│ (kinds) │   (mu)    ┌ School (non-centred) ───────────┐                    │ Inspector · Stan code ·      │
│         │   (tau)   │  (alpha_school)                 │                    │ Problems · Versions · (i)    │
│         │   (beta)  └─────────────────────────────────┘                    │                              │
│         │   (sigma) ┌ Pupils ─────────────────────────────────┐            │                              │
│         │           │ [school] [hours] [score]  ⟨eta⟩  ⟨Scores⟩│            │                              │
└─────────┴──────────────────────────────────────────────────────────────────┴──────────────────────────────┘
```

- At **1440 px**: palette 200 px, side panel 420 px (resizable 320–640 px), canvas the rest.
- At **1280 px**: the same columns, palette 168 px.
- **The panel's width by what it shows** (measured on the mockups at 1440 and 1280 px; Correction of 2026-11-24 from the
  mockups): the chart stands **beside** the numbers only at 600 px (two columns of about 280 px: fields, chart); at
  420 px it goes **under** them. The inspector of a node with a chart (a parameter, a component, a state) opens the
  panel at **600 px when the canvas keeps at least 560 px** (a window of 1400 px or more with the 200 px palette),
  otherwise it keeps 420 px and the chart goes under the numbers. The Stan code tab opens it at **640 px** under the same
  condition (otherwise 420 px, the code scrolling sideways). A width the person set by the handle wins over both until
  the page is left.
- Below 1100 px (a narrow window, not a target of the first release): the palette folds to its icons with their names
  as tooltips; nothing is hidden.
- The side panel's tabs are a `tablist` with `v-roving` and `.main` (DESIGN.md «Rows of tabs»): `,` and `.` walk them.

## The canvas

### Nodes

One Vue Flow node per graph node (`#node-bayes`), one component (`BayesNode.vue`) that draws by kind:

| Kind (the graph spec) | Shape | Icon | Shows |
|---|---|---|---|
| `data` | filled rectangle (`--panel-2`), the plate-notation «observed» | `Database` (real), `Hash` (int), `Users` (group index), `List` (category), `Clock` (time) | label, name in code type, the type in words, «standardized» |
| `parameter` | rounded, outlined (`--panel`), the plate-notation «unknown» | `CircleDot` | label, name, the prior as text (`normal(0, 2.5)`, the language's `Format`), «≥ 0» when bounded, «hyperprior» when its value feeds another prior (the graph's role) |
| `transformed` | rounded rectangle, dashed outline | `SquareFunction` | label, name, the expression |
| `linear_predictor` | rectangle | `Sigma` | label, name, `alpha + b_x × x` |
| `likelihood` | rectangle with a double border | `ChartScatter` | label, `y ~ normal(eta, sigma)` |
| `state` | rounded, outlined | `AudioWaveform` | label, name, «random walk», its form |
| `correlated_effects` | rounded, outlined, one line per component | `Variable` | the components' names, «correlated (LKJ 2)» |

A node with an error has a `--danger` outline and the count of its errors in a badge; with warnings only, a `--warning`
mark; the badge's `aria-label` says «2 problems». A node selected has the `--accent` ring. Handles: one source on the
right (what this node feeds), one target on the left (where it takes references from): the edges are not the program's
order, only who references whom.

### Plates (answer 4)

- A **group plate** is a Vue Flow parent node (`#node-plate`, `BayesPlate.vue`), resizable with `NodeResizer`, its label
  and its key («one box per value of `school`») in its header, and its **switch «non-centred / centred»** in the header
  too (a `role="switch"` as `assets/main.css` draws it), non-centred by default (answer 4). A plate whose parent is
  another plate sits inside it (`parentNode`), so districts hold schools.
- The **rows plate** is a box of its own; its header holds the switch «these rows are a time series», which shows the
  choice of the time node and of the series' unit (`series.by`).
- A category's **levels plate** is not a box (the graph spec): a badge «per level of `channel`» on the nodes on it.
- **Moving a node into a box** changes its `plate` (the deepest box under the node's centre when the drag stops; the
  canvas outside every box is a scalar); the change is one step of the history and the mirror's findings follow at once
  (a data node dropped on a group plate gets `graph.node_plate` if its type is not allowed there). Moving a box moves
  what is inside it; dropping a box inside another sets its `parent` (and the mirror says when the depth passes
  `max_plate_depth`).
- The keyboard way to the same: the inspector's «Box» select (every node) and «Inside» select (a plate).

Vue Flow keeps a child's position relative to its parent; the graph keeps **canvas coordinates** (the decision above).
`bayesCanvasModel.ts` converts both ways and is the only code that does; its round trip is a test.

**Measure first** (the map): a throw-away prototype of nested parent nodes in Vue Flow 1.48 — three levels, resizing,
dragging a node across boxes, a box into a box, 200 nodes — opens lot G1; what it finds (the drop target, `extent`,
the order of parents before children that Vue Flow asks, verified in `FlowEditor.vue` line 720) is recorded in «As built»
before the real canvas is written.

### Edges

Drawn from the mirror's `Edges` (who references whom); never stored. The canvas reads **left to right**, as the flow
editor: a template's layout puts every node's sources to its left, so an edge leaves a source's right handle and enters
its target's left handle going rightwards (Correction of 2026-11-24 from the mockups: with side handles, the textbook's
top-down drawing made edges run backwards). A person draws one by dragging from a node's source
handle to another node: a small menu lists the target's slots that can take the source (`bayesCheckExprText` decides),
and the chosen slot gets the source's name (an expression) or a `ref` when the source has no name (the graph spec: «by
dragging an edge»). Deleting an edge clears a slot that held only that reference; for a slot whose expression holds more,
the delete is refused with «edit the expression of `mu` to remove `tau`», and the inspector opens on it.

### The palette

The kinds, grouped: **Data** (number, integer, group, category, time), **Unknowns** (parameter, correlated effects,
latent state), **Values** (transformed, linear predictor), **Likelihood**, **Box** (group plate). Each item has its icon,
its name and one sentence for a reader new to Bayes (`bayes.editor.kind.<kind>.what`). Dragged onto the canvas, or, with
the keyboard, focused and added with Enter: the node appears in the box that is selected (or at the canvas' centre),
selected, its inspector open on the label. A kind that the graph does not allow there is still added: the mirror's
finding says why (the editor never repairs; the graph spec: «findings reported, never repaired»).

A new node's name is proposed from its label (the graph spec: letters folded to ASCII, lower case, other characters to
`_`), made unique with a number; the person may change or remove it.

## The inspector

For the selected node, top to bottom: the **label**; the **name** (with the rule in one line when it fails: «a name has
lower-case letters, digits and `_`, and starts with a letter»); the **box**; then by kind:

- **parameter**: the **prior** (a `Select` of the catalogue's priors, each with one sentence: «normal: values around a
  centre, as likely above as below»), its **arguments** as expression fields with their constraint in words («`sigma`:
  more than 0»), the **bounds** (lower, upper: numbers), and **the chart beside the numbers** (below);
- **transformed**: the value (an expression); **linear predictor**: the intercept and the terms (data node, coefficient),
  «Add a predictor»; **likelihood**: the observed node, the distribution, its arguments; **state**: the first value's
  prior, the scale, the form; **correlated effects**: the components (name, label, mean, scale), the correlation's name
  and LKJ's η;
- **data**: the type, the support, the target box (a group index), «standardize» (real predictors, on by default:
  answer 6, with the sentence «the flow node centres it and divides it by its standard deviation; the report gives the
  coefficients on both scales»);
- a **plate**: its label, its key, «Inside», the switch non-centred / centred with what it means («the sampler moves on the
  standardized scale; the usual remedy when groups have few rows each»), and the warnings `graph.form_centred` of its
  nodes;
- the node's **findings**, each with its message in the person's language (`bayes.graph.<code>`, `bayes.expr.<code>`),
  plate ids replaced by labels (the E3 hand-off: «the UI substitutes labels before formatting messages»);
- the **explanation**: the template's `why.<node>` while the node's slots still equal the template's (the templates
  spec), the distribution's and the kind's otherwise; the person's **note**.

### The chart beside the numbers (`prior-preview`)

`BayesPriorChart.vue` (`vue-echarts`, the chrome colours from `useChartTheme`, the curve and the bars in `--accent`, the
cut and the left-out counts in `--muted`):

- asks `preview(priorPlan(model), target, seed)` after each change the mirror validates (the preview's 150 ms debounce;
  only the latest answer is drawn);
- draws the **curve** (closed form) or the **histogram** (ancestral draws: with hyperpriors, the true marginal), the
  quantiles 5 %, 50 %, 95 % as three marks, and under it the preview's sentence («Before seeing any data, the model says
  that the mean of a school is between −19.6 and 19.8 nine times in ten…»), the cut («0.5 % of the draws on each side
  are beyond the axis») and the left-out draws with their reason;
- «Draw again» (a button: seed + 1); for a node that needs data, the preview's `none: 'needs_data'` sentence (the prior
  predictive check is a Job of the flow node: answer 13); with errors, **the last valid form stays, dimmed**, under a
  line «last valid form: fix μ to update it» naming the argument in error, «Draw again» disabled (owner, 2026-11-24;
  Correction of 2026-11-24 from the mockups); a node that never had a valid form says «fix the messages above to see
  this prior»;
- when an argument is a **hyperprior** (a parameter whose value feeds this prior), a small card under the chart draws
  that parameter's own prior (closed form when its arguments are constants) with its quantiles and its sentence, and
  opens it (spec, 2026-11-24, the mockups);
- an `aria-label` that says the quantiles in words, and a table of the same numbers for a screen reader (a chart alone
  says nothing to it).

### Expressions while typing (answer 3)

`BayesExprField.vue`: a single-line input (`spellcheck="false"`, `autocapitalize="off"`, class `nokey` so the app's keys
rest while typing — DESIGN.md «When they rest»):

- every input runs `bayesCheckExprText(text, env)` with the slot's environment from the mirror (names of the graph, the
  slot's plate, the series): the first error's span (`pos`, `len`) underlined in `--danger`, its message under the field
  (`aria-describedby`, `aria-invalid`), the hints of the language for operators of other languages («`**` is written `^`
  here») and the confusables («this `−` is U+2212: type `-`»);
- the field's type in words beside it when it checks («a number per school»);
- a list of the names and functions that fit at the caret (the environment's names, the catalogue's functions), opened
  with ↓ or by typing, chosen with Enter, closed with Esc: an ARIA `combobox` with a `listbox`;
- the graph's slot is the text as typed: the mirror, the preview and the history read it; **the gateway checks it again
  at save** and its findings **replace** the mirror's (the graph spec). A difference between the two is a bug of the
  mirror: the console logs both, and the corpus gains a case.

### Saving

`Save` (⌘S / Ctrl+S) sends the graph with `base_version` (`model-store`); the answer's findings replace the mirror's on
the nodes; a stale save (409) opens a dialog: «version 6 was saved by someone else meanwhile» with «Reload» (the person's
graph kept in the history: one Undo brings it back) and «Cancel».

**A graph with errors is not saved** (owner, 2026-11-23): `Save` stays enabled, and pressed with errors it shows the
gateway's 422 — the findings on their nodes and in the Problems tab, «the model has 3 problems to fix before it can be
saved» — and writes nothing; warnings never block a save. The top bar shows «unsaved changes» whenever the graph differs
from the version it was opened at (the history's snapshot against the saved one, as `pages/script.vue`'s `dirty`).

**Leaving with unsaved changes** asks first, as the flow editor does (verified, `components/FlowEditor.vue`): every
navigation of the app goes through `onBeforeRouteLeave` / `onBeforeRouteUpdate` with a dialog «stay · leave without
saving · save and leave» (save and leave stays on the page when the save is refused, the findings in sight); closing the
tab or reloading goes through the browser's `beforeunload` warning; a sign-out the account wanted (`useLeaving()`) does
not ask. The work is never lost silently.

## The Stan code, read only

The **Stan code** tab: the program the gateway generates (`POST /bayes-models/check`, 600 ms after the last change the
mirror finds without error, and when the tab opens), in Monaco read only (loaded the first time the tab opens), with a
tokenizer of the program's alphabet (blocks, types, `~`, numbers, identifiers: the codegen spec's «The alphabet»); above
it the program's version, the generator's version (`CodegenVersion`) and the program's sha256 with a copy button;
**under** it the **mapping table** (name in the graph, label with its kind in words, identifier in the code; a
`renamed` entry with its reason on a line of its own: «no name», or «`beta` is the name of a Stan distribution»), each row
selecting its node on the canvas and a node selected on the canvas selecting its row and the program's lines that use
it (Correction of 2026-11-24 from the mockups). **While the model has errors** the tab shows **the stored program of the
last saved version** (`GET /bayes-models/{id}/versions/{v}`), under a notice «the graph has an error, so there is no
program for the changes: this is version 4's, the last saved, as the flows that pin it run it», with the errors listed
and a link to the Problems tab (owner, 2026-11-24); a model never saved says «no program while the model has errors (3)»
and lists them. The code is never editable and never sent back (owner: «graph only»).

## The templates and the free model (answer 1)

A model with no node shows, over the canvas, **the templates' proposal** (`BayesTemplateGallery.vue`): three groups (GLM,
hierarchical, time series), a card per template with its title, its two sentences of «what it is for» and a small drawing
of its layout (boxes and nodes from its `layout`, an SVG of a few rectangles, no library), and **«Start from an empty
model»** as a button of the same weight (one click: answer 1). Choosing a template asks what the templates spec's open
questions decide (the outcome's typical value and spread, the AR order), every field optional, then opens it
(`bayesOpenTemplate`) as an undoable step. The gallery is reachable later from the palette's «Templates» (replacing the
canvas after a confirm when it holds nodes).

## Explanations for a reader who does not know Bayes (answer 1)

- the palette's sentence per kind; the inspector's per distribution, per argument, per switch; the template's per node;
- a **glossary** in the side panel (`Info`): prior, likelihood, posterior, hyperprior, box (plate), non-centred,
  standardized, each in two sentences, with a link to the docs' page (`docs`' module);
- the findings' messages (already written for this reader by `model-graph` and `expression-language`);
- the rule of words (`docs/specs/report-explanations.md`): facts and numbers, never «wide», «vague», «informative»,
  «reasonable», «good», «bad»; a test scans `bayes.editor.*`.

## Keyboard

Declared with `useKeyGroups` (shown in the list of keys, `?`), written with `keyNames(platformIsMac())`; every action
also has its button. On the canvas (no text field focused):

| Action | PC | Mac | Note |
|---|---|---|---|
| Into the nodes, then the next one | Tab · Enter | Tab · ↩ | as `FlowEditor.vue` |
| The nearest node in a direction | Alt+← ↑ → ↓ | ⌥← ↑ → ↓ | Alt+arrows walk the canvas only (DESIGN.md) |
| Move the selected node or box | Shift+arrows | ⇧+arrows | 10 px a step; into a box by the inspector's «Box» |
| Add a node | `n` (the palette's first item focused) | `n` | Enter adds |
| The node's commands (inspector, box, delete) | Shift+F10 · `m` | `m` | the Menu key has a letter on every keyboard |
| Remove | Del · Backspace | ⌫ | as the flow editor (`ways({pc: ['Del'], any: [backspace]})`) |
| Duplicate the node | Ctrl+D | ⌘D | its name made unique |
| Undo / redo | Ctrl+Z / Ctrl+Y · Ctrl+Shift+Z | ⌘Z / ⌘⇧Z | ⌘Y is the browser's history on a Mac |
| Save | Ctrl+S | ⌘S | |
| Zoom in, out, fit | `+` `-` `0` | `+` `-` `0` | no ⌘+ or ⌘− (the browser's zoom) |
| The side panel's tabs | `,` `.` | `,` `.` | the app's rule for the main row of tabs |
| Back to the canvas from a field | Esc | Esc | the focus on the node |
| The list of keys | `?` | `?` | |

No F-key, Home, End or Menu key is the only way to anything; no Alt with a letter; no three modifiers. In an expression
field the app's keys rest; ↓ opens the names' list, Enter chooses, Esc closes the list then leaves the field.

## Languages, themes, icons

- Five languages: `frontend/i18n/locales/{en,it,fr,de,es}.gen.ts` under `bayes.editor.*` (and the store's, the
  templates', the graph's and the language's keys); a test checks every key in the five and the same `{placeholders}`.
- Five themes: colours by role only (`var(--panel-2)`, never a value: DESIGN.md «The Role, Not Hex Rule»); fields with
  `--control-border`; the canvas background pattern from a token (`FlowEditor.vue` hard-codes `#2a2f3a`: this canvas uses
  `--border-soft`); screenshots of one screen in all five themes (lot G5).
- Icons: only the lucide icons listed in «What exists», at the app's sizes (13 px in nodes, 15 px in headers).
- **No new dependency.** If a lot finds one necessary (a layout library for «tidy the canvas», a combobox), it stops and
  asks (Boundaries).

## Memory and speed

A graph is at most `max_nodes` (200) nodes and `max_plates` (8) boxes: the canvas holds it whole, by design (the graph
spec). The preview runs in its worker (4,000 draws, its pools). The mirror runs after each change, debounced 150 ms; its
time on the largest corpus graph is measured by lot G2 on the development machine in Chrome and Firefox and recorded (an
engineering target, not asserted: the findings shown within 100 ms of the debounce).

## Tech stack

- **Vue 3.5 / Nuxt 3.21**, `@vue-flow/core` 1.48.2 with `background`, `controls`, `minimap`, `node-resizer` (installed),
  `echarts` 6.1 + `vue-echarts` 8.0, `monaco-editor` 0.55.1 (lazy), `lucide-vue-next`, `vue-i18n` 9.14 — all already in
  `frontend/package.json`; **no new package**.
- Tests: `node --test` with `--experimental-strip-types` under Node 22 for the pure composables; `vue-tsc` (the pinned
  command); the browser checks by hand.

## Commands

```bash
cd frontend && npm i --no-save @intlify/message-compiler@9.14.5 && npm test        # one heavy job at a time
cd frontend && node --experimental-strip-types --no-warnings=ExperimentalWarning --test tests/bayes-canvas.test.mjs tests/bayes-editor-keys.test.mjs
cd frontend && npx nuxi prepare && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
# the browser checks, by hand, on the development stack (Firefox through Marionette; Chrome as lot G5 says)
UI=… API=… SHOTS=… python3 e2e/bayes_editor/browser_check.py
```

## Project structure

```text
frontend/pages/bayes-models.vue                 the list
frontend/pages/bayes-model.vue                  the editor
frontend/components/bayes/
  BayesCanvas.vue        the Vue Flow canvas, drop on boxes, edges drawn and drawn by hand
  BayesNode.vue          a node, by kind
  BayesPlate.vue         a box: header, switch, NodeResizer
  BayesPalette.vue       the kinds
  BayesInspector.vue     the selected node's fields
  BayesExprField.vue     an expression with the mirror
  BayesPriorChart.vue    the preview's curve or histogram
  BayesCodePanel.vue     Monaco read only and the mapping table
  BayesProblems.vue      every finding, sorted, each selecting its node
  BayesTemplateGallery.vue   the empty canvas's proposal
  BayesGlossary.vue
frontend/composables/
  bayesCanvasModel.ts    graph ↔ Vue Flow (plates as parents, coordinates, the drop target), pure
  bayesEditorKeys.ts     what a key does on the canvas, pure (as appKeys.ts)
  bayesNames.ts          the name proposed from a label, made unique, pure
  useBayesEditor.ts      the editor's state: graph, selection, history, mirror, preview, save
frontend/composables/appKeys.ts, useKeyHelp.ts, components/AppShell.vue   + the Scripting menu with «Bayesian models»
frontend/tests/bayes-canvas.test.mjs, bayes-editor-keys.test.mjs, bayes-names.test.mjs, bayes-editor-words.test.mjs
frontend/i18n/locales/*.gen.ts                  bayes.editor.*
e2e/bayes_editor/browser_check.py
```

## Code style

A surface's keys, declared where they work and written for the keyboard in use (verified,
`frontend/components/FlowEditor.vue`):

```ts
useKeyGroups(() => {
  const k = keyHelp.names
  const nodes: [string, string][] = [
    [`Tab · ${k.enter}`, t('canvasKeys.tabEnter')],
    [`${k.chord(k.alt, '←')} · ${k.chord(k.alt, '→')}`, t('canvasKeys.walk')],
    [k.chord(k.shift, '← ↑ → ↓'), t('canvasKeys.move')],
    [k.ways({ pc: ['Del'], any: [k.backspace] }), t('canvasKeys.remove')],
```

This module's pure canvas model in the same voice (a sketch; lot G0 writes it): the graph is the truth, Vue Flow a
drawing of it, and the drop target a function of the boxes.

```ts
// The box a node belongs to after a drag: the deepest group box (or the rows box) under its centre,
// null outside every box. The graph's `plate` follows; the mirror then says whether the kind may
// live there. Nothing is moved back: the editor never repairs what the person drew.
export function bayesDropPlate (centre: BayesPoint, boxes: readonly BayesBox[]): string | null {
  let best: BayesBox | null = null
  for (const b of boxes) {
    if (!bayesInside(centre, b)) continue
    if (!best || b.depth > best.depth) best = b
  }
  return best?.id ?? null
}
```

Comments are sentences that say why; names in English; every export of a composable starts with `bayes` or `Bayes`
(`bayesCatalog.ts`: «a composable's exports are auto-imported everywhere»).

## Testing strategy

- **Pure** (Node): `bayesCanvasModel` — every template and every graph of the corpus to Vue Flow and back gives the same
  graph, layout included; canvas ↔ relative coordinates through three levels; the drop target (inside, on an edge, in
  two boxes, outside); a box into a box sets `parent`. `bayesEditorKeys` — every action of «Keyboard» on a Mac and a PC
  keyboard, none needing F-keys, Home, End or Menu, none with Alt and a letter or three modifiers. `bayesNames` — labels
  in five languages and with confusables give names that pass the rule, unique. `useBayesEditor` (with fake mirror,
  preview and client) — a change is one history step, a save sends `base_version`, the gateway's findings replace the
  mirror's, a stale save keeps the person's graph one Undo away.
- **Words**: every `bayes.editor.*` key in five languages, the same placeholders, no word of the judgement list.
- **Types**: `vue-tsc` green.
- **Bundle**: `nuxt build` (lot G5, alone) puts nothing of the editor, Vue Flow's second canvas, Monaco, the worker or
  stdlib in the entry chunk (Nuxt splits pages; the check reads the build's manifest).
- **Browser, by hand** (`e2e/bayes_editor/browser_check.py`, then the person's eye on the screenshots), on **Chrome and
  Firefox**, at **1440 × 900 and 1280 × 800**: open each template; type a prior's numbers and see the chart change; type
  an expression with an error, see it underlined and its message; drag a node into a box and out; nest a box; switch a
  box to centred and see the warning; save, reload, restore a version; the Stan code and its mapping; the whole of it
  with the keyboard only, on a Mac's key names and a PC's; the five themes on one screen; Italian and English at least
  read by a person, the other three checked for overflow. **No secret readable** in any screenshot: a throw-away account
  whose password is never typed on screen, no token in an address, no Administration page, the browser's own bars cropped
  out.
- **Regression**: `FlowEditor.vue`'s and the app keys' suites unchanged (`app-keys.test.mjs`); the bar at 1440 px for an
  administrator still fits (DESIGN.md: «at 1440 px an administrator's bar fits to the pixel»).

## Boundaries

- **Always**: the graph is the truth and the canvas draws it; findings reported, never repaired; the gateway's check at
  save authoritative; every colour a token, every text in five languages, every action reachable by the keyboard on a Mac
  and a PC; Chrome and Firefox before a lot is done; screenshots without secrets.
- **Ask first**: any new dependency; any new token in `main.css`; storing anything the graph format does not hold
  (edges, the template's id inside the graph); a change of `FlowEditor.vue`; a place in the top bar other than Open
  question 1's answer.
- **Never**: editable Stan code; a program written in the browser; data in the model's space; a judgement word; a
  shortcut that only an F-key, Home, End or Menu reaches; a hard-coded colour.

## Success criteria

1. Every template opens on the canvas, with its boxes nested as its plates, and validates in the mirror with no error.
2. Typing a prior's argument redraws its chart within the preview's debounce, with the curve or the histogram the
   preview spec says and its sentence; an expression's error is underlined at its span with its message in the person's
   language.
3. A node dragged into another box changes its plate and the findings follow; the same is done from the inspector by the
   keyboard alone.
4. A saved model reloads to the same graph (layout included); the gateway's findings replace the mirror's; a stale save
   is refused and the person's work is one Undo away; a graph with errors is not saved and shows why; leaving with
   unsaved changes asks first.
5. The Stan code shown is the gateway's, read only, with its mapping table.
6. Every action of «Keyboard» works on Chrome and Firefox with a PC's and a Mac's key names; no new package in
   `package.json`; nothing of the editor in the entry chunk.
7. The screenshots at 1440 and 1280 px, five themes, show no secret.

## Lots

| Lot | Objective | Files | Accepted when | Depends on | Agent |
|---|---|---|---|---|---|
| **G0** | **The first screen**: `bayes-model.vue?template=<id>` on local data — the canvas with a template's nodes, edges and nested boxes, read only but for a prior's numbers; the inspector of a parameter with its expression fields and **the chart alive**; screenshots for the owner | `bayes-model.vue` (part), `BayesCanvas.vue`, `BayesNode.vue`, `BayesPlate.vue`, `BayesInspector.vue` (parameter), `BayesExprField.vue`, `BayesPriorChart.vue`, `bayesCanvasModel.ts`, tests | the owner sees `hier_intercepts` and `glm_logistic` on Chrome and Firefox at both widths, the chart following the numbers | templates T0; model-graph M4 (`priorPlan`, `Edges`); prior-preview P0, P1 | sonnet |
| **G1** | Drawing: the plates' prototype first (recorded), then the palette, adding and removing nodes, boxes (resize, nest, the switch, the series), drop into boxes, edges drawn by hand, names proposed, history | `BayesPalette.vue`, `BayesPlate.vue`, `BayesCanvas.vue`, `bayesNames.ts`, `useBayesEditor.ts` | pure tests green; the drawing by mouse checked on both browsers | G0 | **opus** (the map's hardest interaction) |
| **G2** | The inspector of every kind, the findings on the nodes, the problems' list, the explanations, the glossary, the mirror's time measured | `BayesInspector.vue`, `BayesProblems.vue`, `BayesGlossary.vue`, locales | words test green; findings of the corpus shown on their nodes | G1; model-graph M4 | sonnet |
| **G3** | The space and the store: the list page, the Scripting menu with «Bayesian models» (`g y`), the leave warning, new model from a folder and from Explore, save, versions, restore, the gateway's findings, the stale save, the templates' gallery with its options | `bayes-models.vue`, `bayes-model.vue`, `BayesTemplateGallery.vue`, `appKeys.ts`, `useKeyHelp.ts`, `AppShell.vue` | criteria 1, 4 | G2; model-store D2, D3; templates T2 | sonnet |
| **G4** | The Stan code panel: Monaco read only, its tokenizer, the mapping table | `BayesCodePanel.vue` | criterion 5 | G3; stan-codegen S2 (the check route answers with a program) | sonnet |
| **G5** | The keyboard complete, the browser checks, the bundle check, the five themes and languages, the screenshots | `bayesEditorKeys.ts`, `e2e/bayes_editor/browser_check.py`, «As built» | criteria 6, 7 | G4 | sonnet (by hand, then an **opus** review of the whole module in a fresh context) |

The first screen's path is all frontend and Go unit work, none on the cluster: model-graph M2 → M4, prior-preview P0 → P1,
templates T0, then G0. G0's page is reached by its address only (no link in the bar, no save) until G3.

## Corrections made to the approved specs (2026-11-23)

Applied by the owner's decision of 2026-11-23, marked there «Correction of 2026-11-23 from the step-3/4 specs»:

- `bayes-model-graph.md`, «The document»: `layout` holds **canvas coordinates** (absolute), whatever box a node sits in.
- `bayesian-editor-map.md`, «What exists and is reused»: «A tab or a section «Bayesian models»» becomes «Scripting becomes a
  menu with «Scripting» and «Bayesian models»».

## Decisions of 2026-11-23 (the answers to this spec's questions)

| Question | Decision | By |
|---|---|---|
| Where the space sits in the top bar | **Scripting becomes a menu** holding «Scripting» and «Bayesian models», each shown by its own `needs` (`scripting`, `bayes`); **a plain link when only one of them is visible** (a small change of `AppShell.vue`, whose menus today always open); `g s` for Scripting kept, `g y` for Bayesian models (the letter from its English name: `b` and `a` are taken) | Owner, 2026-11-23 |
| A graph with errors (`model-store`'s question) | Not saved; the editor shows the findings and warns before leaving with unsaved changes, as the flow editor does | Owner, 2026-11-23 |

## Corrections made from the mockups (2026-11-24)

Applied by the owner's decisions of 2026-11-24 on the five mockups («Decisions of 2026-11-24 (mockups)»), each marked
«Correction of 2026-11-24 from the mockups» where it lands:

- this spec: the Stan code as a side-panel tab with the mapping table under it; the stored program of the last saved
  version while the graph has errors; the last valid form of a prior, dimmed, while an argument has an error; the
  hyperprior card; the panel's width by what it shows; the canvas left to right; no «has errors» state in the list; no
  «check now» button; «Mockups» below;
- `bayes-model-store.md`: the list's **family derived from the saved graph** (`family`);
- `bayes-templates.md`: the layouts **left to right** (a lot rewrites them in `templates.json`); the AR order **1 to
  `bayesTemplateMaxOrder` (31)**;
- `bayes-stan-codegen.md`: the example's coefficient named `beta` gets a generated identifier, as the identifier rule says;
- the messages `bayes.graph.form_centred.*` (the five locales, lot G2 with the words test): **a fact, no judgement** —
  «this parameter has bounds, and a parameter with bounds cannot be written in the non-centred form: it stays centred
  even though its box is not» (the present text ends with «may sample less well», a word of the judgement list's kind).

## Decisions of 2026-11-24 (mockups)

| Question | Decision | By |
|---|---|---|
| Where the Stan code is shown | A **tab of the side panel**, the mapping table **under** the code; no Graph / Stan code switch in the top bar | Owner, 2026-11-24 |
| The Stan code while the graph has errors | The **last saved version's** program with the notice (as the mockup) | Owner, 2026-11-24 |
| A prior's chart while an argument has an error | The **last valid form, dimmed**, with «fix μ to update it» (as the mockup) | Owner, 2026-11-24 |
| The family in the list | **Derived from the saved graph**: group plates → hierarchical; rows a series → series; otherwise GLM when it is a linear predictor with a GLM likelihood; otherwise free | Owner, 2026-11-24 |
| The canvas' direction | Left to right, as the flow editor; the templates' layouts follow (a later lot) | Technical, 2026-11-24 |
| A «has errors» state in the list | None (a graph with errors is never saved) | Technical, 2026-11-24 |
| The message on the centred form | A fact, without «may sample less well» | Technical, 2026-11-24 |
| The AR order | 1 to 31 (`bayesTemplateMaxOrder`: the longest expression within the 1,000-byte limit) | Technical, 2026-11-24 |
| The shortcuts written in the menu's items | Kept (`g s`, `g y` beside «Scripting» and «Bayesian models») | Technical, 2026-11-24 |
| A «check now» button | None: the check runs by itself (600 ms after a change, on the code tab, at save) | Technical, 2026-11-24 |
| The panel's width | 600 px for a chart beside the numbers when the canvas keeps 560 px (1440 px: yes; 1280 px: no, 420 px with the chart under) | Technical, 2026-11-24 (measured on the mockups) |

## Mockups

Five static screens, approved by the owner on 2026-11-24, drawn in the app's tokens (five themes), IBM Plex Sans and the
lucide icons, checked on Chromium and Firefox at 1440 × 900 and 1280 × 800. They are described here in words: the files
were throw-away. Every example is invented; the same model, «Scores by school» (pupils in schools in districts), runs
through screens 3 to 5.

1. **The list** (`pages/bayes-models.vue`). The top bar with **Scripting as a menu** open: «Scripting» `g s` and «Bayesian
   models» `g y`, the current one tinted, each item with its two keys written at its right in `kbd`. The page: the
   headline with its icon and count, one sentence for a reader new to Bayes, then the card of `pages/scripting.vue`
   (title with count, a filter field, «New model» as the primary). The table: name (an icon and the name as a link, the
   description under it), folder, family (derived from the graph, «opened from «…»» under it), version, «used by N
   flows» (a link with the flow icon; «no flow» in muted), compile state (icon and words: compiled `accent-2`; compiling
   with the spinner; to compile «compiled the first time it runs»; failed in `danger` with its detail), last save (when,
   and who under it), and three mini icon buttons (duplicate, versions, more: rename, move, delete — delete refused while a
   flow uses it). A line under the table says how the family is derived and that a flow runs the version it pins. The
   empty state: one sentence of what a model is, the four families with their counts of templates, «New model» and
   «What a Bayesian model is».
2. **The empty canvas and a template's dialog.** The editor with nothing on the canvas: over it the **templates'
   gallery** — «Where do you start?», a sentence for a reader new to Bayes, «Empty model» as a dashed button of the same
   weight at the right of the heading, then three groups (GLM 4, hierarchical 3, series 3), each with its one-line
   subtitle and a row of **cards**: a small drawing of the template's layout (boxes and nodes as rectangles, an SVG),
   the title, two sentences with a business example, and what opening it asks («asks the typical value and the
   spread», «no question: log-odds scale», «asks the order p too»). The side panel shows the **glossary** (prior,
   likelihood, posterior, hyperprior, box, non-centred, standardized, two sentences each). Choosing «Varying intercepts
   by group» opens the **template dialog** (the app's dialog skeleton, 540 px): the template's two sentences, why the
   numbers are asked («your numbers, written in the priors in the open; not read from the data; empty they are 0 and
   1»), two fields «typical value of the outcome» and «typical spread» with their question as the hint, then **the
   priors that follow**, a table of label and name, the prior in mono with the person's numbers tinted
   (`normal(300, 2.5 * 50)`, `normal(0, 50), ≥ 0`, `exponential(1 / 50)`) and one muted line of what each is, the
   sources (rstanarm's priors page, the Stan prior recommendations), «Cancel» and «Open the model» («undone with
   Ctrl+Z»). The AR dialog adds «order p» with − and + (1 to 31, default 2), the priors for that p (`phi1`, `phi2` ~
   `normal(0, 0.5)`), the likelihood with `lag`, and two facts: stationarity is not imposed; the first p rows of a
   series stay out of the likelihood.
3. **A hierarchical model on the canvas.** The editor's top bar: the reduced mark, «← Bayesian models», the model's name,
   the folder Select, «v4 · saved yesterday by …», «unsaved changes», Undo, Redo, Save (the primary), the keys button —
   no view switch, no check button. The palette (the flow editor's sidebar language): a search, the groups Data,
   Unknowns, Values, Likelihood, Boxes, each item with its icon, name and one short sentence, «Templates» at the bottom.
   The canvas, **left to right**: the hyperparameters in a column at the left; the box «District» holding the
   correlated effects «Level and slope of the district» and the nested box «School» holding «Mean of the school»; the
   box «Pupils» (the rows) holding the data, the transformed «Level for the pupil», the linear predictor «Expected
   score» and the likelihood «Observed score». **Neutral nodes**, the kind said by icon and shape: data a filled
   rectangle, parameters a pill, the transformed node dashed, the linear predictor a sharp rectangle, the likelihood a
   double border, the correlated effects a rounded card with one line per component; each with its label (two lines at
   most), its name in mono and its prior or expression in muted mono; a small «hyperprior» tag on the top border of a
   parameter that feeds another prior. **Boxes**: a header with the box icon, the label, «one per value of `school`»,
   and the switch «Non-centred» at the right (on by default); the rows box with «Time series» (off). One warning mark
   (a 18 px circle with the triangle on the top border) on «Class», whose data feed nothing. Edges in `edge`, the
   selected node's in `accent`; the minimap and the zoom controls at the bottom; the keys button at the top right. The
   panel shows the **inspector of the box «School»**: label, the key Select, «Inside» Select, the switch with what the
   non-centred form does, what the box holds, how many units («the flow says, from the distinct values of `school`»),
   its nodes' problems, the note. Fitted at 86 % at 1440 px and 72 % at 1280 px.
4. **The prior panel.** «Mean of the school» selected (its edges in `accent`, the error outline and a «2» badge on the
   node); the panel at 600 px in two columns. Left: label and name, the box Select («School, inside District»), the
   distribution Select (Normal and its sentence), the argument μ with the expression `mu_distretto` **underlined wavy
   in `danger`**, the field outlined in `danger`, and under it the real message («`mu_distretto` has a value for each
   unit of District: write `mu_distretto[distretto]` to bring it here»); the argument σ `sigma_scuola` with «a single
   number» and a «hyperprior» tag; the bounds (lower 0) and the truncation's sentence; the node's problems (the error,
   pointing at the field, and the centred-form warning). Right: **the chart beside the numbers** — the histogram of one
   school's marginal from 4,000 ancestral draws (60 bars in `accent`, the 5, 50 and 95 % marks, the axis cut at 0), here
   **dimmed** under «last valid form: fix μ to update it», a table of the three quantiles, the preview's sentences
   («nine times in ten between … and …; half the time below …», how it was drawn, the draws beyond the axis and those
   left out by the truncation), «Draw again» (disabled while the error lasts) with its draw number; under it the
   **hyperprior card** of `sigma_scuola`: its half-normal curve in closed form with its quantiles and sentence, and
   «open»; then «What it is», for a reader new to Bayes. At 1280 px the panel stays 420 px, one column, the chart under
   the numbers.
5. **The Stan code tab.** The canvas fitted at 62 % with the coefficient selected; the panel at 640 px on its «Stan code»
   tab: «Stan program, read only, generated by the gateway, version 4, generator 1», the sha256 in mono with a copy
   button; a notice in the warning tint (the graph has an error, so this is the last saved version's program, as the
   flows that pin it run it), the error in one line and «2 warnings — all in Problems»; the code in a 300 px box, line
   numbers sticky, hand-made colours from the tokens (block names `accent-hi` bold, types `violet`, bounds' words muted
   italic, functions and distributions `accent`, numbers warning-tinted, `~` `accent-2`, generated identifiers muted),
   the selected node's identifier boxed in `accent` and its lines tinted; **under it the mapping table** (name in the
   graph, label with its kind in muted, identifier), the selected row tinted with an `accent` edge, the renamed entry
   `beta` → `V_n11` with its reason on its own line («`beta` is the name of a Stan distribution and a variable cannot
   cover it; the results keep `beta`»). The top bar says «Not saved: 1 error» after a refused save.

**Components to create** (none exists): `BayesNode.vue` (the shapes, the hyperprior tag, the error badge and the warning
mark), `BayesPlate.vue` (the header with the switch and `NodeResizer`), `BayesPalette.vue` (items with a sentence),
`BayesExprField.vue` (the wavy underline at the error's span, the message under it), `BayesPriorChart.vue` (histogram and
curve, quantile marks, the dimmed last valid form, the hyperprior card), `BayesTemplateGallery.vue` (cards with the
layout's drawing, «Empty model») and the template dialog with its table of priors, `BayesCodePanel.vue` (the header with
versions and sha256, the stale notice, the tokenizer, the mapping table under the code), `BayesProblems.vue` (findings as
cards with «Show in the graph»), `BayesGlossary.vue`; in the app: the menu items' written keys (`AppShell.vue`) and the
list's family and compile-state cells.

## Open questions

None.

## As built

### Lot G0 (2026-11-24)

- `pages/bayes-model.vue?template=<id>`: the template's dialog (`BayesTemplateDialog.vue`: the outcome's typical value and
  spread, the AR order 1 to `bayesTemplateMaxOrder` with − and +, the priors that follow with the person's numbers tinted,
  the AR likelihood and its two facts, the sources), then the canvas and the inspector on local data; nothing is saved, a
  tag in the top bar says so; without a template (or with an unknown id) the page lists the ten as links. No palette, no
  history, no Stan code, no tabs but the inspector: lots G1–G4.
- The canvas (`BayesCanvas.vue`, `BayesNode.vue`, `BayesPlate.vue`, `bayesCanvasModel.ts`): nodes by icon and shape,
  boxes as Vue Flow parent nodes nested by `parent`, the rows' box beside them holding the likelihood, edges one line per
  pair (a group index alone dotted, the selected node's in `accent`), nothing dragged or connected; a group box's switch
  changes its form and the mirror's findings follow. The view is fitted once when the model opens and stays where it is
  when the panel widens to 600 px (as the mockup draws it).
- The inspector (`BayesInspector.vue`): a parameter's prior arguments (`BayesExprField.vue`: the mirror at every
  keystroke, the error's span underlined by a transparent copy of the text behind the input, the type in words, the
  hyperprior tag) and its bounds; the other kinds and the boxes read only, with their problems.
- The chart (`BayesPriorChart.vue`) is an SVG in the theme's tokens, as the approved mockup draws it, not `vue-echarts`:
  sixty bars or a curve, three quantile marks and the axis' words in HTML, so it follows the five themes with no colour
  read at runtime and adds nothing to the page's chunk. The last valid form stays dimmed while an argument has an error;
  every parameter an argument reads gets its card with its own prior.
- Words: `bayes.editor.*` and `bayes.templates.<id>.title`, `.what` in the five languages (`tests/bayes-editor-words.test.mjs`);
  the messages `bayes.graph.form_centred.*` rewritten as facts. The nodes' labels stay the templates' English until lot T2.
- Keys: Tab and Enter into the nodes, Alt with an arrow to the nearest node in that direction, `+` `-` `0`, Esc from a
  field back to its node, `?` the list; checked on Chromium and Firefox.

### Lot G1 (2026-11-24)

**The plates' prototype, measured first** (a throw-away page with Vue Flow 1.48.2 and `@vue-flow/node-resizer` 1.5.1:
three nested group boxes and a fourth beside them, 196 nodes, on the development machine): 200 nodes are drawn and
measured in about 190 ms on Chromium and 330 ms on Firefox (development build). Without `extent`, a child dragged out of
its box keeps its `parentNode` and a position relative to it, anywhere on the canvas, with nothing clamped: so the canvas
sets no `extent` and the drop decides the new box (the deepest box under the node's centre, under the middle of a box's
header). Vue Flow still wants parents before their children in the array (`bayesCanvasNodes` sorts the boxes by depth).
A box resized from its left or top edge changes its own position, and its children, relative to it, travel with it: the
canvas moves them back at each step of the resize, so they stay where they are seen. A node's z-index is the parent's
plus one: boxes get their depth, nodes 1000, and `elevate-nodes-on-select` is off, so a node is always above every box
(a node dragged over a nested box stays visible) and a selected box never covers the nodes outside it.

- **The palette** (`BayesPalette.vue`): the five groups of the spec (Data, Unknowns, Values, Observed data, Boxes), each
  item with its icon, name and sentence (`bayes.editor.palette.item.<kind>`), a search, ↑ ↓ along the items. An item is
  dragged onto the canvas (the box under the pointer lights as the target) or added with Enter or a click: in the selected
  box, otherwise at the free spot nearest the centre of the view that straddles no box's edge, in the box it lands in;
  the view pans to show it. The new node is selected and the inspector's label takes the focus. 168 px at 1280 px, icons
  only below 1100 px. «Templates» at its foot is lot G3's (the gallery).
- **The edits** (`bayesGraphEdits.ts`, pure, tested): add (its name proposed and unique, a prior `normal(0, 1)` written in
  the open, the other slots empty for the mirror to name), remove (a box gives what it holds to its parent; the rows box
  stays), duplicate, move (a box takes what it holds along), resize, drop (plate or parent), a box's form and key, a group
  index's target, the rows' series (time node and unit), label (the name follows while it is still the proposed one) and
  name. A group box nests only in a group box; the rows box nests in nothing. **Names** (`bayesNames.ts`): folded to
  ASCII, Greek letters spelt (`μ` → `mu`), cut at a word under 24 characters, `v_` before a leading digit, never a
  function of the language, unique with `_2`, `_3`…; ids `n1`, `p1`….
- **Edges by hand**: from a node's right handle to another node (on its handle, or anywhere on it): a menu lists the
  target's slots that can take the source — each is tried in a copy of the graph, offered when the mirror puts no error
  on that slot and makes no cycle (a warning, as «may not be positive», does not stop it) — with what each holds now. A
  data node also offers the observed slot and «a new term of the sum». The slot gets the source's name, a component's
  name, or a `ref` to a nameless node. An edge is selected by a click or reached by Tab; Delete removes it: a slot that held
  only the reference is cleared, a term whose data it was goes, coefficient and all; a slot whose expression holds more
  refuses with «`eta` uses `a_g` inside a longer expression (Intercept): edit it there» (a toast) and the inspector opens
  on that node.
- **The keyboard**: Shift with an arrow moves the selected node or box 10 px (a bare arrow moves nothing: the canvas takes
  the arrows before Vue Flow's own nudge, as `FlowEditor.vue` does); `n` the palette; `m` (Shift+F10, the Menu key, a
  right click) a node's commands — open in the inspector, link to another node… (the node, then the slot), remove a
  link…, duplicate, remove; Delete or Backspace removes the selected node, box or edge; Ctrl/⌘+D duplicates; Ctrl/⌘+Z
  undoes, Ctrl+Y or Ctrl/⌘+Shift+Z redoes (never ⌘Y). The inspector's «Box» select (every node but the likelihood, whose
  box is its observed data's) and «Inside» select (a group box) do what a drop does. Checked on Chromium and Firefox.
- **History** (`useBayesEditor.ts`): snapshots of the document through `createFlowHistory`; a gesture (an add, a drop, a
  resize, an edge, a choice in a select) is one step at once, typing (a label, a name, a prior's number) one step when it stops for
  400 ms; Undo and Redo in the top bar.
- **The panel**: resizable by its handle from 320 to 640 px (the arrows move it 16 px, with Shift 80 px); a width set by the
  handle wins until the page is left. The canvas follows the room it is given: while the view is as it was fitted, a
  change of its width fits it again; once the person has panned or zoomed, it keeps its centre and brings the selected
  node into sight (in G0 a part of the model stayed under a widened panel).
- **The boxes**: resized by their edges and corners when selected; the switches «Non-centred» and «Time series» both work;
  a box's label keeps its room in the header and «one per value of…» gives way first; a box lists the boxes nested in it.
- `/bayes-model?template=empty` opens a model with only its rows' box (the store's «new model» is lot G3).
- Not in this lot: the other kinds' own fields in the inspector (G2), copy and paste (not in the first release), the
  warning before leaving with unsaved changes and the save (G3).

### Lot G2 (2026-11-24)

- **The inspector of every kind** (`BayesInspector.vue`, `BayesKindFields.vue`, `BayesSlotField.vue`): under the label,
  name and box of lot G1, a parameter's distribution (a `Select` of the catalogue's priors, each with its sentence;
  the LKJ only inside correlated effects), its arguments and bounds; a computed value's formula; a linear predictor's
  intercept (empty is none) and its terms (the data, the coefficient, «Add a predictor», the term removed by its ×,
  and «reported on both scales» for a standardized number); a likelihood's observed data, its distribution and its
  arguments; a latent state's process in words, its first value's distribution and arguments, the scale of its
  steps and its switch non-centred / centred; correlated effects' components (label, name, mean, spread between
  groups; «Add an effect» up to `max_effect_components`, removed down to two) and their correlation (label, name,
  η); a data node's type, its whole numbers, its switch «standardized» with its sentence, and the likelihood that
  observes it. Every change is a function of the document handed to the page (`bayesGraphEdits.ts`: `bayesSetDist`,
  `bayesSetDataType`, `bayesSetSupport`, `bayesSetStandardize`, `bayesSetStateForm`, `bayesAddTerm`,
  `bayesRemoveTerm`, `bayesAddComponent`, `bayesRemoveComponent`, `bayesSetEffectsText`, `bayesSetNote`): a choice is
  a step of the history at once, typing one when it stops. A new distribution keeps the arguments of the same name and
  writes the plainest value in the open for a prior's new ones (1 where it must be positive, else 0; uniform 0 and 1);
  a likelihood's new ones are left empty for the person to connect. A new type drops the old type's fields.
- **Every slot** is read from the mirror the same way (`bayesInspector.ts`, `bayesSlotView`): its first error
  underlined at its span (the whole text when the finding has none), a warning said under it in the warning's tint,
  the type in words when it checks (a single number, one per row, one per unit of a box, one per level of a category),
  the «hyperprior» tag when it reads one hyperprior alone. The node's problems list the ones on a field as «In σ of the
  prior: the message is under the field».
- **The charts**: a parameter and a state (its first value) keep the two columns of lot G0 at 600 px; correlated
  effects open the panel at 600 px too, with their fields at the whole width and a row of cards under them, one per
  effect and one for the correlation; a computed value has the chart of its values before the data (or the preview's
  «needs data» sentence). The two columns are a container query on the panel's body, so a width set by the handle
  decides as the window does. The marks' words of a skewed prior are spread apart so they never overlap.
- **The names at the caret** (`BayesExprField.vue`): an ARIA `combobox` with a `listbox` under the field: the graph's
  names (components included; no correlation, no time node, no likelihood, never the node itself) and the language's
  functions, each with its kind and its type in words or the function's sentence; data and `lag` are left out of a slot
  read before any data. Typing a name opens it, ↓ opens it on an empty word, ↑ ↓ walk, Enter or Tab writes the choice
  (a function with its parenthesis), Esc closes it and a second Esc returns to the canvas.
- **The Problems tab** (`BayesProblems.vue`): the counts of errors and warnings, one sentence (errors keep the model
  from being saved, warnings block nothing), then every finding as a card, errors first, the model's own first, then in
  the document's order: what it is on (label and kind), the field in words, the message, and «Show in the graph», which
  selects the node or box and brings it into sight while the list stays open. The tab's badge counts them, in danger
  while there is an error.
- **The glossary** (`BayesGlossary.vue`, the panel's ⓘ tab): prior, likelihood, posterior, hyperprior, box,
  non-centred, standardized, then data, parameter, linear predictor, latent state, correlated effects and `lag`, two
  sentences each. **What it is**: each node and box has its kind's two sentences (a data node's by its type), and the
  distribution's sentence; the template's `why.<node>` takes their place while the node is as the template wrote it
  (the page compares it with the template as opened), once lot T2 writes those keys.
- The side panel's tabs are a `tablist` with `v-roving.main`: `,` and `.` walk Inspector, Problems and the glossary; a
  node selected on the canvas opens the inspector.
- **Messages**: the placeholders that hold a node's id (`data`, `through`, `observed`, `value`, `expected`) are written
  with the node's label, as the plates' were (`bayesFindingText`); a levels plate is «the levels of Channel».
- **The mirror's time**, decode, validation and prior plan of the corpus's largest graph (29 nodes, 12.7 kB), 50 runs
  after 10, on the development machine: Node 22 median 4.5 ms (p90 6.2 ms), Chromium median 3.2 ms (p90 4 ms),
  Firefox median 8 ms (p90 14 ms; the timer's resolution is 1 ms). Within the 100 ms target.
- Tests: `tests/bayes-inspector.test.mjs` (the edits of each kind, the slot's view, the plate's words, the names at the
  caret, the Problems' order, node labels in messages, every finding of the corpus on a node or box of its graph or on
  the model); the words test covers the new keys in five languages.
- Not in this lot: a docs page for the glossary's words to link (the `docs` module has none yet); the templates'
  `why.<node>` texts (lot T2).

### Lot G5, part one (keys, browser check, bundle, themes)

- **`composables/bayesEditorKeys.ts`** (pure): `bayesKeyAction(event, mac)` turns a key event into the action it asks
  for (walk, move, add, menu, remove, duplicate, undo, redo, zoom in, zoom out, fit) or null; `bayesKeyScope` says
  whether the page (undo, redo, duplicate) or the canvas does it. `BayesCanvas.vue` and `pages/bayes-model.vue` ask it
  and keep to themselves the judging of the focus, the dialogs and the selection; their behaviour is unchanged. The
  module also holds `BAYES_KEY_TABLE`, the rows of «Keyboard» with their ways on a PC and on a Mac, and the two checks
  `bayesKeyMissingOnLaptops` (F keys, Home, End, the Menu key) and `bayesKeyForbidden` (Alt with a letter, three
  modifiers). `tests/bayes-editor-keys.test.mjs` runs every way of every row on both keyboards: each has a way a laptop
  has, none is forbidden, each asks for its row's action; ⌘Y is not redo on a Mac.
- **Browser check** (`e2e/bayes_editor/browser_check.cjs`, started by `browser_check.py`): on Chromium and Firefox of
  Playwright, a PC context and a Mac one (platform and `userAgentData` say Mac, the shortcut key is Command), against
  `nuxt dev` with a made-up `tab_token` and no backend: Shift+arrow moves, a bare arrow does not, Alt+arrows walk,
  Backspace and Delete remove, undo and redo (Ctrl+Y on a PC only), duplicate, `n`, `m` and Shift+F10, `+` `-` `0`
  (and not ⌘+), `,` `.`, Esc from a field to the node, `?` with the keyboard's own names. 36 of 36 on each browser.
- **Bundle**: after `nuxt build` the stdlib's generator is in the prior worker's chunk only. The first load holds the
  five languages' texts (distribution and template names) and no editor, canvas, Monaco or stdlib code; the editor's
  chunks come with the page.
- **Themes** (dark, light, Dracula, Monokai, Imperium, 1440 px, Chromium; dark and Imperium on Firefox): a scan of the
  text on the screen found nothing under 4.5:1; two things were under the bar and are fixed with the app's tokens,
  no new one in `main.css`: the inspector's placeholders (3.35 to 4.46 in four themes: «nessuno» says «no limit») now
  use `--muted`, and the canvas' edges (1.75 to 2.18 in dark, light and Imperium, against the 3:1 of a graphic that
  carries meaning) use a mix of `--muted` and `--bg` (3.35 to 4.85 in the five).
- Left for G5 after G3 and G4: Ctrl+S and the leave warning (G3), the code tab's keys (G4), the keyboard check of
  those, the screenshots at 1280 px and of the Stan tab, the other three languages checked for overflow, and the
  fresh-context review of the whole module.
