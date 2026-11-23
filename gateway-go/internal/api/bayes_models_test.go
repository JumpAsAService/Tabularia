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
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayestemplates"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
)

// The saved Bayesian models' routes on a PostgreSQL the Python migrations made
// (gateway-go/scripts/test.sh): every row of «Who may do what» with a person who has the right and
// one who lacks exactly it, the stale save, the bound of the body, the delete refused while a flow
// pins the model, the search, the recent objects, the folder's delete check and the audit.

type fakeBayesCompiler struct {
	mu       sync.Mutex
	enqueued []bayesmodels.CompileRequest
	state    string
}

func (f *fakeBayesCompiler) Enqueue(_ context.Context, r bayesmodels.CompileRequest) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.enqueued = append(f.enqueued, r)
	return nil
}

func (f *fakeBayesCompiler) State(context.Context, string) (bayesmodels.CompileState, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.state == "" {
		return bayesmodels.CompileState{State: bayesmodels.CompileNone}, nil
	}
	return bayesmodels.CompileState{State: f.state, Detail: "x", At: time.Now()}, nil
}

func (f *fakeBayesCompiler) count() int {
	f.mu.Lock()
	defer f.mu.Unlock()
	return len(f.enqueued)
}

type bayesWorld struct {
	t                                       *testing.T
	ctx                                     context.Context
	db                                      *pgxpool.Pool
	d                                       *Deps
	compiler                                *fakeBayesCompiler
	folder, other                           int64
	env                                     int64
	editor, viewer, runner, stranger, admin *auth.User
}

func newBayesWorld(t *testing.T) *bayesWorld {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set: run gateway-go/scripts/test.sh")
	}
	w := &bayesWorld{t: t, ctx: context.Background(), compiler: &fakeBayesCompiler{}}
	db, err := pgxpool.New(w.ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	w.db = db
	// the auth package's tests TRUNCATE the users (and what refers to them): one at a time
	lock, err := db.Acquire(w.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := lock.Exec(w.ctx, `SELECT pg_advisory_lock(7101)`); err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	w.d = &Deps{DB: db, Log: log, Config: &config.Config{}, BayesCompiler: w.compiler}
	stamp := time.Now().UnixNano()
	user := func(name string, admin bool) *auth.User {
		email := fmt.Sprintf("bmr-%s-%d@x.test", name, stamp)
		id := w.one(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, $2, 'x', true, $3, false, false, now()) RETURNING id`, email, name, admin)
		return &auth.User{ID: id, Email: email, FullName: name, IsActive: true, IsSuperuser: admin}
	}
	w.editor, w.viewer, w.runner = user("editor", false), user("viewer", false), user("runner", false)
	w.stranger, w.admin = user("stranger", false), user("admin", true)
	w.folder = w.one(`INSERT INTO projects (name, description, created_at) VALUES ($1, '', now()) RETURNING id`, fmt.Sprintf("bmr-%d", stamp))
	w.other = w.one(`INSERT INTO projects (name, description, created_at) VALUES ($1, '', now()) RETURNING id`, fmt.Sprintf("bmr-other-%d", stamp))
	for _, g := range []struct {
		project int64
		u       *auth.User
		cap     string
	}{{w.folder, w.editor, "edit"}, {w.folder, w.viewer, "view"}, {w.folder, w.runner, "run"}, {w.other, w.stranger, "manage"}} {
		w.one(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, $3) RETURNING id`, g.project, g.u.ID, g.cap)
	}
	// the group `bayes`: enabled, for everyone who may edit flows
	_, _ = db.Exec(w.ctx, `DELETE FROM model_groups WHERE id = 'bayes'`)
	_, _ = db.Exec(w.ctx, `DELETE FROM script_environments WHERE managed_by = 'model_group:bayes'`)
	w.env = w.one(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
		memory_limit, max_timeout_seconds, created_at, updated_at, managed_by) VALUES ($1, '', 'r', true, '1', '2', '2Gi', '4Gi', 3600,
		now(), now(), 'model_group:bayes') RETURNING id`, fmt.Sprintf("Bayes %d", stamp))
	w.group(true, true)
	t.Cleanup(func() {
		ids := []int64{w.folder, w.other}
		for _, q := range []string{
			`DELETE FROM flows WHERE project_id = ANY($1)`,
			`DELETE FROM bayes_model_versions WHERE model_id IN (SELECT id FROM bayes_models WHERE project_id = ANY($1))`,
			`DELETE FROM bayes_models WHERE project_id = ANY($1)`,
			`DELETE FROM permissions WHERE project_id = ANY($1)`,
			`DELETE FROM projects WHERE id = ANY($1)`,
		} {
			_, _ = db.Exec(w.ctx, q, ids)
		}
		_, _ = db.Exec(w.ctx, `DELETE FROM model_groups WHERE id = 'bayes'`)
		_, _ = db.Exec(w.ctx, `DELETE FROM script_environment_groups WHERE environment_id = $1`, w.env)
		_, _ = db.Exec(w.ctx, `DELETE FROM script_environments WHERE id = $1`, w.env)
		users := []int64{w.editor.ID, w.viewer.ID, w.runner.ID, w.stranger.ID, w.admin.ID}
		_, _ = db.Exec(w.ctx, `DELETE FROM audit_logs WHERE actor_id = ANY($1)`, users)
		_, _ = db.Exec(w.ctx, `DELETE FROM users WHERE id = ANY($1)`, users)
		_, _ = lock.Exec(w.ctx, `SELECT pg_advisory_unlock(7101)`)
		lock.Release()
		db.Close()
	})
	return w
}

func (w *bayesWorld) one(sql string, args ...any) int64 {
	w.t.Helper()
	var id int64
	if err := w.db.QueryRow(w.ctx, sql, args...).Scan(&id); err != nil {
		w.t.Fatalf("%s: %v", sql, err)
	}
	return id
}

// group sets the group `bayes`: enabled or not, for everyone or only for the user groups of its environment.
func (w *bayesWorld) group(enabled, everyone bool) {
	w.t.Helper()
	_, _ = w.db.Exec(w.ctx, `DELETE FROM model_groups WHERE id = 'bayes'`)
	if _, err := w.db.Exec(w.ctx, `INSERT INTO model_groups (id, environment_id, enabled, everyone, updated_at) VALUES ('bayes', $1, $2, $3, now())`,
		w.env, enabled, everyone); err != nil {
		w.t.Fatal(err)
	}
}

type bayesCall struct {
	method, path string
	values       map[string]string
	body         string
}

// call runs a route as a person, with the path values its pattern would have set.
func (w *bayesWorld) call(h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal), u *auth.User, c bayesCall) *httptest.ResponseRecorder {
	w.t.Helper()
	r := httptest.NewRequest(c.method, c.path, strings.NewReader(c.body))
	for k, v := range c.values {
		r.SetPathValue(k, v)
	}
	rec := httptest.NewRecorder()
	h(w.d, rec, r, &auth.Principal{User: *u})
	return rec
}

func (w *bayesWorld) want(rec *httptest.ResponseRecorder, status int) {
	w.t.Helper()
	if rec.Code != status {
		w.t.Fatalf("status %d, want %d: %s", rec.Code, status, rec.Body)
	}
}

func bmID(i int64) string { return fmt.Sprintf("%d", i) }

func bayesTemplate(t *testing.T, id string) json.RawMessage {
	t.Helper()
	tp, ok := bayestemplates.Get(id)
	if !ok {
		t.Fatalf("no template %s", id)
	}
	return tp.Graph
}

// withBox is the graph with one box of its layout moved: the same model, another version.
func withBox(t *testing.T, graph json.RawMessage, dx float64) json.RawMessage {
	t.Helper()
	var doc map[string]any
	if err := json.Unmarshal(graph, &doc); err != nil {
		t.Fatal(err)
	}
	layout, _ := doc["layout"].(map[string]any)
	for _, box := range layout {
		b := box.(map[string]any)
		b["x"] = b["x"].(float64) + dx
		break
	}
	out, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return out
}

func body(t *testing.T, fields map[string]any) string {
	t.Helper()
	b, err := json.Marshal(fields)
	if err != nil {
		t.Fatal(err)
	}
	return string(b)
}

type bayesAnswer struct {
	ID             int64  `json:"id"`
	Name           string `json:"name"`
	ProjectID      int64  `json:"project_id"`
	CurrentVersion int64  `json:"current_version"`
	Family         string `json:"family"`
	Editable       bool   `json:"editable"`
	Runnable       bool   `json:"runnable"`
	Uses           int    `json:"uses"`
	Detail         string `json:"detail"`
	Version        struct {
		Version       int64           `json:"version"`
		Note          string          `json:"note"`
		Program       string          `json:"program"`
		ProgramSHA256 string          `json:"program_sha256"`
		Graph         json.RawMessage `json:"graph"`
		CodegenV      int             `json:"codegen_version"`
	} `json:"version"`
	Compile struct {
		State string `json:"state"`
	} `json:"compile"`
}

func answer(t *testing.T, rec *httptest.ResponseRecorder) bayesAnswer {
	t.Helper()
	var a bayesAnswer
	if err := json.Unmarshal(rec.Body.Bytes(), &a); err != nil {
		t.Fatalf("%v: %s", err, rec.Body)
	}
	return a
}

func (w *bayesWorld) versions(id int64) int64 {
	return w.one(`SELECT count(*) FROM bayes_model_versions WHERE model_id = $1`, id)
}

// create makes a model as the editor from a template.
func (w *bayesWorld) create(name string, project int64, u *auth.User) bayesAnswer {
	w.t.Helper()
	rec := w.call(createBayesModel, u, bayesCall{method: "POST", path: "/x", values: map[string]string{"project_id": bmID(project)},
		body: body(w.t, map[string]any{"name": name, "description": " d ", "graph": bayesTemplate(w.t, "glm_linear"), "template_id": "glm_linear"})})
	w.want(rec, http.StatusCreated)
	return answer(w.t, rec)
}

func TestBayesModelRoutes(t *testing.T) {
	w := newBayesWorld(t)
	g := bayesTemplate(t, "glm_linear")
	pid := map[string]string{"project_id": bmID(w.folder)}

	// ── create ──
	created := w.create("Prices", w.folder, w.editor)
	if created.Version.Version != 1 || created.Version.Note != "created" || created.Version.Program == "" ||
		created.Version.ProgramSHA256 == "" || created.CurrentVersion != 1 || created.Family == "" || !created.Editable || !created.Runnable {
		t.Fatalf("created: %+v", created)
	}
	id := created.ID
	if w.compiler.count() != 1 {
		t.Errorf("the compile asked for %d times after the first save", w.compiler.count())
	}
	mid := map[string]string{"model_id": bmID(id)}
	type refusal struct {
		who    *auth.User
		fields map[string]any
		path   map[string]string
		status int
		why    string
	}
	for _, c := range []refusal{
		{w.viewer, map[string]any{"name": "N", "graph": g}, pid, 403, "viewer: no EDIT"},
		{w.runner, map[string]any{"name": "N", "graph": g}, pid, 403, "runner: no EDIT"},
		{w.stranger, map[string]any{"name": "N", "graph": g}, pid, 403, "stranger: nothing on the folder"},
		{w.editor, map[string]any{"name": "N", "graph": g}, map[string]string{"project_id": "99999999"}, 404, "no such folder"},
		{w.editor, map[string]any{"name": "  ", "graph": g}, pid, 422, "an empty name"},
		{w.editor, map[string]any{"graph": g}, pid, 422, "no name"},
		{w.editor, map[string]any{"name": "N"}, pid, 422, "no graph"},
		{w.editor, map[string]any{"name": "Prices", "graph": g}, pid, 409, "a name taken in the folder"},
		{w.editor, map[string]any{"name": "N", "graph": g, "bogus": 1}, pid, 422, "an unknown key"},
		{w.editor, map[string]any{"name": "N", "graph": g, "base_version": 1}, pid, 422, "a key of another route"},
		{w.editor, map[string]any{"name": "N", "graph": g, "template_id": "nope"}, pid, 422, "an unknown template"},
		{w.editor, map[string]any{"name": "N", "graph": json.RawMessage(`{"format_version":1,"bogus":true}`)}, pid, 422, "a graph that does not decode"},
	} {
		rec := w.call(createBayesModel, c.who, bayesCall{method: "POST", path: "/x", values: c.path, body: body(t, c.fields)})
		if rec.Code != c.status {
			t.Errorf("create, %s: %d %s", c.why, rec.Code, rec.Body)
		}
	}
	if n := w.one(`SELECT count(*) FROM bayes_models WHERE project_id = $1`, w.folder); n != 1 {
		t.Errorf("%d models after the refusals", n)
	}
	// a graph with an error is refused with its findings, and nothing is stored
	rec := w.call(createBayesModel, w.editor, bayesCall{method: "POST", path: "/x", values: pid,
		body: body(t, map[string]any{"name": "N", "graph": json.RawMessage(`{"format_version":1,"bogus":true}`)})})
	var refused struct {
		Findings []map[string]any `json:"findings"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &refused); err != nil || len(refused.Findings) == 0 {
		t.Errorf("the findings: %v %s", err, rec.Body)
	}
	// a body over max_graph_bytes + 16 KiB is refused before it is decoded
	huge := `{"name": "N", "graph": {"x": "` + strings.Repeat("a", 300<<10) + `"}}`
	if rec := w.call(createBayesModel, w.editor, bayesCall{method: "POST", path: "/x", values: pid, body: huge}); rec.Code != http.StatusRequestEntityTooLarge {
		t.Errorf("huge body: %d", rec.Code)
	}

	// ── who sees it ──
	get := func(u *auth.User, model int64) *httptest.ResponseRecorder {
		return w.call(getBayesModel, u, bayesCall{method: "GET", path: "/x", values: map[string]string{"model_id": bmID(model)}})
	}
	for _, u := range []*auth.User{w.editor, w.viewer, w.runner, w.admin} {
		if rec := get(u, id); rec.Code != 200 {
			t.Errorf("%s sees the model: %d", u.FullName, rec.Code)
		}
	}
	absent, hidden := get(w.editor, 99999999), get(w.stranger, id)
	if absent.Code != 404 || hidden.Code != 404 || absent.Body.String() != hidden.Body.String() {
		t.Errorf("404: %d %q, %d %q", absent.Code, absent.Body, hidden.Code, hidden.Body)
	}
	rights := func(u *auth.User) (bool, bool) {
		a := answer(t, get(u, id))
		return a.Editable, a.Runnable
	}
	if e, r := rights(w.viewer); e || r {
		t.Errorf("viewer: editable %v runnable %v", e, r)
	}
	if e, r := rights(w.runner); e || !r {
		t.Errorf("runner: editable %v runnable %v", e, r)
	}
	list := func(h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal), u *auth.User, values map[string]string) (*httptest.ResponseRecorder, []bayesAnswer) {
		rec := w.call(h, u, bayesCall{method: "GET", path: "/x", values: values})
		var out []bayesAnswer
		if rec.Code == 200 {
			if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
				t.Fatal(err)
			}
		}
		return rec, out
	}
	if rec, out := list(listBayesModels, w.viewer, nil); len(out) != 1 || out[0].ID != id || out[0].Family == "" ||
		strings.Contains(rec.Body.String(), `"program"`) || strings.Contains(rec.Body.String(), `"graph"`) {
		t.Errorf("viewer's list: %s", rec.Body)
	}
	if _, out := list(listBayesModels, w.stranger, nil); len(out) != 0 {
		t.Errorf("stranger's list: %v", out)
	}
	if _, out := list(listProjectBayesModels, w.runner, pid); len(out) != 1 {
		t.Errorf("folder list: %v", out)
	}
	if rec, _ := list(listProjectBayesModels, w.stranger, pid); rec.Code != 403 {
		t.Errorf("a folder the stranger cannot see: %d", rec.Code)
	}
	if rec, _ := list(listProjectBayesModels, w.editor, map[string]string{"project_id": "99999999"}); rec.Code != 404 {
		t.Errorf("a folder that does not exist: %d", rec.Code)
	}

	// the group: not in its user group, then disabled: the model is absent for all but an administrator
	w.group(true, false)
	if rec := get(w.editor, id); rec.Code != 404 {
		t.Errorf("not in the group's user group: %d", rec.Code)
	}
	if _, out := list(listBayesModels, w.editor, nil); len(out) != 0 {
		t.Errorf("list outside the group: %v", out)
	}
	if rec := w.call(createBayesModel, w.editor, bayesCall{method: "POST", path: "/x", values: pid, body: body(t, map[string]any{"name": "Z", "graph": g})}); rec.Code != 403 {
		t.Errorf("create outside the group: %d", rec.Code)
	}
	if rec := w.call(checkBayesModel, w.editor, bayesCall{method: "POST", path: "/x", body: body(t, map[string]any{"graph": g})}); rec.Code != 403 {
		t.Errorf("check outside the group: %d", rec.Code)
	}
	gid := w.one(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ($1, '', false, false, now()) RETURNING id`,
		fmt.Sprintf("bmr-group-%d", time.Now().UnixNano()))
	t.Cleanup(func() {
		_, _ = w.db.Exec(w.ctx, `DELETE FROM user_groups WHERE group_id = $1`, gid)
		_, _ = w.db.Exec(w.ctx, `DELETE FROM script_environment_groups WHERE group_id = $1`, gid)
		_, _ = w.db.Exec(w.ctx, `DELETE FROM groups WHERE id = $1`, gid)
	})
	w.one(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2) RETURNING user_id`, w.editor.ID, gid)
	w.one(`INSERT INTO script_environment_groups (environment_id, group_id) VALUES ($1, $2) RETURNING group_id`, w.env, gid)
	if rec := get(w.editor, id); rec.Code != 200 {
		t.Errorf("in the group's user group: %d", rec.Code)
	}
	if rec := get(w.viewer, id); rec.Code != 404 {
		t.Errorf("the viewer is not in it: %d", rec.Code)
	}
	w.group(false, true)
	if rec := get(w.editor, id); rec.Code != 404 {
		t.Errorf("group disabled: %d", rec.Code)
	}
	if rec := get(w.admin, id); rec.Code != 200 {
		t.Errorf("an administrator sees it with the group disabled: %d", rec.Code)
	}
	w.group(true, true)

	// ── check ──
	rec = w.call(checkBayesModel, w.viewer, bayesCall{method: "POST", path: "/x", body: body(t, map[string]any{"graph": g})})
	w.want(rec, 200)
	var checked struct {
		OK      bool            `json:"ok"`
		Program string          `json:"program"`
		Mapping json.RawMessage `json:"mapping"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &checked); err != nil || !checked.OK || checked.Program != created.Version.Program || len(checked.Mapping) == 0 {
		t.Errorf("check: %v %s", err, rec.Body)
	}
	rec = w.call(checkBayesModel, w.viewer, bayesCall{method: "POST", path: "/x", body: `{"graph": {"format_version": 1, "bogus": 1}}`})
	w.want(rec, 200)
	if strings.Contains(rec.Body.String(), `"program"`) || !strings.Contains(rec.Body.String(), `"ok":false`) {
		t.Errorf("check of a graph with an error: %s", rec.Body)
	}
	if n := w.one(`SELECT count(*) FROM bayes_models`); n < 1 || w.versions(id) != 1 {
		t.Errorf("check stored something")
	}

	// ── save: base_version, identical, stale, refused ──
	patch := func(u *auth.User, fields map[string]any) *httptest.ResponseRecorder {
		return w.call(updateBayesModel, u, bayesCall{method: "PATCH", path: "/x", values: mid, body: body(t, fields)})
	}
	for _, u := range []*auth.User{w.viewer, w.runner} {
		if rec := patch(u, map[string]any{"description": "x"}); rec.Code != 403 {
			t.Errorf("%s changes it: %d", u.FullName, rec.Code)
		}
	}
	if rec := patch(w.stranger, map[string]any{"description": "x"}); rec.Code != 404 {
		t.Errorf("the stranger changes it: %d", rec.Code)
	}
	if rec := patch(w.editor, map[string]any{"graph": g}); rec.Code != 422 {
		t.Errorf("a graph without base_version: %d", rec.Code)
	}
	moved := withBox(t, g, 40)
	before := w.compiler.count()
	rec = patch(w.editor, map[string]any{"graph": moved, "base_version": 1})
	w.want(rec, 200)
	if a := answer(t, rec); a.CurrentVersion != 2 || a.Version.Note != "layout" || a.Version.ProgramSHA256 != created.Version.ProgramSHA256 {
		t.Errorf("a layout-only save: %+v", a)
	}
	if w.compiler.count() != before+1 {
		t.Errorf("the compile asked for %d times by a save", w.compiler.count()-before)
	}
	rec = patch(w.editor, map[string]any{"graph": moved, "base_version": 1})
	w.want(rec, 200) // the same bytes as the current version: nothing written, whatever the base
	if a := answer(t, rec); a.CurrentVersion != 2 || w.versions(id) != 2 {
		t.Errorf("an identical save wrote a version: %+v", a)
	}
	rec = patch(w.editor, map[string]any{"graph": withBox(t, g, 80), "base_version": 1})
	w.want(rec, 409)
	if !strings.Contains(rec.Body.String(), "version 2 was saved by someone else meanwhile: reload to see it") ||
		!strings.Contains(rec.Body.String(), `"current_version":2`) {
		t.Errorf("stale: %s", rec.Body)
	}
	rec = patch(w.editor, map[string]any{"graph": json.RawMessage(`{"format_version":1,"bogus":true}`), "base_version": 2})
	w.want(rec, 422)
	if !strings.Contains(rec.Body.String(), "findings") || w.versions(id) != 2 {
		t.Errorf("a graph with an error: %s", rec.Body)
	}
	// rename, describe, and a name taken
	w.create("Other", w.folder, w.editor)
	if rec := patch(w.editor, map[string]any{"name": "Other"}); rec.Code != 409 {
		t.Errorf("rename to a taken name: %d", rec.Code)
	}
	rec = patch(w.editor, map[string]any{"name": " Prices 2 ", "description": "new"})
	w.want(rec, 200)
	if a := answer(t, rec); a.Name != "Prices 2" || a.CurrentVersion != 2 {
		t.Errorf("rename: %+v", a)
	}
	// a rename saved with a graph, in one transaction
	rec = patch(w.editor, map[string]any{"name": "Prices", "graph": withBox(t, g, 80), "base_version": 2})
	w.want(rec, 200)
	if a := answer(t, rec); a.Name != "Prices" || a.CurrentVersion != 3 {
		t.Errorf("rename with a save: %+v", a)
	}
	// move: EDIT on both folders
	if rec := patch(w.editor, map[string]any{"project_id": w.other}); rec.Code != 403 {
		t.Errorf("move to a folder without EDIT: %d", rec.Code)
	}
	if rec := patch(w.editor, map[string]any{"project_id": 99999999}); rec.Code != 404 {
		t.Errorf("move to a folder that does not exist: %d", rec.Code)
	}
	w.one(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'edit') RETURNING id`, w.other, w.editor.ID)
	rec = patch(w.editor, map[string]any{"project_id": w.other})
	w.want(rec, 200)
	if a := answer(t, rec); a.ProjectID != w.other || a.CurrentVersion != 3 {
		t.Errorf("move: %+v", a)
	}
	w.want(patch(w.editor, map[string]any{"project_id": w.folder}), 200)

	// ── versions, restore ──
	vs := w.call(listBayesModelVersions, w.viewer, bayesCall{method: "GET", path: "/x", values: mid})
	w.want(vs, 200)
	var listed []struct {
		Version       int64   `json:"version"`
		Current       bool    `json:"current"`
		CreatedBy     *string `json:"created_by"`
		ProgramSHA256 string  `json:"program_sha256"`
	}
	if err := json.Unmarshal(vs.Body.Bytes(), &listed); err != nil || len(listed) != 3 || listed[0].Version != 3 || !listed[0].Current ||
		listed[2].CreatedBy == nil || *listed[2].CreatedBy != w.editor.Email || strings.Contains(vs.Body.String(), `"graph"`) {
		t.Errorf("versions: %v %s", err, vs.Body)
	}
	version := func(u *auth.User, v string) *httptest.ResponseRecorder {
		return w.call(getBayesModelVersion, u, bayesCall{method: "GET", path: "/x", values: map[string]string{"model_id": bmID(id), "version": v}})
	}
	if rec := version(w.runner, "1"); rec.Code != 200 || !strings.Contains(rec.Body.String(), created.Version.ProgramSHA256) {
		t.Errorf("a version: %d", rec.Code)
	}
	if rec := version(w.runner, "0"); rec.Code != 200 {
		t.Errorf("version 0 is the current one: %d", rec.Code)
	}
	if rec := version(w.runner, "99"); rec.Code != 404 {
		t.Errorf("a version that does not exist: %d", rec.Code)
	}
	if rec := version(w.stranger, "1"); rec.Code != 404 {
		t.Errorf("a version of a model not seen: %d", rec.Code)
	}
	restore := func(u *auth.User, v string) *httptest.ResponseRecorder {
		return w.call(restoreBayesModelVersion, u, bayesCall{method: "POST", path: "/x", values: map[string]string{"model_id": bmID(id), "version": v}})
	}
	if rec := restore(w.runner, "1"); rec.Code != 403 {
		t.Errorf("restore by a runner: %d", rec.Code)
	}
	if rec := restore(w.editor, "99"); rec.Code != 404 {
		t.Errorf("restore of a version that does not exist: %d", rec.Code)
	}
	rec = restore(w.editor, "1")
	w.want(rec, 200)
	if a := answer(t, rec); a.CurrentVersion != 4 || a.Version.Note != "restored from v1" || a.Version.ProgramSHA256 != created.Version.ProgramSHA256 ||
		a.Version.Program != created.Version.Program {
		t.Errorf("restore: %+v", a)
	}
	w.want(restore(w.editor, "4"), 200) // the current version: nothing is written
	if w.versions(id) != 4 {
		t.Errorf("%d versions", w.versions(id))
	}

	// ── duplicate ──
	dup := func(u *auth.User) *httptest.ResponseRecorder {
		return w.call(duplicateBayesModel, u, bayesCall{method: "POST", path: "/x", values: mid})
	}
	if rec := dup(w.viewer); rec.Code != 403 {
		t.Errorf("duplicate by a viewer: %d", rec.Code)
	}
	rec = dup(w.editor)
	w.want(rec, 201)
	copy1 := answer(t, rec)
	if copy1.ID == id || copy1.Version.ProgramSHA256 != created.Version.ProgramSHA256 || copy1.CurrentVersion != 1 ||
		!strings.HasPrefix(copy1.Version.Note, "duplicated from «Prices» v4") || copy1.Name == "Prices" {
		t.Errorf("duplicate: %+v", copy1)
	}

	// ── compile ──
	compile := func(u *auth.User, v string) *httptest.ResponseRecorder {
		return w.call(compileBayesModelVersion, u, bayesCall{method: "POST", path: "/x", values: map[string]string{"model_id": bmID(id), "version": v}})
	}
	if rec := compile(w.viewer, "1"); rec.Code != 403 {
		t.Errorf("compile by a viewer: %d", rec.Code)
	}
	if rec := compile(w.stranger, "1"); rec.Code != 404 {
		t.Errorf("compile by a stranger: %d", rec.Code)
	}
	if rec := compile(w.runner, "99"); rec.Code != 404 {
		t.Errorf("compile of a version that does not exist: %d", rec.Code)
	}
	before = w.compiler.count()
	w.want(compile(w.runner, "1"), 202)
	if w.compiler.count() != before+1 {
		t.Errorf("compile now did not enqueue")
	}
	w.compiler.state = bayesmodels.CompileRunning
	if rec := compile(w.runner, "1"); rec.Code != 409 {
		t.Errorf("compile while running: %d", rec.Code)
	}
	w.compiler.state = ""

	// ── uses and delete ──
	flowDef := fmt.Sprintf(`{"nodes": [{"id": "bm1", "type": "bayes_model", "data": {"bayesModelId": %d, "bayesModelVersion": 2}}], "edges": []}`, id)
	secret := w.one(`INSERT INTO projects (name, description, created_at) VALUES ($1, '', now()) RETURNING id`, fmt.Sprintf("bmr-secret-%d", time.Now().UnixNano()))
	t.Cleanup(func() {
		_, _ = w.db.Exec(w.ctx, `DELETE FROM flows WHERE project_id = $1`, secret)
		_, _ = w.db.Exec(w.ctx, `DELETE FROM projects WHERE id = $1`, secret)
	})
	flow := func(name string, project int64) int64 {
		return w.one(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
			VALUES ($1, '', $2, $3, $4, 'duckdb', now(), now()) RETURNING id`, name, project, w.editor.ID, flowDef)
	}
	open, closed := flow("uses open", w.folder), flow("uses closed", secret)
	uses := func(u *auth.User) (flows []struct {
		FlowID  int64 `json:"flow_id"`
		Version int64 `json:"version"`
		NodeID  string
	}, hidden int) {
		rec := w.call(listBayesModelUses, u, bayesCall{method: "GET", path: "/x", values: mid})
		w.want(rec, 200)
		var out struct {
			Flows []struct {
				FlowID  int64 `json:"flow_id"`
				Version int64 `json:"version"`
				NodeID  string
			} `json:"flows"`
			Hidden int `json:"hidden"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatal(err)
		}
		return out.Flows, out.Hidden
	}
	if fl, hidden := uses(w.editor); len(fl) != 1 || fl[0].FlowID != open || fl[0].Version != 2 || hidden != 1 {
		t.Errorf("uses: %v, %d hidden (flows %d and %d)", fl, hidden, open, closed)
	}
	if a := answer(t, get(w.editor, id)); a.Uses != 2 {
		t.Errorf("used by %d flows", a.Uses)
	}
	if rec := w.call(deleteBayesModel, w.viewer, bayesCall{method: "DELETE", path: "/x", values: mid}); rec.Code != 403 {
		t.Errorf("delete by a viewer: %d", rec.Code)
	}
	rec = w.call(deleteBayesModel, w.editor, bayesCall{method: "DELETE", path: "/x", values: mid})
	w.want(rec, 409)
	if !strings.Contains(rec.Body.String(), "uses open") || !strings.Contains(rec.Body.String(), "uses closed") {
		t.Errorf("the 409 names the flows: %s", rec.Body)
	}
	if _, err := w.db.Exec(w.ctx, `DELETE FROM flows WHERE id = ANY($1)`, []int64{open, closed}); err != nil {
		t.Fatal(err)
	}
	w.want(w.call(deleteBayesModel, w.editor, bayesCall{method: "DELETE", path: "/x", values: mid}), 204)
	if w.versions(id) != 0 || get(w.admin, id).Code != 404 {
		t.Errorf("the model survived its delete")
	}
	if rec := w.call(deleteBayesModel, w.editor, bayesCall{method: "DELETE", path: "/x", values: mid}); rec.Code != 404 {
		t.Errorf("delete of a model that is gone: %d", rec.Code)
	}

	// ── the audit: every action, never a graph, a program or a label ──
	rows, err := w.db.Query(w.ctx, `SELECT action, coalesce(detail, '') FROM audit_logs WHERE target_type = 'bayes_model' AND actor_id = ANY($1)`,
		[]int64{w.editor.ID, w.runner.ID})
	if err != nil {
		t.Fatal(err)
	}
	actions := map[string]bool{}
	for rows.Next() {
		var action, detail string
		if err := rows.Scan(&action, &detail); err != nil {
			t.Fatal(err)
		}
		actions[action] = true
		if strings.Contains(detail, "format_version") || strings.Contains(detail, "parameters {") || strings.Contains(detail, `"graph"`) || len(detail) > 600 {
			t.Errorf("%s: a graph or a program in the detail: %s", action, detail)
		}
	}
	rows.Close()
	for _, a := range []string{"bayes_model.create", "bayes_model.update", "bayes_model.restore", "bayes_model.delete", "bayes_model.compile"} {
		if !actions[a] {
			t.Errorf("no audit record %s (%v)", a, actions)
		}
	}
}

// The search, Explore's recent objects, the folder's delete check and the deletion of a person.
func TestBayesModelsEverywhere(t *testing.T) {
	w := newBayesWorld(t)
	m := w.create("Searchable bayes", w.folder, w.editor)

	kinds := func(u *auth.User) []string {
		hits, err := searchHits(w.ctx, w.d, &auth.Principal{User: *u}, "searchable bayes")
		if err != nil {
			t.Fatal(err)
		}
		var out []string
		for _, h := range hits {
			out = append(out, fmt.Sprintf("%s:%d", h.Kind, h.ID))
		}
		return out
	}
	want := fmt.Sprintf("bayes_model:%d", m.ID)
	for _, u := range []*auth.User{w.editor, w.viewer, w.runner, w.admin} {
		if got := kinds(u); len(got) != 1 || got[0] != want {
			t.Errorf("search as %s: %v", u.FullName, got)
		}
	}
	if got := kinds(w.stranger); len(got) != 0 {
		t.Errorf("search as the stranger: %v", got)
	}
	w.group(true, false)
	if got := kinds(w.editor); len(got) != 0 {
		t.Errorf("search outside the group: %v", got)
	}
	if got := kinds(w.admin); len(got) != 1 {
		t.Errorf("search as an administrator with the group closed: %v", got)
	}

	recent := func(u *auth.User) []RecentItem {
		rec := w.call(func(d *Deps, rw http.ResponseWriter, r *http.Request, p *auth.Principal) { exploreRecent(d)(rw, r, p) }, u,
			bayesCall{method: "GET", path: "/explore/recent?limit=20"})
		w.want(rec, 200)
		var out RecentOut
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil {
			t.Fatal(err)
		}
		var mine []RecentItem
		for _, it := range out.Items {
			if it.Kind == "bayes" && it.ID == m.ID {
				mine = append(mine, it)
			}
		}
		return mine
	}
	if got := recent(w.editor); len(got) != 0 {
		t.Errorf("recent outside the group: %v", got)
	}
	w.group(true, true)
	if got := recent(w.editor); len(got) != 1 || got[0].Name != "Searchable bayes" || got[0].ProjectID != w.folder {
		t.Errorf("recent for the owner: %v", got)
	}
	// a person who saved a version of it, not its owner
	rec := w.call(updateBayesModel, w.editor, bayesCall{method: "PATCH", path: "/x", values: map[string]string{"model_id": bmID(m.ID)},
		body: body(t, map[string]any{"graph": withBox(t, bayesTemplate(t, "glm_linear"), 30), "base_version": 1})})
	w.want(rec, 200)
	if got := recent(w.viewer); len(got) != 0 {
		t.Errorf("recent for a person who touched nothing: %v", got)
	}
	if got := recent(w.admin); len(got) != 0 {
		t.Errorf("recent for an administrator who touched nothing: %v", got)
	}
	w.one(`INSERT INTO bayes_model_versions (model_id, version, graph, format_version, semantic_hash, findings, program, program_sha256,
		mapping, data_manifest, quantity_manifest, codegen_version, family, catalog_sha256, note, created_at, created_by)
		SELECT model_id, 3, graph, format_version, semantic_hash, findings, program, program_sha256, mapping, data_manifest,
		quantity_manifest, codegen_version, family, catalog_sha256, 'edited', now(), $2 FROM bayes_model_versions
		WHERE model_id = $1 AND version = 1 RETURNING id`, m.ID, w.viewer.ID)
	if got := recent(w.viewer); len(got) != 1 {
		t.Errorf("recent for the author of a version: %v", got)
	}

	// a folder holding a model cannot be deleted
	w.create("In the other folder", w.other, w.admin)
	refusal, err := removeFolder(w.ctx, w.d, w.admin, w.other)
	if err != nil || refusal == nil || refusal.Status != http.StatusConflict || refusal.Detail != "The folder contains Bayesian models: move or delete them first" {
		t.Errorf("delete of a folder with a model: %v %v", refusal, err)
	}

	// the people who made and own models can be deleted: what points at them is reset
	owner := w.editor
	if err := removeUser(w.ctx, w.d, &userRow{User: *owner}); err != nil {
		t.Fatalf("deleting a person who owns models: %v", err)
	}
	var ownerID *int64
	if err := w.db.QueryRow(w.ctx, `SELECT owner_id FROM bayes_models WHERE id = $1`, m.ID).Scan(&ownerID); err != nil || ownerID != nil {
		t.Errorf("owner after the delete: %v %v", ownerID, err)
	}
	var by *int64
	if err := w.db.QueryRow(w.ctx, `SELECT created_by FROM bayes_model_versions WHERE model_id = $1 AND version = 1`, m.ID).Scan(&by); err != nil || by != nil {
		t.Errorf("author after the delete: %v %v", by, err)
	}
}

// The routes of the module are Go-only and registered once, in the order the router needs.
func TestBayesModelRoutesAreRegistered(t *testing.T) {
	routes := map[string]bool{}
	for _, r := range Routes(&Deps{Config: &config.Config{}}) {
		routes[r.Method+" "+r.Pattern] = true
	}
	for _, want := range []string{
		"POST /bayes-models/check", "GET /bayes-models", "GET /projects/{project_id}/bayes-models", "POST /projects/{project_id}/bayes-models",
		"GET /bayes-models/{model_id}", "PATCH /bayes-models/{model_id}", "DELETE /bayes-models/{model_id}",
		"POST /bayes-models/{model_id}/duplicate", "GET /bayes-models/{model_id}/versions", "GET /bayes-models/{model_id}/versions/{version}",
		"POST /bayes-models/{model_id}/versions/{version}/restore", "POST /bayes-models/{model_id}/versions/{version}/compile",
		"GET /bayes-models/{model_id}/uses",
	} {
		if !routes[want] {
			t.Errorf("missing route %s", want)
		}
	}
}

// The lists carry each model's compile state, the flows that pin it and who saved its current
// version, from one read of each (no call per model).
func TestBayesModelListCarriesCompileUsesAndAuthor(t *testing.T) {
	w := newBayesWorld(t)
	a := w.create("List state A", w.folder, w.editor)
	b := w.create("List state B", w.folder, w.editor)
	flowDef := fmt.Sprintf(`{"nodes": [{"id": "n1", "type": "bayes_model", "data": {"bayesModelId": %d}}, {"id": "n2", "type": "bayes_model", "data": {"bayesModelId": %d}}], "edges": []}`, a.ID, a.ID)
	flow := w.one(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
		VALUES ('list uses', '', $1, $2, $3, 'duckdb', now(), now()) RETURNING id`, w.folder, w.editor.ID, flowDef)
	t.Cleanup(func() { _, _ = w.db.Exec(w.ctx, `DELETE FROM flows WHERE id = $1`, flow) })

	type listedRow struct {
		ID      int64 `json:"id"`
		Compile struct {
			State  string  `json:"state"`
			Detail string  `json:"detail"`
			At     *string `json:"at"`
		} `json:"compile"`
		Uses    int     `json:"uses"`
		SavedBy *string `json:"saved_by"`
	}
	read := func(h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal), values map[string]string) map[int64]listedRow {
		rec := w.call(h, w.viewer, bayesCall{method: "GET", path: "/x", values: values})
		w.want(rec, 200)
		var rows []listedRow
		if err := json.Unmarshal(rec.Body.Bytes(), &rows); err != nil {
			t.Fatal(err)
		}
		out := map[int64]listedRow{}
		for _, r := range rows {
			out[r.ID] = r
		}
		return out
	}
	for _, list := range []struct {
		h      func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)
		values map[string]string
	}{{listBayesModels, nil}, {listProjectBayesModels, map[string]string{"project_id": bmID(w.folder)}}} {
		got := read(list.h, list.values)
		if got[a.ID].Compile.State != "none" || got[a.ID].Uses != 1 || got[b.ID].Uses != 0 ||
			got[a.ID].SavedBy == nil || *got[a.ID].SavedBy != w.editor.Email {
			t.Errorf("list before: %+v", got)
		}
	}
	w.compiler.mu.Lock()
	w.compiler.state = bayesmodels.CompileReady
	w.compiler.mu.Unlock()
	if got := read(listBayesModels, nil); got[a.ID].Compile.State != "ready" || got[b.ID].Compile.State != "ready" ||
		got[a.ID].Compile.At == nil || got[a.ID].Compile.Detail != "x" {
		t.Errorf("list with a ready compile: %+v", got)
	}
}
