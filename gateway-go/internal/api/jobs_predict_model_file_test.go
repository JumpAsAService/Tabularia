package api

import (
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/mlflowconn/mlflowtest"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
)

// The model file of a boosting model (docs/specs/statistical-models.md, «The saved model,
// MLflow and Predict», lot X5): xgboost.json or catboost.cbm uploaded to MLflow beside the card
// by a flow run, with its sha256 in the version's tag; streamed by Predict from MLflow's artifact
// proxy to the engine's POST /scripts/objects/upload, which hashes it while it receives it; the
// Job told where it is (TABULARIA_MODEL_FILE_IN), holding nothing of MLflow.

// ── a fake engine that does what the real upload route does ──────────────────

type uploadedObject struct {
	key, sha, connection string
	size                 int64
	body                 []byte
}

type uploadEngine struct {
	mu      sync.Mutex
	calls   int
	objects map[string][]byte
	seen    []uploadedObject
	hold    bool // keep the bytes (the small tests); off: hash and discard (the streaming test)
	status  int  // an answer to give whatever the file (0: judge it)
	next    http.Handler
}

func (e *uploadEngine) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	if r.URL.Path != "/scripts/objects/upload" {
		e.next.ServeHTTP(w, r)
		return
	}
	q := r.URL.Query()
	want, _ := strconv.ParseInt(q.Get("bytes"), 10, 64)
	h := sha256.New()
	var kept []byte
	var n int64
	buf := make([]byte, 32<<10)
	for {
		m, err := r.Body.Read(buf)
		h.Write(buf[:m])
		n += int64(m)
		if e.hold {
			kept = append(kept, buf[:m]...)
		}
		if err != nil {
			break
		}
	}
	e.mu.Lock()
	defer e.mu.Unlock()
	e.calls++
	got := hex.EncodeToString(h.Sum(nil))
	e.seen = append(e.seen, uploadedObject{key: q.Get("key"), sha: q.Get("sha256"), size: n, connection: r.Header.Get("X-Tabularia-Scripts-Connection"), body: kept})
	w.Header().Set("Content-Type", "application/json")
	switch {
	case e.status != 0:
		w.WriteHeader(e.status)
		_, _ = w.Write([]byte(`{"detail": "the engine's reason"}`))
	case n != want || got != q.Get("sha256"): // as the route: 409, no object
		w.WriteHeader(http.StatusConflict)
		_, _ = w.Write([]byte(`{"detail": "The file's sha256 is not the declared one"}`))
	default:
		if e.objects == nil {
			e.objects = map[string][]byte{}
		}
		e.objects[q.Get("key")] = kept
		_, _ = w.Write([]byte(fmt.Sprintf(`{"bytes": %d}`, n)))
	}
}

// ── the boosting group, a card that declares a file, a version that has it ───

type boostWorld struct {
	*predictWorld
	up   *uploadEngine
	file []byte
}

const boostMLmodel = "artifact_path: model\nflavors:\n  tabularia:\n    card: model.json\n    format: tabularia-model\n    format_version: 1\n    model_file: xgboost.json\nrun_id: run1\n"

// boostCard is a card of an XGBoost binary model that declares `file`.
func boostCard(file string) []byte {
	return []byte(`{"format": "tabularia-model", "format_version": 1, "model": "xgboost", "variant": "binary", "group": "boosting",
 "group_version": 3, "params": {}, "inputs": [{"name": "price", "type": "number"}],
 "boosting": {"library": "xgboost", "library_version": "3.4.1", "task": "binary", "objective": "binary:logistic",
   "classes": ["no", "yes"], "event_value": "yes", "threshold": 0.5, "trees": 10, "best_iteration": 9, "threads": 2, "missing_level": null` + file + `}}`)
}

func declares(name string, size int, sha string) string {
	return fmt.Sprintf(`, "model_file": {"name": %q, "format": "xgboost-json", "bytes": %d, "sha256": %q}`, name, size, sha)
}

func newBoostWorld(t *testing.T) *boostWorld {
	t.Helper()
	pw := newPredictWorld(t)
	w := &boostWorld{predictWorld: pw, file: append([]byte("\x00\xff{\"learner\": "), mlflowtest.Pattern(60_000)...), up: &uploadEngine{hold: true}}
	// the boosting group, as the regression one the fixture makes (Python this time)
	shipped, ok := modelgroups.Get("boosting")
	if !ok {
		t.Fatal("no boosting group")
	}
	stamp := time.Now().UnixNano()
	_, _ = w.db.Exec(w.ctx, `DELETE FROM model_groups WHERE id = 'boosting'`)
	_, _ = w.db.Exec(w.ctx, `DELETE FROM script_environments WHERE managed_by = 'model_group:boosting'`)
	var env, version int64
	if err := w.db.QueryRow(w.ctx, `INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
		memory_limit, max_timeout_seconds, created_at, updated_at, managed_by) VALUES ($1, '', 'python', true, '1', '2', '2Gi', '4Gi', 3600,
		now(), now(), 'model_group:boosting') RETURNING id`, fmt.Sprintf("Boosting %d", stamp)).Scan(&env); err != nil {
		t.Fatal(err)
	}
	if err := w.db.QueryRow(w.ctx, `INSERT INTO script_environment_versions (environment_id, number, language_version, base_image, packages, status,
		image_ref, arch, recipe_hash, created_at) VALUES ($1, 3, '3.12', 'python:3.12-slim', '', 'ready',
		'registry:5000/envs@sha256:0123456789abcdef', 'amd64', $2, now()) RETURNING id`, env, shipped.RecipeHash).Scan(&version); err != nil {
		t.Fatal(err)
	}
	for _, sql := range []string{`UPDATE script_environments SET current_version_id = %d WHERE id = %d`,
		`INSERT INTO model_groups (id, environment_id, enabled, everyone, updated_at) VALUES ('boosting', %[2]d, true, true, now())`} {
		if _, err := w.db.Exec(w.ctx, fmt.Sprintf(sql, version, env)); err != nil {
			t.Fatal(err)
		}
	}
	t.Cleanup(func() {
		_, _ = w.db.Exec(w.ctx, `DELETE FROM model_groups WHERE id = 'boosting'`)
		_, _ = w.db.Exec(w.ctx, `UPDATE script_environments SET current_version_id = NULL WHERE id = $1`, env)
		_, _ = w.db.Exec(w.ctx, `DELETE FROM script_environment_groups WHERE environment_id = $1`, env)
		_, _ = w.db.Exec(w.ctx, `DELETE FROM script_environment_versions WHERE environment_id = $1`, env)
		_, _ = w.db.Exec(w.ctx, `DELETE FROM script_environments WHERE id = $1`, env)
	})
	// the engine, with the upload route in front of the fixture's
	w.up.next = http.HandlerFunc(w.serveEngine)
	srv := httptest.NewServer(w.up)
	t.Cleanup(srv.Close)
	e, err := engine.New(srv.URL, modelsTestFernetKey)
	if err != nil {
		t.Fatal(err)
	}
	w.d.Engine = e
	return w
}

// registerBoost registers a version whose files are the card and, unless nil, the model file.
func (w *boostWorld) registerBoost(card, file []byte, tags map[string]string) {
	files := map[string][]byte{"model.json": card, "MLmodel": []byte(boostMLmodel)}
	if file != nil {
		files["xgboost.json"] = file
	}
	vtags := map[string]string{"tabularia.model": "xgboost", "tabularia.group": "boosting", "tabularia.group_version": "3",
		"tabularia.card_sha256": shaOf(card), "tabularia.variant": "binary"}
	for k, v := range tags {
		vtags[k] = v
	}
	w.ml.Register("Pasta/price elasticity", "churn", map[string]string{"tabularia.model": "xgboost"}, map[string]float64{"auc": 0.8}, files, vtags)
}

const boostData = `"registeredModel": "churn", "version": "1", "params": {}`

func TestAPredictStreamsTheModelFileIntoTheStepsPrefix(t *testing.T) {
	w := newBoostWorld(t)
	sha := shaOf(w.file)
	w.registerBoost(boostCard(declares("xgboost.json", len(w.file), sha)), w.file, map[string]string{"tabularia.model_file_sha256": sha})
	id, out, err := w.predict(boostData)
	if err != nil {
		t.Fatal(err)
	}
	if r := w.row(id); r.status != "SUCCESS" || out == nil {
		t.Fatalf("%+v", r)
	}
	// the engine got the bytes once, with the card's size and hash, and kept them at <step>/model_file
	if w.up.calls != 1 || len(w.up.seen) != 1 {
		t.Fatalf("%d uploads", w.up.calls)
	}
	u := w.up.seen[0]
	if !strings.HasSuffix(u.key, "/model_file") || !strings.HasPrefix(u.key, "env-") || u.sha != sha || u.size != int64(len(w.file)) ||
		string(w.up.objects[u.key]) != string(w.file) {
		t.Errorf("uploaded: %s %s %d", u.key, u.sha, u.size)
	}
	// the connection travels in a header (its secret still encrypted), never in the query
	raw, err := base64.StdEncoding.DecodeString(u.connection)
	var conn map[string]any
	if err != nil || json.Unmarshal(raw, &conn) != nil || conn["bucket"] != "scripts" || conn["secret_key_encrypted"] == "" || strings.Contains(string(raw), "scripts-s3cret") {
		t.Errorf("connection header: %v %s", err, raw)
	}
	// the Job: the card and the file, nothing of MLflow, no pickle
	if len(w.attempts) != 1 {
		t.Fatalf("%d attempts", len(w.attempts))
	}
	a := w.attempts[0]
	env := map[string]string{}
	c0 := objectList(pyjson.Obj(pyjson.Obj(pyjson.Obj(pyjson.Obj(a.manifest.Value("spec")).Value("template")).Value("spec"))).Value("containers"))[0]
	for _, e := range c0.Value("env").([]any) {
		o := pyjson.Obj(e)
		env[pyjson.Str(o.Value("name"))] = pyjson.Str(o.Value("value"))
	}
	if env[modelgroups.EnvModelFileIn] != "s3://scripts/"+u.key || !strings.HasSuffix(env[modelgroups.EnvModelIn], "/model.json") ||
		modelgroups.EnvModelFileIn != "TABULARIA_MODEL_FILE_IN" {
		t.Errorf("the Job's environment: %v", env)
	}
	everything := pyDumps(a.manifest) + pyDumps(a.code) + pyDumps(a.secret)
	for _, leak := range []string{mlflowSecret, basicOf("ann", mlflowSecret), "MLFLOW", w.ml.URL, "mlflow-artifacts", "native.pkl"} {
		if strings.Contains(everything, leak) {
			t.Errorf("the Job holds %q", leak)
		}
	}
	// MLflow was only read: the version, MLmodel, the card, and the model file (the probe and the stream)
	reads := 0
	for _, p := range w.ml.Paths() {
		if !strings.HasPrefix(p, "GET ") || strings.Contains(p, "native") {
			t.Errorf("a call that is not a read of what is applied: %s", p)
		}
		if strings.HasSuffix(p, "/xgboost.json") {
			reads++
		}
	}
	if reads != 2 {
		t.Errorf("the model file was read %d times (the probe of the plan and the stream)", reads)
	}
	// the file leaves the bucket with the step
	var deleted struct {
		Keys []string `json:"keys"`
	}
	for _, call := range w.modelRunFixture.engine.calls {
		if rest, ok := strings.CutPrefix(call, "/scripts/objects/delete "); ok {
			_ = json.Unmarshal([]byte(rest), &deleted)
		}
	}
	if !strings.Contains(strings.Join(deleted.Keys, " "), u.key) {
		t.Errorf("the model file is not deleted with the step: %v", deleted.Keys)
	}
	var info map[string]any
	_ = json.Unmarshal([]byte(w.row(id).script), &info)
	if info["model_file_sha256"] != sha {
		t.Errorf("the run's script: %s", w.row(id).script)
	}
	// the engine was asked: the card, the file, the rows, the warnings, the clean-up
	if got := w.enginePaths(); got != "/scripts/objects/write /scripts/output/import /scripts/objects/read /scripts/objects/delete" {
		t.Errorf("engine (past the upload's own): %s", got)
	}
}

// What stops a Predict step before any run exists, in a flow and in the editor's trial (which
// calls planPredict, the same checks).
func TestAModelFileThatCannotBeAppliedStopsThePredictBeforeAnyRun(t *testing.T) {
	sha := shaOf([]byte("x"))
	for _, c := range []struct {
		name   string
		card   func(file []byte) []byte
		file   func(file []byte) []byte // what MLflow keeps (nil: no file)
		before func(w *boostWorld)
		want   string
	}{
		{"no file among the version's files", func(f []byte) []byte { return boostCard(declares("xgboost.json", len(f), shaOf(f))) },
			func([]byte) []byte { return nil }, nil, "has no model file among its files (xgboost.json): it is not applied"},
		{"the card declares none", func([]byte) []byte { return boostCard("") },
			func(f []byte) []byte { return f }, nil, "declares no model file: it is not applied"},
		{"a name that is a path", func(f []byte) []byte { return boostCard(declares("../xgboost.json", len(f), shaOf(f))) },
			func(f []byte) []byte { return f }, nil, "names a model file (../xgboost.json) that is not the one of its library (xgboost.json)"},
		{"the other library's file", func(f []byte) []byte { return boostCard(declares("catboost.cbm", len(f), shaOf(f))) },
			func(f []byte) []byte { return f }, nil, "that is not the one of its library"},
		{"a fingerprint that is not one", func(f []byte) []byte { return boostCard(declares("xgboost.json", len(f), "ABC")) },
			func(f []byte) []byte { return f }, nil, "keeps no valid fingerprint of its model file"},
		{"a size above the limit", func(f []byte) []byte { return boostCard(declares("xgboost.json", 64<<20+1, sha)) },
			func(f []byte) []byte { return f }, nil, "is larger than a model file may be (67108864 bytes)"},
		{"a size that is not the file's", func(f []byte) []byte { return boostCard(declares("xgboost.json", len(f)+1, shaOf(f))) },
			func(f []byte) []byte { return f }, nil, "does not match the fingerprint its card keeps: it is not applied"},
		{"a file above the limit, by the length MLflow says", func(f []byte) []byte { return boostCard(declares("xgboost.json", len(f), shaOf(f))) },
			func(f []byte) []byte { return f }, func(w *boostWorld) {
				maxModelFileBytes = func() int64 { return int64(len(w.file) - 1) }
			}, "is larger than a model file may be"},
	} {
		t.Run(c.name, func(t *testing.T) {
			prev := maxModelFileBytes
			t.Cleanup(func() { maxModelFileBytes = prev })
			w := newBoostWorld(t)
			if c.before != nil {
				c.before(w)
			}
			w.registerBoost(c.card(w.file), c.file(w.file), nil)
			_, _, err := w.predict(boostData)
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("%v, want %q", err, c.want)
			}
			if w.runsMade() != 0 || len(w.attempts) != 0 || w.up.calls != 0 {
				t.Errorf("%d runs, %d Jobs, %d uploads: nothing may start", w.runsMade(), len(w.attempts), w.up.calls)
			}
			// the trial reads the same checks
			_, perr := w.q.planPredict(w.ctx, w.ann, w.predictFlow(boostData).node)
			if perr == nil || perr.Error() != err.Error() {
				t.Errorf("the trial's check: %v", perr)
			}
		})
	}
}

func TestAFileThatIsNotTheCardsStopsTheStepAndLeavesNoObject(t *testing.T) {
	w := newBoostWorld(t)
	// the card keeps the hash of another file: the sizes agree, so only the engine can tell
	other := append([]byte(nil), w.file...)
	other[100] ^= 0xff
	w.registerBoost(boostCard(declares("xgboost.json", len(w.file), shaOf(other))), w.file, nil)
	id, _, err := w.predict(boostData)
	want := "does not match the fingerprint its card keeps: it is not applied"
	if err == nil || !strings.Contains(err.Error(), want) {
		t.Fatalf("%v", err)
	}
	if r := w.row(id); r.status != "FAILURE" || len(w.attempts) != 0 || len(w.up.objects) != 0 || w.up.calls != 1 {
		t.Errorf("%+v, %d Jobs, %d objects, %d uploads", w.row(id), len(w.attempts), len(w.up.objects), w.up.calls)
	}
}

// the engine's other refusals have their sentences (one world each: the fixture holds a lock)
func TestTheEnginesOtherRefusalsOfTheFileAreSaid(t *testing.T) {
	w := newBoostWorld(t)
	w.up.status = http.StatusRequestEntityTooLarge
	w.registerBoost(boostCard(declares("xgboost.json", len(w.file), shaOf(w.file))), w.file, nil)
	if _, _, err := w.predict(boostData); err == nil || !strings.Contains(err.Error(), "is larger than a model file may be") {
		t.Errorf("413: %v", err)
	}
}

func TestABucketThatRefusesTheFileIsSaidWithoutMLflowsSecret(t *testing.T) {
	w := newBoostWorld(t)
	w.up.status = http.StatusBadGateway
	w.registerBoost(boostCard(declares("xgboost.json", len(w.file), shaOf(w.file))), w.file, nil)
	if _, _, err := w.predict(boostData); err == nil || !strings.Contains(err.Error(), "could not be written to the scripts bucket: the engine's reason") ||
		strings.Contains(err.Error(), mlflowSecret) {
		t.Errorf("502: %v", err)
	}
}

// ── a flow run uploads the file and tags the version ─────────────────────────

func TestAFlowRunUploadsTheModelFileAndItsHash(t *testing.T) {
	w := newMLflowWorld(t)
	file := append([]byte("\x00CBM\xff"), mlflowtest.Pattern(40_000)...) // binary
	sha := shaOf(file)
	card := boostCard(declares("xgboost.json", len(file), sha))
	w.files["model.json"], w.files["xgboost.json"] = card, file
	delete(w.files, "native.rds")
	id, err := w.runWith(w.block(`, "registerAs": "churn"`))
	if err != nil {
		t.Fatal(err)
	}
	if r := w.row(id); r.status != "SUCCESS" {
		t.Fatalf("%+v", r)
	}
	run := w.ml.OnlyRun()
	dir := "1/" + run.ID + "/artifacts/model/"
	if string(w.ml.Artifacts[dir+"xgboost.json"]) != string(file) || w.ml.ContentType[dir+"xgboost.json"] != "application/octet-stream" {
		t.Errorf("the model file in MLflow: %d bytes", len(w.ml.Artifacts[dir+"xgboost.json"]))
	}
	for _, name := range []string{"native.pkl", "native.rds"} {
		if _, ok := w.ml.Artifacts[dir+name]; ok {
			t.Errorf("%s was uploaded", name)
		}
	}
	v := w.ml.Models["churn"]
	if len(v) != 1 || v[0].Tags["tabularia.model_file_sha256"] != sha || v[0].Tags["tabularia.variant"] != "binary" ||
		v[0].Tags["tabularia.card_sha256"] != shaOf(card) {
		t.Errorf("versions: %+v", v)
	}
	if _, m := w.columns(id); len(m["warnings"].([]any)) != 0 {
		t.Errorf("warnings: %v", m["warnings"])
	}
}

// a native.pkl a runner left among the artifacts is not part of a model: it is not uploaded
func TestALeftoverNativePklIsNotUploadedToMLflow(t *testing.T) {
	w := newMLflowWorld(t)
	file := append([]byte("\x00CBM\xff"), mlflowtest.Pattern(40_000)...)
	w.files["model.json"], w.files["xgboost.json"] = boostCard(declares("xgboost.json", len(file), shaOf(file))), file
	w.files["native.pkl"] = []byte("pickle that must never travel")
	delete(w.files, "native.rds")
	id, err := w.runWith(w.block(`, "registerAs": "churn"`))
	if err != nil {
		t.Fatal(err)
	}
	if r := w.row(id); r.status != "SUCCESS" {
		t.Fatalf("%+v", r)
	}
	run := w.ml.OnlyRun()
	for key := range w.ml.Artifacts {
		if strings.HasPrefix(key, "1/"+run.ID+"/") && strings.Contains(key, "native.pkl") {
			t.Errorf("uploaded: %s", key)
		}
	}
	if len(w.ml.Artifacts["1/"+run.ID+"/artifacts/model/xgboost.json"]) == 0 {
		t.Error("the model file itself was not uploaded")
	}
}

func TestAFileThatItsCardDoesNotDescribeIsNotRegistered(t *testing.T) {
	file := []byte("\x00model file bytes\xff")
	for name, card := range map[string][]byte{
		"another hash": boostCard(declares("xgboost.json", len(file), shaOf([]byte("another")))),
		"another size": boostCard(declares("xgboost.json", len(file)+1, shaOf(file))),
		"no file":      boostCard(""),
		"another name": boostCard(declares("catboost.cbm", len(file), shaOf(file))),
	} {
		t.Run(name, func(t *testing.T) {
			w := newMLflowWorld(t)
			w.files["model.json"], w.files["xgboost.json"] = card, file
			id, err := w.runWith(w.block(`, "registerAs": "churn"`))
			if err == nil || !strings.Contains(err.Error(), "the model was fitted but MLflow could not record it") {
				t.Fatalf("%v", err)
			}
			if len(w.ml.Models["churn"]) != 0 || w.row(id).status != "FAILURE" {
				t.Errorf("a version was registered: %v", w.ml.Models)
			}
		})
	}
}

func TestACardThatDeclaresAFileTheStepDidNotLeaveIsNotRegistered(t *testing.T) {
	w := newMLflowWorld(t)
	w.files["model.json"] = boostCard(declares("xgboost.json", 10, shaOf([]byte("x"))))
	_, err := w.runWith(w.block(`, "registerAs": "churn"`))
	if err == nil || !strings.Contains(err.Error(), "declares a model file (xgboost.json) that the step did not leave") || len(w.ml.Models["churn"]) != 0 {
		t.Errorf("%v %v", err, w.ml.Models)
	}
}

func TestAFileAboveTheLimitIsLoggedWithoutAVersion(t *testing.T) {
	prev := maxModelFileBytes
	maxModelFileBytes = func() int64 { return 100 }
	t.Cleanup(func() { maxModelFileBytes = prev })
	w := newMLflowWorld(t)
	file := make([]byte, 101)
	w.files["model.json"], w.files["xgboost.json"] = boostCard(declares("xgboost.json", len(file), shaOf(file))), file
	id, err := w.runWith(w.block(`, "registerAs": "churn"`))
	if err != nil {
		t.Fatal(err)
	}
	run := w.ml.OnlyRun()
	if _, ok := w.ml.Artifacts["1/"+run.ID+"/artifacts/model/xgboost.json"]; ok || len(w.ml.Models["churn"]) != 0 {
		t.Errorf("the file or a version is in MLflow: %v", w.ml.Models)
	}
	c, m := w.columns(id)
	if c.errText == nil || !strings.Contains(fmt.Sprint(m["warnings"]), "larger than a model file may be") || w.row(id).status != "SUCCESS" {
		t.Errorf("%+v %v", c, m)
	}
}

// ── the stream, without a database ───────────────────────────────────────────

func streamWorld(t *testing.T, size int64, handler http.Handler) (*queue, *predictStep, *scriptStorage, *mlflowtest.Server) {
	t.Helper()
	ml := mlflowtest.New(t)
	ml.Stream("1/run1/artifacts/model/xgboost.json", size)
	srv := httptest.NewServer(handler)
	t.Cleanup(srv.Close)
	e, err := engine.New(srv.URL, modelsTestFernetKey)
	if err != nil {
		t.Fatal(err)
	}
	s := &predictStep{modelStep: modelStep{label: "Churn", predict: true},
		conn:    &mlflowConn{cfg: mlflowconn.Config{URI: ml.URL, Auth: "none", VerifyTLS: true, Timeout: 5 * time.Second}},
		version: &mlflowconn.ModelVersion{Name: "churn", Version: "1", Source: "mlflow-artifacts:/1/run1/artifacts/model"},
		file:    &cardModelFile{Name: "xgboost.json", Bytes: size, SHA256: mlflowtest.PatternSHA256(size)}}
	return &queue{d: &Deps{Engine: e}}, s, &scriptStorage{Bucket: "scripts", AccessKey: "k", SecretEnc: "enc"}, ml
}

// A file of 24 MiB goes from the fake MLflow to the fake engine without being held by the
// gateway: what the process allocates while it passes stays far below the file's size.
func TestAModelFileIsStreamedToTheEngineNotHeld(t *testing.T) {
	const size = 24 << 20
	engineSide := &uploadEngine{} // hashes and discards
	q, s, storage, _ := streamWorld(t, size, engineSide)
	var before, after runtime.MemStats
	runtime.GC()
	runtime.ReadMemStats(&before)
	if err := q.stageModelFile(t.Context(), s, storage, "env-1/step/model_file"); err != nil {
		t.Fatal(err)
	}
	runtime.ReadMemStats(&after)
	if engineSide.calls != 1 || engineSide.seen[0].size != size || engineSide.seen[0].sha != s.file.SHA256 || len(engineSide.objects["env-1/step/model_file"]) != 0 {
		t.Fatalf("the engine saw: %+v", engineSide.seen)
	}
	grown := after.TotalAlloc - before.TotalAlloc
	t.Logf("allocated while %d MiB went through: %d KiB", size>>20, grown>>10)
	if grown > size/4 {
		t.Errorf("%d bytes allocated for a file of %d: it was held", grown, size)
	}
}

func TestTheStreamSaysWhatWentWrong(t *testing.T) {
	const size = 300_000
	for _, c := range []struct {
		name   string
		status int
		bytes  int64 // what the card says (the file is `size` long)
		want   string
	}{
		{"the engine finds another hash", http.StatusConflict, size, "does not match the fingerprint its card keeps"},
		{"the engine finds it too large", http.StatusRequestEntityTooLarge, size, "is larger than a model file may be"},
		{"the engine's bucket refuses", http.StatusBadGateway, size, "could not be written to the scripts bucket: the engine's reason"},
		{"MLflow's file is shorter than the card says", 0, size + 5000, "does not match the fingerprint its card keeps"},
	} {
		t.Run(c.name, func(t *testing.T) {
			engineSide := &uploadEngine{status: c.status}
			q, s, storage, _ := streamWorld(t, size, engineSide)
			s.file.Bytes = c.bytes
			err := q.stageModelFile(t.Context(), s, storage, "env-1/step/model_file")
			if err == nil || !strings.Contains(err.Error(), c.want) || !strings.HasPrefix(err.Error(), "Predict «Churn»: ") {
				t.Errorf("%v", err)
			}
		})
	}
	// MLflow without the file, or too large by its length
	engineSide := &uploadEngine{}
	q, s, storage, ml := streamWorld(t, size, engineSide)
	s.file.Name = "catboost.cbm"
	if err := q.stageModelFile(t.Context(), s, storage, "k"); err == nil || !strings.Contains(err.Error(), "has no model file among its files (catboost.cbm)") {
		t.Errorf("missing: %v", err)
	}
	s.file.Name = "xgboost.json"
	prev := maxModelFileBytes
	maxModelFileBytes = func() int64 { return size - 1 }
	t.Cleanup(func() { maxModelFileBytes = prev })
	if err := q.stageModelFile(t.Context(), s, storage, "k"); err == nil || !strings.Contains(err.Error(), "is larger than a model file may be") || engineSide.calls != 0 {
		t.Errorf("too large: %v (%d uploads)", err, engineSide.calls)
	}
	// MLflow refusing the connection's credentials: said without the secret
	maxModelFileBytes = prev
	ml.Authorization = "Bearer right"
	s.conn.cfg.Auth, s.conn.cfg.Secret = "bearer", "wrong-secret"
	if err := q.stageModelFile(t.Context(), s, storage, "k"); err == nil || !strings.Contains(err.Error(), "401") || strings.Contains(err.Error(), "wrong-secret") ||
		strings.Contains(err.Error(), mlflowtest.Leak) || engineSide.calls != 0 {
		t.Errorf("unauthorized: %v", err)
	}
}

var _ = io.EOF
