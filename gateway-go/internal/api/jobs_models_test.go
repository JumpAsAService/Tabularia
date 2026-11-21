package api

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowresolver"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptimage"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/secret"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

// A model node as a step of a run (docs/specs/statistical-models.md, «A run», «The trial»):
// the Job's spec, the fingerprint that says «not up to date», and — on a real database, with
// the cluster and the engine played by the test — success, refusal, crash, timeout, a full
// quota, a group off or not allowed, results the engine refuses, the row limit.

func mustDef(t *testing.T, s string) pyjson.Object {
	t.Helper()
	v, err := pyjson.Loads(s)
	if err != nil {
		t.Fatal(err)
	}
	return v.(pyjson.Object)
}

func TestAModelJobIsTheGroupsImageOfflineAndIsolated(t *testing.T) {
	img := &ModelGroupImage{Group: "regression", EnvironmentID: 4, Image: "registry:5000/envs@sha256:0123", Arch: "amd64",
		CPURequest: "1", CPULimit: "2", MemoryRequest: "2Gi", MemoryLimit: "4Gi"}
	labels := pyjson.Object{{Key: labelModelGroup, Value: "regression"}, {Key: labelModel, Value: "linear"}}
	env := pyjson.Object{{Key: modelgroups.EnvInput, Value: "s3://b/in"}, {Key: modelgroups.EnvResults, Value: "s3://b/r"}}
	m := modelJobManifest(jobSettings{jobTTL: 60, registryPullSecret: "pull"}, "tabularia-model-9-a1", img, env, labels, 900,
		[]any{pyjson.Object{{Key: "key", Value: "f0"}, {Key: "path", Value: "runner/run.R"}}}, "code", "env", nil)
	job, podLabels := podLabels(t, m)
	for what, l := range map[string]pyjson.Object{"job": job, "pod": podLabels} {
		if l.Value(labelInternet) != "off" || l.Value(labelModelGroup) != "regression" || l.Value(labelModel) != "linear" {
			t.Errorf("%s labels: %v", what, l)
		}
	}
	spec := pyjson.Obj(m.Value("spec"))
	if spec.Value("activeDeadlineSeconds") != int64(900) || spec.Value("backoffLimit") != int64(0) {
		t.Errorf("spec %v", spec)
	}
	pod := pyjson.Obj(pyjson.Obj(spec.Value("template")).Value("spec"))
	if pod.Value("automountServiceAccountToken") != false || pyjson.Obj(pod.Value("nodeSelector")).Value("kubernetes.io/arch") != "amd64" {
		t.Errorf("pod %v", pod)
	}
	c := objectList(pod.Value("containers"))[0]
	if c.Value("image") != img.Image || c.Value("workingDir") != modelgroups.CodeDir {
		t.Errorf("container %v", c)
	}
	cmd, _ := c.Value("command").([]any)
	if run, _ := modelgroups.Commands("r"); len(cmd) != len(run) || cmd[0] != "Rscript" {
		t.Errorf("command %v", cmd)
	}
	for _, e := range objectList(c.Value("env")) {
		if e.Value("name") == "PYTHONHASHSEED" {
			t.Errorf("an R group's Job has PYTHONHASHSEED")
		}
	}
	sec := pyjson.Obj(c.Value("securityContext"))
	if sec.Value("readOnlyRootFilesystem") != true || sec.Value("allowPrivilegeEscalation") != false {
		t.Errorf("security %v", sec)
	}
	limits := pyjson.Obj(pyjson.Obj(c.Value("resources")).Value("limits"))
	if limits.Value("cpu") != "2" || limits.Value("memory") != "4Gi" {
		t.Errorf("limits %v", limits)
	}
	for _, e := range c.Value("env").([]any) {
		if name := pyjson.Str(pyjson.Obj(e).Value("name")); strings.Contains(strings.ToUpper(name), "MLFLOW") {
			t.Errorf("a model Job holds an MLflow variable: %s", name)
		}
	}
}

func TestTheSamplesAChainRead(t *testing.T) {
	ops := mustDef(t, `{"ops": [{"type": "limit", "params": {"n": 100, "_dev_sample": true}},
		{"type": "join", "params": {"right": {"operations": [{"type": "sample", "params": {"fraction": 0.1, "_dev_sample": true}},
			{"type": "limit", "params": {"n": 100, "_dev_sample": true}}]}}}, {"type": "limit", "params": {"n": 5}}]}`).Value("ops")
	got := pyDumps(samplesOf(ops))
	if got != `[{"first": 100}, {"fraction": 0.1}]` {
		t.Errorf("%s", got)
	}
	if pyDumps(samplesOf([]any{})) != "[]" {
		t.Error("a chain without samples")
	}
}

func TestAModelsFingerprintFollowsItsChain(t *testing.T) {
	base := `{"nodes": [{"id": "s", "type": "source", "position": {"x": 1}, "data": {"datasourceId": 1, "label": "Sales"}},
		{"id": "f", "type": "operation", "data": {"opType": "filter", "params": {"column": "x"}}},
		{"id": "m", "type": "model", "data": {"model": "linear", "label": "M", "timeoutSeconds": 900, "params": {"outcome": "y", "predictors": ["x"]}}},
		{"id": "o", "type": "output", "data": {"name": "out"}},
		{"id": "z", "type": "source", "data": {"datasourceId": 9}}],
		"edges": [{"source": "s", "target": "f"}, {"source": "f", "target": "m"}, {"source": "m", "target": "o"}]}`
	fp := func(text string) string {
		d := mustDef(t, text)
		for _, n := range objectList(d.Value("nodes")) {
			if n.Value("id") == "m" {
				return modelFingerprint(d, n)
			}
		}
		t.Fatal("no model node")
		return ""
	}
	ref := fp(base)
	same := map[string]string{
		"a name":                 strings.Replace(base, `"label": "M"`, `"label": "Prices"`, 1),
		"a source's name":        strings.Replace(base, `"label": "Sales"`, `"label": "Orders"`, 1),
		"a position":             strings.Replace(base, `{"x": 1}`, `{"x": 300}`, 1),
		"the timeout":            strings.Replace(base, `"timeoutSeconds": 900`, `"timeoutSeconds": 60`, 1),
		"a node after it":        strings.Replace(base, `"name": "out"`, `"name": "elsewhere"`, 1),
		"a node not in its path": strings.Replace(base, `"datasourceId": 9`, `"datasourceId": 10`, 1),
	}
	for what, text := range same {
		if fp(text) != ref {
			t.Errorf("%s changed the fingerprint", what)
		}
	}
	changed := map[string]string{
		"a parameter":   strings.Replace(base, `"predictors": ["x"]`, `"predictors": ["x", "z"]`, 1),
		"the filter":    strings.Replace(base, `"column": "x"`, `"column": "w"`, 1),
		"the source":    strings.Replace(base, `"datasourceId": 1,`, `"datasourceId": 2,`, 1),
		"an edge":       strings.Replace(base, `{"source": "s", "target": "f"}, `, `{"source": "z", "target": "f"}, `, 1),
		"another model": strings.Replace(base, `"model": "linear"`, `"model": "logistic"`, 1),
	}
	for what, text := range changed {
		if fp(text) == ref {
			t.Errorf("%s did not change the fingerprint", what)
		}
	}
}

func TestTheModelsSmallHelpers(t *testing.T) {
	for in, want := range map[string]int64{"2": 2, "500m": 1, "2500m": 3, "": 1, "1.5": 2} {
		if got := threadsOf(in); got != want {
			t.Errorf("threadsOf(%q) = %d, want %d", in, got, want)
		}
	}
	if thousands(3400000) != "3,400,000" || thousands(51) != "51" || thousands(1000) != "1,000" {
		t.Error("thousands")
	}
	if upperFirst("the model") != "The model" || upperFirst("") != "" || upperFirst("«x»") != "«x»" {
		t.Error("upperFirst")
	}
	t.Setenv("MODELS__ENABLED", "false")
	if modelsOff(&scriptcfg.Settings{}) == "" {
		t.Error("MODELS__ENABLED=false: off")
	}
}

// ── on a real database, the cluster and the engine played by the test ─────────

const modelsTestFernetKey = "Vd3m7oIM2w3o2r6Y-ZbM6O1t3Qk3c7HnS0WBu6Mxx2E="

type modelRunFixture struct {
	t        *testing.T
	ctx      context.Context
	db       *pgxpool.Pool
	d        *Deps
	q        *queue
	admin    *auth.User
	ann      *auth.User // may edit the folder, in no user group
	folder   int64
	env      int64
	version  int64
	engine   *fakeModelEngine
	attempts []modelAttemptSeen
	script   []modelAttemptResult // what each attempt of the Job answers, in turn
	rows     int64                // the input's rows the engine says it wrote
	columns  []string             // the columns the input was asked for
	mu       sync.Mutex
}

type modelAttemptSeen struct {
	name     string
	ns       string
	manifest pyjson.Object
	code     pyjson.Object
	secret   pyjson.Object
}

type modelAttemptResult struct {
	ok     bool
	log    string
	exit   any
	reason string
}

type fakeModelEngine struct {
	mu            sync.Mutex
	calls         []string
	resultsStatus int
	resultsBody   string
	outputBody    string
}

func (e *fakeModelEngine) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	b, _ := io.ReadAll(r.Body)
	e.mu.Lock()
	e.calls = append(e.calls, r.URL.Path+" "+string(b))
	status, body := http.StatusOK, `{}`
	switch r.URL.Path {
	case "/scripts/results/import":
		status, body = e.resultsStatus, e.resultsBody
	case "/scripts/output/import":
		body = e.outputBody
	case "/scripts/objects/delete":
		body = `{"deleted": 9, "failed": []}`
	}
	e.mu.Unlock()
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(body))
}

func (e *fakeModelEngine) paths() []string {
	e.mu.Lock()
	defer e.mu.Unlock()
	var out []string
	for _, c := range e.calls {
		p, _, _ := strings.Cut(c, " ")
		out = append(out, p)
	}
	return out
}

func newModelRunFixture(t *testing.T) *modelRunFixture {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set: run gateway-go/scripts/test.sh")
	}
	t.Cleanup(scriptcfg.Reload)
	t.Setenv("SCRIPTS__ENABLED", "true")
	t.Setenv("SCRIPTS__NAMESPACE", "scripts")
	t.Setenv("SCRIPTS__TRIAL_NAMESPACE", "script-trials")
	t.Setenv("SCRIPTS__REGISTRY", "registry:5000/envs")
	t.Setenv("MODELS__ENABLED", "")
	scriptcfg.Reload()
	ctx := context.Background()
	db, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	lock, err := db.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := lock.Exec(ctx, `SELECT pg_advisory_lock(7101)`); err != nil {
		t.Fatal(err)
	}
	f := &modelRunFixture{t: t, ctx: ctx, db: db, rows: 120, engine: &fakeModelEngine{resultsStatus: http.StatusOK,
		resultsBody: `{"found": true, "rows": 42, "card": "{\"format_version\": 1}"}`, outputBody: `{"found": true, "rows": 120}`}}
	srv := httptest.NewServer(f.engine)
	e, err := engine.New(srv.URL, modelsTestFernetKey)
	if err != nil {
		t.Fatal(err)
	}
	fernet, err := secret.NewFernet(modelsTestFernetKey)
	if err != nil {
		t.Fatal(err)
	}
	cfg := &config.Config{EngineBucket: "data-prep", ScriptsEnabled: true, ScriptsNamespace: "scripts"}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	f.d = &Deps{DB: db, Config: cfg, Log: log, Engine: e, Panel: settings.New(cfg, false, log), Fernet: fernet}
	f.q = &queue{d: f.d, sc: scriptcfg.Load(config.FromOS()), alive: map[int64]bool{}, wake: make(chan struct{}, 1)}
	one := func(sql string, args ...any) int64 {
		t.Helper()
		var id int64
		if err := db.QueryRow(ctx, sql, args...).Scan(&id); err != nil {
			t.Fatalf("%s: %v", sql, err)
		}
		return id
	}
	stamp := time.Now().UnixNano()
	user := func(name string, super bool) *auth.User {
		email := fmt.Sprintf("model-%s-%d@x.test", name, stamp)
		id := one(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, $2, 'x', true, $3, false, false, now()) RETURNING id`, email, name, super)
		return &auth.User{ID: id, Email: email, FullName: name, IsActive: true, IsSuperuser: super}
	}
	f.admin, f.ann = user("admin", true), user("ann", false)
	f.folder = one(`INSERT INTO projects (name, description, created_at) VALUES ($1, '', now()) RETURNING id`, fmt.Sprintf("models-%d", stamp))
	one(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'manage') RETURNING id`, f.folder, f.ann.ID)
	enc, err := fernet.Encrypt("scripts-s3cret")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(ctx, `INSERT INTO script_storage (id, endpoint, region, bucket, access_key, secret_key_enc, updated_at, updated_by)
		VALUES (1, '', '', 'scripts', 'scripts-key', $1, now(), 'test') ON CONFLICT (id) DO UPDATE SET bucket = 'scripts',
		access_key = 'scripts-key', secret_key_enc = $1`, enc); err != nil {
		t.Fatal(err)
	}
	shipped, _ := modelgroups.Get("regression")
	_, _ = db.Exec(ctx, `DELETE FROM model_groups WHERE id = 'regression'`)
	_, _ = db.Exec(ctx, `DELETE FROM script_environments WHERE managed_by = 'model_group:regression'`)
	f.env = one(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
		memory_limit, max_timeout_seconds, created_at, updated_at, managed_by) VALUES ($1, '', 'r', true, '1', '2', '2Gi', '4Gi', 3600,
		now(), now(), 'model_group:regression') RETURNING id`, fmt.Sprintf("Regression %d", stamp))
	f.version = one(`INSERT INTO script_environment_versions (environment_id, number, language_version, base_image, packages, status,
		image_ref, arch, recipe_hash, created_at) VALUES ($1, 3, '4.6', 'rocker/r-ver:4.6.0', '', 'ready',
		'registry:5000/envs@sha256:0123456789abcdef', 'amd64', $2, now()) RETURNING id`, f.env, shipped.RecipeHash)
	if _, err := db.Exec(ctx, `UPDATE script_environments SET current_version_id = $1 WHERE id = $2`, f.version, f.env); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Exec(ctx, `INSERT INTO model_groups (id, environment_id, enabled, everyone, updated_at) VALUES ('regression', $1, true, true, now())`,
		f.env); err != nil {
		t.Fatal(err)
	}
	// the cluster and the input, played
	prevAttempt, prevInput := modelAttemptJob, modelPrepareInput
	modelAttemptJob = func(_ context.Context, _ scriptcfg.Kubernetes, name string, manifest pyjson.Object, code, secret pyjson.Object,
		_ int64, ns string, _ float64, _ int) (bool, string, any, string, error) {
		f.mu.Lock()
		defer f.mu.Unlock()
		f.attempts = append(f.attempts, modelAttemptSeen{name, ns, manifest, code, secret})
		if len(f.script) == 0 {
			return true, "fitted\n", int64(0), "", nil
		}
		r := f.script[0]
		if len(f.script) > 1 {
			f.script = f.script[1:]
		}
		return r.ok, r.log, r.exit, r.reason, nil
	}
	modelPrepareInput = func(_ *queue, ctx context.Context, _ *scriptNodeRun, child *childRun, _ *scriptStorage, _ string,
		columns []string, _ string) (*modelInput, error) {
		f.columns = columns
		rows := f.rows
		return &modelInput{rows: &rows, sample: []any{pyjson.Object{{Key: "first", Value: int64(100)}}}}, nil
	}
	t.Cleanup(func() {
		modelAttemptJob, modelPrepareInput = prevAttempt, prevInput
		srv.Close()
		ids := []int64{f.admin.ID, f.ann.ID}
		_, _ = db.Exec(ctx, `DELETE FROM runs WHERE launched_by = ANY($1)`, ids)
		_, _ = db.Exec(ctx, `DELETE FROM flows WHERE project_id = $1`, f.folder)
		_, _ = db.Exec(ctx, `DELETE FROM permissions WHERE project_id = $1`, f.folder)
		_, _ = db.Exec(ctx, `DELETE FROM projects WHERE id = $1`, f.folder)
		_, _ = db.Exec(ctx, `DELETE FROM model_groups WHERE id = 'regression'`)
		_, _ = db.Exec(ctx, `UPDATE script_environments SET current_version_id = NULL WHERE id = $1`, f.env)
		_, _ = db.Exec(ctx, `DELETE FROM script_environment_groups WHERE environment_id = $1`, f.env)
		_, _ = db.Exec(ctx, `DELETE FROM script_environment_versions WHERE environment_id = $1`, f.env)
		_, _ = db.Exec(ctx, `DELETE FROM script_environments WHERE id = $1`, f.env)
		_, _ = db.Exec(ctx, `DELETE FROM script_storage WHERE id = 1`)
		_, _ = db.Exec(ctx, `DELETE FROM audit_logs WHERE actor_id = ANY($1)`, ids)
		_, _ = db.Exec(ctx, `DELETE FROM users WHERE id = ANY($1)`, ids)
		_, _ = lock.Exec(ctx, `SELECT pg_advisory_unlock(7101)`)
		lock.Release()
		db.Close()
	})
	return f
}

// flow saves a flow source → model (→ output when next) and gives its run of the model node.
func (f *modelRunFixture) flow(params string, next bool) (*jobFlow, pyjson.Object, pyjson.Object) {
	f.t.Helper()
	nodes := `{"id": "s", "type": "source", "data": {"datasourceId": 1}},
		{"id": "m", "type": "model", "data": {"model": "linear", "label": "Elasticity", "params": ` + params + `, "timeoutSeconds": 600, "retries": 2, "retryDelaySeconds": 0}}`
	edges := `{"source": "s", "target": "m"}`
	if next {
		nodes += `, {"id": "o", "type": "output", "data": {"name": "coefficients"}}`
		edges += `, {"source": "m", "target": "o"}`
	}
	text := `{"nodes": [` + nodes + `], "edges": [` + edges + `]}`
	var id int64
	if err := f.db.QueryRow(f.ctx, `INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
		VALUES ('Pricing', '', $1, $2, $3, 'duckdb', now(), now()) RETURNING id`, f.folder, f.ann.ID, text).Scan(&id); err != nil {
		f.t.Fatal(err)
	}
	definition := mustDef(f.t, text)
	var node pyjson.Object
	for _, n := range objectList(definition.Value("nodes")) {
		if n.Value("id") == "m" {
			node = n
		}
	}
	return &jobFlow{ID: id, ProjectID: f.folder, Name: "Pricing", Definition: text, Engine: "duckdb"}, definition, node
}

func (f *modelRunFixture) run(user *auth.User, params string, next bool) (int64, error) {
	f.t.Helper()
	flow, definition, node := f.flow(params, next)
	r := &scriptNodeRun{q: f.q, user: user, flow: flow, definition: definition, node: node, trigger: "manual", mode: "development",
		outputs: flowresolver.ScriptOutputs{}}
	_, err := f.q.runModel(f.ctx, r)
	var id int64
	_ = f.db.QueryRow(f.ctx, `SELECT coalesce(max(id), 0) FROM runs WHERE flow_id = $1 AND kind = 'model'`, flow.ID).Scan(&id)
	return id, err
}

type modelRunRow struct {
	status, script, outputKey, resultsKey, card, errText string
	rows                                                 *int64
}

func (f *modelRunFixture) row(id int64) modelRunRow {
	f.t.Helper()
	var r modelRunRow
	var script, results, card, errText *string
	if err := f.db.QueryRow(f.ctx, `SELECT status, script, output_key, model_results_key, model_card, error, rows_written FROM runs WHERE id = $1`, id).
		Scan(&r.status, &script, &r.outputKey, &results, &card, &errText, &r.rows); err != nil {
		f.t.Fatal(err)
	}
	r.script, r.resultsKey, r.card, r.errText = deref(script), deref(results), deref(card), deref(errText)
	return r
}

const linearParams = `{"outcome": "quantity", "predictors": ["price", "channel"]}`

func TestAModelStepRunsItsGroupsJobAndKeepsItsResults(t *testing.T) {
	f := newModelRunFixture(t)
	id, err := f.run(f.ann, linearParams, false)
	if err != nil {
		t.Fatal(err)
	}
	r := f.row(id)
	if r.status != "SUCCESS" || r.resultsKey == "" || r.outputKey != r.resultsKey || r.card != `{"format_version": 1}` {
		t.Fatalf("the run: %+v", r)
	}
	if r.rows == nil || *r.rows != 42 {
		t.Errorf("the rows the next nodes read: the results' 42, got %v", r.rows)
	}
	// what the report and the outputs read of the run (lot E's contract)
	if !strings.HasPrefix(r.script, `{"node_id": "m",`) {
		t.Errorf("the script starts with the node: %s", r.script)
	}
	var info map[string]any
	if err := json.Unmarshal([]byte(r.script), &info); err != nil {
		t.Fatal(err)
	}
	if info["model"] != "linear" || info["trial"] != false || info["fingerprint"] == "" || info["group"] != "regression" ||
		info["version"] != float64(3) || info["namespace"] != "scripts" || fmt.Sprint(info["sample"]) != "[map[first:100]]" {
		t.Errorf("the run's script: %s", r.script)
	}
	if p, _ := info["params"].(map[string]any); p["outcome"] != "quantity" || p["output"] != "results" {
		t.Errorf("the checked parameters, with their defaults: %v", info["params"])
	}
	// the input asked for the columns the model reads only
	if strings.Join(f.columns, ",") != "quantity,price,channel" {
		t.Errorf("the input's columns: %v", f.columns)
	}
	// one Job: the group's image by digest, in the scripts namespace, offline, the runner and task.json in its ConfigMap
	if len(f.attempts) != 1 {
		t.Fatalf("%d attempts", len(f.attempts))
	}
	a := f.attempts[0]
	if a.ns != "scripts" || a.secret.Value("AWS_SECRET_ACCESS_KEY") != "scripts-s3cret" {
		t.Errorf("namespace %s, secret %v", a.ns, a.secret.Value("AWS_ACCESS_KEY_ID"))
	}
	job, _ := podLabels(t, a.manifest)
	if job.Value(labelInternet) != "off" || job.Value(labelModelGroup) != "regression" || job.Value(labelScriptRun) != fmt.Sprint(id) {
		t.Errorf("labels %v", job)
	}
	var task modelgroups.Task
	found := false
	for _, kv := range a.code {
		if s, _ := kv.Value.(string); strings.HasPrefix(s, `{"protocol":`) {
			found = json.Unmarshal([]byte(s), &task) == nil
		}
	}
	if !found || task.Mode != "fit" || task.GroupVersion != 3 || task.IDs.StepRunID != id || task.InputRows != 120 || task.RowsFrom != "run" ||
		task.WriteOutput || task.MLflow {
		t.Errorf("task.json: %+v", task)
	}
	paths := strings.Join(f.engine.paths(), " ")
	if paths != "/scripts/results/import /scripts/objects/delete" {
		t.Errorf("the engine was asked: %s", paths)
	}
}

func TestAModelsRowsGoToTheNodesAfterIt(t *testing.T) {
	f := newModelRunFixture(t)
	id, err := f.run(f.ann, `{"outcome": "quantity", "predictors": ["price"], "output": "rows"}`, true)
	if err != nil {
		t.Fatal(err)
	}
	r := f.row(id)
	if r.status != "SUCCESS" || r.outputKey == r.resultsKey || r.outputKey == "" || r.rows == nil || *r.rows != 120 {
		t.Errorf("the rows: %+v", r)
	}
	if f.columns != nil {
		t.Errorf("the rows output gives every input column back: %v", f.columns)
	}
	if paths := strings.Join(f.engine.paths(), " "); paths != "/scripts/results/import /scripts/output/import /scripts/objects/delete" {
		t.Errorf("the engine was asked: %s", paths)
	}
}

func TestARefusalOfTheRunnerIsItsSentenceAndIsNotTriedAgain(t *testing.T) {
	f := newModelRunFixture(t)
	f.script = []modelAttemptResult{{log: "reading\nTABULARIA_ERROR {\"code\":\"separation\",\"message\":\"the outcome is separated by price: use Firth\",\"params\":{}}\n",
		exit: int64(modelgroups.ExitRefused), reason: "it ended with exit code 3"}}
	id, err := f.run(f.ann, linearParams, false)
	if err == nil || err.Error() != "model «Elasticity»: the outcome is separated by price: use Firth" {
		t.Fatalf("%v", err)
	}
	r := f.row(id)
	if r.status != "FAILURE" || r.errText != err.Error() || len(f.attempts) != 1 || r.resultsKey != "" {
		t.Errorf("%+v, %d attempts", r, len(f.attempts))
	}
}

func TestACrashIsTriedAgainThenFails(t *testing.T) {
	f := newModelRunFixture(t)
	f.script = []modelAttemptResult{{log: "Error in fit: object 'x' not found\n", exit: int64(1), reason: "it ended with exit code 1"}}
	id, err := f.run(f.ann, linearParams, false)
	if err == nil || err.Error() != "model «Elasticity» failed: it ended with exit code 1" {
		t.Fatalf("%v", err)
	}
	r := f.row(id)
	if r.status != "FAILURE" || len(f.attempts) != 3 || !strings.Contains(r.script, "attempt 3") {
		t.Errorf("%+v, %d attempts", r, len(f.attempts))
	}
	if strings.Contains(strings.Join(f.engine.paths(), " "), "/scripts/results/import") {
		t.Error("a crash imports nothing")
	}
}

// The group is disabled, or taken from the person, while an attempt runs: no attempt after it, and
// the step says why, as when its run is closed.
func TestNoAttemptAfterTheGroupIsDisabledOrTakenAway(t *testing.T) {
	for _, c := range []struct {
		name, sql, why string
	}{
		{"disabled", `UPDATE model_groups SET enabled = false WHERE id = 'regression'`, "the model group regression is disabled"},
		{"taken away", `UPDATE model_groups SET everyone = false WHERE id = 'regression'`, "may not use the model group regression"},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := newModelRunFixture(t)
			f.script = []modelAttemptResult{{log: "Killed\n", exit: int64(137), reason: "it ended with exit code 137"}}
			inner := modelAttemptJob
			t.Cleanup(func() { modelAttemptJob = inner })
			modelAttemptJob = func(ctx context.Context, k scriptcfg.Kubernetes, name string, manifest pyjson.Object, code, secret pyjson.Object,
				timeout int64, ns string, poll float64, logMax int) (bool, string, any, string, error) {
				if _, err := f.db.Exec(ctx, c.sql); err != nil {
					t.Fatal(err)
				}
				return inner(ctx, k, name, manifest, code, secret, timeout, ns, poll, logMax)
			}
			id, err := f.run(f.ann, linearParams, false)
			want := "model «Elasticity» failed: it ended with exit code 137 (not run again: "
			if err == nil || !strings.HasPrefix(err.Error(), want) || !strings.Contains(err.Error(), c.why) {
				t.Fatalf("the step: %v", err)
			}
			if r := f.row(id); r.status != "FAILURE" || len(f.attempts) != 1 {
				t.Errorf("%s, %d Jobs", r.status, len(f.attempts))
			}
		})
	}
}

// Between two attempts, what the first wrote goes: its output, its results and its artifacts (an
// attempt that ends well without writing one of them must not have an earlier one's imported),
// never its input.
func TestAnAttemptsFilesGoBeforeTheNext(t *testing.T) {
	f := newModelRunFixture(t)
	f.script = []modelAttemptResult{{log: "Killed\n", exit: int64(137), reason: "it ended with exit code 137"}, {ok: true, exit: int64(0)}}
	if _, err := f.run(f.ann, linearParams, true); err != nil {
		t.Fatal(err)
	}
	if len(f.attempts) != 2 {
		t.Fatalf("%d attempts", len(f.attempts))
	}
	var calls []string
	for _, c := range f.engine.calls {
		if strings.HasPrefix(c, "/scripts/") {
			calls = append(calls, c)
		}
	}
	// the first delete is between the attempts, before any import
	if len(calls) == 0 || !strings.HasPrefix(calls[0], "/scripts/objects/delete ") {
		t.Fatalf("the engine was asked: %v", calls)
	}
	var body struct {
		Keys []string `json:"keys"`
	}
	if err := json.Unmarshal([]byte(strings.TrimPrefix(calls[0], "/scripts/objects/delete ")), &body); err != nil {
		t.Fatal(err)
	}
	keys := strings.Join(body.Keys, " ")
	for _, want := range []string{"/output.parquet", "/results.parquet", "/artifacts/model.json", "/artifacts/run.json", "/artifacts/error.json",
		"/artifacts/native.rds", "/artifacts/MLmodel", "/artifacts/xgboost.json"} {
		if !strings.Contains(keys, want) {
			t.Errorf("%s is left for the next attempt: %v", want, body.Keys)
		}
	}
	if strings.Contains(keys, "/input.parquet") {
		t.Errorf("the input is deleted before the next attempt: %v", body.Keys)
	}
}

func TestATimeoutAndAFullQuotaSayWhy(t *testing.T) {
	f := newModelRunFixture(t)
	f.script = []modelAttemptResult{{log: "", exit: nil, reason: "it ran longer than its timeout (600 s)"}}
	if _, err := f.run(f.ann, linearParams, false); err == nil || !strings.HasSuffix(err.Error(), "failed: it ran longer than its timeout (600 s)") {
		t.Errorf("timeout: %v", err)
	}
	// a trial: the trials' namespace, whose quota is full
	f.attempts = nil
	quota := "its Pod never started within its timeout (600 s): exceeded quota: script-trials-quota, requested: limits.memory=4Gi"
	f.script = []modelAttemptResult{{reason: quota}}
	flow, definition, node := f.flow(linearParams, false)
	var trial int64
	if err := f.db.QueryRow(f.ctx, `INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, input_key, output_bucket,
		output_key, publish_description, publish_overwrite, publish_sort_keys, script, started_at) VALUES ('model', $1, '', 'PENDING', $2, 'trial',
		'', '', '', '', false, '[]', '{"node_id": "m", "trial": true}', now()) RETURNING id`, flow.ID, f.ann.ID).Scan(&trial); err != nil {
		t.Fatal(err)
	}
	r := &scriptNodeRun{q: f.q, user: f.ann, flow: flow, definition: definition, node: node, trigger: "trial", mode: "development",
		outputs: flowresolver.ScriptOutputs{}, trial: trial}
	_, err := f.q.runModel(f.ctx, r)
	if err == nil || !strings.Contains(err.Error(), "exceeded quota") {
		t.Errorf("quota: %v", err)
	}
	if len(f.attempts) == 0 || f.attempts[0].ns != "script-trials" {
		t.Errorf("a trial runs in the trials' namespace: %+v", f.attempts)
	}
	if row := f.row(trial); row.status != "FAILURE" || !strings.Contains(row.script, `"trial": true`) {
		t.Errorf("the trial's row: %+v", row)
	}
}

func TestAGroupOffOrNotAllowedCreatesNothing(t *testing.T) {
	f := newModelRunFixture(t)
	count := func() int64 {
		var n int64
		_ = f.db.QueryRow(f.ctx, `SELECT count(*) FROM runs WHERE launched_by = $1`, f.ann.ID).Scan(&n)
		return n
	}
	// not for everyone, and ann in none of its user groups; an administrator may
	if _, err := f.db.Exec(f.ctx, `UPDATE model_groups SET everyone = false WHERE id = 'regression'`); err != nil {
		t.Fatal(err)
	}
	if _, err := f.run(f.ann, linearParams, false); err == nil || !strings.Contains(err.Error(), "may not use the model group regression") {
		t.Errorf("not allowed: %v", err)
	}
	if _, err := f.run(f.admin, linearParams, false); err != nil {
		t.Errorf("an administrator: %v", err)
	}
	// disabled
	if _, err := f.db.Exec(f.ctx, `UPDATE model_groups SET enabled = false, everyone = true WHERE id = 'regression'`); err != nil {
		t.Fatal(err)
	}
	if _, err := f.run(f.ann, linearParams, false); err == nil || !strings.Contains(err.Error(), "the model group regression is disabled") {
		t.Errorf("disabled: %v", err)
	}
	// enabled, but its version built from another recipe: being rebuilt
	if _, err := f.db.Exec(f.ctx, `UPDATE model_groups SET enabled = true WHERE id = 'regression'`); err != nil {
		t.Fatal(err)
	}
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET recipe_hash = 'older' WHERE id = $1`, f.version); err != nil {
		t.Fatal(err)
	}
	if _, err := f.run(f.ann, linearParams, false); err == nil || !strings.Contains(err.Error(), "is being rebuilt for this version of Tabularia") {
		t.Errorf("rebuilding: %v", err)
	}
	// parameters the catalogue refuses
	if _, err := f.run(f.ann, `{"outcome": "quantity"}`, false); err == nil || err.Error() != "model «Elasticity»: linear: 'predictors' is required" {
		t.Errorf("parameters: %v", err)
	}
	if n := count(); n != 0 {
		t.Errorf("%d runs created by refusals", n)
	}
	if len(f.attempts) != 1 {
		t.Errorf("only the administrator's run reached the cluster: %d", len(f.attempts))
	}
}

func TestResultsTheEngineRefusesFailTheStep(t *testing.T) {
	f := newModelRunFixture(t)
	f.engine.resultsStatus = http.StatusUnprocessableEntity
	f.engine.resultsBody = `{"detail": "results.parquet: the row 'model|||n' is missing"}`
	id, err := f.run(f.ann, linearParams, false)
	if err == nil || !strings.Contains(err.Error(), "its results were not accepted, a fault of the runner and not of the data: results.parquet: the row 'model|||n' is missing") {
		t.Fatalf("%v", err)
	}
	if r := f.row(id); r.status != "FAILURE" || r.resultsKey != "" {
		t.Errorf("%+v", r)
	}
	if paths := f.engine.paths(); paths[len(paths)-1] != "/scripts/objects/delete" {
		t.Errorf("the step's files are deleted anyway: %v", paths)
	}
}

func TestTheRowLimitStopsTheStepBeforeAnyJob(t *testing.T) {
	f := newModelRunFixture(t)
	if _, err := f.db.Exec(f.ctx, `UPDATE model_groups SET max_rows = 50 WHERE id = 'regression'`); err != nil {
		t.Fatal(err)
	}
	f.rows = 51
	id, err := f.run(f.ann, linearParams, false)
	want := "model «Elasticity»: the model would read 51 rows; the group «regression» allows 50: filter or sample before the node, or ask an administrator"
	if err == nil || err.Error() != want {
		t.Fatalf("%v", err)
	}
	if len(f.attempts) != 0 {
		t.Error("a Job was created")
	}
	if r := f.row(id); r.status != "FAILURE" || r.errText != want {
		t.Errorf("%+v", r)
	}
}

// ── the editor's «Run» of a model node and its outputs ────────────────────────

func (f *modelRunFixture) trial(u *auth.User, flow int64, node string) *httptest.ResponseRecorder {
	f.t.Helper()
	w := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodPost, "/x", nil)
	r.SetPathValue("flow_id", fmt.Sprint(flow))
	r.SetPathValue("node_id", node)
	f.d.trialNode(w, r, &auth.Principal{User: *u})
	return w
}

func (f *modelRunFixture) outputs(u *auth.User, flow int64) map[string]map[string]map[string]any {
	f.t.Helper()
	w := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodGet, "/x", nil)
	r.SetPathValue("flow_id", fmt.Sprint(flow))
	nodeOutputs(f.d)(w, r, &auth.Principal{User: *u})
	if w.Code != http.StatusOK {
		f.t.Fatalf("outputs: %d %s", w.Code, w.Body)
	}
	out := map[string]map[string]map[string]any{}
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		f.t.Fatalf("%v: %s", err, w.Body)
	}
	return out
}

// waitRun is the trial followed to its end, as the editor follows it.
func (f *modelRunFixture) waitRun(id int64) modelRunRow {
	f.t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		r := f.row(id)
		if r.status == "SUCCESS" || r.status == "FAILURE" || time.Now().After(deadline) {
			return r
		}
		time.Sleep(20 * time.Millisecond)
	}
}

func TestTheEditorsRunOfAModelNodeAndItsOutputs(t *testing.T) {
	f := newModelRunFixture(t)
	flow, _, _ := f.flow(linearParams, true)
	// refused before any run: a group the person may not use, parameters the catalogue refuses
	if _, err := f.db.Exec(f.ctx, `UPDATE model_groups SET everyone = false WHERE id = 'regression'`); err != nil {
		t.Fatal(err)
	}
	if w := f.trial(f.ann, flow.ID, "m"); w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), "may not use the model group") {
		t.Errorf("not allowed: %d %s", w.Code, w.Body)
	}
	if _, err := f.db.Exec(f.ctx, `UPDATE model_groups SET everyone = true WHERE id = 'regression'`); err != nil {
		t.Fatal(err)
	}
	bad, _, _ := f.flow(`{"outcome": "quantity"}`, false)
	if w := f.trial(f.ann, bad.ID, "m"); w.Code != http.StatusUnprocessableEntity || !strings.Contains(w.Body.String(), "'predictors' is required") {
		t.Errorf("parameters: %d %s", w.Code, w.Body)
	}
	if w := f.trial(f.ann, flow.ID, "nope"); w.Code != http.StatusNotFound {
		t.Errorf("no such node: %d", w.Code)
	}
	var n int64
	_ = f.db.QueryRow(f.ctx, `SELECT count(*) FROM runs WHERE launched_by = $1`, f.ann.ID).Scan(&n)
	if n != 0 {
		t.Fatalf("%d runs made by refusals", n)
	}
	// the trial: a run of its own, in the trials' namespace, followed to its end
	w := f.trial(f.ann, flow.ID, "m")
	if w.Code != http.StatusAccepted {
		t.Fatalf("%d %s", w.Code, w.Body)
	}
	var started struct {
		RunID int64 `json:"run_id"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &started); err != nil || started.RunID == 0 {
		t.Fatalf("%v %s", err, w.Body)
	}
	r := f.waitRun(started.RunID)
	if r.status != "SUCCESS" || r.resultsKey == "" || !strings.Contains(r.script, `"trial": true`) {
		t.Fatalf("the trial: %+v", r)
	}
	f.mu.Lock()
	ns := f.attempts[len(f.attempts)-1].ns
	f.mu.Unlock()
	if ns != "script-trials" {
		t.Errorf("a trial runs in the trials' namespace, not %s", ns)
	}
	var kind, trigger string
	_ = f.db.QueryRow(f.ctx, `SELECT kind, trigger_type FROM runs WHERE id = $1`, started.RunID).Scan(&kind, &trigger)
	if kind != "model" || trigger != "trial" {
		t.Errorf("kind %s, trigger %s", kind, trigger)
	}
	// its outputs: the latest run, up to date, with the data the nodes after it preview
	out := f.outputs(f.ann, flow.ID)["m"]
	if out == nil || out["last"]["run_id"] != float64(started.RunID) || out["last"]["stale"] != false || out["last"]["trial"] != true ||
		out["output"]["key"] != r.outputKey || out["output"]["stale"] != false {
		t.Fatalf("outputs: %v", out)
	}
	// a parameter changed in the saved flow: not up to date any more
	changed := strings.Replace(flow.Definition, `"predictors": ["price", "channel"]`, `"predictors": ["price"]`, 1)
	if _, err := f.db.Exec(f.ctx, `UPDATE flows SET definition = $2 WHERE id = $1`, flow.ID, changed); err != nil {
		t.Fatal(err)
	}
	if out := f.outputs(f.ann, flow.ID)["m"]; out["last"]["stale"] != true || out["output"]["stale"] != true {
		t.Errorf("after a change: %v", out)
	}
	// and so does a change before it, in its chain
	before := strings.Replace(flow.Definition, `"datasourceId": 1`, `"datasourceId": 2`, 1)
	if _, err := f.db.Exec(f.ctx, `UPDATE flows SET definition = $2 WHERE id = $1`, flow.ID, before); err != nil {
		t.Fatal(err)
	}
	if out := f.outputs(f.ann, flow.ID)["m"]; out["output"]["stale"] != true {
		t.Errorf("after a change of its chain: %v", out)
	}
}

// A Python group's Job: python -I -B on its run.py, and PYTHONHASHSEED set.
func TestAPythonGroupsJob(t *testing.T) {
	img := &ModelGroupImage{Group: "boosting", EnvironmentID: 5, Image: "registry:5000/envs@sha256:4567", CPURequest: "1", CPULimit: "2",
		MemoryRequest: "2Gi", MemoryLimit: "4Gi"}
	labels := pyjson.Object{{Key: labelModelGroup, Value: "boosting"}, {Key: labelModel, Value: "xgboost"}}
	m := modelJobManifest(jobSettings{jobTTL: 60}, "tabularia-model-9-a1", img, pyjson.Object{{Key: modelgroups.EnvInput, Value: "s3://b/in"}},
		labels, 900, []any{pyjson.Object{{Key: "key", Value: "f0"}, {Key: "path", Value: "runner/run.py"}}}, "code", "env", nil)
	pod := pyjson.Obj(pyjson.Obj(pyjson.Obj(m.Value("spec")).Value("template")).Value("spec"))
	c := objectList(pod.Value("containers"))[0]
	cmd, _ := c.Value("command").([]any)
	if fmt.Sprint(cmd) != "[python -I -B /tabularia/code/runner/run.py]" {
		t.Errorf("command %v", cmd)
	}
	seed := ""
	for _, e := range objectList(c.Value("env")) {
		if e.Value("name") == "PYTHONHASHSEED" {
			seed = pyjson.Str(e.Value("value"))
		}
	}
	if seed != "0" {
		t.Errorf("PYTHONHASHSEED %q", seed)
	}
}

// the Job's /tmp is an emptyDir with a size: twice the model file, the input (as large as the memory)
// and a margin
func TestAModelJobsTmpHasASizeLimit(t *testing.T) {
	prev := maxModelFileBytes
	t.Cleanup(func() { maxModelFileBytes = prev })
	maxModelFileBytes = func() int64 { return 64 << 20 }
	tmpOf := func(img *ModelGroupImage) any {
		m := modelJobManifest(jobSettings{jobTTL: 60}, "tabularia-model-9-a1", img, pyjson.Object{{Key: modelgroups.EnvInput, Value: "s3://b/in"}},
			pyjson.Object{{Key: labelModelGroup, Value: "boosting"}}, 900, []any{pyjson.Object{{Key: "key", Value: "f0"}, {Key: "path", Value: "runner/run.py"}}}, "code", "env", nil)
		pod := pyjson.Obj(pyjson.Obj(pyjson.Obj(m.Value("spec")).Value("template")).Value("spec"))
		for _, v := range objectList(pod.Value("volumes")) {
			if v.Value("name") == "tmp" {
				return pyjson.Obj(v.Value("emptyDir")).Value("sizeLimit")
			}
		}
		return nil
	}
	// 2 × 64 MiB + 4 GiB + 256 MiB
	if got := tmpOf(&ModelGroupImage{Group: "boosting", MemoryLimit: "4Gi"}); got != "4480Mi" {
		t.Errorf("4Gi group: %v", got)
	}
	// it follows the group's memory, and the file's limit
	if got := tmpOf(&ModelGroupImage{Group: "boosting", MemoryLimit: "8Gi"}); got != "8576Mi" {
		t.Errorf("8Gi group: %v", got)
	}
	maxModelFileBytes = func() int64 { return 128 << 20 }
	if got := tmpOf(&ModelGroupImage{Group: "boosting", MemoryLimit: "4Gi"}); got != "4608Mi" {
		t.Errorf("a larger file: %v", got)
	}
	// a memory nobody can read: 1 GiB of input
	if got := tmpOf(&ModelGroupImage{Group: "boosting"}); got != "1536Mi" {
		t.Errorf("no memory: %v", got)
	}
}

// rows × predictors: the boosting group's limit, said before the Job; the R groups have none.
func TestTheCellsAModelMayRead(t *testing.T) {
	predictors := make([]any, 120)
	for i := range predictors {
		predictors[i] = fmt.Sprintf("p%d", i)
	}
	got := modelCellsSentence("boosting", 1_000_000, map[string]any{"predictors": predictors})
	if got != "1,000,000 rows × 120 predictors make 120 million values; this group allows 50 million: fewer predictors, or sample before the node" {
		t.Errorf("%q", got)
	}
	if got := modelCellsSentence("boosting", 416_667, map[string]any{"predictors": predictors}); got == "" || !strings.Contains(got, "50.0 million") && !strings.Contains(got, "make 50 million") {
		t.Errorf("just above: %q", got)
	}
	for _, c := range []struct {
		group string
		rows  int64
	}{{"boosting", 416_666}, {"regression", 1_000_000}} {
		if got := modelCellsSentence(c.group, c.rows, map[string]any{"predictors": predictors}); got != "" {
			t.Errorf("%s %d: %q", c.group, c.rows, got)
		}
	}
	if millions(52_500_000) != "52.5 million" || millions(812_340) != "812,340" {
		t.Errorf("millions: %s %s", millions(52_500_000), millions(812_340))
	}
	_, _, _, _, keys := modelStepKeys("env-1/s")
	if !strings.Contains(strings.Join(keys, " "), "env-1/s/artifacts/xgboost.json env-1/s/artifacts/catboost.cbm") {
		t.Errorf("the step's keys: %v", keys)
	}
}

// The check Job of a group's version: the lock, the mark, then the self-test, in the group's language.
func TestTheChecksScriptByLanguage(t *testing.T) {
	r := mgCheckScript("r")
	want := "set -e\nRscript -e \"$TABULARIA_R_CHECK\"\necho\necho TABULARIA_SELFTEST_BEGIN\ncd /tabularia/code\n" +
		"Rscript /tabularia/code/runner/run.R --selftest\n"
	if r != want {
		t.Errorf("the R check changed:\n%s", r)
	}
	// a group whose image records system libraries adds their lines to the lock, before the mark
	withLibs := mgCheckScriptWith("r", true)
	if withLibs != "set -e\nRscript -e \"$TABULARIA_R_CHECK\"\n[ -f /opt/tabularia/system-libraries.txt ] && cat /opt/tabularia/system-libraries.txt || true\n"+
		"echo\necho TABULARIA_SELFTEST_BEGIN\ncd /tabularia/code\nRscript /tabularia/code/runner/run.R --selftest\n" {
		t.Errorf("the R check with system libraries:\n%s", withLibs)
	}
	// the script under sh: an image built before the record (no file) goes on to the self-test, one with it prints its lines
	if _, err := exec.LookPath("sh"); err == nil {
		dir := t.TempDir()
		run := func(withFile bool) string {
			f := filepath.Join(dir, "system-libraries.txt")
			_ = os.Remove(f)
			if withFile {
				if err := os.WriteFile(f, []byte("libgsl-dev@2.7.1+dfsg-6ubuntu2\n"), 0o644); err != nil {
					t.Fatal(err)
				}
			}
			script := strings.NewReplacer(scriptimage.SystemLibrariesFile, f, `Rscript -e "$TABULARIA_R_CHECK"`, "echo lock",
				"cd /tabularia/code", "cd "+dir, "Rscript /tabularia/code/runner/run.R --selftest", "echo selftest ran").Replace(withLibs)
			out, err := exec.Command("sh", "-c", script).CombinedOutput()
			if err != nil {
				t.Fatalf("the script stopped (file %v): %v\n%s", withFile, err, out)
			}
			return string(out)
		}
		if got := run(false); !strings.Contains(got, "selftest ran") || strings.Contains(got, "libgsl-dev") {
			t.Errorf("an image without the record: %q", got)
		}
		if got := run(true); !strings.Contains(got, "lock\nlibgsl-dev@2.7.1+dfsg-6ubuntu2\n") || !strings.Contains(got, "selftest ran") {
			t.Errorf("an image with the record: %q", got)
		}
	}
	if !hasSystemLibraries("customer_lifetime") || hasSystemLibraries("regression") || hasSystemLibraries("survival") {
		t.Error("which groups record system libraries")
	}
	py := mgCheckScript("python")
	for _, w := range []string{`python -I -c "$TABULARIA_PY_MANAGED_CHECK"`, "echo TABULARIA_SELFTEST_BEGIN\n",
		"python -I -B /tabularia/code/runner/run.py --selftest\n"} {
		if !strings.Contains(py, w) {
			t.Errorf("no %q in\n%s", w, py)
		}
	}
	if strings.Contains(mgPyCheck, "polars") || !strings.Contains(mgPyCheck, "S3FileSystem") {
		t.Errorf("the Python lock:\n%s", mgPyCheck)
	}
}

// ARIMA keeps a limit of rows of its own, 20,000, where its group allows more; every other model has the group's alone.
func TestAModelMayHaveALimitOfRowsBelowItsGroups(t *testing.T) {
	for _, c := range []struct {
		model        string
		group, wants int64
	}{{"arima", 200_000, 20_000}, {"arima", 5_000, 5_000}, {"arima", 0, 20_000}, {"decomposition", 200_000, 200_000}, {"linear", 2_000_000, 2_000_000}, {"linear", 0, 0}} {
		if got := modelMaxRows(c.model, c.group); got != c.wants {
			t.Errorf("%s in a group of %d: %d, want %d", c.model, c.group, got, c.wants)
		}
	}
}
