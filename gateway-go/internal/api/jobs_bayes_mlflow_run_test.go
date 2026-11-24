package api

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"sort"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn/mlflowtest"
)

// A Bayesian model node's flow run in MLflow (docs/specs/bayes-mlflow.md, lot M1) on TEST_DATABASE_URL, with the Bayesian
// step's world (the fake cluster, compiler and clock) and the models' fake MLflow and engine: the run's content (the tags,
// the parameters, the metrics, exactly five files, the version), never a draw and never results.parquet whatever the
// output, the program checked against the card, a step without a card, the failures and «Continue without MLflow», a
// failed sampling as a FAILED run, a trial and the prior predictive check logging nothing, a connection the run's authority
// may not use refused before the compile is asked; the node's fingerprint never moved by its MLflow block.

// bayesMLflowWorld is the Bayesian step's world with an MLflow connection in the flows' folder, the fake tracking server and
// an engine that also gives the step's files and the report spec's fixture rows back.
type bayesMLflowWorld struct {
	*bayesRunWorld
	ml    *mlflowWorld
	saved *bayesmodels.Saved
	rows  []resultRow // the fixture's rows, every section (the engine gives back those asked)
	mu    sync.Mutex
	reads []string // the bodies of the read-backs of the results
}

func (w *bayesMLflowWorld) asked() []string {
	w.mu.Lock()
	defer w.mu.Unlock()
	return append([]string{}, w.reads...)
}

func newBayesMLflowWorld(t *testing.T) *bayesMLflowWorld {
	t.Helper()
	bw := newBayesRunWorld(t)
	f := bw.modelRunFixture
	w := &bayesMLflowWorld{bayesRunWorld: bw, saved: bw.save("Schools", templateGraph(t, "hier_slopes")), rows: bayesFixtureRows(t, "sample")}
	ml := &mlflowWorld{modelRunFixture: f, ml: mlflowtest.New(t)}
	w.ml = ml
	all, err := json.Marshal(map[string]any{"rows": w.rows})
	if err != nil {
		t.Fatal(err)
	}
	ml.rows = string(all)
	ml.files = map[string][]byte{
		"results.parquet": []byte("PAR1 bayes results PAR1"),
		"output.parquet":  []byte("PAR1 bayes draws PAR1"),
		"model.json":      w.card(""),
		"MLmodel":         []byte("artifact_path: model\nflavors:\n  tabularia:\n    card: model.json\n"),
	}
	ml.ml.Authorization = "Basic " + basicOf("ann", mlflowSecret)
	srv := httptest.NewServer(http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		if r.URL.Path == "/scripts/results/rows" {
			b, _ := io.ReadAll(r.Body)
			w.mu.Lock()
			w.reads = append(w.reads, string(b))
			w.mu.Unlock()
			r.Body = io.NopCloser(bytes.NewReader(b))
		}
		ml.serveEngine(rw, r)
	}))
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
	ml.conn = ml.connection(f.folder, "Tracking", enc)
	prevTimeout, prevPoll := mlflowCallTimeout, mlflowReportPoll
	mlflowCallTimeout, mlflowReportPoll = 5*time.Second, 10*time.Millisecond
	t.Cleanup(func() {
		mlflowCallTimeout, mlflowReportPoll = prevTimeout, prevPoll
		_, _ = f.db.Exec(f.ctx, `DELETE FROM connections WHERE project_id = $1`, f.folder)
	})
	return w
}

// card is the model.json the runner would write for the saved model's version 1 (the `bayes` object as bayes_card.py
// writes it); programSHA "" is the version's own.
func (w *bayesMLflowWorld) card(programSHA string) []byte {
	c := w.saved.Content
	if programSHA == "" {
		programSHA = c.ProgramSHA256
	}
	return []byte(fmt.Sprintf(`{"format": "tabularia-model", "format_version": 1, "model": "bayes_model", "variant": "sample", "group": "bayes",
		"bayes": {"model": {"id": %d, "version": 1, "semantic_hash": %q, "codegen_version": %d},
		"program": {"sha256": %q, "bytes": %d}, "binary": {"sha256": %q, "cmdstan": "2.40.0", "image": "sha256:dd"},
		"mapping": [], "data": [], "units": {"school": {"count": 6, "labels": ["Liceo Galilei"]}},
		"sampler": {"algorithm": "nuts", "chains": 4, "warmup": 1000, "draws": 1000, "seed": 1840277361, "adapt_delta": 0.8, "max_treedepth": 10},
		"facts": {"divergences": 2, "treedepth_hits": 0, "e_bfmi_min": 0.9}, "summary": []}}`,
		w.saved.Model.ID, c.SemanticHash, c.CodegenVersion, programSHA, len(c.Program), fakeBinarySHA))
}

// node is the Bayesian node with its MLflow block (extra: more keys of the block).
func (w *bayesMLflowWorld) node(extra string, more map[string]any) string {
	var block any
	if err := json.Unmarshal([]byte(fmt.Sprintf(`{"connectionId": %d, "experiment": "Schools/slopes"%s}`, w.ml.conn, extra)), &block); err != nil {
		w.t.Fatal(err)
	}
	data := map[string]any{"mlflow": block}
	for k, v := range more {
		data[k] = v
	}
	return bayesNodeJSON(w.saved.Model.ID, 1, slopesBindings, data)
}

// artifactsOf are the files of the run, by their path in it.
func artifactsOf(ml *mlflowtest.Server, run *mlflowtest.Run) map[string][]byte {
	dir := "1/" + run.ID + "/artifacts/"
	out := map[string][]byte{}
	for k, v := range ml.Artifacts {
		if after, ok := strings.CutPrefix(k, dir); ok {
			out[after] = v
		}
	}
	return out
}

func keysOf[V any](m map[string]V) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// The content of a Bayesian run: the common tags and the saved model's, its parameters, the summary's and the sampler's
// metrics, exactly the five files, the registered version with its tags; the Job told to write MLmodel and nothing of
// MLflow's; the run page's record.
func TestABayesFlowRunIsRecordedInMLflow(t *testing.T) {
	w := newBayesMLflowWorld(t)
	_, id, err := w.step(w.ann, w.node(`, "registerAs": "school-slopes"`, nil), false, "manual")
	if err != nil {
		t.Fatal(err)
	}
	if r := w.row(id); r.status != "SUCCESS" {
		t.Fatalf("%+v", r)
	}
	ml := w.ml.ml
	run := ml.OnlyRun()
	if run == nil || run.Status != "FINISHED" || !strings.HasPrefix(run.Name, "Schools · School scores · ") {
		t.Fatalf("run: %+v", run)
	}
	// the tags: the common ones and the saved model's; no model file
	for k, v := range map[string]string{"tabularia.model": "bayes_model", "tabularia.group": "bayes", "tabularia.group_version": "5",
		"tabularia.node_id": "b", "tabularia.node_name": "School scores", "tabularia.step_run_id": fmt.Sprint(id),
		"tabularia.launched_by": w.ann.Email, "tabularia.bayes_model_id": fmt.Sprint(w.saved.Model.ID), "tabularia.bayes_model_version": "1",
		"tabularia.bayes_model_name": "Schools", "tabularia.bayes_semantic_hash": w.saved.Content.SemanticHash,
		"tabularia.program_sha256": w.saved.Content.ProgramSHA256, "tabularia.binary_sha256": fakeBinarySHA, "tabularia.cmdstan": "2.40.0",
		"tabularia.sampler": "nuts"} {
		if run.Tags[k] != v {
			t.Errorf("tag %s = %q, want %q", k, run.Tags[k], v)
		}
	}
	if _, ok := run.Tags["tabularia.model_file_sha256"]; ok {
		t.Error("a model file's hash on the run")
	}
	// the parameters: the node's and its version's, the bindings; nothing of the generic models'
	for k, v := range map[string]string{"model.id": fmt.Sprint(w.saved.Model.ID), "model.version": "1", "sampler.chains": "4",
		"sampler.warmup": "1000", "sampler.draws": "1000", "sampler.seed": "1840277361", "sampler.adapt_delta": "0.8",
		"sampler.max_treedepth": "10", "sampler.init": "default", "output": "summary", "data.hours.column": "hours_studied"} {
		if run.Params[k] != v {
			t.Errorf("param %s = %q, want %q", k, run.Params[k], v)
		}
	}
	if _, ok := run.Params["mode"]; ok {
		t.Errorf("the catalogue's parameters were logged: %v", run.Params)
	}
	// the metrics: the summary, the sampler's facts, the counts; never a parameter, a draw or a chart's bin
	want, _ := bayesMetrics(w.rows, time.Now())
	if len(run.Metrics) != len(want) || len(want) == 0 {
		t.Errorf("%d metrics, the pure function gives %d", len(run.Metrics), len(want))
	}
	for k := range run.Metrics {
		if len(k) > mlflowconn.MaxKeyLength || metricKey(k) != k {
			t.Errorf("metric key %q", k)
		}
		top, _, _ := strings.Cut(k, ".")
		if top != "summary" && top != "sampler" && top != "run" && top != "quantity" {
			t.Errorf("metric %s", k)
		}
	}
	for _, k := range []string{"sampler.divergences", "run.n"} {
		if _, ok := run.Metrics[k]; !ok {
			t.Errorf("no metric %s: %v", k, keysOf(run.Metrics))
		}
	}
	// exactly five files: the summary, the report, the model's folder with the program; never results.parquet
	files := artifactsOf(ml, run)
	if got := strings.Join(keysOf(files), " "); got != "model/MLmodel model/model.json model/program.stan tabularia/report.pdf tabularia/summary.csv" {
		t.Errorf("the files: %s", got)
	}
	if string(files["model/program.stan"]) != w.saved.Content.Program || string(files["model/model.json"]) != string(w.ml.files["model.json"]) ||
		!strings.HasSuffix(string(files["model/MLmodel"]), "run_id: "+run.ID+"\n") ||
		string(files["tabularia/summary.csv"]) != string(bayesSummaryCSV(w.rows)) || string(files["tabularia/report.pdf"]) != "%PDF-1.7 report" {
		t.Errorf("the files' content: %v", keysOf(files))
	}
	// the version: the common tags, the variant, the card's hash and the saved model's; no model file
	sum := sha256.Sum256(w.ml.files["model.json"])
	versions := ml.Models["school-slopes"]
	if len(versions) != 1 || versions[0].Source != "mlflow-artifacts:/1/"+run.ID+"/artifacts/model" || versions[0].RunID != run.ID {
		t.Fatalf("versions: %+v", versions)
	}
	vt := versions[0].Tags
	for k, v := range map[string]string{"tabularia.model": "bayes_model", "tabularia.group": "bayes", "tabularia.group_version": "5",
		"tabularia.variant": "sample", "tabularia.card_sha256": hex.EncodeToString(sum[:]),
		"tabularia.bayes_model_id": fmt.Sprint(w.saved.Model.ID), "tabularia.bayes_model_version": "1", "tabularia.bayes_model_name": "Schools",
		"tabularia.bayes_semantic_hash": w.saved.Content.SemanticHash} {
		if vt[k] != v {
			t.Errorf("version tag %s = %q, want %q", k, vt[k], v)
		}
	}
	if _, ok := vt["tabularia.model_file_sha256"]; ok {
		t.Error("a model file's hash on the version")
	}
	// the run page's record
	c, m := w.ml.columns(id)
	if deref(c.run) != run.ID || deref(c.model) != "school-slopes" || deref(c.version) != "1" || c.errText != nil ||
		m["artifacts"] != true || m["model_version"] != "1" || len(m["warnings"].([]any)) != 0 {
		t.Errorf("the run's record: %+v %v", c, m)
	}
	// the Job: told to write MLmodel, holding nothing of MLflow's
	a := w.attempts[len(w.attempts)-1]
	task, _ := bayesTaskOf(t, a)
	if !task.MLflow {
		t.Error("task.json's mlflow is false")
	}
	everything := pyDumps(a.manifest) + pyDumps(a.code) + pyDumps(a.secret)
	for _, bad := range []string{mlflowSecret, basicOf("ann", mlflowSecret), ml.URL, "MLFLOW"} {
		if strings.Contains(everything, bad) {
			t.Errorf("the Job holds %q", bad)
		}
	}
	// the read-back, before the step's files left the bucket
	if paths := w.ml.enginePaths(); !strings.Contains(paths, "/scripts/results/rows") || !strings.HasSuffix(paths, "/scripts/objects/delete") {
		t.Errorf("engine: %s", paths)
	}
}

// No draw, ever: whatever the output (the summary, the draws), the files are the same five, the read-back
// asks for the five sections alone, and no value of a curve row (the thinned traces, the densities) is in a file or a metric.
func TestNoDrawOfABayesianRunReachesMLflow(t *testing.T) {
	w := newBayesMLflowWorld(t)
	var drawn []string
	for _, r := range w.rows {
		if r.Section == "curve" && r.Value != nil {
			if s := strconv.FormatFloat(*r.Value, 'g', -1, 64); len(s) >= 9 {
				drawn = append(drawn, s)
			}
		}
	}
	if len(drawn) < 100 {
		t.Fatalf("the fixture has %d long curve values", len(drawn))
	}
	ml := w.ml.ml
	// the replicated data are not an output of this model (the slopes model has correlated effects: the 8 digits of the draws' CSV
	// break their Cholesky factor when Stan reads the draws back, the owner's decision of 2026-11-24): refused before any run
	if _, _, err := w.step(w.ann, w.node("", map[string]any{"output": "replicated"}), true, "manual"); err == nil ||
		!strings.Contains(err.Error(), "the replicated data are not available for this model") || len(ml.Runs) != 0 {
		t.Fatalf("the replicated data of the slopes model: %v (%d runs)", err, len(ml.Runs))
	}
	for i, output := range []string{"summary", "draws"} {
		_, id, err := w.step(w.ann, w.node("", map[string]any{"output": output}), true, "manual")
		if err != nil {
			t.Fatalf("%s: %v", output, err)
		}
		if r := w.row(id); r.status != "SUCCESS" {
			t.Fatalf("%s: %+v", output, r)
		}
		var run *mlflowtest.Run
		for _, r := range ml.Runs {
			if r.Tags["tabularia.step_run_id"] == fmt.Sprint(id) {
				run = r
			}
		}
		if run == nil || len(ml.Runs) != i+1 {
			t.Fatalf("%s: no run of its own (%d runs)", output, len(ml.Runs))
		}
		files := artifactsOf(ml, run)
		if got := strings.Join(keysOf(files), " "); got != "model/MLmodel model/model.json model/program.stan tabularia/report.pdf tabularia/summary.csv" {
			t.Errorf("%s: the files: %s", output, got)
		}
		var all strings.Builder
		for _, b := range files {
			all.Write(b)
		}
		for k, v := range run.Metrics {
			all.WriteString(k + "=" + strconv.FormatFloat(v, 'g', -1, 64) + "\n")
		}
		for k, v := range run.Params {
			all.WriteString(k + "=" + v + "\n")
		}
		text := all.String()
		for _, d := range drawn {
			if strings.Contains(text, d) {
				t.Errorf("%s: the drawn value %s is in MLflow", output, d)
				break
			}
		}
		if strings.Contains(text, "PAR1") {
			t.Errorf("%s: a parquet file in MLflow", output)
		}
	}
	// the sections the read-back asked for, every time: the five, never a chart's
	asked := w.asked()
	if len(asked) != 2 {
		t.Fatalf("%d read-backs", len(asked))
	}
	for _, body := range asked {
		var in struct {
			Sections []string `json:"sections"`
		}
		if err := json.Unmarshal([]byte(body), &in); err != nil || strings.Join(in.Sections, ",") != "summary,sampler,quantity,model,data" {
			t.Errorf("the read-back asked for %s (%v)", body, err)
		}
	}
}

// The program uploaded is the version's: a card that names another program fails the step as a fault of Tabularia, with or
// without «Continue without MLflow», before anything reaches MLflow (no experiment, no run, no file).
func TestABayesProgramThatIsNotTheCardsFailsTheStepAndLogsNothing(t *testing.T) {
	w := newBayesMLflowWorld(t)
	w.ml.files["model.json"] = w.card(strings.Repeat("e", 64))
	for _, extra := range []string{"", `, "continueWithout": true`} {
		_, id, err := w.step(w.ann, w.node(extra, nil), false, "manual")
		if err == nil || !strings.HasPrefix(err.Error(), "Bayesian model «School scores»: the model was fitted but MLflow could not record it: "+
			"the program stored for this version of the model is not the one the run's card names") ||
			!strings.Contains(err.Error(), "a fault of Tabularia, nothing was logged") {
			t.Fatalf("%q: %v", extra, err)
		}
		if r := w.row(id); r.status != "FAILURE" {
			t.Errorf("%q: %+v", extra, r)
		}
		if _, m := w.ml.columns(id); m["continued"] != false || m["failed_step"] != nil {
			t.Errorf("%q: the record %v", extra, m)
		}
	}
	if calls := w.ml.ml.Paths(); len(calls) != 0 {
		t.Errorf("MLflow was called: %v", calls)
	}
}

// A run whose card was too large (the runner wrote none): the parameters, the metrics, the summary and the report, no model
// folder, nothing registered, and the run page says why.
func TestABayesRunWithoutACardLogsTheSummaryAndRegistersNothing(t *testing.T) {
	w := newBayesMLflowWorld(t)
	delete(w.ml.files, "model.json")
	delete(w.ml.files, "MLmodel")
	_, id, err := w.step(w.ann, w.node(`, "registerAs": "school-slopes"`, nil), false, "manual")
	if err != nil {
		t.Fatal(err)
	}
	ml := w.ml.ml
	run := ml.OnlyRun()
	if run == nil || run.Status != "FINISHED" || len(run.Params) == 0 || len(run.Metrics) == 0 || len(ml.Models) != 0 {
		t.Fatalf("run: %+v, %d models", run, len(ml.Models))
	}
	if got := strings.Join(keysOf(artifactsOf(ml, run)), " "); got != "tabularia/report.pdf tabularia/summary.csv" {
		t.Errorf("the files: %s", got)
	}
	if run.Tags["tabularia.bayes_model_name"] != "Schools" || run.Tags["tabularia.program_sha256"] != "" {
		t.Errorf("the tags without a card: %v", run.Tags)
	}
	_, m := w.ml.columns(id)
	warnings := fmt.Sprint(m["warnings"])
	if !strings.Contains(warnings, "The runner wrote no model card") || !strings.Contains(warnings, "was not registered as «school-slopes»") ||
		w.row(id).status != "SUCCESS" {
		t.Errorf("the record: %v", m)
	}
}

// The failures, as the models': credentials refused fail the step with the client's sentence and never the server's text;
// «Continue without MLflow» makes it a warning; a server without the proxy logs what it can.
func TestABayesStepWhenMLflowFails(t *testing.T) {
	w := newBayesMLflowWorld(t)
	ml := w.ml.ml
	ml.Authorization = "Basic " + basicOf("ann", "another")
	_, id, err := w.step(w.ann, w.node("", nil), false, "manual")
	want := "Bayesian model «School scores»: the model was fitted but MLflow could not record it: the tracking server refused the connection's credentials (401)"
	if err == nil || !strings.HasPrefix(err.Error(), want) {
		t.Fatalf("401: %v", err)
	}
	r := w.row(id)
	if r.status != "FAILURE" || r.resultsKey == "" {
		t.Errorf("%+v", r)
	}
	for _, bad := range []string{mlflowSecret, mlflowtest.Leak} {
		if strings.Contains(r.errText+r.script, bad) {
			t.Errorf("%q in the run", bad)
		}
	}
	if len(ml.Runs) != 0 {
		t.Errorf("a FAILED run for a step whose MLflow failed: %d runs", len(ml.Runs))
	}
	// with «Continue without MLflow»: the step succeeds and says why
	_, id, err = w.step(w.ann, w.node(`, "continueWithout": true`, nil), false, "manual")
	if err != nil {
		t.Fatal(err)
	}
	if _, m := w.ml.columns(id); w.row(id).status != "SUCCESS" || m["continued"] != true || !strings.Contains(fmt.Sprint(m["error"]), "(401)") {
		t.Errorf("continued: %v", m)
	}
	// no artifact proxy: parameters, metrics and tags, no file, nothing registered
	ml.Authorization = ""
	ml.NoProxy = true
	_, id, err = w.step(w.ann, w.node(`, "registerAs": "school-slopes"`, nil), false, "manual")
	if err != nil {
		t.Fatal(err)
	}
	run := ml.OnlyRun()
	if run == nil || run.Status != "FINISHED" || len(run.Metrics) == 0 || len(ml.Artifacts) != 0 || len(ml.Models) != 0 {
		t.Fatalf("no proxy: %+v, %d files", run, len(ml.Artifacts))
	}
	if _, m := w.ml.columns(id); m["artifacts"] != false || !strings.Contains(fmt.Sprint(m["warnings"]), "no artifact proxy") {
		t.Errorf("no proxy: %v", m)
	}
}

// A sampling Job that fails is a FAILED run with the error, the node's parameters and the saved model's name; a step stopped
// before its Job (the group's row limit) logs nothing.
func TestAFailedBayesSamplingIsAFailedRun(t *testing.T) {
	w := newBayesMLflowWorld(t)
	w.script = []modelAttemptResult{{log: "Stan: error\n", exit: int64(1), reason: "it ended with exit code 1"}}
	_, id, err := w.step(w.ann, w.node("", nil), false, "manual")
	if err == nil || err.Error() != "Bayesian model «School scores» failed: it ended with exit code 1" {
		t.Fatalf("%v", err)
	}
	run := w.ml.ml.OnlyRun()
	if run == nil || run.Status != "FAILED" || run.Tags["tabularia.error"] != err.Error() || run.Params["sampler.seed"] != "1840277361" ||
		run.Params["model.id"] != fmt.Sprint(w.saved.Model.ID) || run.Tags["tabularia.bayes_model_name"] != "Schools" || len(run.Metrics) != 0 {
		t.Fatalf("%+v", run)
	}
	if c, m := w.ml.columns(id); m["failed_step"] != true || deref(c.run) != run.ID {
		t.Errorf("%v %+v", m, c)
	}
	if len(artifactsOf(w.ml.ml, run)) != 0 {
		t.Error("a failed step uploaded files")
	}
	w.script = nil
	if _, err := w.db.Exec(w.ctx, `UPDATE model_groups SET max_rows = 50 WHERE id = 'bayes'`); err != nil {
		t.Fatal(err)
	}
	if _, _, err := w.step(w.ann, w.node("", nil), false, "manual"); err == nil || len(w.ml.ml.Runs) != 1 {
		t.Errorf("before the Job: %v, %d runs", err, len(w.ml.ml.Runs))
	}
}

// A trial logs nothing, the prior predictive check included, and does not even read the MLflow block.
func TestABayesTrialAndThePriorCheckLogNothing(t *testing.T) {
	w := newBayesMLflowWorld(t)
	for _, node := range []string{w.node(`, "registerAs": "school-slopes"`, nil),
		bayesNodeJSON(w.saved.Model.ID, 1, slopesBindings, map[string]any{"mlflow": map[string]any{"connectionId": 999999, "experiment": ""}})} {
		flow, _, _ := w.flowWith(node, true)
		for _, body := range []string{`{}`, `{"check": "prior_predictive"}`} {
			before := len(w.attempts)
			rec := w.trialWith(w.ann, flow.ID, "b", body)
			if rec.Code != http.StatusAccepted {
				t.Fatalf("%s: %d %s", body, rec.Code, rec.Body)
			}
			var started struct {
				RunID int64 `json:"run_id"`
			}
			_ = json.Unmarshal(rec.Body.Bytes(), &started)
			if r := w.waitRun(started.RunID); r.status != "SUCCESS" {
				t.Fatalf("%s: %+v", body, r)
			}
			if len(w.attempts) != before+1 {
				t.Fatalf("%s: %d attempts", body, len(w.attempts)-before)
			}
			if task, _ := bayesTaskOf(t, w.attempts[len(w.attempts)-1]); task.MLflow {
				t.Errorf("%s: a trial's task.json asks for MLflow's files", body)
			}
			if _, m := w.ml.columns(started.RunID); m != nil {
				t.Errorf("%s: a trial's record: %v", body, m)
			}
		}
	}
	if calls := w.ml.ml.Paths(); len(calls) != 0 {
		t.Errorf("a trial called MLflow: %v", calls)
	}
}

// A connection the run's authority may not use (RUN on the flows' folder, no CONNECT) stops the step before the compile is
// even asked: no run, no Job, no call to MLflow.
func TestABayesConnectionThePersonMayNotUseStopsTheStepBeforeTheCompile(t *testing.T) {
	w := newBayesMLflowWorld(t)
	_, _, err := w.step(w.runner, w.node("", nil), false, "manual")
	if err == nil || err.Error() != fmt.Sprintf("Bayesian model «School scores»: %s may not use the MLflow connection #%d: it needs CONNECT on its folder",
		w.runner.Email, w.ml.conn) {
		t.Fatalf("%v", err)
	}
	_, _, err = w.step(w.ann, bayesNodeJSON(w.saved.Model.ID, 1, slopesBindings, map[string]any{"mlflow": map[string]any{"connectionId": w.ml.conn,
		"experiment": " "}}), false, "manual")
	if err == nil || !strings.Contains(err.Error(), "Bayesian model «School scores»: name the MLflow experiment") {
		t.Errorf("no experiment: %v", err)
	}
	if w.binaries.needCount() != 0 || len(w.attempts) != 0 || len(w.ml.ml.Paths()) != 0 {
		t.Errorf("a refusal went on: %d compiles asked, %d attempts, %v", w.binaries.needCount(), len(w.attempts), w.ml.ml.Paths())
	}
	var n int64
	_ = w.db.QueryRow(w.ctx, `SELECT count(*) FROM runs WHERE launched_by = ANY($1)`, []int64{w.runner.ID, w.ann.ID}).Scan(&n)
	if n != 0 {
		t.Errorf("%d runs created by refusals", n)
	}
}

// The MLflow block never makes the node «not up to date»: it does not change the numbers.
func TestABayesNodesMLflowBlockLeavesItsFingerprint(t *testing.T) {
	fp := func(node string) string {
		d := mustDef(t, `{"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": 1}}, `+node+`], "edges": [{"source": "s", "target": "b"}]}`)
		for _, n := range objectList(d.Value("nodes")) {
			if n.Value("id") == "b" {
				return bayesFingerprint(d, n)
			}
		}
		t.Fatal("no node")
		return ""
	}
	base := fp(bayesNodeJSON(5, 2, slopesBindings, nil))
	with := fp(bayesNodeJSON(5, 2, slopesBindings, map[string]any{"mlflow": map[string]any{"connectionId": 3, "experiment": "x",
		"registerAs": "y", "continueWithout": true}}))
	if base != with || base == "" {
		t.Errorf("the MLflow block changed the fingerprint")
	}
}
