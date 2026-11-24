package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescompile"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayestemplates"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowresolver"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
)

// A Bayesian model node as a step (docs/specs/bayes-flow-node.md, lot F2) on TEST_DATABASE_URL, with the model tests'
// fake cluster and engine, a fake compiler and a played clock: the plan's refusals, each with the person who lacks exactly
// that right; the compile asked for before the input; the wait deducted from the Job's deadline; the binary placed, an
// altered one compiled again; the task against a golden task.json; the step's files deleted, the binary's included.

func TestABayesNodesRunsHaveTheModelsKind(t *testing.T) {
	if bayesRunKind("bayes_model") != "model" || bayesRunKind("model") != "model" || bayesRunKind("script") != "script" {
		t.Error("the kind of a node's runs")
	}
}

// A model node never runs the Bayesian model: it is a node of its own.
func TestAModelNodeNeverRunsTheBayesianModel(t *testing.T) {
	f := newModelRunFixture(t)
	_, definition, node := f.flow(`{"outcome": "y"}`, false)
	_ = definition
	data := nodeData(node)
	data.Set("model", "bayes_model")
	_, err := f.q.planModel(f.ctx, f.ann, node)
	var sf *scriptFailed
	if !errors.As(err, &sf) || !strings.Contains(sf.msg, "add «Bayesian model» from the palette") {
		t.Errorf("model naming bayes_model: %v", err)
	}
	node.Set("type", "bayes_model")
	if _, err = f.q.planModel(f.ctx, f.ann, node); !errors.As(err, &sf) || !strings.Contains(sf.msg, "a node of its own") {
		t.Errorf("a Bayesian node given to planModel: %v", err)
	}
}

// ── the fake compiler ─────────────────────────────────────────────────────────

// fakeBinaries is stan-compile for the runs: Need answers the states in turn (the last repeats; "ready" gives the binary,
// "refused" ErrRefused with a timeout's sentence), Place the errors in turn (the last repeats).
type fakeBinaries struct {
	mu     sync.Mutex
	states []string
	places []error
	needs  []bayesmodels.CompileRequest
	placed []string
	events func(string)
}

func (b *fakeBinaries) Enqueue(context.Context, bayesmodels.CompileRequest) error { return nil }

func (b *fakeBinaries) State(context.Context, string) (bayesmodels.CompileState, error) {
	return bayesmodels.CompileState{State: bayesmodels.CompileNone}, nil
}

func (b *fakeBinaries) Ready(context.Context, string) (*bayescompile.Binary, bayesmodels.CompileState, error) {
	return nil, bayesmodels.CompileState{State: bayesmodels.CompileNone}, nil
}

const fakeBinarySHA = "b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1b1"

func (b *fakeBinaries) Need(_ context.Context, r bayesmodels.CompileRequest) (*bayescompile.Binary, bayesmodels.CompileState, error) {
	b.mu.Lock()
	b.needs = append(b.needs, r)
	state := bayesmodels.CompileReady
	if len(b.states) > 0 {
		state = b.states[0]
		if len(b.states) > 1 {
			b.states = b.states[1:]
		}
	}
	b.mu.Unlock()
	b.events("need")
	switch state {
	case "ready":
		return &bayescompile.Binary{CacheKey: "k", BinarySHA256: fakeBinarySHA, Bytes: 9}, bayesmodels.CompileState{State: state, Detail: "Compiled."}, nil
	case "refused":
		return nil, bayesmodels.CompileState{State: bayesmodels.CompileTimeout,
			Detail: "the program did not compile within the limit of 300 s (23 Nov): it is not compiled again with this limit"}, bayescompile.ErrRefused
	}
	return nil, bayesmodels.CompileState{State: state, Detail: "Compiling since 14:02 (usually 10–30 s)."}, nil
}

func (b *fakeBinaries) Place(_ context.Context, _ *bayescompile.Binary, key string) error {
	b.mu.Lock()
	b.placed = append(b.placed, key)
	var err error
	if len(b.places) > 0 {
		err = b.places[0]
		if len(b.places) > 1 {
			b.places = b.places[1:]
		}
	}
	b.mu.Unlock()
	b.events("place")
	return err
}

func (b *fakeBinaries) needCount() int {
	b.mu.Lock()
	defer b.mu.Unlock()
	return len(b.needs)
}

// ── the world ─────────────────────────────────────────────────────────────────

type bayesRunWorld struct {
	*modelRunFixture
	binaries *fakeBinaries
	store    *bayesmodels.Store
	models   int64 // the folder of the saved models
	// the persons: each has RUN on the flows' folder (f.folder) and on the models' folder what its name says
	runner, viewer, outsider *auth.User
	bayesEnv                 int64
	clock                    time.Time
	events                   []string
	then                     []any // what the input appended after its select
	mu                       sync.Mutex
}

func (w *bayesRunWorld) event(e string) {
	w.mu.Lock()
	defer w.mu.Unlock()
	w.events = append(w.events, e)
}

func (w *bayesRunWorld) seen() []string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return slices.Clone(w.events)
}

func newBayesRunWorld(t *testing.T) *bayesRunWorld {
	t.Helper()
	w := &bayesRunWorld{modelRunFixture: newModelRunFixture(t), clock: time.Date(2026, 11, 24, 12, 0, 0, 0, time.UTC)}
	f := w.modelRunFixture
	w.binaries = &fakeBinaries{events: w.event}
	f.d.BayesCompiler = w.binaries
	w.store = &bayesmodels.Store{DB: f.db, Compiler: w.binaries, Log: f.d.Log}
	one := func(sql string, args ...any) int64 {
		t.Helper()
		var id int64
		if err := f.db.QueryRow(f.ctx, sql, args...).Scan(&id); err != nil {
			t.Fatalf("%s: %v", sql, err)
		}
		return id
	}
	exec := func(sql string, args ...any) {
		t.Helper()
		if _, err := f.db.Exec(f.ctx, sql, args...); err != nil {
			t.Fatalf("%s: %v", sql, err)
		}
	}
	stamp := time.Now().UnixNano()
	w.models = one(`INSERT INTO projects (name, description, created_at) VALUES ($1, '', now()) RETURNING id`, fmt.Sprintf("bayes-models-%d", stamp))
	exec(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'manage')`, w.models, f.ann.ID)
	user := func(name, onModels string) *auth.User {
		email := fmt.Sprintf("bayes-%s-%d@x.test", name, stamp)
		id := one(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, $2, 'x', true, false, false, false, now()) RETURNING id`, email, name)
		exec(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'run')`, f.folder, id)
		if onModels != "" {
			exec(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, $3)`, w.models, id, onModels)
		}
		return &auth.User{ID: id, Email: email, FullName: name, IsActive: true}
	}
	w.runner, w.viewer, w.outsider = user("runner", "run"), user("viewer", "view"), user("outsider", "")
	// the group bayes, ready, its image by digest
	shipped, ok := modelgroups.Get("bayes")
	if !ok {
		t.Fatal("no group bayes")
	}
	exec(`DELETE FROM bayes_settings`)
	_, _ = f.db.Exec(f.ctx, `DELETE FROM model_groups WHERE id = 'bayes'`)
	_, _ = f.db.Exec(f.ctx, `DELETE FROM script_environments WHERE managed_by = 'model_group:bayes'`)
	w.bayesEnv = one(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
		memory_limit, max_timeout_seconds, created_at, updated_at, managed_by) VALUES ($1, '', 'python', true, '1', '4', '2Gi', '2Gi', 3600,
		now(), now(), 'model_group:bayes') RETURNING id`, fmt.Sprintf("Bayes %d", stamp))
	version := one(`INSERT INTO script_environment_versions (environment_id, number, language_version, base_image, packages, status,
		image_ref, arch, recipe_hash, runner_hash, created_at) VALUES ($1, 5, '3.14', 'python:3.14-slim', '', 'ready', $2, 'amd64', $3, $4, now())
		RETURNING id`, w.bayesEnv, "registry:5000/envs/tabularia-env-9@sha256:"+strings.Repeat("d", 64), shipped.RecipeHash, shipped.RunnerHash)
	exec(`UPDATE script_environments SET current_version_id = $1 WHERE id = $2`, version, w.bayesEnv)
	w.groupFor(true)
	// the input and the clock, played
	prevInput, prevNow, prevSleep := modelPrepareInput, bayesNow, bayesSleep
	modelPrepareInput = func(_ *queue, _ context.Context, _ *scriptNodeRun, _ *childRun, _ *scriptStorage, _ string,
		columns []string, then []any, _ string) (*modelInput, error) {
		w.mu.Lock()
		f.columns, w.then = columns, then
		w.mu.Unlock()
		w.event("input")
		rows := f.rows
		return &modelInput{rows: &rows, sample: []any{}}, nil
	}
	bayesNow = func() time.Time {
		w.mu.Lock()
		defer w.mu.Unlock()
		return w.clock
	}
	bayesSleep = func(ctx context.Context, d time.Duration) bool {
		w.mu.Lock()
		defer w.mu.Unlock()
		w.clock = w.clock.Add(d)
		return ctx.Err() == nil
	}
	prevAttempt := modelAttemptJob
	modelAttemptJob = func(ctx context.Context, k scriptcfg.Kubernetes, name string, manifest pyjson.Object, code, secret pyjson.Object,
		timeout int64, ns string, poll float64, logMax int) (bool, string, any, string, error) {
		w.event("job")
		return prevAttempt(ctx, k, name, manifest, code, secret, timeout, ns, poll, logMax)
	}
	t.Cleanup(func() {
		modelPrepareInput, bayesNow, bayesSleep, modelAttemptJob = prevInput, prevNow, prevSleep, prevAttempt
		_, _ = f.db.Exec(f.ctx, `DELETE FROM runs WHERE flow_id IN (SELECT id FROM flows WHERE project_id = $1)`, f.folder)
		_, _ = f.db.Exec(f.ctx, `DELETE FROM flows WHERE project_id = $1`, f.folder)
		_, _ = f.db.Exec(f.ctx, `DELETE FROM bayes_model_versions WHERE model_id IN (SELECT id FROM bayes_models WHERE project_id = $1)`, w.models)
		_, _ = f.db.Exec(f.ctx, `DELETE FROM bayes_models WHERE project_id = $1`, w.models)
		_, _ = f.db.Exec(f.ctx, `DELETE FROM permissions WHERE project_id = $1 OR user_id = ANY($2)`, w.models,
			[]int64{w.runner.ID, w.viewer.ID, w.outsider.ID})
		_, _ = f.db.Exec(f.ctx, `DELETE FROM projects WHERE id = $1`, w.models)
		_, _ = f.db.Exec(f.ctx, `DELETE FROM model_groups WHERE id = 'bayes'`)
		_, _ = f.db.Exec(f.ctx, `UPDATE script_environments SET current_version_id = NULL WHERE id = $1`, w.bayesEnv)
		_, _ = f.db.Exec(f.ctx, `DELETE FROM script_environment_groups WHERE environment_id = $1`, w.bayesEnv)
		_, _ = f.db.Exec(f.ctx, `DELETE FROM script_environment_versions WHERE environment_id = $1`, w.bayesEnv)
		_, _ = f.db.Exec(f.ctx, `DELETE FROM script_environments WHERE id = $1`, w.bayesEnv)
		_, _ = f.db.Exec(f.ctx, `DELETE FROM runs WHERE launched_by = ANY($1)`, []int64{w.runner.ID, w.viewer.ID, w.outsider.ID})
		_, _ = f.db.Exec(f.ctx, `DELETE FROM audit_logs WHERE actor_id = ANY($1)`, []int64{w.runner.ID, w.viewer.ID, w.outsider.ID})
		_, _ = f.db.Exec(f.ctx, `DELETE FROM users WHERE id = ANY($1)`, []int64{w.runner.ID, w.viewer.ID, w.outsider.ID})
	})
	return w
}

// groupFor sets the group bayes for everyone, or for nobody but administrators (no user group holds its environment).
func (w *bayesRunWorld) groupFor(everyone bool) {
	w.t.Helper()
	_, _ = w.db.Exec(w.ctx, `DELETE FROM model_groups WHERE id = 'bayes'`)
	if _, err := w.db.Exec(w.ctx, `INSERT INTO model_groups (id, environment_id, enabled, everyone, updated_at) VALUES ('bayes', $1, true, $2, now())`,
		w.bayesEnv, everyone); err != nil {
		w.t.Fatal(err)
	}
}

// save stores a model from a graph in the models' folder (version 1, saved by ann).
func (w *bayesRunWorld) save(name string, graph []byte) *bayesmodels.Saved {
	w.t.Helper()
	saved, err := w.store.Create(w.ctx, bayesmodels.NewModel{ProjectID: w.models, Name: name, Graph: graph}, w.ann.ID)
	if err != nil {
		w.t.Fatalf("the model %s: %v", name, err)
	}
	return saved
}

func templateGraph(t *testing.T, id string) []byte {
	t.Helper()
	tp, ok := bayestemplates.Get(id)
	if !ok {
		t.Fatalf("no template %s", id)
	}
	return tp.Graph
}

// bayesNodeJSON is a Bayesian model node with the panel's defaults (a timeout of 1,800 s) and the given data over them.
func bayesNodeJSON(model, version int64, bindings map[string]string, more map[string]any) string {
	data := map[string]any{"label": "School scores", "bayesModelId": model, "bayesModelVersion": version, "bindings": bindings,
		"sampler": map[string]any{"chains": 4, "warmup": 1000, "draws": 1000, "seed": 1840277361, "adapt_delta": 0.8,
			"max_treedepth": 10, "init": nil},
		"output": "summary", "replicatedDraws": 50, "timeoutSeconds": 1800, "retries": 0, "retryDelaySeconds": 60}
	for k, v := range more {
		data[k] = v
	}
	b, _ := json.Marshal(map[string]any{"id": "b", "type": "bayes_model", "data": data})
	return string(b)
}

// flowWith saves a flow source → node b (→ output when next), and gives it with its node.
func (w *bayesRunWorld) flowWith(node string, next bool) (*jobFlow, pyjson.Object, pyjson.Object) {
	w.t.Helper()
	nodes := `{"id": "s", "type": "source", "data": {"datasourceId": 1}}, ` + node
	edges := `{"source": "s", "target": "b"}`
	if next {
		nodes += `, {"id": "o", "type": "output", "data": {"name": "summary"}}`
		edges += `, {"source": "b", "target": "o"}`
	}
	text := `{"nodes": [` + nodes + `], "edges": [` + edges + `]}`
	var id int64
	if err := w.db.QueryRow(w.ctx, `INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
		VALUES ('Schools', '', $1, $2, $3, 'duckdb', now(), now()) RETURNING id`, w.folder, w.ann.ID, text).Scan(&id); err != nil {
		w.t.Fatal(err)
	}
	definition := mustDef(w.t, text)
	var b pyjson.Object
	for _, n := range objectList(definition.Value("nodes")) {
		if n.Value("id") == "b" {
			b = n
		}
	}
	return &jobFlow{ID: id, ProjectID: w.folder, Name: "Schools", Definition: text, Engine: "duckdb"}, definition, b
}

// step runs the node as a flow's step, as the person (the run's authority); the latest model run of the flow.
func (w *bayesRunWorld) step(u *auth.User, node string, next bool, trigger string) (int64, int64, error) {
	w.t.Helper()
	flow, definition, b := w.flowWith(node, next)
	r := &scriptNodeRun{q: w.q, user: u, flow: flow, definition: definition, node: b, trigger: trigger, mode: "development",
		outputs: flowresolver.ScriptOutputs{}}
	_, err := w.q.runBayes(w.ctx, r)
	var id int64
	_ = w.db.QueryRow(w.ctx, `SELECT coalesce(max(id), 0) FROM runs WHERE flow_id = $1 AND kind = 'model'`, flow.ID).Scan(&id)
	return flow.ID, id, err
}

// bayesTaskOf is the task.json an attempt's ConfigMap held.
func bayesTaskOf(t *testing.T, a modelAttemptSeen) (modelgroups.Task, string) {
	t.Helper()
	for _, kv := range a.code {
		if s, _ := kv.Value.(string); strings.HasPrefix(s, `{"protocol":`) {
			var task modelgroups.Task
			if err := json.Unmarshal([]byte(s), &task); err != nil {
				t.Fatal(err)
			}
			return task, s
		}
	}
	t.Fatal("no task.json in the ConfigMap")
	return modelgroups.Task{}, ""
}

func containerEnv(t *testing.T, manifest pyjson.Object) map[string]string {
	t.Helper()
	spec := pyjson.Obj(pyjson.Obj(pyjson.Obj(manifest.Value("spec")).Value("template")).Value("spec"))
	out := map[string]string{}
	for _, c := range objectList(spec.Value("containers")) {
		for _, e := range objectList(c.Value("env")) {
			out[pyjson.Str(e.Value("name"))] = pyjson.Str(e.Value("value"))
		}
	}
	return out
}

var slopesBindings = map[string]string{"y": "score", "x": "hours", "g": "school"}

// The whole step: the compile asked for when planned, before the input; the input's bound columns; the wait for the binary
// (three questions, six seconds) deducted from the Job's deadline and from time_budget_seconds; the binary placed in the
// step's prefix, where the Job fetches it; the task against the golden; the results imported against bayes_model; the
// step's files deleted, the binary's included; the run a model run, up to date until a setting changes.
func TestABayesStepWaitsForItsBinaryRunsItsJobAndKeepsItsResults(t *testing.T) {
	w := newBayesRunWorld(t)
	saved := w.save("Schools", templateGraph(t, "hier_slopes"))
	w.binaries.states = []string{"queued", "running", "running", "ready"}
	flow, id, err := w.step(w.runner, bayesNodeJSON(saved.Model.ID, 1, slopesBindings, nil), true, "manual")
	if err != nil {
		t.Fatal(err)
	}
	r := w.row(id)
	if r.status != "SUCCESS" || r.resultsKey == "" || r.outputKey == r.resultsKey || r.card != `{"format_version": 1}` {
		t.Fatalf("the run: %+v", r)
	}
	// the compile asked for with the run's authority before the input; the binary placed after it, then the Job
	if got := strings.Join(w.seen(), " "); got != "need input need need need place job" {
		t.Errorf("the order: %s", got)
	}
	if n := w.binaries.needs[0]; n.ModelID != saved.Model.ID || n.Version != 1 || n.ProgramSHA256 != saved.Content.ProgramSHA256 ||
		n.By != w.runner.ID || n.Again {
		t.Errorf("the compile's request: %+v", n)
	}
	if strings.Join(w.columns, ",") != "school,hours,score" || w.then != nil {
		t.Errorf("the input: %v then %v", w.columns, w.then)
	}
	var info map[string]any
	if err := json.Unmarshal([]byte(r.script), &info); err != nil {
		t.Fatal(err)
	}
	compile, _ := info["compile"].(map[string]any)
	bm, _ := info["bayes_model"].(map[string]any)
	if info["model"] != "bayes_model" || info["group"] != "bayes" || info["variant"] != "sample" || compile["state"] != "ready" ||
		compile["seconds"] != float64(6) || compile["waiting"] != false || bm["name"] != "Schools" || bm["version"] != float64(1) ||
		info["fingerprint"] == "" {
		t.Errorf("the run's script: %s", r.script)
	}
	// one Job: the sampling Job, its deadline the timeout less the wait
	if len(w.attempts) != 1 {
		t.Fatalf("%d attempts", len(w.attempts))
	}
	a := w.attempts[0]
	spec := pyjson.Obj(a.manifest.Value("spec"))
	if spec.Value("activeDeadlineSeconds") != int64(1794) {
		t.Errorf("the Job's deadline: %v", spec.Value("activeDeadlineSeconds"))
	}
	if !strings.Contains(dump(a.manifest), `{"name": "tmp", "emptyDir": {"sizeLimit": "2Gi"}}`) || a.ns != "scripts" {
		t.Errorf("the Job: %s", dump(a.manifest))
	}
	env := containerEnv(t, a.manifest)
	if len(w.binaries.placed) != 1 || env[bayesEnvBinaryIn] != "s3://scripts/"+w.binaries.placed[0] ||
		!strings.HasPrefix(w.binaries.placed[0], fmt.Sprintf("env-%d/", w.bayesEnv)) || !strings.HasSuffix(w.binaries.placed[0], "/model") {
		t.Errorf("the binary placed at %v, fetched from %s", w.binaries.placed, env[bayesEnvBinaryIn])
	}
	prefix := strings.TrimSuffix(w.binaries.placed[0], "/model")
	for k, v := range map[string]string{modelgroups.EnvInput: prefix + "/input.parquet", modelgroups.EnvOutput: prefix + "/output.parquet",
		modelgroups.EnvResults: prefix + "/results.parquet", modelgroups.EnvArtifacts: prefix + "/artifacts/"} {
		if env[k] != "s3://scripts/"+v {
			t.Errorf("%s = %q", k, env[k])
		}
	}
	if env["MALLOC_MMAP_THRESHOLD_"] != bayesMallocThreshold || env["OMP_NUM_THREADS"] != "4" {
		t.Errorf("the environment: %v", env)
	}
	// the task: the contract's, its budget the timeout less the wait, the binary's hash, the binding by node id
	task, _ := bayesTaskOf(t, a)
	if err := task.Bayes.Check(task.Mode, task.Output); err != nil {
		t.Error(err)
	}
	if task.Mode != "sample" || task.Limits["time_budget_seconds"] != 1794 || task.Limits["tmp_bytes"] != 2<<30 ||
		task.Limits["started_unix"] != int(w.clock.Unix()) || task.IDs.StepRunID != id || task.InputRows != 120 || !task.WriteOutput ||
		task.MLflow || task.Bayes.Model.BinarySHA256 != fakeBinarySHA {
		t.Errorf("task.json: %+v", task)
	}
	columns := map[string]string{}
	for _, e := range task.Bayes.Data {
		if e.Column != "" {
			columns[e.Node] = e.Column
		}
	}
	if fmt.Sprint(columns) != fmt.Sprint(slopesBindings) {
		t.Errorf("the columns by node: %v", columns)
	}
	compareTaskGolden(t, task, saved.Model.ID, id, flow)
	// the import against bayes_model, then the step's files deleted by exact key, the binary's included
	calls := strings.Join(w.engine.calls, "\n")
	if !strings.Contains(calls, `"model":"bayes_model","params":{"mode":"sample","output":"summary"}`) {
		t.Errorf("the import: %s", calls)
	}
	if !strings.Contains(calls, `"`+prefix+`/model"`) || !strings.Contains(calls, `"`+prefix+`/input.parquet"`) {
		t.Errorf("the files deleted: %s", calls)
	}
	// «not up to date»: up to date now; the seed, a binding, the output change it; the label and the timeout do not
	if out := w.outputs(w.runner, flow)["b"]; out == nil || out["output"]["stale"] != false {
		t.Errorf("the outputs: %v", out)
	}
}

// compareTaskGolden compares task.json with testdata/bayes_task_hier_slopes.json, its run's own values (ids, the start, the
// hashes of the recipe and of the runner, the model's id) written as placeholders; BAYES_TASK_WRITE=1 rewrites it.
func compareTaskGolden(t *testing.T, task modelgroups.Task, model, step, flow int64) {
	t.Helper()
	task.IDs = modelgroups.IDs{FlowID: 1, NodeID: task.IDs.NodeID, RunID: 2, StepRunID: 2}
	task.Limits["started_unix"] = 0
	task.RecipeHash, task.RunnerHash = "recipe", "runner"
	if task.Bayes.Model.ID != model {
		t.Errorf("the model's id: %d", task.Bayes.Model.ID)
	}
	task.Bayes.Model.ID = 17
	got, err := json.MarshalIndent(task, "", "  ")
	if err != nil {
		t.Fatal(err)
	}
	got = append(got, '\n')
	path := filepath.Join("testdata", "bayes_task_hier_slopes.json")
	if os.Getenv("BAYES_TASK_WRITE") == "1" {
		if err := os.WriteFile(path, got, 0o644); err != nil {
			t.Fatal(err)
		}
		return
	}
	want, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	if string(got) != string(want) {
		t.Errorf("task.json differs from %s (BAYES_TASK_WRITE=1 rewrites it):\n%s", path, got)
	}
	_ = step
	_ = flow
}

// «Not up to date» follows the fingerprint's keys and only them.
func TestABayesNodesFingerprint(t *testing.T) {
	def := func(node string, upstream string) (pyjson.Object, pyjson.Object) {
		text := `{"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": ` + upstream + `}}, ` + node + `],
			"edges": [{"source": "s", "target": "b"}]}`
		d := mustDef(t, text)
		for _, n := range objectList(d.Value("nodes")) {
			if n.Value("id") == "b" {
				return d, n
			}
		}
		t.Fatal("no node")
		return nil, nil
	}
	base := bayesFingerprint(def(bayesNodeJSON(5, 2, slopesBindings, nil), "1"))
	same := map[string]string{
		"the label":   bayesNodeJSON(5, 2, slopesBindings, map[string]any{"label": "Other"}),
		"the timeout": bayesNodeJSON(5, 2, slopesBindings, map[string]any{"timeoutSeconds": 600, "retries": 2, "retryDelaySeconds": 5}),
		"the thinning without the replicated output": bayesNodeJSON(5, 2, slopesBindings, map[string]any{"replicatedDraws": 7}),
	}
	for what, node := range same {
		if bayesFingerprint(def(node, "1")) != base {
			t.Errorf("%s changed the fingerprint", what)
		}
	}
	other := map[string]string{
		"the seed": bayesNodeJSON(5, 2, slopesBindings, map[string]any{"sampler": map[string]any{"chains": 4, "warmup": 1000,
			"draws": 1000, "seed": 7, "adapt_delta": 0.8, "max_treedepth": 10, "init": nil}}),
		"a binding":   bayesNodeJSON(5, 2, map[string]string{"y": "score", "x": "minutes", "g": "school"}, nil),
		"the version": bayesNodeJSON(5, 3, slopesBindings, nil),
		"the model":   bayesNodeJSON(6, 2, slopesBindings, nil),
		"the output":  bayesNodeJSON(5, 2, slopesBindings, map[string]any{"output": "draws"}),
	}
	for what, node := range other {
		if bayesFingerprint(def(node, "1")) == base {
			t.Errorf("%s did not change the fingerprint", what)
		}
	}
	if bayesFingerprint(def(bayesNodeJSON(5, 2, slopesBindings, nil), "2")) == base {
		t.Error("the chain before it did not change the fingerprint")
	}
	r1 := bayesFingerprint(def(bayesNodeJSON(5, 2, slopesBindings, map[string]any{"output": "replicated"}), "1"))
	r2 := bayesFingerprint(def(bayesNodeJSON(5, 2, slopesBindings, map[string]any{"output": "replicated", "replicatedDraws": 7}), "1"))
	if r1 == r2 {
		t.Error("the replicated output's thinning did not change the fingerprint")
	}
}

// The input's request: a model's keeps its bytes (the select, nothing after it); a series' gets the sort after the select.
func TestTheInputsRequestAppendsTheSeriesSortOnly(t *testing.T) {
	req := func() pyjson.Object {
		return mustDef(t, `{"input_key": "in", "operations": [{"type": "filter", "params": {"column": "a"}}]}`)
	}
	plain := dump(req())
	if got := dump(modelInputRequest(req(), nil, nil)); got != plain {
		t.Errorf("nothing asked: %s", got)
	}
	sel := `{"type": "select", "params": {"columns": ["y", "x"]}}`
	want := strings.Replace(plain, `}}]`, `}}, `+sel+`]`, 1)
	if got := dump(modelInputRequest(req(), []string{"y", "x"}, nil)); got != want {
		t.Errorf("a model's:\n got %s\nwant %s", got, want)
	}
	s := &bayesStep{sortBy: []string{"unit", "week"}}
	got := dump(modelInputRequest(req(), []string{"y", "unit", "week"}, s.sortOps()))
	if !strings.HasSuffix(got, `{"type": "select", "params": {"columns": ["y", "unit", "week"]}}, {"type": "sort", "params": {"by": ["unit", "week"]}}]}`) {
		t.Errorf("a series': %s", got)
	}
	if (&bayesStep{}).sortOps() != nil {
		t.Error("a model without a series sorts")
	}
}

// withSeriesBy is ts_ar with a group plate of units and the series by it.
func withSeriesBy(t *testing.T) []byte {
	t.Helper()
	var g map[string]any
	if err := json.Unmarshal(templateGraph(t, "ts_ar"), &g); err != nil {
		t.Fatal(err)
	}
	plates := g["plates"].([]any)
	rows := plates[0].(map[string]any)
	rows["series"] = map[string]any{"time": "t", "by": "unit"}
	g["plates"] = append(plates, map[string]any{"id": "unit", "kind": "group", "label": "Shop", "parent": nil, "key": "u", "form": "non_centred"})
	g["nodes"] = append(g["nodes"].([]any), map[string]any{"id": "u", "kind": "data", "name": "u", "label": "Shop of the row",
		"plate": "rows", "type": "group_index", "target": "unit"})
	b, err := json.Marshal(g)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

// A series: its time column read and the input sorted by the series' unit and its time; bayes.series names both.
func TestABayesSeriesInputIsSortedByItsUnitAndTime(t *testing.T) {
	w := newBayesRunWorld(t)
	for _, c := range []struct {
		name     string
		graph    []byte
		bindings map[string]string
		columns  string
		sort     string
		series   string
	}{
		{"one series", templateGraph(t, "ts_ar"), map[string]string{"y": "sales", "t": "week"}, "sales,week", `["week"]`,
			`{"by":null,"time":"week"}`},
		{"a series a shop", withSeriesBy(t), map[string]string{"y": "sales", "t": "week", "u": "shop"}, "shop,sales,week",
			`["shop", "week"]`, `{"by":"unit","time":"week"}`},
	} {
		t.Run(c.name, func(t *testing.T) {
			saved := w.save("Sales "+c.name, c.graph)
			w.attempts = nil
			_, _, err := w.step(w.ann, bayesNodeJSON(saved.Model.ID, 1, c.bindings, nil), false, "manual")
			if err != nil {
				t.Fatal(err)
			}
			if strings.Join(w.columns, ",") != c.columns || len(w.then) != 1 || dump(pyjson.Obj(w.then[0])) !=
				`{"type": "sort", "params": {"by": `+c.sort+`}}` {
				t.Errorf("the input: %v then %v", w.columns, w.then)
			}
			task, raw := bayesTaskOf(t, w.attempts[0])
			if err := task.Bayes.Check(task.Mode, task.Output); err != nil {
				t.Error(err)
			}
			if !strings.Contains(raw, `"series":`+c.series) || task.WriteOutput {
				t.Errorf("task.json: %s", raw)
			}
		})
	}
}

// Every refusal of «Before anything», each with the person who lacks exactly that right (or the node that lacks exactly
// that setting), before any run, input or Job; and the same step passing with it.
func TestPlanBayesRefusesBeforeAnything(t *testing.T) {
	w := newBayesRunWorld(t)
	saved := w.save("Schools", templateGraph(t, "hier_slopes"))
	gone := w.save("Gone", templateGraph(t, "glm_linear"))
	m := saved.Model.ID
	node := func(more map[string]any) string { return bayesNodeJSON(m, 1, slopesBindings, more) }
	sampler := func(k string, v any) map[string]any {
		s := map[string]any{"chains": 4, "warmup": 1000, "draws": 1000, "seed": 1840277361, "adapt_delta": 0.8, "max_treedepth": 10}
		if v == nil {
			delete(s, k)
		} else {
			s[k] = v
		}
		return map[string]any{"sampler": s}
	}
	notAvailable := func(u *auth.User) string {
		return "the Bayesian model of «School scores» is not available to " + u.Email
	}
	if _, err := w.db.Exec(w.ctx, `DELETE FROM bayes_model_versions WHERE model_id = $1`, gone.Model.ID); err != nil {
		t.Fatal(err)
	}
	if _, err := w.db.Exec(w.ctx, `DELETE FROM bayes_models WHERE id = $1`, gone.Model.ID); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name    string
		who     *auth.User
		node    string
		trigger string
		before  func()
		want    string
	}{
		{"the model's folder unseen", w.outsider, node(nil), "manual", nil, notAvailable(w.outsider)},
		{"the model's folder seen, not runnable", w.viewer, node(nil), "manual", nil, notAvailable(w.viewer)},
		{"a schedule's author who lost RUN on the model", w.viewer, node(nil), "schedule", nil, notAvailable(w.viewer)},
		{"the model deleted", w.runner, bayesNodeJSON(gone.Model.ID, 1, map[string]string{"y": "a", "x": "b"}, nil), "manual", nil,
			"the Bayesian model of «School scores» is not available to " + w.runner.Email},
		{"the group's use", w.runner, node(nil), "manual", func() { w.groupFor(false) },
			w.runner.Email + " may not use the model group bayes"},
		{"no model chosen", w.runner, bayesNodeJSON(0, 0, nil, nil), "manual", nil, "Bayesian model «School scores»: choose a saved model"},
		{"the version gone", w.runner, bayesNodeJSON(m, 9, slopesBindings, nil), "manual", nil,
			"Bayesian model «School scores»: the model or its version no longer exists: choose another"},
		{"a variable unbound", w.runner, bayesNodeJSON(m, 1, map[string]string{"y": "score", "g": "school"}, nil), "manual", nil,
			"Bayesian model «School scores»: bind «Predictor» to a column"},
		{"nine chains", w.runner, node(sampler("chains", 9)), "manual", nil, "chains: a whole number from 1 to 8"},
		{"no seed", w.runner, node(sampler("seed", nil)), "manual", nil, "its seed is missing"},
		{"adapt_delta of 1", w.runner, node(sampler("adapt_delta", 1)), "manual", nil, "adapt_delta: a number between 0 and 1"},
		{"an initial value of no parameter", w.runner, node(sampler("init", map[string]any{"nope": 1})), "manual", nil,
			"the initial value of «nope»: not a parameter"},
		{"an output of its own", w.runner, node(map[string]any{"output": "wide"}), "manual", nil, "its output is the summary"},
		{"a thinning beyond the draws", w.runner, node(map[string]any{"output": "replicated", "replicatedDraws": 1001}), "manual", nil,
			"the replicated data keep from 1 to 1,000 draws a chain"},
		{"a timeout beyond the group's", w.runner, node(map[string]any{"timeoutSeconds": 7200}), "manual", nil,
			"its timeout (7200 s) is longer than the group «bayes» allows (3600 s)"},
		{"a compile that passed its limit", w.runner, node(nil), "manual", func() { w.binaries.states = []string{"refused"} },
			"Bayesian model «School scores»: the program did not compile within the limit of 300 s"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			w.groupFor(true)
			w.binaries.states = nil
			if c.before != nil {
				c.before()
			}
			w.mu.Lock()
			w.events = nil
			w.mu.Unlock()
			w.attempts = nil
			_, id, err := w.step(c.who, c.node, false, c.trigger)
			var sf *scriptFailed
			if !errors.As(err, &sf) || !strings.Contains(sf.msg, c.want) {
				t.Fatalf("got %v, want %q", err, c.want)
			}
			if id != 0 || len(w.attempts) != 0 || slices.Contains(w.seen(), "input") {
				t.Errorf("a run %d, %d attempts, the events %v", id, len(w.attempts), w.seen())
			}
		})
	}
	// and with the right: the runner (RUN on both folders) and a schedule's author who has it
	w.groupFor(true)
	w.binaries.states = nil
	for _, trigger := range []string{"manual", "schedule"} {
		if _, id, err := w.step(w.runner, node(nil), false, trigger); err != nil || w.row(id).status != "SUCCESS" {
			t.Errorf("%s: %v", trigger, err)
		}
	}
	// an initial radius and a value per scalar parameter pass
	for _, init := range []any{0.5, map[string]any{"mu_a": 0.1, "sigma": 1}} {
		if _, _, err := w.step(w.runner, node(sampler("init", init)), false, "manual"); err != nil {
			t.Errorf("init %v: %v", init, err)
		}
	}
}

// An altered binary is forgotten and compiled again: Need again, the new binary placed, the Job run.
func TestAnAlteredBinaryIsCompiledAgain(t *testing.T) {
	w := newBayesRunWorld(t)
	saved := w.save("Schools", templateGraph(t, "hier_slopes"))
	w.binaries.states = []string{"ready", "queued", "ready"}
	w.binaries.places = []error{fmt.Errorf("%w: altered", bayescompile.ErrAltered), nil}
	_, id, err := w.step(w.runner, bayesNodeJSON(saved.Model.ID, 1, slopesBindings, nil), false, "manual")
	if err != nil {
		t.Fatal(err)
	}
	if got := strings.Join(w.seen(), " "); got != "need input place need need place job" {
		t.Errorf("the order: %s", got)
	}
	if len(w.binaries.placed) != 2 || w.binaries.placed[0] != w.binaries.placed[1] || w.row(id).status != "SUCCESS" {
		t.Errorf("placed %v, the run %+v", w.binaries.placed, w.row(id))
	}
	// the wait of two questions (4 s) is the Job's loss
	if d := pyjson.Obj(w.attempts[0].manifest.Value("spec")).Value("activeDeadlineSeconds"); d != int64(1796) {
		t.Errorf("the deadline: %v", d)
	}
	// a gone binary likewise
	w.binaries.states = []string{"ready", "ready"}
	w.binaries.places = []error{bayescompile.ErrGone, nil}
	if _, _, err := w.step(w.runner, bayesNodeJSON(saved.Model.ID, 1, slopesBindings, nil), false, "manual"); err != nil {
		t.Error(err)
	}
}

// A compile that does not end within the step's timeout fails the step, without a Job; a refusal during the wait fails it
// at once with the state's sentence.
func TestACompileLongerThanTheTimeoutFailsTheStep(t *testing.T) {
	w := newBayesRunWorld(t)
	saved := w.save("Schools", templateGraph(t, "hier_slopes"))
	w.binaries.states = []string{"queued", "running"}
	_, id, err := w.step(w.runner, bayesNodeJSON(saved.Model.ID, 1, slopesBindings, map[string]any{"timeoutSeconds": 10}), false, "manual")
	var sf *scriptFailed
	if !errors.As(err, &sf) || !strings.Contains(sf.msg, "the model was still compiling when the step's timeout (10 s) ended") {
		t.Fatalf("%v", err)
	}
	r := w.row(id)
	if r.status != "FAILURE" || len(w.attempts) != 0 || len(w.binaries.placed) != 0 {
		t.Errorf("the run %+v, %d attempts", r, len(w.attempts))
	}
	var info map[string]any
	_ = json.Unmarshal([]byte(r.script), &info)
	if c, _ := info["compile"].(map[string]any); c["state"] != "running" || c["waiting"] != true {
		t.Errorf("the run page's compile: %v", info["compile"])
	}
	// the files deleted all the same, the binary's key included
	if calls := strings.Join(w.engine.calls, "\n"); !strings.Contains(calls, "/model\"") {
		t.Errorf("the files deleted: %s", calls)
	}
	w.binaries.states = []string{"queued", "refused"}
	_, _, err = w.step(w.runner, bayesNodeJSON(saved.Model.ID, 1, slopesBindings, nil), false, "manual")
	if !errors.As(err, &sf) || !strings.Contains(sf.msg, "did not compile within the limit of 300 s") {
		t.Errorf("%v", err)
	}
}

// The editor's trial: refused before any run for the person without RUN on the flow's folder and for the one who may not
// run the model; tried, a model run on the trials' namespace that writes its output.
func TestABayesTrial(t *testing.T) {
	w := newBayesRunWorld(t)
	saved := w.save("Schools", templateGraph(t, "hier_slopes"))
	flow, _, _ := w.flowWith(bayesNodeJSON(saved.Model.ID, 1, slopesBindings, nil), false)
	stranger := w.outsider
	if _, err := w.db.Exec(w.ctx, `DELETE FROM permissions WHERE project_id = $1 AND user_id = $2`, w.folder, stranger.ID); err != nil {
		t.Fatal(err)
	}
	if rec := w.trial(stranger, flow.ID, "b"); rec.Code != http.StatusForbidden && rec.Code != http.StatusNotFound {
		t.Errorf("without RUN on the flow's folder: %d %s", rec.Code, rec.Body)
	}
	if rec := w.trial(w.viewer, flow.ID, "b"); rec.Code != http.StatusForbidden ||
		!strings.Contains(rec.Body.String(), "The Bayesian model of «School scores» is not available to "+w.viewer.Email) {
		t.Errorf("without RUN on the model: %d %s", rec.Code, rec.Body)
	}
	var n int64
	_ = w.db.QueryRow(w.ctx, `SELECT count(*) FROM runs WHERE flow_id = $1`, flow.ID).Scan(&n)
	if n != 0 {
		t.Errorf("%d runs left by the refusals", n)
	}
	rec := w.trial(w.runner, flow.ID, "b")
	if rec.Code != http.StatusAccepted {
		t.Fatalf("the trial: %d %s", rec.Code, rec.Body)
	}
	var started struct {
		RunID int64 `json:"run_id"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &started)
	r := w.waitRun(started.RunID)
	if r.status != "SUCCESS" {
		t.Fatalf("the trial's run: %+v", r)
	}
	var kind string
	_ = w.db.QueryRow(w.ctx, `SELECT kind FROM runs WHERE id = $1`, started.RunID).Scan(&kind)
	task, _ := bayesTaskOf(t, w.attempts[len(w.attempts)-1])
	if kind != "model" || task.RowsFrom != "trial" || !task.WriteOutput || w.attempts[len(w.attempts)-1].ns != "script-trials" {
		t.Errorf("the trial: kind %s, task %+v", kind, task)
	}
	if out := w.outputs(w.runner, flow.ID)["b"]; out == nil || out["output"]["stale"] != false {
		t.Errorf("the outputs after the trial: %v", out)
	}
}
