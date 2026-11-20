package api

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowresolver"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn/mlflowtest"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
)

// A model step's MLflow tracking (docs/specs/statistical-models.md, «MLflow», lot G): a flow run
// logs its experiment, run, parameters, metrics, tags, files and version to a fake tracking
// server; a server without the artifact proxy or the registry; 401 and 403; a server that does
// not answer, with and without «Continue without MLflow»; a trial logs nothing; a failed step
// is a FAILED run; the secret never reaches the Job.

func TestTheContentOfAnMLflowRun(t *testing.T) {
	params := flattenParams(map[string]any{"outcome": "quantity", "predictors": []any{"price", "channel"}, "alpha": 0.05,
		"robust": true, "reference": map[string]any{"channel": "shop"}, "weights": nil})
	got := []string{}
	for _, p := range params {
		got = append(got, p.Key+"="+p.Value)
	}
	if strings.Join(got, " ") != "alpha=0.05 outcome=quantity predictors=price,channel reference.channel=shop robust=true" {
		t.Errorf("parameters: %v", got)
	}
	rows := []resultRow{
		{Section: "model", Stat: "model", Text: ptr("linear")},
		{Section: "model", Stat: "formula", Text: ptr("quantity ~ price + channel")},
		{Section: "model", Stat: "r_squared", Value: ptr(0.81)},
		{Section: "model", Stat: "n", Value: ptr(120.0)},
		{Section: "assumption", Level: ptr("breusch_pagan"), Stat: "statistic", Value: ptr(3.2)},
		{Section: "assumption", Level: ptr("breusch_pagan"), Stat: "p_value", Value: ptr(0.07)},
		{Section: "assumption", Level: ptr("breusch_pagan"), Stat: "verdict", Text: ptr("ok")},
		{Section: "assumption", Level: ptr("schoenfeld"), Level2: ptr("channel=web"), Stat: "p_value", Value: ptr(0.4)},
		{Section: "assumption", Level: ptr("vif"), Level2: ptr("price"), Stat: "value", Value: ptr(1.3)},
		{Section: "assumption", Level: ptr("ljung_box_10"), Stat: "p_value"},
		{Section: "coef", Level: ptr("(Intercept)"), Stat: "estimate", Value: ptr(10.5)},
		{Section: "coef", Level: ptr("(Intercept)"), Stat: "std_error", Value: ptr(0.25)},
		{Section: "coef", Level: ptr("channelweb"), Level2: ptr("web"), Stat: "estimate", Value: ptr(-1.0 / 3)},
		{Section: "coef", Level: ptr("channelweb"), Level2: ptr("web"), Stat: "significance", Text: ptr("**")},
	}
	at := time.UnixMilli(1700000000000)
	metrics := map[string]float64{}
	for _, m := range modelMetrics(rows, at) {
		metrics[m.Key] = m.Value
		if m.Timestamp != 1700000000000 || m.Step != 0 {
			t.Errorf("metric %+v", m)
		}
	}
	want := map[string]float64{"r_squared": 0.81, "n": 120, "assumption.breusch_pagan.statistic": 3.2, "assumption.breusch_pagan.p_value": 0.07,
		"assumption.schoenfeld.channel_web.p_value": 0.4}
	if fmt.Sprint(metrics) != fmt.Sprint(want) {
		t.Errorf("metrics: %v", metrics)
	}
	if f := modelFormula(rows); len(f) != 1 || f[0].Value != "quantity ~ price + channel" {
		t.Errorf("formula: %v", f)
	}
	csv := string(coefficientsCSV(rows))
	if csv != "term,level,estimate,std_error,significance\n(Intercept),,10.5,0.25,\nchannelweb,web,-0.3333333333333333,,**\n" {
		t.Errorf("coefficients:\n%s", csv)
	}
	if coefficientsCSV(rows[:4]) != nil {
		t.Error("a model without coefficients has no table")
	}
	if got := string(withRunID([]byte("artifact_path: model\nflavors:\n  tabularia:\n    card: model.json\n"), "abc")); !strings.HasSuffix(got,
		"card: model.json\nrun_id: abc\n") {
		t.Errorf("MLmodel: %q", got)
	}
	if got := mlflowRunName("Pricing", "Elasticity", time.Date(2026, 11, 20, 9, 5, 0, 0, time.UTC)); got != "Pricing · Elasticity · 2026-11-20 09:05" {
		t.Errorf("run name: %q", got)
	}
}

// ── on a real database ───────────────────────────────────────────────────────

// mlflowWorld is the models' fixture with an MLflow connection, a fake tracking server and an
// engine that also gives the step's files back.
type mlflowWorld struct {
	*modelRunFixture
	ml     *mlflowtest.Server
	conn   int64
	files  map[string][]byte // by the last part of the key: results.parquet, model.json…
	mu     sync.Mutex
	engine []string // the engine's paths, in order, past the fixture's
}

const mlflowSecret = "mlflow-s3cret-pw"

func newMLflowWorld(t *testing.T) *mlflowWorld {
	f := newModelRunFixture(t)
	w := &mlflowWorld{modelRunFixture: f, ml: mlflowtest.New(t), files: map[string][]byte{
		"results.parquet": []byte("PAR1 results PAR1"),
		"model.json":      []byte(`{"format": "tabularia-model", "format_version": 1, "model": "linear"}`),
		"MLmodel":         []byte("artifact_path: model\nflavors:\n  tabularia:\n    card: model.json\n"),
		"native.rds":      []byte("RDS fitted object"),
	}}
	w.ml.Authorization = "Basic " + basicOf("ann", mlflowSecret)
	srv := httptest.NewServer(http.HandlerFunc(w.serveEngine))
	t.Cleanup(srv.Close)
	e, err := engine.New(srv.URL, modelsTestFernetKey)
	if err != nil {
		t.Fatal(err)
	}
	f.d.Engine = e
	enc, err := f.d.Fernet.Encrypt(mlflowSecret)
	if err != nil {
		t.Fatal(err)
	}
	w.conn = w.connection(f.folder, "Tracking", enc)
	prevTimeout, prevPoll := mlflowCallTimeout, mlflowReportPoll
	mlflowCallTimeout, mlflowReportPoll = 5*time.Second, 10*time.Millisecond
	t.Cleanup(func() {
		mlflowCallTimeout, mlflowReportPoll = prevTimeout, prevPoll
		_, _ = f.db.Exec(f.ctx, `DELETE FROM connections WHERE owner_id = ANY($1)`, []int64{f.admin.ID, f.ann.ID})
	})
	return w
}

func basicOf(user, pw string) string {
	r := httptest.NewRequest(http.MethodGet, "/", nil)
	r.SetBasicAuth(user, pw)
	return strings.TrimPrefix(r.Header.Get("Authorization"), "Basic ")
}

func (w *mlflowWorld) connection(folder int64, name, enc string) int64 {
	w.t.Helper()
	extra := `{"auth": "basic", "verify_tls": true, "ui_url": "https://mlflow.example.test/"}`
	var id int64
	if err := w.db.QueryRow(w.ctx, `INSERT INTO connections (name, description, project_id, owner_id, db_type, host, port, username,
		password_encrypted, database, db_schema, extra, created_at, updated_at) VALUES ($1, '', $2, $3, 'mlflow', $4, NULL, 'ann', $5, '', '',
		$6, now(), now()) RETURNING id`, name, folder, w.admin.ID, w.ml.URL, enc, extra).Scan(&id); err != nil {
		w.t.Fatal(err)
	}
	return id
}

func (w *mlflowWorld) serveEngine(rw http.ResponseWriter, r *http.Request) {
	w.mu.Lock()
	w.engine = append(w.engine, r.URL.Path)
	w.mu.Unlock()
	switch {
	case r.URL.Path == "/scripts/results/rows":
		_, _ = rw.Write([]byte(`{"rows": [
			{"section": "model", "level": null, "level_2": null, "stat": "formula", "value": null, "text": "quantity ~ price + channel"},
			{"section": "model", "level": null, "level_2": null, "stat": "r_squared", "value": 0.81, "text": null},
			{"section": "assumption", "level": "breusch_pagan", "level_2": null, "stat": "p_value", "value": 0.07, "text": null},
			{"section": "coef", "level": "price", "level_2": null, "stat": "estimate", "value": -1.5, "text": null}]}`))
	case r.URL.Path == "/scripts/objects/read":
		var in struct {
			Key string `json:"key"`
		}
		_ = json.NewDecoder(r.Body).Decode(&in)
		b, ok := w.files[in.Key[strings.LastIndex(in.Key, "/")+1:]]
		if !ok {
			rw.WriteHeader(http.StatusNotFound)
			_, _ = rw.Write([]byte(`{"detail": "Not in the scripts bucket"}`))
			return
		}
		rw.Header().Set("Content-Length", fmt.Sprint(len(b)))
		_, _ = rw.Write(b)
	case r.URL.Path == "/reports/model":
		_, _ = rw.Write([]byte(`{"task_id": "t1", "status": "submitted"}`))
	case r.URL.Path == "/tasks/exports/t1" && r.URL.Query().Get("owner") != "":
		_, _ = rw.Write([]byte(`{"status": "ready", "filename": "r.pdf", "size": 9}`))
	case r.URL.Path == "/tasks/exports/t1/file" && r.URL.Query().Get("owner") != "":
		_, _ = rw.Write([]byte("%PDF-1.7 report"))
	default:
		w.modelRunFixture.engine.ServeHTTP(rw, r)
	}
}

func (w *mlflowWorld) enginePaths() string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return strings.Join(w.engine, " ")
}

// flowWith saves source → model with the node's MLflow block and gives its run.
func (w *mlflowWorld) flowWith(block string) *scriptNodeRun {
	w.t.Helper()
	text := `{"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": 1}},
		{"id": "m", "type": "model", "data": {"model": "linear", "label": "Elasticity", "params": ` + linearParams + `,
		"mlflow": ` + block + `, "timeoutSeconds": 600, "retries": 0}}], "edges": [{"source": "s", "target": "m"}]}`
	var id int64
	if err := w.db.QueryRow(w.ctx, `INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
		VALUES ('Pricing', '', $1, $2, $3, 'duckdb', now(), now()) RETURNING id`, w.folder, w.ann.ID, text).Scan(&id); err != nil {
		w.t.Fatal(err)
	}
	definition := mustDef(w.t, text)
	node := objectList(definition.Value("nodes"))[1]
	return &scriptNodeRun{q: w.q, user: w.ann, flow: &jobFlow{ID: id, ProjectID: w.folder, Name: "Pricing", Definition: text, Engine: "duckdb"},
		definition: definition, node: node, trigger: "manual", mode: "development", outputs: flowresolver.ScriptOutputs{}}
}

func (w *mlflowWorld) runWith(block string) (int64, error) {
	w.t.Helper()
	r := w.flowWith(block)
	_, err := w.q.runModel(w.ctx, r)
	var id int64
	_ = w.db.QueryRow(w.ctx, `SELECT coalesce(max(id), 0) FROM runs WHERE flow_id = $1 AND kind = 'model'`, r.flow.ID).Scan(&id)
	return id, err
}

func (w *mlflowWorld) block(extra string) string {
	return fmt.Sprintf(`{"connectionId": %d, "experiment": "Pasta/price elasticity"%s}`, w.conn, extra)
}

type mlflowColumns struct{ experiment, run, model, version, errText *string }

func (w *mlflowWorld) columns(id int64) (mlflowColumns, map[string]any) {
	w.t.Helper()
	var c mlflowColumns
	var script string
	if err := w.db.QueryRow(w.ctx, `SELECT mlflow_experiment_id, mlflow_run_id, mlflow_model_name, mlflow_model_version, mlflow_error, script
		FROM runs WHERE id = $1`, id).Scan(&c.experiment, &c.run, &c.model, &c.version, &c.errText, &script); err != nil {
		w.t.Fatal(err)
	}
	var info map[string]any
	if err := json.Unmarshal([]byte(script), &info); err != nil {
		w.t.Fatal(err)
	}
	m, _ := info["mlflow"].(map[string]any)
	return c, m
}

// taskOf is the task.json of the Job's ConfigMap.
func taskOf(t *testing.T, a modelAttemptSeen) modelgroups.Task {
	t.Helper()
	var task modelgroups.Task
	for _, kv := range a.code {
		if s, _ := kv.Value.(string); strings.HasPrefix(s, `{"protocol":`) {
			if err := json.Unmarshal([]byte(s), &task); err != nil {
				t.Fatal(err)
			}
			return task
		}
	}
	t.Fatal("no task.json")
	return task
}

func TestAFlowRunLogsItsModelToMLflow(t *testing.T) {
	w := newMLflowWorld(t)
	id, err := w.runWith(w.block(`, "registerAs": "price-elasticity"`))
	if err != nil {
		t.Fatal(err)
	}
	if r := w.row(id); r.status != "SUCCESS" {
		t.Fatalf("%+v", r)
	}
	// the experiment created, the run FINISHED
	if w.ml.Experiments["Pasta/price elasticity"] != "1" || w.ml.ExpTags["Pasta/price elasticity"]["tabularia.created"] != "true" {
		t.Errorf("experiment: %v %v", w.ml.Experiments, w.ml.ExpTags)
	}
	run := w.ml.OnlyRun()
	if run == nil || run.Status != "FINISHED" || !run.Ended || !strings.HasPrefix(run.Name, "Pricing · Elasticity · ") {
		t.Fatalf("run: %+v", run)
	}
	// parameters: the node's, flattened, and the formula
	if run.Params["outcome"] != "quantity" || run.Params["predictors"] != "price,channel" || run.Params["output"] != "results" ||
		run.Params["formula"] != "quantity ~ price + channel" {
		t.Errorf("params: %v", run.Params)
	}
	if run.Metrics["r_squared"] != 0.81 || run.Metrics["assumption.breusch_pagan.p_value"] != 0.07 || len(run.Metrics) != 2 {
		t.Errorf("metrics: %v", run.Metrics)
	}
	tags := run.Tags
	for k, v := range map[string]string{"tabularia.model": "linear", "tabularia.group": "regression", "tabularia.group_version": "3",
		"tabularia.image_digest": "sha256:0123456789abcdef", "tabularia.node_id": "m", "tabularia.node_name": "Elasticity",
		"tabularia.step_run_id": fmt.Sprint(id), "tabularia.run_id": fmt.Sprint(id), "tabularia.flow_name": "Pricing",
		"tabularia.launched_by": w.ann.Email, "tabularia.input_rows": "120", "tabularia.dev_sample": "true",
		"mlflow.source.type": "JOB", "tabularia.url": fmt.Sprintf("/runs?run=%d", id)} {
		if tags[k] != v {
			t.Errorf("tag %s = %q, want %q", k, tags[k], v)
		}
	}
	if !strings.HasSuffix(tags["mlflow.source.name"], "/editor?flow="+tags["tabularia.flow_id"]) {
		t.Errorf("source: %q", tags["mlflow.source.name"])
	}
	// the files, through the proxy
	dir := "1/" + run.ID + "/artifacts/"
	for _, name := range []string{"tabularia/results.parquet", "tabularia/coefficients.csv", "tabularia/report.pdf", "model/MLmodel",
		"model/model.json", "model/native.rds"} {
		if _, ok := w.ml.Artifacts[dir+name]; !ok {
			t.Errorf("%s not uploaded (%d files)", name, len(w.ml.Artifacts))
		}
	}
	if len(w.ml.Artifacts) != 6 {
		t.Errorf("%d files", len(w.ml.Artifacts))
	}
	if !strings.HasSuffix(string(w.ml.Artifacts[dir+"model/MLmodel"]), "run_id: "+run.ID+"\n") ||
		string(w.ml.Artifacts[dir+"tabularia/coefficients.csv"]) != "term,level,estimate\nprice,,-1.5\n" ||
		string(w.ml.Artifacts[dir+"tabularia/report.pdf"]) != "%PDF-1.7 report" {
		t.Errorf("MLmodel %q, csv %q", w.ml.Artifacts[dir+"model/MLmodel"], w.ml.Artifacts[dir+"tabularia/coefficients.csv"])
	}
	// the version, with the card's hash Predict checks
	sum := sha256.Sum256(w.files["model.json"])
	versions := w.ml.Models["price-elasticity"]
	if len(versions) != 1 || versions[0].Source != "mlflow-artifacts:/1/"+run.ID+"/artifacts/model" || versions[0].RunID != run.ID ||
		versions[0].Tags["tabularia.card_sha256"] != hex.EncodeToString(sum[:]) || versions[0].Tags["tabularia.model"] != "linear" ||
		versions[0].Tags["tabularia.group"] != "regression" {
		t.Errorf("versions: %+v", versions)
	}
	// Tabularia's run keeps the ids and the link
	c, m := w.columns(id)
	if deref(c.experiment) != "1" || deref(c.run) != run.ID || deref(c.model) != "price-elasticity" || deref(c.version) != "1" || c.errText != nil {
		t.Errorf("columns: %+v", c)
	}
	if m["run_url"] != "https://mlflow.example.test/#/experiments/1/runs/"+run.ID || m["model_version"] != "1" || m["artifacts"] != true ||
		m["model_url"] != "https://mlflow.example.test/#/models/price-elasticity/versions/1" || len(m["warnings"].([]any)) != 0 {
		t.Errorf("the run's record: %v", m)
	}
	// the Job: told to write MLmodel and native.rds, and nothing of MLflow's
	a := w.attempts[0]
	if !taskOf(t, a).MLflow {
		t.Error("task.json's mlflow is false")
	}
	everything := pyDumps(a.manifest) + pyDumps(a.code) + pyDumps(a.secret)
	for _, bad := range []string{mlflowSecret, basicOf("ann", mlflowSecret), w.ml.URL, "MLFLOW"} {
		if strings.Contains(everything, bad) {
			t.Errorf("the Job holds %q", bad)
		}
	}
	// the step's files left the bucket after they were logged
	paths := w.enginePaths()
	if !strings.HasSuffix(paths, "/scripts/objects/delete") || !strings.Contains(paths, "/scripts/results/rows") {
		t.Errorf("engine: %s", paths)
	}
}

func TestAServerWithoutTheArtifactProxyOrTheRegistry(t *testing.T) {
	w := newMLflowWorld(t)
	w.ml.NoProxy = true
	id, err := w.runWith(w.block(`, "registerAs": "price-elasticity"`))
	if err != nil {
		t.Fatal(err)
	}
	run := w.ml.OnlyRun()
	if run == nil || run.Status != "FINISHED" || len(run.Params) == 0 || len(w.ml.Artifacts) != 0 || len(w.ml.Models) != 0 {
		t.Fatalf("no proxy: %+v, %d files, %d models", run, len(w.ml.Artifacts), len(w.ml.Models))
	}
	c, m := w.columns(id)
	warnings := fmt.Sprint(m["warnings"])
	if m["artifacts"] != false || !strings.Contains(warnings, "no artifact proxy") || !strings.Contains(warnings, "not registered") ||
		c.version != nil || w.row(id).status != "SUCCESS" {
		t.Errorf("no proxy: %v %+v", m, c)
	}
	// a server with the proxy and without the registry
	w.ml.NoProxy, w.ml.NoRegistry = false, true
	id, err = w.runWith(w.block(`, "registerAs": "price-elasticity"`))
	if err != nil {
		t.Fatal(err)
	}
	_, m = w.columns(id)
	if m["artifacts"] != true || !strings.Contains(fmt.Sprint(m["warnings"]), "has no model registry") || m["model_version"] != nil {
		t.Errorf("no registry: %v", m)
	}
}

func TestRefusedCredentialsFailTheStep(t *testing.T) {
	w := newMLflowWorld(t)
	w.ml.Authorization = "Basic " + basicOf("ann", "another")
	id, err := w.runWith(w.block(""))
	want := "model «Elasticity»: the model was fitted but MLflow could not record it: the tracking server refused the connection's credentials (401)"
	if err == nil || !strings.HasPrefix(err.Error(), want) {
		t.Fatalf("401: %v", err)
	}
	r := w.row(id)
	c, m := w.columns(id)
	if r.status != "FAILURE" || r.errText != err.Error() || r.resultsKey == "" || !strings.HasPrefix(deref(c.errText), "the model was fitted") ||
		m["continued"] != false {
		t.Errorf("%+v %+v %v", r, c, m)
	}
	for _, bad := range []string{mlflowSecret, mlflowtest.Leak} {
		if strings.Contains(r.errText+r.script, bad) {
			t.Errorf("%q in the run", bad)
		}
	}
	// 403: the credentials work, but may not create a run
	w.ml.Authorization = ""
	w.ml.Fail = func(r *http.Request) int {
		if r.URL.Path == "/api/2.0/mlflow/runs/create" {
			return http.StatusForbidden
		}
		return 0
	}
	if _, err := w.runWith(w.block("")); err == nil || !strings.HasSuffix(err.Error(),
		"the connection's credentials are not allowed to do this on the tracking server (403), creating the run") {
		t.Fatalf("403: %v", err)
	}
}

func TestAServerThatDoesNotAnswer(t *testing.T) {
	w := newMLflowWorld(t)
	mlflowCallTimeout = 150 * time.Millisecond
	w.ml.Delay = 2 * time.Second
	start := time.Now()
	id, err := w.runWith(w.block(""))
	if err == nil || !strings.Contains(err.Error(), "MLflow could not record it: the tracking server did not answer in time, finding the experiment") {
		t.Fatalf("timeout: %v", err)
	}
	if time.Since(start) > 5*time.Second || w.row(id).status != "FAILURE" {
		t.Errorf("after %s: %+v", time.Since(start), w.row(id))
	}
	// with «Continue without MLflow»: the step succeeds, the run page says why nothing was logged
	id, err = w.runWith(w.block(`, "continueWithout": true`))
	if err != nil {
		t.Fatal(err)
	}
	c, m := w.columns(id)
	if w.row(id).status != "SUCCESS" || m["continued"] != true || !strings.Contains(fmt.Sprint(m["error"]), "did not answer in time") ||
		!strings.Contains(deref(c.errText), "did not answer in time") || c.run != nil {
		t.Errorf("continued: %v %+v", m, c)
	}
}

func TestATrialLogsNothing(t *testing.T) {
	w := newMLflowWorld(t)
	r := w.flowWith(w.block(`, "registerAs": "price-elasticity"`))
	var trial int64
	if err := w.db.QueryRow(w.ctx, `INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, input_key, output_bucket,
		output_key, publish_description, publish_overwrite, publish_sort_keys, script, started_at) VALUES ('model', $1, '', 'PENDING', $2, 'trial',
		'', '', '', '', false, '[]', '{"node_id": "m", "trial": true}', now()) RETURNING id`, r.flow.ID, w.ann.ID).Scan(&trial); err != nil {
		t.Fatal(err)
	}
	r.trial, r.trigger = trial, "trial"
	if _, err := w.q.runModel(w.ctx, r); err != nil {
		t.Fatal(err)
	}
	if calls := w.ml.Paths(); len(calls) != 0 {
		t.Errorf("a trial called MLflow: %v", calls)
	}
	if taskOf(t, w.attempts[0]).MLflow {
		t.Error("a trial's task.json asks for MLflow's files")
	}
	if _, m := w.columns(trial); m != nil {
		t.Errorf("a trial's record: %v", m)
	}
	// with a broken block too: a trial does not even read it
	r = w.flowWith(`{"connectionId": 999999, "experiment": ""}`)
	if err := w.db.QueryRow(w.ctx, `INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, input_key, output_bucket,
		output_key, publish_description, publish_overwrite, publish_sort_keys, script, started_at) VALUES ('model', $1, '', 'PENDING', $2, 'trial',
		'', '', '', '', false, '[]', '{"node_id": "m", "trial": true}', now()) RETURNING id`, r.flow.ID, w.ann.ID).Scan(&r.trial); err != nil {
		t.Fatal(err)
	}
	if _, err := w.q.runModel(w.ctx, r); err != nil {
		t.Errorf("a trial with an unfinished MLflow block: %v", err)
	}
}

func TestAFailedStepIsAFailedRun(t *testing.T) {
	w := newMLflowWorld(t)
	w.script = []modelAttemptResult{{log: "Error in fit\n", exit: int64(1), reason: "it ended with exit code 1"}}
	id, err := w.runWith(w.block(""))
	if err == nil || err.Error() != "model «Elasticity» failed: it ended with exit code 1" {
		t.Fatalf("%v", err)
	}
	run := w.ml.OnlyRun()
	if run == nil || run.Status != "FAILED" || run.Tags["tabularia.error"] != err.Error() || run.Params["outcome"] != "quantity" {
		t.Fatalf("%+v", run)
	}
	c, m := w.columns(id)
	if m["failed_step"] != true || deref(c.run) != run.ID || w.row(id).errText != err.Error() {
		t.Errorf("%v %+v", m, c)
	}
	// stopped before the Job (the row limit): nothing was fitted, nothing is logged
	if _, err := w.db.Exec(w.ctx, `UPDATE model_groups SET max_rows = 50 WHERE id = 'regression'`); err != nil {
		t.Fatal(err)
	}
	if _, err := w.runWith(w.block("")); err == nil || len(w.ml.Runs) != 1 {
		t.Errorf("before the Job: %v, %d runs", err, len(w.ml.Runs))
	}
}

func TestAConnectionThePersonMayNotUseStopsTheStepBeforeAnything(t *testing.T) {
	w := newMLflowWorld(t)
	var other int64
	if err := w.db.QueryRow(w.ctx, `INSERT INTO projects (name, description, created_at) VALUES ($1, '', now()) RETURNING id`,
		fmt.Sprintf("mlflow-other-%d", time.Now().UnixNano())).Scan(&other); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_, _ = w.db.Exec(w.ctx, `DELETE FROM connections WHERE project_id = $1`, other)
		_, _ = w.db.Exec(w.ctx, `DELETE FROM projects WHERE id = $1`, other)
	})
	conn := w.connection(other, "Elsewhere", "")
	before := len(w.attempts)
	_, err := w.runWith(fmt.Sprintf(`{"connectionId": %d, "experiment": "x"}`, conn))
	if err == nil || err.Error() != fmt.Sprintf("model «Elasticity»: %s may not use the MLflow connection «Elsewhere»: it needs CONNECT on its folder", w.ann.Email) {
		t.Fatalf("%v", err)
	}
	if _, err := w.runWith(`{"connectionId": 999999, "experiment": "x"}`); err == nil || !strings.Contains(err.Error(), "does not exist any more") {
		t.Errorf("a connection gone: %v", err)
	}
	if _, err := w.runWith(fmt.Sprintf(`{"connectionId": %d, "experiment": " "}`, w.conn)); err == nil ||
		!strings.Contains(err.Error(), "name the MLflow experiment") {
		t.Errorf("no experiment: %v", err)
	}
	if len(w.attempts) != before || len(w.ml.Paths()) != 0 {
		t.Errorf("a refusal reached the cluster or MLflow")
	}
	var n int64
	_ = w.db.QueryRow(w.ctx, `SELECT count(*) FROM runs WHERE launched_by = $1`, w.ann.ID).Scan(&n)
	if n != 0 {
		t.Errorf("%d runs created by refusals", n)
	}
}
