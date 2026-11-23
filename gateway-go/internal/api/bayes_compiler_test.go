package api

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescodegen"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescompile"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescontract"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
)

// stan-compile's Compiler over the database (docs/specs/bayes-stan-compile.md, lot C3) on TEST_DATABASE_URL, with the
// compile loop of lot C2 and its fake cluster and engine: a save queues the compile after its commit and the list shows
// the state the loop reaches; two saves of one key at once write one row; the overtaking; «Compile now» and the
// owner's rule on the limits; a run's binary placed, and an altered one forgotten.

// compilerWorld is the loop's world with the real Compiler in its Deps.
type compilerWorld struct {
	*compileWorld
	compiler *bayescompile.Compiler
}

func newCompilerWorld(t *testing.T) *compilerWorld {
	t.Helper()
	w := &compilerWorld{compileWorld: newCompileWorld(t)}
	w.compiler = newBayesCompiler(w.c.d)
	w.c.d.BayesCompiler = w.compiler
	return w
}

// route runs a route of the saved models as a person, with the path values its pattern would have set.
func (w *compilerWorld) route(h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal), u *auth.User, method string,
	values map[string]string, body string) *httptest.ResponseRecorder {
	w.t.Helper()
	r := httptest.NewRequest(method, "/x", strings.NewReader(body))
	for k, v := range values {
		r.SetPathValue(k, v)
	}
	rec := httptest.NewRecorder()
	h(w.c.d, rec, r, &auth.Principal{User: *u})
	return rec
}

// listed is the compile state the list gives the model.
func (w *compilerWorld) listed(id int64) bayesCompileOut {
	w.t.Helper()
	rec := w.route(listBayesModels, w.admin, http.MethodGet, nil, "")
	if rec.Code != http.StatusOK {
		w.t.Fatalf("the list: %d %s", rec.Code, rec.Body)
	}
	var out []bayesListedOut
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
		w.t.Fatal(err)
	}
	for _, m := range out {
		if m.ID == id {
			return m.Compile
		}
	}
	w.t.Fatalf("model %d not in the list", id)
	return bayesCompileOut{}
}

// rowsOf are the rows of a program.
func (w *compilerWorld) rowsOf(program string) []bayescompile.Row {
	w.t.Helper()
	rows, err := w.db.Query(w.ctx, `SELECT `+bayescompile.Columns+` FROM bayes_binaries WHERE program_sha256 = $1 ORDER BY id`, program)
	if err != nil {
		w.t.Fatal(err)
	}
	defer rows.Close()
	var out []bayescompile.Row
	for rows.Next() {
		r, err := bayescompile.Scan(rows)
		if err != nil {
			w.t.Fatal(err)
		}
		out = append(out, r)
	}
	return out
}

// infoFor is the --info stanc would print for a stored version's program, from its manifests: the data it reads, the
// variables it writes by block in the manifest's order, no function or distribution (the loop's cross-check passes).
func infoFor(t *testing.T, p *bayescodegen.Program) []byte {
	t.Helper()
	block := map[string]string{}
	for _, e := range p.Mapping {
		block[e.Stan] = e.Block
	}
	object := func(names []string, dims []int) string {
		var parts []string
		for i, n := range names {
			parts = append(parts, fmt.Sprintf(`%q: {"type": "real", "dimensions": %d}`, n, dims[i]))
		}
		return "{" + strings.Join(parts, ", ") + "}"
	}
	var inputs []string
	var inputDims []int
	for _, d := range p.Data {
		inputs = append(inputs, d.Stan)
		dims := 1
		if d.Kind == bayescontract.DataSize || d.Kind == bayescontract.DataMode {
			dims = 0
		}
		inputDims = append(inputDims, dims)
	}
	parts := []string{`"inputs": ` + object(inputs, inputDims)}
	for _, b := range []string{"parameters", "transformed parameters", "generated quantities"} {
		var names []string
		var dims []int
		for _, q := range p.Quantities {
			if block[q.Stan] == b {
				names, dims = append(names, q.Stan), append(dims, len(q.Dims))
			}
		}
		parts = append(parts, strconv.Quote(b)+": "+object(names, dims))
	}
	info := []byte("{" + strings.Join(parts, ", ") + `, "functions": [], "distributions": [], "included_files": []}`)
	if m := bayescodegen.CrossCheck(info, p); len(m) != 0 {
		t.Fatalf("the info made for the test does not pass the cross-check: %+v", m)
	}
	return info
}

// storedProgram is the version holding a row's program, as the loop's cross-check reads it.
func (w *compilerWorld) storedProgram(row bayescompile.Row) *bayescodegen.Program {
	w.t.Helper()
	tx, err := w.db.Begin(w.ctx)
	if err != nil {
		w.t.Fatal(err)
	}
	defer tx.Rollback(w.ctx)
	v, err := bcVersionOf(w.ctx, tx, row)
	if err != nil || v == nil {
		w.t.Fatalf("the version of row %d: %v", row.ID, err)
	}
	p, err := bcProgram(v)
	if err != nil {
		w.t.Fatal(err)
	}
	return p
}

func someProgram(c byte) string { return strings.Repeat(string(c), 64) }

// A save queues its program's compile after the commit; the list says queued, then compiling, then — the Job ended
// with a valid termination message — compiled, and the run's binary is placed from the cache.
func TestBayesCompileASaveIsCompiledAndTheListSaysSo(t *testing.T) {
	w := newCompilerWorld(t)
	graph := golden(t, "golden/hier_slopes.graph.json")
	body, _ := json.Marshal(map[string]any{"name": "Slopes saved", "graph": json.RawMessage(graph)})
	rec := w.route(createBayesModel, w.admin, http.MethodPost, map[string]string{"project_id": fmt.Sprint(w.project)}, string(body))
	if rec.Code != http.StatusCreated {
		t.Fatalf("create: %d %s", rec.Code, rec.Body)
	}
	var created struct {
		ID      int64 `json:"id"`
		Version struct {
			ProgramSHA256 string `json:"program_sha256"`
		} `json:"version"`
		Compile bayesCompileOut `json:"compile"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &created); err != nil {
		t.Fatal(err)
	}
	prog := created.Version.ProgramSHA256
	rows := w.rowsOf(prog)
	if len(rows) != 1 || rows[0].State != bayescompile.StateQueued || rows[0].RequestedBy != w.admin.ID ||
		rows[0].ModelID != created.ID || rows[0].Version != 1 || rows[0].ImageDigest != w.digest || rows[0].GroupVersion != 2 ||
		rows[0].CmdStan != "2.40.0" || rows[0].Arch != "amd64" {
		t.Fatalf("the save's row: %+v", rows)
	}
	if created.Compile.State != bayesmodels.CompileQueued {
		t.Errorf("the create's answer: %+v", created.Compile)
	}
	if c := w.listed(created.ID); c.State != bayesmodels.CompileQueued || c.Detail != "Waiting for a free place to compile (2 at once)." ||
		c.At == nil {
		t.Fatalf("queued in the list: %+v", c)
	}
	// the loop starts its Job
	if n := w.round(); n != 1 {
		t.Fatalf("round 1: %d", n)
	}
	row := w.row(rows[0].ID)
	if c := w.listed(created.ID); c.State != bayesmodels.CompileRunning || !strings.HasPrefix(c.Detail, "Compiling since ") {
		t.Fatalf("running in the list: %+v", c)
	}
	// the Job ends with a valid termination message
	cj := compileJSON(prog, binaryBytes, infoFor(t, w.storedProgram(row)))
	w.compiled(row, binaryBytes, cj, message(prog, sha(binaryBytes), sha(cj)))
	if n := w.round(); n != 1 {
		t.Fatalf("the end: %d", n)
	}
	if r := w.row(row.ID); r.State != bayescompile.StateReady || r.BinarySHA256 != sha(binaryBytes) {
		t.Fatalf("not ready: %+v", r)
	}
	if c := w.listed(created.ID); c.State != bayesmodels.CompileReady || !strings.HasPrefix(c.Detail, "Compiled on ") || c.At == nil {
		t.Fatalf("ready in the list: %+v", c)
	}
	rec = w.route(getBayesModel, w.admin, http.MethodGet, map[string]string{"model_id": fmt.Sprint(created.ID)}, "")
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), `"state":"ready"`) {
		t.Fatalf("the detail: %d %s", rec.Code, rec.Body)
	}
	// a layout-only save asks again: nothing is queued, the binary answers
	if err := w.compiler.Enqueue(w.ctx, bayesmodels.CompileRequest{ModelID: created.ID, Version: 2, ProgramSHA256: prog,
		By: w.admin.ID}); err != nil || len(w.rowsOf(prog)) != 1 || w.row(row.ID).State != bayescompile.StateReady {
		t.Fatalf("a save of a ready program: %v", err)
	}

	// a run: the binary is ready, placed into the step's prefix, its use recorded
	before := w.row(row.ID).LastUsedAt
	time.Sleep(10 * time.Millisecond)
	b, s, err := w.compiler.Ready(w.ctx, prog)
	if err != nil || b == nil || b.BinarySHA256 != sha(binaryBytes) || b.Bytes != int64(len(binaryBytes)) || s.State != bayesmodels.CompileReady {
		t.Fatalf("ready: %+v %+v %v", b, s, err)
	}
	if err := w.compiler.Place(w.ctx, b, "env-3/step-1/model"); err != nil {
		t.Fatal(err)
	}
	w.engine.mu.Lock()
	placed := w.engine.bucket["env-3/step-1/model"]
	w.engine.mu.Unlock()
	if string(placed) != string(binaryBytes) || !w.row(row.ID).LastUsedAt.After(before) {
		t.Fatalf("the place: %d bytes, used %v (before %v)", len(placed), w.row(row.ID).LastUsedAt, before)
	}
	for _, bad := range []string{"bayes-binaries/" + b.BinarySHA256, "env-3/model", "datasets/x/model", "env-3/step-1/other"} {
		if err := w.compiler.Place(w.ctx, b, bad); err == nil {
			t.Errorf("placed at %q", bad)
		}
	}

	// the stored object altered: refused, the row and the object forgotten, the run compiles again
	w.engine.mu.Lock()
	w.engine.storage["bayes-binaries/"+b.BinarySHA256] = []byte("not the binary")
	w.engine.mu.Unlock()
	if err := w.compiler.Place(w.ctx, b, "env-3/step-2/model"); !errors.Is(err, bayescompile.ErrAltered) {
		t.Fatalf("an altered binary: %v", err)
	}
	w.engine.mu.Lock()
	_, kept := w.engine.storage["bayes-binaries/"+b.BinarySHA256]
	_, placedAltered := w.engine.bucket["env-3/step-2/model"]
	w.engine.mu.Unlock()
	if !w.gone(row.ID) || kept || placedAltered {
		t.Fatalf("after the altered binary: row gone %v, object kept %v, placed %v", w.gone(row.ID), kept, placedAltered)
	}
	if err := w.compiler.Place(w.ctx, b, "env-3/step-2/model"); !errors.Is(err, bayescompile.ErrGone) {
		t.Fatalf("a place after the row went: %v", err)
	}
	b, s, err = w.compiler.Need(w.ctx, bayesmodels.CompileRequest{ModelID: created.ID, Version: 1, ProgramSHA256: prog, By: w.ann.ID})
	if err != nil || b != nil || s.State != bayesmodels.CompileQueued || len(w.rowsOf(prog)) != 1 || w.rowsOf(prog)[0].RequestedBy != w.ann.ID {
		t.Fatalf("the run compiles it again: %+v %+v %v", b, s, err)
	}
}

// Two saves of one program at once (two models made from the same graph) write one row: neither save's request fails
// on the key's unique constraint.
func TestBayesCompileTwoSavesOfOneKeyAtOnce(t *testing.T) {
	w := newCompilerWorld(t)
	store := w.c.d.bayesStore()
	graph := golden(t, "golden/hier_slopes.graph.json")
	for round := 0; round < 3; round++ {
		if _, err := w.db.Exec(w.ctx, `DELETE FROM bayes_binaries`); err != nil {
			t.Fatal(err)
		}
		const n = 6
		saved := make([]*bayesmodels.Saved, n)
		errs := make([]error, n)
		var wg sync.WaitGroup
		start := make(chan struct{})
		for i := range n {
			wg.Add(1)
			go func(i int) {
				defer wg.Done()
				<-start
				saved[i], errs[i] = store.Create(w.ctx, bayesmodels.NewModel{ProjectID: w.project,
					Name: fmt.Sprintf("Twin %d-%d", round, i), Graph: graph}, w.admin.ID)
			}(i)
		}
		close(start)
		wg.Wait()
		var prog string
		for i := range n {
			if errs[i] != nil || !saved[i].Enqueued {
				t.Fatalf("save %d: %v (enqueued %v)", i, errs[i], saved[i] != nil && saved[i].Enqueued)
			}
			prog = saved[i].Content.ProgramSHA256
		}
		if rows := w.rowsOf(prog); len(rows) != 1 || rows[0].State != bayescompile.StateQueued {
			t.Fatalf("round %d: %d rows %+v", round, len(rows), rows)
		}
	}
}

// A newer save of a model takes the person's older queued version out, whatever the newer program's state; a running
// one stays, and so do another model's and another person's.
func TestBayesCompileANewerSaveOvertakesTheOlderQueuedOne(t *testing.T) {
	w := newCompilerWorld(t)
	ctx, c := w.ctx, w.compiler
	ask := func(by, model, version int64, program string) {
		t.Helper()
		if err := c.Enqueue(ctx, bayesmodels.CompileRequest{ModelID: model, Version: version, ProgramSHA256: program, By: by}); err != nil {
			t.Fatal(err)
		}
	}
	state := func(program string) string {
		t.Helper()
		s, err := c.State(ctx, program)
		if err != nil {
			t.Fatal(err)
		}
		return s.State
	}
	ann, admin := w.ann.ID, w.admin.ID
	ask(ann, 7, 1, someProgram('1'))
	ask(ann, 8, 1, someProgram('2'))   // another model of hers
	ask(admin, 7, 1, someProgram('3')) // another person
	if state(someProgram('1')) != bayescompile.StateQueued {
		t.Fatal("not queued")
	}
	// version 2: a new program; version 1 leaves the queue, the others stay
	ask(ann, 7, 2, someProgram('4'))
	if state(someProgram('1')) != bayescompile.StateNone || state(someProgram('2')) != bayescompile.StateQueued ||
		state(someProgram('3')) != bayescompile.StateQueued || state(someProgram('4')) != bayescompile.StateQueued {
		t.Fatal("version 2 did not overtake version 1 alone")
	}
	// version 3 holds a program that is already ready: nothing queued, version 2 leaves all the same
	ready := w.rowsOf(someProgram('2'))[0]
	ready.State, ready.ProgramSHA256, ready.BinarySHA256, ready.BinaryBytes, ready.FinishedAt =
		bayescompile.StateReady, someProgram('5'), someProgram('f'), 10, time.Now().UTC()
	img := bayescompile.Image{CmdStan: "2.40.0", Arch: "amd64", Digest: w.digest}
	var err error
	if ready.CacheKey, err = bayescompile.CacheKey(someProgram('5'), img); err != nil {
		t.Fatal(err)
	}
	if _, err := bayescompile.Insert(ctx, w.db, ready); err != nil {
		t.Fatal(err)
	}
	ask(ann, 7, 3, someProgram('5'))
	if state(someProgram('4')) != bayescompile.StateNone || state(someProgram('5')) != bayescompile.StateReady {
		t.Fatal("a newer version with a ready program did not take the older queued one out")
	}
	// a running older version stays
	ask(ann, 7, 4, someProgram('6'))
	if _, err := w.db.Exec(ctx, `UPDATE bayes_binaries SET state = 'running', job_name = 'x', started_at = now()
		WHERE program_sha256 = $1`, someProgram('6')); err != nil {
		t.Fatal(err)
	}
	ask(ann, 7, 5, someProgram('7'))
	if state(someProgram('6')) != bayescompile.StateRunning || state(someProgram('7')) != bayescompile.StateQueued {
		t.Fatal("a running compile was overtaken")
	}
	// the queued sentence names her running compile
	if s, _ := c.State(ctx, someProgram('7')); s.Detail != "Waiting to compile: another compile of yours is running." {
		t.Errorf("the queued sentence: %q", s.Detail)
	}
	// the list's read: one statement for many programs, none for a program of no row or of no form
	states, err := c.States(ctx, []string{someProgram('6'), someProgram('7'), someProgram('9'), "not a sha"})
	if err != nil || states[someProgram('6')].State != bayescompile.StateRunning || states[someProgram('7')].State != bayescompile.StateQueued ||
		states[someProgram('9')].State != bayescompile.StateNone || states["not a sha"].State != bayescompile.StateNone {
		t.Fatalf("States: %+v %v", states, err)
	}
	// the group disabled: every state is none and a request queues nothing
	if _, err := w.db.Exec(ctx, `UPDATE model_groups SET enabled = false WHERE id = 'bayes'`); err != nil {
		t.Fatal(err)
	}
	ask(ann, 9, 1, someProgram('8'))
	if state(someProgram('7')) != bayescompile.StateNone || len(w.rowsOf(someProgram('8'))) != 0 {
		t.Fatal("a disabled group still answers or queues")
	}
}

// «Compile now»: a failure of the compiler is queued again; a timeout, or a binary larger than the cache, answers 409
// with the state's sentence until the limit is raised, and then compiles once more; a run fails at once meanwhile.
func TestBayesCompileNowAndTheOwnersRule(t *testing.T) {
	w := newCompilerWorld(t)
	ctx := w.ctx
	compileNow := func() *httptest.ResponseRecorder {
		return w.route(compileBayesModelVersion, w.admin, http.MethodPost,
			map[string]string{"model_id": fmt.Sprint(w.model), "version": "1"}, "")
	}
	// a program the loop ended as given
	end := func(state, reason string, extra string) int64 {
		t.Helper()
		if _, err := w.db.Exec(ctx, `DELETE FROM bayes_binaries`); err != nil {
			t.Fatal(err)
		}
		id := w.queue(w.ann.ID, w.model, 1, w.prog, time.Now())
		detail := map[string]string{
			bayescompile.ReasonDeadline: "The program did not compile within the limit of 300 s (24 Nov): it is not compiled again with this limit. A simpler model, or a longer limit set by an administrator, compiles anew.",
			bayescompile.ReasonTooLarge: "The cache's limit (2 GiB) is smaller than this model's binary (3 GiB): an administrator can raise it.",
		}[reason]
		if _, err := w.db.Exec(ctx, `UPDATE bayes_binaries SET state = $2, reason = $3, detail = nullif($4, ''), deadline_seconds = 300,
			memory_bytes = 3221225472, finished_at = now(), job_name = 'x', started_at = now() `+extra+` WHERE id = $1`,
			id, state, reason, detail); err != nil {
			t.Fatal(err)
		}
		return id
	}
	settings := func(sql string) {
		t.Helper()
		if _, err := w.db.Exec(ctx, `INSERT INTO bayes_settings (id, updated_at) VALUES (1, now()) ON CONFLICT (id) DO NOTHING`); err != nil {
			t.Fatal(err)
		}
		if _, err := w.db.Exec(ctx, `UPDATE bayes_settings SET `+sql+` WHERE id = 1`); err != nil {
			t.Fatal(err)
		}
	}
	need := func() (bayesmodels.CompileState, error) {
		_, s, err := w.compiler.Need(ctx, bayesmodels.CompileRequest{ModelID: w.model, Version: 1, ProgramSHA256: w.prog, By: w.ann.ID})
		return s, err
	}

	// the compiler refused it: a save asks nothing again, «Compile now» does
	id := end(bayescompile.StateFailed, bayescompile.ReasonCompileFailed, "")
	if err := w.compiler.Enqueue(ctx, bayesmodels.CompileRequest{ModelID: w.model, Version: 1, ProgramSHA256: w.prog, By: w.ann.ID}); err != nil ||
		w.row(id).State != bayescompile.StateFailed {
		t.Fatalf("a save queued a failure of the compiler again: %v", err)
	}
	if s, err := need(); !errors.Is(err, bayescompile.ErrRefused) || s.State != bayescompile.StateFailed {
		t.Fatalf("a run of a program the compiler refused: %+v %v", s, err)
	}
	if rec := compileNow(); rec.Code != http.StatusAccepted || !strings.Contains(rec.Body.String(), `"state":"queued"`) ||
		w.row(id).State != bayescompile.StateQueued || w.row(id).RequestedBy != w.admin.ID {
		t.Fatalf("«Compile now» after a failure of the compiler: %d %s", rec.Code, rec.Body)
	}
	if rec := compileNow(); rec.Code != http.StatusConflict {
		t.Fatalf("«Compile now» while queued: %d", rec.Code)
	}

	// the deadline passed: 409 with the sentence across «Compile now», saves and runs, until the deadline is raised
	id = end(bayescompile.StateTimeout, bayescompile.ReasonDeadline, "")
	for range 2 {
		rec := compileNow()
		var body struct {
			Detail string `json:"detail"`
		}
		_ = json.Unmarshal(rec.Body.Bytes(), &body)
		if rec.Code != http.StatusConflict || !strings.HasPrefix(body.Detail, "The program did not compile within the limit of 300 s") ||
			w.row(id).State != bayescompile.StateTimeout {
			t.Fatalf("«Compile now» after a timeout: %d %s", rec.Code, rec.Body)
		}
	}
	if s, err := need(); !errors.Is(err, bayescompile.ErrRefused) || s.State != bayescompile.StateTimeout || s.Detail == "" {
		t.Fatalf("a run after a timeout: %+v %v", s, err)
	}
	if c := w.listed(w.model); c.State != bayesmodels.CompileTimeout ||
		!strings.HasPrefix(c.Detail, "The program did not compile within") {
		t.Fatalf("the list after a timeout: %+v", c)
	}
	settings(`compile_seconds = 300`)
	if rec := compileNow(); rec.Code != http.StatusConflict {
		t.Fatalf("the same deadline: %d", rec.Code)
	}
	settings(`compile_seconds = 600`)
	if rec := compileNow(); rec.Code != http.StatusAccepted || w.row(id).State != bayescompile.StateQueued {
		t.Fatalf("«Compile now» under a longer deadline: %d %s", rec.Code, rec.Body)
	}
	settings(`compile_seconds = NULL`)

	// a binary larger than the cache: 409 until the cache holds it; then a run compiles it again by itself
	id = end(bayescompile.StateFailed, bayescompile.ReasonTooLarge, `, binary_bytes = 3221225472`)
	if rec := compileNow(); rec.Code != http.StatusConflict || !strings.Contains(rec.Body.String(), "The cache's limit") {
		t.Fatalf("«Compile now» of a binary larger than the cache: %d %s", rec.Code, rec.Body)
	}
	settings(`cache_max_bytes = 4294967296`)
	if s, err := need(); err != nil || s.State != bayescompile.StateQueued || w.row(id).State != bayescompile.StateQueued {
		t.Fatalf("a run once the cache holds the binary: %+v %v", s, err)
	}
}

// The compiler is set where the statistical models can run, and nowhere else.
func TestBayesCompilerIsSetOnlyWhereTheModelsRun(t *testing.T) {
	t.Setenv("MODELS__ENABLED", "false")
	if c := BayesCompiler(&Deps{}); c != nil {
		t.Fatalf("models off: %T", c)
	}
	var d Deps
	if _, ok := d.bayesCompiler().(bayesmodels.NoCompiler); !ok {
		t.Fatal("no compiler: not NoCompiler")
	}
	d.BayesCompiler = &bayescompile.Compiler{Current: func(context.Context) (*bayescompile.Current, error) { return nil, nil }}
	if _, ok := d.bayesCompiler().(*bayescompile.Compiler); !ok {
		t.Fatal("the compiler set is not the one used")
	}
}
