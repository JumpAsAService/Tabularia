package server

// The routes the Go gateway serves and the Python one does not have: the Python gateway is
// no longer extended, so a new route exists only here. Each is declared by hand, as
// Starlette would compile it, and takes its place in the FastAPI table right after the
// route `after` (its first entry), so the order the routes are tried in stays FastAPI's for
// all the others. The list is explicit: a Go route that is in neither table is still an
// error (api's TestEveryPythonRouteHasItsGoHandler).
var goOnlyRoutes = []struct {
	after string
	route pyRoute
}{
	// the flow editor's right-click menu (api/flow_shortcuts.go)
	{after: "/users/me/locale", route: pyRoute{path: "/users/me/flow-shortcuts", methods: []string{"GET", "PUT"}, regex: `^/users/me/flow\-shortcuts$`}},
}

// mainRoutes is the main application's table: FastAPI's, with the Go-only routes in place.
func mainRoutes() []pyRoute {
	out := make([]pyRoute, 0, len(fastAPIRoutes)+len(goOnlyRoutes))
	placed := make([]bool, len(goOnlyRoutes))
	for _, p := range fastAPIRoutes {
		out = append(out, p)
		for i, g := range goOnlyRoutes {
			if !placed[i] && g.after == p.path && !p.ws {
				out = append(out, g.route)
				placed[i] = true
			}
		}
	}
	for i, g := range goOnlyRoutes {
		if !placed[i] {
			panic("server: the Go-only route " + g.route.path + " follows " + g.after + ", which is not a route")
		}
	}
	return out
}

// GoOnlyRoutes are the routes of goOnlyRoutes, [method, path]: the ones the Go gateway
// answers without a Python twin.
func GoOnlyRoutes() [][2]string {
	var out [][2]string
	for _, g := range goOnlyRoutes {
		for _, m := range g.route.methods {
			out = append(out, [2]string{m, g.route.path})
		}
	}
	return out
}
