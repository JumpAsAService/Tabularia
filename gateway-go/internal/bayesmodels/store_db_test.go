package bayesmodels

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5"
	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
)

// The store on a PostgreSQL the Python migrations made (gateway-go/scripts/test.sh).

type storeFixture struct {
	t                               *testing.T
	ctx                             context.Context
	db                              *pgxpool.Pool
	store                           *Store
	compiler                        *fakeCompiler
	folder, other                   int64
	editor, viewer, stranger, admin *auth.User
}

func newStoreFixture(t *testing.T) *storeFixture {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set: run gateway-go/scripts/test.sh")
	}
	ctx := context.Background()
	db, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	// the auth package's tests TRUNCATE the users (and with them what refers to them): one at a time
	lock, err := db.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := lock.Exec(ctx, `SELECT pg_advisory_lock(7101)`); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = lock.Exec(ctx, `SELECT pg_advisory_unlock(7101)`); lock.Release() })
	f := &storeFixture{t: t, ctx: ctx, db: db, compiler: &fakeCompiler{}}
	f.store = &Store{DB: db, Compiler: f.compiler, Log: slog.New(slog.NewTextHandler(io.Discard, nil))}
	stamp := time.Now().UnixNano()
	user := func(name string, admin bool) *auth.User {
		email := fmt.Sprintf("bm-%s-%d@x.test", name, stamp)
		id := f.one(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, $2, 'x', true, $3, false, false, now()) RETURNING id`, email, name, admin)
		return &auth.User{ID: id, Email: email, FullName: name, IsActive: true, IsSuperuser: admin}
	}
	f.editor, f.viewer, f.stranger, f.admin = user("editor", false), user("viewer", false), user("stranger", false), user("admin", true)
	f.folder = f.one(`INSERT INTO projects (name, description, created_at) VALUES ($1, '', now()) RETURNING id`, fmt.Sprintf("bm-%d", stamp))
	f.other = f.one(`INSERT INTO projects (name, description, created_at) VALUES ($1, '', now()) RETURNING id`, fmt.Sprintf("bm-other-%d", stamp))
	f.one(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'edit') RETURNING id`, f.folder, f.editor.ID)
	f.one(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'view') RETURNING id`, f.folder, f.viewer.ID)
	f.one(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'manage') RETURNING id`, f.other, f.stranger.ID)
	t.Cleanup(func() {
		for _, q := range []string{
			`DELETE FROM flows WHERE project_id = ANY($1)`,
			`DELETE FROM bayes_model_versions WHERE model_id IN (SELECT id FROM bayes_models WHERE project_id = ANY($1))`,
			`DELETE FROM bayes_models WHERE project_id = ANY($1)`,
			`DELETE FROM permissions WHERE project_id = ANY($1)`,
			`DELETE FROM projects WHERE id = ANY($1)`,
		} {
			_, _ = db.Exec(ctx, q, []int64{f.folder, f.other})
		}
		_, _ = db.Exec(ctx, `DELETE FROM users WHERE id = ANY($1)`, []int64{f.editor.ID, f.viewer.ID, f.stranger.ID, f.admin.ID})
	})
	return f
}

func (f *storeFixture) one(sql string, args ...any) int64 {
	f.t.Helper()
	var id int64
	if err := f.db.QueryRow(f.ctx, sql, args...).Scan(&id); err != nil {
		f.t.Fatalf("%s: %v", sql, err)
	}
	return id
}

func (f *storeFixture) count(modelID int64) int64 {
	f.t.Helper()
	return f.one(`SELECT count(*) FROM bayes_model_versions WHERE model_id = $1`, modelID)
}

func (f *storeFixture) version(m *Model, v int64) *Version {
	f.t.Helper()
	got, err := GetVersion(f.ctx, f.db, m, v)
	if err != nil || got == nil {
		f.t.Fatalf("version %d: %v %v", v, got, err)
	}
	return got
}

// committed is whether another connection sees the version: what a compile Job would read.
func (f *storeFixture) committed(r CompileRequest) bool {
	var sha string
	err := f.db.QueryRow(f.ctx, `SELECT program_sha256 FROM bayes_model_versions WHERE model_id = $1 AND version = $2`,
		r.ModelID, r.Version).Scan(&sha)
	return err == nil && sha == r.ProgramSHA256
}

func errorGraph(t *testing.T) []byte {
	t.Helper()
	files, _ := filepath.Glob(filepath.Join(corpusDir, "*.json"))
	for _, path := range files {
		var cs struct {
			Input json.RawMessage `json:"input"`
		}
		readJSON(t, path, &cs)
		var r *Refused
		if _, _, err := Prepare(cs.Input); len(cs.Input) > 0 && errors.As(err, &r) {
			return cs.Input
		}
	}
	t.Fatal("no graph with an error in the corpus")
	return nil
}

func TestStoreOnADatabase(t *testing.T) {
	f := newStoreFixture(t)
	ctx := f.ctx
	g := template(t, "glm_linear")
	var enqueuedBeforeCommit bool
	f.compiler.onEnqueue = func(r CompileRequest) {
		if !f.committed(r) {
			enqueuedBeforeCommit = true
		}
	}

	// created: version 1, its program the generator's, the compile asked for once, after the commit
	tid := "glm_linear"
	s, err := f.store.Create(ctx, NewModel{ProjectID: f.folder, Name: "Prices", Description: "d", TemplateID: &tid, Graph: g}, f.editor.ID)
	if err != nil {
		t.Fatal(err)
	}
	m := s.Model
	want, _ := mustPrepare(t, g)
	v1 := f.version(m, 1)
	if m.CurrentVersion != 1 || v1.Note != NoteCreated || v1.Content != *want || *v1.CreatedBy != f.editor.ID {
		t.Fatalf("created: %+v", v1)
	}
	if m.TemplateVersion == nil || *m.TemplateVersion < 1 || *m.OwnerID != f.editor.ID {
		t.Fatalf("the template's version and the owner: %+v", m)
	}
	if len(f.compiler.enqueued) != 1 || !s.Enqueued || f.compiler.enqueued[0] != (CompileRequest{ModelID: m.ID, Version: 1,
		ProgramSHA256: want.ProgramSHA256, By: f.editor.ID}) {
		t.Fatalf("the compile: %+v", f.compiler.enqueued)
	}
	bad := "no_such_template"
	if _, err := f.store.Create(ctx, NewModel{ProjectID: f.folder, Name: "Bad", TemplateID: &bad, Graph: g}, f.editor.ID); !errors.Is(err, ErrTemplate) {
		t.Fatalf("an unknown template: %v", err)
	}

	// the same graph, written otherwise: nothing stored, nothing asked
	s, err = f.store.Save(ctx, m.ID, 1, []byte(" "+string(g)+"\n"), f.editor.ID, nil)
	if err != nil || s.Written || s.Version != 1 || f.count(m.ID) != 1 || len(f.compiler.enqueued) != 1 {
		t.Fatalf("an unchanged save: %+v %v", s, err)
	}

	// a box moved: version 2, the same program; its binary is ready, and Enqueue is asked all the same (it
	// queues nothing then, but takes the person's older queued version out: stan-compile, the owner's
	// correction of 2026-11-24 from C1)
	f.compiler.state = CompileState{State: CompileReady}
	s, err = f.store.Save(ctx, m.ID, 1, moved(t, g, 25), f.editor.ID, nil)
	if err != nil || !s.Written || s.Version != 2 || s.Note != NoteLayout || s.SemanticChanged || !s.Enqueued ||
		len(f.compiler.enqueued) != 2 || f.compiler.enqueued[1].Version != 2 {
		t.Fatalf("a layout-only save: %+v %v", s, err)
	}
	v2 := f.version(m, 2)
	if v2.Program != v1.Program || v2.ProgramSHA256 != v1.ProgramSHA256 || v2.SemanticHash != v1.SemanticHash || v2.Graph == v1.Graph {
		t.Fatal("a layout-only version keeps the program and the hash")
	}
	f.compiler.state = CompileState{}

	// a save made on version 1 while 2 is current: refused, nothing stored, nothing asked
	edited := relabelled(t, g, "Sale price")
	_, err = f.store.Save(ctx, m.ID, 1, edited, f.editor.ID, nil)
	var stale *Stale
	if !errors.As(err, &stale) || stale.Current != 2 || stale.Error() != "version 2 was saved by someone else meanwhile: reload to see it" ||
		f.count(m.ID) != 2 || len(f.compiler.enqueued) != 2 {
		t.Fatalf("a stale save: %v", err)
	}

	// a graph with an error: refused with its findings, nothing stored, nothing asked
	_, err = f.store.Save(ctx, m.ID, 2, errorGraph(t), f.editor.ID, nil)
	var refused *Refused
	if !errors.As(err, &refused) || len(refused.Findings) == 0 || f.count(m.ID) != 2 || len(f.compiler.enqueued) != 2 {
		t.Fatalf("a graph with an error: %v", err)
	}

	// an edit on the current version, with a rename in the same transaction
	s, err = f.store.Save(ctx, m.ID, 2, edited, f.editor.ID, func(tx pgx.Tx) error {
		_, err := tx.Exec(ctx, `UPDATE bayes_models SET name = 'Sale prices' WHERE id = $1`, m.ID)
		return err
	})
	if err != nil || s.Version != 3 || s.Note != NoteEdited || !s.SemanticChanged || !s.Enqueued || s.Model.Name != "Sale prices" ||
		s.Model.CurrentVersion != 3 {
		t.Fatalf("an edit: %+v %v", s, err)
	}
	// a rename that fails undoes the version
	_, err = f.store.Save(ctx, m.ID, 3, relabelled(t, g, "Third"), f.editor.ID, func(pgx.Tx) error { return errors.New("no") })
	if err == nil || f.count(m.ID) != 3 {
		t.Fatalf("a failing companion statement: %v, %d versions", err, f.count(m.ID))
	}

	// the compile's error never fails the save
	f.compiler.enqueueErr = errors.New("the compile queue is down")
	s, err = f.store.Save(ctx, m.ID, 3, relabelled(t, g, "Fourth"), f.editor.ID, nil)
	if err != nil || s.Version != 4 || s.Enqueued {
		t.Fatalf("a save whose compile could not be asked for: %+v %v", s, err)
	}
	f.compiler.enqueueErr = nil

	// two saves on the same base: one is written, the other is stale
	var wg sync.WaitGroup
	errs := make([]error, 2)
	graphs := [][]byte{relabelled(t, g, "Concurrent 0"), relabelled(t, g, "Concurrent 1")}
	for i := range errs {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			_, errs[i] = f.store.Save(ctx, m.ID, 4, graphs[i], f.editor.ID, nil)
		}(i)
	}
	wg.Wait()
	if (errs[0] == nil) == (errs[1] == nil) || f.count(m.ID) != 5 {
		t.Fatalf("two saves on version 4: %v, %v", errs[0], errs[1])
	}
	for _, err := range errs {
		if err != nil && !errors.As(err, &stale) {
			t.Fatalf("the second save: %v", err)
		}
	}

	// restore: a new version holding version 1's graph and program, byte for byte
	s, err = f.store.Restore(ctx, m.ID, 1, f.viewer.ID)
	if err != nil || s.Version != 6 || s.Note != "restored from v1" || !s.SemanticChanged {
		t.Fatalf("restore: %+v %v", s, err)
	}
	v6 := f.version(m, 6)
	if v6.Content != v1.Content || v6.Note != "restored from v1" || *v6.CreatedBy != f.viewer.ID {
		t.Fatal("a restored version copies the graph, the program, its sha256, the generator's version, the mapping and the manifests")
	}
	if _, err := f.store.Restore(ctx, m.ID, 99, f.editor.ID); !errors.Is(err, ErrNotFound) || f.count(m.ID) != 6 {
		t.Fatalf("a version that is not there: %v", err)
	}
	if _, err := f.store.Restore(ctx, -1, 1, f.editor.ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("a model that is not there: %v", err)
	}

	// the versions, newest first, without graphs
	vs, err := Versions(ctx, f.db, m.ID)
	if err != nil || len(vs) != 6 || vs[0].Version != 6 || vs[5].Version != 1 || vs[0].ProgramSHA256 != v1.ProgramSHA256 || vs[0].Family != FamilyGLM {
		t.Fatalf("versions: %+v %v", vs, err)
	}

	// duplicate: a copy in the same folder, its version 1 the current version's bytes
	s, err = f.store.Duplicate(ctx, m, f.viewer.ID)
	if err != nil {
		t.Fatal(err)
	}
	cp := s.Model
	if cp.ID == m.ID || cp.ProjectID != f.folder || cp.Name == "Sale prices" || cp.CurrentVersion != 1 || *cp.OwnerID != f.viewer.ID {
		t.Fatalf("the copy: %+v", cp)
	}
	c1 := f.version(cp, 1)
	if c1.Content != v6.Content || c1.Note != "duplicated from «Sale prices» v6" {
		t.Fatalf("the copy's version: %q", c1.Note)
	}
	if s2, err := f.store.Duplicate(ctx, m, f.viewer.ID); err != nil || s2.Model.Name == cp.Name {
		t.Fatalf("a second copy takes another name: %v", err)
	}

	// pinned by a flow: «used by», the newer version, the delete refused
	def := fmt.Sprintf(`{"nodes": [{"id": "n1", "type": "bayes_model", "data": {"bayesModelId": %d, "bayesModelVersion": 2}},
		{"id": "n2", "type": "bayes_model", "data": {"bayesModelId": %d, "bayesModelVersion": 3}}], "edges": []}`, m.ID, cp.ID)
	flow := f.one(`INSERT INTO flows (name, description, project_id, owner_id, definition, engine, created_at, updated_at)
		VALUES ('Weekly prices', '', $1, $2, $3, 'duckdb', now(), now()) RETURNING id`, f.other, f.stranger.ID, def)
	uses, err := Using(ctx, f.db, m.ID)
	if err != nil || len(uses) != 1 || uses[0] != (Use{FlowID: flow, FlowName: "Weekly prices", FlowProjectID: f.other, NodeID: "n1", Version: 2}) {
		t.Fatalf("using: %+v %v", uses, err)
	}
	err = f.store.Delete(ctx, m.ID)
	var inUse *InUse
	if !errors.As(err, &inUse) || len(inUse.Flows) != 1 || inUse.Flows[0] != "Weekly prices" || f.count(m.ID) != 6 {
		t.Fatalf("delete while pinned: %v", err)
	}
	for pinned, want := range map[int64]Newness{
		2:  {Current: 6, Newer: true},                                      // a box moved since: not changed
		4:  {Current: 6, Newer: true, Changed: true},                       // a label since
		6:  {Current: 6},                                                   // the current one
		99: {Current: 6, Newer: false, Changed: true, PinnedMissing: true}, // not there
	} {
		n, err := Newer(ctx, f.db, m.ID, pinned)
		if err != nil || n == nil || *n != want {
			t.Errorf("newer than v%d: %+v %v", pinned, n, err)
		}
	}
	if n, err := Newer(ctx, f.db, -1, 1); n != nil || err != nil {
		t.Errorf("newer of a model that is not there: %+v %v", n, err)
	}

	// visibility: VIEW and the group's use; administrators always; one 404 for all
	check := func(who *auth.User, usable bool, see, run, edit bool) {
		t.Helper()
		c := perm.For(f.db, who)
		gotSee, err1 := CanSee(ctx, c, m, usable)
		gotRun, err2 := CanRun(ctx, c, m, usable)
		gotEdit, err3 := CanEdit(ctx, c, m, usable)
		if err := errors.Join(err1, err2, err3); err != nil || gotSee != see || gotRun != run || gotEdit != edit {
			t.Errorf("%s (group usable %v): see %v run %v edit %v, want %v %v %v (%v)", who.FullName, usable, gotSee, gotRun,
				gotEdit, see, run, edit, err)
		}
		_, refusal, err := GetVisible(ctx, c, f.db, m.ID, usable)
		if err != nil || (refusal == nil) != see || (refusal != nil && !isNotFound(refusal)) {
			t.Errorf("%s: GetVisible %+v %v", who.FullName, refusal, err)
		}
		list, err := Visible(ctx, c, f.db, nil, usable)
		found := false
		for _, l := range list {
			if l.ID == m.ID {
				found = l.Family == FamilyGLM && l.CurrentVersion == 6
			}
		}
		if err != nil || found != see {
			t.Errorf("%s: in the list %v, want %v (%v)", who.FullName, found, see, err)
		}
	}
	check(f.editor, true, true, true, true)
	check(f.editor, false, false, false, false)
	check(f.viewer, true, true, false, false)
	check(f.viewer, false, false, false, false)
	check(f.stranger, true, false, false, false)
	check(f.admin, false, true, true, true)
	_, refusal, err := GetVisible(ctx, perm.For(f.db, f.admin), f.db, -1, true)
	if err != nil || refusal == nil || !isNotFound(refusal) {
		t.Errorf("a model that does not exist: %+v %v", refusal, err)
	}
	if list, err := Visible(ctx, perm.For(f.db, f.editor), f.db, []int64{f.other}, true); err != nil || len(list) != 0 {
		t.Errorf("the editor's list in a folder they cannot read: %+v %v", list, err)
	}
	if list, err := Visible(ctx, perm.For(f.db, f.viewer), f.db, []int64{f.folder}, true); err != nil || len(list) != 3 {
		t.Errorf("the viewer's list of the folder: %d %v", len(list), err)
	}

	// the node removed: the delete goes through, with the versions
	if _, err := f.db.Exec(ctx, `UPDATE flows SET definition = '{"nodes": [], "edges": []}' WHERE id = $1`, flow); err != nil {
		t.Fatal(err)
	}
	if err := f.store.Delete(ctx, m.ID); err != nil {
		t.Fatal(err)
	}
	if got, err := Get(ctx, f.db, m.ID); got != nil || err != nil || f.count(m.ID) != 0 {
		t.Fatalf("deleted: %+v %v", got, err)
	}
	if err := f.store.Delete(ctx, m.ID); !errors.Is(err, ErrNotFound) {
		t.Fatalf("deleted twice: %v", err)
	}
	if _, err := f.store.Save(ctx, m.ID, 6, g, f.editor.ID, nil); !errors.Is(err, ErrNotFound) {
		t.Fatalf("a save on a deleted model: %v", err)
	}

	if enqueuedBeforeCommit {
		t.Fatal("a compile was asked for before its version was committed")
	}
}

func isNotFound(r *auth.Refusal) bool {
	return r.Status == http.StatusNotFound && r.Detail == NotFoundDetail && r.Header == nil
}
