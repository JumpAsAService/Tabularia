package main

import (
	"fmt"
	"net/http"
	"strings"
	"time"
)

// routes/scripting.py: the scripting environments (Admin → Scripting environments) and what
// the flow editor reads of them. Run it also with
// DIFF_ENV="SCRIPTS__ENABLED=true SCRIPTS__NAMESPACE=scripts SCRIPTS__REGISTRY=registry.test/tab
// KUBERNETES__API_URL=http://{engine}:8000 KUBERNETES__TOKEN_FILE=/etc/hostname": the
// writes then go through, and the requests to Kubernetes reach the fake engine, compared.
func init() {
	maskColumns("script_storage", "secret_key_enc")
	maskColumns("script_environment_variables", "value_enc")
	ids := map[string]int64{}
	register(Suite{
		Module: "scripting",
		Seed: func(w *World) {
			ids = map[string]int64{}
			coders := w.Insert(`INSERT INTO groups (name, description, is_admin, is_observer, created_at) VALUES ('sc-coders', '', false,
				false, '2026-01-01') RETURNING id`)
			ids["coders"] = coders
			w.Exec(`INSERT INTO user_groups (user_id, group_id) VALUES ($1, $2)`, w.People["writer"], coders)
			env := func(key, name, language string, enabled bool, groups ...int64) {
				ids[key] = w.Insert(`INSERT INTO script_environments (name, description, language, enabled, cpu_request, cpu_limit,
					memory_request, memory_limit, max_timeout_seconds, created_at, created_by, updated_at, updated_by)
					VALUES ($1, 'about', $2, $3, '500m', '2', '1Gi', '4Gi', 900, '2026-01-01', 'a@x', '2026-01-02 03:04:05.5', 'b@x')
					RETURNING id`, name, language, enabled)
				for _, g := range groups {
					w.Exec(`INSERT INTO script_environment_groups (environment_id, group_id) VALUES ($1, $2)`, ids[key], g)
				}
			}
			env("e1", "Sc-Python", "python", true, coders, Groups["Analysts"])
			env("e2", "sc-r", "r", true, Groups["Sales"])
			env("e3", "sc-off", "python", false, coders)
			env("e4", "sc-empty", "python", true)
			env("e5", "sc-used-by-flow", "python", true)
			env("e6", "sc-used-by-script", "python", true)
			version := func(env string, number int, status, lv string, lock, completions any) int64 {
				return w.Insert(`INSERT INTO script_environment_versions (environment_id, number, language_version, base_image, packages,
					status, image_ref, lock, completions, created_at, created_by, started_at) VALUES ($1, $2, $3, 'python:3.12-slim', 'polars==1.0
pandas', $4, 'reg/x@sha256:1', $5, $6, '2026-01-01', 'a@x', '2026-01-01 00:00:01') RETURNING id`, ids[env], number, lv, status, lock, completions)
			}
			ids["v11"] = version("e1", 1, "ready", "3.12", "polars==1.0\npandas==2.0", `{"polars": ["col", "lit"]}`)
			ids["v12"] = version("e1", 2, "failed", "3.13", nil, nil)
			ids["v21"] = version("e2", 1, "building", "4.5", nil, `[1]`)
			ids["v31"] = version("e3", 1, "ready", "3.12", nil, "not json")
			w.Exec(`UPDATE script_environments SET current_version_id = $1 WHERE id = $2`, ids["v11"], ids["e1"])
			w.Exec(`UPDATE script_environments SET current_version_id = $1 WHERE id = $2`, ids["v21"], ids["e2"])
			w.Exec(`UPDATE script_environments SET current_version_id = $1 WHERE id = $2`, ids["v31"], ids["e3"])
			variable := func(env, name string, secret, from bool, value string) {
				var enc any
				if secret {
					enc = "gAAAAA-fixed"
				}
				w.Exec(`INSERT INTO script_environment_variables (environment_id, name, secret, value, value_enc, from_tabularia,
					updated_at, updated_by) VALUES ($1, $2, $3, $4, $5, $6, '2026-01-03', 'c@x')`, ids[env], name, secret, value, enc, from)
			}
			variable("e1", "REGION", false, false, "eu")
			variable("e1", "API_KEY", true, false, "")
			variable("e1", "HANDED", false, true, "TABULARIA_RUN_ID")
			w.Exec(`INSERT INTO script_storage (id, endpoint, region, bucket, access_key, secret_key_enc) VALUES (1, 'http://minio:9000',
				'eu', 'sc-bkt', 'AK', 'gAAAAA-fixed') ON CONFLICT (id) DO UPDATE SET endpoint = 'http://minio:9000', region = 'eu',
				bucket = 'sc-bkt', access_key = 'AK', secret_key_enc = 'gAAAAA-fixed', updated_at = NULL, updated_by = NULL`)
			project := w.Insert(`INSERT INTO projects (name, description, created_at) VALUES ('sc-folder', '', '2026-01-01') RETURNING id`)
			w.Exec(`INSERT INTO flows (name, description, project_id, definition, engine, created_at, updated_at) VALUES
				('sc-flow-b', '', $1, $2, 'polars', '2026-01-01', '2026-01-01'),
				('sc-flow-a', '', $1, $2, 'polars', '2026-01-01', '2026-01-01'),
				('sc-flow-a', '', $1, $2, 'polars', '2026-01-01', '2026-01-01'),
				('sc-flow-x', '', $1, '{"nodes": "script"', 'polars', '2026-01-01', '2026-01-01')`, project,
				fmt.Sprintf(`{"nodes": [{"id": "n", "type": "script", "data": {"environmentId": %d.0}}, 3]}`, ids["e5"]))
			w.Exec(`INSERT INTO saved_scripts (name, description, project_id, environment_id, current_version, created_at, updated_at)
				VALUES ('sc-s', '', $1, $2, 0, '2026-01-01', '2026-01-01')`, project, ids["e6"])
			w.Token("admin", "tab_sc_admin_write_000000000", "write", time.Hour)
			w.Token("admin", "tab_sc_admin_read_0000000000", "read", time.Hour)
		},
		Engine: []EngineRule{{Method: "POST", Path: "^/scripts/storage/test$", Body: map[string]any{"ok": true, "message": "fine", "can_list": false}}},
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
			for _, p := range []string{"admin", "admin_group", "obs_flag", "obs_group", "editor", "writer", "nobody"} {
				add(p+" reads the section", "GET", "/admin/scripting", p, nil)
				add(p+" reads an environment", "GET", fmt.Sprintf("/admin/scripting/environments/%d", id("e1")), p, nil)
				add(p+" reads the editor's list", "GET", "/scripting", p, nil)
				for _, e := range []string{"e1", "e2", "e3", "e4"} {
					add(p+" reads completions of "+e, "GET", fmt.Sprintf("/scripting/environments/%d/completions", id(e)), p, nil)
				}
			}
			add("an environment that does not exist", "GET", "/admin/scripting/environments/999999", "admin", nil)
			add("an environment by a word", "GET", "/admin/scripting/environments/x", "admin", nil)
			add("completions of nothing", "GET", "/scripting/environments/999999/completions", "writer", nil)
			add("the section by a read token", "GET", "/admin/scripting", "", nil)
			s[len(s)-1].Auth = Bearer("tab_sc_admin_read_0000000000")
			add("the section by a write token", "GET", "/admin/scripting", "", nil)
			s[len(s)-1].Auth = Bearer("tab_sc_admin_write_000000000")

			// the bucket's test, before its secret changes
			test := func(name, p string, engine []EngineRule) {
				s = append(s, Step{Name: name, Method: "POST", Path: "/admin/scripting/storage/test", Auth: as(p), Engine: engine})
			}
			test("admin tests the bucket", "admin", nil)
			test("the bucket refused", "admin", []EngineRule{{Method: "POST", Path: "^/scripts/storage/test$", Status: 400,
				Body: map[string]any{"detail": strings.Repeat("x", 600)}}})
			test("the bucket refused without detail", "admin", []EngineRule{{Method: "POST", Path: "^/scripts/storage/test$", Status: 502,
				Body: map[string]any{"error": 1}}})
			test("the engine answers nothing", "admin", []EngineRule{{Method: "POST", Path: "^/scripts/storage/test$", Body: map[string]any{}}})
			test("the engine answers ok as a word", "admin", []EngineRule{{Method: "POST", Path: "^/scripts/storage/test$",
				Body: map[string]any{"ok": "yes", "message": "m", "can_list": 1}}})
			test("an observer tests the bucket", "obs_flag", nil)
			test("nobody tests the bucket", "nobody", nil)
			s = append(s, Step{Name: "a token tests the bucket", Method: "POST", Path: "/admin/scripting/storage/test",
				Auth: Bearer("tab_sc_admin_write_000000000"), Header: map[string]string{"Accept-Language": "fr-CH"}})

			// the bucket
			put := func(name, p string, body any) { add(name, "PUT", "/admin/scripting/storage", p, body) }
			put("admin changes nothing", "admin", map[string]any{})
			put("admin changes the region", "admin", map[string]any{"region": " us ", "bucket": "sc-bkt"})
			put("admin gives a bad endpoint", "admin", map[string]any{"endpoint": "ftp://x"})
			put("admin gives an endpoint without a host", "admin", map[string]any{"endpoint": "http://:9000/"})
			put("admin gives a word as the endpoint", "admin", map[string]any{"endpoint": "minio"})
			put("admin removes the endpoint", "admin", map[string]any{"endpoint": " ", "secret_key": " "})
			put("admin sets the endpoint and a secret", "admin", map[string]any{"endpoint": "https://S3.Example.com:443/x", "secret_key": "s"})
			put("admin gives a long region", "admin", map[string]any{"region": strings.Repeat("r", 65)})
			put("an observer changes the bucket", "obs_flag", map[string]any{"region": "x"})
			put("nobody changes the bucket", "nobody", map[string]any{"region": "x"})
			s = append(s, Step{Name: "broken JSON on the bucket", Method: "PUT", Path: "/admin/scripting/storage", Raw: []byte("[1,"),
				ContentType: "application/json"})
			s = append(s, Step{Name: "a token changes the bucket", Method: "PUT", Path: "/admin/scripting/storage",
				Auth: Bearer("tab_sc_admin_write_000000000"), JSON: map[string]any{"region": 5}})

			// environments
			create := func(name, p string, body any) { add(name, "POST", "/admin/scripting/environments", p, body) }
			create("admin creates one", "admin", map[string]any{"name": " sc-new ", "description": " d ", "language": "python",
				"language_version": "3.12", "packages": "polars==1.2 # pinned\n\nrequests>=2,<3\n", "group_ids": []any{id("coders"), id("coders")}})
			create("admin creates it again, other case", "admin", map[string]any{"name": "SC-NEW", "language": "python", "language_version": "3.12"})
			create("admin creates one in R", "admin", map[string]any{"name": "sc-r2", "language": "r", "language_version": "4.5",
				"packages": "dplyr@1.1.0\narrow", "cpu_request": " 1 ", "memory_limit": "8Gi"})
			create("admin creates one in Julia", "admin", map[string]any{"name": "j", "language": "julia", "language_version": "1"})
			create("admin creates one in Python 2", "admin", map[string]any{"name": "j", "language": "python", "language_version": "2.7"})
			create("admin creates one with a bad package", "admin", map[string]any{"name": "j", "language": "python", "language_version": "3.12",
				"packages": "ok\nrm -rf /"})
			create("admin creates one with TABULARIA_ as a package", "admin", map[string]any{"name": "j", "language": "python",
				"language_version": "3.12", "packages": "tabularia_x"})
			create("admin creates one with too many packages", "admin", map[string]any{"name": "j", "language": "python",
				"language_version": "3.12", "packages": strings.Repeat("p\n", 201)})
			create("admin creates one with a bad cpu", "admin", map[string]any{"name": "j", "language": "python", "language_version": "3.12",
				"cpu_limit": "2 cores"})
			create("admin creates one with an unknown group", "admin", map[string]any{"name": "sc-ghost", "language": "python",
				"language_version": "3.12", "group_ids": []any{999999}})
			create("admin creates one with a blank name", "admin", map[string]any{"name": "  ", "language": "python", "language_version": "3.12"})
			create("admin creates one with a short timeout", "admin", map[string]any{"name": "j", "language": "python",
				"language_version": "3.12", "max_timeout_seconds": 59})
			create("admin creates one after the ghost", "admin", map[string]any{"name": "sc-after", "language": "python", "language_version": "3.13"})
			create("an observer creates one", "obs_flag", map[string]any{"name": "j", "language": "python", "language_version": "3.12"})
			create("a missing body", "admin", nil)

			change := func(name, env, p string, body any) {
				add(name, "PUT", fmt.Sprintf("/admin/scripting/environments/%s", env), p, body)
			}
			e1, e2, e4 := fmt.Sprint(id("e1")), fmt.Sprint(id("e2")), fmt.Sprint(id("e4"))
			change("admin changes nothing", e1, "admin", map[string]any{})
			change("admin changes the same values", e1, "admin", map[string]any{"name": " Sc-Python ", "enabled": true,
				"group_ids": []any{Groups["Analysts"], id("coders")}})
			change("admin changes many things", e1, "admin", map[string]any{"name": "sc-python", "description": "new", "enabled": false,
				"cpu_request": "250m", "max_timeout_seconds": 120, "group_ids": []any{id("coders")}})
			change("admin changes the packages", e1, "admin", map[string]any{"packages": "polars"})
			change("admin changes the version", e1, "admin", map[string]any{"language_version": "3.14"})
			change("admin renames onto another", e1, "admin", map[string]any{"name": "SC-R"})
			change("admin gives a bad memory", e1, "admin", map[string]any{"memory_limit": "lots"})
			change("admin gives unknown groups and bad packages", e1, "admin", map[string]any{"group_ids": []any{999999}, "packages": "a b"})
			change("admin gives bad packages", e1, "admin", map[string]any{"packages": "a b"})
			change("admin gives an unknown version", e1, "admin", map[string]any{"language_version": "9"})
			change("admin changes one with no version", e4, "admin", map[string]any{"description": "x"})
			change("admin gives one with no version a version", e4, "admin", map[string]any{"language_version": "3.12"})
			change("admin changes R's version", e2, "admin", map[string]any{"language_version": "4.6"})
			change("admin changes nothing there", "999999", "admin", map[string]any{})
			change("an observer changes", e1, "obs_flag", map[string]any{})

			add("admin rebuilds e1", "POST", fmt.Sprintf("/admin/scripting/environments/%d/rebuild", id("e1")), "admin", nil)
			add("admin rebuilds e1 again (queued)", "POST", fmt.Sprintf("/admin/scripting/environments/%d/rebuild", id("e1")), "admin", nil)
			add("admin rebuilds an empty one", "POST", fmt.Sprintf("/admin/scripting/environments/%d/rebuild", id("e6")), "admin", nil)
			add("admin rebuilds nothing", "POST", "/admin/scripting/environments/999999/rebuild", "admin", nil)
			current := func(name, env, v string) {
				add(name, "POST", fmt.Sprintf("/admin/scripting/environments/%v/versions/%v/current", env, v), "admin", nil)
			}
			current("admin makes v1 current", e1, fmt.Sprint(id("v11")))
			current("admin makes the failed one current", e1, fmt.Sprint(id("v12")))
			current("admin makes another's version current", e1, fmt.Sprint(id("v31")))
			current("admin makes a word current", e1, "w")
			current("admin makes R's building one current", e2, fmt.Sprint(id("v21")))
			current("admin makes v31 current on e3 again", fmt.Sprint(id("e3")), fmt.Sprint(id("v31")))

			// variables
			setVar := func(name, env, v, p string, body any) {
				add(name, "PUT", fmt.Sprintf("/admin/scripting/environments/%s/variables/%s", env, v), p, body)
			}
			setVar("admin sets a plain one", e1, "PLAIN", "admin", map[string]any{"value": " a b "})
			setVar("admin sets a secret one", e1, "SECRET", "admin", map[string]any{"value": "s", "secret": true})
			setVar("admin keeps the secret", e1, "SECRET", "admin", map[string]any{"secret": true})
			setVar("admin keeps a plain one's value", e1, "PLAIN", "admin", map[string]any{})
			setVar("admin hands over one", e1, "HANDED2", "admin", map[string]any{"value": " TABULARIA_RUN_ID ", "from_tabularia": true, "secret": true})
			setVar("admin hands over a bad name", e1, "HANDED3", "admin", map[string]any{"value": "run id", "from_tabularia": true})
			setVar("admin keeps API_KEY but hands it", e1, "API_KEY", "admin", map[string]any{"secret": true, "from_tabularia": true})
			setVar("admin sets a lower name", e1, "lower", "admin", map[string]any{"value": "x"})
			setVar("admin sets a reserved name", e1, "AWS_KEY", "admin", map[string]any{"value": "x"})
			setVar("admin sets a long name", e1, strings.Repeat("A", 129), "admin", map[string]any{"value": "x"})
			setVar("admin sets one on nothing", "999999", "X", "admin", map[string]any{"value": "x"})
			setVar("an observer sets one", e1, "X", "obs_flag", map[string]any{"value": "x"})
			add("admin deletes a variable", "DELETE", fmt.Sprintf("/admin/scripting/environments/%d/variables/REGION", id("e1")), "admin", nil)
			add("admin deletes it again", "DELETE", fmt.Sprintf("/admin/scripting/environments/%d/variables/REGION", id("e1")), "admin", nil)
			add("admin deletes one on nothing", "DELETE", "/admin/scripting/environments/999999/variables/REGION", "admin", nil)

			// deletes
			del := func(name, env, p string) {
				add(name, "DELETE", fmt.Sprintf("/admin/scripting/environments/%s", env), p, nil)
			}
			del("admin deletes one a flow uses", fmt.Sprint(id("e5")), "admin")
			del("admin deletes one a script uses", fmt.Sprint(id("e6")), "admin")
			del("admin deletes e4", e4, "admin")
			del("admin deletes e4 again", e4, "admin")
			del("an observer deletes e1", e1, "obs_flag")
			del("a word", "x", "admin")
			add("the section at the end", "GET", "/admin/scripting", "admin", nil)
			add("the editor's list at the end", "GET", "/scripting", "writer", nil)
			add("forwarded: the packages", "GET", "/admin/scripting/packages?language=cobol", "admin", nil)
			add("forwarded: the lint", "POST", "/scripting/lint", "writer", map[string]any{"environment_id": id("e1"), "path": "a.R", "content": "x"})
			return s
		},
	})
}
