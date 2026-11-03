package main

import "fmt"

// routes/datasources.py: the catalog of datasources.
func init() {
	register(Suite{
		Module: "datasources",
		Seed:   seedData,
		Steps: func(w *World) []Step {
			p := func(k string) string { return fmt.Sprint(Data[k]) }
			s := forEveryone(w, "every datasource", "GET", "/datasources", nil)
			s = append(s, forEveryone(w, "search", "GET", "/datasources/search", nil)...)
			s = append(s, forEveryone(w, "a folder's datasources", "GET", "/projects/"+p("child")+"/datasources", nil)...)
			as := func(person, name, method, path string, body any) {
				s = append(s, Step{Name: name + " · " + person, Method: method, Path: path, JSON: body, Auth: w.As(person)})
			}
			for _, q := range []string{"?q=ROOT", "?q=about", "?q=", "?q=%25", "?q=_", "?limit=1", "?limit=2&offset=1", "?offset=100",
				"?limit=0", "?limit=201", "?limit=x&offset=-1", "?offset=99999999999999999999", "?offset=9223372036854775807",
				"?q=a&q=ready", "?limit=1.0", "?limit=%201", "?q=%C3%A8"} {
				as("editor", "search "+q, "GET", "/datasources/search"+q, nil)
				as("admin", "search "+q, "GET", "/datasources/search"+q, nil)
			}
			s = append(s, Step{Name: "a bad query, nobody: 401 first", Method: "GET", Path: "/datasources/search?limit=x"})
			as("obs_flag", "search, nothing readable", "GET", "/datasources/search?limit=x", nil)
			for _, f := range []string{"root", "run", "hidden", "connect"} {
				as("editor", "the folder "+f, "GET", "/projects/"+p(f)+"/datasources", nil)
			}
			for _, id := range []string{"999999", "x", "99999999999", "-1"} {
				as("editor", "the folder "+id, "GET", "/projects/"+id+"/datasources", nil)
			}
			return s
		},
	})
}
