package api

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"reflect"
	"strconv"
	"strings"
	"testing"

	"github.com/JumpAsAService/Tabularia/gateway-go/internal/auth"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapis"
	"github.com/JumpAsAService/Tabularia/gateway-go/internal/dataapiwire"
)

// A data API's documentation, its place in Administration and in the lineage, on a real
// database (gateway-go/scripts/test.sh).

type dapiDoc struct {
	State     string `json:"state"`
	BaseURL   string `json:"base_url"`
	Host      string `json:"host"`
	Path      string `json:"path"`
	Version   int64  `json:"version"`
	Resources []struct {
		Name    string `json:"name"`
		Path    string `json:"path"`
		Columns []struct {
			Name, Kind string
		} `json:"columns"`
		Filters []dataapiwire.Filter `json:"filters"`
		MaxPage int                  `json:"max_page"`
	} `json:"resources"`
	MaxInValues int                     `json:"max_in_values"`
	Errors      []dataapiwire.ErrorCode `json:"errors"`
	OpenAPI     map[string]any          `json:"openapi"`
}

func TestDataAPIDocumentation(t *testing.T) {
	f := newDapiFixture(t)
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "Docs", "slug": f.slug("docs"),
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	path := map[string]string{"api_id": dapiID(a.ID)}
	// VIEW on the folder is enough, with no key; who does not see the data API does not see it
	for _, u := range []*userRef{{f.viewer, http.StatusOK}, {f.observer, http.StatusOK}, {f.admin, http.StatusOK},
		{f.outsider, http.StatusNotFound}} {
		if w := f.call(u.u, dataAPIDocumentation, http.MethodGet, nil, path); w.Code != u.want {
			t.Fatalf("%s: %d %s", u.u.FullName, w.Code, w.Body.String())
		}
	}
	w := f.call(f.viewer, dataAPIDocumentation, http.MethodGet, nil, path)
	doc := decode[dapiDoc](t, w)
	if doc.State != "draft" || doc.Path != "/data/"+a.Slug+"/" || doc.Version != 1 || doc.BaseURL != "" || doc.MaxInValues != 500 ||
		len(doc.Resources) != 1 || doc.Resources[0].Path != doc.Path+"customers" || doc.Resources[0].MaxPage != 500 {
		t.Fatalf("%+v", doc)
	}
	kinds := map[string]string{}
	for _, c := range doc.Resources[0].Columns {
		kinds[c.Name] = c.Kind
	}
	if !reflect.DeepEqual(kinds, map[string]string{"id": "integer", "name": "string", "region": "string", "signup": "date"}) {
		t.Errorf("%v", kinds)
	}
	codes := map[string]bool{}
	for _, e := range doc.Errors {
		codes[e.Code] = true
	}
	for _, c := range []string{"key_missing", "data_changed", "rate_limited", "loading", "bad_value"} {
		if !codes[c] {
			t.Errorf("no %s", c)
		}
	}
	text := w.Body.String()
	for _, want := range []string{`"name":"id__in"`, `"name":"signup__gte"`, `"/data/` + a.Slug + `/customers"`, `"openapi":"3.1.0"`,
		`"servers":[{"url":"/"}]`} {
		if !strings.Contains(text, want) {
			t.Errorf("no %s", want)
		}
	}
	if strings.Contains(text, "tdk_") && strings.Contains(text, "hash") {
		t.Error("keys in the documentation")
	}
	// the description is the one the pods make from their manifest: the same generator, the same
	// resources (the pods' own test shows they type the columns as the gateway does)
	if _, err := f.db.Exec(f.ctx, `UPDATE data_apis SET state = 'active' WHERE id = $1`, a.ID); err != nil {
		t.Fatal(err)
	}
	stored, _ := dataapis.Get(f.ctx, f.db, a.ID)
	m, err := buildManifest(f.ctx, f.d, stored)
	if err != nil {
		t.Fatal(err)
	}
	if len(m.Resources) != 1 || m.DefinitionVersion != 1 {
		t.Fatalf("%+v", m)
	}
	polars := map[string]string{}
	var cols []struct{ Name, Dtype string }
	_ = json.Unmarshal([]byte(dapiColumns), &cols)
	for _, c := range cols {
		polars[c.Name] = c.Dtype
	}
	fromPods := dataapiwire.OpenAPI(dataapiwire.Doc{Name: m.Name, Slug: m.Slug, Version: strconv.FormatInt(m.DefinitionVersion, 10),
		Resources: m.Resources}, nil, func(_, col string) (string, bool) {
		typ, ok := polars[col]
		return dataapiwire.KindOfPolars(typ), ok
	}, "/")
	var want, got any
	raw, _ := json.Marshal(fromPods)
	_ = json.Unmarshal(raw, &want)
	doc = decode[dapiDoc](t, f.call(f.viewer, dataAPIDocumentation, http.MethodGet, nil, path))
	raw, _ = json.Marshal(doc.OpenAPI)
	_ = json.Unmarshal(raw, &got)
	if !reflect.DeepEqual(want, got) {
		t.Fatalf("the page's description is not the pods':\n%s", raw)
	}
	// a public host: the address the documentation writes
	f.d.Config.DataAPI.Pods.PublicBaseURL, f.d.Config.DataAPI.Pods.IngressHost = "https://api.example.com", "api.example.com"
	defer func() { f.d.Config.DataAPI.Pods.PublicBaseURL, f.d.Config.DataAPI.Pods.IngressHost = "", "" }()
	w = f.call(f.viewer, dataAPIDocumentation, http.MethodGet, nil, path)
	doc = decode[dapiDoc](t, w)
	if doc.BaseURL != "https://api.example.com" || doc.Host != "api.example.com" ||
		!strings.Contains(w.Body.String(), `"servers":[{"url":"https://api.example.com"}]`) {
		t.Errorf("%s", w.Body.String())
	}
	st := decode[map[string]any](t, f.call(f.viewer, dataAPIStatus, http.MethodGet, nil, path))
	if st["base_url"] != "https://api.example.com" || st["host"] != "api.example.com" {
		t.Errorf("%v", st)
	}
	lim := decode[map[string]any](t, f.call(f.viewer, dataAPILimits, http.MethodGet, nil, nil))
	if lim["base_url"] != "https://api.example.com" || lim["host"] != "api.example.com" {
		t.Errorf("%v", lim)
	}
}

type userRef struct {
	u    *auth.User
	want int
}

func TestDataAPIAdministration(t *testing.T) {
	f := newDapiFixture(t)
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "Admin one", "slug": f.slug("adm"),
		"resources": []any{f.resource("customers", f.customers)}}, http.StatusCreated)
	b, _ := f.create(f.manager, f.folder, map[string]any{"name": "Admin two", "slug": f.slug("adm"),
		"resources": []any{f.resource("orders", f.orders)}}, http.StatusCreated)
	path := map[string]string{"api_id": dapiID(a.ID)}
	if w := f.call(f.creator, activateDataAPI, http.MethodPost, nil, path); w.Code != http.StatusOK {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	// the list: an administrator or an observer, nobody else (not even the creators)
	for _, u := range []*userRef{{f.creator, http.StatusForbidden}, {f.viewer, http.StatusForbidden}, {f.observer, http.StatusOK},
		{f.admin, http.StatusOK}} {
		if w := f.call(u.u, adminListDataAPIsRoute, http.MethodGet, nil, nil); w.Code != u.want {
			t.Fatalf("%s: %d %s", u.u.FullName, w.Code, w.Body.String())
		}
	}
	type row struct {
		ID      int64  `json:"id"`
		State   string `json:"state"`
		Creator *struct {
			Email  string `json:"email"`
			Active bool   `json:"active"`
		} `json:"creator"`
		Resources int   `json:"resources"`
		Keys      int   `json:"keys"`
		Requests  int64 `json:"requests_24h"`
	}
	list := decode[struct {
		Enabled bool  `json:"enabled"`
		APIs    []row `json:"apis"`
	}](t, f.call(f.admin, adminListDataAPIsRoute, http.MethodGet, nil, nil))
	seen := map[int64]row{}
	for _, r := range list.APIs {
		seen[r.ID] = r
	}
	if !list.Enabled || seen[a.ID].State != "active" || seen[a.ID].Creator == nil || seen[a.ID].Creator.Email != f.creator.Email ||
		!seen[a.ID].Creator.Active || seen[a.ID].Resources != 1 || seen[b.ID].State != "draft" || seen[b.ID].Creator.Email != f.manager.Email {
		t.Fatalf("%+v", list)
	}
	// an observer reads, does not act; an administrator pauses and deletes any data API
	if w := f.call(f.observer, adminPauseDataAPIRoute, http.MethodPost, nil, path); w.Code != http.StatusForbidden {
		t.Fatalf("%d", w.Code)
	}
	if w := f.call(f.creator, adminDeleteDataAPIRoute, http.MethodDelete, nil, path); w.Code != http.StatusForbidden {
		t.Fatalf("%d", w.Code)
	}
	if w := f.call(f.admin, adminPauseDataAPIRoute, http.MethodPost, nil, path); w.Code != http.StatusNoContent {
		t.Fatalf("%d %s", w.Code, w.Body.String())
	}
	if got, _ := dataapis.Get(f.ctx, f.db, a.ID); got.State != dataapis.Paused {
		t.Fatalf("%v", got.State)
	}
	if w := f.call(f.admin, adminPauseDataAPIRoute, http.MethodPost, nil, path); w.Code != http.StatusConflict {
		t.Fatalf("paused twice: %d", w.Code)
	}
	var by string
	if err := f.db.QueryRow(f.ctx, `SELECT detail::json->>'by' FROM audit_logs WHERE target_type = 'data_api' AND target_id = $1
		AND action = 'data_api.pause' AND actor_id = $2`, a.ID, f.admin.ID).Scan(&by); err != nil || by != "administration" {
		t.Fatalf("%v %q", err, by)
	}
	// even with the feature switched off, what is left can be deleted
	f.d.Config.DataAPI.Enabled = false
	defer func() { f.d.Config.DataAPI.Enabled = true }()
	off := decode[struct {
		Enabled bool   `json:"enabled"`
		Reason  string `json:"reason"`
		APIs    []row  `json:"apis"`
	}](t, f.call(f.admin, adminListDataAPIsRoute, http.MethodGet, nil, nil))
	if off.Enabled || off.Reason != "disabled" || len(off.APIs) < 2 {
		t.Fatalf("%+v", off)
	}
	for _, id := range []int64{a.ID, b.ID} {
		if w := f.call(f.admin, adminDeleteDataAPIRoute, http.MethodDelete, nil, map[string]string{"api_id": dapiID(id)}); w.Code != http.StatusNoContent {
			t.Fatalf("%d %s", w.Code, w.Body.String())
		}
		if got, _ := dataapis.Get(f.ctx, f.db, id); got != nil {
			t.Fatal("not deleted")
		}
		if f.audited(id, "data_api.delete") != 1 {
			t.Error("not audited")
		}
	}
	if w := f.call(f.admin, adminDeleteDataAPIRoute, http.MethodDelete, nil, path); w.Code != http.StatusNotFound {
		t.Fatalf("%d", w.Code)
	}
}

// A data API reads the datasources it publishes: the lineage graph says so, to whoever sees it.
func TestDataAPIInTheLineage(t *testing.T) {
	f := newDapiFixture(t)
	a, _ := f.create(f.creator, f.folder, map[string]any{"name": "Lineage", "slug": f.slug("lin"),
		"resources": []any{f.resource("customers", f.customers), f.resource("orders", f.orders)}}, http.StatusCreated)
	graph := func(u *auth.User, query string) (int, map[string]any) {
		r := httptest.NewRequest(http.MethodGet, "/lineage?"+query, nil)
		w := httptest.NewRecorder()
		getLineage(f.d)(w, r, &auth.Principal{User: *u})
		var out map[string]any
		_ = json.Unmarshal(w.Body.Bytes(), &out)
		return w.Code, out
	}
	node := "dataapi:" + dapiID(a.ID)
	code, g := graph(f.viewer, "")
	if code != http.StatusOK {
		t.Fatal(code)
	}
	var found map[string]any
	for _, n := range g["nodes"].([]any) {
		if n.(map[string]any)["id"] == node {
			found = n.(map[string]any)
		}
	}
	if found == nil || found["type"] != "data_api" || found["label"] != "Lineage" ||
		found["meta"].(map[string]any)["path"] != "/data/"+a.Slug+"/" || found["meta"].(map[string]any)["state"] != "draft" {
		t.Fatalf("%v", found)
	}
	edges := map[string]bool{}
	for _, e := range g["edges"].([]any) {
		m := e.(map[string]any)
		if m["target"] == node {
			edges[m["source"].(string)+" "+m["kind"].(string)] = true
		}
	}
	if !edges["ds:"+dapiID(f.customers)+" read"] || !edges["ds:"+dapiID(f.orders)+" read"] || len(edges) != 2 {
		t.Errorf("%v", edges)
	}
	// centred on the datasource: the data API is downstream, as a dependent of it
	code, g = graph(f.viewer, "type=datasource&id="+dapiID(f.customers)+"&direction=downstream&depth=1")
	if code != http.StatusOK || !strings.Contains(mustJSON(g), `"`+node+`"`) {
		t.Errorf("%d %v", code, g)
	}
	// centred on the data API
	if code, _ = graph(f.viewer, "type=dataapi&id="+dapiID(a.ID)); code != http.StatusOK {
		t.Errorf("%d", code)
	}
	// who does not see the folder does not see it
	if _, g = graph(f.outsider, ""); strings.Contains(mustJSON(g), node) {
		t.Error("an outsider sees the data API")
	}
}

func mustJSON(v any) string { raw, _ := json.Marshal(v); return string(raw) }
