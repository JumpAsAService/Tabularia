package api

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescompile"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/secret"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

// The compile loop (docs/specs/bayes-stan-compile.md, lot C2) on TEST_DATABASE_URL, the cluster played by fakeKube and
// the engine by fakeCompileEngine: every row of «Finishing a compile», the keep's integrity, the cache, the queue's
// rules with two gateways.

func sha(b []byte) string {
	s := sha256.Sum256(b)
	return hex.EncodeToString(s[:])
}

// fakeCompileEngine is the engine's part the loop and the Compiler call: compile.json read from the scripts bucket, the
// binary kept, placed for a run, deleted, the step's files deleted.
type fakeCompileEngine struct {
	mu      sync.Mutex
	bucket  map[string][]byte // the scripts bucket
	storage map[string][]byte // Tabularia's: bayes-binaries/<sha256>
	lose    int               // the next keeps done but answered 500 (a gateway that died after the keep)
	down    bool              // the engine does not answer the keep
	keeps   int
	evicted []string // binary/delete's sha256s
	cleaned []string // objects/delete's keys
}

func (e *fakeCompileEngine) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	e.mu.Lock()
	defer e.mu.Unlock()
	var body map[string]any
	_ = json.NewDecoder(r.Body).Decode(&body)
	reply := func(status int, v any) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(status)
		_ = json.NewEncoder(w).Encode(v)
	}
	switch r.URL.Path {
	case "/scripts/objects/read":
		b, ok := e.bucket[body["key"].(string)]
		if !ok {
			reply(404, map[string]any{"detail": "Not in the scripts bucket"})
			return
		}
		w.Header().Set("Content-Length", fmt.Sprint(len(b)))
		_, _ = w.Write(b)
	case "/scripts/binary/keep":
		e.keeps++
		if e.down {
			reply(502, map[string]any{"detail": "Tabularia's storage answered: down"})
			return
		}
		b, ok := e.bucket[body["key"].(string)]
		if !ok {
			reply(404, map[string]any{"detail": "missing", "missing": true})
			return
		}
		if int64(len(b)) > int64(body["max_bytes"].(float64)) || sha(b) != body["sha256"] {
			reply(409, map[string]any{"detail": "The binary's sha256 is not the one the compile reported: not kept", "altered": true})
			return
		}
		_, existed := e.storage["bayes-binaries/"+sha(b)]
		e.storage["bayes-binaries/"+sha(b)] = b
		if e.lose > 0 {
			e.lose--
			reply(500, map[string]any{"detail": "Internal Server Error"})
			return
		}
		reply(200, map[string]any{"bytes": len(b), "existed": existed})
	case "/scripts/binary/place":
		b, ok := e.storage["bayes-binaries/"+body["sha256"].(string)]
		switch {
		case !ok:
			reply(404, map[string]any{"detail": "The binary is not in Tabularia's storage", "missing": true})
		case sha(b) != body["sha256"]:
			reply(409, map[string]any{"detail": "The stored binary is not the one recorded", "altered": true})
		default:
			e.bucket[body["target_key"].(string)] = b
			reply(200, map[string]any{"bytes": len(b)})
		}
	case "/scripts/binary/delete":
		for _, s := range body["sha256s"].([]any) {
			e.evicted = append(e.evicted, s.(string))
			delete(e.storage, "bayes-binaries/"+s.(string))
		}
		reply(200, map[string]any{"deleted": len(body["sha256s"].([]any)), "failed": []any{}})
	case "/scripts/objects/delete":
		for _, k := range body["keys"].([]any) {
			e.cleaned = append(e.cleaned, k.(string))
			delete(e.bucket, k.(string))
		}
		reply(200, map[string]any{"deleted": len(body["keys"].([]any)), "failed": []any{}})
	default:
		reply(404, map[string]any{"detail": "no route " + r.URL.Path})
	}
}

type compileWorld struct {
	*modelsFixture
	engine  *fakeCompileEngine
	c       *bayesCompiles
	digest  string
	program string // hier_slopes, the golden program
	prog    string // its sha256
	info    []byte // stanc 2.40.0's --info of it
	model   int64
	project int64
	data    string // its manifests, as a version stores them
	quants  string
	mapping string
}

func golden(t *testing.T, name string) []byte {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "bayescodegen", "testdata", name))
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func newCompileWorld(t *testing.T) *compileWorld {
	t.Helper()
	f := newModelsFixture(t)
	t.Setenv("MODELS__ENABLED", "")
	w := &compileWorld{modelsFixture: f, engine: &fakeCompileEngine{bucket: map[string][]byte{}, storage: map[string][]byte{}},
		digest: "sha256:" + strings.Repeat("d", 64)}
	exec := func(sql string, args ...any) {
		t.Helper()
		if _, err := f.db.Exec(f.ctx, sql, args...); err != nil {
			t.Fatalf("%s: %v", sql, err)
		}
	}
	exec(`DELETE FROM bayes_binaries`)
	exec(`DELETE FROM bayes_settings`)
	// the group bayes, ready, its image by digest
	shipped, ok := modelgroups.Get("bayes")
	if !ok {
		t.Fatal("no group bayes")
	}
	var env, version int64
	if err := f.db.QueryRow(f.ctx, `INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit,
		memory_request, memory_limit, max_timeout_seconds, created_at, updated_at, managed_by) VALUES ($1, '', 'python', true, '1', '4',
		'1Gi', '2Gi', 3600, now(), now(), 'model_group:bayes') RETURNING id`, fmt.Sprintf("Bayes %d", time.Now().UnixNano())).Scan(&env); err != nil {
		t.Fatal(err)
	}
	if err := f.db.QueryRow(f.ctx, `INSERT INTO script_environment_versions (environment_id, number, language_version, base_image, packages,
		status, image_ref, arch, recipe_hash, runner_hash, created_at) VALUES ($1, 2, '3.14', 'python:3.14-slim', '', 'ready', $2, 'amd64', $3,
		$4, now()) RETURNING id`, env, "registry:5000/envs/tabularia-env-1@"+w.digest, shipped.RecipeHash, shipped.RunnerHash).Scan(&version); err != nil {
		t.Fatal(err)
	}
	exec(`UPDATE script_environments SET current_version_id = $1 WHERE id = $2`, version, env)
	exec(`INSERT INTO model_groups (id, environment_id, enabled, everyone, updated_at) VALUES ('bayes', $1, true, true, now())`, env)
	// the scripts bucket, its key encrypted as the administration writes it
	fernet, err := secret.NewFernet(modelsTestFernetKey)
	if err != nil {
		t.Fatal(err)
	}
	enc, err := fernet.Encrypt("s3cret")
	if err != nil {
		t.Fatal(err)
	}
	exec(`INSERT INTO script_storage (id, endpoint, region, bucket, access_key, secret_key_enc, updated_at, updated_by)
		VALUES (1, 'http://minio:9000', '', 'scripts-bucket', 'k', $1, now(), 'test')
		ON CONFLICT (id) DO UPDATE SET endpoint = 'http://minio:9000', bucket = 'scripts-bucket', access_key = 'k', secret_key_enc = $1`, enc)
	srv := httptest.NewServer(w.engine)
	t.Cleanup(srv.Close)
	e, err := engine.New(srv.URL, modelsTestFernetKey)
	if err != nil {
		t.Fatal(err)
	}
	cfg := &config.Config{EngineBucket: "data-prep", ScriptsEnabled: true, ScriptsNamespace: "scripts"}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	d := *f.d
	d.Engine, d.Fernet, d.Config, d.Panel, d.Log = e, fernet, cfg, settings.New(cfg, false, log), log
	w.c = &bayesCompiles{d: &d, s: scriptcfg.Load(config.FromOS())}
	// a saved model holding the golden hier_slopes program
	w.program = string(golden(t, "golden/hier_slopes.stan"))
	w.prog = sha([]byte(w.program))
	w.info = golden(t, "hier_slopes.info.json")
	var manifest struct {
		ProgramSHA256 string          `json:"program_sha256"`
		Data          json.RawMessage `json:"data"`
		Quantities    json.RawMessage `json:"quantities"`
	}
	if err := json.Unmarshal(golden(t, "golden/hier_slopes.manifest.json"), &manifest); err != nil || manifest.ProgramSHA256 != w.prog {
		t.Fatalf("the golden manifest: %v (%s)", err, manifest.ProgramSHA256)
	}
	w.data, w.quants, w.mapping = string(manifest.Data), string(manifest.Quantities), string(golden(t, "golden/hier_slopes.mapping.json"))
	if err := f.db.QueryRow(f.ctx, `INSERT INTO projects (name, description, created_at) VALUES ($1, '', now()) RETURNING id`,
		fmt.Sprintf("bc-%d", time.Now().UnixNano())).Scan(&w.project); err != nil {
		t.Fatal(err)
	}
	w.model = w.newModel("Slopes")
	w.version(w.model, 1, w.program)
	t.Cleanup(func() {
		for _, sql := range []string{`DELETE FROM bayes_binaries`, `DELETE FROM bayes_settings`,
			`DELETE FROM bayes_model_versions WHERE model_id IN (SELECT id FROM bayes_models WHERE project_id = $1)`,
			`DELETE FROM bayes_models WHERE project_id = $1`, `DELETE FROM projects WHERE id = $1`, `DELETE FROM script_storage WHERE id = 1`} {
			if strings.Contains(sql, "$1") {
				_, _ = f.db.Exec(f.ctx, sql, w.project)
			} else {
				_, _ = f.db.Exec(f.ctx, sql)
			}
		}
	})
	return w
}

func (w *compileWorld) newModel(name string) int64 {
	w.t.Helper()
	var id int64
	if err := w.db.QueryRow(w.ctx, `INSERT INTO bayes_models (name, description, project_id, owner_id, current_version, created_at, updated_at)
		VALUES ($1, '', $2, $3, 1, now(), now()) RETURNING id`, name, w.project, w.admin.ID).Scan(&id); err != nil {
		w.t.Fatal(err)
	}
	return id
}

// version stores a version of the model holding the program, with hier_slopes' manifests; its sha256.
func (w *compileWorld) version(model, version int64, program string) string {
	w.t.Helper()
	if _, err := w.db.Exec(w.ctx, `INSERT INTO bayes_model_versions (model_id, version, graph, format_version, semantic_hash, findings,
		program, program_sha256, mapping, data_manifest, quantity_manifest, codegen_version, family, catalog_sha256, note, created_at)
		VALUES ($1, $2, '{}', 1, $3, '[]', $4, $5, $6, $7, $8, 1, 'hierarchical', $9, 'created', now())`, model, version,
		strings.Repeat("e", 64), program, sha([]byte(program)), w.mapping, w.data, w.quants, strings.Repeat("c", 64)); err != nil {
		w.t.Fatal(err)
	}
	return sha([]byte(program))
}

// queue writes the row a save of the version asks for, as the Compiler (lot C3) will: NewRow, PlanEnqueue, Insert.
func (w *compileWorld) queue(by, model, version int64, program string, at time.Time) int64 {
	w.t.Helper()
	row, err := bayescompile.NewRow(program, bayescompile.Image{CmdStan: "2.40.0", Arch: "amd64", Digest: w.digest}, 2)
	if err != nil {
		w.t.Fatal(err)
	}
	p := bayescompile.PlanEnqueue(row, bayesmodels.CompileRequest{ModelID: model, Version: version, ProgramSHA256: program, By: by},
		nil, bayescompile.Limits{}, at)
	if !p.Queue {
		w.t.Fatalf("not queued: %+v", p)
	}
	id, err := bayescompile.Insert(w.ctx, w.db, p.Row)
	if err != nil {
		w.t.Fatal(err)
	}
	return id
}

func (w *compileWorld) row(id int64) bayescompile.Row {
	w.t.Helper()
	r, err := bayescompile.Scan(w.db.QueryRow(w.ctx, `SELECT `+bayescompile.Columns+` FROM bayes_binaries WHERE id = $1`, id))
	if err != nil {
		w.t.Fatalf("row %d: %v", id, err)
	}
	return r
}

func (w *compileWorld) gone(id int64) bool {
	var n int
	_ = w.db.QueryRow(w.ctx, `SELECT count(*) FROM bayes_binaries WHERE id = $1`, id).Scan(&n)
	return n == 0
}

func (w *compileWorld) round() int64 {
	w.t.Helper()
	n, err := w.c.oneRound(w.ctx)
	if err != nil {
		w.t.Fatal(err)
	}
	return n
}

// ── what a compile Job leaves ─────────────────────────────────────────────────

var binaryBytes = append([]byte("\x7fELF"), make([]byte, 3000)...)

// compileJSON is artifacts/compile.json as the runner writes it (lot K3), with info as given.
func compileJSON(program string, binary []byte, info []byte) []byte {
	b, _ := json.Marshal(map[string]any{"program_sha256": program, "binary_sha256": sha(binary), "binary_bytes": len(binary),
		"info": json.RawMessage(info), "stanc": "stanc3 v2.40.0 (Unix)", "seconds": 12.5, "warnings": []string{"Info: a warning"}})
	return b
}

func message(program, binary, info string) string {
	return fmt.Sprintf(`{"program_sha256": %q, "binary_sha256": %q, "info_sha256": %q}`, program, binary, info)
}

func ranState(exit int, reason, msg string) map[string]any {
	t := map[string]any{"exitCode": exit, "reason": reason, "startedAt": "2026-11-24T10:00:00Z", "finishedAt": "2026-11-24T10:00:12Z"}
	if msg != "" {
		t["message"] = msg
	}
	return map[string]any{"containerStatuses": []any{map[string]any{"name": "model", "state": map[string]any{"terminated": t}}}}
}

var (
	jobComplete = map[string]any{"succeeded": 1, "conditions": []any{map[string]any{"type": "Complete", "status": "True"}}}
	jobFailed   = map[string]any{"failed": 1, "conditions": []any{map[string]any{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}}}
	jobDeadline = map[string]any{"failed": 1, "conditions": []any{map[string]any{"type": "Failed", "status": "True",
		"reason": "DeadlineExceeded", "message": "Job was active longer than specified deadline"}}}
)

// ends makes the Job end as the test says: the Job's status, its Pod's status, its log.
func (w *compileWorld) ends(job string, jobStatus, podStatus map[string]any, log string) {
	k := w.kube
	k.mu.Lock()
	defer k.mu.Unlock()
	k.jobStatus[job], k.podStatus[job] = jobStatus, podStatus
	k.ends[job] = fakeEnd{ok: jobStatus["succeeded"] != nil, log: log}
}

// compiled makes the row's Job succeed: the binary and compile.json in its step, the termination message given.
func (w *compileWorld) compiled(row bayescompile.Row, binary, cj []byte, msg string) {
	prefix := bayescompile.StepPrefix(row.ID, row.Attempt)
	w.engine.mu.Lock()
	w.engine.bucket[prefix+"model"], w.engine.bucket[prefix+"artifacts/compile.json"] = binary, cj
	w.engine.mu.Unlock()
	w.ends(row.JobName, jobComplete, ranState(0, "Completed", msg), "compiling\n")
}

// succeed is compiled with what a good compile of the row's program leaves (every program here has hier_slopes'
// manifests, and so its --info).
func (w *compileWorld) succeed(row bayescompile.Row, binary []byte) {
	cj := compileJSON(row.ProgramSHA256, binary, w.info)
	w.compiled(row, binary, cj, message(row.ProgramSHA256, sha(binary), sha(cj)))
}

func (w *compileWorld) cleaned(row bayescompile.Row) bool {
	w.engine.mu.Lock()
	defer w.engine.mu.Unlock()
	for _, k := range bcStepKeys(bayescompile.StepPrefix(row.ID, row.Attempt)) {
		if !slices.Contains(w.engine.cleaned, k) {
			return false
		}
	}
	return true
}

func path(v any, keys ...any) any {
	for _, k := range keys {
		switch key := k.(type) {
		case string:
			m, _ := v.(map[string]any)
			v = m[key]
		case int:
			l, _ := v.([]any)
			if key >= len(l) {
				return nil
			}
			v = l[key]
		}
	}
	return v
}

// ── the tests ─────────────────────────────────────────────────────────────────

// A queued program starts its Job (the manifest the spec gives), and a Job that ends well makes it ready with the
// termination message's sha256, the binary kept by the engine, the Job and its step's files gone.
func TestBayesCompileStartsAJobAndKeepsTheBinary(t *testing.T) {
	w := newCompileWorld(t)
	id := w.queue(w.ann.ID, w.model, 1, w.prog, time.Now())
	if n := w.round(); n != 1 {
		t.Fatalf("round 1: %d", n)
	}
	row := w.row(id)
	if row.State != bayescompile.StateRunning || row.JobName != fmt.Sprintf("bayes-compile-%d-0", id) || row.DeadlineSeconds != 300 ||
		row.MemoryBytes != 3<<30 || row.StartedAt.IsZero() {
		t.Fatalf("%+v", row)
	}
	job := w.kube.job("scripts", row.JobName)
	if job == nil {
		t.Fatal("no Job")
	}
	spec := path(job, "spec", "template", "spec")
	c := path(spec, "containers", 0)
	checks := map[string][2]any{
		"image":            {path(c, "image"), "registry:5000/envs/tabularia-env-1@" + w.digest},
		"command":          {fmt.Sprint(path(c, "command")), "[python -I -B /tabularia/code/runner/run.py]"},
		"cpu request":      {path(c, "resources", "requests", "cpu"), "1"},
		"cpu limit":        {path(c, "resources", "limits", "cpu"), "1"},
		"memory request":   {path(c, "resources", "requests", "memory"), "3Gi"},
		"memory limit":     {path(c, "resources", "limits", "memory"), "3Gi"},
		"deadline":         {path(job, "spec", "activeDeadlineSeconds"), float64(300)},
		"backoff":          {path(job, "spec", "backoffLimit"), float64(0)},
		"tmp":              {path(spec, "volumes", 1, "emptyDir", "sizeLimit"), "512Mi"},
		"uid":              {path(spec, "securityContext", "runAsUser"), float64(10001)},
		"non-root":         {path(spec, "securityContext", "runAsNonRoot"), true},
		"seccomp":          {path(spec, "securityContext", "seccompProfile", "type"), "RuntimeDefault"},
		"read-only root":   {path(c, "securityContext", "readOnlyRootFilesystem"), true},
		"capabilities":     {fmt.Sprint(path(c, "securityContext", "capabilities", "drop")), "[ALL]"},
		"no token":         {path(spec, "automountServiceAccountToken"), false},
		"message policy":   {path(c, "terminationMessagePolicy"), "File"},
		"message path":     {path(c, "terminationMessagePath"), "/dev/termination-log"},
		"offline":          {path(job, "spec", "template", "metadata", "labels", "tabularia.io/internet"), "off"},
		"architecture":     {path(spec, "nodeSelector", "kubernetes.io/arch"), "amd64"},
		"compile label":    {path(job, "metadata", "labels", "tabularia.io/bayes-compile"), fmt.Sprint(id)},
		"restart":          {path(spec, "restartPolicy"), "Never"},
		"no privilege":     {path(c, "securityContext", "allowPrivilegeEscalation"), false},
		"code is readonly": {path(c, "volumeMounts", 0, "readOnly"), true},
	}
	for what, c := range checks {
		if fmt.Sprint(c[0]) != fmt.Sprint(c[1]) {
			t.Errorf("%s: %v, want %v", what, c[0], c[1])
		}
	}
	env := map[string]any{}
	for _, e := range path(c, "env").([]any) {
		env[path(e, "name").(string)] = path(e, "value")
	}
	prefix := bayescompile.StepPrefix(id, 0)
	if env["TABULARIA_BINARY_OUT"] != "s3://scripts-bucket/"+prefix+"model" || env["TABULARIA_ARTIFACTS"] != "s3://scripts-bucket/"+prefix+"artifacts/" ||
		env["HOME"] != "/tmp" || env["AWS_ENDPOINT_URL"] != "http://minio:9000" {
		t.Errorf("env %v", env)
	}
	for name := range env {
		if strings.Contains(strings.ToLower(name), "secret") {
			t.Errorf("a secret in the environment: %s", name)
		}
	}
	// the ConfigMap: the runner, task.json (the model alone, no ids, the compile's limits) and the stored program's bytes
	k := w.kube
	k.mu.Lock()
	cm, sec := k.configmaps["scripts/"+row.JobName+"-code"], k.secrets["scripts/"+row.JobName+"-env"]
	k.mu.Unlock()
	if cm == nil || sec == nil {
		t.Fatalf("ConfigMap %v, Secret %v", cm != nil, sec != nil)
	}
	if path(sec, "stringData", "AWS_SECRET_ACCESS_KEY") != "s3cret" || path(cm, "metadata", "ownerReferences", 0, "name") != row.JobName {
		t.Errorf("the Secret, or the ConfigMap's owner")
	}
	files := map[string]string{}
	for _, it := range path(spec, "volumes", 0, "configMap", "items").([]any) {
		files[path(it, "path").(string)] = path(cm, "data", path(it, "key").(string)).(string)
	}
	if files["program.stan"] != w.program || files["runner/run.py"] == "" {
		t.Errorf("the program in the ConfigMap is not the stored one (%d bytes)", len(files["program.stan"]))
	}
	var task map[string]any
	if err := json.Unmarshal([]byte(files["task.json"]), &task); err != nil {
		t.Fatal(err)
	}
	if path(task, "mode") != "compile" || fmt.Sprint(path(task, "ids")) != "map[]" || path(task, "limits", "time_budget_seconds") != float64(300) ||
		path(task, "limits", "memory_bytes") != float64(3<<30) || path(task, "limits", "tmp_bytes") != float64(512<<20) ||
		path(task, "bayes", "model", "program_sha256") != w.prog || path(task, "bayes", "model", "binary_sha256") != nil ||
		path(task, "bayes", "model", "cmdstan") != "2.40.0" || path(task, "bayes", "model", "id") != float64(w.model) {
		t.Errorf("task.json: %s", files["task.json"])
	}
	// a Job that runs changes nothing
	if n := w.round(); n != 0 || w.row(id).State != bayescompile.StateRunning {
		t.Fatalf("a running compile: %d %s", n, w.row(id).State)
	}
	// it ends: ready, from the termination message
	w.succeed(row, binaryBytes)
	if n := w.round(); n != 1 {
		t.Fatalf("the end: %d", n)
	}
	row = w.row(id)
	if row.State != bayescompile.StateReady || row.BinarySHA256 != sha(binaryBytes) || row.BinaryBytes != int64(len(binaryBytes)) ||
		row.Seconds != 12.5 || row.Warnings != `["Info: a warning"]` || row.LastUsedAt.IsZero() || row.FinishedAt.IsZero() {
		t.Fatalf("%+v", row)
	}
	if b := w.engine.storage["bayes-binaries/"+sha(binaryBytes)]; string(b) != string(binaryBytes) {
		t.Error("the binary is not in Tabularia's storage under its sha256")
	}
	if w.kube.job("scripts", row.JobName) != nil || !w.cleaned(row) {
		t.Error("the Job or its step's files are still there")
	}
}

// Every row of «Finishing a compile» but the keep: each ends the row as the spec says, the Job and the step's files go,
// nothing is kept.
func TestBayesCompileEndings(t *testing.T) {
	w := newCompileWorld(t)
	other := strings.Repeat("9", 64)
	good := func(row bayescompile.Row) []byte { return compileJSON(row.ProgramSHA256, binaryBytes, w.info) }
	evicted := ranState(137, "Error", "")
	evicted["reason"] = "Evicted"
	cases := []struct {
		name   string
		end    func(row bayescompile.Row)
		state  string
		reason string
	}{
		{"a message of another program", func(row bayescompile.Row) {
			cj := good(row)
			w.compiled(row, binaryBytes, cj, message(other, sha(binaryBytes), sha(cj)))
		}, bayescompile.StateFailed, bayescompile.ReasonJob},
		{"a message longer than 4,096 bytes", func(row bayescompile.Row) {
			cj := good(row)
			w.compiled(row, binaryBytes, cj, message(row.ProgramSHA256, sha(binaryBytes), sha(cj))+strings.Repeat(" ", 4096))
		}, bayescompile.StateFailed, bayescompile.ReasonJob},
		{"a message that is not lowercase hex", func(row bayescompile.Row) {
			cj := good(row)
			w.compiled(row, binaryBytes, cj, message(row.ProgramSHA256, strings.ToUpper(sha(binaryBytes)), sha(cj)))
		}, bayescompile.StateFailed, bayescompile.ReasonJob},
		{"no message (a line of the log is not one)", func(row bayescompile.Row) {
			cj := good(row)
			w.compiled(row, binaryBytes, cj, "")
			w.ends(row.JobName, jobComplete, ranState(0, "Completed", ""), message(row.ProgramSHA256, sha(binaryBytes), sha(cj))+"\n")
		}, bayescompile.StateFailed, bayescompile.ReasonJob},
		{"compile.json that the message does not vouch for", func(row bayescompile.Row) {
			cj := good(row)
			w.compiled(row, binaryBytes, append(cj, ' '), message(row.ProgramSHA256, sha(binaryBytes), sha(cj)))
		}, bayescompile.StateFailed, bayescompile.ReasonJob},
		{"a cross-check mismatch", func(row bayescompile.Row) {
			cj := compileJSON(row.ProgramSHA256, binaryBytes, []byte(strings.Replace(string(w.info), `"mu_a"`, `"mu_z"`, 1)))
			w.compiled(row, binaryBytes, cj, message(row.ProgramSHA256, sha(binaryBytes), sha(cj)))
		}, bayescompile.StateFailed, bayescompile.ReasonCrossCheck},
		{"a binary in the bucket that is not the message's", func(row bayescompile.Row) {
			cj := good(row)
			w.compiled(row, append([]byte("poisoned"), binaryBytes...), cj, message(row.ProgramSHA256, sha(binaryBytes), sha(cj)))
		}, bayescompile.StateFailed, bayescompile.ReasonJob},
		{"the deadline", func(row bayescompile.Row) {
			w.ends(row.JobName, jobDeadline, ranState(137, "Error", ""), "compiling\n")
		}, bayescompile.StateTimeout, bayescompile.ReasonDeadline},
		{"killed for memory", func(row bayescompile.Row) {
			w.ends(row.JobName, jobFailed, ranState(137, "OOMKilled", ""), "cc1plus: out of memory\n")
		}, bayescompile.StateTimeout, bayescompile.ReasonMemory},
		{"the compiler refused the program", func(row bayescompile.Row) {
			w.ends(row.JobName, jobFailed, ranState(3, "Error", ""),
				"make: *** error\nTABULARIA_ERROR {\"code\": \"compile_failed\", \"message\": \"the generated program did not compile\", \"params\": {}}\n")
		}, bayescompile.StateFailed, bayescompile.ReasonCompileFailed},
		{"the program was not the stored one", func(row bayescompile.Row) {
			w.ends(row.JobName, jobFailed, ranState(3, "Error", ""),
				"TABULARIA_ERROR {\"code\": \"program_hash\", \"message\": \"the program is not the one this version saved\", \"params\": {}}\n")
		}, bayescompile.StateFailed, bayescompile.ReasonProgramHash},
		{"any other exit", func(row bayescompile.Row) {
			w.ends(row.JobName, jobFailed, ranState(1, "Error", ""), "Traceback\n")
		}, bayescompile.StateFailed, bayescompile.ReasonJob},
		// the cluster's: queued again (and started again in the same round), by people of their own
		{"a Pod that never started within the deadline", func(row bayescompile.Row) {
			w.ends(row.JobName, jobDeadline, map[string]any{"containerStatuses": []any{map[string]any{"name": "model",
				"state": map[string]any{"waiting": map[string]any{"reason": "ContainerCreating"}}}}}, "")
		}, bayescompile.StateQueued, ""},
		{"evicted", func(row bayescompile.Row) {
			w.ends(row.JobName, jobFailed, evicted, "")
		}, bayescompile.StateQueued, ""},
	}
	for i, c := range cases {
		model := w.newModel(fmt.Sprintf("Case %d", i))
		program := w.version(model, 1, w.program+fmt.Sprintf("\n// case %d\n", i))
		by := w.ann.ID
		switch c.name {
		case "a Pod that never started within the deadline":
			by = w.observer.ID
		case "evicted":
			by = w.admin.ID
		}
		id := w.queue(by, model, 1, program, time.Now())
		w.round()
		row := w.row(id)
		if row.State != bayescompile.StateRunning {
			t.Fatalf("%s: not started: %+v", c.name, row)
		}
		c.end(row)
		w.round()
		got := w.row(id)
		switch {
		case c.state == bayescompile.StateQueued:
			if got.State != bayescompile.StateRunning || got.Attempt != 1 {
				t.Errorf("%s: %s attempt %d, want started again", c.name, got.State, got.Attempt)
			}
		case got.State != c.state || got.Reason != c.reason:
			t.Errorf("%s: %s/%s (%s), want %s/%s", c.name, got.State, got.Reason, got.Detail, c.state, c.reason)
		case got.State == bayescompile.StateTimeout && (got.DeadlineSeconds != 300 || got.MemoryBytes != 3<<30):
			t.Errorf("%s: the limits it ran under: %+v", c.name, got)
		}
		if w.kube.job("scripts", row.JobName) != nil || !w.cleaned(row) {
			t.Errorf("%s: the Job or the step's files stayed", c.name)
		}
		if len(w.engine.storage) != 0 {
			t.Errorf("%s: something was kept: %d", c.name, len(w.engine.storage))
		}
	}
}

// The cluster that fails a compile three times: queued again twice, keeping its place, then failed with its reason; a
// cluster that never creates the Pod is one of these failures.
func TestBayesCompileTheClusterFailsItThreeTimes(t *testing.T) {
	w := newCompileWorld(t)
	id := w.queue(w.ann.ID, w.model, 1, w.prog, time.Now().Add(-time.Hour))
	queued := w.row(id).QueuedAt
	evict := func(row bayescompile.Row) {
		pod := ranState(137, "Error", "")
		pod["reason"] = "Evicted"
		w.ends(row.JobName, jobFailed, pod, "")
	}
	w.round()
	evict(w.row(id))
	w.round() // evicted: queued again, and started again in the same round (its place kept)
	second := w.row(id)
	if second.State != bayescompile.StateRunning || second.Attempt != 1 || second.JobName != fmt.Sprintf("bayes-compile-%d-1", id) ||
		!second.QueuedAt.Equal(queued) {
		t.Fatalf("after the first failure: %+v", second)
	}
	// the cluster does not create the second Job's Pod: refused after a while
	w.kube.refuse(second.JobName, "exceeded quota: compute-resources")
	w.kube.older("scripts", second.JobName, 20)
	w.round()
	third := w.row(id)
	if third.State != bayescompile.StateRunning || third.Attempt != 2 {
		t.Fatalf("after the second failure: %+v", third)
	}
	evict(third)
	w.round()
	if got := w.row(id); got.State != bayescompile.StateFailed || got.Reason != bayescompile.ReasonCluster ||
		!strings.Contains(got.Detail, "3 attempts") {
		t.Fatalf("after the third failure: %+v", got)
	}
	for _, name := range []string{w.row(id).JobName, second.JobName, third.JobName} {
		if name != "" && w.kube.job("scripts", name) != nil {
			t.Errorf("the Job %s stayed", name)
		}
	}
}

// A gateway that dies after the engine kept the binary and before the row was written: the next round keeps it again
// (the engine finds it there) and the row becomes ready once.
func TestBayesCompileAKeepDoneTwiceIsReadyOnce(t *testing.T) {
	w := newCompileWorld(t)
	id := w.queue(w.ann.ID, w.model, 1, w.prog, time.Now())
	w.round()
	w.succeed(w.row(id), binaryBytes)
	w.engine.lose = 1
	w.round()
	if got := w.row(id); got.State != bayescompile.StateRunning {
		t.Fatalf("the answer lost: %+v", got)
	}
	if w.kube.job("scripts", w.row(id).JobName) == nil {
		t.Fatal("the Job went before its end was recorded")
	}
	w.round()
	if got := w.row(id); got.State != bayescompile.StateReady || got.BinarySHA256 != sha(binaryBytes) {
		t.Fatalf("the second round: %+v", got)
	}
	if w.engine.keeps != 2 || len(w.engine.storage) != 1 {
		t.Errorf("keeps %d, objects %d", w.engine.keeps, len(w.engine.storage))
	}
	// the engine down: the row waits, nothing else changes
	m := w.newModel("Other")
	id2 := w.queue(w.observer.ID, m, 1, w.version(m, 1, w.program+"\n// other\n"), time.Now())
	w.round()
	w.succeed(w.row(id2), append(binaryBytes, 1))
	w.engine.down = true
	w.round()
	if got := w.row(id2); got.State != bayescompile.StateRunning {
		t.Fatalf("the engine down: %+v", got)
	}
}

// One compile per person and «Compiles at once», with two gateways at the same time: never more running than the
// places, never two of one person; the order is queued_at.
func TestBayesCompileOnePersonAtATimeAndThePlaces(t *testing.T) {
	w := newCompileWorld(t)
	other := &bayesCompiles{d: w.c.d, s: w.c.s}
	t0 := time.Now().Add(-time.Hour)
	var ids []int64
	people := []int64{w.ann.ID, w.ann.ID, w.observer.ID, w.admin.ID}
	for i, by := range people {
		model := w.newModel(fmt.Sprintf("M%d", i))
		program := w.version(model, 1, w.program+fmt.Sprintf("\n// m%d\n", i))
		ids = append(ids, w.queue(by, model, 1, program, t0.Add(time.Duration(i)*time.Second)))
	}
	var wg sync.WaitGroup
	for _, c := range []*bayesCompiles{w.c, other, w.c, other} {
		wg.Add(1)
		go func(c *bayesCompiles) {
			defer wg.Done()
			_, _ = c.oneRound(w.ctx)
		}(c)
	}
	wg.Wait()
	w.round()
	states := func() []string {
		var out []string
		for _, id := range ids {
			out = append(out, w.row(id).State)
		}
		return out
	}
	// ann's first, then (ann waits) the observer's: two places
	if got := states(); fmt.Sprint(got) != "[running queued running queued]" {
		t.Fatalf("two gateways at once: %v", got)
	}
	w.succeed(w.row(ids[0]), binaryBytes)
	w.round()
	// ann's first is ready: her second takes the place before the administrator's (queued earlier)
	if got := states(); fmt.Sprint(got) != "[ready running running queued]" {
		t.Fatalf("after ann's first: %v", got)
	}
	// three places now: the administrator starts
	if _, err := w.db.Exec(w.ctx, `INSERT INTO bayes_settings (id, compile_slots, updated_at) VALUES (1, 3, now())`); err != nil {
		t.Fatal(err)
	}
	w.round()
	if got := states(); fmt.Sprint(got) != "[ready running running running]" {
		t.Fatalf("three places: %v", got)
	}
}

// The cache: a binary that does not fit evicts the least recently used (an older image's first); a binary larger than
// the whole limit is failed with its size and compiles again once the limit holds it; a lowered limit evicts at the
// next round; a stale queued row leaves; the cleaning takes an older image's failures nobody holds.
func TestBayesCompileTheCache(t *testing.T) {
	w := newCompileWorld(t)
	old := "sha256:" + strings.Repeat("0", 64)
	at := time.Now().Add(-48 * time.Hour)
	ready := func(key, image string, bytes int64, used time.Time, binary string) int64 {
		id, err := bayescompile.Insert(w.ctx, w.db, bayescompile.Row{CacheKey: key, ProgramSHA256: strings.Repeat("1", 64),
			CmdStan: "2.40.0", ImageDigest: image, Arch: "amd64", GroupVersion: 1, State: bayescompile.StateReady, BinarySHA256: binary,
			BinaryBytes: bytes, Seconds: 10, FinishedAt: used, LastUsedAt: used})
		if err != nil {
			t.Fatal(err)
		}
		return id
	}
	// the cache holds 10 kB: two binaries of 4 kB, the older image's used last
	if _, err := w.db.Exec(w.ctx, `INSERT INTO bayes_settings (id, cache_max_bytes, updated_at) VALUES (1, 10000, now())`); err != nil {
		t.Fatal(err)
	}
	lru := ready(strings.Repeat("a", 64), w.digest, 4000, at, strings.Repeat("5", 64))
	older := ready(strings.Repeat("b", 64), old, 4000, at.Add(time.Hour), strings.Repeat("6", 64))
	shared := ready(strings.Repeat("f", 64), w.digest, 900, at.Add(2*time.Hour), strings.Repeat("6", 64)) // the same bytes
	id := w.queue(w.ann.ID, w.model, 1, w.prog, time.Now())
	w.round()
	w.succeed(w.row(id), append(binaryBytes, make([]byte, 2000)...)) // 5004 bytes: 8900 + 5004 > 10000
	w.round()
	if got := w.row(id); got.State != bayescompile.StateReady {
		t.Fatalf("%+v", got)
	}
	// the older image's binary went first; its object stays, the current image's row holds the same bytes
	if !w.gone(older) || w.gone(lru) || w.gone(shared) || len(w.engine.evicted) != 0 {
		t.Fatalf("evicted: older %v lru %v shared %v, objects %v", w.gone(older), w.gone(lru), w.gone(shared), w.engine.evicted)
	}
	// the limit lowered: the next round evicts the least recently used until it fits
	if _, err := w.db.Exec(w.ctx, `UPDATE bayes_settings SET cache_max_bytes = 6500`); err != nil {
		t.Fatal(err)
	}
	w.round()
	if !w.gone(lru) || w.gone(id) || !slices.Contains(w.engine.evicted, strings.Repeat("5", 64)) {
		t.Fatalf("the lowered limit: lru %v, objects %v", w.gone(lru), w.engine.evicted)
	}
	// a binary larger than the whole limit: failed with its size, nothing kept
	big := w.newModel("Big")
	program := w.version(big, 1, w.program+"\n// big\n")
	id2 := w.queue(w.observer.ID, big, 1, program, time.Now())
	w.round()
	w.succeed(w.row(id2), make([]byte, 7000))
	w.round()
	got := w.row(id2)
	if got.State != bayescompile.StateFailed || got.Reason != bayescompile.ReasonTooLarge || got.BinaryBytes != 7000 ||
		w.engine.storage["bayes-binaries/"+sha(make([]byte, 7000))] != nil {
		t.Fatalf("too large: %+v", got)
	}
	// the owner's correction of 2026-11-24: queued again by any request once the limit holds it, not before
	limits := bayescompile.Limits{CacheMaxBytes: 6500}
	if p := bayescompile.PlanEnqueue(got, bayesmodels.CompileRequest{ModelID: big, Version: 1, ProgramSHA256: program, By: w.observer.ID},
		nil, limits, time.Now()); p.Queue {
		t.Error("queued again under the same limit")
	}
	limits.CacheMaxBytes = 8000
	if p := bayescompile.PlanEnqueue(got, bayesmodels.CompileRequest{ModelID: big, Version: 1, ProgramSHA256: program, By: w.observer.ID},
		nil, limits, time.Now()); !p.Queue {
		t.Error("not queued again once the limit holds it")
	}
	// a queued row of an older image leaves the queue; an older image's failure nobody holds is cleaned
	staleRow, _ := bayescompile.NewRow(strings.Repeat("2", 64), bayescompile.Image{CmdStan: "2.40.0", Digest: old}, 1)
	staleRow.State, staleRow.QueuedAt, staleRow.RequestedBy = bayescompile.StateQueued, time.Now(), w.admin.ID
	stale, _ := bayescompile.Insert(w.ctx, w.db, staleRow)
	orphan, _ := bayescompile.NewRow(strings.Repeat("3", 64), bayescompile.Image{CmdStan: "2.40.0", Digest: old}, 1)
	orphan.State, orphan.Reason, orphan.FinishedAt = bayescompile.StateFailed, bayescompile.ReasonCompileFailed, at
	orphanID, _ := bayescompile.Insert(w.ctx, w.db, orphan)
	heldRow, _ := bayescompile.NewRow(w.prog, bayescompile.Image{CmdStan: "2.40.0", Digest: old}, 1)
	heldRow.State, heldRow.Reason, heldRow.DeadlineSeconds, heldRow.MemoryBytes, heldRow.FinishedAt =
		bayescompile.StateTimeout, bayescompile.ReasonDeadline, 300, 3<<30, at
	held, _ := bayescompile.Insert(w.ctx, w.db, heldRow)
	w.c.cleaned = time.Time{}
	w.round()
	if !w.gone(stale) || !w.gone(orphanID) || w.gone(held) {
		t.Errorf("stale %v, orphan %v, held %v", w.gone(stale), w.gone(orphanID), w.gone(held))
	}
}
