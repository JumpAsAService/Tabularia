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
