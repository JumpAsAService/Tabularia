# Spec: the node «Bayesian model» in a flow (`flow-node`)

Status: **written and approved by the owner on 2026-11-24.** It specifies the module `flow-node` of
`docs/specs/bayesian-editor-map.md` (**the map**), step 6 of its order of construction, beside `bayes-report`, after the
approved `bayes-model-store.md` (**the store spec**: the saved models, their versions, «used by N flows», the advisory
lock), `bayes-stan-compile.md` (**the compile spec**: `Binaries`, the run's fallback), `bayes-contract.md` (**the
contract**: `task.json`) and the drafts `bayes-runner.md` (**the runner spec**: the sampling Job) and `bayes-report.md`
(**the report spec**). Nothing of it is built. It is the module the owner tied the release to: **version 1.5.0 «Augustus»
ships only when a model is estimated from a flow** (the owner, 2026-11-24). Decisions marked **(owner)** are the owner's (the
map's «Interview answers», **the answers**, and the owner's rules); the rest are this spec's, to confirm; those marked «spec
decision, 2026-11-24, owner may revisit» are small choices the owner may change without reopening the module.

Words: **verified** = read in this repository's code on 2026-11-24; **assumed** = not run here, closed by the lot named
beside it.

## Objective

A node **Bayesian model** in the flow editor that:

- **chooses a saved model** by its folder path and **pins a version** of it, with the notice «a newer version exists» when
  a later version changes the model (answer 7);
- **binds each data variable of the pinned version to a column** of its input (answer 6: the binding happens only here):
  numbers and integers, a group's index from a text or integer column (units in text order), a category from a text
  column (treatment contrasts, the first level the reference), the series' time column; the standardization of a
  predictor, chosen in the model, shown as a fact;
- holds **the sampler's options** — chains, warm-up, draws and seed in view; `adapt_delta`, `max_treedepth` and the
  initial values under «Advanced»; NUTS only (answer 9) — and **the limits** with their defaults visible (answer 10);
- chooses **its output**: the summary by default, the draws (long) or the replicated data (thinned) on request, or the
  results table (answer 14);
- **runs as a trial on the development sample** («Run») and offers the **prior predictive check** as a trial of its own
  (answer 13); compiles first when the background compile has not finished, and says so;
- runs in a flow as a **step** (a Kubernetes Job, the resolver's boundary, the same priority as a model node), checked at
  every run with **the run's authority** (the person who ran the flow, or the schedule's author);
- shows **«not up to date»** when its settings or its chain changed since its last output, and its **Report tab**;
- keeps a saved model from being deleted under a flow being saved (`pg_advisory_xact_lock(bayesmodels.Lock, id)`, the
  store's note D1).

## Decisions

| Question | Decision | By |
|---|---|---|
| Which version a flow runs | **A pinned version**; the notice «a newer version exists» when the current version's semantic hash differs from the pinned one's (a layout-only version says nothing); moving to it is the person's act | Owner (answer 7); the notice's rule: the store spec |
| Where data are bound | **Only in the node**; a text column bound to a group becomes indices 1…J, to a category treatment contrasts with the first level (text order) as reference; NULLs left out with their count | Owner (answer 6) |
| Standardization | **Chosen in the model** (the data node's `standardize`, default on for a real predictor: the graph editor spec); the node shows it, never changes it | Owner (answer 6); `model-graph` |
| Sampler options | Chains (1–8, default 4), warm-up (0–10,000, 1,000), draws (1–10,000, 1,000), seed in view; `adapt_delta` (0 < x < 1, 0.8), `max_treedepth` (1–15, 10), initial values under «Advanced»; NUTS only | Owner (answer 9); the ranges: the contract's correction 7 (`Task.Check`, verified) |
| The seed's default | **Drawn once when the node is created** (1 … 2³¹ − 1), written into the node and shown; «Draw another» changes it; the same seed gives the same numbers | Spec decision, 2026-11-24, owner may revisit (two nodes never share a stream by accident; answer 10's «the same seed must give the same numbers») |
| Limits | The node's timeout (up to the group's longest run, 3,600 s by default) and retries; the group's limits shown read-only with their defaults (memory, CPU, `/tmp`, `max_rows`, `max_quantities`, `max_units`), «an administrator sets them» | Owner (answer 10) |
| Output | `summary` (default), `draws`, `replicated` (with its draws a chain, 1 … draws, default 50), `results` | Owner (answer 14); the contract |
| The node's type and its runs' kind | Node type **`bayes_model`** (the store spec's interface, `bayesmodels.NodeType`, verified); its runs' kind **`model`** (the run page, the reconciliation of orphans, `ModelRunDetail` and the report proxy read `kind = 'model'`, verified), the trial's kind mapped from the node's type | The store spec (the type); the kind: spec decision, 2026-11-24, owner may revisit |
| The prior predictive check | **A trial** started from the node's panel («Prior predictive check»): the same route as Run with `{"check": "prior_predictive"}`; its run is never the node's output for the nodes after it; the Report tab shows it beside the latest sampling run | Owner (answer 13: «run from the flow node's panel on the development sample's bound rows»); the route's form: spec decision |
| The compile, when not ready | **`Binaries.Need` as soon as the step is planned** (it queues the compile with the run's authority), then the input is prepared, then the step waits for the binary while its timeout lasts, saying «compiling the model first (usually 10–30 s)»; a `timeout` or a compiler's `failed` fails the step at once with the state's sentence | Owner (answer 8); the compile spec («The run's fallback», «As built — lot C3») |
| The time the compile took | **Counted in the step's timeout**: the Job's deadline and `time_budget_seconds` are the timeout minus the wait | The compile spec («the step's waiting counts in its own timeout») |
| MLflow | **None in this module**: the node has no MLflow section and `task.mlflow` is false until `bayes-mlflow` (step 7) | The map's order |
| Who may see the report | Whoever may view the flow, as for every model node (the report is the flow's: `proxy_model_report.go`'s rights, verified) | The models spec |

## What exists and is reused (verified)

| Need | Already there | What this module does with it |
|---|---|---|
| The node's keys and «used by» | `bayesmodels.NodeType = "bayes_model"`, `KeyModel = "bayesModelId"`, `KeyVersion = "bayesModelVersion"`; `Pinned(definition)`, `Using`, `UseCounts`, `References` (any node whose data name a model by an integer id); `Newer(ctx, q, modelID, pinned)` → `Newness{Current, Newer, Changed, PinnedMissing}` | The node writes these two keys; the notice reads `Newer`'s rule |
| The lock of a model | `bayesmodels.Lock = 74610046`: `Delete` runs under `pg_advisory_xact_lock(Lock::integer, id::integer)` and refuses while `References` finds a flow («a flow that pins a model takes it too, so that a model is never deleted under a flow being saved») | Taken by every write of a flow's definition that pins a model |
| Visibility and the right to run | `bayesmodels.CanSee`, `CanRun`, `GetVisible` (VIEW on the folder and the group's use; RUN to run; the group's use read once per request by `api.ModelGroupUsable`) | At the flow's save (see) and at every run (run, with the run's authority) |
| The stored version | `bayesmodels.GetVersion`: graph, program, `program_sha256`, mapping, data and quantity manifests, `codegen_version`, `semantic_hash`; never regenerated (owner); versions never pruned (the store's note D1) | The task's model, mapping and manifests |
| The run's binary | `bayescompile.Compiler` implements `Binaries`: `Ready`, `Need(ctx, CompileRequest)` (the binary when ready; `ErrRefused` with the state when the run must fail; otherwise `Enqueue` without `Again` and the state to wait on), `Place(ctx, b, key)` (the key only of the form `env-<…>/<step>/model`; `ErrAltered`, `ErrGone`); `Deps.BayesCompiler` (nil where models cannot run) | Need, wait, Place before the Job |
| A model step | `internal/api/jobs_models.go`: `planModel` («Before anything»: models off, the group usable, the timeout within the group's, retries), `runModel` (`startChild` with the run's `info`, `modelPrepareInput` — the chain before the node and a `select` of the columns read, in the scripts bucket —, the rows' limit, `modelAttempts`, `importModel` with the card); `checkModelGroup`; `retrySettings` (`timeoutSeconds` 900, `retries`, `retryDelaySeconds`), `budgetSeconds`; `modelFingerprint` = `chainFingerprint(definition, node, own)` | `planBayes` and `runBayes` in the same shape; the Job is the runner spec's `bayesJobManifest` |
| The step kinds | `flowresolver.StepNodeTypes` (`script`, `container`, `model`, `model_predict`), `actionTypes`, `defaultPriority` (model 1); `jobs_queue.go` (the dispatch by node type; the orphans by run kind); `scripting_trial.go` (the trial's kinds, its insert with `kind` = the node's type, `runTrial`'s check `node.type != run.Kind` and dispatch); `scripting_runtime.go` (the latest outputs by run kind, the fingerprints by `flowaccess.ModelNodes`); `flowaccess/reach.go` (`ModelNodes`, `ReachOf`: a `Ref{"model", …}` a model node); `dbtexport/export.go` (step nodes left out); `proxy_model_report.go` (`modelNode` takes `type == "model"`); frontend `FlowEditor.vue` (`STEP_TYPES`, `CONTROL_TYPES`, `DATA_PARENTS`, the colours, `modelReportNodeId` takes `type === 'model'`), `flowChain.ts` (`READABLE`), `useFlowModel.ts`, `ui/NodePanel.vue` (`model` and `model_predict` → `ModelNodePanel`), `ui/OpSidebar.vue` (the Models palette) | `bayes_model` added to each, listed in «The step's kind, everywhere» |
| A flow's save | `flows_body.go` `saveFlow` (one `UPDATE`, no transaction), `flows.go` `insertFlow` (one `INSERT`), the promotion of a version (`UPDATE` in `pgx.BeginFunc`); `checkSave` → `ScriptCheckDefinition` («choose a saved script you may see» for a new or changed reference), `RegistryCheckDefinition`; `guardSomeoneElsesSchedule` over `ReachOf` | The three writes take the lock and check the pins in their transaction |
| A saved-object picker | `scripting/ScriptNodePanel.vue`: `Select` searchable over the saved scripts with `useFolderPaths`, the current version shown | The model's picker |
| The model's variables in the browser | `frontend/composables/bayesGraph.ts`: `bayesGraphModel(graph).bindings` (the mirror, lot M4); `useBayesModels.ts`: `get`, `version(id, v)`, `versions` (with `semantic_hash`), `compile` state | The panel's list of variables, the notice, the compile state |
| The trial and its report | `POST /scripting/flows/{id}/nodes/{node}/run` (RUN on the folder; a trial still running is offered again), `ModelRun.vue` (Run, the latest output, «not up to date»), `StatReportView.vue` with `/tasks/model-report` | Run; «Prior predictive check»; the Report tab |

## The node's data

```json
{ "id": "n7", "type": "bayes_model",
  "data": { "label": "School scores",
            "bayesModelId": 17, "bayesModelVersion": 4,
            "bindings": { "n1": "score", "n2": "hours", "n3": "school", "t1": "week" },
            "sampler": { "chains": 4, "warmup": 1000, "draws": 1000, "seed": 1840277361,
                         "adapt_delta": 0.8, "max_treedepth": 10, "init": null },
            "output": "summary", "replicatedDraws": 50,
            "timeoutSeconds": 1800, "retries": 0, "retryDelaySeconds": 60 } }
```

- `bindings` maps a **data node's id** (stable across versions: `model-graph`) to a column's name (data, never code). The
  ids are those of `real`, `int`, `group_index`, `category` and `time` data nodes of the pinned version's graph.
- `sampler` holds the person's choices; `refresh` is the gateway's (100). `init`: null, a radius ≥ 0, or values by the
  quantity's Stan identifier (what the contract's `Init` takes); the panel offers the radius or a value for each scalar parameter («Decisions of
  2026-11-24»).
- The engine's params of the run are `{"mode": "sample" | "prior_predictive", "output"}` (the contract: the catalogue's
  `takes`), made by the gateway, never stored in the node.

## The panel

A component of its own (`components/bayes/BayesNodePanel.vue`), opened by `ui/NodePanel.vue` for `type === 'bayes_model'`,
in five languages, every control reachable by the keyboard on a Mac and on a laptop without F/Home/End keys (the owner's
rule):

1. **The model**: a searchable `Select` of the saved models the person sees (`useBayesModels.list`), each «folder path /
   name» (`useFolderPaths`), its family; choosing one pins its **current version**. Under it: «v4 · saved by … on …», the
   model's compile state for the current image (`compile.state`, the store's words: «compiled», «compiling», «compiled the
   first time it runs», «too long to compile», «failed»), and a link that opens the model in its editor.
2. **The notice**: when the model's current version is newer **and** its semantic hash differs from the pinned one's,
   «A newer version exists (v6): Use it». «Use it» moves the pin; the bindings of the data nodes still there are kept when
   their type still fits the column, the others are cleared, and the variables the newer version adds are listed unbound
   with «bind it to a column» (the map's risk of `flow-node`). A pinned version that no longer exists (impossible while
   versions are never pruned) or a model the person no longer sees: «this model is not available to you», Run disabled.
3. **The variables**: one row a data node of the pinned version (`bayesGraphModel(graph).bindings`, the mirror; the gateway
   checks again at the run): its label (and name), its type in words («a number per row», «a count», «0 or 1», «the group of
   each row», «a category: the first level is the reference», «the time of the series»), its plate, «standardized in the
   model» when it is, and a column picker filtered by the type (numbers for `real` and `int`; text and integers for a group
   or a category; dates, timestamps and numbers for the time). A column of the input that no longer exists stays shown,
   struck, «not in the input any more».
4. **The sampler**: chains, warm-up, draws, seed with «Draw another»; «Advanced»: `adapt_delta`, `max_treedepth`, the initial
   values; each with its default written beside it and «Reset to default».
5. **Output**: summary (default) / draws (long: one row a quantity of a draw) / replicated data (thinned: draws a chain) /
   results table; one sentence each says what the nodes after it receive.
6. **Limits**: the timeout (default 1,800 s, written into the node when it is created: a model node's 900 s, the gateway's
   default when the key is absent, verified in `retrySettings`, is short for sampling; at most the group's longest run),
   retries; the group's limits read-only with their defaults.
7. **Run** (`ModelRun.vue`, as a model node's: Run on the development sample, the latest output, «not up to date»), and
   **Prior predictive check** (offered when the pinned version can draw one: «Which version can run which check»).

The canvas node (`components/nodes/BayesModelNode.vue`) shows the model's name, «v4», the notice's dot when a newer version
changes the model, and «not up to date» as a model node does.

## Which version can run which check

From the **stored** manifests, never from today's generator (a version runs its saved program: the owner):

| The pinned version's data manifest holds | Then |
|---|---|
| no entry of kind `checks_on` (stored by generator 1) | sampling only; the report says «saved before the predictive checks: saving it again adds them» |
| `checks_on` and the `prior_count` entries | sampling with the posterior checks; the prior predictive check offered |
| `checks_on` without `prior_count` (a truncated gamma or inverse-gamma prior: the checks spec) | the posterior checks; the prior predictive check not offered, «this model has a truncated prior Stan cannot draw ancestrally» |

(Verified on the golden `checks_counts.manifest.json`, which has `prior_count`, and `checks_gamma.manifest.json`, which has
not.)

## From the node to the Job (the gateway)

**`planBayes`** («Before anything», no row, no input, no Job), refusing each with its sentence, as `planModel` does:

1. The models' machinery on (`modelsOff`), the group `bayes` usable by the run's authority (`checkModelGroup`).
2. A model and a version named; the model visible **and runnable** with the run's authority (`CanRun`; the same sentence
   whether it is absent or not seen: «the Bayesian model of «School scores» is not available to <email>»); the version read
   (`GetVersion`).
3. The bindings: every data manifest entry of kind `real`, `int`, `group_index`, `category` bound, and the series' time
   when the stored graph's rows are a series («bind «Hours of study» to a column»); the binding taken by the entry's `node`.
4. The sampler within `Task.Check`'s ranges; the output one of four; the replicated draws within 1 … draws; the timeout
   within the group's; the retries within `scriptnodes.MaxRetries`.
5. The compile asked: `Need(CompileRequest{ModelID, Version, ProgramSHA256, By: authority})`. `ErrRefused` fails the step
   now with the state's sentence (`timeout`: «the program did not compile within the limit of 300 s…»; `failed`: «a fault of
   Tabularia, not of the model»).

**`runBayes`** then, in the shape of `runModel`:

1. The child run (`startChild(…, "model", …)`), its `info` the model step's (`model` = `bayes_model`, `params` = the mode
   and the output, the step's id, the fingerprint, the budget) plus `bayes_model` `{id, version, name}` and `compile`
   `{state, detail}`.
2. **The input**: `modelPrepareInput` with the bound columns (and the time column) as the `select`, and for a series the
   engine's `sort` by the column of the group index whose target is the series' `by`, then the time (the contract, «The data
   file»; the runner refuses a series out of order, `data_order`, verified in `bayes_data._plan`). `modelPrepareInput`
   gains the operations to append after the `select` (none for the other models: their requests keep their bytes). The rows
   against the group's `max_rows` (as `modelMaxRowsOf`).
3. **The binary**: while `Need` answers a state to wait on, every 2 seconds and within the step's timeout, the child's
   `info.compile` updated when the state changes (the run page says it); then `Place(binary, <prefix>/model)`; `ErrAltered`
   or `ErrGone` asks `Need` again (the compile spec: «the run compiles again»). The wait is deducted from the timeout the
   Job receives.
4. **The task** (`modelgroups.Task` with `Bayes`): `model` (id, version, semantic hash, program sha256, the placed binary's
   sha256, codegen version, CmdStan, the program's bytes and the image's digest), `sampler` (+ `refresh` 100), `plates` and
   `mapping` (from the stored version), `data` (the stored manifest with `column` written on the four kinds that read one:
   the contract's correction 3), `quantities` (stored), `checks` (`bayeschecks.TaskChecks(true)` when the manifest holds
   `checks_on`, null otherwise), `replicated` (`{draws_per_chain}` when the output is `replicated`), `series` (`by` from the
   stored graph's rows, `time` the bound column), `finite_variance` and `scale_pairs` (the runner spec, from the stored graph
   through `bayesgraph.Decode` and `BuildModel`); the limits (`setRunLimits` with the bayes `/tmp`, the group's
   `max_quantities`, `max_units`, the catalogue's caps); `Task.Check(mode, output)` before anything leaves the gateway.
5. **The Job**: `bayesJobManifest` (the runner spec), `modelAttempts`, `importModel` (the results checked by the engine
   against `bayes_model`'s variant, the output, the card's key); nothing logged to MLflow.
6. **The step's files** deleted by exact key, the binary's included.

**The prior predictive check** is the same path with `mode` `prior_predictive`, the output `results`, the sampler
`{chains: 4, warmup: 0, draws: 250, seed: the node's}` (the checks spec, «The runs»), `checks` on, offered only as a trial.

## The step's kind, everywhere

`bayes_model` joins the step nodes in each place that lists them (verified, «What exists»): `flowresolver.StepNodeTypes`,
`actionTypes` and `defaultPriority` (1, as `model`); the orchestration's dispatch (`jobs_queue.go`: `bayes_model` →
`runBayes`); the trial (`scripting_trial.go`: the node kinds it accepts, the run's kind `model` for a `bayes_model` node,
`runTrial`'s check comparing the run's kind with the node type's kind, its dispatch by the node's type);
`flowaccess.ModelNodes` (each caller branching on the type: `ReachOf`, the fingerprints of `scripting_runtime.go`, the
trial's upstream outputs); `dbtexport` (left out of a dbt export, as every step); `proxy_model_report.go`'s `modelNode`;
and in the interface `STEP_TYPES`, `CONTROL_TYPES`, `DATA_PARENTS`, the node's colour and icon, `flowChain.ts`'s
`READABLE`, `useFlowModel.ts`, `NodePanel.vue`, the palette («Bayesian model» in the Models section of `OpSidebar.vue`,
shown when the group is usable, as the models' items), and `modelReportNodeId`. The Python gateway's resolver
(`gateway/app/services/flow_resolver.py`, frozen) is not changed: the Go gateway resolves every flow (assumed from the
repository's rule «functions only in Go»; F0 checks no Python path runs a flow with a step node).

## Saving a flow that pins a model

In the transaction that writes `flows.definition` — `saveFlow`, `insertFlow` and the promotion of a flow's version, each
made transactional where it is not —, before the write:

1. the models the new definition pins (`bayesmodels.Pinned`), **in increasing id order**, each locked with
   `SELECT pg_advisory_xact_lock($1::integer, $2::integer)` (`bayesmodels.Lock`, the id): a `Delete` waits for the commit and
   then finds the flow (`References`), and two saves never deadlock;
2. each pinned (model, version) exists (`bayes_model_versions`), else 422 «Bayesian model «School scores»: the model or its
   version no longer exists: choose another»;
3. a pin that is **new or changed** (another model or another version than the previous definition's, as
   `ScriptCheckDefinition` compares by node id) must be visible to the saver (`CanSee` with the group's use), else 422
   «Bayesian model «School scores»: choose a model you may see» — the same sentence for absent and not seen.

**The schedule's authority**: `ReachOf` gains `Ref{"bayes_model", "<id>@<version>"}` for each pin, so someone else's edit that
adds a Bayesian model node or moves a pin switches the author's schedule off (`guardSomeoneElsesSchedule`, verified), as
adding a model node does; the bindings and the sampler are settings of the node, not reach (as a model node's parameters).
At every scheduled run the author's `CanRun` is checked again (`planBayes`).

## «Not up to date»

`bayesFingerprint(definition, node)` = `chainFingerprint(definition, node, own)` with `own` the JSON of the model's id, the
pinned version, the bindings, the sampler and the output (and the replicated draws) — not the label, the timeout or the
retries, as `modelFingerprint` leaves out what does not change the numbers. `scripting_runtime.go` puts it for each
`bayes_model` node; `ModelRun.vue` compares it with the latest output's, as today. A run of the prior predictive check is
**not** the node's output: `latestOutputs` leaves out a run whose `variant` is `prior_predictive`, and the outputs' route
gives it apart (`prior_check`: its run, status and end) for the Report tab.

## The Report tab

`modelReportNodeId` takes `bayes_model` nodes; `StatReportView` draws the latest sampling run through `/tasks/model-report`
(the gateway's `modelNode` widened; the run's kept `model` and `params` choose the variant, verified). When the node also
has a prior predictive check, a switch «Posterior | Prior predictive check» shows either; each says the run's date and,
when the node's fingerprint changed since, «not up to date». PDF and Excel as every model report.

## The run page

`ModelRunDetail.vue` (verified: it reads the run's `info`) shows for a Bayesian model's run: the saved model's name and
version, the compile state while the step waited («compiling the model first (usually 10–30 s)», then «compiled in 12 s»),
the rows read, the attempts and exit code, the runner's sentence on a refusal, the log's tail; nothing about MLflow.

## Memory

The gateway holds one stored version (bounded by the graph's limits), the node's JSON and the task (tens of KB); the input,
the data and the draws never pass through it (the scripts bucket, the runner). The rule of 2026-11-22 concerns the runner.

## Tech stack

- **Go** 1.27, standard library and `pgx/v5`: `internal/api` (`jobs_bayes.go`: `planBayes`, `runBayes`; the save; the kinds),
  `internal/flowresolver`, `internal/flowaccess`, `internal/dbtexport`; no new module.
- **TypeScript / Vue 3**: `components/bayes/BayesNodePanel.vue`, `components/nodes/BayesModelNode.vue`, the editor's sets,
  the five locales; no new package.

## Commands

```bash
cd gateway-go && go test ./internal/flowresolver/... ./internal/flowaccess/... ./internal/dbtexport/... ./internal/bayesmodels/...
cd gateway-go && scripts/test.sh ./internal/api/ -run 'Bayes|Flow|Trial|ModelReport'   # on TEST_DATABASE_URL
cd frontend && npm test
cd frontend && npx nuxi prepare && npx -y -p typescript@5.6.3 -p vue-tsc@2.2.10 vue-tsc --noEmit -p .nuxt/tsconfig.json
```

## Project structure

```text
gateway-go/internal/api/jobs_bayes.go        planBayes, runBayes, bayesFingerprint, the wait for the binary (+ the runner spec's bayesJobManifest)
gateway-go/internal/api/jobs_bayes_test.go   fake cluster, fake compiler, fake engine, on TEST_DATABASE_URL
gateway-go/internal/api/flows.go, flows_body.go   (changed) the lock and the pins' checks in the save's transaction
gateway-go/internal/api/{jobs_queue,scripting_trial,scripting_runtime,proxy_model_report,jobs_models}.go   (changed) the kind
gateway-go/internal/flowresolver/flowresolver.go, flowaccess/reach.go, dbtexport/export.go   (changed) the kind, the reach
frontend/components/bayes/BayesNodePanel.vue, frontend/components/nodes/BayesModelNode.vue
frontend/components/{FlowEditor.vue, ui/NodePanel.vue, ui/OpSidebar.vue, models/ModelRunDetail.vue}, frontend/composables/{flowChain,useFlowModel}.ts
frontend/i18n/locales/*.gen.ts               bayesNode.*
frontend/tests/bayes-node.test.mjs           the bindings' rules, the version move, the fingerprint's keys
```

## Code style

What a definition pins, as the store wrote it (verified, `internal/bayesmodels/uses.go`):

```go
// Pinned is what a flow definition's nodes pin: every node whose data names a model by an integer
// id (whatever its type: a node that names a model keeps it from being deleted).
func Pinned(definition string) []Pin {
```

This module's in the same voice (a sketch; lot F1 writes it): the locks taken in one order, so two saves never wait on each
other in a circle.

```go
// lockPins takes, in the save's transaction, the advisory lock of every model the definition pins, in
// increasing id order, and checks that each pinned version exists: a Delete then waits for the commit
// and finds the flow among the references.
func lockPins(ctx context.Context, tx pgx.Tx, pins []bayesmodels.Pin) (*bayesmodels.Pin, error) {
	sort.Slice(pins, func(i, j int) bool { return pins[i].ModelID < pins[j].ModelID })
	for _, p := range pins {
		if _, err := tx.Exec(ctx, `SELECT pg_advisory_xact_lock($1::integer, $2::integer)`, bayesmodels.Lock, p.ModelID); err != nil {
			return nil, err
		}
	}
```

## Testing strategy

Small by construction: the templates' saved programs, a fake cluster (`modelAttemptJob` played as the model tests do), a
fake compiler, a throw-away database, a few hundred rows.

- **The kind** (F0): a flow with a source, a Bayesian model node and an output resolves with the node as a boundary, in the
  model's priority; the trial's run has kind `model`; the orphans' closing, the run page's list and the report proxy find it.
- **The save** (F1, on `TEST_DATABASE_URL`): a save pinning a model takes the lock (a concurrent `Delete` waits and then
  refuses with the flow's name — the test runs both, the order forced); a pinned version that does not exist → 422; a new
  pin of a model the saver cannot see → 422 with the same sentence as absent; an unchanged pin of a model the saver lost
  sight of → saved (as a saved script); two saves pinning two models in opposite orders finish (no deadlock); someone else's
  new pin switches the author's schedule off.
- **The plan and the run** (F2): every refusal of `planBayes` with the person who lacks exactly that right (the folder's RUN,
  the model's RUN, the group's use, the model deleted); the binding by node id into the stored manifest's `column`; the
  series' `sort` appended and nothing appended for the other models (their input requests' bytes unchanged); `Need` called
  before the input, the wait deducted from the Job's deadline, `ErrRefused` failing at once with the state's sentence,
  `ErrAltered` then `Need` again; the task passes `Task.Check` and equals a golden `task.json` for `hier_slopes`; the step's
  files deleted, the binary's included; a scheduled run checks the author's `CanRun`.
- **«Not up to date»**: a change of a binding, the seed, the version, the output or the chain before the node changes the
  fingerprint; the label, the timeout and the retries do not; a prior predictive trial never becomes the node's output.
- **The panel** (F3): the Node test of the pure rules (`bayes-node.test.mjs`: the columns a type accepts, the version move
  keeping and clearing bindings, the unbound variables named); `vue-tsc`; by hand on Chrome and Firefox (the owner's rule),
  keyboard only included.
- **On kind, by hand, one heavy job at a time** (F5): the group enabled; the ten templates saved and compiled in the
  background; for each, a flow from a sample table to the node (bound, a trial on the development sample, then a full run);
  the summary compared with the template's `<id>.expected.json` by its test (`bayes-templates.md`: «Once `bayes-runner`
  exists, `flow-node`'s trial on kind runs the ten templates … and compares its summary»); a prior predictive check of the
  hierarchical template; a scheduled run by its author; the Report tab in PDF and Excel; a binary altered by hand in the
  scripts bucket refused (`binary_hash`).
- **Regression**: the model and Predict nodes' suites, the resolver's, the flows' saves, the schedule's authority, the
  frontend's suite.

## Boundaries

- **Always**: the pinned version's stored program, manifests and mapping; the binding only in the node; `CanRun` with the
  run's authority at every run; the lock at every save that pins; the compile's wait inside the step's timeout; refusals
  before any Job with their sentence; units, labels and column names as data.
- **Ask first**: running a version other than the pinned one; a new kind of run; an MLflow section before `bayes-mlflow`;
  another place for the binding (the model's editor binds nothing: answer 6).
- **Never**: a program, a manifest or a binary hash received from the browser; a model run by someone who cannot run it;
  the pin moved without the person's act; a flow saved that pins a model being deleted; a Python route.

## Success criteria

1. **The release's criterion**: on kind, a flow from a table to a «Bayesian model» node that pins a saved template, bound
   to its columns, runs as a trial and as a full run, and its Report tab shows the summary, the diagnostics as measures and
   facts, the charts and the posterior predictive checks; the ten templates' summaries agree with their reference values by
   the templates spec's test.
2. A model pinned by a flow being saved is never deleted; a deleted or invisible model is never pinned by a save.
3. Every refusal of «From the node to the Job» trips with the right missing and passes with it, before any Job.
4. A newer version that changes the model shows the notice; «Use it» keeps the bindings that fit and names the unbound
   variables; a layout-only version shows nothing.
5. «Not up to date» follows the fingerprint's keys and only them; a prior predictive check never feeds the nodes after it.
6. The compile's fallback: a version whose binary is not ready runs after its compile within the step's timeout; one that
   passed the compile deadline fails at once with the state's sentence.

## Lots

| Lot | Objective | Files | Accepted when | Depends on | Agent |
|---|---|---|---|---|---|
| **F0** | The step's kind in every list (Go and TypeScript), `runBayes` refusing «not built» | «The step's kind, everywhere» | the kind's tests; every suite green | — | sonnet |
| **F1** | The save: the lock, the pins' existence, the see-check, the reach | `flows.go`, `flows_body.go`, `flowaccess/reach.go`, tests | criterion 2; «The save» tests | — | **opus** (concurrency and permissions) |
| **F2** | `planBayes`, `runBayes`: the task, the input with its sort, the binary's wait and place, the Job, the import, the fingerprint | `jobs_bayes.go`, `jobs_models.go`, `scripting_runtime.go`, tests | criteria 3, 5, 6 on the fake cluster | F0; the runner spec's R5 and its task fields (R4); `bayes-report`'s Q1 (the catalogue's entry the import checks against) | **opus** (the binary's integrity, the run's authority) |
| **F3** | The panel and the canvas node, five languages | `BayesNodePanel.vue`, `BayesModelNode.vue`, the editor's sets, locales, tests | criterion 4; Chrome and Firefox by hand | F0 | sonnet |
| **F4** | The prior predictive check as a trial; the Report tab's switch; the run page | `scripting_trial.go`, `scripting_runtime.go`, `FlowEditor.vue`, `ModelRunDetail.vue`, tests | its tests | F2, F3; the runner spec's R3 | sonnet |
| **F5** | The trial on kind (the release's criterion) | «As built» | criterion 1 | F1–F4; the runner spec's R6; `bayes-report`'s Q4 | sonnet (by hand, alone) |

F0, F1 and F3 can start at once; F5 is the only heavy lot.

## Corrections made to the map and to the approved specs (2026-11-24)

Applied on the owner's approval of 2026-11-24, each marked «Correction of 2026-11-24 from the step-5 specs» where it lands:

- **The map**, `flow-node`'s «Choices»: the runs' kind is `model` (the node's type `bayes_model`); the prior predictive check
  is a trial of the node with `{"check": "prior_predictive"}`, never its output; the compile is asked when the step is
  planned, before the input is prepared.
- **`bayes-model-store.md`**, «"Used by N flows" and the pinned version»: the lock `Lock` is taken by `saveFlow`,
  `insertFlow` and the promotion, in increasing id order, with the existence of the pinned version checked under it.
- **`bayes-stan-compile.md`**, «A run (what `flow-node` calls)»: `Binaries` has `Need` (as built by C3), and the wait is
  deducted from the Job's deadline and `time_budget_seconds`.
- **`bayes-contract.md`**, `task.json`: `bayes.series`, `finite_variance`, `scale_pairs`, `model.program_bytes`,
  `model.image` written by this module (the runner spec's table).

## Decisions of 2026-11-24 (the answers to this spec's questions)

| Question | Decision | By |
|---|---|---|
| How much of the initial values the panel offers | **The radius, or a value for each scalar parameter** (the others drawn by CmdStan within its default radius); no values for vector parameters. That CmdStan accepts a partial init file is checked by F2 on the image | Coordinator, 2026-11-24, on the owner's approval of the spec |
| What 1.5.0 «Augustus» needs | **Everything of the three specs**: the sampling, the summary and the diagnostics, the posterior predictive checks, the prior predictive check, the replicated output and the original scale (runner R0–R6, report Q0–Q5, this spec's F0–F5) | Owner, 2026-11-24 |

## Open questions

None.

## As built — lot F2 (2026-11-24)

- **`planBayes`** (`internal/api/jobs_bayes.go`) returns a `*bayesStep` (the model step's `*modelStep` — label, group,
  `{mode, output}` checked by the catalogue's `bayes_model`, image, timeout, retries, budget — and the stored version, its
  decoded graph and manifests, the bound data, the sampler, the series, the input's columns and sort, the compile's request
  and state), refusing in this order: the models off; the group `bayes` (`checkModelGroup`: ready, usable by the run's
  authority); no model or version named («choose a saved model in its panel»); the model absent, unseen or not runnable
  (`CanRun`, one sentence: «the Bayesian model of «School scores» is not available to <email>»); the version gone (F1's
  sentence); a stored graph that no longer validates; an unbound variable («bind «Predictor» to a column», the graph's label);
  the sampler (the contract's ranges, the defaults for an absent key, **the seed required**: «its seed is missing: draw one
  in its panel»), the initial values (a radius ≥ 0, or values by a parameter's Stan identifier: a number for a single value,
  a list for a vector), the output, the replicated thinning (1 … draws, default 50), the timeout within the group's, the
  retries; then `Binaries.Need` with the run's authority (`ErrRefused` fails with the state's sentence). The editor's trial
  route calls it before inserting its run (409 models off, 403 the group's use or the model not available, 422 the rest), so
  the compile is queued from the moment the person presses Run.
- **`runBayes`**: the child run (kind `model`, its `info` the model step's plus `bayes_model {id, version} (never the name: the run page asks the model, a 404 reads «a model you cannot see»)` and
  `compile {state, detail, waiting[, seconds]}`), the input (`modelPrepareInput` gained `then []any`, the operations after the
  `select`: nil for the model and Predict nodes, whose requests keep their bytes — `modelInputRequest`, tested; for a series
  the engine's `sort` `{"by": [<the group index's column of series.by>, <the time column>]}`, or the time alone for one
  series), the rows against `max_rows`, then the binary: asked again every 2 s (`bayesWaitTick`, on `bayesNow`/`bayesSleep`,
  played by the tests) while the step's timeout lasts, `info.compile` saved when the state changes; `Place` at
  `<prefix>/model`; `ErrAltered`/`ErrGone` forget it and ask again. The whole seconds waited (rounded up) are taken from the
  timeout the Job's deadline and `time_budget_seconds` receive. A step still compiling when its timeout ends fails («the model
  was still compiling when the step's timeout (N s) ended»), with no Job.
- **The task**: `modelgroups.Task` mode `sample`, `params {mode, output}`, `write_output` when the node has nodes after it or
  is tried and the output is not `results`; the limits `max_result_rows`, `max_chart_points` (MODELS__MAX_CHART_POINTS), every
  `bayescontract.Limits` key (the catalogue's caps from the catalogue, the others their defaults), then `bayesSetRunLimits`
  (`/tmp` the card's sampling `/tmp`); `bayes.model` with `cmdstan` from the recipe, `program_bytes` the stored program's
  length, `image` the group image's reference by digest; `plates` from the stored graph; `data` with `column` on the four
  kinds; `checks` = `TaskChecks(true)` when the stored manifest holds `checks_on`; `finite_variance` and `scale_pairs` passed
  as `bayeschecks` returns them; `bayes.series` `{by, time}` (**added to `bayescontract.Task`**, `omitempty`, checked: `by` a
  group plate with a group index, `time` a name or null; refused in the compile mode). `Task.Check` before the ConfigMap; a
  failure is «a fault of Tabularia». Golden: `internal/api/testdata/bayes_task_hier_slopes.json` (the run's ids, start,
  hashes of the recipe and runner and the model's id as placeholders; `BAYES_TASK_WRITE=1` rewrites it).
- **The Job**: `bayesJobManifest` with the sampling `/tmp`, `bayesEnvironment` (`TABULARIA_BINARY_IN`, the allocator's
  variables), `TABULARIA_INPUT`, `TABULARIA_RESULTS`, `TABULARIA_ARTIFACTS`, and `TABULARIA_OUTPUT` only when the output is
  written; `modelAttempts` (the files deleted between attempts never include the input nor the binary); `importModel`
  against `bayes_model` with `{mode, output}` (its sentences now name the step by `what()`: «Bayesian model «…»», unchanged
  for a model node); `bayesStepKeys` deleted at the end, the binary's included, also on a failure.
- **«Not up to date»**: `bayesFingerprint` = `chainFingerprint` with the model, the version, the bindings, the sampler, the
  output (`summary` when absent) and, only with the output `replicated`, the thinning, written with sorted keys;
  `scripting_runtime.go` puts it for every `bayes_model` node.
- **A partial init file**, checked on the image of B0 (CmdStan 2.40.0, `diagnose` with `init={"mu": 0.5}` on a program with
  `mu`, `sigma`, `b[2]`, two seeds): `mu` starts at 0.5 under both seeds, the others are drawn within CmdStan's default
  radius; `sample` with the same file exits 0. The panel's choice («Decisions of 2026-11-24») holds.
- **Left to F4**: the prior predictive check (`mode` `prior_predictive`: the sampler `{4, 0, 250, seed}`, output `results`,
  checks on, `latestOutputs` leaving its runs out), the Report tab's switch and the run page's words for `info.compile`.
  Until the runner's R2 and R3 are committed it refuses `not_built` a task with the checks on (every version stored by
  codegen 2 and later), the output `replicated` and the prior predictive.
- Tests (`jobs_bayes_test.go`, on TEST_DATABASE_URL, the model tests' fake cluster and engine, a fake `Binaries`, a played
  clock): the whole step (Need before the input, three questions = 6 s off the deadline, the binary placed where the Job
  fetches it, the golden task, the import, the files with the binary's, «up to date» after it); every refusal with the
  person or the setting that lacks exactly it (a schedule's author included), with no run and no input; an altered and a gone
  binary compiled again; a compile longer than the timeout and a refusal during the wait; the trial route (403 without RUN on
  the model, a `model` run in the trials' namespace); the fingerprint's keys; the series' sort (one series, a series by a
  group plate).

## As built — lot F5 (2026-11-24, kind trial)

On `kind-tabularia` (three nodes, Calico), images of `ad0361e7` for the engine and the two Python services and of `741cc94c` for the Go gateway and the interface, Helm with `--reset-then-reuse-values`, the group `bayes` enabled through `POST /admin/models/bayes/enable` (the call the administration button makes): **building 510 s, checking 81 s, ready at 591 s**. One heavy job at a time.

- **The ten templates**, each saved from its template and compiled (14.7 to 29.7 s each, see the compile spec's «As built — lot C5»), in a flow from a table of the templates' reference CSVs (`testdata/reference`, 200 to 500 rows) to a «Bayesian model» node pinning it, every variable bound by its name: a **trial** on the development sample (6 to 14 s each) and a **full run** with an Output node (9 to 18 s each), ten out of ten ended `SUCCESS` with exit code 0 at the first attempt.
- **The templates spec's test, by the node**: the summary of each full run (`output = summary`, 4 chains of 1,000 draws after 1,000 of warm-up, the seed of the flows) compared with `<id>.expected.json` quantity by quantity, the mean and the standard deviation, `z = (node − reference) / sqrt(mcse_node² + mcse_reference²)`, two-sided normal p-value, Holm over **598 comparisons** at alpha 0.001 (the same count as the reference run): **smallest raw p 2.3e-4, smallest Holm-adjusted p 0.137, none below alpha**. The reference file's names that the summary does not carry (`beta.1`, `z_*`, `L.*`, `eff.*`: the aliases and the internal variables) are not compared.
- **The original scale, `glm_linear` against a regression on the raw columns** (the runner spec's R4 left this to kind): on the 300 rows of the template's data, `b_x` on the column's own scale 0.0807 (sd 0.0066) against the least-squares slope 0.08081 (standard error 0.00662); the intercept −2.5985 (sd 0.3451) against −2.6008 (standard error 0.3430). The hierarchical templates carry `original` rows for `a_g[·]` too.
- **The prior predictive check** as a trial (the node's button): `hier_intercepts`, `hier_slopes`, `hier_nested` and `glm_linear` each ended `SUCCESS` in 6 to 8 s, wrote no output of the node (its rows 1,175 to 1,677 are the check's), and the Report's switch shows «Prior predictive check» for it.
- **The Report tab, PDF and Excel** of the ten full runs: svg pages 4 to 8, rendered in 2 to 5 s each (the three formats), PDF 129 to 287 KB, Excel 15 to 36 KB; on screen in Chrome and in Firefox (below).
- **A scheduled run by its author** (`glm_logistic`, `* * * * *`): fired within the minute, the model run has `trigger_type schedule` and `launched_by` the author, `SUCCESS` in 54 s; the schedule was switched off after.
- **A binary altered by hand**: one byte of the copy placed in the scripts bucket changed between the place and the Job → the step failed at once with «the compiled model is not the one Tabularia recorded: it is not run» (the runner's `binary_hash`), no sampling; a byte of the copy in Tabularia's storage changed → refused at the place and the model compiled again (the compile spec's C5).
- **The page of the example** (`docs/specs/bayes-docs.md`, «As built — lot D5»): the trial of a table of 104,819 rows with no development sample on its source reads every row: 330 s.
- **By hand, Chrome and Firefox** (1600 px, dark): the template's question dialog, the node's panel (saved model, the two variables, sampler, output, MLflow, limits, **Run** and **Run the prior predictive check**), the Report tab with its switch; no error in the console of either browser other than a 404 for a missing optional resource of the page.
- **Found, not corrected** (a shared component, outside this lot): in `components/report/ReportPages.vue`, switching the Report from the posterior to the prior predictive check keeps the viewer's scroll position, so the check's two pages are first seen scrolled to their end (a white page foot, «Page 2 of 2») in both browsers; scrolling up shows the report. Proposed correction: scroll to the top when the pages shown are another document (the node's `view` changes), keeping the proportional scroll only for a change of zoom; a test in `report-pages` of the frontend's suite.
- **Environment (not Tabularia)**: the kind guide's registry address in `networkPolicy.userCodePrivateCIDRs` is the registry container's address on the Docker network; after a restart of Docker the registry had another address and the group's build could not push (i/o timeout at the push) until the value was set again (`--set-json 'networkPolicy.userCodePrivateCIDRs=["<address>/32"]'`).
