package main

import "fmt"

// routes/connections.py: the data connections.
func init() {
	register(Suite{
		Module: "connections",
		Seed: func(w *World) {
			seedData(w)
			w.Exec(`UPDATE flows SET notify_connection_id = $1 WHERE id = $2`, Data["conn:smtp"], Data["flow:duck"])
			w.Exec(`UPDATE datasources SET connection_id = $1 WHERE id = $2`, Data["conn:pg"], Data["ds:run"])
			rep := w.Insert(`INSERT INTO reports (name, description, project_id, owner_id, current_version, created_at, updated_at, views_total)
				VALUES ('data-conn-report', '', $1, NULL, 1, '2026-01-07', '2026-01-07', 0) RETURNING id`, Data["root"])
			w.Exec(`INSERT INTO report_versions (report_id, version, content, note, created_at, created_by) VALUES ($1, 1, $2, '', '2026-01-07', NULL)`,
				rep, fmt.Sprintf(`{"datasets": [{"source": {"kind": "sql", "connection_id": %d}}]}`, Data["conn:root"]))
		},
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
			conn := func(k string) string { return "/connections/" + fmt.Sprint(Data["conn:"+k]) }
			as("writer", "duplicate without CONNECT", "POST", conn("pg")+"/duplicate")
			as("editor", "duplicate", "POST", conn("pg")+"/duplicate")
			as("editor", "duplicate again", "POST", conn("pg")+"/duplicate")
			as("editor", "duplicate where EDIT only", "POST", conn("root")+"/duplicate")
			as("editor", "duplicate an unknown one", "POST", "/connections/999999/duplicate")
			as("editor", "duplicate an odd id", "POST", "/connections/x/duplicate")
			as("editor", "delete, used by a datasource", "DELETE", conn("pg"))
			as("editor", "delete, a flow's notices", "DELETE", conn("smtp"))
			as("admin", "delete, a report reads it", "DELETE", conn("root"))
			as("editor", "delete, where EDIT only", "DELETE", conn("root"))
			as("admin", "delete", "DELETE", conn("hidden"))
			as("admin", "delete, again", "DELETE", conn("hidden"))
			as("editor", "delete an odd id", "DELETE", "/connections/1.5")
			s = append(s, Step{Name: "duplicate with the read token", Method: "POST", Path: conn("pg") + "/duplicate", Auth: Bearer("tab_data_writer_read_000000")})
			return s
		},
	})
}
