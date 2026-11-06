package main

import (
	"fmt"
	"net/http"
	"strings"
)

// routes/notebooks.py: marimo notebooks and their versions. Without SCRIPTS__ENABLED the
// lists are empty and the writes answer 409; with it every branch runs (see suite_scripts.go).
func init() {
	ids := map[string]int64{}
	register(Suite{
		Module: "notebooks",
		Seed: func(w *World) {
			ids = map[string]int64{}
			folder := func(key, name string, parent *int64) int64 {
				ids[key] = w.Insert(`INSERT INTO projects (name, description, parent_id, created_at) VALUES ($1, '', $2, '2026-01-01')
					RETURNING id`, name, parent)
				return ids[key]
			}
			h1 := folder("h1", "nb-root", nil)
			folder("h2", "nb-edit", &h1)
			folder("h3", "nb-hidden", nil)
			grant := func(folder, capability, person string) {
				w.Exec(`INSERT INTO permissions (project_id, user_id, capability) VALUES ($1, $2, $3)`, ids[folder], w.People[person], capability)
			}
			grant("h2", "edit", "writer")
			grant("h1", "view", "editor")
			grant("h2", "view", "obs_flag")
			coders := w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ('nb-coders', '', false, false,
				'2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2), ($3, $2), ($4, $2)`, w.People["writer"], coders,
				w.People["editor"], w.People["obs_flag"])
			env := func(key, name, language string) {
				ids[key] = w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit,
					memory_request, memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ($1, '', $2, true, '1', '1', '1Gi',
					'1Gi', 600, '2026-01-01', '2026-01-01') RETURNING id`, name, language)
				w.Exec(`INSERT INTO script_environment_groups (environment_id, group_id) VALUES ($1, $2)`, ids[key], coders)
			}
			env("py", "nb-py", "python")
			env("r", "nb-r", "r")
			ids["closed"] = w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit,
				memory_request, memory_limit, max_timeout_seconds, created_at, updated_at) VALUES ('nb-closed', '', 'python', true, '1', '1',
				'1Gi', '1Gi', 600, '2026-01-01', '2026-01-01') RETURNING id`)
			nb := func(key, name, folder, environment string, current int, maxVisitors any, versions ...string) {
				ids[key] = w.Insert(`INSERT INTO notebooks (name, description, project_id, owner_id, environment_id, current_version,
					created_at, updated_at, max_visitors) VALUES ($1, 'about', $2, $3, $4, $5, '2026-02-01', '2026-02-02', $6) RETURNING id`,
					name, ids[folder], w.People["admin"], ids[environment], current, maxVisitors)
				for i, c := range versions {
					w.Exec(`INSERT INTO notebook_versions (notebook_id, version, content, note, created_at, created_by)
						VALUES ($1, $2, $3, 'n', '2026-02-01', $4)`, ids[key], i+1, c, w.People[[]string{"writer", "admin"}[i%2]])
				}
			}
			src := func(s string) string { return fmt.Sprintf(`{"source": %q}`, s) }
			nb("n1", "Board", "h2", "py", 2, nil, src("a"), src("b"))
			nb("n2", "RBoard", "h2", "r", 1, int64(3), src("r"))
			nb("n3", "Hidden", "h3", "py", 1, nil, src("h"))
			nb("n4", "Broken", "h2", "py", 1, nil, `{`)
			nb("n5", "Editing", "h2", "py", 2, nil, src("e1"), src("e2"))
			nb("n6", "Drafted", "h2", "py", 1, int64(5), src("d"))
			nb("n7", "Closed", "h2", "closed", 1, nil, src("c"))
			nb("n8", "BadV1", "h2", "py", 2, nil, `{"source": "  "}`, src("ok"))
			nb("n9", "Gone", "h2", "py", 1, nil)
			w.Exec(`UPDATE notebooks SET draft = 'draft text', draft_by = $1, draft_at = '2026-02-03' WHERE id = $2`, w.People["writer"], ids["n6"])
			w.Exec(`INSERT INTO notebook_sessions (notebook_id, mode, user_id, version, namespace, job_name, status, token_fp, started_at,
				last_seen_at) VALUES ($1, 'edit', $2, 2, 'scripts', 'nb-edit-5', 'ready', 'fp', '2026-02-03', '2026-02-03'),
				($1, 'app', $2, 2, 'scripts', 'nb-app-5', 'ready', 'fp2', '2026-02-03', '2026-02-03')`, ids["n5"], w.People["editor"])
			w.Exec(`INSERT INTO notebook_visitors (notebook_id, user_id, opened_at, last_seen_at) VALUES ($1, $2, '2026-02-03', '2026-02-03')`,
				ids["n5"], w.People["writer"])
		},
		Steps: func(w *World) []Step {
			var s []Step
			id := func(k string) int64 { return ids[k] }
			as := func(p string) func(*http.Request) {
				if p == "nobody" {
					return nil
				}
				return w.As(p)
			}
			add := func(name, method, path, p string, body any) {
				s = append(s, Step{Name: name, Method: method, Path: path, Auth: as(p), JSON: body})
			}
			for _, p := range []string{"admin", "editor", "writer", "obs_flag", "obs_group", "nobody"} {
				add(p+" lists the notebooks", "GET", "/notebooks", p, nil)
				for _, f := range []string{"h1", "h2", "h3"} {
					add(p+" lists a folder", "GET", fmt.Sprintf("/projects/%d/notebooks", id(f)), p, nil)
				}
				for _, k := range []string{"n1", "n2", "n3", "n4", "n6", "n7", "n9"} {
					add(p+" opens "+k, "GET", fmt.Sprintf("/notebooks/%d", id(k)), p, nil)
				}
				add(p+" lists n1's versions", "GET", fmt.Sprintf("/notebooks/%d/versions", id("n1")), p, nil)
				for _, v := range []string{"0", "1", "3", "x"} {
					add(p+" reads n1's version "+v, "GET", fmt.Sprintf("/notebooks/%d/versions/%s", id("n1"), v), p, nil)
				}
			}
			add("a notebook that does not exist", "GET", "/notebooks/999999", "admin", nil)
			add("a folder that does not exist", "GET", "/projects/999999/notebooks", "admin", nil)

			create := func(name string, folder any, p string, body any) {
				add(name, "POST", fmt.Sprintf("/projects/%v/notebooks", folder), p, body)
			}
			h2, py := id("h2"), id("py")
			create("writer creates one", h2, "writer", map[string]any{"name": " New ", "description": " d ", "environment_id": py})
			create("writer creates it again", h2, "writer", map[string]any{"name": "New", "environment_id": py})
			create("writer creates one with its source", h2, "writer", map[string]any{"name": "Own", "environment_id": py,
				"content": map[string]any{"source": "import marimo", "extra": 1}, "max_visitors": 7})
			create("writer creates one named with quotes", h2, "writer", map[string]any{"name": `It's "x"`, "environment_id": py})
			create("writer creates one in R", h2, "writer", map[string]any{"name": "R", "environment_id": id("r")})
			create("writer creates one in a closed env", h2, "writer", map[string]any{"name": "C", "environment_id": id("closed")})
			create("writer creates one with too many visitors", h2, "writer", map[string]any{"name": "V", "environment_id": py, "max_visitors": 51})
			create("writer creates one with no visitors", h2, "writer", map[string]any{"name": "V", "environment_id": py, "max_visitors": 0})
			create("writer creates one with an empty source", h2, "writer", map[string]any{"name": "E", "environment_id": py,
				"content": map[string]any{"source": " \n"}})
			create("writer creates one with a source that is no text", h2, "writer", map[string]any{"name": "E", "environment_id": py,
				"content": map[string]any{"source": 1}})
			create("writer creates one with a huge source", h2, "writer", map[string]any{"name": "E", "environment_id": py,
				"content": map[string]any{"source": strings.Repeat("x", 768*1024+1)}})
			create("editor creates one (VIEW)", id("h1"), "editor", map[string]any{"name": "E", "environment_id": py})
			create("nobody creates one", h2, "nobody", map[string]any{"name": "E", "environment_id": py})
			create("a word as the folder", "x", "writer", map[string]any{"name": "E"})

			patch := func(name, key, p string, body any) {
				add(name, "PATCH", fmt.Sprintf("/notebooks/%v", key), p, body)
			}
			n := func(k string) string { return fmt.Sprint(id(k)) }
			patch("writer changes nothing", n("n1"), "writer", map[string]any{})
			patch("writer sends the same source", n("n1"), "writer", map[string]any{"content": map[string]any{"source": "b"}})
			patch("writer changes the source", n("n1"), "writer", map[string]any{"content": map[string]any{"source": "c"}, "base_version": 2})
			patch("writer changes it on an old version", n("n1"), "writer", map[string]any{"content": map[string]any{"source": "d"}, "base_version": 2})
			patch("writer changes it while edited", n("n5"), "writer", map[string]any{"content": map[string]any{"source": "x"}})
			patch("writer renames the edited one", n("n5"), "writer", map[string]any{"name": "Editing 2", "description": "x"})
			patch("writer sets the visitors", n("n6"), "writer", map[string]any{"max_visitors": 9})
			patch("writer sends the visitors back to default", n("n6"), "writer", map[string]any{"max_visitors": nil})
			patch("writer sets too many visitors", n("n6"), "writer", map[string]any{"max_visitors": 60})
			patch("writer changes Drafted's source: the draft goes", n("n6"), "writer", map[string]any{"content": map[string]any{"source": "new"}})
			patch("writer moves to R", n("n1"), "writer", map[string]any{"environment_id": id("r")})
			patch("writer moves to a closed env", n("n1"), "writer", map[string]any{"environment_id": id("closed")})
			patch("writer moves to a folder of VIEW", n("n1"), "writer", map[string]any{"project_id": id("h1")})
			patch("writer moves nowhere", n("n1"), "writer", map[string]any{"project_id": 999999})
			patch("writer renames onto another", n("n1"), "writer", map[string]any{"name": "RBoard"})
			patch("admin moves Board", n("n1"), "admin", map[string]any{"project_id": id("h1"), "name": "Board X"})
			patch("editor changes (VIEW)", n("n2"), "editor", map[string]any{"name": "x"})
			patch("bad fields", "x", "writer", map[string]any{"max_visitors": "many", "base_version": 1.5})

			add("writer discards a draft", "DELETE", fmt.Sprintf("/notebooks/%d/draft", id("n2")), "writer", nil)
			add("editor discards a draft", "DELETE", fmt.Sprintf("/notebooks/%d/draft", id("n2")), "editor", nil)
			for _, c := range []struct{ name, key, p string }{{"writer copies Board", "n1", "writer"}, {"writer copies RBoard", "n2", "writer"},
				{"writer copies Broken", "n4", "writer"}, {"writer copies Gone", "n9", "writer"}, {"editor copies", "n2", "editor"}, {"nobody copies", "n2", "nobody"}} {
				add(c.name, "POST", fmt.Sprintf("/notebooks/%d/duplicate", id(c.key)), c.p, nil)
			}
			restore := func(name, key, v, p string) {
				add(name, "POST", fmt.Sprintf("/notebooks/%d/versions/%s/restore", id(key), v), p, nil)
			}
			restore("writer restores RBoard v1 (current)", "n2", "1", "writer")
			restore("writer restores the edited one", "n5", "1", "writer")
			restore("writer restores BadV1 v1", "n8", "1", "writer")
			restore("writer restores v9", "n8", "9", "writer")
			restore("admin restores Board v1", "n1", "1", "admin")
			restore("editor restores", "n2", "1", "editor")
			for _, c := range []struct{ name, key, p string }{{"editor deletes", "n2", "editor"}, {"writer deletes the edited one", "n5", "writer"},
				{"writer deletes it again", "n5", "writer"}, {"admin deletes Broken", "n4", "admin"}} {
				add(c.name, "DELETE", fmt.Sprintf("/notebooks/%d", id(c.key)), c.p, nil)
			}
			add("the list at the end", "GET", "/notebooks", "admin", nil)
			return s
		},
	})
}
