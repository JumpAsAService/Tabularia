package api

import (
	"bytes"
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
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapis"
)

// The data APIs on a real database (gateway-go/scripts/test.sh): who may make and manage
// them, the checks of a definition and of the caps, the keys, and what they keep from being
// deleted. The rows are committed (the routes use the pool) and removed at the end.

type dapiFixture struct {
	t   *testing.T
	ctx context.Context
	db  *pgxpool.Pool
	d   *Deps
	// creator: MANAGE on the folder, VIEW on the secret one; manager: MANAGE on the folder
	// only; editor: EDIT; viewer: VIEW; outsider: nothing; admin; observer
	creator, manager, editor, viewer, outsider, admin, observer *auth.User
	folder, secretFolder, apiFolder                             int64
	customers, orders, secret                                   int64
}

const dapiColumns = `[{"name": "id", "dtype": "Int64"}, {"name": "name", "dtype": "String"},
	{"name": "region", "dtype": "String"}, {"name": "signup", "dtype": "Date"}, {"name": "score", "dtype": "Float64"}]`

func newDapiFixture(t *testing.T) *dapiFixture {
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
	f := &dapiFixture{t: t, ctx: ctx, db: db}
	// the auth package's tests TRUNCATE the users: one at a time
	lock, err := db.Acquire(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := lock.Exec(ctx, `SELECT pg_advisory_lock(7101)`); err != nil {
		t.Fatal(err)
	}
	log := slog.New(slog.NewTextHandler(io.Discard, nil))
	cfg := &config.Config{ScriptsEnabled: true, ScriptsNamespace: "scripts", ScriptsRegistry: "registry:5000",
		DataAPI: config.DataAPIConfig{Enabled: true, MaxAPIs: 1000, MaxTotalReplicas: 1000, KeyMaxDays: 365, KeyDefaultRate: 600}}
	f.d = &Deps{DB: db, Log: log, Config: cfg,
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
		email := fmt.Sprintf("dapi-%s-%d@x.test", name, stamp)
		id := one(`INSERT INTO users (email, full_name, hashed_password, is_active, is_superuser, is_observer, is_service, created_at)
			VALUES ($1, $2, 'x', true, $3, $4, false, now()) RETURNING id`, email, name, super, observer)
		return &auth.User{ID: id, Email: email, FullName: name, IsActive: true, IsSuperuser: super, IsObserver: observer}
	}
	f.creator, f.manager, f.editor = user("creator", false, false), user("manager", false, false), user("editor", false, false)
	f.viewer, f.outsider = user("viewer", false, false), user("outsider", false, false)
	f.admin, f.observer = user("admin", true, false), user("observer", false, true)
	folder := func(name string) int64 {
		return one(`INSERT INTO projects (name, description, created_at) VALUES ($1, '', now()) RETURNING id`, fmt.Sprintf("dapi-%s-%d", name, stamp))
	}
	f.folder, f.secretFolder, f.apiFolder = folder("main"), folder("secret"), folder("apis")
	grant := func(project int64, u *auth.User, capability string) {
		one(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, $3) RETURNING id`, project, u.ID, capability)
	}
	grant(f.folder, f.creator, "manage")
	grant(f.folder, f.manager, "manage")
	grant(f.folder, f.editor, "edit")
	grant(f.folder, f.viewer, "view")
	grant(f.folder, f.observer, "view")
	grant(f.secretFolder, f.creator, "view")
	grant(f.apiFolder, f.creator, "manage")
	ds := func(name string, project int64) int64 {
		return one(`INSERT INTO datasources (name, description, project_id, bucket, key, columns, sort_keys, column_descriptions, kind,
			created_at, updated_at) VALUES ($1, '', $2, 'b', '', $3, '[]', '{}', 'flow', now(), now()) RETURNING id`, name, project, dapiColumns)
	}
	f.customers, f.orders, f.secret = ds("Customers", f.folder), ds("Orders", f.folder), ds("Salaries", f.secretFolder)
	t.Cleanup(func() {
		ids := []int64{f.creator.ID, f.manager.ID, f.editor.ID, f.viewer.ID, f.outsider.ID, f.admin.ID, f.observer.ID}
		folders := []int64{f.folder, f.secretFolder, f.apiFolder}
		_, _ = db.Exec(ctx, `DELETE FROM audit_logs WHERE actor_id = ANY($1)`, ids)
		_, _ = db.Exec(ctx, `DELETE FROM data_apis WHERE project_id = ANY($1)`, folders)
		_, _ = db.Exec(ctx, `DELETE FROM datasources WHERE project_id = ANY($1)`, folders)
		_, _ = db.Exec(ctx, `DELETE FROM permissions WHERE project_id = ANY($1)`, folders)
		_, _ = db.Exec(ctx, `DELETE FROM projects WHERE id = ANY($1)`, folders)
		_, _ = db.Exec(ctx, `DELETE FROM users WHERE id = ANY($1)`, ids)
		_, _ = lock.Exec(ctx, `SELECT pg_advisory_unlock(7101)`)
		lock.Release()
		db.Close()
	})
	return f
}

type dapiHandler = func(*Deps, http.ResponseWriter, *http.Request, *auth.Principal)

// call runs a route's handler as this person; path holds the path's parameters.
func (f *dapiFixture) call(u *auth.User, h dapiHandler, method string, body any, path map[string]string) *httptest.ResponseRecorder {
	return f.callAs(&auth.Principal{User: *u}, h, method, body, path)
}

func (f *dapiFixture) callAs(p *auth.Principal, h dapiHandler, method string, body any, path map[string]string) *httptest.ResponseRecorder {
	var raw []byte
	if body != nil {
		raw, _ = json.Marshal(body)
	}
	r := httptest.NewRequest(method, "/x", bytes.NewReader(raw))
	for k, v := range path {
		r.SetPathValue(k, v)
	}
	w := httptest.NewRecorder()
	h(f.d, w, r, p)
	return w
}

func dapiID(n int64) string { return fmt.Sprint(n) }

func (f *dapiFixture) resource(name string, ds int64) map[string]any {
	return map[string]any{"name": name, "source": map[string]any{"kind": "datasource", "id": ds},
		"columns": []string{"id", "name", "region", "signup"},
		"filters": []map[string]any{{"column": "id", "operators": []string{"eq", "in"}},
			{"column": "signup", "operators": []string{"gte", "lte"}}},
		"default_sort": []string{"id"}, "max_page": 500}
}

type dapiDetail struct {
	ID             int64            `json:"id"`
	Name           string           `json:"name"`
	Slug           string           `json:"slug"`
	State          string           `json:"state"`
	CurrentVersion int64            `json:"current_version"`
	CanManage      bool             `json:"can_manage"`
	ResourceCount  int              `json:"resources"`
	Runtime        dataapis.Runtime `json:"runtime"`
	Suspended      string           `json:"suspended"`
	Resources      []struct {
		Name       string   `json:"name"`
		Columns    []string `json:"columns"`
		Datasource struct {
			Name     *string `json:"name"`
			Readable bool    `json:"readable"`
		} `json:"datasource"`
	} `json:"resource_list"`
}

func decode[T any](t *testing.T, w *httptest.ResponseRecorder) T {
	t.Helper()
	var v T
	if err := json.Unmarshal(w.Body.Bytes(), &v); err != nil {
		t.Fatalf("%d %s: %v", w.Code, w.Body.String(), err)
	}
	return v
}

// create makes a data API as this person in this folder, and expects `want`.
func (f *dapiFixture) create(u *auth.User, folder int64, body map[string]any, want int) (dapiDetail, *httptest.ResponseRecorder) {
	f.t.Helper()
	w := f.call(u, createDataAPI, http.MethodPost, body, map[string]string{"project_id": dapiID(folder)})
	if w.Code != want {
		f.t.Fatalf("create %v: %d %s", body["slug"], w.Code, w.Body.String())
	}
	if want != http.StatusCreated {
		return dapiDetail{}, w
	}
	return decode[dapiDetail](f.t, w), w
}

func (f *dapiFixture) slug(s string) string {
	return fmt.Sprintf("%s-%d", s, time.Now().UnixNano()%1_000_000_000)
}

func (f *dapiFixture) audited(id int64, action string) int {
	f.t.Helper()
	var n int
	if err := f.db.QueryRow(f.ctx, `SELECT count(*) FROM audit_logs WHERE target_type = 'data_api' AND target_id = $1 AND action = $2`,
		id, action).Scan(&n); err != nil {
		f.t.Fatal(err)
	}
	return n
}

func TestDataAPIsOnlyOnKubernetesAndSwitchedOn(t *testing.T) {
	f := newDapiFixture(t)
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "CRM", "slug": f.slug("crm")}, http.StatusCreated)
	for _, off := range []struct {
		reason string
		set    func(*config.Config)
	}{
		{"kubernetes", func(c *config.Config) { c.ScriptsEnabled = false }},
		{"kubernetes", func(c *config.Config) { c.ScriptsRegistry = "" }},
		{"disabled", func(c *config.Config) { c.DataAPI.Enabled = false }},
	} {
		saved := *f.d.Config
		off.set(f.d.Config)
		limits := decode[map[string]any](t, f.call(f.creator, dataAPILimits, http.MethodGet, nil, nil))
		if limits["enabled"] != false || limits["reason"] != off.reason || limits["max_apis"] != nil {
			t.Errorf("limits: %v", limits)
		}
		if w := f.call(f.creator, listProjectDataAPIs, http.MethodGet, nil, map[string]string{"project_id": dapiID(f.folder)}); w.Code != 200 ||
			strings.TrimSpace(w.Body.String()) != "[]" {
			t.Errorf("list: %d %s", w.Code, w.Body.String())
		}
		if w := f.call(f.creator, listDataAPIs, http.MethodGet, nil, nil); strings.TrimSpace(w.Body.String()) != "[]" {
			t.Errorf("list all: %s", w.Body.String())
		}
		f.create(f.creator, f.folder, map[string]any{"name": "CRM 2", "slug": f.slug("crm")}, http.StatusConflict)
		for _, h := range []dapiHandler{getDataAPI, updateDataAPI, activateDataAPI, listDataAPIKeys, createDataAPIKey, deleteDataAPI} {
			if w := f.call(f.creator, h, http.MethodPost, map[string]any{}, map[string]string{"api_id": dapiID(a.ID)}); w.Code != http.StatusConflict {
				t.Errorf("%s: %d %s", off.reason, w.Code, w.Body.String())
			}
		}
		recent, err := recentObjects(f.ctx, f.db, f.creator.ID, recentScope{admin: true, dataAPIsOn: dataAPIsOn(f.d)}, 20)
		if err != nil {
			t.Fatal(err)
		}
		for _, it := range recent {
			if it.Kind == "dataapi" {
				t.Errorf("recent while off: %+v", it)
			}
		}
		hits, err := searchHits(f.ctx, f.d, &auth.Principal{User: *f.creator}, "CRM")
		if err != nil {
			t.Fatal(err)
		}
		for _, h := range hits {
			if h.Kind == "dataapi" {
				t.Errorf("search while off: %+v", h)
			}
		}
		*f.d.Config = saved
	}
	limits := decode[map[string]any](t, f.call(f.viewer, dataAPILimits, http.MethodGet, nil, nil))
	if limits["enabled"] != true || limits["reason"] != "" || limits["key_max_days"] != float64(365) || limits["max_page"] != float64(10000) {
		t.Errorf("limits: %v", limits)
	}
	// on: in Explore's «Recent» and in the search
	recent, _ := recentObjects(f.ctx, f.db, f.creator.ID, recentScope{admin: true, dataAPIsOn: true}, 20)
	found := false
	for _, it := range recent {
		found = found || (it.Kind == "dataapi" && it.ID == a.ID)
	}
	if !found {
		t.Errorf("not in recent: %+v", recent)
	}
	hits, _ := searchHits(f.ctx, f.d, &auth.Principal{User: *f.viewer}, "CRM")
	found = false
	for _, h := range hits {
		found = found || (h.Kind == "dataapi" && int64(h.ID) == a.ID)
	}
	if !found {
		t.Errorf("not found by the search: %+v", hits)
	}
	if hits, _ := searchHits(f.ctx, f.d, &auth.Principal{User: *f.outsider}, "CRM"); len(hits) != 0 {
		t.Errorf("an outsider finds: %+v", hits)
	}
}

func TestDataAPIRights(t *testing.T) {
	f := newDapiFixture(t)
	body := func(slug string) map[string]any {
		return map[string]any{"name": slug, "slug": f.slug(slug), "resources": []any{f.resource("customers", f.customers)}}
	}
	// EDIT is not enough, nor an observer; MANAGE and an administrator are
	if _, w := f.create(f.editor, f.folder, body("ed"), http.StatusForbidden); !strings.Contains(w.Body.String(), "MANAGE") {
		t.Error(w.Body.String())
	}
	f.create(f.observer, f.folder, body("obs"), http.StatusForbidden)
	f.create(f.viewer, f.folder, body("view"), http.StatusForbidden)
	a, _ := f.create(f.creator, f.folder, body("mine"), http.StatusCreated)
	if a.State != "draft" || a.CurrentVersion != 1 || !a.CanManage || len(a.Resources) != 1 || a.ResourceCount != 1 || a.Runtime.Replicas != 1 ||
		a.Runtime.CPU != dataapis.DefaultCPU {
		t.Fatalf("%+v", a)
	}
	b, _ := f.create(f.admin, f.secretFolder, map[string]any{"name": "Pay", "slug": f.slug("pay"),
		"resources": []any{f.resource("salaries", f.secret)}}, http.StatusCreated)
	if f.audited(a.ID, "data_api.create") != 1 || f.audited(b.ID, "data_api.create") != 1 {
		t.Error("create not audited")
	}
	// VIEW sees it and may not manage it; whoever cannot see the folder gets 404
	path := map[string]string{"api_id": dapiID(a.ID)}
	if v := decode[dapiDetail](t, f.call(f.viewer, getDataAPI, http.MethodGet, nil, path)); v.CanManage || v.Name != "mine" {
		t.Errorf("viewer: %+v", v)
	}
	if w := f.call(f.outsider, getDataAPI, http.MethodGet, nil, path); w.Code != http.StatusNotFound {
		t.Errorf("outsider: %d", w.Code)
	}
	if w := f.call(f.manager, getDataAPI, http.MethodGet, nil, map[string]string{"api_id": dapiID(b.ID)}); w.Code != http.StatusNotFound {
		t.Errorf("another folder: %d", w.Code)
	}
	for _, h := range []dapiHandler{updateDataAPI, deleteDataAPI, activateDataAPI, duplicateDataAPI, listDataAPIKeys, createDataAPIKey} {
		if w := f.call(f.viewer, h, http.MethodPost, map[string]any{"name": "x"}, path); w.Code != http.StatusForbidden {
			t.Errorf("viewer: %d %s", w.Code, w.Body.String())
		}
		if w := f.call(f.editor, h, http.MethodPost, map[string]any{"name": "x"}, path); w.Code != http.StatusForbidden {
			t.Errorf("editor: %d %s", w.Code, w.Body.String())
		}
	}
	// the creator must read every datasource it publishes: the secret one is not the manager's
	if _, w := f.create(f.manager, f.folder, map[string]any{"name": "Leak", "slug": f.slug("leak"),
		"resources": []any{f.resource("salaries", f.secret)}}, http.StatusUnprocessableEntity); !strings.Contains(w.Body.String(), "may not read it") {
		t.Error(w.Body.String())
	}
	// the creator may: the API publishes it; the manager may edit the rest, not that resource
	c, _ := f.create(f.creator, f.folder, map[string]any{"name": "Mixed", "slug": f.slug("mixed"),
		"resources": []any{f.resource("salaries", f.secret), f.resource("customers", f.customers)}}, http.StatusCreated)
	cp := map[string]string{"api_id": dapiID(c.ID)}
	seen := decode[dapiDetail](t, f.call(f.manager, getDataAPI, http.MethodGet, nil, cp))
	if seen.Resources[0].Datasource.Readable || seen.Resources[0].Datasource.Name != nil || !seen.Resources[1].Datasource.Readable {
		t.Errorf("what the manager sees of the datasources: %+v", seen.Resources)
	}
	other := f.resource("customers", f.customers)
	other["columns"] = []string{"id", "name"}
	other["filters"] = []any{}
	other["default_sort"] = []any{}
	if w := f.call(f.manager, updateDataAPI, http.MethodPatch, map[string]any{"resources": []any{f.resource("salaries", f.secret), other}},
		cp); w.Code != http.StatusOK {
		t.Errorf("the manager leaving the secret resource as it is: %d %s", w.Code, w.Body.String())
	}
	changed := f.resource("salaries", f.secret)
	changed["columns"] = []string{"id", "name", "region", "signup", "score"}
	if w := f.call(f.manager, updateDataAPI, http.MethodPatch, map[string]any{"resources": []any{changed, other}}, cp); w.Code != http.StatusUnprocessableEntity ||
		!strings.Contains(w.Body.String(), "salaries") {
		t.Errorf("the manager changing the secret resource: %d %s", w.Code, w.Body.String())
	}
	// moving needs MANAGE on the target too
	if w := f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"project_id": f.secretFolder}, path); w.Code != http.StatusForbidden {
		t.Errorf("move without MANAGE on the target: %d %s", w.Code, w.Body.String())
	}
	if w := f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"project_id": f.apiFolder}, path); w.Code != http.StatusOK {
		t.Errorf("move: %d %s", w.Code, w.Body.String())
	}
	// an API token may edit, not make keys
	tok := &auth.Principal{User: *f.creator, TokenName: "ci", TokenScope: "write"}
	if w := f.callAs(tok, createDataAPIKey, http.MethodPost, map[string]any{"name": "x"}, path); w.Code != http.StatusForbidden {
		t.Errorf("a key with an API token: %d", w.Code)
	}
}

func TestDataAPIDefinitionChecks(t *testing.T) {
	f := newDapiFixture(t)
	slug := f.slug("crm")
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "CRM", "slug": slug, "description": "For the CRM",
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	f.create(f.creator, f.folder, map[string]any{"name": "CRM", "slug": f.slug("crm")}, http.StatusConflict)
	if _, w := f.create(f.creator, f.apiFolder, map[string]any{"name": "Other", "slug": slug}, http.StatusConflict); !strings.Contains(w.Body.String(), "/data/"+slug) {
		t.Error(w.Body.String())
	}
	f.create(f.creator, f.apiFolder, map[string]any{"name": "CRM", "slug": f.slug("crm")}, http.StatusCreated) // a name per folder
	for _, bad := range []struct {
		body map[string]any
		why  string
	}{
		{map[string]any{"name": "", "slug": f.slug("x")}, "needs a name"},
		{map[string]any{"name": "Bad", "slug": "Bad Slug"}, "lowercase letters"},
		{map[string]any{"name": "Bad", "slug": "-x"}, "lowercase letters"},
		{map[string]any{"name": "Bad", "slug": f.slug("x"), "resources": []any{f.resource("Customers", f.customers)}}, "lowercase"},
		{map[string]any{"name": "Bad", "slug": f.slug("x"), "resources": []any{f.resource("health", f.customers)}}, "kept for the API"},
		{map[string]any{"name": "Bad", "slug": f.slug("x"), "resources": []any{f.resource("c", f.customers), f.resource("c", f.orders)}}, "Two resources"},
		{map[string]any{"name": "Bad", "slug": f.slug("x"), "resources": []any{map[string]any{"name": "c", "source": map[string]any{"id": f.customers},
			"columns": []string{"id", "password"}}}}, "«password» is not in the datasource"},
		{map[string]any{"name": "Bad", "slug": f.slug("x"), "resources": []any{map[string]any{"name": "c", "source": map[string]any{"id": f.customers},
			"columns": []string{"id"}, "filters": []any{map[string]any{"column": "score", "operators": []string{"eq"}}}}}}, "does not go out"},
		{map[string]any{"name": "Bad", "slug": f.slug("x"), "resources": []any{map[string]any{"name": "c", "source": map[string]any{"id": f.customers},
			"columns": []string{"name"}, "filters": []any{map[string]any{"column": "name", "operators": []string{"gte"}}}}}}, "only numbers and dates"},
		{map[string]any{"name": "Bad", "slug": f.slug("x"), "resources": []any{map[string]any{"name": "c", "source": map[string]any{"id": f.customers},
			"columns": []string{"id"}, "max_page": 10001}}}, "largest page"},
		{map[string]any{"name": "Bad", "slug": f.slug("x"), "resources": []any{map[string]any{"name": "c", "source": map[string]any{"id": 999999999},
			"columns": []string{"id"}}}}, "may not read it"},
	} {
		if _, w := f.create(f.creator, f.folder, bad.body, http.StatusUnprocessableEntity); !strings.Contains(w.Body.String(), bad.why) {
			t.Errorf("%s: %s", bad.why, w.Body.String())
		}
	}
	// the most data APIs of the installation
	n, _ := dataapis.Count(f.ctx, f.db)
	f.d.Config.DataAPI.MaxAPIs = n
	if _, w := f.create(f.creator, f.folder, map[string]any{"name": "One more", "slug": f.slug("more")}, http.StatusConflict); !strings.Contains(w.Body.String(), "most data APIs") {
		t.Error(w.Body.String())
	}
	if w := f.call(f.creator, duplicateDataAPI, http.MethodPost, nil, map[string]string{"api_id": dapiID(a.ID)}); w.Code != http.StatusConflict {
		t.Errorf("duplicate past the cap: %d", w.Code)
	}
	f.d.Config.DataAPI.MaxAPIs = 1000
	// an edit is a version; the same definition again is not
	path := map[string]string{"api_id": dapiID(a.ID)}
	r2 := f.resource("customers", f.customers)
	r2["columns"] = []string{"id", "name"}
	r2["filters"] = []any{map[string]any{"column": "id", "operators": []string{"eq"}}}
	r2["default_sort"] = []string{"-id"}
	v2 := decode[dapiDetail](t, f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"resources": []any{r2}}, path))
	if v2.CurrentVersion != 2 || strings.Join(v2.Resources[0].Columns, ",") != "id,name" {
		t.Fatalf("%+v", v2)
	}
	same := decode[dapiDetail](t, f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"resources": []any{r2}, "name": "CRM API"}, path))
	if same.CurrentVersion != 2 || same.Name != "CRM API" {
		t.Errorf("a rename made a version: %+v", same)
	}
	var versions []dataAPIVersionOut
	_ = json.Unmarshal(f.call(f.viewer, listDataAPIVersions, http.MethodGet, nil, path).Body.Bytes(), &versions)
	if len(versions) != 2 || versions[0].Version != 2 || !versions[0].Current || versions[1].Note != "created" {
		t.Fatalf("%+v", versions)
	}
	restored := decode[dapiDetail](t, f.call(f.creator, restoreDataAPIVersion, http.MethodPost, nil,
		map[string]string{"api_id": dapiID(a.ID), "version": "1"}))
	if restored.CurrentVersion != 3 || len(restored.Resources[0].Columns) != 4 {
		t.Fatalf("%+v", restored)
	}
	var note string
	_ = f.db.QueryRow(f.ctx, `SELECT note FROM data_api_versions WHERE data_api_id = $1 AND version = 3`, a.ID).Scan(&note)
	if note != "restored from v1" || f.audited(a.ID, "data_api.update") != 3 {
		t.Errorf("note %q, updates %d", note, f.audited(a.ID, "data_api.update"))
	}
	if w := f.call(f.viewer, getDataAPIVersion, http.MethodGet, nil, map[string]string{"api_id": dapiID(a.ID), "version": "9"}); w.Code != http.StatusNotFound {
		t.Errorf("a version that is not: %d", w.Code)
	}
	// a copy: a draft, its own address, no keys, made by whoever copies it
	cp := decode[dapiDetail](t, f.call(f.manager, duplicateDataAPI, http.MethodPost, nil, path))
	if cp.Name != "CRM API (1)" || cp.Slug != slug+"-copy" || cp.State != "draft" || len(cp.Resources) != 1 {
		t.Errorf("copy: %+v", cp)
	}
	var creator int64
	_ = f.db.QueryRow(f.ctx, `SELECT creator_id FROM data_apis WHERE id = $1`, cp.ID).Scan(&creator)
	if creator != f.manager.ID {
		t.Errorf("the copy's creator: %d", creator)
	}
}

func TestDataAPIStatesAndCaps(t *testing.T) {
	f := newDapiFixture(t)
	empty, _ := f.create(f.creator, f.folder, map[string]any{"name": "Empty", "slug": f.slug("empty")}, http.StatusCreated)
	if w := f.call(f.creator, activateDataAPI, http.MethodPost, nil, map[string]string{"api_id": dapiID(empty.ID)}); w.Code != http.StatusConflict ||
		!strings.Contains(w.Body.String(), "at least one resource") {
		t.Errorf("activating without resources: %d %s", w.Code, w.Body.String())
	}
	if w := f.call(f.creator, pauseDataAPI, http.MethodPost, nil, map[string]string{"api_id": dapiID(empty.ID)}); w.Code != http.StatusConflict {
		t.Errorf("pausing a draft: %d", w.Code)
	}
	others, _ := dataapis.ActivePods(f.ctx, f.db, 0)
	f.d.Config.DataAPI.MaxTotalReplicas = others + 3
	rt := map[string]any{"replicas": 2, "cpu": "500m", "memory": "512Mi"}
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "A", "slug": f.slug("a"),
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	b, _ := f.create(f.creator, f.folder, map[string]any{"name": "B", "slug": f.slug("b"),
		"resources": []any{f.resource("orders", f.orders)}}, http.StatusCreated)
	pa, pb := map[string]string{"api_id": dapiID(a.ID)}, map[string]string{"api_id": dapiID(b.ID)}
	for _, p := range []map[string]string{pa, pb} {
		if w := f.call(f.admin, adminDataAPIRuntimeRoute, http.MethodPut, rt, p); w.Code != http.StatusOK {
			t.Fatalf("%d %s", w.Code, w.Body.String())
		}
	}
	if v := decode[dapiDetail](t, f.call(f.creator, activateDataAPI, http.MethodPost, nil, pa)); v.State != "active" || v.Suspended != "" {
		t.Fatalf("%+v", v)
	}
	// 2 + 2 pods past the 3 allowed
	if w := f.call(f.creator, activateDataAPI, http.MethodPost, nil, pb); w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "pods") {
		t.Errorf("past the total: %d %s", w.Code, w.Body.String())
	}
	// an active API's runtime grows only within the total
	if w := f.call(f.admin, adminDataAPIRuntimeRoute, http.MethodPut, map[string]any{"replicas": 3, "cpu": "500m",
		"memory": "512Mi"}, pa); w.Code != http.StatusOK {
		t.Errorf("within the total: %d %s", w.Code, w.Body.String())
	}
	f.d.Config.DataAPI.MaxTotalReplicas = others + 2
	if w := f.call(f.admin, adminDataAPIRuntimeRoute, http.MethodPut, map[string]any{"autoscale": true, "min_replicas": 1,
		"max_replicas": 2, "cpu": "250m", "memory": "512Mi"}, pa); w.Code != http.StatusOK {
		t.Errorf("down to the total: %d %s", w.Code, w.Body.String())
	}
	f.d.Config.DataAPI.MaxTotalReplicas = others + 3
	// the address of an active API stays
	if w := f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"slug": f.slug("moved")}, pa); w.Code != http.StatusConflict {
		t.Errorf("slug of an active API: %d", w.Code)
	}
	if v := decode[dapiDetail](t, f.call(f.creator, pauseDataAPI, http.MethodPost, nil, pa)); v.State != "paused" {
		t.Errorf("%+v", v)
	}
	if w := f.call(f.creator, activateDataAPI, http.MethodPost, nil, pb); w.Code != http.StatusOK {
		t.Errorf("once the other is paused: %d %s", w.Code, w.Body.String())
	}
	if f.audited(a.ID, "data_api.activate") != 1 || f.audited(a.ID, "data_api.pause") != 1 || f.audited(b.ID, "data_api.activate") != 1 {
		t.Error("states not audited")
	}
	// the creator's authority: without VIEW on a datasource, without MANAGE, deactivated
	_, _ = f.db.Exec(f.ctx, `UPDATE permissions SET capability = 'view' WHERE project_id = $1 AND user_id = $2`, f.folder, f.creator.ID)
	if v := decode[dapiDetail](t, f.call(f.manager, getDataAPI, http.MethodGet, nil, pa)); v.Suspended != "creator_rights" {
		t.Errorf("creator without MANAGE: %q", v.Suspended)
	}
	if w := f.call(f.manager, activateDataAPI, http.MethodPost, nil, pa); w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "MANAGE") {
		t.Errorf("activating on a creator without MANAGE: %d %s", w.Code, w.Body.String())
	}
	_, _ = f.db.Exec(f.ctx, `UPDATE users SET is_active = false WHERE id = $1`, f.creator.ID)
	if v := decode[dapiDetail](t, f.call(f.manager, getDataAPI, http.MethodGet, nil, pa)); v.Suspended != "creator_inactive" {
		t.Errorf("creator deactivated: %q", v.Suspended)
	}
	_, _ = f.db.Exec(f.ctx, `UPDATE users SET is_active = true WHERE id = $1`, f.creator.ID)
	_, _ = f.db.Exec(f.ctx, `UPDATE permissions SET capability = 'manage' WHERE project_id = $1 AND user_id = $2`, f.folder, f.creator.ID)
	sec, _ := f.create(f.creator, f.folder, map[string]any{"name": "S", "slug": f.slug("s"), "resources": []any{f.resource("s", f.secret)}}, http.StatusCreated)
	_, _ = f.db.Exec(f.ctx, `DELETE FROM permissions WHERE project_id = $1 AND user_id = $2`, f.secretFolder, f.creator.ID)
	if v := decode[dapiDetail](t, f.call(f.creator, getDataAPI, http.MethodGet, nil, map[string]string{"api_id": dapiID(sec.ID)})); v.Suspended != "datasource_rights" {
		t.Errorf("creator without VIEW on a datasource: %q", v.Suspended)
	}
	// deleted: the rows with it
	if w := f.call(f.creator, deleteDataAPI, http.MethodDelete, nil, pb); w.Code != http.StatusNoContent {
		t.Errorf("delete: %d", w.Code)
	}
	if got, _ := dataapis.Get(f.ctx, f.db, b.ID); got != nil || f.audited(b.ID, "data_api.delete") != 1 {
		t.Error("not deleted, or not audited")
	}
}

type dapiKeyAnswer struct {
	Key      dataAPIKeyOut `json:"key"`
	Secret   string        `json:"secret"`
	Replaced dataAPIKeyOut `json:"replaced"`
}

func TestDataAPIKeys(t *testing.T) {
	f := newDapiFixture(t)
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "CRM", "slug": f.slug("crm"),
		"resources": []any{f.resource("customers", f.customers), f.resource("orders", f.orders)}}, http.StatusCreated)
	path := map[string]string{"api_id": dapiID(a.ID)}
	in := func(days int) string {
		return time.Now().Add(time.Duration(days) * 24 * time.Hour).UTC().Format(time.RFC3339)
	}
	for _, bad := range []struct {
		body map[string]any
		why  string
	}{
		{map[string]any{"name": "Salesforce"}, "needs an expiry"},
		{map[string]any{"name": "Salesforce", "expires_at": in(400)}, "longest life of a key (365 days)"},
		{map[string]any{"name": "Salesforce", "expires_at": in(-1)}, "in the past"},
		{map[string]any{"name": "", "expires_at": in(30)}, "needs a name"},
		{map[string]any{"name": "Salesforce", "expires_at": in(30), "rate_per_minute": 0}, "rate limit"},
		{map[string]any{"name": "Salesforce", "expires_at": in(30), "resources": []string{"invoices"}}, "no resource «invoices»"},
		{map[string]any{"name": "Salesforce", "expires_at": in(30), "cidrs": []string{"example.com"}}, "not an address"},
	} {
		if w := f.call(f.creator, createDataAPIKey, http.MethodPost, bad.body, path); w.Code != http.StatusUnprocessableEntity ||
			!strings.Contains(w.Body.String(), bad.why) {
			t.Errorf("%s: %d %s", bad.why, w.Code, w.Body.String())
		}
	}
	w := f.call(f.creator, createDataAPIKey, http.MethodPost, map[string]any{"name": "Salesforce production", "expires_at": in(90),
		"resources": []string{"customers"}, "cidrs": []string{"203.0.113.9", "198.51.100.0/24"}}, path)
	if w.Code != http.StatusCreated {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	made := decode[dapiKeyAnswer](t, w)
	k := made.Key
	if !dataapis.WellFormedKey(made.Secret) || k.Hint != made.Secret[4:12] || k.State != "active" || k.RatePerMinute != 600 ||
		strings.Join(k.Resources, ",") != "customers" || strings.Join(k.CIDRs, ",") != "203.0.113.9/32,198.51.100.0/24" ||
		k.CreatedBy == nil || k.CreatedBy.ID != f.creator.ID {
		t.Fatalf("%+v", made)
	}
	// the database keeps the hash and the hint, never the key; no answer carries the hash
	var hash, prefix string
	_ = f.db.QueryRow(f.ctx, `SELECT hash, prefix FROM data_api_keys WHERE id = $1`, k.ID).Scan(&hash, &prefix)
	if hash != dataapis.HashKey(made.Secret) || prefix != k.Hint || strings.Contains(w.Body.String(), hash) {
		t.Fatalf("hash %q prefix %q", hash, prefix)
	}
	list := f.call(f.creator, listDataAPIKeys, http.MethodGet, nil, path)
	if strings.Contains(list.Body.String(), hash) || strings.Contains(list.Body.String(), made.Secret) ||
		!strings.Contains(list.Body.String(), "Salesforce production") {
		t.Fatalf("the list: %s", list.Body.String())
	}
	if f.audited(a.ID, "data_api.key_create") != 1 {
		t.Error("key_create not audited")
	}
	var detail string
	_ = f.db.QueryRow(f.ctx, `SELECT detail FROM audit_logs WHERE target_type = 'data_api' AND target_id = $1 AND action = 'data_api.key_create'`,
		a.ID).Scan(&detail)
	if strings.Contains(detail, hash) || strings.Contains(detail, made.Secret) {
		t.Errorf("the audit holds the key: %s", detail)
	}
	if w := f.call(f.viewer, listDataAPIKeys, http.MethodGet, nil, path); w.Code != http.StatusForbidden {
		t.Errorf("a viewer lists the keys: %d", w.Code)
	}
	// renewed with a grace: a new key, the old one answering until then
	kp := map[string]string{"api_id": dapiID(a.ID), "key_id": dapiID(k.ID)}
	w = f.call(f.creator, renewDataAPIKey, http.MethodPost, map[string]any{"expires_at": in(120), "grace_hours": 24}, kp)
	if w.Code != http.StatusCreated {
		t.Fatalf("renew: %d %s", w.Code, w.Body.String())
	}
	renewed := decode[dapiKeyAnswer](t, w)
	if renewed.Secret == made.Secret || !dataapis.WellFormedKey(renewed.Secret) || renewed.Key.Name != "Salesforce production" ||
		strings.Join(renewed.Key.CIDRs, ",") != "203.0.113.9/32,198.51.100.0/24" || renewed.Key.State != "active" ||
		renewed.Replaced.State != "grace" || renewed.Replaced.GraceUntil == nil {
		t.Fatalf("%+v", renewed)
	}
	grace, _ := time.Parse(time.RFC3339Nano, *renewed.Replaced.GraceUntil)
	if d := time.Until(grace); d < 23*time.Hour || d > 25*time.Hour {
		t.Errorf("grace until %v", grace)
	}
	if w := f.call(f.creator, renewDataAPIKey, http.MethodPost, map[string]any{"expires_at": in(120)}, kp); w.Code != http.StatusConflict {
		t.Errorf("renewed twice: %d", w.Code)
	}
	if w := f.call(f.creator, renewDataAPIKey, http.MethodPost, map[string]any{"expires_at": in(120), "grace_hours": 10000},
		map[string]string{"api_id": dapiID(a.ID), "key_id": dapiID(renewed.Key.ID)}); w.Code != http.StatusUnprocessableEntity {
		t.Errorf("a grace too long: %d", w.Code)
	}
	// renewed with no grace: the old one is revoked at once
	np := map[string]string{"api_id": dapiID(a.ID), "key_id": dapiID(renewed.Key.ID)}
	again := decode[dapiKeyAnswer](t, f.call(f.creator, renewDataAPIKey, http.MethodPost, map[string]any{"expires_at": in(120)}, np))
	if again.Replaced.State != "revoked" || again.Key.State != "active" {
		t.Errorf("%+v", again)
	}
	if f.audited(a.ID, "data_api.key_renew") != 2 {
		t.Error("key_renew not audited")
	}
	// revoked: once in the audit, and not renewable; another API's key is not found here
	lp := map[string]string{"api_id": dapiID(a.ID), "key_id": dapiID(again.Key.ID)}
	for i := 0; i < 2; i++ {
		if w := f.call(f.creator, revokeDataAPIKey, http.MethodPost, nil, lp); w.Code != http.StatusNoContent {
			t.Errorf("revoke: %d %s", w.Code, w.Body.String())
		}
	}
	if f.audited(a.ID, "data_api.key_revoke") != 1 {
		t.Errorf("key_revoke audited %d times", f.audited(a.ID, "data_api.key_revoke"))
	}
	if w := f.call(f.creator, renewDataAPIKey, http.MethodPost, map[string]any{"expires_at": in(30)}, lp); w.Code != http.StatusConflict {
		t.Errorf("renewing a revoked key: %d", w.Code)
	}
	if w := f.call(f.editor, revokeDataAPIKey, http.MethodPost, nil, lp); w.Code != http.StatusForbidden {
		t.Errorf("an editor revokes: %d", w.Code)
	}
	if w := f.call(f.outsider, revokeDataAPIKey, http.MethodPost, nil, lp); w.Code != http.StatusNotFound {
		t.Errorf("an outsider revokes: %d", w.Code)
	}
	b, _ := f.create(f.creator, f.folder, map[string]any{"name": "Other", "slug": f.slug("other")}, http.StatusCreated)
	if w := f.call(f.creator, revokeDataAPIKey, http.MethodPost, nil, map[string]string{"api_id": dapiID(b.ID), "key_id": dapiID(k.ID)}); w.Code != http.StatusNotFound {
		t.Errorf("a key through another API: %d", w.Code)
	}
	// a key can be revoked even where the data APIs are switched off
	fresh := decode[dapiKeyAnswer](t, f.call(f.creator, createDataAPIKey, http.MethodPost, map[string]any{"name": "Web", "expires_at": in(10)}, path))
	f.d.Config.DataAPI.Enabled = false
	if w := f.call(f.creator, revokeDataAPIKey, http.MethodPost, nil, map[string]string{"api_id": dapiID(a.ID), "key_id": dapiID(fresh.Key.ID)}); w.Code != http.StatusNoContent {
		t.Errorf("revoke while off: %d %s", w.Code, w.Body.String())
	}
	f.d.Config.DataAPI.Enabled = true
	// the default rate follows the setting; the key's resources are [] = all
	f.d.Config.DataAPI.KeyDefaultRate = 42
	other := decode[dapiKeyAnswer](t, f.call(f.admin, createDataAPIKey, http.MethodPost, map[string]any{"name": "ERP", "expires_at": in(10)}, path))
	if other.Key.RatePerMinute != 42 || len(other.Key.Resources) != 0 || len(other.Key.CIDRs) != 0 {
		t.Errorf("%+v", other.Key)
	}
	// the data API deleted: its keys with it
	f.call(f.creator, deleteDataAPI, http.MethodDelete, nil, path)
	var n int
	_ = f.db.QueryRow(f.ctx, `SELECT count(*) FROM data_api_keys WHERE data_api_id = $1`, a.ID).Scan(&n)
	if n != 0 {
		t.Errorf("%d keys left", n)
	}
}

func TestDataAPIKeepsItsDatasourceAndFolder(t *testing.T) {
	f := newDapiFixture(t)
	a, _ := f.create(f.creator, f.apiFolder, map[string]any{"name": "Published", "slug": f.slug("pub"),
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	dsPath := func(id int64) *http.Request {
		r := httptest.NewRequest(http.MethodDelete, "/x", nil)
		r.SetPathValue("ds_id", dapiID(id))
		return r
	}
	w := httptest.NewRecorder()
	deleteDatasource(f.d)(w, dsPath(f.customers), &auth.Principal{User: *f.creator})
	if w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "published by data APIs ('Published')") {
		t.Errorf("deleting a published datasource: %d %s", w.Code, w.Body.String())
	}
	// another datasource goes as before
	w = httptest.NewRecorder()
	deleteDatasource(f.d)(w, dsPath(f.orders), &auth.Principal{User: *f.creator})
	if w.Code != http.StatusNoContent {
		t.Errorf("deleting another datasource: %d %s", w.Code, w.Body.String())
	}
	refusal, err := removeFolder(f.ctx, f.d, f.creator, f.apiFolder)
	if err != nil || refusal == nil || refusal.Status != http.StatusConflict || !strings.Contains(refusal.Detail, "data APIs") {
		t.Errorf("deleting the folder: %+v %v", refusal, err)
	}
	// taken out of the API, the datasource can go
	if w := f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"resources": []any{}}, map[string]string{"api_id": dapiID(a.ID)}); w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	w = httptest.NewRecorder()
	deleteDatasource(f.d)(w, dsPath(f.customers), &auth.Principal{User: *f.creator})
	if w.Code != http.StatusNoContent {
		t.Errorf("deleting it once taken out: %d %s", w.Code, w.Body.String())
	}
}

// The runtime is the administrator's (the user's decision of 2026-11-07): the creator cannot
// change it, sending it back as it is passes; an administrator sets it on any data API, past
// what one data API could ask before, within the bounds and the most pods of all the active
// ones; the audit has it with before and after; a copy starts again from the default.
func TestDataAPIRuntimeIsTheAdministrators(t *testing.T) {
	f := newDapiFixture(t)
	big := map[string]any{"replicas": 6, "cpu": "4", "memory": "8Gi"}
	code := func(w *httptest.ResponseRecorder) string {
		return decode[struct {
			Code string `json:"code"`
		}](t, w).Code
	}
	if _, w := f.create(f.creator, f.folder, map[string]any{"name": "Mine", "slug": f.slug("rt"), "runtime": big},
		http.StatusForbidden); code(w) != "runtime_admin_only" {
		t.Fatalf("a new one with its own runtime: %s", w.Body.String())
	}
	// the default as the page has it, spelled otherwise: the same
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "Runtime", "slug": f.slug("rt"),
		"runtime":   map[string]any{"replicas": 1, "min_replicas": 1, "max_replicas": 1, "cpu": "0.25", "memory": "256Mi"},
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	if !dataapis.SameRuntime(a.Runtime, dataapis.DefaultRuntime()) || a.Runtime.CPU != dataapis.DefaultCPU {
		t.Fatalf("%+v", a.Runtime)
	}
	path := map[string]string{"api_id": dapiID(a.ID)}
	if w := f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"runtime": big}, path); w.Code != http.StatusForbidden ||
		code(w) != "runtime_admin_only" {
		t.Fatalf("the creator changing it: %d %s", w.Code, w.Body.String())
	}
	if w := f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"name": "Runtime 2", "runtime": a.Runtime}, path); w.Code != http.StatusOK {
		t.Fatalf("the page sending it back: %d %s", w.Code, w.Body.String())
	}
	// who may set it: an administrator only, not an observer, not whoever manages the folder
	for _, u := range []*auth.User{f.creator, f.manager, f.observer, f.viewer} {
		if w := f.call(u, adminDataAPIRuntimeRoute, http.MethodPut, big, path); w.Code != http.StatusForbidden {
			t.Errorf("%s: %d", u.FullName, w.Code)
		}
	}
	for u, want := range map[*auth.User]bool{f.admin: true, f.creator: false, f.observer: false} {
		if got := decode[struct {
			Can bool `json:"can_set_runtime"`
		}](t, f.call(u, getDataAPI, http.MethodGet, nil, path)); got.Can != want {
			t.Errorf("%s: can_set_runtime %v", u.FullName, got.Can)
		}
	}
	// an administrator, on someone else's data API, past the chart's old caps (3 pods, 1 CPU, 1Gi)
	w := f.call(f.admin, adminDataAPIRuntimeRoute, http.MethodPut, map[string]any{"replicas": 6, "cpu": " 4 ", "memory": "8Gi"}, path)
	if w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if got := decode[dataapis.Runtime](t, w); got.Replicas != 6 || got.MinReplicas != 6 || got.CPU != "4" || got.Memory != "8Gi" {
		t.Fatalf("%+v", got)
	}
	v := decode[dapiDetail](t, f.call(f.creator, getDataAPI, http.MethodGet, nil, path))
	if v.Runtime.Replicas != 6 || v.Runtime.Memory != "8Gi" || v.CurrentVersion != 1 {
		t.Fatalf("not a version, and the page has it: %+v", v)
	}
	var before, after, by string
	if err := f.db.QueryRow(f.ctx, `SELECT detail::json->'before'->>'replicas', detail::json->'after'->>'memory', detail::json->>'by'
		FROM audit_logs WHERE target_type = 'data_api' AND target_id = $1 AND action = 'data_api.runtime' AND actor_id = $2`,
		a.ID, f.admin.ID).Scan(&before, &after, &by); err != nil || before != "1" || after != "8Gi" || by != "administration" {
		t.Fatalf("the audit: %v %q %q %q", err, before, after, by)
	}
	// the same again: answered, not recorded twice
	if w := f.call(f.admin, adminDataAPIRuntimeRoute, http.MethodPut, map[string]any{"replicas": 6, "cpu": "4000m", "memory": "8192Mi"}, path); w.Code != http.StatusOK ||
		f.audited(a.ID, "data_api.runtime") != 1 {
		t.Errorf("the same runtime: %d, audited %d", w.Code, f.audited(a.ID, "data_api.runtime"))
	}
	// the creator's page, opened before: its old runtime is now a change, refused; the new one passes
	if w := f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"runtime": dataapis.DefaultRuntime()}, path); w.Code != http.StatusForbidden {
		t.Errorf("the old runtime sent back: %d", w.Code)
	}
	r2 := f.resource("customers", f.customers)
	r2["columns"] = []string{"id", "name"}
	r2["filters"] = []any{map[string]any{"column": "id", "operators": []string{"eq"}}}
	if w := f.call(f.creator, updateDataAPI, http.MethodPatch, map[string]any{"resources": []any{r2}, "runtime": v.Runtime}, path); w.Code != http.StatusOK {
		t.Fatalf("an edit with the runtime as it is: %d %s", w.Code, w.Body.String())
	}
	// a version restored keeps the administrator's runtime
	restored := decode[dapiDetail](t, f.call(f.creator, restoreDataAPIVersion, http.MethodPost, nil,
		map[string]string{"api_id": dapiID(a.ID), "version": "1"}))
	if restored.CurrentVersion != 3 || restored.Runtime.Replicas != 6 || restored.Runtime.CPU != "4" {
		t.Fatalf("restored: %+v", restored)
	}
	// not valid, whoever asks
	f.d.Config.DataAPI.MaxTotalReplicas = 5
	for _, bad := range []struct {
		rt  map[string]any
		why string
	}{
		{map[string]any{"replicas": 1, "cpu": "lots", "memory": "1Gi"}, "not a CPU quantity"},
		{map[string]any{"replicas": 1, "cpu": "1", "memory": "1GB of RAM"}, "not a memory quantity"},
		{map[string]any{"replicas": 1, "cpu": "50m", "memory": "1Gi"}, "at least 100m"},
		{map[string]any{"replicas": 1, "cpu": "1", "memory": "128Mi"}, "at least 256Mi"},
		{map[string]any{"replicas": 1, "cpu": "32", "memory": "1Gi"}, "at most 16"},
		{map[string]any{"replicas": 1, "cpu": "1", "memory": "128Gi"}, "at most 64Gi"},
		{map[string]any{"replicas": 0, "cpu": "1", "memory": "1Gi"}, "at least 1 replica"},
		{map[string]any{"replicas": 6, "cpu": "1", "memory": "1Gi"}, "at most 5 replicas"},
		{map[string]any{"autoscale": true, "min_replicas": 2, "max_replicas": 9, "cpu": "1", "memory": "1Gi"}, "at most 5 replicas"},
	} {
		w := f.call(f.admin, adminDataAPIRuntimeRoute, http.MethodPut, bad.rt, path)
		if w.Code != http.StatusUnprocessableEntity || !strings.Contains(w.Body.String(), bad.why) {
			t.Errorf("%v: %d %s", bad.rt, w.Code, w.Body.String())
		}
	}
	// active: within the most pods of all the active ones together
	if w := f.call(f.admin, adminDataAPIRuntimeRoute, http.MethodPut, map[string]any{"replicas": 2, "cpu": "500m", "memory": "512Mi"}, path); w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if w := f.call(f.creator, activateDataAPI, http.MethodPost, nil, path); w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	// another active one with two pods, so the total is past the pods of one
	b, _ := f.create(f.manager, f.folder, map[string]any{"name": "Other", "slug": f.slug("rt"),
		"resources": []any{f.resource("orders", f.orders)}}, http.StatusCreated)
	pb := map[string]string{"api_id": dapiID(b.ID)}
	if w := f.call(f.admin, adminDataAPIRuntimeRoute, http.MethodPut, map[string]any{"replicas": 2, "cpu": "250m", "memory": "256Mi"}, pb); w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if w := f.call(f.manager, activateDataAPI, http.MethodPost, nil, pb); w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	others, _ := dataapis.ActivePods(f.ctx, f.db, a.ID)
	f.d.Config.DataAPI.MaxTotalReplicas = others + 2
	if w := f.call(f.admin, adminDataAPIRuntimeRoute, http.MethodPut, map[string]any{"autoscale": true, "min_replicas": 1, "max_replicas": 3,
		"cpu": "500m", "memory": "1Gi"}, path); w.Code != http.StatusConflict || !strings.Contains(w.Body.String(), "pods together") {
		t.Errorf("past the total: %d %s", w.Code, w.Body.String())
	}
	if w := f.call(f.admin, adminDataAPIRuntimeRoute, http.MethodPut, map[string]any{"replicas": 2, "cpu": "1", "memory": "2Gi"}, path); w.Code != http.StatusOK {
		t.Errorf("within the total: %d %s", w.Code, w.Body.String())
	}
	// a paused one is not counted: its runtime may wait for the others
	if w := f.call(f.manager, pauseDataAPI, http.MethodPost, nil, pb); w.Code != http.StatusOK {
		t.Fatal(w.Code)
	}
	if w := f.call(f.admin, adminDataAPIRuntimeRoute, http.MethodPut, map[string]any{"replicas": 4, "cpu": "250m", "memory": "256Mi"}, pb); w.Code != http.StatusOK {
		t.Errorf("a paused one: %d %s", w.Code, w.Body.String())
	}
	f.d.Config.DataAPI.MaxTotalReplicas = 1000
	// a copy starts from the default
	cp := decode[dapiDetail](t, f.call(f.manager, duplicateDataAPI, http.MethodPost, nil, path))
	if !dataapis.SameRuntime(cp.Runtime, dataapis.DefaultRuntime()) {
		t.Errorf("the copy's runtime: %+v", cp.Runtime)
	}
	// with the feature off, nothing to set
	f.d.Config.DataAPI.Enabled = false
	if w := f.call(f.admin, adminDataAPIRuntimeRoute, http.MethodPut, big, path); w.Code != http.StatusConflict {
		t.Errorf("switched off: %d", w.Code)
	}
	f.d.Config.DataAPI.Enabled = true
	if w := f.call(f.admin, adminDataAPIRuntimeRoute, http.MethodPut, big, map[string]string{"api_id": "999999999"}); w.Code != http.StatusNotFound {
		t.Errorf("not there: %d", w.Code)
	}
}
