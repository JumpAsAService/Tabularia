package api

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
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

// A boosting model's results also give the diagnostics' value, the importance of the first 20
// predictors and the learning curves as metric histories (step = the round).
func TestABoostingRunLogsTheDiagnosticsTheImportanceAndTheLearningCurves(t *testing.T) {
	rows := []resultRow{
		{Section: "model", Stat: "library", Text: ptr("xgboost")},
		{Section: "model", Stat: "task", Text: ptr("binary")},
		{Section: "model", Stat: "stop_metric", Text: ptr("auc")},
		{Section: "model", Stat: "trees", Value: ptr(3.0)},
		{Section: "assumption", Level: ptr("overfitting"), Stat: "value", Value: ptr(0.02)},
		{Section: "assumption", Level: ptr("zero_importance"), Level2: ptr("noise"), Stat: "value", Value: ptr(0.0)},
		{Section: "importance", Level: ptr("price"), Stat: "gain_share", Value: ptr(0.7)},
		{Section: "importance", Level: ptr("price"), Stat: "mean_abs_shap_share", Value: ptr(0.6)},
		{Section: "importance", Level: ptr("price"), Stat: "rank", Value: ptr(1.0)},
		{Section: "importance", Level: ptr("price"), Stat: "mean_abs_shap", Level2: ptr("a"), Value: ptr(0.3)},
		{Section: "curve", Level: ptr("learning_loss"), Level2: ptr("train"), Stat: "y", X0: ptr(1.0), Value: ptr(0.6)},
		{Section: "curve", Level: ptr("learning_loss"), Level2: ptr("train"), Stat: "y", X0: ptr(2.0), Value: ptr(0.5)},
		{Section: "curve", Level: ptr("learning_loss"), Level2: ptr("validation"), Stat: "y", X0: ptr(2.0), Value: ptr(0.55)},
		{Section: "curve", Level: ptr("learning_stop"), Level2: ptr("validation"), Stat: "y", X0: ptr(3.0), Value: ptr(0.8)},
		{Section: "curve", Level: ptr("roc"), Level2: ptr("validation"), Stat: "y", X0: ptr(0.1), Value: ptr(0.2)},
	}
	for i := 0; i < 25; i++ { // only the first 20 predictors
		rows = append(rows, resultRow{Section: "importance", Level: ptr(fmt.Sprintf("q%02d", i)), Stat: "gain_share", Value: ptr(0.01)})
	}
	got := []string{}
	for _, m := range modelMetrics(rows, time.UnixMilli(1700000000000)) {
		if strings.HasPrefix(m.Key, "importance.q") {
			continue
		}
		got = append(got, fmt.Sprintf("%s@%d=%g", m.Key, m.Step, m.Value))
	}
	want := "trees@0=3 assumption.overfitting.value@0=0.02 assumption.zero_importance.noise.value@0=0 " +
		"importance.price.gain_share@0=0.7 importance.price.mean_abs_shap_share@0=0.6 " +
		"learning.train.logloss@1=0.6 learning.train.logloss@2=0.5 learning.validation.logloss@2=0.55 learning.validation.auc@3=0.8"
	if strings.Join(got, " ") != want {
		t.Errorf("metrics:\n got %s\nwant %s", strings.Join(got, " "), want)
	}
	n := 0
	for _, m := range modelMetrics(rows, time.Now()) {
		if strings.HasPrefix(m.Key, "importance.q") {
			n++
		}
	}
	if n != 19 { // price + q00..q18 are the 20 predictors
		t.Errorf("predictors beyond the 20th are logged: %d keys", n)
	}
}

// MLflow's keys: at most 250 characters (MAX_ENTITY_KEY_LENGTH), and two names that are one once
// cleaned do not lose the second without a word
func TestMetricKeysAreWithinMLflowsLimitAndStayApart(t *testing.T) {
	long := strings.Repeat("x", 300)
	rows := []resultRow{
		{Section: "model", Stat: "library", Text: ptr("xgboost")},
		{Section: "model", Stat: "a#b", Value: ptr(1.0)},
		{Section: "model", Stat: "a_b", Value: ptr(2.0)}, // the same key once cleaned
		{Section: "model", Stat: "a?b", Value: ptr(3.0)},
		{Section: "model", Stat: "a_b", Value: ptr(2.0)}, // the same name again: not a collision
		{Section: "model", Stat: long + "1", Value: ptr(4.0)},
		{Section: "model", Stat: long + "2", Value: ptr(5.0)}, // alike in its first 250 characters
		{Section: "importance", Level: ptr(long), Stat: "gain_share", Value: ptr(0.5)},
	}
	metrics, warnings := modelMetricsChecked(rows, time.UnixMilli(1700000000000))
	got := map[string]float64{}
	for _, m := range metrics {
		if len(m.Key) > 250 {
			t.Errorf("a key of %d characters", len(m.Key))
		}
		if _, dup := got[m.Key]; dup {
			t.Errorf("key twice: %s", m.Key)
		}
		got[m.Key] = m.Value
	}
	if len(metrics) != 6 {
		t.Fatalf("%d metrics, want the 6 distinct names: %v", len(metrics), got)
	}
	// the first keeps its name; the others are kept, apart
	if got["a_b"] != 1 || got["a_b_2"] != 2 || got["a_b_3"] != 3 {
		t.Errorf("the collisions: %v", got)
	}
	if len(warnings) != 1 || !strings.Contains(warnings[0], "are the same name once MLflow's characters are kept") ||
		!strings.Contains(warnings[0], "a_b_2") || !strings.Contains(warnings[0], "a_b_3") {
		t.Errorf("warnings: %q", warnings)
	}
	// clean names say nothing
	if _, w := modelMetricsChecked([]resultRow{{Section: "model", Stat: "n", Value: ptr(1.0)}}, time.Now()); len(w) != 0 {
		t.Errorf("a warning without a collision: %q", w)
	}
}

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
	// rows, when set, is what the engine answers to the read-back of the results (the linear model's otherwise)
	rows string
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
	case r.URL.Path == "/scripts/results/rows" && w.rows != "":
		_, _ = rw.Write([]byte(w.rows))
	case r.URL.Path == "/scripts/results/rows":
		_, _ = rw.Write([]byte(`{"rows": [
			{"section": "model", "level": null, "level_2": null, "stat": "formula", "value": null, "text": "quantity ~ price + channel"},
			{"section": "model", "level": null, "level_2": null, "stat": "r_squared", "value": 0.81, "text": null},
			{"section": "assumption", "level": "breusch_pagan", "level_2": null, "stat": "p_value", "value": 0.07, "text": null},
			{"section": "coef", "level": "price", "level_2": null, "stat": "estimate", "value": -1.5, "text": null},
			{"section": "model", "level": null, "level_2": null, "stat": "distribution", "value": null, "text": "weibull"},
			{"section": "candidate", "level": "weibull", "level_2": null, "stat": "aic", "value": 1234.8, "text": null},
			{"section": "candidate", "level": "weibull", "level_2": null, "stat": "parsimony", "value": 1, "text": null},
			{"section": "candidate", "level": "weibull", "level_2": null, "stat": "status", "value": null, "text": "ok"},
			{"section": "parameter", "level": "shape", "level_2": null, "stat": "estimate", "value": 1.42, "text": null}]}`))
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
	if run.Params["distribution_chosen"] != "weibull" {
		t.Errorf("the distribution chosen: %v", run.Params)
	}
	if run.Metrics["candidate.weibull.aic"] != 1234.8 || run.Metrics["candidate.weibull.parsimony"] != 1 || run.Metrics["parameter.shape.estimate"] != 1.42 {
		t.Errorf("the candidates and parameters: %v", run.Metrics)
	}
	if run.Metrics["r_squared"] != 0.81 || run.Metrics["assumption.breusch_pagan.p_value"] != 0.07 || len(run.Metrics) != 5 {
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
	if err == nil || err.Error() != fmt.Sprintf("model «Elasticity»: %s may not use the MLflow connection #%d: it needs CONNECT on its folder", w.ann.Email, conn) {
		t.Fatalf("%v", err)
	}
	if strings.Contains(err.Error(), "Elsewhere") {
		t.Errorf("the refusal gives the name of a connection the person may not use: %v", err)
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

// ── the candidates, the parameters and the distribution chosen (parametric survival, ARIMA) ──

// An automatic choice's candidates, a distribution's parameters and the distribution chosen are
// logged: `candidate.<level>.<stat>` for every number, `parameter.<level>[.<level_2>].estimate`,
// and the parameter `distribution_chosen`.
func TestTheCandidatesTheParametersAndTheDistributionChosenAreLogged(t *testing.T) {
	rows := []resultRow{
		{Section: "model", Stat: "distribution", Text: ptr("weibull")},
		{Section: "model", Stat: "selection", Text: ptr("auto")},
		{Section: "model", Stat: "aic", Value: ptr(1234.8)},
		{Section: "candidate", Level: ptr("weibull"), Stat: "aic", Value: ptr(1234.8)},
		{Section: "candidate", Level: ptr("weibull"), Stat: "akaike_weight", Value: ptr(0.4)},
		{Section: "candidate", Level: ptr("weibull"), Stat: "chosen", Value: ptr(1.0)},
		{Section: "candidate", Level: ptr("weibull"), Stat: "parsimony", Value: ptr(1.0)},
		{Section: "candidate", Level: ptr("weibull"), Stat: "status", Text: ptr("ok")},
		{Section: "candidate", Level: ptr("gengamma"), Stat: "aic", Value: ptr(1234.5)},
		{Section: "candidate", Level: ptr("gengamma"), Stat: "parsimony", Value: ptr(0.0)},
		{Section: "candidate", Level: ptr("gompertz"), Stat: "aic"}, // not finite: no metric
		{Section: "candidate", Level: ptr("ARIMA(1,1,0)(0,1,1)[12]"), Stat: "aicc", Value: ptr(-582.6)},
		{Section: "parameter", Level: ptr("shape"), Stat: "estimate", Value: ptr(1.42)},
		{Section: "parameter", Level: ptr("shape"), Stat: "std_error", Value: ptr(0.09)}, // only the estimate
		{Section: "parameter", Level: ptr("scale"), Level2: ptr("line=B"), Stat: "estimate", Value: ptr(3.5)},
	}
	got := map[string]float64{}
	for _, m := range modelMetrics(rows, time.Now()) {
		got[m.Key] = m.Value
	}
	want := map[string]float64{"aic": 1234.8,
		"candidate.weibull.aic": 1234.8, "candidate.weibull.akaike_weight": 0.4, "candidate.weibull.chosen": 1, "candidate.weibull.parsimony": 1,
		"candidate.gengamma.aic": 1234.5, "candidate.gengamma.parsimony": 0,
		"candidate.ARIMA_1_1_0__0_1_1__12_.aicc": -582.6,
		"parameter.shape.estimate":               1.42, "parameter.scale.line_B.estimate": 3.5}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Errorf("metrics:\n got %v\nwant %v", got, want)
	}
	if d := modelDistribution(rows); len(d) != 1 || d[0].Key != "distribution_chosen" || d[0].Value != "weibull" {
		t.Errorf("distribution_chosen: %v", d)
	}
	if d := modelDistribution([]resultRow{{Section: "model", Stat: "n", Value: ptr(3.0)}}); len(d) != 0 {
		t.Errorf("a model without a distribution logs one: %v", d)
	}
}

// Candidates' names are kept apart and within MLflow's limit whatever the level says.
func TestCandidateKeysStayWithinTheLimitAndApart(t *testing.T) {
	long := strings.Repeat("o", 300)
	rows := []resultRow{
		{Section: "candidate", Level: ptr(long + "1"), Stat: "aic", Value: ptr(1.0)},
		{Section: "candidate", Level: ptr(long + "2"), Stat: "aic", Value: ptr(2.0)},
		{Section: "candidate", Level: ptr("ARIMA(1,0,0)"), Stat: "aic", Value: ptr(3.0)},
		{Section: "candidate", Level: ptr("ARIMA[1,0,0]"), Stat: "aic", Value: ptr(4.0)}, // the same key once cleaned
	}
	metrics, warnings := modelMetricsChecked(rows, time.Now())
	seen := map[string]bool{}
	for _, m := range metrics {
		if len(m.Key) > 250 || seen[m.Key] {
			t.Errorf("key %q (%d characters) too long or twice", m.Key, len(m.Key))
		}
		seen[m.Key] = true
	}
	if len(metrics) != 4 || len(warnings) != 1 {
		t.Errorf("%d metrics, warnings %q", len(metrics), warnings)
	}
}

// The catalogue's fixtures (backend/tests/fixtures/models, not mounted when the gateway's tests
// run alone: then it is skipped) as the engine returns them: whatever numbers they hold, every
// finite number of the candidate section is a metric of the same value, every parameter's estimate
// too, and the distribution is the parameter. It holds when lot P5 puts the runner's output there.
func TestTheCatalogueFixturesLogTheirCandidatesAndParameters(t *testing.T) {
	dir := "../../../backend/tests/fixtures/models"
	for _, name := range []string{"parametric_survival-auto", "parametric_survival-ph_aft", "parametric_survival-aft",
		"parametric_survival-ph", "arima-auto", "arima-manual", "clv-purchases", "clv-value", "bg_bb-purchases", "bg_bb-value"} {
		raw, err := os.ReadFile(dir + "/" + name + ".json")
		if err != nil {
			t.Skipf("no fixtures here: %v", err)
		}
		var fx struct {
			Columns []string `json:"columns"`
			Rows    [][]any  `json:"rows"`
		}
		if err := json.Unmarshal(raw, &fx); err != nil {
			t.Fatal(err)
		}
		idx := map[string]int{}
		for i, c := range fx.Columns {
			idx[c] = i
		}
		str := func(row []any, c string) *string {
			if s, ok := row[idx[c]].(string); ok {
				return &s
			}
			return nil
		}
		num := func(row []any, c string) *float64 {
			if f, ok := row[idx[c]].(float64); ok {
				return &f
			}
			return nil
		}
		var rows []resultRow
		for _, r := range fx.Rows {
			rows = append(rows, resultRow{Section: *str(r, "section"), Level: str(r, "level"), Level2: str(r, "level_2"),
				Stat: *str(r, "stat"), Value: num(r, "value"), Text: str(r, "text"), X0: num(r, "x0")})
		}
		metrics, warnings := modelMetricsChecked(rows, time.Now())
		got := map[string]float64{}
		for _, m := range metrics {
			if len(m.Key) > 250 {
				t.Errorf("%s: a key of %d characters", name, len(m.Key))
			}
			got[m.Key] = m.Value
		}
		if len(got) != len(metrics) {
			t.Errorf("%s: a key twice", name)
		}
		for _, w := range warnings {
			t.Errorf("%s: a warning: %s", name, w)
		}
		candidates, parameters := 0, 0
		for _, r := range rows {
			if r.Value == nil {
				continue
			}
			switch {
			case r.Section == "candidate":
				candidates++
				if v, ok := got[metricKey("candidate."+*r.Level+"."+r.Stat)]; !ok || v != *r.Value {
					t.Errorf("%s: candidate %s %s is not logged as %g", name, *r.Level, r.Stat, *r.Value)
				}
			case r.Section == "parameter" && r.Stat == "estimate":
				parameters++
			}
		}
		if parameters > 0 && !hasMetricPrefix(got, "parameter.") {
			t.Errorf("%s: the parameters' estimates are not logged", name)
		}
		if strings.HasPrefix(name, "parametric_survival") {
			d := modelDistribution(rows)
			if len(d) != 1 || d[0].Value == "" {
				t.Errorf("%s: distribution_chosen: %v", name, d)
			}
		}
		if strings.HasSuffix(name, "-auto") && candidates == 0 {
			t.Errorf("%s: an automatic choice without candidates", name)
		}
	}
}

// ── customer lifetime (docs/specs/customer-lifetime-models.md, lot B7) ──────

// A customer lifetime fit's results: the model section's numbers (the parameters, the CLV, the
// holdout), every candidate's numbers, the checks' statistic, p-value and value; the purchase model
// used and the horizon used are parameters; the per-customer rows, the charts' points and the
// coefficients' table are not metrics.
func TestACustomerLifetimeFitLogsItsNumbersAndTheChoice(t *testing.T) {
	rows := []resultRow{
		{Section: "model", Stat: "model", Text: ptr("clv")},
		{Section: "model", Stat: "purchase_model", Text: ptr("mbg_nbd")},
		{Section: "model", Stat: "purchase_model_asked", Text: ptr("auto")},
		{Section: "model", Stat: "time_unit", Text: ptr("week")},
		{Section: "model", Stat: "horizon", Value: ptr(52.0)},
		{Section: "model", Stat: "log_lik", Value: ptr(-9582.14)},
		{Section: "model", Stat: "param_r", Value: ptr(0.5248)},
		{Section: "model", Stat: "clv_total", Value: ptr(97108.4)},
		{Section: "model", Stat: "discount_rate", Value: ptr(0.1)},
		{Section: "model", Stat: "margin", Value: ptr(1.0)},
		{Section: "candidate", Level: ptr("pareto_nbd"), Stat: "log_lik", Value: ptr(-9594.98)},
		{Section: "candidate", Level: ptr("pareto_nbd"), Stat: "chosen", Value: ptr(0.0)},
		{Section: "candidate", Level: ptr("mbg_nbd"), Stat: "chosen", Value: ptr(1.0)},
		{Section: "candidate", Level: ptr("mbg_nbd"), Stat: "holdout_rel_error", Value: ptr(-0.12)},
		{Section: "assumption", Level: ptr("frequency_fit"), Stat: "p_value", Value: ptr(0.3)},
		{Section: "assumption", Level: ptr("holdout"), Stat: "value", Value: ptr(-0.12)}, // a measure: no verdict
		{Section: "assumption", Level: ptr("holdout"), Stat: "verdict", Text: ptr("passed")},
		{Section: "customer", Level: ptr("c4217"), Stat: "clv", Value: ptr(1234.5)},
		{Section: "coef", Level: ptr("r"), Level2: ptr("purchases"), Stat: "estimate", Value: ptr(0.5248)},
	}
	got := map[string]float64{}
	for _, m := range modelMetrics(rows, time.Now()) {
		got[m.Key] = m.Value
	}
	want := map[string]float64{"horizon": 52, "log_lik": -9582.14, "param_r": 0.5248, "clv_total": 97108.4, "discount_rate": 0.1, "margin": 1,
		"candidate.pareto_nbd.log_lik": -9594.98, "candidate.pareto_nbd.chosen": 0, "candidate.mbg_nbd.chosen": 1,
		"candidate.mbg_nbd.holdout_rel_error": -0.12, "assumption.frequency_fit.p_value": 0.3, "assumption.holdout.value": -0.12}
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Errorf("metrics:\n got %v\nwant %v", got, want)
	}
	params := func(checked map[string]any) map[string]string {
		out := map[string]string{}
		for _, p := range customerLifetimeParams(rows, checked) {
			out[p.Key] = p.Value
		}
		return out
	}
	// the node said `auto` and no horizon: the model used and the horizon used are logged
	if got := fmt.Sprint(params(map[string]any{"purchase_model": "auto", "horizon": nil})); got != "map[horizon:52 purchase_model_chosen:mbg_nbd]" {
		t.Errorf("params: %s", got)
	}
	// a horizon the node gave is the node's parameter, not repeated
	if got := fmt.Sprint(params(map[string]any{"horizon": 26.0})); got != "map[purchase_model_chosen:mbg_nbd]" {
		t.Errorf("params with a horizon: %s", got)
	}
	// another model's results (no purchase model) add nothing, and keep their checks' `value` out
	other := []resultRow{{Section: "model", Stat: "horizon", Value: ptr(12.0)},
		{Section: "assumption", Level: ptr("ljung_box"), Stat: "value", Value: ptr(0.4)}}
	if p := customerLifetimeParams(other, map[string]any{}); len(p) != 0 {
		t.Errorf("another model's params: %v", p)
	}
	for _, m := range modelMetrics(other, time.Now()) {
		if strings.HasPrefix(m.Key, "assumption.") {
			t.Errorf("another model logs a check's value: %s", m.Key)
		}
	}
}

// A customer lifetime card's purchase model is read for the version's tag.
func TestACustomerLifetimeCardsPurchaseModelIsRead(t *testing.T) {
	c := parseCardFile([]byte(`{"variant": "value", "customer_lifetime": {"node": "clv", "purchase_model": "bg_nbd"}}`))
	if c.variant != "value" || c.purchaseModel != "bg_nbd" {
		t.Errorf("%+v", c)
	}
	if c := parseCardFile([]byte(`{"variant": "unpenalized"}`)); c.purchaseModel != "" {
		t.Errorf("a card without one: %+v", c)
	}
}

// A run of a customer lifetime model registers its version with the tags Predict's form reads:
// the variant and the purchase model the fit used.
func TestACustomerLifetimeRunRegistersItsVersionWithTheVariantAndThePurchaseModel(t *testing.T) {
	w := newMLflowWorld(t)
	w.files["model.json"] = []byte(`{"format": "tabularia-model", "format_version": 1, "model": "clv", "variant": "value", "group": "customer_lifetime",
		"customer_lifetime": {"node": "clv", "purchase_model": "mbg_nbd", "horizon": 52}}`)
	w.rows = `{"rows": [
		{"section": "model", "level": null, "level_2": null, "stat": "purchase_model", "value": null, "text": "mbg_nbd"},
		{"section": "model", "level": null, "level_2": null, "stat": "horizon", "value": 52, "text": null},
		{"section": "model", "level": null, "level_2": null, "stat": "clv_total", "value": 97108.4, "text": null},
		{"section": "candidate", "level": "bg_nbd", "level_2": null, "stat": "log_lik", "value": -9582.43, "text": null}]}`
	id, err := w.runWith(w.block(`, "registerAs": "customers"`))
	if err != nil {
		t.Fatal(err)
	}
	if r := w.row(id); r.status != "SUCCESS" {
		t.Fatalf("%+v", r)
	}
	run := w.ml.OnlyRun()
	if run.Params["purchase_model_chosen"] != "mbg_nbd" || run.Params["horizon"] != "52" ||
		run.Metrics["clv_total"] != 97108.4 || run.Metrics["candidate.bg_nbd.log_lik"] != -9582.43 {
		t.Errorf("params %v, metrics %v", run.Params, run.Metrics)
	}
	versions := w.ml.Models["customers"]
	if len(versions) != 1 || versions[0].Tags["tabularia.variant"] != "value" || versions[0].Tags["tabularia.purchase_model"] != "mbg_nbd" {
		t.Errorf("versions: %+v", versions)
	}
}

func hasMetricPrefix(m map[string]float64, prefix string) bool {
	for k := range m {
		if strings.HasPrefix(k, prefix) {
			return true
		}
	}
	return false
}
