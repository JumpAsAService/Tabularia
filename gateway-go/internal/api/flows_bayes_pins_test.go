package api

import (
	"encoding/json"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayesmodels"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

// A flow's save that pins a saved Bayesian model (docs/specs/bayes-flow-node.md, «Saving a flow that
// pins a model», lot F1), on a PostgreSQL the Python migrations made (gateway-go/scripts/test.sh):
// every write of flows.definition waits on the model's advisory lock, so a save and a delete of the
// model never both win, in either order; the locks in increasing id order (no deadlock); a pinned
// version that does not exist, a new pin of a model the saver cannot see (the same sentence as an
// absent one), an unchanged pin of a model the saver lost sight of, and a new pin as reach.
//
// The order of the save and the delete is forced from outside: the test holds the model's lock, starts
// one and then the other, each seen waiting in pg_locks (PostgreSQL grants a lock to its waiters in
// the order they came), and lets them go.

type pinWorld struct {
	*bayesWorld
	big *pgxpool.Pool
}

func newPinWorld(t *testing.T) *pinWorld {
	t.Helper()
	w := &pinWorld{bayesWorld: newBayesWorld(t)}
	// the routes of a save waiting on a lock, a delete waiting behind it, the lock's holder and the
	// polls of pg_locks hold connections at once: a pool that cannot run short
	cfg, err := pgxpool.ParseConfig(os.Getenv("TEST_DATABASE_URL"))
	if err != nil {
		t.Fatal(err)
	}
	cfg.MaxConns = 24
	if w.big, err = pgxpool.NewWithConfig(w.ctx, cfg); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(w.big.Close)
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	w.d.DB, w.d.Panel = w.big, settings.New(w.d.Config, false, log)
	return w
}

// pinNode is a Bayesian model node of a definition.
type pinNode struct {
	id, label      string
	model, version int64
}

func pinDefinition(nodes ...pinNode) string {
	list := []any{}
	for _, n := range nodes {
		list = append(list, map[string]any{"id": n.id, "type": bayesmodels.NodeType, "data": map[string]any{
			"label": n.label, bayesmodels.KeyModel: n.model, bayesmodels.KeyVersion: n.version}})
	}
	b, _ := json.Marshal(map[string]any{"nodes": list, "edges": []any{}})
	return string(b)
}

// send runs a route that reads a pydantic body.
func (w *pinWorld) send(h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal, pyd.Body), u *auth.User,
	values map[string]string, fields map[string]any) func() *httptest.ResponseRecorder {
	w.t.Helper()
	raw, err := json.Marshal(fields)
	if err != nil {
		w.t.Fatal(err)
	}
	b, resp := pyd.ReadBody(raw, "application/json")
	if resp != nil {
		w.t.Fatalf("the body: %v", resp)
	}
	return func() *httptest.ResponseRecorder {
		r := httptest.NewRequest(http.MethodPost, "/x", nil)
		for k, v := range values {
			r.SetPathValue(k, v)
		}
		rec := httptest.NewRecorder()
		h(w.d, rec, r, &auth.Principal{User: *u}, b)
		return rec
	}
}

// plain runs a route without a body.
func (w *pinWorld) plain(h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal), u *auth.User,
	values map[string]string) func() *httptest.ResponseRecorder {
	return func() *httptest.ResponseRecorder {
		r := httptest.NewRequest(http.MethodPost, "/x", nil)
		for k, v := range values {
			r.SetPathValue(k, v)
		}
		rec := httptest.NewRecorder()
		h(w.d, rec, r, &auth.Principal{User: *u})
		return rec
	}
}

func (w *pinWorld) createFlow(u *auth.User, name, definition string) func() *httptest.ResponseRecorder {
	return w.send((*Deps).createFlow, u, map[string]string{"project_id": bmID(w.folder)},
		map[string]any{"name": name, "definition": definition})
}

func (w *pinWorld) patchFlow(u *auth.User, flow int64, fields map[string]any) func() *httptest.ResponseRecorder {
	return w.send((*Deps).updateFlow, u, map[string]string{"flow_id": bmID(flow)}, fields)
}

func (w *pinWorld) deleteModel(u *auth.User, model int64) func() *httptest.ResponseRecorder {
	return w.plain(deleteBayesModel, u, map[string]string{"model_id": bmID(model)})
}

// flow makes a flow as the editor and answers its id.
func (w *pinWorld) flow(name, definition string) int64 {
	w.t.Helper()
	rec := w.createFlow(w.editor, name, definition)()
	w.want(rec, http.StatusCreated)
	var out struct {
		ID int64 `json:"id"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil || out.ID == 0 {
		w.t.Fatalf("%s %v", rec.Body, err)
	}
	return out.ID
}

func (w *pinWorld) definitionOf(flow int64) string {
	w.t.Helper()
	var def string
	if err := w.big.QueryRow(w.ctx, `SELECT definition FROM flows WHERE id = $1`, flow).Scan(&def); err != nil {
		w.t.Fatal(err)
	}
	return def
}

// hold takes a model's lock in a transaction of the test's; release lets the waiters go.
func (w *pinWorld) hold(model int64) (release func()) {
	w.t.Helper()
	tx, err := w.big.Begin(w.ctx)
	if err != nil {
		w.t.Fatal(err)
	}
	if _, err := tx.Exec(w.ctx, `SELECT pg_advisory_xact_lock($1::integer, $2::integer)`, bayesmodels.Lock, model); err != nil {
		w.t.Fatal(err)
	}
	done := false
	release = func() {
		if !done {
			done = true
			_ = tx.Rollback(w.ctx)
		}
	}
	w.t.Cleanup(release) // a failed test does not leave the routes waiting
	return release
}

// waiting waits until n transactions wait on the model's lock.
func (w *pinWorld) waiting(model int64, n int, what string) {
	w.t.Helper()
	deadline := time.Now().Add(10 * time.Second)
	for {
		var got int
		if err := w.big.QueryRow(w.ctx, `SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND NOT granted
			AND classid::bigint = $1 AND objid::bigint = $2 AND objsubid = 2`, bayesmodels.Lock, model).Scan(&got); err != nil {
			w.t.Fatal(err)
		}
		if got >= n {
			return
		}
		if time.Now().After(deadline) {
			w.t.Fatalf("%s: %d waiting on the model's lock, want %d", what, got, n)
		}
		time.Sleep(20 * time.Millisecond)
	}
}

// start runs a route in the background.
func start(route func() *httptest.ResponseRecorder) <-chan *httptest.ResponseRecorder {
	out := make(chan *httptest.ResponseRecorder, 1)
	go func() { out <- route() }()
	return out
}

func (w *pinWorld) result(c <-chan *httptest.ResponseRecorder, what string) *httptest.ResponseRecorder {
	w.t.Helper()
	select {
	case rec := <-c:
		return rec
	case <-time.After(20 * time.Second):
		w.t.Fatalf("%s: no answer", what)
		return nil
	}
}

func (w *pinWorld) modelExists(id int64) bool {
	w.t.Helper()
	var n int
	if err := w.big.QueryRow(w.ctx, `SELECT count(*) FROM bayes_models WHERE id = $1`, id).Scan(&n); err != nil {
		w.t.Fatal(err)
	}
	return n == 1
}

func TestAFlowSavedWithAPinAndTheModelsDeleteNeverBothWin(t *testing.T) {
	w := newPinWorld(t)

	// the save first: the delete waits for its commit and is refused with the flow's name
	m := w.create("Wages", w.folder, w.editor).ID
	f := w.flow("Pays", pinDefinition())
	release := w.hold(m)
	save := start(w.patchFlow(w.editor, f, map[string]any{"definition": pinDefinition(pinNode{"b", "Wages model", m, 1})}))
	w.waiting(m, 1, "the save")
	del := start(w.deleteModel(w.editor, m))
	w.waiting(m, 2, "the delete behind the save")
	release()
	w.want(w.result(save, "the save"), http.StatusOK)
	rec := w.result(del, "the delete")
	w.want(rec, http.StatusConflict)
	if !strings.Contains(rec.Body.String(), `"Pays"`) || !w.modelExists(m) {
		t.Errorf("the delete after the save: %s, the model there: %v", rec.Body, w.modelExists(m))
	}
	if len(bayesmodels.Pinned(w.definitionOf(f))) != 1 {
		t.Errorf("the pin not saved: %s", w.definitionOf(f))
	}

	// the delete first: the save waits for its commit and finds the model gone
	gone := w.create("Rents", w.folder, w.editor).ID
	release = w.hold(gone)
	del = start(w.deleteModel(w.editor, gone))
	w.waiting(gone, 1, "the delete")
	create := start(w.createFlow(w.editor, "Lets", pinDefinition(pinNode{"b", "Rents model", gone, 1})))
	w.waiting(gone, 2, "the save behind the delete")
	release()
	w.want(w.result(del, "the delete"), http.StatusNoContent)
	rec = w.result(create, "the save")
	w.want(rec, http.StatusUnprocessableEntity)
	if !strings.Contains(rec.Body.String(), "Bayesian model «Rents model»: choose a model you may see") {
		t.Errorf("the save after the delete: %s", rec.Body)
	}
	if n := w.one(`SELECT count(*) FROM flows WHERE project_id = $1 AND name = 'Lets'`, w.folder); n != 0 {
		t.Errorf("the flow pinning a deleted model was written")
	}
}

// Every write of flows.definition takes the lock: the update, the creation, the duplicate, the
// promotion of a version and the schedule's save (which writes the definition back as it read it).
func TestEveryWriteOfAFlowTakesThePinnedModelsLock(t *testing.T) {
	w := newPinWorld(t)
	m := w.create("Prices", w.folder, w.editor).ID
	pinned := pinDefinition(pinNode{"b", "Prices", m, 1})
	f := w.flow("Pinned", pinned)
	// a version that pins it and a current definition that does not: the promotion pins it again
	promoted := w.flow("Promoted", pinned)
	w.want(w.patchFlow(w.editor, promoted, map[string]any{"definition": pinDefinition()})(), http.StatusOK)
	for _, c := range []struct {
		name   string
		route  func() *httptest.ResponseRecorder
		status int
	}{
		{"the update", w.patchFlow(w.editor, f, map[string]any{"definition": pinDefinition(pinNode{"b", "Renamed", m, 1})}), http.StatusOK},
		{"the creation", w.createFlow(w.editor, "Created", pinned), http.StatusCreated},
		{"the duplicate", w.plain((*Deps).duplicateFlow, w.editor, map[string]string{"flow_id": bmID(f)}), http.StatusCreated},
		{"the promotion", w.plain((*Deps).promoteFlowVersion, w.editor, map[string]string{"flow_id": bmID(promoted), "version": "1"}), http.StatusOK},
		{"the schedule", w.send((*Deps).setFlowSchedule, w.editor, map[string]string{"flow_id": bmID(f)}, map[string]any{"cron": nil}), http.StatusOK},
	} {
		release := w.hold(m)
		out := start(c.route)
		w.waiting(m, 1, c.name)
		release()
		w.want(w.result(out, c.name), c.status)
	}
	if len(bayesmodels.Pinned(w.definitionOf(promoted))) != 1 {
		t.Errorf("the promotion did not pin: %s", w.definitionOf(promoted))
	}
	// and the model, pinned, is not deleted
	w.want(w.deleteModel(w.editor, m)(), http.StatusConflict)
}

// Two saves pinning two models in opposite node orders both finish: the locks are taken by
// increasing id. The order is forced so that node order would deadlock: the save of (a, b) waits on
// a first, then the save of (b, a) would take b and wait on a; let go, the first would take a and
// wait on b, held by the second (PostgreSQL would break the circle by failing one).
func TestTwoSavesPinningTwoModelsInOppositeOrdersFinish(t *testing.T) {
	w := newPinWorld(t)
	a := w.create("Alpha", w.folder, w.editor).ID
	b := w.create("Beta", w.folder, w.editor).ID
	if a > b {
		a, b = b, a
	}
	one, two := w.flow("One", pinDefinition()), w.flow("Two", pinDefinition())
	release := w.hold(a)
	first := start(w.patchFlow(w.editor, one, map[string]any{"definition": pinDefinition(
		pinNode{"x", "First", a, 1}, pinNode{"y", "Second", b, 1})}))
	w.waiting(a, 1, "the save of (a, b)")
	second := start(w.patchFlow(w.editor, two, map[string]any{"definition": pinDefinition(
		pinNode{"x", "Second", b, 1}, pinNode{"y", "First", a, 1})}))
	w.waiting(a, 2, "the save of (b, a)")
	release()
	w.want(w.result(first, "the save of (a, b)"), http.StatusOK)
	w.want(w.result(second, "the save of (b, a)"), http.StatusOK)
}

func TestThePinsOfASavedFlowAreChecked(t *testing.T) {
	w := newPinWorld(t)
	m := w.create("Scores", w.folder, w.editor).ID
	hidden := w.create("Hidden", w.other, w.stranger).ID
	refused := func(rec *httptest.ResponseRecorder, sentence string) string {
		t.Helper()
		w.want(rec, http.StatusUnprocessableEntity)
		var out struct {
			Detail string `json:"detail"`
		}
		if err := json.Unmarshal(rec.Body.Bytes(), &out); err != nil || out.Detail != sentence {
			t.Errorf("refused with %s, want %q", rec.Body, sentence)
		}
		return rec.Body.String()
	}

	// a version that does not exist
	refused(w.createFlow(w.editor, "No version", pinDefinition(pinNode{"b", "School scores", m, 9}))(),
		"Bayesian model «School scores»: the model or its version no longer exists: choose another")
	refused(w.createFlow(w.editor, "No version named", pinDefinition(pinNode{"b", "School scores", m, 0}))(),
		"Bayesian model «School scores»: the model or its version no longer exists: choose another")

	// a new pin of a model the saver cannot see: the answer of a model that does not exist
	unseen := refused(w.createFlow(w.editor, "Unseen", pinDefinition(pinNode{"b", "School scores", hidden, 1}))(),
		"Bayesian model «School scores»: choose a model you may see")
	absent := refused(w.createFlow(w.editor, "Absent", pinDefinition(pinNode{"b", "School scores", hidden + 1000000, 1}))(),
		"Bayesian model «School scores»: choose a model you may see")
	if unseen != absent {
		t.Errorf("a model not seen and an absent one answer differently: %s / %s", unseen, absent)
	}
	// an id out of the column's range is an absent model, not a failure
	refused(w.createFlow(w.editor, "Too big", pinDefinition(pinNode{"b", "School scores", 1 << 40, 1}))(),
		"Bayesian model «School scores»: choose a model you may see")
	// the stranger sees it: the same pin is theirs to save (in a flow of their folder)
	w.want(w.send((*Deps).createFlow, w.stranger, map[string]string{"project_id": bmID(w.other)},
		map[string]any{"name": "Theirs", "definition": pinDefinition(pinNode{"b", "Mine", hidden, 1})})(), http.StatusCreated)

	// the group not usable: no model is seen, whatever the folder
	w.group(true, false)
	refused(w.createFlow(w.editor, "Group off", pinDefinition(pinNode{"b", "S", m, 1}))(),
		"Bayesian model «S»: choose a model you may see")
	w.group(true, true)

	// an unchanged pin of a model the saver lost sight of stays: the flow is not broken
	f := w.flow("Kept", pinDefinition(pinNode{"b", "Scores", m, 1}))
	if _, err := w.big.Exec(w.ctx, `UPDATE bayes_models SET project_id = $2 WHERE id = $1`, m, w.other); err != nil {
		t.Fatal(err)
	}
	w.want(w.patchFlow(w.editor, f, map[string]any{"definition": pinDefinition(pinNode{"b", "Scores, renamed", m, 1})})(), http.StatusOK)
	w.want(w.patchFlow(w.editor, f, map[string]any{"name": "Kept, renamed"})(), http.StatusOK)
	// a second node pinning it, or the same node moved to another version, is new: refused
	refused(w.patchFlow(w.editor, f, map[string]any{"definition": pinDefinition(pinNode{"b", "Scores", m, 1}, pinNode{"c", "Again", m, 1})})(),
		"Bayesian model «Again»: choose a model you may see")
	refused(w.patchFlow(w.editor, f, map[string]any{"definition": pinDefinition(pinNode{"b", "Scores", m, 2})})(),
		"Bayesian model «Scores»: choose a model you may see")
	// a node that pinned another model before is a changed pin
	visible := w.create("Visible", w.folder, w.editor).ID
	refused(w.patchFlow(w.editor, w.flow("Other", pinDefinition(pinNode{"b", "Scores", visible, 1})),
		map[string]any{"definition": pinDefinition(pinNode{"b", "Scores", m, 1})})(), "Bayesian model «Scores»: choose a model you may see")
	if got := bayesmodels.Pinned(w.definitionOf(f)); len(got) != 1 || got[0].ModelID != m || got[0].Version != 1 {
		t.Errorf("the flow after the refusals: %v", got)
	}
	// the administrator sees every model; a duplicate of their flow by the editor is a new pin, refused
	rec := w.createFlow(w.admin, "Admin's", pinDefinition(pinNode{"b", "S", hidden, 1}))()
	w.want(rec, http.StatusCreated)
	var admins struct {
		ID int64 `json:"id"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &admins); err != nil {
		t.Fatal(err)
	}
	refused(w.plain((*Deps).duplicateFlow, w.editor, map[string]string{"flow_id": bmID(admins.ID)})(),
		"Bayesian model «S»: choose a model you may see")
}

// Someone else's new pin switches the schedule's author off (the reach widens); a change of the
// node's settings does not.
func TestSomeoneElsesNewPinSwitchesTheScheduleOff(t *testing.T) {
	w := newPinWorld(t)
	m := w.create("Sales", w.folder, w.editor).ID
	f := w.flow("Scheduled", pinDefinition(pinNode{"b", "Sales", m, 1}))
	schedule := func() {
		t.Helper()
		if _, err := w.big.Exec(w.ctx, `UPDATE flows SET run_schedule = '0 3 * * *', run_scheduled_by = $2, next_run_at = now()
			WHERE id = $1`, f, w.runner.ID); err != nil {
			t.Fatal(err)
		}
	}
	scheduled := func() bool {
		var on bool
		if err := w.big.QueryRow(w.ctx, `SELECT run_schedule IS NOT NULL FROM flows WHERE id = $1`, f).Scan(&on); err != nil {
			t.Fatal(err)
		}
		return on
	}
	schedule()
	w.want(w.patchFlow(w.editor, f, map[string]any{"definition": pinDefinition(pinNode{"b", "Sales, relabelled", m, 1})})(), http.StatusOK)
	if !scheduled() {
		t.Error("a new label switched the schedule off")
	}
	other := w.create("Costs", w.folder, w.editor).ID
	w.want(w.patchFlow(w.editor, f, map[string]any{"definition": pinDefinition(pinNode{"b", "Costs", other, 1})})(), http.StatusOK)
	if scheduled() {
		t.Error("someone else's new pin left the schedule on")
	}
}
