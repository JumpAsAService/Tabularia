package server

// The routes the Go gateway serves and the Python one does not have: the Python gateway is
// no longer extended, so a new route exists only here. Each is declared by hand, as
// Starlette would compile it, and takes its place in the FastAPI table right after the
// route `after` (its first entry: a FastAPI route, or a Go-only one placed before it), so the order the routes are tried in stays FastAPI's for
// all the others. The list is explicit: a Go route that is in neither table is still an
// error (api's TestEveryPythonRouteHasItsGoHandler).
var goOnlyRoutes = []struct {
	after string
	route pyRoute
}{
	// the flow editor's right-click menu (api/flow_shortcuts.go)
	{after: "/users/me/locale", route: pyRoute{path: "/users/me/flow-shortcuts", methods: []string{"GET", "PUT"}, regex: `^/users/me/flow\-shortcuts$`}},
	// Explore's top level, the objects one changed last (api/explore_recent.go)
	{after: "/search", route: pyRoute{path: "/explore/recent", methods: []string{"GET"}, regex: `^/explore/recent$`}},
	// share links: the creators' side and the admin tab (api/share_links.go)
	{after: "/explore/recent", route: pyRoute{path: "/share-links", methods: []string{"GET", "POST"}, regex: `^/share\-links$`}},
	{after: "/share-links", route: pyRoute{path: "/share-links/{link_id}/password", methods: []string{"POST"}, regex: `^/share\-links/(?P<link_id>[^/]+)/password$`}},
	{after: "/share-links/{link_id}/password", route: pyRoute{path: "/share-links/{link_id}/revoke", methods: []string{"POST"}, regex: `^/share\-links/(?P<link_id>[^/]+)/revoke$`}},
	{after: "/share-links/{link_id}/revoke", route: pyRoute{path: "/admin/share-links", methods: []string{"GET"}, regex: `^/admin/share\-links$`}},
	{after: "/admin/share-links", route: pyRoute{path: "/admin/share-links/settings", methods: []string{"PUT"}, regex: `^/admin/share\-links/settings$`}},
	// share links: the recipient's side, with no account (api/share_public.go)
	{after: "/admin/share-links/settings", route: pyRoute{path: "/share/{token}", methods: []string{"GET"}, regex: `^/share/(?P<token>[^/]+)$`}},
	{after: "/share/{token}", route: pyRoute{path: "/share/{token}/frame", methods: []string{"GET"}, regex: `^/share/(?P<token>[^/]+)/frame$`}},
	{after: "/share/{token}/frame", route: pyRoute{path: "/share/{token}/session", methods: []string{"POST"}, regex: `^/share/(?P<token>[^/]+)/session$`}},
	{after: "/share/{token}/session", route: pyRoute{path: "/share/{token}/password", methods: []string{"POST"}, regex: `^/share/(?P<token>[^/]+)/password$`}},
	{after: "/share/{token}/password", route: pyRoute{path: "/share/{token}/parameters", methods: []string{"GET"}, regex: `^/share/(?P<token>[^/]+)/parameters$`}},
	{after: "/share/{token}/parameters", route: pyRoute{path: "/share/{token}/data", methods: []string{"POST"}, regex: `^/share/(?P<token>[^/]+)/data$`}},
	{after: "/share/{token}/data", route: pyRoute{path: "/share/{token}/render", methods: []string{"POST"}, regex: `^/share/(?P<token>[^/]+)/render$`}},
	{after: "/share/{token}/render", route: pyRoute{path: "/share/{token}/exports/{handle}", methods: []string{"GET"}, regex: `^/share/(?P<token>[^/]+)/exports/(?P<handle>[^/]+)$`}},
	{after: "/share/{token}/exports/{handle}", route: pyRoute{path: "/share/{token}/exports/{handle}/file", methods: []string{"GET"}, regex: `^/share/(?P<token>[^/]+)/exports/(?P<handle>[^/]+)/file$`}},
	{after: "/share/{token}/exports/{handle}/file", route: pyRoute{path: "/share/{token}/comment", methods: []string{"POST"}, regex: `^/share/(?P<token>[^/]+)/comment$`}},
}

// mainRoutes is the main application's table: FastAPI's, with the Go-only routes in place.
func mainRoutes() []pyRoute {
	out := make([]pyRoute, 0, len(fastAPIRoutes)+len(goOnlyRoutes))
	placed := make([]bool, len(goOnlyRoutes))
	// a Go-only route may follow another Go-only one: placed right after it, in turn
	var place func(after string)
	place = func(after string) {
		for i, g := range goOnlyRoutes {
			if !placed[i] && g.after == after {
				out = append(out, g.route)
				placed[i] = true
				place(g.route.path)
			}
		}
	}
	for _, p := range fastAPIRoutes {
		out = append(out, p)
		if !p.ws {
			place(p.path)
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
