package api

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/bayescompile"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/engine"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// The card of the group bayes (models_admin_bayes.go, docs/specs/bayes-stan-compile.md lot C4): its three routes, their
// rights, the ranges, the audit, the cache's figures and «Empty the cache».

func TestTheBayesCardRoutesAreRegisteredAndGoOnly(t *testing.T) {
	want := [][2]string{{"GET", "/admin/models/bayes/compile"}, {"PUT", "/admin/models/bayes/compile"}, {"POST", "/admin/models/bayes/cache/empty"}}
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
			t.Errorf("%v: registered %v, declared Go-only %v", r, have[r], goOnly[r])
		}
	}
}

// the card's world: the compile world, and the three routes as a person calls them
type cardWorld struct{ *compileWorld }

func newCardWorld(t *testing.T) *cardWorld { return &cardWorld{newCompileWorld(t)} }

func (w *cardWorld) call(u *auth.User, method, body string, admin bool, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) *httptest.ResponseRecorder {
	return w.callAs(&auth.Principal{User: *u}, method, body, admin, h)
}

func (w *cardWorld) callAs(p *auth.Principal, method, body string, admin bool, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) *httptest.ResponseRecorder {
	w.t.Helper()
	r := httptest.NewRequest(method, "/x", bytes.NewReader([]byte(body)))
	rec := httptest.NewRecorder()
	mgGuarded(w.c.d, admin, h)(rec, r, p)
	return rec
}

type cardAnswer struct {
	Limits, Defaults bayesCardValues
	Cache            bayesCardCache
	Compiles         []bayesCardCompile
	Editable         bool
	Emptied          *struct {
		Binaries int64 `json:"binaries"`
		Bytes    int64 `json:"bytes"`
		Kept     int   `json:"kept"`
	}
}

func (w *cardWorld) get(u *auth.User) cardAnswer {
	w.t.Helper()
	rec := w.call(u, "GET", "", false, bayesCardGet)
	if rec.Code != http.StatusOK {
		w.t.Fatalf("GET: %d %s", rec.Code, rec.Body)
	}
	return decodeCard(w.t, rec)
}

func decodeCard(t *testing.T, rec *httptest.ResponseRecorder) cardAnswer {
	t.Helper()
	var a cardAnswer
	if err := json.Unmarshal(rec.Body.Bytes(), &a); err != nil {
		t.Fatal(err)
	}
	return a
}

// seed writes a ready row holding a binary of the bytes given, kept in Tabularia's storage, last used at that time.
func (w *cardWorld) seed(key, digest string, binary []byte, used time.Time, model int64) int64 {
	w.t.Helper()
	var id int64
	if err := w.db.QueryRow(w.ctx, `INSERT INTO bayes_binaries (cache_key, program_sha256, cmdstan, image_digest, arch, group_version, state,
		requested_by, model_id, version, binary_sha256, binary_bytes, seconds, queued_at, finished_at, last_used_at)
		VALUES ($1, $2, '2.40.0', $3, 'amd64', 2, 'ready', $4, $5, 1, $6, $7, 12.5, $8, $8, $8) RETURNING id`,
		strings.Repeat(key, 64)[:64], strings.Repeat(key, 64)[:64], digest, w.admin.ID, nilIf(model), sha(binary), len(binary), used).Scan(&id); err != nil {
		w.t.Fatal(err)
	}
	w.engine.mu.Lock()
	w.engine.storage["bayes-binaries/"+sha(binary)] = binary
	w.engine.mu.Unlock()
	return id
}

func nilIf(n int64) any {
	if n == 0 {
		return nil
	}
	return n
}

func (w *cardWorld) stored() (string, string) {
	w.t.Helper()
	var cpu, mem, tmp *string
	var secs, slots, cache *int64
	if err := w.db.QueryRow(w.ctx, `SELECT compile_cpu, compile_memory, compile_seconds, compile_tmp, compile_slots, cache_max_bytes, sampling_tmp
		FROM bayes_settings WHERE id = 1`).Scan(&cpu, &mem, &secs, &tmp, &slots, &cache, new(*string)); err != nil {
		return "no row", ""
	}
	show := func(p any) string {
		switch v := p.(type) {
		case *string:
			if v != nil {
				return *v
			}
		case *int64:
			if v != nil {
				return fmt.Sprint(*v)
			}
		}
		return "-"
	}
	return fmt.Sprintf("%s|%s|%s|%s|%s|%s", show(cpu), show(mem), show(secs), show(tmp), show(slots), show(cache)), ""
}

func (w *cardWorld) audits() []string {
	w.t.Helper()
	rows, err := w.db.Query(w.ctx, `SELECT action || ' ' || coalesce(detail, '') FROM audit_logs WHERE action LIKE 'models.bayes_%' ORDER BY id`)
	if err != nil {
		w.t.Fatal(err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var s string
		if err := rows.Scan(&s); err != nil {
			w.t.Fatal(err)
		}
		out = append(out, strings.ReplaceAll(s, `": `, `":`))
	}
	return out
}

func TestTheBayesCardRightsAreTheModelsCards(t *testing.T) {
	w := newCardWorld(t)
	for _, c := range []struct {
		name string
		do   func(u *auth.User) *httptest.ResponseRecorder
		read bool
	}{
		{"get", func(u *auth.User) *httptest.ResponseRecorder { return w.call(u, "GET", "", false, bayesCardGet) }, true},
		{"put", func(u *auth.User) *httptest.ResponseRecorder { return w.call(u, "PUT", `{}`, true, bayesCardPut) }, false},
		{"empty", func(u *auth.User) *httptest.ResponseRecorder { return w.call(u, "POST", "", true, bayesCacheEmpty) }, false},
	} {
		if rec := c.do(w.ann); rec.Code != http.StatusForbidden {
			t.Errorf("%s as a person who edits flows: %d", c.name, rec.Code)
		}
		rec := c.do(w.observer)
		if c.read && rec.Code != http.StatusOK || !c.read && rec.Code != http.StatusForbidden {
			t.Errorf("%s as an observer: %d %s", c.name, rec.Code, rec.Body)
		}
	}
	// an API token does not act, even an administrator's, and reads as the person does
	tok := &auth.Principal{User: *w.admin, TokenName: "ci"}
	for name, h := range map[string]func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal){"PUT": bayesCardPut, "POST": bayesCacheEmpty} {
		if rec := w.callAs(tok, name, `{}`, true, h); rec.Code != http.StatusForbidden || !strings.Contains(rec.Body.String(), "API token") {
			t.Errorf("a token on %s: %d %s", name, rec.Code, rec.Body)
		}
	}
	if len(w.audits()) != 0 {
		t.Error("a refused action was audited")
	}
	if a := w.get(w.observer); a.Editable {
		t.Error("the observer's card says it can be edited")
	}
	if a := w.get(w.admin); !a.Editable {
		t.Error("the administrator's card says it cannot be edited")
	}
}

func TestTheBayesCardShowsEveryDefault(t *testing.T) {
	w := newCardWorld(t)
	a := w.get(w.admin)
	want := bayesCardValues{CompileCPU: "1", CompileMemory: "3Gi", CompileSeconds: 300, CompileTmp: "512Mi", CompileSlots: 2, CacheMax: "2Gi",
		SamplingTmp: "2Gi"}
	if a.Defaults != want || a.Limits != want {
		t.Fatalf("defaults %+v, in force %+v", a.Defaults, a.Limits)
	}
	if a.Cache.Binaries != 0 || a.Cache.UsedBytes != 0 || a.Cache.OldestUse != nil || a.Cache.OlderImage.Binaries != 0 || len(a.Compiles) != 0 {
		t.Errorf("an empty cache: %+v %+v", a.Cache, a.Compiles)
	}
}

func TestTheBayesCardSavesLimitsAndTheLoopReadsThem(t *testing.T) {
	w := newCardWorld(t)
	put := func(body string) *httptest.ResponseRecorder { return w.call(w.admin, "PUT", body, true, bayesCardPut) }
	rec := put(`{"compile_cpu":"2","compile_memory":"4Gi","compile_seconds":600,"compile_tmp":"1Gi","compile_slots":3,"cache_max":"512Mi","sampling_tmp":"4Gi"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	a := decodeCard(t, rec)
	want := bayesCardValues{CompileCPU: "2", CompileMemory: "4Gi", CompileSeconds: 600, CompileTmp: "1Gi", CompileSlots: 3, CacheMax: "512Mi", SamplingTmp: "4Gi"}
	if a.Limits != want || a.Defaults.CompileMemory != "3Gi" {
		t.Fatalf("after the save: %+v", a)
	}
	if s, _ := w.stored(); s != "2|4Gi|600|1Gi|3|536870912" {
		t.Errorf("stored: %s", s)
	}
	// what the loop and the compiler read, with the same limits
	g, _ := modelgroups.Get("bayes")
	st, err := bcSettings(w.ctx, w.db)
	if err != nil {
		t.Fatal(err)
	}
	l, err := bayescompile.Resolve(g.Recipe.Compile, g.Recipe.Cache, st)
	if err != nil || l.CPU != "2" || l.MemoryBytes != 4<<30 || l.DeadlineSeconds != 600 || l.TmpBytes != 1<<30 || l.Slots != 3 || l.CacheMaxBytes != 512<<20 {
		t.Fatalf("the loop's limits: %+v %v", l, err)
	}
	// the audit holds the limits before and after
	au := w.audits()
	if len(au) != 1 || !strings.HasPrefix(au[0], "models.bayes_compile_settings") || !strings.Contains(au[0], `"compile_memory":"3Gi"`) ||
		!strings.Contains(au[0], `"compile_memory":"4Gi"`) {
		t.Fatalf("the audit: %v", au)
	}
	// the same limits again change nothing and write nothing
	put(`{"compile_cpu":"2","compile_memory":"4Gi","compile_seconds":600,"compile_tmp":"1Gi","compile_slots":3,"cache_max":"512Mi","sampling_tmp":"4Gi"}`)
	if len(w.audits()) != 1 {
		t.Errorf("an unchanged save was audited: %v", w.audits())
	}
	// a value equal to the default is the default (it follows the recipe); a field left out is the default
	put(`{"compile_cpu":"1","compile_memory":"3Gi","compile_seconds":300,"compile_tmp":"512Mi","compile_slots":2,"cache_max":"2Gi","sampling_tmp":"2Gi"}`)
	if s, _ := w.stored(); s != "-|-|-|-|-|-" {
		t.Errorf("defaults stored as values: %s", s)
	}
	if a := w.get(w.observer); a.Limits != a.Defaults {
		t.Errorf("after the reset: %+v", a.Limits)
	}
	put(`{"compile_slots":4}`)
	if a := w.get(w.admin); a.Limits.CompileSlots != 4 || a.Limits.CompileMemory != "3Gi" {
		t.Errorf("a field left out: %+v", a.Limits)
	}
	if n := len(w.audits()); n != 3 {
		t.Errorf("audits after three changes: %v", w.audits())
	}
}

func TestTheBayesCardRefusesWhatIsOutOfRange(t *testing.T) {
	w := newCardWorld(t)
	for _, c := range []struct{ body, says string }{
		{`{"compile_memory":"1Gi"}`, "1.3 to 1.9 GiB"},
		{`{"compile_memory":"2047Mi"}`, "between 2 GiB"},
		{`{"compile_memory":"65Gi"}`, "between 2 GiB"},
		{`{"compile_memory":"3G"}`, "whole number of Mi or Gi"},
		{`{"compile_memory":"3.5Gi"}`, "whole number of Mi or Gi"},
		{`{"compile_seconds":59}`, "11 to 45 s"},
		{`{"compile_seconds":3601}`, "between 60 and 3600"},
		{`{"compile_seconds":-1}`, "between 60 and 3600"},
		{`{"compile_cpu":"0"}`, "whole number of cores"},
		{`{"compile_cpu":"1.5"}`, "whole number of cores"},
		{`{"compile_cpu":"17"}`, "between 1 and 16"},
		{`{"compile_tmp":"10Mi"}`, "between 64 MiB"},
		{`{"compile_slots":0}`, "between 1 and 16"},
		{`{"compile_slots":17}`, "between 1 and 16"},
		{`{"cache_max":"1Mi"}`, "between 64 MiB"},
		{`{"cache_max":"2000Gi"}`, "between 64 MiB"},
		{`{"sampling_tmp":"100Mi"}`, "between 256 MiB"},
		{`{"compile_seconds":"slow"}`, "not valid JSON"},
	} {
		rec := w.call(w.admin, "PUT", c.body, true, bayesCardPut)
		if rec.Code != http.StatusUnprocessableEntity || !strings.Contains(rec.Body.String(), c.says) {
			t.Errorf("%s: %d %s (wanted %q)", c.body, rec.Code, rec.Body, c.says)
		}
	}
	if s, _ := w.stored(); s != "no row" || len(w.audits()) != 0 {
		t.Errorf("a refused save changed something: %s %v", s, w.audits())
	}
	// the edges are accepted
	if rec := w.call(w.admin, "PUT", `{"compile_memory":"2Gi","compile_seconds":60,"compile_slots":16,"compile_cpu":"16","cache_max":"64Mi"}`, true, bayesCardPut); rec.Code != http.StatusOK {
		t.Errorf("the edges: %d %s", rec.Code, rec.Body)
	}
}

func TestTheBayesCardShowsTheCacheAndTheLastCompiles(t *testing.T) {
	w := newCardWorld(t)
	now := time.Now().UTC().Truncate(time.Second)
	older := "sha256:" + strings.Repeat("0", 64)
	oldest := now.Add(-48 * time.Hour)
	w.seed("a", w.digest, []byte(strings.Repeat("a", 1000)), now.Add(-time.Hour), w.model)
	w.seed("b", w.digest, []byte(strings.Repeat("b", 2000)), now.Add(-2*time.Hour), w.model)
	w.seed("c", older, []byte(strings.Repeat("c", 500)), oldest, 0)
	// a compile that passed its deadline, with the limits it ran under
	if _, err := w.db.Exec(w.ctx, `INSERT INTO bayes_binaries (cache_key, program_sha256, cmdstan, image_digest, arch, group_version, state, reason,
		detail, requested_by, model_id, version, deadline_seconds, memory_bytes, queued_at, finished_at)
		VALUES ($1, $1, '2.40.0', $2, 'amd64', 2, 'timeout', 'timeout', 'Took longer than 300 s.', $3, $4, 1, 300, 3221225472, $5, $5)`,
		strings.Repeat("t", 64), w.digest, w.admin.ID, w.model, now); err != nil {
		t.Fatal(err)
	}
	a := w.get(w.admin)
	if a.Cache.Binaries != 3 || a.Cache.UsedBytes != 3500 || a.Cache.OlderImage.Binaries != 1 || a.Cache.OlderImage.Bytes != 500 ||
		a.Cache.OldestUse == nil || !strings.HasPrefix(*a.Cache.OldestUse, oldest.Format("2006-01-02T15:04:05")) {
		t.Fatalf("the cache: %+v", a.Cache)
	}
	if len(a.Compiles) != 4 || a.Compiles[0].State != "timeout" || a.Compiles[0].DeadlineSeconds == nil || *a.Compiles[0].DeadlineSeconds != 300 ||
		a.Compiles[0].MemoryBytes == nil || *a.Compiles[0].MemoryBytes != 3<<30 || a.Compiles[0].ModelName == nil || *a.Compiles[0].ModelName != "Slopes" {
		t.Fatalf("the compiles: %+v", a.Compiles)
	}
	if a.Compiles[1].BinaryBytes == nil || *a.Compiles[1].BinaryBytes != 1000 || a.Compiles[3].ModelName != nil {
		t.Errorf("a ready row and one with no model: %+v", a.Compiles)
	}
	// a person who does not see the model reads no name; an observer with no folder grant is one
	if o := w.get(w.observer); len(o.Compiles) != 4 || o.Compiles[0].ModelName != nil || o.Compiles[0].ModelID != nil || o.Compiles[0].DeadlineSeconds == nil {
		t.Errorf("an observer who does not see the folder: %+v", o.Compiles)
	}
	// only the last twenty
	for i := 0; i < 25; i++ {
		w.seed(fmt.Sprintf("%c", 'd'+i%20)+fmt.Sprint(i), w.digest, []byte(fmt.Sprintf("binary %d", i)), now, 0)
	}
	if n := len(w.get(w.admin).Compiles); n != bcardCompilesShown {
		t.Errorf("compiles shown: %d", n)
	}
}

func TestEmptyingTheBayesCacheKeepsWhatARunIsPlacing(t *testing.T) {
	w := newCardWorld(t)
	now := time.Now().UTC()
	same := []byte(strings.Repeat("s", 900))
	r1 := w.seed("a", w.digest, same, now.Add(-time.Hour), w.model)
	r2 := w.seed("b", w.digest, []byte(strings.Repeat("t", 800)), now.Add(-2*time.Hour), w.model)
	r3 := w.seed("c", "sha256:"+strings.Repeat("1", 64), same, now.Add(-3*time.Hour), 0) // another image, the same bytes
	queued := w.queue(w.admin.ID, w.model, 1, w.prog, now)
	// a run is placing r3: its row is held FOR SHARE
	hold, err := w.db.Begin(w.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := hold.Exec(w.ctx, `SELECT id FROM bayes_binaries WHERE id = $1 FOR SHARE`, r3); err != nil {
		t.Fatal(err)
	}
	rec := w.call(w.admin, "POST", "", true, bayesCacheEmpty)
	_ = hold.Rollback(w.ctx)
	if rec.Code != http.StatusOK {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	a := decodeCard(t, rec)
	if a.Emptied == nil || a.Emptied.Binaries != 2 || a.Emptied.Bytes != 1700 || a.Emptied.Kept != 1 {
		t.Fatalf("emptied: %+v", a.Emptied)
	}
	if !w.gone(r1) || !w.gone(r2) || w.gone(r3) || w.gone(queued) {
		t.Errorf("rows: r1 gone %v, r2 gone %v, r3 gone %v, queued gone %v", w.gone(r1), w.gone(r2), w.gone(r3), w.gone(queued))
	}
	w.engine.mu.Lock()
	_, heldObject := w.engine.storage["bayes-binaries/"+sha(same)]
	_, otherObject := w.engine.storage["bayes-binaries/"+sha([]byte(strings.Repeat("t", 800)))]
	w.engine.mu.Unlock()
	if !heldObject || otherObject {
		t.Errorf("objects: the one a held row still has stays (%v), the other goes (%v gone=%v)", heldObject, otherObject, !otherObject)
	}
	if a.Cache.Binaries != 1 || a.Cache.UsedBytes != 900 {
		t.Errorf("the cache after: %+v", a.Cache)
	}
	au := w.audits()
	if len(au) != 1 || !strings.HasPrefix(au[0], "models.bayes_cache_empty") || !strings.Contains(au[0], `"binaries":2`) ||
		!strings.Contains(au[0], `"bytes":1700`) || !strings.Contains(au[0], `"kept":1`) {
		t.Errorf("the audit: %v", au)
	}
	// once nothing holds it, the second call leaves nothing, no engine call needed
	rec = w.call(w.admin, "POST", "", true, bayesCacheEmpty)
	if a := decodeCard(t, rec); rec.Code != http.StatusOK || a.Cache.Binaries != 0 || a.Emptied.Binaries != 1 {
		t.Fatalf("the second: %d %+v", rec.Code, a.Emptied)
	}
	// a cache already empty is still an answer
	if rec := w.call(w.admin, "POST", "", true, bayesCacheEmpty); rec.Code != http.StatusOK || decodeCard(t, rec).Emptied.Binaries != 0 {
		t.Errorf("an empty cache: %d %s", rec.Code, rec.Body)
	}
}

// the engine that does not answer leaves every row in place: nothing is deleted, the administrator is told
func TestEmptyingTheBayesCacheWhenTheStorageDoesNotAnswerChangesNothing(t *testing.T) {
	w := newCardWorld(t)
	id := w.seed("a", w.digest, []byte("some binary"), time.Now().UTC(), w.model)
	down := httptest.NewServer(http.HandlerFunc(func(rw http.ResponseWriter, r *http.Request) {
		rw.WriteHeader(http.StatusBadGateway)
		_, _ = rw.Write([]byte(`{"detail":"down"}`))
	}))
	defer down.Close()
	e, err := engine.New(down.URL, modelsTestFernetKey)
	if err != nil {
		t.Fatal(err)
	}
	d := *w.c.d
	d.Engine = e
	rec := httptest.NewRecorder()
	mgGuarded(&d, true, bayesCacheEmpty)(rec, httptest.NewRequest("POST", "/x", nil), &auth.Principal{User: *w.admin})
	if rec.Code != http.StatusBadGateway || w.gone(id) || len(w.audits()) != 0 {
		t.Fatalf("a storage that is down: %d %s, row gone %v, audits %v", rec.Code, rec.Body, w.gone(id), w.audits())
	}
}

// «Empty the cache» waits for the loop's round rather than skipping it, and leaves queued and timeout rows alone
func TestEmptyingTheBayesCacheWaitsForTheLoopAndLeavesOtherRows(t *testing.T) {
	w := newCardWorld(t)
	now := time.Now().UTC()
	id := w.seed("a", w.digest, []byte("a binary"), now, w.model)
	queued := w.queue(w.admin.ID, w.model, 1, w.prog, now)
	loop, err := w.db.Begin(w.ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer loop.Rollback(context.Background())
	if _, err := loop.Exec(w.ctx, `SELECT pg_advisory_xact_lock($1)`, bayescompile.LoopLock); err != nil {
		t.Fatal(err)
	}
	done := make(chan *httptest.ResponseRecorder, 1)
	go func() { done <- w.call(w.admin, "POST", "", true, bayesCacheEmpty) }()
	select {
	case <-done:
		t.Fatal("«Empty the cache» did not wait for the loop's lock")
	case <-time.After(400 * time.Millisecond):
	}
	if w.gone(id) {
		t.Fatal("a row went while the loop held its lock")
	}
	_ = loop.Rollback(w.ctx)
	select {
	case rec := <-done:
		if rec.Code != http.StatusOK || !w.gone(id) || w.gone(queued) {
			t.Fatalf("after the lock: %d %s", rec.Code, rec.Body)
		}
	case <-time.After(10 * time.Second):
		t.Fatal("«Empty the cache» never answered")
	}
}

// a limit raised or lowered touches no row: the next request of a timeout row queues it again by itself (MayRetry)
func TestChangingTheBayesLimitsTouchesNoRow(t *testing.T) {
	w := newCardWorld(t)
	id := w.seed("a", w.digest, []byte("a binary"), time.Now().UTC(), 0)
	before := w.row(id)
	if rec := w.call(w.admin, "PUT", `{"cache_max":"64Mi","compile_seconds":900}`, true, bayesCardPut); rec.Code != http.StatusOK {
		t.Fatalf("%d %s", rec.Code, rec.Body)
	}
	if after := w.row(id); after != before {
		t.Errorf("a row changed with the limits: %+v -> %+v", before, after)
	}
}
