package api

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/flowresolver"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn/mlflowtest"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// A Predict node (docs/specs/statistical-models.md, «Predict», lot H): the form's lists of
// experiments and versions from a fake MLflow server; the step — the version named or «latest»,
// the card's hash, the group, the options, the columns — and its Job in predict mode with the
// card in the scripts bucket and nothing of MLflow in the pod; «not up to date» for «latest».

// ── without a database ───────────────────────────────────────────────────────

func TestThePredictRoutesAreRegisteredAndGoOnly(t *testing.T) {
	want := [][2]string{{"GET", "/models/mlflow/{connection_id}/experiments"}, {"GET", "/models/mlflow/{connection_id}/versions"}}
	have := map[[2]string]bool{}
	for _, r := range Routes(&Deps{Config: &config.Config{}}) {
		have[[2]string{r.Method, r.Pattern}] = true
	}
	goOnly := map[[2]string]bool{}
	for _, r := range server.GoOnlyRoutes() {
		goOnly[r] = true
	}
	for _, r := range want {
		if !have[r] || !goOnly[r] {
			t.Errorf("%v: registered %v, Go-only %v", r, have[r], goOnly[r])
		}
	}
}

func TestTheTabulariaFlavorOfAnMLmodel(t *testing.T) {
	for text, want := range map[string]bool{
		"artifact_path: model\nflavors:\n  tabularia:\n    card: model.json\nsignature:\n  inputs: '[]'\n":   true,
		"flavors:\r\n  python_function:\r\n    loader_module: x\r\n  tabularia:\r\n    card: model.json\r\n": true,
		"flavors:\n  python_function:\n    loader_module: tabularia\n":                                       false,
		"signature:\n  tabularia:\n    card: model.json\n":                                                   false,
		"flavors:\n    tabularia:\n": false,
		"":                           false,
	} {
		if hasTabulariaFlavor([]byte(text)) != want {
			t.Errorf("%q: want %v", text, want)
		}
	}
}

func TestACardThisTabulariaApplies(t *testing.T) {
	good := predictCard{Format: "tabularia-model", FormatVersion: 1, Model: "linear", Variant: "unpenalized", Group: "regression"}
	if why := good.incompatible("linear", "regression"); why != "" {
		t.Fatal(why)
	}
	for what, c := range map[string]predictCard{
		"another format":  {Format: "pickle", FormatVersion: 1, Model: "linear", Variant: "unpenalized", Group: "regression"},
		"a newer format":  {Format: "tabularia-model", FormatVersion: 2, Model: "linear", Variant: "unpenalized", Group: "regression"},
		"another model":   {Format: "tabularia-model", FormatVersion: 1, Model: "logistic", Variant: "unpenalized", Group: "regression"},
		"another group":   {Format: "tabularia-model", FormatVersion: 1, Model: "linear", Variant: "unpenalized", Group: "survival"},
		"unknown variant": {Format: "tabularia-model", FormatVersion: 1, Model: "linear", Variant: "bayesian", Group: "regression"},
	} {
		if c.incompatible("linear", "regression") == "" {
			t.Errorf("%s: applied", what)
		}
	}
	if why := (&predictCard{Format: "tabularia-model", FormatVersion: 2}).incompatible("linear", "regression"); !strings.Contains(why,
		"format version 2, which this version of Tabularia does not read (it reads 1)") {
		t.Errorf("%s", why)
	}
}

func TestAPredictsFingerprint(t *testing.T) {
	base := `{"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": 1}},
		{"id": "p", "type": "model_predict", "data": {"label": "Next month", "connectionId": 4, "experiment": "Pasta",
			"registeredModel": "price", "version": "latest", "params": {"interval": true}, "timeoutSeconds": 900}}],
		"edges": [{"source": "s", "target": "p"}]}`
	fp := func(text string, known int64) string {
		d := mustDef(t, text)
		return predictFingerprint(d, objectList(d.Value("nodes"))[1], known)
	}
	ref := fp(base, 7)
	for what, text := range map[string]string{
		"a name":         strings.Replace(base, `"Next month"`, `"Later"`, 1),
		"the timeout":    strings.Replace(base, `"timeoutSeconds": 900`, `"timeoutSeconds": 60`, 1),
		"the experiment": strings.Replace(base, `"experiment": "Pasta"`, `"experiment": "Other"`, 1),
	} {
		if fp(text, 7) != ref {
			t.Errorf("%s changed the fingerprint", what)
		}
	}
	if fp(base, 8) == ref {
		t.Error("a newer version of «latest» did not change it")
	}
	for what, text := range map[string]string{
		"an option":   strings.Replace(base, `"interval": true`, `"interval": false`, 1),
		"the version": strings.Replace(base, `"version": "latest"`, `"version": "7"`, 1),
		"the model":   strings.Replace(base, `"registeredModel": "price"`, `"registeredModel": "churn"`, 1),
		"the source":  strings.Replace(base, `"datasourceId": 1`, `"datasourceId": 2`, 1),
	} {
		if fp(text, 7) == ref {
			t.Errorf("%s did not change the fingerprint", what)
		}
	}
	pinned := strings.Replace(base, `"version": "latest"`, `"version": "7"`, 1)
	if fp(pinned, 7) != fp(pinned, 9) {
		t.Error("a version named does not depend on what «latest» is")
	}
}

// ── on a real database, with a fake MLflow server ────────────────────────────

// predictWorld is the MLflow world of lot G with versions registered and a Predict node.
type predictWorld struct {
	*mlflowWorld
	card []byte
}

const predictCardText = `{"format": "tabularia-model", "format_version": 1, "model": "linear", "variant": "unpenalized", "group": "regression",
 "group_version": 3, "params": {"outcome": "quantity", "predictors": ["price", "channel"]},
 "inputs": [{"name": "price", "type": "number"}, {"name": "channel", "type": "category", "levels": ["shop", "web"]}],
 "coefficients": [10.5, -1.5, 2.0], "sigma": 1.2, "df_residual": 117}`

const predictMLmodel = "artifact_path: model\nflavors:\n  tabularia:\n    card: model.json\n    format: tabularia-model\n    format_version: 1\nrun_id: run1\n"

func shaOf(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

func newPredictWorld(t *testing.T) *predictWorld {
	w := &predictWorld{mlflowWorld: newMLflowWorld(t), card: []byte(predictCardText)}
	w.files["run.json"] = []byte(`{"warnings": ["3 rows hold a value the model did not see when fitted: their prediction is empty"]}`)
	return w
}

// register adds a version Tabularia registered, from a run of the experiment.
func (w *predictWorld) register(experiment, model string, card []byte, tags map[string]string) string {
	runTags := map[string]string{"tabularia.model": "linear", "tabularia.flow_name": "Pricing", "tabularia.node_name": "Elasticity"}
	vtags := map[string]string{"tabularia.model": "linear", "tabularia.group": "regression", "tabularia.group_version": "3",
		"tabularia.card_sha256": shaOf(card)}
	for k, v := range tags {
		if v == "" {
			delete(vtags, k)
		} else {
			vtags[k] = v
		}
	}
	_, version := w.ml.Register(experiment, model, runTags, map[string]float64{"r_squared": 0.8, "adj_r_squared": 0.79},
		map[string][]byte{"model.json": card, "MLmodel": []byte(predictMLmodel), "native.rds": []byte("RDS")}, vtags)
	return version
}

// predictFlow saves source → Predict (→ output) and gives its run.
func (w *predictWorld) predictFlow(data string) *scriptNodeRun {
	w.t.Helper()
	text := `{"nodes": [{"id": "s", "type": "source", "data": {"datasourceId": 1}},
		{"id": "p", "type": "model_predict", "data": {"label": "Next month", "connectionId": ` + fmt.Sprint(w.conn) + `,
		"experiment": "Pasta/price elasticity", ` + data + `, "timeoutSeconds": 600, "retries": 0}},
		{"id": "o", "type": "output", "data": {"name": "predicted"}}],
		"edges": [{"source": "s", "target": "p"}, {"source": "p", "target": "o"}]}`
	var id int64
	if err := w.db.QueryRow(w.ctx, `INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
		VALUES ('Forecast', '', $1, $2, $3, 'duckdb', now(), now()) RETURNING id`, w.folder, w.ann.ID, text).Scan(&id); err != nil {
		w.t.Fatal(err)
	}
	definition := mustDef(w.t, text)
	return &scriptNodeRun{q: w.q, user: w.ann, flow: &jobFlow{ID: id, ProjectID: w.folder, Name: "Forecast", Definition: text, Engine: "duckdb"},
		definition: definition, node: objectList(definition.Value("nodes"))[1], trigger: "manual", mode: "development",
		outputs: flowresolver.ScriptOutputs{}}
}

func (w *predictWorld) predict(data string) (int64, *flowresolver.Source, error) {
	w.t.Helper()
	r := w.predictFlow(data)
	out, err := w.q.runPredict(w.ctx, r)
	var id int64
	_ = w.db.QueryRow(w.ctx, `SELECT coalesce(max(id), 0) FROM runs WHERE flow_id = $1 AND kind = 'model_predict'`, r.flow.ID).Scan(&id)
	return id, out, err
}

func (w *predictWorld) runsMade() int64 {
	var n int64
	_ = w.db.QueryRow(w.ctx, `SELECT count(*) FROM runs WHERE launched_by = $1 AND kind = 'model_predict'`, w.ann.ID).Scan(&n)
	return n
}

func TestAPredictStepAppliesTheVersionInItsGroupsJob(t *testing.T) {
	w := newPredictWorld(t)
	w.register("Pasta/price elasticity", "price-elasticity", w.card, nil)
	id, out, err := w.predict(`"registeredModel": "price-elasticity", "version": "1", "params": {"interval": true}`)
	if err != nil {
		t.Fatal(err)
	}
	r := w.row(id)
	if r.status != "SUCCESS" || out == nil || out.Key != r.outputKey || r.rows == nil || *r.rows != 120 {
		t.Fatalf("the run: %+v, out %+v", r, out)
	}
	c, ml := w.columns(id)
	if deref(c.model) != "price-elasticity" || deref(c.version) != "1" || deref(c.run) != "run1" {
		t.Errorf("the run's MLflow columns: %+v", c)
	}
	var info map[string]any
	_ = json.Unmarshal([]byte(r.script), &info)
	if info["model"] != "linear" || info["group"] != "regression" || info["version"] != float64(3) || info["asked_version"] != "1" ||
		info["model_version"] != "1" || info["card_sha256"] != shaOf(w.card) || info["card_group_version"] != float64(3) || info["trial"] != false {
		t.Errorf("the run's script: %s", r.script)
	}
	if ml["registered_model"] != "price-elasticity" || ml["model_url"] != "https://mlflow.example.test/#/models/price-elasticity/versions/1" ||
		!strings.Contains(fmt.Sprint(ml["warnings"]), "3 rows hold a value the model did not see") {
		t.Errorf("what the run page shows: %v", ml)
	}
	// MLflow was only read: the version and its two small files, never native.rds
	for _, p := range w.ml.Paths() {
		if !strings.HasPrefix(p, "GET ") || strings.Contains(p, "native.rds") {
			t.Errorf("a call that is not a read of the version or its card: %s", p)
		}
	}
	// the Job: predict mode, the card in the scripts bucket, nothing of MLflow
	if len(w.attempts) != 1 {
		t.Fatalf("%d attempts", len(w.attempts))
	}
	a := w.attempts[0]
	task := taskOf(t, a)
	if task.Mode != "predict" || task.Model != "linear" || task.Variant != "unpenalized" || task.GroupVersion != 3 || !task.WriteOutput ||
		task.Output != "rows" || fmt.Sprint(task.Params) != "map[interval:true]" || task.IDs.StepRunID != id || task.MLflow {
		t.Errorf("task.json: %+v", task)
	}
	manifest := pyDumps(a.manifest)
	env := map[string]string{}
	c0 := objectList(pyjson.Obj(pyjson.Obj(pyjson.Obj(pyjson.Obj(a.manifest.Value("spec")).Value("template")).Value("spec"))).Value("containers"))[0]
	for _, e := range c0.Value("env").([]any) {
		o := pyjson.Obj(e)
		env[pyjson.Str(o.Value("name"))] = pyjson.Str(o.Value("value"))
	}
	if !strings.HasPrefix(env[modelgroups.EnvModelIn], "s3://scripts/env-") || !strings.HasSuffix(env[modelgroups.EnvModelIn], "/model.json") ||
		!strings.HasSuffix(env[modelgroups.EnvOutput], "/output.parquet") || env[modelgroups.EnvResults] != "" {
		t.Errorf("the Job's environment: %v", env)
	}
	everything := manifest + pyDumps(a.code) + pyDumps(a.secret)
	for _, leak := range []string{mlflowSecret, "MLFLOW", w.ml.URL, "mlflow-artifacts"} {
		if strings.Contains(everything, leak) {
			t.Errorf("the Job holds %q", leak)
		}
	}
	job, _ := podLabels(t, a.manifest)
	if job.Value(labelInternet) != "off" || job.Value(labelModel) != "linear" {
		t.Errorf("labels %v", job)
	}
	// the engine wrote the card (the bytes MLflow keeps, checked), imported the rows, read the
	// runner's warnings and cleaned up
	var wrote string
	for _, call := range w.modelRunFixture.engine.calls {
		if strings.HasPrefix(call, "/scripts/objects/write ") {
			wrote = strings.TrimPrefix(call, "/scripts/objects/write ")
		}
	}
	var body struct {
		Key  string `json:"key"`
		Text string `json:"text"`
	}
	if err := json.Unmarshal([]byte(wrote), &body); err != nil || body.Text != predictCardText || "s3://scripts/"+body.Key != env[modelgroups.EnvModelIn] {
		t.Errorf("the card written: %v %s", err, wrote)
	}
	if got := w.enginePaths(); got != "/scripts/objects/write /scripts/output/import /scripts/objects/read /scripts/objects/delete" {
		t.Errorf("the engine was asked: %s", got)
	}
	var deleted struct {
		Keys []string `json:"keys"`
	}
	for _, call := range w.modelRunFixture.engine.calls {
		if rest, ok := strings.CutPrefix(call, "/scripts/objects/delete "); ok {
			_ = json.Unmarshal([]byte(rest), &deleted)
		}
	}
	if !strings.Contains(strings.Join(deleted.Keys, " "), "/model.json") {
		t.Errorf("the card is not deleted with the step: %v", deleted.Keys)
	}
}

func TestLatestIsTheNewestVersionTabulariaRegistered(t *testing.T) {
	w := newPredictWorld(t)
	w.register("Pasta/price elasticity", "price-elasticity", w.card, nil)
	w.register("Pasta/price elasticity", "price-elasticity", w.card, nil)
	// a version 3 someone registered by hand: not Tabularia's, not «latest»
	w.register("Pasta/price elasticity", "price-elasticity", w.card, map[string]string{"tabularia.model": ""})
	id, _, err := w.predict(`"registeredModel": "price-elasticity", "version": "latest", "params": {}`)
	if err != nil {
		t.Fatal(err)
	}
	r := w.row(id)
	var info map[string]any
	_ = json.Unmarshal([]byte(r.script), &info)
	if r.status != "SUCCESS" || info["asked_version"] != "latest" || info["model_version"] != "2" {
		t.Errorf("«latest» resolved to %v: %s", info["model_version"], r.script)
	}
	if c, _ := w.columns(id); deref(c.version) != "2" {
		t.Errorf("the version recorded: %+v", c)
	}
	// a registered model with no version of Tabularia's
	w.register("Pasta/price elasticity", "by-hand", w.card, map[string]string{"tabularia.model": ""})
	if _, _, err := w.predict(`"registeredModel": "by-hand", "version": "latest"`); err == nil ||
		!strings.Contains(err.Error(), "the registered model «by-hand» has no version Tabularia registered") {
		t.Errorf("no version of Tabularia's: %v", err)
	}
}

func TestAVersionThatCannotBeAppliedIsRefusedBeforeAnyRun(t *testing.T) {
	w := newPredictWorld(t)
	good := w.register("Pasta/price elasticity", "good", w.card, nil)
	w.register("Pasta/price elasticity", "tampered", w.card, map[string]string{"tabularia.card_sha256": shaOf([]byte("another card"))})
	w.register("Pasta/price elasticity", "foreign", w.card, map[string]string{"tabularia.model": ""})
	newer := []byte(strings.Replace(predictCardText, `"format_version": 1`, `"format_version": 2`, 1))
	w.register("Pasta/price elasticity", "newer", newer, nil)
	w.ml.Register("Pasta/price elasticity", "noflavor", map[string]string{"tabularia.model": "linear"}, nil,
		map[string][]byte{"model.json": w.card, "MLmodel": []byte("flavors:\n  python_function:\n    loader_module: x\n")},
		map[string]string{"tabularia.model": "linear", "tabularia.card_sha256": shaOf(w.card)})
	w.ml.Register("Pasta/price elasticity", "nofiles", map[string]string{"tabularia.model": "linear"}, nil, nil,
		map[string]string{"tabularia.model": "linear", "tabularia.card_sha256": shaOf(w.card)})
	for what, c := range map[string]struct{ data, want string }{
		"a card changed after registration": {`"registeredModel": "tampered", "version": "1"`,
			"Predict «Next month»: the model card of the version 1 of «tampered» does not match the fingerprint MLflow keeps for it (tabularia.card_sha256)"},
		"a version that is not there": {`"registeredModel": "good", "version": "9"`,
			"Predict «Next month»: the version 9 of «good» is not on the MLflow server (deleted, or never made)"},
		"a model that is not there": {`"registeredModel": "nobody", "version": "latest"`, "has no version Tabularia registered"},
		"not Tabularia's":           {`"registeredModel": "foreign", "version": "1"`, "was not registered by Tabularia (it has no tag tabularia.model)"},
		"no tabularia flavor":       {`"registeredModel": "noflavor", "version": "1"`, "has no `tabularia` flavor"},
		"no files":                  {`"registeredModel": "nofiles", "version": "1"`, "the version 1 of «nofiles» has no MLmodel among its files"},
		"a newer card format":       {`"registeredModel": "newer", "version": "1"`, "has the format version 2, which this version of Tabularia does not read"},
		"an option of another model": {`"registeredModel": "good", "version": "` + good + `", "params": {"threshold": 0.4}`,
			"Predict's option 'threshold' applies to logistic, xgboost (binary), catboost (binary), not to linear"},
		"no version chosen": {`"registeredModel": "good", "version": ""`, "choose the model's version (or its latest version)"},
		"not a version":     {`"registeredModel": "good", "version": "v1"`, "«v1» is not a version: a number, or «latest»"},
	} {
		if _, _, err := w.predict(c.data); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: %v", what, err)
		}
	}
	if n := w.runsMade(); n != 0 || len(w.attempts) != 0 {
		t.Errorf("%d runs and %d Jobs made by refusals", n, len(w.attempts))
	}
	if strings.Contains(w.enginePaths(), "/scripts/") {
		t.Errorf("the engine was asked: %s", w.enginePaths())
	}
}

func TestAPredictedColumnTheModelReadsIsRefused(t *testing.T) {
	w := newPredictWorld(t)
	card := []byte(strings.Replace(predictCardText, `{"name": "price", "type": "number"}`, `{"name": "prediction_low", "type": "number"}`, 1))
	w.register("Pasta/price elasticity", "odd", card, nil)
	if _, _, err := w.predict(`"registeredModel": "odd", "version": "1", "params": {"interval": true}`); err == nil || err.Error() !=
		"Predict «Next month»: the model reads a column «prediction_low», which the prediction would overwrite: rename it before the node" {
		t.Errorf("%v", err)
	}
	// without the interval it adds `prediction` only: it runs
	if _, _, err := w.predict(`"registeredModel": "odd", "version": "1", "params": {}`); err != nil {
		t.Errorf("without the interval: %v", err)
	}
	// the runner refuses a column of the rows that the model does not read (it sees the rows)
	w.attempts = nil
	w.script = []modelAttemptResult{{log: "TABULARIA_ERROR {\"code\":\"output_column_exists\",\"message\":\"the rows already have a column «prediction»: rename it before the node\",\"params\":{}}\n",
		exit: int64(modelgroups.ExitRefused), reason: "it ended with exit code 3"}}
	id, _, err := w.predict(`"registeredModel": "odd", "version": "1", "params": {}`)
	if err == nil || err.Error() != "Predict «Next month»: the rows already have a column «prediction»: rename it before the node" {
		t.Errorf("the runner's refusal: %v", err)
	}
	if r := w.row(id); r.status != "FAILURE" || len(w.attempts) != 1 {
		t.Errorf("%+v, %d attempts", r, len(w.attempts))
	}
}

func TestAGroupThatCannotRunStopsThePredict(t *testing.T) {
	w := newPredictWorld(t)
	w.register("Pasta/price elasticity", "price-elasticity", w.card, nil)
	data := `"registeredModel": "price-elasticity", "version": "1"`
	if _, err := w.db.Exec(w.ctx, `UPDATE model_groups SET everyone = false WHERE id = 'regression'`); err != nil {
		t.Fatal(err)
	}
	if _, _, err := w.predict(data); err == nil || !strings.Contains(err.Error(), "may not use the model group regression") {
		t.Errorf("not allowed: %v", err)
	}
	if _, err := w.db.Exec(w.ctx, `UPDATE model_groups SET everyone = true, enabled = false WHERE id = 'regression'`); err != nil {
		t.Fatal(err)
	}
	if _, _, err := w.predict(data); err == nil || err.Error() != "Predict «Next month»: the model group regression is disabled" {
		t.Errorf("disabled: %v", err)
	}
	if _, err := w.db.Exec(w.ctx, `UPDATE model_groups SET enabled = true WHERE id = 'regression'`); err != nil {
		t.Fatal(err)
	}
	// the group allows 50 rows: the input has 120
	if _, err := w.db.Exec(w.ctx, `UPDATE model_groups SET max_rows = 50 WHERE id = 'regression'`); err != nil {
		t.Fatal(err)
	}
	id, _, err := w.predict(data)
	if err == nil || !strings.Contains(err.Error(), "the prediction would read 120 rows; the group «regression» allows 50") {
		t.Errorf("rows: %v", err)
	}
	if r := w.row(id); r.status != "FAILURE" || len(w.attempts) != 0 {
		t.Errorf("%+v, %d Jobs", r, len(w.attempts))
	}
	// a person without CONNECT on the connection's folder
	if _, err := w.db.Exec(w.ctx, `DELETE FROM permissions WHERE project_id = $1 AND user_id = $2`, w.folder, w.ann.ID); err != nil {
		t.Fatal(err)
	}
	if _, _, err := w.predict(data); err == nil || !strings.Contains(err.Error(), "may not use the MLflow connection #") || strings.Contains(err.Error(), "Tracking") {
		t.Errorf("no CONNECT, or the name of the connection told: %v", err)
	}
}

// ── the form's lists ─────────────────────────────────────────────────────────

func (w *predictWorld) browse(u *auth.User, path string, connection int64, query string) *httptest.ResponseRecorder {
	w.t.Helper()
	rec := httptest.NewRecorder()
	r := httptest.NewRequest(http.MethodGet, "/x?"+query, nil)
	r.SetPathValue("connection_id", fmt.Sprint(connection))
	h := mlflowExperiments
	if path == "versions" {
		h = mlflowVersions
	}
	h(w.d, rec, r, &auth.Principal{User: *u})
	return rec
}

func TestTheFormListsTheExperimentsAndTabulariasVersions(t *testing.T) {
	w := newPredictWorld(t)
	w.register("Pasta/price elasticity", "price-elasticity", w.card, map[string]string{"tabularia.variant": "binary"})
	w.register("Pasta/price elasticity", "price-elasticity", w.card, nil)
	w.register("Pasta/price elasticity", "by-hand", w.card, map[string]string{"tabularia.model": ""})
	w.register("Other", "churn", w.card, nil)
	// a run of the experiment Tabularia did not make, with a version
	w.ml.Register("Pasta/price elasticity", "external", map[string]string{"owner": "someone"}, nil, nil,
		map[string]string{"tabularia.model": "linear"})
	rec := w.browse(w.ann, "experiments", w.conn, "")
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"name":"Other"`) ||
		strings.Index(rec.Body.String(), "Other") > strings.Index(rec.Body.String(), "Pasta") {
		t.Errorf("experiments: %d %s", rec.Code, rec.Body)
	}
	rec = w.browse(w.ann, "versions", w.conn, "experiment=Pasta%2Fprice+elasticity")
	var got struct {
		Versions []map[string]any `json:"versions"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil || rec.Code != http.StatusOK {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	var seen []string
	for _, v := range got.Versions {
		seen = append(seen, fmt.Sprintf("%s:%s", v["model"], v["version"]))
	}
	if strings.Join(seen, " ") != "price-elasticity:2 price-elasticity:1" {
		t.Errorf("the versions, newest first, Tabularia's in the experiment only: %v", seen)
	}
	if got.Versions[1]["variant"] != "binary" || got.Versions[0]["variant"] != nil {
		t.Errorf("the variant is the version's tag tabularia.variant, null without it: %v %v", got.Versions[1]["variant"], got.Versions[0]["variant"])
	}
	v := got.Versions[0]
	if v["kind"] != "linear" || v["flow"] != "Pricing" || v["node"] != "Elasticity" || !strings.HasPrefix(fmt.Sprint(v["created_at"]), "2023-11-") ||
		fmt.Sprint(v["metric"]) != "map[name:adj_r_squared value:0.79]" {
		t.Errorf("a version: %v", v)
	}
	// what MLflow does not have, and what the person may not use
	if rec := w.browse(w.ann, "versions", w.conn, "experiment=Nope"); rec.Code != http.StatusNotFound ||
		!strings.Contains(rec.Body.String(), "The experiment «Nope» is not on the MLflow server") {
		t.Errorf("missing experiment: %d %s", rec.Code, rec.Body)
	}
	if rec := w.browse(w.ann, "versions", w.conn, ""); rec.Code != http.StatusUnprocessableEntity {
		t.Errorf("no experiment: %d", rec.Code)
	}
	if rec := w.browse(w.ann, "experiments", 999999999, ""); rec.Code != http.StatusNotFound {
		t.Errorf("no connection: %d", rec.Code)
	}
	w.ml.Fail = func(r *http.Request) int { return http.StatusInternalServerError }
	if rec := w.browse(w.ann, "experiments", w.conn, ""); rec.Code != http.StatusBadGateway || strings.Contains(rec.Body.String(), mlflowtest.Leak) ||
		strings.Contains(rec.Body.String(), mlflowSecret) {
		t.Errorf("MLflow failing: %d %s", rec.Code, rec.Body)
	}
	w.ml.Fail = nil
	if _, err := w.db.Exec(w.ctx, `DELETE FROM permissions WHERE project_id = $1 AND user_id = $2`, w.folder, w.ann.ID); err != nil {
		t.Fatal(err)
	}
	if rec := w.browse(w.ann, "experiments", w.conn, ""); rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), "needs CONNECT") {
		t.Errorf("without CONNECT: %d %s", rec.Code, rec.Body)
	}
}

// ── the editor: Run, and «not up to date» ────────────────────────────────────

func TestTheEditorsRunOfAPredictAndLatestNotUpToDate(t *testing.T) {
	w := newPredictWorld(t)
	w.register("Pasta/price elasticity", "price-elasticity", w.card, nil)
	r := w.predictFlow(`"registeredModel": "price-elasticity", "version": "latest", "params": {"interval": true}`)
	// refused before any run: a version whose card does not match
	w.ml.Models["price-elasticity"][0].Tags["tabularia.card_sha256"] = shaOf([]byte("x"))
	if rec := w.trial(w.ann, r.flow.ID, "p"); rec.Code != http.StatusUnprocessableEntity || !strings.Contains(rec.Body.String(), "does not match the fingerprint") {
		t.Errorf("a tampered card: %d %s", rec.Code, rec.Body)
	}
	if n := w.runsMade(); n != 0 {
		t.Fatalf("%d runs made by a refusal", n)
	}
	w.ml.Models["price-elasticity"][0].Tags["tabularia.card_sha256"] = shaOf(w.card)
	rec := w.trial(w.ann, r.flow.ID, "p")
	if rec.Code != http.StatusAccepted {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	var started struct {
		RunID int64 `json:"run_id"`
	}
	_ = json.Unmarshal(rec.Body.Bytes(), &started)
	run := w.waitRun(started.RunID)
	if run.status != "SUCCESS" || !strings.Contains(run.script, `"trial": true`) {
		t.Fatalf("the trial: %+v", run)
	}
	w.mu.Lock()
	ns := w.attempts[len(w.attempts)-1].ns
	w.mu.Unlock()
	if ns != "script-trials" {
		t.Errorf("a trial runs in the trials' namespace, not %s", ns)
	}
	out := w.outputs(w.ann, r.flow.ID)["p"]
	if out == nil || out["last"]["stale"] != false || out["output"]["key"] != run.outputKey {
		t.Fatalf("outputs: %v", out)
	}
	// a flow run of this Tabularia registers version 2 of the model: «latest» is not up to date
	var model int64
	if err := w.db.QueryRow(w.ctx, `INSERT INTO runs (kind, flow_id, task_id, status, launched_by, trigger_type, input_key, output_bucket,
		output_key, publish_description, publish_overwrite, publish_sort_keys, script, started_at, mlflow_connection_id, mlflow_model_name,
		mlflow_model_version) VALUES ('model', $1, '', 'SUCCESS', $2, 'manual', '', '', '', '', false, '[]', '{}', now(), $3, 'price-elasticity', '2')
		RETURNING id`, r.flow.ID, w.ann.ID, w.conn).Scan(&model); err != nil {
		t.Fatal(err)
	}
	if out := w.outputs(w.ann, r.flow.ID)["p"]; out["last"]["stale"] != true {
		t.Errorf("after a new version: %v", out)
	}
	// another registered model's version changes nothing
	if _, err := w.db.Exec(w.ctx, `UPDATE runs SET mlflow_model_name = 'churn' WHERE id = $1`, model); err != nil {
		t.Fatal(err)
	}
	if out := w.outputs(w.ann, r.flow.ID)["p"]; out["last"]["stale"] != false {
		t.Errorf("another model's version: %v", out)
	}
}

func TestATrialsReadOfMLflowHasALimitAndSaysSo(t *testing.T) {
	w := newPredictWorld(t)
	w.register("Pasta/price elasticity", "price-elasticity", w.card, nil)
	r := w.predictFlow(`"registeredModel": "price-elasticity", "version": "latest", "params": {"interval": true}`)
	prev := predictTrialReadTimeout
	predictTrialReadTimeout = 300 * time.Millisecond
	t.Cleanup(func() { predictTrialReadTimeout = prev })
	// the server accepts the call and never answers (until the caller gives up, or 5 s)
	w.ml.Fail = func(req *http.Request) int {
		select {
		case <-req.Context().Done():
		case <-time.After(5 * time.Second):
		}
		return http.StatusGatewayTimeout
	}
	start := time.Now()
	rec := w.trial(w.ann, r.flow.ID, "p")
	if took := time.Since(start); took > 3*time.Second {
		t.Errorf("the request waited %s", took)
	}
	if rec.Code != http.StatusGatewayTimeout || !strings.Contains(rec.Body.String(), "MLflow did not answer in") {
		t.Errorf("a server that does not answer: %d %s", rec.Code, rec.Body)
	}
	if n := w.runsMade(); n != 0 {
		t.Errorf("%d runs made by a refusal", n)
	}
}

// ── customer lifetime (docs/specs/customer-lifetime-models.md, lot B7) ──────

const clvCardText = `{"format": "tabularia-model", "format_version": 1, "model": "clv", "variant": "value", "group": "customer_lifetime",
 "group_version": 1, "params": {"customer": "id", "frequency": "x", "recency": "t_x", "age": "T", "time_unit": "week", "spend": "m"},
 "inputs": [{"name": "x", "type": "number"}, {"name": "t_x", "type": "number"}, {"name": "T", "type": "number"}, {"name": "m", "type": "number"}],
 "customer_lifetime": {"node": "clv", "time_unit": "week", "purchase_model": "mbg_nbd", "chosen_by": "log_lik",
   "purchases": {"model": "mbg_nbd", "params": {"r": 0.5, "alpha": 6.2, "a": 0.9, "b": 1.6}},
   "spend": {"model": "gamma_gamma", "params": {"p": 6.2, "q": 3.7, "gamma": 15.4}},
   "horizon": 52, "discount_rate": 0.1, "periods_per_year": 52.17857142857143, "margin": 1}}`

func TestACustomerLifetimeCardIsAppliedOnlyWithItsParameters(t *testing.T) {
	read := func(text string) predictCard {
		var c predictCard
		if err := json.Unmarshal([]byte(text), &c); err != nil {
			t.Fatal(err)
		}
		return c
	}
	good := read(clvCardText)
	if why := good.incompatible("clv", "customer_lifetime"); why != "" {
		t.Fatal(why)
	}
	purchases := read(strings.Replace(strings.Replace(clvCardText, `"variant": "value"`, `"variant": "purchases"`, 1),
		`"spend": {"model": "gamma_gamma", "params": {"p": 6.2, "q": 3.7, "gamma": 15.4}},`, "", 1))
	if why := purchases.incompatible("clv", "customer_lifetime"); why != "" {
		t.Errorf("a card without a spend part: %s", why)
	}
	for what, c := range map[string]struct{ text, want string }{
		"no parameters":    {strings.Replace(clvCardText, `"customer_lifetime": {`, `"other": {`, 1), "holds no customer lifetime parameters (customer_lifetime.purchases)"},
		"no purchase part": {strings.Replace(clvCardText, `"purchases": {"model"`, `"nothing": {"model"`, 1), "holds no customer lifetime parameters (customer_lifetime.purchases)"},
		"another node":     {strings.Replace(clvCardText, `"node": "clv"`, `"node": "bg_bb"`, 1), "holds the parameters of the node «bg_bb», but the version says «clv»"},
		"a spend column and no spend part": {strings.Replace(clvCardText, `"spend": {"model": "gamma_gamma"`, `"x": {"model": "gamma_gamma"`, 1),
			"holds no spend parameters (customer_lifetime.spend)"},
		"a spend part and no spend column": {strings.Replace(clvCardText, `"variant": "value"`, `"variant": "purchases"`, 1),
			"was fitted without a spend column but holds spend parameters"},
		"another group's card": {strings.Replace(clvCardText, `"group": "customer_lifetime"`, `"group": "survival"`, 1), "says the group «survival», but the model «clv» is in «customer_lifetime»"},
	} {
		got := read(c.text)
		if why := got.incompatible("clv", "customer_lifetime"); !strings.Contains(why, c.want) {
			t.Errorf("%s: %q", what, why)
		}
	}
	// the other groups' cards need no such object
	if why := (&predictCard{Format: "tabularia-model", FormatVersion: 1, Model: "linear", Variant: "unpenalized", Group: "regression"}).incompatible("linear", "regression"); why != "" {
		t.Error(why)
	}
}

// enableGroup makes a group of the shipped ones usable by everyone, with a ready image, as the
// fixture does for regression; the rows are removed with the test.
func (w *predictWorld) enableGroup(id string) {
	w.t.Helper()
	shipped, ok := modelgroups.Get(id)
	if !ok {
		w.t.Fatalf("no group %s", id)
	}
	one := func(query string, args ...any) int64 {
		var n int64
		if err := w.db.QueryRow(w.ctx, query, args...).Scan(&n); err != nil {
			w.t.Fatal(err)
		}
		return n
	}
	_, _ = w.db.Exec(w.ctx, `DELETE FROM model_groups WHERE id = $1`, id)
	_, _ = w.db.Exec(w.ctx, `DELETE FROM script_environments WHERE managed_by = $1`, "model_group:"+id)
	env := one(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
		memory_limit, max_timeout_seconds, created_at, updated_at, managed_by) VALUES ($1, '', 'r', true, '1', '2', '2Gi', '4Gi', 3600,
		now(), now(), $2) RETURNING id`, fmt.Sprintf("%s %d", id, time.Now().UnixNano()), "model_group:"+id)
	version := one(`INSERT INTO script_environment_versions (environment_id, number, language_version, base_image, packages, status,
		image_ref, arch, recipe_hash, created_at) VALUES ($1, 1, '4.6', 'rocker/r-ver:4.6.0', '', 'ready',
		'registry:5000/envs@sha256:fedcba9876543210', 'amd64', $2, now()) RETURNING id`, env, shipped.RecipeHash)
	if _, err := w.db.Exec(w.ctx, `UPDATE script_environments SET current_version_id = $1 WHERE id = $2`, version, env); err != nil {
		w.t.Fatal(err)
	}
	if _, err := w.db.Exec(w.ctx, `INSERT INTO model_groups (id, environment_id, enabled, everyone, updated_at) VALUES ($1, $2, true, true, now())`, id, env); err != nil {
		w.t.Fatal(err)
	}
	w.t.Cleanup(func() {
		_, _ = w.db.Exec(context.Background(), `DELETE FROM model_groups WHERE id = $1`, id)
		_, _ = w.db.Exec(context.Background(), `UPDATE script_environments SET current_version_id = NULL WHERE id = $1`, env)
		_, _ = w.db.Exec(context.Background(), `DELETE FROM script_environment_versions WHERE environment_id = $1`, env)
		_, _ = w.db.Exec(context.Background(), `DELETE FROM script_environments WHERE id = $1`, env)
	})
}

func (w *predictWorld) registerCLV(model string, card string, tags map[string]string) {
	t := map[string]string{"tabularia.model": "clv", "tabularia.group": "customer_lifetime", "tabularia.group_version": "1",
		"tabularia.variant": "value", "tabularia.purchase_model": "mbg_nbd"}
	for k, v := range tags {
		t[k] = v
	}
	w.register("Pasta/price elasticity", model, []byte(card), t)
}

// A registered customer lifetime version is applied by a Predict node in the group's Job: the
// horizon (in the card's unit) is the only option, the columns are the catalogue's of the variant,
// and nothing here depends on the numbers of the fit.
func TestAPredictOfACustomerLifetimeVersion(t *testing.T) {
	w := newPredictWorld(t)
	w.enableGroup("customer_lifetime")
	w.registerCLV("customers", clvCardText, nil)
	id, _, err := w.predict(`"registeredModel": "customers", "version": "1", "params": {"horizon": 26}`)
	if err != nil {
		t.Fatal(err)
	}
	if r := w.row(id); r.status != "SUCCESS" {
		t.Fatalf("%+v", r)
	}
	if len(w.attempts) != 1 {
		t.Fatalf("%d attempts", len(w.attempts))
	}
	task := taskOf(t, w.attempts[0])
	if task.Mode != "predict" || task.Group != "customer_lifetime" || task.Model != "clv" || task.Variant != "value" || task.Output != "rows" ||
		fmt.Sprint(task.Params) != "map[horizon:26]" {
		t.Errorf("task.json: %+v", task)
	}
	// without a horizon: the card's own
	w.attempts = nil
	if _, _, err := w.predict(`"registeredModel": "customers", "version": "1", "params": {}`); err != nil {
		t.Fatal(err)
	}
	if got := fmt.Sprint(taskOf(t, w.attempts[0]).Params); got != "map[]" {
		t.Errorf("params without a horizon: %s", got)
	}
}

func TestAVersionOfACustomerLifetimeModelThatCannotBeAppliedIsRefused(t *testing.T) {
	w := newPredictWorld(t)
	w.enableGroup("customer_lifetime")
	w.registerCLV("good", clvCardText, nil)
	w.registerCLV("noparams", strings.Replace(clvCardText, `"customer_lifetime": {`, `"other": {`, 1), nil)
	w.registerCLV("clash", strings.Replace(clvCardText, `{"name": "m", "type": "number"}`, `{"name": "prediction_clv", "type": "number"}`, 1), nil)
	// the version's group tag says another group than the model's
	w.registerCLV("elsewhere", clvCardText, map[string]string{"tabularia.group": "survival"})
	// a tag that names a model of another group, with this group's card
	w.registerCLV("wrongcard", clvCardText, map[string]string{"tabularia.model": "linear", "tabularia.group": "regression"})
	for what, c := range map[string]struct{ data, want string }{
		"an option of another model": {`"registeredModel": "good", "version": "1", "params": {"interval": true}`,
			"Predict's option 'interval' applies to linear, kaplan_meier, arima, parametric_survival, not to clv"},
		"a negative horizon": {`"registeredModel": "good", "version": "1", "params": {"horizon": -3}`, "Predict: 'horizon'"},
		"no parameters in the card": {`"registeredModel": "noparams", "version": "1"`,
			"holds no customer lifetime parameters (customer_lifetime.purchases): fit the model again"},
		"a column the model reads": {`"registeredModel": "clash", "version": "1"`,
			"the model reads a column «prediction_clv», which the prediction would overwrite: rename it before the node"},
		"another group's tag": {`"registeredModel": "elsewhere", "version": "1"`,
			"says it belongs to the group «survival», but the model «clv» is in «customer_lifetime»"},
		"a card of this group under another model": {`"registeredModel": "wrongcard", "version": "1"`,
			"holds the model «clv», but the version says «linear»"},
	} {
		if _, _, err := w.predict(c.data); err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: %v", what, err)
		}
	}
	if n := w.runsMade(); n != 0 || len(w.attempts) != 0 {
		t.Errorf("%d runs and %d Jobs made by refusals", n, len(w.attempts))
	}
	// the group that is off stops it, in its own words
	if _, err := w.db.Exec(w.ctx, `UPDATE model_groups SET enabled = false WHERE id = 'customer_lifetime'`); err != nil {
		t.Fatal(err)
	}
	if _, _, err := w.predict(`"registeredModel": "good", "version": "1"`); err == nil || err.Error() != "Predict «Next month»: the model group customer_lifetime is disabled" {
		t.Errorf("disabled: %v", err)
	}
}
