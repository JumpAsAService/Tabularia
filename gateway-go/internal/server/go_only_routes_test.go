package server

import (
	"regexp"
	"testing"
)

// A Go-only route sits right after its FastAPI neighbour and is served as the others are.
func TestTheGoOnlyRoutesTakeTheirPlace(t *testing.T) {
	table := mainRoutes()
	if len(table) != len(fastAPIRoutes)+len(goOnlyRoutes) {
		t.Fatalf("table of %d routes", len(table))
	}
	for _, g := range goOnlyRoutes {
		if _, err := regexp.Compile(goRegex(g.route.regex)); err != nil {
			t.Errorf("%s: %v", g.route.path, err)
		}
		for i, p := range table {
			if p.path == g.route.path && (i == 0 || table[i-1].path != g.after) {
				t.Errorf("%s is not right after %s", g.route.path, g.after)
			}
		}
	}
	h := New([]Route{
		{Method: "GET", Pattern: "/users/me/flow-shortcuts", Handler: mark("get")},
		{Method: "PUT", Pattern: "/users/me/flow-shortcuts", Handler: mark("put")},
	}, Options{})
	if rec := serve(h, "GET", "/users/me/flow-shortcuts", nil); rec.Code != 200 || rec.Body.String() != "get " {
		t.Errorf("GET: %d %q", rec.Code, rec.Body.String())
	}
	if rec := serve(h, "PATCH", "/users/me/flow-shortcuts", nil); rec.Code != 405 || rec.Header().Get("Allow") != "GET, PUT" {
		t.Errorf("PATCH: %d %v", rec.Code, rec.Header())
	}
}

// The data APIs' routes reach their own handlers: «limits» is not an id, a folder's list is not
// a folder.
func TestTheDataAPIRoutesReachTheirHandlers(t *testing.T) {
	var routes []Route
	for _, r := range [][3]string{
		{"GET", "/data-apis/limits", "limits"}, {"GET", "/data-apis/{api_id}", "get"}, {"PATCH", "/data-apis/{api_id}", "patch"},
		{"GET", "/projects/{project_id}/data-apis", "folder"}, {"POST", "/data-apis/{api_id}/keys/{key_id}/renew", "renew"},
		{"GET", "/data-apis/{api_id}/versions/{version}", "version"},
		{"GET", "/internal/data-apis/{api_id}/manifest", "manifest"}, {"POST", "/internal/data-apis/{api_id}/usage", "usage"},
	} {
		routes = append(routes, Route{Method: r[0], Pattern: r[1], Handler: mark(r[2])})
	}
	h := New(routes, Options{})
	for _, c := range [][3]string{
		{"GET", "/data-apis/limits", "limits "}, {"GET", "/data-apis/7", "get "}, {"PATCH", "/data-apis/7", "patch "},
		{"GET", "/projects/3/data-apis", "folder "}, {"POST", "/data-apis/7/keys/2/renew", "renew "},
		{"GET", "/data-apis/7/versions/2", "version "},
		{"GET", "/internal/data-apis/7/manifest", "manifest "}, {"POST", "/internal/data-apis/7/usage", "usage "},
	} {
		if rec := serve(h, c[0], c[1], nil); rec.Code != 200 || rec.Body.String() != c[2] {
			t.Errorf("%s %s: %d %q", c[0], c[1], rec.Code, rec.Body.String())
		}
	}
	if rec := serve(h, "PUT", "/data-apis/7", nil); rec.Code != 405 || rec.Header().Get("Allow") != "GET, PATCH, DELETE" {
		t.Errorf("PUT: %d %v", rec.Code, rec.Header())
	}
}
