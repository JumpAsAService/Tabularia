package main

import "fmt"

// routes/saved_views.py: the Viewer's configurations saved in a folder.
func init() {
	register(Suite{
		Module: "saved_views",
		Seed:   seedData,
		Steps: func(w *World) []Step {
			v := func(k string) string { return fmt.Sprint(Data["view:"+k]) }
			p := func(k string) string { return fmt.Sprint(Data[k]) }
			s := forEveryone(w, "every view", "GET", "/saved-views", nil)
			s = append(s, forEveryone(w, "a folder's views", "GET", "/projects/"+p("root")+"/saved-views", nil)...)
			s = append(s, forEveryone(w, "a view", "GET", "/saved-views/"+v("root"), nil)...)
			as := func(person, name, method, path string, body any) {
				s = append(s, Step{Name: name + " · " + person, Method: method, Path: path, JSON: body, Auth: w.As(person)})
			}
			for _, person := range []string{"editor", "writer", "admin"} {
				as(person, "a child folder's views", "GET", "/projects/"+p("child")+"/saved-views", nil)
				as(person, "a hidden folder's views", "GET", "/projects/"+p("hidden")+"/saved-views", nil)
				as(person, "a RUN folder's views", "GET", "/projects/"+p("run")+"/saved-views", nil)
				as(person, "a view in a child folder", "GET", "/saved-views/"+v("child"), nil)
				as(person, "a hidden view", "GET", "/saved-views/"+v("hidden"), nil)
				as(person, "a view on a hidden datasource", "GET", "/saved-views/"+v("root2"), nil)
			}
			for _, id := range []string{"999999", "0", "abc", "1.0", "-1", "%201", "99999999999", "2147483648", "2147483647", "007"} {
				as("editor", "an odd view id "+id, "GET", "/saved-views/"+id, nil)
				as("editor", "an odd folder id "+id, "GET", "/projects/"+id+"/saved-views", nil)
			}
			s = append(s, Step{Name: "an odd id, nobody: 401 first", Method: "GET", Path: "/saved-views/abc"})
			// creating
			create := func(person, name string, project string, body any) {
				as(person, name, "POST", "/projects/"+project+"/saved-views", body)
			}
			good := map[string]any{"name": "data-new-view", "datasource_id": Data["ds:root"], "spec": `{"engine": "duckdb"}`}
			create("obs_flag", "create, an observer", p("root"), good)
			create("writer", "create, VIEW only", p("root"), good)
			create("editor", "create in an unknown folder", "999999", good)
			create("editor", "create on an unknown datasource", p("root"), map[string]any{"name": "x", "datasource_id": 999999})
			create("editor", "create on a hidden datasource", p("root"), map[string]any{"name": "x", "datasource_id": Data["ds:hidden"]})
			create("editor", "create with a blank name", p("root"), map[string]any{"name": "  ", "datasource_id": Data["ds:root"]})
			create("editor", "create with a name taken", p("root"), map[string]any{"name": " data-view-b ", "datasource_id": Data["ds:root"]})
			create("editor", "create with a spec not JSON", p("root"), map[string]any{"name": "x", "datasource_id": Data["ds:root"], "spec": "{"})
			create("editor", "create without a name", p("root"), map[string]any{"datasource_id": Data["ds:root"]})
			create("editor", "create with a number as name", p("root"), map[string]any{"name": 5, "datasource_id": Data["ds:root"]})
			for i, spec := range []string{"null", " ", "NaN", "\ufeff{}", "[1,]", "{\"a\": Infinity}", "1e999", "\"x\""} {
				create("editor", fmt.Sprintf("create with the spec %q", spec), p("root"), map[string]any{"name": fmt.Sprintf("data-spec-%d", i), "datasource_id": Data["ds:root"], "spec": spec})
			}
			create("editor", "create in a RUN folder", p("run"), map[string]any{"name": "x", "datasource_id": Data["ds:run"]})
			create("editor", "create with a huge datasource id", p("root"), map[string]any{"name": "x", "datasource_id": 99999999999})
			create("editor", "create with a huge folder id", "99999999999", good)
			create("editor", "create with a bad folder id and a bad body", "x", map[string]any{"name": 1})
			create("obs_flag", "create with a bad body, an observer", p("root"), map[string]any{"name": 1})
			s = append(s, Step{Name: "create with a broken JSON, nobody", Method: "POST", Path: "/projects/" + p("root") + "/saved-views", Raw: []byte(`{"name"`), ContentType: "application/json"})
			s = append(s, Step{Name: "create with no body", Method: "POST", Path: "/projects/" + p("root") + "/saved-views", Auth: w.As("editor")})
			s = append(s, Step{Name: "create with a form", Method: "POST", Path: "/projects/" + p("root") + "/saved-views", Raw: []byte(`name=x`), ContentType: "application/x-www-form-urlencoded", Auth: w.As("editor")})
			create("editor", "create", p("root"), good)
			create("editor", "create a name with a copy number", p("root"), map[string]any{"name": "data_100% (2)", "datasource_id": Data["ds:root"]})
			create("editor", "create with an empty spec", p("child"), map[string]any{"name": "data-empty-spec", "datasource_id": Data["ds:root"], "spec": ""})
			create("admin", "create in a hidden folder", p("hidden"), map[string]any{"name": "data-admin-view", "datasource_id": Data["ds:hidden"], "description": "d"})
			// duplicating
			for _, person := range []string{"obs_flag", "writer", "editor", "admin"} {
				as(person, "duplicate", "POST", "/saved-views/"+v("root")+"/duplicate", nil)
			}
			as("editor", "duplicate twice", "POST", "/saved-views/"+v("root")+"/duplicate", nil)
			s = append(s, Step{Name: "duplicate the copy numbered", Method: "POST", Path: "/saved-views/" + fmt.Sprint(Data["view:root"]+11) + "/duplicate", Auth: w.As("editor")})
			as("editor", "duplicate a view on a hidden datasource", "POST", "/saved-views/"+v("root2")+"/duplicate", nil)
			as("editor", "duplicate a hidden view", "POST", "/saved-views/"+v("hidden")+"/duplicate", nil)
			as("editor", "duplicate an unknown view", "POST", "/saved-views/999999/duplicate", nil)
			s = append(s, Step{Name: "duplicate with the read token", Method: "POST", Path: "/saved-views/" + v("root") + "/duplicate", Auth: Bearer("tab_data_writer_read_000000")})
			// changing
			patch := func(person, name, view string, body any) { as(person, name, "PATCH", "/saved-views/"+view, body) }
			patch("writer", "rename, VIEW only", v("root"), map[string]any{"name": "x"})
			patch("editor", "rename", v("root"), map[string]any{"name": " data-view-b2 "})
			patch("editor", "rename to a name taken", v("root"), map[string]any{"name": "data-view-a"})
			patch("editor", "rename to blank", v("root"), map[string]any{"name": " "})
			patch("editor", "a spec not JSON", v("root"), map[string]any{"spec": "nope"})
			patch("editor", "a new spec and description", v("root"), map[string]any{"spec": `{"engine": "chdb"}`, "description": "new"})
			patch("editor", "move to a hidden folder", v("root"), map[string]any{"project_id": Data["hidden"]})
			patch("editor", "move to an unknown folder", v("root"), map[string]any{"project_id": 999999})
			patch("editor", "move to the child folder", v("root"), map[string]any{"project_id": Data["child"]})
			patch("editor", "nothing", v("root"), map[string]any{})
			patch("editor", "a hidden view", v("hidden"), map[string]any{"name": "x"})
			patch("editor", "an unknown view", "999999", map[string]any{"name": "x"})
			// deleting
			for _, person := range []string{"obs_flag", "writer", "nobody"} {
				st := Step{Name: "delete · " + person, Method: "DELETE", Path: "/saved-views/" + v("child")}
				if person != "nobody" {
					st.Auth = w.As(person)
				}
				s = append(s, st)
			}
			as("editor", "delete a hidden view", "DELETE", "/saved-views/"+v("hidden"), nil)
			as("editor", "delete", "DELETE", "/saved-views/"+v("child"), nil)
			as("editor", "delete it again", "DELETE", "/saved-views/"+v("child"), nil)
			as("editor", "delete an odd id", "DELETE", "/saved-views/x", nil)
			as("editor", "every view after the changes", "GET", "/saved-views", nil)
			return s
		},
	})
}
