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
	"testing"
	"time"

	"github.com/jackc/pgx/v5/pgxpool"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/clientip"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/config"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyd"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/pyjson"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/scriptcfg"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/server"
)

// A scripting environment's version without internet access (docs/scripting.md, «Internet
// access»): the pods of its scripts and notebooks carry labelInternet "off", which the
// chart's policies select; only an administrator changes it, and the audit says so.

// podLabels is a Job manifest's labels: the Job's own and its pod template's.
func podLabels(t *testing.T, manifest pyjson.Object) (pyjson.Object, pyjson.Object) {
	t.Helper()
	meta := pyjson.Obj(manifest.Value("metadata"))
	spec := pyjson.Obj(manifest.Value("spec"))
	tmpl := pyjson.Obj(pyjson.Obj(spec.Value("template")).Value("metadata"))
	return pyjson.Obj(meta.Value("labels")), pyjson.Obj(tmpl.Value("labels"))
}

func TestAScriptOfAVersionWithoutInternetCarriesTheLabel(t *testing.T) {
	env := &scriptEnv{ID: 3, Name: "Python", Language: "python", CPURequest: "100m", CPULimit: "1", MemoryRequest: "128Mi", MemoryLimit: "1Gi"}
	image := "registry:5000/envs@sha256:abc"
	node := pyjson.Object{{Key: "id", Value: "s1"}, {Key: "data", Value: pyjson.Object{{Key: "entrypoint", Value: "main.py"}}}}
	labels := pyjson.Object{{Key: "app.kubernetes.io/component", Value: "scripting"}, {Key: labelEnv, Value: "3"}}
	for _, internet := range []bool{true, false} {
		version := &scriptVersion{Number: 2, Status: "ready", ImageRef: &image, InternetAccess: internet}
		m := scriptJobManifest(jobSettings{jobTTL: 60}, "tabularia-script-9-a1", env, version, node, pyjson.Object{}, labels, 900,
			[]any{}, "code", "env", nil)
		job, pod := podLabels(t, m)
		for what, l := range map[string]pyjson.Object{"job": job, "pod": pod} {
			v, has := l.Get(labelInternet)
			if internet && has {
				t.Errorf("internet on: the %s has %s=%v", what, labelInternet, v)
			}
			if !internet && v != "off" {
				t.Errorf("internet off: the %s's labels are %v", what, l)
			}
			if l.Value(labelEnv) != "3" || l.Value("app.kubernetes.io/component") != "scripting" {
				t.Errorf("the %s lost its other labels: %v", what, l)
			}
		}
	}
	if _, has := labels.Get(labelInternet); has || len(labels) != 2 {
		t.Errorf("the caller's labels were changed (the next attempt would carry them): %v", labels)
	}
}

func TestANotebookOfAVersionWithoutInternetCarriesTheLabel(t *testing.T) {
	env := &scEnvironment{ID: 4, Name: "Notebooks", Language: "python", CPURequest: "100m", CPULimit: "1", MemoryRequest: "128Mi", MemoryLimit: "1Gi"}
	for _, internet := range []bool{true, false} {
		img := &nbImage{env: env, versionID: 7, imageRef: "registry:5000/envs@sha256:abc", internet: internet}
		for _, mode := range []string{"app", "edit"} {
			job, pod := podLabels(t, nbJobManifest("nb-1-"+mode+"-abc", 1, mode, img, "files", "token", nil))
			for what, l := range map[string]pyjson.Object{"job": job, "pod": pod} {
				if v, has := l.Get(labelInternet); internet == has || (!internet && v != "off") {
					t.Errorf("internet %v, %s %s: %v", internet, mode, what, l)
				}
				if l.Value("app.kubernetes.io/component") != "notebook" {
					t.Errorf("%s %s: the policies select the notebooks by their component: %v", mode, what, l)
				}
			}
		}
	}
}

func TestTheOfflineNoticeAndFailure(t *testing.T) {
	if got := offlineNotice("Python", 3); !strings.HasPrefix(got, "Internet access is off for «Python» v3:") {
		t.Error(got)
	}
	if offlineFailure(true) != "" || !strings.Contains(offlineFailure(false), "internet access is off") {
		t.Error("the failure's addition")
	}
}

// ── on a real database ─────────────────────────────────────────────────────────

type netFixture struct {
	t                    *testing.T
	ctx                  context.Context
	db                   *pgxpool.Pool
	d                    *Deps
	admin, observer, ann *auth.User
	env                  int64
	v1, v2               int64
}

func newNetFixture(t *testing.T) *netFixture {
	t.Helper()
	dsn := os.Getenv("TEST_DATABASE_URL")
	if dsn == "" {
		t.Skip("TEST_DATABASE_URL not set: run gateway-go/scripts/test.sh")
	}
	// scripting on, as on Kubernetes; the process's settings read again at the end, after
	// the variables are put back
	t.Cleanup(scriptcfg.Reload)
	t.Setenv("SCRIPTS__ENABLED", "true")
	t.Setenv("SCRIPTS__NAMESPACE", "scripts")
	t.Setenv("SCRIPTS__REGISTRY", "registry:5000/envs")
	scriptcfg.Reload()
	ctx := context.Background()
	db, err := pgxpool.New(ctx, dsn)
	if err != nil {
		t.Fatal(err)
	}
	f := &netFixture{t: t, ctx: ctx, db: db}
	// the auth package's tests TRUNCATE the users: one at a time
	lock, err := db.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := lock.Exec(ctx, `SELECT pg_advisory_lock(7101)`); err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	f.d = &Deps{DB: db, Log: log, Config: &config.Config{ScriptsEnabled: true, ScriptsNamespace: "scripts", ScriptsRegistry: "registry:5000/envs"},
		Auth: &auth.Authenticator{DB: db, Secret: []byte("a test secret, not of anything"), Algorithm: "HS256",
			Settings: shareSettingsStub{}, ClientIP: clientip.New(nil), Log: log}}
	one := func(sql string, args ...any) int64 {
		t.Helper()
		var id int64
		if err := db.QueryRow(ctx, sql, args...).Scan(&id); err != nil {
			t.Fatalf("%s: %v", sql, err)
		}
		return id
	}
	stamp := time.Now().UnixNano()
	user := func(name string, super, observer bool) *auth.User {
		email := fmt.Sprintf("net-%s-%d@x.test", name, stamp)
		id := one(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, $2, 'x', true, $3, $4, false, now()) RETURNING id`, email, name, super, observer)
		return &auth.User{ID: id, Email: email, FullName: name, IsActive: true, IsSuperuser: super, IsObserver: observer}
	}
	f.admin, f.observer, f.ann = user("admin", true, false), user("observer", false, true), user("ann", false, false)
	f.env = one(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit, memory_request,
		memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ($1, '', 'python', true, '500m', '2', '1Gi', '4Gi', 3600,
		now(), now()) RETURNING id`, fmt.Sprintf("zznet-%d", stamp))
	version := func(n int, status string) int64 {
		// as the previous version of the code inserts it: without the column, which is on
		return one(`INSERT INTO script_environment_versions (environment_id, number, language_version, base_image, packages, status,
			image_ref, created_at) VALUES ($1, $2, '3.12', 'python:3.12-slim', '', $3, 'registry:5000/envs@sha256:abc', now()) RETURNING id`,
			f.env, n, status)
	}
	f.v1, f.v2 = version(1, "ready"), version(2, "failed")
	if _, err := db.Exec(ctx, `UPDATE script_environments SET current_version_id = $1 WHERE id = $2`, f.v1, f.env); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		ids := []int64{f.admin.ID, f.observer.ID, f.ann.ID}
		_, _ = db.Exec(ctx, `DELETE FROM audit_logs WHERE actor_id = ANY($1)`, ids)
		_, _ = db.Exec(ctx, `DELETE FROM script_environment_versions WHERE environment_id = $1`, f.env)
		_, _ = db.Exec(ctx, `DELETE FROM script_environments WHERE id = $1`, f.env)
		_, _ = db.Exec(ctx, `DELETE FROM users WHERE id = ANY($1)`, ids)
		_, _ = lock.Exec(ctx, `SELECT pg_advisory_unlock(7101)`)
		lock.Release()
		db.Close()
	})
	return f
}

// patch is PATCH …/versions/{version_id} as this person, through the section's admin gate.
func (f *netFixture) patch(u *auth.User, env, version int64, raw string) *httptest.ResponseRecorder {
	f.t.Helper()
	body, resp := pyd.ReadBody([]byte(raw), "application/json")
	w := httptest.NewRecorder()
	if resp != nil {
		resp.Write(w)
		return w
	}
	r := httptest.NewRequest(http.MethodPatch, "/x", strings.NewReader(raw))
	r.SetPathValue("environment_id", fmt.Sprint(env))
	r.SetPathValue("version_id", fmt.Sprint(version))
	scAsAdmin(f.d, w, r, &auth.Principal{User: *u}, body, changeVersion)
	return w
}

func (f *netFixture) internet(version int64) bool {
	f.t.Helper()
	var on bool
	if err := f.db.QueryRow(f.ctx, `SELECT internet_access FROM script_environment_versions WHERE id = $1`, version).Scan(&on); err != nil {
		f.t.Fatal(err)
	}
	return on
}

func (f *netFixture) audits() []string {
	f.t.Helper()
	rows, err := f.db.Query(f.ctx, `SELECT action, coalesce(detail, '') FROM audit_logs WHERE target_type = 'script_environment'
		AND target_id = $1 ORDER BY id`, f.env)
	if err != nil {
		f.t.Fatal(err)
	}
	defer rows.Close()
	var out []string
	for rows.Next() {
		var action, detail string
		if err := rows.Scan(&action, &detail); err != nil {
			f.t.Fatal(err)
		}
		out = append(out, action+" "+detail)
	}
	return out
}

func TestOnlyAnAdministratorTakesTheInternetAwayAndTheAuditSaysSo(t *testing.T) {
	f := newNetFixture(t)
	if !f.internet(f.v1) || !f.internet(f.v2) {
		t.Fatal("a version inserted without the column has internet access")
	}
	for _, u := range []*auth.User{f.observer, f.ann} {
		if w := f.patch(u, f.env, f.v1, `{"internet_access": false}`); w.Code != http.StatusForbidden {
			t.Errorf("%s: %d %s", u.FullName, w.Code, w.Body)
		}
	}
	if !f.internet(f.v1) || len(f.audits()) != 0 {
		t.Fatal("a refused change changed something")
	}
	w := f.patch(f.admin, f.env, f.v1, `{"internet_access": false}`)
	if w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body)
	}
	var out struct {
		InternetAccess *bool `json:"internet_access"`
		Versions       []struct {
			ID             int64 `json:"id"`
			InternetAccess bool  `json:"internet_access"`
		} `json:"versions"`
	}
	if err := json.Unmarshal(w.Body.Bytes(), &out); err != nil {
		t.Fatal(err)
	}
	if out.InternetAccess == nil || *out.InternetAccess || len(out.Versions) != 2 || out.Versions[1].ID != f.v1 ||
		out.Versions[1].InternetAccess || !out.Versions[0].InternetAccess {
		t.Errorf("the answer: %s", w.Body)
	}
	if f.internet(f.v1) || !f.internet(f.v2) {
		t.Error("only the version asked for changes")
	}
	got := f.audits()
	if len(got) != 1 || !strings.HasPrefix(got[0], "scripting.version_internet ") ||
		!strings.Contains(got[0], `"version": 1`) || !strings.Contains(got[0], `"from": true`) || !strings.Contains(got[0], `"to": false`) {
		t.Errorf("the audit: %v", got)
	}
	// the same value again: nothing changes, nothing is written
	if w := f.patch(f.admin, f.env, f.v1, `{"internet_access": false}`); w.Code != http.StatusOK || len(f.audits()) != 1 {
		t.Errorf("again: %d, %v", w.Code, f.audits())
	}
	// the editor's list says it of the version in use
	r := httptest.NewRequest(http.MethodGet, "/scripting", nil)
	rec := httptest.NewRecorder()
	editorEnvironments(f.d, rec, r, &auth.Principal{User: *f.admin})
	var editor struct {
		Environments []struct {
			ID             int64 `json:"id"`
			InternetAccess bool  `json:"internet_access"`
		} `json:"environments"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &editor); err != nil {
		t.Fatal(err)
	}
	found := false
	for _, e := range editor.Environments {
		if e.ID == f.env {
			found = true
			if e.InternetAccess {
				t.Error("the editor sees internet access on a version without it")
			}
		}
	}
	if !found {
		t.Errorf("the environment is not in the editor's list: %s", rec.Body)
	}
	// back on
	if w := f.patch(f.admin, f.env, f.v1, `{"internet_access": true}`); w.Code != http.StatusOK || !f.internet(f.v1) || len(f.audits()) != 2 {
		t.Errorf("back on: %d, %v", w.Code, f.audits())
	}
}

func TestTheVersionChangeRefusesWhatIsNotOne(t *testing.T) {
	f := newNetFixture(t)
	for _, c := range []struct {
		env, version int64
		body         string
		status       int
	}{
		{f.env, f.v1, `{}`, http.StatusUnprocessableEntity},
		{f.env, f.v1, `{"internet_access": "nope"}`, http.StatusUnprocessableEntity},
		{f.env, f.v1, `{"internet_access": null}`, http.StatusUnprocessableEntity},
		{f.env, NoSuchID, `{"internet_access": false}`, http.StatusNotFound},
		{NoSuchID, f.v1, `{"internet_access": false}`, http.StatusNotFound},
	} {
		if w := f.patch(f.admin, c.env, c.version, c.body); w.Code != c.status {
			t.Errorf("%d/%d %s: %d %s", c.env, c.version, c.body, w.Code, w.Body)
		}
	}
	// a version of another environment is not found under this one
	var other int64
	if err := f.db.QueryRow(f.ctx, `INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit,
		memory_request, memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ($1, '', 'r', true, '500m', '2', '1Gi', '4Gi',
		3600, now(), now()) RETURNING id`, fmt.Sprintf("zznet-other-%d", time.Now().UnixNano())).Scan(&other); err != nil {
		t.Fatal(err)
	}
	defer f.db.Exec(f.ctx, `DELETE FROM script_environments WHERE id = $1`, other) //nolint:errcheck
	if w := f.patch(f.admin, other, f.v1, `{"internet_access": false}`); w.Code != http.StatusNotFound || !f.internet(f.v1) {
		t.Errorf("another environment's version: %d", w.Code)
	}
	if len(f.audits()) != 0 {
		t.Errorf("a refused change is not audited: %v", f.audits())
	}
}

func TestANewVersionKeepsTheInternetAccessOfTheVersionInUse(t *testing.T) {
	f := newNetFixture(t)
	inherited := func() bool {
		t.Helper()
		on, err := scInheritedInternet(f.ctx, f.db, f.env)
		if err != nil {
			t.Fatal(err)
		}
		return on
	}
	if !inherited() {
		t.Error("on, as the version in use")
	}
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environment_versions SET internet_access = false WHERE id = $1`, f.v1); err != nil {
		t.Fatal(err)
	}
	if inherited() {
		t.Error("the version in use (v1, off) counts, not the newest (v2, on)")
	}
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environments SET current_version_id = NULL WHERE id = $1`, f.env); err != nil {
		t.Fatal(err)
	}
	if !inherited() {
		t.Error("without a version in use, the newest's (v2, on)")
	}
	if on, err := scInheritedInternet(f.ctx, f.db, NoSuchID); err != nil || !on {
		t.Errorf("an environment's first version is on: %v %v", on, err)
	}
	// through the route that builds again: v3 from v2, with v1 (off) in use
	if _, err := f.db.Exec(f.ctx, `UPDATE script_environments SET current_version_id = $1 WHERE id = $2`, f.v1, f.env); err != nil {
		t.Fatal(err)
	}
	r := httptest.NewRequest(http.MethodPost, "/x", nil)
	r.SetPathValue("environment_id", fmt.Sprint(f.env))
	w := httptest.NewRecorder()
	scAsAdmin(f.d, w, r, &auth.Principal{User: *f.admin}, pyd.Body{}, rebuildEnvironment)
	if w.Code != http.StatusOK {
		t.Fatalf("rebuild: %d %s", w.Code, w.Body)
	}
	var on bool
	if err := f.db.QueryRow(f.ctx, `SELECT internet_access FROM script_environment_versions WHERE environment_id = $1 AND number = 3`,
		f.env).Scan(&on); err != nil {
		t.Fatal(err)
	}
	if on {
		t.Error("v3 got the internet back by being built again")
	}
}

func TestTheVersionRouteIsTheAdministratorsOne(t *testing.T) {
	want := [2]string{"PATCH", "/admin/scripting/environments/{environment_id}/versions/{version_id}"}
	found := false
	for _, r := range server.GoOnlyRoutes() {
		found = found || r == want
	}
	if !found {
		t.Error("the route is not declared among the Go-only ones")
	}
	for _, r := range Routes(&Deps{Config: &config.Config{}}) {
		if r.Method == want[0] && r.Pattern == want[1] {
			return
		}
	}
	t.Error("the route is not registered")
}
