package main

import "fmt"

// routes/connections.py: the data connections.
func init() {
	register(Suite{
		Module: "connections",
		Seed:   seedData,
		Engine: []EngineRule{{Method: "GET", Path: "^/db/drivers$", Body: map[string]any{
			"sqlserver": map[string]any{"installed": false}, "oracle": map[string]any{"installed": 1}, "x": "no", "y": map[string]any{}}}},
		Steps: func(w *World) []Step {
			p := func(k string) string { return fmt.Sprint(Data[k]) }
			s := forEveryone(w, "every connection", "GET", "/connections", nil)
			s = append(s, forEveryone(w, "search", "GET", "/connections/search", nil)...)
			s = append(s, forEveryone(w, "a folder's connections", "GET", "/projects/"+p("connect")+"/connections", nil)...)
			s = append(s, forEveryone(w, "the drivers", "GET", "/connections/drivers", nil)...)
			as := func(person, name, method, path string) {
				s = append(s, Step{Name: name + " · " + person, Method: method, Path: path, Auth: w.As(person)})
			}
			for _, q := range []string{"?q=example", "?q=SALES", "?q=pg", "?limit=1&offset=1", "?limit=0", "?q="} {
				as("editor", "search "+q, "GET", "/connections/search"+q)
				as("admin", "search "+q, "GET", "/connections/search"+q)
			}
			for _, f := range []string{"root", "hidden", "999999", "x"} {
				as("editor", "the folder "+f, "GET", "/projects/"+map[string]string{"root": p("root"), "hidden": p("hidden")}[f]+map[bool]string{true: f}[f != "root" && f != "hidden"]+"/connections")
			}
			drivers := func(name string, rule EngineRule) {
				s = append(s, Step{Name: "the drivers: " + name, Method: "GET", Path: "/connections/drivers", Auth: w.As("editor"), Engine: []EngineRule{rule}})
			}
			drivers("the engine fails", EngineRule{Path: ".", Status: 500, Body: map[string]any{"sqlserver": map[string]any{"installed": true}}})
			drivers("a list", EngineRule{Path: ".", Body: []any{1}})
			drivers("not JSON", EngineRule{Path: ".", Text: "<html>"})
			drivers("201", EngineRule{Path: ".", Status: 201, Body: map[string]any{"sqlserver": map[string]any{"installed": true}}})
			drivers("installed truthy", EngineRule{Path: ".", Text: `{"sqlserver": {"installed": "yes"}, "a": {"installed": []}, "a": {"installed": 0.0}}`})
			return s
		},
	})
}
