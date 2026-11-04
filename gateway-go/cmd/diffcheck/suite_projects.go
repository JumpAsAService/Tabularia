package main

import (
	"fmt"
	"net/http"
	"time"
)

// Folders by name, seeded by the projects suite (other suites of the identity lane use them).
var Folders = map[string]int64{}

// routes/projects.py: the folder tree, with the permissions inherited down it and the
// personal folders.
func init() {
	register(Suite{
		Module: "projects",
		Seed: func(w *World) {
			folder := func(name string, parent *int64, owner string) int64 {
				var ownerID *int64
				if owner != "" {
					ownerID = ptr(w.People[owner])
				}
				id := w.Insert(`INSERT INTO projects (name, description, parent_id, owner_id, created_at)
					VALUES ($1, '', $2, $3, '2026-01-01') RETURNING id`, name, parent, ownerID)
				Folders[name] = id
				return id
			}
			grant := func(folder int64, person, group, capability string) {
				var u, g *int64
				if person != "" {
					u = ptr(w.People[person])
				}
				if group != "" {
					g = ptr(Groups[group])
				}
				w.Exec(`INSERT INTO permissions (project_id, user_id, group_id, capability) VALUES ($1, $2, $3, $4)`, folder, u, g, capability)
			}
			w.Token("writer", "tab_pr_writer_read_000000000", "read", time.Hour)
			w.Token("service", "tab_pr_service_000000000000", "write", time.Hour)
			root := folder("pr-Root", nil, "admin")
			child := folder("pr-Child", &root, "admin")
			grand := folder("pr-Grand", &child, "editor")
			other := folder("pr-Other", nil, "admin")
			personal := folder("writer", nil, "writer")
			folder("pr-Empty", nil, "admin")
			runs := folder("pr-Runs", &root, "admin")
			// a second folder with the personal shape, newer: the older one is the personal one
			late := folder("pr-Late", nil, "writer")
			grant(child, "editor", "", "manage")
			grant(other, "", "Analysts", "view")
			grant(personal, "writer", "", "edit")
			grant(personal, "writer", "", "manage")
			grant(late, "writer", "", "edit")
			grant(root, "obs_flag", "", "view")
			grant(grand, "service", "", "edit")
			w.Exec(`INSERT INTO runs (kind, task_id, status, trigger_type, input_key, output_bucket, output_key,
				publish_description, publish_overwrite, publish_sort_keys, started_at, publish_project_id)
				VALUES ('flow', 't-pr', 'SUCCESS', 'manual', '', '', '', '', false, '', '2026-01-01', $1)`, runs)
			env := w.Insert(`INSERT INTO script_environments (name, language, enabled, cpu_request, cpu_limit, memory_request,
				memory_limit, max_timeout_seconds, created_at, updated_at)
				VALUES ('pr-env', 'python', true, '1', '1', '1Gi', '1Gi', 60, '2026-01-01', '2026-01-01') RETURNING id`)
			holder := func(kind string) int64 { return folder("pr-Has-"+kind, &root, "admin") }
			stamp := `'2026-01-01', '2026-01-01'`
			w.Exec(`INSERT INTO flows (name, description, project_id, definition, engine, created_at, updated_at)
				VALUES ('pr-flow', '', $1, '{}', 'polars', `+stamp+`)`, holder("flow"))
			ds := w.Insert(`INSERT INTO datasources (name, description, project_id, bucket, key, columns, sort_keys,
				column_descriptions, kind, created_at, updated_at)
				VALUES ('pr-ds', '', $1, 'b', 'datasets/pr.parquet', '[]', '[]', '{}', 'file', `+stamp+`) RETURNING id`, holder("datasource"))
			w.Exec(`INSERT INTO connections (name, description, project_id, db_type, host, username, password_encrypted,
				database, db_schema, extra, created_at, updated_at)
				VALUES ('pr-conn', '', $1, 'postgres', 'h', 'u', '', 'd', '', '{}', `+stamp+`)`, holder("connection"))
			w.Exec(`INSERT INTO saved_views (name, description, project_id, datasource_id, spec, created_at, updated_at)
				VALUES ('pr-view', '', $1, $2, '{}', `+stamp+`)`, holder("view"), ds)
			w.Exec(`INSERT INTO flow_groups (name, description, project_id, definition, created_at, updated_at)
				VALUES ('pr-group', '', $1, '{}', `+stamp+`)`, holder("group"))
			w.Exec(`INSERT INTO saved_scripts (name, description, project_id, environment_id, current_version, created_at, updated_at)
				VALUES ('pr-script', '', $1, $2, 1, `+stamp+`)`, holder("script"), env)
			w.Exec(`INSERT INTO sql_scripts (name, description, project_id, current_version, created_at, updated_at)
				VALUES ('pr-sql', '', $1, 1, `+stamp+`)`, holder("sql"))
			w.Exec(`INSERT INTO notebooks (name, description, project_id, environment_id, current_version, created_at, updated_at)
				VALUES ('pr-nb', '', $1, $2, 1, `+stamp+`)`, holder("notebook"), env)
			w.Exec(`INSERT INTO reports (name, description, project_id, current_version, created_at, updated_at)
				VALUES ('pr-report', '', $1, 1, `+stamp+`)`, holder("report"))
			w.Exec(`INSERT INTO dashboards (name, description, project_id, current_version, created_at, updated_at)
				VALUES ('pr-dash', '', $1, 1, `+stamp+`)`, holder("dashboard"))
			w.Exec(`INSERT INTO webhooks (name, url, secret_enc, events, active, created_at, project_id)
				VALUES ('pr-hook', 'http://x', '', '[]', true, '2026-01-01', $1)`, holder("webhook"))
		},
		Steps: func(w *World) []Step {
			f := func(name string) string { return fmt.Sprint(Folders[name]) }
			var s []Step
			add := func(name, method, path string, auth func(*http.Request), body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: auth, JSON: body})
			}
			for _, p := range []string{"admin", "editor", "obs_flag", "obs_group", "admin_group", "writer", "service", "sso"} {
				add(p+" lists the folders", "GET", "/projects", w.As(p), nil)
			}
			add("nobody lists", "GET", "/projects", nil, nil)
			add("a read token lists", "GET", "/projects", Bearer("tab_pr_writer_read_000000000"), nil)

			for _, c := range []struct{ who, folder, why string }{
				{"admin", "pr-Child", "an administrator"},
				{"editor", "pr-Grand", "inherited MANAGE"},
				{"editor", "pr-Root", "an ancestor only: no VIEW"},
				{"editor", "pr-Other", "VIEW by a group"},
				{"writer", "writer", "the personal folder"},
				{"writer", "pr-Late", "the newer folder of the personal shape"},
				{"obs_flag", "pr-Grand", "VIEW inherited from the root"},
				{"admin_group", "writer", "an administrator by group, someone's personal folder"},
			} {
				add(c.who+" reads "+c.folder+" ("+c.why+")", "GET", "/projects/"+f(c.folder), w.As(c.who), nil)
			}
			add("an unknown folder", "GET", "/projects/999999", w.As("editor"), nil)
			for _, bad := range []string{"abc", "1.5", "%201%20", "1.0", "+1", "1_0", "-1", "0x1"} {
				add("path «"+bad+"»", "GET", "/projects/"+bad, w.As("admin"), nil)
			}
			add("an id past the integers", "GET", "/projects/3000000000", w.As("admin"), nil)
			add("an id past int64", "GET", "/projects/99999999999999999999", w.As("admin"), nil)
			add("a parent past the integers", "POST", "/projects", w.As("admin"), map[string]any{"name": "x", "parent_id": 3000000000})
			add("move under an id past the integers", "PATCH", "/projects/3000000000", w.As("admin"), map[string]any{"name": "x"})
			add("nobody, a bad path", "GET", "/projects/abc", nil, nil)

			// creations and changes (forwarded until the bodies' validation is ported)
			add("admin creates a top-level folder", "POST", "/projects", w.As("admin"), map[string]any{"name": "pr-New", "description": "d"})
			add("editor cannot create at the top", "POST", "/projects", w.As("editor"), map[string]any{"name": "pr-No"})
			add("editor creates under its MANAGE", "POST", "/projects", w.As("editor"), map[string]any{"name": "pr-Sub", "parent_id": Folders["pr-Child"]})
			add("an unknown parent", "POST", "/projects", w.As("editor"), map[string]any{"name": "pr-No", "parent_id": 999999})
			add("no MANAGE on the parent", "POST", "/projects", w.As("editor"), map[string]any{"name": "pr-No", "parent_id": Folders["pr-Other"]})
			add("no name", "POST", "/projects", w.As("admin"), map[string]any{"description": "d"})
			add("a name that is a number", "POST", "/projects", w.As("admin"), map[string]any{"name": 5})
			add("a parent that is text", "POST", "/projects", w.As("admin"), map[string]any{"name": "x", "parent_id": "abc"})
			s = append(s, Step{Name: "not JSON", Method: "POST", Path: "/projects", Auth: w.As("admin"), Raw: []byte("{"), ContentType: "application/json"})
			s = append(s, bodyEdges("POST", "/projects", w.As("admin"))...)
			s = append(s, bodyEdges("PATCH", "/projects/"+f("pr-Grand"), w.As("editor"))...)
			add("nobody creates", "POST", "/projects", nil, map[string]any{"name": "x"})
			add("an observer creates", "POST", "/projects", w.As("obs_flag"), map[string]any{"name": "x"})

			add("rename", "PATCH", "/projects/"+f("pr-Grand"), w.As("editor"), map[string]any{"name": "  pr-Grand2  "})
			add("rename to blanks", "PATCH", "/projects/"+f("pr-Grand"), w.As("editor"), map[string]any{"name": "   "})
			add("the same name and description", "PATCH", "/projects/"+f("pr-Grand"), w.As("editor"), map[string]any{"name": "pr-Grand2", "description": ""})
			add("a description", "PATCH", "/projects/"+f("pr-Grand"), w.As("editor"), map[string]any{"description": "new"})
			add("no EDIT", "PATCH", "/projects/"+f("pr-Other"), w.As("editor"), map[string]any{"name": "x"})
			add("an unknown folder", "PATCH", "/projects/999999", w.As("editor"), map[string]any{"name": "x"})
			add("move into itself", "PATCH", "/projects/"+f("pr-Child"), w.As("admin"), map[string]any{"parent_id": Folders["pr-Child"]})
			add("move into a descendant", "PATCH", "/projects/"+f("pr-Child"), w.As("admin"), map[string]any{"parent_id": Folders["pr-Grand"]})
			add("move under an unknown folder", "PATCH", "/projects/"+f("pr-Grand"), w.As("editor"), map[string]any{"parent_id": 999999})
			add("move without MANAGE on the target", "PATCH", "/projects/"+f("pr-Grand"), w.As("editor"), map[string]any{"parent_id": Folders["pr-Other"]})
			add("move one's personal folder", "PATCH", "/projects/"+f("writer"), w.As("writer"), map[string]any{"parent_id": Folders["pr-Late"]})
			add("rename and move", "PATCH", "/projects/"+f("pr-Grand"), w.As("admin"), map[string]any{"name": "pr-Grand", "parent_id": Folders["pr-Root"]})
			add("move back, the same parent named", "PATCH", "/projects/"+f("pr-Grand"), w.As("admin"), map[string]any{"parent_id": Folders["pr-Root"]})
			add("PATCH a bad path", "PATCH", "/projects/abc", w.As("admin"), map[string]any{"name": "x"})

			// deletions
			add("delete without MANAGE", "DELETE", "/projects/"+f("pr-Root"), w.As("editor"), nil)
			add("delete an unknown folder", "DELETE", "/projects/999999", w.As("editor"), nil)
			add("delete one's own personal folder", "DELETE", "/projects/"+f("writer"), w.As("writer"), nil)
			add("delete a folder with subfolders", "DELETE", "/projects/"+f("pr-Root"), w.As("admin"), nil)
			for _, k := range []string{"flow", "datasource", "connection", "view", "group", "script", "sql", "notebook", "report", "dashboard", "webhook"} {
				add("delete a folder holding a "+k, "DELETE", "/projects/"+f("pr-Has-"+k), w.As("admin"), nil)
			}
			add("an observer deletes", "DELETE", "/projects/"+f("pr-Empty"), w.As("obs_flag"), nil)
			add("a read token deletes", "DELETE", "/projects/"+f("pr-Empty"), Bearer("tab_pr_writer_read_000000000"), nil)
			s = append(s, Step{Name: "delete without the CSRF header", Method: "DELETE", Path: "/projects/" + f("pr-Empty"), Auth: Cookie(w.JWT("admin", "s-admin"))})
			add("nobody deletes", "DELETE", "/projects/"+f("pr-Empty"), nil, nil)
			add("delete a bad path", "DELETE", "/projects/abc", w.As("admin"), nil)
			add("nobody, a bad path", "DELETE", "/projects/abc", nil, nil)
			add("an empty folder", "DELETE", "/projects/"+f("pr-Empty"), w.As("admin"), nil)
			add("a folder runs published into", "DELETE", "/projects/"+f("pr-Runs"), w.As("admin"), nil)
			add("a service account's EDIT is not MANAGE", "DELETE", "/projects/"+f("pr-Grand"), Bearer("tab_pr_service_000000000000"), nil)
			add("an administrator deletes a personal folder", "DELETE", "/projects/"+f("writer"), w.As("admin_group"), nil)
			add("the folder gone", "GET", "/projects/"+f("writer"), w.As("admin"), nil)
			add("writer's list now", "GET", "/projects", w.As("writer"), nil)
			return s
		},
	})
}
