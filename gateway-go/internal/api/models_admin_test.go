package api

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/clientip"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/modelgroups"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/perm"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptnodes"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/settings"
)

// The statistical models' groups in Administration (models_admin.go) and their build in the loop of
// the scripting environments (models_build.go), on a real database and a fake Kubernetes API.

// ── without a database ───────────────────────────────────────────────────────

func TestTheModelsRoutesAreRegisteredAndGoOnly(t *testing.T) {
	want := [][2]string{{"GET", "/models/groups"}, {"GET", "/admin/models"}, {"GET", "/admin/models/{group_id}"},
		{"POST", "/admin/models/{group_id}/enable"}, {"POST", "/admin/models/{group_id}/disable"},
		{"POST", "/admin/models/{group_id}/rebuild"}, {"PUT", "/admin/models/{group_id}/settings"}, {"PUT", "/admin/models/{group_id}/access"}}
	have := map[[2]string]bool{}
	for _, r := range Routes(&Deps{Config: &config.Config{}}) {
		have[[2]string{r.Method, r.Pattern}] = true
	}
	goOnly := map[[2]string]bool{}
	for _, r := range server.GoOnlyRoutes() {
		goOnly[r] = true
	}
	for _, r := range want {
		if !have[r] {
			t.Errorf("%v is not registered", r)
		}
		if !goOnly[r] {
			t.Errorf("%v is not declared among the Go-only routes", r)
		}
	}
}

func TestAManagedEnvironmentIsNamedByItsGroup(t *testing.T) {
	m := mgManagedBy("regression")
	if m != "model_group:regression" || mgGroupOfManaged(&m) != "regression" || mgGroupOfManaged(nil) != "" {
		t.Error(m)
	}
	plain := "something"
	if mgGroupOfManaged(&plain) != "" {
		t.Error("an environment not managed by a group")
	}
}

func TestTheRequestOfAResourceIsHalfItsLimit(t *testing.T) {
	for _, c := range []struct {
		in  string
		cpu bool
		out string
	}{{"2", true, "1000m"}, {"500m", true, "250m"}, {"1", true, "500m"}, {"4.35", true, "2175m"}, {"60m", true, "50m"},
		// never above the limit: a Pod whose request is above its limit is not created
		{"20m", true, "20m"},
		{"4Gi", false, "2048Mi"}, {"2Gi", false, "1024Mi"}, {"1G", false, "476Mi"}} {
		if got := mgHalf(c.in, c.cpu); got != c.out {
			t.Errorf("%s: %s, want %s", c.in, got, c.out)
		}
	}
}

func TestTheRecipesBecomeVersionsTheBuildLoopAccepts(t *testing.T) {
	for _, g := range modelgroups.Groups() {
		if _, err := sbParsePackages(g.Recipe.Language, strings.Join(g.Recipe.Packages, "\n")); err != nil {
			t.Errorf("%s: %v", g.ID, err)
		}
		if !scCPU.MatchString(g.Recipe.Resources.CPU) || !scMemory.MatchString(g.Recipe.Resources.Memory) {
			t.Errorf("%s: resources %v", g.ID, g.Recipe.Resources)
		}
		if why := mgBaseSentence(g); why != "" {
			t.Errorf("%s: %s", g.ID, why)
		}
	}
}

func TestTheStateOfAGroup(t *testing.T) {
	g := modelgroups.Groups()[0]
	cur, other, job := int64(7), "old", "tabularia-recheck-e1-v7"
	version := func(id int64, status string, hash string) mgVersion {
		h := hash
		return mgVersion{ID: id, Number: id, Status: status, RecipeHash: &h}
	}
	// a version self-tested with an earlier runner, its recheck's Job, and the attempts that could not run
	rechecked := func(v mgVersion, job *string, attempts int64) mgVersion {
		old, shipped := "an earlier runner", g.RunnerHash
		v.RunnerHash, v.JobName, v.RecheckRunner, v.RecheckAttempts = &old, job, &shipped, attempts
		return v
	}
	cases := []struct {
		name  string
		state mgState
		want  string
		ready bool
	}{
		{"never enabled", mgState{}, "not_enabled", false},
		{"disabled with a ready version", mgState{CurrentID: &cur, Versions: []mgVersion{version(7, "ready", g.RecipeHash)}}, "not_enabled", true},
		{"enabled, nothing yet", mgState{Enabled: true}, "building", false},
		{"queued", mgState{Enabled: true, Versions: []mgVersion{version(1, "queued", g.RecipeHash)}}, "building", false},
		{"checking", mgState{Enabled: true, Versions: []mgVersion{version(1, "checking", g.RecipeHash)}}, "checking", false},
		{"ready", mgState{Enabled: true, CurrentID: &cur, Versions: []mgVersion{version(7, "ready", g.RecipeHash)}}, "ready", true},
		{"ready while the next is built", mgState{Enabled: true, CurrentID: &cur, Versions: []mgVersion{version(8, "building", g.RecipeHash), version(7, "ready", g.RecipeHash)}}, "building", true},
		{"the next failed, the version in use serves", mgState{Enabled: true, CurrentID: &cur, Versions: []mgVersion{version(8, "failed", g.RecipeHash), version(7, "ready", g.RecipeHash)}}, "ready", true},
		{"failed", mgState{Enabled: true, Versions: []mgVersion{version(1, "failed", g.RecipeHash)}}, "failed", false},
		{"the recipe changed", mgState{Enabled: true, CurrentID: &cur, Versions: []mgVersion{version(7, "ready", other)}}, "rebuild_needed", false},
		{"rechecked with a new runner", mgState{Enabled: true, CurrentID: &cur, Versions: []mgVersion{rechecked(version(7, "ready", g.RecipeHash), &job, 0)}},
			"rechecking", true},
		{"a recheck that could not run, tried again", mgState{Enabled: true, CurrentID: &cur,
			Versions: []mgVersion{rechecked(version(7, "ready", g.RecipeHash), nil, 2)}}, "recheck_retrying", true},
		{"no recheck could run", mgState{Enabled: true, CurrentID: &cur,
			Versions: []mgVersion{rechecked(version(7, "ready", g.RecipeHash), nil, scriptnodes.MaxRetries+1)}}, "ready", true},
	}
	for _, c := range cases {
		c.state.Group = g
		if got := c.state.state(); got != c.want {
			t.Errorf("%s: %s, want %s", c.name, got, c.want)
		}
		if got := c.state.current() != nil; got != c.ready {
			t.Errorf("%s: ready %v, want %v", c.name, got, c.ready)
		}
	}
}

func TestTheSelftestFailureNamesTheValuesThatDiffer(t *testing.T) {
	r := &modelgroups.SelftestReport{OK: 3, Failed: 1}
	r.Checks = append(r.Checks, struct {
		Name   string `json:"name"`
		Status string `json:"status"`
		Detail string `json:"detail"`
	}{"linear/coef", "failed", "got 1.5, expected 2"})
	got := mgSelftestFailure(r)
	if !strings.Contains(got, "1 of 4 checks") || !strings.Contains(got, "- linear/coef: got 1.5, expected 2") {
		t.Error(got)
	}
}

// ── a database and a Kubernetes API that is not one ───────────────────────────

// fakeKube is the part of the Kubernetes API the build loop calls: Jobs, their Pods and logs,
// ConfigMaps.
type fakeKube struct {
	mu         sync.Mutex
	srv        *httptest.Server
	jobs       map[string]map[string]any // "<namespace>/<name>"
	configmaps map[string]map[string]any
	ends       map[string]fakeEnd // by Job name: how it ends (absent: it runs)
	noLog      map[string]bool    // by Job name: its Pod's log is not given (503)
	refused    map[string]string  // by Job name: the cluster does not create its Pod, with this event's message
	deleted    []string
	selectors  []string // the label selectors of the Jobs deleted by a collection delete
}

type fakeEnd struct {
	ok   bool
	log  string
	exit int
}

func newFakeKube(t *testing.T) *fakeKube {
	t.Helper()
	k := &fakeKube{jobs: map[string]map[string]any{}, configmaps: map[string]map[string]any{}, ends: map[string]fakeEnd{},
		noLog: map[string]bool{}, refused: map[string]string{}}
	k.srv = httptest.NewServer(http.HandlerFunc(k.serve))
	t.Cleanup(k.srv.Close)
	token := filepath.Join(t.TempDir(), "token")
	if err := os.WriteFile(token, []byte("test-token"), 0o600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("KUBERNETES__API_URL", k.srv.URL)
	t.Setenv("KUBERNETES__TOKEN_FILE", token)
	return k
}

func (k *fakeKube) reply(w http.ResponseWriter, status int, body any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(body)
}

func (k *fakeKube) serve(w http.ResponseWriter, r *http.Request) {
	k.mu.Lock()
	defer k.mu.Unlock()
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	notFound := func() { k.reply(w, 404, map[string]any{"message": "not found"}) }
	switch {
	// /apis/batch/v1/namespaces/{ns}/jobs[/{name}]
	case len(parts) >= 6 && parts[0] == "apis" && parts[1] == "batch" && parts[5] == "jobs":
		ns := parts[4]
		if len(parts) == 6 && r.Method == http.MethodPost {
			var job map[string]any
			_ = json.NewDecoder(r.Body).Decode(&job)
			meta := job["metadata"].(map[string]any)
			name := meta["name"].(string)
			if _, there := k.jobs[ns+"/"+name]; there {
				k.reply(w, 409, map[string]any{"message": "exists"})
				return
			}
			meta["uid"], meta["creationTimestamp"] = "uid-"+name, time.Now().UTC().Format(time.RFC3339)
			k.jobs[ns+"/"+name] = job
			k.reply(w, 201, job)
			return
		}
		if len(parts) == 6 && r.Method == http.MethodDelete {
			k.selectors = append(k.selectors, r.URL.Query().Get("labelSelector"))
			k.reply(w, 200, map[string]any{})
			return
		}
		if len(parts) == 7 {
			key := ns + "/" + parts[6]
			job, there := k.jobs[key]
			switch {
			case r.Method == http.MethodDelete:
				delete(k.jobs, key)
				k.deleted = append(k.deleted, parts[6])
				k.reply(w, 200, map[string]any{})
			case !there:
				notFound()
			default:
				status := map[string]any{"active": 1}
				if _, no := k.refused[parts[6]]; no {
					status = map[string]any{}
				}
				if end, ended := k.ends[parts[6]]; ended {
					cond := map[string]any{"type": "Failed", "status": "True", "reason": "BackoffLimitExceeded"}
					status = map[string]any{"failed": 1, "conditions": []any{cond}}
					if end.ok {
						cond = map[string]any{"type": "Complete", "status": "True"}
						status = map[string]any{"succeeded": 1, "conditions": []any{cond}}
					}
				}
				out := map[string]any{"metadata": job["metadata"], "spec": job["spec"], "status": status}
				k.reply(w, 200, out)
			}
			return
		}
	// /api/v1/namespaces/{ns}/pods[/{pod}/log]
	case len(parts) >= 5 && parts[0] == "api" && parts[4] == "pods":
		if len(parts) == 5 {
			sel := strings.TrimPrefix(r.URL.Query().Get("labelSelector"), "job-name=")
			end, ended := k.ends[sel]
			pod := map[string]any{"metadata": map[string]any{"name": "pod-" + sel, "creationTimestamp": "2026-11-20T10:00:00Z"}}
			if ended {
				pod["status"] = map[string]any{"containerStatuses": []any{map[string]any{"state": map[string]any{"terminated": map[string]any{"exitCode": end.exit}}}}}
			}
			k.reply(w, 200, map[string]any{"items": []any{pod}})
			return
		}
		if len(parts) == 7 && parts[6] == "log" {
			if k.noLog[strings.TrimPrefix(parts[5], "pod-")] {
				k.reply(w, 503, map[string]any{"message": "the kubelet does not answer"})
				return
			}
			_, _ = io.WriteString(w, k.ends[strings.TrimPrefix(parts[5], "pod-")].log)
			return
		}
	case len(parts) == 5 && parts[0] == "api" && parts[4] == "configmaps" && r.Method == http.MethodPost:
		var cm map[string]any
		_ = json.NewDecoder(r.Body).Decode(&cm)
		name := cm["metadata"].(map[string]any)["name"].(string)
		k.configmaps[parts[3]+"/"+name] = cm
		k.reply(w, 201, cm)
		return
	case len(parts) == 5 && parts[4] == "events":
		name := strings.TrimPrefix(r.URL.Query().Get("fieldSelector"), "involvedObject.name=")
		items := []any{}
		if msg, no := k.refused[name]; no {
			items = append(items, map[string]any{"reason": "FailedCreate", "message": msg})
		}
		k.reply(w, 200, map[string]any{"items": items})
		return
	}
	notFound()
}

func (k *fakeKube) end(job string, ok bool, exit int, log string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.ends[job] = fakeEnd{ok, log, exit}
}

// older makes a Job look created that many seconds ago.
func (k *fakeKube) older(ns, name string, seconds int) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.jobs[ns+"/"+name]["metadata"].(map[string]any)["creationTimestamp"] = time.Now().UTC().Add(-time.Duration(seconds) * time.Second).Format(time.RFC3339)
}

// recheckCodes are the code's ConfigMaps of the rechecks of the group's version 1 (each has a name
// of its own).
func (k *fakeKube) recheckCodes(env int64) []string {
	k.mu.Lock()
	defer k.mu.Unlock()
	var names []string
	for key := range k.configmaps {
		if strings.HasPrefix(key, fmt.Sprintf("scripts/tabularia-reselftest-e%d-v1-", env)) {
			names = append(names, key)
		}
	}
	return names
}

// hideLog makes the cluster not give the log of the Job's Pod.
func (k *fakeKube) hideLog(job string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.noLog[job] = true
}

// refuse makes the cluster not create the Job's Pod, and say why in an event.
func (k *fakeKube) refuse(job, message string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	k.refused[job] = message
}

// remove takes a Job away, as someone or its TTL would.
func (k *fakeKube) remove(ns, name string) {
	k.mu.Lock()
	defer k.mu.Unlock()
	delete(k.jobs, ns+"/"+name)
}

func (k *fakeKube) job(ns, name string) map[string]any {
	k.mu.Lock()
	defer k.mu.Unlock()
	return k.jobs[ns+"/"+name]
}

type modelsFixture struct {
	t                    *testing.T
	ctx                  context.Context
	db                   *pgxpool.Pool
	d                    *Deps
	admin, observer, ann *auth.User
	kube                 *fakeKube
}

func newModelsFixture(t *testing.T) *modelsFixture {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set: run gateway-go/scripts/test.sh")
	}
	t.Cleanup(scriptcfg.Reload)
	t.Setenv("SECURITY__FERNET_KEY", "dGVzdGtleXRlc3RrZXl0ZXN0a2V5dGVzdGtleTEyMzQ=")
	t.Setenv("SCRIPTS__ENABLED", "true")
	t.Setenv("SCRIPTS__NAMESPACE", "scripts")
	t.Setenv("SCRIPTS__BUILD_NAMESPACE", "builds")
	t.Setenv("SCRIPTS__REGISTRY", "registry:5000/envs")
	f := &modelsFixture{t: t, ctx: context.Background()}
	f.kube = newFakeKube(t)
	scriptcfg.Reload()
	db, err := pgxpool.New(f.ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	f.db = db
	lock, err := db.Acquire(f.ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := lock.Exec(f.ctx, `SELECT pg_advisory_lock(7101)`); err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	f.d = &Deps{DB: db, Log: log, Config: &config.Config{ScriptsEnabled: true, ScriptsNamespace: "scripts", ScriptsRegistry: "registry:5000/envs"},
		Auth: &auth.Authenticator{DB: db, Secret: []byte("a test secret, not of anything"), Algorithm: "HS256",
			Settings: shareSettingsStub{}, ClientIP: clientip.New(nil), Log: log}}
	one := func(sql string, args ...any) int64 {
		t.Helper()
		var id int64
		if err := db.QueryRow(f.ctx, sql, args...).Scan(&id); err != nil {
			t.Fatalf("%s: %v", sql, err)
		}
		return id
	}
	stamp := time.Now().UnixNano()
	user := func(name string, super, observer bool) *auth.User {
		email := fmt.Sprintf("mg-%s-%d@x.test", name, stamp)
		id := one(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, $2, 'x', true, $3, $4, false, now()) RETURNING id`, email, name, super, observer)
		return &auth.User{ID: id, Email: email, FullName: name, IsActive: true, IsSuperuser: super, IsObserver: observer}
	}
	f.admin, f.observer, f.ann = user("admin", true, false), user("observer", false, true), user("ann", false, false)
	f.clean()
	t.Cleanup(func() {
		f.clean()
		ids := []int64{f.admin.ID, f.observer.ID, f.ann.ID}
		_, _ = db.Exec(f.ctx, `DELETE FROM audit_logs WHERE actor_id = ANY($1)`, ids)
		_, _ = db.Exec(f.ctx, `DELETE FROM users WHERE id = ANY($1)`, ids)
		_, _ = lock.Exec(f.ctx, `SELECT pg_advisory_unlock(7101)`)
		lock.Release()
		db.Close()
	})
	return f
}

// clean takes the groups and their managed environments away.
func (f *modelsFixture) clean() {
	for _, sql := range []string{
		`DELETE FROM script_environment_groups WHERE environment_id IN (SELECT id FROM script_environments WHERE managed_by IS NOT NULL)`,
		`UPDATE script_environments SET current_version_id = NULL WHERE managed_by IS NOT NULL`,
		`DELETE FROM script_environment_versions WHERE environment_id IN (SELECT id FROM script_environments WHERE managed_by IS NOT NULL)`,
		`DELETE FROM model_groups`,
		`DELETE FROM script_environments WHERE managed_by IS NOT NULL`,
	} {
		if _, err := f.db.Exec(f.ctx, sql); err != nil {
			f.t.Fatalf("%s: %v", sql, err)
		}
	}
}

// call runs a route of the section as this person, through its guard.
func (f *modelsFixture) call(u *auth.User, method, group, action, body string, admin bool, h func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)) *httptest.ResponseRecorder {
	f.t.Helper()
	r := httptest.NewRequest(method, "/x", bytes.NewReader([]byte(body)))
	r.SetPathValue("group_id", group)
	w := httptest.NewRecorder()
	mgGuarded(f.d, admin, h)(w, r, &auth.Principal{User: *u})
	return w
}

func (f *modelsFixture) enable(u *auth.User, group string) *httptest.ResponseRecorder {
	return f.call(u, "POST", group, "enable", "", true, mgEnable)
}

type groupAnswer struct {
	DisabledReason *string    `json:"disabled_reason"`
	Editable       bool       `json:"editable"`
	Group          mgGroupOut `json:"group"`
	Groups         []mgGroupOut
}

func answerOf(t *testing.T, w *httptest.ResponseRecorder) groupAnswer {
	t.Helper()
	if w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body)
	}
	var a groupAnswer
	if err := json.Unmarshal(w.Body.Bytes(), &a); err != nil {
		t.Fatal(err)
	}
	return a
}

func (f *modelsFixture) audits() []string {
	f.t.Helper()
	rows, err := f.db.Query(f.ctx, `SELECT action || ' ' || coalesce(target_label, '') || ' ' || coalesce(detail, '') FROM audit_logs
		WHERE target_type = 'model_group' ORDER BY id`)
	if err != nil {
		f.t.Fatal(err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var s string
		if err := rows.Scan(&s); err != nil {
			f.t.Fatal(err)
		}
		out = append(out, s)
	}
	return out
}

func (f *modelsFixture) versions(group string) []mgVersion {
	f.t.Helper()
	g, _ := modelgroups.Get(group)
	s, err := mgLoad(f.ctx, f.db, g, true, 0)
	if err != nil {
		f.t.Fatal(err)
	}
	return s.Versions
}

func TestOnlyAnAdministratorChangesTheGroupsAndAnObserverReadsThem(t *testing.T) {
	f := newModelsFixture(t)
	for _, c := range []struct {
		name string
		do   func(u *auth.User) *httptest.ResponseRecorder
		read bool // an observer may
	}{
		{"list", func(u *auth.User) *httptest.ResponseRecorder { return f.call(u, "GET", "", "", "", false, mgList) }, true},
		{"detail", func(u *auth.User) *httptest.ResponseRecorder {
			return f.call(u, "GET", "regression", "", "", false, mgDetail)
		}, true},
		{"enable", func(u *auth.User) *httptest.ResponseRecorder { return f.enable(u, "regression") }, false},
		{"disable", func(u *auth.User) *httptest.ResponseRecorder {
			return f.call(u, "POST", "regression", "", "", true, mgDisable)
		}, false},
		{"rebuild", func(u *auth.User) *httptest.ResponseRecorder {
			return f.call(u, "POST", "regression", "", "", true, mgRebuild)
		}, false},
		{"settings", func(u *auth.User) *httptest.ResponseRecorder {
			return f.call(u, "PUT", "regression", "", `{"cpu_limit":"1","memory_limit":"1Gi","max_timeout_seconds":600}`, true, mgSettings)
		}, false},
		{"access", func(u *auth.User) *httptest.ResponseRecorder {
			return f.call(u, "PUT", "regression", "", `{"everyone":true}`, true, mgAccess)
		}, false},
	} {
		if w := c.do(f.ann); w.Code != http.StatusForbidden {
			t.Errorf("%s as a person who edits flows: %d", c.name, w.Code)
		}
		w := c.do(f.observer)
		if c.read && w.Code != http.StatusOK || !c.read && w.Code != http.StatusForbidden {
			t.Errorf("%s as an observer: %d %s", c.name, w.Code, w.Body)
		}
	}
	// an API token does not act, even an administrator's
	r := httptest.NewRequest("POST", "/x", nil)
	r.SetPathValue("group_id", "regression")
	w := httptest.NewRecorder()
	mgGuarded(f.d, true, mgEnable)(w, r, &auth.Principal{User: *f.admin, TokenName: "ci"})
	if w.Code != http.StatusForbidden || !strings.Contains(w.Body.String(), "API token") {
		t.Errorf("a token: %d %s", w.Code, w.Body)
	}
	if len(f.audits()) != 0 || len(f.versions("regression")) != 0 {
		t.Error("a refused action changed something")
	}
	// an unknown group
	if w := f.enable(f.admin, "nope"); w.Code != http.StatusNotFound {
		t.Errorf("unknown group: %d", w.Code)
	}
	// the observer's answer says it cannot edit
	if a := answerOf(t, f.call(f.observer, "GET", "", "", "", false, mgList)); a.Editable || len(a.Groups) != len(modelgroups.Groups()) {
		t.Errorf("the observer's list: %+v", a)
	}
}

func TestEnablingAGroupQueuesItsVersionAndHidesItsEnvironment(t *testing.T) {
	f := newModelsFixture(t)
	a := answerOf(t, f.call(f.admin, "GET", "", "", "", false, mgList))
	if !a.Editable || a.DisabledReason != nil {
		t.Fatalf("the admin's list: %+v", a)
	}
	for _, g := range a.Groups {
		if g.Enabled || g.State != "not_enabled" || g.Ready || len(g.Versions) != 0 || g.Limits.MaxRows == 0 || g.Limits.CPULimit == "" {
			t.Errorf("a group never enabled: %+v", g)
		}
	}
	got := answerOf(t, f.enable(f.admin, "regression")).Group
	if !got.Enabled || got.State != "building" || got.Ready || len(got.Versions) != 1 || got.Versions[0].Status != "queued" || got.Versions[0].Number != 1 ||
		got.Versions[0].Stale {
		t.Fatalf("enabled: %+v", got)
	}
	var managed *string
	var internet bool
	var hash, runner *string
	if err := f.db.QueryRow(f.ctx, `SELECT e.managed_by, v.internet_access, v.recipe_hash, v.runner_hash FROM script_environments e
		JOIN script_environment_versions v ON v.environment_id = e.id WHERE e.managed_by = 'model_group:regression'`).Scan(&managed, &internet, &hash, &runner); err != nil {
		t.Fatal(err)
	}
	g, _ := modelgroups.Get("regression")
	if internet || hash == nil || *hash != g.RecipeHash || runner == nil || *runner != g.RunnerHash {
		t.Errorf("the version: internet %v, hashes %v %v", internet, hash, runner)
	}
	// again: no second version
	if again := answerOf(t, f.enable(f.admin, "regression")).Group; len(again.Versions) != 1 {
		t.Errorf("enabled twice: %d versions", len(again.Versions))
	}
	// the environment is in no list of the scripting: not the section, not the editor, not by its id
	r := httptest.NewRequest("GET", "/admin/scripting", nil)
	w := httptest.NewRecorder()
	readScripting(f.d, w, r, &auth.Principal{User: *f.admin})
	if w.Code != 200 || strings.Contains(w.Body.String(), "Statistical models") {
		t.Errorf("the Scripting section lists it: %d", w.Code)
	}
	w = httptest.NewRecorder()
	editorEnvironments(f.d, w, httptest.NewRequest("GET", "/scripting", nil), &auth.Principal{User: *f.admin})
	if w.Code != 200 || strings.Contains(w.Body.String(), "Statistical models") {
		t.Errorf("the editor lists it: %d", w.Code)
	}
	var id int64
	_ = f.db.QueryRow(f.ctx, `SELECT id FROM script_environments WHERE managed_by = 'model_group:regression'`).Scan(&id)
	for name, h := range map[string]func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal){"read": readEnvironment} {
		r := httptest.NewRequest("GET", "/x", nil)
		r.SetPathValue("environment_id", fmt.Sprint(id))
		w := httptest.NewRecorder()
		h(f.d, w, r, &auth.Principal{User: *f.admin})
		if w.Code != http.StatusNotFound {
			t.Errorf("%s by its id: %d", name, w.Code)
		}
	}
	for _, rb := range []func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal){rebuildEnvironmentRoute, deleteEnvironmentRoute} {
		r := httptest.NewRequest("POST", "/x", nil)
		r.SetPathValue("environment_id", fmt.Sprint(id))
		w := httptest.NewRecorder()
		rb(f.d, w, r, &auth.Principal{User: *f.admin})
		if w.Code != http.StatusNotFound {
			t.Errorf("a change by its id: %d", w.Code)
		}
	}
	if au := f.audits(); len(au) != 2 || !strings.HasPrefix(au[0], "models.group_enable regression ") {
		t.Errorf("the audit: %v", au)
	}
}

// Two enables of a group never enabled, at the same time: the second waits for the first and finds
// its environment, and both answer.
func TestTwoEnablesAtOnceBothAnswer(t *testing.T) {
	f := newModelsFixture(t)
	g, _ := modelgroups.Get("timeseries")
	// the first holds its transaction once it has made the environment
	tx, err := f.db.Begin(f.ctx)
	if err != nil {
		t.Fatal(err)
	}
	defer tx.Rollback(f.ctx) //nolint:errcheck
	first, err := mgEnsureEnvironment(f.ctx, tx, g, f.admin.Email)
	if err != nil {
		t.Fatal(err)
	}
	done := make(chan *httptest.ResponseRecorder)
	go func() { done <- f.enable(f.admin, "timeseries") }()
	for deadline := time.Now().Add(10 * time.Second); ; time.Sleep(20 * time.Millisecond) {
		var waiting int
		if err := f.db.QueryRow(f.ctx, `SELECT count(*) FROM pg_locks WHERE NOT granted`).Scan(&waiting); err != nil {
			t.Fatal(err)
		}
		if waiting > 0 {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("the second enable does not wait for the first")
		}
	}
	if err := tx.Commit(f.ctx); err != nil {
		t.Fatal(err)
	}
	if got := answerOf(t, <-done).Group; !got.Enabled || len(got.Versions) != 1 || f.envOf("timeseries") != first {
		t.Errorf("the second enable: %+v (environment %d, the first made %d)", got, f.envOf("timeseries"), first)
	}
	// a double click, with two real requests
	for i := 0; i < 5; i++ {
		f.clean()
		codes := make([]int, 2)
		var wg sync.WaitGroup
		start := make(chan struct{})
		for j := range codes {
			wg.Add(1)
			go func() {
				defer wg.Done()
				<-start
				codes[j] = f.enable(f.admin, "timeseries").Code
			}()
		}
		close(start)
		wg.Wait()
		if codes[0] != http.StatusOK || codes[1] != http.StatusOK || len(f.versions("timeseries")) != 1 {
			t.Fatalf("double click %d: %v, %d versions", i, codes, len(f.versions("timeseries")))
		}
	}
}

// An ordinary environment that already has the name the group's environment takes does not stop
// enabling: the managed one, shown nowhere, takes the next free name.
func TestAnEnvironmentOfTheGroupsNameDoesNotStopEnabling(t *testing.T) {
	f := newModelsFixture(t)
	var ids []int64
	for _, name := range []string{"Statistical models: timeseries", "statistical models: timeseries (2)"} {
		var id int64
		if err := f.db.QueryRow(f.ctx, `INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
			memory_limit, max_timeout_seconds, created_at, created_by, updated_at, updated_by) VALUES ($1, '', 'r', true,
			'500m', '1', '512Mi', '1Gi', 3600, now(), 'x', now(), 'x') RETURNING id`, name).Scan(&id); err != nil {
			t.Fatal(err)
		}
		ids = append(ids, id)
	}
	defer f.db.Exec(f.ctx, `DELETE FROM script_environments WHERE id = ANY($1)`, ids) //nolint:errcheck
	if got := answerOf(t, f.enable(f.admin, "timeseries")).Group; !got.Enabled || len(got.Versions) != 1 {
		t.Fatalf("enabled: %+v", got)
	}
	var name string
	if err := f.db.QueryRow(f.ctx, `SELECT name FROM script_environments WHERE managed_by = 'model_group:timeseries'`).Scan(&name); err != nil ||
		name != "Statistical models: timeseries (3)" {
		t.Errorf("the managed environment's name: %q %v", name, err)
	}
	// the administrator's environment is untouched, and enabling again finds the managed one
	if got := answerOf(t, f.enable(f.admin, "timeseries")).Group; len(got.Versions) != 1 {
		t.Errorf("enabled again: %+v", got)
	}
}

// Whoever may use a model group may not run code of their own in its image: the managed environment
// is no Script node's environment, nor a saved script's, for a person in the group nor for an
// administrator, and the access check of a run does not know it.
func TestTheManagedEnvironmentRunsNoScript(t *testing.T) {
	f := newModelsFixture(t)
	f.enable(f.admin, "regression")
	f.buildTo(t, "regression", true)
	var gid int64
	if err := f.db.QueryRow(f.ctx, `INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ($1, '', false, false, now()) RETURNING id`,
		fmt.Sprintf("mg-%d", time.Now().UnixNano())).Scan(&gid); err != nil {
		t.Fatal(err)
	}
	defer f.db.Exec(f.ctx, `DELETE FROM groups WHERE id = $1`, gid) //nolint:errcheck
	if w := f.call(f.admin, "PUT", "regression", "", fmt.Sprintf(`{"everyone":false,"group_ids":[%d]}`, gid), true, mgAccess); w.Code != http.StatusOK {
		t.Fatalf("access: %d %s", w.Code, w.Body)
	}
	if _, err := f.db.Exec(f.ctx, `INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, f.ann.ID, gid); err != nil {
		t.Fatal(err)
	}
	defer f.db.Exec(f.ctx, `DELETE FROM user_groups WHERE group_id = $1`, gid) //nolint:errcheck
	if usable, err := ModelGroupUsable(f.ctx, f.db, perm.For(f.db, f.ann), f.ann.ID, "regression"); err != nil || !usable {
		t.Fatalf("ann may use the group: %v %v", usable, err)
	}
	env := f.envOf("regression")
	if e, err := loadScriptEnv(f.ctx, f.db, env); err != nil || e != nil {
		t.Errorf("the managed environment is a scripting environment: %+v %v", e, err)
	}
	sc := scriptcfg.Load(config.FromOS())
	for _, u := range []*auth.User{f.ann, f.admin} {
		e, v, err := f.d.environmentOf(f.ctx, sc, u, env, "choose an environment")
		var refused *scriptFailed
		if !errors.As(err, &refused) || refused.msg != "choose an environment" || e != nil || v != nil {
			t.Errorf("%s's Script node in the managed environment: %+v %+v %v", u.FullName, e, v, err)
		}
	}
}

func rebuildEnvironmentRoute(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	rebuildEnvironment(d, w, r, p, pyd.Body{})
}

func deleteEnvironmentRoute(d *Deps, w http.ResponseWriter, r *http.Request, p *auth.Principal) {
	deleteEnvironment(d, w, r, p, pyd.Body{})
}

func TestDisablingAndEnablingAgainKeepsTheVersionInUse(t *testing.T) {
	f := newModelsFixture(t)
	f.enable(f.admin, "survival")
	f.buildTo(t, "survival", true)
	if img, err := ReadyModelGroupImage(f.ctx, f.db, "survival"); err != nil || img.Version != 1 {
		t.Fatalf("ready: %v %v", img, err)
	}
	w := f.call(f.admin, "POST", "survival", "", "", true, mgDisable)
	if g := answerOf(t, w).Group; g.Enabled || g.State != "not_enabled" || !g.Ready || g.CurrentVersion == nil {
		t.Errorf("disabled: %+v", g)
	}
	if _, err := ReadyModelGroupImage(f.ctx, f.db, "survival"); err == nil || err.(*ModelGroupUnavailable).Reason != "disabled" ||
		err.Error() != "the model group survival is disabled" {
		t.Errorf("a disabled group runs: %v", err)
	}
	if w := f.call(f.admin, "POST", "survival", "", "", true, mgDisable); w.Code != http.StatusConflict {
		t.Errorf("disabled twice: %d", w.Code)
	}
	// enabling again uses the version in use at once: nothing queued
	g := answerOf(t, f.enable(f.admin, "survival")).Group
	if !g.Enabled || g.State != "ready" || len(g.Versions) != 1 {
		t.Errorf("enabled again: %+v", g)
	}
	if _, err := ReadyModelGroupImage(f.ctx, f.db, "survival"); err != nil {
		t.Error(err)
	}
	actions := f.audits()
	if len(actions) != 3 || !strings.HasPrefix(actions[1], "models.group_disable survival") {
		t.Errorf("the audit: %v", actions)
	}
}

func TestRebuildingWaitsForTheVersionBeingBuilt(t *testing.T) {
	f := newModelsFixture(t)
	if w := f.call(f.admin, "POST", "regression", "", "", true, mgRebuild); w.Code != http.StatusConflict {
		t.Errorf("a group not enabled: %d", w.Code)
	}
	f.enable(f.admin, "regression")
	if w := f.call(f.admin, "POST", "regression", "", "", true, mgRebuild); w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "Version 1 is still being built") {
		t.Errorf("a version being built: %d %s", w.Code, w.Body)
	}
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET status = 'failed'`); err != nil {
		t.Fatal(err)
	}
	g := answerOf(t, f.call(f.admin, "POST", "regression", "", "", true, mgRebuild)).Group
	if len(g.Versions) != 2 || g.Versions[0].Number != 2 || g.Versions[0].Status != "queued" {
		t.Errorf("rebuilt: %+v", g.Versions)
	}
	if au := f.audits(); len(au) != 2 || !strings.HasPrefix(au[1], "models.group_rebuild regression ") {
		t.Errorf("the audit: %v", au)
	}
}

// The version in use serves however many newer versions failed since: the list and the palette keep
// fewer versions than the detail, and all three say the same.
func TestManyFailedRebuildsLeaveTheVersionInUseServing(t *testing.T) {
	f := newModelsFixture(t)
	f.grantEdit(t, f.admin)
	f.bucket(t)
	f.enable(f.admin, "regression")
	f.buildTo(t, "regression", true)
	for i := 1; i <= 6; i++ { // more than the list keeps (5)
		if w := f.call(f.admin, "POST", "regression", "", "", true, mgRebuild); w.Code != http.StatusOK {
			t.Fatalf("rebuild %d: %d %s", i, w.Code, w.Body)
		}
		f.buildTo(t, "regression", false)
	}
	img, err := ReadyModelGroupImage(f.ctx, f.db, "regression")
	if err != nil || img.Version != 1 {
		t.Fatalf("the version in use stopped serving: %v %v", img, err)
	}
	detail := answerOf(t, f.call(f.admin, "GET", "regression", "", "", false, mgDetail)).Group
	var listed mgGroupOut
	for _, g := range answerOf(t, f.call(f.admin, "GET", "", "", "", false, mgList)).Groups {
		if g.ID == "regression" {
			listed = g
		}
	}
	for name, g := range map[string]mgGroupOut{"detail": detail, "list": listed} {
		if g.State != "ready" || !g.Ready || g.CurrentVersion == nil || *g.CurrentVersion != 1 {
			t.Errorf("%s: state %s, ready %v, current %v", name, g.State, g.Ready, g.CurrentVersion)
		}
		if last := g.Versions[len(g.Versions)-1]; last.Number != 1 || !last.Current {
			t.Errorf("%s: the version in use is not among the versions: %+v", name, last)
		}
	}
	if len(listed.Versions) != 6 || listed.Versions[0].Number != 7 || listed.Versions[4].Number != 3 {
		t.Errorf("the list: the five newest and the one in use, newest first: %+v", listed.Versions)
	}
	w := httptest.NewRecorder()
	mgPalette(f.d, w, httptest.NewRequest("GET", "/models/groups", nil), &auth.Principal{User: *f.admin})
	var palette struct {
		Groups []struct {
			ID    string `json:"id"`
			Ready bool   `json:"ready"`
		} `json:"groups"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &palette); err != nil || w.Code != http.StatusOK {
		t.Fatalf("palette: %d %s", w.Code, w.Body)
	}
	for _, g := range palette.Groups {
		if g.ID == "regression" && !g.Ready {
			t.Error("the palette says the group is not ready")
		}
	}
}

func TestTheGroupsResourcesLimitsAndAccess(t *testing.T) {
	f := newModelsFixture(t)
	settings := func(body string) *httptest.ResponseRecorder {
		return f.call(f.admin, "PUT", "regression", "", body, true, mgSettings)
	}
	if w := settings(`{"cpu_limit":"2","memory_limit":"4Gi","max_timeout_seconds":600}`); w.Code != http.StatusConflict {
		t.Errorf("before it is enabled: %d", w.Code)
	}
	f.enable(f.admin, "regression")
	for _, bad := range []string{`{"cpu_limit":"lots","memory_limit":"4Gi","max_timeout_seconds":600}`,
		`{"cpu_limit":"2","memory_limit":"10 bananas","max_timeout_seconds":600}`, `{"cpu_limit":"2","memory_limit":"64Mi","max_timeout_seconds":600}`,
		`{"cpu_limit":"2","memory_limit":"4Gi","max_timeout_seconds":5}`, `{"cpu_limit":"2","memory_limit":"4Gi","max_timeout_seconds":600,"max_rows":0}`,
		`{"cpu_limit":"2","memory_limit":"4Gi","max_timeout_seconds":600,"max_rows":99999999999}`, `not json`} {
		if w := settings(bad); w.Code != http.StatusUnprocessableEntity {
			t.Errorf("%s: %d", bad, w.Code)
		}
	}
	// a CPU limit below the least request a model's Job makes: refused with its sentence
	if w := settings(`{"cpu_limit":"20m","memory_limit":"4Gi","max_timeout_seconds":600}`); w.Code != http.StatusUnprocessableEntity ||
		!strings.Contains(w.Body.String(), "less than 50m, the least CPU a model's Job asks for") {
		t.Errorf("a CPU limit of 20m: %d %s", w.Code, w.Body)
	}
	g := answerOf(t, settings(`{"cpu_limit":"1500m","memory_limit":"3Gi","max_timeout_seconds":1800,"max_rows":50}`)).Group
	if g.Limits != (mgLimits{"1500m", "3Gi", 1800, 50}) || g.Defaults.MaxRows == 50 {
		t.Errorf("the limits: %+v (defaults %+v)", g.Limits, g.Defaults)
	}
	var request, memRequest string
	if err := f.db.QueryRow(f.ctx, `SELECT cpu_request, memory_request FROM script_environments WHERE managed_by = 'model_group:regression'`).Scan(&request, &memRequest); err != nil ||
		request != "750m" || memRequest != "1536Mi" {
		t.Errorf("the requests are half: %q %q %v", request, memRequest, err)
	}
	// null max_rows: the recipe's again
	g = answerOf(t, settings(`{"cpu_limit":"1500m","memory_limit":"3Gi","max_timeout_seconds":1800,"max_rows":null}`)).Group
	if g.Limits.MaxRows != g.Defaults.MaxRows {
		t.Errorf("the recipe's max_rows: %+v", g.Limits)
	}
	// who may use it
	var gid int64
	if err := f.db.QueryRow(f.ctx, `INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ($1, '', false, false, now()) RETURNING id`,
		fmt.Sprintf("mg-%d", time.Now().UnixNano())).Scan(&gid); err != nil {
		t.Fatal(err)
	}
	defer f.db.Exec(f.ctx, `DELETE FROM groups WHERE id = $1`, gid) //nolint:errcheck
	access := func(body string) *httptest.ResponseRecorder {
		return f.call(f.admin, "PUT", "regression", "", body, true, mgAccess)
	}
	if w := access(`{"everyone":false,"group_ids":[999999999]}`); w.Code != http.StatusUnprocessableEntity {
		t.Errorf("an unknown user group: %d", w.Code)
	}
	g = answerOf(t, access(fmt.Sprintf(`{"everyone":false,"group_ids":[%d]}`, gid))).Group
	if g.Everyone || len(g.GroupIDs) != 1 || g.GroupIDs[0] != gid {
		t.Errorf("the access: %+v", g)
	}
	// the palette: ann is not in the group, then she is
	f.grantEdit(t, f.ann)
	f.bucket(t)
	palette := func(u *auth.User) (map[string]any, map[string]any) {
		w := httptest.NewRecorder()
		mgPalette(f.d, w, httptest.NewRequest("GET", "/models/groups", nil), &auth.Principal{User: *u})
		if w.Code != 200 {
			t.Fatalf("palette: %d %s", w.Code, w.Body)
		}
		var out struct {
			Reason *string          `json:"disabled_reason"`
			Groups []map[string]any `json:"groups"`
		}
		if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
			t.Fatal(err)
		}
		if len(out.Groups) != len(modelgroups.Groups()) {
			t.Fatalf("a line for every group shipped: %v", out.Groups)
		}
		by := map[string]map[string]any{}
		for _, e := range out.Groups {
			by[e["id"].(string)] = e
		}
		return by["regression"], by["survival"]
	}
	reg, surv := palette(f.ann)
	if reg["enabled"] != true || reg["usable"] != false || reg["ready"] != false || surv["enabled"] != false || surv["usable"] != false ||
		reg["max_timeout_seconds"].(float64) != 1800 {
		t.Errorf("ann, not in the group: %v %v", reg, surv)
	}
	if _, err := f.db.Exec(f.ctx, `INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, f.ann.ID, gid); err != nil {
		t.Fatal(err)
	}
	defer f.db.Exec(f.ctx, `DELETE FROM user_groups WHERE group_id = $1`, gid) //nolint:errcheck
	if reg, _ = palette(f.ann); reg["usable"] != true {
		t.Errorf("ann, in the group: %v", reg)
	}
	if usable, err := ModelGroupUsable(f.ctx, f.db, perm.For(f.db, f.ann), f.ann.ID, "regression"); err != nil || !usable {
		t.Errorf("usable: %v %v", usable, err)
	}
	g = answerOf(t, access(`{"everyone":true}`)).Group
	if !g.Everyone || len(g.GroupIDs) != 0 {
		t.Errorf("everyone: %+v", g)
	}
	// someone who edits no flow does not get the palette
	w := httptest.NewRecorder()
	mgPalette(f.d, w, httptest.NewRequest("GET", "/models/groups", nil), &auth.Principal{User: *f.observer})
	if w.Code != http.StatusForbidden {
		t.Errorf("an observer's palette: %d", w.Code)
	}
	if au := f.audits(); len(au) != 5 || !strings.HasPrefix(au[1], "models.group_settings ") || !strings.HasPrefix(au[3], "models.group_access ") {
		t.Errorf("the audit: %v", au)
	}
}

// the palette's scripting-off answers
func TestThePaletteSaysWhyTheModelsAreOff(t *testing.T) {
	f := newModelsFixture(t)
	f.grantEdit(t, f.ann)
	ask := func() map[string]any {
		w := httptest.NewRecorder()
		mgPalette(f.d, w, httptest.NewRequest("GET", "/models/groups", nil), &auth.Principal{User: *f.ann})
		var out map[string]any
		if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil || w.Code != 200 {
			t.Fatalf("%d %s", w.Code, w.Body)
		}
		return out
	}
	// no bucket yet: not configured
	if got := ask(); got["disabled_reason"] != "not_configured" {
		t.Errorf("no bucket: %v", got["disabled_reason"])
	}
	t.Setenv("SCRIPTS__ENABLED", "false")
	scriptcfg.Reload()
	if got := ask(); got["disabled_reason"] != "kubernetes" {
		t.Errorf("Compose: %v", got["disabled_reason"])
	}
	if w := f.enable(f.admin, "regression"); w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "need Kubernetes") {
		t.Errorf("enabling on Compose: %d %s", w.Code, w.Body)
	}
	t.Setenv("SCRIPTS__ENABLED", "true")
	t.Setenv("SCRIPTS__REGISTRY", "")
	scriptcfg.Reload()
	if w := f.enable(f.admin, "regression"); w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "SCRIPTS__REGISTRY") {
		t.Errorf("no registry: %d %s", w.Code, w.Body)
	}
	if got := ask(); got["disabled_reason"] != "not_configured" {
		t.Errorf("no registry: %v", got["disabled_reason"])
	}
	if a := answerOf(t, f.call(f.admin, "GET", "", "", "", false, mgList)); a.DisabledReason == nil || *a.DisabledReason != "not_configured" || a.Editable {
		t.Errorf("the section: %+v", a)
	}
}

// grantEdit gives the person EDIT on a folder of their own: someone who edits flows.
func (f *modelsFixture) grantEdit(t *testing.T, u *auth.User) {
	t.Helper()
	var project int64
	if err := f.db.QueryRow(f.ctx, `INSERT INTO projects (name, description, created_at) VALUES ($1, '', now()) RETURNING id`, fmt.Sprintf("mg-%d", time.Now().UnixNano())).Scan(&project); err != nil {
		t.Skipf("the projects' table is not what this test expects: %v", err)
	}
	if _, err := f.db.Exec(f.ctx, `INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, 'edit')`, project, u.ID); err != nil {
		t.Skipf("the permissions' table is not what this test expects: %v", err)
	}
	t.Cleanup(func() {
		_, _ = f.db.Exec(f.ctx, `DELETE FROM permissions WHERE project_id = $1`, project)
		_, _ = f.db.Exec(f.ctx, `DELETE FROM projects WHERE id = $1`, project)
	})
}

// bucket gives the scripts a bucket: a model Job reads its input there. It goes at the test's end.
func (f *modelsFixture) bucket(t *testing.T) {
	t.Helper()
	if _, err := f.db.Exec(f.ctx, `INSERT INTO script_storage (id, endpoint, region, bucket, access_key, secret_key_enc, updated_at, updated_by)
		VALUES (1, '', '', 'b', 'k', 'x', now(), 'test') ON CONFLICT (id) DO UPDATE SET bucket = 'b', access_key = 'k', secret_key_enc = 'x'`); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _, _ = f.db.Exec(f.ctx, `DELETE FROM script_storage WHERE id = 1`) })
}

// ── the build: a version through the loop, step by step ───────────────────────

func (f *modelsFixture) builds() *scriptBuilds {
	cfg, err := config.Load(config.FromOS())
	if err != nil {
		f.t.Fatal(err)
	}
	return &scriptBuilds{db: f.db, log: f.d.Log, panel: settings.New(cfg, false, f.d.Log), cfg: cfg.Jobs.Kube, s: scriptcfg.Load(config.FromOS())}
}

const digest = "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

func buildLog() string {
	return "#1 building\nTABULARIA_IMAGE_DIGEST=" + digest + "\nTABULARIA_IMAGE_ARCH=x86_64\n"
}

func selftestLog(report string) string {
	return "arrow@26.0.1\nstats@4.6.0\nTABULARIA_COMPLETIONS {\"stats\":[\"lm\"]}\n\n" + mgSelftestMark + "\n" +
		"checking linear\nTABULARIA_SELFTEST_REPORT " + report + "\n"
}

const passed = `{"group":"regression","ok":12,"failed":0,"skipped":0,"software":{"R":"4.6.0"},"checks":[]}`
const notPassed = `{"group":"regression","ok":11,"failed":1,"skipped":0,"software":{},"checks":[` +
	`{"name":"linear/coef/x","status":"failed","detail":"got 1.5, expected 2 (tolerance 1e-9)"}]}`

// buildTo drives a queued version of the group to its end: ready (ok) or failed by its self-test.
func (f *modelsFixture) buildTo(t *testing.T, group string, ok bool) {
	t.Helper()
	b := f.builds()
	v := f.versions(group)[0]
	env := v.ID
	_ = env
	var envID int64
	if err := f.db.QueryRow(f.ctx, `SELECT environment_id FROM script_environment_versions WHERE id = $1`, v.ID).Scan(&envID); err != nil {
		t.Fatal(err)
	}
	name := func(stage string) string { return fmt.Sprintf("tabularia-%s-e%d-v%d", stage, envID, v.Number) }
	round := func(want string) {
		t.Helper()
		if _, err := b.oneRound(f.ctx); err != nil {
			t.Fatal(err)
		}
		if got := f.versions(group)[0].Status; got != want {
			t.Fatalf("after the round: %s, want %s", got, want)
		}
	}
	round("building")
	f.kube.end(name("build"), true, 0, buildLog())
	round("checking")
	if ok {
		f.kube.end(name("check"), true, 0, selftestLog(passed))
	} else {
		f.kube.end(name("check"), false, 4, selftestLog(notPassed))
	}
	if ok {
		round("ready")
	} else {
		round("failed")
	}
}

func TestAGroupsVersionIsBuiltCheckedAndSelfTested(t *testing.T) {
	f := newModelsFixture(t)
	f.enable(f.admin, "regression")
	b := f.builds()
	var envID int64
	if err := f.db.QueryRow(f.ctx, `SELECT id FROM script_environments WHERE managed_by = 'model_group:regression'`).Scan(&envID); err != nil {
		t.Fatal(err)
	}
	build, check := fmt.Sprintf("tabularia-build-e%d-v1", envID), fmt.Sprintf("tabularia-check-e%d-v1", envID)
	// queued → building: the BuildKit Job of the recipe, in the build namespace
	if n, err := b.oneRound(f.ctx); err != nil || n != 1 {
		t.Fatalf("round 1: %d %v", n, err)
	}
	job := f.kube.job("builds", build)
	if job == nil {
		t.Fatal("no build Job")
	}
	dockerfile := fmt.Sprint(job)
	if !strings.Contains(dockerfile, "rocker/r-ver:4.6.0") || !strings.Contains(dockerfile, "arrow@26.0.1") && !strings.Contains(dockerfile, "arrow") {
		t.Errorf("the Dockerfile is not the recipe's: %.300s", dockerfile)
	}
	// a Job still running changes nothing
	if n, err := b.oneRound(f.ctx); err != nil || n != 0 {
		t.Errorf("a build that runs: %d %v", n, err)
	}
	// building → checking: the check Job with the self-test and its code, and the build's Job goes
	f.kube.end(build, true, 0, buildLog())
	if n, err := b.oneRound(f.ctx); err != nil || n != 1 {
		t.Fatalf("round 3: %d %v", n, err)
	}
	checkJob := f.kube.job("scripts", check)
	if checkJob == nil {
		t.Fatal("no check Job")
	}
	text, _ := json.Marshal(checkJob)
	for _, want := range []string{"--selftest", "TABULARIA_R_CHECK", modelgroups.CodeDir, "registry:5000/envs/tabularia-env-" + fmt.Sprint(envID) + "@" + digest,
		`"readOnlyRootFilesystem":true`, `"runAsUser":10001`} {
		if !strings.Contains(string(text), want) {
			t.Errorf("the check Job lacks %s: %.400s", want, text)
		}
	}
	if f.kube.job("builds", build) != nil {
		t.Error("the build's Job is still there")
	}
	cm := f.kube.configmaps["scripts/"+fmt.Sprintf("tabularia-selftest-e%d-v1", envID)]
	if cm == nil {
		t.Fatal("no ConfigMap of the code")
	}
	data, _ := json.Marshal(cm)
	if !strings.Contains(string(data), `"ownerReferences"`) || !strings.Contains(string(data), "uid-"+check) {
		t.Errorf("the ConfigMap does not go with the Job: %.300s", data)
	}
	// the Job maps the ConfigMap's keys to the runner's paths and the reference cases
	for _, want := range []string{`"path":"runner/run.R"`, `"path":"reference/expected.json"`, `"path":"common/R/main.R"`, `"path":"card.schema.json"`} {
		if !strings.Contains(string(text), want) {
			t.Errorf("the check Job does not mount %s", want)
		}
	}
	if files, _ := cm["data"].(map[string]any); len(files) < 10 {
		t.Errorf("the ConfigMap holds %d files", len(files))
	}
	if v := f.versions("regression")[0]; v.Status != "checking" || v.ImageRef == nil || !strings.HasSuffix(*v.ImageRef, "@"+digest) || v.Arch == nil || *v.Arch != "amd64" {
		t.Errorf("the version after the build: %+v", v)
	}
	// checking → ready: the self-test passed
	f.kube.end(check, true, 0, selftestLog(passed))
	if n, err := b.oneRound(f.ctx); err != nil || n != 1 {
		t.Fatalf("round 4: %d %v", n, err)
	}
	v := f.versions("regression")[0]
	if v.Status != "ready" || v.Error != nil || v.Lock == nil || !strings.Contains(*v.Lock, "arrow@26.0.1") || strings.Contains(*v.Lock, "SELFTEST") ||
		v.Selftest == nil || !strings.Contains(*v.Selftest, "TABULARIA_SELFTEST_REPORT") {
		t.Errorf("the version: %+v", v)
	}
	img, err := ReadyModelGroupImage(f.ctx, f.db, "regression")
	if err != nil {
		t.Fatal(err)
	}
	g, _ := modelgroups.Get("regression")
	if img.Version != 1 || img.Digest != digest || !strings.HasSuffix(img.Image, "@"+digest) || img.Arch != "amd64" || img.RecipeHash != g.RecipeHash ||
		img.RunnerHash != g.RunnerHash || img.MaxRows != g.Recipe.MaxRows || img.CPULimit == "" || img.CPURequest == "" || img.EnvironmentID != envID || img.InternetAccess {
		t.Errorf("the image: %+v", img)
	}
	// the section says so, with its lock and its self-test
	d := answerOf(t, f.call(f.admin, "GET", "regression", "", "", false, mgDetail)).Group
	if d.State != "ready" || !d.Ready || d.CurrentVersion == nil || *d.CurrentVersion != 1 || d.Versions[0].Lock == nil || d.Versions[0].Selftest == nil || d.Versions[0].Packages < 1 {
		t.Errorf("the detail: %+v", d)
	}
	// the list keeps the logs out
	l := answerOf(t, f.call(f.observer, "GET", "", "", "", false, mgList))
	for _, g := range l.Groups {
		for _, v := range g.Versions {
			if v.Lock != nil || v.BuildLog != nil || v.Selftest != nil {
				t.Errorf("the list carries a log: %+v", v)
			}
		}
	}
}

// The card of the list names the system library with the version the image was built with, as the
// detail does, and still carries no lock (found on kind: the list said «GSL (Debian)» alone).
func TestTheListNamesTheSystemLibrariesVersionsWithoutTheLock(t *testing.T) {
	f := newModelsFixture(t)
	f.enable(f.admin, "customer_lifetime")
	f.buildTo(t, "customer_lifetime", true)
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET lock = E'arrow@26.0.1\nlibgsl-dev@2.7.1+dfsg-6ubuntu2' WHERE id = $1`,
		f.versions("customer_lifetime")[0].ID); err != nil {
		t.Fatal(err)
	}
	l := answerOf(t, f.call(f.observer, "GET", "", "", "", false, mgList))
	found := false
	for _, g := range l.Groups {
		for _, v := range g.Versions {
			if v.Lock != nil || v.BuildLog != nil || v.Selftest != nil {
				t.Errorf("the list carries a log: %+v", v)
			}
		}
		if g.ID == "customer_lifetime" {
			// the library's own version, and the package's whole one beside it (69dc54eb)
			found = len(g.SystemLibraries) == 1 && g.SystemLibraries[0].Package == "libgsl-dev" && g.SystemLibraries[0].Version == "2.7.1" &&
				g.SystemLibraries[0].PackageVersion == "2.7.1+dfsg-6ubuntu2"
		}
	}
	if !found {
		t.Errorf("the list's system libraries: %+v", l.Groups)
	}
}

func TestAVersionWhoseSelftestFailsIsFailedNotReady(t *testing.T) {
	f := newModelsFixture(t)
	f.enable(f.admin, "regression")
	f.buildTo(t, "regression", false)
	v := f.versions("regression")[0]
	if v.Status != "failed" || v.Error == nil || !strings.Contains(*v.Error, "self-test of the group failed") || !strings.Contains(*v.Error, "linear/coef/x: got 1.5, expected 2") {
		t.Errorf("the version: %+v", v)
	}
	if v.Selftest == nil || !strings.Contains(*v.Selftest, "checking linear") {
		t.Error("the self-test's log is not kept")
	}
	g := answerOf(t, f.call(f.admin, "GET", "regression", "", "", false, mgDetail)).Group
	if g.State != "failed" || g.Ready || g.CurrentVersion != nil {
		t.Errorf("the group: %+v", g)
	}
	_, err := ReadyModelGroupImage(f.ctx, f.db, "regression")
	un, isUn := err.(*ModelGroupUnavailable)
	if !isUn || un.Reason != "failed" || !strings.Contains(un.Message, "last build failed") {
		t.Errorf("a failed group runs: %v", err)
	}
	// it stays failed: the loop does not build it again by itself, the administrator does
	if n, err := f.builds().oneRound(f.ctx); err != nil || n != 0 || len(f.versions("regression")) != 1 {
		t.Errorf("a round after a failure: %d %v, %d versions", n, err, len(f.versions("regression")))
	}
	if g := answerOf(t, f.call(f.admin, "POST", "regression", "", "", true, mgRebuild)).Group; len(g.Versions) != 2 {
		t.Errorf("rebuilt: %+v", g.Versions)
	}
}

func TestAnImageThatDoesNotStartOrAnAbsentReportFailsTheVersion(t *testing.T) {
	f := newModelsFixture(t)
	f.enable(f.admin, "timeseries")
	b := f.builds()
	var envID int64
	_ = f.db.QueryRow(f.ctx, `SELECT id FROM script_environments WHERE managed_by = 'model_group:timeseries'`).Scan(&envID)
	build, check := fmt.Sprintf("tabularia-build-e%d-v1", envID), fmt.Sprintf("tabularia-check-e%d-v1", envID)
	for range 2 {
		if _, err := b.oneRound(f.ctx); err != nil {
			t.Fatal(err)
		}
		f.kube.end(build, true, 0, buildLog())
	}
	// before the self-test: arrow has no S3 support
	f.kube.end(check, false, 1, "Error: the arrow package of this image has no S3 support\n")
	if _, err := b.oneRound(f.ctx); err != nil {
		t.Fatal(err)
	}
	v := f.versions("timeseries")[0]
	if v.Status != "failed" || v.Error == nil || !strings.Contains(*v.Error, "does not start as a script would") || !strings.Contains(*v.Error, "S3 support") {
		t.Errorf("an image that does not start: %+v", v)
	}
	// a self-test that dies with no report: the exit code and the end of its log say so
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET status = 'failed'`); err != nil {
		t.Fatal(err)
	}
	f.call(f.admin, "POST", "timeseries", "", "", true, mgRebuild)
	build2, check2 := fmt.Sprintf("tabularia-build-e%d-v2", envID), fmt.Sprintf("tabularia-check-e%d-v2", envID)
	f.kube.end(build2, true, 0, buildLog())
	for range 2 {
		if _, err := b.oneRound(f.ctx); err != nil {
			t.Fatal(err)
		}
	}
	f.kube.end(check2, false, 4, selftestLog("not json")[:strings.Index(selftestLog(""), "TABULARIA_SELFTEST_REPORT")]+"Error in fit: singular\n")
	if _, err := b.oneRound(f.ctx); err != nil {
		t.Fatal(err)
	}
	v = f.versions("timeseries")[0]
	if v.Status != "failed" || v.Error == nil || !strings.Contains(*v.Error, "did not run to its end: exit code 4") || !strings.Contains(*v.Error, "singular") {
		t.Errorf("a self-test with no report: %+v", v)
	}
}

func TestAChangedRecipeQueuesANewVersionByItself(t *testing.T) {
	f := newModelsFixture(t)
	f.enable(f.admin, "survival")
	f.buildTo(t, "survival", true)
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET recipe_hash = 'of-another-recipe'`); err != nil {
		t.Fatal(err)
	}
	g := answerOf(t, f.call(f.admin, "GET", "survival", "", "", false, mgDetail)).Group
	if g.State != "rebuild_needed" || g.Ready || !g.Versions[0].Stale {
		t.Errorf("rebuild needed: %+v", g)
	}
	_, err := ReadyModelGroupImage(f.ctx, f.db, "survival")
	if un, ok := err.(*ModelGroupUnavailable); !ok || un.Reason != "rebuilding" || un.Message != "the model group survival is being rebuilt for this version of Tabularia" {
		t.Errorf("a stale group runs: %v", err)
	}
	// the loop's next round queues it, once
	b := f.builds()
	if n, err := b.oneRound(f.ctx); err != nil || n < 1 {
		t.Fatalf("round: %d %v", n, err)
	}
	vs := f.versions("survival")
	if len(vs) != 2 || vs[0].Number != 2 || vs[0].RecipeHash == nil || *vs[0].RecipeHash == "of-another-recipe" {
		t.Fatalf("the versions: %+v", vs)
	}
	if _, err := b.oneRound(f.ctx); err != nil || len(f.versions("survival")) != 2 {
		t.Errorf("queued twice: %v", err)
	}
	// a group that is not enabled is not built again
	f.call(f.admin, "POST", "survival", "", "", true, mgDisable)
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET recipe_hash = 'old', status = 'ready' WHERE number = 2`); err != nil {
		t.Fatal(err)
	}
	if _, err := b.oneRound(f.ctx); err != nil || len(f.versions("survival")) != 2 {
		t.Errorf("a disabled group was queued: %v", err)
	}
}

// ── a new runner: the self-test again, on the image in use ─────────────────────

func (f *modelsFixture) envOf(group string) int64 {
	var id int64
	if err := f.db.QueryRow(f.ctx, `SELECT id FROM script_environments WHERE managed_by = $1`, mgManagedBy(group)).Scan(&id); err != nil {
		f.t.Fatal(err)
	}
	return id
}

func TestANewRunnerSelftestsTheImageInUseAgainAndItKeepsServing(t *testing.T) {
	f := newModelsFixture(t)
	f.enable(f.admin, "survival")
	f.buildTo(t, "survival", true)
	b := f.builds()
	env := f.envOf("survival")
	recheck := fmt.Sprintf("tabularia-recheck-e%d-v1", env)
	g, _ := modelgroups.Get("survival")
	// the same runner: nothing runs
	if n, err := b.oneRound(f.ctx); err != nil || n != 0 || f.kube.job("scripts", recheck) != nil {
		t.Fatalf("the same runner: %d %v", n, err)
	}
	image, err := ReadyModelGroupImage(f.ctx, f.db, "survival")
	if err != nil {
		t.Fatal(err)
	}
	// an upgrade changed the runner: the version was self-tested with another one
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET runner_hash = 'of-the-old-runner'`); err != nil {
		t.Fatal(err)
	}
	if _, err := b.oneRound(f.ctx); err != nil {
		t.Fatal(err)
	}
	job := f.kube.job("scripts", recheck)
	if job == nil {
		t.Fatal("the self-test was not started again")
	}
	text, _ := json.Marshal(job)
	if !strings.Contains(string(text), "--selftest") || !strings.Contains(string(text), strings.TrimPrefix(image.Image, "")) {
		t.Errorf("the Job is not the self-test on the same image: %.300s", text)
	}
	if len(f.kube.recheckCodes(env)) != 1 {
		t.Error("no ConfigMap of the code")
	}
	if len(f.versions("survival")) != 1 {
		t.Error("a version was built: the image does not change")
	}
	// meanwhile the version serves, and the card says it is being checked
	still, err := ReadyModelGroupImage(f.ctx, f.db, "survival")
	if err != nil || still.Image != image.Image {
		t.Fatalf("the version in use stopped serving: %v", err)
	}
	if d := answerOf(t, f.call(f.admin, "GET", "survival", "", "", false, mgDetail)).Group; d.State != "rechecking" || !d.Ready ||
		!d.Versions[0].Rechecking || d.Versions[0].Recheck != "rechecking" {
		t.Errorf("the card: %+v", d)
	}
	// a Job that runs is not started twice
	if _, err := b.oneRound(f.ctx); err != nil || len(f.kube.jobs) != 1 {
		t.Errorf("a second round: %v, %d Jobs", err, len(f.kube.jobs))
	}
	// it passes: the version records the new runner and nothing runs again
	f.kube.end(recheck, true, 0, selftestLog(passed))
	if _, err := b.oneRound(f.ctx); err != nil {
		t.Fatal(err)
	}
	v := f.versions("survival")[0]
	if v.Status != "ready" || v.JobName != nil || v.RunnerHash == nil || *v.RunnerHash != g.RunnerHash || v.Error != nil {
		t.Errorf("the version after the check: %+v", v)
	}
	if f.kube.job("scripts", recheck) != nil {
		t.Error("the Job is still there")
	}
	if _, err := b.oneRound(f.ctx); err != nil || f.kube.job("scripts", recheck) != nil {
		t.Errorf("run again: %v", err)
	}
	if d := answerOf(t, f.call(f.admin, "GET", "survival", "", "", false, mgDetail)).Group; d.State != "ready" || d.Versions[0].Rechecking ||
		d.Versions[0].Recheck != "" {
		t.Error("still said to be checked")
	}
}

func TestANewRunnerThatTheImageDoesNotPassFailsTheGroupWithTheDifference(t *testing.T) {
	f := newModelsFixture(t)
	f.enable(f.admin, "survival")
	f.buildTo(t, "survival", true)
	b := f.builds()
	recheck := fmt.Sprintf("tabularia-recheck-e%d-v1", f.envOf("survival"))
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET runner_hash = NULL`); err != nil {
		t.Fatal(err)
	}
	if _, err := b.oneRound(f.ctx); err != nil || f.kube.job("scripts", recheck) == nil {
		t.Fatalf("not started: %v", err)
	}
	f.kube.end(recheck, false, 4, selftestLog(notPassed))
	if _, err := b.oneRound(f.ctx); err != nil {
		t.Fatal(err)
	}
	v := f.versions("survival")[0]
	if v.Status != "failed" || v.JobName != nil || v.Error == nil || !strings.Contains(*v.Error, "runner of the statistical models changed") ||
		!strings.Contains(*v.Error, "linear/coef/x: got 1.5, expected 2") {
		t.Errorf("the version: %+v", v)
	}
	if g := answerOf(t, f.call(f.admin, "GET", "survival", "", "", false, mgDetail)).Group; g.State != "failed" || g.Ready {
		t.Errorf("the group: %+v", g)
	}
	if _, err := ReadyModelGroupImage(f.ctx, f.db, "survival"); err == nil {
		t.Error("a group whose self-test fails runs")
	}
	// it is not tried again by itself
	if _, err := b.oneRound(f.ctx); err != nil || f.kube.job("scripts", recheck) != nil {
		t.Errorf("tried again: %v", err)
	}
}

// A recheck the cluster stops before the self-test reports says nothing of the image: the version in
// use stays ready and serves, the card says the recheck is tried again and why, and it is, later, with
// its code in a ConfigMap of a name of its own.
func TestARecheckThatDidNotRunIsTriedAgainAndTheVersionServes(t *testing.T) {
	for _, c := range []struct {
		name string
		stop func(k *fakeKube, job string)
		why  string
	}{
		{"the Job failed before its Pod ran", func(k *fakeKube, job string) { k.end(job, false, 0, "") },
			"it stopped before the self-test began (BackoffLimitExceeded)"},
		{"the Job ended without any log", func(k *fakeKube, job string) { k.end(job, true, 0, "") }, "it stopped before the self-test began"},
		{"the cluster does not give the log", func(k *fakeKube, job string) { k.end(job, false, 4, selftestLog(notPassed)); k.hideLog(job) },
			"the cluster did not give its log: "},
		{"the self-test ended without its report", func(k *fakeKube, job string) {
			k.end(job, false, 137, "arrow@26.0.1\n\n"+mgSelftestMark+"\nchecking linear\n")
		}, "the self-test ended without its report (BackoffLimitExceeded)"},
		{"the Job is gone", func(k *fakeKube, job string) { k.remove("scripts", job) }, "its Job is no longer in the cluster"},
		{"the cluster does not create its Pod", func(k *fakeKube, job string) {
			k.refuse(job, `pods "x" is forbidden: exceeded quota: scripts, requested: limits.cpu=2`)
			k.older("scripts", job, 60)
		}, "it cannot start: the namespace scripts has no room left (its ResourceQuota is full)"},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := newModelsFixture(t)
			f.enable(f.admin, "survival")
			f.buildTo(t, "survival", true)
			b := f.builds()
			env := f.envOf("survival")
			recheck := fmt.Sprintf("tabularia-recheck-e%d-v1", env)
			if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET runner_hash = 'of-the-old-runner'`); err != nil {
				t.Fatal(err)
			}
			if _, err := b.oneRound(f.ctx); err != nil || f.kube.job("scripts", recheck) == nil {
				t.Fatalf("no recheck: %v", err)
			}
			c.stop(f.kube, recheck)
			if _, err := b.oneRound(f.ctx); err != nil {
				t.Fatal(err)
			}
			if f.kube.job("scripts", recheck) != nil {
				t.Error("the Job that did not run is still there")
			}
			v := f.versions("survival")[0]
			if v.Status != "ready" || v.Error != nil || v.JobName != nil || v.RecheckAttempts != 1 || !strings.HasPrefix(deref(v.RecheckError), c.why) {
				t.Fatalf("the version: %s %q, attempts %d, why %q", v.Status, deref(v.Error), v.RecheckAttempts, deref(v.RecheckError))
			}
			if _, err := ReadyModelGroupImage(f.ctx, f.db, "survival"); err != nil {
				t.Fatalf("the version in use stopped serving: %v", err)
			}
			d := answerOf(t, f.call(f.admin, "GET", "survival", "", "", false, mgDetail)).Group
			if out := d.Versions[0]; d.State != "recheck_retrying" || !d.Ready || out.Recheck != "recheck_retrying" || out.RecheckAttempts != 1 ||
				out.RecheckMax != 6 || out.RecheckAfter == nil || !strings.HasPrefix(deref(out.RecheckError), c.why) {
				t.Errorf("the card: %s %+v", d.State, out)
			}
			// not before its time
			if _, err := b.oneRound(f.ctx); err != nil || f.kube.job("scripts", recheck) != nil {
				t.Fatalf("tried again at once: %v", err)
			}
			// then again, with a ConfigMap of its own
			if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET recheck_after = now() - interval '1 second'`); err != nil {
				t.Fatal(err)
			}
			if _, err := b.oneRound(f.ctx); err != nil || f.kube.job("scripts", recheck) == nil {
				t.Fatalf("not tried again: %v", err)
			}
			if codes := f.kube.recheckCodes(env); len(codes) != 2 {
				t.Errorf("the code's ConfigMaps: %v", codes)
			}
			if d := answerOf(t, f.call(f.admin, "GET", "survival", "", "", false, mgDetail)).Group; d.State != "rechecking" {
				t.Errorf("the card: %s", d.State)
			}
		})
	}
}

// As many rechecks as a node may have retries, then no more: the version in use serves on, the card
// says the recheck stopped and why. A later runner starts them again.
func TestRechecksThatNeverRunStopAndTheVersionServesOn(t *testing.T) {
	f := newModelsFixture(t)
	f.enable(f.admin, "survival")
	f.buildTo(t, "survival", true)
	b := f.builds()
	recheck := fmt.Sprintf("tabularia-recheck-e%d-v1", f.envOf("survival"))
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET runner_hash = 'of-the-old-runner'`); err != nil {
		t.Fatal(err)
	}
	round := func() {
		t.Helper()
		if _, err := b.oneRound(f.ctx); err != nil {
			t.Fatal(err)
		}
	}
	for attempt := 1; attempt <= scriptnodes.MaxRetries+1; attempt++ {
		round()
		if f.kube.job("scripts", recheck) == nil {
			t.Fatalf("attempt %d not started", attempt)
		}
		f.kube.end(recheck, false, 0, "")
		round()
		if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET recheck_after = now() - interval '1 second'`); err != nil {
			t.Fatal(err)
		}
	}
	round()
	if f.kube.job("scripts", recheck) != nil {
		t.Fatal("tried again past the retries")
	}
	if _, err := ReadyModelGroupImage(f.ctx, f.db, "survival"); err != nil {
		t.Fatalf("the version in use stopped serving: %v", err)
	}
	d := answerOf(t, f.call(f.admin, "GET", "survival", "", "", false, mgDetail)).Group
	if out := d.Versions[0]; d.State != "ready" || out.Recheck != "recheck_stopped" || out.RecheckAttempts != 6 || out.RecheckAfter != nil ||
		!strings.HasPrefix(deref(out.RecheckError), "it stopped before the self-test began") {
		t.Errorf("the card: %s %+v", d.State, out)
	}
	// the attempts counted were for the runner shipped: a later one starts them again
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET recheck_runner = 'of-an-earlier-runner'`); err != nil {
		t.Fatal(err)
	}
	round()
	if v := f.versions("survival")[0]; f.kube.job("scripts", recheck) == nil || v.RecheckAttempts != 0 || v.RecheckError != nil {
		t.Errorf("a new runner's recheck: %+v", v)
	}
}

// A recheck started by a gateway of another runner (R2), followed by this one (R1, the release
// rolled back to, which had already self-tested the image): whatever R2's code found, the version
// stays ready and self-tested with R1, no attempt is counted, and nothing runs again.
func TestARecheckOfAnotherRunnerIsNotJudgedByThisOne(t *testing.T) {
	for _, c := range []struct {
		name string
		end  func(k *fakeKube, job string)
	}{
		{"its values differ", func(k *fakeKube, job string) { k.end(job, false, 4, selftestLog(notPassed)) }},
		{"it passed", func(k *fakeKube, job string) { k.end(job, true, 0, selftestLog(passed)) }},
		{"it did not run", func(k *fakeKube, job string) { k.end(job, false, 0, "") }},
		{"its Job is gone", func(k *fakeKube, job string) { k.remove("scripts", job) }},
	} {
		t.Run(c.name, func(t *testing.T) {
			f := newModelsFixture(t)
			f.enable(f.admin, "survival")
			f.buildTo(t, "survival", true)
			b := f.builds()
			recheck := fmt.Sprintf("tabularia-recheck-e%d-v1", f.envOf("survival"))
			g, _ := modelgroups.Get("survival")
			if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET runner_hash = 'of-the-old-runner'`); err != nil {
				t.Fatal(err)
			}
			if _, err := b.oneRound(f.ctx); err != nil || f.kube.job("scripts", recheck) == nil {
				t.Fatalf("no recheck: %v", err)
			}
			// R2 started it; the gateway now following it is R1, with which the image passed
			if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET runner_hash = $1, recheck_runner = 'R2'`, g.RunnerHash); err != nil {
				t.Fatal(err)
			}
			// while it runs, it is left to its end
			if _, err := b.oneRound(f.ctx); err != nil || f.kube.job("scripts", recheck) == nil {
				t.Fatalf("a running Job of another runner was not left alone: %v", err)
			}
			c.end(f.kube, recheck)
			if _, err := b.oneRound(f.ctx); err != nil {
				t.Fatal(err)
			}
			v := f.versions("survival")[0]
			if v.Status != "ready" || v.Error != nil || v.JobName != nil || deref(v.RunnerHash) != g.RunnerHash || v.RecheckRunner != nil ||
				v.RecheckAttempts != 0 || v.RecheckError != nil || v.RecheckAfter != nil {
				t.Fatalf("the version: %s %q runner %q recheck %q attempts %d why %q", v.Status, deref(v.Error), deref(v.RunnerHash),
					deref(v.RecheckRunner), v.RecheckAttempts, deref(v.RecheckError))
			}
			if _, err := ReadyModelGroupImage(f.ctx, f.db, "survival"); err != nil {
				t.Fatalf("the group stopped: %v", err)
			}
			if f.kube.job("scripts", recheck) != nil {
				t.Error("the Job is still there, or a recheck started for a runner the image already passed")
			}
			if _, err := b.oneRound(f.ctx); err != nil || f.kube.job("scripts", recheck) != nil {
				t.Errorf("run again: %v", err)
			}
			if d := answerOf(t, f.call(f.admin, "GET", "survival", "", "", false, mgDetail)).Group; d.State != "ready" || d.Versions[0].Recheck != "" {
				t.Errorf("the card: %s %q", d.State, d.Versions[0].Recheck)
			}
		})
	}
}

// R1 → R2 → R3: R2's recheck passes, but this gateway is R3. The image is not recorded as passed
// with R3 (it never ran R3's code): R3's own recheck starts at once, and its pass is recorded.
func TestAPassOfAnotherRunnerStartsThisRunnersRecheck(t *testing.T) {
	f := newModelsFixture(t)
	f.enable(f.admin, "survival")
	f.buildTo(t, "survival", true)
	b := f.builds()
	recheck := fmt.Sprintf("tabularia-recheck-e%d-v1", f.envOf("survival"))
	g, _ := modelgroups.Get("survival")
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET runner_hash = 'R1'`); err != nil {
		t.Fatal(err)
	}
	if _, err := b.oneRound(f.ctx); err != nil || f.kube.job("scripts", recheck) == nil {
		t.Fatalf("no recheck: %v", err)
	}
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET recheck_runner = 'R2', recheck_attempts = 2`); err != nil {
		t.Fatal(err)
	}
	f.kube.end(recheck, true, 0, selftestLog(passed))
	if _, err := b.oneRound(f.ctx); err != nil {
		t.Fatal(err)
	}
	v := f.versions("survival")[0]
	if v.Status != "ready" || deref(v.RunnerHash) != "R1" || deref(v.RecheckRunner) != g.RunnerHash || v.RecheckAttempts != 0 {
		t.Fatalf("the version: %s runner %q recheck %q attempts %d", v.Status, deref(v.RunnerHash), deref(v.RecheckRunner), v.RecheckAttempts)
	}
	if f.kube.job("scripts", recheck) == nil {
		t.Fatal("no recheck with this runner")
	}
	f.kube.end(recheck, true, 0, selftestLog(passed))
	if _, err := b.oneRound(f.ctx); err != nil {
		t.Fatal(err)
	}
	if v := f.versions("survival")[0]; deref(v.RunnerHash) != g.RunnerHash || v.RecheckRunner != nil || v.JobName != nil {
		t.Errorf("this runner's pass: runner %q recheck %q", deref(v.RunnerHash), deref(v.RecheckRunner))
	}
}

func TestNoSelftestAgainWhileAVersionIsBeingBuilt(t *testing.T) {
	f := newModelsFixture(t)
	f.enable(f.admin, "survival")
	f.buildTo(t, "survival", true)
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET runner_hash = 'old'`); err != nil {
		t.Fatal(err)
	}
	f.call(f.admin, "POST", "survival", "", "", true, mgRebuild) // version 2 queued
	if _, err := f.builds().oneRound(f.ctx); err != nil {
		t.Fatal(err)
	}
	if f.kube.job("scripts", fmt.Sprintf("tabularia-recheck-e%d-v1", f.envOf("survival"))) != nil {
		t.Error("a self-test again beside a build that will have it")
	}
}
